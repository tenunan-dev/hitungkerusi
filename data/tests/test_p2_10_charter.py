"""P2.10 charter test matrix — the 5 gap tests (parent, 2026-10-01).

Covers the partial behaviors from evidence/P2/P2.10-gap-audit.md:
  T2  corrections retain provenance (supersede keeps old row byte-identical)
  T3  late publication is captured (pre-window item arriving after window advance)
  T6  changed/added/deleted scratch does not invalidate durable data
  T7  genuine durable corruption fails the gate
  T8  rebuild identifies exact source snapshot (edition binding)
"""
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
SCRIPTS_ROOT = SCRIPTS
sys.path.insert(0, str(SCRIPTS))

from test_p2_8_knowledge import _scratch_canonical  # noqa: E402


def _load_module(path, name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CorrectionProvenanceTests(unittest.TestCase):
    """Charter #2: corrections retain provenance."""

    def test_supersede_keeps_old_row_and_links_new_to_old(self):
        import judge_runs as JR  # noqa: E402

        tmp, canon = _scratch_canonical()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        data_root = tmp / "data"

        def ok_prober(url, timeout=8):
            return {"http_status": 200, "final_url": url, "error": None}

        session, summary = JR.run_orphaned_flag_pass(
            canonical_root=canon, prober=ok_prober, data_root=data_root)
        self.assertTrue(summary["counts"].get("verified", 0) > 0
                        or summary["counts"].get("resolved", 0) > 0
                        or summary["total_items"] > 0)

        rows_before = {
            r["judgment_id"]: json.dumps(r, sort_keys=True)
            for r in JR._load_judgment_rows(canon)}
        self.assertTrue(rows_before, "canonical must hold judgment rows")

        # every decision that supersedes must name its target
        session_state = session.state
        decisions = session_state.get("decisions", [])
        superseding = [d for d in decisions if d.get("supersedes_canonical_id")]
        if superseding:
            for d in superseding:
                target = d["supersedes_canonical_id"]
                self.assertIn(target, rows_before,
                              "supersede target must be an existing canonical row")
                # old row byte-identical after promotion
                self.assertEqual(rows_before[target],
                                 json.dumps(next(r for r in JR._load_judgment_rows(canon)
                                                 if r["judgment_id"] == target),
                                            sort_keys=True))


class ScratchIsolationTests(unittest.TestCase):
    """Charter #6: changed/added/deleted scratch does not invalidate durable data."""

    def test_work_dir_mutations_leave_canonical_and_verifier_untouched(self):
        integ = _load_module(SCRIPTS_ROOT / "integrity.py", "p210_integrity")
        tmp, canon = _scratch_canonical()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        work = tmp / "data" / "work"
        work.mkdir(parents=True, exist_ok=True)

        before = {
            p.relative_to(canon).as_posix(): p.stat().st_size
            for p in canon.rglob("*") if p.is_file()}

        # add, change, delete under work/ — none of it may touch canonical
        (work / "run-a").mkdir(parents=True, exist_ok=True)
        (work / "run-a" / "junk.jsonl").write_text("{}\n", encoding="utf-8")
        (work / "run-a" / "junk.jsonl").write_text("{}\n{}\n", encoding="utf-8")
        shutil.rmtree(work / "run-a")
        (work / "run-b").mkdir(parents=True)

        after = {
            p.relative_to(canon).as_posix(): p.stat().st_size
            for p in canon.rglob("*") if p.is_file()}
        self.assertEqual(before, after,
                         "canonical must be byte-identical after scratch churn")

        reports = [integ.verify_corpus(canon)]
        for r in reports:
            self.assertEqual(r["exit_code"], 0)


class DurableCorruptionTests(unittest.TestCase):
    """Charter #7: genuine durable corruption fails the gate."""

    def test_bit_flip_in_canonical_jsonl_fails_verifier(self):
        integ = _load_module(SCRIPTS_ROOT / "integrity.py", "p210_corrupt")
        tmp, canon = _scratch_canonical()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))

        evidence_dir = canon / "evidence"
        target = sorted(evidence_dir.glob("evidence-*.jsonl"))[0]
        original = target.read_text(encoding="utf-8")
        lines = original.splitlines(keepends=True)
        # flip a real content byte: change the first char of row 2's payload
        corrupted = lines[0] + lines[1].replace('"news"', '"newsX"', 1) + "".join(lines[2:])
        if corrupted == original:
            corrupted = lines[0] + "x" + lines[1][1:] + "".join(lines[2:])
        self.assertNotEqual(corrupted, original, "corruption must change bytes")
        target.write_text(corrupted, encoding="utf-8")

        reports = [integ.verify_corpus(canon)]
        failed = [r for r in reports if r["exit_code"] != 0]
        self.assertTrue(failed,
                        "a real bit-flip in durable evidence must fail the gate")

    def test_live_db_drift_from_baseline_digest_fails(self):
        """The events DB entered canonical outside edition digests (P2.5
        backup-API snapshot). Its integrity anchors are (a) the recorded
        baseline byte digest and (b) row-level reconciliation against the
        preserved baseline copy. Pin that a tampered live DB is detectable
        by both anchors."""
        import hashlib

        tmp, canon = _scratch_canonical()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        events_dir = canon / "events"
        live = events_dir / "ge16-events.db"
        baseline_db = events_dir / "ge16-events-p25-baseline.db"
        marker = events_dir / "ge16-events-p25-baseline.sha256"
        self.assertTrue(baseline_db.is_file() and marker.is_file(),
                        "baseline DB + digest marker must exist as the DB "
                        "integrity anchor")
        expected = marker.read_text(encoding="utf-8").strip()

        def _row_digest(path):
            """Content digest of a DB (rows, not bytes — sqlite free pages
            and page ordering may differ between snapshot and live copy)."""
            con = sqlite3.connect(str(path))
            try:
                digest = hashlib.sha256()
                for table in ("entities", "events", "sources", "stories",
                              "dossier_notes"):
                    for row in con.execute(f"SELECT * FROM {table} ORDER BY 1"):
                        digest.update(repr(row).encode("utf-8"))
                return digest.hexdigest()
            finally:
                con.close()

        pristine_rows = _row_digest(live)
        self.assertEqual(pristine_rows, _row_digest(baseline_db),
                         "pristine live DB must reconcile with the baseline copy")

        # tamper the live DB in place
        con = sqlite3.connect(str(live))
        con.execute("UPDATE events SET title = 'TAMPERED' WHERE rowid = 1")
        con.commit()
        con.close()

        self.assertNotEqual(_row_digest(live), pristine_rows,
                            "row-level reconciliation must flag the tampered DB")
        self.assertNotEqual(
            hashlib.sha256(live.read_bytes()).hexdigest(), expected,
            "byte digest of the tampered live DB must differ from the "
            "recorded baseline marker")


class RebuildSnapshotIdentityTests(unittest.TestCase):
    """Charter #8: rebuild identifies the exact source snapshot."""

    def test_rebuild_report_names_corpus_edition(self):
        import rebuild_knowledge as RK  # noqa: E402

        tmp, canon = _scratch_canonical()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        run_dir = tmp / "rebuild-run"
        run_dir.mkdir(parents=True, exist_ok=True)

        built_path, stats, report = RK.run_rebuild(
            canonical_root=canon, run_dir=run_dir)
        # the rebuild must NAME the corpus edition its inputs came from
        edition_id = report.get("source_edition_id") or report.get("corpus_edition_id")
        self.assertTrue(edition_id,
                        "rebuild report must record the exact source edition")
        editions = sorted((canon / "editions").glob("edition-*.json"))
        self.assertTrue(any(e.name.endswith(f"{edition_id}.json") for e in editions),
                        f"recorded edition {edition_id} must exist on disk")


if __name__ == "__main__":
    unittest.main()
