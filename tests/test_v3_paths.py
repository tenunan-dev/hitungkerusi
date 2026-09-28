"""Tests for v3_paths project-root resolution (P1.6)."""
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
v3_paths = importlib.util.spec_from_file_location(
    "v3_paths", REPO / "v3_paths.py"
)
module = importlib.util.module_from_spec(v3_paths)
v3_paths.loader.exec_module(module)


class TestProjectRoot(unittest.TestCase):
    def test_env_override_wins(self):
        with tempfile.TemporaryDirectory() as td:
            try:
                old = os.environ.get("HITUNGKERUSI_ROOT")
                os.environ["HITUNGKERUSI_ROOT"] = td
                self.assertEqual(module.project_root(), Path(td).resolve())
            finally:
                if old is None:
                    del os.environ["HITUNGKERUSI_ROOT"]
                else:
                    os.environ["HITUNGKERUSI_ROOT"] = old

    def test_walks_up_to_marker(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "requirements.txt").write_text("# marker\n")
            nested = root / "a" / "b"
            nested.mkdir(parents=True)
            self.assertEqual(module.project_root(nested), root.resolve())

    def test_data_root_is_root_slash_data(self):
        self.assertEqual(module.data_root(), REPO / "data")

    def test_real_root_found(self):
        self.assertEqual(module.project_root(), REPO)


if __name__ == "__main__":
    unittest.main()
