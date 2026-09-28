"""Contract tests for the GE16 relational events database (builder + FTS5 layer).

Hermetic: builds a throwaway database from inline fixtures in a temp directory, so
the tests never touch work/events or the sweep dossiers. The entity index is built
from whatever repository stores are present (knowledge graph, figure vectors,
canonical 1_DATA); missing optional inputs only produce warnings.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.events import build_events_db as builder  # noqa: E402
from tools.events import query_events as query  # noqa: E402


FIXTURE_PART_A = """# GE16 Sweep — PART A (fixture)

**Window:** 1 January 2026 → 24 September 2026

---

## 0. Executive summary — what changes vs the baseline brief

| Baseline assumption | Verified reality | Confidence |
|---|---|---|
| Kinabatangan turnout 55.56% | **55.06%** (26,827 / 48,722) | High |

---

## 1. Parliamentary seat vacancies

### 1.1 P104 Subang — resignation followed by a party switch

- **Wong Chen** (PKR, three-term MP for Subang) handed his resignation letter to Speaker Johari Abdul
  on **5 Aug 2026**. He cited loss of access to constituency funds.
  - https://www.malaymail.com/news/malaysia/2026/08/05/wong-chen-resigns-subang/230854
- Seat declared vacant; the **Speaker notified the EC on 11 Aug 2026**.
  - https://www.thestar.com.my/news/nation/2026/08/11/speaker-notifies-ec-of-subang-seat-vacancy

### 1.7 All 2026 by-elections actually held (national, complete)

| Contest | Date | Winner | Majority | Turnout |
|---|---|---|---|---|
| P.187 Kinabatangan | 24 Jan 2026 | BN/UMNO (Naim Moktar) | 14,214 (53.8%) | 55.06% |
Source: https://electiondata.my/byelections

---

## 9. Corrections to the baseline brief (explicit)

1. **Kinabatangan turnout was 55.06%**, not 55.56%.

## 10. Unknowns / could not verify

- Whether the Speaker's ruling will be challenged in court by the second half of 2026.
"""

FIXTURE_PART_B = {
    "generated": "2026-09-24",
    "window": {"from": "2026-01-01", "to": "2026-09-24"},
    "states": {
        "perlis": {
            "state": "Perlis",
            "dun_seats": 15,
            "last_election": "PRN-15, 2022",
            "vacancies": [{
                "seat": "N.03 Chuping",
                "former_rep": "Saad Seman (PAS)",
                "since": "2025-12-25",
                "detail": "Seat declared vacant after PAS sacked him on 24-25 Dec 2025.",
                "source": {"url": "https://www.thestar.com.my/news/nation/2025/12/25/pas-sacks-three-perlis-assemblymen",
                           "date": "2025-12-25", "title": "PAS sacks three Perlis assemblymen"},
            }],
            "byelections": [{
                "seat": "N.03 Chuping",
                "status": "NOT HELD - cancelled/not required",
                "date_expected": "Jan 2026",
                "detail": "The EC confirmed on 1 Jan 2026 that no by-elections would be held for the three seats.",
                "source": {"url": "https://www.channelnewsasia.com/asia/malaysia-perlis-5713526",
                           "date": "2026-01-01", "title": "EC confirms no by-elections for three vacant Perlis seats"},
            }],
            "composition_change": [],
            "mb_changes": [],
            "polls": [],
            "notes": "Fixture note: three Perlis seats stayed empty through the window.",
            "corrections_to_baseline": ["Baseline said two Perlis seats were vacant; three were."],
        }
    },
    "sabah_prn_2026": {
        "headline": "Fixture headline",
        "election": "17th Sabah state election (PRN-17)",
        "polling_day": "2025-11-29",
        "result_official": {"WARISAN": 25, "GRS": 29, "elected_total": 73},
        "government_formation": {
            "date": "2025-11-30",
            "chief_minister": "Hajiji Noor (GRS)",
            "detail": "Sworn in for a second term.",
            "source": {"url": "https://www.bernama.com/en/news.php?id=2497113", "date": "2025-11-30",
                       "title": "Hajiji Secures Second Term As Chief Minister In Sabah"},
        },
        "cm_rotation": "NONE. No rotation arrangement exists.",
        "in_window_changes": [{
            "date": "2026-01-24",
            "event": "Lamag (N.58) by-election — BN retained.",
            "detail": "Triggered by the death of Bung Moktar Radin (5 Dec 2025).",
            "source": {"url": "https://en.wikipedia.org/wiki/2026_Lamag_by-election",
                       "date": "2026-01-24", "title": "2026 Lamag by-election"},
        }],
        "current_composition": {"GRS": 29},
        "corrections": ["The brief's 'Sabah after Aug 2026 PRN' is wrong."],
    },
}

FIXTURE_PART_C = {
    "meta": {"sweep": "fixture", "compiled": "2026-09-24"},
    "national_polls": [{
        "pollster": "Merdeka Center",
        "title": "Fixture national survey",
        "fieldwork": "2026-03-12 to 2026-04-09",
        "published": "2026-06-25",
        "n": 1209,
        "type": "leadership_approval_and_direction",
        "results": {"anwar_approval_overall_pct": 52, "federal_govt_satisfied_pct": 50},
        "source": ["https://merdeka.org/fixture-survey/"],
    }],
    "approval": [{
        "month": "2026-05", "pollster": "Merdeka Center", "anwar_pct": 55,
        "govt_pct": None, "right_direction_pct": 43, "wrong_direction_pct": 50,
        "source": ["https://merdeka.org/fixture-approval/"],
    }],
    "dosm": [{
        "indicator": "GDP yoy", "period": "Q2 2026", "value": 6.0, "unit": "pct",
        "source": ["https://www.bnm.gov.my/fixture-gdp"], "note": "Q2 2026 actual.",
    }, {
        "indicator": "growth range", "period": "2026", "value": "4.0-5.0 (around 5.0)",
        "unit": "pct", "source": [], "note": "Range print must not crash the metric loader.",
    }],
    "bnm_opr": [{"date": "2026-05-07", "opr_pct": 2.75, "decision": "hold",
                 "source": ["https://www.bnm.gov.my/monetary-stability"]}],
    "ringgit": [{"date": "2026-01-27", "myr_usd": 3.954, "note": "strongest since 2018",
                 "source": ["https://www.malaymail.com/news/money/2026/01/27/fixture"]}],
    "corrections": [{
        "field": "gdp_yoy", "engine_value": 5.8, "issue": "STALE/WRONG",
        "verified_latest": "Q2 2026 actual = 6.0%", "explanation": "5.8 was only the advance estimate.",
        "source": ["https://theedgemalaysia.com/node/815232"],
    }],
    "unknowns": ["No national poll published in the fixture window."],
    "bnm_opr_note": "OPR held throughout the window.",
}


class EventsDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        dossiers = root / "01_RESEARCH" / "events" / "_draft"
        dossiers.mkdir(parents=True)
        (dossiers / "ge16-sweep-partA-federal.md").write_text(FIXTURE_PART_A, encoding="utf-8")
        (dossiers / "ge16-sweep-partB-states.json").write_text(json.dumps(FIXTURE_PART_B), encoding="utf-8")
        (dossiers / "ge16-sweep-partC-polls-macro.json").write_text(json.dumps(FIXTURE_PART_C), encoding="utf-8")
        cls.db_path = root / "work" / "events" / "ge16-events.db"
        cls.report = builder.build(
            builder.discover_dossiers(dossiers), [], cls.db_path,
            repo_root=REPOSITORY_ROOT, use_drafts=False, quiet=True,
        )
        cls.conn = sqlite3.connect(cls.db_path)
        cls.conn.row_factory = sqlite3.Row

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls._tmp.cleanup()

    # ------------------------------------------------------------- the build --
    def test_build_reports_all_checks_passing(self):
        self.assertTrue(self.report["ok"], self.report["checks"])
        for check in self.report["checks"]:
            self.assertTrue(check["ok"], check)

    def test_provenance_run_row_records_inputs_and_hash(self):
        row = self.conn.execute(
            "SELECT built_at, builder_sha256, schema_version, source_window, counts_json, inputs_json, ok"
            "  FROM ingest_runs ORDER BY run_id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["ok"], 1)
        self.assertEqual(row["schema_version"], builder.SCHEMA_VERSION)
        inputs = json.loads(row["inputs_json"])
        self.assertEqual(len(inputs), 3)
        for entry in inputs.values():
            self.assertEqual(len(entry["sha256"]), 64)
        self.assertGreaterEqual(json.loads(row["counts_json"])["events"], 8)

    def test_fixture_facts_land_as_typed_dated_events(self):
        wong = self.conn.execute(
            "SELECT event_date, event_type, seat_code, jurisdiction FROM events"
            " WHERE title LIKE 'Wong Chen%' AND event_type = 'resignation'").fetchone()
        self.assertIsNotNone(wong, "resignation event for Wong Chen is missing")
        self.assertEqual(wong["event_date"], "2026-08-05")
        self.assertEqual(wong["jurisdiction"], "federal")

        by_election = self.conn.execute(
            "SELECT event_date, event_type, seat_code FROM events"
            " WHERE title LIKE '%Kinabatangan by-election%'").fetchone()
        self.assertIsNotNone(by_election, "by-election table row is missing")
        self.assertEqual(by_election["event_type"], "by_election")
        self.assertEqual(by_election["event_date"], "2026-01-24")
        self.assertEqual(by_election["seat_code"], "P187")

        vacancy = self.conn.execute(
            "SELECT event_date, event_type, seat_code FROM events WHERE title LIKE 'N.03 Chuping declared vacant%'"
        ).fetchone()
        self.assertIsNotNone(vacancy, "Perlis vacancy is missing")
        self.assertEqual(vacancy["event_type"], "vacancy")
        self.assertEqual(vacancy["seat_code"], "N.03")

    def test_metrics_are_stored_numerically(self):
        gdp = self.conn.execute(
            "SELECT value_num, value_text, unit FROM event_metrics WHERE metric = 'GDP yoy'").fetchone()
        self.assertIsNotNone(gdp)
        self.assertAlmostEqual(gdp["value_num"], 6.0)
        self.assertEqual(gdp["unit"], "pct")
        # the text range print still yields a number rather than crashing the loader
        ranged = self.conn.execute(
            "SELECT value_num FROM event_metrics WHERE metric = 'growth range'").fetchone()
        self.assertAlmostEqual(ranged["value_num"], 4.0)
        approval = self.conn.execute(
            "SELECT value_num FROM event_metrics WHERE metric = 'anwar_approval_overall_pct'").fetchone()
        self.assertAlmostEqual(approval["value_num"], 52.0)

    def test_entities_roles_and_kg_join(self):
        mention = self.conn.execute(
            "SELECT ee.role, en.entity_type, en.kg_node_id FROM event_entities ee"
            "  JOIN entities en ON en.entity_id = ee.entity_id"
            "  JOIN events e ON e.event_id = ee.event_id"
            " WHERE en.name = 'Wong Chen' AND e.title LIKE 'Wong Chen%'").fetchone()
        self.assertIsNotNone(mention, "Wong Chen is not linked to his resignation event")
        self.assertEqual(mention["role"], "actor")
        self.assertEqual(mention["entity_type"], "person")
        self.assertEqual(mention["kg_node_id"], "person:wong chen")
        seat_link = self.conn.execute(
            "SELECT COUNT(*) FROM event_entities WHERE role = 'seat' AND entity_id = 'seat:P187'").fetchone()[0]
        self.assertGreaterEqual(seat_link, 1, "P187 was not linked as a seat entity")

    def test_event_links_chain_the_by_election_to_its_trigger(self):
        link = self.conn.execute(
            "SELECT l.relation, l.basis, f.event_type AS from_type, t.event_type AS to_type"
            "  FROM event_links l JOIN events f ON f.event_id = l.from_event_id"
            "  JOIN events t ON t.event_id = l.to_event_id"
            " WHERE l.relation = 'follows' AND t.event_type = 'by_election'").fetchone()
        self.assertIsNotNone(link, "no by-election 'follows' link was derived")
        self.assertIn(link["from_type"], {"vacancy", "resignation", "death"})

    def test_claims_cover_corrections_and_unknowns(self):
        kinds = {row["kind"] for row in self.conn.execute("SELECT DISTINCT kind FROM claim_reviews")}
        self.assertIn("correction", kinds)
        self.assertIn("unknown", kinds)
        verified = self.conn.execute(
            "SELECT COUNT(*) FROM claim_reviews WHERE kind = 'correction' AND verdict = 'corrected'").fetchone()[0]
        self.assertGreaterEqual(verified, 2)

    def test_notes_are_kept_out_of_the_event_stream(self):
        notes = self.conn.execute("SELECT COUNT(*) FROM dossier_notes").fetchone()[0]
        self.assertGreaterEqual(notes, 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = 'note'").fetchone()[0], 0)

    # ------------------------------------------------------- the FTS5 layer ---
    def test_full_text_index_matches_event_count(self):
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events_fts").fetchone()[0],
                         self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM entities_fts").fetchone()[0],
                         self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0])

    def test_search_returns_events_with_snippets(self):
        for term in ("resignation", "by-election", "deregistration OR vacant"):
            rows = self.conn.execute(
                "SELECT e.event_id, snippet(events_fts, 1, '[', ']', ' … ', 8) AS s"
                "  FROM events_fts JOIN events e ON e.event_id = events_fts.event_id"
                " WHERE events_fts MATCH ? LIMIT 20", (query.sanitize_fts_query(term),)).fetchall()
            self.assertGreaterEqual(len(rows), 1, f"no FTS match for {term!r}")
            self.assertTrue(all(row["s"] for row in rows))

    def test_search_accepts_punctuated_queries(self):
        self.assertEqual(query.sanitize_fts_query("show-cause notice"), '"show-cause" notice')
        self.assertEqual(query.sanitize_fts_query('"exact phrase"'), '"exact phrase"')
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM events_fts WHERE events_fts MATCH ?",
            (query.sanitize_fts_query("show-cause"),)).fetchone()[0]
        self.assertEqual(rows, 0)          # fixture has no show-cause notice: query must not error
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM events_fts WHERE events_fts MATCH ?",
            (query.sanitize_fts_query("by-election"),)).fetchone()[0]
        self.assertGreaterEqual(rows, 1)

    # ----------------------------------------------------------- integrity ----
    def test_virtual_tables_and_views_are_queryable(self):
        for view in ("v_event_full", "v_timeline", "v_entity_activity", "v_open_claims",
                     "v_metric_series", "v_corroboration"):
            self.conn.execute(f"SELECT * FROM {view} LIMIT 1").fetchall()
        row = self.conn.execute(
            "SELECT * FROM v_event_full WHERE title LIKE '%Kinabatangan by-election%' LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["event_type"], "by_election")
        self.assertTrue(row["actors"], "v_event_full dropped the actor list")

    def test_foreign_keys_and_external_content_integrity(self):
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        orphans = self.conn.execute(
            "SELECT COUNT(*) FROM event_entities ee LEFT JOIN events e ON e.event_id = ee.event_id"
            " WHERE e.event_id IS NULL").fetchone()[0]
        self.assertEqual(orphans, 0)

    def test_rebuild_is_deterministic(self):
        second = self.db_path.with_name("ge16-events-second.db")
        report = builder.build(builder.discover_dossiers(self.db_path.parent.parent.parent /
                                                         "01_RESEARCH" / "events" / "_draft"),
                               [], second, repo_root=REPOSITORY_ROOT, use_drafts=False, quiet=True)
        self.assertTrue(report["ok"])
        first_ids = {row[0] for row in self.conn.execute("SELECT event_id FROM events")}
        other = sqlite3.connect(second)
        try:
            second_ids = {row[0] for row in other.execute("SELECT event_id FROM events")}
        finally:
            other.close()
        self.assertEqual(first_ids, second_ids)

    # ------------------------------------------------------------- the CLI ----
    def test_query_cli_reads_the_database(self):
        result = subprocess.run(
            [sys.executable, str(REPOSITORY_ROOT / "tools" / "events" / "query_events.py"),
             "--db", str(self.db_path), "stats"],
            capture_output=True, text=True, cwd=REPOSITORY_ROOT, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("events:", result.stdout)
        search = subprocess.run(
            [sys.executable, str(REPOSITORY_ROOT / "tools" / "events" / "query_events.py"),
             "--db", str(self.db_path), "search", "by-election", "--limit", "3"],
            capture_output=True, text=True, cwd=REPOSITORY_ROOT, timeout=120)
        self.assertEqual(search.returncode, 0, search.stderr)
        self.assertIn("match(es)", search.stdout)

    def test_query_cli_refuses_writes(self):
        result = subprocess.run(
            [sys.executable, str(REPOSITORY_ROOT / "tools" / "events" / "query_events.py"),
             "--db", str(self.db_path), "sql", "DELETE FROM events"],
            capture_output=True, text=True, cwd=REPOSITORY_ROOT, timeout=120)
        self.assertEqual(result.returncode, 2)
        self.assertIn("read-only", result.stderr)


if __name__ == "__main__":
    unittest.main()
