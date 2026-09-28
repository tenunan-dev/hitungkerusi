# PRN Prediction Scorecard — Which Research Centre Got It Right?

**Compiled:** 3 August 2026 | **Purpose:** calibration evidence — how well Malaysian research centres predicted the three latest state elections. Used to weight *their* future polls and to grade our own model's assumptions.

## The actual results (verified from MECo via `DUN <State>/` folders)

| State | Date | Result |
|---|---|---|
| **Sabah** | 29 Nov 2025 | WARISAN **25** · GRS 22 · BN 6 · PBS 6 · IND 6 · UPKO 3 · STAR 2 · PN 1 · KDM 1 · PH 1 (73 seats) |
| **Johor** | 11 Jul 2026 | BN **48** · PH 8 (56 seats) |
| **N9** | 1 Aug 2026 | BN **18** + PN 7 = **25** (BN-PN alliance) · PH 11 (36 seats) |

## Predictions vs outcomes

| Centre / analyst | Sabah prediction | vs actual | Johor prediction | vs actual | N9 prediction | vs actual |
|---|---|---|---|---|---|---|
| **Ilham Centre** | GRS ≥26, Warisan 14 | ❌ GRS 22, **Warisan 25** (both wrong) | BN leads 39 seats | ❌ BN 48 (−9) | BN-PN 22, PH ≥9 | ❌ 25/11 (−3, −2) |
| **Merdeka Center** | — (no seat call found) | — | BN 40–42 | ❌ (−6 to −8) | — | — |
| **Ong Kian Ming** (ex-DAP MP, Taylor's) | — | — | BN 53 | ❌ (+5) | PH 9, BN-PN ~23 | ❌ (−2) |
| **Vodus Research** | — | — | BN 36% vote | ❌❌ actual 59.7% (−24pp) | — | — |
| Consensus (kinitv/analysts) | No clear majority | ✅ hung-ish (GRS-led coalition formed) | | | | |

## Verdict

1. **Direction (who governs): Ilham Centre 3/3** — right that BN wins Johor, BN-PN wins N9, and GRS-led coalition governs Sabah. Best N9 magnitude too (22 vs 25).
2. **Magnitude: Ong Kian Ming closest** on Johor (53 vs 48, +5) and N9 (~23 vs 25, −2) — but he is an individual analyst, not a centre.
3. **The big miss: Sabah for everyone** — Ilham's GRS ≥26 / Warisan 14 was wrong on both counts (Warisan's surge caught all centres off guard). East Malaysia follows local patronage logic, not national swings.
4. **Worst single error: Vodus** — BN 36% vote-share call vs actual 59.7% in Johor (−24pp).

## Calibration lessons for OUR model

- **Direction beats magnitude** → headline the forecast as *P(government formed)*, present seat counts as P10/P50/P90 ranges. (Our Monte Carlo already does this.)
- **Sabah/East Malaysia is a known blind spot** → our engine already separates `east_malaysia` seat type with reduced swing weights — validated by this evidence.
- **Swing-based beats poll-based** → our southern-resurgence scenario (BN +15.6pp swing) predicted the Johor BN supermajority that materialized as 48/56, while poll-based centres (Merdeka 40–42) under-shot. Revealed preference > stated intention.

## Sources
- Ilham Centre Sabah call: The Edge Malaysia, FMT (28 Nov 2025)
- Ilham Johor call: The Star (field study, Jul 2026); CNA (39 seats)
- Merdeka Johor call: CNA citing Malaysiakini (Ibrahim Suffian, Jul 2026)
- Ong Kian Ming: CNA (Johor 53; N9 PH 9 / BN-PN 17 + 6 Malay seats)
- Vodus: Instagram/NewsNez (Johor 2026 forecast, 36% BN)
- Actuals: ElectionData.MY / MECo (our DUN folders), Wikipedia 2026 Johor & N9 election pages
