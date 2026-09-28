#!/usr/bin/env python3
"""GE16 Forecast Engine — weekly CONFIG. Edit THIS file each week; never the engine.

Reference: ../../knowledge/forecast-theory.md (theory) and
../../knowledge/forecast-factor-rankings.md (factor sources).
"""

# =====================================================================
# FACTOR WEIGHTS (per forecast-theory.md §3) — revise only with the basis doc
# =====================================================================
FACTORS = {
    "state_swing": 0.40,   # revealed preference: latest state elections (boundary-grouped)
    "approval": 0.22,      # govt approval (Merdeka/Ilham)
    "economy": 0.18,       # inflation / cost-of-living / growth
    "leader": 0.08,        # PM-candidate preference by ethnicity
    "turnout": 0.04,       # youth turnout differentials
    "events": 0.08,        # coalition ruptures, third-force, shocks
}

# =====================================================================
# CURRENT MACRO READINGS — update weekly from news/DOSM/BNM
# =====================================================================
MACRO = {
    "gdp_yoy": 6.0,        # % — Q2 2026 ACTUAL (DOSM/BNM 14 Aug 2026; Q1 5.4, Q4-2025 6.2, H1 5.7)
    "cpi_yoy": 1.9,        # % — Aug 2026 1.9 (18 Sep release), Jul 1.8 (below 2% target band)
    "ringgit": 4.0816,     # MYR/USD — 24 Sep 2026 BNM spot (2026 range 3.9175-4.1480; model-neutral)
    "approval_delta": 3.0, # pp — Merdeka Center Mar-Apr 2026 fieldwork (pub 25 Jun 2026):
                           #   Anwar 52% overall, govt 50%; Malay-only 45% (ranks 4th behind
                           #   Khairy 62/Muhyiddin 49/Samsuri 48). "vs GE15" delta not publicly
                           #   verifiable — retained as standing assumption pending a
                           #   re-baselined anchor.
    "pm_pref_malay": -7.0, # pp shift — Merdeka Center national survey, fieldwork
                           #   2026-03-12→04-09 (published 25 Jun 2026): Anwar 45% among
                           #   Malay voters vs 52% overall = −7 pp.
    "pm_pref_nonmalay": 7.0, # pp shift — Merdeka Center same survey: Anwar 59% among
                           #   non-Malay voters vs 52% overall = +7 pp.
}

# =====================================================================
# EVENT SHOCKS — {seat_code: {bloc: pp}} — update from candidate tracker/news
# =====================================================================
EVENT_SHOCKS = {
    # No seat-level event shocks are currently carried. Reviewed 2026-09-24
    # against work/events/ge16-events.db; removals below.
    #
    # REMOVED — Bersatu-rump PN -6 at P074/P091/P143/P154/P183: premise
    # contradicted by Muhyiddin 19 Sep 2026 "Bersatu will not go solo in GE16"
    # (NST/FMT) and by PN's admission of Wawasan/Pejuang 22 Jun 2026; none of
    # the five seats belongs to a sacked-Bersatu MP (corroborated sacked seats
    # (Feb 2026): P054, P056, P061, P184).
    # REMOVED — P146 {"PH": -3}: Muar is MUDA (Syed Saddiq), not PH;
    # mis-attribution.
    # REMOVED — P100/P118 {"PH": -3}: double-count. The double-count source is
    # the SCENARIO LAYER, not the vacancy metadata:
    # SCENARIO_DEFS["Full fragmentation (Bersatu −6 + Bersama −3 PH urban)"]
    # already applies {"PH": -3} to EVERY seat (the Bersama-siphon term) and its
    # bersatu_penalty=6 stacks on top of the removed Bersatu-rump PN shocks, so
    # a seat-level P100/P118 {"PH": -3} shock charged the same Bersama siphon a
    # second time. The vacancy metadata itself has ZERO numeric effect:
    # VACANCIES / df["vacated"] is consumed nowhere on the numeric path (it only
    # populates the "vacancy_status" metadata field).
    # Vacancies P100/P104/P118 are tracked in VACANCIES with baseline
    # attribution held.
}

# =====================================================================
# VACANCIES (anti-hopping law, Art 49A) — seats legally vacated by defection
# =====================================================================
# A vacancy is occupancy metadata, NOT a swing and NOT removal from the 222-seat
# universe: the seat keeps its last recorded holder as the baseline attribution
# and is contested fresh at the general election. Seats vacated by RESIGNATION or
# an independent joining a party (Art 49A(2)); seats kept on expulsion (49A(2)(c)).
# Format: code -> reason. See 01_RESEARCH/knowledge/anti-hopping-law-factor.md.
VACANCIES = {
    # "P000": "resigned DAP -> MCA (Art 49A(2)(a))",   # example only
    # VACANCY TYPE KEY (9 Aug 2026):
    #   Type A = Speaker notified EC → by-election held/scheduled (e.g. Kinabatangan Jan 2026)
    #   Type B = Speaker did NOT notify → seat stays vacant until GE16 (Art 49A)
    # Synced from 01_RESEARCH/data/derived/master-list-222-parliamentary-seats.csv (party=VAC).
    "P100": "Rafizi Ramli resigned PH for Bersama (Art 49A(1)(a)); TYPE B — Speaker not notified, EC ruled no by-election 20 May 2026, vacant until GE16",
    "P104": "Wong Chen resigned PH for Bersama (Art 49A(1)(a)); TYPE B — Speaker notified EC 11 Aug 2026, EC ruled no by-election 12 Aug 2026, vacant until GE16",
    "P118": "Nik Nazmi Nik Ahmad resigned PH for Bersama (Art 49A(1)(a)); TYPE B — Speaker not notified, EC ruled no by-election 20 May 2026, vacant until GE16",
}

# =====================================================================
# TYPE MODULATION (theory §7): seat-type -> {bloc: multiplier}
# =====================================================================
TYPE_MOD = {
    "pn_core":            {"PN": 1.2, "PH": 0.8, "BN": 1.0},
    "mixed_malay":        {"PN": 1.1, "PH": 0.9, "BN": 1.0},
    "true_mixed":         {"PN": 0.9, "PH": 1.1, "BN": 1.0},
    "non_malay":          {"PN": 0.7, "PH": 1.2, "BN": 1.0},
    "east_malaysia":      {"PN": 0.8, "PH": 1.0, "BN": 1.0, "GPS": 1.0, "GRS": 1.0},
}

# Monte Carlo settings
MC_ITERATIONS = 5000
MC_SEED = 42
