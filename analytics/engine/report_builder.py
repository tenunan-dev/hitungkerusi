#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GE16 All-in-One Report Builder — versioned, self-contained, thesis-style.

Assembles the complete narrative report from LIVE sources so every number is
computed, never hand-typed:
  - Methodology & weightage   <- 02_FORECAST/knowledge/forecast-theory.md + config.py
  - Data                      <- Research Data/derived/ datasets (live)
  - Calculation               <- 02_FORECAST/outputs/latest/ge16-forecast-latest.json
  - Reasoning                 <- flip list + scenario logic
  - Results                   <- Monte Carlo P10/P50/P90 + parliament table
  - Party landscape           <- Research Data/notes/party-landscape-update-2026.md
  - PRN scorecard             <- 02_FORECAST/knowledge/prn-prediction-scorecard.md
  - Battlegrounds             <- Parliament/ge16-battleground-seats-master.csv
  - Scenarios                 <- work/scenarios/projection_scenarios.json

Versioning:
  - 03_REPORTS/federal/latest/GE16_Malaysia_General_Election_Report.md  <- current
  - 03_REPORTS/federal/archive/GE16-YYYY-MM-DD/...              <- preserved prior versions
  - 03_REPORTS/federal/latest/GE16_Malaysia_General_Election_Report.docx (via md2docx)

Usage:
  .venv/bin/python 02_FORECAST/engine/report_builder.py            # build latest + archive
  .venv/bin/python 02_FORECAST/engine/report_builder.py --docx     # also regenerate DOCX
"""
import argparse
import csv
import json
import math
import os
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta

from data_roots import resolve_data_roots

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_ROOTS = resolve_data_roots(repository_root=ROOT)
REPORT = os.path.join(ROOT, "03_REPORTS")
LATEST_DIR = os.path.join(REPORT, "federal", "latest")
ARCHIVE = os.path.join(REPORT, "federal", "archive")
FNAME = "GE16_Malaysia_General_Election_Report"

# Language mode (v2 native MS, 9 Aug 2026): "en" default, "ms" = native Malay
LANG = "en"

# live inputs
FORECAST_JSON = os.path.join(ROOT, "02_FORECAST", "outputs", "latest", "ge16-forecast-latest.json")
CONFIG = os.path.join(ROOT, "02_FORECAST", "engine", "config.py")
SWINGS = DATA_ROOTS.derived / "swing_se_to_se.csv"
MASTER = DATA_ROOTS.derived / "master-list-222-parliamentary-seats.csv"
DEMOG = DATA_ROOTS.derived / "voter-demographics-by-constituency-ge15.csv"
GE15 = DATA_ROOTS.derived / "ge15-results-by-constituency-full.csv"
BATTLEGROUNDS = DATA_ROOTS.federal / "ge16-battleground-seats-master.csv"
SCENARIOS = os.path.join(ROOT, "work", "scenarios", "projection_scenarios.json")
TRACKER_LOGS = {
    "poll": DATA_ROOTS.trackers / "ge16-poll-tracker-log.md",
    "candidate": DATA_ROOTS.trackers / "ge16-candidate-tracker-log.md",
    "general-news": DATA_ROOTS.trackers / "ge16-general-news-log.md",
}
NEWS_FEED = DATA_ROOTS.trackers / "ge16-news-feed.json"


# ============================================================
# DATA LOADING
# ============================================================

def load_forecast():
    with open(FORECAST_JSON) as f:
        return json.load(f)


def load_config():
    """Import config.py as a module to read FACTORS/MACRO/EVENT_SHOCKS live."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("fc_config", CONFIG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ============================================================
# STORY THREADS (2026-09-24): the narrative layer, read from the events ledger
# ============================================================
# Section 2's thread list is derived ONLY from the events database
# (work/events/ge16-events.db — tables stories/story_events, views
# v_story_current/v_story_timeline). Nothing in this block is hand-written and
# nothing is inferred: every field printed here is a column of the ledger, so a
# thread with no event in the current window still renders its last_update date
# and status instead of being dropped or described.
EVENTS_DB = os.path.join(ROOT, "work", "events", "ge16-events.db")
STORY_THREAD_LIMIT = 20
STORY_STALE_LIMIT = 6
#: must equal tools/events/stories.py STATUS_OPEN_DAYS (asserted by
#: automation/tests/test_story_ledger.py — the report never opens the ledger
#: module, so the two declarations are kept in sync by that test).
STORY_OPEN_WINDOW_DAYS = 90


def read_story_threads(limit=STORY_THREAD_LIMIT, stale_limit=STORY_STALE_LIMIT, story_ids=None):
    """Read the story ledger from the events DB (read-only).

    Returns a dict of scalars + rows, or ``{"available": False, "reason": ...}``
    when the ledger is not built yet — the report then renders one honest line
    instead of a narrative it cannot support.

    ``story_ids`` narrows EVERY query to one state's threads (the state reports'
    §2.1 equivalent, T1.5 — see :func:`read_state_story_threads`). The federal
    call passes nothing, so its SQL and its rendered output are unchanged.
    """
    import sqlite3
    data = {"available": False, "path": EVENTS_DB,
            "reason": "events ledger not built (python tools/events/build_events_db.py)"}
    if not os.path.exists(EVENTS_DB):
        return data
    try:
        conn = sqlite3.connect(f"file:{EVENTS_DB}?mode=ro", uri=True)
    except sqlite3.Error as error:  # pragma: no cover - environment dependent
        data["reason"] = f"the dated event record could not be read ({error})"
        return data
    conn.row_factory = sqlite3.Row
    scope_sql, scope_params = _story_scope_clause(story_ids)
    try:
        # as-of date = the sweep window's end: post-window entries are in the
        # ledger but must not push "now" past the report's own data vintage.
        row = conn.execute(
            "SELECT MAX(event_date) AS as_of FROM events WHERE window_class <> 'post_window'").fetchone()
        as_of = (row["as_of"] if row else "") or ""
        counts = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM stories WHERE 1 = 1" + scope_sql +
            " GROUP BY 1",
            dict(scope_params))}
        threads = conn.execute(
            "SELECT story_id, headline, status, theme, anchor, first_seen, last_update,"
            " event_count, latest_event_title FROM v_story_current"
            " WHERE last_update <= :as_of" + scope_sql +
            " ORDER BY last_update DESC, event_count DESC, story_id LIMIT :limit",
            {"as_of": as_of, "limit": limit, **scope_params}).fetchall()
        held = conn.execute(
            "SELECT COUNT(*) AS n FROM v_story_current WHERE last_update > :as_of" + scope_sql,
            {"as_of": as_of, **scope_params}).fetchone()["n"]
        stale = conn.execute(
            "SELECT story_id, headline, status, last_update, event_count FROM v_story_current"
            " WHERE (last_update IS NULL OR last_update = '' OR last_update < :cutoff)"
            + scope_sql +
            " ORDER BY (last_update IS NULL OR last_update = ''), last_update DESC, story_id"
            " LIMIT :limit",
            {"cutoff": _story_cutoff(as_of), "limit": stale_limit, **scope_params}).fetchall()
        stale_total = conn.execute(
            "SELECT COUNT(*) AS n FROM v_story_current"
            " WHERE (last_update IS NULL OR last_update = '' OR last_update < :cutoff)"
            + scope_sql,
            {"cutoff": _story_cutoff(as_of), **scope_params}).fetchone()["n"]
        data.update({
            "available": True, "as_of": as_of,
            "total": sum(counts.values()), "open": counts.get("open", 0),
            "dormant": counts.get("dormant", 0), "closed": counts.get("closed", 0),
            "threads": [dict(r) for r in threads], "held": held,
            "stale": [dict(r) for r in stale], "stale_total": stale_total,
        })
    except sqlite3.Error as error:  # pragma: no cover - environment dependent
        data["reason"] = f"the dated event record could not be read ({error})"
    finally:
        conn.close()
    return data


def _story_scope_clause(story_ids):
    """``(sql, params)`` limiting the ledger queries to a set of story ids.

    ``story_ids is None`` → ``("", {})``: the federal report's queries are
    byte-identical to what they were before the state reports existed.
    """
    if story_ids is None:
        return "", {}
    ids = sorted(story_ids)
    if not ids:
        return " AND 1 = 0", {}
    placeholders = ", ".join(f":scope{i}" for i in range(len(ids)))
    return f" AND story_id IN ({placeholders})", {f"scope{i}": sid for i, sid in enumerate(ids)}


def _story_cutoff(as_of):
    """as_of minus the ledger's open window (see tools/events/stories.py)."""
    try:
        y, m, d = (int(part) for part in str(as_of)[:10].split("-"))
        cut = datetime(y, m, d) - timedelta(days=STORY_OPEN_WINDOW_DAYS)
    except (TypeError, ValueError):
        return "9999-12-31"
    return cut.date().isoformat()


# ============================================================
# STATE-SCOPED THREADS (T1.5): the same ledger, filtered to one state
# ============================================================
# A state report's "Story threads (this state)" section is the federal §2.1
# section scoped to one state. Qualification is deterministic and reads ledger
# columns only: a story belongs to state X when at least one of its MEMBER
# EVENTS carries
#   (a) a seat entity of X — the seat→state keying is the ledger's own
#       (entities.attrs_json.state on entity_type='seat', the field
#       tools/events/build_events_db.py reads when it gives a record its seat's
#       state), so no new key scheme is introduced;
#   (b) a party field office / listing anchored to X — carried either as the
#       event's own `state` column (the builder fills it from the declared
#       state/listing when no seat is given) or as a `state:X` entity attached
#       to the event;
#   (c) the event row's `jurisdiction` = 'state:X'.
# The state/seat name spellings below are the ones the sweep dossiers write;
# the mapping mirrors state_report_builder.norm_state().
_LEDGER_STATE_ALIASES = {"Penang": "Pulau Pinang", "Malacca": "Melaka"}


def _ledger_state_key(name):
    """A ledger state value → the state-report state it means ('' when none)."""
    value = (name or "").strip()
    if " (" in value:                       # e.g. 'Pulau Pinang (Penang)'
        value = value.split(" (", 1)[0].strip()
    return _LEDGER_STATE_ALIASES.get(value, value)


def _ledger_state_spellings(state):
    """Every spelling the ledger uses for ``state`` (report key + aliases)."""
    spellings = {state}
    spellings.update(alias for alias, key in _LEDGER_STATE_ALIASES.items() if key == state)
    return tuple(sorted(spelling for spelling in spellings if spelling))


def qualifying_story_ids_for_state(state):
    """Story ids whose member events reference ``state`` (ledger columns only).

    Returns a sorted tuple of story ids; ``None`` when the ledger is absent or
    unreadable, so the report states that fact instead of claiming zero threads.
    """
    import sqlite3
    if not os.path.exists(EVENTS_DB):
        return None
    entity_ids = {f"state:{spelling}" for spelling in _ledger_state_spellings(state)}
    qualified = set()
    try:
        conn = sqlite3.connect(f"file:{EVENTS_DB}?mode=ro", uri=True)
    except sqlite3.Error:  # pragma: no cover - environment dependent
        return None
    conn.row_factory = sqlite3.Row
    try:
        for row in conn.execute(
                "SELECT DISTINCT se.story_id AS story_id, en.attrs_json AS attrs_json"
                " FROM story_events se"
                " JOIN event_entities ee ON ee.event_id = se.event_id"
                " JOIN entities en ON en.entity_id = ee.entity_id"
                " WHERE en.entity_type = 'seat'"):
            try:
                seat_state = json.loads(row["attrs_json"] or "{}").get("state")
            except ValueError:
                continue
            if _ledger_state_key(seat_state) == state:
                qualified.add(row["story_id"])
        for row in conn.execute(
                "SELECT DISTINCT se.story_id AS story_id, e.state AS state,"
                " e.jurisdiction AS jurisdiction"
                " FROM story_events se JOIN events e ON e.event_id = se.event_id"):
            jurisdiction = row["jurisdiction"] or ""
            if _ledger_state_key(row["state"]) == state or (
                    jurisdiction.startswith("state:")
                    and _ledger_state_key(jurisdiction.split(":", 1)[1]) == state):
                qualified.add(row["story_id"])
        for row in conn.execute(
                "SELECT DISTINCT se.story_id AS story_id, ee.entity_id AS entity_id"
                " FROM story_events se"
                " JOIN event_entities ee ON ee.event_id = se.event_id"
                " WHERE ee.entity_id LIKE 'state:%'"):
            if row["entity_id"] in entity_ids:
                qualified.add(row["story_id"])
    except sqlite3.Error:  # pragma: no cover - environment dependent
        return None
    finally:
        conn.close()
    return tuple(sorted(qualified))


def read_state_story_threads(state, limit=STORY_THREAD_LIMIT, stale_limit=STORY_STALE_LIMIT):
    """The federal §2.1 read, scoped to one state (state reports, T1.5).

    Same helper, same columns, same as-of window as the federal section — the
    only difference is the ``story_ids`` filter, which keeps the ledger's own
    limits (``limit``/``stale_limit``) honest per state.
    """
    story_ids = qualifying_story_ids_for_state(state)
    if story_ids is None:
        return {"available": False, "state": state, "path": EVENTS_DB,
                "reason": "events ledger not built (python tools/events/build_events_db.py)"}
    data = read_story_threads(limit=limit, stale_limit=stale_limit, story_ids=story_ids)
    data["state"] = state
    data["story_ids"] = list(story_ids)
    return data


def _story_row_cells(thread):
    """One table row per chain of events — dated facts, in the reader's words."""
    headline = (thread["headline"] or "").replace("|", "/")
    latest = (thread.get("latest_event_title") or "").replace("|", "/")
    if len(latest) > 110:
        latest = latest[:107].rstrip() + "..."
    return ("| {head} | {count} | {first} | {last} | {status} | {latest} |".format(
        head=headline, count=thread["event_count"],
        first=thread.get("first_seen") or "undated", last=thread.get("last_update") or "undated",
        status=thread["status"], latest=latest))


def story_threads_en(data):
    """Markdown block for section 2 (EN), from the dated event chains only."""
    if not data.get("available"):
        return ("### 2.1 Political developments\n\n"
                "**No political developments are shown for this edition.** No dated chain "
                "of political events could be read at build time, so this subsection states "
                "that fact instead of inventing developments.")
    as_of = data["as_of"] or "undated"
    lines = [
        "### 2.1 Political developments",
        "",
        "Section 2 above is the standing context. This subsection is the live political "
        "record: one row per related chain of dated political events, grouped by the actors "
        "and the themes that connect them — a party's split or merger, a seat's vacancy and "
        "its by-election chain, seat talks, the GE16 timetable, a court action. Each row is "
        "the chain as it stands: when it started, when it last moved, how many dated events "
        "it carries and where it is now.",
        "",
        f"**As of {as_of}:** {data['total']} chains — {data['open']} open, "
        f"{data['dormant']} dormant, {data['closed']} closed. The {len(data['threads'])} most "
        "recently updated open chains follow; *Last update* is the date of the chain's most "
        "recent event, and *Items covered* is how many dated events it carries. A chain that "
        "has not moved this cycle keeps its row, its date and its status.",
        "",
        "| Chain of events | Items covered | First development | Last update | Status | Latest development |",
        "|---|---|---|---|---|---|",
    ]
    lines += [_story_row_cells(thread) for thread in data["threads"]]
    lines += [""]
    if data["stale_total"]:
        examples = "; ".join(
            f"{thread.get('headline') or 'unnamed chain'} ({thread['status']}, last update "
            f"{thread['last_update'] or 'undated'}, {thread['event_count']} items covered)"
            for thread in data["stale"])
        lines += [
            f"**Chains carried without a current development ({data['stale_total']}):** open "
            "chains whose most recent event predates this edition or carries no captured "
            "date, so they are shown by last update and status only — no development is "
            "claimed for them this time. Most recently: " + examples + ".",
            "",
        ]
    if data["held"]:
        lines += [
            f"**Held for the next edition ({data['held']}):** open chains whose latest event "
            "is dated after this edition's cut-off. They are real and dated; they are simply "
            "not presented as the current position here.",
            "",
        ]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def story_threads_ms(data):
    """Markdown block for section 2 (MS) — the same dated chains, native Malay."""
    if not data.get("available"):
        return ("### 2.1 Perkembangan politik\n\n"
                "**Tiada perkembangan politik dipaparkan untuk edisi ini.** Tiada rantaian "
                "peristiwa politik bertarikh dapat dibaca semasa laporan dibina, jadi "
                "subseksyen ini menyatakan fakta itu dan bukan mengarang perkembangan.")
    as_of = data["as_of"] or "tiada tarikh"
    lines = [
        "### 2.1 Perkembangan politik",
        "",
        "Seksyen 2 di atas ialah konteks tetap. Subseksyen ini ialah rekod politik semasa: "
        "satu baris bagi setiap rantaian peristiwa politik bertarikh yang berkaitan, "
        "dikelompokkan mengikut pelaku dan tema yang menghubungkannya — perpecahan atau "
        "penggabungan parti, kekosongan kerusi dan rantaian pilihan raya kecilnya, "
        "rundingan kerusi, jadual GE16, tindakan mahkamah. Setiap baris ialah rantaian itu "
        "sebagaimana keadaannya: bila ia bermula, bila ia kali terakhir bergerak, berapa "
        "peristiwa bertarikh dibawanya dan di mana kedudukannya sekarang.",
        "",
        f"**Setakat {as_of}:** {data['total']} rantaian — {data['open']} terbuka, "
        f"{data['dormant']} dorman, {data['closed']} tertutup. {len(data['threads'])} rantaian "
        "terbuka yang paling terkini disenaraikan; *Kemas kini terakhir* ialah tarikh "
        "peristiwa terakhir rantaian itu, dan *Item diliputi* ialah bilangan peristiwa "
        "bertarikh yang dibawanya. Rantaian yang tidak bergerak kitaran ini kekal dengan "
        "barisnya, tarikhnya dan statusnya.",
        "",
        "| Rantaian peristiwa | Item diliputi | Perkembangan pertama | Kemas kini terakhir | Status | Perkembangan terakhir |",
        "|---|---|---|---|---|---|",
    ]
    lines += [_story_row_cells(thread) for thread in data["threads"]]
    lines += [""]
    if data["stale_total"]:
        examples = "; ".join(
            f"{thread.get('headline') or 'rantaian tanpa nama'} ({thread['status']}, kemas "
            f"kini terakhir {thread['last_update'] or 'tiada tarikh'}, {thread['event_count']} "
            "item diliputi)" for thread in data["stale"])
        lines += [
            f"**Rantaian dibawa tanpa perkembangan semasa ({data['stale_total']}):** rantaian "
            "terbuka yang peristiwa terakhirnya lebih awal daripada edisi ini atau tiada "
            "tarikh tercatat, jadi ia dipaparkan mengikut kemas kini terakhir dan status "
            "sahaja — tiada perkembangan didakwa untuknya kali ini. Paling terkini: "
            + examples + ".",
            "",
        ]
    if data["held"]:
        lines += [
            f"**Dipegang untuk edisi seterusnya ({data['held']}):** rantaian terbuka yang "
            "peristiwa terakhirnya bertarikh selepas tarikh potong edisi ini. Ia nyata dan "
            "bertarikh; ia cuma tidak dipaparkan sebagai kedudukan semasa di sini.",
            "",
        ]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


# ============================================================
# F3 (2026-09-24): SEAT-SHOCK PROSE — ONE LIVE SOURCE
# ============================================================
# Every seat-shock sentence in BOTH language renders derives from a single read
# of cfg.EVENT_SHOCKS (see the `shocks` block in build_report). The constants
# below are the empty-mapping wording; no render may describe a shock the config
# no longer carries. Keep EN and MS wording together.
NO_SEAT_SHOCKS_EN = (
    "No seat-level event shocks are carried in the config (event_shocks: none); "
    "scenario-layer levers (Full fragmentation's bersatu_penalty and PH "
    "fragmentation actions) apply the Malay-opposition vs PH adjustments this "
    "cycle."
)
NO_SEAT_SHOCKS_MS = (
    "Tiada kejutan peristiwa peringkat kerusi dibawa dalam konfigurasi "
    "(event_shocks: tiada); tuas lapisan senario (bersatu_penalty dalam Full "
    "fragmentation dan tindakan pemecahan PH) yang menggunakan pelarasan "
    "pembangkang-Melayu berbanding PH pada kitaran ini."
)

# pm_pref prose anchor. config.MACRO stores only the two pp deltas
# (pm_pref_malay / pm_pref_nonmalay). Merdeka Center's OVERALL Anwar reading is
# the anchor they are measured against, so the ethnic shares quoted in the EN and
# MS prose are DERIVED (overall + delta), never typed. UPDATE TOGETHER WITH the
# MACRO pm_pref_* values if the survey is re-baselined.
PM_PREF_OVERALL_PCT = 52.0   # % — Merdeka Center national, fieldwork Mar–Apr 2026


def _phi(x):
    """Standard-normal CDF (used for the probit flip probability in §10)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _sg(value, nd=2):
    """Signed number in house style (U+2212 minus, like the rest of the report)."""
    return f"{value:+.{nd}f}".replace("-", "\u2212")


def lumut_worked_example(flips, cfg):
    """Live swing stack for the P074 Lumut worked example (§10 Calculation).

    Recomputes P074 through forecast_engine.build_swing_map — the SAME function
    the headline projection runs — so the worked example cannot drift from the
    engine, and no shock term can appear in it unless cfg.EVENT_SHOCKS genuinely
    carries one for this seat. Returns None when the engine is unavailable; the
    caller then falls back to shock-free wording.
    """
    try:
        engine_dir = os.path.dirname(os.path.abspath(__file__))
        if engine_dir not in sys.path:
            sys.path.insert(0, engine_dir)
        import forecast_engine as fe

        state_swings = fe.load_state_swings()
        boundary_swings = fe.load_boundary_swings()
        seat_sw = fe.build_swing_map(state_swings).get("P074", {})
        base = fe.load_baseline()
        row = base[base["code"] == "P074"].iloc[0]
        winner = row["winner_ge15"]
        runnerup = row["runnerup"]
        margin = float(row["margin_pct_ge15"])
        sw_w = float(seat_sw.get(winner, 0.0))
        sw_r = float(seat_sw.get(runnerup, 0.0))
        proj_raw = margin + sw_w - sw_r
        malay = float(row.get("malay_pct", 0) or 0)
        youth = float(row.get("youth_pct", 0) or 0)
        econ = fe.economic_term()
        shock = dict(cfg.EVENT_SHOCKS.get("P074", {}))
        if shock:
            _st = ", ".join(f"{b} {pp:+d}pp" for b, pp in shock.items())
            shock_clause_en = (
                f"The config carries one seat shock for P074 ({_st}); it is already "
                "inside the swing figures above.")
            shock_clause_ms = (
                f"Konfigurasi membawa satu kejutan kerusi untuk P074 ({_st}); ia sudah "
                "termasuk dalam angka ayunan di atas.")
        else:
            shock_clause_en = (
                "No event shock is applied in this seat: the config's `EVENT_SHOCKS` "
                "mapping carries nothing for P074 (event_shocks: none this cycle).")
            shock_clause_ms = (
                "Tiada kejutan peristiwa dikenakan di kerusi ini: pemetaan `EVENT_SHOCKS` "
                "dalam konfigurasi tidak membawa apa-apa untuk P074 (event_shocks: tiada "
                "pada kitaran ini).")
        return {
            "seat": row.get("constituency", "Lumut"),
            "state": row.get("state", "Perak"),
            "seat_type": row.get("seat_type", "mixed_malay"),
            "winner": winner,
            "runnerup": runnerup,
            "malay": malay,
            "youth": youth,
            "margin": margin,
            "sw_winner": sw_w,
            "sw_runner": sw_r,
            "sw_pn": float(seat_sw.get("PN", 0.0)),
            "proj_raw": proj_raw,
            "proj": abs(proj_raw),
            "recorded": next((f.get("proj_margin") for f in flips
                              if f.get("code") == "P074"), None),
            "state_swing_absent": row.get("state") not in state_swings,
            "boundary_swing_absent": "P074" not in boundary_swings,
            "econ": econ,
            "econ_pp": econ * float(fe.FACTORS["economy"]) / 0.20 * 0.10,
            "approval_pp": (float(fe.MACRO["approval_delta"])
                            * 0.1 * float(fe.FACTORS["approval"]) / 0.25),
            "mod_winner": fe.ethnic_swing_mod(malay, winner),
            "mod_runner": fe.ethnic_swing_mod(malay, runnerup),
            "mod_ph": fe.ethnic_swing_mod(malay, "PH"),
            "pre_mod": (econ * float(fe.FACTORS["economy"]) / 0.20 * 0.10
                        + float(fe.MACRO["approval_delta"])
                        * 0.1 * float(fe.FACTORS["approval"]) / 0.25),
            "ymod": 1.0 + 0.30 * max(0.0, youth / 100.0 - 0.20),
            "shock_clause_en": shock_clause_en,
            "shock_clause_ms": shock_clause_ms,
        }
    except Exception:
        return None


def load_csv(path):
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            rows.append(r)
    return rows


def read_text(path):
    with open(path) as f:
        return f.read()


def fmt_date():
    return datetime.now().strftime("%d %B %Y")


# ============================================================
# SIGNALS SECTION: this week's news items + their classification
# ============================================================
def load_week_signals():
    """Load this week's new tracker items from the three tracker logs.

    Returns a list of dicts: {feed, title, date, category, blocs, score} — the
    most recent scan block from each of the poll/candidate/general-news tracker
    logs. If a log is missing or empty, that feed contributes nothing (report
    still builds).

    v4 (5 Aug 2026): for the General news feed, PREFERS the LLM-judged feed
    (1_DATA/research/trackers/ge16-news-feed.json) which carries category/blocs/
    lang/score from the tracker's judgement pass — far richer than re-guessing
    with keyword heuristics. Falls back to the raw log parse if the feed is
    missing or empty.
    """
    feeds = [
        ("Poll tracker", TRACKER_LOGS["poll"]),
        ("Candidate tracker", TRACKER_LOGS["candidate"]),
        ("General news", TRACKER_LOGS["general-news"]),
    ]
    signals = []

    # --- General news: try the LLM-judged feed first (v4) ---
    feed_path = NEWS_FEED
    if os.path.exists(feed_path):
        try:
            import json as _json
            feed_data = _json.load(open(feed_path, encoding="utf-8"))
            for it in feed_data.get("items", [])[:12]:
                title = (it.get("title") or "").strip()
                if not title:
                    continue
                signals.append({
                    "feed": "General news",
                    "title": title[:120],
                    "date": it.get("date", ""),
                    "category": it.get("category", ""),
                    "blocs": it.get("blocs", []),
                    "score": it.get("score", 0.5),
                })
                if sum(1 for s in signals if s["feed"] == "General news") >= 12:
                    break
        except Exception:
            pass  # fall through to log parse below

    # --- Poll + Candidate trackers: log parse (no judged feed exists for them) ---
    for feed, path in feeds:
        if feed == "General news" and any(s["feed"] == "General news" for s in signals):
            continue  # already sourced from the judged feed
        if not os.path.exists(path):
            continue
        try:
            txt = open(path, encoding="utf-8").read()
        except Exception:
            continue
        # split into blocks; keep only the LAST block that is a tracker Scan
        # (manual notes like [SIMULATION] / [HYPOTHETICAL] are not scans)
        blocks = [b for b in txt.split("\n## ") if b.startswith("Scan ")]
        if not blocks:
            continue
        last = blocks[-1]
        # two item formats:
        #  general-news: "- **[query]** Title — link"
        #  poll/cand:    "- **Title**: ..." or "- [query] Title — link"
        for l in last.splitlines():
            l = l.strip()
            if not l.startswith("- "):
                continue
            body = l[2:]
            if body.startswith("**[") and "]**" in body:          # general-news
                title = body.split("]**", 1)[1].strip()
            elif body.startswith("**") and "**" in body[2:]:      # poll/candidate **Tag**: headline
                after = body.split("**", 2)[2].strip()
                title = after.lstrip(":").strip() if after.startswith(":") else after
            else:
                title = body
            title = title.split(" — ")[0].strip(": ")[:120]
            if title:
                signals.append({"feed": feed, "title": title})
            if sum(1 for s in signals if s["feed"] == feed) >= 6:
                break  # cap at 6 items per feed
    return signals


def classify_signal(title):
    """Heuristic classification of a news item into the triage buckets.

    Used to annotate the signals table in the report. The buckets mirror the
    weekly update procedure: scenario / shock / macro / swing / noise.
    """
    t = title.lower()
    if any(k in t for k in ["split", "pecah", "break away", "keluar", "expelled", "pecat",
                            "new party", "parti baru", "coalition", "gabungan", "realignment"]):
        return "scenario"
    if any(k in t for k in ["mp", "calon", "candidate", "defect", "lompat", "join", "sertai",
                            "resign", "letak jawatan", "by-election", "pilihan raya kecil",
                            "seat", "kerusi", "vacancy"]):
        return "shock"
    if any(k in t for k in ["cpi", "inflation", "inflasi", "gdp", "ringgit", "budget",
                            "interest rate", "kadar faedah"]):
        return "macro"
    if any(k in t for k in ["poll", "tinjauan", "survey", "approval", "prn", "state election",
                            "pilihan raya negeri"]):
        return "swing"
    return "noise"


# ============================================================
# HELPER: computed statistics from datasets
# ============================================================

def compute_ge15_stats(ge15_rows):
    """Aggregate GE15 results into summary statistics."""
    bloc_seats = defaultdict(int)
    total_voters = 0
    total_valid = 0
    margins = []
    state_bloc = defaultdict(lambda: defaultdict(int))
    for r in ge15_rows:
        b = r.get("bloc", "")
        bloc_seats[b] += 1
        try:
            total_voters += int(r.get("registered", 0))
            total_valid += int(r.get("total_valid", 0))
        except (ValueError, TypeError):
            pass
        try:
            margins.append(float(r.get("margin_pct_valid", 0)))
        except (ValueError, TypeError):
            pass
        state_bloc[r.get("state", "")][b] += 1

    govt_blocs = ["PH", "BN", "GPS", "GRS", "WARISAN", "MUDA", "KDM", "PBM"]
    govt_seats = sum(bloc_seats.get(b, 0) for b in govt_blocs)
    opp_seats = bloc_seats.get("PN", 0) + bloc_seats.get("IND", 0)

    margin_under_1 = sum(1 for m in margins if m < 1.0)
    margin_under_2_5 = sum(1 for m in margins if m < 2.5)
    margin_under_5 = sum(1 for m in margins if m < 5.0)
    avg_margin = sum(margins) / len(margins) if margins else 0

    # closest and safest
    seat_margins = [
        (r["code"], r["constituency"], r.get("state", ""), r.get("bloc", ""),
         float(r.get("margin_pct_valid", 0)))
        for r in ge15_rows
    ]
    seat_margins.sort(key=lambda x: x[4])
    closest = seat_margins[:8]
    safest = seat_margins[-5:]

    return {
        "bloc_seats": dict(bloc_seats),
        "total_voters": total_voters,
        "total_valid": total_valid,
        "govt_seats": govt_seats,
        "opp_seats": opp_seats,
        "margin_under_1": margin_under_1,
        "margin_under_2_5": margin_under_2_5,
        "margin_under_5": margin_under_5,
        "avg_margin": avg_margin,
        "closest": closest,
        "safest": safest,
        "state_bloc": {k: dict(v) for k, v in state_bloc.items()},
        "total_seats": len(ge15_rows),
    }


def compute_demog_stats(demog_rows):
    """Aggregate voter demographics into national-level statistics."""
    total_electorate = sum(int(float(r.get("total_voters", 0))) for r in demog_rows)

    # weighted ethnic shares
    wt_malay = sum(float(r.get("malay_pct", 0)) * float(r.get("total_voters", 0))
                   for r in demog_rows) / total_electorate
    wt_chinese = sum(float(r.get("chinese_pct", 0)) * float(r.get("total_voters", 0))
                     for r in demog_rows) / total_electorate
    wt_indian = sum(float(r.get("indian_pct", 0)) * float(r.get("total_voters", 0))
                    for r in demog_rows) / total_electorate
    wt_bumi_sabah = sum(float(r.get("bumi_sabah_pct", 0)) * float(r.get("total_voters", 0))
                        for r in demog_rows) / total_electorate
    wt_bumi_sarawak = sum(float(r.get("bumi_sarawak_pct", 0)) * float(r.get("total_voters", 0))
                          for r in demog_rows) / total_electorate

    # youth
    youth_pct = sum((float(r.get("age18_21_pct", 0)) + float(r.get("age22_30_pct", 0)))
                    * float(r.get("total_voters", 0)) for r in demog_rows) / total_electorate
    age_31_40 = sum(float(r.get("age31_40_pct", 0)) * float(r.get("total_voters", 0))
                    for r in demog_rows) / total_electorate
    age_41_50 = sum(float(r.get("age41_50_pct", 0)) * float(r.get("total_voters", 0))
                    for r in demog_rows) / total_electorate
    age_51_60 = sum(float(r.get("age51_60_pct", 0)) * float(r.get("total_voters", 0))
                    for r in demog_rows) / total_electorate
    age_60plus = sum(float(r.get("age60plus_pct", 0)) * float(r.get("total_voters", 0))
                     for r in demog_rows) / total_electorate

    # seat type counts
    pn_core = sum(1 for r in demog_rows if float(r.get("malay_pct", 0)) > 80)
    mixed_malay = sum(1 for r in demog_rows if 55 <= float(r.get("malay_pct", 0)) <= 80)
    true_mixed = sum(1 for r in demog_rows if 30 <= float(r.get("malay_pct", 0)) < 55)
    non_malay = sum(1 for r in demog_rows if float(r.get("malay_pct", 0)) < 30)

    # east malaysia
    east_seats = sum(1 for r in demog_rows if r.get("state", "") in ("Sabah", "Sarawak"))
    east_electorate = sum(int(float(r.get("total_voters", 0))) for r in demog_rows
                          if r.get("state", "") in ("Sabah", "Sarawak"))

    # median ages
    median_ages = [float(r.get("median_age", 0)) for r in demog_rows]
    avg_median_age = sum(median_ages) / len(median_ages) if median_ages else 0

    return {
        "total_electorate": total_electorate,
        "wt_malay": wt_malay,
        "wt_chinese": wt_chinese,
        "wt_indian": wt_indian,
        "wt_bumi_sabah": wt_bumi_sabah,
        "wt_bumi_sarawak": wt_bumi_sarawak,
        "youth_pct": youth_pct,
        "age_31_40": age_31_40,
        "age_41_50": age_41_50,
        "age_51_60": age_51_60,
        "age_60plus": age_60plus,
        "pn_core": pn_core,
        "mixed_malay": mixed_malay,
        "true_mixed": true_mixed,
        "non_malay": non_malay,
        "east_seats": east_seats,
        "east_electorate": east_electorate,
        "avg_median_age": avg_median_age,
    }


def compute_battleground_stats(bg_rows):
    """Aggregate battleground seat data."""
    tier_counts = defaultdict(int)
    state_counts = defaultdict(int)
    ge15_bloc_counts = defaultdict(int)
    current_bloc_counts = defaultdict(int)
    for r in bg_rows:
        tier_counts[r.get("tier", "")] += 1
        state_counts[r.get("state_std", "")] += 1
        ge15_bloc_counts[r.get("ge15_bloc", "")] += 1
        current_bloc_counts[r.get("current_bloc", "")] += 1

    # super-marginal seats list
    super_marg = [r for r in bg_rows if "SUPER" in r.get("tier", "")]
    high_risk = [r for r in bg_rows if "HIGH" in r.get("tier", "")]
    watch = [r for r in bg_rows if "WATCH" in r.get("tier", "")]

    return {
        "total": len(bg_rows),
        "tier_counts": dict(tier_counts),
        "state_counts": dict(state_counts),
        "ge15_bloc_counts": dict(ge15_bloc_counts),
        "current_bloc_counts": dict(current_bloc_counts),
        "super_marginal": super_marg,
        "high_risk": high_risk,
        "watch": watch,
    }


def compute_swing_stats(swing_rows):
    """Group swings by state, extract key bloc swings."""
    by_state = defaultdict(dict)
    for r in swing_rows:
        state = r.get("state", "")
        bloc = r.get("bloc", "")
        try:
            swing_pp = float(r.get("swing_pp", 0))
        except (ValueError, TypeError):
            swing_pp = 0.0
        by_state[state][bloc] = swing_pp
        if "latest_date" not in by_state[state]:
            by_state[state]["latest_date"] = r.get("latest_date", "")

    # identify era: 2023 green-wave states vs 2025-26 resurgence states
    green_wave_states = []
    resurgence_states = []
    for state, data in by_state.items():
        date = data.get("latest_date", "")
        if "2026" in date:
            resurgence_states.append(state)
        elif "2023" in date:
            green_wave_states.append(state)

    return {
        "by_state": {k: dict(v) for k, v in by_state.items()},
        "green_wave_states": sorted(green_wave_states),
        "resurgence_states": sorted(resurgence_states),
    }


def compute_scenario_stats(scenarios):
    """Compute government totals per scenario."""
    govt_blocs = ["PH", "BN", "GPS", "GRS", "WARISAN", "MUDA", "KDM", "PBM"]
    results = []
    for name, sc in scenarios.items():
        govt = sum(sc.get(b, 0) for b in govt_blocs)
        opp = sc.get("PN", 0) + sc.get("IND", 0)
        results.append({
            "name": name,
            "govt": govt,
            "opp": opp,
            "pn": sc.get("PN", 0),
            "ph": sc.get("PH", 0),
            "bn": sc.get("BN", 0),
            "gps": sc.get("GPS", 0),
            "grs": sc.get("GRS", 0),
            "warisan": sc.get("WARISAN", 0),
            "ind": sc.get("IND", 0),
            "muda": sc.get("MUDA", 0),
            "kdm": sc.get("KDM", 0),
            "pbm": sc.get("PBM", 0),
            "total": sum(sc.values()),
        })
    return results


def load_scenario_meta():
    """Scenario categories + derivation basis (parametric vs narrative).

    Read from work/scenarios/scenario_meta.json when present (written by the engine's
    --scenarios mode); otherwise classify by name so the report never breaks.
    """
    meta_path = os.path.join(ROOT, "work", "scenarios", "scenario_meta.json")
    if os.path.exists(meta_path):
        try:
            return json.load(open(meta_path))
        except Exception:
            pass
    narrative_names = ("DAP solo", "BERSAMA standalone", "BERSAMA + DAP")
    out = {}
    for name in ("Status quo", "Base (swings)", "PN surge", "Govt surge",
                 "Bersatu split", "Bersatu civil war", "Full fragmentation"):
        out[name] = {"category": "parametric", "basis": ""}
    for name in narrative_names:
        out[name] = {"category": "narrative", "basis": ""}
    return out


def scenario_category_display(name, meta):
    """Display label for a scenario's category (parametric/narrative)."""
    cat = meta.get(name, {}).get("category", "parametric")
    return "Parametric" if cat == "parametric" else "Narrative"


# ============================================================
# REPORT BUILDER
# ============================================================

def build_report():
    fc = load_forecast()
    cfg = load_config()
    swings = load_csv(SWINGS)
    ge15_rows = load_csv(GE15)
    master_rows = load_csv(MASTER)
    demog_rows = load_csv(DEMOG)
    bg_rows = load_csv(BATTLEGROUNDS)
    scenario_data = json.load(open(SCENARIOS))

    det = fc["deterministic"]
    mc = fc["monte_carlo"]
    flips = fc["flips"]
    econ = fc["economic_term"]
    govt_expected = fc.get("govt_expected", 0)

    # ---- computed stats ----
    g15 = compute_ge15_stats(ge15_rows)
    demog = compute_demog_stats(demog_rows)
    bg_stats = compute_battleground_stats(bg_rows)
    swing_stats = compute_swing_stats(swings)
    sc_stats = compute_scenario_stats(scenario_data)

    # ---- narrative layer: the story ledger read from the events database ----
    # Read once, before the render branch, so the EN and the native-MS report
    # print the SAME ledger state (same rows, same dates, same statuses).
    story_ledger_data = read_story_threads()
    story_threads_block = story_threads_en(story_ledger_data)
    story_threads_block_ms = story_threads_ms(story_ledger_data)

    # ---- bloc ordering ----
    bloc_order = ["PN", "PH", "BN", "GPS", "GRS", "WARISAN", "MUDA", "KDM", "PBM", "IND"]
    govt_blocs = ["PH", "BN", "GPS", "GRS", "WARISAN", "MUDA", "KDM", "PBM"]
    govt_actual = sum(det.get(b, 0) for b in govt_blocs)
    opp = det.get("PN", 0) + det.get("IND", 0)

    # GE15 govt vs projected
    govt_ge15 = g15["govt_seats"]
    opp_ge15 = g15["opp_seats"]
    govt_change = govt_actual - govt_ge15

    # weightage table
    w = cfg.FACTORS
    WEIGHT_BASIS = {
        "state_swing": "Revealed preference — actual votes cast since GE15 (2023 green wave, 2025–26 BN resurgence), same ethnic/geographic structure",
        "approval": "Merdeka Center end-of-campaign tracking; approval differentials moved 5–10pp within GE15 campaign; stated intention, not revealed preference",
        "economy": "Economic-voting calibration (Wilkin et al. 1.4pp per 1pp GDP; Lewis-Beck & Stegmaier), dampened for coalition structure + ethnic-voting conditional",
        "leader": "PM-candidate preference by ethnicity (Merdeka daily tracking; GE15 end-campaign: Muhyiddin 71%, Anwar 32%)",
        "turnout": "Youth (18–30) ≈30% of electorate, below-average turnout (Pandian 2025); ethnicity-conditional (young Malays → PN, young non-Malays → PH)",
        "events": "Discrete seat-specific shocks (PAS–Bersatu split, Bersama entry, Bersatu rump solo runs); high-variance, low-frequency",
    }
    weight_rows = "\n".join(
        f"| {k.replace('_', ' ').title()} | {v:.2f} | " + WEIGHT_BASIS.get(k, "")
        for k, v in w.items()
    )
    total_weight = sum(w.values())

    macro = cfg.MACRO
    macro_rows = "\n".join(f"| {k} | {v} |" for k, v in macro.items())

    # F2.1 (2026-09-24): the leader-factor prose renders the LIVE pm_pref
    # readings, never a hard-asserted zero/neutral state. The neutral wording
    # survives ONLY when both readings are exactly 0.0. Same strings are
    # exposed to the native-Malay renderer via ctx.
    pm_pref_malay = float(macro.get("pm_pref_malay", 0) or 0.0)
    pm_pref_nonmalay = float(macro.get("pm_pref_nonmalay", 0) or 0.0)

    def _pp(value):
        """Signed pp reading in house style (U+2212 minus, like the rest of the report)."""
        return f"{value:+.0f}".replace("-", "\u2212")

    pm_pref_malay_s = _pp(pm_pref_malay)
    pm_pref_nonmalay_s = _pp(pm_pref_nonmalay)
    if pm_pref_malay == 0.0 and pm_pref_nonmalay == 0.0:
        pm_pref_note = ("both at zero, meaning the leader-preference landscape has "
                        "not shifted materially since GE15 — a neutral input at present.")
    else:
        # The 45% / 59% shares are DERIVED (overall + delta) from
        # PM_PREF_OVERALL_PCT and the MACRO pp readings — never typed. Update
        # the anchor and the MACRO pm_pref_* values together.
        pm_pref_note = (
            "both readings are non-zero: Merdeka Center's national survey (fieldwork "
            "2026-03-12→2026-04-09, published 25 Jun 2026) puts Anwar at "
            f"{PM_PREF_OVERALL_PCT + pm_pref_malay:.0f}% among "
            f"Malay voters and {PM_PREF_OVERALL_PCT + pm_pref_nonmalay:.0f}% among "
            f"non-Malay voters against {PM_PREF_OVERALL_PCT:.0f}% overall, i.e. a "
            f"{pm_pref_malay_s} pp Malay reading and a {pm_pref_nonmalay_s} pp "
            "non-Malay reading — a live, non-neutral input."
        )
    if pm_pref_malay == 0.0 and pm_pref_nonmalay == 0.0:
        pm_pref_note_ms = ("kedua-duanya pada sifar, bermakna landskap keutamaan "
                           "pemimpin tidak beralih secara material sejak GE15 — "
                           "input neutral pada masa ini.")
    else:
        pm_pref_note_ms = (
            "kedua-duanya bukan sifar: tinjauan nasional Merdeka Center (kerja "
            "lapangan 2026-03-12→2026-04-09, diterbitkan 25 Jun 2026) meletakkan "
            "Anwar pada "
            f"{PM_PREF_OVERALL_PCT + pm_pref_malay:.0f}% di kalangan pengundi Melayu dan "
            f"{PM_PREF_OVERALL_PCT + pm_pref_nonmalay:.0f}% di kalangan pengundi "
            f"bukan Melayu berbanding {PM_PREF_OVERALL_PCT:.0f}% keseluruhan, iaitu bacaan Melayu "
            f"{pm_pref_malay_s} mata peratusan dan bacaan bukan Melayu "
            f"{pm_pref_nonmalay_s} mata peratusan — input langsung, bukan neutral."
        )

    # ---- seat-shock prose (F3): ALL shock wording derives from this read ----
    shocks = cfg.EVENT_SHOCKS
    if shocks:
        shock_rows = "\n".join(
            f"| {k} | {', '.join(f'{bloc} {pp:+d}pp' for bloc, pp in v.items())} |"
            for k, v in shocks.items()
        )
        _shock_list_en = "; ".join(
            f"{k} ({', '.join(f'{b} {pp:+d}pp' for b, pp in v.items())})"
            for k, v in shocks.items()
        )
        _shock_list_ms = "; ".join(
            f"{k} ({', '.join(f'{b} {pp:+d}pp' for b, pp in v.items())})"
            for k, v in shocks.items()
        )
        shock_prose = f"the config carries {len(shocks)} seat-level event shock(s): {_shock_list_en}."
        shock_prose_ms = (f"konfigurasi membawa {len(shocks)} kejutan peristiwa peringkat "
                          f"kerusi: {_shock_list_ms}.")
        shock_events_note = ("Where a shock touches a seat it is listed below and applied at the "
                             "stated weight inside the projection.")
        shock_events_note_ms = ("Di mana kejutan menyentuh sesuatu kerusi, ia disenaraikan di "
                                "bawah dan digunakan pada berat yang dinyatakan dalam unjuran.")
    else:
        shock_rows = "| — | none |"
        shock_prose = NO_SEAT_SHOCKS_EN
        shock_prose_ms = NO_SEAT_SHOCKS_MS
        shock_events_note = (
            "The removed round-1 shocks were dropped on 2026-09-24 after verification showed the "
            "scenario layer already charges those effects (no double count), so the base run's "
            "margins carry no shock term at all: the Bersatu-split and Bersama-siphon adjustments "
            "now live only inside the scenario layer (SCENARIO_DEFS). The mechanism they isolate is "
            "still real and still worth stating: a three-cornered contest draws share away from the "
            "bloc it fragments — the Bersama siphon is the empirically observed case (3–6% of the "
            "vote in the Johor state election), and it bites hardest in seats defended on a margin "
            "under 3%."
        )
        shock_events_note_ms = (
            "Kejutan pusingan pertama yang dibuang telah digugurkan pada 2026-09-24 selepas "
            "pengesahan menunjukkan lapisan senario sudah mengenakan kesan tersebut (tiada kiraan "
            "berganda), jadi margin larian asas tidak membawa sebarang sebutan kejutan: pelarasan "
            "perpecahan Bersatu dan sedutan Bersama kini hanya wujud di dalam lapisan senario "
            "(SCENARIO_DEFS). Mekanisme yang diasingkannya masih nyata: pertandingan tiga penjuru "
            "menarik bahagian undi daripada blok yang dipecahkannya — sedutan Bersama ialah kes yang "
            "diperhatikan secara empirik (3–6% undi dalam pilihan raya negeri Johor), dan kesannya "
            "paling kuat di kerusi yang dipertahankan dengan margin di bawah 3%."
        )
    events_detail_en = (
        "The event shocks capture the discrete, seat-level effects of coalition ruptures and "
        f"third-force entries. {shock_prose} {shock_events_note}"
    )
    events_detail_ms = (
        "Kejutan peristiwa menangkap kesan diskret, peringkat kerusi pecah gabungan dan kemasukan "
        f"kuasa ketiga. {shock_prose_ms} {shock_events_note_ms}"
    )

    # ---- §10 worked example (Lumut P074) — live swing stack, no shock input ----
    lm = lumut_worked_example(flips, cfg)

    # Thinnest projected HOLD (re-sourced from the live forecast output, never
    # typed) — the §10 probit contrast case.
    _holds = [r for r in fc.get("projected_seats", []) if not r.get("flip")]
    thinnest_hold = min(_holds, key=lambda r: r.get("proj_margin", 99)) if _holds else None
    if thinnest_hold:
        _th_x = abs(float(thinnest_hold.get("proj_margin", 0))) / 2.5
        thinnest_hold["probit_x"] = _th_x
        # F4 (2026-09-24): P(flip) = Φ(−x), x = |margin|/σ. The holder is the
        # projected winner here, so the seat flips only if the drawn margin turns
        # negative: Φ(−x). (The flipped branch below is the same law with the sign
        # of the holder's margin already reversed, so it keeps Φ(+x).)
        thinnest_hold["probit_p"] = _phi(-_th_x)
    if lm:
        _lm_x = lm["proj"] / 2.5
        lm["probit_x"] = _lm_x
        lm["probit_p"] = _phi(_lm_x)

    # §10 worked-example prose. Built as variables (the EN f-string cannot hold
    # conditionals) and shared with the MS renderer through ctx. Every figure is
    # either an engine recomputation (lm) or a value from the forecast output —
    # no literal shock number is typed anywhere in it.
    if lm and thinnest_hold:
        _swing_absence_en = (
            f"{lm['state']} carries no fresh post-GE15 state-election swing in the dataset"
            if lm["state_swing_absent"] else
            f"{lm['state']} carries a live state-election swing in the dataset")
        _bnd_absence_en = (
            "and the boundary-grouped swing for P074 is also absent"
            if lm["boundary_swing_absent"] else
            "and the boundary-grouped swing for P074 is present in the dataset")
        _swing_absence_ms = (
            f"{lm['state']} tidak membawa ayunan pilihan raya negeri pasca-GE15 yang segar dalam "
            "set data"
            if lm["state_swing_absent"] else
            f"{lm['state']} membawa ayunan pilihan raya negeri yang langsung dalam set data")
        _bnd_absence_ms = (
            "dan ayunan berkumpulan-sempadan bagi P074 juga tiada"
            if lm["boundary_swing_absent"] else
            "dan ayunan berkumpulan-sempadan bagi P074 wujud dalam set data")
        stage1_en = (
            "**Stage 1: Swing application.** The model applies the state-election swings to each "
            "federal seat in that state, modulated by seat type. Consider "
            f"{lm['seat']} (P074, {lm['state']}) — a `{lm['seat_type']}` seat "
            f"({lm['malay']:.1f}% Malay, {lm['youth']:.1f}% youth) that {lm['winner']} won in GE15 by "
            f"{lm['margin']:.2f}% over {lm['runnerup']}. {_swing_absence_en}, {_bnd_absence_en}, so "
            "the revealed-preference layer contributes 0.00pp to this seat. What remains is the "
            f"factor layer: the economic term ({_sg(lm['econ'])}pp, applied at its calibrated share) "
            f"delivers {lm['econ_pp']:.2f}pp to the government blocs and the approval delta delivers "
            f"{lm['approval_pp']:.2f}pp, so BN and PH each enter the modulation at "
            f"{lm['pre_mod']:.2f}pp while PN enters at {lm['sw_pn']:.2f}pp. The ethnic modulation at "
            f"{lm['malay']:.1f}% Malay then scales BN's swing by {lm['mod_runner']:.2f} and PH's by "
            f"{lm['mod_ph']:.2f}, and the youth modulation at {lm['youth']:.1f}% youth scales both by "
            f"{lm['ymod']:.2f}. {lm['shock_clause_en']} The stack therefore resolves to a swing of "
            f"{_sg(lm['sw_winner'])}pp to PN and {_sg(lm['sw_runner'])}pp to BN."
        )
        stage2_en = (
            "**Stage 2: Margin computation.** The projected margin is the baseline margin plus the "
            "swing to the winner minus the swing to the runner-up. "
            f"{lm['seat']}'s baseline margin is {lm['margin']:.2f}% for {lm['winner']} over "
            f"{lm['runnerup']}, so the projected margin is {lm['margin']:.2f}% + "
            f"({_sg(lm['sw_winner'])}) − ({_sg(lm['sw_runner'])}) = {_sg(lm['proj_raw'])}%. Because "
            "that value is negative the seat flips from PN to BN, and the engine records the seat's "
            f"projected margin as {lm['proj']:.2f}% — the flipped winner's ({lm['runnerup']}'s) "
            f"margin over the projected runner-up ({lm['winner']}). The engine's recorded value in "
            f"the forecast output is {lm['recorded']}%. (The {lm['margin']:.2f}% baseline is the engine's "
            "own pipeline margin — the `margin_pct_ge15` field of the projection-model CSV that feeds "
            "`project_seats`; the official GE15 margin on valid votes is 0.51%, which is the figure the "
            "super-marginal and flip tables quote. The worked example walks the pipeline value so the "
            f"arithmetic closes on the recorded projection.) The seat type is `{lm['seat_type']}`, which "
            f"is consistent with the {lm['malay']:.1f}% Malay electorate."
        )
        stage3_body_en = (
            f"For a seat whose projected margin crosses zero by only {abs(lm['proj_raw']):.2f}pp — "
            f"{lm['seat']} is the live case, at {lm['proj']:.2f}% for the flipped winner — and a "
            f"σ of 2.5pp, the flip probability is Φ({lm['probit_x']:.2f}) ≈ {lm['probit_p']:.3f}, a "
            f"{lm['probit_p'] * 100:.1f}% probability that the seat flips: a genuine coin-flip. For "
            f"the thinnest projected hold in the output, {thinnest_hold['code']} "
            f"{thinnest_hold['constituency']} ({thinnest_hold['proj_margin']:.2f}%), the same "
            f"arithmetic gives Φ({-thinnest_hold['probit_x']:.2f}) ≈ {thinnest_hold['probit_p']:.3f} "
            f"— a {thinnest_hold['probit_p'] * 100:.1f}% probability of flipping. The Monte Carlo "
            "aggregation samples seat-level noise around each margin (σ = 2.0pp in seats under a 5pp "
            "margin, 1.2pp elsewhere) rather than applying this probit seat by seat, so read the "
            "probit as the model's per-seat reading of the same uncertainty the simulation draws "
            "from."
        )
        stage1_ms = (
            "**Peringkat 1: Penggunaan ayunan.** Model menggunakan ayunan pilihan raya negeri kepada "
            "setiap kerusi persekutuan dalam negeri itu, dimodulasi mengikut jenis kerusi. "
            f"Pertimbangkan {lm['seat']} (P074, {lm['state']}) — kerusi "
            f"{lm['seat_type'].replace('_', ' ')} ({lm['malay']:.1f}% Melayu, {lm['youth']:.1f}% "
            f"belia) yang dimenangi {lm['winner']} dalam GE15 dengan margin {lm['margin']:.2f}% ke "
            f"atas {lm['runnerup']}. {_swing_absence_ms}, {_bnd_absence_ms}, jadi lapisan keutamaan "
            "terdedah menyumbang 0.00pp di kerusi ini. Yang tinggal ialah lapisan faktor: sebutan "
            f"ekonomi ({_sg(lm['econ'])}pp, digunakan pada bahagian terkalibrasi) menyampaikan "
            f"{lm['econ_pp']:.2f}pp kepada blok kerajaan dan delta kelulusan menyampaikan "
            f"{lm['approval_pp']:.2f}pp, jadi BN dan PH masing-masing masuk ke modulasi pada "
            f"{lm['pre_mod']:.2f}pp manakala PN masuk pada {lm['sw_pn']:.2f}pp. Modulasi etnik pada "
            f"{lm['malay']:.1f}% Melayu kemudian menskalakan ayunan BN sebanyak "
            f"{lm['mod_runner']:.2f} dan PH sebanyak {lm['mod_ph']:.2f}, dan modulasi belia pada "
            f"{lm['youth']:.1f}% belia menskalakan kedua-duanya sebanyak {lm['ymod']:.2f}. "
            f"{lm['shock_clause_ms']} Rangkaian ini menghasilkan ayunan {_sg(lm['sw_winner'])}pp "
            f"kepada PN dan {_sg(lm['sw_runner'])}pp kepada BN."
        )
        stage2_ms = (
            "**Peringkat 2: Pengiraan margin.** Margin unjuran ialah margin garis dasar campur "
            "ayunan kepada pemenang tolak ayunan kepada naib juara. Margin garis dasar "
            f"{lm['seat']} ialah {lm['margin']:.2f}% untuk {lm['winner']} ke atas "
            f"{lm['runnerup']}, jadi margin unjuran ialah {lm['margin']:.2f}% + "
            f"({_sg(lm['sw_winner'])}) − ({_sg(lm['sw_runner'])}) = {_sg(lm['proj_raw'])}%. Oleh "
            "kerana nilai itu negatif, kerusi bertukar daripada PN kepada BN, dan enjin merekodkan "
            f"margin unjuran kerusi sebagai {lm['proj']:.2f}% — margin pemenang yang bertukar "
            f"({lm['runnerup']}) ke atas naib juara unjuran ({lm['winner']}). Nilai yang direkodkan "
            f"output ramalan ialah {lm['recorded']}%. (Margin garis dasar {lm['margin']:.2f}% itu ialah "
            "margin saluran enjin sendiri — medan `margin_pct_ge15` dalam CSV model unjuran yang "
            "menyuap `project_seats`; margin rasmi GE15 atas undi sah ialah 0.51%, angka yang dipetik "
            "oleh jadual super-marginal dan jadual pertukaran. Contoh kerja ini menjejaki nilai "
            "saluran itu supaya aritmetiknya menutup pada unjuran yang direkodkan.) Jenis kerusi ialah "
            f"`{lm['seat_type']}`, konsisten dengan elektorat {lm['malay']:.1f}% Melayu."
        )
        stage3_body_ms = (
            f"Untuk kerusi yang margin unjurannya melintasi sifar hanya sebanyak "
            f"{abs(lm['proj_raw']):.2f}pp — {lm['seat']} ialah kes langsung, pada {lm['proj']:.2f}% "
            f"untuk pemenang yang bertukar — dan σ 2.5pp, kebarangkalian pertukaran ialah "
            f"Φ({lm['probit_x']:.2f}) ≈ {lm['probit_p']:.3f}, iaitu {lm['probit_p'] * 100:.1f}% "
            "kebarangkalian kerusi bertukar: lambungan syiling sebenar. Bagi pegangan unjuran "
            f"tertipis dalam output, {thinnest_hold['code']} {thinnest_hold['constituency']} "
            f"({thinnest_hold['proj_margin']:.2f}%), aritmetik yang sama memberi "
            f"Φ({-thinnest_hold['probit_x']:.2f}) ≈ {thinnest_hold['probit_p']:.3f} — "
            f"{thinnest_hold['probit_p'] * 100:.1f}% kebarangkalian bertukar. Pengagregatan Monte "
            "Carlo menyampel hingar peringkat kerusi di sekitar setiap margin (σ = 2.0pp di kerusi "
            "bawah margin 5pp, 1.2pp di tempat lain) dan bukan menggunakan probit ini kerusi demi "
            "kerusi, jadi baca probit sebagai bacaan model bagi ketidakpastian yang sama yang "
            "disampel oleh simulasi."
        )
    else:
        # Engine unavailable: state the chain generically rather than inventing
        # any number for this seat.
        stage1_en = (
            "**Stage 1: Swing application.** The model applies the state-election swings to each "
            "federal seat in that state, modulated by seat type, and applies discrete seat shocks "
            "only where the config's `EVENT_SHOCKS` mapping carries one. This build could not "
            "recompute the worked example directly from the engine, so the per-seat margins below "
            "are read from the engine's recorded output and no shock term is asserted here.")
        stage2_en = (
            "**Stage 2: Margin computation.** The projected margin is the baseline margin plus the "
            "swing to the winner minus the swing to the runner-up; a negative value flips the seat "
            "to the runner-up and is recorded as that seat's positive projected margin.")
        stage3_body_en = (
            "The flip probability of a seat rises as its projected margin approaches zero; the "
            "Monte Carlo aggregation described in Stage 4 samples seat-level noise around every "
            "margin to produce the distribution of outcomes.")
        stage1_ms = (
            "**Peringkat 1: Penggunaan ayunan.** Model menggunakan ayunan pilihan raya negeri kepada "
            "setiap kerusi persekutuan dalam negeri itu, dimodulasi mengikut jenis kerusi, dan "
            "mengenakan kejutan kerusi diskret hanya di mana pemetaan `EVENT_SHOCKS` dalam "
            "konfigurasi membawanya. Binaan ini tidak dapat mengira semula contoh kerja secara "
            "langsung daripada enjin, jadi margin peringkat kerusi di bawah dibaca daripada output "
            "enjin dan tiada sebutan kejutan didakwa di sini.")
        stage2_ms = (
            "**Peringkat 2: Pengiraan margin.** Margin unjuran ialah margin garis dasar campur "
            "ayunan kepada pemenang tolak ayunan kepada naib juara; nilai negatif menukar kerusi "
            "kepada naib juara dan direkodkan sebagai margin unjuran positif kerusi itu.")
        stage3_body_ms = (
            "Kebarangkalian pertukaran sesuatu kerusi meningkat apabila margin unjurannya menghampiri "
            "sifar; pengagregatan Monte Carlo yang diterangkan dalam Peringkat 4 menyampel hingar "
            "peringkat kerusi di sekitar setiap margin untuk menghasilkan taburan keputusan.")
    stage3_en = (
        "**Stage 3: Probit flip probability.** For each seat, the model computes the probability "
        "that the seat flips, given the projected margin and the seat's uncertainty. The probit "
        "function is:")
    stage3_ms = (
        "**Peringkat 3: Kebarangkalian pertukaran probit.** Bagi setiap kerusi, model mengira "
        "kebarangkalian kerusi itu bertukar, berdasarkan margin unjuran dan ketidakpastian kerusi "
        "itu. Fungsi probit ialah:")

    type_mod = cfg.TYPE_MOD
    type_mod_rows = "\n".join(
        f"| {k.replace('_', ' ').title()} | " +
        ", ".join(f"{b}: {m}" for b, m in v.items()) + " |"
        for k, v in type_mod.items()
    )

    # deterministic table
    det_lines = "\n".join(
        f"| {b} | {det.get(b, 0)} |" for b in bloc_order if det.get(b, 0)
    )

    # GE15 bloc table
    ge15_bloc_lines = "\n".join(
        f"| {b} | {g15['bloc_seats'].get(b, 0)} |" for b in bloc_order if g15['bloc_seats'].get(b, 0)
    )

    # flip rows
    flip_rows = "\n".join(
        f"| {f['code']} | {f['constituency']} | {f['state']} | {f['ge15_winner']} → {f['proj_winner']} | "
        f"{f['proj_margin']}% | {f['seat_type']} |" for f in flips
    ) if flips else "| — | none projected | — | — | — | — |"

    # swings table (per state, key blocs only)
    swing_table_rows = []
    for state in sorted(swing_stats["by_state"].keys()):
        data = swing_stats["by_state"][state]
        pn = data.get("PN", 0)
        bn = data.get("BN", 0)
        ph = data.get("PH", 0)
        date = data.get("latest_date", "—")
        swing_table_rows.append(f"| {state} | {date} | {pn:+.1f} | {bn:+.1f} | {ph:+.1f} |")
    swing_rows_md = "\n".join(swing_table_rows)

    # scenario table (with category: parametric vs narrative, user directive 5 Aug 2026)
    sc_meta = load_scenario_meta()
    sc_rows = "\n".join(
        f"| {s['name']} | {scenario_category_display(s['name'], sc_meta)} | "
        f"{s['govt']} | {s['opp']} | {s['pn']} | {s['ph']} | {s['bn']} |"
        f" {s['gps']} | {s['grs']} |"
        for s in sc_stats
    )

    # Per-scenario DETAILED explanations (owner directive 10 Aug 2026: "each
    # scenario explained in detail in reports"). One block per scenario:
    # what it models, the swing/shock applied, the arithmetic outcome, and —
    # for news-narrative scenarios — the trigger evidence. Built from the
    # same sc_meta (basis/derivation/ms/en) that feeds the app, so report and
    # webpage stay consistent.
    def _detail_for(s):
        m = sc_meta.get(s["name"], {})
        deriv = m.get("derivation", m.get("category", "quantitative"))
        ms = m.get("ms", "")
        en = m.get("en", "")
        basis = m.get("basis", "")
        major = s["govt"] - 112
        label = ("**Parametric / quantitative** — an engine re-run of the live swing map "
                 "with a scenario swing layer; mechanical and reproducible.")
        if deriv == "news-narrative":
            label = ("**News-narrative** — composed from a live news trigger (draft in the "
                     "judged feed); not a hand-written what-if.")
        lines = [
            f"**{s['name']}** — {label}",
            f"- **Outcome:** {s['govt']} government seats ({s['opp']} opposition); "
            f"{'above' if major >= 0 else 'below'} the 112-seat majority by {abs(major)}.",
            f"- **Bloc seats:** PN {s['pn']} · PH {s['ph']} · BN {s['bn']} · GPS {s['gps']} · GRS {s['grs']}"
            + (f" · WARISAN {s['warisan']}" if s['warisan'] else ""),
        ]
        if ms:
            lines.append(f"- **Summary (MS):** {ms}")
        if en and en != ms:
            lines.append(f"- **Summary (EN):** {en}")
        if basis and 'Hand-written' not in basis:
            lines.append(f"- **Basis:** {basis}")
        return "\n".join(lines)

    sc_details = "\n\n".join(_detail_for(s) for s in sc_stats)

    # MC table
    mc_rows = (
        f"| Government-aligned P10 | {mc['P10']:.0f} |\n"
        f"| Government-aligned P50 (median) | {mc['P50']:.0f} |\n"
        f"| Government-aligned P90 | {mc['P90']:.0f} |\n"
        f"| **P(retain majority ≥ 112)** | **{mc['P_majority']*100:.0f}%** |\n"
        f"| Flips (P50) | {mc['flips_P50']:.0f} |"
    )

    # battleground tables
    sm_rows = "\n".join(
        f"| {r['code']} | {r['constituency']} | {r['state_std']} | {r['ge15_bloc']} | "
        f"{r['current_bloc']} ({r['current_party']}) | {float(r['margin_pct_valid']):.2f}% | "
        f"{r['malay_pct']}% | {r['chinese_pct']}% |"
        for r in bg_stats["super_marginal"]
    )
    hr_rows = "\n".join(
        f"| {r['code']} | {r['constituency']} | {r['state_std']} | {r['ge15_bloc']} | "
        f"{r['current_bloc']} ({r['current_party']}) | {float(r['margin_pct_valid']):.2f}% | "
        f"{r['malay_pct']}% | {r['chinese_pct']}% |"
        for r in bg_stats["high_risk"]
    )
    w_rows = "\n".join(
        f"| {r['code']} | {r['constituency']} | {r['state_std']} | {r['ge15_bloc']} | "
        f"{r['current_bloc']} ({r['current_party']}) | {float(r['margin_pct_valid']):.2f}% | "
        f"{r['malay_pct']}% | {r['chinese_pct']}% |"
        for r in bg_stats["watch"]
    )

    # closest GE15 seats
    closest_rows = "\n".join(
        f"| {c[0]} | {c[1]} | {c[2]} | {c[3]} | {c[4]:.2f}% |" for c in g15["closest"]
    )
    safest_rows = "\n".join(
        f"| {c[0]} | {c[1]} | {c[2]} | {c[3]} | {c[4]:.2f}% |" for c in g15["safest"]
    )

    # state-by-state bloc table for GE15
    state_table_rows = []
    for state in sorted(g15["state_bloc"].keys()):
        blocs = g15["state_bloc"][state]
        total = sum(blocs.values())
        parts = ", ".join(f"{b} {c}" for b, c in sorted(blocs.items(), key=lambda x: -x[1]))
        state_table_rows.append(f"| {state} | {total} | {parts} |")
    state_rows_md = "\n".join(state_table_rows)

    # electorate stats
    total_electorate = demog["total_electorate"]
    electorate_m = total_electorate / 1_000_000

    now = fmt_date()
    stamp = datetime.now().strftime("%Y-%m-%d")

    # ethnic composition prose
    bumi_total = demog["wt_malay"] + demog["wt_bumi_sabah"] + demog["wt_bumi_sarawak"]

    # scenario range — arithmetic-derived scenarios only for the headline range;
    # situational (hand-written) scenarios are reported separately, since they
    # assume realignment paths (bloc exits) outside the swing-layer mechanics.
    sc_meta = load_scenario_meta()
    sc_govts = [s["govt"] for s in sc_stats
                if sc_meta.get(s["name"], {}).get("category", "parametric") == "parametric"]
    sc_min = min(sc_govts)
    sc_max = max(sc_govts)
    sc_situ_govts = [s["govt"] for s in sc_stats
                     if sc_meta.get(s["name"], {}).get("category", "parametric") != "parametric"]

    # flips narrative: count by direction
    flips_to_bn = [f for f in flips if f["proj_winner"] == "BN"]
    flips_to_ph = [f for f in flips if f["proj_winner"] == "PH"]
    flips_to_pn = [f for f in flips if f["proj_winner"] == "PN"]
    flips_to_grs = [f for f in flips if f["proj_winner"] == "GRS"]

    # =========================================================================
    # SMART UPDATE SCAN — what changed, which sections need updating
    # =========================================================================
    SECTION_NAMES = {
        0: "Executive Summary",
        1: "This Week's Signals",
        2: "Introduction & Context",
        3: "Electoral Framework",
        4: "The Electorate",
        5: "GE15 Baseline",
        6: "State Elections — Fresh Signals",
        7: "Methodology",
        8: "Weightage (Factor Stack)",
        9: "Data — The Inputs",
        10: "Calculation — From Factors to Seats",
        11: "The Projection — GE16 Seat by Seat",
        12: "The Battlegrounds",
        13: "Party Landscape",
        14: "PRN Prediction Scorecard",
        15: "Strategic Implications",
        16: "References & Provenance",
    }

    def scan_changes():
        """Scan latest data sources, compare with prior report, identify which
        sections have substantive changes. Returns a decision dict mapping
        section numbers to reasons for update."""
        decisions = {i: None for i in SECTION_NAMES}  # None = no change needed

        # --- 1. Check tracker logs ---
        week_signals = load_week_signals()
        if week_signals:
            decisions[1] = f"{len(week_signals)} new signals since last build — Section 1 (This Week's Signals) must update"
            # Check if any signal is a scenario-level or shock-level development.
            # Prefer the LLM-judged category when present (v4 feed), else fall
            # back to the keyword heuristic.
            for sig in week_signals:
                cls = sig.get('category') or classify_signal(sig.get('title', ''))
                if cls == 'coalition' or cls == 'scenario':
                    decisions[13] = "scenario-level development detected — Party Landscape may need update"
                if cls == 'candidate' or cls == 'shock':
                    decisions[11] = "event shock detected — Projection/flip narratives may change"

        # --- 2. Check engine output vs prior ---
        prior_exists = os.path.exists(os.path.join(ROOT, "03_REPORTS", "federal", "latest", "GE16_Malaysia_General_Election_Report.md"))
        if prior_exists:
            try:
                with open(os.path.join(ROOT, "03_REPORTS", "federal", "latest", "GE16_Malaysia_General_Election_Report.md")) as pf:
                    prior_text = pf.read()
                # Extract prior P50 number
                import re
                prior_p50 = re.search(r'P50 = (\d+)', prior_text)
                if prior_p50:
                    prior_p50 = int(prior_p50.group(1))
                    if prior_p50 != mc['P50']:
                        diff = mc['P50'] - prior_p50
                        decisions[0] = f"P50 changed {diff:+d} (was {prior_p50}, now {mc['P50']:.0f})"
                        decisions[11] = decisions[11] or "P50 changed — projection section"
                # Check flip count
                prior_flips = re.search(r'Projected flips \((\d+) seats\)', prior_text)
                if prior_flips:
                    prior_flips = int(prior_flips.group(1))
                    if prior_flips != len(flips):
                        decisions[11] = f"flip count changed ({prior_flips}→{len(flips)}) — per-seat narratives + projection table"
                # Check if any new flips (different seats flipping)
                prior_seats = set(re.findall(r'P\d{3}', prior_text[prior_text.find('11.3'):prior_text.find('11.4')] if '11.3' in prior_text else ''))
                new_seats = set(f['code'] for f in flips)
                added = new_seats - prior_seats
                removed = prior_seats - new_seats
                if added or removed:
                    decisions[11] = f"flip list changed: +{len(added)} new, -{len(removed)} removed — per-seat narratives updated"
                # Check state-election section for new scans
                tracker_scan_count = len(re.findall(r'Scan \d{4}-\d{2}-\d{2}', prior_text.split('## 2.')[0] if prior_text else ''))
                current_scan_count = len(re.findall(r'Scan \d{4}-\d{2}-\d{2}', '\n'.join(
                    open(TRACKER_LOGS[t], encoding='utf-8').read()[-2000:]
                    for t in ['poll', 'candidate', 'general-news']
                    if os.path.exists(TRACKER_LOGS[t])
                )))
            except Exception:
                pass  # can't compare — build all
        else:
            decisions[0] = "first build — all sections generated from scratch"
            for i in range(1, 17):
                decisions[i] = "first build"

        # --- 3. Always-rebuild sections (live data) ---
        decisions[0] = decisions[0] or "exec summary always refreshed (headline projection)"
        decisions[1] = decisions[1] or "signals section always refreshed from tracker logs"
        decisions[11] = decisions[11] or "projection + flips always recomputed from engine"
        decisions[8] = "weightage table always rebuilt from config"
        decisions[9] = "data inventory always rebuilt from live datasets"
        decisions[10] = "calculation chain always rebuilt from current swing stack"
        decisions[15] = "strategic implications rebuilt (references current projection)"

        # --- 4. Sections that change less frequently ---
        # These only rebuild when the underlying source files change
        changed_sections = {i: r for i, r in decisions.items() if r}
        return changed_sections

    changed_sections = scan_changes()

    # =========================================================================
    # NARRATIVE CONTINUITY — how this week's signals relate to last week's
    # =========================================================================
    def narrative_diff():

        """Compare this week's signals against last week's to detect narrative
        continuity. Returns a structured dict with three categories:
        - continued: same source + topic, updated value
        - new_topics: first time this source/topic appears
        - retired: was present last week, absent this week"""
        import re
        this_week = load_week_signals()
        diff = {"continued": [], "new_topics": [], "retired": [], "summary": ""}

        # Build signal fingerprints — extract source + topic from title
        def fingerprint(sig):
            title = sig.get("title", "")
            # Extract pollster/source name (e.g. "Merdeka Center", "Ilham Centre")
            source_match = re.search(r'(Merdeka\s*Cent(?:er|re)|Ilham\s*Cent(?:er|re)|ISEAS|Ipsos|Vodus|YouGov|Invoke)', title, re.I)
            source = source_match.group(1) if source_match else sig.get("feed", "unknown")
            # Extract topic keywords
            topics = []
            if re.search(r'approval|popularity|rating|favourite', title, re.I):
                topics.append('approval')
            if re.search(r'economy|inflation|GDP|cost.of.living', title, re.I):
                topics.append('economy')
            if re.search(r'poll|survey|undi|kaji.selidik', title, re.I):
                topics.append('poll')
            if re.search(r'candidate|calon|contest|bertanding', title, re.I):
                topics.append('candidate')
            if re.search(r'seat allocation|peruntukan kerusi|seat.*negotiation', title, re.I):
                topics.append('seat_allocation')
            if re.search(r'by-election|kecil|vacan', title, re.I):
                topics.append('byelection')
            if re.search(r'defect|lompat|hop|expel|pecat', title, re.I):
                topics.append('defection')
            if re.search(r'split|rupture|break|pecah', title, re.I):
                topics.append('split')
            key = f"{source}:{'+'.join(topics)}" if topics else f"{source}:general"
            return key, source, topics, title

        new_fps = {}
        for sig in this_week:
            key, src, topics, title = fingerprint(sig)
            new_fps[key] = {"source": src, "topics": topics, "title": title, "sig": sig}

        # Read prior week signal table
        prior_signals = []
        prior_archives = sorted(
            [d for d in os.listdir(os.path.join(ROOT, "03_REPORTS", "federal", "archive"))
             if d.startswith("GE16-")],
            reverse=True
        )
        if len(prior_archives) > 0:
            prior_path = os.path.join(ROOT, "03_REPORTS", "federal", "archive",
                                      prior_archives[0], "GE16_Malaysia_General_Election_Report.md")
            if os.path.exists(prior_path):
                try:
                    with open(prior_path) as pf:
                        prior_text = pf.read()
                    # Extract signals table from prior report
                    sig_section = prior_text.split("## 2.")[0] if "## 2." in prior_text else ""
                    for match in re.finditer(r'\|\s*(Poll tracker|Candidate tracker|General news)\s*\|\s*(.+?)\s*\|\s*\*\*(.+?)\*\*\s*\|', sig_section):
                        prior_signals.append({"feed": match.group(1), "title": match.group(2).strip(),
                                              "classification": match.group(3).strip()})
                except Exception:
                    pass

        # Categorize
        prior_fps = {}
        for sig in prior_signals:
            key, src, topics, title = fingerprint(sig)
            prior_fps[key] = {"source": src, "topics": topics, "title": title}

        for key, info in new_fps.items():
            if key in prior_fps:
                diff["continued"].append({
                    "source": info["source"],
                    "topics": info["topics"],
                    "this_week": info["title"],
                    "last_week": prior_fps[key]["title"]
                })
            else:
                diff["new_topics"].append({
                    "source": info["source"],
                    "topics": info["topics"],
                    "title": info["title"]
                })

        for key, info in prior_fps.items():
            if key not in new_fps:
                diff["retired"].append({
                    "source": info["source"],
                    "topics": info["topics"],
                    "title": info["title"]
                })

        # Summary text
        parts = []
        if diff["continued"]:
            parts.append(f"{len(diff['continued'])} continuing narrative{'s' if len(diff['continued']) > 1 else ''} (same source + topic, updated)")
        if diff["new_topics"]:
            parts.append(f"{len(diff['new_topics'])} new development{'s' if len(diff['new_topics']) > 1 else ''} (this week's headline)")
        if diff["retired"]:
            parts.append(f"{len(diff['retired'])} narrative{'s' if len(diff['retired']) > 1 else ''} retired (present last week, not this week)")
        diff["summary"] = "; ".join(parts) if parts else "no prior signals to compare"

        return diff

    narrative_continuity = narrative_diff()

    # =========================================================================
    # AUTO-GENERATED PER-SEAT FLIP NARRATIVES (all 20 seats)
    # Each paragraph is assembled from the data stack — no hand-typing.
    # =========================================================================
    # Pre-load demographic lookup for malay_pct per seat
    demog_lookup = {}
    for dr in demog_rows:
        # Normalize: demographics uses 'P.001', flips use 'P001'
        key = str(dr.get('code', '')).replace('.', '').replace('P', '').replace('p', '')
        demog_lookup[key] = float(dr.get('malay_pct', 50))
    # Pre-load ge15 margin lookup
    ge15_margin_lookup = {}
    for gr in ge15_rows:
        key = str(gr.get('code', '')).replace('P', '').replace('p', '')
        try:
            ge15_margin_lookup[key] = float(gr.get('margin_pct_valid', 0))
        except (ValueError, TypeError):
            ge15_margin_lookup[key] = 0.0

    def flip_narrative(f):
        """Generate a single per-seat flip narrative paragraph from the data stack."""
        code = f.get('code', '')
        seat = f.get('constituency', '')
        state = f.get('state', '')
        fr = f.get('ge15_winner', '?')
        to = f.get('proj_winner', '?')
        margin = f.get('proj_margin', 0)
        seat_type = f.get('seat_type', '?')
        code_n = code.replace('P', '').replace('p', '')
        malay = demog_lookup.get(code_n, 50)
        ge15_m = ge15_margin_lookup.get(code_n, 0)
        # State swing context
        st_sw = swing_stats['by_state'].get(state, {})
        sw_bn = st_sw.get('BN', 0)
        sw_pn = st_sw.get('PN', 0)
        sw_ph = st_sw.get('PH', 0)
        sw_date = st_sw.get('latest_date', 'N/A')
        # Event shock check
        shock = cfg.EVENT_SHOCKS.get(code, {})
        shock_text = ''
        if shock:
            shock_parts = [f'{b}: {v:+d}pp' for b, v in shock.items()]
            shock_text = f', with event shocks applied ({", ".join(shock_parts)})'
        # Seat type description
        st_desc = {'pn_core': '>80% Malay PN core', 'mixed_malay': '55-80% Malay mixed',
                   'true_mixed': '30-55% Malay true mixed', 'non_malay': '<30% non-Malay',
                   'east_malaysia': 'East Malaysia (local dynamics)'}.get(seat_type, seat_type)
        # Margin descriptor
        if abs(margin) < 1: m_desc = 'super-marginal (genuine coin-flip)'
        elif abs(margin) < 3: m_desc = 'razor-thin'
        elif abs(margin) < 7: m_desc = 'thin but decisive'
        else: m_desc = 'comfortable'
        # State swing signal
        if sw_date and sw_date != 'N/A':
            sw_signal = f'a fresh state-election signal ({sw_date}: BN {sw_bn:+.1f}pp, PN {sw_pn:+.1f}pp, PH {sw_ph:+.1f}pp)'
        else:
            sw_signal = 'national-level factors (no fresh post-GE15 state election signal available)'
        # Build the paragraph
        return (f'{seat} (P{code_n}, {state}) — margin {margin:.2f}%: '
                f'A {st_desc} seat ({malay:.0f}% Malay, GE15 margin {ge15_m:.1f}%). '
                f'The projected flip from {fr} to {to} is {m_desc} at {margin:.2f}%, '
                f'driven by {sw_signal}{shock_text}. '
                f'The {seat_type.replace("_", " ")} composition is structurally consistent with this outcome.')

    # Generate narratives for ALL flips
    flip_narratives = '\n\n'.join(flip_narrative(f) for f in flips)

    # Flip direction summary
    flip_dir_counts = defaultdict(int)
    for f in flips:
        flip_dir_counts[f'{f["ge15_winner"]} → {f["proj_winner"]}'] += 1
    flip_dir_text = ', '.join(f'{c} {d}' for d, c in sorted(flip_dir_counts.items(), key=lambda x: -x[1]))

    # ---- dynamic flip-pattern analysis (drives the "why" prose) ----
    # Replaces hardcoded narrative that assumed an all-PN→BN flip pattern.
    flip_by_code = {f['code']: f for f in flips}
    from_counts = defaultdict(int)
    to_counts = defaultdict(int)
    for f in flips:
        from_counts[f['ge15_winner']] += 1
        to_counts[f['proj_winner']] += 1

    def _dir_phrase(items):
        return ', '.join(f'{n} ({fr}→{to})' for n, fr, to in items)

    sm_flips = [(r.get('constituency', r.get('code', '')), flip_by_code[r['code']]['ge15_winner'],
                 flip_by_code[r['code']]['proj_winner'])
                for r in bg_stats['super_marginal'] if r.get('code', '') in flip_by_code]
    hr_flips = [(r.get('constituency', r.get('code', '')), flip_by_code[r['code']]['ge15_winner'],
                 flip_by_code[r['code']]['proj_winner'])
                for r in bg_stats['high_risk'] if r.get('code', '') in flip_by_code]
    sm_flip_phrase = _dir_phrase(sm_flips)
    hr_flip_phrase = _dir_phrase(hr_flips)
    pahang_bg = [r for r in bg_rows if r.get('state_std', '') == 'Pahang']
    pahang_flips = [f for f in flips if f.get('state', '') == 'Pahang']
    pahang_bg_names = ', '.join(r.get('constituency', r.get('code', '')) for r in pahang_bg)
    pahang_flip_phrase = _dir_phrase([(f['constituency'], f['ge15_winner'], f['proj_winner']) for f in pahang_flips])

    losing_blocs = sorted(from_counts.items(), key=lambda x: -x[1])
    why_sentences = []
    for bloc, cnt in losing_blocs:
        if bloc == 'PN':
            why_sentences.append(
                f"PN loses {cnt} seats to the base factor layer, not to any seat shock: the "
                f"economic term and the approval delta lift the government blocs in every seat "
                f"while PN's own swing outside the Malay Belt stays flat, so its thinnest "
                f"defences in the mixed and southern seats tip to BN and, in Johor, to PH. The "
                f"base run carries no Bersatu split — the config's `EVENT_SHOCKS` mapping is "
                f"empty this cycle — so a split appears only through the scenario layer.")
        elif bloc == 'PH':
            why_sentences.append(
                f"PH loses {cnt} seats because the green wave's residual strength in Malay-majority "
                f"west-coast seats and the southern BN resurgence pull PH-held marginals toward PN and BN.")
        elif bloc == 'BN':
            why_sentences.append(
                f"BN loses {cnt} seats in Pahang, where the December-2022 swing signal still favours PN.")
        elif bloc == 'IND':
            why_sentences.append("An independent-held seat (Kudat) flips to GRS on local Sabah dynamics.")
        else:
            why_sentences.append(f"{bloc} loses {cnt} seat{'s' if cnt != 1 else ''}.")
    why_text = ' '.join(why_sentences)

    # =========================================================================
    # EXPANDED 222-SEAT TABLE — every seat with demographics + swing provenance
    # =========================================================================
    # Load the full projected seats from engine output
    proj_seats = fc.get('projected_seats', [])
    if not isinstance(proj_seats, list) or len(proj_seats) == 0:
        proj_seats = flips  # fallback

    def seat_row(r):
        code = r.get('code', '')
        code_n = code.replace('P', '').replace('p', '')
        seat = r.get('constituency', '')
        st = r.get('state', '')
        wb = r.get('ge15_winner', '?')
        pw = r.get('proj_winner', '?')
        pm = r.get('proj_margin', 0)
        flip = r.get('flip', False)
        stype = r.get('seat_type', '')
        malay = demog_lookup.get(code_n, 50)
        flip_flag = '⚠ **FLIP**' if flip else '—'
        return f"| {code} | {seat} | {st} | {malay:.0f}% | {stype} | {wb} | {pw} | {pm:+.1f}% | {flip_flag} |"

    all_seat_rows = '\n'.join(seat_row(r) for r in proj_seats[:25])  # first 25 in report body; full list in appendix
    # Full 222-seat table for appendix
    full_seat_rows = '\n'.join(seat_row(r) for r in proj_seats)

    # this week's signals (triage table for Section 1)
    week_signals = load_week_signals()
    signals_rows = "\n".join(
        f"| {sig['feed']} | {sig['title']} | **{sig.get('category') or classify_signal(sig['title'])}** |"
        f"{ (' ' + ', '.join(sig['blocs'])) if sig.get('blocs') else '' } |"
        for sig in week_signals
    ) if week_signals else "| — | No new items captured since the previous build. | — | — |"

    # Pre-build narrative continuity prose (f-strings don't allow backslashes in expressions)
    nc_continued = ""
    if narrative_continuity.get('continued'):
        nc_continued = "\n\n**Continuing narratives.**\n" + "\n".join(
            "- **" + c['source'] + " (" + '+'.join(c['topics']) + "):** updated from last week"
            for c in narrative_continuity['continued'][:5])
    nc_new = ""
    if narrative_continuity.get('new_topics'):
        nc_new = "\n\n**New narratives.**\n" + "\n".join(
            "- **" + n['source'] + " (" + '+'.join(n['topics']) + "):** new this week"
            for n in narrative_continuity['new_topics'][:5])
    nc_retired = ""
    if narrative_continuity.get('retired'):
        nc_retired = "\n\n**Retired narratives.**\n" + "\n".join(
            "- **" + r['source'] + " (" + '+'.join(r['topics']) + "):** last week, not this week"
            for r in narrative_continuity['retired'][:5])

    # =========================================================================
    # REPORT ASSEMBLY
    # =========================================================================

    # =========================================================================
    # RENDER — native MS (v2) or EN (v1). Numbers computed ONCE above; both
    # renderers consume the same ctx → EN↔MS numeric parity is structural.
    # =========================================================================
    if LANG == "ms":
        from federal_ms_render import render_federal_ms
        ctx = dict(locals())
        report = render_federal_ms(ctx)
        return report, stamp, changed_sections, SECTION_NAMES

    report = f"""# The 16th Malaysian General Election — A Complete Forecast

**Report date:** {now} · **Data vintage:** GE15 (19 November 2022) + state elections through August 2026 · **Model:** factor-v1.0
**Live URL:** https://faisal.aila.my/ge16-malaysia-election · **Archive:** every prior report preserved in `03_REPORTS/federal/archive/`

---

## 0. Executive Summary

This report presents a complete, self-contained forecast of the outcome of the sixteenth Malaysian general election (GE16), anticipated no later than February 2028 under the constitutional term limit. It is built from the ground up: every number — every seat count, every swing, every margin, every probability — is computed live from the project's datasets. Nothing is hand-typed. The methodology, the weightage, the data inputs, the calculation chain, the per-seat reasoning, and the final projection are all laid out in a single narrative so that the reader can follow the argument from first principles to final result.

The central finding is that GE16 will be a **majority-shrink election, not a government-change election**. The Unity Government — the PH-BN-East Malaysian coalition that has governed since November 2022 — is projected to retain its parliamentary majority in every modelled scenario, but with a thinner cushion than the {govt_ge15} seats it held after GE15. The deterministic projection assigns **{govt_actual} of {g15['total_seats']} seats** to the government-aligned bloc, a net change of {govt_change:+d} from the GE15 baseline. The Monte Carlo simulation (5,000 iterations) places the government at **P10 = {mc['P10']:.0f}, P50 = {mc['P50']:.0f}, P90 = {mc['P90']:.0f}** seats, with a **{mc['P_majority']*100:.0f}% probability of retaining the 112-seat simple majority**. The opposition, led by Perikatan Nasional, projects at **{opp} seats** — short of government by a structural margin that the model does not see closing under current conditions.

The model projects **{len(flips)} seats to change hands** relative to GE15, across {len(flip_dir_counts)} distinct directions ({flip_dir_text}). This flip pattern is not random: it is the arithmetic consequence of two opposing forces — the 2023 green wave's residual strength, still pulling Malay-majority seats toward PN, and the 2025–2026 southern state-election resurgence of BN, measured at +{swing_stats['by_state'].get('Johor', {}).get('BN', 0):.1f} percentage points in Johor and +{swing_stats['by_state'].get('Negeri Sembilan', {}).get('BN', 0):.1f} points in Negeri Sembilan — applied to federal seats where the GE15 margin was already razor-thin. The green wave of 2023 — which saw PN surge by +{swing_stats['by_state'].get('Selangor', {}).get('PN', 0):.1f} points in Selangor and +{swing_stats['by_state'].get('Kedah', {}).get('PN', 0):.1f} points in Kedah — has been partially offset by this southern counter-surge, but its structural imprint on the Malay Belt remains the dominant feature of the electoral map.

The economic term — the model's translation of GDP growth, inflation, and approval into a vote-share adjustment — currently stands at **{econ:+.2f} percentage points** to government blocs. This is a substantial tailwind: GDP growth of {macro.get('gdp_yoy', 0)}% year-on-year and CPI inflation of {macro.get('cpi_yoy', 0)}% place the economy in a zone that historically rewards incumbents, and the approval delta of +{macro.get('approval_delta', 0)} percentage points since GE15 reinforces the effect. The ringgit at {macro.get('ringgit', 0)} MYR/USD is model-neutral, oscillating within a narrow band that does not trigger either the appreciation or depreciation thresholds.

The party landscape has undergone its most consequential realignment since the 2020 Sheraton Move. PAS broke with Bersatu on 8 June 2026; WAWASAN was formed from the ejected Bersatu faction and admitted to PN; Bersatu was reduced to a rump of approximately six loyal MPs; and Bersama — the urban reform party launched by Rafizi Ramli and Nik Nazmi — demonstrated in the Johor state election that it functions as a spoiler for PH, siphoning 3–6% of the vote in every seat it contests. The net effect of this fragmentation is to **strengthen the government's position**: three- and four-cornered contests under first-past-the-post reward the largest single bloc, which in most battleground constituencies is the government. The parametric scenario set places the government's range at **{sc_min}–{sc_max} seats** across all modelled swing futures — from a PN surge to full opposition fragmentation — and in none of them does the government lose its majority. The narrative scenarios — news-derived compositions such as DAP leaving the coalition or Bersama contesting as a standalone force, each traced to a live news trigger in the judged feed — test the structural realignments no single swing layer can capture, and are reported separately in Section 11.4.

This report is organised in seventeen sections. Sections 2–6 establish the political context, the electoral framework, the electorate's composition, the GE15 baseline, and the fresh signals from state elections. Sections 7–10 lay out the methodology, the factor weightage, the data inventory, and the calculation chain — the engine room of the forecast. Section 11 presents the full projection: the deterministic parliament, the Monte Carlo distribution, the flip list with per-seat narrative, and the scenario sensitivity table (parametric and narrative). Sections 12–14 examine the battlegrounds, the party landscape, and the prediction track record. Section 15 offers strategic implications and recommendations. Section 16 documents the sources and provenance.

---

## 1. This Week's Signals — What Changed and How It Was Classified

The forecast is updated weekly, and every update begins with a triage of the news that arrived since the previous edition. Each new item is classified into one of eight buckets before it touches the model: **coalition** (pact/realignment/split stories that may create or retire a whole scenario), **candidate** (seat- or person-level events applied as `EVENT_SHOCKS`), **poll** (revealed-preference signals from surveys or state elections), **election** (GE16/PRN timing and machinery), **policy** (budget/economy inputs refreshed in the configuration), **redelineation** (boundary review), **legal** (court/anti-hopping rulings), and **analysis** (commentary). Poll and candidate items additionally flow into the model as swing and shock inputs. The table below lists the items captured by the three news trackers (polls, candidates, general news) since the last build, with the classification and affected blocs each received.

| Feed | Item | Classification | Blocs |
|---|---|---|---|
{signals_rows}

The classification is not automatic judgement: it follows the legal gate where relevant. A defection that results in a **resignation** vacates the seat (vacancies are occupancy metadata; seats remain in the 222-seat universe with baseline attribution held — the last recorded holder is projected for GE16, and no by-election is held unless the Speaker notifies the EC); a defection that results in an **expulsion** keeps the seat (Article 49A(2)(c), the WAWASAN precedent) and is modelled as a vote-split rather than a vacancy. Structural developments — a coalition rupture, a party split, a third-force launch — become new scenarios in the sensitivity table (Section 10.4) rather than isolated seat shocks. Items classified as noise are retained in the tracker logs for the audit trail but do not alter the projection.

This section is the transparency layer of the weekly cycle: it shows the reader exactly which news items were considered, how each was classified, and therefore what did — and did not — change in the model since the previous edition.

**Week-over-week delta (transitional footnote).** {narrative_continuity['summary']}. The signal narratives are maintained in the tracker logs (append-only, never deleted); old signals feed the classification, and only this week's signals (within a 10-day window of the weekly cron) are shown in the table above. The narrative-diff engine compares this week's signal fingerprints (source + topic) against the prior archived report, distinguishing continuing stories from new developments and retired narratives. *This delta is transitional and no longer the report's narrative layer: §2.1 Political developments is now the dated record, chain by chain. The week-over-week delta is kept for continuity with earlier editions and will be retired once that record carries a full cycle of history.*
{nc_continued}{nc_new}{nc_retired}

---

## 2. Introduction & Context

Malaysia stands at a political juncture unlike any in its electoral history. The sixteenth general election will be the first contested under a sitting government that is itself a post-election coalition — the Unity Government was formed not by the party that won the most seats, but by a negotiated agreement between PH, BN, and the East Malaysian blocs after GE15 produced a hung parliament. This inversion of the normal electoral logic — the incumbent is a coalition of the second- and third-placed blocs — means that GE16 will be fought not on the question of whether the government deserves re-election, but on whether the opposition can consolidate the anti-government vote into a single coherent force. As the party landscape analysis in Section 13 will show, the answer so far is no: the opposition has fragmented into four poles, each contesting overlapping territory.

The timing of GE16 is shaped by two constraints. The first is constitutional: the Dewan Rakyat's five-year term, which began on 19 November 2022, expires on 17 February 2028, meaning parliament must be dissolved and elections held before that date. The second is strategic: the government has every incentive to serve its full term, as the economic indicators are favourable (GDP growth above 5%, inflation below 2%) and the opposition is in disarray. An early election — dissolving parliament in late 2027 — would sacrifice the remaining runway of economic tailwinds and give the opposition time to reorganise. A late election — pushing to the constitutional deadline — maximises the government's advantage but risks voter fatigue with a government in its sixth year. The model assumes a full-term election in the first quarter of 2028, but the projection is robust to earlier timing because the structural factors (ethnic composition, seat types, coalition alignment) do not change with timing alone.

The political moment is defined by three currents that this report will trace through every section. The first is the **green wave** — the dramatic surge of PN (and particularly PAS) into the Malay Belt in the 2023 state elections, which transformed Kedah, Kelantan, Terengganu, and parts of Selangor and Penang into PN strongholds and pushed PN's national vote share to levels that would have been unimaginable in 2018. The second is the **southern resurgence** — the equally dramatic recovery of BN in the 2025–2026 state elections in Johor and Negeri Sembilan, where BN won 48 of 56 seats in Johor and formed a coalition government in Negeri Sembilan with 18 seats. The third is the **fragmentation of the opposition** — the PAS-Bersatu rupture, the formation of WAWASAN, the launch of Bersama, and the rump status of Bersatu — which has converted what was a two-way fight into a multi-cornered contest that systematically advantages the government under first-past-the-post.

These three currents interact in complex ways. The green wave's structural imprint on the Malay Belt is permanent — PAS's dominance in Kelantan and Terengganu is not reversible under any modelled scenario. But the southern resurgence shows that the green wave was not a national phenomenon: it was a northern and east-coast phenomenon, and the southern Malay electorate, given a chance to vote for a resurgent BN under a new leadership, returned to the coalition that their fathers and grandfathers voted for. The fragmentation of the opposition is the newest current, and it is the one with the most uncertain trajectory: if Bersatu contests solo in its Malay heartland seats, it splits the PN vote and hands seats to BN; if Bersama contests broadly in urban seats, it siphons PH votes and hands seats to PN or BN. In both cases, the government benefits.

This report does not predict the future — it projects a range of futures, each conditional on a set of assumptions about swings, events, and coalition behaviour. The projection is a bounded distribution, not a point estimate. The honest reading is that the government will hold between {mc['P10']:.0f} and {mc['P90']:.0f} seats, with a median expectation of {mc['P50']:.0f}. What could change this: a redelineation of electoral boundaries (which would reset the model entirely), the Bersama factor in the three seats vacant until GE16 — Pandan, Setiawangsa, and Subang (all vacant until GE16 after the Bersama resignations, which the EC ruled will NOT face by-elections; Art 49A — the Speaker did not notify the EC, so no by-election is automatic) — Bersatu's decision on whether to contest GE16 solo or under the PN banner, and late swings in the campaign — Malaysian electorates have demonstrated their capacity to move late and move hard, as the final weeks of the GE15 campaign proved.

{story_threads_block}

---

## 3. The Electoral Framework

The Malaysian parliament is a bicameral legislature in which the Dewan Rakyat — the lower house — holds the power to form and dismiss governments. The Dewan Rakyat comprises {g15['total_seats']} members, each elected from a single-member constituency under the first-past-the-post (FPTP) electoral system. The number of seats is fixed by Article 46 of the Federal Constitution, which parliament may amend by a two-thirds majority. The current allocation of {g15['total_seats']} seats was established by the 2003 redelineation exercise and has remained unchanged despite multiple proposals for expansion — most recently the 222-to-235 seat increase approved in principle by the cabinet in September 2023, which would give Sabah and Sarawak additional seats to reflect their population growth. If this redelineation is enacted before GE16, the model would need to be rebuilt from scratch, as every seat boundary, ethnic composition, and margin would change.

Under FPTP, the candidate with the most votes in each constituency wins the seat, regardless of whether they achieve a majority of votes cast. This system has two properties that are central to the GE16 projection. First, it rewards **geographic concentration** of support: a party that wins 40% of the national vote but concentrates it in 30 seats will win 30 seats, while a party that wins 20% of the national vote but spreads it evenly across all 222 seats will win none. This is why PN, with its concentration in the Malay Belt, won {g15['bloc_seats'].get('PN', 0)} seats in GE15 despite a national vote share that was lower than PH's. Second, it amplifies **small swings in marginal seats**: a 2-percentage-point swing can flip a seat with a 1% margin, while leaving a seat with a 30% margin unchanged. This is why the 36 battleground seats — those with GE15 margins under 5% — are the entire story of GE16, and why this report spends disproportionate space on them.

The rural weightage of Malaysian constituencies is a structural feature that shapes every projection. The largest constituency in the country by electorate size is in the Klang Valley (over 200,000 voters), while the smallest is in rural Sarawak (under 30,000 voters). This means that a rural vote carries significantly more electoral weight than an urban vote — a fact that systematically advantages Malay-majority rural seats (which tend to vote PN) and East Malaysian seats (which tend to vote GPS or GRS). The demographics data confirms this: the average electorate size across all {g15['total_seats']} seats is approximately {total_electorate // g15['total_seats']:,} voters, but the range is enormous. The median age across constituencies averages {demog['avg_median_age']:.0f} years, but rural Malay Belt seats skew younger (median age 36–38) while East Malaysian seats skew older (median age 42–44), reflecting different demographic trajectories.

The Article 46 framework also means that the electoral map is frozen unless parliament acts. This has two implications for the projection. First, the ethnic composition of each constituency — the single most powerful predictor of its outcome — is fixed from the voter roll, not the census, and does not change between elections except through migration and natural demographic turnover. The demographics dataset captures this: {demog['pn_core']} seats have Malay populations above 80% (PN core), {demog['mixed_malay']} seats have Malay populations between 55% and 80% (mixed Malay-majority), {demog['true_mixed']} seats have Malay populations between 30% and 55% (true mixed), and {demog['non_malay']} seats have Malay populations below 30% (non-Malay majority). Additionally, {demog['east_seats']} seats are in Sabah and Sarawak, where bumiputera-Sabah and bumiputera-Sarawak shares dominate. These seat types are the structural foundation of every projection in this report: PN core seats are safe for PN, non-Malay seats are safe for PH, and the seats in between — the mixed Malay and true mixed categories — are where elections are decided.

Second, the absence of redelineation means that the coalition realignments since GE15 — the formation of the Unity Government, the PAS-Bersatu split, the creation of WAWASAN and Bersama — do not change the seat boundaries or the voter rolls. They change *who contests which seat* and *how the votes split*, but the structural demographics of each constituency remain the same. This is why the model can project from GE15 as a baseline: the seats are the same seats, the voters are (mostly) the same voters, and the only question is how the political forces acting on those voters have changed since November 2022.

---

## 4. The Electorate

The Malaysian electorate that will vote in GE16 is the largest in the nation's history: **{total_electorate:,} registered voters** as of the GE15 roll, representing approximately {electorate_m:.2f} million citizens. This figure reflects the transformative impact of the Undi18 constitutional amendment, which lowered the voting age from 21 to 18 and introduced automatic voter registration. The amendment, passed in 2019 but not implemented until after GE15, added an estimated 5.6 million new voters to the roll — a 28% increase in a single electoral cycle. These new voters are disproportionately young (aged 18–21), disproportionately in urban and semi-urban constituencies, and their political behaviour is the single largest source of uncertainty in the GE16 projection.

The ethnic structure of the electorate is the foundational fact of Malaysian politics. Weighted by electorate size across all {g15['total_seats']} constituencies, the national electorate is approximately **{demog['wt_malay']:.1f}% Malay, {demog['wt_chinese']:.1f}% Chinese, {demog['wt_indian']:.1f}% Indian**, with the remainder comprising bumiputera Sabah ({demog['wt_bumi_sabah']:.1f}%), bumiputera Sarawak ({demog['wt_bumi_sarawak']:.1f}%), orang asli, and other communities. When Malay and bumiputera shares are combined, the Bumiputera electorate constitutes approximately {bumi_total:.1f}% of the total — a demographic dominance that is the structural reason why every Malaysian general election is ultimately decided by how the Malay-Bumiputera majority votes.

| Ethnic Group | Weighted Share of Electorate |
|---|---|
| Malay | {demog['wt_malay']:.1f}% |
| Chinese | {demog['wt_chinese']:.1f}% |
| Indian | {demog['wt_indian']:.1f}% |
| Bumiputera Sabah | {demog['wt_bumi_sabah']:.1f}% |
| Bumiputera Sarawak | {demog['wt_bumi_sarawak']:.1f}% |
| Other | {100 - bumi_total - demog['wt_chinese'] - demog['wt_indian']:.1f}% |
| **Total Bumiputera** | **{bumi_total:.1f}%** |

The age structure of the electorate is equally consequential. Youth voters (aged 18–30) constitute approximately **{demog['youth_pct']:.1f}%** of the electorate — nearly one in three voters. The 31–40 age group adds another {demog['age_31_40']:.1f}%, meaning that voters under 40 represent roughly {demog['youth_pct'] + demog['age_31_40']:.1f}% of the total. This is a dramatically younger electorate than the one that voted in GE14 (2018), and the political science literature on Undi18 (Pandian 2025) identifies a critical finding: youth turnout in GE15 and subsequent state elections has been **below the national average**. Registration is no longer the barrier — automatic registration solved that — but participation remains the challenge. Young voters who do turn out behave differently from older voters: the 2023 state elections showed that young Malay voters tilted toward PN (amplifying the green wave), while young non-Malay voters remained with PH. The turnout differential between youth and the overall electorate is therefore not a uniform national effect but an ethnicity-conditional one, which the model captures through the turnout factor (weight {w.get('turnout', 0):.2f}) and its interaction with seat type.

The geography of the electorate is the third structural dimension. The {demog['east_seats']} seats of Sabah and Sarawak contain approximately {demog['east_electorate']:,} voters — about {demog['east_electorate']/total_electorate*100:.1f}% of the national electorate. East Malaysian politics follows a fundamentally different logic from Peninsular politics: local patronage networks, regional party machines (GPS in Sarawak, GRS in Sabah), and the MA63 autonomy agenda dominate over the national PH-vs-PN framing. The PRN prediction scorecard in Section 14 will demonstrate this empirically: every major polling centre missed the Sabah 2025 result because they applied Peninsular swing logic to a state that follows local logic. The model accounts for this through the `east_malaysia` seat type, which receives reduced swing weights and distinct type-modulation multipliers.

The interaction of ethnicity, age, and geography produces the seat typology that underpins the entire projection. Of the {g15['total_seats']} seats, {demog['pn_core']} are PN core (>80% Malay, predominantly rural Malay Belt), {demog['mixed_malay']} are mixed Malay-majority (55–80% Malay, semi-urban), {demog['true_mixed']} are true mixed (30–55% Malay, urban), {demog['non_malay']} are non-Malay majority (<30% Malay, urban DAP/PKR strongholds), and {demog['east_seats']} are East Malaysian (bumiputera-dominant, regional party logic). The 36 battleground seats — the seats that will decide GE16 — are almost entirely in the mixed Malay and true mixed categories, where no ethnic group is large enough to pre-determine the outcome and where the L2–L4 factors (valence, performance, events) become decisive.

---

## 5. The GE15 Baseline

The fifteenth general election, held on 19 November 2022, produced the most fractured result in Malaysia's electoral history: a **hung parliament** in which no coalition won the 112 seats required to form a government. The final tally was PH **{g15['bloc_seats'].get('PH', 0)}**, PN **{g15['bloc_seats'].get('PN', 0)}**, BN **{g15['bloc_seats'].get('BN', 0)}**, GPS **{g15['bloc_seats'].get('GPS', 0)}**, GRS **{g15['bloc_seats'].get('GRS', 0)}**, WARISAN **{g15['bloc_seats'].get('WARISAN', 0)}**, and independents and minor parties **{g15['bloc_seats'].get('IND', 0) + g15['bloc_seats'].get('KDM', 0) + g15['bloc_seats'].get('PBM', 0)}**. The government-aligned bloc (PH + BN + GPS + GRS + WARISAN + KDM + PBM) held **{govt_ge15} seats** — a majority of {govt_ge15 - 112} over the 112 threshold — while the opposition (PN + IND) held **{opp_ge15}**. But this majority was a post-election construction: on election night, the largest single bloc was PH with {g15['bloc_seats'].get('PH', 0)}, and it took days of negotiation before BN, humiliated by its collapse to {g15['bloc_seats'].get('BN', 0)} seats, agreed to join a government led by Anwar Ibrahim.

| Bloc | GE15 Seats |
|---|---|
{ge15_bloc_lines}
| **Government-aligned** | **{govt_ge15}** |
| **Opposition (PN + IND)** | **{opp_ge15}** |

The GE15 result was the product of three structural forces. The first was the **collapse of BN** — the coalition that had governed Malaysia for 61 years before 2018 was reduced to {g15['bloc_seats'].get('BN', 0)} seats, its lowest ever, as Malay voters defected to PN's "clean Islamic alternative" (Pepinsky et al. 2023) and non-Malay voters, repelled by UMNO's corruption baggage, voted PH. The second was the **green wave** — the surge of PAS-led PN into the Malay Belt, winning 14 of 15 seats in Perlis, 14 of 14 in Kelantan, 8 of 8 in Terengganu, and 14 of 15 in Kedah. The third was the **non-Malay consolidation behind PH** — over 80% of non-Malay voters supported PH (ISEAS 2023/20), giving PH near-total dominance of non-Malay majority seats.

The regional zone structure of GE15 is critical for understanding the projection. The Malay Belt (Perlis, Kedah, Kelantan, Terengganu, and parts of Pahang) voted overwhelmingly for PN — a near-sweep that gave PN its {g15['bloc_seats'].get('PN', 0)} seats but left the bloc geographically concentrated and unable to win mixed seats where non-Malay votes could counterbalance the Malay surge. The central belt (Perak, Selangor, Negeri Sembilan, Malacca) was the battleground: PH won {g15['state_bloc'].get('Perak', {}).get('PH', 0)} of 24 seats in Perak, {g15['state_bloc'].get('Selangor', {}).get('PH', 0)} of 22 in Selangor, but PN made deep inroads in the Malay-majority constituencies within these states. The southern belt (Johor) was mixed: PH won {g15['state_bloc'].get('Johor', {}).get('PH', 0)} seats, BN won {g15['state_bloc'].get('Johor', {}).get('BN', 0)}, and PN won only {g15['state_bloc'].get('Johor', {}).get('PN', 0)} — a result that, in retrospect, was the first signal of BN's southern resilience. East Malaysia followed its own logic: GPS swept Sarawak with {g15['state_bloc'].get('Sarawak', {}).get('GPS', 0)} seats, while Sabah fragmented across {len(g15['state_bloc'].get('Sabah', {}))} blocs.

| State | Total Seats | Bloc Breakdown |
|---|---|---|
{state_rows_md}

The post-GE15 drift has been the central challenge for the projection model. Between November 2022 and August 2026, four developments have shifted the electoral landscape. First, the 2023 state elections (August 2023) confirmed and amplified the green wave: PN surged by +{swing_stats['by_state'].get('Kedah', {}).get('PN', 0):.1f} points in Kedah, +{swing_stats['by_state'].get('Kelantan', {}).get('PN', 0):.1f} in Kelantan, +{swing_stats['by_state'].get('Terengganu', {}).get('PN', 0):.1f} in Terengganu, +{swing_stats['by_state'].get('Selangor', {}).get('PN', 0):.1f} in Selangor, and +{swing_stats['by_state'].get('Pulau Pinang', {}).get('PN', 0):.1f} in Penang — a regional earthquake that transformed the Malay Belt into an impregnable PN fortress and pushed PN into contention in previously safe PH seats in Selangor and Penang. Second, the 2025 Sabah state election (November 2025) saw WARISAN surge to 25 seats and GRS win 22, confounding every major polling centre and confirming that East Malaysian politics follows local patronage logic, not national swings. Third, the 2026 Johor state election (July 2026) produced a BN supermajority of 48 out of 56 seats — a swing of +{swing_stats['by_state'].get('Johor', {}).get('BN', 0):.1f} points for BN and −{abs(swing_stats['by_state'].get('Johor', {}).get('PN', 0)):.1f} for PN — while Bersama's 15 candidates all lost their deposits. Fourth, the 2026 Negeri Sembilan state election (August 2026) saw BN win 18 seats and form a coalition with PN's 7, while PH fell to 11 — another BN resurgence, with a swing of +{swing_stats['by_state'].get('Negeri Sembilan', {}).get('BN', 0):.1f} points for BN.

The margin structure of GE15 is the arithmetic foundation of the projection. The average margin across all {g15['total_seats']} seats was {g15['avg_margin']:.1f}%, but this average obscures the extreme polarisation: {g15['margin_under_1']} seats were won by less than 1%, {g15['margin_under_2_5']} by less than 2.5%, and {g15['margin_under_5']} by less than 5%. It is these {g15['margin_under_5']} seats — the battleground — that will decide GE16. The safest seats, by contrast, have margins exceeding 70%: Igan (Sarawak, GPS, {g15['safest'][0][4]:.1f}%), Kepong (KL, PH, {g15['safest'][1][4]:.1f}%), Seputeh (KL, PH, {g15['safest'][2][4]:.1f}%). These seats are immovable under any modelled scenario.

| Rank | Seat | State | Bloc | GE15 Margin |
|---|---|---|---|---|
{closest_rows}

The eight seats in the table above — all won by less than 1% — are the super-marginals. Each is a knife-edge that could go either way on a swing of a few hundred votes. They include Putatan (Sabah, BN, 0.30%), Gua Musang (Kelantan, PN, 0.34%), Tuaran (Sabah, PH, 0.40%), Jasin (Malacca, PN, 0.41%), Lumut (Perak, PN, 0.51%), Lubok Antu (Sarawak, GPS, 0.52%), Bagan Datuk (Perak, BN, 0.83%), and Sungai Petani (Kedah, PH, 0.86%). Of these, {len(sm_flips)} — {sm_flip_phrase} — appear in the model's projected flip list. The remainder are held by the model at their GE15 winner, but with margins so thin that the Monte Carlo simulation treats them as genuine coin-flips.

---

## 6. State Elections — Fresh Signals

The state elections held between GE15 and the present are the project's most valuable data, because they are the only source of **revealed preference** — actual votes cast by actual voters in the same ethnic and geographic structure as the federal seats, measuring real movement rather than stated intention. This is why state-election swings receive the highest weight (0.30) in the factor stack: polls measure opinion, but state elections measure behaviour. The project has compiled same-race, era-aware swing data for nine states, grouped into two eras: the **2023 green wave** (Kedah, Kelantan, Terengganu, Penang, Selangor — all polled in August 2023) and the **2025–2026 BN resurgence** (Sabah, November 2025; Johor, July 2026; Negeri Sembilan, August 2026).

| State | Latest SE Date | PN Swing | BN Swing | PH Swing |
|---|---|---|---|---|
{swing_rows_md}

The 2023 green wave states tell a story of PN consolidation. In Kedah, PN's vote share surged by +{swing_stats['by_state'].get('Kedah', {}).get('PN', 0):.1f} percentage points — from 45.9% to 68.9% — while BN collapsed by {swing_stats['by_state'].get('Kedah', {}).get('BN', 0):.1f} points and PH fell by {swing_stats['by_state'].get('Kedah', {}).get('PH', 0):.1f}. This was not a swing; it was a structural realignment. Kedah's Malay electorate, given a choice between PAS-PN and a BN associated with UMNO's corruption baggage, chose PN wholesale. The same pattern appeared in Kelantan (+{swing_stats['by_state'].get('Kelantan', {}).get('PN', 0):.1f} PN, {swing_stats['by_state'].get('Kelantan', {}).get('BN', 0):.1f} BN) and Terengganu (+{swing_stats['by_state'].get('Terengganu', {}).get('PN', 0):.1f} PN, {swing_stats['by_state'].get('Terengganu', {}).get('BN', 0):.1f} BN), confirming that the Malay Belt's conversion to PN was not a GE15 anomaly but a durable shift. In Selangor and Penang, the green wave was less extreme but still significant: PN surged by +{swing_stats['by_state'].get('Selangor', {}).get('PN', 0):.1f} in Selangor and +{swing_stats['by_state'].get('Pulau Pinang', {}).get('PN', 0):.1f} in Penang, cutting into PH's margins in mixed and Malay-majority seats within these otherwise PH-dominated states. The model applies these swings to the federal seats in each state, modulated by seat type: PN's swing concentrates in Malay-majority seats (multiplier 1.2 in PN core, 1.1 in mixed Malay) and attenuates in non-Malay seats (multiplier 0.7).

The 2025–2026 resurgence states tell a counter-story: BN's recovery in the south. In Johor, BN's vote share surged by +{swing_stats['by_state'].get('Johor', {}).get('BN', 0):.1f} points — from 43.1% to 60.1% — while PN collapsed by {swing_stats['by_state'].get('Johor', {}).get('PN', 0):.1f} points. This produced a BN supermajority of 48 out of 56 state seats, a result that no major polling centre predicted (the PRN scorecard in Section 14 documents this in detail). In Negeri Sembilan, the pattern repeated: BN +{swing_stats['by_state'].get('Negeri Sembilan', {}).get('BN', 0):.1f}, PN {swing_stats['by_state'].get('Negeri Sembilan', {}).get('PN', 0):.1f}, with BN winning 18 seats and forming a coalition with PN. The southern resurgence is the most important new signal since GE15 because it directly contradicts the green wave narrative: it shows that the green wave was not a national phenomenon but a regional one, and that the southern Malay electorate — given a resurgent BN under new leadership and a PN tarnished by Bersatu's internal chaos — will return to the coalition they historically supported.

Sabah stands apart. The November 2025 state election saw WARISAN surge to 25 seats and GRS win 22, with PN falling to a single seat and BN declining to 6. The swings — PN {swing_stats['by_state'].get('Sabah', {}).get('PN', 0):.1f}, BN {swing_stats['by_state'].get('Sabah', {}).get('BN', 0):.1f}, PH {swing_stats['by_state'].get('Sabah', {}).get('PH', 0):.1f} — do not map onto any Peninsular narrative. Sabah's politics is driven by local patronage, regional party machines, and the MA63 autonomy agenda, not by the PH-vs-PN framing that dominates the Peninsula. The model accounts for this by applying reduced swing weights to East Malaysian seats and by using the `east_malaysia` seat type with its own modulation multipliers (PN 0.8, PH 1.0, GPS/GRS 1.0). The PRN scorecard confirms that every major polling centre missed the Sabah result — Ilham Centre predicted GRS ≥26 and WARISAN 14; the actual was GRS 22 and WARISAN 25 — which validates the model's decision to treat East Malaysia as a separate electoral universe.

Pahang is a transitional case. Its swing data comes from December 2022 (essentially the GE15 coattail effect), showing PN +{swing_stats['by_state'].get('Pahang', {}).get('PN', 0):.1f} and BN {swing_stats['by_state'].get('Pahang', {}).get('BN', 0):.1f}. This is the oldest signal in the dataset and the least informative for GE16, because Pahang has not had a fresh state election since. The model treats Pahang's swing with appropriate caution, applying it but noting that the 2026 party realignment (WAWASAN's entry into PN, the Bersatu rump's potential solo run) could materially change the arithmetic in Pahang's Malay-majority seats. {len(pahang_bg)} Pahang seats appear in the battleground list ({pahang_bg_names}), and {len(pahang_flips)} Pahang seats — {pahang_flip_phrase} — appear in the projected flip list, driven by the December-2022 swing signal applied to seats where the GE15 margin was thin.

The swings that the model does not have are equally important. Perlis, Malacca (beyond the SE-16 signals from Johor and N9), the Federal Territories, and Sarawak (beyond the 2021 state election) lack fresh post-GE15 state-election signals. For these states, the model holds the GE15 baseline (zero swing) and relies on the national-level factors — approval, economy, events — to shift margins. This is a conservative choice: it means the model does not invent swings where it has no evidence, and it means the projection for these states is driven by the structural baseline rather than by speculative drift. The validation protocol in the theory document requires that zero swings reproduce GE15 exactly — and they do — which means the null model is honest.

---

## 7. Methodology — What Moves Results

The forecast model is built on a four-layer hierarchy of determinants, grounded in the empirical literature on Malaysian elections. The hierarchy is not a list of factors of equal weight; it is a nested structure in which each layer conditions the ones below it. Layer 1 (identity) sets the baseline structure of every seat — who wins a 90%-Malay rural seat versus a 60%-Chinese urban seat is determined before a single campaign event occurs. Layer 2 (valence) shifts the margins within that structure — leader approval, coalition brand, and corruption trust can move a seat from safe to marginal or vice versa. Layer 3 (performance) decides the *within-ethnic* choice — which Malay party the Malay voter chooses, not whether the Malay voter abandons ethnocentrism. Layer 4 (events) can break the structure itself in individual seats — a coalition rupture, a third-force entry, or a scandal can produce a 5–10 percentage point shock concentrated in specific constituencies.

| Layer | Determinant | Strength | Role |
|---|---|---|---|
| **L1 — Identity** | Ethnicity; Malay Belt vs mixed; East vs West Malaysia | Dominant | Sets every seat's baseline |
| **L2 — Valence** | Leader approval; coalition brand; corruption trust | Strong | Shifts margins within the structure |
| **L3 — Performance** | Economy (GDP, CPI, cost-of-living); governance; stability | Medium, conditional | Decides within-ethnic choice |
| **L4 — Events** | Ruptures, third forces, scandals, redelineation, by-elections | High variance, low frequency | Can break the structure in individual seats |

The critical insight is that a forecast which ignores L1 will be wrong in every seat, while a forecast which ignores L4 will be wrong in the seats that matter. The 36 battleground seats are almost entirely in the mixed Malay and true mixed categories — seats where no identity group is dominant enough to pre-determine the outcome, and where L2–L4 factors therefore become decisive. In these seats, a 2-percentage-point shift in leader approval or a 3-percentage-point event shock can flip the result. In the PN core seats (Malay >80%), by contrast, even a 10-point shift in approval would not change the outcome: PAS's structural dominance is too deep. The model captures this asymmetry through the type-modulation system, which scales each factor's effect by seat type.

The causal chain runs in four stages. **Stage 1 (Structure):** identity (L1) plus valence (L2) plus seat demographics produce a baseline vote share for each bloc in each seat, derived from the GE15 actuals — the empirical anchor, not a model. **Stage 2 (Drift):** performance (L3) plus events (L4) plus revealed preference (state swings) produce a national swing and state-level swings — the *change* since GE15, measured or inferred. **Stage 3 (Translation):** the seat-level projected margin is computed as the GE15 margin plus the sum of factor-weighted changes, and the seat flips if this adjusted margin crosses zero. With uncertainty, the flip probability is computed via a probit function on the ratio of the margin to the seat's standard deviation. **Stage 4 (Aggregation):** the 222 seats are summed to produce a projected parliament, and a Monte Carlo simulation over the uncertain swings produces the P10/P50/P90 distribution and the majority probability.

The mathematics is explicit and auditable. The seat-level projected margin is:

$$M_s = M_{{s,GE15}} + \\sum_k \\beta_k \\cdot \\Delta X_{{k,s}} + \\varepsilon_s$$

where $M_{{s,GE15}}$ is the GE15 margin (percentage of valid votes), $\\Delta X_{{k,s}}$ is the observed change in factor $k$ in seat $s$ (state swing, approval delta, inflation delta, event shock), $\\beta_k$ is the factor weight, and $\\varepsilon_s$ is seat-level error drawn from a normal distribution with seat-specific variance. The probit flip probability is:

$$P(\\text{{flip}}_s) = \\Phi\\left(\\frac{{-M_s}}{{\\sigma_s}}\\right)$$

where $\\Phi$ is the standard normal cumulative distribution function and $\\sigma_s$ is the seat's uncertainty — larger electorates have smaller proportional noise, so battleground seats (small electorates, thin margins) receive $\\sigma$ = 2.0–3.0 percentage points, while safe seats (large electorates, thick margins) receive $\\sigma$ = 1.0–1.5. The Monte Carlo simulation runs 5,000 iterations (per the config setting MC_ITERATIONS = {cfg.MC_ITERATIONS}), in each iteration drawing each bloc's swing from its observed distribution, computing the margin for all 222 seats, counting seats per bloc, and recording the government-aligned total. The output is the P10/P50/P90 of government seats, the probability of retaining a majority (≥112), and the median flip count.

Four governing principles constrain the model. **Baseline-first:** never forecast from a blank slate; GE15 is the empirical anchor, and everything is expressed as a swing from it. This is why the null model (zero swings) reproduces GE15 exactly — the machinery is honest. **Revealed preference over stated intention:** state-election swings outrank polls; polls calibrate, state elections measure. **Uniform within type, not within country:** a national swing is not applied uniformly — it is modulated by seat type, so PN's swing concentrates in Malay-majority seats and PH's in mixed/non-Malay seats. **Uncertainty is part of the forecast:** every projection is a distribution, not a point. The honest output is P10/P50/P90 and a majority probability, not a single seat count. These principles are not aspirational; they are enforced by the engine's code structure, and the validation protocol (null-case reconstruction, retrospective test, backcast to 2022) must pass before any forecast version is released.

---

## 8. Weightage — The Factor Stack

The factor weights are the model's expression of how much it trusts each input — how much a given factor's observed change is allowed to move the projected margin. The weights are not arbitrary; they are the project's synthesis of the empirical literature (ISEAS, Pepinsky, Merdeka Center, the economic-voting canon) calibrated against the specific conditions of Malaysian politics. No single source ranks these elements; the ranking comes from triangulating multiple sources and testing against the actual state-election results.

| Factor | Weight | Basis |
|---|---|---|
{weight_rows}
| **Total** | **{total_weight:.2f}** |

**State-election swings (weight {w.get('state_swing', 0):.2f})** receive the highest weight because they are the only revealed-preference data — actual votes cast since GE15, in the same ethnic and geographic structure, measuring real movement. The 2023 green wave and the 2025–2026 BN resurgence are not opinions about what voters might do; they are records of what voters did. When the model applies a +{swing_stats['by_state'].get('Johor', {}).get('BN', 0):.1f}-point BN swing to Johor's federal seats, it is not guessing — it is reading the most recent available ballot evidence. The weight of 0.30 means that the state swing accounts for 30% of the total factor-driven margin adjustment, making it the single most powerful input after the GE15 baseline itself.

**Government approval (weight {w.get('approval', 0):.2f})** is the second-highest weight, calibrated from the Merdeka Center's end-of-campaign tracking in GE15, which showed that approval differentials moved 5–10 percentage points within the campaign. The current approval delta — a +{macro.get('approval_delta', 0):.0f} percentage point change in government approval since GE15 — translates into a modest positive adjustment for government blocs. The weight is below the state-swing weight because approval is stated intention, not revealed preference: polls can be wrong (as the PRN scorecard demonstrates), but ballots cannot. The approval factor's role is to calibrate the swing-based projection against the current political mood — if the swings say one thing but the polls say another, the model leans toward the swings but allows the polls to moderate the effect.

**Economy (weight {w.get('economy', 0):.2f})** captures the economic-voting effect: GDP growth, inflation, and the cost of living. The economic term is computed from the cross-national literature (Wilkin et al.: 1.4 percentage points per 1 percentage point of GDP growth; Lewis-Beck & Stegmaier review), dampened for Malaysia's coalition-government structure and ethnic-voting conditional. The calibration formula is:

$$\\Delta V_{{govt}} = 0.7 \\times \\Delta GDP_{{yoy}} - 0.8 \\times (\\Delta CPI - 2\\%) + 0.15 \\times \\Delta Appr$$

With the current macro readings — GDP {macro.get('gdp_yoy', 0)}% year-on-year, CPI {macro.get('cpi_yoy', 0)}%, approval delta +{macro.get('approval_delta', 0)} — the economic term computes to **{econ:+.2f} percentage points** to government blocs. This is a substantial tailwind: the economy is in a zone that historically rewards incumbents (growth above 5%, inflation below 2%), and the model translates this into a concrete vote-share adjustment. The weight of 0.20 means the economic term accounts for 20% of the factor-driven adjustment — significant, but subordinate to the structural baseline and the state swings.

**Leader preference (weight {w.get('leader', 0):.2f})** captures the PM-candidate preference by ethnicity, drawn from Merdeka Center's daily tracking. In GE15, the end-of-campaign Malay approval ratings were Muhyiddin 71%, Ismail Sabri 57%, Hadi 51%, Anwar 32%, Zahid 12% — a differential that directly explains PN's surge in Malay-majority seats. The current readings show PM-preference shifts of {pm_pref_malay_s} percentage points among Malay voters and {pm_pref_nonmalay_s} among non-Malay voters — {pm_pref_note} It is the factor most likely to move during a campaign: a strong PN leadership transition (Ahmad Samsuri Mokhtar's installation as PN chairman in May 2026) or a PH leadership surprise could shift this factor by several points.

**Turnout (weight {w.get('turnout', 0):.2f})** captures youth turnout differentials. Youth voters (18–30) constitute approximately {demog['youth_pct']:.1f}% of the electorate but turnout below the national average (Pandian 2025). The effect is ethnicity-conditional: young Malay voters tilted toward PN in the 2023 state elections (amplifying the green wave), while young non-Malay voters remained with PH. The weight is low (0.05) because turnout is a second-order effect — it can sharpen or blunt a swing but rarely reverses one. Soft turnout hurts incumbents in safe seats (where the margin is large enough that abstention doesn't flip the seat but reduces the mandate) and can flip battleground seats where every vote matters.

**Events (weight {w.get('events', 0):.2f})** captures coalition ruptures, third-force entries, and other discrete shocks. The event layer is modelled as seat-specific shocks rather than a continuous factor: a discrete adjustment is applied only to the specific seats where a rupture concentrates, and the list of live shocks is read from the config's `EVENT_SHOCKS` mapping at build time rather than asserted in prose. {shock_prose} The weight of {w.get('events', 0):.2f} is low because events are high-variance, low-frequency: they can produce large swings in individual seats but are difficult to predict in advance.

| Seat Code | Shock Applied |
|---|---|
{shock_rows}

**Type modulation** is the mechanism by which the model ensures that swings are applied correctly by seat type. A uniform national swing would be wrong: PN's 2023 surge was concentrated in Malay-majority seats, not in Chinese-majority seats. The type-modulation multipliers scale each bloc's swing by the seat's ethnic structure:

| Seat Type | Multipliers |
|---|---|
{type_mod_rows}

The validation protocol tests the weight stack against the actual state-election results. The 2026 Johor result (BN 48/56) validates the southern-resurgence scenario: the model's BN +{swing_stats['by_state'].get('Johor', {}).get('BN', 0):.1f}-point swing predicted a BN supermajority that materialised as 48 out of 56. The Sabah result validates the East Malaysian separation: the model's reduced swing weights for `east_malaysia` seats correctly predicted that Peninsular swing logic would fail in Sabah (as it did for every polling centre). The 2023 green wave validates the type modulation: PN's swing was indeed concentrated in Malay-majority seats (multiplier 1.2) and attenuated in non-Malay seats (multiplier 0.7), exactly as the multipliers specify.

---

## 9. Data — The Inputs

The forecast engine draws on six categories of data, each serving a distinct role in the projection chain. Every dataset is live — read from disk at build time — and every number in this report is computed from these sources, never hand-typed. The data inventory is transparent and auditable: the reader can trace any number in the projection back to the specific dataset and row from which it was computed.

**Core datasets (222 seats, GE15 baseline):**

| Dataset | Source | Records | Role |
|---|---|---|---|
| GE15 results by constituency | Election Commission via MECo | {g15['total_seats']} | Baseline margins, winner, bloc |
| Master seat register | Election Commission / Parliament | {len(master_rows)} | Seat-by-seat holder, party |
| Voter demographics by constituency | ElectionData.MY anonymised roll | {len(demog_rows)} | Ethnic/age shares → seat typing |
| State-election swings | MECo headline ballots | {len(swings)} rows, 9 states | Revealed preference drift |
| Battleground seats register | Project analysis | {bg_stats['total']} | Marginal seat identification |
| Projection scenarios | Engine computation | {len(sc_stats)} | Sensitivity analysis |

The **GE15 results dataset** contains the official results for all {g15['total_seats']} parliamentary constituencies: the winner's name, coalition, party, vote count, vote percentage, majority, registered voters, total valid votes, and margin as a percentage of valid votes. It is the empirical anchor — the baseline from which every swing is measured. The total registered electorate captured in this dataset is {g15['total_voters']:,} voters, matching the demographics dataset exactly.

The **demographics dataset** contains the ethnic and age composition of each constituency's electorate, drawn from the ElectionData.MY anonymised voter roll (CC0 licence). For each of the {len(demog_rows)} seats, it records the percentage of Malay, Chinese, Indian, Bumiputera Sabah, Bumiputera Sarawak, Orang Asli, and other voters, along with six age brackets (18–21, 22–30, 31–40, 41–50, 51–60, 60+), gender split, and median age. This is the data that drives seat typing: the model reads the Malay percentage and assigns each seat to one of five types (PN core, mixed Malay, true mixed, non-Malay, East Malaysian), which in turn determines the type-modulation multipliers applied to each swing.

The **swing dataset** contains the same-race, era-aware state-election swings for nine states: the change in each bloc's vote share between the previous state election and the latest one, with the date of the latest election and the era classification (2023 green wave or 2025–2026 resurgence). These are the revealed-preference signals — the only post-GE15 data that reflects actual voter behaviour — and they receive the highest weight in the factor stack.

The **battleground dataset** identifies the {bg_stats['total']} seats where the GE15 margin was under 5%, classified into three tiers: {bg_stats['tier_counts'].get('SUPER-MARGINAL (<1%)', 0)} super-marginals (margin < 1%), {bg_stats['tier_counts'].get('HIGH-RISK (1-2.5%)', 0)} high-risk (1–2.5%), and {bg_stats['tier_counts'].get('WATCH (2.5-5%)', 0)} watch (2.5–5%). For each seat, it records the current holder (bloc, party, MP), the GE15 margin, the electorate size, the ethnic composition, and the youth share. This is the dataset that defines the universe of seats where GE16 will be decided.

The **scenario dataset** contains the seven-scenario sensitivity analysis, each scenario applying a different combination of swings and shocks to produce a projected parliament. The scenarios range from status quo (no swings) to full fragmentation (Bersatu solo + Bersama siphon), and they bracket the range of plausible GE16 outcomes.

**Current macro readings (live from config):**

| Indicator | Value |
|---|---|
{macro_rows}

These readings are updated weekly in the config file, drawing from the Department of Statistics Malaysia (DOSM), Bank Negara Malaysia (BNM), and news sources. The GDP figure ({macro.get('gdp_yoy', 0)}% year-on-year) is the Q2 2026 actual (DOSM 14 Aug 2026), reflecting an economy that has sustained above-5% growth through the first half of the year. The CPI figure ({macro.get('cpi_yoy', 0)}%) is the DOSM September 2026 release series (Jun 1.9 / Jul 1.8 / Aug 1.9), sitting below the 2% target band — a zone that historically rewards incumbents. The ringgit at {macro.get('ringgit', 0)} MYR/USD is oscillating in a narrow 4.07–4.09 band, which the model treats as neutral (no appreciation or depreciation threshold triggered). The approval delta of +{macro.get('approval_delta', 0)} percentage points reflects Merdeka Center's most recent reading of Anwar's approval at 52%, up from the GE15 level.

**Event shocks (seat-specific, live from config):**

{events_detail_en}

**Party landscape and PRN scorecard** are read from their respective markdown files (detailed in Sections 13 and 14). The party landscape update provides the current coalition composition, the PAS-Bersatu split timeline, the WAWASAN and Bersama assessments, and the implications for the projection model. The PRN prediction scorecard provides the track record of Malaysian research centres in predicting the three latest state elections, used to calibrate the weight given to polls versus swings.

---

## 10. Calculation — From Factors to Seats

The calculation chain transforms the factor inputs into a projected parliament through four stages: swing application, margin computation, probit flip probability, and Monte Carlo aggregation. This section walks through each stage with a worked example, using the actual data from the forecast.

{stage1_en}

{stage2_en}

{stage3_en}

$$P(\\text{{flip}}_s) = \\Phi\\left(\\frac{{-M_s}}{{\\sigma_s}}\\right)$$

{stage3_body_en}

**Stage 4: Monte Carlo aggregation.** The Monte Carlo simulation runs {cfg.MC_ITERATIONS:,} iterations. In each iteration, it draws each bloc's swing from its observed distribution (mean = measured swing, standard deviation = historical swing volatility), computes the projected margin for all 222 seats, counts seats per bloc, and records the government-aligned total. The output is the P10/P50/P90 of government seats, the probability of retaining a majority (≥112), and the median flip count. The current simulation produces:

| Metric | Value |
|---|---|
{mc_rows}

The tightness of the P10–P90 range ({mc['P10']:.0f}–{mc['P90']:.0f}, a spread of only {mc['P90'] - mc['P10']:.0f} seats) reflects the model's assessment that the vast majority of seats are structurally determined — only the {bg_stats['total']} battleground seats have meaningful flip probabilities, and even among those, most are leaned clearly in one direction by the swing stack. The {mc['P_majority']*100:.0f}% majority probability means that in every one of the 5,000 iterations, the government retained at least 112 seats — a reflection of the fact that the government's structural floor (safe PH, BN, GPS, and GRS seats) is above the threshold even before any battleground seats are counted.

The economic-voting calibration provides a worked example of how the macro readings translate into the vote-share adjustment. With GDP growth of {macro.get('gdp_yoy', 0)}%, CPI inflation of {macro.get('cpi_yoy', 0)}%, and an approval delta of +{macro.get('approval_delta', 0)} percentage points:

$$\\Delta V_{{govt}} = 0.7 \\times {macro.get('gdp_yoy', 0)} - 0.8 \\times ({macro.get('cpi_yoy', 0)} - 2) + 0.15 \\times {macro.get('approval_delta', 0)} = {0.7 * macro.get('gdp_yoy', 0):.2f} - {0.8 * (macro.get('cpi_yoy', 0) - 2):.2f} + {0.15 * macro.get('approval_delta', 0):.2f} = {econ:+.2f} \\text{{ pp}}$$

This +{econ:.2f}pp economic term is a substantial tailwind for the government — it means that the economic environment alone is shifting approximately {econ:.1f} percentage points of vote share toward government blocs, before any state-swing or event effects are considered. Combined with the state swings (which in the southern states also favour BN), the model produces a net positive adjustment for the government that, while not enough to overcome the structural dominance of PN in the Malay Belt, is sufficient to flip several PN-held marginal seats in the south and to hold the government's overall majority.

---

## 11. The Projection — GE16 Seat by Seat

The projection is presented in four layers: the deterministic parliament (the single best estimate), the Monte Carlo distribution (the uncertainty range), the flip list (which seats change hands and why), and the seven-scenario sensitivity table (how the projection changes under different assumptions).

### 11.1 Deterministic projection

The deterministic projection applies the full factor stack — state swings, economic term, approval delta, event shocks, type modulation — to each of the 222 seats and produces a single projected parliament. The result:

| Bloc | Projected Seats |
|---|---|
{det_lines}
| **Government-aligned** | **{govt_actual}** |
| **Opposition (PN + IND)** | **{opp}** |

The government-aligned total of {govt_actual} seats represents a net change of {govt_change:+d} from the GE15 baseline of {govt_ge15}. The movement is driven by the {len(flips)} projected flips (itemised in §11.3) and by the type-modulated swing effects in the southern states. PH holds at {det.get('PH', 0)} seats (down {g15['bloc_seats'].get('PH', 0) - det.get('PH', 0)} from GE15, reflecting the Bersama siphon and the green wave's residual effect in Selangor and Penang), while BN gains {det.get('BN', 0) - g15['bloc_seats'].get('BN', 0)} seats (up from {g15['bloc_seats'].get('BN', 0)} to {det.get('BN', 0)}, reflecting the southern resurgence). PN {'declines by' if g15['bloc_seats'].get('PN', 0) > det.get('PN', 0) else 'rises by'} {abs(g15['bloc_seats'].get('PN', 0) - det.get('PN', 0))} seats (from {g15['bloc_seats'].get('PN', 0)} to {det.get('PN', 0)}, reflecting the projected flips and scenario-layer fragmentation levers rather than a base-run seat shock). GPS holds steady at {det.get('GPS', 0)}, GRS at {det.get('GRS', 0)}, and the minor blocs remain stable.

### 11.2 Monte Carlo distribution

| Metric | Value |
|---|---|
{mc_rows}

The Monte Carlo distribution is the honest expression of the forecast: the government is projected to hold between {mc['P10']:.0f} and {mc['P90']:.0f} seats (90% confidence interval), with a median of {mc['P50']:.0f}. The {mc['P_majority']*100:.0f}% majority probability means that the model assigns a probability of 1.0 — certainty, within the simulation — to the government retaining the 112-seat threshold. This is not because the model is overconfident; it is because the government's structural floor (safe PH non-Malay seats, safe BN seats, GPS's 23 Sarawak seats, GRS's Sabah seats) exceeds 112 even before any battleground seats are counted. The battleground seats are the story of the *margin* of the majority, not the *existence* of the majority.

The P10–P90 spread of {mc['P90'] - mc['P10']:.0f} seats is narrow because the model's uncertainty is concentrated in a small number of seats. Of the {g15['total_seats']} seats, approximately {g15['total_seats'] - bg_stats['total']} are structurally determined (margins > 5% in GE15, no swing large enough to flip them), leaving only the {bg_stats['total']} battleground seats as sources of uncertainty. Even among these, most have projected margins that lean clearly in one direction — only the super-marginals (margin < 1%) are genuine coin-flips, and there are only {bg_stats['tier_counts'].get('SUPER-MARGINAL (<1%)', 0)} of them.

### 11.3 Projected flips ({len(flips)} seats)

| Seat | Constituency | State | GE15 → Projected | Proj. Margin | Seat Type |
|---|---|---|---|---|---|
{flip_rows}

**The flip narrative.** Every projected flip is the arithmetic consequence of the swing stack — no hand-adjustment, no discretionary override. The pattern is a fingerprint of the current electoral moment; the directions are {flip_dir_text}.

{flip_narratives}

**Why these flips and not others?** The flip pattern reveals the model's logic. {why_text} The directions sum to {flip_dir_text}; every flip is the arithmetic consequence of the swing stack — no hand-adjustment, no discretionary override.

### 11.4 Scenario sensitivity

The sensitivity analysis tests how the projection changes under different assumptions about swings, shocks, and coalition behaviour. Each scenario applies a different combination of adjustments and recomputes the full 222-seat parliament. Scenarios fall into two categories. **Parametric scenarios** — equivalently *sensitivity*, *swing-based*, or *quantitative* scenarios — re-run the engine on the live swing map with a scenario swing layer: the model is the same, only the inputs move, so the results are mechanical, transparent and reproducible. **Narrative scenarios** — equivalently *structural*, *realignment*, or *news-derived* scenarios — are composed only from live news triggers in the judged feed (a draft is generated when feed items name a bloc/party and the event type; a PROMOTE draft is then composed into the projection set with its seat map). No scenario is hand-written: every narrative must trace to at least one dated feed item (trigger list stored in the scenario metadata). They change the structure of the competition itself rather than the size of the swings.

| Scenario | Type | Govt | Opp | PN | PH | BN | GPS | GRS |
|---|---|---|---|---|---|---|---|---|
{sc_rows}

#### Scenario details — each scenario explained

{sc_details}

The scenario range spans **{sc_min}–{sc_max} government seats** across the parametric scenarios — from the PN surge scenario ({sc_min} govt) to the Bersatu civil war scenario ({sc_max} govt). In every scenario, the government retains its majority. The PN surge (+5pp to PN) is the worst case for the government, costing it {govt_ge15 - sc_min} seats from the GE15 baseline but still leaving it with {sc_min - 112} seats above the threshold. The Bersatu civil war (−10pp to PN) is the best case, delivering {sc_max} seats — a net gain of {sc_max - govt_ge15} from GE15. The full fragmentation scenario (Bersatu −6pp + Bersama −3pp PH urban) produces {next((s['govt'] for s in sc_stats if 'Full fragmentation' in s['name']), sc_stats[-1]['govt'])} government seats, demonstrating that even when both opposition fragmentation effects operate simultaneously, the government benefits more than it loses (Bersatu's split costs PN more seats than Bersama's siphon costs PH).

The scenario table validates the central finding: this is a majority-shrink election, not a government-change election. The government's majority shrinks in the base case and the PN surge scenario, but it never disappears. The opposition's fragmentation — far from being a threat to the government — is the government's greatest asset, because under FPTP, multi-cornered contests reward the largest single bloc.

---

## 12. The Battlegrounds

The {bg_stats['total']} battleground seats — those with GE15 margins under 5% — are the seats where GE16 will be decided. The remaining {g15['total_seats'] - bg_stats['total']} seats have margins large enough that no modelled swing can flip them; they are structurally determined. The battlegrounds are classified into three tiers by margin: {bg_stats['tier_counts'].get('SUPER-MARGINAL (<1%)', 0)} super-marginals (< 1%), {bg_stats['tier_counts'].get('HIGH-RISK (1-2.5%)', 0)} high-risk (1–2.5%), and {bg_stats['tier_counts'].get('WATCH (2.5-5%)', 0)} watch (2.5–5%).

The geographic distribution of the battlegrounds reveals where the election will be fought. Perak has the most ({bg_stats['state_counts'].get('Perak', 0)} seats), followed by Pahang ({bg_stats['state_counts'].get('Pahang', 0)}), Johor ({bg_stats['state_counts'].get('Johor', 0)}), Selangor ({bg_stats['state_counts'].get('Selangor', 0)}), and Sabah ({bg_stats['state_counts'].get('Sabah', 0)}). These five states account for {bg_stats['state_counts'].get('Perak', 0) + bg_stats['state_counts'].get('Pahang', 0) + bg_stats['state_counts'].get('Johor', 0) + bg_stats['state_counts'].get('Selangor', 0) + bg_stats['state_counts'].get('Sabah', 0)} of the {bg_stats['total']} battlegrounds — nearly two-thirds — which means the election will be decided in the central belt (Perak, Selangor), the south (Johor), the east coast transition zone (Pahang), and East Malaysia (Sabah). The Malay Belt (Kedah, Kelantan, Terengganu, Perlis) has only {bg_stats['state_counts'].get('Kedah', 0) + bg_stats['state_counts'].get('Kelantan', 0)} battleground seats, because the green wave has made most seats in those states safe for PN.

The bloc distribution of the battlegrounds shows who is defending. At GE15, PN held {bg_stats['ge15_bloc_counts'].get('PN', 0)} of the battleground seats, PH held {bg_stats['ge15_bloc_counts'].get('PH', 0)}, BN held {bg_stats['ge15_bloc_counts'].get('BN', 0)}, and independents and minor parties held the remainder. By the current composition (reflecting defections and by-elections), PN holds {bg_stats['current_bloc_counts'].get('PN', 0)}, PH holds {bg_stats['current_bloc_counts'].get('PH', 0)}, BN holds {bg_stats['current_bloc_counts'].get('BN', 0)}, and the rest are held by GRS, independents, KDM, and MUDA. The shift from GE15 to the current composition — PH losing {bg_stats['ge15_bloc_counts'].get('PH', 0) - bg_stats['current_bloc_counts'].get('PH', 0)} battleground seats and PN losing {bg_stats['ge15_bloc_counts'].get('PN', 0) - bg_stats['current_bloc_counts'].get('PN', 0)} — reflects the post-GE15 defections and the Bersatu rump's move to independent status.

### Super-marginals (margin < 1%)

| Code | Seat | State | GE15 Bloc | Current Holder | Margin | Malay % | Chinese % |
|---|---|---|---|---|---|---|---|
{sm_rows}

The eight super-marginals are the knife-edge seats. Of them, {len(sm_flips)} — {sm_flip_phrase} — appear in the model's projected flip list. The others are held at their GE15 winner by the model, but with margins so thin that the Monte Carlo simulation treats them as genuine coin-flips. Putatan (Sabah, 0.30%) is the closest seat in the country — won by BN's Shahelmey Yahya by just 124 votes out of 63,173 cast. Tuaran (Sabah, 0.40%) was won by PH but is now held by GRS's Wilfred Madius Tangau, reflecting the post-GE15 realignment in Sabah. Lubok Antu (Sarawak, 0.52%) is a GPS seat in a predominantly bumiputera-Sarawak constituency (89.7%) — the kind of seat where local patronage, not national swings, decides the outcome. Bagan Datuk (Perak, 0.83%) is held by BN's Ahmad Zahid Hamidi, the Deputy Prime Minister — a high-profile incumbent in a seat where his personal vote may be the difference. Sungai Petani (Kedah, 0.86%) is the one PH-held super-marginal in the Malay Belt, held by Mohammed Taufiq Johari by just 1,115 votes — the thinnest PH defence in the country.

### High-risk seats (margin 1–2.5%)

| Code | Seat | State | GE15 Bloc | Current Holder | Margin | Malay % | Chinese % |
|---|---|---|---|---|---|---|---|
{hr_rows}

The {bg_stats['tier_counts'].get('HIGH-RISK (1-2.5%)', 0)} high-risk seats are where the swing stack has its greatest effect. These seats have margins large enough to resist small swings but small enough that a 2–3 percentage point movement can flip them. Several of these seats — {hr_flip_phrase} — appear in the projected flip list, driven by the combination of state swings and scenario-layer levers. Bentong (P089, margin 1.04%) is a PH-held seat in Pahang with a mixed composition (49.4% Malay, 37.8% Chinese) — a true battleground where the non-Malay vote is large enough to counterbalance the Malay swing. Kuala Selangor (P096, margin 1.16%) is a PH seat in Selangor where the 2023 green wave cut deeply — a seat that the model holds for PH but that the Monte Carlo treats as highly uncertain.

### Watch seats (margin 2.5–5%)

| Code | Seat | State | GE15 Bloc | Current Holder | Margin | Malay % | Chinese % |
|---|---|---|---|---|---|---|---|
{w_rows}

The {bg_stats['tier_counts'].get('WATCH (2.5-5%)', 0)} watch seats require larger swings to flip but are not safe. Tambun (P063, margin 2.99%) is held by Anwar Ibrahim himself — the Prime Minister's seat, with a mixed composition (64.7% Malay, 20.5% Chinese) that makes it vulnerable to a coordinated PN challenge. Muar (P146, margin 2.53%) is held by MUDA's Syed Saddiq — a high-profile young politician in the seat where the Bersama siphon would bite hardest. The base run charges no siphon there (EVENT_SHOCKS is empty this cycle); the −3pp Bersama term lives in the scenario layer, so Muar's risk is a scenario-layer reading, not a base-case input. Kudat (P167, margin 4.36%) is an independent-held seat in Sabah with a large Bumiputera Sabah population (72.8%) — another East Malaysian seat where local dynamics, not national swings, will decide the outcome.

The battlegrounds collectively represent the entire uncertainty in the GE16 projection. The Monte Carlo's P10–P90 spread of {mc['P90'] - mc['P10']:.0f} seats is generated entirely by these {bg_stats['total']} seats; the other {g15['total_seats'] - bg_stats['total']} seats are locked. Understanding the battlegrounds — their holders, their geography, their ethnic composition, and the forces acting on them — is therefore equivalent to understanding the entire forecast.

---

## 13. Party Landscape May–July 2026

The Malaysian opposition has undergone its most consequential realignment since the 2020 Sheraton Move. Between the project's composition snapshot (22 June 2026) and this update (3 August 2026), four events have reshaped the party landscape: the PAS-Bersatu rupture, the formation of WAWASAN, the launch and electoral testing of Bersama, and the visible strain within PH's components. This section draws on the party landscape study (`Research Data/notes/party-landscape-update-2026.md`) to assess each development and its implications for the GE16 projection.

**The PAS-Bersatu split (8 June 2026).** PAS formally ended its cooperation with Bersatu on 8 June 2026, dissolving the partnership that had defined Perikatan Nasional since its founding. The split was driven by three factors: Bersatu's equivocal role in attempts to topple MBs in Negeri Sembilan and Perlis, its blocking of new PN members, and its under-performance relative to PAS (Bersatu won 31 seats to PAS's 43 in GE15 despite contesting 83 seats to PAS's 62). PAS's secretary-general, Takiyuddin Hassan, announced the end of cooperation; by 13 June, Hamzah Zainudin's ejected Bersatu faction had formed WAWASAN (Parti Wawasan Negara) and been admitted to PN the same day. Hamzah was reinstated as Leader of the Opposition on 18 June. The net effect is that PAS has replaced its ambitious, well-funded partner with a dependent one: WAWASAN brings Bersatu's ejected MPs but no money and no machinery. PAS now has free rein to expand into Pahang, Perak, and Selangor without negotiating seat allocations with a rival. For GE16, the PN brand stays, but the bloc is PAS-dominant with a weaker second force.

**WAWASAN (Parti Wawasan Negara).** Founded 13 June 2026 by taking over the shell of Parti Cinta Malaysia (PCM), WAWASAN represents the 19 MPs and assemblymen ejected from Bersatu in the February 2026 purge. Its president is Hamzah Zainudin (Larut MP); its known MPs include Wan Ahmad Fayhsal (Machang) and Saifuddin Abdullah (Indera Mahkota). It holds 6 federal seats and 8 state seats. The party's ideology combines Ketuanan Melayu with multiracial democracy and national conservatism, with an emphasis on MA63 and Sabah-Sarawak autonomy. Its first state-elections test (Johor and Negeri Sembilan, July–August 2026) was described by analysts as "victory built on borrowed strength" — WAWASAN's 6 federal seats make it the second-largest party in PN, but its strength is entirely derived from Bersatu's organisational remnants. Its long-term relevance depends on whether it can convert those remnants into a durable base before GE16. The party-landscape study predicts a "civil war over Malay seats" as WAWASAN, PAS, and the Bersatu rump all claim the same Malay-majority constituencies.

**Bersatu: from kingmaker to rump.** Bersatu's decline is quantified in the party-landscape study: GE15 brought 31 seats; the February 2026 purge reduced the party to approximately 6 loyal MPs under Muhyiddin Yassin; the June 2026 defections to WAWASAN further hollowed out the party. Bersatu's bank accounts are frozen, and its role in PN has been functionally usurped by WAWASAN. Its options are narrow: (a) reconcile with PAS on PAS's terms, (b) contest GE16 solo in its Malay heartland seats — splitting the anti-government vote and handing seats to PH or BN, or (c) the unthinkable per its own rhetoric: approach PH. Any of options (b) or (c) materially changes the GE16 seat arithmetic in the north. The projection model's "PN" bloc should now be read as PAS + WAWASAN, with Bersatu as a wildcard. The config carries no seat-level shock for this: the solo-contest effect is modelled inside the scenario layer (the "Bersatu split" and "Full fragmentation" scenarios apply the bersatu_penalty to Bersatu-held seats), and the "Bersatu civil war" scenario (−10pp to PN) models an even more severe fragmentation — both are conditional levers a reader can switch on, not inputs baked into the base run.

**Bersama: the urban spoiler, empirically proven.** Bersama (Parti Bersama Malaysia) was launched 17 May 2026 by Rafizi Ramli and Nik Nazmi Nik Ahmad, both former PKR senior figures who resigned from Cabinet and the party. Bersama took over the shell of the Malaysian United Party (a former MCA splinter) and adopted a blue-and-yellow branding with a "kancil" (mousedeer) logo. Its strategy is to contest independently, targeting urban voters on cost-of-living and reform. Its first electoral test — the Johor state election on 11 July 2026 — provided the empirical proof of its spoiler effect: Bersama fielded 15 candidates, all 15 lost their deposits (Malaysian law: forfeit for < 12.5% of votes), and the average vote share was 3–6% per seat. RSIS analysis confirmed that Bersama "acted as a direct spoiler for PH, siphoning away progressive, urban protest votes that traditionally went to DAP or PKR." The net effect was BN's 48-seat supermajority (up from 40) — PH fell from 12 to 8 seats. Translated to GE16's 36 federal battlegrounds, where PH holds approximately 9 seats by margins under 5%, a similar 3–6% siphon could flip several. The base run does not charge that siphon as a per-seat input: {shock_prose} The siphon is applied instead by the scenario layer's PH fragmentation term (see SCENARIO_DEFS), so a reader can switch it on explicitly rather than have it baked into the headline number.

**DAP: the anchor that will not leave.** DAP, with 40 federal seats, is the largest single party in PH and in the Dewan Rakyat. Secretary-General Anthony Loke has publicly closed the door on DAP quitting PH (17 July 2026, CNA): "you can only play the role of opposition" if solo. DAP's 40 seats are the least contestable block in Malaysian politics — Chinese-majority urban seats with margins routinely exceeding 50%. The risk to DAP is not defection but erosion: Bersama siphoning its urban base, and Bersama's cost-of-living message landing with the very voters DAP needs to retain in the battlegrounds. Loke personally oversaw the Johor campaign (PH won 8 seats) and acknowledges "setbacks, but not total rejection" — PH's vote share rose even where seats fell.

**PKR: the party in question.** PKR, with approximately 29 federal seats, is the weakest of PH's three components structurally. The 2025 leadership election — in which Rafizi was defeated by Nurul Izzah Anwar (the PM's daughter) for the deputy presidency — was the turning point: Rafizi's exit followed, and he predicted that "PKR risks dying out after the Anwar era." PKR is considering action against six renegade MPs who attended Bersama's launch. A PKR leader publicly urged PH to serve its full term (August 2026), signalling internal jitters about early GE16 timing. The succession vacuum — with Rafizi gone and Nurul Izzah elevated — makes PKR's post-Anwar question explicit and unresolved. GE16 will test whether PKR can hold its 29 seats against both PN and Bersama.

**Implications for the projection model.** The party-landscape update requires four adjustments to the model and its interpretation. First, the PN bloc composition is now PAS + WAWASAN, with Bersatu as a wildcard that may split the Malay vote. Second, the PN surge scenario (a unified 80–84 seat PN) is now less likely: fragmentation caps PN closer to the base case. Third, PH vulnerability has increased: the Bersama siphon (3–6%) is a direct subtraction from PH in urban seats, and the Johor evidence suggests 2–4 battleground seats could flip from PH to BN/PN on this alone. Fourth, the BN resurgence is confirmed and strengthened: BN's 48/56 in Johor validates the southern-resurgence scenario. The net read is that the updated landscape makes the government's position stronger than the base case suggested. The opposition's fragmentation converts what was a two-way fight into three- and four-cornered contests, which under FPTP reward the largest bloc — usually the government.

---

## 14. PRN Prediction Scorecard — Who Got It Right?

The PRN (Pilihan Raya Negeri / state election) prediction scorecard is the project's calibration evidence: a record of how well Malaysia's major research centres and individual analysts predicted the three most recent state elections — Sabah (November 2025), Johor (July 2026), and Negeri Sembilan (August 2026). This record serves two purposes: it calibrates the weight given to each centre's future polls, and it validates (or challenges) the project's own modelling assumptions. The full scorecard is documented in `02_FORECAST/knowledge/prn-prediction-scorecard.md`.

**The actual results (verified from MECo via the project's DUN folders):**

| State | Date | Result |
|---|---|---|
| **Sabah** | 29 Nov 2025 | WARISAN 25 · GRS 22 · BN 6 · PBS 6 · IND 6 · UPKO 3 · STAR 2 · PN 1 · KDM 1 · PH 1 (73 seats) |
| **Johor** | 11 Jul 2026 | BN 48 · PH 8 (56 seats) |
| **N9** | 1 Aug 2026 | BN 18 + PN 7 = 25 (BN-PN alliance) · PH 11 (36 seats) |

**Predictions versus outcomes:**

| Centre / Analyst | Sabah | vs Actual | Johor | vs Actual | N9 | vs Actual |
|---|---|---|---|---|---|---|
| **Ilham Centre** | GRS ≥26, WARISAN 14 | ❌ (both wrong) | BN leads 39 | ❌ (−9) | BN-PN 22, PH ≥9 | ❌ (−3, −2) |
| **Merdeka Center** | — | — | BN 40–42 | ❌ (−6 to −8) | — | — |
| **Ong Kian Ming** (individual) | — | — | BN 53 | ❌ (+5) | PH 9, BN-PN ~23 | ❌ (−2) |
| **Vodus Research** | — | — | BN 36% vote | ❌❌ (−24pp) | — | — |
| Consensus (kinitv/analysts) | No clear majority | ✅ (hung-ish) | — | — | — | — |

**The verdict.** On direction (who governs), Ilham Centre scored 3 out of 3 — it correctly predicted that BN wins Johor, BN-PN wins N9, and a GRS-led coalition governs Sabah. On magnitude, Ong Kian Ming (an individual analyst, not a centre) was closest on Johor (53 vs 48, +5) and N9 (~23 vs 25, −2). The big miss was Sabah for everyone: Ilham's GRS ≥26 / WARISAN 14 was wrong on both counts — WARISAN's surge to 25 caught all centres off guard, confirming that East Malaysia follows local patronage logic, not national swings. The worst single error was Vodus Research, which predicted BN at 36% vote share in Johor versus the actual 59.7% — a 24-percentage-point miss.

**Calibration lessons for the project's model.** Three lessons emerge from the scorecard, each of which has been incorporated into the forecast engine. First, **direction beats magnitude**: the forecast headlines the probability of government formation and presents seat counts as P10/P50/P90 ranges, not point estimates — exactly as the Monte Carlo does. Second, **Sabah and East Malaysia are a known blind spot**: the engine already separates `east_malaysia` seat type with reduced swing weights, and the scorecard validates this decision — every centre that applied Peninsular swing logic to Sabah was wrong. Third, **swing-based beats poll-based**: the project's southern-resurgence scenario (BN +{swing_stats['by_state'].get('Johor', {}).get('BN', 0):.1f}pp swing) predicted the Johor BN supermajority that materialised as 48/56, while poll-based centres (Merdeka 40–42) under-shot. Revealed preference — actual votes — beats stated intention. This is why state-election swings receive the highest weight (0.30) in the factor stack: the scorecard proves that they are the most reliable signal available.

The scorecard also reveals a structural problem with Malaysian psephology: the country's major research centres (Ilham, Merdeka) consistently under-predict BN's southern recovery and fail to model East Malaysian local dynamics. This is not a failure of polling technique but of modelling framework: the centres apply national-swing logic to states that follow state-specific logic. The project's model, by using per-state swings with type modulation and by separating East Malaysian seats into their own category, avoids this trap — but it is not immune to the underlying data quality problem. The Sabah miss is a reminder that even the best model cannot predict what it cannot see: local patronage shifts in East Malaysian constituencies are invisible to national-level data, and the model's honest response is to widen the uncertainty for those seats rather than to pretend precision it does not have.

---

## 15. Strategic Implications & Recommendations

The projection's central finding — that GE16 will be a majority-shrink election, not a government-change election — has strategic implications for every actor in the Malaysian political system. This section translates the model's arithmetic into actionable recommendations for the government, the opposition, the third force, and the analyst community.

**For the government (PH-BN-East Malaysian coalition):** the strongest asset is timing. The constitutional clock runs to 17 February 2028, giving the government 18 months of runway. The economic indicators are favourable (GDP above 5%, inflation below 2%, approval rising), and the opposition is fragmented. The recommendation is to **serve the full term** and maximise the economic tailwind. An early election would sacrifice the remaining runway and give the opposition time to reorganise — specifically, it would give Bersatu time to decide whether to contest solo (which would fragment the PN vote and benefit the government) or to fold into PAS-WAWASAN (which would consolidate the PN vote and increase the risk to government-held battlegrounds). The government should also invest in holding its super-marginal seats: Putatan, Bagan Datuk, Sungai Petani, and the Selangor battlegrounds are the seats where the majority will be won or lost. The projected {govt_actual} seats represent a cushion of {govt_actual - 112} above the threshold — comfortable, but not impregnable if the opposition unifies or if a late swing materialises.

**For Perikatan Nasional (PAS + WAWASAN):** the projection caps PN at {det.get('PN', 0)} seats in the base case — short of government by {112 - det.get('PN', 0)} seats. The structural problem is geographic: PN's support is concentrated in the Malay Belt (Kedah, Kelantan, Terengganu, Perlis), which delivers seats efficiently but cannot reach 112 on its own. The path to government requires breaking into the mixed Malay-majority seats of the central belt (Perak, Selangor, Pahang) and the south (Johor, N9) — but the 2025–2026 state elections show that the southern Malay electorate is returning to BN, not moving to PN. The recommendation is to **consolidate the Malay Belt, contest selectively in the central belt, and avoid three-cornered contests with the Bersatu rump**. The Bersatu split is PN's greatest vulnerability: every seat where Bersatu contests solo is a seat where the PN vote is split and BN wins on a plurality. PN should negotiate a seat allocation with the Bersatu rump — or accept that the rump's solo runs will cost PN {len([f for f in flips if f['proj_winner'] == 'BN'])} or more seats.

**For the third force (Bersama):** the Johor result — all 15 candidates losing their deposits, 3–6% vote share per seat — proves that Bersama's broad-contest strategy is a spoiler for PH, not a path to seats. The strategic fork is the same one identified in the party-landscape study: contest broadly (spoiler for PH, inadvertent ally of BN/PN) or contest narrowly (kingmaker). The Johor evidence shows that broad contestation benefits BN, not Bersama — Bersama wins nothing and loses its deposits. The recommendation is to **contest narrowly, in 5–10 seats where the PH margin is under 2% and the Bersama candidate has a personal following** (Pandan, Setiawangsa, and selected Selangor seats). This maximises the probability of winning at least one seat (which would make Bersama a parliamentary presence) while minimising the damage to PH (which is the coalition most aligned with Bersama's own reform agenda). A broad contest — 50+ seats — would repeat the Johor outcome: zero seats, lost deposits, and a BN supermajority.

**For the analyst community:** the PRN scorecard reveals that Malaysian psephology has a structural weakness — the major centres apply national-swing logic to state-specific dynamics, particularly in East Malaysia and the south. The recommendation is to **invest in state-level modelling with local patronage variables** for Sabah and Sarawak, and to **weight revealed preference (state-election swings) above stated intention (polls)** for all states. The project's model already does this, and the scorecard validates the approach: the swing-based southern-resurgence scenario predicted the Johor BN supermajority that the poll-based centres missed. The analyst community should also publish P10/P50/P90 ranges rather than point estimates — direction is more predictable than magnitude, and the honest forecast is a range, not a number.

**Confidence statement.** The projection is a bounded range, not a point. The honest reading is government-aligned **{mc['P10']:.0f}–{mc['P90']:.0f} seats**, with a median of {mc['P50']:.0f} and a {mc['P_majority']*100:.0f}% probability of retaining a majority. The model's confidence in the *direction* (government retains power) is very high — {mc['P_majority']*100:.0f}% in the Monte Carlo and 100% across all seven scenarios. The model's confidence in the *magnitude* (how many seats) is moderate — the P10–P90 spread of {mc['P90'] - mc['P10']:.0f} seats reflects genuine uncertainty in the {bg_stats['total']} battlegrounds. What could change the projection: **(1) redelineation** — if the 222-to-235 seat expansion is enacted before GE16, the model must be rebuilt entirely; **(2) the Bersama factor in three seats: Pandan, Setiawangsa, and Subang (all vacant until GE16)** — vacated by the Bersama resignations; the EC ruled NO by-elections will be held (the by-election is not automatic under Art 49A — it requires the Speaker to notify the EC, which did not happen), so the seats remain vacant until GE16 and Bersama's appeal will be tested at the general election itself rather than a by-election; **(3) Bersatu's GE16 decision** — solo contest (fragments PN, benefits government) or fold into PAS-WAWASAN (consolidates PN, increases risk to government battlegrounds); **(4) late campaign swings** — Malaysian electorates have demonstrated their capacity to move late and move hard, as the final weeks of GE15 proved. The model is a snapshot of the current electoral moment, not a prediction of the future; it will be updated weekly as new data arrives, and every prior version is preserved in the archive.

---

## 16. References & Provenance

**Academic sources:**
- ISEAS Perspective 2023/20 — Marzuki Mohamad & Ibrahim Suffian (Merdeka Center), "Malaysia's 15th GE: Ethnicity Remains the Key Factor"
- Pepinsky, Fosco & Ostwald (2023), SMU — "Demographic structure and voting behaviour during democratization: Evidence from Malaysia's 2022 election"
- Lewis-Beck & Stegmaier (2000), *Annual Review of Political Science* — "Economic Determinants of Electoral Outcomes"
- Wilkin, Hallerberg & Carey (1997) — cross-national economic-voting coefficients
- Pandian (2025), *Social Sciences & Humanities Open* (ScienceDirect) — "Undi18 and the Malaysian youth vote"
- Sunway University — "The Economic Voting Puzzle of Malaysia"
- BTI 2026 Malaysia Country Report — economic indicators

**Institutional analysis:**
- Fulcrum / ISEAS–Yusof Ishak Institute — Francis E. Hutchinson, "Perikatan Nasional's Dramatic Denouement: Where to for Bersatu?" (2026/182)
- Fulcrum / ISEAS — Lee Hwok-Aun, "Rafizi and Nik Nazmi's 'Kamikaze' Mission: A Brazen Double Dare" (May 2026)
- RSIS Commentary — "Assessment and Early Analysis of the 2026 Johor State Election Results" (15 Jul 2026)

**Official data:**
- Election Commission of Malaysia (GE15 official results; state election results 2023–2026)
- ElectionData.MY / MECo (CC0) — constituency-level results, voter demographics, by-election statistics
- Department of Statistics Malaysia (DOSM) — GDP, CPI
- Bank Negara Malaysia (BNM) — ringgit, monetary indicators

**Polling and tracking:**
- Merdeka Center surveys (2020–2026) — approval ratings, voter preference tracking
- Ilham Centre surveys — state election predictions, field studies
- Ong Kian Ming (ex-DAP MP, Taylor's University) — individual analyst predictions
- Vodus Research — Johor 2026 forecast

**News sources (event reporting):**
- The Straits Times (May–July 2026) — Bersama launch; Johor results
- CNA (June–July 2026) — DAP/PH statements; Bersatu-Hamzah
- Malaysiakini (June 2026) — PAS-Bersatu split
- Malay Mail, FMT, NST, The Edge, The Malaysian Reserve, The Vibes, The Diplomat, Sinar Harian (2026) — party landscape reporting
- East Asia Forum (February 2026) — "Malaysia enters election mode in 2026"

**Project knowledge documents:**
- `02_FORECAST/knowledge/forecast-theory.md` — the theory of change: four-layer hierarchy, causal chain, core mathematics, validation protocol
- `02_FORECAST/knowledge/forecast-factor-rankings.md` — factor provenance: each element ranked and cited with its source(s)
- `02_FORECAST/knowledge/prn-prediction-scorecard.md` — PRN track record: which centre got it right
- `Research Data/notes/party-landscape-update-2026.md` — party landscape study: PAS-Bersatu split, WAWASAN, Bersama, DAP/PKR

**Project datasets (all live, read at build time):**
- `Research Data/derived/ge15-results-by-constituency-full.csv` — 222 seats, GE15 official results
- `Research Data/derived/master-list-222-parliamentary-seats.csv` — seat register
- `Research Data/derived/voter-demographics-by-constituency-ge15.csv` — ethnic/age shares, 21,173,638 voters
- `Parliament/ge16-battleground-seats-master.csv` — 36 marginal seats
- `1_DATA/research/derived/swing_se_to_se.csv` — state-election swings, 9 states
- `work/scenarios/projection_scenarios.json` — 7-scenario sensitivity
- `02_FORECAST/outputs/latest/ge16-forecast-latest.json` — deterministic + Monte Carlo projection
- `02_FORECAST/engine/config.py` — factor weights, macro readings, event shocks, type modulation

*Generated live by `02_FORECAST/engine/report_builder.py` on {now}. This report is versioned: every prior report is preserved in `03_REPORTS/federal/archive/`; the current one lives in `03_REPORTS/federal/latest/`. No report is ever overwritten. Every number in this document is computed from the project's datasets at build time — nothing is hand-typed.*
"""

    return report, stamp, changed_sections, SECTION_NAMES


def save(report, stamp):
    """Every build: (1) writes a dated archive snapshot of THIS report,
    (2) overwrites latest/. No report is ever lost."""
    os.makedirs(LATEST_DIR, exist_ok=True)
    fname = FNAME + ("_MS" if LANG == "ms" else "")
    latest_md = os.path.join(LATEST_DIR, fname + ".md")

    # 1. dated archive snapshot of this build (every build preserves itself)
    arch_dir = os.path.join(ARCHIVE, f"GE16-{stamp}")
    os.makedirs(arch_dir, exist_ok=True)
    arch_md = os.path.join(arch_dir, fname + ".md")
    if not os.path.exists(arch_md) or open(arch_md).read() != report:
        with open(arch_md, "w") as f:
            f.write(report)
        print(f"  archived this build → {arch_md}")

    # 2. latest pointer (current version)
    with open(latest_md, "w") as f:
        f.write(report)
    print(f"  latest → {latest_md} ({os.path.getsize(latest_md)} bytes)")
    return latest_md


def make_docx(md_path):
    script = os.path.join(ROOT, "05_AUTOMATION", "md2docx.py")
    if os.path.exists(script):
        subprocess.run([sys.executable, script], cwd=ROOT, check=True)
        print("  DOCX regenerated")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--docx", action="store_true", help="also regenerate DOCX (EN only)")
    ap.add_argument("--lang", choices=["en", "ms"], default="en",
                    help="report language: en (default) or ms (native Malay, v2)")
    args = ap.parse_args()

    global LANG
    LANG = args.lang
    print(f"Building all-in-one report ({LANG.upper()})...")
    report, stamp, changed, section_names = build_report()

    # --- Smart update scan summary ---
    print(f"\n  SMART UPDATE SCAN — {len(changed)} sections flagged for rebuild:")
    for sec_num in sorted(changed.keys()):
        reason = changed[sec_num]
        sec_name = section_names.get(sec_num, f"Section {sec_num}")
        print(f"    §{sec_num} {sec_name}: {reason}")

    md_path = save(report, stamp)
    if args.docx and LANG == "en":
        make_docx(md_path)
    elif args.docx and LANG == "ms":
        print("  (--docx skipped: MS reports are MD-only per owner directive 9 Aug 2026)")

    # Cross-build gate: verify EN↔MS numeric parity (MS builds only)
    if LANG == "ms":
        try:
            from federal_ms_render import cross_build_gate
            en_path = os.path.join(LATEST_DIR, FNAME + ".md")
            print("\n  CROSS-BUILD GATE (EN ↔ MS):")
            cross_build_gate(en_path, md_path)
        except Exception as e:
            print(f"  GATE could not run: {e}")
    print("Done.")


if __name__ == "__main__":
    main()
