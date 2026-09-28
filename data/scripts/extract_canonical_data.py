#!/usr/bin/env python3
"""Copy an explicit approved legacy import into a fresh DATA repository."""

import argparse
import errno
import hashlib
import json
import os
import stat
import sys
from pathlib import Path, PurePosixPath

try:
    from methodology_contract import METHODOLOGY_INPUTS
except ImportError:  # Supports importing this script as scripts.extract_canonical_data.
    from scripts.methodology_contract import METHODOLOGY_INPUTS


REPOSITORY_ROOT = Path(__file__).resolve().parents[1] / "canonical"  # V3: canonical tree lives at data/canonical
ROOTS = (
    ("../HERMES/01_RESEARCH/data/raw", "research/raw"),
    ("../HERMES/01_RESEARCH/data/derived", "research/derived"),
    ("../HERMES/01_RESEARCH/data/trackers", "research/trackers"),
    ("../HERMES/01_RESEARCH/federal", "research/federal"),
    ("../HERMES/01_RESEARCH/geo", "geo"),
    ("../HERMES/01_RESEARCH/states", "research/states"),
)
SNAPSHOT_COMMIT = "6ce692d29bc3831a4cf72dba996f6ee0a3f2a98c"


_CREATED_PATH_CALLBACK = None
_CREATED_FILE_DESCRIPTOR_CALLBACK = None
_ACTIVE_DIRECTORY_IDENTITIES = None
_ACTIVE_DIRECTORY_DESCRIPTORS = None


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _verify_directory_descriptor(descriptor: int, path: Path, label: str) -> os.stat_result:
    try:
        result = os.fstat(descriptor)
    except OSError as exc:
        raise RuntimeError(f"cannot inspect {label}: {path}: {exc}") from exc
    if not stat.S_ISDIR(result.st_mode):
        raise RuntimeError(f"{label} is not a directory: {path}")
    if _ACTIVE_DIRECTORY_IDENTITIES is not None:
        identity = (result.st_dev, result.st_ino)
        selected = _ACTIVE_DIRECTORY_IDENTITIES.get(path.absolute())
        if selected is None:
            _ACTIVE_DIRECTORY_IDENTITIES[path.absolute()] = identity
        elif selected != identity:
            raise RuntimeError(f"{label} changed identity: {path}")
    return result


def _open_selected_root(path: Path, label: str) -> int:
    try:
        descriptor = os.open(os.fspath(path), _directory_open_flags())
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise RuntimeError(f"{label} is a symlink: {path}") from exc
        raise RuntimeError(f"cannot open {label}: {path}: {exc}") from exc
    try:
        _verify_directory_descriptor(descriptor, path, label)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_anchored_directory(path: Path, label: str) -> int:
    """Open a directory relative to the longest retained trusted descriptor."""
    if not _ACTIVE_DIRECTORY_DESCRIPTORS:
        raise RuntimeError(f"no selected directory anchor for {label}: {path}")
    absolute_path = path.absolute()
    candidates = []
    for root_path in _ACTIVE_DIRECTORY_DESCRIPTORS:
        try:
            absolute_path.relative_to(root_path)
        except ValueError:
            continue
        candidates.append(root_path)
    if not candidates:
        raise RuntimeError(f"path is outside selected directory anchors: {path}")
    selected_root = max(candidates, key=lambda candidate: len(candidate.parts))
    descriptor = os.dup(_ACTIVE_DIRECTORY_DESCRIPTORS[selected_root])
    logical_path = selected_root
    try:
        _verify_directory_descriptor(descriptor, logical_path, label)
        for component in absolute_path.relative_to(selected_root).parts:
            next_path = logical_path / component
            try:
                next_descriptor = os.open(
                    component, _directory_open_flags(), dir_fd=descriptor
                )
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise RuntimeError(f"{label} is a symlink: {next_path}") from exc
                raise RuntimeError(f"cannot open {label}: {next_path}: {exc}") from exc
            try:
                os.close(descriptor)
            except OSError as exc:
                try:
                    os.close(next_descriptor)
                except OSError:
                    pass
                raise RuntimeError(
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


def _record_created_path(kind: str, path: Path, file_stat=None) -> None:
    if _CREATED_PATH_CALLBACK is not None:
        device = file_stat.st_dev if file_stat is not None else None
        inode = file_stat.st_ino if file_stat is not None else None
        _CREATED_PATH_CALLBACK(kind, path, device, inode)


def _retain_created_file_descriptor(path: Path, descriptor: int) -> None:
    if _CREATED_FILE_DESCRIPTOR_CALLBACK is not None:
        try:
            retained = os.dup(descriptor)
        except OSError as exc:
            raise RuntimeError(
                f"cannot retain created file descriptor: {path}: {exc}"
            ) from exc
        try:
            _CREATED_FILE_DESCRIPTOR_CALLBACK(path, retained)
        except BaseException:
            try:
                os.close(retained)
            except OSError:
                pass
            raise


def _remove_retained_directory(path: Path, device: int, inode: int) -> None:
    """Remove an owned empty directory even if its pathname was replaced."""
    if not _ACTIVE_DIRECTORY_DESCRIPTORS:
        return
    retained = _ACTIVE_DIRECTORY_DESCRIPTORS.get(path.absolute())
    if retained is None:
        return
    parent_descriptor = None
    try:
        parent_descriptor = os.open("..", _directory_open_flags(), dir_fd=retained)
        for name in os.listdir(parent_descriptor):
            try:
                result = os.stat(
                    name, dir_fd=parent_descriptor, follow_symlinks=False
                )
            except OSError:
                continue
            if (result.st_dev, result.st_ino) == (device, inode):
                os.rmdir(name, dir_fd=parent_descriptor)
                return
    except OSError:
        return
    finally:
        if parent_descriptor is not None:
            try:
                os.close(parent_descriptor)
            except OSError:
                pass


def _remove_retained_file(path: Path, device: int, inode: int) -> None:
    """Remove an owned file from its retained original parent by identity."""
    if not _ACTIVE_DIRECTORY_DESCRIPTORS:
        return
    try:
        parent_descriptor = _open_anchored_directory(
            path.parent, "rollback destination parent"
        )
    except (OSError, RuntimeError):
        return
    try:
        for name in os.listdir(parent_descriptor):
            try:
                result = os.stat(
                    name, dir_fd=parent_descriptor, follow_symlinks=False
                )
            except OSError:
                continue
            if (result.st_dev, result.st_ino) == (device, inode):
                try:
                    os.unlink(name, dir_fd=parent_descriptor)
                except OSError:
                    pass
                return
    finally:
        try:
            os.close(parent_descriptor)
        except OSError:
            pass


def _rollback_created_paths(created_paths, retained_file_descriptors=None) -> None:
    """Remove only paths that still have the identities created by this run."""
    for kind, path, device, inode in reversed(created_paths):
        if kind == "file" and device is None and retained_file_descriptors:
            retained = retained_file_descriptors.get(path)
            if retained is not None:
                try:
                    retained_stat = os.fstat(retained)
                except OSError:
                    pass
                else:
                    device, inode = retained_stat.st_dev, retained_stat.st_ino
        # A creation recorded before its descriptor can be fingerprinted has no
        # pathname ownership proof.  It may have been replaced before fstat
        # failed, so preserving it is the only safe rollback action.
        if device is None:
            continue
        parent_descriptor = None
        try:
            if _ACTIVE_DIRECTORY_DESCRIPTORS is None:
                if not _directory_chain_matches(path.parent):
                    continue
                current = path.lstat()
            else:
                parent_descriptor = _open_anchored_directory(
                    path.parent, "rollback destination parent"
                )
                current = os.stat(
                    path.name, dir_fd=parent_descriptor, follow_symlinks=False
                )
        except FileNotFoundError:
            if parent_descriptor is not None:
                try:
                    os.close(parent_descriptor)
                except OSError:
                    pass
            if device is not None:
                if kind == "file":
                    _remove_retained_file(path, device, inode)
                else:
                    _remove_retained_directory(path, device, inode)
            continue
        except (OSError, RuntimeError):
            if parent_descriptor is not None:
                try:
                    os.close(parent_descriptor)
                except OSError:
                    pass
            continue
        if device is not None and (
            current.st_dev != device or current.st_ino != inode
        ):
            if parent_descriptor is not None:
                try:
                    os.close(parent_descriptor)
                except OSError:
                    pass
            if kind == "file":
                _remove_retained_file(path, device, inode)
            else:
                _remove_retained_directory(path, device, inode)
            continue
        try:
            if parent_descriptor is None:
                if kind == "file":
                    path.unlink()
                else:
                    path.rmdir()
            elif kind == "file":
                os.unlink(path.name, dir_fd=parent_descriptor)
            else:
                os.rmdir(path.name, dir_fd=parent_descriptor)
        except OSError:
            # Preserve the primary extraction failure and any path now in use.
            pass
        finally:
            if parent_descriptor is not None:
                try:
                    os.close(parent_descriptor)
                except OSError:
                    pass


def _write_exclusive_file(path: Path, content: bytes, label: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    parent_descriptor = None
    try:
        _require_selected_directory_chain(path.parent, "destination ancestor")
        try:
            if _ACTIVE_DIRECTORY_DESCRIPTORS is None:
                descriptor = os.open(os.fspath(path), flags, 0o666)
            else:
                parent_descriptor = _open_anchored_directory(
                    path.parent, "destination ancestor"
                )
                descriptor = os.open(
                    path.name, flags, 0o666, dir_fd=parent_descriptor
                )
        except OSError as exc:
            raise RuntimeError(
                f"{label} already exists or cannot be created: {path}: {exc}"
            ) from exc
        _record_created_path("file", path)
        _retain_created_file_descriptor(path, descriptor)
        created = os.fstat(descriptor)
        _record_created_path("file", path, created)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise RuntimeError(f"cannot write {label}: {path}")
            view = view[written:]
        _require_selected_directory_chain(path.parent, "destination ancestor")
    except OSError as exc:
        raise RuntimeError(f"cannot write {label}: {path}: {exc}") from exc
    finally:
        active_failure = sys.exc_info()[0] is not None
        close_failure = None
        for close_descriptor, close_label, close_path in (
            (descriptor, label, path),
            (parent_descriptor, f"{label} parent", path.parent),
        ):
            if close_descriptor is None:
                continue
            try:
                os.close(close_descriptor)
            except OSError as exc:
                if close_failure is None:
                    close_failure = RuntimeError(
                        f"cannot close {close_label}: {close_path}: {exc}"
                    )
                    close_failure.__cause__ = exc
        if close_failure is not None and not active_failure:
            raise close_failure


def _lstat(path: Path, label: str) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise RuntimeError(f"cannot inspect {label}: {path}: {exc}") from exc


def _remember_directory_identity(path: Path, label: str, result=None) -> os.stat_result:
    """Record or verify the device/inode selected for one directory path."""
    current = _lstat(path, label) if result is None else result
    if stat.S_ISLNK(current.st_mode):
        raise RuntimeError(f"{label} is a symlink: {path}")
    if not stat.S_ISDIR(current.st_mode):
        raise RuntimeError(f"{label} is not a directory: {path}")
    if _ACTIVE_DIRECTORY_IDENTITIES is not None:
        absolute_path = path.absolute()
        identity = (current.st_dev, current.st_ino)
        selected = _ACTIVE_DIRECTORY_IDENTITIES.get(absolute_path)
        if selected is None:
            _ACTIVE_DIRECTORY_IDENTITIES[absolute_path] = identity
        elif selected != identity:
            raise RuntimeError(f"{label} changed identity: {path}")
    return current


def _require_selected_directory_chain(path: Path, label: str) -> None:
    """Fail if an already-selected ancestor was replaced or redirected."""
    if _ACTIVE_DIRECTORY_IDENTITIES is None:
        return
    absolute_path = path.absolute()
    for candidate in reversed((absolute_path,) + tuple(absolute_path.parents)):
        if candidate not in _ACTIVE_DIRECTORY_IDENTITIES:
            continue
        _remember_directory_identity(candidate, label)


def _directory_chain_matches(path: Path) -> bool:
    """Return false without following a parent chain changed during extraction."""
    if _ACTIVE_DIRECTORY_IDENTITIES is None:
        return True
    try:
        _require_selected_directory_chain(path, "destination ancestor")
    except RuntimeError:
        return False
    return True


def _require_directory(path: Path, label: str) -> None:
    _remember_directory_identity(path, label)


def _require_source_directory_chain(import_root: Path, relative_path: PurePosixPath) -> Path:
    _require_directory(import_root, "import root")
    current = import_root
    for index, component in enumerate(relative_path.parts):
        current = current / component
        mode = _remember_directory_identity(current, "source ancestor").st_mode
        if stat.S_ISLNK(mode):
            kind = "source root" if index == len(relative_path.parts) - 1 else "source ancestor"
            raise RuntimeError(f"{kind} is a symlink: {current}")
        if not stat.S_ISDIR(mode):
            kind = "source root" if index == len(relative_path.parts) - 1 else "source ancestor"
            raise RuntimeError(f"{kind} is not a directory: {current}")
    return current


def _source_files(root: Path) -> list[Path]:
    files: list[Path] = []

    def fail_on_walk_error(exc: OSError) -> None:
        error_path = exc.filename if exc.filename is not None else os.fspath(root)
        raise RuntimeError(f"cannot scan source path: {error_path}: {exc}") from exc

    try:
        _require_selected_directory_chain(root, "source root")
        for current, directory_names, file_names in os.walk(
            root,
            topdown=True,
            onerror=fail_on_walk_error,
            followlinks=False,
        ):
            directory_names.sort(key=os.fsencode)
            file_names.sort(key=os.fsencode)
            current_path = Path(current)
            _require_selected_directory_chain(current_path, "source directory")
            _remember_directory_identity(current_path, "source directory")
            for directory_name in directory_names:
                candidate = current_path / directory_name
                _remember_directory_identity(candidate, "source directory")
            for file_name in file_names:
                candidate = current_path / file_name
                mode = _lstat(candidate, "source file").st_mode
                if stat.S_ISLNK(mode):
                    raise RuntimeError(f"source contains a symlinked file: {candidate}")
                if not stat.S_ISREG(mode):
                    raise RuntimeError(f"source contains a non-regular file: {candidate}")
                files.append(candidate)
        _require_selected_directory_chain(root, "source root")
    except OSError as exc:
        raise RuntimeError(f"cannot scan source root: {root}: {exc}") from exc
    return files


def _check_regular_source(path: Path, label: str) -> None:
    mode = _lstat(path, label).st_mode
    if stat.S_ISLNK(mode):
        raise RuntimeError(f"{label} is a symlink: {path}")
    if not stat.S_ISREG(mode):
        raise RuntimeError(f"{label} is not a regular file: {path}")


def _open_source(path: Path, label: str) -> int:
    chain_label = (
        "destination ancestor" if label == "copied destination file" else "source ancestor"
    )
    _require_selected_directory_chain(path.parent, chain_label)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    else:
        _check_regular_source(path, label)
    if _ACTIVE_DIRECTORY_DESCRIPTORS is None:
        try:
            return os.open(os.fspath(path), flags)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise RuntimeError(f"{label} is a symlink: {path}") from exc
            raise RuntimeError(f"cannot open {label}: {path}: {exc}") from exc

    parent_descriptor = _open_anchored_directory(path.parent, chain_label)
    source_descriptor = None
    try:
        source_descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
    except OSError as exc:
        try:
            os.close(parent_descriptor)
        except OSError:
            pass
        if exc.errno == errno.ELOOP:
            raise RuntimeError(f"{label} is a symlink: {path}") from exc
        raise RuntimeError(f"cannot open {label}: {path}: {exc}") from exc
    try:
        os.close(parent_descriptor)
    except OSError as exc:
        try:
            os.close(source_descriptor)
        except OSError:
            pass
        try:
            os.close(parent_descriptor)
        except OSError:
            pass
        raise RuntimeError(
            f"cannot close {chain_label}: {path.parent}: {exc}"
        ) from exc
    return source_descriptor


def _copy_regular_file(source: Path, destination: Path, label: str) -> tuple[int, str]:
    source_descriptor = _open_source(source, label)
    destination_descriptor = None
    destination_parent_descriptor = None
    try:
        before = os.fstat(source_descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise RuntimeError(f"{label} is not a regular file: {source}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        _require_selected_directory_chain(destination.parent, "destination ancestor")
        if _ACTIVE_DIRECTORY_DESCRIPTORS is None:
            destination_descriptor = os.open(os.fspath(destination), flags, 0o666)
        else:
            destination_parent_descriptor = _open_anchored_directory(
                destination.parent, "destination ancestor"
            )
            destination_descriptor = os.open(
                destination.name,
                flags | getattr(os, "O_NOFOLLOW", 0),
                0o666,
                dir_fd=destination_parent_descriptor,
            )
        _record_created_path("file", destination)
        _retain_created_file_descriptor(destination, destination_descriptor)
        destination_stat = os.fstat(destination_descriptor)
        _record_created_path("file", destination, destination_stat)
        digest = hashlib.sha256()
        bytes_written = 0
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            bytes_written += len(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_descriptor, view)
                if written <= 0:
                    raise RuntimeError(f"cannot write destination file: {destination}")
                view = view[written:]
        after = os.fstat(source_descriptor)
        pathname = _lstat(source, label)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
            or bytes_written != before.st_size
            or stat.S_ISLNK(pathname.st_mode)
            or not stat.S_ISREG(pathname.st_mode)
            or before.st_dev != pathname.st_dev
            or before.st_ino != pathname.st_ino
        ):
            raise RuntimeError(f"{label} changed while being copied: {source}")
        _require_selected_directory_chain(destination.parent, "destination ancestor")
        return bytes_written, digest.hexdigest()
    except OSError as exc:
        raise RuntimeError(f"cannot copy {label}: {source}: {exc}") from exc
    finally:
        active_failure = sys.exc_info()[0] is not None
        close_failure = None
        descriptors = (
            (destination_descriptor, "destination file", destination),
            (source_descriptor, label, source),
            (
                destination_parent_descriptor,
                "destination parent",
                destination.parent,
            ),
        )
        for descriptor, close_label, close_path in descriptors:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError as exc:
                    if close_failure is None:
                        close_failure = RuntimeError(
                            f"cannot close {close_label}: {close_path}: {exc}"
                        )
                        close_failure.__cause__ = exc
        if close_failure is not None and not active_failure:
            raise close_failure


def _uses_crlf(path: Path) -> bool:
    descriptor = _open_source(path, "copied destination file")
    try:
        try:
            crlf_count = 0
            newline_count = 0
            previous_was_cr = False
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                newline_count += chunk.count(b"\n")
                crlf_count += chunk.count(b"\r\n")
                if previous_was_cr and chunk.startswith(b"\n"):
                    crlf_count += 1
                previous_was_cr = chunk.endswith(b"\r")
            result = bool(crlf_count) and crlf_count == newline_count
        except OSError as exc:
            raise RuntimeError(
                f"cannot inspect copied destination file: {path}: {exc}"
            ) from exc
    finally:
        active_failure = sys.exc_info()[0] is not None
        try:
            os.close(descriptor)
        except OSError as exc:
            if not active_failure:
                raise RuntimeError(
                    f"cannot close copied destination file: {path}: {exc}"
                ) from exc
    return result


def format_exceptions(crlf_destinations: set[str]) -> dict[str, object]:
    return {
        "byte_preservation_required": True,
        "documentation": (
            "Source-preserved whitespace is intentional; canonical source and destination "
            "bytes must remain byte-identical."
        ),
        "entries": [
            {
                "destination_path": destination_path,
                "source_preserved": "CRLF",
            }
            for destination_path in sorted(crlf_destinations, key=os.fsencode)
        ]
        + [
            {
                "destination_path": "research/derived/ge16-per-seat-flip-narratives.md",
                "source_preserved": "blank-at-EOF",
            }
        ],
    }


def _destination_path_is_available(destination_root: Path, relative_path: PurePosixPath) -> bool:
    candidate = destination_root / relative_path
    return not candidate.exists() and not candidate.is_symlink()


def _require_safe_destination_ancestors(
    destination_root: Path, relative_path: PurePosixPath
) -> None:
    _require_directory(destination_root, "destination repository root")
    current = destination_root
    for component in relative_path.parent.parts:
        current = current / component
        try:
            existing = current.lstat()
        except FileNotFoundError:
            return
        _remember_directory_identity(current, "destination ancestor", existing)


def _create_destination_directory(destination_root: Path, relative_path: PurePosixPath) -> Path:
    _require_directory(destination_root, "destination repository root")
    if _ACTIVE_DIRECTORY_DESCRIPTORS is None:
        current = destination_root
        for component in relative_path.parts:
            current = current / component
            try:
                existing = current.lstat()
            except FileNotFoundError:
                current.mkdir()
                created = _lstat(current, "created destination directory")
                _record_created_path("directory", current, created)
                _remember_directory_identity(current, "destination ancestor", created)
                continue
            _remember_directory_identity(current, "destination ancestor", existing)
        return current

    current = destination_root.absolute()
    descriptor = _open_anchored_directory(current, "destination repository root")
    try:
        for component in relative_path.parts:
            next_path = current / component
            created_now = False
            try:
                next_descriptor = os.open(
                    component, _directory_open_flags(), dir_fd=descriptor
                )
            except FileNotFoundError:
                try:
                    os.mkdir(component, dir_fd=descriptor)
                except OSError as exc:
                    raise RuntimeError(
                        f"cannot create destination directory: {next_path}: {exc}"
                    ) from exc
                # Record immediately so rollback can account for the creation,
                # but do not treat the pathname as owned until fstat pins it.
                _record_created_path("directory", next_path)
                created_now = True
                try:
                    next_descriptor = os.open(
                        component, _directory_open_flags(), dir_fd=descriptor
                    )
                except OSError as exc:
                    raise RuntimeError(
                        f"cannot open created destination directory: {next_path}: {exc}"
                    ) from exc
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise RuntimeError(
                        f"destination ancestor is a symlink: {next_path}"
                    ) from exc
                raise RuntimeError(
                    f"cannot open destination ancestor: {next_path}: {exc}"
                ) from exc
            retained_descriptor = None
            try:
                result = _verify_directory_descriptor(
                    next_descriptor, next_path, "destination ancestor"
                )
                if created_now:
                    _record_created_path("directory", next_path, result)
                if next_path not in _ACTIVE_DIRECTORY_DESCRIPTORS:
                    retained_descriptor = os.dup(next_descriptor)
            except BaseException:
                try:
                    os.close(next_descriptor)
                except OSError:
                    pass
                raise
            if retained_descriptor is not None:
                _ACTIVE_DIRECTORY_DESCRIPTORS[next_path] = retained_descriptor
            try:
                os.close(descriptor)
            except OSError as exc:
                try:
                    os.close(next_descriptor)
                except OSError:
                    pass
                raise RuntimeError(
                    f"cannot close destination directory: {current}: {exc}"
                ) from exc
            descriptor = next_descriptor
            current = next_path
        return current
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _extract_canonical_data_selected(destination_repository_root: Path, import_root: Path) -> dict:
    """Copy the approved import into a fresh repository and return its manifest."""
    destination_repository_root = destination_repository_root.absolute()
    import_root = import_root.absolute()
    _require_directory(destination_repository_root, "destination repository root")
    if not _destination_path_is_available(
        destination_repository_root, PurePosixPath("canonical-data-provenance.json")
    ):
        raise RuntimeError("destination manifest already exists; refusing to overwrite")

    historic_plan = []
    for source_string, destination_string in ROOTS:
        source_relative = PurePosixPath(source_string).relative_to("../HERMES")
        source_root = _require_source_directory_chain(import_root, source_relative)
        source_files = _source_files(source_root)
        destination_relative = PurePosixPath(destination_string)
        if not _destination_path_is_available(destination_repository_root, destination_relative):
            raise RuntimeError(
                "destination already exists; refusing to overwrite: "
                + str(destination_repository_root / destination_relative)
            )
        _require_safe_destination_ancestors(
            destination_repository_root, destination_relative
        )
        historic_plan.append((source_string, destination_string, source_root, source_files))

    methodology_plan = []
    for legacy_path, canonical_path, consumer_roles in METHODOLOGY_INPUTS:
        source_relative = PurePosixPath(legacy_path)
        _require_source_directory_chain(import_root, source_relative.parent)
        source_file = import_root / source_relative
        _check_regular_source(source_file, "methodology source")
        destination_relative = PurePosixPath(canonical_path)
        if not _destination_path_is_available(destination_repository_root, destination_relative):
            raise RuntimeError(
                "destination already exists; refusing to overwrite: "
                + str(destination_repository_root / destination_relative)
            )
        _require_safe_destination_ancestors(
            destination_repository_root, destination_relative
        )
        methodology_plan.append((legacy_path, canonical_path, consumer_roles, source_file))

    global _CREATED_PATH_CALLBACK, _CREATED_FILE_DESCRIPTOR_CALLBACK
    created_paths = []
    retained_file_descriptors = {}
    previous_callback = _CREATED_PATH_CALLBACK
    previous_file_descriptor_callback = _CREATED_FILE_DESCRIPTOR_CALLBACK

    def remember_created(kind: str, path: Path, device, inode) -> None:
        if created_paths and created_paths[-1][:2] == (kind, path):
            old_kind, old_path, old_device, old_inode = created_paths[-1]
            if old_device is None and device is not None:
                created_paths[-1] = (old_kind, old_path, device, inode)
                return
        created_paths.append((kind, path, device, inode))

    def remember_file_descriptor(path: Path, descriptor: int) -> None:
        retained_file_descriptors[path] = descriptor

    _CREATED_PATH_CALLBACK = remember_created
    _CREATED_FILE_DESCRIPTOR_CALLBACK = remember_file_descriptor
    primary_error = False
    try:
        manifest_files: list[dict[str, object]] = []
        crlf_destinations: set[str] = set()
        for source_string, destination_string, source_root, source_files in historic_plan:
            destination_root = _create_destination_directory(
                destination_repository_root, PurePosixPath(destination_string)
            )
            for source_file in source_files:
                relative_path = source_file.relative_to(source_root).as_posix()
                destination_file = destination_root / relative_path
                _create_destination_directory(
                    destination_repository_root,
                    PurePosixPath(destination_string) / PurePosixPath(relative_path).parent,
                )
                bytes_copied, digest = _copy_regular_file(
                    source_file, destination_file, "historic source file"
                )
                manifest_files.append(
                    {
                        "historic_source_path": f"{source_string}/{relative_path}",
                        "destination_path": f"{destination_string}/{relative_path}",
                        "bytes": bytes_copied,
                        "sha256": digest,
                    }
                )
                if _uses_crlf(destination_file):
                    crlf_destinations.add(f"{destination_string}/{relative_path}")
        manifest_files.sort(key=lambda entry: os.fsencode(str(entry["historic_source_path"])))
        methodology_inputs = []
        for legacy_path, canonical_path, consumer_roles, source_file in methodology_plan:
            destination_file = destination_repository_root / canonical_path
            _create_destination_directory(
                destination_repository_root, PurePosixPath(canonical_path).parent
            )
            bytes_copied, digest = _copy_regular_file(
                source_file, destination_file, "methodology source"
            )
            methodology_inputs.append(
                {
                    "canonical_path": canonical_path,
                    "legacy_source_relative_path": legacy_path,
                    "sha256": digest,
                    "bytes": bytes_copied,
                    "classification": "canonical-methodology-input",
                    "consumer_roles": list(consumer_roles),
                }
            )
        manifest = {
            "schema": "data.canonical-provenance.v2",
            "root_set": "hermes-phase-1.1-canonical-data",
            "snapshot_commit": SNAPSHOT_COMMIT,
            "format_exceptions": format_exceptions(crlf_destinations),
            "historic_import": {
                "source_repository": "HERMES",
                "source_commit": SNAPSHOT_COMMIT,
                "roots": [
                    {"source_root": source, "destination_root": destination}
                    for source, destination in ROOTS
                ],
            },
            "roots": [destination for _, destination in ROOTS],
            "files": manifest_files,
            "methodology_inputs": methodology_inputs,
        }
        manifest_path = destination_repository_root / "canonical-data-provenance.json"
        manifest_bytes = (
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
        ).encode("utf-8")
        _write_exclusive_file(manifest_path, manifest_bytes, "destination manifest")
        return manifest
    except BaseException:
        primary_error = True
        _rollback_created_paths(created_paths, retained_file_descriptors)
        raise
    finally:
        _CREATED_PATH_CALLBACK = previous_callback
        _CREATED_FILE_DESCRIPTOR_CALLBACK = previous_file_descriptor_callback
        close_failure = None
        for path, descriptor in retained_file_descriptors.items():
            try:
                os.close(descriptor)
            except OSError as exc:
                if close_failure is None:
                    close_failure = RuntimeError(
                        f"cannot close retained created file: {path}: {exc}"
                    )
                    close_failure.__cause__ = exc
        if close_failure is not None and not primary_error:
            raise close_failure


def extract_canonical_data(destination_repository_root: Path, import_root: Path) -> dict:
    """Run one extraction while retaining every selected directory identity."""
    global _ACTIVE_DIRECTORY_IDENTITIES, _ACTIVE_DIRECTORY_DESCRIPTORS
    previous_identities = _ACTIVE_DIRECTORY_IDENTITIES
    previous_descriptors = _ACTIVE_DIRECTORY_DESCRIPTORS
    _ACTIVE_DIRECTORY_IDENTITIES = {}
    _ACTIVE_DIRECTORY_DESCRIPTORS = {}
    primary_error = False
    try:
        destination_root = destination_repository_root.absolute()
        selected_import_root = import_root.absolute()
        _require_directory(destination_root, "destination repository root")
        _ACTIVE_DIRECTORY_DESCRIPTORS[destination_root] = _open_selected_root(
            destination_root, "destination repository root"
        )
        _require_directory(selected_import_root, "import root")
        _ACTIVE_DIRECTORY_DESCRIPTORS[selected_import_root] = _open_selected_root(
            selected_import_root, "import root"
        )
        return _extract_canonical_data_selected(destination_root, selected_import_root)
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
                    close_failure = RuntimeError(
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
        "--import-root",
        type=Path,
        required=True,
        help="explicit root of the legacy HERMES import tree",
    )
    parser.add_argument(
        "--destination-root",
        type=Path,
        default=REPOSITORY_ROOT,
        help="fresh DATA repository root (defaults to this repository)",
    )
    args = parser.parse_args()
    manifest = extract_canonical_data(args.destination_root, args.import_root)
    print(
        "extracted "
        f"files={len(manifest['files'])} "
        f"bytes={sum(entry['bytes'] for entry in manifest['files'])} "
        f"methodology_inputs={len(manifest['methodology_inputs'])}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"extraction failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
