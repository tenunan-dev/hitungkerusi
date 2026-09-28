"""Tiny release adapter copied into isolated fixtures; never runs on live roots."""
import argparse
import hashlib
import json
import pathlib


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


parser = argparse.ArgumentParser()
parser.add_argument("--stage", choices=("write", "seal"))
parser.add_argument("--release-id", required=True)
parser.add_argument("--analytics-sha")
parser.add_argument("--data-sha")
parser.add_argument("--sealed-commit")
args = parser.parse_args()
assert pathlib.Path(args.release_id).name == args.release_id
outputs = pathlib.Path("3_OUTPUTS/releases") / args.release_id
seal_path = pathlib.Path("3_OUTPUTS/manifest/ge16-release-seal-input.json")
if args.stage == "write":
    artifact = outputs / "artifacts/fixture.json"
    write_json(artifact, {"fixture": True})
    # Same manifest/artifact/provenance shape as O-20260906-01, with one file.
    write_json(outputs / "manifest.json", {
        "schema": "outputs.release-manifest.v1", "release_id": args.release_id,
        "artifacts": [{"path": "artifacts/fixture.json", "category": "forecast",
                       "sha256": digest(artifact), "bytes": artifact.stat().st_size}],
        "provenance": {"run_id": args.release_id,
                       "analytics_commit_sha": args.analytics_sha,
                       "data_commit_sha": args.data_sha},
    })
elif args.stage == "seal":
    write_json(seal_path, {
        "schema": "outputs.release-seal-input.v1", "release_id": args.release_id,
        "sealed_commit": args.sealed_commit, "sealed_at_utc": "2026-09-21T00:00:00Z",
        "manifest_sha256": digest(outputs / "manifest.json"),
    })
else:
    seal = json.loads(seal_path.read_text())
    release = pathlib.Path("4_DELIVERY/releases") / args.release_id
    artifact = release / "fixture.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes((outputs / "artifacts/fixture.json").read_bytes())
    write_json(release / "DELIVERY.json", {
        "delivery_id": args.release_id,
        "files": {"fixture.json": {"sha256": digest(artifact), "bytes": artifact.stat().st_size}},
    })
    write_json(pathlib.Path("4_DELIVERY/manifest/ge16-delivery-input.json"), {
        "schema": "delivery.gate-input.v1", "delivery_id": args.release_id,
        "outputs_release_id": args.release_id, "outputs_sealed_commit": seal["sealed_commit"],
        "delivery_manifest_sha256": digest(release / "DELIVERY.json"), "delivery_commit": None,
    })
    current = pathlib.Path("4_DELIVERY/current")
    current.symlink_to(pathlib.Path("releases") / args.release_id)
