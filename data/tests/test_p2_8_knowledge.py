"""P2.8 knowledge-layer rebuild tests: events DB, links, vectors, polls."""
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import rebuild_knowledge as RK  # noqa: E402
import links_build as LB  # noqa: E402
import vector_store as VS  # noqa: E402
import vectors_build as VB  # noqa: E402
import polls_store as PS  # noqa: E402

REAL_CANONICAL = SCRIPTS.parent / "canonical"


def _scratch_canonical():
    tmp = Path(tempfile.mkdtemp())
    shutil.copytree(REAL_CANONICAL, tmp / "canonical")
    return tmp, tmp / "canonical"


class RebuildDeterminismTests(unittest.TestCase):
    def test_two_rebuilds_are_row_identical(self):
        tmp, canon = _scratch_canonical()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        from datetime import datetime, timezone
        pinned = datetime(2026, 9, 30, tzinfo=timezone.utc)
        path1, stats1, _ = RK.run(canonical_root=canon, run_dir=tmp / "run1", now=pinned)
        path2, stats2, _ = RK.run(canonical_root=canon, run_dir=tmp / "run2", now=pinned)
        self.assertEqual(stats1, stats2)
        con1, con2 = sqlite3.connect(str(path1)), sqlite3.connect(str(path2))
        for table in ("sources", "events", "event_sources", "event_entities", "entities"):
            rows1 = sorted(con1.execute(f"SELECT * FROM {table}").fetchall())
            rows2 = sorted(con2.execute(f"SELECT * FROM {table}").fetchall())
            self.assertEqual(rows1, rows2, table)
        con1.close()
        con2.close()


class ReconciliationReportTests(unittest.TestCase):
    def test_v2_only_story_is_explained_not_dropped(self):
        tmp, canon = _scratch_canonical()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        baseline_path, _ = RK.ensure_baseline(canon / "events")
        built_path, _ = RK.rebuild(canon, out_path=tmp / "fresh.db")
        report = RK.reconcile(baseline_path, built_path)
        self.assertEqual(report["unexplained_removals"], [])
        stories = report["tables"]["stories"]
        keys = [row["key"] for row in stories["removed"]]
        self.assertIn("story-015c1fa48128", keys)
        for row in stories["removed"]:
            self.assertEqual(row["reason_class"], "v2_only_no_v3_evidence")


class LinksBuildTests(unittest.TestCase):
    def test_links_deterministic_and_cited(self):
        tmp, canon = _scratch_canonical()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        out1 = tmp / "links1"
        out2 = tmp / "links2"
        stats1 = LB.build_links(canonical_root=canon, out_dir=out1, edition_id="edition-test")
        stats2 = LB.build_links(canonical_root=canon, out_dir=out2, edition_id="edition-test")
        for name in ("evidence-entity.jsonl", "entity-entity.jsonl", "seat-state.jsonl"):
            bytes1 = (out1 / name).read_bytes()
            bytes2 = (out2 / name).read_bytes()
            self.assertEqual(bytes1, bytes2, name)
        rows = [json.loads(line) for line in (out1 / "evidence-entity.jsonl").read_text().splitlines() if line]
        self.assertGreater(len(rows), 0)
        for row in rows[:50]:
            self.assertIn("evidence_id", row)
            self.assertIn("edition_id", row)


class VectorStoreTests(unittest.TestCase):
    def test_round_trip_and_top_k_self_match(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        store_path = tmp / "vectors.db"

        def stub_embed(texts):
            import hashlib as h
            import struct
            vectors = []
            for text in texts:
                digest = h.sha256(text.encode("utf-8")).digest()
                vectors.append([b / 255.0 for b in digest[:16]])
            return vectors

        store = VS.VectorStore(store_path)
        rows = [{"id": "a", "text": "budget 2027 tourism allocation"},
                {"id": "b", "text": "BN melaka polls cooperation"}]
        store.write_collection("news", rows, embed_fn=stub_embed,
                              model_name="stub", model_version="1", edition_id="edition-test")
        hits = store.top_k("news", stub_embed(["budget 2027 tourism allocation"])[0], k=1)
        self.assertEqual(hits[0]["id"], "a")
        meta = store.collection_metadata("news")
        self.assertEqual(meta["model_name"], "stub")
        self.assertEqual(meta["row_count"], 2)


class VectorsBuildTests(unittest.TestCase):
    def test_build_uses_stub_and_no_network(self):
        tmp, canon = _scratch_canonical()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))

        def stub_embed(texts):
            return [[float(len(t) % 7), 1.0, 0.0] for t in texts]

        store_path = tmp / "vectors.db"
        stats = VB.build_vectors(canonical_root=canon, store_path=store_path,
                                 embed_fn=stub_embed, model_name="stub", model_version="0")
        self.assertGreater(stats["news"], 0)


class PollsStoreTests(unittest.TestCase):
    def test_selector_finds_poll_rows_on_live_corpus(self):
        rows = PS.select_poll_rows(canonical_root=REAL_CANONICAL)
        self.assertGreater(len(rows), 0)
        for row in rows[:20]:
            self.assertIn("evidence_id", row)
            self.assertIn("judgment_id", row)
            self.assertIn("source_url", row)


class PromotionWiringTests(unittest.TestCase):
    """§1.2/§1.3/§1.4 artifacts stage into a run dir and promote via the
    existing refresh promotion + post-promotion gate — never written to
    canonical directly."""

    def test_promote_links(self):
        tmp, canon = _scratch_canonical()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        edition_id, edition_path, reports, stats = LB.promote_links(canonical_root=canon, data_root=tmp)
        self.assertIsNotNone(edition_id)
        for report in reports:
            self.assertEqual(report["exit_code"], 0)
        self.assertTrue((canon / "links" / "evidence-entity.jsonl").is_file())

    def test_promote_polls(self):
        tmp, canon = _scratch_canonical()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        edition_id, edition_path, reports, count = PS.promote_polls(canonical_root=canon, data_root=tmp)
        self.assertIsNotNone(edition_id)
        for report in reports:
            self.assertEqual(report["exit_code"], 0)
        self.assertGreater(count, 0)

    def test_promote_vectors(self):
        tmp, canon = _scratch_canonical()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))

        def stub_embed(texts):
            return [[float(len(t) % 5), 1.0] for t in texts]

        edition_id, edition_path, reports, stats = VB.promote_vectors(
            canonical_root=canon, data_root=tmp, embed_fn=stub_embed)
        self.assertIsNotNone(edition_id)
        for report in reports:
            self.assertEqual(report["exit_code"], 0)
        self.assertTrue((canon / "vectors" / "vectors.db").is_file())


class OrphanedFlagSupersedeSelectorTests(unittest.TestCase):
    def test_selector_excludes_already_superseded(self):
        import judge_runs as JR
        tmp, canon = _scratch_canonical()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        items, total, disagree = JR.select_orphaned_flag_subset(canon)
        if not items:
            self.skipTest("no no-surviving-batch items in current corpus")
        target = items[0]
        judgments_dir = canon / "judgments"
        nibble = target["judgment_id"][2]
        path = judgments_dir / f"judgment-{nibble}.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines() if line]
        fake_row = dict(rows[0])
        fake_row["judgment_id"] = "jd" + "0" * 62
        fake_row["evidence_id"] = target["evidence_id"]
        fake_row["verdict"] = "accept"
        fake_row["confidence_note"] = f"P2.7 re-judge: verified — probe ok (supersedes {target['judgment_id']})"
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(fake_row) + "\n")
        items_after, _, _ = JR.select_orphaned_flag_subset(canon, include_already_superseded=False)
        remaining_ids = {item["judgment_id"] for item in items_after}
        self.assertNotIn(target["judgment_id"], remaining_ids)
        items_all, _, _ = JR.select_orphaned_flag_subset(canon, include_already_superseded=True)
        all_ids = {item["judgment_id"] for item in items_all}
        self.assertIn(target["judgment_id"], all_ids)


class OrphanedFlagPromoteE2ETests(unittest.TestCase):
    """P2.8 F5: sandboxed end-to-end promote test (tmp corpus copy -> complete
    orphaned-flag run with a stub prober -> promote -> post_promotion_gate
    exit 0 -> promoted row carries the supersede note)."""

    def test_e2e_promote(self):
        import work_paths
        import judge_runs as JR

        tmp, canon = _scratch_canonical()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        data_root = tmp

        def stub_prober(url, timeout=8):
            return {"http_status": 200, "final_url": url, "error": None}

        session, summary = JR.run_orphaned_flag_pass(
            canonical_root=canon, prober=stub_prober, data_root=data_root)
        self.assertTrue(session.state["complete"])
        self.assertEqual(summary["counts"]["pending"], 0)

        edition_id, edition_path, reports = JR.promote_orphaned_flag_run(
            session, canonical_root=canon)
        self.assertIsNotNone(edition_id)
        for report in reports:
            self.assertEqual(report["exit_code"], 0)

        judgment_rows = JR._load_judgment_rows(canon)
        superseded_notes = [row for row in judgment_rows
                            if row.get("basis", {}).get("origin") == "orphaned-flag"
                            and "supersedes" in (row.get("confidence_note") or "")]
        self.assertGreater(len(superseded_notes), 0)

    def test_selector_ignores_unresolved_supersede_links(self):
        """P2.8 R2-N1 selector-level negative test: an UNRESOLVED decision
        envelope may carry a supersedes link in the work dir, but the
        selector must still return that item — unresolved never supersedes
        (owner ruling)."""
        import judge_runs as JR  # noqa: E402
        import shutil
        tmp, canon = _scratch_canonical()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        data_root = tmp / "data"

        def timeout_prober(url, timeout=8):
            return {"http_status": None, "final_url": url,
                    "error": "timeout after 8s"}

        session, summary = JR.run_orphaned_flag_pass(
            canonical_root=canon, prober=timeout_prober, data_root=data_root)
        # every decision is unresolved; nothing resolved, nothing complete
        self.assertEqual(summary["counts"]["unresolved"],
                         summary["total_items"])

        # a second pass MUST find the same items again (unresolved
        # decisions do not remove items from the re-judge target)
        session2, summary2 = JR.run_orphaned_flag_pass(
            canonical_root=canon, prober=timeout_prober, data_root=data_root)
        self.assertEqual(summary2["total_items"], summary["total_items"],
                         "unresolved decisions must NOT shrink the target set")


# P2.8 R1 MAJOR-3 remediation: additive merge proven on a sandbox copy of
# the REAL canonical DB — origin-tagged co-existence (v2_baseline rows kept
# verbatim, v3_rebuild rows added), V2 row count invariant across merge,
# and second-promotion no_changes.
class AdditiveMergeTests(unittest.TestCase):
    # R2-1: the pre-ALTER helper was REMOVED — the tests below deliberately
    # run against the raw committed baseline (no origin column) so the
    # merge's own ensure-origin path is what gets exercised.

    def test_merge_preserves_v2_and_adds_v3(self):
        import shutil
        import sqlite3

        import rebuild_knowledge as RK

        tmp, canon = _scratch_canonical()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        # R2-1: NO pre-ALTER here — the committed baseline lacks the origin
        # column and merge_additive must tag it. Capture the V2 counts from
        # a read-only connection first.
        import sqlite3 as _sq
        con0 = _sq.connect(str(canon / "events" / "ge16-events.db"))
        v2_entities, v2_events = con0.execute(
            "SELECT (SELECT COUNT(*) FROM entities), (SELECT COUNT(*) FROM events)"
        ).fetchone()
        con0.close()

        edition_id, _, reports, stats, report = RK.promote_rebuild(canonical_root=canon)
        self.assertIsNotNone(edition_id)
        for r in reports:
            self.assertEqual(r["exit_code"], 0)

        con = sqlite3.connect(str(canon / "events" / "ge16-events.db"))
        # R2-1: the merged DB must carry origin columns on BOTH tables
        entity_cols = [c[1] for c in con.execute("PRAGMA table_info(entities)")]
        event_cols = [c[1] for c in con.execute("PRAGMA table_info(events)")]
        self.assertIn("origin", entity_cols)
        self.assertIn("origin", event_cols)
        self.assertEqual(
            con.execute("SELECT COUNT(*) FROM events WHERE origin='v2_baseline'").fetchone()[0],
            v2_events, "every V2 event row must survive the merge verbatim")
        self.assertEqual(
            con.execute("SELECT COUNT(*) FROM entities WHERE origin='v2_baseline'").fetchone()[0],
            v2_entities, "every V2 entity row must survive the merge verbatim")
        v3_events = con.execute(
            "SELECT COUNT(*) FROM events WHERE origin='v3_rebuild'").fetchone()[0]
        self.assertGreater(v3_events, 0, "rebuild must contribute v3_rebuild rows")
        total_events = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.assertEqual(total_events, v2_events + v3_events)
        con.close()
        self.assertIn("retained_v2_collisions", report)
        self.assertIn("retained_v2_rows_total", report)
        self.assertIn("additive", report.get("merge", ""))

    def test_second_promotion_is_no_changes(self):
        import shutil

        import rebuild_knowledge as RK

        tmp, canon = _scratch_canonical()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        # R2-1: no pre-ALTER — the merge itself must handle the untagged
        # committed baseline (and the second run's no_changes must hold on
        # the tagged result).
        first, _, _, _, _ = RK.promote_rebuild(canonical_root=canon)
        self.assertIsNotNone(first)
        second, _, _, _, _ = RK.promote_rebuild(canonical_root=canon)
        self.assertIsNone(second, "unchanged input must produce a no_changes promotion")


class EntityCandidatesTotalTests(unittest.TestCase):
    """P2.8 F6: promotion edition row_counts include entity_candidates_total."""

    def test_promotion_edition_includes_entity_candidates_total(self):
        import json as _json
        import judge_runs as JR

        tmp, canon = _scratch_canonical()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))

        def stub_prober(url, timeout=8):
            return {"http_status": 200, "final_url": url, "error": None}

        session, _ = JR.run_orphaned_flag_pass(canonical_root=canon, prober=stub_prober, data_root=tmp)
        edition_id, edition_path, _ = JR.promote_orphaned_flag_run(session, canonical_root=canon)
        manifest = _json.loads(Path(edition_path).read_text())
        self.assertIn("entity_candidates_total", manifest["row_counts"])
        self.assertGreater(manifest["row_counts"]["entity_candidates_total"], 0)


class WrapperHostnameTests(unittest.TestCase):
    def test_hostname_not_path_segment(self):
        import judge_runs as JR
        self.assertFalse(JR._is_wrapper("https://example.com/news.google.com/article"))
        self.assertTrue(JR._is_wrapper("https://news.google.com/rss/articles/xyz"))


if __name__ == "__main__":
    unittest.main()
