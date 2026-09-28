#!/usr/bin/env python3
# Provenance: original path 05_AUTOMATION/build_figures_vdb.py; original SHA-256 f615e81c2e6e3a694033a464c3081bfaffb7680058452c4baed224fdffb6e48f; classification active (figure-vector; OPS 8c1852b); versioned 2026-09-11.
"""Build the GE16 Key Figures vector database.

Reads the work/figures draft inputs (bloc drafts produced by research agents),
merges all figures, embeds each figure's profile text with a multilingual
sentence-transformer model (fastembed), and writes:

  work/figures index artifacts and query tool (cosine similarity)

Profile text embedded per figure = name + role + seat + party + bloc +
ideology + faction + profile + history (joined). Search can also be run in
Malay — the model is multilingual.

Run: .venv/bin/python -m tools.figures.build_figures_vdb
Query: .venv/bin/python -m tools.figures.search_figures "ketua pemuda umno"
"""
import os
import json
import glob
import sys
try:
    import numpy as np
except ModuleNotFoundError:  # allow provenance/import checks without build extras
    np = None
from pathlib import Path

def resolve_repository_root(anchor=None):
    """Find the ANALYTICS repository root independently of cwd."""
    return Path(anchor or __file__).resolve().parents[2]


ROOT = str(resolve_repository_root())
# Task 4.6 contract: runtime figure artifacts live under work/figures.
FIGURES_WORK_ROOT = resolve_repository_root() / "work" / "figures"
DRAFT_DIR = str(FIGURES_WORK_ROOT / "_draft")
OUT_DIR = os.environ.get("GE16_FIGURES_OUT_DIR") or str(FIGURES_WORK_ROOT)
MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

os.makedirs(OUT_DIR, exist_ok=True)


def load_drafts():
    """Only merge bloc drafts (dict with 'parties' key); skip personnel layers."""
    blocs = []
    for path in sorted(glob.glob(os.path.join(DRAFT_DIR, "*.json"))):
        base = os.path.basename(path)
        if base.startswith("_") or base.startswith("wiki"):
            continue  # raw cache / parser artifacts
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "parties" in data:
                blocs.append(data)
            else:
                print(f"SKIP (not a bloc draft): {base}")
        except Exception as e:
            print(f"SKIP {path}: {e}")
    return blocs


def flatten(blocs):
    """Return list of figure dicts with bloc/party attached."""
    figs = []
    for bloc in blocs:
        bloc_name = bloc.get("bloc", "?")
        for party in bloc.get("parties", []):
            party_name = party.get("party", "?")
            for fig in party.get("figures", []):
                fig["bloc"] = bloc_name
                fig["party"] = party_name
                figs.append(fig)
    return figs


# common short names / aliases for well-known figures (name-substring key)
ALIASES = {
    "Abang Abdul Rahman Zohari Abang Openg": ["abang johari", "johari openg"],
    "Mohamad Sabu": ["mat sabu"],
    "M. Kulasegaran": ["kula", "kulasegaran"],
    "Gobind Singh Deo": ["gobind singh", "gobind"],
    "Nga Kor Ming": ["nga"],
    "Anthony Loke Siew Fook": ["loke", "anthony loke"],
    "Lim Guan Eng": ["guan eng", "lge"],
    "Lim Kit Siang": ["kit siang"],
    "Wan Azizah Wan Ismail": ["wan azizah", "dr wan azizah"],
    "Nurul Izzah Anwar": ["nurul izzah"],
    "Ahmad Zahid Hamidi": ["zahid", "ahmad zahid"],
    "Najib Razak": ["najib"],
    "Khairy Jamaluddin": ["khairy"],
    "Hishammuddin Hussein": ["hishammuddin", "hisham"],
    "Tengku Zafrul Aziz": ["zafrul", "tengku zafrul"],
    "Mohamad Hasan": ["tok mat", "mohamad hasan"],
    "Abdul Hadi Awang": ["hadi awang", "hadi"],
    "Muhyiddin Yassin": ["muhyiddin", "tan sri muhyiddin"],
    "Hamzah Zainudin": ["hamzah"],
    "Muhammad Sanusi Md Nor": ["sanusi"],
    "Ahmad Samsuri Mokhtar": ["samsuri", "ahmad samsuri"],
    "Takiyuddin Hassan": ["takiyuddin"],
    "Tuan Ibrahim Tuan Man": ["tuan ibrahim"],
    "Mukhriz Mahathir": ["mukhriz"],
    "Rafizi Ramli": ["rafizi"],
    "Nik Nazmi Nik Ahmad": ["nik nazmi"],
    "Syed Saddiq Syed Abdul Rahman": ["syed saddiq", "saddiq"],
    "Hajiji Noor": ["hajiji"],
    "Jeffrey Kitingan": ["kitingan"],
    "Shafie Apdal": ["shafie", "warisan shafie"],
    "Wee Ka Siong": ["wee ka siong", "wks"],
    "Vigneswaran Sanasee": ["vigneswaran"],
    "Zuraida Kamarudin": ["zuraida"],
    "Maszlee Malik": ["maszlee"],
    "Fadillah Haji Yusof": ["fadillah"],
    "Azmin Ali": ["azmin"],
}


def build_profile_text(fig):
    parts = [
        fig.get("name", ""),
        fig.get("role", ""),
        f"Seat: {fig.get('seat', '')}" if fig.get("seat") else "",
        f"Party: {fig.get('party', '')} ({fig.get('bloc', '')})",
        fig.get("education", ""),
        fig.get("career", ""),
        fig.get("ideology", ""),
        fig.get("faction", ""),
        fig.get("profile", ""),
    ]
    aliases = ALIASES.get(fig.get("name", ""), [])
    if aliases:
        parts.append("Aliases: " + ", ".join(aliases))
    history = fig.get("history", [])
    if isinstance(history, list):
        parts.append("History: " + " ".join(str(h) for h in history))
    return " | ".join(p for p in parts if p)


def main():
    blocs = load_drafts()
    figs = flatten(blocs)
    print(f"Loaded {len(blocs)} drafts, {len(figs)} figures")

    # de-duplicate by name (keep first occurrence, prefer non-empty profile)
    seen = {}
    for f in figs:
        key = f.get("name", "").strip().lower()
        if not key:
            continue
        if key not in seen or (len(f.get("profile", "")) > len(seen[key].get("profile", ""))):
            seen[key] = f
    figs = list(seen.values())
    print(f"After dedup: {len(figs)} unique figures")

    texts = [build_profile_text(f) for f in figs]

    # ---- embed ----
    from fastembed import TextEmbedding
    print(f"Loading model {MODEL} ...")
    embedder = TextEmbedding(model_name=MODEL, cache_dir=os.path.join(OUT_DIR, ".model_cache"))
    print("Embedding ...")
    vectors = np.vstack([np.array(v, dtype="float32") for v in embedder.embed(texts)])
    print(f"Vectors: {vectors.shape}")

    # ---- write master JSON (no vectors) ----
    master = {
        "title": "GE16 Key Political Figures — profile & history database",
        "model": MODEL,
        "dim": int(vectors.shape[1]),
        "count": len(figs),
        "figures": figs,
    }
    with open(os.path.join(OUT_DIR, "ge16-key-figures.json"), "w", encoding="utf-8") as f:
        json.dump(master, f, ensure_ascii=False, indent=1)

    # ---- write vectors + meta ----
    np.save(os.path.join(OUT_DIR, "ge16-figures-vectors.npy"), vectors)
    meta = []
    for i, fig in enumerate(figs):
        meta.append({
            "name": fig.get("name", ""),
            "role": fig.get("role", ""),
            "party": fig.get("party", ""),
            "bloc": fig.get("bloc", ""),
            "seat": fig.get("seat", ""),
            "prominence": fig.get("prominence", 0.5),
            "aliases": ALIASES.get(fig.get("name", ""), []),
            "profile": fig.get("profile", ""),
        })
    with open(os.path.join(OUT_DIR, "ge16-figures-meta.json"), "w", encoding="utf-8") as f:
        json.dump({"model": MODEL, "dim": int(vectors.shape[1]), "count": len(meta), "items": meta},
                  f, ensure_ascii=False, indent=1)

    # ---- write query tool ----
    tool = '''#!/usr/bin/env python3
"""Search the GE16 key-figures vector database (cosine similarity).

Usage:
  .venv/bin/python -m tools.figures.search_figures "anwar reformasi"
  .venv/bin/python -m tools.figures.search_figures "ketua pemuda umno" --top 10
  .venv/bin/python -m tools.figures.search_figures "sabah ketua menteri" --party GRS
"""
import os
import sys
import json
import numpy as np

FIG_ROOT = os.path.dirname(os.path.abspath(__file__))
VEC = os.path.join(FIG_ROOT, "ge16-figures-vectors.npy")
META = os.path.join(FIG_ROOT, "ge16-figures-meta.json")


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
    query = " ".join(args)

    meta_full = json.load(open(META))
    model_name = meta_full["model"]
    meta = meta_full["items"]
    vecs = np.load(VEC)
    from fastembed import TextEmbedding
    embedder = TextEmbedding(model_name=model_name,
                             cache_dir=os.path.join(FIG_ROOT, ".model_cache"))
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
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
    else:
        main()
'''
    with open(os.path.join(OUT_DIR, "search_figures.py"), "w", encoding="utf-8") as f:
        f.write(tool)

    # summary by party
    from collections import Counter
    by_party = Counter(f["party"] for f in figs)
    print("\nFigures per party:")
    for p, c in by_party.most_common():
        print(f"  {c:3d}  {p}")
    print(f"\nSaved: ge16-key-figures.json, ge16-figures-vectors.npy ({vectors.shape}), "
          f"ge16-figures-meta.json, search_figures.py")


if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
    else:
        main()
