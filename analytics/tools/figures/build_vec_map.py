#!/usr/bin/env python3
# Provenance: original path 05_AUTOMATION/build_vec_map.py; original SHA-256 7cc763efed5ea0937f8b12b11971d15cdca9bbffa4859e979206904b1b667174; classification active (figure-vector; OPS 8c1852b); versioned 2026-09-11.
"""Build a 2D semantic map of the GE16 vector DBs (PCA via numpy SVD).

Purpose: make cosine similarity VISIBLE — project the real 384-d MiniLM
embeddings down to 2D so clusters (blocs, parties, states) can be seen.
Pure numpy (no sklearn/umap needed): PCA = SVD of the centred matrix.

Emits work/graph-explorer/vec_map_data.js, beside the staged vec-map.html:
  window.VEC_MAP = {
    "model": "...", "projection": "pca",
    "layers": { "figures": { "points": [ {x,y,name,bloc,party,role} ... ] }, ... }
  }
"""
import json
import sys
try:
    import numpy as np
except ModuleNotFoundError:  # allow provenance/import checks without build extras
    np = None
import os
from pathlib import Path

def resolve_repository_root(anchor=None):
    """Find the ANALYTICS repository root independently of cwd."""
    return Path(anchor or __file__).resolve().parents[2]


ROOT = str(resolve_repository_root())
# Task 4.6 contract: runtime figure artifacts live under work/figures.
FIGURES_WORK_ROOT = resolve_repository_root() / "work" / "figures"
FIG = str(FIGURES_WORK_ROOT)
# Task 4.6: this is a servable bundle, not a tracked-source artifact.
OUT = os.path.join(ROOT, "work", "graph-explorer", "vec_map_data.js")

LAYERS = [
    ("figures", "ge16-figures-meta.json", "ge16-figures-vectors.npy",
     lambda i: {"name": i.get("name"), "bloc": i.get("bloc", ""),
                "party": i.get("party", ""), "role": (i.get("role") or "")[:60]}),
    ("personnel", "ge16-personnel-meta.json", "ge16-personnel-vectors.npy",
     lambda i: {"name": i.get("name"), "bloc": i.get("bloc", ""),
                "party": i.get("party", ""),
                "role": (i.get("profile") or "")[:60]}),
    ("seats", "ge16-seats-meta.json", "ge16-seats-vectors.npy",
     lambda i: {"name": i.get("name"),
                # seats have NO party/bloc — colour by STATE, not by kind.
                "bloc": i.get("state", ""), "party": "",
                "role": (i.get("constituency") or "")[:60]}),
    ("scenarios", "ge16-scenarios-meta.json", "ge16-scenarios-vectors.npy",
     lambda i: {"name": i.get("name"), "bloc": i.get("template", ""),
                "party": "", "role": (i.get("logic") or "")[:80]}),
    ("news", "ge16-news-meta.json", "ge16-news-vectors.npy",
     lambda i: {"name": (i.get("title") or "")[:70], "bloc": " · ".join(i.get("blocs", []) or [])[:30],
                "party": " · ".join(i.get("parties", []) or [])[:30],
                "role": (i.get("category") or "")[:40]}),
]


def pca2d(vecs, seed: int = 7):
    """Project [N,384] to [N,2] via PCA (SVD of centred matrix)."""
    X = vecs - vecs.mean(axis=0, keepdims=True)
    U, S, Vt = np.linalg.svd(X, full_matrices=False)
    return U[:, :2] * S[:2]  # scores: U * singular values


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    out = {"model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
           "projection": "pca (numpy SVD)", "layers": {}}
    for name, meta_name, vec_name, mapper in LAYERS:
        with open(os.path.join(FIG, meta_name), encoding="utf-8") as source:
            meta = json.load(source)
        vecs = np.load(os.path.join(FIG, vec_name))
        items = meta["items"]
        pts = pca2d(vecs.astype(np.float64))
        # normalise to [-1,1] per axis so the chart is stable
        for ax in range(2):
            col = pts[:, ax]
            mx = max(abs(col.min()), abs(col.max()), 1e-9)
            pts[:, ax] = col / mx
        points = []
        for i, it in enumerate(items):
            info = mapper(it)
            points.append({"x": round(float(pts[i, 0]), 4),
                           "y": round(float(pts[i, 1]), 4),
                           "name": info["name"], "bloc": info["bloc"],
                           "party": info["party"], "role": info["role"]})
        out["layers"][name] = {"count": len(points), "points": points}
        print(f"{name}: {len(points)} points projected")

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("window.VEC_MAP = " + json.dumps(out, ensure_ascii=False) + ";\n")
    print(f"\nSaved: {OUT}")


if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
    else:
        main()
