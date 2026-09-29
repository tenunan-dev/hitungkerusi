# ARCHITECTURE.md — HitungKerusi 222, V3 monorepo

Living manual describing the system as built. Updated at defined plan
milestones (see "Maintenance rule"). Facts below verified 2026-09-28 against
commit `0dd89c5` (P2.3 complete; P2.2 `738d13a` pushed).

---

## 1. What the system is

An autonomous Malaysian election-research and forecasting application:
source-backed knowledge accumulation over an append-only, provenance-hashed
evidence corpus; a reproducible 222-seat federal parliamentary forecast AND
state (DUN) forecasts for all applicable state legislatures — federal and
state scope are co-equal; readable EN/MS reports; one internally
consistent verified edition published to Vercel (`hitungkerusi.fyi`).

- Repo: `github.com/tenunan-dev/hitungkerusi`, branch `main`, public.
- Monorepo root is the repo root. Python venv: `.venv/` (Python 3.12).
- Predecessor V2 tree is a **read-only** research reference:
  `~/Documents/HermesWorkFolder/Malaysia General Election v2` — never
  modified by any V3 process.

## 2. Top-level layout

```
data/        durable canonical corpus + schemas + importers + collectors + tests
analytics/   forecast + analysis layer (P4+, currently V2-reuse copies)
site/        web frontend (P6; V2-reuse copies staged in P1)
ops/         deployment/operational scripts
outputs/     generated reports (delivery artifacts)
delivery/    publication pipeline (P7)
docs/        REQUIREMENTS.md, REUSE-MANIFEST.md, this manual
evidence/    LOCAL-ONLY per-task packets, verification logs, review verdicts (gitignored)
archive/, models/ (gitignored ONNX), var/, tests/   support dirs
PLAN.md, TAKEOVER.md, OWNER-DECISIONS.md             LOCAL-ONLY planning (gitignored)
START-HERE.md, README.md, IDEA.md, ARCHITECTURE.md   committed
```

## 3. The data layer (the heart of V3)

### 3.1 Canonical corpus — `data/canonical/`

| Path | Contents | Writer |
|---|---|---|
| `evidence/evidence-<hex>.jsonl` | 21,081 immutable observation rows (news 2,621 / tracker-note 18,460), sharded 16 files by id prefix | P2.2 importer only |
| `judgments/judgment-<hex>.jsonl` | 3,006 verdict rows: `judged-batch` 934 (dual-hash provenance), `accepted-corpus-inline` 1,261, `orphaned-flag` 811 (unverifiable, 292 liveness probes 200-OK) | P2.2 importer only |
| `entities/entities.json` | registry, `ge16.entity-registry.v1`: 44 seeded entities (schema `seeded_from: seed-vocabulary.json`) | entity CLI only |
| `entities/entity-candidates.jsonl` | 211 auto-proposed entities, all `status: proposed`, **never auto-promoted** | import scan |
| `entities/seed-vocabulary.json` | controlled vocab seed (6 blocs, 18 parties, sources) | hand-authored |
| `evidence/dupe-of-candidates.json` | 308 proposed duplicate-link candidates (154 accepted-corpus + 141 queue + 12 tracked-list + 1 judged-batch; `source_class` per row) | importer |
| `editions/edition-<UTCts>.json` | immutable snapshot manifests (§3.3) | edition/promotion writers |
| `dun/`, `geo/`, `research/` | verified DUN baseline (600/600), geo crosswalks, V2 tracker mirrors | P1 verified pipeline |
| `events/` | `ge16-events.db` — P2.5 baseline migration snapshot (512 events / 2,141 entities / 315 sources / 117 stories); P2.8 rebuilds it from V3 judgments | P2.5 migration → P2.8 builder |
| `links/` | **empty by design** — populated in P2.8 from judgments | future P2.8 builder |

**Core invariant — evidence/judgment separation:** an evidence row records
what was observed (never edited; corrections are new rows with `supersedes`);
a judgment row records what was decided about it (many per evidence allowed;
latest-wins for display, all retained for audit). Every judgment cites its
basis: batch file + `batch_file_sha256` (disk-verifiable), plus
`embedded_source_sha256` (the judged input generation, lost in V2 — kept as
history) with explicit `source_hash_semantics`.

### 3.2 Identity

- `evidence_id = "ev" + sha256(normalize_link.v1(link))[:16]` — deterministic,
  re-import-stable, human-spot-checkable.
- `normalize_link.v1`: strip query+fragment, lowercase host, decode HTML
  entities, drop tracking params (`utm_*`, `fbclid`). Versioned function;
  rule changes create `.v2`, never rewrite existing ids.
- `judgment_id = "jg" + sha256(evidence_id + judged_at + verdict)[:12]`.
- Timestamps live only in provenance fields — never inside id computation.

### 3.3 Editions — the snapshot discipline

- `ge16.edition.v1` (P2.2 import): `{schema, edition_id, created_at,
  content_hashes{path→sha256}, row_counts, lineage{prior_edition, inputs},
  note}`. Deterministic re-import produces byte-identical inputs; no-op
  reruns are recorded as their own editions.
- `ge16.edition.promotion.v1` (P2.3): same shape plus
  `lineage.run_id` binding the snapshot to the run dir whose staged outputs
  were promoted. Distinct schema; the frozen `ge16.edition.v1` is unchanged.
- Editions chain via `lineage.prior_edition` — a rebuild can always name the
  exact source snapshot it consumed (P2.10 will test this).
- **P2.4 verifier** — `data/scripts/integrity.py`, REPORT-ONLY (never
  writes/repairs; zero write calls, AST-pinned): subcommands `verify-edition`
  (re-hash every `content_hashes` path), `verify-chain` (broken
  `prior_edition` links, duplicate ids, timestamp order, promotion
  `run_id` resolvability — a pruned `data/work/` run is `run_pruned_ok`,
  legal), `verify-corpus` (`--full|--sampled N`, seeded: schema, id
  uniqueness, `evidence_id` re-derivation, judgment→evidence references,
  basis batch-file hash re-check, dupe-candidate refs), and `verify-recorded`
  (the additions rule: rows beyond every edition's `row_counts` are
  UNRECORDED additions; deficits are lost rows). `--json` for machines; exit
  codes 0 clean / 1 corrupt / 2 lost, worst wins. Wired into
  `refresh_canonical_data.py` run mode only, after promotion: edition +
  corpus checks, hard stop naming the edition on failure (`--no-run`
  untouched).
- Currently 3 editions (P2.2 import chain `20260928T142216Z` → …`145304Z`;
  P2.3 promotion editions follow the same pattern).

- **P2.5 baseline migration** — `data/scripts/migrate_baseline.py`, idempotent
  and content-diff-driven (`inventory`/`migrate --stage`/`migrate --promote`):
  diffs the six P1.3 canonical-data-provenance roots plus the one named gap
  (V2 `2_ANALYTICS/work/events/ge16-events.db`) against `data/canonical/`,
  stages missing/changed items under `data/work/<run_id>/baseline-stage/`
  (the events DB via the sqlite3 backup API — a consistent snapshot, never a
  live copy — with per-table row-count sidecars, since backup-API output is
  not byte-identical to the source and equivalence is judged by
  `integrity_check`+row counts, not raw sha256), then promotes into canonical
  only after verifying staged sha256s and refusing any destination collision.
  A successful promote writes one more `ge16.edition.v1` manifest (reused
  verbatim — the brief's proposed `ge16.edition.baseline-migration.v1` +
  `lineage.kind` would violate the frozen schema's and `integrity.py`'s
  `additionalProperties: false`/known-schema checks) and gates on
  `verify-chain` + `verify-edition`. Every P0.7 disposition row not covered by
  the file diff or the events-DB migration is recorded as `out-of-scope` with
  its own reason — nothing is silently dropped. Landed: `data/canonical/events/ge16-events.db`
  (512 events / 2,141 entities / 315 sources / 117 stories), edition
  `20260929T064041Z`.

### 3.4 Schemas — `data/scripts/schemas/` (9 files)

`ge16_{evidence,judgment,entity,entity-candidate,link,event,edition}.schema.json`
+ `ge16_edition-promotion.schema.json` + `ge16_source-checkpoint.schema.json`
(P2.6). All records jsonschema-validated (jsonschema, pinned). Event/link
schemas ship now, populated in P2.8.

### 3.5 Import pipeline — `data/scripts/import/`

| Module | Role |
|---|---|
| `normalize_link.py` | versioned URL normalizer (pure, unit-tested) |
| `identity.py` | deterministic id derivation |
| `parse_verdicts.py` | port of V2 `_compact_verdict` (`batch:index:accept[:cat[:blocs[:parties[:seats[:lang[:score]]]]]]`); round-trip tested on all 420 real lines |
| `import_evidence.py` | the importer. Classifies source docs **by content** (schema strings, document shape) — never by filename (RED test pins this: a file named `ge16-news-backfill-anything.json` with valid items MUST import). Dispositions from P2.1: accepted-corpus → evidence+judgments; judged-batch → provenance-backbone judgments; live-judged/queue → evidence only (their inputs are unverifiable — P2.1 Finding 2); tracked-lists → tracker-note rows carrying the P0.5 caveat (seen-key ≠ verified poll). Orphaned-flag items: judgment kept with `verifiable: false` + HTTP liveness probe recorded on the evidence row. Deterministic: re-run adds zero rows. |
| `entity_candidates.py` | vocab seed + auto-proposal scan + `approve|reject|list` CLI; nothing enters the registry without owner approval |
| `edition.py` | edition manifest writer |

### 3.6 Working state — `data/work/` (P2.3)

- One directory per run: `data/work/<run_id>/{trackers,judge,state,logs}/` +
  `run.json`; `run_id = YYYYMMDDTHHMMSSZ-<8hex>`.
- `data/scripts/work_paths.py`: `new_run(label)`, `current_run()` (env
  `GE16_RUN_DIR`), `apply_env()` — which **auto-wires the two collector knobs
  that already existed** (`GE16_TRACKER_OUT_DIR`, `GE16_SELFHEAL_STATE`, built
  in P1.7) so collectors stage into the run dir with **zero collector-file
  edits** (a packet prohibition, verified by empty diff).
- `refresh_canonical_data.py --run` (default): stages collectors → explicit
  promotion into canonical → `ge16.edition.promotion.v1` with `lineage.run_id`.
  `--no-run`: byte-identical pre-P2.3 behavior.
- **Retention rule: no code deletes run dirs or canonical artifacts**
  (AST-pinned tests). `data/work/` is gitignored — working state is
  reproducible; editions are the durable record.
- Known follow-up (non-blocking, from review): `apply_env` has no
  restore/scoping primitive yet; add one before any second in-process caller
  of `staged_refresh`.

### 3.7 Provenance chain (end to end)

```
V2 artifact (read-only, sha256-recorded in P2.1 classification)
  → P2.1 content-classified disposition (86 artifacts)
    → P2.2 import (content-based, deterministic ids)
      → evidence/judgment rows (dual-hash basis)
        → ge16.edition.v1 manifest (content_hashes, lineage)
          → P2.3 run staging + promotion (ge16.edition.promotion.v1)
            → canonical update, prior_edition chained
```

Every hop is hash-bound; any future audit can walk the chain from a canonical
row back to the V2 bytes it came from.

### 3.8 Source checkpoints, the complete news archive, and federal-results derivation (P2.6)

- **Checkpoints** — `data/canonical/checkpoints/<source_id>.json`
  (`ge16.source-checkpoint.v1`, one file per source — chosen over a single
  bundle so independent-cadence sources never serialize around one writer;
  see `data/scripts/source_checkpoints.py` module docstring §2.1). Records
  `{window_start, window_end, items_seen, items_accepted, collected_at,
  run_id, mode}` after a collection cycle. `compute_window(source, mode)`:
  baseline sweeps from a declared start (default 2026-01-01, R08) to now;
  incremental resumes exactly at the prior checkpoint's `window_end`. A true
  no-op incremental (`items_seen == items_accepted == 0`) skips the write
  entirely — stronger than the boundary's "only `collected_at` may mutate":
  a no-op touches zero bytes.
- **Complete accepted archive** — `data/canonical/archive/news-accepted/accepted.jsonl`,
  append-only, one line per accepted news item, never rewritten. The rolling
  `ge16-news-accepted.json` (2,367 items, byte-sensitive consumers) is
  untouched; the archive is the durable superset. Identity for dedup: the
  full item with its `link` normalized (`normalize_link.v1`) then hashed —
  link-only identity was tried and rejected (148 pairs of genuinely distinct
  accepted entries share one normalized link in the live corpus; it would
  undercount below the 2,367 floor). Baseline seed: 2,367/2,367 items
  appended, 0 skipped; re-seeding is byte-identical (0 appended). Committed
  to the repo: the `.gitignore` `archive/` pattern is root-anchored
  (`/archive/`, preserving the original root-level-archive ignore intent)
  with `!data/canonical/archive/` re-including the canonical tree's archive
  (owner decision 2026-09-29, MAJOR 3 — an uncommitted "durable" archive
  would not survive a fresh clone). Provenance/edition coverage for it is
  deferred to the P2.4-carryover work in P2.8 (verify-recorded extension),
  recorded in PLAN §8.
- **Mode layer** — `data/scripts/source_checkpoints.py` (its own
  `python3 source_checkpoints.py {seed-archive|run --mode baseline|incremental}`
  entry point, the brief's named alternative to editing the orchestrator).
  Drives `track_ge16_news.py` unmodified via its pre-existing
  `GE16_NEWS_MAX_DAYS` env knob (no new collector knob, no collector edit
  beyond the adjudicated candidates-collector staging fix — the collector
  diff allowlist covers exactly that one file). Failure policy: a non-zero
  collector exit raises `CollectionCycleError` and writes NO checkpoint —
  the failed window stays open and the next run retries from the same
  `window_end`. Archive capture: each successful cycle reconciles the
  archive against the rolling accepted corpus via idempotent re-seed
  (`seed_archive_from_corpus` → `append_items` dedupe), so items accepted
  by the async collect→judge→commit pipeline land exactly once, on the
  first cycle after their commit (re-review remediation 2026-09-29).
- **Per-state federal-results derivation** — `data/scripts/federal_results_derive.py`.
  Recon finding: the brief's named central inputs
  (`ge16-per-seat-projection.csv`, `ge16-battleground-seats-master.csv`)
  lack the candidate-level fields (`winner`/`votes`/`majority`/
  `previous_winner`) the existing `DUN Johor/federal-election-results-latest.csv`
  template actually carries; byte-identity against Johor's 26 rows is only
  achievable from the row-level source those rows were themselves built
  from — `research/raw/meco-candidates-ge15-federal.csv` (GE-15, 945
  candidate rows / 222 seats) for the current-election columns, and
  `meco-federal-election-candidates-1955-2022.csv` (GE-14 rows) for
  `previous_*`/`changed_hands`. The derivation is a pure function of those
  two files (deterministic, idempotent, CRLF-preserving to match the
  template byte-for-byte) and reproduces Johor's file byte-identically.
  Output grouping (brief §5 open question): 13 `DUN <State>/` dirs (one per
  state with an assembly) + one `federal-territories/` grouping file for
  Kuala Lumpur (11) + Putrajaya (1) + Labuan (1) — 14 files total, every one
  of the 222 seats in exactly one file (Sigma = 222).

## 4. Testing — `data/tests/`

203 tests green as of `0dd89c5`; **238** as of P2.5; **261** as of P2.6
(pytest, `-p no:cacheprovider`, `PYTHONDONTWRITEBYTECODE=1`; ~11 min full
run). P2.6 adds 20 collection-mode tests + 3 staging-contract tests
(`test_p2_6_staging_contract.py`: every collector routes tracker writes
through `ge16_tracker_outdir`, the env knob covers every
`COLLECTOR_FILES` basename, and a staged subprocess run leaves live
canonical tracker bytes+mtimes untouched — added after the candidates
collector was found bypassing staging).
Notable pins:

- Baseline migration (P2.5): idempotency (re-run stages nothing; empty
  promote is a zero-write no-op), collision refusal (canonical
  byte-identical on refusal), backup-API snapshot equivalence
  (integrity_check + per-table counts, not raw sha), verifier gate
  re-invoked independently of promote's self-report, V2 bytes/mtimes
  pinned untouched.
- Edition selection is key-aware: editions are multi-kind under the frozen
  schema, so tests select the baseline edition by the row-count key they
  exercise (`latest_corpus_edition`), matching `verify-recorded`'s walk.

- Determinism: repeated import/promotion adds no duplicates; identical runs
  produce identical file sets.
- RED test: filename-based exclusion would fail the suite (P1.7-R1
  regression guard).
- No-deletion: AST walk asserts zero deletion calls in work_paths; refresh
  may delete only its own scratch; double-promotion snapshot-diff keeps every
  prior canonical file.
- Parity: `--no-run` byte-identical to pre-P2.3 invocation; unset env =
  today's exact path resolution.
- Frozen-schema guard: P2.2 `ge16.edition.v1` fixture untouched by P2.3.
- Read-only verifier (P2.4): every subcommand leaves a tmp canonical copy
  bit-identical, and an AST walk pins zero filesystem-write calls in
  `integrity.py`; each of the 8 defect classes (corrupt byte, lost file,
  unrecorded row, legit supersedes, broken chain, sampling) injects a real
  defect into a tmp copy.

## 5. Quality gates and how work moves

Every task runs the same chain (details in the routing skill; model seats in
its registry, deliberately not named here):

```
packet (tier + acceptance criteria + prohibitions, router line)
  → CLI implementer → parent independent verification (re-runs, recounts,
  probes — never trusting the worker report)
    → cross-provider read-only reviewer (pytest self-executed)
      → parent commits → owner authorizes push
```

- Tiers T1–T5 classify risk; T3+ requires cross-provider review; children
  never commit; pushes are owner-gated.
- Evidence per task lives in `evidence/P<phase>/` (local-only): packets,
  briefs, verify logs, review findings, dispatch results.

## 6. Owner decisions baked into the architecture

- **OD5**: V2 is port-clean reference — copied, never fixed in place.
- **OD7**: incomplete editions need an owner-defined label (pending); no
  silent degraded publishes.
- **D6**: exclusion from canonical inputs ≠ deletion — flagged rows are kept
  and labeled (`verifiable: false`, `status: proposed`).
- **P0.5**: tracker seen-keys are never treated as verified polls.
- **No filename logic**: source selection is content-classified (P2.1
  dispositions), enforced by test.
- **Stable ids**: content-derived, re-import-stable; identity rules are
  versioned functions.

## 7. Known limitations (honest ledger)

1. 811 orphaned-flag judgments remain unverifiable by design (provenance
   lost in V2); 292 liveness probes passed; full re-judge is P2.7.
2. In-flight V2 queue judgments were not imported (inputs postdate their
   outputs' producing commit) — those items exist as evidence only, awaiting
   P2.7 re-judging.
3. 308 duplicate-link candidates (154 accepted-corpus + 141 queue + 12
   tracked-list + 1 judged-batch) unresolved pending owner review. The
   P2.2 report's "154" counted the accepted-corpus class only; the
   audit (2026-09-29) caught the prose/file mismatch — full breakdown
   is authoritative in `dupe-of-candidates.json`.
4. 211 entity candidates await owner approval (`entity_candidates.py list`).
5. `events/` holds the P2.5 baseline-migration snapshot (`ge16-events.db`);
   it is rebuilt from V3 judgments in P2.8. `links/` is empty until P2.8.
6.270 of 292 probe URLs are Google News wrappers — wrapper liveness, not
   publisher liveness.
7. `apply_env` env-restore scoping deferred until a second caller exists.

## 8. Maintenance rule

This manual is a living artifact, updated at **defined plan milestones** (see
PLAN.md §1 "Architecture manual updates"): after each plan phase completes
(P2…P7) and after any ad-hoc architectural change. Each update: refresh the
tables/invariants above against the actual tree, re-verify stated counts,
add new sections for new subsystems, and record the update in the changelog
below. The manual is committed; updates ride the phase's push.

**Plain-language companion:** `docs/ARCHITECTURE-APPENDIX-A.md` explains the
data layer file-by-file in plain language — what each file/function does,
who calls whom, and a who-writes-what map. It is updated on the same
milestones; when the two documents disagree, §3's spec wins and both get
fixed together.

### Changelog

| Date | Commit | Change |
|---|---|---|
| 2026-09-28 | `0dd89c5` | Initial manual: state as of P2.3 complete (P2.2 pushed `738d13a`, P2.3 local). |
| 2026-09-29 | (P2.4, local) | §3.3/§4: read-only integrity verifier (`integrity.py`) + refresh run-mode gate; suite 203→226. |
| 2026-09-29 | (P2.5, local) | §3.3/§4: baseline migration (`migrate_baseline.py`, events DB 512 events/2,141 entities landed, edition `20260929T064041Z`); suite 226→238; key-aware edition selection in test helpers. |
| 2026-09-29 | (P2.6, local) | §3.4/§3.8/§4: source checkpoints + complete accepted archive (2,367 items) + per-state federal-results derivation (222 seats, zero dups) + staging-contract tests; candidates collector outdir fix; provenance manifest 149→162; suite 238→264. |
| 2026-09-29 | (P2.6 addendum, local) | Added `ARCHITECTURE-APPENDIX-A.md` — plain-language data-layer manual (per-file/per-function how-it-works, call relationships, who-writes-what map), linked from §8; rides the P2.6 push. |
