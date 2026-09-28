"""Self-healing Stage-1 news pipeline guards (v5 owner directive).

Covers, with the network stubbed (no real HTTP):
  * collect() never advances the persistent seen file before a commit succeeds;
  * a new collect re-presents previously collected but uncommitted candidates;
  * --judge-input batched judgment, and partial judgment refusing to commit;
  * complete batch commit merges everything and clears the tombstone;
  * the 200-cap overflow is reported and carried instead of burned;
  * an explicit zero-acceptance {"accepted": []} cycle stays legal;
  * the self-heal state records design vs operational failures and prints the
    banner the live cron agent reads on the next run.

Run from the repository root (imports resolve by path, not by package):
    python3 -m pytest 1_DATA/tests/test_news_selfheal.py -q
"""

import contextlib
import importlib.util
import io
import json
import pathlib
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
COLLECTOR_ROOT = REPOSITORY_ROOT / "scripts" / "collect"
TRACKER_ROOT = REPOSITORY_ROOT / "canonical" / "research" / "trackers"
if str(COLLECTOR_ROOT) not in sys.path:
    sys.path.insert(0, str(COLLECTOR_ROOT))

import ge16_selfheal_state as SELFHEAL  # noqa: E402  (same object the tracker imports)


def load_module(filename):
    path = COLLECTOR_ROOT / filename
    spec = importlib.util.spec_from_file_location("ge16_fixture_" + path.stem, path)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load " + str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NEWS = load_module("track_ge16_news.py")

RSS_HEAD = "<rss><channel>"
RSS_TAIL = "</channel></rss>"


class NewsSelfHealTestCase(unittest.TestCase):
    """Fixture: isolated tracker directory + stubbed fetch, real module code."""

    def setUp(self):
        self.workspace = pathlib.Path(tempfile.mkdtemp(prefix="ge16-news-selfheal-"))
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.trackers = self.workspace / "research" / "trackers"
        self.trackers.mkdir(parents=True)

        self.patch(
            NEWS,
            DIR=str(self.trackers),
            LOG=str(self.trackers / "ge16-general-news-log.md"),
            DB=str(self.trackers / "ge16-general-news-tracked.json"),
            CANDIDATES=str(self.trackers / "ge16-news-candidates.json"),
            JUDGED=str(self.trackers / "ge16-news-judged.json"),
            ACCEPTED=str(self.trackers / "ge16-news-accepted.json"),
            FEED=str(self.trackers / "ge16-news-feed.json"),
            SEEN_PENDING=str(self.trackers / "ge16-news-seen-pending.json"),
            JUDGE_MANIFEST=str(self.trackers / "ge16-news-judge-manifest.json"),
            JUDGE_BATCH_FMT=str(self.trackers / "ge16-news-judge-batch-%d.json"),
            JUDGED_BATCH_FMT=str(self.trackers / "ge16-news-judged-batch-%d.json"),
            JUDGED_BATCH_GLOB=str(self.trackers / "ge16-news-judged-batch-*.json"),
            CANDIDATE_CAP=200,
            JUDGE_BATCH=50,
            DIRECT_FEEDS=[("Fixture Feed", "https://fixture.invalid/feed")],
            QUERIES=[],
            fetch=self.fixture_fetch,
        )
        self.patch(SELFHEAL, STATE=str(self.trackers / "ge16-selfheal-state.json"))
        self.feed_titles = []

    # ---- fixture helpers -------------------------------------------------

    def patch(self, module, **values):
        for name, value in values.items():
            original = getattr(module, name)
            setattr(module, name, value)
            self.addCleanup(setattr, module, name, original)

    def fixture_fetch(self, url):
        """Stubbed network: one RSS item per queued title, dated now."""
        stamp = format_datetime(datetime.now(timezone.utc))
        items = "".join(
            f"<item><title>{title}</title><link>https://fixture.invalid/{index}</link>"
            f"<pubDate>{stamp}</pubDate><description>Fixture description</description></item>"
            for index, title in enumerate(self.feed_titles)
        )
        return RSS_HEAD + items + RSS_TAIL

    def serve(self, titles):
        self.feed_titles = list(titles)

    def write_json(self, path, payload):
        pathlib.Path(path).write_text(json.dumps(payload), encoding="utf-8")

    def read_json(self, path):
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))

    def read_text(self, path):
        return pathlib.Path(path).read_text(encoding="utf-8")

    def exists(self, path):
        return pathlib.Path(path).exists()

    def run_cli(self, *argv):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = NEWS.main(list(argv))
        return code, stream.getvalue()

    def candidate_titles(self):
        payload = self.read_json(NEWS.CANDIDATES)
        return [item["title"] for item in payload["items"]]

    # ---- (a) seen is not advanced by collect ------------------------------

    def test_collect_does_not_advance_persistent_seen_until_commit_succeeds(self):
        self.write_json(NEWS.DB, {"seen": ["a story judged in an earlier cycle"]})
        before = self.read_text(NEWS.DB)
        self.serve(["Fresh story one", "Fresh story two"])

        code, output = self.run_cli()
        self.assertEqual(0, code, output)

        # The seen history is untouched: nothing was judged yet.
        self.assertEqual(before, self.read_text(NEWS.DB))
        pending = self.read_json(NEWS.SEEN_PENDING)
        self.assertEqual(PENDING_SCHEMA_KEYS, set(pending) & PENDING_SCHEMA_KEYS)
        self.assertEqual({"Fresh story one", "Fresh story two"}, set(pending["pending"]))

        payload = self.read_json(NEWS.CANDIDATES)
        self.assertEqual(2, payload["count"])
        self.assertTrue(payload["collection_id"])
        for item in payload["items"]:
            self.assertEqual(payload["collection_id"], item["collection_id"])

        # A successful commit is what moves the keys into the seen history.
        self.write_json(NEWS.JUDGED, {"accepted": [
            {"title": "Fresh story one", "date": self.now_iso(), "source": "Fixture Feed",
             "link": "https://fixture.invalid/0", "category": "election", "score": 0.9}]})
        code, output = self.run_cli("--commit")
        self.assertEqual(0, code, output)
        seen = set(self.read_json(NEWS.DB)["seen"])
        self.assertIn("a story judged in an earlier cycle", seen)
        self.assertIn("Fresh story one", seen)
        self.assertIn("Fresh story two", seen)
        self.assertEqual({}, self.read_json(NEWS.SEEN_PENDING)["pending"])

    def now_iso(self):
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    # ---- (b) uncommitted candidates are re-presented ----------------------

    def test_collect_replays_previously_collected_but_unjudged_candidates(self):
        carried = {"title": "Carried story from the failed cycle", "desc": "old",
                   "link": "https://fixture.invalid/carried", "date": self.now_iso(),
                   "source": "Fixture Feed", "query": "feed:Fixture Feed",
                   "found_at": self.now_iso()}
        self.write_json(NEWS.SEEN_PENDING, {
            "schema": "ge16.news-seen-pending.v1", "collection_id": "previous-cycle",
            "count": 1, "pending": {"Carried story from the failed cycle": {
                "collection_id": "previous-cycle", "item": carried}}})
        self.serve(["Brand new story"])

        code, output = self.run_cli()
        self.assertEqual(0, code, output)
        payload = self.read_json(NEWS.CANDIDATES)
        titles = [item["title"] for item in payload["items"]]
        self.assertEqual(["Carried story from the failed cycle", "Brand new story"], titles)
        self.assertEqual(1, payload["replayed_unjudged"])
        self.assertTrue(payload["items"][0]["replayed_unjudged"])
        # Both are tombstoned again, so the carried item is not lost either way.
        self.assertEqual({"Carried story from the failed cycle", "Brand new story"},
                         set(self.read_json(NEWS.SEEN_PENDING)["pending"]))

    def test_replayed_candidates_that_left_the_window_are_dropped_from_the_tombstone(self):
        stale = {"title": "Ancient story", "date": "2001-01-01T00:00:00+00:00",
                 "source": "Fixture Feed", "link": "https://fixture.invalid/ancient"}
        self.write_json(NEWS.SEEN_PENDING, {
            "count": 1, "pending": {"Ancient story": {"collection_id": "old", "item": stale}}})
        self.serve(["Fresh story"])

        code, _ = self.run_cli()
        self.assertEqual(0, code)
        pending = self.read_json(NEWS.SEEN_PENDING)["pending"]
        self.assertNotIn("Ancient story", pending)
        self.assertEqual(0, self.read_json(NEWS.CANDIDATES)["replayed_unjudged"])

    # ---- (c) chunked judgment, partial judgment refusal -------------------

    def test_judge_input_batches_candidates_and_partial_commit_refuses(self):
        self.serve([f"Story number {index} in the political feed" for index in range(120)])
        self.patch(NEWS, JUDGE_BATCH=50)
        code, output = self.run_cli()
        self.assertEqual(0, code, output)

        code, output = self.run_cli("--judge-input")
        self.assertEqual(0, code, output)
        manifest = self.read_json(NEWS.JUDGE_MANIFEST)
        self.assertEqual(3, manifest["batch_count"])
        self.assertEqual(120, manifest["total"])
        self.assertEqual([50, 50, 20], [batch["count"] for batch in manifest["batches"]])
        for number in (1, 2, 3):
            self.assertTrue(self.exists(NEWS.JUDGE_BATCH_FMT % number))
        self.assertEqual(50, len(self.read_json(NEWS.JUDGE_BATCH_FMT % 1)["items"]))

        # Only the first batch is judged: committing must fail loud, and write
        # nothing at all (no accepted history, no feed, no seen, no tombstone drop).
        self.write_json(NEWS.JUDGED_BATCH_FMT % 1, {"accepted": [
            {"title": "Story number 0 in the political feed", "date": self.now_iso(),
             "source": "Fixture Feed", "category": "election", "score": 0.8}]})
        accepted_before = self.exists(NEWS.ACCEPTED)
        seen_before = self.exists(NEWS.DB)
        pending_before = self.read_json(NEWS.SEEN_PENDING)

        code, output = self.run_cli("--commit")
        self.assertEqual(1, code, output)
        self.assertIn("partial judgment: 1/3 batches judged", output)
        self.assertIn("rerun judgment for missing batches", output)
        self.assertIn("ge16-news-judged-batch-2.json", output)
        self.assertEqual(accepted_before, self.exists(NEWS.ACCEPTED))
        self.assertEqual(seen_before, self.exists(NEWS.DB))
        self.assertFalse(self.exists(NEWS.FEED))
        self.assertEqual(pending_before, self.read_json(NEWS.SEEN_PENDING))

        # The failure is on disk, and the next run says so out loud.
        state = self.read_json(SELFHEAL.STATE)
        self.assertEqual("partial", state["steps"]["judge"]["outcome"])
        self.assertEqual("fail", state["steps"]["commit"]["outcome"])
        self.assertEqual(SELFHEAL.OPERATIONAL, state["steps"]["commit"]["failure_class"])

        code, output = self.run_cli()
        self.assertEqual(0, code, output)
        self.assertIn("SELFHEAL: previous cycle failed at judge", output)
        self.assertIn("this run will retry it automatically", output)

    def test_stale_batch_evidence_refuses_to_commit(self):
        self.serve(["Story about a by-election"])
        self.run_cli()
        self.patch(NEWS, JUDGE_BATCH=50)
        self.run_cli("--judge-input")
        self.write_json(NEWS.JUDGED_BATCH_FMT % 1, {"accepted": []})

        manifest = self.read_json(NEWS.JUDGE_MANIFEST)
        manifest["collection_id"] = "a-different-collection"
        self.write_json(NEWS.JUDGE_MANIFEST, manifest)

        code, output = self.run_cli("--commit")
        self.assertEqual(1, code, output)
        self.assertIn("stale judged batch evidence", output)
        self.assertEqual(SELFHEAL.DESIGN,
                         self.read_json(SELFHEAL.STATE)["steps"]["commit"]["failure_class"])
        self.assertFalse(self.exists(NEWS.ACCEPTED))

    # ---- (d) complete batch commit ---------------------------------------

    def test_complete_batch_commit_merges_everything_and_clears_the_tombstone(self):
        titles = [f"Alpha{index} Bravo{index} Charlie{index} newsflash{index}"
                  for index in range(120)]
        self.serve(titles)
        self.patch(NEWS, JUDGE_BATCH=50)
        self.run_cli()
        self.run_cli("--judge-input")

        for number in (1, 2, 3):
            batch = self.read_json(NEWS.JUDGE_BATCH_FMT % number)
            accepted = [{"title": item["title"], "date": item["date"],
                         "source": "Fixture Source %d" % index, "link": item["link"],
                         "category": "election", "blocs": ["PH"], "score": 0.7}
                        for index, item in enumerate(batch["items"])]
            self.write_json(NEWS.JUDGED_BATCH_FMT % number, {"accepted": accepted})

        code, output = self.run_cli("--commit")
        self.assertEqual(0, code, output)
        self.assertIn("3 judged batch file(s)", output)
        self.assertEqual(120, self.read_json(NEWS.ACCEPTED)["count"])
        self.assertEqual(120, self.read_json(NEWS.FEED)["count"])
        self.assertEqual({}, self.read_json(NEWS.SEEN_PENDING)["pending"])
        self.assertEqual(0, self.read_json(NEWS.SEEN_PENDING)["count"])
        self.assertIn(titles[7], self.read_text(NEWS.LOG))
        self.assertEqual(120, len(self.read_json(NEWS.DB)["seen"]))
        # Consumed artefacts are cleared so the next cycle cannot replay them.
        for number in (1, 2, 3):
            self.assertFalse(self.exists(NEWS.JUDGE_BATCH_FMT % number))
            self.assertFalse(self.exists(NEWS.JUDGED_BATCH_FMT % number))
        self.assertFalse(self.exists(NEWS.JUDGE_MANIFEST))
        self.assertEqual("ok", self.read_json(SELFHEAL.STATE)["steps"]["commit"]["outcome"])

    def test_explicit_zero_acceptance_cycle_still_commits_and_clears_the_tombstone(self):
        self.serve(["Off-topic story one", "Off-topic story two"])
        self.run_cli()
        self.write_json(NEWS.JUDGED, {"accepted": []})

        code, output = self.run_cli("--commit")
        self.assertEqual(0, code, output)
        self.assertIn("ZERO-ACCEPTANCE", output)
        self.assertEqual({}, self.read_json(NEWS.SEEN_PENDING)["pending"])
        seen = set(self.read_json(NEWS.DB)["seen"])
        self.assertEqual({"Off-topic story one", "Off-topic story two"}, seen)
        self.assertEqual(0, self.read_json(NEWS.FEED)["count"])
        self.assertEqual("ok", self.read_json(SELFHEAL.STATE)["steps"]["commit"]["outcome"])

    # ---- (e) cap overflow is loud and carried ----------------------------

    def test_cap_overflow_is_reported_and_carried_not_burned(self):
        self.serve([f"Overflow story {index}" for index in range(5)])
        self.patch(NEWS, CANDIDATE_CAP=3)

        code, output = self.run_cli()
        self.assertEqual(0, code, output)
        self.assertIn("WARNING: dropped 2 candidates beyond 3 cap", output)
        payload = self.read_json(NEWS.CANDIDATES)
        self.assertEqual(3, payload["count"])
        self.assertEqual(2, payload["dropped_beyond_cap"])

        # Nothing was burned and nothing was lost: all five are tombstoned as
        # uncommitted, and the overflow is re-presented by the next collect.
        pending = self.read_json(NEWS.SEEN_PENDING)["pending"]
        self.assertEqual(5, len(pending))
        self.assertFalse(self.exists(NEWS.DB))

        self.serve([])
        code, output = self.run_cli()
        self.assertEqual(0, code, output)
        titles = self.candidate_titles()
        self.assertEqual(3, len(titles))
        payload = self.read_json(NEWS.CANDIDATES)
        self.assertEqual(5, payload["replayed_unjudged"])
        # Still over the cap while unjudged: dropped again, loudly, and still
        # carried - never burned, never silent.
        self.assertEqual(2, payload["dropped_beyond_cap"])
        self.assertIn("WARNING: dropped 2 candidates beyond 3 cap", output)
        for title in titles:
            self.assertIn(title, pending)

    # ---- (f) self-heal state and banner ----------------------------------

    def test_selfheal_classifies_operational_versus_design_failures(self):
        self.assertEqual(SELFHEAL.OPERATIONAL,
                         SELFHEAL.classify("fetch error: connection timed out"))
        self.assertEqual(SELFHEAL.OPERATIONAL,
                         SELFHEAL.classify("partial judgment: 1/3 batches judged"))
        self.assertEqual(SELFHEAL.OPERATIONAL,
                         SELFHEAL.classify("judge key file missing"))
        self.assertEqual(SELFHEAL.OPERATIONAL,
                         SELFHEAL.classify("no such file", FileNotFoundError("x")))
        self.assertEqual(SELFHEAL.DESIGN,
                         SELFHEAL.classify("validation failed after judging well-formed output"))
        self.assertEqual(SELFHEAL.DESIGN,
                         SELFHEAL.classify("contract precondition: digest mismatch"))
        self.assertEqual(SELFHEAL.DESIGN,
                         SELFHEAL.classify("stale judged batch evidence"))
        # Unmatched messages default to the retry-safe class.
        self.assertEqual(SELFHEAL.OPERATIONAL, SELFHEAL.classify("something odd happened"))

    def test_selfheal_records_each_step_and_banner_clears_after_recovery(self):
        SELFHEAL.record("collect", "ok", detail="9 candidates", cycle="cycle-1")
        SELFHEAL.record("judge", "fail", detail="fetch error: timed out", cycle="cycle-1")
        state = self.read_json(SELFHEAL.STATE)
        self.assertEqual("fail", state["steps"]["judge"]["outcome"])
        self.assertEqual(SELFHEAL.OPERATIONAL, state["steps"]["judge"]["failure_class"])
        self.assertEqual("cycle-1", state["steps"]["judge"]["cycle"])
        self.assertEqual("ok", state["steps"]["collect"]["outcome"])

        banner = SELFHEAL.banner(state, "cycle-2")
        self.assertIn("SELFHEAL: previous cycle failed at judge", banner)
        self.assertIn("this run will retry it automatically", banner)
        # A re-run inside the same cycle still reports what is unfinished.
        self.assertIn("SELFHEAL: previous cycle failed at judge", SELFHEAL.banner(state, "cycle-1"))

        # A design failure is recorded as such, and recovery clears the banner.
        SELFHEAL.record("commit", "fail", detail="validation failed on judged output",
                        cycle="cycle-1")
        state = self.read_json(SELFHEAL.STATE)
        self.assertEqual(SELFHEAL.DESIGN, state["steps"]["commit"]["failure_class"])
        SELFHEAL.record("judge", "ok", detail="3 batches", cycle="cycle-2")
        SELFHEAL.record("commit", "ok", detail="committed", cycle="cycle-2")
        self.assertEqual("", SELFHEAL.banner(self.read_json(SELFHEAL.STATE), "cycle-2"))

    def test_corrupt_state_file_self_heals_to_the_empty_state(self):
        pathlib.Path(SELFHEAL.STATE).write_text("{not json", encoding="utf-8")
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            state = SELFHEAL.load_state()
        self.assertEqual({}, state["steps"])
        self.assertEqual(SELFHEAL.SCHEMA, state["schema"])

        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            SELFHEAL.record("collect", "ok", detail="recovered", cycle="cycle-3")
        self.assertEqual("ok", self.read_json(SELFHEAL.STATE)["steps"]["collect"]["outcome"])


PENDING_SCHEMA_KEYS = {"schema", "updated_at", "collection_id", "count", "pending"}


if __name__ == "__main__":
    unittest.main()
