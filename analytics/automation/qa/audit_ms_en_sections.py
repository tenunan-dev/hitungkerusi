#!/usr/bin/env python3
# Provenance: original path 05_AUTOMATION/audit_ms_en_sections.py; original SHA-256 9ecbd538377c4812e26afbcef036f6ac918ca58f29b3149c9bb63795ea955b2a; classification active (reports; OPS 8c1852b); versioned 2026-09-11.
"""audit_ms_en_sections.py — PER-SECTION MS/EN completeness gate (owner directive 9 Aug 2026).

Every section of every MS report must be ≥ MIN_RATIO (85%) of its EN counterpart's
word count. Malay is naturally ~90-97% of English length; anything under 85% flags a
section that was truncated, condensed, or dropped during generation.

Usage:
    .venv/bin/python 05_AUTOMATION/audit_ms_en_sections.py            # all 14 reports
    .venv/bin/python 05_AUTOMATION/audit_ms_en_sections.py --min 85   # custom threshold
    .venv/bin/python 05_AUTOMATION/audit_ms_en_sections.py --json     # machine-readable

Exit code: 0 = all sections ≥ threshold; 1 = at least one section below.
Stage 3 cron Step 4 (f9bfa07de673) runs this after MS generation and must exit 0.
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
MIN_RATIO = 85.0

REPORTS = [
    ("FEDERAL",
     os.path.join(ROOT, "03_REPORTS", "federal", "latest", "GE16_Malaysia_General_Election_Report.md"),
     os.path.join(ROOT, "03_REPORTS", "federal", "latest", "GE16_Malaysia_General_Election_Report_MS.md")),
]
for st in ["Melaka", "Sarawak", "Pahang", "Perak", "Perlis", "Johor", "Kedah", "Kelantan",
           "Negeri Sembilan", "Pulau Pinang", "Sabah", "Selangor", "Terengganu"]:
    REPORTS.append((
        st,
        os.path.join(ROOT, "03_REPORTS", "states", f"DUN {st}", "latest", f"GE16_{st}_Report.md"),
        os.path.join(ROOT, "03_REPORTS", "states", f"DUN {st}", "latest", f"GE16_{st}_Report_MS.md"),
    ))


def split_sections(txt):
    """Split by top-level '## N.' headers (not ### subsections).
    Returns list of (section_key, header, word_count)."""
    lines = txt.splitlines()
    sections = []
    cur_key, cur_hdr, cur_words = None, None, 0
    for line in lines:
        m = re.match(r"^## (\d+)\.\s*(.*)$", line)
        if m:
            if cur_key is not None:
                sections.append((cur_key, cur_hdr, cur_words))
            cur_key = int(m.group(1))
            cur_hdr = line.strip()
            cur_words = 0
        elif cur_key is not None:
            cur_words += len(line.split())
    if cur_key is not None:
        sections.append((cur_key, cur_hdr, cur_words))
    return sections


def audit(min_ratio=MIN_RATIO, as_json=False):
    results = []
    all_ok = True
    for label, en_p, ms_p in REPORTS:
        if not (os.path.exists(en_p) and os.path.exists(ms_p)):
            results.append({"report": label, "error": "missing file"})
            all_ok = False
            continue
        en_secs = split_sections(open(en_p, encoding="utf-8").read())
        ms_secs = split_sections(open(ms_p, encoding="utf-8").read())
        en_d = {k: (h, w) for k, h, w in en_secs}
        ms_d = {k: (h, w) for k, h, w in ms_secs}
        report_ok = True
        for k in sorted(en_d):
            hdr, ew = en_d[k]
            mw = ms_d.get(k, (None, 0))[1]
            ratio = mw / ew * 100 if ew else 100.0
            status = "PASS" if ratio >= min_ratio else "FAIL"
            if status == "FAIL":
                report_ok = False
                all_ok = False
            results.append({
                "report": label, "section": k, "header": hdr,
                "en_words": ew, "ms_words": mw, "ratio_pct": round(ratio, 1),
                "status": status,
            })
        if not report_ok:
            results.append({"report": label, "error": f"report has sections below {min_ratio}%"})

    if as_json:
        print(json.dumps(results, indent=2))
    else:
        print(f"PER-SECTION MS/EN COMPLETENESS GATE (min {min_ratio:.0f}%)")
        print("=" * 100)
        cur_report = None
        for r in results:
            if "error" in r and "section" not in r:
                print(f"\n{r['report']}: ❌ {r['error']}")
                continue
            if r["report"] != cur_report:
                cur_report = r["report"]
                print(f"\n--- {cur_report} ---")
            mark = "✅" if r["status"] == "PASS" else "❌"
            print(f"  {mark} §{r['section']:<3} {r['header'][:52]:<52} "
                  f"EN {r['en_words']:<6} MS {r['ms_words']:<6} {r['ratio_pct']:>5.1f}%")
        print("\n" + ("ALL SECTIONS PASS" if all_ok else "❌ FAIL: at least one section below threshold"))
    return all_ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=float, default=MIN_RATIO, help=f"min ratio %% (default {MIN_RATIO})")
    ap.add_argument("--json", action="store_true", help="machine-readable JSON output")
    args = ap.parse_args()
    ok = audit(min_ratio=args.min, as_json=args.json)
    sys.exit(0 if ok else 1)
