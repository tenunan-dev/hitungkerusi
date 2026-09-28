"""Path guards for collectors that own canonical tracker state."""

import importlib.util
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_ROOT = REPOSITORY_ROOT / "canonical"  # V3: the DATA tree lives at data/canonical
COLLECTOR_ROOT = REPOSITORY_ROOT / "scripts" / "collect"
TRACKER_ROOT = CANONICAL_ROOT / "research" / "trackers"
COLLECTORS = {
    "track_ge16_candidates.py": "BASE",
    "track_ge16_news.py": "DIR",
    "track_ge16_polls.py": "BASE",
}


def load_collector(path: Path):
    spec = importlib.util.spec_from_file_location("collector_" + path.stem, path)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load " + str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CollectorPathTests(unittest.TestCase):
    def test_collectors_resolve_the_data_tracker_root_from_their_source_path(self):
        for filename, directory_name in COLLECTORS.items():
            with self.subTest(filename=filename):
                path = COLLECTOR_ROOT / filename
                module = load_collector(path)
                self.assertEqual(module.resolve_data_root(path), CANONICAL_ROOT)
                self.assertEqual(Path(getattr(module, directory_name)), TRACKER_ROOT)

    def test_collectors_do_not_retain_analytics_work_tracking_paths(self):
        for filename in COLLECTORS:
            with self.subTest(filename=filename):
                source = (COLLECTOR_ROOT / filename).read_text(encoding="utf-8")
                self.assertNotIn('"work", "tracking"', source)
                self.assertNotIn("automation/collection", source)


if __name__ == "__main__":
    unittest.main()
