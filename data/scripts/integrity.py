#!/usr/bin/env python3
"""Read-only integrity verifier for the canonical corpus and edition chain (P2.4).

REPORT-ONLY: this module never writes, deletes, repairs, or rolls back
anything under canonical (pinned by an AST test in test_p2_4_integrity.py).
Repair and recovery are always owner decisions; the verifier detects and
reports, and the refresh gate turns a nonzero exit into a hard stop.

Subcommands (CLI and same-named library functions):

  verify-edition ID    recompute sha256 for every path in one edition's
                       content_hashes. Path absent = lost; present with the
                       wrong hash = corrupt; else OK. Unknown edition id =
                       lost edition.
  verify-chain         every edition manifest: filename/manifest id match,
                       duplicate ids, prior_edition links (broken = lost,
                       non-older = corrupt), and promotion run resolvability
                       in data/work/ (a pruned run dir is LEGAL gitignored
                       working state -> run_pruned_ok, never a failure).
  verify-corpus        schema-validate rows (full or seeded sample per
                       store), id uniqueness, evidence_id re-derivation
                       through normalize_link.v1, judgment->evidence
                       referential integrity, judgment basis batch-file hash
                       re-check against disk (judged-batch binds
                       batch_file_sha256, accepted-corpus-inline binds
                       source_sha256), dupe-candidate reference existence.
  verify-recorded ID   the additions rule: canonical rows must be covered by
                       the row_counts of the edition chain up to the selected
                       edition (default: latest). Rows beyond the recorded
                       counts = UNRECORDED ADDITION (tampering or skipped
                       process), reported with row path+offset; fewer rows
                       than recorded = LOST rows.

Exit codes (0/1/2; several severities at once -> the worst wins):
  0 clean   1 corrupt   2 lost

Rows are immutable and shards append-only, so "covered by an edition" is a
count comparison attributed deterministically: rows of each metric keep load
order (shard filename, line offset), the first `recorded` are covered, the
remainder are the unrecorded additions.

Path convention: edition content_hashes and basis.batch_file record
repository-relative paths ("data/canonical/...") for import editions and
canonical-root-relative paths ("research/trackers/...") for promotion
editions; both resolve against the canonical root.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import random
import re
import sqlite3
from pathlib import Path, PurePosixPath

import jsonschema

ROOT = Path(__file__).resolve().parents[1] / "canonical"
SCHEMAS_DIR = Path(__file__).resolve().parent / "schemas"
DEFAULT_SAMPLE = 500

#: P2.8 verify-recorded extension (packet §1.5.7): derived artifacts not
#: covered by the by-kind/by-origin breakdowns above. Each maps a
#: ``row_counts`` key a promotion edition MAY record to the canonical-root-
#: relative file it counts rows in; ``dupe_candidates_total`` counts a JSON
#: array's length, the rest count JSONL lines. Additive only: an edition
#: that records none of these keys is checked exactly as before.
DERIVED_ROW_COUNT_FILES = {
    "dupe_candidates_total": ("json_object_array:candidates",
                              "evidence/dupe-of-candidates.json"),
    "links_evidence_entity_total": ("jsonl", "links/evidence-entity.jsonl"),
    "links_entity_entity_total": ("jsonl", "links/entity-entity.jsonl"),
    "links_seat_state_total": ("jsonl", "links/seat-state.jsonl"),
    "poll_observations_total": ("jsonl", "polls/poll-observations.jsonl"),
    # P2.8 R1 MAJOR-1 remediation: vector collections (sqlite counts).
    "vectors_news_total": ("sqlite:news", "vectors/vectors.db"),
    "vectors_events_total": ("sqlite:events", "vectors/vectors.db"),
    "vectors_dossier_notes_total": ("sqlite:dossier_notes", "vectors/vectors.db"),
    # P2.8 R1 MAJOR-4 remediation (archive provenance): the complete
    # accepted-news archive, registered as a derived artifact so the
    # additions rule covers it (P2.6 carry).
    "archive_items_total": ("jsonl", "archive/news-accepted/accepted.jsonl"),
}


def _derived_file_row_count(root, kind, relative_path):
    path = root / relative_path
    if not path.is_file():
        return 0
    if kind == "json_array":
        return len(json.loads(path.read_text(encoding="utf-8")))
    if kind.startswith("json_object_array:"):
        # P2.8 R1 MAJOR-1 remediation: dupe-of-candidates.json is a dict
        # {"candidates": [...], ...}, not a bare array — count the named
        # member (the R1 reviewer measured 3 under the old array read).
        member = kind.split(":", 1)[1]
        document = json.loads(path.read_text(encoding="utf-8"))
        return len(document.get(member, []))
    if kind.startswith("sqlite:"):
        # P2.8 R1 MAJOR-1 remediation: vector collections live in a sqlite
        # DB — count rows in the named table.
        table = kind.split(":", 1)[1]
        connection = sqlite3.connect(path)
        try:
            return connection.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            connection.close()
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


#: Reports list findings in full up to this many entries per bucket, then
#: summarize the remainder as one count (a wholesale corruption produces
#: thousands of findings; the exit code and totals stay exact).
LISTING_CAP = 50
EDITION_ID_RE = re.compile(r"^\d{8}T\d{6}Z$")

CLEAN, CORRUPT, LOST = 0, 1, 2
STATUS_BY_CODE = {CLEAN: "clean", CORRUPT: "corrupt", LOST: "lost"}


def _load_sibling(filename, module_name):
    """Load a helper from scripts/import/ by path (repo convention; the
    package dir is not importable as a package)."""
    path = Path(__file__).resolve().parent / "import" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_EDITION = _load_sibling("edition.py", "p24_integrity_edition")
_IDENTITY = _load_sibling("identity.py", "p24_integrity_identity")


# ---------------------------------------------------------------- read helpers

def _canonical_relative(recorded):
    """Strip the recorded path prefix ("data/canonical/" or "canonical/");
    None when the path cannot name a file inside the canonical root."""
    if not isinstance(recorded, str) or not recorded:
        return None
    posix = PurePosixPath(recorded)
    if posix.is_absolute():
        return None
    parts = posix.parts
    if parts[:2] == ("data", "canonical"):
        parts = parts[2:]
    elif parts[:1] == ("canonical",):
        parts = parts[1:]
    if not parts or any(part == ".." for part in parts):
        return None
    return PurePosixPath(*parts)


def _resolve(canonical_root, recorded):
    relative = _canonical_relative(recorded)
    return None if relative is None else Path(canonical_root) / relative


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _capped(findings):
    """(listed findings, one summary finding when the list was capped)."""
    if len(findings) <= LISTING_CAP:
        return findings, None
    summary = dict(findings[0])
    summary["detail"] = (f"{len(findings)} findings of code {findings[0].get('code')!r}; "
                         f"listing capped at {LISTING_CAP}")
    summary = {key: value for key, value in summary.items() if key != "row"}
    return findings[:LISTING_CAP], summary


def _edition_ids(editions_dir):
    if not editions_dir.is_dir():
        return []
    return sorted((name[len("edition-"):-len(".json")]
                   for name in os.listdir(editions_dir)
                   if name.startswith("edition-") and name.endswith(".json")),
                  key=os.fsencode)


def _validator(schema_filename):
    schema = json.loads((SCHEMAS_DIR / schema_filename).read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema)


_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def _manifest_structure_findings(manifest):
    """Structural checks on exactly the fields this verifier consumes.

    Deliberately NOT a full jsonschema pass: the frozen ge16.edition.v1
    schema declares row_counts values as flat integers, while the P2.2
    importer legitimately writes nested count dicts (evidence_by_kind, …) —
    a known schema/data discrepancy outside P2.4's write scope. Structural
    checks still catch a rewritten manifest without false positives on the
    historical editions.
    """
    findings = []
    if manifest.get("schema") not in (_EDITION.EDITION_SCHEMA,
                                      "ge16.edition.promotion.v1"):
        findings.append({"code": "unknown_edition_schema",
                         "schema": manifest.get("schema")})
    hashes = manifest.get("content_hashes")
    if not isinstance(hashes, dict):
        findings.append({"code": "content_hashes_not_object"})
        hashes = {}
    else:
        bad = [path for path, digest in hashes.items()
               if not isinstance(digest, str) or not _HEX64_RE.match(digest)]
        if bad:
            findings.append({"code": "content_hash_not_sha256",
                             "paths": sorted(bad)[:LISTING_CAP],
                             "count": len(bad)})
    lineage = manifest.get("lineage")
    if not isinstance(lineage, dict) or "prior_edition" not in lineage:
        findings.append({"code": "lineage_missing"})
    return findings


def _load_manifest(canonical_root, edition_id):
    """(manifest|None, findings). Findings cover every way an edition can be
    absent or untrustworthy; manifest is None exactly when it is unusable."""
    if not EDITION_ID_RE.match(edition_id or ""):
        return None, {"corrupt": [{"code": "invalid_edition_id",
                                   "edition_id": edition_id}],
                      "lost": []}
    path = Path(canonical_root) / "editions" / f"edition-{edition_id}.json"
    if not path.is_file():
        return None, {"corrupt": [],
                      "lost": [{"code": "edition_absent", "edition_id": edition_id,
                                "detail": "edition manifest missing from canonical"}]}
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        return None, {"corrupt": [{"code": "manifest_unparseable",
                                   "edition_id": edition_id, "detail": str(error)[:120]}],
                      "lost": []}
    if not isinstance(manifest, dict):
        return None, {"corrupt": [{"code": "manifest_not_object",
                                   "edition_id": edition_id}], "lost": []}
    corrupt = [{"code": finding["code"], "edition_id": edition_id, **{
        key: value for key, value in finding.items() if key != "code"}}
               for finding in _manifest_structure_findings(manifest)]
    if manifest.get("edition_id") != edition_id:
        corrupt.append({"code": "edition_id_mismatch", "edition_id": edition_id,
                        "declared": manifest.get("edition_id"),
                        "detail": "manifest edition_id differs from its filename"})
    return manifest, {"corrupt": corrupt, "lost": []}


def _load_rows(canonical_root, relative_dir, prefix, id_key):
    """(rows, errors). rows: {file, offset, row} in load order (shard name,
    then line); errors carry the same location so a defect is actionable."""
    directory = Path(canonical_root) / relative_dir
    rows, errors = [], []
    if not directory.is_dir():
        return rows, errors
    for name in sorted(os.listdir(directory), key=os.fsencode):
        if not (name.startswith(prefix) and name.endswith(".jsonl")):
            continue
        rel = f"{relative_dir}/{name}"
        with open(directory / name, "r", encoding="utf-8") as handle:
            for offset, line in enumerate(handle):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError("line is not a JSON object")
                    rows.append({"file": rel, "offset": offset, "row": row})
                except ValueError as error:
                    errors.append({"row": rel, "offset": offset,
                                   "detail": str(error)[:120]})
    return rows, errors


def _duplicate_groups(rows, id_key):
    seen, duplicates = {}, []
    for entry in rows:
        rid = entry["row"].get(id_key)
        if rid in seen:
            duplicates.append((rid, seen[rid], {"row": entry["file"],
                                                "offset": entry["offset"]}))
        else:
            seen[rid] = {"row": entry["file"], "offset": entry["offset"]}
    findings = [{"code": "duplicate_id", "id_key": id_key, "id": rid,
                 "first": first, "again": again}
                for rid, first, again in duplicates]
    return _capped(findings)


def _batch_binding(basis):
    """(field name, expected sha256) for the disk-verifiable binding this
    basis carries, or None when it has none (orphaned-flag rows)."""
    if not basis.get("batch_file"):
        return None
    if basis.get("origin") == "judged-batch" and basis.get("batch_file_sha256"):
        return "batch_file_sha256", basis["batch_file_sha256"]
    if basis.get("origin") == "accepted-corpus-inline" and basis.get("source_sha256"):
        return "source_sha256", basis["source_sha256"]
    return None


# ------------------------------------------------------------- report plumbing

def _report(command, canonical_root, corrupt=(), lost=(), **fields):
    corrupt, lost = list(corrupt), list(lost)
    code = LOST if lost else CORRUPT if corrupt else CLEAN
    return {"command": command,
            "canonical_root": str(Path(canonical_root).resolve()),
            "status": ("corrupt+lost" if corrupt and lost else STATUS_BY_CODE[code]),
            "exit_code": code,
            "findings": {"corrupt": corrupt, "lost": lost}, **fields}


def _emit(report, as_json):
    if as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
        return report["exit_code"]
    print(f"{report['command']}: {report['status']} (exit {report['exit_code']})")
    for kind in ("lost", "corrupt"):
        for finding in report["findings"][kind]:
            print(f"  [{kind}] {json.dumps(finding, ensure_ascii=False, sort_keys=True)}")
    return report["exit_code"]


# ----------------------------------------------------------------- subcommands

def verify_edition(edition_id, canonical_root=ROOT):
    """Re-hash every path in one edition manifest against disk."""
    root = Path(canonical_root)
    manifest, load = _load_manifest(root, edition_id)
    if manifest is None:
        return _report("verify-edition", root, corrupt=load["corrupt"],
                       lost=load["lost"], edition_id=edition_id, paths_ok=0,
                       paths_total=0)
    corrupt, lost = list(load["corrupt"]), list(load["lost"])
    hashes = manifest.get("content_hashes") or {}
    if not isinstance(hashes, dict):
        corrupt.append({"code": "content_hashes_not_object", "edition_id": edition_id})
        hashes = {}
    ok = 0
    for path, expected in sorted(hashes.items()):
        resolved = _resolve(root, path)
        if resolved is None:
            corrupt.append({"code": "invalid_path", "path": path,
                            "detail": "not a canonical-root-relative path"})
        elif not resolved.is_file():
            lost.append({"code": "missing", "path": path,
                         "detail": "path absent from canonical (lost)"})
        elif _sha256_file(resolved) != expected:
            corrupt.append({"code": "mismatch", "path": path,
                            "detail": "present with wrong sha256 (corrupt)"})
        else:
            ok += 1
    return _report("verify-edition", root, corrupt=corrupt, lost=lost,
                   edition_id=edition_id, schema=manifest.get("schema"),
                   paths_ok=ok, paths_total=len(hashes))


def verify_chain(canonical_root=ROOT):
    """Structural checks over every edition manifest and the prior chain."""
    root = Path(canonical_root)
    editions_dir = root / "editions"
    if not editions_dir.is_dir():
        return _report("verify-chain", root, lost=[{
            "code": "editions_dir_absent", "detail": str(editions_dir)}],
            editions_verified=0, editions_on_disk=0, latest_edition_id=None,
            runs={})
    corrupt, lost = [], []
    ids = _edition_ids(editions_dir)
    manifests = {}
    for edition_id in ids:
        manifest, load = _load_manifest(root, edition_id)
        corrupt += load["corrupt"]
        lost += load["lost"]
        if manifest is not None:
            manifests[edition_id] = manifest
    declared = {}
    for edition_id, manifest in sorted(manifests.items()):
        declared.setdefault(manifest.get("edition_id"), []).append(edition_id)
    for value, holders in sorted(declared.items(), key=lambda item: str(item[0])):
        if value is None:
            corrupt.append({"code": "edition_id_missing",
                            "detail": f"manifest without edition_id: {holders}"})
        elif len(holders) > 1:
            corrupt.append({"code": "duplicate_edition_id", "edition_id": value,
                            "detail": f"declared by files: {sorted(holders)}"})
    for edition_id, manifest in sorted(manifests.items()):
        prior = (manifest.get("lineage") or {}).get("prior_edition")
        if prior is None:
            continue
        if prior not in ids:
            lost.append({"code": "broken_link", "edition_id": edition_id,
                         "prior_edition": prior,
                         "detail": "prior edition id absent from canonical"})
        elif prior >= edition_id:
            corrupt.append({"code": "timestamp_order", "edition_id": edition_id,
                            "prior_edition": prior,
                            "detail": "prior_edition is not strictly older "
                                      "(edition ids are UTC stamps)"})
    runs = {"run_resolved": 0, "run_pruned_ok": 0, "run_ids": []}
    work_root = root.parent / "work"
    for edition_id, manifest in sorted(manifests.items()):
        run_id = (manifest.get("lineage") or {}).get("run_id")
        if not run_id:
            continue  # ge16.edition.v1 import editions carry no run binding
        entry = {"edition_id": edition_id, "run_id": run_id}
        run_dir = work_root / run_id
        if not run_dir.is_dir():
            runs["run_pruned_ok"] += 1
            entry["status"] = "run_pruned_ok"  # gitignored working state; legal
        else:
            run_json = run_dir / "run.json"
            if not run_json.is_file():
                corrupt.append({"code": "run_json_missing", "edition_id": edition_id,
                                "run_id": run_id,
                                "detail": "run dir exists without run.json"})
                entry["status"] = "run_json_missing"
            else:
                try:
                    declared_id = json.loads(
                        run_json.read_text(encoding="utf-8")).get("run_id")
                except ValueError as error:
                    declared_id = f"<unparseable: {error}>"
                if declared_id != run_id:
                    corrupt.append({"code": "run_json_mismatch",
                                    "edition_id": edition_id, "run_id": run_id,
                                    "detail": f"run.json declares run_id {declared_id!r}"})
                    entry["status"] = "run_json_mismatch"
                else:
                    runs["run_resolved"] += 1
                    entry["status"] = "run_resolved"
        runs["run_ids"].append(entry)
    return _report("verify-chain", root, corrupt=corrupt, lost=lost,
                   editions_verified=len(manifests), editions_on_disk=len(ids),
                   latest_edition_id=ids[-1] if ids else None, runs=runs)


def verify_corpus(canonical_root=ROOT, sample=None, seed=0):
    """Corpus checks; sample=None is full, an int is a seeded sample per store."""
    root = Path(canonical_root)
    corrupt, lost = [], []
    evidence, evidence_errors = _load_rows(root, "evidence", "evidence-", "evidence_id")
    judgments, judgment_errors = _load_rows(root, "judgments", "judgment-", "judgment_id")
    for store, errors in (("evidence", evidence_errors),
                          ("judgments", judgment_errors)):
        listed, summary = _capped([dict(error, code="unparseable_line", store=store)
                                   for error in errors])
        corrupt += listed + ([summary] if summary else [])
    for id_key, rows in (("evidence_id", evidence), ("judgment_id", judgments)):
        listed, summary = _duplicate_groups(rows, id_key)
        corrupt += listed + ([summary] if summary else [])

    evidence_ids = {entry["row"].get("evidence_id") for entry in evidence}
    for entry in judgments:
        if entry["row"].get("evidence_id") not in evidence_ids:
            lost.append({"code": "judgment_dangling_evidence", "row": entry["file"],
                         "offset": entry["offset"],
                         "evidence_id": entry["row"].get("evidence_id"),
                         "detail": "judgment references evidence absent from the "
                                   "corpus (lost evidence or tampered judgment)"})

    dupe_path = root / "evidence" / "dupe-of-candidates.json"
    if dupe_path.is_file():
        try:
            candidates = json.loads(
                dupe_path.read_text(encoding="utf-8")).get("candidates") or []
        except ValueError as error:
            corrupt.append({"code": "dupe_file_unparseable", "row": str(dupe_path),
                            "detail": str(error)[:120]})
            candidates = []
        dangling = [{"code": "dupe_candidate_dangling", "candidate_index": index,
                     "evidence_id": candidate.get("evidence_id") if
                     isinstance(candidate, dict) else None}
                    for index, candidate in enumerate(candidates)
                    if not isinstance(candidate, dict)
                    or candidate.get("evidence_id") not in evidence_ids]
        listed, summary = _capped(dangling)
        lost += listed + ([summary] if summary else [])

    if sample is None:
        evidence_sample, judgment_sample = evidence, judgments
    else:
        rng = random.Random(seed)
        evidence_sample = rng.sample(evidence, min(sample, len(evidence)))
        judgment_sample = rng.sample(judgments, min(sample, len(judgments)))
    covers_full = sample is None or (len(evidence_sample) == len(evidence)
                                     and len(judgment_sample) == len(judgments))

    validators = {"evidence": _validator("ge16_evidence.schema.json"),
                  "judgments": _validator("ge16_judgment.schema.json")}
    for store, rows in (("evidence", evidence_sample), ("judgments", judgment_sample)):
        for entry in rows:
            errors = sorted(validators[store].iter_errors(entry["row"]),
                            key=lambda error: list(error.path))
            if errors:
                corrupt.append({"code": "schema_invalid", "store": store,
                                "row": entry["file"], "offset": entry["offset"],
                                "detail": "; ".join(f"{list(error.path)}: {error.message}"
                                                    for error in errors[:3])})

    for entry in evidence_sample:
        row = entry["row"]
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        key = payload.get("link") or payload.get("seen")
        if not isinstance(key, str):
            continue  # sampled schema validation is the check for shape damage
        try:
            want = _IDENTITY.evidence_id(_IDENTITY.normalize_link(key))
        except Exception as error:  # normalizer contract violation is corruption
            corrupt.append({"code": "normalizer_error", "row": entry["file"],
                            "offset": entry["offset"], "detail": repr(error)})
            continue
        if want != row.get("evidence_id"):
            corrupt.append({"code": "evidence_id_rederivation", "row": entry["file"],
                            "offset": entry["offset"],
                            "evidence_id": row.get("evidence_id"), "rederived": want,
                            "detail": "normalize_link.v1 does not reproduce the id"})
    for entry in judgment_sample:
        row = entry["row"]
        try:
            want = _IDENTITY.judgment_id(row["evidence_id"], row["judged_at"],
                                         row["verdict"])
        except (KeyError, TypeError) as error:
            corrupt.append({"code": "judgment_fields", "row": entry["file"],
                            "offset": entry["offset"], "detail": repr(error)})
            continue
        if want != row.get("judgment_id"):
            corrupt.append({"code": "judgment_id_rederivation", "row": entry["file"],
                            "offset": entry["offset"],
                            "judgment_id": row.get("judgment_id"), "rederived": want})
        basis = row.get("basis") if isinstance(row.get("basis"), dict) else {}
        binding = _batch_binding(basis)
        if binding is None:
            continue
        binding_field, expected = binding
        resolved = _resolve(root, basis.get("batch_file"))
        if resolved is None or not resolved.is_file():
            lost.append({"code": "batch_file_absent", "row": entry["file"],
                         "offset": entry["offset"], "batch_file": basis.get("batch_file"),
                         "detail": f"basis.{binding_field} binds a file absent from "
                                   "canonical (lost source)"})
        elif _sha256_file(resolved) != expected:
            corrupt.append({"code": "batch_hash_mismatch", "row": entry["file"],
                            "offset": entry["offset"], "batch_file": basis.get("batch_file"),
                            "detail": f"basis.{binding_field} does not match the file "
                                      "on disk"})

    return _report("verify-corpus", root, corrupt=corrupt, lost=lost,
                   mode="full" if sample is None else "sampled",
                   seed=seed if sample is not None else None,
                   covers_full_corpus=covers_full,
                   rows={"evidence": len(evidence), "judgments": len(judgments),
                         "evidence_parse_errors": len(evidence_errors),
                         "judgment_parse_errors": len(judgment_errors)},
                   checked={"evidence": len(evidence_sample),
                            "judgments": len(judgment_sample)})


def verify_recorded(edition_id=None, canonical_root=ROOT):
    """The additions rule: disk rows must not exceed the recorded row_counts."""
    root = Path(canonical_root)
    corrupt, lost = [], []
    if edition_id is None:
        edition_id = _EDITION.prior_edition_id(str(root / "editions"))
        if edition_id is None:
            edition_id = ""
    manifest = None
    if edition_id:
        manifest, load = _load_manifest(root, edition_id)
        corrupt, lost = list(load["corrupt"]), list(load["lost"])

    chain, broken_at = [], None
    if manifest is not None:
        seen = set()
        current = edition_id
        while current:
            seen.add(current)
            step, _ = _load_manifest(root, current)
            if step is None:
                break
            chain.append(step)
            current = (step.get("lineage") or {}).get("prior_edition")
            if current in seen:  # defensive: a self/cyclic link cannot loop us
                broken_at = broken_at or {"edition_id": current,
                                          "prior_edition": current}
                break
            if current and current not in _edition_ids(root / "editions"):
                broken_at = {"edition_id": chain[-1].get("edition_id"),
                             "prior_edition": current}
                break
        if broken_at:
            lost.append({"code": "chain_broken", **broken_at,
                         "detail": "baseline scope stops at the break"})
    # chain is newest-first: the baseline is the newest edition in scope that
    # carries corpus row_counts. Promotion editions CAN carry them now
    # (P2.6/P2.7 promotions publish flat totals; P2.8 R1 MAJOR-1 remediation:
    # the walk stops at the newest edition recording evidence_total, and
    # DEFICITS are not flagged against an older-than-newest baseline).
    # Carry-forward rule (R1 MAJOR-1): an edition recording only PART of the
    # known metrics inherits the missing ones from the previous qualifying
    # edition, so a promotion edition recording judgments_total (but not
    # evidence_by_kind) still baselines evidence counts from its ancestor.
    baseline = None
    recorded = {}
    for step in chain:
        step_counts = step.get("row_counts") or {}
        if "evidence_total" in step_counts:
            if baseline is None:
                baseline = step
                recorded = dict(step_counts)
            else:
                # newest qualifying edition first; inherit only metrics the
                # newest one does not itself record (carry-forward).
                for key, value in step_counts.items():
                    recorded.setdefault(key, value)
                if all(metric in step_counts
                       for metric in DERIVED_ROW_COUNT_FILES) and \
                        "evidence_by_kind" in step_counts:
                    break
    if baseline is None:
        baseline = {}
        recorded = {}

    evidence_rows, _ = _load_rows(root, "evidence", "evidence-", "evidence_id")
    judgment_rows, _ = _load_rows(root, "judgments", "judgment-", "judgment_id")
    entity_rows, _ = _load_rows(root, "entities", "entity-candidates", "candidate_id")

    def rows_for(kind, bucket):
        if kind == "evidence":
            return [e for e in evidence_rows
                    if bucket is None or e["row"].get("kind") == bucket]
        if kind == "judgment":
            return [j for j in judgment_rows
                    if bucket is None
                    or (j["row"].get("basis") or {}).get("origin") == bucket]
        return entity_rows

    # The by-kind/by-origin breakdowns subsume the totals when present (each
    # row counts once); totals are checked only for manifests that record no
    # breakdown, so one physical row is never reported as two additions.
    checks = []
    kinds = recorded.get("evidence_by_kind") \
        if isinstance(recorded.get("evidence_by_kind"), dict) else None
    for kind, count in sorted((kinds or {}).items()):
        checks.append((f"evidence:{kind}", "evidence", kind, count))
    if kinds is None and "evidence_total" in recorded:
        checks.append(("evidence_total", "evidence", None, recorded["evidence_total"]))
    origins = recorded.get("judgments_by_origin") \
        if isinstance(recorded.get("judgments_by_origin"), dict) else None
    for origin, count in sorted((origins or {}).items()):
        checks.append((f"judgment:{origin}", "judgment", origin, count))
    if origins is None and "judgments_total" in recorded:
        checks.append(("judgments_total", "judgment", None, recorded["judgments_total"]))
    if "entity_candidates_total" in recorded:
        checks.append(("entity_candidates_total", "entity_candidate", None,
                       recorded["entity_candidates_total"]))
    derived_checks = [(metric, DERIVED_ROW_COUNT_FILES[metric])
                      for metric in DERIVED_ROW_COUNT_FILES if metric in recorded]

    unrecorded, lost_rows = [], []
    for metric, (file_kind, relative_path) in derived_checks:
        expected = recorded[metric]
        on_disk = _derived_file_row_count(root, file_kind, relative_path)
        if on_disk > expected:
            unrecorded.append({"metric": metric, "recorded": expected, "on_disk": on_disk,
                               "excess": on_disk - expected, "rows": [],
                               "detail": f"derived file {relative_path} has more rows than recorded"})
        elif on_disk < expected:
            lost_rows.append({"metric": metric, "recorded": expected, "on_disk": on_disk,
                              "deficit": expected - on_disk})
    for name, kind, bucket, expected in checks:
        rows = rows_for(kind, bucket)
        if len(rows) > expected:
            excess = rows[expected:]
            unrecorded.append({
                "metric": name, "recorded": expected, "on_disk": len(rows),
                "excess": len(excess),
                "rows": [{"row": entry["file"], "offset": entry["offset"],
                          "id": entry["row"].get("evidence_id")
                          or entry["row"].get("judgment_id")
                          or entry["row"].get("candidate_id")}
                         for entry in excess[:LISTING_CAP]]})
        elif len(rows) < expected:
            lost_rows.append({"metric": name, "recorded": expected,
                              "on_disk": len(rows), "deficit": expected - len(rows)})
    if unrecorded:
        corrupt.append({"code": "unrecorded_addition",
                        "detail": "row(s) on disk beyond the edition-recorded "
                                  "row_counts (tampering or skipped process)",
                        "metrics": unrecorded})
    for deficit in lost_rows:
        lost.append({"code": "lost_rows",
                     "detail": "edition records more rows than remain on disk",
                     **deficit})
    return _report("verify-recorded", root, corrupt=corrupt, lost=lost,
                   edition_id=edition_id or None,
                   baseline_edition=baseline.get("edition_id") if baseline else None,
                   chain_depth=len(chain),
                   unrecorded_count=sum(metric["excess"] for metric in unrecorded),
                   lost_rows=lost_rows)


# -------------------------------------------------------------------------- CLI

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="integrity.py",
        description="Read-only integrity verifier (P2.4). REPORT-ONLY: never "
                    "writes or repairs anything. Exit codes: 0 clean, 1 corrupt, "
                    "2 lost; worst wins.")
    parser.add_argument("--data-root", default=str(ROOT.parent),
                        help="V3 data/ directory (default: %(default)s)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    edition_parser = subparsers.add_parser(
        "verify-edition", help="re-hash every path in one edition manifest")
    edition_parser.add_argument("edition_id")
    edition_parser.add_argument("--json", action="store_true",
                                help="machine-readable report on stdout")

    chain_parser = subparsers.add_parser(
        "verify-chain", help="prior_edition chain + promotion run bindings")
    chain_parser.add_argument("--json", action="store_true",
                              help="machine-readable report on stdout")

    corpus_parser = subparsers.add_parser(
        "verify-corpus",
        help="schema/ids/references/batch hashes over canonical rows")
    corpus_mode = corpus_parser.add_mutually_exclusive_group()
    corpus_mode.add_argument("--full", action="store_true",
                             help="check every row (default: --sampled 500)")
    corpus_mode.add_argument("--sampled", type=int, default=None, metavar="N",
                             help="seeded sample of N rows per store (default 500)")
    corpus_parser.add_argument("--seed", type=int, default=0,
                               help="sample seed (default %(default)s; deterministic)")
    corpus_parser.add_argument("--json", action="store_true",
                               help="machine-readable report on stdout")

    recorded_parser = subparsers.add_parser(
        "verify-recorded", help="additions rule: rows must be edition-recorded")
    recorded_parser.add_argument("--edition", default=None,
                                 help="edition scoping the baseline (default: latest)")
    recorded_parser.add_argument("--json", action="store_true",
                                 help="machine-readable report on stdout")

    args = parser.parse_args(argv)
    canonical_root = Path(args.data_root) / "canonical"
    if args.command == "verify-edition":
        report = verify_edition(args.edition_id, canonical_root=canonical_root)
    elif args.command == "verify-chain":
        report = verify_chain(canonical_root=canonical_root)
    elif args.command == "verify-corpus":
        sample = None if args.full else (args.sampled if args.sampled is not None
                                         else DEFAULT_SAMPLE)
        report = verify_corpus(canonical_root=canonical_root, sample=sample,
                               seed=args.seed)
    else:
        report = verify_recorded(edition_id=args.edition, canonical_root=canonical_root)
    return _emit(report, args.json)


if __name__ == "__main__":
    raise SystemExit(main())
