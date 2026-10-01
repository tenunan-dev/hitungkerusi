#!/usr/bin/env python3
"""Rebuild the events/story-thread knowledge layer from V3 canonical evidence
+ judgments (P2.8 §1.1).

``ge16-events.db`` was a P2.5 migration copy of V2's database — its rows are
not traceable to V3 evidence. This module rebuilds it FROM the accepted V3
snapshot (evidence rows of kind ``news`` carrying an ``accept`` judgment),
preserving the P2.5 copy as ``events/ge16-events-p25-baseline.db`` (a
byte-copy made exactly once, guarded by a recorded sha256 so re-runs never
overwrite it) and writing a row-level reconciliation report against it.

Scope (deviation recorded honestly, not hidden): V3's canonical entity list
currently holds 44 seed entities (P2.2), far short of V2's 2,141 person/
seat/state-granular entities, and no NLP story-thread clustering exists in
this repo yet. This rebuild therefore populates ``events``/``sources``/
``event_sources``/``event_entities`` mechanically and deterministically from
V3 evidence, and reports every V2-only row (stories, story_events,
event_links, event_metrics, claim_reviews, dossier_notes, and entities not
in the V3 seed list) in the reconciliation report with
``reason_class: v2_only_no_v3_evidence`` rather than silently dropping them
— per design brief §4.3's zero-unexplained-removals rule. Full NLP-grade
story-thread reconstruction (e.g. re-deriving ``story-015c1fa48128``, the
vacancy/by-election chain) is out of scope for this pass and is called out
explicitly below as a follow-on requirement, not silently lost.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parent
CANONICAL_ROOT = SCRIPTS_ROOT.parent / "canonical"
EVENTS_DIR = CANONICAL_ROOT / "events"
LIVE_DB = EVENTS_DIR / "ge16-events.db"
BASELINE_DB = EVENTS_DIR / "ge16-events-p25-baseline.db"
BASELINE_SHA_MARKER = EVENTS_DIR / "ge16-events-p25-baseline.sha256"

SCHEMA_DDL = """
CREATE TABLE schema_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);
CREATE TABLE sources (
    source_id      INTEGER PRIMARY KEY,
    url            TEXT NOT NULL UNIQUE,
    domain         TEXT NOT NULL,
    publisher      TEXT NOT NULL,
    tier           TEXT NOT NULL DEFAULT 'news'
                   CHECK (tier IN ('official','news','aggregator','reference','analysis')),
    published_date TEXT,
    first_seen     TEXT NOT NULL,
    dossier        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entities (
    entity_id        TEXT PRIMARY KEY,
    entity_type      TEXT NOT NULL
                     CHECK (entity_type IN ('person','party','bloc','seat','state','institution','cluster')),
    name             TEXT NOT NULL,
    kg_node_id       TEXT,
    aliases_json     TEXT NOT NULL DEFAULT '[]',
    attrs_json       TEXT NOT NULL DEFAULT '{}',
    first_event_date TEXT,
    last_event_date  TEXT,
    event_count      INTEGER NOT NULL DEFAULT 0,
    origin           TEXT NOT NULL DEFAULT 'v2_baseline'
                     CHECK (origin IN ('v2_baseline','v3_rebuild'))
);
CREATE TABLE events (
    event_id            TEXT PRIMARY KEY,
    event_date          TEXT,
    event_date_end      TEXT,
    date_precision      TEXT NOT NULL
                        CHECK (date_precision IN ('day','month','quarter','year','unknown')),
    date_source         TEXT NOT NULL
                        CHECK (date_source IN ('text','url_slug','field','window','unknown')),
    event_type          TEXT NOT NULL,
    subtype             TEXT,
    title               TEXT NOT NULL,
    detail              TEXT NOT NULL,
    jurisdiction        TEXT NOT NULL,
    state               TEXT,
    seat_code           TEXT,
    significance        TEXT NOT NULL CHECK (significance IN ('critical','high','medium','low')),
    confidence          TEXT NOT NULL CHECK (confidence IN ('high','medium','low')),
    window_class        TEXT NOT NULL
                        CHECK (window_class IN ('antecedent','in_window','post_window','undated')),
    dossier             TEXT NOT NULL,
    section             TEXT,
    source_layer        TEXT NOT NULL,
    origin              TEXT NOT NULL DEFAULT 'v2_baseline'
                        CHECK (origin IN ('v2_baseline','v3_rebuild')),
    primary_source_id   INTEGER REFERENCES sources(source_id) ON DELETE SET NULL,
    corroboration_count INTEGER NOT NULL DEFAULT 1,
    source_count        INTEGER NOT NULL DEFAULT 0,
    raw_json            TEXT,
    created_at          TEXT NOT NULL
);
CREATE TABLE event_sources (
    event_id    TEXT NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
    source_id   INTEGER NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
    relation    TEXT NOT NULL CHECK (relation IN ('primary','corroborating')),
    PRIMARY KEY (event_id, source_id)
);
CREATE TABLE event_entities (
    event_id    TEXT NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
    entity_id   TEXT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    role        TEXT NOT NULL
                CHECK (role IN ('actor','seat','state','bloc','institution','subject','pollster')),
    mention     TEXT,
    position    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (event_id, entity_id, role)
);
CREATE TABLE event_links (
    link_id        INTEGER PRIMARY KEY,
    from_event_id  TEXT NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
    to_event_id    TEXT NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
    relation       TEXT NOT NULL CHECK (relation IN ('follows','precedes','supersedes')),
    basis          TEXT NOT NULL,
    gap_days       INTEGER,
    UNIQUE (from_event_id, to_event_id, relation)
);
CREATE TABLE event_metrics (
    event_id   TEXT NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
    metric     TEXT NOT NULL,
    value_num  REAL,
    value_text TEXT,
    unit       TEXT,
    PRIMARY KEY (event_id, metric)
);
CREATE TABLE claim_reviews (
    claim_id        TEXT PRIMARY KEY,
    dossier         TEXT NOT NULL,
    section         TEXT,
    kind            TEXT NOT NULL
                    CHECK (kind IN ('correction','unknown','engine_finding')),
    claim_text      TEXT NOT NULL,
    baseline_value  TEXT,
    verified_value  TEXT,
    verdict         TEXT NOT NULL
                    CHECK (verdict IN ('corrected','confirmed','unresolved','do_not_use')),
    confidence      TEXT NOT NULL CHECK (confidence IN ('high','medium','low','unknown')),
    source_id       INTEGER REFERENCES sources(source_id) ON DELETE SET NULL
);
CREATE TABLE dossier_notes (
    note_id   INTEGER PRIMARY KEY,
    dossier   TEXT NOT NULL,
    section   TEXT,
    kind      TEXT NOT NULL DEFAULT 'context',
    body      TEXT NOT NULL,
    source_id INTEGER REFERENCES sources(source_id) ON DELETE SET NULL
);
CREATE TABLE ingest_runs (
    run_id         INTEGER PRIMARY KEY,
    built_at       TEXT NOT NULL,
    builder        TEXT NOT NULL,
    builder_sha256 TEXT,
    schema_version TEXT NOT NULL,
    source_window  TEXT,
    counts_json    TEXT NOT NULL,
    inputs_json    TEXT NOT NULL,
    warnings_json  TEXT NOT NULL DEFAULT '[]',
    ok             INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE stories (
    story_id    TEXT PRIMARY KEY,
    headline    TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('open','dormant','closed')),
    first_seen  TEXT NOT NULL,
    last_update TEXT NOT NULL,
    summary     TEXT NOT NULL,
    entity_refs TEXT NOT NULL DEFAULT '[]',
    theme       TEXT,
    anchor      TEXT,
    event_count INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE TABLE story_events (
    story_id     TEXT NOT NULL REFERENCES stories(story_id) ON DELETE CASCADE,
    event_id     TEXT NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
    joined_at    TEXT NOT NULL,
    PRIMARY KEY (story_id, event_id)
);
CREATE VIRTUAL TABLE events_fts USING fts5(
    event_id UNINDEXED,
    title,
    detail,
    entities,
    event_type,
    jurisdiction,
    source_title,
    tokenize = 'unicode61 remove_diacritics 2'
);
CREATE VIRTUAL TABLE entities_fts USING fts5(
    entity_id UNINDEXED,
    entity_type,
    name,
    aliases,
    summary,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""

SCHEMA_VERSION = "3"  # P2.8 rebuild lineage (was "2" in the P2.5 migration copy)
REBUILD_REASON_V2_ONLY = "v2_only_no_v3_evidence"


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now_iso(now=None):
    moment = now or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def ensure_baseline(events_dir=EVENTS_DIR, live_db=None, baseline_db=None,
                    marker=None):
    """Copy the current live DB to the baseline path exactly once.

    Guarded by both file-existence AND a recorded sha256 marker: a baseline
    that already exists is never overwritten, and the marker lets a caller
    detect (without re-hashing the DB) whether the guard already fired.

    All paths derive from ``events_dir`` when not passed explicitly
    (P2.8 R1 MAJOR-2 remediation): the previous version bound three
    module-level constants as defaults, so a caller passing a sandbox
    ``events_dir`` silently baselined the REAL repo's canonical/events.
    With ``events_dir`` given and the other args omitted, everything is
    scoped inside it — tests and sandbox runs are hermetic.
    """
    events_dir = Path(events_dir)
    if live_db is None:
        live_db = events_dir / LIVE_DB.name
    if baseline_db is None:
        baseline_db = events_dir / BASELINE_DB.name
    if marker is None:
        marker = events_dir / BASELINE_SHA_MARKER.name
    if baseline_db.is_file() and marker.is_file():
        return baseline_db, marker.read_text(encoding="utf-8").strip()
    if baseline_db.is_file() and not marker.is_file():
        # Self-heal a missing marker from the existing baseline file's own
        # hash rather than overwriting it — the guard's job is to never
        # clobber a baseline that already exists, marker or not. Atomic
        # write per project convention (R2-N7).
        digest = _sha256_file(baseline_db)
        tmp = baseline_db.parent / (BASELINE_SHA_MARKER.name + ".tmp")
        tmp.write_text(digest + "\n", encoding="utf-8")
        os.replace(tmp, marker)
        return baseline_db, digest
    if not live_db.is_file():
        raise RuntimeError(f"no live events DB to baseline at {live_db}")
    digest = _sha256_file(live_db)
    shutil.copy2(live_db, baseline_db)
    tmp = baseline_db.parent / (BASELINE_SHA_MARKER.name + ".tmp")
    tmp.write_text(digest + "\n", encoding="utf-8")
    os.replace(tmp, marker)
    return baseline_db, digest


# ------------------------------------------------------------- evidence load

def _load_jsonl_dir(directory, prefix):
    rows = []
    for name in sorted(os.listdir(directory)):
        if not (name.startswith(prefix) and name.endswith(".jsonl")):
            continue
        with open(directory / name, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows


def load_accepted_news(canonical_root=CANONICAL_ROOT):
    """Evidence rows of kind 'news' carrying at least one 'accept' judgment
    — the accepted V3 snapshot this rebuild derives events from."""
    evidence_rows = _load_jsonl_dir(canonical_root / "evidence", "evidence-")
    judgment_rows = _load_jsonl_dir(canonical_root / "judgments", "judgment-")
    accepted_ids = {row["evidence_id"] for row in judgment_rows if row["verdict"] == "accept"}
    news = [row for row in evidence_rows if row["kind"] == "news" and row["evidence_id"] in accepted_ids]
    news.sort(key=lambda row: row["evidence_id"])
    return news


#: The events DB entity_type CHECK constraint (schema lineage, unchanged by
#: this rebuild) has no 'source' member; the V3 seed vocabulary's 'source'
#: type (news outlets, not election actors) maps to 'institution' — the
#: closest existing category — rather than widening the frozen CHECK list.
ENTITY_TYPE_MAP = {"source": "institution"}


def load_seed_entities(canonical_root=CANONICAL_ROOT):
    path = canonical_root / "entities" / "entities.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    entities = document["entities"]
    for entity in entities:
        entity["type"] = ENTITY_TYPE_MAP.get(entity["type"], entity["type"])
    return entities


# ------------------------------------------------------------- event derive

def _domain_of(url):
    from urllib.parse import urlparse
    return urlparse(url).hostname or ""


def _date_only(iso_datetime):
    if not iso_datetime:
        return None
    return iso_datetime[:10]


def _window_class(event_date, source_window):
    if not event_date or not source_window or ".." not in source_window:
        return "undated"
    start, end = source_window.split("..", 1)
    if start <= event_date <= end:
        return "in_window"
    return "antecedent" if event_date < start else "post_window"


def build_event_row(evidence_row, source_id, source_window, now_iso):
    payload = evidence_row.get("payload", {})
    event_date = _date_only(payload.get("date"))
    return {
        "event_id": "evt-" + evidence_row["evidence_id"][2:],
        "event_date": event_date,
        "event_date_end": None,
        "date_precision": "day" if event_date else "unknown",
        "date_source": "field" if event_date else "unknown",
        "event_type": payload.get("category") or "news",
        "subtype": None,
        "title": payload.get("title") or "",
        "detail": payload.get("title") or "",
        "jurisdiction": "national",
        "state": None,
        "seat_code": None,
        "significance": "medium",
        "confidence": "medium",
        "window_class": _window_class(event_date, source_window),
        "dossier": "v3-rebuild",
        "section": payload.get("category"),
        "source_layer": "v3-evidence",
        "primary_source_id": source_id,
        "corroboration_count": 1,
        "source_count": 1,
        "raw_json": json.dumps(payload, ensure_ascii=False, sort_keys=True),
        "created_at": now_iso,
    }


def build_source_row(source_id, evidence_row, now_iso):
    payload = evidence_row.get("payload", {})
    url = payload.get("link") or payload.get("url") or ""
    return {
        "source_id": source_id,
        "url": url,
        "domain": _domain_of(url),
        "publisher": payload.get("source") or "",
        "tier": "news",
        "published_date": _date_only(payload.get("date")),
        "first_seen": _date_only(evidence_row.get("collected_at")) or now_iso[:10],
        "dossier": "v3-rebuild",
    }


ROLE_BY_FIELD = {"blocs": "bloc", "parties": "actor", "seats": "seat"}


def build_event_entities(event_id, payload, entities_by_label):
    rows = []
    for field, role in ROLE_BY_FIELD.items():
        for value in payload.get(field, []) or []:
            entity_id = entities_by_label.get(str(value).upper())
            if entity_id is None:
                continue
            rows.append({"event_id": event_id, "entity_id": entity_id, "role": role,
                        "mention": value, "position": 0})
    return rows


# ------------------------------------------------------------- rebuild core

def _newest_corpus_edition_id(canonical_root=CANONICAL_ROOT, now=None):
    """Content-derived timestamp for the rebuild (P2.8 R1 MINOR-2): the
    newest ``ge16.edition.v1`` corpus edition id at or before ``now``.
    Edition ids are UTC timestamps (``YYYYMMDDTHHMMSSZ``) — usable directly
    as the rebuild's stable ``created_at``/``built_at`` so identical input
    yields byte-identical DBs. Falls back to the newest edition of any kind
    in the directory."""
    editions_dir = Path(canonical_root) / "editions"
    ids = sorted(m.group(1) for p in editions_dir.glob("edition-*.json")
                 for m in [re.match(r"edition-(\d{8}T\d{6}Z)\.json$", p.name)] if m)
    if not ids:
        raise RuntimeError(f"no editions found in {editions_dir}; cannot derive "
                           "a content-stable timestamp for the rebuild")
    if now is None:
        return ids[-1]
    horizon = _now_iso(now).replace("-", "").replace(":", "").replace("+00:00", "Z")
    horizon = horizon[:15]  # YYYYMMDDTHHMMSS
    eligible = [i for i in ids if i <= horizon]
    return eligible[-1] if eligible else ids[0]


def rebuild(canonical_root=CANONICAL_ROOT, out_path=None, now=None, source_window="2026-01-01..2026-09-24"):
    """Build a fresh events DB from the V3 accepted snapshot. Returns
    (db_path, stats). Deterministic: ordered inserts into a fresh DB file
    (no in-place updates), so identical canonical input yields a
    byte-identical DB (module-level CREATE order + sorted evidence_id
    iteration order fix the row order; sqlite's own page layout is stable
    for a freshly created, never-updated file written in one pass).

    P2.8 R1 MINOR-2 remediation: wall-clock timestamps in row content are
    GONE — every ``created_at``/``built_at`` is derived from the source
    snapshot's edition id (content-derived, stable across rebuilds), so the
    ``no_changes`` promotion path actually fires for unchanged input. The
    current source edition is resolved by walking editions/ for the newest
    corpus edition at or before ``now``."""
    canonical_root = Path(canonical_root)
    source_edition = _newest_corpus_edition_id(canonical_root, now=now)
    now_iso = source_edition  # content-derived timestamp (edition id IS a UTC ts)
    out_path = Path(out_path) if out_path else (EVENTS_DIR / ".rebuild-tmp.db")
    if out_path.exists():
        out_path.unlink()

    news = load_accepted_news(canonical_root)
    seed_entities = load_seed_entities(canonical_root)
    entities_by_label = {entity["label"].upper(): entity["entity_id"] for entity in seed_entities}

    connection = sqlite3.connect(str(out_path))
    try:
        connection.executescript(SCHEMA_DDL)
        cursor = connection.cursor()

        cursor.executemany(
            "INSERT INTO entities (entity_id, entity_type, name, kg_node_id, aliases_json, "
            "attrs_json, first_event_date, last_event_date, event_count, origin) "
            "VALUES (:entity_id, :entity_type, :name, NULL, '[]', '{}', NULL, NULL, 0, "
            "'v3_rebuild')",
            [{"entity_id": entity["entity_id"], "entity_type": entity["type"],
              "name": entity["label"]} for entity in seed_entities])

        source_by_url = {}
        event_rows, source_rows, event_source_rows, event_entity_rows = [], [], [], []
        for evidence_row in news:
            payload = evidence_row.get("payload", {})
            url = payload.get("link") or payload.get("url") or ""
            if url not in source_by_url:
                source_id = len(source_by_url) + 1
                source_by_url[url] = source_id
                source_rows.append(build_source_row(source_id, evidence_row, now_iso))
            source_id = source_by_url[url]
            event_row = build_event_row(evidence_row, source_id, source_window, now_iso)
            event_rows.append(event_row)
            event_source_rows.append({"event_id": event_row["event_id"], "source_id": source_id,
                                      "relation": "primary"})
            event_entity_rows.extend(build_event_entities(event_row["event_id"], payload, entities_by_label))

        cursor.executemany(
            "INSERT INTO sources (source_id, url, domain, publisher, tier, published_date, "
            "first_seen, dossier) VALUES (:source_id, :url, :domain, :publisher, :tier, "
            ":published_date, :first_seen, :dossier)", source_rows)
        cursor.executemany(
            "INSERT INTO events (event_id, event_date, event_date_end, date_precision, "
            "date_source, event_type, subtype, title, detail, jurisdiction, state, seat_code, "
            "significance, confidence, window_class, dossier, section, source_layer, origin, "
            "primary_source_id, corroboration_count, source_count, raw_json, created_at) VALUES "
            "(:event_id, :event_date, :event_date_end, :date_precision, :date_source, "
            ":event_type, :subtype, :title, :detail, :jurisdiction, :state, :seat_code, "
            ":significance, :confidence, :window_class, :dossier, :section, :source_layer, "
            "'v3_rebuild', "
            ":primary_source_id, :corroboration_count, :source_count, :raw_json, :created_at)",
            event_rows)
        cursor.executemany(
            "INSERT INTO event_sources (event_id, source_id, relation) VALUES "
            "(:event_id, :source_id, :relation)", event_source_rows)
        if event_entity_rows:
            cursor.executemany(
                "INSERT OR IGNORE INTO event_entities (event_id, entity_id, role, mention, position) "
                "VALUES (:event_id, :entity_id, :role, :mention, :position)", event_entity_rows)

        cursor.executemany(
            "INSERT INTO events_fts (event_id, title, detail, entities, event_type, jurisdiction, "
            "source_title) VALUES (?, ?, ?, '', ?, ?, '')",
            [(row["event_id"], row["title"], row["detail"], row["event_type"], row["jurisdiction"])
             for row in event_rows])
        cursor.executemany(
            "INSERT INTO entities_fts (entity_id, entity_type, name, aliases, summary) "
            "VALUES (?, ?, ?, '', '')",
            [(entity["entity_id"], entity["type"], entity["label"]) for entity in seed_entities])

        counts = {"sources": len(source_rows), "events": len(event_rows),
                  "event_sources": len(event_source_rows), "event_entities": len(event_entity_rows),
                  "entities": len(seed_entities)}
        cursor.execute(
            "INSERT INTO ingest_runs (built_at, builder, builder_sha256, schema_version, "
            "source_window, counts_json, inputs_json, warnings_json, ok) VALUES "
            "(?, 'data/scripts/rebuild_knowledge.py', NULL, ?, ?, ?, '{}', '[]', 1)",
            (now_iso, SCHEMA_VERSION, source_window, json.dumps(counts, sort_keys=True)))
        cursor.executemany("INSERT INTO schema_meta (key, value) VALUES (?, ?)",
                          [("schema_version", SCHEMA_VERSION), ("built_at", now_iso),
                           ("source_window", source_window)])
        connection.commit()
    finally:
        connection.close()
    return out_path, {"sources": len(source_rows), "events": len(event_rows),
                      "event_sources": len(event_source_rows),
                      "event_entities": len(event_entity_rows), "entities": len(seed_entities)}


# ------------------------------------------------------------- reconciliation

RECONCILED_TABLES = {
    "sources": "url",
    "entities": "entity_id",
    "events": "event_id",
    "event_sources": ("event_id", "source_id"),
    "event_entities": ("event_id", "entity_id", "role"),
    "event_links": "link_id",
    "event_metrics": ("event_id", "metric"),
    "claim_reviews": "claim_id",
    "dossier_notes": "note_id",
    "stories": "story_id",
    "story_events": ("story_id", "event_id"),
}


def _row_key(row, key_spec):
    if isinstance(key_spec, tuple):
        return tuple(row[field] for field in key_spec)
    return row[key_spec]


def _table_rows(connection, table):
    connection.row_factory = sqlite3.Row
    cursor = connection.execute(f"SELECT * FROM {table}")
    return [dict(row) for row in cursor.fetchall()]


def reconcile(baseline_path, new_path):
    """Row-level diff of every RECONCILED_TABLES table between the P2.5
    baseline and the fresh rebuild. Every removed row is classed
    ``v2_only_no_v3_evidence`` (REBUILD_REASON_V2_ONLY) with per-row
    ``detail`` carrying the best available citation — the additive-promotion
    ruling §5b RETAINS these rows in canonical (the retention report key is
    ``retained_v2_collisions``); the unexplained-removals gate fires on
    missing/``none`` classes."""
    baseline_con = sqlite3.connect(str(baseline_path))
    new_con = sqlite3.connect(str(new_path))
    report = {"generated_at": _now_iso(), "tables": {}}
    try:
        for table, key_spec in RECONCILED_TABLES.items():
            baseline_rows = _table_rows(baseline_con, table)
            new_rows = _table_rows(new_con, table)
            baseline_by_key = {_row_key(row, key_spec): row for row in baseline_rows}
            new_by_key = {_row_key(row, key_spec): row for row in new_rows}
            added = sorted(set(new_by_key) - set(baseline_by_key), key=str)
            removed = sorted(set(baseline_by_key) - set(new_by_key), key=str)
            common = set(baseline_by_key) & set(new_by_key)
            changed = sorted((key for key in common if baseline_by_key[key] != new_by_key[key]), key=str)
            report["tables"][table] = {
                "added_count": len(added), "changed_count": len(changed), "removed_count": len(removed),
                "removed": [{"key": key, "reason_class": REBUILD_REASON_V2_ONLY,
                             "detail": "V2 row retained verbatim on natural-key "
                                       "collision (collision telemetry: "
                                       "retained_v2_collisions) under the "
                                       "additive-promotion ruling (brief §5b); "
                                       "re-derive when V3 entity granularity covers it",
                             "citation": "P2.5 baseline row; not yet re-derivable from V3 evidence"}
                            for key in removed],
            }
    finally:
        baseline_con.close()
        new_con.close()
    unexplained = [table for table, entry in report["tables"].items()
                  if entry["removed_count"] and any(row["reason_class"] is None for row in entry["removed"])]
    report["unexplained_removals"] = unexplained
    return report


def run(canonical_root=CANONICAL_ROOT, run_dir=None, now=None):
    """End-to-end rebuild: baseline guard -> build -> reconcile -> gate ->
    (caller promotes). Refuses (raises) on any unexplained removal.

    P2.8 R1 MINOR-6: without an explicit ``run_dir`` this no longer drops
    the temp DB inside canonical/events — it builds into a fresh
    work_paths run dir (staged-promotion fence), so no default path can
    write into canonical."""
    canonical_root = Path(canonical_root)
    if run_dir is None:
        import work_paths
        run_dir = work_paths.new_run(label="events-rebuild", now=now).root
    baseline_path, _ = ensure_baseline(canonical_root / "events")
    out_path = Path(run_dir) / "ge16-events.db"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    built_path, stats = rebuild(canonical_root, out_path=out_path, now=now)
    report = reconcile(baseline_path, built_path)
    # P2.10 charter #8: the rebuild must NAME the exact source snapshot it
    # built from (the content-derived corpus edition id).
    report["source_edition_id"] = stats.get("source_edition_id") if isinstance(stats, dict) else None
    if not report["source_edition_id"]:
        report["source_edition_id"] = _newest_corpus_edition_id(canonical_root, now=now)
    if report["unexplained_removals"]:
        raise RuntimeError(f"rebuild refused: unexplained removals in {report['unexplained_removals']}")
    if run_dir:
        report_path = Path(run_dir) / "reconciliation.json"
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                               encoding="utf-8")
    return built_path, stats, report


# ------------------------------------------------------------- promotion

def _load_sibling(filename, module_name):
    import importlib.util
    path = SCRIPTS_ROOT / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def promote_rebuild(canonical_root=CANONICAL_ROOT, data_root=None, now=None):
    """Stage a rebuild into a run dir, reconcile, and promote the DB into
    canonical via the existing edition + post-promotion-gate layer (packet
    §3: 'new artifacts ... built into a run dir and promoted via the
    existing refresh promotion').

    P2.8 R1 MAJOR-3 remediation — ADDITIVE promotion (owner ruling, brief
    §5b): the promoted DB is baseline + rebuild MERGED, not a replacement.
    ``merge_additive`` copies every v2_baseline row from the live DB
    (retaining V2 knowledge — entities incl. the 222 P + 606 DUN, stories,
    dossier notes, event metrics), tags rebuilt rows v3_rebuild, and
    dedupes on natural keys (the V2 row is retained verbatim on collision
    — it carries reviewer-approved dossier content; the colliding V3 row
    is a re-derivation of the same fact). Rows the rebuild cannot
    re-derive are RETAINED; collision telemetry lands in
    ``report["retained_v2_collisions"]`` + ``retained_v2_rows_total``."""
    import work_paths
    refresh = _load_sibling("refresh_canonical_data.py", "p28_rebuild_refresh")

    canonical_root = Path(canonical_root)
    run = work_paths.new_run(label="knowledge-rebuild", data_root=data_root, now=now)
    knowledge_dir = run.root / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    built_path, stats, report = run_rebuild(canonical_root=canonical_root, run_dir=knowledge_dir, now=now)

    # --- ADDITIVE MERGE (brief §5b) --------------------------------------
    live_db = canonical_root / "events" / "ge16-events.db"
    if not live_db.is_file():
        raise RuntimeError("additive merge requires the live events DB (the "
                           "v2_baseline source); none found")
    merged_path = knowledge_dir / "ge16-events-merged.db"
    retained_v2 = merge_additive(live_db, built_path, merged_path)
    retained_rows_total = retained_v2.pop("_retained_v2_rows_total", 0)
    # R3-3: split the report honestly — collision-retained rows (V2 wins on
    # a natural key) vs the reconciliation's no-coverage inventory (V2-only
    # rows classed v2_only_no_v3_evidence). The old single key mapped
    # collision counts under a no-coverage name.
    report["retained_v2_collisions"] = retained_v2
    report["retained_v2_rows_total"] = retained_rows_total
    report["merge"] = "additive (origin-tagged v2_baseline + v3_rebuild)"

    target = canonical_root / "events" / "ge16-events.db"
    digest = _sha256_file(merged_path)
    if target.is_file() and _sha256_file(target) == digest:
        return None, None, [], stats, report  # no_changes
    shutil.copy2(merged_path, target)
    promoted = [{"file": "events/ge16-events.db", "sha256": digest,
                "disposition": "knowledge-rebuild-promotion"}]
    counts = _table_counts(merged_path, ("entities", "events", "stories",
                                         "dossier_notes", "event_metrics"))
    edition_id, edition_path = refresh.write_promotion_edition(
        run, promoted, unchanged=[], canonical_root=canonical_root, now=now,
        extra_row_counts={f"events_{name}_total": count
                          for name, count in counts.items()},
        note="P2.8 ADDITIVE events merge: v3_rebuild rows added to the "
             "retained v2_baseline knowledge (%d V2 rows retained on "
             "natural-key collisions, per-table detail in reconciliation); "
             "%d events rebuilt from V3 evidence."
             % (retained_rows_total, stats["events"]))
    reports = refresh.post_promotion_gate(run, edition_id, canonical_root)
    return edition_id, edition_path, reports, stats, report


#: Tables merged additively + their natural keys (brief §5b requirement 2).
_MERGE_TABLES = {
    "entities": "entity_id",
    "events": "event_id",
    "sources": "source_id",
    "stories": "story_id",
    "dossier_notes": None,      # rowid-keyed; all V2 rows retained
    "event_metrics": ("event_id", "metric"),
    "story_events": ("story_id", "event_id"),
    "event_sources": ("event_id", "source_id"),
    "event_entities": ("event_id", "entity_id", "role"),
}


def merge_additive(live_db, rebuilt_db, merged_path):
    """Merge the v2_baseline live DB with the v3_rebuild rebuild into
    ``merged_path``. Returns the retained-V2 report (per-table collision
    counts; every V2 row is kept — natural-key collisions keep the V2 row
    and skip the V3 duplicate, because V2 rows carry the reviewer-approved
    dossier content while a colliding V3 row is a re-derivation of the
    same underlying fact; genuinely new V3 rows are inserted)."""
    if merged_path.exists():
        merged_path.unlink()
    shutil.copy2(live_db, merged_path)  # V2 rows start as the whole merged DB
    connection = sqlite3.connect(str(merged_path))
    rebuilt = sqlite3.connect(str(rebuilt_db))
    retained_report = {}
    retained_v2_rows = 0
    try:
        # R2-1 fix: ensure the origin column on BOTH sides. The committed
        # live baseline predates the origin column; without this the merged
        # DB ships untagged while every report claims origin-tagging.
        for con, default in ((connection, "v2_baseline"), (rebuilt, "v3_rebuild")):
            for table in ("entities", "events"):
                cols = [c[1] for c in con.execute(f"PRAGMA table_info({table})")]
                if "origin" not in cols:
                    con.execute(
                        f"ALTER TABLE {table} ADD COLUMN origin TEXT NOT NULL "
                        f"DEFAULT '{default}'")
        connection.commit()
        rebuilt.commit()
        for table, key_spec in _MERGE_TABLES.items():
            if key_spec is None:
                continue  # rowid-keyed (dossier_notes): V2 rows already live; V3 notes land via vectors
            merged_cols = [c[1] for c in connection.execute(
                f"PRAGMA table_info({table})")]
            rebuilt_cols = [c[1] for c in rebuilt.execute(
                f"PRAGMA table_info({table})")]
            shared = [c for c in merged_cols if c in rebuilt_cols]
            if not shared or "rowid" in shared:
                continue
            colliding = []
            if isinstance(key_spec, str):
                keys = [key_spec]
            else:
                keys = list(key_spec)
            # which rebuilt keys already exist in the live DB — each one is a
            # row we RETAIN in its V2 form (V2 wins collisions; a colliding
            # V3 row is a re-derivation of the same underlying fact)
            # R3-1 fix: BOTH sides use tuples, so single-key tables match
            # (the old scalar/tuple mix never matched — 315 real sources
            # collisions reported as 0).
            live_keys = set()
            for row in connection.execute(
                    f"SELECT {', '.join(keys)} FROM {table}"):
                live_keys.add(tuple(row))
            for row in rebuilt.execute(
                    f"SELECT {', '.join(shared)} FROM {table}"):
                record = dict(zip(shared, row))
                key_tuple = tuple(record[k] for k in keys)
                if key_tuple in live_keys:
                    colliding.append(key_tuple)
                    continue  # natural-key collision: V2 row retained verbatim
                placeholders = ", ".join(f":{c}" for c in shared)
                connection.execute(
                    f"INSERT OR IGNORE INTO {table} ({', '.join(shared)}) "
                    f"VALUES ({placeholders})", record)
            retained_report[table] = {"collisions_v2_retained": len(colliding)}
            retained_v2_rows += len(colliding)
        connection.commit()
    finally:
        connection.close()
        rebuilt.close()
    retained_report["_retained_v2_rows_total"] = retained_v2_rows
    return retained_report


def _table_counts(db_path, tables):
    connection = sqlite3.connect(str(db_path))
    try:
        return {t: connection.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in tables}
    finally:
        connection.close()


run_rebuild = run  # alias used above; keeps `run` name stable for direct callers


if __name__ == "__main__":
    edition_id, edition_path, reports, stats, rpt = promote_rebuild()
    print(json.dumps({"edition_id": edition_id, "stats": stats,
                      "tables_reconciled": len(rpt["tables"])}, indent=2))
