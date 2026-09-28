# outputs/ — release payload staging and sealing

## Purpose
Owns the output release contract and the validators that stage and seal
forecast/report releases. Only sealed releases may flow to delivery.

## Layout
- `output-contract.json` — the release contract
- `seal_release_intake.py` — stage an intake or bind its committed tree
- `validate_output_contract.py`, `validate_release_intake.py`
- `tests/` — contract validator tests (not yet green in V3)

## Commands
None verified in V3 yet. The validator suite still assumes the V2 repo layout
(a `scripts/` subdir and a local-Git sealing anchor); the remap lands with P5.
Don't list it as a passing gate until it passes.

## Rules
- Editions are sealed snapshots only: immutable after sealed validation
  against an exact 40-character commit.
- Sealed releases are append-only; a staging intake may be corrected, but it
  must never overwrite a sealed release.
- Incomplete editions must never publish silently; a degraded edition
  requires an explicit owner label (OD7 wording pending).

## Don't
- Don't deploy, push or call external APIs from here.
- Don't create mutable release aliases.
- Don't hand delivery an unsealed release.
