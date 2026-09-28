"""Approved canonical methodology imports shared by extraction and validation."""

import re

# Canonical inputs are selected by P2.1 disposition (canonical-input-candidate),
# never by filename: the tuples below are the approved methodology imports by
# legacy+canonical path pair, and nothing is excluded by name anywhere (the
# P1.7 R1 transient-filename exclusion was reverted). Dispositions for the
# tracker/backfill artifacts live in evidence/P2/P2.1-artifact-classification.json.
METHODOLOGY_INPUTS = (
    (
        "01_RESEARCH/data/notes/byelections-malaysia-1957-2026.md",
        "research/data/notes/byelections-malaysia-1957-2026.md",
        ("historical-baseline", "forecast-modeling"),
    ),
    (
        "01_RESEARCH/data/notes/election-study-organizations-malaysia.md",
        "research/data/notes/election-study-organizations-malaysia.md",
        ("research-methodology",),
    ),
    (
        "01_RESEARCH/data/notes/ge15-candidates-demographics.md",
        "research/data/notes/ge15-candidates-demographics.md",
        ("demographic-analysis",),
    ),
    (
        "01_RESEARCH/data/notes/ge15-results-by-state.md",
        "research/data/notes/ge15-results-by-state.md",
        ("results-analysis",),
    ),
    (
        "01_RESEARCH/data/notes/marginal-seats-ge15.md",
        "research/data/notes/marginal-seats-ge15.md",
        ("seat-prioritization",),
    ),
    (
        "01_RESEARCH/data/notes/parliamentary-seats-malaysia-data.md",
        "research/data/notes/parliamentary-seats-malaysia-data.md",
        ("constituency-reference",),
    ),
    (
        "01_RESEARCH/data/notes/party-landscape-update-2026.md",
        "research/data/notes/party-landscape-update-2026.md",
        ("party-analysis", "forecast-modeling"),
    ),
    (
        "01_RESEARCH/data/notes/voter-demographics-by-constituency-ge15.md",
        "research/data/notes/voter-demographics-by-constituency-ge15.md",
        ("demographic-analysis",),
    ),
    (
        "01_RESEARCH/knowledge/anti-hopping-law-factor.md",
        "research/knowledge/anti-hopping-law-factor.md",
        ("institutional-analysis",),
    ),
    (
        "01_RESEARCH/knowledge/demographics-parties-analysis.md",
        "research/knowledge/demographics-parties-analysis.md",
        ("demographic-analysis", "party-analysis"),
    ),
    (
        "01_RESEARCH/knowledge/forecast-factor-rankings.md",
        "research/knowledge/forecast-factor-rankings.md",
        ("forecast-modeling",),
    ),
    (
        "01_RESEARCH/knowledge/forecast-theory.md",
        "research/knowledge/forecast-theory.md",
        ("forecast-modeling",),
    ),
    (
        "01_RESEARCH/knowledge/political-parties.md",
        "research/knowledge/political-parties.md",
        ("party-analysis",),
    ),
    (
        "01_RESEARCH/knowledge/prn-prediction-scorecard.md",
        "research/knowledge/prn-prediction-scorecard.md",
        ("state-forecasting",),
    ),
)

METHODOLOGY_INPUT_PATH_PAIRS = tuple(
    (legacy_path, canonical_path)
    for legacy_path, canonical_path, _ in METHODOLOGY_INPUTS
)
METHODOLOGY_CONSUMER_ROLES = {
    legacy_path: consumer_roles
    for legacy_path, _, consumer_roles in METHODOLOGY_INPUTS
}
METHODOLOGY_DESTINATION_ROOTS = ("research/data/notes", "research/knowledge")
