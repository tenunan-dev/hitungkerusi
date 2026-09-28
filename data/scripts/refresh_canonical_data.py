#!/usr/bin/env python3
"""Refresh current DATA fingerprints without changing historical import provenance.

The manifest bytes are deterministic. An atomic replacement on every successful
refresh also records validation freshness in its mtime, even with unchanged data.
No output path is caller selectable; all writes use the opened DATA directory.
"""
import hashlib
import json
import os
import stat
import sys
import uuid
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1] / "canonical"  # V3: canonical tree lives at data/canonical
MANIFEST = "canonical-data-provenance.json"
CANONICAL_ROOTS = ("research/raw", "research/derived", "research/trackers",
                   "research/federal", "geo", "research/states")
COLLECTOR_FILES = {
    "news": ("ge16-general-news-log.md", "ge16-general-news-tracked.json",
             "ge16-news-candidates.json", "ge16-news-judged.json",
             "ge16-news-accepted.json", "ge16-news-feed.json"),
    "polls": ("ge16-poll-tracker-log.md", "ge16-polls-tracked.json"),
    "candidates": ("ge16-candidate-tracker-log.md", "ge16-candidates-tracked.json"),
}


def current_source(destination):
    for collector, names in COLLECTOR_FILES.items():
        if destination in {"research/trackers/" + name for name in names}:
            return f"collector:scripts/collect/track_ge16_{collector}.py#{destination}"
    # Other new files have no known collector: record observation, not authorship.
    return f"observed:scripts/refresh_canonical_data.py#{destination}"


def open_directory(path):
    """Traverse using directory descriptors; reject every symlink component."""
    path = Path(path).absolute()
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if part in (".", ".."):
                raise ValueError("unsafe DATA directory")
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_file(root_fd, relative):
    parts = PurePosixPath(relative)
    if parts.is_absolute() or ".." in parts.parts or parts.as_posix() != relative:
        raise ValueError(f"unsafe canonical path: {relative}")
    fd = os.dup(root_fd)
    try:
        for part in parts.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        source = os.open(parts.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(source, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ValueError(f"unsafe canonical file: {relative}")
            raw = handle.read()
            after = os.fstat(handle.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError(f"canonical file changed while reading: {relative}")
            return raw
    finally:
        os.close(fd)


def refresh(root=ROOT):
    # Resolve the caller-supplied root itself: open_directory walks every path
    # component from the anchor with O_NOFOLLOW, and macOS temp roots sit behind
    # the /var -> /private/var symlink (V2 suites only passed because their
    # tempdirs were created under /private/tmp). Components INSIDE the tree keep
    # their O_NOFOLLOW rejection; the root is caller-supplied, so resolving it
    # is not a traversal-weakening.
    root = Path(root).resolve()
    root_fd = open_directory(root)
    previous_cwd = os.open(".", os.O_RDONLY | os.O_DIRECTORY)
    temporary = ".canonical-refresh-" + uuid.uuid4().hex + ".tmp"
    created = False
    try:
        # Python's open audit event omits dir_fd. Align cwd with the selected
        # DATA descriptor so the runner can also classify relative write names.
        os.fchdir(root_fd)
        manifest = json.loads(read_file(root_fd, MANIFEST))
        if tuple(manifest["roots"]) != CANONICAL_ROOTS:
            raise ValueError("unexpected canonical roots")
        previous = {entry["destination_path"]: entry for entry in manifest["files"]}
        if len(previous) != len(manifest["files"]):
            raise ValueError("duplicate canonical destinations")
        files, exceptions = [], {"CRLF": [], "blank-at-EOF": []}
        for relative_root in manifest["roots"]:
            directory = root / relative_root
            # Explicitly check even empty roots before walking.
            selected = open_directory(directory)
            os.close(selected)
            def walk_error(error):
                raise error
            for current, directories, names in os.walk(directory, followlinks=False, onerror=walk_error):
                for name in directories:
                    if (Path(current) / name).is_symlink():
                        raise ValueError("symlinked canonical directory")
                for name in sorted(names, key=os.fsencode):
                    destination = (Path(current) / name).relative_to(root).as_posix()
                    # Canonical inputs are selected by P2.1 disposition (canonical-input-candidate),
                    # never by filename: this walk hashes EVERY file under the approved roots and
                    # excludes nothing (the P1.7 R1 filename exclusion was reverted and stays out).
                    # Working-state vs durable-evidence is a content classification recorded in
                    # evidence/P2/P2.1-artifact-classification.json, applied by the P2.2 importer.
                    raw = read_file(root_fd, destination)
                    entry = dict(previous[destination]) if destination in previous else {
                        "destination_path": destination, "bytes": 0, "sha256": "",
                        "historic_source_path": current_source(destination),
                    }
                    entry.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
                    files.append(entry)
                    if b"\r\n" in raw:
                        exceptions["CRLF"].append(destination)
                    elif len(raw.splitlines()) > 1 and not raw.splitlines()[-1].strip():
                        exceptions["blank-at-EOF"].append(destination)
        manifest["files"] = sorted(files, key=lambda entry: os.fsencode(entry["historic_source_path"]))
        # Fail closed on silent corpus shrink. Under bounded-live the loop is
        # refresh -> validate, so a deletion would otherwise rewrite provenance to
        # match the loss and validate cleanly, erasing the evidence of it.
        # Removing a canonical file must be a deliberate, reviewed act.
        missing = sorted(set(previous) - {entry["destination_path"] for entry in files})
        if missing:
            raise ValueError(
                "canonical corpus shrank: %d file(s) in the manifest are absent from disk; "
                "refusing to rewrite provenance. First: %s" % (len(missing), missing[0])
            )
        manifest["format_exceptions"]["entries"] = [
            {"destination_path": destination, "source_preserved": kind}
            for kind, paths in exceptions.items() for destination in sorted(paths, key=os.fsencode)
        ]
        for entry in manifest["methodology_inputs"]:
            raw = read_file(root_fd, entry["canonical_path"])
            entry.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        output = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o644, dir_fd=root_fd)
        created = True
        with os.fdopen(fd, "wb") as handle:
            handle.write(output)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, MANIFEST, src_dir_fd=root_fd, dst_dir_fd=root_fd)
        created = False
        return len(files)
    finally:
        if created:
            os.unlink(temporary, dir_fd=root_fd)
        os.fchdir(previous_cwd)
        os.close(previous_cwd)
        os.close(root_fd)


if __name__ == "__main__":
    try:
        print(f"canonical refresh passed: files={refresh()}")
    except (OSError, ValueError, KeyError) as error:
        print(f"canonical refresh failed: {error}", file=sys.stderr)
        raise SystemExit(1)
