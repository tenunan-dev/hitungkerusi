import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "outputs" / "build_release_payloads.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("build_release_payloads", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _git(arguments, cwd):
    return subprocess.run(
        ["git", *arguments], cwd=str(cwd), check=True, text=True, capture_output=True
    ).stdout.strip()


def _repository(path):
    path.mkdir()
    _git(["init"], path)
    _git(["config", "user.email", "payload-test@example.invalid"], path)
    _git(["config", "user.name", "Payload test"], path)
    (path / ".keep").write_text("fixture\n", encoding="utf-8")
    _git(["add", ".keep"], path)
    _git(["commit", "-m", "fixture"], path)


def _write_state_inputs(data_root, *, narrative=False):
    states = {
        "Melaka": ("melaka", 28, {"BN": 21, "PH": 5, "PN": 2}),
        "Pahang": ("pahang", 42, {"BN": 17, "PH": 8, "PN": 17}),
        "Perak": ("perak", 59, {"BN": 9, "PH": 24, "PN": 26}),
        "Perlis": ("perlis", 15, {"PN": 14, "PH": 1}),
        "Sarawak": ("sarawak", 82, {"GPS": 76, "PSB": 4, "PH": 2}),
    }
    for state, (slug, total, blocs) in states.items():
        root = data_root / "research" / "states" / ("DUN " + state)
        root.mkdir(parents=True, exist_ok=True)
        rows = []
        index = 0
        for bloc, count in blocs.items():
            for _ in range(count):
                index += 1
                rows.append("N.%02d,Member %d,%s,%s,%s" % (index, index, bloc, bloc, state))
        (root / "dun-election-results-latest.csv").write_text(
            "seat,winner,winner_party,winner_bloc,state\n" + "\n".join(rows) + "\n",
            encoding="utf-8",
        )
        results_path = root / "dun-election-results-latest.csv"
        scenario = {
            "scenario": "Measured baseline",
            "category": "narrative" if narrative and slug == "melaka" else "parametric",
            "seats_total": total,
            "flips": 0,
            "provenance": (
                {"derivation": "news-narrative", "evidence": [{
                    "repository": "DATA", "path": "research/trackers/ge16-news-feed.json",
                    "sha256": "0" * 64, "record_id": "feed-1", "record_sha256": "0" * 64,
                }]}
                if narrative and slug == "melaka"
                else {"derivation": "quantitative-scenario", "evidence": [{
                    "repository": "DATA",
                    "path": "research/states/DUN %s/dun-election-results-latest.csv" % state,
                    "sha256": hashlib.sha256(results_path.read_bytes()).hexdigest(),
                }]}
            ),
        }
        scenario.update(blocs)
        (root / (slug + "-state-scenarios.json")).write_text(
            json.dumps({"state": state, "generated_at": "2026-09-05T00:00:00+00:00", "scenarios": [scenario]}),
            encoding="utf-8",
        )


def _workspace(tmp_path):
    parent = tmp_path / "renamed-workspace"
    parent.mkdir()
    analytics = parent / "2_ANALYTICS"
    data = parent / "1_DATA"
    _repository(analytics)
    _repository(data)
    forecast_path = analytics / "02_FORECAST" / "outputs" / "latest" / "ge16-forecast-latest.json"
    forecast_path.parent.mkdir(parents=True)
    forecast_path.write_text(json.dumps(_forecast()), encoding="utf-8")
    (analytics / "work" / "scenarios").mkdir(parents=True)
    (analytics / "work" / "scenarios" / "scenario_meta.json").write_text("{}", encoding="utf-8")
    (data / "canonical-data-provenance.json").write_text(
        json.dumps({"schema": "data.canonical-provenance.v2", "files": []}), encoding="utf-8"
    )
    _write_state_inputs(data)
    (data / "research" / "trackers").mkdir(parents=True)
    (data / "research" / "trackers" / "ge16-news-feed.json").write_text(
        json.dumps({"items": [{"id": "feed-1", "title": "Evidence", "date": "2026-09-05"}]}),
        encoding="utf-8",
    )
    (analytics / "04_SOCIAL").mkdir()
    (analytics / "04_SOCIAL" / "current.md").write_text("Federal source\n", encoding="utf-8")
    for state in ("Melaka", "Pahang", "Perak", "Perlis", "Sarawak"):
        path = analytics / "04_SOCIAL" / "state" / state / "current.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(state + " source\n", encoding="utf-8")
    _git(["add", "."], data)
    _git(["commit", "-m", "canonical fixture inputs"], data)
    return analytics, data


class _AppData:
    @staticmethod
    def generate(_analytics_root, generated_at=None):
        assert generated_at in {"2026-09-05T12:00:00+00:00", "2026-09-05T12:00:00Z"}
        scenarios = {
            "Measured baseline": {
                "PH": 100, "PN": 80, "BN": 20, "GPS": 10, "GRS": 5, "WARISAN": 4,
                "IND": 3,
            }
        }
        return {
            "master": [{"code": "P%03d" % index} for index in range(1, 223)],
            "projection": [{"code": "P%03d" % index, "proj_winner": "PH"} for index in range(1, 223)],
            "scenarios": scenarios, "history": [], "general_news": [], "dun_national": [],
            "dun_schedule": [], "state_proj": {},
            "summary": {"updated": generated_at, "govt_p50": 120, "govt_p10": 110,
                        "govt_p90": 130, "majority_pct": 80, "flips": 1, "econ_term": 0,
                        "coalition": {}, "coalition_text": "", "govt_blocs": [], "opp_blocs": [],
                        "bloc_colors": {}, "party_colors": {}, "party_bloc": {}, "flips_list": [],
                        "tight_seats": []},
        }

    @staticmethod
    def build_forecast_json(app_data, _forecast):
        return {"updated": app_data["summary"]["updated"], "govt_p50": 120, "govt_p10": 110,
                "govt_p90": 130, "majority_pct": 80, "flips": 1, "coalition": {},
                "econ_term": 0, "coalition_text": "",
                "govt_blocs": [], "opp_blocs": [], "bloc_colors": {}, "party_colors": {},
                "party_bloc": {}, "history": [], "flips_list": [], "tight_seats": []}

    @staticmethod
    def build_scenarios_json(app_data):
        return {"scenarios": app_data["scenarios"], "scenarios_display": [],
                "scenario_descriptions": [
                    {"full": name, "category": "parametric"}
                    for name in app_data["scenarios"]
                ], "scenario_deltas": []}


def _forecast():
    return {
        "generated": "2026-09-05T00:00:00+00:00",
        "deterministic": {"PH": 100, "PN": 80, "BN": 20, "GPS": 10, "GRS": 5,
                          "WARISAN": 4, "IND": 3},
        "projected_seats": [{"code": "P001", "proj_winner": "PH"}],
    }


def _metadata(analytics):
    return {
        "Measured baseline": {
            "category": "parametric",
            "provenance": {"derivation": "quantitative-scenario", "evidence": [{
                "repository": "ANALYTICS", "path": "02_FORECAST/outputs/latest/ge16-forecast-latest.json",
                "sha256": hashlib.sha256((analytics / "02_FORECAST" / "outputs" / "latest" / "ge16-forecast-latest.json").read_bytes()).hexdigest(),
            }]},
        }
    }


def _record_sha256(record):
    return hashlib.sha256(
        (json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    ).hexdigest()


def _narrative_metadata(data, record_id, record_sha256=None):
    feed_path = data / "research" / "trackers" / "ge16-news-feed.json"
    feed_sha256 = hashlib.sha256(feed_path.read_bytes()).hexdigest()
    if record_sha256 is None:
        record = next(item for item in json.loads(feed_path.read_text(encoding="utf-8"))["items"]
                      if item["id"] == record_id)
        record_sha256 = _record_sha256(record)
    return {
        "BN-PN federal pact (N9 template to GE16)": {
            "category": "narrative",
            "provenance": {"derivation": "news-narrative", "evidence": [{
                "repository": "DATA", "path": "research/trackers/ge16-news-feed.json",
                "sha256": feed_sha256, "record_id": record_id,
                "record_sha256": record_sha256,
            }]},
        }
    }


class _NarrativeAppData(_AppData):
    @staticmethod
    def generate(analytics_root, generated_at=None):
        app_data = _AppData.generate(analytics_root, generated_at=generated_at)
        app_data["scenarios"] = {
            "BN-PN federal pact (N9 template to GE16)": app_data["scenarios"]["Measured baseline"]
        }
        return app_data

    @staticmethod
    def build_scenarios_json(app_data):
        return {"scenarios": app_data["scenarios"], "scenarios_display": [],
                "scenario_descriptions": [{
                    "full": "BN-PN federal pact (N9 template to GE16)", "category": "narrative",
                }], "scenario_deltas": []}


def test_federal_scenario_metadata_requires_category(tmp_path, monkeypatch):
    module = _load_module()
    analytics, data = _workspace(tmp_path)
    monkeypatch.setattr(module, "_load_app_data_module", lambda: _AppData)
    monkeypatch.setattr(module, "_load_forecast", lambda _root: _forecast())
    metadata = _metadata(analytics)
    metadata["Measured baseline"].pop("category")
    monkeypatch.setattr(module, "_federal_metadata", lambda _root: metadata)
    root = tmp_path / "payload"

    with pytest.raises(module.PayloadError, match="category"):
        module.build_payloads(analytics, data, root, "2026-09-05T12:00:00+00:00")
    assert not root.exists()


def test_builds_federal_narrative_with_record_bound_canonical_evidence(tmp_path, monkeypatch):
    module = _load_module()
    analytics, data = _workspace(tmp_path)
    record = {
        "id": "https://example.invalid/umno-election-talks",
        "link": "https://example.invalid/umno-election-talks",
        "title": "UMNO state leaders cleared for election talks",
        "date": "2026-09-05",
    }
    feed_path = data / "research" / "trackers" / "ge16-news-feed.json"
    feed_path.write_text(json.dumps({"items": [record]}), encoding="utf-8")
    _git(["add", "."], data)
    _git(["commit", "-m", "record-bound narrative evidence"], data)
    monkeypatch.setattr(module, "_load_app_data_module", lambda: _NarrativeAppData)
    monkeypatch.setattr(module, "_load_forecast", lambda _root: _forecast())
    monkeypatch.setattr(module, "_federal_metadata", lambda _root: _narrative_metadata(data, record["id"]))
    root = tmp_path / "payload"

    module.build_payloads(analytics, data, root, "2026-09-05T12:00:00+00:00")

    scenario = json.loads((root / "scenarios.json").read_text(encoding="utf-8"))
    description = scenario["scenario_descriptions"][0]
    evidence = scenario["scenario_provenance"][description["full"]]["evidence"][0]
    assert description["category"] == "narrative"
    assert evidence["record_id"] == record["link"]
    assert evidence["record_sha256"] == _record_sha256(record)
    module.validate_payload_root(root, analytics, data)


@pytest.mark.parametrize(
    ("record_id", "record_sha256"),
    (("https://example.invalid/unknown", "0" * 64),
     ("https://example.invalid/umno-election-talks", "0" * 64)),
)
def test_federal_narrative_rejects_wrong_record_or_hash(tmp_path, monkeypatch, record_id, record_sha256):
    module = _load_module()
    analytics, data = _workspace(tmp_path)
    record = {
        "id": "https://example.invalid/umno-election-talks",
        "link": "https://example.invalid/umno-election-talks",
        "title": "UMNO state leaders cleared for election talks",
        "date": "2026-09-05",
    }
    feed_path = data / "research" / "trackers" / "ge16-news-feed.json"
    feed_path.write_text(json.dumps({"items": [record]}), encoding="utf-8")
    _git(["add", "."], data)
    _git(["commit", "-m", "record-bound narrative evidence"], data)
    monkeypatch.setattr(module, "_load_app_data_module", lambda: _NarrativeAppData)
    monkeypatch.setattr(module, "_load_forecast", lambda _root: _forecast())
    monkeypatch.setattr(
        module, "_federal_metadata",
        lambda _root: _narrative_metadata(data, record_id, record_sha256),
    )
    root = tmp_path / "payload"

    with pytest.raises(module.PayloadError, match="unknown feed record ID|canonical input content"):
        module.build_payloads(analytics, data, root, "2026-09-05T12:00:00+00:00")
    assert not root.exists()


def test_feed_record_resolves_by_link_when_the_item_carries_no_id(tmp_path):
    """The live Stage-1 feed carries no ``id``: a record is identified by its link."""
    module = _load_module()
    _analytics, data = _workspace(tmp_path)
    record = {"link": "https://example.invalid/news/no-id-item", "title": "No id item",
              "date": "2026-09-05"}
    feed_path = data / "research" / "trackers" / "ge16-news-feed.json"
    feed_path.write_text(json.dumps({"items": [record]}), encoding="utf-8")

    evidence = module._canonical_evidence("DATA", data, module.NEWS_FEED_RELATIVE, record["link"])

    assert evidence == {
        "repository": "DATA", "path": module.NEWS_FEED_RELATIVE,
        "sha256": hashlib.sha256(feed_path.read_bytes()).hexdigest(),
        "record_id": record["link"], "record_sha256": _record_sha256(record),
    }


def test_narrative_binding_the_feed_file_digest_is_rejected_by_name(tmp_path):
    """File-level feed evidence is not reviewable provenance: it dies each cycle."""
    module = _load_module()
    analytics, data = _workspace(tmp_path)
    provenance = {"derivation": "news-narrative", "evidence": [
        module._canonical_evidence("DATA", data, module.NEWS_FEED_RELATIVE)]}

    with pytest.raises(module.PayloadError) as failure:
        module._validate_provenance(provenance, "narrative",
                                    "federal scenario %r" % "BN-PN federal pact", analytics, data)
    message = str(failure.value)
    assert "BN-PN federal pact" in message and "record_sha256" in message


def test_narrative_requires_every_feed_evidence_item_to_be_record_bound(tmp_path):
    """One record-bound item does not excuse a sibling that binds the file."""
    module = _load_module()
    analytics, data = _workspace(tmp_path)
    provenance = {"derivation": "news-narrative", "evidence": [
        module._canonical_evidence("DATA", data, module.NEWS_FEED_RELATIVE, "feed-1"),
        module._canonical_evidence("DATA", data, module.NEWS_FEED_RELATIVE),
    ]}

    with pytest.raises(module.PayloadError) as failure:
        module._validate_provenance(provenance, "narrative", "federal scenario 'two records'",
                                    analytics, data)
    assert "two records" in str(failure.value)


def test_narrative_unknown_record_names_scenario_and_record(tmp_path):
    module = _load_module()
    analytics, data = _workspace(tmp_path)
    missing = "https://example.invalid/news/evicted"
    provenance = {"derivation": "news-narrative", "evidence": [
        {"repository": "DATA", "path": module.NEWS_FEED_RELATIVE, "sha256": "0" * 64,
         "record_id": missing, "record_sha256": "0" * 64}]}

    with pytest.raises(module.PayloadError) as failure:
        module._validate_provenance(provenance, "narrative", "federal scenario 'evicted record'",
                                    analytics, data)
    message = str(failure.value)
    assert "unknown feed record ID" in message and missing in message
    assert "evicted record" in message


def test_rejects_free_text_provenance_and_requires_bound_evidence(tmp_path):
    module = _load_module()
    analytics, data = _workspace(tmp_path)
    provenance = {"kind": "measured", "source": "handwritten claim"}

    with pytest.raises(module.PayloadError, match="derivation|evidence|provenance"):
        module._validate_provenance(
            provenance, "parametric", "test scenario", analytics, data
        )


def test_payload_root_validator_rejects_cross_file_inconsistency_and_symlinks(tmp_path):
    module = _load_module()
    root = tmp_path / "payload"
    root.mkdir()
    for name in module.PAYLOAD_NAMES:
        (root / name).write_text("{}", encoding="utf-8")

    with pytest.raises(module.PayloadError, match="schema|generated_at|provenance"):
        module.validate_payload_root(root, tmp_path / "2_ANALYTICS", tmp_path / "1_DATA")

    (root / "extra.json").write_text("{}", encoding="utf-8")
    with pytest.raises(module.PayloadError, match="exactly"):
        module.validate_payload_root(root, tmp_path / "2_ANALYTICS", tmp_path / "1_DATA")


def test_payload_root_requires_roots_and_rejects_fabricated_evidence_hashes(tmp_path, monkeypatch):
    module = _load_module()
    analytics, data = _workspace(tmp_path)
    monkeypatch.setattr(module, "_load_app_data_module", lambda: _AppData)
    monkeypatch.setattr(module, "_load_forecast", lambda _root: _forecast())
    monkeypatch.setattr(module, "_federal_metadata", _metadata)
    root = tmp_path / "payload"
    module.build_payloads(analytics, data, root, "2026-09-05T12:00:00+00:00")

    with pytest.raises(TypeError):
        module.validate_payload_root(root)

    app_path = root / "app-data.json"
    app = json.loads(app_path.read_text(encoding="utf-8"))
    app["provenance"]["evidence"][0]["sha256"] = "0" * 64
    app_path.write_text(json.dumps(app), encoding="utf-8")
    with pytest.raises(module.PayloadError, match="canonical input content"):
        module.validate_payload_root(root, analytics, data)


@pytest.mark.parametrize(
    "forecast_key",
    ("coalition", "history", "govt_blocs", "opp_blocs", "bloc_colors", "party_colors",
     "party_bloc", "flips_list", "tight_seats"),
)
def test_payload_root_rejects_every_forecast_app_data_duplicate_mismatch(tmp_path, monkeypatch, forecast_key):
    module = _load_module()
    analytics, data = _workspace(tmp_path)
    monkeypatch.setattr(module, "_load_app_data_module", lambda: _AppData)
    monkeypatch.setattr(module, "_load_forecast", lambda _root: _forecast())
    monkeypatch.setattr(module, "_federal_metadata", _metadata)
    root = tmp_path / "payload"
    module.build_payloads(analytics, data, root, "2026-09-05T12:00:00+00:00")
    forecast_path = root / "forecast.json"
    forecast = json.loads(forecast_path.read_text(encoding="utf-8"))
    forecast[forecast_key] = {"fabricated": True} if isinstance(forecast[forecast_key], dict) else ["fabricated"]
    forecast_path.write_text(json.dumps(forecast), encoding="utf-8")

    with pytest.raises(module.PayloadError, match="forecast and app-data"):
        module.validate_payload_root(root, analytics, data)


@pytest.mark.parametrize("forecast_key", ("econ_term", "coalition_text"))
def test_payload_root_requires_forecast_summary_duplicates(tmp_path, monkeypatch, forecast_key):
    module = _load_module()
    analytics, data = _workspace(tmp_path)
    monkeypatch.setattr(module, "_load_app_data_module", lambda: _AppData)
    monkeypatch.setattr(module, "_load_forecast", lambda _root: _forecast())
    monkeypatch.setattr(module, "_federal_metadata", _metadata)
    root = tmp_path / "payload"
    module.build_payloads(analytics, data, root, "2026-09-05T12:00:00+00:00")
    forecast_path = root / "forecast.json"
    forecast = json.loads(forecast_path.read_text(encoding="utf-8"))
    del forecast[forecast_key]
    forecast_path.write_text(json.dumps(forecast), encoding="utf-8")

    with pytest.raises(module.PayloadError, match="forecast schema|forecast and app-data"):
        module.validate_payload_root(root, analytics, data)


@pytest.mark.parametrize(
    ("forecast_key", "adversarial_value"),
    (("econ_term", 999), ("coalition_text", "fabricated coalition")),
)
def test_payload_root_rejects_forecast_summary_duplicate_mismatch(
    tmp_path, monkeypatch, forecast_key, adversarial_value
):
    module = _load_module()
    analytics, data = _workspace(tmp_path)
    monkeypatch.setattr(module, "_load_app_data_module", lambda: _AppData)
    monkeypatch.setattr(module, "_load_forecast", lambda _root: _forecast())
    monkeypatch.setattr(module, "_federal_metadata", _metadata)
    root = tmp_path / "payload"
    module.build_payloads(analytics, data, root, "2026-09-05T12:00:00+00:00")
    forecast_path = root / "forecast.json"
    forecast = json.loads(forecast_path.read_text(encoding="utf-8"))
    forecast[forecast_key] = adversarial_value
    forecast_path.write_text(json.dumps(forecast), encoding="utf-8")

    with pytest.raises(module.PayloadError, match="forecast and app-data"):
        module.validate_payload_root(root, analytics, data)


def test_payload_root_rejects_coordinated_duplicate_mutation_against_canonical_build(
    tmp_path, monkeypatch
):
    module = _load_module()
    analytics, data = _workspace(tmp_path)
    monkeypatch.setattr(module, "_load_app_data_module", lambda: _AppData)
    monkeypatch.setattr(module, "_load_forecast", lambda _root: _forecast())
    monkeypatch.setattr(module, "_federal_metadata", _metadata)
    root = tmp_path / "payload"
    module.build_payloads(analytics, data, root, "2026-09-05T12:00:00+00:00")

    app_path, forecast_path = root / "app-data.json", root / "forecast.json"
    app = json.loads(app_path.read_text(encoding="utf-8"))
    forecast = json.loads(forecast_path.read_text(encoding="utf-8"))
    app["summary"]["coalition_text"] = "coordinated fabrication"
    forecast["coalition_text"] = "coordinated fabrication"
    app_path.write_text(json.dumps(app), encoding="utf-8")
    forecast_path.write_text(json.dumps(forecast), encoding="utf-8")

    with pytest.raises(module.PayloadError, match="deterministic canonical build"):
        module.validate_payload_root(root, analytics, data)


def test_payload_root_rejects_hand_authored_nine_file_root_with_valid_provenance(
    tmp_path, monkeypatch
):
    module = _load_module()
    analytics, data = _workspace(tmp_path)
    monkeypatch.setattr(module, "_load_app_data_module", lambda: _AppData)
    monkeypatch.setattr(module, "_load_forecast", lambda _root: _forecast())
    monkeypatch.setattr(module, "_federal_metadata", _metadata)
    root = tmp_path / "payload"
    module.build_payloads(analytics, data, root, "2026-09-05T12:00:00+00:00")

    # Each document still has its correct role-specific provenance, canonical
    # hashes, generated_at, counts, and cross-file duplicate values.  The
    # arbitrary extra field makes this a coherent hand-authored inventory.
    for path in root.iterdir():
        value = json.loads(path.read_text(encoding="utf-8"))
        value["hand_authored"] = True
        path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(module.PayloadError, match="deterministic canonical build"):
        module.validate_payload_root(root, analytics, data)


@pytest.mark.parametrize("bad_value", (-1, 1.5, True))
def test_state_composition_rejects_invalid_counts_even_when_sums_match(tmp_path, monkeypatch, bad_value):
    module = _load_module()
    analytics, data = _workspace(tmp_path)
    monkeypatch.setattr(module, "_load_app_data_module", lambda: _AppData)
    monkeypatch.setattr(module, "_load_forecast", lambda _root: _forecast())
    monkeypatch.setattr(module, "_federal_metadata", _metadata)
    root = tmp_path / "payload"
    module.build_payloads(analytics, data, root, "2026-09-05T12:00:00+00:00")
    state_path = root / "melaka.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["composition_by_bloc"] = {"BN": bad_value, "PH": 28 - bad_value}
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(module.PayloadError, match="non-negative integers"):
        module.validate_payload_root(root, analytics, data)


def _snapshot(root):
    return {
        (root.name, path.relative_to(root)): (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size,
                                              path.stat().st_mtime_ns)
        for path in root.rglob("*") if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def test_builds_exact_nine_deterministic_payloads_without_mutating_sources(tmp_path, monkeypatch):
    module = _load_module()
    analytics, data = _workspace(tmp_path)
    monkeypatch.setattr(module, "_load_app_data_module", lambda: _AppData)
    monkeypatch.setattr(module, "_load_forecast", lambda _root: _forecast())
    monkeypatch.setattr(module, "_federal_metadata", _metadata)
    before = _snapshot(analytics) | _snapshot(data)
    delivery = analytics.parent / "4_DELIVERY"
    delivery.mkdir()
    original_read_text = Path.read_text

    def reject_delivery_read(path, *args, **kwargs):
        assert delivery not in (path, *path.parents)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", reject_delivery_read)
    one = tmp_path / "payload-one"
    two = tmp_path / "payload-two"

    module.build_payloads(analytics, data, one, "2026-09-05T12:00:00+00:00")
    module.build_payloads(analytics, data, two, "2026-09-05T12:00:00+00:00")

    assert sorted(path.name for path in one.iterdir()) == [
        "app-data.json", "forecast.json", "melaka.json", "pahang.json", "perak.json",
        "perlis.json", "sarawak.json", "scenarios.json", "social-payload.json",
    ]
    assert {path.name: path.read_bytes() for path in one.iterdir()} == {
        path.name: path.read_bytes() for path in two.iterdir()
    }
    assert before == (_snapshot(analytics) | _snapshot(data))
    for path in one.iterdir():
        module.validate_payload_file(
            path.name, json.loads(path.read_text(encoding="utf-8")), analytics, data
        )
    for name, total in (("melaka.json", 28), ("pahang.json", 42), ("perak.json", 59),
                        ("perlis.json", 15), ("sarawak.json", 82)):
        assert json.loads((one / name).read_text(encoding="utf-8"))["total"] == total
    social = json.loads((one / "social-payload.json").read_text(encoding="utf-8"))
    assert [item["path"] for item in social["sources"]] == [
        "04_SOCIAL/current.md", "04_SOCIAL/state/Melaka/current.md",
        "04_SOCIAL/state/Pahang/current.md", "04_SOCIAL/state/Perak/current.md",
        "04_SOCIAL/state/Perlis/current.md", "04_SOCIAL/state/Sarawak/current.md",
    ]


def test_fails_closed_on_undocumented_narrative_or_existing_output_root(tmp_path, monkeypatch):
    module = _load_module()
    analytics, data = _workspace(tmp_path)
    monkeypatch.setattr(module, "_load_app_data_module", lambda: _AppData)
    monkeypatch.setattr(module, "_load_forecast", lambda _root: _forecast())
    monkeypatch.setattr(module, "_federal_metadata", _metadata)
    scenario_path = data / "research" / "states" / "DUN Melaka" / "melaka-state-scenarios.json"
    scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
    scenario["scenarios"][0]["category"] = "narrative"
    scenario["scenarios"][0].pop("provenance")
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
    _git(["add", "."], data)
    _git(["commit", "-m", "invalid but committed fixture"], data)
    payload = tmp_path / "payload"

    with pytest.raises(module.PayloadError, match="narrative"):
        module.build_payloads(analytics, data, payload, "2026-09-05T12:00:00+00:00")
    assert not payload.exists()
    payload.mkdir()
    with pytest.raises(module.PayloadError, match="must not already exist"):
        module.build_payloads(analytics, data, payload, "2026-09-05T12:00:00+00:00")


def test_rejects_dirty_canonical_data_before_creating_payload_root(tmp_path, monkeypatch):
    module = _load_module()
    analytics, data = _workspace(tmp_path)
    monkeypatch.setattr(module, "_load_app_data_module", lambda: _AppData)
    monkeypatch.setattr(module, "_load_forecast", lambda _root: _forecast())
    monkeypatch.setattr(module, "_federal_metadata", _metadata)
    (data / "uncommitted-input.txt").write_text("dirty\n", encoding="utf-8")
    payload = tmp_path / "payload"

    with pytest.raises(module.PayloadError, match="canonical DATA is dirty"):
        module.build_payloads(analytics, data, payload, "2026-09-05T12:00:00+00:00")
    assert not payload.exists()


def test_payload_file_requires_roots_and_rejects_fabricated_evidence_hash(tmp_path, monkeypatch):
    module = _load_module()
    analytics, data = _workspace(tmp_path)
    monkeypatch.setattr(module, "_load_app_data_module", lambda: _AppData)
    monkeypatch.setattr(module, "_load_forecast", lambda _root: _forecast())
    monkeypatch.setattr(module, "_federal_metadata", _metadata)
    root = tmp_path / "payload"
    module.build_payloads(analytics, data, root, "2026-09-05T12:00:00+00:00")
    value = json.loads((root / "forecast.json").read_text(encoding="utf-8"))

    with pytest.raises(TypeError):
        module.validate_payload_file("forecast.json", value)
    with pytest.raises(module.PayloadError, match="roots"):
        module.validate_payload_file("forecast.json", value, None, None)

    value["provenance"]["evidence"][0]["sha256"] = "0" * 64
    with pytest.raises(module.PayloadError, match="canonical input content"):
        module.validate_payload_file("forecast.json", value, analytics, data)


@pytest.mark.parametrize(
    ("payload_name", "wrong_repository", "wrong_path"),
    (
        ("app-data.json", "ANALYTICS", "04_SOCIAL/current.md"),
        ("forecast.json", "ANALYTICS", "work/scenarios/scenario_meta.json"),
        ("scenarios.json", "DATA", "research/trackers/ge16-news-feed.json"),
        ("social-payload.json", "ANALYTICS", "02_FORECAST/outputs/latest/ge16-forecast-latest.json"),
        ("melaka.json", "DATA", "research/states/DUN Pahang/dun-election-results-latest.csv"),
        ("pahang.json", "DATA", "research/states/DUN Perak/dun-election-results-latest.csv"),
        ("perak.json", "DATA", "research/states/DUN Perlis/dun-election-results-latest.csv"),
        ("perlis.json", "DATA", "research/states/DUN Sarawak/dun-election-results-latest.csv"),
        ("sarawak.json", "DATA", "research/states/DUN Melaka/dun-election-results-latest.csv"),
    ),
)
def test_each_payload_role_rejects_valid_wrong_role_canonical_evidence(
    tmp_path, monkeypatch, payload_name, wrong_repository, wrong_path
):
    module = _load_module()
    analytics, data = _workspace(tmp_path)
    monkeypatch.setattr(module, "_load_app_data_module", lambda: _AppData)
    monkeypatch.setattr(module, "_load_forecast", lambda _root: _forecast())
    monkeypatch.setattr(module, "_federal_metadata", _metadata)
    root = tmp_path / "payload"
    module.build_payloads(analytics, data, root, "2026-09-05T12:00:00+00:00")
    payload_path = root / payload_name
    value = json.loads(payload_path.read_text(encoding="utf-8"))
    canonical_root = analytics if wrong_repository == "ANALYTICS" else data
    value["provenance"]["evidence"] = [
        module._canonical_evidence(wrong_repository, canonical_root, wrong_path)
    ]
    payload_path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(module.PayloadError, match="role|required evidence"):
        module.validate_payload_root(root, analytics, data)


def test_payload_root_values_requires_roots():
    module = _load_module()
    with pytest.raises(TypeError):
        module.validate_payload_root_values({})
    with pytest.raises(module.PayloadError, match="roots"):
        module.validate_payload_root_values({}, None, None)
