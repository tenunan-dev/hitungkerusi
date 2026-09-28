"""Validate the GE16 OPS repository contract without external dependencies."""

from __future__ import annotations

import base64
import hashlib
import datetime as dt
import json
import pathlib
import re
import sys
from typing import Any


class ContractError(ValueError):
    """Raised when an OPS contract violates its safety boundary."""


PIPELINE = ("1_DATA", "2_ANALYTICS", "3_OUTPUTS", "4_DELIVERY", "5_WEBSITES")
KNOWN_DOMAINS = frozenset((*PIPELINE, "OPS"))
ALLOWED_CALLS = frozenset(zip(PIPELINE, PIPELINE[1:]))
IMMEDIATE_NEXT_HOP = dict(ALLOWED_CALLS)
LUNA_ALLOWED_AUTHORITIES = frozenset(
    {
        "independent-review",
        "independent-audit",
        "independent-test",
        "deterministic-validation",
        "evidence-reporting",
    }
)
FULL_GIT_SHA = re.compile(r"[0-9a-f]{40}")
SHA256_REF = re.compile(r"sha256:[0-9a-f]{64}")
SHA256_EVIDENCE_REF = re.compile(r"[^#]+#sha256:[0-9a-f]{64}")
HISTORICAL_EVIDENCE_CLASSIFICATION = "immutable-pre-lock-history-not-authorization"
HISTORICAL_EVIDENCE_STATUS = "recorded"
HISTORICAL_EVIDENCE_DISPOSITION = "historical-only"
CRON_MIGRATION_IDS = (
    "441fedd48bc8",
    "2b0a9111c836",
    "c4cebfce9fb0",
    "152172eb38fd",
    "9194211d627e",
)
CRON_LEGACY_ID_MAPPINGS = (
    ("2f817443c8e9", "441fedd48bc8"),
    ("4c3dee85457f", "2b0a9111c836"),
    ("22f17e2baabd", "c4cebfce9fb0"),
    ("a62815d7de8a", "152172eb38fd"),
    ("b539a7a77c39", "9194211d627e"),
)
CRON_CONTRACT_VERSIONS = ("1.4.0", "1.4.0", "1.4.0", "1.4.0", "1.5.0")
CRON_OWNERS = ("1_DATA", "2_ANALYTICS", "3_OUTPUTS", "4_DELIVERY", "5_WEBSITES")
FINAL_RUNNER_WORKDIR = "/Users/faisal.muthalib/Documents/HermesWorkFolder/Malaysia General Election v2"
CRON_WORKDIRS = (FINAL_RUNNER_WORKDIR,) * 5
CRON_SCHEDULES = ("0 22 * * 5", "0 1 * * 6", "0 3 * * 6", "0 5 * * 6", "30 5 * * 6")
CRON_READS = (("1_DATA",), ("1_DATA", "2_ANALYTICS"), ("2_ANALYTICS", "3_OUTPUTS"), ("3_OUTPUTS", "4_DELIVERY"), ("4_DELIVERY", "5_WEBSITES"))
CRON_WRITES = (("1_DATA",), ("2_ANALYTICS",), ("3_OUTPUTS",), ("4_DELIVERY",), ("5_WEBSITES",))
CRON_MUTABLE_WRITES = (("1_DATA",), ("2_ANALYTICS",), ("3_OUTPUTS",), ("4_DELIVERY",), ())
CRON_DOWNSTREAM = (("2_ANALYTICS",), ("3_OUTPUTS",), ("4_DELIVERY",), ("5_WEBSITES",), ())
STAGE_FIVE_RELEASE_REQUIREMENTS = {
    "delivery_id_pattern": "^H-[0-9]{8}-[0-9]{2}$",
    "vercel_git_sha_pattern": "^[0-9a-f]{40}$",
    "delivery_manifest_sha256_pattern": "^sha256:[0-9a-f]{64}$",
    "authorization_state": "exact-release-record-required",
    "required_gates": [
        "website-delivery-lineage-valid",
        "website-build-passed",
        "github-mirror-parity-passed",
        "vercel-manifest-bound",
        "vercel-post-deploy-readback-recorded",
        "release-authorization-approved",
    ],
    "direct_vercel_cli": "required",
    "vercel_post_deploy_readback": "recorded-required",
    "github_mirror_parity": "exact-required",
    "git_integration_deployment": "forbidden",
}
CRON_IMMUTABLE_INPUTS = (
    ("1_DATA/source-snapshot manifest sha256",),
    ("1_DATA/manifest/ge16-data-handoff.json#sha256",),
    ("2_ANALYTICS/manifest/ge16-analytics-handoff.json#sha256",),
    ("3_OUTPUTS/manifest/ge16-release-seal.json#sha256",),
    ("4_DELIVERY/manifest/ge16-delivery.json#sha256",),
)
CRON_OUTPUT_MANIFEST_REFS = (
    ("1_DATA/manifest/ge16-data-handoff.json#sha256",),
    ("2_ANALYTICS/manifest/ge16-analytics-handoff.json#sha256",),
    ("3_OUTPUTS/manifest/ge16-release-seal.json#sha256",),
    ("4_DELIVERY/manifest/ge16-delivery.json#sha256",),
    ("5_WEBSITES/manifest/ge16-release-gate.json#sha256",),
)
CRON_GATE_RECORDS = (
    ("data-source-lineage-valid", "data-freshness-valid", "data-validation-passed"),
    ("analytics-input-lineage-valid", "forecast-validation-passed", "reports-validation-passed", "social-output-validation-passed"),
    ("outputs-input-lineage-valid", "outputs-seal-integrity-valid", "outputs-validation-passed"),
    ("delivery-input-lineage-valid", "delivery-manifest-valid", "delivery-validation-passed"),
    tuple(STAGE_FIVE_RELEASE_REQUIREMENTS["required_gates"]),
)
CRON_ISOLATED_ADAPTERS = (
    ["data-stage-1a", "data-stage-1b"],
    "analytics-stage-2",
    "outputs-stage-3",
    "delivery-stage-4",
    "websites-stage-5",
)
MANIFEST_RECORD_SCHEMA = {
    "version": "1.0.0",
    "required_fields": ["run_id", "contract_id", "contract_version", "owner_domain", "input_manifest_refs", "output_manifest_refs", "created_at_utc"],
}
GATE_RECORD_SCHEMA = {
    "version": "1.0.0",
    "required_fields": ["run_id", "gate_id", "status", "checked_at_utc", "evidence_ref"],
}


OPS_CONTRACT_VERSION = "1.9.0"
PERMANENT_CONTROLS = {
    "domain_call_graph": "1_DATA->2_ANALYTICS->3_OUTPUTS->4_DELIVERY->5_WEBSITES",
    "scheduler_registry_write": "forbidden-from-repository",
    "deployment": "owner-authorized-release-gate-only",
}
LOCK_RETIREMENT_REASON = "structural migration complete; pipeline verified through Round 3"


def _validate_retired_migration(migration: Any) -> None:
    """Require exact retirement metadata while preserving policy and history."""
    if not isinstance(migration, dict):
        _fail("migration must be an object")
    fields = {"lock_retired", "lock_retired_at_utc", "lock_retirement_reason",
              "permanent_controls", "bounded_live_execution", "historical_activity_evidence"}
    if set(migration) != fields:
        _fail("migration retired record fields must be exact")
    if migration["lock_retired"] is not True:
        _fail("migration.lock_retired must be exactly true")
    if migration["lock_retirement_reason"] != LOCK_RETIREMENT_REASON:
        _fail("migration.lock_retirement_reason must be exact")
    if migration["permanent_controls"] != PERMANENT_CONTROLS:
        _fail("migration.permanent_controls fields and values must be exact")
    timestamp = migration["lock_retired_at_utc"]
    if not isinstance(timestamp, str) or re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z", timestamp
    ) is None:
        _fail("migration.lock_retired_at_utc must be a real UTC timestamp in ISO Z format")
    try:
        dt.datetime.fromisoformat(timestamp[:-1] + "+00:00")
    except ValueError:
        _fail("migration.lock_retired_at_utc must be a real UTC timestamp in ISO Z format")


COMMITTED_MODULE_SHA256 = {
    "655064f7994a970ca400bd8b73c5fb9c96600b72:validate_release_intake.py":
        "909a7c5fc2f547a32806d8513b03bf6a19662315d0d244a3f6e899c71bde71b6",
    "655064f7994a970ca400bd8b73c5fb9c96600b72:scripts/validate_release_intake.py":
        "909a7c5fc2f547a32806d8513b03bf6a19662315d0d244a3f6e899c71bde71b6",
}

# Review gate: changing the owner requires BOTH code and contract edits.
# B64 is DER SubjectPublicKeyInfo; SHA256 covers the exact PEM file bytes.
OWNER_PUB_KEY_B64 = "MCowBQYDK2VwAyEAIHvi8ezkQTA3cjuEGgFQ9aY9N33kAw9BJ0RST0B3zPM="
OWNER_PUB_KEY_SHA256 = "e590fcbe2b45b75095aded5bc9958216a25ca96edff554d752864bad1bc34fe6"
FIRE_KEY_DIRECTORY = "/Users/faisal.muthalib/.ge16-keys/one-shot"

# Scheduled fires bind the job configuration AND its execution payload, never
# scheduler-owned tick/run metadata. This exact tuple is duplicated in
# cron/run_stage.py; a missing field fails closed rather than being omitted.
SCHEDULER_JOB_DIGEST_REQUIRED_FIELDS = (
    "id", "name", "schedule", "model", "provider", "state", "workdir",
    "context_from", "enabled_toolsets", "prompt", "skills", "skill",
    "script", "no_agent", "base_url", "monitor_script", "monitor_url",
    "origin",
)


def validate_scheduled_job_binding(job: dict[str, Any], expected_id: str) -> str:
    """Validate scheduled state and hash the required job configuration/payload.

    The job record must be a dict carrying every field in
    ``SCHEDULER_JOB_DIGEST_REQUIRED_FIELDS`` for the exact expected id, with
    ``enabled`` exactly true and ``state`` exactly ``scheduled``. A missing
    field fails closed with the same error as an invalid state. ``None`` is a
    legitimate value for optional payload fields (``script``, ``monitor_*``)
    and is hashed as ``null`` because the key is always included.
    """
    if (not isinstance(job, dict) or not set(SCHEDULER_JOB_DIGEST_REQUIRED_FIELDS) <= job.keys()
            or job["id"] != expected_id or job.get("enabled") is not True
            or job.get("state") != "scheduled"
            or job.get("manual_run_prompt") is not None
            or job.get("manual_run_at") is not None):
        _fail("scheduled fire job binding requires complete enabled, unpaused scheduled job")
    content = {field: job[field] for field in SCHEDULER_JOB_DIGEST_REQUIRED_FIELDS}
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


BOUNDED_LIVE_POLICY = {
    "committed_module_sha256": COMMITTED_MODULE_SHA256,
    "version": "1.3.0",
    "manual_execution_enabled": True,
    "authorization": "external-owner-approval-required",
    "execution_mode": "manual-one-stage",
    "allowed_stages": [1, 2, 3, 4],
    "unavailable_stages": {},
    "stage_five": "isolated-dry-run-only",
    "recurring_scheduler_execution": {
        "mode": "owner-key-authorisation",
        "key_requirement": "embedded-owner-key-required",
        "key_binding": {
            "delivery": FIRE_KEY_DIRECTORY + "/<stage>-<phase>-<ts>.json",
            "rotation": "on owner instruction only",
            "revocation": "manual_execution_enabled=false kills all modes",
        },
    },
    "owner_public_key": {
        "path": "OPS/security/owner_ed25519.pub", "algorithm": "ed25519",
        "public_key_b64": OWNER_PUB_KEY_B64, "key_sha256": OWNER_PUB_KEY_SHA256,
    },
    "scheduler_mutation": "forbidden",
    "deployment": "forbidden",
    "gateway_start": "forbidden",
    "scheduler_registry_read_only": "/Users/faisal.muthalib/.hermes/profiles/coding/cron/jobs.json",
    "script_reference_access": "read-only-for-hash-verification-and-owner-sandbox-execution",
    "authorization_lifetime_seconds": 900,
    "exclusive_lock": "external-owner-only-process-wide-nonblocking",
    "child_environment": {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
    "child_output": "discard",
    "residual_risk": "No rollback or snapshots; failed runs can leave owner-domain changes. The advisory lock serializes this runner only. External writers, unpinned imports and unrestricted reads remain outside that guarantee.",
    "scheduler_bindings": [
        {"stage_number": stage, "id": identifier, "enabled": False,
         "state": "paused", "manual_one_shot_allowed": True,
         "scheduled_fire": {"enabled": True, "state": "scheduled", "owner_key_required": True}}
        for stage, identifier in enumerate(CRON_MIGRATION_IDS[:4], 1)
    ],
}
LIVE_SCRIPT_PATHS = (
    "1_DATA/scripts/collect/track_ge16_news.py",
    "1_DATA/scripts/collect/track_ge16_polls.py",
    "1_DATA/scripts/collect/track_ge16_candidates.py",
    "1_DATA/scripts/refresh_canonical_data.py",
    "1_DATA/scripts/validate_canonical_data.py",
    "2_ANALYTICS/02_FORECAST/engine/forecast_engine.py",
    "2_ANALYTICS/02_FORECAST/engine/report_builder.py",
    "2_ANALYTICS/02_FORECAST/engine/state_report_builder.py",
    "2_ANALYTICS/05_AUTOMATION/publish_stage_manifests.py",
    "2_ANALYTICS/automation/qa/audit_ms_en_sections.py",
    "2_ANALYTICS/automation/qa/audit_state_number_parity.py",
    "3_OUTPUTS/scripts/seal_release_intake.py",
    "4_DELIVERY/scripts/publish_delivery.py",
)


PINNED_MODULE_PATHS = ('2_ANALYTICS/automation/delivery/publish_delivery.py', '2_ANALYTICS/automation/outputs/stage_current_release.py', '2_ANALYTICS/automation/outputs/build_release_payloads.py', '3_OUTPUTS/scripts/validate_release_intake.py')

def validate_bounded_live_execution(contract: dict[str, Any]) -> dict[str, Any]:
    """An exact, versioned execution policy, never a scheduler unlock."""
    if contract.get("contract_version") != OPS_CONTRACT_VERSION:
        _fail("bounded live execution requires OPS contract 1.9.0")
    migration = contract.get("migration")
    _validate_retired_migration(migration)
    clause = migration.get("bounded_live_execution")
    if not isinstance(clause, dict) or set(clause) != set(BOUNDED_LIVE_POLICY) | {"script_sha256", "modules_pinned"}:
        _fail("bounded live execution clause fields must be exact")
    if type(clause["manual_execution_enabled"]) is not bool:
        _fail("bounded live manual_execution_enabled must be an exact boolean")
    # Compare against the current code constants directly, not just a policy
    # dictionary constructed at import time. Contract-only re-pinning fails.
    key = clause["owner_public_key"]
    if (not isinstance(key, dict) or key.get("public_key_b64") != OWNER_PUB_KEY_B64
            or key.get("key_sha256") != OWNER_PUB_KEY_SHA256):
        _fail("owner public key must literally match the code pins")
    try:
        der = base64.b64decode(OWNER_PUB_KEY_B64, validate=True)
        pem = ("-----BEGIN PUBLIC KEY-----\n" + OWNER_PUB_KEY_B64 +
               "\n-----END PUBLIC KEY-----\n").encode("ascii")
        if (len(der) != 44 or der[:12] != bytes.fromhex("302a300506032b6570032100")
                or base64.b64encode(der).decode("ascii") != OWNER_PUB_KEY_B64
                or hashlib.sha256(pem).hexdigest() != OWNER_PUB_KEY_SHA256):
            _fail("owner public key code pins are inconsistent")
    except (ValueError, UnicodeError):
        _fail("owner public key code pins are invalid")
    policy = {key: clause[key] for key in BOUNDED_LIVE_POLICY}
    policy["manual_execution_enabled"] = BOUNDED_LIVE_POLICY["manual_execution_enabled"]
    if json.dumps(policy, sort_keys=True) != json.dumps(BOUNDED_LIVE_POLICY, sort_keys=True):
        _fail("bounded live execution policy must be exact")
    if clause["modules_pinned"] != list(PINNED_MODULE_PATHS):
        _fail("bounded live modules_pinned must exactly cover runner-owned release modules")
    hashes = clause["script_sha256"]
    if not isinstance(hashes, dict) or set(hashes) != set(LIVE_SCRIPT_PATHS) | set(PINNED_MODULE_PATHS):
        _fail("bounded live script paths must exactly cover Stages 1-4 and pinned modules")
    if any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
           for value in hashes.values()):
        _fail("bounded live script SHA-256 must be 64 lowercase hex characters")
    return clause


def _fail(message: str) -> None:
    raise ContractError(message)


def _required_string(item: dict[str, Any], name: str, context: str) -> str:
    value = item.get(name)
    if not isinstance(value, str) or not value:
        _fail(f"{context}: {name} must be a non-empty string")
    return value


def _validate_immutable_evidence_ref(value: str, context: str) -> None:
    """Check only the established path-and-anchor reference syntax.

    This is deliberately not an evidence-authenticity check: it neither reads
    the referenced content nor establishes that it is immutable or approved.
    Stage 5 lifecycle verification is deferred to Phases 7 and 9.
    """
    evidence_path, separator, anchor_fragment = value.partition("#")
    if not separator or not evidence_path.strip() or not anchor_fragment.strip():
        _fail(f"{context}: evidence_ref must include a non-empty path and anchor fragment")


def _validate_sha256_evidence_ref(value: str, context: str) -> None:
    """Require a safe relative file path anchored to a SHA-256 digest.

    This mirrors the runtime's content-addressed evidence syntax without
    reading an evidence file at contract-validation time.
    """
    if SHA256_EVIDENCE_REF.fullmatch(value) is None:
        _fail(f"{context}: evidence_ref must exactly use <path>#sha256:<64 lowercase hex>")
    evidence_path = value.partition("#")[0]
    path = pathlib.PurePosixPath(evidence_path)
    if (
        not evidence_path.strip()
        or evidence_path != evidence_path.strip()
        or path.is_absolute()
        or not path.parts
        or any(part in {".", ".."} for part in path.parts)
    ):
        _fail(f"{context}: evidence_ref path must be a safe non-empty relative path")


def _domain_list(item: dict[str, Any], name: str, context: str) -> list[str]:
    value = item.get(name)
    if not isinstance(value, list) or not all(isinstance(domain, str) for domain in value):
        _fail(f"{context}: {name} must be a list of domains")
    unknown = set(value) - KNOWN_DOMAINS
    if unknown:
        _fail(f"{context}: {name} contains unknown domains: {sorted(unknown)}")
    return value


def _string_list(item: dict[str, Any], name: str, context: str) -> list[str]:
    value = item.get(name)
    if not isinstance(value, list) or not value or not all(isinstance(entry, str) and entry for entry in value):
        _fail(f"{context}: {name} must be a non-empty list of strings")
    return value


def _validate_codex_responsibilities(contract: dict[str, Any]) -> None:
    responsibilities = contract.get("codex_responsibilities")
    if not isinstance(responsibilities, dict):
        _fail("codex_responsibilities must be an object")
    if set(responsibilities) != {"terra", "luna"}:
        _fail("codex_responsibilities must define exactly terra and luna")

    for role in ("terra", "luna"):
        role_responsibilities = responsibilities[role]
        if not isinstance(role_responsibilities, dict):
            _fail(f"codex_responsibilities.{role} must be an object")
        _string_list(role_responsibilities, "authorities", f"codex_responsibilities.{role}")

    luna_authorities = responsibilities["luna"]["authorities"]
    forbidden = set(luna_authorities) - LUNA_ALLOWED_AUTHORITIES
    if forbidden:
        _fail(
            "Luna may not receive implementation or contract-authority; "
            f"invalid Luna authorities: {sorted(forbidden)}"
        )


def _validate_evidence_record(item: dict[str, Any], name: str, expected_status: str) -> None:
    record = item.get(name)
    if not isinstance(record, dict):
        _fail(f"approved_release_authorization: {name} must be an object")
    if set(record) != {"status", "evidence_ref"}:
        _fail(f"approved_release_authorization: {name} fields must exactly be status and evidence_ref")
    if record.get("status") != expected_status:
        _fail(f"approved_release_authorization: {name}.status must be {expected_status}")
    evidence_ref = _required_string(record, "evidence_ref", f"approved_release_authorization.{name}")
    _validate_sha256_evidence_ref(evidence_ref, f"approved_release_authorization.{name}")


def _validate_authorization_gate_evidence(authorization: dict[str, Any]) -> dict[str, dict[str, Any]]:
    records = authorization.get("gate_evidence")
    if not isinstance(records, list) or not records:
        _fail("approved_release_authorization.gate_evidence must be a non-empty list of gate records")
    expected_gates = STAGE_FIVE_RELEASE_REQUIREMENTS["required_gates"]
    if len(records) != len(expected_gates):
        _fail("approved_release_authorization.gate_evidence must contain each required gate exactly once")

    by_gate = {}
    for record in records:
        if not isinstance(record, dict) or set(record) != set(GATE_RECORD_SCHEMA["required_fields"]):
            _fail("approved_release_authorization.gate_evidence records must exactly use the required gate record schema")
        gate_id = _required_string(record, "gate_id", "approved_release_authorization.gate_evidence")
        if gate_id in by_gate:
            _fail(f"approved_release_authorization.gate_evidence has duplicate gate_id {gate_id}")
        by_gate[gate_id] = record
        if _required_string(record, "run_id", "approved_release_authorization.gate_evidence") != authorization["delivery_id"]:
            _fail("approved_release_authorization.gate_evidence run_id must exactly bind the authorized delivery_id")
        if record.get("status") != "passed":
            _fail(f"approved_release_authorization.gate_evidence {gate_id}.status must be passed")
        _required_string(record, "checked_at_utc", "approved_release_authorization.gate_evidence")
        evidence_ref = _required_string(record, "evidence_ref", "approved_release_authorization.gate_evidence")
        _validate_sha256_evidence_ref(evidence_ref, "approved_release_authorization.gate_evidence")
    if set(by_gate) != set(expected_gates):
        _fail("approved_release_authorization.gate_evidence must contain exactly the required Stage 5 gates")
    return by_gate


def _validate_approved_release_authorization(migration: dict[str, Any]) -> None:
    """Validate future Stage 5 metadata syntactically, without authorizing it.

    This validates declared identity fields and evidence-reference shape only.
    It does not verify the referenced content, evidence authenticity, or a
    deployment authorization.  The pre-deployment/post-deployment lifecycle is
    intentionally deferred to the Phase 7 runner and Phase 9 controlled
    release procedure.
    """
    authorization = migration.get("approved_release_authorization")
    if not isinstance(authorization, dict):
        _fail("migration.approved_release_authorization must be an object for future Stage 5 syntax validation")
    expected_fields = {
        "approval_status", "delivery_id", "target", "vercel_git_sha",
        "delivery_manifest_sha256", "authorization_state",
        "required_gates", "direct_vercel_cli",
        "vercel_post_deploy_readback", "github_mirror_parity", "git_integration_deployment",
        "gate_evidence", "vercel_readback_evidence",
    }
    if set(authorization) != expected_fields:
        _fail("approved_release_authorization fields must exactly match Stage 5 release identity requirements")
    if authorization.get("approval_status") != "approved":
        _fail("approved_release_authorization.approval_status must be approved")
    if authorization.get("target") != "5_WEBSITES":
        _fail("approved_release_authorization.target must be 5_WEBSITES")
    requirements = STAGE_FIVE_RELEASE_REQUIREMENTS
    for field, pattern in (
        ("delivery_id", requirements["delivery_id_pattern"]),
        ("vercel_git_sha", requirements["vercel_git_sha_pattern"]),
        ("delivery_manifest_sha256", requirements["delivery_manifest_sha256_pattern"]),
    ):
        value = _required_string(authorization, field, "approved_release_authorization")
        if re.fullmatch(pattern, value) is None:
            _fail(f"approved_release_authorization.{field} must match the exact Stage 5 pattern")
    for field in (
        "authorization_state", "required_gates", "direct_vercel_cli",
        "vercel_post_deploy_readback", "github_mirror_parity", "git_integration_deployment",
    ):
        if authorization.get(field) != requirements[field]:
            _fail(f"approved_release_authorization.{field} must exactly match Stage 5 requirements")
    gate_evidence = _validate_authorization_gate_evidence(authorization)
    for name, gate_id in (("vercel_readback_evidence", "vercel-post-deploy-readback-recorded"),):
        _validate_evidence_record(authorization, name, "recorded")
        if authorization[name]["evidence_ref"] == gate_evidence[gate_id]["evidence_ref"]:
            _fail(f"approved_release_authorization.{name} must be distinct from {gate_id} gate evidence")


def _validate_historical_activity_evidence(migration: dict[str, Any]) -> None:
    history = migration.get("historical_activity_evidence")
    if not isinstance(history, dict):
        _fail("migration.historical_activity_evidence must be an object")
    if set(history) != {"version", "classification", "records"}:
        _fail("migration.historical_activity_evidence must contain only version, classification, and records")
    _required_string(history, "version", "migration.historical_activity_evidence")
    if history.get("classification") != HISTORICAL_EVIDENCE_CLASSIFICATION:
        _fail("migration.historical_activity_evidence must be immutable pre-lock history, not authorization")

    records = history.get("records")
    if not isinstance(records, list) or not records:
        _fail("migration.historical_activity_evidence.records must be a non-empty list")

    record_ids = set()
    activity_types = set()
    for record in records:
        if not isinstance(record, dict):
            _fail("migration.historical_activity_evidence.records: every record must be an object")
        identifier = _required_string(record, "id", "historical activity evidence")
        if identifier in record_ids:
            _fail(f"migration.historical_activity_evidence.records: duplicate id {identifier}")
        record_ids.add(identifier)

        activity_type = _required_string(record, "activity_type", f"historical activity evidence {identifier}")
        activity_types.add(activity_type)
        required_fields = {
            "id",
            "activity_type",
            "evidence_ref",
            "evidence_status",
            "historical_outcome",
            "disposition",
        }
        if activity_type == "release":
            required_fields.add("delivery_id")
        elif activity_type == "cron":
            required_fields.add("cron_job_ids")
        else:
            _fail(f"historical activity evidence {identifier}: activity_type must be release or cron")
        if set(record) != required_fields:
            _fail(f"historical activity evidence {identifier}: fields must be immutable evidence fields only")
        evidence_ref = _required_string(record, "evidence_ref", f"historical activity evidence {identifier}")
        evidence_path, separator, anchor_fragment = evidence_ref.partition("#")
        if not separator or not evidence_path or not anchor_fragment:
            _fail(
                f"historical activity evidence {identifier}: "
                "evidence_ref must include a non-empty path and anchor fragment"
            )
        if record.get("evidence_status") != HISTORICAL_EVIDENCE_STATUS:
            _fail(f"historical activity evidence {identifier}: evidence_status must be recorded")
        _required_string(record, "historical_outcome", f"historical activity evidence {identifier}")
        if record.get("disposition") != HISTORICAL_EVIDENCE_DISPOSITION:
            _fail(f"historical activity evidence {identifier}: disposition must be historical-only")

        if activity_type == "release":
            _required_string(record, "delivery_id", f"historical activity evidence {identifier}")
        else:
            _string_list(record, "cron_job_ids", f"historical activity evidence {identifier}")

    if not {"release", "cron"}.issubset(activity_types):
        _fail("migration.historical_activity_evidence.records must include release and cron evidence")


def _validate_contract_item(item: Any, kind: str, lock_retired: bool) -> None:
    if not isinstance(item, dict):
        _fail(f"{kind}: contract item must be an object")
    identifier = _required_string(item, "id", kind)
    context = f"{kind} {identifier}"
    _required_string(item, "version", context)
    owner = _required_string(item, "owner_domain", context)
    if owner not in KNOWN_DOMAINS:
        _fail(f"{context}: owner_domain must be a known domain")
    reads = _domain_list(item, "reads_from", context)
    writes = _domain_list(item, "writes_to", context)
    mutable_writes = _domain_list(item, "mutable_writes", context)
    downstream_allowed = _domain_list(item, "downstream_allowed", context)

    if item.get("manifest_required") is not True:
        _fail(f"{context}: manifest_required must be true")
    gates = item.get("gate_records_required")
    if not isinstance(gates, list) or not gates or not all(isinstance(gate, str) and gate for gate in gates):
        _fail(f"{context}: gate_records_required must be a non-empty list")

    if kind != "cron":
        for destination in mutable_writes:
            if destination != owner:
                _fail(f"{context}: {owner} may not directly mutate {destination}")
            if destination == "5_WEBSITES":
                _fail(f"{context}: 5_WEBSITES is immutable; delivery is consumed as an immutable artifact")
    if kind != "cron" and not set(mutable_writes).issubset(writes):
        _fail(f"{context}: mutable_writes must be included in writes_to")

    expected_downstream = IMMEDIATE_NEXT_HOP.get(owner)
    if expected_downstream is None:
        if downstream_allowed:
            _fail(f"{context}: downstream_allowed for {owner} must be empty")
    elif any(destination != expected_downstream for destination in downstream_allowed):
        _fail(
            f"{context}: downstream_allowed for {owner} may contain only immediate next hop "
            f"{expected_downstream}"
        )

    for source in reads:
        for destination in writes:
            if source in PIPELINE and destination in PIPELINE and source != destination:
                if (source, destination) not in ALLOWED_CALLS:
                    _fail(f"{context}: call {source}->{destination} is outside 1_DATA->2_ANALYTICS->3_OUTPUTS->4_DELIVERY->5_WEBSITES")

    if kind == "cron":
        if not lock_retired:
            _fail(f"{context}: migration lock retirement is required")
        if "enabled" in item and (type(item["enabled"]) is not bool or
                                  item["enabled"] is not item.get("scheduler_binding", {}).get("enabled")):
            _fail(f"{context}: enabled must match scheduler_binding")


def _validate_cron_contracts(crons: list[Any], migration: dict[str, Any]) -> None:
    """Enforce the five permanent functional-boundary contracts.

    The optional AI-authoring contract may follow them and nothing else may:
    it is not a functional boundary, it is checked by
    ``_validate_optional_cron_contract`` below, and it never gains an adapter.
    """
    if not len(CRON_MIGRATION_IDS) <= len(crons) <= len(CRON_MIGRATION_IDS) + 2:
        _fail(
            "cron_contracts must define the five functional-boundary stages, "
            "optionally followed by the optional AI-authoring contract and the "
            "optional OPS chain-orchestrator contract"
        )
    expected_upstreams = (None, "1_DATA", "2_ANALYTICS", "3_OUTPUTS", "4_DELIVERY")
    expected_downstreams = ("2_ANALYTICS", "3_OUTPUTS", "4_DELIVERY", "5_WEBSITES", None)
    expected_dependencies = ([], [CRON_MIGRATION_IDS[0]], [CRON_MIGRATION_IDS[1]], [CRON_MIGRATION_IDS[2]], [CRON_MIGRATION_IDS[3]])
    if migration.get("lock_retired") is not True:
        _fail("cron contracts require migration lock retirement")

    for item in crons:
        if not isinstance(item, dict):
            _fail("cron_contracts: every stage must be an object")
    seen_ids = {_required_string(item, "id", "cron contract") for item in crons}
    if len(seen_ids) != len(crons):
        _fail("cron_contracts: duplicate id")

    for index, item in enumerate(crons[:len(CRON_MIGRATION_IDS)]):
        identifier = _required_string(item, "id", "cron contract")
        context = f"cron contract {identifier}"
        if item.get("owner_domain") != CRON_OWNERS[index]:
            _fail(f"{context}: owner_domain must be {CRON_OWNERS[index]}")
        exact_domains = (
            ("reads_from", CRON_READS[index]),
            ("writes_to", CRON_WRITES[index]),
            ("mutable_writes", CRON_MUTABLE_WRITES[index]),
            ("downstream_allowed", CRON_DOWNSTREAM[index]),
        )
        for field, expected in exact_domains:
            if item.get(field) != list(expected):
                _fail(f"{context}: {field} must exactly be {list(expected)}")
        if item.get("immediate_upstream") != expected_upstreams[index]:
            _fail(f"{context}: immediate_upstream must be {expected_upstreams[index]}")
        if item.get("immediate_downstream") != expected_downstreams[index]:
            _fail(f"{context}: immediate_downstream must be {expected_downstreams[index]}")
        for name in (
            "required_immutable_inputs",
            "freshness_lineage_requirements",
            "output_manifest_refs",
            "deterministic_validation_commands",
            "stop_conditions",
        ):
            _string_list(item, name, context)
        if item.get("version") != CRON_CONTRACT_VERSIONS[index]:
            _fail(f"{context}: version must be {CRON_CONTRACT_VERSIONS[index]}")
        expected_command = "python3 OPS/cron/run_stage.py --contract %s --version %s" % (identifier, CRON_CONTRACT_VERSIONS[index])
        if item["deterministic_validation_commands"] != [expected_command]:
            _fail(f"{context}: deterministic_validation_commands must be exactly [{expected_command!r}]")
        if item["required_immutable_inputs"] != list(CRON_IMMUTABLE_INPUTS[index]):
            _fail(f"{context}: required_immutable_inputs must exactly preserve immutable lineage references")
        if item["output_manifest_refs"] != list(CRON_OUTPUT_MANIFEST_REFS[index]):
            _fail(f"{context}: output_manifest_refs must exactly preserve immutable lineage references")
        if item.get("gate_records_required") != list(CRON_GATE_RECORDS[index]):
            _fail(f"{context}: gate_records_required must exactly be the declared stage gates")
        expected_adapter = CRON_ISOLATED_ADAPTERS[index]
        if item.get("isolated_adapter") != expected_adapter:
            _fail(f"{context}: isolated_adapter must exactly be {expected_adapter}")
        notification = _required_string(item, "notification_result", context)
        if "concise" not in notification.lower() or "stop reason" not in notification.lower():
            _fail(f"{context}: notification_result must be concise and include stop reason")

        binding = item.get("scheduler_binding")
        if not isinstance(binding, dict):
            _fail(f"{context}: scheduler_binding must be an object")
        if type(binding.get("enabled")) is not bool:
            _fail(f"{context}: scheduler_binding enabled must be an exact boolean")
        expected_model = "deepseek/deepseek-v4-pro" if index == 1 else "z-ai/glm-5.3-flash"
        expected_binding = {
            "migration_job_id": CRON_MIGRATION_IDS[index],
            "schedule": CRON_SCHEDULES[index],
            "timezone": "Asia/Kuala_Lumpur",
            "dependencies": expected_dependencies[index],
            "enabled": binding["enabled"],
            "delivery": "local",
            "model": expected_model,
            "provider": "nous",
            "model_snapshot": expected_model,
            "provider_snapshot": "nous",
            "workdir": CRON_WORKDIRS[index],
        }
        if binding != expected_binding:
            _fail(f"{context}: scheduler_binding must exactly define the locked model, snapshots, path, and dependency")
        workdir = pathlib.Path(binding["workdir"])
        if not workdir.is_absolute() or workdir != pathlib.Path(FINAL_RUNNER_WORKDIR):
            _fail(f"{context}: scheduler_binding workdir must be the exact absolute final runner workdir")
        if not workdir.parent.is_dir() or any(part.upper() == "HERMES" for part in workdir.parts):
            _fail(f"{context}: scheduler_binding workdir must have an existing parent and no HERMES component")

        if index == 4:
            release = item.get("release_authorization_requirements")
            if not isinstance(release, dict):
                _fail(f"{context}: release_authorization_requirements must be an object")
            if release != STAGE_FIVE_RELEASE_REQUIREMENTS:
                missing = sorted(set(STAGE_FIVE_RELEASE_REQUIREMENTS) - set(release))
                extra = sorted(set(release) - set(STAGE_FIVE_RELEASE_REQUIREMENTS))
                detail = "missing %s" % missing if missing else "extra %s or incorrect values" % extra
                _fail(f"{context}: release_authorization_requirements must exactly define Stage 5 authorization patterns, gates, and separate readbacks ({detail})")
        elif "release_authorization_requirements" in item:
            _fail(f"{context}: only Stage 5 may define release authorization")

    for item in crons[len(CRON_MIGRATION_IDS):]:
        if isinstance(item, dict) and item.get("id") == OPTIONAL_CHAIN_CONTRACT_ID:
            _validate_optional_chain_cron_contract(item)
        else:
            _validate_optional_cron_contract(item)


#: The one contract allowed to follow the five functional-boundary stages: the
#: optional AI-authored narrative edition. It is pinned here so the optional step
#: cannot be re-shaped into a sixth live stage from contract data alone.
OPTIONAL_CRON_CONTRACT_ID = "ge16-cron-analytics-authoring"
OPTIONAL_CRON_CONTRACT_VERSION = "1.0.0"


def _validate_optional_cron_contract(item: dict[str, Any]) -> None:
    """The optional AI-authoring contract: declared, disabled, never sandboxed."""
    identifier = _required_string(item, "id", "optional cron contract")
    context = f"optional cron contract {identifier}"
    if identifier != OPTIONAL_CRON_CONTRACT_ID or item.get("version") != OPTIONAL_CRON_CONTRACT_VERSION:
        _fail(
            "cron_contracts: the only contract allowed after the five stages is "
            f"{OPTIONAL_CRON_CONTRACT_ID} {OPTIONAL_CRON_CONTRACT_VERSION}"
        )
    if item.get("owner_domain") != "2_ANALYTICS" or item.get("optional_stage") is not True:
        _fail(f"{context}: must declare the optional 2_ANALYTICS stage")
    if "isolated_adapter" in item:
        _fail(f"{context}: the optional step is never executed by the sandboxed runner")
    if "release_authorization_requirements" in item:
        _fail(f"{context}: only Stage 5 may define release authorization")
    for field in ("reads_from", "writes_to", "mutable_writes"):
        if set(_domain_list(item, field, context)) - {"2_ANALYTICS"}:
            _fail(f"{context}: {field} may only name its own domain")
    if list(_domain_list(item, "downstream_allowed", context)) != ["3_OUTPUTS"]:
        _fail(f"{context}: downstream_allowed must exactly be the immediate next hop 3_OUTPUTS")
    if item.get("manifest_required") is not True:
        _fail(f"{context}: manifest_required must be true")
    gates = item.get("gate_records_required")
    if not isinstance(gates, list) or not gates:
        _fail(f"{context}: gate_records_required must be a non-empty list")
    for name in (
        "required_immutable_inputs",
        "freshness_lineage_requirements",
        "output_manifest_refs",
        "deterministic_validation_commands",
        "stop_conditions",
    ):
        _string_list(item, name, context)
    notification = _required_string(item, "notification_result", context)
    if "concise" not in notification.lower() or "stop reason" not in notification.lower():
        _fail(f"{context}: notification_result must be concise and include stop reason")
    binding = item.get("scheduler_binding")
    if not isinstance(binding, dict):
        _fail(f"{context}: scheduler_binding must be an object")
    if binding.get("enabled") is not False or binding.get("delivery") != "local":
        _fail(f"{context}: the optional job must ship disabled with local delivery")
    if binding.get("timezone") != "Asia/Kuala_Lumpur":
        _fail(f"{context}: scheduler_binding timezone must be Asia/Kuala_Lumpur")
    workdir = pathlib.Path(str(binding.get("workdir")))
    if not workdir.is_absolute() or workdir != pathlib.Path(FINAL_RUNNER_WORKDIR):
        _fail(f"{context}: scheduler_binding workdir must be the exact absolute final runner workdir")


#: The second and last contract allowed to follow the five functional-boundary
#: stages: the optional OPS chain orchestrator (owner-side, completion-triggered).
#: It owns no pipeline domain, never gains an adapter, and every field is pinned
#: here so a contract edit cannot turn it into a sixth live stage or into write
#: authority over a pipeline domain.
OPTIONAL_CHAIN_CONTRACT_ID = "ge16-cron-chain-orchestrator"
OPTIONAL_CHAIN_CONTRACT_VERSION = "1.0.0"
OPTIONAL_CHAIN_JOB_ID = "ge16-chain"
OPTIONAL_CHAIN_SCHEDULE = "0 22 * * 5"
OPTIONAL_CHAIN_MODEL = "deepseek/deepseek-v4-pro"
OPTIONAL_CHAIN_RUNNER_PATH = "OPS/cron/ge16_chain_runner.py"
OPTIONAL_CHAIN_STATE_FILE = "/Users/faisal.muthalib/.hermes/profiles/coding/cron/ge16-chain-state.json"
OPTIONAL_CHAIN_LEDGER = "OPS/logs/chain/ge16-chain-<run_id>.json#sha256"
OPTIONAL_CHAIN_VALIDATION_COMMAND = "python3 OPS/cron/ge16_chain_runner.py --status"
OPTIONAL_CHAIN_GATES = ("chain-run-complete-recorded",)
OPTIONAL_CHAIN_WRITES = ("OPS",)


def _validate_optional_chain_cron_contract(item: dict[str, Any]) -> None:
    """The optional OPS chain orchestrator: declared, disabled, OPS-local only."""
    identifier = _required_string(item, "id", "optional cron contract")
    context = f"optional cron contract {identifier}"
    if identifier != OPTIONAL_CHAIN_CONTRACT_ID or item.get("version") != OPTIONAL_CHAIN_CONTRACT_VERSION:
        _fail(
            "cron_contracts: the second contract allowed after the five stages is "
            f"{OPTIONAL_CHAIN_CONTRACT_ID} {OPTIONAL_CHAIN_CONTRACT_VERSION}"
        )
    if item.get("owner_domain") != "OPS" or item.get("optional_stage") is not True:
        _fail(f"{context}: must declare the optional OPS control-plane step")
    if "isolated_adapter" in item:
        _fail(f"{context}: the chain orchestrator is never executed by the sandboxed runner")
    if "release_authorization_requirements" in item:
        _fail(f"{context}: only Stage 5 may define release authorization")
    if item.get("immediate_upstream") is not None or item.get("immediate_downstream") is not None:
        _fail(f"{context}: the chain orchestrator claims no pipeline hop")
    if list(_domain_list(item, "reads_from", context)) != ["OPS"]:
        _fail(f"{context}: reads_from must exactly be OPS")
    if list(_domain_list(item, "writes_to", context)) != list(OPTIONAL_CHAIN_WRITES):
        _fail(f"{context}: writes_to must exactly be OPS")
    if list(_domain_list(item, "mutable_writes", context)) != []:
        _fail(f"{context}: mutable_writes must be empty; the chain writes declared OPS-local chain evidence only")
    if list(_domain_list(item, "downstream_allowed", context)) != []:
        _fail(f"{context}: downstream_allowed must be empty")
    if item.get("manifest_required") is not True:
        _fail(f"{context}: manifest_required must be true")
    if item.get("gate_records_required") != list(OPTIONAL_CHAIN_GATES):
        _fail(f"{context}: gate_records_required must exactly be the chain run record gate")
    for name in (
        "required_immutable_inputs",
        "freshness_lineage_requirements",
        "output_manifest_refs",
        "deterministic_validation_commands",
        "stop_conditions",
    ):
        _string_list(item, name, context)
    if item["output_manifest_refs"] != [OPTIONAL_CHAIN_LEDGER]:
        _fail(f"{context}: output_manifest_refs must exactly be the OPS chain ledger")
    if item["deterministic_validation_commands"] != [OPTIONAL_CHAIN_VALIDATION_COMMAND]:
        _fail(
            f"{context}: deterministic_validation_commands must be exactly "
            f"[{OPTIONAL_CHAIN_VALIDATION_COMMAND!r}]"
        )
    notification = _required_string(item, "notification_result", context)
    if "concise" not in notification.lower() or "stop reason" not in notification.lower():
        _fail(f"{context}: notification_result must be concise and include stop reason")
    runner = pathlib.Path(__file__).with_name("cron") / "ge16_chain_runner.py"
    if not runner.is_file():
        _fail(f"{context}: the declared owner-side orchestrator {OPTIONAL_CHAIN_RUNNER_PATH} must exist")
    binding = item.get("scheduler_binding")
    if not isinstance(binding, dict):
        _fail(f"{context}: scheduler_binding must be an object")
    expected_binding = {
        "migration_job_id": OPTIONAL_CHAIN_JOB_ID,
        "schedule": OPTIONAL_CHAIN_SCHEDULE,
        "timezone": "Asia/Kuala_Lumpur",
        "dependencies": [],
        "enabled": False,
        "delivery": "local",
        "model": OPTIONAL_CHAIN_MODEL,
        "provider": "nous",
        "model_snapshot": OPTIONAL_CHAIN_MODEL,
        "provider_snapshot": "nous",
        "workdir": FINAL_RUNNER_WORKDIR,
    }
    if binding != expected_binding:
        _fail(f"{context}: scheduler_binding must exactly mirror the disabled optional chain job")
    workdir = pathlib.Path(binding["workdir"])
    if not workdir.is_absolute() or any(part.upper() == "HERMES" for part in workdir.parts):
        _fail(f"{context}: scheduler_binding workdir must be the exact absolute final runner workdir")


def _validate_cron_source_alignment(contract: dict[str, Any]) -> None:
    """Validate the checked-in declarative source and its contract bindings."""
    try:
        from cron.sync_jobs import SyncError, validate_canonical_config
        config_path = pathlib.Path(__file__).with_name("cron") / "ge16_jobs.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        validate_canonical_config(config, contract["migration"], contract["cron_contracts"])
    except (OSError, json.JSONDecodeError, SyncError) as error:
        _fail(f"canonical cron source invalid: {error}")
    contracts = contract["cron_contracts"]
    expected_legacy_mappings = [
        {"stage_number": index + 1, "legacy_id": legacy_id, "active_target_id": target_id}
        for index, (legacy_id, target_id) in enumerate(CRON_LEGACY_ID_MAPPINGS)
    ]
    if config.get("legacy_id_mappings") != expected_legacy_mappings:
        _fail("canonical cron source must exactly document legacy-to-active scheduler ID mappings")
    for cron, job in zip(contracts, config["jobs"]):
        binding = cron["scheduler_binding"]
        values = {
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
        if binding != values or cron["id"] != job["contract_id"] or cron["version"] != job["contract_version"]:
            _fail(f"canonical cron source does not exactly match contract {cron['id']}")


def validate_contract(contract: Any) -> None:
    if not isinstance(contract, dict):
        _fail("contract must be an object")
    if contract.get("schema") != "ge16.ops-contract.v1":
        _fail("schema must be ge16.ops-contract.v1")
    if contract.get("contract_version") != OPS_CONTRACT_VERSION:
        _fail("contract_version must be 1.9.0")

    migration = contract.get("migration")
    _validate_retired_migration(migration)
    _validate_historical_activity_evidence(migration)

    validate_bounded_live_execution(contract)

    call_graph = contract.get("allowed_domain_call_graph")
    if call_graph != ["1_DATA->2_ANALYTICS", "2_ANALYTICS->3_OUTPUTS", "3_OUTPUTS->4_DELIVERY", "4_DELIVERY->5_WEBSITES"]:
        _fail("allowed_domain_call_graph must be 1_DATA->2_ANALYTICS->3_OUTPUTS->4_DELIVERY->5_WEBSITES")

    _validate_codex_responsibilities(contract)
    if contract.get("required_manifest_record") != MANIFEST_RECORD_SCHEMA:
        _fail("required_manifest_record must exactly define the required manifest record schema")
    if contract.get("required_gate_record") != GATE_RECORD_SCHEMA:
        _fail("required_gate_record must exactly define the required gate record schema")

    for kind, key in (("task", "task_contracts"), ("cron", "cron_contracts")):
        items = contract.get(key)
        if not isinstance(items, list):
            _fail(f"{key} must be a list")
        for item in items:
            _validate_contract_item(item, kind, migration["lock_retired"])
        if kind == "cron":
            _validate_cron_contracts(items, migration)


def main() -> int:
    contract_path = pathlib.Path(__file__).with_name("ops-contract.json")
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        validate_contract(contract)
        _validate_cron_source_alignment(contract)
    except (OSError, json.JSONDecodeError, ContractError) as error:
        print(f"OPS contract invalid: {error}", file=sys.stderr)
        return 1
    print("OPS contract valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
