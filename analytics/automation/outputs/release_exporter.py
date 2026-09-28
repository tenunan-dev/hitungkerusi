"""Stage immutable, OUTPUTS-contract-compatible releases from ANALYTICS files.

``artifact_mapping`` is deliberately explicit: each required category maps
release-local artifact paths to the individual source files to export.  For
example::

    {
        "forecast": {"artifacts/forecast/latest.json": Path("...")},
        "report": {"artifacts/reports/federal.md": Path("...")},
        "social": {"artifacts/social/post.md": Path("...")},
        "tracking": {"artifacts/tracking/run.json": Path("...")},
        "state-composition": {"artifacts/state-composition/states.json": Path("...")},
    }

``delivery_semantics`` is also supplied explicitly.  It is the publisher's
complete, immutable selection map and is emitted into ``manifest.json``
without generated roles, paths, or filenames.

The caller supplies a staging root, never an OUTPUTS checkout.  A run ID is
also the immutable release ID, yielding ``<destination>/releases/<run_id>``.
"""

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath


REQUIRED_CATEGORIES = frozenset(
    {"forecast", "report", "social", "tracking", "state-composition"}
)
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
FORBIDDEN_SEGMENTS = frozenset({"current", "latest", "websites", "website", "delivery", "5_websites", "4_delivery"})
DELIVERY_SEMANTIC_FIELDS = frozenset(
    {
        "role",
        "source_path",
        "category",
        "destination_path",
        "content_type",
        "content_schema",
        "content_version",
    }
)
OUTPUTS_CONTRACT_COMMIT = "655064f7994a970ca400bd8b73c5fb9c96600b72"
OUTPUTS_ROOT = Path(__file__).resolve().parents[3] / "3_OUTPUTS"


def _sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _validate_identifier(name, value, pattern):
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError("%s is invalid" % name)


def _canonical_artifact_path(path):
    if not isinstance(path, str) or "\\" in path or not path.startswith("artifacts/"):
        return None
    candidate = PurePosixPath(path)
    if candidate.is_absolute():
        return None
    parts = candidate.parts
    if not (
        len(parts) > 1
        and all(part not in {"", ".", ".."} for part in parts)
        and not any(part.casefold() in FORBIDDEN_SEGMENTS for part in parts)
    ):
        return None
    return str(candidate)


def _canonical_delivery_destination_path(path):
    if not isinstance(path, str) or not path or "\\" in path:
        return None
    candidate = PurePosixPath(path)
    if candidate.is_absolute():
        return None
    parts = candidate.parts
    if not (
        parts
        and parts[0].casefold() != "artifacts"
        and all(part not in {"", ".", ".."} for part in parts)
        and not any(part.casefold() in FORBIDDEN_SEGMENTS for part in parts)
    ):
        return None
    return str(candidate)


def _reject_symlinked_path_components(path, label, *, include_final=True):
    """Reject any existing lexical component without resolving a symlink."""
    absolute_path = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute_path.anchor)
    components = absolute_path.parts[1:] if include_final else absolute_path.parts[1:-1]
    for component in components:
        current /= component
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError("%s cannot be inspected: %s" % (label, current)) from exc
        if stat.S_ISLNK(mode):
            raise ValueError("%s has symlinked path component: %s" % (label, current))


def _reject_symlinked_source_parents(source_path):
    """Reject a source whose spelled path traverses a symlinked directory."""
    _reject_symlinked_path_components(source_path, "declared source", include_final=False)


def _validate_mapping(artifact_mapping):
    if not hasattr(artifact_mapping, "items"):
        raise ValueError("artifact_mapping must map categories to artifact paths")
    categories = set(artifact_mapping)
    missing = REQUIRED_CATEGORIES - categories
    unknown = categories - REQUIRED_CATEGORIES
    if missing:
        raise ValueError("missing required artifact categories: %s" % ", ".join(sorted(missing)))
    if unknown:
        raise ValueError("invalid artifact categories: %s" % ", ".join(sorted(unknown)))

    declared = []
    paths = set()
    for category in sorted(REQUIRED_CATEGORIES):
        entries = artifact_mapping[category]
        if not hasattr(entries, "items") or not entries:
            raise ValueError("artifact category %s must declare at least one artifact" % category)
        for artifact_path, source in entries.items():
            canonical_path = _canonical_artifact_path(artifact_path)
            if canonical_path is None:
                raise ValueError("artifact path must be a safe artifacts/ path: %r" % (artifact_path,))
            if artifact_path != canonical_path:
                raise ValueError("artifact path must be a canonical artifacts/ path: %r" % (artifact_path,))
            if canonical_path in paths:
                raise ValueError("duplicate artifact path: %s" % canonical_path)
            paths.add(canonical_path)
            source_path = Path(source)
            _reject_symlinked_source_parents(source_path)
            try:
                source_mode = source_path.lstat().st_mode
            except OSError as exc:
                raise ValueError("declared source is missing: %s" % source_path) from exc
            if not stat.S_ISREG(source_mode):
                raise ValueError("declared source must be a regular file, not a symlink: %s" % source_path)
            declared.append((canonical_path, category, source_path))
    return sorted(declared)


def _validate_delivery_semantics(delivery_semantics, declared):
    """Validate the supplied publisher-selection map without deriving it."""
    if not hasattr(delivery_semantics, "keys"):
        raise ValueError("delivery_semantics must be an object")
    if set(delivery_semantics) != {"schema", "entries"}:
        raise ValueError("delivery_semantics must contain only schema and entries")
    if delivery_semantics.get("schema") != "outputs.delivery-semantics.v1":
        raise ValueError("delivery_semantics schema is invalid")
    entries = delivery_semantics.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("delivery_semantics entries must be a non-empty list")

    declared_categories = {}
    for artifact_path, category, _source_path in declared:
        declared_categories.setdefault(artifact_path, []).append(category)

    roles = set()
    sources = set()
    destinations = set()
    for index, entry in enumerate(entries):
        label = "delivery semantic %d" % index
        if not hasattr(entry, "keys"):
            raise ValueError("%s must be an object" % label)
        if set(entry) != DELIVERY_SEMANTIC_FIELDS:
            raise ValueError("%s must contain the complete semantic mapping" % label)

        role = entry.get("role")
        if not isinstance(role, str) or not role:
            raise ValueError("%s role must be a non-empty string" % label)
        if role in roles:
            raise ValueError("duplicate delivery semantic role: %s" % role)
        roles.add(role)

        source_path = entry.get("source_path")
        canonical_source_path = _canonical_artifact_path(source_path)
        if canonical_source_path is None:
            raise ValueError("%s source_path must be a safe artifacts/ path" % label)
        if source_path != canonical_source_path:
            raise ValueError("%s source_path must be a canonical artifacts/ path" % label)
        if source_path in sources:
            raise ValueError("duplicate delivery semantic source path: %s" % source_path)
        sources.add(source_path)
        categories = declared_categories.get(source_path, [])
        if len(categories) != 1:
            raise ValueError(
                "%s source_path must match exactly one declared artifact: %s"
                % (label, source_path)
            )
        if entry.get("category") != categories[0]:
            raise ValueError("%s category does not match its declared artifact" % label)

        destination_path = entry.get("destination_path")
        canonical_destination_path = _canonical_delivery_destination_path(destination_path)
        if canonical_destination_path is None:
            raise ValueError("%s destination_path must be a safe delivery destination path" % label)
        if destination_path != canonical_destination_path:
            raise ValueError("%s destination_path must be a canonical delivery destination path" % label)
        if destination_path in destinations:
            raise ValueError("duplicate delivery semantic destination path: %s" % destination_path)
        destinations.add(destination_path)

        if not isinstance(entry.get("content_type"), str) or not entry["content_type"]:
            raise ValueError("%s content_type must be a non-empty string" % label)

    if sources != set(declared_categories):
        raise ValueError("delivery_semantics must map every declared artifact exactly once")


def _validate_destination(destination_root, run_id):
    destination_root = Path(destination_root)
    _reject_symlinked_path_components(destination_root, "destination_root")
    if destination_root.exists() and destination_root.is_symlink():
        raise ValueError("destination_root must not be a symlink")
    releases = destination_root / "releases"
    _reject_symlinked_path_components(releases, "destination releases directory")
    if releases.exists() and (releases.is_symlink() or not releases.is_dir()):
        raise ValueError("destination releases directory must be a real directory")
    release_dir = releases / run_id
    if release_dir.exists() or release_dir.is_symlink():
        raise FileExistsError("release directory already exists: %s" % release_dir)
    return destination_root, releases, release_dir


def _write_new_file(path, contents):
    with path.open("xb") as file:
        file.write(contents)


def _directory_identity(path):
    try:
        metadata = path.lstat()
    except OSError:
        return None
    if not stat.S_ISDIR(metadata.st_mode):
        return None
    return metadata.st_dev, metadata.st_ino


def _source_snapshot(path):
    """Return an immutable-source baseline, rejecting a changing source."""
    _reject_symlinked_source_parents(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("declared source must be a regular file, not a symlink: %s" % path)
    digest = _sha256_file(path)
    after = path.lstat()
    baseline = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    observed = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if observed != baseline:
        raise RuntimeError("source changed while staging artifact: %s" % path)
    return baseline, digest


def _validate_outputs_contract(staging_dir):
    """Run the exact committed OUTPUTS release validator over candidate bytes."""
    result = subprocess.run(
        [
            "git", "-C", os.fspath(OUTPUTS_ROOT), "show",
            "%s:scripts/validate_release_intake.py" % OUTPUTS_CONTRACT_COMMIT,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise ValueError("cannot load committed OUTPUTS contract validator: %s" % detail)
    namespace = {"__name__": "sealed_outputs_contract_validator"}
    try:
        exec(
            compile(result.stdout, "%s:validate_release_intake.py" % OUTPUTS_CONTRACT_COMMIT, "exec"),
            namespace,
        )
        validator = namespace["validate_release_directory"]
    except (KeyError, SyntaxError, TypeError, ValueError) as exc:
        raise ValueError("cannot load committed OUTPUTS contract validator: %s" % exc) from exc
    errors = validator(staging_dir)
    if errors:
        raise ValueError("OUTPUTS contract validation failed: %s" % "; ".join(map(str, errors)))


def stage_release(
    destination_root,
    *,
    analytics_commit_sha,
    data_commit_sha,
    run_id,
    artifact_mapping,
    delivery_semantics,
):
    """Materialize one new, unsealed OUTPUTS-style release in ``destination_root``.

    All arguments and every declared source are validated before the output
    directory is created.  Existing releases always fail; this function never
    modifies an existing release or any source artifact.
    """
    _validate_identifier("analytics_commit_sha", analytics_commit_sha, GIT_SHA)
    _validate_identifier("data_commit_sha", data_commit_sha, GIT_SHA)
    _validate_identifier("run_id", run_id, RUN_ID)
    if run_id.casefold() in {"current", "latest"}:
        raise ValueError("run_id must not be a mutable release alias")
    declared = _validate_mapping(artifact_mapping)
    _validate_delivery_semantics(delivery_semantics, declared)
    destination_root, releases, release_dir = _validate_destination(destination_root, run_id)
    destination_root.mkdir(parents=True, exist_ok=True)
    releases.mkdir(exist_ok=True)
    stage_parent = Path(tempfile.mkdtemp(prefix=".%s.staging-" % run_id, dir=str(destination_root)))
    staging_dir = stage_parent / "releases" / run_id
    staging_dir.mkdir(parents=True)
    staging_identity = None
    try:
        artifacts_root = staging_dir / "artifacts"
        artifacts_root.mkdir()

        artifacts = []
        for artifact_path, category, source_path in declared:
            destination = staging_dir / PurePosixPath(artifact_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            source_metadata, source_hash = _source_snapshot(source_path)
            shutil.copyfile(str(source_path), str(destination), follow_symlinks=False)
            destination_hash = _sha256_file(destination)
            source_metadata_after, source_hash_after = _source_snapshot(source_path)
            if (
                destination_hash != source_hash
                or source_metadata_after != source_metadata
                or source_hash_after != source_hash
            ):
                raise RuntimeError("source changed while staging artifact: %s" % source_path)
            artifacts.append(
                {
                    "path": artifact_path,
                    "category": category,
                    "sha256": destination_hash,
                    "bytes": destination.stat().st_size,
                }
            )

        artifacts.sort(key=lambda artifact: artifact["path"])
        sums_contents = "".join(
            "%s  %s\n" % (artifact["sha256"], artifact["path"]) for artifact in artifacts
        ).encode("utf-8")
        sums_path = staging_dir / "SHA256SUMS"
        _write_new_file(sums_path, sums_contents)

        manifest = {
            "schema": "outputs.release-manifest.v1",
            "release_id": run_id,
            "provenance": {
                "analytics_commit_sha": analytics_commit_sha,
                "data_commit_sha": data_commit_sha,
                "run_id": run_id,
                "artifact_manifest_sha256": _sha256_file(sums_path),
            },
            "artifacts": artifacts,
            "delivery_semantics": delivery_semantics,
        }
        manifest_path = staging_dir / "manifest.json"
        _write_new_file(manifest_path, _json_bytes(manifest))

        receipt = {
            "schema": "outputs.release-receipt.v1",
            "release_id": run_id,
            "manifest_sha256": _sha256_file(manifest_path),
            "sha256sums_sha256": _sha256_file(sums_path),
            "artifacts": [
                {key: artifact[key] for key in ("path", "sha256", "bytes")}
                for artifact in artifacts
            ],
        }
        _write_new_file(staging_dir / "receipt.json", _json_bytes(receipt))

        _validate_outputs_contract(staging_dir)

        if release_dir.exists() or release_dir.is_symlink():
            raise FileExistsError("release directory already exists: %s" % release_dir)
        staging_identity = _directory_identity(staging_dir)
        staging_dir.rename(release_dir)
    except BaseException:
        if staging_identity is not None and _directory_identity(release_dir) == staging_identity:
            shutil.rmtree(release_dir, ignore_errors=True)
        shutil.rmtree(stage_parent, ignore_errors=True)
        raise
    shutil.rmtree(stage_parent, ignore_errors=True)
    return release_dir
