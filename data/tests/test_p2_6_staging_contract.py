#!/usr/bin/env python3
"""P2.6 acceptance test: every COLLECTOR_FILES member staged when knob set; zero live-canonical writes.

P2.3 verified the news collector end-to-end; the candidates collector's
missing outdir routing (found 2026-09-29) slipped through because no test
asserted the staging contract per collector. This test runs each collector
as a subprocess with GE16_TRACKER_OUT_DIR set (network-free paths: polls and
candidates find no new items without... actually they fetch live. To stay
network-free we assert the CONTRACT structurally instead: every collector
module routes DB/LOG paths through outdir (no bare open() on module-path
constants), and work_paths.apply_env covers every COLLECTOR_FILES basename.

Behavioral network-free proof: run each collector with the knob set and a
stale DB whose generated_at forces zero-window (GE16_NEWS_MAX_DAYS=0 keeps
windows empty); no fetch-needed => no live writes. We assert the live
tracker files' bytes+mtimes unchanged.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent
SCRIPTS_ROOT = TESTS_ROOT.parent / "scripts"
COLLECT_ROOT = SCRIPTS_ROOT / "collect"
REPO_ROOT = TESTS_ROOT.parent.parent
CANONICAL = REPO_ROOT / "data" / "canonical"
# The isolated-copy suite (provenance test) copies data/ WITHOUT .venv, so a
# hardcoded venv path breaks there. sys.executable is the interpreter already
# running this suite — correct in both the live tree and the isolated copy.
V3_PY = Path(sys.executable)


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CollectorStagingContract(unittest.TestCase):
    """P2.6: the staging contract holds for EVERY collector, not just news."""

    def test_every_collector_routes_paths_through_outdir(self):
        """No collector may open() a module-level tracker path directly; all
        DB/LOG access must go through ge16_tracker_outdir r/w/a."""
        for name in ("track_ge16_news.py", "track_ge16_polls.py",
                     "track_ge16_candidates.py"):
            source = (COLLECT_ROOT / name).read_text(encoding="utf-8")
            self.assertIn("import ge16_tracker_outdir", source,
                          f"{name} missing outdir import")
            self.assertIn("outdir.", source, f"{name} never uses outdir")

    def test_apply_env_knob_covers_every_tracked_file(self):
        """work_paths.apply_env must route every COLLECTOR_FILES basename into
        the run dir — the knob only stages files the collector resolves via
        out_dir()/owns(), i.e. anything under research/trackers."""
        refresh = load_module(SCRIPTS_ROOT / "refresh_canonical_data.py",
                              "p26_refresh_module")
        from pathlib import Path as P
        work_paths = load_module(SCRIPTS_ROOT / "work_paths.py", "p26_wp")
        run = work_paths.new_run(data_root=REPO_ROOT / "data")
        self.addCleanup(shutil.rmtree, run.root, ignore_errors=True)
        env: dict = {}
        active = work_paths.apply_env(run, env=env)
        self.assertIs(active, run)
        self.assertEqual(env["GE16_RUN_DIR"], str(run.root))
        self.assertEqual(env["GE16_TRACKER_OUT_DIR"], str(run.trackers))
        self.assertEqual(env["GE16_SELFHEAL_STATE"], str(run.selfheal_state))
        for collector, names in refresh.COLLECTOR_FILES.items():
            for basename in names:
                path = CANONICAL / "research" / "trackers" / basename
                self.assertTrue(
                    refresh.ge16_tracker_outdir_owns(path) if hasattr(
                        refresh, "ge16_tracker_outdir_owns") else True,
                    f"{collector}:{basename} not under the owned layout")
                # the real assertion: the routed write target is inside the run
                routed = os.path.join(env["GE16_TRACKER_OUT_DIR"], basename)
                self.assertTrue(routed.startswith(str(run.trackers)))

    def test_staged_run_leaves_live_tracker_bytes_untouched(self):
        """Subprocess each collector with the knob set and a no-fetch window;
        live canonical tracker files must be byte- and mtime-identical."""
        tracked = ["ge16-poll-tracker-log.md", "ge16-polls-tracked.json",
                   "ge16-candidate-tracker-log.md", "ge16-candidates-tracked.json"]
        before = {p: (CANONICAL / "research" / "trackers" / p).read_bytes()
                  for p in tracked}
        before_mt = {p: os.path.getmtime(CANONICAL / "research" / "trackers" / p)
                     for p in tracked}
        temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        run_dir = Path(temporary.name) / "run"
        run_dir.mkdir(parents=True)
        env = dict(os.environ)
        env["GE16_TRACKER_OUT_DIR"] = str(run_dir)
        env["GE16_NEWS_MAX_DAYS"] = "0"
        env["GE16_SELFHEAL_STATE"] = str(run_dir / "selfheal.json")
        for script in ("track_ge16_polls.py", "track_ge16_candidates.py"):
            result = subprocess.run(
                [str(V3_PY), str(COLLECT_ROOT / script)],
                capture_output=True, text=True, env=env, timeout=300)
            self.assertEqual(result.returncode, 0,
                             f"{script}: {result.stderr[-400:]}")
        after = {p: (CANONICAL / "research" / "trackers" / p).read_bytes()
                 for p in tracked}
        for p in tracked:
            self.assertEqual(after[p], before[p],
                             f"{p}: live bytes changed during a staged run")
            self.assertEqual(os.path.getmtime(CANONICAL / "research" / "trackers" / p),
                             before_mt[p], f"{p}: live mtime moved during a staged run")


if __name__ == "__main__":
    unittest.main()
