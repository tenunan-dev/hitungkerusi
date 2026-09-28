#!/usr/bin/env python3
"""Build the nine P1.3c generated payloads in a new caller-supplied directory.

This is deliberately neither an OUTPUTS writer nor a DELIVERY writer.  It is
the deterministic, read-only ANALYTICS-to-payload boundary used by the release
stager.  A caller must supply ``generated_at`` so no wall-clock value enters a
payload.
"""

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


STATE_SPECS = (
    ("Melaka", "melaka", 28), ("Pahang", "pahang", 42),
    ("Perak", "perak", 59), ("Perlis", "perlis", 15),
    ("Sarawak", "sarawak", 82),
)
PAYLOAD_NAMES = frozenset(
    ("app-data.json", "forecast.json", "scenarios.json", "social-payload.json",
     "melaka.json", "pahang.json", "perak.json", "perlis.json", "sarawak.json")
)
PAYLOAD_CONTENT_SCHEMAS = {
    "app-data.json": ("hermes.delivery.app-data", 1),
    "forecast.json": ("hermes.delivery.forecast", 1),
    "scenarios.json": ("hermes.delivery.scenarios", 1),
    "social-payload.json": ("hermes.delivery.social-payload", 1),
    "melaka.json": ("hermes.delivery.state-composition", 1),
    "pahang.json": ("hermes.delivery.state-composition", 1),
    "perak.json": ("hermes.delivery.state-composition", 1),
    "perlis.json": ("hermes.delivery.state-composition", 1),
    "sarawak.json": ("hermes.delivery.state-composition", 1),
}
ROLE_EVIDENCE_REQUIREMENTS = {
    "app-data.json": frozenset({
        ("ANALYTICS", "02_FORECAST/outputs/latest/ge16-forecast-latest.json"),
        ("ANALYTICS", "work/scenarios/scenario_meta.json"),
        ("DATA", "research/trackers/ge16-news-feed.json"),
    }),
    "forecast.json": frozenset({
        ("ANALYTICS", "02_FORECAST/outputs/latest/ge16-forecast-latest.json"),
    }),
    "scenarios.json": frozenset({
        ("ANALYTICS", "02_FORECAST/outputs/latest/ge16-forecast-latest.json"),
        ("ANALYTICS", "work/scenarios/scenario_meta.json"),
    }),
    "social-payload.json": frozenset(
        {("ANALYTICS", "04_SOCIAL/current.md")}
        | {("ANALYTICS", "04_SOCIAL/state/%s/current.md" % state)
           for state, _slug, _seat_total in STATE_SPECS}
    ),
}
for _state, _slug, _seat_total in STATE_SPECS:
    ROLE_EVIDENCE_REQUIREMENTS[_slug + ".json"] = frozenset({
        ("DATA", "research/states/DUN %s/dun-election-results-latest.csv" % _state),
        ("DATA", "research/states/DUN %s/%s-state-scenarios.json" % (_state, _slug)),
    })
MAX_INPUT_AGE = timedelta(days=14)
SHA256 = __import__("re").compile(r"^[0-9a-f]{64}$")

# Stage 1 rewrites the news feed file every cycle, so the feed FILE digest is
# stale by construction and can never carry reviewed narrative provenance.
# Record-level evidence does: ``record_id`` identifies a feed record and
# ``record_sha256`` is the digest of that record's canonical bytes, both of
# which survive a rewrite as long as the record keeps its identity and bytes.
NEWS_FEED_RELATIVE = "research/trackers/ge16-news-feed.json"
DERIVATION_KINDS = frozenset({
    "release-payload", "quantitative-scenario", "news-narrative", "state-composition", "social-source",
})


class PayloadError(ValueError):
    """An unsafe, stale, malformed, or non-provenanced payload input."""


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _symlink_error(path, label, include_final=True):
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    parts = absolute.parts[1:] if include_final else absolute.parts[1:-1]
    for part in parts:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise PayloadError("%s cannot be inspected: %s" % (label, current)) from exc
        if stat.S_ISLNK(mode):
            raise PayloadError("%s has symlinked path component: %s" % (label, current))


def _regular_file(path, label):
    path = Path(path)
    _symlink_error(path, label, include_final=False)
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise PayloadError("%s is missing: %s" % (label, path)) from exc
    if not stat.S_ISREG(mode):
        raise PayloadError("%s must be a regular non-symlink file: %s" % (label, path))
    return path


def _parse_generated_at(value, label="generated_at"):
    if not isinstance(value, str) or not value:
        raise PayloadError("%s must be an explicit ISO-8601 timestamp" % label)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PayloadError("%s must be an explicit ISO-8601 timestamp" % label) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PayloadError("%s must include a UTC offset" % label)
    return parsed.astimezone(timezone.utc)


def _utc_z(value, label="generated_at"):
    parsed = _parse_generated_at(value, label)
    canonical = parsed.isoformat().replace("+00:00", "Z")
    if value != canonical:
        raise PayloadError("%s must be canonical UTC Z" % label)
    return parsed


def _parse_source_time(value, label):
    if not isinstance(value, str):
        raise PayloadError("%s is missing its generated timestamp" % label)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PayloadError("%s has an invalid generated timestamp" % label) from exc
    # Historical source payloads pre-date the offset requirement.  Their own
    # timestamp is only an input freshness marker, not output provenance.
    return (parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc))


def _read_json(path, label):
    _regular_file(path, label)
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PayloadError("%s is malformed JSON: %s" % (label, path)) from exc
    if not isinstance(value, dict):
        raise PayloadError("%s must contain a JSON object: %s" % (label, path))
    return value


def _require_clean_data(data_root):
    import subprocess
    _symlink_error(data_root, "DATA root")
    result = subprocess.run(
        ["git", "-C", os.fspath(data_root), "status", "--porcelain", "--untracked-files=normal"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode:
        raise PayloadError("canonical DATA must be a readable Git repository")
    if result.stdout:
        raise PayloadError("canonical DATA is dirty")
    provenance = _read_json(Path(data_root) / "canonical-data-provenance.json", "DATA provenance")
    if provenance.get("schema") != "data.canonical-provenance.v2":
        raise PayloadError("DATA provenance has an unexpected schema")


def _load_app_data_module():
    path = Path(__file__).resolve().parents[1] / "delivery" / "build_app_data.py"
    spec = importlib.util.spec_from_file_location("p13c_build_app_data", path)
    if spec is None or spec.loader is None:
        raise PayloadError("canonical app-data generator is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_forecast(analytics_root):
    path = Path(analytics_root) / "02_FORECAST" / "outputs" / "latest" / "ge16-forecast-latest.json"
    return _read_json(path, "forecast engine output")


def _require_fresh(payload, generated_at, label):
    source_time = _parse_source_time(payload.get("generated"), label)
    if source_time > generated_at or generated_at - source_time > MAX_INPUT_AGE:
        raise PayloadError("%s is stale relative to generated_at" % label)


def _total(blocks):
    total = 0
    for value in blocks.values():
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise PayloadError("scenario seat values must be non-negative integers")
        total += value
    return total


def _allowed_evidence_path(repository, relative):
    if repository == "ANALYTICS":
        return relative in {
            "02_FORECAST/outputs/latest/ge16-forecast-latest.json",
            "work/scenarios/scenario_meta.json",
        } or relative == "04_SOCIAL/current.md" or any(
            relative == "04_SOCIAL/state/%s/current.md" % state for state, _slug, _total in STATE_SPECS
        )
    if repository == "DATA":
        return relative == NEWS_FEED_RELATIVE or any(
            relative in {
                "research/states/DUN %s/dun-election-results-latest.csv" % state,
                "research/states/DUN %s/%s-state-scenarios.json" % (state, slug),
            } for state, slug, _total in STATE_SPECS
        )
    return False


def _feed_record_key(item):
    """Canonical identity of a news-feed record: its ``id``, else its ``link``.

    Historic Stage-1 captures wrote an ``id`` on every item; the live collector
    guarantees only ``link``. Both are properties of the record itself and are
    stable across the rewrite of the feed file, unlike the file's own digest.
    """
    if not isinstance(item, dict):
        return None
    for key in ("id", "link"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _feed_records_matching(records, record_id):
    """Feed records identified by ``record_id``: exact ``id`` match wins.

    Falls back to ``link`` because the live feed carries no ``id`` while the
    reviewed evidence records the record's canonical URL.
    """
    items = [item for item in records or [] if isinstance(item, dict)]
    by_id = [item for item in items if item.get("id") == record_id]
    if by_id:
        return by_id
    return [item for item in items if item.get("link") == record_id]


def _canonical_evidence(repository, root, relative, record_id=None, context=None):
    if not isinstance(relative, str) or not _allowed_evidence_path(repository, relative):
        raise PayloadError("evidence references a non-canonical input path")
    path = _regular_file(Path(root) / relative, "provenance evidence")
    evidence = {"repository": repository, "path": relative, "sha256": _sha256(path)}
    if record_id is not None:
        feed = _read_json(path, "canonical news feed")
        matches = _feed_records_matching(feed.get("items"), record_id)
        if len(matches) != 1:
            raise PayloadError(
                "evidence references an unknown feed record ID %r (%d match(es))%s"
                % (record_id, len(matches), " for %s" % context if context else "")
            )
        evidence["record_id"] = record_id
        evidence["record_sha256"] = hashlib.sha256(_json_bytes(matches[0])).hexdigest()
    return evidence


def _validate_evidence(evidence, analytics_root, data_root, context=None):
    if analytics_root is None or data_root is None:
        raise PayloadError("canonical ANALYTICS and DATA roots are required")
    if not isinstance(evidence, list) or not evidence:
        raise PayloadError("provenance requires nonempty evidence references")
    for item in evidence:
        if not isinstance(item, dict) or set(item) - {"repository", "path", "sha256", "record_id", "record_sha256"}:
            raise PayloadError("provenance evidence has an invalid shape")
        repository, relative, digest = item.get("repository"), item.get("path"), item.get("sha256")
        if repository not in {"ANALYTICS", "DATA"} or not isinstance(relative, str) or not SHA256.fullmatch(str(digest)):
            raise PayloadError("provenance evidence is not content-bound")
        if not _allowed_evidence_path(repository, relative):
            raise PayloadError("provenance evidence path is not canonical")
        has_record = "record_id" in item or "record_sha256" in item
        if has_record and (not isinstance(item.get("record_id"), str) or not item["record_id"]
                           or not SHA256.fullmatch(str(item.get("record_sha256")))):
            raise PayloadError("provenance record evidence is not content-bound")
        if analytics_root is not None and data_root is not None:
            root = analytics_root if repository == "ANALYTICS" else data_root
            expected = _canonical_evidence(repository, root, relative, item.get("record_id"), context)
            if expected != item:
                raise PayloadError(
                    "%sprovenance evidence does not match canonical input content"
                    % ("%s " % context if context else "")
                )


def _validate_document_provenance(provenance, label, analytics_root, data_root):
    if not isinstance(provenance, dict) or set(provenance) != {"derivation", "evidence"}:
        raise PayloadError("%s provenance must be structured" % label)
    if provenance.get("derivation") not in DERIVATION_KINDS:
        raise PayloadError("%s provenance has unsupported derivation" % label)
    _validate_evidence(provenance.get("evidence"), analytics_root, data_root, label)


def _validate_role_evidence(name, evidence):
    """Bind each generated payload role to its declarative canonical source set."""
    expected = ROLE_EVIDENCE_REQUIREMENTS[name]
    actual = frozenset((item.get("repository"), item.get("path")) for item in evidence)
    if actual != expected or len(evidence) != len(expected):
        raise PayloadError("%s provenance does not contain its exact required evidence role set" % name)


def _narrative_feed_evidence(evidence):
    """The news-feed evidence items of a scenario provenance block."""
    return [item for item in evidence or []
            if isinstance(item, dict) and item.get("repository") == "DATA"
            and item.get("path") == NEWS_FEED_RELATIVE]


def _require_record_bound_feed_evidence(provenance, label):
    """A news-narrative scenario must cite feed RECORDS, never the feed file.

    Stage 1 rewrites the feed file every cycle, so a file-level digest is stale
    by construction and can never be verified at publish time. Only the record
    binding (``record_id`` + ``record_sha256``) survives that rewrite, so a
    narrative that names no record — or names one without its record digest —
    fails closed here, by scenario name, instead of failing later as a generic
    content mismatch.
    """
    feed_items = _narrative_feed_evidence(provenance.get("evidence") if isinstance(provenance, dict) else None)
    if not feed_items:
        raise PayloadError("%s narrative scenario lacks identified feed record evidence" % label)
    for item in feed_items:
        record_id = item.get("record_id")
        if not isinstance(record_id, str) or not record_id or not item.get("record_sha256"):
            raise PayloadError(
                "%s narrative scenario binds the news feed file digest, not a feed record "
                "(record_id=%r): every DATA feed evidence item must carry record_id and "
                "record_sha256" % (label, record_id)
            )


def _validate_provenance(provenance, category, label, analytics_root, data_root):
    if category == "parametric":
        expected = "quantitative-scenario"
    elif category == "narrative":
        expected = "news-narrative"
    else:
        raise PayloadError("%s has an unsupported scenario category" % label)
    if not isinstance(provenance, dict):
        raise PayloadError("%s %s scenario lacks structured provenance" % (label, category))
    _validate_document_provenance(provenance, label, analytics_root, data_root)
    if provenance["derivation"] != expected:
        raise PayloadError("%s provenance derivation does not match scenario category" % label)
    if category == "narrative":
        _require_record_bound_feed_evidence(provenance, label)
    elif not any(item.get("path") in {
        "02_FORECAST/outputs/latest/ge16-forecast-latest.json",
    } or item.get("path", "").endswith("dun-election-results-latest.csv") for item in provenance["evidence"]):
        raise PayloadError("%s quantitative scenario lacks measured/config/run evidence" % label)


def _federal_metadata(analytics_root):
    path = Path(analytics_root) / "work" / "scenarios" / "scenario_meta.json"
    return _read_json(path, "federal scenario metadata")


def _validate_federal_scenarios(app_data, forecast, metadata, analytics_root, data_root):
    scenarios = app_data.get("scenarios") if isinstance(app_data, dict) else None
    if not isinstance(scenarios, dict) or not scenarios:
        raise PayloadError("app-data has no scenarios")
    deterministic = forecast.get("deterministic")
    if not isinstance(deterministic, dict) or _total(deterministic) != 222:
        raise PayloadError("forecast engine deterministic composition must reconcile to 222")
    for name, blocks in scenarios.items():
        if not isinstance(name, str) or not isinstance(blocks, dict) or _total(blocks) != 222:
            raise PayloadError("federal scenario %r does not reconcile to 222" % name)
        item = metadata.get(name)
        if not isinstance(item, dict):
            raise PayloadError("federal scenario %r has no provenance metadata" % name)
        category = item.get("category")
        _validate_provenance(item.get("provenance"), category, "federal scenario %r" % name, analytics_root, data_root)
        if item["provenance"]["derivation"] == "quantitative-scenario":
            if name == "Base (swings)" and blocks != deterministic:
                raise PayloadError("Base (swings) does not match forecast engine deterministic composition")


def _state_composition(analytics_root, data_root, state, slug, expected_total, generated_at):
    root = Path(data_root) / "research" / "states" / ("DUN " + state)
    results_path = _regular_file(root / "dun-election-results-latest.csv", "%s canonical DUN results" % state)
    scenarios = _read_json(root / (slug + "-state-scenarios.json"), "%s state scenarios" % state)
    if scenarios.get("state") != state:
        raise PayloadError("%s state scenarios name the wrong state" % state)
    source_time = _parse_source_time(scenarios.get("generated_at"), "%s state scenarios" % state)
    if source_time > generated_at or generated_at - source_time > MAX_INPUT_AGE:
        raise PayloadError("%s state scenarios are stale relative to generated_at" % state)
    scenario_rows = scenarios.get("scenarios")
    if not isinstance(scenario_rows, list) or not scenario_rows:
        raise PayloadError("%s has no state scenarios" % state)
    reserved = {"scenario", "category", "flips", "seats_total", "provenance"}
    for row in scenario_rows:
        if not isinstance(row, dict) or not isinstance(row.get("scenario"), str):
            raise PayloadError("%s has a malformed state scenario" % state)
        blocks = {key: value for key, value in row.items() if key not in reserved}
        if row.get("seats_total") != expected_total or _total(blocks) != expected_total:
            raise PayloadError("%s scenario %r does not reconcile to %d" % (state, row.get("scenario"), expected_total))
        _validate_provenance(row.get("provenance"), row.get("category"), "%s scenario %r" % (state, row["scenario"]), analytics_root, data_root)

    by_bloc, by_party, rows = {}, {}, []
    try:
        with results_path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, csv.Error) as exc:
        raise PayloadError("%s canonical DUN results cannot be read" % state) from exc
    for row in rows:
        bloc, party = row.get("winner_bloc"), row.get("winner_party")
        if not isinstance(bloc, str) or not bloc or not isinstance(party, str) or not party:
            raise PayloadError("%s canonical DUN results have no winner bloc/party" % state)
        by_bloc[bloc] = by_bloc.get(bloc, 0) + 1
        by_party[party] = by_party.get(party, 0) + 1
    if len(rows) != expected_total or sum(by_bloc.values()) != expected_total or sum(by_party.values()) != expected_total:
        raise PayloadError("%s composition does not reconcile to %d" % (state, expected_total))
    return {"schema": "hermes.delivery.state-composition", "version": 1,
            "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
            "state": state, "slug": slug, "composition_by_bloc": dict(sorted(by_bloc.items())),
            "composition_by_party": dict(sorted(by_party.items())), "total": expected_total,
            "provenance": {"derivation": "state-composition", "evidence": [
                _canonical_evidence("DATA", data_root, "research/states/DUN %s/dun-election-results-latest.csv" % state),
                _canonical_evidence("DATA", data_root, "research/states/DUN %s/%s-state-scenarios.json" % (state, slug)),
            ]}}


def _social_payload(analytics_root, generated_at):
    root = Path(analytics_root) / "04_SOCIAL"
    paths = [root / "current.md"] + [root / "state" / state / "current.md" for state, _slug, _total in STATE_SPECS]
    posts, sources = [], []
    for path in paths:
        _regular_file(path, "social source")
        relative = path.relative_to(analytics_root).as_posix()
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            raise PayloadError("social source is empty: %s" % relative)
        sources.append({"path": relative, "sha256": _sha256(path), "bytes": len(text.encode("utf-8"))})
        posts.append({"scope": "federal" if relative == "04_SOCIAL/current.md" else "state",
                      "state": None if relative == "04_SOCIAL/current.md" else path.parent.name,
                      "source_path": relative, "markdown": text})
    return {"schema": "hermes.delivery.social-payload", "version": 1,
            "generated_at": generated_at.isoformat().replace("+00:00", "Z"), "sources": sources, "posts": posts,
            "provenance": {"derivation": "social-source", "evidence": [
                _canonical_evidence("ANALYTICS", analytics_root, source["path"]) for source in sources
            ]}}


def _require_validation_roots(analytics_root, data_root):
    if analytics_root is None or data_root is None:
        raise PayloadError("canonical ANALYTICS and DATA roots are required")


def validate_payload_file(name, value, analytics_root, data_root):
    """Validate one generated role against explicit canonical evidence roots."""
    _require_validation_roots(analytics_root, data_root)
    if name not in PAYLOAD_CONTENT_SCHEMAS or not isinstance(value, dict):
        raise PayloadError("payload file has an unknown schema: %s" % name)
    schema, version = PAYLOAD_CONTENT_SCHEMAS[name]
    if value.get("schema") != schema or value.get("version") != version:
        raise PayloadError("payload schema/version is invalid: %s" % name)
    _utc_z(value.get("generated_at"), "%s generated_at" % name)
    _validate_document_provenance(value.get("provenance"), name, analytics_root, data_root)
    _validate_role_evidence(name, value["provenance"]["evidence"])
    if name == "social-payload.json":
        if not isinstance(value.get("sources"), list) or not isinstance(value.get("posts"), list):
            raise PayloadError("social payload lacks source provenance/posts")
        evidence = {item["path"]: item["sha256"] for item in value["provenance"]["evidence"]}
        for source in value["sources"]:
            if (not isinstance(source, dict) or set(source) != {"path", "sha256", "bytes"}
                    or source.get("path") not in evidence or evidence[source["path"]] != source.get("sha256")
                    or not isinstance(source.get("bytes"), int) or source["bytes"] < 0):
                raise PayloadError("social payload source evidence is invalid")
        if {post.get("source_path") for post in value["posts"] if isinstance(post, dict)} != {
            source["path"] for source in value["sources"]
        }:
            raise PayloadError("social payload posts do not reconcile to source evidence")
    elif name.endswith(".json") and name not in {"app-data.json", "forecast.json", "scenarios.json"}:
        expected_state, expected_slug, expected_total = next(
            (state, slug, total) for state, slug, total in STATE_SPECS if name == slug + ".json"
        )
        if value.get("state") != expected_state or value.get("slug") != expected_slug or value.get("total") != expected_total:
            raise PayloadError("state-composition schema is invalid")
        if (not isinstance(value.get("total"), int) or isinstance(value.get("total"), bool)
                or not isinstance(value.get("composition_by_bloc"), dict)
                or not isinstance(value.get("composition_by_party"), dict)
                or any(not isinstance(count, int) or isinstance(count, bool) or count < 0
                       for composition in (value["composition_by_bloc"], value["composition_by_party"])
                       for count in composition.values())
                or sum(value["composition_by_bloc"].values()) != value["total"]
                or sum(value["composition_by_party"].values()) != value["total"]):
            raise PayloadError("state-composition counts must be non-negative integers with exact total")
    elif name == "app-data.json":
        required = ("master", "projection", "summary", "scenarios", "history", "general_news",
                    "dun_national", "dun_schedule", "state_proj", "scenario_provenance")
        if (any(key not in value for key in required) or not isinstance(value.get("summary"), dict)
                or not isinstance(value.get("scenario_provenance"), dict)):
            raise PayloadError("app-data schema is invalid")
    elif name == "forecast.json":
        required = ("updated", "govt_p50", "govt_p10", "govt_p90", "majority_pct", "flips",
                    "econ_term", "coalition", "coalition_text", "govt_blocs", "opp_blocs",
                    "bloc_colors", "party_colors",
                    "party_bloc", "history", "flips_list", "tight_seats")
        if any(key not in value for key in required) or not isinstance(value.get("updated"), str):
            raise PayloadError("forecast schema is invalid")
    elif name == "scenarios.json":
        required = ("scenarios", "scenarios_display", "scenario_descriptions", "scenario_deltas", "scenario_provenance")
        if (any(key not in value for key in required) or not isinstance(value.get("scenarios"), dict)
                or not isinstance(value.get("scenario_provenance"), dict)):
            raise PayloadError("scenarios schema is invalid")


def validate_payload_root_values(values, analytics_root, data_root):
    """Validate all nine documents together, including release-wide invariants."""
    _require_validation_roots(analytics_root, data_root)
    if set(values) != PAYLOAD_NAMES:
        raise PayloadError("payload root must contain exactly the nine generated artifacts")
    for name, value in values.items():
        validate_payload_file(name, value, analytics_root, data_root)
    generated = {value["generated_at"] for value in values.values()}
    if len(generated) != 1:
        raise PayloadError("payload documents have inconsistent generated_at values")
    app, forecast, scenarios = values["app-data.json"], values["forecast.json"], values["scenarios.json"]
    if len(app["master"]) != 222 or len(app["projection"]) != 222:
        raise PayloadError("app-data seat identity does not reconcile to 222")
    master_codes = [row.get("code") for row in app["master"] if isinstance(row, dict)]
    projection_codes = [row.get("code") for row in app["projection"] if isinstance(row, dict)]
    if (len(master_codes) != 222 or len(projection_codes) != 222 or len(set(master_codes)) != 222
            or len(set(projection_codes)) != 222 or set(master_codes) != set(projection_codes)):
        raise PayloadError("app-data master/projection seat identities are inconsistent")
    if app["scenarios"] != scenarios["scenarios"] or app["scenario_provenance"] != scenarios["scenario_provenance"]:
        raise PayloadError("app-data and scenarios documents are inconsistent")
    if set(app["scenario_provenance"]) != set(app["scenarios"]):
        raise PayloadError("scenario provenance does not cover every scenario")
    for name, blocks in app["scenarios"].items():
        if not isinstance(name, str) or not isinstance(blocks, dict) or _total(blocks) != 222:
            raise PayloadError("scenario document seat totals do not reconcile")
        provenance = app["scenario_provenance"].get(name)
        category = next((item.get("category") for item in scenarios.get("scenario_descriptions", [])
                         if isinstance(item, dict) and item.get("full") == name), "parametric")
        _validate_provenance_shape(provenance, category, "scenario %r" % name,
                                   analytics_root, data_root)
    summary = app["summary"]
    for key in ("updated", "govt_p50", "govt_p10", "govt_p90", "majority_pct", "flips"):
        if forecast.get(key) != summary.get(key):
            raise PayloadError("forecast and app-data summary are inconsistent")
    duplicated = {
        "econ_term": summary, "coalition": summary, "coalition_text": summary, "history": app,
        "govt_blocs": summary, "opp_blocs": summary,
        "bloc_colors": summary, "party_colors": summary, "party_bloc": summary,
        "flips_list": summary, "tight_seats": summary,
    }
    if any(forecast.get(key) != source.get(key) for key, source in duplicated.items()):
        raise PayloadError("forecast and app-data duplicated fields are inconsistent")
    if forecast["updated"] != next(iter(generated)):
        raise PayloadError("forecast updated timestamp is inconsistent")
    if not all(isinstance(forecast[key], int) and not isinstance(forecast[key], bool) for key in ("govt_p10", "govt_p50", "govt_p90")) or not (0 <= forecast["govt_p10"] <= forecast["govt_p50"] <= forecast["govt_p90"] <= 222):
        raise PayloadError("forecast seat totals are invalid")


def _validate_provenance_shape(provenance, category, label, analytics_root, data_root):
    if category == "narrative":
        expected = "news-narrative"
    else:
        expected = "quantitative-scenario"
    _validate_document_provenance(provenance, label, analytics_root, data_root)
    if provenance["derivation"] != expected:
        raise PayloadError("%s provenance derivation is invalid" % label)
    if category == "narrative":
        _require_record_bound_feed_evidence(provenance, label)


def _reconstruct_payload_values(analytics_root, data_root, generated_at):
    """Build all nine expected documents in memory from canonical inputs.

    This intentionally shares the deterministic construction path with the
    publisher but performs no payload-root inspection or output writes.  It is
    therefore safe for public-root validation and cannot recurse into it.
    """
    analytics_root, data_root = Path(analytics_root), Path(data_root)
    timestamp = _parse_generated_at(generated_at)
    generated = timestamp.isoformat().replace("+00:00", "Z")
    forecast = _load_forecast(analytics_root)
    _require_fresh(forecast, timestamp, "forecast engine output")
    app_module = _load_app_data_module()
    app_data = app_module.generate(analytics_root, generated_at=generated)
    if not isinstance(app_data, dict) or app_data.get("_input_gaps"):
        raise PayloadError("canonical app-data inputs are missing or malformed")
    metadata = _federal_metadata(analytics_root)
    _validate_federal_scenarios(app_data, forecast, metadata, analytics_root, data_root)
    # The app-data generator owns its computation but this boundary owns its
    # outward timestamp and all release provenance.
    app_data["summary"]["updated"] = generated
    app_evidence = [
        _canonical_evidence("ANALYTICS", analytics_root, "02_FORECAST/outputs/latest/ge16-forecast-latest.json"),
        _canonical_evidence("ANALYTICS", analytics_root, "work/scenarios/scenario_meta.json"),
        _canonical_evidence("DATA", data_root, NEWS_FEED_RELATIVE),
    ]
    app_data.update({
        "schema": "hermes.delivery.app-data", "version": 1, "generated_at": generated,
        "provenance": {"derivation": "release-payload", "evidence": app_evidence},
        "scenario_provenance": {name: metadata[name]["provenance"] for name in app_data["scenarios"]},
    })
    forecast_payload = app_module.build_forecast_json(app_data, forecast)
    forecast_payload.update({
        "schema": "hermes.delivery.forecast", "version": 1, "generated_at": generated,
        "provenance": {"derivation": "release-payload", "evidence": [app_evidence[0]]},
    })
    scenarios_payload = app_module.build_scenarios_json(app_data)
    scenarios_payload.update({
        "schema": "hermes.delivery.scenarios", "version": 1, "generated_at": generated,
        "provenance": {"derivation": "release-payload", "evidence": app_evidence[:2]},
        "scenario_provenance": app_data["scenario_provenance"],
    })
    values = {
        "app-data.json": app_data,
        "forecast.json": forecast_payload,
        "scenarios.json": scenarios_payload,
        "social-payload.json": _social_payload(analytics_root, timestamp),
    }
    for state, slug, total in STATE_SPECS:
        values[slug + ".json"] = _state_composition(analytics_root, data_root, state, slug, total, timestamp)
    if set(values) != PAYLOAD_NAMES:
        raise PayloadError("payload inventory is not exactly the required nine artifacts")
    # This is the structural/provenance validator only; it deliberately does
    # not call validate_payload_root, preventing reconstruction recursion.
    validate_payload_root_values(values, analytics_root, data_root)
    return values


def validate_payload_root(payload_root, analytics_root, data_root):
    """Strictly validate a new, direct-child P1.3c payload root for staging."""
    _require_validation_roots(analytics_root, data_root)
    root = Path(payload_root)
    _symlink_error(root, "payload root")
    try:
        mode = root.lstat().st_mode
    except OSError as exc:
        raise PayloadError("payload root is missing: %s" % root) from exc
    if not stat.S_ISDIR(mode):
        raise PayloadError("payload root must be a real directory: %s" % root)
    entries = list(root.iterdir())
    if {entry.name for entry in entries} != PAYLOAD_NAMES:
        raise PayloadError("payload root must contain exactly the nine generated artifacts")
    values = {}
    for entry in entries:
        _regular_file(entry, "payload artifact")
        values[entry.name] = _read_json(entry, "payload artifact")
    validate_payload_root_values(values, analytics_root, data_root)
    generated_at = values["app-data.json"]["generated_at"]
    expected_values = _reconstruct_payload_values(analytics_root, data_root, generated_at)
    for name in sorted(PAYLOAD_NAMES):
        if _json_bytes(values[name]) != _json_bytes(expected_values[name]):
            raise PayloadError("%s does not match the deterministic canonical build" % name)
    return values


def build_payloads(analytics_root, data_root, payload_root, generated_at):
    """Create precisely nine JSON files, atomically, in a new payload root."""
    analytics_root, data_root, payload_root = Path(analytics_root), Path(data_root), Path(payload_root)
    _parse_generated_at(generated_at)
    _symlink_error(analytics_root, "ANALYTICS root")
    _require_clean_data(data_root)
    _symlink_error(payload_root, "payload root")
    if payload_root.exists():
        raise PayloadError("payload root must not already exist: %s" % payload_root)
    if not payload_root.parent.exists() or not payload_root.parent.is_dir():
        raise PayloadError("payload root parent must be an existing directory")

    values = _reconstruct_payload_values(analytics_root, data_root, generated_at)

    temporary = Path(tempfile.mkdtemp(prefix=".p13c-payload-", dir=os.fspath(payload_root.parent)))
    try:
        for name in sorted(values):
            (temporary / name).write_bytes(_json_bytes(values[name]))
        os.replace(os.fspath(temporary), os.fspath(payload_root))
    except Exception:
        shutil.rmtree(os.fspath(temporary), ignore_errors=True)
        raise
    return payload_root


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analytics-root", required=True, type=Path)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--payload-root", required=True, type=Path)
    parser.add_argument("--generated-at", required=True)
    arguments = parser.parse_args(argv)
    try:
        root = build_payloads(arguments.analytics_root, arguments.data_root, arguments.payload_root, arguments.generated_at)
    except (OSError, PayloadError) as exc:
        print(json.dumps({"status": "payload-failed", "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps({"status": "payload-built", "payload_root": str(root)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
