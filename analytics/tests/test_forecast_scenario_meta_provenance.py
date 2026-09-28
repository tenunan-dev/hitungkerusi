"""Regression coverage for Stage-3 provenance digest refresh in the forecast engine.

Stage 3 seals every federal scenario against its declared provenance, binding
evidence to the *current* bytes of the canonical inputs. The engine rewrites
"02_FORECAST/outputs/latest/ge16-forecast-latest.json" on every run, so the
digest recorded in work/scenarios/scenario_meta.json must be re-bound by that
same run — otherwise the seal fail-closes with
"provenance evidence does not match canonical input content".
"""

import ast
import hashlib
import importlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ENGINE_ROOT = REPOSITORY_ROOT / "02_FORECAST" / "engine"
ENGINE_SOURCE = ENGINE_ROOT / "forecast_engine.py"
BUILDER_SOURCE = REPOSITORY_ROOT / "automation" / "outputs" / "build_release_payloads.py"
FORECAST_LATEST = REPOSITORY_ROOT / "02_FORECAST" / "outputs" / "latest" / "ge16-forecast-latest.json"
FORECAST_EVIDENCE_PATH = "02_FORECAST/outputs/latest/ge16-forecast-latest.json"
FEED_EVIDENCE_PATH = "research/trackers/ge16-news-feed.json"
STALE_DIGEST = "4e1020af71c909ddc270724ffb4a45769bb816607dd8b7822030ec6468e325bc"

if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))


@pytest.fixture()
def engine():
    return importlib.import_module("forecast_engine")


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write_sources(tmp_path, meta, forecast_bytes=b'{"generated": "2026-09-23T00:00:00+00:00"}\n'):
    """Create an isolated forecast output + scenario metadata pair."""
    forecast_path = tmp_path / "ge16-forecast-latest.json"
    forecast_path.write_bytes(forecast_bytes)
    meta_path = tmp_path / "work" / "scenarios" / "scenario_meta.json"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return forecast_path, meta_path


def _parametric(name, digest=STALE_DIGEST, with_provenance=True):
    entry = {"category": "parametric", "derivation": "quantitative", "basis": name + " basis"}
    if with_provenance:
        entry["provenance"] = {"derivation": "quantitative-scenario", "evidence": [
            {"repository": "ANALYTICS", "path": FORECAST_EVIDENCE_PATH, "sha256": digest},
        ]}
    return entry


def _narrative(composed="composed from the reviewed feed record"):
    return {"category": "narrative", "derivation": "news-narrative", "basis": composed,
            "provenance": {"derivation": "news-narrative", "evidence": [
                {"repository": "DATA", "path": FEED_EVIDENCE_PATH, "sha256": STALE_DIGEST,
                 "record_id": "ge16-2026-09-20-01", "record_sha256": STALE_DIGEST},
            ]}}


def test_refresh_rebinds_stale_forecast_evidence_digest(engine, tmp_path):
    meta = {"Base (swings)": _parametric("Base (swings)"), "PN surge +5pp": _parametric("PN surge +5pp")}
    forecast_path, meta_path = _write_sources(tmp_path, meta)

    report = engine.refresh_scenario_meta_evidence(forecast_path, meta_path)

    assert report["digest"] == _sha256(forecast_path)
    assert sorted(report["refreshed"]) == ["Base (swings)", "PN surge +5pp"]
    assert report["bound"] == []
    written = json.loads(meta_path.read_text(encoding="utf-8"))
    for name in meta:
        assert written[name]["provenance"]["evidence"] == [
            {"repository": "ANALYTICS", "path": FORECAST_EVIDENCE_PATH, "sha256": _sha256(forecast_path)}
        ]
        # Nothing but the digest moves.
        assert written[name]["category"] == "parametric"
        assert written[name]["derivation"] == "quantitative"
        assert written[name]["basis"] == meta[name]["basis"]


def test_refresh_leaves_data_record_provenance_and_compositions_intact(engine, tmp_path):
    meta = {"Bersatu civil war": _parametric("Bersatu civil war"), "Hand-written narrative": _narrative()}
    forecast_path, meta_path = _write_sources(tmp_path, meta)

    engine.refresh_scenario_meta_evidence(forecast_path, meta_path)

    written = json.loads(meta_path.read_text(encoding="utf-8"))
    # Reviewed narrative provenance is authored upstream: never invented, never extended.
    assert written["Hand-written narrative"]["provenance"] == meta["Hand-written narrative"]["provenance"]
    assert written["Hand-written narrative"]["provenance"]["evidence"] == [
        {"repository": "DATA", "path": FEED_EVIDENCE_PATH, "sha256": STALE_DIGEST,
         "record_id": "ge16-2026-09-20-01", "record_sha256": STALE_DIGEST}]
    assert set(written) == set(meta)


def test_refresh_rebinds_forecast_evidence_recorded_on_a_narrative_scenario(engine, tmp_path):
    """An already-recorded ANALYTICS digest is refreshed whatever the category."""
    narrative = _narrative()
    narrative["provenance"]["evidence"].append(
        {"repository": "ANALYTICS", "path": FORECAST_EVIDENCE_PATH, "sha256": STALE_DIGEST})
    forecast_path, meta_path = _write_sources(tmp_path, {"Narrative + run": narrative})

    report = engine.refresh_scenario_meta_evidence(forecast_path, meta_path)

    assert report["refreshed"] == ["Narrative + run"]
    assert report["bound"] == []
    evidence = json.loads(meta_path.read_text(encoding="utf-8"))["Narrative + run"]["provenance"]["evidence"]
    assert evidence[0] == narrative["provenance"]["evidence"][0]  # DATA record untouched
    assert evidence[1] == {"repository": "ANALYTICS", "path": FORECAST_EVIDENCE_PATH,
                           "sha256": _sha256(forecast_path)}


def test_refresh_binds_missing_required_evidence_for_parametric_scenarios_only(engine, tmp_path):
    meta = {"Govt surge": _parametric("Govt surge", with_provenance=False),
            "Unprovenanced narrative": {"category": "narrative", "derivation": "news-narrative",
                                        "basis": "hand-written"}}
    forecast_path, meta_path = _write_sources(tmp_path, meta)

    report = engine.refresh_scenario_meta_evidence(forecast_path, meta_path)

    assert report["bound"] == ["Govt surge"]
    written = json.loads(meta_path.read_text(encoding="utf-8"))
    assert written["Govt surge"]["provenance"] == {
        "derivation": "quantitative-scenario",
        "evidence": [{"repository": "ANALYTICS", "path": FORECAST_EVIDENCE_PATH,
                      "sha256": _sha256(forecast_path)}],
    }
    assert written["Govt surge"]["basis"] == meta["Govt surge"]["basis"]
    assert "provenance" not in written["Unprovenanced narrative"]


def test_refresh_never_creates_metadata_and_never_rewrites_nothing(engine, tmp_path):
    forecast_path = tmp_path / "ge16-forecast-latest.json"
    forecast_path.write_bytes(b"{}\n")
    absent = tmp_path / "work" / "scenarios" / "scenario_meta.json"

    report = engine.refresh_scenario_meta_evidence(forecast_path, absent)

    assert report["digest"] is None
    assert report["refreshed"] == [] and report["bound"] == []
    assert report["record_bound"] == [] and report["unbound"] == [] and report["warnings"] == []
    assert not absent.exists()

    # Nothing stale: the file is not rewritten at all (no timestamp/format churn).
    meta_path = absent
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps({"Status quo": _parametric("Status quo", _sha256(forecast_path))}) + "\n",
                         encoding="utf-8")
    before = meta_path.read_bytes()

    report = engine.refresh_scenario_meta_evidence(forecast_path, meta_path)

    assert report["refreshed"] == [] and report["bound"] == []
    assert report["digest"] == _sha256(forecast_path)
    assert meta_path.read_bytes() == before


def test_refresh_is_idempotent(engine, tmp_path):
    forecast_path, meta_path = _write_sources(tmp_path, {"Status quo": _parametric("Status quo")})

    engine.refresh_scenario_meta_evidence(forecast_path, meta_path)
    once = meta_path.read_bytes()
    report = engine.refresh_scenario_meta_evidence(forecast_path, meta_path)

    assert report["refreshed"] == [] and report["bound"] == []
    assert meta_path.read_bytes() == once


def test_refresh_digest_matches_stage3_canonical_evidence(engine, tmp_path):
    """The refreshed digest is byte-identical to Stage 3's own evidence binding."""
    if not FORECAST_LATEST.is_file():
        pytest.skip("canonical forecast output is not present in this workspace")
    meta = {"Base (swings)": _parametric("Base (swings)")}
    forecast_path, meta_path = _write_sources(tmp_path, meta, FORECAST_LATEST.read_bytes())

    engine.refresh_scenario_meta_evidence(forecast_path, meta_path)

    builder = _load_module("scenario_meta_refresh_builder", BUILDER_SOURCE)
    expected = builder._canonical_evidence("ANALYTICS", REPOSITORY_ROOT, FORECAST_EVIDENCE_PATH)
    written = json.loads(meta_path.read_text(encoding="utf-8"))
    assert written["Base (swings)"]["provenance"]["evidence"] == [expected]
    # The helper's own digest source agrees with Stage 3's evidence hasher.
    assert engine.sha256_file(forecast_path) == expected["sha256"]


def test_every_generation_path_calls_the_refresh():
    """Both the default (cron) run and the --scenarios branch must re-bind."""
    tree = ast.parse(ENGINE_SOURCE.read_text(encoding="utf-8"), filename=str(ENGINE_SOURCE))
    main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
    calls = [node for node in ast.walk(main)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id == "refresh_scenario_meta_evidence"]
    assert len(calls) == 2
    # The default path re-binds against the output it just wrote.
    assert any(isinstance(arg, ast.Name) and arg.id == "latest_path"
               for call in calls for arg in call.args)


# ---------------------------------------------------------------------------
# Record-level (never file-level) narrative provenance
#
# Stage 1 rewrites the feed file every cycle, so a file digest recorded against
# it is stale by construction. A reviewed news-narrative scenario must bind the
# RECORDS it was composed from, and those bindings must be recomputed the same
# canonical way Stage 3 verifies them (_canonical_evidence / _json_bytes).
# ---------------------------------------------------------------------------
DATA_ROOT = REPOSITORY_ROOT.parent / "1_DATA"
LIVE_FEED = DATA_ROOT / "research" / "trackers" / "ge16-news-feed.json"
NARRATIVE_NAME = "BN-PN federal pact (N9 template to GE16)"
FEED_RECORD = {
    "query": "feed:Scoop",
    "title": "BN-PN federal pact takes shape in Negeri Sembilan",
    "date": "2026-09-22T01:13:56+00:00",
    "source": "Scoop",
    "link": "https://example.invalid/news/bn-pn-federal-pact",
    "lang": "en",
    "category": "coalition",
    "blocs": ["BN", "PN"],
    "parties": [],
    "seats": [],
    "score": 0.9,
    "judged_at": "2026-09-22T01:24:01+00:00",
}


def _record_sha256(record):
    return hashlib.sha256(
        (json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    ).hexdigest()


def _data_root(tmp_path, records):
    """A minimal DATA tree holding one news feed; returns (root, feed_path)."""
    feed_path = tmp_path / "1_DATA" / "research" / "trackers" / "ge16-news-feed.json"
    feed_path.parent.mkdir(parents=True, exist_ok=True)
    feed_path.write_text(
        json.dumps({"schema": "ge16-news-feed v1", "count": len(records), "items": records},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    return feed_path.parents[2], feed_path


def _builder():
    return _load_module("scenario_meta_record_binding_builder", BUILDER_SOURCE)


def _file_bound_narrative(basis, record_id=None):
    """A narrative whose feed evidence binds the FILE digest (the stale shape)."""
    evidence = {"repository": "DATA", "path": FEED_EVIDENCE_PATH, "sha256": STALE_DIGEST}
    if record_id is not None:
        evidence["record_id"] = record_id
    return {NARRATIVE_NAME: {
        "category": "narrative", "derivation": "news-narrative", "basis": basis,
        "provenance": {"derivation": "news-narrative", "evidence": [evidence]},
    }}


def _evidence(meta_path):
    written = json.loads(meta_path.read_text(encoding="utf-8"))
    return written[NARRATIVE_NAME]["provenance"]["evidence"]


def test_refresh_upgrades_file_level_narrative_binding_to_record_evidence(engine, tmp_path):
    """A recorded record_id that is still in the feed is re-bound, not trusted."""
    data_root, feed_path = _data_root(tmp_path, [FEED_RECORD])
    meta = _file_bound_narrative("Reviewed 22 Sep 2026 against %s" % FEED_RECORD["link"],
                                 record_id=FEED_RECORD["link"])
    forecast_path, meta_path = _write_sources(tmp_path, meta)

    report = engine.refresh_scenario_meta_evidence(forecast_path, meta_path, feed_path)

    assert report["record_bound"] == [NARRATIVE_NAME]
    assert report["unbound"] == [] and report["warnings"] == []
    assert _evidence(meta_path) == [
        _builder()._canonical_evidence("DATA", data_root, FEED_EVIDENCE_PATH, FEED_RECORD["link"])
    ]
    assert _evidence(meta_path)[0]["record_sha256"] == _record_sha256(FEED_RECORD)


@pytest.mark.parametrize("citation", ("url", "headline"))
def test_refresh_resolves_record_cited_only_in_the_basis_text(engine, tmp_path, citation):
    """A file-level narrative that names its record in prose is upgraded too."""
    data_root, feed_path = _data_root(tmp_path, [FEED_RECORD])
    cited = FEED_RECORD["link"] if citation == "url" else FEED_RECORD["title"]
    meta = _file_bound_narrative("Composed after reviewing: %s" % cited)
    forecast_path, meta_path = _write_sources(tmp_path, meta)

    report = engine.refresh_scenario_meta_evidence(forecast_path, meta_path, feed_path)

    assert report["record_bound"] == [NARRATIVE_NAME]
    assert report["unbound"] == []
    assert _evidence(meta_path) == [
        _builder()._canonical_evidence("DATA", data_root, FEED_EVIDENCE_PATH, FEED_RECORD["link"])
    ]


def test_refresh_never_fabricates_a_record_id_when_the_record_is_gone(engine, tmp_path):
    """An unresolvable record fails loud by scenario name; nothing is invented."""
    data_root, feed_path = _data_root(tmp_path, [FEED_RECORD])
    missing = "https://example.invalid/news/record-that-aged-out"
    meta = _file_bound_narrative("Reviewed against a record today's feed no longer carries.",
                                 record_id=missing)
    forecast_path, meta_path = _write_sources(tmp_path, meta)
    before = meta_path.read_bytes()

    report = engine.refresh_scenario_meta_evidence(forecast_path, meta_path, feed_path)

    assert report["record_bound"] == []
    assert report["unbound"] == [NARRATIVE_NAME]
    assert any(NARRATIVE_NAME in warning and missing in warning for warning in report["warnings"])
    # Left exactly as reviewed: no fabricated record_id, no rewritten digest.
    assert meta_path.read_bytes() == before

    # ...so Stage 3 fails closed naming both the scenario and the record.
    builder = _builder()
    with pytest.raises(builder.PayloadError) as failure:
        builder._canonical_evidence("DATA", data_root, FEED_EVIDENCE_PATH, missing,
                                    "federal scenario %r" % NARRATIVE_NAME)
    assert NARRATIVE_NAME in str(failure.value) and missing in str(failure.value)


def test_refresh_warns_when_a_narrative_binds_no_feed_record_at_all(engine, tmp_path):
    """A narrative with no feed evidence is reported, never given invented evidence."""
    _data_root(tmp_path, [FEED_RECORD])
    meta = {NARRATIVE_NAME: {"category": "narrative", "derivation": "news-narrative",
                             "basis": "Reviewed narrative without feed evidence."}}
    forecast_path, meta_path = _write_sources(tmp_path, meta)

    report = engine.refresh_scenario_meta_evidence(forecast_path, meta_path)

    assert report["unbound"] == [NARRATIVE_NAME]
    assert any(NARRATIVE_NAME in warning for warning in report["warnings"])
    assert "provenance" not in json.loads(meta_path.read_text(encoding="utf-8"))[NARRATIVE_NAME]


def test_record_digest_matches_stage3_canonical_evidence_for_live_feed(engine, tmp_path):
    """The recomputed record binding is byte-identical to Stage 3's own.

    Uses the canonical DATA feed in this workspace, so the two serializations
    (engine ``canonical_json_bytes`` vs builder ``_json_bytes``) and the two
    record lookup rules are proven identical against real data.

    The feed is a ROLLING view: its newest item is whatever the tracker collected
    last. Pick the newest record the engine's own citation extractor can resolve
    (an RSS link carrying an HTML entity such as ``&#038;`` is not citable — see
    ``test_rss_entity_link_is_not_citable_yet``) so this contract test cannot flap
    on collection order.
    """
    if not LIVE_FEED.is_file():
        pytest.skip("canonical DATA news feed is not present in this workspace")
    items = json.loads(LIVE_FEED.read_text(encoding="utf-8"))["items"]
    record = next(item for item in items
                  if engine.cited_feed_records(engine.feed_record_key(item), items))
    record_id = engine.feed_record_key(record)
    assert record_id and engine.feed_record_key(record) == _builder()._feed_record_key(record)
    meta = _file_bound_narrative("Composed after reviewing: %s" % record_id)
    forecast_path, meta_path = _write_sources(tmp_path, meta)

    report = engine.refresh_scenario_meta_evidence(forecast_path, meta_path, LIVE_FEED)

    assert report["record_bound"] == [NARRATIVE_NAME] and report["unbound"] == []
    expected = _builder()._canonical_evidence("DATA", DATA_ROOT, FEED_EVIDENCE_PATH, record_id)
    assert _evidence(meta_path) == [expected]
    assert expected["record_sha256"] == _record_sha256(record)


def test_refresh_leaves_parametric_evidence_on_the_file_level(engine, tmp_path):
    """Only news-narrative provenance is record-bound; parametric paths are untouched."""
    _data_root(tmp_path, [FEED_RECORD])
    meta = {"Status quo": {
        "category": "parametric", "derivation": "quantitative", "basis": "Status quo basis",
        "provenance": {"derivation": "quantitative-scenario", "evidence": [
            {"repository": "ANALYTICS", "path": FORECAST_EVIDENCE_PATH, "sha256": STALE_DIGEST},
            {"repository": "DATA", "path": FEED_EVIDENCE_PATH, "sha256": STALE_DIGEST},
        ]},
    }}
    forecast_path, meta_path = _write_sources(tmp_path, meta)

    report = engine.refresh_scenario_meta_evidence(forecast_path, meta_path)

    assert report["refreshed"] == ["Status quo"]
    assert report["record_bound"] == [] and report["unbound"] == [] and report["warnings"] == []
    written = json.loads(meta_path.read_text(encoding="utf-8"))["Status quo"]
    assert written["provenance"]["evidence"] == [
        {"repository": "ANALYTICS", "path": FORECAST_EVIDENCE_PATH, "sha256": _sha256(forecast_path)},
        {"repository": "DATA", "path": FEED_EVIDENCE_PATH, "sha256": STALE_DIGEST},
    ]
    assert written["category"] == "parametric" and written["basis"] == "Status quo basis"


def test_refresh_is_idempotent_for_record_bound_narratives(engine, tmp_path):
    """A second refresh of an already record-bound narrative rewrites nothing."""
    _data_root_path, feed_path = _data_root(tmp_path, [FEED_RECORD])
    meta = _file_bound_narrative("Composed after reviewing: %s" % FEED_RECORD["link"])
    forecast_path, meta_path = _write_sources(tmp_path, meta)

    engine.refresh_scenario_meta_evidence(forecast_path, meta_path, feed_path)
    once = meta_path.read_bytes()
    report = engine.refresh_scenario_meta_evidence(forecast_path, meta_path, feed_path)

    assert report["record_bound"] == [] and report["unbound"] == []
    assert meta_path.read_bytes() == once


@pytest.mark.xfail(strict=True, reason=(
    "known Stage-3 limitation: CITED_URL's character class excludes ';', so a feed "
    "URL carrying an HTML entity (&#038;) cannot be cited by a reviewed basis text; "
    "the live feed already contains such records. Widen CITED_URL and drop this "
    "marker (strict=True makes an unexpected pass fail the suite until it is)."))
def test_rss_entity_link_is_not_citable_yet(engine):
    """Characterization of the citation limit the live feed keeps hitting.

    Proven pre-existing: reordering the COMMITTED feed so an entity-escaped link
    comes first reproduces this with no baseline-rebuild data at all.
    """
    record = {"title": "Entity link fixture",
              "link": "https://example.test/news/item?utm_source=rss&#038;utm_medium=rss"}
    basis = "Composed after reviewing: %s" % record["link"]
    assert engine.cited_feed_records(basis, [record]) == [record]
