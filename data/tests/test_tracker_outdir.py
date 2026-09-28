"""``GE16_TRACKER_OUT_DIR``: the staging knob that makes a dry run side-effect free.

The baseline rebuild sets the knob for a ``--dry-run`` (and for a live staged
merge) so that every tracker write a collector performs lands in
``2_ANALYTICS/work/baseline/stage/<run_id>/trackers/`` instead of the live
``1_DATA/research/trackers`` directory (review MED-1/MED-2, 26 Sep 2026).

Two failure modes are pinned here, both of which are silent in production:

  * the module's live-root constant drifts off by a directory. ``owns()`` then
    answers False for real tracker files, the knob becomes a no-op, and a
    ``--dry-run`` writes live tracker state (this actually happened: the root was
    three ``dirname`` calls up instead of two, so the knob did nothing and a
    composite dry run rewrote 13 live tracker files).
  * a collector writes a tracker file without going through the knob.

The second is tested by running the real collectors offline (``--judge-input``
needs no network) against a staged directory and asserting the live directory is
byte-identical afterwards.
"""

import hashlib
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_ROOT = REPOSITORY_ROOT / "scripts" / "collect"
TRACKER_ROOT = REPOSITORY_ROOT / "canonical" / "research" / "trackers"
OUTDIR_PATH = COLLECTOR_ROOT / "ge16_tracker_outdir.py"
#: Offline collector commands that read a tracker candidate file and write tracker
#: batch/manifest files. Neither opens a network connection.
OFFLINE_COLLECTORS = (
    ("track_ge16_news.py", "ge16-news-judge-batch-1.json"),
    ("ge16_news_backfill.py", "ge16-news-backfill-judge-batch-1.json"),
)


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load " + str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


OUTDIR = load_module(OUTDIR_PATH, "ge16_tracker_outdir_under_test")


def snapshot(root: Path):
    state = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            state[str(path.relative_to(root))] = digest
    return state


class TrackerOutdirUnitTests(unittest.TestCase):
    def test_the_live_dir_is_the_data_tracker_root(self):
        """Pin the constant: an off-by-one here silently disables all staging."""
        self.assertEqual(Path(OUTDIR.LIVE_DIR), TRACKER_ROOT)
        self.assertTrue(TRACKER_ROOT.is_dir(), TRACKER_ROOT)
        self.assertTrue((TRACKER_ROOT / "ge16-news-accepted.json").is_file())

    def test_owns_covers_every_live_tracker_file_and_nothing_else(self):
        self.assertTrue(OUTDIR.owns(TRACKER_ROOT / "ge16-news-accepted.json"))
        self.assertTrue(OUTDIR.owns(Path(OUTDIR.LIVE_DIR) / "anything.json"))
        for outside in (REPOSITORY_ROOT / "research" / "other" / "x.json",
                        REPOSITORY_ROOT / "work" / "x.json",
                        REPOSITORY_ROOT / "x.json",
                        "", None):
            with self.subTest(path=outside):
                self.assertFalse(OUTDIR.owns(outside))

    def test_knob_unset_is_the_identity_function(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(OUTDIR.OUT_ENV, None)
            live = TRACKER_ROOT / "ge16-news-accepted.json"
            self.assertIsNone(OUTDIR.out_dir())
            self.assertEqual(OUTDIR.w(live), str(live))
            self.assertEqual(OUTDIR.a(live), str(live))
            self.assertEqual(OUTDIR.r(live), str(live))
            self.assertIsNone(OUTDIR.staged("x.json"))
            self.assertEqual(OUTDIR.g(str(TRACKER_ROOT / "ge16-news-accepted.json")),
                             [str(TRACKER_ROOT / "ge16-news-accepted.json")])

    def test_knob_set_routes_writes_appends_reads_and_globs_into_the_stage(self):
        with tempfile.TemporaryDirectory() as temp:
            staged_root = Path(temp) / "stage" / "trackers"
            live = TRACKER_ROOT / "ge16-news-accepted.json"
            live_bytes = live.read_bytes()
            with unittest.mock.patch.dict(os.environ, {OUTDIR.OUT_ENV: str(staged_root)}):
                OUTDIR.ensure()
                self.assertTrue(staged_root.is_dir())
                target = Path(OUTDIR.w(live))
                self.assertEqual(target.parent, staged_root)
                # reads fall back to the live file until this run writes a staged copy
                self.assertEqual(OUTDIR.r(live), str(live))
                target.write_text("staged", encoding="utf-8")
                self.assertEqual(OUTDIR.r(live), str(target))
                # appends are seeded from the live bytes so the swap cannot drop history
                log = TRACKER_ROOT / "ge16-general-news-log.md"
                log_bytes = log.read_bytes()
                appended = Path(OUTDIR.a(log))
                self.assertEqual(appended.parent, staged_root)
                self.assertEqual(appended.read_bytes(), log_bytes)
                self.assertEqual(log.read_bytes(), log_bytes)
                # a glob in staged mode matches the stage only, never live evidence
                globbed = OUTDIR.g(str(TRACKER_ROOT / "ge16-news-*.json"))
                self.assertTrue(globbed, "the staged accepted copy is a match")
                self.assertTrue(all(Path(path).parent == staged_root for path in globbed),
                                globbed)
                self.assertEqual(Path(OUTDIR.staged("x.json")), staged_root / "x.json")
                self.assertEqual(live.read_bytes(), live_bytes)


class CollectorStagingTests(unittest.TestCase):
    """The real collectors, offline, with the knob set: live bytes must not move."""

    def _run(self, script: str, argument: str, env: dict):
        result = subprocess.run(
            [sys.executable, str(COLLECTOR_ROOT / script), argument],
            cwd=str(REPOSITORY_ROOT), capture_output=True, text=True, env=env,
        )
        return result

    def test_offline_collector_passes_write_only_into_the_stage(self):
        before = snapshot(TRACKER_ROOT)
        with tempfile.TemporaryDirectory() as temp:
            staged_root = Path(temp) / "trackers"
            # Exactly the env the baseline rebuild sets for a --dry-run: the tracker
            # directory AND the self-heal ledger are both staged.
            env = dict(os.environ, **{
                OUTDIR.OUT_ENV: str(staged_root),
                "GE16_SELFHEAL_STATE": str(staged_root / "ge16-selfheal-state.json"),
            })
            for script, expected in OFFLINE_COLLECTORS:
                with self.subTest(script=script):
                    result = self._run(script, "--judge-input", env)
                    self.assertEqual(result.returncode, 0,
                                     "%s --judge-input failed: %s" % (script, result.stderr[-2000:]))
                    self.assertTrue((staged_root / expected).is_file(),
                                    "%s did not route %s into the stage" % (script, expected))
            self.assertTrue((staged_root / "ge16-news-judge-manifest.json").is_file())
        self.assertEqual(snapshot(TRACKER_ROOT), before,
                         "a staged collector pass changed live tracker bytes")

    def test_offline_collector_without_the_knob_keeps_the_live_default(self):
        """Unset, the collectors own the live directory: the knob is not a policy.

        Asserted at the module level so this suite never writes live tracker state.
        """
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(OUTDIR.OUT_ENV, None)
            self.assertIsNone(OUTDIR.out_dir())
            live = TRACKER_ROOT / "ge16-news-candidates.json"
            self.assertEqual(OUTDIR.w(live), str(live))
            self.assertIsNone(OUTDIR.staged(live.name))
            self.assertEqual(set(OUTDIR.g(str(TRACKER_ROOT / "*.json"))),
                             set(str(path) for path in TRACKER_ROOT.glob("*.json")))


if __name__ == "__main__":
    unittest.main()
