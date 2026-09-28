#!/usr/bin/env python3
# Provenance: original path 05_AUTOMATION/build_remaining_vdbs.py; original SHA-256 a5da7f3c3016b126ed1f26e37f4ef708bd7d3e37e3ca84f2b46ac74120ec550a; classification active (figure-vector; OPS 8c1852b); versioned 2026-09-11.
"""Build the remaining GE16 vector DBs: SEATS, NEWS, SCENARIOS, CLUSTERS.

Fills the coverage gap found in the 15 Aug audit — only persons + parties had
embeddings. This adds semantic search for the other entity types, using the
same model as the existing DBs:
  sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2  (384-d)

Inputs:
  graph, seat, DUN mapping, and accepted-news inputs
  work/scenarios/narrative_scenarios.json             (scenario narratives)

Outputs (work/figures):
  ge16-seats-vectors.npy / ge16-seats-meta.json
  ge16-news-vectors.npy  / ge16-news-meta.json
  ge16-scenarios-vectors.npy / ge16-scenarios-meta.json
  ge16-clusters-vectors.npy / ge16-clusters-meta.json
  search_seats.py / search_news.py / search_scenarios.py / search_clusters.py
"""
import os
import json
import sys
try:
    import numpy as np
except ModuleNotFoundError:  # allow provenance/import checks without build extras
    np = None
from collections import Counter
from pathlib import Path

def resolve_repository_root(anchor=None):
    """Find the ANALYTICS repository root independently of cwd."""
    return Path(anchor or __file__).resolve().parents[2]


ROOT = str(resolve_repository_root())
DATA_ROOT = resolve_repository_root().parent / "1_DATA"
REQUIRED_DATA_RELATIVES = (
    "research/derived/master-list-222-parliamentary-seats.csv",
    "research/derived/dun_to_parliament_mapping.json",
)


def canonical_path(*relative_parts):
    path = DATA_ROOT.joinpath("research", *relative_parts)
    if not path.exists():
        raise FileNotFoundError(f"Required canonical DATA input is missing: {path}")
    return path


# Task 4.6 contract: runtime figure and graph artifacts live under work/.
FIGURES_WORK_ROOT = resolve_repository_root() / "work" / "figures"
GRAPH_WORK_PATH = resolve_repository_root() / "work" / "graph" / "ge16-knowledge-graph.json"
FIG = os.environ.get("GE16_FIGURES_OUT_DIR") or str(FIGURES_WORK_ROOT)
# Accepted-news inputs are canonical DATA (1_DATA/research/trackers) — the same
# root the graph builder reads; work/tracking/ only carries the run tracker.
TRACKING_WORK_ROOT = str(canonical_path("trackers"))
SCENARIOS_WORK_ROOT = os.path.join(ROOT, "work", "scenarios")
MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DIM = 384


def load_graph():
    return json.load(open(GRAPH_WORK_PATH))


def embed(texts):
    from fastembed import TextEmbedding
    embedder = TextEmbedding(model_name=MODEL, cache_dir=os.path.join(FIG, ".model_cache"))
    return np.vstack([np.array(v, dtype="float32") for v in embedder.embed(texts)])


def save(name, texts, items):
    vecs = embed(texts)
    np.save(os.path.join(FIG, f"ge16-{name}-vectors.npy"), vecs)
    with open(os.path.join(FIG, f"ge16-{name}-meta.json"), "w", encoding="utf-8") as f:
        json.dump({"model": MODEL, "dim": int(vecs.shape[1]), "count": len(items),
                   "items": items}, f, ensure_ascii=False, indent=1)
    print(f"  ge16-{name}: {len(items)} items x {vecs.shape[1]} dim")
    return vecs.shape[0]


def build_seats():
    import csv
    g = load_graph()
    seat_nodes = [n for n in g["nodes"].values() if n.get("type") == "seat"]

    # state map: parliament code -> state (from master list); DUN code -> state via d2p
    fed_state = {}
    with open(canonical_path("derived", "master-list-222-parliamentary-seats.csv")) as f:
        for r in csv.DictReader(f):
            fed_state[r["code"].strip().upper()] = r.get("state", "").strip()
    d2p = json.load(open(canonical_path("derived", "dun_to_parliament_mapping.json")))
    # DUN code space is NOT globally unique (every state has N.01..N.xx), so match
    # by constituency NAME (unique per state) rather than bare code.
    dun_by_name = {}
    for d in d2p:
        key = d.get("dun_name", "").strip().lower()
        if key:
            dun_by_name[key] = (d["dun"].strip().upper(), d.get("dun_name", "").strip(),
                                fed_state.get(d["parliament"].strip().upper(), ""))
    # manual state fallback for seats whose name differs from the d2p mapping
    STATE_OVERRIDE = {
        "temengor": "Perak", "tanjung aru": "Sabah",
        "tualang sekah": "Perak", "tanjung batu": "Sabah",
    }

    texts, items = [], []
    for n in sorted(seat_nodes, key=lambda x: x["id"]):
        raw_name = n.get("name", "") or ""
        kind = n.get("kind", "")
        code = raw_name.split(" ")[0].upper() if raw_name else ""
        constituency = n.get("constituency", "")
        state = n.get("state", "")
        if kind == "dun":
            # KG DUN name is already "N.25 Kajang" — split code + constituency
            # directly, then resolve state via the constituency name.
            if not constituency and len(raw_name.split(" ")) >= 2:
                constituency = raw_name.split(" ", 1)[1].strip()
            if not state and constituency:
                hit = dun_by_name.get(constituency.strip().lower())
                if hit:
                    state = hit[2]
                else:
                    state = STATE_OVERRIDE.get(constituency.strip().lower(), "")
        chamber = "Parliament (Dewan Rakyat)" if kind == "federal" else "State Assembly (DUN)"
        t = f"{code} {constituency} {state} {chamber} seat constituency kawasan parlimen dun"
        texts.append(t)
        items.append({"id": n["id"], "code": code, "name": raw_name,
                      "constituency": constituency, "state": state, "kind": kind,
                      "chamber": chamber})
    return save("seats", texts, items)


def build_news():
    path = os.path.join(TRACKING_WORK_ROOT, "ge16-news-accepted.json")
    if not os.path.exists(path):
        import warnings
        warnings.warn(f"Optional generated tracking input is missing: {path}", RuntimeWarning)
        return
    na = json.load(open(path))
    items_all = na.get("items", [])
    texts, items = [], []
    for it in items_all:
        title = it.get("title", "")
        category = it.get("category", "")
        blocs = " ".join(it.get("blocs", []) or [])
        parties = " ".join(it.get("parties", []) or [])
        seats = " ".join(it.get("seats", []) or [])
        t = f"{title} | category: {category} | blocs: {blocs} | parties: {parties} | seats: {seats}"
        texts.append(t)
        items.append({"title": title, "date": it.get("date", ""), "source": it.get("source", ""),
                      "link": it.get("link", ""), "category": category,
                      "blocs": it.get("blocs", []), "parties": it.get("parties", []),
                      "seats": it.get("seats", []), "score": it.get("score", 0)})
    return save("news", texts, items)


def build_scenarios():
    ns = json.load(open(os.path.join(SCENARIOS_WORK_ROOT, "narrative_scenarios.json")))
    sm = json.load(open(os.path.join(SCENARIOS_WORK_ROOT, "scenario_meta.json")))
    scens = ns.get("scenarios", [])
    texts, items = [], []
    for s in scens:
        name = s.get("name", "")
        template = s.get("template", "")
        logic = s.get("logic", "")
        trigger = " ".join(s.get("trigger", []) or [])
        impact = s.get("govt_impact", "")
        rec = s.get("recommendation", "")
        # also append parametric basis if present in meta
        basis = ""
        if isinstance(sm, dict):
            for k, v in sm.items():
                if k == name or name.startswith(k):
                    basis = v.get("basis", "") if isinstance(v, dict) else ""
                    break
        t = f"{name} | template {template} | {logic} | triggers: {trigger} | impact: {impact} | recommendation: {rec} | basis: {basis}"
        texts.append(t)
        items.append({"name": name, "template": template, "signal_strength": s.get("signal_strength"),
                      "n_signals": s.get("n_signals"), "trigger": s.get("trigger", []),
                      "logic": logic, "govt_impact": impact, "recommendation": rec,
                      "seat_map": s.get("seat_map", {})})
    return save("scenarios", texts, items)


def build_clusters():
    g = load_graph()
    clusters = [n for n in g["nodes"].values() if n.get("type") == "cluster"]
    texts, items = [], []
    for n in sorted(clusters, key=lambda x: x["id"]):
        # cluster name is like "PAS:hadi"; enrich with member names via edges
        t = n.get("name", "")
        texts.append(t)
        items.append({"id": n["id"], "name": t})
    return save("clusters", texts, items)


def main():
    print("Building missing vector DBs …")
    build_seats()
    build_news()
    build_scenarios()
    build_clusters()
    print("Done.")


if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
    else:
        main()
