"""P2.2 importer tests: classification, provenance classes, probes (stubbed —
no network in tests), collisions, the RED no-filename-exclusion guard, and
determinism (fresh-dir byte equality + same-dir zero-new-rows).

Run from the repository root:
    python3 -m pytest data/tests/test_p2_2_import.py -q
"""
import importlib.util
import json
import pathlib
import shutil
import tempfile
import unittest

import jsonschema

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
IMPORT_DIR = REPO_ROOT / "data" / "scripts" / "import"
SEED_PATH = REPO_ROOT / "data" / "canonical" / "entities" / "seed-vocabulary.json"
REAL_TRACKERS = REPO_ROOT / "data" / "canonical" / "research" / "trackers"

_spec = importlib.util.spec_from_file_location("import_evidence", IMPORT_DIR / "import_evidence.py")
import_evidence = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(import_evidence)

_e_spec = importlib.util.spec_from_file_location("entity_candidates", IMPORT_DIR / "entity_candidates.py")
entity_candidates = importlib.util.module_from_spec(_e_spec)
_e_spec.loader.exec_module(entity_candidates)


def load_schema(filename):
    with open(REPO_ROOT / "data" / "scripts" / "schemas" / filename, encoding="utf-8") as handle:
        return json.load(handle)


EVIDENCE_SCHEMA = load_schema("ge16_evidence.schema.json")
JUDGMENT_SCHEMA = load_schema("ge16_judgment.schema.json")

FIXED_CLOCK = lambda: "2026-09-28T00:00:00+00:00"  # noqa: E731

BATCH = "ge16-news-backfill-judged-batch-77.json"
RED_NAME = "ge16-news-backfill-anything.json"  # RED: a P1.7-R1-style transient name


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)


def matched_item():
    return {"query": "q1", "title": "Verbatim merged story", "date": "2026-09-20T00:00:00+00:00",
            "source": "Malay Mail", "link": "https://www.malaymail.com/news/a/?utm_source=rss",
            "lang": "en", "category": "election", "blocs": ["PH"], "parties": ["DAP"],
            "seats": [], "score": 0.8, "judged_at": "2026-09-26T06:09:16+00:00", "backfill": True}


def orphan_item():
    return {"query": "q2", "title": "Orphaned flag story", "date": "2026-09-21T00:00:00+00:00",
            "source": "The Vibes", "link": "https://www.thevibes.com/articles/b",
            "lang": "en", "category": "coalition", "blocs": ["PN"], "parties": ["BERSATU"],
            "seats": [], "score": 0.7, "judged_at": "2026-09-26T05:42:53+00:00", "backfill": True}


def weekly_item():
    return {"query": "q3", "title": "Weekly queue story", "date": "2026-09-22T00:00:00+00:00",
            "source": "NST Online", "link": "https://www.nst.com.my/news/c",
            "lang": "en", "category": "policy", "blocs": ["BN"], "parties": ["UMNO"],
            "seats": [], "score": 0.6, "judged_at": "2026-09-26T06:10:31+00:00"}


def build_trackers(trackers_dir):
    """Synthetic tracker set covering every disposition, collisions, and the
    RED-named file. Content — never filename — drives classification."""
    write_json(trackers_dir / "my-corpus.json", {
        "generated_at": "2026-09-26T06:10:31+00:00", "count": 5,
        "items": [matched_item(), orphan_item(), weekly_item(),
                  # same normalized link as matched_item(): documented collision
                  dict(matched_item(), title="Verbatim merged story (dupe copy)"),
                  {"query": "q4", "title": "No judgment fields", "date": "2026-09-23T00:00:00+00:00",
                   "source": "CNA", "link": "https://www.channelnewsasia.com/d"}]})
    write_json(trackers_dir / BATCH, {
        "schema": "ge16.news-backfill-judge-batch.v1", "batch": 77,
        "collection_id": "2026-09-26T06:06:07+00:00", "count": 2,
        "accepted_count": 2, "rejected_count": 0, "rejected_indexes": [],
        "judged_at": "2026-09-26T06:08:12+00:00", "judged_by": "deepseek",
        "judge_model": "deepseek-chat", "source_sha256": "a" * 64,
        "accepted": [
            # merge-key twin of matched_item() with identical judgment fields
            {"query": "q1", "title": "Verbatim merged story", "desc": "x",
             "link": "https://www.malaymail.com/news/a/?utm_source=rss",
             "date": "2026-09-20T00:00:00+00:00", "source": "Malay Mail",
             "found_at": "2026-09-26T06:00:00+00:00", "category": "election",
             "blocs": ["PH"], "parties": ["DAP"], "seats": [], "lang": "en",
             "score": 0.8, "judge_reason": "r", "judged_by": "deepseek",
             "judge_model": "deepseek-chat"},
            # link absent from the corpus: the batch supplies the evidence row too
            {"query": "q5", "title": "Batch-only story",
             "link": "https://www.theedge.com.my/e", "date": "2026-09-24T00:00:00+00:00",
             "source": "The Edge Malaysia", "category": "fiscal-federal",
             "blocs": [], "parties": [], "seats": [], "lang": "en", "score": 0.6}]})
    write_json(trackers_dir / RED_NAME, {
        # RED test: transient-styled NAME, judged-batch CONTENT -> must import.
        "schema": "ge16.news-backfill-judge-batch.v1", "batch": 78,
        "collection_id": "2026-09-26T06:06:07+00:00", "count": 1,
        "accepted_count": 1, "rejected_count": 0, "rejected_indexes": [],
        "judged_at": "2026-09-26T06:09:00+00:00", "judged_by": "deepseek",
        "judge_model": "deepseek-chat", "source_sha256": "b" * 64,
        "accepted": [{"query": "q6", "title": "RED name guard story",
                      "link": "https://example.com/red-name-guard",
                      "date": "2026-09-24T00:00:00+00:00", "source": "Google News",
                      "category": "campaign", "blocs": [], "parties": [], "seats": [],
                      "lang": "en", "score": 0.6}]})
    write_json(trackers_dir / "ge16-news-judged.json", {
        "schema": "ge16.news-judged.v1", "generated_at": "2026-09-26T06:09:16+00:00",
        "count": 1, "judged_by": "deepseek", "judge_model": "deepseek-chat",
        "accepted": [dict(weekly_item(), desc="live queue replay")]})
    write_json(trackers_dir / "ge16-news-candidates.json", {
        "generated_at": "2026-09-26T06:09:16+00:00", "collection_id": "2026-09-26T06:09:16+00:00",
        "count": 2, "items": [
            {"query": "q7", "title": "Unjudged candidate story",
             "link": "https://example.com/new-queue-link",
             "date": "2026-09-25T00:00:00+00:00", "source": "New Source on the Block"},
            # duplicate link inside the same queue file -> documented collision
            {"query": "q7", "title": "Unjudged candidate story again",
             "link": "https://example.com/new-queue-link?utm_term=x",
             "date": "2026-09-25T00:00:00+00:00", "source": "New Source on the Block"}]})
    write_json(trackers_dir / "ge16-polls-tracked.json", {
        "seen": ["Ilham Centre|Johor polls - FMT", "Ilham Centre|Johor polls - FMT",
                 "Merdeka Center|Seat trends - NST"], "generated_at": "2026-09-26T06:10:47+00:00"})
    write_json(trackers_dir / "ge16-news-seen-pending.json", {
        "schema": "ge16.news-seen-pending.v1", "updated_at": "2026-09-26T06:10:47+00:00",
        "collection_id": "2026-09-26T06:10:47+00:00", "count": 1,
        "pending": {"Pending cache story": {"query": "q8", "title": "Pending cache story",
                                            "link": "https://example.com/pending-cache"}}})
    write_json(trackers_dir / "ge16-news-backfill-judge-manifest.json", {
        "schema": "ge16.news-backfill-manifest.v1", "generated_at": "t",
        "collection_id": "t", "batch_count": 0, "total": 0, "batches": []})
    write_json(trackers_dir / "ge16-selfheal-state.json", {
        "schema": "ge16.selfheal-state.v1", "updated_at": "t", "steps": []})
    (trackers_dir / "ge16-poll-tracker-log.md").write_text("## Scan t — 0 items\n", encoding="utf-8")


def stub_probe(url):
    if "thevibes.com" in url:
        return {"http_status": 404, "final_url": url, "ok": False, "error": "HTTP 404"}
    if "orphan-down.example" in url:
        raise OSError("connection refused (synthetic)")
    return {"http_status": 200, "final_url": url, "ok": True, "error": None}


def make_data_root(parent):
    data_root = parent / "data"
    (data_root / "canonical" / "research" / "trackers").mkdir(parents=True)
    (data_root / "canonical" / "entities").mkdir(parents=True)
    shutil.copy(SEED_PATH, data_root / "canonical" / "entities" / "seed-vocabulary.json")
    return data_root


def read_rows(directory, prefix):
    rows = []
    if not directory.is_dir():
        return rows
    for name in sorted(directory.iterdir()):
        if name.name.startswith(prefix) and name.suffix == ".jsonl":
            rows.extend(json.loads(line) for line in
                        name.read_text(encoding="utf-8").splitlines() if line.strip())
    return rows


def shard_bytes(directory):
    return {name.name: name.read_bytes()
            for name in sorted(directory.iterdir()) if name.suffix == ".jsonl"}


class ImportBasics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.parent = pathlib.Path(self.tmp.name)
        self.data_root = make_data_root(self.parent)
        self.trackers = self.data_root / "canonical" / "research" / "trackers"
        build_trackers(self.trackers)
        self.canonical = self.data_root / "canonical"

    def run_import(self, data_root=None):
        return import_evidence.import_all(
            str(data_root or self.data_root), probe_function=stub_probe,
            clock=FIXED_CLOCK)

    def test_counts_by_class_and_kind(self):
        summary = self.run_import()
        counts = summary["counts"]
        self.assertEqual(counts["files_by_disposition"]["accepted-corpus"], 1)
        self.assertEqual(counts["files_by_disposition"]["judged-batch"], 2)  # incl. the RED file
        self.assertEqual(counts["files_by_disposition"]["tracked-list"], 1)
        self.assertEqual(counts["files_by_disposition"]["queue"], 2)  # candidates + seen-pending
        # news evidence: 4 corpus items (dupe collapses) + batch-only + RED +
        # queue link + pending cache = 8; tracker-notes: 2 (seen dupe collapses)
        self.assertEqual(counts["evidence"]["by_kind"], {"news": 8, "tracker-note": 2})
        self.assertEqual(counts["judgments"]["by_origin"],
                         {"judged-batch": 3, "accepted-corpus-inline": 1, "orphaned-flag": 2})
        self.assertEqual(counts["judgments"]["inline_batch_backed_skipped"], 1)
        # the corpus dupe copy (same link, DIFFERENT title) matches no batch row
        # by the V2 merge key, so its flag is orphaned too — 2 orphan rows; the
        # dupe's evidence keeps batch coverage, so only 1 evidence row is probed
        self.assertEqual(counts["judgments"]["orphaned_flag_evidence_probed"], 1)
        self.assertEqual(counts["probes_planned"], 1)

    def test_all_rows_validate_against_schemas(self):
        self.run_import()
        for row in read_rows(self.canonical / "evidence", "evidence-"):
            jsonschema.validate(row, EVIDENCE_SCHEMA)
        for row in read_rows(self.canonical / "judgments", "judgment-"):
            jsonschema.validate(row, JUDGMENT_SCHEMA)

    def test_tracker_notes_carry_p05_caveat(self):
        self.run_import()
        notes = [row for row in read_rows(self.canonical / "evidence", "evidence-")
                 if row["kind"] == "tracker-note"]
        self.assertEqual(len(notes), 2)  # third seen string is a same-file dupe
        for row in notes:
            self.assertIn("P0.5", row["notes"])
            self.assertIn("|", row["payload"]["seen"])

    def test_batch_judgment_has_full_basis(self):
        self.run_import()
        rows = [row for row in read_rows(self.canonical / "judgments", "judgment-")
                if row["basis"]["origin"] == "judged-batch"]
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertTrue(row["basis"]["verifiable"])
            self.assertTrue(row["basis"]["batch_file"].startswith(
                "data/canonical/research/trackers/"))
            # dual-hash basis (P2.2-R1): the auditor-verifiable binding is the
            # import-time hash of the batch file itself; the embedded hash is the
            # (stale, input-generation) self-reported value, kept verbatim
            self.assertRegex(row["basis"]["batch_file_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(row["basis"]["embedded_source_sha256"], r"^[0-9a-f]{64}$")
            self.assertIn("batch_file_sha256", row["basis"]["source_hash_semantics"])
            self.assertNotIn("source_sha256", row["basis"])
            self.assertEqual(row["judge"]["by"], "deepseek")
            self.assertEqual(row["judge"]["model"], "deepseek-chat")

    def test_orphan_flag_unverifiable_with_probe(self):
        summary = self.run_import()
        rows = [row for row in read_rows(self.canonical / "judgments", "judgment-")
                if row["basis"]["origin"] == "orphaned-flag"]
        self.assertEqual(len(rows), 2)  # the orphan story + the corpus dupe copy
        for row in rows:
            self.assertFalse(row["basis"]["verifiable"])
            self.assertIn("source_recovery", row["confidence_note"])
        evidence = read_rows(self.canonical / "evidence", "evidence-")
        orphan_evidence = [row for row in evidence
                           if row["payload"].get("title") == "Orphaned flag story"]
        self.assertEqual(len(orphan_evidence), 1)
        recovery = orphan_evidence[0]["source_recovery"]
        self.assertFalse(recovery["ok"])               # honest 404, never fabricated
        self.assertEqual(recovery["http_status"], 404)
        self.assertEqual(recovery["checked_at"], FIXED_CLOCK())
        self.assertEqual(summary["counts"]["probes_planned"], 1)

    def test_in_flight_queue_never_yields_judgments(self):
        self.run_import()
        evidence = read_rows(self.canonical / "evidence", "evidence-")
        judgments = read_rows(self.canonical / "judgments", "judgment-")
        judged_ids = {row["evidence_id"] for row in judgments}
        for link in ("https://example.com/new-queue-link", "https://example.com/pending-cache"):
            eid = [row["evidence_id"] for row in evidence
                   if row["payload"].get("link") == link]
            self.assertEqual(len(eid), 1, link)
            self.assertNotIn(eid[0], judged_ids, f"queue link {link} must stay unjudged")
        # the weekly story's single judgment is the corpus inline row; the live
        # queue replay (ge16-news-judged.json) must not add a second one
        weekly = [row for row in judgments if row["judged_at"] == "2026-09-26T06:10:31+00:00"]
        self.assertEqual(len(weekly), 1)
        self.assertEqual(weekly[0]["basis"]["origin"], "accepted-corpus-inline")

    def test_RED_transient_named_file_imports(self):
        """RED guard: a source file named ge16-news-backfill-anything.json with
        valid items MUST import (the reverted P1.7 R1 regression)."""
        summary = self.run_import()
        evidence = read_rows(self.canonical / "evidence", "evidence-")
        guard = [row for row in evidence
                 if row["payload"].get("link") == "https://example.com/red-name-guard"]
        self.assertEqual(len(guard), 1)
        self.assertEqual(guard[0]["source_ref"]["file"],
                         "data/canonical/research/trackers/" + RED_NAME)
        self.assertEqual(summary["counts"]["files_by_disposition"]["judged-batch"], 2)

    def test_collisions_recorded_never_dropped(self):
        self.run_import()
        with open(self.canonical / "evidence" / "dupe-of-candidates.json",
                  encoding="utf-8") as handle:
            dupes = json.load(handle)["candidates"]
        keys = [d["normalized_key"] for d in dupes]
        self.assertIn("https://www.malaymail.com/news/a/", keys)
        self.assertIn("https://example.com/new-queue-link", keys)
        self.assertTrue(any("|" in key for key in keys))  # tracker seen dupe

    def test_verbatim_merge_key_twin_skips_inline_row(self):
        self.run_import()
        rows = [row for row in read_rows(self.canonical / "judgments", "judgment-")
                if row["judged_at"] == "2026-09-26T06:09:16+00:00"]
        # only the dupe copy's orphaned-flag row keeps this commit timestamp; the
        # verbatim twin's inline copy was superseded by the batch judgment row
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["basis"]["origin"], "orphaned-flag")

    def test_edition_manifest_and_prior(self):
        first = self.run_import()
        editions = self.canonical / "editions"
        self.assertEqual(len(list(editions.glob("edition-*.json"))), 1)
        with open(sorted(editions.glob("edition-*.json"))[0], encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertEqual(manifest["schema"], "ge16.edition.v1")
        self.assertEqual(manifest["edition_id"], first["edition_id"])
        self.assertIsNone(manifest["lineage"]["prior_edition"])
        self.assertTrue(all(len(entry["sha256"]) == 64 for entry in manifest["lineage"]["inputs"]))
        second = self.run_import()
        self.assertNotEqual(second["edition_id"], first["edition_id"])
        with open(sorted(editions.glob("edition-*.json"))[-1], encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["lineage"]["prior_edition"], first["edition_id"])

    def test_entity_candidates_flow(self):
        self.run_import()
        candidates = read_rows(self.canonical / "entities", "entity-candidates")
        self.assertTrue(candidates)
        self.assertTrue(all(row["status"] == "proposed" for row in candidates))
        strings = {row["candidate_string"] for row in candidates}
        self.assertIn("New Source on the Block", strings)   # unseen source -> candidate
        self.assertNotIn("PH", strings)                     # seed vocabulary never proposed
        with open(self.canonical / "entities" / "entities.json", encoding="utf-8") as handle:
            registry = json.load(handle)
        self.assertEqual(len(registry["entities"]), 44)      # seed only: nothing auto-promoted


class DeterminismTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.parent = pathlib.Path(self.tmp.name)

    def build(self, suffix):
        data_root = make_data_root(self.parent / suffix)
        build_trackers(data_root / "canonical" / "research" / "trackers")
        return data_root

    def test_fresh_imports_byte_identical(self):
        root_a, root_b = self.build("a"), self.build("b")
        import_evidence.import_all(str(root_a), probe_function=stub_probe, clock=FIXED_CLOCK)
        import_evidence.import_all(str(root_b), probe_function=stub_probe, clock=FIXED_CLOCK)
        for sub in ("evidence", "judgments"):
            self.assertEqual(shard_bytes(root_a / "canonical" / sub),
                             shard_bytes(root_b / "canonical" / sub), sub)

    def test_rerun_same_root_adds_zero_rows_and_is_byte_identical(self):
        data_root = self.build("c")
        import_evidence.import_all(str(data_root), probe_function=stub_probe, clock=FIXED_CLOCK)
        before = {sub: shard_bytes(data_root / "canonical" / sub)
                  for sub in ("evidence", "judgments")}
        dupe_before = (data_root / "canonical" / "evidence" /
                       "dupe-of-candidates.json").read_bytes()
        candidates_before = (data_root / "canonical" / "entities" /
                             "entity-candidates.jsonl").read_bytes()
        second = import_evidence.import_all(str(data_root), probe_function=stub_probe,
                                            clock=FIXED_CLOCK)
        self.assertEqual(second["counts"]["evidence"]["new"], 0)
        self.assertEqual(second["counts"]["judgments"]["new"], 0)
        self.assertEqual(second["counts"]["probes"], [])  # rows immutable -> no re-probe
        for sub in ("evidence", "judgments"):
            self.assertEqual(shard_bytes(data_root / "canonical" / sub), before[sub], sub)
        self.assertEqual((data_root / "canonical" / "evidence" /
                          "dupe-of-candidates.json").read_bytes(), dupe_before)
        self.assertEqual((data_root / "canonical" / "entities" /
                          "entity-candidates.jsonl").read_bytes(), candidates_before)


class ProbeHonestyTests(unittest.TestCase):
    def test_network_failure_recorded_not_fabricated(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as tmp:
            data_root = make_data_root(pathlib.Path(tmp))
            trackers = data_root / "canonical" / "research" / "trackers"
            item = orphan_item()
            item["link"] = "https://orphan-down.example/gone"
            write_json(trackers / "corpus.json", {
                "generated_at": "t", "count": 1, "items": [item]})

            def crashing_probe(url):
                raise OSError("connection refused (synthetic)")

            import_evidence.import_all(str(data_root), probe_function=crashing_probe,
                                       clock=FIXED_CLOCK)
            rows = read_rows(data_root / "canonical" / "evidence", "evidence-")
            self.assertEqual(len(rows), 1)
            recovery = rows[0]["source_recovery"]
            self.assertFalse(recovery["ok"])
            self.assertIsNone(recovery["http_status"])
            self.assertIn("connection refused", recovery["error"])


class RealDataTests(unittest.TestCase):
    """Full import of the real tracker mirror with stubbed probes (no network
    in tests); headline reconciliation figures from P2.1."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
        cls.data_root = make_data_root(pathlib.Path(cls.tmp.name))
        shutil.copytree(REAL_TRACKERS, cls.data_root / "canonical" / "research" / "trackers",
                       dirs_exist_ok=True)
        cls.summary = import_evidence.import_all(str(cls.data_root), probe_function=stub_probe,
                                                 clock=FIXED_CLOCK)
        cls.canonical = cls.data_root / "canonical"

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_headline_counts(self):
        counts = self.summary["counts"]
        self.assertEqual(counts["files_by_disposition"]["accepted-corpus"], 1)
        self.assertEqual(counts["files_by_disposition"]["judged-batch"], 24)
        self.assertEqual(counts["files_by_disposition"]["tracked-list"], 3)
        self.assertEqual(counts["judgments"]["batch_accepted_rows_seen"], 935)
        self.assertEqual(counts["judgments"]["orphaned_flag_rows"], 811)
        # 313 no-match orphans minus 21 whose evidence already carries a
        # surviving judged-batch judgment (same link under a different title)
        self.assertEqual(counts["judgments"]["orphaned_flag_evidence_probed"], 292)
        self.assertEqual(counts["probes_planned"], 292)
        by_source = counts["evidence"]["by_source_class"]
        self.assertEqual(by_source["accepted-corpus"], 2213)  # 2367 items - 154 collisions
        self.assertEqual(by_source["judged-batch"], 6)
        self.assertEqual(counts["evidence"]["by_kind"]["tracker-note"], 18460)
        accepted_collisions = [c for c in counts["collisions"]
                               if c["source_class"] == "accepted-corpus"]
        self.assertEqual(len(accepted_collisions), 154)
        self.assertEqual(counts["judgments"]["by_origin"],
                         {"judged-batch": 934, "accepted-corpus-inline": 1261,
                          "orphaned-flag": 811})

    def test_all_real_rows_validate(self):
        validator = jsonschema.Draft202012Validator(EVIDENCE_SCHEMA)
        rows = read_rows(self.canonical / "evidence", "evidence-")
        self.assertGreater(len(rows), 20000)
        errors = []
        for row in rows:
            for error in validator.iter_errors(row):
                errors.append(f"{row['evidence_id']}: {error.message}")
                if len(errors) > 5:
                    break
            if len(errors) > 5:
                break
        self.assertEqual(errors, [])
        for row in read_rows(self.canonical / "judgments", "judgment-"):
            jsonschema.validate(row, JUDGMENT_SCHEMA)

    def test_batch_basis_hashes_match_disk(self):
        """P2.2-R1: every sampled judged-batch row carries BOTH hashes.
        batch_file_sha256 must equal the sha256 of the referenced file on disk
        (the auditor-verifiable binding); embedded_source_sha256 is the batch
        doc's self-reported input-generation hash, which must NOT match the
        file — with source_hash_semantics documenting exactly that."""
        rows = [row for row in read_rows(self.canonical / "judgments", "judgment-")
                if row["basis"]["origin"] == "judged-batch"]
        sample = rows[::max(1, len(rows) // 10)]
        self.assertGreaterEqual(len(sample), 10)
        for row in sample:
            basis = row["basis"]
            rel = pathlib.PurePosixPath(basis["batch_file"]).relative_to("data")
            digest = import_evidence.sha256_file(str(self.data_root / rel))
            self.assertEqual(basis["batch_file_sha256"], digest,
                             f"{basis['batch_file']}: verifiable hash mismatch")
            self.assertNotEqual(basis["embedded_source_sha256"], digest,
                                "embedded hash must not be presented as the file hash")
            self.assertIn("batch_file_sha256", basis["source_hash_semantics"])
            self.assertIn("overwritten in V2", basis["source_hash_semantics"])

    def test_no_filename_exclusion_logic_in_production_scripts(self):
        production = [
            IMPORT_DIR / "import_evidence.py", IMPORT_DIR / "entity_candidates.py",
            IMPORT_DIR / "normalize_link.py", IMPORT_DIR / "identity.py",
            IMPORT_DIR / "edition.py", IMPORT_DIR / "parse_verdicts.py",
            REPO_ROOT / "data" / "scripts" / "refresh_canonical_data.py",
            REPO_ROOT / "data" / "scripts" / "validate_canonical_data.py",
            REPO_ROOT / "data" / "scripts" / "methodology_contract.py",
        ]
        banned = ("fnmatch", "glob.glob", "blocklist", "denylist", "allowlist")
        for path in production:
            text = path.read_text(encoding="utf-8")
            for marker in banned:
                self.assertNotIn(marker, text,
                                 f"{path.name} contains exclusion logic: {marker}")


if __name__ == "__main__":
    unittest.main()
