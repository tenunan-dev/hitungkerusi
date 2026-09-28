"""Working-root contract tests for generated ANALYTICS artifacts."""

import importlib.util
import sys
import unittest
import warnings
from pathlib import Path

from automation import paths


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
STAGE_SCRIPT = REPOSITORY_ROOT / "automation" / "outputs" / "stage_current_release.py"


def load_stage_module():
    spec = importlib.util.spec_from_file_location("work_root_stage_current_release", STAGE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class WorkRootContractTests(unittest.TestCase):
    def test_slots_are_beneath_the_single_working_root(self):
        root = REPOSITORY_ROOT / "work"
        self.assertEqual(paths.work_root(), root)
        self.assertEqual(paths.forecast_latest(), root / "forecast" / "latest")
        self.assertEqual(paths.reports_latest(), root / "reports" / "latest")
        self.assertEqual(paths.social_current(), root / "social" / "current")
        self.assertEqual(paths.tracking_root(), root / "tracking")
        for slot in (paths.forecast_latest(), paths.reports_latest(), paths.social_current(), paths.tracking_root()):
            self.assertTrue(slot.is_relative_to(root), slot)

    def test_work_contents_are_ignored_but_placeholders_are_retained(self):
        import subprocess

        slots = [
            (REPOSITORY_ROOT / "work" / "forecast" / "latest", "work/forecast/latest"),
            (REPOSITORY_ROOT / "work" / "reports" / "latest", "work/reports/latest"),
            (REPOSITORY_ROOT / "work" / "social" / "current", "work/social/current"),
            (REPOSITORY_ROOT / "work" / "tracking", "work/tracking"),
        ]

        for slot_dir, slot_path in slots:
            ignored = subprocess.run(
                ["git", "check-ignore", "-q", f"{slot_path}/.gitkeep"],
                cwd=REPOSITORY_ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode == 0
            self.assertFalse(ignored, f"{slot_path}/.gitkeep must be retained")

            ignored = subprocess.run(
                ["git", "check-ignore", "-q", f"{slot_path}/probe.json"],
                cwd=REPOSITORY_ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode == 0
            self.assertTrue(ignored, f"{slot_path}/probe.json must be ignored")

    def test_tracking_producer_and_consumer_agree_on_working_root(self):
        producer = (REPOSITORY_ROOT / "02_FORECAST/engine/common/log_utils.py").read_text(encoding="utf-8")
        self.assertIn('"work", "tracking"', producer)
        stage = load_stage_module()
        self.assertEqual(stage.resolve_tracking_source(REPOSITORY_ROOT), paths.tracking_root(REPOSITORY_ROOT) / "LATEST.json")

    def test_tracking_consumer_prefers_work_then_warns_for_legacy_fallback(self):
        import tempfile

        stage = load_stage_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / ".tracking" / "LATEST.json"
            legacy.parent.mkdir()
            legacy.write_text("legacy", encoding="utf-8")
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                self.assertEqual(stage.resolve_tracking_source(root), legacy)
            self.assertTrue(any("falling back" in str(item.message) for item in caught))
            current = paths.tracking_root(root) / "LATEST.json"
            current.parent.mkdir(parents=True)
            current.write_text("work", encoding="utf-8")
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                self.assertEqual(stage.resolve_tracking_source(root), current)
            self.assertFalse(caught)


if __name__ == "__main__":
    unittest.main()
