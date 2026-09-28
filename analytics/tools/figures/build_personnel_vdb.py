#!/usr/bin/env python3
# Provenance: original path 05_AUTOMATION/build_personnel_vdb.py; original SHA-256 9d928b3ebb486dbb4c0d93ee8a4908c7086463813a20dad5afc2fa9764ed3175; classification active (figure-vector; OPS 8c1852b); versioned 2026-09-11.
"""Build the GE16 PERSONNEL vector database (all layers).

Merges every _draft/*.json layer:
  current-occupants.json   — 222 MPs + ~600 DUN + key-figure deep profiles
  federal-cabinets.json    — ministers + deputy ministers since 2018 (agent)
  excos-states-a.json      — EXCO/MB Johor..Pahang since 2018 (agent)
  excos-states-b.json      — EXCO/MB/CM Kedah..Sarawak since 2018 (agent)

Name reconciliation is the crux: the same person appears as
  "Onn Hafiz bin Ghazi" (DUN CSV) / "Onn Hafiz Ghazi" (Wikipedia, no bin)
  "Datuk Seri ..." (EXCO) / plain name (MP list)
Canonical key = lowercase, strip honorifics + patronymics (bin/binti/bte/a-l/a-p),
collapse whitespace. Roles from all layers merge onto the canonical person.

Outputs (work/figures):
  ge16-personnel.json         — master roster (persons with roles[] + key_figure)
  ge16-personnel-vectors.npy  — [N, 384] embeddings
  ge16-personnel-meta.json    — aligned metadata + aliases
  search_personnel.py         — hybrid lexical+cosine search tool
"""
import os
import re
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
# Task 4.6 contract: figure VDB artifacts and the _draft ingestion layers share
# the ANALYTICS working root. work/figures is the single figure root every
# versioned reader resolves (search tools, graph builder, explorer bundling); it
# is not a stub — the state-report fingerprint DB lives there too.
FIGURES_WORK_ROOT = resolve_repository_root() / "work" / "figures"
DRAFT_DIR = str(FIGURES_WORK_ROOT / "_draft")
OUT_DIR = os.environ.get("GE16_FIGURES_OUT_DIR") or str(FIGURES_WORK_ROOT)
MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# honorifics to strip from the FRONT of names (word boundary)
HONORIFICS = [
    "datuk seri", "datuk", "dato seri", "dato'", "dato", "tan sri", "tun",
    "ybhg", "yab", "yb", "dr", "hajah", "haji", "ustaz", "ustazah", "tuan",
    "datin seri", "datin", "puan", "prof", "professor", "mrs", "mr", "ms",
    "sri", "datin paduka", "dato' seri", "yang berhormat", "dato' sri",
]
# title words stripped ANYWHERE in the name (not just front)
TITLE_WORDS = [
    "haji", "hajah", "tun", "tan sri", "datuk seri", "datuk", "dato seri",
    "dato'", "dato", "datin", "sri", "dr", "ustaz", "ustazah", "tuan", "ybhg",
    "patinggi", "temenggong", "abang", "dayang", "ampun",
    # Muhammad-name variants (patronymic/abbreviation particles)
    "muhammad", "mohammad", "mohamad", "mohd", "muhd", "muhammd", "md",
]
PATRONYMIC = re.compile(r"\s+(bin|binti|bte|bt|a/l|a/p|a\.l\.|a\.p\.)\s+", re.I)
# trailing date/term phrases accidentally glued to names ("X from 2 December 2023")
DATE_SUFFIX = re.compile(
    r"\s+(?:from|since|until|as of)\s+(?:\d{1,2}\s+[A-Za-z]+\s+)?\d{4}\s*$", re.I)

# manual canonical overrides for names whose variants differ beyond regex
# (key: normalized-with-honorifics-stripped form -> canonical display key)
NAME_OVERRIDES = {
    "muhyiddin muhammad yassin": "muhyiddin yassin",
    "muhyiddin mohammad yassin": "muhyiddin yassin",
    "mohammad najib abdul razak": "najib abdul razak",
    "mohd najib abdul razak": "najib abdul razak",
    "mohd shahriman abdul rahman": "mohd shahriman abdul rahman",
}


def normalize(name):
    """Canonical key: lowercase, strip honorifics + patronymics + title words."""
    n = (name or "").strip()
    n = re.sub(r"[\(\[].*?[\)\]]", " ", n)          # drop parentheticals
    n = re.sub(r"\.", " ", n)                        # Dr. -> Dr
    n = DATE_SUFFIX.sub(" ", n)                      # "X from 2023" -> "X"
    n = re.sub(r"\s+", " ", n).strip()
    low = n.lower()
    for h in HONORIFICS:
        if low.startswith(h + " ") or low == h:
            n = n[len(h):].strip()
            low = n.lower()
            break
    n = PATRONYMIC.sub(" ", n)
    # strip title words anywhere (Haji/Tun/Dr mid-name)
    for t in TITLE_WORDS:
        n = re.sub(r"(?i)\b" + re.escape(t) + r"\b", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    key = n.lower()
    return NAME_OVERRIDES.get(key, key)


def load_drafts():
    """Only merge personnel-layer drafts (dict with 'persons' key)."""
    layers = []
    for path in sorted(glob.glob(os.path.join(DRAFT_DIR, "*.json"))):
        base = os.path.basename(path)
        if base.startswith("_") or base.startswith("wiki"):
            continue  # raw cache / parser artifacts, not layers
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "persons" in data:
                layers.append((base, data))
            else:
                print(f"SKIP (not a layer): {base}")
        except Exception as e:
            print(f"SKIP {path}: {e}")
    return layers


def load_factions():
    """Load the curated faction/cluster map (factions-seed.json)."""
    path = os.path.join(DRAFT_DIR, "factions-seed.json")
    if not os.path.exists(path):
        return {}
    data = json.load(open(path, encoding="utf-8"))
    out = {}
    for p in data.get("persons", []):
        key = normalize(p.get("name", ""))
        if key:
            out[key] = p
    return out


def merge_all(layers):
    """Merge layers onto canonical persons dict keyed by normalized name."""
    persons = {}   # key -> {"name": display, "aliases": set, "key_figure": bool, "roles": []}
    order = []     # first-seen order

    def get(key, display):
        if key not in persons:
            persons[key] = {"name": display, "aliases": set(), "key_figure": False, "roles": []}
            order.append(key)
        return persons[key]

    for fname, layer in layers:
        for p in layer.get("persons", []):
            display = p.get("name", "").strip()
            key = normalize(display)
            if not key:
                continue
            # cleaned display name (strip date phrases etc. from the raw name)
            cleaned = DATE_SUFFIX.sub("", display).strip()
            rec = get(key, cleaned or display)
            if p.get("key_figure"):
                rec["key_figure"] = True
                for field in ("role", "seat", "birth_year", "education", "career",
                              "history", "ideology", "faction", "prominence", "profile"):
                    if p.get(field) is not None:
                        rec[field] = p[field]
            # derive aliases from parenthetical nicknames: "Mohamad Hasan (Tok Mat)"
            m = re.search(r"\(([^()]+)\)", display)
            if m:
                rec["aliases"].add(m.group(1).strip().lower())
            for al in p.get("aliases", []) or []:
                rec["aliases"].add(al)
            for r in p.get("roles", []) or []:
                if r not in rec["roles"]:
                    rec["roles"].append(r)

    # build final list
    out = []
    for key in order:
        rec = persons[key]
        rec["aliases"] = sorted(rec["aliases"])
        # dedupe roles (dict unhashable — manual)
        seen = set()
        roles = []
        for r in rec["roles"]:
            sig = json.dumps(r, sort_keys=True)
            if sig not in seen:
                seen.add(sig)
                roles.append(r)
        rec["roles"] = roles
        out.append(rec)
    return out


def build_profile_text(p):
    parts = [p.get("name", "")]
    if p.get("role"):
        parts.append(p["role"])
    if p.get("profile"):
        parts.append(p["profile"])
    if p.get("ideology"):
        parts.append("Ideology: " + p["ideology"])
    if p.get("faction"):
        parts.append("Faction: " + p["faction"])
    if p.get("cluster"):
        parts.append("Cluster: " + p["cluster"])
    role_strs = []
    for r in p.get("roles", []):
        bits = []
        t = r.get("type", "")
        if t == "mp":
            bits.append(f"MP for {r.get('constituency', r.get('seat', ''))}")
            bits.append(f"Ahli Parlimen {r.get('constituency', r.get('seat', ''))}")
        elif t == "dun":
            bits.append(f"State assembly member {r.get('seat', '')} ({r.get('state', '')})")
            bits.append(f"Ahli Dewan Undangan Negeri {r.get('seat', '')} ({r.get('state', '')})")
        elif t == "minister":
            bits.append(f"Minister of {r.get('portfolio', '')}")
            bits.append(f"Menteri {r.get('portfolio', '')}")
        elif t == "deputy_minister":
            bits.append(f"Deputy Minister of {r.get('portfolio', '')}")
            bits.append(f"Timbalan Menteri {r.get('portfolio', '')}")
        elif t == "exco":
            bits.append(f"EXCO {r.get('portfolio', '')} ({r.get('state', '')})")
            bits.append(f"EXCO {r.get('state', '')}")
        elif t == "mb":
            bits.append(f"Menteri Besar of {r.get('state', '')}")
            bits.append(f"MB {r.get('state', '')}")
        elif t == "chief_minister":
            bits.append(f"Chief Minister of {r.get('state', '')}")
            bits.append(f"Ketua Menteri {r.get('state', '')}")
        elif t == "figure":
            bits.append("Key figure")
        if r.get("party"):
            bits.append(r["party"])
        if r.get("coalition"):
            bits.append(r["coalition"])
        if r.get("start"):
            bits.append(f"since {r['start']}")
        role_strs.append(" | ".join(b for b in bits if b))
    if role_strs:
        parts.append("Roles: " + " ; ".join(role_strs))
    if p.get("aliases"):
        parts.append("Aliases: " + ", ".join(p["aliases"]))
    history = p.get("history", [])
    if isinstance(history, list) and history:
        parts.append("History: " + " ".join(str(h) for h in history))
    return " | ".join(x for x in parts if x)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    layers = load_drafts()
    print("Layers:", [n for n, _ in layers])
    persons = merge_all(layers)

    # apply curated faction/cluster map (cascade analysis)
    factions = load_factions()
    n_frac = 0
    for p in persons:
        key = normalize(p.get("name", ""))
        f = factions.get(key)
        if f:
            if f.get("cluster"):
                p["cluster"] = f["cluster"]
            if f.get("faction") and not p.get("faction"):
                p["faction"] = f["faction"]
            n_frac += 1
    print(f"Merged {len(persons)} unique persons")
    print(f"Faction/cluster applied: {n_frac}")
    n_kf = sum(1 for p in persons if p.get("key_figure"))
    print(f"Key figures: {n_kf}")
    n_roles = sum(len(p["roles"]) for p in persons)
    print(f"Total role records: {n_roles}")

    texts = [build_profile_text(p) for p in persons]

    from fastembed import TextEmbedding
    print(f"Loading {MODEL} ...")
    embedder = TextEmbedding(model_name=MODEL,
                             cache_dir=os.path.join(OUT_DIR, ".model_cache"))
    print("Embedding ...")
    vectors = np.vstack([np.array(v, dtype="float32") for v in embedder.embed(texts)])
    print(f"Vectors: {vectors.shape}")

    master = {
        "title": "GE16 Personnel Database — MPs, ADUN, ministers, deputy ministers, EXCOs, Menteris Besar (2018–present)",
        "model": MODEL,
        "dim": int(vectors.shape[1]),
        "count": len(persons),
        "persons": persons,
    }
    with open(os.path.join(OUT_DIR, "ge16-personnel.json"), "w", encoding="utf-8") as f:
        json.dump(master, f, ensure_ascii=False, indent=1)

    np.save(os.path.join(OUT_DIR, "ge16-personnel-vectors.npy"), vectors)
    meta = []
    for i, p in enumerate(persons):
        meta.append({
            "name": p.get("name", ""),
            "role": p.get("role", ""),
            "party": _main_party(p),
            "bloc": _main_bloc(p),
            "key_figure": bool(p.get("key_figure")),
            "faction": p.get("faction", ""),
            "cluster": p.get("cluster", ""),
            "aliases": p.get("aliases", []),
            "profile": p.get("profile", ""),
            "roles": p.get("roles", []),
        })
    with open(os.path.join(OUT_DIR, "ge16-personnel-meta.json"), "w", encoding="utf-8") as f:
        json.dump({"model": MODEL, "dim": int(vectors.shape[1]), "count": len(meta), "items": meta},
                  f, ensure_ascii=False, indent=1)

    _write_search_tool()

    from collections import Counter
    by_party = Counter(_main_party(p) for p in persons)
    print("\nPersons per party (top 20):")
    for p, c in by_party.most_common(20):
        print(f"  {c:4d}  {p}")
    print(f"\nSaved: ge16-personnel.json, ge16-personnel-vectors.npy ({vectors.shape}), "
          f"ge16-personnel-meta.json, search_personnel.py")


def _main_party(p):
    for r in p.get("roles", []):
        if r.get("party"):
            return r["party"]
    return ""


def _main_bloc(p):
    for r in p.get("roles", []):
        if r.get("coalition"):
            return r["coalition"]
    return ""


def _write_search_tool():
    tool = '''#!/usr/bin/env python3
"""Search the GE16 personnel vector database (hybrid lexical + cosine).

Usage:
  .venv/bin/python -m tools.figures.search_personnel "anwar"
  .venv/bin/python -m tools.figures.search_personnel "menteri kewangan" --top 10
  .venv/bin/python -m tools.figures.search_personnel "exco selangor" --party DAP
  .venv/bin/python -m tools.figures.search_personnel "fahmi" --cluster PKR:anwar_core
"""
import os
import sys
import json
import re as _re
import numpy as np

FIG_ROOT = os.path.dirname(os.path.abspath(__file__))
VEC = os.path.join(FIG_ROOT, "ge16-personnel-vectors.npy")
META = os.path.join(FIG_ROOT, "ge16-personnel-meta.json")


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
    query = " ".join(args)

    meta_full = json.load(open(META))
    model_name = meta_full["model"]
    meta = meta_full["items"]
    vecs = np.load(VEC)
    from fastembed import TextEmbedding
    embedder = TextEmbedding(model_name=model_name,
                             cache_dir=os.path.join(FIG_ROOT, ".model_cache"))
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
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
    else:
        main()
'''
    with open(os.path.join(OUT_DIR, "search_personnel.py"), "w", encoding="utf-8") as f:
        f.write(tool)


if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
    else:
        main()
