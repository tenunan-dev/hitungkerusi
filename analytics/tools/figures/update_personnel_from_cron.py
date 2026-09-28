#!/usr/bin/env python3
# Provenance: original path 05_AUTOMATION/update_personnel_from_cron.py; original SHA-256 fb7a070ba08d460a5f2506820a9c5f73bf936d51f13d1283882fd09f4b0e61ac; classification active (figure-vector; OPS 8c1852b); versioned 2026-09-11.
"""Fold cron-job personnel findings into the GE16 personnel database.

Cron agents (Stage 1b candidate tracker, Stage 1c news tracker) write dated
update files when they spot personnel-relevant facts in the news, e.g.:

  work/figures/_draft/personnel-updates-2026-08-05.json
  {
    "layer": "cron_updates",
    "date": "2026-08-05",
    "source": "Stage 1c news tracker (LLM-judged)",
    "persons": [
      {
        "name": "Hamzah Zainudin",
        "roles": [
          {"type": "party_switch", "from": "BERSATU", "to": "WAWASAN",
           "date": "2026-06-13", "note": "Founded WAWASAN after Bersatu purge"}
        ]
      }
    ]
  }

HOW IT WORKS: the update file IS a layer. build_personnel_vdb.py merges every
_personnel-updates-*.json in _draft/ into the DB (name reconciliation + role
dedup by signature, idempotent), so the DB is rebuilt from all layers and the
update files persist in _draft as the historical trail. This script:

1. Validates pending update files (schema check)
2. Lists any named persons NOT resolvable in the DB (research follow-up)
3. Runs build_personnel_vdb.py to fold updates in and rebuild vectors

Run: .venv/bin/python -m tools.figures.update_personnel_from_cron
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
PERSONNEL = str(FIGURES_WORK_ROOT / "ge16-personnel.json")
BUILD_MODULE = "tools.figures.build_personnel_vdb"

try:  # runnable as a script (python tools/figures/update_personnel_from_cron.py) …
    from .build_personnel_vdb import normalize  # reuse the exact reconciliation rules
except ImportError:  # … or as a module (-m tools.figures.update_personnel_from_cron)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from build_personnel_vdb import normalize


def main():
    files = sorted(glob.glob(os.path.join(DRAFT, "personnel-updates-*.json")))
    if not files:
        print("No personnel update files found in _draft/.")
        return

    # known persons in the current DB
    personnel = json.load(open(PERSONNEL, encoding="utf-8"))
    known = {normalize(p["name"]) for p in personnel.get("persons", [])}

    n_roles = 0
    unknown = set()
    bad = []
    for path in files:
        try:
            data = json.load(open(path, encoding="utf-8"))
        except Exception as e:
            bad.append((os.path.basename(path), str(e)))
            continue
        for up in data.get("persons", []):
            name = (up.get("name") or "").strip()
            if not name:
                continue
            n_roles += len(up.get("roles", []) or [])
            if normalize(name) not in known:
                unknown.add(name)
        print(f"{os.path.basename(path)}: OK")

    print(f"\nUpdate files: {len(files)} | total update roles: {n_roles}")
    if unknown:
        print("\nPERSONS NOT IN DB (research follow-up needed):")
        for n in sorted(unknown):
            print("  -", n)
    if bad:
        print("\nINVALID FILES (fix before rebuild):")
        for fn, err in bad:
            print(f"  - {fn}: {err}")
        return

    print("\nRebuilding personnel DB from all layers (incl. updates) ...")
    result = subprocess.run([sys.executable, "-m", BUILD_MODULE], cwd=ROOT, check=False)
    if result.returncode == 0:
        print("Done. Updates are now searchable via search_personnel.py")
    else:
        print(f"REBUILD FAILED (rc={result.returncode})")


if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
    else:
        main()
