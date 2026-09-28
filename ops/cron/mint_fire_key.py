#!/usr/bin/env python3
"""Owner-terminal Ed25519 minting. Never called by the runner or cron agent."""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import pathlib
import pwd
import shlex
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from cron import run_stage, ed25519_support as ed25519

KEYCHAIN_SERVICE = "ge16_owner_ed25519"
KEYCHAIN_ACCOUNT = "faisal.muthalib"


def _require_owner_terminal(confirm_owner: bool) -> None:
    # This is a refusal of accidental cron use, NOT an identity/security boundary:
    # a same-user process can allocate a PTY. The Keychain ACL must gate access.
    if confirm_owner is not True:
        raise ValueError("--confirm-owner is required")
    if pwd.getpwuid(os.geteuid()).pw_name != "faisal.muthalib":
        raise ValueError("minting requires owner faisal.muthalib")
    if not all(os.isatty(fd) for fd in (0, 1, 2)):
        raise ValueError("owner foreground terminal required; cron/non-interactive minting denied")
    try:
        with open("/dev/tty", "rb", buffering=0) as terminal:
            if os.tcgetpgrp(terminal.fileno()) != os.getpgrp():
                raise ValueError("owner foreground terminal required")
    except OSError as error:
        raise ValueError("owner controlling terminal required") from error


def _read_keychain_private(confirm_owner: bool, init_keychain: bool = False) -> bytes:
    _require_owner_terminal(confirm_owner)
    env = {"PATH": "/usr/bin:/bin"}
    # No unlock-keychain, passwords, AppleScript, retries, or ACL bypass. Any OS
    # authentication prompt must be handled by the human at their terminal.
    result = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
         "-a", KEYCHAIN_ACCOUNT, "-w"], capture_output=True, env=env, timeout=60,
    )
    if result.returncode == 44 and init_keychain:  # explicit owner --init only
        der = ed25519.generate_private_der()
        # security -i keeps the secret off process argv. Empty trusted-app list
        # (-T '') avoids implicitly granting /usr/bin/security permanent access.
        command = shlex.join(["add-generic-password", "-s", KEYCHAIN_SERVICE,
                              "-a", KEYCHAIN_ACCOUNT, "-T", "", "-w",
                              base64.b64encode(der).decode("ascii")]) + "\n"
        created = subprocess.run(["/usr/bin/security", "-i"], input=command.encode("ascii"),
                                 capture_output=True, env=env, timeout=60)
        if created.returncode:
            raise ValueError("Keychain provisioning failed; owner must inspect access policy")
        public = ed25519.public_from_private_der(der)
        pem = ("-----BEGIN PUBLIC KEY-----\n" + base64.b64encode(public).decode("ascii") +
               "\n-----END PUBLIC KEY-----\n").encode("ascii")
        raise ValueError("Keychain item initialized; no fire minted. Owner must review ACL and BOTH code/contract pins: "
                         "public_key_b64=" + base64.b64encode(public).decode("ascii")
                         + " key_sha256=" + hashlib.sha256(pem).hexdigest())
    if result.returncode == 44:
        raise ValueError("Keychain item ge16_owner_ed25519 not found; run with --init")
    if result.returncode:
        raise ValueError("Keychain private-key access denied; human terminal authentication required")
    return run_stage._canonical_base64(result.stdout.rstrip(b"\r\n").decode("ascii"), "Keychain private key")


def _sign_payload(payload: bytes, public_der: bytes, confirm_owner: bool, init_keychain: bool = False) -> bytes:
    private_der = _read_keychain_private(confirm_owner, init_keychain)
    # Ephemeral owner-only signing file, removed on success AND every exception.
    # No persistent private key or secret is written anywhere in the repository.
    with tempfile.TemporaryDirectory(prefix="ge16-owner-sign-") as directory:
        os.chmod(directory, 0o700)
        temporary_key = pathlib.Path(directory) / "T"
        descriptor = os.open(temporary_key, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as target:
            os.fchmod(target.fileno(), 0o600)
            target.write(private_der)
        actual_public = ed25519.public_from_private_file(temporary_key)
        if actual_public != public_der:
            raise ValueError("Keychain private key does not match reviewed owner public key")
        return ed25519.sign_private_file(temporary_key, payload)


def mint(stage: int, phase: str | None, ttl_minutes: int, out: pathlib.Path,
         *, confirm_owner: bool = False, init_keychain: bool = False) -> str:
    if type(ttl_minutes) is not int or not 1 <= ttl_minutes <= 15:
        raise ValueError("ttl-minutes must be an integer from 1 through 15")
    if type(stage) is not int or stage not in (1, 2, 3, 4):
        raise ValueError("only stages 1-4 may receive scheduled fire keys")
    _require_owner_terminal(confirm_owner)
    run_stage._validate_fire_path(out)
    if out.exists():
        raise ValueError("out must be a new absolute external file; refusing overwrite")
    _, _, contract_id, version = run_stage.HANDOFFS[stage]
    contract = run_stage._load_validated_request(contract_id, version)
    if stage == 1 and phase is None:
        phase = "a"
    binding = run_stage._scheduled_fire_binding(contract, stage, phase)
    clause = run_stage.validate_bounded_live_execution(json.loads(run_stage.CONTRACT_PATH.read_text()))
    public_der = run_stage._read_owner_public_key(clause)
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    stamp = lambda value: value.isoformat().replace("+00:00", "Z")
    payload = {**binding, "key_id": str(uuid.uuid4()), "approved_at_utc": stamp(now),
               "expires_at_utc": stamp(now + dt.timedelta(minutes=ttl_minutes))}
    canonical = run_stage._canonical_json_bytes(payload)
    signature = _sign_payload(canonical, public_der, confirm_owner, init_keychain)
    envelope = {"payload": payload, "payload_b64": base64.b64encode(canonical).decode("ascii"),
                "signature": base64.b64encode(signature).decode("ascii")}
    raw = (json.dumps(envelope, indent=2) + "\n").encode("utf-8")
    run_stage._validate_fire_path(out)
    descriptor = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as target:
        os.fchmod(target.fileno(), 0o600)
        target.write(raw)
        target.flush()
        os.fsync(target.fileno())
    run_stage._validate_scheduled_fire(out, contract, stage, phase)
    return hashlib.sha256(raw).hexdigest()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=int, required=True, choices=(1, 2, 3, 4))
    parser.add_argument("--phase", choices=("a", "b"))
    parser.add_argument("--ttl-minutes", type=int, required=True)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    parser.add_argument("--confirm-owner", action="store_true", required=True)
    parser.add_argument("--init", action="store_true",
                        help="first-run only: provision the owner Ed25519 key into the macOS Keychain; later runs must omit this")
    args = parser.parse_args(argv)
    try:
        digest = mint(args.stage, args.phase, args.ttl_minutes, args.out,
                      confirm_owner=args.confirm_owner, init_keychain=args.init)
    except (OSError, ValueError, subprocess.SubprocessError, run_stage.ContractError, run_stage.SyncError) as error:
        print("mint-fire-key: " + str(error), file=sys.stderr)
        return 1
    print(digest + "  " + str(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
