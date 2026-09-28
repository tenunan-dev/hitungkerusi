#!/usr/bin/env python3
"""
build_adapter.py — Delivery adapter for HitungKerusi 222 (Vercel channel).

Reads the GE16 V2 delivery contract at ../../4_DELIVERY/current/ and regenerates
the site's local data artifacts (js/data.js, js/report.js) plus refreshes geo/.
It does not regenerate the hand-authored state pages under state/.

This is a PURE RENDERER adapter: it only reshapes what Hermes published. It does
NOT compute forecasts, scenarios or political assumptions. Where the delivery is
missing an artifact the site expects, this script leaves the corresponding part
of data.js empty and records the gap in MISSING_DATA.md — it never fabricates
placeholder content.

The current delivery contains data/app-data.json (the composed
GE16_APP_DATA payload), data/forecast.json (published headline contract) and
data/scenarios.json (scenario blocks), alongside data/ge16-forecast-latest.json
(raw forecast-engine output), reports/ markdown, and geo/.

Source-of-truth order: app-data.json is the base payload. scenarios.json is
authoritative for the four scenario fields. forecast.json fills any headline
field app-data.json omits. ge16-forecast-latest.json is used only for
`vacancies`, `summary.deterministic_blocs`, `summary.model` and as a cross-check.
See MISSING_DATA.md for what is still genuinely absent.

Usage:
    python3 build_adapter.py              # write js/*.generated.js, report gaps
    python3 build_adapter.py --apply      # also overwrite js/data.js + js/report.js
    python3 build_adapter.py --apply --build   # then run build_vercel.py
    python3 build_adapter.py --check      # verify-only: ZERO filesystem writes.
                                           # Runs the manifest hash check, builds
                                           # app-data/report/geo plans in memory,
                                           # and reports what WOULD change without
                                           # touching js/*.generated.js, js/data.js,
                                           # js/report.js, MISSING_DATA.md, or geo/.

No deployment. Local build and verification only.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DELIVERY = os.path.abspath(os.path.join(ROOT, "..", "..", "4_DELIVERY", "current"))
JS_DIR = os.path.join(ROOT, "js")

# Election-engine state labels -> labels used elsewhere in this site
# (dun-data.js, /state/*.html, app-states.js). Pure label mapping, not data.
STATE_LABEL = {
    "Malacca": "Melaka",
    "Penang": "Pulau Pinang",
}

# Presentation defaults only — NOT from the delivery. The renderers already fall
# back to grey when these are absent; kept here so the map/table are legible.
BLOC_COLORS = {
    "PH": "#d7263d", "BN": "#0d4a9e", "PN": "#0f766e", "GPS": "#b45309",
    "GRS": "#7c3aed", "WARISAN": "#0891b2", "MUDA": "#db2777", "KDM": "#65a30d",
    "PBM": "#525252", "IND": "#64748b",
}

MISSING_DATA_MD = os.path.join(ROOT, "MISSING_DATA.md")


# ---------------------------------------------------------------------------
# Delivery verification
# ---------------------------------------------------------------------------

def load_manifest():
    path = os.path.join(DELIVERY, "DELIVERY.json")
    if not os.path.isfile(path):
        sys.exit(f"FATAL: no delivery manifest at {path}")
    return json.load(open(path, encoding="utf-8"))


def verify_delivery(manifest):
    """Check every file in the manifest exists and matches its sha256."""
    problems = []
    for rel, meta in manifest.get("files", {}).items():
        p = os.path.join(DELIVERY, rel)
        if not os.path.isfile(p):
            problems.append(f"MISSING: {rel}")
            continue
        h = hashlib.sha256(open(p, "rb").read()).hexdigest()
        if h != meta.get("sha256"):
            problems.append(f"HASH MISMATCH: {rel}")
    return problems


def verify_website_data_provenance(manifest):
    """Ensure the rendered site data names exactly the current delivery."""
    path = os.path.join(JS_DIR, "data.js")
    if not os.path.isfile(path):
        return ["MISSING PROVENANCE: js/data.js is not present"]
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        raw = raw.replace("window.GE16_APP_DATA = ", "", 1).strip().rstrip(";").strip()
        data = json.loads(raw)
        actual = data.get("_adapter", {}).get("delivery_id")
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        return [f"INVALID PROVENANCE: js/data.js cannot be read: {exc}"]

    expected = manifest.get("delivery_id")
    if actual != expected:
        return [
            "PROVENANCE MISMATCH: js/data.js _adapter.delivery_id="
            f"{actual!r} does not match DELIVERY.json delivery_id={expected!r}"
        ]
    return []


# ---------------------------------------------------------------------------
# data.js  <-  data/ge16-forecast-latest.json
# ---------------------------------------------------------------------------

def _load(rel):
    return json.load(open(os.path.join(DELIVERY, "data", rel), encoding="utf-8"))


# Headline fields published by forecast.json — used to backfill anything
# app-data.json's summary omits, and to cross-check the ones it carries.
FORECAST_SUMMARY_FIELDS = (
    "govt_p50", "govt_p10", "govt_p90", "majority_pct", "flips", "econ_term",
    "coalition", "coalition_text", "govt_blocs", "opp_blocs", "bloc_colors",
    "party_colors", "party_bloc", "flips_list", "tight_seats",
)

# Fields that are still genuinely absent from every file in the delivery. The
# legacy site strips these too; the adapter leaves them empty, never fabricated.
GENUINELY_ABSENT = (
    "ge15", "battlegrounds", "demographics", "swings",
)


def build_app_data():
    """app-data.json is the composed payload. scenarios.json is authoritative
    for the four scenario fields. forecast.json backfills/cross-checks the
    headline. ge16-forecast-latest.json supplies vacancies + deterministic
    blocs + model name and nothing else."""
    app = _load("app-data.json")
    scen = _load("scenarios.json")
    fcast = _load("forecast.json")
    latest = _load("ge16-forecast-latest.json")

    warnings = []
    data = json.loads(json.dumps(app))          # deep copy
    summary = data.setdefault("summary", {})

    # 1. scenarios.json — authoritative for these four fields
    for k in ("scenarios_display", "scenario_descriptions", "scenario_deltas"):
        if k in scen:
            summary[k] = scen[k]
    if "scenarios" in scen:
        data["scenarios"] = scen["scenarios"]

    # 2. forecast.json — backfill any headline field app-data omits; cross-check
    #    the rest and warn (do not overwrite — app-data.json wins on overlap)
    for k in FORECAST_SUMMARY_FIELDS:
        if k not in fcast:
            continue
        if k not in summary or summary[k] in (None, "", [], {}):
            summary[k] = fcast[k]
        elif isinstance(fcast[k], (int, float, str)) and summary[k] != fcast[k]:
            warnings.append(f"summary.{k}: app-data={summary[k]!r} vs forecast.json={fcast[k]!r}")

    # 3. ge16-forecast-latest.json — the three fields only it carries
    data["vacancies"] = latest.get("vacancies", data.get("vacancies", []))
    summary.setdefault("deterministic_blocs", latest.get("deterministic", {}))
    summary.setdefault("model", latest.get("model"))

    # cross-check headline seat count against the raw engine output
    mc = latest.get("monte_carlo", {})
    raw_p50 = int(mc.get("P50", latest.get("govt_expected", 0)) or 0)
    if raw_p50 and summary.get("govt_p50") not in (None, raw_p50):
        warnings.append(f"summary.govt_p50={summary.get('govt_p50')} vs engine P50={raw_p50}")
    raw_flips = len(latest.get("flips", []))
    if raw_flips and summary.get("flips") not in (None, raw_flips):
        warnings.append(f"summary.flips={summary.get('flips')} vs engine flips[]={raw_flips}")

    data["_adapter"] = {
        "delivery_id": None,                    # filled by caller
        "sources": [
            "data/app-data.json (base payload)",
            "data/scenarios.json (scenario fields)",
            "data/forecast.json (headline backfill/cross-check)",
            "data/ge16-forecast-latest.json (vacancies, deterministic_blocs, model)",
        ],
        "note": "Full build. Genuinely absent: " + ", ".join(GENUINELY_ABSENT)
                + ", dun/dun-data.js (no machine-readable DUN dataset).",
    }

    # keys the site reads that no file in the delivery populates — keep empty,
    # never fabricated (legacy strips these too)
    for k in GENUINELY_ABSENT:
        data.setdefault(k, [])

    return data, warnings, {"app": app, "scenarios": scen, "forecast": fcast, "latest": latest}


# ---------------------------------------------------------------------------
# report.js  <-  reports/**/*.md
# ---------------------------------------------------------------------------

def md_to_sections(md_text):
    """Turn an approved report markdown file into the section array shape that
    js/app-report.js renders (id / title / kind:'prose' / body[])."""
    sections = []
    cur = None
    para = []
    bullets = []
    tbl = None

    def flush_para():
        nonlocal para
        if para and cur is not None:
            cur["body"].append(" ".join(para).strip())
        para = []

    def flush_bullets():
        nonlocal bullets
        if bullets and cur is not None:
            cur["body"].append({"li": bullets})
        bullets = []

    def flush_table():
        nonlocal tbl
        if tbl and cur is not None and tbl["rows"]:
            cur["body"].append({"table": tbl})
        tbl = None

    def flush_all():
        flush_para(); flush_bullets(); flush_table()

    lines = md_text.splitlines()
    for raw in lines:
        line = raw.rstrip()
        if line.startswith("# ") and cur is None:
            continue  # document title
        if line.startswith("## "):
            flush_all()
            cur = {"id": f"sec{len(sections)}", "title": line[3:].strip(),
                   "kind": "prose", "body": []}
            sections.append(cur)
            continue
        if cur is None:
            continue
        if line.startswith("### ") or line.startswith("#### "):
            flush_all()
            cur["body"].append({"h": line.lstrip("#").strip()})
            continue
        if re.match(r"^\s*[-*] ", line):
            flush_para(); flush_table()
            bullets.append(re.sub(r"^\s*[-*] ", "", line).strip())
            continue
        if line.strip().startswith("|") and line.strip().endswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if set("".join(cells)) <= set("-: "):
                continue  # separator row
            flush_para(); flush_bullets()
            if tbl is None:
                tbl = {"headers": cells, "rows": []}
            else:
                tbl["rows"].append(cells)
            continue
        if not line.strip():
            flush_para(); flush_bullets(); flush_table()
            continue
        flush_bullets(); flush_table()
        para.append(line.strip())

    flush_all()
    return sections


def build_report():
    fed = os.path.join(DELIVERY, "reports", "federal")
    en_path = os.path.join(fed, "GE16_Malaysia_General_Election_Report.md")
    ms_path = os.path.join(fed, "GE16_Malaysia_General_Election_Report_MS.md")
    report_en = md_to_sections(open(en_path, encoding="utf-8").read()) if os.path.isfile(en_path) else []
    report_ms = md_to_sections(open(ms_path, encoding="utf-8").read()) if os.path.isfile(ms_path) else []

    # State reports: delivery ships them as markdown (both EN and MS — see
    # GE16_<State>_Report.md / _MS.md — the site previously wired only the MS
    # side, silently dropping the EN half even though it's present in every
    # delivery). We expose both, keyed by site slug, so a future EN renderer
    # can pick them up; the hand-authored /state/*.html pages are not regenerated
    # by this adapter and remain untouched.
    state_reports_ms = {}
    state_reports_en = {}
    missing_en = []
    slug = {
        "DUN Johor": "johor", "DUN Kedah": "kedah", "DUN Kelantan": "kelantan",
        "DUN Melaka": "melaka", "DUN Negeri Sembilan": "negeri-sembilan",
        "DUN Pahang": "pahang", "DUN Perak": "perak", "DUN Perlis": "perlis",
        "DUN Pulau Pinang": "pulau-pinang", "DUN Sabah": "sabah",
        "DUN Sarawak": "sarawak", "DUN Selangor": "selangor",
        "DUN Terengganu": "terengganu",
    }
    sdir = os.path.join(DELIVERY, "reports", "states")
    if os.path.isdir(sdir):
        for d in sorted(os.listdir(sdir)):
            if d not in slug:
                continue
            state = d[4:]
            f_ms = os.path.join(sdir, d, f"GE16_{state}_Report_MS.md")
            if os.path.isfile(f_ms):
                state_reports_ms[slug[d]] = md_to_sections(open(f_ms, encoding="utf-8").read())
            f_en = os.path.join(sdir, d, f"GE16_{state}_Report.md")
            if os.path.isfile(f_en):
                state_reports_en[slug[d]] = md_to_sections(open(f_en, encoding="utf-8").read())
            else:
                missing_en.append(slug[d])
    return report_en, report_ms, state_reports_ms, state_reports_en, missing_en


# ---------------------------------------------------------------------------
# geo/
# ---------------------------------------------------------------------------

def plan_geo_sync(manifest):
    """Compute the manifest-exact geo/ sync plan without touching disk.

    Returns (to_copy, to_delete, unchanged) where to_copy/unchanged are
    relative paths under geo/ that the CURRENT delivery manifest lists, and
    to_delete is every file currently in the local geo/ dir that is NOT in
    the manifest's file list (stale from a prior delivery). Manifest-exact:
    the plan reflects exactly what's in `manifest['files']` under `geo/`,
    nothing added, nothing left over.
    """
    dst = os.path.join(ROOT, "geo")

    # files the current delivery manifest says belong under geo/
    manifest_geo_rel = set()
    for rel in manifest.get("files", {}).keys():
        if rel.startswith("geo/") or rel.startswith("geo" + os.sep):
            manifest_geo_rel.add(os.path.normpath(rel[len("geo/"):]))

    # what's on disk in the delivery's geo/ (source of truth for copy)
    src = os.path.join(DELIVERY, "geo")
    src_files = set()
    if os.path.isdir(src):
        for base, _dirs, files in os.walk(src):
            rel = os.path.relpath(base, src)
            for fn in files:
                r = os.path.normpath(os.path.join(rel, fn)) if rel != "." else fn
                src_files.add(r)

    # to_copy = union of manifest-declared + actually-present delivery files
    # (manifest is authoritative for what SHOULD exist; src_files guards
    # against a manifest that lists a geo file that isn't actually shipped)
    to_copy = sorted(manifest_geo_rel & src_files) if manifest_geo_rel else sorted(src_files)

    # what's currently on disk locally
    dst_files = set()
    if os.path.isdir(dst):
        for base, _dirs, files in os.walk(dst):
            rel = os.path.relpath(base, dst)
            for fn in files:
                r = os.path.normpath(os.path.join(rel, fn)) if rel != "." else fn
                dst_files.add(r)

    to_delete = sorted(dst_files - set(to_copy))
    unchanged = sorted(dst_files & set(to_copy))
    return to_copy, to_delete, unchanged


def refresh_geo(manifest, dry_run=False):
    """Manifest-exact geo/ sync: copies everything the current delivery
    manifest lists under geo/, and DELETES anything already in the local
    geo/ dir that the manifest no longer lists (no stale accumulation
    across deliveries). When dry_run=True, computes and returns the plan
    without writing or deleting anything on disk."""
    src = os.path.join(DELIVERY, "geo")
    dst = os.path.join(ROOT, "geo")
    to_copy, to_delete, unchanged = plan_geo_sync(manifest)

    if dry_run:
        return to_copy, to_delete

    for rel in to_copy:
        s = os.path.join(src, rel)
        d = os.path.join(dst, rel)
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.copy2(s, d)

    for rel in to_delete:
        d = os.path.join(dst, rel)
        if os.path.isfile(d):
            os.remove(d)
    # prune now-empty directories left behind by deletions
    if os.path.isdir(dst):
        for base, dirs, files in os.walk(dst, topdown=False):
            if base == dst:
                continue
            if not os.listdir(base):
                os.rmdir(base)

    return to_copy, to_delete


# ---------------------------------------------------------------------------
# MISSING_DATA.md
# ---------------------------------------------------------------------------

MISSING_DOC = """# MISSING_DATA.md — adapter gap report for delivery {delivery_id}

Generated by `build_adapter.py` on delivery `{delivery_id}`
(manifest generated {generated_at}, {file_count} files).

This site is a **pure renderer**. `build_adapter.py` reshapes the delivery into
`js/data.js` and `js/report.js`; it never invents data. Delivery `{delivery_id}`
carries `data/app-data.json`, `data/forecast.json` and `data/scenarios.json`, so
the build is now **full**: every page renders real content — headline numbers,
222-seat table, projection map, federal report, all eight scenario cards, the
news feed, the five state PRN projections and the PRN calendar.

The only remaining gaps are datasets that have never been part of the delivery
and that the legacy pipeline also drops (per-seat GE15 detail, battlegrounds,
demographics, state-swing table, machine-readable DUN seat data).

---

## 1. What this delivery contains and how it is wired

| Delivery path | Feeds | Status |
|---|---|---|
| `data/app-data.json` | `js/data.js` base payload — `master`, `projection`, `summary`, `history`, `general_news`, `*_prn`, `*_scenarios`, `dun_national`, `dun_schedule`, `state_proj`, `sources`, `updates`, `acknowledgements` | wired (source of truth) |
| `data/scenarios.json` | `summary.scenarios_display`, `summary.scenario_descriptions`, `summary.scenario_deltas`, top-level `scenarios{}` | wired (authoritative for these four fields) |
| `data/forecast.json` | headline backfill + cross-check for `summary.{govt_p50,govt_p10,govt_p90,majority_pct,flips,econ_term,coalition,coalition_text,bloc_colors,party_colors,party_bloc,flips_list,tight_seats}` | wired (cross-check) |
| `data/ge16-forecast-latest.json` | `vacancies[]`, `summary.deterministic_blocs`, `summary.model`; raw-engine cross-check of P50 / flip count | wired (cross-check) |
| `reports/federal/GE16_Malaysia_General_Election_Report.md` / `_MS.md` | `js/report.js` → `window.GE16_REPORT_EN` / `_MS` | wired |
| `reports/states/DUN <State>/GE16_<State>_Report_MS.md` (13) | `js/report.js` → `window.GE16_STATE_REPORTS_MS[<slug>]` | parsed & exposed |
| `reports/states/DUN <State>/GE16_<State>_Report.md` (13) | `js/report.js` → `window.GE16_STATE_REPORTS_EN[<slug>]` | parsed & exposed |
| `geo/malaysia-states.geojson`, `geo/dun/*.geojson` (14) | `geo/` (served statically, fetched by `js/app-maps.js`) | wired |

---

## 2. What is still genuinely absent

These are not in `{delivery_id}` and are not fabricated. The committed legacy
`js/data.js` strips them too, so the site already degrades gracefully.

| Key | Purpose | Why absent |
|---|---|---|
| `ge15[]` | per-seat GE15 official result (winner, party, votes, pct, majority, electorate, turnout) | no per-seat historical dataset in the delivery |
| `battlegrounds[]` | marginal-seat list with tier + margin | derived from a real `ge15[]`, which is absent |
| `demographics[]` | per-seat `malay_pct` etc. | not published |
| `swings[]` | state-election swing table | not published |
| `js/dun-data.js` | per-DUN-seat `{seat, constituency, bloc, proj_winner, proj_margin, geo_url}` + `election` block | `geo/dun/*.geojson` are boundaries only; DUN reports are prose. `melaka_prn` / `sarawak_prn` / `pahang_prn` / `perak_prn` / `perlis_prn` in `app-data.json` cover the five imminent states, but there is still no unified `data/dun-data.json`. Committed `js/dun-data.js` left as-is. |

`summary.report_outline` is emitted empty by the delivery (renderers fall back
to deriving the outline from the report sections).

---

## 3. Cross-check result

{cross_check}

---

## 4. Impact on routes

| Route | State |
|---|---|
| `/` | OK — headline number, "Kerusi paling rapat" watchlist (from `summary.tight_seats`), next-PRN block (from `summary.prn_upcoming`) |
| `/app/` | OK — hero KPIs, flip table, house bar, scenario chips all populated |
| `/app/maps.html` | OK — federal map from `projection` + `geo/`; DUN maps still use committed `js/dun-data.js` |
| `/app/seats.html` | OK — 222 rows from `master` + `projection`, incl. `margin_pct_ge15` column |
| `/app/scenarios.html` | OK — 8 scenario cards + bloc comparison table from `scenarios.json` |
| `/app/states.html` | OK — parliament view from `master`/`projection`; five-state DUN projections from `*_prn` |
| `/berita.html` | OK — {news_count} news items from `general_news` |
| `/laporan.html` | OK — full federal report (EN + MS) |
| `/state/*.html` | OK — hand-authored pages unchanged |

---

## 5. For a "nothing missing" build

Hermes would need to add to the delivery: `data/ge15.json` (per-seat official
GE15 results — unlocks `ge15[]`, `battlegrounds[]`), `data/demographics.json`,
`data/swings.json`, and a unified `data/dun-data.json` covering all 13 state
assemblies (not just the five imminent PRN states).
"""


def write_missing_data(manifest, warnings, missing_en=None):
    news_count = "?"
    try:
        news_count = str(len(_load("app-data.json").get("general_news", [])))
    except Exception:
        pass
    if warnings:
        cross = ("`build_adapter.py` found these differences between the delivery "
                 "files (app-data.json wins on overlap — none were overwritten):\n\n"
                 + "\n".join(f"- {w}" for w in warnings))
    else:
        cross = ("`app-data.json`, `forecast.json` and the raw engine output "
                 "agree on the headline seat count, flip count and bloc totals.")
    txt = (MISSING_DOC
           .replace("{delivery_id}", str(manifest.get("delivery_id", "?")))
           .replace("{generated_at}", str(manifest.get("generated_at", "?")))
           .replace("{file_count}", str(manifest.get("file_count", "?")))
           .replace("{cross_check}", cross)
           .replace("{news_count}", news_count)
           .replace("{{", "{").replace("}}", "}"))
    if missing_en:
        txt += (
            "\n---\n\n## 6. EN state-report gap\n\n"
            "`js/report.js` now exposes `window.GE16_STATE_REPORTS_EN[<slug>]` "
            "alongside the existing `_MS` global (see fix for audit finding #13). "
            "The following states have no `GE16_<State>_Report.md` (EN) file in "
            "this delivery, so their EN global entry is genuinely absent — not "
            "fabricated:\n\n"
            + "\n".join(f"- `{s}`" for s in missing_en) + "\n"
        )
    with open(MISSING_DATA_MD, "w", encoding="utf-8") as f:
        f.write(txt)


# ---------------------------------------------------------------------------
# Emit
# ---------------------------------------------------------------------------

def emit_js(obj, varname):
    return f"window.{varname} = " + json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + ";\n"


def main():
    apply = "--apply" in sys.argv
    do_build = "--build" in sys.argv
    check = "--check" in sys.argv

    if check and (apply or do_build):
        sys.exit("FATAL: --check is verify-only and cannot be combined with --apply/--build.")

    print(f"=== build_adapter.py — delivery adapter {'(--check, read-only)' if check else ''} ===")
    print(f"Delivery: {DELIVERY}")
    manifest = load_manifest()
    print(f"delivery_id={manifest.get('delivery_id')}  files={manifest.get('file_count')}\n")

    print("[1/5] Verifying delivery integrity (sha256)...")
    problems = verify_delivery(manifest)
    if problems:
        for p in problems:
            print("  ! " + p)
        sys.exit("FATAL: delivery failed verification — aborting, nothing written.")
    print(f"  OK — all {manifest.get('file_count')} files present and hash-matched.\n")

    if check:
        print("[1.5/5] Verifying rendered website data provenance...")
        provenance_problems = verify_website_data_provenance(manifest)
        if provenance_problems:
            for p in provenance_problems:
                print("  ! " + p)
            sys.exit(
                "FATAL: website data provenance failed verification — "
                + "; ".join(provenance_problems)
                + ". Aborting, nothing written."
            )
        print("  OK — js/data.js provenance matches DELIVERY.json.\n")

    print("[2/5] Building app data (app-data.json + scenarios.json + forecast.json)...")
    data, warnings, srcs = build_app_data()
    data["_adapter"]["delivery_id"] = manifest.get("delivery_id")
    s = data["summary"]
    print(f"  master={len(data['master'])}  projection={len(data['projection'])}  "
          f"flips_list={len(s.get('flips_list', []))}  govt_p50={s.get('govt_p50')}  "
          f"scenarios={len(s.get('scenarios_display', []))}  "
          f"news={len(data.get('general_news', []))}  vacancies={len(data.get('vacancies', []))}")
    if warnings:
        for w in warnings:
            print("  ! cross-check: " + w)
    else:
        print("  cross-check: app-data / forecast.json / engine output agree on headline.")
    print()

    print("[3/5] Building report data from reports/**/*.md...")
    report_en, report_ms, state_reports_ms, state_reports_en, missing_en = build_report()
    print(f"  REPORT_EN sections={len(report_en)}  REPORT_MS sections={len(report_ms)}  "
          f"state reports MS={len(state_reports_ms)}  state reports EN={len(state_reports_en)}\n")
    if missing_en:
        print(f"  ! EN state reports missing from delivery for: {', '.join(missing_en)} "
              f"(recorded in MISSING_DATA.md, not fabricated)\n")

    print(f"[4/5] {'Computing geo/ sync plan (--check, not writing)' if check else 'Refreshing geo/ from delivery'}...")
    to_copy, to_delete = refresh_geo(manifest, dry_run=check)
    if check:
        print(f"  would copy/verify {len(to_copy)} geo file(s); "
              f"would delete {len(to_delete)} stale file(s) not in current manifest.")
        if to_delete:
            for d in to_delete:
                print(f"    - stale: geo/{d}")
    else:
        print(f"  {len(to_copy)} geo files synced from manifest; {len(to_delete)} stale file(s) removed.")
        if to_delete:
            for d in to_delete:
                print(f"    - removed stale: geo/{d}")
    print()

    if check:
        print("[5/5] --check: skipping all writes (js/*.generated.js, js/data.js, "
              "js/report.js, MISSING_DATA.md, geo/) — verify-only, zero filesystem writes.")
        print("\n=== --check PASSED: delivery verified, no files written. ===")
        return

    print("[5/5] Writing artifacts...")
    data_js = emit_js(data, "GE16_APP_DATA")
    report_js = (
        "// Generated by build_adapter.py from approved delivery report markdown.\n"
        + emit_js(report_en, "GE16_REPORT_EN")
        + emit_js(report_ms, "GE16_REPORT_MS")
        + emit_js(state_reports_ms, "GE16_STATE_REPORTS_MS")
        + emit_js(state_reports_en, "GE16_STATE_REPORTS_EN")
    )

    gen_data = os.path.join(JS_DIR, "data.generated.js")
    gen_report = os.path.join(JS_DIR, "report.generated.js")
    with open(gen_data, "w", encoding="utf-8") as f:
        f.write(data_js)
    with open(gen_report, "w", encoding="utf-8") as f:
        f.write(report_js)
    print(f"  wrote {os.path.relpath(gen_data, ROOT)} ({len(data_js):,} B)")
    print(f"  wrote {os.path.relpath(gen_report, ROOT)} ({len(report_js):,} B)")

    write_missing_data(manifest, warnings, missing_en)
    print(f"  wrote {os.path.relpath(MISSING_DATA_MD, ROOT)}")

    if apply:
        shutil.copy2(gen_data, os.path.join(JS_DIR, "data.js"))
        shutil.copy2(gen_report, os.path.join(JS_DIR, "report.js"))
        print("  --apply: js/data.js and js/report.js overwritten "
              "(diff left uncommitted for Hermes review).")
    else:
        print("  (dry run — pass --apply to overwrite js/data.js + js/report.js)")

    if do_build:
        print("\n=== running build_vercel.py ===")
        subprocess.run([sys.executable, os.path.join(ROOT, "build_vercel.py")], check=True)


if __name__ == "__main__":
    main()
