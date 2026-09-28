#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GE16 Reference Pack — the deterministic claim ledger authored prose may cite.

WHY THIS EXISTS
---------------
The deterministic builders (``report_builder.py`` / ``state_report_builder.py``)
compute every number themselves, so the renderer and the data can never drift.
An AI-authored layer cannot do that: a language model writing prose will happily
invent "146 seats" or "P999". This module removes that freedom. It extracts
EVERY number, date, seat code, threading fact and ledger row an authored
sentence is allowed to cite, gives each one a stable id, and emits the result as
one JSON document — the *reference pack*.

The pack is:
  - deterministic (no wall clock, no LLM, sorted output) — same inputs produce a
    byte-identical pack, so a pack hash can be quoted in an authoring manifest;
  - complete for the authored sections (every family the section templates need);
  - the only source of truth the authoring harness lets a writer quote from:
    each claim carries ``value``, ``value_str`` (the exact token text), ``unit``,
    ``as_of``/``source`` pointer and a stable ``claim_id``.

CLAIM ID SCHEME
---------------
``<scope>:<key>:<value_str>`` — scope is ``fed`` (federal) or ``dun`` (one
state), key is the dotted/slugged fact key, and the last segment is the exact
rendered value, e.g. ``fed:P50:139``, ``fed:bloc:PN:81``,
``dun:kedah:seats_federal:15``. Because the value is IN the id, a claim_id is
self-verifying: ``claim_index(pack)[claim_id]`` must equal the value the author
quoted. Duplicate ids (same key AND same value) get a ``~2`` suffix in
deterministic insertion order so ids stay unique without reordering facts.

CLI
---
  .venv/bin/python 02_FORECAST/engine/reference_pack.py                 # federal EN
  .venv/bin/python 02_FORECAST/engine/reference_pack.py --lang ms
  .venv/bin/python 02_FORECAST/engine/reference_pack.py --state Kedah
  .venv/bin/python 02_FORECAST/engine/reference_pack.py --all-states
Output: ``work/reports/reference/GE16-reference-<as_of>-<lang>[-dun-<State>].json``

NOTHING here calls a model and nothing here writes to the deterministic report
editions under 03_REPORTS/{federal,states}.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:  # report_builder/data_roots use flat engine imports
    sys.path.insert(0, HERE)

import report_builder as rb            # noqa: E402  federal data spine readers
import state_report_builder as srb     # noqa: E402  per-state readers

PACK_VERSION = "1.0"

ROOT = os.path.dirname(os.path.dirname(HERE))
WORK_REFERENCE = os.path.join(ROOT, "work", "reports", "reference")

#: Blocs that sit on the government side of the 112-seat line (mirrors
#: report_builder.build_report's ``govt_blocs`` — kept in sync by test).
GOVT_BLOCS = ("PH", "BN", "GPS", "GRS", "WARISAN", "MUDA", "KDM", "PBM")
OPP_BLOCS = ("PN", "IND")

#: The Malaysian simple-majority and two-thirds lines over the 222-seat house.
MAJORITY_SEATS = 112
TWO_THIRDS_SEATS = 148

#: Seat-type thresholds (malay_pct) used by compute_demog_stats.
SEAT_TYPE_THRESHOLDS = (
    ("pn_core", 80.0, None, ">80% Malay"),
    ("mixed_malay", 55.0, 80.0, "55-80% Malay"),
    ("true_mixed", 30.0, 55.0, "30-55% Malay"),
    ("non_malay", None, 30.0, "<30% Malay"),
)

#: Battleground tier label -> slug used in claim keys.
TIER_SLUGS = (
    ("SUPER", "super_marginal"),
    ("HIGH", "high_risk"),
    ("WATCH", "watch"),
)

#: Per-section selection rules: (family, limit) — ``limit`` caps the number of
#: distinct claim *groups* (e.g. stories, seats, signals) drawn from a family.
FEDERAL_SECTIONS = (
    {
        "key": "intro_context",
        "number": "1",
        "title_en": "Introduction & Context",
        "title_ms": "Pengenalan & Konteks",
        "brief_en": "Frame the election and the forecast: what the model projects, "
                    "how many seats change hands, and what the government's cushion is.",
        "brief_ms": "Bingkai pilihan raya dan unjuran: apa yang model ramalkan, "
                    "berapa kerusi bertukar tangan, dan berapa selesa kedudukan kerajaan.",
        "rules": (("projection", None), ("bloc", None), ("threshold", None), ("meta", None)),
    },
    {
        "key": "this_week",
        "number": "2",
        "title_en": "This Week's Signals",
        "title_ms": "Isyarat Minggu Ini",
        "brief_en": "One paragraph: what the tracker feeds carried this week and how "
                    "each item was classified (scenario / shock / macro / swing / noise).",
        "brief_ms": "Satu perenggan: apa yang suapan pemantau bawa minggu ini dan "
                    "bagaimana setiap item dikelaskan (senario / kejutan / makro / ayunan / bunyi).",
        "rules": (("signal", 12), ("meta", None)),
    },
    {
        "key": "electorate",
        "number": "3",
        "title_en": "The Electorate",
        "title_ms": "Pengundi",
        "brief_en": "The electorate the forecast is modelling: size, ethnic mix, seat-type "
                    "structure and how many seats sit in each type.",
        "brief_ms": "Pengundi yang diunjurkan: saiz, komposisi etnik, struktur jenis kerusi "
                    "dan berapa kerusi dalam setiap jenis.",
        "rules": (("electorate", None), ("seat_type_count", None), ("threshold", None)),
    },
    {
        "key": "electorate_dynamics",
        "number": "4",
        "title_en": "Electorate Dynamics",
        "title_ms": "Dinamik Pengundi",
        "brief_en": "What has moved since GE15: state swings by bloc, the era split "
                    "(2023 green wave vs 2025-26 resurgence) and the macro readings.",
        "brief_ms": "Apa yang berubah sejak PRU15: ayunan negeri mengikut blok, pecahan era "
                    "(gelombang hijau 2023 vs kebangkitan 2025-26) dan bacaan makro.",
        "rules": (("swing", None), ("macro", None), ("threshold", None)),
    },
    {
        "key": "story_threads_intro",
        "number": "5",
        "title_en": "Political Developments This Cycle",
        "title_ms": "Perkembangan Politik Kitaran Ini",
        "brief_en": "Open the cycle's political narrative: name the three chains of related "
                    "events that are carrying it, each with its latest development and the "
                    "date of that development. Close with a one-line lead-in to the brief "
                    "list that follows. Do not list every chain here.",
        "brief_ms": "Buka naratif politik kitaran ini: namakan tiga rantaian peristiwa "
                    "berkaitan yang membawanya, setiap satu dengan perkembangan terkini dan "
                    "tarikh perkembangan itu. Akhiri dengan satu baris pengenalan kepada "
                    "senarai ringkas yang menyusul. Jangan senaraikan semua rantaian di sini.",
        "rules": (("story", 3),),
    },
    {
        "key": "story_threads_items",
        "number": "6",
        "title_en": "Key Developments in Brief",
        "title_ms": "Perkembangan Utama Secara Ringkas",
        "brief_en": "One short line per chain of related events: its headline, the latest "
                    "development in that chain and the date of that development — every "
                    "value taken verbatim from the pack.",
        "brief_ms": "Satu baris ringkas bagi setiap rantaian peristiwa: tajuknya, "
                    "perkembangan terkini dalam rantaian itu dan tarikh perkembangan itu — "
                    "setiap nilai diambil verbatim daripada pek.",
        "rules": (("story", 20),),
    },
    {
        "key": "scenario_narrative",
        "number": "7",
        "title_en": "Scenario Narrative",
        "title_ms": "Naratif Senario",
        "brief_en": "Walk the parametric scenarios: what each assumes, where the government "
                    "lands, and whether the majority survives in all of them.",
        "brief_ms": "Huraikan senario parametrik: apa andaian masing-masing, di mana kerajaan "
                    "mendarat, dan sama ada majoriti bertahan dalam semua senario.",
        "rules": (("scenario", None), ("projection", None), ("threshold", None)),
    },
    {
        "key": "watch_list",
        "number": "8",
        "title_en": "Watch-List Seats",
        "title_ms": "Kerusi Senarai Pantau",
        "brief_en": "Prose for the battleground seats: tier, seat code, constituency, state, "
                    "GE15 and projected winner and the projected margin.",
        "brief_ms": "Prosa untuk kerusi medan pertempuran: tier, kod kerusi, nama kerusi, "
                    "negeri, pemenang PRU15 dan unjuran serta margin unjuran.",
        "rules": (("watch", 37), ("threshold", None)),
    },
    {
        "key": "closing",
        "number": "9",
        "title_en": "Closing",
        "title_ms": "Penutup",
        "brief_en": "Close the report: what the projection does and does not claim, and "
                    "what would most move the result before polling day.",
        "brief_ms": "Tutup laporan: apa yang unjuran dakwa dan tidak dakwa, serta apa yang "
                    "paling boleh mengubah keputusan sebelum hari pengundian.",
        "rules": (("projection", None), ("bloc", None), ("scenario", 2), ("threshold", None)),
    },
)

STATE_SECTIONS = (
    {
        "key": "intro_context",
        "number": "1",
        "title_en": "Introduction & Context",
        "title_ms": "Pengenalan & Konteks",
        "brief_en": "Frame this state's place in the national forecast: how many "
                    "parliamentary seats it holds and how they are projected to fall.",
        "brief_ms": "Bingkai kedudukan negeri ini dalam unjuran kebangsaan: berapa kerusi "
                    "parlimen dan bagaimana ia diunjurkan jatuh.",
        "rules": (("projection", None), ("bloc", None), ("threshold", None), ("meta", None)),
    },
    {
        "key": "this_week",
        "number": "2",
        "title_en": "This Week's Signals",
        "title_ms": "Isyarat Minggu Ini",
        "brief_en": "One paragraph on the tracker items touching this state and how they "
                    "were classified.",
        "brief_ms": "Satu perenggan tentang item pemantau yang menyentuh negeri ini dan "
                    "bagaimana ia dikelaskan.",
        "rules": (("signal", 12), ("meta", None)),
    },
    {
        "key": "electorate",
        "number": "3",
        "title_en": "The Electorate",
        "title_ms": "Pengundi",
        "brief_en": "This state's electorate and its seat-type structure.",
        "brief_ms": "Pengundi negeri ini dan struktur jenis kerusinya.",
        "rules": (("electorate", None), ("seat_type_count", None), ("threshold", None)),
    },
    {
        "key": "electorate_dynamics",
        "number": "4",
        "title_en": "Electorate Dynamics",
        "title_ms": "Dinamik Pengundi",
        "brief_en": "What moved in this state since GE15: the state swing by bloc and the "
                    "national macro readings that feed the model.",
        "brief_ms": "Apa yang berubah di negeri ini sejak PRU15: ayunan mengikut blok dan "
                    "bacaan makro kebangsaan yang memacu model.",
        "rules": (("swing", None), ("macro", None), ("threshold", None)),
    },
    {
        "key": "story_threads_intro",
        "number": "5",
        "title_en": "Political Developments This Cycle",
        "title_ms": "Perkembangan Politik Kitaran Ini",
        "brief_en": "Open this state's political narrative: name the three chains of related "
                    "events that are carrying it, each with its latest development and the "
                    "date of that development, then lead in to the brief list that follows.",
        "brief_ms": "Buka naratif politik negeri ini: namakan tiga rantaian peristiwa "
                    "berkaitan yang membawanya, setiap satu dengan perkembangan terkini dan "
                    "tarikh perkembangan itu, kemudian bawa pembaca kepada senarai ringkas "
                    "yang menyusul.",
        "rules": (("story", 3),),
    },
    {
        "key": "story_threads_items",
        "number": "6",
        "title_en": "Key Developments in Brief",
        "title_ms": "Perkembangan Utama Secara Ringkas",
        "brief_en": "One short line per chain of related events in this state: its headline, "
                    "the latest development in that chain and the date of that development.",
        "brief_ms": "Satu baris ringkas bagi setiap rantaian peristiwa di negeri ini: "
                    "tajuknya, perkembangan terkini dalam rantaian itu dan tarikh "
                    "perkembangan itu.",
        "rules": (("story", 20),),
    },
    {
        "key": "scenario_narrative",
        "number": "7",
        "title_en": "Scenario Narrative",
        "title_ms": "Naratif Senario",
        "brief_en": "How the national scenarios reallocate this state's parliamentary seats.",
        "brief_ms": "Bagaimana senario kebangsaan mengagihkan semula kerusi parlimen negeri ini.",
        "rules": (("scenario", None), ("projection", None), ("threshold", None)),
    },
    {
        "key": "watch_list",
        "number": "8",
        "title_en": "Watch-List Seats",
        "title_ms": "Kerusi Senarai Pantau",
        "brief_en": "The battleground seats inside this state, with tier, code, winner and margin.",
        "brief_ms": "Kerusi medan pertempuran dalam negeri ini, dengan tier, kod, pemenang dan margin.",
        "rules": (("watch", 37), ("threshold", None)),
    },
    {
        "key": "closing",
        "number": "9",
        "title_en": "Closing",
        "title_ms": "Penutup",
        "brief_en": "Close the state section: what the projection claims and what would move it.",
        "brief_ms": "Tutup bahagian negeri: apa yang unjuran dakwa dan apa yang boleh mengubahnya.",
        "rules": (("projection", None), ("bloc", None), ("scenario", 2), ("threshold", None)),
    },
)


# ---------------------------------------------------------------------------
# value rendering + claim ledger
# ---------------------------------------------------------------------------

def _fmt(value, dp=2):
    """Canonical quotable text for a value (ints bare, floats trimmed to >=1 dp)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value
    text = "{0:.{1}f}".format(float(value), dp)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
        if "." not in text:
            text += ".0"
    return text


def _whole(value):
    """A float seat count rendered as a bare integer (139.0 -> 139)."""
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return value


def _slug(text, limit=48):
    text = re.sub(r"[^0-9A-Za-z]+", "_", str(text)).strip("_").lower()
    return text[:limit] or "x"


def _id_safe(text, limit=96):
    """A value segment that can live inside a claim id.

    Bracket characters would break the inline-citation syntax the harness
    renders (``[fed:P50:139]``) and newlines would break the manifest, so an
    embedded value is flattened; the claim's own ``value_str`` keeps the
    original bytes for verification.
    """
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    flat = flat.replace("[", "(").replace("]", ")").replace("\n", " ")
    return flat[:limit].strip() or "x"


class _Ledger:
    """Ordered claim ledger with deterministic id de-duplication."""

    def __init__(self, scope):
        self.scope = scope
        self.claims = []
        self._used = {}
        #: Vintage stamped on any claim whose builder does not name one; set by
        #: the pack builders once the forecast's data vintage is known.
        self.default_as_of = ""

    def add(self, key, value, unit, family, source, group="",
            value_str=None, label="", aliases=(), as_of=""):
        if value is None:
            return None
        as_of = as_of or self.default_as_of
        text = value_str if value_str is not None else _fmt(value)
        base = "{0}:{1}:{2}".format(self.scope, key, _id_safe(text))
        seq = self._used.get(base, 0) + 1
        self._used[base] = seq
        claim_id = base if seq == 1 else "{0}~{1}".format(base, seq)
        claim = {
            "claim_id": claim_id,
            "family": family,
            "key": key,
            "group": group,
            "value": value,
            "value_str": text,
            "unit": unit,
            "label": label,
            "aliases": [a for a in aliases if a],
            "as_of": as_of,
            "source": source,
        }
        self.claims.append(claim)
        return claim_id

    def families(self):
        out = defaultdict(list)
        for claim in self.claims:
            out[claim["family"]].append(claim["claim_id"])
        return {k: v for k, v in out.items()}


def _family_index(claims):
    by_family = defaultdict(list)
    for claim in claims:
        by_family[claim["family"]].append(claim)
    return by_family


def _select_section_claims(claims, rules):
    """Apply (family, limit) rules; ``limit`` counts distinct groups."""
    by_family = _family_index(claims)
    picked = []
    seen = set()
    for family, limit in rules:
        family_claims = by_family.get(family, [])
        if limit is not None:
            groups = []
            for claim in family_claims:
                group = claim["group"] or claim["claim_id"]
                if group not in groups:
                    if len(groups) >= limit:
                        break
                    groups.append(group)
            allowed_groups = set(groups)
            family_claims = [c for c in family_claims
                             if (c["group"] or c["claim_id"]) in allowed_groups]
        for claim in family_claims:
            if claim["claim_id"] not in seen:
                seen.add(claim["claim_id"])
                picked.append(claim["claim_id"])
    return picked


def _build_sections(claims, spec):
    sections = []
    for definition in spec:
        sections.append({
            "key": definition["key"],
            "number": definition["number"],
            "title_en": definition["title_en"],
            "title_ms": definition["title_ms"],
            "brief_en": definition["brief_en"],
            "brief_ms": definition["brief_ms"],
            "claim_ids": _select_section_claims(claims, definition["rules"]),
        })
    return sections


# ---------------------------------------------------------------------------
# source fingerprints
# ---------------------------------------------------------------------------

def _rel(path):
    try:
        return os.path.relpath(str(path), ROOT)
    except ValueError:  # pragma: no cover - different drive
        return str(path)


def _sha256(path):
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def source_fingerprints(paths):
    out = {}
    for name, path in sorted(paths.items()):
        digest = _sha256(path)
        if digest:
            out[name] = {"path": _rel(path), "sha256": digest}
    return out


def _input_paths():
    return {
        "forecast": rb.FORECAST_JSON,
        "scenarios": rb.SCENARIOS,
        "scenario_meta": os.path.join(ROOT, "work", "scenarios", "scenario_meta.json"),
        "battlegrounds": str(rb.BATTLEGROUNDS),
        "master_222": str(rb.MASTER),
        "demog": str(rb.DEMOG),
        "ge15": str(rb.GE15),
        "swings": str(rb.SWINGS),
        "config": rb.CONFIG,
        "events_db": rb.EVENTS_DB,
        "news_feed": str(rb.NEWS_FEED),
    }


def _as_of_date(forecast):
    """The pack's data vintage: the forecast's own generation date (deterministic)."""
    generated = str(forecast.get("generated") or "")[:10]
    if re.match(r"^\d{4}-\d{2}-\d{2}$", generated):
        return generated
    return datetime.now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# shared fact families
# ---------------------------------------------------------------------------

def _seat_type_slug(tier):
    for needle, slug in TIER_SLUGS:
        if needle in (tier or ""):
            return slug
    return _slug(tier or "unclassified")


def _add_threshold_claims(ledger, as_of):
    src = "02_FORECAST/engine/reference_pack.py#threshold"
    ledger.add("threshold:simple_majority", MAJORITY_SEATS, "seats", "threshold", src,
               value_str=str(MAJORITY_SEATS),
               label="Seats needed for a simple majority of the 222-seat house")
    ledger.add("threshold:two_thirds", TWO_THIRDS_SEATS, "seats", "threshold", src,
               label="Seats needed to hold a two-thirds majority")
    for name, floor, ceil, _desc in SEAT_TYPE_THRESHOLDS:
        if floor is not None:
            ledger.add("threshold:{0}_malay_floor".format(name), floor, "% Malay voters",
                       "threshold", src, label="{0}: Malay share floor".format(name))
        if ceil is not None:
            ledger.add("threshold:{0}_malay_ceiling".format(name), ceil, "% Malay voters",
                       "threshold", src, label="{0}: Malay share ceiling".format(name))
    ledger.add("threshold:margin_super_marginal", 1.0, "% margin", "threshold", src,
               label="GE15 margin band: super-marginal")
    ledger.add("threshold:margin_high_risk", 2.5, "% margin", "threshold", src,
               label="GE15 margin band: high-risk")
    ledger.add("threshold:margin_watch", 5.0, "% margin", "threshold", src,
               label="GE15 margin band: watch")
    ledger.add("threshold:youth_age_floor", 18, "years", "threshold", src,
               label="Youngest voter age")
    ledger.add("threshold:youth_age_ceiling", 30, "years", "threshold", src,
               label="Youth cohort upper age bound")
    ledger.add("threshold:mc_iterations", 5000, "iterations", "threshold", src,
               value_str="5,000", aliases=["5000"],
               label="Monte Carlo iterations")
    ledger.add("threshold:mc_seed", 42, "seed", "threshold", src,
               label="Monte Carlo random seed")
    ledger.add("threshold:battleground_total", 36, "seats", "threshold", src,
               label="Seats in the battleground watch universe (margin < 5%)")


def _add_macro_claims(ledger, cfg, as_of):
    src = "02_FORECAST/engine/config.py#MACRO"
    units = {
        "gdp_yoy": "% year-on-year",
        "cpi_yoy": "% year-on-year",
        "ringgit": "MYR per USD",
        "approval_delta": "pp",
        "pm_pref_malay": "pp",
        "pm_pref_nonmalay": "pp",
    }
    for key, value in cfg.MACRO.items():
        # ringgit is a 4-decimal spot rate in the deterministic edition; every
        # other MACRO reading is house-style 1-2 dp.
        dp = 4 if key == "ringgit" else 2
        ledger.add("macro:{0}".format(key), value, units.get(key, ""), "macro", src,
                   as_of=as_of, value_str=_fmt(value, dp=dp),
                   label="MACRO reading: {0}".format(key))
    ledger.add("macro:pm_pref_overall_pct", rb.PM_PREF_OVERALL_PCT, "% of respondents",
               "macro", "02_FORECAST/engine/report_builder.py#PM_PREF_OVERALL_PCT",
               as_of=as_of, label="Merdeka Center national: Anwar overall approval anchor")


def _add_event_shock_claims(ledger, cfg, as_of):
    src = "02_FORECAST/engine/config.py#EVENT_SHOCKS"
    if not cfg.EVENT_SHOCKS:
        ledger.add("event_shocks:none", 0, "seats", "event_shock", src, as_of=as_of,
                   label="EVENT_SHOCKS is empty: no seat-level shocks are carried")
        return
    for seat, blocs in sorted(cfg.EVENT_SHOCKS.items()):
        for bloc, pp in sorted(blocs.items()):
            ledger.add("event_shock:{0}:{1}".format(seat, bloc), pp, "pp", "event_shock",
                       src, group="event_shock:{0}".format(seat), as_of=as_of,
                       label="Seat shock at {0} for {1}".format(seat, bloc))


#: The ledger keys a chain of events by an opaque ``story-<hash>`` id. The id
#: belongs in claim ids and citations; prose a reader sees never carries it.
_STORY_ID_IN_TEXT = re.compile(
    r"\bstory[-_][0-9a-f]{4,}(?![0-9a-z])\s*[:\u2013\u2014-]?\s*", re.IGNORECASE)


def _human_story_text(text):
    """A ledger string as prose: the opaque chain id stripped out."""
    cleaned = _STORY_ID_IN_TEXT.sub("", str(text or ""))
    return re.sub(r"\s{2,}", " ", cleaned).strip(" \u2013\u2014-:")


def _add_vacancy_claims(ledger, cfg, as_of):
    src = "02_FORECAST/engine/config.py#VACANCIES"
    for code, reason in sorted(cfg.VACANCIES.items()):
        ledger.add("vacancy:{0}".format(code), reason, "seat", "vacancy", src,
                   group="vacancy:{0}".format(code), as_of=as_of,
                   label="Vacancy metadata for {0}".format(code))
    if not cfg.VACANCIES:
        ledger.add("vacancy:none", 0, "seats", "vacancy", src, as_of=as_of,
                   label="No vacated seats carried")
    ledger.add("vacancy:count", len(cfg.VACANCIES), "seats", "vacancy", src,
               as_of=as_of, label="Number of seats vacant under the anti-hopping law")


def _add_story_claims(ledger, story_data, as_of, scope_key):
    src = "work/events/ge16-events.db#v_story_current"
    if not story_data.get("available"):
        ledger.add("story_ledger:unavailable", story_data.get("reason", "unavailable"),
                   "text", "story_summary", src, as_of=as_of,
                   label="Political developments could not be read")
        return
    ledger.add("story_summary:total", story_data.get("total", 0), "chains",
               "story_summary", src, as_of=as_of,
               label="Chains of related political events tracked")
    ledger.add("story_summary:open", story_data.get("open", 0), "chains",
               "story_summary", src, as_of=as_of, label="Chains still open")
    ledger.add("story_summary:listed", len(story_data.get("threads", [])), "chains",
               "story_summary", src, as_of=as_of,
               label="Chains detailed in this edition")
    ledger.add("story_summary:held", story_data.get("held", 0), "chains",
               "story_summary", src, as_of=as_of,
               label="Chains dated after this edition's cut-off")
    ledger.add("story_summary:stale_total", story_data.get("stale_total", 0), "chains",
               "story_summary", src, as_of=as_of,
               label="Chains with no development this cycle")
    ledger.add("story_summary:as_of", story_data.get("as_of", ""), "date",
               "story_summary", src, as_of=as_of,
               value_str=story_data.get("as_of", ""),
               label="As of (date of the latest development)")
    for thread in story_data.get("threads", []):
        sid = thread.get("story_id", "")
        group = "story:{0}".format(sid)
        ledger.add("story_headline:{0}".format(sid),
                   _human_story_text(thread.get("headline", "")), "text",
                   "story", src, group=group, as_of=as_of, label="Chain headline")
        ledger.add("story_status:{0}".format(sid), thread.get("status", ""), "text",
                   "story", src, group=group, as_of=as_of, label="Status")
        ledger.add("story_events:{0}".format(sid), thread.get("event_count", 0), "events",
                   "story", src, group=group, as_of=as_of,
                   label="Developments in the chain")
        latest = _human_story_text(thread.get("latest_event_title") or "")
        if latest:
            ledger.add("story_latest:{0}".format(sid), latest, "text", "story", src,
                       group=group, as_of=as_of, label="Latest development")
        first_seen = thread.get("first_seen") or ""
        last_update = thread.get("last_update") or ""
        if first_seen:
            ledger.add("story_first_seen:{0}".format(sid), first_seen, "date", "story",
                       src, group=group, as_of=as_of, value_str=first_seen,
                       label="First development")
        if last_update:
            ledger.add("story_last_update:{0}".format(sid), last_update, "date", "story",
                       src, group=group, as_of=as_of, value_str=last_update,
                       aliases=_date_aliases(last_update),
                       label="Date of the latest development")


def _clean_title(text, limit=140):
    """A tracker headline as a quotable string: no markup or feed-tag noise.

    The state signal reader hands back raw tracker lines (``**feed:Malay Mail **
    Perlis Umno gears up ...``); the pack quotes headlines, so the noise is
    stripped once, here, and the same cleaned string is what the ledger carries.
    """
    cleaned = re.sub(r"\[[^\]]*\]", " ", str(text or ""))
    cleaned = cleaned.replace("**", " ").replace("`", " ").replace("•", " ")
    cleaned = re.sub(r"\bfeed\s*:\s*", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -—–·|")
    return cleaned[:limit].strip()


def _add_signal_claims(ledger, signals, as_of):
    src = "1_DATA/research/trackers/{ge16-poll-tracker-log.md,ge16-candidate-tracker-log.md,ge16-news-feed.json}"
    if not signals:
        ledger.add("signal:none", 0, "items", "signal", src, as_of=as_of,
                   label="No tracker items in the latest scan")
        return
    for index, signal in enumerate(signals, start=1):
        title = _clean_title(signal.get("title") or "")
        category = (signal.get("category") or "").strip()
        if not category:
            category = rb.classify_signal(title)
        group = "signal:{0}".format(index)
        ledger.add("signal_title:{0:02d}".format(index), title, "text", "signal", src,
                   group=group, as_of=as_of,
                   label="Tracker item ({0})".format(signal.get("feed", "")))
        ledger.add("signal_category:{0:02d}".format(index), category, "text", "signal",
                   src, group=group, as_of=as_of, label="Signal classification")


def _date_aliases(iso):
    """Accepted spellings of an ISO date: long form + 3-letter month form."""
    try:
        parsed = datetime.strptime(str(iso)[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        return []
    long_form = "{0} {1} {2}".format(parsed.day, parsed.strftime("%B"), parsed.year)
    short_form = "{0} {1} {2}".format(parsed.day, parsed.strftime("%b"), parsed.year)
    return [long_form] if long_form == short_form else [long_form, short_form]


# ---------------------------------------------------------------------------
# federal pack
# ---------------------------------------------------------------------------

def build_federal_pack(lang="en", as_of=None, root=None):
    """Build the federal claim ledger (deterministic, no LLM)."""
    forecast = rb.load_forecast()
    cfg = rb.load_config()
    scenarios = json.load(open(rb.SCENARIOS, encoding="utf-8"))
    scenario_meta = rb.load_scenario_meta()
    g15 = rb.compute_ge15_stats(rb.load_csv(rb.GE15))
    demog = rb.compute_demog_stats(rb.load_csv(rb.DEMOG))
    bg_stats = rb.compute_battleground_stats(rb.load_csv(rb.BATTLEGROUNDS))
    swing_stats = rb.compute_swing_stats(rb.load_csv(rb.SWINGS))
    sc_stats = rb.compute_scenario_stats(scenarios)

    vintage = as_of or _as_of_date(forecast)
    ledger = _Ledger("fed")
    ledger.default_as_of = vintage
    fc_src = _rel(rb.FORECAST_JSON)

    det = forecast.get("deterministic", {})
    mc = forecast.get("monte_carlo", {})
    flips = forecast.get("flips", [])
    seats = forecast.get("projected_seats", [])

    # ---- meta ------------------------------------------------------------
    ledger.add("pack_version", PACK_VERSION, "", "meta", "reference_pack.py", as_of=vintage,
               label="Model version")
    ledger.add("lang", lang, "", "meta", "reference_pack.py", as_of=vintage,
               label="Edition language")
    ledger.add("kind", "federal", "", "meta", "reference_pack.py", as_of=vintage,
               label="Report scope")
    ledger.add("as_of", vintage, "date", "meta", fc_src, as_of=vintage, value_str=vintage,
               aliases=_date_aliases(vintage), label="Data as of")
    ledger.add("model", forecast.get("model", ""), "text", "meta", fc_src, as_of=vintage,
               label="Forecast model version")
    ledger.add("parliament_seats", forecast.get("parliament_seats", 0), "seats", "meta",
               fc_src, as_of=vintage, label="Parliamentary seats in the house")
    ledger.add("filled_seats", forecast.get("filled_seats", 0), "seats", "meta", fc_src,
               as_of=vintage, label="Seats currently filled")
    ledger.add("vacant_seats", forecast.get("vacant_seats", 0), "seats", "meta", fc_src,
               as_of=vintage, label="Seats currently vacant")

    # ---- projection ------------------------------------------------------
    ledger.add("P10", _whole(mc.get("P10")), "seats", "projection", fc_src, as_of=vintage,
               label="Monte Carlo 10th percentile: government-aligned seats",
               aliases=("P10", "10th percentile", "peratus ke-10", "persentil ke-10"))
    ledger.add("P50", _whole(mc.get("P50")), "seats", "projection", fc_src, as_of=vintage,
               label="Monte Carlo 50th percentile: government-aligned seats",
               aliases=("P50", "median", "peratus ke-50", "persentil ke-50"))
    ledger.add("P90", _whole(mc.get("P90")), "seats", "projection", fc_src, as_of=vintage,
               label="Monte Carlo 90th percentile: government-aligned seats",
               aliases=("P90", "90th percentile", "peratus ke-90", "persentil ke-90"))
    p_majority = mc.get("P_majority")
    if p_majority is not None:
        ledger.add("P_majority", int(round(float(p_majority) * 100)), "%", "projection",
                   fc_src, as_of=vintage,
                   label="Probability the government retains the simple majority")
    ledger.add("flips_P50", _whole(mc.get("flips_P50")), "seats", "projection", fc_src,
               as_of=vintage, label="Seats changing hands at the median")
    ledger.add("govt_expected", forecast.get("govt_expected"), "seats", "projection",
               fc_src, as_of=vintage, label="Model central estimate: government-aligned seats")
    ledger.add("economic_term", forecast.get("economic_term"), "pp", "projection", fc_src,
               as_of=vintage, label="Economic term applied by the model")
    ledger.add("flips_total", len(flips), "seats", "projection", fc_src, as_of=vintage,
               label="Seats projected to change hands")

    # ---- blocs -----------------------------------------------------------
    govt_actual = sum(det.get(b, 0) for b in GOVT_BLOCS)
    opp_actual = sum(det.get(b, 0) for b in OPP_BLOCS)
    for bloc in sorted(det, key=lambda b: (-det[b], b)):
        ledger.add("bloc:{0}".format(bloc), det[bloc], "seats", "bloc", fc_src,
                   group="bloc:{0}".format(bloc), as_of=vintage,
                   label="Projected seats for {0} (model central estimate)".format(bloc))
    ledger.add("bloc_total:government", govt_actual, "seats", "bloc", fc_src,
               as_of=vintage, label="Government-aligned seats, model central estimate")
    ledger.add("bloc_total:opposition", opp_actual, "seats", "bloc", fc_src,
               as_of=vintage, label="Opposition seats, model central estimate")
    ledger.add("bloc_total:ge15_government", g15["govt_seats"], "seats", "bloc",
               _rel(rb.GE15), as_of=vintage, label="Government-aligned seats after GE15")
    ledger.add("bloc_total:ge15_opposition", g15["opp_seats"], "seats", "bloc",
               _rel(rb.GE15), as_of=vintage, label="Opposition seats after GE15")
    for bloc, count in sorted(g15["bloc_seats"].items(), key=lambda kv: (-kv[1], kv[0])):
        ledger.add("ge15_bloc:{0}".format(bloc), count, "seats", "bloc", _rel(rb.GE15),
                   group="ge15_bloc:{0}".format(bloc), as_of=vintage,
                   label="GE15 seats won by {0}".format(bloc))
    ledger.add("bloc_change:government", govt_actual - g15["govt_seats"], "seats", "bloc",
               fc_src, as_of=vintage,
               label="Net government seat change from GE15 to the projection",
               value_str="{:d}".format(govt_actual - g15["govt_seats"]))

    # ---- electorate ------------------------------------------------------
    for key in ("total_electorate", "east_electorate", "total_voters"):
        if key in demog:
            ledger.add("electorate:{0}".format(key), demog[key], "voters", "electorate",
                       _rel(rb.DEMOG), as_of=vintage,
                       label="Electorate: {0}".format(key))
    ledger.add("electorate:total_voters_ge15", g15["total_voters"], "voters", "electorate",
               _rel(rb.GE15), as_of=vintage, label="Registered voters at GE15")
    ledger.add("electorate:total_valid", g15["total_valid"], "votes", "electorate",
               _rel(rb.GE15), as_of=vintage, label="Valid votes cast at GE15")
    for key in ("wt_malay", "wt_chinese", "wt_indian", "wt_bumi_sabah", "wt_bumi_sarawak",
                "youth_pct", "age_31_40", "age_41_50", "age_51_60", "age_60plus",
                "avg_median_age"):
        ledger.add("electorate:{0}".format(key), demog[key], "%", "electorate",
                   _rel(rb.DEMOG), as_of=vintage,
                   label="Electorate share/demographic: {0}".format(key))
    ledger.add("electorate:east_seats", demog["east_seats"], "seats", "electorate",
               _rel(rb.DEMOG), as_of=vintage, label="Sabah + Sarawak seats")
    for key in ("margin_under_1", "margin_under_2_5", "margin_under_5"):
        ledger.add("electorate:{0}".format(key), g15[key], "seats", "electorate",
                   _rel(rb.GE15), as_of=vintage,
                   label="GE15 seats inside the {0} margin band".format(key))
    ledger.add("electorate:avg_margin", g15["avg_margin"], "% margin", "electorate",
               _rel(rb.GE15), as_of=vintage, label="Average GE15 winning margin")
    ledger.add("electorate:total_seats", g15["total_seats"], "seats", "electorate",
               _rel(rb.GE15), as_of=vintage, label="Seats with a GE15 result")

    # seat-type counts
    ledger.add("seat_type_count:east_malaysia", demog["east_seats"], "seats",
               "seat_type_count", _rel(rb.DEMOG), as_of=vintage,
               label="Seats typed east-malaysia")
    for key in ("pn_core", "mixed_malay", "true_mixed", "non_malay"):
        ledger.add("seat_type_count:{0}".format(key), demog[key], "seats",
                   "seat_type_count", _rel(rb.DEMOG), group="seat_type:{0}".format(key),
                   as_of=vintage, label="Seats typed {0}".format(key))

    # ---- swings ----------------------------------------------------------
    for state in sorted(swing_stats["by_state"]):
        for bloc, pp in sorted(swing_stats["by_state"][state].items()):
            if bloc == "latest_date":
                continue
            ledger.add("swing:{0}:{1}".format(_slug(state), bloc), pp, "pp", "swing",
                       _rel(rb.SWINGS), group="swing:{0}".format(_slug(state)),
                       as_of=vintage, value_str=_fmt(pp, dp=1),
                       aliases=["+{0}".format(_fmt(pp, dp=1))],
                       label="{0} swing since GE15: {1}".format(state, bloc))
        latest = swing_stats["by_state"][state].get("latest_date")
        if latest:
            ledger.add("swing_date:{0}".format(_slug(state)), latest, "date", "swing",
                       _rel(rb.SWINGS), group="swing:{0}".format(_slug(state)),
                       as_of=vintage, value_str=latest, label="Date of the {0} reading".format(state))
    ledger.add("swing_green_wave_states", len(swing_stats["green_wave_states"]), "states",
               "swing", _rel(rb.SWINGS), as_of=vintage,
               label="States whose latest swing reading is from 2023")
    ledger.add("swing_resurgence_states", len(swing_stats["resurgence_states"]), "states",
               "swing", _rel(rb.SWINGS), as_of=vintage,
               label="States whose latest swing reading is from 2025-26")

    # ---- macro / shocks / vacancies --------------------------------------
    _add_macro_claims(ledger, cfg, vintage)
    _add_event_shock_claims(ledger, cfg, vintage)
    _add_vacancy_claims(ledger, cfg, vintage)
    _add_threshold_claims(ledger, vintage)

    # ---- scenarios -------------------------------------------------------
    for index, scenario in enumerate(sc_stats, start=1):
        family = scenario_meta.get(scenario["name"], {}).get("category", "parametric")
        group = "scenario:{0}".format(index)
        ledger.add("scenario:{0:02d}:name".format(index), scenario["name"], "text",
                   "scenario", _rel(rb.SCENARIOS), group=group, as_of=vintage,
                   label="Scenario name ({0})".format(family))
        ledger.add("scenario:{0:02d}:government".format(index), scenario["govt"], "seats",
                   "scenario", _rel(rb.SCENARIOS), group=group, as_of=vintage,
                   label="Government-aligned seats under '{0}'".format(scenario["name"]))
        ledger.add("scenario:{0:02d}:opposition".format(index), scenario["opp"], "seats",
                   "scenario", _rel(rb.SCENARIOS), group=group, as_of=vintage,
                   label="Opposition seats under '{0}'".format(scenario["name"]))
        ledger.add("scenario:{0:02d}:pn".format(index), scenario["pn"], "seats",
                   "scenario", _rel(rb.SCENARIOS), group=group, as_of=vintage,
                   label="PN seats under '{0}'".format(scenario["name"]))

    # ---- flips -----------------------------------------------------------
    directions = defaultdict(int)
    for flip in flips:
        code = flip.get("code", "")
        group = "flip:{0}".format(code)
        direction = "{0}_TO_{1}".format(flip.get("ge15_winner", ""), flip.get("proj_winner", ""))
        directions[direction] += 1
        ledger.add("flip:{0}".format(code), flip.get("proj_winner", ""), "bloc", "flip",
                   fc_src, group=group, as_of=vintage,
                   label="{0} ({1}): {2} -> {3}".format(
                       code, flip.get("constituency", ""), flip.get("ge15_winner", ""),
                       flip.get("proj_winner", "")))
        ledger.add("flip_constituency:{0}".format(code), flip.get("constituency", ""),
                   "text", "flip", fc_src, group=group, as_of=vintage,
                   label="Constituency name")
        ledger.add("flip_state:{0}".format(code), flip.get("state", ""), "text", "flip",
                   fc_src, group=group, as_of=vintage, label="State of the flip")
        ledger.add("flip_margin:{0}".format(code), flip.get("proj_margin"), "% margin",
                   "flip", fc_src, group=group, as_of=vintage,
                   label="Projected winning margin")
    for direction in sorted(directions):
        ledger.add("flip_dir:{0}".format(direction), directions[direction], "seats",
                   "flip", fc_src, group="flip_direction:{0}".format(direction),
                   as_of=vintage, label="Flips in direction {0}".format(direction.replace("_TO_", " -> ")))

    # ---- seats (per claimable seat) --------------------------------------
    seat_types = defaultdict(int)
    state_seat_counts = defaultdict(int)
    for seat in seats:
        code = seat.get("code", "")
        group = "seat:{0}".format(code)
        seat_types[seat.get("seat_type", "")] += 1
        state_seat_counts[seat.get("state", "")] += 1
        ledger.add("seat_proj:{0}".format(code), seat.get("proj_winner", ""), "bloc",
                   "seat", fc_src, group=group, as_of=vintage,
                   label="{0} {1}: projected winner".format(code, seat.get("constituency", "")))
        ledger.add("seat_ge15:{0}".format(code), seat.get("ge15_winner", ""), "bloc",
                   "seat", fc_src, group=group, as_of=vintage, label="GE15 winner")
        ledger.add("seat_margin:{0}".format(code), seat.get("proj_margin"), "% margin",
                   "seat", fc_src, group=group, as_of=vintage,
                   label="Projected winning margin")
        ledger.add("seat_type:{0}".format(code), seat.get("seat_type", ""), "text", "seat",
                   fc_src, group=group, as_of=vintage, label="Seat-type descriptor")
    for seat_type, count in sorted(seat_types.items()):
        ledger.add("seat_type_total:{0}".format(seat_type or "unclassified"), count, "seats",
                   "seat", fc_src, as_of=vintage,
                   label="Seats carrying this projected seat type")
    for state, count in sorted(state_seat_counts.items()):
        ledger.add("state_seats:{0}".format(_slug(state)), count, "seats", "state_seat",
                   fc_src, group="state:{0}".format(_slug(state)), as_of=vintage,
                   label="Parliamentary seats in {0}".format(state))
        # per-state projected totals (same read, state scope)
        state_blocs = defaultdict(int)
        for seat in seats:
            if seat.get("state", "") == state:
                state_blocs[seat.get("proj_winner", "")] += 1
        for bloc, bloc_count in sorted(state_blocs.items()):
            ledger.add("state_bloc:{0}:{1}".format(_slug(state), bloc), bloc_count, "seats",
                       "state_seat", fc_src, group="state:{0}".format(_slug(state)),
                       as_of=vintage,
                       label="Projected {0} seats in {1}".format(bloc, state))

    # ---- watch list ------------------------------------------------------
    for row in sorted(bg_stats["super_marginal"] + bg_stats["high_risk"] + bg_stats["watch"],
                      key=lambda r: (r.get("tier", ""), r.get("code", ""))):
        code = row.get("code", "")
        group = "watch:{0}".format(code)
        ledger.add("watch_tier:{0}".format(code), _seat_type_slug(row.get("tier", "")),
                   "text", "watch", _rel(rb.BATTLEGROUNDS), group=group, as_of=vintage,
                   label="Battleground tier ({0})".format(row.get("tier", "")))
        ledger.add("watch_constituency:{0}".format(code), row.get("constituency", ""),
                   "text", "watch", _rel(rb.BATTLEGROUNDS), group=group, as_of=vintage,
                   label="Battleground seat name")
        ledger.add("watch_state:{0}".format(code), row.get("state_std", ""), "text",
                   "watch", _rel(rb.BATTLEGROUNDS), group=group, as_of=vintage,
                   label="Battleground seat state")
        ledger.add("watch_margin:{0}".format(code), _safe_float(row.get("margin_pct_valid")),
                   "% margin", "watch", _rel(rb.BATTLEGROUNDS), group=group, as_of=vintage,
                   label="GE15 winning margin")
        ledger.add("watch_ge15_bloc:{0}".format(code), row.get("ge15_bloc", ""), "bloc",
                   "watch", _rel(rb.BATTLEGROUNDS), group=group, as_of=vintage,
                   label="GE15 winner at this battleground seat")
        ledger.add("watch_holders:{0}".format(code), row.get("current_bloc", ""), "bloc",
                   "watch", _rel(rb.BATTLEGROUNDS), group=group, as_of=vintage,
                   label="Current holder at this battleground seat")
    for tier, count in sorted(bg_stats["tier_counts"].items()):
        ledger.add("watch_tier_count:{0}".format(_seat_type_slug(tier)), count, "seats",
                   "watch", _rel(rb.BATTLEGROUNDS), as_of=vintage,
                   label="Seats in battleground tier {0}".format(tier))

    # ---- this week's signals --------------------------------------------
    _add_signal_claims(ledger, rb.load_week_signals(), vintage)

    # ---- story threads ---------------------------------------------------
    _add_story_claims(ledger, rb.read_story_threads(), vintage, "fed")

    return _finalise(ledger, lang=lang, kind="federal", state=None, as_of=vintage,
                     extra_spec=FEDERAL_SECTIONS)


def _safe_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# state pack
# ---------------------------------------------------------------------------

def build_state_pack(state, lang="en", as_of=None, root=None):
    """Build one state's claim ledger (federal seats in the state + local ledger)."""
    canon = srb.norm_state(state)
    forecast = rb.load_forecast()
    cfg = rb.load_config()
    scenarios = json.load(open(rb.SCENARIOS, encoding="utf-8"))
    scenario_meta = rb.load_scenario_meta()

    vintage = as_of or _as_of_date(forecast)
    ledger = _Ledger("dun")
    ledger.default_as_of = vintage
    slug = _slug(canon)
    fc_src = _rel(rb.FORECAST_JSON)

    seats = [s for s in forecast.get("projected_seats", [])
             if srb.norm_state(s.get("state", "")) == canon]
    flips = [f for f in forecast.get("flips", [])
             if srb.norm_state(f.get("state", "")) == canon]

    demog_rows = [r for r in rb.load_csv(rb.DEMOG)
                  if srb.norm_state(r.get("state", "")) == canon]
    ge15_rows = [r for r in rb.load_csv(rb.GE15)
                 if srb.norm_state(r.get("state", "")) == canon]
    demog = rb.compute_demog_stats(demog_rows) if demog_rows else None
    g15 = rb.compute_ge15_stats(ge15_rows) if ge15_rows else None
    swing_stats = rb.compute_swing_stats(rb.load_csv(rb.SWINGS))
    swings = {k: v for k, v in swing_stats["by_state"].get(canon, {}).items()}
    if not swings:  # the swings dataset spells the state its own way
        for key, value in swing_stats["by_state"].items():
            if srb.norm_state(key) == canon:
                swings = dict(value)
                break

    bg_rows = [r for r in rb.load_csv(rb.BATTLEGROUNDS)
               if srb.norm_state(r.get("state_std", "")) == canon]

    # ---- meta ------------------------------------------------------------
    ledger.add("pack_version", PACK_VERSION, "", "meta", "reference_pack.py", as_of=vintage,
               label="Model version")
    ledger.add("lang", lang, "", "meta", "reference_pack.py", as_of=vintage,
               label="Edition language")
    ledger.add("kind", "state", "", "meta", "reference_pack.py", as_of=vintage,
               label="Report scope")
    ledger.add("state", canon, "text", "meta", "reference_pack.py", as_of=vintage,
               label="State this edition covers")
    ledger.add("as_of", vintage, "date", "meta", fc_src, as_of=vintage, value_str=vintage,
               aliases=_date_aliases(vintage), label="Data as of")
    ledger.add("model", forecast.get("model", ""), "text", "meta", fc_src, as_of=vintage,
               label="Forecast model version")
    ledger.add("parliament_seats", forecast.get("parliament_seats", 0), "seats", "meta",
               fc_src, as_of=vintage, label="Parliamentary seats in the house")
    ledger.add("seats_federal", len(seats), "seats", "projection", fc_src, as_of=vintage,
               label="Parliamentary seats in this state")

    # ---- projection / blocs ---------------------------------------------
    state_blocs = defaultdict(int)
    ge15_blocs = defaultdict(int)
    seat_types = defaultdict(int)
    for seat in seats:
        state_blocs[seat.get("proj_winner", "")] += 1
        ge15_blocs[seat.get("ge15_winner", "")] += 1
        seat_types[seat.get("seat_type", "")] += 1
    govt_in_state = sum(count for bloc, count in state_blocs.items() if bloc in GOVT_BLOCS)
    opp_in_state = sum(count for bloc, count in state_blocs.items() if bloc in OPP_BLOCS)
    ge15_govt_in_state = sum(count for bloc, count in ge15_blocs.items() if bloc in GOVT_BLOCS)

    ledger.add("projection:government", govt_in_state, "seats", "projection", fc_src,
               as_of=vintage, label="Government-aligned seats projected in this state")
    ledger.add("projection:opposition", opp_in_state, "seats", "projection", fc_src,
               as_of=vintage, label="Opposition seats projected in this state")
    ledger.add("projection:ge15_government", ge15_govt_in_state, "seats", "projection",
               fc_src, as_of=vintage, label="Government-aligned seats won here at GE15")
    ledger.add("projection:flips", len(flips), "seats", "projection", fc_src,
               as_of=vintage, label="Seats changing hands in this state")
    for bloc in sorted(state_blocs, key=lambda b: (-state_blocs[b], b)):
        ledger.add("bloc:{0}".format(bloc), state_blocs[bloc], "seats", "bloc", fc_src,
                   group="bloc:{0}".format(bloc), as_of=vintage,
                   label="Projected {0} seats in this state".format(bloc))
    for bloc in sorted(ge15_blocs, key=lambda b: (-ge15_blocs[b], b)):
        ledger.add("ge15_bloc:{0}".format(bloc), ge15_blocs[bloc], "seats", "bloc",
                   _rel(rb.GE15), group="ge15_bloc:{0}".format(bloc), as_of=vintage,
                   label="GE15 seats for {0} in this state".format(bloc))
    ledger.add("mc_P50", _whole(forecast.get("monte_carlo", {}).get("P50")), "seats", "projection",
               fc_src, as_of=vintage, label="National Monte Carlo P50 (government seats)")
    ledger.add("mc_P10", _whole(forecast.get("monte_carlo", {}).get("P10")), "seats", "projection",
               fc_src, as_of=vintage, label="National Monte Carlo P10 (government seats)")
    ledger.add("mc_P90", _whole(forecast.get("monte_carlo", {}).get("P90")), "seats", "projection",
               fc_src, as_of=vintage, label="National Monte Carlo P90 (government seats)")
    p_majority = forecast.get("monte_carlo", {}).get("P_majority")
    if p_majority is not None:
        ledger.add("mc_P_majority", int(round(float(p_majority) * 100)), "%", "projection",
                   fc_src, as_of=vintage,
                   label="Probability the government retains the national majority")

    # ---- electorate ------------------------------------------------------
    if demog:
        ledger.add("electorate:total_electorate", demog["total_electorate"], "voters",
                   "electorate", _rel(rb.DEMOG), as_of=vintage,
                   label="Registered voters in this state")
        for key in ("wt_malay", "wt_chinese", "wt_indian", "wt_bumi_sabah", "wt_bumi_sarawak",
                    "youth_pct", "age_31_40", "age_41_50", "age_51_60", "age_60plus",
                    "avg_median_age"):
            ledger.add("electorate:{0}".format(key), demog[key], "%", "electorate",
                       _rel(rb.DEMOG), as_of=vintage,
                       label="Electorate demographic: {0}".format(key))
    if g15:
        ledger.add("electorate:total_seats", g15["total_seats"], "seats", "electorate",
                   _rel(rb.GE15), as_of=vintage, label="Seats with a GE15 result")
        ledger.add("electorate:total_voters_ge15", g15["total_voters"], "voters",
                   "electorate", _rel(rb.GE15), as_of=vintage, label="Registered voters at GE15")
        ledger.add("electorate:avg_margin", g15["avg_margin"], "% margin", "electorate",
                   _rel(rb.GE15), as_of=vintage, label="Average GE15 winning margin")
        for key in ("margin_under_1", "margin_under_2_5", "margin_under_5"):
            ledger.add("electorate:{0}".format(key), g15[key], "seats", "electorate",
                       _rel(rb.GE15), as_of=vintage,
                       label="GE15 seats inside the {0} band".format(key))
    for seat_type, count in sorted(seat_types.items()):
        ledger.add("seat_type_count:{0}".format(seat_type or "unclassified"), count, "seats",
                   "seat_type_count", fc_src, group="seat_type:{0}".format(seat_type or "x"),
                   as_of=vintage, label="Projected seats typed {0}".format(seat_type))

    # ---- swings ----------------------------------------------------------
    for bloc, pp in sorted(swings.items()):
        if bloc == "latest_date":
            continue
        ledger.add("swing:{0}".format(bloc), pp, "pp", "swing", _rel(rb.SWINGS),
                   group="swing:{0}".format(bloc), as_of=vintage,
                   aliases=["+{0}".format(_fmt(pp))],
                   label="This state's swing since GE15: {0}".format(bloc))
    if swings.get("latest_date"):
        latest = swings["latest_date"]
        ledger.add("swing_date", latest, "date", "swing", _rel(rb.SWINGS), as_of=vintage,
                   value_str=latest, label="Date of this state's latest swing reading")

    # ---- macro / thresholds / shocks / vacancies -------------------------
    _add_macro_claims(ledger, cfg, vintage)
    _add_event_shock_claims(ledger, cfg, vintage)
    _add_vacancy_claims(ledger, cfg, vintage)
    _add_threshold_claims(ledger, vintage)

    # ---- scenarios (national parametric scenarios, this state's share) ----
    for index, (name, blocs) in enumerate(scenarios.items(), start=1):
        family = scenario_meta.get(name, {}).get("category", "parametric")
        group = "scenario:{0}".format(index)
        local = sum(count for bloc, count in blocs.items() if bloc in GOVT_BLOCS)
        ledger.add("scenario:{0:02d}:name".format(index), name, "text", "scenario",
                   _rel(rb.SCENARIOS), group=group, as_of=vintage,
                   label="Scenario name ({0})".format(family))
        ledger.add("scenario:{0:02d}:government".format(index), local, "seats", "scenario",
                   _rel(rb.SCENARIOS), group=group, as_of=vintage,
                   label="National government seats under '{0}'".format(name))

    # ---- flips in this state --------------------------------------------
    directions = defaultdict(int)
    for flip in flips:
        code = flip.get("code", "")
        group = "flip:{0}".format(code)
        directions["{0}_TO_{1}".format(flip.get("ge15_winner", ""),
                                       flip.get("proj_winner", ""))] += 1
        ledger.add("flip:{0}".format(code), flip.get("proj_winner", ""), "bloc", "flip",
                   fc_src, group=group, as_of=vintage,
                   label="{0} ({1}): {2} -> {3}".format(code, flip.get("constituency", ""),
                                                        flip.get("ge15_winner", ""),
                                                        flip.get("proj_winner", "")))
        ledger.add("flip_constituency:{0}".format(code), flip.get("constituency", ""),
                   "text", "flip", fc_src, group=group, as_of=vintage,
                   label="Constituency name")
        ledger.add("flip_margin:{0}".format(code), flip.get("proj_margin"), "% margin",
                   "flip", fc_src, group=group, as_of=vintage,
                   label="Projected winning margin")
    for direction in sorted(directions):
        ledger.add("flip_dir:{0}".format(direction), directions[direction], "seats",
                   "flip", fc_src, group="flip_direction:{0}".format(direction),
                   as_of=vintage, label="Flips in direction {0}".format(direction.replace("_TO_", " -> ")))
    if not flips:
        ledger.add("flip:none", 0, "seats", "flip", fc_src, as_of=vintage,
                   label="No seats in this state are projected to change hands")

    # ---- per-seat ---------------------------------------------------------
    for seat in sorted(seats, key=lambda s: s.get("code", "")):
        code = seat.get("code", "")
        group = "seat:{0}".format(code)
        ledger.add("seat_proj:{0}".format(code), seat.get("proj_winner", ""), "bloc",
                   "seat", fc_src, group=group, as_of=vintage,
                   label="{0} {1}: projected winner".format(code, seat.get("constituency", "")))
        ledger.add("seat_constituency:{0}".format(code), seat.get("constituency", ""),
                   "text", "seat", fc_src, group=group, as_of=vintage,
                   label="Constituency name")
        ledger.add("seat_ge15:{0}".format(code), seat.get("ge15_winner", ""), "bloc",
                   "seat", fc_src, group=group, as_of=vintage, label="GE15 winner")
        ledger.add("seat_margin:{0}".format(code), seat.get("proj_margin"), "% margin",
                   "seat", fc_src, group=group, as_of=vintage,
                   label="Projected winning margin")
        ledger.add("seat_type:{0}".format(code), seat.get("seat_type", ""), "text", "seat",
                   fc_src, group=group, as_of=vintage, label="Seat-type descriptor")

    # ---- watch list -------------------------------------------------------
    for row in sorted(bg_rows, key=lambda r: (r.get("tier", ""), r.get("code", ""))):
        code = row.get("code", "")
        group = "watch:{0}".format(code)
        ledger.add("watch_tier:{0}".format(code), _seat_type_slug(row.get("tier", "")),
                   "text", "watch", _rel(rb.BATTLEGROUNDS), group=group, as_of=vintage,
                   label="Battleground tier ({0})".format(row.get("tier", "")))
        ledger.add("watch_constituency:{0}".format(code), row.get("constituency", ""),
                   "text", "watch", _rel(rb.BATTLEGROUNDS), group=group, as_of=vintage,
                   label="Battleground seat name")
        ledger.add("watch_margin:{0}".format(code), _safe_float(row.get("margin_pct_valid")),
                   "% margin", "watch", _rel(rb.BATTLEGROUNDS), group=group, as_of=vintage,
                   label="GE15 winning margin")
        ledger.add("watch_ge15_bloc:{0}".format(code), row.get("ge15_bloc", ""), "bloc",
                   "watch", _rel(rb.BATTLEGROUNDS), group=group, as_of=vintage,
                   label="GE15 winner at this battleground seat")
    ledger.add("watch_total", len(bg_rows), "seats", "watch", _rel(rb.BATTLEGROUNDS),
               as_of=vintage, label="Battleground seats in this state")
    if not bg_rows:
        ledger.add("watch:none", 0, "seats", "watch", _rel(rb.BATTLEGROUNDS),
                   as_of=vintage, label="No battleground seat in this state")

    # ---- signals (state-scoped) ------------------------------------------
    _add_signal_claims(ledger, srb.state_signals_from_trackers(canon), vintage)

    # ---- story threads (state-scoped) ------------------------------------
    _add_story_claims(ledger, rb.read_state_story_threads(canon), vintage, slug)

    return _finalise(ledger, lang=lang, kind="state", state=canon, as_of=vintage,
                     extra_spec=STATE_SECTIONS)


# ---------------------------------------------------------------------------
# finalise / hash / write
# ---------------------------------------------------------------------------

def _canonical(pack):
    return json.dumps(pack, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def pack_hash(pack):
    """sha256 over the canonical JSON of the pack (the stored hash is excluded)."""
    body = {k: v for k, v in pack.items() if k != "pack_hash"}
    return hashlib.sha256(_canonical(body)).hexdigest()


def _finalise(ledger, lang, kind, state, as_of, extra_spec):
    claims = ledger.claims
    pack = {
        "pack_version": PACK_VERSION,
        "kind": kind,
        "lang": lang,
        "state": state,
        "as_of": as_of,
        "sources": source_fingerprints(_input_paths()),
        "claims": claims,
        "families": ledger.families(),
        "sections": _build_sections(claims, extra_spec),
    }
    pack["counts"] = {
        "claims": len(claims),
        "top_level_claims": sum(1 for c in claims
                                if c["family"] not in ("seat", "flip", "watch")),
        "families": len(pack["families"]),
    }
    pack["codes"] = {"seats": sorted(pack_codes(pack))}
    pack["pack_hash"] = pack_hash(pack)
    return pack


def build_pack(lang="en", state=None, as_of=None, root=None):
    """Entry point: the federal pack, or one state's pack when ``state`` is given."""
    if state:
        return build_state_pack(state, lang=lang, as_of=as_of, root=root)
    return build_federal_pack(lang=lang, as_of=as_of, root=root)


def pack_filename(pack):
    scope = "" if pack["kind"] == "federal" else "-dun-{0}".format(_slug(pack.get("state") or ""))
    return "GE16-reference-{0}-{1}{2}.json".format(pack["as_of"], pack["lang"], scope)


def write_pack(pack, out_path=None, root=None):
    """Write the pack deterministically (sorted keys, LF, trailing newline)."""
    base = root or WORK_REFERENCE
    path = out_path or os.path.join(base, pack_filename(pack))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(pack, ensure_ascii=False, sort_keys=True, indent=2))
        handle.write("\n")
    return path


# ---------------------------------------------------------------------------
# citable tokens — the ONE definition of "a fact token" in authored prose
# ---------------------------------------------------------------------------
# The authoring harness quotes facts, never computes them, so "did the writer
# invent a number?" is decidable by regex: every number, date and seat code in
# the emitted prose is a TOKEN, and every token must already exist inside the
# pack (as a claim value, an accepted alias, or a claimable seat code). The
# extractor lives here so the pack and the verifier can never disagree about
# what a fact token is.

_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December",
    "Januari", "Februari", "Mac", "Mei", "Jun", "Julai", "Ogos", "Oktober",
    "Disember",
    "Jan", "Feb", "Mar", "Apr", "Jun", "Jul", "Aug", "Sep", "Sept", "Oct",
    "Nov", "Dec", "Ogo", "Okt", "Dis",
)

#: '24 September 2026' / '24 Sep 2026' (EN or MS month, longest first so
#: 'September' wins over 'Sep'). Built by concatenation: the pattern carries
#: {1,2}/{4} quantifiers that str.format() would try to substitute.
_MONTH_ALT = "|".join(sorted(set(_MONTH_NAMES), key=len, reverse=True))
DATE_LONG_RE = re.compile(r"\b\d{1,2}\s+(?:" + _MONTH_ALT + r")\s+\d{4}\b")
DATE_ISO_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
#: P015 / P.015 / N.15 / N15 — a parliamentary or state seat code.
SEAT_RE = re.compile(r"(?<![A-Za-z0-9])P\.?\d{3}(?!\d)|(?<![A-Za-z0-9])N\.?\d{1,2}(?!\d)")
#: 139 / 23.3 / 5,000 / 4.0816 — a bare number (never the tail of P015 or GE16).
NUMBER_RE = re.compile(
    r"(?<![\w.,])(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?![\w])")

#: An inline provenance citation: ``[fed:P50:139]``, ``[dun:seat_margin:P015:23.3]``.
CITATION_RE = re.compile(r"\[([a-z]{2,4}:[^\[\]\n]*)\]")

#: Spans that are structure, not data: §-references, "Section 4.2" prose
#: references, heading numbering ("## 3. The Electorate") and list markers.
_MASK_PATTERNS = (
    (re.compile(r"§\s*\d+(?:\.\d+)*"), 0),
    (re.compile(r"\b(?:Section|Seksyen|Bahagian)\s+\d+(?:\.\d+)*", re.IGNORECASE), 0),
    (re.compile(r"(?m)^(#{1,6}\s*)(\d+(?:\.\d+)*)\.?"), 2),
    (re.compile(r"(?m)^\s*(\d+)[.)]\s"), 1),
    # A citation is provenance, not prose: its own digits (a claim key like
    # ``scenario:01``) must not read as a fact token. Unknown citations are
    # caught separately by the authoring harness, so masking them here cannot
    # hide an invented number.
    (CITATION_RE, 0),
)


def _blank(text, start, end):
    return text[:start] + " " * (end - start) + text[end:]


def _blank_matches(text, pattern, group=0):
    out = text
    for match in reversed(list(pattern.finditer(text))):
        out = _blank(out, match.start(group), match.end(group))
    return out


def extract_tokens(text):
    """Every number / date / seat-code token in ``text`` (order preserved).

    Returns a list of ``{"token", "kind", "start"}``. Masking is applied first
    so structural numbering (headings, list markers, section references) is
    never mistaken for a citable fact.
    """
    masked = text or ""
    for pattern, group in _MASK_PATTERNS:
        masked = _blank_matches(masked, pattern, group)

    tokens = []
    for match in SEAT_RE.finditer(masked):
        tokens.append({"token": match.group(0), "kind": "seat", "start": match.start()})
    masked = _blank_matches(masked, SEAT_RE)

    for pattern in (DATE_LONG_RE, DATE_ISO_RE):
        for match in pattern.finditer(masked):
            tokens.append({"token": match.group(0), "kind": "date", "start": match.start()})
        masked = _blank_matches(masked, pattern)

    for match in NUMBER_RE.finditer(masked):
        tokens.append({"token": match.group(0), "kind": "number", "start": match.start()})

    tokens.sort(key=lambda item: item["start"])
    return tokens


def normalise_token(token, kind):
    """Seat codes ignore the optional dot (``N.15`` == ``N15``), others are exact."""
    return token.replace(".", "") if kind == "seat" else token


def claim_index(pack):
    """``{claim_id: claim}`` — the only lookup an authoring pass needs."""
    return {claim["claim_id"]: claim for claim in pack.get("claims", [])}


def pack_codes(pack):
    """Seat codes this pack is allowed to name (dot-stripped for comparison)."""
    codes = set()
    for claim in pack.get("claims", []):
        group = claim.get("group") or ""
        if group.startswith(("seat:", "watch:", "flip:")):
            code = group.split(":", 1)[1].strip()
            if code:
                codes.add(code.replace(".", ""))
    for code in (pack.get("codes") or {}).get("seats", ()):
        codes.add(str(code).replace(".", ""))
    return codes


def allowed_tokens(pack):
    """Every fact token an authored sentence may contain (strict verification).

    A token is allowed when it appears inside a claim value, inside an accepted
    alias, or is a seat code the pack itself carries — so a ledger headline that
    quotes "BNM hold OPR at 2.75%" legitimately licenses the token ``2.75``,
    while a computed "150 seats" never does.
    """
    tokens = set()
    for claim in pack.get("claims", []):
        strings = [str(claim.get("value_str") or "")]
        strings.extend(str(alias) for alias in (claim.get("aliases") or ()))
        for text in strings:
            if not text:
                continue
            tokens.add(text)
            for found in extract_tokens(text):
                tokens.add(normalise_token(found["token"], found["kind"]))
    tokens.update(pack_codes(pack))
    return tokens


def load_pack(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


ALL_STATES = ("Johor", "Kedah", "Kelantan", "Melaka", "Negeri Sembilan", "Pahang",
              "Perak", "Perlis", "Pulau Pinang", "Sabah", "Sarawak", "Selangor",
              "Terengganu")


def main(argv=None):
    parser = argparse.ArgumentParser(description="GE16 reference pack extractor (deterministic).")
    parser.add_argument("--lang", default="en", choices=("en", "ms"))
    parser.add_argument("--state", default=None, help="one state (DUN scope)")
    parser.add_argument("--all-states", action="store_true", help="federal + all 13 states")
    parser.add_argument("--as-of", default=None, help="override the data vintage (YYYY-MM-DD)")
    parser.add_argument("--out", default=None, help="explicit output path (single pack)")
    parser.add_argument("--stdout", action="store_true", help="print the pack JSON")
    parser.add_argument("--summary", action="store_true", help="print the family/claim summary")
    args = parser.parse_args(argv)

    packs = []
    if args.all_states:
        for state in ALL_STATES:
            packs.append(build_pack(lang=args.lang, state=state, as_of=args.as_of))
    packs.insert(0, build_pack(lang=args.lang, state=args.state, as_of=args.as_of))

    for pack in packs:
        path = write_pack(pack, out_path=args.out if len(packs) == 1 else None)
        print("wrote {0} ({1} claims, hash {2})".format(
            path, pack["counts"]["claims"], pack["pack_hash"][:12]))
        if args.summary:
            for family, ids in sorted(pack["families"].items()):
                print("  {0:<18} {1}".format(family, len(ids)))
            print("  sections: {0}".format(", ".join(s["key"] for s in pack["sections"])))
        if args.stdout:
            print(json.dumps(pack, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
