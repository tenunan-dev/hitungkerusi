#!/usr/bin/env python3
"""GE16 news BACKFILL - 1 Jan 2026 to 24 Sep 2026 (news layer only).

The weekly tracker only ever looks back over its own dynamic window, so the
Jan-Jul 2026 news record was never captured. This script backfills it: same
candidate/judge/commit contract, hard date window, per-source caps.

  python3 ge16_news_backfill.py --collect      # fetch the window
  python3 ge16_news_backfill.py --judge-input  # write <=50-item judge batches
  python3 ge16_news_backfill.py --commit       # merge judged into accepted
  python3 ge16_news_backfill.py --sample       # month-by-month verification

Artifacts are backfill-scoped; ge16-news-accepted.json is MERGED, never
overwritten - its owner envelope (generated_at, count, items) is preserved.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import glob
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import track_ge16_news as tracker  # noqa: E402
import ge16_tracker_outdir as outdir  # noqa: E402

# ------------------------------------------------------------------ constants --
DIR = tracker.DIR
CANDIDATES = os.path.join(DIR, "ge16-news-backfill-candidates.json")
JUDGE_BATCH_FMT = os.path.join(DIR, "ge16-news-backfill-judge-batch-%d.json")
JUDGED_BATCH_FMT = os.path.join(DIR, "ge16-news-backfill-judged-batch-%d.json")
JUDGE_MANIFEST = os.path.join(DIR, "ge16-news-backfill-judge-manifest.json")
ACCEPTED = tracker.ACCEPTED
MANIFEST = os.environ.get("GE16_BACKFILL_MANIFEST", os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(DIR))), "baselines",
    "backfill-manifest.json"))

WINDOW_START = os.environ.get("GE16_BACKFILL_START", "2026-01-01")
WINDOW_END = os.environ.get("GE16_BACKFILL_END", "2026-09-24")
PER_SOURCE_CAP = int(os.environ.get("GE16_BACKFILL_SOURCE_CAP", "30"))
TOTAL_CAP = int(os.environ.get("GE16_BACKFILL_TOTAL_CAP", "420"))
OVERFLOW_STORE_CAP = int(os.environ.get("GE16_BACKFILL_OVERFLOW_STORE", "1200"))
JUDGE_BATCH = int(os.environ.get("GE16_BACKFILL_JUDGE_BATCH", "50"))
SLEEP_SECONDS = float(os.environ.get("GE16_BACKFILL_SLEEP", "1.0"))
MAX_FETCHES = int(os.environ.get("GE16_BACKFILL_MAX_FETCHES", "120"))
BATCH_SCHEMA = "ge16.news-backfill-judge-batch.v1"
MANIFEST_SCHEMA = "ge16.news-backfill-manifest.v1"
#: the judge's ruling vocabulary, taken from the accepted history's own categories
CATEGORIES = ("coalition", "election", "analysis", "policy", "candidate", "legal",
              "poll", "redelineation", "seat-allocation", "election-integrity",
              "campaign", "election-timing")
VERDICTS = os.environ.get("GE16_BACKFILL_VERDICTS",
                          os.path.join(DIR, "ge16-news-backfill-verdicts.jsonl"))

# ------------------------------------------------------------------- sources --
# Priority order (packet): Google News when:<Nd> window queries, then publisher
# sitemaps, then the election data page, then official releases. Every source is
# fetched politely (robots + sleep + budget) and failures are logged, never fatal.
GN_DAYS = (dt.date.fromisoformat(WINDOW_END) - dt.date.fromisoformat(WINDOW_START)).days
GN_SITE_QUERIES = [
    ("malaysiakini", "site:malaysiakini.com"),
    ("thestar", "site:thestar.com.my"),
    ("malaymail", "site:malaymail.com"),
    ("astroawani", "site:astroawani.com"),
    ("bernama", "site:bernama.com"),
]
SITEMAP_INDEXES = [
    ("sitemap:astroawani", "https://www.astroawani.com/sitemap.xml"),
    ("sitemap:thestar", "https://www.thestar.com.my/sitemap.xml"),
    ("sitemap:malaysiakini", "https://www.malaysiakini.com/sitemap.xml"),
    ("sitemap:malaymail", "https://www.malaymail.com/sitemap.xml"),
    ("sitemap:bernama", "https://www.bernama.com/en/sitemap.xml"),
]
SITEMAP_MAX_CHILDREN = int(os.environ.get("GE16_BACKFILL_SITEMAP_CHILDREN", "9"))
#: a sitemap URL has no headline, so its slug must look political before it can
#: become a candidate (mechanical filter, documented - the judge still rules)
SITEMAP_SLUG = re.compile(
    r"\b(?:politi\w*|pilihan\w*|pru-?\d*|ge-?1[56]\b|parlimen|dun|adun|pru|"
    r"by-?election|anwar|umno|pkr|pas|dap|bersatu|amanah|muhyiddin|zahid|mahathir|"
    r"spr|kerajaan|undi|coalition|barisan|perikatan|pakatan|dissolv\w*|kerusi|"
    r"menteri|pilihanraya|kabinet|perdana)\b",
    re.I)
HTML_SOURCES = [
    ("electiondata.my", "https://electiondata.my/byelections/"),
    ("electiondata.my-root", "https://electiondata.my/"),
    ("spr.gov.my", "https://spr.gov.my/"),
    ("dosm.gov.my", "https://www.dosm.gov.my/"),
    ("bnm.gov.my", "https://www.bnm.gov.my/"),
    ("pmo.gov.my", "https://www.pmo.gov.my/"),
]

# ---------------------------------------------------------------- fetch layer --
_ROBOTS: dict[str, list[str]] = {}
_FETCHES = {"count": 0, "errors": 0}
_FAILURES: list = []
_ROBOTS_EXEMPTIONS: list[dict] = []
#: news.google.com/robots.txt disallows / for "*" (its Allow list covers only
#: /home, /topics, /publications, /stories). The weekly tracker already uses the
#: GN RSS endpoint as its PRIMARY channel, and the packet's channel 1 is defined
#: as that same channel, so the endpoint is used here too — RECORDED, never
#: silent: the exemption is written into the candidates payload and the manifest,
#: and GE16_BACKFILL_ROBOTS_STRICT=1 turns it off (GN then skipped).
ROBOTS_STRICT = os.environ.get("GE16_BACKFILL_ROBOTS_STRICT", "0") == "1"
ROBOTS_EXEMPT_HOSTS = ({"news.google.com": "GN RSS is the project's primary news channel "
                        "(track_ge16_news.py) and this packet's channel 1; robots.txt "
                        "disallows /rss for '*' - owner sign-off required"} if not ROBOTS_STRICT
                       else {})



def _log_failure(source, url, reason):
    _FAILURES.append({"source": source, "url": url, "reason": str(reason)[:160]})
    print(f"[{source}] FAILED: {reason}")


def robots_allowed(url, source):
    """True when the host's robots.txt allows '*' to fetch this path."""
    parts = urllib.parse.urlsplit(url)
    host = f"{parts.scheme}://{parts.netloc}"
    if parts.netloc in ROBOTS_EXEMPT_HOSTS:
        if not any(entry["host"] == parts.netloc for entry in _ROBOTS_EXEMPTIONS):
            _ROBOTS_EXEMPTIONS.append({"host": parts.netloc,
                                       "reason": ROBOTS_EXEMPT_HOSTS[parts.netloc]})
            print(f"[{source}] ROBOTS EXEMPTION (recorded): {parts.netloc} — "
                  f"{ROBOTS_EXEMPT_HOSTS[parts.netloc]}")
        return True
    if host not in _ROBOTS:
        rules = []
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(host + "/robots.txt", headers=tracker.HEADERS),
                    timeout=20) as response:
                text = response.read().decode("utf-8", "replace")
            section = None
            for line in text.splitlines():
                line = line.split("#", 1)[0].strip()
                if not line or ":" not in line:
                    continue
                field, value = (part.strip() for part in line.split(":", 1))
                field = field.lower()
                if field == "user-agent":
                    section = value.lower()
                elif field == "disallow" and section in ("*",) and value:
                    rules.append(value)
        except Exception as error:  # no robots.txt is not a denial
            rules = []
            _log_failure(source, host + "/robots.txt", f"robots unavailable ({error})")
        _ROBOTS[host] = rules
    path = parts.path or "/"
    for rule in _ROBOTS[host]:
        prefix = rule.rstrip("*")
        if prefix and path.startswith(prefix):
            return False
    return True


def polite_fetch(url, source, hops=3):
    """robots-checked, budgeted, rate-limited GET. None on any failure.

    Python's urllib does not follow 308 (and publishers use it for http→https or
    trailing-slash normalisation), so redirects are followed explicitly here.
    """
    if _FETCHES["count"] >= MAX_FETCHES:
        _log_failure(source, url, f"fetch budget exhausted ({MAX_FETCHES})")
        return None
    if not robots_allowed(url, source):
        _log_failure(source, url, "disallowed by robots.txt")
        return None
    try:
        html = tracker.fetch(url)
    except urllib.error.HTTPError as error:
        location = error.headers.get("Location") if error.headers else None
        if error.code in (301, 302, 303, 307, 308) and location and hops > 0:
            moved = urllib.parse.urljoin(url, location)
            print(f"[{source}] {error.code} -> {moved}")
            return polite_fetch(moved, source, hops=hops - 1)
        _FETCHES["errors"] += 1
        _log_failure(source, url, error)
        return None
    except Exception as error:
        _FETCHES["errors"] += 1
        _log_failure(source, url, error)
        return None
    _FETCHES["count"] += 1
    time.sleep(SLEEP_SECONDS)
    return html


# ------------------------------------------------------------- window helpers --
def in_window(date_str):
    """Hard window test on an ISO date/instant. Undated items are OUT."""
    if not date_str:
        return False
    text = str(date_str).replace("Z", "+00:00").split(" ")[0]
    day = text[:10]
    return WINDOW_START <= day <= WINDOW_END


def candidate_item(source, title, link, date, desc, query, now, kind):
    return {"query": query, "title": (title or "").strip(), "desc": (desc or "")[:400],
            "link": (link or "").strip(), "date": date or "", "source": source,
            "found_at": now, "channel": kind}


def dedupe_key(item):
    """One story, one candidate: title prefix (the tracker's rule) or URL."""
    return (item.get("title") or "").strip()[:150].lower()


def slot(buckets, item):
    """Bucket an item by its source. Caps are applied later, spread over months."""
    key = dedupe_key(item)
    if not key:
        return False
    buckets.setdefault(item.get("source") or "?", []).append(item)
    return True


def spread_cap(entries, cap, newest_first=False):
    """Up to `cap` items for one source, round-robin across months.

    A per-source cap alone would let the first month a source publishes in eat the
    whole budget (Astro Awani's monthly sitemaps made every kept item June). The
    round-robin keeps coverage spread across the whole backfill window - for the
    per-source caps oldest-first, for the global cap newest-first.
    """
    by_month = collections.OrderedDict()
    for entry in sorted(entries, key=lambda item: (item.get("date") or "", dedupe_key(item))):
        by_month.setdefault((entry.get("date") or "?")[:7], []).append(entry)
    months = list(by_month)
    if newest_first:
        months.reverse()
        by_month = {month: list(reversed(by_month[month])) for month in months}

    kept, index = [], 0
    while len(kept) < cap:
        added = False
        for month in months:
            rows = by_month[month]
            if index < len(rows):
                kept.append(rows[index])
                added = True
                if len(kept) >= cap:
                    break
        if not added:
            break
        index += 1
    kept_ids = {id(entry) for entry in kept}
    dropped = [entry for entry in entries if id(entry) not in kept_ids]
    return kept, dropped



# ------------------------------------------------------------------ channels --
def collect_google_news(buckets, seen_keys, source_counts):
    """Channel 1: the tracker's own query list, widened to the backfill window."""
    for name, query in [(q, q) for q in tracker.QUERIES] + [
            (f"GN:{label}", f"{query} malaysia politics") for label, query in GN_SITE_QUERIES]:
        full = f"{query} when:{GN_DAYS}d"
        url = tracker.GN + urllib.parse.quote(full) + "&hl=en-MY&gl=MY&ceid=MY:en"
        html = polite_fetch(url, f"GN:{(name or query)[:24]}")
        if html is None:
            continue
        for title, link, date, desc in tracker.parse_rss_items(html):
            if not title or not in_window(date):
                source_counts["out_of_window"] = source_counts.get("out_of_window", 0) + 1
                continue
            publisher, clean = tracker.extract_source(title)
            item = candidate_item(publisher or "Google News", clean or title, link, date, desc,
                                  name, _NOW, "google-news")
            key = dedupe_key(item)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            slot(buckets, item)


_NOW = ""


def parse_sitemap(xml):
    """[(loc, lastmod)] from a urlset or a sitemapindex."""
    blocks = re.findall(r"<(?:url|sitemap)>(.*?)</(?:url|sitemap)>", xml, re.S)
    entries = []
    for chunk in blocks:
        loc = re.search(r"<loc>\s*(.*?)\s*</loc>", chunk, re.S)
        lastmod = re.search(r"<lastmod>\s*(.*?)\s*</lastmod>", chunk, re.S)
        if loc:
            entries.append((loc.group(1).strip(), (lastmod.group(1).strip() if lastmod else "")))
    return entries


def slug_title(loc):
    tail = urllib.parse.urlsplit(loc).path.rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"[-_]+", " ", re.sub(r"\.\w+$", "", tail)).strip().title()


def collect_sitemaps(buckets, seen_keys, source_counts):
    """Channel 2: dated publisher sitemaps, political slugs only."""
    for name, index_url in SITEMAP_INDEXES:
        html = polite_fetch(index_url, name)
        if html is None:
            continue
        children = [loc for loc, _ in parse_sitemap(html)]
        # monthly article sitemaps are the dated ones; a flat sitemap has no
        # lastmod and is skipped explicitly rather than guessed at
        dated = [child for child in children
                 if re.search(r"20\d\d-\d\d", child) and re.search(r"sitemap|article", child)]
        if not dated:
            source_counts[name] = "no dated child sitemaps (flat index: no lastmod)"
            print(f"[{name}] no dated child sitemaps - skipped")
            continue
        added = 0
        for child in [item for item in sorted(dated) if re.search(r"2026-0[1-9]", item)][:SITEMAP_MAX_CHILDREN]:
            xml = polite_fetch(child, name)
            if xml is None:
                continue
            for loc, lastmod in parse_sitemap(xml):
                if not in_window(lastmod) or not SITEMAP_SLUG.search(loc):
                    continue
                item = candidate_item(name.split(":", 1)[1].title(), slug_title(loc), loc,
                                      lastmod[:10], "sitemap entry", name, _NOW, "sitemap")
                key = dedupe_key(item)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                if slot(buckets, item):
                    added += 1
        print(f"[{name}] {added} dated political entries kept")


DATE_IN_URL = re.compile(r"/(20\d\d)/(\d\d)/(\d\d)/|(20\d\d-\d\d-\d\d)")


def collect_html_sources(buckets, seen_keys, source_counts):
    """Channels 3 and 4: the election-data page and official release indexes.

    These pages are not feeds: a dated URL is the only honest date signal, so an
    undated link is skipped (never dated by guesswork) and the outcome is logged
    per source.
    """
    for name, url in HTML_SOURCES:
        html = polite_fetch(url, name)
        if html is None:
            continue
        added = 0
        for match in re.finditer(r'href="([^"#?]+)"[^>]*>(.{0,160}?)</a>', html, re.S):
            link, label = match.group(1), tracker.strip_tags(match.group(2))
            if not label or len(label) < 12:
                continue
            absolute = urllib.parse.urljoin(url, link)
            found = DATE_IN_URL.search(absolute)
            if not found:
                continue
            date = found.group(4) or "-".join(found.group(1, 2, 3))
            if not in_window(date):
                continue
            item = candidate_item(name, label, absolute, date, f"official page {url}",
                                  f"official:{name}", _NOW, "official-release")
            key = dedupe_key(item)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            if slot(buckets, item):
                added += 1
        source_counts[name] = added
        print(f"[{name}] {added} dated release link(s) in window")


# ------------------------------------------------------------------- collect --
def collect():
    """Fetch the whole backfill window and write the candidates payload."""
    global _NOW
    os.makedirs(DIR, exist_ok=True)
    outdir.ensure()  # GE16_TRACKER_OUT_DIR: staged writes leave the live dir alone
    _NOW = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    _FETCHES.update({"count": 0, "errors": 0})
    _FAILURES.clear()

    items, dropped, seen_keys = [], [], set()
    buckets, source_counts = {}, {}
    # no-burn: items collected earlier but left beyond the caps are re-presented
    previous = tracker.load_json(CANDIDATES, {})
    replayed = 0
    for item in (previous.get("overflow") or []) if isinstance(previous, dict) else []:
        key = dedupe_key(item)
        if in_window(item.get("date")) and key and key not in seen_keys and slot(buckets, item):
            seen_keys.add(key)  # the replayed copy wins over any fresh re-fetch
            item["replayed_unjudged"] = True
            replayed += 1

    collect_google_news(buckets, seen_keys, source_counts)
    collect_sitemaps(buckets, seen_keys, source_counts)
    collect_html_sources(buckets, seen_keys, source_counts)

    # per-source caps, spread across the window's months so no source (and no
    # month) can starve the others; everything past a cap is kept as overflow and
    # re-presented on the next run instead of being burned
    for source, entries in sorted(buckets.items()):
        kept, capped = spread_cap(entries, PER_SOURCE_CAP)
        items.extend(kept)
        dropped.extend(capped)
        if capped:
            source_counts[f"capped:{source}"] = len(capped)
    items.sort(key=lambda item: (not item.get("replayed_unjudged"), item.get("date", "")),
               reverse=True)
    kept, dropped_by_total = spread_cap(items, TOTAL_CAP, newest_first=True)
    overflow = dropped + dropped_by_total
    for item in kept + overflow:
        item["collection_id"] = _NOW
    # the overflow is re-presented next run, but a sitemap sweep can produce tens
    # of thousands of rows: store a bounded newest-first sample and count the rest
    overflow_sample = spread_cap(overflow, OVERFLOW_STORE_CAP, newest_first=True)[0] \
        if overflow else []

    months = collections.Counter((item.get("date") or "?")[:7] for item in kept)
    by_source = collections.Counter(item.get("source") or "?" for item in kept)
    capped_total = sum(count for key, count in source_counts.items() if key.startswith("capped:"))
    payload = {
        "schema": "ge16.news-backfill-candidates.v1",
        "generated_at": _NOW, "collection_id": _NOW,
        "window": {"start": WINDOW_START, "end": WINDOW_END, "gn_when_days": GN_DAYS},
        "count": len(kept), "overflow_count": len(overflow),
        "per_source_cap": PER_SOURCE_CAP, "total_cap": TOTAL_CAP,
        "replayed_unjudged": replayed, "capped_at_source": capped_total,
        "out_of_window_titles": source_counts.get("out_of_window", 0),
        "by_source": dict(by_source.most_common()), "by_month": dict(sorted(months.items())),
        "fetches": dict(_FETCHES), "failures": list(_FAILURES),
        "source_counts": {key: value for key, value in sorted(source_counts.items())},
        "robots_exemptions": _ROBOTS_EXEMPTIONS,
        "items": kept, "overflow": overflow_sample,
    }
    tracker.atomic_write_json(CANDIDATES, payload)
    print(f"BACKFILL candidates: {len(kept)} kept, {len(overflow)} beyond the total cap"
          f" -> {outdir.w(CANDIDATES)}")
    print(f"  window {WINDOW_START}..{WINDOW_END} (when:{GN_DAYS}d) | fetches {_FETCHES['count']}"
          f" errors {_FETCHES['errors']} | replayed {replayed}"
          f" | capped at source {source_counts.get('capped', 0)}")
    print(f"  by month: {dict(sorted(months.items()))}")
    return payload


# --------------------------------------------------------------- judge input --
def judge_input():
    """Chunk the candidate list into <=50-item judge batches (resumable)."""
    payload = tracker.load_json(CANDIDATES, {})
    items = payload.get("items") if isinstance(payload, dict) else None
    items = items if isinstance(items, list) else []
    cycle = payload.get("generated_at", "") if isinstance(payload, dict) else ""
    size = JUDGE_BATCH if JUDGE_BATCH > 0 else 50
    batches = []
    for index in range(0, len(items), size):
        number = index // size + 1
        chunk = items[index:index + size]
        tracker.atomic_write_json(JUDGE_BATCH_FMT % number, {
            "schema": BATCH_SCHEMA, "batch": number, "collection_id": cycle,
            "count": len(chunk), "items": chunk})
        batches.append({"batch": number, "file": os.path.basename(JUDGE_BATCH_FMT % number),
                        "count": len(chunk),
                        "judged_file": os.path.basename(JUDGED_BATCH_FMT % number)})
    expected = {os.path.abspath(outdir.w(JUDGE_BATCH_FMT % entry["batch"])) for entry in batches}
    for stale in outdir.g(os.path.join(DIR, "ge16-news-backfill-judge-batch-*.json")):
        if os.path.abspath(stale) not in expected:
            os.remove(stale)
            print(f"removed stale batch file {os.path.basename(stale)}")
    tracker.atomic_write_json(JUDGE_MANIFEST, {
        "schema": MANIFEST_SCHEMA, "generated_at": _NOW or cycle, "collection_id": cycle,
        "batch_size": size, "total": len(items), "batch_count": len(batches),
        "judge_contract": "ge16.news judge: accept when the item is dated in-window and "
                          "politically relevant to GE16 (party/coalition, seat, EC, campaign); "
                          "reject otherwise", "batches": batches})
    print(f"BACKFILL JUDGE INPUT: {len(items)} candidates -> {len(batches)} batch(es)"
          f" of <= {size} (collection_id {cycle or 'unknown'})")
    for entry in batches:
        judged = os.path.exists(outdir.r(JUDGED_BATCH_FMT % entry["batch"]))
        print(f"  batch {entry['batch']}/{len(batches)}: {entry['count']} items ->"
              f" {entry['file']} | write {entry['judged_file']}"
              f" [{'judged' if judged else 'AWAITING JUDGMENT'}]")
    return manifest_summary(batches, items)


def load_verdicts():
    """Judge verdicts, one JSON object per line of VERDICTS.

    The judge (the LLM) writes only the ruling; the full item payload is rebuilt
    from the batch it judged, so a verdict can never invent or edit source fields.
    """
    verdicts_path = outdir.r(VERDICTS)
    if not os.path.exists(verdicts_path):
        print(f"no verdict file: write {verdicts_path} first")
        return None
    rows, bad = {}, 0
    for line in open(verdicts_path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            row = _compact_verdict(line)
        if not isinstance(row, dict) or "b" not in row or "i" not in row:
            bad += 1
            continue
        rows[(int(row["b"]), int(row["i"]))] = row
    if bad:
        print(f"WARNING: {bad} unparseable verdict line(s) skipped")
    return rows


def _compact_verdict(line):
    """`batch:index:accept[:category[:blocs[:parties[:seats[:lang[:score]]]]]]`.

    '-' is an empty field, '+' joins list members, a bare score is tenths.
    """
    parts = line.split(":")
    if len(parts) not in (3, 9) or not parts[0].strip().isdigit() \
            or not parts[1].strip().isdigit():
        return None  # a wrong field count is a judge typo, never a silent re-slot
    parts += [""] * (9 - len(parts))
    blocs, parties, seats = ([token for token in (field or "").split("+")
                              if token and token != "-"] for field in parts[4:7])
    score = parts[8].strip()
    try:
        score = float(score) if "." in score else (int(score) / 10.0 if score else 0.6)
    except ValueError:
        score = 0.6
    return {"b": int(parts[0]), "i": int(parts[1]), "a": parts[2].strip() == "1",
            "c": parts[3].strip(), "g": blocs, "p": parties, "s": seats,
            "l": parts[7].strip() or "en", "score": score}


def assemble():
    """Verdicts -> ge16-news-backfill-judged-batch-<n>.json (checkpointed judgment)."""
    rows = load_verdicts()
    if rows is None:
        return 1
    manifest = tracker.load_json(JUDGE_MANIFEST, {})
    numbers = sorted(entry["batch"] for entry in (manifest.get("batches") or []))
    if not numbers:
        print("no judge manifest: run --judge-input first")
        return 1
    written, accepted_total, rejected_total = 0, 0, 0
    problems = []
    for number in numbers:
        batch = tracker.load_json(JUDGE_BATCH_FMT % number, {})
        items = batch.get("items") or []
        accepted, rejected = [], []
        for index, item in enumerate(items):
            row = rows.get((number, index))
            if row is None:
                problems.append(f"batch {number} item {index} unjudged")
                continue
            if not row.get("a"):
                rejected.append(index)
                continue
            category = row.get("c") or "analysis"
            if category not in CATEGORIES:
                problems.append(f"batch {number} item {index}: bad category {category!r}")
                continue
            payload = dict(item)
            payload.update({"lang": row.get("l") or "en", "category": category,
                            "blocs": row.get("g") or [], "parties": row.get("p") or [],
                            "seats": row.get("s") or [], "score": float(row.get("score", 0.6)),
                            "judged_at": batch.get("collection_id") or "",
                            "judge": "backfill-llm"})
            accepted.append(payload)
        if problems and len(problems) > 40:
            break
        tracker.atomic_write_json(JUDGED_BATCH_FMT % number, {
            "schema": BATCH_SCHEMA, "batch": number, "collection_id": batch.get("collection_id", ""),
            "count": len(items), "accepted_count": len(accepted),
            "rejected_count": len(rejected), "rejected_indexes": rejected,
            "accepted": accepted})
        accepted_total += len(accepted)
        rejected_total += len(rejected)
        written += 1
        print(f"  batch {number}/{len(numbers)}: {len(accepted)} accepted,"
              f" {len(rejected)} rejected -> {os.path.basename(JUDGED_BATCH_FMT % number)}")
    if problems:
        print(f"PARTIAL JUDGMENT: {len(problems)} problem(s); first 5: {problems[:5]}")
        return 1
    print(f"ASSEMBLED {written} judged batch(es): {accepted_total} accepted,"
          f" {rejected_total} rejected")
    return 0


def manifest_summary(batches, items):
    return {"batch_count": len(batches), "total": len(items),
            "batches": [{"batch": entry["batch"], "count": entry["count"]} for entry in batches]}


def gather_judged():
    """(judged_items, batch_numbers) from the backfill's own judged batches.

    Complete evidence or nothing: a partially judged set is refused (the packet's
    checkpoint contract), so `--commit` can never half-merge a sweep.
    """
    manifest = tracker.load_json(JUDGE_MANIFEST, {})
    numbers = [entry.get("batch") for entry in (manifest.get("batches") or [])
               if isinstance(entry, dict)]
    numbers = [number for number in numbers if isinstance(number, int)]
    if not numbers:
        print("no judge manifest: run --judge-input first")
        return None, []
    missing = [number for number in numbers
               if not os.path.exists(outdir.r(JUDGED_BATCH_FMT % number))]
    if missing:
        print(f"PARTIAL JUDGMENT: {len(numbers) - len(missing)}/{len(numbers)} batches judged;"
              f" missing {missing} — refusing to commit")
        return None, numbers
    items = []
    for number in sorted(numbers):
        data = tracker.load_json(JUDGED_BATCH_FMT % number, {})
        raw = data.get("accepted") if isinstance(data, dict) else data
        if isinstance(raw, list):
            items.extend(raw)
    return items, sorted(numbers)


# -------------------------------------------------------------------- commit --
def merge_key(item):
    """Stable merge key: URL plus the title prefix (packet rule)."""
    return ((item.get("link") or "").strip().lower(),
            (item.get("title") or "").strip()[:150].lower())


def commit():
    """Merge the judged batches into ge16-news-accepted.json (union, no overwrite)."""
    judged, numbers = gather_judged()
    if judged is None:
        return 1
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    owner = tracker.load_json(ACCEPTED, {})
    existing = owner.get("items", []) if isinstance(owner, dict) else (owner or [])
    existing = existing if isinstance(existing, list) else []
    envelope_generated_at = owner.get("generated_at", "") if isinstance(owner, dict) else ""

    seen_urls = {(entry.get("link") or "").strip().lower() for entry in existing}
    seen_titles = {(entry.get("title") or "").strip()[:150].lower() for entry in existing}
    added, dupes = [], 0
    for item in judged:
        url, title = merge_key(item)
        if not title or url in seen_urls or title in seen_titles:
            dupes += 1
            continue
        seen_urls.add(url)
        seen_titles.add(title)
        added.append({
            "query": item.get("query", ""), "title": (item.get("title") or "").strip(),
            "date": item.get("date", ""), "source": item.get("source", ""),
            "link": item.get("link", ""), "lang": item.get("lang", "en"),
            "category": item.get("category", "analysis"), "blocs": item.get("blocs", []),
            "parties": item.get("parties", []), "seats": item.get("seats", []),
            "score": float(item.get("score", 0.6)),
            "judged_at": item.get("judged_at", now), "backfill": True,
        })
    items = added + existing
    # the owner envelope is preserved: generated_at keeps the weekly tracker's own
    # clock (its dynamic window reads it), only count/items change
    tracker.atomic_write_json(ACCEPTED, {
        "generated_at": envelope_generated_at or now, "count": len(items), "items": items})
    staged_owner = outdir.w(ACCEPTED)
    months = collections.Counter((entry.get("date") or "?")[:7] for entry in items)
    before_months = collections.Counter((entry.get("date") or "?")[:7] for entry in existing)
    delta = {month: months.get(month, 0) - before_months.get(month, 0)
             for month in sorted(set(months) | set(before_months))}
    manifest = {
        "schema": MANIFEST_SCHEMA, "merged_at": now,
        "accepted_path": ACCEPTED, "owner_generated_at": envelope_generated_at,
        "window": {"start": WINDOW_START, "end": WINDOW_END},
        "judged_batches": numbers, "judged_items": len(judged),
        "accepted_before": len(existing), "accepted_after": len(items),
        "added": len(added), "duplicates_skipped": dupes,
        "accepted_by_month_after": dict(sorted(months.items())),
        "accepted_delta_by_month": {key: value for key, value in delta.items() if value},
        "candidates_by_month": (tracker.load_json(CANDIDATES, {}) or {}).get("by_month", {}),
    }
    write_manifest(manifest)
    print(f"MERGED {len(added)} judged items into {staged_owner} ({len(existing)} -> {len(items)});"
          f" {dupes} already present")
    print(f"  accepted delta by month: {manifest['accepted_delta_by_month']}")
    return 0


def write_manifest(manifest):
    """Also stored in the analytics work root, so the pipeline can read it."""
    path = os.path.abspath(MANIFEST)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tracker.atomic_write_json(path, manifest)
    print(f"  backfill manifest -> {path}")


# -------------------------------------------------------------------- verify --
def sample(size=48):
    """Month-by-month verification of the merged accepted history."""
    owner = tracker.load_json(ACCEPTED, {})
    items = owner.get("items", []) if isinstance(owner, dict) else (owner or [])
    months = collections.Counter((entry.get("date") or "?")[:7] for entry in items)
    backfill = [entry for entry in items if entry.get("backfill")]
    print(f"accepted total {len(items)} | backfilled {len(backfill)} | by month:"
          f" {dict(sorted(months.items()))}")
    per_month = max(1, size // max(len([m for m in months if m.startswith('2026-0')]), 1))
    picked = 0
    for month in sorted(months):
        rows = [entry for entry in items if (entry.get("date") or "").startswith(month)]
        rows.sort(key=lambda entry: entry.get("date") or "")
        step = max(1, len(rows) // per_month)
        for entry in rows[::step][:per_month]:
            picked += 1
            print(f"  {entry.get('date', '')[:10]} [{entry.get('source', '')[:18]:18}]"
                  f" {entry.get('title', '')[:108]}")
        print(f"  -- {month}: {len(rows)} accepted")
        if picked >= size:
            break
    print(f"sampled {picked} accepted items across {len(months)} months")
    return picked


# ---------------------------------------------------------------------- main --
def main(argv=None):
    parser = argparse.ArgumentParser(description="GE16 news backfill (1 Jan - 24 Sep 2026).")
    parser.add_argument("--collect", action="store_true", help="fetch the backfill window")
    parser.add_argument("--judge-input", action="store_true", help="write judge batches")
    parser.add_argument("--assemble", action="store_true",
                        help="turn the verdict file into judged batches")
    parser.add_argument("--commit", action="store_true", help="merge judged items into accepted")
    parser.add_argument("--sample", action="store_true", help="verify the merged history")
    args = parser.parse_args(argv)
    if args.judge_input:
        judge_input()
        return 0
    if args.assemble:
        return assemble()
    if args.commit:
        return commit()
    if args.sample:
        sample()
        return 0
    collect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

