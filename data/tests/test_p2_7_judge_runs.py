"""P2.7 resumable judgment-run machinery: checkpointing, resume, idempotence,
source-hash binding, run-state schema, partial-completion guard, probe
three-way mapping, and the orphaned-flag corpus count guard.

Run from the repository root:
    python3 -m pytest data/tests/test_p2_7_judge_runs.py -q
"""
import importlib.util
import json
import os
import signal
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]   # data/
SCRIPTS_ROOT = REPOSITORY_ROOT / "scripts"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


JR = load_module(SCRIPTS_ROOT / "judge_runs.py", "judge_runs_p27")
WP = load_module(SCRIPTS_ROOT / "work_paths.py", "work_paths_p27")


def _fake_run(tmp_path, run_id="20260930T000000Z-deadbeef"):
    root = tmp_path / "work" / run_id
    run = WP._paths(run_id, root)
    for directory in run.subdirs():
        directory.mkdir(parents=True, exist_ok=True)
    run.run_json.write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
    return run


def _items(n):
    return [{"evidence_id": f"ev{'0' * 14}{i:02x}", "url": f"https://publisher.example/{i}"}
            for i in range(n)]


def _stub_prober_all(outcome):
    def prober(url, timeout=8):
        if outcome == "verified":
            return {"http_status": 200, "final_url": url, "error": None}
        if outcome == "rejected-stale":
            return {"http_status": 404, "final_url": url, "error": None}
        return {"http_status": None, "final_url": url, "error": "timed out"}
    return prober


class RecordDecisionTests(unittest.TestCase):
    def test_per_item_idempotence(self):
        tmp = self._tmp()
        run = _fake_run(tmp)
        items = _items(3)
        session = JR.open_run(items, "test-judge", run=run, item_class="queue-evidence")
        row1 = JR.record_decision(session, items[0], "verified", "r1", "a" * 64)
        row2 = JR.record_decision(session, items[0], "verified", "r1", "a" * 64)
        self.assertEqual(row1, row2)
        self.assertEqual(session.state["decided"], 1)
        self.assertEqual(session.state["counts"]["verified"], 1)
        lines = session.decisions_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)

    def test_canonical_supersede_id_threads_through(self):
        """P2.7 R1-F1: a FIRST decision on an item with a pre-existing
        canonical judgment must carry that judgment id as its supersede
        target — envelope row AND embedded judgment note — plus run_id
        binding in the embedded judge (R1-F3)."""
        tmp = self._tmp()
        run = _fake_run(tmp)
        items = [{"evidence_id": "ev" + "a" * 16, "url": "https://p.example/1",
                  "judgment_id": "jgcanonical0001"}]
        session = JR.open_run(items, "test-judge", run=run, item_class="orphaned-flag")
        row = JR.record_decision(session, items[0], "verified", "probe 200",
                                 "a" * 64, supersedes_canonical_id="jgcanonical0001")
        self.assertEqual(row["supersedes"], "jgcanonical0001")
        self.assertIn("(supersedes jgcanonical0001)",
                      row["judgment"]["confidence_note"])
        self.assertEqual(row["judgment"]["judge"]["run_id"], session.run_id)

    def test_empty_run_is_never_complete(self):
        """P2.7 R1-F2: a zero-item run reports complete=False — deciding
        nothing demonstrates nothing."""
        tmp = self._tmp()
        run = _fake_run(tmp)
        session = JR.open_run([], "test-judge", run=run, item_class="queue-evidence")
        summary = JR.close_run(session)
        self.assertFalse(summary["complete"])

    def test_source_hash_binding_creates_superseding_row(self):
        tmp = self._tmp()
        run = _fake_run(tmp)
        items = _items(2)
        session = JR.open_run(items, "test-judge", run=run, item_class="queue-evidence")
        row1 = JR.record_decision(session, items[0], "verified", "first probe", "a" * 64)
        row2 = JR.record_decision(session, items[0], "rejected-stale", "second probe", "b" * 64)
        self.assertNotEqual(row1["judgment_id"], row2["judgment_id"])
        self.assertEqual(row2["supersedes"], row1["judgment_id"])
        lines = session.decisions_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(session.state["counts"]["rejected-stale"], 1)
        self.assertEqual(session.state["counts"]["verified"], 0)
        self.assertEqual(session.state["decided"], 1)

    def test_embedded_judgment_row_validates_against_schema(self):
        tmp = self._tmp()
        run = _fake_run(tmp)
        items = _items(1)
        items[0]["evidence_id"] = "ev" + "0" * 16
        session = JR.open_run(items, "test-judge", run=run, item_class="orphaned-flag")
        row = JR.record_decision(session, items[0], "verified", "publisher 200", "c" * 64)
        errors = list(JR.judgment_validator().iter_errors(row["judgment"]))
        self.assertEqual(errors, [])
        self.assertEqual(row["judgment"]["verdict"], "accept")

    def _tmp(self):
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        return tmp


class TornTailTests(unittest.TestCase):
    def test_torn_last_line_is_discarded_and_item_redecided(self):
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        run = _fake_run(tmp)
        items = _items(3)
        session = JR.open_run(items, "test-judge", run=run, item_class="queue-evidence")
        JR.record_decision(session, items[0], "verified", "r0", "a" * 64)
        # Simulate a crash mid-write: append a truncated JSON fragment.
        with open(session.decisions_path, "a", encoding="utf-8") as handle:
            handle.write('{"evidence_id": "ev00000000000001", "verdict": "verif')
        session2 = JR.open_run(items, "test-judge", run=run, item_class="queue-evidence")
        self.assertEqual(session2.state["next_index"], 1)
        self.assertEqual(session2.state["decided"], 1)
        lines = session2.decisions_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        for line in lines:
            json.loads(line)  # every remaining line parses cleanly
        JR.record_decision(session2, items[1], "verified", "r1", "b" * 64)
        self.assertEqual(session2.state["decided"], 2)


class RunStateSchemaTests(unittest.TestCase):
    def test_fixture_state_validates(self):
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        run = _fake_run(tmp)
        session = JR.open_run(_items(2), "test-judge", run=run, item_class="queue-evidence")
        errors = list(JR.run_state_validator().iter_errors(session.state))
        self.assertEqual(errors, [])

    def test_missing_required_field_fails_validation(self):
        state = {"schema": "ge16.judgment-run.v1", "run_id": "20260930T000000Z-deadbeef"}
        errors = list(JR.run_state_validator().iter_errors(state))
        self.assertGreater(len(errors), 0)


class PartialCompletionTests(unittest.TestCase):
    def test_pending_item_means_not_complete(self):
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        run = _fake_run(tmp)
        items = _items(2)
        session = JR.open_run(items, "test-judge", run=run, item_class="queue-evidence")
        JR.record_decision(session, items[0], "verified", "r0", "a" * 64)
        summary = JR.close_run(session)
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["counts"]["pending"], 1)

    def test_fully_decided_run_reports_complete(self):
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        run = _fake_run(tmp)
        items = _items(2)
        session = JR.open_run(items, "test-judge", run=run, item_class="queue-evidence")
        JR.record_decision(session, items[0], "verified", "r0", "a" * 64)
        JR.record_decision(session, items[1], "unresolved", "r1", "b" * 64)
        summary = JR.close_run(session)
        self.assertTrue(summary["complete"])


class ProbeMappingTests(unittest.TestCase):
    def test_publisher_200_is_verified(self):
        result = {"http_status": 200, "final_url": "https://publisher.example/a", "error": None}
        verdict, _reason = JR.classify_probe(result)
        self.assertEqual(verdict, "verified")

    def test_wrapper_only_is_rejected_stale(self):
        result = {"http_status": 200, "final_url": "https://news.google.com/rss/articles/x", "error": None}
        verdict, _reason = JR.classify_probe(result)
        self.assertEqual(verdict, "rejected-stale")

    def test_404_is_rejected_stale(self):
        result = {"http_status": 404, "final_url": "https://publisher.example/a", "error": None}
        verdict, _reason = JR.classify_probe(result)
        self.assertEqual(verdict, "rejected-stale")

    def test_410_is_rejected_stale(self):
        result = {"http_status": 410, "final_url": "https://publisher.example/a", "error": None}
        verdict, _reason = JR.classify_probe(result)
        self.assertEqual(verdict, "rejected-stale")

    def test_timeout_is_unresolved(self):
        result = {"http_status": None, "final_url": "https://publisher.example/a", "error": "timed out"}
        verdict, _reason = JR.classify_probe(result)
        self.assertEqual(verdict, "unresolved")

    def test_probe_item_uses_injected_prober_never_network(self):
        called = {}

        def stub(url, timeout=8):
            called["url"] = url
            return {"http_status": 200, "final_url": url, "error": None}

        verdict, reason, input_sha256 = JR.probe_item("https://publisher.example/x", prober=stub)
        self.assertEqual(verdict, "verified")
        self.assertEqual(called["url"], "https://publisher.example/x")
        self.assertEqual(len(input_sha256), 64)


class OrphanedFlagCountGuardTests(unittest.TestCase):
    """Read-only against the live corpus (design brief scoping correction:
    the 811 orphaned-flag judgments split into a 498-row no-surviving-batch
    subset — the true re-judge target — and 313 disagree rows excluded from
    this pass). If the corpus changes, this test's expectations update WITH
    it, loudly (RuntimeError names the actual count found)."""

    def test_counts_match_corpus(self):
        items, total, disagree = JR.select_orphaned_flag_subset()
        self.assertEqual(total, 811, f"expected 811 orphaned-flag rows, found {total}")
        self.assertEqual(len(items), 498, f"expected 498 no-surviving-batch rows, found {len(items)}")
        self.assertEqual(disagree, 313, f"expected 313 disagree rows, found {disagree}")

    def test_pass_function_enforces_the_guard_before_proceeding(self):
        import inspect
        source = inspect.getsource(JR.run_orphaned_flag_pass)
        self.assertIn("!= 811", source)
        self.assertIn("!= 498", source)


class ResumeAfterKillTests(unittest.TestCase):
    """Real SIGKILL of a subprocess mid-run (P2.3/P2.4 precedent: real kill,
    not a mock)."""

    def test_resume_after_sigkill(self):
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        run_id = "20260930T000000Z-cafebabe"
        run = _fake_run(tmp, run_id)

        driver = tmp / "driver.py"
        driver.write_text(textwrap.dedent(f"""
            import importlib.util, json, sys, time
            spec = importlib.util.spec_from_file_location("jr", {str(SCRIPTS_ROOT / "judge_runs.py")!r})
            jr = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(jr)
            spec2 = importlib.util.spec_from_file_location("wp", {str(SCRIPTS_ROOT / "work_paths.py")!r})
            wp = importlib.util.module_from_spec(spec2)
            spec2.loader.exec_module(wp)
            import pathlib
            run = wp._paths({run_id!r}, pathlib.Path({str(run.root)!r}))
            items = [{{"evidence_id": "ev%016x" % i, "url": "https://publisher.example/%d" % i}}
                     for i in range(6)]
            session = jr.open_run(items, "kill-test-judge", run=run, item_class="queue-evidence")
            for index in range(session.state["next_index"], len(items)):
                jr.record_decision(session, items[index], "verified", "r", "%064d" % index)
                if index == 2:
                    sys.stdout.write("CHECKPOINT\\n")
                    sys.stdout.flush()
                    time.sleep(30)
            sys.stdout.write("DONE\\n")
        """), encoding="utf-8")

        proc = subprocess.Popen([sys.executable, str(driver)], stdout=subprocess.PIPE,
                                text=True)
        line = proc.stdout.readline()
        self.assertEqual(line.strip(), "CHECKPOINT")
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
        self.assertNotEqual(proc.returncode, 0)

        items = [{"evidence_id": "ev%016x" % i, "url": "https://publisher.example/%d" % i}
                 for i in range(6)]
        session = JR.open_run(items, "kill-test-judge", run=run, item_class="queue-evidence")
        self.assertEqual(session.state["next_index"], 3)
        for index in range(session.state["next_index"], len(items)):
            JR.record_decision(session, items[index], "verified", "r", "%064d" % index)
        summary = JR.close_run(session)
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["decided"], 6)
        seen_ids = set()
        with open(session.decisions_path, encoding="utf-8") as handle:
            for raw_line in handle:
                row = json.loads(raw_line)
                key = (row["evidence_id"], row["input_sha256"])
                self.assertNotIn(key, seen_ids, "duplicate decision after resume")
                seen_ids.add(key)
        self.assertEqual(len(seen_ids), 6)


if __name__ == "__main__":
    unittest.main()
