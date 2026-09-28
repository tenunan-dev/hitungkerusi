#!/usr/bin/env python3
# Provenance: original path 05_AUTOMATION/archive_superseded_reports.py; original SHA-256 2bc6d76c83960112d0ce9681b205650e99b826ed1d9e455e24be9bf1f5b33636; classification active (reports; OPS 8c1852b); versioned 2026-09-11.
"""GE16 report file hygiene — archive superseded formats (5 Aug 2026, user directive).

Every Stage 3 cron run must:
1. Move SUPERSEDED report formats out of latest/ into the dated archive:
     *British_Concise*.md / *_MS.md     (4 Aug concise-format experiment — replaced)
     *Compact*.md / *_MS.md             (older compact format — replaced)
2. KEEP ONLY what the VS Code build scripts consume in latest/:
     GE16_<State>_Report.md / _MS.md           → md_to_html_report.py + gen_app_data2.py
     GE16_Malaysia_General_Election_Report*.md → gen_report_js_bi.py fallback + HTML
     dun-election-summary*.md                  → gen_app_data2.py state_deepdives
     <state>-prn-projection-report.md          → PRN view
     ge16-battleground-deepdive.md             → analysis reference
     <state>-prn-projection.csv                → PRN chips (data, not report)

This keeps the archive as the history trail and latest/ as a deterministic
input set for the app build — no stale-format confusion (e.g. report.js
preferring the superseded concise federal report).

Usage: .venv/bin/python 05_AUTOMATION/archive_superseded_reports.py [--date YYYY-MM-DD]
       (default date = today; dry-run with --dry)
"""
import os
import re
import sys
import shutil
import datetime
from pathlib import Path

def resolve_repository_root(anchor=None):
    """Find the ANALYTICS repository root independently of cwd."""
    return Path(anchor or __file__).resolve().parents[2]


ROOT = str(resolve_repository_root())
REPORTS = os.path.join(ROOT, "03_REPORTS")
SUPERSEDED_PATTERNS = re.compile(r"British_Concise|Compact", re.I)

# files that MUST stay in latest/ (VS Code build inputs)
KEEP_SUFFIXES = (
    "_Report.md", "_Report_MS.md",
    "dun-election-summary.md", "dun-election-summary_MS.md",
    "-prn-projection-report.md", "-prn-projection.csv",
    "ge16-battleground-deepdive.md",
)


def is_superseded(fname):
    return bool(SUPERSEDED_PATTERNS.search(fname))


def is_keep(fname):
    return fname.endswith(KEEP_SUFFIXES)


def main():
    dry = "--dry" in sys.argv
    date = None
    if "--date" in sys.argv:
        date = sys.argv[sys.argv.index("--date") + 1]
    date = date or datetime.date.today().isoformat()

    moved, kept, skipped = [], [], []
    # federal
    fed_latest = os.path.join(REPORTS, "federal", "latest")
    if os.path.isdir(fed_latest):
        for f in sorted(os.listdir(fed_latest)):
            if not f.endswith(".md"):
                continue
            if is_superseded(f):
                moved.append((fed_latest, f))
            elif is_keep(f):
                kept.append((fed_latest, f))
            else:
                skipped.append((fed_latest, f))
    # states
    states_dir = os.path.join(REPORTS, "states")
    for sd in sorted(os.listdir(states_dir)):
        latest = os.path.join(states_dir, sd, "latest")
        if not os.path.isdir(latest):
            continue
        for f in sorted(os.listdir(latest)):
            if not f.endswith(".md"):
                continue
            if is_superseded(f):
                moved.append((latest, f))
            elif is_keep(f):
                kept.append((latest, f))
            else:
                skipped.append((latest, f))

    # archive destination (per-scope, since 9 Aug 2026): federal superseded ->
    # 03_REPORTS/federal/archive/GE16-<date>/superseded/ ; state superseded ->
    # 03_REPORTS/states/DUN <State>/archive/GE16-<date>/superseded/
    # (legacy flat 03_REPORTS/archive/GE16-<date>/superseded/ was retired 9 Aug 2026)
    def _scope_archive(src_dir):
        rel = os.path.relpath(src_dir, REPORTS)
        parts = rel.split(os.sep)
        if parts[0] == "federal":
            return os.path.join(REPORTS, "federal", "archive", f"GE16-{date}", "superseded")
        # parts = ["states", "DUN <State>", "latest"]
        return os.path.join(REPORTS, "states", parts[1], "archive", f"GE16-{date}", "superseded")

    print(f"{'DRY-RUN: ' if dry else ''}archive superseded reports (per-scope, GE16-{date})")
    print(f"  superseded to move: {len(moved)}")
    for src_dir, f in moved:
        arch = _scope_archive(src_dir)
        os.makedirs(arch, exist_ok=True)
        dst = os.path.join(arch, f)
        print(f"    {os.path.relpath(os.path.join(src_dir, f), ROOT)} -> {os.path.relpath(dst, ROOT)}")
        if not dry:
            shutil.move(os.path.join(src_dir, f), dst)

    print(f"  kept in latest/: {len(kept)}")
    for src_dir, f in kept:
        print(f"    {os.path.relpath(os.path.join(src_dir, f), ROOT)}")

    if skipped:
        print(f"  OTHER (not .md-report, left in place): {len(skipped)}")
        for src_dir, f in skipped:
            print(f"    {os.path.relpath(os.path.join(src_dir, f), ROOT)}")

    print(f"\nDONE: {len(moved)} superseded -> archive, {len(kept)} build inputs "
          f"remain in latest/")


if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
    else:
        main()
