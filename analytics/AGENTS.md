# analytics/ — forecast engine, authoring and automation

## Purpose
Owns the probabilistic GE16 forecast engine plus report authoring and the
automation that stages releases and deliveries. Analytics reads canonical
inputs and writes only its own code and generated staging.

## Layout
- `engine/` — `forecast_engine.py`, `author_reports.py`, `config.py`, `data_roots.py`
- `automation/` — `delivery/`, `outputs/`, `reports/`, `state_analysis/`, `qa/`, `md2docx/`
- `tools/` — `baseline/`, `events/`, `explorer/`
- `tests/` — analytics suites (not yet green in V3)

## Commands
None verified yet: analytics suites are BASELINE-DEFERRED. The
02_FORECAST→engine import remap lands in P4. Do not add a pytest command here
until the suite actually passes in V3.

## Rules
- Forecasts are probabilistic. Never alter assumptions, weights or seeds to
  obtain a preferred political outcome.
- Read canonical inputs from `data/canonical/…` only; never write there.
- Leave the engine's V2 sibling-path lookups alone until the P4 remap; no
  shortcut hacks around them.

## Don't
- Don't write into `data/`, `outputs/`, `delivery/` or `site/` from analytics code.
- Don't claim suite results without running them.
- Don't commit secrets or `.env`.
