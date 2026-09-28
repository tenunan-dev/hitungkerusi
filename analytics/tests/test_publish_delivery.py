import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "delivery"))
import publish_delivery as pd


OUTPUTS_ROOT = Path(__file__).resolve().parents[3] / "3_OUTPUTS"
OUTPUTS_CONTRACT_COMMIT = "655064f7994a970ca400bd8b73c5fb9c96600b72"
VALIDATOR_SOURCE = subprocess.check_output(
    [
        "git", "-C", str(OUTPUTS_ROOT), "show",
        f"{OUTPUTS_CONTRACT_COMMIT}:scripts/validate_release_intake.py",
    ],
    text=True,
)
VALIDATOR_NAMESPACE = {"__name__": "outputs_validator_fixture"}
exec(compile(VALIDATOR_SOURCE, "outputs-validator-fixture", "exec"), VALIDATOR_NAMESPACE)
REQUIRED_ENTRIES = VALIDATOR_NAMESPACE["required_delivery_entries"]()
EXPECTED_STATES = VALIDATOR_NAMESPACE["STATES"]
DUN_NATIONAL = (
    ("Johor", 56), ("Kedah", 36), ("Kelantan", 45), ("Melaka", 28),
    ("Negeri Sembilan", 36), ("Pahang", 42), ("Perak", 59), ("Perlis", 15),
    ("Pulau Pinang", 40), ("Sabah", 73), ("Sarawak", 82), ("Selangor", 56),
    ("Terengganu", 32),
)


def _run(*args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _feature_collection(count):
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"id": index + 1},
                "geometry": {"type": "Point", "coordinates": [0, 0]},
            }
            for index in range(count)
        ],
    }


def _app_data():
    return {
        "master": [{"code": f"P{index:03d}"} for index in range(1, 223)],
        "summary": {"govt_p50": 140, "majority_pct": 0.9},
        "dun_national": [{"state": state, "seats": seats} for state, seats in DUN_NATIONAL],
    }


def _artifact_bytes(entry):
    role = entry["role"]
    if role == "app-data":
        return json.dumps(_app_data()).encode()
    if entry["content_type"] == "application/json":
        return json.dumps({"role": role, "from": "sealed-outputs"}).encode()
    return ("# " + role + "\n").encode()


def _write_release(outputs_root, release_id):
    release_dir = outputs_root / "releases" / release_id
    artifacts = []
    for entry in REQUIRED_ENTRIES:
        artifact = release_dir / entry["source_path"]
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(_artifact_bytes(entry))
        artifacts.append({
            "path": entry["source_path"],
            "category": entry["category"],
            "sha256": _sha256(artifact),
            "bytes": artifact.stat().st_size,
        })
    artifacts.sort(key=lambda item: item["path"])
    sums = "".join(f"{item['sha256']}  {item['path']}\n" for item in artifacts)
    (release_dir / "SHA256SUMS").write_text(sums)
    manifest = {
        "schema": "outputs.release-manifest.v1",
        "release_id": release_id,
        "provenance": {
            "analytics_commit_sha": "a" * 40,
            "data_commit_sha": "b" * 40,
            "run_id": release_id,
            "artifact_manifest_sha256": _sha256(release_dir / "SHA256SUMS"),
        },
        "artifacts": artifacts,
        "delivery_semantics": {
            "schema": "outputs.delivery-semantics.v1", "entries": REQUIRED_ENTRIES,
        },
    }
    (release_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    receipt = {
        "schema": "outputs.release-receipt.v1",
        "release_id": release_id,
        "manifest_sha256": _sha256(release_dir / "manifest.json"),
        "sha256sums_sha256": _sha256(release_dir / "SHA256SUMS"),
        "artifacts": [
            {key: item[key] for key in ("path", "sha256", "bytes")}
            for item in artifacts
        ],
    }
    (release_dir / "receipt.json").write_text(json.dumps(receipt, indent=2))
    return release_dir


def _seal(outputs_root):
    _run("git", "add", ".", cwd=outputs_root)
    _run("git", "commit", "-m", "sealed outputs fixture", cwd=outputs_root)
    return _run("git", "rev-parse", "HEAD", cwd=outputs_root).stdout.strip()


def _write_geo(data_root):
    geo = data_root / "geo"
    (geo / "dun").mkdir(parents=True)
    (geo / "malaysia-states.geojson").write_text(json.dumps(_feature_collection(13)))
    for state, seats in DUN_NATIONAL:
        (geo / "dun" / f"{state}.geojson").write_text(
            json.dumps(_feature_collection(seats))
        )
    return geo


@pytest.fixture
def sealed_fixture(tmp_path, monkeypatch):
    outputs_root = tmp_path / "3_OUTPUTS"
    outputs_root.mkdir()
    _run("git", "init", cwd=outputs_root)
    _run("git", "config", "user.email", "tests@example.invalid", cwd=outputs_root)
    _run("git", "config", "user.name", "Publisher Tests", cwd=outputs_root)
    (outputs_root / "scripts").mkdir()
    (outputs_root / "scripts" / "validate_release_intake.py").write_text(VALIDATOR_SOURCE)
    release_id = "outputs-20260906.1"
    release_dir = _write_release(outputs_root, release_id)
    sealed_commit = _seal(outputs_root)

    delivery_root = tmp_path / "4_DELIVERY"
    delivery_root.mkdir()
    data_root = tmp_path / "1_DATA"
    geo = _write_geo(data_root)
    monkeypatch.setattr(pd, "OUTPUTS_ROOT", outputs_root)
    monkeypatch.setattr(pd, "DELIVERY_ROOT", delivery_root)
    monkeypatch.setattr(pd, "DATA_ROOT", data_root)
    return outputs_root, release_id, sealed_commit, release_dir, delivery_root, geo


def _publish(fixture, delivery_id="delivery-20260906.1"):
    _, release_id, sealed_commit, _, _, _ = fixture
    return pd.publish(delivery_id, release_id, sealed_commit)


def test_publishes_only_the_sealed_semantic_release_and_records_provenance(sealed_fixture):
    _, release_id, sealed_commit, release_dir, delivery_root, geo = sealed_fixture
    source_stats = {
        path: (path.read_bytes(), path.stat().st_size, path.stat().st_mtime_ns)
        for path in release_dir.rglob("*") if path.is_file()
    }
    geo_stats = {
        path: (path.read_bytes(), path.stat().st_size, path.stat().st_mtime_ns)
        for path in geo.rglob("*") if path.is_file()
    }

    result = _publish(sealed_fixture)

    assert result["status"] == "published"
    current = delivery_root / "current"
    manifest = json.loads((current / "DELIVERY.json").read_text())
    assert manifest["outputs_release_id"] == release_id
    assert manifest["outputs_sealed_commit"] == sealed_commit
    assert manifest["file_count"] == 83 + len(geo_stats)
    for entry in REQUIRED_ENTRIES:
        assert (current / entry["destination_path"]).read_bytes() == (
            release_dir / entry["source_path"]
        ).read_bytes()
    assert source_stats == {
        path: (path.read_bytes(), path.stat().st_size, path.stat().st_mtime_ns)
        for path in source_stats
    }
    assert geo_stats == {
        path: (path.read_bytes(), path.stat().st_size, path.stat().st_mtime_ns)
        for path in geo_stats
    }


def test_rejects_missing_or_non_exact_sealed_commit_before_reading_artifacts(sealed_fixture):
    _, release_id, sealed_commit, release_dir, delivery_root, _ = sealed_fixture
    original = (release_dir / "artifacts" / "data" / "app-data.json").read_bytes()

    for bad_commit in (None, sealed_commit.upper(), sealed_commit[:-1], "c" * 40):
        result = pd.publish("delivery-invalid-seal", release_id, bad_commit)
        assert result["status"] == "failed"
        assert not (delivery_root / "current").exists()
        assert (release_dir / "artifacts" / "data" / "app-data.json").read_bytes() == original


def test_worktree_modification_after_sealing_fails_and_legacy_hermes_files_are_ignored(sealed_fixture):
    outputs_root, release_id, sealed_commit, release_dir, delivery_root, _ = sealed_fixture
    legacy = outputs_root.parent / "HERMES" / "02_FORECAST" / "outputs" / "latest"
    legacy.mkdir(parents=True)
    (legacy / "ge16-forecast-latest.json").write_text('{"legacy": true}')
    artifact = release_dir / "artifacts" / "data" / "forecast.json"
    artifact.write_text('{"tampered": true}')

    result = pd.publish("delivery-tampered", release_id, sealed_commit)

    assert result["status"] == "failed"
    assert any("sealed validation" in item for item in result["problems"])
    assert not (delivery_root / "current").exists()


def test_legacy_hermes_generated_files_cannot_change_a_valid_publish(sealed_fixture):
    outputs_root, _release_id, _sealed_commit, release_dir, delivery_root, _ = sealed_fixture
    legacy = outputs_root.parent / "HERMES"
    for relative, contents in {
        "02_FORECAST/outputs/latest/app-data.json": b'{"legacy": "app"}',
        "02_FORECAST/outputs/latest/forecast.json": b'{"legacy": "forecast"}',
        "03_REPORTS/federal/latest/GE16_Malaysia_General_Election_Report.md": b"legacy report",
        "05_AUTOMATION/projection_scenarios.json": b'{"legacy": "scenarios"}',
    }.items():
        path = legacy / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)

    result = _publish(sealed_fixture, "delivery-with-legacy-bait")

    assert result["status"] == "published"
    current = delivery_root / "current"
    assert (current / "data" / "app-data.json").read_bytes() == (
        release_dir / "artifacts" / "data" / "app-data.json"
    ).read_bytes()
    assert (current / "reports" / "federal" / "GE16_Malaysia_General_Election_Report.md").read_bytes() == (
        release_dir / "artifacts" / "reports" / "federal" / "GE16_Malaysia_General_Election_Report.md"
    ).read_bytes()


@pytest.mark.parametrize(
    "mutation",
    (
        "missing-role", "unexpected-role", "duplicate-destination",
        "unsafe-destination", "release-id-mismatch", "undeclared-file", "symlink",
    ),
)
def test_rejects_bad_or_incomplete_semantics_and_release_structure(sealed_fixture, mutation):
    _, release_id, sealed_commit, release_dir, delivery_root, _ = sealed_fixture
    manifest_path = release_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    entries = manifest["delivery_semantics"]["entries"]
    if mutation == "missing-role":
        entries.pop()
    elif mutation == "unexpected-role":
        entries[-1]["role"] = "unexpected.role"
    elif mutation == "duplicate-destination":
        entries[1]["destination_path"] = entries[0]["destination_path"]
    elif mutation == "unsafe-destination":
        entries[0]["destination_path"] = "../escape.json"
    elif mutation == "release-id-mismatch":
        manifest["release_id"] = "different-release"
    elif mutation == "undeclared-file":
        (release_dir / "artifacts" / "surprise.txt").write_text("no")
    else:
        target = release_dir / "artifacts" / "data" / "forecast.json"
        target.unlink()
        target.symlink_to(release_dir / "artifacts" / "data" / "app-data.json")
    if mutation != "undeclared-file":
        manifest_path.write_text(json.dumps(manifest))

    result = pd.publish("delivery-invalid-semantics", release_id, sealed_commit)

    assert result["status"] == "failed"
    assert not (delivery_root / "current").exists()


def test_release_id_must_match_explicit_outputs_directory_and_preserves_rollback(sealed_fixture):
    _, release_id, sealed_commit, release_dir, delivery_root, _ = sealed_fixture
    first = _publish(sealed_fixture, "delivery-first")
    assert first["status"] == "published"
    prior_target = os.readlink(delivery_root / "current")

    wrong = pd.publish("delivery-wrong-release", "other-release", sealed_commit)
    assert wrong["status"] == "failed"
    assert os.readlink(delivery_root / "current") == prior_target
    assert (delivery_root / "current" / "data" / "app-data.json").read_bytes() == (
        release_dir / "artifacts" / "data" / "app-data.json"
    ).read_bytes()


@pytest.mark.parametrize(
    "delivery_id",
    ("../escape", "nested/release", r"nested\\release", ".", "..", "current", "LATEST", "/absolute"),
)
def test_rejects_noncanonical_delivery_ids_before_staging(sealed_fixture, delivery_id):
    _, release_id, sealed_commit, _, delivery_root, _ = sealed_fixture

    result = pd.publish(delivery_id, release_id, sealed_commit)

    assert result["status"] == "failed"
    assert not (delivery_root / "current").exists()
    assert not (delivery_root / ".staging").exists()
    assert not (delivery_root / "releases").exists()


@pytest.mark.parametrize("unsafe_root", ("data", "outputs-root", "outputs-releases", "outputs-release"))
def test_rejects_symlinked_ancestor_components_at_any_depth(sealed_fixture, monkeypatch, unsafe_root):
    outputs_root, release_id, sealed_commit, release_dir, delivery_root, _ = sealed_fixture
    linked_parent = outputs_root.parent / "linked-parent"
    linked_parent.symlink_to(outputs_root.parent, target_is_directory=True)
    if unsafe_root == "data":
        monkeypatch.setattr(pd, "DATA_ROOT", linked_parent / "1_DATA")
    elif unsafe_root == "outputs-root":
        monkeypatch.setattr(pd, "OUTPUTS_ROOT", linked_parent / "3_OUTPUTS")
    elif unsafe_root == "outputs-releases":
        actual_releases = outputs_root / "real-releases"
        (outputs_root / "releases").rename(actual_releases)
        (outputs_root / "releases").symlink_to(actual_releases, target_is_directory=True)
    else:
        actual_release = outputs_root / "actual-release"
        release_dir.rename(actual_release)
        (outputs_root / "releases" / release_id).symlink_to(actual_release, target_is_directory=True)

    result = pd.publish("delivery-symlink-depth", release_id, sealed_commit)

    assert result["status"] == "failed"
    assert not (delivery_root / "current").exists()
    assert not (delivery_root / ".staging").exists()


@pytest.mark.parametrize("prune", [True, False])
def test_prune_switch_preserves_default_and_can_disable_hook(sealed_fixture, monkeypatch, prune):
    calls = []
    monkeypatch.setattr(pd, "_prune_old_releases", calls.append)
    _, release_id, sealed_commit, _, delivery_root, _ = sealed_fixture
    kwargs = {} if prune else {"prune": False}
    result = pd.publish("prune-switch", release_id, sealed_commit, **kwargs)
    assert result["status"] == "published"
    assert calls == ([delivery_root / "releases"] if prune else [])
