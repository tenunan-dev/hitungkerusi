#!/usr/bin/env python3
"""Refresh current DATA fingerprints without changing historical import provenance.

The manifest bytes are deterministic. An atomic replacement on every successful
refresh also records validation freshness in its mtime, even with unchanged data.
No output path is caller selectable; all writes use the opened DATA directory.

P2.3 run mode (default): this script is also the run orchestrator. ``--run``
creates a run directory under ``data/work/`` (work_paths.new_run), wires the
existing collector staging knobs to it (work_paths.apply_env — zero collector
changes), runs the collectors so every tracker write lands in the run dir,
then PROMOTES the staged tracker outputs into canonical and records a
``ge16.edition.promotion.v1`` manifest whose ``lineage.run_id`` names the run,
before refreshing provenance. ``--no-run`` is exactly the pre-P2.3 behavior:
collectors are not invoked, no run dir exists, only provenance refreshes.
P2.4 adds a REPORT-ONLY integrity gate after promotion (run mode only):
``verify-edition`` on the new manifest plus ``verify-corpus --sampled 500``
(integrity.py); a nonzero verdict is a hard stop naming the edition id — the
verifier never repairs or rolls back, the owner decides.
"""
import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1] / "canonical"  # V3: canonical tree lives at data/canonical
SCRIPTS_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COLLECT_ROOT = SCRIPTS_ROOT / "collect"
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
#: The collectors a staged run invokes, in order. Their tracker writes are
#: routed into the run dir by the env knobs work_paths.apply_env() exports;
#: these modules themselves are never modified for run scoping (P2.3 rule).
DEFAULT_COLLECTORS = ("track_ge16_news.py", "track_ge16_polls.py", "track_ge16_candidates.py")
PROMOTION_SCHEMA = "ge16.edition.promotion.v1"
TRACKERS_RELATIVE = Path("research") / "trackers"

if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))
import work_paths  # noqa: E402  (sibling module; conftest puts data/ on sys.path too)
import integrity  # noqa: E402  (P2.4 read-only verifier; gates run mode below)


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


# ----------------------------------------------------------------- P2.3 run layer

def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stage_collectors(run, commands=None, env=None, cwd=None):
    """Run the collectors as subprocesses under the run's staged environment.

    Their combined stdout/stderr is kept per collector in ``run/logs/`` (the
    live cron flow reads collector stdout; a run dir must be self-describing).
    A nonzero collector exit fails the whole stage BEFORE any promotion — the
    run layer never half-promotes a broken collect, mirroring the collectors'
    own all-or-nothing commit contract.
    """
    if commands is None:
        commands = [[sys.executable, str(COLLECT_ROOT / name)] for name in DEFAULT_COLLECTORS]
    results = []
    for command in commands:
        script = next((part for part in command[1:] if part.endswith(".py")), command[0])
        log_path = run.logs / (Path(script).stem + ".log")
        completed = subprocess.run(
            [str(part) for part in command],
            capture_output=True, text=True,
            env=dict(os.environ if env is None else env),
            cwd=str(cwd or REPOSITORY_ROOT),
        )
        log_path.write_text(
            f"$ {' '.join(str(part) for part in command)}\n"
            f"exit={completed.returncode}\n"
            f"--- stdout ---\n{completed.stdout}"
            f"--- stderr ---\n{completed.stderr}", encoding="utf-8")
        results.append({"command": [str(part) for part in command],
                        "script": Path(script).name,
                        "returncode": completed.returncode,
                        "log": str(log_path)})
        if completed.returncode != 0:
            raise RuntimeError(
                "collector failed (exit %d), promotion refused: %s — log: %s"
                % (completed.returncode, command, log_path))
    return results


def promote(run, canonical_root=ROOT):
    """Copy the run's staged tracker outputs into canonical, deterministically.

    Staged tracker outputs are flat files (the outdir staging layout keeps live
    basenames), so promotion is a per-file copy sorted by name: two runs with
    identical staged bytes promote identical file sets, and promoting the same
    run twice copies nothing the second time (byte-identical targets are
    skipped, so even mtimes do not move). Promotion only ever ADDS or replaces
    whole files; nothing under canonical is ever deleted by it.
    """
    canonical_root = Path(canonical_root)
    destination = canonical_root / TRACKERS_RELATIVE
    destination.mkdir(parents=True, exist_ok=True)
    promoted, unchanged = [], []
    staged_files = sorted((path for path in run.trackers.iterdir() if path.is_file()),
                          key=lambda path: os.fsencode(path.name))
    for staged in staged_files:
        digest = _sha256_file(staged)
        relative = (TRACKERS_RELATIVE / staged.name).as_posix()
        target = destination / staged.name
        if target.is_file() and _sha256_file(target) == digest:
            unchanged.append({"file": relative, "sha256": digest})
            continue
        shutil.copy2(staged, target)
        promoted.append({"file": relative, "sha256": digest,
                         "disposition": "staged-tracker-output"})
    return promoted, unchanged


def _edition_module():
    """Load the P2.2 edition helper by path (its package dir is ``import/``)."""
    import importlib.util
    path = SCRIPTS_ROOT / "import" / "edition.py"
    spec = importlib.util.spec_from_file_location("ge16_edition_module_for_promotion", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_promotion_edition(run, promoted, unchanged, canonical_root=ROOT,
                            prior=None, now=None):
    """Record one ``ge16.edition.promotion.v1`` manifest bound to the run.

    Reuses the P2.2 edition helpers (timestamp shape, prior-id scan) so both
    edition kinds share one chain in ``canonical/editions/``; the importer's
    own writer and the frozen ``ge16.edition.v1`` schema are untouched.
    """
    edition = _edition_module()
    editions_dir = Path(canonical_root) / "editions"
    editions_dir.mkdir(parents=True, exist_ok=True)
    while True:
        edition_id, created_at = edition.utc_timestamps(now)
        if prior is None:
            prior = edition.prior_edition_id(str(editions_dir))
        manifest = {
            "schema": PROMOTION_SCHEMA,
            "edition_id": edition_id,
            "created_at": created_at,
            "content_hashes": {entry["file"]: entry["sha256"] for entry in promoted},
            "lineage": {"prior_edition": prior, "run_id": run.run_id,
                        "inputs": promoted},
            "row_counts": {"promoted_files": len(promoted),
                           "unchanged_files": len(unchanged)},
            "note": "P2.3 refresh promotion: run-staged tracker outputs copied into "
                    "canonical; file paths are canonical-root-relative.",
        }
        path = editions_dir / f"edition-{edition_id}.json"
        if not path.exists():
            break
        if now is not None:
            raise ValueError(f"edition already exists: {path} (pinned clock collision)")
        time.sleep(0.05)
        now = None
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
    return edition_id, str(path)


def post_promotion_gate(run, edition_id, canonical_root):
    """P2.4 REPORT-ONLY gate: verify the new edition + a corpus sample.

    Runs exactly the two checks the P2.4 brief wires into refresh run mode
    (``verify-edition <new>`` + ``verify-corpus --sampled 500``), records both
    JSON reports under ``run/logs/`` so the run dir stays self-describing,
    and on any nonzero verdict raises with the edition id — a hard stop. The
    verifier never repairs or rolls back: promotion has already landed its
    files, and the owner decides what happens next. Never runs under
    ``--no-run`` (that path has no run, no promotion, nothing to gate).
    """
    reports = [
        integrity.verify_edition(edition_id, canonical_root=canonical_root),
        integrity.verify_corpus(canonical_root=canonical_root, sample=500, seed=0),
    ]
    for report in reports:
        (run.logs / f"integrity-{report['command']}.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8")
    failed = [(report["command"], report["exit_code"], report["status"])
              for report in reports if report["exit_code"]]
    if failed:
        (run.logs / "integrity-verify-failure.json").write_text(
            json.dumps({"edition_id": edition_id, "failed": failed}, indent=2,
                       ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        raise RuntimeError(
            "post-promotion integrity gate failed for edition %s (%s): verifier is "
            "report-only, canonical left exactly as promoted; reports in %s — "
            "owner decides on repair" % (
                edition_id,
                ", ".join("%s exit %d (%s)" % failure for failure in failed),
                run.logs))
    return reports


def staged_refresh(label="", canonical_root=ROOT, collector_commands=None, now=None):
    """One full run-scoped refresh: run -> stage -> promote -> edition -> gate -> refresh.

    Returns the run summary (also printed by ``main --run``). The P2.4 gate
    sits between the promotion edition and the provenance refresh, so a
    failed verification is a hard stop BEFORE provenance rewrites anything.
    The refresh at the end is exactly the pre-P2.3 ``refresh()`` — promotion
    only ever adds canonical files, so its fail-closed corpus-shrink guard
    holds.
    """
    canonical_root = Path(canonical_root)
    prior_edition = _edition_module().prior_edition_id(str(canonical_root / "editions"))
    run = work_paths.new_run(label=label, data_root=canonical_root.parent,
                             inputs_edition=prior_edition)
    work_paths.apply_env(run)
    staged = stage_collectors(run, commands=collector_commands)
    promoted, unchanged = promote(run, canonical_root)
    edition_id, edition_path = write_promotion_edition(
        run, promoted, unchanged, canonical_root, prior=prior_edition, now=now)
    verified = post_promotion_gate(run, edition_id, canonical_root)
    files = refresh(canonical_root)
    return {"run_id": run.run_id, "run_dir": str(run.root),
            "label": label, "inputs_edition": prior_edition,
            "collectors": staged, "promoted": promoted, "unchanged": unchanged,
            "edition_id": edition_id, "edition_path": edition_path,
            "integrity": [{"command": report["command"], "exit_code": report["exit_code"],
                           "status": report["status"]} for report in verified],
            "files": files}


def main(argv=None, canonical_root=None):
    root = Path(canonical_root) if canonical_root else ROOT
    parser = argparse.ArgumentParser(
        description="Refresh canonical provenance; --run (default) wraps the "
                    "refresh in a run-scoped staged collect + promotion.")
    parser.add_argument("--run", dest="run", action="store_true", default=True,
                        help="Create a run under data/work/, stage collectors "
                             "into it, promote into canonical, write a promotion "
                             "edition, then refresh provenance (default).")
    parser.add_argument("--no-run", dest="run", action="store_false",
                        help="Pre-P2.3 behavior exactly: refresh provenance only; "
                             "no run dir, no collector invocation, no promotion.")
    parser.add_argument("--label", default="", help="Run label recorded in run.json.")
    args = parser.parse_args(argv)
    if not args.run:
        print(f"canonical refresh passed: files={refresh(root)}")
        return 0
    summary = staged_refresh(label=args.label, canonical_root=root)
    print(f"run {summary['run_id']} (label={summary['label']!r}) -> {summary['run_dir']}")
    for collector in summary["collectors"]:
        print(f"collector exit={collector['returncode']}: {collector['script']} "
              f"(log: {collector['log']})")
    print(f"promotion: {len(summary['promoted'])} file(s) copied, "
          f"{len(summary['unchanged'])} already current; edition {summary['edition_id']} "
          f"(lineage.run_id={summary['run_id']})")
    print("integrity gate: " + ", ".join(
        f"{check['command']} exit={check['exit_code']} ({check['status']})"
        for check in summary.get("integrity", ())))
    print(f"canonical refresh passed: files={summary['files']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        print(f"canonical refresh failed: {error}", file=sys.stderr)
        raise SystemExit(1)
