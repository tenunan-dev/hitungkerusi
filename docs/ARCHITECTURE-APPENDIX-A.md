# Appendix A — The data layer in plain language (how each file works)

*Added 2026-09-29 (P2.6 complete, commit `7caccc4`). This appendix explains the
same system as §3, but in plain language: what each file is for, how its
functions work, and who calls whom. When §3 and this appendix disagree, §3 is
the spec and this appendix is the explanation — file an issue by updating both.*

---

## A.1 The big picture in five sentences

1. **Collectors** watch the outside world (news feeds, poll announcements,
   candidate news) and write what they see into *tracker files* — fast-moving
   working notes.
2. **The importer** turns the trackers that matter into the *canonical
   corpus* — immutable evidence rows and judgment rows with ids derived from
   content, so the same input always produces the same row.
3. **Editions** are snapshots: after every real change, a manifest records
   "these files, these hashes, built on the previous snapshot". The chain of
   editions is the corpus's history.
4. **The verifier** (`integrity.py`) re-checks any snapshot on demand — it
   can prove the corpus is exactly what the editions say it is, and can
   prove nothing was lost or corrupted. It never repairs; it only reports.
5. **Working state** (`data/work/<run_id>/`) is scratch space per run; only
   *promotion* copies staged results into canonical, and promotion writes
   another edition so the copy is traceable.

The rule that keeps this trustworthy: **canonical files are append-only or
hash-pinned; nothing edits history in place; every writer announces what it
wrote via an edition.**

---

## A.2 `data/scripts/collect/` — the collectors

| File | What it does |
|---|---|
| `track_ge16_news.py` | Pulls Google News RSS on GE16 queries. Three steps, run separately: `collect` (fetch → `ge16-news-candidates.json`, a candidate queue), `--judge-input` (prepare LLM-judgment batches), `--commit` (merge judged items into `ge16-news-accepted.json`, the rolling accepted corpus, and update the seen-keys file). Only `--commit` advances the seen file — a crashed run leaves work pending, never half-committed. |
| `track_ge16_polls.py` | Watches for poll releases; appends findings to `ge16-poll-tracker-log.md`, dedupes via `ge16-polls-tracked.json`. |
| `track_ge16_candidates.py` | Same pattern for candidate/seat-allocation news (`ge16-candidate-tracker-log.md`, `ge16-candidates-tracked.json`). |
| `ge16_tracker_outdir.py` | The **staging switchboard**. Every tracker-file read/write in the collectors above goes through its `r()` / `w()` / `a()` helpers. When the env knob `GE16_TRACKER_OUT_DIR` is set, writes land in the run dir instead of live canonical; reads prefer the staged copy when the run "owns" the file; appends seed the staged copy with the live bytes first so log history survives promotion. When the knob is unset, every helper returns the live path — collectors behave exactly as before. |

**Who calls whom:** `refresh_canonical_data.py` (run mode) sets the knobs and
invokes the three collectors as subprocesses; `source_checkpoints.py` invokes
`track_ge16_news.py` the same way. The collectors never import the refresh
orchestrator — the dependency arrow points one way: orchestrator → collector.

**The staging contract** (test-pinned after the P2.6 finding): *every*
collector must route tracker paths through `ge16_tracker_outdir`, and a run
with the knob set must leave live canonical bytes and mtimes untouched.
`data/tests/test_p2_6_staging_contract.py` checks this per collector.

---

## A.3 `data/scripts/work_paths.py` — run-scoped working state

- `new_run(label)` → makes `data/work/<run_id>/` with `trackers/ judge/
  state/ logs/` subdirs and a `run.json`; `run_id = <UTC timestamp>-<8 hex>`.
- `current_run()` → reads `GE16_RUN_DIR` from the environment; returns None
  when unset, which is the "no run" mode where everything stays live.
- `apply_env(run)` → sets `GE16_RUN_DIR`, `GE16_TRACKER_OUT_DIR` and
  `GE16_SELFHEAL_STATE` in an environment dict so child processes inherit
  the staging. This is the function that makes "zero collector edits" work:
  the collectors already had the knobs; this wires them per run.

**Relationships:** `refresh_canonical_data.py` and `source_checkpoints.py`
both create runs and call `apply_env` before spawning collectors. Nothing in
`collect/` calls `work_paths` directly — they only read the environment.

---

## A.4 `data/scripts/source_checkpoints.py` — collection modes (P2.6)

Purpose: make collection **resumable and failure-safe**. One checkpoint file
per source (`data/canonical/checkpoints/news.json`) answers "where did I get
up to?".

Functions, in call order for one cycle:

1. `read_checkpoint(source_id)` → the last recorded `{window_start,
   window_end, items_seen, items_accepted, collected_at, run_id, mode}`, or
   None if this source never ran.
2. `compute_window(source_id, mode)` → the time window to cover.
   `baseline` sweeps from a declared start (2026-01-01, owner ruling R08) to
   now; `incremental` starts exactly at the prior checkpoint's `window_end`
   (falling back to baseline when no checkpoint exists yet).
3. `run_news_collection(mode)` → the cycle itself:
   - runs `track_ge16_news.py` as a subprocess with
     `GE16_NEWS_MAX_DAYS=<window days>` (an existing knob — the collector is
     not modified);
   - **if the collector exits non-zero** → raise `CollectionCycleError`
     (carries exit code + stderr tail). *No checkpoint is written and the
     archive is untouched* — the failed window stays open, and the next run
     recomputes the same window and retries. This is the "failures do not
     advance checkpoints" rule, enforced by
     `RunNewsCollectionCycleTests::test_failed_collector_raises_and_writes_no_checkpoint`.
   - if the collector succeeded → reconcile the archive:
     `seed_archive_from_corpus()` asks `append_items()` to copy every item
     in the rolling accepted corpus that the archive does not have yet,
     deduped by archive identity. Items committed to the accepted corpus by
     the async judge→commit pipeline land in the archive on the first cycle
     after their commit, exactly once (second cycle appends zero).
   - finally `write_checkpoint(...)` records the window as covered. A true
     no-op incremental (nothing seen, nothing accepted) skips the write
     entirely — zero bytes move.
4. `seed_archive_from_corpus` / `append_items` / `archive_count` — the
   archive mechanics. `append_items` is append-only JSONL; identity is the
   item's normalized link (`normalize_link.v1`) hashed together with the
   item content, so genuinely distinct items that share a URL are both kept.
5. CLI: `python3 source_checkpoints.py {seed-archive | run --mode
   baseline|incremental}`.

**Relationships:** reads the checkpoint it wrote before; invokes
`track_ge16_news.py` (via subprocess, env-wired); writes
`data/canonical/checkpoints/news.json` and
`data/canonical/archive/news-accepted/accepted.jsonl`. It does not call the
importer — new accepted items reach the corpus through the importer, not
through this module.

---

## A.5 `data/scripts/federal_results_derive.py` — per-state federal results (P2.6)

Purpose: every state directory gets a `federal-election-results-latest.csv`
answering "which federal seats sit in this state, who won each last time".
Feeds P3.7 state reports without special-casing.

The pipeline is a pure function of two raw inputs:

- `research/raw/meco-candidates-ge15-federal.csv` (GE-15 candidates, 945
  rows / 222 seats) → current-election columns;
- `research/raw/meco-federal-election-candidates-1955-2022.csv` (historic
  rows) → `previous_winner`, `changed_hands`.

Functions:

- `derive_rows(...)` — joins the two sources per seat into output rows.
- `group_by_output_file(rows)` — 13 `DUN <State>/` groups + one
  `federal-territories/` group (KL 11 + Putrajaya 1 + Labuan 1). Every one
  of the 222 seats lands in exactly one file (Σ = 222, test-pinned).
- `write_csv_bytes(rows)` — deterministic CSV bytes, CRLF to match the
  original Johor template byte-for-byte.
- `verify_johor_byte_identity()` — the self-check: derived Johor must equal
  the existing Johor file byte-for-byte (it does; this is how the derivation
  is proven correct without a second opinion).
- `stage_outputs(destination_root)` — writes all 14 CSVs plus
  `federal-results-staged-manifest.json` (per-file rows + sha256 +
  `derivation_lineage` recording both input hashes) into a stage dir;
  promotion (the refresh run layer) does the collision-refusing copy into
  canonical.

**Relationships:** reads only `research/raw/` CSVs; writes only into a stage
dir it was handed; never writes canonical directly.

---

## A.6 `data/scripts/integrity.py` — the read-only verifier (P2.4)

Four subcommands, all report-only (an AST test pins that the file contains
zero filesystem-write calls):

| Command | Question it answers |
|---|---|
| `verify-edition <id>` | "Does every file this edition pinned still have the recorded hash?" Detects corrupt or replaced files. |
| `verify-chain` | "Is the edition history coherent?" — every `prior_edition` link resolves, no duplicate ids, timestamps ordered, promotion `run_id`s resolve (a pruned run dir is legal and reported as such). |
| `verify-corpus` | "Is the data itself healthy?" — schema-valid rows, unique ids, `evidence_id` re-derivable from the link, every judgment referencing real evidence, batch-file hashes matching. |
| `verify-recorded` | "Is anything unrecorded or lost?" — rows beyond every edition's counts are unrecorded additions; deficits are lost rows. |

Exit codes: 0 clean, 1 corrupt, 2 lost — worst wins. Wired into the refresh
run mode as a hard gate after promotion, and usable standalone (`--json`).

**Relationships:** reads editions + canonical files; writes nothing. Both
`refresh_canonical_data.py` (after promote) and `migrate_baseline.py`
(before promote) call it as a gate; CI/tests call it directly.

---

## A.7 `data/scripts/refresh_canonical_data.py` — the orchestrator

Run modes:

- **run mode (default, `--run`)** — the staged pipeline: `work_paths.new_run`
  → `apply_env` → run the three collectors as subprocesses (their tracker
  writes land in the run dir) → `current_source` diff decides what changed →
  collision-refusing promotion into canonical → one
  `ge16.edition.promotion.v1` manifest with `lineage.run_id` →
  `integrity.py` verify-edition + verify-corpus as a hard gate.
- **`--no-run`** — byte-identical to the pre-P2.3 behavior (live writes), for
  parity.

**The promotion rule:** nothing goes from a run dir into canonical without
(a) an explicit copy step that refuses to overwrite a changed destination,
and (b) an edition recording the result. This is why the corpus's history is
complete.

---

## A.8 `data/scripts/import/` — corpus construction (P2.2)

| Module | Plain-language role |
|---|---|
| `normalize_link.py` | `normalize_link.v1`: strip query+fragment, lowercase host, decode entities, drop tracking params. Versioned — a rule change ships as `.v2`, never rewriting old ids. |
| `identity.py` | `evidence_id = "ev" + sha256(normalized link)[:16]`; `judgment_id = "jg" + sha256(evidence_id + judged_at + verdict)[:12]`. Deterministic: same input → same id, forever. |
| `parse_verdicts.py` | Reads V2's compact verdict strings (`batch:index:accept[:...]`) into structured rows; round-trip tested on all 420 real lines. |
| `import_evidence.py` | The importer. Classifies V2 files **by content** (never filename — a RED test proves a renamed valid file still imports). Dispositions: accepted-corpus → evidence + judgments; judged-batch → judgment backbone; live-judged/queue → evidence only (unverifiable inputs); tracked-lists → tracker-note rows carrying the seen-key caveat; orphaned flags → kept with `verifiable: false` + liveness probe results. Re-running adds zero rows. |
| `entity_candidates.py` | Controlled vocabulary seeds the registry; anything unrecognized becomes a `status: proposed` candidate. Nothing enters the registry without owner approval. |
| `edition.py` | Writes `ge16.edition.v1` manifests. |

**Relationships:** the importer consumes tracker/judged files (V2 exports or
live-collected), produces canonical rows, then asks `edition.py` to record
the snapshot. Identity flows one way: normalize → hash → id; timestamps
never enter an id.

---

## A.9 `data/scripts/migrate_baseline.py` — the V2→V3 bridge (P2.5)

One-time-but-idempotent migration: `inventory` lists what V2 has that V3
lacks; `migrate --stage` copies missing/changed items into the run's
baseline-stage dir (the events DB via sqlite backup API — a consistent
snapshot with row-count sidecars, since backup output is not byte-identical
but is judged equivalent by `integrity_check` + counts); `migrate
--promote` verifies staged hashes and refuses destination collisions, then
writes an edition and gates on the verifier. Every P0.7 disposition not
covered by the diff gets an explicit `out-of-scope` row — nothing silently
dropped.

---

## A.10 File-relationship map (who writes what)

```
collectors (collect/)                    source_checkpoints.py
  track_ge16_news ──┐                     │ runs news collector,
  track_ge16_polls ─┤ ge16_tracker_outdir │ writes checkpoint + archive
  track_ge16_cand. ─┘ (staging switch) ←───┤
        │                                  │
        ▼ live (no knob) or run dir        ▼
  research/trackers/*.json,md    canonical/checkpoints/news.json
        │                          canonical/archive/.../accepted.jsonl
        ▼                                  │
  refresh_canonical_data.py ───────────────┘ (both feed the orchestrator)
        │ stage → promote → edition → integrity gate
        ▼
  data/canonical/** (evidence, judgments, entities, editions, research)
        ▲
  import_evidence.py (P2.2 corpus build; deterministic re-import)
  migrate_baseline.py (P2.5 V2→V3 bridge; edition + gate)
  federal_results_derive.py (P2.6; stage only, promotion copies)
```

Reading the map: collectors and the mode layer are the only things that
touch tracker files; the orchestrator is the only thing that promotes into
canonical; the importer and migrator are the only bulk writers; editions
record everything; the verifier reads everything and writes nothing.

---

## A.11 Invariants worth remembering (plain versions)

1. **Same input, same row** — ids come from content, so re-importing never
   duplicates.
2. **A failure never pretends progress** — failed cycles leave the window
   open; nothing half-commits.
3. **Nothing enters canonical quietly** — promotion + edition + verifier
   gate, every time.
4. **The verifier never repairs** — a human (or an adjudicated packet)
   decides what to do with a finding.
5. **Working state is disposable; editions are forever** — you can delete
   `data/work/` and lose nothing but scratch (the chain records pruned runs
   as legal).
