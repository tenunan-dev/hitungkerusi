#!/usr/bin/env python3
# Provenance: original path 05_AUTOMATION/track_ge16_news.py; original SHA-256 0315e8d88c36674d9251597c61c855e1e53b26c10b6290718efe478521180d20; classification active (trackers; OPS 8c1852b); versioned 2026-09-11.
"""GE16 News Tracker v5 — self-healing collect, chunked judgment, safe commit.

v4 simplification (user directive 5 Aug 2026):
- NO keyword search rules. The script only COLLECTS: fetch feeds + Google News
  queries, dedupe against seen history, keep the recent window, and write a
  candidate list (title + description + date + source + link).
- The LLM (cron agent) reviews each candidate's title AND story description and
  decides whether it is Malaysian political / election related, then classifies
  it (lang, category, blocs, score). Judged items go to ge16-news-judged.json.
- `--commit` merges judged items into the accepted history, appends the MD log
  (v2 line format, so report_builder.py keeps working), and rebuilds the
  structured page feed (dedup EN/MS pairs, per-source cap, newest-first).

v5 self-healing contract (owner directive 24 Sep 2026):
1. COLLECT NEVER BURNS OR SILENTLY DROPS A CANDIDATE.
   - collect() no longer advances the persistent seen file
     (ge16-general-news-tracked.json). It records every candidate it presents
     in ge16-news-seen-pending.json ("uncommitted" tombstone: key -> item +
     collection_id) and stamps each candidate with that collection_id.
   - Each candidate write is announced in the payload (`dropped_beyond_cap`,
     `replayed_unjudged`) and on stdout; the 200-cap overflow is reported with
     "WARNING: dropped N candidates beyond 200 cap" and is carried in the
     tombstone, never forgotten.
   - Only a SUCCESSFUL --commit merges those keys into the persistent seen file
     and shrinks the tombstone. If commit never runs (phase B failed), the next
     collect re-presents the carried items first, re-judged, inside the dynamic
     window.
2. CHUNKED, CHECKPOINTED JUDGMENT (`--judge-input`).
   - Splits ge16-news-candidates.json into batches of GE16_JUDGE_BATCH (50) and
     writes ge16-news-judge-batch-<n>.json + ge16-news-judge-manifest.json
     (total, batch_size, batch_count, which batch files exist / are judged).
   - --commit accepts the consolidated ge16-news-judged.json, or - when that
     file is absent - the ge16-news-judged-batch-<n>.json files listed in the
     manifest. Partial judgment (some but not all batches) FAILS LOUD with
     "partial judgment: X/Y batches judged - rerun judgment for missing
     batches", exits non-zero and commits nothing, so the previous feed and
     accepted history stay valid and nothing is half-committed.
3. Automatic operational retry without owner monitoring. Every mode records its
   outcome in research/trackers/ge16-selfheal-state.json through the helper
   ge16_selfheal_state.py (step/outcome/failure_class/detail/cycle). A previous
   cycle that failed or was partial makes the next run print the
   "SELFHEAL: previous cycle failed at <step> (...)" banner, so the live cron
   agent reruns exactly that step. No new cron job, no daemon.

Zero-acceptance cycles stay legal: an explicit {"accepted": []} judged payload
(consolidated or all batches) commits the empty outcome, rebuilds the feed from
accepted history, clears the tombstone and exits 0.

Files:
  ge16-news-candidates.json  (NEW — unjudged candidates, written by collect mode)
  ge16-news-judged.json      (NEW — LLM's accepted+classified items, consumed by --commit)
  ge16-news-judge-batch-<n>.json    (NEW — judge input batches, written by --judge-input)
  ge16-news-judged-batch-<n>.json   (NEW — per-batch judged output, consumed by --commit)
  ge16-news-judge-manifest.json     (NEW — batch manifest, written by --judge-input)
  ge16-news-seen-pending.json       (NEW — uncommitted candidate keys + items)
  ge16-selfheal-state.json          (NEW — last outcome per step, see helper docstring)
  ge16-news-accepted.json    (NEW — append-only accepted history)
  ge16-general-news-log.md   (append-only, v2 format — unchanged for report_builder)
  ge16-general-news-tracked.json (dedup seen keys; only --commit advances it)
  ge16-news-feed.json        (page feed — what gen_app_data2.py should read)

Usage:
  python3 scripts/collect/track_ge16_news.py              # collect candidates
  python3 scripts/collect/track_ge16_news.py --judge-input # split into judge batches
  python3 scripts/collect/track_ge16_news.py --commit      # merge judged + rebuild feed

Exit codes: 0 success (including a legal zero-acceptance commit); 1 loud refusal
(partial judgment, stale batch evidence) with nothing committed.
"""
import glob
import re
import os
import sys
import json
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Self-heal helper lives beside this script; import it by absolute path so the
# cron runner, a plain shell invocation and the importlib-based tests all work
# regardless of cwd.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import ge16_selfheal_state as selfheal
import ge16_tracker_outdir as outdir

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
DIR = str(DATA_ROOT / "research" / "trackers")
LOG = os.path.join(DIR, "ge16-general-news-log.md")
DB = os.path.join(DIR, "ge16-general-news-tracked.json")
CANDIDATES = os.path.join(DIR, "ge16-news-candidates.json")
JUDGED = os.path.join(DIR, "ge16-news-judged.json")
ACCEPTED = os.path.join(DIR, "ge16-news-accepted.json")
FEED = os.path.join(DIR, "ge16-news-feed.json")

# --- v5 self-healing state (see module docstring) -----------------------
# Uncommitted candidate tombstone: key -> {collection_id, item}. Only a
# successful --commit moves these keys into DB ("seen").
SEEN_PENDING = os.path.join(DIR, "ge16-news-seen-pending.json")
# Chunked judgment artefacts (the cron agent writes the judged-* side).
JUDGE_MANIFEST = os.path.join(DIR, "ge16-news-judge-manifest.json")
JUDGE_BATCH_FMT = os.path.join(DIR, "ge16-news-judge-batch-%d.json")
JUDGED_BATCH_FMT = os.path.join(DIR, "ge16-news-judged-batch-%d.json")
JUDGED_BATCH_GLOB = os.path.join(DIR, "ge16-news-judged-batch-*.json")

# Candidate cap: kept for contract/stability. Overflow is reported, never
# silently dropped, and carried in SEEN_PENDING for the next cycle.
CANDIDATE_CAP = int(os.environ.get("GE16_NEWS_CANDIDATE_CAP", "200"))
# Judge batch size for --judge-input (agent-side chunking under the idle limit).
JUDGE_BATCH = int(os.environ.get("GE16_JUDGE_BATCH", "50"))

PENDING_SCHEMA = "ge16.news-seen-pending.v1"
JUDGE_BATCH_SCHEMA = "ge16.news-judge-batch.v1"
JUDGE_MANIFEST_SCHEMA = "ge16.news-judge-manifest.v1"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
}

# DIRECT_FEEDS: verified working RSS endpoints (6 Aug 2026 audit).
# - Direct feeds kept where they work (FMT, The Vibes, Utusan, CNA, SCMP, Kosmo,
#   MalaysiaNow, TMR, Scoop, Twentytwo13, Borneo Post, Dayak Daily, Rakyat Post,
#   Astro Awani, Merdeka Times).
# - Dead publisher RSS (Malaysiakini, The Star, Malay Mail, NST, The Edge, Bernama,
#   Berita Harian, Sinar Harian, Daily Express, Astro Awani legacy) are replaced by
#   Google News site: queries — the site: operator returns their articles through
#   Google's index (verified 100-item responses 6 Aug 2026).
# - NOTE: Google News site: URLs do NOT need a when: operator here — the tracker's
#   dynamic window appends when:<Nd> automatically in the collect loop.
GN = "https://news.google.com/rss/search?q="  # prefix; site: query + hl/gl appended below
DIRECT_FEEDS = [
    # --- direct RSS (working) ---
    ("Free Malaysia Today", "https://www.freemalaysiatoday.com/feed/"),
    ("The Vibes", "https://www.thevibes.com/rss"),
    ("Utusan Malaysia", "https://www.utusan.com.my/rss"),
    ("CNA Malaysia", "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml"),
    ("SCMP", "https://www.scmp.com/rss/91/feed"),
    ("Kosmo", "https://www.kosmo.com.my/rss"),
    ("MalaysiaNow", "https://www.malaysianow.com/feed/"),
    ("The Malaysian Reserve", "https://themalaysianreserve.com/feed/"),
    ("Scoop", "https://scoop.my/feed/"),
    ("Twentytwo13", "https://www.twentytwo13.my/feed/"),
    ("Borneo Post", "https://www.theborneopost.com/feed/"),
    ("Dayak Daily", "https://dayakdaily.com/feed/"),
    ("Rakyat Post", "https://www.therakyatpost.com/feed/"),
    ("Astro Awani", "https://www.astroawani.com/rss.xml"),
    ("The Merdeka Times", "https://www.themerdekatimes.com/feed/"),
    # --- dead RSS revived via Google News site: ---
    ("Malaysiakini [GN]", GN + urllib.parse.quote("site:malaysiakini.com malaysia politics") + "&hl=en-MY&gl=MY&ceid=MY:en"),
    ("The Star [GN]", GN + urllib.parse.quote("site:thestar.com.my malaysia politics") + "&hl=en-MY&gl=MY&ceid=MY:en"),
    ("Malay Mail [GN]", GN + urllib.parse.quote("site:malaymail.com malaysia politics") + "&hl=en-MY&gl=MY&ceid=MY:en"),
    ("New Straits Times [GN]", GN + urllib.parse.quote("site:nst.com.my malaysia politics") + "&hl=en-MY&gl=MY&ceid=MY:en"),
    ("The Edge Malaysia [GN]", GN + urllib.parse.quote("site:theedgemalaysia.com malaysia politics") + "&hl=en-MY&gl=MY&ceid=MY:en"),
    ("Bernama [GN]", GN + urllib.parse.quote("site:bernama.com malaysia politics") + "&hl=en-MY&gl=MY&ceid=MY:en"),
    ("Berita Harian [GN]", GN + urllib.parse.quote("site:bharian.com.my malaysia politics") + "&hl=en-MY&gl=MY&ceid=MY:en"),
    ("Sinar Harian [GN]", GN + urllib.parse.quote("site:sinarharian.com.my malaysia politics") + "&hl=en-MY&gl=MY&ceid=MY:en"),
    ("Daily Express [GN]", GN + urllib.parse.quote("site:dailyexpress.com.my malaysia politics") + "&hl=en-MY&gl=MY&ceid=MY:en"),
    ("Astro Awani [GN]", GN + urllib.parse.quote("site:astroawani.com malaysia politics") + "&hl=en-MY&gl=MY&ceid=MY:en"),
    # --- new media / digital-first sources ---
    ("Bersih [GN]", GN + urllib.parse.quote("site:bersih.org malaysia election") + "&hl=en-MY&gl=MY&ceid=MY:en"),
    ("MalaysiaGazette [GN]", GN + urllib.parse.quote("site:malaysiagazette.com malaysia politics") + "&hl=en-MY&gl=MY&ceid=MY:en"),
]

QUERIES = [
    "GE16 Malaysia",
    "PRU16",
    "Malaysia election news",
    "Malaysia politics",
    "Pakatan Harapan",
    "Perikatan Nasional",
    "Barisan Nasional",
    "Anwar government",
    "Malaysia parliament",
    "Malaysia by-election 2026",
    "pilihan raya Malaysia",
    "politik Malaysia",
    "kerajaan Malaysia",
    "Malaysia coalition politics",
    "Dewan Rakyat",
    "Malaysia candidate GE16",
    "undi Malaysia",
]

# ---- helpers -----------------------------------------------------------

def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def strip_tags(s):
    s = re.sub(r"<!\[CDATA\[|\]\]>", "", s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_pubdate(block):
    for pat in (r"<pubDate>(.*?)</pubDate>",
                r"<dc:date>(.*?)</dc:date>",
                r"<published>(.*?)</published>",
                r"<updated>(.*?)</updated>",
                r"<date>([0-9]{4}-[0-9]{2}-[0-9]{2})</date>"):
        m = re.search(pat, block, re.S)
        if m:
            d = m.group(1).strip()
            try:
                return datetime.fromisoformat(d.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat(timespec="seconds")
            except Exception:
                try:
                    from email.utils import parsedate_to_datetime
                    return parsedate_to_datetime(d).astimezone(timezone.utc).isoformat(timespec="seconds")
                except Exception:
                    return d
    return ""


def parse_rss_items(html):
    """Parse RSS 2.0 / Atom items -> list of (title, link, date, description)."""
    items = []
    for m in re.finditer(r"<item>(.*?)</item>", html, re.S):
        block = m.group(1)
        tm = re.search(r"<title>(.*?)</title>", block, re.S)
        lm = re.search(r"<link>(.*?)</link>", block, re.S)
        dm = re.search(r"<description>(.*?)</description>", block, re.S)
        if tm:
            items.append((strip_tags(tm.group(1)),
                          lm.group(1).strip() if lm else "",
                          parse_pubdate(block),
                          strip_tags(dm.group(1)) if dm else ""))
    for m in re.finditer(r"<entry>(.*?)</entry>", html, re.S):
        block = m.group(1)
        tm = re.search(r"<title[^>]*>(.*?)</title>", block, re.S)
        lm = re.search(r"<link[^>]*href=\"(.*?)\"", block, re.S)
        dm = re.search(r"<summary[^>]*>(.*?)</summary>", block, re.S)
        if tm:
            items.append((strip_tags(tm.group(1)),
                          lm.group(1) if lm else "",
                          parse_pubdate(block),
                          strip_tags(dm.group(1)) if dm else ""))
    return items


def extract_source(title):
    """Google News titles look like 'Headline - Publisher'. Split them."""
    m = re.search(r"\s-\s([A-Z][A-Za-z &'.\u00b7-]{2,40})$", title.strip())
    if m:
        return m.group(1).strip(), title[:m.start()].strip()
    return "", title.strip()


def dynamic_window_days():
    """Lookback = max(cron interval, days since last successful update).

    User directive (6 Aug 2026): the collect window must cover AT LEAST the
    gap since the last successful run, so a failed/missed cron never creates
    a data hole. GE16_NEWS_MAX_DAYS env var still wins when set (one-off
    baseline sweeps like the 120-day run). GE16_CRON_INTERVAL_DAYS defaults
    to 7 (weekly).
    """
    env = os.environ.get("GE16_NEWS_MAX_DAYS")
    if env:
        return int(env)
    interval = int(os.environ.get("GE16_CRON_INTERVAL_DAYS", "7"))
    since_last = interval
    try:
        acc = load_json(ACCEPTED, {"generated_at": ""})
        ts = acc.get("generated_at", "")
        if ts:
            last = datetime.fromisoformat(ts.replace("Z", "+00:00").split("[")[0])
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            gap = (datetime.now(timezone.utc) - last).total_seconds() / 86400.0
            since_last = max(interval, int(gap) + 1)  # +1 for partial-day margin
    except Exception:
        pass
    return since_last


def is_recent(date_str, max_days=None):
    """Keep items within the window; unparseable dates pass through.

    max_days defaults to a DYNAMIC window (6 Aug 2026): the larger of the
    cron interval (GE16_CRON_INTERVAL_DAYS, default 7) and the time since the
    last successful commit (ge16-news-accepted.json generated_at) — so a
    missed cron run backfills the gap. GE16_NEWS_MAX_DAYS env var overrides
    for one-off baseline sweeps (e.g. =120 for the 5 Aug baseline).
    """
    if max_days is None:
        max_days = dynamic_window_days()
    if not date_str:
        return True
    d = None
    try:
        s = date_str.replace("Z", "+00:00")
        if "+" not in s:
            s = s + "+00:00"
        d = datetime.fromisoformat(s.split("[")[0])
    except Exception:
        try:
            from email.utils import parsedate_to_datetime
            d = parsedate_to_datetime(date_str)
        except Exception:
            return True
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_days)
    return d >= cutoff


def load_json(path, default):
    # GE16_TRACKER_OUT_DIR: this run's staged copy wins, else the live file
    path = outdir.r(path)
    if os.path.exists(path):
        try:
            return json.load(open(path))
        except Exception as e:
            # Self-healing visibility: a corrupt state file must never be a
            # silent no-op. Never raise here - the caller falls back to default.
            print(f"WARNING: {os.path.basename(path)} is unreadable ({e}); using defaults")
    return default


def atomic_write_json(path, payload):
    """Write JSON through a temp file + os.replace, so a crash never truncates
    a canonical tracker (a half-written seen/accepted file used to require
    manual repair).

    GE16_TRACKER_OUT_DIR reroutes the write into the run's staging directory, so a
    staged commit produces a complete candidate blob the orchestrator can verify
    and only then swap into place.
    """
    path = outdir.w(path)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    import tempfile
    handle, temporary = tempfile.mkstemp(prefix=".tmp-" + os.path.basename(path), dir=directory)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=1)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.remove(temporary)
        raise


def candidate_key(item):
    """Dedup key for a candidate/accepted item (same rule as the seen history)."""
    title = (item.get("title") or "").strip() if isinstance(item, dict) else ""
    return title[:150]


def load_pending():
    """Read the uncommitted-candidate tombstone: {key: item-or-{item: item}}.

    A tombstone entry is the candidate item itself (with its collection_id); the
    nested {"item": ...} shape is also accepted so an older/foreign tombstone
    cannot silently blind the replay.
    """
    payload = load_json(SEEN_PENDING, {})
    entries = payload.get("pending") if isinstance(payload, dict) else None
    return entries if isinstance(entries, dict) else {}


def pending_item(entry):
    """The candidate item inside a tombstone entry (flat or nested), else None."""
    if not isinstance(entry, dict):
        return None
    nested = entry.get("item")
    if isinstance(nested, dict):
        return nested
    return entry if entry.get("title") else None


def write_pending(items, collection_id):
    """Atomically rebuild the tombstone from the candidates presented this run."""
    entries = {}
    for item in items:
        key = candidate_key(item)
        if not key:
            continue
        entry = dict(item)
        entry["collection_id"] = collection_id
        entries[key] = entry
    atomic_write_json(SEEN_PENDING, {
        "schema": PENDING_SCHEMA,
        "updated_at": collection_id,
        "collection_id": collection_id,
        "count": len(entries),
        "pending": entries,
    })
    return entries


def print_selfheal_banner(current_cycle):
    """One stdout line the live cron agent must see (see helper docstring)."""
    message = selfheal.banner(selfheal.load_state(), current_cycle)
    if message:
        print(message)
    return message


# ---- collect mode ------------------------------------------------------

def collect(record=True):
    os.makedirs(DIR, exist_ok=True)
    outdir.ensure()
    db = load_json(DB, {"seen": []})
    seen = set(db.get("seen") or []) if isinstance(db, dict) else set(db or [])
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print_selfheal_banner(now)
    candidates = []
    fetched = 0
    candidate_keys = set()

    # (v5.1) NEVER BURN: re-present candidates that were collected earlier but
    # never committed (phase B failed, or they overflowed the cap). They are put
    # first so a carried item can never starve behind the cap, and they are
    # re-judged by this cycle's agent. Entries that have aged out of the dynamic
    # window are dropped here - they can no longer be presented at all.
    pending = load_pending()
    replayed = 0
    for key, entry in pending.items():
        item = pending_item(entry)
        if item is None or not (item.get("title") or "").strip():
            continue
        if key in seen or key in candidate_keys:
            continue
        if not is_recent(item.get("date", "")):
            continue
        item = dict(item)
        item["replayed_unjudged"] = True
        candidates.append(item)
        candidate_keys.add(key)
        replayed += 1

    for name, feed_url in DIRECT_FEEDS:
        try:
            html = fetch(feed_url)
            fetched += 1
        except Exception as e:
            print(f"[feed: {name}] FETCH ERROR: {e}")
            continue
        for title, link, date, desc in parse_rss_items(html):
            if not title or not is_recent(date):
                continue
            key = title[:150]
            if key in seen or key in candidate_keys:
                continue
            candidate_keys.add(key)
            candidates.append({"query": f"feed:{name}", "title": title, "desc": desc[:400],
                               "link": link, "date": date or now, "source": name, "found_at": now})

    # Google News queries: add the when:<Nd> operator so the server returns
    # the dynamic window (gap since last successful run, or one-off override).
    # Only added when window > 1 day (Google's default ~24h already covers 1).
    g_windows = dynamic_window_days()
    for q in QUERIES:
        qq = q + f" when:{g_windows}d" if g_windows > 1 else q
        url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(qq) + "&hl=en-MY&gl=MY&ceid=MY:en"
        try:
            html = fetch(url)
            fetched += 1
        except Exception as e:
            print(f"[query: {q}] FETCH ERROR: {e}")
            continue
        for title, link, date, desc in parse_rss_items(html):
            if not title or not is_recent(date):
                continue
            source, clean_title = extract_source(title)
            key = (clean_title or title)[:150]
            if key in seen or key in candidate_keys:
                continue
            candidate_keys.add(key)
            candidates.append({"query": q, "title": clean_title or title, "desc": desc[:400],
                               "link": link, "date": date or now, "source": source or "Google News",
                               "found_at": now})

    # Carried (already-waiting) candidates first, then newest-first inside each
    # group, so the cap can never silently starve a previously collected item.
    candidates.sort(key=lambda x: x.get("date", ""), reverse=True)
    candidates = ([c for c in candidates if c.get("replayed_unjudged")]
                  + [c for c in candidates if not c.get("replayed_unjudged")])

    # (v5.3) cap kept for contract/stability, but overflow is LOUD: counted into
    # the payload, announced on stdout, and carried in the tombstone below.
    dropped = 0
    if len(candidates) > CANDIDATE_CAP:
        dropped = len(candidates) - CANDIDATE_CAP
        print(f"WARNING: dropped {dropped} candidates beyond {CANDIDATE_CAP} cap")
    kept = candidates[:CANDIDATE_CAP]
    overflow = candidates[CANDIDATE_CAP:]
    for item in kept + overflow:
        item["collection_id"] = now

    # (v5.1) The persistent seen file is NOT touched here. Everything presented
    # (kept + overflow) is tombstoned as uncommitted; only a successful --commit
    # moves those keys into the seen history.
    pending_written = write_pending(kept + overflow, now)

    payload = {"generated_at": now, "collection_id": now, "count": len(kept),
               "dropped_beyond_cap": dropped, "replayed_unjudged": replayed,
               "uncommitted_pending": len(pending_written), "items": kept}
    atomic_write_json(CANDIDATES, payload)
    print(f"{len(kept)} candidates -> {outdir.w(CANDIDATES)} ({fetched} feeds/queries)"
          f" | dropped_beyond_cap={dropped} replayed_unjudged={replayed}"
          f" | seen-pending={len(pending_written)} (uncommitted until --commit)")
    for c in kept[:12]:
        print(f"- [{c['date'][:16]}] ({c['source']}) {c['title'][:70]}")
    if record:
        selfheal.record("collect", "ok",
                        detail=f"{len(kept)} candidates; dropped_beyond_cap={dropped}; replayed_unjudged={replayed}",
                        cycle=now)
    return payload


# ---- judge input mode (v5.2 chunked, checkpointed judgment) ------------

def judge_input(record=True):
    """Split the candidate list into judge batches the cron agent can work
    through across separate turns (idle-limit safe, resumable).

    Writes ge16-news-judge-batch-<n>.json for every batch plus
    ge16-news-judge-manifest.json. Already-judged batches are reported as such
    and never overwritten - only the judge input side is regenerated, so a
    resumed cycle keeps the batches the agent already judged.
    """
    os.makedirs(DIR, exist_ok=True)
    outdir.ensure()
    payload = load_json(CANDIDATES, {})
    items = payload.get("items") if isinstance(payload, dict) else None
    items = items if isinstance(items, list) else []
    cycle = payload.get("generated_at", "") if isinstance(payload, dict) else ""
    print_selfheal_banner(cycle)

    size = JUDGE_BATCH if JUDGE_BATCH > 0 else 50
    batches = []
    for index in range(0, len(items), size):
        number = index // size + 1
        chunk = items[index:index + size]
        path = JUDGE_BATCH_FMT % number
        atomic_write_json(path, {"schema": JUDGE_BATCH_SCHEMA, "batch": number,
                                 "collection_id": cycle, "count": len(chunk),
                                 "items": chunk})
        batch = {"batch": number, "file": os.path.basename(path), "count": len(chunk),
                 "judged_file": os.path.basename(JUDGED_BATCH_FMT % number)}
        batches.append(batch)

    # Self-healing: a previous, larger cycle can leave higher-numbered batch
    # files behind. Drop the stale ones so nothing can be judged twice.
    expected = {os.path.abspath(outdir.w(JUDGE_BATCH_FMT % batch["batch"])) for batch in batches}
    for stale in outdir.g(os.path.join(DIR, "ge16-news-judge-batch-*.json")):
        if os.path.abspath(stale) not in expected:
            os.remove(stale)
            print(f"removed stale batch file {os.path.basename(stale)}")

    manifest = {"schema": JUDGE_MANIFEST_SCHEMA,
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "collection_id": cycle, "batch_size": size, "total": len(items),
                "batch_count": len(batches), "batches": batches}
    atomic_write_json(JUDGE_MANIFEST, manifest)

    pending_judged = [batch for batch in batches
                      if os.path.exists(outdir.r(JUDGED_BATCH_FMT % batch["batch"]))]
    print(f"JUDGE INPUT: {len(items)} candidates -> {len(batches)} batch file(s) of <= {size}"
          f" (collection_id {cycle or 'unknown'})")
    print(f"  manifest -> {outdir.w(JUDGE_MANIFEST)}")
    for batch in batches:
        judged = os.path.exists(outdir.r(JUDGED_BATCH_FMT % batch["batch"]))
        state = "already judged" if judged else "AWAITING JUDGMENT"
        print(f"  batch {batch['batch']}/{len(batches)}: {batch['count']} items"
              f" -> {batch['file']} | write {batch['judged_file']} [{state}]")
    if pending_judged and len(pending_judged) < len(batches):
        print(f"RESUME: {len(pending_judged)}/{len(batches)} batch(es) already judged"
              f" - judge the remaining {len(batches) - len(pending_judged)} first")
    print("After judging every batch, consolidate all accepted items into"
          f" {os.path.basename(JUDGED)} (the phase B gate requires it), then run --commit.")
    if record:
        selfheal.record("judge", "ok",
                        detail=f"{len(batches)} batch(es) written, {len(pending_judged)} already judged",
                        cycle=cycle)
    return manifest


# ---- commit mode -------------------------------------------------------

def norm_tokens(title):
    t = re.sub(r"[^a-z0-9 ]", " ", title.lower())
    return set(w for w in t.split() if len(w) >= 2)


STOP = set(
    "yang dan untuk dengan kata says said the a an of to in on at by from and or bagi kepada akan selepas "
    "sebelum kerana apabila ini itu tidak tak sedia mahu mesti is are was were be been has have had "
    "his her its their your my our me us we you they he she it about into over under after before during "
    "against between through without within across behind beyond near off onto out up down again further "
    "then once here there when where why how all any both each few more most other some such no nor not "
    "only own same so than too very can will just should now".split()
)


def dup_tokens(title):
    toks = norm_tokens(title)
    return set(w for w in toks if w not in STOP)


def is_dupe_pair(a_tokens, b_tokens):
    overlap = a_tokens & b_tokens
    return len(overlap) >= 3


def same_story_same_source(item, kept):
    """Same outlet + same bloc + different lang within 12h => EN/MS pair."""
    if not kept or not item.get("source") or not item.get("date"):
        return False
    blocs = set(item.get("blocs", []))
    lang = item.get("lang", "")
    if not blocs or not lang:
        return False
    try:
        t_item = datetime.fromisoformat(str(item["date"]).replace("Z", "+00:00").split("[")[0])
        if t_item.tzinfo is None:
            t_item = t_item.replace(tzinfo=timezone.utc)
    except Exception:
        return False
    for other in kept:
        if other.get("source") != item.get("source") or other.get("lang", "") == lang:
            continue
        if not set(other.get("blocs", [])) & blocs:
            continue
        try:
            t_other = datetime.fromisoformat(str(other["date"]).replace("Z", "+00:00").split("[")[0])
            if t_other.tzinfo is None:
                t_other = t_other.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if abs((t_item - t_other).total_seconds()) <= 12 * 3600:
            return True
    return False


class PartialJudgmentError(RuntimeError):
    """Some but not all judge batches produced output: commit must refuse."""

    def __init__(self, expected, present, missing):
        self.expected = expected
        self.present = present
        self.missing = missing
        super().__init__(
            "partial judgment: %d/%d batches judged \u2014 rerun judgment for missing batches %s"
            % (present, expected, ", ".join("ge16-news-judged-batch-%d.json" % n for n in missing)))


class StaleJudgmentError(RuntimeError):
    """Batch evidence belongs to a different (older) candidate collection."""


def _judged_batch_numbers():
    """Batch numbers listed in the judge manifest (fallback: judged batch files)."""
    manifest = load_json(JUDGE_MANIFEST, {})
    numbers = []
    if isinstance(manifest, dict):
        for entry in manifest.get("batches") or []:
            if isinstance(entry, dict) and isinstance(entry.get("batch"), int):
                numbers.append(entry["batch"])
    if numbers:
        return sorted(set(numbers)), manifest
    found = []
    for path in outdir.g(JUDGED_BATCH_GLOB):
        match = re.search(r"ge16-news-judged-batch-(\d+)\.json$", path)
        if match:
            found.append(int(match.group(1)))
    return sorted(set(found)), manifest


def gather_judged():
    """(items, source, manifest) for this cycle, or None when there is no evidence.

    Precedence: the consolidated ge16-news-judged.json (an explicit
    {"accepted": []} is a LEGAL zero-acceptance cycle), else the per-batch
    judged files listed in the judge manifest. Partial judgment raises
    PartialJudgmentError; batch evidence from another collection raises
    StaleJudgmentError. Both refuse to commit anything.
    """
    judged_path = outdir.r(JUDGED)
    if os.path.exists(judged_path):
        data = load_json(judged_path, None)
        if isinstance(data, dict) and ("accepted" in data or "items" in data):
            raw = data.get("accepted") if "accepted" in data else data.get("items")
            return (raw if isinstance(raw, list) else []), os.path.basename(judged_path), {}
        if isinstance(data, list):
            return data, os.path.basename(judged_path), {}
        print(f"WARNING: {os.path.basename(judged_path)} is not a judged payload;"
              " falling back to the judged batch files")

    numbers, manifest = _judged_batch_numbers()
    if not numbers:
        return None

    cycle = manifest.get("collection_id", "") if isinstance(manifest, dict) else ""
    payload = load_json(CANDIDATES, {})
    current = payload.get("generated_at", "") if isinstance(payload, dict) else ""
    if cycle and current and cycle != current:
        raise StaleJudgmentError(
            "stale judged batch evidence: judge manifest collection_id %s does not match"
            " candidates collection_id %s" % (cycle, current))

    present = [n for n in numbers if os.path.exists(outdir.r(JUDGED_BATCH_FMT % n))]
    if len(present) != len(numbers):
        raise PartialJudgmentError(len(numbers), len(present),
                                   [n for n in numbers if n not in present])

    items = []
    for number in numbers:
        data = load_json(JUDGED_BATCH_FMT % number, {})
        if isinstance(data, dict):
            raw = data.get("accepted") if "accepted" in data else data.get("items")
        else:
            raw = data
        if isinstance(raw, list):
            items.extend(raw)
    return items, "%d judged batch file(s)" % len(numbers), manifest


def mark_committed(judged_items):
    """(v5.1) AFTER a successful commit + feed rebuild: merge the judged
    candidate keys into the persistent seen history and shrink the tombstone.

    ONLY the keys of this cycle's judged set move: every candidate presented to
    the judge (the candidates payload) plus every accepted/classified item. Keys
    still waiting in the tombstone (cap overflow that nobody judged yet) are
    carried, so they get re-presented instead of burned.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db = load_json(DB, {"seen": []})
    seen = set(db.get("seen") or []) if isinstance(db, dict) else set(db or [])
    before = len(seen)
    payload = load_json(CANDIDATES, {})
    cand_items = payload.get("items") if isinstance(payload, dict) else None
    cand_items = cand_items if isinstance(cand_items, list) else []
    keys = {key for key in (candidate_key(item) for item in list(judged_items) + list(cand_items)) if key}
    seen |= keys

    record_db = dict(db) if isinstance(db, dict) else {"seen": []}
    record_db["seen"] = sorted(seen)
    record_db["last_commit_at"] = now
    record_db["last_judged_collection_id"] = payload.get("generated_at", "") if isinstance(payload, dict) else ""
    atomic_write_json(DB, record_db)

    entries = load_pending()
    pending_before = len(entries)
    for key in list(entries):
        if key in keys:
            del entries[key]
    atomic_write_json(SEEN_PENDING, {"schema": PENDING_SCHEMA, "updated_at": now,
                                     "collection_id": "", "count": len(entries),
                                     "pending": entries})
    print(f"SEEN: +{len(seen) - before} keys committed (seen total {len(seen)})"
          f" | seen-pending {pending_before} -> {len(entries)}"
          " (leftover keys = collected but never judged, re-presented next cycle)")
    return len(seen) - before, len(entries)


def clear_judge_artifacts():
    """A committed cycle's batch + manifest evidence is consumed; drop it so the
    next cycle starts clean. The accepted history is the durable record.

    With a staging directory set the staged copies are cleared only: live evidence
    is consumed by the orchestrator AFTER the verified merge has been swapped in,
    so a staged run never deletes live evidence it did not use.
    """
    removed = []
    for pattern in (os.path.join(DIR, "ge16-news-judge-batch-*.json"),
                    JUDGED_BATCH_GLOB, JUDGE_MANIFEST):
        for path in outdir.g(pattern):
            try:
                os.remove(path)
                removed.append(os.path.basename(path))
            except OSError as error:
                print(f"WARNING: could not clear {path}: {error}")
    if removed:
        shown = ", ".join(removed[:6]) + (" ..." if len(removed) > 6 else "")
        print(f"CLEARED: {len(removed)} judge artefact(s) after commit ({shown})")
    return removed


def commit(record=True):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = load_json(CANDIDATES, {})
    cycle = payload.get("generated_at", "") if isinstance(payload, dict) else ""
    print_selfheal_banner(cycle)

    try:
        evidence = gather_judged()
    except PartialJudgmentError as error:
        print(f"ERROR: {error}")
        print("REFUSING to commit: nothing was written to the accepted history or the feed,"
              " so the previous cycle's feed stays valid (a checkpoint, not a data hole)."
              " Run --judge-input, judge the missing batches, then --commit again.")
        if record:
            selfheal.record("judge", "partial", detail=str(error), cycle=cycle)
            selfheal.record("commit", "fail", cycle=cycle,
                            detail="refused partial judgment; nothing committed",
                            failure_class=selfheal.OPERATIONAL)
        return 1
    except StaleJudgmentError as error:
        print(f"ERROR: {error}")
        print("REFUSING to commit: nothing was written. Re-run collect and --judge-input for"
              " the current candidate set, then --commit.")
        if record:
            selfheal.record("commit", "fail", detail=str(error), cycle=cycle,
                            failure_class=selfheal.DESIGN)
        return 1

    if evidence is None:
        print("No judged evidence found (ge16-news-judged.json absent and no judged batch"
              " files) — nothing to commit; the candidate tombstone stays for the next cycle.")
        if record:
            selfheal.record("commit", "partial", cycle=cycle,
                            detail="no judged evidence yet; uncommitted candidates carried")
        return 0

    items, source, _manifest = evidence
    zero_acceptance = not items
    if zero_acceptance:
        print(f"ZERO-ACCEPTANCE cycle: {source} is an explicit empty judged payload."
              " Committing the empty outcome and rebuilding the feed from accepted history.")

    acc = load_json(ACCEPTED, {"items": []})
    acc_items = acc.get("items", []) if isinstance(acc, dict) else acc
    acc_titles = {it["title"][:150] for it in acc_items}

    new_acc, new_log = [], []
    for it in items:
        title = (it.get("title") or "").strip()
        if not title or title[:150] in acc_titles:
            continue
        acc_titles.add(title[:150])
        entry = {
            "query": it.get("query", ""),
            "title": title,
            "date": it.get("date", ""),
            "source": it.get("source", ""),
            "link": it.get("link", ""),
            "lang": it.get("lang", "en"),
            "category": it.get("category", "analysis"),
            "blocs": it.get("blocs", []),
            "parties": it.get("parties", []),
            "seats": it.get("seats", []),
            "score": float(it.get("score", 0.6)),
            "judged_at": now,
        }
        new_acc.append(entry)
        # v2 log line — keep byte-identical format for report_builder.py
        d = entry["date"][:16].replace("T", " ")
        src = f" [{entry['source']}]" if entry.get("source") else ""
        new_log.append(f"- **{entry['query']}** {title} — ({d}){src} — {entry['link']}")

    if not new_acc:
        print("All judged items already in accepted history — nothing new to commit.")
    else:
        acc_items = new_acc + acc_items
        atomic_write_json(ACCEPTED, {"generated_at": now, "count": len(acc_items), "items": acc_items})
        with open(outdir.a(LOG), "a") as f:
            f.write(f"\n## Scan {now} — {len(new_acc)} new items (LLM-judged)\n")
            for line in new_log:
                f.write(line + "\n")
        print(f"Committed {len(new_acc)} items from {source} -> {outdir.w(ACCEPTED)} + {outdir.a(LOG)}")

    # rebuild the page feed from accepted history
    # 8 Aug 2026 (user directive): hold the FULL dynamic window (≈7 days) so
    # the app can show 40 by default with a "see more" expand — cap 120
    # (7-day volume runs 3-147 items; 120 covers event weeks after dedup).
    feed_cap = int(os.environ.get("GE16_FEED_CAP", "120"))
    recent = [it for it in acc_items if is_recent(it.get("date", ""))]
    recent.sort(key=lambda x: x.get("date", ""), reverse=True)
    kept, per_source, seen_toks = [], {}, []
    for it in recent:
        src = it.get("source", "") or "?"
        if per_source.get(src, 0) >= 5:
            continue
        toks = dup_tokens(it["title"])
        if any(is_dupe_pair(toks, st) for st in seen_toks) or same_story_same_source(it, kept):
            continue
        seen_toks.append(toks)
        per_source[src] = per_source.get(src, 0) + 1
        kept.append(it)
        if len(kept) >= feed_cap:
            break
    feed_payload = {"generated_at": now, "schema": "ge16-news-feed v1", "count": len(kept), "items": kept}
    atomic_write_json(FEED, feed_payload)
    cats = {}
    for it in kept:
        cats[it["category"]] = cats.get(it["category"], 0) + 1
    print(f"FEED: {len(kept)} items -> {outdir.w(FEED)} | categories: {cats}")

    # (v5.1) ONLY NOW is the judge's ruling durable: the accepted history and the
    # feed are on disk, so the presented candidates may enter the seen history
    # and the tombstone may shrink.
    marked, pending_left = mark_committed(items)
    cleared = clear_judge_artifacts()
    if record:
        selfheal.record("judge", "ok", cycle=cycle,
                        detail=f"judged evidence consumed by commit ({source})")
        selfheal.record("commit", "ok", cycle=cycle,
                        detail=("zero-acceptance committed; " if zero_acceptance else "")
                               + f"{len(new_acc)} new accepted; seen +{marked}; pending {pending_left}"
                               + f"; cleared {len(cleared)} artefacts")
    return 0


def main(argv=None):
    """CLI: collect (default) | --judge-input | --commit. Returns an exit code.

    Every mode records its outcome in the self-heal state and prints the
    SELFHEAL banner when the previous cycle left work behind, so the live cron
    agent (which reads stdout) reruns the failed step. Exit 1 means a loud,
    all-or-nothing refusal: nothing was half-committed.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return 0
    if "--judge-input" in argv:
        mode, step = "judge-input", "judge"
    elif "--commit" in argv:
        mode, step = "commit", "commit"
    else:
        mode, step = "collect", "collect"
    try:
        if mode == "judge-input":
            judge_input()
        elif mode == "commit":
            return commit()
        else:
            collect()
    except Exception as error:
        print(f"ERROR: {mode} failed: {type(error).__name__}: {error}")
        try:
            selfheal.record(step, "fail", detail=f"{type(error).__name__}: {error}",
                            failure_class=selfheal.classify(str(error), error))
        except Exception as state_error:  # never mask the original failure
            print(f"WARNING: could not record self-heal state: {state_error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

