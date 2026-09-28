#!/usr/bin/env python3
"""Fail-closed, read-only GE16 scheduler configuration checker and planner.

This module intentionally has no scheduler client and no apply mode.  Hermes owns
any separately authorized live mutation outside this repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from typing import Any, Dict, Iterable, List, Optional, Tuple


class SyncError(ValueError):
    """Raised for an unsafe or invalid scheduler definition."""


ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "cron" / "ge16_jobs.json"
RUNNER_PATH = ROOT / "cron" / "run_stage.py"
FINAL_RUNNER_WORKDIR = "/Users/faisal.muthalib/Documents/HermesWorkFolder/Malaysia General Election v2"
TARGET_IDS = (
    "441fedd48bc8",
    "2b0a9111c836",
    "c4cebfce9fb0",
    "152172eb38fd",
    "9194211d627e",
)
LEGACY_ID_MAPPINGS = (
    ("2f817443c8e9", "441fedd48bc8"),
    ("4c3dee85457f", "2b0a9111c836"),
    ("22f17e2baabd", "c4cebfce9fb0"),
    ("a62815d7de8a", "152172eb38fd"),
    ("b539a7a77c39", "9194211d627e"),
)
OWNERS = ("1_DATA", "2_ANALYTICS", "3_OUTPUTS", "4_DELIVERY", "5_WEBSITES")
WORKDIRS = (FINAL_RUNNER_WORKDIR,) * 5
NAMES = (
    "GE16 P1.4 1_DATA Collection and Validation",
    "GE16 P1.4 2_ANALYTICS Forecast Reports and Social Outputs",
    "GE16 P1.4 3_OUTPUTS Immutable Release Intake and Sealing",
    "GE16 P1.4 4_DELIVERY Publish and Validation",
    "GE16 P1.5 5_WEBSITES Build and Release Gate",
)
CONTRACT_VERSIONS = ("1.4.0", "1.4.0", "1.4.0", "1.4.0", "1.5.0")
CONTRACT_IDS = (
    "ge16-cron-data-collection-validation",
    "ge16-cron-analytics-forecast-reports-social",
    "ge16-cron-outputs-intake-sealing",
    "ge16-cron-delivery-publish-validation",
    "ge16-cron-websites-build-release-gate",
)
PROMPT_HASHES = (
    "521b656290fbd56051fa47ea42e067bbc41df9bea32d176677df4b758731ad8e",
    "c2b3685fa04d851c52c05ce1a45ffd548449aea767772cd1381eb84ab30809ad",
    "5d6143905eb91e2bcb91627576358f2d285404cf79cffeff2bf16c8da9b83630",
    "88a00f083a368175d8d38433df04a893e031c5649f8e0da290dcf7b6126df2e1",
    "fe723aa5e532d61a7ba2cf39ff9543fef61712777aaeec00c8010e15913858ee",
)
PROMPT_SEMANTICS = (
    ("stage 1", "data", "immediate 2_analytics handoff", "manifest", "freshness", "gate", "stop reason"),
    ("stage 2", "analytics", "immediate immutable 1_data handoff", "outputs", "manifest", "freshness", "gate", "stop reason"),
    ("stage 3", "outputs", "immediate immutable 2_analytics output", "delivery", "manifest", "freshness", "gate", "stop reason"),
    ("stage 4", "delivery", "immediate immutable 3_outputs sealed", "websites", "manifest", "freshness", "gate", "stop reason"),
    ("stage 5", "websites", "immediate immutable 4_delivery", "vercel", "manifest", "gate", "readback", "stop reason"),
)
SCHEDULES = (
    "0 22 * * 5",
    "0 1 * * 6",
    "0 3 * * 6",
    "0 5 * * 6",
    "30 5 * * 6",
)
# The optional companion job: the AI-authored narrative edition. It is NOT a
# functional-boundary stage, so it may only ever appear *after* the five stage
# jobs, disabled, non-blocking, and with everything about it pinned here. The
# canonical config is allowed to carry it (and the OPS contract its matching
# scheduler_binding) so the step is declared in the same source of truth as the
# stages it follows; nothing else may be added to the list.
#: The top-of-prompt acknowledgment every GE16 job carries once chain mode
#: exists: a stage whose handoff for the current run is already recorded must
#: verify and record, never re-execute. It is a no-op sentence, not authority.
NOOP_STAGE_PREFIX = (
    "If the chain orchestrator has already produced this stage's handoff manifest for the current run_id, "
    "verify and record it as complete (no-op) and exit; else execute your stage normally."
)
OPTIONAL_JOB_IDS = ("ge16-authoring", "ge16-chain")
OPTIONAL_JOB_NAMES = (
    "GE16 P1.5 2_ANALYTICS AI-Authored Narrative Edition (optional)",
    "GE16 P1.6 OPS Chain Orchestrator (optional)",
)
OPTIONAL_JOB_CONTRACT_ID = "ge16-cron-analytics-authoring"
OPTIONAL_JOB_CONTRACT_VERSION = "1.0.0"
OPTIONAL_JOB_SCHEDULE = "0 2 * * 6"
OPTIONAL_JOB_STAGE_NUMBER = 2
OPTIONAL_JOB_MODEL = "deepseek/deepseek-v4-pro"
OPTIONAL_JOB_WRITES = ("2_ANALYTICS/03_REPORTS/ai", "2_ANALYTICS/work/reports")
OPTIONAL_JOB_MUST_NOT_WRITE = (
    "2_ANALYTICS/03_REPORTS/federal",
    "2_ANALYTICS/03_REPORTS/states",
    "1_DATA", "3_OUTPUTS", "4_DELIVERY", "5_WEBSITES", "OPS",
)
OPTIONAL_JOB_PROMPT_TERMS = (
    "author_reports.py", "non-blocking", "never commit", "03_reports/federal",
)
# The second optional companion job: the OPS chain orchestrator. It owns no
# pipeline domain, it may only ever appear *after* the authoring job, and it
# ships disabled with a pinned schedule, model, toolset, prompt and write list.
# Everything about it is pinned here so a registry edit cannot turn it into a
# sixth live stage, give it a domain, or let it mint keys on the agent side.
CHAIN_JOB_ID = "ge16-chain"
CHAIN_JOB_CONTRACT_ID = "ge16-cron-chain-orchestrator"
CHAIN_JOB_CONTRACT_VERSION = "1.0.0"
CHAIN_JOB_OWNER_DOMAIN = "OPS"
CHAIN_JOB_SCHEDULE = "0 22 * * 5"
CHAIN_JOB_MODEL = "deepseek/deepseek-v4-pro"
CHAIN_JOB_INACTIVITY_LIMIT = 1800
CHAIN_JOB_STATE_FILE = "/Users/faisal.muthalib/.hermes/profiles/coding/cron/ge16-chain-state.json"
CHAIN_JOB_RUNNER_PATH = "OPS/cron/ge16_chain_runner.py"
CHAIN_JOB_WRITES = ("OPS/logs/chain",)
CHAIN_JOB_MUST_NOT_WRITE = OWNERS
CHAIN_JOB_PROMPT_TERMS = (
    "ge16_chain_runner.py", "--start", "never mint", "stage 1", "stage 5", "fallback",
)
CHAIN_JOB_PROMPT_SHA256 = "64f8caf3cfa060c7c3377bf2fbec6e0853c9d1b6b1d435ef201f01407afd803d"


def _fail(message: str) -> None:
    raise SyncError(message)


def _string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value:
        _fail("%s must be a non-empty string" % context)
    return value


def _string_list(value: Any, context: str) -> List[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        _fail("%s must be a list of non-empty strings" % context)
    return value


def _expected_model(index: int) -> str:
    return "deepseek/deepseek-v4-pro" if index == 1 else "z-ai/glm-5.3-flash"


def _validate_prompt(prompt: Any, index: int, context: str) -> None:
    text = _string(prompt, "%s prompt" % context)
    lowered = text.lower()
    missing = [term for term in PROMPT_SEMANTICS[index] if term not in lowered]
    if missing:
        _fail("%s prompt is semantically incomplete: missing %s" % (context, ", ".join(missing)))
    if not text.startswith(NOOP_STAGE_PREFIX):
        _fail("%s prompt must open with the chain no-op acknowledgment sentence" % context)
    command = "python3 OPS/cron/run_stage.py --contract %s --version %s" % (CONTRACT_IDS[index], CONTRACT_VERSIONS[index])
    if command not in text or not RUNNER_PATH.is_file():
        _fail("%s prompt runner command must name the existing exact contract runner" % context)
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != PROMPT_HASHES[index]:
        _fail("%s prompt hash/content does not match the canonical Stage %d instruction" % (context, index + 1))
    if "hermes" in lowered:
        _fail("%s prompt contains stale HERMES path or authority" % context)


def _validate_schedule(schedule: Any, index: int, context: str) -> None:
    if not isinstance(schedule, dict):
        _fail("%s schedule must be an object" % context)
    expected = SCHEDULES[index]
    if schedule != {"kind": "cron", "expr": expected, "display": expected}:
        _fail("%s schedule must exactly be %s" % (context, expected))


def _lock_retired(migration: Dict[str, Any]) -> bool:
    if str(ROOT) not in sys.path:
        # Standalone use (`python3 cron/sync_jobs.py`) has no OPS root on
        # sys.path; without this the import below dies with
        # ModuleNotFoundError and the read-only drift checker is unusable
        # for exactly the enablement readback it exists for.
        sys.path.insert(0, str(ROOT))
    from validate_ops_contract import PERMANENT_CONTROLS
    return (
        isinstance(migration, dict)
        and migration.get("lock_retired") is True
        and migration.get("permanent_controls") == PERMANENT_CONTROLS
    )


def validate_canonical_config(config: Any, migration: Dict[str, Any], cron_contracts: Any = None) -> None:
    """Validate the declarative source independently of any live registry."""
    if not isinstance(config, dict):
        _fail("canonical config must be an object")
    if config.get("schema") != "ge16.ops.cron-jobs.v1":
        _fail("canonical config schema must be ge16.ops.cron-jobs.v1")
    if config.get("version") != "1.5.0":
        _fail("canonical config version must be 1.5.0")
    if config.get("migration_identities") != list(TARGET_IDS):
        _fail("canonical config migration_identities must be the five ordered V2 IDs")
    expected_legacy_mappings = [
        {"stage_number": index + 1, "legacy_id": legacy_id, "active_target_id": target_id}
        for index, (legacy_id, target_id) in enumerate(LEGACY_ID_MAPPINGS)
    ]
    if config.get("legacy_id_mappings") != expected_legacy_mappings:
        _fail("canonical config legacy_id_mappings must exactly map every retired ID to its active target ID")
    jobs = config.get("jobs")
    if not isinstance(jobs, list) or len(jobs) < len(TARGET_IDS):
        _fail("canonical config must define the five stage jobs")
    stage_jobs = jobs[:len(TARGET_IDS)]
    optional_jobs = jobs[len(TARGET_IDS):]
    ids = [job.get("id") if isinstance(job, dict) else None for job in stage_jobs]
    if len(set(ids)) != len(ids):
        _fail("canonical config contains duplicate job id")
    if tuple(ids) != TARGET_IDS:
        _fail("canonical config jobs must use the five ordered migration identities")
    if not _lock_retired(migration):
        _fail("canonical config requires retired migration lock and exact permanent controls")
    if cron_contracts is None:
        contract = json.loads((ROOT / "ops-contract.json").read_text(encoding="utf-8"))
        cron_contracts = contract.get("cron_contracts")
    if not isinstance(cron_contracts, list) or len(cron_contracts) < len(TARGET_IDS):
        _fail("canonical config requires the five scheduler_binding declarations")
    if len(cron_contracts) != len(jobs):
        _fail("canonical config jobs and scheduler_binding declarations must line up one for one")
    stage_contracts = cron_contracts[:len(TARGET_IDS)]
    optional_contracts = cron_contracts[len(TARGET_IDS):]
    if len(optional_jobs) > len(OPTIONAL_JOB_IDS):
        _fail("canonical config may declare at most two optional jobs")

    for index, job in enumerate(stage_jobs):
        if not isinstance(job, dict):
            _fail("job %d must be an object" % (index + 1))
        context = "job %s" % TARGET_IDS[index]
        if job.get("name") != NAMES[index]:
            _fail("%s name must be %s" % (context, NAMES[index]))
        if job.get("stage_number") != index + 1:
            _fail("%s stage_number must be %d" % (context, index + 1))
        if job.get("owner_domain") != OWNERS[index]:
            _fail("%s owner_domain must be %s" % (context, OWNERS[index]))
        if job.get("contract_id") != CONTRACT_IDS[index]:
            _fail("%s contract_id must be %s" % (context, CONTRACT_IDS[index]))
        if job.get("contract_version") != CONTRACT_VERSIONS[index]:
            _fail("%s contract_version must be %s" % (context, CONTRACT_VERSIONS[index]))
        _validate_schedule(job.get("schedule"), index, context)
        if job.get("timezone") != "Asia/Kuala_Lumpur":
            _fail("%s timezone must be Asia/Kuala_Lumpur" % context)
        expected_dependency = [] if index == 0 else [TARGET_IDS[index - 1]]
        if job.get("dependencies") != expected_dependency:
            _fail("%s must have immediate dependency %s" % (context, expected_dependency))
        declared = stage_contracts[index]
        binding = declared.get("scheduler_binding") if isinstance(declared, dict) else None
        if (not isinstance(binding, dict) or declared.get("id") != job["contract_id"]
                or binding.get("migration_job_id") != job["id"]
                or type(binding.get("enabled")) is not bool
                or type(job.get("enabled")) is not bool
                or job["enabled"] is not binding["enabled"]):
            _fail("%s enabled must match contract scheduler_binding" % context)
        allowed_states = ("scheduled",) if job["enabled"] else ("disabled", "paused")
        if job.get("state") not in allowed_states:
            _fail("%s state must agree with enabled (%s)" % (context, ", ".join(allowed_states)))
        expected_model = _expected_model(index)
        for field, value in (
            ("model", expected_model),
            ("provider", "nous"),
            ("model_snapshot", expected_model),
            ("provider_snapshot", "nous"),
        ):
            if job.get(field) != value:
                _fail("%s %s must be %s" % (context, field, value))
        if job.get("delivery") != "local":
            _fail("%s delivery must be local" % context)
        if job.get("enabled_toolsets") != ["terminal", "file"]:
            _fail("%s enabled_toolsets must be the minimum terminal and file toolsets" % context)
        if isinstance(job.get("workdir"), str) and "HERMES" in job["workdir"]:
            _fail("%s workdir contains stale HERMES path" % context)
        if job.get("workdir") != WORKDIRS[index]:
            _fail("%s workdir must be %s" % (context, WORKDIRS[index]))
        if "inactivity_limit" in job and (type(job["inactivity_limit"]) is not int
                or not 1 <= job["inactivity_limit"] <= 3600):
            _fail("inactivity_limit must be an integer from 1 through 3600 seconds")
        if index == 0:
            if type(job.get("inactivity_limit")) is not int or job["inactivity_limit"] != 1800:
                _fail("Stage 1 inactivity_limit must be exactly 1800 seconds")
        elif "inactivity_limit" in job:
            _fail("only Stage 1 may override inactivity_limit")
        _validate_prompt(job.get("prompt"), index, context)
        if index == 4:
            lower_prompt = job["prompt"].lower()
            required = ("exact delivery id", "vercel", "github-mirror", "direct vercel cli", "approved release authorization", "vercel manifest")
            if any(term not in lower_prompt for term in required):
                _fail("%s prompt omits a Stage 5 release requirement" % context)

    for offset, job in enumerate(optional_jobs):
        _validate_optional_job(
            job, optional_contracts[offset] if offset < len(optional_contracts) else None)


def _validate_optional_job(job: Any, contract: Any = None) -> None:
    """The optional authoring job, fail-closed: disabled, non-blocking, pinned.

    Everything about it is compared against the code constants above, so a
    registry edit cannot turn the optional step into a sixth live stage, give it
    a schedule of its own, or point it at a published deterministic tree. When
    the matching scheduler_binding is present it must agree field for field.
    """
    if not isinstance(job, dict):
        _fail("optional job must be an object")
    identifier = job.get("id")
    context = "optional job %s" % (identifier,)
    if identifier not in OPTIONAL_JOB_IDS:
        _fail("%s: only the optional AI-authoring job and the optional OPS chain orchestrator may follow the five stages" % context)
    if identifier == CHAIN_JOB_ID:
        _validate_optional_chain_job(job, contract)
        return
    if job.get("name") != OPTIONAL_JOB_NAMES[OPTIONAL_JOB_IDS.index(identifier)]:
        _fail("%s name must be the canonical optional-job name" % context)
    if job.get("optional_stage") is not True:
        _fail("%s must declare optional_stage" % context)
    if job.get("stage_number") != OPTIONAL_JOB_STAGE_NUMBER:
        _fail("%s stage_number must be %d" % (context, OPTIONAL_JOB_STAGE_NUMBER))
    if job.get("owner_domain") != "2_ANALYTICS":
        _fail("%s owner_domain must be 2_ANALYTICS" % context)
    if job.get("contract_id") != OPTIONAL_JOB_CONTRACT_ID:
        _fail("%s contract_id must be %s" % (context, OPTIONAL_JOB_CONTRACT_ID))
    if job.get("contract_version") != OPTIONAL_JOB_CONTRACT_VERSION:
        _fail("%s contract_version must be %s" % (context, OPTIONAL_JOB_CONTRACT_VERSION))
    if job.get("schedule") != {"kind": "cron", "expr": OPTIONAL_JOB_SCHEDULE,
                               "display": OPTIONAL_JOB_SCHEDULE}:
        _fail("%s schedule must exactly be %s" % (context, OPTIONAL_JOB_SCHEDULE))
    if job.get("timezone") != "Asia/Kuala_Lumpur":
        _fail("%s timezone must be Asia/Kuala_Lumpur" % context)
    if job.get("dependencies") != [TARGET_IDS[OPTIONAL_JOB_STAGE_NUMBER - 1]]:
        _fail("%s must depend on the Stage %d job" % (context, OPTIONAL_JOB_STAGE_NUMBER))
    if job.get("enabled") is not False:
        _fail("%s must ship disabled" % context)
    if job.get("state") != "disabled":
        _fail("%s state must agree with enabled (disabled)" % context)
    if job.get("non_blocking") is not True:
        _fail("%s must be declared non_blocking" % context)
    if job.get("gate_impact") != "none":
        _fail("%s must declare gate_impact none" % context)
    for field in ("model", "model_snapshot"):
        if job.get(field) != OPTIONAL_JOB_MODEL:
            _fail("%s %s must be %s" % (context, field, OPTIONAL_JOB_MODEL))
    for field in ("provider", "provider_snapshot"):
        if job.get(field) != "nous":
            _fail("%s %s must be nous" % (context, field))
    if job.get("delivery") != "local":
        _fail("%s delivery must be local" % context)
    if job.get("enabled_toolsets") != ["terminal", "file"]:
        _fail("%s enabled_toolsets must be the minimum terminal and file toolsets" % context)
    if job.get("workdir") != FINAL_RUNNER_WORKDIR:
        _fail("%s workdir must be %s" % (context, FINAL_RUNNER_WORKDIR))
    if "inactivity_limit" in job and (type(job["inactivity_limit"]) is not int
                                      or not 1 <= job["inactivity_limit"] <= 3600):
        _fail("inactivity_limit must be an integer from 1 through 3600 seconds")
    if job.get("writes") != list(OPTIONAL_JOB_WRITES):
        _fail("%s writes must exactly be the AI-edition and authoring-work trees" % context)
    if job.get("must_not_write") != list(OPTIONAL_JOB_MUST_NOT_WRITE):
        _fail("%s must_not_write must name both deterministic report trees and every downstream domain" % context)
    prompt = _string(job.get("prompt"), "%s prompt" % context)
    lowered = prompt.lower()
    missing = [term for term in OPTIONAL_JOB_PROMPT_TERMS if term not in lowered]
    if missing:
        _fail("%s prompt is missing %s" % (context, ", ".join(missing)))
    if contract is None:
        return
    if not isinstance(contract, dict):
        _fail("%s scheduler_binding must be an object" % context)
    if (contract.get("id") != OPTIONAL_JOB_CONTRACT_ID
            or contract.get("version") != OPTIONAL_JOB_CONTRACT_VERSION
            or contract.get("owner_domain") != "2_ANALYTICS"):
        _fail("%s contract id, version and owner must match the job" % context)
    if contract.get("isolated_adapter") is not None:
        _fail("%s is never a sandboxed adapter; it must not declare isolated_adapter" % context)
    binding = contract.get("scheduler_binding")
    if not isinstance(binding, dict):
        _fail("%s scheduler_binding must be an object" % context)
    expected = {
        "migration_job_id": job["id"],
        "schedule": job["schedule"]["expr"],
        "timezone": job["timezone"],
        "dependencies": job["dependencies"],
        "enabled": job["enabled"],
        "delivery": job["delivery"],
        "model": job["model"],
        "provider": job["provider"],
        "model_snapshot": job["model_snapshot"],
        "provider_snapshot": job["provider_snapshot"],
        "workdir": job["workdir"],
    }
    if binding != expected:
        _fail("%s scheduler_binding must exactly mirror the optional job" % context)
    if contract.get("optional_stage") is not True:
        _fail("%s contract must declare optional_stage" % context)


def _validate_optional_chain_job(job: Any, contract: Any = None) -> None:
    """The optional OPS chain-orchestrator job, fail-closed.

    Same discipline as the authoring job with two inversions: it declares no
    stage_number (it is not a functional-boundary stage) and it is never
    non_blocking (a chain that will not start is an owner-visible stop, not a
    step silently skipped). It owns no pipeline domain and writes OPS-local
    chain evidence only.
    """
    if not isinstance(job, dict):
        _fail("optional chain job must be an object")
    identifier = job.get("id")
    context = "optional chain job %s" % (identifier,)
    expected_name = OPTIONAL_JOB_NAMES[OPTIONAL_JOB_IDS.index(CHAIN_JOB_ID)]
    if job.get("name") != expected_name:
        _fail("%s name must be %s" % (context, expected_name))
    if job.get("optional_stage") is not True:
        _fail("%s must declare optional_stage" % context)
    if "stage_number" in job:
        _fail("%s must not declare a stage_number: it is not a functional-boundary stage" % context)
    if job.get("owner_domain") != CHAIN_JOB_OWNER_DOMAIN:
        _fail("%s owner_domain must be %s" % (context, CHAIN_JOB_OWNER_DOMAIN))
    if job.get("contract_id") != CHAIN_JOB_CONTRACT_ID:
        _fail("%s contract_id must be %s" % (context, CHAIN_JOB_CONTRACT_ID))
    if job.get("contract_version") != CHAIN_JOB_CONTRACT_VERSION:
        _fail("%s contract_version must be %s" % (context, CHAIN_JOB_CONTRACT_VERSION))
    if job.get("schedule") != {"kind": "cron", "expr": CHAIN_JOB_SCHEDULE,
                               "display": CHAIN_JOB_SCHEDULE}:
        _fail("%s schedule must exactly be %s" % (context, CHAIN_JOB_SCHEDULE))
    if job.get("timezone") != "Asia/Kuala_Lumpur":
        _fail("%s timezone must be Asia/Kuala_Lumpur" % context)
    if job.get("dependencies") != []:
        _fail("%s must declare no scheduler dependency: it starts the chain itself" % context)
    if job.get("enabled") is not False:
        _fail("%s must ship disabled" % context)
    if job.get("state") != "disabled":
        _fail("%s state must agree with enabled (disabled)" % context)
    if job.get("non_blocking") is not False:
        _fail("%s must declare non_blocking false: a chain that cannot start is an owner-visible stop" % context)
    if job.get("gate_impact") != "none":
        _fail("%s must declare gate_impact none" % context)
    for field in ("model", "model_snapshot"):
        if job.get(field) != CHAIN_JOB_MODEL:
            _fail("%s %s must be %s" % (context, field, CHAIN_JOB_MODEL))
    for field in ("provider", "provider_snapshot"):
        if job.get(field) != "nous":
            _fail("%s %s must be nous" % (context, field))
    if job.get("delivery") != "local":
        _fail("%s delivery must be local" % context)
    if job.get("enabled_toolsets") != ["terminal", "file"]:
        _fail("%s enabled_toolsets must be the minimum terminal and file toolsets" % context)
    if job.get("workdir") != FINAL_RUNNER_WORKDIR:
        _fail("%s workdir must be %s" % (context, FINAL_RUNNER_WORKDIR))
    if type(job.get("inactivity_limit")) is not int or job.get("inactivity_limit") != CHAIN_JOB_INACTIVITY_LIMIT:
        _fail("%s inactivity_limit must be exactly %d seconds" % (context, CHAIN_JOB_INACTIVITY_LIMIT))
    if job.get("chain_state_file") != CHAIN_JOB_STATE_FILE:
        _fail("%s chain_state_file must be the owner-side chain state file" % context)
    if job.get("writes") != list(CHAIN_JOB_WRITES):
        _fail("%s writes must exactly be the OPS-local chain evidence tree" % context)
    if job.get("must_not_write") != list(CHAIN_JOB_MUST_NOT_WRITE):
        _fail("%s must_not_write must name every pipeline domain" % context)
    if not RUNNER_PATH.with_name("ge16_chain_runner.py").is_file():
        _fail("%s requires the owner-side orchestrator %s" % (context, CHAIN_JOB_RUNNER_PATH))
    prompt = _string(job.get("prompt"), "%s prompt" % context)
    lowered = prompt.lower()
    if not prompt.startswith(NOOP_STAGE_PREFIX):
        _fail("%s prompt must open with the chain no-op acknowledgment sentence" % context)
    missing = [term for term in CHAIN_JOB_PROMPT_TERMS if term not in lowered]
    if missing:
        _fail("%s prompt is missing %s" % (context, ", ".join(missing)))
    if hashlib.sha256(prompt.encode("utf-8")).hexdigest() != CHAIN_JOB_PROMPT_SHA256:
        _fail("%s prompt hash/content does not match the canonical chain instruction" % context)
    if contract is None:
        return
    if not isinstance(contract, dict):
        _fail("%s scheduler binding declaration must be an object" % context)
    if (contract.get("id") != CHAIN_JOB_CONTRACT_ID
            or contract.get("version") != CHAIN_JOB_CONTRACT_VERSION
            or contract.get("owner_domain") != CHAIN_JOB_OWNER_DOMAIN):
        _fail("%s contract id, version and owner must match the job" % context)
    if contract.get("isolated_adapter") is not None:
        _fail("%s is never a sandboxed adapter; it must not declare isolated_adapter" % context)
    if contract.get("optional_stage") is not True:
        _fail("%s contract must declare optional_stage" % context)
    binding = contract.get("scheduler_binding")
    if not isinstance(binding, dict):
        _fail("%s scheduler_binding must be an object" % context)
    expected_binding = {
        "migration_job_id": job["id"],
        "schedule": job["schedule"]["expr"],
        "timezone": job["timezone"],
        "dependencies": job["dependencies"],
        "enabled": job["enabled"],
        "delivery": job["delivery"],
        "model": job["model"],
        "provider": job["provider"],
        "model_snapshot": job["model_snapshot"],
        "provider_snapshot": job["provider_snapshot"],
        "workdir": job["workdir"],
    }
    if binding != expected_binding:
        _fail("%s scheduler_binding must exactly mirror the optional chain job" % context)


def _load_jobs(value: Any, label: str) -> List[Dict[str, Any]]:
    jobs = value.get("jobs") if isinstance(value, dict) else value
    if not isinstance(jobs, list):
        _fail("%s must contain a jobs list" % label)
    result = []
    for job in jobs:
        if not isinstance(job, dict):
            _fail("%s contains a non-object job" % label)
        result.append(job)
    return result


def _index_live_jobs(live: Any) -> Dict[str, Dict[str, Any]]:
    indexed: Dict[str, Dict[str, Any]] = {}
    for job in _load_jobs(live, "live jobs"):
        identifier = _string(job.get("id"), "live job id")
        if identifier in indexed:
            _fail("duplicate live job id %s" % identifier)
        indexed[identifier] = job
    return indexed


def _live_value(job: Dict[str, Any], field: str) -> Any:
    if field == "dependencies":
        value = job.get("dependencies", job.get("context_from"))
        return [] if value is None else value
    if field == "delivery":
        return job.get("delivery", job.get("deliver"))
    return job.get(field)


def _prompt_matches(expected: Dict[str, Any], actual: Dict[str, Any]) -> bool:
    prompt = actual.get("prompt")
    if isinstance(prompt, str):
        return prompt == expected["prompt"]
    actual_hash = actual.get("prompt_sha256")
    expected_hash = hashlib.sha256(expected["prompt"].encode("utf-8")).hexdigest()
    return isinstance(actual_hash, str) and actual_hash == expected_hash


def _job_differences(expected: Dict[str, Any], actual: Dict[str, Any]) -> List[str]:
    fields = (
        "name", "schedule", "timezone", "dependencies", "workdir", "enabled", "state", "inactivity_limit",
        "delivery", "enabled_toolsets", "model", "provider", "model_snapshot", "provider_snapshot",
    )
    differences = [field for field in fields if _live_value(actual, field) != expected.get(field)]
    if not _prompt_matches(expected, actual):
        differences.append("prompt")
    return differences


def compare_live_jobs(config: Any, live: Any, migration: Dict[str, Any]) -> List[str]:
    """Return deterministic drift findings; never writes a live registry."""
    validate_canonical_config(config, migration)
    indexed = _index_live_jobs(live)
    expected_jobs = config["jobs"]
    expected_by_id = {job["id"]: job for job in expected_jobs}
    findings: List[str] = []
    canonical_names = set(NAMES)
    for identifier in sorted(set(indexed) - set(TARGET_IDS)):
        if indexed[identifier].get("name") in canonical_names:
            findings.append("%s collision/manual review: canonical GE16 stage name" % identifier)
    for legacy_id, target_id in LEGACY_ID_MAPPINGS:
        actual = indexed.get(legacy_id)
        if actual is None:
            continue
        findings.append("%s legacy drift: retired ID present; active target id %s" % (legacy_id, target_id))
        for field in _job_differences(expected_by_id[target_id], actual):
            findings.append("%s legacy drift: %s" % (legacy_id, field))
        if actual.get("enabled") is True:
            findings.append("%s legacy drift: enabled=true" % legacy_id)
    for expected in expected_jobs:
        identifier = expected["id"]
        actual = indexed.get(identifier)
        if actual is None:
            findings.append("missing live target id %s" % identifier)
            continue
        differences = _job_differences(expected, actual)
        for field in differences:
            findings.append("%s drift: %s" % (identifier, field))
        workdir = actual.get("workdir")
        if isinstance(workdir, str) and "HERMES" in workdir:
            findings.append("%s drift: stale HERMES workdir" % identifier)
        prompt = actual.get("prompt")
        if isinstance(prompt, str):
            if identifier in TARGET_IDS:
                try:
                    _validate_prompt(prompt, TARGET_IDS.index(identifier), "live job %s" % identifier)
                except SyncError as error:
                    findings.append(str(error))
            else:
                # The optional job is never stage-scheduled: its whole record,
                # prompt included, is checked against the optional-job contract.
                try:
                    _validate_optional_job(expected, None)
                except SyncError as error:
                    findings.append("%s: %s" % (identifier, error))
        elif not isinstance(actual.get("prompt_sha256"), str):
            findings.append("%s drift: prompt missing both prompt and prompt_sha256" % identifier)
    return findings


def plan_operations(config: Any, live: Any, migration: Dict[str, Any]) -> List[str]:
    """Produce a deterministic, non-applying migration plan."""
    validate_canonical_config(config, migration)
    indexed = _index_live_jobs(live)
    operations: List[str] = []
    for expected in config["jobs"]:
        actual = indexed.get(expected["id"])
        if actual is None:
            operations.append("PLAN: CREATE %s disabled definition" % expected["id"])
        else:
            differences = _job_differences(expected, actual)
            if differences:
                operations.append("PLAN: UPDATE %s fields=%s" % (expected["id"], ",".join(differences)))
    if not operations:
        operations.append("PLAN: no changes")
    operations.append("NO APPLY: live cron change authorization is forbidden.")
    return operations


def _read_json(path: pathlib.Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        _fail("cannot read %s: %s" % (path, error))


def _load_migration() -> Dict[str, Any]:
    contract = _read_json(ROOT / "ops-contract.json")
    migration = contract.get("migration") if isinstance(contract, dict) else None
    if not isinstance(migration, dict):
        _fail("ops-contract migration must be an object")
    return migration


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only GE16 cron drift checker and planner")
    parser.add_argument("--config", type=pathlib.Path, default=DEFAULT_CONFIG)
    parser.add_argument("--live-jobs", type=pathlib.Path)
    parser.add_argument("--check", action="store_true", help="read-only comparison; exits non-zero on drift")
    parser.add_argument("--plan", action="store_true", help="emit proposed operations; never apply them")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.check and args.plan:
        parser.error("--check and --plan are mutually exclusive")
    try:
        config = _read_json(args.config)
        migration = _load_migration()
        validate_canonical_config(config, migration)
        if args.live_jobs is None:
            if args.plan:
                print("PLAN: live registry not supplied; no live operation can be proposed")
                print("NO APPLY: live cron change authorization is forbidden.")
            else:
                print("Canonical GE16 cron config valid; no live comparison requested.")
            return 0
        live = _read_json(args.live_jobs)
        if args.plan:
            for operation in plan_operations(config, live, migration):
                print(operation)
            return 0
        findings = compare_live_jobs(config, live, migration)
        if findings:
            for finding in findings:
                print("DRIFT: %s" % finding, file=sys.stderr)
            return 1
        print("Canonical GE16 cron config matches supplied live jobs.")
        return 0
    except SyncError as error:
        print("GE16 cron sync check failed: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
