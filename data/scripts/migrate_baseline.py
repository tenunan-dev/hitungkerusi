#!/usr/bin/env python3
"""P2.5 — idempotent V2 baseline migration (inventory / stage / promote).

Design brief: evidence/P2/P2.5-design-brief.md. PLAN text: "Import the
verified V2 baseline using an idempotent migration with source/destination
reconciliation and rollback. No blind all-or-nothing database replacement."

Scope, established by ``inventory`` (a real content diff, not an assumption):
the six P1.3 canonical-data-provenance.json roots (research/raw,
research/derived, research/trackers, research/federal, geo, research/states)
are ALREADY fully migrated byte-for-byte (149/149 files identical, 0
missing/changed) — inventory proves this every run rather than asserting it.
The one real gap the design brief names is the events database
(``2_ANALYTICS/work/events/ge16-events.db``, absent from
``data/canonical/events/``); inventory also re-diffs the six roots on every
run so a future V2 change would surface as ``changed``/``missing`` here too.

Every other P0.7-dispositioned asset (scripts, site, ops, tests, figures/vdb,
knowledge-graph, history-preservation, owner-pending decisions) is NOT a V2
baseline DATA asset under those six roots or the events db, so it is recorded
as ``out-of-scope`` with its own P0.7 disposition + reason — never silently
dropped (hard prohibition: every inventory decision is content/disposition
-based and recorded).

Commands:
  inventory            diff V2 sources vs V3 canonical; JSON report, no writes.
  migrate --stage      snapshot-copy missing/changed items into
                        data/work/<run_id>/baseline-stage/, with sha256
                        sidecars and a staged manifest. The events DB is
                        copied via the sqlite3 backup API (consistent
                        snapshot), never a live file copy.
  migrate --promote    verify staged sha256s, then move into canonical.
                        Collision at any destination path = abort, canonical
                        untouched. No-op (no writes at all) when the staged
                        manifest is empty — the idempotency contract.

Edition manifest deviation from the design brief (recorded here per brief
§3: "worker proposes in report; parent adjudicates on review"): the brief
proposes ``schema: "ge16.edition.baseline-migration.v1"`` with
``lineage.kind: "baseline-migration"``. Both the frozen ge16_edition.v1
JSON Schema and integrity.py's structural check (deliberately not modified —
hard prohibition #4) hard-fail on an unrecognized ``schema`` value or an
unlisted ``lineage`` property (``additionalProperties: false``). So this
migration reuses ``schema: "ge16.edition.v1"`` verbatim via the existing
P2.2 ``edition.write_edition`` helper (no duplication, no schema change) and
carries the "this is a baseline migration, not an import" fact in ``note``
plus a ``migration_kind`` marker inside each ``lineage.inputs[].disposition``
string. Fully reviewable by verify-edition/verify-chain unmodified.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parent
DATA_ROOT = SCRIPTS_ROOT.parent
REPOSITORY_ROOT = DATA_ROOT.parent
DEFAULT_V2_ROOT = Path(
    "/Users/faisal.muthalib/Documents/HermesWorkFolder/Malaysia General Election v2")
DEFAULT_CANONICAL_ROOT = DATA_ROOT / "canonical"
DEFAULT_P07_DISPOSITIONS = REPOSITORY_ROOT / "evidence" / "P0" / "P0.7-reuse-dispositions.json"

#: The P1.3 canonical-data-provenance.json roots — identical relative layout
#: on both sides (V2 "1_DATA/<root>/..." <-> V3 "data/canonical/<root>/...").
CANONICAL_ROOTS = ("research/raw", "research/derived", "research/trackers",
                    "research/federal", "geo", "research/states")
V2_DATA_SUBDIR = "1_DATA"

#: The one gap the brief names explicitly.
EVENTS_DB_V2_RELATIVE = Path("2_ANALYTICS") / "work" / "events" / "ge16-events.db"
EVENTS_DB_V3_RELATIVE = Path("events") / "ge16-events.db"

#: Classification of every P0.7 disposition row (60 rows, evidence/P0/
#: P0.7-reuse-dispositions.json) into this packet's scope. "corpus-walk":
#: the row's asset lives inside CANONICAL_ROOTS and is covered by the
#: generic file diff below (content-verified, not assumed). "events-db":
#: row 1f, the one migration target. Everything else is genuinely out of
#: scope for a V2 baseline DATA migration (code, ops, site, tests, figure
#: vector stores, knowledge graph, history preservation, owner-pending
#: non-file decisions) and is recorded verbatim with its own reason.
ROW_CLASSIFICATION = {
    "1a": "corpus-walk", "1b": "corpus-walk", "1c": "corpus-walk",
    "1d": "out-of-scope", "1e": "corpus-walk", "1f": "events-db",
    "1g": "corpus-walk", "1h": "corpus-walk", "1i": "out-of-scope",
    "2a": "out-of-scope", "2b": "out-of-scope", "2c": "out-of-scope",
    "2d": "corpus-walk",
    "3a": "out-of-scope", "3b": "out-of-scope", "3c": "out-of-scope",
    "4a": "out-of-scope", "4b": "out-of-scope", "4c": "out-of-scope",
    "4d": "out-of-scope",
    "5a": "out-of-scope", "5b": "out-of-scope", "5c": "out-of-scope",
    "5d": "out-of-scope", "5e": "out-of-scope", "5f": "out-of-scope",
    "5g": "out-of-scope",
    "6a": "out-of-scope", "6b": "out-of-scope", "6c": "out-of-scope",
    "6d": "out-of-scope", "6e": "out-of-scope", "6f": "out-of-scope",
    "6g": "out-of-scope",
    "7a": "out-of-scope", "7b": "out-of-scope", "7c": "out-of-scope",
    "7d": "out-of-scope",
    "8a": "out-of-scope", "8b": "out-of-scope", "8c": "out-of-scope",
    "8d": "out-of-scope", "8e": "out-of-scope", "8f": "out-of-scope",
    "8g": "out-of-scope",
    "9a": "out-of-scope", "9b": "out-of-scope", "9c": "out-of-scope",
    "10a": "out-of-scope", "10b": "out-of-scope", "10c": "out-of-scope",
    "10d": "out-of-scope", "10e": "out-of-scope",
    "S1": "out-of-scope", "S2": "out-of-scope", "S3": "out-of-scope",
    "S4": "out-of-scope", "S5": "out-of-scope", "S6": "out-of-scope",
    "S7": "out-of-scope",
}

MIGRATION_SCHEMA_NOTE_PREFIX = "P2.5 baseline migration"


def _load_sibling(filename, module_name):
    path = SCRIPTS_ROOT / "import" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_EDITION = _load_sibling("edition.py", "p25_migrate_baseline_edition")

sys.path.insert(0, str(SCRIPTS_ROOT))
try:
    import work_paths  # noqa: E402
    import integrity  # noqa: E402  (P2.4 read-only verifier; gate after promote)
finally:
    sys.path.remove(str(SCRIPTS_ROOT))


# --------------------------------------------------------------------- hashing

def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mtime_ns(path):
    return os.stat(path).st_mtime_ns


# --------------------------------------------------------------- P0.7 loading

def _load_p07_rows(path=DEFAULT_P07_DISPOSITIONS):
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    return document["rows"]


def _p07_out_of_scope(rows):
    """Every row NOT covered by the corpus walk or the events-db migration,
    recorded verbatim with its own P0.7 disposition + reason (prohibition #2:
    no filename-based exclusion — this is a documentation-level scope record,
    the file diff below never excludes a canonical-root file by name)."""
    entries = []
    for row in rows:
        kind = ROW_CLASSIFICATION.get(row["id"])
        if kind is None:
            raise ValueError(f"P0.7 row {row['id']!r} has no ROW_CLASSIFICATION "
                              f"entry — inventory would silently drop it")
        if kind != "out-of-scope":
            continue
        entries.append({
            "p07_id": row["id"], "asset": row["asset"],
            "p07_disposition": row["disposition"],
            "reason": "not a V2 baseline data asset under CANONICAL_ROOTS or the "
                      "events db; " + (row.get("reason") or row["disposition"]),
        })
    return entries


# ------------------------------------------------------- canonical-root diff

def _diff_canonical_roots(v2_root, canonical_root):
    """Content diff of the six canonical-data-provenance roots. Every V2 file
    under these roots gets exactly one disposition; nothing is skipped by
    name."""
    v2_data = Path(v2_root) / V2_DATA_SUBDIR
    canonical_root = Path(canonical_root)
    items = []
    for root in CANONICAL_ROOTS:
        source_dir = v2_data / root
        if not source_dir.is_dir():
            continue
        for current, directories, names in os.walk(source_dir):
            directories.sort()
            for name in sorted(names):
                source_path = Path(current) / name
                relative = source_path.relative_to(v2_data).as_posix()
                destination_path = canonical_root / relative
                source_sha = sha256_file(source_path)
                if not destination_path.is_file():
                    disposition = "missing"
                    destination_sha = None
                else:
                    destination_sha = sha256_file(destination_path)
                    disposition = ("already-present" if destination_sha == source_sha
                                   else "changed")
                items.append({
                    "relative_path": relative,
                    "v2_source": str(source_path),
                    "v3_destination": str(destination_path),
                    "v2_sha256": source_sha,
                    "v3_sha256": destination_sha,
                    "disposition": disposition,
                })
    return items


# ------------------------------------------------------------- events-db diff

def _sqlite_table_counts(db_path):
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        return {table: connection.execute(
            f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in sorted(tables)}
    finally:
        connection.close()


def _sqlite_integrity_check(db_path):
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()


def _diff_events_db(v2_root, canonical_root):
    source_path = Path(v2_root) / EVENTS_DB_V2_RELATIVE
    destination_path = Path(canonical_root) / EVENTS_DB_V3_RELATIVE
    if not source_path.is_file():
        return {"relative_path": EVENTS_DB_V3_RELATIVE.as_posix(),
                "v2_source": str(source_path), "v3_destination": str(destination_path),
                "disposition": "out-of-scope",
                "reason": "V2 source absent (nothing to migrate)"}
    integrity_check = _sqlite_integrity_check(source_path)
    source_sha = sha256_file(source_path)
    table_counts = _sqlite_table_counts(source_path)
    entry = {
        "relative_path": EVENTS_DB_V3_RELATIVE.as_posix(),
        "v2_source": str(source_path), "v3_destination": str(destination_path),
        "v2_sha256": source_sha, "v2_integrity_check": integrity_check,
        "v2_table_counts": table_counts,
    }
    if not destination_path.is_file():
        entry["v3_sha256"] = None
        entry["disposition"] = "missing"
    else:
        # The sqlite3 backup API (used by `stage`, per the design brief) writes
        # a fresh database file: logically identical to the source but NOT
        # byte-identical (page layout is not guaranteed to be preserved), so
        # raw sha256 against the V2 source would spuriously read "changed" on
        # every run forever and break idempotency. Equivalence for an already
        # -migrated snapshot is integrity_check=ok + identical per-table row
        # counts; v3_sha256 is still recorded for corruption detection against
        # the value the edition manifest itself bound at promotion time.
        destination_sha = sha256_file(destination_path)
        destination_integrity = _sqlite_integrity_check(destination_path)
        destination_counts = _sqlite_table_counts(destination_path)
        entry["v3_sha256"] = destination_sha
        entry["v3_integrity_check"] = destination_integrity
        entry["v3_table_counts"] = destination_counts
        entry["disposition"] = ("already-present"
                                 if destination_integrity == "ok"
                                 and destination_counts == table_counts
                                 else "changed")
    return entry


# ------------------------------------------------------------------ inventory

def inventory(v2_root=DEFAULT_V2_ROOT, canonical_root=DEFAULT_CANONICAL_ROOT,
              p07_path=DEFAULT_P07_DISPOSITIONS):
    before_v2 = _v2_snapshot(v2_root)
    rows = _load_p07_rows(p07_path)
    file_items = _diff_canonical_roots(v2_root, canonical_root)
    events_item = _diff_events_db(v2_root, canonical_root)
    out_of_scope = _p07_out_of_scope(rows)
    counts = {"already-present": 0, "missing": 0, "changed": 0, "out-of-scope": 0}
    for item in file_items + [events_item]:
        counts[item["disposition"]] = counts.get(item["disposition"], 0) + 1
    counts["out-of-scope"] += len(out_of_scope)
    after_v2 = _v2_snapshot(v2_root)
    assert before_v2 == after_v2, "V2 read-only invariant violated during inventory"
    return {
        "schema": "ge16.baseline-migration-inventory.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "v2_root": str(v2_root), "canonical_root": str(canonical_root),
        "canonical_roots_walked": list(CANONICAL_ROOTS),
        "file_items": file_items,
        "events_db": events_item,
        "p07_rows_total": len(rows),
        "p07_out_of_scope": out_of_scope,
        "counts": counts,
    }


def _v2_snapshot(v2_root):
    """(path -> (size, mtime_ns)) for every file this tool ever reads from V2,
    used to prove the read-only invariant before/after each command."""
    snapshot = {}
    v2_data = Path(v2_root) / V2_DATA_SUBDIR
    for root in CANONICAL_ROOTS:
        source_dir = v2_data / root
        if not source_dir.is_dir():
            continue
        for current, directories, names in os.walk(source_dir):
            directories.sort()
            for name in names:
                path = Path(current) / name
                stat = os.stat(path)
                snapshot[str(path)] = (stat.st_size, stat.st_mtime_ns)
    events_path = Path(v2_root) / EVENTS_DB_V2_RELATIVE
    if events_path.is_file():
        stat = os.stat(events_path)
        snapshot[str(events_path)] = (stat.st_size, stat.st_mtime_ns)
    return snapshot


# ---------------------------------------------------------------------- stage

def stage(report, canonical_root=DEFAULT_CANONICAL_ROOT, data_root=None, label=""):
    """Snapshot-copy every missing/changed item into a fresh run's
    ``baseline-stage/`` dir, with a per-item sha256 sidecar and a staged
    manifest. The events DB is copied via the sqlite3 backup API — a
    consistent snapshot, never a live file copy. Never touches canonical."""
    data_root = Path(data_root) if data_root else Path(canonical_root).parent
    run = work_paths.new_run(label=label or "P2.5 baseline migration stage",
                             data_root=data_root)
    stage_dir = run.root / "baseline-stage"
    stage_dir.mkdir(parents=True, exist_ok=False)
    to_migrate = [item for item in report["file_items"]
                  if item["disposition"] in ("missing", "changed")]
    if report["events_db"]["disposition"] in ("missing", "changed"):
        to_migrate.append(report["events_db"])
    staged_items = []
    for item in to_migrate:
        relative = item["relative_path"]
        target = stage_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if relative == EVENTS_DB_V3_RELATIVE.as_posix():
            integrity_check = item.get("v2_integrity_check") \
                or _sqlite_integrity_check(item["v2_source"])
            if integrity_check != "ok":
                raise RuntimeError(
                    f"refusing to stage {item['v2_source']}: sqlite integrity_check "
                    f"= {integrity_check!r} (not ok)")
            source_connection = sqlite3.connect(f"file:{item['v2_source']}?mode=ro",
                                                uri=True)
            destination_connection = sqlite3.connect(str(target))
            try:
                source_connection.backup(destination_connection)
            finally:
                destination_connection.close()
                source_connection.close()
            staged_integrity = _sqlite_integrity_check(target)
            if staged_integrity != "ok":
                raise RuntimeError(
                    f"staged snapshot of {item['v2_source']} failed integrity_check: "
                    f"{staged_integrity!r}")
            table_counts = _sqlite_table_counts(target)
        else:
            with open(item["v2_source"], "rb") as source_handle, \
                    open(target, "wb") as dest_handle:
                dest_handle.write(source_handle.read())
            table_counts = None
        staged_sha = sha256_file(target)
        sidecar = target.with_suffix(target.suffix + ".sha256")
        sidecar.write_text(staged_sha + "\n", encoding="utf-8")
        staged_items.append({
            "relative_path": relative, "v2_source": item["v2_source"],
            "v2_sha256": item["v2_sha256"], "staged_path": str(target),
            "staged_sha256": staged_sha,
            "disposition": item["disposition"],
            "table_counts": table_counts,
        })
    manifest = {"schema": "ge16.baseline-migration-staged.v1",
                "run_id": run.run_id, "staged_at": datetime.now(
                    timezone.utc).isoformat(timespec="seconds"),
                "items": staged_items}
    (stage_dir / "staged-manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8")
    return run, stage_dir, manifest


# -------------------------------------------------------------------- promote

def promote(stage_dir, canonical_root=DEFAULT_CANONICAL_ROOT, run=None,
            prior_edition=None, now=None):
    """Verify staged sha256s, then move into canonical. Any pre-existing
    destination = abort before writing anything (report + no-op). An empty
    staged manifest is a no-op: no canonical writes, no edition — this is
    what makes a second ``migrate`` run byte-identical."""
    stage_dir = Path(stage_dir)
    canonical_root = Path(canonical_root)
    manifest = json.loads((stage_dir / "staged-manifest.json").read_text(
        encoding="utf-8"))
    items = manifest["items"]
    if not items:
        return {"promoted": [], "edition_id": None, "verified": [],
                "note": "nothing staged; no-op"}
    for item in items:
        staged_path = Path(item["staged_path"])
        if sha256_file(staged_path) != item["staged_sha256"]:
            raise RuntimeError(
                f"staged file corrupted before promotion: {staged_path} "
                "(sha256 mismatch against staged-manifest.json)")
        destination = canonical_root / item["relative_path"]
        if destination.exists():
            raise RuntimeError(
                f"promotion collision: {destination} already exists in canonical; "
                "refusing to overwrite (staged dir kept for audit: "
                f"{stage_dir})")
    promoted = []
    for item in items:
        destination = canonical_root / item["relative_path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        with open(item["staged_path"], "rb") as source_handle, \
                open(destination, "wb") as dest_handle:
            dest_handle.write(source_handle.read())
        promoted.append({
            "file": item["relative_path"], "sha256": item["staged_sha256"],
            "disposition": "baseline-migration-snapshot:" + item["disposition"],
        })
    row_counts = {"migrated_files": len(promoted)}
    for item in items:
        if item.get("table_counts"):
            row_counts.update({f"events_db.{table}": count
                               for table, count in item["table_counts"].items()})
    editions_dir = canonical_root / "editions"
    note_lines = [f"{MIGRATION_SCHEMA_NOTE_PREFIX}: staged run "
                 f"{manifest['run_id']}; items: "
                 + ", ".join(item["relative_path"] for item in items) + ". "
                 "Design brief proposed schema=ge16.edition.baseline-migration.v1 + "
                 "lineage.kind=baseline-migration; reused ge16.edition.v1 verbatim "
                 "instead (frozen schema + integrity.py structural check both reject "
                 "unrecognized schema/lineage fields; see migrate_baseline.py "
                 "module docstring)."]
    edition_id, edition_path = _EDITION.write_edition(
        str(editions_dir),
        inputs=[{"file": item["relative_path"], "sha256": item["staged_sha256"],
                "disposition": "baseline-migration-snapshot"} for item in items],
        row_counts=row_counts, note=" ".join(note_lines),
        prior=prior_edition, now=now)
    verified = [
        integrity.verify_chain(canonical_root=canonical_root),
        integrity.verify_edition(edition_id, canonical_root=canonical_root),
    ]
    failed = [(report["command"], report["exit_code"], report["status"])
             for report in verified if report["exit_code"]]
    if failed:
        raise RuntimeError(
            "post-promotion integrity gate failed for edition %s (%s): promotion "
            "already landed (report-only verifier never repairs/rolls back); "
            "owner decides on repair" % (
                edition_id, ", ".join("%s exit %d (%s)" % f for f in failed)))
    return {"promoted": promoted, "edition_id": edition_id,
            "edition_path": edition_path,
            "verified": [{"command": r["command"], "exit_code": r["exit_code"],
                         "status": r["status"]} for r in verified]}


# ------------------------------------------------------------------------ CLI

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="P2.5 idempotent V2 baseline migration "
                    "(evidence/P2/P2.5-design-brief.md).")
    parser.add_argument("--v2-root", default=str(DEFAULT_V2_ROOT))
    parser.add_argument("--canonical-root", default=str(DEFAULT_CANONICAL_ROOT))
    parser.add_argument("--p07-dispositions", default=str(DEFAULT_P07_DISPOSITIONS))
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("inventory", help="diff V2 sources vs V3 canonical")

    migrate_parser = subparsers.add_parser("migrate", help="stage or promote")
    stage_group = migrate_parser.add_mutually_exclusive_group(required=True)
    stage_group.add_argument("--stage", action="store_true")
    stage_group.add_argument("--promote", metavar="STAGE_DIR",
                             help="path to a previously staged baseline-stage/ dir")

    args = parser.parse_args(argv)
    canonical_root = Path(args.canonical_root)

    if args.command == "inventory":
        report = inventory(v2_root=args.v2_root, canonical_root=canonical_root,
                           p07_path=args.p07_dispositions)
        print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
        return 0

    if args.stage:
        report = inventory(v2_root=args.v2_root, canonical_root=canonical_root,
                           p07_path=args.p07_dispositions)
        run, stage_dir, manifest = stage(report, canonical_root=canonical_root)
        print(json.dumps({"run_id": run.run_id, "stage_dir": str(stage_dir),
                          "items_staged": len(manifest["items"])},
                         indent=2, ensure_ascii=False, sort_keys=True))
        return 0

    result = promote(args.promote, canonical_root=canonical_root)
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
