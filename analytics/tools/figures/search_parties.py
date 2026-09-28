#!/usr/bin/env python3
# Provenance: original path 01_RESEARCH/figures/search_parties.py; original SHA-256 df9310b62b44a3a9b6c5697b2f0ae7995bc76152ffc26747d0f9479f12827505; classification active (figure-search; OPS 8c1852b); versioned 2026-09-11.
"""Search the GE16 political parties vector database (hybrid lexical + cosine).

Usage:
  python3 -m tools.figures.search_parties "islamist ideology"
  python3 -m tools.figures.search_parties "parti melayu" --top 10
  python3 -m tools.figures.search_parties "sabah" --bloc GRS
"""
import os
import sys
import json
import re as _re
try:
    import numpy as np
except ModuleNotFoundError:  # allow provenance/import checks without search extras
    np = None
from pathlib import Path


def resolve_repository_root(anchor=None):
    """Find the ANALYTICS repository root independently of cwd."""
    return Path(anchor or __file__).resolve().parents[2]


ROOT = str(resolve_repository_root())
# Task 4.6 contract: runtime figure artifacts live under work/figures.
FIGURES_WORK_ROOT = resolve_repository_root() / "work" / "figures"
VEC = str(FIGURES_WORK_ROOT / "ge16-parties-vectors.npy")
META = str(FIGURES_WORK_ROOT / "ge16-parties-meta.json")


def main():
    # robust arg parsing: consume option values so "--top 3" doesn't leak "3" into the query
    args = []
    top = 10
    bloc_filter = None
    i = 1
    argv = sys.argv
    while i < len(argv):
        a = argv[i]
        if a == "--top" or a.startswith("--top="):
            if "=" in a:
                top = int(a.split("=", 1)[1])
            else:
                top = int(argv[i + 1])
                i += 1
        elif a == "--bloc" or a.startswith("--bloc="):
            if "=" in a:
                bloc_filter = a.split("=", 1)[1].upper()
            else:
                bloc_filter = argv[i + 1].upper()
                i += 1
        elif a.startswith("--"):
            pass
        else:
            args.append(a)
        i += 1
    if not args:
        print(__doc__)
        return
    if np is None:
        raise SystemExit("numpy is required to search the parties vector database")
    query = " ".join(args)

    meta_full = json.load(open(META))
    model_name = meta_full["model"]
    meta = meta_full["items"]
    vecs = np.load(VEC)
    from fastembed import TextEmbedding
    embedder = TextEmbedding(model_name=model_name,
                             cache_dir=str(FIGURES_WORK_ROOT / ".model_cache"))
    qv = np.array(list(embedder.embed([query]))[0], dtype="float32")

    # hybrid scoring
    q_tokens = set(_re.findall(r"[a-z0-9]+", query.lower()))
    SYNONYMS = {
        "parti": ["party"], "parti politik": ["political party"], "kerajaan": ["government"],
        "pilihan raya": ["election"], "undi": ["vote"], "ideologi": ["ideology"],
        "melayu": ["malay"], "islam": ["islamist", "islamic"], "cina": ["chinese"],
        "indian": ["indian"], "sabah": ["sabah"], "sarawak": ["sarawak"],
        "pembangkang": ["opposition"], "pemerintah": ["government", "ruling"],
    }
    expanded = set(q_tokens)
    for ms, ens in SYNONYMS.items():
        mt = set(_re.findall(r"[a-z0-9]+", ms))
        if mt and mt <= q_tokens:
            for en in ens:
                expanded.update(_re.findall(r"[a-z0-9]+", en))

    def lex_score(it):
        if not q_tokens:
            return 0.0
        name_toks = set(_re.findall(r"[a-z0-9]+", (it.get("name") or "").lower()))
        alias_toks = set(_re.findall(r"[a-z0-9]+", " ".join(it.get("aliases") or []).lower()))
        blob = (it.get("ideology", "") + " " + it.get("profile", "") + " " + it.get("bloc", "")
                + " " + " ".join(it.get("leaders") or []))
        blob_toks = set(_re.findall(r"[a-z0-9]+", blob.lower()))
        n_hit = len(expanded & (name_toks | alias_toks))
        b_hit = len(expanded & blob_toks)
        if n_hit == len(expanded) and n_hit > 0:
            return 1.0
        return 0.6 * (n_hit / max(len(expanded), 1)) + 0.4 * (b_hit / max(len(expanded), 1))

    qn = qv / (np.linalg.norm(qv) + 1e-9)
    vn = vecs / (np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9)
    cos = vn @ qn

    scores = []
    for i, it in enumerate(meta):
        combined = 0.55 * lex_score(it) + 0.45 * float(cos[i])
        scores.append((combined, i))
    scores.sort(key=lambda x: -x[0])

    shown = 0
    print("Query: " + query + "  (" + str(len(meta)) + " parties indexed)")
    print()
    for combined, i in scores:
        it = meta[i]
        if bloc_filter and it.get("bloc", "").upper() != bloc_filter:
            continue
        leaders = ", ".join(it.get("leaders") or [])[:80]
        print(f"{combined:.3f}  {it['name']}  [bloc {it.get('bloc','')}] "
              f"mp={it.get('mp_seats',0)} dun={it.get('dun_seats',0)} | {it.get('ideology','')[:50]}")
        if leaders:
            print(f"        leaders: {leaders}")
        shown += 1
        if shown >= top:
            break


if __name__ == "__main__":
    main()
