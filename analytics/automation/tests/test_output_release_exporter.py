import hashlib
import importlib.util
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest


EXPORTER_PATH = Path(__file__).resolve().parents[1] / "outputs" / "release_exporter.py"
VALIDATOR_PATH = Path(__file__).resolve().parents[3] / "3_OUTPUTS" / "scripts" / "validate_release_intake.py"
OUTPUTS_ROOT = VALIDATOR_PATH.parents[1]
OUTPUTS_CONTRACT_COMMIT = "655064f7994a970ca400bd8b73c5fb9c96600b72"


def _required_delivery_entries():
    validator_source = subprocess.check_output(
        [
            "git",
            "-C",
            str(OUTPUTS_ROOT),
            "show",
            "%s:scripts/validate_release_intake.py" % OUTPUTS_CONTRACT_COMMIT,
        ],
        text=True,
    )
    namespace = {"__name__": "outputs_delivery_semantics_fixture"}
    exec(compile(validator_source, "outputs-validator-fixture", "exec"), namespace)
    return namespace["required_delivery_entries"]()


def _load_exporter():
    spec = importlib.util.spec_from_file_location("release_exporter", EXPORTER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _complete_delivery_fixture(source_root):
    semantics = {
        "schema": "outputs.delivery-semantics.v1",
        "entries": _required_delivery_entries(),
    }
    mapping = {
        "forecast": {},
        "report": {},
        "social": {},
        "tracking": {},
        "state-composition": {},
    }
    for index, entry in enumerate(semantics["entries"]):
        source = source_root / ("%03d-%s" % (index, entry["role"])).replace("/", "-")
        source.write_bytes((entry["role"] + "\n").encode("utf-8"))
        mapping[entry["category"]][entry["source_path"]] = source
    return mapping, semantics


def _stage(tmp_path, **overrides):
    exporter = _load_exporter()
    source_root = tmp_path / "analytics-sources"
    source_root.mkdir()
    artifact_mapping, delivery_semantics = _complete_delivery_fixture(source_root)
    arguments = {
        "destination_root": tmp_path / "staging-output",
        "analytics_commit_sha": "a" * 40,
        "data_commit_sha": "b" * 40,
        "run_id": "run-20260905.1",
        "artifact_mapping": artifact_mapping,
        "delivery_semantics": delivery_semantics,
    }
    arguments.update(overrides)
    return exporter, source_root, arguments


def test_stages_deterministic_valid_outputs_release_without_mutating_sources(tmp_path):
    exporter, source_root, arguments = _stage(tmp_path)
    source_hashes = {path: _sha256(path) for path in source_root.iterdir()}

    release_dir = exporter.stage_release(**arguments)

    assert release_dir == arguments["destination_root"] / "releases" / arguments["run_id"]
    assert {path: _sha256(path) for path in source_root.iterdir()} == source_hashes
    assert sorted(path.name for path in release_dir.iterdir()) == [
        "SHA256SUMS",
        "artifacts",
        "manifest.json",
        "receipt.json",
    ]
    manifest = json.loads((release_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == "outputs.release-manifest.v1"
    assert manifest["release_id"] == arguments["run_id"]
    assert manifest["provenance"] == {
        "analytics_commit_sha": "a" * 40,
        "data_commit_sha": "b" * 40,
        "run_id": arguments["run_id"],
        "artifact_manifest_sha256": _sha256(release_dir / "SHA256SUMS"),
    }
    assert [artifact["path"] for artifact in manifest["artifacts"]] == sorted(
        artifact["path"] for artifact in manifest["artifacts"]
    )
    assert {artifact["category"] for artifact in manifest["artifacts"]} == set(arguments["artifact_mapping"])
    assert manifest["delivery_semantics"] == arguments["delivery_semantics"]

    receipt = json.loads((release_dir / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["manifest_sha256"] == _sha256(release_dir / "manifest.json")
    assert receipt["sha256sums_sha256"] == _sha256(release_dir / "SHA256SUMS")

    result = subprocess.run(
        [sys.executable, str(VALIDATOR_PATH), "--release-dir", str(release_dir)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    second_root = tmp_path / "second-staging-output"
    second_release = exporter.stage_release(**dict(arguments, destination_root=second_root))
    assert {
        path.relative_to(release_dir): path.read_bytes()
        for path in release_dir.rglob("*")
        if path.is_file()
    } == {
        path.relative_to(second_release): path.read_bytes()
        for path in second_release.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize(
    "field, value, message",
    [
        ("analytics_commit_sha", "A" * 40, "analytics_commit_sha"),
        ("data_commit_sha", "not-a-full-sha", "data_commit_sha"),
        ("run_id", "../unsafe", "run_id"),
    ],
)
def test_requires_exact_provenance_identifiers_before_writing(tmp_path, field, value, message):
    exporter, _, arguments = _stage(tmp_path, **{field: value})

    with pytest.raises(ValueError, match=message):
        exporter.stage_release(**arguments)

    assert not arguments["destination_root"].exists()


def test_rejects_missing_categories_duplicate_and_unsafe_target_paths(tmp_path):
    exporter, _, arguments = _stage(tmp_path)
    missing = dict(arguments["artifact_mapping"])
    missing.pop("tracking")
    with pytest.raises(ValueError, match="missing required artifact categories"):
        exporter.stage_release(**dict(arguments, artifact_mapping=missing))

    forecast_path = next(iter(arguments["artifact_mapping"]["forecast"]))
    duplicate = dict(arguments["artifact_mapping"])
    duplicate["social"] = {forecast_path: next(iter(arguments["artifact_mapping"]["social"].values()))}
    with pytest.raises(ValueError, match="duplicate artifact path"):
        exporter.stage_release(**dict(arguments, artifact_mapping=duplicate))

    unsafe = dict(arguments["artifact_mapping"])
    unsafe["tracking"] = {"artifacts/../escape.json": next(iter(arguments["artifact_mapping"]["tracking"].values()))}
    with pytest.raises(ValueError, match="safe artifacts/"):
        exporter.stage_release(**dict(arguments, artifact_mapping=unsafe))


def test_rejects_noncanonical_artifact_path_before_duplicate_detection(tmp_path):
    exporter, _, arguments = _stage(tmp_path)
    forecast_path = next(iter(arguments["artifact_mapping"]["forecast"]))
    noncanonical = dict(arguments["artifact_mapping"])
    noncanonical["social"] = {
        forecast_path.replace("artifacts/", "artifacts//", 1): next(
            iter(arguments["artifact_mapping"]["social"].values())
        )
    }

    with pytest.raises(ValueError, match="canonical artifacts/"):
        exporter.stage_release(**dict(arguments, artifact_mapping=noncanonical))

    assert not arguments["destination_root"].exists()


def test_rejects_symlink_or_non_regular_declared_sources_and_existing_release(tmp_path):
    exporter, source_root, arguments = _stage(tmp_path)
    forecast_path = next(iter(arguments["artifact_mapping"]["forecast"]))
    forecast_source = arguments["artifact_mapping"]["forecast"][forecast_path]
    linked = source_root / "linked.json"
    linked.symlink_to(forecast_source)
    mapping = dict(arguments["artifact_mapping"])
    mapping["forecast"] = {forecast_path: linked}
    with pytest.raises(ValueError, match="regular file"):
        exporter.stage_release(**dict(arguments, artifact_mapping=mapping))
    assert not arguments["destination_root"].exists()

    mapping["forecast"] = {forecast_path: source_root}
    with pytest.raises(ValueError, match="regular file"):
        exporter.stage_release(**dict(arguments, artifact_mapping=mapping))
    assert not arguments["destination_root"].exists()

    release_dir = exporter.stage_release(**arguments)
    original_manifest = (release_dir / "manifest.json").read_bytes()
    with pytest.raises(FileExistsError, match="already exists"):
        exporter.stage_release(**arguments)
    assert (release_dir / "manifest.json").read_bytes() == original_manifest


def test_rejects_regular_source_reached_through_symlinked_parent(tmp_path):
    exporter, source_root, arguments = _stage(tmp_path)
    forecast_path = next(iter(arguments["artifact_mapping"]["forecast"]))
    forecast_source = arguments["artifact_mapping"]["forecast"][forecast_path]
    linked_parent = tmp_path / "linked-analytics-sources"
    linked_parent.symlink_to(source_root, target_is_directory=True)
    mapping = dict(arguments["artifact_mapping"])
    mapping["forecast"] = {forecast_path: linked_parent / forecast_source.name}

    with pytest.raises(ValueError, match="symlinked path component"):
        exporter.stage_release(**dict(arguments, artifact_mapping=mapping))

    assert not arguments["destination_root"].exists()


def test_requires_delivery_semantics_before_creating_destination(tmp_path):
    exporter, _, arguments = _stage(tmp_path)
    missing = dict(arguments)
    missing.pop("delivery_semantics")

    with pytest.raises(TypeError, match="delivery_semantics"):
        exporter.stage_release(**missing)
    with pytest.raises(ValueError, match="delivery_semantics"):
        exporter.stage_release(**dict(arguments, delivery_semantics=None))

    assert not arguments["destination_root"].exists()


@pytest.mark.parametrize(
    "mutate, message",
    [
        (
            lambda semantics, _mapping: semantics["entries"].pop(),
            "map every declared artifact exactly once",
        ),
        (
            lambda semantics, _mapping: semantics["entries"].__setitem__(
                0, dict(semantics["entries"][0], source_path="artifacts/unknown.json")
            ),
            "exactly one declared artifact",
        ),
        (
            lambda semantics, _mapping: semantics["entries"].__setitem__(
                0, dict(semantics["entries"][0], category="report")
            ),
            "category does not match",
        ),
        (
            lambda semantics, _mapping: semantics["entries"].__setitem__(
                1, dict(semantics["entries"][1], role=semantics["entries"][0]["role"])
            ),
            "duplicate delivery semantic role",
        ),
        (
            lambda semantics, _mapping: semantics["entries"].__setitem__(
                1,
                dict(
                    semantics["entries"][1],
                    destination_path=semantics["entries"][0]["destination_path"],
                ),
            ),
            "duplicate delivery semantic destination path",
        ),
        (
            lambda semantics, _mapping: semantics["entries"].__setitem__(
                0, dict(semantics["entries"][0], destination_path="data//forecast.json")
            ),
            "canonical delivery destination path",
        ),
        (
            lambda semantics, _mapping: semantics["entries"].__setitem__(
                0, dict(semantics["entries"][0], destination_path="current/forecast.json")
            ),
            "safe delivery destination path",
        ),
    ],
)
def test_rejects_absent_or_mismatched_delivery_semantics_before_writing(
    tmp_path, mutate, message
):
    exporter, _, arguments = _stage(tmp_path)
    semantics = deepcopy(arguments["delivery_semantics"])
    mutate(semantics, arguments["artifact_mapping"])

    with pytest.raises(ValueError, match=message):
        exporter.stage_release(**dict(arguments, delivery_semantics=semantics))

    assert not arguments["destination_root"].exists()


def test_cleans_sibling_temporary_staging_when_copying_fails(tmp_path, monkeypatch):
    exporter, _, arguments = _stage(tmp_path)

    def fail_copy(*_args, **_kwargs):
        raise OSError("injected copy failure")

    monkeypatch.setattr(exporter.shutil, "copyfile", fail_copy)

    with pytest.raises(OSError, match="injected copy failure"):
        exporter.stage_release(**arguments)

    releases = arguments["destination_root"] / "releases"
    assert not (releases / arguments["run_id"]).exists()
    assert list(releases.iterdir()) == []


def test_cleans_moved_staging_when_rename_raises_after_moving(tmp_path, monkeypatch):
    exporter, _, arguments = _stage(tmp_path)
    original_rename = Path.rename

    def move_then_raise(path, target):
        original_rename(path, target)
        raise OSError("injected post-rename failure")

    monkeypatch.setattr(Path, "rename", move_then_raise)

    with pytest.raises(OSError, match="injected post-rename failure"):
        exporter.stage_release(**arguments)

    releases = arguments["destination_root"] / "releases"
    assert not (releases / arguments["run_id"]).exists()
    assert list(releases.iterdir()) == []


def test_rejects_a_self_consistent_five_entry_semantics_map_via_outputs_contract(tmp_path):
    exporter, _, arguments = _stage(tmp_path)
    categories = ("forecast", "report", "social", "tracking", "state-composition")
    reduced_mapping = {category: {} for category in categories}
    reduced_entries = []
    for entry in arguments["delivery_semantics"]["entries"]:
        if entry["category"] not in reduced_mapping or reduced_mapping[entry["category"]]:
            continue
        reduced_mapping[entry["category"]][entry["source_path"]] = (
            arguments["artifact_mapping"][entry["category"]][entry["source_path"]]
        )
        reduced_entries.append(entry)

    with pytest.raises(ValueError, match="OUTPUTS contract"):
        exporter.stage_release(
            **dict(
                arguments,
                artifact_mapping=reduced_mapping,
                delivery_semantics={"schema": "outputs.delivery-semantics.v1", "entries": reduced_entries},
            )
        )

    assert not (arguments["destination_root"] / "releases" / arguments["run_id"]).exists()
    assert not list((arguments["destination_root"] / "releases").iterdir())


def test_detects_source_mutation_after_copy_and_leaves_no_release_residue(tmp_path, monkeypatch):
    exporter, _, arguments = _stage(tmp_path)
    original_copyfile = exporter.shutil.copyfile
    source = next(iter(arguments["artifact_mapping"]["forecast"].values()))

    def copy_then_mutate(copy_source, destination, **kwargs):
        result = original_copyfile(copy_source, destination, **kwargs)
        if Path(copy_source) == source:
            source.write_bytes(b"mutated after copy\n")
        return result

    monkeypatch.setattr(exporter.shutil, "copyfile", copy_then_mutate)

    with pytest.raises(RuntimeError, match="source changed while staging"):
        exporter.stage_release(**arguments)

    releases = arguments["destination_root"] / "releases"
    assert not (releases / arguments["run_id"]).exists()
    assert not list(releases.iterdir())


def test_rejects_symlinked_destination_ancestors_at_any_depth(tmp_path):
    exporter, _, arguments = _stage(tmp_path)
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(ValueError, match="symlinked path component"):
        exporter.stage_release(**dict(arguments, destination_root=linked_parent / "exports"))

    destination_root = tmp_path / "destination"
    destination_root.mkdir()
    real_releases = tmp_path / "real-releases"
    real_releases.mkdir()
    (destination_root / "releases").symlink_to(real_releases, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinked path component"):
        exporter.stage_release(**dict(arguments, destination_root=destination_root))
