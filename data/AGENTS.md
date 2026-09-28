# data/ — GE16 evidence collection and the canonical tree

## Purpose
Owns evidence collection and the canonical data tree for the HitungKerusi
monorepo. Collectors gather polls, news and candidate records; `data/canonical/`
is the single tree every other domain reads.

## Layout
- `canonical/` — canonical tree: `geo/`, `dun/`, `research/` (incl. `research/trackers/`)
- `scripts/collect/` — collectors, judges, self-heal and tracker-outdir helpers
- `scripts/` — `extract/refresh/validate_canonical_data.py`, `methodology_contract.py`
- `baselines/`, `geo/`, `meco/`, `research/` — imported V2 assets, kept as reference
- `tests/` — data suite (123 tests)

## Commands
From the repo root:
- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python3 -m pytest data/tests -q` → 123 passed

## Rules
- Canonical data lives only under `data/canonical/…`; trackers in
  `data/canonical/research/trackers/` are append-only.
- Backfill rebuilds stage a replacement and swap atomically; they must never
  delete durable evidence.
- Excluding canonical files by transient filename patterns is forbidden until
  the P2.1 classification audit lands (PLAN.md:181).
- The V2 tree is read-only; write only inside this repo root.

## Don't
- Don't edit imported reference assets (`baselines/`, `meco/`) in place.
- Don't hand-edit canonical JSON without running the validators.
- Don't commit secrets or `.env`.
