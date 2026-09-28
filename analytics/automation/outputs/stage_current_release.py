#!/usr/bin/env python3
"""Stage the current ANALYTICS artifact set into one unsealed OUTPUTS release.

This is deliberately a staging boundary, not a publisher.  It accepts only
current ANALYTICS files and delegates the atomic copy plus intake validation to
``release_exporter.stage_release``.  It never reads DELIVERY.
"""

import argparse
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path

try:
    from automation import paths as working_paths
except ModuleNotFoundError:  # Direct-script execution has only outputs/ on sys.path.
    sys.path.insert(0, os.fspath(Path(__file__).resolve().parents[2]))
    from automation import paths as working_paths


CONTRACT_COMMIT = "655064f7994a970ca400bd8b73c5fb9c96600b72"
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
REQUIRED_CATEGORIES = frozenset(
    {"forecast", "report", "social", "tracking", "state-composition"}
)
STATES = (
    ("johor", "Johor"), ("kedah", "Kedah"), ("kelantan", "Kelantan"),
    ("melaka", "Melaka"), ("negeri-sembilan", "Negeri Sembilan"),
    ("pahang", "Pahang"), ("perak", "Perak"), ("perlis", "Perlis"),
    ("pulau-pinang", "Pulau Pinang"), ("sabah", "Sabah"),
    ("sarawak", "Sarawak"), ("selangor", "Selangor"),
    ("terengganu", "Terengganu"),
)
PRN_STATE_IDS = frozenset({"melaka", "pahang", "perak", "perlis", "sarawak"})


def _report_sources():
    """Name only files that the current ANALYTICS layout actually generates."""
    sources = {
        "report.federal.en": "03_REPORTS/federal/latest/GE16_Malaysia_General_Election_Report.md",
        "report.federal.ms": "03_REPORTS/federal/latest/GE16_Malaysia_General_Election_Report_MS.md",
    }
    for state_id, state_name in STATES:
        root = "03_REPORTS/states/DUN %s/latest" % state_name
        sources.update(
            {
                "report.state.%s.en" % state_id: "%s/GE16_%s_Report.md" % (root, state_name),
                "report.state.%s.ms" % state_id: "%s/GE16_%s_Report_MS.md" % (root, state_name),
                "report.state-summary.%s.en" % state_id: "%s/dun-election-summary.md" % root,
                "report.state-summary.%s.ms" % state_id: "%s/dun-election-summary_MS.md" % root,
                "report.state-deepdive.%s" % state_id: "%s/ge16-battleground-deepdive.md" % root,
            }
        )
        if state_id in PRN_STATE_IDS:
            sources["report.prn-projection.%s" % state_id] = (
                "%s/%s-prn-projection-report.md" % (root, state_id)
            )
    return sources


# Generated roles are intentionally not paths in ANALYTICS.  They are accepted
# only from the explicit, validated temporary payload root supplied by the
# P1.3c builder; the remaining roles retain their current source paths below.
AUTHORITATIVE_SOURCE_RELATIVES = _report_sources()
AUTHORITATIVE_SOURCE_RELATIVES.update(
    {
        "forecast-engine": "02_FORECAST/outputs/latest/ge16-forecast-latest.json",
        "tracking.payload": "work/tracking/LATEST.json",
    }
)


def resolve_tracking_source(analytics_root):
    """Prefer the working tracker pointer, retaining only a warned legacy fallback."""
    working = working_paths.tracking_root(analytics_root) / "LATEST.json"
    if working.exists():
        return working
    legacy = Path(analytics_root) / ".tracking" / "LATEST.json"
    if legacy.exists():
        warnings.warn(
            "stage_current_release is falling back to legacy .tracking/LATEST.json; "
            "generate work/tracking/LATEST.json for the working-root contract",
            RuntimeWarning,
            stacklevel=2,
        )
        return legacy
    return working
PAYLOAD_ROLE_FILENAMES = {
    "app-data": "app-data.json", "forecast": "forecast.json", "scenarios": "scenarios.json",
    "social.payload": "social-payload.json", "state-composition.melaka": "melaka.json",
    "state-composition.pahang": "pahang.json", "state-composition.perak": "perak.json",
    "state-composition.perlis": "perlis.json", "state-composition.sarawak": "sarawak.json",
}


class PreflightError(ValueError):
    """A complete, no-write current-artifact preflight failure."""

    def __init__(self, report):
        super().__init__("current artifact preflight failed")
        self.report = report


@dataclass
class StagePlan:
    artifact_mapping: dict
    delivery_semantics: dict
    source_paths: tuple
    report: dict


def _run_git(repository, arguments):
    return subprocess.run(
        ["git", "-C", os.fspath(repository), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _git_text(repository, arguments):
    result = _run_git(repository, arguments)
    if result.returncode:
        return None, result.stderr.decode("utf-8", "replace").strip()
    return result.stdout.decode("utf-8", "strict").strip(), None


def _symlink_error(path, label):
    """Return an error for a lexical symlink ancestor, without resolving it."""
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            return "%s cannot be inspected: %s" % (label, current)
        if stat.S_ISLNK(mode):
            return "%s has symlinked path component: %s" % (label, current)
    return None


def _repository_facts(repository, label, supplied_sha):
    repository = Path(repository)
    errors = []
    symlink_error = _symlink_error(repository, "%s root" % label)
    if symlink_error:
        errors.append({"code": "%s_unsafe_root" % label.lower(), "detail": symlink_error})
        return repository, errors
    root_text, root_error = _git_text(repository, ["rev-parse", "--show-toplevel"])
    if root_error or not root_text:
        errors.append(
            {
                "code": "%s_not_repository" % label.lower(),
                "detail": root_error or "no Git top-level returned",
            }
        )
        return repository, errors
    root = Path(root_text)
    if os.path.abspath(os.fspath(repository)) != os.path.abspath(os.fspath(root)):
        errors.append(
            {
                "code": "%s_not_repository_root" % label.lower(),
                "detail": "%s is not its Git top-level %s" % (repository, root),
            }
        )
    head, head_error = _git_text(root, ["rev-parse", "HEAD"])
    if head_error or not head:
        errors.append({"code": "%s_head_unavailable" % label.lower(), "detail": head_error or ""})
    elif supplied_sha is not None and supplied_sha != head:
        errors.append(
            {
                "code": "%s_sha_mismatch" % label.lower(),
                "expected": head,
                "supplied": supplied_sha,
            }
        )
    # Non-ignored untracked inputs have no commit provenance and must fail
    # preflight.  Ignored generated artifacts remain allowed: every selected
    # artifact is independently byte-hashed into the OUTPUTS release.
    status, status_error = _git_text(root, ["status", "--porcelain", "--untracked-files=all"])
    if status_error:
        errors.append({"code": "%s_status_unavailable" % label.lower(), "detail": status_error})
    elif status:
        errors.append(
            {
                "code": "%s_dirty" % label.lower(),
                "paths": [line for line in status.splitlines() if line],
            }
        )
    return root, errors


def _load_contract_entries(outputs_root):
    """Load only the exact immutable selection contract from OUTPUTS."""
    result = _run_git(
        outputs_root,
        ["show", "%s:scripts/validate_release_intake.py" % CONTRACT_COMMIT],
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise ValueError("cannot load committed OUTPUTS contract: %s" % detail)
    namespace = {"__name__": "stage_current_outputs_contract"}
    try:
        exec(
            compile(result.stdout, "%s:validate_release_intake.py" % CONTRACT_COMMIT, "exec"),
            namespace,
        )
        entries = namespace["required_delivery_entries"]()
    except (KeyError, SyntaxError, TypeError, ValueError) as exc:
        raise ValueError("cannot load committed OUTPUTS contract: %s" % exc) from exc
    if not isinstance(entries, list) or len(entries) != 83:
        raise ValueError("committed OUTPUTS contract does not define exactly 83 delivery roles")
    return entries


def _data_provenance_error(data_root):
    path = Path(data_root) / "canonical-data-provenance.json"
    symlink_error = _symlink_error(path, "DATA provenance")
    if symlink_error:
        return symlink_error
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return "DATA provenance must be a regular non-symlink file: %s" % path
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return "DATA provenance is unavailable or invalid: %s" % exc
    if not isinstance(payload, dict) or payload.get("schema") != "data.canonical-provenance.v2":
        return "DATA provenance has an unexpected schema: %s" % path
    return None


def _source_problem(analytics_root, source):
    if source is None:
        return "no authoritative current ANALYTICS source is defined"
    source = Path(source)
    try:
        source.relative_to(analytics_root)
    except ValueError:
        return "source is outside ANALYTICS root: %s" % source
    symlink_error = _symlink_error(source, "declared source")
    if symlink_error:
        return symlink_error
    try:
        mode = source.lstat().st_mode
    except OSError:
        return "source is missing: %s" % source
    if not stat.S_ISREG(mode):
        return "source must be a regular non-symlink file: %s" % source
    return None


def _load_payload_builder():
    path = Path(__file__).with_name("build_release_payloads.py")
    spec = importlib.util.spec_from_file_location("stage_current_payload_builder", path)
    module = importlib.util.module_from_spec(spec)
    if spec.loader is None:
        raise RuntimeError("payload builder cannot be loaded")
    spec.loader.exec_module(module)
    return module


def _payload_sources(payload_root, analytics_root, data_root):
    """Return the nine validated direct children of a caller-provided payload root."""
    if payload_root is None:
        return {}, "no explicit payload root was supplied"
    root = Path(payload_root)
    problem = _symlink_error(root, "payload root")
    if problem:
        return {}, problem
    try:
        mode = root.lstat().st_mode
    except OSError:
        return {}, "payload root is missing: %s" % root
    if not stat.S_ISDIR(mode):
        return {}, "payload root must be a real directory: %s" % root
    expected = set(PAYLOAD_ROLE_FILENAMES.values())
    try:
        actual = {path.name for path in root.iterdir()}
    except OSError as exc:
        return {}, "payload root cannot be inspected: %s" % exc
    if actual != expected:
        return {}, "payload root must contain exactly the nine generated artifacts"
    try:
        builder = _load_payload_builder()
        # The builder owns the full nine-document contract.  Do not weaken it
        # here to independent filename/schema checks: staging must reject a
        # hand-authored inventory even when all nine names are present.
        builder.validate_payload_root(root, analytics_root, data_root)
        sources = {}
        for role, filename in PAYLOAD_ROLE_FILENAMES.items():
            path = root / filename
            problem = _source_problem(root, path)
            if problem:
                return {}, problem
            sources[role] = path
    except (OSError, ValueError, RuntimeError) as exc:
        return {}, "payload root is not a valid builder payload: %s" % exc
    return sources, None


def build_plan(analytics_root, data_root, outputs_root, release_id, analytics_sha, data_sha, payload_root=None):
    """Build the 83-entry mapping and collect every pre-write failure."""
    report = {"release_id": release_id, "provenance": [], "missing": [], "unsafe": []}
    if not isinstance(release_id, str) or RUN_ID.fullmatch(release_id) is None or release_id.casefold() in {"current", "latest"}:
        report["provenance"].append({"code": "release_id_invalid", "supplied": release_id})
    if not isinstance(analytics_sha, str) or GIT_SHA.fullmatch(analytics_sha) is None:
        report["provenance"].append({"code": "analytics_sha_invalid", "supplied": analytics_sha})
    if not isinstance(data_sha, str) or GIT_SHA.fullmatch(data_sha) is None:
        report["provenance"].append({"code": "data_sha_invalid", "supplied": data_sha})
    analytics_root, analytics_errors = _repository_facts(analytics_root, "ANALYTICS", analytics_sha)
    data_root, data_errors = _repository_facts(data_root, "DATA", data_sha)
    report["provenance"].extend(analytics_errors)
    report["provenance"].extend(data_errors)
    outputs_root, outputs_errors = _repository_facts(outputs_root, "OUTPUTS", None)
    report["unsafe"].extend(outputs_errors)
    provenance_error = _data_provenance_error(data_root)
    if provenance_error:
        report["unsafe"].append({"code": "data_provenance_invalid", "detail": provenance_error})

    try:
        entries = _load_contract_entries(outputs_root)
    except ValueError as exc:
        report["unsafe"].append({"code": "outputs_contract_unavailable", "detail": str(exc)})
        entries = []

    payload_sources, payload_problem = _payload_sources(payload_root, analytics_root, data_root)
    artifact_mapping = {category: {} for category in REQUIRED_CATEGORIES}
    source_paths = []
    for entry in entries:
        role = entry["role"]
        relative = AUTHORITATIVE_SOURCE_RELATIVES.get(role)
        source = payload_sources.get(role) if role in PAYLOAD_ROLE_FILENAMES else (
            resolve_tracking_source(analytics_root) if role == "tracking.payload" else
            analytics_root / relative if relative is not None else None
        )
        artifact_mapping[entry["category"]][entry["source_path"]] = source
        source_paths.append(source)
        problem = payload_problem if role in PAYLOAD_ROLE_FILENAMES and payload_problem else _source_problem(
            payload_root if role in PAYLOAD_ROLE_FILENAMES and payload_root is not None else analytics_root, source
        )
        if problem:
            record = {"role": role, "artifact": entry["source_path"], "detail": problem}
            if source is None or not source.exists():
                report["missing"].append(record)
            else:
                report["unsafe"].append(record)

    report["provenance"].sort(key=lambda item: item.get("code", ""))
    report["missing"].sort(key=lambda item: item["role"])
    report["unsafe"].sort(key=lambda item: item.get("role", item.get("code", "")))
    return StagePlan(
        artifact_mapping=artifact_mapping,
        delivery_semantics={"schema": "outputs.delivery-semantics.v1", "entries": entries},
        source_paths=tuple(source_paths),
        report=report,
    )


def _load_release_exporter():
    path = Path(__file__).with_name("release_exporter.py")
    spec = importlib.util.spec_from_file_location("stage_current_release_exporter", path)
    module = importlib.util.module_from_spec(spec)
    if spec.loader is None:
        raise RuntimeError("release exporter cannot be loaded")
    spec.loader.exec_module(module)
    return module


def stage_current_release(analytics_root, data_root, outputs_root, release_id, analytics_sha, data_sha, payload_root=None):
    """Preflight, then atomically stage one release with the real OUTPUTS validator."""
    plan = build_plan(analytics_root, data_root, outputs_root, release_id, analytics_sha, data_sha, payload_root)
    if any(plan.report[key] for key in ("provenance", "missing", "unsafe")):
        raise PreflightError(plan.report)
    exporter = _load_release_exporter()
    # ``release_exporter`` has no destination-contract argument.  Bind its
    # process-local validator lookup to the caller's explicit OUTPUTS root.
    exporter.OUTPUTS_ROOT = Path(outputs_root)
    release_dir = exporter.stage_release(
        outputs_root,
        analytics_commit_sha=analytics_sha,
        data_commit_sha=data_sha,
        run_id=release_id,
        artifact_mapping=plan.artifact_mapping,
        delivery_semantics=plan.delivery_semantics,
    )
    return release_dir, plan.report


def _default_roots():
    analytics_root = Path(__file__).resolve().parents[2]
    data_root = analytics_root.parent / "1_DATA"
    return analytics_root, data_root


def main(argv=None):
    default_analytics, default_data = _default_roots()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--analytics-sha", required=True)
    parser.add_argument("--data-sha", required=True)
    parser.add_argument("--outputs-root", required=True, type=Path)
    parser.add_argument("--payload-root", required=True, type=Path)
    parser.add_argument("--analytics-root", type=Path, default=default_analytics)
    parser.add_argument("--data-root", type=Path, default=default_data)
    arguments = parser.parse_args(argv)
    try:
        release_dir, report = stage_current_release(
            arguments.analytics_root,
            arguments.data_root,
            arguments.outputs_root,
            arguments.release_id,
            arguments.analytics_sha,
            arguments.data_sha,
            arguments.payload_root,
        )
    except PreflightError as exc:
        print(json.dumps({"status": "preflight-failed", "report": exc.report}, sort_keys=True))
        return 2
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"status": "staging-failed", "error": str(exc)}, sort_keys=True))
        return 1
    print(
        json.dumps(
            {
                "status": "staged",
                "release_id": arguments.release_id,
                "release_dir": str(release_dir),
                "validator": "outputs.release-intake@%s" % CONTRACT_COMMIT,
                "report": report,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
