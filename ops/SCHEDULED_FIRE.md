# Scheduled fires: contract 1.9.0 / bounded-live 1.3.0

This is an uncommitted security redesign for owner-authorized Stages 1–4.
HEAD is contract 1.8.0; the previous 1.9.0 working tree was never published, so
1.9.0 is rewritten in place. Stage 5 remains isolated-only. Review approval and
owner Keychain ACL verification are still required before relying on this design.
No unattended weekly minting is authorized.

## Public key, envelope, and review boundary

The runner has only `OPS/security/owner_ed25519.pub`. The former repository HMAC
secret and scheme have been removed. `validate_ops_contract.py` pins both:

- `OWNER_PUB_KEY_B64` (DER SubjectPublicKeyInfo):
  `MCowBQYDK2VwAyEAIHvi8ezkQTA3cjuEGgFQ9aY9N33kAw9BJ0RST0B3zPM=`
- `OWNER_PUB_KEY_SHA256` (exact PEM file, including newlines):
  `e590fcbe2b45b75095aded5bc9958216a25ca96edff554d752864bad1bc34fe6`

These are real, non-null pins in the working tree. The contract embeds the same
bytes/hash in `owner_public_key`; the validator compares them literally against
the code constants. The runner checks the actual public file before verification.
Changing only the contract or public file fails. Rotation requires both code and
contract edits passing the review gate. This is a two-artifact review requirement,
not a cryptographic two-person approval system: a same-user repo writer could
change both, so review and protection of executable code remain trust assumptions.

The envelope has exactly `payload`, `payload_b64`, and `signature`. Payload bytes
are sorted-key, compact UTF-8 JSON; both base64 fields must use canonical standard
base64, including padding/pad bits. `payload_b64` must exactly encode the canonical
`payload`. Ed25519 signs those bytes; a SHA-256 of them remains the consumption
identity. Different formatting of the envelope cannot bypass one-shot claims.

## Body

Scheduled-fire payloads bind `scheduler_binding` to the expected job `id` from
`scheduler_bindings`, `enabled: true`, and `state: scheduled`. The field
`scheduler_job_binding_sha256` is a job-record digest — not a whole-file hash —
because the scheduler rewrites jobs.json at fire time and every tick. It replaces
`scheduler_registry_sha256` only in scheduled-fire payloads.

The digest covers exactly these 18 fields from the single matching job:
`id`, `name`, `schedule`, `model`, `provider`, `state`, `workdir`,
`context_from`, `enabled_toolsets`, `prompt`, `skills`, `skill`, `script`,
`no_agent`, `base_url`, `monitor_script`, `monitor_url`, and `origin`. Real
Hermes `create_job` records never contain a top-level `paused` key, so all 18
fields must be present and a missing field fails closed with the same error as an
invalid state. `None` is a legitimate value — hashed as `null`, because the key
is always included — for optional payload fields such as `script`, `skill`,
`monitor_script`, and `monitor_url`. Hash input is sorted-key, compact JSON
encoded as UTF-8 with literal Unicode (`ensure_ascii=False`) and no trailing
newline. `enabled` must be exactly true and `state` must be `scheduled`; `state`
is itself part of the digest and is embedded in `scheduler_binding`. A paused or
disabled job, a missing required field, or a duplicate matching job fails closed
at minting and consumption.

Only the 18 listed fields are hashed. Extra fields are ignored, including the
runtime keys `fire_claim`, `last_*`, `next_run_at`, `model_snapshot`,
`provider_snapshot`, `updated_at`, `deliver`, `failure_streak`, `created_at`,
`paused_at`, `paused_reason`, `monitor_state`, `schedule_display`,
`inactivity_limit`, and `repeat.*`. `repeat` is skipped entirely because this
pipeline schedules `repeat=forever`. Other jobs and registry formatting do not
affect the digest. Runtime fire-claim changes leave that same key valid through
guard checks; configuration changes such as a schedule promotion, workdir
escape, model swap, or provider swap invalidate a minted key.

Because the digest now also covers the execution payload — `prompt` plus the
endpoint/agent fields `context_from`, `enabled_toolsets`, `skills`, `skill`,
`script`, `no_agent`, `base_url`, `monitor_script`, `monitor_url`, and `origin` —
a valid signature can no longer be paired with a different instruction or
endpoint. This closes the earlier signed-payload gap, where only configuration and
not the payload the job would actually execute was bound.

Payload fields retain all prior mode, UUID4, owner public-key hash, job, stage,
phase, contract/policy version, whole-contract/canonical-jobs hash, scheduled state,
one-shot, owner attribution, timestamp, and permission bindings. Payload fields
must match the new schema exactly. Lifetime remains at most 900 seconds. Expiry, revocation, registry,
and signature are rechecked by live guards; a failed run after claiming remains
consumed. Deployment, gateway start and schedule enablement remain false.
Manual authorization retains its union rule and separate paused/disabled job
binding, including its unchanged whole-registry `scheduler_registry_sha256`.
Script/module pins, `.git` denies, audit-hook `GIT_*` denies, reference checks and
sandbox fences are unchanged by this round.

## Owner-terminal minting and delivery

Delivery is exclusively under:
`/Users/faisal.muthalib/.ge16-keys/one-shot/`.
The owner must create a real, non-symlink directory owned by `faisal.muthalib`, mode
0700, outside all repositories. The runner rejects other directories, symlinks,
unsafe parent permissions, non-owner files, hardlinks and file modes other than
0600. This directory was provisioned by the owner on 2026-09-22 (mode 0700, owned by faisal.muthalib).

Owner terminal example, after provisioning and review:

```sh
python3 OPS/cron/mint_fire_key.py --confirm-owner --stage 1 --phase a --ttl-minutes 10 --out /Users/faisal.muthalib/.ge16-keys/one-shot/1-a-20260922T120000Z.json
```

Use a fresh timestamp in `<stage>-<phase>-<ts>.json` (`none` for single-phase
stages). Stage 1 defaults to phase a if omitted. Phase b needs a separate fresh
file after the agent's judging step. No file is overwritten. The exact file is
passed to `run_stage.py --contract ge16-cron-data-collection-validation --version
1.4.0 --scheduled-fire --fire-key <file> --stage 1 --phase a|b`. Stage 5 is rejected
by both mint and scheduled runner entry points, including explicit `--stage 5`.

The minter requires `--confirm-owner`, the owner UID, three TTY descriptors and a
foreground controlling terminal. It reads only service `ge16_owner_ed25519`,
account `faisal.muthalib`, using `/usr/bin/security find-generic-password ... -w`.
The stored value is canonical base64 of PKCS8 DER for an Ed25519 private key.
A mismatched private key cannot sign a fire accepted by this runner.

Only an item-not-found result combined with the explicit --init flag initializes a new key. Initialization submits
`add-generic-password -s ge16_owner_ed25519 -a faisal.muthalib -T '' -w <value>` through
`security -i` stdin, keeping the secret out of process arguments. No `-A`, trusted
`security` executable, update/overwrite, automatic unlock, unlock password,
AppleScript, retry, or ACL bypass is used. Initialization stops without minting
and reports public material only. It never edits the public file, pins, contract
or registry. A newly generated key will differ from the existing pinned key;
retain/use the matching existing Keychain item or explicitly review a rotation.

Signing uses cryptography when installed, otherwise the fixed
`/opt/homebrew/bin/openssl` CLI (OpenSSL 3 is required). The default pyenv Python
currently lacks cryptography; the existing Hermes venv has cryptography 50.0.0.
No dependency was installed. A temporary directory mode 0700 contains the signing
file `T`, mode exactly 0600, removed on normal completion or exception. Abrupt
process termination can leave temporary data; this is not secure erasure.

## Actual Keychain trust basis and outstanding owner check

Ed25519 prevents a holder of only the public key from signing. It does **not**
prove the signer was human. Same-user directory modes do not separate the agent
from the owner. The script's confirmation and TTY checks reject normal cron
invocations but a hostile same-user process can bypass the script or allocate a
PTY. A generic-password item is exportable private material, not a non-exportable
hardware signing key.

The intended operational boundary is a Keychain item requiring explicit human
approval in the owner terminal/login session. The owner must inspect the item ACL
and test that a non-interactive cron-context access is denied without disclosing
the item. Do not grant Always Allow to `security`, Python, an agent host or a shell.
Restrict access to the intended owner terminal and require human authorization;
Terminal identity and a login session alone are not proof that an initiating
request is human. Unlocked keychain state, permissive ACLs or cached authorization
may allow a direct `security` call to succeed. Do not approve an unexpected prompt.

Implementation-round status (2026-09-23, owner-applied): the Keychain item
`ge16_owner_ed25519` (account `faisal.muthalib`) HAS been provisioned by the
owner; the private key exists ONLY inside the login Keychain, and the disk
copy was deleted. `cron/mint_fire_key.py --init` documents the one-time
provisioning path; every later mint must omit it. On first entry, if the item
does not exist, the minter fails with
`Keychain item ge16_owner_ed25519 not found; run with --init` and writes
nothing.

The earlier implementation packet did not inspect the real Keychain.
The cron-context regression proves **the minter refuses before any Keychain
subprocess**; mocked denial tests prove it never retries/unlocks/provisions after
access denial. They do not prove deployment ACL enforcement. A hostile same-user
process could directly invoke Keychain APIs, automate a trusted app, abuse granted
accessibility rights, read the temporary private file during owner signing, or
alter reviewed code. Strong separation would require a distinct identity or an
external/non-exportable signer and is not supplied by this change. F3's real
Keychain boundary remains pending owner verification, not silently declared closed.
The required strict Stage-1 prompt is an operational rule under these assumptions.

## Registry and patch state, observed 2026-09-22

Read-only inspection confirmed the earlier per-job watchdog helper is already
installed, and Stage 1 job `441fedd48bc8` is enabled/scheduled with
`inactivity_limit: 1800`. The old claim that neither patch had been applied was
incorrect. This session did not apply them; prior authorship is not established
by file contents or timestamps.

The owner reports manually pausing Stage 5 using the cron tool. Read-only
inspection confirms job `9194211d627e` has `enabled:false, state:paused`.
Codex did not mutate either registry job. Canonical Stage 1 now has the exact
delivery directory and required owner-signature instruction. This session did
not publish canonical prompts into the live registry; Stages 2–5 prompts are
unchanged. The chain is not activated by this packet.

`patches/scheduler-per-job-inactivity.patch` is now an **unapplied follow-up**
against the observed installed scheduler/jobs source. It enforces exact integer
1..3600 at job creation/update, before scheduling/persistence, and again at runtime.
Booleans, null, fractions, strings, zero, negatives and values above 3600 error.
Only an absent field uses the existing global/default timeout. The canonical
validator enforces this bound and retains Stage 1's stricter value of 1800.
`stage1-inactivity.patch` describes the already-observed 1800 change; do not reapply
it blindly. Applying the new external patch remains owner work, prohibited here.

## Chain mode (completion-triggered, optional)

Chain mode removes the clock from the weekly chain. Instead of five independent
fires at fixed times, the run advances the moment the previous stage's handoff
manifest validates, so a fast stage no longer waits hours for its slot.

The owner-side listener is `OPS/cron/ge16_chain_runner.py` (same file next to
`ge16_remint.py` in the profile directory), launchd label
`com.tenunan.ge16-chain`, template `OPS/cron/launchd/com.tenunan.ge16-chain.plist`,
one poll every 2 minutes, state `/Users/faisal.muthalib/.hermes/profiles/coding/cron/ge16-chain-state.json`,
evidence `OPS/logs/chain`, log `~/.hermes/logs/ge16-chain.log`.

- The chain job `ge16-chain` (`ge16-cron-chain-orchestrator` 1.0.0, Friday 22:00
  KL, ships **disabled**) is the owner-visible start: its agent runs
  `python3 OPS/cron/ge16_chain_runner.py --start` and then executes Stage 1
  (phase a, its own judgement, phase b) with Stage 1 keys. Those keys are minted
  owner-side and surfaced by the trigger itself: while Stage 1 is waiting, each
  poll mints a phase a and a phase b key when that phase has no unconsumed,
  unexpired one, exactly the rule the remint daemon uses. The agent never mints,
  never drives another stage and never writes a pipeline domain.
- The listener owns the two side effects: it mints one one-shot owner key per
  driven stage through the same `/usr/bin/script` owner-terminal path the remint
  daemon uses, and it launches
  `python3 OPS/cron/run_stage.py --contract <id> --version <version> --scheduled-fire --fire-key <fresh key> --stage N`.
  Nothing else changes in the runner's live path.
- Completion is content-hash tracked, never time- or timestamp-guessed: a stage
  counts only when its handoff manifest is present, differs by SHA-256 from the
  snapshot taken at run start, and (Stage 2 onwards) cites the recorded SHA-256
  of the immediate predecessor manifest. A manifest that appears for the run
  while the chain waits is recorded as complete, so an already-done stage is
  never re-executed and never re-minted.
- Stage 1 and Stage 5 stay agent-driven. `run_stage` allows bounded-live
  execution for Stages 1-4 and Stage 5 is isolated-dry-run-only, so the listener
  drives 2, 3 and 4 only; the optional authoring step after Stage 2 is
  agent-side and non-blocking, so the chain proceeds Stage 2 -> Stage 3.
- Failure stops the chain. A stage handoff missing inside its window
  (240/600/30/120 minutes for Stages 1-4), a refused mint, a runner that exits
  without a handoff, or the 12-hour run deadline records a stop reason and
  advances nothing. The next weekly run resumes at the first un-completed stage.
  Stage 5 is bounded by the run deadline instead of a 45-minute window: it is
  owner-authorized release work whose own job fires at 05:30 after the Stage 4
  handoff, and 45 minutes is that job's inactivity limit.
- The five stage jobs stay scheduled as the paused-functional fallback floor, and
  every one of them (and the chain job) opens with the same no-op sentence: if
  the chain already produced this stage's handoff for the current run, verify,
  record and exit. So a chain that never starts, or dies mid-week, still leaves a
  complete Saturday chain behind it.

Chain mode needs no runner change, but it does need the four stage job records
to stay enabled/scheduled in the live registry: the mint and the fire both bind
the live job-record digest. `hermes cron run <id>` also refuses a paused job, so
pausing the stage jobs disables chain mode as well as the fallback floor.

```sh
python3 OPS/cron/ge16_chain_runner.py --status   # read-only chain state
python3 OPS/cron/ge16_chain_runner.py --poll     # one listener pass
python3 OPS/cron/ge16_chain_runner.py --start    # owner on-demand chain trigger
```

## Offline verification

```sh
cd OPS
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test*.py'
PYTHONDONTWRITEBYTECODE=1 python3 validate_ops_contract.py
PYTHONDONTWRITEBYTECODE=1 python3 tests/fixtures/verify_scheduled_fire.py
```

Fixtures use newly generated temporary keys, mocked Keychain/owner terminal
access and temporary repositories. They never access the real private key.
Native-only tests explicitly skip when nested macOS sandboxing is unavailable;
fixture children then exercise the existing audit hook and generated profiles.
The replay script reproduces Opus attacks i–iv with adapter dispatch mocked.
Actual transcripts and a unified review diff are in `verification/schedfire-round2`.
