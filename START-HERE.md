# START-HERE.md — agent handoff for any new chat session

**Read this first.** It replaces stale chat memory with durable references.
Committed to the repo (safe to read after a fresh clone). Last updated
2026-09-28.

## 1. What this project is

HitungKerusi 222 — an autonomous Malaysian election-research and forecasting
application: source-backed knowledge accumulation, a reproducible 222-seat
federal parliamentary forecast AND state (DUN) forecasts for all applicable
state legislatures — federal and state scope are co-equal — readable EN/MS
reports, one internally consistent verified edition published to Vercel
(`hitungkerusi.fyi`).

- Repo: `github.com/tenunan-dev/hitungkerusi` (public, branch `main`)
- First push 2026-09-28, commit `cf89d9a` (P1 foundation, 532 files)
- V2 predecessor (READ-ONLY reference, never modify):
  `/Users/faisal.muthalib/Documents/HermesWorkFolder/Malaysia General Election v2`
- Never silently use the older `Malaysia General Election` V1 folder.

## 2. Read order in a new session

1. This file (`START-HERE.md`).
2. `PLAN.md` — master execution plan (LOCAL-ONLY, gitignored; exists only in
   this working copy). Checkbox = independently verified completion. Read in
   full before executing any task; keep ticks + evidence links current.
3. `TAKEOVER.md` — operational rails §0 for the main agent (local-only).
4. The target domain's `AGENTS.md` (`data/`, `analytics/`, `site/`, `ops/`,
   `outputs/`, `delivery/`) — authored fresh in P1.8, review-approved.
5. `docs/ARCHITECTURE.md` — living architecture manual, as-built state
   (committed). Read the sections for whatever subsystem you touch.
6. `docs/REUSE-MANIFEST.md` — the 60-row V2→V3 reuse contract (committed).
7. `evidence/P0..P7/` — per-task packets, run outputs, review verdicts,
   verification records (local-only).

## 3. Key owner decisions (details in evidence/P0/OWNER-DECISIONS.md)

- OD1 build polls store in P2; OD5 V2 port-clean, never fix V2; OD6 exclude
  Aila; OD7 incomplete editions need an owner-defined label (wording still
  pending — no silent degraded publishes); OD8 single repo `hitungkerusi`
  (V2 GitHub repos + Vercel projects deleted); OD9 Rail-3 consult lane.
- `models/` (ONNX 235 MB) is gitignored; integrity via
  `models/verify_model_cache.py` (5/5 sha256 OK as of 2026-09-28).
- PLAN.md / TAKEOVER.md / evidence/ / archive/ are local-only (gitignored).
- Git identity: `Tenunan Digital <dev@tenunan.com>` (GitHub tenunan-dev).

## 4. Environment facts

- One pinned interpreter: `.venv` on Python 3.12.10 (pyenv). Use
  `.venv/bin/python3`; no global installs, no copied virtualenvs.
- `v3_paths.py` is the project-root resolver (`HITUNGKERUSI_ROOT` override);
  no hardcoded V2 sibling paths.
- Verified suites (2026-09-28): `data/tests` 123 passed; from `site/`,
  `pytest tests` 13 passed; `tests/test_v3_paths.py` 4 passed.
- Recorded baselines (do NOT silently "fix"): `ops/tests` 31 fail-closed
  (runner rename → P4); analytics suites fail on `02_FORECAST→engine` import
  remap (→ P4); `outputs/tests` fails on V2 layout assumptions (→ P5).

## 5. Working rules (binding)

1. **Coding routing, always on.** Every coding task T1–T5 goes through the
   `coding-subagent-routing` skill + runner registry. Packet header carries:
   `Tier: T<n> | Implementer: … | Reviewer: … | Commits: parent-only | Fallback: deepseek fail-closed`. Fixed roles: ZCode implements, Claude reviews,
   OpenCode standby either leg. Codex PARKED — never assign.
2. **Two lanes, never mixed.** Tiered dispatch ≠ Rail-3 consult. Rail 3
   (TAKEOVER §0) is only when main-agent-level work overloads this session:
   consult a stronger model (ZCode glm-5.3 or Claude opus), take the answer
   back, resume ownership. It never replaces tiered routing.
3. **Verify everything yourself.** Agent IMPLEMENTED ≠ releasable: rerun the
   tests, inspect the diff, read back remote state. Never self-certify.
4. **Packets carry exclusion lists.** Name known RED fixtures and forbidden
   features (e.g. the P2.1 filename-exclusion ban, PLAN.md) explicitly, or a
   faithful implementer will port a trap. (P1.7b/c lesson.)
5. **Authorization boundaries.** Commits, pushes, deployments, scheduler
   changes: parent-owned + owner-confirmed. First publication of a V3 edition:
   explicit owner approval. Never run V2 and V3 production writers together.
6. **Reply style.** i-have-adhd/explaining-to-adhd skills: action-first,
   numbered, short; terang lesson block after coding changes.
7. **No data deletion;** preserve evidence; exclusions only after the P2.1
   classification audit. No secrets in repo or chat.

## 6. Current position (keep this block current)

- P0 ✅ P1 ✅ P2.1–P2.5 ✅ — P2.5 verified 2026-09-29; HEAD `4754534`
  (baseline migration, edition `20260929T064041Z`, suite 238/238).
- Next: **P2.6** — baseline/incremental collection modes with explicit
  windows + source checkpoints; scope includes normalizing per-state
  federal-results staging (owner seat-coverage addition, 2026-09-29).
- Objective (corrected 2026-09-29): federal 222-seat AND state DUN
  forecasts are co-equal scope.
- Phase order: P2 evidence model (P2.6–P2.10 remain) → P3 analytics/reports
  → P4 runner remap → P5 release/publish → P6 integration verification →
  P7 live cutover (owner-gated).

## 7. Known open items

- OD7 label wording: owner to supply (blocks 3 tests by design choice).
- OD1 polls store: no dedicated P2 task yet — scheduled decision recorded
  in PLAN.md §8 (fold into P2.8 or defer explicitly; auditor finding 3).
- `site/AGENTS.md:9` cosmetic nit (js/css/assets/geo read as nested under
  `state/`; they are siblings) — needs an approval-capable client to edit
  AGENTS.md files.
- ZCode long-run lane down 2026-09-29 (server-side cancels); lane B
  active until tomorrow (Claude implements, ZCode reviews). OpenCode
  `nous` provider disabled in its config — deepseek failover down.
- P2.4 advisory: `work_paths.apply_env` env-restore scoping needed before
  any second in-process caller. P2.5 advisories A1–A3 logged non-blocking.
- P2.4 verifier gap: `dupe-of-candidates.json` not covered by edition
  `row_counts` — fold into P2.6+ work before P2.8 (auditor finding 5).
