"""TDD coverage for the forecast engine's canonical DATA cutover."""

import importlib
import sys
from pathlib import Path
from typing import Optional

import pandas as pd
import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ENGINE_ROOT = REPOSITORY_ROOT / "02_FORECAST" / "engine"
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))


def _make_data_root(root: Path, missing: Optional[str] = None) -> Path:
    """Create the complete canonical layout, optionally omitting one mapping."""
    from data_roots import CANONICAL_DATA_PATHS

    data_root = root / "1_DATA"
    for key, relative_path in CANONICAL_DATA_PATHS.items():
        if key != missing:
            (data_root / relative_path).mkdir(parents=True, exist_ok=True)
    return data_root


@pytest.mark.parametrize("repository_name", ["2_ANALYTICS", "arbitrary-analytics-root"])
def test_repository_root_resolution_is_basename_independent(tmp_path, repository_name):
    from data_roots import resolve_repository_root

    engine_file = tmp_path / repository_name / "02_FORECAST" / "engine" / "data_roots.py"
    engine_file.parent.mkdir(parents=True)
    engine_file.touch()

    assert resolve_repository_root(anchor=engine_file) == (tmp_path / repository_name).resolve()


@pytest.mark.parametrize("repository_name", ["2_ANALYTICS", "arbitrary-analytics-root"])
def test_resolver_finds_sibling_data_root_and_all_canonical_mappings(tmp_path, repository_name):
    from data_roots import CANONICAL_DATA_PATHS, resolve_data_roots

    repository_root = tmp_path / repository_name
    repository_root.mkdir()
    data_root = _make_data_root(tmp_path)

    roots = resolve_data_roots(repository_root=repository_root)

    assert roots.repository_root == repository_root.resolve()
    assert roots.data_root == data_root.resolve()
    for key, relative_path in CANONICAL_DATA_PATHS.items():
        assert roots.path(key) == data_root / relative_path


@pytest.mark.parametrize("repository_name", ["2_ANALYTICS", "arbitrary-analytics-root"])
def test_resolver_rejects_absent_data_root(tmp_path, repository_name):
    from data_roots import DataRootResolutionError, resolve_data_roots

    repository_root = tmp_path / repository_name
    repository_root.mkdir()

    with pytest.raises(DataRootResolutionError, match="Canonical DATA root is missing"):
        resolve_data_roots(repository_root=repository_root)


@pytest.mark.parametrize("repository_name", ["2_ANALYTICS", "arbitrary-analytics-root"])
def test_resolver_rejects_absent_mapped_path(tmp_path, repository_name):
    from data_roots import DataRootResolutionError, resolve_data_roots

    repository_root = tmp_path / repository_name
    repository_root.mkdir()
    _make_data_root(tmp_path, missing="trackers")

    with pytest.raises(DataRootResolutionError, match="Canonical DATA mapping 'trackers' is missing"):
        resolve_data_roots(repository_root=repository_root)


def test_forecast_runtime_source_reads_use_data_root_not_hermes_research(monkeypatch):
    """Exercise every forecast-model loader without touching output writers."""
    import forecast_engine

    forecast_engine = importlib.reload(forecast_engine)
    read_paths = []

    def fake_read_csv(path, *args, **kwargs):
        read_paths.append(Path(path).resolve())
        name = Path(path).name
        if name == "ge16-projection-model.csv":
            return pd.DataFrame(
                [{"code": "P001", "winner_ge15": "PH", "runnerup": "PN", "margin_pct_ge15": 3.0}]
            )
        if name == "ge15-results-by-constituency-full.csv":
            return pd.DataFrame([{"code": "P001", "state_std": "Johor", "constituency": "Test"}])
        if name == "voter-demographics-by-constituency-ge15.csv":
            return pd.DataFrame(
                [{"code": "P001", "malay_pct": 50, "chinese_pct": 20, "indian_pct": 10,
                  "bumi_sabah_pct": 0, "bumi_sarawak_pct": 0, "age18_21_pct": 5, "age22_30_pct": 10}]
            )
        if name == "swing_se_to_se.csv":
            return pd.DataFrame([{"state": "Johor", "bloc": "PH", "swing_pp": 1.0}])
        if name == "swing_boundary_grouped.csv":
            return pd.DataFrame([{"parliament": "P001", "bloc": "PH", "boundary_swing_pp": 1.0}])
        raise AssertionError(f"unexpected data read: {path}")

    monkeypatch.setattr(forecast_engine.pd, "read_csv", fake_read_csv)

    forecast_engine.load_baseline()
    forecast_engine.load_state_swings()
    forecast_engine.load_boundary_swings()

    data_root = (REPOSITORY_ROOT.parent / "1_DATA").resolve()
    hermes_research = (REPOSITORY_ROOT / "01_RESEARCH").resolve()
    assert read_paths
    assert all(path.is_relative_to(data_root) for path in read_paths)
    assert not any(path.is_relative_to(hermes_research) for path in read_paths)


def test_report_builder_runtime_inputs_use_canonical_data_domains():
    """The federal report builder binds every data input to sibling DATA."""
    import report_builder

    report_builder = importlib.reload(report_builder)
    roots = report_builder.DATA_ROOTS

    assert Path(report_builder.SWINGS) == roots.derived / "swing_se_to_se.csv"
    assert Path(report_builder.MASTER) == roots.derived / "master-list-222-parliamentary-seats.csv"
    assert Path(report_builder.DEMOG) == roots.derived / "voter-demographics-by-constituency-ge15.csv"
    assert Path(report_builder.GE15) == roots.derived / "ge15-results-by-constituency-full.csv"
    assert Path(report_builder.BATTLEGROUNDS) == roots.federal / "ge16-battleground-seats-master.csv"
    assert report_builder.TRACKER_LOGS == {
        "poll": roots.trackers / "ge16-poll-tracker-log.md",
        "candidate": roots.trackers / "ge16-candidate-tracker-log.md",
        "general-news": roots.trackers / "ge16-general-news-log.md",
    }
    assert report_builder.NEWS_FEED == roots.trackers / "ge16-news-feed.json"


def test_state_report_builder_runtime_research_inputs_use_sibling_data(monkeypatch):
    """State reporting must never read its canonical research inputs from HERMES."""
    import state_report_builder

    state_report_builder = importlib.reload(state_report_builder)
    roots = state_report_builder.DATA_ROOTS

    assert Path(state_report_builder.STATES_DIR) == roots.states
    assert Path(state_report_builder.DERIVED) == roots.derived
    assert Path(state_report_builder.MASTER_222) == (
        roots.derived / "master-list-222-parliamentary-seats.csv"
    )
    assert Path(state_report_builder.GE15_FULL) == (
        roots.derived / "ge15-results-by-constituency-full.csv"
    )
    assert Path(state_report_builder.DEMOG) == (
        roots.derived / "voter-demographics-by-constituency-ge15.csv"
    )
    assert Path(state_report_builder.SWINGS) == roots.derived / "swing_se_to_se.csv"
    assert Path(state_report_builder.NATIONAL_COMP) == (
        roots.states / "national-composition-summary.md"
    )
    assert Path(state_report_builder.BATTLEGROUNDS_MASTER) == (
        roots.federal / "ge16-battleground-seats-master.csv"
    )
    assert Path(state_report_builder.THEORY).is_relative_to(roots.data_root)
    assert Path(state_report_builder.FACTOR_RANKINGS).is_relative_to(roots.data_root)
    assert Path(state_report_builder.PARTY_LANDSCAPE).is_relative_to(roots.data_root)

    fingerprint_paths = []
    monkeypatch.setattr(
        state_report_builder,
        "compute_file_hash",
        lambda path: fingerprint_paths.append(Path(path)) or None,
    )
    state_report_builder.compute_state_fingerprints("Melaka")

    assert roots.federal / "ge16-battleground-seats-master.csv" in fingerprint_paths
    assert Path(state_report_builder.FACTOR_RANKINGS) in fingerprint_paths
    assert Path(state_report_builder.PARTY_LANDSCAPE) in fingerprint_paths
    assert all(
        path.is_relative_to(roots.data_root)
        for path in fingerprint_paths
        if path.name not in {"ge16-forecast-latest.json", "projection_scenarios.json"}
    )

    tracker_reads = []
    monkeypatch.setattr(state_report_builder, "read_text", lambda path: tracker_reads.append(Path(path)) or "")
    state_report_builder.state_signals_from_trackers("Johor")

    assert tracker_reads
    assert all(path.parent == roots.trackers for path in tracker_reads)
    assert all(path.is_relative_to(roots.data_root) for path in tracker_reads)
    assert not any(path.is_relative_to(REPOSITORY_ROOT / "01_RESEARCH") for path in tracker_reads)
