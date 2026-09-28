import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "outputs" / "stage_current_release.py"
OUTPUTS_ROOT = Path(__file__).resolve().parents[3] / "3_OUTPUTS"
CONTRACT_COMMIT = "655064f7994a970ca400bd8b73c5fb9c96600b72"


def _load_module():
    spec = importlib.util.spec_from_file_location("stage_current_release", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _contract_entries():
    source = subprocess.check_output(
        [
            "git", "-C", str(OUTPUTS_ROOT), "show",
            "%s:scripts/validate_release_intake.py" % CONTRACT_COMMIT,
        ],
        text=True,
    )
    namespace = {"__name__": "stage_current_release_contract_fixture"}
    exec(compile(source, "outputs-contract-fixture", "exec"), namespace)
    return namespace["required_delivery_entries"]()


def _git(arguments, cwd):
    return subprocess.run(
        ["git", *arguments], cwd=str(cwd), check=True, text=True, capture_output=True
    ).stdout.strip()


def _repository(path):
    path.mkdir()
    _git(["init"], path)
    _git(["config", "user.email", "stage-test@example.invalid"], path)
    _git(["config", "user.name", "Stage test"], path)
    (path / ".keep").write_text("fixture\n", encoding="utf-8")
    _git(["add", ".keep"], path)
    _git(["commit", "-m", "fixture"], path)
    return _git(["rev-parse", "HEAD"], path)


def _workspace(tmp_path):
    parent = tmp_path / "renamed-workspace"
    parent.mkdir()
    analytics = parent / "2_ANALYTICS"
    data = parent / "1_DATA"
    outputs = parent / "3_OUTPUTS"
    analytics_sha = _repository(analytics)
    (analytics / ".gitignore").write_text(
        "02_FORECAST/\n03_REPORTS/\n04_SOCIAL/\n05_AUTOMATION/\n.tracking/\nwork/\n", encoding="utf-8"
    )
    _git(["add", ".gitignore"], analytics)
    _git(["commit", "-m", "ignore generated artifacts"], analytics)
    analytics_sha = _git(["rev-parse", "HEAD"], analytics)
    data_sha = _repository(data)
    _repository(outputs)
    (data / "canonical-data-provenance.json").write_text(
        json.dumps({"schema": "data.canonical-provenance.v2", "files": []}),
        encoding="utf-8",
    )
    feed = data / "research/trackers/ge16-news-feed.json"
    feed.parent.mkdir(parents=True)
    feed.write_text(json.dumps({"items": []}), encoding="utf-8")
    for state, slug in (("Melaka", "melaka"), ("Pahang", "pahang"), ("Perak", "perak"),
                        ("Perlis", "perlis"), ("Sarawak", "sarawak")):
        root = data / "research/states" / ("DUN " + state)
        root.mkdir(parents=True)
        (root / "dun-election-results-latest.csv").write_text("fixture\n", encoding="utf-8")
        (root / (slug + "-state-scenarios.json")).write_text("{}", encoding="utf-8")
    _git(["add", "."], data)
    _git(["commit", "-m", "provenance"], data)
    data_sha = _git(["rev-parse", "HEAD"], data)
    return analytics, data, outputs, analytics_sha, data_sha


def _materialize_sources(module, analytics, entries, monkeypatch, include_missing=False):
    """Make every required role available without changing production layout rules."""
    for entry in entries:
        role = entry["role"]
        relative = module.AUTHORITATIVE_SOURCE_RELATIVES.get(role)
        if relative is None:
            if not include_missing:
                continue
            # Test-only generated sources follow the repository's ignored
            # generated-artifact convention; their selected bytes are still
            # hashed by the OUTPUTS release exporter.
            relative = ".tracking/generated-contract-inputs/%s.bin" % role
            monkeypatch.setitem(module.AUTHORITATIVE_SOURCE_RELATIVES, role, relative)
        path = analytics / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((role + "\n").encode("utf-8"))


def _materialize_payload(payload_root, analytics, data):
    payload_root.mkdir()
    generated_at = "2026-09-05T12:00:00Z"
    (analytics / "work/scenarios").mkdir(parents=True, exist_ok=True)
    (analytics / "work/scenarios/scenario_meta.json").write_text("{}", encoding="utf-8")
    social_paths = ["04_SOCIAL/current.md"] + [
        "04_SOCIAL/state/%s/current.md" % state
        for state in ("Melaka", "Pahang", "Perak", "Perlis", "Sarawak")
    ]
    for relative in social_paths:
        path = analytics / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative + "\n", encoding="utf-8")

    def evidence(repository, relative):
        root = analytics if repository == "ANALYTICS" else data
        return {"repository": repository, "path": relative,
                "sha256": hashlib.sha256((root / relative).read_bytes()).hexdigest()}

    forecast_evidence = evidence("ANALYTICS", "02_FORECAST/outputs/latest/ge16-forecast-latest.json")
    metadata_evidence = evidence("ANALYTICS", "work/scenarios/scenario_meta.json")
    news_evidence = evidence("DATA", "research/trackers/ge16-news-feed.json")
    provenance = {"derivation": "release-payload",
                  "evidence": [forecast_evidence, metadata_evidence, news_evidence]}
    scenarios = {"Base (swings)": {"PH": 100, "PN": 80, "BN": 20, "GPS": 10, "GRS": 5, "WARISAN": 4, "IND": 3}}
    scenario_provenance = {"Base (swings)": {"derivation": "quantitative-scenario",
                                               "evidence": [forecast_evidence]}}
    summary = {"updated": generated_at, "govt_p50": 120, "govt_p10": 110, "govt_p90": 130,
               "majority_pct": 80, "flips": 1, "econ_term": 0, "coalition": {},
               "coalition_text": "", "govt_blocs": [],
               "opp_blocs": [], "bloc_colors": {}, "party_colors": {}, "party_bloc": {},
               "flips_list": [], "tight_seats": []}
    values = {
        "app-data.json": {"schema": "hermes.delivery.app-data", "version": 1,
                          "generated_at": generated_at, "provenance": provenance,
                          "master": [{"code": "P%03d" % i} for i in range(1, 223)],
                          "projection": [{"code": "P%03d" % i, "proj_winner": "PH"} for i in range(1, 223)],
                          "summary": summary, "scenarios": scenarios, "scenario_provenance": scenario_provenance,
                          "history": [], "general_news": [], "dun_national": [], "dun_schedule": [], "state_proj": {}},
        "forecast.json": {"schema": "hermes.delivery.forecast", "version": 1,
                          "generated_at": generated_at,
                          "provenance": {"derivation": "release-payload", "evidence": [forecast_evidence]}, **summary,
                          "coalition": {}, "govt_blocs": [], "opp_blocs": [], "bloc_colors": {},
                          "party_colors": {}, "party_bloc": {}, "history": [], "flips_list": [],
                          "tight_seats": []},
        "scenarios.json": {"schema": "hermes.delivery.scenarios", "version": 1,
                           "generated_at": generated_at,
                           "provenance": {"derivation": "release-payload",
                                          "evidence": [forecast_evidence, metadata_evidence]}, "scenarios": scenarios,
                           "scenario_provenance": scenario_provenance, "scenarios_display": [],
                           "scenario_descriptions": [], "scenario_deltas": []},
        "social-payload.json": {
            "schema": "hermes.delivery.social-payload", "version": 1, "generated_at": generated_at,
            "provenance": {"derivation": "social-source",
                           "evidence": [evidence("ANALYTICS", path) for path in social_paths]},
            "sources": [{"path": path, "sha256": evidence("ANALYTICS", path)["sha256"],
                         "bytes": len((analytics / path).read_bytes())} for path in social_paths],
            "posts": [{"source_path": path} for path in social_paths],
        },
    }
    for state, slug, total in (
        ("Melaka", "melaka", 28), ("Pahang", "pahang", 42), ("Perak", "perak", 59),
        ("Perlis", "perlis", 15), ("Sarawak", "sarawak", 82),
    ):
        values[slug + ".json"] = {
            "schema": "hermes.delivery.state-composition", "version": 1, "generated_at": generated_at,
            "provenance": {"derivation": "state-composition", "evidence": [
                evidence("DATA", "research/states/DUN %s/dun-election-results-latest.csv" % state),
                evidence("DATA", "research/states/DUN %s/%s-state-scenarios.json" % (state, slug)),
            ]},
            "state": state, "slug": slug, "total": total,
            "composition_by_bloc": {"BN": total}, "composition_by_party": {"UMNO": total},
        }
    for name, value in values.items():
        (payload_root / name).write_text(json.dumps(value), encoding="utf-8")


class _MappingFixturePayloadBuilder:
    """Keep mapping tests independent from the builder's content contract.

    The dedicated payload-builder tests exercise reconstruction.  These
    fixtures deliberately hand-author the nine role files so the tests can
    concentrate on the OUTPUTS 83-role mapping and intake contract.
    """

    @staticmethod
    def validate_payload_root(_root, _analytics_root, _data_root):
        return None


def test_payload_root_rejects_hand_authored_nine_file_inventory(tmp_path):
    module = _load_module()
    analytics = tmp_path / "2_ANALYTICS"
    data = tmp_path / "1_DATA"
    forecast = analytics / "02_FORECAST/outputs/latest/ge16-forecast-latest.json"
    forecast.parent.mkdir(parents=True)
    forecast.write_text("{}", encoding="utf-8")
    data.mkdir()
    feed = data / "research/trackers/ge16-news-feed.json"
    feed.parent.mkdir(parents=True)
    feed.write_text(json.dumps({"items": []}), encoding="utf-8")
    for state, slug in (("Melaka", "melaka"), ("Pahang", "pahang"), ("Perak", "perak"),
                        ("Perlis", "perlis"), ("Sarawak", "sarawak")):
        root = data / "research/states" / ("DUN " + state)
        root.mkdir(parents=True)
        (root / "dun-election-results-latest.csv").write_text("fixture\n", encoding="utf-8")
        (root / (slug + "-state-scenarios.json")).write_text("{}", encoding="utf-8")
    payload = tmp_path / "payload"
    _materialize_payload(payload, analytics, data)
    app = json.loads((payload / "app-data.json").read_text(encoding="utf-8"))
    app.pop("schema")
    (payload / "app-data.json").write_text(json.dumps(app), encoding="utf-8")

    sources, problem = module._payload_sources(payload, analytics, data)

    assert sources == {}
    assert problem is not None
    assert "valid builder payload" in problem


class _Exporter:
    def __init__(self):
        self.calls = []
        self.OUTPUTS_ROOT = None

    def stage_release(self, destination_root, **kwargs):
        self.calls.append((Path(destination_root), kwargs))
        return Path(destination_root) / "releases" / kwargs["run_id"]


def test_builds_the_complete_83_role_contract_mapping_without_delivery_sources(tmp_path, monkeypatch):
    module = _load_module()
    entries = _contract_entries()
    analytics, data, outputs, analytics_sha, data_sha = _workspace(tmp_path)
    monkeypatch.setattr(module, "_load_contract_entries", lambda _root: entries)
    _materialize_sources(module, analytics, entries, monkeypatch)
    payload = tmp_path / "payload"
    _materialize_payload(payload, analytics, data)
    monkeypatch.setattr(module, "_load_payload_builder", lambda: _MappingFixturePayloadBuilder)

    plan = module.build_plan(
        analytics, data, outputs, "P1.3c-20260906", analytics_sha, data_sha, payload
    )

    assert len(plan.delivery_semantics["entries"]) == 83
    assert plan.delivery_semantics["entries"] == entries
    assert sum(len(category) for category in plan.artifact_mapping.values()) == 83
    assert set(plan.artifact_mapping) == module.REQUIRED_CATEGORIES
    assert not any("DELIVERY" in str(source) for source in plan.source_paths if source)
    assert plan.report["missing"] == []
    assert {source.parent for source in plan.source_paths if source.name.endswith(".json")} >= {payload}


def test_missing_or_unsafe_inputs_report_all_failures_before_any_destination_write(tmp_path, monkeypatch):
    module = _load_module()
    entries = _contract_entries()
    analytics, data, outputs, analytics_sha, data_sha = _workspace(tmp_path)
    monkeypatch.setattr(module, "_load_contract_entries", lambda _root: entries)
    _materialize_sources(module, analytics, entries, monkeypatch)
    destination = outputs
    exporter = _Exporter()
    monkeypatch.setattr(module, "_load_release_exporter", lambda: exporter)

    with pytest.raises(module.PreflightError) as error:
        module.stage_current_release(
            analytics, data, destination, "P1.3c-20260906", analytics_sha, data_sha
        )

    report = error.value.report
    assert len(report["missing"]) == 9
    assert not (destination / "releases").exists()
    assert exporter.calls == []


def test_preflight_never_reads_a_sibling_delivery_tree(tmp_path, monkeypatch):
    module = _load_module()
    entries = _contract_entries()
    analytics, data, outputs, analytics_sha, data_sha = _workspace(tmp_path)
    monkeypatch.setattr(module, "_load_contract_entries", lambda _root: entries)
    _materialize_sources(module, analytics, entries, monkeypatch)
    delivery = analytics.parent / "4_DELIVERY"
    delivery.mkdir()
    bait = delivery / "must-not-be-read"
    bait.write_text("delivery is forbidden\n", encoding="utf-8")
    original_read_text = Path.read_text

    def reject_delivery_read(path, *args, **kwargs):
        assert delivery not in (path, *path.parents)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", reject_delivery_read)
    plan = module.build_plan(
        analytics, data, outputs, "P1.3c-20260906", analytics_sha, data_sha
    )

    assert len(plan.report["missing"]) == 9


def test_wrong_shas_and_dirty_tracked_repositories_fail_closed(tmp_path, monkeypatch):
    module = _load_module()
    entries = _contract_entries()
    analytics, data, outputs, analytics_sha, data_sha = _workspace(tmp_path)
    monkeypatch.setattr(module, "_load_contract_entries", lambda _root: entries)
    _materialize_sources(module, analytics, entries, monkeypatch, include_missing=True)
    (analytics / ".keep").write_text("dirty\n", encoding="utf-8")
    (data / ".keep").write_text("dirty\n", encoding="utf-8")
    destination = outputs

    with pytest.raises(module.PreflightError) as error:
        module.stage_current_release(
            analytics, data, destination, "P1.3c-20260906", "a" * 40, data_sha
        )

    issues = error.value.report["provenance"]
    assert {issue["code"] for issue in issues} >= {
        "analytics_sha_mismatch", "analytics_dirty", "data_dirty"
    }
    assert not (destination / "releases").exists()


@pytest.mark.parametrize(
    ("repository_name", "expected_code"),
    (("ANALYTICS", "analytics_dirty"), ("DATA", "data_dirty")),
)
def test_untracked_nonignored_input_fails_provenance_before_destination_creation(
    tmp_path, monkeypatch, repository_name, expected_code
):
    module = _load_module()
    entries = _contract_entries()
    analytics, data, outputs, analytics_sha, data_sha = _workspace(tmp_path)
    monkeypatch.setattr(module, "_load_contract_entries", lambda _root: entries)
    _materialize_sources(module, analytics, entries, monkeypatch, include_missing=True)
    repository = {"ANALYTICS": analytics, "DATA": data}[repository_name]
    untracked = repository / "untracked-provenance-input.txt"
    untracked.write_text("must be represented by a commit\n", encoding="utf-8")
    exporter = _Exporter()
    monkeypatch.setattr(module, "_load_release_exporter", lambda: exporter)

    with pytest.raises(module.PreflightError) as error:
        module.stage_current_release(
            analytics, data, outputs, "P1.3c-20260906", analytics_sha, data_sha
        )

    issue = next(
        item for item in error.value.report["provenance"] if item["code"] == expected_code
    )
    assert "?? untracked-provenance-input.txt" in issue["paths"]
    assert not (outputs / "releases").exists()
    assert exporter.calls == []


def test_rejects_symlinked_source_ancestor_before_staging(tmp_path, monkeypatch):
    module = _load_module()
    entries = _contract_entries()
    analytics, data, outputs, analytics_sha, data_sha = _workspace(tmp_path)
    monkeypatch.setattr(module, "_load_contract_entries", lambda _root: entries)
    _materialize_sources(module, analytics, entries, monkeypatch, include_missing=True)
    payload = tmp_path / "payload"
    _materialize_payload(payload, analytics, data)
    real_reports = analytics / "03_REPORTS"
    linked_reports = analytics / "linked-reports"
    linked_reports.symlink_to(real_reports, target_is_directory=True)
    monkeypatch.setitem(
        module.AUTHORITATIVE_SOURCE_RELATIVES,
        "report.federal.en",
        "linked-reports/federal/latest/GE16_Malaysia_General_Election_Report.md",
    )
    exporter = _Exporter()
    monkeypatch.setattr(module, "_load_release_exporter", lambda: exporter)

    with pytest.raises(module.PreflightError) as error:
        module.stage_current_release(
            analytics, data, outputs, "P1.3c-20260906", analytics_sha, data_sha, payload
        )

    assert any("symlinked path component" in item["detail"] for item in error.value.report["unsafe"])
    assert exporter.calls == []


def test_stages_only_after_full_preflight_and_never_mutates_sources(tmp_path, monkeypatch):
    module = _load_module()
    entries = _contract_entries()
    analytics, data, outputs, analytics_sha, data_sha = _workspace(tmp_path)
    monkeypatch.setattr(module, "_load_contract_entries", lambda _root: entries)
    _materialize_sources(module, analytics, entries, monkeypatch, include_missing=True)
    payload = tmp_path / "payload"
    _materialize_payload(payload, analytics, data)
    monkeypatch.setattr(module, "_load_payload_builder", lambda: _MappingFixturePayloadBuilder)
    source_hashes = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in analytics.rglob("*") if path.is_file()
    }
    exporter = _Exporter()
    monkeypatch.setattr(module, "_load_release_exporter", lambda: exporter)
    destination = outputs

    release_dir, report = module.stage_current_release(
        analytics, data, destination, "P1.3c-20260906", analytics_sha, data_sha, payload
    )

    assert release_dir == destination / "releases" / "P1.3c-20260906"
    assert report["missing"] == []
    assert len(exporter.calls) == 1
    staged_root, arguments = exporter.calls[0]
    assert staged_root == destination
    assert exporter.OUTPUTS_ROOT == destination
    assert len(arguments["delivery_semantics"]["entries"]) == 83
    assert sum(len(category) for category in arguments["artifact_mapping"].values()) == 83
    assert source_hashes == {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in analytics.rglob("*") if path.is_file()
    }


def test_payload_mapping_passes_the_actual_committed_outputs_validator(tmp_path, monkeypatch):
    module = _load_module()
    entries = _contract_entries()
    analytics, data, outputs, analytics_sha, data_sha = _workspace(tmp_path)
    monkeypatch.setattr(module, "_load_contract_entries", lambda _root: entries)
    _materialize_sources(module, analytics, entries, monkeypatch, include_missing=True)
    payload = tmp_path / "payload"
    _materialize_payload(payload, analytics, data)
    monkeypatch.setattr(module, "_load_payload_builder", lambda: _MappingFixturePayloadBuilder)
    plan = module.build_plan(
        analytics, data, outputs, "P1.3c-20260906", analytics_sha, data_sha, payload
    )
    assert plan.report["provenance"] == []
    assert plan.report["missing"] == []
    assert plan.report["unsafe"] == []
    exporter = module._load_release_exporter()
    # The candidate release is under pytest's directory.  The validator itself
    # is loaded from the immutable committed OUTPUTS contract, never copied or
    # mocked into the fixture OUTPUTS repository.
    exporter.OUTPUTS_ROOT = OUTPUTS_ROOT
    release = exporter.stage_release(
        tmp_path / "validator-destination", analytics_commit_sha=analytics_sha,
        data_commit_sha=data_sha, run_id="P1.3c-20260906",
        artifact_mapping=plan.artifact_mapping, delivery_semantics=plan.delivery_semantics,
    )
    assert release.is_dir()


def test_cli_returns_one_machine_readable_preflight_failure_without_writes(tmp_path, monkeypatch, capsys):
    module = _load_module()
    entries = _contract_entries()
    analytics, data, outputs, analytics_sha, data_sha = _workspace(tmp_path)
    monkeypatch.setattr(module, "_load_contract_entries", lambda _root: entries)
    _materialize_sources(module, analytics, entries, monkeypatch)
    destination = outputs

    exit_code = module.main(
        [
            "--analytics-root", str(analytics), "--data-root", str(data),
            "--outputs-root", str(destination), "--release-id", "P1.3c-20260906",
            "--analytics-sha", analytics_sha, "--data-sha", data_sha,
            "--payload-root", str(tmp_path / "missing-payload"),
        ]
    )

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert result["status"] == "preflight-failed"
    assert len(result["report"]["missing"]) == 9
    assert not (destination / "releases").exists()
