#!/usr/bin/env python3
"""Validate an immutable OUTPUTS release manifest and, optionally, its files."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath


REQUIRED_PROVENANCE = {
    "analytics_commit_sha",
    "data_commit_sha",
    "run_id",
    "artifact_manifest_sha256",
}
REQUIRED_CATEGORIES = {"forecast", "report", "social", "tracking", "state-composition"}
REQUIRED_ARTIFACT_FIELDS = {"path", "category", "sha256", "bytes"}
REQUIRED_SEMANTIC_FIELDS = {
    "role", "source_path", "category", "destination_path", "content_type",
    "content_schema", "content_version",
}
REQUIRED_RECEIPT_FIELDS = {"schema", "release_id", "manifest_sha256", "sha256sums_sha256", "artifacts"}
REQUIRED_RECEIPT_ARTIFACT_FIELDS = {"path", "sha256", "bytes"}
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
FORBIDDEN_SEGMENTS = {"current", "latest", "websites", "website", "delivery", "5_websites", "4_delivery"}
CONTROL_FILES = {"manifest.json", "SHA256SUMS", "receipt.json", "artifacts"}
STATES = (
    ("johor", "Johor"), ("kedah", "Kedah"), ("kelantan", "Kelantan"),
    ("melaka", "Melaka"), ("negeri-sembilan", "Negeri Sembilan"),
    ("pahang", "Pahang"), ("perak", "Perak"), ("perlis", "Perlis"),
    ("pulau-pinang", "Pulau Pinang"), ("sabah", "Sabah"),
    ("sarawak", "Sarawak"), ("selangor", "Selangor"),
    ("terengganu", "Terengganu"),
)
PRN_STATE_IDS = {"melaka", "pahang", "perak", "perlis", "sarawak"}


def semantic_entry(role, source_path, category, destination_path, content_type,
                   content_schema=None, content_version=None):
    return {
        "role": role,
        "source_path": source_path,
        "category": category,
        "destination_path": destination_path,
        "content_type": content_type,
        "content_schema": content_schema,
        "content_version": content_version,
    }


def required_delivery_entries():
    """Return the publisher's complete generated-file inventory by semantic role."""
    entries = [
        semantic_entry("app-data", "artifacts/data/app-data.json", "forecast", "data/app-data.json", "application/json", "hermes.delivery.app-data", 1),
        semantic_entry("forecast", "artifacts/data/forecast.json", "forecast", "data/forecast.json", "application/json", "hermes.delivery.forecast", 1),
        semantic_entry("forecast-engine", "artifacts/data/ge16-forecast-latest.json", "forecast", "data/ge16-forecast-latest.json", "application/json", "hermes.forecast.engine-output", 1),
        semantic_entry("scenarios", "artifacts/data/scenarios.json", "forecast", "data/scenarios.json", "application/json", "hermes.delivery.scenarios", 1),
        semantic_entry("report.federal.en", "artifacts/reports/federal/GE16_Malaysia_General_Election_Report.md", "report", "reports/federal/GE16_Malaysia_General_Election_Report.md", "text/markdown"),
        semantic_entry("report.federal.ms", "artifacts/reports/federal/GE16_Malaysia_General_Election_Report_MS.md", "report", "reports/federal/GE16_Malaysia_General_Election_Report_MS.md", "text/markdown"),
        semantic_entry("social.payload", "artifacts/social/social-payload.json", "social", "social/social-payload.json", "application/json", "hermes.delivery.social-payload", 1),
        semantic_entry("tracking.payload", "artifacts/tracking/tracking-payload.json", "tracking", "tracking/tracking-payload.json", "application/json", "hermes.delivery.tracking-payload", 1),
    ]
    for state_id, state_name in STATES:
        root = "reports/states/DUN {}".format(state_name)
        entries.extend((
            semantic_entry("report.state.{}.en".format(state_id), "artifacts/{}/GE16_{}_Report.md".format(root, state_name), "report", "{}/GE16_{}_Report.md".format(root, state_name), "text/markdown"),
            semantic_entry("report.state.{}.ms".format(state_id), "artifacts/{}/GE16_{}_Report_MS.md".format(root, state_name), "report", "{}/GE16_{}_Report_MS.md".format(root, state_name), "text/markdown"),
            semantic_entry("report.state-summary.{}.en".format(state_id), "artifacts/{}/dun-election-summary.md".format(root), "report", "{}/dun-election-summary.md".format(root), "text/markdown"),
            semantic_entry("report.state-summary.{}.ms".format(state_id), "artifacts/{}/dun-election-summary_MS.md".format(root), "report", "{}/dun-election-summary_MS.md".format(root), "text/markdown"),
            semantic_entry("report.state-deepdive.{}".format(state_id), "artifacts/{}/ge16-battleground-deepdive.md".format(root), "report", "{}/ge16-battleground-deepdive.md".format(root), "text/markdown"),
        ))
    for state_id, state_name in STATES:
        if state_id in PRN_STATE_IDS:
            root = "reports/states/DUN {}".format(state_name)
            entries.append(semantic_entry("report.prn-projection.{}".format(state_id), "artifacts/{}/{}-prn-projection-report.md".format(root, state_id), "report", "{}/{}-prn-projection-report.md".format(root, state_id), "text/markdown"))
            entries.append(semantic_entry("state-composition.{}".format(state_id), "artifacts/state-composition/{}.json".format(state_id), "state-composition", "state-composition/{}.json".format(state_id), "application/json", "hermes.delivery.state-composition", 1))
    return entries


REQUIRED_DELIVERY_ENTRIES = {entry["role"]: entry for entry in required_delivery_entries()}


def fail(errors, condition, message):
    if not condition:
        errors.append(message)


def path_is_safe(path):
    """Return whether an artifact path is a safe release-local path."""
    if not isinstance(path, str):
        return False
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or "\\" in path or not path.startswith("artifacts/"):
        return False
    parts = candidate.parts
    return (
        len(parts) > 1
        and all(part not in {"", ".", ".."} for part in parts)
        and not any(part.lower() in FORBIDDEN_SEGMENTS for part in parts)
    )


def destination_path_is_safe(path):
    """Return whether a delivery destination is a safe relative path."""
    if not isinstance(path, str) or not path or "\\" in path:
        return False
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or path.startswith("artifacts/"):
        return False
    return (
        len(candidate.parts) > 0
        and all(part not in {"", ".", ".."} for part in candidate.parts)
        and not any(part.lower() in FORBIDDEN_SEGMENTS for part in candidate.parts)
    )


def validate_delivery_semantics(manifest):
    """Validate the immutable publisher-selection map in a release manifest."""
    errors = []
    semantics = manifest.get("delivery_semantics")
    if not isinstance(semantics, dict):
        return ["delivery_semantics must be an object"]
    fail(errors, set(semantics) == {"schema", "entries"}, "delivery_semantics must contain only schema and entries")
    fail(errors, semantics.get("schema") == "outputs.delivery-semantics.v1", "invalid delivery_semantics schema")
    entries = semantics.get("entries")
    fail(errors, isinstance(entries, list), "delivery_semantics entries must be a list")
    if not isinstance(entries, list):
        return errors

    artifacts = manifest.get("artifacts")
    artifact_categories = {}
    if isinstance(artifacts, list):
        for artifact in artifacts:
            if isinstance(artifact, dict) and isinstance(artifact.get("path"), str):
                artifact_categories.setdefault(artifact["path"], []).append(artifact.get("category"))

    roles = set()
    sources = set()
    destinations = set()
    actual_by_role = {}
    for index, entry in enumerate(entries):
        label = "delivery semantic {}".format(index)
        fail(errors, isinstance(entry, dict), "{} must be an object".format(label))
        if not isinstance(entry, dict):
            continue
        fail(errors, set(entry) == REQUIRED_SEMANTIC_FIELDS, "{} must contain the complete semantic mapping".format(label))
        role = entry.get("role")
        fail(errors, isinstance(role, str) and role in REQUIRED_DELIVERY_ENTRIES, "{} role is not a required delivery role".format(label))
        if isinstance(role, str):
            fail(errors, role not in roles, "duplicate delivery semantic role: {}".format(role))
            roles.add(role)
            actual_by_role[role] = entry
        source_path = entry.get("source_path")
        fail(errors, path_is_safe(source_path), "{} source_path must be a safe artifacts/ path".format(label))
        if isinstance(source_path, str):
            fail(errors, source_path not in sources, "duplicate delivery semantic source path: {}".format(source_path))
            sources.add(source_path)
            categories = artifact_categories.get(source_path, [])
            fail(errors, len(categories) == 1, "{} source_path must match exactly one declared artifact: {}".format(label, source_path))
            fail(errors, categories == [entry.get("category")], "{} category does not match its declared artifact".format(label))
        destination_path = entry.get("destination_path")
        fail(errors, destination_path_is_safe(destination_path), "{} destination_path must be a safe relative path".format(label))
        if isinstance(destination_path, str):
            fail(errors, destination_path not in destinations, "duplicate delivery semantic destination path: {}".format(destination_path))
            destinations.add(destination_path)
        fail(errors, entry.get("category") in REQUIRED_CATEGORIES, "{} category is invalid".format(label))
        fail(errors, isinstance(entry.get("content_type"), str) and entry["content_type"], "{} content_type is required".format(label))

    expected_roles = set(REQUIRED_DELIVERY_ENTRIES)
    for role in sorted(expected_roles - roles):
        errors.append("missing required delivery semantic role: {}".format(role))
    for role in sorted(roles - expected_roles):
        errors.append("unexpected delivery semantic role: {}".format(role))
    for role in sorted(expected_roles & roles):
        expected = REQUIRED_DELIVERY_ENTRIES[role]
        actual = actual_by_role[role]
        for field in REQUIRED_SEMANTIC_FIELDS - {"role"}:
            fail(errors, actual.get(field) == expected[field], "delivery semantic {} has an invalid {}".format(role, field))

    artifact_paths = set(artifact_categories)
    fail(errors, sources == artifact_paths, "delivery_semantics must map every declared artifact exactly once")
    return errors


def receipt_artifact_baseline(manifest):
    """Return the artifact fields a receipt must preserve for staging consistency."""
    artifacts = manifest.get("artifacts") if isinstance(manifest, dict) else None
    if not isinstance(artifacts, list):
        return None
    baseline = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            return None
        baseline.append({key: artifact.get(key) for key in ("path", "sha256", "bytes")})
    return baseline


def validate_manifest(manifest):
    """Return manifest validation errors; empty means the descriptor is valid."""
    errors = []
    if not isinstance(manifest, dict):
        return ["release manifest must be a JSON object"]

    fail(errors, manifest.get("schema") == "outputs.release-manifest.v1", "invalid release manifest schema")
    release_id = manifest.get("release_id")
    fail(errors, isinstance(release_id, str) and RUN_ID.fullmatch(release_id), "release_id must be a safe non-empty identifier")
    fail(
        errors,
        not isinstance(release_id, str) or release_id.casefold() not in {"current", "latest"},
        "mutable release aliases are prohibited",
    )

    provenance = manifest.get("provenance")
    fail(errors, isinstance(provenance, dict), "provenance must be an object")
    if isinstance(provenance, dict):
        missing = REQUIRED_PROVENANCE - set(provenance)
        for field in sorted(missing):
            errors.append(f"missing required provenance: {field}")
        fail(
            errors,
            isinstance(provenance.get("analytics_commit_sha"), str)
            and GIT_SHA.fullmatch(provenance["analytics_commit_sha"]) is not None,
            "analytics_commit_sha must be a 40-character lowercase Git commit SHA",
        )
        fail(
            errors,
            isinstance(provenance.get("data_commit_sha"), str) and GIT_SHA.fullmatch(provenance["data_commit_sha"]) is not None,
            "data_commit_sha must be a 40-character lowercase Git commit SHA",
        )
        fail(
            errors,
            isinstance(provenance.get("run_id"), str) and RUN_ID.fullmatch(provenance["run_id"]) is not None,
            "run_id must be a safe non-empty identifier",
        )
        fail(
            errors,
            isinstance(provenance.get("artifact_manifest_sha256"), str)
            and SHA256.fullmatch(provenance["artifact_manifest_sha256"]) is not None,
            "artifact_manifest_sha256 must be a lowercase SHA-256 digest",
        )

    artifacts = manifest.get("artifacts")
    fail(errors, isinstance(artifacts, list) and artifacts, "artifacts must be a non-empty list")
    if isinstance(artifacts, list):
        paths = set()
        categories = set()
        for index, artifact in enumerate(artifacts):
            label = f"artifact {index}"
            fail(errors, isinstance(artifact, dict), f"{label} must be an object")
            if not isinstance(artifact, dict):
                continue
            fail(errors, set(artifact) == REQUIRED_ARTIFACT_FIELDS, f"{label} must contain only path, category, sha256, and bytes")
            path = artifact.get("path")
            fail(errors, path_is_safe(path), f"{label} path must be a safe artifacts/ path without aliases, website, or DELIVERY segments")
            if isinstance(path, str):
                fail(errors, path not in paths, f"duplicate artifact path: {path}")
                paths.add(path)
            category = artifact.get("category")
            fail(errors, category in REQUIRED_CATEGORIES, f"{label} category is invalid")
            if isinstance(category, str):
                categories.add(category)
            digest = artifact.get("sha256")
            fail(errors, isinstance(digest, str) and SHA256.fullmatch(digest) is not None, f"{label} sha256 must be a lowercase SHA-256 digest")
            size = artifact.get("bytes")
            fail(errors, isinstance(size, int) and not isinstance(size, bool) and size >= 0, f"{label} bytes must be a non-negative integer")
        for category in sorted(REQUIRED_CATEGORIES - categories):
            errors.append(f"missing required artifact category: {category}")
    errors.extend(validate_delivery_semantics(manifest))
    return errors


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_sha256sums(contents):
    entries = {}
    errors = []
    for number, line in enumerate(contents.splitlines(), 1):
        match = re.fullmatch(r"([0-9a-f]{64})  (artifacts/[^\\]+)", line)
        if match is None:
            errors.append(f"SHA256SUMS line {number} is invalid")
            continue
        digest, path = match.groups()
        if not path_is_safe(path):
            errors.append(f"SHA256SUMS line {number} has an unsafe artifact path")
            continue
        if path in entries:
            errors.append(f"SHA256SUMS duplicate path: {path}")
        entries[path] = digest
    return entries, errors


def validate_receipt(receipt, manifest, manifest_path, sums_path):
    """Validate the release-local consistency record used during staging."""
    errors = []
    if not isinstance(receipt, dict):
        return ["release receipt must be a JSON object"]
    fail(errors, set(receipt) == REQUIRED_RECEIPT_FIELDS, "release receipt must contain only consistency-record fields")
    fail(errors, receipt.get("schema") == "outputs.release-receipt.v1", "invalid release receipt schema")
    release_id = receipt.get("release_id")
    fail(errors, isinstance(release_id, str) and RUN_ID.fullmatch(release_id), "receipt release_id must be a safe non-empty identifier")
    fail(
        errors,
        not isinstance(release_id, str) or release_id.casefold() not in {"current", "latest"},
        "receipt mutable release aliases are prohibited",
    )
    fail(
        errors,
        isinstance(receipt.get("manifest_sha256"), str) and SHA256.fullmatch(receipt["manifest_sha256"]) is not None,
        "receipt manifest_sha256 must be a lowercase SHA-256 digest",
    )
    fail(
        errors,
        isinstance(receipt.get("sha256sums_sha256"), str) and SHA256.fullmatch(receipt["sha256sums_sha256"]) is not None,
        "receipt sha256sums_sha256 must be a lowercase SHA-256 digest",
    )
    artifacts = receipt.get("artifacts")
    fail(errors, isinstance(artifacts, list) and artifacts, "receipt artifacts must be a non-empty list")
    if isinstance(artifacts, list):
        paths = set()
        for index, artifact in enumerate(artifacts):
            label = f"receipt artifact {index}"
            fail(errors, isinstance(artifact, dict), f"{label} must be an object")
            if not isinstance(artifact, dict):
                continue
            fail(errors, set(artifact) == REQUIRED_RECEIPT_ARTIFACT_FIELDS, f"{label} must contain only path, sha256, and bytes")
            path = artifact.get("path")
            fail(errors, path_is_safe(path), f"{label} path must be a safe artifacts/ path")
            if isinstance(path, str):
                fail(errors, path not in paths, f"duplicate receipt artifact path: {path}")
                paths.add(path)
            digest = artifact.get("sha256")
            fail(errors, isinstance(digest, str) and SHA256.fullmatch(digest) is not None, f"{label} sha256 must be a lowercase SHA-256 digest")
            size = artifact.get("bytes")
            fail(errors, isinstance(size, int) and not isinstance(size, bool) and size >= 0, f"{label} bytes must be a non-negative integer")

    fail(errors, receipt.get("release_id") == manifest.get("release_id"), "receipt release_id does not match manifest")
    fail(errors, sha256_file(manifest_path) == receipt.get("manifest_sha256"), "receipt manifest baseline does not match manifest.json")
    fail(errors, sha256_file(sums_path) == receipt.get("sha256sums_sha256"), "receipt SHA256SUMS baseline does not match SHA256SUMS")
    fail(errors, artifacts == receipt_artifact_baseline(manifest), "receipt artifact baseline does not match manifest")
    return errors


def has_symlink_component(root, relative_path):
    """Return whether any lexical component of a release-local path is a symlink."""
    candidate = root
    for part in PurePosixPath(relative_path).parts:
        candidate /= part
        if candidate.is_symlink():
            return True
    return False


def git_output(repository, arguments):
    """Run a local Git read command and return its completed process."""
    return subprocess.run(
        ["git", "-C", os.fspath(repository), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def sealed_validation_errors(release_dir, sealed_commit):
    """Compare a release directory exactly with its local Git commit tree."""
    if not isinstance(sealed_commit, str) or GIT_SHA.fullmatch(sealed_commit) is None:
        return ["sealed validation requires an exact 40-character lowercase Git commit SHA"]

    repository_result = git_output(release_dir, ["rev-parse", "--show-toplevel"])
    if repository_result.returncode != 0:
        return ["sealed validation requires the release directory to be inside a local Git repository"]
    repository = Path(repository_result.stdout.decode("utf-8", "strict").strip()).resolve()
    try:
        release_relative = release_dir.resolve().relative_to(repository).as_posix()
    except ValueError:
        return ["sealed validation release directory is outside its local Git repository"]

    object_type = git_output(repository, ["cat-file", "-t", sealed_commit])
    if object_type.returncode != 0:
        return [f"sealed validation commit object does not exist locally: {sealed_commit}"]
    if object_type.stdout.strip() != b"commit":
        return [f"sealed validation object is not a commit: {sealed_commit}"]

    tree_result = git_output(repository, ["ls-tree", "-r", "-z", "--full-tree", sealed_commit, "--", release_relative])
    if tree_result.returncode != 0:
        return [f"sealed validation cannot read commit tree: {sealed_commit}"]

    committed = {}
    errors = []
    for entry in tree_result.stdout.split(b"\0"):
        if not entry:
            continue
        header, separator, path_bytes = entry.partition(b"\t")
        fields = header.split()
        if not separator or len(fields) != 3:
            errors.append("sealed validation encountered an invalid Git tree entry")
            continue
        mode, object_type, object_id = fields
        path = os.fsdecode(path_bytes)
        committed[path] = (mode, object_type, object_id)
    if not committed:
        return errors + [f"sealed validation commit does not contain release: {release_relative}"]

    actual = set()
    for candidate in release_dir.rglob("*"):
        if candidate.is_file() or candidate.is_symlink():
            actual.add((PurePosixPath(release_relative) / candidate.relative_to(release_dir)).as_posix())
    committed_paths = set(committed)
    for path in sorted(committed_paths - actual):
        errors.append(f"sealed validation working tree is missing committed release path: {path}")
    for path in sorted(actual - committed_paths):
        errors.append(f"sealed validation working tree has extra release path: {path}")

    for path in sorted(committed_paths & actual):
        mode, object_type, object_id = committed[path]
        if mode not in {b"100644", b"100755"} or object_type != b"blob":
            errors.append(f"sealed validation commit path is not a regular file blob: {path}")
            continue
        file_path = repository / path
        if file_path.is_symlink() or not file_path.is_file():
            errors.append(f"sealed validation working tree path is not a regular file: {path}")
            continue
        blob = git_output(repository, ["cat-file", "blob", object_id.decode("ascii")])
        if blob.returncode != 0:
            errors.append(f"sealed validation cannot read committed blob: {path}")
        elif file_path.read_bytes() != blob.stdout:
            errors.append(f"sealed validation working tree content differs from commit: {path}")
    return errors


def validate_release_directory(release_dir, sealed_commit=None):
    """Validate staging structure, then optionally compare it with a sealing commit."""
    errors = []
    release_dir = Path(release_dir)
    fail(errors, release_dir.is_dir() and not release_dir.is_symlink(), "release directory must be a real directory")
    fail(errors, release_dir.parent.name == "releases", "release directory must be directly under releases/")
    fail(
        errors,
        release_dir.name.casefold() not in {"current", "latest"},
        "mutable release aliases are prohibited",
    )
    if errors:
        return errors
    root_entries = {entry.name for entry in release_dir.iterdir()}
    for entry in sorted(root_entries - CONTROL_FILES):
        errors.append(f"undeclared release root entry: {entry}")
    manifest_path = release_dir / "manifest.json"
    sums_path = release_dir / "SHA256SUMS"
    receipt_path = release_dir / "receipt.json"
    control_files_valid = True
    if manifest_path.is_symlink():
        errors.append("release manifest.json must not be a symlink")
        control_files_valid = False
    elif not manifest_path.is_file():
        errors.append("release manifest.json is required")
        control_files_valid = False
    if sums_path.is_symlink():
        errors.append("release SHA256SUMS must not be a symlink")
        control_files_valid = False
    elif not sums_path.is_file():
        errors.append("release SHA256SUMS is required")
        control_files_valid = False
    if receipt_path.is_symlink():
        errors.append("release receipt.json must not be a symlink")
        control_files_valid = False
    elif not receipt_path.is_file():
        errors.append("release receipt.json is required")
        control_files_valid = False
    if not control_files_valid:
        return errors
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return errors + [f"invalid release manifest input: {exc}"]
    errors.extend(validate_manifest(manifest))
    fail(errors, manifest.get("release_id") == release_dir.name, "manifest release_id must match its release directory")
    try:
        sums_contents = sums_path.read_text(encoding="utf-8")
    except OSError as exc:
        return errors + [f"cannot read SHA256SUMS: {exc}"]
    provenance = manifest.get("provenance", {})
    expected_sums_hash = provenance.get("artifact_manifest_sha256") if isinstance(provenance, dict) else None
    fail(errors, sha256_file(sums_path) == expected_sums_hash, "SHA256SUMS hash does not match artifact_manifest_sha256")
    sum_entries, sum_errors = parse_sha256sums(sums_contents)
    errors.extend(sum_errors)

    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return errors + [f"invalid release receipt input: {exc}"]
    errors.extend(validate_receipt(receipt, manifest, manifest_path, sums_path))

    artifacts = manifest.get("artifacts", [])
    expected_paths = set()
    if isinstance(artifacts, list):
        for artifact in artifacts:
            if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str):
                continue
            path = artifact["path"]
            expected_paths.add(path)
            file_path = release_dir / path
            is_symlinked = has_symlink_component(release_dir, path)
            fail(errors, not is_symlinked, f"declared artifact must not be a symlink: {path}")
            fail(errors, file_path.is_file() and not is_symlinked, f"declared artifact is missing or not a regular file: {path}")
            if file_path.is_file() and not is_symlinked:
                fail(errors, file_path.stat().st_size == artifact.get("bytes"), f"artifact bytes do not match manifest: {path}")
                fail(errors, sha256_file(file_path) == artifact.get("sha256"), f"artifact hash does not match manifest: {path}")
            fail(errors, sum_entries.get(path) == artifact.get("sha256"), f"SHA256SUMS does not match manifest: {path}")
    fail(errors, set(sum_entries) == expected_paths, "SHA256SUMS must list exactly the declared artifacts")

    artifact_root = release_dir / "artifacts"
    if artifact_root.is_symlink():
        errors.append("artifacts directory must not be a symlink")
    elif artifact_root.is_dir():
        actual_paths = set()
        for file in artifact_root.rglob("*"):
            if file.is_symlink():
                errors.append(f"artifacts directory must not contain symlinks: {file.relative_to(release_dir).as_posix()}")
            elif file.is_file():
                actual_paths.add(file.relative_to(release_dir).as_posix())
            elif not file.is_dir():
                errors.append(f"artifacts directory contains a non-regular entry: {file.relative_to(release_dir).as_posix()}")
        fail(errors, actual_paths == expected_paths, "artifacts directory must contain exactly the declared artifacts")
    else:
        errors.append("artifacts directory is required")
    if sealed_commit is not None:
        errors.extend(sealed_validation_errors(release_dir, sealed_commit))
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("manifest", nargs="?", default="manifest.json")
    source.add_argument("--stdin", action="store_true", help="read a release manifest from standard input")
    source.add_argument("--release-dir", type=Path, help="verify a materialized releases/<release_id> directory")
    parser.add_argument(
        "--sealed-commit",
        help="exact local 40-character commit SHA whose tree must exactly match --release-dir",
    )
    args = parser.parse_args()
    if args.sealed_commit is not None and args.release_dir is None:
        parser.error("--sealed-commit requires --release-dir")
    try:
        if args.release_dir:
            errors = validate_release_directory(args.release_dir, sealed_commit=args.sealed_commit)
        else:
            manifest = json.load(sys.stdin) if args.stdin else json.loads(Path(args.manifest).read_text(encoding="utf-8"))
            errors = validate_manifest(manifest)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"invalid release intake input: {exc}", file=sys.stderr)
        return 2
    if errors:
        for error in errors:
            print(f"invalid release intake: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
