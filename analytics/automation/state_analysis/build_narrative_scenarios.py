#!/usr/bin/env python3
# Provenance: original path 05_AUTOMATION/build_narrative_scenarios.py; original SHA-256 1229f5e97add0c56342b6c2d9f2d9d28ad106547cc9f9717c1e8d27c01b5f6f2; classification active (high; scenario-builders); versioned 2026-09-11.
"""Automated NARRATIVE scenario development (5 Aug 2026, user directive).

Narrative scenarios are the hand-written what-if compositions (a bloc exits
the government, a new force absorbs defectors, a pact forms). This script
automates their DEVELOPMENT: it scans the live political state and produces
evidence-backed DRAFT narrative scenarios, each with a seat composition
computed from ACTUAL seat-holder data (master-list-222) + the judged news feed
+ personnel switch records + party postures.

Templates (each checks current signals, then computes seats from real data):
  EXIT      — a government bloc leaves the coalition (its seats leave govt)
  ABSORB    — a party/bloc absorbs defectors or a splinter (seats move)
  PACT      — two blocs form an electoral pact (seat columns merge)
  SPLIT     — a bloc splits internally (seats divide between rump + breakaway)

Output: work/scenarios/narrative_scenarios.json — DRAFT scenarios with trigger
evidence, computed seat maps, and a promote/dismiss recommendation per item.
The Stage 2 agent (or the analyst) reviews the drafts and promotes the live
ones into work/scenarios/projection_scenarios.json (narrative category) +
scenario_meta.json. Nothing is promoted automatically — this is a drafting
tool, not an autopilot.

Run:  .venv/bin/python 05_AUTOMATION/build_narrative_scenarios.py
"""
import os
from pathlib import Path
import re
import json
import glob
import warnings

def resolve_repository_root(anchor=None):
    """Find the ANALYTICS repository root independently of cwd."""
    anchor_path = Path(anchor or __file__).resolve()
    return anchor_path.parents[2]


ROOT = str(resolve_repository_root())
DATA_ROOT = resolve_repository_root().parent / "1_DATA"
REQUIRED_DATA_RELATIVES = (
    "research/derived/master-list-222-parliamentary-seats.csv",
)


def canonical_path(*relative_parts):
    path = DATA_ROOT.joinpath("research", *relative_parts)
    if not path.exists():
        raise FileNotFoundError(f"Required canonical DATA input is missing: {path}")
    return path


DERIVED = str(canonical_path("derived"))
MASTER_SEATS = canonical_path("derived", "master-list-222-parliamentary-seats.csv")
TRACKERS = os.path.join(ROOT, "work", "tracking")
# Runtime figure and graph artifacts are staged beneath the ANALYTICS work root.
WORK_FIGURES_ROOT = resolve_repository_root() / "work" / "figures"
WORK_GRAPH_PATH = resolve_repository_root() / "work" / "graph" / "ge16-knowledge-graph.json"
FIGURES = str(WORK_FIGURES_ROOT)
DRAFT = os.path.join(FIGURES, "_draft")

GOVT_BLOCS = {"PH", "BN", "GPS", "GRS", "WARISAN", "MUDA", "KDM", "PBM"}


def norm(name):
    """Normalise a party/bloc name for graph matching (mirrors the graph builder)."""
    if not name:
        return ""
    n = name.strip().lower()
    n = re.sub(r"\b(dato'?|datuk|seri|tan sri|tun|hajah|haji|dr|hajjah)\b", "", n)
    n = re.sub(r"\(.*?\)", "", n)
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def load_seats():
    """Actual seat holdings: {bloc: {party: [codes]}} from master-list-222."""
    import csv
    bloc_party = {}
    with open(MASTER_SEATS) as f:
        for r in csv.DictReader(f):
            bloc = r["coalition"].strip() or "IND"
            party = r["party"].strip() or "IND"
            bloc_party.setdefault(bloc, {}).setdefault(party, []).append(r["code"])
    return bloc_party


def load_news():
    """Judged news feed: list of items with category/blocs/parties/title."""
    path = os.path.join(TRACKERS, "ge16-news-feed.json")
    if not os.path.exists(path):
        warnings.warn(f"Optional generated tracking input is missing: {path}", RuntimeWarning)
        return []
    d = json.load(open(path))
    return d.get("items", [])


def load_personnel_switches(days=14):
    """Recent party switches from personnel-updates-*.json layer files."""
    switches = []
    for path in sorted(glob.glob(os.path.join(DRAFT, "personnel-updates-*.json"))):
        try:
            d = json.load(open(path))
        except Exception:
            continue
        for p in d.get("persons", []):
            for r in p.get("roles", []):
                if r.get("type") == "party_switch":
                    switches.append({
                        "name": p.get("name", ""),
                        "from": r.get("from", ""),
                        "to": r.get("to", ""),
                        "seat": r.get("seat", ""),
                        "note": r.get("note", "")[:100],
                    })
    return switches


def load_candidate_tracker():
    """Candidate-tracker items mentioning a party/bloc (Bersama's candidacy
    signals live here, not in the news feed)."""
    items = []
    path = os.path.join(TRACKERS, "ge16-candidate-tracker-log.md")
    if not os.path.exists(path):
        warnings.warn(f"Optional generated tracking input is missing: {path}", RuntimeWarning)
        return items
    try:
        txt = open(path, encoding="utf-8", errors="ignore").read()
    except Exception:
        return items
    for line in txt.splitlines():
        line = line.strip()
        if not (line.startswith("- [") or line.startswith("- **") or line.startswith("* [")):
            continue
        items.append({"category": "candidate", "title": line[:180],
                      "raw": line})
    return items


def load_parties():
    """Parties DB: posture/bloc per party."""
    path = os.path.join(FIGURES, "ge16-parties.json")
    if not os.path.exists(path):
        return {}
    d = json.load(open(path))
    return {p["name"]: p for p in d.get("parties", [])}


def bloc_seats(bloc_party):
    return {b: sum(len(v) for v in pps.values()) for b, pps in bloc_party.items()}


def party_seats(bloc_party, party):
    for bloc, pps in bloc_party.items():
        if party in pps:
            return len(pps[party])
    return 0


def mentions(text, names):
    t = (text or "").lower()
    for n in names:
        if n.lower() in t:
            return True
    return False


def news_for(news, cats, blocs=None, parties=None):
    out = []
    for it in news:
        if it.get("category") not in cats:
            continue
        if blocs and not (set(it.get("blocs", [])) & set(blocs)):
            continue
        if parties and not (set(it.get("parties", [])) & set(parties)):
            continue
        out.append(it)
    return out


def main():
    bloc_party = load_seats()
    news = load_news()
    switches = load_personnel_switches()
    cand_items = load_candidate_tracker()
    parties = load_parties()
    seat_totals = bloc_seats(bloc_party)
    print(f"Current seat holdings: {seat_totals}")
    print(f"News feed items: {len(news)} | recent party switches: {len(switches)}\n")

    # party-level seat counts (parties DB mp_seats; fallback to holdings)
    def pseats(party, bloc=None):
        p = parties.get(party)
        if p and p.get("mp_seats"):
            return p["mp_seats"]
        if bloc:
            return seat_totals.get(bloc, 0)
        return 0

    # EVENT categories carry real developments; 'analysis' is commentary
    EVENT_CATS = ("coalition", "legal", "candidate", "election", "poll")
    WEIGHT = {"coalition": 2.0, "legal": 1.5, "candidate": 1.5, "election": 1.0,
              "poll": 0.5, "analysis": 0.2, "policy": 0.2}
    STATE_NOISE = ("exco", "adun", "menteri besar", "mb ", "cm ", "state exco",
                   "awak", "sabah ", "selangor ", "penang ", "perak ", "johor ")

    def is_state_noise(it):
        """Candidate/analysis items about state-level appointments are NOT
        federal-narrative signals."""
        if it.get("category") not in ("candidate", "analysis"):
            return False
        title = it.get("title", "").lower()
        return any(k in title for k in STATE_NOISE)

    def news_for_exact(news, cats, blocs=None, parties=None, require_all_blocs=False,
                       drop_state_noise=True):
        """Items whose TAGGED blocs/parties match the target.

        require_all_blocs=True: every requested bloc must be in the item's
        tags (for PACT — both partners must be mentioned together).
        drop_state_noise=True: exclude state-level appointment items.
        """
        out = []
        for it in news:
            if it.get("category") not in cats:
                continue
            if drop_state_noise and is_state_noise(it):
                continue
            it_blocs = set(it.get("blocs", []))
            it_parties = set(it.get("parties", []))
            if blocs:
                if require_all_blocs:
                    if not (set(blocs) <= it_blocs):
                        continue
                elif not (it_blocs & set(blocs)):
                    continue
            if parties and not (it_parties & set(parties)):
                continue
            w = WEIGHT.get(it.get("category"), 0.2)
            out.append((it, w))
        return out

    def fmt_ev(ev):
        return [f"[{e.get('category')}] {e.get('title','')}" for e, _ in ev[:4]]

    def recommend(strength, n_signals, promote_at=3.0, watch_at=1.0):
        if strength >= promote_at and n_signals >= 2:
            return "PROMOTE"
        if strength >= watch_at:
            return "WATCH"
        return "DORMANT"

    drafts = []

    # =================================================================
    # TEMPLATE 1 — EXIT: a government bloc/party leaves the coalition
    # Party-level exits (DAP/PKR/UMNO) REQUIRE the party tag — no loose
    # bloc fallback (a PH-tagged DAP story must not fire a PKR exit).
    # =================================================================
    exit_specs = [
        ("DAP", "DAP", "PH"),
        ("PKR", "PKR", "PH"),
        ("BN", "BN", "BN"),
        ("UMNO", "UMNO", "BN"),
        ("GPS", "GPS", "GPS"),
        ("GRS", "GRS", "GRS"),
        ("WARISAN", "WARISAN", "WARISAN"),
    ]
    for label, party, bloc in exit_specs:
        # party-level exits (DAP/PKR/UMNO) REQUIRE the party tag — no loose
        # bloc fallback (a PH-tagged DAP story must not fire a PKR exit).
        party_level = party in ("DAP", "PKR", "UMNO")
        ev = news_for_exact(news, EVENT_CATS, blocs=[bloc], parties=[party.lower()])
        if not ev and not party_level:
            # bloc-level labels only: fall back to bloc-tagged items
            ev = news_for_exact(news, EVENT_CATS, blocs=[bloc])
        sw_ev = [s for s in switches if s["from"] and bloc.upper() in s["from"].upper()]
        strength = sum(w for _, w in ev) + 1.5 * len(sw_ev)
        n_signals = len(ev) + len(sw_ev)
        if strength < 0.5:
            continue
        n_seats = pseats(party, bloc)
        if n_seats <= 0:
            continue
        govt_now = sum(v for k, v in seat_totals.items() if k in GOVT_BLOCS)
        govt_after = govt_now - (n_seats if bloc in GOVT_BLOCS or party in GOVT_BLOCS else 0)
        drafts.append({
            "name": f"{label} exits the government (contests separately)",
            "template": "EXIT",
            "signal_strength": round(strength, 1),
            "n_signals": n_signals,
            "trigger": fmt_ev(ev) + [f"[switch] {s['name']} {s['from']}->{s['to']}" for s in sw_ev[:2]],
            "logic": f"{label} holds {n_seats} seats; if it leaves the government these seats "
                     f"cease to count for the govt column (they remain won, but not govt-aligned).",
            "seat_map": None,
            "govt_impact": f"govt {govt_now} -> {govt_after}",
            "recommendation": recommend(strength, n_signals),
        })

    # =================================================================
    # TEMPLATE 2 — ABSORB: a rising force absorbs defectors
    # =================================================================
    absorb_targets = {"BERSAMA": {"from_blocs": ["PH", "PKR"], "seat_bonus": 3},
                      "WAWASAN": {"from_blocs": ["BERSATU", "PN"], "seat_bonus": 6},
                      "BERSATU": {"from_blocs": ["PN"], "seat_bonus": 0}}
    for target, cfg in absorb_targets.items():
        ev = [s for s in switches if s["to"] and target.lower() in s["to"].lower()]
        # news-feed signals naming the target
        nev = news_for_exact(news, EVENT_CATS, blocs=cfg["from_blocs"], parties=[target.lower()])
        # candidate-tracker signals naming the target (Bersama's candidacy
        # signals live here — the news feed misses them entirely)
        cev = [c for c in cand_items if target.lower() in c["title"].lower()]
        gained = len(ev)
        strength = 1.5 * gained + sum(w for _, w in nev) + 0.8 * len(cev)
        n_signals = gained + len(nev) + len(cev)
        if strength < 0.5:
            continue
        # new parties (0 seats) get seat_bonus; established ones add defector seats
        cur = pseats(target)
        seats = (cfg["seat_bonus"] + max(0, gained - 2)) if cur == 0 else (cur + gained)
        drafts.append({
            "name": f"{target} absorbs defectors and wins {seats} seats",
            "template": "ABSORB",
            "signal_strength": round(strength, 1),
            "n_signals": n_signals,
            "trigger": [f"[switch] {e['name']} {e['from']}->{e['to']}" for e in ev[:3]]
                       + fmt_ev(nev)
                       + [f"[cand] {c['title'][:90]}" for c in cev[:3]],
            "logic": f"{gained} recorded switch(es) + {len(nev)} feed signal(s) + "
                     f"{len(cev)} candidate-tracker signal(s); modelled as {target} "
                     f"winning {seats} seats (new party: seat_bonus {cfg['seat_bonus']}; "
                     f"established: base {cur} + defectors {gained}).",
            "seat_map": None,
            "govt_impact": "siphons from origin bloc; govt arithmetic unchanged unless origin is govt",
            "recommendation": recommend(strength, n_signals, promote_at=2.0),
        })

    # =================================================================
    # TEMPLATE 3 — PACT: two blocs/parties form an electoral pact
    # Tag-based match requires BOTH blocs in one item's tags. Graph-based
    # fallback: any story whose title MENTIONS both blocs (e.g. "BN-PN
    # pact" tagged only PBM/GPS/GRS) — the knowledge graph's title-mention
    # edges catch these, and the news->news linkage connects them to the
    # related formation story (e.g. N9). This is the situation-synthesis
    # layer: a single tag-limited story can no longer be missed.
    # =================================================================
    pact_pairs = [("BN", "PAS"), ("BN", "PH"), ("BN", "PN"), ("PAS", "WAWASAN"),
                  ("PH", "BERSAMA"), ("GPS", "PH"), ("GRS", "PH")]
    graph = None
    graph_path = WORK_GRAPH_PATH
    if os.path.exists(graph_path):
        try:
            graph = json.load(open(graph_path))
        except Exception:
            graph = None

    for a, b in pact_pairs:
        # both partners must be TAGGED in the same item for a pact signal
        ev = news_for_exact(news, EVENT_CATS, blocs=[a, b], require_all_blocs=True)
        detail = "tagged together"
        if not ev and graph:
            # graph fallback: items whose title mentions BOTH blocs (the
            # knowledge graph's title-mention edges are authoritative —
            # built from the raw title, not the LLM tags)
            an, bn = norm(a), norm(b)
            both = []
            for e in graph["edges"]:
                if e["type"] != "news->party":
                    continue
                dst = graph["nodes"][e["dst"]]["name"]
                if norm(dst) not in (an, bn):
                    continue
                src_name = graph["nodes"][e["src"]]["name"]
                # item mentions the other bloc anywhere in its edges
                other = bn if norm(dst) == an else an
                other_mentioned = any(
                    norm(graph["nodes"][ee["dst"]]["name"]) == other
                    for ee in graph["edges"]
                    if ee["type"] == "news->party" and ee["src"] == e["src"])
                if other_mentioned and e["src"] not in [x[0] for x in both]:
                    both.append((e["src"], src_name))
            ev = [(t, 1.2) for _, t in both[:4]]
            detail = "title mention (graph)"
        if not ev:
            continue
        sa = pseats(a) if a in parties else seat_totals.get(a, 0)
        sb = pseats(b) if b in parties else seat_totals.get(b, 0)
        strength = sum(w for _, w in ev)
        n_signals = len(ev)
        drafts.append({
            "name": f"{a}-{b} electoral pact ({sa + sb} combined seats)",
            "template": "PACT",
            "signal_strength": round(strength, 1),
            "n_signals": n_signals,
            "trigger": [f"[{detail}] {(t.get('title', t) if isinstance(t, dict) else t)[:90]}"
                        for t, _ in ev],
            "logic": f"if {a} ({sa}) and {b} ({sb}) contest together, their combined "
                     f"{sa + sb} seats act as one column (no three-cornered fight between them). "
                     f"Matched via {detail}.",
            "seat_map": None,
            "govt_impact": f"combined column = {sa + sb} seats",
            "recommendation": recommend(strength, n_signals, promote_at=4.0),
        })

    # =================================================================
    # TEMPLATE 4 — SPLIT: a bloc splits internally
    # =================================================================
    split_specs = [("PN", "BERSATU", "WAWASAN"), ("PH", "PKR", "BERSAMA")]
    for bloc, rump, breakaway in split_specs:
        # split signals must name the parties involved (rump or breakaway),
        # not merely any bloc-tagged item
        ev = news_for_exact(news, EVENT_CATS, blocs=[bloc],
                            parties=[rump.lower(), breakaway.lower()])
        ev += [("SWITCH", 1.5)] * len(
            [s for s in switches if s["from"] and bloc.upper() in s["from"].upper()])
        strength = sum(w for _, w in ev)
        n_signals = len(ev)
        if strength < 0.5:
            continue
        total = seat_totals.get(bloc, 0)
        drafts.append({
            "name": f"{bloc} split: {rump} rump vs {breakaway} breakaway",
            "template": "SPLIT",
            "signal_strength": round(strength, 1),
            "n_signals": n_signals,
            "trigger": fmt_ev(ev),
            "logic": f"{bloc} holds {total} seats; a hard split divides the column "
                     f"(rump keeps the core, breakaway takes the defectors).",
            "seat_map": None,
            "govt_impact": f"{bloc} column splits into two, reducing its effective size",
            "recommendation": recommend(strength, n_signals),
        })

    # =================================================================
    # Output — merge with previously filed drafts so manual/analyst entries
    # (e.g. a hand-filed BN-PN pact with N9 evidence) survive regeneration.
    # =================================================================
    out_path = os.path.join(ROOT, "work", "scenarios", "narrative_scenarios.json")
    existing = []
    if os.path.exists(out_path):
        try:
            existing = json.load(open(out_path)).get("scenarios", [])
        except Exception:
            existing = []
    gen_names = {d["name"] for d in drafts}
    merged = drafts + [e for e in existing if e["name"] not in gen_names]
    out = {
        "generated_at": __import__("datetime").datetime.now().isoformat(),
        "note": "DRAFT narrative scenarios auto-derived from live signals. "
                "Review, adjust seat maps, and promote live ones into "
                "projection_scenarios.json (category: narrative) manually. "
                "07_MANUAL/analyst-filed entries are preserved across regenerations.",
        "scenarios": merged,
    }
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"\nDRAFT narrative scenarios: {len(drafts)} generated + "
          f"{len(merged) - len(drafts)} preserved from prior filings")
    for d in drafts:
        print(f"\n[{d['template']}] {d['name']}")
        print(f"  rec: {d['recommendation']}")
        for t in d["trigger"][:2]:
            print(f"    - {t}")
        print(f"  logic: {d['logic']}")
    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
