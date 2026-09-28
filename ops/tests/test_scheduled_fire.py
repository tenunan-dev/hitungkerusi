"""Scheduled fire acceptance, exclusively in isolated fixture repositories."""
import ast
import base64
import hashlib
import json
import os
import pathlib
import stat
import tempfile
import unittest
from unittest import mock

from cron import mint_fire_key, run_stage
import validate_ops_contract as validator
from validate_ops_contract import ContractError, validate_contract
from cron import ed25519_support as ed25519
import test_isolated_stage_adapters as fixtures


# Frozen copy of the real Hermes ``create_job`` record for Stage 1
# (id 441fedd48bc8) as observed in the live registry. There is NO top-level
# ``paused`` key; paused state lives in ``paused_at``/``paused_reason`` and the
# ``state`` field. The prompt body is redacted to a short string; every other
# field and value, including the non-ASCII name, is exact.
REAL_HERMES_STAGE_ONE_JOB = {
    "base_url": None, "context_from": None,
    "created_at": "2026-09-14T19:17:43.418324+08:00", "deliver": "local",
    "enabled": True, "enabled_toolsets": ["terminal", "file"],
    "failure_streak": 0, "fire_claim": None, "id": "441fedd48bc8",
    "inactivity_limit": 1800, "last_delivery_error": None,
    "last_delivery_unverified": None, "last_error": None,
    "last_run_at": "2026-09-22T20:29:43.972654+08:00", "last_status": "ok",
    "model": "z-ai/glm-5.3-flash", "model_snapshot": "z-ai/glm-5.3-flash",
    "monitor_script": None, "monitor_state": None, "monitor_url": None,
    "name": "GE16 P1.4 DATA Collection and Validation \u2014 Refactored",
    "next_run_at": "2026-09-25T22:00:00+08:00", "no_agent": False, "origin": None,
    "paused_at": None, "paused_reason": None, "prompt": "[redacted Stage 1 prompt]",
    "provider": "nous", "provider_snapshot": "nous",
    "repeat": {"times": None, "completed": 2},
    "schedule": {"kind": "cron", "expr": "0 22 * * 5", "display": "0 22 * * 5"},
    "schedule_display": "0 22 * * 5", "script": None, "skill": None, "skills": [],
    "state": "scheduled",
    "workdir": "/Users/faisal.muthalib/Documents/HermesWorkFolder/Malaysia General Election v2",
}


class ScheduledFireTests(fixtures.FixtureSandboxTests):
    pin_scripts = fixtures.BoundedLiveStageTests.pin_scripts
    authorization = fixtures.BoundedLiveStageTests.authorization
    save_auth = fixtures.BoundedLiveStageTests.save_auth

    def setUp(self):
        super().setUp()
        temporary = fixtures.fixture_root()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        external = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(external.cleanup)
        self.auth_path = pathlib.Path(external.name) / "authorization.json"
        (self.root / "OPS").mkdir()
        self.contract_path = self.root / "OPS/ops-contract.json"
        self.contract_path.write_bytes(run_stage.CONTRACT_PATH.read_bytes())
        self.registry_path = self.auth_path.with_name("scheduler.json")
        self.registry_path.write_text(json.dumps({"jobs": [
            {"id": binding["id"], "enabled": False, "state": "paused"}
            for binding in run_stage.BOUNDED_LIVE_POLICY["scheduler_bindings"]
        ]}))
        self.registry_path.chmod(0o600)
        for name, value in {
            "REPOSITORY_ROOT": self.root, "ROOT": self.root / "OPS",
            "CONTRACT_PATH": self.contract_path,
            "SCHEDULER_REGISTRY_PATH": self.registry_path,
            "LIVE_LOCK_PATH": self.auth_path.with_name("runner.lock"),
        }.items():
            patch = mock.patch.object(run_stage, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.private_der = ed25519.generate_private_der()
        der = ed25519.public_from_private_der(self.private_der)
        pem = ("-----BEGIN PUBLIC KEY-----\n" + base64.b64encode(der).decode("ascii") +
               "\n-----END PUBLIC KEY-----\n").encode("ascii")
        self.public_path = self.root / "OPS/security/owner_ed25519.pub"
        self.public_path.parent.mkdir(mode=0o700)
        self.public_path.write_bytes(pem)
        self.public_path.chmod(0o600)
        key = {"path": "OPS/security/owner_ed25519.pub", "algorithm": "ed25519",
               "public_key_b64": base64.b64encode(der).decode("ascii"), "key_sha256": hashlib.sha256(pem).hexdigest()}
        for name, value in {"OWNER_PUB_KEY_B64": key["public_key_b64"],
                            "OWNER_PUB_KEY_SHA256": key["key_sha256"]}.items():
            patch = mock.patch.object(validator, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.dict(validator.BOUNDED_LIVE_POLICY, owner_public_key=key)
        patch.start()
        self.addCleanup(patch.stop)
        contract = json.loads(self.contract_path.read_text())
        contract["migration"]["bounded_live_execution"]["owner_public_key"] = key
        self.contract_path.write_text(json.dumps(contract))
        delivery = self.auth_path.parent / "one-shot"
        delivery.mkdir(mode=0o700)
        patch = mock.patch.object(run_stage, "FIRE_KEY_DIRECTORY", delivery)
        patch.start()
        self.addCleanup(patch.stop)
        self.terminal_patch = mock.patch.object(mint_fire_key, "_require_owner_terminal")
        self.terminal_patch.start()
        self.addCleanup(self.terminal_patch.stop)
        self.keychain_patch = mock.patch.object(mint_fire_key, "_read_keychain_private", return_value=self.private_der)
        self.keychain_patch.start()
        self.addCleanup(self.keychain_patch.stop)
        self.pin_scripts()
        self.auth = self.authorization(1)
        self.save_auth()
        self.set_registry(True, "scheduled")
        self.fire = delivery / "1-b-fixture.json"
        mint_fire_key.mint(1, "b", 10, self.fire)

    def job_record(self, binding, enabled, state):
        """The real Hermes ``create_job`` record shape: no top-level ``paused``."""
        return {
            "id": binding["id"], "enabled": enabled, "state": state,
            "name": "Fixture stage %d" % binding["stage_number"],
            "schedule": {"kind": "cron", "expr": "0 9 * * *",
                         "display": "0 9 * * *"},
            "model": "fixture-model", "provider": "fixture-provider",
            "workdir": str(self.root / run_stage.DOMAIN_NAMES[binding["stage_number"] - 1]),
            "context_from": None, "enabled_toolsets": ["terminal"],
            "prompt": "Fixture stage %d collection prompt" % binding["stage_number"],
            "skills": [], "skill": None, "script": None, "no_agent": False,
            "base_url": None, "monitor_script": None, "monitor_url": None,
            "origin": None,
        }

    def set_registry(self, enabled, state):
        self.registry_path.write_text(json.dumps({"jobs": [
            self.job_record(binding, enabled, state)
            for binding in run_stage.BOUNDED_LIVE_POLICY["scheduler_bindings"]
        ]}))

    def mutate_job(self, mutate):
        registry = json.loads(self.registry_path.read_text())
        mutate(registry["jobs"][0])
        self.registry_path.write_text(json.dumps(registry))

    def validate_fire(self):
        contract = run_stage._load_validated_request(*run_stage.HANDOFFS[1][2:])
        return run_stage._validate_scheduled_fire(self.fire, contract, 1, "b")

    def args(self, phase="b"):
        return ["--contract", run_stage.HANDOFFS[1][2], "--version", run_stage.HANDOFFS[1][3],
                "--scheduled-fire", "--fire-key", str(self.fire), "--stage", "1", "--phase", phase]

    def sign(self, raw):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "T"
            ed25519._write_private_file(path, self.private_der)
            return ed25519.sign_private_file(path, raw)

    def resign(self, mutate):
        envelope = json.loads(self.fire.read_text())
        mutate(envelope["payload"])
        raw = run_stage._canonical_json_bytes(envelope["payload"])
        envelope["payload_b64"] = base64.b64encode(raw).decode("ascii")
        envelope["signature"] = base64.b64encode(self.sign(raw)).decode("ascii")
        self.fire.write_text(json.dumps(envelope))

    def rejected(self):
        with mock.patch.object(run_stage, "_run_allowlisted_command") as child:
            status, record = fixtures.invoke(*self.args())
        self.assertEqual(1, status, record)
        child.assert_not_called()
        return record

    def test_mint_to_scheduled_cli_happy_path_and_replay(self):
        status, record = fixtures.invoke(*self.args())
        self.assertEqual((0, "completed", "scheduled-fire"), (status, record["result"], record["mode"]))
        self.assertTrue(run_stage._handoff_path(self.root, 1).is_file())
        claims = list((self.root / "1_DATA" / run_stage.LIVE_CLAIMS_DIRECTORY).iterdir())
        self.assertEqual(1, len(claims))
        self.assertEqual(hashlib.sha256(base64.b64decode(json.loads(self.fire.read_text())["payload_b64"])).hexdigest(), claims[0].name)
        envelope = json.loads(self.fire.read_text())
        self.fire = self.fire.with_name("copy.json")
        self.fire.write_text(json.dumps(envelope, separators=(",", ":")))
        self.fire.chmod(0o600)
        self.rejected()
        with self.assertRaisesRegex(ValueError, "already consumed"):
            run_stage._claim_live_authorization(self.root, "1_DATA", claims[0].name)

    def test_expired_future_and_excessive_lifetime(self):
        original = self.fire.read_bytes()
        now = run_stage.dt.datetime.now(run_stage.dt.timezone.utc).replace(microsecond=0)
        stamp = lambda value: value.isoformat().replace("+00:00", "Z")
        for start, end in [(-700, -1), (60, 600), (-1, 901)]:
            with self.subTest(start=start, end=end):
                self.fire.write_bytes(original)
                self.resign(lambda p: p.update(approved_at_utc=stamp(now + run_stage.dt.timedelta(seconds=start)),
                                              expires_at_utc=stamp(now + run_stage.dt.timedelta(seconds=end))))
                self.rejected()
        self.assertFalse((self.root / "1_DATA" / run_stage.LIVE_CLAIMS_DIRECTORY).exists())

    def test_exact_fields_types_hashes_and_stage(self):
        original = self.fire.read_bytes()
        for key, value in [("stage_number", 2), ("stage_number", True), ("stage_phase", "a"),
                           ("scheduler_job_id", "wrong"), ("authorized_by", "agent"),
                           ("one_shot", False), ("allow_deploy", True), ("allow_gateway_start", True),
                           ("allow_schedule_enablement", True), ("contract_sha256", "a" * 64),
                           ("canonical_jobs_sha256", "b" * 64), ("scheduler_job_binding_sha256", "c" * 64),
                           ("scheduler_registry_sha256", "c" * 64),
                           ("scheduler_binding", {"id": "wrong", "enabled": True, "state": "scheduled"}),
                           ("scheduler_binding", {"id": run_stage.BOUNDED_LIVE_POLICY["scheduler_bindings"][0]["id"],
                                                  "enabled": 1, "state": "scheduled"}),
                           ("owner_key_sha256", "d" * 64), ("key_id", "bad"), ("extra", True),
                           ("approved_at_utc", "2026-09-22T00:00:00.000Z")]:
            with self.subTest(key=key):
                self.fire.write_bytes(original)
                self.resign(lambda p: p.update({key: value}))
                self.rejected()
        for key in json.loads(original)["payload"]:
            self.fire.write_bytes(original)
            self.resign(lambda p: p.pop(key))
            self.rejected()

    def test_unsigned_tampering_digest_and_envelope(self):
        original = self.fire.read_bytes()
        for mutate in [lambda e: e.update(signature="0" * 64),
                       lambda e: e.update(signature="é" * 44),
                       lambda e: e.update(payload_b64="é" * 64),
                       lambda e: e.update(payload_b64="0" * 64),
                       lambda e: e.update(extra=True),
                       lambda e: e["payload"].update(expires_at_utc="2099-01-01T00:00:00Z")]:
            envelope = json.loads(original)
            mutate(envelope)
            self.fire.write_text(json.dumps(envelope))
            self.rejected()

    def test_paused_job_rejected_and_manual_paused_still_accepted(self):
        self.set_registry(False, "paused")
        self.rejected()
        self.auth = self.authorization(1)
        self.save_auth()
        status, record = fixtures.invoke(*fixtures.BoundedLiveStageTests.args(self))
        self.assertEqual((0, "bounded-live"), (status, record["mode"]))

    def test_job_digest_exact_whitelist_and_canonical_normalization(self):
        fields = {"id", "name", "schedule", "model", "provider", "state",
                  "workdir", "context_from", "enabled_toolsets", "prompt",
                  "skills", "skill", "script", "no_agent", "base_url",
                  "monitor_script", "monitor_url", "origin"}
        self.assertEqual(18, len(fields))
        self.assertEqual(fields, set(validator.SCHEDULER_JOB_DIGEST_REQUIRED_FIELDS))
        self.assertEqual(fields, set(run_stage.SCHEDULER_JOB_DIGEST_REQUIRED_FIELDS))
        job = json.loads(self.registry_path.read_text())["jobs"][0]
        canonical = json.dumps({k: job[k] for k in fields}, sort_keys=True,
                               separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        payload = json.loads(self.fire.read_text())["payload"]
        self.assertNotIn("scheduler_registry_sha256", payload)
        self.assertEqual({"id": job["id"], "enabled": True, "state": "scheduled"}, payload["scheduler_binding"])
        self.assertEqual(hashlib.sha256(canonical).hexdigest(), payload["scheduler_job_binding_sha256"])
        identity = self.validate_fire()
        registry = json.loads(self.registry_path.read_text())
        registry["jobs"][0] = dict(reversed(list(job.items())))
        registry["jobs"][0]["unknown_extra"] = {"ignored": True}
        registry["jobs"][1]["model"] = "unrelated-job-change"
        registry["tick"] = 123
        registry["jobs"].reverse()
        self.registry_path.write_text(json.dumps(registry, indent=4))
        self.assertEqual(identity, self.validate_fire())

    def test_real_hermes_record_mints_consumes_and_binds_execution_payload(self):
        """End-to-end on the exact live Stage-1 record shape (no ``paused`` key)."""
        original = self.registry_path.read_bytes()
        try:
            record = json.loads(json.dumps(REAL_HERMES_STAGE_ONE_JOB))
            self.registry_path.write_text(json.dumps({"jobs": [record]}))
            # A rewritten signed execution payload invalidates a minted key.
            self.fire = run_stage.FIRE_KEY_DIRECTORY / "real-prompt.json"
            mint_fire_key.mint(1, "b", 10, self.fire)
            self.mutate_job(lambda job: job.update(prompt="attacker prompt"))
            with mock.patch.object(run_stage, "_run_isolated") as child:
                status, record_out = fixtures.invoke(*self.args())
            self.assertEqual(1, status, record_out)
            child.assert_not_called()
            self.assertFalse((self.root / "1_DATA" / run_stage.LIVE_CLAIMS_DIRECTORY).exists())
            # The exact live-shaped record mints and consumes; a mid-flight
            # scheduler ``fire_claim`` rewrite leaves the identity intact.
            self.registry_path.write_text(json.dumps({"jobs": [record]}))
            self.fire = run_stage.FIRE_KEY_DIRECTORY / "real-race.json"
            mint_fire_key.mint(1, "b", 10, self.fire)
            identity = self.validate_fire()

            def adapter(contract, root, stage, **kwargs):
                self.mutate_job(lambda job: job.update(fire_claim={"token": "tick-real"}))
                kwargs["live_guard"]()
                return "fixture-real"

            with mock.patch.object(run_stage, "_run_isolated", side_effect=adapter) as child:
                status, record_out = fixtures.invoke(*self.args())
            self.assertEqual((0, "completed"), (status, record_out["result"]))
            child.assert_called_once()
            claims = self.root / "1_DATA" / run_stage.LIVE_CLAIMS_DIRECTORY
            self.assertEqual([identity], [p.name for p in claims.iterdir()])
        finally:
            self.registry_path.write_bytes(original)

    def test_post_mint_configuration_changes_rejected_before_claim(self):
        original = self.registry_path.read_bytes()
        changes = {"id": "attacker-id", "name": "attacker-name",
                   "schedule": {"kind": "cron", "expr": "* * * * *"},
                   "model": "attacker-model", "provider": "attacker-provider",
                   "state": "paused",
                   "workdir": "/private/tmp/escape", "context_from": "attacker-context",
                   "enabled_toolsets": ["all"], "prompt": "attacker prompt",
                   "skills": ["all"], "skill": "attacker-skill",
                   "script": "attacker.py", "no_agent": True,
                   "base_url": "https://attacker.invalid",
                   "monitor_script": "attacker-monitor.py",
                   "monitor_url": "https://attacker.invalid/monitor",
                   "origin": "attacker-origin",
                   "enabled": False}
        for field, value in changes.items():
            with self.subTest(field=field):
                self.registry_path.write_bytes(original)
                self.mutate_job(lambda job: job.update({field: value}))
                self.rejected()
                self.assertFalse((self.root / "1_DATA" / run_stage.LIVE_CLAIMS_DIRECTORY).exists())

    def test_missing_fields_disabled_and_duplicate_job_fail_closed(self):
        original = self.registry_path.read_bytes()
        for field in set(validator.SCHEDULER_JOB_DIGEST_REQUIRED_FIELDS) | {"enabled"}:
            with self.subTest(missing=field):
                self.registry_path.write_bytes(original)
                self.mutate_job(lambda job: job.pop(field))
                self.rejected()
                output = self.fire.with_name("missing-%s.json" % field)
                with self.assertRaises((ValueError, ContractError)):
                    mint_fire_key.mint(1, "b", 10, output)
                self.assertFalse(output.exists())
        # A missing execution-payload field fails with the clear existing reason.
        self.registry_path.write_bytes(original)
        self.mutate_job(lambda job: job.pop("prompt"))
        with self.assertRaisesRegex((ValueError, ContractError),
                                    "scheduled fire job binding requires complete"):
            mint_fire_key.mint(1, "b", 10, self.fire.with_name("missing-prompt.json"))
        for field, value in (("enabled", False), ("enabled", 1), ("state", "paused"), ("state", "disabled")):
            self.registry_path.write_bytes(original)
            self.mutate_job(lambda job: job.update({field: value}))
            with self.assertRaises((ValueError, ContractError)):
                mint_fire_key.mint(1, "b", 10, self.fire.with_name("invalid-state.json"))
            self.rejected()
        registry = json.loads(original)
        registry["jobs"].append(registry["jobs"][0])
        self.registry_path.write_text(json.dumps(registry))
        self.rejected()

        # Manual-run injection channel (fire-time prompt append) fails closed.
        for injected_field in ("manual_run_prompt", "manual_run_at"):
            self.registry_path.write_bytes(original)
            self.mutate_job(lambda job, key=injected_field: job.update({key: "injected"}))
            with self.assertRaises((ValueError, ContractError)):
                mint_fire_key.mint(1, "b", 10, self.fire.with_name("manual-run.json"))
            self.assertFalse(self.fire.with_name("manual-run.json").exists())
        self.registry_path.write_bytes(original)

    def test_runtime_noise_after_mint_and_mid_run_preserves_key_and_claim(self):
        original = self.fire.read_bytes()
        identity = self.validate_fire()
        old_registry_hash = run_stage._sha256(self.registry_path)
        noise = {"fire_claim": {"token": "tick-1"}, "last_run_at": "now",
                 "last_status": "running", "last_error": None, "next_run_at": "later",
                 "model_snapshot": "snapshot", "provider_snapshot": "snapshot",
                 "updated_at": "now", "deliver": "none", "failure_streak": 1,
                 "created_at": "then", "paused_at": "then", "paused_reason": "x",
                 "monitor_state": "idle", "schedule_display": "0 9 * * *",
                 "inactivity_limit": 600, "repeat": {"times": 3, "completed": 1},
                 "last_delivery_error": None, "last_delivery_unverified": None}
        for field, value in noise.items():
            self.mutate_job(lambda job: job.update({field: value}))
            self.assertEqual(identity, self.validate_fire())
        self.assertNotEqual(old_registry_hash, run_stage._sha256(self.registry_path))

        def adapter(contract, root, stage, **kwargs):
            self.mutate_job(lambda job: job.update(fire_claim={"token": "tick-mid-run"}))
            kwargs["live_guard"]()
            return "fixture-run"

        with mock.patch.object(run_stage, "_run_isolated", side_effect=adapter) as child:
            status, record = fixtures.invoke(*self.args())
        self.assertEqual((0, "completed"), (status, record["result"]))
        child.assert_called_once()
        self.assertEqual(original, self.fire.read_bytes())
        claims = self.root / "1_DATA" / run_stage.LIVE_CLAIMS_DIRECTORY
        self.assertEqual([identity], [p.name for p in claims.iterdir()])
        self.assertEqual("consumed\n", (claims / identity).read_text())
        self.rejected()

    def test_manual_authorization_still_binds_whole_registry(self):
        self.set_registry(False, "paused")
        auth = self.authorization(1)
        self.auth = auth
        self.save_auth()
        contract = run_stage._load_validated_request(*run_stage.HANDOFFS[1][2:])
        run_stage._validate_live_authorization(self.auth_path, contract, 1, "b")
        self.mutate_job(lambda job: job.update(fire_claim={"token": "changed"}))
        with self.assertRaisesRegex(ValueError, "binding or permission"):
            run_stage._validate_live_authorization(self.auth_path, contract, 1, "b")

    def test_file_permissions_and_public_key_fail_closed(self):
        for mode in (0o644, 0o640, 0o604, 0o400, 0o660):
            with self.subTest(mode=mode):
                self.fire.chmod(mode)
                self.rejected()
        self.fire.chmod(0o600)
        self.public_path.chmod(0o660)
        self.rejected()
        self.public_path.chmod(0o600)
        self.public_path.unlink()
        self.assertIn("scheduled-fire-owner-public-key-missing", self.rejected()["reason"])

    def test_wrong_public_key_symlink_and_owner_rejected(self):
        raw = self.public_path.read_bytes()
        self.public_path.write_bytes(b"wrong public key")
        self.rejected()
        self.public_path.write_bytes(raw)
        with mock.patch.object(run_stage.pwd, "getpwuid", return_value=mock.Mock(pw_name="someone-else")):
            self.rejected()
        target = self.public_path.with_name("target")
        self.public_path.rename(target)
        self.public_path.symlink_to(target)
        self.rejected()

    def test_revocation(self):
        contract = json.loads(self.contract_path.read_text())
        contract["migration"]["bounded_live_execution"]["manual_execution_enabled"] = False
        self.contract_path.write_text(json.dumps(contract))
        self.assertEqual("bounded-live-execution-revoked", self.rejected()["reason"])

    def test_modes_are_exclusive_and_phase_cannot_redirect(self):
        for extra in (["--live-run"], ["--authorization", str(self.auth_path)],
                      ["--dry-run", "--isolated-root", str(self.root)]):
            with mock.patch.object(run_stage, "_run_live") as runner:
                self.assertEqual(1, fixtures.invoke(*(self.args() + extra))[0])
                runner.assert_not_called()
        self.assertEqual(1, fixtures.invoke(*self.args("a"))[0])
        self.assertFalse((self.root / "1_DATA" / run_stage.LIVE_CLAIMS_DIRECTORY).exists())

    def test_mint_mode_sha_default_phase_and_no_overwrite(self):
        out = self.fire.with_name("phase-a.json")
        digest = mint_fire_key.mint(1, None, 10, out)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        self.assertEqual(out.stat().st_uid, os.geteuid())
        self.assertEqual(run_stage._sha256(out), digest)
        self.assertEqual("a", json.loads(out.read_text())["payload"]["stage_phase"])
        with self.assertRaisesRegex(ValueError, "new absolute external file"):
            mint_fire_key.mint(1, None, 10, out)
        for ttl in (0, 16, True):
            with self.assertRaises(ValueError):
                mint_fire_key.mint(1, "b", ttl, self.fire.with_name("invalid.json"))

    def test_contract_scheme_and_no_rollback(self):
        contract = json.loads(self.contract_path.read_text())
        validate_contract(contract)
        contract["migration"]["bounded_live_execution"]["recurring_scheduler_execution"] = "forbidden"
        with self.assertRaises(ContractError):
            validate_contract(contract)

    def test_code_constant_and_embedded_public_key_must_match(self):
        original = json.loads(self.contract_path.read_text())
        for field in ("public_key_b64", "key_sha256"):
            contract = json.loads(json.dumps(original))
            contract["migration"]["bounded_live_execution"]["owner_public_key"][field] = None
            with self.assertRaises(ContractError):
                validate_contract(contract)
        for constant in ("OWNER_PUB_KEY_B64", "OWNER_PUB_KEY_SHA256"):
            with mock.patch.object(validator, constant, "wrong"):
                with self.assertRaisesRegex(ContractError, "literally match"):
                    validator.validate_bounded_live_execution(original)

    def test_contract_only_repin_to_attacker_key_is_denied(self):
        private = ed25519.generate_private_der()
        public = ed25519.public_from_private_der(private)
        b64 = base64.b64encode(public).decode("ascii")
        pem = ("-----BEGIN PUBLIC KEY-----\n" + b64 + "\n-----END PUBLIC KEY-----\n").encode("ascii")
        contract = json.loads(self.contract_path.read_text())
        contract["migration"]["bounded_live_execution"]["owner_public_key"].update(
            public_key_b64=b64, key_sha256=hashlib.sha256(pem).hexdigest())
        self.public_path.write_bytes(pem)
        self.contract_path.write_text(json.dumps(contract))
        with self.assertRaisesRegex(ContractError, "literally match"):
            validate_contract(contract)
        with mock.patch.object(mint_fire_key, "_read_keychain_private") as keychain:
            with self.assertRaises(ContractError):
                mint_fire_key.mint(1, "b", 10, self.fire.with_name("forged.json"))
            keychain.assert_not_called()
        self.rejected()

    def test_guard_rechecks_expiry_and_registry_before_handoff(self):
        for change in ("expiry", "registry", "public_key"):
            with self.subTest(change=change):
                def adapter(contract, root, stage, **kwargs):
                    self.assertTrue((root / "1_DATA" / run_stage.LIVE_CLAIMS_DIRECTORY).is_dir())
                    if change == "expiry":
                        with mock.patch.object(run_stage, "_validate_authorization_times", side_effect=ValueError("expired")):
                            kwargs["live_guard"]()
                    elif change == "registry":
                        self.set_registry(False, "paused")
                        kwargs["live_guard"]()
                    else:
                        self.public_path.chmod(0o660)
                        kwargs["live_guard"]()
                with mock.patch.object(run_stage, "_run_isolated", side_effect=adapter):
                    self.assertEqual(1, fixtures.invoke(*self.args())[0])
                self.assertFalse(run_stage._handoff_path(self.root, 1).exists())
                self.set_registry(True, "scheduled")
                self.public_path.chmod(0o600)
                self.fire = self.fire.with_name(change + ".json")
                mint_fire_key.mint(1, "b", 10, self.fire)

    def test_owner_initialization_is_unpinned_until_review(self):
        """Provisioning happens only behind --init; a plain mint on a missing item fails."""
        self.keychain_patch.stop()
        missing = mock.Mock(returncode=44)
        created = mock.Mock(returncode=0)
        output = self.fire.with_name("initial.json")
        # Without --init, a missing item is a hard error and no provisioning write occurs.
        with mock.patch.object(mint_fire_key.subprocess, "run", side_effect=[missing]) as security, \
                self.assertRaisesRegex(ValueError, "Keychain item ge16_owner_ed25519 not found; run with --init"):
            mint_fire_key.mint(1, "a", 10, output)
        security.assert_called_once()
        self.assertFalse(output.exists())
        # With --init, the owner provisions: the security -i branch runs and peaks "initialized".
        with mock.patch.object(mint_fire_key.subprocess, "run", side_effect=[missing, created]) as security:
            with self.assertRaisesRegex(ValueError, "Keychain item initialized; no fire minted"):
                mint_fire_key.mint(1, "a", 10, output, init_keychain=True)
        args, kwargs = security.call_args_list[1]
        self.assertEqual(["/usr/bin/security", "-i"], args[0])
        self.assertIn(b"-T ''", kwargs["input"])
        self.assertNotIn(b"unlock-keychain", kwargs["input"])
        self.assertFalse(output.exists())

    def test_noncanonical_base64_and_signature_on_different_payload(self):
        original = json.loads(self.fire.read_text())
        for field in ("signature", "payload_b64"):
            for value in (original[field] + "\n", original[field] + "=", "-___"):
                envelope = dict(original, **{field: value})
                self.fire.write_text(json.dumps(envelope))
                self.rejected()
        # Pad bits are ignored by ordinary base64 decoders; enforce re-encoding.
        sig = original["signature"]
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
        noncanonical = sig[:-3] + alphabet[alphabet.index(sig[-3]) + 1] + "=="
        self.assertEqual(base64.b64decode(sig), base64.b64decode(noncanonical))
        self.fire.write_text(json.dumps(dict(original, signature=noncanonical)))
        self.rejected()
        self.fire.write_text(json.dumps(dict(original, signature=base64.b64encode(
            self.sign(b"different payload")).decode("ascii"))))
        self.rejected()
        altered = dict(original)
        altered["payload"] = dict(original["payload"], key_id=str(run_stage.uuid.uuid4()))
        altered["payload_b64"] = base64.b64encode(run_stage._canonical_json_bytes(altered["payload"])).decode("ascii")
        self.fire.write_text(json.dumps(altered))
        self.rejected()

    def test_outside_delivery_directory_and_unsafe_directory_denied(self):
        self.fire.rename(self.auth_path.with_name("elsewhere.json"))
        self.fire = self.auth_path.with_name("elsewhere.json")
        self.rejected()
        for mode in (0o770, 0o755):
            run_stage.FIRE_KEY_DIRECTORY.chmod(mode)
            with self.assertRaises(ValueError):
                mint_fire_key.mint(1, "b", 10, run_stage.FIRE_KEY_DIRECTORY / "new.json")
        run_stage.FIRE_KEY_DIRECTORY.chmod(0o700)

    def test_private_key_mismatch_and_temporary_file_cleanup(self):
        other = ed25519.generate_private_der()
        real_loader = ed25519.public_from_private_file
        seen = []
        real_tmp = tempfile.TemporaryDirectory
        def temporary(**kwargs):
            folder = real_tmp(**kwargs)
            seen.append(pathlib.Path(folder.name))
            return folder
        def check_mode(path):
            self.assertEqual(0o600, stat.S_IMODE((seen[-1] / "T").stat().st_mode))
            return real_loader(path)
        with mock.patch.object(mint_fire_key, "_read_keychain_private", return_value=other), \
                mock.patch.object(mint_fire_key.tempfile, "TemporaryDirectory", side_effect=temporary), \
                mock.patch.object(ed25519, "public_from_private_file", side_effect=check_mode):
            with self.assertRaisesRegex(ValueError, "does not match"):
                mint_fire_key.mint(1, "b", 10, self.fire.with_name("mismatch.json"))
        self.assertTrue(seen)
        self.assertTrue(all(not path.exists() for path in seen))

    def test_cron_context_cannot_reach_keychain_through_minter(self):
        self.terminal_patch.stop()
        self.keychain_patch.stop()
        with mock.patch.object(mint_fire_key.os, "isatty", return_value=False), \
                mock.patch.object(mint_fire_key.subprocess, "run") as security:
            with self.assertRaisesRegex(ValueError, "cron/non-interactive minting denied"):
                mint_fire_key._read_keychain_private(True)
            with self.assertRaisesRegex(ValueError, "confirm-owner"):
                mint_fire_key._read_keychain_private(False)
            security.assert_not_called()

    def test_keychain_denial_never_attempts_unlock_or_provisioning(self):
        self.keychain_patch.stop()
        with mock.patch.object(mint_fire_key.subprocess, "run", return_value=mock.Mock(returncode=36)) as security:
            with self.assertRaisesRegex(ValueError, "access denied"):
                mint_fire_key._read_keychain_private(True)
            self.assertEqual(1, security.call_count)
            self.assertEqual("find-generic-password", security.call_args.args[0][1])

    def test_scheduled_binding_accepts_stages_two_through_four(self):
        for stage in (2, 3, 4):
            output = self.fire.with_name("stage-%d.json" % stage)
            mint_fire_key.mint(stage, None, 10, output)
            _, _, identifier, version = run_stage.HANDOFFS[stage]
            contract = run_stage._load_validated_request(identifier, version)
            identity = run_stage._validate_scheduled_fire(output, contract, stage)
            self.assertEqual(hashlib.sha256(base64.b64decode(json.loads(output.read_text())["payload_b64"])).hexdigest(), identity)

    def test_stage_five_remains_forbidden(self):
        with self.assertRaisesRegex(ValueError, "only stages 1-4"):
            mint_fire_key.mint(5, None, 10, self.fire.with_name("5-none.json"))
        _, _, identifier, version = run_stage.HANDOFFS[5]
        status, record = fixtures.invoke("--contract", identifier, "--version", version,
                                       "--scheduled-fire", "--fire-key", str(self.fire), "--stage", "5")
        self.assertEqual((1, "stage-5-live-execution-forbidden"), (status, record["reason"]))


class WatchdogPatchTests(unittest.TestCase):
    def test_schedule_time_guard_and_watchdog_bounds(self):
        patch = (pathlib.Path(__file__).resolve().parents[1] / "cron/patches/scheduler-per-job-inactivity.patch").read_text()
        jobs_patch = patch[patch.index("--- a/cron/jobs.py"):]
        added = "\n".join(line[1:] for line in jobs_patch.splitlines()
                          if line.startswith("+") and not line.startswith("+++"))
        helper_source = added[:added.index("# A sentinel")]
        namespace = {"Any": object}
        exec(compile(ast.parse(helper_source), "<schedule-limit-patch>", "exec"), namespace)
        helper = namespace["_validate_inactivity_limit"]
        for valid in (1, 1800, 3600):
            self.assertEqual(valid, helper(valid))
        for bad in (True, False, 0, -1, 3601, 1000000, "1800", 1.5, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                helper(bad)
        self.assertIn('+        _validate_inactivity_limit(inactivity_limit)', jobs_patch)
        self.assertIn('+        _validate_inactivity_limit(updates["inactivity_limit"])', jobs_patch)
        self.assertIn('+        job["inactivity_limit"] = inactivity_limit', jobs_patch)
        scheduler_patch = patch[:patch.index("--- a/cron/jobs.py")]
        # Reconstruct the whole helper from unchanged context plus new lines.
        lines = scheduler_patch.splitlines()
        start = next(i for i, line in enumerate(lines) if line == " def _job_inactivity_seconds(job: dict) -> float:")
        function_lines = []
        for line in lines[start:]:
            if line.startswith("@@"):
                break
            if line.startswith((" ", "+")):
                function_lines.append(line[1:])
        scope = {"_cron_inactivity_seconds": lambda: 600}
        exec(compile(ast.parse("\n".join(function_lines)), "<watchdog-patch>", "exec"), scope)
        watchdog = scope["_job_inactivity_seconds"]
        self.assertEqual(600, watchdog({}))
        self.assertEqual(3600, watchdog({"inactivity_limit": 3600}))
        for bad in (True, 0, -1, 3601, "1800", 1.5, None):
            with self.assertRaises(ValueError):
                watchdog({"inactivity_limit": bad})

    def test_canonical_validator_rejects_out_of_range_limit(self):
        from cron.sync_jobs import validate_canonical_config, SyncError
        root = pathlib.Path(__file__).resolve().parents[1]
        contract = json.loads((root / "ops-contract.json").read_text())
        for bad in (3601, 0, -1, True, "1800", None):
            config = json.loads((root / "cron/ge16_jobs.json").read_text())
            config["jobs"][0]["inactivity_limit"] = bad
            with self.assertRaisesRegex(SyncError, "1 through 3600"):
                validate_canonical_config(config, contract["migration"], contract["cron_contracts"])
