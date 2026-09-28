"""The optional analytics-authoring step sits in the weekly chain — safely.

The weekly chain runs the deterministic editions under the child sandbox, then
(optionally) the AI-authored narrative pass. This file pins the contract that
makes the second half safe to declare next to the first:

  * the chain order is code-owned and places `analytics-authoring` after
    `analytics-state-report-ms` and before Stage 3;
  * the step is never handed a sandbox slot and never gains outbound network —
    the per-command network containment stays exactly as narrow as it was;
  * its command is read-only with respect to every published edition: a pass
    writes 03_REPORTS/ai and its own manifests, and the deterministic federal and
    state editions are byte-identical afterwards;
  * the companion job spec exists in the canonical location, ships disabled, and
    is not silently part of the active registry.
"""

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ANALYTICS = HERE.parent.parent                      # .../2_ANALYTICS
REPO = ANALYTICS.parent                             # .../Malaysia General Election v2
OPS = REPO / "OPS"
ENGINE = ANALYTICS / "02_FORECAST" / "engine"
PYTHON = ANALYTICS / ".venv" / "bin" / "python"
JOB_SPEC = OPS / "cron" / "jobs" / "ge16-authoring.json"
REGISTRY = OPS / "cron" / "ge16_jobs.json"
TMP = tempfile.mkdtemp(prefix="ge16-authoring-cron-")

sys.path.insert(0, str(ENGINE))
import author_reports as ar  # noqa: E402


def _load_run_stage():
    """Import OPS/cron/run_stage.py without disturbing this suite's imports."""
    sys.path.insert(0, str(OPS))
    try:
        spec = importlib.util.spec_from_file_location(
            "ge16_ops_run_stage", OPS / "cron" / "run_stage.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(OPS))


RUN_STAGE = _load_run_stage()
STEP = RUN_STAGE.OPTIONAL_CHAIN_STEPS[2][0]


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ChainOrderTests(unittest.TestCase):
    def test_authoring_follows_the_last_deterministic_report_command(self):
        chain = RUN_STAGE.chain_steps(2)
        self.assertIn("analytics-authoring", chain)
        self.assertIn("analytics-state-report-ms", chain)
        self.assertLess(chain.index("analytics-state-report-ms"),
                        chain.index("analytics-authoring"))

    def test_authoring_is_declared_for_stage_two_only(self):
        self.assertEqual(RUN_STAGE.chain_steps(2), RUN_STAGE.STAGE2_CHAIN)
        for stage in (1, 3):
            self.assertNotIn("analytics-authoring", RUN_STAGE.chain_steps(stage))

    def test_declared_chain_keeps_every_sandboxed_command(self):
        declared = set(RUN_STAGE.STAGE2_CHAIN)
        for name in RUN_STAGE.STAGE2_COMMAND_NAMES:
            self.assertIn(name, declared)
        self.assertEqual(len(RUN_STAGE.STAGE2_CHAIN), len(declared),
                         "the chain must not list a command twice")


class SandboxBoundaryTests(unittest.TestCase):
    def test_authoring_is_allowlisted_but_never_gets_a_sandbox_slot(self):
        self.assertFalse(STEP["sandboxed"])
        # The runner knows the exact executable from a code-owned table ...
        self.assertIn(STEP["name"], RUN_STAGE.COMMAND_ALLOWLIST)
        self.assertEqual(tuple(RUN_STAGE.COMMAND_ALLOWLIST[STEP["name"]]),
                         (STEP["script"],) + tuple(STEP["argv"]))
        # ... and nothing beyond it: the step is not a command of any adapter, so
        # it is never handed a sandbox slot, and its child policy stays "deny".
        self.assertNotIn(STEP["name"], RUN_STAGE.STAGE2_COMMAND_NAMES)
        for adapter, (_stage, _owner, commands) in RUN_STAGE.STAGE_ADAPTERS.items():
            self.assertNotIn(STEP["name"], commands, adapter)
        self.assertEqual(RUN_STAGE.COMMAND_CHILDPOLICY[STEP["name"]], "deny")

    def test_outbound_network_stays_with_the_collectors(self):
        allowed = {name for name, policy in RUN_STAGE.COMMAND_CHILDPOLICY.items()
                   if policy == "https"}
        self.assertEqual(allowed, {"data-collect-news", "data-collect-polls",
                                   "data-collect-candidates"})

    def test_step_is_non_blocking_by_contract(self):
        self.assertTrue(STEP["non_blocking"])
        self.assertEqual(STEP["owner_domain"], "2_ANALYTICS")
        self.assertEqual(RUN_STAGE.STAGE_ADAPTERS["analytics-stage-2"][1], "2_ANALYTICS")


class ReadonlyContractTests(unittest.TestCase):
    def test_declared_command_is_readonly_toward_published_editions(self):
        self.assertEqual(STEP["script"], "2_ANALYTICS/02_FORECAST/engine/author_reports.py")
        self.assertTrue((REPO / STEP["script"]).is_file())
        for path in STEP["writes"]:
            self.assertIn(path, ("2_ANALYTICS/03_REPORTS/ai", "2_ANALYTICS/work/reports"))
        for published in ("2_ANALYTICS/03_REPORTS/federal", "2_ANALYTICS/03_REPORTS/states"):
            self.assertIn(published, STEP["must_not_write"])
        for domain in ("1_DATA", "3_OUTPUTS", "4_DELIVERY", "5_WEBSITES", "OPS"):
            self.assertIn(domain, STEP["must_not_write"])

    def test_a_pass_leaves_the_published_editions_byte_identical(self):
        """A full offline pass into a scratch root, with the published files hashed."""
        published = [
            ANALYTICS / "03_REPORTS" / "federal" / "latest" / "GE16_Malaysia_General_Election_Report.md",
            ANALYTICS / "03_REPORTS" / "federal" / "latest" / "GE16_Malaysia_General_Election_Report_MS.md",
            ANALYTICS / "03_REPORTS" / "states" / "DUN Pulau Pinang" / "latest" / "GE16_Pulau Pinang_Report.md",
            ANALYTICS / "03_REPORTS" / "states" / "DUN Pulau Pinang" / "latest" / "GE16_Pulau Pinang_Report_MS.md",
        ]
        before = {path: _digest(path) for path in published if path.is_file()}
        self.assertTrue(before, "expected published deterministic editions to exist")

        for lang in ("en", "ms"):
            completed = subprocess.run(
                [str(PYTHON), str(ENGINE / "author_reports.py"), "--lang", lang, "--offline",
                 "--out-root", os.path.join(TMP, lang, "ai"),
                 "--work-root", os.path.join(TMP, lang, "work"),
                 "--pack-root", os.path.join(TMP, lang, "pack")],
                cwd=str(ANALYTICS), capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr[-800:])

        for path, digest in before.items():
            self.assertEqual(_digest(path), digest, "{0} was rewritten".format(path.name))
        # The pass writes editions where it was told to, and nowhere else.
        self.assertTrue((Path(TMP) / "en" / "ai").is_dir())

    def test_edition_roots_are_distinct(self):
        self.assertTrue(ar.REPORT_AI.endswith(os.path.join("03_REPORTS", "ai")))
        authority = ar.REPORT_AUTHORITY["federal"]
        self.assertTrue(authority.endswith(os.path.join(
            "03_REPORTS", "federal", "latest", "GE16_Malaysia_General_Election_Report.md")))
        for path in ar.REPORT_AUTHORITY.values():
            self.assertNotIn(os.path.join("03_REPORTS", "ai"), path)
        self.assertNotEqual(ar.REPORT_AI, os.path.dirname(authority))


class ChainNonBlockingTests(unittest.TestCase):
    """A failed authoring pass never stops the deterministic stages behind it."""

    def test_a_failed_authoring_step_does_not_stop_the_chain(self):
        ran = []
        failed = []

        def run_step(name):
            ran.append(name)
            if name == STEP["name"]:
                raise RuntimeError("writer transport down")

        advanced = RUN_STAGE.advance_chain(2, run_step, log=failed.append)

        self.assertEqual(tuple(ran), RUN_STAGE.chain_steps(2),
                         "every command of the stage-2 chain must still be attempted")
        self.assertNotIn(STEP["name"], advanced)
        self.assertEqual(tuple(advanced), tuple(
            name for name in RUN_STAGE.chain_steps(2) if name != STEP["name"]))
        self.assertEqual(len(failed), 1)
        self.assertIn("analytics-authoring", failed[0])
        # Stage 3 is the stage the authoring step must never be able to stop.
        self.assertEqual(RUN_STAGE.chain_steps(3)[0], "outputs-intake-seal")
        self.assertEqual(RUN_STAGE.advance_chain(3, lambda name: ran.append(name)),
                         list(RUN_STAGE.chain_steps(3)))

    def test_a_failed_sandboxed_step_still_stops_the_chain(self):
        def run_step(name):
            if name == "analytics-report-en":
                raise RuntimeError("adapter failed")
        with self.assertRaises(RuntimeError):
            RUN_STAGE.advance_chain(2, run_step)

    def test_only_the_declared_steps_are_non_blocking(self):
        declared = {step["name"]: step for step in RUN_STAGE.OPTIONAL_CHAIN_STEPS[2]}
        self.assertIn(STEP["name"], declared)
        self.assertIs(RUN_STAGE.optional_step(2, STEP["name"]), STEP)
        self.assertTrue(STEP["non_blocking"])
        for name, step in declared.items():
            self.assertTrue(step["non_blocking"], name)
            self.assertFalse(step["sandboxed"], name)
        for name in RUN_STAGE.STAGE2_CHAIN:
            if name not in declared:
                self.assertIsNone(RUN_STAGE.optional_step(2, name))


MIGRATION_IDS = ("441fedd48bc8", "2b0a9111c836", "c4cebfce9fb0", "152172eb38fd",
                 "9194211d627e")
OPTIONAL_JOB_ID = "ge16-authoring"
OPTIONAL_CONTRACT_ID = "ge16-cron-analytics-authoring"
CONTRACT_PATH = OPS / "ops-contract.json"


def _load_sync_jobs():
    """Import OPS/cron/sync_jobs.py read-only, as the runner does."""
    sys.path.insert(0, str(OPS))
    try:
        spec = importlib.util.spec_from_file_location(
            "ge16_ops_sync_jobs", OPS / "cron" / "sync_jobs.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(OPS))


SYNC_JOBS = _load_sync_jobs()


class CanonicalRegistryTests(unittest.TestCase):
    """The sixth job is in the canonical registry, and the validators accept it.

    OPS cannot run its own suite in this environment, so the acceptance logic is
    additionally driven here for real: the OPS validator functions are imported
    and called against the checked-in registry and contract, both for the
    declared shape and for the shapes that must still fail closed.
    """

    @classmethod
    def setUpClass(cls):
        cls.registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
        cls.contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        cls.jobs = cls.registry["jobs"]
        cls.crons = cls.contract["cron_contracts"]

    def _validate(self, jobs=None, crons=None):
        SYNC_JOBS.validate_canonical_config(
            self.registry if jobs is None else dict(self.registry, jobs=jobs),
            self.contract["migration"],
            self.crons if crons is None else crons)

    def test_the_five_stage_jobs_are_untouched_and_in_order(self):
        self.assertEqual([job["id"] for job in self.jobs[:5]], list(MIGRATION_IDS))
        self.assertEqual(list(self.registry["migration_identities"]), list(MIGRATION_IDS))

    def test_the_sixth_job_is_declared_disabled_and_non_blocking(self):
        # The authoring job stays the first optional job, at index 5. The OPS
        # chain-orchestrator job (OPS chain mode, ge16-chain) is the only record
        # allowed to follow it, and it ships disabled and never non-blocking.
        self.assertIn(len(self.jobs), (6, 7))
        if len(self.jobs) == 7:
            self.assertEqual(self.jobs[6]["id"], "ge16-chain")
            self.assertFalse(self.jobs[6]["enabled"])
            self.assertEqual(self.jobs[6]["state"], "disabled")
            self.assertFalse(self.jobs[6]["non_blocking"])
        job = self.jobs[5]
        self.assertEqual(job["id"], OPTIONAL_JOB_ID)
        self.assertEqual(job["contract_id"], OPTIONAL_CONTRACT_ID)
        self.assertFalse(job["enabled"])
        self.assertEqual(job["state"], "disabled")
        self.assertTrue(job["non_blocking"])
        self.assertEqual(job["gate_impact"], "none")
        self.assertEqual(job["stage_number"], 2)
        self.assertEqual(job["dependencies"], [MIGRATION_IDS[1]])
        self.assertEqual(job["schedule"]["expr"], "0 2 * * 6")
        self.assertEqual(job["timezone"], "Asia/Kuala_Lumpur")
        self.assertEqual((job["model"], job["provider"]),
                         ("deepseek/deepseek-v4-pro", "nous"))
        self.assertEqual(job["workdir"], str(REPO))
        self.assertEqual(tuple(job["writes"]), STEP["writes"])
        self.assertEqual(tuple(job["must_not_write"]), STEP["must_not_write"])
        for published in ("2_ANALYTICS/03_REPORTS/federal", "2_ANALYTICS/03_REPORTS/states"):
            self.assertNotIn(published, job["writes"])
        self.assertEqual(job["prompt"], json.loads(
            JOB_SPEC.read_text(encoding="utf-8"))["job"]["prompt"],
            "the registry record and the spec must carry one prompt")

    def test_the_validators_accept_the_declared_sixth_job(self):
        self.assertIsNone(self._validate())
        self.assertEqual([cron["id"] for cron in self.crons[:5]],
                         ["ge16-cron-data-collection-validation",
                          "ge16-cron-analytics-forecast-reports-social",
                          "ge16-cron-outputs-intake-sealing",
                          "ge16-cron-delivery-publish-validation",
                          "ge16-cron-websites-build-release-gate"])
        optional = self.crons[5]
        self.assertEqual(optional["id"], OPTIONAL_CONTRACT_ID)
        self.assertEqual(optional["version"], "1.0.0")
        self.assertTrue(optional["optional_stage"])
        self.assertNotIn("isolated_adapter", optional)
        binding = optional["scheduler_binding"]
        job = self.jobs[5]
        self.assertEqual(binding["migration_job_id"], job["id"])
        self.assertFalse(binding["enabled"])
        for field in ("schedule", "timezone", "delivery", "model", "provider",
                      "model_snapshot", "provider_snapshot"):
            value = binding[field]
            self.assertEqual(value, job["schedule"]["expr"] if field == "schedule" else job[field])

    def test_the_validators_still_fail_closed_on_everything_else(self):
        cases = {}
        jobs = json.loads(json.dumps(self.jobs))
        jobs[5]["enabled"] = True
        cases["an enabled sixth job"] = (jobs, None)
        jobs = json.loads(json.dumps(self.jobs))
        jobs[5]["id"] = "ge16-something-else"
        cases["an unknown sixth job id"] = (jobs, None)
        jobs = json.loads(json.dumps(self.jobs))
        jobs[5]["non_blocking"] = False
        cases["a blocking sixth job"] = (jobs, None)
        jobs = json.loads(json.dumps(self.jobs))
        jobs.pop(2)
        cases["a missing stage job"] = (jobs, None)
        jobs = json.loads(json.dumps(self.jobs))
        cases["a sixth job without its contract"] = (jobs, self.crons[:5])
        crons = json.loads(json.dumps(self.crons))
        crons[5]["scheduler_binding"]["enabled"] = True
        cases["an enabled optional contract"] = (None, crons)
        crons = json.loads(json.dumps(self.crons))
        crons[5]["isolated_adapter"] = "analytics-authoring"
        cases["an optional contract claiming an adapter"] = (None, crons)
        for label, (job_value, cron_value) in cases.items():
            with self.subTest(case=label):
                self.assertRaises(SYNC_JOBS.SyncError, self._validate, job_value, cron_value)

    def test_the_ops_contract_validator_accepts_one_optional_contract_only(self):
        validator = _load_ops_validator()
        self.assertIsNone(validator.validate_contract(self.contract))
        self.assertIsNone(validator._validate_cron_source_alignment(self.contract))
        crons = json.loads(json.dumps(self.crons))
        crons[5]["version"] = "9.9.9"
        with self.assertRaises(validator.ContractError):
            validator.validate_contract(dict(self.contract, cron_contracts=crons))
        crons = json.loads(json.dumps(self.crons))
        crons.append(json.loads(json.dumps(self.crons[5])))
        with self.assertRaises(validator.ContractError):
            validator.validate_contract(dict(self.contract, cron_contracts=crons))

    def test_the_source_no_longer_pins_the_count_to_five(self):
        sync_source = (OPS / "cron" / "sync_jobs.py").read_text(encoding="utf-8")
        ops_source = (OPS / "validate_ops_contract.py").read_text(encoding="utf-8")
        for stale in ("len(jobs) != 5", "len(cron_contracts) != 5", "len(crons) != 5"):
            self.assertNotIn(stale, sync_source)
            self.assertNotIn(stale, ops_source)
        self.assertIn("_validate_optional_job", sync_source)
        self.assertIn("OPTIONAL_JOB_IDS", sync_source)
        self.assertIn("_validate_optional_cron_contract", ops_source)


def _load_ops_validator():
    """Import OPS/validate_ops_contract.py read-only."""
    sys.path.insert(0, str(OPS))
    try:
        spec = importlib.util.spec_from_file_location(
            "ge16_ops_validate_contract", OPS / "validate_ops_contract.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(OPS))


class JobSpecTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = json.loads(JOB_SPEC.read_text(encoding="utf-8"))
        cls.job = cls.spec["job"]

    def test_spec_lives_in_the_canonical_location_and_is_not_enabled(self):
        self.assertTrue(JOB_SPEC.is_file())
        self.assertTrue(self.spec["registry"]["registered"],
                        "the record is declared in the canonical registry")
        self.assertEqual(self.spec["registry"]["state"], "declared-disabled")
        self.assertFalse(self.job["enabled"])
        self.assertEqual(self.job["state"], "disabled")

    def test_the_registry_entry_is_the_same_record_and_stays_disabled(self):
        registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
        declared = {job["id"]: job for job in registry["jobs"]}.get(self.job["id"])
        self.assertIsNotNone(declared, "the optional job must be declared, not implied")
        self.assertFalse(declared["enabled"],
                         "enabling the optional job is an owner decision, not a side effect")
        self.assertEqual(declared["state"], "disabled")
        self.assertTrue(declared["non_blocking"])
        self.assertEqual(declared["prompt"], self.job["prompt"])
        for field in ("name", "stage_number", "owner_domain", "contract_id",
                      "contract_version", "schedule", "timezone", "dependencies",
                      "model", "provider", "delivery", "enabled_toolsets", "workdir"):
            self.assertEqual(declared[field], self.job[field], field)

    def test_spec_carries_the_non_blocking_contract(self):
        self.assertTrue(self.job["non_blocking"])
        self.assertEqual(self.job["gate_impact"], "none")
        self.assertEqual(self.job["chain_position"]["after"], STEP["after"])
        self.assertEqual(self.job["schedule"]["expr"], "0 2 * * 6")
        self.assertEqual(self.job["timezone"], "Asia/Kuala_Lumpur")
        self.assertEqual(self.job["model"], "deepseek/deepseek-v4-pro")
        self.assertEqual(self.job["provider"], "nous")
        self.assertEqual(self.job["enabled_toolsets"], ["terminal", "file"])
        self.assertEqual(self.job["dependencies"], ["2b0a9111c836"])

    def test_spec_boundaries_match_the_declared_step(self):
        self.assertEqual(tuple(self.job["writes"]), STEP["writes"])
        self.assertEqual(tuple(self.job["must_not_write"]), STEP["must_not_write"])
        self.assertNotIn("2_ANALYTICS/03_REPORTS/federal", self.job["writes"])
        self.assertNotIn("2_ANALYTICS/03_REPORTS/states", self.job["writes"])
        self.assertTrue(self.job["no_commit"])

    def test_prompt_keeps_the_writer_split_and_the_shortfall_path(self):
        prompt = self.job["prompt"]
        self.assertIn("--lang en", prompt)
        self.assertIn("--lang ms", prompt)
        self.assertIn("authoring_config.json", prompt)
        self.assertIn("never", prompt.lower())
        self.assertIn("deterministic editions", prompt)

    def test_prompt_states_the_verbatim_rule_and_the_failover_policy(self):
        """No authored-rate threshold: 100% verbatim, per-section failover, and a
        pass with nothing authored publishes no edition at all (F1)."""
        prompt = self.job["prompt"]
        self.assertNotIn("70%", prompt)
        self.assertNotIn("at least 70", prompt.lower())
        self.assertNotIn("threshold (at least", prompt)
        self.assertIn("100% of the figures", prompt)
        self.assertIn("NO authored-rate threshold", prompt)
        self.assertIn("languages.<lang>.fallbacks", prompt)
        self.assertIn("chunks of at most 18 claims", prompt)
        self.assertIn("publishes NOTHING to 2_ANALYTICS/03_REPORTS/ai", prompt)
        self.assertEqual(self.job["gate_impact"], "none")


BASELINE_STEPS = {1: "data-baseline-news-delta", 2: "analytics-baseline-derived"}


def _fail_on(name):
    """A run_step that raises for exactly one chain step."""
    def run_step(step_name):
        if step_name == name:
            raise RuntimeError("baseline rebuild failed")
    return run_step


class BaselineChainStepTests(unittest.TestCase):
    """The baseline program's two optional chain steps (26 Sep wiring).

    The weekly run rebuilds the baseline from the data it just collected: the
    news delta joins Stage 1 right after the judged news commit, the derived
    layers (events, graph, VDBs, figures) join Stage 2 after the deterministic
    reports and before the 3_OUTPUTS handoff is computed. These tests mirror the
    authoring probes: the declaration is code-owned, neither step is ever given a
    sandbox slot or outbound network, a failed step leaves the deterministic
    chain — Stage 3's inputs included — standing, and a step declared
    non-optional is refused fail-closed instead of silently swallowed.
    """

    def test_baseline_steps_are_optional_non_blocking_and_gate_free(self):
        for stage, name in BASELINE_STEPS.items():
            step = RUN_STAGE.optional_step(stage, name)
            self.assertIsNotNone(step, (stage, name))
            self.assertTrue(step["non_blocking"], name)
            self.assertEqual(step["gate_impact"], "none", name)
            self.assertFalse(step["sandboxed"], name)
            adapters = {adapter for adapter, (number, _owner, _commands)
                        in RUN_STAGE.STAGE_ADAPTERS.items() if number == stage}
            self.assertNotIn(name, adapters, "an optional step is never a sandbox adapter")

    def test_baseline_commands_are_allowlisted_and_stay_deny(self):
        for name in BASELINE_STEPS.values():
            step = RUN_STAGE.optional_step(
                1 if name == BASELINE_STEPS[1] else 2, name)
            self.assertEqual(RUN_STAGE.COMMAND_ALLOWLIST[name],
                             (step["script"],) + tuple(step["argv"]))
            self.assertEqual(RUN_STAGE.COMMAND_CHILDPOLICY[name], "deny")
            self.assertTrue((REPO / step["script"]).is_file(), step["script"])

    def test_baseline_steps_sit_in_the_declared_chain_positions(self):
        stage1 = RUN_STAGE.chain_steps(1)
        self.assertEqual(stage1[stage1.index("data-commit-news") + 1], BASELINE_STEPS[1])
        self.assertLess(stage1.index(BASELINE_STEPS[1]), stage1.index("data-refresh-canonical"))
        stage2 = RUN_STAGE.chain_steps(2)
        tail = stage2[stage2.index("analytics-state-report-ms") + 1:]
        self.assertEqual(tail[0], BASELINE_STEPS[2])
        self.assertIn("analytics-authoring", tail)
        self.assertLess(tail.index(BASELINE_STEPS[2]), len(stage2) - 1,
                        "the derived rebuild runs before the last stage-2 command, "
                        "i.e. before the handoff manifest hashes are computed")

    def test_a_failed_baseline_step_does_not_stop_either_chain(self):
        for stage, name in BASELINE_STEPS.items():
            log = []
            advanced = RUN_STAGE.advance_chain(stage, _fail_on(name), log.append)
            self.assertNotIn(name, advanced, name)
            self.assertTrue(log, "the failure is logged, not swallowed")
            self.assertIn("without blocking the chain", log[0])
            self.assertEqual(RUN_STAGE.chain_steps(stage)[-1], advanced[-1],
                             "the last deterministic command of the stage still ran")

    def test_a_baseline_step_declared_non_optional_stops_the_chain(self):
        """Fail-closed probe: the non-blocking contract is enforced, not decorative."""
        for stage, name in BASELINE_STEPS.items():
            declared = RUN_STAGE.OPTIONAL_CHAIN_STEPS[stage]
            patched = tuple(dict(step, non_blocking=False) if step.get("name") == name else step
                            for step in declared)
            with mock.patch.dict(RUN_STAGE.OPTIONAL_CHAIN_STEPS, {stage: patched}):
                with self.assertRaises(RuntimeError):
                    RUN_STAGE.advance_chain(stage, _fail_on(name))

    def test_the_stage_two_step_never_mutates_another_domain(self):
        step = RUN_STAGE.optional_step(2, BASELINE_STEPS[2])
        self.assertIn("1_DATA", step["must_not_write"])
        self.assertTrue(all(w.startswith("2_ANALYTICS/") for w in step["writes"]))
        self.assertNotIn("--polls", step["argv"],
                         "the poll tracker is a 1_DATA-owned file Stage 2 must not mutate")

    def test_the_stage_one_step_owns_only_the_news_side(self):
        step = RUN_STAGE.optional_step(1, BASELINE_STEPS[1])
        self.assertEqual(step["owner_domain"], "1_DATA")
        self.assertIn("1_DATA/research/trackers", step["writes"])
        self.assertIn("1_DATA/canonical-data-provenance.json", step["must_not_write"])
        self.assertIn("2_ANALYTICS/03_REPORTS", step["must_not_write"])

    def test_baseline_steps_are_inert_in_this_repository(self):
        """This wiring is declarative: no job is enabled, no command runs here."""
        jobs = json.loads(REGISTRY.read_text(encoding="utf-8"))["jobs"]
        self.assertEqual(len(jobs), 7, "five stages plus the two optional jobs, unchanged")
        for index, (stage, name) in ((0, (1, BASELINE_STEPS[1])), (1, (2, BASELINE_STEPS[2]))):
            prompt = jobs[index]["prompt"]
            self.assertIn(name, prompt)
            self.assertIn("2_ANALYTICS/work/baseline/latest.json", prompt)
            self.assertIn("REBUILD_STATUS", prompt)
            self.assertIn("non-blocking", prompt)
            self.assertFalse(jobs[index]["enabled"], "the wiring ships disabled")


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
