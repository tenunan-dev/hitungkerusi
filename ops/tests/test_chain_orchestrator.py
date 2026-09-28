"""Executable tests for the owner-side GE16 chain orchestrator (chain mode).

The daemon has exactly two side effects -- mint one owner key and launch one
`run_stage.py --scheduled-fire` process -- and both are injected here, so every
test drives the real state machine without a key, a runner or a scheduler.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cron import ge16_chain_runner as chain  # noqa: E402
from cron import run_stage  # noqa: E402
from cron import sync_jobs  # noqa: E402


CONTRACTS = ROOT / "ops-contract.json"
JOBS = ROOT / "cron" / "ge16_jobs.json"


def load_json(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def digest_of(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ChainHarness:
    """A chain run over a throw-away handoff tree with stubbed effects."""

    def __init__(self, root: pathlib.Path, now=None):
        self.root = root
        self.now = now or chain.parse_stamp("2026-09-25T14:00:00Z")  # Friday 22:00 MYT
        self.handoffs = {
            stage: root / run_stage.HANDOFFS[stage][0] / "manifest" / run_stage.HANDOFFS[stage][1]
            for stage in chain.CHAIN_ORDER
        }
        for path in self.handoffs.values():
            path.parent.mkdir(parents=True, exist_ok=True)
        self.raw_mints: list[dict] = []
        self.mint_error = None
        self.launches: list[dict] = []
        self.alive: dict[int, bool] = {}
        self.logged: list[str] = []
        self.runner = chain.ChainRunner(
            state_path=root / "state.json",
            evidence_dir=root / "evidence",
            key_directory=root / "keys",
            stage_log_dir=root / "logs",
            handoffs=self.handoffs,
            repo_root=self.root,
            mint=self._mint,
            launch=self._launch,
            alive=self._alive,
            now_fn=lambda: self.now,
            log_fn=self.logged.append,
        )

    # -- injected effects --------------------------------------------------
    def _mint(self, spec, key_directory=None):
        self.raw_mints.append(dict(spec))
        if self.mint_error:
            raise RuntimeError(self.mint_error)
        key_file = pathlib.Path(spec["key_file"])
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(json.dumps({
            "payload": {"key_id": "key-%d-%d" % (spec["stage"], len(self.raw_mints)),
                        "expires_at_utc": chain.stamp(
                            self.now + dt.timedelta(minutes=spec["ttl_minutes"]))},
            "payload_b64": "", "signature": ""}))
        return {"key_file": str(key_file), "key_id": "key-%d-%d" % (spec["stage"], len(self.mints)),
                "expires_at_utc": chain.stamp(self.now), "sha256": digest_of(key_file)}

    def _launch(self, command, log_path):
        pid = 5000 + len(self.launches)
        self.launches.append({"command": list(command), "log_path": log_path, "pid": pid})
        self.alive[pid] = True
        return pid

    @property
    def mints(self) -> list[dict]:
        """Keys minted to execute a stage: Stage 1's keys are the agent's."""
        return [spec for spec in self.raw_mints if spec["stage"] != 1]

    @property
    def stage_one_mints(self) -> list[dict]:
        return [spec for spec in self.raw_mints if spec["stage"] == 1]

    def _alive(self, pid):
        return self.alive.get(pid, False)

    def exit_process(self, index=-1):
        self.alive[self.launches[index]["pid"]] = False

    def advance_clock(self, **kwargs):
        self.now = self.now + chain.dt.timedelta(**kwargs)

    # -- artifacts ---------------------------------------------------------
    def write_manifest(self, stage, run_id="R-1", predecessor=None):
        refs = []
        if predecessor is not None:
            refs.append("%s/manifest/%s#sha256:%s" % (
                run_stage.HANDOFFS[stage - 1][0], run_stage.HANDOFFS[stage - 1][1], predecessor))
        payload = {
            "contract_id": run_stage.HANDOFFS[stage][2],
            "contract_version": run_stage.HANDOFFS[stage][3],
            "run_id": run_id,
            "created_at_utc": chain.stamp(self.now),
            "input_manifest_refs": refs,
            "output_manifest_refs": [],
        }
        path = self.handoffs[stage]
        path.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
        return digest_of(path)

    def run_through(self, upto=4, run_id="R-1"):
        """Drive stages 1..upto to completion with stubbed effects."""
        self.runner.start()
        self.runner.poll()
        recorded = None
        for stage in range(1, upto + 1):
            if stage > 1:
                recorded = self.runner.status()["steps"][str(stage - 1)]["manifest_sha256"]
            digest = self.write_manifest(stage, run_id=run_id, predecessor=recorded)
            if stage in chain.DRIVEN_STAGES:
                self.exit_process()
            self.runner.poll()
        return digest


class ChainOrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.harness = ChainHarness(pathlib.Path(self._tmp.name))

    # -- code-owned posture -----------------------------------------------
    def test_stage_split_matches_the_runner_policy_and_key_lifetime(self) -> None:
        self.assertEqual(chain.CHAIN_ORDER, (1, 2, 3, 4, 5))
        self.assertEqual(chain.DRIVEN_STAGES, (2, 3, 4))
        self.assertEqual(chain.AGENT_STAGES, (1, 5))
        self.assertEqual(set(chain.DRIVEN_STAGES) | set(chain.AGENT_STAGES), set(chain.CHAIN_ORDER))
        self.assertNotIn(5, chain.DRIVEN_STAGES, "Stage 5 is isolated-dry-run-only for the runner")
        self.assertLessEqual(chain.KEY_TTL_MINUTES, 15, "keys never outlive the runner's 15-minute cap")
        self.assertEqual(run_stage.LIVE_AUTHORIZATION_SECONDS, 900)
        self.assertEqual(chain.KEY_DIRECTORY, pathlib.Path(run_stage.FIRE_KEY_DIRECTORY))
        self.assertEqual(chain.STAGE_WINDOW_MINUTES, {1: 240, 2: 600, 3: 30, 4: 120, 5: None})
        self.assertEqual(chain.POLL_MINUTES, 2)
        self.assertEqual(chain.MAX_RUN_HOURS, 12)
        for stage in chain.CHAIN_ORDER:
            self.assertTrue(chain.stage_command(stage, pathlib.Path("/k.json"))[0].endswith("python3"))

    def test_stage_command_is_the_exact_contract_runner_argv(self) -> None:
        command = chain.stage_command(3, pathlib.Path("/owner/keys/3-xZ.json"))
        self.assertEqual(command, [
            "/usr/bin/python3", str(ROOT / "cron" / "run_stage.py"),
            "--contract", "ge16-cron-outputs-intake-sealing", "--version", "1.4.0",
            "--scheduled-fire", "--fire-key", "/owner/keys/3-xZ.json", "--stage", "3",
        ])
        for stage in chain.DRIVEN_STAGES:
            argv = chain.stage_command(stage, pathlib.Path("/k.json"))
            self.assertIn("--scheduled-fire", argv)
            self.assertNotIn("--live-run", argv)
            self.assertEqual(argv[argv.index("--stage") + 1], str(stage))

    def test_contract_and_job_pins_agree_with_the_code(self) -> None:
        contract = load_json(CONTRACTS)
        jobs = load_json(JOBS)
        job = next(entry for entry in jobs["jobs"] if entry["id"] == "ge16-chain")
        binding = next(entry for entry in contract["cron_contracts"]
                       if entry["id"] == "ge16-cron-chain-orchestrator")
        self.assertEqual(job["chain_state_file"], str(chain.STATE_PATH))
        self.assertEqual(binding["scheduler_binding"]["migration_job_id"], "ge16-chain")
        self.assertEqual(binding["scheduler_binding"]["schedule"], "0 22 * * 5")
        self.assertEqual(binding["deterministic_validation_commands"],
                         ["python3 OPS/cron/ge16_chain_runner.py --status"])
        self.assertEqual(job["writes"], ["OPS/logs/chain"])
        self.assertTrue(job["prompt"].startswith(sync_jobs.NOOP_STAGE_PREFIX))
        self.assertEqual(job["enabled"], False)
        self.assertEqual(job["non_blocking"], False)
        self.assertEqual(binding["gate_records_required"], ["chain-run-complete-recorded"])
        self.assertEqual(binding["scheduler_binding"]["dependencies"], [])
        runner = ROOT / "cron" / "ge16_chain_runner.py"
        self.assertTrue(runner.is_file())
        for stage in chain.CHAIN_ORDER:
            self.assertEqual(chain.HANDOFF_FILES[stage].relative_to(chain.REPO_ROOT).as_posix(),
                             "%s/manifest/%s" % (run_stage.HANDOFFS[stage][0], run_stage.HANDOFFS[stage][1]))

    def test_anchor_is_the_friday_2200_malaysia_slot(self) -> None:
        friday_night = chain.parse_stamp("2026-09-25T14:00:00Z")   # 22:00 MYT Friday
        self.assertEqual(chain.anchor_for(friday_night), "2026-09-25T2200+0800")
        self.assertEqual(chain.anchor_for(friday_night + chain.dt.timedelta(hours=3)),
                         "2026-09-25T2200+0800")
        self.assertEqual(chain.anchor_for(friday_night - chain.dt.timedelta(hours=1)),
                         "2026-09-18T2200+0800")

    # -- run lifecycle -----------------------------------------------------
    def test_new_run_snapshots_baselines_and_starts_every_stage_pending(self) -> None:
        self.harness.write_manifest(1, run_id="last-week")
        self.harness.write_manifest(2, run_id="last-week")
        state = self.harness.runner.start()
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["stop_reason"], None)
        self.assertEqual(state["run_id"], "2026-09-25T2200+0800")
        self.assertEqual(set(state["steps"]), {"1", "2", "3", "4", "5"})
        for stage in chain.CHAIN_ORDER:
            step = state["steps"][str(stage)]
            self.assertEqual(step["status"], "pending")
            self.assertEqual(step["attempts"], 0)
            self.assertEqual(step["mode"], "agent" if stage in chain.AGENT_STAGES else "runner")
        self.assertEqual(state["baseline"]["1"], digest_of(self.harness.handoffs[1]))
        self.assertEqual(state["baseline"]["5"], None)

    def test_agent_stage_one_then_mint_and_launch_the_chain_in_order(self) -> None:
        runner = self.harness.runner
        runner.start()
        runner.poll()
        self.assertEqual([step["status"] for step in runner.status()["steps"].values()][:2], ["awaiting", "pending"])
        self.assertEqual(self.harness.mints, [], "the daemon never mints before Stage 1 completes")

        stage1 = self.harness.write_manifest(1)
        runner.poll()
        state = runner.status()
        self.assertEqual(state["steps"]["1"]["status"], "complete")
        self.assertEqual(state["steps"]["1"]["manifest_sha256"], stage1)
        self.assertEqual([spec["stage"] for spec in self.harness.mints], [2])
        self.assertEqual(self.harness.mints[0]["phase"], None)
        self.assertEqual(self.harness.mints[0]["predecessor_sha256"], stage1)
        self.assertEqual(len(self.harness.launches), 1)
        argv = self.harness.launches[0]["command"]
        self.assertEqual(argv[argv.index("--contract") + 1], "ge16-cron-analytics-forecast-reports-social")
        self.assertEqual(argv[argv.index("--version") + 1], "1.4.0")
        self.assertEqual(argv[argv.index("--stage") + 1], "2")
        self.assertTrue(argv[argv.index("--fire-key") + 1].startswith(str(self.harness.runner.key_directory)))

        stage2 = self.harness.write_manifest(2, predecessor=stage1)
        self.harness.exit_process()
        runner.poll()
        self.assertEqual(runner.status()["steps"]["2"]["status"], "complete")
        self.assertEqual(runner.status()["steps"]["3"]["status"], "running")
        self.assertEqual([spec["stage"] for spec in self.harness.mints], [2, 3])
        self.assertEqual(self.harness.mints[1]["predecessor_sha256"], stage2)

        stage3 = self.harness.write_manifest(3, predecessor=stage2)
        self.harness.exit_process()
        runner.poll()
        self.assertEqual(runner.status()["steps"]["3"]["status"], "complete")
        self.assertEqual([spec["stage"] for spec in self.harness.mints], [2, 3, 4])
        self.assertEqual(self.harness.mints[2]["predecessor_sha256"], stage3)
        self.assertEqual(runner.status()["status"], "running")
        self.assertEqual(self.harness.launches[-1]["command"][-2:], ["--stage", "4"])

    def test_completed_stage_is_never_re_executed_and_never_re_minted(self) -> None:
        runner = self.harness.runner
        runner.start()
        self.harness.write_manifest(1)          # produced while the chain was waiting
        runner.poll()
        self.assertEqual(runner.status()["steps"]["1"]["status"], "complete")
        self.assertEqual(runner.status()["steps"]["1"]["attempts"], 0)
        self.assertEqual([spec["stage"] for spec in self.harness.mints], [2])

        for _ in range(3):
            runner.poll()
        self.assertEqual([spec["stage"] for spec in self.harness.mints], [2],
                         "a live launched stage is waited on, never re-minted")
        self.assertEqual(len(self.harness.launches), 1)

    def test_predecessor_handoff_from_before_the_run_never_advances(self) -> None:
        runner = self.harness.runner
        self.harness.write_manifest(1, run_id="last-week")
        runner.start()
        runner.poll()
        runner.poll()
        self.assertEqual(runner.status()["steps"]["1"]["status"], "awaiting")
        self.assertEqual(self.harness.mints, [])
        self.assertEqual(self.harness.launches, [])

    def test_unbound_predecessor_reference_does_not_count_as_complete(self) -> None:
        runner = self.harness.runner
        self.harness.run_through(upto=1)
        self.assertEqual(runner.status()["steps"]["1"]["status"], "complete")
        self.harness.write_manifest(2, predecessor="f" * 64)   # wrong predecessor hash
        runner.poll()
        self.assertEqual(runner.status()["steps"]["2"]["status"], "running")
        self.assertEqual(runner.status()["steps"]["2"]["manifest_sha256"], None)

    def test_runner_exit_without_handoff_stops_the_chain(self) -> None:
        runner = self.harness.runner
        self.harness.run_through(upto=1)
        self.assertEqual(runner.status()["steps"]["2"]["status"], "running")
        self.harness.exit_process()
        runner.poll()
        state = runner.status()
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["stop_reason"], "stage-2-runner-exited-without-handoff")
        self.assertEqual([spec["stage"] for spec in self.harness.mints], [2], "no advance after a failure")
        polls, launches = state["polls"], len(self.harness.launches)
        for _ in range(3):
            runner.poll()
        self.assertEqual(runner.status()["status"], "failed")
        self.assertEqual(len(self.harness.launches), launches)
        self.assertEqual([spec["stage"] for spec in self.harness.mints], [2])
        self.assertGreater(runner.status()["polls"], polls, "polling keeps a heartbeat but changes nothing")
        self.assertTrue(any("failed" in line for line in self.harness.logged))

    def test_stage_five_completion_finalizes_the_run_and_records_the_gate(self) -> None:
        runner = self.harness.runner
        stage4 = self.harness.run_through(upto=4)
        state = runner.status()
        self.assertEqual(state["steps"]["4"]["status"], "complete")
        self.assertEqual(state["steps"]["5"]["status"], "awaiting")
        self.assertEqual([spec["stage"] for spec in self.harness.mints], [2, 3, 4],
                         "Stage 5 is never driven by the daemon")
        self.harness.write_manifest(5, predecessor=stage4)
        runner.poll()
        state = runner.status()
        self.assertEqual(state["status"], "complete")
        self.assertEqual(state["stop_reason"], None)
        evidence = load_json(pathlib.Path(runner.evidence_dir) / ("ge16-chain-%s.json" % state["run_id"]))
        self.assertEqual(evidence["status"], "complete")
        self.assertEqual(evidence["owner_domain"], "OPS")
        self.assertEqual(evidence["writes"], ["OPS/logs/chain"])
        self.assertEqual(evidence["gate_records"][0]["gate_id"], "chain-run-complete-recorded")
        self.assertEqual(evidence["gate_records"][0]["status"], "passed")

    def test_stage_five_wait_is_bounded_by_the_run_deadline(self) -> None:
        runner = self.harness.runner
        self.harness.run_through(upto=4)
        for _ in range(2):
            self.harness.advance_clock(hours=3)
            runner.poll()
        self.assertEqual(runner.status()["status"], "running")
        self.assertEqual(runner.status()["steps"]["5"]["status"], "awaiting")
        self.assertEqual(self.harness.mints[-1]["stage"], 4)

    def test_run_deadline_guard_fails_without_launching_anything(self) -> None:
        runner = self.harness.runner
        runner.start()
        self.harness.advance_clock(hours=13)
        runner.poll()
        state = runner.status()
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["stop_reason"], "run-deadline-exceeded")
        self.assertEqual(self.harness.mints, [])

    def test_agent_stage_window_failure_stops_the_chain(self) -> None:
        runner = self.harness.runner
        runner.start()
        runner.poll()
        self.harness.advance_clock(minutes=241)
        runner.poll()
        self.assertEqual(runner.status()["status"], "failed")
        self.assertEqual(runner.status()["stop_reason"], "stage-1-handoff-missing-within-window")
        self.assertEqual(self.harness.mints, [])

    def test_restart_resumes_at_the_first_uncompleted_and_a_new_anchor_starts_fresh(self) -> None:
        runner = self.harness.runner
        self.harness.run_through(upto=3)
        anchor = runner.status()["run_id"]
        self.assertEqual([spec["stage"] for spec in self.harness.mints], [2, 3, 4])

        resumed = runner.start()                       # same anchor: resume, never re-baseline
        self.assertEqual(resumed["run_id"], anchor)
        self.assertEqual([resumed["steps"][str(stage)]["status"] for stage in (1, 2, 3)],
                         ["complete", "complete", "complete"])
        self.assertEqual(resumed["steps"]["4"]["status"], "running")

        self.harness.advance_clock(days=7)
        fresh = runner.start()                         # next Friday: a brand-new run
        self.assertEqual(fresh["run_id"], "2026-10-02T2200+0800")
        self.assertEqual([fresh["steps"][str(stage)]["status"] for stage in chain.CHAIN_ORDER],
                         ["pending"] * 5)
        self.assertEqual(fresh["baseline"], {str(stage): chain.sha256_file(self.harness.handoffs[stage])
                                             for stage in chain.CHAIN_ORDER})

    def test_poll_without_a_run_is_inert(self) -> None:
        self.assertIsNone(self.harness.runner.poll())
        self.assertEqual(self.harness.mints, [])
        self.assertEqual(list(pathlib.Path(self.harness.runner.evidence_dir).glob("*")), [])

    # -- real owner-side effects, exercised with stubs ----------------------
    def test_stage_one_keys_are_surfaced_once_per_phase_and_never_over_minted(self) -> None:
        runner = self.harness.runner
        runner.start()                                   # the on-demand chain trigger
        surfacing = self.harness.stage_one_mints
        self.assertEqual([spec["phase"] for spec in surfacing], ["a", "b"])
        self.assertEqual([spec["ttl_minutes"] for spec in surfacing], [chain.KEY_TTL_MINUTES] * 2)
        self.assertTrue(all(spec["trigger"] == "chain-start" for spec in surfacing))
        self.assertTrue(all(pathlib.Path(spec["key_file"]).parent == self.harness.root / "keys"
                            for spec in surfacing))
        self.assertEqual(sorted(pathlib.Path(spec["key_file"]).name[:4] for spec in surfacing),
                         ["1-a-", "1-b-"])
        self.assertEqual(self.harness.mints, [])
        for _ in range(3):                               # three more polls: no over-minting
            runner.poll()
        self.assertEqual(len(self.harness.stage_one_mints), 2)
        self.assertEqual(runner.status()["steps"]["1"]["status"], "awaiting")

    def test_real_mint_refuses_agent_driven_stages_and_uses_the_owner_pty_path(self) -> None:
        key_file = self.harness.root / "keys" / "5-xZ.json"
        for spec in ({"stage": 5, "phase": None}, {"stage": 5, "phase": "a"}, {"stage": 2, "phase": "b"}):
            with self.subTest(spec=spec):
                with self.assertRaisesRegex(ValueError, "single-phase driven stages"):
                    chain.mint_key(dict(spec, key_file=str(key_file)))

        payload = {"payload": {"key_id": "k-1", "expires_at_utc": "2026-09-25T14:14:00Z"}}
        completed = mock.Mock(returncode=0)
        with mock.patch.object(chain.subprocess, "run", return_value=completed) as run:
            with mock.patch.object(pathlib.Path, "exists", return_value=True), \
                    mock.patch.object(pathlib.Path, "read_text", return_value=json.dumps(payload)):
                minted = chain.mint_key({"stage": 2, "phase": None, "key_file": str(key_file)},
                                        key_directory=self.harness.root / "keys")
        self.assertEqual(minted["key_id"], "k-1")
        self.assertEqual(minted["expires_at_utc"], "2026-09-25T14:14:00Z")
        self.assertEqual(run.call_args.args[0], [
            "/usr/bin/script", "-q", "/dev/null", "/usr/bin/python3", str(chain.MINT_SCRIPT),
            "--stage", "2", "--ttl-minutes", str(chain.KEY_TTL_MINUTES),
            "--out", str(key_file), "--confirm-owner",
        ])
        self.assertEqual(run.call_args.kwargs["env"], chain.CHILD_ENVIRONMENT)
    def test_real_launch_is_detached_and_pins_the_child_environment(self) -> None:
        process = mock.Mock(pid=4242)
        log_path = self.harness.root / "logs" / "stage-2.log"
        with mock.patch.object(chain.subprocess, "Popen", return_value=process) as popen:
            pid = chain.launch_stage(["/usr/bin/python3", "x"], log_path)
        self.assertEqual(pid, 4242)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(popen.call_args.kwargs["env"], chain.CHILD_ENVIRONMENT)
        self.assertEqual(popen.call_args.kwargs["cwd"], str(chain.REPO_ROOT))
        self.assertEqual(popen.call_args.args[0], ["/usr/bin/python3", "x"])

    def test_mint_failure_stops_the_chain_without_a_key(self) -> None:
        runner = self.harness.runner
        runner.start()
        self.harness.write_manifest(1)
        self.harness.mint_error = "owner key mint refused"
        runner.poll()
        state = runner.status()
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["stop_reason"], "stage-2-key-mint-failed")
        self.assertEqual(state["steps"]["2"]["status"], "pending")
        self.assertEqual(self.harness.launches, [], "no key, no launch")
        for _ in range(2):
            runner.poll()
        self.assertEqual(len(self.harness.mints), 1, "a failed run never re-mints")
        self.assertEqual(self.harness.launches, [])

    # -- CLI ---------------------------------------------------------------
    def test_cli_start_poll_status_and_single_flight(self) -> None:
        state_path = self.harness.root / "cli-state.json"
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(chain.main(["--status", "--state", str(state_path)]), 0)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "no-run")

        arguments = ["--start", "--state", str(state_path), "--evidence-dir",
                     str(self.harness.root / "cli-evidence")]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(chain.main(list(arguments)), 0)
        state = load_json(state_path)
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["run_id"], chain.anchor_for(chain.utc_now()))

        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(chain.main(["--poll", "--state", str(state_path), "--evidence-dir",
                                         str(self.harness.root / "cli-evidence")]), 0)
        self.assertEqual(json.loads(stdout.getvalue())["steps"]["1"], "awaiting")

        before = state_path.read_bytes()
        lock = open(state_path.with_suffix(".lock"), "w")
        try:
            chain.fcntl.flock(lock, chain.fcntl.LOCK_EX | chain.fcntl.LOCK_NB)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(chain.main(["--poll", "--state", str(state_path)]), 0)
        finally:
            chain.fcntl.flock(lock, chain.fcntl.LOCK_UN)
            lock.close()
        self.assertEqual(state_path.read_bytes(), before, "a poll blocked by the lock changes nothing")

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                chain.main(["--state", str(state_path)])


if __name__ == "__main__":
    unittest.main()
