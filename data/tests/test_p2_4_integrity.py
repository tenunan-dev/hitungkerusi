"""P2.4 integrity-verifier tests (design brief evidence/P2/P2.4-design-brief.md §4).

Every test runs against a tmp copy of the live canonical tree — the live
tree is never touched. Each class injects a real defect into its copy and
asserts the specific subcommand detects it with the brief's exit-code
contract (0 clean / 1 corrupt / 2 lost; worst wins):

  1. TestCleanTree            clean tmp copy: all subcommands exit 0, JSON parses
  2. TestCorruptByte          one corrupted byte -> MISMATCH/corrupt, exit 1
  3. TestLostShard            deleted file -> LOST, exit 2
  4. TestUnrecordedAddition   appended row no edition records -> exit 1
  5. TestLegitimateSupersedes supersedes row + fresh edition -> passes
  6. TestBrokenChain          prior_edition tampering -> detected
  7. TestSamplingVsFull       seeded sampling vs full coverage
  8. TestVerifierWritesNothing byte-identity + AST pin: zero write calls
  9. TestRefreshRunModeGate   the refresh run-mode wiring (packet deliverable 2)

Run from the repository root:
    python3 -m pytest data/tests/test_p2_4_integrity.py -q
"""
import ast
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]   # data/
SCRIPTS_ROOT = REPOSITORY_ROOT / "scripts"
LIVE_CANONICAL = REPOSITORY_ROOT / "canonical"
INTEGRITY_PATH = SCRIPTS_ROOT / "integrity.py"
REFRESH_PATH = SCRIPTS_ROOT / "refresh_canonical_data.py"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


INTEGRITY = load_module(INTEGRITY_PATH, "p24_integrity_module")
IDENTITY = load_module(SCRIPTS_ROOT / "import" / "identity.py", "p24_identity_module")


def edition_ids(canonical_root: Path):
    return sorted(name[len("edition-"):-len(".json")]
                  for name in os.listdir(canonical_root / "editions")
                  if name.startswith("edition-") and name.endswith(".json"))


def latest_row_counts(canonical_root: Path):
    latest = edition_ids(canonical_root)[-1]
    manifest = json.loads(
        (canonical_root / "editions" / f"edition-{latest}.json").read_text(
            encoding="utf-8"))
    return manifest["row_counts"]


def count_store_rows(canonical_root: Path, relative_dir: str, prefix: str):
    directory = canonical_root / relative_dir
    total = 0
    for name in os.listdir(directory):
        if name.startswith(prefix) and name.endswith(".jsonl"):
            with open(directory / name, encoding="utf-8") as handle:
                total += sum(1 for line in handle if line.strip())
    return total


def tree_digest(root: Path):
    """Content digest of a tree (relative path + file sha256), in walk order."""
    digest = hashlib.sha256()
    for current, directories, names in os.walk(root):
        directories.sort()
        for name in sorted(names, key=os.fsencode):
            path = Path(current) / name
            digest.update(str(path.relative_to(root)).encode("utf-8") + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def first_news_line(canonical_root: Path):
    """(shard relative path, offset, raw line) of the first https news row."""
    directory = canonical_root / "evidence"
    for name in sorted(os.listdir(directory), key=os.fsencode):
        if not (name.startswith("evidence-") and name.endswith(".jsonl")):
            continue
        with open(directory / name, encoding="utf-8") as handle:
            for offset, line in enumerate(handle):
                if '"link":"https://' not in line:
                    continue
                row = json.loads(line)
                if row.get("kind") == "news" and \
                        str(row.get("payload", {}).get("link", "")).startswith("https://"):
                    return f"evidence/{name}", offset, line
    raise AssertionError("no https news row found in the canonical copy")


def append_evidence_row(canonical_root: Path, link, supersedes=None):
    """Append one schema-valid, id-consistent news row; returns (eid, shard, offset)."""
    row = {"schema": "ge16.evidence.v1",
           "evidence_id": IDENTITY.evidence_id(IDENTITY.normalize_link(link)),
           "kind": "news",
           "payload": {"link": link, "title": "P2.4 verifier fixture row"},
           "source_ref": {"file": "data/canonical/research/trackers/"
                                  "ge16-news-accepted.json",
                          "sha256": "0" * 64, "offset": 0},
           "collected_at": "", "normalizer_v": "normalize_link.v1"}
    if supersedes:
        row["supersedes"] = supersedes
    shard = canonical_root / "evidence" / f"evidence-{row['evidence_id'][2]}.jsonl"
    with open(shard, encoding="utf-8") as handle:
        offset = sum(1 for line in handle if line.strip())
    with open(shard, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":")) + "\n")
    return row["evidence_id"], f"evidence/{shard.name}", offset


class CanonicalSandbox(unittest.TestCase):
    """Shared fixture: a private tmp copy of the live canonical tree."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.data_root = Path(temporary.name) / "data"
        self.canonical = self.data_root / "canonical"
        shutil.copytree(LIVE_CANONICAL, self.canonical)

    def verify(self, *argv):
        """In-process CLI call returning (exit_code, parsed JSON report)."""
        output = io.StringIO()
        with redirect_stdout(output):
            code = INTEGRITY.main(["--data-root", str(self.data_root), *argv, "--json"])
        return code, json.loads(output.getvalue())


class TestCleanTree(CanonicalSandbox):
    """Brief §4.1 — clean tmp copy passes full + sampled; JSON parses; exit 0."""

    def test_every_subcommand_reports_clean_and_parses(self):
        latest = edition_ids(self.canonical)[-1]
        paths_total = len(json.loads(
            (self.canonical / "editions" / f"edition-{latest}.json").read_text(
            encoding="utf-8"))["content_hashes"])
        evidence_total = count_store_rows(self.canonical, "evidence", "evidence-")
        judgment_total = count_store_rows(self.canonical, "judgments", "judgment-")
        before = tree_digest(self.canonical)
        reports = [
            self.verify("verify-edition", latest),
            self.verify("verify-chain"),
            self.verify("verify-corpus", "--full"),
            self.verify("verify-corpus", "--sampled", "100", "--seed", "7"),
            self.verify("verify-recorded"),
        ]
        for code, report in reports:
            self.assertEqual(code, 0, report["command"])
            self.assertEqual(report["exit_code"], 0, report)
            self.assertEqual(report["status"], "clean", report)
            self.assertEqual(report["findings"], {"corrupt": [], "lost": []}, report)
        edition, chain, full, sampled, recorded = [r for _, r in reports]
        self.assertEqual((edition["paths_ok"], edition["paths_total"]),
                         (paths_total, paths_total))
        self.assertEqual(chain["editions_verified"], len(edition_ids(self.canonical)))
        self.assertEqual(full["mode"], "full")
        self.assertTrue(full["covers_full_corpus"])
        self.assertEqual(full["rows"]["evidence"], evidence_total)
        self.assertEqual(full["rows"]["judgments"], judgment_total)
        self.assertEqual(sampled["mode"], "sampled")
        self.assertEqual(sampled["checked"], {"evidence": 100, "judgments": 100})
        self.assertEqual(recorded["unrecorded_count"], 0)
        self.assertEqual(recorded["lost_rows"], [])
        self.assertEqual(recorded["baseline_edition"], latest)
        self.assertEqual(tree_digest(self.canonical), before)  # nothing written

    def test_human_output_mode_still_exits_zero(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = INTEGRITY.main(["--data-root", str(self.data_root), "verify-chain"])
        self.assertEqual(code, 0)
        self.assertIn("clean", output.getvalue())

    def test_unknown_edition_id_is_lost_not_clean(self):
        code, report = self.verify("verify-edition", "20200101T000000Z")
        self.assertEqual(code, 2)
        self.assertEqual(report["findings"]["lost"][0]["code"], "edition_absent")


class TestCorruptByte(CanonicalSandbox):
    """Brief §4.2 — one corrupted byte -> MISMATCH detected, exit 1."""

    def test_edition_input_single_byte_flip_is_a_hash_mismatch(self):
        latest = edition_ids(self.canonical)[-1]
        target = self.canonical / "entities" / "seed-vocabulary.json"
        raw = target.read_bytes()
        target.write_bytes(raw.replace(b":", b";", 1))  # one byte changed
        code, report = self.verify("verify-edition", latest)
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "corrupt")
        self.assertEqual([finding["code"] for finding in report["findings"]["corrupt"]],
                         ["mismatch"])
        self.assertEqual(report["findings"]["corrupt"][0]["path"],
                         "data/canonical/entities/seed-vocabulary.json")

    def test_shard_single_byte_corruption_is_rederivation_failure(self):
        shard, offset, line = first_news_line(self.canonical)
        # exactly one byte of the serialized payload link: s -> x
        self.assertIn('"link":"https://', line)
        corrupted = line.replace('"link":"https://', '"link":"httpx://', 1)
        path = self.canonical / shard
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        lines[offset] = corrupted
        path.write_text("".join(lines), encoding="utf-8")
        code, report = self.verify("verify-corpus", "--full")
        self.assertEqual(code, 1)
        findings = report["findings"]["corrupt"]
        self.assertIn("evidence_id_rederivation", [f["code"] for f in findings])
        hit = next(f for f in findings if f["code"] == "evidence_id_rederivation")
        self.assertEqual((hit["row"], hit["offset"]), (shard, offset))


class TestLostShard(CanonicalSandbox):
    """Brief §4.3 — a deleted file is LOST, exit 2 (worst severity wins)."""

    def test_deleted_evidence_shard_is_lost_rows_exit_2(self):
        shard = self.canonical / "evidence" / "evidence-3.jsonl"
        with open(shard, encoding="utf-8") as handle:
            lost = sum(1 for line in handle if line.strip())
        shard.unlink()
        code, report = self.verify("verify-recorded")
        self.assertEqual(code, 2)
        self.assertEqual(report["status"], "lost")
        self.assertEqual(report["unrecorded_count"], 0)
        deficits = {row["metric"]: row["deficit"] for row in report["lost_rows"]}
        # the by-kind breakdown subsumes the total: losses sum to the shard
        self.assertTrue({"evidence:news", "evidence:tracker-note"} <= set(deficits),
                        deficits)
        self.assertEqual(sum(deficits.values()), lost)

    def test_deleted_edition_input_file_is_missing_path_exit_2(self):
        latest = edition_ids(self.canonical)[-1]
        (self.canonical / "entities" / "seed-vocabulary.json").unlink()
        code, report = self.verify("verify-edition", latest)
        self.assertEqual(code, 2)
        self.assertEqual(report["status"], "lost")
        self.assertEqual(report["findings"]["lost"][0]["code"], "missing")


class TestUnrecordedAddition(CanonicalSandbox):
    """Brief §4.4 — an appended row no edition records is UNRECORDED, exit 1."""

    def test_appended_row_is_unrecorded_with_path_and_offset(self):
        recorded_news = latest_row_counts(self.canonical)["evidence_by_kind"]["news"]
        eid, shard, offset = append_evidence_row(
            self.canonical, "https://p24-fixture.example/unrecorded-addition")
        code, report = self.verify("verify-recorded")
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "corrupt")
        addition = report["findings"]["corrupt"][0]
        self.assertEqual(addition["code"], "unrecorded_addition")
        self.assertEqual(report["unrecorded_count"], 1)
        metric = next(m for m in addition["metrics"] if m["metric"] == "evidence:news")
        self.assertEqual(metric["recorded"], recorded_news)
        self.assertEqual(metric["on_disk"], recorded_news + 1)
        self.assertEqual(len(metric["rows"]), 1)
        # editions record counts, not row ids, so the flagged row is the one
        # beyond coverage in deterministic load order — but it must name a
        # REAL row on disk (actionable path+offset), not an abstract count.
        flagged = metric["rows"][0]
        with open(self.canonical / flagged["row"], encoding="utf-8") as handle:
            lines = [line for line in handle if line.strip()]
        row = json.loads(lines[flagged["offset"]])
        self.assertEqual(row["kind"], "news")
        self.assertEqual(row["evidence_id"], flagged["id"])

    def test_the_same_row_is_shape_clean_for_verify_corpus(self):
        """A well-formed row the corpus accepts but no edition records: the
        additions rule is edition coverage, not row shape."""
        append_evidence_row(self.canonical,
                            "https://p24-fixture.example/unrecorded-addition")
        code, report = self.verify("verify-corpus", "--full")
        self.assertEqual(code, 0, report["findings"])

    def test_single_unrecorded_judgment_yields_one_metric_entry(self):
        """R1 regression (review finding): the judgments_by_origin checks were
        appended twice, doubling every judgment finding. One appended
        judged-batch row must produce exactly one metric entry and
        unrecorded_count == 1. Precondition: the live edition carries
        judgments_by_origin (all three origins) — the case the duplicate
        loop inflated in production data."""
        recorded = latest_row_counts(self.canonical)
        self.assertIn("judgments_by_origin", recorded)
        shard_dir = self.canonical / "judgments"
        name = sorted(n for n in os.listdir(shard_dir)
                      if n.startswith("judgment-") and n.endswith(".jsonl"))[0]
        with open(shard_dir / name, encoding="utf-8") as handle:
            row = next(json.loads(line) for line in handle if line.strip())
        with open(shard_dir / name, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")) + "\n")
        code, report = self.verify("verify-recorded")
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "corrupt")
        self.assertEqual(report["unrecorded_count"], 1)
        additions = [f for f in report["findings"]["corrupt"]
                     if f["code"] == "unrecorded_addition"]
        self.assertEqual(len(additions), 1)
        judgment_metrics = [m for m in additions[0]["metrics"]
                            if m["metric"].startswith("judgment:")]
        self.assertEqual(len(judgment_metrics), 1)
        self.assertEqual(judgment_metrics[0]["on_disk"],
                         judgment_metrics[0]["recorded"] + 1)


class TestLegitimateSupersedes(CanonicalSandbox):
    """Brief §4.5 — a supersedes row recorded by a fresh edition passes."""

    def test_supersedes_addition_with_fresh_edition_verifies_clean(self):
        latest = edition_ids(self.canonical)[-1]
        counts = latest_row_counts(self.canonical)
        with open(self.canonical / "evidence" / "evidence-0.jsonl",
                  encoding="utf-8") as handle:
            supersedes = json.loads(
                next(line for line in handle if line.strip()))["evidence_id"]
        append_evidence_row(self.canonical,
                            "https://p24-fixture.example/legitimate-revision",
                            supersedes=supersedes)
        prior_manifest = json.loads(
            (self.canonical / "editions" / f"edition-{latest}.json").read_text(
                encoding="utf-8"))
        updated = dict(counts,
                       evidence_total=counts["evidence_total"] + 1,
                       evidence_by_kind=dict(counts["evidence_by_kind"],
                                             news=counts["evidence_by_kind"]["news"] + 1))
        edition = load_module(SCRIPTS_ROOT / "import" / "edition.py",
                              "p24_edition_writer")
        new_id, _ = edition.write_edition(
            str(self.canonical / "editions"),
            prior_manifest["lineage"]["inputs"], updated,
            note="P2.4 test: legitimate supersedes addition",
            prior=latest, now=datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc))

        self.assertEqual(new_id, "20260929T120000Z")
        for argv in (("verify-recorded",), ("verify-corpus", "--full"),
                     ("verify-edition", new_id), ("verify-chain",)):
            code, report = self.verify(*argv)
            self.assertEqual(code, 0, (argv, report["findings"]))
        code, chain = self.verify("verify-chain")
        self.assertEqual((chain["editions_verified"], chain["latest_edition_id"]),
                         (len(edition_ids(self.canonical)), new_id))

    def test_baseline_scoping_an_old_edition_sees_the_row_as_unrecorded(self):
        latest = edition_ids(self.canonical)[-1]
        append_evidence_row(self.canonical,
                            "https://p24-fixture.example/scoped-baseline")
        code, report = self.verify("verify-recorded", "--edition", latest)
        self.assertEqual(code, 1)
        self.assertEqual(report["baseline_edition"], latest)
        self.assertEqual(report["unrecorded_count"], 1)


class TestBrokenChain(CanonicalSandbox):
    """Brief §4.6 — prior_edition tampering is detected by verify-chain."""

    def rewrite(self, edition_id, section, field, value):
        path = self.canonical / "editions" / f"edition-{edition_id}.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if section is None:  # top-level field (e.g. edition_id itself)
            manifest[field] = value
        else:
            manifest[section][field] = value
        path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False,
                                   sort_keys=True) + "\n", encoding="utf-8")

    def test_prior_pointing_at_absent_id_is_lost_exit_2(self):
        middle = edition_ids(self.canonical)[1]
        self.rewrite(middle, "lineage", "prior_edition", "20200101T000000Z")
        code, report = self.verify("verify-chain")
        self.assertEqual(code, 2)
        broken = report["findings"]["lost"][0]
        self.assertEqual((broken["code"], broken["edition_id"]),
                         ("broken_link", middle))
        # the recorded baseline walk also reports the break
        code, recorded = self.verify("verify-recorded")
        self.assertEqual(code, 2)
        self.assertEqual(recorded["findings"]["lost"][0]["code"], "chain_broken")

    def test_duplicate_declared_edition_id_is_corrupt_exit_1(self):
        ids = edition_ids(self.canonical)
        self.rewrite(ids[-1], None, "edition_id", ids[1])  # declare a duplicate
        code, report = self.verify("verify-chain")
        self.assertEqual(code, 1)
        codes = [finding["code"] for finding in report["findings"]["corrupt"]]
        self.assertIn("duplicate_edition_id", codes)
        self.assertIn("edition_id_mismatch", codes)

    def test_prior_not_strictly_older_is_corrupt_exit_1(self):
        ids = edition_ids(self.canonical)
        self.rewrite(ids[1], "lineage", "prior_edition", ids[-1])  # newer prior
        code, report = self.verify("verify-chain")
        self.assertEqual(code, 1)
        ordering = next(f for f in report["findings"]["corrupt"]
                        if f["code"] == "timestamp_order")
        self.assertEqual(ordering["edition_id"], ids[1])


class TestSamplingVsFull(CanonicalSandbox):
    """Brief §4.7 — seeded sampling finds corruption with enough samples;
    full mode always finds it."""

    def setUp(self):
        super().setUp()
        shard, offset, line = first_news_line(self.canonical)
        path = self.canonical / shard
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        lines[offset] = line.replace('"link":"https://', '"link":"httpx://', 1)
        path.write_text("".join(lines), encoding="utf-8")

    def test_full_mode_finds_the_corruption(self):
        code, report = self.verify("verify-corpus", "--full")
        self.assertEqual(code, 1)

    def test_enough_samples_covers_the_corpus_and_finds_it(self):
        evidence_total = count_store_rows(self.canonical, "evidence", "evidence-")
        code, report = self.verify("verify-corpus", "--sampled", str(evidence_total))
        self.assertEqual(code, 1)
        self.assertTrue(report["covers_full_corpus"])
        self.assertEqual(report["checked"]["evidence"], evidence_total)

    def test_sampling_is_deterministic_for_a_fixed_seed(self):
        first = self.verify("verify-corpus", "--sampled", "50", "--seed", "7")
        second = self.verify("verify-corpus", "--sampled", "50", "--seed", "7")
        self.assertEqual(first[1], second[1])
        self.assertEqual(first[1]["checked"], {"evidence": 50, "judgments": 50})
        self.assertFalse(first[1]["covers_full_corpus"])


class TestVerifierWritesNothing(CanonicalSandbox):
    """Brief §4.8 — any verify invocation leaves the tree bit-identical, and
    the verifier source contains zero filesystem-write calls (AST pin)."""

    WRITE_ATTRIBUTES = {"write_text", "write_bytes", "write", "writelines", "mkdir",
                        "makedirs", "rmdir", "unlink", "rename", "replace", "touch",
                        "truncate", "chmod", "lchmod", "utime", "symlink_to",
                        "hardlink_to", "link_to"}
    OS_WRITE_FUNCTIONS = {"remove", "unlink", "rmdir", "mkdir", "makedirs", "rename",
                          "replace", "symlink", "link", "truncate", "write", "open",
                          "utime", "chmod", "fchmod", "creat"}

    def test_every_subcommand_leaves_the_tree_bit_identical(self):
        latest = edition_ids(self.canonical)[-1]
        before = tree_digest(self.canonical)
        for argv in (("verify-edition", latest), ("verify-chain",),
                     ("verify-corpus", "--full"), ("verify-corpus", "--sampled", "25"),
                     ("verify-recorded",), ("verify-recorded", "--edition", latest)):
            self.verify(*argv)
        self.assertEqual(tree_digest(self.canonical), before)

    def _write_mode_of_open_call(self, node):
        """Constant mode string of an open() call, positional or keyword."""
        mode = None
        if len(node.args) > 1:
            mode = node.args[1]
        for keyword in node.keywords:
            if keyword.arg == "mode":
                mode = keyword.value
        return mode.value if isinstance(mode, ast.Constant) else None

    def test_verifier_source_has_zero_write_calls(self):
        source = INTEGRITY_PATH.read_text(encoding="utf-8")
        self.assertNotIn("import shutil", source)
        self.assertNotIn("subprocess", source)
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, self.WRITE_ATTRIBUTES,
                                 f"write-like call: {ast.dump(node.func)}")
                if isinstance(node.func.value, ast.Name) and \
                        node.func.value.id == "shutil":
                    self.fail("verifier calls into shutil")
            elif isinstance(node.func, ast.Name) and node.func.id == "open":
                mode = self._write_mode_of_open_call(node)
                self.assertIsNotNone(
                    mode, "open() must pin an explicit read-only mode")
                self.assertFalse(any(char in mode for char in "wax+"),
                                 f"open() with write mode {mode!r}")
            for child in ast.walk(node):
                if (isinstance(child, ast.Attribute)
                        and isinstance(child.value, ast.Name)
                        and child.value.id == "os"
                        and child.attr in self.OS_WRITE_FUNCTIONS):
                    self.fail(f"os.{child.attr} call in the verifier")

    def test_ast_pin_actually_rejects_a_write_call(self):
        """Guard the guard: the AST walk must flag a planted write call."""
        probe = "def f(p):\n    p.write_text('x')\n"
        flagged = [node for node in ast.walk(ast.parse(probe))
                   if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                   and node.func.attr in self.WRITE_ATTRIBUTES]
        self.assertEqual(len(flagged), 1)


class TestRefreshRunModeGate(CanonicalSandbox):
    """Packet wiring — the run-mode-only post-promotion gate (deliverable 2).

    A stub collector stages a tracker file; the gate then verifies the new
    edition and a 500-row corpus sample. Byte-identical staging passes;
    staging a modified ge16-news-accepted.json makes promotion replace a file
    that inline judgments bind by hash (source_sha256) — a real provenance
    break the corpus sample catches (seed 0 deterministically draws 204 of
    those inline rows).
    """

    STUB = ("import os\n"
            "import shutil\n"
            "from pathlib import Path\n"
            "outdir = Path(os.environ['GE16_TRACKER_OUT_DIR'])\n"
            "source = Path(os.environ['P24_STAGE_SOURCE'])\n"
            "staged_name = os.environ.get('P24_STAGE_NAME', source.name)\n"
            "shutil.copyfile(source, outdir / staged_name)\n")

    def setUp(self):
        super().setUp()
        self.refresh = load_module(REFRESH_PATH, "p24_refresh_for_gate")
        self.stub = self.data_root.parent / "stub-collector.py"
        self.stub.write_text(self.STUB, encoding="utf-8")
        self.trackers = self.canonical / "research" / "trackers"
        self.manifest = self.canonical / "canonical-data-provenance.json"

    def staged_refresh(self, source, label, staged_name=None):
        environment = {"P24_STAGE_SOURCE": str(source)}
        if staged_name:
            environment["P24_STAGE_NAME"] = staged_name
        with mock.patch.dict(os.environ, environment):
            return self.refresh.staged_refresh(
                label=label, canonical_root=self.canonical,
                collector_commands=[[sys.executable, str(self.stub)]])

    def run_log(self, run_dir, name):
        return Path(run_dir) / "logs" / name

    def test_clean_promotion_passes_the_gate(self):
        summary = self.staged_refresh(self.trackers / "ge16-news-candidates.json",
                                      "p2.4 clean gate")
        self.assertEqual([check["exit_code"] for check in summary["integrity"]],
                         [0, 0])
        self.assertEqual(summary["promoted"], [])  # identical bytes -> unchanged
        logs = Path(summary["run_dir"]) / "logs"
        for name in ("integrity-verify-edition.json", "integrity-verify-corpus.json"):
            self.assertTrue((logs / name).is_file(), name)
        self.assertFalse((logs / "integrity-verify-failure.json").exists())
        self.assertTrue(self.manifest.is_file())  # refresh ran after the gate

    def test_corrupting_promotion_fails_the_gate_naming_the_edition(self):
        accepted = self.trackers / "ge16-news-accepted.json"
        tampered = self.data_root.parent / "tampered-accepted.json"
        tampered.write_bytes(accepted.read_bytes() + b"\n")
        manifest_before = self.manifest.read_bytes()
        with self.assertRaises(RuntimeError) as caught:
            # stage the tampered bytes UNDER THE BOUND FILE'S NAME, so
            # promotion replaces the file 1261 inline judgments hash-bind
            self.staged_refresh(tampered, "p2.4 broken gate",
                                staged_name="ge16-news-accepted.json")
        message = str(caught.exception)
        self.assertIn("post-promotion integrity gate failed", message)
        self.assertRegex(message, r"edition \d{8}T\d{6}Z")
        self.assertIn("verify-corpus exit 1", message)
        # the run dir records the failure for the owner's repair decision
        run_dirs = sorted((self.data_root / "work").iterdir())
        self.assertEqual(len(run_dirs), 1)
        logs = run_dirs[0] / "logs"
        edition_id = re.search(r"edition (\d{8}T\d{6}Z)", message).group(1)
        failure = json.loads(
            (logs / "integrity-verify-failure.json").read_text(encoding="utf-8"))
        self.assertEqual(failure["edition_id"], edition_id)
        self.assertIn(["verify-corpus", 1, "corrupt"], failure["failed"])
        corpus = json.loads(
            (logs / "integrity-verify-corpus.json").read_text(encoding="utf-8"))
        mismatches = [f for f in corpus["findings"]["corrupt"]
                      if f["code"] == "batch_hash_mismatch"]
        self.assertTrue(mismatches)
        self.assertTrue(all("ge16-news-accepted.json" in f["batch_file"]
                            for f in mismatches))
        # report-only: promotion stays landed, provenance was never refreshed
        self.assertEqual(accepted.read_bytes(), tampered.read_bytes())
        self.assertEqual(self.manifest.read_bytes(), manifest_before)

    def test_no_run_mode_has_no_gate(self):
        """--no-run is the pre-P2.3 refresh only: no run dir, no gate writes."""
        output = io.StringIO()
        with redirect_stdout(output):
            code = self.refresh.main(["--no-run"], canonical_root=self.canonical)
        self.assertEqual(code, 0)
        self.assertIn("canonical refresh passed", output.getvalue())
        self.assertNotIn("integrity", output.getvalue())
        self.assertFalse((self.data_root / "work").exists())


if __name__ == "__main__":
    unittest.main()
