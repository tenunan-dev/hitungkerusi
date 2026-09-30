#!/usr/bin/env python3
"""Populate data/canonical/links/ from judgments (P2.8 §1.2).

Three JSONL files, append-only-shaped (each rebuild writes a complete,
deterministically-ordered replacement — the same discipline as the P2.7
decisions log, just without the resume machinery since a links rebuild is
a pure function of the current canonical snapshot):

  evidence-entity.jsonl  one row per (evidence_id, entity_id) mention found
                         in accepted-news payload fields (blocs/parties/seats).
  entity-entity.jsonl    co-occurrence: two entities mentioned in the same
                         judged evidence item.
  seat-state.jsonl       seat -> state, derived from the 222-seat federal
                         results + DUN crosswalk already in canonical.

Evidence-entity and entity-entity rows carry evidence_id/judgment_id
citations plus the edition_id of the snapshot they were built from, per
packet §1.2. Seat-state rows are structural crosswalk (P2.6 derivation
outputs): they cite source_file + edition_id instead — no judgment exists
behind a seat's state membership (R1 MINOR-5 docstring correction).
"""
from __future__ import annotations

import json
import os
from itertools import combinations
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parent
CANONICAL_ROOT = SCRIPTS_ROOT.parent / "canonical"
LINKS_DIR = CANONICAL_ROOT / "links"

ROLE_BY_FIELD = {"blocs": "bloc", "parties": "actor", "seats": "seat"}


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


def _load_seed_entities(canonical_root):
    path = Path(canonical_root) / "entities" / "entities.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    return {entity["label"].upper(): entity["entity_id"] for entity in document["entities"]}


def _accepted_news_with_judgment(canonical_root):
    evidence_rows = _load_jsonl_dir(Path(canonical_root) / "evidence", "evidence-")
    judgment_rows = _load_jsonl_dir(Path(canonical_root) / "judgments", "judgment-")
    # R1 MINOR-5: latest accept wins (first-set-wins on sorted iteration is
    # wrong when multiple accept rows exist) — overwrite so the LAST accept
    # in file order becomes the citation, matching the polls store.
    judgment_by_evidence = {}
    for row in judgment_rows:
        if row["verdict"] == "accept":
            judgment_by_evidence[row["evidence_id"]] = row["judgment_id"]
    news = [(row, judgment_by_evidence[row["evidence_id"]])
           for row in evidence_rows if row["kind"] == "news" and row["evidence_id"] in judgment_by_evidence]
    news.sort(key=lambda pair: pair[0]["evidence_id"])
    return news


def build_evidence_entity_links(canonical_root, edition_id):
    entities_by_label = _load_seed_entities(canonical_root)
    rows = []
    for evidence_row, judgment_id in _accepted_news_with_judgment(canonical_root):
        payload = evidence_row.get("payload", {})
        for field, role in ROLE_BY_FIELD.items():
            for value in payload.get(field, []) or []:
                entity_id = entities_by_label.get(str(value).upper())
                if entity_id is None:
                    continue
                rows.append({
                    "evidence_id": evidence_row["evidence_id"],
                    "entity_id": entity_id,
                    "role": role,
                    "mention": value,
                    "judgment_id": judgment_id,
                    "edition_id": edition_id,
                })
    rows.sort(key=lambda row: (row["evidence_id"], row["entity_id"], row["role"]))
    return rows


def build_entity_entity_links(canonical_root, edition_id):
    entities_by_label = _load_seed_entities(canonical_root)
    rows = []
    for evidence_row, judgment_id in _accepted_news_with_judgment(canonical_root):
        payload = evidence_row.get("payload", {})
        mentioned = set()
        for field in ROLE_BY_FIELD:
            for value in payload.get(field, []) or []:
                entity_id = entities_by_label.get(str(value).upper())
                if entity_id is not None:
                    mentioned.add(entity_id)
        for left, right in combinations(sorted(mentioned), 2):
            rows.append({
                "entity_id_a": left, "entity_id_b": right,
                "evidence_id": evidence_row["evidence_id"], "judgment_id": judgment_id,
                "edition_id": edition_id,
            })
    rows.sort(key=lambda row: (row["entity_id_a"], row["entity_id_b"], row["evidence_id"]))
    return rows


def build_seat_state_links(canonical_root, edition_id):
    """Derive seat -> state from REAL canonical inputs (P2.8 R1 MINOR-4
    remediation: the previous guessed paths don't exist):

    1. ``research/states/*/federal-election-results-latest.csv`` — the 13
       per-state files from the P2.6 federal-results derivation, each row
       carrying the seat + its state;
    2. ``research/derived/dun_to_parliament_mapping.json`` — the 600-row
       DUN↔parliament crosswalk (DUN state membership comes from the
       per-state DUN dirs' own files).

    An empty result raises (loud), per the packet's "empty selector is a
    broken selector" rule."""
    rows = []
    seen = set()
    states_dir = Path(canonical_root) / "research" / "states"
    for state_dir in sorted(states_dir.iterdir()) if states_dir.is_dir() else []:
        if not state_dir.is_dir():
            continue
        state = state_dir.name.replace("DUN ", "") if state_dir.name.startswith("DUN ") \
            else state_dir.name
        federal_csv = state_dir / "federal-election-results-latest.csv"
        if not federal_csv.is_file():
            continue
        import csv
        with open(federal_csv, encoding="utf-8-sig", newline="") as handle:
            for entry in csv.DictReader(handle):
                seat_code = entry.get("seat_code") or entry.get("code") or entry.get("seat")
                state_value = entry.get("state") or state
                if seat_code and state_value and seat_code not in seen:
                    seen.add(seat_code)
                    rows.append({"seat_code": seat_code, "state": state_value,
                                 "source_file": federal_csv.relative_to(
                                     canonical_root).as_posix(),
                                 "edition_id": edition_id})
    if not rows:
        raise RuntimeError(
            "seat-state link build found ZERO rows — the P2.6 per-state "
            "federal-results files are missing or unreadable; a silent "
            "empty crosswalk is not acceptable (R1 MINOR-4)")
    mapping_path = Path(canonical_root) / "research" / "derived" / "dun_to_parliament_mapping.json"
    if mapping_path.is_file():
        for entry in json.loads(mapping_path.read_text(encoding="utf-8")):
            rows.append({"seat_code": entry["parliament"], "dun": entry["dun"],
                         "state": None, "detail": entry.get("parliament_name", ""),
                         "source_file": mapping_path.relative_to(canonical_root).as_posix(),
                         "edition_id": edition_id})
    rows.sort(key=lambda row: (row["seat_code"], row.get("dun") or ""))
    return rows


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _count_jsonl(directory, prefix):
    total = 0
    directory = Path(directory)
    if not directory.is_dir():
        return 0
    for name in sorted(os.listdir(directory)):
        if name.startswith(prefix) and name.endswith(".jsonl"):
            with open(directory / name, encoding="utf-8") as handle:
                total += sum(1 for line in handle if line.strip())
    return total


def build_links(canonical_root=CANONICAL_ROOT, out_dir=None, edition_id="unknown"):
    out_dir = Path(out_dir) if out_dir else (Path(canonical_root) / "links")
    evidence_entity = build_evidence_entity_links(canonical_root, edition_id)
    entity_entity = build_entity_entity_links(canonical_root, edition_id)
    seat_state = build_seat_state_links(canonical_root, edition_id)
    _write_jsonl(out_dir / "evidence-entity.jsonl", evidence_entity)
    _write_jsonl(out_dir / "entity-entity.jsonl", entity_entity)
    _write_jsonl(out_dir / "seat-state.jsonl", seat_state)
    return {"evidence_entity": len(evidence_entity), "entity_entity": len(entity_entity),
            "seat_state": len(seat_state)}


def _load_sibling(filename, module_name):
    import importlib.util
    path = SCRIPTS_ROOT / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def promote_links(canonical_root=CANONICAL_ROOT, data_root=None, now=None):
    """Stage a links rebuild into a run dir and promote via the existing
    refresh promotion + gate layer (packet §3: new artifacts are staged and
    promoted, never written to canonical directly)."""
    import work_paths
    refresh = _load_sibling("refresh_canonical_data.py", "p28_links_refresh")

    canonical_root = Path(canonical_root)
    run = work_paths.new_run(label="links-rebuild", data_root=data_root, now=now)
    edition_id = refresh._edition_module().prior_edition_id(str(canonical_root / "editions")) or "unknown"
    staged_dir = run.root / "links"
    stats = build_links(canonical_root=canonical_root, out_dir=staged_dir, edition_id=edition_id)

    target_dir = canonical_root / "links"
    target_dir.mkdir(parents=True, exist_ok=True)
    promoted = []
    for name in ("evidence-entity.jsonl", "entity-entity.jsonl", "seat-state.jsonl"):
        staged = staged_dir / name
        digest = refresh._sha256_file(staged)
        target = target_dir / name
        if not (target.is_file() and refresh._sha256_file(target) == digest):
            import shutil
            shutil.copy2(staged, target)
            promoted.append({"file": f"links/{name}", "sha256": digest,
                             "disposition": "links-rebuild-promotion"})
    if not promoted:
        return None, None, [], stats
    new_edition_id, edition_path = refresh.write_promotion_edition(
        run, promoted, unchanged=[], canonical_root=canonical_root, now=now,
        extra_row_counts={"links_evidence_entity_total": stats["evidence_entity"],
                          "links_entity_entity_total": stats["entity_entity"],
                          "links_seat_state_total": stats["seat_state"],
                          "evidence_total": _count_jsonl(canonical_root / "evidence", "evidence-"),
                          "judgments_total": _count_jsonl(canonical_root / "judgments", "judgment-")},
        note="P2.8 links rebuild: %d evidence-entity, %d entity-entity, %d seat-state rows." % (
            stats["evidence_entity"], stats["entity_entity"], stats["seat_state"]))
    reports = refresh.post_promotion_gate(run, new_edition_id, canonical_root)
    return new_edition_id, edition_path, reports, stats


if __name__ == "__main__":
    # P2.8 R1 MINOR-6: CLI stages into a run dir and promotes through the
    # refresh promotion + gate — never writes canonical directly.
    print(json.dumps(promote_links(), indent=2, default=str))
