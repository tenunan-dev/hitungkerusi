#!/usr/bin/env python3
"""Judged poll-observation store (P2.8 §1.4, OD1).

Poll rows come from canonical evidence + judgments where the evidence is a
POLL by the importer's own content classification (``payload.category ==
'poll'`` on a ``kind: 'news'`` evidence row) — never from tracker seen-keys
(a tracker-note payload is bookkeeping, ``{'seen': ...}``, not a poll
observation).

A rebuild that finds ZERO poll rows is a loud failure: V2 tracked polls, so
zero means the selector broke, not that polls vanished from the corpus.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parent
CANONICAL_ROOT = SCRIPTS_ROOT.parent / "canonical"
POLLS_DIR = CANONICAL_ROOT / "polls"
POLL_CATEGORY = "poll"


def _load_jsonl_dir(directory, prefix):
    rows = []
    for name in sorted(os.listdir(directory)):
        if not (name.startswith(prefix) and name.endswith(".jsonl")):
            continue
        with open(directory / name, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows


def select_poll_rows(canonical_root=CANONICAL_ROOT):
    """Poll-observation rows with provenance: pollster (best-effort from
    ``payload.source``), fieldwork/published date, headline numbers if the
    payload carries them, evidence_id + judgment_id, source URL. V3
    evidence rows currently carry only lightly-structured poll payloads (no
    per-bloc numeric breakdown field yet) — ``support`` is populated when
    present, otherwise recorded null (not fabricated)."""
    canonical_root = Path(canonical_root)
    evidence_rows = _load_jsonl_dir(canonical_root / "evidence", "evidence-")
    judgment_rows = _load_jsonl_dir(canonical_root / "judgments", "judgment-")
    judgment_by_evidence = {row["evidence_id"]: row["judgment_id"]
                            for row in judgment_rows if row["verdict"] == "accept"}
    rows = []
    for evidence_row in evidence_rows:
        if evidence_row["kind"] != "news":
            continue
        payload = evidence_row.get("payload", {})
        if payload.get("category") != POLL_CATEGORY:
            continue
        judgment_id = judgment_by_evidence.get(evidence_row["evidence_id"])
        if judgment_id is None:
            continue
        rows.append({
            "evidence_id": evidence_row["evidence_id"],
            "judgment_id": judgment_id,
            "pollster": payload.get("source"),
            "fieldwork_date": payload.get("date"),
            "headline_title": payload.get("title"),
            "support": payload.get("support"),
            "blocs": payload.get("blocs") or [],
            "source_url": payload.get("link") or payload.get("url") or "",
        })
    rows.sort(key=lambda row: row["evidence_id"])
    return rows


def build_polls_store(canonical_root=CANONICAL_ROOT, out_path=None):
    out_path = Path(out_path) if out_path else (Path(canonical_root) / "polls" / "poll-observations.jsonl")
    rows = select_poll_rows(canonical_root)
    if not rows:
        raise RuntimeError(
            "polls store rebuild found ZERO poll rows — the selector is broken "
            "(V2 tracked polls; this is a loud failure, not a quiet empty result)")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(tmp, out_path)
    return len(rows)


def _load_sibling(filename, module_name):
    import importlib.util
    path = SCRIPTS_ROOT / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def promote_polls(canonical_root=CANONICAL_ROOT, data_root=None, now=None):
    """Stage a polls-store rebuild into a run dir and promote via the
    existing refresh promotion + gate layer."""
    import work_paths
    refresh = _load_sibling("refresh_canonical_data.py", "p28_polls_refresh")

    canonical_root = Path(canonical_root)
    run = work_paths.new_run(label="polls-rebuild", data_root=data_root, now=now)
    staged = run.root / "polls" / "poll-observations.jsonl"
    count = build_polls_store(canonical_root=canonical_root, out_path=staged)

    target = canonical_root / "polls" / "poll-observations.jsonl"
    digest = refresh._sha256_file(staged)
    if target.is_file() and refresh._sha256_file(target) == digest:
        return None, None, [], count
    target.parent.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copy2(staged, target)
    promoted = [{"file": "polls/poll-observations.jsonl", "sha256": digest,
                "disposition": "polls-rebuild-promotion"}]
    # P2.8 R1 MAJOR-1 remediation: publish corpus totals alongside derived
    # totals so the verify-recorded carry-forward baselines this edition.
    evidence_total = _count_jsonl(canonical_root / "evidence", "evidence-")
    judgments_total = _count_jsonl(canonical_root / "judgments", "judgment-")
    edition_id, edition_path = refresh.write_promotion_edition(
        run, promoted, unchanged=[], canonical_root=canonical_root, now=now,
        extra_row_counts={"poll_observations_total": count,
                          "evidence_total": evidence_total,
                          "judgments_total": judgments_total},
        note="P2.8 judged polls store rebuild: %d poll-observation rows." % count)
    reports = refresh.post_promotion_gate(run, edition_id, canonical_root)
    return edition_id, edition_path, reports, count


def _count_jsonl(directory, prefix):
    total = 0
    if not directory.is_dir():
        return 0
    for name in sorted(os.listdir(directory)):
        if name.startswith(prefix) and name.endswith(".jsonl"):
            with open(directory / name, encoding="utf-8") as handle:
                total += sum(1 for line in handle if line.strip())
    return total


if __name__ == "__main__":
    # P2.8 R1 MINOR-6: the CLI never writes canonical directly — it stages
    # into a run dir and promotes through the refresh promotion + gate.
    edition_id, edition_path, reports, count = promote_polls()
    print(json.dumps({"poll_rows": count, "edition_id": edition_id,
                      "edition_path": str(edition_path) if edition_path else None},
                     indent=2))
