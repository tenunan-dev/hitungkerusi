# REUSE-MANIFEST — V3 monorepo (tenunan-dev/hitungkerusi)

**Task:** P1.2. **Tier:** T2 | **Implementer:** flash main agent (direct, bounded docs) | **Reviewer:** parent-readback per Rail 1 | **Commits:** parent-only | **Fallback:** deepseek fail-closed.
**Date:** 2026-09-28.

- **Inputs read:** `evidence/P0/P0.7-reuse-dispositions.md` + `.json` (60 disposition rows, source of truth), `evidence/P1/P1.1-flash-probe-result.json` (approved layout), `evidence/P0/OWNER-DECISIONS.md` (OD1–OD6 applied outcomes).
- **Method:** manifest only. Nothing has been copied. Every destination below is a *planned* V3 path for the P1.3 copy step, not a completed action. No V2 file was written to produce this document.
- **Scope note:** `README.md` at the V3 repo root is out of scope for this manifest.

## 1. Gitignore plan (applies at V3 repo root, ahead of any copy)

| Path | Git treatment | Basis |
|---|---|---|
| `models/` | **gitignored** (tracked on disk, not in git) | Owner decision 1 (P1.1 escalation D1 — the 235 MB ONNX cache exceeds GitHub's 100 MB per-file limit): recommendation accepted. |
| `evidence/` | **local-only** (gitignored) | Owner decision 2 (P1.1 escalation D2 — public-repo content boundary): recommendation accepted. |
| `archive/` | **local-only** (gitignored) | Same as above. |
| `PLAN.md` | **local-only** (gitignored) | Same as above. |
| `TAKEOVER.md` | **local-only** (gitignored) | Same as above. |

Everything else under the approved P1.1 layout (`data/ analytics/ outputs/ delivery/ site/ ops/ docs/`) is a normal tracked path in the public repo, subject to the per-row dispositions below.

## 2. Legend

**Disposition** (from P0.7 §"Disposition vocabulary", with OD1–OD6 owner outcomes applied where P0.7 had left `[OWNER-DECISION]` open):

| Value | Meaning |
|---|---|
| copy-unchanged | Copy bytes as-is into V3 |
| copy-refactor | Reuse as the base, with named changes during/after port |
| regenerate | Not copied as a live artifact; rebuilt in V3 from verified inputs |
| preserve-history | Retained as audit/history reference; not an active V3 input |
| exclude | Not copied (V2 original remains — exclusion ≠ deletion) |

**State:**

| Value | Meaning |
|---|---|
| PENDING-COPY | Disposition implies bytes move into V3; P1.3 has not run yet |
| EXCLUDED | Nothing is ever copied for this row |
| REGENERATE-PLANNED | V3 builds this fresh later; no copy step applies |

## 3. Manifest — Group 1: 1_DATA canonical 149-file set

| # | V2 source path | V3 destination (planned) | State | Disposition | Reason |
|---|---|---|---|---|---|
| 1a | `1_DATA/canonical-data-provenance.json` + 149 destination files | `data/canonical/` | PENDING-COPY | copy-unchanged | All 149 verified present, 0 mismatch vs manifest size+SHA-256; imports as growing research baseline (P0.7 1a). |
| 1b | `1_DATA/research/trackers/ge16-news-accepted.json` (2,367 items) | `data/canonical/` | PENDING-COPY | copy-unchanged | Accepted-research archive preserved; dup-link groups carried to V3 dedup, not altered at copy (P0.7 1b). |
| 1c | `1_DATA/research/trackers/ge16-news-feed.json` (120 items) | — | EXCLUDED | exclude | Rolling subset fully contained in accepted corpus; V3 collector regenerates its own feed per run (P0.7 1c). |
| 1d | 14 unmanifested files: `research/data/notes/*.md` (8) + `research/knowledge/*.md` (6) | `archive/verification/` | PENDING-COPY | preserve-history | Outside manifest roots; classification deferred to P2.1 (P0.7 1d). |
| 1e | meco historical corpus (5 CSVs, 1955–2026) | `data/meco/` | PENDING-COPY | copy-unchanged (dormant reference) | **OD2 applied:** copy-unchanged as dormant reference — zero active readers, R08 preservation at zero integration risk. |
| 1f | `2_ANALYTICS/work/events/ge16-events.db` | `data/baselines/` | PENDING-COPY | copy-unchanged | Knowledge baseline with embedded provenance; known defects carried to P2.2/P2.5 import, not silently fixed; read-only snapshot copy method (P0.7 1f). |
| 1g | DUN CSVs: `research/states/DUN <State>/*-latest.csv` ×13 + ×13 | `data/canonical/` | PENDING-COPY | copy-unchanged | Uniform schemas, 0 duplicate seats/(seat,name); consumed by state_report_builder.py (P0.7 1g). |
| 1h | `1_DATA/geo/malaysia-states.geojson` + `geo/dun/` ×13 + `LICENSE` + `README.md` | `data/geo/` | PENDING-COPY | copy-unchanged | Byte-verified static geography; LICENSE kept with set (P0.7 1h). |
| 1i | `1_DATA/scripts/{refresh_canonical_data,validate_canonical_data,extract_canonical_data,methodology_contract}.py` | `data/scripts/` | PENDING-COPY | copy-refactor | Proven (manifest 111→149) but must separate dynamic research from immutable snapshots (P0.7 1i). |

## 4. Manifest — Group 2: Collectors and trackers

| # | V2 source path | V3 destination (planned) | State | Disposition | Reason |
|---|---|---|---|---|---|
| 2a | `1_DATA/scripts/collect/{track_ge16_news,ge16_judge_deepseek,ge16_news_backfill,ge16_selfheal_state,ge16_tracker_outdir}.py` | `data/scripts/collect/` | PENDING-COPY | copy-refactor | Proven in 2026-09-26 real rebuilds, stdlib-only; add windows/checkpoints/resumable judgment (P0.7 2a). |
| 2b | `1_DATA/scripts/collect/track_ge16_polls.py` | `data/scripts/collect/` | PENDING-COPY | copy-refactor | **OD1 applied:** copied as the base collector; a real polls-observations store (field dates/sample size/topline) is built on top in P2, sequenced not either/or. |
| 2c | `1_DATA/scripts/collect/track_ge16_candidates.py` | `data/scripts/collect/` | PENDING-COPY | copy-refactor | Headline-only record shape; only collector writing DB directly without staging — refactor adds staged writes (P0.7 2c). |
| 2d | `ge16-polls-tracked.json` (234) + `ge16-candidates-tracked.json` (47) + both tracker logs | `data/canonical/` | PENDING-COPY | copy-unchanged | Canonical, byte-verified; carried with the standing correction that seen-keys are dedup keys, not observations (P0.7 2d). |

## 5. Manifest — Group 3: Forecast engine + config constants

| # | V2 source path | V3 destination (planned) | State | Disposition | Reason |
|---|---|---|---|---|---|
| 3a | `2_ANALYTICS/02_FORECAST/engine/forecast_engine.py` | `analytics/engine/` | PENDING-COPY | copy-unchanged | Reads exactly five canonical CSVs, zero polls-file references; model math frozen at initial port (P0.7 3a). |
| 3b | `2_ANALYTICS/02_FORECAST/engine/config.py` (MACRO, pm_pref_*, VACANCIES, EVENT_SHOCKS) | `analytics/engine/` | PENDING-COPY | copy-unchanged (byte-identical) | **OD1 applied:** kept byte-identical at initial port for numerical parity with V2; any statistical change is a separately reviewed task. |
| 3c | `2_ANALYTICS/02_FORECAST/engine/{data_roots,log_utils}.py` + `engine/common/` | `analytics/engine/` | PENDING-COPY | copy-refactor | Drop V2 absolute-path coupling for project-root config, testable from another cwd (P0.7 3c). |

## 6. Manifest — Group 4: Report builders + authoring chain

| # | V2 source path | V3 destination (planned) | State | Disposition | Reason |
|---|---|---|---|---|---|
| 4a | `2_ANALYTICS/02_FORECAST/engine/report_builder.py` | `analytics/engine/` | PENDING-COPY | copy-refactor | Currently derives signals from a capped 6-item tracker-log scan only; knowledge-driven narrative required while preserving the 72-file edition structure (P0.7 4a). |
| 4b | `2_ANALYTICS/02_FORECAST/engine/state_report_builder.py` | `analytics/engine/` | PENDING-COPY | copy-refactor | Reads DUN results+candidates CSVs; rebind to frozen run snapshot instead of live research paths (P0.7 4b). |
| 4c | `2_ANALYTICS/02_FORECAST/engine/author_reports.py` + `authoring_config.json` | `analytics/engine/` | PENDING-COPY | copy-refactor (fail-closed gate) | **OD4 applied:** component (writer-chain retry, verification, reference packs) is reused; publish gate replaced with fail-closed — violations block publish unless the owner explicitly labels a degraded edition. |
| 4d | `2_ANALYTICS/02_FORECAST/engine/{reference_pack,federal_ms_render}.py` | `analytics/engine/` | PENDING-COPY | copy-refactor | Claim/number verification and Malay rendering required; refactor separates reader-facing references from internal ledgers (P0.7 4d). |

## 7. Manifest — Group 5: Sealing / delivery / website scripts and site

| # | V2 source path | V3 destination (planned) | State | Disposition | Reason |
|---|---|---|---|---|---|
| 5a | `3_OUTPUTS/scripts/{seal_release_intake,validate_output_contract,validate_release_intake}.py` + `output-contract.json` | `outputs/` | PENDING-COPY | copy-unchanged | Stdlib-only; seal path proven end-to-end, 83/83 artifact hashes re-verified. Dirty-manifest caveat carried to P1.3/P1.4 (P0.7 5a). |
| 5b | `4_DELIVERY/scripts/publish_delivery.py` | `delivery/` | PENDING-COPY | copy-unchanged | 99/99 file hashes verified, atomic `current` symlink, 8-release history preserved (P0.7 5b). |
| 5c | `5_WEBSITES/vercel/{build_vercel,build_adapter,link_checker}.py` + `vercel.json` | `site/` | PENDING-COPY | copy-unchanged | Stdlib-only, no Node build; four preflight gates passed in the 2026-09-23 production deploy receipt (P0.7 5c). |
| 5d | Site payload: `index.html`, `berita.html`, `laporan.html`, `state/` (26 pages), `js/`, `css/`, `app/`, `geo/`, `assets/` | `site/` | PENDING-COPY | copy-unchanged | Proven rendering of the sealed H-20260923-11 edition; 28-of-72 rendering gap carried to P5.5 (P0.7 5d). |
| 5e | `5_WEBSITES/vercel/DEPLOY_LOG.md` | `archive/releases/` | PENDING-COPY | preserve-history | **OD5 applied:** port-clean, V2 stays read-only; the mislabeled/append-order defects are not fixed in V2 — V3 starts a fresh log at first deploy rather than continuing this file. |
| 5f | `5_WEBSITES/vercel/.vercel/` (project link) + deploy-identity notes | — | EXCLUDED | exclude | Auth/linking state; first V3 publication destination is an explicit owner-approval boundary. Re-bound at first V3 deploy per OD8 (P0.7 5f). |
| 5g | `5_WEBSITES/vercel/{MISSING_DATA,AGENTS,CLAUDE}.md` | `archive/v2-docs/` | PENDING-COPY | preserve-history | Documents the current (V2) delivery binding; reference for P5.4, rewritten fresh for V3 in P1.8/P7.8 (P0.7 5g). |

## 8. Manifest — Group 6: OPS cron contracts / runner / remint

| # | V2 source path | V3 destination (planned) | State | Disposition | Reason |
|---|---|---|---|---|---|
| 6a | `OPS/cron/run_stage.py` + `ge16_jobs.json` + `cron/jobs/{ge16-authoring,ge16-chain}.json` | `ops/` | PENDING-COPY | copy-refactor | Sound sandbox/allowlist primitives, but optional steps have no sandbox slot and one allowlist entry is dead; P4.2 gives every required step a real invocation + receipt (P0.7 6a). |
| 6b | `OPS/cron/ge16_chain_runner.py` | `ops/` | PENDING-COPY | copy-refactor | Detached-Popen chain; V3 replaces with a persisted run-state coordinator, reusing stage-command and locking primitives (P0.7 6b). |
| 6c | `OPS/cron/sync_jobs.py` | `ops/` | PENDING-COPY | copy-refactor | Read-only "NO APPLY" checker preserved; never reconciles `cron/jobs/*.json` — the gap V3 closes (P0.7 6c). |
| 6d | `OPS/cron/{mint_fire_key,ed25519_support}.py` + `SCHEDULED_FIRE.md` | `ops/` | PENDING-COPY | copy-refactor | Owner-minted one-shot fire-key model is the authorization mechanism to preserve (R02); cryptography/openssl fallback must be declared (P0.7 6d). |
| 6e | `OPS/ops-contract.json` + `OPS/validate_ops_contract.py` | `ops/` | PENDING-COPY | copy-refactor (reference until reconciled) | Contains conflicting locks vs V3's one-coordinator design; do not inherit stale policy (P0.7 6e). |
| 6f | `OPS/cron/launchd/com.tenunan.ge16-chain.plist` + `OPS/cron/patches/*.patch` + `.fixture-reconciliation-evidence/` | `archive/verification/` | PENDING-COPY | preserve-history | Uncommitted scheduler work products; V3 scheduling/cutover is P7 with the single-production-writer rule (P0.7 6f). |
| 6g | Owner remint daemon (outside V2 repo tree) | — | EXCLUDED | exclude | Not a copyable repo asset; loaded/running status unverified; P4.7 designs V3's supported owner-side renewal instead (P0.7 6g). |

## 9. Manifest — Group 7: VDB collections + ONNX cache

| # | V2 source path | V3 destination (planned) | State | Disposition | Reason |
|---|---|---|---|---|---|
| 7a | 7 collections `2_ANALYTICS/work/figures/ge16-{parties,personnel,figures,seats,news,scenarios,clusters}-{meta.json,vectors.npy}` | `data/baselines/vdb/` | PENDING-COPY | copy-unchanged (baseline reference) | **OD3 applied:** copy current live sets as a baseline/parity reference, then regenerate all seven in V3 from the verified corpus with corrected builders and recorded snapshot binding. |
| 7b | `2_ANALYTICS/tools/figures/build_{personnel,parties,figures}_vdb.py` + `build_remaining_vdbs.py` + `build_vec_map.py` + `search_*.py` | `analytics/tools/` | PENDING-COPY | copy-refactor | Rebuild path proven; refactor adds snapshot-binding metadata and fixes the seats-layer junk-row/mislabel root cause (P0.7 7b). |
| 7c | ONNX model cache `2_ANALYTICS/work/figures/.model_cache/models--qdrant--paraphrase-multilingual-MiniLM-L12-v2-onnx-Q/` (~253 MB, single copy) | `models/` (gitignored) | PENDING-COPY | copy-unchanged (single copy) | Cache verified complete file-by-file → local embedding with no download; duplicate second copy excluded (dedupe); V3 adds an offline-enforcement flag absent in V2 (P0.7 7c). |
| 7d | `2_ANALYTICS/work/graph/ge16-knowledge-graph.json` (106.8 MB) | — | REGENERATE-PLANNED | regenerate | Derived search aid, not authority; not snapshot-bound to the events DB; rebuilt in V3 from the imported accepted corpus with recorded source-snapshot metadata (P0.7 7d). |

## 10. Manifest — Group 8: Tests per repo

| # | V2 source path | V3 destination (planned) | State | Disposition | Reason |
|---|---|---|---|---|---|
| 8a | `1_DATA/tests/` (8 files) | `data/tests/` | PENDING-COPY | copy-refactor | Port valid regression tests, excluding accidental/environmental failures; review the +46 uncommitted provenance-test lines against HEAD first (P0.7 8a). |
| 8b | `2_ANALYTICS/automation/tests/` (27 files) | `analytics/tests/` | PENDING-COPY | copy-refactor | Engine/authoring/baseline/explorer contracts worth porting; requires the pytest 8.4.2 set; P1.7 splits baseline failures from regressions (P0.7 8b). |
| 8c | `3_OUTPUTS/tests/test_output_contract_validator.py` | `outputs/tests/` | PENDING-COPY | copy-unchanged | Stdlib-only, matches the proven seal path (P0.7 8c). |
| 8d | `4_DELIVERY/tests/` | — | EXCLUDED | exclude | Directory is empty; V3 adds its own suite under P6.1; no requirements conflict (P0.7 8d). |
| 8e | `5_WEBSITES/vercel/{test_build_adapter,test_build_vercel}.py` | `site/tests/` | PENDING-COPY | copy-unchanged | Stdlib-only; covered gates passed in the production deploy receipt (P0.7 8e). |
| 8f | `OPS/tests/` (8 files + `fixtures/`) | `ops/tests/` | PENDING-COPY | copy-refactor | Proven under system Python 3.11/3.12 + pytest 9.1.1, not the 3.9 venv; V3 pins one interpreter (P0.7 8f). |
| 8g | `5_WEBSITES/aila/tests/test_deploy_aila_manifest.py` | — | EXCLUDED | exclude | **OD6 applied (settled, not reopened):** Aila excluded entirely from V3 (R03). |

## 11. Manifest — Group 9: AGENTS.md domain contracts

| # | V2 source path | V3 destination (planned) | State | Disposition | Reason |
|---|---|---|---|---|---|
| 9a | `2_ANALYTICS/AGENTS.md`, `3_OUTPUTS/AGENTS.md`, `OPS/AGENTS.md`, `5_WEBSITES/vercel/AGENTS.md` | `archive/v2-docs/` (reference copies; fresh per-domain `AGENTS.md` authored in P1.8) | PENDING-COPY | copy-refactor (consult, then author fresh) | P1.8 requires V3 domain instructions consistent with owner intent and actual implementation, not inherited stale locks (P0.7 9a). |
| 9b | V2 root `README.md`, `docs/MANUAL.md`, `docs/ARCHITECTURE.md` | `archive/v2-docs/` | PENDING-COPY | preserve-history | Dated/contradictory content; not copied as current truth — V3 manuals written in P7.8 against implemented code (P0.7 9b). |
| 9c | `5_WEBSITES/aila/AGENTS.md` | — | EXCLUDED | exclude | **OD6 applied (settled, not reopened):** Aila excluded entirely from V3 (R03). |

## 12. Manifest — Group 10: Evidence / verification directories

| # | V2 source path | V3 destination (planned) | State | Disposition | Reason |
|---|---|---|---|---|---|
| 10a | `OPS/verification/` (`fix3-shock-prose/`, `fix4-probit-sign/`, `schedfire-round2/`, `scheduler-binding-race/` + untracked) | `archive/verification/` | PENDING-COPY | preserve-history | Audit evidence; not an active analytical input (P0.7 10a). |
| 10b | `OPS/migration/` incl. `evidence/` + `schema/` | `archive/verification/` | PENDING-COPY | preserve-history | Rename-migration audit trail; content informs V3 environment but the migration context itself is history (P0.7 10b). |
| 10c | V2 top-level `baseline/` + `ARCHIVE/` | — | EXCLUDED | preserve-history (retained in V2 only) | Historical snapshots and superseded plans; D6 no-deletion; not copied as active inputs — remain readable in the untouched V2 clone (P0.7 10c). |
| 10d | Run receipts/edition history: `1_DATA/manifest/`, `2_ANALYTICS/{manifest,.tracking}/`, `3_OUTPUTS/{manifest,releases}/`, `4_DELIVERY/{manifest,releases}/`, `03_REPORTS/**/archive/` | `archive/releases/` | PENDING-COPY | preserve-history | The handoff→seal→delivery→deploy proof chain rests on these receipts; sealed editions immutable after sealing (P0.7 10d). |
| 10e | Transient/working state: `__pycache__/`, `.DS_Store`, `.bounded-live-claims*`, empty staging/payload dirs, `work/baseline/stage/<ts>/` copies, `_draft/`, probe scripts | — | EXCLUDED | exclude | Working state per do-not-copy list; stage copies never diffed against originals; exclusion ≠ deletion (P0.7 10e). |

## 13. Manifest — Supplementary rows (S1–S7)

| # | V2 source path | V3 destination (planned) | State | Disposition | Reason |
|---|---|---|---|---|---|
| S1 | `2_ANALYTICS/tools/baseline/rebuild_baseline.py` | `analytics/tools/` | PENDING-COPY | copy-refactor | Three real full rebuilds all layers ok; two open MED findings (dry-run side effects, rollback→swap) fixed in the V3 port (P0.7 S1). |
| S2 | `2_ANALYTICS/tools/events/build_events_db.py` | `analytics/tools/` | PENDING-COPY | copy-refactor | Built the 1f events DB with embedded provenance; refactor adds stable-ID handling for duplicate/cross-type entity groups (P0.7 S2). |
| S3 | `2_ANALYTICS/GE16-Graph-Explorer/` + `2_ANALYTICS/work/graph-explorer/` | `analytics/tools/explorer/` | PENDING-COPY | copy-refactor | Retained manual research-QA feature; authoritative-source vs generated-copy determination required at copy time; stdlib-only helpers (P0.7 S3). |
| S4 | `2_ANALYTICS/automation/` (state_analysis, delivery, reports, qa) + `04_SOCIAL` outputs | `analytics/automation/` | PENDING-COPY | copy-refactor | `analytics-social-stage` is a required stage-2 adapter command; duplicate `05_AUTOMATION/` copies consolidated to one location (P0.7 S4). |
| S5 | `2_ANALYTICS/05_AUTOMATION/md2docx.py` + `automation/reports/md2docx.py` | `analytics/automation/` | PENDING-COPY | copy-refactor (or drop in V3 scoping) | `python-docx` imported lazily and absent from the venv — a broken-if-exercised optional path; declare the dependency or make DOCX explicitly optional (P0.7 S5). |
| S6 | `2_ANALYTICS/01_RESEARCH/figures/_draft/` scripts + duplicate `.model_cache` copy | — | EXCLUDED | exclude | Draft scripts import a package installed nowhere; duplicate model cache dedupes to 7c; V2 originals retained (P0.7 S6). |
| S7 | `OPS/security/owner_ed25519.pub` | — | EXCLUDED | exclude | Public (non-secret) but key material — excluded categorically from copying; V3's authorization chain is (re)established under P4.7. No key bytes are reproduced in this manifest. |

## 14. Owner decisions applied (OD1–OD6)

| ID | Rows affected | Applied outcome |
|---|---|---|
| OD1 | 2b, 3b | Build a real polls-observations store in V3 (P2); keep `config.py` constants byte-identical at initial port for numerical parity with V2. |
| OD2 | 1e | Copy-unchanged as a dormant canonical reference (zero active readers = zero integration risk). |
| OD3 | 7a | Copy current live VDB sets as a baseline/parity reference, then regenerate all seven in V3 with corrected builders and recorded snapshot binding. |
| OD4 | 4c | Fail-closed publish gate: violations block publish unless the owner explicitly labels a degraded edition (OD7 defines the label wording; not yet finalized, non-blocking here). |
| OD5 | 5e (and V2 defects generally) | Port-clean into V3; V2 stays read-only; defects are fixed in the V3 copies, never in V2. |
| OD6 | 8g, 9c | Aila excluded entirely from V3 (R03 settled; not reopened by this manifest). |

OD7 (label wording), OD8 (repo/Vercel consolidation — already executed), and OD9 (main-agent model routing) do not change any row's disposition and are not reflected in the table above.

## 15. Coverage and verification notes

- 60 rows total: Groups 1–10 (§§3–12) = 53 rows + Supplementary S1–S7 (§13) = 7 rows.
- All six OWNER-DECISION rows from P0.7 (1e/OD2, 2b+3b/OD1, 4c/OD4, 5e/OD5, 7a/OD3, 8g+9c/OD6) now carry a concrete disposition and destination reflecting the applied owner outcome, not the `[OWNER-DECISION]` placeholder.
- No row references a V2 write path; every destination column names only a planned V3 path, or "—" where the row is excluded/regenerated and nothing is copied.
- No `.env` or credential rows appear. The one key-material row (S7) is excluded, states only its (public, non-secret) V2 path and disposition, and reproduces no key bytes.
- Nothing has been copied by this document. P1.3 executes the PENDING-COPY rows.

*End of P1.2 reuse manifest. Source: `evidence/P0/P0.7-reuse-dispositions.{md,json}`. Layout: `evidence/P1/P1.1-flash-probe-result.json`. Owner outcomes: `evidence/P0/OWNER-DECISIONS.md`.*
