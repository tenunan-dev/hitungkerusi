#!/usr/bin/env python3
"""Publish the exact externally bound OUTPUTS seal, without pruning or Git writes."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re

DELIVERY_ROOT = Path(__file__).resolve().parents[1]


def safe_path(path):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("symlinked delivery input or record")
    if path.is_file() and path.stat().st_nlink != 1:
        raise ValueError("hard-linked delivery input or record")
    return path


def digest(path):
    return hashlib.sha256(safe_path(path).read_bytes()).hexdigest()


def load_publisher():
    path = DELIVERY_ROOT.parent / "2_ANALYTICS/automation/delivery/publish_delivery.py"
    spec = importlib.util.spec_from_file_location("ge16_delivery_publisher", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.DELIVERY_ROOT = DELIVERY_ROOT
    module.OUTPUTS_ROOT = DELIVERY_ROOT.parent / "3_OUTPUTS"
    module.DATA_ROOT = DELIVERY_ROOT.parent / "1_DATA"
    return module


def publish(release_id):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", release_id) or release_id.casefold() in {"current", "latest"}:
        raise ValueError("invalid release ID")
    outputs = DELIVERY_ROOT.parent / "3_OUTPUTS"
    seal_path = safe_path(outputs / "manifest/ge16-release-seal-input.json")
    seal = json.loads(seal_path.read_text())
    if (set(seal) != {"schema", "release_id", "sealed_commit", "sealed_at_utc", "manifest_sha256"}
            or seal["schema"] != "outputs.release-seal-input.v1" or seal["release_id"] != release_id
            or not isinstance(seal["sealed_commit"], str) or not re.fullmatch(r"[0-9a-f]{40}", seal["sealed_commit"])
            or seal["manifest_sha256"] != digest(outputs / "releases" / release_id / "manifest.json")):
        raise ValueError("release seal input mismatch")
    publisher = load_publisher()
    release = safe_path(DELIVERY_ROOT / "releases" / release_id)
    record_path = safe_path(DELIVERY_ROOT / "manifest/ge16-delivery-input.json")
    marker = safe_path(DELIVERY_ROOT / "manifest/.needs-git-init")
    # Mark even a failed attempt that leaves staging writes behind.
    if not (DELIVERY_ROOT / ".git").is_dir():
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("4_DELIVERY commit: none (no local repo yet)\n")
    if release.exists():
        publisher._validated_outputs_release(release_id, seal["sealed_commit"])
        manifest = json.loads(safe_path(release / "DELIVERY.json").read_text())
        if any(manifest.get(key) != value for key, value in {
            "delivery_id": release_id, "outputs_release_id": release_id,
            "outputs_sealed_commit": seal["sealed_commit"],
        }.items()):
            raise ValueError("existing delivery provenance mismatch")
        actual = publisher.compute_manifest(release)
        if any(actual[key] != manifest.get(key) for key in ("files", "file_count", "total_bytes")) or publisher.validate_staging(release, manifest):
            raise ValueError("existing delivery hashes or gates mismatch")
        result = {"status": "already-published", "delivery_id": release_id}
    else:
        result = publisher.publish(delivery_id=release_id, outputs_release_id=release_id,
                                   sealed_commit=seal["sealed_commit"], prune=False)
        if result["status"] != "published":
            return result
    record = {"schema": "delivery.gate-input.v1", "delivery_id": release_id,
              "outputs_release_id": release_id, "outputs_sealed_commit": seal["sealed_commit"],
              "outputs_seal_input_sha256": digest(seal_path),
              "delivery_manifest_sha256": digest(release / "DELIVERY.json"),
              "delivery_commit": None,
              "delivery_commit_note": "4_DELIVERY commit: none (no local repo yet)"}
    record_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(record, sort_keys=True, indent=2) + "\n"
    if not record_path.exists() or record_path.read_text() != encoded:
        record_path.write_text(encoded)
    return {**result, "delivery_commit": None, "needs_git_init": str(marker) if marker.exists() else None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-id", required=True)
    args = parser.parse_args(argv)
    try:
        result = publish(args.release_id)
    except (OSError, ValueError, RuntimeError) as exc:
        result = {"status": "failed", "error": str(exc)}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] in {"published", "already-published"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
