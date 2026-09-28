# site/ — HitungKerusi website and deploy adapter

## Purpose
The static HitungKerusi site for Vercel (`hitungkerusi.fyi`). A pure renderer
of approved delivery content; it never computes forecasts itself.

## Layout
- `app/` — page HTML (index, seats, states, scenarios, maps)
- `state/` — hand-authored state pages; `js/`, `css/`, `assets/`, `geo/`
- Root pages `index.html`, `berita.html`, `laporan.html`; `vercel.json`
- `build_adapter.py`, `build_vercel.py`, `link_checker.py` — delivery-input
  wiring remaps at P5
- `tests/` — site suite (13 tests)

## Commands
From the repo root:
- `(cd site && PYTHONDONTWRITEBYTECODE=1 ../.venv/bin/python3 -m pytest tests -q)` → 13 passed

## Rules
- Render approved delivery artifacts only; keep their provenance intact.
- The single repo mirror is `tenunan-dev/hitungkerusi`; production deploys go
  through the Vercel flow, never via git push.
- The first V3 publication is an owner-approval boundary: no deploy until the
  owner approves it.

## Don't
- Don't read mutable research or tracker paths; delivery content only.
- Don't deploy while any local verification gate fails.
- Don't recompute or reinterpret forecasts, scenarios or political assumptions.
- Don't commit secrets or `.env`.
