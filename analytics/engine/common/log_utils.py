#!/usr/bin/env python3
# Provenance: original path 02_FORECAST/engine/log_utils.py; original SHA-256 495dc9e419072cf15169861f73fd0bc52a1fc451d74534912d40ab931ad40b2a; classification ignored (engine family; Task 4.1 split action); versioned 2026-09-11.
"""Shared audit logger for GE16 cron jobs.

Every cron run finishes by calling this: it appends a CHANGELOG entry, writes a
machine-readable run record, updates the LATEST pointer, and regenerates STATUS.md.

Usage:
  .venv/bin/python 02_FORECAST/engine/log_utils.py \
      --job <job_id> --name "<job name>" \
      --summary "<what changed / key numbers>" \
      --files "path1,path2,path3" \
      --kind forecast|polls|candidates

Run from the project root.

Compatibility: this version writes run tracking to `work/tracking/`. Existing consumers of `.tracking/LATEST.json` are intentionally unchanged until Task 4.5.
"""
import argparse
import json
import os
import warnings
from pathlib import Path
from datetime import datetime

def resolve_repository_root(anchor=None):
    """Find the ANALYTICS repository root independently of cwd."""
    anchor_path = Path(anchor or __file__).resolve()
    return anchor_path.parents[3]


ROOT = str(resolve_repository_root())
TRACKING = os.path.join(ROOT, "work", "tracking")
CHANGELOG = os.path.join(ROOT, "CHANGELOG.md")
STATUS = os.path.join(ROOT, "STATUS.md")
RUNS = os.path.join(TRACKING, "runs")

KIND_EMOJI = {"forecast": "📈", "polls": "📊", "candidates": "🗳️"}


def ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def stamp():
    return datetime.now().strftime("%Y-%m-%d")


def files_info(file_list):
    out = []
    for f in file_list:
        p = os.path.join(ROOT, f)
        if os.path.exists(p):
            out.append({"path": f, "size": os.path.getsize(p),
                        "mtime": datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M")})
        else:
            out.append({"path": f, "size": None, "mtime": None, "missing": True})
    return out


def append_changelog(entry):
    """Insert entry at the TOP of the changelog body (newest-first order).
    Preserves the 3-line header: title + blank + description + blank."""
    os.makedirs(os.path.dirname(CHANGELOG), exist_ok=True)
    if not os.path.exists(CHANGELOG):
        with open(CHANGELOG, "w") as f:
            f.write("# GE16 Project Changelog\n\nNewest entries first. Audit of every cron run and manual update.\n\n")
        return
    existing = open(CHANGELOG, encoding="utf-8").read()
    # Find the first entry (starts with "### ")
    idx = existing.find("\n### ")
    if idx == -1:
        # No entries yet — just append
        with open(CHANGELOG, "a", encoding="utf-8") as f:
            f.write("\n" + entry + "\n")
        return
    header = existing[:idx + 1]  # everything before first ### (includes trailing \n)
    body = existing[idx + 1:]     # all entries
    with open(CHANGELOG, "w", encoding="utf-8") as f:
        f.write(header + entry + "\n" + body)


def write_run_record(job, name, summary, files, kind):
    os.makedirs(RUNS, exist_ok=True)
    rec = {
        "run_at": datetime.now().isoformat(),
        "job_id": job,
        "job_name": name,
        "kind": kind,
        "summary": summary,
        "files": files,
    }
    fname = f"{stamp()}-{kind}-{job[:8]}.json"
    path = os.path.join(RUNS, fname)
    with open(path, "w") as f:
        json.dump(rec, f, indent=2)
    # LATEST pointer
    with open(os.path.join(TRACKING, "LATEST.json"), "w") as f:
        json.dump({"latest_run": fname, "latest_at": rec["run_at"], "record": rec}, f, indent=2)
    return fname


def collect_state():
    """Scan tracked files/dirs for the STATUS.md table."""
    tracked = {
        "Poll tracker log": (os.path.join(TRACKING, "ge16-poll-tracker-log.md"), "append-only"),
        "Candidate tracker log": (os.path.join(TRACKING, "ge16-candidate-tracker-log.md"), "append-only"),
        "Forecast latest": ("02_FORECAST/outputs/latest/ge16-forecast-latest.json", "LATEST (overwritten)"),
        "Forecast weekly log": ("02_FORECAST/logs/weekly-forecast-log.md", "append-only"),
        "Forecast engine config": ("02_FORECAST/engine/config.py", "overwritten weekly"),
        "App data (data.js)": ("HitungKerusi 222/js/data.js", "regenerated"),
        "Report (MD)": ("03_REPORTS/GE16_Malaysia_General_Election_Report.md", "manual updates"),
    }
    rows = []
    for label, (path, mode) in tracked.items():
        p = path if os.path.isabs(path) else os.path.join(ROOT, path)
        if os.path.exists(p):
            mt = datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M")
            rows.append((label, mode, path, mt))
        elif path.startswith(TRACKING):
            warnings.warn(f"Optional generated tracking input is missing: {p}", RuntimeWarning)
    return rows


def history_count():
    h = os.path.join(ROOT, "02_FORECAST", "outputs", "history")
    if os.path.isdir(h):
        return len([f for f in os.listdir(h) if f.endswith(".json")])
    return 0


def latest_forecast_numbers():
    p = os.path.join(ROOT, "02_FORECAST", "outputs", "latest", "ge16-forecast-latest.json")
    if os.path.exists(p):
        try:
            d = json.load(open(p))
            mc = d.get("monte_carlo", {})
            return (f"Govt-aligned P50={mc.get('P50')} · P10={mc.get('P10')} · P90={mc.get('P90')} · "
                    f"P(majority)={round(mc.get('P_majority', 0)*100, 1)}% · flips≈{mc.get('flips_P50')}")
        except Exception:
            return "n/a"
    return "n/a"


def recent_runs(n=6):
    if not os.path.isdir(RUNS):
        return []
    files = sorted(os.listdir(RUNS), reverse=True)[:n]
    out = []
    for f in files:
        try:
            d = json.load(open(os.path.join(RUNS, f)))
            out.append((d.get("run_at", "")[:16], d.get("job_name", ""), d.get("summary", "")))
        except Exception:
            pass
    return out


def regenerate_status():
    rows = collect_state()
    hcount = history_count()
    nums = latest_forecast_numbers()
    runs = recent_runs()
    now = ts()
    lines = []
    lines.append("# Project Status — GE16 Malaysia\n")
    lines.append(f"**Auto-generated:** {now} — updated after every cron run.\n")
    lines.append("> **How to read this project:** files named `latest/` or marked **LATEST** are the current "
                 "version; `history/` + dated files are the archive (refer back to them); `CHANGELOG.md` records "
                 "what changed and when.\n")
    lines.append("## Current forecast\n")
    lines.append(f"- {nums}\n")
    lines.append(f"- History snapshots: **{hcount}** in `02_FORECAST/outputs/history/`\n")
    lines.append("## Tracked files (current state)\n")
    lines.append("| File | Mode | Last updated |\n|---|---|---|")
    for label, mode, path, mt in rows:
        marker = " **← LATEST**" if "latest" in path or "LATEST" in mode else ""
        lines.append(f"| `{path}`{marker} | {mode} | {mt} |")
    lines.append("\n## Recent runs\n")
    if runs:
        lines.append("| When | Job | Summary |\n|---|---|---|")
        for when, job, summary in runs:
            lines.append(f"| {when} | {job} | {summary} |")
    else:
        lines.append("_No tracked runs yet._\n")
    lines.append("\n## Latest run pointer\n")
    lat = os.path.join(TRACKING, "LATEST.json")
    if os.path.exists(lat):
        d = json.load(open(lat))
        lines.append(f"- **LATEST run:** `{d.get('latest_run')}` at {d.get('latest_at', '')[:16]}\n")
    with open(STATUS, "w") as f:
        f.write("\n".join(lines))
    print(f"STATUS.md regenerated ({os.path.getsize(STATUS)} bytes)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True, help="cron job id")
    ap.add_argument("--name", required=True, help="job display name")
    ap.add_argument("--summary", required=True, help="what changed / key numbers")
    ap.add_argument("--files", required=True, help="comma-separated relative paths touched")
    ap.add_argument("--kind", default="forecast", choices=["forecast", "polls", "candidates"])
    args = ap.parse_args()

    files = [f.strip() for f in args.files.split(",") if f.strip()]
    finfo = files_info(files)
    emoji = KIND_EMOJI.get(args.kind, "🔧")
    entry = (f"### {ts()} — {emoji} {args.name} (`{args.job}`)\n"
             f"- **Summary:** {args.summary}\n"
             f"- **Files touched:** {', '.join(f['path'] for f in finfo)}\n")
    append_changelog(entry)
    fname = write_run_record(args.job, args.name, args.summary, finfo, args.kind)
    regenerate_status()
    print(f"Changelog appended · run record: {fname}")


if __name__ == "__main__":
    main()
