#!/usr/bin/env python3
# Provenance: original path 05_AUTOMATION/update_parties_from_cron.py; original SHA-256 cd1385b47cfb688f5883a229add2f1f97ccc4dd0ac23fbeb49e44929e20e4c86; classification active (figure-vector; OPS 8c1852b); versioned 2026-09-11.
"""Fold cron-job party findings into the GE16 political parties database.

Cron agents (Stage 1b/1c) write party-updates-<date>.json when news reports a
party-level fact: new party formed, coalition split/merger, leadership change,
party renamed, seat-allocation change.

  work/figures/_draft/party-updates-2026-08-05.json
  {
    "layer": "party_updates",
    "date": "2026-08-05",
    "source": "Stage 1c news tracker (LLM-judged)",
    "parties": [
      {"name": "WAWASAN",
       "history": ["2026-08-05: ..."],
       "ge16_posture": "...", "ideology": "...", "bloc": "PN",
       "note": "New development reported ..."}
    ]
  }

The update file IS a layer: build_parties_vdb.py merges every party-updates-*.json
into the seed before computing cross-links. This script validates the files and
rebuilds the DB.

Run: .venv/bin/python -m tools.figures.update_parties_from_cron
"""
import os
import sys
import glob
import json
import subprocess
from pathlib import Path

def resolve_repository_root(anchor=None):
    """Find the ANALYTICS repository root independently of cwd."""
    return Path(anchor or __file__).resolve().parents[2]


ROOT = str(resolve_repository_root())
# Task 4.6 contract: figure VDB artifacts and the _draft ingestion layers share
# the ANALYTICS working root (the same root the builders and readers resolve).
FIGURES_WORK_ROOT = resolve_repository_root() / "work" / "figures"
DRAFT = str(FIGURES_WORK_ROOT / "_draft")
BUILD_MODULE = "tools.figures.build_parties_vdb"


def main():
    files = sorted(glob.glob(os.path.join(DRAFT, "party-updates-*.json")))
    if not files:
        print("No party update files found in _draft/.")
        return

    bad = []
    n_parties = 0
    for path in files:
        try:
            data = json.load(open(path, encoding="utf-8"))
            n_parties += len(data.get("parties", []))
            print(f"{os.path.basename(path)}: OK ({len(data.get('parties', []))} party records)")
        except Exception as e:
            bad.append((os.path.basename(path), str(e)))

    if bad:
        print("\nINVALID FILES (fix before rebuild):")
        for fn, err in bad:
            print(f"  - {fn}: {err}")
        return

    print(f"\nUpdate files: {len(files)} | party records: {n_parties}")
    print("Rebuilding parties DB from seed + updates ...")
    result = subprocess.run([sys.executable, "-m", BUILD_MODULE], cwd=ROOT, check=False)
    if result.returncode == 0:
        print("Done. Updates are searchable via search_parties.py")
    else:
        print(f"REBUILD FAILED (rc={result.returncode})")


if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
    else:
        main()
