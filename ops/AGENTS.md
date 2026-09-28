# ops/ — chain runner and coordination

## Purpose
Owns the GE16 five-stage contract runner, the completion-triggered chain
coordinator, the job registry and the ops contract validator.

## Layout
- `cron/` — `run_stage.py`, `ge16_chain_runner.py`, `sync_jobs.py`, `jobs/`,
  `mint_fire_key.py`, `ed25519_support.py`
- `cron-jobs/` — `ge16-authoring.json`, `ge16-chain.json`
- `migration/` — inventory builder and schema
- `tests/`, `ops-contract.json`, `validate_ops_contract.py`

## Commands
None runnable against V3 domains yet. The runner is still keyed to the V2
five-repo domain names (`1_DATA`, `2_ANALYTICS`, `3_OUTPUTS`, `4_DELIVERY`,
`5_WEBSITES` — V2 legacy names) until the P4 remap lands.

## Rules
- The runner is fail-closed: live execution requires a fresh owner
  authorization key; never bypass scheduler-binding checks.
- Chains advance on completion, not clocks; don't retime stage jobs
  independently.
- Scheduler state and deployment remain forbidden through this runner.

## Don't
- Don't run or modify live cron until the P4 remap.
- Don't mint fire keys from agent context; minting is owner-side only.
- Don't treat historical activity evidence as deploy approval.
