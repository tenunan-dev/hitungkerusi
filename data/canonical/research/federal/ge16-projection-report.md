# GE16 Seat Projection Model — Methodology & Results

**Compiled:** 3 August 2026 | **Baseline:** GE15 (19 Nov 2022) results, 222 federal seats

## 1. Model logic

The model projects GE16 outcomes by applying the **post-GE15 vote drift** observed in the most recent state elections (SE) to the GE15 federal margins, seat by seat:

```
Swing(state, bloc) = vote_share(bloc, latest SE) − vote_share(bloc, previous SE)
Projected_margin(seat) = GE15_margin_pct(seat) + Swing(winner bloc) − Swing(runner-up bloc)
Seat flips if Projected_margin < 0
```

**Data:** MECo/ElectionData.MY headline ballots (CC0) — state elections 1958–2026; GE15 per-seat results (EC via Wikipedia).

**Swing sources (states with a fresh post-GE15 state poll):**

| State | Swing pair | Signal |
|---|---|---|
| Johor | SE-15 (Mar 2022) → SE-16 (Jul 2026) | BN +17.0pp, PN −19.1pp, PH +6.2pp |
| Negeri Sembilan | SE-15 (Aug 2023) → SE-16 (Aug 2026) | BN +14.3pp, PN −15.7pp, PH +1.5pp |
| Sabah | SE-14 (Sep 2020) → SE-15 (Nov 2025) | GRS +23.2pp, PH −11.2pp, KDM +4.8pp |
| Kedah | SE-14 (2018) → SE-15 (Aug 2023) | PN +23.0pp, BN −19.6pp |
| Kelantan | SE-14 (2018) → SE-15 (Aug 2023) | PN +15.3pp |
| Terengganu | SE-14 (2018) → SE-15 (Aug 2023) | PN +16.1pp |
| Penang | SE-14 (2018) → SE-15 (Aug 2023) | PN +20.8pp, BN −16.9pp |
| Selangor | SE-14 (2018) → SE-15 (Aug 2023) | PN +18.1pp, PH −6.4pp |

**States with NO fresh signal → status quo applied:** Perlis, Perak, Pahang (SE concurrent with GE15), Melaka (2021 poll), Sarawak (2021 poll).

## 2. Base-case projection (all swings applied)

| Bloc | GE15 | Projected GE16 | Δ |
|---|---|---|---|
| PN | 74 | **80** | +6 |
| PH | 81 | **70** | −11 |
| BN | 30 | **34** | +4 |
| GPS | 23 | **23** | 0 |
| GRS | 6 | **8** | +2 |
| WARISAN | 3 | **3** | 0 |
| MUDA | 1 | **1** | 0 |
| KDM | 1 | **1** | 0 |
| PBM | 1 | **1** | 0 |
| IND | 2 | **1** | −1 |
| **Total** | **222** | **222** | |

**Government-aligned seats (PH+BN+GPS+GRS+WARISAN+MUDA+KDM+PBM): 141** vs PN/IND: 81. Government retains a clear majority (112 needed), but its margin shrinks from ~146 to ~141.

## 3. Flips in the base case (15 seats)

| Seat | State | GE15 → Projected | Trigger |
|---|---|---|---|
| P015 Sungai Petani | Kedah | PH → PN | Kedah PN wave (+23pp) |
| P047 Nibong Tebal | Penang | PH → PN | Penang PN surge |
| P053 Balik Pulau | Penang | PH → PN | Penang PN surge |
| P097 Selayang | Selangor | PH → PN | Selangor PN surge |
| P098 Gombak | Selangor | PH → PN | Selangor PN surge |
| P101 Hulu Langat | Selangor | PH → PN | Selangor PN surge |
| P108 Shah Alam | Selangor | PH → PN | Selangor PN surge |
| P113 Sepang | Selangor | PH → PN | Selangor PN surge |
| P141 Sekijang | Johor | PH → BN | Johor BN surge (+17pp) |
| P142 Labis | Johor | PH → BN | Johor BN surge |
| P149 Sri Gading | Johor | PH → BN | Johor BN surge |
| P143 Pagoh | Johor | PN → PH | Muhyiddin seat; PH +6pp in Johor |
| P154 Mersing | Johor | PN → BN | Johor BN surge |
| P167 Kudat | Sabah | IND → GRS | Sabah GRS wave (+23pp) |
| P170 Tuaran | Sabah | PH → GRS | UPKO→GRS realignment |

## 4. Scenario sensitivity

| Scenario | PN | PH | BN | GPS | GRS | Govt-aligned | PN+IND |
|---|---|---|---|---|---|---|---|
| **Status quo** (no swings) | 74 | 81 | 30 | 23 | 6 | 146 | 76 |
| **Base** (fresh swings) | 80 | 70 | 34 | 23 | 8 | 141 | 81 |
| **PN surge** (+5pp PN everywhere) | 84 | 68 | 32 | 23 | 8 | 137 | 85 |
| **Govt surge** (+3pp PH/BN, +2pp GPS/GRS) | 75 | 71 | 38 | 23 | 9 | 146 | 76 |

Government holds a majority in ALL scenarios (≥137). The question is not *whether* the Unity Government survives, but **how large its majority is** — from 146 (status quo/govt surge) down to 137 (PN surge).

## 5. Caveats & assumptions

- **Uniform swing assumption:** the state-level swing is applied equally to every federal seat in that state. Real swings vary by seat demographics (youth share, ethnicity, local incumbents).
- **SE → federal transfer:** state-election swings are a *proxy* for federal drift; turnout and candidate effects differ. SE swings are typically larger than federal swings (mid-term protest effects).
- **No candidate effects:** new candidates, retirements, and the Bersama/third-force split (PKR breakaway) are not modelled.
- **Battleground seats** (margin <5%) are where these swings bite hardest — see `ge16-battleground-analysis.md`.
- **Vacancies:** Pandan & Setiawangsa (vacant since May 2026) treated as PH-held (PKR) for arithmetic.
- **Malaysia FPTP amplification:** a ~5pp national swing can translate into 15–25 seat changes, consistent with the model output.

---
*Companion dataset: `ge16-projection-model.csv` (all 222 seats, per-seat swings and projected winners).*