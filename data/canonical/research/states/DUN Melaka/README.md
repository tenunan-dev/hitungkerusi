# DUN Melaka — State Election Data Hub

State Assembly (DUN) + federal parliamentary data for Melaka.

## DUN (state assembly)
- **Seats:** 28
- **Latest election composition (SE-15, 20 Nov 2021):** BN 21, PH 5, PN 2
- `dun-election-results-latest.csv` — per-seat winners, party, bloc, votes, majority, turnout
- `dun-candidates-latest.csv` — all candidates (sex, ethnicity, age)
- see `Report/states/DUN Melaka/dun-election-summary.md` — readable results table

## Next PRN projection (2026–27)
- **Timing:** Assembly auto-dissolves 27 Dec 2026; polling by 25 Feb 2027 (possibly late 2026 / with GE16)
- `melaka-prn-projection.csv` — 28 seats, per-seat margins and projected winners (base scenario)
- see `Report/states/DUN Melaka/melaka-prn-projection-report.md` — full projection: 4 scenarios, methodology, what would change it
- **Base case (southern resurgence): BN 24, PH 4, PN 0** — 3 flips (Sungai Udang, Bukit Katil, Bemban)
- **Scenario range:** BN 24 (resurgence) ↔ BN 6 (green wave) ↔ BN 0 (federal convergence)
- **Key insight:** Melaka had the largest federal-state divergence in Malaysia (BN won 21/28 state seats in 2021 but 0/6 federal seats in 2022) — the widest projection error band of any state

## Federal (parliamentary)
- `parliamentary-seats.csv` — federal seats within Melaka (6)
- `voter-demographics.csv` — GE15 voter profile by constituency
- `ge16-battlegrounds.csv` — GE16 marginal seats in Melaka (Jasin 0.41%, Alor Gajah 1.22%)
- see `Report/states/DUN Melaka/ge16-battleground-deepdive.md` — seat-by-seat GE16 analysis
