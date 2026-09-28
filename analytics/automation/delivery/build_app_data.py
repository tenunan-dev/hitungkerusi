"""Build the composed app-data JSON payload for GE16 V2 deliveries.

Adapted from the legacy 05_AUTOMATION/gen_app_data2.py generator (Hermes-owned,
pre-V2). That script writes `window.GE16_APP_DATA = {...}` directly into the
Aila app's js/data.js — a JS-embedding convention that couples the generator
to one specific website's file layout.

This module extracts the SAME computation (scenarios, news, PRN state data,
colours, sources, summary stats — everything both website adapters flagged as
missing in MISSING_DATA.md) and publishes it as three portable JSON files
under the delivery contract instead:

  data/app-data.json  — master/projection/summary/scenarios/news/etc.
  data/forecast.json  — stable published forecast contract (subset of engine output)
  data/scenarios.json — the named scenario set + descriptions

Read-only against SOURCE_ROOT (legacy project). Never writes there.
"""

import glob as _glob
from importlib.util import module_from_spec, spec_from_file_location
import json
import logging
import os
import re
import stat
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

import pandas as pd


_DATA_ROOTS_MODULE = "_build_app_data_data_roots"


def _resolve_canonical_data_roots(source_root: Path):
    """Reuse the forecast resolver without making this delivery module cwd-dependent.

    The resolver requires every canonical sibling-DATA domain and deliberately
    has no compatibility fallback to the in-repository legacy research tree.
    """
    resolver_path = Path(__file__).resolve().parents[2] / "02_FORECAST" / "engine" / "data_roots.py"
    module = sys.modules.get(_DATA_ROOTS_MODULE)
    if module is None or Path(getattr(module, "__file__", "")).resolve() != resolver_path.resolve():
        spec = spec_from_file_location(_DATA_ROOTS_MODULE, resolver_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Canonical DATA resolver is unavailable: {resolver_path}")
        module = module_from_spec(spec)
        sys.modules[_DATA_ROOTS_MODULE] = module
        spec.loader.exec_module(module)
    return module.resolve_data_roots(repository_root=source_root)


def _guarded_input(path: Path) -> Path:
    """Reject lexical symlinks before any canonical input is opened.

    Do not use ``resolve()`` here: resolving would hide the exact ancestor
    through which a caller attempted to escape the canonical DATA tree.
    Missing final paths are allowed for optional inputs, but every existing
    component (including the final file) must be a real filesystem object.
    """
    path = Path(path)
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError("canonical input cannot be inspected: %s" % current) from exc
        if stat.S_ISLNK(mode):
            raise ValueError("canonical input has symlinked path component: %s" % current)
    return path


def _read_csv_safe(path: Path):
    _guarded_input(path)
    try:
        return pd.read_csv(path)
    except Exception:
        return None


def _load_json_safe(path: Path, default=None):
    _guarded_input(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _valid_projected_seats(records) -> Optional[str]:
    """Return a validation failure for a forecast seat list, if any.

    `projected_seats` is the forecast engine's required delivery contract: a
    usable forecast must cover each parliamentary seat with a winner. This is
    deliberately stricter than a JSON parse check so a present but incomplete
    engine output cannot be mistaken for an absent file and fall back to CSV.
    """
    if not isinstance(records, list):
        return "projected_seats is missing or is not a list"
    if len(records) != 222:
        return f"projected_seats has {len(records)} records, expected 222"

    codes = set()
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            return f"projected_seats[{index}] is not an object"
        code = record.get("code")
        winner = record.get("proj_winner")
        if not isinstance(code, str) or not code.strip():
            return f"projected_seats[{index}] has no valid code"
        if not isinstance(winner, str) or not winner.strip():
            return f"projected_seats[{index}] has no valid proj_winner"
        if code in codes:
            return f"projected_seats has duplicate code {code}"
        codes.add(code)
    return None


def _load_news_feed_contract(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    """Load the required canonical news feed without conflating invalid presence with absence."""
    _guarded_input(path)
    if not path.exists():
        return None, "required file is missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return None, f"malformed JSON: {exc}"
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return None, "schema requires an object with an items list"
    if any(not isinstance(item, dict) for item in payload["items"]):
        return None, "schema requires every items entry to be an object"
    return payload, None


def _load_forecast_contract(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    """Load a valid engine forecast, preserving absence vs. invalid presence.

    Returns ``(None, None)`` only when the file is absent. Any other second
    value is a reason that a *present* file cannot satisfy the contract.
    """
    _guarded_input(path)
    if not path.exists():
        return None, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return None, f"failed to parse JSON: {exc}"
    if not isinstance(payload, dict):
        return None, "top-level JSON value is not an object"
    reason = _valid_projected_seats(payload.get("projected_seats"))
    if reason:
        return None, reason
    return payload, None


def _validated_projection_csv(path: Path) -> Tuple[Optional[list], Optional[str]]:
    """Read the explicit absent-JSON fallback and validate the same contract."""
    frame = _read_csv_safe(path)
    if frame is None:
        return None, "file missing or failed to parse as CSV"
    records = frame.to_dict("records")
    reason = _valid_projected_seats(records)
    if reason:
        return None, reason
    return records, None


_PRN_STATES = [
    ("Melaka", "melaka"), ("Sarawak", "sarawak"), ("Pahang", "pahang"),
    ("Perak", "perak"), ("Perlis", "perlis"),
]

_SHORT = {
    "Status quo": "Status quo", "Base (swings)": "Base",
    "PN surge +5pp": "PN surge", "Govt surge": "Govt surge",
    "Bersatu split (\u22126pp PN in Bersatu seats)": "Bersatu split",
    "Bersatu civil war (\u221210pp PN)": "Bersatu civil war",
    "Full fragmentation (Bersatu \u22126 + Bersama \u22123 PH urban)": "Full fragmentation",
    "DAP solo (DAP keluar PH)": "DAP solo",
    "BERSAMA standalone (spoiler PH urban)": "BERSAMA",
    "BN-PN federal pact (N9 template to GE16)": "BN-PN pact",
}

_BLOC_COLORS = {
    "PH": "#e6423e", "PN": "#16a34a", "BN": "#1d4ed8", "GPS": "#a16207",
    "GRS": "#0d9488", "WARISAN": "#f97316", "KDM": "#65a30d", "PBM": "#c2410c",
    "MUDA": "#eab308", "IND": "#64748b", "DAP": "#b91c1c", "PSB": "#0ea5e9",
    "PBS": "#0d9488", "UPKO": "#0d9488", "STAR": "#64748b",
    "BERSAMA": "#0ea5e9", "LAIN": "#475569",
}
_PARTY_COLORS = {
    "UMNO": "#1d4ed8", "MCA": "#1d4ed8", "MIC": "#1d4ed8", "PBRS": "#1d4ed8",
    "DAP": "#b91c1c", "PKR": "#e6423e", "AMANAH": "#e6423e", "WAWASAN": "#e6423e",
    "PAS": "#16a34a", "BERSATU": "#16a34a", "GERAKAN": "#16a34a",
    "PBB": "#a16207", "SUPP": "#a16207", "PRS": "#a16207", "PDP": "#a16207",
    "PGRS": "#0d9488", "PBS": "#0d9488", "UPKO": "#0d9488",
    "WARISAN": "#f97316", "PSB": "#0ea5e9", "STAR": "#64748b",
    "KDM": "#65a30d", "BEBAS": "#64748b", "MUDA": "#eab308", "PBM": "#c2410c",
}
_PARTY_BLOC = {
    "UMNO": "BN", "MCA": "BN", "MIC": "BN", "PBRS": "BN",
    "DAP": "PH", "PKR": "PH", "AMANAH": "PH", "WAWASAN": "PN",
    "PAS": "PN", "BERSATU": "PN", "GERAKAN": "PN",
    "PBB": "GPS", "SUPP": "GPS", "PRS": "GPS", "PDP": "GPS",
    "PGRS": "GRS", "PBS": "GRS", "UPKO": "GRS",
    "WARISAN": "WARISAN", "PSB": "PSB", "STAR": "STAR", "KDM": "KDM",
    "BEBAS": "IND", "MUDA": "MUDA", "PBM": "PBM",
}
_PARTY_BLOC_FALLBACK = dict(_PARTY_BLOC)


def _load_canonical_party_bloc() -> dict:
    """Return delivery-owned party presentation metadata.

    Canonical research reads are resolved exclusively through sibling DATA;
    this static display map is not a research-data input.
    """
    return dict(_PARTY_BLOC_FALLBACK)

_DUN_NATIONAL = [
    {"state": "Johor", "seats": 56, "gov": "BN 48/56", "el": "SE-16 (11 Jul 2026)"},
    {"state": "Kedah", "seats": 36, "gov": "PN 33/36", "el": "SE-15 (12 Aug 2023)"},
    {"state": "Kelantan", "seats": 45, "gov": "PN 43/45", "el": "SE-15 (12 Aug 2023)"},
    {"state": "Melaka", "seats": 28, "gov": "BN 21/28", "el": "SE-15 (20 Nov 2021)"},
    {"state": "Negeri Sembilan", "seats": 36, "gov": "BN+PN 25/36", "el": "SE-16 (1 Aug 2026)"},
    {"state": "Pahang", "seats": 42, "gov": "BN+PH 25/42 (hung)", "el": "SE-15 (Nov-Dec 2022)"},
    {"state": "Perak", "seats": 59, "gov": "BN+PH 33/59", "el": "SE-15 (19 Nov 2022)"},
    {"state": "Perlis", "seats": 15, "gov": "PN 14/15", "el": "SE-15 (19 Nov 2022)"},
    {"state": "Pulau Pinang", "seats": 40, "gov": "PH 27/40", "el": "SE-15 (12 Aug 2023)"},
    {"state": "Sabah", "seats": 73, "gov": "GRS-led 39/73", "el": "SE-15 (29 Nov 2025)"},
    {"state": "Sarawak", "seats": 82, "gov": "GPS 76/82", "el": "SE-12 (18 Dec 2021)"},
    {"state": "Selangor", "seats": 56, "gov": "PH+BN 34/56", "el": "SE-15 (12 Aug 2023)"},
    {"state": "Terengganu", "seats": 32, "gov": "PN 32/32", "el": "SE-15 (12 Aug 2023)"},
]

_DUN_SCHEDULE = [
    {"state": "Sarawak", "last_election": "2021-12-18", "dissolution": "2026-12-17", "status": "upcoming", "projection": "sarawak_prn"},
    {"state": "Melaka", "last_election": "2021-11-20", "dissolution": "2026-12-27", "status": "upcoming", "projection": "melaka_prn"},
    {"state": "Pahang", "last_election": "2022-11-19", "dissolution": "2027-11-18", "status": "upcoming", "projection": "pahang_prn"},
    {"state": "Perak", "last_election": "2022-11-19", "dissolution": "2027-11-18", "status": "upcoming", "projection": "perak_prn"},
    {"state": "Perlis", "last_election": "2022-11-19", "dissolution": "2027-11-18", "status": "upcoming", "projection": "perlis_prn"},
    {"state": "Kedah", "last_election": "2023-08-12", "dissolution": "2028-08-11", "status": "later", "projection": None},
    {"state": "Kelantan", "last_election": "2023-08-12", "dissolution": "2028-08-11", "status": "later", "projection": None},
    {"state": "Terengganu", "last_election": "2023-08-12", "dissolution": "2028-08-11", "status": "later", "projection": None},
    {"state": "Pulau Pinang", "last_election": "2023-08-12", "dissolution": "2028-08-11", "status": "later", "projection": None},
    {"state": "Selangor", "last_election": "2023-08-12", "dissolution": "2028-08-11", "status": "later", "projection": None},
    {"state": "Johor", "last_election": "2026-07-11", "dissolution": "2031-07-10", "status": "done", "projection": None},
    {"state": "Negeri Sembilan", "last_election": "2026-08-01", "dissolution": "2031-07-31", "status": "done", "projection": None},
    {"state": "Sabah", "last_election": "2025-11-29", "dissolution": "2030-11-28", "status": "done", "projection": None},
]

_SOURCES = [
    {"id": "S-01", "name": "Election Commission of Malaysia (SPR)", "type": "Official",
     "use": "GE15 official results, margins, turnout", "license": "Public domain", "link": "https://www.spr.gov.my"},
    {"id": "S-02", "name": "ElectionData.MY / MECo \u2014 Malaysian Election Corpus (Thevananthan 2025)", "type": "Academic corpus",
     "use": "Voter roll aggregates, DUN results, by-elections, candidate records", "license": "CC0", "link": "https://electiondata.my"},
    {"id": "S-03", "name": "DOSM / OpenDOSM", "type": "Official",
     "use": "Population by constituency", "license": "Public", "link": "https://www.dosm.gov.my"},
    {"id": "S-04", "name": "Merdeka Center", "type": "Research centre",
     "use": "Approval ratings, leadership surveys", "license": "Cite on use", "link": "https://merdeka.org"},
    {"id": "S-05", "name": "Ilham Centre", "type": "Research centre",
     "use": "Poll projections, field studies", "license": "Cite on use", "link": "https://ilhamcentre.com"},
    {"id": "S-06", "name": "ISEAS Perspective 2023/20 (Marzuki Mohamad & Ibrahim Suffian)", "type": "Academic",
     "use": "Ethnicity and voter preferences (GE15)", "license": "APA", "link": "https://www.iseas.edu.sg"},
    {"id": "S-07", "name": "Pepinsky, Fosco & Ostwald (2023), SMU", "type": "Academic",
     "use": "Demographic structure and voting behaviour", "license": "APA", "link": ""},
    {"id": "S-08", "name": "Lewis-Beck & Stegmaier (2000), Annu. Rev. Polit. Sci.", "type": "Academic",
     "use": "Economic-voting coefficients", "license": "APA", "link": ""},
    {"id": "S-09", "name": "RSIS Commentary (Jul 2026)", "type": "Academic",
     "use": "Johor 2026 assessment, Bersama spoiler", "license": "Cite on use", "link": "https://www.rsis.edu.sg"},
    {"id": "S-10", "name": "Fulcrum / ISEAS (Hutchinson 2026/182)", "type": "Academic",
     "use": "PAS\u2013Bersatu rupture analysis", "license": "Cite on use", "link": "https://fulcrum.sg"},
    {"id": "S-11", "name": "News media (Malaysiakini, The Star, NST, Malay Mail, FMT, The Edge, CNA, ST, Borneo Post)", "type": "News",
     "use": "Event facts (splits, candidates, timing)", "license": "Cite on use", "link": ""},
    {"id": "S-12", "name": "BTI 2026 Country Report", "type": "Institutional",
     "use": "Macroeconomic indicators", "license": "Cite on use", "link": "https://bti-project.org"},
    {"id": "S-13", "name": "Wilkin, Hallerberg & Carey (1997)", "type": "Academic",
     "use": "GDP\u2192vote elasticity (~1.4pp per 1pp)", "license": "APA", "link": ""},
]

_ACKNOWLEDGEMENTS = (
    "Data: Election Commission of Malaysia (official results) \u00b7 ElectionData.MY / MECo \u2014 "
    "The Malaysian Election Corpus (Thevananthan 2025), CC0-licensed \u00b7 DOSM / OpenDOSM. "
    "Research & analysis: Merdeka Center \u00b7 Ilham Centre \u00b7 ISEAS \u2013 Yusof Ishak Institute (Perspective 2023/20; Fulcrum) \u00b7 "
    "RSIS \u00b7 SMU (Pepinsky, Fosco & Ostwald) \u00b7 BTI Project. Scholarship: Lewis-Beck & Stegmaier (2000); "
    "Wilkin, Hallerberg & Carey (1997). Media: Malaysiakini \u00b7 The Star \u00b7 New Straits Times \u00b7 Malay Mail \u00b7 "
    "Free Malaysia Today \u00b7 The Edge \u00b7 Channel NewsAsia \u00b7 The Straits Times \u00b7 Borneo Post. "
    "Tools: Python \u00b7 pandas \u00b7 NumPy \u00b7 Aila (hosting). "
    "Disclaimer: all projections are the author\u2019s own analysis; no source endorses the conclusions."
)


def _generated_clock(generated_at: str):
    """Return an explicit, canonical UTC generation clock and serialized form."""
    if not isinstance(generated_at, str) or not generated_at:
        raise ValueError("generated_at must be an explicit ISO-8601 timestamp")
    try:
        value = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("generated_at must be an explicit ISO-8601 timestamp") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("generated_at must include a UTC offset")
    value = value.astimezone(timezone.utc)
    return value, value.isoformat().replace("+00:00", "Z")


def generate(source_root: Path, generated_at: str) -> dict:
    """Read canonical data (read-only) and return the composed app-data dict.

    Mirrors 05_AUTOMATION/gen_app_data2.py's computation exactly, adapted to
    read from an explicit source_root rather than assuming cwd, and to RETURN
    the dict rather than writing `window.GE16_APP_DATA = ...` JS.
    """
    generated_clock, explicit_generated_at = _generated_clock(generated_at)
    source_root = Path(source_root)
    # The resolver normalizes its repository argument.  Inspect the lexical
    # caller path first so a symlinked HERMES/DATA ancestor cannot disappear
    # before the per-file guarded readers see it.
    _guarded_input(source_root)
    _guarded_input(source_root.parent / "1_DATA")
    roots = _resolve_canonical_data_roots(source_root)
    # Resolving once validates raw, derived, trackers, federal, geo, and
    # states together before any delivery input is consumed.
    R = roots.derived
    P = roots.federal
    A = source_root / "work" / "scenarios"

    data = {}

    # Canonical party->bloc mapping (single source of truth — see finding #4:
    # WAWASAN was previously hand-mapped to the wrong bloc in a local dict).
    party_bloc = _load_canonical_party_bloc()

    # Required-contract-input gaps: logged loudly here so a downstream
    # publisher validation failure (e.g. "master has 0 seats, expected 222")
    # is traceable to its root cause instead of a silent empty-list default
    # (see finding #7 — required inputs must fail closed, not quietly
    # degrade). Optional PRN display inputs remain allowed to be absent. The
    # canonical news feed is required because release provenance hashes it;
    # malformed or schema-invalid bytes must therefore block release output.
    data["_input_gaps"] = []

    def _flag_gap(label: str, path: Path, reason: str) -> None:
        data["_input_gaps"].append({"field": label, "path": str(path), "reason": reason})
        logging.getLogger("build_app_data").error(
            "REQUIRED INPUT MISSING/UNPARSEABLE: %s (%s) — %s", label, path, reason
        )

    # 1. Master list 222 seats
    master_path = R / "master-list-222-parliamentary-seats.csv"
    m = _read_csv_safe(master_path)
    if m is None:
        _flag_gap("master (222 seats)", master_path, "file missing or failed to parse as CSV")
    data["master"] = m.to_dict("records") if m is not None else []

    # 2/3/5/6/9/15/17. Stripped in legacy for bundle size — keep stripped here too.
    data["ge15"] = []
    data["battlegrounds"] = []
    data["demographics"] = []
    data["dun"] = {}
    data["swings"] = []
    data["state_deepdives"] = {}
    data["state_reports"] = {}
    data["changelog"] = []

    # 4. Projection (base) — engine output is source of truth
    engine_out = source_root / "02_FORECAST" / "outputs" / "latest" / "ge16-forecast-latest.json"
    fc, forecast_error = _load_forecast_contract(engine_out)
    g = _read_csv_safe(R / "ge15-results-by-constituency-full.csv")
    proj_records = fc.get("projected_seats", []) if fc is not None else []
    if fc is not None:
        margin_lookup = {}
        if g is not None:
            for _, gr in g.iterrows():
                code_n = str(gr["code"]).replace("P", "").replace("p", "").zfill(3)
                margin_lookup[code_n] = gr.get("margin_pct_valid")
        norm = []
        for r in proj_records:
            code = r.get("code")
            code_n = str(code).replace("P", "").replace("p", "").zfill(3) if code else None
            norm.append({
                "code": code,
                "winner_ge15": r.get("ge15_winner") or r.get("winner_ge15"),
                "runnerup": r.get("runnerup"),
                "margin_pct_ge15": r.get("margin_pct_ge15") or margin_lookup.get(code_n),
                "proj_winner": r.get("proj_winner"),
                "proj_margin": r.get("proj_margin"),
                "flip": bool(r.get("flip", False)),
                "bersatu_held": bool(r.get("bersatu_held", False)),
            })
        data["projection"] = norm
    elif forecast_error is not None:
        # A present engine file is authoritative. Its failure is a required
        # input gap; never silently substitute a CSV projection for it.
        _flag_gap(
            "forecast engine output", engine_out,
            f"present but invalid forecast contract: {forecast_error}",
        )
        data["projection"] = []
    else:
        pj_path = P / "ge16-projection-model.csv"
        fallback_records, fallback_error = _validated_projection_csv(pj_path)
        if fallback_error is not None:
            _flag_gap(
                "projection (fallback)", pj_path,
                f"forecast JSON absent and fallback CSV invalid: {fallback_error}",
            )
            data["projection"] = []
        else:
            data["projection"] = fallback_records
            data["projection_provenance"] = {
                "kind": "validated_csv_fallback", "path": str(pj_path),
            }

    # 7. PRN projections per state (base scenario)
    for sn, sl in _PRN_STATES:
        path = roots.states / f"DUN {sn}" / f"{sl}-prn-projection.csv"
        df = _read_csv_safe(path)
        data[f"{sl}_prn"] = df.to_dict("records") if df is not None else []

    # 7b. PRN scenario compositions — live per-state JSON, fallback to frozen legacy
    for sn, sl in _PRN_STATES:
        live_path = roots.states / f"DUN {sn}" / f"{sl}-state-scenarios.json"
        rows = []
        live_d = _load_json_safe(live_path)
        if live_d:
            for s in live_d.get("scenarios", []):
                blocks = {b: s[b] for b in ("BN", "PH", "PN", "GPS", "GRS", "WARISAN", "IND", "MUDA") if s.get(b)}
                rows.append({
                    "label": s.get("scenario", "").replace("[Fed] ", "").replace("[State] ", ""),
                    "full": s.get("scenario", ""),
                    "type": s.get("category", "parametric"),
                    "govt": s.get("BN", 0) + s.get("GPS", 0) + s.get("GRS", 0) + s.get("WARISAN", 0),
                    "flips": s.get("flips", 0),
                    "blocks": blocks,
                })
        if not rows:
            prn_scen = _load_json_safe(A / "prn_scenarios.json", default={})
            rows = prn_scen.get(sl, [])
        data[f"{sl}_scenarios"] = rows

    # 8. DUN national summary
    data["dun_national"] = _DUN_NATIONAL

    # 10. Scenario aggregates
    scen = _load_json_safe(A / "projection_scenarios.json", default={})
    data["scenarios"] = scen

    # 11. Source registry
    data["sources"] = _SOURCES

    # 12. DUN election calendar
    data["dun_schedule"] = _DUN_SCHEDULE

    # 13. Weekly history
    mc = fc.get("monte_carlo", {}) if fc else {}
    engine_hist = []
    if fc:
        engine_hist = [{
            "week": "2026-08-04",
            "govt_p50": int(mc.get("P50", 0)),
            "govt_p10": int(mc.get("P10", 0)),
            "govt_p90": int(mc.get("P90", 0)),
            "majority_pct": int(mc.get("P_majority", 0) * 100),
            "flips": int(mc.get("flips_P50", 0)),
            "econ_term": fc.get("economic_term", 0),
            "note": "Full build: boundary-grouped vote-weighted DUN swings + ethnic continuous mod + party-level peaks + Bersatu split effect. PH+BN+GPS+GRS+WAR framework.",
        }]
    data["history"] = engine_hist + [
        {"week": "2026-08-03", "govt_p50": 150, "govt_p10": 149, "govt_p90": 152, "majority_pct": 100,
         "flips": 8, "econ_term": 4.59, "note": "Baseline \u2014 first published projection (v1.0)"},
    ]

    # 14. Updates (kept minimal — a real per-delivery changelog is a separate concern)
    data["updates"] = [{
        "date": generated_clock.date().isoformat(),
        "items": ["V2 delivery pipeline: composed app-data payload now published as portable JSON (data/app-data.json)."],
    }]

    # 14b. General news feed
    news = []
    news_json = roots.trackers / "ge16-news-feed.json"
    feed, feed_error = _load_news_feed_contract(news_json)
    if feed_error is not None:
        _flag_gap("general news feed", news_json, feed_error)
    if feed is not None:
        for it in feed["items"]:
            news.append({
                "query": it.get("query", ""), "title": it.get("title", ""),
                "date": str(it.get("date") or "")[:10], "source": it.get("source", ""),
                "link": it.get("link", ""), "lang": it.get("lang", "ms"),
                "category": it.get("category", ""), "blocs": it.get("blocs", []) or [],
                "states": it.get("states", []) or [], "chambers": it.get("chambers", []) or [],
                "dun_seats": it.get("dun_seats", []) or [], "parties": it.get("parties", []) or [],
                "event_date": str(it.get("event_date") or "")[:10],
                "source_url": it.get("source_url") or it.get("link", ""),
            })
        news.sort(key=lambda x: x.get("date", ""), reverse=True)
    data["general_news"] = news[:120]
    data["general_news_window_days"] = 7

    # 15c. state_proj availability map
    data["state_proj"] = {
        "Melaka": "melaka_prn", "Sarawak": "sarawak_prn", "Pahang": "pahang_prn",
        "Perak": "perak_prn", "Perlis": "perlis_prn",
    }

    # 16. Acknowledgements
    data["acknowledgements"] = _ACKNOWLEDGEMENTS

    # ---- summary ----
    summary = {}
    summary["govt_p50"] = int(mc.get("P50", 0))
    summary["govt_p10"] = int(mc.get("P10", 0))
    summary["govt_p90"] = int(mc.get("P90", 0))
    summary["majority_pct"] = int(mc.get("P_majority", 0) * 100)
    summary["flips"] = int(mc.get("flips_P50", 0))
    summary["econ_term"] = round(fc.get("economic_term", 0), 2) if fc else 0
    summary["updated"] = explicit_generated_at

    scen0 = data["scenarios"].get("Base (swings)") or {}
    if not scen0:
        # Defensive fallback only if the canonical name is ever renamed —
        # never silently pick an arbitrary index (see finding #6: insertion
        # order is not a stable substitute for identity).
        scen0 = next(iter(data["scenarios"].values()), {})
    summary["coalition"] = {b: scen0.get(b, 0) for b in ("PH", "BN", "PN", "GPS", "GRS", "WARISAN")}
    summary["coalition_text"] = "PH %d \u00b7 BN %d \u00b7 PN %d \u00b7 GPS %d \u00b7 GRS %d \u00b7 WARISAN %d" % (
        summary["coalition"]["PH"], summary["coalition"]["BN"], summary["coalition"]["PN"],
        summary["coalition"]["GPS"], summary["coalition"]["GRS"], summary["coalition"]["WARISAN"],
    )

    scen_meta = _load_json_safe(A / "scenario_meta.json", default={})
    if not isinstance(scen_meta, dict):
        scen_meta = {}

    def _scenario_meta_for(scenario_key: str) -> dict:
        metadata = scen_meta.get(scenario_key, {})
        return metadata if isinstance(metadata, dict) else {}

    def _scenario_category_for(scenario_key: str) -> str:
        # Categories control aggregation. Keep the delivery contract closed to
        # the two supported kinds instead of exposing arbitrary metadata prose.
        return (
            "narrative"
            if _scenario_meta_for(scenario_key).get("category") == "narrative"
            else "parametric"
        )

    def _govt_blocs_for(scenario_key: str) -> tuple:
        """Government-coalition bloc membership for a given scenario. Most
        scenarios use the standard PH-led coalition; a scenario may override
        this via scenario_meta.json['govt_blocs'] when its own composition
        differs (see finding #5 — do not hardcode one fixed formula for
        every scenario; a BN-PN pact scenario has NO PH in government)."""
        override = _scenario_meta_for(scenario_key).get("govt_blocs")
        if isinstance(override, list) and all(isinstance(bloc, str) for bloc in override):
            return tuple(override)
        return ("PH", "BN", "GPS", "GRS", "WARISAN", "MUDA", "KDM", "PBM")

    parametric_govt_vals = []
    non_parametric_governing_totals = []
    for scenario_key, scenario in data["scenarios"].items():
        governing_total = sum(scenario.get(bloc, 0) for bloc in _govt_blocs_for(scenario_key))
        if _scenario_category_for(scenario_key) == "parametric":
            parametric_govt_vals.append(governing_total)
        else:
            non_parametric_governing_totals.append({
                "full": scenario_key, "governing_total": governing_total,
            })
    summary["govt_range"] = (
        [min(parametric_govt_vals), max(parametric_govt_vals)]
        if parametric_govt_vals else [0, 0]
    )
    summary["non_parametric_governing_totals"] = non_parametric_governing_totals

    summary["flips_list"] = [p for p in data["projection"] if p.get("flip")]

    flip_dirs = {}
    for p in summary["flips_list"]:
        k = "%s \u2192 %s" % (p.get("winner_ge15", "?"), p.get("proj_winner", "?"))
        flip_dirs[k] = flip_dirs.get(k, 0) + 1
    summary["flip_summary"] = ", ".join("%d %s" % (v, k) for k, v in sorted(flip_dirs.items(), key=lambda x: -x[1]))

    summary["tight_seats"] = []
    for p in sorted(summary["flips_list"], key=lambda x: abs(x.get("margin_pct_ge15") or 99))[:3]:
        mm = next((mm for mm in data["master"] if str(mm.get("code")) == str(p.get("code"))), {})
        summary["tight_seats"].append({
            "code": p.get("code"), "constituency": mm.get("constituency", ""),
            "state": mm.get("state", ""), "margin": round(p.get("margin_pct_ge15") or 0, 1),
        })

    summary["scenarios_display"] = []
    for k, s in data["scenarios"].items():
        governing_blocs = list(_govt_blocs_for(k))
        gtot = sum(s.get(b, 0) for b in governing_blocs)
        summary["scenarios_display"].append({
            "label": _SHORT.get(k, k), "full": k, "govt": gtot,
            "governing_total": gtot, "governing_blocs": governing_blocs,
            "category": _scenario_category_for(k),
            "PH": s.get("PH", 0), "BN": s.get("BN", 0), "PN": s.get("PN", 0),
            "GPS": s.get("GPS", 0), "GRS": s.get("GRS", 0), "WAR": s.get("WARISAN", 0),
            "DAP": s.get("DAP", 0), "BDP": s.get("BERSAMA+DAP", 0),
            "other": sum(s.get(b, 0) for b in ("MUDA", "KDM", "PBM", "IND", "BERSAMA")),
        })

    summary["scenario_descriptions"] = []
    for k, s in data["scenarios"].items():
        governing_blocs = list(_govt_blocs_for(k))
        governing_total = sum(s.get(bloc, 0) for bloc in governing_blocs)
        summary["scenario_descriptions"].append({
            "label": _SHORT.get(k, k), "full": k,
            # Metadata prose can contain stale, independent numerical claims.
            # Publish only labels and values derived from canonical scenarios.
            "ms": _SHORT.get(k, k), "en": _SHORT.get(k, k),
            "detail_ms": "", "detail_en": "",
            "category": _scenario_category_for(k),
            "derivation": _scenario_category_for(k),
            "governing_blocs": governing_blocs,
            "governing_total": governing_total,
        })

    summary["govt_blocs"] = ["PH", "BN", "GPS", "GRS", "WARISAN", "MUDA", "KDM", "PBM"]
    summary["opp_blocs"] = ["PN", "DAP", "BERSAMA+DAP", "BERSAMA", "IND"]
    summary["bloc_colors"] = dict(_BLOC_COLORS)
    summary["party_colors"] = dict(_PARTY_COLORS)
    summary["party_bloc"] = dict(party_bloc)

    summary["scenario_deltas"] = []
    baseline = next((s for k, s in data["scenarios"].items() if "status quo" in k.lower()), {})
    all_blocs = ["PH", "BN", "PN", "GPS", "GRS", "WARISAN", "MUDA", "KDM", "PBM", "IND", "DAP", "BERSAMA", "BERSAMA+DAP"]
    for k, s in data["scenarios"].items():
        delta = {}
        for b in all_blocs:
            diff = s.get(b, 0) - (baseline.get(b, 0) if baseline else 0)
            if diff != 0:
                delta[b] = diff
        summary["scenario_deltas"].append({"label": _SHORT.get(k, k), "full": k, "delta": delta})

    prn_upcoming, prn_recent = [], []
    for d in data["dun_schedule"]:
        dn = next((x for x in data["dun_national"] if x.get("state") == d.get("state")), {})
        rec = {
            "state": d.get("state"), "days": None, "seats": dn.get("seats"),
            "gov": dn.get("gov"), "dissolution": d.get("dissolution"),
            "last_election": d.get("last_election"), "status": d.get("status"),
        }
        if rec["dissolution"]:
            try:
                parts = str(rec["dissolution"]).split("-")
                target = date(int(parts[0]), int(parts[1]), int(parts[2]))
                rec["days"] = (target - generated_clock.date()).days
            except Exception:
                rec["days"] = None
        (prn_upcoming if d.get("status") == "upcoming" else prn_recent).append(rec)
    summary["prn_upcoming"] = prn_upcoming
    summary["prn_recent"] = prn_recent[:4]
    summary["report_outline"] = []

    data["summary"] = summary
    return data


def build_forecast_json(app_data: dict, fc: dict) -> dict:
    """Stable published forecast contract, derived from app_data + raw engine output."""
    s = app_data["summary"]
    return {
        "updated": s["updated"],
        "govt_p50": s["govt_p50"], "govt_p10": s["govt_p10"], "govt_p90": s["govt_p90"],
        "majority_pct": s["majority_pct"], "flips": s["flips"], "econ_term": s["econ_term"],
        "coalition": s["coalition"], "coalition_text": s["coalition_text"],
        "govt_blocs": s["govt_blocs"], "opp_blocs": s["opp_blocs"],
        "bloc_colors": s["bloc_colors"], "party_colors": s["party_colors"], "party_bloc": s["party_bloc"],
        "history": app_data["history"],
        "flips_list": s["flips_list"],
        "tight_seats": s["tight_seats"],
    }


def build_scenarios_json(app_data: dict) -> dict:
    s = app_data["summary"]
    return {
        "scenarios_display": s["scenarios_display"],
        "scenario_descriptions": s["scenario_descriptions"],
        "scenario_deltas": s["scenario_deltas"],
        "scenarios": app_data["scenarios"],
    }
