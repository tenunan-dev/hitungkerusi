# Forecast Methodology — HitungKerusi 222 (factor model v1.0)

## 1. What the model is

A per-constituency deterministic swing projection plus a Monte Carlo
uncertainty layer, wrapped in a scenario generator. It produces, in one run:

- a deterministic seat-by-seat winner projection across the 222-seat universe,
- a Monte Carlo distribution of total government-bloc seats,
- a scenario set (quantitative + evidence-backed narrative) that reuses the
  SAME machinery, and
- an auditable JSON snapshot stamped with provenance digests.

## 2. Input data (all canonical, single-source-of-truth)

| source | role | where |
|---|---|---|
| `ge16-projection-model.csv` | verified GE15 per-seat winner / runner-up / margin base | `research/federal/` |
| `ge15-results-by-constituency-full.csv` | GE15 result + state_std + constituency | `research/derived/` |
| `voter-demographics-by-constituency-ge15.csv` | ethnic % + youth cohort % per seat | `research/derived/` |
| `swing_se_to_se.csv` | post-GE15 revealed-preference state swings | `research/derived/` |
| `swing_boundary_grouped.csv` | boundary-grouped per-parliament swings (from child DUNs) | `research/derived/` |
| `config.py` (hand-curated) | FACTORS weights, MACRO readings, EVENT_SHOCKS | `engine/` |

## 3. Step 1 — Baseline assembly (`load_baseline`)

Three CSVs are merged on normalised seat code:
- projection model gives winner / runner-up / GE15 margin,
- GE15 full results give the standard state name,
- demographics give `malay_pct`, `chinese_pct`, `indian_pct`, Sabah/Sarawak
  bumiputera %, and youth share `youth_pct = age18_21 + age22_30`.

Also classifies each seat (`seat_type`) and annotates Art 49A vacancies. A
vacancy is legal/occupancy metadata only — the seat stays in the 222-seat
forecast universe with its last holder as the baseline attribution.

## 4. Step 2 — Swing map build (`build_swing_map`)

For every seat, start with a per-seat, per-bloc swing:

- **State swing** from `swing_se_to_se.csv` (Pahang excluded — concurrent with
  GE15, not fresh signal; the Penang/Malacca name renames are normalisation).
- **Boundary swing** from `swing_boundary_grouped.csv` takes precedence where
  a per-parliament DUN-derived number exists.
- **Economic term** `0.7·gdp_yoy − 0.8·(cpi_yoy − 2.0) + 0.15·approval_delta`
  (basis §8.4) is added to govt-aligned blocs (PH/BN/GPS/GRS) scaled by
  `FACTORS["economy"]` (0.18) → /0.20 × 0.10 pp.
- **Approval term** from Merdeka/Ilham, added to govt-aligned blocs
  via `FACTORS["approval"]` (0.22) → ×0.1/0.25.
- **Event shocks** (`EVENT_SHOCKS[code]`) — currently empty after the
  2026-09-24 review that removed 5 Bersatu-rump shocks and 2 misattributed
  double-counts so every remaining seat is only that seat's own fresh
  evidence.

Three modulation layers then shape the per-seat swing (applied multiplicatively
or additively within the same code block):

- **Ethnic modulation** — every seat's `malay_pct` continuously modulates each
  bloc's swing. PN multiplies by `0.60 + 0.80·m`; PH by `1.40 − 0.80·m`
  (ethnicity is the dominant predictor per ISEAS 2023 / Pepinsky 2023; PN scales
  with Malay share, PH inversely); BN gets a bump peaking near the ~60% Malay
  mixed-seat zone. GPS/GRS are held at 1.0 (Sabah/Sarawak ethnic logic is
  separate).
- **Youth modulation** — youth-heavy seats (above a 20% floor) amplify swings
  across all blocs by up to +30% via `1 + 0.30·max(0, youth−0.20)`.
- **Party-level modulation + Bersatu split** — each incumbent party has an
  ethnic "sweet spot" (a bell curve peaking at that party's characteristic
  Malay share, e.g. PAS 0.88, DAP 0.20, PKR 0.48). In Bersatu-held seats the
  PAS-Bersatu structural split applies: −12 pp to PN, +6 to BN, +3 to PH
  (documented danger: this layer is currently unreachable because
  `projection CSV` has no party column — the split effect is instead carried
  by `SCENARIO_DEFS` bersatu_penalty, so no double-count occurs).

## 5. Step 3 — Deterministic projection (`project_seats`)

For each seat: `proj_margin = margin_GE15 + swing(winner) − swing(runner-up)`.
The seat flips when `proj_margin < 0`. This is the whole rule: swing advantage
candidates move the margin, not a separate winner-calibration model.

## 6. Step 4 — Monte Carlo (`monte_carlo`, default 5000 iters, seed 42)

Same machinery, one sampled noise term per seat per iteration:
`noise ∼ N(0, 2.0 pp)` in seats where the GE15 margin was < 5 pp
(marginal, volatile), else `N(0, 1.2 pp)`. Winner per iteration recomputed
from the noisy margin. Output: govt-bloc seat total distribution `(P10, P50,
P90)`, `P(govt ≥ 112)` majority probability, and a flips distribution.

The seed is explicit and recorded in the run output — every run is bit-repro
for the same inputs. Vacancies are mortality-hidden inside the govt-vs-rest
totals only via which bloc the last holder belonged to; no seat ever
disappears from the 222 target.

## 7. Step 5 — Scenario layer (`build_scenarios`)

Two categories, one machinery:

- **parametric** — re-derives swing adjustments from `SCENARIO_DEFS` (a
  deterministic per-scenario swing multiplier on `build_swing_map`). "Base
  (swings)" is by construction identical to the deterministic projection —
  the same function, zero extra adjustments.
- **narrative** — never hand-written. Every narrative scenario must import a
  real accepted news record from the canonical news feed; Stage-3 seals the
  narrative against its digest binding. Missing evidence or dead feed_digest
  → fail closed naming the scenario.

Every scenario is re-normalised to Σ = 222 seats (vacancies restored to their
last holder's bloc). Assert-checked: any scenario summing to something other
than 222 aborts the run rather than exiting silently.

## 8. Step 6 — Output and provenance binding

A single JSON written to `work/forecast/latest/ge16-forecast-latest.json`
(in V3's working layout) contains:

- the economic term, deterministic composition, Monte Carlo percentiles,
- the full flip list with GE15-winner → projected-winner transitions,
- the projected seats record-by-record,
- vacancy accounting (filled = 222 − vacancies, projected = 222) with an
  explicit seat-accounting string printed for audit.
- scenario provenance bound via sha256 of the canonical feed bytes
  (`refresh_scenario_meta_evidence`), so a stale digest fails Stage 3's seal.

Scenario metadata never claims "database-driven analysis" — the engine is
CSV+config driven; the DB and trackers supply evidence that is consumed
through (a) human curation into config.py and (b) digest-bound narrative
scenarios. This is stated explicitly in the JSON's `model` field (factor-v1.0)
so consumers never over-trust it.

## 9. Known limits (stated on purpose)

- `youth_pct` coarsely affects all blocs; no econometric calibration of
  turnout by age yet.
- Approval delta is anchored to Merdeka Center Mar–Apr 2026 only; a re-baseline
  anchor is a standing assumption.
- Party-level modulation is currently unreachable on this path; a party column
  in the projection CSV would enable it (design comment document).
- The Bersatu-split structural effect is carried by the scenario layer, not
  the seat-level path; the unreachable helper documents why.
- No turnout model (all seats assumed full-turnout): vacancy-aware, but
  differential-turnout shifts are not yet modelled.
