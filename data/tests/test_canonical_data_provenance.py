import hashlib
import importlib.util
import json
import os
import shutil
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]  # V3: data/
CANONICAL_ROOT = REPOSITORY_ROOT / "canonical"  # V3: the canonical tree
VALIDATOR = REPOSITORY_ROOT / "scripts" / "validate_canonical_data.py"
REFRESHER = REPOSITORY_ROOT / "scripts" / "refresh_canonical_data.py"
MANIFEST = CANONICAL_ROOT / "canonical-data-provenance.json"  # V3
GITATTRIBUTES = CANONICAL_ROOT / ".gitattributes"  # V3
HISTORIC_ROOTS = (
    ("../HERMES/01_RESEARCH/data/raw", "research/raw"),
    ("../HERMES/01_RESEARCH/data/derived", "research/derived"),
    ("../HERMES/01_RESEARCH/data/trackers", "research/trackers"),
    ("../HERMES/01_RESEARCH/federal", "research/federal"),
    ("../HERMES/01_RESEARCH/geo", "geo"),
    ("../HERMES/01_RESEARCH/states", "research/states"),
)
ROOTS = tuple(destination_root for _, destination_root in HISTORIC_ROOTS)
SNAPSHOT_COMMIT = "6ce692d29bc3831a4cf72dba996f6ee0a3f2a98c"
STATE_SCENARIO_PATHS = (
    "research/states/DUN Melaka/melaka-state-scenarios.json",
    "research/states/DUN Pahang/pahang-state-scenarios.json",
    "research/states/DUN Perak/perak-state-scenarios.json",
    "research/states/DUN Perlis/perlis-state-scenarios.json",
    "research/states/DUN Sarawak/sarawak-state-scenarios.json",
)
METHODOLOGY_INPUT_FIELDS = {
    "canonical_path",
    "legacy_source_relative_path",
    "sha256",
    "bytes",
    "classification",
    "consumer_roles",
}
METHODOLOGY_INPUTS = (
    ("01_RESEARCH/data/notes/byelections-malaysia-1957-2026.md", "research/data/notes/byelections-malaysia-1957-2026.md"),
    ("01_RESEARCH/data/notes/election-study-organizations-malaysia.md", "research/data/notes/election-study-organizations-malaysia.md"),
    ("01_RESEARCH/data/notes/ge15-candidates-demographics.md", "research/data/notes/ge15-candidates-demographics.md"),
    ("01_RESEARCH/data/notes/ge15-results-by-state.md", "research/data/notes/ge15-results-by-state.md"),
    ("01_RESEARCH/data/notes/marginal-seats-ge15.md", "research/data/notes/marginal-seats-ge15.md"),
    ("01_RESEARCH/data/notes/parliamentary-seats-malaysia-data.md", "research/data/notes/parliamentary-seats-malaysia-data.md"),
    ("01_RESEARCH/data/notes/party-landscape-update-2026.md", "research/data/notes/party-landscape-update-2026.md"),
    ("01_RESEARCH/data/notes/voter-demographics-by-constituency-ge15.md", "research/data/notes/voter-demographics-by-constituency-ge15.md"),
    ("01_RESEARCH/knowledge/anti-hopping-law-factor.md", "research/knowledge/anti-hopping-law-factor.md"),
    ("01_RESEARCH/knowledge/demographics-parties-analysis.md", "research/knowledge/demographics-parties-analysis.md"),
    ("01_RESEARCH/knowledge/forecast-factor-rankings.md", "research/knowledge/forecast-factor-rankings.md"),
    ("01_RESEARCH/knowledge/forecast-theory.md", "research/knowledge/forecast-theory.md"),
    ("01_RESEARCH/knowledge/political-parties.md", "research/knowledge/political-parties.md"),
    ("01_RESEARCH/knowledge/prn-prediction-scorecard.md", "research/knowledge/prn-prediction-scorecard.md"),
)
METHODOLOGY_CONSUMER_ROLES = {
    "01_RESEARCH/data/notes/byelections-malaysia-1957-2026.md": ["historical-baseline", "forecast-modeling"],
    "01_RESEARCH/data/notes/election-study-organizations-malaysia.md": ["research-methodology"],
    "01_RESEARCH/data/notes/ge15-candidates-demographics.md": ["demographic-analysis"],
    "01_RESEARCH/data/notes/ge15-results-by-state.md": ["results-analysis"],
    "01_RESEARCH/data/notes/marginal-seats-ge15.md": ["seat-prioritization"],
    "01_RESEARCH/data/notes/parliamentary-seats-malaysia-data.md": ["constituency-reference"],
    "01_RESEARCH/data/notes/party-landscape-update-2026.md": ["party-analysis", "forecast-modeling"],
    "01_RESEARCH/data/notes/voter-demographics-by-constituency-ge15.md": ["demographic-analysis"],
    "01_RESEARCH/knowledge/anti-hopping-law-factor.md": ["institutional-analysis"],
    "01_RESEARCH/knowledge/demographics-parties-analysis.md": ["demographic-analysis", "party-analysis"],
    "01_RESEARCH/knowledge/forecast-factor-rankings.md": ["forecast-modeling"],
    "01_RESEARCH/knowledge/forecast-theory.md": ["forecast-modeling"],
    "01_RESEARCH/knowledge/political-parties.md": ["party-analysis"],
    "01_RESEARCH/knowledge/prn-prediction-scorecard.md": ["state-forecasting"],
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def methodology_entries(base: Path) -> list[dict]:
    entries = []
    for legacy_path, canonical_path in METHODOLOGY_INPUTS:
        source = base / "../HERMES" / legacy_path
        entries.append(
            {
                "canonical_path": canonical_path,
                "legacy_source_relative_path": legacy_path,
                "sha256": sha256(source),
                "bytes": source.stat().st_size,
                "classification": "canonical-methodology-input",
                "consumer_roles": METHODOLOGY_CONSUMER_ROLES[legacy_path],
            }
        )
    return entries


def run_validator(manifest: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VALIDATOR), "--manifest", str(manifest)],
        text=True,
        capture_output=True,
        check=False,
    )


def run_import_audit(manifest: Path, legacy_source_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "--manifest",
            str(manifest),
            "--import-audit-root",
            str(legacy_source_root),
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def load_validator_module():
    spec = importlib.util.spec_from_file_location("canonical_data_validator", VALIDATOR)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load canonical-data validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_refresher_module():
    spec = importlib.util.spec_from_file_location("canonical_data_refresher", REFRESHER)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load canonical-data refresher")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_csv_line_endings(manifest: dict) -> tuple[set[str], set[str]]:
    crlf_paths: set[str] = set()
    non_crlf_paths: set[str] = set()
    for entry in manifest["files"]:
        destination_path = entry["destination_path"]
        if not destination_path.endswith(".csv"):
            continue
        source_bytes = (CANONICAL_ROOT / entry["destination_path"]).read_bytes()  # V3
        if b"\r\n" in source_bytes and source_bytes.count(b"\r\n") == source_bytes.count(b"\n"):
            crlf_paths.add(destination_path)
        else:
            non_crlf_paths.add(destination_path)
    return crlf_paths, non_crlf_paths


def crlf_attribute_paths() -> set[str]:
    paths: set[str] = set()
    for line in GITATTRIBUTES.read_text(encoding="utf-8").splitlines():
        fields = shlex.split(line, comments=True)
        if len(fields) == 2 and fields[1] == "whitespace=cr-at-eol":
            paths.add(fields[0])
    return paths


class CanonicalDataProvenanceTests(unittest.TestCase):
    def test_validator_import_does_not_require_pep_604_unions(self) -> None:
        probe = """
import sys
from pathlib import Path

source = Path(sys.argv[1]).read_text(encoding="utf-8")
source = source.replace(
    "from pathlib import Path, PurePosixPath",
    '''class _NoPep604Meta(type):
    def __or__(cls, other):
        raise TypeError("PEP 604 unions are unavailable")

class Path(metaclass=_NoPep604Meta):
    pass

class PurePosixPath:
    pass''',
)
exec(compile(source, sys.argv[1], "exec"), {"__name__": "validator_compat_probe"})
"""
        result = subprocess.run(
            ["python3", "-c", probe, str(VALIDATOR)],
            cwd=REPOSITORY_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name) / "1_DATA"
        self.base.mkdir()
        for source_root, destination_root in HISTORIC_ROOTS:
            (self.base / source_root).mkdir(parents=True)
            (self.base / destination_root).mkdir(parents=True)
        self.source = self.base / "../HERMES/01_RESEARCH/data/raw/nested/data.bin"
        self.destination = self.base / "research/raw/nested/data.bin"
        self.source.parent.mkdir()
        self.destination.parent.mkdir()
        self.source.write_bytes(b"canonical\x00data\n")
        shutil.copyfile(self.source, self.destination)
        for legacy_path, canonical_path in METHODOLOGY_INPUTS:
            methodology_source = self.base / "../HERMES" / legacy_path
            methodology_destination = self.base / canonical_path
            methodology_source.parent.mkdir(parents=True, exist_ok=True)
            methodology_destination.parent.mkdir(parents=True, exist_ok=True)
            methodology_source.write_bytes(
                b"methodology input: " + legacy_path.encode("utf-8") + b"\n"
            )
            shutil.copyfile(methodology_source, methodology_destination)
        self.manifest = self.base / "provenance.json"
        self.write_manifest()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write_manifest(self) -> None:
        payload = {
            "schema": "data.canonical-provenance.v2",
            "root_set": "hermes-phase-1.1-canonical-data",
            "snapshot_commit": SNAPSHOT_COMMIT,
            "format_exceptions": {
                "byte_preservation_required": True,
                "documentation": (
                    "Source-preserved whitespace is intentional; canonical source and "
                    "destination bytes must remain byte-identical."
                ),
                "entries": [],
            },
            "historic_import": {
                "source_repository": "HERMES",
                "source_commit": SNAPSHOT_COMMIT,
                "roots": [
                    {"source_root": source_root, "destination_root": destination_root}
                    for source_root, destination_root in HISTORIC_ROOTS
                ],
            },
            "roots": list(ROOTS),
            "methodology_inputs": methodology_entries(self.base),
            "files": [
                {
                    "historic_source_path": "../HERMES/01_RESEARCH/data/raw/nested/data.bin",
                    "destination_path": "research/raw/nested/data.bin",
                    "bytes": self.source.stat().st_size,
                    "sha256": sha256(self.source),
                }
            ],
        }
        self.manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def test_accepts_matching_manifest_and_trees(self) -> None:
        result = run_validator(self.manifest)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_current_collector_provenance_validates_without_fake_import_path(self):
        relative = "research/trackers/ge16-news-feed.json"
        path = self.base / relative
        path.write_bytes(b"[]\n")
        payload = json.loads(self.manifest.read_text())
        entry = {
            "destination_path": relative, "bytes": 3, "sha256": sha256(path),
            "historic_source_path": "collector:scripts/collect/track_ge16_news.py#" + relative,
        }
        payload["files"].append(entry)
        self.manifest.write_text(json.dumps(payload))
        result = run_validator(self.manifest)
        self.assertEqual(0, result.returncode, result.stderr)
        for invalid in (
            "collector:scripts/collect/track_ge16_polls.py#" + relative,
            "collector:../../evil.py#" + relative,
            "observed:scripts/refresh_canonical_data.py#" + relative,
        ):
            entry["historic_source_path"] = invalid
            self.manifest.write_text(json.dumps(payload))
            self.assertNotEqual(0, run_validator(self.manifest).returncode)

    def test_rejects_symlinked_manifest(self) -> None:
        real_manifest = self.base / "real-provenance.json"
        self.manifest.rename(real_manifest)
        os.symlink(real_manifest, self.manifest)

        result = run_validator(self.manifest)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("manifest is a symlink", result.stderr)

    def test_rejects_open_top_level_manifest_schema(self) -> None:
        original = json.loads(self.manifest.read_text(encoding="utf-8"))
        cases = (
            ({**original, "unexpected": True}, "unexpected top-level fields"),
            (
                {key: value for key, value in original.items() if key != "format_exceptions"},
                "unexpected top-level fields",
            ),
        )
        for payload, message in cases:
            with self.subTest(fields=sorted(payload)):
                self.manifest.write_text(
                    json.dumps(payload, indent=2) + "\n", encoding="utf-8"
                )
                result = run_validator(self.manifest)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)

    def test_rejects_internal_historic_source_traversal(self) -> None:
        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        payload["files"][0]["historic_source_path"] = (
            "../HERMES/01_RESEARCH/data/raw/nested/../nested/data.bin"
        )
        self.manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

        result = run_validator(self.manifest)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("historic_source_path", result.stderr)

    def test_rejects_extra_destination_file(self) -> None:
        (self.base / "research/raw/unexpected.txt").write_text("no", encoding="utf-8")
        result = run_validator(self.manifest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("destination tree differs from manifest", result.stderr)

    def test_rejects_missing_destination_file(self) -> None:
        self.destination.unlink()
        result = run_validator(self.manifest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("destination tree differs from manifest", result.stderr)

    def test_rejects_wrong_hash(self) -> None:
        self.destination.write_bytes(b"corrupted\x00data\n")
        result = run_validator(self.manifest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("hash mismatch", result.stderr)

    def test_repository_manifest_validates(self) -> None:
        result = run_validator(MANIFEST)
        self.assertEqual(result.returncode, 0, result.stderr)

    def copy_repository_for_methodology_test(self) -> Path:
        clone = self.base / "repository-copy"
        # V3: clone the whole canonical tree (research incl. data/notes + knowledge,
        # geo, manifest, .gitattributes) so the refresh module sees the same shape
        # as the real data/canonical directory.
        shutil.copytree(CANONICAL_ROOT, clone, ignore=shutil.ignore_patterns("__pycache__", ".DS_Store"))  # V3
        return clone

    def test_repository_preserves_original_file_provenance_records(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["files"]), 149)
        originals = [
            entry for entry in manifest["files"]
            if entry["historic_source_path"].startswith("../HERMES/")
        ]
        self.assertEqual(len(originals), 111)
        semantic_hash = hashlib.sha256(
            json.dumps(
                [
                    (entry["destination_path"], entry["historic_source_path"])
                    for entry in originals
                ],
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(
            semantic_hash,
            # Updated Round 4: the three tracker files were union-merged from the
            # 2_ANALYTICS shadow copies (disjoint captures, evidenced in
            # OPS/migration/evidence/ge16-round2-tracker-reconciliation.txt).
            # 2026-09-26 refresh (pipeline step refresh_canonical_data.py) absorbed
            # 38 observed: entries from the 2026-09-24 baseline backfill sweep
            # (commit 7a19acd); original-import path provenance is unchanged
            # (digest pinned); 8 living tracker logs were re-fingerprinted by design.
            # Evidence: OPS/migration/evidence/ge16-canonical-provenance-149-refresh.txt.
            "d479fa54fcd506797f5cce2022991bd7d8b0cffca93d8e5aacd0ac35222e7d95",
        )

    def test_refresh_preserves_all_111_original_historic_source_paths(self) -> None:
        # Freeze the original path provenance independently of refreshed bytes.
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        # Exclude additive observed: entries; only original HERMES imports are pinned.
        paths = [(entry["destination_path"], entry["historic_source_path"])
                 for entry in manifest["files"]
                 if entry["historic_source_path"].startswith("../HERMES/")]
        digest = hashlib.sha256(json.dumps(
            paths, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        ).encode("utf-8")).hexdigest()
        self.assertEqual(digest, "d479fa54fcd506797f5cce2022991bd7d8b0cffca93d8e5aacd0ac35222e7d95")

    def test_repository_declares_exact_methodology_input_set(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        entries = manifest["methodology_inputs"]
        self.assertEqual(
            {
                (entry["legacy_source_relative_path"], entry["canonical_path"])
                for entry in entries
            },
            set(METHODOLOGY_INPUTS),
        )
        for entry in entries:
            with self.subTest(path=entry["canonical_path"]):
                self.assertEqual(set(entry), METHODOLOGY_INPUT_FIELDS)
                self.assertEqual(entry["classification"], "canonical-methodology-input")
                self.assertIsInstance(entry["consumer_roles"], list)
                self.assertTrue(entry["consumer_roles"])
                self.assertTrue(all(isinstance(role, str) and role for role in entry["consumer_roles"]))
                self.assertFalse(entry["legacy_source_relative_path"].startswith("/"))
                self.assertNotIn("..", Path(entry["legacy_source_relative_path"]).parts)

    def test_ordinary_suite_runs_without_a_sibling_hermes_repository(self) -> None:
        if os.environ.get("DATA_NO_HERMES_COPY_PROOF"):
            self.skipTest("prevent recursive temporary-copy suite invocation")
        copy_parent = Path(self.tempdir.name) / "isolated-parent"
        # V3: the data tree is <root>/data and resolves its repository root
        # through the monorepo marker files beside it (v3_paths.py walks up to
        # requirements.txt), so the isolated copy reproduces that shape.
        copy_root = copy_parent / "data"
        shutil.copytree(
            REPOSITORY_ROOT,
            copy_root,
            ignore=shutil.ignore_patterns(".git", "__pycache__", ".DS_Store"),
        )
        for marker in ("v3_paths.py", "requirements.txt"):
            shutil.copyfile(REPOSITORY_ROOT.parent / marker, copy_parent / marker)
        self.assertFalse((copy_parent / "HERMES").exists())
        init_result = subprocess.run(
            ["git", "init", "-q"],
            cwd=copy_root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(init_result.returncode, 0, init_result.stderr)
        environment = os.environ.copy()
        environment["DATA_NO_HERMES_COPY_PROOF"] = "1"
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            cwd=copy_root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_symlinked_historic_destination_root(self) -> None:
        destination_root = self.base / "research/raw"
        relocated_root = self.base / "relocated-raw"
        destination_root.rename(relocated_root)
        os.symlink(relocated_root, destination_root)
        result = run_validator(self.manifest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("destination tree root is a symlink", result.stderr)

    def test_rejects_symlinked_destination_ancestor_above_selected_root(self) -> None:
        destination_ancestor = self.base / "research"
        relocated_ancestor = self.base / "relocated-research"
        destination_ancestor.rename(relocated_ancestor)
        os.symlink(relocated_ancestor, destination_ancestor)

        result = run_validator(self.manifest)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "destination tree ancestor is a symlink: " + str(destination_ancestor),
            result.stderr,
        )

    def test_rejects_destination_ancestor_swapped_after_tree_scan(self) -> None:
        validator = load_validator_module()
        destination_ancestor = self.base / "research"
        relocated_ancestor = self.base / "relocated-research"
        real_fingerprint = validator.fingerprint_file
        swapped = False

        def fingerprint_after_swap(path, label="validated file"):
            nonlocal swapped
            if not swapped:
                destination_ancestor.rename(relocated_ancestor)
                os.symlink(relocated_ancestor, destination_ancestor)
                swapped = True
            return real_fingerprint(path, label)

        with mock.patch.object(
            validator, "fingerprint_file", side_effect=fingerprint_after_swap
        ):
            with self.assertRaisesRegex(
                validator.ValidationError,
                "destination file parent is a symlink: " + str(destination_ancestor),
            ):
                validator.validate(self.manifest)
        self.assertTrue(swapped)

    def test_rejects_symlinked_methodology_destination_root(self) -> None:
        destination_root = self.base / "research/knowledge"
        relocated_root = self.base / "relocated-knowledge"
        destination_root.rename(relocated_root)
        os.symlink(relocated_root, destination_root)
        result = run_validator(self.manifest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("methodology destination tree root is a symlink", result.stderr)

    def test_rejects_missing_declared_methodology_record(self) -> None:
        clone = self.copy_repository_for_methodology_test()
        manifest = clone / "canonical-data-provenance.json"
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["methodology_inputs"].pop()
        manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        result = run_validator(manifest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("methodology declared set", result.stderr)

    def test_rejects_missing_canonical_methodology_input(self) -> None:
        clone = self.copy_repository_for_methodology_test()
        manifest = clone / "canonical-data-provenance.json"
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        (clone / payload["methodology_inputs"][0]["canonical_path"]).unlink()
        result = run_validator(manifest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("methodology destination tree differs from manifest", result.stderr)

    def test_rejects_tampered_canonical_methodology_input(self) -> None:
        clone = self.copy_repository_for_methodology_test()
        manifest = clone / "canonical-data-provenance.json"
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        destination = clone / payload["methodology_inputs"][0]["canonical_path"]
        destination.write_bytes(b"x" * payload["methodology_inputs"][0]["bytes"])
        result = run_validator(manifest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("methodology hash mismatch", result.stderr)

    def test_rejects_wrong_canonical_methodology_byte_size(self) -> None:
        clone = self.copy_repository_for_methodology_test()
        manifest = clone / "canonical-data-provenance.json"
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        destination = clone / payload["methodology_inputs"][0]["canonical_path"]
        destination.write_bytes(b"short\n")
        result = run_validator(manifest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("methodology byte-size mismatch", result.stderr)

    def test_rejects_symlinked_canonical_methodology_input(self) -> None:
        clone = self.copy_repository_for_methodology_test()
        manifest = clone / "canonical-data-provenance.json"
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        destination = clone / payload["methodology_inputs"][0]["canonical_path"]
        destination.unlink()
        os.symlink("/dev/null", destination)
        result = run_validator(manifest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink", result.stderr)

    def test_rejects_malformed_methodology_provenance(self) -> None:
        clone = self.copy_repository_for_methodology_test()
        manifest = clone / "canonical-data-provenance.json"
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["methodology_inputs"][0]["consumer_roles"] = "forecast-modeling"
        manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        result = run_validator(manifest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("methodology", result.stderr)

    def test_rejects_extra_canonical_methodology_input(self) -> None:
        clone = self.copy_repository_for_methodology_test()
        extra = clone / "research/data/notes/unexpected-methodology-input.md"
        extra.write_text("unexpected\n", encoding="utf-8")
        result = run_validator(clone / "canonical-data-provenance.json")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("methodology destination tree differs from manifest", result.stderr)

    def test_rejects_unsafe_methodology_path(self) -> None:
        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        payload["methodology_inputs"][0]["canonical_path"] = "research/data/notes/../escape.md"
        self.manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        result = run_validator(self.manifest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must not contain '..'", result.stderr)

    def test_import_audit_rejects_symlinked_methodology_source(self) -> None:
        legacy_path, _ = METHODOLOGY_INPUTS[0]
        source = self.base / "../HERMES" / legacy_path
        source.unlink()
        os.symlink("/dev/null", source)
        result = run_import_audit(self.manifest, self.base.parent / "HERMES")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("methodology import audit source is a symlink", result.stderr)

    def test_import_audit_rejects_symlinked_methodology_source_parent(self) -> None:
        source_parent = self.base / "../HERMES/01_RESEARCH/knowledge"
        relocated_parent = self.base / "relocated-source-knowledge"
        source_parent.rename(relocated_parent)
        os.symlink(relocated_parent, source_parent)
        result = run_import_audit(self.manifest, self.base.parent / "HERMES")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("methodology import audit source parent is a symlink", result.stderr)

    def test_import_audit_rejects_symlinked_historic_source_ancestor(self) -> None:
        source_ancestor = self.base.parent / "HERMES/01_RESEARCH/data"
        relocated_ancestor = self.base / "relocated-source-data"
        source_ancestor.rename(relocated_ancestor)
        os.symlink(relocated_ancestor, source_ancestor)

        result = run_import_audit(self.manifest, self.base.parent / "HERMES")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "import audit source tree ancestor is a symlink: " + str(source_ancestor),
            result.stderr,
        )

    def test_rejects_boolean_byte_counts(self) -> None:
        original = self.manifest.read_text(encoding="utf-8")
        cases = (
            ("files", "bytes", "invalid canonical byte size"),
            ("files", "import_bytes", "invalid import byte size"),
            ("methodology_inputs", "bytes", "invalid methodology byte size"),
        )
        for collection, field, message in cases:
            with self.subTest(collection=collection, field=field):
                payload = json.loads(original)
                entry = payload[collection][0]
                if field.startswith("import_"):
                    entry["import_bytes"] = self.source.stat().st_size
                    entry["import_sha256"] = sha256(self.source)
                entry[field] = True
                self.manifest.write_text(
                    json.dumps(payload, indent=2) + "\n", encoding="utf-8"
                )
                result = run_validator(self.manifest)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)

    def test_rejects_noncanonical_sha256_strings(self) -> None:
        original = self.manifest.read_text(encoding="utf-8")
        cases = (
            ("files", "sha256", "A" * 64, "invalid canonical SHA-256"),
            ("files", "sha256", "g" * 64, "invalid canonical SHA-256"),
            ("files", "import_sha256", "A" * 64, "invalid import SHA-256"),
            ("files", "import_sha256", "g" * 64, "invalid import SHA-256"),
            ("methodology_inputs", "sha256", "A" * 64, "invalid methodology SHA-256"),
            ("methodology_inputs", "sha256", "g" * 64, "invalid methodology SHA-256"),
        )
        for collection, field, value, message in cases:
            with self.subTest(collection=collection, field=field, value=value[:1]):
                payload = json.loads(original)
                entry = payload[collection][0]
                if field.startswith("import_"):
                    entry["import_bytes"] = self.source.stat().st_size
                    entry["import_sha256"] = sha256(self.source)
                entry[field] = value
                self.manifest.write_text(
                    json.dumps(payload, indent=2) + "\n", encoding="utf-8"
                )
                result = run_validator(self.manifest)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)

    def test_invalid_methodology_size_and_hash_errors_name_the_path(self) -> None:
        original = self.manifest.read_text(encoding="utf-8")
        for field, value, message in (
            ("bytes", True, "invalid methodology byte size"),
            ("sha256", "A" * 64, "invalid methodology SHA-256"),
        ):
            with self.subTest(field=field):
                payload = json.loads(original)
                entry = payload["methodology_inputs"][0]
                path = entry["canonical_path"]
                entry[field] = value
                self.manifest.write_text(
                    json.dumps(payload, indent=2) + "\n", encoding="utf-8"
                )
                result = run_validator(self.manifest)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertIn(path, result.stderr)

    def test_fingerprint_rejects_final_component_symlink(self) -> None:
        validator = load_validator_module()
        target = self.base / "matching-content.bin"
        target.write_bytes(self.destination.read_bytes())
        self.destination.unlink()
        os.symlink(target, self.destination)
        with self.assertRaisesRegex(validator.ValidationError, str(self.destination)):
            validator.fingerprint_file(self.destination)

    def test_fingerprint_uses_binary_open_flag_when_available(self) -> None:
        validator = load_validator_module()
        binary_flag = 1 << 30
        real_open = validator.os.open
        seen_flags: list[int] = []

        def binary_open(path: str, flags: int, *args: object) -> int:
            seen_flags.append(flags)
            self.assertTrue(flags & binary_flag)
            return real_open(path, flags & ~binary_flag, *args)

        with mock.patch.object(validator.os, "O_BINARY", binary_flag, create=True), mock.patch.object(
            validator.os, "open", side_effect=binary_open
        ):
            self.assertEqual(
                validator.fingerprint_file(self.destination),
                (self.destination.stat().st_size, sha256(self.destination)),
            )
        self.assertEqual(len(seen_flags), 1)

    def test_fingerprint_converts_concurrent_change_to_validation_error(self) -> None:
        validator = load_validator_module()
        before = self.destination.stat()
        changed = list(before)
        changed[8] += 1
        after = os.stat_result(changed)
        with mock.patch.object(validator.os, "fstat", side_effect=(before, after)):
            with self.assertRaisesRegex(validator.ValidationError, str(self.destination)):
                validator.fingerprint_file(self.destination)

    def test_fingerprint_rejects_path_replaced_while_original_is_open(self) -> None:
        validator = load_validator_module()
        replacement = self.base / "replacement.bin"
        replacement.write_bytes(b"replacement canonical data\n")
        original_read = validator.os.read
        replaced = False

        def read_then_replace(descriptor: int, size: int) -> bytes:
            nonlocal replaced
            chunk = original_read(descriptor, size)
            if not replaced:
                os.replace(replacement, self.destination)
                replaced = True
            return chunk

        with mock.patch.object(validator.os, "read", side_effect=read_then_replace):
            with self.assertRaisesRegex(
                validator.ValidationError,
                "validated file changed while being fingerprinted: "
                + str(self.destination),
            ):
                validator.fingerprint_file(self.destination)
        self.assertTrue(replaced)

    def test_fingerprint_preserves_concurrent_change_error_when_close_fails(self) -> None:
        validator = load_validator_module()
        before = self.destination.stat()
        changed = list(before)
        changed[8] += 1
        after = os.stat_result(changed)
        original_close = validator.os.close

        def close_then_fail(descriptor: int) -> None:
            original_close(descriptor)
            raise OSError("close failed")

        with mock.patch.object(
            validator.os, "fstat", side_effect=(before, after)
        ), mock.patch.object(validator.os, "close", side_effect=close_then_fail) as close:
            with self.assertRaisesRegex(
                validator.ValidationError,
                "validated file changed while being fingerprinted: "
                + str(self.destination),
            ):
                validator.fingerprint_file(self.destination)
        self.assertEqual(close.call_count, 1)

    def test_fingerprint_close_failure_is_a_pathful_validation_error(self) -> None:
        validator = load_validator_module()
        original_close = validator.os.close

        def close_then_fail(descriptor: int) -> None:
            original_close(descriptor)
            raise OSError("close failed")

        with mock.patch.object(validator.os, "close", side_effect=close_then_fail) as close:
            with self.assertRaisesRegex(
                validator.ValidationError,
                "cannot close validated file: " + str(self.destination),
            ):
                validator.fingerprint_file(self.destination)
        self.assertEqual(close.call_count, 1)

    def test_fingerprint_preserves_primary_validation_error_when_close_fails(self) -> None:
        validator = load_validator_module()
        original_close = validator.os.close

        def close_then_fail(descriptor: int) -> None:
            original_close(descriptor)
            raise OSError("close failed")

        with mock.patch.object(
            validator.os, "fstat", side_effect=OSError("fstat failed")
        ), mock.patch.object(validator.os, "close", side_effect=close_then_fail) as close:
            with self.assertRaisesRegex(
                validator.ValidationError,
                "cannot fingerprint validated file: " + str(self.destination),
            ):
                validator.fingerprint_file(self.destination)
        self.assertEqual(close.call_count, 1)

    def test_state_scenario_evidence_is_data_content_bound(self) -> None:
        evidence_count = 0
        for scenario_path in STATE_SCENARIO_PATHS:
            payload = json.loads((CANONICAL_ROOT / scenario_path).read_text(encoding="utf-8"))
            expected_path = str(Path(scenario_path).parent / "dun-election-results-latest.csv")
            for scenario in payload["scenarios"]:
                for evidence in scenario["provenance"]["evidence"]:
                    evidence_count += 1
                    with self.subTest(scenario_path=scenario_path, scenario=scenario["scenario"]):
                        self.assertEqual(set(evidence), {"repository", "path", "sha256"})
                        self.assertEqual(evidence["repository"], "DATA")
                        self.assertEqual(evidence["path"], expected_path)
                        self.assertRegex(evidence["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(evidence_count, 20)

    def test_normal_validation_is_self_contained_and_import_audit_is_explicit(self) -> None:
        legacy_repository = self.base.parent / "HERMES"
        renamed_legacy_repository = self.base.parent / "2_ANALYTICS"
        audit_manifest = self.base / "audit-provenance.json"
        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        audit_manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        legacy_repository.rename(renamed_legacy_repository)

        normal_result = run_validator(audit_manifest)
        self.assertEqual(normal_result.returncode, 0, normal_result.stderr)

        audit_result = run_import_audit(audit_manifest, renamed_legacy_repository)
        self.assertEqual(audit_result.returncode, 0, audit_result.stderr)

    def test_import_audit_tolerates_unrelated_methodology_source_files(self) -> None:
        legacy_repository = self.base.parent / "HERMES"
        (legacy_repository / "01_RESEARCH/data/notes/legacy-note.md").write_text(
            "not an approved import\n", encoding="utf-8"
        )
        (legacy_repository / "01_RESEARCH/knowledge/legacy-reference.md").write_text(
            "not an approved import\n", encoding="utf-8"
        )

        self.assertEqual(len(METHODOLOGY_INPUTS), 14)
        result = run_import_audit(self.manifest, legacy_repository)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_post_import_revision_checks_current_and_import_hashes_separately(self) -> None:
        self.destination.write_bytes(b"repaired canonical data\n")
        payload = {
            "schema": "data.canonical-provenance.v2",
            "root_set": "hermes-phase-1.1-canonical-data",
            "snapshot_commit": SNAPSHOT_COMMIT,
            "format_exceptions": {
                "byte_preservation_required": True,
                "documentation": (
                    "Source-preserved whitespace is intentional; canonical source and "
                    "destination bytes must remain byte-identical."
                ),
                "entries": [],
            },
            "historic_import": {
                "source_repository": "HERMES",
                "source_commit": SNAPSHOT_COMMIT,
                "roots": [
                    {"source_root": source_root, "destination_root": destination_root}
                    for source_root, destination_root in HISTORIC_ROOTS
                ],
            },
            "roots": list(ROOTS),
            "methodology_inputs": methodology_entries(self.base),
            "files": [
                {
                    "historic_source_path": "../HERMES/01_RESEARCH/data/raw/nested/data.bin",
                    "destination_path": "research/raw/nested/data.bin",
                    "bytes": self.destination.stat().st_size,
                    "sha256": sha256(self.destination),
                    "import_bytes": self.source.stat().st_size,
                    "import_sha256": sha256(self.source),
                }
            ],
        }
        self.manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

        self.assertEqual(run_validator(self.manifest).returncode, 0)
        self.assertEqual(run_import_audit(self.manifest, self.base.parent / "HERMES").returncode, 0)

        self.destination.write_bytes(b"tampered canonical data\n")
        result = run_validator(self.manifest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("hash mismatch", result.stderr)

        self.destination.write_bytes(b"repaired canonical data\n")
        self.source.write_bytes(b"tampered imported data\n")
        result = run_import_audit(self.manifest, self.base.parent / "HERMES")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("import audit", result.stderr)

    def test_rejects_incomplete_or_invalid_import_provenance(self) -> None:
        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        entry = payload["files"][0]
        entry["import_bytes"] = self.source.stat().st_size
        self.manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

        result = run_validator(self.manifest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("import provenance fields", result.stderr)

    def test_source_preserved_whitespace_exceptions_are_declared(self) -> None:
        attributes = GITATTRIBUTES.read_text(encoding="utf-8")
        self.assertNotIn("research/**/*.csv whitespace=cr-at-eol", attributes)
        self.assertIn(
            "research/derived/ge16-per-seat-flip-narratives.md whitespace=-blank-at-eof",
            attributes,
        )

        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertTrue(manifest["format_exceptions"]["byte_preservation_required"])
        self.assertEqual(
            manifest["format_exceptions"]["documentation"],
            "Source-preserved whitespace is intentional; canonical source and destination "
            "bytes must remain byte-identical.",
        )
        self.assertIn(
            {
                "destination_path": "research/derived/ge16-per-seat-flip-narratives.md",
                "source_preserved": "blank-at-EOF",
            },
            manifest["format_exceptions"]["entries"],
        )

        result = run_validator(MANIFEST)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_crlf_exceptions_match_manifest_source_bytes_exactly(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        crlf_paths, non_crlf_paths = source_csv_line_endings(manifest)
        manifest_crlf_paths = {
            entry["destination_path"]
            for entry in manifest["format_exceptions"]["entries"]
            if entry["source_preserved"] == "CRLF"
        }

        self.assertEqual(len(crlf_paths), 17)
        self.assertEqual(manifest_crlf_paths, crlf_paths)
        self.assertEqual(crlf_attribute_paths(), crlf_paths)
        self.assertFalse(manifest_crlf_paths & non_crlf_paths)

    def test_python_cache_files_are_ignored_and_not_untracked(self) -> None:
        gitignore = CANONICAL_ROOT / ".gitignore"
        self.assertIn("__pycache__/", gitignore.read_text(encoding="utf-8"))
        # V3 has no git repository yet: hold the canonical tree in an isolated
        # repository (as the V2 DATA repository did) and require the ignore rule
        # to hide bytecode litter there.
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "DATA-repository"
            shutil.copytree(
                CANONICAL_ROOT,
                repository,
                ignore=shutil.ignore_patterns("__pycache__", ".DS_Store"),
            )
            init_result = subprocess.run(
                ["git", "init", "-q"],
                cwd=repository,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(init_result.returncode, 0, init_result.stderr)
            # The litter a run without PYTHONDONTWRITEBYTECODE leaves behind.
            litter = repository / "research" / "__pycache__"
            litter.mkdir()
            (litter / "collector.cpython-312.pyc").write_bytes(b"\x00")
            result = subprocess.run(
                ["git", "status", "--short", "--untracked-files=all"],
                cwd=repository,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("__pycache__", result.stdout)


if __name__ == "__main__":
    unittest.main()
