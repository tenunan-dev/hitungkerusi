"""Executable acceptance tests for the initial GE16 OPS contract."""

from __future__ import annotations

import copy
import contextlib
import hashlib
import io
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from cron import ge16_chain_runner as chain

from validate_ops_contract import (
    ContractError,
    _validate_approved_release_authorization,
    validate_contract,
    validate_bounded_live_execution,
    _validate_cron_source_alignment,
    PERMANENT_CONTROLS,
)
from cron.run_stage import main as run_stage_main
from cron.sync_jobs import (
    NOOP_STAGE_PREFIX,
    OPTIONAL_JOB_IDS,
    SyncError,
    compare_live_jobs,
    main as sync_main,
    plan_operations,
    validate_canonical_config,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNNER_WORKDIR = "/Users/faisal.muthalib/Documents/HermesWorkFolder/Malaysia General Election v2"


def load_contract() -> dict:
    return json.loads((ROOT / "ops-contract.json").read_text(encoding="utf-8"))


def load_jobs() -> dict:
    return json.loads((ROOT / "cron" / "ge16_jobs.json").read_text(encoding="utf-8"))


def canonical_live_jobs() -> dict:
    """Return a scheduler-shaped registry exactly matching the canonical source."""
    jobs = []
    for job in load_jobs()["jobs"]:
        jobs.append(
            {
                "id": job["id"],
                "name": job["name"],
                "schedule": copy.deepcopy(job["schedule"]),
                "timezone": job["timezone"],
                "enabled": job["enabled"],
                "state": job["state"],
                "model": job["model"],
                "provider": job["provider"],
                "model_snapshot": job["model_snapshot"],
                "provider_snapshot": job["provider_snapshot"],
                "deliver": job["delivery"],
                "enabled_toolsets": copy.deepcopy(job["enabled_toolsets"]),
                **({"inactivity_limit": job["inactivity_limit"]} if "inactivity_limit" in job else {}),
                "context_from": copy.deepcopy(job["dependencies"]),
                "workdir": job["workdir"],
                "prompt": job["prompt"],
                "prompt_sha256": hashlib.sha256(job["prompt"].encode("utf-8")).hexdigest(),
            }
        )
    return {"jobs": jobs}


def unrelated_jobs() -> list[dict]:
    return [
        {"id": "unrelated-%02d" % index, "name": "Unrelated %02d" % index}
        for index in range(1, 9)
    ]


def synthetic_release_authorization(contract: dict) -> dict:
    """Return synthetic future-only evidence; it cannot authorize a release."""
    delivery_id = "H-20260905-01"
    gates = contract["cron_contracts"][4]["release_authorization_requirements"]["required_gates"]
    gate_evidence = []
    for index, gate_id in enumerate(gates, start=1):
        gate_evidence.append({
            "run_id": delivery_id,
            "gate_id": gate_id,
            "status": "passed",
            "checked_at_utc": "2026-09-05T12:00:00Z",
            "evidence_ref": "OPS/evidence/synthetic-%s.json#sha256:%064x" % (gate_id, index),
        })
    return {
        "approval_status": "approved", "delivery_id": delivery_id, "target": "5_WEBSITES",
        "vercel_git_sha": "b" * 40,
        "delivery_manifest_sha256": "sha256:" + "c" * 64,
        "authorization_state": "exact-release-record-required",
        "required_gates": copy.deepcopy(gates),
        "direct_vercel_cli": "required",
        "vercel_post_deploy_readback": "recorded-required", "github_mirror_parity": "exact-required",
        "git_integration_deployment": "forbidden",
        "gate_evidence": gate_evidence,
        "vercel_readback_evidence": {
            "status": "recorded",
            "evidence_ref": "OPS/evidence/synthetic-vercel-post-deploy-readback-recorded.json#sha256:%s" % ("f" * 64),
        },
    }


class OpsContractTests(unittest.TestCase):
    def test_bounded_live_clause_version_and_every_policy_field_are_exact(self) -> None:
        contract = load_contract()
        self.assertEqual("1.9.0", contract["contract_version"])
        clause = contract["migration"]["bounded_live_execution"]
        self.assertEqual("1.3.0", clause["version"])
        self.assertIs(True, clause["manual_execution_enabled"])
        validate_contract(contract)
        self.assertEqual([1, 2, 3, 4], clause["allowed_stages"])
        self.assertEqual({}, clause["unavailable_stages"])
        for field in clause:
            for remove in (False, True):
                with self.subTest(field=field, remove=remove):
                    mutated = copy.deepcopy(contract)
                    target = mutated["migration"]["bounded_live_execution"]
                    if remove:
                        del target[field]
                    else:
                        target[field] = "unauthorized"
                    with self.assertRaises(ContractError):
                        validate_contract(mutated)
        for version in ("1.5.0", "1.6", "2.0.0"):
            mutated = copy.deepcopy(contract)
            mutated["contract_version"] = version
            with self.assertRaises(ContractError):
                validate_contract(mutated)
        del contract["migration"]["bounded_live_execution"]["manual_execution_enabled"]
        # Rebuild the record with the field gone via a shallow patch: the retired
        # migration record's exact-field check runs BEFORE the clause check, so a
        # wholly missing clause fails on "migration retired record fields must be
        # exact" instead. Deleting only the field inside the clause reaches the
        # intended clause-level validation.
        with self.assertRaisesRegex(ContractError, "bounded live execution clause fields must be exact"):
            validate_contract(contract)

    def test_bounded_live_revocation_is_valid(self) -> None:
        contract = load_contract()
        contract["migration"]["bounded_live_execution"]["manual_execution_enabled"] = False
        validate_contract(contract)
        self.assertIs(False, contract["migration"]["bounded_live_execution"]["manual_execution_enabled"])

    def test_bounded_live_revocation_switch_rejects_non_booleans(self) -> None:
        for value in (None, 0, 1, 0.0, 1.0, "true", "false", [], {}):
            with self.subTest(value=value):
                contract = load_contract()
                contract["migration"]["bounded_live_execution"]["manual_execution_enabled"] = value
                with self.assertRaisesRegex(ContractError, "manual_execution_enabled.*boolean"):
                    validate_contract(contract)

    def test_bounded_live_scheduler_bindings_reject_missing_wrong_and_integer_states(self) -> None:
        original = load_contract()
        for index in range(4):
            for field, values in {
                "id": (None, "wrong-id"), "enabled": (None, True, 0, "false"),
                "state": (None, "disabled", "running"),
                "manual_one_shot_allowed": (None, False, 1), "stage_number": (None, True, "1"),
            }.items():
                for value in values:
                    with self.subTest(stage=index + 1, field=field, value=value):
                        contract = copy.deepcopy(original)
                        binding = contract["migration"]["bounded_live_execution"]["scheduler_bindings"][index]
                        if value is None:
                            del binding[field]
                        else:
                            binding[field] = value
                        with self.assertRaises(ContractError):
                            validate_contract(contract)

    def test_bounded_live_hash_schema_and_stage_five_exclusion(self) -> None:
        original = load_contract()
        pins = original["migration"]["bounded_live_execution"]["script_sha256"]
        self.assertEqual(17, len(pins))
        self.assertFalse(any(path.startswith("5_WEBSITES/") for path in pins))
        key = next(iter(pins))
        for value in (None, "", "a" * 63, "A" * 64, "sha256:" + "a" * 64, 0):
            contract = copy.deepcopy(original)
            contract["migration"]["bounded_live_execution"]["script_sha256"][key] = value
            with self.assertRaises(ContractError):
                validate_contract(contract)
        for path in (key, "5_WEBSITES/scripts/verify_release_gate.py", "../outside.py"):
            contract = copy.deepcopy(original)
            target = contract["migration"]["bounded_live_execution"]["script_sha256"]
            if path == key:
                del target[path]
            else:
                target[path] = "a" * 64
            with self.assertRaises(ContractError):
                validate_contract(contract)

    def test_baseline_contract_is_valid(self) -> None:
        validate_contract(load_contract())

    def test_forbidden_website_writer_fails(self) -> None:
        contract = copy.deepcopy(load_contract())
        contract["task_contracts"].append(
            {
                "id": "invalid-website-writer",
                "version": "1.0.0",
                "owner_domain": "5_WEBSITES",
                "reads_from": ["4_DELIVERY"],
                "writes_to": ["5_WEBSITES"],
                "mutable_writes": ["5_WEBSITES"],
                "downstream_allowed": [],
                "manifest_required": True,
                "gate_records_required": ["delivery-accepted"],
            }
        )

        with self.assertRaisesRegex(ContractError, "5_WEBSITES.*immutable"):
            validate_contract(contract)

    def test_ops_direct_data_mutation_fails(self) -> None:
        contract = copy.deepcopy(load_contract())
        contract["task_contracts"][0]["writes_to"] = ["1_DATA"]
        contract["task_contracts"][0]["mutable_writes"] = ["1_DATA"]

        with self.assertRaisesRegex(ContractError, "OPS.*1_DATA"):
            validate_contract(contract)

    def test_retired_record_requires_exact_fields_values_and_real_utc_timestamp(self) -> None:
        original = load_contract()
        for field in original["migration"]:
            with self.subTest(missing=field):
                contract = copy.deepcopy(original)
                del contract["migration"][field]
                with self.assertRaises(ContractError):
                    validate_contract(contract)
        contract = copy.deepcopy(original)
        del contract["migration"]
        with self.assertRaises(ContractError):
            validate_contract(contract)
        mutations = [
            ("extra", True), ("lock_retired", False), ("lock_retired", 1),
            ("lock_retired", "true"), ("lock_retirement_reason", "complete"),
        ] + [("lock_retired_at_utc", value) for value in (
            None, 1, "", "2026-02-30T12:00:00Z", "2026-09-18T25:00:00Z",
            "2026-09-18", "2026-09-18T12:00:00", "2026-09-18T12:00:00+00:00",
            "2026-09-18T12:00:00+08:00",
        )]
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                contract = copy.deepcopy(original)
                contract["migration"][field] = value
                for validator in (validate_contract, validate_bounded_live_execution):
                    with self.assertRaises(ContractError):
                        validator(contract)

    def test_historical_release_and_cron_evidence_is_pre_lock_history_not_authorization(self) -> None:
        contract = load_contract()
        migration = contract["migration"]

        self.assertIs(migration["lock_retired"], True)
        self.assertEqual(migration["permanent_controls"], PERMANENT_CONTROLS)

        history = migration["historical_activity_evidence"]
        self.assertEqual(history["classification"], "immutable-pre-lock-history-not-authorization")
        self.assertEqual(
            {record["activity_type"] for record in history["records"]},
            {"release", "cron"},
        )
        validate_contract(contract)

        contract["migration"]["approved_release_authorization"] = {
            "approval_status": "approved"
        }
        with self.assertRaisesRegex(ContractError, "migration retired record fields must be exact"):
            validate_contract(contract)

    def test_historical_evidence_ref_requires_path_and_anchor_fragment(self) -> None:
        for malformed_ref in ("#H-20260905-01", "4_DELIVERY/EXCHANGE.md#"):
            with self.subTest(malformed_ref=malformed_ref):
                contract = copy.deepcopy(load_contract())
                contract["migration"]["historical_activity_evidence"]["records"][0]["evidence_ref"] = malformed_ref

                with self.assertRaisesRegex(
                    ContractError, "evidence_ref must include a non-empty path and anchor fragment"
                ):
                    validate_contract(contract)

    def test_downstream_allowed_must_be_the_immediate_pipeline_hop(self) -> None:
        for source, skipped_destination, immediate_next_hop in (
            ("1_DATA", "3_OUTPUTS", "2_ANALYTICS"),
            ("2_ANALYTICS", "4_DELIVERY", "3_OUTPUTS"),
        ):
            with self.subTest(source=source, skipped_destination=skipped_destination):
                contract = copy.deepcopy(load_contract())
                contract["task_contracts"].append(
                    {
                        "id": f"invalid-{source.lower()}-handoff",
                        "version": "1.0.0",
                        "owner_domain": source,
                        "reads_from": [],
                        "writes_to": [source],
                        "mutable_writes": [source],
                        "downstream_allowed": [skipped_destination],
                        "manifest_required": True,
                        "gate_records_required": ["handoff-gate"],
                    }
                )

                with self.assertRaisesRegex(
                    ContractError,
                    f"downstream_allowed.*{source}.*{immediate_next_hop}",
                ):
                    validate_contract(contract)

    def test_luna_cannot_receive_implementation_or_contract_authority(self) -> None:
        contract = copy.deepcopy(load_contract())
        contract["codex_responsibilities"]["luna"] = {
            "authorities": [
                "independent-review",
                "independent-audit",
                "independent-test",
            ]
        }
        validate_contract(contract)

        for forbidden_authority in ("implementation", "contract-authority"):
            with self.subTest(forbidden_authority=forbidden_authority):
                mutated = copy.deepcopy(contract)
                mutated["codex_responsibilities"]["luna"]["authorities"].append(forbidden_authority)

                with self.assertRaisesRegex(
                    ContractError, "Luna.*implementation.*contract-authority"
                ):
                    validate_contract(mutated)

    def test_retired_record_rejects_embedded_synthetic_authorization(self) -> None:
        contract = copy.deepcopy(load_contract())
        migration = contract["migration"]
        migration["approved_release_authorization"] = synthetic_release_authorization(contract)
        _validate_approved_release_authorization(migration)

        # Structurally plausible metadata cannot grant repository deployment authority.
        with self.assertRaisesRegex(ContractError, "migration retired record fields must be exact"):
            validate_contract(contract)

    def test_permanent_controls_require_exact_fields_and_values(self) -> None:
        for field in PERMANENT_CONTROLS:
            for change in ("missing", "altered"):
                with self.subTest(field=field, change=change):
                    contract = load_contract()
                    controls = contract["migration"]["permanent_controls"]
                    if change == "missing":
                        del controls[field]
                    else:
                        controls[field] = "allowed"
                    for validator in (validate_contract, validate_bounded_live_execution):
                        with self.assertRaisesRegex(ContractError, "permanent_controls"):
                            validator(contract)
                    with self.assertRaises(SyncError):
                        validate_canonical_config(load_jobs(), contract["migration"])
        for controls in (None, [], dict(PERMANENT_CONTROLS, extra=True)):
            contract = load_contract()
            contract["migration"]["permanent_controls"] = controls
            with self.assertRaisesRegex(ContractError, "permanent_controls"):
                validate_contract(contract)

    def test_obsolete_lock_keys_are_rejected(self) -> None:
        for field in ("structural_migration_complete", "live_hermes_cron_jobs_modified",
                      "live_cron_change_authorization", "deploy_enabled", "deploy_command_policy"):
            contract = load_contract()
            contract["migration"][field] = False
            with self.assertRaisesRegex(ContractError, "retired record fields"):
                validate_contract(contract)

    def test_cron_intended_state_must_match_contract(self) -> None:
        for index in range(5):
            for state, enabled in (("disabled", False), ("paused", False), ("scheduled", True)):
                with self.subTest(stage=index + 1, state=state):
                    contract, jobs = load_contract(), load_jobs()
                    job = jobs["jobs"][index]
                    job.update(enabled=enabled, state=state)
                    contract["cron_contracts"][index]["scheduler_binding"]["enabled"] = enabled
                    validate_contract(contract)
                    validate_canonical_config(jobs, contract["migration"], contract["cron_contracts"])
                    with mock.patch("pathlib.Path.read_text", return_value=json.dumps(jobs)):
                        _validate_cron_source_alignment(contract)
                    contract["cron_contracts"][index]["scheduler_binding"]["enabled"] = not enabled
                    with self.assertRaisesRegex(SyncError, "enabled must match"):
                        validate_canonical_config(jobs, contract["migration"], contract["cron_contracts"])
                    with mock.patch("pathlib.Path.read_text", return_value=json.dumps(jobs)):
                        with self.assertRaises(ContractError):
                            _validate_cron_source_alignment(contract)
        for enabled, state in ((False, "scheduled"), (True, "paused"), (True, "disabled"),
                               (False, "unknown"), (0, "disabled"), (1, "scheduled")):
            contract, jobs = load_contract(), load_jobs()
            jobs["jobs"][0].update(enabled=enabled, state=state)
            contract["cron_contracts"][0]["scheduler_binding"]["enabled"] = enabled
            with self.assertRaises(SyncError):
                validate_canonical_config(jobs, contract["migration"], contract["cron_contracts"])
        for enabled in (0, 1, None, "false"):
            contract = load_contract()
            contract["cron_contracts"][0]["scheduler_binding"]["enabled"] = enabled
            with self.assertRaises(ContractError):
                validate_contract(contract)

    def test_cron_validation_commands_are_exact_argv_safe_locked_runner_calls(self) -> None:
        for index, suffix in enumerate((
            " --extra",
            "; touch /private/tmp/ops-review-marker",
            " && python3 OPS/evil.py",
        )):
            with self.subTest(suffix=suffix):
                contract = copy.deepcopy(load_contract())
                contract["cron_contracts"][index]["deterministic_validation_commands"] = [
                    "python3 OPS/cron/run_stage.py --contract %s --version 1.4.0%s"
                    % (contract["cron_contracts"][index]["id"], suffix)
                ]
                with self.assertRaisesRegex(ContractError, "deterministic_validation_commands"):
                    validate_contract(contract)

        for replacement in (
            "python3 OPS/cron/run_stage.py --validate-contract ge16-cron-data-collection-validation --version 1.4.0",
            "python3 OPS/other.py --contract ge16-cron-data-collection-validation --version 1.4.0",
            "python3 OPS/cron/run_stage.py --contract ge16-cron-data-collection-validation --version 1.4.0 # suffix",
        ):
            with self.subTest(replacement=replacement):
                contract = copy.deepcopy(load_contract())
                contract["cron_contracts"][0]["deterministic_validation_commands"] = [replacement]
                with self.assertRaisesRegex(ContractError, "deterministic_validation_commands"):
                    validate_contract(contract)

    def test_cron_gate_lists_lineage_and_record_schemas_are_exact(self) -> None:
        contract = copy.deepcopy(load_contract())
        contract["cron_contracts"][4]["gate_records_required"] = ["release-authorization-approved"]
        with self.assertRaisesRegex(ContractError, "gate_records_required"):
            validate_contract(contract)

        for field, value in (
            ("required_immutable_inputs", ["mutable/latest"]),
            ("output_manifest_refs", ["mutable/latest"]),
        ):
            with self.subTest(field=field):
                contract = copy.deepcopy(load_contract())
                contract["cron_contracts"][1][field] = value
                with self.assertRaisesRegex(ContractError, field):
                    validate_contract(contract)

        for record in ("required_manifest_record", "required_gate_record"):
            with self.subTest(record=record):
                contract = copy.deepcopy(load_contract())
                contract.pop(record)
                with self.assertRaisesRegex(ContractError, record):
                    validate_contract(contract)
                contract = copy.deepcopy(load_contract())
                contract[record]["required_fields"].append("unexpected")
                with self.assertRaisesRegex(ContractError, record):
                    validate_contract(contract)

    def test_future_release_authorization_is_exact_stage_five_identity(self) -> None:
        contract = copy.deepcopy(load_contract())
        migration = contract["migration"]
        migration.update({
            "approved_release_authorization": synthetic_release_authorization(contract),
        })
        _validate_approved_release_authorization(migration)
        with self.assertRaisesRegex(ContractError, "migration retired record fields must be exact"):
            validate_contract(contract)
        for field, value in (("delivery_id", "x"), ("vercel_git_sha", "b" * 39), ("target", "x")):
            with self.subTest(field=field):
                mutated = copy.deepcopy(contract)
                mutated["migration"]["approved_release_authorization"][field] = value
                with self.assertRaises(ContractError):
                    _validate_approved_release_authorization(mutated["migration"])
        for field in ("vercel_readback_evidence",):
            with self.subTest(field=field):
                mutated = copy.deepcopy(contract)
                mutated["migration"]["approved_release_authorization"].pop(field)
                with self.assertRaises(ContractError):
                    _validate_approved_release_authorization(mutated["migration"])
        contract["migration"]["approved_release_authorization"]["unexpected"] = "field"
        with self.assertRaisesRegex(ContractError, "fields"):
            _validate_approved_release_authorization(contract["migration"])

    def test_future_release_authorization_requires_bound_passed_gate_records(self) -> None:
        contract = copy.deepcopy(load_contract())
        migration = contract["migration"]
        migration.update({
            "approved_release_authorization": synthetic_release_authorization(contract),
        })
        _validate_approved_release_authorization(migration)
        with self.assertRaisesRegex(ContractError, "migration retired record fields must be exact"):
            validate_contract(contract)

        invalid_cases = (
            ("missing-evidence", lambda authorization: authorization.pop("gate_evidence")),
            ("missing-gate", lambda authorization: authorization["gate_evidence"].pop()),
            ("duplicate", lambda authorization: authorization["gate_evidence"].append(copy.deepcopy(authorization["gate_evidence"][0]))),
            ("failed", lambda authorization: authorization["gate_evidence"].__setitem__(0, dict(authorization["gate_evidence"][0], status="failed"))),
            ("wrong-release", lambda authorization: authorization["gate_evidence"].__setitem__(0, dict(authorization["gate_evidence"][0], run_id="H-20260906-01"))),
            ("empty-ref", lambda authorization: authorization["gate_evidence"].__setitem__(0, dict(authorization["gate_evidence"][0], evidence_ref=" "))),
            ("malformed-ref", lambda authorization: authorization["gate_evidence"].__setitem__(0, dict(authorization["gate_evidence"][0], evidence_ref="x"))),
            ("legacy-delivery-id-anchor", lambda authorization: authorization["gate_evidence"].__setitem__(0, dict(authorization["gate_evidence"][0], evidence_ref="OPS/evidence/gate.json#H-20260905-01"))),
            ("uppercase-sha-anchor", lambda authorization: authorization["gate_evidence"].__setitem__(0, dict(authorization["gate_evidence"][0], evidence_ref="OPS/evidence/gate.json#sha256:" + "A" * 64))),
            ("short-sha-anchor", lambda authorization: authorization["gate_evidence"].__setitem__(0, dict(authorization["gate_evidence"][0], evidence_ref="OPS/evidence/gate.json#sha256:" + "a" * 63))),
            ("unsafe-gate-path", lambda authorization: authorization["gate_evidence"].__setitem__(0, dict(authorization["gate_evidence"][0], evidence_ref="../evidence/gate.json#sha256:" + "a" * 64))),
            ("meaningless-readback", lambda authorization: authorization["vercel_readback_evidence"].__setitem__("evidence_ref", "x")),
            ("legacy-readback-anchor", lambda authorization: authorization["vercel_readback_evidence"].__setitem__("evidence_ref", "OPS/evidence/readback.json#H-20260905-01")),
            ("uppercase-readback-anchor", lambda authorization: authorization["vercel_readback_evidence"].__setitem__("evidence_ref", "OPS/evidence/readback.json#sha256:" + "A" * 64)),
            ("short-readback-anchor", lambda authorization: authorization["vercel_readback_evidence"].__setitem__("evidence_ref", "OPS/evidence/readback.json#sha256:" + "a" * 63)),
        )
        for name, mutate in invalid_cases:
            with self.subTest(name=name):
                mutated = copy.deepcopy(contract)
                mutate(mutated["migration"]["approved_release_authorization"])
                with self.assertRaises(ContractError):
                    _validate_approved_release_authorization(mutated["migration"])

    def test_future_release_authorization_rejects_readback_gate_reference_reuse(self) -> None:
        contract = copy.deepcopy(load_contract())
        migration = contract["migration"]
        migration.update({
            "approved_release_authorization": synthetic_release_authorization(contract),
        })
        authorization = migration["approved_release_authorization"]
        readback_gate = next(
            gate for gate in authorization["gate_evidence"]
            if gate["gate_id"] == "vercel-post-deploy-readback-recorded"
        )
        authorization["vercel_readback_evidence"]["evidence_ref"] = readback_gate["evidence_ref"]
        with self.assertRaisesRegex(ContractError, "must be distinct"):
            _validate_approved_release_authorization(migration)

    def test_cron_domain_semantics_are_exact_and_reject_missing_stage_two_data_read(self) -> None:
        contract = copy.deepcopy(load_contract())
        contract["cron_contracts"][1]["reads_from"] = ["2_ANALYTICS"]
        with self.assertRaisesRegex(ContractError, "reads_from must exactly"):
            validate_contract(contract)

        contract = copy.deepcopy(load_contract())
        contract["cron_contracts"][2]["mutable_writes"] = ["2_ANALYTICS"]
        with self.assertRaisesRegex(ContractError, "mutable_writes must exactly"):
            validate_contract(contract)

    def test_stage_five_patterns_and_vercel_readback_are_exact(self) -> None:
        for field, value in (
            ("delivery_id_pattern", "H-[0-9]+"),
            ("vercel_git_sha_pattern", "[0-9A-F]{40}"),
            ("delivery_manifest_sha256_pattern", "sha256:[0-9a-f]{64}"),
            ("vercel_post_deploy_readback", "optional"),
        ):
            with self.subTest(field=field):
                contract = copy.deepcopy(load_contract())
                contract["cron_contracts"][4]["release_authorization_requirements"][field] = value
                with self.assertRaises(ContractError):
                    validate_contract(contract)


class CronContractTests(unittest.TestCase):
    def test_exact_five_stage_topology_and_order(self) -> None:
        contract = load_contract()
        jobs = load_jobs()
        validate_contract(contract)
        validate_canonical_config(jobs, contract["migration"])

        # Ordered stage jobs first; the two optional companion jobs -- the
        # non-blocking AI-authoring edition (P1.5) and the disabled OPS chain
        # orchestrator (P1.6) -- trail the five stages in exactly that order.
        OPTIONAL_AUTH = "ge16-authoring"
        OPTIONAL_CHAIN = "ge16-chain"
        optional_ids = (OPTIONAL_AUTH, OPTIONAL_CHAIN)
        stage_ids = ["441fedd48bc8", "2b0a9111c836", "c4cebfce9fb0", "152172eb38fd", "9194211d627e"]
        expected_ids = stage_ids + list(optional_ids)
        stages_only = [job for job in jobs["jobs"] if job["id"] not in optional_ids]
        self.assertEqual([job["id"] for job in jobs["jobs"]], expected_ids, "ordered stage and optional job ids")
        self.assertEqual(len(jobs["jobs"]), len(expected_ids), f"exactly {len(expected_ids)} jobs")
        self.assertTrue(all(job["id"] in OPTIONAL_JOB_IDS for job in jobs["jobs"][5:]))
        auth_job = next(job for job in jobs["jobs"] if job["id"] == OPTIONAL_AUTH)
        self.assertFalse(auth_job.get("enabled"), "authoring job ships disabled")
        self.assertEqual(auth_job.get("state"), "disabled")
        self.assertTrue(auth_job.get("non_blocking"))
        chain_job = next(job for job in jobs["jobs"] if job["id"] == OPTIONAL_CHAIN)
        self.assertFalse(chain_job.get("enabled"), "chain orchestrator ships disabled")
        self.assertEqual(chain_job.get("state"), "disabled")
        self.assertFalse(chain_job.get("non_blocking"), "a chain that cannot start is a visible stop")
        self.assertNotIn("stage_number", chain_job)

        self.assertTrue(pathlib.Path(RUNNER_WORKDIR).is_dir())
        self.assertEqual([job["workdir"] for job in stages_only], [RUNNER_WORKDIR] * 5)
        self.assertEqual(
            [job["name"] for job in stages_only],
            [
                "GE16 P1.4 1_DATA Collection and Validation",
                "GE16 P1.4 2_ANALYTICS Forecast Reports and Social Outputs",
                "GE16 P1.4 3_OUTPUTS Immutable Release Intake and Sealing",
                "GE16 P1.4 4_DELIVERY Publish and Validation",
                "GE16 P1.5 5_WEBSITES Build and Release Gate",
            ],
        )
        self.assertEqual(
            [job["owner_domain"] for job in stages_only],
            ["1_DATA", "2_ANALYTICS", "3_OUTPUTS", "4_DELIVERY", "5_WEBSITES"],
        )
        self.assertEqual(
            [job["dependencies"] for job in stages_only],
            [[], ["441fedd48bc8"], ["2b0a9111c836"], ["c4cebfce9fb0"], ["152172eb38fd"]],
        )
        self.assertEqual(jobs["version"], "1.5.0")
        self.assertEqual(
            [job["contract_version"] for job in stages_only],
            ["1.4.0", "1.4.0", "1.4.0", "1.4.0", "1.5.0"],
        )
        self.assertEqual(auth_job.get("contract_version"), "1.0.0")
        self.assertEqual(chain_job.get("contract_version"), "1.0.0")
        self.assertEqual(
            jobs["legacy_id_mappings"],
            [
                {"stage_number": 1, "legacy_id": "2f817443c8e9", "active_target_id": "441fedd48bc8"},
                {"stage_number": 2, "legacy_id": "4c3dee85457f", "active_target_id": "2b0a9111c836"},
                {"stage_number": 3, "legacy_id": "22f17e2baabd", "active_target_id": "c4cebfce9fb0"},
                {"stage_number": 4, "legacy_id": "a62815d7de8a", "active_target_id": "152172eb38fd"},
                {"stage_number": 5, "legacy_id": "b539a7a77c39", "active_target_id": "9194211d627e"},
            ],
        )

    def test_stale_hermes_path_and_skipped_edge_are_rejected(self) -> None:
        jobs = load_jobs()
        migration = load_contract()["migration"]
        jobs["jobs"][0]["workdir"] = "HERMES"
        with self.assertRaisesRegex(SyncError, "HERMES"):
            validate_canonical_config(jobs, migration)

        jobs = load_jobs()
        jobs["jobs"][2]["dependencies"] = ["441fedd48bc8"]
        with self.assertRaisesRegex(SyncError, "immediate dependency"):
            validate_canonical_config(jobs, migration)

    def test_wrong_model_provider_or_snapshot_and_enabled_lock_are_rejected(self) -> None:
        migration = load_contract()["migration"]
        for field, value in (
            ("model", "wrong/model"),
            ("provider", "wrong-provider"),
            ("model_snapshot", "wrong/model"),
            ("provider_snapshot", "wrong-provider"),
        ):
            with self.subTest(field=field):
                jobs = load_jobs()
                jobs["jobs"][1][field] = value
                with self.assertRaisesRegex(SyncError, field):
                    validate_canonical_config(jobs, migration)

        jobs = load_jobs()
        jobs["jobs"][0]["enabled"] = True
        with self.assertRaisesRegex(SyncError, "enabled must match"):
            validate_canonical_config(jobs, migration)

    def test_exact_name_contract_id_and_version_are_rejected_when_changed(self) -> None:
        migration = load_contract()["migration"]
        for field, value in (
            ("name", "GE16 stage one"),
            ("contract_id", "another-contract"),
            ("contract_version", "1.4"),
        ):
            with self.subTest(field=field):
                jobs = load_jobs()
                jobs["jobs"][0][field] = value
                with self.assertRaisesRegex(SyncError, field):
                    validate_canonical_config(jobs, migration)

        jobs = load_jobs()
        jobs["version"] = "1.4"
        with self.assertRaisesRegex(SyncError, "config version"):
            validate_canonical_config(jobs, migration)

        jobs = load_jobs()
        jobs["legacy_id_mappings"][0]["active_target_id"] = "wrong-target"
        with self.assertRaisesRegex(SyncError, "legacy_id_mappings"):
            validate_canonical_config(jobs, migration)

    def test_incomplete_prompt_is_rejected(self) -> None:
        jobs = load_jobs()
        jobs["jobs"][0]["prompt"] = "Run the stage."
        with self.assertRaisesRegex(SyncError, "prompt.*incomplete"):
            validate_canonical_config(jobs, load_contract()["migration"])

    def test_prompt_hash_and_semantic_contract_command_are_rejected_when_changed(self) -> None:
        jobs = load_jobs()
        jobs["jobs"][1]["prompt"] = jobs["jobs"][1]["prompt"].replace(
            "immediate immutable 1_DATA handoff", "an upstream handoff"
        )
        with self.assertRaisesRegex(SyncError, "prompt"):
            validate_canonical_config(jobs, load_contract()["migration"])

        jobs = load_jobs()
        jobs["jobs"][0]["prompt"] = jobs["jobs"][0]["prompt"].replace(
            "run_stage.py", "missing_runner.py"
        )
        with self.assertRaisesRegex(SyncError, "runner command"):
            validate_canonical_config(jobs, load_contract()["migration"])

    def test_stage_five_requires_exact_release_authorization_fields(self) -> None:
        contract = load_contract()
        contract["cron_contracts"][4]["release_authorization_requirements"].pop("vercel_git_sha_pattern")
        with self.assertRaisesRegex(ContractError, "vercel_git_sha_pattern"):
            validate_contract(contract)

    def test_stage_five_rejects_aila_authorization_fields(self) -> None:
        contract = load_contract()
        migration = contract["migration"]
        authorization = synthetic_release_authorization(contract)
        authorization["aila_git_sha"] = "a" * 40
        migration.update({
            "approved_release_authorization": authorization,
        })
        with self.assertRaisesRegex(ContractError, "fields"):
            _validate_approved_release_authorization(migration)

    def test_historical_v2_snapshot_detects_legacy_and_field_level_drift(self) -> None:
        """A SYNTHETIC fixture exercises drift detection; it is never historical evidence.

        The fixture is machine-generated from cron/ge16_jobs.json with deliberate
        field-level drift injected. It does not record any live scheduler state and
        must never be cited as evidence of one. It lives in-repo because the previous
        /private/tmp path was cleared by the OS and silently broke this regression.
        """
        fixture = pathlib.Path(__file__).resolve().parent / "fixtures" / "ge16_v2_live_cron_snapshot.json"
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        self.assertIn("SYNTHETIC", payload["_provenance"])
        self.assertIn("NOT HISTORICAL EVIDENCE", payload["_provenance"])
        live = payload["jobs"]
        drift = compare_live_jobs(load_jobs(), live, load_contract()["migration"])
        self.assertEqual(
            [job["id"] for job in live],
            ["2f817443c8e9", "4c3dee85457f", "22f17e2baabd", "b539a7a77c39", "a62815d7de8a"],
        )
        for legacy_id, target_id in (
            ("2f817443c8e9", "441fedd48bc8"),
            ("4c3dee85457f", "2b0a9111c836"),
            ("22f17e2baabd", "c4cebfce9fb0"),
            ("a62815d7de8a", "152172eb38fd"),
            ("b539a7a77c39", "9194211d627e"),
        ):
            self.assertIn("%s legacy drift: retired ID present; active target id %s" % (legacy_id, target_id), drift)
            self.assertIn("%s legacy drift: enabled=true" % legacy_id, drift)
        for legacy_id, field in (
            ("2f817443c8e9", "name"),
            ("4c3dee85457f", "dependencies"),
            ("4c3dee85457f", "model"),
            ("2f817443c8e9", "workdir"),
            ("b539a7a77c39", "prompt"),
        ):
            self.assertIn("%s legacy drift: %s" % (legacy_id, field), drift)
        for target_id in ("441fedd48bc8", "2b0a9111c836", "c4cebfce9fb0", "152172eb38fd", "9194211d627e"):
            self.assertIn("missing live target id %s" % target_id, drift)

    def test_plan_mode_is_deterministic_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = pathlib.Path(temp_dir)
            live_path = temp / "live.json"
            live_path.write_text(json.dumps(canonical_live_jobs(), sort_keys=True), encoding="utf-8")
            before = live_path.read_bytes()

            outputs = []
            for _ in range(2):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                    code = sync_main(["--plan", "--live-jobs", str(live_path)])
                self.assertEqual(code, 0)
                outputs.append(stdout.getvalue())
                self.assertEqual(live_path.read_bytes(), before)

            self.assertEqual(outputs[0], outputs[1])
            self.assertIn("PLAN: no changes", outputs[0])
            self.assertIn("NO APPLY", outputs[0])

    def test_full_registry_preserves_unrelated_jobs_and_reports_only_name_collisions(self) -> None:
        live = canonical_live_jobs()
        live["jobs"].extend(unrelated_jobs())
        self.assertEqual(compare_live_jobs(load_jobs(), live, load_contract()["migration"]), [])
        plan = plan_operations(load_jobs(), live, load_contract()["migration"])
        self.assertEqual(plan[0], "PLAN: no changes")
        self.assertNotIn("REMOVE", "\n".join(plan))

        live["jobs"].append({
            "id": "unrelated-name-collision",
            "name": load_jobs()["jobs"][0]["name"],
        })
        findings = compare_live_jobs(load_jobs(), live, load_contract()["migration"])
        self.assertIn("collision/manual review", "\n".join(findings))
        self.assertNotIn("REMOVE", "\n".join(plan_operations(load_jobs(), live, load_contract()["migration"])))

    def test_runner_is_read_only_and_reports_posture_for_all_five_stages(self) -> None:
        contract_path = ROOT / "ops-contract.json"
        jobs_path = ROOT / "cron" / "ge16_jobs.json"
        before_contract = contract_path.read_bytes()
        before_jobs = jobs_path.read_bytes()
        for job in load_jobs()["jobs"]:
            with self.subTest(contract=job["contract_id"]):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                    code = run_stage_main(["--contract", job["contract_id"], "--version", job["contract_version"]])
                self.assertEqual(code, 0)
                result = json.loads(stdout.getvalue())
                self.assertEqual(result, {
                    "result": "posture", "mode": "posture-only", "contract_id": job["contract_id"],
                    "contract_version": job["contract_version"], "ops_contract_version": "1.9.0",
                    "lock_retired": True,
                })
        self.assertEqual(contract_path.read_bytes(), before_contract)
        self.assertEqual(jobs_path.read_bytes(), before_jobs)

    def test_runner_rejects_unknown_contract_and_never_executes(self) -> None:
        for contract, version in (("not-a-contract", "1.4.0"), ("ge16-cron-data-collection-validation", "1.4")):
            with self.subTest(contract=contract, version=version):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                    code = run_stage_main(["--contract", contract, "--version", version])
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(stdout.getvalue())["result"], "validation-error")

    def test_runner_rejects_non_exact_argument_vectors_with_json(self) -> None:
        invalid_vectors = (
            ["--validate-contract", "ge16-cron-data-collection-validation", "--version", "1.4.0"],
            ["--contract", "ge16-cron-data-collection-validation", "--version", "1.4.0", "--extra"],
            ["--contract", "ge16-cron-data-collection-validation;touch", "--version", "1.4.0"],
        )
        for arguments in invalid_vectors:
            with self.subTest(arguments=arguments):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                    code = run_stage_main(arguments)
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(stdout.getvalue())["result"], "validation-error")

    def test_every_stage_and_chain_prompt_opens_with_the_chain_noop_acknowledgment(self) -> None:
        jobs = load_jobs()
        chain_job = jobs["jobs"][6]
        self.assertEqual(chain_job["id"], "ge16-chain")
        for job in jobs["jobs"][:5] + [chain_job]:
            with self.subTest(job=job["id"]):
                self.assertTrue(job["prompt"].startswith(NOOP_STAGE_PREFIX),
                                "prompt must open with the chain no-op sentence")
                self.assertEqual(job["prompt"].count(NOOP_STAGE_PREFIX), 1)

    def test_chain_mode_registry_is_exactly_seven_jobs_and_six_still_validates(self) -> None:
        contract, jobs = load_contract(), load_jobs()
        validate_contract(contract)
        validate_canonical_config(jobs, contract["migration"], contract["cron_contracts"])
        self.assertEqual(len(jobs["jobs"]), 7)
        self.assertEqual(len(contract["cron_contracts"]), 7)

        chain_job, chain_contract = jobs["jobs"][6], contract["cron_contracts"][6]
        self.assertEqual(chain_job["id"], "ge16-chain")
        self.assertEqual(chain_contract["id"], "ge16-cron-chain-orchestrator")
        self.assertEqual(chain_contract["version"], "1.0.0")
        self.assertEqual(chain_contract["owner_domain"], "OPS")
        self.assertIs(chain_contract["optional_stage"], True)
        self.assertEqual(chain_contract["reads_from"], ["OPS"])
        self.assertEqual(chain_contract["writes_to"], ["OPS"])
        self.assertEqual(chain_contract["mutable_writes"], [])
        self.assertEqual(chain_contract["downstream_allowed"], [])
        self.assertNotIn("isolated_adapter", chain_contract)
        self.assertNotIn("release_authorization_requirements", chain_contract)
        self.assertEqual(chain_contract["gate_records_required"], ["chain-run-complete-recorded"])
        self.assertEqual(chain_contract["scheduler_binding"]["migration_job_id"], "ge16-chain")
        self.assertEqual(chain_job["schedule"]["expr"], "0 22 * * 5")
        self.assertEqual(chain_job["chain_state_file"], str(chain.STATE_PATH))
        self.assertEqual(chain_job["writes"], ["OPS/logs/chain"])
        self.assertEqual(chain_job["must_not_write"], ["1_DATA", "2_ANALYTICS", "3_OUTPUTS",
                                                       "4_DELIVERY", "5_WEBSITES"])
        self.assertEqual(chain_job["prompt"].count(NOOP_STAGE_PREFIX), 1)

        # The chain contract is optional: dropping it and its job leaves a
        # registry that is still exactly the five stages plus authoring.
        six_jobs = copy.deepcopy(jobs)
        six_jobs["jobs"] = six_jobs["jobs"][:6]
        six_contract = copy.deepcopy(contract)
        six_contract["cron_contracts"] = six_contract["cron_contracts"][:6]
        validate_contract(six_contract)
        validate_canonical_config(six_jobs, six_contract["migration"], six_contract["cron_contracts"])
        with mock.patch("pathlib.Path.read_text", return_value=json.dumps(six_jobs)):
            _validate_cron_source_alignment(six_contract)

        # The two optional jobs are ordered: the chain orchestrator may never
        # take the authoring slot, and no third optional job may exist.
        swapped = copy.deepcopy(jobs)
        swapped["jobs"][5], swapped["jobs"][6] = swapped["jobs"][6], swapped["jobs"][5]
        with self.assertRaisesRegex(SyncError, "contract id, version and owner must match the job"):
            validate_canonical_config(swapped, contract["migration"], contract["cron_contracts"])
        swapped_contract = copy.deepcopy(contract)
        swapped_contract["cron_contracts"][5], swapped_contract["cron_contracts"][6] = (
            swapped_contract["cron_contracts"][6], swapped_contract["cron_contracts"][5])
        validate_contract(swapped_contract)  # each contract is self-consistent ...
        with mock.patch("pathlib.Path.read_text", return_value=json.dumps(jobs)):
            with self.assertRaises(ContractError):
                _validate_cron_source_alignment(swapped_contract)  # ... but the order must align
        ninth = copy.deepcopy(contract)
        ninth["cron_contracts"].append(copy.deepcopy(ninth["cron_contracts"][6]))
        with self.assertRaisesRegex(ContractError, "cron_contracts"):
            validate_contract(ninth)

    def test_chain_contract_is_pinned_fail_closed(self) -> None:
        mutations = (
            ("id", "ge16-cron-chain"),
            ("version", "1.0"),
            ("owner_domain", "2_ANALYTICS"),
            ("optional_stage", False),
            ("reads_from", ["2_ANALYTICS"]),
            ("writes_to", ["OPS", "2_ANALYTICS"]),
            ("mutable_writes", ["2_ANALYTICS"]),
            ("downstream_allowed", ["3_OUTPUTS"]),
            ("immediate_upstream", "1_DATA"),
            ("immediate_downstream", "3_OUTPUTS"),
            ("isolated_adapter", "chain-stage"),
            ("manifest_required", False),
            ("gate_records_required", ["authoring-verification-recorded"]),
            ("output_manifest_refs", ["OPS/logs/chain/anything.json#sha256"]),
            ("deterministic_validation_commands",
             ["python3 OPS/cron/run_stage.py --contract ge16-cron-data-collection-validation --version 1.4.0"]),
            ("notification_result", "chain run summary"),
            ("stop_conditions", []),
            ("required_immutable_inputs", []),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                contract = copy.deepcopy(load_contract())
                contract["cron_contracts"][6][field] = value
                with self.assertRaises(ContractError):
                    validate_contract(contract)

        contract = copy.deepcopy(load_contract())
        contract["cron_contracts"][6]["release_authorization_requirements"] = {"authorization_state": "exact"}
        with self.assertRaises(ContractError):
            validate_contract(contract)

        for field, value in (
            ("enabled", True),
            ("dependencies", ["441fedd48bc8"]),
            ("schedule", "0 22 * * 6"),
            ("model", "z-ai/glm-5.3-flash"),
            ("workdir", "HERMES"),
            ("timezone", "UTC"),
        ):
            with self.subTest(binding=field):
                contract = copy.deepcopy(load_contract())
                contract["cron_contracts"][6]["scheduler_binding"][field] = value
                with self.assertRaises(ContractError):
                    validate_contract(contract)

    def test_chain_job_is_pinned_fail_closed(self) -> None:
        migration = load_contract()["migration"]
        mutations = (
            ("name", "GE16 chain"),
            ("stage_number", 2),
            ("optional_stage", False),
            ("owner_domain", "2_ANALYTICS"),
            ("contract_id", "ge16-cron-analytics-authoring"),
            ("contract_version", "1.0"),
            ("schedule", {"kind": "cron", "expr": "0 22 * * 6", "display": "0 22 * * 6"}),
            ("timezone", "UTC"),
            ("dependencies", ["441fedd48bc8"]),
            ("enabled", True),
            ("state", "scheduled"),
            ("non_blocking", True),
            ("gate_impact", "blocking"),
            ("model", "z-ai/glm-5.3-flash"),
            ("provider", "deepseek"),
            ("provider_snapshot", "deepseek"),
            ("model_snapshot", "z-ai/glm-5.3-flash"),
            ("delivery", "email"),
            ("enabled_toolsets", ["terminal"]),
            ("workdir", "HERMES"),
            ("inactivity_limit", 900),
            ("inactivity_limit", "1800"),
            ("chain_state_file", "/Users/faisal.muthalib/.hermes/profiles/coding/cron/other.json"),
            ("writes", ["OPS"]),
            ("writes", ["2_ANALYTICS/03_REPORTS/ai"]),
            ("must_not_write", ["1_DATA", "2_ANALYTICS", "3_OUTPUTS", "4_DELIVERY"]),
        )
        for field, value in mutations:
            with self.subTest(field=field, value=repr(value)):
                jobs = load_jobs()
                jobs["jobs"][6][field] = value
                with self.assertRaises(SyncError):
                    validate_canonical_config(jobs, migration)

        for prompt in (
            NOOP_STAGE_PREFIX + " Run the chain.",
            load_jobs()["jobs"][6]["prompt"] + " ",
            load_jobs()["jobs"][6]["prompt"].replace("ge16_chain_runner.py", "the runner", 1),
            load_jobs()["jobs"][6]["prompt"].replace("never mint", "may mint", 1),
            load_jobs()["jobs"][6]["prompt"].replace("--start", "--go", 1),
        ):
            with self.subTest(prompt=prompt[:60]):
                jobs = load_jobs()
                jobs["jobs"][6]["prompt"] = prompt
                with self.assertRaises(SyncError):
                    validate_canonical_config(jobs, migration)

        # The chain job may never be dropped from a seven-contract registry, and
        # the five stage jobs may never gain the no-op sentence in the wrong place.
        jobs = load_jobs()
        jobs["jobs"][6]["prompt"] = "If the chain orchestrator has already produced this stage's handoff " \
                                   "manifest for the current run_id, verify and record it as complete " \
                                   "(no-op) and exit; else execute your stage normally." + jobs["jobs"][6]["prompt"]
        with self.assertRaises(SyncError):
            validate_canonical_config(jobs, migration)

    def test_check_detects_duplicate_id_without_mutating_live_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            live_path = pathlib.Path(temp_dir) / "live.json"
            live = canonical_live_jobs()
            live["jobs"].append(copy.deepcopy(live["jobs"][0]))
            live_path.write_text(json.dumps(live), encoding="utf-8")
            before = live_path.read_bytes()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                code = sync_main(["--check", "--live-jobs", str(live_path)])
            self.assertEqual(code, 1)
            self.assertIn("duplicate live job id", stderr.getvalue())
            self.assertEqual(live_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
