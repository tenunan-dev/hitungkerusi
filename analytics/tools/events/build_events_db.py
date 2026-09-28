#!/usr/bin/env python3
"""Build the GE16 relational events database (SQLite + FTS5).

    python tools/events/build_events_db.py [--dossier-dir DIR] [--draft-dir DIR ...] [--out PATH]

Inputs (read-only):
  * sweep dossiers   <dossier-dir>/ge16-sweep-partA-federal.md
                     <dossier-dir>/ge16-sweep-partB-states.json
                     <dossier-dir>/ge16-sweep-partC-polls-macro.json
  * cron update layers  <draft-dir>/party-updates-*.json, personnel-updates-*.json,
                     polls-update-*.json
  * identity stores  work/graph/ge16-knowledge-graph.json (node ids),
                     work/figures/ge16-personnel.json, work/figures/ge16-parties.json,
                     <data-root>/research/derived/master-list-222-parliamentary-seats.csv,
                     <data-root>/research/derived/dun_to_parliament_mapping.json

Output:
  work/events/ge16-events.db          the database (schema.sql, FTS5 index)
  work/events/ge16-events-stats.json  build report + validation results

The build is deterministic: the same inputs produce the same event ids, the same
rows and the same counts. It fails (exit 1) when a validation check fails, so a
broken store never looks like a good one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

try:  # package import
    from . import extractors as ex
    from . import stories as story_ledger
    from .entity_index import (POLLSTER_IDS, EntityIndex, build_index, mask_urls,
                               publisher_for, slug, url_published_date)
except ImportError:  # direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import extractors as ex
    import stories as story_ledger
    from entity_index import (POLLSTER_IDS, EntityIndex, build_index, mask_urls,
                              publisher_for, slug, url_published_date)

SCHEMA_VERSION = "2"
SCHEMA_PATH = Path(__file__).with_name("schema.sql")

#: richer/typed layers win a de-duplication contest against dossier prose
LAYER_PRIORITY = {"cron-update": 0, "dossier": 1}
#: federal territories are not states: a record about Putrajaya/Parliament is federal
FEDERAL_TERRITORIES = {"putrajaya", "kuala lumpur", "labuan", "wp kuala lumpur", "wp putrajaya"}
#: types that are inherently state-level, so a state named up front sets jurisdiction
STATE_LEVEL_TYPES = {"state_election", "state_poll", "dissolution", "by_election", "vacancy",
                     "mb_change", "election_result", "composition_change"}
#: event types that can trigger a by-election / EC or party reaction
TRIGGER_TYPES = {"vacancy", "resignation", "death", "expulsion", "dissolution", "party_switch"}
SEAT_TOKEN_RE = re.compile(r"^(P\d{3}|N\.\d{1,2})")
#: seat codes written the way prose writes them: '(P104)', 'seat P104', 'assemblyman (N.13)'
SEAT_DECLARED_RE = re.compile(
    r"(?:\(\s*|(?:seat|constituency|dun|parliamentary|parlimen|mp for|assemblyman(?: for)?"
    r"|adun(?: for)?|mp)\s*[:\-–]?\s*\(?\s*)(P\s?\d{3}|N\.?\s?\d{1,2})\b",
    re.IGNORECASE,
)


def normalise_seat_code(raw: str | None) -> str | None:
    """'p 104'/'P104' → 'P104'; 'n13'/'N.3' → 'N.03'."""
    if not raw:
        return None
    token = raw.replace(" ", "").upper()
    match = re.match(r"^([PN])\.?(\d{1,3})$", token)
    if not match:
        return None
    letter, digits = match.group(1), int(match.group(2))
    return f"{letter}{digits:03d}" if letter == "P" else f"N.{digits:02d}"


def declared_seat_code(record) -> str | None:
    """The seat the record itself claims: its seat field, then title, then opening prose."""
    for source in (record.seat_code, record.title, (record.detail or "")[:260]):
        if not source:
            continue
        match = SEAT_TOKEN_RE.match(str(source).strip()) or SEAT_DECLARED_RE.search(str(source))
        code = normalise_seat_code(match.group(1)) if match else None
        if code:
            return code
    return None
SEAT_CHAIN_TYPES = {"vacancy", "resignation", "death", "by_election", "election_result",
                    "dissolution", "state_election", "appointment", "expulsion", "ec_action"}
_SIGNIFICANT_TOKEN = re.compile(r"[a-z0-9]{4,}")


def _signature(text: str) -> set[str]:
    """Significant-token signature used by the near-duplicate pass."""
    return set(_SIGNIFICANT_TOKEN.findall(text.lower()))


def resolve_repository_root(anchor=None) -> Path:
    """Find the ANALYTICS repository root independently of cwd."""
    return Path(anchor or __file__).resolve().parents[2]


# ----------------------------------------------------------------- inputs -----

def discover_dossiers(directory: Path) -> dict[str, Path]:
    """Locate the three sweep dossiers (exact names first, then by prefix)."""
    found: dict[str, Path] = {}
    patterns = {
        "partA": ("ge16-sweep-partA-federal.md", "ge16-sweep-partA*.md"),
        "partB": ("ge16-sweep-partB-states.json", "ge16-sweep-partB*.json"),
        "partC": ("ge16-sweep-partC-polls-macro.json", "ge16-sweep-partC*.json"),
    }
    for key, (exact, glob) in patterns.items():
        candidate = directory / exact
        if candidate.exists():
            found[key] = candidate
            continue
        matches = sorted(directory.glob(glob))
        if matches:
            found[key] = matches[0]
    return found


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def extract_all(dossiers: dict[str, Path], draft_dirs: list[Path], use_drafts: bool) -> tuple[ex.Extraction, dict]:
    extraction = ex.Extraction()
    inputs: dict[str, dict] = {}
    for key, path in dossiers.items():
        if key == "partA":
            part = ex.read_parta(path)
        elif key == "partB":
            part = ex.read_partb(path)
        else:
            part = ex.read_partc(path)
        extraction.extend(part)
        inputs[path.name] = {
            "path": str(path), "sha256": file_sha256(path),
            "records": len(part.records), "claims": len(part.claims), "notes": len(part.notes),
        }
    if use_drafts:
        for directory in draft_dirs:
            if not directory.exists():
                continue
            part = ex.read_drafts(directory)
            extraction.extend(part)
            for path in sorted(list(directory.glob("party-updates-*.json")) +
                               list(directory.glob("personnel-updates-*.json")) +
                               list(directory.glob("polls-update-*.json"))):
                inputs[path.name] = {
                    "path": str(path), "sha256": file_sha256(path), "layer": "cron-update",
                }
    return extraction, inputs


# ------------------------------------------------------------ resolution ------

class Resolver:
    """Attach canonical entity ids, seat codes and jurisdiction to a record."""

    def __init__(self, index: EntityIndex):
        self.index = index
        self.entities = index.entities

    def resolve(self, record: ex.Record) -> dict:
        text = record.detail or record.text
        matches = self.index.resolve(text, state=record.state)
        by_role: dict[str, list[str]] = {"actor": [], "seat": [], "state": [], "bloc": []}
        ordered: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for match in matches:
            entity = self.entities.get(match["entity_id"])
            if not entity:
                continue
            role = match["role"]
            if entity["entity_type"] == "institution":
                role = "pollster" if match["entity_id"] in POLLSTER_IDS else "institution"
            key = (match["entity_id"], role)
            if key in seen:
                continue
            seen.add(key)
            ordered.append({"entity_id": match["entity_id"], "role": role,
                            "mention": match["mention"], "position": match["position"],
                            "entity_type": entity["entity_type"]})
            by_role.setdefault(role, [])
            if role in by_role:
                by_role[role].append(match["entity_id"])

        declared_seat = declared_seat_code(record)
        seat_entity = None
        for match in ordered:
            if match["role"] != "seat":
                continue
            candidate = self.entities[match["entity_id"]]
            code = normalise_seat_code(match["entity_id"].split(":", 1)[1].split(" ")[0])
            if declared_seat and code == declared_seat:
                seat_entity = candidate
                break
            if seat_entity is None:
                seat_entity = candidate
        seat_code = None
        if seat_entity:
            seat_code = normalise_seat_code(seat_entity["entity_id"].split(":", 1)[1].split(" ")[0])
        if seat_code is None and record.seat_code:
            seat_code = normalise_seat_code(record.seat_code.strip())

        state = record.state
        if not state and seat_entity:
            state = seat_entity["attrs"].get("state") or None
        declared_state = bool(record.state)
        if not state:
            # a state *mentioned* in the prose is context, not the record's own level
            for match in ordered:
                if match["role"] == "state":
                    state = self.entities[match["entity_id"]]["name"]
                    break
        federal_territory = (state or "").strip().lower() in FEDERAL_TERRITORIES

        primary = None
        for match in ordered:
            if match["role"] == "actor" and match["entity_type"] == "person":
                primary = match["entity_id"]
                break
        if primary is None:
            for match in ordered:
                if match["role"] == "actor":
                    primary = match["entity_id"]
                    break

        jurisdiction = record.jurisdiction or "federal"
        if seat_entity and seat_entity["attrs"].get("kind") == "dun":
            jurisdiction = f"state:{state}" if state else "state:unknown"
        elif seat_entity:
            jurisdiction = "federal"
        elif declared_state and state and not federal_territory and jurisdiction in ("federal", ""):
            jurisdiction = f"state:{state}"
        elif (not declared_state and state and not federal_territory
              and record.event_type in STATE_LEVEL_TYPES
              and any(m["role"] == "state" and m["position"] < 160 for m in ordered)
              and jurisdiction in ("federal", "")):
            # "GRS: … 17th Sabah state election" is a Sabah event: the state is named
            # in the opening line and the type is inherently state-level.
            jurisdiction = f"state:{state}"
        elif federal_territory and jurisdiction.startswith("state:"):
            jurisdiction = "federal"
        elif jurisdiction.startswith("state:") or jurisdiction in ("federal", "national", "external"):
            pass
        else:
            jurisdiction = "federal"

        return {
            "entities": ordered[:16],
            "actors": by_role.get("actor", [])[:8],
            "seat_code": seat_code,
            "state": state,
            "jurisdiction": jurisdiction,
            "primary_actor": primary,
        }


def event_key(record: ex.Record, resolved: dict) -> str:
    """Deterministic de-duplication key: when + what + where + who."""
    when = record.event_date or "undated"
    actor = resolved["primary_actor"] or slug(record.title)[:48]
    key = f"{when}|{record.event_type}|{resolved['seat_code'] or ''}|{actor}"
    if when == "undated":
        key += "|" + slug(record.title)[:64]
    return key


def merge_records(records: list[ex.Record], resolver: Resolver) -> tuple[list[dict], int]:
    """Resolve every record, then collapse duplicates onto one event row."""
    prepared = []
    for record in records:
        resolved = resolver.resolve(record)
        prepared.append({
            "record": record, "resolved": resolved,
            "key": event_key(record, resolved),
            "rank": (LAYER_PRIORITY.get(record.source_layer, 9), -len(record.urls), -len(record.detail)),
        })
    prepared.sort(key=lambda item: item["rank"])

    events: dict[str, dict] = {}
    merged = 0

    def absorb(entry: dict, item: dict) -> None:
        record = item["record"]
        entry["members"].append(item)
        for url in record.urls:
            if url not in entry["urls"]:
                entry["urls"].append(url)
        for metric, value in record.metrics.items():
            entry["metrics"].setdefault(metric, value)

    for item in prepared:
        entry = events.get(item["key"])
        if entry is None:
            events[item["key"]] = {"key": item["key"], "primary": item, "members": [item],
                                   "urls": list(item["record"].urls),
                                   "metrics": dict(item["record"].metrics)}
            continue
        merged += 1
        absorb(entry, item)

    # Second pass: the same fact is often phrased differently by different layers
    # ("PN floated abolishing the chairman post" vs "PN: 01-28: PN floated …"), so
    # events that share a date, actor and seat and whose titles overlap by >= 70%
    # of their significant tokens collapse onto the first (best-ranked) record.
    anchors: dict[str, list[tuple[str, set[str]]]] = {}
    for key, entry in list(events.items()):
        record = entry["primary"]["record"]
        resolved = entry["primary"]["resolved"]
        if not record.event_date:
            continue
        anchor = f"{record.event_date}|{resolved['primary_actor'] or ''}|{resolved['seat_code'] or ''}"
        signature = _signature(record.title)
        duplicate_of = None
        for other_key, other_signature in anchors.setdefault(anchor, []):
            union = signature | other_signature
            if union and len(signature & other_signature) / len(union) >= 0.7:
                duplicate_of = other_key
                break
        if duplicate_of is None:
            anchors.setdefault(anchor, []).append((key, signature))
            continue
        target = events[duplicate_of]
        for member in entry["members"]:
            absorb(target, member)
        for url in entry["urls"]:
            if url not in target["urls"]:
                target["urls"].append(url)
        for metric, value in entry["metrics"].items():
            target["metrics"].setdefault(metric, value)
        merged += 1
        del events[key]

    # Third pass: the same event reported once with a seat and once without ("Bung
    # Moktar Radin died 5 Dec 2025" vs the same death naming Kinabatangan). Fold the
    # seat-less record into the seated one so the seat and state carry across layers.
    groups: dict[tuple, list[str]] = {}
    for key, entry in list(events.items()):
        record = entry["primary"]["record"]
        resolved = entry["primary"]["resolved"]
        if not record.event_date or not resolved["primary_actor"]:
            continue
        groups.setdefault((record.event_date, record.event_type, resolved["primary_actor"]),
                          []).append(key)
    for keys in groups.values():
        if len(keys) < 2:
            continue
        seated = [k for k in keys if events[k]["primary"]["resolved"]["seat_code"]]
        seatless = [k for k in keys if not events[k]["primary"]["resolved"]["seat_code"]]
        if len(seated) != 1 or not seatless:
            continue
        target = events[seated[0]]
        target_signature = _signature(target["primary"]["record"].title)
        for key in seatless:
            entry = events[key]
            if len(target_signature & _signature(entry["primary"]["record"].title)) < 2:
                continue
            if os.environ.get("GE16_EVENTS_DEBUG_MERGE"):
                print(f"  [merge:seatless] {entry['primary']['record'].title[:70]!r}"
                      f" → {target['primary']['record'].title[:70]!r}")
            for member in entry["members"]:
                absorb(target, member)
            for url in entry["urls"]:
                if url not in target["urls"]:
                    target["urls"].append(url)
            for metric, value in entry["metrics"].items():
                target["metrics"].setdefault(metric, value)
            merged += 1
            del events[key]
    return list(events.values()), merged


# ------------------------------------------------------------------ output -----

def event_id_for(key: str) -> str:
    return "evt-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def claim_id_for(claim: ex.ClaimReview) -> str:
    seed = f"{claim.dossier}|{claim.section}|{claim.claim_text[:120]}"
    return "claim-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]


def domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except ValueError:
        return ""


def write_database(db_path: Path, extraction: ex.Extraction, resolver: Resolver,
                   inputs: dict, warnings: list[str], builder_sha: str) -> dict:
    started = time.time()
    built_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    resolved_events, merged = merge_records(extraction.records, resolver)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = db_path.with_name(db_path.name + ".tmp")
    if tmp_path.exists():
        tmp_path.unlink()
    conn = sqlite3.connect(tmp_path)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.execute("PRAGMA foreign_keys = ON")

    # ---- sources -------------------------------------------------------------
    source_ids: dict[str, int] = {}
    first_seen = datetime.now(timezone.utc).date().isoformat()

    def source_id(url: str, dossier: str) -> int:
        if url in source_ids:
            return source_ids[url]
        publisher, tier = publisher_for(url)
        cursor = conn.execute(
            "INSERT OR IGNORE INTO sources (url, domain, publisher, tier, published_date, first_seen, dossier)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (url, domain_of(url), publisher, tier, url_published_date(url), first_seen, dossier),
        )
        row = conn.execute("SELECT source_id FROM sources WHERE url = ?", (url,)).fetchone()
        source_id_value = int(row[0])
        source_ids[url] = source_id_value
        return source_id_value

    # ---- entities ------------------------------------------------------------
    conn.executemany(
        "INSERT OR REPLACE INTO entities (entity_id, entity_type, name, kg_node_id, aliases_json, attrs_json)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [(e["entity_id"], e["entity_type"], e["name"], e["kg_node_id"],
          json.dumps(e["aliases"], ensure_ascii=False), json.dumps(e["attrs"], ensure_ascii=False))
         for e in resolver.entities.values()],
    )

    # ---- events --------------------------------------------------------------
    event_rows, entity_rows, metric_rows, source_rows, raw_events = [], [], [], [], []
    for entry in sorted(resolved_events, key=lambda item: (item["primary"]["record"].event_date or "9999",
                                                           item["primary"]["record"].event_type)):
        record: ex.Record = entry["primary"]["record"]
        resolved = entry["primary"]["resolved"]
        event_id = event_id_for(entry["key"])
        urls = entry["urls"]
        primary_source = source_id(urls[0], record.dossier) if urls else None
        window = ex.window_class(record.event_date)
        raw = dict(record.raw or {})
        raw.update({"section": record.section, "type_basis": record.type_basis,
                    "date_lineage": record.date_lineage, "text": record.text[:2000],
                    "merged_records": len(entry["members"]),
                    "layers": sorted({member["record"].source_layer for member in entry["members"]})})
        event_rows.append((
            event_id, record.event_date, record.event_date_end, record.date_precision,
            record.date_source, record.event_type, record.subtype, record.title, record.detail,
            resolved["jurisdiction"], resolved["state"], resolved["seat_code"],
            record.significance, record.confidence, window, record.dossier, record.section,
            record.source_layer, primary_source, len(entry["members"]), len(urls),
            json.dumps(raw, ensure_ascii=False), built_at,
        ))
        for match in resolved["entities"]:
            entity_rows.append((event_id, match["entity_id"], match["role"],
                                match["mention"], match["position"]))
        for metric, value in entry["metrics"].items():
            value_num, value_text, unit = (list(value) + [None, None, None])[:3]
            metric_rows.append((event_id, metric, value_num, value_text, unit))
        for index, url in enumerate(urls):
            source_rows.append((event_id, source_id(url, record.dossier),
                                "primary" if index == 0 else "corroborating"))
        raw_events.append({"event_id": event_id, "event_date": record.event_date,
                           "event_type": record.event_type, "seat_code": resolved["seat_code"],
                           "title": record.title})

    conn.executemany(
        "INSERT INTO events (event_id, event_date, event_date_end, date_precision, date_source,"
        " event_type, subtype, title, detail, jurisdiction, state, seat_code, significance,"
        " confidence, window_class, dossier, section, source_layer, primary_source_id,"
        " corroboration_count, source_count, raw_json, created_at)"
        " VALUES (" + ",".join("?" * 23) + ")", event_rows)
    conn.executemany(
        "INSERT OR IGNORE INTO event_entities (event_id, entity_id, role, mention, position)"
        " VALUES (?, ?, ?, ?, ?)", entity_rows)
    conn.executemany(
        "INSERT OR REPLACE INTO event_metrics (event_id, metric, value_num, value_text, unit)"
        " VALUES (?, ?, ?, ?, ?)", metric_rows)
    conn.executemany(
        "INSERT OR IGNORE INTO event_sources (event_id, source_id, relation) VALUES (?, ?, ?)",
        source_rows)

    # ---- event-to-event relations -------------------------------------------
    def insert_links(rows):
        conn.executemany(
            "INSERT OR IGNORE INTO event_links (from_event_id, to_event_id, relation, basis, gap_days)"
            " VALUES (?, ?, ?, ?, ?)", rows)

    by_seat: dict[str, list[dict]] = {}
    for row in raw_events:
        if row["seat_code"] and row["event_date"]:
            by_seat.setdefault(row["seat_code"], []).append(row)
    links: list[tuple] = []
    for seat, rows in by_seat.items():
        rows.sort(key=lambda item: (item["event_date"], item["event_id"]))
        for earlier, later in zip(rows, rows[1:]):
            gap = _day_gap(earlier["event_date"], later["event_date"])
            if gap is not None and gap <= 400:
                links.append((earlier["event_id"], later["event_id"], "precedes",
                              f"consecutive events on {seat}", gap))
        for row in rows:
            if row["event_type"] != "by_election":
                continue
            triggers = [candidate for candidate in rows
                        if candidate["event_date"] <= row["event_date"]
                        and candidate["event_type"] in TRIGGER_TYPES
                        and (candidate["event_date"], candidate["event_id"]) < (row["event_date"], row["event_id"])]
            if triggers:
                trigger = triggers[-1]
                links.append((trigger["event_id"], row["event_id"], "follows",
                              f"by-election follows {trigger['event_type']} on {seat}",
                              _day_gap(trigger["event_date"], row["event_date"])))
    # a party switch within 180 days of the person's own resignation/expulsion
    switches = [row for row in raw_events if row["event_type"] == "party_switch"]
    for switch in switches:
        people = conn.execute(
            "SELECT ee.entity_id, e2.event_date, e2.event_type, e2.event_id FROM event_entities ee"
            " JOIN events e2 ON e2.event_id = ee.event_id"
            " WHERE ee.event_id = ? AND ee.role = 'actor'", (switch["event_id"],)).fetchall()
        for entity_id, _, _, _ in people:
            candidates = conn.execute(
                "SELECT e3.event_id, e3.event_date, e3.event_type FROM event_entities ee2"
                " JOIN events e3 ON e3.event_id = ee2.event_id"
                " WHERE ee2.entity_id = ? AND e3.event_type IN ('resignation','expulsion')"
                "   AND e3.event_date IS NOT NULL AND e3.event_date <= ?"
                " ORDER BY e3.event_date DESC LIMIT 1",
                (entity_id, switch["event_date"] or "9999-12-31")).fetchall()
            for candidate_id, candidate_date, candidate_type in candidates:
                gap = _day_gap(candidate_date, switch["event_date"])
                if gap is not None and gap <= 180:
                    links.append((candidate_id, switch["event_id"], "follows",
                                  f"party switch follows {candidate_type} by the same person", gap))
    insert_links(sorted(set(links)))

    # ---- claims + notes ------------------------------------------------------
    claim_rows = []
    for claim in extraction.claims:
        urls = [url for url in claim.urls if url]
        claim_rows.append((
            claim_id_for(claim), claim.dossier, claim.section, claim.kind, claim.claim_text,
            claim.baseline_value, claim.verified_value, claim.verdict, claim.confidence,
            source_id(urls[0], claim.dossier) if urls else None,
        ))
    conn.executemany(
        "INSERT OR IGNORE INTO claim_reviews (claim_id, dossier, section, kind, claim_text,"
        " baseline_value, verified_value, verdict, confidence, source_id)"
        " VALUES (" + ",".join("?" * 10) + ")", claim_rows)
    note_rows = []
    for note in extraction.notes:
        urls = [url for url in note.urls if url]
        note_rows.append((note.dossier, note.section, note.kind, note.body,
                          source_id(urls[0], note.dossier) if urls else None))
    conn.executemany(
        "INSERT INTO dossier_notes (dossier, section, kind, body, source_id) VALUES (?, ?, ?, ?, ?)",
        note_rows)

    # ---- full-text layer + entity rollups ------------------------------------
    conn.execute(
        "INSERT INTO events_fts (event_id, title, detail, entities, event_type, jurisdiction, source_title)"
        " SELECT e.event_id, e.title, e.detail,"
        "        COALESCE((SELECT GROUP_CONCAT(en.name, ' | ') FROM event_entities ee"
        "                    JOIN entities en ON en.entity_id = ee.entity_id"
        "                   WHERE ee.event_id = e.event_id), ''),"
        "        e.event_type, e.jurisdiction, COALESCE(s.publisher || ' — ' || s.url, '')"
        "   FROM events e LEFT JOIN sources s ON s.source_id = e.primary_source_id")
    conn.execute(
        "INSERT INTO entities_fts (entity_id, entity_type, name, aliases, summary)"
        " SELECT en.entity_id, en.entity_type, en.name,"
        "        REPLACE(REPLACE(REPLACE(en.aliases_json, '[', ''), ']', ''), '\"', ''),"
        "        COALESCE(en.attrs_json, '') || ' ' ||"
        "        COALESCE((SELECT GROUP_CONCAT(DISTINCT e.event_type) FROM event_entities ee"
        "                    JOIN events e ON e.event_id = ee.event_id"
        "                   WHERE ee.entity_id = en.entity_id), '')"
        "   FROM entities en")
    conn.execute(
        "UPDATE entities SET"
        " event_count = (SELECT COUNT(*) FROM event_entities ee WHERE ee.entity_id = entities.entity_id),"
        " first_event_date = (SELECT MIN(e.event_date) FROM event_entities ee"
        "                       JOIN events e ON e.event_id = ee.event_id"
        "                      WHERE ee.entity_id = entities.entity_id),"
        " last_event_date = (SELECT MAX(e.event_date) FROM event_entities ee"
        "                       JOIN events e ON e.event_id = ee.event_id"
        "                      WHERE ee.entity_id = entities.entity_id)")

    # ---- story ledger (narrative layer, append-only) --------------------------
    # Clustered from what was just written, so the ledger and the events always
    # agree. Ids already published by a previous build of this database are
    # inherited, so appending a later event extends its story instead of
    # spawning a second one.
    prior_stories = story_ledger.read_prior_mapping(db_path)
    story_stats = story_ledger.write_stories(conn, prior=prior_stories, created_at=built_at)

    # ---- rollups + validation ------------------------------------------------
    counts = _counts(conn)
    counts["story_ledger"] = story_stats
    checks = _validate(conn, counts)
    counts["event_links"] = {"follows": _scalar(conn, "SELECT COUNT(*) FROM event_links WHERE relation='follows'"),
                            "precedes": _scalar(conn, "SELECT COUNT(*) FROM event_links WHERE relation='precedes'")}
    ok = all(check["ok"] for check in checks)
    conn.execute(
        "INSERT INTO ingest_runs (built_at, builder, builder_sha256, schema_version, source_window,"
        " counts_json, inputs_json, warnings_json, ok) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (built_at, "tools/events/build_events_db.py", builder_sha, SCHEMA_VERSION,
         f"{ex.WINDOW_START}..{ex.WINDOW_END}", json.dumps(counts, default=str),
         json.dumps(inputs, default=str), json.dumps(warnings, default=str), int(ok)))
    conn.execute("INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                 ("schema_version", SCHEMA_VERSION))
    conn.execute("INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                 ("built_at", built_at))
    conn.execute("INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                 ("source_window", f"{ex.WINDOW_START}..{ex.WINDOW_END}"))
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    os.replace(tmp_path, db_path)

    report = {
        "db_path": str(db_path),
        "built_at": built_at,
        "build_seconds": round(time.time() - started, 2),
        "schema_version": SCHEMA_VERSION,
        "counts": counts,
        "checks": checks,
        "ok": ok,
        "warnings": warnings,
        "inputs": inputs,
        "records_in": len(extraction.records),
        "records_merged": merged,
        "claims_in": len(extraction.claims),
        "notes_in": len(extraction.notes),
    }
    return report


def _day_gap(earlier: str | None, later: str | None) -> int | None:
    if not earlier or not later:
        return None
    try:
        first = datetime.fromisoformat(earlier[:10])
        second = datetime.fromisoformat(later[:10])
    except ValueError:
        return None
    return abs((second - first).days)


def _scalar(conn, sql: str):
    row = conn.execute(sql).fetchone()
    return row[0] if row else None


def _counts(conn) -> dict:
    def grouped(sql: str) -> dict:
        return {str(key): value for key, value in conn.execute(sql).fetchall()}

    counts = {
        "events": _scalar(conn, "SELECT COUNT(*) FROM events"),
        "entities": _scalar(conn, "SELECT COUNT(*) FROM entities"),
        "entities_linked_to_kg": _scalar(conn, "SELECT COUNT(*) FROM entities WHERE kg_node_id IS NOT NULL"),
        "event_entity_rows": _scalar(conn, "SELECT COUNT(*) FROM event_entities"),
        "event_metrics": _scalar(conn, "SELECT COUNT(*) FROM event_metrics"),
        "sources": _scalar(conn, "SELECT COUNT(*) FROM sources"),
        "event_sources": _scalar(conn, "SELECT COUNT(*) FROM event_sources"),
        "claim_reviews": _scalar(conn, "SELECT COUNT(*) FROM claim_reviews"),
        "dossier_notes": _scalar(conn, "SELECT COUNT(*) FROM dossier_notes"),
        "events_fts": _scalar(conn, "SELECT COUNT(*) FROM events_fts"),
        "entities_fts": _scalar(conn, "SELECT COUNT(*) FROM entities_fts"),
        "events_without_source": _scalar(conn, "SELECT COUNT(*) FROM events WHERE primary_source_id IS NULL"),
        "events_without_entity": _scalar(conn, "SELECT COUNT(*) FROM events WHERE event_id NOT IN"
                                              " (SELECT DISTINCT event_id FROM event_entities)"),
        "events_undated": _scalar(conn, "SELECT COUNT(*) FROM events WHERE date_precision = 'unknown'"),
        "events_multi_source": _scalar(conn, "SELECT COUNT(*) FROM events WHERE corroboration_count > 1"),
        "by_type": grouped("SELECT event_type, COUNT(*) FROM events GROUP BY event_type ORDER BY 2 DESC"),
        "by_dossier": grouped("SELECT dossier, COUNT(*) FROM events GROUP BY dossier ORDER BY 2 DESC"),
        "by_window": grouped("SELECT window_class, COUNT(*) FROM events GROUP BY window_class"),
        "by_significance": grouped("SELECT significance, COUNT(*) FROM events GROUP BY significance"),
        "by_precision": grouped("SELECT date_precision, COUNT(*) FROM events GROUP BY date_precision"),
        "by_confidence": grouped("SELECT confidence, COUNT(*) FROM events GROUP BY confidence"),
        "entities_by_type": grouped("SELECT entity_type, COUNT(*) FROM entities GROUP BY entity_type ORDER BY 2 DESC"),
        "claims_by_kind": grouped("SELECT kind || '/' || verdict, COUNT(*) FROM claim_reviews GROUP BY 1"),
        "stories": _scalar(conn, "SELECT COUNT(*) FROM stories"),
        "story_events": _scalar(conn, "SELECT COUNT(*) FROM story_events"),
        "stories_open": _scalar(conn, "SELECT COUNT(*) FROM stories WHERE status = 'open'"),
        "stories_dormant": _scalar(conn, "SELECT COUNT(*) FROM stories WHERE status = 'dormant'"),
        "stories_closed": _scalar(conn, "SELECT COUNT(*) FROM stories WHERE status = 'closed'"),
        "largest_story_events": _scalar(conn, "SELECT COALESCE(MAX(event_count), 0) FROM stories"),
        "multi_event_stories": _scalar(conn, "SELECT COUNT(*) FROM stories WHERE event_count > 1"),
        "events_without_story": _scalar(conn, "SELECT COUNT(*) FROM events WHERE event_id NOT IN"
                                              " (SELECT event_id FROM story_events)"),
        "stories_by_theme": grouped("SELECT COALESCE(theme, '(none)'), COUNT(*) FROM stories"
                                    " GROUP BY 1 ORDER BY 2 DESC"),
        "top_entities": [{"entity_id": row[0], "name": row[1], "type": row[2], "events": row[3]}
                         for row in conn.execute(
                             "SELECT en.entity_id, en.name, en.entity_type, COUNT(*) AS n FROM event_entities ee"
                             " JOIN entities en ON en.entity_id = ee.entity_id"
                             " GROUP BY 1, 2, 3 ORDER BY n DESC, en.name LIMIT 15").fetchall()],
        "top_sources": [{"url": row[0], "publisher": row[1], "events": row[2]}
                        for row in conn.execute(
                            "SELECT s.url, s.publisher, COUNT(*) AS n FROM event_sources es"
                            " JOIN sources s ON s.source_id = es.source_id"
                            " GROUP BY 1, 2 ORDER BY n DESC, s.url LIMIT 10").fetchall()],
    }
    total = counts["events"] or 1
    counts["entity_coverage_pct"] = round(100 * (total - counts["events_without_entity"]) / total, 1)
    counts["source_coverage_pct"] = round(100 * (total - counts["events_without_source"]) / total, 1)
    counts["dated_coverage_pct"] = round(100 * (total - counts["events_undated"]) / total, 1)
    return counts


def _validate(conn, counts: dict) -> list[dict]:
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    integrity = _scalar(conn, "PRAGMA integrity_check")
    check("sqlite_integrity_check", integrity == "ok", str(integrity))
    foreign = conn.execute("PRAGMA foreign_key_check").fetchall()
    check("foreign_key_check_empty", not foreign, f"{len(foreign)} violations")
    check("events_present", counts["events"] > 0, str(counts["events"]))
    check("events_fts_matches_events", counts["events_fts"] == counts["events"],
          f"fts={counts['events_fts']} events={counts['events']}")
    check("entities_fts_matches_entities", counts["entities_fts"] == counts["entities"],
          f"fts={counts['entities_fts']} entities={counts['entities']}")
    duplicates = _scalar(conn, "SELECT COUNT(*) FROM (SELECT event_id FROM events GROUP BY event_id HAVING COUNT(*) > 1)")
    check("no_duplicate_event_ids", duplicates == 0, str(duplicates))
    check("entity_coverage", counts["entity_coverage_pct"] >= 50.0,
          f"{counts['entity_coverage_pct']}% of events carry an entity")
    check("dated_coverage", counts["dated_coverage_pct"] >= 60.0,
          f"{counts['dated_coverage_pct']}% of events carry a date")
    check("claims_present", counts["claim_reviews"] > 0, str(counts["claim_reviews"]))
    check("entity_graph_join", counts["entities_linked_to_kg"] > 0,
          f"{counts['entities_linked_to_kg']} entities carry a knowledge-graph node id")
    # story ledger: every event belongs to exactly one thread, and the threads
    # are not one giant blob (a collapsed ledger would make the report's story
    # section meaningless).
    check("stories_present", counts["stories"] > 0, str(counts["stories"]))
    check("every_event_in_one_story", counts["story_events"] == counts["events"],
          f"members={counts['story_events']} events={counts['events']}")
    check("story_membership_unique",
          _scalar(conn, "SELECT COUNT(*) FROM (SELECT event_id FROM story_events"
                        " GROUP BY 1 HAVING COUNT(*) > 1)") == 0,
          "no event appears in two stories")
    if counts["events"] >= 20:
        largest_share = counts["largest_story_events"] / (counts["events"] or 1)
        check("largest_story_below_half_corpus", largest_share < 0.5,
              f"largest story carries {counts['largest_story_events']} of {counts['events']} events")
    check("story_timeline_view_queryable",
          _scalar(conn, "SELECT COUNT(*) FROM v_story_timeline") == counts["story_events"],
          "v_story_timeline returns one row per membership")
    return checks


def build(dossiers: dict[str, Path], draft_dirs: list[Path], out_path: Path | None,
          repo_root: Path | None = None, use_drafts: bool = True, kg_path: Path | None = None,
          figures_dir: Path | None = None, data_root: Path | None = None,
          quiet: bool = False) -> dict:
    root = repo_root or resolve_repository_root()
    out = out_path or (root / "work" / "events" / "ge16-events.db")
    index = build_index(root, kg_path=kg_path, figures_dir=figures_dir, data_root=data_root)
    extraction, inputs = extract_all(dossiers, draft_dirs, use_drafts)
    warnings = list(index.warnings) + list(extraction.warnings)
    builder_sha = file_sha256(Path(__file__).resolve())
    report = write_database(out, extraction, Resolver(index), inputs, warnings, builder_sha)
    report["entity_index"] = index.stats()
    stats_path = out.with_name(out.stem + "-stats.json")
    stats_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    report["stats_path"] = str(stats_path)
    if not quiet:
        _print_report(report)
    return report


def _print_report(report: dict) -> None:
    counts = report["counts"]
    print(f"GE16 events database → {report['db_path']}")
    print(f"  events {counts['events']}  entities {counts['entities']} "
          f"({counts['entities_by_type'].get('person', 0)} persons, "
          f"{counts['entities_by_type'].get('party', 0)} parties, "
          f"{counts['entities_by_type'].get('seat', 0)} seats)  "
          f"links {counts['event_links']['follows']}+{counts['event_links']['precedes']}")
    print(f"  mentions {counts['event_entity_rows']}  metrics {counts['event_metrics']}  "
          f"sources {counts['sources']}  claims {counts['claim_reviews']}  notes {counts['dossier_notes']}")
    print(f"  coverage: entity {counts['entity_coverage_pct']}%  source {counts['source_coverage_pct']}%  "
          f"dated {counts['dated_coverage_pct']}%  kg-joinable entities {counts['entities_linked_to_kg']}")
    print(f"  merged on ingest: {report['records_merged']} duplicate records of {report['records_in']}")
    ledger = counts.get("story_ledger", {})
    if ledger:
        print(f"  story ledger: {ledger.get('stories', 0)} stories "
              f"({counts.get('stories_open', 0)} open, {counts.get('multi_event_stories', 0)} multi-event, "
              f"largest {ledger.get('largest', 0)} events)")
        for theme, count in list(ledger.get("by_theme", {}).items())[:6]:
            print(f"    {theme:<24} {count}")
    for line in sorted(counts["by_type"].items(), key=lambda item: -item[1])[:12]:
        print(f"    {line[0]:<26} {line[1]}")
    failed = [check for check in report["checks"] if not check["ok"]]
    print(f"  checks: {len(report['checks']) - len(failed)}/{len(report['checks'])} passed"
          + (f"  FAILED: {[check['check'] for check in failed]}" if failed else ""))
    print(f"  stats → {report.get('stats_path', '')}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build the GE16 relational events database.")
    parser.add_argument("--dossier-dir", default=None,
                        help="directory holding the sweep dossiers (default work/events/_draft)")
    parser.add_argument("--draft-dir", action="append", default=None,
                        help="cron update layer directory (repeatable; default work/figures/_draft)")
    parser.add_argument("--out", default=None, help="database path (default work/events/ge16-events.db)")
    parser.add_argument("--kg", default=None, help="knowledge-graph json path")
    parser.add_argument("--figures-dir", default=None, help="figure artifacts directory")
    parser.add_argument("--data-root", default=None, help="canonical 1_DATA root")
    parser.add_argument("--no-drafts", action="store_true", help="skip the cron update layers")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    root = resolve_repository_root()
    dossier_dir = Path(args.dossier_dir) if args.dossier_dir else root / "work" / "events" / "_draft"
    draft_dirs = [Path(item) for item in args.draft_dir] if args.draft_dir else \
        [root / "work" / "figures" / "_draft"]
    dossiers = discover_dossiers(dossier_dir)
    if not dossiers:
        print(f"no sweep dossiers found in {dossier_dir}", file=sys.stderr)
        return 2
    report = build(
        dossiers, draft_dirs, Path(args.out) if args.out else None, repo_root=root,
        use_drafts=not args.no_drafts, kg_path=Path(args.kg) if args.kg else None,
        figures_dir=Path(args.figures_dir) if args.figures_dir else None,
        data_root=Path(args.data_root) if args.data_root else None,
        quiet=args.quiet,
    )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
