import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/refresh_canonical_data.py"
spec = importlib.util.spec_from_file_location("refresh_canonical_data", SCRIPT)
refresh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh)


class RefreshTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "1_DATA"
        self.root.mkdir()
        for path in refresh.CANONICAL_ROOTS:
            (self.root / path).mkdir(parents=True, exist_ok=True)
        original = json.loads((Path(__file__).resolve().parents[1] / "canonical" / refresh.MANIFEST).read_text())  # V3 canonical root
        self.manifest = copy.deepcopy(original)
        self.manifest["files"] = [{
            "destination_path": "research/raw/existing.csv", "bytes": 1,
            "sha256": "0" * 64,
            "historic_source_path": "../HERMES/01_RESEARCH/data/raw/existing.csv",
        }]
        self.manifest["methodology_inputs"] = [{
            "canonical_path": "research/data/notes/method.md",
            "legacy_source_relative_path": "01_RESEARCH/data/notes/method.md",
            "sha256": "0" * 64, "bytes": 0,
            "classification": "canonical-methodology-input", "consumer_roles": ["methodology"],
        }]
        self.path = self.root / refresh.MANIFEST
        self.path.write_text(json.dumps(self.manifest))
        (self.root / "research/raw/existing.csv").write_bytes(b"x,y\r\n1,2\r\n")
        notes = self.root / "research/data/notes"
        notes.mkdir(parents=True)
        (notes / "method.md").write_bytes(b"methodology\n")

    def test_deleted_canonical_file_fails_closed_and_preserves_manifest(self):
        """A shrinking corpus must never silently rewrite provenance to match the loss.

        Under bounded-live the adapter runs refresh -> validate, so a refresh that
        quietly dropped a deleted file would produce a manifest the validator then
        accepts, erasing the only evidence the file ever existed.
        """
        before = self.path.read_bytes()
        (self.root / "research/raw/existing.csv").unlink()
        with self.assertRaises(ValueError) as caught:
            refresh.refresh(self.root)
        self.assertIn("canonical corpus shrank", str(caught.exception))
        self.assertIn("research/raw/existing.csv", str(caught.exception))
        # The manifest must be untouched: provenance survives the failed run.
        self.assertEqual(before, self.path.read_bytes())

    def test_idempotence_preserves_schema_history_and_methodology(self):
        (self.root / "research/trackers/ge16-news-feed.json").write_bytes(b"[]\n")
        (self.root / "research/derived/new.md").write_bytes(b"new\n\n")
        outside = self.parent / "outside.txt"
        outside.write_text("unchanged")
        refresh.refresh(self.root)
        first = self.path.read_bytes()
        os.utime(self.path, (1, 1))
        refresh.refresh(self.root)
        self.assertEqual(first, self.path.read_bytes())
        self.assertGreater(self.path.stat().st_mtime, 1)
        result = json.loads(first)
        self.assertEqual(set(self.manifest), set(result))
        for key in set(result) - {"files", "methodology_inputs", "format_exceptions"}:
            self.assertEqual(self.manifest[key], result[key])
        existing = next(e for e in result["files"] if e["destination_path"] == "research/raw/existing.csv")
        self.assertEqual(self.manifest["files"][0]["historic_source_path"], existing["historic_source_path"])
        self.assertEqual(hashlib.sha256(b"x,y\r\n1,2\r\n").hexdigest(), existing["sha256"])
        for entry in result["files"]:
            self.assertEqual(set(self.manifest["files"][0]), set(entry))
        news = next(e for e in result["files"] if e["destination_path"].endswith("feed.json"))
        self.assertEqual("collector:scripts/collect/track_ge16_news.py#research/trackers/ge16-news-feed.json", news["historic_source_path"])
        method = result["methodology_inputs"][0]
        self.assertEqual(set(self.manifest["methodology_inputs"][0]), set(method))
        self.assertEqual(hashlib.sha256(b"methodology\n").hexdigest(), method["sha256"])
        self.assertEqual([
            {"destination_path": "research/raw/existing.csv", "source_preserved": "CRLF"},
            {"destination_path": "research/derived/new.md", "source_preserved": "blank-at-EOF"},
        ], result["format_exceptions"]["entries"])
        self.assertEqual("unchanged", outside.read_text())
        self.assertFalse(list(self.root.glob(".canonical-refresh-*")))

    def test_symlink_manifest_never_writes_outside_data(self):
        outside = self.parent / "outside.json"
        original = self.path.read_bytes()
        outside.write_bytes(original)
        self.path.unlink()
        self.path.symlink_to(outside)
        with self.assertRaises(OSError):
            refresh.refresh(self.root)
        self.assertEqual(original, outside.read_bytes())

    def test_symlink_roots_files_and_hardlinks_fail_before_write(self):
        outside = self.parent / "external"
        outside.mkdir()
        (outside / "data").write_text("outside")
        original = self.path.read_bytes()
        for kind in ("directory", "file", "hardlink"):
            with self.subTest(kind=kind):
                link = self.root / "research/raw/link"
                if kind == "hardlink":
                    os.link(outside / "data", link)
                else:
                    link.symlink_to(outside if kind == "directory" else outside / "data")
                with self.assertRaises((OSError, ValueError)):
                    refresh.refresh(self.root)
                self.assertEqual(original, self.path.read_bytes())
                self.assertEqual("outside", (outside / "data").read_text())
                link.unlink()

    def test_unapproved_root_and_methodology_escape_fail_before_write(self):
        for target in ("roots", "methodology_inputs"):
            manifest = copy.deepcopy(self.manifest)
            if target == "roots":
                manifest[target] = ["../2_ANALYTICS"]
            else:
                manifest[target][0]["canonical_path"] = "../outside.md"
            raw = json.dumps(manifest)
            self.path.write_text(raw)
            with self.assertRaises(ValueError):
                refresh.refresh(self.root)
            self.assertEqual(raw, self.path.read_text())


class RunModeParityTests(unittest.TestCase):
    """P2.3 CLI surface: --no-run is byte-for-byte the pre-P2.3 behavior."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "1_DATA"
        self.root.mkdir()
        for path in refresh.CANONICAL_ROOTS:
            (self.root / path).mkdir(parents=True, exist_ok=True)
        original = json.loads((Path(__file__).resolve().parents[1] / "canonical" / refresh.MANIFEST).read_text())
        original["files"] = [{
            "destination_path": "research/raw/existing.csv", "bytes": 1,
            "sha256": "0" * 64,
            "historic_source_path": "../HERMES/01_RESEARCH/data/raw/existing.csv",
        }]
        original["methodology_inputs"] = []
        (self.root / refresh.MANIFEST).write_text(json.dumps(original))
        (self.root / "research/raw/existing.csv").write_bytes(b"x,y\r\n1,2\r\n")

    def test_no_run_main_is_exactly_the_prew_p23_invocation(self):
        files = refresh.refresh(self.root)  # pre-change code path, directly
        first = self.path_manifest_bytes()
        os.utime(self.root / refresh.MANIFEST, (1, 1))
        output = io.StringIO()
        with redirect_stdout(output), mock.patch.dict(os.environ, {}, clear=False):
            for knob in ("GE16_RUN_DIR", "GE16_TRACKER_OUT_DIR", "GE16_SELFHEAL_STATE"):
                os.environ.pop(knob, None)
            code = refresh.main(["--no-run"], canonical_root=self.root)
        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue(), f"canonical refresh passed: files={files}\n")
        self.assertEqual(first, self.path_manifest_bytes())  # deterministic bytes
        self.assertFalse((self.parent / "work").exists())  # no run dir anywhere

    def test_bare_argv_defaults_to_run_mode(self):
        stub = {"run_id": "20260929T000000Z-00000000", "run_dir": "/tmp/x",
                "label": "", "collectors": [], "promoted": [], "unchanged": [],
                "edition_id": "20260929T000000Z", "edition_path": "/tmp/e",
                "files": 0}
        with mock.patch.object(refresh, "staged_refresh", return_value=stub) as patched:
            output = io.StringIO()
            with redirect_stdout(output):
                code = refresh.main([], canonical_root=self.root)
        self.assertEqual(code, 0)
        patched.assert_called_once_with(label="", canonical_root=self.root)
        self.assertIn("run 20260929T000000Z-00000000", output.getvalue())

    def path_manifest_bytes(self):
        return (self.root / refresh.MANIFEST).read_bytes()


if __name__ == "__main__":
    unittest.main()
