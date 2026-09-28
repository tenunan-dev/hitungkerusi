#!/usr/bin/env python3
"""Stage an intake, or bind its validated committed tree outside the release."""
import argparse
import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import tempfile

OUTPUTS_ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def safe_path(path):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("symlinked release or binding path")
    if path.is_file() and path.stat().st_nlink != 1:
        raise ValueError("hard-linked release binding")
    return path


def release_path(release_id):
    if not isinstance(release_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", release_id) or release_id.casefold() in {"current", "latest"}:
        raise ValueError("invalid release ID")
    return safe_path(OUTPUTS_ROOT / "releases" / release_id)


def write(release_id, analytics_sha, data_sha):
    release = release_path(release_id)
    if release.exists():
        raise ValueError("release already exists; refusing to rewrite intake")
    analytics = OUTPUTS_ROOT.parent / "2_ANALYTICS"
    data = OUTPUTS_ROOT.parent / "1_DATA"
    sys.path.insert(0, str(analytics))
    stage = load_module("ge16_stage_current", analytics / "automation/outputs/stage_current_release.py")
    builder = stage._load_payload_builder()
    # Temporary payloads are OUTPUTS-owned, removed before the parent's commit.
    with tempfile.TemporaryDirectory(prefix=".payload-", dir=OUTPUTS_ROOT) as temporary:
        payload = Path(temporary) / "payload"
        builder.build_payloads(analytics, data, payload, dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"))
        stage.stage_current_release(analytics, data, OUTPUTS_ROOT, release_id, analytics_sha, data_sha, payload)
    return {"status": "staged", "release_id": release_id}


def seal(release_id, sealed_commit):
    release = release_path(release_id)
    if not isinstance(sealed_commit, str) or not re.fullmatch(r"[0-9a-f]{40}", sealed_commit):
        raise ValueError("sealed commit must be a full lowercase 40-character SHA")
    validator = load_module("ge16_release_validator", OUTPUTS_ROOT / "scripts/validate_release_intake.py")
    errors = validator.validate_release_directory(release, sealed_commit=sealed_commit)
    if errors:
        raise ValueError("sealed validation failed: " + "; ".join(errors))
    digest = hashlib.sha256((release / "manifest.json").read_bytes()).hexdigest()
    path = safe_path(OUTPUTS_ROOT / "manifest/ge16-release-seal-input.json")
    binding = {"schema": "outputs.release-seal-input.v1", "release_id": release_id,
               "sealed_commit": sealed_commit, "manifest_sha256": digest}
    if path.exists():
        previous = json.loads(path.read_text())
        if previous.get("release_id") == release_id:
            if set(previous) != set(binding) | {"sealed_at_utc"} or any(previous.get(k) != v for k, v in binding.items()):
                raise ValueError("existing seal binding mismatch")
            return {"status": "already-sealed", **previous}
    binding["sealed_at_utc"] = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(binding, sort_keys=True, indent=2) + "\n")
    return {"status": "sealed", **binding}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("write", "seal"), required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--analytics-sha")
    parser.add_argument("--data-sha")
    parser.add_argument("--sealed-commit")
    args = parser.parse_args(argv)
    try:
        result = write(args.release_id, args.analytics_sha, args.data_sha) if args.stage == "write" else seal(args.release_id, args.sealed_commit)
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
