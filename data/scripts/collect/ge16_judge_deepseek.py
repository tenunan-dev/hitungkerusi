#!/usr/bin/env python3
"""GE16 news judge — the deepseek judge backend for the collector batch contracts.

Both news collectors split their candidate list into <=50-item judge batches and
expect an LLM to write the *judged* side of every batch:

    weekly tracker (track_ge16_news.py)
        ge16-news-judge-batch-<n>.json   -> ge16-news-judged-batch-<n>.json
        (+ optional consolidated ge16-news-judged.json)

    archive backfill (ge16_news_backfill.py)
        ge16-news-backfill-judge-batch-<n>.json
        -> ge16-news-backfill-judged-batch-<n>.json

This module IS that judge. It performs the judgment itself with OpenAI-compatible
chat.completions calls against DeepSeek and writes the judged batch files, so the
collectors' ``--commit`` step keeps working **byte-compatibly**: commit reads only
the ``accepted`` list of each judged file, so the provenance keys this module adds
(``source_sha256``, ``judged_by``, ``judge_model``) are ignored by it.

Contract guarantees (fail-closed):
  * a batch is never larger than ``MAX_BATCH_ITEMS`` (50) — an oversized batch file
    is refused loudly instead of being sent;
  * model, base URL and API key come from the environment ONLY
    (``DEEPSEEK_MODEL`` / ``DEEPSEEK_BASE_URL`` / ``DEEPSEEK_API_KEY``); the key is
    never printed, logged, or embedded in an error message;
  * 429/5xx/timeouts are retried with exponential backoff;
  * every reply passes a JSON guard and a schema check; a reply that omits any
    batch index is retried once with a repair instruction and, failing that, the
    BATCH FAILS — nothing partial is ever written, so ``--commit`` can only ever
    see complete, schema-valid evidence;
  * each judged file records the sha256 of the judge batch it judged, so a stale
    judged file (left by an earlier collection) is re-judged instead of silently
    merging an older ruling onto today's candidates.

Usage:
  python3 scripts/collect/ge16_judge_deepseek.py judge-batches \
      --pipeline backfill [--manifest PATH] [--batch-dir DIR] [--force]
      [--consolidate PATH] [--stats PATH] [--max-items 50] [--model NAME]

  python3 scripts/collect/ge16_judge_deepseek.py judge-file \
      --batch PATH --out PATH [--consolidate PATH]
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import http.client
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# ---------------------------------------------------------------- configuration --
API_KEY_ENV = "DEEPSEEK_API_KEY"
BASE_URL_ENV = "DEEPSEEK_BASE_URL"
MODEL_ENV = "DEEPSEEK_MODEL"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
MODEL_FALLBACK = "deepseek-chat"

#: hard ceiling shared with the collectors' judge contract (GE16_JUDGE_BATCH=50)
MAX_BATCH_ITEMS = 50
#: retryable HTTP statuses
RETRY_STATUSES = (408, 409, 425, 429, 500, 502, 503, 504)
DEFAULT_TIMEOUT = 120.0
DEFAULT_MAX_RETRIES = 5
BACKOFF_CAP = 30.0
#: extra calls allowed when the model omits indexes (completeness repair)
DEFAULT_REPAIR_ATTEMPTS = 2

LANGUAGES = ("en", "ms")
DEFAULT_CATEGORY = "analysis"
DEFAULT_SCORE = 0.6

#: the judge's ruling vocabulary, taken from the accepted history's own categories
CATEGORY_FALLBACK = (
    "coalition", "election", "analysis", "policy", "candidate", "legal", "poll",
    "redelineation", "seat-allocation", "election-integrity", "campaign",
    "election-timing", "state-election", "fiscal-federal",
)

#: tracker/backfill filenames, resolved relative to a tracker directory
PIPELINES = {
    "tracker": {
        "manifest": "ge16-news-judge-manifest.json",
        "batch_fmt": "ge16-news-judge-batch-%d.json",
        "judged_fmt": "ge16-news-judged-batch-%d.json",
        "consolidate": "ge16-news-judged.json",
    },
    "backfill": {
        "manifest": "ge16-news-backfill-judge-manifest.json",
        "batch_fmt": "ge16-news-backfill-judge-batch-%d.json",
        "judged_fmt": "ge16-news-backfill-judged-batch-%d.json",
        "consolidate": None,
    },
}


class JudgeError(RuntimeError):
    """The batch could not be judged; nothing was written."""


class MissingCredentialError(JudgeError):
    """DEEPSEEK_API_KEY is not in the environment."""


class IncompleteVerdictsError(JudgeError):
    """The model did not rule on every index in the batch."""


class OversizedBatchError(JudgeError):
    """A judge batch carries more items than the contract allows."""


# ------------------------------------------------------------------------- key --
def api_key() -> str:
    """The API key from the environment. Never logged, never echoed."""
    key = (os.environ.get(API_KEY_ENV) or "").strip()
    if not key:
        raise MissingCredentialError(
            f"{API_KEY_ENV} is not set: the deepseek judge backend reads its "
            "credential from the environment only")
    return key


def base_url() -> str:
    return (os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL).rstrip("/")


def default_model() -> str:
    return (os.environ.get(MODEL_ENV) or DEFAULT_MODEL).strip() or DEFAULT_MODEL


def redact(text, key: str | None = None) -> str:
    """Never let a credential reach a log line or an exception message."""
    text = str(text)
    secret = key or os.environ.get(API_KEY_ENV) or ""
    if secret and secret in text:
        return text.replace(secret, "***")
    return text


def category_vocabulary() -> tuple:
    """Ruling vocabulary: the backfill's declared categories plus whatever the
    accepted history already uses. Falls back to the static list."""
    values = set()
    try:  # the backfill module owns the declared vocabulary
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import ge16_news_backfill as backfill  # noqa: PLC0415
        values.update(backfill.CATEGORIES)
    except Exception:
        pass
    values.update(CATEGORY_FALLBACK)
    return tuple(sorted(values))


# ------------------------------------------------------------------- transport --
def _urlopen(request, timeout):  # pragma: no cover - thin indirection for tests
    return urllib.request.urlopen(request, timeout=timeout)


class ChatClient:
    """Minimal OpenAI-compatible chat.completions client for DeepSeek.

    Retries 429/5xx/timeouts with exponential backoff. ``sleep`` is injectable so
    tests never wait, and the API key is only ever placed in the Authorization
    header.
    """

    def __init__(self, model=None, base=None, key=None, timeout=DEFAULT_TIMEOUT,
                 max_retries=DEFAULT_MAX_RETRIES, opener=None, sleep=None):
        self._model = model
        self._base = base
        self._key = key
        self.timeout = timeout
        self.max_retries = max_retries
        self._opener = opener or _urlopen
        self._sleep = sleep or time.sleep
        self.stats = {"calls": 0, "retries": 0, "http_errors": 0, "failures": 0}

    @property
    def model(self) -> str:
        return self._model or default_model()

    @property
    def base(self) -> str:
        return (self._base or base_url()).rstrip("/")

    def _credential(self) -> str:
        return self._key or api_key()

    def complete(self, messages, temperature=0.0, response_format=None) -> str:
        """One chat completion; returns the assistant content string."""
        payload = {"model": self.model, "messages": messages, "temperature": temperature,
                   "stream": False}
        if response_format:
            payload["response_format"] = response_format
        body = json.dumps(payload).encode("utf-8")
        url = f"{self.base}/chat/completions"
        key = self._credential()
        attempt = 0
        last = "unknown error"
        while attempt <= self.max_retries:
            request = urllib.request.Request(
                url, data=body, method="POST",
                headers={"Content-Type": "application/json",
                         "Accept": "application/json",
                         "Authorization": f"Bearer {key}",
                         "User-Agent": "GE16-Judge/1.0"})
            self.stats["calls"] += 1
            try:
                with self._opener(request, self.timeout) as response:
                    raw = response.read().decode("utf-8", errors="replace")
                return _content_of(raw)
            except urllib.error.HTTPError as error:
                status = getattr(error, "code", 0)
                detail = ""
                try:
                    detail = error.read().decode("utf-8", errors="replace")[:400]
                except Exception:
                    detail = ""
                self.stats["http_errors"] += 1
                last = f"HTTP {status}: {redact(detail, key)}"
                if status not in RETRY_STATUSES or attempt >= self.max_retries:
                    break
            except (urllib.error.URLError, socket.timeout, http.client.HTTPException,
                    TimeoutError, ConnectionError) as error:
                last = f"{type(error).__name__}: {redact(error, key)}"
                if attempt >= self.max_retries:
                    break
            except JudgeError:
                raise
            except Exception as error:  # malformed body, decoding, ...
                last = f"{type(error).__name__}: {redact(error, key)}"
                break
            attempt += 1
            self.stats["retries"] += 1
            self._sleep(min(2 ** attempt, BACKOFF_CAP))
        self.stats["failures"] += 1
        raise JudgeError(f"chat.completions failed after {attempt} retr(ies): {last}")


def _content_of(raw: str) -> str:
    try:
        payload = json.loads(raw)
    except ValueError as error:
        raise JudgeError(f"reply is not JSON: {error}") from error
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list) or not choices:
        raise JudgeError("reply carries no choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise JudgeError("reply carries no assistant content")
    return content


# ------------------------------------------------------------------- parsing ---
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str):
    """JSON guard: pull the first JSON object/array out of a model reply."""
    if text is None:
        raise JudgeError("empty reply")
    raw = text.strip()
    fenced = _FENCE.search(raw)
    if fenced:
        raw = fenced.group(1).strip()
    try:
        return json.loads(raw)
    except ValueError:
        pass
    # scan the candidate starts left to right so the outermost, earliest JSON
    # value wins (a reply that mentions a stray brace before the real payload
    # still parses), and balance braces/strings by hand.
    candidates = sorted(
        [(index, opener, closer) for opener, closer in (("{", "}"), ("[", "]"))
         for index in [raw.find(opener)] if index != -1],
        key=lambda entry: entry[0])
    for start, opener, closer in candidates:
        depth, in_string, escaped = 0, False, False
        for index in range(start, len(raw)):
            char = raw[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(raw[start:index + 1])
                    except ValueError:
                        break
    raise JudgeError("reply carries no parsable JSON")


def _as_list_of_strings(value) -> list:
    if isinstance(value, str):
        value = [part.strip() for part in re.split(r"[,;]", value) if part.strip()]
    if not isinstance(value, (list, tuple)):
        return []
    return [str(entry).strip() for entry in value if str(entry).strip()]


def normalize_verdict(verdict, index, vocabulary) -> tuple:
    """(accepted_entry or None, notes) for one raw verdict dict."""
    notes = {}
    if not isinstance(verdict, dict):
        return None, {"bad_shape": True}
    accept = verdict.get("accept", verdict.get("accepted", verdict.get("keep")))
    if isinstance(accept, str):
        accept = accept.strip().lower() in ("true", "yes", "1", "accept")
    if not bool(accept):
        return None, notes
    category = str(verdict.get("category") or "").strip().lower()
    if category not in vocabulary:
        if category:
            notes["category_coerced"] = category
        category = DEFAULT_CATEGORY if DEFAULT_CATEGORY in vocabulary else vocabulary[0]
    lang = str(verdict.get("lang") or verdict.get("language") or "en").strip().lower()
    if lang not in LANGUAGES:
        notes["lang_coerced"] = lang
        lang = "en"
    try:
        score = float(verdict.get("score", DEFAULT_SCORE))
    except (TypeError, ValueError):
        score = DEFAULT_SCORE
    score = min(max(score, 0.0), 1.0)
    entry = {
        "category": category,
        "blocs": _as_list_of_strings(verdict.get("blocs", verdict.get("bloc"))),
        "parties": _as_list_of_strings(verdict.get("parties", verdict.get("party"))),
        "seats": _as_list_of_strings(verdict.get("seats", verdict.get("seat"))),
        "lang": lang,
        "score": round(score, 3),
    }
    reason = verdict.get("reason") or verdict.get("rationale")
    if reason:
        entry["judge_reason"] = str(reason)[:280]
    return entry, notes


def verdict_index(verdict):
    for key in ("i", "index", "id", "n"):
        if isinstance(verdict, dict) and key in verdict:
            try:
                return int(verdict[key])
            except (TypeError, ValueError):
                continue
    return None


def parse_verdicts(text, vocabulary):
    """(by_index: {index: entry|None}, notes) — None marks an explicit reject."""
    payload = extract_json(text)
    if isinstance(payload, dict):
        for key in ("verdicts", "results", "items", "judgments"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        raise JudgeError("reply JSON is neither a list nor an object with 'verdicts'")
    by_index, notes = {}, {}
    for verdict in payload:
        index = verdict_index(verdict)
        if index is None:
            notes.setdefault("unindexed", 0)
            notes["unindexed"] += 1
            continue
        entry, extra = normalize_verdict(verdict, index, vocabulary)
        by_index[index] = entry
        for key, value in extra.items():
            if key in ("category_coerced", "lang_coerced"):
                notes.setdefault(key, [])
                notes[key].append(value)
            else:
                notes[key] = notes.get(key, 0) + 1 if isinstance(value, int) else True
    return by_index, notes


# -------------------------------------------------------------------- judging --
SYSTEM_PROMPT = (
    "You are the GE16 news judge for a Malaysian general-election research "
    "pipeline. For every numbered item you decide whether the headline and its "
    "description report Malaysian POLITICAL or ELECTION news (parties, "
    "coalitions, candidates, seats, Parliament/DUN, the Election Commission, "
    "campaigns, election law, polls, redelineation, government composition). "
    "Reject anything else: foreign news, sport, entertainment, crime without a "
    "political angle, business/market stories, lifestyle, weather. "
    "Classify accepted items with: category (one of {categories}), blocs "
    "(coalition codes such as PN, PH, BN, GPS, GRS, WARISAN), parties (party "
    "codes or names), seats (seat codes or names), lang (en or ms), score "
    "(0.0-1.0 relevance). Reply with JSON only: "
    '{{"verdicts": [{{"i": 1, "accept": true, "category": "coalition", '
    '"blocs": ["PN"], "parties": [], "seats": [], "lang": "en", "score": 0.8, '
    '"reason": "<=12 words"}}, ...]}} '
    "Rule on EVERY item index, in order, exactly once."
)


def item_brief(index, item):
    """The compact, judge-visible view of a candidate (never invented fields)."""
    desc = (item.get("desc") or item.get("description") or "") or ""
    desc = re.sub(r"<[^>]+>", " ", str(desc))
    desc = re.sub(r"\s+", " ", desc).strip()
    return {
        "i": index,
        "title": (item.get("title") or "")[:300],
        "date": str(item.get("date") or "")[:32],
        "source": str(item.get("source") or "")[:80],
        "query": str(item.get("query") or "")[:80],
        "desc": desc[:400],
    }


def build_messages(items, vocabulary, repair_missing=None):
    listing = json.dumps([item_brief(i + 1, item) for i, item in enumerate(items)],
                         ensure_ascii=False)
    user = ("Judge these candidate news items. Reply with JSON only, using this "
            f"exact shape: {{\"verdicts\": [...]}}.\n{listing}")
    if repair_missing:
        user += ("\n\nYour previous reply omitted indexes: "
                 f"{sorted(repair_missing)}. Rule on EVERY index, once each.")
    return [{"role": "system", "content": SYSTEM_PROMPT.format(
        categories=", ".join(vocabulary))},
            {"role": "user", "content": user}]


class DeepSeekJudge:
    """Judges batches of candidates through DeepSeek chat.completions."""

    def __init__(self, client=None, vocabulary=None, max_items=MAX_BATCH_ITEMS,
                 repair_attempts=DEFAULT_REPAIR_ATTEMPTS):
        self.client = client or ChatClient()
        self.vocabulary = tuple(vocabulary or category_vocabulary())
        self.max_items = max_items
        self.repair_attempts = repair_attempts

    @property
    def model(self):
        return self.client.model

    def judge_items(self, items):
        """dict with accepted entries + per-batch statistics. Fails closed."""
        items = list(items)
        if len(items) > self.max_items:
            raise OversizedBatchError(
                f"judge batch carries {len(items)} items; the contract allows "
                f"at most {self.max_items}")
        if not items:
            return {"accepted": [], "accepted_count": 0, "rejected_count": 0,
                    "rejected_indexes": [], "unjudged_indexes": [], "notes": {},
                    "completions": 0}
        by_index, notes, completions = {}, {}, 0
        missing = set()
        attempts = 0
        while True:
            # a transport failure (bad status, timeout, malformed envelope) is NOT
            # repaired here: the client already retried it, so it propagates and
            # the batch fails loud. Only a reply that parses badly or omits
            # indexes is re-asked.
            text = self.client.complete(
                build_messages(items, self.vocabulary, None if not by_index else missing),
                response_format={"type": "json_object"})
            completions += 1
            try:
                fresh, extra = parse_verdicts(text, self.vocabulary)
            except JudgeError:
                if attempts >= self.repair_attempts:
                    raise
                fresh, extra = {}, {}
            for key, value in extra.items():
                notes[key] = value
            # a repair may only ADD indexes: the first reply's rulings stand
            for index, entry in fresh.items():
                by_index.setdefault(index, entry)
            missing = {index for index in range(1, len(items) + 1) if index not in by_index}
            if not missing:
                break
            if attempts >= self.repair_attempts:
                raise IncompleteVerdictsError(
                    f"model omitted indexes {sorted(missing)[:10]} after "
                    f"{completions} completion(s): refusing to write a partial judgment")
            notes["repairs"] = notes.get("repairs", 0) + 1
            attempts += 1
        accepted, rejected = [], []
        for index in range(1, len(items) + 1):
            entry = by_index[index]
            if entry is None:
                rejected.append(index)
                continue
            accepted.append({**items[index - 1], **entry, "judged_by": "deepseek",
                             "judge_model": self.model})
        return {"accepted": accepted, "accepted_count": len(accepted),
                "rejected_count": len(rejected), "rejected_indexes": rejected,
                "unjudged_indexes": [], "notes": notes, "completions": completions}

    def _repair(self, items, missing, notes):
        """Deprecated shim kept out of the hot path (the loop handles repairs)."""
        text = self.client.complete(build_messages(items, self.vocabulary, missing),
                                    response_format={"type": "json_object"})
        by_index, extra = parse_verdicts(text, self.vocabulary)
        notes.update(extra)
        return by_index, notes


# -------------------------------------------------------------------- batches --
def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(os.fspath(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path, default=None):
    try:
        with open(os.fspath(path), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def atomic_write_json(path, payload):
    path = os.fspath(path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def batch_is_current(batch_path, judged_path) -> bool:
    """True when the judged file already judges THIS batch (same source sha)."""
    judged = load_json(judged_path, None)
    if not isinstance(judged, dict) or "accepted" not in judged:
        return False
    recorded = judged.get("source_sha256")
    if not recorded:
        return False  # unverifiable provenance (e.g. hand-written) -> re-judge
    return recorded == sha256_file(batch_path)


def judge_batch_file(batch_path, judged_path, judge, force=False):
    """Judge one batch file into its judged file. Returns a per-batch report."""
    batch = load_json(batch_path, None)
    if not isinstance(batch, dict) or not isinstance(batch.get("items"), list):
        raise JudgeError(f"{os.path.basename(batch_path)} is not a judge batch file")
    items = batch["items"]
    if len(items) > judge.max_items:
        raise OversizedBatchError(
            f"{os.path.basename(batch_path)} carries {len(items)} items; the "
            f"contract allows at most {judge.max_items}")
    if not force and batch_is_current(batch_path, judged_path):
        prior = load_json(judged_path, {}) or {}
        return {"batch": batch.get("batch"), "file": os.path.basename(batch_path),
                "status": "already-judged", "count": len(items),
                "accepted_count": prior.get("accepted_count", len(prior.get("accepted", []))),
                "calls": 0}
    started = time.time()
    outcome = judge.judge_items(items)
    payload = {
        "schema": batch.get("schema"),
        "batch": batch.get("batch"),
        "collection_id": batch.get("collection_id", ""),
        "count": len(items),
        "accepted_count": outcome["accepted_count"],
        "rejected_count": outcome["rejected_count"],
        "rejected_indexes": outcome["rejected_indexes"],
        "judged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "judged_by": "deepseek",
        "judge_model": judge.model,
        "source_sha256": sha256_file(batch_path),
        "accepted": outcome["accepted"],
    }
    atomic_write_json(judged_path, payload)
    return {"batch": batch.get("batch"), "file": os.path.basename(batch_path),
            "judged_file": os.path.basename(judged_path), "status": "judged",
            "count": len(items), "accepted_count": outcome["accepted_count"],
            "rejected_count": outcome["rejected_count"],
            "calls": outcome["completions"],
            "seconds": round(time.time() - started, 2)}


def consolidate(judged_paths, out_path, model):
    """Write the tracker's consolidated judged payload ({"accepted": [...]}, the
    only shape its commit reads when the consolidated file exists)."""
    items = []
    for path in judged_paths:
        payload = load_json(path, {}) or {}
        raw = payload.get("accepted")
        if isinstance(raw, list):
            items.extend(raw)
    atomic_write_json(out_path, {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "schema": "ge16.news-judged.v1", "count": len(items),
        "judged_by": "deepseek", "judge_model": model, "accepted": items})
    return len(items)


def judge_pipeline(pipeline, tracker_dir, judge, force=False, consolidate_path=None,
                   max_batches=None, log=print):
    """Judge every batch listed in a pipeline's judge manifest."""
    contract = PIPELINES[pipeline]
    manifest_path = os.path.join(tracker_dir, contract["manifest"])
    manifest = load_json(manifest_path, None)
    if not isinstance(manifest, dict):
        raise JudgeError(f"no judge manifest at {manifest_path}: run --judge-input first")
    entries = [entry for entry in (manifest.get("batches") or [])
               if isinstance(entry, dict) and isinstance(entry.get("batch"), int)]
    if not entries:
        raise JudgeError(f"{os.path.basename(manifest_path)} lists no batches")
    if max_batches is not None:
        entries = entries[:max_batches]
    reports, judged_paths = [], []
    for entry in sorted(entries, key=lambda item: item["batch"]):
        number = entry["batch"]
        batch_path = os.path.join(tracker_dir, contract["batch_fmt"] % number)
        judged_path = os.path.join(tracker_dir, contract["judged_fmt"] % number)
        if not os.path.exists(batch_path):
            raise JudgeError(f"judge batch file missing: {batch_path}")
        report = judge_batch_file(batch_path, judged_path, judge, force=force)
        reports.append(report)
        judged_paths.append(judged_path)
        log(f"  [judge] batch {number}: {report['status']} "
            f"({report.get('accepted_count', 0)}/{report['count']} accepted, "
            f"{report.get('calls', 0)} call(s))")
    consolidated = None
    if consolidate_path:
        consolidated = consolidate(judged_paths, consolidate_path, judge.model)
        log(f"  [judge] consolidated {consolidated} accepted -> "
            f"{os.path.basename(consolidate_path)}")
    return {"pipeline": pipeline, "model": judge.model,
            "batches": len(reports), "reports": reports,
            "consolidated": consolidated,
            "calls": sum(report.get("calls", 0) for report in reports),
            "judged": sum(1 for report in reports if report["status"] == "judged"),
            "already_judged": sum(1 for report in reports
                                  if report["status"] == "already-judged")}


# ------------------------------------------------------------------------ cli --
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="GE16 deepseek news judge backend.")
    sub = parser.add_subparsers(dest="command")

    batches = sub.add_parser("judge-batches", help="judge a pipeline's pending batches")
    batches.add_argument("--pipeline", choices=sorted(PIPELINES), required=True)
    batches.add_argument("--tracker-dir", default=None,
                         help="tracker directory (default data/canonical/research/trackers)")
    batches.add_argument("--force", action="store_true",
                         help="re-judge batches whose judged file is already current")
    batches.add_argument("--consolidate", default=None,
                         help="write the consolidated judged payload here")
    batches.add_argument("--no-consolidate", action="store_true")
    batches.add_argument("--max-batches", type=int, default=None)
    batches.add_argument("--stats", default=None, help="write judge stats JSON here")
    batches.add_argument("--model", default=None)
    batches.add_argument("--base-url", default=None)
    batches.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    batches.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    batches.add_argument("--max-items", type=int, default=MAX_BATCH_ITEMS)

    single = sub.add_parser("judge-file", help="judge one batch file")
    single.add_argument("--batch", required=True)
    single.add_argument("--out", required=True)
    single.add_argument("--consolidate", default=None)
    single.add_argument("--force", action="store_true")
    single.add_argument("--model", default=None)
    single.add_argument("--max-items", type=int, default=MAX_BATCH_ITEMS)

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    try:
        client = ChatClient(model=args.model, base=getattr(args, "base_url", None),
                            timeout=getattr(args, "timeout", DEFAULT_TIMEOUT),
                            max_retries=getattr(args, "max_retries", DEFAULT_MAX_RETRIES))
        judge = DeepSeekJudge(client=client, max_items=args.max_items)
        if args.command == "judge-file":
            report = judge_batch_file(args.batch, args.out, judge, force=args.force)
            if args.consolidate:
                consolidate([args.out], args.consolidate, judge.model)
            print(json.dumps({"judge_status": "ok", "report": report}, ensure_ascii=False))
            return 0
        tracker_dir = args.tracker_dir or _default_tracker_dir()
        contract = PIPELINES[args.pipeline]
        consolidate_path = None
        if not args.no_consolidate:
            consolidate_path = args.consolidate or (
                os.path.join(tracker_dir, contract["consolidate"])
                if contract["consolidate"] else None)
        summary = judge_pipeline(args.pipeline, tracker_dir, judge, force=args.force,
                                 consolidate_path=consolidate_path,
                                 max_batches=args.max_batches)
        stats = {**judge.client.stats, "model": judge.model, "pipeline": args.pipeline,
                 "batches": summary["batches"], "judged": summary["judged"],
                 "already_judged": summary["already_judged"],
                 "consolidated": summary["consolidated"]}
        if args.stats:
            atomic_write_json(args.stats, stats)
        print(json.dumps({"judge_status": "ok", "stats": stats,
                          "batches": summary["reports"]}, ensure_ascii=False))
        return 0
    except MissingCredentialError as error:
        print(f"JUDGE ERROR (credential): {error}", file=sys.stderr)
        return 3
    except JudgeError as error:
        print(f"JUDGE ERROR: {redact(error)}", file=sys.stderr)
        return 4


def _default_tracker_dir() -> str:
    import pathlib
    return str(pathlib.Path(__file__).resolve().parents[3] / "data" / "canonical" / "research" / "trackers")


if __name__ == "__main__":
    sys.exit(main())
