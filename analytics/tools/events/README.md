# GE16 relational events database (SQLite + FTS5)

The third structured store in the GE16 analytics stack, and the one the domain was
missing: the knowledge graph holds *entities and their relationships*, the vector
databases hold *semantic neighbourhoods*, and this store holds **dated, typed,
sourced political events with entity roles, event-to-event relations and numeric
metric payloads**.

```
tools/events/
  schema.sql            DDL: 13 tables, 12 views, 2 FTS5 virtual indexes plus their
                        shadow tables (schema version 2; story ledger appended in v2)
  extractors.py         source readers: dossier markdown/JSON + cron update layers
  entity_index.py       entity index + mention resolution (shares ids with the graph)
  stories.py            story ledger: deterministic clustering of events into threads
  build_events_db.py    ETL: extract → resolve → dedupe → link → write → self-check
  query_events.py       read-only CLI over the store

01_RESEARCH/events/_draft/     staged sweep dossiers (input layer)
work/events/ge16-events.db     the database (generated)
work/events/ge16-events-stats.json   build report (counts, coverage, checks)
```

## Build and query

```bash
./.venv/bin/python tools/events/build_events_db.py            # rebuild the store
./.venv/bin/python tools/events/build_events_db.py --dossier-dir DIR --out DB
./.venv/bin/python tools/events/query_events.py search "RoS show-cause PN"
./.venv/bin/python tools/events/query_events.py timeline --type by_election
./.venv/bin/python tools/events/query_events.py entity "Wong Chen"
./.venv/bin/python tools/events/query_events.py event evt-…   links evt-…   claims
./.venv/bin/python tools/events/query_events.py metrics --metric "GDP yoy"
./.venv/bin/python tools/events/query_events.py stories            # open threads
./.venv/bin/python tools/events/query_events.py --stories --all     # every thread
./.venv/bin/python tools/events/query_events.py --story story-…     # one thread + its events
./.venv/bin/python tools/events/query_events.py stats
./.venv/bin/python tools/events/query_events.py sql "SELECT event_type, COUNT(*) FROM events GROUP BY 1"
```

The CLI opens the file read-only (`mode=ro`) and refuses anything that is not a
single `SELECT`/`WITH`. `search` quotes bare tokens containing punctuation
(`show-cause` → `"show-cause"`); pass `"`, `*`, `(`, `AND`/`OR`/`NOT`/`NEAR` to keep
raw FTS5 syntax.

## Inputs

| Dossier / layer | Ingestion |
|---|---|
| `ge16-sweep-partA-federal.md` | markdown bullets, `\|` tables (§0 corrections, §1.7 by-elections, §4 appointments, §6 approval), §8 engine findings, §9 corrections, §10 unknowns |
| `ge16-sweep-partB-states.json` | per-state vacancies, by-elections, composition changes, MB changes, state polls, baseline corrections |
| `ge16-sweep-partC-polls-macro.json` | national/state polls, approval + leader preference, DOSM prints, BNM OPR, FX, corrections, unknowns |
| `01_RESEARCH/figures/_draft/*-updates-*.json` | cron party history lines, personnel role changes, poll releases (`--no-drafts` to skip) |
| knowledge graph, personnel/party vectors, `1_DATA` master list + DUN map | **read-only** entity identity and seat resolution |

Entity ids are shared with the knowledge graph: `entities.kg_node_id` carries
`person:wong chen`, `party:pkr`, `seat:P104`, … so an event can be joined to the
graph without a name-matching step.

## Data model

`events` (date, precision, type, subtype, title, detail, jurisdiction, seat, window
class, significance, confidence, corroboration), `entities`, `event_entities`
(roles: actor/seat/state/bloc/institution/pollster), `event_sources`, `sources`,
`event_links` (`follows` = by-election/switch after its trigger; `precedes` =
consecutive events on the same seat), `event_metrics` (numeric payloads),
`claim_reviews` (corrections/unknowns/engine findings), `dossier_notes`,
`ingest_runs` (provenance), `stories` + `story_events` (the append-only story
ledger). Views: `v_event_full`, `v_timeline`,
`v_entity_activity`, `v_seat_history`, `v_event_links_full`, `v_source_usage`,
`v_open_claims`, `v_metric_series`, `v_corroboration`, `v_notes`,
`v_story_timeline`, `v_story_current`.
FTS5: `events_fts`, `entities_fts`.

## Story ledger (schema v2)

A **story** is one dated thread of related events (an RoS action against a party,
a by-election chain on one seat, a coalition split, GE16-timing signals). The
ledger is *derived*, never typed: `stories.py` re-clusters the events on every
build, so the ledger can always be re-derived from `events` + `event_entities`.

Membership rules (deterministic, in `stories.py`):

1. **shared actors** — IDF-weighted Jaccard of the two events' entity sets
   ≥ `STORY_JACCARD` (0.6). Weighting is what stops the ubiquitous entities
   (PN, BN, PH, UMNO, the EC) from fusing unrelated events into one blob.
2. **headline family** — both events match the same keyword family (RoS and
   suspension, vacancy and by-election chain, party split and merger, seat talks,
   GE16 timing and dissolution, court cases) **and** carry the same anchor.
3. **series** — neither event matches a family, but both share an event type and
   the same *specific* anchor (an entity carried by at most `ANCHOR_DF_MAX` = 6
   events), e.g. one pollster's monthly releases.

`story_id` is seeded on the thread's anchor + its first event, and a rebuild
inherits the ids the previous build published (`read_prior_mapping`), so a later
event **appends** to its thread instead of spawning a second one. `status` is
`open | dormant | closed` relative to the sweep window's own end date (not the
wall clock), so a rebuild of the same data is byte-identical. Every event belongs
to exactly one thread; single-event threads are kept, not discarded.

## Extraction rules (deterministic, reviewable)

- **Dates**: ISO, `17 May 2026`, `8–9 Jun 2026`, `26 Jun–9 Jul 2026`, `May 2026`,
  `Q2 2026`, year — URL slugs are masked for classification but used as a date
  fallback; every event records `date_precision` and `date_source`.
- **Type**: ordered keyword rules; a match is skipped when the text negates it
  ("no by-election", "seat NOT vacant", "differs from the earlier by-election").
  A structured layer label wins unless it is generic and the text asserts a
  specific strong type (the label is then kept as subtype `layer:<label>`).
  Significance and confidence derive from type + source strength.
- **Who/where**: mentions resolve against the knowledge graph, master seat list and
  DUN→parliament mapping. A constituency named without its code needs an electoral
  cue nearby (so "Kota Kinabalu" in a hospital dateline is not seat P172); a record's
  own seat declaration (`seat P104`, `(N.13)`) outranks the first mention in prose.
- **Dedupe**: same `date|type|seat|primary actor` collapses, plus a second pass for
  the same date/actor/seat with ≥70% title-token overlap, plus a third that folds a
  seat-less report of the same date/type/actor into the seated one. Merged rows keep
  every citation and a `corroboration_count`; `raw_json` retains the source layers.
  `GE16_EVENTS_DEBUG_MERGE=1` prints what the third pass folds.
- **Self-checks** fail the build if FTS row counts drift from the tables, foreign
  keys are broken, coverage drops below the floor, or duplicate ids appear.
