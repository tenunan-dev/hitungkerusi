#!/usr/bin/env python3
# Provenance: original path 01_RESEARCH/figures/search_figures.py; original SHA-256 893300b0d1aae0a39f2abe3d72b3120f51687747420f7cf4d2f72e016189f309; classification active (figure-search; OPS 8c1852b); versioned 2026-09-11.
"""Search the GE16 key-figures vector database (cosine similarity).

Usage:
  python3 -m tools.figures.search_figures "anwar reformasi"
  python3 -m tools.figures.search_figures "ketua pemuda umno" --top 10
  python3 -m tools.figures.search_figures "sabah ketua menteri" --party GRS
"""
import os
import sys
import json
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
VEC = str(FIGURES_WORK_ROOT / "ge16-figures-vectors.npy")
META = str(FIGURES_WORK_ROOT / "ge16-figures-meta.json")


def main():
    # robust arg parsing: consume option values so "--top 3" doesn't leak "3" into the query
    args = []
    top = 10
    party_filter = None
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
        elif a == "--party" or a.startswith("--party="):
            if "=" in a:
                party_filter = a.split("=", 1)[1].upper()
            else:
                party_filter = argv[i + 1].upper()
                i += 1
        elif a.startswith("--"):
            pass  # unknown option, ignore
        else:
            args.append(a)
        i += 1
    if not args:
        print(__doc__)
        return
    if np is None:
        raise SystemExit("numpy is required to search the figures vector database")
    query = " ".join(args)

    meta_full = json.load(open(META))
    model_name = meta_full["model"]
    meta = meta_full["items"]
    vecs = np.load(VEC)
    from fastembed import TextEmbedding
    embedder = TextEmbedding(model_name=model_name,
                             cache_dir=str(FIGURES_WORK_ROOT / ".model_cache"))
    qv = np.array(list(embedder.embed([query]))[0], dtype="float32")

    # ---- hybrid scoring: lexical (names/roles) + semantic (cosine) ----
    # Semantic vectors are weak on proper nouns (esp. Malay names), so a
    # token-overlap score on name/role/seat/party runs first and is blended.
    import re as _re
    q_tokens = set(_re.findall(r"[a-z0-9]+", query.lower()))

    def lex_score(it):
        if not q_tokens:
            return 0.0
        name_toks = set(_re.findall(r"[a-z0-9]+", (it.get("name") or "").lower()))
        alias_toks = set(_re.findall(r"[a-z0-9]+", " ".join(it.get("aliases") or []).lower()))
        role_toks = set(_re.findall(r"[a-z0-9]+", (it.get("role") or "").lower()))
        other_toks = set(_re.findall(r"[a-z0-9]+",
                                     ((it.get("seat") or "") + " " + (it.get("party") or "") + " "
                                      + (it.get("bloc") or "") + " " + (it.get("profile") or "")[:200]).lower()))
        n_hit = len(q_tokens & (name_toks | alias_toks))
        r_hit = len(q_tokens & role_toks)
        o_hit = len(q_tokens & other_toks)
        if n_hit == len(q_tokens) and n_hit > 0:
            return 1.0  # all query words in the name or an alias
        return 0.55 * (n_hit / max(len(q_tokens), 1)) + 0.25 * (r_hit / max(len(q_tokens), 1)) + 0.2 * (o_hit / max(len(q_tokens), 1))

    # cosine similarity
    qn = qv / (np.linalg.norm(qv) + 1e-9)
    vn = vecs / (np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9)
    cos = vn @ qn

    scores = []
    for i, it in enumerate(meta):
        combined = 0.60 * lex_score(it) + 0.40 * float(cos[i])
        scores.append((combined, cos[i], i))
    scores.sort(key=lambda x: -x[0])

    shown = 0
    print("Query: " + query + "  (" + str(len(meta)) + " figures indexed)")
    print()
    for combined, cos, i in scores:
        it = meta[i]
        if party_filter and it["party"].upper() != party_filter:
            continue
        print(f"{combined:.3f}  {it['name']}  [{it['party']}/{it['bloc']}] {it['role']}")
        if it.get("profile"):
            print(f"        {it['profile'][:120]}")
        shown += 1
        if shown >= top:
            break


if __name__ == "__main__":
    main()
