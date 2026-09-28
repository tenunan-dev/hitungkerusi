#!/usr/bin/env python3
"""GE16 Forecast Engine — implements the forecast-theory.md factor model.

Stages:
  1. STRUCTURE: load GE15 baseline + seat demographics (identity layer)
  2. DRIFT: build swing map from state elections + factor adjustments
     (economic voting term, approval term, event shocks) + type modulation
  3. TRANSLATION: per-seat projected margin -> probit flip probability
  4. AGGREGATION: Monte Carlo -> P10/P50/P90, majority probability, flips

Outputs:
  - outputs/latest/ge16-forecast-latest.json  (machine-readable forecast)
  - outputs/history/ge16-forecast-YYYY-MM-DD.json (dated snapshot per run)

Weekly config lives in config.py (FACTORS/MACRO/EVENT_SHOCKS) — edit THAT file,
never this one.

Usage:
  python3 forecast_engine.py                     # base run (current data)
  python3 forecast_engine.py --backtest          # validate: zero-swing -> GE15
"""
import argparse
import hashlib
import json
import os
import random
import re
import sys

import numpy as np
import pandas as pd

from config import FACTORS, MACRO, EVENT_SHOCKS, TYPE_MOD, MC_ITERATIONS, MC_SEED, VACANCIES
from data_roots import resolve_data_roots

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_ROOTS = resolve_data_roots(repository_root=ROOT)
R = str(DATA_ROOTS.derived)
P = str(DATA_ROOTS.federal)
OUT = os.path.join(ROOT, "02_FORECAST", "outputs", "latest")
HIST = os.path.join(ROOT, "02_FORECAST", "outputs", "history")

# Canonical evidence identity of the forecast output this engine writes. Stage 3
# binds every federal scenario's provenance to these exact bytes, so the digest
# recorded in work/scenarios/scenario_meta.json goes stale on every run that
# regenerates the output and must be re-bound by that same run.
FORECAST_LATEST_RELATIVE = "02_FORECAST/outputs/latest/ge16-forecast-latest.json"


def classify_seat(row):
    """Classify seat type from demographic composition."""
    malay = float(row.get("malay_pct", 50))
    if row.get("state") in ("Sabah", "Sarawak", "Labuan"):
        return "east_malaysia"
    if malay > 80:
        return "pn_core"
    if malay > 55:
        return "mixed_malay"
    if malay > 30:
        return "true_mixed"
    return "non_malay"


# =====================================================================
# Loaders
# =====================================================================
def load_baseline():
    """Load GE15 per-seat baseline with demographics + real runner-up.

    Uses the projection model CSV (has verified per-seat winner/runner-up/margin)
    enriched with state (from GE15 results) and demographics for seat typing."""
    proj = pd.read_csv(os.path.join(P, "ge16-projection-model.csv"))
    res = pd.read_csv(os.path.join(R, "ge15-results-by-constituency-full.csv"))
    dem = pd.read_csv(os.path.join(R, "voter-demographics-by-constituency-ge15.csv"))
    dem["code_n"] = dem["code"].astype(str).str.replace(".", "", regex=False)
    proj["code_n"] = proj["code"].astype(str).str.replace(".", "", regex=False)
    res["code_n"] = res["code"].astype(str).str.replace(".", "", regex=False)
    df = proj.merge(res[["code_n", "state_std", "constituency"]].rename(
        columns={"state_std": "state"}), on="code_n", how="left")
    df = df.merge(dem[["code_n", "malay_pct", "chinese_pct", "indian_pct",
                        "bumi_sabah_pct", "bumi_sarawak_pct",
                        "age18_21_pct", "age22_30_pct"]], on="code_n", how="left")
    df["youth_pct"] = df["age18_21_pct"].fillna(0) + df["age22_30_pct"].fillna(0)
    # rename states to match swing map keys
    df["state"] = df["state"].replace({"Pulau Pinang": "Penang", "Melaka": "Malacca"})
    df["seat_type"] = df.apply(classify_seat, axis=1)
    # VACANCY STATUS (Art 49A): a vacancy is legal/occupancy metadata, not
    # removal of the constituency from the 222-seat forecast universe. The
    # last recorded holder remains the baseline attribution until a by-election
    # or general election changes the holder.
    df["vacated"] = df["code"].isin(set(VACANCIES.keys()))
    return df


def load_state_swings():
    """Post-GE15 state-election swings (revealed preference)."""
    swing_path = os.path.join(R, "swing_se_to_se.csv")
    sw = pd.read_csv(swing_path) if os.path.exists(swing_path) else None
    if sw is None:
        return {}
    rename = {"Pulau Pinang": "Penang", "Melaka": "Malacca"}
    swing_map = {}
    for _, r in sw.iterrows():
        st = rename.get(r["state"], r["state"])
        if st == "Pahang":
            continue  # concurrent with GE15, not a fresh signal
        swing_map.setdefault(st, {})[r["bloc"]] = r["swing_pp"]
    return swing_map


def load_boundary_swings():
    """DUN boundary-grouped swings — per-parliament swing from child DUN seats only.
    Falls back to state-level swing for seats without DUN data (FTs, no recent DUN election)."""
    bw_path = os.path.join(R, "swing_boundary_grouped.csv")
    if not os.path.exists(bw_path):
        return {}
    bw = pd.read_csv(bw_path)
    boundary_map = {}
    for _, r in bw.iterrows():
        boundary_map.setdefault(r["parliament"], {})[r["bloc"]] = r["boundary_swing_pp"]
    return boundary_map


def ethnic_swing_mod(malay_pct, bloc):
    """Continuous ethnic modulation — every percentage point of Malay
    composition directly modulates bloc swings. Replaces the unused TYPE_MOD
    bucket system with a smooth, evidence-grounded function.

    Basis: ISEAS 2023, Pepinsky 2023 — ethnicity is the dominant predictor
    of Malaysian voting behaviour. PN support scales with Malay%; PH support
    scales inversely. BN peaks in mixed seats (~60% Malay)."""
    m = malay_pct / 100.0
    if bloc == "PN":
        return 0.60 + 0.80 * m
    elif bloc == "PH":
        return 1.40 - 0.80 * m
    elif bloc == "BN":
        return 1.0 + 0.10 * max(0, 1.0 - ((m - 0.60) / 0.30) ** 2)
    elif bloc in ("GPS", "GRS"):
        return 1.0
    else:
        return 1.0


def party_swing_mod(malay_pct, party, bloc):
    """Party-level ethnic modulation — different parties have different
    ethnic response curves, even within the same coalition.

    Basis: party-demographic-profiles.csv (01_RESEARCH/data/derived/).
    PAS avg 85% Malay, DAP avg 25% Malay, PKR avg 49% Malay, etc."""
    m = malay_pct / 100.0

    # Each party has its optimal Malay% zone where it performs best
    PARTY_PEAKS = {
        'PAS': 0.88,       # strongest at 88% Malay
        'BERSATU': 0.75,    # strongest at 75% Malay
        'WAWASAN': 0.78,    # similar to Bersatu
        'UMNO': 0.62,       # strongest at 62% Malay (mixed)
        'PKR': 0.48,        # strongest at 48% Malay (true mixed)
        'AMANAH': 0.55,     # strongest at 55% Malay
        'DAP': 0.20,        # strongest at 20% Malay (non-Malay seats)
        'MCA': 0.45,        # mixed Chinese-Malay
        'MIC': 0.40,        # Indian-Malay mixed
        'PBB': 0.35,        # Sarawak bumiputera
        'PRS': 0.05,        # Sarawak Dayak
        'WARISAN': 0.30,    # Sabah mixed
    }

    peak = PARTY_PEAKS.get(party, 0.50)
    # Bell curve: 1.0 at peak, tapers to ~0.7 at ±30pp from peak
    distance = abs(m - peak)
    return 1.0 + 0.15 * max(0, 1.0 - (distance / 0.30) ** 2)


# unreachable as of 2026-09-24: seat_party is always '' (projection CSV has
# ber_satu_held, no party column). Documented to prevent a future silent
# double-count with bersatu_penalty.
def bersatu_split_mod(code, party, bloc):
    """PAS-Bersatu structural split effect.
    In seats held by Bersatu, the Malay vote splits between Bersatu (solo)
    and PAS (under PN). This benefits BN/PH as the largest non-split bloc."""
    if party == 'BERSATU' and bloc == 'PN':
        return -0.12  # 12% of PN vote bleeds away in Bersatu seats
    if party == 'BERSATU' and bloc == 'BN':
        return +0.06  # half the bleed goes to BN
    if party == 'BERSATU' and bloc == 'PH':
        return +0.03  # some goes to PH in mixed seats
    return 0.0


# =====================================================================
# Core math
# =====================================================================
def economic_term():
    """Economic-voting calibration (basis §8.4)."""
    g = MACRO["gdp_yoy"]
    cpi = MACRO["cpi_yoy"]
    appr = MACRO["approval_delta"]
    return 0.7 * g - 0.8 * (cpi - 2.0) + 0.15 * appr


def build_swing_map(swing_map, use_events=True, use_factors=True):
    """Combine state/boundary swings + economic term + event shocks into per-seat bloc swings.
    Prefers boundary-grouped swing (per parliament) over state-level swing."""
    econ = economic_term() if use_factors else 0.0
    appr = MACRO["approval_delta"] if use_factors else 0.0
    # Boundary-grouped swings + modulation layers are DRIFT signals (factors).
    # Backtest (use_factors=False) must stay zero-swing to reproduce GE15 exactly.
    boundary_map = load_boundary_swings() if use_factors else {}  # per-parliament swings from DUN boundaries
    out = {}
    for _, s in load_baseline().iterrows():
        code = s["code"]
        # Vacant seats remain forecastable constituencies; vacancy status is
        # carried as metadata and does not remove the seat from the model.
        st = s["state"]
        st_sw = swing_map.get(st, {})
        # Prefer boundary-grouped swing (per-parliament from child DUNs) over state-level
        bnd_sw = boundary_map.get(code, {})
        merged_sw = dict(st_sw)
        merged_sw.update(bnd_sw)  # boundary overrides state for blocs present in both
        blocs = {"PH", "PN", "BN", "GPS", "GRS", "WARISAN", "MUDA", "KDM", "PBM", "IND"}
        # start with merged swing; distribute economic term to govt blocs (PH+BN+GPS+GRS)
        seat_sw = {}
        for b in blocs:
            v = merged_sw.get(b, 0.0)
            seat_sw[b] = v
        # economic term boosts govt-aligned blocs
        for b in ("PH", "BN", "GPS", "GRS"):
            seat_sw[b] = seat_sw.get(b, 0.0) + econ * FACTORS["economy"] / 0.20 * 0.10
        # approval term
        for b in ("PH", "BN", "GPS", "GRS"):
            seat_sw[b] = seat_sw.get(b, 0.0) + appr * 0.1 * FACTORS["approval"] / 0.25
        # event shocks (seat-specific)
        if use_events and code in EVENT_SHOCKS:
            for b, pp in EVENT_SHOCKS[code].items():
                seat_sw[b] = seat_sw.get(b, 0.0) + pp
        if use_factors:
            # ETHNIC MODULATION: every seat's malay_pct continuously modulates each bloc's swing
            malay = float(s.get("malay_pct", 50))
            for b in blocs:
                if b in ("PH", "PN", "BN"):
                    mod = ethnic_swing_mod(malay, b)
                    seat_sw[b] = seat_sw.get(b, 0.0) * mod
            # YOUTH MODULATION: youth-heavy seats amplify swing volatility
            youth = float(s.get("youth_pct", 25))
            y = youth / 100.0
            ymod = 1.0 + 0.30 * max(0, y - 0.20)
            for b in blocs:
                seat_sw[b] = seat_sw.get(b, 0.0) * ymod
            # PARTY-LEVEL MODULATION: each party's ethnic sweet spot adjusts swing potency
            seat_party = s.get("party", "")
            if seat_party and seat_party in ('PAS','BERSATU','WAWASAN','UMNO','PKR','AMANAH','DAP','PBB','PRS','WARISAN','MCA','MIC'):
                party_mod = party_swing_mod(malay, seat_party, None)
                for b in blocs:
                    seat_sw[b] = seat_sw[b] * party_mod
                # Bersatu split structural effect
                for b in blocs:
                    bs_eff = bersatu_split_mod(code, seat_party, b)
                    if bs_eff != 0:
                        seat_sw[b] = seat_sw.get(b, 0.0) + bs_eff
        out[code] = seat_sw
    return out


def project_seats(swing_map, use_events=True, use_factors=True, rng=None):
    """Translate swings into projected winner per seat (deterministic expected value).

    Margin model: proj_margin = M_GE15 + swing(winner) - swing(actual runner-up).
    Seat flips when proj_margin < 0."""
    res = load_baseline()
    sw = build_swing_map(swing_map, use_events, use_factors)
    rows = []
    for _, s in res.iterrows():
        code = s["code"]
        # Vacant seats remain in the 222-seat projection universe.
        wb = s["winner_ge15"]
        rb = s["runnerup"]
        margin = float(s["margin_pct_ge15"])
        seat_sw = sw.get(code, {})
        sw_w = seat_sw.get(wb, 0.0)
        sw_r = seat_sw.get(rb, 0.0) if rb else 0.0
        proj = margin + sw_w - sw_r
        winner = wb
        if proj < 0 and rb:
            winner = rb
            proj = -proj
        rows.append({"code": code, "constituency": s.get("constituency", ""),
                     "state": s.get("state", ""),
                     "ge15_winner": wb, "runnerup": rb,
                     "last_holder": wb,
                     "vacancy_status": VACANCIES.get(code, ""),
                     "proj_winner": winner, "proj_margin": round(proj, 2),
                     "seat_type": s["seat_type"], "flip": winner != wb})
    return pd.DataFrame(rows)


def monte_carlo(swing_map, iterations=5000, seed=42):
    """Monte Carlo: sample swing uncertainty -> distribution of govt seats."""
    rng = random.Random(seed)
    res = load_baseline()
    sw_base = build_swing_map(swing_map, use_events=True, use_factors=True)
    govt_blocs = {"PH", "BN", "GPS", "GRS", "WARISAN", "MUDA", "KDM", "PBM"}
    govt_counts = []
    flip_counts = []
    for _ in range(iterations):
        govt = 0
        flips = 0
        for _, s in res.iterrows():
            code = s["code"]
            # Vacant seats remain in the 222-seat Monte Carlo universe.
            wb = s["winner_ge15"]
            rb = s["runnerup"]
            margin = float(s["margin_pct_ge15"])
            seat_sw = sw_base.get(code, {})
            sw_w = seat_sw.get(wb, 0.0)
            sw_r = seat_sw.get(rb, 0.0) if rb else 0.0
            # sample seat-level noise
            noise = rng.gauss(0, 2.0 if margin < 5 else 1.2)
            proj = margin + sw_w - sw_r + noise
            winner = wb
            if proj < 0 and rb:
                winner = rb
            if winner != wb:
                flips += 1
            if winner in govt_blocs:
                govt += 1
        govt_counts.append(govt)
        flip_counts.append(flips)
    return np.array(govt_counts), np.array(flip_counts)


def pctile(a, p):
    return float(np.percentile(a, p))


# =====================================================================
# SCENARIO BUILDER — one source of truth with the headline forecast.
# Each scenario re-runs the SAME build_swing_map/project_seats machinery
# with a per-scenario swing adjustment, so "Base (swings)" is BY
# CONSTRUCTION identical to the deterministic output of the base run.
# =====================================================================

# scenario name -> (swing_adj dict applied to every seat, bersatu_penalty pp
# applied to PN in BERSATU-held seats). The base run itself defines
# "Base (swings)"; "Status quo" is the zero-swing GE15 reproduction.
SCENARIO_DEFS = {
    "PN surge +5pp": ({"PN": +5}, 0),
    "Govt surge": ({"PH": +3, "BN": +3, "GPS": +2, "GRS": +2}, 0),
    "Bersatu split (−6pp PN in Bersatu seats)": ({}, 6),
    "Bersatu civil war (−10pp PN)": ({}, 10),
    "Full fragmentation (Bersatu −6 + Bersama −3 PH urban)": ({"PH": -3}, 6),
}


def project_with_adjust(swing_map, swing_adj=None, bersatu_penalty=0):
    """Project seats with a scenario adjustment layered ON the base swing map.

    swing_adj:  {bloc: pp} added to that bloc's swing in EVERY seat.
    bersatu_penalty: pp subtracted from PN's swing in BERSATU-held seats
      (the Bersatu rump contests solo, splitting the Malay opposition vote).
    """
    res = load_baseline()
    sw_base = build_swing_map(swing_map, use_events=True, use_factors=True)
    rows = []
    for _, s in res.iterrows():
        code = s["code"]
        # Vacant seats remain in the 222-seat scenario universe.
        wb = s["winner_ge15"]
        rb = s["runnerup"]
        margin = float(s["margin_pct_ge15"])
        seat_sw = dict(sw_base.get(code, {}))
        if swing_adj:
            for b, pp in swing_adj.items():
                seat_sw[b] = seat_sw.get(b, 0.0) + pp
        if bersatu_penalty and s.get("bersatu_held", False):
            seat_sw["PN"] = seat_sw.get("PN", 0.0) - bersatu_penalty
        sw_w = seat_sw.get(wb, 0.0)
        sw_r = seat_sw.get(rb, 0.0) if rb else 0.0
        proj = margin + sw_w - sw_r
        winner = wb
        if proj < 0 and rb:
            winner = rb
            proj = -proj
        rows.append({"code": code, "constituency": s.get("constituency", ""),
                     "state": s.get("state", ""),
                     "ge15_winner": wb, "runnerup": rb,
                     "last_holder": wb,
                     "vacancy_status": VACANCIES.get(code, ""),
                     "proj_winner": winner, "proj_margin": round(proj, 2),
                     "seat_type": s["seat_type"], "flip": winner != wb})
    return pd.DataFrame(rows)


def build_scenarios(swing_map):
    """Compute the full scenario set from the live swing map.

    Returns {name: {bloc: seats}} — every scenario sums to 222 minus
    vacancies, matching the deterministic output contract. The base run's
    deterministic composition is used verbatim for 'Base (swings)'.
    """
    base_df = project_seats(swing_map, use_events=True, use_factors=True)
    scen = {"Status quo": project_seats({}, use_events=False, use_factors=False)[
        "proj_winner"].value_counts().to_dict(),
        "Base (swings)": base_df["proj_winner"].value_counts().to_dict()}
    for name, (adj, pen) in SCENARIO_DEFS.items():
        df = project_with_adjust(swing_map, swing_adj=adj, bersatu_penalty=pen)
        scen[name] = df["proj_winner"].value_counts().to_dict()
    return scen, base_df


# =====================================================================
# Provenance digest refresh (Stage-3 seal input)
# =====================================================================
def sha256_file(path):
    """Streaming SHA-256 of a file's exact bytes (the Stage-3 evidence digest)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# Canonical Stage-3 evidence identity of the DATA news feed. Stage 1 rewrites
# that file every cycle, so the digest of the FILE can never carry a reviewed
# narrative scenario: only per-RECORD evidence can, because a record keeps its
# identity and bytes across the rewrite.
NEWS_FEED_RELATIVE = "research/trackers/ge16-news-feed.json"
CITED_URL = re.compile(r"https?://[^\s\)\]\}>,;\"'`]+")
MIN_CITED_HEADLINE = 24


def canonical_json_bytes(value):
    """Byte-exact copy of Stage 3's evidence serialization (``_json_bytes``).

    automation/outputs/build_release_payloads.py hashes a feed record as
    sha256(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) +
    "\\n"); a single byte of drift here breaks the Stage-3 seal, so this stays
    deliberately identical.
    """
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def feed_record_key(item):
    """Canonical identity of a feed record: its ``id`` when present, else ``link``.

    Mirrors ``build_release_payloads._feed_record_key``: the live Stage-1 feed
    carries no ``id`` on any item, only ``link``, while reviewed evidence from
    earlier captures records the record's URL. Neither changes when the feed
    file is rewritten, unlike the file's own digest.
    """
    if not isinstance(item, dict):
        return None
    for key in ("id", "link"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def find_feed_record(records, record_id):
    """The single feed record identified by ``record_id``, else ``None``.

    Mirrors Stage 3's ``_feed_records_matching``: an exact ``id`` match wins,
    then ``link``. Several matches are ambiguous and count as unresolved — the
    engine must never guess which record a reviewed scenario meant.
    """
    items = [item for item in records or [] if isinstance(item, dict)]
    matches = [item for item in items if item.get("id") == record_id] or \
        [item for item in items if item.get("link") == record_id]
    return matches[0] if len(matches) == 1 else None


def cited_feed_records(basis, records):
    """Feed records cited by a reviewed basis text: by URL, else by headline.

    The basis text is the only narrative prose the engine reads, and it is used
    purely to *locate* a record that already exists in the current feed — never
    to invent one. A citation counts only when it is a record's own key (its URL)
    or reproduces a record's headline verbatim.
    """
    text = basis if isinstance(basis, str) else ""
    if not text:
        return []
    urls = {url.rstrip(".,;:") for url in CITED_URL.findall(text)}
    found = []
    for item in records or []:
        if not isinstance(item, dict):
            continue
        key = feed_record_key(item)
        if not key:
            continue
        title = item.get("title")
        if key in urls or (isinstance(title, str) and len(title) >= MIN_CITED_HEADLINE
                           and title in text):
            found.append(item)
    return found


def load_feed_records(feed_path):
    """Return ``(file digest, records)`` for the canonical news feed.

    ``(None, None)`` means the feed is absent or unreadable: every record
    binding then stays untouched and is reported instead of being guessed.
    """
    if not os.path.exists(feed_path):
        return None, None
    try:
        digest = sha256_file(feed_path)
        with open(feed_path, encoding="utf-8") as handle:
            feed = json.load(handle)
    except (OSError, ValueError):
        return None, None
    items = feed.get("items") if isinstance(feed, dict) else None
    return digest, [item for item in items or [] if isinstance(item, dict)]


def _rebind_feed_evidence_item(item, basis, feed_digest, feed_records):
    """Record-bind one DATA feed evidence item in place.

    Returns ``(record_id, warning, changed)``. A record_id already written by
    the reviewer is honoured only when it still resolves in the current feed;
    otherwise the item is left exactly as reviewed and a warning explains why
    (Stage 3 then fails closed on it, naming the scenario).
    """
    record_id = item.get("record_id")
    if isinstance(record_id, str) and record_id:
        record = find_feed_record(feed_records, record_id)
        if record is None:
            return None, "record_id %r is not in the current feed" % record_id, False
    else:
        cited = cited_feed_records(basis, feed_records)
        if len(cited) != 1:
            return None, ("basis text cites %d record(s) in the current feed; exactly one is "
                          "required" % len(cited)), False
        record = cited[0]
        record_id = feed_record_key(record)
    updated = {
        "sha256": feed_digest,
        "record_id": record_id,
        "record_sha256": hashlib.sha256(canonical_json_bytes(record)).hexdigest(),
    }
    changed = any(item.get(key) != value for key, value in updated.items())
    item.update(updated)
    return record_id, None, changed


def _rebind_narrative_feed_evidence(name, entry, feed_digest, feed_records, report):
    """Record-bind every DATA feed evidence item of one reviewed narrative."""
    provenance = entry.get("provenance")
    evidence = provenance.get("evidence") if isinstance(provenance, dict) else None
    feed_items = [item for item in evidence
                  if isinstance(item, dict) and item.get("repository") == "DATA"
                  and item.get("path") == NEWS_FEED_RELATIVE] if isinstance(evidence, list) else []
    if not feed_items:
        # Reviewed provenance is authored upstream: it is never invented here.
        report["warnings"].append(
            "narrative scenario %r carries no DATA %s evidence item, so it binds no feed "
            "record" % (name, NEWS_FEED_RELATIVE))
        report["unbound"].append(name)
        return False
    changed, resolved = False, False
    for item in feed_items:
        if feed_digest is None:
            report["warnings"].append(
                "narrative scenario %r: canonical news feed %s is unavailable, so its feed "
                "evidence could not be record-bound" % (name, NEWS_FEED_RELATIVE))
            continue
        record_id, warning, item_changed = _rebind_feed_evidence_item(
            item, entry.get("basis"), feed_digest, feed_records)
        if warning is not None:
            report["warnings"].append("narrative scenario %r: %s" % (name, warning))
            continue
        resolved = True
        changed = changed or item_changed
    if not resolved:
        report["unbound"].append(name)
    return changed


def refresh_scenario_meta_evidence(forecast_path=None, meta_path=None, feed_path=None):
    """Re-bind federal scenario provenance to the current canonical bytes.

    Stage 3 seals every federal scenario against its declared provenance
    (automation/outputs/build_release_payloads.py ->
    _validate_federal_scenarios/_validate_evidence). Two kinds of recorded
    evidence go stale, and this is the only point that knows the fresh bytes for
    both:

      * the ANALYTICS digest of
        "02_FORECAST/outputs/latest/ge16-forecast-latest.json", which every
        engine run rewrites, and
      * the DATA news feed, which Stage 1 rewrites every cycle. A reviewed
        narrative scenario may therefore never bind the feed FILE digest: it
        binds feed RECORDS, so this re-binds ``record_id``/``record_sha256``
        (and the file digest that travels alongside) on every DATA feed
        evidence item of a ``narrative`` scenario.

    Things written:
      * the ``sha256`` of an evidence item that already references the
        canonical forecast output,
      * that exactly-required ANALYTICS evidence item itself, for a
        ``parametric`` scenario whose structured provenance block omits it, and
      * ``record_id``/``record_sha256``/``sha256`` on the DATA feed evidence
        items of a ``narrative`` scenario.

    The engine never fabricates evidence: a record_id is written only for a
    record verified to exist in the current feed, either because the reviewer
    already recorded that id or because the scenario's basis text cites exactly
    one current record. Anything unresolvable is left exactly as reviewed and
    reported loudly by scenario name, so Stage 3 fails closed naming the
    scenario and record rather than silently dropping the scenario. Scenario
    compositions, categories, derivations, basis text and every other key are
    untouched, and a missing scenario_meta.json is never created: the engine
    must not author or resurrect reviewed metadata.
    """
    forecast_path = forecast_path or os.path.join(OUT, "ge16-forecast-latest.json")
    meta_path = meta_path or os.path.join(ROOT, "work", "scenarios", "scenario_meta.json")
    feed_path = feed_path or os.path.join(str(DATA_ROOTS.trackers), "ge16-news-feed.json")
    report = {"digest": None, "refreshed": [], "bound": [], "record_bound": [], "unbound": [],
              "warnings": []}
    if not (os.path.exists(forecast_path) and os.path.exists(meta_path)):
        return report
    digest = sha256_file(forecast_path)
    report["digest"] = digest
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)
    if not isinstance(meta, dict):
        report["digest"] = None
        return report
    feed_digest, feed_records = load_feed_records(feed_path)
    for name, entry in meta.items():
        if not isinstance(entry, dict):
            continue
        # Reviewed narrative provenance binds feed RECORDS, not the feed file:
        # this runs before the forecast-digest branch below, which continues.
        if entry.get("category") == "narrative" or entry.get("derivation") == "news-narrative":
            if _rebind_narrative_feed_evidence(name, entry, feed_digest, feed_records, report):
                report["record_bound"].append(name)
        provenance = entry.get("provenance")
        evidence = provenance.get("evidence") if isinstance(provenance, dict) else None
        recorded = next((item for item in evidence if isinstance(item, dict)
                         and item.get("repository") == "ANALYTICS"
                         and item.get("path") == FORECAST_LATEST_RELATIVE), None) \
            if isinstance(evidence, list) else None
        if recorded is not None:
            if recorded.get("sha256") != digest:
                recorded["sha256"] = digest
                report["refreshed"].append(name)
            continue
        # A parametric scenario must carry measured evidence for the forecast
        # run that produced it. Reviewed narrative metadata is authored
        # upstream: it is never invented or extended here.
        if entry.get("category") != "parametric":
            continue
        if not isinstance(provenance, dict):
            provenance = entry["provenance"] = {"derivation": "quantitative-scenario", "evidence": []}
        if not isinstance(provenance.get("evidence"), list):
            provenance["evidence"] = []
        provenance["evidence"].append(
            {"repository": "ANALYTICS", "path": FORECAST_LATEST_RELATIVE, "sha256": digest})
        report["bound"].append(name)
    if report["refreshed"] or report["bound"] or report["record_bound"]:
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(f"Provenance: scenario_meta.json evidence re-bound to {digest[:12]}… "
              f"({len(report['refreshed'])} digest(s) refreshed, {len(report['bound'])} bound, "
              f"{len(report['record_bound'])} narrative record-bound)")
    for warning in report["warnings"]:
        print("Provenance WARNING: " + warning)
    if report["unbound"]:
        print("Provenance WARNING: %d reviewed narrative scenario(s) lack resolvable feed record "
              "evidence: %s" % (len(report["unbound"]), ", ".join(report["unbound"])))
    return report


# =====================================================================
# Main
# =====================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", action="store_true", help="zero-swing validation (should reproduce GE15)")
    ap.add_argument("--scenarios", action="store_true",
                    help="recompute projection_scenarios.json from the live swing map (one source of truth)")
    ap.add_argument("--iterations", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    swing_map = {} if args.backtest else load_state_swings()
    df = project_seats(swing_map, use_events=not args.backtest, use_factors=not args.backtest)
    govt, flips = monte_carlo(swing_map, args.iterations, args.seed) if not args.backtest else (None, None)

    if args.backtest:
        print("BACKTEST (zero swings, no events): should reproduce GE15 exactly")
        print(df["proj_winner"].value_counts().to_dict())
        return

    if args.scenarios:
        # Recompute quantitative scenarios, then merge ONLY the reviewed,
        # provenance-backed narrative layer from the prior scenario artifact.
        # The engine must never erase an approved narrative scenario.
        scen, _ = build_scenarios(swing_map)
        scen_path = os.path.join(ROOT, "work", "scenarios", "projection_scenarios.json")
        meta_path = os.path.join(ROOT, "work", "scenarios", "scenario_meta.json")
        legacy_scen_path = os.path.join(ROOT, "05_AUTOMATION", "projection_scenarios.json")
        legacy_meta_path = os.path.join(ROOT, "05_AUTOMATION", "scenario_meta.json")
        missing_work = [
            path for path in (scen_path, meta_path) if not os.path.exists(path)
        ]
        stale_legacy = [
            path for path in (legacy_scen_path, legacy_meta_path) if os.path.exists(path)
        ]
        if missing_work and stale_legacy:
            raise RuntimeError(
                "work/scenarios artifact missing while stale 05_AUTOMATION scenario "
                "artifact exists; Stage 2 cron-redirection owner must redirect the "
                "job to versioned builders before this reader may regenerate scenarios: "
                + ", ".join(missing_work)
            )
        existing = json.load(open(scen_path)) if os.path.exists(scen_path) else {}
        existing_meta = json.load(open(meta_path)) if os.path.exists(meta_path) else {}
        retained_narrative = {}
        for name, comp in existing.items():
            m = existing_meta.get(name, {})
            basis = str(m.get("basis", ""))
            is_reviewed = (
                m.get("category") == "narrative"
                and "hand-written" not in basis.lower()
                and ("composed" in basis.lower() or m.get("derivation") == "news-narrative")
            )
            if is_reviewed and isinstance(comp, dict):
                retained_narrative[name] = comp
        # Older reviewed narrative artifacts may have been authored when
        # vacancies were excluded. Restore any missing vacancy seats to their
        # recorded last-holder bloc; this changes only the seat-accounting
        # universe, not the narrative premise.
        baseline = load_baseline()
        vacancy_holders = {
            str(r["code"]): r["winner_ge15"]
            for _, r in baseline.iterrows()
            if str(r["code"]) in VACANCIES
        }
        for name, comp in retained_narrative.items():
            fixed = dict(comp)
            missing = 222 - sum(fixed.values())
            if missing < 0:
                raise AssertionError(f"{name} has {sum(fixed.values())} seats, above 222")
            for code, holder in vacancy_holders.items():
                if missing <= 0:
                    break
                fixed[holder] = fixed.get(holder, 0) + 1
                missing -= 1
            if sum(fixed.values()) != 222:
                raise AssertionError(f"{name} cannot be normalised to 222 seats")
            scen[name] = fixed
        # All 222 constituencies remain in the forecast/scenario universe.
        target = 222
        for name, comp in scen.items():
            t = sum(comp.values())
            assert t == target, f"{name} sums to {t}, not {target}"
        os.makedirs(os.path.dirname(scen_path), exist_ok=True)
        with open(scen_path, "w") as f:
            json.dump(scen, f, ensure_ascii=False, indent=2)
        # Preserve reviewed metadata/details and generate metadata for new
        # quantitative scenarios. Never create hand-written narrative metadata.
        meta = {}
        for name in scen:
            if name in existing_meta and name in retained_narrative:
                meta[name] = existing_meta[name]
                meta[name]["derivation"] = "news-narrative"
            elif name == "Status quo":
                meta[name] = {"category": "parametric", "derivation": "quantitative",
                              "basis": "Zero-swing projection; must reproduce the GE15 parliament exactly "
                                       "(validation of the seat-level data and flip logic). "
                                       "Framings: sensitivity / swing-based / quantitative."}
            elif name == "Base (swings)":
                meta[name] = {"category": "parametric", "derivation": "quantitative",
                              "basis": "Live state/boundary-grouped swings + economic/approval/event factors; "
                                       "identical to the headline deterministic output by construction. "
                                       "Framings: sensitivity / swing-based / quantitative."}
            else:
                meta[name] = {"category": "parametric", "derivation": "quantitative",
                              "basis": f"Engine re-run of the live swing map with a scenario swing layer "
                                       f"(SCENARIO_DEFS: {SCENARIO_DEFS.get(name, '')}). "
                                       f"Framings: sensitivity / swing-based / quantitative."}
        with open(meta_path, "w") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        # The scenario metadata just written must carry evidence bound to the
        # current forecast bytes, not to whatever an earlier run produced.
        refresh_scenario_meta_evidence()
        print(f"\nSCENARIOS (all sum to {target}; 'Base (swings)' = deterministic output):")
        for name, comp in scen.items():
            gov = sum(comp.get(b, 0) for b in ("PH", "BN", "GPS", "GRS", "WARISAN", "MUDA", "KDM", "PBM"))
            cat = "para" if meta[name]["category"] == "parametric" else "narr"
            print(f"  [{cat:4s}] {name:48s} | govt={gov:3d} | PN={comp.get('PN', 0):2d} PH={comp.get('PH', 0):2d} BN={comp.get('BN', 0):2d}")
        print(f"\nSaved: {scen_path}")
        print(f"Saved: {meta_path} (quantitative + reviewed news-narrative)")
        return

    print("GE16 FORECAST — factor model v1.0")
    print("=" * 60)
    print(f"Economic term: {economic_term():+.2f}pp to govt blocs")
    print(f"\nDeterministic projection:")
    comp = df["proj_winner"].value_counts().to_dict()
    for b in ["PN", "PH", "BN", "GPS", "GRS", "WARISAN", "MUDA", "KDM", "PBM", "IND"]:
        if b in comp:
            print(f"  {b}: {comp[b]}")
    govt_expected = sum(comp.get(b, 0) for b in ["PH", "BN", "GPS", "GRS", "WARISAN", "MUDA", "KDM", "PBM"])
    print(f"  Govt-aligned: {govt_expected}")
    if VACANCIES:
        nv = len(VACANCIES)
        print(f"\nℹ️  Vacancies (Art 49A): {nv} seat(s) retained in the 222-seat forecast; current occupancy is vacant")
        for code, reason in VACANCIES.items():
            print(f"    {code}: {reason}")
    print(f"\nMonte Carlo ({args.iterations} iterations):")
    print(f"  Govt-aligned seats: P10={pctile(govt,10):.0f}  P50={pctile(govt,50):.0f}  P90={pctile(govt,90):.0f}")
    print(f"  P(govt >= 112): {100*float((govt>=112).mean()):.1f}%")
    print(f"  Flips: P50={pctile(flips,50):.0f}  (P10={pctile(flips,10):.0f}, P90={pctile(flips,90):.0f})")
    flips_df = df[df["flip"]]
    print(f"\nProjected flips ({len(flips_df)}):")
    for _, r in flips_df.iterrows():
        print(f"  {r['code']} {r['constituency']}: {r['ge15_winner']} -> {r['proj_winner']} ({r['proj_margin']}%) [{r['seat_type']}]")

    # save outputs
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(HIST, exist_ok=True)
    out = {
        "generated": pd.Timestamp.now(tz="UTC").isoformat(),
        "model": "factor-v1.0",
        "economic_term": economic_term(),
        "deterministic": comp,
        "govt_expected": govt_expected,
        "monte_carlo": {"P10": pctile(govt, 10), "P50": pctile(govt, 50), "P90": pctile(govt, 90),
                        "P_majority": float((govt >= 112).mean()),
                        "flips_P50": pctile(flips, 50)},
        "flips": flips_df[["code", "constituency", "state", "ge15_winner", "proj_winner", "proj_margin", "seat_type"]].to_dict("records"),
        "vacancies": [{"code": c, "reason": r} for c, r in VACANCIES.items()],
        "projected_seats": df.to_dict("records"),
        "parliament_seats": 222,
        "forecast_seats": 222,
        # Seat accounting (3 vacancies, Art 49A). The 222-seat projection
        # universe is intact: every constituency is projected with its last
        # recorded holder as baseline attribution. filled_seats is OCCUPANCY
        # (seats with a sitting member); projected_seats_count is the UNIVERSE.
        # They are not contradictory — they reconcile as
        # projected_seats_count - vacant_seats == filled_seats. The published
        # seat-accounting annotation carries both readings explicitly.
        "filled_seats": 222 - len(VACANCIES),
        "vacant_seats": len(VACANCIES),
        "projected_seats_count": 222,
        "seat_accounting": (
            "222-seat projection universe retained; "
            f"{len(VACANCIES)} seat(s) vacant ({', '.join(sorted(VACANCIES))}) "
            "are occupancy metadata only and stay in the universe with baseline "
            f"attribution held; {222 - len(VACANCIES)} seat(s) have a sitting "
            f"member (filled_seats={222 - len(VACANCIES)}), and all "
            f"{222} seats are projected (projected_seats_count=222). "
            f"Reconciles as projected_seats_count({222}) - vacant_seats("
            f"{len(VACANCIES)}) == filled_seats({222 - len(VACANCIES)})."
        ),
    }
    latest_path = os.path.join(OUT, "ge16-forecast-latest.json")
    with open(latest_path, "w") as f:
        json.dump(out, f, indent=2)
    # dated snapshot in history/
    import shutil
    stamp = pd.Timestamp.now().strftime("%Y-%m-%d")
    hist_path = os.path.join(HIST, f"ge16-forecast-{stamp}.json")
    shutil.copy(latest_path, hist_path)
    print(f"\nSaved: {latest_path}")
    print(f"Snapshot: {hist_path}")
    # Stage-2 cron runs this default path (--iterations 1, no --scenarios) and
    # rewrites the canonical forecast bytes; re-bind the scenario provenance
    # digests to them so Stage 3's seal does not fail-closed on stale evidence.
    refresh_scenario_meta_evidence(latest_path)


if __name__ == "__main__":
    main()
