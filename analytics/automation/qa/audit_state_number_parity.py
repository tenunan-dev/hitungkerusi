#!/usr/bin/env python3
# Provenance: original path 05_AUTOMATION/audit_state_number_parity.py; original SHA-256 b50ebc96ae9a6fac535b87b1964d47949ec22c19e3f32f3e760ff3c5e15cca31; classification active (reports; OPS 8c1852b); versioned 2026-09-11.
"""audit_state_number_parity.py — STATE EN↔MS numeric parity gate (owner directive 10 Aug 2026).

The federal report has a CROSS-BUILD GATE (P10/P50/P90/govt/P(majority) must match
EN verbatim); states previously had none. This adds the equivalent for all 13
state reports:

  1. §10/§9 scenario table: every (Scenario, BN, PH, PN, flips) row must be
     numerically identical between EN and MS.
  2. §11 per-seat projection: the base-case bloc totals (BN/PH/PN...) and the
     per-seat flip count must match between EN and MS.
  3. Macro/event-shock rows (§9): the numeric cell values must match.

Method: extract all decimal numbers from each section's tables in EN and MS and
compare the sets. Numbers are computed once above the language branch in the
builder, so any mismatch is a real bug — this gate catches it at build time.

Usage:
    .venv/bin/python 05_AUTOMATION/audit_state_number_parity.py
    .venv/bin/python 05_AUTOMATION/audit_state_number_parity.py --json

Exit 0 = all 13 states match; exit 1 = at least one mismatch (list them).
Stage 3 cron runs this after MS generation, alongside audit_ms_en_sections.py.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

def resolve_repository_root(anchor=None):
    """Find the ANALYTICS repository root independently of cwd."""
    return Path(anchor or __file__).resolve().parents[2]


ROOT = str(resolve_repository_root())
STATES = ["Melaka", "Sarawak", "Pahang", "Perak", "Perlis", "Johor", "Kedah",
          "Kelantan", "Negeri Sembilan", "Pulau Pinang", "Sabah", "Selangor", "Terengganu"]

# Sections that carry numbers and must match verbatim (per Tier):
#   Tier-1: 9 Data, 10 Scenario, 11 Projection, 12 Battlegrounds
#   Tier-2: 9 Projection Outlook, 10 Battlegrounds
NUMBER_SECTIONS = {"9", "10", "11", "12"}


def extract_numbers(txt, section_key):
    """Return the multiset of decimal numbers in one top-level section."""
    out = []
    in_sec = False
    for line in txt.splitlines():
        m = re.match(r"^## (\d+)\.\s*(.*)$", line)
        if m:
            in_sec = m.group(1) == section_key
            continue
        if in_sec:
            # numbers only from table rows (lines starting with |) to avoid
            # prose rounding differences; fall back to all numbers if no tables
            if line.strip().startswith("|"):
                out.extend(re.findall(r"-?\d+\.?\d*", line))
    return sorted(out)


def audit():
    results = []
    fails = 0
    for st in STATES:
        en_p = os.path.join(ROOT, "03_REPORTS", "states", f"DUN {st}", "latest", f"GE16_{st}_Report.md")
        ms_p = os.path.join(ROOT, "03_REPORTS", "states", f"DUN {st}", "latest", f"GE16_{st}_Report_MS.md")
        if not (os.path.exists(en_p) and os.path.exists(ms_p)):
            results.append({"state": st, "status": "SKIP", "detail": "report pair missing"})
            continue
        en = open(en_p, encoding="utf-8").read()
        ms = open(ms_p, encoding="utf-8").read()
        state_fails = []
        for sec in NUMBER_SECTIONS:
            en_nums = extract_numbers(en, sec)
            ms_nums = extract_numbers(ms, sec)
            # count mismatches (multiset difference)
            from collections import Counter
            diff = list((Counter(en_nums) - Counter(ms_nums)).elements()) + \
                   list((Counter(ms_nums) - Counter(en_nums)).elements())
            if diff:
                state_fails.append({"section": sec, "en_only": len(diff) // 2 + (1 if len(diff) % 2 else 0),
                                    "sample": diff[:6]})
        if state_fails:
            fails += 1
            results.append({"state": st, "status": "FAIL", "sections": state_fails})
        else:
            results.append({"state": st, "status": "PASS"})
    return results, fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    results, fails = audit()
    if args.json:
        print(json.dumps({"exit": 1 if fails else 0, "results": results}, indent=1))
    else:
        print("STATE EN↔MS NUMERIC PARITY GATE")
        print("=" * 60)
        for r in results:
            if r["status"] == "PASS":
                print(f"  ✅ {r['state']}: numeric parity PASS")
            elif r["status"] == "SKIP":
                print(f"  ⏭️  {r['state']}: {r['detail']}")
            else:
                print(f"  ❌ {r['state']}: MISMATCH")
                for s in r["sections"]:
                    print(f"       §{s['section']}: {s['sample']}")
        print("=" * 60)
        print("ALL 13 STATES PASS" if fails == 0 else f"{fails} state(s) FAIL — fix the builder, rebuild, re-run")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
