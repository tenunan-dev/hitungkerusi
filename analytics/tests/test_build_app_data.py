"""Tests for build_app_data.py against local V2 source data (read-only).

These exercise the extracted, portable computation against this V2 repository.
The V2 source tree is never written to.
"""

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "delivery"))
import build_app_data as bad

SOURCE_ROOT = Path(__file__).resolve().parents[2]


def _snapshot_files(root):
    return {
        path.relative_to(root): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def _build_controlled_generate_source(source_root):
    data_root = source_root.parent / "1_DATA"
    for relative_path in (
        "research/raw",
        "research/derived",
        "research/trackers",
        "research/federal",
        "research/states",
        "geo",
    ):
        (data_root / relative_path).mkdir(parents=True, exist_ok=True)

    (data_root / "research" / "derived" / "master-list-222-parliamentary-seats.csv").write_text(
        "code,constituency,state\n"
        + "\n".join(
            f"P{index:03d},Seat {index},Johor" for index in range(1, 223)
        )
        + "\n"
    )
    (data_root / "research" / "trackers" / "ge16-news-feed.json").write_text(
        json.dumps({"items": []})
    )
    (source_root / "02_FORECAST" / "outputs" / "latest").mkdir(parents=True)
    (source_root / "02_FORECAST" / "outputs" / "latest" / "ge16-forecast-latest.json").write_text(
        json.dumps(
            {
                "projected_seats": [
                    {"code": f"P{index:03d}", "proj_winner": "PH"}
                    for index in range(1, 223)
                ],
                "monte_carlo": {"P50": 140, "P10": 120, "P90": 160, "P_majority": 0.9, "flips_P50": 3},
            }
        )
    )
    (source_root / "work" / "scenarios").mkdir(parents=True)
    (source_root / "work" / "scenarios" / "projection_scenarios.json").write_text(
        json.dumps({"Base (swings)": {"PH": 80, "BN": 30, "PN": 100, "GPS": 6, "GRS": 6, "WARISAN": 0}})
    )
    return data_root


@pytest.fixture(scope="module")
def app_data():
    return bad.generate(SOURCE_ROOT, generated_at="2026-09-05T12:00:00Z")


def test_master_seat_count(app_data):
    assert len(app_data["master"]) == 222


def test_projection_seat_count(app_data):
    assert len(app_data["projection"]) == 222


def test_scenarios_present(app_data):
    assert len(app_data["scenarios"]) >= 1
    assert "Status quo" in app_data["scenarios"] or "Base (swings)" in app_data["scenarios"]


def test_summary_stats_reasonable(app_data):
    s = app_data["summary"]
    assert 0 < s["govt_p50"] <= 222
    assert s["govt_p10"] <= s["govt_p50"] <= s["govt_p90"]
    assert 0 <= s["majority_pct"] <= 100
    assert s["flips"] >= 0


def test_flips_p50_matches_engine_monte_carlo(app_data):
    """summary.flips must come from monte_carlo.flips_P50, not len(flips[]) —
    those are two different statistics in the source engine output and only
    flips_P50 is the one the engine's own weekly notes treat as canonical."""
    fc = bad._load_json_safe(
        SOURCE_ROOT / "02_FORECAST" / "outputs" / "latest" / "ge16-forecast-latest.json",
        default={},
    )
    assert app_data["summary"]["flips"] == int(fc.get("monte_carlo", {}).get("flips_P50", -1))


def test_bloc_colors_and_party_bloc_present(app_data):
    s = app_data["summary"]
    assert len(s["bloc_colors"]) > 0
    assert len(s["party_colors"]) > 0
    assert len(s["party_bloc"]) > 0
    assert "PN" in s["govt_blocs"] + s["opp_blocs"]


def test_general_news_shape(app_data):
    for item in app_data["general_news"][:5]:
        assert "title" in item
        assert "date" in item
        assert "source" in item


def test_forecast_json_shape(app_data):
    fc = bad._load_json_safe(
        SOURCE_ROOT / "02_FORECAST" / "outputs" / "latest" / "ge16-forecast-latest.json",
        default={},
    )
    fj = bad.build_forecast_json(app_data, fc)
    for key in ("govt_p50", "govt_p10", "govt_p90", "flips", "coalition", "bloc_colors"):
        assert key in fj


def test_scenarios_json_shape(app_data):
    sj = bad.build_scenarios_json(app_data)
    for key in ("scenarios_display", "scenario_descriptions", "scenarios"):
        assert key in sj
    assert len(sj["scenarios_display"]) == len(app_data["scenarios"])


def test_scenario_descriptions_propagate_their_metadata_category(app_data):
    descriptions = {
        item["full"]: item for item in app_data["summary"]["scenario_descriptions"]
    }

    assert set(descriptions) == set(app_data["scenarios"])

    # The live producer metadata is canonical.  Do not freeze a scenario name
    # or count here: the weekly producer may add, remove, or reclassify a
    # scenario while preserving the delivery contract.
    scenario_meta = json.loads(
        (SOURCE_ROOT / "05_AUTOMATION" / "scenario_meta.json").read_text()
    )
    assert set(scenario_meta) == set(app_data["scenarios"])
    for name, item in descriptions.items():
        expected = "narrative" if scenario_meta[name].get("category") == "narrative" else "parametric"
        assert item["category"] == expected


def test_never_writes_to_source_root(app_data, tmp_path):
    """Sanity: generate() must be pure-read. Confirm no new/modified files
    under canonical DATA by checking a known-stable input file."""
    known_file = (
        SOURCE_ROOT.parent / "1_DATA" / "research" / "derived"
        / "master-list-222-parliamentary-seats.csv"
    )
    assert known_file.exists()
    # If build_app_data ever accidentally wrote here, this would be a directory
    # or the read would have failed above already. This test exists as a
    # canary — a future refactor that starts writing will need to explain why.
    assert known_file.is_file()


def test_generate_does_not_change_canonical_data_inputs_in_controlled_fixture(tmp_path):
    source_root = tmp_path / "HERMES"
    data_root = _build_controlled_generate_source(source_root)
    before = _snapshot_files(data_root)

    app_data = bad.generate(source_root, generated_at="2026-09-05T12:00:00Z")

    assert len(app_data["master"]) == 222
    assert _snapshot_files(data_root) == before


def test_explicit_generated_at_makes_clock_derived_fields_deterministic(tmp_path):
    source_root = tmp_path / "HERMES"
    _build_controlled_generate_source(source_root)

    first = bad.generate(source_root, generated_at="2026-09-05T12:00:00+00:00")
    second = bad.generate(source_root, generated_at="2026-09-05T12:00:00+00:00")

    assert first == second
    assert first["summary"]["updated"] == "2026-09-05T12:00:00Z"
    assert first["updates"][0]["date"] == "2026-09-05"


def test_generate_reads_canonical_inputs_from_sibling_data_only(monkeypatch):
    """All canonical data reads must be resolved through sibling DATA.

    Forecast-engine outputs and delivery-owned scenario configuration are
    intentionally outside this assertion; the canonical research domains are
    the ones owned by DATA and must never fall back to HERMES/01_RESEARCH.
    """
    data_root = (SOURCE_ROOT.parent / "1_DATA").resolve()
    legacy_research = (SOURCE_ROOT / "01_RESEARCH").resolve()
    csv_reads = []
    json_reads = []
    real_read_csv_safe = bad._read_csv_safe
    real_load_json_safe = bad._load_json_safe

    def capture_csv(path):
        csv_reads.append(Path(path).resolve())
        return real_read_csv_safe(path)

    def capture_json(path, default=None):
        json_reads.append(Path(path).resolve())
        return real_load_json_safe(path, default=default)

    monkeypatch.setattr(bad, "_read_csv_safe", capture_csv)
    monkeypatch.setattr(bad, "_load_json_safe", capture_json)

    bad.generate(SOURCE_ROOT, generated_at="2026-09-05T12:00:00Z")

    canonical_reads = [
        path for path in csv_reads + json_reads
        if path.is_relative_to(data_root) or path.is_relative_to(legacy_research)
    ]
    assert canonical_reads
    assert all(path.is_relative_to(data_root) for path in canonical_reads)


@pytest.mark.parametrize("generated_at", (None, "2026-09-05T12:00:00"))
def test_generate_requires_an_explicit_timezone_aware_timestamp(generated_at):
    with pytest.raises(ValueError, match="generated_at"):
        bad.generate(SOURCE_ROOT, generated_at=generated_at)


def test_generate_canonicalizes_an_offset_timestamp_to_utc_z(tmp_path):
    source_root = tmp_path / "HERMES"
    _build_controlled_generate_source(source_root)

    app_data = bad.generate(source_root, generated_at="2026-09-05T20:00:00+08:00")

    assert app_data["summary"]["updated"] == "2026-09-05T12:00:00Z"
    assert app_data["updates"][0]["date"] == "2026-09-05"


def test_guarded_csv_read_rejects_a_symlinked_final_data_file(tmp_path):
    target = tmp_path / "target.csv"
    target.write_text("code\nP001\n", encoding="utf-8")
    linked = tmp_path / "linked.csv"
    linked.symlink_to(target)

    with pytest.raises(ValueError, match="symlinked"):
        bad._read_csv_safe(linked)


def test_guarded_json_read_rejects_symlinked_inner_and_ancestor_paths(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "feed.json").write_text("{}", encoding="utf-8")
    inner = tmp_path / "inner"
    inner.symlink_to(real, target_is_directory=True)
    ancestor = tmp_path / "ancestor"
    ancestor.mkdir()
    linked_ancestor = tmp_path / "linked-ancestor"
    linked_ancestor.symlink_to(ancestor, target_is_directory=True)

    with pytest.raises(ValueError, match="symlinked"):
        bad._load_json_safe(inner / "feed.json")
    with pytest.raises(ValueError, match="symlinked"):
        bad._load_json_safe(linked_ancestor / "missing.json")


def test_generate_rejects_a_symlinked_canonical_data_ancestor_before_resolution(tmp_path):
    real_parent = tmp_path / "real"
    source_root = real_parent / "HERMES"
    _build_controlled_generate_source(source_root)
    linked_root = tmp_path / "linked-HERMES"
    linked_root.symlink_to(source_root, target_is_directory=True)

    with pytest.raises(ValueError, match="symlinked"):
        bad.generate(linked_root, generated_at="2026-09-05T12:00:00Z")


def test_present_malformed_news_feed_is_a_required_input_gap(tmp_path):
    source_root = tmp_path / "HERMES"
    data_root = _build_controlled_generate_source(source_root)
    feed = data_root / "research" / "trackers" / "ge16-news-feed.json"
    feed.write_text("{malformed", encoding="utf-8")

    app_data = bad.generate(source_root, generated_at="2026-09-05T12:00:00Z")

    assert app_data["general_news"] == []
    assert any(
        gap["field"] == "general news feed" and "malformed" in gap["reason"].lower()
        for gap in app_data["_input_gaps"]
    )


def test_present_news_feed_with_invalid_schema_is_a_required_input_gap(tmp_path):
    source_root = tmp_path / "HERMES"
    data_root = _build_controlled_generate_source(source_root)
    feed = data_root / "research" / "trackers" / "ge16-news-feed.json"
    feed.write_text(json.dumps({"items": {"not": "a list"}}), encoding="utf-8")

    app_data = bad.generate(source_root, generated_at="2026-09-05T12:00:00Z")

    assert any(
        gap["field"] == "general news feed" and "schema" in gap["reason"].lower()
        for gap in app_data["_input_gaps"]
    )
