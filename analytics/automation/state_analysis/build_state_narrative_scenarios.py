#!/usr/bin/env python3
# Provenance: original path 05_AUTOMATION/build_state_narrative_scenarios.py; original SHA-256 8f4d778db632cb549a9f4a2d7335ec139672bf58c60fb31fd0313f03a98b01fd; classification active (high; scenario-builders); versioned 2026-09-11.
"""build_state_narrative_scenarios.py — STATE-SPECIFIC narrative scenario drafts (10 Aug 2026).

Owner directive 10 Aug 2026: full finding-stage parity with the federal machine.
The federal generator (build_narrative_scenarios.py) deliberately FILTERS OUT
state-level items (STATE_NOISE); this script is the state-side complement. It
scans state-level signals and produces DRAFT narrative scenarios per Tier-1
state, with WATCH/PROMOTE/DORMANT recommendations.

Templates (state-level equivalents of the federal ones):
  MBSTAB   — Menteri Besar/CM stability: no-confidence motion, MB pressure,
             leadership challenge, MB announces exit, coalition backs MB.
  EXCO     — Executive Council reshuffle / exco defection / exco expelled.
  LOCALPACT— state-level electoral pact between two blocs (e.g. N9's BN-PN
             pact going national, or a state-specific pact forming).
  STATEXIT — a bloc leaves the STATE government (state coalition arithmetic).
  DEFECT   — a state assemblyman (ADUN) defects / jumps party.
  FEDDIVERG— federal-state divergence: state result contradicts federal model
             direction (the Melaka 2021-vs-2022 pattern).

Output: work/scenarios/state-narrative-scenarios.json — per-state draft list:
  {state, drafts: [{name, template, signal_strength, n_signals, trigger,
                    logic, recommendation}]}

Consumers:
  - Stage 2 Step 3 analyst review (same promote/downgrade loop as federal).
  - build_state_prn.py reads the PROMOTE entries and folds their seat-shift
    direction into <state>-state-scenarios.json narrative rows.

Run:  .venv/bin/python 05_AUTOMATION/build_state_narrative_scenarios.py [State1 State2 ...]
"""

import glob
import json
import os
from pathlib import Path
import re
import warnings

def resolve_repository_root(anchor=None):
    """Find the ANALYTICS repository root independently of cwd."""
    anchor_path = Path(anchor or __file__).resolve()
    return anchor_path.parents[2]


ROOT = str(resolve_repository_root())
DATA_ROOT = resolve_repository_root().parent / "1_DATA"
REQUIRED_DATA_RELATIVES = (
    "research/states",
)


def canonical_path(*relative_parts):
    path = DATA_ROOT.joinpath("research", *relative_parts)
    if not path.exists():
        raise FileNotFoundError(f"Required canonical DATA input is missing: {path}")
    return path


TRACKERS = os.path.join(ROOT, "work", "tracking")
# Runtime figure artifacts are staged beneath the ANALYTICS work root.
WORK_FIGURES_ROOT = resolve_repository_root() / "work" / "figures"
FIGURES = str(WORK_FIGURES_ROOT)
RES_STATES = str(canonical_path("states"))

STATES = ["Melaka", "Sarawak", "Pahang", "Perak", "Perlis"]
STATE_ALIAS = {
    "Melaka": ["melaka", "malacca"],    "Sarawak": ["sarawak"],
    "Pahang": ["pahang"],
    "Perak": ["perak"],
    "Perlis": ["perlis"],
}
STATE_MB = {
    "Melaka": ["Ab Rauf bin Yusoh"],
    "Sarawak": ["Abang Johari"],
    "Pahang": ["Wan Rosdy"],
    "Perak": ["Saarani Mohamad"],
    "Perlis": ["Mohd Shukri Ramli"],
}

WEIGHT = {"coalition": 2.0, "legal": 1.5, "candidate": 1.5, "election": 1.0,
          "poll": 0.5, "analysis": 0.2, "policy": 0.2}
EVENT_CATS = ("coalition", "legal", "candidate", "election", "poll")

MB_KEYWORDS = ["menteri besar", "no-confidence", "nol keyakinan", "mb ",
               "resign", "letak jawatan", "leadership", "kepimpinan", "challenge",
               "cabaran", "sokongan", "cm ", "chief minister", "mb's", "mb\'s",
               "jatuh", "fall of", "kerajaan tumbang"]
EXCO_KEYWORDS = ["exco", "state exco", "portfolio", "rombak", "reshuffle",
                 "pecat", "expelled", "keluar exco", "exco shake-up", "exco letak"]
PACT_KEYWORDS = ["pact", "pakatan", "electoral", "kerjasama", "cooperation",
                 "seat allocation", "peruntukan kerusi", "runding", "talks",
                 "bincang", "eyes", "in talks", "rounding", "hadapi",
                 "contests all seats solo", "solo", "gears up"]
DIVERGE_KEYWORDS = ["divergence", "berbeza", "contradict", "bertentangan",
                    "swing state", "state vs federal", "cannot be measured"]
# Company-name suffixes that make a state-word match a false positive
# (e.g. "Perak Transit" is a company, not the state).
COMPANY_SUFFIX = r"(transit|bhd|berhad|sdn|corp|holdings|logistics|rail|properties|development)"


def norm_state(state):
    return re.sub(r"\s+", " ", state.strip())


def load_news():
    path = os.path.join(TRACKERS, "ge16-news-feed.json")
    if not os.path.exists(path):
        warnings.warn(f"Optional generated tracking input is missing: {path}", RuntimeWarning)
        return []
    return json.load(open(path)).get("items", [])


def load_switches(days=21):
    switches = []
    for path in sorted(glob.glob(os.path.join(FIGURES, "_draft", "personnel-updates-*.json"))):
        try:
            d = json.load(open(path))
        except Exception:
            continue
        for p in d.get("persons", []):
            for r in p.get("roles", []):
                if r.get("type") == "party_switch":
                    switches.append({"name": p.get("name", ""), "from": r.get("from", ""),
                                     "to": r.get("to", ""), "seat": r.get("seat", ""),
                                     "note": r.get("note", "")[:100]})
    return switches


def load_personnel():
    path = os.path.join(FIGURES, "ge16-personnel.json")
    if not os.path.exists(path):
        return []
    d = json.load(open(path))
    return d.get("persons", d if isinstance(d, list) else [])


def load_dun_baseline(state):
    """Current DUN seat distribution per bloc (state baseline)."""
    base = os.path.join(RES_STATES, f"DUN {state}")
    path = os.path.join(base, "dun-election-results-latest.csv")
    out = {}
    if not os.path.exists(path):
        return out
    import csv
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            b = (r.get("winner_coalition") or "").strip() or "IND"
            out[b] = out.get(b, 0) + 1
    return out


def state_items(news, state, category=None):
    aliases = STATE_ALIAS.get(state, [state.lower()])
    out = []
    for it in news:
        if category and it.get("category") != category:
            continue
        title = it.get("title", "")
        hay = " ".join([title, it.get("query", ""),
                        json.dumps(it.get("blocs", [])), json.dumps(it.get("parties", []))]).lower()
        if not any(a in hay for a in aliases):
            continue
        # Company-name false-positive guard: "Perak Transit", "Sarawak Energy",
        # "Melaka Water" etc. are companies, not state references.
        tlow = title.lower()
        for a in aliases:
            m = re.search(re.escape(a) + r"\s+(?:" + COMPANY_SUFFIX + r")\b", tlow)
            if m and not re.search(re.escape(a) + r"\s+(?:assembly|govt|government|mb|exco|dun|state)", tlow):
                break
        else:
            out.append(it)
    return out


def mentions(text, keywords):
    t = (text or "").lower()
    return any(k.lower() in t for k in keywords)


def recommend(strength, n_signals, promote_at=2.5, watch_at=0.8):
    if strength >= promote_at and n_signals >= 2:
        return "PROMOTE"
    if strength >= watch_at:
        return "WATCH"
    return "DORMANT"


def main():
    targets = sys.argv[1:] if len(sys.argv) > 1 else STATES
    news = load_news()
    switches = load_switches()
    persons = load_personnel()
    all_drafts = []

    for state in targets:
        st = norm_state(state)
        if st not in STATES:
            print(f"SKIP unknown state: {st}")
            continue
        snews = state_items(news, st)
        base = load_dun_baseline(st)
        drafts = []

        # ---- TEMPLATE MBSTAB — MB/CM stability ---------------------------
        mnev = [it for it in snews if mentions(it.get("title", ""), MB_KEYWORDS)]
        if mnev:  # guarded — no draft without news about the MB; office-holder is static
            mbs = [p for p in persons if any(
                (r.get("type") == "mb" or r.get("type") == "chief_minister") and
                st.lower() in str(r.get("state", "")).lower() or
                (r.get("type") == "mb") and p.get("name") in STATE_MB.get(st, [])
                for r in p.get("roles", []))]
            # MB office-holder presence modulates the news strength, never creates a draft.
            strength = sum(WEIGHT.get(it.get("category"), 0.2) for it in mnev) + 0.05 * min(5, len(mbs))
            if strength >= 0.8:
                drafts.append({
                    "name": f"{st} MB/CM stability under pressure",
                    "template": "MBSTAB",
                    "signal_strength": round(strength, 1),
                    "n_signals": len(mnev) + len(mbs),
                    "trigger": [f"[{it.get('category')}] {it.get('title','')}" for it in mnev[:3]],
                    "logic": (f"{len(mnev)} feed signal(s) name the {st} MB/CM; "
                              f"if the MB falls or is challenged, the state government's "
                              f"arithmetic {base} is up for renegotiation."),
                    "recommendation": recommend(strength, len(mnev) + len(mbs)),
                })

        # ---- TEMPLATE EXCO — exco reshuffle / defection -------------------
        eev = [it for it in snews if mentions(it.get("title", ""), EXCO_KEYWORDS)]
        if eev:  # guarded — office-holder counts are static context, never create a draft
            # EXCO office-holders modulate the news strength only.
            excos = [p for p in persons if any(r.get("type") == "exco" for r in p.get("roles", []))]
            exco_state = [p for p in excos if st.lower() in json.dumps(p).lower()]
            strength = sum(WEIGHT.get(it.get("category"), 0.2) for it in eev) + 0.05 * min(5, len(exco_state))
            if strength >= 0.8:
                drafts.append({
                    "name": f"{st} EXCO reshuffle / instability",
                    "template": "EXCO",
                    "signal_strength": round(strength, 1),
                    "n_signals": len(eev) + len(exco_state),
                    "trigger": [f"[{it.get('category')}] {it.get('title','')}" for it in eev[:3]],
                    "logic": (f"{len(eev)} feed signal(s) + {len(exco_state)} EXCO office-holders "
                              f"in the personnel DB; an EXCO rupture signals coalition tension "
                              f"inside the {st} government."),
                    "recommendation": recommend(strength, len(eev) + len(exco_state)),
                })

        # ---- TEMPLATE LOCALPACT — state-level electoral pact --------------
        pev = [it for it in snews if mentions(it.get("title", ""), PACT_KEYWORDS)]
        strength = sum(WEIGHT.get(it.get("category"), 0.2) for it in pev)
        if strength >= 0.8 or pev:
            drafts.append({
                "name": f"{st} state-level pact / seat allocation talks",
                "template": "LOCALPACT",
                "signal_strength": round(strength, 1),
                "n_signals": len(pev),
                "trigger": [f"[{it.get('category')}] {it.get('title','')}" for it in pev[:3]],
                "logic": (f"{len(pev)} pact-signal(s) for {st}; a local pact (e.g. the N9 "
                          f"BN-PN template going national) would rewire the state "
                          f"arithmetic {base}."),
                "recommendation": recommend(strength, len(pev)),
            })

        # ---- TEMPLATE DEFECT — ADUN party switch --------------------------
        dev = [s for s in switches if st.lower() in json.dumps(s).lower()]
        if dev:
            strength = 1.5 * len(dev)
            drafts.append({
                "name": f"{st} ADUN defection(s)",
                "template": "DEFECT",
                "signal_strength": round(strength, 1),
                "n_signals": len(dev),
                "trigger": [f"[switch] {s['name']} {s['from']}->{s['to']}" for s in dev[:3]],
                "logic": (f"{len(dev)} recorded state-level switch(es); each moved ADUN "
                          f"changes the {st} assembly arithmetic {base}."),
                "recommendation": recommend(strength, len(dev)),
            })

        # ---- TEMPLATE FEDDIVERG — federal-state divergence ----------------
        dev2 = [it for it in snews if mentions(it.get("title", ""), DIVERGE_KEYWORDS)]
        strength = sum(WEIGHT.get(it.get("category"), 0.2) for it in dev2)
        if strength >= 0.8 or dev2:
            drafts.append({
                "name": f"{st} federal-state divergence signal",
                "template": "FEDDIVERG",
                "signal_strength": round(strength, 1),
                "n_signals": len(dev2),
                "trigger": [f"[{it.get('category')}] {it.get('title','')}" for it in dev2[:3]],
                "logic": (f"{len(dev2)} divergence signal(s); {st} is Malaysia's most "
                          f"divergent state (BN 21/28 state 2021 vs 0/6 federal 2022) — "
                          f"the state PRN must not be read as a federal proxy."),
                "recommendation": recommend(strength, len(dev2)),
            })

        all_drafts.append({"state": st, "drafts": drafts,
                           "baseline": base,
                           "n_feed_items": len(snews)})
        print(f"{st}: {len(drafts)} state-narrative drafts "
              f"({len(snews)} state feed items; baseline {base})")

    # ---- Output (merge with existing, preserve analyst edits) -------------
    out_path = os.path.join(ROOT, "work", "scenarios", "state-narrative-scenarios.json")
    existing = []
    if os.path.exists(out_path):
        try:
            existing = json.load(open(out_path))
        except Exception:
            existing = []
    existing_map = {f"{e.get('state')}|{d.get('name')}": d
                    for e in (existing.get("states", []) if isinstance(existing, dict) else existing)
                    for d in e.get("drafts", [])}
    for entry in all_drafts:
        for d in entry["drafts"]:
            key = f"{entry['state']}|{d['name']}"
            if key in existing_map:
                d["recommendation"] = existing_map[key].get("recommendation", d["recommendation"])
                if existing_map[key].get("analyst_note"):
                    d["analyst_note"] = existing_map[key]["analyst_note"]
    out = {"generated_at": __import__("datetime").datetime.now().isoformat(),
           "note": "STATE-SPECIFIC narrative drafts (MB stability, EXCO, local pacts, "
                   "ADUN defections, federal-state divergence). Review in Stage 2 Step 3; "
                   "PROMOTE entries feed build_state_prn.py narrative rows.",
           "states": all_drafts}
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"Saved: {out_path}")


import sys

if __name__ == "__main__":
    main()
