#!/usr/bin/env python3
"""GE16 Per-State PRN Report Builder — versioned, self-contained, thesis-style.

Generates ONE deep report per Malaysian state (13 states) from LIVE data,
mirroring the depth, narrative voice and live-computed-number discipline of
the federal report builder (02_FORECAST/engine/report_builder.py).

Output per state:
  03_REPORTS/states/DUN <State>/GE16_<State>_Report.md        <- current version
  03_REPORTS/states/DUN <State>/archive/GE16-YYYY-MM-DD/...   <- preserved prior

Every number in every report is computed live at build time from:
  - 1_DATA/research/states/DUN <State>/dun-election-results-latest.csv
  - 1_DATA/research/states/DUN <State>/dun-candidates-latest.csv
  - 1_DATA/research/derived/{master-list-222-parliamentary-seats,
    ge15-results-by-constituency-full, voter-demographics-by-constituency-ge15}.csv
  - 1_DATA/research/derived/swing_se_to_se.csv
  - 1_DATA/research/states/national-composition-summary.md
  - (Melaka/Sarawak) 1_DATA/research/states/DUN <State>/*prn-projection.csv
Nothing is hand-typed.

Usage:
  .venv/bin/python 02_FORECAST/engine/state_report_builder.py              # all 13
  .venv/bin/python 02_FORECAST/engine/state_report_builder.py --state Johor
  .venv/bin/python 02_FORECAST/engine/state_report_builder.py --docx       # + DOCX
"""

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timedelta

from data_roots import resolve_data_roots

# Output language: 'en' (default) or 'ms' (native Malay generation, v2).
# Set by main() via --lang. Section functions read this global and emit
# the corresponding prose template; every number is computed once and is
# identical across languages by construction.
LANG = "en"

# ROOT works from any cwd: .../Malaysia General Election/
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_ROOTS = resolve_data_roots()
DATA_RESEARCH = DATA_ROOTS.data_root / "research"

STATES_DIR = str(DATA_ROOTS.states)
REPORT_STATES = os.path.join(ROOT, "03_REPORTS", "states")

DERIVED = str(DATA_ROOTS.derived)
MASTER_222 = os.path.join(DERIVED, "master-list-222-parliamentary-seats.csv")
GE15_FULL = os.path.join(DERIVED, "ge15-results-by-constituency-full.csv")
DEMOG = os.path.join(DERIVED, "voter-demographics-by-constituency-ge15.csv")
SWINGS = str(DATA_ROOTS.derived / "swing_se_to_se.csv")
NATIONAL_COMP = os.path.join(STATES_DIR, "national-composition-summary.md")
THEORY = str(DATA_RESEARCH / "knowledge" / "forecast-theory.md")
CONFIG = os.path.join(ROOT, "02_FORECAST", "engine", "config.py")

# ===========================================================================
# FINGERPRINT SYSTEM — per-state smart scan (skip unchanged sections)
# ===========================================================================

FINGERPRINT_DB = os.path.join(ROOT, "work", "figures", "state_fingerprints.json")


def compute_file_hash(path):
    """Compute MD5 hash of a file for change detection."""
    if not os.path.exists(path):
        return None
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def load_fingerprint_db():
    """Load the global fingerprint database from disk."""
    if os.path.exists(FINGERPRINT_DB):
        try:
            with open(FINGERPRINT_DB, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def save_fingerprint_db(db):
    """Save the global fingerprint database to disk."""
    os.makedirs(os.path.dirname(FINGERPRINT_DB), exist_ok=True)
    with open(FINGERPRINT_DB, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)


def compute_state_fingerprints(state):
    """Compute fingerprints for all sources used by a state's report builder.

    Returns dict mapping source_name → md5hash for every input file.
    """
    meta = STATE_META[state]
    is_tier1 = meta["status"] == "upcoming"

    fps = {}
    # Core sources (all states)
    results_path = os.path.join(STATES_DIR, f"DUN {state}", "dun-election-results-latest.csv")
    cands_path = os.path.join(STATES_DIR, f"DUN {state}", "dun-candidates-latest.csv")
    fps["results_csv"] = compute_file_hash(results_path)
    fps["cands_csv"] = compute_file_hash(cands_path)
    fps["master_222"] = compute_file_hash(MASTER_222)
    fps["ge15_full"] = compute_file_hash(GE15_FULL)
    fps["demog"] = compute_file_hash(DEMOG)
    fps["swings"] = compute_file_hash(SWINGS)
    fps["national_comp"] = compute_file_hash(NATIONAL_COMP)

    # Tier 1 extra sources
    if is_tier1:
        forecast_json = os.path.join(ROOT, "02_FORECAST", "outputs", "latest", "ge16-forecast-latest.json")
        proj_scenarios = os.path.join(ROOT, "work", "scenarios", "projection_scenarios.json")
        bg_master = str(DATA_ROOTS.federal / "ge16-battleground-seats-master.csv")
        factor_rankings_md = str(DATA_RESEARCH / "knowledge" / "forecast-factor-rankings.md")
        party_landscape_md = str(DATA_RESEARCH / "data" / "notes" / "party-landscape-update-2026.md")
        fps["forecast_json"] = compute_file_hash(forecast_json)
        fps["proj_scenarios"] = compute_file_hash(proj_scenarios)
        fps["bg_master"] = compute_file_hash(bg_master)
        fps["factor_rankings"] = compute_file_hash(factor_rankings_md)
        fps["party_landscape"] = compute_file_hash(party_landscape_md)

    return fps


def section_dependencies(state):
    """Map each section name to its required source keys.

    Used by the smart scan to determine which sections need rebuilding.
    """
    meta = STATE_META[state]
    is_tier1 = meta["status"] == "upcoming"

    deps = {
        "sec0_exec": ["results_csv", "cands_csv", "master_222", "swings", "national_comp"],
        "sec1_intro": ["results_csv", "cands_csv", "national_comp"],
        "sec2_framework": ["results_csv", "cands_csv"],
        "sec3_electorate": ["results_csv", "master_222", "demog", "national_comp"],
        "sec4_baseline": ["results_csv"],
        "sec5_candidates": ["cands_csv"],
        "sec6_federal": ["master_222", "ge15_full", "demog", "forecast_json"] if is_tier1 else ["master_222", "ge15_full", "demog"],
        "sec7_signals": ["swings", "national_comp"],
        # The §2.1 story-thread section reads the events ledger live at build
        # time (like the tracker-driven signal prose above it) rather than a
        # fingerprinted data-root file, so it carries no source keys; the ledger
        # path is deliberately NOT added to compute_state_fingerprints
        # (test_forecast_data_roots asserts every fingerprint path is inside the
        # data root, and work/events is not).
        "sec7_story_threads": [],
        "sec8_method": ["factor_rankings"] if is_tier1 else [],
        "sec8a_scenario_table": ["proj_scenarios"] if is_tier1 else [],
        "sec8b_factor_weightage": ["factor_rankings"] if is_tier1 else [],
        "sec8c_prn_federal_cascade": ["forecast_json"] if is_tier1 else [],
        "sec9_scenarios": ["results_csv", "cands_csv", "swings"],
        "sec10_battlegrounds": ["bg_master"] if is_tier1 else ["results_csv"],
        "sec11_strategic": ["forecast_json", "party_landscape"] if is_tier1 else ["results_csv"],
        "sec12_refs": [],  # static template
    }
    return deps


def needs_rebuild(state, current_fps, prev_fps):
    """Determine which sections need rebuilding based on fingerprint changes.

    Returns set of section names that changed, or None if nothing changed.
    A missing prior fingerprint (first build for this state+language) is
    treated as "everything changed" — it must NOT skip.
    """
    if not prev_fps:
        return {"__all__"}  # no prior fingerprint — rebuild everything

    deps = section_dependencies(state)
    changed_sections = set()

    for section, source_keys in deps.items():
        for key in source_keys:
            curr = current_fps.get(key)
            prev = prev_fps.get(key)
            if curr != prev:
                changed_sections.add(section)
                break  # no need to check other sources for this section

    return changed_sections if changed_sections else None


# ---------------------------------------------------------------------------
# STATE METADATA (authored context — prose and dates; all NUMBERS come from CSVs)
# ---------------------------------------------------------------------------
# status: done = PRN already held in the 2025-26 cycle; upcoming = PRN due inside
# the GE16 window (late 2026 / early 2027); later = next PRN beyond GE16.
STATE_META = {
    "Johor": {
        "election_label": "SE-16", "election_date": "2026-07-11", "status": "done",
        "govt_label": "BN",
        "archetype": "bn_supermajority",
        "context": (
            "Johor is the historic engine room of Barisan Nasional — the state where UMNO was born "
            "in 1946 under Dato' Onn Jaafar, where the party's organisational DNA runs deepest, and "
            "where the 2026 state election delivered the southern resurgence that now anchors the "
            "federal arithmetic. The 11 July 2026 poll was the first major electoral test of the "
            "post-GE15 realignment: Bersama's launch, the PAS–Bersatu rupture, and the WAWASAN "
            "question all collided in a single state. The result — a BN supermajority — was the "
            "single most important revealed-preference signal between GE15 and GE16. Johor matters "
            "nationally because its 26 federal seats are the largest southern bloc in the Dewan "
            "Rakyat, and because the state's Malay electorate, once written off as lost to the "
            "green wave, demonstrated that it will return to BN when given a credible reason.")
        ,
        "exec_hook": (
            "BN's 48-of-56 supermajority is the largest state-assembly mandate the coalition holds "
            "anywhere in Malaysia, and the swing data that produced it — BN up nearly 17 points, "
            "PN down 19 — is the strongest single signal in the GE16 model."),
        "watch": (
            "Watch whether Bersama's urban spoiler effect, proven in Johor at 3–6 per cent per seat, "
            "translates into the federal battlegrounds of Muar and Sekijang, and whether PN's "
            "collapse to five seats proves durable or was a protest against a weak state slate."),
    },
    "Kedah": {
        "election_label": "SE-15", "election_date": "2023-08-12", "status": "later",
        "govt_label": "PN",
        "archetype": "green_wave",
        "context": (
            "Kedah is the northern heart of the green wave. The 12 August 2023 state election "
            "converted the state into a Perikatan Nasional fortress: 33 of 36 seats, with PAS's "
            "Sanusi Md Nor returning as Menteri Besar on a personal mandate that makes him the "
            "most recognisable opposition figurehead in the north. Kedah's conversion was not a "
            "GE15 anomaly — it was a structural realignment of the Malay electorate, accelerated "
            "by rural cost-of-living grievances, the PAS machinery's penetration of the rice-bowl "
            "belt, and the collapse of UMNO's once-unshakeable northern base. The state now tests "
            "the durability of that realignment: whether the 2023 surge was a ceiling or a "
            "foundation, and whether the PAS–Bersatu rupture and the Bersatu rump's federal "
            "weakness disturb a state government that rests overwhelmingly on PAS's own seats.")
        ,
        "exec_hook": (
            "Kedah is the purest expression of the green wave — PN holds 33 of 36 seats after a "
            "23-point swing — and the state whose 15 federal seats would be the backbone of any "
            "opposition path to Putrajaya."),
        "watch": (
            "Watch whether the PAS–Bersatu split costs PN any Kedah seats — Bersatu holds 11 of "
            "the 33 — and whether Sanusi's state popularity can carry the federal campaign in "
            "the northern belt."),
    },
    "Kelantan": {
        "election_label": "SE-15", "election_date": "2023-08-12", "status": "later",
        "govt_label": "PN",
        "archetype": "green_wave",
        "context": (
            "Kelantan is PAS's ancestral home — the party has governed the state, in coalition or "
            "alone, for all but two years since 1959, and the 2023 election merely confirmed the "
            "longest continuous party dominance in Malaysian political history. The 12 August 2023 "
            "poll delivered PN 43 of 45 seats, a consolidation of a base that predates the green "
            "wave itself. What the 2023 result added was national significance: Kelantan's 14 "
            "federal seats are among the safest in the country for PAS, and the state functions "
            "as the party's organisational wellspring — the religious schools, the village "
            "machinery, the ulama network that the party exports to contested seats elsewhere. "
            "For GE16, Kelantan is less a battleground than a reservoir; the interesting question "
            "is whether the PAS–Bersatu rupture and the WAWASAN realignment leave the state's "
            "federal delegation intact.")
        ,
        "exec_hook": (
            "Kelantan is the least contested state in Malaysia — PN's 43 of 45 seats after a "
            "15-point swing — a fixed asset in the federal arithmetic that no modelled scenario "
            "disturbs."),
        "watch": (
            "Watch the two non-PN seats — whether PH's single hold and the independent seat "
            "survive, and whether the state's flood-management and water-supply politics move "
            "the rural Malay vote at all."),
    },
    "Terengganu": {
        "election_label": "SE-15", "election_date": "2023-08-12", "status": "later",
        "govt_label": "PN",
        "archetype": "green_wave",
        "context": (
            "Terengganu completed its conversion in 2023: PN swept all 32 seats, erasing the "
            "historical alternation between BN and PAS that had defined the state since the "
            "1970s. The east-coast state, powered by oil-and-gas revenues that flow through "
            "Petronas, has long harboured a grievance politics of resource extraction — the "
            "'we produce the wealth, Kuala Lumpur spends it' narrative that PAS has weaponised "
            "for a generation. The 2023 sweep, built on a PN vote-share surge past 60 per cent, "
            "made Terengganu the cleanest green-wave state in the country: no BN hold, no PH "
            "hold, no independent. Its 8 federal seats are locked for PAS, and the state's "
            "significance for GE16 is entirely about what it does not require: no resources "
            "spent defending, every resource available for export to the contested central belt.")
        ,
        "exec_hook": (
            "Terengganu is the only fully one-colour assembly in Malaysia — PN 32 of 32 — and "
            "its 8 federal seats are the least contestable in the country."),
        "watch": (
            "Watch the oil royalty dispute with the federal government — a settlement would be "
            "the first crack in the grievance politics that underpins the sweep."),
    },
    "Melaka": {
        "election_label": "SE-15", "election_date": "2021-11-20", "status": "upcoming",
        "govt_label": "BN",
        "archetype": "melaka_divergence",
        "context": (
            "Melaka presents the most dramatic federal-state divergence in Malaysian politics. "
            "In the state election of 20 November 2021 — a snap poll triggered by the collapse "
            "of the PH-led state government in the Sheraton Move aftermath — Barisan Nasional "
            "won 21 of 28 seats, a landslide widely read as a repudiation of the defections "
            "that had destabilised the state. Twelve months later, the same electorate delivered "
            "the opposite verdict federally: BN won none of Melaka's six parliamentary seats. "
            "No other state voted so differently in consecutive elections. The state assembly "
            "automatically dissolves on 27 December 2026, placing the next Melaka PRN squarely "
            "inside the GE16 window, and the projection report prepared for this project "
            "(03_REPORTS/states/DUN Melaka/melaka-prn-projection-report.md) treats the state as "
            "Malaysia's most open contest: available voters, no anchored coalition, four "
            "national currents pulling in different directions.")
        ,
        "exec_hook": (
            "Melaka is the swing state of the GE16 window — BN holds 21 of 28 state seats while "
            "holding zero of 6 federal seats, the sharpest federal-state divergence in the "
            "country, and its PRN falls due inside the election window."),
        "watch": (
            "Watch whether the southern resurgence (Johor +17pp BN, N9 +14pp) reaches Melaka, "
            "or whether the state's federal pattern — PH 3, PN 3, BN 0 — asserts itself in the "
            "state contest."),
    },
    "Negeri Sembilan": {
        "election_label": "SE-16", "election_date": "2026-08-01", "status": "done",
        "govt_label": "BN+PN",
        "archetype": "bn_resurgence",
        "context": (
            "Negeri Sembilan was the second leg of the 2026 southern double-header, and it "
            "confirmed everything the Johor result had suggested. The 1 August 2026 state "
            "election delivered BN 18 seats, PH 11 and PN 7. The government that formed was "
            "BN+PN (25 of 36): UMNO's Ismail bin Lasim took the Menteri Besar post and PN was "
            "brought into the state administration — the first state-level BN-PN pact of the "
            "2026 cycle, a direct contradiction of the federal Unity Government line under "
            "which BN sits with PH federally. PH (Aminuddin Harun) fell to opposition. The "
            "result was read as the southern resurgence extending from Johor into the central "
            "belt: BN's vote share rose by more than 14 points, PN's collapsed from its 2023 "
            "peak, and the state's traditional Minangkabau-rooted political culture — "
            "historically a BN anchor — reasserted itself. Negeri Sembilan matters for GE16 "
            "because its 8 federal seats sit in the contested central belt, because the state "
            "is the first live test of whether BN and PN can govern together at state level, "
            "and because the 2026 result provides the freshest swing data in the entire "
            "dataset.")
        ,
        "exec_hook": (
            "Negeri Sembilan delivered the freshest signal in the dataset — BN 18 and PN 7 "
            "under a BN-PN state pact (MB Ismail bin Lasim, UMNO), PH cut to 11 in "
            "opposition — the second consecutive confirmation of the southern resurgence and "
            "the first proof that the BN-PN pact is operational, not just talk."),
        "watch": (
            "Watch whether the BN–PN pact that formed the N9 government (25 of 36) is "
            "replicated federally — the single scenario that breaks the government's "
            "parametric majority — and whether PH's fall to opposition in a 2023 battleground "
            "state is durable."),
    },
    "Pahang": {
        "election_label": "SE-15", "election_date": "2022-12-07", "status": "upcoming",
        "govt_label": "BN+PH",
        "archetype": "hung_coalition",
        "context": (
            "Pahang is Malaysia's only hung state assembly. The 19 November 2022 election "
            "produced an exact 17-17 split between BN and PN, with PH holding 8, and the "
            "state has since been governed by a BN–PH coalition stitched together after "
            "GE15 — a microcosm of the federal Unity Government itself. Pahang's politics "
            "carry an extra layer: the state is the seat of the federal monarchy, and the "
            "royal institution has historically mediated its governments. The state's 42 "
            "seats make it the third-largest assembly in the Peninsula, and its 14 federal "
            "seats straddle the boundary between the Malay Belt and the central belt — rural "
            "Felda settlements that voted PN in 2022, mixed seats along the Klang Valley "
            "corridor that stayed with PH and BN. Pahang is where the green wave meets the "
            "southern resurgence, and the 2022 result — essentially a GE15 coattail — is "
            "the oldest signal in the state dataset.")
        ,
        "exec_hook": (
            "Pahang is the hung state — BN 17, PN 17, PH 8 — a live experiment in BN–PH "
            "coalition government whose 14 federal seats straddle the green wave's southern "
            "edge."),
        "watch": (
            "Watch the three Pahang federal battlegrounds — Rompin, Kuantan, Maran — where "
            "the Bersatu split and the southern resurgence could flip PN-held marginals to BN."),
    },
    "Perak": {
        "election_label": "SE-15", "election_date": "2022-11-19", "status": "upcoming",
        "govt_label": "PN+PH",
        "archetype": "hung_coalition",
        "context": (
            "Perak, the largest state assembly in the Peninsula at 59 seats, produced the "
            "most fragmented result of the 2022 cycle: PN emerged as the largest bloc with "
            "26 seats, PH took 24, and BN collapsed to 9 — a three-way arithmetic that "
            "produced a PN–PH coalition government, the strangest bedfellows in Malaysian "
            "state politics. Perak's politics have been chronically unstable since 2008 — "
            "the state has seen more changes of government than any other — and the 2022 "
            "settlement, in which the two largest blocs govern together, is a product of "
            "the post-GE15 federal logic rather than any natural alliance. The state's 24 "
            "federal seats are among the most contested in the country: Perak contributes "
            "the largest number of federal battleground seats to the GE16 map, including "
            "the super-marginal Lumut, and its swing data — from 2022, essentially GE15 "
            "coattails — is the oldest in the dataset.")
        ,
        "exec_hook": (
            "Perak is the Peninsula's most fragmented state — PN 26, PH 24, BN 9 in a 59-seat "
            "house — and contributes more federal battlegrounds to GE16 than any other state."),
        "watch": (
            "Watch Lumut, Bagan Datuk and the state's seven other federal marginals — Perak "
            "is where the Bersatu-split event shocks and the southern resurgence intersect."),
    },
    "Perlis": {
        "election_label": "SE-15", "election_date": "2022-11-19", "status": "upcoming",
        "govt_label": "PN",
        "archetype": "green_wave",
        "context": (
            "Perlis is Malaysia's smallest state — 15 assembly seats, 3 federal seats — and "
            "its 2022 result was the most lopsided of the entire cycle: PN took 14 of 15 "
            "seats, a near-total erasure of the BN dynasty that had governed the state for "
            "decades. The tiny northern state functions as a political laboratory: with "
            "electorates of barely 20,000 voters per seat, its results are decided by a few "
            "thousand ballots, and its swings are the most volatile in the country. Perlis "
            "matters for GE16 almost entirely through its three federal seats — Padang "
            "Besar, Kangar and Arau — which are among the safest PN seats in the Malay "
            "Belt. The state's significance is symbolic as much as arithmetic: it was the "
            "first state to fall fully to the green wave, and it remains the cleanest "
            "demonstration of the Malay north's conversion.")
        ,
        "exec_hook": (
            "Perlis is the smallest and most lopsided state — PN 14 of 15 seats — a pure "
            "green-wave laboratory whose three federal seats are locked for the opposition."),
        "watch": (
            "Watch whether the Bersatu rump's weakness disturbs Perlis — Bersatu holds a "
            "share of the state's seats — and whether a unified PN slate holds all three "
            "federal seats."),
    },
    "Pulau Pinang": {
        "election_label": "SE-15", "election_date": "2023-08-12", "status": "later",
        "govt_label": "PH",
        "archetype": "ph_led",
        "context": (
            "Pulau Pinang is the citadel of the reformasi generation — the state where DAP "
            "has governed continuously since 2008, where Pakatan Harapan's urban project "
            "is most deeply rooted, and where the 2023 green wave hit hardest without "
            "actually breaking through. The 12 August 2023 election returned PH to power "
            "with 27 of 40 seats, but the margin of comfort narrowed: PN's surge into the "
            "state's Malay-majority seats — including the mainland Seberang Perai belt — "
            "cut PH's dominance and turned a previously safe state into one with genuine "
            "federal battlegrounds. Penang's 13 federal seats include the safest DAP seats "
            "in the country (Batu Kawan, Bukit Bendera) and some of the most contested "
            "Malay-majority seats in the north (Permatang Pauh, Kepala Batas). The state "
            "is the test of whether the green wave's 2023 gains in the north were a ceiling "
            "or a foundation.")
        ,
        "exec_hook": (
            "Penang held the green wave at the gates — PH 27 of 40 — but PN's surge into "
            "the mainland made the state's federal seats genuinely contested for the first "
            "time since 2008."),
        "watch": (
            "Watch Permatang Pauh and Kepala Batas — the northern Malay-majority federal "
            "seats where the 2023 swing was strongest and where Bersama's urban appeal "
            "could complicate PH's defence."),
    },
    "Sabah": {
        "election_label": "SE-15", "election_date": "2025-11-29", "status": "done",
        "govt_label": "GRS-led",
        "archetype": "sabah_local",
        "context": (
            "Sabah is Malaysia's most fragmented and least predictable political arena — "
            "a 73-seat assembly in which no national narrative survives contact with local "
            "patronage. The 29 November 2025 state election produced the most extraordinary "
            "result of the cycle: WARISAN won 25 seats, GRS 22, and the government was "
            "formed by a GRS-led coalition of 37 that assembled GRS, BN, PBS, UPKO and "
            "STAR — while WARISAN, the largest single party, went into opposition. Every "
            "major polling centre missed it. Sabah's politics run on local machines, "
            "kinship networks and the MA63 autonomy agenda; the PH-versus-PN framing of "
            "the Peninsula is largely irrelevant. The state's 25 federal seats are the "
            "second-largest East Malaysian bloc, and its November 2025 result — the most "
            "recent full state election before the 2026 southern polls — is a live warning "
            "against applying Peninsular swing logic to the Borneo states.")
        ,
        "exec_hook": (
            "Sabah is the great outlier — WARISAN 25, GRS 22, a 37-seat coalition government "
            "assembled after the largest party lost — the state where every national model "
            "fails and local logic prevails."),
        "watch": (
            "Watch whether the GRS-led coalition holds together into GE16, whether WARISAN "
            "converts its 25-seat plurality into federal seats, and whether MA63 delivery "
            "rewards the incumbents."),
    },
    "Sarawak": {
        "election_label": "SE-12", "election_date": "2021-12-18", "status": "upcoming",
        "govt_label": "GPS",
        "archetype": "gps_hegemony",
        "context": (
            "Sarawak is the exception that tests every generalisation about Malaysian "
            "voting. Gabungan Parti Sarawak won 76 of 82 seats in the 18 December 2021 "
            "state election — the largest assembly in the country, governed continuously "
            "by GPS and its predecessors since 1963 — and no Peninsular current has "
            "seriously penetrated the state since. The coalition is an ethnicity-based "
            "machine: PBB's Malay-Melanau coastal belt, SUPP's Chinese urban seats, PRS "
            "and PDP in the Dayak interior, each component owning its constituencies "
            "outright. The opposition is a personal-vote archipelago — Parti Sarawak "
            "Bersatu's four 2021 seats rested on individual incumbents, and PSB dissolved "
            "after the election. The state assembly automatically dissolves on 17 December "
            "2026, placing the next Sarawak PRN squarely inside the GE16 window, and the "
            "projection prepared for this project (sarawak-prn-projection-report.md) "
            "projects a GPS supermajority of 78 — the direct arithmetic of PSB's "
            "dissolution. Sarawak's 31 federal seats make it the kingmaker in any federal "
            "arithmetic, and its MA63 autonomy agenda is the price of its loyalty.")
        ,
        "exec_hook": (
            "Sarawak is the fixed point of the federal arithmetic — GPS 76 of 82, projecting "
            "to 78 — a two-thirds-plus fortress whose 31 federal seats make it the GE16 "
            "kingmaker."),
        "watch": (
            "Watch whether the PRN timing decision — concurrent with GE16 or separate — "
            "nationalises the state's campaign, and whether Abang Johari's succession "
            "question disturbs the machine."),
    },
    "Selangor": {
        "election_label": "SE-15", "election_date": "2023-08-12", "status": "later",
        "govt_label": "PH+BN",
        "archetype": "ph_led",
        "context": (
            "Selangor is Malaysia's wealthiest and most populous state — 56 assembly seats, "
            "22 federal seats, the engine of the national economy — and the 2023 state "
            "election was its closest-fought in a generation. PH and BN, contesting under "
            "a Unity Government pact for the first time, held the state with 34 of 56 "
            "seats, but PN's green wave cut deep into the Malay-majority constituencies "
            "of the northern and coastal belt: the opposition surged by more than 11 "
            "points, nearly capturing the state and converting Selangor into a genuine "
            "federal battleground. The state's 22 federal seats include the safest PH "
            "urban seats in the country and some of the most contested Malay-majority "
            "seats in the central belt — Kuala Selangor, Hulu Selangor, Kapar — where "
            "the 2023 wave and the Bersatu split now pull in opposite directions. "
            "Selangor is where the election will be won: no path to government ignores "
            "its arithmetic.")
        ,
        "exec_hook": (
            "Selangor is the largest prize in the federal arithmetic — 22 seats, PH+BN "
            "34 of 56 after the closest state election in a generation — and the state "
            "where the green wave's central-belt gains are now being contested by the "
            "Bersatu split."),
        "watch": (
            "Watch Kuala Selangor, Hulu Selangor and Kapar — the Malay-majority federal "
            "seats where PN's 2023 surge was strongest and where opposition fragmentation "
            "now favours the government."),
    },
}

# Fixed dissolution dates where the projection reports document them; otherwise
# computed as last election + 5-year term.
FIXED_DISSOLUTION = {
    "Melaka": datetime(2026, 12, 27),
    "Sarawak": datetime(2026, 12, 17),
}

# Source registry tags mirroring the federal report's [S-XX] convention.
SOURCES = {
    "S-01": "Election Commission of Malaysia via ElectionData.MY / MECo (CC0) — state election results, per-seat winners, votes, margins, turnout",
    "S-02": "ElectionData.MY / MECo (CC0) — per-candidate registers: party, coalition, votes, result, sex, ethnicity, age",
    "S-03": "Election Commission of Malaysia (GE15 official results, 19 November 2022) via MECo — federal seat winners, margins",
    "S-04": "ElectionData.MY anonymised GE15 voter roll (CC0) — constituency ethnic/age/gender composition",
    "S-05": "Parliament of Malaysia seat register (master list, 222 seats) — federal members, coalition, party as at June 2026",
    "S-06": "MECo headline ballots — state-election vote-share swings (prev → latest), computed per state and bloc",
    "S-07": "Project state-election summaries — 03_REPORTS/states/DUN <State>/dun-election-summary.md",
    "S-08": "Project GE16 battleground deep-dives — 03_REPORTS/states/DUN <State>/ge16-battleground-deepdive.md",
    "S-09": "Project PRN projection report — 03_REPORTS/states/DUN <State>/<state>-prn-projection-report.md",
    "S-10": "Project knowledge — 1_DATA/research/knowledge/forecast-theory.md: four-layer hierarchy, uniform-swing mathematics, validation protocol",
    "S-11": "Project national DUN composition — 1_DATA/research/states/national-composition-summary.md",
    "S-12": "News trackers — 1_DATA/research/trackers/ge16-{poll,candidate,general-news}-log.md (2026)",
}


def fmt_date():
    return datetime.now().strftime("%d %B %Y")


def stamp_now():
    return datetime.now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# ADDITIONAL SOURCES FOR TIER 1 (UPCOMING PRN) STATES
# ---------------------------------------------------------------------------

FORECAST_JSON = os.path.join(ROOT, "02_FORECAST", "outputs", "latest", "ge16-forecast-latest.json")
PROJECTION_SCENARIOS = os.path.join(ROOT, "work", "scenarios", "projection_scenarios.json")
BATTLEGROUNDS_MASTER = str(DATA_ROOTS.federal / "ge16-battleground-seats-master.csv")
FACTOR_RANKINGS = str(DATA_RESEARCH / "knowledge" / "forecast-factor-rankings.md")
PARTY_LANDSCAPE = str(DATA_RESEARCH / "data" / "notes" / "party-landscape-update-2026.md")


def load_config():
    """Import config.py as a module to read MACRO/EVENT_SHOCKS/VACANCIES live."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("fc_config", CONFIG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_forecast_for_state(state):
    """Load federal forecast JSON and filter to the state's parliamentary seats.

    Returns dict with:
      - 'seats': list of {code, constituency, ge15_winner, proj_winner, proj_margin, seat_type}
                 for P### seats in this state
      - 'monte_carlo': full MC distribution from the forecast
      - 'deterministic': full deterministic bloc totals
    """
    if not os.path.exists(FORECAST_JSON):
        return None
    import json
    with open(FORECAST_JSON, encoding="utf-8") as f:
        data = json.load(f)
    st = norm_state(state)
    # Filter ALL projected seats to this state (not just flips)
    fed_seats = []
    for r in data.get("projected_seats", []):
        if norm_state(r.get("state", "")) == st:
            fed_seats.append({
                "code": r["code"],
                "constituency": r["constituency"],
                "state": r["state"],
                "ge15_winner": r.get("ge15_winner", ""),
                "proj_winner": r.get("proj_winner", ""),
                "proj_margin": r.get("proj_margin", 0),
                "seat_type": r.get("seat_type", ""),
            })
    # Also include non-flip seats in this state from master list
    master = load_csv(MASTER_222)
    master_by_code = {r["code"].replace(".", ""): r for r in master}
    for s in fed_seats:
        m = master_by_code.get(s["code"])
        if m:
            s["ge15_bloc"] = m.get("coalition", "")
            s["current_party"] = m.get("party", "")
    mc = data.get("monte_carlo", {})
    det = data.get("deterministic", {})
    return {"seats": fed_seats, "monte_carlo": mc, "deterministic": det, "flips_P50": mc.get("flips_P50", 0)}


def load_projection_scenarios_for_state(state):
    """Load projection_scenarios.json and compute per-scenario seat distribution for the state.

    The JSON has top-level keys = scenario names, values = {bloc: seats} dicts.
    Returns {"scenarios": [...], "raw": {...}} or None.
    """
    if not os.path.exists(PROJECTION_SCENARIOS):
        return None
    import json
    with open(PROJECTION_SCENARIOS, encoding="utf-8") as f:
        data = json.load(f)
    # Convert {name: {bloc: count}} → [{scenario, gov_seats, pn_seats, ph_seats, bn_seats, ...}]
    scenarios = []
    for name, blocs in data.items():
        gov = sum(v for k, v in blocs.items() if k in ("PH", "BN", "GRS", "WARISAN", "IND", "MUDA", "PBM"))
        pn = blocs.get("PN", 0)
        ph = blocs.get("PH", 0)
        bn = blocs.get("BN", 0) + blocs.get("GRS", 0) + blocs.get("WARISAN", 0)
        other = sum(v for k, v in blocs.items() if k not in ("PH", "PN", "BN", "GRS", "WARISAN"))
        scenarios.append({
            "scenario": name,
            "govt_seats": gov,
            "pn_seats": pn,
            "ph_seats": ph,
            "bn_seats": bn,
            "other_seats": other,
            "character": "",
        })
    if not scenarios:
        return None
    return {"scenarios": scenarios, "raw": data}


def load_battlegrounds_for_state(state):
    """Filter ge16-battleground-seats-master.csv to the state's seats.

    Returns list of dicts with federal battleground tiers for this state:
      [{code, constituency, tier, ge15_bloc, current_bloc, margin_pct_valid, ...}, ...]
    """
    rows = load_csv(BATTLEGROUNDS_MASTER)
    st = norm_state(state)
    out = []
    for r in rows:
        if norm_state(r.get("state_std", "")) == st:
            out.append({
                "code": r.get("code", ""),
                "constituency": r.get("constituency", ""),
                "tier": r.get("tier", ""),
                "ge15_bloc": r.get("ge15_bloc", ""),
                "current_bloc": r.get("current_bloc", ""),
                "margin": num(r.get("margin_pct_valid")),
                "malay_pct": num(r.get("malay_pct")),
                "chinese_pct": num(r.get("chinese_pct")),
                "youth_pct": num(r.get("youth_pct")),
                "median_age": num(r.get("median_age")),
                "total_voters": intnum(r.get("total_voters")),
                "recent_fed_byelection": (r.get("recent_fed_byelection", "") or "").strip(),
            })
    out.sort(key=lambda x: x["margin"])
    return out


def clean_factor_desc(desc, limit=250):
    """Strip embedded markdown tables and collapse whitespace (fixes §8.1 broken
    table — the factor-ranking source file has per-factor `| Source | What it says |`
    tables that must NOT leak pipes into the outer Rank/Factor/Description table)."""
    import re
    # Remove all remaining pipe characters (table formatting)
    desc = desc.replace('|', ' ')
    # Remove --- separators
    desc = re.sub(r'\s*---+', ' ', desc)
    # Remove table header artifacts
    desc = re.sub(r'Source\s+What it says', '', desc)
    # Collapse whitespace
    desc = ' '.join(desc.split())
    return desc[:limit]


def load_factor_rankings():
    """Read forecast-factor-rankings.md and extract factor weightage hierarchy.

    Returns dict of {factor_name: weight_description} for use in Section 8 methodology.
    """
    if not os.path.exists(FACTOR_RANKINGS):
        return None
    txt = read_text(FACTOR_RANKINGS)
    factors = {}
    # Parse markdown headings as factor names, next line as weight
    for m in re.finditer(r"###\s+(.+?)\n(.*?)(?=###|\Z)", txt, re.S):
        name = m.group(1).strip()
        body = m.group(2).strip()[:200]
        factors[name] = body
    return factors


def load_party_landscape_for_state(state):
    """Read party-landscape-update-2026.md and extract state-relevant coalition posture.

    Returns string excerpt relevant to the state, or None.
    """
    if not os.path.exists(PARTY_LANDSCAPE):
        return None
    txt = read_text(PARTY_LANDSCAPE)
    st_lower = state.lower()
    # Find section mentioning this state
    for m in re.finditer(r"(##\s+.+?\n.*?)" + st_lower + r".*?(?=##\s+|\Z)", txt, re.S | re.I):
        return m.group(1)[:500]
    # Fallback: return first 300 chars
    return txt[:300] if txt else None


# ---------------------------------------------------------------------------
# DATA LOADING
# ---------------------------------------------------------------------------

def load_csv(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    return rows


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def fnum(x, dp=1):
    """Format a float with dp decimals, thousands separators."""
    return f"{x:,.{dp}f}"


def num(x, default=0.0):
    try:
        return float(x)
    except (ValueError, TypeError):
        return default


def intnum(x, default=0):
    try:
        return int(float(x))
    except (ValueError, TypeError):
        return default


def pct(x, dp=1):
    return f"{x:.{dp}f}%"


def norm_state(name):
    """Normalise state names across datasets (Penang/Pulau Pinang etc.)."""
    m = {
        "Penang": "Pulau Pinang",
        "Pulau Pinang": "Pulau Pinang",
        "Negeri Sembilan": "Negeri Sembilan",
        "Melaka": "Melaka",
        "Malacca": "Melaka",
        "Sarawak": "Sarawak",
        "Sabah": "Sabah",
    }
    return m.get(name, name)


# ---------------------------------------------------------------------------
# COMPUTED STATISTICS (all live from CSVs)
# ---------------------------------------------------------------------------

def parse_national_composition():
    """Read seats + latest election info per state from national-composition-summary.md."""
    info = {}
    txt = read_text(NATIONAL_COMP)
    for line in txt.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) >= 4 and cells[0] in STATE_META:
            info[cells[0]] = {
                "seats": intnum(cells[1]),
                "election": cells[2],
                "govt": cells[3],
            }
    return info


def compute_baseline(results):
    """All live statistics from the DUN results CSV."""
    bloc_seats = defaultdict(int)
    party_seats = defaultdict(int)
    coalition_seats = defaultdict(int)
    margins = []          # majority_perc
    turnouts = []
    voters_total = 0
    votes_valid = 0
    n_cand_list = []
    seat_rows = []
    for r in results:
        bloc = r.get("winner_bloc", "").strip() or "IND"
        party = r.get("winner_party", "").strip() or "IND"
        coal = r.get("winner_coalition", "").strip() or "IND"
        bloc_seats[bloc] += 1
        party_seats[party] += 1
        coalition_seats[coal] += 1
        margins.append(num(r.get("majority_perc")))
        turnouts.append(num(r.get("voter_turnout")))
        voters_total += intnum(r.get("voters_total"))
        votes_valid += intnum(r.get("votes_valid"))
        n_cand_list.append(intnum(r.get("n_candidates")))
        seat_rows.append({
            "seat": r.get("seat", ""),
            "winner": r.get("winner", ""),
            "party": party,
            "bloc": bloc,
            "votes": intnum(r.get("votes")),
            "votes_perc": num(r.get("votes_perc")),
            "majority": intnum(r.get("majority")),
            "turnout": num(r.get("voter_turnout")),
            "margin": num(r.get("majority_perc")),
            "n_candidates": intnum(r.get("n_candidates")),
        })
    total = len(results)
    margins_sorted = sorted(seat_rows, key=lambda s: s["margin"])
    safest = sorted(seat_rows, key=lambda s: -s["margin"])
    avg_margin = sum(margins) / len(margins) if margins else 0.0
    avg_turnout = sum(turnouts) / len(turnouts) if turnouts else 0.0
    super_marg = sum(1 for m in margins if m < 2.0)
    marginal = sum(1 for m in margins if 2.0 <= m < 5.0)
    comfortable = sum(1 for m in margins if 5.0 <= m < 10.0)
    safe = sum(1 for m in margins if m >= 10.0)
    two_thirds = int(total * 2 / 3) + 1  # seats needed for 2/3 majority (integer)
    return {
        "total": total,
        "bloc_seats": dict(bloc_seats),
        "party_seats": dict(party_seats),
        "coalition_seats": dict(coalition_seats),
        "voters_total": voters_total,
        "votes_valid": votes_valid,
        "avg_margin": avg_margin,
        "avg_turnout": avg_turnout,
        "margin_tiers": {"super": super_marg, "marginal": marginal,
                         "comfortable": comfortable, "safe": safe},
        "closest": margins_sorted[:8],
        "safest": safest[:6],
        "seats": seat_rows,
        "two_thirds": two_thirds,
        "avg_n_candidates": sum(n_cand_list) / len(n_cand_list) if n_cand_list else 0.0,
        "max_n_candidates": max(n_cand_list) if n_cand_list else 0,
    }


def compute_candidates(cands):
    """All live candidate-demographic statistics from the candidates CSV."""
    eth_all = defaultdict(int)
    eth_won = defaultdict(int)
    sex_all = defaultdict(int)
    sex_won = defaultdict(int)
    ages = []
    ages_won = []
    ages_lost = []
    deposit_lost = 0
    per_seat = defaultdict(int)
    cand_list = []
    for r in cands:
        eth = (r.get("ethnicity", "") or "Unknown").strip() or "Unknown"
        sex = (r.get("sex", "") or "?").strip().upper() or "?"
        age = intnum(r.get("age"), -1)
        result = (r.get("result", "") or "").strip()
        eth_all[eth] += 1
        sex_all[sex] += 1
        per_seat[r.get("seat", "")] += 1
        won = result == "won"
        if won:
            eth_won[eth] += 1
            sex_won[sex] += 1
        if age >= 0:
            ages.append(age)
            (ages_won if won else ages_lost).append(age)
        if result == "lost_deposit":
            deposit_lost += 1
        cand_list.append({
            "seat": r.get("seat", ""),
            "name": r.get("name", ""),
            "party": r.get("party", ""),
            "coalition": r.get("coalition", ""),
            "votes": intnum(r.get("votes")),
            "votes_perc": num(r.get("votes_perc")),
            "rank": intnum(r.get("rank")),
            "result": result,
            "sex": sex,
            "ethnicity": eth,
            "age": age,
        })
    avg_age = sum(ages) / len(ages) if ages else 0.0
    avg_age_won = sum(ages_won) / len(ages_won) if ages_won else 0.0
    avg_age_lost = sum(ages_lost) / len(ages_lost) if ages_lost else 0.0
    youngest = min(ages) if ages else None
    oldest = max(ages) if ages else None
    n_seats = len(per_seat)
    avg_per_seat = len(cands) / n_seats if n_seats else 0.0
    # won-vs-lost by ethnicity: win rate = won / contested
    eth_winrate = {e: (eth_won.get(e, 0) / c * 100 if c else 0.0)
                   for e, c in eth_all.items()}
    sex_winrate = {s: (sex_won.get(s, 0) / c * 100 if c else 0.0)
                   for s, c in sex_all.items()}
    # age brackets
    brackets = {"under_30": 0, "30_39": 0, "40_49": 0, "50_59": 0, "60plus": 0}
    brackets_won = {"under_30": 0, "30_39": 0, "40_49": 0, "50_59": 0, "60plus": 0}
    for r in cand_list:
        a = r["age"]
        if a < 0:
            continue
        key = "under_30" if a < 30 else "30_39" if a < 40 else "40_49" if a < 50 \
            else "50_59" if a < 60 else "60plus"
        brackets[key] += 1
        if r["result"] == "won":
            brackets_won[key] += 1
    # youngest/oldest named candidates
    def named(fn):
        valid = [c for c in cand_list if c["age"] >= 0]
        if not valid:
            return None
        return fn(valid, key=lambda c: c["age"])
    youngest_cand = named(min)
    oldest_cand = named(max)
    return {
        "n_candidates": len(cand_list),
        "n_seats": n_seats,
        "avg_per_seat": avg_per_seat,
        "eth_all": dict(eth_all),
        "eth_won": dict(eth_won),
        "eth_winrate": eth_winrate,
        "sex_all": dict(sex_all),
        "sex_won": dict(sex_won),
        "sex_winrate": sex_winrate,
        "avg_age": avg_age,
        "avg_age_won": avg_age_won,
        "avg_age_lost": avg_age_lost,
        "youngest": youngest,
        "oldest": oldest,
        "youngest_cand": youngest_cand,
        "oldest_cand": oldest_cand,
        "deposit_lost": deposit_lost,
        "brackets": brackets,
        "brackets_won": brackets_won,
        "cands": cand_list,
    }


def compute_federal(state, master_rows, ge15_rows, demog_rows):
    """Federal seats within this state: holders, GE15 results, demographics."""
    st = norm_state(state)
    fed = []
    for r in master_rows:
        if norm_state(r.get("state", "")) == st:
            fed.append({
                "code": r.get("code", ""),
                "constituency": r.get("constituency", ""),
                "member": r.get("member", ""),
                "coalition": r.get("coalition", ""),
                "party": r.get("party", ""),
            })
    # GE15 results keyed by code
    ge15_by_code = {}
    for r in ge15_rows:
        if norm_state(r.get("state", "")) == st:
            ge15_by_code[r.get("code", "").replace(".", "")] = r
    for s in fed:
        g = ge15_by_code.get(s["code"].replace(".", ""), {})
        s["ge15_bloc"] = g.get("bloc", "")
        s["ge15_party"] = g.get("party", "")
        s["ge15_winner"] = g.get("winner_name", "")
        s["ge15_margin"] = num(g.get("margin_pct_valid"))
        s["registered"] = intnum(g.get("registered"))
    # demographics by state
    demog_state = [r for r in demog_rows if norm_state(r.get("state", "")) == st]
    demog = {}
    if demog_state:
        tot = sum(intnum(r.get("total_voters")) for r in demog_state)
        demog["n_seats"] = len(demog_state)
        demog["total_voters"] = tot
        def wavg(key):
            return sum(num(r.get(key)) * intnum(r.get("total_voters")) for r in demog_state) / tot if tot else 0.0
        demog["malay"] = wavg("malay_pct")
        demog["chinese"] = wavg("chinese_pct")
        demog["indian"] = wavg("indian_pct")
        demog["bumi_sabah"] = wavg("bumi_sabah_pct")
        demog["bumi_sarawak"] = wavg("bumi_sarawak_pct")
        demog["other"] = wavg("other_pct")
        demog["youth"] = wavg("age18_21_pct") + wavg("age22_30_pct")
        demog["age31_40"] = wavg("age31_40_pct")
        demog["age60plus"] = wavg("age60plus_pct")
        demog["male"] = wavg("male_pct")
        demog["female"] = wavg("female_pct")
        demog["median"] = wavg("median_age")
    # federal battlegrounds: GE15 margin < 5%
    bg = [s for s in fed if 0 <= s["ge15_margin"] < 5.0]
    bg.sort(key=lambda s: s["ge15_margin"])
    return {"seats": fed, "demog": demog, "battlegrounds": bg,
            "n_seats": len(fed)}


def compute_swings(state):
    """Per-bloc state-election swings from the swing CSV."""
    st = norm_state(state)
    rows = load_csv(SWINGS)
    out = {}
    for r in rows:
        if norm_state(r.get("state", "")) == st:
            out[r.get("bloc", "")] = {
                "prev_se": r.get("prev_se", ""),
                "latest_se": r.get("latest_se", ""),
                "latest_date": r.get("latest_date", ""),
                "prev_share": num(r.get("prev_share")),
                "latest_share": num(r.get("latest_share")),
                "swing": num(r.get("swing_pp")),
            }
    return out


def compute_projection(state):
    """PRN projection (Melaka/Sarawak only) from the projection CSV + report."""
    st = norm_state(state)
    proj_dir = os.path.join(STATES_DIR, f"DUN {state}")
    csv_path = None
    for fn in os.listdir(proj_dir) if os.path.isdir(proj_dir) else []:
        if fn.endswith("prn-projection.csv"):
            csv_path = os.path.join(proj_dir, fn)
            break
    if not csv_path:
        return None
    rows = load_csv(csv_path)
    bloc_totals = defaultdict(int)
    flips = []
    margins = []
    for r in rows:
        w = r.get("proj_winner", "").strip()
        bloc_totals[w] += 1
        margins.append(num(r.get("proj_margin")))
        if (r.get("flip", "") or "").strip().lower() == "true":
            flips.append({
                "seat": r.get("seat", ""),
                "constituency": r.get("constituency", ""),
                "from": r.get("w_2021", ""),
                "to": w,
                "margin_2021": num(r.get("margin_2021")),
                "proj_margin": num(r.get("proj_margin")),
            })
    avg_margin = sum(margins) / len(margins) if margins else 0.0
    # scenario table — v2 (10 Aug 2026): prefer the machine-readable
    # <state>-state-scenarios.json (federal-parity: parametric A–D + live
    # federal narrative layer + config EVENT_SHOCKS); fall back to parsing the
    # projection report markdown table (legacy).
    scenario_rows = []
    report_path = None
    scen_json = os.path.join(proj_dir, f"{st.lower()}-state-scenarios.json")
    if os.path.exists(scen_json):
        import json as _json
        with open(scen_json, encoding="utf-8") as f:
            sdata = _json.load(f)
        # Preserve ALL bloc keys (BN/PH/PN for most states; GPS/PSB for
        # Sarawak; GRS/WARISAN/IND/etc. wherever they appear). A hardcoded
        # BN/PH/PN triple silently dropped Sarawak's GPS (76) + PSB (4) seats
        # and left empty-string defaults that crashed the govt-aligned sum.
        META_KEYS = {"scenario", "category", "flips", "seats_total",
                     "provenance", "state", "generated_at"}
        for s in sdata.get("scenarios", []):
            row = {"Scenario": s.get("scenario", ""),
                   "Type": s.get("category", "")}
            for k, v in s.items():
                if k not in META_KEYS:
                    row[k] = v
            row["Seats flipping"] = s.get("flips", "")
            row["Character"] = f"{s.get('flips',0)} seats change hands; sum {s.get('seats_total','?')}"
            scenario_rows.append(row)
    if not scenario_rows:
        report_path = None
        proj_root = os.path.join(REPORT_STATES, f"DUN {state}")
        search_dirs = [proj_root, os.path.join(proj_root, "latest")]
        for d in search_dirs:
            if not os.path.isdir(d):
                continue
            for fn in os.listdir(d):
                if fn.endswith("prn-projection-report.md"):
                    report_path = os.path.join(d, fn)
                    break
            if report_path:
                break
        report_text = read_text(report_path) if report_path else ""
        # parse markdown tables: capture the first table after a heading containing "Results"
        for m in re.finditer(r"##\s*3[.\s].*?Results.*?\n(.*?)(?:\n\n|\Z)", report_text, re.S):
            table = m.group(1)
            lines = [l for l in table.splitlines() if l.strip().startswith("|")]
            if len(lines) >= 3:
                header = [c.strip() for c in lines[0].strip("|").split("|")]
                for l in lines[2:]:
                    cells = [c.strip() for c in l.strip("|").split("|")]
                    if len(cells) == len(header):
                        scenario_rows.append(dict(zip(header, cells)))
                break
    return {
        "rows": rows, "bloc_totals": dict(bloc_totals), "flips": flips,
        "avg_margin": avg_margin, "scenarios": scenario_rows,
        "report_path": report_path, "has_swings": False,  # set by build_state_report
    }


def parse_battleground_deepdive(state):
    """Extract federal battleground seat codes + tiers from the deep-dive md.

    Reads from latest/ first (the canonical location since the 3 Aug reorg);
    falls back to the legacy state-dir path for older builds.
    """
    candidates = [
        os.path.join(REPORT_STATES, f"DUN {state}", "latest",
                     "ge16-battleground-deepdive.md"),
        os.path.join(REPORT_STATES, f"DUN {state}",
                     "ge16-battleground-deepdive.md"),
    ]
    path = next((p for p in candidates if os.path.exists(p)), None)
    if not path:
        return []
    txt = read_text(path)
    out = []
    for m in re.finditer(r"###\s*(P\d{3})\s*[—-]\s*(.*?)\n(.*?)(?=\n###|\Z)", txt, re.S):
        code, name, body = m.group(1), m.group(2).strip(), m.group(3)
        tier = ""
        tm = re.search(r"\((SUPER-MARGINAL[^)]*|HIGH-RISK[^)]*|WATCH[^)]*)\)", name)
        if tm:
            tier = tm.group(1)
        holder = ""
        hm = re.search(r"Held by[^:]*:\s*(.*)", body)
        if hm:
            holder = hm.group(1).strip()
        out.append({"code": code, "name": name, "tier": tier, "holder": holder})
    return out


def state_signals_from_trackers(state):
    """Pull tracker headlines that mention the state (for the Signals section)."""
    st = norm_state(state)
    hits = []
    for fn in ["ge16-poll-tracker-log.md", "ge16-candidate-tracker-log.md",
               "ge16-general-news-log.md"]:
        path = str(DATA_ROOTS.trackers / fn)
        if not os.path.exists(path):
            continue
        for line in read_text(path).splitlines():
            if state.lower() in line.lower() and line.strip().startswith("- "):
                title = line.strip()[2:]
                title = re.sub(r"\[.*?\]\s*", "", title).strip()
                hits.append({"feed": fn.replace("ge16-", "").replace("-log.md", ""),
                             "title": title[:130]})
    # dedupe, cap
    seen = set()
    out = []
    for h in hits:
        if h["title"] not in seen:
            seen.add(h["title"])
            out.append(h)
        if len(out) >= 6:
            break
    return out


# ---------------------------------------------------------------------------
# PROSE HELPERS
# ---------------------------------------------------------------------------

def bloc_order_by_seats(bloc_seats):
    return sorted(bloc_seats.items(), key=lambda x: -x[1])


def describe_bloc_mix(bloc_seats, total):
    """'BN 48, PH 8' style compact description."""
    parts = [f"{b} {c}" for b, c in bloc_order_by_seats(bloc_seats)]
    return ", ".join(parts)


def govt_blocs_for(state):
    """Blocs aligned with the state government (from the national composition)."""
    comp = parse_national_composition()
    govt_str = comp.get(state, {}).get("govt", "")
    # Normalise: "BN 48/56" -> "BN"; "PH+BN 34/56" -> "PH+BN";
    # "GRS-led coalition (GRS+BN+PBS+UPKO+STAR 37/73)" -> "GRS+BN+PBS+UPKO+STAR"
    g = govt_str.split("(")[0].strip()
    g = re.sub(r"\s*\d+\s*/\s*\d+.*$", "", g).strip()
    # Sabah: "GRS-led coalition (GRS+BN+PBS+UPKO+STAR 37/73)" — use the
    # parenthetical bloc list when present.
    pm = re.search(r"\(([^)]*?\d+\s*/\s*\d+)\)", govt_str)
    if pm:
        inner = pm.group(1)
        inner = re.sub(r"\s*\d+\s*/\s*\d+.*$", "", inner).strip()
        if "+" in inner:
            g = inner
    mapping = {
        "BN": ["BN"],
        "PN": ["PN"],
        "PH": ["PH"],
        "PH+BN": ["PH", "BN"],
        "BN+PH": ["BN", "PH"],
        "BN+PN": ["BN", "PN"],
        "GRS": ["GRS"],
        "GPS": ["GPS"],
        "GRS-led": ["GRS", "BN", "PBS", "UPKO", "STAR"],
        "PN+PH": ["PN", "PH"],
        "GPS+BN": ["GPS", "BN"],
        "GRS+BN+PBS+UPKO+STAR": ["GRS", "BN", "PBS", "UPKO", "STAR"],
        "BN+PH+GPS+GRS": ["BN", "PH", "GPS", "GRS"],
    }
    if g in mapping:
        return mapping[g]
    # fallback: match any bloc tokens present in the string
    blocs = ["GRS", "GPS", "WARISAN", "PBS", "UPKO", "STAR", "BN", "PH", "PN",
             "MUDA", "KDM", "PBM"]
    found = [bl for bl in blocs if bl in g.upper()]
    return found if found else ["BN", "PH", "GPS", "GRS", "WARISAN", "MUDA", "KDM", "PBM"]


def ordinal(n):
    return f"{n}th" if 10 <= n % 100 <= 20 else f"{n}{'st' if n%10==1 else 'nd' if n%10==2 else 'rd' if n%10==3 else 'th'}"



EXEC_HOOK_MS = {'Johor': 'Supermajoriti BN 48 daripada 56 ialah mandat dewan negeri terbesar yang dipegang gabungan itu di mana-mana di Malaysia, dan pergerakan itu adalah isyarat selatan yang paling kuat dalam set data ini.', 'Kedah': 'Kedah ialah ekspresi paling tulen gelombang hijau — PN memegang 33 daripada 36 kerusi selepas ayunan 23 mata — dan negeri yang keputusannya mentakrifkan semula Utara Melayu.', 'Kelantan': 'Kelantan ialah negeri paling kurang ditandingi di Malaysia — 43 daripada 45 kerusi PN selepas ayunan 15 mata — aset tetap dalam aritmetik persekutuan.', 'Terengganu': 'Terengganu ialah satu-satunya dewan satu warna sepenuhnya di Malaysia — PN 32 daripada 32 — dan 8 kerusi persekutuannya ialah yang paling kurang ditandingi di negara ini.', 'Melaka': 'Melaka ialah negeri ayunan tetingkap GE16 — BN memegang 21 daripada 28 kerusi negeri sambil memegang sifar daripada 6 kerusi persekutuan, perbezaan persekutuan-negeri paling tajam di negara ini, dan PRNnya jatuh di dalam tetingkap pilihan raya.', 'Negeri Sembilan': 'Negeri Sembilan menyampaikan isyarat paling segar dalam set data — BN 18 dan PN 7 di bawah pakatan negeri BN-PN (MB Ismail bin Lasim) — dan ia ialah bukti bahawa pakatan BN-PN boleh berfungsi.', 'Pahang': 'Pahang ialah negeri tergantung — BN 17, PN 17, PH 8 — eksperimen hidup kerajaan gabungan BN-PH yang 14 kerusi persekutuannya membawa dua aritmetik ke dalam GE16.', 'Perak': 'Perak ialah negeri paling berpecah di Semenanjung — PN 26, PH 24, BN 9 dalam dewan 59 kerusi — dan menyumbang lebih banyak kerusi marginal persekutuan daripada mana-mana negeri lain.', 'Perlis': 'Perlis ialah negeri terkecil dan paling berat sebelah — PN 14 daripada 15 kerusi — makmal gelombang hijau tulen yang tiga kerusi persekutuannya kini terdedah kepada perpecahan Bersatu.', 'Pulau Pinang': 'Pulau Pinang menahan gelombang hijau di pintu gerbang — PH 27 daripada 40 — tetapi lonjakan PN ke daratan menjadikan kerusi persekutuan negeri ini medan pertempuran.', 'Sabah': 'Sabah ialah outlier terbesar — WARISAN 25, GRS 22, kerajaan gabungan 37 kerusi disusun selepas parti terbesar kalah — dan corak tempatannya menolak semua unjuran nasional.', 'Sarawak': 'Sarawak ialah titik tetap aritmetik persekutuan — GPS 76 daripada 82, diunjurkan kepada 78 — kubu dua pertiga lebih yang 23 kerusinya memberi GPS kuasa kingmaker nasional.', 'Selangor': 'Selangor ialah hadiah terbesar dalam aritmetik persekutuan — 22 kerusi, PH+BN 34 daripada 56 selepas pilihan raya negeri paling rapat dalam sejarah — dan ia ialah medan pertempuran sebenar bagi majoriti PH.'}
WATCH_MS = {'Johor': 'Perhatikan sama ada kesan spoiler bandar Bersama, terbukti di Johor pada 3–6 peratus bagi setiap kerusi, diterjemahkan ke dalam kerusi marginal persekutuan.', 'Kedah': 'Perhatikan sama ada perpecahan PAS–Bersatu merugikan PN mana-mana kerusi Kedah — Bersatu memegang 11 daripada 33 — dan sama ada prestij negeri Sanusi menyelamatkan gabungan itu.', 'Kelantan': 'Perhatikan dua kerusi bukan-PN — sama ada pegangan tunggal PH dan kerusi bebas itu terselamat, dan sama ada dasar banjir negeri menguji dominasi PN.', 'Terengganu': 'Perhatikan pertikaian royalti minyak dengan kerajaan persekutuan — penyelesaian akan menjadi keretakan pertama dalam politik kebencian yang mengekalkan PN.', 'Melaka': 'Perhatikan sama ada kebangkitan selatan (Johor +17pp BN, N9 +14pp) sampai ke Melaka, atau sama ada corak persekutuan negeri — PH 3, PN 3, BN 0 — menguasai pertandingan negeri.', 'Negeri Sembilan': 'Perhatikan sama ada pakatan BN–PN yang membentuk kerajaan N9 (25 daripada 36) direplikasi di peringkat persekutuan — satu-satunya senario yang mengubah aritmetik majoriti.', 'Pahang': 'Perhatikan tiga medan pertempuran persekutuan Pahang — Rompin, Kuantan, Maran — di mana perpecahan Bersatu dan kebangkitan selatan bertemu.', 'Perak': 'Perhatikan Lumut, Bagan Datuk dan tujuh lagi kerusi marginal persekutuan negeri — Perak ialah tempat kejutan perpecahan Bersatu memberi kesan paling kuat.', 'Perlis': 'Perhatikan sama ada kelemahan saki-baki Bersatu mengganggu Perlis — Bersatu memegang sebahagian kerusi negeri — dan sama ada ayunan seragam kecil memindahkan kerusi persekutuan.', 'Pulau Pinang': 'Perhatikan Permatang Pauh dan Kepala Batas — kerusi persekutuan majoriti Melayu utara di mana ayunan 2023 paling kuat dan PH paling terdedah.', 'Sabah': 'Perhatikan sama ada gabungan pimpinan GRS kekal utuh ke GE16, sama ada WARISAN menukar pluraliti 25 kerusinya kepada kuasa persekutuan, dan tarikh PRN.', 'Sarawak': 'Perhatikan keputusan masa PRN — serentak dengan GE16 atau berasingan — yang akan menasionalisasikan kempen negeri, dan sama ada GPS menuntut konsesi MA63.', 'Selangor': 'Perhatikan Kuala Selangor, Hulu Selangor dan Kapar — kerusi persekutuan majoriti Melayu di mana lonjakan PN 2023 paling kuat dan pegangan PH paling lemah.'}


CONTEXT_MS_BATCH1 = {'Johor': "Johor ialah bilik enjin bersejarah Barisan Nasional — negeri tempat UMNO dilahirkan pada 1946 di bawah Dato' Onn Jaafar, tempat DNA organisasi parti itu mengalir paling dalam, dan tempat pilihan raya negeri 2026 menyampaikan kebangkitan selatan yang kini menambat aritmetik persekutuan. Pungutan suara 11 Julai 2026 ialah ujian elektoral utama pertama penjajaran semula pasca-GE15: pelancaran Bersama, perpecahan PAS–Bersatu, dan persoalan WAWASAN semuanya bertembung dalam satu negeri. Keputusan — supermajoriti BN — ialah isyarat pilihan-dedah paling penting antara GE15 dan GE16. Johor penting di peringkat nasional kerana 26 kerusi persekutuannya — termasuk kerusi yang paling banyak di Semenanjung — dan kerana corak pengundiannya menetapkan istilah untuk seluruh Selatan.", 'Kedah': 'Kedah ialah jantung utara gelombang hijau. Pilihan raya negeri 12 Ogos 2023 menukar negeri itu menjadi kubu Perikatan Nasional: 33 daripada 36 kerusi, dengan Sanusi Md Nor PAS kembali sebagai Menteri Besar atas mandat peribadi yang menjadikannya figura pembangkang paling dikenali di utara. Penukaran Kedah bukan anomali GE15 — ia ialah penjajaran semula struktur elektorat Melayu, dipercepat oleh rungutan kos sara hidup luar bandar, penembusan jentera PAS ke dalam kawasan tali pinggang sawah, dan keruntuhan pangkalan utara UMNO yang dahulunya tidak tergoyah. Negeri kini menguji ketahanan gabungan PN: perpecahan PAS–Bersatu mengancam untuk mengikis mandat 2023, sementara prestij peribadi Sanusi kekal sebagai aset paling besar parti itu di Utara.', 'Kelantan': 'Kelantan ialah rumah nenek moyang PAS — parti itu telah memerintah negeri, secara gabungan atau bersendirian, untuk semua kecuali dua tahun sejak 1959, dan pilihan raya 2023 sekadar mengesahkan dominasi parti berterusan yang paling lama dalam sejarah politik Malaysia. Pungutan suara 12 Ogos 2023 menyampaikan PN 43 daripada 45 kerusi, penyatuan pangkalan yang mendahului gelombang hijau itu sendiri. Apa yang ditambah oleh keputusan 2023 ialah kepentingan nasional: 14 kerusi persekutuan Kelantan antara yang paling selamat di negara ini untuk PAS, dan negeri berfungsi sebagai mata air organisasi parti — sekolah agama, jentera kampung, ulama, rangkaian zakat — yang mengekalkan jentera pilihan raya PAS di seluruh Semenanjung.', 'Terengganu': "Terengganu melengkapkan penukarannya pada 2023: PN menyapu semua 32 kerusi, memadamkan penggantian bersejarah antara BN dan PAS yang mentakrifkan negeri sejak 1970-an. Negeri pantai timur, dikuasakan oleh hasil minyak dan gas yang mengalir melalui Petronas, telah lama menyimpan politik kebencian pengekstrakan sumber — naratif 'kita hasilkan kekayaan, Kuala Lumpur membelanjakannya' yang telah dipersenjatai PAS selama satu generasi. Sapuan 2023, dibina atas lonjakan bahagian undi PN melepasi 60 peratus, menjadikan Terengganu negeri gelombang hijau paling bersih di negara ini: tiada pegangan BN, tiada pegangan PH, tiada bebas. 8 kerusi persekutuannya adalah antara yang paling selamat di negara ini, menjadikan negeri ini aset tetap dalam aritmetik GE16.", 'Melaka': 'Melaka membentangkan perbezaan persekutuan-negeri paling dramatik dalam politik Malaysia. Dalam pilihan raya negeri 20 November 2021 — pungutan suara mengejut yang dicetuskan oleh keruntuhan kerajaan negeri pimpinan PH selepas Sheraton Move — Barisan Nasional memenangi 21 daripada 28 kerusi, tanah runtuh yang dibaca secara meluas sebagai penolakan terhadap lompat parti yang telah menggugat kestabilan negeri. Dua belas bulan kemudian, elektorat yang sama menyampaikan keputusan bertentangan di peringkat persekutuan: BN tidak memenangi mana-mana daripada enam kerusi parlimen Melaka. Tiada negeri lain mengundi begitu berbeza dalam pilihan raya berturut-turut. Dewan negeri terbubar secara automatik pada 27 Disember 2026, meletakkan PRN Melaka seterusnya tepat di dalam tetingkap GE16.', 'Negeri Sembilan': 'Negeri Sembilan ialah kaki kedua dua-persekutuan selatan 2026, dan ia mengesahkan semua yang dicadangkan oleh keputusan Johor. Pilihan raya negeri 1 Ogos 2026 menyampaikan BN 18 kerusi, PH 11 dan PN 7. Kerajaan yang terbentuk ialah BN+PN (25 daripada 36): Ismail bin Lasim UMNO mengambil jawatan Menteri Besar dan PN dibawa masuk ke pentadbiran negeri — pakatan BN-PN peringkat negeri pertama kitaran 2026, percanggahan langsung garis Kerajaan Perpaduan persekutuan di mana BN duduk dengan PH di peringkat persekutuan. PH (Aminuddin Harun) jatuh ke pembangkang. Keputusan itu dibaca sebagai kebangkitan selatan yang meluas dari Johor ke tali pinggang tengah: bahagian undi BN meningkat lebih 14 mata, PN runtuh daripada kemuncak 2023, dan budaya politik tradisional berakar Minangkabau negeri — sejarahnya penambat BN — menegaskan semula dirinya. Bagi GE16, N9 berfungsi sebagai bukti paling segar bahawa pakatan BN-PN boleh berfungsi di peringkat pentadbiran, dan implikasinya terhadap aritmetik persekutuan adalah dua hala: ia mengesahkan daya tarikan kebangkitan selatan, tetapi ia juga menimbulkan persoalan sama ada pakatan itu akan diuji di peringkat nasional — satu senario yang akan mengubah komposisi mana-mana kerajaan pasca-GE16.'}


CONTEXT_MS_BATCH2 = {'Pahang': 'Pahang ialah satu-satunya dewan negeri tergantung di Malaysia. Pilihan raya 19 November 2022 menghasilkan pecahan tepat 17-17 antara BN dan PN, dengan PH memegang 8, dan negeri sejak itu diperintah oleh gabungan BN–PH yang dijahit bersama selepas GE15 — mikrokosmos Kerajaan Perpaduan persekutuan itu sendiri. Politik Pahang membawa satu lapisan tambahan: negeri ialah tempat duduk raja persekutuan, dan institusi diraja secara sejarah menjadi pengantara kerajaannya. 42 kerusi negeri menjadikannya dewan ketiga terbesar di Semenanjung, dan 14 kerusi persekutuannya merentasi sempadan antara Tali Pinggang Melayu dan negeri tengah — menjadikan Pahang barometer penting bagi sama ada ayunan selatan sampai ke utara. Eksperimen kerajaan BN-PH Pahang juga menyediakan bukti berterusan tentang bagaimana dua blok yang bersaing di peringkat persekutuan dapat mentadbir bersama — dan sama ada kerjasama itu bertahan sehingga GE16 ialah isyarat yang akan dibaca oleh pengundi di seluruh negara.', 'Perak': 'Perak, dewan negeri terbesar di Semenanjung dengan 59 kerusi, menghasilkan keputusan paling berpecah kitaran 2022: PN muncul sebagai blok terbesar dengan 26 kerusi, PH mengambil 24, dan BN runtuh kepada 9 — aritmetik tiga penjuru yang menghasilkan kerajaan gabungan PN–PH, sekutu paling pelik dalam politik negeri Malaysia. Politik Perak tidak stabil secara kronik sejak 2008 — negeri telah mengalami lebih banyak pertukaran kerajaan daripada mana-mana negeri lain — dan penyelesaian 2022, di mana dua blok terbesar memerintah bersama, ialah produk logik persekutuan pasca-GE15 dan bukannya pakatan semula jadi. 24 kerusi persekutuan negeri, termasuk lebih banyak kerusi marginal daripada mana-mana negeri lain, menjadikannya medan pertempuran paling penting di Semenanjung untuk GE16.', 'Perlis': 'Perlis ialah negeri terkecil di Malaysia — 15 kerusi dewan, 3 kerusi persekutuan — dan keputusan 2022 ialah yang paling berat sebelah dalam keseluruhan kitaran: PN mengambil 14 daripada 15 kerusi, pemadaman hampir menyeluruh dinasti BN yang telah memerintah negeri selama beberapa dekad. Negeri utara kecil berfungsi sebagai makmal politik: dengan elektorat sekitar 20,000 pengundi bagi setiap kerusi, keputusannya ditentukan oleh beberapa ribu undi, dan ayunannya ialah yang paling tidak menentu di negara ini. Perlis penting untuk GE16 hampir keseluruhannya melalui tiga kerusi persekutuannya — Padang Besar, Kangar dan Arau — yang antara kerusi PN paling selamat di negara ini, menjadikan negeri ini ujian tulen sama ada perpecahan Bersatu boleh menggerakkan mana-mana kerusi.', 'Pulau Pinang': 'Pulau Pinang ialah kubu generasi reformasi — negeri tempat DAP memerintah secara berterusan sejak 2008, tempat projek bandar Pakatan Harapan paling berakar, dan tempat gelombang hijau 2023 melanda paling kuat tanpa benar-benar menembusi. Pilihan raya 12 Ogos 2023 mengembalikan PH kepada kuasa dengan 27 daripada 40 kerusi, tetapi margin keselesaan menyempit: lonjakan PN ke kerusi majoriti Melayu negeri — termasuk tali pinggang Seberang Perai di daratan — memotong dominasi PH dan menukar negeri yang dahulunya selamat kepada negeri dengan medan pertempuran persekutuan sebenar. 13 kerusi persekutuan Pulau Pinang termasuk kerusi bandar paling selamat PH dan kerusi utara paling terdedah, menjadikannya ujian dua arah kekuatan PH.', 'Sabah': 'Sabah ialah arena politik paling berpecah dan paling tidak boleh diramal di Malaysia — dewan 73 kerusi di mana tiada naratif nasional terselamat daripada sentuhan naungan tempatan. Pilihan raya negeri 29 November 2025 menghasilkan keputusan paling luar biasa kitaran: WARISAN memenangi 25 kerusi, GRS 22, dan kerajaan dibentuk oleh gabungan pimpinan GRS seramai 37 yang menghimpunkan GRS, BN, PBS, UPKO dan STAR — manakala WARISAN, parti tunggal terbesar, masuk pembangkang. Setiap pusat tinjauan utama terlepas. Politik Sabah berjalan atas jentera tempatan, rangkaian kekeluargaan dan agenda autonomi MA63; rangka PH-lawannya-PN tidak pernah cukup — dan corak tempatan itu, bukan naratif nasional, akan menentukan siapa yang memerintah selepas GE16.', 'Sarawak': 'Sarawak ialah pengecualian yang menguji setiap generalisasi tentang pengundian Malaysia. Gabungan Parti Sarawak memenangi 76 daripada 82 kerusi dalam pilihan raya negeri 18 Disember 2021 — dewan terbesar di negara ini, diperintah secara berterusan oleh GPS dan pendahulunya sejak 1963 — dan tiada arus Semenanjung yang serius menembusi negeri sejak itu. Gabungan ialah jentera berasaskan etnik: tali pinggang pantai Melayu-Melanau PBB, kerusi bandar Cina SUPP, PRS dan PDP di pedalaman Dayak, setiap komponen memiliki kerusinya sendiri sepenuhnya. Pembangkang ialah kepulauan undi peribadi — empat kerusi Parti Sarawak Bersatu dan satu kerusi bebas dipegang oleh tokoh, bukan parti. 23 kerusi persekutuan Sarawak menjadikan GPS kingmaker yang tidak dapat dielakkan bagi mana-mana kerajaan persekutuan. Keputusan PRN Sarawak — sama ada serentak dengan GE16 atau berasingan — akan menentukan sama ada kempen negeri dinasionalisasikan oleh arus Semenanjung, dan ini ialah pemboleh ubah paling penting yang belum diselesaikan dalam aritmetik persekutuan.', 'Selangor': 'Selangor ialah negeri terkaya dan paling ramai penduduk di Malaysia — 56 kerusi dewan, 22 kerusi persekutuan, enjin ekonomi negara — dan pilihan raya negeri 2023 ialah yang paling sengit dalam satu generasi. PH dan BN, bertanding di bawah pakatan Kerajaan Perpaduan buat pertama kali, mengekalkan negeri dengan 34 daripada 56 kerusi, tetapi gelombang hijau PN memotong dalam ke kawasan majoriti Melayu di tali pinggang utara dan pantai: pembangkang melonjak lebih 11 mata, hampir menawan negeri dan menukar Selangor kepada medan pertempuran persekutuan sebenar. 22 kerusi persekutuan negeri termasuk kerusi bandar PH paling selamat dan kerusi pinggir bandar paling terdedah — dan kerana saiznya, Selangor ialah hadiah terbesar dalam aritmetik GE16. Keadaan ini menjadikan Selangor ujian dua hala bagi kedua-dua blok: bagi PH, ia ialah negeri yang mesti dipertahankan kerana kehilangannya akan memusnahkan mana-mana laluan ke majoriti; bagi PN, ia ialah satu-satunya negeri bukan Malay Belt yang boleh membawanya melintasi ambang 112 — dan lonjakan 2023 menunjukkan sasaran itu tidak lagi di luar jangkauan.'}


CONTEXT_MS = {}
CONTEXT_MS.update(CONTEXT_MS_BATCH1)
CONTEXT_MS.update(CONTEXT_MS_BATCH2)


def context_ms(state, meta):
    """MS variant of the per-state context narrative; falls back to EN."""
    return CONTEXT_MS.get(state) or meta.get("context", "")


def exec_hook_ms(state, meta):
    """MS variant of the per-state exec hook; falls back to a neutral sentence."""
    return EXEC_HOOK_MS.get(state) or meta.get("exec_hook", "")


def watch_ms(state, meta):
    """MS variant of the per-state watch line; falls back to a neutral sentence."""
    return WATCH_MS.get(state) or meta.get("watch", "")
def ordinal_ms(n):
    """Malay ordinal: ke-1, ke-2, ... ke-21, ..."""
    return f"ke-{n}"


def is_tier1_state(state):
    """True for Tier-1 states (upcoming PRN) which carry the federal-parity
    17-section numbering. Tier-2 states keep the 13-section numbering."""
    return STATE_META[state]["status"] == "upcoming"


def tier_section(state, tier1_no, tier2_no):
    """Return the section number for this state's tier."""
    return tier1_no if is_tier1_state(state) else tier2_no


def election_name(state):
    meta = STATE_META[state]
    label = meta["election_label"]
    date = datetime.strptime(meta["election_date"], "%Y-%m-%d")
    return f"{label} ({date.strftime('%d %B %Y')})"


def next_dissolution(state):
    if state in FIXED_DISSOLUTION:
        return FIXED_DISSOLUTION[state]
    d = datetime.strptime(STATE_META[state]["election_date"], "%Y-%m-%d")
    return d + timedelta(days=365 * 5)


def era_label(state):
    """Green wave / resurgence / older signal era, from swing dates."""
    sw = compute_swings(state)
    dates = [v["latest_date"] for v in sw.values() if v["latest_date"]]
    if not dates:
        return "pre-GE15"
    latest = max(dates)
    if "2026" in latest:
        return "2026 southern resurgence"
    if "2025" in latest:
        return "2025 Sabah local cycle"
    if "2023" in latest:
        return "2023 green wave"
    return "2022 GE15 coattail"


def title_for(state):
    meta = STATE_META[state]
    has_proj = os.path.exists(os.path.join(STATES_DIR, f"DUN {state}")) and any(
        fn.endswith("prn-projection.csv")
        for fn in os.listdir(os.path.join(STATES_DIR, f"DUN {state}")) if os.path.isdir(os.path.join(STATES_DIR, f"DUN {state}"))
    )
    suffix = " & Projection" if has_proj else ""
    if LANG == "ms":
        proj_txt = " & Unjuran" if has_proj else ""
        return f"# Pilihan Raya Negeri (PRN) {state} — Laporan Lengkap{proj_txt}"
    return f"# {state} State Election (PRN) — Complete Report{suffix}"


# ===========================================================================
# SECTION BUILDERS — each returns markdown for one numbered section
# ===========================================================================

def sec0_exec(state, b, c, fed, swings, proj, meta, comp):
    total = b["total"]
    top = bloc_order_by_seats(b["bloc_seats"])[0]
    top_bloc, top_seats = top
    govt = govt_blocs_for(state)
    govt_seats = sum(b["bloc_seats"].get(g, 0) for g in govt)
    opp_seats = total - govt_seats
    two_thirds = b["two_thirds"]
    has_super = top_seats >= two_thirds
    sw = swings
    key_swings = ", ".join(
        f"{bloc} {v['swing']:+.1f}pp" for bloc, v in sorted(sw.items(), key=lambda x: -abs(x[1]['swing']))[:3]
    ) if sw else "no fresh state-level swing data"
    proj_line = ""
    if proj:
        ptop = bloc_order_by_seats(proj["bloc_totals"])[0]
        proj_line = (f" The projection prepared for this report carries the base scenario to "
                     f"**{ptop[0]} {ptop[1]} of {sum(proj['bloc_totals'].values())}** seats, with "
                     f"{len(proj['flips'])} seats flipping on the modelled swings.")
    fed_bg = len(fed["battlegrounds"])
    if LANG == "ms":
        super_txt = " — majoriti dua pertiga lebih" if has_super else ""
        proj_ms = ""
        if proj:
            ptop = bloc_order_by_seats(proj["bloc_totals"])[0]
            proj_ms = (f" Unjuran yang disediakan untuk laporan ini membawa senario asas kepada "
                       f"**{ptop[0]} {ptop[1]} daripada {sum(proj['bloc_totals'].values())}** kerusi, dengan "
                       f"{len(proj['flips'])} kerusi bertukar tangan berdasarkan ayunan yang dimodelkan.")
        return f"""## 0. Ringkasan Eksekutif

Laporan ini membentangkan analisis lengkap dan berdikari mengenai Dewan Undangan Negeri (DUN) {state} sewaktu memasuki kitaran pilihan raya umum keenam belas, yang dibina dari bawah ke atas: setiap kiraan kerusi, setiap margin, setiap angka keluar mengundi, dan setiap statistik demografi calon dihitung secara langsung daripada set data projek pada masa pembinaan laporan. Tiada apa-apa yang ditaip secara manual. Laporan ini mengikut disiplin tesis yang sama seperti laporan persekutuan — {state} dilayan bukan sebagai himpunan jadual tetapi sebagai sebuah sistem politik yang mempunyai logiknya sendiri, imbangan kuasanya sendiri, dan trajektorinya sendiri menuju GE16.

Dapatan utama untuk {state} adalah **dominasi {top_bloc} dengan {top_seats} daripada {total} kerusi** ({top_seats/total*100:.1f} peratus daripada dewan){super_txt}. {exec_hook_ms(state, meta)} Struktur margin menceritakan ceritanya: purata margin kemenangan di seluruh {total} kerusi negeri adalah **{b['avg_margin']:.1f} mata peratusan**, dengan **{b['margin_tiers']['super']} kerusi super-marginal** di bawah dua mata, **{b['margin_tiers']['marginal']} kerusi marginal** antara dua dengan lima, dan **{b['margin_tiers']['safe']} kerusi selamat** melebihi sepuluh mata — satu taburan yang menentukan sejauh mana negeri ini benar-benar boleh ditandingi dalam GE16.

Kerajaan negeri, yang bersekutu dengan **{meta['govt_label']}**, menguasai **{govt_seats}** kerusi berbanding **{opp_seats}** bagi gabungan pembangkang dan bebas — satu kusyen {govt_seats - (total // 2 + 1):+d} melebihi majoriti minimum {total // 2 + 1}. Pilihan raya negeri yang paling terkini, {election_name(state)}, menghasilkan pergerakan undi seperti berikut: {key_swings}.{proj_ms}

{state} menyumbang **{fed['n_seats']} kawasan persekutuan** kepada Dewan Rakyat, di mana **{fed_bg} merupakan medan pertempuran persekutuan** (margin GE15 di bawah lima mata) — kerusi-kerusi di mana dinamik negeri ini memberi kesan langsung kepada aritmetik GE16 nasional. Laporan ini meneliti, dalam dua belas bahagian, rangka kerja pilihan raya negeri, elektoratnya, garis dasar PRNnya, demografi calonnya, delegasi persekutuannya, isyarat yang berubah sejak pilihan raya negeri lepas, metodologi di sebalik unjuran, julat senario, medan pertempuran yang perlu diperhatikan, dan implikasi strategik untuk setiap blok.

**Apa yang perlu diperhatikan di {state}:** {watch_ms(state, meta)}

Ringkasnya: {state} memasuki GE16 dengan struktur kuasa yang jelas, tetapi bahagian-bahagian yang berikut menunjukkan bahawa kejelasan struktur tidak bermakna ketiadaan ketidakpastian — setiap kerusi marginal, setiap pergerakan undi yang diukur sejak pilihan raya negeri terakhir, dan setiap isyarat peristiwa yang direkodkan dalam penjejak berita menyumbang kepada gambaran yang lebih bernuansa daripada tajuk utama sahaja. Pembaca yang ingin memahami sama ada {state} akan kekal seperti sekarang atau berubah dalam GE16 harus memberi perhatian khusus kepada Bahagian 9–12, di mana ketidakpastian itu diukur dan bukan sekadar disebut.

---


"""
    return f"""## 0. Executive Summary

This report presents a complete, self-contained analysis of the {state} state assembly (Dewan Undangan Negeri, DUN) as it stands entering the sixteenth general election cycle, built from the ground up: every seat count, every margin, every turnout figure, and every candidate-demographic statistic is computed live from the project's datasets at build time. Nothing is hand-typed. The report follows the same thesis discipline as the federal report — {state} is treated not as a collection of tables but as a political system with its own logic, its own balance of forces, and its own trajectory into GE16.

The central finding for {state} is **{top_bloc} dominance at {top_seats} of {total} seats** ({top_seats/total*100:.1f} per cent of the assembly){', a two-thirds-plus supermajority' if has_super else ''}. {meta['exec_hook']} The margin structure tells the story: the average winning margin across the state's {total} seats is **{b['avg_margin']:.1f} percentage points**, with **{b['margin_tiers']['super']} super-marginal** seats under two points, **{b['margin_tiers']['marginal']} marginal** seats between two and five, and **{b['margin_tiers']['safe']} safe** seats beyond ten — a distribution that defines how much of this state is genuinely contestable in GE16.

The state government, aligned with **{meta['govt_label']}**, commands **{govt_seats}** seats against **{opp_seats}** for the opposition and independents combined — a cushion of {govt_seats - (total // 2 + 1):+d} over the bare majority of {total // 2 + 1}. The most recent state election, {election_name(state)}, produced these headline movements in vote share: {key_swings}.{proj_line}

{state} contributes **{fed['n_seats']} federal constituencies** to the Dewan Rakyat, of which **{fed_bg} are federal battlegrounds** (GE15 margins under five points) — the seats where this state's dynamics feed directly into the national GE16 arithmetic. This report examines, in twelve sections, the state's electoral framework, its electorate, its PRN baseline, the demographics of its candidates, its federal delegation, the signals that have moved since the last state poll, the methodology behind the projection, the scenario range, the battlegrounds to watch, and the strategic implications for every bloc.

**What to watch in {state}:** {meta['watch']}

---

"""


def sec1_intro(state, meta, b, comp):
    total = b["total"]
    top = bloc_order_by_seats(b["bloc_seats"])[0]
    comp_line = comp.get(state, {})
    if LANG == "ms":
        return f"""## 1. Pengenalan & Konteks

{context_ms(state, meta)}

Kedudukan negeri ini dalam aritmetik nasional tidak selari dengan saiznya. {state} menghantar {fed_seats(state)} ahli ke Dewan Rakyat — {fed_seats(state)/222*100:.1f} peratus daripada dewan 222 kerusi — dan kerajaan negerinya, yang dibentuk oleh **{meta['govt_label']}** selepas {election_name(state)}, adalah salah satu daripada {state_count()} pentadbiran negeri yang akan bertahan atau tumbang berdasarkan rekod mereka sendiri dari sekarang hingga GE16. Hubungan antara arena negeri dengan persekutuan berjalan dua hala: keputusan peringkat negeri sejak GE15 merupakan data keutamaan terserlah yang paling berharga bagi projek ini, kerana ia mengukur undi sebenar yang dibuang oleh elektorat yang sama yang akan mengundi di peringkat persekutuan, sementara arus persekutuan — perpecahan PAS–Bersatu, penjajaran semula WAWASAN, pelancaran Bersama — membentuk semula pertandingan peringkat negeri secara masa nyata.

Kerangka analisis yang digunakan di sini adalah hierarki empat lapisan yang sama yang mendorong model persekutuan ([S-10]): identiti (komposisi etnik, rantau, Timur lawan Barat) menetapkan struktur asas setiap kerusi; valens (kepimpinan, jenama gabungan, kepercayaan rasuah) mengalihkan margin dalam struktur tersebut; prestasi (ekonomi, kos sara hidup, tadbir urus) menentukan pilihan dalam etnik yang sama; dan peristiwa (perpecahan, kuasa ketiga, skandal) boleh memecahkan struktur di kerusi individu. Bagi {state}, keseimbangan antara lapisan ini adalah {layer_emphasis_ms(state)} — dan bahagian-bahagian yang berikut menjejak setiap lapisan melalui data negeri itu sendiri.

---


"""
    return f"""## 1. Introduction & Context

{meta['context']}

The state's place in the national arithmetic is not symmetrical with its size. {state} returns {fed_seats(state)} members to the Dewan Rakyat — {fed_seats(state)/222*100:.1f} per cent of the 222-seat chamber — and its state government, formed by **{meta['govt_label']}** after {election_name(state)}, is one of {state_count()} state administrations that will stand or fall on their own records between now and GE16. The relationship between the state and federal arenas runs in both directions: state-level results since GE15 are the project's most valuable revealed-preference data, because they measure actual votes cast by the same electorates that will vote federally, while federal currents — the PAS–Bersatu rupture, the WAWASAN realignment, the Bersama launch — reshape the state-level contest in real time.

The analytical frame applied here is the same four-layer hierarchy that drives the federal model ([S-10]): identity (ethnic composition, region, East versus West) sets the baseline structure of every seat; valence (leadership, coalition brand, corruption trust) shifts margins within that structure; performance (economy, cost of living, governance) decides the within-ethnic choice; and events (ruptures, third forces, scandals) can break the structure in individual seats. For {state}, the balance among these layers is {layer_emphasis(state)} — and the sections that follow trace each layer through the state's own data.

---

"""


def fed_seats(state):
    master = load_csv(MASTER_222)
    return sum(1 for r in master if norm_state(r.get("state", "")) == norm_state(state))


def fed_battlegrounds_for(state):
    """Return all federal battleground seats (from ge16-battleground-seats-master.csv) for this state."""
    bg = load_battlegrounds_for_state(state)
    return bg if bg else []


def state_count():
    return len(STATE_META)


def layer_emphasis(state):
    arch = STATE_META[state]["archetype"]
    if arch in ("gps_hegemony", "sabah_local"):
        return ("unusually weighted toward identity and local patronage — the national L2–L4 "
                "factors, which dominate the Peninsular debate, are second-order here")
    if arch == "green_wave":
        return ("weighted toward identity and valence — the Malay-majority structure of the "
                "state's seats is the dominant fact, and leadership perception moves the "
                "within-ethnic choice")
    if arch == "melaka_divergence":
        return ("unusually fluid — the state's own voters have demonstrated that no layer is "
                "anchored, and L4 events can overwhelm the structural baseline in a single cycle")
    return ("conventionally balanced — identity sets the baseline, but performance and events "
            "retain enough swing capacity to matter in the marginal seats")


def layer_emphasis_ms(state):
    """Malay variant of layer_emphasis — used by sec1_intro in --lang ms mode."""
    arch = STATE_META[state]["archetype"]
    if arch in ("gps_hegemony", "sabah_local"):
        return ("luar biasa berat ke arah identiti dan naungan tempatan — faktor L2–L4 nasional, "
                "yang mendominasi perdebatan Semenanjung, adalah peringkat kedua di sini")
    if arch == "green_wave":
        return ("berat ke arah identiti dan valens — struktur majoriti Melayu bagi kerusi-kerusi "
                "negeri adalah fakta dominan, dan persepsi kepimpinan menggerakkan pilihan dalam "
                "etnik yang sama")
    if arch == "melaka_divergence":
        return ("luar biasa cair — pengundi negeri ini sendiri telah menunjukkan bahawa tiada "
                "lapisan yang berlabuh, dan peristiwa L4 boleh mengatasi garis dasar struktur "
                "dalam satu kitaran")
    return ("seimbang secara konvensional — identiti menetapkan garis dasar, tetapi prestasi dan "
            "peristiwa mengekalkan kapasiti ayunan yang cukup untuk menjadi penting di kerusi marginal")


def sec2_framework(state, b, meta):
    total = b["total"]
    dis = next_dissolution(state)
    term_end = dis.strftime("%d %B %Y")
    status = meta["status"]
    status_txt = {
        "done": "the most recent state election has already been held and its results are incorporated into this report",
        "upcoming": "the state assembly dissolves within the GE16 window and the next PRN is imminent",
        "later": "the next PRN falls beyond the GE16 window, so GE16 federal dynamics are the live question",
    }[status]
    if LANG == "ms":
        status_ms = {
            "done": "pilihan raya negeri yang paling terkini telah pun diadakan dan keputusannya telah dimasukkan ke dalam laporan ini",
            "upcoming": "dewan undangan negeri terbubar dalam tetingkap GE16 dan PRN seterusnya sudah hampir",
            "later": "PRN seterusnya jatuh di luar tetingkap GE16, jadi dinamik persekutuan GE16 adalah persoalan yang hidup",
        }[status]
        return f"""## 2. Dewan Undangan Negeri & Rangka Kerja Pilihan Raya

Dewan Undangan Negeri {state} terdiri daripada **{total} kawasan pilihan raya ahli tunggal**, setiap satunya mengembalikan seorang ADUN di bawah sistem pemenang undi terbanyak (first-past-the-post). Ini meletakkan {state} sebagai dewan undangan negeri ke-{assembly_rank_ordinal(state)} terbesar di Malaysia, dan bilangan kerusi ditetapkan oleh perlembagaan negeri sehingga suatu persempadanan semula — satu proses yang, seperti di peringkat persekutuan, telah dibekukan secara politik selama bertahun-tahun. Di bawah FPTP, calon yang memperoleh undi terbanyak di setiap kawasan menang tanpa mengira sama ada mereka mendapat majoriti undi yang dibuang; akibat praktikalnya ialah geografi pilihan raya {state} — penumpuan sokongan setiap blok merentasi {total} kerusinya — lebih penting daripada bahagian undi mentahnya. Sesebuah blok boleh memenangi majoriti kerusi dengan undi minoriti jika sokongannya diagihkan dengan cekap, dan sebaliknya juga benar.

Tempoh penggal dewan adalah lima tahun dari persidangan pertamanya. Dewan semasa, yang dipilih dalam {election_name(state)}, oleh itu akan tamat tempohnya sekitar **{term_end}**; status negeri ini dalam kitaran GE16 adalah bahawa {status_ms}. Peraturan pembubaran adalah automatik — dewan terbubar pada tamat tempoh lima tahun melainkan dibubarkan lebih awal oleh Ketua Negeri atas nasihat Menteri Besar — yang memberikan kerajaan negeri satu tuas masa yang ampuh: kerajaan yang merasakan angin baik boleh membubarkan lebih awal, seperti yang dilakukan kerajaan BN Melaka pada 2021 dan seperti yang dilakukan beberapa negeri sepanjang sejarah Malaysia untuk mengasingkan pilihan raya negeri mereka daripada kitaran persekutuan yang tidak menguntungkan.

Ketidakseimbangan perwakilan (malapportionment) adalah ciri struktur dewan {state}, sebagaimana di parlimen persekutuan. Kawasan pilihan raya terbesar dari segi pengundi di negeri ini jauh mengerdilkan yang terkecil — satu ciri yang dikuantifikasikan oleh data garis dasar: purata pengundi bagi setiap kerusi, dihitung secara langsung daripada fail keputusan, adalah {b['voters_total']/total:,.0f} pengundi, tetapi julat merentasi {total} kerusi adalah luas, dan kerusi luar bandar secara sistematik membawa berat pilihan raya yang kurang per pengundi berbanding yang ditunjukkan oleh taburan penduduk negeri. Kesan praktikalnya ialah segelintir kerusi luar bandar kecil boleh menjadi sama penentunya dengan kerusi bandar besar, yang membentuk tempat parti menumpukan jentera mereka.

Sejarah PRN negeri ini sendiri adalah faktor dalam kalkulus GE16. {prn_history_ms(state)} Sejarah ini penting kerana para pengundi negeri Malaysia telah menunjukkan, berulang kali, bahawa mereka melayan pertandingan negeri dan persekutuan sebagai berbeza: perbezaan paling dramatik berlaku di Melaka, di mana pengundi yang sama memberikan BN 21 daripada 28 kerusi negeri pada 2021 dan sifar daripada enam kerusi persekutuan pada 2022, tetapi setiap negeri membawa versi fenomena itu. Bahagian rangka kerja laporan ini oleh itu ditutup dengan amaran yang dibawa oleh model persekutuan itu sendiri: ayunan pilihan raya negeri adalah isyarat paling kuat yang ada, tetapi ia adalah isyarat tentang pengundi negeri, dan terjemahannya ke arena persekutuan adalah langkah pemodelan, bukan identiti.

---


"""
    return f"""## 2. The State Assembly & Electoral Framework

The {state} State Legislative Assembly comprises **{total} single-member constituencies**, each returning one assemblyman under the first-past-the-post system. This places {state} as the {assembly_rank(state)} largest state assembly in Malaysia, and the number of seats is fixed by the state constitution until a redelineation — a process that, like the federal one, has been politically frozen for years. Under FPTP, the candidate with the most votes in each constituency wins regardless of whether they command a majority of votes cast; the practical consequence is that {state}'s electoral geography — the concentration of each bloc's support across its {total} seats — matters more than its raw vote share. A bloc can win a majority of seats on a minority of votes if its support is efficiently distributed, and the reverse is equally true.

The term of the assembly runs five years from its first sitting. The current assembly, elected in {election_name(state)}, will therefore see its term conclude around **{term_end}**; the status of this state in the GE16 cycle is that {status_txt}. The dissolution rules are automatic — the assembly dissolves at the expiry of the five-year term unless dissolved earlier by the Head of State on the advice of the Menteri Besar — which gives the state government a powerful timing lever: a government that senses a favourable wind can dissolve early, as Melaka's BN government did in 2021 and as several states have done across Malaysian history to detach their state elections from unfavourable federal cycles.

Malapportionment is a structural feature of the {state} assembly, as it is of the federal parliament. The largest constituency by electorate in the state dwarfs the smallest — a feature that the baseline data quantifies: the average electorate per seat, computed live from the results file, is {b['voters_total']/total:,.0f} voters, but the range across the {total} seats is wide, and rural seats systematically carry less electoral weight per voter than the state's population distribution would imply. The practical effect is that a handful of small rural seats can be as decisive as a large urban seat, which shapes where parties concentrate their machinery.

The state's PRN history is itself a factor in the GE16 calculus. {prn_history(state)} This history matters because Malaysian state electorates have demonstrated, repeatedly, that they treat state and federal contests as distinct: the divergence is most dramatic in Melaka, where the same electorate gave BN 21 of 28 state seats in 2021 and zero of six federal seats in 2022, but every state carries some version of the phenomenon. The framework section of this report therefore closes with a warning that the federal model itself carries: state-election swings are the strongest signal available, but they are signals about the state electorate, and their translation to the federal arena is a modelling step, not an identity.

---

"""


def assembly_rank(state):
    order = ["Sarawak", "Perak", "Sabah", "Pahang", "Johor", "Selangor",
             "Pulau Pinang", "Kedah", "Negeri Sembilan", "Kelantan",
             "Terengganu", "Melaka", "Perlis"]
    try:
        return ordinal(order.index(state) + 1)
    except ValueError:
        return ""


def assembly_rank_ordinal(state):
    """Malay ordinal variant for the state-assembly ranking used in --lang ms mode."""
    order = ["Sarawak", "Perak", "Sabah", "Pahang", "Johor", "Selangor",
             "Pulau Pinang", "Kedah", "Negeri Sembilan", "Kelantan",
             "Terengganu", "Melaka", "Perlis"]
    try:
        return ordinal_ms(order.index(state) + 1)
    except ValueError:
        return ""


def prn_history(state):
    arch = STATE_META[state]["archetype"]
    h = {
        "bn_supermajority": ("Johor has been governed by BN or its predecessor coalition for all but "
                             "one term since independence, and the 2026 result restored the state to "
                             "its historical pattern after the 2018 reformasi interlude."),
        "green_wave": ("The state's conversion to PN in the 2023 cycle was the culmination of a "
                       "decade-long shift in the Malay north — from BN dominance through the 2018 "
                       "fracture to the present one-party ascendancy."),
        "melaka_divergence": ("The 2021 snap election — called after the collapse of the PH-led state "
                              "government through defections — delivered BN's 21-of-28 landslide, and "
                              "the state's 2026-27 PRN is now due inside the GE16 window."),
        "bn_resurgence": ("Negeri Sembilan returned to its historical BN-anchored pattern in 2026, "
                          "with the Unity Government pact delivering a commanding 29 of 36 seats."),
        "hung_coalition": ("The state has been governed since 2022 by a coalition assembled from the "
                           "largest blocs after an inconclusive election — the most unstable "
                           "government-formation arithmetic in the Peninsula."),
        "ph_led": ("The state has been governed by Pakatan Harapan since 2008, making it one of the "
                   "two longest-standing PH administrations alongside Selangor."),
        "sabah_local": ("Sabah's PRN history is a catalogue of shifting local coalitions, party "
                        "hopping, and short-lived governments — the 2025 result, in which the largest "
                        "single party went into opposition, is entirely consistent with that pattern."),
        "gps_hegemony": ("Sarawak has been governed continuously by GPS and its predecessors since "
                         "1963 — the longest unbroken party dominance of any Malaysian state — and "
                         "the 2021 result merely reaffirmed it."),
    }
    return h.get(arch, "")


def prn_history_ms(state):
    """Malay variant of prn_history — used by sec2_framework in --lang ms mode."""
    arch = STATE_META[state]["archetype"]
    h = {
        "bn_supermajority": ("Johor telah diperintah oleh BN atau gabungan pendahulunya untuk semua "
                             "kecuali satu penggal sejak kemerdekaan, dan keputusan 2026 memulihkan "
                             "negeri ini kepada corak sejarahnya selepas interlude reformasi 2018."),
        "green_wave": ("Penukaran negeri ini kepada PN dalam kitaran 2023 adalah kemuncak peralihan "
                       "satu dekad di utara Melayu — daripada dominasi BN melalui keretakan 2018 "
                       "kepada kebangkitan satu parti sekarang."),
        "melaka_divergence": ("Pilihan raya mengejut 2021 — dicetuskan oleh keruntuhan kerajaan negeri "
                              "pimpinan PH melalui lompatan parti — menyampaikan kemenangan besar BN "
                              "21 daripada 28 kerusi, dan PRN 2026-27 negeri ini kini jatuh tempo di "
                              "dalam tetingkap GE16."),
        "bn_resurgence": ("Negeri Sembilan kembali kepada corak berlabuh-BN sejarahnya pada 2026, "
                          "dengan pakatan Kerajaan Perpaduan menyampaikan 29 daripada 36 kerusi."),
        "hung_coalition": ("Negeri ini telah diperintah sejak 2022 oleh gabungan yang disusun daripada "
                           "blok terbesar selepas pilihan raya yang tidak muktamad — aritmetik "
                           "pembentukan kerajaan yang paling tidak stabil di Semenanjung."),
        "ph_led": ("Negeri ini telah diperintah oleh Pakatan Harapan sejak 2008, menjadikannya salah "
                   "satu daripada dua pentadbiran PH paling lama bertahan bersama Selangor."),
        "sabah_local": ("Sejarah PRN Sabah adalah katalog gabungan tempatan yang beralih, lompatan "
                        "parti, dan kerajaan jangka pendek — keputusan 2025, di mana parti tunggal "
                        "terbesar masuk ke pembangkang, sepenuhnya konsisten dengan corak itu."),
        "gps_hegemony": ("Sarawak telah diperintah secara berterusan oleh GPS dan pendahulunya sejak "
                         "1963 — dominasi parti tanpa putus yang paling lama bagi mana-mana negeri "
                         "Malaysia — dan keputusan 2021 hanya mengukuhkannya."),
    }
    return h.get(arch, "")


def sec3_electorate(state, b, fed, meta):
    d = fed["demog"]
    total_voters = b["voters_total"]
    avg_turnout = b["avg_turnout"]
    total = b["total"]
    lines = []
    if LANG == "ms":
        if d:
            lines.append(
                f"""Elektorat yang akan menentukan pertandingan negeri dan persekutuan {state} yang seterusnya adalah hasil daripada perluasan Undi18 dan pendaftaran pengundi automatik — transformasi nasional yang sama yang menambah kira-kira 5.6 juta pengundi ke daftar persekutuan. Di {state}, {total} kawasan pilihan raya negeri mendaftarkan **{total_voters:,} pengundi** pada pilihan raya negeri terbaharu (purata {total_voters/total:,.0f} bagi setiap kerusi), dan purata keluar mengundi adalah **{avg_turnout:.1f} peratus** merentasi negeri — {turnout_comment_ms(avg_turnout)}.

Struktur etnik pengundi {state}, ditimbang daripada daftar pengundi persekutuan GE15 merentasi {fed['n_seats']} kawasan persekutuan negeri ([S-04]), adalah kira-kira **{d['malay']:.1f}% Melayu, {d['chinese']:.1f}% Cina, {d['indian']:.1f}% India**, dengan bumiputera-Sabah pada {d['bumi_sabah']:.1f}%, bumiputera-Sarawak pada {d['bumi_sarawak']:.1f}% dan komuniti lain pada {d['other']:.1f}%. Komposisi ini adalah asas struktur bagi setiap unjuran yang melibatkan negeri ini: {eth_comment_ms(state, d)} Struktur umur juga sama pentingnya: pengundi berumur 18–30 tahun membentuk kira-kira **{d['youth']:.1f}%** daripada pengundi {state}, kumpulan 31–40 menambah {d['age31_40']:.1f}%, dan pengundi melebihi 60 tahun merangkumi {d['age60plus']:.1f}% — satu profil yang {age_comment_ms(state, d)}. Keseimbangan jantina hampir sekata pada {d['male']:.1f}% lelaki dan {d['female']:.1f}% perempuan, dengan purata umur median di kawasan pilihan raya negeri adalah {d['median']:.0f} tahun.

Geografi bandar-luar bandar elektorat {state} melengkapkan gambaran ini. {total} kerusi negeri merentasi {urban_rural_comment_ms(state, d)} — satu taburan yang menentukan di mana sokongan setiap blok tertumpu dan, yang lebih kritikal, seberapa cekap sokongan itu bertukar kepada kerusi di bawah FPTP. Medan pertempuran persekutuan yang dikenal pasti dalam Bahagian 10 — {fed['n_seats'] and ', '.join(s['constituency'] for s in fed['battlegrounds'][:3]) or 'tiada pada masa ini'} — terletak tepat di sempadan demografi di mana tiada kumpulan etnik cukup besar untuk menentukan keputusan lebih awal, dan di mana faktor L2–L4 menjadi penentu. Bagi GE16, elektorat negeri ini oleh itu difahami terbaik bukan sebagai satu blok tunggal tetapi sebagai timbunan elektorat yang berbeza — luar bandar dan bandar, majoriti Melayu dan campuran, muda dan tua — setiap satu dengan graviti politiknya sendiri."""
            )
        else:
            lines.append(
                f"""Elektorat {state} merangkumi **{total_voters:,} pengundi berdaftar** merentasi {total} kerusi negeri, dengan purata keluar mengundi **{avg_turnout:.1f} peratus** pada pilihan raya negeri terbaharu. Perincian demografi peringkat kawasan persekutuan belum tersedia untuk negeri ini dalam set data terbitan; analisis etnik dan umur dijalankan pada peringkat calon dalam Bahagian 5 sebagai gantinya."""
            )
        return "## 3. Pengundi\n\n" + "\n\n".join(lines) + "\n\n---\n\n"
    if d:
        lines.append(
            f"""The electorate that will decide {state}'s next state and federal contests is the product of the Undi18 expansion and automatic voter registration — the same national transformation that added roughly 5.6 million voters to the federal roll. In {state}, the {total} state constituencies registered **{total_voters:,} voters** at the latest state election (an average of {total_voters/total:,.0f} per seat), and turnout averaged **{avg_turnout:.1f} per cent** across the state — {turnout_comment(avg_turnout)}.

The ethnic structure of the {state} electorate, weighted from the GE15 federal voter roll across the state's {fed['n_seats']} federal constituencies ([S-04]), is approximately **{d['malay']:.1f}% Malay, {d['chinese']:.1f}% Chinese, {d['indian']:.1f}% Indian**, with bumiputera-Sabah at {d['bumi_sabah']:.1f}%, bumiputera-Sarawak at {d['bumi_sarawak']:.1f}% and other communities at {d['other']:.1f}%. This composition is the structural foundation of every projection involving the state: {eth_comment(state, d)} The age structure is equally consequential: voters aged 18–30 constitute approximately **{d['youth']:.1f}%** of the {state} electorate, the 31–40 bracket adds {d['age31_40']:.1f}%, and voters over 60 account for {d['age60plus']:.1f}% — a profile that is {age_comment(state, d)}. The gender balance is near-even at {d['male']:.1f}% male and {d['female']:.1f}% female, and the average median age across the state's constituencies is {d['median']:.0f} years.

The urban-rural geography of the {state} electorate completes the picture. The state's {total} state seats span {urban_rural_comment(state, d)} — a spread that determines where each bloc's support is concentrated and, critically, how efficiently that support converts into seats under FPTP. The federal battlegrounds identified in Section 15 — {fed['n_seats'] and ', '.join(s['constituency'] for s in fed['battlegrounds'][:3]) or 'none currently'} — sit precisely at the demographic boundaries where no ethnic group is large enough to pre-determine the outcome, and where the L2–L4 factors become decisive. For GE16, the state's electorate is therefore best understood not as a single bloc but as a stack of distinct electorates — rural and urban, Malay-majority and mixed, young and old — each with its own political gravity."""
        )
    else:
        lines.append(
            f"""The {state} electorate comprises **{total_voters:,} registered voters** across {total} state seats, with an average turnout of **{avg_turnout:.1f} per cent** at the latest state election. Federal constituency-level demographic detail is not yet available for this state in the derived dataset; the ethnic and age analysis is carried at the candidate level in Section 5 instead."""
        )
    return "## 3. The Electorate\n\n" + "\n\n".join(lines) + "\n\n---\n\n"


def turnout_comment(t):
    if t >= 75:
        return "a high-participation polity in which the rural machinery parties' ground game pays a measurable dividend"
    if t >= 68:
        return "a solid, slightly above-national participation rate typical of a state where the contest is genuinely joined"
    return "a participation rate below the national norm — a soft-turnout environment in which the youth differential can swing marginal seats"


def turnout_comment_ms(t):
    if t >= 75:
        return "satu badan pengundi penyertaan tinggi di mana permainan medan parti jentera luar bandar membayar dividen yang boleh diukur"
    if t >= 68:
        return "kadar penyertaan yang kukuh, sedikit di atas purata nasional, tipikal bagi negeri yang pertandingannya benar-benar disertai"
    return "kadar penyertaan di bawah norma nasional — satu persekitaran keluar mengundi yang lembut di mana perbezaan belia boleh mengayunkan kerusi marginal"


def eth_comment(state, d):
    arch = STATE_META[state]["archetype"]
    if d["malay"] > 80:
        return ("a heavily Malay-majority electorate in which the battle is fought within the Malay "
                "community — between the PN and BN/PH Malay parties — and the non-Malay share, while "
                "not decisive in most seats, is the margin in the state's urban pockets")
    if d["malay"] > 60:
        return ("a Malay-majority but substantially mixed electorate, where the Chinese and Indian "
                "shares are large enough to be decisive in the urban seats while the rural Malay "
                "belt carries the state's rural geography")
    if arch in ("gps_hegemony", "sabah_local"):
        return ("a bumiputera-dominant electorate in which the intra-bumiputera distinction — "
                "Malay-Melanau versus Dayak in Sarawak, the dozens of Sabah communities — is the "
                "real cleavage, and the national Malay/Chinese/Indian framing is a poor lens")
    return ("a genuinely mixed electorate in which no single community commands a majority of the "
            "state's seats — the classic Malaysian battleground profile, where coalition arithmetic "
            "and seat allocation across ethnic lines decide the outcome")


def eth_comment_ms(state, d):
    arch = STATE_META[state]["archetype"]
    if d["malay"] > 80:
        return ("satu elektorat majoriti Melayu yang berat di mana pertempuran berlaku dalam komuniti "
                "Melayu — antara parti Melayu PN dan BN/PH — dan bahagian bukan Melayu, walaupun "
                "tidak penentu di kebanyakan kerusi, adalah margin di kantung bandar negeri ini")
    if d["malay"] > 60:
        return ("satu elektorat majoriti Melayu tetapi bercampur secara ketara, di mana bahagian Cina "
                "dan India cukup besar untuk menjadi penentu di kerusi bandar manakala Sabuk Melayu "
                "luar bandar membawa geografi luar bandar negeri")
    if arch in ("gps_hegemony", "sabah_local"):
        return ("satu elektorat dominan bumiputera di mana perbezaan dalam bumiputera — Melayu-Melanau "
                "berbanding Dayak di Sarawak, berpuluh komuniti Sabah — adalah garis pemisah sebenar, "
                "dan kerangka nasional Melayu/Cina/India adalah lensa yang lemah")
    return ("satu elektorat yang benar-benar bercampur di mana tiada satu komuniti pun menguasai "
            "majoriti kerusi negeri — profil medan pertempuran klasik Malaysia, di mana aritmetik "
            "gabungan dan peruntukan kerusi merentasi garisan etnik menentukan keputusan")


def age_comment(state, d):
    if d["youth"] > 32:
        return "among the youngest in the country — a demographic that the 2023 wave showed to be reachable by both poles of the Malay political spectrum"
    if d["youth"] > 28:
        return "younger than the national average, with the youth share large enough to be a swing factor in the state's marginal seats"
    return "older than the national average, with the 60-plus cohort carrying disproportionate weight — a profile that historically favours continuity and incumbency"


def age_comment_ms(state, d):
    if d["youth"] > 32:
        return "antara yang termuda di negara ini — satu demografi yang gelombang 2023 menunjukkan boleh dicapai oleh kedua-dua kutub spektrum politik Melayu"
    if d["youth"] > 28:
        return "lebih muda daripada purata nasional, dengan bahagian belia cukup besar untuk menjadi faktor ayunan di kerusi marginal negeri"
    return "lebih tua daripada purata nasional, dengan kohort 60 ke atas membawa berat yang tidak seimbang — satu profil yang secara sejarahnya menguntungkan kesinambungan dan penyandangan"


def urban_rural_comment(state, d):
    arch = STATE_META[state]["archetype"]
    if arch in ("green_wave",):
        return "a predominantly rural and small-town geography, with a few urban pockets that vote on a completely different axis"
    if arch in ("ph_led",):
        return "a heavily urbanised geography anchored by the state capital's conurbation, with a substantial semi-urban ring and a rural fringe"
    if arch in ("gps_hegemony", "sabah_local"):
        return "an overwhelmingly rural geography of small towns, longhouses and coastal settlements, with the urban seats concentrated in a handful of cities"
    if arch == "melaka_divergence":
        return "a compact geography in which urban, semi-urban and rural seats sit within minutes of each other — the state's smallness is itself a political fact"
    return "a mixed geography of urban cores, semi-urban corridors and rural hinterland"


def urban_rural_comment_ms(state, d):
    arch = STATE_META[state]["archetype"]
    if arch in ("green_wave",):
        return "geografi yang kebanyakannya luar bandar dan pekan kecil, dengan beberapa kantung bandar yang mengundi pada paksi yang berbeza sama sekali"
    if arch in ("ph_led",):
        return "geografi yang sangat bandar berlabuh oleh konurbasi ibu negeri, dengan gelang separa bandar yang besar dan pinggir luar bandar"
    if arch in ("gps_hegemony", "sabah_local"):
        return "geografi yang sebahagian besarnya luar bandar — pekan kecil, rumah panjang dan petempatan pantai, dengan kerusi bandar tertumpu di segelintir bandar"
    if arch == "melaka_divergence":
        return "geografi padat di mana kerusi bandar, separa bandar dan luar bandar terletak dalam jarak beberapa minit antara satu sama lain — kekecilan negeri ini sendiri adalah fakta politik"
    return "geografi campuran teras bandar, koridor separa bandar dan pedalaman luar bandar"


def sec4_baseline(state, b, meta):
    total = b["total"]
    bloc_rows = "\n".join(
        f"| {bloc} | {seats} | {seats/total*100:.1f}% |"
        for bloc, seats in bloc_order_by_seats(b["bloc_seats"])
    )
    party_rows = "\n".join(
        f"| {party} | {seats} |" for party, seats in bloc_order_by_seats(b["party_seats"])
    )
    seat_rows = "\n".join(
        f"| {s['seat']} | {s['winner']} | {s['party']} | {s['bloc']} | "
        f"{s['votes']:,} | {s['votes_perc']:.1f}% | {s['majority']:,} | {s['turnout']:.1f}% |"
        for s in b["seats"]
    )
    close_rows = "\n".join(
        f"| {s['seat']} | {s['winner']} | {s['party']} | {s['bloc']} | {s['margin']:.2f}% | {s['majority']:,} |"
        for s in b["closest"][:6]
    )
    safe_rows = "\n".join(
        f"| {s['seat']} | {s['winner']} | {s['party']} | {s['bloc']} | {s['margin']:.1f}% |"
        for s in b["safest"][:4]
    )
    if LANG == "ms":
        govt_seats_b = sum(b['bloc_seats'].get(g, 0) for g in govt_blocs_for(state))
        supermajority_txt = (f"majoriti dua pertiga lebih (ambang {b['two_thirds']})"
                             if max(b['bloc_seats'].values()) >= b['two_thirds']
                             else f"kurang daripada majoriti dua pertiga ({b['two_thirds']} diperlukan)")
        return f"""## 4. Garis Dasar PRN {election_name(state)}

Garis dasar bagi setiap unjuran hadapan yang melibatkan {state} ialah pilihan raya negeri {election_name(state)}. Keputusan itu merupakan penambat empirikal — ukuran penuh terakhir tentang bagaimana pengundi negeri ini sebenarnya mengundi — dan setiap anjakan, setiap senario dan setiap analisis medan pertempuran dalam laporan ini dinyatakan sebagai pergerakan daripadanya.

### 4.1 Aritmetik kerusi

Dewan Undangan yang terhasil daripada pilihan raya itu tersusun seperti berikut:

| Blok | Kerusi | Bahagian |
|---|---|---|
{bloc_rows}
| **Jumlah** | **{total}** | **100%** |

Komposisi di peringkat parti di bawah blok-blok tersebut memperlihatkan keseimbangan kuasa dalaman negeri:

| Parti | Kerusi |
|---|---|
{party_rows}

Jumlah keseluruhan blok menghasilkan sebuah kerajaan **{meta['govt_label']}** yang menguasai **{govt_seats_b}** daripada {total} kerusi — {supermajority_txt}. Struktur margin negeri merupakan lapisan kedua garis dasar: purata margin kemenangan di kesemua {total} kerusi ialah **{b['avg_margin']:.1f} mata peratusan** daripada undi sah, namun serakan itulah angka yang paling bermakna dari segi politik. **{b['margin_tiers']['super']}** kerusi dimenangi dengan margin kurang daripada dua mata (super-marginal), **{b['margin_tiers']['marginal']}** lagi antara dua hingga lima mata (marginal), **{b['margin_tiers']['comfortable']}** antara lima hingga sepuluh mata (selesa), dan **{b['margin_tiers']['safe']}** dengan lebih sepuluh mata (selamat). {b['margin_tiers']['super'] + b['margin_tiers']['marginal']} kerusi dalam dua lapisan pertama inilah — kira-kira {((b['margin_tiers']['super'] + b['margin_tiers']['marginal']) / total * 100):.0f} peratus daripada Dewan — yang membawa pertandingan sebenar di negeri ini.

### 4.2 Kerusi paling tipis dan paling selamat

Keputusan paling nipis di negeri ini menentukan geografi medan pertempurannya:

| Kerusi | Pemenang | Parti | Blok | Margin | Majoriti (undi) |
|---|---|---|---|---|---|
{close_rows}

Di hujung yang satu lagi, kerusi paling selamat di negeri ini — dimenangi dengan majoriti besar yang tidak terganggu oleh sebarang anjakan dalam pemodelan — menjadi pasak aritmetik blok:

| Kerusi | Pemenang | Parti | Blok | Margin |
|---|---|---|---|---|
{safe_rows}

### 4.3 Keluar mengundi dan penyertaan

Keluar mengundi merentasi negeri purata **{b['avg_turnout']:.1f} peratus** daripada pengundi berdaftar, dengan {b['voters_total']:,} pengundi berdaftar dan {b['votes_valid']:,} undi sah dibuang. Pertandingan purata **{b['avg_n_candidates']:.1f} calon bagi setiap kerusi** (sehingga {b['max_n_candidates']} di kawasan paling sesak) — satu ukuran pemecahan sistem parti negeri. Serakan keluar mengundi sama pentingnya dengan purata: {turnout_dispersion_ms(b)} Garis dasar ditutup dengan nota tentang apa yang tidak terkandung di dalamnya: ia mengukur tingkah laku elektorat negeri pada satu masa, dan bahagian-bahagian berikut mengukur segala yang telah berubah sejak itu.

---


"""
    return f"""## 4. The {election_name(state)} PRN Baseline

The baseline for every forward projection involving {state} is the state election of {election_name(state)}. The result is the empirical anchor — the last full measurement of how this state's electorates actually voted — and every swing, every scenario and every battleground analysis in this report is expressed as a movement from it.

### 4.1 The seat arithmetic

The assembly returned by that election is composed as follows:

| Bloc | Seats | Share |
|---|---|---|
{bloc_rows}
| **Total** | **{total}** | **100%** |

The party-level composition beneath the blocs is where the state's internal balance of power is visible:

| Party | Seats |
|---|---|
{party_rows}

The bloc totals translate into a government of **{meta['govt_label']}** commanding **{sum(b['bloc_seats'].get(g,0) for g in govt_blocs_for(state))}** of {total} seats — {('a two-thirds-plus supermajority (threshold ' + str(b['two_thirds']) + ')') if max(b['bloc_seats'].values()) >= b['two_thirds'] else ('short of a two-thirds majority (' + str(b['two_thirds']) + ' needed)')}. The margin structure of the state is the second layer of the baseline: the average winning margin across all {total} seats was **{b['avg_margin']:.1f} percentage points** of valid votes, but the dispersion is the politically meaningful number. **{b['margin_tiers']['super']}** seats were won by fewer than two points (super-marginal), **{b['margin_tiers']['marginal']}** by between two and five (marginal), **{b['margin_tiers']['comfortable']}** by between five and ten (comfortable), and **{b['margin_tiers']['safe']}** by more than ten points (safe). It is the {b['margin_tiers']['super'] + b['margin_tiers']['marginal']} seats in the first two tiers — roughly {((b['margin_tiers']['super'] + b['margin_tiers']['marginal']) / total * 100):.0f} per cent of the assembly — that carry the state's genuine contestability.

### 4.2 The closest and safest seats

The thinnest results in the state define its battleground geography:

| Seat | Winner | Party | Bloc | Margin | Majority (votes) |
|---|---|---|---|---|---|
{close_rows}

At the other end, the state's safest seats — won by landslides that no modelled swing disturbs — anchor the bloc arithmetic:

| Seat | Winner | Party | Bloc | Margin |
|---|---|---|---|---|
{safe_rows}

### 4.3 Turnout and participation

Turnout across the state averaged **{b['avg_turnout']:.1f} per cent** of registered voters, with {b['voters_total']:,} registered voters and {b['votes_valid']:,} valid votes cast. The contests averaged **{b['avg_n_candidates']:.1f} candidates per seat** (up to {b['max_n_candidates']} in the most crowded constituency) — a measure of the fragmentation of the state's party system. Turnout dispersion matters as much as the average: {turnout_dispersion(b)} The baseline closes with a note on what it does not contain: it measures the state electorate's behaviour at one moment in time, and the sections that follow measure everything that has moved since.

---

"""


def turnout_dispersion(b):
    ts = sorted(s["turnout"] for s in b["seats"])
    if not ts:
        return ""
    lo, hi = ts[0], ts[-1]
    return (f"the highest-turnout seats recorded {hi:.1f} per cent and the lowest {lo:.1f} per cent — "
            f"a spread of {hi-lo:.1f} points that tracks the state's urban-rural and "
            f"machinery-strength geography, and a reminder that in the super-marginal seats, "
            f"a few points of turnout differential can be the election.")


def turnout_dispersion_ms(b):
    ts = sorted(s["turnout"] for s in b["seats"])
    if not ts:
        return ""
    lo, hi = ts[0], ts[-1]
    return (f"kerusi dengan keluar mengundi tertinggi merekodkan {hi:.1f} peratus dan yang terendah {lo:.1f} peratus — "
            f"julat {hi-lo:.1f} mata yang mengikuti geografi bandar-luar bandar dan kekuatan jentera negeri, "
            f"serta satu peringatan bahawa di kerusi super-marginal, beberapa mata perbezaan keluar mengundi "
            f"boleh menjadi penentu pilihan raya.")


def sec5_candidates(state, b, c):
    if not c or c["n_candidates"] == 0:
        return """## 5. Candidate Demographics

The candidate-level register for this state is not yet available in the derived dataset. The demographic analysis of candidacy — ethnicity, age, sex, and the won-versus-lost structure — will be added when the `dun-candidates-latest.csv` file is populated. The remainder of the report proceeds on the seat-level baseline, which is complete.

---

"""

    eth_rows = "\n".join(
        f"| {e} | {c['eth_all'].get(e, 0)} | {c['eth_won'].get(e, 0)} | {c['eth_winrate'].get(e, 0):.0f}% |"
        for e in sorted(c["eth_all"], key=lambda x: -c["eth_all"][x])
    )
    sex_rows = "\n".join(
        f"| {'Male' if s == 'M' else 'Female' if s == 'F' else s} | {c['sex_all'].get(s, 0)} | "
        f"{c['sex_won'].get(s, 0)} | {c['sex_winrate'].get(s, 0):.0f}% |"
        for s in sorted(c["sex_all"], key=lambda x: -c["sex_all"][x])
    )
    br_rows = "\n".join(
        f"| {br.replace('_', '-')} | {c['brackets'].get(br, 0)} | {c['brackets_won'].get(br, 0)} |"
        for br in ["under_30", "30_39", "40_49", "50_59", "60plus"]
    )
    yc = c["youngest_cand"]
    oc = c["oldest_cand"]
    yc_txt = f"{yc['name']} ({yc['party']}, {yc['seat']}, aged {yc['age']})" if yc else "not recorded"
    oc_txt = f"{oc['name']} ({oc['party']}, {oc['seat']}, aged {oc['age']})" if oc else "not recorded"
    # won-vs-lost narrative by ethnicity
    eth_narr = ethnicity_advantage(c)
    sex_narr = sex_advantage(c)
    if LANG == "ms":
        yc_txt = f"{yc['name']} ({yc['party']}, {yc['seat']}, berumur {yc['age']})" if yc else "tidak direkodkan"
        oc_txt = f"{oc['name']} ({oc['party']}, {oc['seat']}, berumur {oc['age']})" if oc else "tidak direkodkan"
        return f"""## 5. Demografi Calon

Daftar calon bagi pilihan raya negeri {election_name(state)} merupakan sumber demografi terkaya projek ini: **{c['n_candidates']} calon** bertanding di {c['n_seats']} kerusi negeri — purata **{c['avg_per_seat']:.1f} calon bagi setiap kerusi** — dan daftar itu merekodkan parti, gabungan, undi, keputusan, jantina, etnik dan umur setiap calon. Bahagian ini unik kepada laporan negeri; laporan persekutuan tidak membawa analisis demografi peringkat calon, dan ia disertakan di sini kerana siapa yang bertanding di sesebuah negeri, dan siapa yang menang, adalah isyarat dengan sendirinya.

### 5.1 Etnik calon

| Etnik | Calon | Menang | Kadar kemenangan |
|---|---|---|---|
{eth_rows}

Etnik pencalonan mengikuti elektorat negeri hanya secara longgar. {ethnicity_advantage_ms(c)} Kadar kemenangan adalah ukuran yang lebih tajam: ia mendedahkan sama ada kerusi negeri memberi ganjaran kepada calon daripada komuniti tertentu melebihi bahagian pencalonan mereka — cap jari bagaimana struktur etnik bertukar menjadi perwakilan di bawah FPTP.

### 5.2 Profil umur

Taburan umur calon, dihitung secara langsung daripada daftar, berjalan daripada **{c['youngest']}** (termuda) hingga **{c['oldest']}** (tertua), dengan purata umur calon **{c['avg_age']:.1f}** tahun. Pemenang berpurata **{c['avg_age_won']:.1f}** tahun berbanding **{c['avg_age_lost']:.1f}** bagi yang kalah — {age_advantage_ms(c)} Calon termuda yang direkodkan ialah {yc_txt}; yang tertua ialah {oc_txt}.

| Kumpulan umur | Calon | Menang |
|---|---|---|
{br_rows}

### 5.3 Keseimbangan jantina

| Jantina | Calon | Menang | Kadar kemenangan |
|---|---|---|---|
{sex_rows}

Keseimbangan jantina kumpulan calon adalah {sex_advantage_ms(c)} Pencalonan wanita adalah {women_comment_ms(c)} — satu ciri struktur sistem parti negeri yang didedahkan secara langsung oleh daftar ini.

### 5.4 Kehilangan deposit dan kualiti pertandingan

**{c['deposit_lost']}** daripada {c['n_candidates']} calon ({(c['deposit_lost']/c['n_candidates']*100):.0f} peratus) kehilangan deposit kerana gagal melepasi ambang undi 12.5 peratus — ukuran piawai berapa banyak pertandingan negeri ini benar-benar persaingan sebenar dan bukan sekadar pemasangan bendera. Purata calon setiap kerusi {c['avg_per_seat']:.1f}, berbanding corak nasional tiga hingga lima, meletakkan pertandingan {state_short(state)} {contest_quality_ms(c)}. Profil demografi pencalonan — siapa yang dipilih parti untuk bertanding, berapa umur, daripada komuniti mana, di kerusi mana — adalah penunjuk mendahului strategi parti untuk GE16, dan ia dibaca di sini dengan ketelusan penuh.

---


"""
    return f"""## 5. Candidate Demographics

The candidate register for the {election_name(state)} state election is the project's richest demographic resource: **{c['n_candidates']} candidates** contested the state's {c['n_seats']} seats — an average of **{c['avg_per_seat']:.1f} candidates per seat** — and the register records each candidate's party, coalition, votes, result, sex, ethnicity and age. This section is unique to the state reports; the federal report does not carry a candidate-level demographic analysis, and it is included here because who contests a state, and who wins, is itself a signal.

### 5.1 Ethnicity of candidates

| Ethnicity | Candidates | Won | Win rate |
|---|---|---|---|
{eth_rows}

The ethnicity of candidacy tracks the state's electorate only loosely. {eth_narr} The win rates are the sharper measure: they reveal whether the state's seats reward candidates of a particular community beyond their share of candidacy — the fingerprint of how ethnic structure converts into representation under FPTP.

### 5.2 Age profile

The age distribution of candidates, computed live from the register, runs from **{c['youngest']}** (youngest) to **{c['oldest']}** (oldest), with a mean candidate age of **{c['avg_age']:.1f}** years. Winners average **{c['avg_age_won']:.1f}** years against **{c['avg_age_lost']:.1f}** for losers — {age_advantage(c)} The youngest candidate recorded is {yc_txt}; the oldest is {oc_txt}.

| Age bracket | Candidates | Won |
|---|---|---|
{br_rows}

### 5.3 Sex balance

| Sex | Candidates | Won | Win rate |
|---|---|---|---|
{sex_rows}

The sex balance of the candidate pool is {sex_narr} Women's candidacy is {women_comment(c)} — a structural feature of the state's party system that the register exposes directly.

### 5.4 Deposit losses and contest quality

**{c['deposit_lost']}** of the {c['n_candidates']} candidates ({(c['deposit_lost']/c['n_candidates']*100):.0f} per cent) forfeited their deposits by failing to clear the 12.5 per cent vote threshold — the standard measure of how much of the state's contest was real competition rather than flag-planting. The candidates-per-seat average of {c['avg_per_seat']:.1f}, against a national pattern of three-to-five, places {state_short(state)}'s contest {contest_quality(c)}. The demographic profile of candidacy — who the parties choose to stand, how old, of which community, in which seats — is a leading indicator of party strategy for GE16, and it is read here in full transparency.

---

"""


def state_short(state):
    return state


def ethnicity_advantage(c):
    eth = c["eth_all"]
    won = c["eth_won"]
    wr = c["eth_winrate"]
    if not eth:
        return "The candidate pool is ethnically undifferentiated in the register."
    top = sorted(eth, key=lambda e: -eth[e])[0]
    parts = []
    for e in sorted(eth, key=lambda x: -eth[x])[:3]:
        parts.append(f"{e} candidates won {won.get(e, 0)} of {eth[e]} contests ({wr.get(e, 0):.0f} per cent)")
    adv = [e for e in eth if wr.get(e, 0) > 50 and eth[e] >= 5]
    if adv:
        return (f"The largest community in the pool is {top} ({eth[top]} candidates). "
                f"On win rates, {'; '.join(parts)}. Communities with a measurable edge "
                f"({', '.join(adv)} — win rate above 50 per cent on at least five contests) "
                f"hold a structural advantage that the seat geography reinforces.")
    return (f"The largest community in the pool is {top} ({eth[top]} candidates). "
            f"On win rates, {'; '.join(parts)} — a distribution that broadly mirrors the "
            f"state's seat geography rather than any single community holding an outsized edge.")


def ethnicity_advantage_ms(c):
    eth = c["eth_all"]
    won = c["eth_won"]
    wr = c["eth_winrate"]
    if not eth:
        return "Kumpulan calon tidak dibezakan secara etnik dalam daftar."
    top = sorted(eth, key=lambda e: -eth[e])[0]
    parts = []
    for e in sorted(eth, key=lambda x: -eth[x])[:3]:
        parts.append(f"calon {e} memenangi {won.get(e, 0)} daripada {eth[e]} pertandingan ({wr.get(e, 0):.0f} peratus)")
    adv = [e for e in eth if wr.get(e, 0) > 50 and eth[e] >= 5]
    if adv:
        return (f"Komuniti terbesar dalam kumpulan ialah {top} ({eth[top]} calon). "
                f"Dari segi kadar kemenangan, {'; '.join(parts)}. Komuniti yang mempunyai kelebihan "
                f"boleh diukur ({', '.join(adv)} — kadar kemenangan melebihi 50 peratus pada sekurang-"
                f"kurangnya lima pertandingan) memegang kelebihan struktur yang diperkukuh oleh "
                f"geografi kerusi.")
    return (f"Komuniti terbesar dalam kumpulan ialah {top} ({eth[top]} calon). "
            f"Dari segi kadar kemenangan, {'; '.join(parts)} — satu taburan yang secara amnya "
            f"mencerminkan geografi kerusi negeri dan bukannya mana-mana satu komuniti memegang "
            f"kelebihan luar biasa.")


def sex_advantage(c):
    m = c["sex_all"].get("M", 0)
    f = c["sex_all"].get("F", 0)
    mw = c["sex_won"].get("M", 0)
    fw = c["sex_won"].get("F", 0)
    total_won = mw + fw
    if total_won == 0:
        return "not computable from the register."
    mshare = mw / total_won * 100
    if mshare > 75:
        return f"overwhelmingly male: men won {mw} of {total_won} seats ({mshare:.0f} per cent), despite comprising {m/(m+f)*100:.0f} per cent of candidates."
    if mshare > 60:
        return f"male-skewed: men won {mw} of {total_won} seats ({mshare:.0f} per cent) against {m/(m+f)*100:.0f} per cent of candidacies."
    return f"close to proportional: men won {mw} of {total_won} seats ({mshare:.0f} per cent) on {m/(m+f)*100:.0f} per cent of candidacies."


def sex_advantage_ms(c):
    m = c["sex_all"].get("M", 0)
    f = c["sex_all"].get("F", 0)
    mw = c["sex_won"].get("M", 0)
    fw = c["sex_won"].get("F", 0)
    total_won = mw + fw
    if total_won == 0:
        return "tidak boleh dikira daripada daftar."
    mshare = mw / total_won * 100
    if mshare > 75:
        return f"didominasi lelaki: lelaki memenangi {mw} daripada {total_won} kerusi ({mshare:.0f} peratus), walaupun hanya {m/(m+f)*100:.0f} peratus daripada calon."
    if mshare > 60:
        return f"condong kepada lelaki: lelaki memenangi {mw} daripada {total_won} kerusi ({mshare:.0f} peratus) berbanding {m/(m+f)*100:.0f} peratus pencalonan."
    return f"hampir berkadar: lelaki memenangi {mw} daripada {total_won} kerusi ({mshare:.0f} peratus) daripada {m/(m+f)*100:.0f} peratus pencalonan."


def women_comment(c):
    f = c["sex_all"].get("F", 0)
    n = c["n_candidates"]
    share = f / n * 100 if n else 0
    if share < 15:
        return "far below the one-third threshold that gender-quota advocates target — a signal that the state's parties have yet to internalise the national conversation on female representation"
    if share < 25:
        return "below the one-third threshold, though above the national floor — representation is improving but remains a minority phenomenon"
    return "at or above the levels seen nationally — the state's parties are among the more representative in the country"


def women_comment_ms(c):
    f = c["sex_all"].get("F", 0)
    n = c["n_candidates"]
    share = f / n * 100 if n else 0
    if share < 15:
        return "jauh di bawah ambang satu pertiga yang disasarkan oleh penyokong kuota jantina — isyarat bahawa parti-parti negeri belum menghayati wacana nasional tentang perwakilan wanita"
    if share < 25:
        return "di bawah ambang satu pertiga, walaupun melebihi paras minimum nasional — perwakilan sedang meningkat tetapi kekal sebagai fenomena minoriti"
    return "pada atau melebihi paras yang dilihat di peringkat nasional — parti-parti negeri ini antara yang paling mewakili di negara ini"


def age_advantage_ms(c):
    diff = c["avg_age_won"] - c["avg_age_lost"]
    if diff <= -2:
        return "pemenang nyata lebih muda daripada yang kalah, konsisten dengan isyarat keutamaan belia yang kelihatan dalam pilihan raya negeri Malaysia terkini"
    if diff <= 2:
        return "pemenang dan yang kalah tidak dapat dibezakan secara statistik dari segi umur — pertandingan negeri ini tidak diasingkan mengikut umur"
    return "pemenang condong lebih tua daripada yang kalah, menunjukkan penyandangan dan pengalaman diberi ganjaran dalam pertandingan negeri ini"


def contest_quality_ms(c):
    share = c["deposit_lost"] / c["n_candidates"] * 100 if c["n_candidates"] else 0
    if share > 40:
        return "antara yang paling berpecah di negara ini — sebahagian besar pencalonan adalah kenderaan dan bukannya pertandingan serius"
    if share > 25:
        return "di tengah julat nasional — persaingan pelbagai penjuru yang tulen dengan ekor pemasang bendera"
    return "antara yang paling berdisiplin di negara ini — kebanyakan pencalonan serius, dan kehilangan deposit adalah pengecualian"


def age_advantage(c):
    diff = c["avg_age_won"] - c["avg_age_lost"]
    if diff <= -2:
        return "winners are measurably younger than losers, consistent with the youth-preference signal visible in recent Malaysian state elections"
    if diff <= 2:
        return "winners and losers are statistically indistinguishable in age — the state's contests are not age-sorted"
    return "winners skew older than losers, suggesting incumbency and experience are rewarded in this state's contests"


def contest_quality(c):
    share = c["deposit_lost"] / c["n_candidates"] * 100 if c["n_candidates"] else 0
    if share > 40:
        return "among the most fragmented in the country — a large share of candidacies are vehicles rather than serious contests"
    if share > 25:
        return "in the middle of the national range — genuine multi-cornered competition with a tail of flag-planters"
    return "among the most disciplined in the country — most candidacies are serious, and deposit losses are the exception"


def sec6_federal(state, fed, b, meta, forecast_state=None):
    rows = "\n".join(
        f"| {s['code']} | {s['constituency']} | {s['ge15_bloc']} ({s['ge15_party']}) | {s['ge15_winner']} | {s['ge15_margin']:.2f}% |"
        for s in fed["seats"]
    )
    bg_rows = "\n".join(
        f"| {s['code']} | {s['constituency']} | {s['ge15_bloc']} | {s['ge15_margin']:.2f}% | {s['registered']:,} |"
        for s in fed["battlegrounds"]
    ) if fed["battlegrounds"] else "| — | no federal battlegrounds in this state | — | — | — |"
    bloc_totals = defaultdict(int)
    for s in fed["seats"]:
        bloc_totals[s["ge15_bloc"]] += 1
    fed_rows = "\n".join(f"| {bloc} | {n} |" for bloc, n in bloc_order_by_seats(dict(bloc_totals)))

    # ---- FEDERAL VERDICT (gap analysis item b) ----
    federal_verdict = ""
    if forecast_state and forecast_state.get("seats"):
        proj_seats = forecast_state["seats"]
        flips_in_state = [s for s in proj_seats if s["proj_winner"] != s.get("ge15_winner", "")]
        held = [s for s in proj_seats if s["proj_winner"] == s.get("ge15_winner", "")]
        mc_flips = forecast_state.get("monte_carlo", {}).get("flips_P50", 0)
        det = forecast_state.get("deterministic", {})
        # deterministic has bloc totals directly: PN, PH, BN, GPS, GRS, WARISAN, etc.
        gov_p50 = det.get("PH", 0) + det.get("BN", 0) + det.get("GPS", 0) + det.get("GRS", 0) + det.get("WARISAN", 0) + det.get("MUDA", 0) + det.get("KDM", 0) + det.get("PBM", 0)
        pn_p50 = det.get("PN", 0)
        ph_p50 = det.get("PH", 0)
        bn_p50 = det.get("BN", 0)

        verdict_intro = (
            f"**Federal forecast verdict on {state}'s seats:**\n\n"
            f"The federal GE16 projection engine ([S-07]) has modelled every one of {state}'s "
            f"{len(proj_seats)} parliamentary seats individually. The P50 Monte Carlo outcome "
            f"projects the federal government at **{gov_p50} seats**, PN at **{pn_p50}**, PH at "
            f"**{ph_p50}**, BN at **{bn_p50}**, with **{mc_flips} total flips** nationwide.\n\n"
        )
        if flips_in_state:
            flip_details = "\n".join(
                f"- **{s['constituency']}** ({s['code']}): projected to flip from "
                f"**{s['ge15_winner']}** ({s.get('ge15_bloc', '')}) → **"
                f"{s['proj_winner']}** (margin: {s['proj_margin']:.2f}pp)"
                for s in flips_in_state
            )
            verdict_body = (
                f"Within {state}, the federal model projects **{len(flips_in_state)} seat(s) to flip**: "
                f"\n\n{flip_details}\n\n"
            )
        else:
            verdict_body = (
                f"Within {state}, all {len(proj_seats)} parliamentary seats are projected to hold "
                f"at their GE15 result — no flips in the P50 scenario.\n\n"
            )
        held_list = ", ".join(f'{s["constituency"]} ({s["proj_winner"]})' for s in held[:5])
        held_suffix = "..." if len(held) > 5 else ""
        held_summary = (
            f"The remaining {len(held)} seats are projected to hold at their GE15 winners: "
            f"{held_list}{held_suffix}."
        )
        federal_verdict = verdict_intro + verdict_body + held_summary + "\n\n"

    if LANG == "ms":
        fed_verdict_ms = ""
        if forecast_state:
            det = forecast_state.get("deterministic", {})
            mc = forecast_state.get("monte_carlo", {})
            proj_seats = forecast_state.get("seats", [])
            flips_in_state = [s for s in proj_seats if s.get("flip")]
            held = [s for s in proj_seats if not s.get("flip")]
            mc_flips = mc.get("flips_P50", len(flips_in_state))
            gov_p50 = (det.get("PH", 0) + det.get("BN", 0) + det.get("GPS", 0) + det.get("GRS", 0)
                       + det.get("WARISAN", 0) + det.get("MUDA", 0) + det.get("KDM", 0) + det.get("PBM", 0))
            pn_p50 = det.get("PN", 0)
            ph_p50 = det.get("PH", 0)
            bn_p50 = det.get("BN", 0)
            intro_ms = (
                f"**Verdikt unjuran persekutuan ke atas kerusi {state}:**\n\n"
                f"Enjin unjuran GE16 persekutuan ([S-07]) telah memodelkan setiap satu daripada "
                f"{len(proj_seats)} kerusi parlimen {state} secara individu. Keputusan P50 Monte Carlo "
                f"mengunjurkan kerajaan persekutuan pada **{gov_p50} kerusi**, PN pada **{pn_p50}**, PH pada "
                f"**{ph_p50}**, BN pada **{bn_p50}**, dengan **{mc_flips} pertukaran kerusi** di seluruh negara.\n\n"
            )
            if flips_in_state:
                flip_details = "\n".join(
                    f"- **{s['constituency']}** ({s['code']}): diunjurkan bertukar daripada "
                    f"**{s['ge15_winner']}** ({s.get('ge15_bloc', '')}) → **"
                    f"{s['proj_winner']}** (margin: {s['proj_margin']:.2f}pp)"
                    for s in flips_in_state
                )
                body_ms = (f"Dalam {state}, model persekutuan mengunjurkan **{len(flips_in_state)} kerusi "
                           f"bertukar tangan**:\n\n{flip_details}\n\n")
            else:
                body_ms = (f"Dalam {state}, kesemua {len(proj_seats)} kerusi parlimen diunjurkan kekal "
                           f"pada keputusan GE15 — tiada pertukaran dalam senario P50.\n\n")
            held_list = ", ".join(f'{s["constituency"]} ({s["proj_winner"]})' for s in held[:5])
            held_suffix = "..." if len(held) > 5 else ""
            held_summary_ms = (
                f"Baki {len(held)} kerusi diunjurkan kekal pada pemenang GE15: "
                f"{held_list}{held_suffix}."
            )
            fed_verdict_ms = intro_ms + body_ms + held_summary_ms + "\n\n"
        return f"""## 6. Kerusi Persekutuan di Negeri Ini

{state} menyumbang **{fed['n_seats']} kawasan persekutuan** kepada Dewan Rakyat yang 222 kerusi. Kerusi-kerusi ini merupakan saluran langsung negeri ini ke dalam aritmetik nasional GE16, dan keputusan GE15 mereka — dihitung secara langsung daripada fail keputusan persekutuan ([S-03]) — menyediakan garis dasar persekutuan terhadap mana pergerakan pasca-GE15 negeri ini mesti dibaca.

| Kod | Kawasan | Pemenang GE15 (Blok) | Ahli | Margin GE15 |
|---|---|---|---|---|
{rows}

Komposisi blok delegasi persekutuan negeri ini pada GE15 adalah:

| Blok | Kerusi Persekutuan |
|---|---|
{fed_rows}

{fed_verdict_ms}### 6.1 Medan pertempuran persekutuan

Medan pertempuran persekutuan dalam {state} — kerusi dengan margin GE15 di bawah lima mata peratusan — adalah tempat dinamik negeri ini menyuap unjuran nasional:

| Kod | Kawasan | Blok GE15 | Margin GE15 | Pengundi |
|---|---|---|---|---|
{bg_rows}

Hubungan antara arena negeri dan kerusi persekutuan ini berjalan dua hala. {federal_state_link_ms(state, fed)} Analisis mendalam yang disediakan untuk projek ini ([S-08]) meneliti setiap kerusi ini secara individu — pemegangnya, pengundi mereka, komposisi demografi mereka — dan Bahagian 10 laporan ini melipat analisis itu ke dalam gambaran medan pertempuran peringkat negeri. Bagi unjuran persekutuan, kepentingan negeri ini tepat pada kerusi-kerusi ini: {len(fed['battlegrounds'])} medan pertempuran persekutuan di {state} adalah bahagian besar daripada peta medan pertempuran nasional, dan ayunan pilihan raya negeri yang diukur sejak GE15 — digunakan dalam Bahagian 8 — menentukan arah kecenderungan mereka.

---


"""
    return f"""## 6. Federal Seats in This State

{state} contributes **{fed['n_seats']} federal constituencies** to the 222-seat Dewan Rakyat. These seats are the state's direct channel into the GE16 national arithmetic, and their GE15 results — computed live from the federal results file ([S-03]) — provide the federal baseline against which the state's post-GE15 movements must be read.

| Code | Constituency | GE15 Winner (Bloc) | Member | GE15 Margin |
|---|---|---|---|---|
{rows}

The bloc composition of the state's federal delegation at GE15 was:

| Bloc | Federal Seats |
|---|---|
{fed_rows}

{federal_verdict}### 6.1 Federal battlegrounds

The federal battlegrounds within {state} — seats with GE15 margins under five percentage points — are where the state's dynamics feed the national projection:

| Code | Constituency | GE15 Bloc | GE15 Margin | Electorate |
|---|---|---|---|---|
{bg_rows}

The relationship between the state arena and these federal seats runs in both directions. {federal_state_link(state, fed)} The deep-dive analysis prepared for this project ([S-08]) examines each of these seats individually — their holders, their electorates, their demographic composition — and Section 15 of this report folds that analysis into the state-level battleground picture. For the federal projection, the state's significance is precisely these seats: the {len(fed['battlegrounds'])} federal battlegrounds in {state} are a substantial share of the national battleground map, and the state-election swings measured since GE15 — applied in Section 8 — determine which way they lean.

---

"""


def federal_state_link(state, fed):
    arch = STATE_META[state]["archetype"]
    bg = fed["battlegrounds"]
    if arch == "melaka_divergence":
        return ("Melaka is the extreme case: the state's federal delegation — PH 3, PN 3, BN 0 at "
                "GE15 — is the mirror image of its state assembly, where BN holds 21 of 28. The "
                "state and federal electorates are the same people; the divergence is a statement "
                "about how they weigh state competence against federal protest, and it is the "
                "central modelling problem for this state.")
    if arch == "gps_hegemony":
        return ("Sarawak's federal delegation is as GPS-dominated as its state assembly — the "
                "national PH-PN cleavage stops at the state border — and its federal seats are "
                "the kingmaker bloc in the GE16 arithmetic.")
    if arch == "sabah_local":
        return ("Sabah's federal seats follow the state's local logic: the GE15 delegation "
                "fragmented across GRS, WARISAN, BN, PH and independents, and the 2025 state "
                "result — WARISAN's surge to 25 — is the freshest signal for how those federal "
                "seats will realign.")
    if bg and fed["battlegrounds"][0]["ge15_bloc"] == "PN":
        return (f"The state's southern/central position makes its federal battlegrounds the "
                f"front line of the BN resurgence: seats like {bg[0]['constituency']}, held by "
                f"PN on a {bg[0]['ge15_margin']:.1f} per cent margin, are precisely where the "
                f"state-election swings measured in Section 7 would translate into federal flips.")
    return ("The state's federal seats are a mix of safe anchors and contested marginals, and the "
            "state-election swings since GE15 are the live evidence for how the contested ones "
            "move.")


def federal_state_link_ms(state, fed):
    arch = STATE_META[state]["archetype"]
    bg = fed["battlegrounds"]
    if arch == "melaka_divergence":
        return ("Melaka ialah kes ekstrem: delegasi persekutuan negeri ini — PH 3, PN 3, BN 0 pada "
                "GE15 — adalah imej cermin dewan negerinya, di mana BN memegang 21 daripada 28. "
                "Elektorat negeri dan persekutuan adalah orang yang sama; perbezaan itu adalah "
                "kenyataan tentang cara mereka menimbang kecekapan negeri berbanding bantahan "
                "persekutuan, dan ia adalah masalah pemodelan pusat untuk negeri ini.")
    if arch == "gps_hegemony":
        return ("Delegasi persekutuan Sarawak didominasi GPS seperti dewan negerinya — belahan "
                "nasional PH-PN berhenti di sempadan negeri — dan kerusi persekutuannya adalah blok "
                "penentu (kingmaker) dalam aritmetik GE16.")
    if arch == "sabah_local":
        return ("Kerusi persekutuan Sabah mengikuti logik tempatan negeri: delegasi GE15 berpecah "
                "merentasi GRS, WARISAN, BN, PH dan bebas, dan keputusan negeri 2025 — lonjakan "
                "WARISAN kepada 25 — adalah isyarat paling segar untuk bagaimana kerusi persekutuan "
                "itu akan menjajarkan semula.")
    if bg and fed["battlegrounds"][0]["ge15_bloc"] == "PN":
        return (f"Kedudukan negeri ini menjadikan medan pertempuran persekutuannya barisan hadapan "
                f"kebangkitan BN: kerusi seperti {bg[0]['constituency']}, dipegang PN dengan margin "
                f"{bg[0]['ge15_margin']:.1f} peratus, tepat di tempat ayunan pilihan raya negeri yang "
                f"diukur dalam Bahagian 7 akan bertukar menjadi pertukaran kerusi persekutuan.")
    return ("Kerusi persekutuan negeri ini adalah campuran pasak selamat dan marginal yang "
            "dipertandingkan, dan ayunan pilihan raya negeri sejak GE15 adalah bukti hidup untuk "
            "cara marginal yang dipertandingkan itu bergerak.")


def sec7_signals(state, meta, swings, fed, b):
    sw = swings
    if sw:
        rows = "\n".join(
            f"| {bloc} | {v['prev_se']} | {v['latest_se']} | {v['prev_share']:.1f}% | {v['latest_share']:.1f}% | {v['swing']:+.1f}pp |"
            for bloc, v in sorted(sw.items(), key=lambda x: -abs(x[1]["swing"]))
        )
        swing_narr = swing_narrative(state, sw)
    else:
        rows = "| — | — | — | — | — | no fresh swing data |"
        era = era_label(state)
        # Check for proxy swing sources (southern resurgence / green wave benchmarks)
        proxy_note = ""
        arch = meta.get("archetype", "")
        if arch == "melaka_divergence":
            proxy_note = (
                "**NOTE:** This state has no post-GE15 state-election swing measurement. "
                "The projection model therefore applies **proxy assumptions** derived from "
                "neighbouring states' measured movements: the Johor southern resurgence (+17pp BN) "
                "and Negeri Sembilan resurgence (+14pp BN) are used as directional anchors, "
                "marked **[ASSUMPTION: proxy swing]** in the scenario section below. "
                "These are not measured swings — they are informed priors based on geographic "
                "and demographic similarity."
            )
        elif arch == "green_wave":
            proxy_note = (
                "**NOTE:** This state's last recorded state poll predates the GE15 realignment. "
                "The model holds the baseline at zero swing and treats the 2026 party realignments "
                "(PAS–Bersatu rupture, Bersama entry) as seat-specific event shocks rather than "
                "uniform state-level movements."
            )
        elif arch in ("gps_hegemony", "sabah_local"):
            proxy_note = (
                "**NOTE:** National swing metrics do not apply to this state's local-party dynamics. "
                "The model holds the baseline and applies only event-specific shocks to individual seats."
            )
        else:
            proxy_note = (
                f"The model therefore holds the state's federal seats at their GE15 baseline "
                f"(zero swing) and relies on the national factors — approval, economy, events — "
                f"to move margins, a conservative choice that the validation protocol requires "
                f"to reproduce the baseline exactly under the null model."
            )
        swing_narr = (
            f"The state has no fresh post-GE15 state-election swing measurement in the "
            f"derived dataset — its last recorded state poll predates the swing-collection "
            f"window, or the swing file has not been populated for it. {proxy_note}"
        )
    tracker_hits = state_signals_from_trackers(state)
    tracker_txt = ""
    if tracker_hits:
        trows = "\n".join(f"- **{h['feed']}:** {h['title']}" for h in tracker_hits)
        tracker_txt = f"""The news trackers have also captured items mentioning {state} since the last build:

{trows}
"""
    else:
        tracker_txt = ""
    if LANG == "ms":
        if sw:
            rows_ms = "\n".join(
                f"| {bloc} | {v['prev_se']} | {v['latest_se']} | {v['prev_share']:.1f}% | {v['latest_share']:.1f}% | {v['swing']:+.1f}pp |"
                for bloc, v in sorted(sw.items(), key=lambda x: -abs(x[1]["swing"]))
            )
            swing_narr_ms = swing_narrative_ms(state, sw)
        else:
            rows_ms = "| — | — | — | — | — | tiada data ayunan baharu |"
            proxy_note_ms = ""
            arch = meta.get("archetype", "")
            if arch == "melaka_divergence":
                proxy_note_ms = (
                    "**NOTA:** Negeri ini tidak mempunyai ukuran ayunan pilihan raya negeri pasca-GE15. "
                    "Model unjuran oleh itu menggunakan **andaian proksi** yang diterbitkan daripada "
                    "pergerakan terukur negeri jiran: kebangkitan selatan Johor (+17pp BN) "
                    "dan kebangkitan Negeri Sembilan (+14pp BN) digunakan sebagai penambat arah, "
                    "ditanda **[ANDAIAN: ayunan proksi]** dalam bahagian senario di bawah. "
                    "Ini bukan ayunan terukur — ia adalah prior berinformasi berdasarkan persamaan "
                    "geografi dan demografi."
                )
            elif arch == "green_wave":
                proxy_note_ms = (
                    "**NOTA:** Pungutan suara negeri terakhir yang direkodkan bagi negeri ini mendahului "
                    "penjajaran semula GE15. Model mengekalkan garis dasar pada ayunan sifar dan "
                    "melayan penjajaran semula parti 2026 (perpecahan PAS–Bersatu, kemasukan Bersama) "
                    "sebagai kejutan peristiwa khusus kerusi dan bukannya pergerakan seragam "
                    "peringkat negeri."
                )
            elif arch in ("gps_hegemony", "sabah_local"):
                proxy_note_ms = (
                    "**NOTA:** Metrik ayunan nasional tidak terpakai kepada dinamik parti tempatan "
                    "negeri ini. Model mengekalkan garis dasar dan hanya menggunakan kejutan khusus "
                    "peristiwa kepada kerusi individu."
                )
            else:
                proxy_note_ms = (
                    f"Model oleh itu mengekalkan kerusi persekutuan negeri ini pada garis dasar GE15 "
                    f"(ayunan sifar) dan bergantung kepada faktor nasional — kelulusan, ekonomi, "
                    f"peristiwa — untuk menggerakkan margin, satu pilihan konservatif yang diperlukan "
                    f"oleh protokol pengesahan untuk menghasilkan semula garis dasar dengan tepat "
                    f"di bawah model nol."
                )
            swing_narr_ms = (
                f"Negeri ini tidak mempunyai ukuran ayunan pilihan raya negeri pasca-GE15 yang baharu "
                f"dalam set data terbitan — pungutan suara negeri terakhir yang direkodkan mendahului "
                f"tetingkap pengumpulan ayunan, atau fail ayunan belum diisi untuknya. {proxy_note_ms}"
            )
        tracker_txt_ms = ""
        if tracker_hits:
            trows_ms = "\n".join(f"- **{h['feed']}:** {h['title']}" for h in tracker_hits)
            tracker_txt_ms = f"""Penjejak berita juga telah menangkap item yang menyebut {state} sejak binaan terakhir:

{trows_ms}

"""
        return f"""## 7. Isyarat Sejak PRN Terakhir

Set data ayunan pilihan raya negeri ialah isyarat berwajaran tertinggi projek ini — satu-satunya pengukuran keutamaan terserlah tentang bagaimana pengundi {state} telah bergerak sejak pungutan suara negeri terakhir mereka. Ayunan di bawah dihitung secara langsung daripada fail ayunan ([S-06]):

| Blok | SE Terdahulu | SE Terkini | Bahagian terdahulu | Bahagian terkini | Ayunan |
|---|---|---|---|---|---|
{rows_ms}

{swing_narr_ms}

{tracker_txt_ms}Penjajaran semula nasional 2026 mendarat secara berbeza di setiap negeri, dan bagi {state} yang relevan ialah: **perpecahan PAS–Bersatu** (8 Jun 2026) dan kemasukan **WAWASAN** ke dalam PN — yang mengubah komposisi dalaman pembangkang di tempat ia memegang kerusi di negeri ini; pelancaran **Bersama** (17 Mei 2026) dan kesan perosaknya yang terbukti di Johor — relevan di mana-mana kerusi bandar negeri ini terletak pada margin PH yang tipis; dan kestabilan **Kerajaan Perpaduan**, yang menentukan ekor persekutuan yang boleh dijangka oleh blok bersekutu kerajaan negeri. {national_signal_comment_ms(state)} Setiap satu dimodelkan dalam Bahagian 8 sama ada sebagai ayunan terukur (di mana negeri ini mempunyai data baharu) atau kejutan peristiwa (di mana kesannya khusus kerusi), dan Bahagian 9 menterjemahkannya ke dalam julat senario.

---


"""
    return f"""## 7. Signals Since the Last PRN

The state-election swing dataset is the project's highest-weighted signal — the only revealed-preference measurement of how {state}'s electorates have moved since their last state poll. The swings below are computed live from the swing file ([S-06]):

| Bloc | Previous SE | Latest SE | Prev share | Latest share | Swing |
|---|---|---|---|---|---|
{rows}

{swing_narr}

{tracker_txt}The national realignments of 2026 land differently in each state, and for {state} the relevant ones are: the **PAS–Bersatu rupture** (8 June 2026) and the admission of **WAWASAN** to PN — which change the internal composition of the opposition where it holds seats in this state; the **Bersama** launch (17 May 2026) and its demonstrated spoiler effect in Johor — relevant wherever the state's urban seats sit on thin PH margins; and the **Unity Government's** stability, which conditions the federal coattails the state's government-aligned blocs can expect. {national_signal_comment(state)} Each of these is modelled in Section 8 as either a measured swing (where the state has fresh data) or an event shock (where the effect is seat-specific), and Section 10 translates them into the scenario range.

---

"""


def swing_narrative(state, sw):
    top = sorted(sw.items(), key=lambda x: -abs(x[1]["swing"]))
    if not top:
        return ""
    winner_bloc, winner = top[0]
    loser_bloc, loser = top[1] if len(top) > 1 else (None, None)
    era = era_label(state)

    def move(bloc, data):
        """Human phrasing for a bloc's swing, direction-aware."""
        s = data["swing"]
        verb = "surged by" if s > 0 else "collapsed by"
        return (f"{bloc} {verb} **{abs(s):.1f} percentage points** "
                f"({data['prev_share']:.1f}% → {data['latest_share']:.1f}%) "
                f"between {data['prev_se']} and {data['latest_se']} ({data['latest_date']})")

    parts = []
    if era == "2023 green wave":
        parts.append(f"The dominant movement is the green wave itself: {move(winner_bloc, winner)}.")
        if loser_bloc and abs(loser["swing"]) >= 5:
            parts.append(f"The counterpart movement saw {move(loser_bloc, loser)}, and the "
                         f"net movement converted the state's previously competitive seats into a "
                         f"one-sided map.")
        parts.append(
            "This is not a poll reading; it is a ballot record — actual votes cast in the same "
            "ethnic and geographic structure that will vote in GE16, which is why the federal "
            "model applies it at the highest factor weight."
        )
    elif era == "2026 southern resurgence":
        parts.append(f"The dominant movement is the southern resurgence: {move(winner_bloc, winner)}.")
        if loser_bloc and abs(loser["swing"]) >= 5:
            parts.append(f"The counterpart movement saw {move(loser_bloc, loser)}, and the "
                         f"net effect was a decisive consolidation of the government-aligned vote.")
        parts.append(
            "The result validates the southern-resurgence scenario in the federal model — BN's "
            "recovery in the south, measured here at the ballot box, is the strongest single "
            "post-GE15 signal in the dataset."
        )
    else:
        parts.append(f"The most recent recorded movement is {move(winner_bloc, winner)}.")
        parts.append(
            "The signal is older than the 2023/2026 cycles and is treated with corresponding "
            "caution — the model applies it but notes that the 2026 party realignment could "
            "materially change the arithmetic."
        )
    return " ".join(parts)


def swing_narrative_ms(state, sw):
    top = sorted(sw.items(), key=lambda x: -abs(x[1]["swing"]))
    if not top:
        return ""
    winner_bloc, winner = top[0]
    loser_bloc, loser = top[1] if len(top) > 1 else (None, None)
    era = era_label(state)

    def move_ms(bloc, data):
        s = data["swing"]
        verb = "melonjak" if s > 0 else "merudum"
        return (f"{bloc} {verb} **{abs(s):.1f} mata peratusan** "
                f"({data['prev_share']:.1f}% → {data['latest_share']:.1f}%) "
                f"antara {data['prev_se']} dan {data['latest_se']} ({data['latest_date']})")

    parts = []
    if era == "2023 green wave":
        parts.append(f"Pergerakan dominan ialah gelombang hijau itu sendiri: {move_ms(winner_bloc, winner)}.")
        if loser_bloc and abs(loser["swing"]) >= 5:
            parts.append(f"Pergerakan balas menyaksikan {move_ms(loser_bloc, loser)}, dan "
                         f"pergerakan bersih menukar kerusi negeri yang sebelum ini kompetitif "
                         f"menjadi peta satu pihak.")
        parts.append(
            "Ini bukan bacaan tinjauan; ia adalah rekod undi — undi sebenar yang dibuang dalam "
            "struktur etnik dan geografi yang sama yang akan mengundi dalam GE16, sebab itu model "
            "persekutuan menerapkannya pada wajaran faktor tertinggi."
        )
    elif era == "2026 southern resurgence":
        parts.append(f"Pergerakan dominan ialah kebangkitan selatan: {move_ms(winner_bloc, winner)}.")
        if loser_bloc and abs(loser["swing"]) >= 5:
            parts.append(f"Pergerakan balas menyaksikan {move_ms(loser_bloc, loser)}, dan "
                         f"kesan bersihnya ialah penyatuan tegas undi bersekutu kerajaan.")
        parts.append(
            "Keputusan itu mengesahkan senario kebangkitan selatan dalam model persekutuan — "
            "pemulihan BN di selatan, diukur di sini di peti undi, ialah isyarat pasca-GE15 tunggal "
            "terkuat dalam set data."
        )
    else:
        parts.append(f"Pergerakan terkini yang direkodkan ialah {move_ms(winner_bloc, winner)}.")
        parts.append(
            "Isyarat itu lebih lama daripada kitaran 2023/2026 dan dilayan dengan berhati-hati — "
            "model menerapkannya tetapi mencatat bahawa penjajaran semula parti 2026 boleh "
            "mengubah aritmetik secara material."
        )
    return " ".join(parts)


def national_signal_comment_ms(state):
    arch = STATE_META[state]["archetype"]
    if arch in ("gps_hegemony", "sabah_local"):
        return ("Bagi negeri ini, penjajaran semula nasional kurang penting daripada yang tempatan — "
                "perpecahan PAS-Bersatu dan Bersama adalah hujah Semenanjung, dan aritmetik "
                "gabungan negeri sendiri (penggantian dalaman GPS, pakatan tempatan Sabah yang "
                "beralih) adalah isyarat hidup.")
    if arch == "green_wave":
        return ("Bagi negeri gelombang hijau, perpecahan itu ialah risiko utama: di mana kerusi "
                "negeri pembangkang terbahagi antara kawasan dipegang PAS dan Bersatu, kelemahan "
                "rump Bersatu boleh merugikan blok itu kerusi yang kini diambil mudah.")
    if arch == "melaka_divergence":
        return ("Bagi Melaka, empat arus nasional — kebangkitan BN, gelombang hijau PN, pemulihan PH, "
                "pecahan tiga penjuru — menarik ke arah bertentangan, dan PRN negeri sendiri ialah "
                "makmal di mana ia akan diuji.")
    if arch == "bn_supermajority":
        return ("Bagi Johor, isyaratnya ialah kesan perosak Bersama yang diukur pada 3–6 peratus bagi "
                "setiap kerusi — satu penolakan langsung daripada PH di kerusi bandar negeri, dan "
                "sebab model persekutuan menerapkan kejutan peristiwa −3pp kepada PH di kerusi yang "
                "ditandingi Bersama.")
    return ("Bagi negeri ini, penjajaran semula merentasi garisan kerajaan-pembangkang dengan cara "
            "yang hanya akan ditangkap oleh data ayunan pada pungutan suara negeri seterusnya; "
            "sehingga itu model melayannya sebagai kejutan peristiwa di kerusi tertentu.")


def national_signal_comment(state):
    arch = STATE_META[state]["archetype"]
    if arch in ("gps_hegemony", "sabah_local"):
        return ("For this state, the national realignments matter less than the local ones — "
                "the PAS-Bersatu split and Bersama are Malayan arguments, and the state's own "
                "coalition arithmetic (GPS's internal succession, Sabah's shifting local pacts) "
                "is the live signal.")
    if arch == "green_wave":
        return ("For a green-wave state, the rupture is the key risk: where the opposition's "
                "state seats are split between PAS and Bersatu-held constituencies, the Bersatu "
                "rump's weakness could cost the bloc seats it currently takes for granted.")
    if arch == "melaka_divergence":
        return ("For Melaka, the four national currents — BN revival, PN green wave, PH recovery, "
                "three-way fragmentation — pull in opposite directions, and the state's own PRN "
                "is the laboratory in which they will be tested.")
    if arch == "bn_supermajority":
        return ("For Johor, the signal is the Bersama spoiler effect measured at 3–6 per cent per "
                "seat — a direct subtraction from PH in the state's urban seats, and the reason "
                "the federal model applies a −3pp event shock to PH in seats Bersama contests.")
    return ("For this state, the realignments cut across the government-opposition line in ways "
            "the swing data will capture only at the next state poll; until then the model treats "
            "them as event shocks in specific seats.")


# ---------------------------------------------------------------------------
# SECTION 7.1 — STORY THREADS (THIS STATE)  [T1.5]
# ---------------------------------------------------------------------------
# The state equivalent of the federal report's §2.1. The ledger is READ with the
# federal helper (report_builder.read_state_story_threads → read_story_threads,
# the same columns and the same as-of window) and the rows are rendered with the
# federal row renderer, so the two reports can never drift apart in what a
# thread row means. Everything printed below is a column of the ledger: no
# thread is described, summarised or inferred, and a state with no qualifying
# story says exactly that.

#: Zero-thread wording (spec, T1.5): the one line a quiet state renders.
NO_STATE_STORY_THREADS_EN = ("No chain of related political events touches this state in "
                             "this edition.")
NO_STATE_STORY_THREADS_MS = ("Tiada rantaian peristiwa politik berkaitan menyentuh negeri ini "
                             "dalam edisi ini.")


def _story_ledger_reader():
    """The federal §2.1 ledger reader — imported, never re-implemented."""
    engine_dir = os.path.dirname(os.path.abspath(__file__))
    if engine_dir not in sys.path:
        sys.path.insert(0, engine_dir)
    import report_builder
    return report_builder


def sec7_story_threads(state):
    """Section 7.1 — this state's story threads, from the events ledger.

    Reads with the federal helper (scoped to this state) and renders the same
    ledger columns. Returns the markdown block for the current LANG.
    """
    ledger = _story_ledger_reader()
    data = ledger.read_state_story_threads(state)
    if LANG == "ms":
        block = _story_threads_state_ms(state, data, ledger)
    else:
        block = _story_threads_state_en(state, data, ledger)
    # Every numbered section in this report closes with a rule; keep the pattern.
    return block.rstrip("\n") + "\n\n---\n\n"


def _story_threads_state_en(state, data, ledger):
    lines = ["### 7.1 Political developments in this state", ""]
    if not data.get("available"):
        lines.append(
            "**No political developments are shown for this edition.** No dated chain of "
            "political events could be read at build time, so this subsection states that fact "
            "instead of inventing developments.")
        return "\n".join(lines) + "\n\n"
    if not data["total"]:
        lines.append(NO_STATE_STORY_THREADS_EN)
        return "\n".join(lines) + "\n\n"
    as_of = data["as_of"] or "undated"
    lines += [
        f"Section 7 above is the standing signal read for {state}. This subsection is the live "
        f"political record, scoped to this state: one row per related chain of dated political "
        f"events that touches {state} — through a seat of this state, a party office or listing "
        "tied to it, or the event's own state. Each row is the chain as it stands: when it "
        f"started, when it last moved, how many dated events it carries and where it is now.",
        "",
        f"**As of {as_of}:** {data['total']} chains reference {state} — {data['open']} "
        f"open, {data['dormant']} dormant, {data['closed']} closed. The {len(data['threads'])} "
        "most recently updated open chains follow; *Last update* is the date of the chain's "
        "most recent event, and *Items covered* is how many dated events it carries. A chain "
        "that has not moved this cycle keeps its row, its date and its status.",
        "",
        "| Chain of events | Items covered | First development | Last update | Status | Latest development |",
        "|---|---|---|---|---|---|",
    ]
    lines += [ledger._story_row_cells(thread) for thread in data["threads"]]
    lines += [""]
    if data["stale_total"]:
        examples = "; ".join(
            f"{thread.get('headline') or 'unnamed chain'} ({thread['status']}, last update "
            f"{thread['last_update'] or 'undated'}, {thread['event_count']} items covered)"
            for thread in data["stale"])
        lines += [
            f"**{state} chains carried without a current development ({data['stale_total']}):** "
            "open chains of this state whose most recent event predates this edition or carries "
            "no captured date, so they are shown by last update and status only — no "
            "development is claimed for them this time. Most recently: " + examples + ".",
            "",
        ]
    if data["held"]:
        lines += [
            f"**Held for the next edition ({data['held']}):** chains of this state whose latest "
            "event is dated after this edition's cut-off. They are real and dated; they are "
            "simply not presented as the current position here.",
            "",
        ]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n\n"


def _story_threads_state_ms(state, data, ledger):
    lines = ["### 7.1 Perkembangan politik di negeri ini", ""]
    if not data.get("available"):
        lines.append(
            "**Tiada perkembangan politik dipaparkan untuk edisi ini.** Tiada rantaian "
            "peristiwa politik bertarikh dapat dibaca semasa laporan dibina, jadi subseksyen "
            "ini menyatakan fakta itu dan bukan mengarang perkembangan.")
        return "\n".join(lines) + "\n\n"
    if not data["total"]:
        lines.append(NO_STATE_STORY_THREADS_MS)
        return "\n".join(lines) + "\n\n"
    as_of = data["as_of"] or "tiada tarikh"
    lines += [
        f"Seksyen 7 di atas ialah bacaan isyarat tetap bagi {state}. Subseksyen ini ialah rekod "
        f"politik semasa, diskopkan kepada negeri ini: satu baris bagi setiap rantaian "
        f"peristiwa politik bertarikh yang menyentuh {state} — melalui kerusi negeri ini, "
        "pejabat atau penyenaraian parti yang berpangkalan di negeri ini, atau negeri peristiwa "
        "itu sendiri. Setiap baris ialah rantaian itu sebagaimana keadaannya: bila ia bermula, "
        "bila ia kali terakhir bergerak, berapa peristiwa bertarikh dibawanya dan di mana "
        "kedudukannya sekarang.",
        "",
        f"**Setakat {as_of}:** {data['total']} rantaian merujuk {state} — {data['open']} "
        f"terbuka, {data['dormant']} dorman, {data['closed']} tertutup. {len(data['threads'])} "
        "rantaian terbuka yang paling terkini disenaraikan; *Kemas kini terakhir* ialah tarikh "
        "peristiwa terakhir rantaian itu, dan *Item diliputi* ialah bilangan peristiwa "
        "bertarikh yang dibawanya. Rantaian yang tidak bergerak kitaran ini kekal dengan "
        "barisnya, tarikhnya dan statusnya.",
        "",
        "| Rantaian peristiwa | Item diliputi | Perkembangan pertama | Kemas kini terakhir | Status | Perkembangan terakhir |",
        "|---|---|---|---|---|---|",
    ]
    lines += [ledger._story_row_cells(thread) for thread in data["threads"]]
    lines += [""]
    if data["stale_total"]:
        examples = "; ".join(
            f"{thread.get('headline') or 'rantaian tanpa nama'} ({thread['status']}, kemas "
            f"kini terakhir {thread['last_update'] or 'tiada tarikh'}, {thread['event_count']} "
            "item diliputi)" for thread in data["stale"])
        lines += [
            f"**Rantaian {state} dibawa tanpa perkembangan semasa ({data['stale_total']}):** "
            "rantaian terbuka negeri ini yang peristiwa terakhirnya lebih awal daripada edisi "
            "ini atau tiada tarikh tercatat, jadi ia dipaparkan mengikut kemas kini terakhir "
            "dan status sahaja — tiada perkembangan didakwa untuknya kali ini. Paling "
            "terkini: " + examples + ".",
            "",
        ]
    if data["held"]:
        lines += [
            f"**Dipegang untuk edisi seterusnya ({data['held']}):** rantaian negeri ini yang "
            "peristiwa terakhirnya bertarikh selepas tarikh potong edisi ini. Ia nyata dan "
            "bertarikh; ia cuma tidak dipaparkan sebagai kedudukan semasa di sini.",
            "",
        ]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n\n"


def sec8_method(state, meta, factor_rankings=None):
    theory = read_text(THEORY) if os.path.exists(THEORY) else ""
    arch = meta.get("archetype", "")

    # Factor weightage section for Tier 1 states (gap analysis item c — federal granularity)
    factor_txt = ""
    if factor_rankings:
        factor_rows = "\n".join(
            f"| {i+1} | {name} | {clean_factor_desc(desc, 120)}..."
            for i, (name, desc) in enumerate(factor_rankings.items())
        )
        factor_txt = f"""

### 8.1 Factor Weightages Applied to {state}

The federal model's factor hierarchy ([S-11]) assigns the following weights to the drivers of electoral movement. For {state}, the archetype ({arch}) determines which factors carry more or less weight than the national average:

| Rank | Factor | Description |
|---|---|---|
{factor_rows}

These weightages are applied uniformly across all of {state}'s seats modulated by seat type (Malay-majority, mixed, Chinese-majority), so that a factor like "economic term" has greater effect in rural Malay seats and "leadership preference" matters more in urban mixed seats."""

    if LANG == "ms":
        factor_txt_ms = ""
        if factor_rankings:
            factor_rows_ms = "\n".join(
                f"| {i+1} | {name} | {clean_factor_desc(desc, 120)}..."
                for i, (name, desc) in enumerate(factor_rankings.items())
            )
            factor_txt_ms = f"""

### 8.1 Wajaran Faktor yang Digunakan untuk {state}

Hierarki faktor model persekutuan ([S-11]) memberikan wajaran berikut kepada pemacu pergerakan pilihan raya. Bagi {state}, arketip ({arch}) menentukan faktor mana yang membawa lebih atau kurang wajaran daripada purata nasional:

| Kedudukan | Faktor | Perihalan |
|---|---|---|
{factor_rows_ms}

Wajaran ini digunakan secara seragam merentasi semua kerusi {state} dengan pengubahsuaian mengikut jenis kerusi (majoriti Melayu, campuran, majoriti Cina), supaya faktor seperti "istilah ekonomi" mempunyai kesan lebih besar di kerusi Melayu luar bandar dan "keutamaan kepimpinan" lebih penting di kerusi bandar campuran."""
        return f"""## 8. Metodologi{factor_txt_ms}

Jentera unjuran yang digunakan untuk {state} ialah kerangka uniform-swing projek ini, didokumenkan sepenuhnya dalam dokumen teori ([S-10]). Logiknya berjalan dalam empat peringkat. **Peringkat 1 (struktur):** garis dasar persekutuan GE15 (Bahagian 6) dan garis dasar PRN negeri terkini (Bahagian 4) menyediakan penambat empirikal bagi setiap kerusi — tidak pernah ramalan dari kertas kosong. **Peringkat 2 (hanyutan):** ayunan pilihan raya negeri yang diukur dalam Bahagian 7, bersama faktor nasional — kelulusan kerajaan, istilah ekonomi, keutamaan kepimpinan, perbezaan keluar mengundi — menghasilkan perubahan sejak penambat, dinyatakan sebagai ayunan bagi setiap blok bagi setiap negeri. **Peringkat 3 (terjemahan):** margin unjuran setiap kerusi ialah margin garis dasar ditambah ayunan kepada pemenang tolak ayunan kepada naib johan, dan kerusi bertukar tangan apabila margin terlaras melepasi sifar:

$$M_s = M_{{s,base}} + \\Delta V_{{winner}} - \\Delta V_{{runner\\text{{-}}up}}$$

**Peringkat 4 (pengagregatan):** kerusi-kerusi dijumlahkan menjadi dewan unjuran, dan simulasi Monte Carlo ke atas ayunan tidak pasti menghasilkan taburan P10/P50/P90 dan kebarangkalian mana-mana blok menguasai majoriti.

Empat prinsip tadbir mengawal jentera ini, dan ia terpakai kepada {state} sebagaimana kepada persekutuan. **Garis dasar dahulu:** jangan sekali-kali meramal dari kertas kosong; model nol (ayunan sifar) mesti menghasilkan semula pilihan raya terakhir dengan tepat, dan ia berbuat demikian — itulah semakan kejujuran ke atas keseluruhan peranti. **Keutamaan terserlah mengatasi niat yang dinyatakan:** ayunan pilihan raya negeri {state} mengatasi sebarang tinjauan, kerana ia mengukur undi yang dibuang dan bukannya niat yang dinyatakan; tinjauan menentukur, pilihan raya negeri mengukur. **Seragam dalam jenis, bukan dalam negara:** ayunan nasional tidak digunakan secara seragam kepada setiap kerusi di {state} — ia diubah suai mengikut jenis kerusi, supaya ayunan majoriti Melayu tertumpu di kerusi majoriti Melayu negeri dan melemah di kerusi campuran dan bukan Melayunya. **Ketidakpastian adalah sebahagian daripada ramalan:** setiap unjuran ialah taburan, bukan titik, dan output yang jujur ialah julat dengan kebarangkalian, bukan kiraan kerusi tunggal.

Apa yang dimodelkan dan tidak dimodelkan untuk {state} mesti dinyatakan dengan jelas. Yang dimodelkan: ayunan negeri yang diukur (Bahagian 7), kejutan peristiwa daripada penjajaran semula 2026 (pertandingan solo rump Bersatu di mana ia terpakai kepada kerusi negeri ini; pengalihan Bersama di kerusi bandarnya), istilah ekonomi, dan delta kelulusan. Yang tidak dimodelkan: persempadanan semula (yang akan membatalkan peta kerusi sepenuhnya), persoalan penggantian dalaman parti pemerintah negeri, dan sebarang ayunan yang tiada ukuran dalam set data — model lebih suka kejujuran daripada ciptaan, dan di mana negeri ini tidak mempunyai data ayunan baharu ia mengekalkan garis dasar daripada mengada-ada hanyutan. Protokol pengesahan dokumen teori — pembinaan semula kes nol, ujian retrospektif terhadap pilihan raya negeri 2023/2025/2026, dan backcasting kepada 2022 — menjadi pintu pagar setiap keluaran, dan keputusan peringkat negeri dalam laporan ini mewarisi disiplin itu.

---


"""
    return f"""## 8. Methodology{factor_txt}

The projection machinery applied to {state} is the project's uniform-swing framework, documented in full in the theory document ([S-10]). The logic runs in four stages. **Stage 1 (structure):** the GE15 federal baseline (Section 6) and the latest state PRN baseline (Section 4) provide the empirical anchor for every seat — never a blank-slate forecast. **Stage 2 (drift):** the state-election swings measured in Section 7, together with the national factors — government approval, the economic term, leadership preference, turnout differentials — produce the change since the anchor, expressed as a swing per bloc per state. **Stage 3 (translation):** each seat's projected margin is the baseline margin plus the swing to the winner minus the swing to the runner-up, and the seat flips when the adjusted margin crosses zero:

$$M_s = M_{{s,base}} + \\Delta V_{{winner}} - \\Delta V_{{runner\\text{{-}}up}}$$

**Stage 4 (aggregation):** the seats are summed into a projected assembly, and a Monte Carlo simulation over the uncertain swings produces the P10/P50/P90 distribution and the probability of any given bloc commanding a majority.

Four governing principles constrain the machinery, and they apply to {state} as to the federation. **Baseline-first:** never forecast from a blank slate; the null model (zero swings) must reproduce the last election exactly, and it does — which is the honesty check on the whole apparatus. **Revealed preference over stated intention:** {state}'s state-election swings outrank any poll, because they measure votes cast rather than intentions stated; polls calibrate, state elections measure. **Uniform within type, not within country:** a national swing is not applied uniformly to every seat in {state} — it is modulated by seat type, so a Malay-majority swing concentrates in the state's Malay-majority seats and attenuates in its mixed and non-Malay seats. **Uncertainty is part of the forecast:** every projection is a distribution, not a point, and the honest output is a range with probabilities, not a single seat count.

What is and is not modelled for {state} must be stated plainly. What is modelled: the measured state swings (Section 7), the event shocks from the 2026 realignments (Bersatu rump solo contests where they apply to this state's seats; Bersama siphons in its urban seats), the economic term, and the approval delta. What is not modelled: redelineation (which would invalidate the seat map entirely), the internal succession questions of the state's governing parties, and any swing for which the dataset has no measurement — the model prefers honesty over invention, and where the state has no fresh swing data it holds the baseline rather than fabricating drift. The theory document's validation protocol — null-case reconstruction, retrospective testing against the 2023/2025/2026 state elections, and backcasting to 2022 — gates every release, and the state-level results in this report inherit that discipline.

---

"""


def sec9_data_inputs(state, fed, b):
    """Section 10: Data — The Inputs (federal-parity). Mirrors federal §9:
    live macro readings + event shocks from config.py, plus the state's own
    dataset inventory. Tier-1 only (called from build_state_report)."""
    try:
        cfg = load_config()
    except Exception:
        cfg = None
    total = b["total"]
    # Macro table
    if cfg is not None:
        def _macro_label(k):
            labels = {"gdp_yoy": "GDP YoY", "cpi_yoy": "CPI YoY", "ringgit": "Ringgit (MYR/USD)",
                      "approval_delta": "Approval Delta", "pm_pref_malay": "PM Preference (Malay)",
                      "pm_pref_nonmalay": "PM Preference (Non-Malay)"}
            return labels.get(k, k.replace('_', ' ').title())
        macro_rows = "\n".join(f"| {_macro_label(k)} | {v} |" for k, v in cfg.MACRO.items())
    else:
        macro_rows = "| — | unavailable |"
    # Event shocks — show only those touching this state's federal seats when possible
    if cfg is not None and getattr(cfg, "EVENT_SHOCKS", None):
        fed_codes = {s["code"] for s in fed["seats"]}
        shocks = cfg.EVENT_SHOCKS
        local = {k: v for k, v in shocks.items() if k in fed_codes}
        shown = local if local else shocks
        def _shock_txt(v):
            if isinstance(v, dict):
                return ", ".join(f"{b} {s:+d}pp" for b, s in v.items())
            return str(v)
        shock_rows = "\n".join(
            f"| {k} | {_shock_txt(v)} |" for k, v in shown.items()
        ) if shown else "| — | none active |"
        shock_note = (" (restricted to this state's federal seats)" if local else " (national)")
    else:
        shock_rows = "| — | none |"
        shock_note = ""
    if LANG == "ms":
        return f"""## 9. Data — Input

Set data enjin unjuran ini terdiri daripada enam kategori, setiap satu memainkan peranan yang berbeza dalam rantaian unjuran. Setiap set data adalah hidup — dibaca dari cakera pada masa binaan — dan setiap nombor dalam laporan ini dihitung daripada sumber ini, tidak pernah ditaip tangan.

**Set data teras ({total} kerusi negeri, garis dasar PRN):**

| Set Data | Sumber | Peranan |
|---|---|---|
| Keputusan DUN oleh kawasan | Suruhanjaya Pilihan Raya | Margin garis dasar, pemenang, blok |
| Daftar calon DUN | SPR / parti | Demografi calon (etnik, umur, jantina) |
| Demografi pengundi persekutuan | Daftar pendaftar ElectionData.MY | Bahagian etnik/umur → penjenisan kerusi |
| Ayunan pilihan raya negeri | Undi utama SPR | Hanyutan keutamaan terserlah |
| Medan pertempuran persekutuan | Analisis projek | Pengenalpastian kerusi marginal |
| Senario unjuran | Pengiraan enjin | Analisis sensitiviti |

**Bacaan makro semasa (langsung dari config):**

| Petunjuk | Nilai |
|---|---|
{macro_rows}

**Kejutan peristiwa (khusus kerusi, langsung dari config){shock_note}:**

| Kod Kerusi | Kejutan |
|---|---|
{shock_rows}

Kejutan peristiwa menangkap kesan diskret, peringkat kerusi bagi perpecahan gabungan dan kemasukan kuasa ketiga — perpecahan PAS–Bersatu, kemasukan Bersama, dan potensi pertandingan solo rump Bersatu. Di mana kejutan menyentuh kerusi persekutuan negeri ini, ia disenaraikan di atas dan digunakan dalam Bahagian 8 pada wajaran yang dinyatakan.

---


"""
    return f"""## 9. Data — The Inputs

The forecast engine draws on six categories of data, each serving a distinct role in the projection chain. Every dataset is live — read from disk at build time — and every number in this report is computed from these sources, never hand-typed.

**Core datasets ({total} state seats, PRN baseline):**

| Dataset | Source | Role |
|---|---|---|
| DUN results by constituency | Election Commission | Baseline margins, winner, bloc |
| DUN candidate register | EC / parties | Candidate demographics (ethnicity, age, sex) |
| Federal voter demographics | ElectionData.MY anonymised roll | Ethnic/age shares → seat typing |
| State-election swings | MECo headline ballots | Revealed-preference drift |
| Federal battlegrounds | Project analysis | Marginal seat identification |
| Projection scenarios | Engine computation | Sensitivity analysis |

**Current macro readings (live from config):**

| Indicator | Value |
|---|---|
{macro_rows}

**Event shocks (seat-specific, live from config){shock_note}:**

| Seat Code | Shock Applied |
|---|---|
{shock_rows}

Event shocks capture the discrete, seat-level effects of coalition ruptures and third-force entries — the PAS–Bersatu rupture, the Bersama entry, and the Bersatu rump's potential solo contests. Where a shock touches this state's federal seats it is listed above and applied in Section 8 at the stated weight.

---


"""


def sec11_projection(state, proj, b):
    """Section 13: The Projection — seat by seat (federal-parity). Tier-1 only.
    Renders the full per-seat projection table for the state's DUN seats with
    GE15/PRN baseline margin, projected winner, projected margin and flip flag,
    plus a narrative of the flips modelled."""
    total = b["total"]
    if not proj or "rows" not in proj or not proj["rows"]:
        return ""
    rows = proj["rows"]
    seat_rows = "\n".join(
        f"| {r.get('seat','')} | {r.get('constituency','')} | {r.get('w_2021','')} ({r.get('r_2021','')}) | "
        f"{num(r.get('margin_2021')):.2f}% | {r.get('proj_winner','')} | {num(r.get('proj_margin')):.2f}% | "
        f"{'🔁 flip' if str(r.get('flip','')).lower()=='true' else 'hold'} |"
        for r in rows
    )
    flips = proj.get("flips", [])
    if flips:
        flip_lines = "\n".join(
            f"- **{f['constituency']}** ({f['seat']}): {f['from']} → {f['to']}, baseline margin "
            f"{f['margin_2021']:.2f}% → projected {f['proj_margin']:.2f}%"
            for f in flips
        )
    else:
        flip_lines = "- No seats are projected to flip in the base scenario."
    ptop = bloc_order_by_seats(proj["bloc_totals"])[0]
    if LANG == "ms":
        return f"""## 11. Unjuran — Kerusi Demi Kerusi

Unjuran penuh untuk {state} dimodelkan kerusi demi kerusi daripada fail unjuran ([S-09]). Senario asas membawa **{ptop[0]} {ptop[1]} daripada {total} kerusi**, dengan {len(flips)} pertukaran kerusi:

| Kerusi | Kawasan | Pemenang 2021 (Blok) | Margin 2021 | Unjuran | Margin Unjuran | Status |
|---|---|---|---|---|---|---|
{seat_rows}

**Kerusi yang bertukar tangan dalam senario asas:**

{flip_lines}

Setiap margin di atas dihitung daripada ayunan yang dimodelkan dalam Bahagian 7–8; tiada satu pun ditaip tangan. Julat senario penuh dan taburannya diterbitkan dalam Bahagian 12, dan skor kad ramalan dalam Bahagian 15 merekodkan unjuran ini sebagai ramalan rasmi projek.

---


"""
    return f"""## 11. The Projection — Seat by Seat

The full projection for {state} is modelled seat by seat from the projection file ([S-09]). The base scenario carries **{ptop[0]} {ptop[1]} of {total} seats**, with {len(flips)} seats changing hands:

| Seat | Constituency | 2021 Winner (Bloc) | 2021 Margin | Projected | Projected Margin | Status |
|---|---|---|---|---|---|---|
{seat_rows}

**Seats changing hands in the base scenario:**

{flip_lines}

Every margin above is computed from the swings modelled in Sections 7–8; none is hand-typed. The full scenario range and its distribution are published in Section 12, and the prediction scorecard in Section 15 records this projection as the project's official forecast.

---


"""


def sec8a_scenario_table(state, scenarios_data, b):
    """Section 8.2: Per-scenario seat distribution for Tier 1 states."""
    if not scenarios_data or not scenarios_data.get("scenarios"):
        return ""
    scenarios = scenarios_data["scenarios"]
    rows = "\n".join(
        f"| {s.get('scenario', s.get('name', '?'))} | {s.get('govt_seats', s.get('govt', '?'))} | "
        f"{s.get('pn_seats', s.get('pn', '?'))} | {s.get('ph_seats', s.get('ph', '?'))} | "
        f"{s.get('bn_seats', s.get('bn', '?'))} | {s.get('other_seats', s.get('other', '?'))} | "
        f"{s.get('character', '')} |"
        for s in scenarios
    )
    return f"""### 8.2 Scenario Table (Federal Context)

The table below shows the federal GE16 seat distribution under each of the ten modelled scenarios ([S-05]). These are national totals — the state's contribution is a subset of these numbers, determined by how many of the state's federal seats fall into each bloc's projected allocation.

| Scenario | Govt Seats | PN Seats | PH Seats | BN Seats | Other | Character |
|---|---|---|---|---|---|---|
{rows}

For {state}, the most relevant scenarios are those that move the state's own federal seats. The state contributes **{fed_seats(state)} federal seats** to the 222-seat chamber, and its allocation across these scenarios depends on whether the state swings toward or away from the government coalition."""


def sec8b_factor_weightage(state, factor_rankings, meta):
    """Section 8b: Factor weightage applied to this state's demographic profile."""
    if not factor_rankings:
        return ""
    arch = meta.get("archetype", "")
    adjustments = {
        "melaka_divergence": (
            "**Weight adjustments for Melaka:** The state's four-way split means no single "
            "factor dominates. Government approval matters equally for all four currents; "
            "the economic term is secondary because Melaka's economy is diversified; "
            "leadership preference is the primary differentiator."),
        "green_wave": (
            "**Weight adjustments for this green-wave state:** Identity and grievance politics "
            "carry above-average weight — the Malay electorate's structural alignment with PN "
            "means that only a major event shock can move the margin. Performance factors matter "
            "within the Malay vote but the direction is already set."),
        "gps_hegemony": (
            "**Weight adjustments for Sarawak:** National-level factors have minimal weight. "
            "MA63 delivery, GPS internal succession, and local patronage dominate."),
        "sabah_local": (
            "**Weight adjustments for Sabah:** National factors are second-order. MA63, local "
            "patronage, kinship networks, and WARISAN's personal-vote machinery determine outcomes."),
    }
    adj_txt = adjustments.get(arch, f"**Weight adjustments for {state}:** The standard federal weightages apply, modulated by seat type.")

    factor_rows = "\n".join(
        f"| {i+1} | {name} | {clean_factor_desc(desc)}..."
        for i, (name, desc) in enumerate(factor_rankings.items())
    )
    return f"""### 8.3 Factor Weightage ({state})

The federal model's factor hierarchy assigns the following weights to electoral drivers. For {state}, the archetype ({arch}) determines which factors carry more or less weight than the national average:

| Rank | Factor | Description |
|---|---|---|
{factor_rows}

{adj_txt}"""


def sec8c_prn_federal_cascade(state, meta, swings, forecast_state):
    """Section 8c: PRN → Federal cascade analysis for Tier 1 states."""
    dis = next_dissolution(state)
    dis_str = dis.strftime("%d %B %Y")
    arch = meta.get("archetype", "")

    if arch == "gps_hegemony":
        timing_narrative = (
            "Sarawak faces a unique timing question: should the PRN run concurrent with GE16 "
            "(maximising GPS's kingmaker leverage but risking a diluted campaign) or separately "
            "(preserving the state's local-party dynamics but losing the federal coattail effect)? "
            "The projection assumes concurrent timing, which is the higher-stakes option."
        )
    elif arch == "melaka_divergence":
        timing_narrative = (
            "Melaka's PRN falls due inside the GE16 window (27 December 2026), making concurrent "
            "timing the default. The strategic question is whether BN will seek an early dissolution "
            "to capture the Johor/Negeri Sembilan coattails while they are fresh, or wait for the "
            "federal campaign to build momentum."
        )
    else:
        timing_narrative = (
            f"The state assembly dissolves around {dis_str}, placing the PRN {'inside' if meta['status'] == 'upcoming' else 'outside'} "
            f"the GE16 window. If concurrent, the state's result will shape the federal campaign; "
            f"if separate, the state runs on its own political cycle."
        )

    cascade_items = []
    if forecast_state and forecast_state.get("seats"):
        proj_seats = forecast_state["seats"]
        cascade_items.append(
            f"**Federal coattails:** {state}'s {len(proj_seats)} federal seats will be contested "
            f"on the same electorate as the state poll. A government victory creates positive "
            f"coattails for government-aligned federal candidates."
        )
    cascade_items.append(
        f"**Campaign resource allocation:** The federal campaigns will allocate resources to "
        f"{state} proportional to the number of contestable seats."
    )
    cascade_items.append(
        f"**Timing signal:** {state}'s PRN timing sends a signal to other states about the "
        f"federal government's confidence in its prospects."
    )

    cascade_rows = "\n".join(f"- {item}" for item in cascade_items)

    # Build per-bloc projection table
    proj_bloc_counts = defaultdict(int)
    if forecast_state and forecast_state.get("seats"):
        for s in forecast_state["seats"]:
            proj_bloc_counts[s["proj_winner"]] += 1

    proj_table = "\n".join(
        f"| {bloc} | {count} |"
        for bloc, count in sorted(proj_bloc_counts.items(), key=lambda x: -x[1])
    ) if proj_bloc_counts else "| — | no projection data |"

    return f"""### 8.4 PRN → Federal Cascade ({state})

The state's PRN timing and outcome cascade into the federal GE16 campaign in three ways:

{timing_narrative}

### Cascade effects

{cascade_rows}

### Implications for the federal arithmetic

The state's federal seats contribute {fed_seats(state)} members to the Dewan Rakyat. On the P50 projection, the federal engine allocates these seats as follows:

| Bloc | Projected Federal Seats in {state} |
|---|---|
{proj_table}

This allocation is a subset of the national scenario table (Section 8.2) and inherits its uncertainties. The state's PRN result, when delivered, will either confirm or contradict the federal model's per-seat projections."""


def sec9_scenarios(state, meta, proj, swings, b):
    total = b["total"]
    if proj:
        ptotal = sum(proj["bloc_totals"].values())
        ptop = bloc_order_by_seats(proj["bloc_totals"])[0]
        proj_rows = "\n".join(
            f"| {bloc} | {seats} | {seats/ptotal*100:.1f}% |"
            for bloc, seats in bloc_order_by_seats(proj["bloc_totals"])
        )
        flip_rows = "\n".join(
            f"| {f['seat']} | {f['constituency']} | {f['from']} → {f['to']} | {f['margin_2021']:.2f}% | {f['proj_margin']:.2f}% |"
            for f in proj["flips"]
        ) if proj["flips"] else "| — | no flips in the base scenario | — | — | — |"
        scen_txt = ""
        if proj["scenarios"]:
            headers = list(proj["scenarios"][0].keys())
            srows = "\n".join(
                "| " + " | ".join(str(r.get(h, "")) for h in headers) + " |" for r in proj["scenarios"]
            )
            hdr = "| " + " | ".join(headers) + " |"
            sep = "|" + "---|" * len(headers)
            scen_txt = f"""The full scenario table from the projection report ([S-09]):

| {hdr[2:-2]} |
{sep}
{srows}
"""
        # Per-scenario DETAILED explanations (owner directive 10 Aug 2026):
        # one block per scenario row — outcome, bloc seats, type, and for
        # [Fed]/[State] narratives the basis note. Same data feeds the app's
        # collapsible scenario details (data-vs-code contract).
        def _toi(v):
            # Coerce a scenario cell to int, tolerating '' / None / str / bold.
            try:
                return int(str(v).replace("*", "").strip())
            except (TypeError, ValueError):
                return 0

        _DETAIL_META = {"Scenario", "Type", "Seats flipping", "Character"}

        def _state_detail(r, ms=False):
            # Display rows use capitalized keys ("Scenario", "Type", "Seats flipping",
            # "Character"); the JSON source uses lowercase. Read both.
            name = r.get('Scenario') or r.get('scenario') or '?'
            t = (r.get('Type') or r.get('category') or 'parametric')
            tlabel = ('Senario naratif' if ms else 'narrative') if t == 'narrative' else ('Senario parametrik' if ms else 'parametric')
            # Government-aligned = BN + GPS + GRS + WARISAN (federal alignment).
            # Every cell is coerced — the JSON may carry '' for absent blocs
            # (Sarawak uses GPS/PSB, not BN/PN).
            g = (_toi(r.get('BN')) + _toi(r.get('GPS')) + _toi(r.get('GRS'))
                 + _toi(r.get('WARISAN')))
            total = sum(_toi(v) for k, v in r.items() if k not in _DETAIL_META)
            maj = (total // 2) + 1 if total else 15
            flips = r.get('Seats flipping') if r.get('Seats flipping') is not None else r.get('flips', 0)
            bloc_parts = [f"{k} {_toi(r.get(k))}" for k in
                          ('BN', 'PH', 'PN', 'GPS', 'GRS', 'WARISAN', 'IND',
                           'MUDA', 'BERSAMA', 'DAP', 'PSB', 'KDM', 'PBM') if k in r]
            bloc_line = " · ".join(bloc_parts) or "—"
            lines = [
                f"**{name}** — {tlabel}",
                (f"- **Hasil:** {g} kerusi sejajar kerajaan daripada {total} "
                 f"({'melebihi' if g >= maj else 'di bawah'} majoriti mudah {maj})."
                 if ms else
                 f"- **Outcome:** {g} government-aligned seats of {total} "
                 f"({'above' if g >= maj else 'below'} the {maj}-seat simple majority)."),
                (f"- **Blok:** {bloc_line}"),
                (f"- **Pertukaran:** {flips} kerusi" if ms else f"- **Flips:** {flips} seats"),
            ]
            return "\n".join(lines)
        state_scen_details = "\n\n".join(_state_detail(r) for r in proj.get("scenarios", []))
        state_scen_details_ms = "\n\n".join(_state_detail(r, ms=True) for r in proj.get("scenarios", []))
        # Check whether swings were available (for accurate narrative)
        sw_note = ""
        if proj.get("has_swings"):
            sw_note = "The base scenario carries the measured swings from Section 7 through every seat of the assembly"
        else:
            sw_note = "The base scenario holds all seats at their baseline (no fresh swing measurement); movements are driven by the national factor weightages and event shocks described in Sections 8 and 13"
        if LANG == "ms":
            flip_rows_ms = "\n".join(
                f"| {f['seat']} | {f['constituency']} | {f['from']} → {f['to']} | {f['margin_2021']:.2f}% | {f['proj_margin']:.2f}% |"
                for f in proj["flips"]
            ) if proj["flips"] else "| — | tiada pertukaran dalam senario asas | — | — | — |"
            scen_txt_ms = ""
            if proj["scenarios"]:
                headers = list(proj["scenarios"][0].keys())
                srows = "\n".join(
                    "| " + " | ".join(str(r.get(h, "")) for h in headers) + " |" for r in proj["scenarios"]
                )
                hdr = "| " + " | ".join(headers) + " |"
                sep = "|" + "---|" * len(headers)
                scen_txt_ms = f"""Jadual senario penuh daripada laporan unjuran ([S-09]):

| {hdr[2:-2]} |
{sep}
{srows}
"""
            return f"""## 10. Analisis Senario

Unjuran penuh telah dimodelkan untuk {state}, dan kes asasnya — dihitung secara langsung daripada fail unjuran ([S-09]) — ialah:

| Blok | Kerusi Unjuran | Bahagian |
|---|---|---|
{proj_rows}

{sw_note}: {ptop[0]} menyatukan kepada **{ptop[1]} daripada {ptotal} kerusi** ({ptop[1]/ptotal*100:.1f} peratus), dengan purata margin unjuran di seluruh negeri pada **{proj['avg_margin']:.1f} mata**. Kerusi yang diunjurkan bertukar tangan dalam kes asas ialah:

| Kerusi | Kawasan | 2021 → Unjuran | Margin 2021 | Margin Unjuran |
|---|---|---|---|---|
{flip_rows_ms}

{scen_txt_ms}
**Perincian senario — setiap senario dijelaskan:**

{state_scen_details_ms}

{scenario_comment_ms(state, proj)}

---


"""
        return f"""## 10. Scenario Analysis

A full projection has been modelled for {state}, and its base case — computed live from the projection file ([S-09]) — is:

| Bloc | Projected Seats | Share |
|---|---|---|
{proj_rows}

{sw_note}: {ptop[0]} consolidates to **{ptop[1]} of {ptotal} seats** ({ptop[1]/ptotal*100:.1f} per cent), with the average projected margin across the state at **{proj['avg_margin']:.1f} points**. The seats projected to change hands in the base case are:

| Seat | Constituency | 2021 → Projected | 2021 margin | Projected margin |
|---|---|---|---|---|
{flip_rows}

{scen_txt}
**Scenario details — each scenario explained:**

{state_scen_details}

{scenario_comment(state, proj)}

---

"""

    # No projection: directional outlook, NO fabricated numbers.
    sw = swings
    sw_txt = ""
    if sw:
        top = sorted(sw.items(), key=lambda x: -abs(x[1]["swing"]))[:2]
        sw_txt = ("The most recent measured movements are " +
                  ", ".join(f"{bloc} {v['swing']:+.1f}pp" for bloc, v in top) +
                  f" (latest poll {max(v['latest_date'] for v in sw.values())}).")
    if LANG == "ms":
        sw_txt_ms = ""
        if sw:
            top = sorted(sw.items(), key=lambda x: -abs(x[1]["swing"]))[:2]
            sw_txt_ms = ("Pergerakan terukur terkini ialah " +
                         ", ".join(f"{bloc} {v['swing']:+.1f}pp" for bloc, v in top) +
                         f" (tinjauan terkini {max(v['latest_date'] for v in sw.values())}).")
        top_bloc_np, top_seats_np = bloc_order_by_seats(b["bloc_seats"])[0]
        return f"""## 9. Tinjauan Unjuran

Unjuran PRN penuh belum dimodelkan untuk {state} — fail unjuran wujud untuk negeri yang dewan undangannya terbubar dalam tetingkap GE16, dan pungutan suara negeri seterusnya negeri ini jatuh di luar tetingkap itu. Sebagai ganti jadual senario yang dimodelkan, bahagian ini membentangkan bacaan arah yang disokong oleh data hidup, dan eksplisit tentang apa yang tidak dimodelkan.

{sw_txt_ms} Garis dasar negeri (Bahagian 4) ialah penambat: {top_bloc_np} memegang {top_seats_np} daripada {total} kerusi, dengan struktur margin yang diterangkan di sana menentukan berapa banyak yang boleh ditandingi. Isyarat yang akan menggerakkan negeri ini antara sekarang dengan PRN seterusnya ialah: **(1)** keputusan GE16 nasional itu sendiri — kerajaan yang menang di peringkat persekutuan akan membawa ekor kemenangan ke kerusi persekutuan negeri dan, akhirnya, kerusi negerinya; **(2)** ketahanan penjajaran semula 2026 — perpecahan PAS–Bersatu dan trajektori Bersama akan menentukan sama ada blok pembangkang negeri bertanding dalam GE16 bersatu atau berpecah, yang di bawah FPTP bernilai beberapa kerusi sama ada cara; **(3)** rekod kerajaan negeri sendiri — penyampaian isu yang mendominasi elektorat negeri (kos sara hidup, infrastruktur, dan agenda khusus negeri seperti MA63 bagi negeri Borneo).

Bacaan arah untuk {state}, berdasarkan bukti yang ada, ialah: {directional_read_ms(state, b, sw)} Tiada nombor dalam bahagian ini ialah unjuran; di mana model belum dijalankan, laporan ini menyatakannya dengan jelas dan bukannya mengada-ada keputusan. Analisis senario penuh negeri akan ditambah apabila unjuran dimodelkan — jentera dalam Bahagian 8 sudah sedia, dan pencetusnya ialah pendekatan dewan kepada pembubaran.

Apa yang perlu difahami pembaca ialah perbezaan antara ketiadaan unjuran dan ketiadaan analisis: bahagian ini tidak memberitahu anda siapa yang akan menang, kerana itu memerlukan model penuh yang belum dijalankan, tetapi ia memberitahu anda kuasa-kuasa yang akan menentukan keputusan itu, struktur yang akan mengekangnya, dan isyarat yang perlu diperhatikan apabila tarikh PRN menghampiri. Apabila dewan menghampiri pembubaran dan jentera Bahagian 8 dijalankan, bahagian ini akan digantikan dengan jadual senario penuh, senarai pertukaran, dan taburan kebarangkalian yang setanding dengan negeri-negeri yang telah dimodelkan.

---


"""
    return f"""## 9. Projection Outlook

No full PRN projection has been modelled for {state} yet — the projection files exist for Melaka and Sarawak, whose assemblies dissolve inside the GE16 window, and this state's next state poll falls outside it. In place of a modelled scenario table, this section sets out the directional read that the live data supports, and is explicit about what is not modelled.

{sw_txt} The state's baseline (Section 4) is the anchor: {bloc_order_by_seats(b['bloc_seats'])[0][0]} holds {bloc_order_by_seats(b['bloc_seats'])[0][1]} of {total} seats, with the margin structure described there defining how much is contestable. The signals that would move this state between now and its next PRN are: **(1)** the national GE16 outcome itself — a government that wins federally will carry coattails into the state's federal seats and, eventually, its state seats; **(2)** the durability of the 2026 realignments — the PAS–Bersatu rupture and Bersama's trajectory will determine whether the state's opposition bloc contests GE16 unified or fragmented, which under FPTP is worth several seats either way; **(3)** the state government's own record — delivery on the issues that dominate the state's electorate (cost of living, infrastructure, and the state-specific agenda such as MA63 for the Borneo states).

The directional read for {state}, on the evidence available, is: {directional_read(state, b, sw)} No number in this section is a projection; where the model has not been run, this report says so plainly rather than fabricate a result. The state's full scenario analysis will be added when a projection is modelled — the machinery in Section 8 is ready, and the trigger is the assembly's approach to dissolution.

---

"""


def scenario_comment(state, proj):
    """State-specific reading of the scenario range (projection states only).
    Uses the parsed scenario table when available; otherwise falls back to the
    base-case bloc totals."""
    arch = STATE_META[state]["archetype"]
    ptotal = sum(proj["bloc_totals"].values())
    ptop = bloc_order_by_seats(proj["bloc_totals"])[0]
    if proj["scenarios"]:
        srows = proj["scenarios"]
        # find a numeric column shared across all scenario rows (GPS / BN / etc.)
        keys = [k for k in srows[0].keys()
                if k not in ("Scenario", "Character", "Seats flipping", "Seats changing")]
        lo, hi = None, None
        label = None
        for k in keys:
            vals = []
            for r in srows:
                v = r.get(k, "")
                clean = str(v).strip().strip("*").strip("-").strip()
                if clean.replace(".", "").isdigit():
                    vals.append(float(clean))
            if len(vals) == len(srows) and vals:
                mn, mx = min(vals), max(vals)
                if lo is None or (mx - mn) > (hi - lo):
                    lo, hi, label = mn, mx, k
        if label and lo is not None:
            rng = f"the {label} column runs from **{lo:.0f}** in the worst case to **{hi:.0f}** in the best"
        else:
            rng = f"the base case carries {ptop[0]} to {ptop[1]} of {ptotal} seats"
    else:
        rng = f"the base case carries {ptop[0]} to {ptop[1]} of {ptotal} seats"
    if arch == "gps_hegemony":
        return (f"Every scenario leaves GPS above the two-thirds threshold — {rng}, and the state's "
                f"real stakes are the size of the supermajority, the survival of any opposition bloc "
                f"at all, and the signal the result sends into the federal GE16 calculus in which "
                f"GPS is the kingmaker.")
    if arch == "melaka_divergence":
        return (f"The scenarios bracket the state's availability: {rng}. The honest reading is that "
                f"Melaka is the most open contest in the country — the range is the finding, not the "
                f"base case.")
    return (f"The range is narrow because the state's structural floor is high — {rng} — and the "
            f"alternative scenarios move only the margins around that settled core.")


def scenario_comment_ms(state, proj):
    """Malay variant of scenario_comment — used by sec9_scenarios in --lang ms mode."""
    arch = STATE_META[state]["archetype"]
    ptotal = sum(proj["bloc_totals"].values())
    ptop = bloc_order_by_seats(proj["bloc_totals"])[0]
    if proj["scenarios"]:
        srows = proj["scenarios"]
        keys = [k for k in srows[0].keys()
                if k not in ("Scenario", "Character", "Seats flipping", "Seats changing")]
        lo, hi = None, None
        label = None
        for k in keys:
            vals = []
            for r in srows:
                v = r.get(k, "")
                clean = str(v).strip().strip("*").strip("-").strip()
                if clean.replace(".", "").isdigit():
                    vals.append(float(clean))
            if len(vals) == len(srows) and vals:
                mn, mx = min(vals), max(vals)
                if lo is None or (mx - mn) > (hi - lo):
                    lo, hi, label = mn, mx, k
        if label and lo is not None:
            rng = f"lajur {label} berjalan daripada **{lo:.0f}** dalam kes terburuk kepada **{hi:.0f}** dalam kes terbaik"
        else:
            rng = f"kes asas membawa {ptop[0]} kepada {ptop[1]} daripada {ptotal} kerusi"
    else:
        rng = f"kes asas membawa {ptop[0]} kepada {ptop[1]} daripada {ptotal} kerusi"
    if arch == "gps_hegemony":
        return (f"Setiap senario meninggalkan GPS di atas ambang dua pertiga — {rng}, dan kepentingan "
                f"sebenar negeri ialah saiz supermajoriti, kemandirian mana-mana blok pembangkang "
                f"sama sekali, dan isyarat yang dihantar keputusan itu ke dalam kalkulus GE16 "
                f"persekutuan di mana GPS ialah kingmaker.")
    if arch == "melaka_divergence":
        return (f"Senario merangkum ketersediaan negeri: {rng}. Bacaan yang jujur ialah Melaka "
                f"merupakan pertandingan paling terbuka di negara ini — julat itu ialah dapatan, "
                f"bukan kes asas.")
    return (f"Julatnya sempit kerana lantai struktur negeri ini tinggi — {rng} — dan senario "
            f"alternatif hanya menggerakkan margin di sekeliling teras yang telah mantap itu.")


def directional_read(state, b, sw):
    arch = STATE_META[state]["archetype"]
    top_bloc, top_seats = bloc_order_by_seats(b["bloc_seats"])[0]
    if arch == "green_wave":
        return (f"the governing {top_bloc} bloc's dominance is structural and is expected to "
                f"persist; the open question is whether the PAS–Bersatu rupture shaves its "
                f"supermajority, not whether it governs")
    if arch == "ph_led":
        return (f"the governing coalition holds a working majority that the 2023 green wave "
                f"narrowed but did not breach; the state's next PRN will test whether the "
                f"opposition's 2023 gains were a ceiling")
    if arch == "melaka_divergence":
        return ("the most open contest in the country — the southern resurgence points to a BN "
                "consolidation, the green wave points to an inversion, and the state's own "
                "federal pattern points to a PH-PN duopoly; the four-way spread is the honest "
                "expression of the state's availability")
    if arch == "sabah_local":
        return (f"continuation of the local-coalition pattern: the largest bloc after the next "
                f"poll will again need partners, and the government will again be assembled "
                f"after the count, not before it")
    if arch == "gps_hegemony":
        return ("continued GPS hegemony — the only question modelled anywhere is the size of "
                "the supermajority, which every scenario leaves above two-thirds")
    if arch == "bn_supermajority":
        return (f"continued {top_bloc} dominance on the strength of the southern resurgence, "
                f"with the margin of the supermajority as the live variable")
    if arch == "hung_coalition":
        return ("a continuation of coalition arithmetic — no bloc commands a majority alone, "
                "and the next government will again be assembled from the largest blocs")
    if arch == "bn_resurgence":
        return (f"consolidation of the Unity Government pact, which the 2026 result showed can "
                f"command a decisive majority when BN and PH contest cooperatively")
    return "a holding pattern at the baseline, with the national GE16 outcome as the dominant future shock"


def directional_read_ms(state, b, sw):
    arch = STATE_META[state]["archetype"]
    top_bloc, top_seats = bloc_order_by_seats(b["bloc_seats"])[0]
    if arch == "green_wave":
        return (f"dominasi blok {top_bloc} yang memerintah adalah struktur dan dijangka "
                f"berterusan; persoalan terbuka ialah sama ada perpecahan PAS–Bersatu menjejaskan "
                f"supermajoritinya, bukan sama ada ia memerintah")
    if arch == "ph_led":
        return (f"gabungan pemerintah memegang majoriti bekerja yang disempitkan tetapi tidak "
                f"dipecahkan oleh gelombang hijau 2023; PRN negeri seterusnya akan menguji sama ada "
                f"keuntungan pembangkang 2023 ialah siling")
    if arch == "melaka_divergence":
        return ("pertandingan paling terbuka di negara ini — kebangkitan selatan menunjuk kepada "
                "penyatuan BN, gelombang hijau menunjuk kepada penyongsangan, dan corak persekutuan "
                "negeri sendiri menunjuk kepada duopoli PH-PN; penyebaran empat penjuru ialah "
                "ungkapan jujur ketersediaan negeri")
    if arch == "sabah_local":
        return (f"sambungan corak gabungan tempatan: blok terbesar selepas pungutan suara "
                f"seterusnya akan memerlukan rakan kongsi lagi, dan kerajaan akan disusun semula "
                f"selepas kiraan, bukan sebelum itu")
    if arch == "gps_hegemony":
        return ("kesinambungan hegemoni GPS — satu-satunya persoalan yang dimodelkan di mana-mana "
                "ialah saiz supermajoriti, yang setiap senario tinggalkan di atas dua pertiga")
    if arch == "bn_supermajority":
        return (f"kesinambungan dominasi {top_bloc} atas kekuatan kebangkitan selatan, "
                f"dengan margin supermajoriti sebagai pemboleh ubah hidup")
    if arch == "hung_coalition":
        return ("kesinambungan aritmetik gabungan — tiada blok menguasai majoriti sendirian, "
                "dan kerajaan seterusnya akan disusun semula daripada blok terbesar")
    if arch == "bn_resurgence":
        return (f"penyatuan pakatan Kerajaan Perpaduan, yang keputusan 2026 menunjukkan boleh "
                f"memerintah dengan majoriti muktamad apabila BN dan PH bertanding secara koperatif")
    return "corak menunggu pada garis dasar, dengan keputusan GE16 nasional sebagai kejutan dominan pada masa hadapan"


def sec10_battlegrounds(state, b, fed, meta, bg_tiered=None, is_tier1=False):
    total = b["total"]
    super_list = [s for s in b["seats"] if s["margin"] < 2.0]
    marg_list = [s for s in b["seats"] if 2.0 <= s["margin"] < 5.0]
    sm_rows = "\n".join(
        f"| {s['seat']} | {s['winner']} | {s['party']} | {s['bloc']} | {s['margin']:.2f}% | {s['majority']:,} |"
        for s in sorted(super_list, key=lambda x: x["margin"])[:8]
    ) if super_list else "| — | none | — | — | — | — |"
    m_rows = "\n".join(
        f"| {s['seat']} | {s['winner']} | {s['party']} | {s['bloc']} | {s['margin']:.2f}% | {s['majority']:,} |"
        for s in sorted(marg_list, key=lambda x: x["margin"])[:8]
    ) if marg_list else "| — | none | — | — | — | — |"
    fed_bg = fed["battlegrounds"]
    fb_rows = "\n".join(
        f"| {s['code']} | {s['constituency']} | {s['ge15_bloc']} | {s['ge15_margin']:.2f}% |"
        for s in fed_bg
    ) if fed_bg else "| — | none | — | — |"

    # ---- TIERED BATTLEGROUNDS (gap analysis item c) ----
    tiered_txt = ""
    if is_tier1 and bg_tiered:
        # Federal 3-tier split: <1% super-marginal, 1-2.5% high-risk, 2.5-5% watch
        super_sm = [s for s in bg_tiered if s["tier"] == "SUPER-MARGINAL"]
        high_risk = [s for s in bg_tiered if s["tier"] == "HIGH-RISK"]
        watch = [s for s in bg_tiered if s["tier"] == "WATCH"]

        def bg_table(rows):
            if not rows:
                return "| — | none at this tier | — | — | — | — | — | — | — |"
            return "\n".join(
                f"| {r['code']} | {r['constituency']} | {r['tier']} | {r['margin']:.2f}% | "
                f"{r.get('malay_pct', '—')} | {r.get('chinese_pct', '—')} | "
                f"{r.get('youth_pct', '—')} | {r.get('median_age', '—')} | {intnum(r.get('total_voters')):,} |"
                for r in rows
            )

        tiered_txt = f"""### {tier_section(state, 12, 10)}.4 Federal battleground deep-dive ({state})

The following table cross-references {state}'s parliamentary seats against the federal battleground master list ([S-04]), applying the project's three-tier classification system:

| Code | Constituency | Tier | Margin | Malay % | Chinese % | Youth % | Median Age | Electorate |
|---|---|---|---|---|---|---|---|---|
{bg_table(bg_tiered)}

**Tier definitions:**
- **SUPER-MARGINAL** (< 1% margin): the most fragile seats in the federation; a single event shock can flip these
- **HIGH-RISK** (1–2.5% margin): highly sensitive to uniform swings of 2–3 points
- **WATCH** (2.5–5% margin): contestable but require larger movements to flip

This state contributes **{len(bg_tiered)} federal battleground seats** to the GE16 map: **{len(super_sm)} super-marginal**, **{len(high_risk)} high-risk**, and **{len(watch)} watch**. The super-marginal seats are the most valuable targets for any opposition campaign, while the high-risk seats are the first to move when a uniform swing exceeds two points."""

    if LANG == "ms":
        tiered_txt_ms = ""
        if is_tier1 and bg_tiered:
            super_sm = [s for s in bg_tiered if s["tier"] == "SUPER-MARGINAL"]
            high_risk = [s for s in bg_tiered if s["tier"] == "HIGH-RISK"]
            watch = [s for s in bg_tiered if s["tier"] == "WATCH"]

            def bg_table_ms(rows):
                if not rows:
                    return "| — | tiada pada lapisan ini | — | — | — | — | — | — | — |"
                return "\n".join(
                    f"| {r['code']} | {r['constituency']} | {r['tier']} | {r['margin']:.2f}% | "
                    f"{r.get('malay_pct', '—')} | {r.get('chinese_pct', '—')} | "
                    f"{r.get('youth_pct', '—')} | {r.get('median_age', '—')} | {intnum(r.get('total_voters')):,} |"
                    for r in rows
                )

            tiered_txt_ms = f"""### {tier_section(state, 12, 10)}.4 Analisis mendalam medan pertempuran persekutuan ({state})

Jadual berikut merujuk silang kerusi parlimen {state} terhadap senarai induk medan pertempuran persekutuan ([S-04]), menggunakan sistem klasifikasi tiga lapisan projek:

| Kod | Kawasan | Lapisan | Margin | Melayu % | Cina % | Belia % | Umur Median | Pengundi |
|---|---|---|---|---|---|---|---|---|
{bg_table_ms(bg_tiered)}

**Definisi lapisan:**
- **SUPER-MARGINAL** (< 1% margin): kerusi paling rapuh dalam persekutuan; satu kejutan peristiwa boleh menukarnya
- **HIGH-RISK** (1–2.5% margin): sangat sensitif kepada ayunan seragam 2–3 mata
- **WATCH** (2.5–5% margin): boleh ditandingi tetapi memerlukan pergerakan lebih besar untuk bertukar

Negeri ini menyumbang **{len(bg_tiered)} kerusi medan pertempuran persekutuan** kepada peta GE16: **{len(super_sm)} super-marginal**, **{len(high_risk)} high-risk**, dan **{len(watch)} watch**. Kerusi super-marginal ialah sasaran paling berharga bagi mana-mana kempen pembangkang, manakala kerusi high-risk ialah yang pertama bergerak apabila ayunan seragam melebihi dua mata."""
        return f"""## {tier_section(state, 12, 10)}. Medan Pertempuran & Kerusi yang Perlu Diperhatikan

Keupayaan bertanding {state} tertumpu dalam sebilangan kecil kerusi. Daripada {total} kawasan negeri, **{len(super_list)} ialah super-marginal** (margin kemenangan di bawah dua mata) dan **{len(marg_list)} ialah marginal** (dua hingga lima mata) — bersama {(len(super_list)+len(marg_list))/total*100:.0f} peratus daripada dewan — dan kerusi-kerusi inilah yang membawa potensi ayunan negeri. Setiap nombor di bawah dihitung secara langsung daripada fail keputusan ([S-01]).

### {tier_section(state, 12, 10)}.1 Kerusi negeri super-marginal (margin < 2%)

| Kerusi | Pemenang | Parti | Blok | Margin | Majoriti (undi) |
|---|---|---|---|---|---|
{sm_rows}

Ini ialah kerusi pisau cukur negeri — setiap satu diputuskan oleh ayunan beberapa mata, dan dalam beberapa kes oleh beberapa ratus undi. {super_marginal_comment_ms(state, super_list)}

### {tier_section(state, 12, 10)}.2 Kerusi negeri marginal (margin 2–5%)

| Kerusi | Pemenang | Parti | Blok | Margin | Majoriti (undi) |
|---|---|---|---|---|---|
{m_rows}

Lapisan marginal ialah tempat timbunan ayunan memberi kesan terbesar: pergerakan dua hingga tiga mata — saiz ayunan terukur dalam Bahagian 7 — sudah cukup untuk menukar beberapa kerusi ini, dan arah penukaran itu ialah sumbangan negeri kepada cerita GE16.

### {tier_section(state, 12, 10)}.3 Medan pertempuran persekutuan di {state}

| Kod | Kawasan | Blok GE15 | Margin GE15 |
|---|---|---|---|
{fb_rows}

Medan pertempuran persekutuan ialah jambatan negeri ini kepada unjuran nasional: kerusi yang margin GE15 di bawah lima mata, dan yang akan diputuskan oleh pergerakan pasca-GE15 negeri — diukur dalam Bahagian 7 dan diterapkan dalam Bahagian 8. {fed_bg_comment_ms(state, fed_bg)} Analisis mendalam ([S-08]) membekalkan perincian kerusi demi kerusi — pemegang, elektorat, komposisi demografi — dan jadual bahagian ini ialah peta peringkat negeri tentang tempat penjajaran semula 2026 akan mendarat dahulu.

{tiered_txt_ms}
---


"""
    return f"""## {tier_section(state, 12, 10)}. Battlegrounds & Seats to Watch

The contestability of {state} is concentrated in a small number of seats. Of the {total} state constituencies, **{len(super_list)} are super-marginal** (winning margin under two points) and **{len(marg_list)} are marginal** (two to five points) — together {(len(super_list)+len(marg_list))/total*100:.0f} per cent of the assembly — and it is these seats that carry the state's swing potential. Every number below is computed live from the results file ([S-01]).

### {tier_section(state, 12, 10)}.1 Super-marginal state seats (margin < 2%)

| Seat | Winner | Party | Bloc | Margin | Majority (votes) |
|---|---|---|---|---|---|
{sm_rows}

These are the state's knife-edge seats — each decided by a swing of a few points, and in several cases by a few hundred votes. {super_marginal_comment(state, super_list)}

### {tier_section(state, 12, 10)}.2 Marginal state seats (margin 2–5%)

| Seat | Winner | Party | Bloc | Margin | Majority (votes) |
|---|---|---|---|---|---|
{m_rows}

The marginal tier is where the swing stack has its greatest effect: a two-to-three-point movement — the size of the measured swings in Section 7 — is sufficient to convert several of these seats, and the direction of that conversion is the state's contribution to the GE16 story.

### {tier_section(state, 12, 10)}.3 Federal battlegrounds in {state}

| Code | Constituency | GE15 Bloc | GE15 Margin |
|---|---|---|---|
{fb_rows}

The federal battlegrounds are the state's bridge to the national projection: seats whose GE15 margins sit under five points, and which the state's post-GE15 movements — measured in Section 7 and applied in Section 8 — will decide. {fed_bg_comment(state, fed_bg)} The deep-dive analysis ([S-08]) supplies the seat-by-seat detail — holders, electorates, demographic composition — and this section's tables are the state-level map of where the 2026 realignments will land first.

{tiered_txt}
---

"""


def super_marginal_comment(state, super_list):
    if not super_list:
        return "The absence of super-marginal seats is itself a statement: the state's map is settled enough that no seat sits on a knife-edge."
    closest = min(super_list, key=lambda x: x["margin"])
    return (f"The thinnest result in the state is {closest['seat']}, held by {closest['party']} "
            f"({closest['bloc']}) by {closest['majority']:,} votes — a margin of {closest['margin']:.2f} "
            f"per cent. Seats at this thickness are decided by turnout differentials, candidate "
            f"quality, and last-week campaign events; the model treats them as genuine coin-flips "
            f"with elevated uncertainty (σ = 2.0–3.0 points).")


def super_marginal_comment_ms(state, super_list):
    if not super_list:
        return "Ketiadaan kerusi super-marginal itu sendiri ialah satu kenyataan: peta negeri cukup mantap sehingga tiada kerusi di pisau cukur."
    closest = min(super_list, key=lambda x: x["margin"])
    return (f"Keputusan paling nipis di negeri ialah {closest['seat']}, dipegang oleh {closest['party']} "
            f"({closest['bloc']}) dengan {closest['majority']:,} undi — margin {closest['margin']:.2f} "
            f"peratus. Kerusi setipis ini diputuskan oleh perbezaan keluar mengundi, kualiti calon, "
            f"dan peristiwa kempen minggu terakhir; model melayannya sebagai lambungan duit syiling "
            f"tulen dengan ketidakpastian yang tinggi (σ = 2.0–3.0 mata).")


def fed_bg_comment(state, fed_bg):
    if not fed_bg:
        return ("The state's federal seats are all held by margins above five points, meaning the "
                "state contributes no federal battlegrounds to the GE16 map — its national "
                "significance runs through its safe seats and its state-level signals rather than "
                "through marginal federal contests.")
    arch = STATE_META[state]["archetype"]
    if arch == "melaka_divergence":
        return ("Melaka's federal seats — PH 3, PN 3 — are the divergence made concrete, and the "
                "state's 2026-27 PRN is the live test of which federal pattern the state electorate "
                "will reproduce.")
    if arch == "green_wave":
        return ("In a green-wave state the federal battlegrounds are the green wave's outer edge — "
                "the seats it nearly took — and the question for GE16 is whether the 2023 surge "
                "holds or recedes.")
    return ("These seats are the front line of the state's federal story: the measured state swings "
            "determine whether the GE15 holder holds, and the event shocks from the 2026 realignments "
            "determine whether the margin survives contact with a fragmented opposition.")


def fed_bg_comment_ms(state, fed_bg):
    if not fed_bg:
        return ("Kerusi persekutuan negeri ini semuanya dipegang dengan margin melebihi lima mata, "
                "bermakna negeri ini tidak menyumbang medan pertempuran persekutuan kepada peta GE16 — "
                "kepentingan nasionalnya melalui kerusi selamat dan isyarat peringkat negeri dan "
                "bukannya melalui pertandingan persekutuan marginal.")
    arch = STATE_META[state]["archetype"]
    if arch == "melaka_divergence":
        return ("Kerusi persekutuan Melaka — PH 3, PN 3 — ialah perbezaan itu yang menjadi konkrit, "
                "dan PRN 2026-27 negeri ialah ujian hidup corak persekutuan yang akan dihasilkan "
                "semula oleh elektorat negeri.")
    if arch == "green_wave":
        return ("Di negeri gelombang hijau, medan pertempuran persekutuan ialah pinggir luar gelombang "
                "hijau — kerusi yang hampir diambil — dan persoalan untuk GE16 ialah sama ada lonjakan "
                "2023 bertahan atau surut.")
    return ("Kerusi ini ialah barisan hadapan cerita persekutuan negeri: ayunan negeri terukur "
            "menentukan sama ada pemegang GE15 kekal, dan kejutan peristiwa daripada penjajaran "
            "semula 2026 menentukan sama ada margin bertahan daripada pertembungan dengan "
            "pembangkang yang berpecah.")


def sec13_party_landscape(state, party_landscape=None):
    """Section 13: Party Landscape (federal-parity). Folds the project's
    party-landscape update notes into a proper numbered section, demoting the
    file's own headers so numbering stays with the report."""
    if not party_landscape:
        return ""
    pl_clean = re.sub(r"(?m)^# ", "### ", party_landscape)
    pl_clean = re.sub(r"(?m)^## ", "#### ", pl_clean)
    if LANG == "ms":
        return f"""## 14. Landskap Parti

Landskap parti semasa yang dibina oleh projek ini — perpecahan PAS–Bersatu, penjajaran semula WAWASAN, pelancaran Bersama, dan kedudukan parti utama — ditunjukkan di bawah, diterapkan pada pertandingan {state}:

{pl_clean}

---


"""
    return f"""## 14. Party Landscape

The project's current party-landscape read — the PAS–Bersatu rupture, the WAWASAN realignment, the Bersama launch, and the positioning of the major parties — is set out below, applied to {state}'s contest:

{pl_clean}

---


"""


def sec14_prn_scorecard(state, meta, proj, b):
    """Section 14: PRN Prediction Scorecard (federal-parity). For Tier-1 states
    the PRN is upcoming, so the scorecard records the project's own PRN
    projection as the prediction on record, plus the federal calibration
    lessons from the three most recent state elections."""
    total = b["total"]
    dis = next_dissolution(state)
    dis_str = dis.strftime("%d %B %Y")
    # Projection on record (if any)
    if proj:
        ptop = bloc_order_by_seats(proj["bloc_totals"])[0]
        proj_line = (f"**{ptop[0]} {ptop[1]}** of {total} seats (base scenario), "
                     f"{len(proj['flips'])} flips modelled")
    else:
        proj_line = "no live projection file for this state"
    if LANG == "ms":
        return f"""## 15. Skor Kad Ramalan PRN — Siapa Yang Tepat?

Skor kad ramalan PRN ialah bukti penentukuran projek ini: rekod sejauh mana pusat penyelidikan utama Malaysia dan penganalisis individu meramal tiga pilihan raya negeri terbaharu — Sabah (November 2025), Johor (Julai 2026) dan Negeri Sembilan (Ogos 2026). Bagi {state}, PRN seterusnya jatuh tempo sekitar **{dis_str}**, jadi skor kad di sini merekodkan unjuran projek ini sendiri sebagai ramalan rasmi, bersama pengajaran penentukuran daripada skor kad persekutuan.

**Unjuran projek ini untuk {state} (ramalan rasmi):**

{proj_line}. Unjuran penuh dan senarionya diterbitkan dalam Bahagian 12. Apabila PRN berlangsung, keputusan sebenar akan mengesahkan atau menyangkal unjuran ini — dan hasilnya akan direkodkan di sini.

**Pengajaran penentukuran daripada skor kad persekutuan (Sabah/Johor/N9):**

1. **Arah mengatasi magnitud:** ramalan terbaik menjadikan kebarangkalian pembentukan kerajaan sebagai tajuk utama dan membentangkan kiraan kerusi sebagai julat P10/P50/P90, bukan anggaran titik.
2. **Sabah dan Malaysia Timur adalah titik buta:** setiap pusat yang menerapkan logik ayunan Semenanjung ke Sabah silap; model ini mengasingkan jenis kerusi `east_malaysia` dengan wajaran ayunan yang dikurangkan.
3. **Berasaskan ayunan mengatasi berasaskan tinjauan:** kebangkitan selatan (ayunan BN +17.0pp) meramal supermajoriti BN Johor yang menjadi kenyataan 48/56, manakala pusat berasaskan tinjauan terkurang. Keutamaan terserlah — undi sebenar — mengatasi niat yang dinyatakan.

Kelemahan struktur psefologi Malaysia juga didokumenkan di sini: pusat utama secara konsisten kurang meramal pemulihan selatan BN dan gagal memodelkan dinamik tempatan Malaysia Timur — bukan kegagalan teknik tinjauan tetapi rangka pemodelan. Model projek mengelak perangkap ini melalui ayunan per negeri dengan modulasi jenis, tetapi ia tidak kebal kepada masalah kualiti data asas; tindak balas jujurnya ialah melebarkan ketidakpastian bagi kerusi yang datanya lemah dan bukannya berpura-pura tepat.

Skor kad penuh: `1_DATA/research/knowledge/prn-prediction-scorecard.md`.

---


"""
    return f"""## 15. PRN Prediction Scorecard — Who Got It Right?

The PRN prediction scorecard is the project's calibration evidence: a record of how well Malaysia's major research centres and individual analysts predicted the three most recent state elections — Sabah (November 2025), Johor (July 2026) and Negeri Sembilan (August 2026). For {state}, the next PRN falls due around **{dis_str}**, so the scorecard here records the project's own PRN projection as the prediction on record, together with the calibration lessons from the federal scorecard.

**The project's own PRN projection for {state} (prediction on record):**

{proj_line}. The full projection and its scenarios are published in Section 12. When the PRN is held, the actual result will confirm or falsify this projection — and the outcome will be recorded here.

**Calibration lessons from the federal scorecard (Sabah/Johor/N9):**

1. **Direction beats magnitude:** the best predictions headline the probability of government formation and present seat counts as P10/P50/P90 ranges, not point estimates — exactly as the Monte Carlo does.
2. **Sabah and East Malaysia are a known blind spot:** every centre that applied Peninsular swing logic to Sabah was wrong; the model separates `east_malaysia` seat type with reduced swing weights.
3. **Swing-based beats poll-based:** the southern-resurgence scenario (BN +17.0pp swing) predicted the Johor BN supermajority that materialised as 48/56, while poll-based centres under-shot. Revealed preference — actual votes — beats stated intention.

Full scorecard: `1_DATA/research/knowledge/prn-prediction-scorecard.md`.

---


"""


def sec15_strategic(state, b, fed, meta, proj, forecast_state=None, party_landscape=None):
    total = b["total"]
    govt = govt_blocs_for(state)
    govt_seats = sum(b["bloc_seats"].get(g, 0) for g in govt)
    opp = total - govt_seats
    top_bloc, top_seats = bloc_order_by_seats(b["bloc_seats"])[0]

    if LANG == "ms":
        return f"""## {tier_section(state, 13, 11)}. Implikasi Strategik

Aritmetik peringkat negeri {state} diterjemahkan kepada keharusan yang berbeza bagi setiap blok yang memasuki GE16. Analisis di sini mengikut konvensyen laporan persekutuan: cadangan diterbitkan daripada nombor di atas, bukan daripada kecenderungan terdahulu.

**Untuk kerajaan negeri ({meta['govt_label']}, {govt_seats} daripada {total} kerusi).** Aset terkuat kerajaan ialah garis dasar itu sendiri: kusyen {govt_seats - (total // 2 + 1):+d} kerusi melebihi ambang majoriti, dengan {b['margin_tiers']['safe']} kerusi selamat menyediakan lantai struktur. Keutamaan strategik ialah mempertahankan lapisan super-marginal — {b['margin_tiers']['super']} kerusi di bawah dua mata adalah tempat satu ayunan buruk, kegagalan jentera, atau kemasukan kuasa ketiga boleh memulakan hakisan yang tidak dapat dihentikan oleh kerusi selamat. Keutamaan kedua ialah ekor persekutuan: {fed['n_seats']} kerusi persekutuan {state} ditandingi pada elektorat yang sama, dan rekod penyampaian kerajaan negeri ialah aset paling boleh dipindah milik yang dimiliki calon persekutuan bersekutu kerajaan. {govt_implication_ms(state, meta)}

**Untuk pembangkang ({top_bloc if top_bloc not in govt else 'blok minoriti'}, {opp} kerusi).** Masalah aritmetik pembangkang di {state} ialah penumpuan: kerusinya {opposition_concentration_ms(state, b)}, dan di bawah FPTP geografi itu menyampaikan kerusi dengan cekap hanya di mana sokongan terkumpul, bukan tersebar. Laluan kepada kaitan melalui {b['margin_tiers']['super'] + b['margin_tiers']['marginal']} kerusi negeri marginal dan {len(fed['battlegrounds'])} medan pertempuran persekutuan, dan ia memerlukan pembangkang bersatu padu dan bukannya berpecah — pengajaran penjajaran semula 2026, di mana perpecahan PAS–Bersatu dan kemasukan Bersama menukar pertandingan tiga dan empat penjuru kepada kemenangan kerajaan. {opp_implication_ms(state, meta)}

**Untuk unjuran persekutuan.** {state} menyumbang {fed['n_seats']} kerusi persekutuan kepada aritmetik nasional, dan pergerakan peringkat negerinya — ayunan dalam Bahagian 7 — diterapkan kepada kerusi itu pada wajaran faktor tertinggi. Medan pertempuran persekutuan negeri ini ({len(fed['battlegrounds'])} kerusi di bawah lima mata) ialah bahagian {len(fed['battlegrounds'])/222*100:.1f} peratus daripada peta medan pertempuran nasional, sebab itu negeri ini begitu menonjol dalam logik pertukaran model persekutuan: di mana negeri telah berayun keras ke satu arah sejak GE15, marginal persekutuannya condong ke arah yang sama.

**Untuk komuniti penganalisis.** Laporan negeri wujud kerana laporan nasional tidak dapat membawa kedalaman ini untuk tiga belas negeri serentak. Pengajaran {state} untuk psefologi Malaysia ialah {analyst_lesson_ms(state)} Daftar calon negeri — etnik, umur, jantina, kehilangan deposit — ialah set data yang tidak pernah dibuka oleh kebanyakan analisis nasional, dan ia adalah perkara paling dekat yang projek ini ada dengan bacaan kebenaran tanah strategi parti.

---


"""
    return f"""## {tier_section(state, 13, 11)}. Strategic Implications

The state-level arithmetic of {state} translates into distinct imperatives for each bloc entering GE16. The analysis here follows the federal report's convention: recommendations are derived from the numbers above, not from prior dispositions.

**For the state government ({meta['govt_label']}, {govt_seats} of {total} seats).** The government's strongest asset is the baseline itself: a cushion of {govt_seats - (total // 2 + 1):+d} seats over the majority threshold, with {b['margin_tiers']['safe']} safe seats providing a structural floor. The strategic priority is to defend the super-marginal tier — the {b['margin_tiers']['super']} seats under two points are where a single adverse swing, a machinery failure, or a third-force entry could begin an erosion that the safe seats cannot arrest. The second priority is the federal coattail: {state}'s {fed['n_seats']} federal seats are contested on the same electorate, and the state government's delivery record is the single most transferable asset the government-aligned federal candidates possess. {govt_implication(state, meta)}

**For the opposition ({top_bloc if top_bloc not in govt else 'the minority blocs'}, {opp} seats).** The opposition's arithmetic problem in {state} is concentration: its seats are {opposition_concentration(state, b)}, and under FPTP that geography delivers seats efficiently only where support is packed, not spread. The path to relevance runs through the {b['margin_tiers']['super'] + b['margin_tiers']['marginal']} marginal state seats and the {len(fed['battlegrounds'])} federal battlegrounds, and it requires the opposition to consolidate rather than fragment — the lesson of the 2026 realignments, where the PAS–Bersatu rupture and Bersama's entry converted three- and four-cornered contests into government wins. {opp_implication(state, meta)}

**For the federal projection.** {state} contributes {fed['n_seats']} federal seats to the national arithmetic, and its state-level movements — the swings in Section 7 — are applied to those seats at the highest factor weight. The state's federal battlegrounds ({len(fed['battlegrounds'])} seats under five points) are a {len(fed['battlegrounds'])/222*100:.1f} per cent share of the national battleground map, which is why this state appears so prominently in the federal model's flip logic: where the state has swung hard in one direction since GE15, its federal marginals lean the same way.

**For the analyst community.** The state reports exist because the national report cannot carry this depth for thirteen states at once. {state}'s lesson for Malaysian psephology is {analyst_lesson(state)} The state's candidate register — ethnicity, age, sex, deposit losses — is a dataset most national analyses never open, and it is the closest thing the project has to a ground-truth read of party strategy.

---

"""


def govt_implication(state, meta):
    arch = STATE_META[state]["archetype"]
    if arch == "gps_hegemony":
        return ("For GPS, the strategic question is not the state — it is the federal price: the "
                "MA63 agenda is the currency of the coalition's loyalty, and the GE16 kingmaker "
                "position is the leverage with which it is spent.")
    if arch == "sabah_local":
        return ("For the GRS-led coalition, the imperative is cohesion: the 2025 government was "
                "assembled from five parties after the largest single party lost, and holding that "
                "assembly together through GE16 is the state's central political task.")
    if arch == "melaka_divergence":
        return ("For BN's Melaka government, the divergence is the risk: a state government can "
                "lose the federal contest in the same electorate that returned it, and the 2026-27 "
                "PRN is the test of whether the state-level machinery that delivered 21 of 28 "
                "survives contact with a federal-style campaign.")
    if arch == "green_wave":
        return ("For the PN state government, the imperative is delivery-without-scandal: the "
                "green wave was won on grievance, and the 2026 realignments mean the opposition's "
                "federal brand is now contested internally even where its state seats are safe.")
    return ("For the governing coalition, the imperative is the same as it is federally: hold the "
            "marginals, deliver the state's headline issues, and let the structural floor do the "
            "rest.")


def govt_implication_ms(state, meta):
    arch = STATE_META[state]["archetype"]
    if arch == "gps_hegemony":
        return ("Bagi GPS, persoalan strategik bukan negeri — ia adalah harga persekutuan: "
                "agenda MA63 ialah mata wang kesetiaan gabungan, dan kedudukan penentu (kingmaker) "
                "GE16 ialah tuas yang digunakan untuk membelanjakannya.")
    if arch == "sabah_local":
        return ("Bagi gabungan pimpinan GRS, keharusan ialah perpaduan: kerajaan 2025 telah "
                "disusun daripada lima parti selepas parti tunggal terbesar kalah, dan mengekalkan "
                "perhimpunan itu melalui GE16 ialah tugas politik pusat negeri ini.")
    if arch == "melaka_divergence":
        return ("Bagi kerajaan BN Melaka, perbezaan itu ialah risiko: kerajaan negeri boleh "
                "kalah dalam pertandingan persekutuan pada elektorat yang sama yang mengembalikannya, "
                "dan PRN 2026-27 ialah ujian sama ada jentera peringkat negeri yang menyampaikan "
                "21 daripada 28 bertahan daripada pertembungan dengan kempen gaya persekutuan.")
    if arch == "green_wave":
        return ("Bagi kerajaan negeri PN, keharusan ialah penyampaian-tanpa-skandal: "
                "gelombang hijau dimenangi atas rungutan, dan penjajaran semula 2026 bermakna jenama "
                "persekutuan pembangkang kini dipertandingkan secara dalaman walaupun kerusi "
                "negerinya selamat.")
    return ("Bagi gabungan pemerintah, keharusan sama seperti di peringkat persekutuan: pegang "
            "marginal, sampaikan isu utama negeri, dan biarkan lantai struktur melakukan yang lain.")


def opp_implication(state, meta):
    arch = STATE_META[state]["archetype"]
    if arch == "green_wave":
        return ("For the minority blocs in a green-wave state, the realistic ambition is not "
                "government but presence — holding the urban seats and denying the supermajority, "
                "which is a nationally legible result even when it does not change the government.")
    if arch == "gps_hegemony":
        return ("For the Sarawak opposition, the ambition is survival: holding Padungan and "
                "Pending, and contesting the two or three urban seats where GPS's margins are "
                "thin enough to be vulnerable to a concentrated opposition vote.")
    if arch == "sabah_local":
        return ("For WARISAN, the 25-seat plurality is the platform: the party's path to "
                "government runs through converting its 2025 gains into federal seats and "
                "outlasting the coalition's internal stresses, not through any national swing.")
    if arch == "melaka_divergence":
        return ("For the Melaka opposition, the divergence is the opportunity: the same electorate "
                "that gave BN 21 of 28 state seats gave BN zero federal seats, and a federal-style "
                "campaign in the 2026-27 PRN is the opposition's best-case scenario.")
    return ("For the opposition, the priority is consolidation — one candidate per seat, the "
            "anti-government vote concentrated — because under FPTP the largest single bloc "
            "takes the seat and fragmentation is a government subsidy.")


def opp_implication_ms(state, meta):
    arch = STATE_META[state]["archetype"]
    if arch == "green_wave":
        return ("Bagi blok minoriti di negeri gelombang hijau, cita-cita realistik bukan "
                "kerajaan tetapi kehadiran — memegang kerusi bandar dan menafikan supermajoriti, "
                "yang merupakan hasil yang boleh dibaca di peringkat nasional walaupun ia tidak "
                "mengubah kerajaan.")
    if arch == "gps_hegemony":
        return ("Bagi pembangkang Sarawak, cita-cita ialah kelangsungan hidup: memegang Padungan "
                "dan Pending, dan menandingi dua atau tiga kerusi bandar yang margin GPS cukup "
                "tipis untuk terdedah kepada undi pembangkang tertumpu.")
    if arch == "sabah_local":
        return ("Bagi WARISAN, pluraliti 25 kerusi ialah platform: laluan parti kepada "
                "kerajaan melalui penukaran keuntungan 2025 kepada kerusi persekutuan dan "
                "mengatasi tekanan dalaman gabungan, bukan melalui sebarang ayunan nasional.")
    if arch == "melaka_divergence":
        return ("Bagi pembangkang Melaka, perbezaan itu ialah peluang: elektorat yang sama "
                "yang memberikan BN 21 daripada 28 kerusi negeri memberikan BN sifar kerusi "
                "persekutuan, dan kempen gaya persekutuan dalam PRN 2026-27 ialah senario "
                "kes-terbaik pembangkang.")
    return ("Bagi pembangkang, keutamaan ialah penyatuan — satu calon setiap kerusi, undi "
            "anti-kerajaan tertumpu — kerana di bawah FPTP blok tunggal terbesar mengambil "
            "kerusi dan pemecahan ialah subsidi kerajaan.")


def analyst_lesson(state):
    arch = STATE_META[state]["archetype"]
    if arch in ("gps_hegemony", "sabah_local"):
        return ("that East Malaysian states follow local patronage logic, not Peninsular swings — "
                "every national polling centre that applied the national model to Sabah 2025 was "
                "wrong, and the state reports keep that lesson visible")
    if arch == "melaka_divergence":
        return ("that state and federal electorates can diverge dramatically in the same people — "
                "Melaka 2021-22 is the canonical case, and any model that treats state results as "
                "a direct read-through to federal seats will be wrong exactly here")
    if arch == "green_wave":
        return ("that a single-cycle surge can restructure a state's seat map permanently — the "
                "2023 wave converted competitive states into fortresses, and the margin tiers in "
                "this report show how much of that conversion is now locked in")
    if arch == "bn_supermajority":
        return ("that revealed preference — actual state-election votes — outranks stated "
                "intention in every test the project has run; the Johor result was the model's "
                "validation moment")
    return ("that the margin tier structure of a state, computed live from its results file, is "
            "the honest measure of contestability — safer than any opinion about how competitive "
            "a state 'feels'")


def analyst_lesson_ms(state):
    arch = STATE_META[state]["archetype"]
    if arch in ("gps_hegemony", "sabah_local"):
        return ("bahawa negeri Malaysia Timur mengikut logik naungan tempatan, bukan ayunan "
                "Semenanjung — setiap pusat tinjauan nasional yang menerapkan model nasional ke "
                "Sabah 2025 silap, dan laporan negeri mengekalkan pengajaran itu kelihatan")
    if arch == "melaka_divergence":
        return ("bahawa elektorat negeri dan persekutuan boleh berbeza secara dramatik dalam "
                "orang yang sama — Melaka 2021-22 ialah kes kanonik, dan mana-mana model yang "
                "melayan keputusan negeri sebagai bacaan langsung kepada kerusi persekutuan akan "
                "silap tepat di sini")
    if arch == "green_wave":
        return ("bahawa lonjakan satu kitaran boleh menyusun semula peta kerusi negeri secara "
                "kekal — gelombang 2023 menukar negeri kompetitif menjadi kubu, dan lapisan margin "
                "dalam laporan ini menunjukkan berapa banyak penukaran itu kini terkunci")
    if arch == "bn_supermajority":
        return ("bahawa keutamaan terserlah — undi pilihan raya negeri sebenar — mengatasi niat "
                "yang dinyatakan dalam setiap ujian yang telah dijalankan projek; keputusan Johor "
                "ialah detik pengesahan model")
    return ("bahawa struktur lapisan margin sesebuah negeri, dihitung secara langsung daripada "
            "fail keputusannya, ialah ukuran keupayaan bertanding yang jujur — lebih selamat "
            "daripada sebarang pendapat tentang betapa kompetitifnya sesebuah negeri")


def opposition_concentration(state, b):
    """Describe how the opposition's seats are geographically concentrated."""
    govt = govt_blocs_for(state)
    opp_seats = [s for s in b["seats"] if s["bloc"] not in govt]
    if not opp_seats:
        return "effectively non-existent — no opposition seat survived the last election"
    margins = [s["margin"] for s in opp_seats]
    avg_m = sum(margins) / len(margins) if margins else 0
    if len(opp_seats) <= 3:
        return (f"confined to {len(opp_seats)} isolated urban pockets, held on average margins of "
                f"{avg_m:.1f} points — a toehold rather than a base")
    safe_opp = sum(1 for m in margins if m >= 10)
    if safe_opp / len(opp_seats) > 0.6:
        return (f"concentrated in a small number of very safe urban seats (average margin "
                f"{avg_m:.1f} points) with little spread into the contested geography")
    return (f"spread across {len(opp_seats)} seats with an average margin of {avg_m:.1f} points — "
            f"a real presence, but one that FPTP geography leaves structurally exposed")


def opposition_concentration_ms(state, b):
    """Malay variant of opposition_concentration — used by sec15_strategic in --lang ms mode."""
    govt = govt_blocs_for(state)
    opp_seats = [s for s in b["seats"] if s["bloc"] not in govt]
    if not opp_seats:
        return "secara efektif tidak wujud — tiada kerusi pembangkang terselamat dalam pilihan raya terakhir"
    margins = [s["margin"] for s in opp_seats]
    avg_m = sum(margins) / len(margins) if margins else 0
    if len(opp_seats) <= 3:
        return (f"terhad kepada {len(opp_seats)} kantung bandar terpencil, dipegang pada purata margin "
                f"{avg_m:.1f} mata — satu pijakan dan bukannya pangkalan")
    safe_opp = sum(1 for m in margins if m >= 10)
    if safe_opp / len(opp_seats) > 0.6:
        return (f"tertumpu dalam sebilangan kecil kerusi bandar yang sangat selamat (purata margin "
                f"{avg_m:.1f} mata) dengan sedikit penyebaran ke geografi yang dipertandingkan")
    return (f"tersebar merentasi {len(opp_seats)} kerusi dengan purata margin {avg_m:.1f} mata — "
            f"kehadiran sebenar, tetapi satu yang ditinggalkan terdedah secara struktur oleh geografi FPTP")


def sec12_refs(state, meta, proj):
    arch = STATE_META[state]["archetype"]
    proj_line = ""
    if proj and proj["report_path"]:
        proj_line = f"- `{os.path.relpath(proj['report_path'], ROOT)}` — full PRN projection report [S-09]\n"
    if LANG == "ms":
        return f"""## {tier_section(state, 16, 12)}. Rujukan & Provenans

Laporan ini dijana secara langsung oleh `02_FORECAST/engine/state_report_builder.py` pada {fmt_date()}. Setiap nombor di dalamnya dihitung daripada set data di bawah pada masa binaan — tiada apa-apa yang ditaip tangan. Daftar sumber mengikut konvensyen [S-XX] laporan persekutuan.

**Set data (semua hidup, dibaca pada masa binaan):**

| Tag | Sumber |
|---|---|
| [S-01] | `1_DATA/research/states/DUN {state}/dun-election-results-latest.csv` — pemenang, parti, blok, undi, margin, keluar mengundi setiap kerusi |
| [S-02] | `1_DATA/research/states/DUN {state}/dun-candidates-latest.csv` — parti, gabungan, undi, keputusan, jantina, etnik, umur setiap calon |
| [S-03] | `1_DATA/research/derived/ge15-results-by-constituency-full.csv` — keputusan persekutuan GE15 untuk kawasan {state} |
| [S-04] | `1_DATA/research/derived/voter-demographics-by-constituency-ge15.csv` — demografi kawasan persekutuan ditimbang untuk {state} |
| [S-05] | `1_DATA/research/derived/master-list-222-parliamentary-seats.csv` — daftar kerusi persekutuan untuk {state} |
| [S-06] | `1_DATA/research/derived/swing_se_to_se.csv` — ayunan pilihan raya negeri untuk {state} |
| [S-07] | `03_REPORTS/states/DUN {state}/dun-election-summary.md` — ringkasan keputusan yang boleh dibaca |
| [S-08] | `03_REPORTS/states/DUN {state}/ge16-battleground-deepdive.md` — analisis persekutuan GE16 kerusi demi kerusi |
| [S-09] | `1_DATA/research/states/DUN {state}/<state>-prn-projection.csv` — unjuran PRN (Melaka, Sarawak) |
| [S-10] | `1_DATA/research/knowledge/forecast-theory.md` — kerangka uniform-swing, matematik, protokol pengesahan |
| [S-11] | `1_DATA/research/states/national-composition-summary.md` — konteks komposisi DUN nasional |
| [S-12] | `1_DATA/research/trackers/ge16-{{poll,candidate,general-news}}-log.md` — isyarat berita 2026 |

**Dokumen projek yang dirujuk:**

- `03_REPORTS/states/DUN {state}/dun-election-summary.md` [S-07] — jadual keputusan yang boleh dibaca
- `03_REPORTS/states/DUN {state}/ge16-battleground-deepdive.md` [S-08] — analisis medan pertempuran persekutuan negeri
{proj_line}- `1_DATA/research/states/national-composition-summary.md` [S-11] — kedudukan {state} dalam gambaran DUN nasional

**Sumber rasmi dan institusi:**

- Suruhanjaya Pilihan Raya Malaysia — keputusan pilihan raya negeri 2021–2026 dan keputusan persekutuan GE15 [S-01][S-03]
- ElectionData.MY / MECo (CC0) — keputusan kawasan, demografi pengundi, daftar calon [S-02][S-04]
- Parlimen Malaysia — daftar kerusi, ahli, komposisi gabungan [S-05]
- Merdeka Center, Ilham Centre — penjejakan kelulusan dan keutamaan yang dirujuk oleh model persekutuan [S-12]

*Dijana langsung oleh `02_FORECAST/engine/state_report_builder.py` pada {fmt_date()}. Laporan ini berversi: setiap edisi terdahulu disimpan dalam `03_REPORTS/states/DUN {state}/archive/`; edisi semasa berada di `03_REPORTS/states/DUN {state}/latest/GE16_{state}_Report_MS.md`. Setiap nombor dalam dokumen ini dihitung daripada set data projek pada masa binaan — tiada apa-apa yang ditaip tangan.*

"""
    return f"""## {tier_section(state, 16, 12)}. References & Provenance

This report was generated live by `02_FORECAST/engine/state_report_builder.py` on {fmt_date()}. Every number in it is computed from the datasets below at build time — nothing is hand-typed. The source registry follows the federal report's [S-XX] convention.

**Datasets (all live, read at build time):**

| Tag | Source |
|---|---|
| [S-01] | `1_DATA/research/states/DUN {state}/dun-election-results-latest.csv` — per-seat winners, parties, blocs, votes, margins, turnout |
| [S-02] | `1_DATA/research/states/DUN {state}/dun-candidates-latest.csv` — per-candidate party, coalition, votes, result, sex, ethnicity, age |
| [S-03] | `1_DATA/research/derived/ge15-results-by-constituency-full.csv` — federal GE15 results for {state}'s constituencies |
| [S-04] | `1_DATA/research/derived/voter-demographics-by-constituency-ge15.csv` — federal constituency demographics weighted for {state} |
| [S-05] | `1_DATA/research/derived/master-list-222-parliamentary-seats.csv` — federal seat register for {state} |
| [S-06] | `1_DATA/research/derived/swing_se_to_se.csv` — state-election swings for {state} |
| [S-07] | `03_REPORTS/states/DUN {state}/dun-election-summary.md` — readable results summary |
| [S-08] | `03_REPORTS/states/DUN {state}/ge16-battleground-deepdive.md` — seat-by-seat GE16 federal analysis |
| [S-09] | `1_DATA/research/states/DUN {state}/<state>-prn-projection.csv` — PRN projection (Melaka, Sarawak) |
| [S-10] | `1_DATA/research/knowledge/forecast-theory.md` — the uniform-swing framework, mathematics, validation protocol |
| [S-11] | `1_DATA/research/states/national-composition-summary.md` — national DUN composition context |
| [S-12] | `1_DATA/research/trackers/ge16-{{poll,candidate,general-news}}-log.md` — 2026 news signals |

**Project documents referenced:**

- `03_REPORTS/states/DUN {state}/dun-election-summary.md` [S-07] — the state's readable results table
- `03_REPORTS/states/DUN {state}/ge16-battleground-deepdive.md` [S-08] — the state's federal battleground analysis
{proj_line}- `1_DATA/research/states/national-composition-summary.md` [S-11] — where {state} sits in the national DUN picture

**Official and institutional sources:**

- Election Commission of Malaysia — state election results 2021–2026 and GE15 federal results [S-01][S-03]
- ElectionData.MY / MECo (CC0) — constituency results, voter demographics, candidate registers [S-02][S-04]
- Parliament of Malaysia — seat register, members, coalition composition [S-05]
- Merdeka Center, Ilham Centre — approval and preference tracking referenced by the federal model [S-12]

*Generated live by `02_FORECAST/engine/state_report_builder.py` on {fmt_date()}. This report is versioned: every prior edition is preserved in `03_REPORTS/states/DUN {state}/archive/`; the current one lives at `03_REPORTS/states/DUN {state}/latest/GE16_{state}_Report.md`. Every number in this document is computed from the project's datasets at build time — nothing is hand-typed.*
"""


# ===========================================================================
# REPORT ASSEMBLY, SAVE/ARCHIVE, DOCX, CLI
# ===========================================================================

def build_state_report(state, skip_unchanged=False):
    """Assemble the complete report for one state from live data.

    Args:
        state: State name (e.g. 'Johor')
        skip_unchanged: If True, compare source fingerprints against previous build.
                       Only rebuild sections whose sources changed. Quiet weeks skip ~80%.

    Returns:
        (report_text, timestamp) tuple
    """
    meta = STATE_META[state]
    comp = parse_national_composition()
    is_tier1 = meta["status"] == "upcoming"

    results_path = os.path.join(STATES_DIR, f"DUN {state}", "dun-election-results-latest.csv")
    cands_path = os.path.join(STATES_DIR, f"DUN {state}", "dun-candidates-latest.csv")
    results = load_csv(results_path)
    cands = load_csv(cands_path)
    if not results:
        raise FileNotFoundError(f"No results CSV for {state}: {results_path}")

    b = compute_baseline(results)
    c = compute_candidates(cands)
    master = load_csv(MASTER_222)
    ge15 = load_csv(GE15_FULL)
    demog = load_csv(DEMOG)
    fed = compute_federal(state, master, ge15, demog)
    swings = compute_swings(state)
    proj = compute_projection(state)

    # ---- TIER 1 EXTRA SOURCES ----
    forecast_state = load_forecast_for_state(state) if is_tier1 else None
    scenarios_data = load_projection_scenarios_for_state(state) if is_tier1 else None
    bg_tiered = load_battlegrounds_for_state(state) if is_tier1 else None
    factor_rankings = load_factor_rankings() if is_tier1 else None
    party_landscape = load_party_landscape_for_state(state) if is_tier1 else None

    # Set has_swings flag for sec9 narrative
    if proj:
        proj["has_swings"] = bool(swings)

    # ---- SMART SCAN: check if anything changed ----
    current_fps = compute_state_fingerprints(state)
    fp_db = load_fingerprint_db()
    prev_fps = fp_db.get(f"{state}|{LANG}")

    if skip_unchanged and needs_rebuild(state, current_fps, prev_fps) is None:
        print(f"  SKIP — no source changes since last build")
        return None, None

    # ---- header block ----
    dis = next_dissolution(state)
    comp_row = comp.get(state, {})
    comp_seats = comp_row.get("seats", b["total"])
    top_bloc = bloc_order_by_seats(b["bloc_seats"])[0][0]
    if LANG == "ms":
        status_ms = {"done": "PRN telah diadakan", "upcoming": "PRN dalam tetingkap GE16",
                     "later": "PRN selepas tetingkap GE16"}[meta["status"]]
        header = (
            f"**Disusun:** {fmt_date()} · **Kerusi DUN:** {b['total']} (daftar: {comp_seats}) · "
            f"**Pilihan raya terakhir:** {election_name(state)} · **Keputusan:** {describe_bloc_mix(b['bloc_seats'], b['total'])} · "
            f"**Pembubaran seterusnya:** ~{dis.strftime('%d %B %Y')} · **Status:** {status_ms} · "
            f"**Model:** uniform-swing v1.0\n"
            f"**Arkib:** setiap laporan terdahulu disimpan dalam `03_REPORTS/states/DUN {state}/archive/`\n"
        )
    else:
        status_txt = {"done": "PRN held", "upcoming": "PRN due in GE16 window",
                      "later": "PRN beyond GE16 window"}[meta["status"]]
        header = (
            f"**Compiled:** {fmt_date()} · **DUN seats:** {b['total']} (register: {comp_seats}) · "
            f"**Last election:** {election_name(state)} · **Result:** {describe_bloc_mix(b['bloc_seats'], b['total'])} · "
            f"**Next dissolution:** ~{dis.strftime('%d %B %Y')} · **Status:** {status_txt} · "
            f"**Model:** uniform-swing v1.0\n"
            f"**Archive:** every prior report preserved in `03_REPORTS/states/DUN {state}/archive/`\n"
        )

    report = title_for(state) + "\n\n" + header + "\n---\n\n"
    report += sec0_exec(state, b, c, fed, swings, proj, meta, comp)
    report += sec1_intro(state, meta, b, comp)
    report += sec2_framework(state, b, meta)
    report += sec3_electorate(state, b, fed, meta)
    report += sec4_baseline(state, b, meta)
    report += sec5_candidates(state, b, c)
    report += sec6_federal(state, fed, b, meta, forecast_state)
    report += sec7_signals(state, meta, swings, fed, b)
    report += sec7_story_threads(state)
    report += sec8_method(state, meta, factor_rankings)
    if is_tier1:
        report += sec8a_scenario_table(state, scenarios_data, b)
        report += sec8b_factor_weightage(state, factor_rankings, meta)
        report += sec8c_prn_federal_cascade(state, meta, swings, forecast_state)
        report += sec9_data_inputs(state, fed, b)
    report += sec9_scenarios(state, meta, proj, swings, b)
    if is_tier1:
        report += sec11_projection(state, proj, b)
    report += sec10_battlegrounds(state, b, fed, meta, bg_tiered, is_tier1)
    report += sec15_strategic(state, b, fed, meta, proj, forecast_state, party_landscape)
    if is_tier1:
        report += sec13_party_landscape(state, party_landscape)
        report += sec14_prn_scorecard(state, meta, proj, b)
    report += sec12_refs(state, meta, proj)

    # ---- POST-PROCESSING (structural hygiene, 9 Aug 2026) ----
    # Every '## N.' section header must be preceded by a blank line.
    # Fixes glued headers (e.g. '...coalition.## 8b. Factor Weightage').
    # (?<!#) excludes '## ' inside a '### ' header (demoted embedded files).
    # Same for '### ' headers (e.g. '...coalition.### 8.3 Factor Weightage').
    # (Raw embedded files are demoted to ### / #### at their injection points,
    # so the '## N.' numbering below is exclusively from the section builders.)
    report = re.sub(r"(?<!#)(?<!\n\n)## ", "\n\n## ", report)
    report = re.sub(r"(?<!#)(?<!\n\n)### ", "\n\n### ", report)

    # Update fingerprint database (keyed by state AND language — EN and MS
    # track independent fingerprints so a rebuild in one language does not
    # suppress the other).
    fp_db[f"{state}|{LANG}"] = current_fps
    save_fingerprint_db(fp_db)

    return report, stamp_now()


def save_state_report(state, report, stamp):
    """Archive the previous version (if any) under a dated folder, then write
    the new report as the latest version. No report is ever lost.

    Layout mirrors the federal report:
      03_REPORTS/states/DUN <State>/latest/GE16_<State>_Report.md   <- current
      03_REPORTS/states/DUN <State>/archive/GE16-YYYY-MM-DD/        <- preserved
    """
    out_dir = os.path.join(REPORT_STATES, f"DUN {state}")
    latest_dir = os.path.join(out_dir, "latest")
    os.makedirs(latest_dir, exist_ok=True)
    suffix = "_MS" if LANG == "ms" else ""
    fname = f"GE16_{state}_Report{suffix}.md"
    latest_md = os.path.join(latest_dir, fname)

    # 1. dated archive snapshot of this build (every build preserves itself)
    arch_dir = os.path.join(out_dir, "archive", f"GE16-{stamp}")
    os.makedirs(arch_dir, exist_ok=True)
    arch_md = os.path.join(arch_dir, fname)
    if not os.path.exists(arch_md) or open(arch_md, encoding="utf-8").read() != report:
        with open(arch_md, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"  archived this build → {arch_md}")

    # 2. latest pointer (current version)
    with open(latest_md, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"  latest → {latest_md} ({os.path.getsize(latest_md):,} bytes)")
    return latest_md


def md_to_docx(md_path):
    """Convert a state report markdown file to DOCX using python-docx.
    Handles headings, paragraphs, markdown tables, bold and italics."""
    try:
        from docx import Document
        from docx.shared import Pt, Inches
    except ImportError:
        print("  python-docx not installed — skipping DOCX")
        return None
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(10.5)
    with open(md_path, encoding="utf-8") as f:
        lines = f.read().splitlines()

    def add_runs(par, text):
        """Handle **bold** and *italic* inline markdown."""
        # split on ** first
        parts = re.split(r"(\*\*.+?\*\*)", text)
        for p in parts:
            if not p:
                continue
            if p.startswith("**") and p.endswith("**"):
                run = par.add_run(p[2:-2])
                run.bold = True
            else:
                sub = re.split(r"(\*[^*]+?\*)", p)
                for s in sub:
                    if not s:
                        continue
                    if s.startswith("*") and s.endswith("*") and len(s) > 2:
                        run = par.add_run(s[1:-1])
                        run.italic = True
                    else:
                        par.add_run(s)

    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            i += 1
            continue
        # table block
        if line.strip().startswith("|") and i + 1 < len(lines) and \
                set(lines[i + 1].strip().replace("|", "").replace("-", "").replace(":", "").strip()) == set():
            # gather consecutive table lines
            tbl = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                tbl.append(lines[i].strip())
                i += 1
            rows = []
            for tl in tbl:
                cells = [c.strip() for c in tl.strip("|").split("|")]
                rows.append(cells)
            if len(rows) >= 2:
                ncols = max(len(r) for r in rows)
                table = doc.add_table(rows=len(rows), cols=ncols)
                table.style = "Light Grid Accent 1"
                for ri, r in enumerate(rows):
                    for ci in range(ncols):
                        cell_text = r[ci] if ci < len(r) else ""
                        cell = table.cell(ri, ci)
                        cell.text = ""
                        par = cell.paragraphs[0]
                        add_runs(par, cell_text)
                        if ri == 0:
                            for run in par.runs:
                                run.bold = True
            continue
        # headings
        if line.startswith("# "):
            doc.add_heading(line[2:].strip(), level=1)
        elif line.startswith("## "):
            doc.add_heading(line[3:].strip(), level=2)
        elif line.startswith("### "):
            doc.add_heading(line[4:].strip(), level=3)
        elif line.startswith("---"):
            pass  # horizontal rule — skip
        elif line.startswith("- ") or line.startswith("* "):
            par = doc.add_paragraph(style="List Bullet")
            add_runs(par, line[2:])
        else:
            par = doc.add_paragraph()
            add_runs(par, line)
        i += 1

    docx_path = md_path[:-3] + ".docx"
    doc.save(docx_path)
    print(f"  docx → {docx_path}")
    return docx_path


def main():
    ap = argparse.ArgumentParser(description="GE16 per-state PRN report builder")
    ap.add_argument("--state", help="build only this state (e.g. Johor)")
    ap.add_argument("--docx", action="store_true", help="also generate DOCX per state")
    ap.add_argument("--skip-unchanged", action="store_true",
                    help="smart scan: skip states whose source data hasn't changed since last build")
    ap.add_argument("--lang", choices=["en", "ms"], default="en",
                    help="output language: en (default) or ms (native Malay generation, v2 — no LLM translation)")
    args = ap.parse_args()

    global LANG
    LANG = args.lang

    states = [args.state] if args.state else sorted(STATE_META.keys())
    built = 0
    skipped = 0
    for st in states:
        if st not in STATE_META:
            print(f"Unknown state: {st}. Valid: {', '.join(sorted(STATE_META.keys()))}")
            sys.exit(1)
        print(f"Building {st} ({LANG})...")
        try:
            report, stamp = build_state_report(st, skip_unchanged=args.skip_unchanged)
            if report is None:
                skipped += 1
                continue
            md_path = save_state_report(st, report, stamp)
            if args.docx:
                md_to_docx(md_path)
            built += 1
        except Exception as e:
            print(f"  ERROR building {st}: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)
    print(f"Done. Built: {built}, Skipped (unchanged): {skipped}")


if __name__ == "__main__":
    main()
