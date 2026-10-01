# START-HERE.md — agent handoff for any new chat session

**Read this first.** It replaces stale chat memory with durable references.
Committed to the repo (safe to read after a fresh clone). Last updated
**2026-10-01** (P2 phase close).

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

- P0 ✅ P1 ✅ **P2 COMPLETE** (P2.1–P2.10 + end-of-P2 manual gate) —
  closed 2026-10-01; HEAD `d85668a` on `origin/main` (verified: 0 ahead,
  tree clean). Suite **305/305** (`evidence/P2/P2.10-suite-full.log`).
- P2 delivered (committed): collection modes + checkpoints + archive
  (P2.6, `7caccc4`); resumable judgment runs (P2.7, `b01b1a9`);
  knowledge rebuild layer — additive merge, links, vectors, polls store
  (P2.8, `63b711b`+`5a9ad34`+`382e0d6`, R4 APPROVE 0 blocking);
  DATA-COVERAGE.md + JSON manifest (P2.9, `3d89fa5`); charter test
  matrix 8/8 (P2.10, `f7b4e75`); manual gate (P2.4–P2.10 subsystems,
  `d85668a`). Full push batch `54004ca..d85668a`.
- Next: **P3** — analytics/reports per PLAN.md. Read P3 rows in full
  before dispatch.
- Objective: federal 222-seat AND state DUN forecasts are co-equal scope.
- Phase order: P3 analytics/reports → P4 runner remap → P5 release/publish
  → P6 integration verification → P7 live cutover (owner-gated).

## 6b. Carried into P3 / integration (do not lose these)

- **Live promotions not yet run**: links/, vectors/, polls store are
  code-complete + review-approved + sandbox-proven; `links/` is empty —
  real promotion runs happen at integration (P6) or when P3 needs them.
- **Live additive events merge** not run: sandbox-proven 512 V2 + 2,217
  V3 = 2,729 events, origin-tagged; live canonical still shows the P2.5
  baseline (512 events). The live DB needs the origin ALTER via the merge
  itself — never a hand ALTER.
- **498-row orphaned-flag re-judge**: machinery ready, real pass not
  executed (`judge_runs.py orphaned-flags --promote`; live network).
- **R2-4 (P2.9 carry)**: derived-counter/carry-forward machinery has no
  dedicated tests yet (counters verified only by spot-check).
- **Per-feed checkpoints**: current checkpoint registers one consolidated
  `news` source_id.
- **OD7 label wording** still owner-pending (blocks 3 tests by design).
- Provenance manifest now **163 files** (data-coverage.json joined
  research/derived in P2.9; refreshed + pins updated to (149,162,163)).

## 7. Known open items

- OD7 label wording: owner to supply (blocks 3 tests by design choice).
- ~~OD1 polls store~~: **delivered in P2.8** (`polls_store.py`, judged
  poll-observation store; sandbox-proven; live promotion pending).
- `site/AGENTS.md:9` cosmetic nit (js/css/assets/geo read as nested under
  `state/`; they are siblings) — needs an approval-capable client to edit
  AGENTS.md files.
- ZCode long-run lane flaky (server-side "Turn was cancelled" ×2 in the
  P2.8 R3 review, 2026-09-30) — after two clean cancels, switch the seat
  to OpenCode adjudicator (`~/.opencode/bin/opencode run --model
  zai/glm-5.3`); that lane completed R3+R4 successfully. Launch pattern:
  `node /Applications/ZCode.app/Contents/Resources/glm/zcode.cjs --prompt
  ... --cwd ...` (bare `zcode` is NOT on PATH).
- P2.4 advisory: `work_paths.apply_env` env-restore scoping needed before
  any second in-process caller. P2.5 advisories A1–A3 logged non-blocking.
- P2.4 verifier gap: `dupe-of-candidates.json` not covered by edition
  `row_counts` — fold into P2.6+ work before P2.8 (auditor finding 5).
