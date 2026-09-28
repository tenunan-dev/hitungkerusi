#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GE16 AI-authored report layer — dedicated, swappable writer model.

WHAT THIS IS
------------
A SECOND, clearly-labelled edition of the GE16 report, written by an LLM and
published beside (never over) the deterministic edition:

  03_REPORTS/ai/federal/GE16_Malaysia_General_Election_Report.md      (+ _MS.md)
  03_REPORTS/ai/states/DUN <State>/GE16_<State>_Report.md             (+ _MS.md)
  03_REPORTS/ai/**/archive/GE16-<as_of>/...                           (edition copy)

The deterministic builders (``report_builder.py``, ``state_report_builder.py``)
and their outputs under ``03_REPORTS/federal`` / ``03_REPORTS/states`` are NOT
touched by this module: they stay the authoritative, byte-reproducible edition.

HOW THE WRITER IS CONSTRAINED
-----------------------------
1. The writer never sees the raw inputs. It sees ONE section's slice of the
   reference pack (``reference_pack.py``) — every fact rendered as
   ``claim_id | label | value`` — plus an instruction to copy values verbatim.
2. Every number, date and seat code the writer emits is extracted by regex
   (``reference_pack.extract_tokens``) and must already exist in the pack
   (as a claim value, an accepted alias spelling, or a claimable seat code).
   A single mismatching token fails the section.
3. A failed section is regenerated (up to ``--retries`` extra attempts). If it
   still fails, the NEXT writer in that language's chain is tried for that
   section (``languages.<lang>.fallbacks`` in the config file); if no writer
   verifies, the section falls back to the deterministic pack deck and is marked
   ``render = "degraded"`` in the manifest. There is NO authored-rate threshold:
   a section is published as authored only if some writer passed its
   verification, and the manifest records which writer wrote which section.
4. A dense section is written in chunks. A section whose fact sheet carries more
   than ``DENSE_SECTION_CLAIMS`` claims is split into pieces of at most
   ``CHUNK_CLAIM_LIMIT`` claims (kept together by group family, so the facts in
   one piece belong to one story). Each piece is authored and verified on its
   own, the paragraphs are concatenated under ONE heading, and a piece that
   fails every writer degrades alone — the reader gets the deterministic
   sentences for that piece's facts, not a fallback for the whole section.
5. EN and MS are separate authoring passes over the same pack: the Malay
   edition is written natively from Malay instructions, never translated from
   the EN output.
6. Titles and footers follow the mode the edition was actually written in. An
   edition with no authored section is never branded as an AI edition, and a
   live pass that produced no authored section publishes nothing at all: the
   previous edition, which was verified, stays where it is and the failure is
   recorded in the manifest.

WRITER MODEL (swappable, one config file)
-----------------------------------------
``02_FORECAST/engine/authoring_config.json`` (gitignored) — edit this file to
change the writer, nothing else:

    {"provider": "nous", "model": "z-ai/glm-5.3-flash",
     "api_key_env": "NOUS_API_KEY", "temperature": 0.7, "max_tokens": 4096}

Missing file → the built-in defaults above are used. ``base_url`` may be set
explicitly to point at any OpenAI-compatible endpoint.

CLI
---
    .venv/bin/python 02_FORECAST/engine/author_reports.py --lang en
    .venv/bin/python 02_FORECAST/engine/author_reports.py --lang ms --state Kedah
    .venv/bin/python 02_FORECAST/engine/author_reports.py --lang en --sections intro_context,closing
    .venv/bin/python 02_FORECAST/engine/author_reports.py --lang en --offline

``--offline`` never calls a model: every section is the deterministic pack deck
(this is what the test suite exercises — no live LLM calls in tests).

A LIVE RUN IS A MANUAL STEP
---------------------------
This script makes real, billed model calls when ``--offline`` is absent. It is
NOT wired into the automated suite and NOT run by CI. Run it by hand, or from an
optional ops stage (see ``tools/authoring/README.md``) that owns its own cron
model entry, independent of the ``analytics`` job.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import os
import shutil
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import reference_pack as rp  # noqa: E402
_CITE_INLINE_RE = rp.CITATION_RE

ROOT = os.path.dirname(os.path.dirname(HERE))
WRITER_CONFIG = os.path.join(HERE, "authoring_config.json")
REPORT_AI = os.path.join(ROOT, "03_REPORTS", "ai")
WORK_AUTHORING = os.path.join(ROOT, "work", "reports", "authoring")
PACK_DIR = rp.WORK_REFERENCE

REPORT_AUTHORITY = {
    "federal": os.path.join(ROOT, "03_REPORTS", "federal", "latest",
                            "GE16_Malaysia_General_Election_Report.md"),
    "state": os.path.join(ROOT, "03_REPORTS", "states", "DUN {state}", "latest",
                          "GE16_{state}_Report.md"),
}

#: Writer defaults. The config file overrides these; nothing else does.
DEFAULT_WRITER_CONFIG = {
    "provider": "nous",
    "model": "z-ai/glm-5.3-flash",
    "api_key_env": "NOUS_API_KEY",
    "temperature": 0.7,
    "max_tokens": 4096,
    "timeout_s": 240,
    "base_url": None,
}

#: Known OpenAI-compatible endpoints (a config ``base_url`` always wins).
PROVIDER_BASE_URLS = {
    "nous": "https://inference-api.nousresearch.com/v1",
    "zai": "https://api.z.ai/api/paas/v4",
    "deepseek": "https://api.deepseek.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}

PROVIDER_KEY_ENV = {
    "nous": "NOUS_API_KEY",
    "zai": "ZAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

DEFAULT_RETRIES = 2
FACT_SHEET_LINE_LIMIT = 320
VIOLATION_CAP = 20

#: A section whose fact sheet carries more claims than this is authored in
#: chunks (see ``section_chunks``): one prompt per chunk, verified per chunk.
DENSE_SECTION_CLAIMS = 25
#: The most claims one authoring prompt (and its verification) carries.
CHUNK_CLAIM_LIMIT = 18
#: The renders that mean a writer wrote the text a reader gets.
AUTHORED_RENDERS = ("authored", "chunked-partial")


class AuthoringError(RuntimeError):
    """Raised when an authoring pass cannot proceed (config/key/transport)."""


# ---------------------------------------------------------------------------
# writer configuration (the ONLY place the writer model is chosen)
# ---------------------------------------------------------------------------

def load_writer_config(path=None, lang=None):
    """Effective writer config for one language pass.

    The config file may carry a ``languages`` block — one writer per edition
    language (the report is authored natively in EN and in MS, and the two passes
    may use different models). The flat settings stay the fallback for every
    language, so a config file that names a single writer, or no config file at
    all, selects exactly what it always did.

    ``lang=None`` returns the flat configuration: the identity callers that do
    not belong to a language pass (a dry run, ``--list-sections``, a test) have
    always seen.
    """
    config = dict(DEFAULT_WRITER_CONFIG)
    path = path or WRITER_CONFIG
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as handle:
                loaded = json.load(handle)
        except ValueError as error:
            raise AuthoringError("authoring_config.json is not valid JSON: {0}".format(error))
        if not isinstance(loaded, dict):
            raise AuthoringError("authoring_config.json must contain a JSON object")
        languages = loaded.get("languages")
        config.update({key: value for key, value in loaded.items()
                       if value is not None and key not in ("languages", "fallbacks")})
        if lang:
            if languages is not None and not isinstance(languages, dict):
                raise AuthoringError("authoring_config.json 'languages' must be an object")
            per_lang = (languages or {}).get(lang)
            if per_lang is not None and not isinstance(per_lang, dict):
                raise AuthoringError(
                    "authoring_config.json 'languages.{0}' must be an object".format(lang))
            if isinstance(per_lang, dict):
                config.update({key: value for key, value in per_lang.items()
                               if value is not None and key not in ("languages", "fallbacks")})
    if not config.get("base_url"):
        config["base_url"] = PROVIDER_BASE_URLS.get(config.get("provider", ""), "")
    if not config.get("api_key_env"):
        config["api_key_env"] = PROVIDER_KEY_ENV.get(config.get("provider", ""), "")
    return config


def writer_config_hash(config):
    """Stable sha256 of the *effective* writer settings (never the secret)."""
    public = {key: config.get(key) for key in
              ("provider", "model", "api_key_env", "temperature", "max_tokens",
               "timeout_s", "base_url")}
    return sha256_json(public)


def writer_identity(config):
    return {
        "provider": config.get("provider"),
        "model": config.get("model"),
        "base_url": config.get("base_url"),
        "api_key_env": config.get("api_key_env"),
        "temperature": config.get("temperature"),
        "max_tokens": config.get("max_tokens"),
    }


def _raw_writer_config(path=None):
    """The config file as written (fallback blocks included)."""
    loaded = {}
    path = path or WRITER_CONFIG
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as handle:
                loaded = json.load(handle)
        except ValueError as error:
            raise AuthoringError(
                "authoring_config.json is not valid JSON: {0}".format(error))
        if not isinstance(loaded, dict):
            raise AuthoringError("authoring_config.json must contain a JSON object")
    return loaded


def _fallback_blocks(path=None, lang=None):
    """``languages.<lang>.fallbacks`` (or the flat ``fallbacks``) as a list."""
    loaded = _raw_writer_config(path)
    blocks = None
    languages = loaded.get("languages")
    if lang and isinstance(languages, dict) and isinstance(languages.get(lang), dict):
        blocks = languages[lang].get("fallbacks")
    if blocks is None:
        blocks = loaded.get("fallbacks")
    if blocks is None:
        return []
    if not isinstance(blocks, list):
        raise AuthoringError(
            "authoring_config.json 'fallbacks' must be a list of writer blocks")
    return blocks


def load_writer_chain(path=None, lang=None):
    """The writers one language pass may use, best first: ``[primary, *fallbacks]``.

    A section goes to the next writer in this chain when the one before it cannot
    deliver text that verifies — a transport failure, or a section that keeps
    failing verification after its retry budget (see ``author_section``). Each
    fallback entry is a partial writer block: the keys it does not name keep the
    effective primary value (the same rule a ``languages`` block follows), so a
    chain entry can be the model name alone. A config file with no ``fallbacks``
    names one writer, exactly as before.
    """
    primary = load_writer_config(path, lang)
    chain = [primary]
    for block in _fallback_blocks(path, lang):
        if isinstance(block, str):
            block = {"model": block}
        if not isinstance(block, dict):
            raise AuthoringError(
                "authoring_config.json 'fallbacks' entries must be objects or model names")
        merged = dict(primary)
        merged.update({key: value for key, value in block.items()
                       if value is not None and key not in ("languages", "fallbacks")})
        if not merged.get("base_url"):
            merged["base_url"] = PROVIDER_BASE_URLS.get(merged.get("provider", ""), "")
        if not merged.get("api_key_env"):
            merged["api_key_env"] = PROVIDER_KEY_ENV.get(merged.get("provider", ""), "")
        chain.append(merged)
    return chain


def writer_chain_hash(chain):
    """Stable sha256 of every writer a pass could reach, in order."""
    return sha256_json([writer_config_hash(config) for config in chain])


def writer_label_of(writer):
    """How a manifest names the writer that wrote a section."""
    label = getattr(writer, "label", None)
    if label:
        return label
    kind = type(writer).__name__
    return "fixture" if kind == "FixtureWriter" else kind


def sha256_json(payload):
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def load_api_key(config, env_path="~/.hermes/.env"):
    """The writer's key: process env first, then the Hermes .env file."""
    name = config.get("api_key_env") or ""
    if name and os.environ.get(name):
        return os.environ[name].strip()
    path = os.path.expanduser(env_path)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("{0}=".format(name)):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise AuthoringError(
        "no API key for writer provider '{0}' (set {1} or add it to {2})".format(
            config.get("provider"), name or "<api_key_env>", env_path))


# ---------------------------------------------------------------------------
# writers
# ---------------------------------------------------------------------------

class LiveWriter:
    """The configured model, over an OpenAI-compatible chat-completions API."""

    def __init__(self, config):
        self.config = config
        self.label = "{0}/{1}".format(config.get("provider"), config.get("model"))

    def write(self, prompt, system, section=None):
        url = (self.config.get("base_url") or "").rstrip("/")
        if not url:
            raise AuthoringError("no base_url for provider '{0}'".format(
                self.config.get("provider")))
        payload = json.dumps({
            "model": self.config.get("model"),
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": prompt}],
            "temperature": self.config.get("temperature", 0.7),
            "max_tokens": self.config.get("max_tokens", 4096),
        }).encode("utf-8")
        request = urllib.request.Request(
            url + "/chat/completions", data=payload,
            headers=self._headers())
        last_error = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(
                        request, timeout=self.config.get("timeout_s", 240)) as response:
                    body = json.loads(response.read().decode("utf-8"))
                message = body["choices"][0]["message"]
                content = message.get("content")
                if content is None:
                    raise ValueError("writer returned no content (finish={0})".format(body["choices"][0].get("finish_reason")))
                return content
            except urllib.error.HTTPError as error:
                # 429/5xx: back off and retry; 4xx other than 429 is terminal.
                last_error = error
                if error.code == 429 or 500 <= error.code <= 599:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                raise AuthoringError("writer request failed: {0}".format(error)) from error
            except (urllib.error.URLError, KeyError, ValueError, OSError) as error:
                last_error = error
                time.sleep(2.0 * (attempt + 1))
        raise AuthoringError("writer request failed: {0}".format(last_error))

    def _headers(self):
        headers = {"Authorization": "Bearer {0}".format(load_api_key(self.config)),
                   "Content-Type": "application/json",
                   "User-Agent": self.config.get("user_agent", "HermesAgent/1.0")}
        session = self.config.get("session_header") or os.environ.get(
            "GE16_WRITER_SESSION_HEADER")
        if session:
            headers["x-opencode-session"] = session
        extra = self.config.get("extra_headers") or {}
        if isinstance(extra, dict):
            headers.update({str(k): str(v) for k, v in extra.items()})
        return headers


class FixtureWriter:
    """A canned writer for tests: a ``{section_key: text}`` mapping (offline)."""

    def __init__(self, texts=None, default=""):
        self.texts = dict(texts or {})
        self.default = default
        self.calls = []

    def write(self, prompt, system, section=None):
        self.calls.append({"section": section, "prompt": prompt, "system": system})
        if section is not None and section in self.texts:
            text = self.texts[section]
        else:
            text = self.default
        return text(section) if callable(text) else text


SYSTEM_EN = """You are the lead analyst writing the AI-authored edition of the \
GE16 (16th Malaysian general election) forecast report for a professional \
audience of Malaysian political analysts.

Write for a reader of political analysis: the people, parties, seats and \
decisions that moved, told as narrative. The report has to read as if a \
political analyst wrote it, never as if a machine assembled it. Never \
mention how the report was produced, and never use any of these words in your \
output: ledger, events ledger, events database, claim, claim id, pack, reference \
pack, window, vintage, degraded, retry, retries, fallback, deterministic, \
authoritative, citation, citations, manifest, schema. Give numbers and dates \
naturally inside the sentence that needs them ("the model's central estimate of \
140 seats", "the show-cause notice of 24 September 2026") rather than as table \
metadata, and use the words a political reader uses — political developments, \
developments, the cycle's events — not the names of the system that produced \
your fact sheet.

Non-negotiable rules:
- You are given a SECTION BRIEF and a FACT SHEET. The fact sheet is the ONLY \
source of facts you may use.
- Every number, percentage, date, year and seat code you write must be copied \
VERBATIM from a fact-sheet row's `value` column, character for character. Never \
round, convert, add, subtract, average, extrapolate or estimate a value.
- Never introduce a number that is not on the fact sheet, not even a harmless \
looking one in a date, an age range, an ordinal or an aside.
- Refer to percentile bounds by their names (P10, P50, P90); never write them as ordinals like "the 10th/90th percentile" or "ke-10/ke-90", because those numerals are not on the fact sheet.

- After a fact appears, cite its claim id once in square brackets, e.g. \
[fed:P50:139]. Cite only claim ids that are on the fact sheet.
- Write flowing, specific analytical prose. Interpret freely; quantify never.
- Markdown only. No heading of your own (the harness adds the section heading). \
Use short paragraphs, and bullet lists only where the brief asks for per-item \
notes.
- Output the section body and nothing else: no preamble, no sign-off."""

SYSTEM_MS = """Anda ialah penganalisis utama yang menulis edisi laporan ramalan \
GE16 (Pilihan Raya Umum ke-16 Malaysia) yang dihasilkan oleh AI, untuk pembaca \
profesional dalam bidang politik Malaysia.

Tulis untuk pembaca analisis politik: orang, parti, kerusi dan keputusan yang \
bergerak, disampaikan sebagai naratif. Laporan ini mesti berbunyi seperti ditulis \
oleh penganalisis politik, bukan seperti disusun oleh mesin. Jangan sekali-kali \
menyebut bagaimana laporan ini dihasilkan, dan jangan gunakan perkataan berikut \
dalam output anda: lejar, lejar peristiwa, pangkalan data peristiwa, dakwaan, id \
dakwaan, pek, pek rujukan, tetingkap, vintaj, dinyahgred, cuba semula, sandaran, \
deterministik, berwibawa, sitasi, manifes, skema. Sebut nombor dan tarikh secara \
semula jadi di dalam ayat yang memerlukannya ("anggaran tengah model 140 kerusi", \
"notis tunjuk sebab pada 24 September 2026"), bukan sebagai metadata jadual, dan \
gunakan perkataan yang difahami pembaca politik — perkembangan politik, \
perkembangan, peristiwa kitaran ini — bukan nama sistem yang menghasilkan senarai \
fakta anda.

Peraturan yang tidak boleh dilanggar:
- Anda diberi RINGKASAN SEKSYEN dan SENARAI FAKTA. Senarai fakta itu satu-satunya \
sumber fakta yang boleh anda gunakan.
- Setiap nombor, peratusan, tarikh, tahun dan kod kerusi yang anda tulis mesti \
disalin VERBATIM daripada lajur `value` dalam senarai fakta, huruf demi huruf. \
Jangan bulatkan, tukar unit, tambah, tolak, puratakan atau anggarkan nilai.
- Jangan sekali-kali memperkenalkan nombor yang tiada dalam senarai fakta — \
termasuk dalam tarikh, julat umur, nombor turutan atau sisipan.
- Selepas sesuatu fakta disebut, sitasikan id dakwaannya sekali dalam kurungan \
segi empat, contohnya [fed:P50:139]. Sitasi hanya id yang ada dalam senarai fakta.
- Tulis prosa analitikal yang mengalir dan bersifat khusus. Tafsir dengan bebas; \
jangan sekali-kali mengira atau menganggar angka baharu.
- Markdown sahaja. Jangan tulis tajuk sendiri (harness menambah tajuk seksyen). \
Gunakan perenggan pendek, dan senarai berbulet hanya apabila ringkasan memintanya.
- Keluarkan isi seksyen sahaja: tiada mukadimah, tiada penutup.
- Sebut sempadan persentil dengan nama P10, P50, P90; jangan tulis ordinal \
seperti "peratus ke-10/ke-90" kerana angka ordinal itu tidak ada dalam senarai \
fakta."""



def system_prompt(lang):
    return SYSTEM_MS if lang == "ms" else SYSTEM_EN


def build_fact_sheet(pack, section, index, lang, line_limit=FACT_SHEET_LINE_LIMIT,
                     claim_ids=None):
    """The section's slice of the pack: one block per group, one line per claim.

    ``claim_ids`` narrows the sheet to one chunk of a dense section (see
    ``section_chunks``): the writer then sees exactly the facts it is being asked
    to write about, and nothing else.
    """
    lines = []
    group = None
    truncated = 0
    wanted = section.get("claim_ids", []) if claim_ids is None else claim_ids
    for claim_id in wanted:
        claim = index.get(claim_id)
        if claim is None:
            continue
        if len(lines) >= line_limit:
            truncated += 1
            continue
        if claim.get("group") != group:
            group = claim.get("group")
            lines.append("")
            lines.append("* group: {0}".format(group or claim.get("family") or "-"))
        unit = reader_unit(claim.get("unit"), lang) or "-"
        value = claim.get("value_str")
        if value in (None, ""):
            value = claim.get("value")
        lines.append("  - {0} | {1} | value: {2} | unit: {3}".format(
            claim_id, _human_label(claim), reader_value(value, lang), unit))
    if truncated:
        lines.append("")
        lines.append("* ({0} further claims omitted for length)".format(truncated))
    return "\n".join(lines).strip()


def build_prompt(pack, section, index, lang, claim_ids=None, part=None, parts=None):
    """One section's authoring prompt: brief + that section's facts only.

    ``claim_ids``/``part``/``parts`` build the prompt for one chunk of a dense
    section: the brief stays the section's, the fact sheet carries only this
    chunk's claims, and the writer is told which paragraph of the section it is
    writing so the chunks can be concatenated under one heading.
    """
    scope = "federal" if pack.get("kind") == "federal" else "the state of {0}".format(
        pack.get("state"))
    wanted = section.get("claim_ids", []) if claim_ids is None else claim_ids
    chunk_note = ""
    if part and parts and parts > 1:
        chunk_note = ("Write only this part of the section (part {0} of {1}): do not "
                      "repeat the other parts and do not write a heading.\n").format(
                          part, parts)
    chunk_note_ms = ""
    if part and parts and parts > 1:
        chunk_note_ms = ("Tulis bahagian ini sahaja (bahagian {0} daripada {1}): jangan "
                         "ulang bahagian lain dan jangan tulis tajuk.\n").format(part, parts)
    if lang == "ms":
        header = (
            "LAPORAN: GE16, skop {0}. Data setakat: {1}.\n"
            "SEKSYEN: {2}\n"
            "RINGKASAN: {3}\n{4}".format(
                scope, pack.get("as_of"), section.get("title_ms") or section.get("title_en"),
                section.get("brief_ms") or section.get("brief_en"), chunk_note_ms))
        sheet_title = "SENARAI FAKTA"
    else:
        header = (
            "REPORT: GE16, scope {0}. Data as of: {1}.\n"
            "SECTION: {2}\n"
            "BRIEF: {3}\n{4}".format(
                scope, pack.get("as_of"), section.get("title_en"),
                section.get("brief_en"), chunk_note))
        sheet_title = "FACT SHEET"
    return "{0}\n{1} (lagi daripada {2} dakwaan; salin lajur `value` secara verbatim):\n\n{3}\n".format(
        header, sheet_title, len(wanted),
        build_fact_sheet(pack, section, index, lang, claim_ids=claim_ids)) if lang == "ms" else \
        "{0}\n{1} ({2} claims; copy the `value` column verbatim):\n\n{3}\n".format(
            header, sheet_title, len(wanted),
            build_fact_sheet(pack, section, index, lang, claim_ids=claim_ids))


def verify_text(text, allowed, index=None):
    """Every number/date/seat token in ``text`` must exist in ``allowed``.

    When ``index`` (``reference_pack.claim_index``) is supplied, every inline
    citation must resolve to a real claim id as well. Returns a list of
    violations ``{token, kind, line, context}`` — empty means the section is a
    faithful quotation of the pack.

    One vocabulary rule rides along: a raw enum value (``high_risk``) is the
    machinery's word, not the page's — the writer's fact sheet hands it the words
    ("high risk"), so a section that prints the token anyway is not published.
    """
    text = text or ""
    violations = []
    seen = set()
    for token in rp.extract_tokens(text):
        normalised = rp.normalise_token(token["token"], token["kind"])
        if normalised in allowed:
            continue
        key = (token["token"], token["kind"])
        if key in seen:
            continue
        seen.add(key)
        violations.append({
            "token": token["token"],
            "kind": token["kind"],
            "line": text.count("\n", 0, token["start"]) + 1,
            "context": _line_at(text, token["start"]),
        })
        if len(violations) >= VIOLATION_CAP:
            return violations
    if index is not None:
        for match in rp.CITATION_RE.finditer(text):
            claim_id = match.group(1).strip()
            if claim_id in index or ("citation", claim_id) in seen:
                continue
            seen.add(("citation", claim_id))
            violations.append({
                "token": claim_id,
                "kind": "citation",
                "line": text.count("\n", 0, match.start()) + 1,
                "context": _line_at(text, match.start()),
            })
            if len(violations) >= VIOLATION_CAP:
                break
    return violations


def _line_at(text, offset):
    start = (text or "").rfind("\n", 0, offset) + 1
    end = (text or "").find("\n", offset)
    if end == -1:
        end = len(text or "")
    return (text or "")[start:end].strip()[:160]


#: The vocabulary a reader of the edition must never meet. The system prompt
#: forbids these words; this is the same rule as a machine check, so a section
#: that uses them fails verification and the reader-clean deterministic text is
#: published in its place instead of the offending prose.
MACHINERY_WORDS_EN = (
    "ledger", "claim", "pack", "window", "vintage", "degraded", "retry",
    "retries", "fallback", "deterministic", "authoritative", "citation",
    "citations", "manifest", "schema", "deck", "triage", "anchor",
)
MACHINERY_WORDS_MS = (
    "lejar", "dakwaan", "pek", "tetingkap", "vintaj", "dinyahgred",
    "cuba semula", "sandaran", "deterministik", "berwibawa", "sitasi",
    "manifes", "skema", "triaj", "sauh",
)


def reader_violations(text, lang="en"):
    """Violations of the reader rule: machinery vocabulary in the prose.

    Two families: the words that describe how the report is MADE (``ledger``,
    ``degraded``), and a raw enum value (``high_risk``) — the pack's own word for
    a category, which the writer's fact sheet hands over as words ("high risk").
    A section that prints either anyway is not published as authored.
    """
    lowered = (text or "").lower()
    words = MACHINERY_WORDS_MS if lang == "ms" else MACHINERY_WORDS_EN
    found = []
    for word in words:
        match = re.search(r"\b{0}\b".format(re.escape(word)), lowered)
        if match:
            found.append({
                "token": word,
                "kind": "reader-word",
                "line": lowered.count("\n", 0, match.start()) + 1,
                "context": _line_at(text, match.start()),
            })
    # A claim id may legitimately carry an enum value (``fed:watch_tier:P053:
    # high_risk``): that is the machinery's name for the claim, not prose, and
    # the reader never sees it — ``footnote_transform`` turns it into ``[^n]`` —
    # so a match inside a citation is not a violation.
    cited = [match.span() for match in rp.CITATION_RE.finditer(text or "")]
    for match in ENUM_RESIDUE_RE.finditer(lowered):
        if any(start <= match.start() < end for start, end in cited):
            continue
        found.append({
            "token": match.group(0),
            "kind": "vocabulary",
            "line": lowered.count("\n", 0, match.start()) + 1,
            "context": _line_at(text, match.start()),
        })
    return found


# ---------------------------------------------------------------------------
# deterministic fallback text (--offline, and the post-retry degrade path)
# ---------------------------------------------------------------------------
# The fallback is a DECK, not an essay: templated sentences where the pack
# carries the shape of a sentence, then one bullet per claim. It is composed
# only from pack values, so it passes the same verifier the writer must pass.

def key_index(pack):
    """``{claim key: claim}`` — first claim wins (keys are unique in practice)."""
    index = {}
    for claim in pack.get("claims", []):
        index.setdefault(claim.get("key"), claim)
    return index


def _cite(claim):
    return "[{0}]".format(claim["claim_id"])


# --- what a registry token is called on the page ---------------------------
# The pack names some claims by a registry token (``seat_type_count:pn_core``,
# ``macro:gdp_yoy``, ``pn_core: Malay share floor``). Those tokens belong in
# claim ids, citations and prompts. A reader meets the human phrase instead, and
# that translation happens in ONE place — ``_human_label`` — so the figure
# sentences and the reader's Notes can never print a raw key, in either
# language. Adding a token here is how a new registry term reaches the page.
LABEL_TOKENS = {
    "pn_core": "PN core seat",
    "mixed_malay": "mixed-Malay seat",
    "true_mixed": "true mixed seat",
    "non_malay": "non-Malay seat",
    "east_malaysia": "East Malaysia seat",
    "east-malaysia": "East Malaysia seat",
    "seat_type": "seat profile",
    "seat_type_count": "seat profile",
    "projection": "model central estimate",
    "threshold": "majority threshold",
    "macro": "economic backdrop",
}
#: Registry tokens that appear as an ALL-CAPS word rather than a key prefix.
LABEL_WORDS = (
    ("MACRO", "economic backdrop"),
    ("MAKRO", "economic backdrop"),
)
#: An enum-style pack value — the machinery's own word for a category — as the
#: page prints it. The reader meets the words; the value stays in the pack for
#: the verifier. A value that is not listed here is printed as it stands.
ENUM_VALUES = {
    "high_risk": "high risk",
    "super_marginal": "highly marginal",
}
#: The same enum values for the Malay edition.
ENUM_VALUES_MS = {
    "high_risk": "berisiko tinggi",
    "super_marginal": "super-marginal",
}
#: A machine enum value anywhere in a text the page may print (F3): the fact
#: sheet hands the writer the words, and this is the belt to that brace — a
#: section that echoes the raw token anyway does not verify.
ENUM_RESIDUE_RE = re.compile(r"\b(?:{0})\b".format("|".join(
    sorted(set(ENUM_VALUES) | set(ENUM_VALUES_MS)))))
#: A pack label that is machinery end to end, rewritten as a reader sentence.
LABEL_REWRITES = {
    "EVENT_SHOCKS is empty: no seat-level shocks are carried":
        "No seat-level electoral shocks are carried in this edition",
}
#: A pack field name as the phrase a reader reads.
FIELD_LABELS = {
    "gdp_yoy": "GDP growth",
    "cpi_yoy": "inflation",
    "ringgit": "ringgit exchange rate",
    "approval_delta": "approval change",
    "pm_pref_malay": "approval among Malay voters",
    "pm_pref_nonmalay": "approval among non-Malay voters",
    "wt_malay": "Malay share of the electorate",
    "wt_chinese": "Chinese share of the electorate",
    "wt_indian": "Indian share of the electorate",
    "wt_bumi_sabah": "Sabah Bumiputera share of the electorate",
    "wt_bumi_sarawak": "Sarawak Bumiputera share of the electorate",
    "youth_pct": "youth share of the electorate",
    "age_31_40": "share of voters aged 31 to 40",
    "age_41_50": "share of voters aged 41 to 50",
    "age_51_60": "share of voters aged 51 to 60",
    "age_60plus": "share of voters aged 60 and over",
    "avg_median_age": "average median age of voters",
    "total_electorate": "voters modelled",
    "east_electorate": "voters in Sabah and Sarawak",
    "east_seats": "seats in Sabah and Sarawak",
    "margin_under_1": "under-1% margin band",
    "margin_under_2_5": "under-2.5% margin band",
    "margin_under_5": "under-5% margin band",
    "total_valid": "valid votes cast at GE15",
    "avg_margin": "average GE15 winning margin",
    "total_seats": "seats with a GE15 result",
}

_SNAKE_TOKEN = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+")
_SEATS_TYPED_LABEL = re.compile(r"^Seats typed ([\w-]+)$")
_SEAT_SHARE_LABEL = re.compile(r"^([a-z][a-z0-9_]*): (Malay share (?:floor|ceiling))$")
_MARGIN_BAND_LABEL = re.compile(r"^GE15 seats inside the (\w+) margin band$")
_MACRO_LABEL = re.compile(r"^MACRO reading: (.+)$")
_ELECTORATE_LABEL = re.compile(r"^Electorate: (.+)$")
_TRAILING_FIELD_LABEL = re.compile(r": ([a-z][a-z0-9]*(?:_[a-z0-9]+)+)$")


def _field_phrase(token):
    """A pack field name as the phrase a reader reads."""
    if token in FIELD_LABELS:
        return FIELD_LABELS[token]
    return _SNAKE_TOKEN.sub(lambda match: match.group(0).replace("_", " "), str(token))


def human_label(label):
    """A pack label as the page prints it: never a registry token, never a key."""
    text = str(label or "").strip()
    if not text:
        return text
    if text in LABEL_REWRITES:
        return LABEL_REWRITES[text]
    match = _SEATS_TYPED_LABEL.match(text)
    if match:
        token = match.group(1)
        return "{0} count".format(LABEL_TOKENS.get(token) or _field_phrase(token))
    match = _SEAT_SHARE_LABEL.match(text)
    if match:
        return "{0}: {1}".format(LABEL_TOKENS.get(match.group(1), match.group(1)),
                                 match.group(2))
    match = _MACRO_LABEL.match(text)
    if match:
        return "economic backdrop: {0}".format(_field_phrase(match.group(1)))
    match = _ELECTORATE_LABEL.match(text)
    if match:
        return _field_phrase(match.group(1))
    match = _MARGIN_BAND_LABEL.match(text)
    if match:
        return "GE15 seats inside the {0}".format(_field_phrase(match.group(1)))
    for token, phrase in LABEL_WORDS:
        text = text.replace(token, phrase)
    match = _TRAILING_FIELD_LABEL.search(text)
    if match:
        text = text[:match.start(1)] + _field_phrase(match.group(1))
    if text in LABEL_TOKENS:
        return LABEL_TOKENS[text]
    # Nothing above claimed it: no field name survives as an identifier.
    return _SNAKE_TOKEN.sub(lambda m: m.group(0).replace("_", " "), text)


def _groups(section, index):
    """``[(group, [claims])]`` for a section, in pack order."""
    order = []
    buckets = {}
    for claim_id in section.get("claim_ids", []):
        claim = index.get(claim_id)
        if claim is None:
            continue
        group = claim.get("group") or ""
        if group not in buckets:
            buckets[group] = []
            order.append(group)
        buckets[group].append(claim)
    return [(group, buckets[group]) for group in order]


def _figure_sentences(section, index, lang, covered=()):
    """The section's remaining figures as short reader sentences.

    There is no figure list and no list heading anywhere in a published edition:
    a figure the section's own prose has not already carried is written into one
    short sentence that names what it measures (``Model central estimate: 140
    seats. [^6]``), never as a ``collection:field`` row with a machine identifier.

    A claim label is free prose and may carry a digit that is not itself a claim
    (``margin < 5%``): labels are shown only when they add no new token, so these
    sentences always pass the same verifier the writer must pass. The Malay
    edition keeps the same guard, reporting a figure by its value and unit alone
    when its label would introduce a number of its own.
    """
    sentences = []
    for claim in [index.get(cid) for cid in section.get("claim_ids", [])]:
        if claim is None or claim.get("claim_id") in covered:
            continue
        value = claim.get("value_str")
        if value in (None, ""):
            value = claim.get("value")
        unit = reader_unit(claim.get("unit"), lang)
        figure = "{0}{1}".format(str(reader_value(value, lang)), (" " + unit) if unit else "")
        label = _human_label(claim)
        if lang == "ms" or rp.extract_tokens(label):
            label = ""
        if label:
            sentences.append("{0}: {1}. {2}".format(label, figure, _cite(claim)))
        else:
            sentences.append("{0}. {1}".format(figure, _cite(claim)))
    return sentences


def _sentences(k, specs):
    """Fill ``(template, [(name, key)])`` specs, skipping any incomplete one."""
    out = []
    for template, pairs in specs:
        values = {}
        cites = []
        complete = True
        for name, key in pairs:
            claim = k.get(key)
            if claim is None or claim.get("value_str") in (None, ""):
                complete = False
                break
            values[name] = claim["value_str"]
            cites.append(_cite(claim))
        if complete:
            out.append(template.format(**values) + " " + " ".join(cites))
    return out


def _fb_intro(k, lang, section=None, index=None):
    if lang == "ms":
        return _sentences(k, [
            ("Anggaran tengah model ialah {govt} kerusi berpihak kerajaan daripada {total} kerusi, "
             "berbanding majoriti mudah {maj} kerusi.",
             [("govt", "govt_expected"), ("total", "parliament_seats"),
              ("maj", "threshold:simple_majority")]),
            ("Julat Monte Carlo ialah P10 {p10}, P50 {p50} dan P90 {p90}, dengan kebarangkalian "
             "{pmaj}% untuk mengekalkan majoriti.",
             [("p10", "P10"), ("p50", "P50"), ("p90", "P90"), ("pmaj", "P_majority")]),
            ("GE15 meninggalkan kerajaan dengan {g15g} kerusi dan pembangkang dengan {g15o} kerusi; "
             "unjuran ini ialah perubahan {delta} kerusi.",
             [("g15g", "bloc_total:ge15_government"), ("g15o", "bloc_total:ge15_opposition"),
              ("delta", "bloc_change:government")]),
            ("{flips} kerusi dijangka bertukar tangan.",
             [("flips", "flips_total")]),
            ("{vacant} daripada {total} kerusi kosong dalam edisi ini.",
             [("vacant", "vacant_seats"), ("total", "parliament_seats")]),
            ("Latar belakang ekonomi yang dibawa model: pertumbuhan KDNK {gdp}%, inflasi {cpi}%, "
             "dan ringgit pada {fx} per dolar AS.",
             [("gdp", "macro:gdp_yoy"), ("cpi", "macro:cpi_yoy"), ("fx", "macro:ringgit")]),
        ])
    return _sentences(k, [
        ("The model's central estimate is {govt} government-aligned seats of {total}, against a "
         "{maj}-seat simple majority.",
         [("govt", "govt_expected"), ("total", "parliament_seats"),
          ("maj", "threshold:simple_majority")]),
        ("The Monte Carlo range is P10 {p10}, P50 {p50} and P90 {p90}, with a {pmaj}% probability "
         "of retaining the majority.",
         [("p10", "P10"), ("p50", "P50"), ("p90", "P90"), ("pmaj", "P_majority")]),
        ("GE15 left the government on {g15g} seats and the opposition on {g15o}; the projection is "
         "a change of {delta} seats.",
         [("g15g", "bloc_total:ge15_government"), ("g15o", "bloc_total:ge15_opposition"),
          ("delta", "bloc_change:government")]),
        ("{flips} seats are projected to change hands.",
         [("flips", "flips_total")]),
        ("{vacant} of the {total} seats stand vacant in this edition.",
         [("vacant", "vacant_seats"), ("total", "parliament_seats")]),
        ("The economic backdrop the model carries: GDP growth {gdp}%, inflation {cpi}%, and the "
         "ringgit at {fx} per USD.",
         [("gdp", "macro:gdp_yoy"), ("cpi", "macro:cpi_yoy"), ("fx", "macro:ringgit")]),
    ])


def _fb_electorate(k, lang, section=None, index=None):
    if lang == "ms":
        return _sentences(k, [
            ("Pengundi yang dimodelkan ialah {tot} pengundi dalam {seats} kerusi, dengan {malay}% "
             "Melayu, {chinese}% Cina dan {indian}% India.",
             [("tot", "electorate:total_electorate"), ("seats", "electorate:total_seats"),
              ("malay", "electorate:wt_malay"), ("chinese", "electorate:wt_chinese"),
              ("indian", "electorate:wt_indian")]),
            ("{youth}% pengundi berumur {floor} hingga {ceil} tahun; purata umur median ialah {mam}.",
             [("youth", "electorate:youth_pct"), ("floor", "threshold:youth_age_floor"),
              ("ceil", "threshold:youth_age_ceiling"), ("mam", "electorate:avg_median_age")]),
            ("Garis dasar GE15 mengira {valid} undi sah; purata majoriti kemenangan ialah {am}% dan "
             "{u5} kerusi dimenangi dengan margin bawah {mw}%.",
             [("valid", "electorate:total_valid"), ("am", "electorate:avg_margin"),
              ("u5", "electorate:margin_under_5"), ("mw", "threshold:margin_watch")]),
        ])
    return _sentences(k, [
        ("The electorate modelled here is {tot} voters across {seats} seats — {malay}% Malay, "
         "{chinese}% Chinese and {indian}% Indian.",
         [("tot", "electorate:total_electorate"), ("seats", "electorate:total_seats"),
          ("malay", "electorate:wt_malay"), ("chinese", "electorate:wt_chinese"),
          ("indian", "electorate:wt_indian")]),
        ("{youth}% of voters are aged {floor} to {ceil}, and the average median age is {mam}.",
         [("youth", "electorate:youth_pct"), ("floor", "threshold:youth_age_floor"),
          ("ceil", "threshold:youth_age_ceiling"), ("mam", "electorate:avg_median_age")]),
        ("The GE15 baseline counted {valid} valid votes; the average winning margin was {am}% and "
         "{u5} seats were won on a margin under {mw}%.",
         [("valid", "electorate:total_valid"), ("am", "electorate:avg_margin"),
          ("u5", "electorate:margin_under_5"), ("mw", "threshold:margin_watch")]),
    ])


def _story_chain_ids(k):
    """The chains of events a section carries, in pack order (most recent first)."""
    ids = []
    for key in k:
        if key.startswith("story_headline:"):
            sid = key.split(":", 1)[1]
            if sid not in ids:
                ids.append(sid)
    return ids


def _section_chain_ids(section, index):
    """The chains THIS section carries (its own slice, most recent first)."""
    if not section or index is None:
        return []
    ids = []
    for claim_id in section.get("claim_ids", []):
        claim = index.get(claim_id)
        key = (claim or {}).get("key") or ""
        if key.startswith("story_headline:"):
            sid = key.split(":", 1)[1]
            if sid not in ids:
                ids.append(sid)
    return ids


def _fb_story_intro(k, lang, section=None, index=None):
    """The cycle's political scene: who is moving, on what, and how recently.

    Reader-facing by construction: each chain is named by its own headline, with
    the latest development and the date it happened, and never by the harness's
    names for it — no counts of chains, no windows, no ledger.
    """
    named = []
    cites = []
    chain_ids = _section_chain_ids(section, index) or _story_chain_ids(k)
    for sid in chain_ids:
        headline = k.get("story_headline:{0}".format(sid))
        latest = k.get("story_latest:{0}".format(sid))
        last = k.get("story_last_update:{0}".format(sid))
        if headline is None or not (headline.get("value_str") or "").strip():
            continue
        if latest is None or not (latest.get("value_str") or "").strip():
            continue
        date = (last.get("value_str") if last else "") or ""
        development = (latest.get("value_str") or "").rstrip(". \u2014\u2013-,(")
        if lang == "ms":
            named.append("**{0}** (perkembangan terkini: {1}{2})".format(
                headline["value_str"], development,
                ", " + date if date else ""))
        else:
            named.append("**{0}** (latest development: {1}{2})".format(
                headline["value_str"], development,
                ", " + date if date else ""))
        cites.extend(_cite(claim) for claim in (headline, latest, last) if claim)
    if not named:
        return (["Tiada rantaian peristiwa politik berkaitan dibawa dalam edisi ini."]
                if lang == "ms" else
                ["No chains of related political events are carried in this edition."])
    if lang == "ms":
        opening = ("Politik Malaysia kitaran ini berpaksikan rantaian peristiwa berkaitan "
                   "berikut: " + "; ".join(named) +
                   ". Setiap rantaian ialah urutan perkembangan bertarikh yang masih "
                   "bergerak, bukan keputusan yang sudah muktamad.")
    else:
        opening = ("Malaysian politics this cycle turns on these chains of related events: "
                   + "; ".join(named) +
                   ". Each chain is a sequence of dated developments still in motion, not a "
                   "settled outcome.")
    return [opening + " " + " ".join(cites)]


def _fb_story_bullets(section, index, lang):
    lines = []
    for group, claims in _groups(section, index):
        by_suffix = {}
        for claim in claims:
            by_suffix[claim.get("key", "").split(":", 1)[0]] = claim
        headline = by_suffix.get("story_headline")
        if headline is None:
            continue
        latest = by_suffix.get("story_latest")
        last = by_suffix.get("story_last_update")
        development = (latest.get("value_str") if latest else "") or ""
        date = (last.get("value_str") if last else "") or ""
        bits = ", ".join(part for part in
                         (development.rstrip(". \u2014\u2013-,(").strip(), date) if part)
        if not bits:
            continue
        cites = " ".join(_cite(c) for c in claims if c)
        lines.append("- **{0}** — {1}. {2}".format(_enum(headline, lang), bits, cites))
    return lines


def _fb_closing(k, lang, section=None, index=None):
    if lang == "ms":
        return _sentences(k, [
            ("Bacaan akhir: {govt} daripada {total} kerusi berpihak kerajaan, pembangkang {opp}, "
             "pada kebarangkalian {pmaj}% untuk memegang majoriti {maj} kerusi.",
             [("govt", "govt_expected"), ("total", "parliament_seats"),
              ("opp", "bloc_total:opposition"), ("pmaj", "P_majority"),
              ("maj", "threshold:simple_majority")]),
            ("Ambang dua pertiga ialah {tt} kerusi.", [("tt", "threshold:two_thirds")]),
        ])
    return _sentences(k, [
        ("Final read: {govt} of {total} government-aligned seats against {opp} opposition seats, on "
         "a {pmaj}% probability of holding the {maj}-seat majority.",
         [("govt", "govt_expected"), ("total", "parliament_seats"),
          ("opp", "bloc_total:opposition"), ("pmaj", "P_majority"),
          ("maj", "threshold:simple_majority")]),
        ("The two-thirds threshold is {tt} seats.", [("tt", "threshold:two_thirds")]),
    ])


def _enum(claim, lang="en"):
    """A claim's value as the page prints it: an enum in words, a figure verbatim."""
    return reader_value((claim or {}).get("value_str"), lang)


def _fb_signal_bullets(section, index, lang):
    lines = []
    for group, claims in _groups(section, index):
        by_key = {claim.get("key"): claim for claim in claims}
        title = by_key.get("signal_title:{0}".format(group.split(":")[-1])) or \
            next((c for c in claims if c.get("key", "").startswith("signal_title")), None)
        category = next((c for c in claims if c.get("key", "").startswith("signal_category")), None)
        if title is None:
            continue
        tail = (" — dilaporkan sebagai {0}".format(_enum(category, lang))
                if category else "") if lang == "ms" else \
            (" — reported as {0}".format(_enum(category, lang)) if category else "")
        cites = " ".join(_cite(c) for c in (title, category) if c)
        lines.append("- {0}{1} {2}".format(_enum(title, lang), tail, cites))
    return lines


def _fb_watch_bullets(section, index, lang):
    lines = []
    for group, claims in _groups(section, index):
        code = group.split(":", 1)[1] if ":" in group else ""
        by_suffix = {claim.get("key", "").split(":", 1)[0]: claim for claim in claims}
        name = by_suffix.get("watch_constituency")
        if name is None:
            continue
        bits = []
        for prefix, label in (("watch_margin", "margin"), ("watch_tier", "tier"),
                              ("watch_holders", "holders"), ("watch_state", "state")):
            claim = by_suffix.get(prefix)
            if claim:
                bits.append("{0} {1}".format(label, _enum(claim, lang)))
        cites = " ".join(_cite(c) for c in claims)
        lines.append("- {0} ({1}) — {2} {3}".format(_enum(name, lang), code,
                                                    ", ".join(bits), cites))
    return lines


def _fb_scenario_bullets(section, index, lang):
    lines = []
    for group, claims in _groups(section, index):
        by_key = {claim.get("key"): claim for claim in claims}
        name = next((c for c in claims if c.get("key", "").endswith(":name")), None)
        govt = next((c for c in claims if c.get("key", "").endswith(":government")), None)
        opp = next((c for c in claims if c.get("key", "").endswith(":opposition")), None)
        if name is None or govt is None:
            continue
        tail = ", opposition {0}".format(_enum(opp, lang)) if opp else ""
        cites = " ".join(_cite(c) for c in (name, govt, opp) if c)
        lines.append("- **{0}** — government {1}{2} {3}".format(
            _enum(name, lang), _enum(govt, lang), tail, cites))
    return lines


def _fb_swing_bullets(section, index, lang):
    lines = []
    for group, claims in _groups(section, index):
        if not group.startswith("swing:"):
            continue
        state = group.split(":", 1)[1]
        parts = []
        cites = []
        for claim in claims:
            if claim.get("family") != "swing":
                continue
            bloc = (claim.get("key") or "").rsplit(":", 1)[-1]
            parts.append("{0} {1}".format(bloc, _enum(claim, lang)))
            cites.append(_cite(claim))
        if parts:
            lines.append("- {0}: {1} {2}".format(state, ", ".join(parts), " ".join(cites)))
    return lines


#: section key -> renderer. Renderers return the bullet/paragraph lines that
#: precede the plain figure list (``None`` = list only). Signature:
#: ``renderer(key_index, lang, section=None, index=None)`` — the section and its
#: claim index are passed so a renderer can stay inside its own slice.
FALLBACK_RENDERERS = {
    "intro_context": _fb_intro,
    "electorate": _fb_electorate,
    "story_threads_intro": _fb_story_intro,
    "closing": _fb_closing,
}
FALLBACK_BULLETS = {
    "this_week": _fb_signal_bullets,
    "story_threads_items": _fb_story_bullets,
    "watch_list": _fb_watch_bullets,
    "scenario_narrative": _fb_scenario_bullets,
    "electorate_dynamics": _fb_swing_bullets,
}
#: Sections whose deterministic text already carries their figures — their own
#: renderer or their per-group bullets. Adding a figure list on top would say the
#: same thing twice to the reader.
NO_FIGURE_LIST = {"story_threads_intro"} | set(FALLBACK_BULLETS)


def render_fallback(pack, section, index, lang, claim_ids=None):
    """Deterministic section text: the pack's sentences, then figures in prose.

    There is no deck and no figure list: a reader gets the pack's own sentences
    and then one short sentence per figure those sentences did not already carry.

    ``claim_ids`` renders the deterministic text for ONE chunk of a dense
    section: only the sentences whose facts live in that chunk (the templates
    that chunk completes), and only that chunk's figures. A chunk that fell back
    therefore degrades alone instead of replacing its whole section.
    """
    key = section.get("key")
    k = key_index(pack)
    scope = section
    if claim_ids is not None:
        members = set(claim_ids)
        k = {name: claim for name, claim in k.items()
             if claim.get("claim_id") in members}
        scope = dict(section)
        scope["claim_ids"] = [cid for cid in claim_ids if cid in index]
    lines = []
    renderer = FALLBACK_RENDERERS.get(key)
    if renderer:
        lines.extend(renderer(k, lang, section=scope, index=index))
    bullets = FALLBACK_BULLETS.get(key)
    if bullets:
        lines.extend(bullets(scope, index, lang))
    rendered = "\n".join(lines)
    figures = []
    # A section whose own text carries its figures gets no figure list on top —
    # unless this is a chunk that rendered nothing at all, where the figures are
    # the only reader text those facts have.
    if key not in NO_FIGURE_LIST or (claim_ids is not None and not lines):
        cited = {match.group(1).strip()
                 for match in rp.CITATION_RE.finditer(rendered)}
        figures = _figure_sentences(scope, index, lang, covered=cited)
    if figures:
        lines.append("")
        lines.append(" ".join(figures))
    if not lines:
        lines.append("This section carries no figures for this edition."
                     if lang != "ms" else
                     "Seksyen ini tidak membawa angka untuk edisi ini.")
    return "\n".join(lines).strip()


def section_chunks(section, index, limit=CHUNK_CLAIM_LIMIT):
    """A dense section's claims as ``[[claim_id, ...], ...]``, ``limit`` each.

    Claims keep their pack order and stay together by group family, so one chunk
    is one coherent set of facts (a chain of events, one state's swing, one
    bloc's seat profile) rather than a cut through the middle of a story. A
    section that is not dense returns a single chunk — exactly the old
    behaviour — so only the sections whose fact sheets are too big to write in
    one prompt are split.
    """
    claim_ids = [cid for cid in section.get("claim_ids", []) if cid in index]
    if len(claim_ids) <= DENSE_SECTION_CLAIMS:
        return [claim_ids]
    order, buckets = [], {}
    for cid in claim_ids:
        claim = index[cid]
        family = (claim.get("group") or claim.get("family")
                  or str(claim.get("key") or "").split(":", 1)[0])
        if family not in buckets:
            buckets[family] = []
            order.append(family)
        buckets[family].append(cid)
    chunks, current = [], []
    for family in order:
        if current and len(current) + len(buckets[family]) > limit:
            chunks.append(current)
            current = []
        for cid in buckets[family]:
            current.append(cid)
            if len(current) == limit:
                chunks.append(current)
                current = []
    if current:
        chunks.append(current)
    return chunks or [claim_ids]


def _merge_violations(chunk_records):
    """The violations of every chunk that did not come back authored, deduped."""
    merged, seen = [], set()
    for item in chunk_records:
        for violation in item.get("violations") or []:
            token = (violation.get("kind"), violation.get("token"))
            if token in seen:
                continue
            seen.add(token)
            merged.append(violation)
            if len(merged) >= VIOLATION_CAP:
                return merged
    return merged


def _author_text(pack, section, index, allowed, chain, lang, retries,
                 claim_ids=None, part=None, parts=None):
    """One paragraph: try each writer in turn, verify what comes back.

    A writer is retried in place up to ``retries`` times; when it still cannot
    deliver text that verifies — or when its transport fails — the next writer in
    the chain gets the same facts. Returns ``render = "authored"`` only when some
    writer passed this paragraph's verification.
    """
    outcome = {"render": "degraded", "body": "", "writer": None, "attempts": 0,
               "violations": [], "error": None, "claims": list(claim_ids or []),
               "writers_tried": []}
    system = system_prompt(lang)
    prompt = build_prompt(pack, section, index, lang, claim_ids=claim_ids,
                          part=part, parts=parts)
    for writer in chain:
        label = writer_label_of(writer)
        if label not in outcome["writers_tried"]:
            outcome["writers_tried"].append(label)
        for _attempt in range(retries + 1):
            outcome["attempts"] += 1
            try:
                text = writer.write(prompt, system, section=section.get("key"))
            except Exception as error:  # noqa: BLE001 - a dead writer is not fatal
                outcome["error"] = "{0}: {1}".format(type(error).__name__, error)
                break  # this writer is unreachable: the next one gets the facts
            body = (text or "").strip()
            # The writer was told never to name the machinery it was given; a
            # paragraph that does anyway is not publishable, however faithful its
            # numbers are. Verification is per paragraph, so one bad chunk cannot
            # take the rest of its section down with it.
            violations = verify_text(body, allowed, index) + reader_violations(body, lang)
            if body and not violations:
                outcome.update({"render": "authored", "body": body,
                                "writer": label, "violations": []})
                return outcome
            outcome["violations"] = violations
    return outcome


def author_section(pack, section, index, allowed, writer, lang,
                   retries=DEFAULT_RETRIES, offline=False, writers=None,
                   chunk_claim_limit=CHUNK_CLAIM_LIMIT):
    """Author ONE section; verify it; regenerate on a mismatch; else fall back.

    ``writers`` is the language's failover chain for this section (best first);
    ``writer`` alone means one writer, which is exactly the old behaviour.

    A dense section (more than ``DENSE_SECTION_CLAIMS`` claims) is written in
    chunks of at most ``chunk_claim_limit`` claims, each verified on its own and
    concatenated under the section's ONE heading. A chunk that no writer could
    deliver falls back to the deterministic sentences for that chunk's facts
    alone — the section as a whole is ``chunked-partial``, not ``degraded``.
    """
    chain = [item for item in (writers if writers is not None else [writer])
             if item is not None]
    record = {
        "key": section.get("key"),
        "number": section.get("number"),
        "title": section.get("title_ms") if lang == "ms" else section.get("title_en"),
        "claims": len(section.get("claim_ids", [])),
        "attempts": 0,
        "render": "offline-fallback",
        "violations": [],
        "chars": 0,
        "writer": None,
        "writers_tried": [],
        "chunks": [],
    }
    if offline or not chain:
        body = render_fallback(pack, section, index, lang)
        # A fallback that fails its own verifier is a template bug: publish it
        # but say so, instead of silently labelling it clean.
        record["violations"] = verify_text(body, allowed, index)
        record["reader_violations"] = reader_violations(body, lang)
        if record["violations"] or record["reader_violations"]:
            record["render"] = "degraded"
        record["body"] = body
        record["chars"] = len(body)
        return record

    chunks = section_chunks(section, index, chunk_claim_limit)
    multi = len(chunks) > 1
    paragraphs, chunk_records, errors = [], [], []
    for position, claim_ids in enumerate(chunks, start=1):
        outcome = _author_text(pack, section, index, allowed, chain, lang, retries,
                               claim_ids=claim_ids,
                               part=position if multi else None,
                               parts=len(chunks) if multi else None)
        if outcome["render"] == "authored":
            text = outcome["body"]
        else:
            text = render_fallback(pack, section, index, lang,
                                   claim_ids=claim_ids if multi else None)
            if verify_text(text, allowed, index) or reader_violations(text, lang):
                outcome["render"] = "degraded"
                outcome["fallback_violations"] = True
        if outcome.get("error"):
            errors.append(outcome["error"])
        paragraphs.append(text)
        chunk_records.append({
            "part": position,
            "claims": len(claim_ids),
            "render": outcome["render"],
            "attempts": outcome["attempts"],
            "writer": outcome["writer"],
            "writers_tried": list(outcome["writers_tried"]),
            "violations": outcome["violations"],
        })
    authored = [item for item in chunk_records if item["render"] == "authored"]
    record["attempts"] = max(item["attempts"] for item in chunk_records)
    record["body"] = "\n\n".join(paragraphs)
    record["chars"] = len(record["body"])
    record["chunks"] = chunk_records if multi else []
    tried = []
    for item in chunk_records:
        for label in item["writers_tried"]:
            if label not in tried:
                tried.append(label)
    record["writers_tried"] = tried
    record["writer"] = authored[-1]["writer"] if authored else None
    record["error"] = errors[0] if errors else None
    if authored and len(authored) == len(chunk_records):
        record["render"] = "authored"
        record["violations"] = []
        return record
    record["render"] = "chunked-partial" if authored else "degraded"
    record["violations"] = _merge_violations(chunk_records)
    return record


def author_document(pack, writer=None, lang="en", sections=None, offline=False,
                    retries=DEFAULT_RETRIES, writer_label=None, pack_path=None,
                    writers=None, chunk_claim_limit=CHUNK_CLAIM_LIMIT, mode=None):
    """Author every requested section and assemble the Markdown document."""
    index = rp.claim_index(pack)
    allowed = rp.allowed_tokens(pack)
    wanted = set(sections) if sections else None
    records = []
    for section in pack.get("sections", []):
        if wanted and section.get("key") not in wanted:
            continue
        records.append(author_section(pack, section, index, allowed, writer, lang,
                                      retries=retries, offline=offline,
                                      writers=writers,
                                      chunk_claim_limit=chunk_claim_limit))
    markdown = assemble_document(pack, records, lang, writer_label,
                                 pack_path, pack.get("pack_hash", ""), mode=mode)
    return markdown, records


def edition_mode(records, offline=False):
    """``"live"`` when a writer wrote at least one section, else ``"offline"``.

    The title and the footer are derived from THIS, never from the intent of the
    run: a pass in which every section fell back is not an AI edition.
    """
    if offline:
        return "offline"
    return "live" if authored_count(records) else "offline"


def authored_count(records):
    """Sections a writer wrote (a partly authored section counts as AI-assisted)."""
    return sum(1 for record in records if record.get("render") in AUTHORED_RENDERS)


def assemble_document(pack, records, lang, writer_label, pack_path, pack_hash,
                      mode=None):
    """The published Markdown: a quiet edition note + one section per record.

    ``mode`` is ``"live"`` (some writer authored at least one section) or
    ``"offline"`` (the deterministic render). It decides the title and the footer
    so a reader can never be told an unwritten edition was AI-authored: an
    edition with no authored section is titled as a model report, and a partly
    authored edition says how many sections were written by the model.
    """
    federal = pack.get("kind") == "federal"
    authored = authored_count(records)
    total = len(records)
    if mode is None:
        mode = "live" if authored else "offline"
    state = pack.get("state")
    if mode == "offline":
        # No writer wrote this edition, so it is NOT branded as one: the title
        # says what it is (a report of the model's own numbers). The word
        # "deterministic" is machinery vocabulary and lives in this line only,
        # never in the prose (see ReaderFocusTests).
        if federal:
            title = ("GE16 — Laporan Model (paparan deterministik)" if lang == "ms"
                     else "GE16 — Model Report (deterministic render)")
        else:
            title = ("GE16 — {0} (Laporan Model — paparan deterministik)".format(state)
                     if lang == "ms"
                     else "GE16 — {0} (Model Report — deterministic render)".format(state))
        footer = OFFLINE_FOOTER_TEXTS[1 if lang == "ms" else 0]
    else:
        if lang == "ms":
            title = ("GE16 — Edisi Ditulis AI" if federal
                     else "GE16 — {0} (Edisi Ditulis AI)".format(state))
            footer = (EDITION_FOOTER_TEXTS[1] if authored == total
                      else partial_footer(authored, total, "ms"))
        else:
            title = ("GE16 — AI-Authored Edition" if federal
                     else "GE16 — {0} (AI-Authored Edition)".format(state))
            footer = (EDITION_FOOTER_TEXTS[0] if authored == total
                      else partial_footer(authored, total, "en"))
    lines = [
        "# {0}".format(title),
        "",
        "---",
        "",
    ]
    for record in records:
        body = (record.get("body") or "").strip()
        lines.append("## {0}. {1}".format(record.get("number"), record.get("title")))
        lines.append("")
        lines.append(body)
        lines.append("")
    # The edition note is the only line the harness adds to the reader's page:
    # who wrote what, how many sections were replaced, the manifest path and the
    # writer/language/vintage header all live in the manifest JSON instead.
    lines.append("---")
    lines.append("")
    lines.append("*{0}*".format(footer))
    lines.append("")
    return "\n".join(lines)


#: The last line the harness itself writes. Everything the harness adds goes
#: *around* the authored prose, never into it — a reader of the report should
#: never meet the machinery that assembled it. The footer is the one line that
#: has to state the edition's standing, so it is fixed text and known here.
EDITION_FOOTER_TEXTS = (
    "(AI-assisted edition — figures machine-verified)",
    "(Edisi berbantukan AI — setiap angka disemak oleh mesin)",
)
EDITION_FOOTERS = tuple("*{0}*".format(text) for text in EDITION_FOOTER_TEXTS)
#: The footers of an edition no writer contributed to (deterministic render).
OFFLINE_FOOTER_TEXTS = (
    "(Model report — figures machine-verified)",
    "(Laporan model — setiap angka disemak oleh mesin)",
)
OFFLINE_FOOTERS = tuple("*{0}*".format(text) for text in OFFLINE_FOOTER_TEXTS)
#: A partly authored edition says how much of it a model wrote. Reader-safe
#: wording, no machinery vocabulary: "the rest" is the model-checked text.
PARTIAL_FOOTER_TEXTS = {
    "en": "(AI-assisted edition — {0} of {1} sections AI-authored; the rest "
          "model-checked text)",
    "ms": "(Edisi berbantukan AI — {0} daripada {1} bahagian ditulis AI; selebihnya "
          "teks yang disemak model)",
}
_PARTIAL_FOOTER_RE = re.compile(
    r"^\*\((?:AI-assisted edition — \d+ of \d+ sections AI-authored; the rest "
    r"model-checked text|Edisi berbantukan AI — \d+ daripada \d+ bahagian ditulis "
    r"AI; selebihnya teks yang disemak model)\)\*$")


def partial_footer(authored, total, lang="en"):
    """The footer of a mixed edition: how much of it a model wrote."""
    template = PARTIAL_FOOTER_TEXTS["ms" if lang == "ms" else "en"]
    return template.format(authored, total)


def is_edition_footer(line):
    """True for any footer the harness writes — the anchor for the notes list."""
    text = (line or "").strip()
    return text in EDITION_FOOTERS or text in OFFLINE_FOOTERS or bool(
        _PARTIAL_FOOTER_RE.match(text))


def status_lines(records, lang="en"):
    """Deprecated: the harness's run accounting lives in the manifest.

    Nothing in the published page may carry it, and the manifest fields
    (``verify``/``degraded_sections``) are the audit trail, so this returns the
    manifest-shaped counts only and is never rendered into an edition.
    """
    authored = sum(1 for r in records if r.get("render") == "authored")
    degraded = [r["key"] for r in records if r.get("render") == "degraded"]
    return {
        "sections": len(records),
        "sections_authored": authored,
        "degraded_sections": degraded,
    }


def footnote_transform(markdown, pack, index_map=None):
    """Inline citation markers -> superscript refs + a list of the figures behind them.

    The body stays the smooth authored prose. Every machine-verified figure keeps
    a numbered superscript marker, and the figures behind those markers are listed
    at the end as short human descriptions — the value, its unit and what it
    measures — never an internal identifier.
    """
    counter = {}
    order = []
    def _sub(match):
        cid = match.group(1).strip()
        if cid not in counter:
            counter[cid] = len(counter) + 1
            order.append(cid)
        return "[^{0}]".format(counter[cid])
    transformed = rp.CITATION_RE.sub(_sub, markdown)
    if not order:
        return markdown, []
    lang = pack.get("lang") or "en"
    index = index_map or rp.claim_index(pack)
    notes = ["", "---", "", "## Notes / Catatan", ""]
    for cid in order:
        claim = index.get(cid)
        if not isinstance(claim, dict):
            continue
        notes.append("^{0} {1}".format(counter[cid], cite_line(claim, lang)))
        notes.append("")
    block = "\n".join(notes)
    anchor = None
    offset = 0
    for line in transformed.splitlines(True):
        if is_edition_footer(line):
            anchor = offset
            break
        offset += len(line)
    if anchor is not None:
        sep = transformed.rfind("\n---\n", 0, anchor)
        cut = sep if sep >= 0 else anchor
        transformed = transformed[:cut] + block + "\n" + transformed[cut:]
    else:
        transformed = transformed + "\n" + block
    return transformed, order


#: A unit of measure as a reader of the Malay edition reads it.
UNIT_MS = {
    "seats": "kerusi", "seat": "kerusi", "pp": "mata peratusan",
    "chains": "rantaian", "events": "peristiwa", "items": "item",
    "threads": "rantaian", "%": "%",
}

#: The machinery's own name for the KIND of a value — ``text``, the date a claim
#: was read, the seed of a run. None of them is a unit of measure, so none of
#: them is printed beside a figure the reader reads.
MACHINE_UNITS = ("text", "date", "seed")


def reader_value(value, lang="en"):
    """A pack value as the page prints it: an enum in words, a figure verbatim."""
    text = "" if value is None else str(value)
    mapping = ENUM_VALUES_MS if lang == "ms" else ENUM_VALUES
    return mapping.get(text.strip(), text)


def reader_unit(unit, lang="en"):
    """A unit of measure as the page prints it — machine units are dropped."""
    unit = (unit or "").strip()
    if unit.lower() in MACHINE_UNITS:
        return ""
    if lang == "ms":
        return UNIT_MS.get(unit, unit)
    return unit


def _human_label(claim):
    """The claim's own human label, never a machine identifier.

    Registry tokens and field names are translated here (see ``human_label``),
    which is the one place both the figure sentences and the reader's Notes read
    a label from — so neither can print ``pn_core:`` or ``MACRO reading: gdp_yoy``.
    """
    label = (claim.get("label") or "").strip()
    if not label:
        key = str(claim.get("key") or "").split(":", 1)[0]
        label = LABEL_TOKENS.get(key) or key.replace("_", " ").replace("-", " ").strip()
    return human_label(label) or "-"


def cite_line(claim, lang="en"):
    """One figure as a reader reads it: value, unit, then what it measures.

    The value is the pack's own (a figure verbatim, an enum in words) and the
    unit is a real unit of measure — the machinery's own value kind (``text``,
    ``date``, ``seed``) is never printed beside a figure.
    """
    value = claim.get("value_str")
    if value in (None, ""):
        value = claim.get("value")
    unit = reader_unit(claim.get("unit"), lang)
    value = str(reader_value(value, lang))
    tail = " {0}".format(unit) if unit else ""
    return "{0}{1} — {2}".format(value, tail, _human_label(claim))


def _cite_line_en(claim):
    return cite_line(claim, "en")


def _cite_line_ms(claim):
    return cite_line(claim, "ms")


def _relpath(path):
    if not path:
        return "-"
    try:
        return os.path.relpath(path, ROOT)
    except ValueError:  # pragma: no cover - cross-drive (Windows)
        return path


def output_paths(pack, out_root=None):
    """``(edition path, archive path)`` for this pack — never the deterministic file."""
    root = out_root or REPORT_AI
    if pack.get("kind") == "federal":
        base = os.path.join(root, "federal")
        name = "GE16_Malaysia_General_Election_Report"
    else:
        base = os.path.join(root, "states", "DUN {0}".format(pack.get("state")))
        name = "GE16_{0}_Report".format(pack.get("state"))
    suffix = "_MS" if pack.get("lang") == "ms" else ""
    edition = os.path.join(base, name + suffix + ".md")
    archive = os.path.join(base, "archive", "GE16-{0}".format(pack.get("as_of")),
                           name + suffix + ".md")
    return edition, archive


def manifest_path(pack, work_root=None):
    root = work_root or WORK_AUTHORING
    scope = "" if pack.get("kind") == "federal" else "-dun-{0}".format(
        pack.get("state", "").lower().replace(" ", "-"))
    return os.path.join(root, "GE16-authoring-{0}{1}-{2}.json".format(
        pack.get("as_of"), scope, pack.get("lang")))


def write_outputs(pack, markdown, records, config, offline, out_root=None,
                  work_root=None, writer_label=None, pack_path=None,
                  publish=True, chain=None, skipped_reason=None):
    """Publish the edition + archive it + write the authoring manifest.

    ``publish=False`` writes ONLY the manifest: a live pass in which no writer
    verified a single section leaves the previous — verified — edition where it
    is and records the failure instead of overwriting it with a less accurate
    file. The manifest is written either way.
    """
    edition, archive = output_paths(pack, out_root)
    if publish:
        for path in (edition, archive):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(markdown)
    authored = authored_count(records)
    mode = edition_mode(records, offline)
    if not publish:
        edition_mode_name = "not-published"
    elif mode == "offline":
        edition_mode_name = "deterministic-render"
    else:
        edition_mode_name = "ai-authored"
    lines = [line for line in (markdown or "").splitlines() if line.strip()]
    manifest = {
        "kind": pack.get("kind"),
        "state": pack.get("state"),
        "lang": pack.get("lang"),
        "as_of": pack.get("as_of"),
        "mode": mode,
        "intent": "offline" if offline else "live",
        "edition_mode": edition_mode_name,
        "published": bool(publish),
        "skipped_reason": skipped_reason,
        "title": lines[0] if lines else "",
        "footer": lines[-1] if lines else "",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "writer": writer_identity(config) if not offline else {
            "provider": "offline", "model": "deterministic-pack-deck"},
        "writer_config_hash": writer_config_hash(config),
        "writer_chain": [writer_identity(item) for item in (chain or [config])]
                        if not offline else [],
        "writer_chain_hash": writer_chain_hash(chain) if (chain and not offline) else "",
        "writer_label": writer_label,
        "pack": {"path": _relpath(pack_path), "hash": pack.get("pack_hash"),
                 "claims": pack.get("counts", {}).get("claims"),
                 "as_of": pack.get("as_of")},
        "outputs": {"edition": _relpath(edition), "archive": _relpath(archive)},
        "sections": [{"key": r.get("key"), "number": r.get("number"),
                      "title": r.get("title"), "claims": r.get("claims"),
                      "attempts": r.get("attempts"), "render": r.get("render"),
                      "chars": r.get("chars"), "violations": r.get("violations"),
                      "error": r.get("error"), "writer": r.get("writer"),
                      "writers_tried": r.get("writers_tried") or [],
                      "chunks": r.get("chunks") or []} for r in records],
        "verify": {
            "sections": len(records),
            "authored": sum(1 for r in records if r.get("render") == "authored"),
            "chunked_partial": sum(1 for r in records
                                   if r.get("render") == "chunked-partial"),
            "ai_assisted": authored,
            "offline_fallback": sum(1 for r in records
                                    if r.get("render") == "offline-fallback"),
            "degraded": sum(1 for r in records if r.get("render") == "degraded"),
            "violations_total": sum(len(r.get("violations") or []) for r in records),
        },
        "degraded_sections": [r.get("key") for r in records
                              if r.get("render") == "degraded"],
    }
    path = manifest_path(pack, work_root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2))
        handle.write("\n")
    return edition, archive, path, manifest


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------

def run_authoring(lang="en", state=None, sections=None, offline=False, config=None,
                  pack=None, pack_path=None, out_root=None, work_root=None,
                  pack_root=None, retries=DEFAULT_RETRIES, emit_pack=True,
                  config_path=None, chain=None):
    """One authoring pass. Returns a result dict (also used by the tests).

    The pass language selects its own writer chain (``languages.<lang>`` plus its
    ``fallbacks`` in the config file), so the manifest always records the model
    that actually wrote THIS edition — section by section.

    Publishing follows the mode: a live pass that produced no authored section
    writes its manifest and NOTHING else, so a good previous edition is never
    replaced by a deterministic render.
    """
    explicit = config is not None
    cfg = config or load_writer_config(config_path, lang=lang)
    if offline:
        chain_cfgs = []
    elif explicit and chain is None:
        chain_cfgs = [cfg]
    else:
        chain_cfgs = chain or load_writer_chain(config_path, lang=lang)
    writers = [LiveWriter(item) for item in chain_cfgs]
    if pack is None:
        if pack_path:
            pack = rp.load_pack(pack_path)
        else:
            pack = rp.build_pack(lang=lang, state=state)
            if emit_pack:
                pack_path = rp.write_pack(pack, root=pack_root)
    writer = None if offline else (writers[0] if writers else None)
    label = ("offline (deterministic render)" if offline
             else " -> ".join(writer_label_of(item) for item in writers))
    markdown, records = author_document(
        pack, writer=writer, lang=lang, sections=sections, offline=offline,
        retries=retries, writer_label=label, pack_path=pack_path,
        writers=writers)
    markdown, footnote_order = footnote_transform(markdown, pack)
    # A live pass in which no writer verified a section publishes nothing: the
    # previous edition was verified, this one would be a deterministic render of
    # the same facts under an AI title. The manifest records the failure.
    authored = authored_count(records)
    publish = bool(offline) or authored > 0
    skipped_reason = None
    if not publish:
        skipped_reason = ("no section passed verification with any writer in the "
                          "chain; the previous edition is left in place")
    edition, archive, manifest, manifest_data = write_outputs(
        pack, markdown, records, cfg, offline, out_root=out_root,
        work_root=work_root, writer_label=label, pack_path=pack_path,
        publish=publish, chain=chain_cfgs, skipped_reason=skipped_reason)
    return {
        "pack": pack, "pack_path": pack_path, "markdown": markdown,
        "records": records,
        "edition": edition if publish else None,
        "archive": archive if publish else None,
        "intended_edition": edition,
        "manifest": manifest, "manifest_data": manifest_data,
        "published": publish, "skipped_reason": skipped_reason,
        "writers": chain_cfgs,
        "writer": writer_identity(cfg) if not offline else {"provider": "offline"},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Author the AI edition of the GE16 report from the reference pack.")
    parser.add_argument("--lang", default="en", choices=("en", "ms"))
    parser.add_argument("--state", default=None, help="one state (DUN scope)")
    parser.add_argument("--all-states", action="store_true", help="federal + all 13 states")
    parser.add_argument("--sections", default=None,
                        help="comma-separated section keys (default: all)")
    parser.add_argument("--offline", action="store_true",
                        help="deterministic pack deck only — never calls a model")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    parser.add_argument("--config", default=None, help="authoring_config.json path")
    parser.add_argument("--pack", default=None, help="use an existing pack JSON")
    parser.add_argument("--pack-root", default=None, help="where to emit the pack")
    parser.add_argument("--out-root", default=None, help="edition root (default 03_REPORTS/ai)")
    parser.add_argument("--work-root", default=None, help="manifest root (default work/reports/authoring)")
    parser.add_argument("--no-emit-pack", action="store_true",
                        help="do not write the pack JSON")
    parser.add_argument("--list-sections", action="store_true",
                        help="print the pack's section keys and exit")
    args = parser.parse_args(argv)

    sections = [part.strip() for part in (args.sections or "").split(",") if part.strip()]
    states = []
    if args.all_states:
        states = list(rp.ALL_STATES)
    elif args.state:
        states = [args.state]
    targets = states or [None]

    if args.list_sections:
        pack = rp.load_pack(args.pack) if args.pack else rp.build_pack(
            lang=args.lang, state=states[0] if states else None)
        for section in pack.get("sections", []):
            print("{0:<22} {1:>4} claims  {2}".format(
                section.get("key"), len(section.get("claim_ids", [])),
                section.get("title_en")))
        return 0

    exit_code = 0
    for state in targets:
        result = run_authoring(
            lang=args.lang, state=state, sections=sections, offline=args.offline,
            config_path=args.config, pack_path=args.pack,
            out_root=args.out_root, work_root=args.work_root,
            pack_root=args.pack_root, retries=args.retries,
            emit_pack=not args.no_emit_pack)
        verify = result["manifest_data"]["verify"]
        chain = result.get("writers") or []
        if result.get("published"):
            print("edition  {0}".format(_relpath(result["edition"])))
            print("archive  {0}".format(_relpath(result["archive"])))
        else:
            print("edition  NOT PUBLISHED — {0}".format(result.get("skipped_reason")))
            print("intended {0}".format(_relpath(result.get("intended_edition"))))
        print("manifest {0}".format(_relpath(result["manifest"])))
        print("pack     {0} ({1})".format(_relpath(result["pack_path"] or ""),
                                          str(result["pack"].get("pack_hash"))[:12]))
        print("writers  {0}".format(" -> ".join(writer_label_of(item) for item in
                                                [LiveWriter(item) for item in chain])
                                    or "offline (deterministic render)"))
        print("sections authored {0} (partly authored {1}) / offline {2} / degraded {3} / "
              "violations {4}".format(
                  verify["authored"], verify["chunked_partial"], verify["offline_fallback"],
                  verify["degraded"], verify["violations_total"]))
        for record in result["records"]:
            if record.get("writer") or record.get("writers_tried"):
                print("  {0:<22} {1:<16} {2}".format(
                    record.get("key"), record.get("render"),
                    record.get("writer") or "tried: {0}".format(
                        ", ".join(record.get("writers_tried") or []))))
        # NO authored-rate threshold: a thin or empty pass is recorded in the
        # manifest (and, when nothing at all verified, publishes nothing) rather
        # than failed. The only hard failure left is a chain in which every writer
        # was unreachable — nothing was even attempted against the page.
        errors = [r.get("error") for r in result["records"] if r.get("error")]
        if (not args.offline and not verify["ai_assisted"] and errors
                and len(errors) == len(result["records"])):
            raise AuthoringError(
                "every section failed on the writer transport; first error: {0}".format(
                    errors[0]))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())