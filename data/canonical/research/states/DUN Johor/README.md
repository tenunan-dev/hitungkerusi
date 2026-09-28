# DUN Johor — State Election Data Hub

State Assembly (DUN) + federal parliamentary data for Johor.

## DUN (state assembly)
- **Seats:** 56
- **Latest election composition:** BN 48, PH 8
- `dun-election-results-latest.csv` — per-seat winners, party, bloc, votes, majority, turnout
- `dun-candidates-latest.csv` — all candidates (sex, ethnicity, age)
- see `Report/states/DUN Johor/dun-election-summary.md` — readable results table

## Federal (parliamentary)
- `federal-election-results-latest.csv` — 26 federal seats, GE-15 results (winner, runner-up, previous GE-14 winner, changed_hands) — same schema as DUN CSVs
- `voter-demographics.csv` — GE15 voter profile by constituency
- `ge16-battlegrounds.csv` — GE16 marginal seats (<5%) in Johor
- see `Report/states/DUN Johor/ge16-battleground-deepdive.md` — seat-by-seat GE16 analysis
