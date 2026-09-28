#!/usr/bin/env python3
"""Story ledger — deterministic narrative clustering over the event store.

The event layer answers "what happened, when, to whom". The story ledger answers
"which dated thread is this part of": one RoS action against a party, one
by-election chain on one seat, one coalition split, one GE16-timing signal.

Two membership rules, both deterministic and both re-derivable from the store:

  (a) SHARED ACTORS — two events whose entity sets (event_entities, all roles)
      are similar enough (IDF-weighted Jaccard >= STORY_JACCARD) belong to the
      same thread. IDF weighting is what keeps a party that appears in 100
      events (bloc:PN, party:umno) from welding the whole corpus into one blob:
      a shared rare entity (a person, a seat) carries real signal, a shared
      ubiquitous party barely any.
  (b) HEADLINE FAMILIES — the six narrative families the sweep tracks
      (RoS/suspension, vacancy/by-election chain, Bersatu split -> merger, seat
      talks, GE16 timing/dissolution, court cases) group by family AND anchor:
      all RoS events on the same party are one thread, but the RoS thread on
      Bersatu and the one on Pejuang stay separate. Events with no family fall
      back to a series rule (same event type + same *specific* anchor).

Transitive closure is INTENTIONAL (a chain of pairwise links is one thread) but
its blast radius is bounded by the two guards above, so the ledger never collapses
into "everything is one story".

Story identity is stable across rebuilds: the id is seeded on the thread's
canonical first event, and `build_stories(..., prior=...)` re-uses the id a
previous build assigned to any member event. Appending a later event therefore
extends an existing story instead of spawning a new one.

Reading: build_events_db.py writes the ledger after the events; query_events.py
renders it (`--stories`, `--story <id>`).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from datetime import date, timedelta

#: IDF-weighted Jaccard floor for the shared-actor rule (a).
STORY_JACCARD = 0.6
#: Floor for merging two whole threads whose entity sets overlap (see
#: consolidate()); looser than STORY_JACCARD, still well above noise overlap.
CONSOLIDATE_JACCARD = 0.34
#: A consolidated thread may not exceed this many events: without the cap one
#: ubiquitous actor (Anwar/PH/PN) chains unrelated threads into a mega-story.
MAX_STORY_EVENTS = 48
#: A thread anchored on an entity carried by MORE events than this is not a
#: series (the anchor must be specific: a person, a seat, a niche institution).
ANCHOR_DF_MAX = 6
#: Status windows, measured against the corpus' latest event date (never against
#: wall-clock time, so a rebuild of the same store produces the same ledger and
#: the same report bytes).
STATUS_OPEN_DAYS = 90
STATUS_DORMANT_DAYS = 180

#: Ordered headline families (first match wins). Case-insensitive.
THEME_RULES = (
    ("ros_suspension", re.compile(
        r"\bros\b|registrar of societ|show[- ]cause|suspend\w*|deregist\w*|dissolv\w* (the )?part|"
        r"statutory declaration|de-registration", re.I)),
    ("vacancy_by_election", re.compile(
        r"\bvacan\w*|\bby[- ]election\b|\bwrit\b|speaker notif|seat declared|remain\w* empty|"
        r"uncontested|nomination day|polling day", re.I)),
    ("bersatu_split_merger", re.compile(
        r"\bbersatu\b|\bbersama\b|\bwawasan\b|merger|gabungan|\bspectre\b|party split|"
        r"\bsplit\b|\bexpulsion\b|sacked|axed", re.I)),
    ("seat_talks", re.compile(
        r"seat (talk|negotiat|allocat|sharing)|seat-?sharing|electoral pact|poll pact|"
        r"pact with|cooperation talks|alliance talks|negotiat\w* (for )?(the )?seats?", re.I)),
    ("ge16_timing", re.compile(
        r"dissolv\w*|\bge-?16\b|\bpru-?16\b|general election|snap poll|early (polls|election)|"
        r"election period|parliament\w* (term|expir)|electoral roll|redelineat\w*", re.I)),
    ("court_case", re.compile(
        r"\bcourt\b|tribunal|judicial|\bsuit\b|lawsuit|injunction|\bhearing\b|\bappeal\b|\bagc\b|"
        r"judgment|litigation|challenge\w* in court", re.I)),
)

#: Human labels for the report/render layer (derived from the family keys above).
THEME_LABELS = {
    "ros_suspension": "RoS / suspension",
    "vacancy_by_election": "Vacancy / by-election chain",
    "bersatu_split_merger": "Party split / merger",
    "seat_talks": "Seat talks",
    "ge16_timing": "GE16 timing / dissolution",
    "court_case": "Court case",
}
THEME_ORDER = tuple(name for name, _ in THEME_RULES)


# ------------------------------------------------------------------ helpers ---

def theme_of(title, subtype=None):
    """The narrative family an event belongs to, or None when it matches none.

    Scans the headline first (the strongest signal), then the subtype label.
    """
    haystacks = [title or ""]
    if subtype:
        haystacks.append(str(subtype).replace("_", " "))
    for text in haystacks:
        if not text:
            continue
        for name, pattern in THEME_RULES:
            if pattern.search(text):
                return name
    return None


def entity_weights(entity_df, total):
    """IDF weight per entity id: rare entities dominate, ubiquitous ones fade.

    A tiny corpus (unit tests) still gets non-zero weights, so the rule behaves
    the same way at any corpus size.
    """
    weights = {}
    for entity_id, df in entity_df.items():
        weights[entity_id] = math.log((total + 1.0) / (df + 1.0)) + 1e-9
    return weights


def weighted_jaccard(left, right, weights):
    """IDF-weighted Jaccard over two entity-id sets."""
    if not left or not right:
        return 0.0
    shared = left & right
    if not shared:
        return 0.0
    union = left | right
    numerator = sum(weights.get(entity_id, 0.0) for entity_id in shared)
    denominator = sum(weights.get(entity_id, 0.0) for entity_id in union)
    if denominator <= 0.0:
        return 0.0
    return numerator / denominator


def anchor_of(event, entities, entity_df):
    """The thread's anchor key: the most specific thing the event is about.

    Priority: the seat the event resolves to, else the rarest entity it carries,
    else the state, else the jurisdiction. "national" is the last resort so an
    event with no entity at all can still be its own thread.
    """
    if event.get("seat_code"):
        return "seat:" + str(event["seat_code"])
    if entities:
        ranked = sorted(entities, key=lambda item: (entity_df.get(item, 0), item))
        return ranked[0]
    if event.get("state"):
        return "state:" + str(event["state"])
    jurisdiction = event.get("jurisdiction") or ""
    if jurisdiction and jurisdiction != "national":
        return "jurisdiction:" + str(jurisdiction)
    return "national"


# --------------------------------------------------------------- clustering ---

class _Union:
    """Deterministic union-find over event ids."""

    def __init__(self, keys):
        self.parent = {key: key for key in keys}

    def find(self, key):
        root = key
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[key] != root:      # path compression, order-free
            self.parent[key], key = root, self.parent[key]
        return root

    def union(self, left, right):
        a, b = self.find(left), self.find(right)
        if a == b:
            return False
        # merge towards the lexicographically smaller root: order-independent
        if b < a:
            a, b = b, a
        self.parent[b] = a
        return True


def link_predicate(left, right, weights, entity_df):
    """True when two prepared events belong to the same story.

    (a) shared actors: IDF-weighted Jaccard >= STORY_JACCARD
    (b) same headline family AND the same anchor
    (c) no family on either side: same event type AND the same SPECIFIC anchor
        (a series thread, e.g. one pollster's monthly releases)
    """
    if weighted_jaccard(left["entities"], right["entities"], weights) >= STORY_JACCARD:
        return True
    if left["theme"] and left["theme"] == right["theme"] and left["anchor"] == right["anchor"]:
        return True
    if (not left["theme"] and not right["theme"]
            and left["event_type"] == right["event_type"]
            and left["anchor"] == right["anchor"]
            and entity_df.get(left["anchor"], ANCHOR_DF_MAX + 1) <= ANCHOR_DF_MAX):
        return True
    return False


def prepare_events(event_rows, entity_map):
    """Turn (event rows, entity map) into the clustering input records."""
    entity_df = {}
    for entities in entity_map.values():
        for entity_id in entities:
            entity_df[entity_id] = entity_df.get(entity_id, 0) + 1
    total = max(len(event_rows), 1)
    weights = entity_weights(entity_df, total)

    prepared = []
    for row in event_rows:
        entities = entity_map.get(row["event_id"], set())
        prepared.append({
            "event_id": row["event_id"],
            "event_date": row["event_date"] or "",
            "event_type": row["event_type"],
            "title": row["title"],
            "subtype": row.get("subtype"),
            "seat_code": row.get("seat_code"),
            "state": row.get("state"),
            "jurisdiction": row.get("jurisdiction"),
            "window_class": row.get("window_class"),
            "significance": row.get("significance"),
            "entities": entities,
            "theme": theme_of(row["title"], row.get("subtype")),
            "anchor": anchor_of(row, entities, entity_df),
        })
    return prepared, entity_df, weights


def _story_id(anchor, seed_event_id):
    """Stable id: seeded on the thread's anchor + canonical first event."""
    digest = hashlib.sha1(f"{anchor}|{seed_event_id}".encode("utf-8")).hexdigest()
    return "story-" + digest[:12]


def cluster(prepared, entity_df, weights, prior=None):
    """Cluster prepared events into stories (list of dicts, sorted by first_seen).

    `prior` maps event_id -> story_id from an earlier build. When a cluster
    contains events a previous build already assigned, that id is re-used, so a
    later event joining a thread APPENDS to the existing story instead of
    creating a second one.
    """
    prior = prior or {}
    union = _Union([item["event_id"] for item in prepared])
    ordered = sorted(prepared, key=lambda item: (item["event_date"], item["event_id"]))
    for index, left in enumerate(ordered):
        for right in ordered[index + 1:]:
            if link_predicate(left, right, weights, entity_df):
                union.union(left["event_id"], right["event_id"])

    groups = {}
    for item in ordered:
        groups.setdefault(union.find(item["event_id"]), []).append(item)

    stories = []
    for members in groups.values():
        members.sort(key=lambda item: (item["event_date"], item["event_id"]))
        seed = members[0]
        inherited = sorted({prior[item["event_id"]] for item in members
                            if item["event_id"] in prior})
        story_id = inherited[0] if inherited else _story_id(seed["anchor"], seed["event_id"])
        # the thread's family = the family most members share (a thread keeps a
        # label even when a later event joins it), else its seed's family
        theme = _majority_theme(members) or seed["theme"]
        # the headlined event = the most connected member (the thread's centre),
        # so a 30-event thread is not titled after whichever event happened first
        representative = min(members, key=lambda item: (-len(item["entities"]),
                                                         item["event_date"], item["event_id"]))
        entity_refs = sorted({entity_id for item in members for entity_id in item["entities"]})
        stories.append({
            "story_id": story_id,
            "seed": seed,
            "representative": representative,
            "members": members,
            "theme": theme,
            "anchor": _thread_anchor(members, entity_df) or seed["anchor"],
            "first_seen": seed["event_date"],
            "last_update": members[-1]["event_date"],
            "entity_refs": entity_refs,
        })
    stories.sort(key=lambda item: (item["first_seen"], item["story_id"]))
    return _unique_ids(consolidate(stories, weights, entity_df))


def _unique_ids(stories):
    """Guarantee one id per thread.

    Two merged threads can both inherit the same prior story_id (a prior build
    that had merged them, or a bad upstream run); a reused id would collide in
    the primary key. The first thread in ledger order keeps it, the others get a
    fresh deterministic id from their own representative event.
    """
    used = set()
    for story in stories:
        if story["story_id"] not in used:
            used.add(story["story_id"])
            continue
        while story["story_id"] in used:
            story["story_id"] = _story_id(story["anchor"],
                                          story["representative"]["event_id"])
        used.add(story["story_id"])
    return stories


def consolidate(stories, weights, entity_df):
    """Second pass: union whole threads that are plainly the same storyline.

    The event pass is deliberately strict (no transitive chain through one
    ubiquitous actor), which leaves a storyline split across two one-event
    threads reported from different angles. Two threads merge here when they
    share a family AND an anchor, or when their entity sets overlap strongly
    (CONSOLIDATE_JACCARD, looser than the event threshold because a story's
    entity set is the union of all its events, so it is inevitably broader).
    The merged thread keeps the smallest story_id: deterministic and stable.
    """
    if len(stories) < 2:
        return stories
    union = _Union(list(range(len(stories))))
    size = {index: len(story["members"]) for index, story in enumerate(stories)}
    for index, left in enumerate(stories):
        for offset, right in enumerate(stories[index + 1:], start=index + 1):
            same_family = bool(left["theme"]) and left["theme"] == right["theme"]
            same_anchor = bool(left["anchor"]) and left["anchor"] == right["anchor"]
            if not ((same_family and same_anchor) or weighted_jaccard(
                    set(left["entity_refs"]), set(right["entity_refs"]),
                    weights) >= CONSOLIDATE_JACCARD):
                continue
            a, b = union.find(index), union.find(offset)
            if a == b or size[a] + size[b] > MAX_STORY_EVENTS:
                continue  # a cap keeps one ubiquitous actor from eating the ledger
            if union.union(a, b):
                size[union.find(a)] = size[a] + size[b]

    groups = {}
    for index, story in enumerate(stories):
        groups.setdefault(union.find(index), []).append(story)
    merged = []
    for members in groups.values():
        if len(members) == 1:
            merged.append(members[0])
            continue
        members.sort(key=lambda item: (item["first_seen"], item["story_id"]))
        events = sorted({item["event_id"]: item for story in members
                         for item in story["members"]}.values(),
                        key=lambda item: (item["event_date"], item["event_id"]))
        kept = members[0]
        entity_refs = sorted({entity_id for story in members
                              for entity_id in story["entity_refs"]})
        representative = min(events, key=lambda item: (-len(item["entities"]),
                                                       item["event_date"], item["event_id"]))
        merged.append(dict(kept, members=events, representative=representative,
                           entity_refs=entity_refs, first_seen=events[0]["event_date"],
                           last_update=events[-1]["event_date"],
                           theme=_majority_theme(events) or kept["theme"]))
    merged.sort(key=lambda item: (item["first_seen"], item["story_id"]))
    return merged


def _majority_theme(members):
    """The headline family most members carry (None when nobody carries one)."""
    counts = {}
    for item in members:
        if item["theme"]:
            counts[item["theme"]] = counts.get(item["theme"], 0) + 1
    if not counts:
        return None
    return sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[0][0]


def _thread_anchor(members, entity_df):
    """The entity the thread is really about: the one most of its events carry.

    Global frequency is the tie-break, so a thread anchored on a party beats one
    anchored on a person who happens to appear once more.
    """
    counts = {}
    for item in members:
        for entity_id in item["entities"]:
            counts[entity_id] = counts.get(entity_id, 0) + 1
    candidates = [(entity_id, n) for entity_id, n in counts.items() if n >= 2]
    if not candidates:
        return None
    return sorted(candidates, key=lambda pair: (-pair[1], entity_df.get(pair[0], 0), pair[0]))[0][0]


def status_for(last_update, as_of):
    """open | dormant | closed, from the corpus' latest event date (as_of)."""
    if not last_update or not as_of:
        return "open"
    try:
        last = date.fromisoformat(last_update[:10])
        latest = date.fromisoformat(as_of[:10])
    except ValueError:
        return "open"
    if last >= latest - timedelta(days=STATUS_OPEN_DAYS):
        return "open"
    if last >= latest - timedelta(days=STATUS_DORMANT_DAYS):
        return "dormant"
    return "closed"


def headline_for(story, names):
    """Ledger headline: family label + anchor + the thread's central event title."""
    theme = story["theme"]
    lead = story.get("representative") or story["seed"]
    label = THEME_LABELS.get(theme) or (lead.get("event_type") or "thread").replace("_", " ")
    label = label[:1].upper() + label[1:]
    anchor = story["anchor"]
    anchor_label = names.get(anchor, anchor)
    title = (lead["title"] or "").strip()
    if len(title) > 120:
        title = title[:117].rstrip() + "..."
    return f"{label} — {anchor_label}: {title}"


def summary_for(story):
    """Derived, never narrated: counts, dates and the latest event's title."""
    members = story["members"]
    latest = members[-1]
    latest_title = (latest["title"] or "").strip()
    if len(latest_title) > 120:
        latest_title = latest_title[:117].rstrip() + "..."
    span = story["first_seen"] or "undated"
    if story["last_update"] and story["last_update"] != story["first_seen"]:
        span = f"{span} → {story['last_update']}"
    return (f"{len(members)} event(s), {span}; latest {latest['event_date'] or 'undated'}:"
            f" {latest_title}")


# ------------------------------------------------------- database interfaces ---

def read_prior_mapping(db_path):
    """event_id -> story_id from an existing store, or {} when there is none.

    Used so a rebuild keeps the story ids the previous build published (append
    semantics: a story is never renumbered, only extended).
    """
    if not db_path or not str(db_path):
        return {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute("SELECT event_id, story_id FROM story_events").fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    mapping = {}
    for event_id, story_id in rows:
        existing = mapping.get(event_id)
        if existing is None or story_id < existing:
            mapping[event_id] = story_id
    return mapping


def build_stories(conn, as_of=None, prior=None, created_at=None):
    """Cluster the store's events and return the ledger rows (no writes)."""
    rows = conn.execute(
        "SELECT event_id, event_date, event_type, subtype, title, seat_code, state,"
        " jurisdiction, significance, window_class FROM events"
    ).fetchall()
    event_rows = [dict(zip(row.keys(), row)) if hasattr(row, "keys") else
                  {"event_id": row[0], "event_date": row[1], "event_type": row[2],
                   "subtype": row[3], "title": row[4], "seat_code": row[5],
                   "state": row[6], "jurisdiction": row[7], "significance": row[8],
                   "window_class": row[9]}
                  for row in rows]
    entity_map = {}
    for event_id, entity_id in conn.execute(
            "SELECT event_id, entity_id FROM event_entities ORDER BY event_id, entity_id"):
        entity_map.setdefault(event_id, set()).add(entity_id)
    names = {entity_id: (name or entity_id) for entity_id, name in
             conn.execute("SELECT entity_id, name FROM entities")}
    for entity_id, seat_code in conn.execute(
            "SELECT 'seat:' || seat_code, COALESCE(NULLIF(name, ''), 'seat ' || seat_code)"
            " FROM (SELECT seat_code, MAX(title) AS name FROM events"
            "        WHERE seat_code IS NOT NULL GROUP BY seat_code)"):
        names.setdefault(entity_id, seat_code)

    prepared, entity_df, weights = prepare_events(event_rows, entity_map)
    stories = cluster(prepared, entity_df, weights, prior=prior)
    if not as_of:
        # The as-of date is the sweep window's own end: post-window entries (the
        # constitutional clock, next-year projections) are IN the ledger but must
        # not move "now" forward and mark every live thread dormant.
        dated = [item["event_date"] for item in prepared if item["event_date"]
                 and item.get("window_class") != "post_window"]
        as_of = max(dated) if dated else max(
            (item["event_date"] for item in prepared if item["event_date"]), default="")
    ledger = []
    for story in stories:
        ledger.append({
            "story_id": story["story_id"],
            "headline": headline_for(story, names),
            "status": status_for(story["last_update"], as_of),
            "first_seen": story["first_seen"],
            "last_update": story["last_update"],
            "summary": summary_for(story),
            "entity_refs": story["entity_refs"],
            "theme": story["theme"],
            "anchor": story["anchor"],
            "event_count": len(story["members"]),
            "event_ids": [item["event_id"] for item in story["members"]],
        })
    return ledger


def write_stories(conn, as_of=None, prior=None, created_at=None):
    """Rebuild the story ledger in `conn` (the events must already be written).

    Returns a small stats dict for the build report. The ledger is append-only in
    meaning: story ids carried by `prior` keep their identity, and each thread's
    membership is (re)stated whole, so the views always agree with the events.
    """
    created_at = created_at or ""
    ledger = build_stories(conn, as_of=as_of, prior=prior, created_at=created_at)
    conn.execute("DELETE FROM story_events")
    conn.execute("DELETE FROM stories")
    story_rows, member_rows = [], []
    for story in ledger:
        story_rows.append((
            story["story_id"], story["headline"], story["status"], story["first_seen"],
            story["last_update"], story["summary"],
            json.dumps(story["entity_refs"], ensure_ascii=False),
            story["theme"], story["anchor"], story["event_count"], created_at,
        ))
        for event_id in story["event_ids"]:
            member_rows.append((story["story_id"], event_id, created_at))
    conn.executemany(
        "INSERT INTO stories (story_id, headline, status, first_seen, last_update, summary,"
        " entity_refs, theme, anchor, event_count, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", story_rows)
    conn.executemany(
        "INSERT INTO story_events (story_id, event_id, joined_at) VALUES (?, ?, ?)", member_rows)

    by_theme = {}
    by_status = {}
    for story in ledger:
        by_theme[story["theme"] or "(none)"] = by_theme.get(story["theme"] or "(none)", 0) + 1
        by_status[story["status"]] = by_status.get(story["status"], 0) + 1
    return {
        "stories": len(ledger),
        "story_events": len(member_rows),
        "events_clustered": len({event_id for story in ledger for event_id in story["event_ids"]}),
        "by_theme": dict(sorted(by_theme.items(), key=lambda pair: -pair[1])),
        "by_status": dict(sorted(by_status.items())),
        "largest": max((story["event_count"] for story in ledger), default=0),
        "inherited_ids": sum(1 for story in ledger
                             if prior and any(event_id in prior
                                              for event_id in story["event_ids"])),
    }
