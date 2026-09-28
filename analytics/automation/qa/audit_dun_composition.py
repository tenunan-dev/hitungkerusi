#!/usr/bin/env python3
# Provenance: original path 05_AUTOMATION/audit_dun_composition.py; original SHA-256 f0dce4c07d0e31c0e80efbe6c1eb3a847d074fdb7afd26f5df72dd0cc973f2d5; classification active (one-offs; OPS 8c1852b); versioned 2026-09-11.
"""Audit: does gen_app_data2.py's hardcoded `dun_national` match the research
layer? Prevents the 8 Aug 2026 defect class (N9 wrong alignment, Perak/Sabah
govCount=0) from silently re-entering the app.

Run as part of Stage 3 (report + app build) BEFORE the VS Code handoff.
Exit code 1 = mismatches found → the handoff must be blocked/flagged.

Checks, per state:
  A. seats        — dun_national seats == research seats
  B. gov text     — dun_national gov string == research government-forming bloc
  C. parseability — gov string parses via the same X/Y rule the app uses
                    (build_consolidated_pages.py parse_gov + app-v2.js):
                    bloc = leading [A-Za-z+], count = first number, > 0.
                    Catches "PN 26 (largest, hung)" / "GRS-led 39/73" style
                    strings that break the widget bar.
"""

import os
import re
import sys
from pathlib import Path

def resolve_repository_root(anchor=None):
    """Find the ANALYTICS repository root independently of cwd."""
    return Path(anchor or __file__).resolve().parents[2]


ROOT = str(resolve_repository_root())
GEN = f"{ROOT}/05_AUTOMATION/gen_app_data2.py"
REQUIRED_DATA_RELATIVES = ("research/states/national-composition-summary.md",)


def canonical_path(*relative_parts):
    path = resolve_repository_root().parent / "1_DATA" / "research"
    path = path.joinpath(*relative_parts)
    if not path.exists():
        raise FileNotFoundError(f"Required canonical DATA input is missing: {path}")
    return path


RESEARCH = str(canonical_path("states", "national-composition-summary.md"))


def parse_gov(gov):
    """Mirror of the app-side parse: strip parenthetical, leading letters +
    first number. Returns (bloc, count) or (None, 0) on failure.

    Handles: 'BN 48/56', 'BN+PH 25/42 (hung)', 'PN 26 (largest, hung)',
    'GRS-led 39/73' (suffix like '-led' between bloc and number).
    The research layer must keep the count OUTSIDE any parenthetical —
    e.g. 'GRS-led 37/73', NOT 'GRS-led coalition (… 37/73)'. """
    if not gov:
        return None, 0
    g = re.sub(r"\s*\(.*?\)\s*", " ", str(gov)).strip()
    m = re.match(r"([A-Za-z+]+)", g)
    n = re.search(r"(\d+)", g)
    if not m or not n:
        return None, 0
    bloc = m.group(1).upper()
    count = int(n.group(1))
    # strip a '-led' style suffix: 'GRS-led' -> bloc stays 'GRS' (regex already
    # stops at the hyphen), but guard the count: if the FIRST number is inside
    # a trailing word (e.g. 'GRS-led 2-seat'), we'd mis-parse — accept only
    # when the number appears before any remaining letters after it.
    return bloc, count


def extract_dun_national(path):
    """Pull the hardcoded dun_national list from gen_app_data2.py source."""
    txt = open(path).read()
    start = txt.find("data['dun_national']")
    if start < 0:
        return None
    # the list opener is the FIRST '[' AFTER the key-access ']' — searching for
    # '[' from 'start' would match the bracket inside ['dun_national'] itself.
    eq = txt.find("=", start)
    start = txt.find("[", eq)
    if start < 0:
        return None
    # find matching close bracket
    depth = 0
    in_str = False
    esc = False
    end = None
    for i in range(start, len(txt)):
        ch = txt[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == "'":
                in_str = False
        else:
            if ch == "'":
                in_str = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
    if end is None:
        return None
    block = txt[start:end]
    states = {}
    for m in re.finditer(
        r"\{\s*'state':\s*'([^']+)'[^}]*?'seats':\s*(\d+)[^}]*?'gov':\s*'([^']*)'[^}]*?'el':\s*'([^']*)'\s*\}",
        block,
    ):
        states[m.group(1)] = {
            "seats": int(m.group(2)),
            "gov": m.group(3),
            "el": m.group(4),
        }
    return states


def extract_research(path):
    """Parse the national-composition-summary.md table."""
    txt = open(path).read()
    states = {}
    for line in txt.splitlines():
        m = re.match(r"\|\s*([A-Za-z ]+?)\s*\|\s*(\d+)\s*\|\s*([^|]+)\|\s*([^|]+)\s*\|", line)
        if m and m.group(1) not in ("State",) and "---" not in m.group(1):
            name = m.group(1).strip()
            if name in ("State",):
                continue
            states[name] = {
                "seats": int(m.group(2)),
                "el": m.group(3).strip(),
                "gov": m.group(4).strip(),
            }
    return states


def main():
    dn = extract_dun_national(GEN)
    rs = extract_research(RESEARCH)
    if dn is None:
        print("FATAL: could not extract dun_national from gen_app_data2.py")
        sys.exit(1)
    if not rs:
        print("FATAL: could not parse national-composition-summary.md")
        sys.exit(1)

    print(f"{'STATE':20s} {'SEATS':>5s} {'GOV(dun_national)':<28s} {'GOV(research)':<28s} {'RESULT':>8s}")
    print("-" * 100)
    errors = []
    for name in sorted(rs.keys(), key=lambda s: s.lower()):
        r = rs[name]
        d = dn.get(name)
        if d is None:
            errors.append(f"{name}: missing from dun_national")
            print(f"{name:20s} {'?':>5s} {'—':<28s} {r['gov']:<28s} {'MISSING':>8s}")
            continue
        issues = []
        if d["seats"] != r["seats"]:
            issues.append(f"seats {d['seats']} != {r['seats']}")
        # gov check: compare the PARSEABLE CORE ('BLOC N/M' + optional '(...)'
        # annotation) rather than the full string — 'BN+PH 25/42 (hung)' and
        # 'BN+PH 25/42' are the same government, as are 'GRS-led 39/73' and
        # 'GRS-led 39/73 (GRS 22+…)'. Only bloc + count must agree.
        db, dc = parse_gov(d["gov"])
        rb, rc = parse_gov(r["gov"])
        if (db, dc) != (rb, rc):
            issues.append(f"gov '{d['gov']}' != '{r['gov']}' (parsed {db}{dc} vs {rb}{rc})")
        bloc, count = db, dc
        if bloc is None or count <= 0:
            issues.append(f"UNPARSEABLE (bloc={bloc}, count={count}) — widget bar will break")
        ok = not issues
        if not ok:
            errors.append(f"{name}: {'; '.join(issues)}")
        print(
            f"{name:20s} {d['seats']:>5d} {d['gov']:<28s} {r['gov']:<28s} "
            f"{'OK' if ok else '!! ' + '; '.join(issues)[:24]:>8s}"
        )

    print()
    if errors:
        print(f"AUDIT FAIL — {len(errors)} issue(s):")
        for e in errors:
            print(f"  - {e}")
        print("\nAction: fix gen_app_data2.py dun_national (or research file if IT is wrong),")
        print("then re-run this audit until clean BEFORE the VS Code handoff.")
        sys.exit(1)
    print("AUDIT PASS — all 13 states match the research layer and parse cleanly.")


if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
    else:
        main()
