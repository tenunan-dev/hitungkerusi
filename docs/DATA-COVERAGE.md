# Data Coverage — GE16 canonical tree

**Machine-readable companion:** `data/canonical/research/derived/data-coverage.json`
(`ge16.data-coverage.v1`). This document explains what the numbers mean;
that file carries the numbers. Regenerate both whenever a phase changes
what canonical holds. Counts below are as of edition
`20260929T104845Z` + the P2.5 events baseline (2026-10-01).

## The one rule

**What a tracker saw is not a verified fact.** Tracker rows are recorded
as `tracker-note` evidence; only rows that carry an *accept* judgment are
durable corpus. Nothing in the forecast layer may treat tracker links as
verified polls without checking record meaning (P2.9 charter).

## Coverage by dataset

| Dataset | Count | What it is | Verified? |
|---|---|---|---|
| News evidence | 3,717 rows | Accepted/pending/candidate news items from 9 feeds | accepted subset only |
| Tracker-note evidence | 18,463 rows | What the 3 trackers saw (news 18,179 / polls 234 / candidates 50) | **No — observations of trackers** |
| Judgments | 3,006 accept | 1,261 inline corpus + 934 judged-batch + 811 orphaned-flag | yes (by definition) |
| Accepted-news archive | 2,367 items | Durable superset of rolling feeds, window 2026-01-01 → 2026-09-29 | yes |
| Events DB | 512 events / 2,141 entities / 117 stories / 82 notes | P2.5 V2 baseline (person 1,236 / seat 828 / party 30 / institution 22 / state 16 / bloc 9) | V2-reviewed |
| Seat coverage | **222/222 federal + 600 DUN** | 13 CSVs + territories; geo ×13 states | structural |

## Coverage by source (news publishers)

172 distinct publishers in accepted news. Largest: Free Malaysia Today
(499), Google News syndication (302), The Star (226+144 GN), The Vibes
(207), Malay Mail (199+138 GN), NST (185+147 GN), Malaysiakini (147+72
GN), The Edge Malaysia (121+101 GN). Full table in the JSON companion.

## What is deliberately NOT counted yet

- **Links, vectors, polls store** — builders are code-complete,
  review-approved, and sandbox-proven (P2.8, through R4 APPROVE), but the
  **live promotions have not run**; `links/` is empty. Coverage rows for
  them appear in the JSON as `"status": "NOT BUILT LIVE"` rather than as
  fabricated numbers.
- **Live additive merge** — sandbox-proven at 512 V2 + 2,217 V3 = 2,729
  events; live canonical still shows the P2.5 baseline until integration
  runs it.
- **Orphaned-flag re-judge** — machinery ready (498-row target), run not
  yet executed.

## Known limitations

1. News window opens 2026-01-01 — pre-2026 coverage exists only where V2
   carried it.
2. The checkpoint file registers one consolidated `news` source_id; per-
   feed checkpoints arrive with the next incremental cycle.
3. The 811 orphaned-flag judgments are *flag* provenance, not batch
   provenance — the 498-row subset is queued for re-judgment.
4. Publisher names are taken from the collector's `source` field as-is
   (e.g. `[GN]` suffix = Google News syndication of that outlet); no
   dedupe across syndication variants has been applied.

*Update rule: this page is regenerated at every plan milestone that
changes canonical contents (same trigger as ARCHITECTURE.md updates), and
the JSON companion's `generated_at_edition` must name the edition the
numbers were taken from.*
