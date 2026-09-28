#!/usr/bin/env python3
# Provenance: original path 05_AUTOMATION/track_ge16_candidates.py; original SHA-256 8809ab62e91bf54e5ae9f213e514423550e131fd6b0a74591246491c16e67f58; classification active (trackers; OPS 8c1852b); versioned 2026-09-11.
"""GE16 Candidate Tracker — monitors news for which candidate goes to which seat.

Searches Google News RSS (multi-query) for GE16 candidate announcements and
seat allocations. New findings are appended to ge16-candidate-tracker-log.md
(de-duplicated via ge16-candidates-tracked.json). Prints summary to stdout
(used by cron delivery).

Usage: python3 scripts/collect/track_ge16_candidates.py
"""
import json
import os
import re
import urllib.request
import urllib.parse
import sys
from datetime import datetime, timezone
from pathlib import Path

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
LOG = os.path.join(BASE, "ge16-candidate-tracker-log.md")
DB = os.path.join(BASE, "ge16-candidates-tracked.json")

HEADERS = {"User-Agent": "GE16-Candidate-Tracker/1.0 (research)"}

# Google News RSS queries — each targets a candidate/seat angle
QUERIES = [
    # English — general
    "GE16 candidate seat",
    "GE16 candidate announced",
    "general election 16 candidate Malaysia seat",
    "PRU16 calon",
    "PRU16 calon kerusi",
    # Malay — candidate & seat allocation
    "calon GE16 diumumkan",
    "senarai calon PRU16",
    "kerusi PRU16 calon",
    "calon bertanding PRU16",
    # Seat allocation between coalition partners
    "seat allocation GE16",
    "peruntukan kerusi PRU16",
    "kerusi PH BN PRU16",
    "kerusi PN PRU16",
    "kerusi PAS PRU16",
    # Party-specific
    "BN calon PRU16",
    "PH calon PRU16",
    "PKR calon PRU16",
    "DAP calon PRU16",
    "PAS calon PRU16",
    "Bersatu calon PRU16",
    "Amanah calon PRU16",
    "GPS calon PRU16",
    "GRS calon PRU16",
    "Warisan calon PRU16",
    "Bersama calon PRU16",
    "Wawasan calon PRU16",
    # Big-name / battleground focus
    "Anwar Ibrahim GE16 seat",
    "Muhyiddin GE16 seat",
    "Zahid GE16 seat",
    "Hamzah Zainudin GE16",
    "Rafizi GE16 candidate",
    "battleground seat candidate GE16",
]

# Keywords that indicate a candidate/seat story (title-level filter)
TITLE_KEYWORDS = ["calon", "candidate", "seat", "kerusi", "contest", "bertanding",
                  "field", "calonkan", "menang tanpa bertanding", "seat allocation",
                  "peruntukan kerusi", "incumbent", "penyandang"]

# Keywords that indicate the story is about a specific person going to a seat
PERSON_PATTERNS = [
    r"\b(?:Tan Sri|Datuk Seri|Datuk|Dato|YB|Dr|Hajah|Haji|Ustaz)\b",  # honorific
    r"\b(?:Anwar|Muhyiddin|Zahid|Hamzah|Rafizi|Nik Nazmi|Nurul Izzah|Sanusi|Hadi|Samsuri|Loke|Saifuddin|Azmin|Fadillah|Abang Johari|Hajiji|Shafie|Mukhriz|Mahathir|Najib|Ismail Sabri|Ahmad Zahid|Hishammuddin|Wan Saiful|Syed Saddiq)\b",
]


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def load_db():
    if os.path.exists(DB):
        with open(DB) as f:
            return json.load(f)
    return {"seen": []}


def save_db(db):
    db["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(DB, "w") as f:
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


def parse_rss(html):
    """Return list of (title, link) from a Google News RSS feed."""
    items = []
    for m in re.finditer(r"<item>(.*?)</item>", html, re.S):
        block = m.group(1)
        tm = re.search(r"<title>(.*?)</title>", block, re.S)
        lm = re.search(r"<link>(.*?)</link>", block, re.S)
        if tm:
            title = re.sub(r"<!\[CDATA\[|\]\]>", "", tm.group(1)).strip()
            link = lm.group(1).strip() if lm else ""
            items.append((title, link))
    return items


def is_relevant(title):
    t = title.lower()
    if not any(k in t for k in TITLE_KEYWORDS):
        return False
    # must mention a person (honorific or known name) going to / contesting a seat
    for p in PERSON_PATTERNS:
        if re.search(p, title, re.I):
            return True
    return False


def main():
    os.makedirs(BASE, exist_ok=True)
    db = load_db()
    seen = set(db["seen"])
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new_items = []
    fetched_queries = 0

    for q in QUERIES:
        qq = q + f" when:{dynamic_window_days()}d" if dynamic_window_days() > 1 else q
        url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(qq) + "&hl=en-MY&gl=MY&ceid=MY:en"
        try:
            html = fetch(url)
            fetched_queries += 1
        except Exception as e:
            print(f"[query: {q}] FETCH ERROR: {e}")
            continue
        for title, link in parse_rss(html):
            if not is_relevant(title):
                continue
            key = title[:150]
            if key in seen:
                continue
            seen.add(key)
            new_items.append({"query": q, "title": title, "link": link, "found_at": now})

    if new_items:
        db["seen"] = sorted(seen)
        save_db(db)
        with open(LOG, "a") as f:
            f.write(f"\n## Scan {now} — {len(new_items)} new candidate/seat items\n")
            for item in new_items:
                f.write(f"- **[{item['query']}]** {item['title']} — {item['link']}\n")
        print(f"NEW GE16 CANDIDATE/SEAT ITEMS FOUND ({len(new_items)} from {fetched_queries} queries):")
        for item in new_items:
            print(f"- [{item['query']}] {item['title']}")
            print(f"  {item['link']}")
    else:
        print(f"No new GE16 candidate/seat items detected since last scan ({fetched_queries} queries run).")


if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
    else:
        main()
