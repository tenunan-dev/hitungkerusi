-- GE16 relational events database — SQLite schema (FTS5 full-text layer).
--
-- Third structured store in the GE16 analytics stack, alongside:
--   * the knowledge graph   (work/graph/ge16-knowledge-graph.json, 2,813 nodes / 95,723 edges)
--   * the vector databases  (01_RESEARCH/figures/ge16-*.npy + -meta.json, 384-dim MiniLM)
-- It carries what neither of those can express: dated, typed, sourced POLITICAL EVENTS
-- with entity roles, event-to-event relations and numeric metric payloads.
--
-- Entity identity is deliberately shared with the knowledge graph: entities.kg_node_id
-- holds the graph node id ("person:wong chen", "party:pkr", "seat:P104") so the two
-- stores can be joined without a name-matching step.
--
-- Schema version: 2 (v2 adds the append-only story ledger: stories, story_events,
-- v_story_timeline, v_story_current — see the end of this file)

CREATE TABLE schema_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);

-- ---------------------------------------------------------------- sources ---
-- One row per cited URL. Every event points at its primary citation here and may
-- carry further corroborating citations through event_sources.
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
CREATE INDEX idx_sources_domain ON sources(domain);
CREATE INDEX idx_sources_published ON sources(published_date);

-- --------------------------------------------------------------- entities ---
CREATE TABLE entities (
    entity_id        TEXT PRIMARY KEY,
    entity_type      TEXT NOT NULL
                     CHECK (entity_type IN ('person','party','bloc','seat','state','institution','cluster')),
    name             TEXT NOT NULL,
    kg_node_id       TEXT,                       -- join key into the knowledge graph
    aliases_json     TEXT NOT NULL DEFAULT '[]',
    attrs_json       TEXT NOT NULL DEFAULT '{}',
    first_event_date TEXT,
    last_event_date  TEXT,
    event_count      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_entities_type ON entities(entity_type);
CREATE INDEX idx_entities_kg ON entities(kg_node_id);

-- ----------------------------------------------------------------- events ---
CREATE TABLE events (
    event_id            TEXT PRIMARY KEY,
    event_date          TEXT,                    -- ISO 8601, truncated to date_precision
    event_date_end      TEXT,                    -- range upper bound when the source gives a span
    date_precision      TEXT NOT NULL
                        CHECK (date_precision IN ('day','month','quarter','year','unknown')),
    date_source         TEXT NOT NULL
                        CHECK (date_source IN ('text','url_slug','field','window','unknown')),
    event_type          TEXT NOT NULL,
    subtype             TEXT,
    title               TEXT NOT NULL,
    detail              TEXT NOT NULL,
    jurisdiction        TEXT NOT NULL,           -- federal | state:<Name> | national | external
    state               TEXT,
    seat_code           TEXT,                    -- P### / N.## when the seat is resolvable
    significance        TEXT NOT NULL CHECK (significance IN ('critical','high','medium','low')),
    confidence          TEXT NOT NULL CHECK (confidence IN ('high','medium','low')),
    window_class        TEXT NOT NULL
                        CHECK (window_class IN ('antecedent','in_window','post_window','undated')),
    dossier             TEXT NOT NULL,           -- partA-federal | partB-states | partC-polls-macro | party-updates | personnel-updates | polls-update
    section             TEXT,                    -- dossier section / state / party name
    source_layer        TEXT NOT NULL,           -- dossier | cron-update
    primary_source_id   INTEGER REFERENCES sources(source_id) ON DELETE SET NULL,
    corroboration_count INTEGER NOT NULL DEFAULT 1,
    source_count        INTEGER NOT NULL DEFAULT 0,
    raw_json            TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX idx_events_date ON events(event_date);
CREATE INDEX idx_events_type ON events(event_type);
CREATE INDEX idx_events_jurisdiction ON events(jurisdiction);
CREATE INDEX idx_events_seat ON events(seat_code);
CREATE INDEX idx_events_window ON events(window_class);
CREATE INDEX idx_events_sig ON events(significance);

-- Event to source citations: exactly one 'primary' row per event that has a URL,
-- plus zero or more 'corroborating' rows.
CREATE TABLE event_sources (
    event_id    TEXT NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
    source_id   INTEGER NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
    relation    TEXT NOT NULL CHECK (relation IN ('primary','corroborating')),
    PRIMARY KEY (event_id, source_id)
);

-- Entity mentions with the role the entity plays in that event.
CREATE TABLE event_entities (
    event_id    TEXT NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
    entity_id   TEXT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    role        TEXT NOT NULL
                CHECK (role IN ('actor','seat','state','bloc','institution','subject','pollster')),
    mention     TEXT,                            -- surface form found in the text
    position    INTEGER NOT NULL DEFAULT 0,      -- character offset of the first mention
    PRIMARY KEY (event_id, entity_id, role)
);
CREATE INDEX idx_event_entities_entity ON event_entities(entity_id, role);

-- Event to event relations: the "relational" half of the store.
--   follows   : a consequential event (by-election, EC ruling, switch) following its trigger
--   precedes  : consecutive events on the same seat, chronological chain
--   supersedes: a later event that revises an earlier record of the same fact
CREATE TABLE event_links (
    link_id        INTEGER PRIMARY KEY,
    from_event_id  TEXT NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
    to_event_id    TEXT NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
    relation       TEXT NOT NULL CHECK (relation IN ('follows','precedes','supersedes')),
    basis          TEXT NOT NULL,                -- why the link was derived
    gap_days       INTEGER,
    UNIQUE (from_event_id, to_event_id, relation)
);
CREATE INDEX idx_event_links_from ON event_links(from_event_id);
CREATE INDEX idx_event_links_to ON event_links(to_event_id);

-- Numeric payloads (poll percentages, GDP/CPI prints, OPR, FX, vote shares) so the
-- store can be queried quantitatively and not only by text.
CREATE TABLE event_metrics (
    event_id   TEXT NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
    metric     TEXT NOT NULL,
    value_num  REAL,
    value_text TEXT,
    unit       TEXT,
    PRIMARY KEY (event_id, metric)
);
CREATE INDEX idx_event_metrics_metric ON event_metrics(metric);

-- ------------------------------------------------- claim / correction layer ---
-- Baseline-brief assertions checked by the sweep: corrections, engine-config
-- findings and questions the research could not close.
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
CREATE INDEX idx_claim_kind ON claim_reviews(kind, verdict);

-- Free-text context that is NOT an event (state notes, CM-rotation findings,
-- sweep meta remarks). Kept separate so no fabricated date is ever attached.
CREATE TABLE dossier_notes (
    note_id   INTEGER PRIMARY KEY,
    dossier   TEXT NOT NULL,
    section   TEXT,
    kind      TEXT NOT NULL DEFAULT 'context',
    body      TEXT NOT NULL,
    source_id INTEGER REFERENCES sources(source_id) ON DELETE SET NULL
);

-- ------------------------------------------------------------- provenance ---
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

-- ---------------------------------------------------------- full-text layer ---
-- Standalone FTS5 tables (not external-content) so every indexed document can be
-- rebuilt from scratch per run and snippet()/highlight() stay available. The
-- *_id columns are UNINDEXED join keys back to the relational tables.
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

-- ---------------------------------------------------------------- views ------
CREATE VIEW v_event_full AS
SELECT e.event_id            AS event_id,
       e.event_date          AS event_date,
       e.event_type          AS event_type,
       e.title               AS title,
       e.jurisdiction        AS jurisdiction,
       e.state               AS state,
       e.significance        AS significance,
       e.confidence          AS confidence,
       e.window_class        AS window_class,
       e.dossier             AS dossier,
       s.url                 AS source_url,
       s.publisher           AS publisher,
       e.corroboration_count AS corroboration,
       (SELECT GROUP_CONCAT(x.name, '; ')
          FROM (SELECT en.name AS name
                  FROM event_entities ee
                  JOIN entities en ON en.entity_id = ee.entity_id
                 WHERE ee.event_id = e.event_id AND ee.role = 'actor'
                 ORDER BY en.name) x) AS actors,
       e.seat_code           AS seat_code
  FROM events e
  LEFT JOIN sources s ON s.source_id = e.primary_source_id;

CREATE VIEW v_timeline AS
SELECT event_date, event_type, title, jurisdiction, significance, confidence, event_id
  FROM events
 WHERE event_date IS NOT NULL
 ORDER BY event_date, event_type;

CREATE VIEW v_entity_activity AS
SELECT en.entity_id,
       en.entity_type,
       en.name,
       COUNT(ee.event_id)                        AS event_count,
       MIN(e.event_date)                         AS first_event_date,
       MAX(e.event_date)                         AS last_event_date,
       GROUP_CONCAT(DISTINCT e.event_type)        AS event_types
  FROM entities en
  JOIN event_entities ee ON ee.entity_id = en.entity_id
  JOIN events e ON e.event_id = ee.event_id
 GROUP BY en.entity_id, en.entity_type, en.name;

CREATE VIEW v_seat_history AS
SELECT e.seat_code       AS seat_code,
       e.event_date      AS event_date,
       e.event_type      AS event_type,
       e.title           AS title,
       e.event_id        AS event_id,
       en.name           AS actor
  FROM events e
  LEFT JOIN event_entities ee ON ee.event_id = e.event_id AND ee.role = 'actor'
  LEFT JOIN entities en ON en.entity_id = ee.entity_id
 WHERE e.seat_code IS NOT NULL
 ORDER BY e.seat_code, e.event_date;

CREATE VIEW v_event_links_full AS
SELECT l.link_id,
       l.relation,
       l.basis,
       l.gap_days,
       a.event_date AS from_date,
       a.event_type AS from_type,
       a.title      AS from_title,
       a.event_id   AS from_event_id,
       b.event_date AS to_date,
       b.event_type AS to_type,
       b.title      AS to_title,
       b.event_id   AS to_event_id
  FROM event_links l
  JOIN events a ON a.event_id = l.from_event_id
  JOIN events b ON b.event_id = l.to_event_id;

CREATE VIEW v_source_usage AS
SELECT s.source_id, s.url, s.publisher, s.tier,
       COUNT(es.event_id) AS event_count
  FROM sources s
  LEFT JOIN event_sources es ON es.source_id = s.source_id
 GROUP BY s.source_id, s.url, s.publisher, s.tier;

CREATE VIEW v_open_claims AS
SELECT claim_id, dossier, section, kind, verdict, confidence, claim_text,
       baseline_value, verified_value
  FROM claim_reviews
 WHERE verdict IN ('unresolved','do_not_use')
 ORDER BY dossier, claim_id;

CREATE VIEW v_metric_series AS
SELECT m.metric,
       m.unit,
       COUNT(*)      AS observations,
       MIN(m.value_num) AS min_value,
       MAX(m.value_num) AS max_value,
       (SELECT e.event_date FROM event_metrics m2
          JOIN events e ON e.event_id = m2.event_id
         WHERE m2.metric = m.metric AND m2.value_num IS NOT NULL
         ORDER BY e.event_date DESC LIMIT 1) AS latest_date
  FROM event_metrics m
 WHERE m.value_num IS NOT NULL
 GROUP BY m.metric, m.unit;

CREATE VIEW v_corroboration AS
SELECT event_id, event_date, event_type, title, corroboration_count, source_count,
       CASE WHEN corroboration_count > 1 THEN 'multi' ELSE 'single' END AS corroboration_class
  FROM events
 WHERE date_precision <> 'unknown';

CREATE VIEW v_notes AS
SELECT note_id, dossier, section, kind, body, source_id
  FROM dossier_notes
 ORDER BY dossier, note_id;

-- ============================================================ story ledger ===
-- Schema version 2 (2026-09-24). APPEND-ONLY: a story is never deleted, only
-- extended (story_events gains rows as later events join it) and re-stamped.
--
-- A story is the narrative layer above the event layer: one dated thread of
-- related events (RoS action against a party, a by-election chain on one seat,
-- a coalition split, GE16-timing signals). Events keep their own identity — a
-- story only RECORDS membership, so every story can be re-derived from
-- events + event_entities by tools/events/stories.py (deterministic clustering).
--
-- Membership rules live in stories.py (entity-set Jaccard + headline keyword
-- families); this schema stores only the resulting ledger plus its provenance:
--   story_id   stable across rebuilds (seeded on the thread's first event)
--   status     open | dormant | closed, derived from last_update vs the corpus
--   entity_refs JSON array of the entity ids the thread touches
CREATE TABLE stories (
    story_id    TEXT PRIMARY KEY,
    headline    TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('open','dormant','closed')),
    first_seen  TEXT NOT NULL,          -- earliest member event date
    last_update TEXT NOT NULL,          -- latest member event date
    summary     TEXT NOT NULL,
    entity_refs TEXT NOT NULL DEFAULT '[]',   -- JSON array of entity ids
    theme       TEXT,                   -- keyword family that seeded the thread
    anchor      TEXT,                   -- seat:/entity:/state: key it is anchored on
    event_count INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_stories_status ON stories(status, last_update);
CREATE INDEX idx_stories_theme ON stories(theme);

-- Story membership: append-only, one row per (story, event).
CREATE TABLE story_events (
    story_id     TEXT NOT NULL REFERENCES stories(story_id) ON DELETE CASCADE,
    event_id     TEXT NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
    joined_at    TEXT NOT NULL,
    PRIMARY KEY (story_id, event_id)
);
CREATE INDEX idx_story_events_event ON story_events(event_id);

-- Story + its ordered events + the entities each event carries.
CREATE VIEW v_story_timeline AS
SELECT s.story_id, s.headline, s.status, s.theme, s.anchor,
       s.first_seen, s.last_update, s.event_count,
       e.event_id, e.event_date, e.event_type, e.subtype, e.title,
       e.seat_code, e.jurisdiction, e.significance,
       ROW_NUMBER() OVER (PARTITION BY s.story_id
                          ORDER BY e.event_date, e.event_id) AS seq,
       (SELECT group_concat(x.entity_id, ',')
          FROM (SELECT ee.entity_id FROM event_entities ee
                 WHERE ee.event_id = e.event_id ORDER BY ee.entity_id) x) AS event_entities
  FROM stories s
  JOIN story_events se ON se.story_id = s.story_id
  JOIN events e ON e.event_id = se.event_id;

-- The open stories only (what a report may show as live threads), each with its
-- own latest event so a thread that was not touched this week still renders its
-- last_update date and status instead of being dropped.
CREATE VIEW v_story_current AS
SELECT s.story_id, s.headline, s.status, s.theme, s.anchor, s.summary,
       s.first_seen, s.last_update, s.entity_refs, s.event_count,
       (SELECT MAX(e.event_date) FROM story_events se2
          JOIN events e ON e.event_id = se2.event_id
         WHERE se2.story_id = s.story_id) AS last_event_date,
       (SELECT e.title FROM story_events se3
          JOIN events e ON e.event_id = se3.event_id
         WHERE se3.story_id = s.story_id
         ORDER BY e.event_date DESC, e.event_id DESC LIMIT 1) AS latest_event_title,
       (SELECT MIN(e.event_date) FROM story_events se4
          JOIN events e ON e.event_id = se4.event_id
         WHERE se4.story_id = s.story_id) AS first_event_date
  FROM stories s
 WHERE s.status = 'open'
 ORDER BY s.last_update DESC, s.event_count DESC, s.story_id;
