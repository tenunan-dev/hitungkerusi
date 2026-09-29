"""P2.5 baseline-migration tests (evidence/P2/P2.5-design-brief.md §5).

Every test builds a private tmp V2-shaped fixture root and a tmp copy of the
live canonical tree — neither the real V2 tree nor the real canonical tree is
ever touched by this suite.

  1. TestIdempotency          second migrate run changes zero canonical bytes
  2. TestCollisionRefusal     pre-existing destination aborts promotion
  3. TestReconciliation       every P0.7 row + every walked file gets a
                              disposition (nothing silently dropped)
  4. TestVerifierGate         post-promotion verify-chain + verify-edition green
  5. TestFailingStage         a corrupt source DB leaves canonical untouched
  6. TestSqliteSnapshot       backup-API snapshot: integrity_check ok, table
                              counts match source, sidecar sha256 correct
  7. TestV2ReadOnly           V2 fixture mtimes/bytes identical before/after

Run from the repository root:
    PYTHONPATH= .venv/bin/python3 -m pytest data/tests/test_p2_5_baseline_migration.py -q
"""
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]  # data/
SCRIPTS_ROOT = REPOSITORY_ROOT / "scripts"
LIVE_CANONICAL = REPOSITORY_ROOT / "canonical"
MIGRATE_PATH = SCRIPTS_ROOT / "migrate_baseline.py"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIGRATE = load_module(MIGRATE_PATH, "p25_migrate_baseline_module")
INTEGRITY = load_module(SCRIPTS_ROOT / "integrity.py", "p25_integrity_module")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def tree_digest(root: Path):
    digest = hashlib.sha256()
    for current, directories, names in os.walk(root):
        directories.sort()
        for name in sorted(names, key=os.fsencode):
            path = Path(current) / name
            digest.update(str(path.relative_to(root)).encode("utf-8") + b"\0")
            digest.update(path.read_bytes())
    return digest.hexdigest()


def make_events_db(path, events=3, corrupt=False):
    if Path(path).exists():
        Path(path).unlink()
    connection = sqlite3.connect(str(path))
    connection.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, title TEXT)")
    connection.execute("CREATE TABLE sources (id INTEGER PRIMARY KEY, url TEXT)")
    for i in range(events):
        connection.execute("INSERT INTO events (title) VALUES (?)", (f"event-{i}",))
    connection.execute("INSERT INTO sources (url) VALUES (?)", ("https://example.test",))
    connection.commit()
    connection.close()
    if corrupt:
        # Truncate the file mid-header: a real structural corruption that
        # PRAGMA integrity_check (and even opening the DB) will reject.
        with open(path, "r+b") as handle:
            handle.truncate(200)


class BaselineMigrationFixture(unittest.TestCase):
    """Shared fixture: a small V2-shaped root + a tmp copy of live canonical,
    plus a minimal P0.7 dispositions file covering just the rows this tool
    reads a schema/id/asset/disposition shape from."""

    EXTRA_ROW_IDS = ("1a", "1f")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

        self.v2_root = self.tmp / "v2"
        self.data_root = self.tmp / "data"
        self.canonical = self.data_root / "canonical"
        shutil.copytree(LIVE_CANONICAL, self.canonical)
        # This suite tests the pre-P2.5 "gap" scenario against its own small
        # synthetic events DB fixture; the live canonical tree may already
        # carry the real migrated ge16-events.db (this tool's own prior run),
        # which would otherwise appear as a pre-existing collision here.
        live_events_db = self.canonical / "events" / "ge16-events.db"
        if live_events_db.exists():
            live_events_db.unlink()

        # A tiny V2-shaped tree: one file per canonical root (all already
        # present + byte-identical, mirroring the real corpus's clean state)
        # plus the events DB fixture (absent from the canonical copy).
        for root in MIGRATE.CANONICAL_ROOTS:
            source_dir = self.v2_root / MIGRATE.V2_DATA_SUBDIR / root
            source_dir.mkdir(parents=True, exist_ok=True)
            content = f"fixture content for {root}\n".encode("utf-8")
            (source_dir / "fixture.txt").write_bytes(content)
            destination_dir = self.canonical / root
            destination_dir.mkdir(parents=True, exist_ok=True)
            (destination_dir / "fixture.txt").write_bytes(content)

        events_dir = self.v2_root / MIGRATE.EVENTS_DB_V2_RELATIVE.parent
        events_dir.mkdir(parents=True, exist_ok=True)
        self.events_db_source = self.v2_root / MIGRATE.EVENTS_DB_V2_RELATIVE
        make_events_db(self.events_db_source)

        self.p07_path = self.tmp / "p07-dispositions.json"
        rows = [{"id": row_id, "group": "fixture", "asset": "fixture",
                "disposition": "copy-unchanged", "owner_decision": None,
                "reason": "fixture row"} for row_id in MIGRATE.ROW_CLASSIFICATION]
        self.p07_path.write_text(json.dumps({"rows": rows}), encoding="utf-8")

    def inventory(self):
        return MIGRATE.inventory(v2_root=self.v2_root, canonical_root=self.canonical,
                                 p07_path=self.p07_path)

    def stage(self, report=None):
        return MIGRATE.stage(report or self.inventory(), canonical_root=self.canonical,
                             data_root=self.data_root)

    def v2_file_stats(self):
        stats = {}
        for current, _, names in os.walk(self.v2_root):
            for name in names:
                path = Path(current) / name
                stat = os.stat(path)
                stats[str(path)] = (stat.st_size, stat.st_mtime_ns, path.read_bytes())
        return stats


class TestReconciliation(BaselineMigrationFixture):
    """Brief §5.3 — every P0.7 row and every walked file gets a disposition."""

    def test_every_p07_row_gets_a_disposition(self):
        report = self.inventory()
        classified = {item["p07_id"] for item in report["p07_out_of_scope"]}
        covered_elsewhere = {row_id for row_id, kind in MIGRATE.ROW_CLASSIFICATION.items()
                             if kind != "out-of-scope"}
        self.assertEqual(classified | covered_elsewhere,
                         set(MIGRATE.ROW_CLASSIFICATION))
        self.assertEqual(report["p07_rows_total"], len(MIGRATE.ROW_CLASSIFICATION))

    def test_unknown_row_id_raises_instead_of_silently_dropping(self):
        self.p07_path.write_text(json.dumps({"rows": [
            {"id": "unknown-row", "asset": "x", "disposition": "copy-unchanged",
             "reason": "not classified"}]}), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.inventory()

    def test_every_canonical_root_file_gets_a_disposition(self):
        report = self.inventory()
        self.assertEqual(len(report["file_items"]), len(MIGRATE.CANONICAL_ROOTS))
        for item in report["file_items"]:
            self.assertIn(item["disposition"], ("already-present", "missing", "changed"))
        self.assertEqual(report["counts"]["already-present"], len(MIGRATE.CANONICAL_ROOTS))
        self.assertEqual(report["events_db"]["disposition"], "missing")


class TestIdempotency(BaselineMigrationFixture):
    """Brief §5.1 — running migrate twice changes zero canonical bytes."""

    def test_second_run_is_a_byte_identical_no_op(self):
        run, stage_dir, manifest = self.stage()
        self.assertEqual(len(manifest["items"]), 1)
        result = MIGRATE.promote(stage_dir, canonical_root=self.canonical)
        self.assertIsNotNone(result["edition_id"])
        after_first = tree_digest(self.canonical)

        second_report = self.inventory()
        self.assertEqual(second_report["events_db"]["disposition"], "already-present")
        run2, stage_dir2, manifest2 = self.stage(second_report)
        self.assertEqual(manifest2["items"], [])
        second_result = MIGRATE.promote(stage_dir2, canonical_root=self.canonical)
        self.assertEqual(second_result["promoted"], [])
        self.assertIsNone(second_result["edition_id"])
        self.assertEqual(tree_digest(self.canonical), after_first)


class TestCollisionRefusal(BaselineMigrationFixture):
    """Brief §5.2 — a pre-existing destination aborts promotion before writing."""

    def test_preexisting_destination_aborts_and_leaves_canonical_untouched(self):
        run, stage_dir, manifest = self.stage()
        destination = self.canonical / "events" / "ge16-events.db"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"pre-existing unrelated content")
        before = tree_digest(self.canonical)
        editions_before = sorted((self.canonical / "editions").iterdir())
        with self.assertRaises(RuntimeError) as caught:
            MIGRATE.promote(stage_dir, canonical_root=self.canonical)
        self.assertIn("collision", str(caught.exception))
        self.assertEqual(tree_digest(self.canonical), before)
        self.assertEqual(sorted((self.canonical / "editions").iterdir()), editions_before)
        self.assertEqual(destination.read_bytes(), b"pre-existing unrelated content")
        # staged dir survives for audit (rollback = don't promote, not delete)
        self.assertTrue(Path(stage_dir).is_dir())


class TestVerifierGate(BaselineMigrationFixture):
    """Brief §5.4 — post-promotion verify-chain + verify-edition are green."""

    def test_promotion_passes_the_integrity_gate(self):
        run, stage_dir, manifest = self.stage()
        result = MIGRATE.promote(stage_dir, canonical_root=self.canonical)
        commands = {entry["command"]: entry["exit_code"] for entry in result["verified"]}
        self.assertEqual(commands, {"verify-chain": 0, "verify-edition": 0})
        # independent re-check directly against integrity.py, not just the
        # promote() call's own report
        chain = INTEGRITY.verify_chain(canonical_root=self.canonical)
        self.assertEqual(chain["exit_code"], 0, chain)
        edition = INTEGRITY.verify_edition(result["edition_id"], canonical_root=self.canonical)
        self.assertEqual(edition["exit_code"], 0, edition)
        manifest_json = json.loads(
            (self.canonical / "editions" / f"edition-{result['edition_id']}.json")
            .read_text(encoding="utf-8"))
        self.assertEqual(manifest_json["schema"], "ge16.edition.v1")
        self.assertIn("events/ge16-events.db", manifest_json["content_hashes"])


class TestFailingStage(BaselineMigrationFixture):
    """Brief §5.5 — a failing stage leaves canonical untouched."""

    def test_corrupt_source_db_refuses_to_stage_and_canonical_is_untouched(self):
        make_events_db(self.events_db_source, corrupt=True)
        before = tree_digest(self.canonical)
        with self.assertRaises((RuntimeError, sqlite3.DatabaseError)):
            self.stage()
        self.assertEqual(tree_digest(self.canonical), before)
        self.assertFalse((self.canonical / "events" / "ge16-events.db").exists())

    def test_integrity_check_not_ok_is_refused_before_any_copy(self):
        report = self.inventory()
        report["events_db"]["v2_integrity_check"] = "corruption detected at page 1"
        before = tree_digest(self.canonical)
        with self.assertRaises(RuntimeError) as caught:
            MIGRATE.stage(report, canonical_root=self.canonical, data_root=self.data_root)
        self.assertIn("integrity_check", str(caught.exception))
        self.assertEqual(tree_digest(self.canonical), before)


class TestSqliteSnapshot(BaselineMigrationFixture):
    """Brief §5.6 — backup-API snapshot consistency."""

    def test_staged_snapshot_matches_source_table_counts_and_passes_integrity(self):
        run, stage_dir, manifest = self.stage()
        item = manifest["items"][0]
        staged_path = Path(item["staged_path"])
        self.assertTrue(staged_path.is_file())
        connection = sqlite3.connect(f"file:{staged_path}?mode=ro", uri=True)
        self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], 3)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 1)
        connection.close()
        self.assertEqual(item["table_counts"], {"events": 3, "sources": 1})
        sidecar = staged_path.with_suffix(staged_path.suffix + ".sha256")
        self.assertEqual(sidecar.read_text(encoding="utf-8").strip(),
                         hashlib.sha256(staged_path.read_bytes()).hexdigest())
        self.assertEqual(sidecar.read_text(encoding="utf-8").strip(), item["staged_sha256"])

    def test_second_inventory_reads_already_present_via_table_counts_not_raw_bytes(self):
        """The backup API does not guarantee byte-identical output, so
        already-migrated detection must use integrity_check + table counts,
        not a raw sha256 compare against the V2 source file."""
        run, stage_dir, manifest = self.stage()
        MIGRATE.promote(stage_dir, canonical_root=self.canonical)
        destination = self.canonical / "events" / "ge16-events.db"
        source_sha = hashlib.sha256(self.events_db_source.read_bytes()).hexdigest()
        # a byte-for-byte compare against the source would very likely differ
        self.assertNotEqual(hashlib.sha256(destination.read_bytes()).hexdigest(), source_sha)
        report = self.inventory()
        self.assertEqual(report["events_db"]["disposition"], "already-present")


class TestV2ReadOnly(BaselineMigrationFixture):
    """Brief §5.7 — V2 mtimes/bytes identical before and after every command."""

    def test_inventory_stage_promote_leave_v2_untouched(self):
        before = self.v2_file_stats()
        report = self.inventory()
        run, stage_dir, manifest = self.stage(report)
        MIGRATE.promote(stage_dir, canonical_root=self.canonical)
        after = self.v2_file_stats()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
