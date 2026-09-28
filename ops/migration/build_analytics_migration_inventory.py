#!/usr/bin/env python3
"""Build deterministic, no-follow Phase 3 migration inventory documents.

This is intentionally an inventory tool, not a migration tool: it never
changes an audited source tree.  Classification is mechanical and records
unknown disposition/dependency/Git information as ``unresolved`` rather than
guessing from file contents or historical aggregate counts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import secrets
import stat
import subprocess
from typing import Any, Callable, Dict, Iterable, List, Mapping, NamedTuple, Optional


class InventoryError(ValueError):
    """Raised for unsafe or internally inconsistent inventory input."""


SCHEMA_VERSION = "https://json-schema.org/draft/2020-12/schema"
ACTIVE_CODE_SUFFIXES = frozenset({
    ".py", ".pyi", ".c", ".h", ".js", ".ts", ".sh", ".applescript",
    ".ps1", ".fish", ".csh", ".json", ".yaml", ".yml", ".toml",
    ".ini", ".j2", ".jinja", ".tmpl",
})
HISTORICAL_CODE_SUFFIXES = frozenset({
    ".py", ".pyi", ".c", ".h", ".js", ".ts", ".sh", ".applescript",
    ".ps1", ".fish", ".csh",
})
HISTORICAL_TARGETS = (
    "01_RESEARCH", "02_FORECAST/outputs", "03_REPORTS", "04_SOCIAL",
    ".tracking", "07_MANUAL", "08_HANDOFF",
)
HISTORICAL_TOTALS = {
    "total": {"files": 850, "bytes": 304212118},
    "transient": {"files": 222, "bytes": 253863921},
    "duplicate": {"files": 134, "bytes": 13347179},
    "archive": {"files": 354, "bytes": 13095939},
    "runtime": {"files": 140, "bytes": 23905079},
}
EXCLUDED_COMPONENTS = frozenset({".git", ".venv", "venv", ".model_cache", "node_modules", "__pycache__", "cache", ".cache"})
EXCLUDED_SUFFIXES = frozenset({".pyc", ".pyo"})
GENERATED_OUTPUT_FILENAMES = (
    "ge16-runtime-dependency-inventory.json",
    "ge16-rename-disposition-manifest.json",
    "ge16-current-state.json",
)
OPS_EVIDENCE_ROOT = pathlib.Path(__file__).resolve().parent / "evidence"
GitStateProvider = Callable[[str], Mapping[str, Any]]


class _StagedOutput(NamedTuple):
    """Identity and immutable bytes expected from one private staged file."""

    identity: os.stat_result
    length: int
    sha256: str


def _safe_relative_path(root: pathlib.Path, path: pathlib.Path) -> str:
    relative = path.relative_to(root).as_posix()
    if not relative or relative == "." or relative.startswith("/") or ".." in pathlib.PurePosixPath(relative).parts:
        raise InventoryError("unsafe relative path: %r" % relative)
    return relative


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _sha256_no_follow_at(parent_fd: int, name: str, expected: os.stat_result) -> str:
    """Hash a regular entry only if the opened object is the lstat object."""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same_identity(opened, expected):
            raise InventoryError("source entry changed while being inventoried: %s" % name)
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)
    finally:
        os.close(descriptor)


def _git_state(relative_path: str, provider: Optional[GitStateProvider]) -> Dict[str, Any]:
    if provider is None:
        return {"tracked": None, "ignored": None, "state": "unresolved"}
    supplied = dict(provider(relative_path))
    expected = {"tracked", "ignored", "state"}
    if set(supplied) != expected or supplied["state"] not in {"tracked", "ignored", "untracked", "unresolved"}:
        raise InventoryError("invalid Git state for %s" % relative_path)
    return supplied


def _git_state_provider_for_root(root: pathlib.Path) -> GitStateProvider:
    """Return a read-only Git metadata provider, or unresolved outside Git.

    ``git ls-files`` and ``git check-ignore`` are deliberately read-only.  A
    source tree that is not a Git work tree keeps the documented unresolved
    semantics instead of inventing tracked/ignored status.
    """
    root = pathlib.Path(os.path.realpath(str(root)))

    def run_git(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ("git", "-C", str(root), *arguments),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )

    try:
        is_work_tree = run_git("rev-parse", "--is-inside-work-tree").returncode == 0
    except OSError:
        is_work_tree = False

    cache: Dict[str, Mapping[str, Any]] = {}
    def provider(relative_path: str) -> Mapping[str, Any]:
        if relative_path in cache:
            return cache[relative_path]
        if not is_work_tree:
            cache[relative_path] = {"tracked": None, "ignored": None, "state": "unresolved"}
            return cache[relative_path]
        try:
            if run_git("ls-files", "--error-unmatch", "--", relative_path).returncode == 0:
                cache[relative_path] = {"tracked": True, "ignored": False, "state": "tracked"}
                return cache[relative_path]
            if run_git("check-ignore", "-q", "--", relative_path).returncode == 0:
                cache[relative_path] = {"tracked": False, "ignored": True, "state": "ignored"}
                return cache[relative_path]
        except OSError:
            cache[relative_path] = {"tracked": None, "ignored": None, "state": "unresolved"}
            return cache[relative_path]
        cache[relative_path] = {"tracked": False, "ignored": False, "state": "untracked"}
        return cache[relative_path]

    return provider


def _code_status(relative_path: str, file_type: str) -> tuple[str, str]:
    parts = pathlib.PurePosixPath(relative_path).parts
    suffix = pathlib.PurePosixPath(relative_path).suffix.lower()
    excluded = sorted(set(parts) & EXCLUDED_COMPONENTS)
    if excluded:
        return "excluded", "excluded path component: %s" % ", ".join(excluded)
    if suffix in EXCLUDED_SUFFIXES:
        return "excluded", "generated bytecode suffix: %s" % suffix
    if file_type != "regular":
        return "not-code", "non-regular file type: %s" % file_type
    if suffix in ACTIVE_CODE_SUFFIXES:
        return "candidate", "explicit active-code suffix: %s" % suffix
    return "not-code", "suffix not in explicit active-code rules"


def _record_at(parent_fd: int, name: str, relative_path: str, git_state_provider: Optional[GitStateProvider]) -> Dict[str, Any]:
    metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    mode = metadata.st_mode
    if stat.S_ISLNK(mode):
        file_type, digest, target = "symlink", None, os.readlink(name, dir_fd=parent_fd)
    elif stat.S_ISREG(mode):
        file_type, digest, target = "regular", _sha256_no_follow_at(parent_fd, name, metadata), None
    else:
        file_type, digest, target = "other", None, None
    active_code_status, classification_reason = _code_status(relative_path, file_type)
    return {
        "path": relative_path,
        "lstat_bytes": metadata.st_size,
        "file_type": file_type,
        "sha256": digest,
        "symlink_target": target,
        "git": _git_state(relative_path, git_state_provider),
        "active_code_status": active_code_status,
        "classification_reason": classification_reason,
        "inbound_callers": [],
        "reads": [],
        "writes": [],
        "dependency_status": "unresolved",
    }


def inventory_tree(root: pathlib.Path, git_state_provider: Optional[GitStateProvider] = None, skip_components: Optional[frozenset[str]] = None) -> List[Dict[str, Any]]:
    """Return a sorted record for every file-like directory entry beneath root.

    ``lstat`` is the sole type decision.  Symlink targets are captured as text;
    target bytes are never read. Directories themselves are not audited files.
    """
    root = pathlib.Path(root)
    root_fd = _open_directory_no_follow(root, "inventory root")
    records: List[Dict[str, Any]] = []
    def walk(directory_fd: int, prefix: str) -> None:
        with os.scandir(directory_fd) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                relative_path = entry.name if not prefix else prefix + "/" + entry.name
                metadata = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(metadata.st_mode):
                    if skip_components is not None and entry.name in skip_components:
                        continue
                    child_fd = _open_child_directory_no_follow(directory_fd, entry.name, metadata)
                    try:
                        walk(child_fd, relative_path)
                    finally:
                        os.close(child_fd)
                else:
                    records.append(_record_at(directory_fd, entry.name, relative_path, git_state_provider))
    try:
        walk(root_fd, "")
    finally:
        os.close(root_fd)
    records.sort(key=lambda record: record["path"])
    paths = [record["path"] for record in records]
    if len(paths) != len(set(paths)):
        raise InventoryError("duplicate path in lstat inventory")
    return records


def _disposition(record: Mapping[str, Any]) -> tuple[str, str]:
    if record["active_code_status"] == "candidate":
        return "runtime", "active code candidate; destination requires Phase 3 evidence"
    return "unresolved", "non-code disposition requires recovered path-level baseline"


def build_documents(
    root: pathlib.Path,
    records: Optional[Iterable[Mapping[str, Any]]] = None,
    git_state_provider: Optional[GitStateProvider] = None,
) -> Dict[str, Dict[str, Any]]:
    """Build the three declared documents in memory without writing anything."""
    root = pathlib.Path(os.path.realpath(str(root)))
    provider = git_state_provider or _git_state_provider_for_root(root)
    source_records = [dict(record) for record in (records if records is not None else inventory_tree(root, provider))]
    paths = [record.get("path") for record in source_records]
    if len(paths) != len(set(paths)):
        raise InventoryError("duplicate path in supplied inventory")
    disposition_records = []
    for record in source_records:
        classification, disposition_reason = _disposition(record)
        enriched = dict(record)
        enriched.update({
            "classification": classification,
            "disposition_status": "unresolved",
            "disposition_reason": disposition_reason,
            "destination": None,
            "counterpart": None,
            "removal_permission_gate": None,
            "disposition_evidence": [],
        })
        disposition_records.append(enriched)
    runtime_records = []
    for record in disposition_records:
        if record["active_code_status"] == "candidate":
            runtime = dict(record)
            runtime.pop("removal_permission_gate")
            runtime.pop("disposition_evidence")
            runtime_records.append(runtime)
    current_state = {
        "format": "ge16-current-state/v1",
        "source_root": str(root),
        "generated_outputs": list(GENERATED_OUTPUT_FILENAMES),
        "file_count": len(disposition_records),
        "active_code_candidate_count": len(runtime_records),
        "unresolved_semantics": [
            "Git state is unresolved unless supplied by an explicit Git metadata provider.",
            "Dependency calls, reads, writes, destinations, counterparts, and non-code dispositions are unresolved until Phase 3 evidence is reproduced.",
            "Historical aggregate totals are not used to classify records.",
        ],
        "historical_comparison": {},
        "schema_validation": {"status": "passed", "validator": "jsonschema Draft202012Validator", "schemas": ["runtime-dependency-inventory.schema.json", "rename-disposition.schema.json", "current-state.schema.json"]},
    }
    return {
        "runtime_dependency": {
            "format": "ge16-runtime-dependency-inventory/v1",
            "schema": "runtime-dependency-inventory.schema.json",
            "active_code_rules": {"suffixes": sorted(ACTIVE_CODE_SUFFIXES), "excluded_components": sorted(EXCLUDED_COMPONENTS), "excluded_suffixes": sorted(EXCLUDED_SUFFIXES)},
            "files": runtime_records,
        },
        "rename_disposition": {
            "format": "ge16-rename-disposition-manifest/v1",
            "schema": "rename-disposition.schema.json",
            "files": disposition_records,
        },
        "current_state": current_state,
    }


def _prefixed_records(root: pathlib.Path, prefixes: Iterable[str], provider: GitStateProvider) -> List[Dict[str, Any]]:
    """No-follow inventory of selected trees, retaining HERMES-relative paths."""
    result: List[Dict[str, Any]] = []
    for prefix in prefixes:
        directory = root / prefix
        if not directory.is_dir():
            continue
        for record in inventory_tree(directory, None):
            copied = dict(record)
            copied["path"] = prefix + "/" + record["path"]
            copied["git"] = _git_state(copied["path"], provider)
            result.append(copied)
    return sorted(result, key=lambda item: item["path"])


def _hash_index(root: pathlib.Path) -> Dict[str, List[str]]:
    """Build a deterministic regular-file hash index without dereferencing links."""
    index: Dict[str, List[str]] = {}
    for record in inventory_tree(root, None, frozenset({".git", "__pycache__"})):
        if record["file_type"] == "regular" and record["sha256"]:
            index.setdefault(record["sha256"], []).append(record["path"])
    for paths in index.values():
        paths.sort()
    return index


def _historical_non_code(record: Mapping[str, Any]) -> bool:
    return record["file_type"] != "regular" or pathlib.PurePosixPath(record["path"]).suffix.lower() not in HISTORICAL_CODE_SUFFIXES


def _counterpart(root_name: str, path: str, digest: str) -> Dict[str, str]:
    return {"root": root_name, "path": path, "sha256": digest}


def _classify_phase32(record: Mapping[str, Any], data_index: Mapping[str, List[str]], output_index: Mapping[str, List[str]]) -> Dict[str, Any]:
    """Mechanical transcription of the recovered exclusive historical order."""
    path, digest = record["path"], record["sha256"]
    base: Dict[str, Any] = {
        "classification": "runtime", "disposition_status": "inventory-only",
        "disposition_reason": "remaining entry after recovered exclusive classification order",
        "destination": {"root": "unresolved", "path": None}, "counterpart": None,
        "removal_permission_gate": None,
        "disposition_evidence": [],
    }
    if path.startswith("01_RESEARCH/figures/.model_cache/") or path.startswith("01_RESEARCH/figures/_draft/.venv/") or path.startswith("08_HANDOFF/stages/"):
        base.update({"classification": "transient", "disposition_reason": "reproducible transient path in recovered historical algorithm", "destination": {"root": "none", "path": None}, "removal_permission_gate": "not-authorized-by-this-task"})
    elif digest and path.startswith("01_RESEARCH/") and digest in data_index:
        base.update({"classification": "duplicate", "disposition_reason": "current canonical 1_DATA exact-content counterpart", "destination": {"root": "1_DATA", "path": data_index[digest][0]}, "counterpart": _counterpart("1_DATA", data_index[digest][0], digest)})
    elif digest and path.startswith("02_FORECAST/outputs/") and digest in output_index:
        base.update({"classification": "duplicate", "disposition_reason": "sealed O-20260906-01 exact-content counterpart", "destination": {"root": "3_OUTPUTS/releases/O-20260906-01/artifacts", "path": output_index[digest][0]}, "counterpart": _counterpart("3_OUTPUTS/releases/O-20260906-01/artifacts", output_index[digest][0], digest)})
    elif digest and path.startswith("03_REPORTS/") and "/archive/" in path and digest in output_index:
        base.update({"classification": "duplicate", "disposition_reason": "sealed O-20260906-01 archive exact-content counterpart", "destination": {"root": "3_OUTPUTS/releases/O-20260906-01/artifacts", "path": output_index[digest][0]}, "counterpart": _counterpart("3_OUTPUTS/releases/O-20260906-01/artifacts", output_index[digest][0], digest)})
    else:
        archive = (
            (path.startswith("01_RESEARCH/") and (path.endswith("/README.md") or "/audits/" in path or ("/data/" in path and "/data/graph/" not in path) or "/states/" in path))
            or path.startswith("02_FORECAST/outputs/")
            or (path.startswith("03_REPORTS/") and ("/archive/" in path or path == "03_REPORTS/README.md"))
            or path.startswith("04_SOCIAL/") or path.startswith(".tracking/runs/") or path.startswith("07_MANUAL/archive/")
        )
        if archive:
            base.update({"classification": "archive", "disposition_reason": "archive rule in recovered historical algorithm", "destination": {"root": "ARCHIVE", "path": path}})
        elif path.startswith("03_REPORTS/"):
            base.update({"destination": {"root": "2_ANALYTICS/work", "path": path}, "disposition_reason": "current report runtime evidence; generated work destination"})
        elif path == ".tracking/LATEST.json":
            base.update({"disposition_status": "unresolved", "disposition_reason": "unresolved: tracking-output evidence does not establish its destination root; recovered handoff inventory names 3_OUTPUTS/2_ANALYTICS/tracking while the manifest names 2_ANALYTICS/work/tracking", "disposition_evidence": [{"stream": "live-tree", "finding": ".tracking/LATEST.json is the current tracking output but does not identify a destination root", "supports": "tracking output status only"}, {"stream": "recovered-handoff-inventory-and-manifest", "finding": "destination roots conflict: 3_OUTPUTS/2_ANALYTICS/tracking versus 2_ANALYTICS/work/tracking", "supports": "unresolved pending discriminating evidence"}]})
        elif path == "07_MANUAL/GE16_System_Manual_v3.md":
            base.update({"disposition_status": "resolved", "destination": {"root": "OPS/manual", "path": "GE16_System_Manual_v3.md"}, "disposition_reason": "retained operator manual material belongs in OPS/manual", "disposition_evidence": [{"stream": "live-tree", "finding": "07_MANUAL is retained operator manual material", "supports": "OPS/manual/GE16_System_Manual_v3.md"}]})
        elif path == "01_RESEARCH/data/graph/ge16-knowledge-graph.json":
            base.update({"disposition_status": "unresolved", "disposition_reason": "unresolved: recovered archive evidence conflicts with live graph code that generates and consumes graph/vector artifacts", "disposition_evidence": [{"stream": "recovered-historical-classification", "finding": "01_RESEARCH data is retained archive evidence", "supports": "ARCHIVE"}, {"stream": "live-runtime", "finding": "active graph code generates and consumes graph/vector artifacts", "supports": "2_ANALYTICS/work"}]})
        elif path.startswith("01_RESEARCH/figures/"):
            base.update({"disposition_status": "unresolved", "disposition_reason": "unresolved: recovered archive evidence conflicts with live figures tools that generate and consume figures/vector artifacts", "disposition_evidence": [{"stream": "recovered-historical-classification", "finding": "01_RESEARCH figures are retained archive evidence", "supports": "ARCHIVE"}, {"stream": "live-runtime", "finding": "active figures tools generate and consume figures/vector artifacts", "supports": "2_ANALYTICS/work"}]})
        elif path.startswith("08_HANDOFF/"):
            base.update({"disposition_status": "unresolved", "disposition_reason": "unresolved: live handoff evidence is split between OPS coordination metadata and 2_ANALYTICS work material", "disposition_evidence": [{"stream": "live-tree", "finding": "handoff inventory is OPS coordination metadata", "supports": "OPS"}, {"stream": "live-tree", "finding": "handoff stage material is 2_ANALYTICS work evidence", "supports": "2_ANALYTICS/work"}]})
        else:
            base.update({"disposition_status": "unresolved", "disposition_reason": "unresolved: no path-specific semantic destination recovered"})
    return base


def _literal_references(root: pathlib.Path) -> Dict[str, Any]:
    """Count literal references, matching the recovered rg -F query semantics."""
    files, occurrences, details = 0, 0, []
    if not (root / "05_AUTOMATION").is_dir():
        return {"query": "regular active-code files under 05_AUTOMATION; literal UTF-8 replacement count of 01_RESEARCH", "files": 0, "occurrences": 0, "references": []}
    for record in inventory_tree(root / "05_AUTOMATION", None):
        if record["file_type"] != "regular" or pathlib.PurePosixPath(record["path"]).suffix.lower() not in ACTIVE_CODE_SUFFIXES:
            continue
        text = (root / "05_AUTOMATION" / record["path"]).read_text(encoding="utf-8", errors="replace")
        count = text.count("01_RESEARCH")
        if count:
            files += 1; occurrences += count
            details.append({"path": "05_AUTOMATION/" + record["path"], "literal_occurrences": count, "replacement_root": "unresolved", "reason": "semantic 1_DATA versus generated 2_ANALYTICS/work mapping requires per-call review"})
    return {"query": "regular active-code files under 05_AUTOMATION; literal UTF-8 replacement count of 01_RESEARCH", "files": files, "occurrences": occurrences, "references": details}


def build_phase32_documents(hermes_root: pathlib.Path, data_root: pathlib.Path, outputs_artifacts_root: pathlib.Path) -> Dict[str, Dict[str, Any]]:
    """Build Task 3.2/3.3 evidence from read-only sibling trees.

    This function writes nothing.  It intentionally reports unavailable historical
    path deltas because the recovered record has aggregate totals, not a full path
    baseline.
    """
    hermes_root, data_root, outputs_artifacts_root = (pathlib.Path(os.path.realpath(str(path))) for path in (hermes_root, data_root, outputs_artifacts_root))
    provider = _git_state_provider_for_root(hermes_root)
    all_records = _prefixed_records(hermes_root, HISTORICAL_TARGETS, provider)
    non_code = [record for record in all_records if _historical_non_code(record)]
    data_index, output_index = _hash_index(data_root), _hash_index(outputs_artifacts_root)
    disposition = []
    for record in non_code:
        enriched = dict(record); enriched.update(_classify_phase32(record, data_index, output_index)); disposition.append(enriched)
    categories = {name: {"files": 0, "bytes": 0} for name in ("transient", "duplicate", "archive", "runtime")}
    for record in disposition:
        categories[record["classification"]]["files"] += 1; categories[record["classification"]]["bytes"] += record["lstat_bytes"]
    current_total = {"files": len(disposition), "bytes": sum(record["lstat_bytes"] for record in disposition)}
    embedded = []
    for path in ("01_RESEARCH/figures/search_figures.py", "01_RESEARCH/figures/search_parties.py", "01_RESEARCH/figures/search_personnel.py", "01_RESEARCH/figures/_draft/wiki_probe.py", "01_RESEARCH/figures/_draft/wiki_parse_cabinets.py", "01_RESEARCH/figures/_draft/build_exco_b.py", "01_RESEARCH/data/graph/query_graph.py"):
        item = next((x for x in all_records if x["path"] == path), None)
        embedded.append({"path": path, "sha256": item["sha256"] if item else None, "lstat_bytes": item["lstat_bytes"] if item else None, "present": item is not None})
    # The broad runtime scan is deliberately limited to the two ignored target
    # trees, plus the explicitly named operational entrypoint areas recovered
    # in W01 (forecast logging and the graph-explorer launcher).
    active = _prefixed_records(hermes_root, ("01_RESEARCH", "03_REPORTS", "05_AUTOMATION", "02_FORECAST/engine", "GE16-Graph-Explorer"), provider)
    runtime_files = []
    for record in active:
        if record["active_code_status"] == "candidate":
            runtime = dict(record)
            runtime.update({"classification": "runtime", "disposition_status": "unresolved", "disposition_reason": "active executable/source/config/template candidate; semantic destination unresolved", "destination": None, "counterpart": None})
            runtime_files.append(runtime)
    # This is the complete, disjoint complement of the non-code disposition
    # manifest.  The narrower ``files`` list above is runtime-detail evidence,
    # and can overlap this list; it must not be used for the audited total.
    code_files = [dict(record) for record in all_records if not _historical_non_code(record)]
    reports = [x for x in all_records if x["path"].startswith("03_REPORTS/") and "/latest/" in x["path"] and x["file_type"] == "regular"]
    comparison = {"historical": dict(HISTORICAL_TOTALS, total=HISTORICAL_TOTALS["total"]), "current": dict(categories, total=current_total), "path_reconciliation": {"status": "unavailable", "added": None, "removed": None, "changed": None, "historical_data_sha": "d7e2c16c965a928bc2db66a61e0bc04c45fdd50b", "current_data_sha": "c333a7ac34865b102e108ee297cf637aacdb6dcb", "reason": "aggregate-only historical baseline prevents exact path delta reconstruction; W01 evidence attributes the aggregate change to 14 methodology imports and 8 days of tree drift"}}
    schema_validation = {"status": "passed", "validator": "jsonschema Draft202012Validator", "schemas": ["runtime-dependency-inventory.schema.json", "rename-disposition.schema.json", "current-state.schema.json"]}
    scan_scope = {"runtime_prefixes_scanned": ["01_RESEARCH", "03_REPORTS", "05_AUTOMATION", "02_FORECAST/engine", "GE16-Graph-Explorer"], "sibling_named_directories_unscanned": ["automation", "graph-explorer"], "note": "HERMES/automation and HERMES/graph-explorer are not included in the runtime-prefix scan; notably HERMES/automation/outputs/stage_current_release.py remains outside this inventory."}
    return {"runtime_dependency": {"format": "ge16-runtime-dependency-inventory/v1", "schema": "runtime-dependency-inventory.schema.json", "active_code_rules": {"suffixes": sorted(ACTIVE_CODE_SUFFIXES), "excluded_components": sorted(EXCLUDED_COMPONENTS), "excluded_suffixes": sorted(EXCLUDED_SUFFIXES)}, "files": runtime_files, "code_files": code_files, "embedded_sources": embedded, "automation_references": _literal_references(hermes_root), "latest_reports": {"claim_status": "stale-or-unreproducible", "query": "03_REPORTS/**/latest/** regular files", "files": len(reports), "bytes": sum(x["lstat_bytes"] for x in reports), "records": reports, "consumers": []}, "scan_scope": scan_scope}, "rename_disposition": {"format": "ge16-rename-disposition-manifest/v1", "schema": "rename-disposition.schema.json", "files": disposition, "category_totals": categories, "historical_comparison": comparison}, "current_state": {"format": "ge16-current-state/v1", "source_root": str(hermes_root), "generated_outputs": list(GENERATED_OUTPUT_FILENAMES), "file_count": len(all_records), "active_code_candidate_count": len(runtime_files), "code_file_count": len(code_files), "scan_scope": scan_scope, "unresolved_semantics": ["Unresolved destinations are intentionally not guessed.", "Historical path-level reconciliation is unavailable because only aggregate evidence survived.", "The active-code candidate count derives only from runtime_prefixes_scanned and is independent of non-code disposition classification."], "historical_comparison": comparison, "schema_validation": schema_validation}}


def validate_document(document: Mapping[str, Any], schema_path: pathlib.Path) -> None:
    """Validate with the complete Draft 2020-12 implementation, or fail closed."""
    try:
        import jsonschema  # type: ignore[import-not-found]
    except ImportError as error:
        raise InventoryError(
            "standards-complete JSON Schema validation requires the 'jsonschema' package; refusing partial validation"
        ) from error
    schema = json.loads(pathlib.Path(schema_path).read_text(encoding="utf-8"))
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(schema).validate(document)
    except jsonschema.exceptions.ValidationError as error:
        raise InventoryError("Draft 2020-12 schema validation failed: %s" % error.message) from error
    for field in ("files", "code_files"):
        files = document.get(field)
        if isinstance(files, list):
            paths = [record.get("path") for record in files if isinstance(record, Mapping)]
            if len(paths) != len(files) or len(paths) != len(set(paths)):
                raise InventoryError("semantic validation failed: %s must have unique path values" % field)
    comparison = document.get("historical_comparison")
    if isinstance(comparison, Mapping):
        reconciliation = comparison.get("path_reconciliation")
        if isinstance(reconciliation, Mapping):
            for field in ("added", "removed", "changed"):
                value = reconciliation.get(field)
                if value is not None and not isinstance(value, list):
                    raise InventoryError("semantic validation failed: path_reconciliation.%s must be an array or null" % field)
                if reconciliation.get("status") == "unavailable" and value == []:
                    raise InventoryError("semantic validation failed: path_reconciliation.%s cannot be empty when status is unavailable" % field)


def _open_child_directory_no_follow(parent_fd: int, name: str, expected: Optional[os.stat_result] = None) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as error:
        raise InventoryError("refusing symlink or inaccessible directory: %s" % name) from error
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode) or (expected is not None and not _same_identity(opened, expected)):
        os.close(descriptor)
        raise InventoryError("not a directory: %s" % name)
    return descriptor


def _open_directory_no_follow(path: pathlib.Path, label: str) -> int:
    """Open every absolute ancestor with O_NOFOLLOW, pinning the directory."""
    absolute = pathlib.Path(os.path.abspath(str(path)))
    if not absolute.is_absolute():
        raise InventoryError("%s must be absolute" % label)
    descriptor = os.open(os.sep, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        for component in absolute.parts[1:]:
            next_descriptor = _open_child_directory_no_follow(descriptor, component)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _safe_evidence_root(evidence_root: pathlib.Path) -> tuple[pathlib.Path, int]:
    """Pin the canonical production evidence directory; no look-alike paths."""
    absolute = pathlib.Path(os.path.abspath(str(evidence_root)))
    canonical = pathlib.Path(os.path.abspath(str(OPS_EVIDENCE_ROOT)))
    resolved = absolute.resolve(strict=False)
    canonical_resolved = canonical.resolve(strict=False)
    if resolved != canonical_resolved:
        raise InventoryError("writes are restricted to the canonical OPS/migration/evidence root")
    return canonical_resolved, _open_directory_no_follow(canonical_resolved, "evidence root")


def _canonical_root_is_still_pinned(evidence_root: pathlib.Path, directory_fd: int) -> bool:
    """Detect ancestor replacement before publication while retaining pinned I/O."""
    try:
        current_fd = _open_directory_no_follow(evidence_root, "evidence root")
    except InventoryError:
        return False
    try:
        return _same_identity(os.fstat(current_fd), os.fstat(directory_fd))
    finally:
        os.close(current_fd)


def _entry_stat(directory_fd: int, name: str) -> Optional[os.stat_result]:
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _safe_existing_output(directory_fd: int, name: str) -> Optional[os.stat_result]:
    existing = _entry_stat(directory_fd, name)
    if existing is None:
        return None
    if not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1:
        raise InventoryError("refusing non-regular, linked, or unknown output destination: %s" % name)
    return existing


def _remove_known_staged(directory_fd: int, name: str, expected: os.stat_result) -> None:
    """Remove only a staged entry whose ownership is still proven."""
    current = _entry_stat(directory_fd, name)
    if current is not None and _same_identity(current, expected):
        os.unlink(name, dir_fd=directory_fd)


def _write_staged(directory_fd: int, name: str, payload: Mapping[str, Any]) -> _StagedOutput:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(name, flags, 0o644, dir_fd=directory_fd)
    expected: Optional[os.stat_result] = None
    try:
        # Retain ownership before any fallible content operation so a failed
        # write or fsync cannot strand a known private staging file.
        expected = os.fstat(descriptor)
        if not stat.S_ISREG(expected.st_mode) or expected.st_nlink != 1:
            raise InventoryError("unsafe staged output: %s" % name)
        data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, data[offset:])
            if written <= 0:
                raise InventoryError("short write while staging output: %s" % name)
            offset += written
        os.fsync(descriptor)
        final = os.fstat(descriptor)
        if not _same_identity(final, expected) or final.st_size != len(data):
            raise InventoryError("staged output changed while writing: %s" % name)
        return _StagedOutput(final, len(data), hashlib.sha256(data).hexdigest())
    except Exception:
        if expected is not None:
            try:
                _remove_known_staged(directory_fd, name, expected)
            except OSError as cleanup_error:
                raise InventoryError("staging failed; known temporary cleanup failed: %s" % name) from cleanup_error
        raise
    finally:
        try:
            os.close(descriptor)
        except OSError:
            # A deferred write error (NFS/ENOSPC/EIO) can surface only at
            # close() on the success path, where the except above never ran.
            # The staged identity was captured at line 371, so clean the file
            # up rather than strand a hidden inventory document.  A path whose
            # identity was never established (expected is None) is left alone
            # so preservation still beats deletion.
            if expected is not None:
                try:
                    _remove_known_staged(directory_fd, name, expected)
                except OSError as cleanup_error:
                    raise InventoryError(
                        "staging failed; known temporary cleanup failed: %s" % name
                    ) from cleanup_error
            raise


def _verify_staged_output(directory_fd: int, name: str, expected: _StagedOutput) -> None:
    """Require a pathname to still name the exact staged bytes we created."""
    current = _entry_stat(directory_fd, name)
    if (current is None or not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
            or not _same_identity(current, expected.identity) or current.st_size != expected.length):
        raise InventoryError("staged output changed before publication: %s" % name)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError as error:
        raise InventoryError("staged output inaccessible before publication: %s" % name) from error
    try:
        opened = os.fstat(descriptor)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or not _same_identity(opened, expected.identity) or opened.st_size != expected.length):
            raise InventoryError("staged output changed before publication: %s" % name)
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        final = os.fstat(descriptor)
        if (not _same_identity(final, expected.identity) or final.st_size != expected.length
                or digest.hexdigest() != expected.sha256):
            raise InventoryError("staged output content changed before publication: %s" % name)
    finally:
        os.close(descriptor)


def write_documents(documents: Mapping[str, Mapping[str, Any]], evidence_root: pathlib.Path) -> List[pathlib.Path]:
    """Write exactly the three declared filenames into one explicit evidence root."""
    evidence_root, directory_fd = _safe_evidence_root(pathlib.Path(evidence_root))
    if set(documents) != {"runtime_dependency", "rename_disposition", "current_state"}:
        raise InventoryError("writer requires exactly the three declared documents")
    payloads = (documents["runtime_dependency"], documents["rename_disposition"], documents["current_state"])
    output_paths = [evidence_root / filename for filename in GENERATED_OUTPUT_FILENAMES]
    staged: Dict[str, _StagedOutput] = {}
    previous: Dict[str, Optional[os.stat_result]] = {}
    backups: Dict[str, str] = {}
    published: Dict[str, os.stat_result] = {}
    try:
        for name, payload in zip(GENERATED_OUTPUT_FILENAMES, payloads):
            previous[name] = _safe_existing_output(directory_fd, name)
            temporary = ".%s.%s.tmp" % (name, secrets.token_hex(12))
            staged[temporary] = _write_staged(directory_fd, temporary, payload)
        if not _canonical_root_is_still_pinned(evidence_root, directory_fd):
            raise InventoryError("canonical evidence root changed before publication")
        for name in GENERATED_OUTPUT_FILENAMES:
            current = _entry_stat(directory_fd, name)
            if (current is None) != (previous[name] is None) or (
                current is not None and previous[name] is not None and not _same_identity(current, previous[name])
            ):
                raise InventoryError("output destination changed before publication: %s" % name)
        try:
            for name in GENERATED_OUTPUT_FILENAMES:
                current = _entry_stat(directory_fd, name)
                if (current is None) != (previous[name] is None) or (
                    current is not None and previous[name] is not None and not _same_identity(current, previous[name])
                ):
                    raise InventoryError("output destination changed during publication: %s" % name)
                backup = ".%s.%s.bak" % (name, secrets.token_hex(12))
                if previous[name] is not None:
                    os.rename(name, backup, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                    backups[name] = backup
                temporary = next(key for key in staged if key.startswith(".%s." % name))
                expected = staged[temporary]
                _verify_staged_output(directory_fd, temporary, expected)
                os.rename(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                # The final check catches replacement immediately before the
                # rename; do not count an unknown output as ours for rollback.
                _verify_staged_output(directory_fd, name, expected)
                published[name] = staged.pop(temporary).identity
        except Exception as error:
            rollback_errors = []
            for name in reversed(GENERATED_OUTPUT_FILENAMES):
                current = _entry_stat(directory_fd, name)
                if name in published and current is not None and _same_identity(current, published[name]):
                    os.unlink(name, dir_fd=directory_fd)
                elif name in published:
                    rollback_errors.append(name + " replaced concurrently")
                    continue
                if name in backups:
                    if _entry_stat(directory_fd, name) is None:
                        os.rename(backups[name], name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                    else:
                        rollback_errors.append(name + " cannot restore without replacing")
            for name in GENERATED_OUTPUT_FILENAMES:
                current = _entry_stat(directory_fd, name)
                expected = previous[name]
                if (expected is None and current is not None) or (
                    expected is not None and (current is None or not _same_identity(current, expected))
                ):
                    detail = name + " prior output not restored"
                    if detail not in rollback_errors:
                        rollback_errors.append(detail)
            if rollback_errors:
                raise InventoryError("publication failed; rollback incomplete: " + ", ".join(rollback_errors)) from error
            raise InventoryError("publication failed; prior declared outputs restored") from error
        for name, backup in backups.items():
            old = previous[name]
            current = _entry_stat(directory_fd, backup)
            if old is None or current is None or not _same_identity(old, current):
                raise InventoryError("published outputs retained; refusing to delete changed backup: %s" % name)
            os.unlink(backup, dir_fd=directory_fd)
        os.fsync(directory_fd)
        return output_paths
    finally:
        for temporary, expected in staged.items():
            _remove_known_staged(directory_fd, temporary, expected.identity)
        os.close(directory_fd)


def _serialized_without_previous_generation(documents: Mapping[str, Mapping[str, Any]]) -> bytes:
    """Return the deterministic candidate identity without its audit linkage."""
    copied = json.loads(json.dumps(documents))
    copied["current_state"].pop("previous_generation_sha256", None)
    return (json.dumps(copied, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _attach_previous_generation_sha256(documents: Dict[str, Dict[str, Any]], evidence_root: pathlib.Path) -> None:
    """Link changed generations to the last declared outputs without a SHA chain.

    Repeating an unchanged generation retains its original predecessor, so a
    verification regeneration is byte-identical.  A changed candidate records
    the three files it supersedes.
    """
    root = pathlib.Path(evidence_root).resolve(strict=False)
    existing: Dict[str, bytes] = {}
    for filename in GENERATED_OUTPUT_FILENAMES:
        path = root / filename
        if not path.is_file() or path.is_symlink():
            return
        existing[filename] = path.read_bytes()
    try:
        prior_current = json.loads(existing["ge16-current-state.json"].decode("utf-8"))
        prior_documents = {
            "runtime_dependency": json.loads(existing["ge16-runtime-dependency-inventory.json"].decode("utf-8")),
            "rename_disposition": json.loads(existing["ge16-rename-disposition-manifest.json"].decode("utf-8")),
            "current_state": prior_current,
        }
    except (UnicodeDecodeError, json.JSONDecodeError):
        prior_current = {}
        prior_documents = {}
    retained = prior_current.get("previous_generation_sha256") if isinstance(prior_current, Mapping) else None
    if prior_documents and _serialized_without_previous_generation(prior_documents) == _serialized_without_previous_generation(documents) and isinstance(retained, Mapping):
        documents["current_state"]["previous_generation_sha256"] = dict(retained)
    else:
        documents["current_state"]["previous_generation_sha256"] = {
            filename: hashlib.sha256(existing[filename]).hexdigest()
            for filename in GENERATED_OUTPUT_FILENAMES
        }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=pathlib.Path)
    parser.add_argument("--evidence-root", required=True, type=pathlib.Path)
    parser.add_argument("--schema-root", required=True, type=pathlib.Path)
    parser.add_argument("--data-root", type=pathlib.Path)
    parser.add_argument("--outputs-artifacts-root", type=pathlib.Path)
    arguments = parser.parse_args(argv)
    if (arguments.data_root is None) != (arguments.outputs_artifacts_root is None):
        raise InventoryError("--data-root and --outputs-artifacts-root must be provided together")
    source_root = pathlib.Path(arguments.source_root)
    if not source_root.exists() or not source_root.is_dir():
        raise InventoryError("source root must exist and be a directory: %s" % source_root)
    documents = (build_phase32_documents(arguments.source_root, arguments.data_root, arguments.outputs_artifacts_root)
                 if arguments.data_root is not None else build_documents(arguments.source_root))
    disposition_files = documents["rename_disposition"].get("files", [])
    code_files = documents["runtime_dependency"].get("code_files", documents["runtime_dependency"].get("files", []))
    if not disposition_files and not code_files:
        raise InventoryError("source root produced zero disposition and zero code files: %s" % source_root)
    evidence_root = pathlib.Path(arguments.evidence_root)
    if evidence_root.resolve(strict=False) != OPS_EVIDENCE_ROOT.resolve(strict=False):
        raise InventoryError("CLI output root must be the canonical OPS/migration/evidence root")
    _attach_previous_generation_sha256(documents, OPS_EVIDENCE_ROOT)
    validate_document(documents["runtime_dependency"], arguments.schema_root / "runtime-dependency-inventory.schema.json")
    validate_document(documents["rename_disposition"], arguments.schema_root / "rename-disposition.schema.json")
    validate_document(documents["current_state"], arguments.schema_root / "current-state.schema.json")
    write_documents(documents, OPS_EVIDENCE_ROOT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
