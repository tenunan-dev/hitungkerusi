import errno
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_ROOT = REPOSITORY_ROOT / "canonical"  # V3: the DATA tree lives at data/canonical
EXTRACTOR = REPOSITORY_ROOT / "scripts" / "extract_canonical_data.py"
VALIDATOR = REPOSITORY_ROOT / "scripts" / "validate_canonical_data.py"
HISTORIC_ROOTS = (
    ("../HERMES/01_RESEARCH/data/raw", "research/raw"),
    ("../HERMES/01_RESEARCH/data/derived", "research/derived"),
    ("../HERMES/01_RESEARCH/data/trackers", "research/trackers"),
    ("../HERMES/01_RESEARCH/federal", "research/federal"),
    ("../HERMES/01_RESEARCH/geo", "geo"),
    ("../HERMES/01_RESEARCH/states", "research/states"),
)
EXPECTED_METHODOLOGY = (
    ("01_RESEARCH/data/notes/byelections-malaysia-1957-2026.md", "research/data/notes/byelections-malaysia-1957-2026.md", ["historical-baseline", "forecast-modeling"]),
    ("01_RESEARCH/data/notes/election-study-organizations-malaysia.md", "research/data/notes/election-study-organizations-malaysia.md", ["research-methodology"]),
    ("01_RESEARCH/data/notes/ge15-candidates-demographics.md", "research/data/notes/ge15-candidates-demographics.md", ["demographic-analysis"]),
    ("01_RESEARCH/data/notes/ge15-results-by-state.md", "research/data/notes/ge15-results-by-state.md", ["results-analysis"]),
    ("01_RESEARCH/data/notes/marginal-seats-ge15.md", "research/data/notes/marginal-seats-ge15.md", ["seat-prioritization"]),
    ("01_RESEARCH/data/notes/parliamentary-seats-malaysia-data.md", "research/data/notes/parliamentary-seats-malaysia-data.md", ["constituency-reference"]),
    ("01_RESEARCH/data/notes/party-landscape-update-2026.md", "research/data/notes/party-landscape-update-2026.md", ["party-analysis", "forecast-modeling"]),
    ("01_RESEARCH/data/notes/voter-demographics-by-constituency-ge15.md", "research/data/notes/voter-demographics-by-constituency-ge15.md", ["demographic-analysis"]),
    ("01_RESEARCH/knowledge/anti-hopping-law-factor.md", "research/knowledge/anti-hopping-law-factor.md", ["institutional-analysis"]),
    ("01_RESEARCH/knowledge/demographics-parties-analysis.md", "research/knowledge/demographics-parties-analysis.md", ["demographic-analysis", "party-analysis"]),
    ("01_RESEARCH/knowledge/forecast-factor-rankings.md", "research/knowledge/forecast-factor-rankings.md", ["forecast-modeling"]),
    ("01_RESEARCH/knowledge/forecast-theory.md", "research/knowledge/forecast-theory.md", ["forecast-modeling"]),
    ("01_RESEARCH/knowledge/political-parties.md", "research/knowledge/political-parties.md", ["party-analysis"]),
    ("01_RESEARCH/knowledge/prn-prediction-scorecard.md", "research/knowledge/prn-prediction-scorecard.md", ["state-forecasting"]),
)


def load_extractor_module():
    spec = importlib.util.spec_from_file_location("canonical_data_extractor", EXTRACTOR)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load canonical-data extractor")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CanonicalDataExtractionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tempdir.name)
        self.destination = self.workspace / "empty-DATA"
        self.destination.mkdir()
        self.import_root = self.workspace / "legacy-import"
        for legacy_root, canonical_root in HISTORIC_ROOTS:
            source = CANONICAL_ROOT / canonical_root  # V3: canonical tree under data/canonical
            target = self.import_root / Path(legacy_root).relative_to("../HERMES")
            shutil.copytree(source, target)
        for legacy_path, canonical_path, _ in EXPECTED_METHODOLOGY:
            source = CANONICAL_ROOT / canonical_path  # V3
            target = self.import_root / legacy_path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_fresh_explicit_import_copies_bytes_and_validates_without_legacy_root(self) -> None:
        extractor = load_extractor_module()

        extractor.extract_canonical_data(self.destination, self.import_root)

        manifest_path = self.destination / "canonical-data-provenance.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema"], "data.canonical-provenance.v2")
        # 149 = 111 imported originals + 38 observed (baseline backfill sweep 7a19acd, absorbed by refresh 2026-09-26);
        # 162 as of P2.6 = 149 + 13 derived per-state federal-election-results CSVs (P2.6 collection, refresh 2026-09-29T092239Z)
        # 163 as of P2.9 = 162 + research/derived/data-coverage.json (machine-readable coverage manifest)
        self.assertIn(len(manifest["files"]), (149, 162, 163))
        self.assertEqual(len(manifest["methodology_inputs"]), 14)
        self.assertEqual(
            [
                (
                    entry["legacy_source_relative_path"],
                    entry["canonical_path"],
                    entry["consumer_roles"],
                )
                for entry in manifest["methodology_inputs"]
            ],
            list(EXPECTED_METHODOLOGY),
        )
        for entry in manifest["methodology_inputs"]:
            legacy_source = self.import_root / entry["legacy_source_relative_path"]
            canonical_destination = self.destination / entry["canonical_path"]
            self.assertEqual(entry["classification"], "canonical-methodology-input")
            self.assertEqual(entry["bytes"], legacy_source.stat().st_size)
            self.assertEqual(entry["sha256"], sha256(legacy_source))
            self.assertEqual(canonical_destination.read_bytes(), legacy_source.read_bytes())
        for entry in manifest["files"]:
            legacy_source = self.import_root / Path(entry["historic_source_path"]).relative_to("../HERMES")
            canonical_destination = self.destination / entry["destination_path"]
            self.assertEqual(canonical_destination.read_bytes(), legacy_source.read_bytes())

        self.import_root.rename(self.workspace / "legacy-import-unavailable")
        result = subprocess.run(
            [sys.executable, str(VALIDATOR), "--manifest", str(manifest_path)],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_refuses_symlinked_import_ancestor_before_creating_destinations(self) -> None:
        extractor = load_extractor_module()
        source_parent = self.import_root / "01_RESEARCH/data"
        relocated_parent = self.workspace / "relocated-data"
        source_parent.rename(relocated_parent)
        os.symlink(relocated_parent, source_parent)

        with self.assertRaisesRegex(RuntimeError, "source.*ancestor.*symlink"):
            extractor.extract_canonical_data(self.destination, self.import_root)

        self.assertFalse((self.destination / "canonical-data-provenance.json").exists())
        self.assertFalse((self.destination / "research/raw").exists())

    def test_copy_rejects_destination_close_failure(self) -> None:
        extractor = load_extractor_module()
        source = self.workspace / "close-source.bin"
        destination = self.workspace / "close-destination.bin"
        source.write_bytes(b"source bytes")
        real_close = os.close
        close_count = 0

        def close_destination_then_fail(descriptor):
            nonlocal close_count
            close_count += 1
            real_close(descriptor)
            if close_count == 1:
                raise OSError(errno.EIO, "simulated destination close failure")

        with mock.patch.object(
            extractor.os, "close", side_effect=close_destination_then_fail
        ):
            with self.assertRaisesRegex(
                RuntimeError, "cannot close destination file: " + str(destination)
            ):
                extractor._copy_regular_file(source, destination, "historic source file")

        self.assertEqual(close_count, 2)

    def test_crlf_inspection_normalizes_close_failure(self) -> None:
        extractor = load_extractor_module()
        copied_file = self.workspace / "crlf-close.bin"
        copied_file.write_bytes(b"line\r\n")
        real_close = os.close

        def close_then_fail(descriptor):
            real_close(descriptor)
            raise OSError(errno.EIO, "simulated CRLF close failure")

        with mock.patch.object(extractor.os, "close", side_effect=close_then_fail):
            with self.assertRaisesRegex(
                RuntimeError, "cannot close copied destination file: " + str(copied_file)
            ):
                extractor._uses_crlf(copied_file)

    def test_refuses_walk_traversal_error_before_creating_destinations(self) -> None:
        extractor = load_extractor_module()
        offending_path = self.import_root / "01_RESEARCH/data/raw/unreadable"

        def walk_with_error(root, *, topdown, onerror=None, followlinks):
            self.assertTrue(topdown)
            self.assertFalse(followlinks)
            if onerror is not None:
                onerror(OSError(errno.EACCES, "permission denied", offending_path))
            return []

        with mock.patch.object(extractor.os, "walk", side_effect=walk_with_error):
            with self.assertRaisesRegex(RuntimeError, str(offending_path)):
                extractor.extract_canonical_data(self.destination, self.import_root)

        self.assertFalse((self.destination / "canonical-data-provenance.json").exists())
        for _, destination_root in HISTORIC_ROOTS:
            self.assertFalse((self.destination / destination_root).exists())

    def test_rolls_back_all_created_paths_after_copy_failure(self) -> None:
        extractor = load_extractor_module()
        real_copy = extractor._copy_regular_file
        copy_count = 0

        def fail_second_copy(source, destination, label):
            nonlocal copy_count
            copy_count += 1
            if copy_count == 2:
                raise RuntimeError("simulated copy failure")
            return real_copy(source, destination, label)

        with mock.patch.object(
            extractor, "_copy_regular_file", side_effect=fail_second_copy
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated copy failure"):
                extractor.extract_canonical_data(self.destination, self.import_root)

        self.assertGreaterEqual(copy_count, 2)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_manifest_creation_does_not_follow_racing_symlink(self) -> None:
        extractor = load_extractor_module()
        manifest_path = self.destination / "canonical-data-provenance.json"
        victim = self.workspace / "external-victim.json"
        original_victim = b"do not overwrite\n"
        victim.write_bytes(original_victim)
        real_dumps = extractor.json.dumps
        inserted = False

        def dumps_then_insert_symlink(*args, **kwargs):
            nonlocal inserted
            payload = real_dumps(*args, **kwargs)
            if not inserted:
                os.symlink(victim, manifest_path)
                inserted = True
            return payload

        with mock.patch.object(
            extractor.json, "dumps", side_effect=dumps_then_insert_symlink
        ):
            with self.assertRaisesRegex(RuntimeError, "destination manifest"):
                extractor.extract_canonical_data(self.destination, self.import_root)

        self.assertTrue(inserted)
        self.assertEqual(victim.read_bytes(), original_victim)
        for _, destination_root in HISTORIC_ROOTS:
            self.assertFalse((self.destination / destination_root).exists())


if __name__ == "__main__":
    unittest.main()
