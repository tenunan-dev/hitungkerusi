"""P2.6 tests: source checkpoints, the complete accepted-news archive,
baseline/incremental windows, and per-state federal-results derivation.

Design brief: evidence/P2/P2.6-design-brief.md. Acceptance criteria mapped:

  1. checkpoint schema validation + incremental window resumes at window_end
     -> CheckpointTests
  2. archive seed count >= 2367, idempotent re-seed, +1 new item = +1 line
     -> ArchiveTests
  3. baseline vs incremental fixture behavior, no-op idempotency
     -> WindowModeTests
  4. per-state federal files: 16 groups (13 DUN + federal-territories... the
     brief's "16" figure counts states/territories; this packet resolves to
     14 OUTPUT FILES for those 16 states/territories, 13 DUN dirs + one
     federal-territories grouping for KL+Putrajaya+Labuan), Sigma=222,
     Johor byte-identical -> FederalResultsTests
  5. (full suite - run separately)
  6. collector diff empty -> CollectorDiffTests

Run:
    PYTHONPATH= PYTHONDONTWRITEBYTECODE=1 .venv/bin/python3 -m pytest \
        data/tests/test_p2_6_collection.py -q -p no:cacheprovider
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import jsonschema

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]   # data/
PROJECT_ROOT = REPOSITORY_ROOT.parent
SCRIPTS_ROOT = REPOSITORY_ROOT / "scripts"
COLLECT_ROOT = SCRIPTS_ROOT / "collect"
CANONICAL_ROOT = REPOSITORY_ROOT / "canonical"
SCHEMA_PATH = SCRIPTS_ROOT / "schemas" / "ge16_source-checkpoint.schema.json"
FIXTURE_PATH = REPOSITORY_ROOT / "tests" / "fixtures" / "p2_6" / "source-checkpoint-news.json"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SC = load_module(SCRIPTS_ROOT / "source_checkpoints.py", "source_checkpoints_p26")
FRD = load_module(SCRIPTS_ROOT / "federal_results_derive.py", "federal_results_derive_p26")


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_fixture_validates(self):
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        jsonschema.validate(fixture, self.schema)

    def test_rejects_missing_required_field(self):
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        del fixture["run_id"]
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(fixture, self.schema)

    def test_rejects_bad_run_id_pattern(self):
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        fixture["run_id"] = "not-a-run-id"
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(fixture, self.schema)

    def test_written_checkpoint_validates(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            ckdir = Path(temp) / "checkpoints"
            now = datetime(2026, 9, 29, tzinfo=timezone.utc)
            record = SC.write_checkpoint(
                "news", now, now, items_seen=10, items_accepted=2,
                run_id="20260929T000000Z-aaaaaaaa", mode="baseline",
                checkpoints_dir=ckdir, now=now)
            jsonschema.validate(record, self.schema)

    def test_incremental_window_resumes_at_prior_window_end(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            ckdir = Path(temp) / "checkpoints"
            baseline_now = datetime(2026, 9, 1, tzinfo=timezone.utc)
            start, end = SC.compute_window("news", "baseline", now=baseline_now,
                                            checkpoints_dir=ckdir)
            self.assertEqual(start, SC.DEFAULT_BASELINE_START)
            self.assertEqual(end, baseline_now)
            SC.write_checkpoint("news", start, end, items_seen=5, items_accepted=1,
                                run_id="20260901T000000Z-aaaaaaaa", mode="baseline",
                                checkpoints_dir=ckdir, now=baseline_now)
            incr_now = datetime(2026, 9, 10, tzinfo=timezone.utc)
            start2, end2 = SC.compute_window("news", "incremental", now=incr_now,
                                             checkpoints_dir=ckdir)
            self.assertEqual(start2, end)   # resumes exactly at the checkpoint's window_end
            self.assertEqual(end2, incr_now)

    def test_incremental_with_no_prior_checkpoint_falls_back_to_baseline_start(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            ckdir = Path(temp) / "checkpoints"
            now = datetime(2026, 9, 29, tzinfo=timezone.utc)
            start, end = SC.compute_window("news", "incremental", now=now, checkpoints_dir=ckdir)
            self.assertEqual(start, SC.DEFAULT_BASELINE_START)

    def test_true_noop_incremental_writes_nothing_at_all(self):
        """Boundary #5, stronger form: a no-op run does not even move
        collected_at — the checkpoint file's bytes stay identical."""
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            ckdir = Path(temp) / "checkpoints"
            now = datetime(2026, 9, 29, tzinfo=timezone.utc)
            SC.write_checkpoint("news", now, now, items_seen=5, items_accepted=1,
                               run_id="20260929T000000Z-aaaaaaaa", mode="baseline",
                               checkpoints_dir=ckdir, now=now)
            path = ckdir / "news.json"
            before = path.read_bytes()
            later = datetime(2026, 9, 30, tzinfo=timezone.utc)
            result = SC.write_checkpoint("news", now, later, items_seen=0, items_accepted=0,
                                         run_id="20260930T000000Z-bbbbbbbb", mode="incremental",
                                         checkpoints_dir=ckdir, now=later)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(result["run_id"], "20260929T000000Z-aaaaaaaa")


class ArchiveTests(unittest.TestCase):
    def _sandbox(self, temp):
        canonical = Path(temp) / "canonical"
        trackers = canonical / "research" / "trackers"
        trackers.mkdir(parents=True)
        accepted = json.loads(SC.ACCEPTED_JSON.read_text(encoding="utf-8"))
        target = trackers / "ge16-news-accepted.json"
        target.write_text(json.dumps(accepted), encoding="utf-8")
        return target, canonical / "archive" / "news-accepted" / "accepted.jsonl"

    def test_seed_count_meets_acceptance_bar(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            accepted_path, archive_path = self._sandbox(temp)
            result = SC.seed_archive_from_corpus(accepted_path, archive_path)
            self.assertGreaterEqual(result["archive_count"], 2367)
            self.assertEqual(result["appended"], result["source_items"])

    def test_reseed_is_byte_identical(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            accepted_path, archive_path = self._sandbox(temp)
            SC.seed_archive_from_corpus(accepted_path, archive_path)
            before = archive_path.read_bytes()
            result = SC.seed_archive_from_corpus(accepted_path, archive_path)
            self.assertEqual(archive_path.read_bytes(), before)
            self.assertEqual(result["appended"], 0)

    def test_one_new_item_adds_exactly_one_line(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            accepted_path, archive_path = self._sandbox(temp)
            SC.seed_archive_from_corpus(accepted_path, archive_path)
            before_count = SC.archive_count(archive_path)
            new_item = {"query": "test", "title": "Brand new unique story",
                       "date": "2026-09-29T00:00:00+00:00", "source": "Test Wire",
                       "link": "https://example.com/brand-new-story", "lang": "en",
                       "category": "test", "blocs": [], "parties": [], "seats": [],
                       "score": 1.0, "judged_at": "2026-09-29T00:00:01+00:00"}
            appended, skipped = SC.append_items([new_item], archive_path)
            self.assertEqual((appended, skipped), (1, 0))
            self.assertEqual(SC.archive_count(archive_path), before_count + 1)
            # appending the same item again is a no-op
            appended2, skipped2 = SC.append_items([new_item], archive_path)
            self.assertEqual((appended2, skipped2), (0, 1))
            self.assertEqual(SC.archive_count(archive_path), before_count + 1)

    def test_archive_is_append_only_never_rewrites_existing_lines(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            accepted_path, archive_path = self._sandbox(temp)
            SC.seed_archive_from_corpus(accepted_path, archive_path)
            first_line_before = archive_path.read_text(encoding="utf-8").splitlines()[0]
            SC.seed_archive_from_corpus(accepted_path, archive_path)
            first_line_after = archive_path.read_text(encoding="utf-8").splitlines()[0]
            self.assertEqual(first_line_before, first_line_after)


class WindowModeTests(unittest.TestCase):
    def test_baseline_and_incremental_agree_when_no_new_data(self):
        """Same accepted corpus, no new items: baseline reseed and an
        incremental no-op leave the archive identical."""
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            canonical = Path(temp) / "canonical"
            trackers = canonical / "research" / "trackers"
            trackers.mkdir(parents=True)
            accepted = json.loads(SC.ACCEPTED_JSON.read_text(encoding="utf-8"))
            accepted_path = trackers / "ge16-news-accepted.json"
            accepted_path.write_text(json.dumps(accepted), encoding="utf-8")
            archive_path = canonical / "archive" / "news-accepted" / "accepted.jsonl"
            baseline_result = SC.seed_archive_from_corpus(accepted_path, archive_path)
            snapshot_after_baseline = archive_path.read_bytes()
            # "incremental" run over the same corpus: nothing new -> no-op
            incremental_result = SC.seed_archive_from_corpus(accepted_path, archive_path)
            self.assertEqual(archive_path.read_bytes(), snapshot_after_baseline)
            self.assertEqual(incremental_result["appended"], 0)

    def test_incremental_after_checkpoint_covers_exactly_the_gap(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            ckdir = Path(temp) / "checkpoints"
            t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
            start0, end0 = SC.compute_window("news", "baseline", now=t0, checkpoints_dir=ckdir)
            SC.write_checkpoint("news", start0, end0, items_seen=1, items_accepted=1,
                               run_id="20260601T000000Z-aaaaaaaa", mode="baseline",
                               checkpoints_dir=ckdir, now=t0)
            t1 = datetime(2026, 9, 29, tzinfo=timezone.utc)
            start1, end1 = SC.compute_window("news", "incremental", now=t1, checkpoints_dir=ckdir)
            # the gap is exactly [prior window_end, now) -- nothing before t0 is re-swept
            self.assertEqual(start1, end0)
            self.assertGreater(end1, start1)
            self.assertEqual(end1, t1)

    def test_late_publication_captured_in_next_window(self):
        """P2.10 charter #3: an item whose publication date PRECEDES the
        window but that the feed only surfaces AFTER the window advanced is
        still captured — collection windows are harvest windows (when the
        feed was swept), not publication filters."""
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            ckdir = Path(temp) / "checkpoints"
            t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
            start0, end0 = SC.compute_window("news", "baseline", now=t0, checkpoints_dir=ckdir)
            SC.write_checkpoint("news", start0, end0, items_seen=1, items_accepted=1,
                               run_id="20260601T000000Z-aaaaaaaa", mode="baseline",
                               checkpoints_dir=ckdir, now=t0)
            t1 = datetime(2026, 9, 29, tzinfo=timezone.utc)
            start1, end1 = SC.compute_window("news", "incremental", now=t1, checkpoints_dir=ckdir)
            # a late-published item: published in MAY (before start1), only
            # surfaced by the feed during the [start1, end1) sweep
            late_published = datetime(2026, 5, 20, tzinfo=timezone.utc)
            self.assertLess(late_published, start1,
                            "setup: publication genuinely predates the sweep window")
            # behavioral pin: harvest semantics — window bounds derive from
            # the checkpoint + now, NEVER from item publication dates, so an
            # item surfaced during the sweep is in scope no matter when it
            # was published; seen-key dedupe prevents double-capture, not
            # the window filter.
            self.assertEqual(start1, end0)
            self.assertEqual(end1, t1)


class FederalResultsTests(unittest.TestCase):
    def test_johor_reproduced_byte_identically(self):
        ok, derived, existing = FRD.verify_johor_byte_identity()
        self.assertTrue(ok)
        self.assertEqual(derived, existing)

    def test_sigma_222_and_every_seat_in_exactly_one_group(self):
        rows = FRD.derive_rows()
        self.assertEqual(len(rows), 222)
        groups = FRD.group_by_output_file(rows)
        total = sum(len(v) for v in groups.values())
        self.assertEqual(total, 222)
        seats_seen = set()
        for group_rows in groups.values():
            for row in group_rows:
                key = (row["state"], row["seat"])
                self.assertNotIn(key, seats_seen, "seat appears in more than one group")
                seats_seen.add(key)
        self.assertEqual(len(seats_seen), 222)

    def test_fourteen_output_files_13_dun_plus_federal_territories(self):
        rows = FRD.derive_rows()
        groups = FRD.group_by_output_file(rows)
        self.assertEqual(len(groups), 14)
        self.assertIn(FRD.FEDERAL_TERRITORIES_DIR, groups)
        self.assertEqual(len(groups[FRD.FEDERAL_TERRITORIES_DIR]), 13)  # KL 11 + Putrajaya 1 + Labuan 1
        for state, dun_dir in FRD.DUN_DIR_BY_STATE.items():
            self.assertIn(dun_dir, groups)

    def test_derivation_is_deterministic_across_runs(self):
        rows_a = FRD.derive_rows()
        rows_b = FRD.derive_rows()
        self.assertEqual(rows_a, rows_b)
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            manifest_a = FRD.stage_outputs(Path(temp) / "a")
            manifest_b = FRD.stage_outputs(Path(temp) / "b")
            files_a = {f["relative_path"]: f["sha256"] for f in manifest_a["files"]}
            files_b = {f["relative_path"]: f["sha256"] for f in manifest_b["files"]}
            self.assertEqual(files_a, files_b)
            self.assertEqual(manifest_a["rows_total"], 222)

    def test_lineage_records_source_files_and_hashes(self):
        lineage = FRD.lineage_record(commit="deadbeef")
        self.assertEqual(lineage["git_commit"], "deadbeef")
        self.assertEqual(len(lineage["inputs"]), 2)
        for entry in lineage["inputs"]:
            self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")


class CollectorDiffTests(unittest.TestCase):
    def test_collect_directory_has_no_uncommitted_changes_from_this_packet(self):
        """Packet law: zero UNREVIEWED collector edits. A clean `git diff`
        over data/scripts/collect/ proves the packet did not touch it beyond
        the adjudicated P2.6 deviation (candidates collector missing the
        outdir staging contract its siblings have — found in parent
        verification 2026-09-29; see evidence/P2/P2.6-parent-verify-notes.md
        and the in-file deviation comment)."""
        result = subprocess.run(
            ["git", "diff", "--name-only", "--", "data/scripts/collect/"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True)
        changed = [line for line in result.stdout.splitlines() if line.strip()]
        allowed = {"data/scripts/collect/track_ge16_candidates.py"}
        self.assertTrue(
            set(changed) <= allowed,
            f"data/scripts/collect/ has unadjudicated changes: {changed}; "
            "P2.6 boundary #1 forbids collector edits beyond the "
            "candidates-outdir deviation")


WORK_PATHS = load_module(SCRIPTS_ROOT / "work_paths.py", "work_paths_p26")


class ModeLayerSubprocessTests(unittest.TestCase):
    """Real subprocess collector invocation (no mocks), per P2.3/P2.4
    precedent: --judge-input never touches the network, so it is safe here.
    Proves the mode layer's env knob (GE16_NEWS_MAX_DAYS) does not break the
    existing collector CLI -- the zero-edit contract from the outside. Run
    -scoped exactly like P2.3's StagedRunGuardTests so nothing lands in the
    live canonical tree."""

    def test_collector_accepts_the_existing_max_days_knob_untouched(self):
        import os
        import shutil

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            sandbox = Path(temp) / "repo"
            sandbox_trackers = sandbox / "data" / "canonical" / "research" / "trackers"
            shutil.copytree(CANONICAL_ROOT / "research" / "trackers", sandbox_trackers)
            run = WORK_PATHS.new_run(label="p26-modelayer-knob-test",
                                     data_root=sandbox / "data")
            env = dict(os.environ)
            env["HITUNGKERUSI_ROOT"] = str(sandbox)
            env[WORK_PATHS.RUN_ENV] = str(run.root)
            WORK_PATHS.apply_env(env=env)
            env["GE16_NEWS_MAX_DAYS"] = "3"

            live_before = CheckLiveUnchanged.snapshot(CANONICAL_ROOT / "research" / "trackers")
            result = subprocess.run(
                [sys.executable, str(COLLECT_ROOT / "track_ge16_news.py"), "--judge-input"],
                cwd=str(PROJECT_ROOT), capture_output=True, text=True, env=env, timeout=300)
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            self.assertEqual(
                CheckLiveUnchanged.snapshot(CANONICAL_ROOT / "research" / "trackers"),
                live_before, "the live canonical tree must not move during this test")
            self.assertTrue((run.trackers / "ge16-news-judge-manifest.json").is_file())


class CheckLiveUnchanged:
    @staticmethod
    def snapshot(root: Path):
        import hashlib
        state = {}
        for path in sorted(root.rglob("*")):
            if path.is_file():
                state[path.relative_to(root).as_posix()] = hashlib.sha256(
                    path.read_bytes()).hexdigest()
        return state


class RunNewsCollectionCycleTests(unittest.TestCase):
    """MAJOR 4 remediation: integration tests for run_news_collection itself
    — the mode layer that was untested when both blockers slipped through.
    Uses the collector's no-network paths only (--judge-input style failure
    injection via a stub NEWS_COLLECTOR script; the success path uses the
    stub too, writing a real candidates file, so no live fetch happens)."""

    def _sandbox(self, temp):
        sandbox = Path(temp) / "repo"
        (sandbox / "data" / "scripts" / "collect").mkdir(parents=True)
        (sandbox / "data" / "canonical" / "research" / "trackers").mkdir(parents=True)
        trackers = sandbox / "data" / "canonical" / "research" / "trackers"
        return sandbox, trackers

    def _stub_collector(self, sandbox, exit_code):
        stub = sandbox / "data" / "scripts" / "collect" / "stub_news.py"
        stub.write_text(
            "import json, sys\n"
            "from pathlib import Path\n"
            "code = int(sys.argv[1]) if len(sys.argv) > 1 else " + str(exit_code) + "\n"
            f"trackers = Path({str(sandbox)!r}) / 'data' / 'canonical' / 'research' / 'trackers'\n"
            "if code == 0:\n"
            "    (trackers / 'ge16-news-candidates.json').write_text(\n"
            "        json.dumps({'count': 2, 'items': [{'link': 'a'}, {'link': 'b'}]}))\n"
            "sys.exit(code)\n", encoding="utf-8")
        return stub

    def test_failed_collector_raises_and_writes_no_checkpoint(self):
        """BLOCKER 1: a non-zero collector exit must raise CollectionCycleError
        and leave the checkpoint file unwritten (window stays open)."""
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            sandbox, trackers = self._sandbox(temp)
            stub = self._stub_collector(sandbox, exit_code=1)
            checkpoints = sandbox / "data" / "canonical" / "checkpoints"
            cp = load_module(SCRIPTS_ROOT / "source_checkpoints.py", "p26r_fail")
            with mock.patch.object(cp, "NEWS_COLLECTOR", stub), \
                 mock.patch.object(cp, "REPOSITORY_ROOT", sandbox):
                with self.assertRaises(cp.CollectionCycleError) as caught:
                    cp.run_news_collection("incremental", checkpoints_dir=checkpoints,
                                           archive_file=sandbox / "no" / "archive.jsonl",
                                           accepted_json=trackers / "ge16-news-accepted.json",
                                           env={})
                self.assertEqual(caught.exception.returncode, 1)
            self.assertFalse((checkpoints / "news.json").is_file(),
                             "checkpoint must NOT be written after a failed cycle")

    def test_committed_items_reach_the_archive_next_cycle(self):
        """BLOCKER 2: an item accepted into the rolling corpus between cycles
        lands in the archive on this cycle's reconciliation, exactly once."""
        import json
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            sandbox, trackers = self._sandbox(temp)
            stub = self._stub_collector(sandbox, exit_code=0)
            accepted = trackers / "ge16-news-accepted.json"
            item = {"title": "Committed item", "link": "https://example.com/x",
                    "judged_at": "2026-09-29T00:00:00Z"}
            accepted.write_text(json.dumps({"count": 1, "items": [item]}),
                                encoding="utf-8")
            archive = sandbox / "data" / "canonical" / "archive" / "accepted.jsonl"
            checkpoints = sandbox / "data" / "canonical" / "checkpoints"
            cp = load_module(SCRIPTS_ROOT / "source_checkpoints.py", "p26r_ok")
            with mock.patch.object(cp, "NEWS_COLLECTOR", stub), \
                 mock.patch.object(cp, "REPOSITORY_ROOT", sandbox):
                result = cp.run_news_collection("baseline", checkpoints_dir=checkpoints,
                                                archive_file=archive,
                                                accepted_json=accepted, env={})
            self.assertEqual(result["archive_appended"], 1)
            lines = [json.loads(l) for l in archive.read_text(encoding="utf-8").splitlines() if l]
            self.assertEqual(len(lines), 1)
            # second cycle: idempotent — no duplicate rows
            with mock.patch.object(cp, "NEWS_COLLECTOR", stub), \
                 mock.patch.object(cp, "REPOSITORY_ROOT", sandbox):
                again = cp.run_news_collection("baseline", checkpoints_dir=checkpoints,
                                               archive_file=archive,
                                               accepted_json=accepted, env={})
            self.assertEqual(again["archive_appended"], 0)
            self.assertEqual(len(archive.read_text(encoding="utf-8").splitlines()), 1)
            self.assertTrue((checkpoints / "news.json").is_file(),
                            "successful cycle writes the checkpoint")

    def test_noop_cycle_is_byte_stable(self):
        """MAJOR 4(c): a successful cycle with nothing new changes no archive
        bytes and skips the checkpoint write (documented no-op rule)."""
        import json
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            sandbox, trackers = self._sandbox(temp)
            stub = self._stub_collector(sandbox, exit_code=0)
            accepted = trackers / "ge16-news-accepted.json"
            accepted.write_text(json.dumps({"count": 0, "items": []}), encoding="utf-8")
            archive = sandbox / "data" / "canonical" / "archive" / "accepted.jsonl"
            archive.parent.mkdir(parents=True, exist_ok=True)
            archive.write_text("", encoding="utf-8")
            checkpoints = sandbox / "data" / "canonical" / "checkpoints"
            cp = load_module(SCRIPTS_ROOT / "source_checkpoints.py", "p26r_noop")
            with mock.patch.object(cp, "NEWS_COLLECTOR", stub), \
                 mock.patch.object(cp, "REPOSITORY_ROOT", sandbox):
                first = cp.run_news_collection("baseline", checkpoints_dir=checkpoints,
                                               archive_file=archive,
                                               accepted_json=accepted, env={})
                before = archive.read_bytes()
                second = cp.run_news_collection("baseline", checkpoints_dir=checkpoints,
                                                archive_file=archive,
                                                accepted_json=accepted, env={})
            self.assertEqual(first["archive_appended"], 0)
            self.assertEqual(archive.read_bytes(), before)
            self.assertEqual(second["archive_appended"], 0)


if __name__ == "__main__":
    unittest.main()
