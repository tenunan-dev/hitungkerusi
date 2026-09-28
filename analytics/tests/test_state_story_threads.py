"""Contract tests for the state reports' story-thread section (§2.1 scoped, T1.5).

Hermetic: a throwaway schema-v2 ledger is built in a temp directory from inline
fixtures, so the tests never touch work/events, the sweep dossiers or the
published reports.

What is asserted:
  * the state read REUSES the federal §2.1 helper (same columns, same as-of
    window) and only narrows it to one state's story ids;
  * qualification is ledger-native: a seat entity of the state, a party field
    office / listing anchored to it (state entity or the event's state column),
    or the event row's jurisdiction — and a seat code that collides with
    another state's seat does not cross the boundary;
  * a quiet state renders the single zero-thread line in both languages, and an
    absent ledger says so instead of inventing threads;
  * the section is wired into the report build immediately after the signals
    section, in both languages.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = REPOSITORY_ROOT / "02_FORECAST" / "engine"
for _path in (str(REPOSITORY_ROOT), str(ENGINE_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from tools.events import build_events_db as builder  # noqa: E402
from tools.events import stories as ledger  # noqa: E402

import report_builder  # noqa: E402  — the federal §2.1 helper, reused
import state_report_builder  # noqa: E402  — the state report's section

SCHEMA_SQL = Path(builder.SCHEMA_PATH).read_text(encoding="utf-8")
CREATED_AT = "2026-09-24T00:00:00+00:00"

# Entities, keyed exactly the way tools/events/build_events_db.py keys them:
# a seat entity's attrs carry the state it belongs to (that is where the builder
# reads a record's state from), and states are 'state:<name>' entities.
ENTITIES = {
    "seat:N.01 TITI TINGGI": ("seat", {"kind": "dun", "state": "Perlis", "federal": "P001"}),
    # the same seat CODE in another state: the filter must not cross the boundary
    "seat:N.01 AYER HANGAT": ("seat", {"kind": "dun", "state": "Kedah", "federal": "P004"}),
    "seat:P100 PANDAN": ("seat", {"kind": "parliamentary", "state": "Selangor", "federal": "P100"}),
    "state:Perlis": ("state", {}),
    "party:beta": ("party", {}),
}

# One event per qualification route; every event carries a person entity of its
# own so the clustering never merges two routes into one story.
# (event_id, date, type, title, entity ids, state, jurisdiction)
EVENTS = (
    ("evt-seat", "2026-02-01", "candidacy", "Fixture seat filing", ["person:seat", "seat:N.01 TITI TINGGI"],
     None, "federal"),                                    # (a) seat entity of Perlis
    ("evt-listing", "2026-02-02", "appointment", "Fixture field office", ["person:listing", "state:Perlis", "party:beta"],
     None, "national"),                                   # (b) field-office listing anchored to the state
    ("evt-state-column", "2026-02-03", "statement", "Fixture state column", ["person:column"],
     "Perlis", "national"),                               # (b) the event's own state
    ("evt-jurisdiction", "2026-02-04", "state_election", "Fixture jurisdiction", ["person:juris"],
     None, "state:Perlis"),                               # (c) the event row's jurisdiction
    ("evt-penang-alias", "2026-02-05", "statement", "Fixture Penang spelling", ["person:penang"],
     "Penang", "national"),                               # ledger spelling of Pulau Pinang
    ("evt-other-state-seat", "2026-02-06", "vacancy", "Fixture Kedah seat", ["person:kedah", "seat:N.01 AYER HANGAT"],
     None, "federal"),                                    # belongs to Kedah, not Perlis
)

PERLIS_STORIES = ("evt-seat", "evt-listing", "evt-state-column", "evt-jurisdiction")


def make_db(path: Path) -> sqlite3.Connection:
    """A schema-v2 ledger carrying the fixtures above, clustered like the real one."""
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    conn.executemany(
        "INSERT OR REPLACE INTO entities (entity_id, entity_type, name, kg_node_id,"
        " aliases_json, attrs_json) VALUES (?, ?, ?, NULL, '[]', ?)",
        [(entity_id, entity_type, entity_id.split(":", 1)[1], json.dumps(attrs))
         for entity_id, (entity_type, attrs) in ENTITIES.items()],
    )
    for event_id, event_date, event_type, title, ids, state, jurisdiction in EVENTS:
        conn.execute(
            "INSERT INTO events (event_id, event_date, event_date_end, date_precision,"
            " date_source, event_type, subtype, title, detail, jurisdiction, state, seat_code,"
            " significance, confidence, window_class, dossier, section, source_layer,"
            " primary_source_id, corroboration_count, source_count, raw_json, created_at)"
            " VALUES (?, ?, NULL, 'day', 'field', ?, NULL, ?, '', ?, ?, NULL,"
            " 'medium', 'high', 'in_window', 'fixture', 'fixture', 'dossier', NULL, 1, 0,"
            " NULL, ?)",
            (event_id, event_date, event_type, title, jurisdiction, state, CREATED_AT),
        )
        conn.executemany(
            "INSERT OR IGNORE INTO event_entities (event_id, entity_id, role, mention, position)"
            " VALUES (?, ?, 'actor', NULL, 0)",
            [(event_id, entity_id) for entity_id in ids],
        )
    ledger.write_stories(conn)
    conn.commit()
    return conn


def story_of(conn, event_id) -> str:
    row = conn.execute("SELECT story_id FROM story_events WHERE event_id = ?", (event_id,)).fetchone()
    assert row is not None, f"{event_id} is in no story"
    return row[0]


class StateStoryThreadsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "state-ledger.db"
        self.conn = make_db(self.db_path)
        self._original_db = report_builder.EVENTS_DB
        report_builder.EVENTS_DB = str(self.db_path)
        self._original_lang = state_report_builder.LANG

    def tearDown(self):
        report_builder.EVENTS_DB = self._original_db
        state_report_builder.LANG = self._original_lang
        self.conn.close()
        self._tmp.cleanup()

    # --------------------------------------------------------- qualification --
    def test_state_reader_qualifies_on_seat_entity_state_and_jurisdiction(self):
        """Each qualification route in the spec pulls its story in — and only its own."""
        data = report_builder.read_state_story_threads("Perlis")
        expected = {story_of(self.conn, event_id) for event_id in PERLIS_STORIES}
        self.assertTrue(data["available"])
        self.assertEqual(expected, set(data["story_ids"]))
        self.assertEqual(len(expected), data["total"])
        self.assertEqual(expected, {thread["story_id"] for thread in data["threads"]})

    def test_a_seat_code_from_another_state_does_not_cross_the_boundary(self):
        """'N.01' exists in every state: the filter goes through attrs_json, not the code."""
        kedah_story = story_of(self.conn, "evt-other-state-seat")
        self.assertNotIn(kedah_story, report_builder.read_state_story_threads("Perlis")["story_ids"])
        self.assertEqual(
            [kedah_story], list(report_builder.read_state_story_threads("Kedah")["story_ids"]),
            "the same seat code belongs to the state its seat entity is anchored to")

    def test_ledger_state_spellings_map_to_the_report_state(self):
        """The ledger writes 'Penang'/'Malacca'; the reports are Pulau Pinang/Melaka."""
        penang = report_builder.qualifying_story_ids_for_state("Pulau Pinang")
        self.assertEqual([story_of(self.conn, "evt-penang-alias")], list(penang))
        self.assertEqual([], list(report_builder.qualifying_story_ids_for_state("Melaka")))

    def test_state_read_is_the_federal_read_narrowed_to_one_state(self):
        """§2.1's helper is reused, not re-implemented: same columns, same window."""
        everything = report_builder.read_story_threads()
        scoped = report_builder.read_state_story_threads("Perlis")
        self.assertEqual(len(EVENTS), everything["total"])
        self.assertEqual(everything["as_of"], scoped["as_of"], "same as-of window")
        union = set()
        for state in ("Perlis", "Kedah", "Selangor", "Pulau Pinang", "Terengganu"):
            union |= set(report_builder.read_state_story_threads(state)["story_ids"])
        self.assertEqual({thread["story_id"] for thread in everything["threads"]}, union,
                         "the state scopes partition the ledger: nothing is invented, nothing lost")
        self.assertEqual(set(), set(report_builder.read_state_story_threads("Terengganu")["story_ids"]),
                         "a state with no referenced story contributes nothing")
        for key in ("story_id", "headline", "status", "anchor", "first_seen",
                    "last_update", "event_count", "latest_event_title"):
            self.assertIn(key, scoped["threads"][0], f"{key} is a v_story_current column")

    def test_status_counts_match_the_ledger_rows_for_the_state(self):
        """Regression: the scoped counts are grouped per status, not a bare column."""
        for state in ("Perlis", "Kedah", "Pulau Pinang", "Terengganu"):
            data = report_builder.read_state_story_threads(state)
            ids = list(data["story_ids"])
            rows = self.conn.execute(
                "SELECT status, COUNT(*) FROM stories WHERE story_id IN (%s) GROUP BY 1"
                % ",".join("?" * len(ids)), ids).fetchall() if ids else []
            expected = {status: n for status, n in rows}
            self.assertEqual(data["total"], sum(expected.values()), state)
            for status in ("open", "dormant", "closed"):
                self.assertEqual(data[status], expected.get(status, 0), f"{state} {status}")
        unscoped = report_builder.read_story_threads()
        self.assertEqual(unscoped["total"],
                         unscoped["open"] + unscoped["dormant"] + unscoped["closed"],
                         "the federal §2.1 counts keep their status grouping")

    def test_reader_column_provenance_is_the_ledger_views(self):
        """The columns rendered are read from the ledger views, never derived."""
        source = (ENGINE_DIR / "report_builder.py").read_text(encoding="utf-8")
        for fragment in (
            "SELECT story_id, headline, status, theme, anchor, first_seen, last_update,",
            " event_count, latest_event_title FROM v_story_current",
            "FROM story_events se",
            "en.attrs_json",
            "entity_type = 'seat'",
            "ee.entity_id LIKE 'state:%'",
        ):
            self.assertIn(fragment, source)

    # ------------------------------------------------------------- rendering --
    def test_en_section_renders_heading_counts_and_ledger_rows(self):
        state_report_builder.LANG = "en"
        block = state_report_builder.sec7_story_threads("Perlis")
        self.assertIn("### 7.1 Political developments in this state", block)
        self.assertIn("**As of", block)
        self.assertIn("4 chains reference Perlis", block)
        self.assertIn(
            "| Chain of events | Items covered | First development | Last update | Status | "
            "Latest development |", block)
        self.assertNotIn(state_report_builder.NO_STATE_STORY_THREADS_EN, block)
        # The section is read by a political reader, not by the harness: none of
        # the machinery's own words may reach the page.
        for machinery in ("ledger", "schema", "events.db", "story_id", "anchor",
                          "query_events", "window", "vintage"):
            self.assertNotIn(machinery, block.lower(), machinery)

    def test_ms_section_renders_the_same_ledger_rows(self):
        state_report_builder.LANG = "ms"
        block = state_report_builder.sec7_story_threads("Perlis")
        self.assertIn("### 7.1 Perkembangan politik di negeri ini", block)
        self.assertIn("4 rantaian merujuk Perlis", block)
        self.assertIn(
            "| Rantaian peristiwa | Item diliputi | Perkembangan pertama | Kemas kini "
            "terakhir | Status | Perkembangan terakhir |", block)
        self.assertNotIn(state_report_builder.NO_STATE_STORY_THREADS_MS, block)

    def test_quiet_state_renders_the_zero_line_in_both_languages(self):
        state_report_builder.LANG = "en"
        en = state_report_builder.sec7_story_threads("Terengganu")
        self.assertIn(state_report_builder.NO_STATE_STORY_THREADS_EN, en)
        self.assertNotIn("| Chain of events |", en, "a quiet state gets no empty table")
        state_report_builder.LANG = "ms"
        ms = state_report_builder.sec7_story_threads("Terengganu")
        self.assertIn(state_report_builder.NO_STATE_STORY_THREADS_MS, ms)
        self.assertNotIn("| Rantaian peristiwa |", ms)

    def test_absent_ledger_states_the_fact_instead_of_a_thread_list(self):
        report_builder.EVENTS_DB = str(self.root / "absent.db")
        state_report_builder.LANG = "en"
        block = state_report_builder.sec7_story_threads("Perlis")
        self.assertIn("**No political developments are shown", block)
        self.assertNotIn("| Chain of events |", block)
        state_report_builder.LANG = "ms"
        self.assertIn("**Tiada perkembangan politik dipaparkan",
                      state_report_builder.sec7_story_threads("Perlis"))

    def test_section_is_deterministic(self):
        state_report_builder.LANG = "en"
        self.assertEqual(state_report_builder.sec7_story_threads("Perlis"),
                         state_report_builder.sec7_story_threads("Perlis"))

    # ---------------------------------------------------------------- wiring --
    def test_section_is_wired_after_the_signals_section_in_both_languages(self):
        source = (ENGINE_DIR / "state_report_builder.py").read_text(encoding="utf-8")
        self.assertIn(
            "    report += sec7_signals(state, meta, swings, fed, b)\n"
            "    report += sec7_story_threads(state)\n", source)
        self.assertIn('"sec7_story_threads": [],', source)
        self.assertIn('NO_STATE_STORY_THREADS_EN = ("No chain of related political events '
                      'touches this state in "', source)
        self.assertIn('NO_STATE_STORY_THREADS_MS = ("Tiada rantaian peristiwa politik berkaitan '
                      'menyentuh negeri ini "', source)
        self.assertIn("import report_builder", source,
                      "the section reads with the federal helper, never a second reader")


if __name__ == "__main__":
    unittest.main()
