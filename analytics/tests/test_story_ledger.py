"""Contract tests for the GE16 story ledger (clustering, views, report section).

Hermetic: the ledger is clustered over a throwaway events database built from
inline fixtures in a temp directory, so the tests never touch work/events, the
sweep dossiers or the published reports.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.events import stories as ledger  # noqa: E402
from tools.events import build_events_db as builder  # noqa: E402

SCHEMA_SQL = Path(builder.SCHEMA_PATH).read_text(encoding="utf-8")
CREATED_AT = "2026-09-24T00:00:00+00:00"

# The chain: three events on one actor set that must land in ONE story through
# entity overlap alone (their event types differ, so the headline-family and
# same-anchor rules cannot link them).
CHAIN = (
    ("evt-chain-1", "2026-01-05", "statement",
     "Alpha: fixture statement one", {"person:alpha", "party:beta", "institution:gamma"}),
    ("evt-chain-2", "2026-02-05", "appointment",
     "Beta: fixture appointment two",
     {"person:alpha", "party:beta", "institution:gamma", "seat:P900"}),
    ("evt-chain-3", "2026-03-05", "resignation",
     "Beta: fixture resignation three", {"party:beta", "institution:gamma", "seat:P900"}),
)

# Five unrelated events, so the corpus has enough events for the IDF weights to
# behave as they do on the real store (each carries one entity of its own).
FILLER = tuple(
    (f"evt-filler-{index}", f"2026-0{index}-15", "other",
     f"Isolated note number {index}", {f"person:isolated{index}"})
    for index in range(1, 6)
)



def make_db(path: Path, events) -> sqlite3.Connection:
    """A schema-v2 database with just the events/entities a test needs."""
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    entities = sorted({entity_id for _, _, _, _, ids in events for entity_id in ids})
    conn.executemany(
        "INSERT OR REPLACE INTO entities (entity_id, entity_type, name, kg_node_id,"
        " aliases_json, attrs_json) VALUES (?, ?, ?, NULL, '[]', '{}')",
        [(entity_id, entity_id.split(":")[0], entity_id.split(":", 1)[1]) for entity_id in entities],
    )
    for event_id, event_date, event_type, title, ids in events:
        conn.execute(
            "INSERT INTO events (event_id, event_date, event_date_end, date_precision,"
            " date_source, event_type, subtype, title, detail, jurisdiction, state, seat_code,"
            " significance, confidence, window_class, dossier, section, source_layer,"
            " primary_source_id, corroboration_count, source_count, raw_json, created_at)"
            " VALUES (?, ?, NULL, 'day', 'field', ?, NULL, ?, '', 'national', NULL, NULL,"
            " 'medium', 'high', 'in_window', 'fixture', 'fixture', 'dossier', NULL, 1, 0,"
            " NULL, ?)",
            (event_id, event_date, event_type, title, CREATED_AT),
        )
        conn.executemany(
            "INSERT OR IGNORE INTO event_entities (event_id, entity_id, role, mention, position)"
            " VALUES (?, ?, 'actor', NULL, 0)",
            [(event_id, entity_id) for entity_id in sorted(ids)],
        )
    conn.commit()
    return conn


def story_of(conn, event_id) -> str:
    row = conn.execute("SELECT story_id FROM story_events WHERE event_id = ?", (event_id,)).fetchone()
    assert row is not None, f"{event_id} is in no story"
    return row[0]


class StoryLedgerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _chain_db(self):
        conn = make_db(self.root / "chain.db", CHAIN + FILLER)
        stats = ledger.write_stories(conn)
        conn.commit()
        return conn, stats

    # ------------------------------------------------------------ clustering --
    def test_entity_overlap_chain_clusters_into_one_story(self):
        conn, stats = self._chain_db()
        self.assertEqual(len(FILLER) + 1, stats["stories"],
                         "the overlapping events are one thread; the isolated ones their own")
        self.assertEqual(3, stats["largest"])
        story_ids = {story_of(conn, event_id) for event_id, _, _, _, _ in CHAIN}
        self.assertEqual(1, len(story_ids), "one story for the whole chain")
        member_count = conn.execute(
            "SELECT event_count FROM stories WHERE story_id = ?", (story_ids.pop(),)).fetchone()[0]
        self.assertEqual(3, member_count)
        conn.close()

    def test_later_event_appends_to_the_existing_story_id(self):
        conn, _ = self._chain_db()
        before = story_of(conn, "evt-chain-1")
        prior = ledger.read_prior_mapping(self.root / "chain.db")
        self.assertEqual(prior["evt-chain-1"], before)
        # drop the thread's last event, then re-cluster: the later event rejoins
        # the same thread and the published id must not be re-seeded
        conn.execute("DELETE FROM story_events WHERE event_id = 'evt-chain-3'")
        conn.commit()
        after = ledger.write_stories(conn, prior=prior)
        self.assertEqual(before, story_of(conn, "evt-chain-1"))
        self.assertEqual(before, story_of(conn, "evt-chain-3"))
        self.assertEqual(3, conn.execute(
            "SELECT event_count FROM stories WHERE story_id = ?", (before,)).fetchone()[0])
        self.assertEqual(len(FILLER) + 1, after["stories"])
        conn.close()

    def test_zero_overlap_events_are_distinct_stories(self):
        events = (
            ("evt-left", "2026-01-05", "statement", "Left note", {"person:left"}),
            ("evt-right", "2026-01-06", "appointment", "Right note", {"person:right"}),
        )
        conn = make_db(self.root / "disjoint.db", events)
        stats = ledger.write_stories(conn)
        self.assertEqual(2, stats["stories"])
        self.assertNotEqual(story_of(conn, "evt-left"), story_of(conn, "evt-right"))
        conn.close()

    # ---------------------------------------------------------------- views ---
    def test_story_timeline_is_ordered_and_open_stories_are_current(self):
        conn, _ = self._chain_db()
        story_id = story_of(conn, "evt-chain-1")
        rows = conn.execute(
            "SELECT seq, event_id, event_date, event_entities FROM v_story_timeline"
            " WHERE story_id = ? ORDER BY seq", (story_id,)).fetchall()
        self.assertEqual([1, 2, 3], [row[0] for row in rows])
        self.assertEqual(["evt-chain-1", "evt-chain-2", "evt-chain-3"], [row[1] for row in rows])
        self.assertEqual(["2026-01-05", "2026-02-05", "2026-03-05"], [row[2] for row in rows])
        self.assertTrue(all(row[3] for row in rows), "each timeline row carries its entities")
        current = conn.execute(
            "SELECT story_id, status, last_update, latest_event_title, event_count"
            " FROM v_story_current WHERE story_id = ?", (story_id,)).fetchone()
        self.assertEqual("open", current[1])
        self.assertEqual("2026-03-05", current[2])
        self.assertEqual("Beta: fixture resignation three", current[4 - 1])
        conn.close()

    def test_ledger_is_append_only_and_every_event_is_in_exactly_one_story(self):
        conn, _ = self._chain_db()
        total_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        members = conn.execute("SELECT COUNT(*) FROM story_events").fetchone()[0]
        self.assertEqual(total_events, members)
        self.assertEqual(0, conn.execute(
            "SELECT COUNT(*) FROM (SELECT event_id FROM story_events"
            " GROUP BY event_id HAVING COUNT(*) > 1)").fetchone()[0])
        # re-running the ledger adds no duplicate membership and keeps the ids
        before = {row[0] for row in conn.execute("SELECT story_id FROM stories")}
        ledger.write_stories(conn, prior=ledger.read_prior_mapping(self.root / "chain.db"))
        after = {row[0] for row in conn.execute("SELECT story_id FROM stories")}
        self.assertEqual(before, after)
        self.assertEqual(members, conn.execute("SELECT COUNT(*) FROM story_events").fetchone()[0])
        conn.close()

    def test_status_window_follows_the_corpus_not_the_wall_clock(self):
        events = (
            ("evt-old", "2026-01-05", "statement", "Old note", {"person:old"}),
            ("evt-new", "2026-07-05", "statement", "New note", {"person:last"}),
        )
        conn = make_db(self.root / "status.db", events)
        ledger.write_stories(conn)
        rows = dict(conn.execute("SELECT story_id, status FROM stories").fetchall())
        self.assertEqual("open", rows[story_of(conn, "evt-new")])
        self.assertEqual("closed", rows[story_of(conn, "evt-old")])
        conn.close()


class StoryReportSectionTests(unittest.TestCase):
    """The section-2 render, driven from a fixture ledger (no report build)."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.db_path = cls.root / "ge16-events.db"
        conn = make_db(cls.db_path, CHAIN + FILLER)
        ledger.write_stories(conn, created_at=CREATED_AT)
        conn.commit()
        conn.close()
        cls.report_builder = cls._import_report_builder()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    @staticmethod
    def _import_report_builder():
        import importlib.util
        engine = REPOSITORY_ROOT / "02_FORECAST" / "engine"
        spec = importlib.util.spec_from_file_location(
            "story_section_report_builder", engine / "report_builder.py")
        module = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(engine))
        spec.loader.exec_module(module)
        sys.path.pop(0)
        return module

    def _data(self):
        builder_module = self.report_builder
        original = builder_module.EVENTS_DB
        builder_module.EVENTS_DB = str(self.db_path)
        try:
            return builder_module.read_story_threads()
        finally:
            builder_module.EVENTS_DB = original

    def test_reader_exposes_the_ledger_rows(self):
        data = self._data()
        self.assertTrue(data["available"], data.get("reason"))
        self.assertEqual(1 + len(FILLER), data["total"], "the chain is one thread")
        self.assertEqual("2026-05-15", data["as_of"])
        self.assertEqual("open", data["threads"][0]["status"])

    def test_en_section_renders_header_counts_and_last_update(self):
        block = self.report_builder.story_threads_en(self._data())
        self.assertIn("### 2.1 Political developments", block)
        self.assertIn("**As of 2026-05-15:**", block)
        self.assertIn("Beta:", block)
        self.assertIn("| 2026-03-05 |", block, "the chain's last_update date is rendered")
        self.assertIn(
            "| Chain of events | Items covered | First development | Last update | Status | "
            "Latest development |", block)
        # The section is read by a political reader, not by the harness: none of
        # the machinery's own words may reach the page.
        for machinery in ("ledger", "schema", "events.db", "story_id", "anchor",
                          "query_events", "window", "vintage"):
            self.assertNotIn(machinery, block.lower(), machinery)

    def test_ms_section_renders_the_same_ledger(self):
        block = self.report_builder.story_threads_ms(self._data())
        self.assertIn("### 2.1 Perkembangan politik", block)
        self.assertIn("**Setakat 2026-05-15:**", block)
        self.assertIn("| 2026-03-05 |", block)

    def test_section_is_deterministic_and_states_an_absent_ledger(self):
        first = self.report_builder.story_threads_en(self._data())
        self.assertEqual(first, self.report_builder.story_threads_en(self._data()))
        missing = self.report_builder.story_threads_en(
            {"available": False, "reason": "events ledger not built"})
        self.assertIn("No political developments are shown", missing)
        self.assertNotIn("ledger", missing.lower())
        self.assertNotIn("| Chain of events |", missing)

    def test_report_templates_carry_the_story_block_in_both_languages(self):
        source = (REPOSITORY_ROOT / "02_FORECAST" / "engine" / "report_builder.py").read_text(
            encoding="utf-8")
        self.assertIn("{story_threads_block}", source)
        self.assertIn("story_threads_block_ms = story_threads_ms(", source)
        ms_source = (REPOSITORY_ROOT / "02_FORECAST" / "engine" / "federal_ms_render.py").read_text(
            encoding="utf-8")
        self.assertIn('ctx["story_threads_block_ms"]', ms_source)
        self.assertIn("{story_threads_block_ms}", ms_source)

    def test_week_over_week_delta_is_demoted_to_a_transitional_footnote(self):
        source = (REPOSITORY_ROOT / "02_FORECAST" / "engine" / "report_builder.py").read_text(
            encoding="utf-8")
        self.assertIn("**Week-over-week delta (transitional footnote).**", source)
        self.assertNotIn("**Narrative continuity (week-over-week).**", source)
        ms_source = (REPOSITORY_ROOT / "02_FORECAST" / "engine" / "federal_ms_render.py").read_text(
            encoding="utf-8")
        self.assertIn("**Delta minggu ke minggu (nota kaki peralihan).**", ms_source)

    def test_report_window_constant_matches_the_ledger_declaration(self):
        self.assertEqual(self.report_builder.STORY_OPEN_WINDOW_DAYS, ledger.STATUS_OPEN_DAYS)



