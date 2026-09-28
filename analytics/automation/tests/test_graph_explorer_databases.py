"""Coverage for the four-database Graph Explorer viewer (T2 packet).

The explorer builder converts read-only inputs into browser bundles:
  family 1  work/events/ge16-events.db        -> events_data.js
  family 2  work/graph/ge16-knowledge-graph.json -> graph.json/graph_data.js
  family 3  work/figures/ge16-*-meta.json     -> *_vecs.js (7 stores)
  family 4  sibling 1_DATA research/trackers  -> trackers_data.js

These tests run the builder into a throwaway directory, so they never touch the
shipped app bundle, and they assert the live inputs are unchanged afterwards.
"""

import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BUILDER = REPOSITORY_ROOT / "tools" / "graph-explorer" / "build_data.py"
PAGE = REPOSITORY_ROOT / "tools" / "graph-explorer" / "index.html"
SHIM = REPOSITORY_ROOT / "GE16-Graph-Explorer" / "build_data.py"
APP_PAGE = REPOSITORY_ROOT / "GE16-Graph-Explorer" / "index.html"
EVENTS_DB = REPOSITORY_ROOT / "work" / "events" / "ge16-events.db"
TRACKERS = REPOSITORY_ROOT.parent / "1_DATA" / "research" / "trackers"
ACCEPTED = TRACKERS / "ge16-news-accepted.json"
FEED = TRACKERS / "ge16-news-feed.json"
POLLS = TRACKERS / "ge16-polls-tracked.json"
POLL_LOG = TRACKERS / "ge16-poll-tracker-log.md"
LIVE_INPUTS = (ACCEPTED, FEED, POLLS, POLL_LOG, EVENTS_DB)

# Every new view/table the packet requires the SPA to expose.
REQUIRED_PAGE_IDS = (
    "events-view",
    "ev-filters",
    "ev-table",
    "ev-rows",
    "ev-stories",
    "ev-story-rows",
    "ev-detail",
    "trackers-view",
    "tk-table",
    "tk-rows",
    "tk-polls",
    "tk-poll-rows",
    "tk-feed",
    "tk-blocs",
    "store-chooser",
    "vec-db",
    "graph-badges",
    "vec-store-badges",
)
REQUIRED_PAGE_FILES = ("events_data.js", "trackers_data.js")


def load_builder_module():
    spec = importlib.util.spec_from_file_location(
        "graph_explorer_databases_builder", BUILDER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_bundle(path, variable):
    """Parse a 'window.<VAR> = {...};' bundle written by the builder."""
    text = path.read_text(encoding="utf-8")
    prefix = "window.%s = " % variable
    if not text.startswith(prefix):
        raise AssertionError("%s does not start with %r" % (path, prefix))
    return json.loads(text[len(prefix):].rstrip().rstrip(";"))


def digest(path):
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def live_input_fingerprint():
    return {str(path): (path.stat().st_size, digest(path))
            for path in LIVE_INPUTS if path.exists()}


@unittest.skipUnless(BUILDER.exists(), "versioned explorer builder is missing")
class GraphExplorerDatabaseBundleTests(unittest.TestCase):
    def build(self, temp_dir, only):
        result = subprocess.run(
            [sys.executable, str(BUILDER), "--only", only, "--out", temp_dir],
            cwd=REPOSITORY_ROOT, capture_output=True, text=True,
        )
        self.assertEqual(
            0, result.returncode,
            "builder failed:\n%s\n%s" % (result.stdout, result.stderr))
        return result.stdout

    def test_events_bundle_carries_every_event_and_story(self):
        """Packet test (a): events_data.js has >=512 events and all 117 stories."""
        if not EVENTS_DB.exists():
            self.skipTest("events database is absent")
        with tempfile.TemporaryDirectory() as temp_dir:
            self.build(temp_dir, "events")
            bundle = load_bundle(Path(temp_dir) / "events_data.js", "EVENTS_DATA")
            connection = sqlite3.connect(EVENTS_DB.as_uri() + "?mode=ro", uri=True)
            try:
                db_events = connection.execute(
                    "SELECT COUNT(*) FROM events").fetchone()[0]
                db_stories = connection.execute(
                    "SELECT COUNT(*) FROM stories").fetchone()[0]
            finally:
                connection.close()
        counts = bundle["counts"]
        self.assertGreaterEqual(counts["events"], 512)
        self.assertEqual(db_events, counts["events_in_db"])
        self.assertEqual(db_stories, counts["stories"])
        self.assertEqual(db_stories, 117)
        self.assertEqual(len(bundle["stories"]), db_stories)
        self.assertEqual(counts["events"], len(bundle["events"]))
        self.assertGreater(counts["entities"], 0)
        self.assertGreater(counts["sources"], 0)

    def test_events_bundle_is_filter_ready_for_the_browser(self):
        """Every exported event carries the fields the JS tab filters on."""
        if not EVENTS_DB.exists():
            self.skipTest("events database is absent")
        with tempfile.TemporaryDirectory() as temp_dir:
            self.build(temp_dir, "events")
            bundle = load_bundle(Path(temp_dir) / "events_data.js", "EVENTS_DATA")
        for event in bundle["events"]:
            for field in ("id", "date", "date_precision", "event_type", "title",
                          "detail", "jurisdiction", "level", "significance",
                          "dossier", "entities", "blocs", "stories"):
                self.assertIn(field, event)
            self.assertIn(event["level"], ("federal", "state", "other"))
            self.assertIsInstance(event["blocs"], list)
        facets = bundle["facets"]
        for facet in ("levels", "states", "categories", "blocs", "significance",
                      "dossiers", "date_min", "date_max"):
            self.assertIn(facet, facets)
        # facet counts cover the whole slice, so nothing is silently dropped
        self.assertEqual(
            sum(entry["count"] for entry in facets["levels"]),
            len(bundle["events"]))
        self.assertEqual(
            sum(len(story["event_ids"]) for story in bundle["stories"]),
            len(bundle["events"]))

    def test_trackers_bundle_matches_the_live_tracker_counts(self):
        """Packet test (b): exported accepted-news count == live tracker count."""
        if not ACCEPTED.exists():
            self.skipTest("tracker files are absent")
        live = json.loads(ACCEPTED.read_text(encoding="utf-8"))
        live_feed = json.loads(FEED.read_text(encoding="utf-8"))
        live_polls = json.loads(POLLS.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temp_dir:
            self.build(temp_dir, "trackers")
            bundle = load_bundle(Path(temp_dir) / "trackers_data.js", "TRACKERS_DATA")
        counts = bundle["counts"]
        self.assertEqual(live["count"], counts["accepted_declared"])
        self.assertEqual(len(live["items"]), counts["accepted_in_tracker"])
        self.assertEqual(counts["accepted"], counts["accepted_in_tracker"])
        self.assertEqual(counts["accepted_dropped_by_cap"], 0)
        self.assertEqual(len(bundle["accepted"]), counts["accepted"])
        self.assertEqual(len(bundle["feed"]), counts["feed"])
        self.assertEqual(counts["feed"], len(live_feed["items"]))
        self.assertEqual(len(bundle["polls"]), counts["polls"])
        self.assertEqual(len(live_polls["seen"]), counts["polls"])
        # poll rows carry a pollster and a scan date joined from the markdown log
        with_dates = [row for row in bundle["polls"] if row["scan"]]
        self.assertEqual(len(bundle["polls"]), len(with_dates))
        for row in bundle["polls"]:
            self.assertTrue(row["pollster"])
            self.assertTrue(row["title"])
        self.assertEqual(len(bundle["poll_scans"]),
                         counts["poll_scans"])
        self.assertGreaterEqual(counts["poll_scans"], 1)

    def test_accepted_rows_expose_score_language_and_link(self):
        """Trackers tab columns: judged score, lang, link."""
        if not ACCEPTED.exists():
            self.skipTest("tracker files are absent")
        with tempfile.TemporaryDirectory() as temp_dir:
            self.build(temp_dir, "trackers")
            bundle = load_bundle(Path(temp_dir) / "trackers_data.js", "TRACKERS_DATA")
        for row in bundle["accepted"][:200]:
            for field in ("title", "date", "source", "link", "lang", "category",
                          "blocs", "score"):
                self.assertIn(field, row)
            self.assertIn(row["lang"], ("en", "ms", ""))
            self.assertTrue(isinstance(row["score"], (int, float)) or row["score"] is None)
        for row in bundle["accepted"]:
            self.assertTrue(row["title"])
            self.assertTrue(row["source"])
        for facet in ("sources", "categories", "blocs", "langs", "months"):
            self.assertTrue(bundle["facets"][facet])

    def test_only_selector_builds_one_family_and_skips_the_rest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            self.build(temp_dir, "events")
            written = {path.name for path in Path(temp_dir).iterdir()}
        self.assertIn("events_data.js", written)
        self.assertNotIn("trackers_data.js", written)
        self.assertNotIn("graph_data.js", written)
        self.assertNotIn("personnel_vecs.js", written)

    def test_builder_rejects_an_unknown_section(self):
        result = subprocess.run(
            [sys.executable, str(BUILDER), "--only", "events,telemetry"],
            cwd=REPOSITORY_ROOT, capture_output=True, text=True,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("unknown section", result.stdout + result.stderr)

    def test_build_never_mutates_the_live_inputs(self):
        """Packet test (c): live tracker/DB files are byte-identical after a build."""
        if not LIVE_INPUTS[4].exists() and not ACCEPTED.exists():
            self.skipTest("live inputs are absent")
        before = live_input_fingerprint()
        self.assertTrue(before, "expected at least one live input to hash")
        with tempfile.TemporaryDirectory() as temp_dir:
            self.build(temp_dir, "events,trackers")
        self.assertEqual(before, live_input_fingerprint())

    def test_events_connection_is_read_only(self):
        """The DB must be opened with a mode=ro URI, and writes must fail."""
        source = BUILDER.read_text(encoding="utf-8")
        self.assertIn("query_only", source)
        self.assertIn("?mode=ro", source)
        if not EVENTS_DB.exists():
            self.skipTest("events database is absent")
        connection = load_builder_module()._readonly_connection(str(EVENTS_DB))
        try:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("CREATE TABLE should_not_exist (id TEXT)")
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM events")
        finally:
            connection.close()

    def test_events_levels_split_federal_and_state(self):
        if not EVENTS_DB.exists():
            self.skipTest("events database is absent")
        with tempfile.TemporaryDirectory() as temp_dir:
            self.build(temp_dir, "events")
            bundle = load_bundle(Path(temp_dir) / "events_data.js", "EVENTS_DATA")
        levels = {row["value"]: row["count"] for row in bundle["facets"]["levels"]}
        self.assertGreater(levels.get("federal", 0), 0)
        self.assertGreater(levels.get("state", 0), 0)
        for event in bundle["events"]:
            if event["jurisdiction"] in ("federal", "national"):
                self.assertEqual("federal", event["level"])
            elif event["jurisdiction"].startswith("state:"):
                self.assertEqual("state", event["level"])


@unittest.skipUnless(PAGE.exists(), "explorer page is missing")
class GraphExplorerPageContractTests(unittest.TestCase):
    def test_page_loads_every_new_view_table_and_bundle(self):
        """Packet test (d): the SPA declares all new view/table ids + bundles."""
        source = PAGE.read_text(encoding="utf-8")
        for element_id in REQUIRED_PAGE_IDS:
            self.assertIn('id="%s"' % element_id, source, element_id)
        for filename in REQUIRED_PAGE_FILES:
            self.assertIn('<script src="%s"></script>' % filename, source, filename)
        # the four tabs must exist and still include the original two
        for view in ("graph", "vector", "events", "trackers"):
            self.assertIn('data-view="%s"' % view, source, view)

    def test_page_never_opens_the_database_or_fetches_trackers_directly(self):
        """The browser consumes generated bundles only (file:// friendly)."""
        source = PAGE.read_text(encoding="utf-8")
        self.assertNotIn("sqlite", source.lower())
        self.assertNotIn("events.db", source)
        self.assertIn("window.EVENTS_DATA", source)
        self.assertIn("window.TRACKERS_DATA", source)
        self.assertIn("window.GRAPH_DATA", source)

    def test_graph_tab_badges_come_from_the_loaded_bundle(self):
        source = PAGE.read_text(encoding="utf-8")
        self.assertIn("function setGraphBadges(", source)
        self.assertIn("file_bytes", source)
        self.assertIn("graph-badges", source)

    def test_deep_links_select_every_tab_at_load(self):
        """#graph/#vector/#events/#trackers must select the tab without a click."""
        source = PAGE.read_text(encoding="utf-8")
        self.assertIn("location.hash", source)
        self.assertIn(".tab[data-view=\"' + want + '\"]", source)

    def test_store_chooser_lists_all_seven_vector_stores(self):
        source = PAGE.read_text(encoding="utf-8")
        self.assertIn(
            "var STORE_ORDER=['personnel','figures','parties','seats','news',"
            "'scenarios','clusters'];", source)
        self.assertIn("function fillStoreChooser(", source)
        self.assertIn("function markStore(", source)
        # the matcher must report the store that produced the hits
        self.assertIn("markStore(effectiveDb)", source)

    def test_app_copy_and_versioned_page_stay_identical(self):
        if not APP_PAGE.exists():
            self.skipTest("in-place app copy is absent")
        self.assertEqual(
            APP_PAGE.read_text(encoding="utf-8"),
            PAGE.read_text(encoding="utf-8"),
        )

    def test_app_shim_delegates_to_the_versioned_builder(self):
        """The in-place app app must not fork the builder logic."""
        if not SHIM.exists():
            self.skipTest("in-place app shim is absent")
        source = SHIM.read_text(encoding="utf-8")
        self.assertIn("tools", source)
        self.assertIn("graph-explorer", source)
        self.assertNotIn("def build_events(", source)
        self.assertNotIn("def build_graph(", source)


class ShippedBundleTests(unittest.TestCase):
    """The bundles the app serves today (skip when the app dir is absent)."""

    def bundle(self, name, variable):
        path = REPOSITORY_ROOT / "GE16-Graph-Explorer" / name
        if not path.exists():
            self.skipTest("%s has not been generated" % name)
        return load_bundle(path, variable)

    def test_shipped_events_bundle_is_current(self):
        bundle = self.bundle("events_data.js", "EVENTS_DATA")
        self.assertGreaterEqual(bundle["counts"]["events"], 512)
        self.assertEqual(117, bundle["counts"]["stories"])
        if EVENTS_DB.exists():
            self.assertEqual(EVENTS_DB.stat().st_size, bundle["source"]["bytes"])

    def test_shipped_trackers_bundle_is_current(self):
        bundle = self.bundle("trackers_data.js", "TRACKERS_DATA")
        if ACCEPTED.exists():
            live = json.loads(ACCEPTED.read_text(encoding="utf-8"))
            self.assertEqual(live["count"], bundle["counts"]["accepted"])
        self.assertEqual(bundle["counts"]["accepted"], len(bundle["accepted"]))
        self.assertTrue(bundle["polls"])
        self.assertTrue(bundle["feed"])

    def test_shipped_graph_bundle_reports_sizes_for_the_badges(self):
        path = REPOSITORY_ROOT / "GE16-Graph-Explorer" / "graph_data.js"
        if not path.exists():
            self.skipTest("graph_data.js has not been generated")
        bundle = load_bundle(path, "GRAPH_DATA")
        self.assertIn("file_bytes", bundle)
        self.assertGreater(bundle["file_bytes"], 0)
        self.assertEqual(bundle["node_count"], len(bundle["nodes"]))
        self.assertEqual(bundle["edge_count"], len(bundle["edges"]))


if __name__ == "__main__":
    unittest.main()
