# The Malaysian Election Forecast Model — Basis, Logic & Mathematics

**Version:** 1.0 (foundation) | **Compiled:** 3 August 2026 | **Status:** living document — basis for all future GE16 forecast updates
**Purpose:** This document is the *theory of change* behind every seat projection this project produces. It defines what moves Malaysian election results, why, and how those movements are converted into numbers. It is the skill that future weekly forecast updates will load and apply.

---

## Part I — The Elements: What Moves Election Results in Malaysia

> **Source appendix:** every element below is ranked and cited in detail in
> **`forecast-factor-rankings.md`** (same folder) — each factor's source(s) and what they say.
> This section is the condensed theory; that file is the provenance record.

### 1. The hierarchy of determinants (from the evidence)

Malaysian elections are not decided by a single factor but by a **nested hierarchy** — each layer conditions the ones below it. The empirical literature (ISEAS 2023/20; Pepinsky et al. 2023; Merdeka Center 2020–2022 daily tracking; Washida 2023) supports a four-layer structure:

| Layer | Determinant | Strength | Evidence |
|---|---|---|---|
| **L1 — Identity** | Ethnicity; region (Malay Belt vs mixed); East vs West Malaysia | **Dominant** | ISEAS: >80% non-Malay voted PH; ~57% Malay voted PN; 95% Chinese → PH. Pepinsky: ethnicity predicts outcome; urbanization adds little once ethnicity is controlled |
| **L2 — Valence** | Leader approval; coalition brand; corruption trust; "clean" reputation | **Strong** | Merdeka end-of-campaign: Muhyiddin 71% Malay approval vs Anwar 32%; Zahid 12%. Approval differentials moved 5–10pp within the campaign |
| **L3 — Performance** | Economy (growth, inflation, cost-of-living); governance; stability | **Medium, conditional** | Economic voting literature: ~1.4pp vote per 1pp GDP growth (Wilkin et al); sociotropic > pocketbook; but *secondary* to ethnicity in Malaysia — it decides *within-ethnic* choice (which Malay party, not whether Malay voters abandon ethnocentrism) |
| **L4 — Events** | Scandals, defections, coalition ruptures, third-force entries, electoral-timing shocks | **High variance, low frequency** | 2026: PAS–Bersatu split, WAWASAN, Bersama entry, redelineation, by-elections — each a potential 2–10pp shock concentrated in specific seats |

**The critical insight:** Layers 1–2 set the *baseline structure* of every seat (who wins a 90%-Malay rural seat vs a 60%-Chinese urban seat). Layer 3 shifts the *margins* within that structure. Layer 4 can *break* the structure itself in individual seats. A forecast that ignores L1 will be wrong in every seat; a forecast that ignores L4 will be wrong in the seats that matter.

### 2. The seat-level anatomy

Every seat is a combination of five fixed structural properties (from our datasets):

1. **Ethnic composition** — Malay/Chinese/Indian/Bumiputera-Sabah/Sarawak shares (voter-roll data, not census — the actual electorate)
2. **Age structure** — youth (18–30) share; elderly share; Undi18 concentration
3. **GE15 margin & bloc** — incumbent coalition, margin %, runner-up bloc
4. **State & region** — which state; Malay Belt vs mixed; East vs West; state-election swing history
5. **Electorate size** — determines vote-count thresholds and statistical noise

These five properties define a seat's *type*. The typology (from Pepinsky + our battleground analysis):

| Seat type | Malay % | Typical character | GE15 default | Battleground risk |
|---|---|---|---|---|
| **PN core** | >80% | Rural/urban Malay Belt; PAS machinery | PN | Low (safe) |
| **Mixed Malay-majority** | 55–80% | Semi-urban; contested | PN or BN | **High** |
| **True mixed** | 30–55% | Urban; three-cornered | PH or PN | **Highest** |
| **Non-Malay majority** | <30% | Urban; DAP/PKR strongholds | PH | Low (safe) |
| **Sabah/Sarawak** | Bumi-dominant | Patronage-based local coalitions | GPS/GRS/BN | Regional logic |

The 36 battlegrounds are almost entirely in the two middle rows — the seats where no identity group is dominant enough to pre-determine the outcome, and where L2–L4 factors therefore decide.

### 3. The dynamic factors (measurable, weekly-updatable)

These are the inputs a weekly forecast refresh can actually observe and quantify:

| Factor | Metric(s) | Source | Direction of effect | Weight |
|---|---|---|---|---|
| **Government approval** | PM + cabinet approval %; satisfaction by ethnicity | Merdeka, Ilham (poll tracker) | +1pp approval ≈ +0.1–0.2pp govt vote (Malaysia-specific calibration) | 0.25 |
| **Economic sentiment** | Inflation (CPI), ringgit, fuel/food prices, cost-of-living surveys | DOSM, BNM, news | High inflation/cost-of-living ≈ −2 to −4pp govt; sociotropic dominant | 0.20 |
| **Growth** | GDP growth, trade | DOSM | +1pp growth ≈ +1.4pp incumbent (global est.; dampen to 0.7 for coalition govts) | 0.10 |
| **State-election swings** | Latest SE vote-share changes per bloc | MECo (our DUN data) | The *measured* post-GE15 drift — our strongest signal | 0.30 |
| **Party/coalition events** | Split/merge/defection/entry (PAS–Bersatu, WAWASAN, Bersama) | News (candidate tracker) | Seat-specific shocks, ±2–10pp where they concentrate | event |
| **Leader ratings** | PM-candidate preference by ethnicity | Merdeka daily tracking | Swings Malay vs non-Malay blocs oppositely | 0.10 |
| **Turnout differentials** | Youth turnout vs overall; early voting | EC, Undi18 studies | Young voters tilt PH (but 2023 SEs show young Malay → PN); soft turnout hurts incumbents | 0.05 |

**Why state-election swings get the highest weight (0.30):** they are the only *revealed preference* data — actual votes cast since GE15, in the same ethnic/regional structure, measuring real movement rather than stated intention. Polls measure opinion; state elections measure behavior. Our projection model is therefore swing-based first, poll-calibrated second.

### 4. The event layer (discrete, model as shocks)

Events are not continuous factors; they are shocks applied to specific seats or blocs:

| Event type | Mechanism | Modelling approach |
|---|---|---|
| **Coalition rupture** (PAS–Bersatu) | Splits the bloc's vote in overlapping seats | Apply anti-PN swing in Bersatu-held seats (our −6/−10pp scenarios) |
| **Third-force entry** (Bersama) | Siphons urban protest votes from PH (Johor: 3–6% per seat) | Apply anti-PH swing in mixed/urban seats where it contests |
| **New party admission** (WAWASAN→PN) | Replaces a strong partner with a weak one; PAS dominance | Reduce PN's effective machinery advantage |
| **Redelineation** | Redraws boundaries; changes seat types | Rebuild seat map entirely (model invalidated until done) |
| **By-elections** | Early signal of drift in specific seats | Treat as mini-referenda; update state swing |
| **Scandal/graft** | Trust deficit concentrated in the affected party | Apply negative valence swing to that bloc, stronger in its core |
| **Electoral timing** | When polls are called (coattails, state-federal alignment) | Modulates turnout and nationalization of the campaign |

---

## Part II — The Logic: From Factors to Seats

### 5. The causal chain

The logic runs in four stages:

```
STAGE 1: STRUCTURE
Identity (L1) + valence (L2) + seat demographics
  → baseline vote share V0(s,b) for each bloc b in each seat s
  → derived from GE15 actuals (our empirical baseline, not a model)

STAGE 2: DRIFT
Performance (L3) + events (L4) + revealed preference (state swings)
  → national swing ΔV_nat(b) and state swings ΔV_st(b)
  → the *change* since GE15, measured or inferred

STAGE 3: TRANSLATION
Seat margin M(s) = M_GE15(s) + ΔV_winner(s) − ΔV_runnerup(s)
  → seat flips if M(s) < 0
  → this is a *deterministic* translation given the swings (our current model)
  → with uncertainty: P(flip) via probit on M(s)/σ(s)

STAGE 4: AGGREGATION
Sum over 222 seats → projected parliament
  → Monte Carlo over uncertain swings → P10/P50/P90 + majority probability
```

### 6. The four governing principles

1. **Baseline-first.** Never forecast from a blank slate. GE15 is the empirical anchor; everything is expressed as a *swing* from it. This is why our null model (no swings) reproduces GE15 exactly — the machinery is honest.

2. **Revealed preference over stated intention.** State-election swings outrank polls. Polls calibrate; state elections measure.

3. **Uniform within type, not within country.** A national swing is not applied uniformly — it is modulated by seat type. PN's swing concentrates in Malay-majority seats; PH's in mixed/non-Malay seats; a Malay-Belt swing ≠ a Penang swing. (Our current model applies per-state uniform swings; the v2 refinement applies type-modulated swings.)

4. **Uncertainty is part of the forecast.** Every projection is a distribution, not a point. The honest output is P10/P50/P90 and a majority probability — not a single seat count.

### 7. The type-modulated swing (v2 refinement, recommended)

The current model applies one swing per bloc per state. The evidence says this is too coarse: within a state, PN's 2023 surge was concentrated in Malay-majority seats. The refinement:

```
ΔV(s,b) = ΔV_state(b) × w_type(s)
```

where w_type(s) is the seat-type multiplier: PN swings ×1.2 in >80% Malay seats, ×0.8 in mixed; PH swings ×1.2 in non-Malay seats, ×0.9 in mixed; etc. The multipliers are calibrated from the 2023/2025/2026 state-election data (which we have at seat level via MECo).

---

## Part III — The Mathematics

### 8. The core equations

**8.1 Seat-level projected margin**

$$M_s = M_{s,GE15} + \sum_k \beta_k \cdot \Delta X_{k,s} + \varepsilon_s$$

where:
- $M_{s,GE15}$ = GE15 margin (% of two-party-preferred votes) in seat s
- $\Delta X_{k,s}$ = observed/measured change in factor k since GE15 (state swing, approval delta, inflation delta, event shock)
- $\beta_k$ = factor weight (Table in §3)
- $\varepsilon_s$ = seat-level error, $\varepsilon_s \sim \mathcal{N}(0, \sigma_s^2)$

**8.2 The probit flip probability**

$$P(\text{flip}_s) = \Phi\left(\frac{-M_s}{\sigma_s}\right)$$

where $\Phi$ is the standard normal CDF and $\sigma_s$ is the seat's uncertainty (electorate-size dependent: larger electorates → smaller proportional noise; battleground seats get σ = 2.0–3.0pp, safe seats σ = 1.0–1.5pp).

**8.3 Monte Carlo aggregation**

For N iterations (≥5,000):
1. Draw each bloc's national swing from its observed distribution (mean = measured, sd = historical swing volatility)
2. Compute $M_s$ for all 222 seats
3. Count seats per bloc; record government-aligned total
4. Output: P10/P50/P90 of govt seats; P(govt ≥ 112); flip counts

**8.4 Economic-voting calibration (evidence-based)**

From the cross-national literature (Wilkin et al.: 1.4pp per 1pp GDP growth; Lewis-Beck & Stegmaier review) dampened for coalition governments and Malaysia's ethnic structure:

$$\Delta V_{govt} = 0.7 \times \Delta GDP_{yoy} - 0.8 \times (\Delta CPI - 2\%) + 0.15 \times \Delta Appr$$

Example: GDP 5.2%, CPI 2.8%, approval +3pp → ΔV_govt = 0.7(5.2) − 0.8(0.8) + 0.15(3) = +3.64 − 0.64 + 0.45 = **+3.5pp** — a plausible mid-term government boost consistent with the 2025–26 BN resurgence.

### 9. Validation protocol

Every forecast version must pass three checks before release:

1. **Null-case reconstruction:** zero swings must reproduce GE15 exactly (passes today).
2. **Retrospective test:** apply the model to the 2023/2025/2026 state elections — did it predict the actual swings? (The 2026 Johor/N9 results confirmed our southern-resurgence scenario; the model's BN +15.6pp predicted a BN supermajority that materialized as 48/56.)
3. **Backcast to 2022:** would the model, run with 2022 inputs, have produced the GE15 outcome? (Qualitative pass: the green wave + BN collapse are both captured by the swing logic.)

### 10. Output contract (what every weekly forecast must deliver)

| Output | Format |
|---|---|
| Projected parliament (7 scenarios) | Table: bloc × scenario |
| Government majority | P10/P50/P90 + P(majority) |
| Flip list | Seat-level: GE15 → projected, with trigger |
| New signals since last week | What changed: polls, swings, events, candidates |
| Confidence statement | What the model can/cannot see this week |

---

## Part IV — Data Sources & Update Cadence

| Signal | Source | Frequency | Model input |
|---|---|---|---|
| State swings | MECo (ElectionData.MY) | After each state poll | Highest-weight drift |
| Polls | Merdeka, Ilham (poll tracker cron, Mon) | Weekly | Approval/valence calibration |
| Candidate/seat news | Google News RSS (candidate tracker cron, Wed) | Weekly | Event shocks |
| Economy | DOSM, BNM, news | Monthly/weekly | Economic-voting term |
| Party events | News (party landscape study) | Event-driven | Seat-specific shocks |
| By-elections | MECo | Event-driven | Mini-referenda |
| Redelineation | EC | Event-driven | **Model reset if enacted** |

---

## Part V — Version History

| Version | Date | Change |
|---|---|---|
| 1.0 | 3 Aug 2026 | Foundation: factor hierarchy, causal logic, core mathematics, validation protocol, output contract |

*Next version triggers: redelineation decision; first GE16 by-elections; new state-election data; Bersama's seat strategy; Bersatu's GE16 decision.*
