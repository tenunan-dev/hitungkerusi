# Forecast Engine — README

**Files:** `forecast_engine.py` (the model) + `config.py` (weekly inputs). **Do not edit the engine; edit config.py.**

## Run

```bash
cd "/Users/faisal.muthalib/Documents/HermesWorkFolder/Malaysia General Election/Forecast/engine"
../../.venv/bin/python forecast_engine.py              # full forecast (Monte Carlo 5000)
../../.venv/bin/python forecast_engine.py --backtest   # validation: must reproduce GE15
```

## Weekly update (cron c176829fbdb3 does this automatically each Friday)

1. **Edit `config.py` only:**
   - `MACRO`: gdp_yoy, cpi_yoy, ringgit, approval_delta — from latest DOSM/BNM/Merdeka
   - `EVENT_SHOCKS`: seat-code shocks from `Research/data/trackers/ge16-candidate-tracker-log.md` + news
   - `FACTORS` weights: ONLY when `knowledge/forecast-theory.md` is revised
2. **Run backtest** — must reproduce GE15 (PH 81 + MUDA 1, PN 74, BN 30, GPS 23, GRS 6).
3. **Run full forecast** — sanity-check flips (a Peninsular seat flipping to a Sabah bloc = bug).
4. **Append** the week's note to `../logs/weekly-forecast-log.md`.

## Outputs

- `../outputs/latest/ge16-forecast-latest.json` — machine-readable (overwritten each run)
- `../outputs/history/ge16-forecast-YYYY-MM-DD.json` — dated snapshot (append-only history)

## Data dependencies (read from project root)

| File | Role |
|---|---|
| `Research/federal/ge16-projection-model.csv` | per-seat GE15 winner/runner-up/margin (baseline) |
| `Research/data/derived/ge15-results-by-constituency-full.csv` | seat state + constituency names |
| `Research/data/derived/voter-demographics-by-constituency-ge15.csv` | ethnic/age shares → seat typing |
| `Research/data/derived/swing_se_to_se.csv` | post-GE15 state-election swings (revealed preference) |

## Model summary

```
M_s = M_GE15(s) + swing(winner) − swing(runner-up)   →  flip if M_s < 0
P(flip) = Φ(−M_s / σ_s)                               →  probit
Monte Carlo ×5000                                     →  P10/P50/P90, P(govt ≥ 112)
```

Theory: `../knowledge/forecast-theory.md` · Factor provenance: `../knowledge/forecast-factor-rankings.md`.

---

## State reports (13 states) — `state_report_builder.py`

Rebuilds all per-state reports into `Report/states/DUN <State>/latest/`. Tier 1 states (Melaka, Sarawak, Pahang, Perak, Perlis — `status: "upcoming"`) get full federal-level sections (§8a/§8b/§8c + §6 verdict); Tier 2 states keep the retrospective briefing.

```bash
../../.venv/bin/python state_report_builder.py                     # all 13 states (full rebuild, ~30s)
../../.venv/bin/python state_report_builder.py --state Melaka      # one state
../../.venv/bin/python state_report_builder.py --skip-unchanged    # smart scan (see below)
```

### Smart scan (`--skip-unchanged`) — USE IN THE WEEKLY CRON

**The Stage 3 cron MUST call `state_report_builder.py --skip-unchanged`** so quiet weeks (no new CSVs/JSON/markdown) skip in <1s instead of a ~30s full rebuild. The flag is safe: it only skips a state when **every** source file feeding its sections has an identical MD5 fingerprint to the last build.

- Fingerprint DB: `.cache/state_fingerprints.json` (auto-created/updated per state; commit it)
- Section-level granularity: a change to the forecast JSON rebuilds only §6/§8a/§8c/§11 for Tier 1 states; a change to a results CSV rebuilds the baseline-dependent sections; `sec12_refs` never changes
- On the first run after adding the flag the DB is empty → full rebuild once, then skips
- If any input changed, the affected sections rebuild and the report is re-saved (archive preserved)
- Verified 2026-08-13: identical re-run skips all 13 states; touching `ge16-forecast-latest.json` triggers selective rebuild; restoring it re-skips

