#!/usr/bin/env python3
"""Validate the byte-level provenance of the DATA canonical-data extraction."""

import argparse
import errno
import hashlib
import json
import os
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Optional

try:
    from methodology_contract import (
        METHODOLOGY_DESTINATION_ROOTS,
        METHODOLOGY_INPUT_PATH_PAIRS,
        METHODOLOGY_INPUTS,
    )
except ImportError:  # Supports importing this script as scripts.validate_canonical_data.
    from scripts.methodology_contract import (
        METHODOLOGY_DESTINATION_ROOTS,
        METHODOLOGY_INPUT_PATH_PAIRS,
        METHODOLOGY_INPUTS,
    )


HISTORIC_ROOTS = (
    ("../HERMES/01_RESEARCH/data/raw", "research/raw"),
    ("../HERMES/01_RESEARCH/data/derived", "research/derived"),
    ("../HERMES/01_RESEARCH/data/trackers", "research/trackers"),
    ("../HERMES/01_RESEARCH/federal", "research/federal"),
    ("../HERMES/01_RESEARCH/geo", "geo"),
    ("../HERMES/01_RESEARCH/states", "research/states"),
)
CANONICAL_ROOTS = tuple(destination_root for _, destination_root in HISTORIC_ROOTS)
SNAPSHOT_COMMIT = "6ce692d29bc3831a4cf72dba996f6ee0a3f2a98c"
METHODOLOGY_INPUT_FIELDS = {
    "canonical_path",
    "legacy_source_relative_path",
    "sha256",
    "bytes",
    "classification",
    "consumer_roles",
}
MANIFEST_FIELDS = {
    "schema",
    "root_set",
    "snapshot_commit",
    "format_exceptions",
    "historic_import",
    "roots",
    "files",
    "methodology_inputs",
}
FORMAT_EXCEPTION_DOCUMENTATION = (
    "Source-preserved whitespace is intentional; canonical source and destination "
    "bytes must remain byte-identical."
)
FORMAT_EXCEPTION_KINDS = {"CRLF", "blank-at-EOF"}
_ACTIVE_DIRECTORY_IDENTITIES = None
_ACTIVE_DIRECTORY_DESCRIPTORS = None


class ValidationError(Exception):
    pass


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _verify_directory_descriptor(descriptor: int, path: Path, label: str) -> None:
    try:
        result = os.fstat(descriptor)
    except OSError as exc:
        raise ValidationError(f"cannot inspect {label}: {path}: {exc}") from exc
    if not stat.S_ISDIR(result.st_mode):
        raise ValidationError(f"{label} is not a directory: {path}")
    if _ACTIVE_DIRECTORY_IDENTITIES is not None:
        expected = _ACTIVE_DIRECTORY_IDENTITIES.get(path.absolute())
        identity = (result.st_dev, result.st_ino)
        if expected is None:
            _ACTIVE_DIRECTORY_IDENTITIES[path.absolute()] = identity
        elif expected != identity:
            raise ValidationError(f"{label} changed identity: {path}")


def _open_selected_root(path: Path, label: str) -> int:
    try:
        descriptor = os.open(os.fspath(path), _directory_open_flags())
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValidationError(f"{label} is a symlink: {path}") from exc
        raise ValidationError(f"cannot open {label}: {path}: {exc}") from exc
    try:
        _verify_directory_descriptor(descriptor, path, label)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_anchored_directory(path: Path, label: str) -> int:
    """Open a selected directory through retained directory descriptors."""
    if not _ACTIVE_DIRECTORY_DESCRIPTORS:
        raise ValidationError(f"no selected directory anchor for {label}: {path}")
    absolute_path = path.absolute()
    candidates = []
    for root_path in _ACTIVE_DIRECTORY_DESCRIPTORS:
        try:
            absolute_path.relative_to(root_path)
        except ValueError:
            continue
        candidates.append(root_path)
    if not candidates:
        raise ValidationError(f"path is outside selected directory anchors: {path}")
    selected_root = max(candidates, key=lambda candidate: len(candidate.parts))
    descriptor = os.dup(_ACTIVE_DIRECTORY_DESCRIPTORS[selected_root])
    logical_path = selected_root
    try:
        _verify_directory_descriptor(descriptor, logical_path, label)
        relative = absolute_path.relative_to(selected_root)
        for component in relative.parts:
            next_path = logical_path / component
            try:
                next_descriptor = os.open(
                    component, _directory_open_flags(), dir_fd=descriptor
                )
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise ValidationError(f"{label} is a symlink: {next_path}") from exc
                raise ValidationError(
                    f"cannot open {label}: {next_path}: {exc}"
                ) from exc
            try:
                os.close(descriptor)
            except OSError as exc:
                try:
                    os.close(next_descriptor)
                except OSError:
                    pass
                raise ValidationError(
                    f"cannot close {label}: {logical_path}: {exc}"
                ) from exc
            descriptor = next_descriptor
            logical_path = next_path
            _verify_directory_descriptor(descriptor, logical_path, label)
        return descriptor
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def read_stable_file(
    path: Path, label: str, capture_bytes: bool = False
) -> tuple[int, str, Optional[bytes]]:
    """Read one stable regular file without following its final symlink."""
    _require_selected_directory_chain(path.parent, f"{label} parent")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    else:
        try:
            if stat.S_ISLNK(path.lstat().st_mode):
                raise ValidationError(f"{label} is a symlink: {path}")
        except OSError as exc:
            raise ValidationError(f"cannot inspect {label}: {path}: {exc}") from exc
    parent_descriptor = None
    descriptor = None
    try:
        if _ACTIVE_DIRECTORY_DESCRIPTORS is not None:
            parent_descriptor = _open_anchored_directory(path.parent, f"{label} parent")
            descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
        else:
            descriptor = os.open(os.fspath(path), flags)
    except OSError as exc:
        if parent_descriptor is not None:
            try:
                os.close(parent_descriptor)
            except OSError:
                pass
            parent_descriptor = None
        if exc.errno == errno.ELOOP:
            raise ValidationError(f"{label} is a symlink: {path}") from exc
        raise ValidationError(f"cannot open {label}: {path}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValidationError(f"{label} is not a regular file: {path}")
        digest = hashlib.sha256()
        bytes_read = 0
        chunks: Optional[list[bytes]] = [] if capture_bytes else None
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            bytes_read += len(chunk)
            digest.update(chunk)
            if chunks is not None:
                chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
            or bytes_read != before.st_size
        ):
            raise ValidationError(f"{label} changed while being fingerprinted: {path}")
        try:
            if parent_descriptor is None:
                pathname = path.lstat()
            else:
                pathname = os.stat(
                    path.name, dir_fd=parent_descriptor, follow_symlinks=False
                )
        except OSError as exc:
            raise ValidationError(f"cannot inspect {label}: {path}: {exc}") from exc
        if (
            stat.S_ISLNK(pathname.st_mode)
            or not stat.S_ISREG(pathname.st_mode)
            or before.st_dev != pathname.st_dev
            or before.st_ino != pathname.st_ino
        ):
            raise ValidationError(f"{label} changed while being fingerprinted: {path}")
    except OSError as exc:
        raise ValidationError(f"cannot fingerprint {label}: {path}: {exc}") from exc
    finally:
        primary_error_active = sys.exc_info()[0] is not None
        close_failure = None
        for close_descriptor, close_label in (
            (descriptor, label),
            (parent_descriptor, f"{label} parent"),
        ):
            if close_descriptor is None:
                continue
            try:
                os.close(close_descriptor)
            except OSError as exc:
                if close_failure is None:
                    close_failure = ValidationError(
                        f"cannot close {close_label}: {path}: {exc}"
                    )
                    close_failure.__cause__ = exc
        if close_failure is not None and not primary_error_active:
            raise close_failure
    content = b"".join(chunks) if chunks is not None else None
    return before.st_size, digest.hexdigest(), content


def fingerprint_file(path: Path, label: str = "validated file") -> tuple[int, str]:
    """Return a stable size/hash pair from one descriptor."""
    size, digest, _ = read_stable_file(path, label)
    return size, digest


def is_lowercase_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def sha256(path: Path) -> str:
    """Compatibility wrapper for callers outside this validator."""
    return fingerprint_file(path)[1]


def lstat_or_validation_error(path: Path, label: str) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise ValidationError(f"cannot inspect {label}: {path}: {exc}") from exc


def _remember_directory_identity(
    path: Path, label: str, result: Optional[os.stat_result] = None
) -> os.stat_result:
    """Record or verify one selected directory's device/inode identity."""
    current = lstat_or_validation_error(path, label) if result is None else result
    if stat.S_ISLNK(current.st_mode):
        raise ValidationError(f"{label} is a symlink: {path}")
    if not stat.S_ISDIR(current.st_mode):
        raise ValidationError(f"{label} is not a directory: {path}")
    if _ACTIVE_DIRECTORY_IDENTITIES is not None:
        absolute_path = path.absolute()
        identity = (current.st_dev, current.st_ino)
        selected = _ACTIVE_DIRECTORY_IDENTITIES.get(absolute_path)
        if selected is None:
            _ACTIVE_DIRECTORY_IDENTITIES[absolute_path] = identity
        elif selected != identity:
            raise ValidationError(f"{label} changed identity: {path}")
    return current


def _require_selected_directory_chain(path: Path, label: str) -> None:
    """Verify selected ancestors without resolving or following replacements."""
    if _ACTIVE_DIRECTORY_IDENTITIES is None:
        return
    absolute_path = path.absolute()
    for candidate in reversed((absolute_path,) + tuple(absolute_path.parents)):
        if candidate not in _ACTIVE_DIRECTORY_IDENTITIES:
            continue
        _remember_directory_identity(candidate, label)


def _select_directory_chain(path: Path, label: str) -> None:
    """Select real directories from the filesystem root to path."""
    absolute_path = path.absolute()
    for candidate in reversed((absolute_path,) + tuple(absolute_path.parents)):
        current = lstat_or_validation_error(candidate, label)
        # macOS exposes /var and other stable system ancestors as symlinks. They
        # are outside the caller-selected tree; retain identities only once the
        # path enters real directories beneath them.
        if stat.S_ISLNK(current.st_mode):
            if candidate == absolute_path:
                raise ValidationError(f"{label} is a symlink: {candidate}")
            continue
        _remember_directory_identity(candidate, label, current)


def require_directory_not_symlink(path: Path, label: str) -> None:
    _remember_directory_identity(path, label)


def require_real_directory_chain(
    trusted_root: Path,
    relative_directory: PurePosixPath,
    label: str,
    selected_label: str = "root",
) -> Path:
    """Validate and retain each directory identity beneath a trusted root."""
    require_directory_not_symlink(trusted_root, f"{label} trusted")
    current = trusted_root
    components = relative_directory.parts
    for index, component in enumerate(components):
        current = current / component
        component_label = (
            selected_label if selected_label != "root" else
            ("root" if index == len(components) - 1 else "ancestor")
        )
        _remember_directory_identity(current, f"{label} {component_label}")
    return current


def require_real_file_parent_chain(
    trusted_root: Path, relative_file: PurePosixPath, label: str
) -> None:
    require_real_directory_chain(
        trusted_root,
        relative_file.parent,
        label,
        selected_label="parent",
    )


def normalized_relative(value: str, label: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not value or path == PurePosixPath("."):
        raise ValidationError(f"{label} must be a non-empty relative path: {value!r}")
    return path


def safe_relative(value: str, label: str) -> PurePosixPath:
    path = normalized_relative(value, label)
    if ".." in path.parts:
        raise ValidationError(f"{label} must not contain '..': {value!r}")
    return path


def canonical_safe_relative(value: str, label: str) -> PurePosixPath:
    path = safe_relative(value, label)
    if value != path.as_posix():
        raise ValidationError(f"{label} must be canonical: {value!r}")
    return path


def historic_path_mapping(source_path: str, destination_path: str) -> None:
    """Require one exact approved source-root to destination-root mapping."""
    if source_path.startswith(("collector:", "observed:")):
        destination = canonical_safe_relative(destination_path, "destination_path")
        if not any(destination.is_relative_to(root) and destination != PurePosixPath(root)
                   for root in CANONICAL_ROOTS):
            raise ValidationError("current provenance destination is outside canonical roots")
        producers = {
            "news": ("ge16-general-news-log.md", "ge16-general-news-tracked.json",
                     "ge16-news-candidates.json", "ge16-news-judged.json",
                     "ge16-news-accepted.json", "ge16-news-feed.json"),
            "polls": ("ge16-poll-tracker-log.md", "ge16-polls-tracked.json"),
            "candidates": ("ge16-candidate-tracker-log.md", "ge16-candidates-tracked.json"),
        }
        expected = f"observed:scripts/refresh_canonical_data.py#{destination_path}"
        for producer, names in producers.items():
            if destination_path in {"research/trackers/" + name for name in names}:
                expected = f"collector:scripts/collect/track_ge16_{producer}.py#{destination_path}"
        if source_path != expected:
            raise ValidationError("current provenance does not match the canonical producer")
        return
    source = normalized_relative(source_path, "historic_source_path")
    if source_path != source.as_posix() or source.parts[:2] != ("..", "HERMES"):
        raise ValidationError(f"historic_source_path must be canonical: {source_path!r}")
    if ".." in source.parts[1:]:
        raise ValidationError(
            f"historic_source_path must preserve only leading '../HERMES': {source_path!r}"
        )
    destination = canonical_safe_relative(destination_path, "destination_path")
    for source_root_string, destination_root_string in HISTORIC_ROOTS:
        source_root = PurePosixPath(source_root_string)
        try:
            relative = source.relative_to(source_root)
        except ValueError:
            continue
        if relative == PurePosixPath("."):
            break
        expected_destination = PurePosixPath(destination_root_string) / relative
        if destination != expected_destination:
            raise ValidationError(
                "historic source/destination path does not match the approved root mapping"
            )
        return
    raise ValidationError(
        f"historic_source_path is outside the approved historic roots: {source_path!r}"
    )


def files_under(root: Path, label: str) -> dict[str, Path]:
    require_directory_not_symlink(root, label)
    result: dict[str, Path] = {}
    def walk_error(exc: OSError) -> None:
        path = Path(exc.filename) if exc.filename else root
        raise ValidationError(f"cannot scan {label}: {path}: {exc}") from exc

    try:
        for current, directory_names, file_names in os.walk(
            root, topdown=True, followlinks=False, onerror=walk_error
        ):
            directory_names.sort(key=os.fsencode)
            file_names.sort(key=os.fsencode)
            current_path = Path(current)
            _require_selected_directory_chain(current_path, label)
            _remember_directory_identity(current_path, label)
            for directory_name in directory_names:
                candidate = current_path / directory_name
                _remember_directory_identity(candidate, label)
            for file_name in file_names:
                candidate = current_path / file_name
                mode = lstat_or_validation_error(candidate, label).st_mode
                if stat.S_ISLNK(mode):
                    raise ValidationError(f"{label} contains a symlinked file: {candidate}")
                if not stat.S_ISREG(mode):
                    raise ValidationError(f"{label} contains a non-regular file: {candidate}")
                result[candidate.relative_to(root).as_posix()] = candidate
    except OSError as exc:
        raise ValidationError(f"cannot scan {label}: {root}: {exc}") from exc
    return result


def manifest_tree_entries(manifest: dict) -> tuple[dict[str, dict], dict[str, dict]]:
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValidationError("manifest files must be a list")
    by_historic_source: dict[str, dict] = {}
    by_destination: dict[str, dict] = {}
    last_source_bytes: Optional[bytes] = None
    for entry in files:
        if not isinstance(entry, dict):
            raise ValidationError("every manifest file entry must be an object")
        required = {"historic_source_path", "destination_path", "bytes", "sha256"}
        import_fields = {"import_bytes", "import_sha256"}
        fields = set(entry)
        if fields & import_fields and fields != (required | import_fields):
            raise ValidationError(f"manifest import provenance fields are incomplete: {entry}")
        if fields != required and fields != (required | import_fields):
            raise ValidationError(f"manifest file entry has unexpected fields: {entry}")
        source_path = entry["historic_source_path"]
        destination_path = entry["destination_path"]
        if not isinstance(source_path, str) or not isinstance(destination_path, str):
            raise ValidationError("manifest paths must be strings")
        historic_path_mapping(source_path, destination_path)
        for bytes_field, hash_field, label in (
            ("bytes", "sha256", "canonical"),
            ("import_bytes", "import_sha256", "import") if import_fields <= fields else (None, None, None),
        ):
            if bytes_field is None:
                continue
            if (
                isinstance(entry[bytes_field], bool)
                or not isinstance(entry[bytes_field], int)
                or entry[bytes_field] < 0
            ):
                raise ValidationError(f"invalid {label} byte size for {source_path}")
            if not is_lowercase_sha256(entry[hash_field]):
                raise ValidationError(f"invalid {label} SHA-256 for {source_path}")
        source_order = os.fsencode(source_path)
        if last_source_bytes is not None and source_order <= last_source_bytes:
            raise ValidationError("manifest files must be in strictly bytewise source-path order")
        last_source_bytes = source_order
        if source_path in by_historic_source or destination_path in by_destination:
            raise ValidationError("manifest contains duplicate source or destination paths")
        by_historic_source[source_path] = entry
        by_destination[destination_path] = entry
    return by_historic_source, by_destination


def validate_format_exceptions(manifest: dict, historic_destinations: set[str]) -> None:
    exceptions = manifest.get("format_exceptions")
    if not isinstance(exceptions, dict) or set(exceptions) != {
        "byte_preservation_required",
        "documentation",
        "entries",
    }:
        raise ValidationError("manifest format_exceptions is invalid")
    if exceptions["byte_preservation_required"] is not True:
        raise ValidationError("format_exceptions byte_preservation_required must be true")
    if exceptions["documentation"] != FORMAT_EXCEPTION_DOCUMENTATION:
        raise ValidationError("format_exceptions documentation is invalid")
    entries = exceptions["entries"]
    if not isinstance(entries, list):
        raise ValidationError("format_exceptions entries must be a list")
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "destination_path",
            "source_preserved",
        }:
            raise ValidationError("format exception entry has unexpected fields")
        destination_path = entry["destination_path"]
        kind = entry["source_preserved"]
        if not isinstance(destination_path, str):
            raise ValidationError("format exception destination_path must be a string")
        canonical_safe_relative(destination_path, "format exception destination_path")
        if not isinstance(kind, str) or kind not in FORMAT_EXCEPTION_KINDS:
            raise ValidationError("format exception source_preserved kind is invalid")
        if destination_path in seen:
            raise ValidationError("format exceptions contain duplicate destinations")
        if destination_path not in historic_destinations:
            raise ValidationError(
                "format exception destination is not a canonical historic destination"
            )
        seen.add(destination_path)


def validate_hash_and_size(
    entry: dict, bytes_field: str, hash_field: str, label: str, path: str
) -> None:
    if (
        isinstance(entry[bytes_field], bool)
        or not isinstance(entry[bytes_field], int)
        or entry[bytes_field] < 0
    ):
        raise ValidationError(f"invalid {label} byte size for {path}")
    if not is_lowercase_sha256(entry[hash_field]):
        raise ValidationError(f"invalid {label} SHA-256 for {path}")


def manifest_methodology_entries(manifest: dict) -> tuple[dict[str, dict], dict[str, dict]]:
    inputs = manifest.get("methodology_inputs")
    if not isinstance(inputs, list):
        raise ValidationError("manifest methodology_inputs must be a list")
    by_legacy_source: dict[str, dict] = {}
    by_canonical_path: dict[str, dict] = {}
    for entry in inputs:
        if not isinstance(entry, dict) or set(entry) != METHODOLOGY_INPUT_FIELDS:
            raise ValidationError("methodology input record has unexpected fields")
        legacy_source = entry["legacy_source_relative_path"]
        canonical_path = entry["canonical_path"]
        if not isinstance(legacy_source, str) or not isinstance(canonical_path, str):
            raise ValidationError("methodology input paths must be strings")
        safe_relative(legacy_source, "legacy_source_relative_path")
        safe_relative(canonical_path, "canonical_path")
        if entry["classification"] != "canonical-methodology-input":
            raise ValidationError("methodology input classification is invalid")
        validate_hash_and_size(
            entry, "bytes", "sha256", "methodology", canonical_path
        )
        roles = entry["consumer_roles"]
        if (
            not isinstance(roles, list)
            or not roles
            or any(not isinstance(role, str) or not role.strip() for role in roles)
        ):
            raise ValidationError("methodology input consumer_roles must be a non-empty string list")
        if legacy_source in by_legacy_source or canonical_path in by_canonical_path:
            raise ValidationError("methodology inputs contain duplicate source or destination paths")
        by_legacy_source[legacy_source] = entry
        by_canonical_path[canonical_path] = entry
    declared_pairs = {
        (legacy_source, entry["canonical_path"])
        for legacy_source, entry in by_legacy_source.items()
    }
    if declared_pairs != set(METHODOLOGY_INPUT_PATH_PAIRS):
        raise ValidationError("methodology declared set does not match the approved input set")
    if [
        (entry["legacy_source_relative_path"], entry["canonical_path"])
        for entry in inputs
    ] != list(METHODOLOGY_INPUT_PATH_PAIRS):
        raise ValidationError("methodology input order does not match the approved input set")
    expected_roles = {
        legacy_source: list(consumer_roles)
        for legacy_source, _, consumer_roles in METHODOLOGY_INPUTS
    }
    if any(
        entry["consumer_roles"] != expected_roles[legacy_source]
        for legacy_source, entry in by_legacy_source.items()
    ):
        raise ValidationError("methodology input consumer roles do not match the approved input set")
    return by_legacy_source, by_canonical_path


def validate_historic_import(manifest: dict) -> tuple[tuple[str, str], ...]:
    historic_import = manifest.get("historic_import")
    if not isinstance(historic_import, dict) or set(historic_import) != {
        "source_repository",
        "source_commit",
        "roots",
    }:
        raise ValidationError("manifest historic_import is invalid")
    if historic_import["source_repository"] != "HERMES":
        raise ValidationError("manifest historic source repository is invalid")
    if historic_import["source_commit"] != SNAPSHOT_COMMIT:
        raise ValidationError("manifest historic source commit is invalid")
    roots = historic_import["roots"]
    if not isinstance(roots, list):
        raise ValidationError("manifest historic import roots must be a list")
    root_pairs: list[tuple[str, str]] = []
    for root in roots:
        if not isinstance(root, dict) or set(root) != {"source_root", "destination_root"}:
            raise ValidationError("each historic import root must have source_root and destination_root")
        source_root, destination_root = root["source_root"], root["destination_root"]
        if not isinstance(source_root, str) or not isinstance(destination_root, str):
            raise ValidationError("historic import root paths must be strings")
        normalized_relative(source_root, "source_root")
        normalized_relative(destination_root, "destination_root")
        root_pairs.append((source_root, destination_root))
    if tuple(root_pairs) != HISTORIC_ROOTS:
        raise ValidationError("manifest historic import roots do not match the approved six-root mapping")
    return tuple(root_pairs)


def _validate_selected(manifest_path: Path, import_audit_root: Optional[Path] = None) -> tuple[int, int, int, int]:
    try:
        _, _, manifest_bytes = read_stable_file(
            manifest_path, "manifest", capture_bytes=True
        )
        if manifest_bytes is None:  # Defensive; capture_bytes=True always returns bytes.
            raise ValidationError("cannot read manifest bytes")
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read manifest: {exc}") from exc
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_FIELDS:
        raise ValidationError("manifest has unexpected top-level fields")
    if manifest.get("schema") != "data.canonical-provenance.v2":
        raise ValidationError("unsupported provenance manifest schema")
    if manifest.get("snapshot_commit") != SNAPSHOT_COMMIT:
        raise ValidationError("manifest snapshot commit is invalid")
    historic_roots = validate_historic_import(manifest)
    roots = manifest.get("roots")
    if not isinstance(roots, list) or not roots:
        raise ValidationError("manifest roots must be a non-empty list")
    destination_roots: list[str] = []
    for root in roots:
        if not isinstance(root, str):
            raise ValidationError("destination root paths must be strings")
        normalized_relative(root, "destination_root")
        destination_roots.append(root)
    if len(set(destination_roots)) != len(destination_roots):
        raise ValidationError("manifest contains duplicate destination roots")
    if manifest.get("root_set") != "hermes-phase-1.1-canonical-data":
        raise ValidationError("manifest is not the approved HERMES canonical-data root set")
    if tuple(destination_roots) != CANONICAL_ROOTS:
        raise ValidationError("manifest canonical destination roots do not match the approved six-root mapping")

    base = manifest_path.parent.absolute()
    by_historic_source, by_destination = manifest_tree_entries(manifest)
    validate_format_exceptions(manifest, set(by_destination))
    by_methodology_source, by_methodology_destination = manifest_methodology_entries(manifest)
    destination_files: dict[str, Path] = {}
    for destination_root_string in destination_roots:
        destination_root = require_real_directory_chain(
            base, PurePosixPath(destination_root_string), "destination tree"
        )
        for relative_path, file_path in files_under(destination_root, "destination tree").items():
            destination_files[f"{destination_root_string}/{relative_path}"] = file_path
    # Canonical inputs are selected by P2.1 disposition (canonical-input-candidate),
    # never by filename: this check compares the manifest against the FULL destination
    # tree with no name-based exclusion (the P1.7 R1 exclusion was reverted). Working-state
    # vs durable-evidence content classification is recorded in
    # evidence/P2/P2.1-artifact-classification.json and applied by the P2.2 importer.
    if set(by_destination) != set(destination_files):
        raise ValidationError("destination tree differs from manifest")

    methodology_destination_files: dict[str, Path] = {}
    for destination_root_string in METHODOLOGY_DESTINATION_ROOTS:
        destination_root = require_real_directory_chain(
            base, PurePosixPath(destination_root_string), "methodology destination tree"
        )
        for relative_path, file_path in files_under(
            destination_root, "methodology destination tree"
        ).items():
            methodology_destination_files[
                f"{destination_root_string}/{relative_path}"
            ] = file_path
    if set(by_methodology_destination) != set(methodology_destination_files):
        raise ValidationError("methodology destination tree differs from manifest")
    for destination_path, methodology_entry in by_methodology_destination.items():
        historic_entry = by_destination.get(destination_path)
        if historic_entry is not None and (
            historic_entry["bytes"] != methodology_entry["bytes"]
            or historic_entry["sha256"] != methodology_entry["sha256"]
        ):
            raise ValidationError("methodology destination collides with a historic file")

    source_files: dict[str, Path] = {}
    methodology_source_files: dict[str, Path] = {}
    audit_root: Optional[Path] = None
    if import_audit_root is not None:
        audit_root = import_audit_root.absolute()
        for source_root_string, _ in historic_roots:
            source_relative_root = PurePosixPath(source_root_string).relative_to("../HERMES")
            source_root = require_real_directory_chain(
                audit_root, source_relative_root, "import audit source tree"
            )
            for relative_path, file_path in files_under(source_root, "import audit source tree").items():
                source_files[f"{source_root_string}/{relative_path}"] = file_path
        if set(by_historic_source) != set(source_files):
            raise ValidationError("import audit source tree differs from manifest")
        for source_path in by_methodology_source:
            require_real_directory_chain(
                audit_root,
                PurePosixPath(source_path).parent,
                "methodology import audit source",
                selected_label="parent",
            )
            source_file = audit_root / source_path
            methodology_source_files[source_path] = source_file

    total_bytes = 0
    for source_path in sorted(by_historic_source, key=os.fsencode):
        entry = by_historic_source[source_path]
        destination_path = entry["destination_path"]
        destination_file = destination_files[destination_path]
        destination_relative = PurePosixPath(destination_path)
        require_real_file_parent_chain(base, destination_relative, "destination file")
        destination_size, destination_hash = fingerprint_file(
            destination_file, "destination file"
        )
        require_real_file_parent_chain(base, destination_relative, "destination file")
        if destination_size != entry["bytes"]:
            raise ValidationError(f"byte-size mismatch: {source_path}")
        if destination_hash != entry["sha256"]:
            raise ValidationError(f"hash mismatch: {source_path}")
        if audit_root is not None:
            source_file = source_files[source_path]
            import_bytes = entry.get("import_bytes", entry["bytes"])
            import_sha256 = entry.get("import_sha256", entry["sha256"])
            source_relative = PurePosixPath(source_path).relative_to("../HERMES")
            require_real_file_parent_chain(
                audit_root, source_relative, "import audit source"
            )
            source_size, source_hash = fingerprint_file(source_file, "import audit source")
            require_real_file_parent_chain(
                audit_root, source_relative, "import audit source"
            )
            if source_size != import_bytes:
                raise ValidationError(f"import audit byte-size mismatch: {source_path}")
            if source_hash != import_sha256:
                raise ValidationError(f"import audit hash mismatch: {source_path}")
        total_bytes += destination_size

    methodology_total_bytes = 0
    for source_path in sorted(by_methodology_source, key=os.fsencode):
        entry = by_methodology_source[source_path]
        destination_path = entry["canonical_path"]
        destination_file = methodology_destination_files[destination_path]
        destination_relative = PurePosixPath(destination_path)
        require_real_file_parent_chain(base, destination_relative, "destination file")
        destination_size, destination_hash = fingerprint_file(
            destination_file, "destination file"
        )
        require_real_file_parent_chain(base, destination_relative, "destination file")
        if destination_size != entry["bytes"]:
            raise ValidationError(f"methodology byte-size mismatch: {source_path}")
        if destination_hash != entry["sha256"]:
            raise ValidationError(f"methodology hash mismatch: {source_path}")
        if audit_root is not None:
            source_file = methodology_source_files[source_path]
            source_relative = PurePosixPath(source_path)
            require_real_file_parent_chain(
                audit_root, source_relative, "methodology import audit source"
            )
            source_size, source_hash = fingerprint_file(
                source_file, "methodology import audit source"
            )
            require_real_file_parent_chain(
                audit_root, source_relative, "methodology import audit source"
            )
            if source_size != entry["bytes"]:
                raise ValidationError(
                    f"methodology import audit byte-size mismatch: {source_path}"
                )
            if source_hash != entry["sha256"]:
                raise ValidationError(f"methodology import audit hash mismatch: {source_path}")
        methodology_total_bytes += destination_size
    return (
        len(by_historic_source),
        total_bytes,
        len(by_methodology_source),
        methodology_total_bytes,
    )


def validate(
    manifest_path: Path, import_audit_root: Optional[Path] = None
) -> tuple[int, int, int, int]:
    """Validate while retaining the identity of every selected directory."""
    global _ACTIVE_DIRECTORY_IDENTITIES, _ACTIVE_DIRECTORY_DESCRIPTORS
    previous_identities = _ACTIVE_DIRECTORY_IDENTITIES
    previous_descriptors = _ACTIVE_DIRECTORY_DESCRIPTORS
    _ACTIVE_DIRECTORY_IDENTITIES = {}
    _ACTIVE_DIRECTORY_DESCRIPTORS = {}
    primary_error = False
    try:
        _select_directory_chain(manifest_path.parent, "manifest ancestor")
        manifest_root = manifest_path.parent.absolute()
        _ACTIVE_DIRECTORY_DESCRIPTORS[manifest_root] = _open_selected_root(
            manifest_root, "manifest parent"
        )
        if import_audit_root is not None:
            _select_directory_chain(import_audit_root, "import audit ancestor")
            audit_root = import_audit_root.absolute()
            _ACTIVE_DIRECTORY_DESCRIPTORS[audit_root] = _open_selected_root(
                audit_root, "import audit root"
            )
        return _validate_selected(manifest_path, import_audit_root)
    except BaseException:
        primary_error = True
        raise
    finally:
        close_failure = None
        for root_path, descriptor in tuple(_ACTIVE_DIRECTORY_DESCRIPTORS.items()):
            try:
                os.close(descriptor)
            except OSError as exc:
                if close_failure is None:
                    close_failure = ValidationError(
                        f"cannot close selected directory: {root_path}: {exc}"
                    )
                    close_failure.__cause__ = exc
        _ACTIVE_DIRECTORY_IDENTITIES = previous_identities
        _ACTIVE_DIRECTORY_DESCRIPTORS = previous_descriptors
        if close_failure is not None and not primary_error:
            raise close_failure


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "canonical" / "canonical-data-provenance.json",  # V3
        help="path to the repository-local provenance manifest",
    )
    parser.add_argument(
        "--import-audit-root",
        type=Path,
        help="explicit root of a legacy HERMES/renamed source copy to compare against the snapshot",
    )
    args = parser.parse_args()
    try:
        count, total_bytes, methodology_count, methodology_total_bytes = validate(
            args.manifest.absolute(), args.import_audit_root
        )
    except ValidationError as exc:
        print(f"validation failed: {exc}", file=sys.stderr)
        return 1
    print(
        "validation passed: "
        f"files={count} bytes={total_bytes} "
        f"methodology_inputs={methodology_count} methodology_bytes={methodology_total_bytes}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
