#!/usr/bin/env python3
# Provenance: original path 05_AUTOMATION/track_ge16_polls.py; original SHA-256 f19503bdb5185d4fd4c321589624759700c37e02f3a98056ef4c2a8a7ba04109; classification active (trackers; OPS 8c1852b); versioned 2026-09-11.
"""GE16 Poll Tracker — monitors Merdeka Center & Ilham Centre for new election polls.

Watches:
  1. Merdeka Center homepage (og tags)
  2. Ilham Centre Facebook page (og tags)
  3. Google News RSS for poll-related headlines mentioning either organisation

New findings are appended to ge16-poll-tracker-log.md (and de-duplicated via
ge16-polls-tracked.json). Prints summary to stdout (used by cron delivery).

Usage: python3 scripts/collect/track_ge16_polls.py
"""
import json
import os
import re
import urllib.request
import urllib.parse
import sys
from datetime import datetime, timezone
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import ge16_tracker_outdir as outdir  # noqa: E402

def resolve_data_root(anchor=None):
    """Find the DATA repository root independently of cwd."""
    # V3 (P1.7): monorepo — canonical tree is <root>/data/canonical
    import sys as _sys
    _here = Path(anchor or __file__).resolve()
    _sys.path.insert(0, str(_here.parents[3]))
    try:
        import v3_paths
        return v3_paths.data_root() / "canonical"
    finally:
        _sys.path.pop(0)


DATA_ROOT = resolve_data_root()
BASE = str(DATA_ROOT / "research" / "trackers")
LOG = os.path.join(BASE, "ge16-poll-tracker-log.md")
DB = os.path.join(BASE, "ge16-polls-tracked.json")

HEADERS = {"User-Agent": "GE16-Poll-Tracker/1.0 (research)"}

WATCHES = [
    {"org": "Merdeka Center", "type": "page", "url": "https://merdeka.org/",
     "keywords": ["poll", "survey", "election", "GE16", "approval", "undi"]},
    {"org": "Ilham Centre", "type": "page", "url": "https://www.facebook.com/ILHAMResearchCentre/",
     "keywords": ["poll", "survey", "election", "GE16", "undi"]},
    # Google News RSS queries (reliable detection of poll announcements)
    {"org": "Merdeka Center", "type": "rss",
     "url": "https://news.google.com/rss/search?q=" + urllib.parse.quote("Merdeka Center poll Malaysia"),
     "keywords": ["poll", "survey", "election", "GE16", "approval", "undi"]},
    {"org": "Ilham Centre", "type": "rss",
     "url": "https://news.google.com/rss/search?q=" + urllib.parse.quote("Ilham Centre poll Malaysia"),
     "keywords": ["poll", "survey", "election", "GE16", "undi"]},
    {"org": "Malaysia GE16", "type": "rss",
     "url": "https://news.google.com/rss/search?q=" + urllib.parse.quote("GE16 Malaysia election survey"),
     "keywords": ["poll", "survey", "GE16", "election"]},
]


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def load_db():
    # read live (or the staged copy when this run owns one): a staged pass must
    # dedupe against the real owner state, never against an empty staging file
    path = outdir.r(DB)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"seen": []}


def save_db(db):
    db["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    target = outdir.w(DB)
    with open(target, "w") as f:
        json.dump(db, f, indent=2)


def dynamic_window_days():
    """Lookback = max(cron interval, days since last successful run).

    User directive (6 Aug 2026): the collect window must cover AT LEAST the
    gap since the last successful run, so a failed/missed cron never creates
    a data hole. GE16_NEWS_MAX_DAYS env var wins when set (one-off baseline
    sweeps). GE16_CRON_INTERVAL_DAYS defaults to 7 (weekly).
    """
    env = os.environ.get("GE16_NEWS_MAX_DAYS")
    if env:
        return int(env)
    interval = int(os.environ.get("GE16_CRON_INTERVAL_DAYS", "7"))
    since_last = interval
    try:
        d = load_db()
        ts = d.get("generated_at", "")
        if ts:
            last = datetime.fromisoformat(ts.replace("Z", "+00:00").split("[")[0])
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            gap = (datetime.now(timezone.utc) - last).total_seconds() / 86400.0
            since_last = max(interval, int(gap) + 1)
    except Exception:
        pass
    return since_last


def extract_items(src, html):
    items = []
    if src["type"] == "page":
        og = re.findall(r'property="og:(?:title|description)"\s+content="([^"]+)"', html, re.I)
        for c in og:
            c = re.sub(r"&amp;", "&", c).strip()
            if c:
                items.append(c)
    elif src["type"] == "rss":
        # parse <item><title>...</title></item> blocks
        for m in re.finditer(r"<item>(.*?)</item>", html, re.S):
            block = m.group(1)
            tm = re.search(r"<title>(.*?)</title>", block, re.S)
            if tm:
                items.append(re.sub(r"<!\[CDATA\[|\]\]>", "", tm.group(1)).strip())
    return items


def main():
    os.makedirs(BASE, exist_ok=True)
    outdir.ensure()  # GE16_TRACKER_OUT_DIR: staged writes leave the live DB alone
    db = load_db()
    seen = set(db["seen"])
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new_items = []

    for src in WATCHES:
        url = src["url"]
        if src["type"] == "rss" and "news.google.com/rss/search" in url:
            # dynamic window: max(cron interval, gap since last successful run).
            # The when:Nd operator must live INSIDE the q= parameter.
            w = dynamic_window_days()
            if w > 1 and "when:" not in url:
                url = re.sub(r"(q=)([^&]+)",
                             lambda m: m.group(1) + urllib.parse.quote(
                                 urllib.parse.unquote(m.group(2)) + " when:" + str(w) + "d"),
                             url)
        try:
            html = fetch(url)
        except Exception as e:
            print(f"[{src['org']}|{src['type']}] FETCH ERROR: {e}")
            continue
        for c in extract_items(src, html):
            if any(k.lower() in c.lower() for k in src["keywords"]):
                key = f"{src['org']}|{c[:100]}"
                if key not in seen:
                    seen.add(key)
                    new_items.append({"org": src["org"], "title": c, "source": src["url"], "found_at": now})

    if new_items:
        db["seen"] = sorted(seen)
        save_db(db)
        with open(outdir.a(LOG), "a") as f:
            f.write(f"\n## Scan {now}\n")
            for item in new_items:
                f.write(f"- **{item['org']}**: {item['title']}\n")
        print(f"NEW GE16 POLL ITEMS FOUND ({len(new_items)}):")
        for item in new_items:
            print(f"- [{item['org']}] {item['title']}")
    else:
        print("No new GE16 poll items detected since last scan.")


if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
    else:
        main()
