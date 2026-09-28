# HitungKerusi 222 — V3 monorepo

Autonomous Malaysian election-research and forecasting application: source-backed
knowledge accumulation, a reproducible 222-seat parliamentary forecast, state
forecasts, EN/MS reports, and one internally consistent verified edition on
Vercel (hitungkerusi.fyi).

## Layout
- `data/` — evidence collection + canonical tree (`data/canonical/…`, append-only trackers)
- `analytics/` — forecast engine, AI authoring harness, release automation
- `site/` — the public website (build_adapter, build_vercel, static assets)
- `ops/` — five-stage contract runner + completion-triggered chain coordinator
- `outputs/` — release contract validators, sealed-edition staging
- `delivery/` — publish helper binding sealed releases to delivery
- `docs/REUSE-MANIFEST.md` — V2→V3 reuse dispositions (60 rows, verified)

## Status
Phase P1 foundation complete (see PLAN.md for the master checklist).
Suites green: data 123/123, site 13/13, v3_paths 4/4. Ops runner rename and
analytics import remap deferred to P4 by recorded architecture consult.

## Environment
Python 3.12 pinned (`requirements.txt` from the audited dependency inventory).
`python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
`models/` is a local-only ONNX cache (gitignored) — rebuild via fastembed on
first run, or restore from backup.

## Reading order for agents
Each domain has an `AGENTS.md` with purpose, layout, verified commands, rules
and don'ts. Read the domain's AGENTS.md before touching that domain. V2
(`/Documents/HermesWorkFolder/Malaysia General Election v2`) is a read-only
reference — never write there.
