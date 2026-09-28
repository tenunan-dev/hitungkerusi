#!/usr/bin/env python3
"""Entity registry machinery: controlled-vocab seed + auto-proposed candidates.

Owner-ruled (P2.2 brief decision 3): import scans judgment fields
(``blocs``/``parties``/``seats``) and evidence payloads (``source``); any
string that survives alias normalization without matching the controlled
vocabulary becomes a row in ``entity-candidates.jsonl``. Nothing auto-enters
``entities.json`` — promotion happens only through this CLI after owner
review, and every promotion records ``approved_by``/``approved_at``/
``approved_from_candidate``. Unmatched strings keep their raw values in the
judgment rows; candidates never block an import.

Usage:
  python3 entity_candidates.py scan   [--data-root DIR]
  python3 entity_candidates.py list   [--status proposed] [--data-root DIR]
  python3 entity_candidates.py approve <candidate_id> --by OWNER [--data-root DIR]
  python3 entity_candidates.py reject  <candidate_id> --by OWNER [--data-root DIR]
"""
import argparse
import datetime
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import identity  # noqa: E402

CANDIDATE_SCHEMA = "ge16.entity-candidate.v1"
ENTITY_SCHEMA = "ge16.entity.v1"
ENTITIES_REL = os.path.join("canonical", "entities")
FIELD_TYPES = {"blocs": "bloc", "parties": "party", "seats": "seat", "source": "source"}
SLUG_SAFE = re.compile(r"[^a-z0-9-]+")


def _utcnow_iso():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def load_seed(data_root):
    path = os.path.join(data_root, ENTITIES_REL, "seed-vocabulary.json")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def normalized_form(value):
    return (value or "").strip().casefold()


def build_matcher(seed):
    """normalized string -> canonical label, from labels + alias keys."""
    table = {}
    for field in ("blocs", "parties", "seats", "sources"):
        for label in seed.get(field) or []:
            table[normalized_form(label)] = label
    for alias, mapping in (seed.get("aliases") or {}).items():
        table[normalized_form(alias)] = mapping["canonical"]
    return table


def slugify(label):
    slug = SLUG_SAFE.sub("-", label.strip().casefold()).strip("-")
    return slug or "x"


def load_candidates(data_root):
    path = os.path.join(data_root, ENTITIES_REL, "entity-candidates.jsonl")
    rows = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows, path


def write_candidates(path, rows):
    """Deterministic serialization: sorted by candidate_id, stable fields."""
    rows = sorted(rows, key=lambda row: row["candidate_id"])
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")) + "\n")


def scan_sightings(sightings, seed, existing_rows, now_iso):
    """Merge ``sightings`` {(type, string): {evidence_id, ...}} into candidate rows.

    Existing rows keep their ``proposed_at`` and ``status``; occurrences and
    ``seen_in`` refresh; new rows are appended with ``status: proposed``.
    """
    by_id = {row["candidate_id"]: row for row in existing_rows}
    for (proposed_type, string), evidence_ids in sorted(sightings.items()):
        cid = identity.candidate_id(string, proposed_type)
        evidence_ids = sorted(evidence_ids)
        if cid in by_id:
            row = by_id[cid]
            if row.get("status") == "proposed":
                merged = set(row.get("seen_in") or []) | set(evidence_ids)
                row["seen_in"] = sorted(merged)
                row["occurrences"] = max(int(row.get("occurrences", 1)), len(merged))
            continue
        by_id[cid] = {
            "schema": CANDIDATE_SCHEMA,
            "candidate_id": cid,
            "candidate_string": string,
            "proposed_type": proposed_type,
            "seen_in": evidence_ids,
            "occurrences": len(evidence_ids),
            "proposed_at": now_iso,
            "status": "proposed",
        }
    return sorted(by_id.values(), key=lambda row: row["candidate_id"])


def collect_sightings_from_rows(evidence_rows, judgment_rows, matcher):
    """Sightings dict from in-memory canonical rows (importer path)."""
    sightings = {}
    for row in judgment_rows:
        for field, proposed_type in FIELD_TYPES.items():
            if field == "source":
                continue
            for value in row.get(field) or []:
                if not (value or "").strip():
                    continue
                if normalized_form(value) in matcher:
                    continue
                sightings.setdefault((proposed_type, value.strip()), set()).add(row["evidence_id"])
    for row in evidence_rows:
        if row.get("kind") != "news":
            continue
        value = (row.get("payload") or {}).get("source")
        if not value or normalized_form(value) in matcher:
            continue
        sightings.setdefault(("source", value.strip()), set()).add(row["evidence_id"])
    return sightings


def load_canonical_rows(data_root):
    """Read evidence/judgment JSONL shards back into lists (standalone scan path)."""
    evidence_rows, judgment_rows = [], []
    for sub, prefix, sink in (("evidence", "evidence-", evidence_rows),
                              ("judgments", "judgment-", judgment_rows)):
        directory = os.path.join(data_root, "canonical", sub)
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            if not (name.startswith(prefix) and name.endswith(".jsonl")):
                continue
            with open(os.path.join(directory, name), encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        sink.append(json.loads(line))
    return evidence_rows, judgment_rows


def entities_registry_path(data_root):
    return os.path.join(data_root, ENTITIES_REL, "entities.json")


def seed_entities_registry(data_root, seed):
    """Initialize entities.json from the seed vocabulary (seed rows are the
    approved baseline, not auto-promotions; candidate promotion is the only
    other writer). Idempotent: an existing registry is left untouched."""
    path = entities_registry_path(data_root)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            return json.load(handle), False
    entities = []
    for field, entity_type in (("blocs", "bloc"), ("parties", "party"),
                               ("seats", "seat"), ("sources", "source")):
        for label in seed.get(field) or []:
            entities.append({
                "schema": ENTITY_SCHEMA,
                "entity_id": slugify(label),
                "type": entity_type,
                "label": label,
                "first_seen": seed.get("created") or "",
                "notes": "Seed vocabulary entry (P2.2).",
            })
    registry = {"schema": "ge16.entity-registry.v1",
                "seeded_from": "seed-vocabulary.json",
                "entities": sorted(entities, key=lambda row: (row["type"], row["entity_id"]))}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(registry, handle, indent=2, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
    return registry, True


def run_scan(data_root, evidence_rows=None, judgment_rows=None, now_iso=None):
    """Scan + rewrite candidates; returns (candidates, matcher, counts)."""
    seed = load_seed(data_root)
    matcher = build_matcher(seed)
    existing, path = load_candidates(data_root)
    if evidence_rows is None or judgment_rows is None:
        evidence_rows, judgment_rows = load_canonical_rows(data_root)
    sightings = collect_sightings_from_rows(evidence_rows, judgment_rows, matcher)
    rows = scan_sightings(sightings, seed, existing, now_iso or _utcnow_iso())
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_candidates(path, rows)
    return rows, matcher, {"candidates": len(rows),
                           "proposed": sum(1 for r in rows if r["status"] == "proposed")}


def _set_status(data_root, candidate_id, status, owner):
    rows, path = load_candidates(data_root)
    target = None
    for row in rows:
        if row["candidate_id"] == candidate_id:
            target = row
            break
    if target is None:
        raise SystemExit(f"no such candidate: {candidate_id}")
    if target["status"] != "proposed":
        raise SystemExit(f"candidate {candidate_id} already {target['status']}")
    target["status"] = status
    target["approved_by" if status == "approved" else "rejected_by"] = owner
    target[f"{status}_at"] = _utcnow_iso()
    write_candidates(path, rows)
    if status == "approved":
        registry, _ = seed_entities_registry(data_root, load_seed(data_root))
        entity = {
            "schema": ENTITY_SCHEMA,
            "entity_id": slugify(target["candidate_string"]),
            "type": target["proposed_type"],
            "label": target["candidate_string"],
            "first_seen": target["proposed_at"],
            "approved_by": owner,
            "approved_at": target["approved_at"],
            "approved_from_candidate": candidate_id,
        }
        if any(e["entity_id"] == entity["entity_id"] for e in registry["entities"]):
            raise SystemExit(f"entity_id collision: {entity['entity_id']}")
        registry["entities"].append(entity)
        registry["entities"].sort(key=lambda row: (row["type"], row["entity_id"]))
        with open(entities_registry_path(data_root), "w", encoding="utf-8") as handle:
            json.dump(registry, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
    print(f"candidate {candidate_id} -> {status}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    default_root = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir))
    parser.add_argument("--data-root", default=default_root)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scan")
    list_parser = sub.add_parser("list")
    list_parser.add_argument("--status", default=None)
    approve_parser = sub.add_parser("approve")
    approve_parser.add_argument("candidate_id")
    approve_parser.add_argument("--by", required=True)
    reject_parser = sub.add_parser("reject")
    reject_parser.add_argument("candidate_id")
    reject_parser.add_argument("--by", required=True)
    args = parser.parse_args(argv)

    if args.command == "scan":
        rows, _, counts = run_scan(args.data_root)
        print(f"scan complete: {counts}")
    elif args.command == "list":
        rows, _ = load_candidates(args.data_root)
        for row in rows:
            if args.status and row["status"] != args.status:
                continue
            print(f"{row['candidate_id']}  {row['status']:9} {row['proposed_type']:7} "
                  f"x{row['occurrences']:<4} {row['candidate_string']}")
    elif args.command == "approve":
        _set_status(args.data_root, args.candidate_id, "approved", args.by)
    elif args.command == "reject":
        _set_status(args.data_root, args.candidate_id, "rejected", args.by)


if __name__ == "__main__":
    main()
