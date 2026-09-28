#!/usr/bin/env python3
# Provenance: original path 01_RESEARCH/figures/search_personnel.py; original SHA-256 864920b7217a42e024ec1b16e9beadd95d60e0f76f28271489604cfd0c05f80a; classification active (figure-search; OPS 8c1852b); versioned 2026-09-11.
"""Search the GE16 personnel vector database (hybrid lexical + cosine).

Usage:
  python3 -m tools.figures.search_personnel "anwar"
  python3 -m tools.figures.search_personnel "menteri kewangan" --top 10
  python3 -m tools.figures.search_personnel "exco selangor" --party DAP
  python3 -m tools.figures.search_personnel "fahmi" --cluster PKR:anwar_core
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
VEC = str(FIGURES_WORK_ROOT / "ge16-personnel-vectors.npy")
META = str(FIGURES_WORK_ROOT / "ge16-personnel-meta.json")


def main():
    # robust arg parsing: consume option values so "--top 3" doesn't leak "3" into the query
    args = []
    top = 10
    party_filter = None
    cluster_filter = None
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
        elif a == "--cluster" or a.startswith("--cluster="):
            if "=" in a:
                cluster_filter = a.split("=", 1)[1]
            else:
                cluster_filter = argv[i + 1]
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
        raise SystemExit("numpy is required to search the personnel vector database")
    query = " ".join(args)

    meta_full = json.load(open(META))
    model_name = meta_full["model"]
    meta = meta_full["items"]
    vecs = np.load(VEC)
    from fastembed import TextEmbedding
    embedder = TextEmbedding(model_name=model_name,
                             cache_dir=str(FIGURES_WORK_ROOT / ".model_cache"))
    qv = np.array(list(embedder.embed([query]))[0], dtype="float32")

    # hybrid scoring: lexical (name/roles/party) + semantic (cosine)
    q_tokens = set(_re.findall(r"[a-z0-9]+", query.lower()))

    # Malay/English synonym expansion for common role terms
    SYNONYMS = {
        "perdana menteri": ["prime minister", "pm"],
        "menteri": ["minister", "ministry"],
        "timbalan menteri": ["deputy minister", "deputy"],
        "ketua menteri": ["chief minister"],
        "menteri besar": ["mb", "chief minister"],
        "ahli parlimen": ["mp", "member of parliament"],
        "ahli dewan undangan negeri": ["dun", "state assembly"],
        "wakil rakyat": ["mp", "adun", "representative"],
        "kewangan": ["finance"],
        "pendidikan": ["education"],
        "pertahanan": ["defence", "defense"],
        "dalam negeri": ["home affairs", "interior"],
        "luar negeri": ["foreign affairs"],
        "kesihatan": ["health"],
        "ekonomi": ["economy"],
        "belia": ["youth"],
        "wanita": ["women"],
        "pemuda": ["youth"],
        "undi": ["vote", "election"],
        "pilihan raya": ["election"],
        "kerajaan": ["government"],
    }
    expanded = set(q_tokens)
    for ms, ens in SYNONYMS.items():
        ms_tokens = set(_re.findall(r"[a-z0-9]+", ms))
        if ms_tokens and ms_tokens <= q_tokens:
            for en in ens:
                expanded.update(_re.findall(r"[a-z0-9]+", en))

    def lex_score(it):
        if not q_tokens:
            return 0.0
        name_toks = set(_re.findall(r"[a-z0-9]+", (it.get("name") or "").lower()))
        alias_toks = set(_re.findall(r"[a-z0-9]+", " ".join(it.get("aliases") or []).lower()))
        role_blob = " ".join(
            (r.get("type", "") + " " + r.get("portfolio", "") + " " + r.get("state", "")
             + " " + r.get("constituency", "") + " " + r.get("party", "") + " " + r.get("coalition", ""))
            for r in it.get("roles", []))
        role_toks = set(_re.findall(r"[a-z0-9]+", role_blob.lower()))
        other_toks = set(_re.findall(r"[a-z0-9]+",
                                     ((it.get("role") or "") + " " + (it.get("profile") or "")[:200]).lower()))
        n_hit = len(expanded & (name_toks | alias_toks))
        r_hit = len(expanded & role_toks)
        o_hit = len(expanded & other_toks)
        if n_hit == len(expanded) and n_hit > 0:
            return 1.0
        return 0.55 * (n_hit / max(len(expanded), 1)) + 0.3 * (r_hit / max(len(expanded), 1)) + 0.15 * (o_hit / max(len(expanded), 1))

    qn = qv / (np.linalg.norm(qv) + 1e-9)
    vn = vecs / (np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9)
    cos = vn @ qn

    scores = []
    for i, it in enumerate(meta):
        combined = 0.60 * lex_score(it) + 0.40 * float(cos[i])
        scores.append((combined, cos[i], i))
    scores.sort(key=lambda x: -x[0])

    shown = 0
    print("Query: " + query + "  (" + str(len(meta)) + " persons indexed)")
    print()
    for combined, cos, i in scores:
        it = meta[i]
        if party_filter and it.get("party", "").upper() != party_filter:
            continue
        if cluster_filter:
            c = it.get("cluster", "") or ""
            if c.lower() != cluster_filter.lower() and cluster_filter.lower() not in c.lower():
                continue
        # concise role history line
        role_bits = []
        for r in it.get("roles", []):
            t = r.get("type", "")
            bit = ""
            if t == "mp":
                bit = "MP " + r.get("constituency", r.get("seat", ""))
            elif t == "dun":
                bit = "ADUN " + r.get("seat", "")
            elif t == "minister":
                bit = "Min " + r.get("portfolio", "")
            elif t == "deputy_minister":
                bit = "DepMin " + r.get("portfolio", "")
            elif t == "exco":
                bit = "EXCO " + r.get("portfolio", "") + " (" + r.get("state", "") + ")"
            elif t == "mb":
                bit = "MB " + r.get("state", "")
            elif t == "chief_minister":
                bit = "CM " + r.get("state", "")
            elif t == "figure":
                continue
            party = r.get("party", "")
            if party:
                bit += " [" + party + "]"
            if bit:
                role_bits.append(bit)
        # dedupe consecutive identical
        seen_bits = []
        for b in role_bits:
            if b not in seen_bits:
                seen_bits.append(b)
        tag = "K" if it.get("key_figure") else " "
        extra = ""
        if it.get("cluster"):
            extra = "  <" + it["cluster"] + ">"
        print(f"{combined:.3f} [{tag}] {it['name']}{extra}  | " + " ; ".join(seen_bits[:8]))
        shown += 1
        if shown >= top:
            break


if __name__ == "__main__":
    main()
