#!/usr/bin/env python3
# Provenance: original path 05_AUTOMATION/build_parties_vdb.py; original SHA-256 fe0172f5046d91456b49191c040cc5303fc2be74d63a31b995f668ea673866db; classification active (figure-vector; OPS 8c1852b); versioned 2026-09-11.
"""Build the GE16 POLITICAL PARTIES vector database.

Merges:
  _draft/parties-seed.json       — curated party profiles (ideology, history, posture)
  _draft/party-updates-*.json    — cron-written party facts (mergers, formations, splits)
  ge16-personnel.json            — computed cross-links: seat counts, leaders, member counts
                                   (personnel roles carry "party" — the natural join key)

Each party record:
  name, aliases, bloc, founded, ideology, character, history[], ge16_posture,
  prominence, + COMPUTED: seats_now, leaders[] (key figures), member_count,
  key_figure_names[] (for cross-DB lookup)

Outputs (work/figures):
  ge16-parties.json          — master party roster
  ge16-parties-vectors.npy   — [N, 384] embeddings
  ge16-parties-meta.json     — aligned metadata
  search_parties.py          — hybrid lexical+cosine search (EN+MS)
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
DRAFT = str(FIGURES_WORK_ROOT / "_draft")
OUT_DIR = os.environ.get("GE16_FIGURES_OUT_DIR") or str(FIGURES_WORK_ROOT)
MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

try:  # runnable as a script (python tools/figures/build_parties_vdb.py) …
    from .build_personnel_vdb import normalize  # reuse name reconciliation
except ImportError:  # … or as a module (-m tools.figures.build_parties_vdb)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from build_personnel_vdb import normalize

# party name normalization: map role-party strings to canonical party names
PARTY_ALIAS_MAP = {
    "umno": "UMNO", "pas": "PAS", "dap": "DAP", "pkr": "PKR",
    "bersatu": "BERSATU", "wawasan": "WAWASAN", "amanah": "AMANAH",
    "mca": "MCA", "mic": "MIC", "pbb": "PBB", "prs": "PRS", "supp": "SUPP",
    "pdp": "PDP", "star": "STAR", "pbs": "PBS", "gagasan": "GAGASAN",
    "upko": "UPKO", "kdm": "KDM", "pbm": "PBM", "muda": "MUDA",
    "bersama": "Bersama", "gerakan": "GERAKAN", "pejuang": "PEJUANG",
    "warisan": "WARISAN", "pbrs": "PBRS", "pgrs": "GRS",
    # "Parti Wawasan" is the same organisation as the seed's WAWASAN; without
    # this fold a cron layer under the long name forks a second party record.
    "parti wawasan": "WAWASAN",
    "bn (umno)": "UMNO", "bn (mca)": "MCA", "bn (mic)": "MIC",
    "ph (dap)": "DAP", "ph (pkr)": "PKR", "ph (amanah)": "AMANAH",
    "pn (bersatu)": "BERSATU", "pn (pas)": "PAS", "gps (pbb)": "PBB",
    "direct": "IND", "independent": "IND",
}


def canonical_party_key(name):
    """Canonical party key: normalized name folded through the alias map.

    Variants such as "Parti Wawasan" must land on the seed key "wawasan"
    instead of forking a duplicate record for the same organisation.
    """
    bare = normalize(name)
    return normalize(PARTY_ALIAS_MAP.get(bare, bare))


def load_seed():
    data = json.load(open(os.path.join(DRAFT, "parties-seed.json"), encoding="utf-8"))
    parties = {}
    for p in data.get("parties", []):
        key = normalize(p["name"])
        parties[key] = p
    return parties


# Leading date token of a history line — used only for chronological ordering.
LEADING_ISO_DATE = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})")
LEADING_WRITTEN_DATE = re.compile(r"^\s*(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4})")
LEADING_YEAR = re.compile(r"^\s*(\d{4})")
MONTH_NUMBERS = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
    "jul": "07", "aug": "08", "sep": "09", "sept": "09", "oct": "10",
    "nov": "11", "dec": "12",
    "january": "01", "february": "02", "march": "03", "april": "04",
    "june": "06", "july": "07", "august": "08", "september": "09",
    "october": "10", "november": "11", "december": "12",
}


def leading_date_token(line):
    """Sortable date token at the start of a history line, or None if unparsable."""
    iso = LEADING_ISO_DATE.match(line)
    if iso:
        return f"{iso.group(1)}-{iso.group(2)}-{iso.group(3)}"
    written = LEADING_WRITTEN_DATE.match(line)
    if written:
        month = MONTH_NUMBERS.get(written.group(2).lower())
        if month:
            return f"{written.group(3)}-{month}-{int(written.group(1)):02d}"
    year = LEADING_YEAR.match(line)
    if year:
        return year.group(1)
    return None


def merge_history(*layers):
    """Append-only union of history lines across every layer.

    History is curated seed doctrine plus each cron layer's dated findings, so a
    newer layer must never delete an older line: the layers are unioned and
    deduped by exact string. Dated lines are ordered chronologically by their
    leading date token; lines without a parseable leading date keep first-seen
    order and come last.
    """
    seen = set()
    dated = []
    undated = []
    for layer in layers:
        for line in layer or []:
            text = str(line).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            token = leading_date_token(text)
            if token is None:
                undated.append(text)
            else:
                dated.append((token, len(seen), text))
    dated.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in dated] + undated


def load_updates():
    """Cron party-update files, oldest layer first.

    Returns (newest, layers):
      newest — canonical key -> the newest layer's record. Scalar fields
               (ideology, character, ge16_posture, bloc, founded) win from the
               newest layer, and parties absent from the seed are added here.
      layers — canonical key -> that key's records from every layer, oldest
               first: the source for the append-only history union.
    """
    newest = {}
    layers = {}
    for path in sorted(glob.glob(os.path.join(DRAFT, "party-updates-*.json"))):
        try:
            data = json.load(open(path, encoding="utf-8"))
        except Exception as e:
            print(f"SKIP {path}: {e}")
            continue
        for p in data.get("parties", []):
            key = canonical_party_key(p["name"])
            newest[key] = p
            layers.setdefault(key, []).append(p)
            print(f"  party update: {p.get('name')} ({os.path.basename(path)})")
    return newest, layers


def compute_from_personnel(parties):
    """Join personnel DB: federal/state seats, leaders, member counts per party."""
    personnel = json.load(open(os.path.join(OUT_DIR, "ge16-personnel.json"), encoding="utf-8"))
    mp_seats = {}
    dun_seats = {}
    members = {}
    leaders = {}
    for p in personnel.get("persons", []):
        for r in p.get("roles", []):
            rp = r.get("party", "")
            if not rp:
                continue
            canon = PARTY_ALIAS_MAP.get(normalize(rp), rp)
            canon = canon.upper()
            # map to seed keys
            seed_key = None
            for sk in parties:
                if sk.upper() == canon or canon in sk.upper():
                    seed_key = sk
                    break
            if seed_key is None:
                continue
            members.setdefault(seed_key, set()).add(p["name"])
            if r.get("type") == "mp":
                mp_seats[seed_key] = mp_seats.get(seed_key, 0) + 1
            elif r.get("type") == "dun":
                dun_seats[seed_key] = dun_seats.get(seed_key, 0) + 1
            if p.get("key_figure"):
                leaders.setdefault(seed_key, []).append((p["name"], r.get("type", "")))
    return mp_seats, dun_seats, members, leaders


# coalition → component parties (for federal seat rollups)
COALITION_COMPONENTS = {
    "PH": ["PKR", "DAP", "AMANAH", "MUDA"],
    "BN": ["UMNO", "MCA", "MIC", "PBRS"],
    "PN": ["PAS", "BERSATU", "WAWASAN", "GERAKAN", "PEJUANG"],
    "GPS": ["PBB", "PRS", "SUPP", "PDP"],
    "GRS": ["GRS", "GAGASAN", "PBS", "STAR", "UPKO"],
}


def rollup(seed_key, mp_seats, dun_seats):
    """For coalition keys (PH/BN/PN/GPS/GRS in seed), sum component seats."""
    for coalkey, comps in COALITION_COMPONENTS.items():
        if normalize(coalkey) == seed_key:
            m = sum(mp_seats.get(normalize(c), 0) for c in comps)
            d = sum(dun_seats.get(normalize(c), 0) for c in comps)
            return m, d
    return mp_seats.get(seed_key, 0), dun_seats.get(seed_key, 0)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    parties = load_seed()
    updates, layers = load_updates()
    for k, v in updates.items():
        histories = [rec.get("history") for rec in layers.get(k, [])]
        if k in parties:
            for f in ("ideology", "character", "ge16_posture", "bloc", "founded"):
                if v.get(f):
                    parties[k][f] = v[f]
            merged = merge_history(parties[k].get("history"), *histories)
            if merged:
                parties[k]["history"] = merged
        else:
            record = dict(v)
            merged = merge_history(*histories)
            if merged:
                record["history"] = merged
            parties[k] = record

    seats, dun, members, leaders = compute_from_personnel(parties)

    # attach computed fields
    for key, p in parties.items():
        m, d = rollup(key, seats, dun)
        p["mp_seats"] = m
        p["dun_seats"] = d
        p["member_count"] = len(members.get(key, set()))
        ls = sorted(leaders.get(key, []), key=lambda x: -1 if x[1] == "minister" else 0)
        seen = set()
        uniq = []
        for n, t in ls:
            if n not in seen:
                seen.add(n)
                uniq.append(n)
        p["leaders"] = uniq[:8]
        p["key_figure_names"] = sorted(uniq)

    party_list = [parties[k] for k in sorted(parties)]

    texts = []
    for p in party_list:
        parts = [
            p.get("name", ""),
            f"Bloc: {p.get('bloc', '')}",
            f"Founded: {p.get('founded', '')}",
            f"Ideology: {p.get('ideology', '')}",
            f"Character: {p.get('character', '')}",
            f"GE16 posture: {p.get('ge16_posture', '')}",
            f"Current federal seats: {p.get('mp_seats', 0)}",
            f"State assembly seats: {p.get('dun_seats', 0)}",
            "Leaders: " + ", ".join(p.get("leaders", [])),
        ]
        if p.get("aliases"):
            parts.append("Aliases: " + ", ".join(p["aliases"]))
        history = p.get("history", [])
        if isinstance(history, list) and history:
            parts.append("History: " + " ".join(str(h) for h in history))
        texts.append(" | ".join(x for x in parts if x))

    from fastembed import TextEmbedding
    print(f"Embedding {len(party_list)} parties ...")
    embedder = TextEmbedding(model_name=MODEL,
                             cache_dir=os.path.join(OUT_DIR, ".model_cache"))
    vectors = np.vstack([np.array(v, dtype="float32") for v in embedder.embed(texts)])
    print(f"Vectors: {vectors.shape}")

    master = {
        "title": "GE16 Political Parties Database — profiles, ideology, history, leadership",
        "model": MODEL,
        "dim": int(vectors.shape[1]),
        "count": len(party_list),
        "parties": party_list,
    }
    with open(os.path.join(OUT_DIR, "ge16-parties.json"), "w", encoding="utf-8") as f:
        json.dump(master, f, ensure_ascii=False, indent=1)

    np.save(os.path.join(OUT_DIR, "ge16-parties-vectors.npy"), vectors)
    meta = []
    for i, p in enumerate(party_list):
        meta.append({
            "name": p.get("name", ""),
            "bloc": p.get("bloc", ""),
            "founded": p.get("founded", ""),
            "ideology": p.get("ideology", ""),
            "mp_seats": p.get("mp_seats", 0),
            "dun_seats": p.get("dun_seats", 0),
            "leaders": p.get("leaders", []),
            "aliases": p.get("aliases", []),
            "profile": p.get("character", ""),
        })
    with open(os.path.join(OUT_DIR, "ge16-parties-meta.json"), "w", encoding="utf-8") as f:
        json.dump({"model": MODEL, "dim": int(vectors.shape[1]), "count": len(meta), "items": meta},
                  f, ensure_ascii=False, indent=1)

    _write_search_tool()

    print("\nParties:")
    for p in party_list:
        print(f"  {p['name']:10s} bloc={p.get('bloc',''):14s} mp={p.get('mp_seats',0):3d} "
              f"dun={p.get('dun_seats',0):4d} members={p.get('member_count',0):4d} "
              f"leaders={len(p.get('leaders', []))}")
    print(f"\nSaved: ge16-parties.json, ge16-parties-vectors.npy ({vectors.shape}), "
          f"ge16-parties-meta.json, search_parties.py")


def _write_search_tool():
    tool = '''#!/usr/bin/env python3
"""Search the GE16 political parties vector database (hybrid lexical + cosine).

Usage:
  .venv/bin/python -m tools.figures.search_parties "islamist ideology"
  .venv/bin/python -m tools.figures.search_parties "parti melayu" --top 10
  .venv/bin/python -m tools.figures.search_parties "sabah" --bloc GRS
"""
import os
import sys
import json
import re as _re
import numpy as np

FIG_ROOT = os.path.dirname(os.path.abspath(__file__))
VEC = os.path.join(FIG_ROOT, "ge16-parties-vectors.npy")
META = os.path.join(FIG_ROOT, "ge16-parties-meta.json")


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
    query = " ".join(args)

    meta_full = json.load(open(META))
    model_name = meta_full["model"]
    meta = meta_full["items"]
    vecs = np.load(VEC)
    from fastembed import TextEmbedding
    embedder = TextEmbedding(model_name=model_name,
                             cache_dir=os.path.join(FIG_ROOT, ".model_cache"))
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
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
    else:
        main()
'''
    with open(os.path.join(OUT_DIR, "search_parties.py"), "w", encoding="utf-8") as f:
        f.write(tool)


if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
    else:
        main()
