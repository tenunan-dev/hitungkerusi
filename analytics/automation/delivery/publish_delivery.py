"""Atomic, sealed-OUTPUTS delivery publisher for GE16 V2."""

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Optional


logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("publish_delivery")

PROJECT_ROOT = Path(__file__).resolve().parents[3]
OUTPUTS_ROOT = PROJECT_ROOT / "3_OUTPUTS"
DATA_ROOT = PROJECT_ROOT / "1_DATA"
DELIVERY_ROOT = PROJECT_ROOT / "4_DELIVERY"
RELEASES_TO_KEEP = 4
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
FORBIDDEN_DESTINATION_SEGMENTS = {"current", "latest", "websites", "website", "delivery", "5_websites", "4_delivery"}


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_symlinked_path_components(path: Path, label: str) -> None:
    """Reject every existing lexical component without resolving links."""
    absolute_path = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute_path.anchor)
    for component in absolute_path.parts[1:]:
        current /= component
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError(f"{label} cannot be inspected: {current}") from exc
        if stat.S_ISLNK(mode):
            raise ValueError(f"{label} has symlinked path component: {current}")


def _validate_delivery_id(delivery_id: object) -> str:
    if not isinstance(delivery_id, str) or RELEASE_ID.fullmatch(delivery_id) is None:
        raise ValueError("delivery_id must be one safe canonical basename")
    candidate = PurePosixPath(delivery_id)
    if candidate.parts != (delivery_id,) or delivery_id in {".", ".."}:
        raise ValueError("delivery_id must be one safe canonical basename")
    if delivery_id.casefold() in {"current", "latest"}:
        raise ValueError("delivery_id must not be a mutable release alias")
    return delivery_id


def _resolve_geo_source() -> Path:
    """Return the one permitted non-OUTPUTS input domain."""
    _reject_symlinked_path_components(DATA_ROOT, "DATA_ROOT")
    geo_source = DATA_ROOT / "geo"
    _reject_symlinked_path_components(geo_source, "DATA_ROOT geo source")
    if geo_source.is_symlink() or not geo_source.is_dir():
        raise FileNotFoundError(f"Canonical DATA geo source is missing: {geo_source}")
    return geo_source


def _safe_destination_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    candidate = PurePosixPath(value)
    return (
        not candidate.is_absolute()
        and not value.startswith("artifacts/")
        and all(part not in {"", ".", ".."} for part in candidate.parts)
        and not any(part.casefold() in FORBIDDEN_DESTINATION_SEGMENTS for part in candidate.parts)
    )


def _safe_source_path(value: object) -> bool:
    if not isinstance(value, str) or "\\" in value or not value.startswith("artifacts/"):
        return False
    candidate = PurePosixPath(value)
    return (
        not candidate.is_absolute()
        and len(candidate.parts) > 1
        and all(part not in {"", ".", ".."} for part in candidate.parts)
    )


def _has_symlink_component(root: Path, relative: PurePosixPath) -> bool:
    candidate = root
    for part in relative.parts:
        candidate /= part
        if candidate.is_symlink():
            return True
    return False


def _load_committed_validator(sealed_commit: str) -> dict[str, Any]:
    """Load the validator recorded in the sealing OUTPUTS commit, never HEAD."""
    result = subprocess.run(
        [
            "git", "-C", os.fspath(OUTPUTS_ROOT), "show",
            f"{sealed_commit}:scripts/validate_release_intake.py",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise ValueError(f"cannot load committed OUTPUTS validator: {detail}")
    namespace: dict[str, Any] = {"__name__": "sealed_outputs_validator"}
    try:
        exec(
            compile(
                result.stdout,
                f"{sealed_commit}:scripts/validate_release_intake.py",
                "exec",
            ),
            namespace,
        )
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"committed OUTPUTS validator could not load: {exc}") from exc
    validator = namespace.get("validate_release_directory")
    if not callable(validator):
        raise ValueError("committed OUTPUTS validator has no release validator")
    return namespace


def _validated_outputs_release(
    release_id: str, sealed_commit: object
) -> tuple[Path, list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Validate an exact immutable release before its manifest or artifacts are read."""
    if not isinstance(sealed_commit, str) or GIT_SHA.fullmatch(sealed_commit) is None:
        raise ValueError("sealed validation requires an exact 40-character lowercase OUTPUTS commit SHA")
    if not isinstance(release_id, str) or RELEASE_ID.fullmatch(release_id) is None:
        raise ValueError("OUTPUTS release ID must be a safe non-empty identifier")
    if release_id.casefold() in {"current", "latest"}:
        raise ValueError("OUTPUTS mutable release aliases are prohibited")
    _reject_symlinked_path_components(OUTPUTS_ROOT, "OUTPUTS root")
    releases_root = OUTPUTS_ROOT / "releases"
    _reject_symlinked_path_components(releases_root, "OUTPUTS releases")
    if OUTPUTS_ROOT.is_symlink() or not OUTPUTS_ROOT.is_dir():
        raise ValueError(f"OUTPUTS repository is missing or unsafe: {OUTPUTS_ROOT}")

    release_dir = releases_root / release_id
    _reject_symlinked_path_components(release_dir, "OUTPUTS release")
    if release_dir.is_symlink() or not release_dir.is_dir():
        raise ValueError(f"OUTPUTS release directory is missing or unsafe: {release_dir}")

    namespace = _load_committed_validator(sealed_commit)
    errors = namespace["validate_release_directory"](release_dir, sealed_commit=sealed_commit)
    if errors:
        raise ValueError("OUTPUTS sealed validation failed: " + "; ".join(map(str, errors)))

    # Only after the committed validator has verified exact tree bytes may the
    # publisher consume the selector map or any generated source file.
    try:
        manifest = json.loads((release_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"validated OUTPUTS manifest cannot be read: {exc}") from exc
    if manifest.get("release_id") != release_id:
        raise ValueError("validated OUTPUTS manifest release ID does not match requested release")
    semantics = manifest.get("delivery_semantics")
    entries = semantics.get("entries") if isinstance(semantics, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError("validated OUTPUTS release has absent or malformed delivery_semantics")
    if not all(isinstance(entry, dict) for entry in entries):
        raise ValueError("validated OUTPUTS release has malformed delivery_semantics entries")
    declared = manifest.get("artifacts")
    if not isinstance(declared, list):
        raise ValueError("validated OUTPUTS artifact declaration is malformed")
    declared_by_path: dict[str, dict[str, Any]] = {}
    for artifact in declared:
        if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str):
            raise ValueError("validated OUTPUTS artifact declaration is malformed")
        if artifact["path"] in declared_by_path:
            raise ValueError(f"validated OUTPUTS has duplicate declared artifact: {artifact['path']}")
        declared_by_path[artifact["path"]] = artifact

    # The manifest snapshot above is now the only selection/hash metadata used
    # for copying. Validate once more so the snapshot itself is known to belong
    # to the sealed tree; later source checks use those frozen hashes.
    errors = namespace["validate_release_directory"](release_dir, sealed_commit=sealed_commit)
    if errors:
        raise ValueError("OUTPUTS sealed validation failed while selecting semantics: " + "; ".join(map(str, errors)))
    return release_dir, entries, declared_by_path


def _copy_validated_semantics(
    release_dir: Path,
    entries: list[dict[str, Any]],
    declared_by_path: dict[str, dict[str, Any]],
    staging_dir: Path,
) -> None:
    """Copy only the committed semantic selection map, with race-safe rechecks."""
    destinations: set[str] = set()
    sources: set[str] = set()
    for entry in entries:
        source_name = entry.get("source_path")
        destination_name = entry.get("destination_path")
        if not _safe_source_path(source_name) or not _safe_destination_path(destination_name):
            raise ValueError("validated OUTPUTS delivery semantic has an unsafe source or destination")
        assert isinstance(source_name, str) and isinstance(destination_name, str)
        if source_name in sources or destination_name in destinations:
            raise ValueError("validated OUTPUTS delivery semantics has duplicate source or destination")
        sources.add(source_name)
        destinations.add(destination_name)
        artifact = declared_by_path.get(source_name)
        if artifact is None or artifact.get("category") != entry.get("category"):
            raise ValueError(f"validated OUTPUTS semantic does not match declared artifact: {source_name}")
        source_relative = PurePosixPath(source_name)
        source = release_dir.joinpath(*source_relative.parts)
        if _has_symlink_component(release_dir, source_relative) or source.is_symlink() or not source.is_file():
            raise ValueError(f"validated OUTPUTS semantic source is not a regular non-symlink file: {source_name}")
        if source.stat().st_size != artifact.get("bytes") or _sha256_of(source) != artifact.get("sha256"):
            raise ValueError(f"validated OUTPUTS semantic source changed after sealing: {source_name}")
        destination = staging_dir.joinpath(*PurePosixPath(destination_name).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        if (
            destination.stat().st_size != artifact.get("bytes")
            or _sha256_of(destination) != artifact.get("sha256")
        ):
            raise ValueError(f"validated OUTPUTS semantic source changed while copying: {source_name}")

    if sources != set(declared_by_path):
        raise ValueError("validated OUTPUTS delivery semantics does not map every declared artifact exactly once")


def _copy_geo_tree(source_root: Path, destination_root: Path) -> None:
    """Copy canonical DATA geometry without modifying its files or metadata."""
    files = []
    for candidate in sorted(source_root.rglob("*")):
        relative = candidate.relative_to(source_root)
        if candidate.is_symlink() or _has_symlink_component(source_root, PurePosixPath(relative.as_posix())):
            raise ValueError(f"canonical DATA geo contains a symlink: {relative}")
        if candidate.is_file():
            files.append((candidate, relative))
        elif not candidate.is_dir():
            raise ValueError(f"canonical DATA geo contains a non-regular entry: {relative}")
    for source, relative in files:
        destination = destination_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def build_staging(outputs_release_id: str, sealed_commit: object) -> tuple[Path, list[dict[str, Any]]]:
    """Create a candidate delivery tree from one sealed OUTPUTS release only."""
    release_dir, entries, declared_by_path = _validated_outputs_release(outputs_release_id, sealed_commit)
    _reject_symlinked_path_components(DELIVERY_ROOT, "DELIVERY root")
    staging_dir = DELIVERY_ROOT / ".staging"
    _reject_symlinked_path_components(staging_dir, "DELIVERY staging")
    if staging_dir.exists() or staging_dir.is_symlink():
        if staging_dir.is_symlink():
            raise ValueError(f"unsafe existing delivery staging path: {staging_dir}")
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True)
    try:
        _copy_validated_semantics(release_dir, entries, declared_by_path, staging_dir)
        _copy_geo_tree(_resolve_geo_source(), staging_dir / "geo")
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise
    return staging_dir, entries


def compute_manifest(staging_dir: Path) -> dict[str, Any]:
    files = {}
    total_bytes = 0
    for path in sorted(staging_dir.rglob("*")):
        if path.is_dir():
            continue
        if path.name == "DELIVERY.json":
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"staging contains a non-regular file: {path.relative_to(staging_dir)}")
        relpath = str(path.relative_to(staging_dir))
        size = path.stat().st_size
        files[relpath] = {"bytes": size, "sha256": _sha256_of(path)}
        total_bytes += size
    return {"files": files, "file_count": len(files), "total_bytes": total_bytes, "generated_at": datetime.now(timezone.utc).isoformat()}


_EXPECTED_STATES = {
    "Johor", "Kedah", "Kelantan", "Melaka", "Negeri Sembilan", "Pahang",
    "Perak", "Perlis", "Pulau Pinang", "Sabah", "Sarawak", "Selangor", "Terengganu",
}


def _load_app_data(staging_dir: Path) -> Optional[dict[str, Any]]:
    path = staging_dir / "data" / "app-data.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception as exc:  # noqa: BLE001
        return {"__parse_error__": str(exc)}


def _validate_seat_invariants(staging_dir: Path) -> list[str]:
    problems: list[str] = []
    app_data = _load_app_data(staging_dir)
    if app_data is None:
        return ["required delivery input missing: data/app-data.json is missing"]
    if "__parse_error__" in app_data:
        return [f"data/app-data.json failed to parse: {app_data['__parse_error__']}"]
    master = app_data.get("master", [])
    if len(master) != 222:
        problems.append(f"seat invariant failed: master has {len(master)} seats, expected 222")
    summary = app_data.get("summary", {})
    govt_p50 = summary.get("govt_p50")
    if govt_p50 is None:
        problems.append("seat invariant failed: summary.govt_p50 is missing")
    elif not (0 <= govt_p50 <= 222):
        problems.append(f"seat invariant failed: summary.govt_p50={govt_p50} out of [0, 222]")
    if "majority_pct" not in summary:
        problems.append("seat invariant failed: summary.majority_pct is missing")
    return problems


def _validate_required_payloads(staging_dir: Path) -> list[str]:
    """Keep the app-data, forecast-engine, forecast, and scenario gates explicit."""
    problems = []
    for relative in (
        "data/app-data.json",
        "data/forecast.json",
        "data/ge16-forecast-latest.json",
        "data/scenarios.json",
    ):
        candidate = staging_dir / relative
        if candidate.is_symlink() or not candidate.is_file():
            problems.append(f"required delivery input missing: {relative}")
    return problems


def _validate_dun_invariants(staging_dir: Path) -> list[str]:
    problems: list[str] = []
    app_data = _load_app_data(staging_dir)
    if app_data is None or "__parse_error__" in app_data:
        return problems
    declared_counts: dict[str, int] = {}
    for row in app_data.get("dun_national", []):
        state, seats = row.get("state"), row.get("seats")
        if not isinstance(state, str) or not isinstance(seats, int):
            problems.append(f"DUN invariant failed: invalid declaration {row!r}")
        elif state in declared_counts:
            problems.append(f"DUN invariant failed: duplicate declaration for {state}")
        else:
            declared_counts[state] = seats
    missing_states = _EXPECTED_STATES - set(declared_counts)
    unexpected_states = set(declared_counts) - _EXPECTED_STATES
    if missing_states:
        problems.append(f"DUN invariant failed: dun_national missing states {sorted(missing_states)}")
    if unexpected_states:
        problems.append(f"DUN invariant failed: dun_national has unexpected states {sorted(unexpected_states)}")
    if sum(declared_counts.values()) != 600:
        problems.append(f"DUN invariant failed: dun_national seats sum to {sum(declared_counts.values())}, expected 600")
    geo_dun_dir = staging_dir / "geo" / "dun"
    if not geo_dun_dir.is_dir():
        return problems + ["DUN invariant failed: geo/dun directory is missing"]
    geo_files = {path.stem: path for path in geo_dun_dir.glob("*.geojson") if path.is_file()}
    missing_geo, unexpected_geo = _EXPECTED_STATES - set(geo_files), set(geo_files) - _EXPECTED_STATES
    if missing_geo:
        problems.append(f"DUN invariant failed: geo/dun missing geometry for {sorted(missing_geo)}")
    if unexpected_geo:
        problems.append(f"DUN invariant failed: geo/dun has unexpected geometry for {sorted(unexpected_geo)}")
    geo_total = 0
    for state in sorted(_EXPECTED_STATES & set(geo_files)):
        try:
            geometry = json.loads(geo_files[state].read_text())
            features = geometry.get("features") if isinstance(geometry, dict) else None
            if geometry.get("type") != "FeatureCollection" or not isinstance(features, list):
                raise ValueError("not a FeatureCollection")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"DUN invariant failed: {state} GeoJSON failed to parse: {exc}")
            continue
        count = len(features)
        geo_total += count
        if declared_counts.get(state) is not None and count != declared_counts[state]:
            problems.append(f"DUN invariant failed: {state} GeoJSON has {count} features, expected {declared_counts[state]}")
    if geo_total != 600:
        problems.append(f"DUN invariant failed: GeoJSON features sum to {geo_total}, expected 600")
    return problems


def _validate_report_parity(staging_dir: Path) -> list[str]:
    problems: list[str] = []
    reports_dir = staging_dir / "reports"
    for report in (
        reports_dir / "federal" / "GE16_Malaysia_General_Election_Report.md",
        reports_dir / "federal" / "GE16_Malaysia_General_Election_Report_MS.md",
    ):
        if not report.is_file():
            problems.append(f"required report missing: {report.relative_to(staging_dir)}")
    for state in sorted(_EXPECTED_STATES):
        state_dir = reports_dir / "states" / f"DUN {state}"
        for report in (state_dir / f"GE16_{state}_Report.md", state_dir / f"GE16_{state}_Report_MS.md"):
            if not report.is_file():
                problems.append(f"required report missing: {report.relative_to(staging_dir)}")
    return problems


def validate_staging(staging_dir: Path, manifest: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if not staging_dir.exists() or not any(staging_dir.iterdir()):
        problems.append(f"staging_dir is empty or missing: {staging_dir}")
    for relpath, meta in manifest.get("files", {}).items():
        full_path = staging_dir / relpath
        if not full_path.is_file() or full_path.is_symlink():
            problems.append(f"manifest file missing on disk: {relpath}")
        elif full_path.stat().st_size != meta["bytes"]:
            problems.append(f"size mismatch for {relpath}: manifest={meta['bytes']} actual={full_path.stat().st_size}")
        elif _sha256_of(full_path) != meta["sha256"]:
            problems.append(f"hash mismatch for {relpath}")
    if (staging_dir / "DELIVERY.json").exists():
        problems.append("DELIVERY.json must not exist before final write")
    problems.extend(_validate_required_payloads(staging_dir))
    problems.extend(_validate_seat_invariants(staging_dir))
    problems.extend(_validate_dun_invariants(staging_dir))
    problems.extend(_validate_report_parity(staging_dir))
    return problems


def _prune_old_releases(releases_dir: Path) -> None:
    releases = sorted((path for path in releases_dir.iterdir() if path.is_dir()), key=lambda path: path.stat().st_mtime)
    if len(releases) > RELEASES_TO_KEEP:
        logger.info("%d releases present, exceeds keep count of %d (not pruning yet)", len(releases), RELEASES_TO_KEEP)


def publish(delivery_id: str, outputs_release_id: str, sealed_commit: object, *, prune: bool = True) -> dict[str, Any]:
    """Publish one delivery release from one sealed OUTPUTS release."""
    try:
        delivery_id = _validate_delivery_id(delivery_id)
        _reject_symlinked_path_components(DELIVERY_ROOT, "DELIVERY root")
        _reject_symlinked_path_components(DELIVERY_ROOT / "releases", "DELIVERY releases")
        staging_dir, _entries = build_staging(outputs_release_id, sealed_commit)
    except (OSError, ValueError) as exc:
        return {"status": "failed", "problems": [str(exc)]}
    releases_dir = DELIVERY_ROOT / "releases"
    releases_dir.mkdir(parents=True, exist_ok=True)
    manifest = compute_manifest(staging_dir)
    problems = validate_staging(staging_dir, manifest)
    if problems:
        shutil.rmtree(staging_dir, ignore_errors=True)
        return {"status": "failed", "problems": problems}
    delivery_json = {
        "delivery_id": delivery_id,
        "outputs_release_id": outputs_release_id,
        "outputs_sealed_commit": sealed_commit,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **manifest,
    }
    (staging_dir / "DELIVERY.json").write_text(json.dumps(delivery_json, indent=2, sort_keys=True))
    release_path = releases_dir / delivery_id
    if release_path.exists() or release_path.is_symlink():
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise FileExistsError(f"Release already exists: {release_path}")
    current_link, current_tmp = DELIVERY_ROOT / "current", DELIVERY_ROOT / ".current.tmp"
    if current_link.exists() and not current_link.is_symlink():
        shutil.rmtree(staging_dir, ignore_errors=True)
        return {"status": "failed", "problems": [f"current exists but is not a symlink: {current_link}"]}
    prior_target = os.readlink(current_link) if current_link.is_symlink() else None
    shutil.move(str(staging_dir), str(release_path))
    try:
        readback = json.loads((release_path / "DELIVERY.json").read_text())
        for relpath, meta in readback["files"].items():
            if _sha256_of(release_path / relpath) != meta["sha256"]:
                raise ValueError(f"Pre-exposure hash mismatch for {relpath}")
        if current_tmp.exists() or current_tmp.is_symlink():
            current_tmp.unlink()
        current_tmp.symlink_to(os.path.relpath(release_path, DELIVERY_ROOT))
        os.replace(str(current_tmp), str(current_link))
        for relpath, meta in readback["files"].items():
            if _sha256_of(current_link / relpath) != meta["sha256"]:
                raise ValueError(f"Post-exposure hash mismatch for {relpath}")
        if prune:
            _prune_old_releases(releases_dir)
        return {"status": "published", "delivery_id": delivery_id, "release_path": str(release_path), "file_count": manifest["file_count"], "total_bytes": manifest["total_bytes"]}
    except Exception:
        if current_link.exists() or current_link.is_symlink():
            current_link.unlink()
        if prior_target is not None:
            current_link.symlink_to(prior_target)
        if current_tmp.exists() or current_tmp.is_symlink():
            current_tmp.unlink()
        if release_path.exists():
            shutil.rmtree(release_path)
        raise


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python3 publish_delivery.py <delivery-id> <outputs-release-id> <sealed-outputs-commit>", file=sys.stderr)
        raise SystemExit(1)
    result = publish(sys.argv[1], sys.argv[2], sys.argv[3])
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["status"] == "published" else 1)
