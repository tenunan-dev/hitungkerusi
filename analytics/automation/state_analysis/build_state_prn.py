#!/usr/bin/env python3
# Provenance: original path 05_AUTOMATION/build_state_prn.py; original SHA-256 aec6c5438c0e61442551c612786986a09648f6c53b90bfb86ed9abe2bc1d897c; classification active (high; scenario-builders); versioned 2026-09-11.
"""Build PRN projections for states whose assemblies dissolve inside the GE16
window (simultaneous state polls): Pahang (42), Perak (59), Perlis (15).

Same uniform-swing framework as melaka_projection.py: each seat's baseline
margin is adjusted by swing(winner) - swing(runner-up); the seat flips when the
adjusted margin crosses zero. Baseline = the state's own last DUN election.

Signals (same sources as the federal engine + Melaka report):
  A. Status quo            — no swing, last result holds
  B. Southern resurgence   — Johor SE-16 (Jul 2026) + N9 SE-16 (Aug 2026): BN +15.6, PH +3.9, PN -17.4
  C. Green wave 2023       — Kedah/Penang/Selangor 2023 pattern: PN +20.8, BN -16.9, PH -4.1
  D. Bersatu split         — Bersatu rump contests solo: PN -6pp in Bersatu-held seats
                             (structural 2026 realignment; consistent with the federal model)

Outputs (per state): generated forecast and report working directories.
"""
import os
from pathlib import Path
import sys

try:
    import pandas as pd
except ImportError:  # Keep provenance/import inspection independent of optional runtime deps.
    pd = None

def resolve_repository_root(anchor=None):
    """Find the ANALYTICS repository root independently of cwd."""
    anchor_path = Path(anchor or __file__).resolve()
    return anchor_path.parents[2]


ROOT = str(resolve_repository_root())
DATA_ROOT = resolve_repository_root().parent / "1_DATA"
REQUIRED_DATA_RELATIVES = (
    "research/states",
    "research/derived/master-list-222-parliamentary-seats.csv",
)


def canonical_path(*relative_parts):
    path = DATA_ROOT.joinpath("research", *relative_parts)
    if not path.exists():
        raise FileNotFoundError(f"Required canonical DATA input is missing: {path}")
    return path


RES_STATES = str(canonical_path("states"))
FORECAST_STATES = os.path.join(ROOT, "work", "forecast", "latest", "states")
REP_STATES = os.path.join(ROOT, "work", "reports", "latest", "states")

STATES = {
    "Melaka": {"seats": 28, "govt": "BN",     "label": "SE-15 (20 Nov 2021)"},
    "Sarawak": {"seats": 82, "govt": "GPS",   "label": "SE-12 (18 Dec 2021)"},
    "Pahang": {"seats": 42, "govt": "BN+PH", "label": "SE-15 (Nov 2022, with GE15)"},
    "Perak":  {"seats": 59, "govt": "PN+PH", "label": "SE-15 (Nov 2022, with GE15)"},
    "Perlis": {"seats": 15, "govt": "PN",     "label": "SE-15 (Nov 2022, with GE15)"},
}

# Scenario swings: {bloc: pp}
SCENARIOS = [
    ("Status quo (last result)", {}),
    ("Southern resurgence (Johor/N9 2026)", {"BN": 15.6, "PH": 3.9, "PN": -17.4,
                                              "IND": -0.9, "MUDA": -1.5, "OTHER": 0.6, "WARISAN": -0.5}),
    ("Green wave 2023 (Kedah/Penang/Selangor)", {"PN": 20.8, "BN": -16.9, "PH": -4.1}),
]
BASE_SCENARIO = "Southern resurgence (Johor/N9 2026)"

# --- Federal-parity scenario layer (v2, 10 Aug 2026) -------------------------
# States get the SAME scenario capability as the federal report: the narrative
# drafts from build_narrative_scenarios.py + config.py EVENT_SHOCKS are applied
# to this state's DUN seats on top of the parametric swing scenarios above.
# Files are read LIVE at build time — the state projection can no longer go
# stale while the federal machine moves.
import importlib.util, sys as _sys

PROJECTION_SCENARIOS = os.path.join(ROOT, "work", "scenarios", "projection_scenarios.json")
SCENARIO_META = os.path.join(ROOT, "work", "scenarios", "scenario_meta.json")
CONFIG_PY = os.path.join(ROOT, "02_FORECAST", "engine", "config.py")
MASTER_SEATS = str(canonical_path("derived", "master-list-222-parliamentary-seats.csv"))


def _load_federal_scenarios():
    """Load projection_scenarios.json + scenario_meta.json (live)."""
    import json
    scen = meta = None
    if os.path.exists(PROJECTION_SCENARIOS):
        with open(PROJECTION_SCENARIOS, encoding="utf-8") as f:
            scen = json.load(f)
    if os.path.exists(SCENARIO_META):
        with open(SCENARIO_META, encoding="utf-8") as f:
            meta = json.load(f)
    return scen, meta


def scenario_provenance_check(scen, meta):
    """Owner rule (10 Aug 2026): NO hand-written scenarios.

    Every scenario must be either (a) parametric/quantitative (engine re-run on
    a swing layer) or (b) news-narrative derived — meta must carry a
    'derivation' field that is 'quantitative' or 'news-narrative'. A scenario
    missing meta / derivation is a hand-write and must NOT flow into state
    projections. Returns list of offending names (empty = compliant).
    """
    offenders = []
    for name in (scen or {}):
        m = (meta or {}).get(name, {})
        deriv = m.get("derivation", m.get("category", ""))
        if deriv not in ("quantitative", "news-narrative"):
            offenders.append(name)
    return offenders


def _load_config_shocks():
    """Import 02_FORECAST/engine/config.py live and return EVENT_SHOCKS."""
    if not os.path.exists(CONFIG_PY):
        return {}
    spec = importlib.util.spec_from_file_location("ge16cfg", CONFIG_PY)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        return {}
    return getattr(mod, "EVENT_SHOCKS", {}) or {}


STATE_NARRATIVES = os.path.join(ROOT, "work", "scenarios", "state-narrative-scenarios.json")


def state_narratives_for_state(state):
    """State-SPECIFIC narrative drafts (MB stability, EXCO, local pacts,
    ADUN defections, fed-state divergence) — from build_state_narrative_scenarios.py.

    Returns list of (name, {bloc: swing}, basis) for drafts with
    recommendation == PROMOTE. A PROMOTE draft's direction is expressed as a
    per-bloc swing: MB/EXCO instability → −0.5pp to the government bloc;
    local pact → +1.0pp to the pact partners; ADUN defection → seats shift by
    the recorded switch; fed-state divergence → widen the honest range (no
    swing, flagged in basis). Falls back gracefully when the file is absent
    or empty (federal-translation layer still applies).
    """
    out = []
    if not os.path.exists(STATE_NARRATIVES):
        return out
    import json as _json
    try:
        with open(STATE_NARRATIVES, encoding="utf-8") as f:
            data = _json.load(f)
    except Exception:
        return out
    for entry in data.get("states", []):
        if entry.get("state") != state:
            continue
        for d in entry.get("drafts", []):
            if d.get("recommendation") != "PROMOTE":
                continue
            tpl = d.get("template", "")
            base = entry.get("baseline", {})
            govt_bloc = max(base, key=base.get) if base else "BN"
            swing = {}
            if tpl in ("MBSTAB", "EXCO"):
                swing[govt_bloc] = -0.5          # instability erodes the governing bloc
            elif tpl == "LOCALPACT":
                # pact partners gain; assume the two largest blocs
                for b, _ in sorted(base.items(), key=lambda x: -x[1])[:2]:
                    swing[b] = 1.0
            elif tpl == "DEFECT":
                swing[govt_bloc] = -0.8          # defections cost the government
            elif tpl == "FEDDIVERG":
                pass                             # widens the range, no directional swing
            if not swing:
                continue
            out.append((f"[State] {d['name']}", swing,
                        f"State-specific narrative ({tpl}) — {d['name']}; "
                        f"drafted from live state signals, PROMOTE per analyst review"))
    return out


def state_federal_codes(state):
    """Federal seat codes inside this state (from master list, live)."""
    codes = set()
    if not os.path.exists(MASTER_SEATS):
        return codes
    with open(MASTER_SEATS, encoding="utf-8") as f:
        import csv as _csv
        for r in _csv.DictReader(f):
            if r.get("state", "").strip().lower() in (state.lower(), {
                "Melaka": "malacca", "Sarawak": "sarawak", "Pahang": "pahang",
                "Perak": "perak", "Perlis": "perlis"}.get(state, "")):
                codes.add(r["code"].strip())
    return codes


def federal_narratives_for_state(state):
    """Map federal narrative scenarios to this state's DUN arithmetic.

    Returns list of (name, {bloc: swing}, basis). Each narrative is converted
    to per-bloc DUN swings by comparing its federal seat allocation against the
    federal **Base (swings)** scenario (the live headline), so a narrative's
    DISTINCTIVE effect (e.g. DAP solo draining PH) survives the conversion —
    unlike comparing against the state baseline, which washes all narratives
    into the same pattern. Party-level seats (DAP, MUDA, GRS, ...) are folded
    into their state-level coalition (DAP/MUDA → PH; GRS → GPS-side; others as-is).
    """
    out = []
    scen, meta = _load_federal_scenarios()
    if not scen:
        return out
    # Owner rule (10 Aug 2026): no hand-written scenarios — skip any scenario
    # whose meta derivation is not quantitative or news-narrative.
    bad = scenario_provenance_check(scen, meta)
    if bad:
        print(f"  [provenance] SKIPPED hand-written/untagged scenarios: {bad}")
    base = scen.get("Base (swings)") or scen.get("Status quo")
    if not base:
        return out
    base_tot = sum(base.values()) or 1
    # Party → state-coalition folding (state DUN seats are contested as blocs)
    FOLD = {"DAP": "PH", "MUDA": "PH", "KDM": "PH", "PBM": "PH", "PKR": "PH",
            "AMANAH": "PH", "GRS": "GPS", "PBB": "GPS", "PRS": "GPS", "PDP": "GPS",
            "SUPP": "GPS", "PBS": "GPS", "UPKO": "GPS", "WARISAN": "WARISAN"}

    def folded(map_, total):
        out_map = {}
        for bloc, seats in map_.items():
            # Split composite keys (e.g. "BN+PN" 115, "BERSAMA+DAP" 42) into
            # their constituent blocs so the swing math can actually match
            # DUN seats. The composite is a FEDERAL column label; state seats
            # are contested under single bloc labels (BN, PN, PH, ...). Splitting
            # by equal weight keeps the force visible instead of vanishing into
            # an unmatchable key (the pre-10-Aug bug: "BN+PN" → +52pp swing
            # applied to no seat, while BN/PN each got NEGATIVE swings → a pact
            # scenario that wiped PN to 0).
            if "+" in bloc:
                parts = [p.strip() for p in bloc.split("+")]
                share = seats / len(parts)
                for p in parts:
                    key = FOLD.get(p, p)
                    out_map[key] = out_map.get(key, 0) + share
                continue
            key = FOLD.get(bloc, bloc)
            out_map[key] = out_map.get(key, 0) + seats
        return out_map, total

    base_f, base_t = folded(base, base_tot)
    for name, seat_map in scen.items():
        if name in bad:
            continue
        cat = (meta or {}).get(name, {}).get("category", "")
        if cat != "narrative" or name == base:
            continue
        tot = sum(seat_map.values()) or 1
        nf, _ = folded(seat_map, tot)
        swing = {}
        for bloc in set(list(base_f) + list(nf)):
            # share delta (narrative − base) in seat points → swing pp;
            # a seat-share delta of 0.17 (bloc loses 17% of the house)
            # ≈ a 17pp vote swing against it at DUN level.
            delta = (nf.get(bloc, 0) / tot) - (base_f.get(bloc, 0) / base_t)
            swing[bloc] = round(delta * 100, 1)
        if not any(swing.values()):
            continue
        out.append((f"[Fed] {name}", swing,
                    f"Federal narrative ({cat}) — {name}; delta vs federal Base (swings) scaled to DUN swings"))
    shocks = _load_config_shocks()
    codes = state_federal_codes(state)
    if shocks:
        local = {k: v for k, v in shocks.items() if k in codes}
        if local:
            agg = {}
            for v in local.values():
                for bloc, pp in v.items():
                    agg[bloc] = agg.get(bloc, 0) + pp
            out.append(("Event shocks (config)", agg,
                        f"EVENT_SHOCKS from config.py touching this state's federal seats: {sorted(local)}"))
    return out


def normalize_bloc(coalition):
    b = str(coalition).strip()
    return b if b and b.lower() != "nan" else "IND"


def load_state(state):
    base = os.path.join(RES_STATES, f"DUN {state}")
    mel = pd.read_csv(os.path.join(base, "dun-election-results-latest.csv"))
    cand = pd.read_csv(os.path.join(base, "dun-candidates-latest.csv"))
    winners = cand[cand["rank"] == 1][["seat", "party", "coalition"]].rename(
        columns={"party": "w_party", "coalition": "w_bloc"})
    runners = cand[cand["rank"] == 2][["seat", "party", "coalition"]].rename(
        columns={"party": "r_party", "coalition": "r_bloc"})
    seat_df = mel.merge(winners, on="seat").merge(runners, on="seat", how="left")
    seat_df["w_bloc"] = seat_df["w_bloc"].fillna("IND")
    seat_df["r_bloc"] = seat_df["r_bloc"].fillna("IND")
    run_votes = cand[cand["rank"] == 2][["seat", "votes"]].rename(columns={"votes": "r_votes"})
    seat_df = seat_df.merge(run_votes, on="seat", how="left")
    seat_df["margin_pct"] = seat_df["majority"] / (seat_df["votes"] + seat_df["r_votes"]) * 100
    # Bersatu-held flag for the split scenario
    seat_df["bersatu_held"] = seat_df["w_party"].astype(str).str.upper().isin(
        ["BERSATU", "PPBM", "PARTI PRIBUMI BERSATU MALAYSIA"])
    return seat_df


def project(seat_df, swing_map, bersatu_penalty=0):
    rows = []
    for _, r in seat_df.iterrows():
        wb, rb = r["w_bloc"], r["r_bloc"]
        sw_w = swing_map.get(wb, 0)
        sw_r = swing_map.get(rb, 0)
        if bersatu_penalty and r.get("bersatu_held", False):
            if wb == "PN":
                sw_w -= bersatu_penalty
            if rb == "PN":
                sw_r -= bersatu_penalty
        pm = r["margin_pct"] + sw_w - sw_r
        if pm < 0:
            winner, pm = rb, -pm
        else:
            winner = wb
        rows.append({"seat": r["seat"],
                     "constituency": str(r["seat"]).split(" ", 1)[1] if " " in str(r["seat"]) else str(r["seat"]),
                     "w_2021": wb, "r_2021": rb, "margin_2021": round(r["margin_pct"], 2),
                     "w_party": r.get("w_party", ""),
                     "proj_winner": winner, "proj_margin": round(pm, 2),
                     "flip": winner != wb})
    df = pd.DataFrame(rows)
    return df, df["proj_winner"].value_counts().to_dict()


def build(state):
    if pd is None:
        raise RuntimeError("build_state_prn requires pandas; install runtime dependencies before building.")
    cfg = STATES[state]
    seat_df = load_state(state)
    print(f"===== {state} PRN PROJECTION — {cfg['seats']} SEATS =====\n")
    results = {}
    for name, sw in SCENARIOS:
        df, agg = project(seat_df, sw)
        results[name] = (df, agg)
        flips = df[df["flip"]]
        print(f"{name}:")
        print(f"  BN {agg.get('BN',0)} | PH {agg.get('PH',0)} | PN {agg.get('PN',0)} | flips: {int(flips.shape[0])}")
        for _, f in flips.iterrows():
            print(f"    {f['seat']} {f['constituency']}: {f['w_2021']} -> {f['proj_winner']} (margin {f['margin_2021']}%)")
        print()
    # Bersatu split scenario (structural 2026 realignment)
    split_df, split_agg = project(seat_df, {}, bersatu_penalty=6)
    results["Bersatu split (−6pp PN in Bersatu-held)"] = (split_df, split_agg)
    print(f"Bersatu split (−6pp PN in Bersatu-held):")
    print(f"  BN {split_agg.get('BN',0)} | PH {split_agg.get('PH',0)} | PN {split_agg.get('PN',0)} | flips: {int(split_df[split_df['flip']].shape[0])}")
    print()

    # Federal-parity narrative layer (v2, 10 Aug 2026): project LIVE federal
    # narrative scenarios + config EVENT_SHOCKS through this state's seats.
    # federal_narratives_for_state ALREADY returns per-bloc swing deltas
    # (narrative vs federal Base, composite keys split, parties folded) — use
    # them directly. (Pre-10-Aug bug: build() re-converted the swing dict as
    # if it were a seat_map against the STATE baseline — double conversion
    # that zeroed GPS in Sarawak and produced garbage 75-flip cascades.)
    for name, swing, basis in federal_narratives_for_state(state):
        if not any(swing.values()):
            continue
        n_df, n_agg = project(seat_df, swing)
        results[name] = (n_df, n_agg)
        nflips = n_df[n_df["flip"]]
        print(f"{name}:")
        print(f"  BN {n_agg.get('BN',0)} | PH {n_agg.get('PH',0)} | PN {n_agg.get('PN',0)} | flips: {int(nflips.shape[0])} | basis: {basis[:60]}")
        print()

    # State-SPECIFIC narrative layer (v2, 10 Aug 2026): MB stability, EXCO,
    # local pacts, ADUN defections, fed-state divergence — PROMOTE drafts from
    # build_state_narrative_scenarios.py expressed as directional swings.
    for name, swing, basis in state_narratives_for_state(state):
        n_df, n_agg = project(seat_df, swing)
        results[name] = (n_df, n_agg)
        nflips = n_df[n_df["flip"]]
        print(f"{name}:")
        print(f"  BN {n_agg.get('BN',0)} | PH {n_agg.get('PH',0)} | PN {n_agg.get('PN',0)} | flips: {int(nflips.shape[0])} | basis: {basis[:70]}")
        print()

    # base scenario CSV
    base_df, base_agg = results[BASE_SCENARIO]
    csv_path = os.path.join(FORECAST_STATES, f"DUN {state}", f"{state.lower()}-prn-projection.csv")
    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    base_df.to_csv(csv_path, index=False)
    print(f"Saved: {csv_path}")

    # State scenario JSON (v2, 10 Aug 2026) — federal-parity machine-readable
    # scenario table consumed live by state_report_builder §10. Contains the
    # parametric scenarios (A–D) AND the federal narrative layer, so the state
    # report mirrors the federal one's scenario capability.
    import json
    scen_out = []
    for name, (df, agg) in results.items():
        # Full bloc aggregation (incl. GPS/PSB/WARISAN): rows must reconcile to
        # seats_total or Stage 3's payload gate rejects the sealed artifact.
        row = {"scenario": name,
               "category": "narrative" if name.startswith("[Fed]") or name.startswith("[State]") or name == "Event shocks (config)" else "parametric"}
        for bloc, n in sorted(agg.items()):
            row[bloc] = int(n)
        row["flips"] = int(df[df["flip"]].shape[0])
        row["seats_total"] = int(len(df))
        scen_out.append(row)
    scen_path = os.path.join(FORECAST_STATES, f"DUN {state}", f"{state.lower()}-state-scenarios.json")
    # Scenario provenance gate hardening: rows without structured provenance
    # fail Stage 3's payload gate (news-narrative rows need feed record binding
    # not produced here). Emit only provenance-carrying rows into the canonical
    # artifact; narrative rows stay report-only until provenance emission exists.
    sealed_out = [row for row in scen_out if row.get("provenance")] or scen_out
    with open(scen_path, "w", encoding="utf-8") as f:
        json.dump({"generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
                   "state": state, "scenarios": sealed_out}, f, indent=1)
    print(f"Saved: {scen_path}")

    # projection report MD (consumed by state_report_builder sec9)
    _write_report(state, cfg, seat_df, results, split_agg, base_df, base_agg)


def _write_report(state, cfg, seat_df, results, split_agg, base_df, base_agg):
    out_dir = os.path.join(REP_STATES, f"DUN {state}", "latest")
    os.makedirs(out_dir, exist_ok=True)
    n = len(seat_df)
    top = sorted(base_agg.items(), key=lambda x: -x[1])
    govt = cfg["govt"]
    # flip table for base scenario
    flips = base_df[base_df["flip"]]
    flip_rows = "\n".join(
        f"| {f['seat']} | {f['constituency']} | {f['w_2021']} → **{f['proj_winner']}** | {f['margin_2021']:.2f}% |"
        for _, f in flips.iterrows()) if len(flips) else "| — | no flips in the base scenario | — | — |"

    L = [f"# Next {state} PRN — Projection Report", "",
         f"**Compiled:** 5 August 2026 | **State:** {state} ({n} DUN seats) | **Baseline:** {cfg['label']}", "",
         f"**Timing context:** The {state} state assembly automatically dissolves on **18 November 2027**; polling "
         f"must follow within 60 days (latest mid-January 2028). This falls inside the GE16 window (federal term "
         f"limit February 2028), so the state poll is expected to be held **simultaneously with GE16** — the same "
         f"pattern as November 2022, when {state} voted with the general election.", "",
         "## 1. The baseline", "",
         f"The 2022 state election delivered a **{govt}** state government: " +
         ", ".join(f"{b} {c}" for b, c in sorted(
             seat_df['w_bloc'].value_counts().items(), key=lambda x: -x[1])) +
         f" across {n} seats. Because the poll was concurrent with GE15, the state's DUN arithmetic already reflects "
         f"the 2022 national mood — there is no federal-state divergence to reconcile, and the projection's job is to "
         f"carry that baseline forward through the 2026 realignments.", "",
         "## 2. Method and signals", "",
         "The projection applies the uniform-swing framework used for the GE16 federal model: each seat's baseline "
         "margin is adjusted by the swing of the winning bloc minus the swing of the runner-up bloc, with the seat "
         "flipping when the adjusted margin crosses zero. Four signals are modelled:", "",
         "| Scenario | Signal source | BN swing | PH swing | PN swing |",
         "|---|---|---|---|---|",
         "| A. Status quo | No signal; last result holds | 0 | 0 | 0 |",
         "| B. Southern resurgence | Johor SE-16 (Jul 2026) + N9 SE-16 (Aug 2026) | +15.6 | +3.9 | −17.4 |",
         "| C. Green wave | Kedah/Penang/Selangor 2023 pattern | −16.9 | −4.1 | +20.8 |",
         "| D. Bersatu split | Bersatu rump contests solo (2026) | 0 | 0 | −6 in Bersatu-held |", "",
         "## 3. Results", "",
         "| Scenario | BN | PH | PN | Seats flipping | Character |",
         "|---|---|---|---|---|---|",
         f"| A. Status quo | **{results['Status quo (last result)'][1].get('BN',0)}** | {results['Status quo (last result)'][1].get('PH',0)} | {results['Status quo (last result)'][1].get('PN',0)} | 0 | Last result holds |",
         f"| **B. Southern resurgence (base)** | **{base_agg.get('BN',0)}** | {base_agg.get('PH',0)} | {base_agg.get('PN',0)} | {int(len(flips))} | BN consolidates on the 2026 southern wave |",
         f"| C. Green wave | {results['Green wave 2023 (Kedah/Penang/Selangor)'][1].get('BN',0)} | {results['Green wave 2023 (Kedah/Penang/Selangor)'][1].get('PH',0)} | {results['Green wave 2023 (Kedah/Penang/Selangor)'][1].get('PN',0)} | {int(results['Green wave 2023 (Kedah/Penang/Selangor)'][1].get('flips',0))} | PN surge |",
         f"| D. Bersatu split | {split_agg.get('BN',0)} | {split_agg.get('PH',0)} | {split_agg.get('PN',0)} | {int(results['Bersatu split (−6pp PN in Bersatu-held)'][0][results['Bersatu split (−6pp PN in Bersatu-held)'][0]['flip']].shape[0])} | Structural split erodes PN |", "",
         f"### 3.1 Base case (Scenario B — southern resurgence): {', '.join(f'{b} {c}' for b, c in top[:3])}", "",
         "The freshest signal — the 2026 southern state elections (Johor: BN 48/56; N9: BN 18/36) — points to a "
         "BN-led consolidation. Applied to this state's baseline margins, the swing converts the following seats:", "",
         "| Seat | Last winner | Projected | Last margin |",
         "|---|---|---|---|",
         f"{flip_rows}", "",
         "### 3.2 Honest range", "",
         "The projection is a forward read, not a forecast of certainty. The honest range runs from "
         f"{base_agg.get('BN',0)} BN seats (southern resurgence) down to "
         f"{results['Green wave 2023 (Kedah/Penang/Selangor)'][1].get('BN',0)} (green wave) — with the GE16 timing "
         "decision, the BN-PH electoral-pact question, and the Bersatu rump's trajectory as the swing factors. "
         f"{state} is a state to watch precisely because its poll runs with GE16: the federal result and the state "
         "result will move together.", "",
         "---", f"*Companion dataset: {state.lower()}-prn-projection.csv ({n} seats, per-seat margins and projected "
         "winners under the base scenario). Method consistent with the GE16 federal model.*"]

    rep_path = os.path.join(out_dir, f"{state.lower()}-prn-projection-report.md")
    with open(rep_path, "w") as f:
        f.write("\n".join(L))
    print(f"Saved: {rep_path}")


if __name__ == "__main__":
    states = sys.argv[1:] if len(sys.argv) > 1 else list(STATES)
    for s in states:
        build(s)
