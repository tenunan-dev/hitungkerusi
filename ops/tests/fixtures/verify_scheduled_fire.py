#!/usr/bin/env python3
"""Replay attacks and scheduler races using temporary registries and fixture keys."""
import base64
import contextlib
import io
import json
import pathlib
import sys
from unittest import mock

OPS = pathlib.Path(__file__).resolve().parents[2]
sys.path[:0] = [str(OPS), str(OPS / "tests")]
from cron import mint_fire_key, run_stage
from test_scheduled_fire import ScheduledFireTests
import test_isolated_stage_adapters as fixtures


def attack(label, action):
    fixture = ScheduledFireTests("test_mint_to_scheduled_cli_happy_path_and_replay")
    try:
        fixture.setUp()
        action(fixture)
        print(label + ": BLOCKED")
    finally:
        fixture.doCleanups()


def bit_flip(f):
    envelope = json.loads(f.fire.read_text())
    signature = bytearray(base64.b64decode(envelope["signature"]))
    signature[0] ^= 1
    envelope["signature"] = base64.b64encode(signature).decode("ascii")
    f.fire.write_text(json.dumps(envelope))
    f.rejected()


def lifetime(f):
    original = f.fire.read_bytes()
    now = run_stage.dt.datetime.now(run_stage.dt.timezone.utc).replace(microsecond=0)
    stamp = lambda value: value.isoformat().replace("+00:00", "Z")
    for start, end in ((0, 901), (-700, -1)):
        f.fire.write_bytes(original)
        f.resign(lambda p: p.update(approved_at_utc=stamp(now + run_stage.dt.timedelta(seconds=start)),
                                   expires_at_utc=stamp(now + run_stage.dt.timedelta(seconds=end))))
        f.rejected()
        print("  lifetime=%ds expiry_offset=%ds: BLOCKED" % (end - start, end))


def cross_job(f):
    f.resign(lambda p: p.update(scheduler_job_id=run_stage.BOUNDED_LIVE_POLICY["scheduler_bindings"][1]["id"]))
    f.rejected()


def replay(f):
    # Exact reviewer setup: successful runner dispatch, adapter mocked. No domains run.
    with mock.patch.object(run_stage, "_run_isolated", return_value={"fixture": True}) as child:
        status, record = fixtures.invoke(*f.args())
    f.assertEqual((0, "completed"), (status, record["result"]))
    child.assert_called_once()
    with mock.patch.object(run_stage, "_run_isolated") as child:
        status, record = fixtures.invoke(*f.args())
    f.assertEqual(1, status)
    child.assert_not_called()


def race_replay(f):
    key = f.fire.read_bytes()
    before = f.validate_fire()
    old_hash = run_stage._sha256(f.registry_path)
    print("  PRE: minted key accepted before scheduler fire_claim mutation: PASS")
    f.mutate_job(lambda job: job.update(fire_claim={"token": "scheduler-fire"}))
    f.assertNotEqual(old_hash, run_stage._sha256(f.registry_path))
    print("  OLD whole-file binding: pre/post hashes differ; key would be rejected: PASS")
    f.assertEqual(before, f.validate_fire())
    print("  POST: SAME key accepted after fire_claim mutation, identity unchanged: PASS")

    def adapter(contract, root, stage, **kwargs):
        f.mutate_job(lambda job: job.update(fire_claim={"token": "mid-run"}, last_status="running"))
        kwargs["live_guard"]()
        return "fixture-race"

    with mock.patch.object(run_stage, "_run_isolated", side_effect=adapter) as child:
        status, record = fixtures.invoke(*f.args())
    f.assertEqual((0, "completed"), (status, record["result"]))
    child.assert_called_once()
    f.assertEqual(key, f.fire.read_bytes())
    claim = f.root / "1_DATA" / run_stage.LIVE_CLAIMS_DIRECTORY / before
    f.assertEqual("consumed\n", claim.read_text())
    print("  MID-RUN: fire_claim and last_status changed; guard accepted: PASS")
    print("  One-shot claim consumed for original identity: PASS")
    with mock.patch.object(run_stage, "_run_isolated") as child:
        f.assertEqual("bounded-live-validation-failed", f.rejected()["reason"])
    child.assert_not_called()
    with f.assertRaisesRegex(ValueError, "already consumed"):
        run_stage._claim_live_authorization(f.root, "1_DATA", before)
    print("  Replay of consumed key rejected before dispatch: PASS")


def freshmint(f):
    registry = f.registry_path.read_bytes()
    changes = {"fire_claim": {"token": "after-mint"}, "id": "attacker-id",
               "name": "attacker-name", "schedule": {"kind": "cron", "expr": "* * * * *"},
               "model": "attacker-model", "provider": "attacker-provider",
               "state": "paused", "workdir": "/private/tmp/escape",
               "context_from": "attacker-context", "enabled_toolsets": ["all"],
               "prompt": "attacker prompt", "skills": ["all"], "skill": "attacker-skill",
               "script": "attacker.py", "no_agent": True,
               "base_url": "https://attacker.invalid",
               "monitor_script": "attacker-monitor.py",
               "monitor_url": "https://attacker.invalid/monitor",
               "origin": "attacker-origin", "enabled": False}
    for field, value in changes.items():
        f.registry_path.write_bytes(registry)
        f.fire = f.fire.with_name("fresh-%s.json" % field)
        with contextlib.redirect_stdout(io.StringIO()):
            status = mint_fire_key.main(["--confirm-owner", "--stage", "1", "--phase", "b",
                                         "--ttl-minutes", "10", "--out", str(f.fire)])
        f.assertEqual(0, status)
        key = f.fire.read_bytes()
        identity = f.validate_fire()
        f.mutate_job(lambda job: job.update({field: value}))
        with mock.patch.object(run_stage, "_run_isolated", return_value="fixture-freshmint") as child:
            status, record = fixtures.invoke(*f.args())
        if field == "fire_claim":
            f.assertEqual((0, "completed"), (status, record["result"]))
            child.assert_called_once()
            f.assertEqual("consumed\n", (f.root / "1_DATA" / run_stage.LIVE_CLAIMS_DIRECTORY / identity).read_text())
            print("  mint_fire_key.py -> change fire_claim -> ACCEPTED, claim consumed: PASS")
        else:
            f.assertEqual(1, status)
            child.assert_not_called()
            f.assertFalse((f.root / "1_DATA" / run_stage.LIVE_CLAIMS_DIRECTORY / identity).exists())
            print("  mint_fire_key.py -> change %s -> REJECTED before claim/dispatch: PASS" % field)
        f.assertEqual(key, f.fire.read_bytes())


def check(label, action):
    fixture = ScheduledFireTests("test_mint_to_scheduled_cli_happy_path_and_replay")
    try:
        fixture.setUp()
        print(label)
        action(fixture)
    finally:
        fixture.doCleanups()


if __name__ == "__main__":
    print("Temporary fixtures only; Keychain/owner terminal mocked; adapter dispatch mocked.")
    print("No real domain or live registry writes.")
    mode = sys.argv[1:]
    if mode not in ([], ["--race"], ["--freshmint"]):
        raise SystemExit("usage: verify_scheduled_fire.py [--race|--freshmint]")
    if mode in ([], ["--race"]):
        check("V3 structural race replay", race_replay)
    if mode in ([], ["--freshmint"]):
        check("V4 freshmint post-mint job mutations", freshmint)
        check("Exact digest whitelist and extra-key normalization: PASS on completion",
              lambda f: f.test_job_digest_exact_whitelist_and_canonical_normalization())
        attack("i signature bit-flip", bit_flip)
        attack("ii excessive and expired lifetime, validly re-signed", lifetime)
        attack("iii Stage-2 job ID in Stage-1 envelope, validly re-signed", cross_job)
        attack("iv replay after successful fixture dispatch", replay)
