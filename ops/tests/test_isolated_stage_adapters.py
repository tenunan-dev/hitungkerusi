"""Acceptance tests for the opt-in isolated five-stage adapters."""

from __future__ import annotations

import contextlib
import copy
import ast
import io
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

from cron import run_stage


ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA_CONTRACT = "ge16-cron-data-collection-validation"
ANALYTICS_CONTRACT = "ge16-cron-analytics-forecast-reports-social"
OUTPUTS_CONTRACT = "ge16-cron-outputs-intake-sealing"
DELIVERY_CONTRACT = "ge16-cron-delivery-publish-validation"
WEBSITES_CONTRACT = "ge16-cron-websites-build-release-gate"
VERSION = "1.4.0"
WEBSITES_VERSION = "1.5.0"
MANIFEST_FIELDS = {
    "run_id", "contract_id", "contract_version", "owner_domain",
    "input_manifest_refs", "output_manifest_refs", "created_at_utc",
}
GATE_FIELDS = {"run_id", "gate_id", "status", "checked_at_utc", "evidence_ref"}


def data_phase(contract, phase="b"):
    """Stage 1 is two-phase; every other stage's contract takes no --phase."""
    return ["--phase", phase] if contract == DATA_CONTRACT else []


def native_sandbox_available():
    """Probe capability only; this never invokes a domain adapter."""
    if not pathlib.Path("/usr/bin/sandbox-exec").is_file():
        return False
    result = subprocess.run(["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)",
                             "/usr/bin/true"], capture_output=True, text=True)
    return result.returncode == 0


NATIVE_SANDBOX_AVAILABLE = native_sandbox_available()


class FixtureSandboxTests(unittest.TestCase):
    """When nested Seatbelt is prohibited, test the audit hook and profile.

    This is a test-only subprocess substitution, never a production fallback.
    Tests that need the native fence are explicitly skipped in that environment.
    """

    def setUp(self):
        if NATIVE_SANDBOX_AVAILABLE:
            return
        real_run = subprocess.run

        def fixture_subprocess(command, **kwargs):
            if command[0] == "git":
                # _parent_git currently emits this exact prefix, not a marker.
                # Only parent reads may reach a real process in this fixture.
                root = run_stage._validated_isolated_root(pathlib.Path(kwargs["cwd"]))
                repository = pathlib.Path(command[2])
                self.assertIn(repository, [root / domain for domain in run_stage.DOMAIN_NAMES[:4]])
                self.assertEqual([
                    "git", "-C", str(repository), "-c", "core.hooksPath=/dev/null",
                    "-c", "commit.gpgsign=false", "-c", "core.fsmonitor=false",
                    "-c", "core.attributesFile=/dev/null", "-c", "core.fsmonitorHookVersion=0",
                ], command[:13])
                policy = next(node for node in ast.parse(run_stage._AUDITED_PYTHON).body
                              if isinstance(node, ast.FunctionDef) and node.name == "readonly_git")
                scope = {"network": "ro-git", "root": root, "pathlib": pathlib}
                exec(compile(ast.Module(body=[policy], type_ignores=[]), "<fixture-readonly-git>", "exec"), scope)
                self.assertTrue(scope["readonly_git"]("git", ["git", *command[13:]]),
                                "fixture refuses parent Git mutation or escape: " + repr(command[13:]))
                return real_run(command, **kwargs)
            self.assertEqual("/usr/bin/sandbox-exec", command[0])
            profile = pathlib.Path(command[2]).read_text()
            root, owner = pathlib.Path(command[8]), command[9]
            self.assertIn('(deny default)', profile)
            self.assertIn('(allow file-write* (subpath %s))' % json.dumps(str(root / owner)), profile)
            self.assertNotIn('(allow file-write* (subpath %s))' % json.dumps(str(root)), profile)
            self.assertIn('(deny file-write* (subpath %s))' % json.dumps(str(root / owner / ".bounded-live-claims")), profile)
            return real_run(command[3:], **kwargs)

        patch = mock.patch.object(run_stage.subprocess, "run", side_effect=fixture_subprocess)
        patch.start()
        self.addCleanup(patch.stop)


class RealParentFixtureSandboxTests(FixtureSandboxTests):
    """Run actual parent Git mutations, exclusively in validated temp roots.

    Child fallback retains every audit/profile assertion from the base harness.
    Native-fence attacks must never use this fallback as evidence of Seatbelt.
    """
    def setUp(self):
        real_run = subprocess.run
        super().setUp()
        child_run = subprocess.run

        def fixture_process(command, **kwargs):
            if command[0] != "git":
                return child_run(command, **kwargs)
            root = run_stage._validated_isolated_root(pathlib.Path(kwargs["cwd"]))
            self.assertIn(pathlib.Path(command[2]), [root / domain for domain in run_stage.DOMAIN_NAMES[:4]])
            self.assertEqual(command[:13], [
                "git", "-C", command[2], "-c", "core.hooksPath=/dev/null",
                "-c", "commit.gpgsign=false", "-c", "core.fsmonitor=false",
                "-c", "core.attributesFile=/dev/null", "-c", "core.fsmonitorHookVersion=0",
            ])
            for key, expected in {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
                                  "GIT_CONFIG_NOSYSTEM": "1", "GIT_ATTR_NOSYSTEM": "1"}.items():
                self.assertEqual(expected, kwargs["env"][key])
            self.assertNotIn("GIT_NO_REPLACE_OBJECTS", kwargs["env"])
            self.assertIn(command[13], {"init", "add", "commit", "rev-parse", "status", "diff", "log", "ls-tree"})
            return real_run(command, **kwargs)

        patch = mock.patch.object(run_stage.subprocess, "run", side_effect=fixture_process)
        patch.start()
        self.addCleanup(patch.stop)


def assert_execution_imports(test, tree):
    # The existing single top-level subprocess import supplies the two approved
    # subprocess.run sites. No added/aliased/nested execution imports are allowed.
    approved = [node for node in tree.body if isinstance(node, ast.Import)
                and len(node.names) == 1 and node.names[0].name == "subprocess"
                and node.names[0].asname is None]
    test.assertEqual(1, len(approved))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)) and node not in approved:
            names = [alias.name.split(".")[0] for alias in node.names]
            if isinstance(node, ast.ImportFrom):
                names.append((node.module or "").split(".")[0])
            test.assertTrue({"subprocess", "_posixsubprocess", "asyncio"}.isdisjoint(names))
        if isinstance(node, ast.Call):
            test.assertFalse(isinstance(node.func, ast.Name) and node.func.id == "_posixsubprocess")
            if isinstance(node.func, ast.Attribute):
                value = node.func.value
                while isinstance(value, ast.Attribute):
                    value = value.value
                if isinstance(value, ast.Name) and value.id == "os":
                    test.assertNotIn(node.func.attr, {"system", "fork", "forkpty", "posix_spawn", "spawn", "spawnl", "spawnle", "spawnv", "spawnve"})


def invoke(*args):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        status = run_stage.main(args)
    return status, json.loads(output.getvalue())


def write_script(root: pathlib.Path, relative: str, body: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("import pathlib\n" + body + "\n", encoding="utf-8")


def write_stage_five_authorization(root: pathlib.Path, authorization: dict) -> None:
    """Write the exact structured evidence referenced by a fixture authorization."""
    for gate in authorization["gate_evidence"]:
        evidence = run_stage.fixture_stage_five_evidence(
            authorization["delivery_id"], authorization["delivery_manifest_sha256"],
            authorization["vercel_git_sha"], gate["gate_id"], gate["status"],
        )
        relative = gate["evidence_ref"].partition("#")[0]
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(run_stage._canonical_json_bytes(evidence))
    readback = authorization["vercel_readback_evidence"]
    evidence = run_stage.fixture_stage_five_evidence(
        authorization["delivery_id"], authorization["delivery_manifest_sha256"],
        authorization["vercel_git_sha"], "vercel-post-deploy-readback-recorded", readback["status"],
    )
    relative = readback["evidence_ref"].partition("#")[0]
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(run_stage._canonical_json_bytes(evidence))
    (root / "5_WEBSITES/release-authorization.json").write_text(json.dumps(authorization), encoding="utf-8")


def fixture_root() -> tempfile.TemporaryDirectory:
    temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
    root = pathlib.Path(temporary.name)
    (root / "1_DATA").mkdir()
    (root / "2_ANALYTICS").mkdir()
    (root / "3_OUTPUTS").mkdir()
    (root / "4_DELIVERY").mkdir()
    (root / "5_WEBSITES").mkdir()
    (root / "1_DATA" / "canonical-data-provenance.json").write_text(
        '{"fixture":"current"}\n', encoding="utf-8"
    )
    trackers = root / "1_DATA/research/trackers"
    trackers.mkdir(parents=True, exist_ok=True)
    # Fresh by construction: evidence that phase a already ran this cycle.
    (trackers / "ge16-news-candidates.json").write_text(
        '{"generated_at":"fixture","count":0,"items":[]}\n', encoding="utf-8"
    )
    # Judged output for this cycle: phase b refuses to publish a cycle where
    # judging never happened. Non-empty so the default fixtures exercise the
    # publish path; the no-acceptance path has its own dedicated test.
    (trackers / "ge16-news-judged.json").write_text(
        '{"accepted":[{"title":"fixture judged item"}]}\n', encoding="utf-8"
    )
    # Phase-a completion receipt bound to both digests, as phase a itself
    # would write it. Phase b verifies these digests before publishing.
    (root / "1_DATA" / "manifest").mkdir(parents=True, exist_ok=True)
    receipt = {
        "stage": 1,
        "phase": "a",
        "candidates_sha256": run_stage._sha256(
            trackers / "ge16-news-candidates.json"
        ),
        "judged_sha256": run_stage._sha256(trackers / "ge16-news-judged.json"),
    }
    (root / run_stage.STAGE_ONE_RECEIPT_PATH).write_text(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    write_script(root, "1_DATA/scripts/validate_canonical_data.py", "print('data validated')")
    for script in run_stage.COMMAND_ALLOWLIST:
        write_script(root, run_stage.COMMAND_ALLOWLIST[script][0], "print('analytics validated')")
    release_fixture = (ROOT / "tests/fixtures/release_adapter.py").read_text(encoding="utf-8")
    for name in ("outputs-intake-seal", "delivery-publish"):
        write_script(root, run_stage.COMMAND_ALLOWLIST[name][0], release_fixture)
    for relative in run_stage.PINNED_MODULE_PATHS:
        write_script(root, relative, "# Fixture pinned release module")
    return temporary


class IsolatedStageAdapterTests(RealParentFixtureSandboxTests):
    def test_hashed_data_validator_imports_its_sibling_under_audited_child(self):
        # Copy the real entry point and helper read-only; --help exercises their
        # imports but exits before canonical validation or canonical file access.
        temporary = fixture_root()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        relative = run_stage.COMMAND_ALLOWLIST["data-validate-canonical"][0]
        script = root / relative
        script.write_bytes((ROOT.parent / relative).read_bytes())
        helper = script.with_name("methodology_contract.py")
        pins = {relative: run_stage._sha256(script)}
        # Neither the working directory nor a caller's PYTHONPATH may supply
        # the missing helper, even when the pinned script parent is enabled.
        (root / helper.name).write_text("raise AssertionError('caller import path used')\n")
        before = run_stage._inventory(root)
        with mock.patch.dict(run_stage.COMMAND_ALLOWLIST, {
            "data-validate-canonical": (relative, "--help"),
        }), mock.patch.dict(os.environ, {"PYTHONPATH": str(root)}):
            with self.assertRaises(run_stage.IsolatedRunError):
                run_stage._run_allowlisted_command(
                    root, "data-validate-canonical", "1_DATA", live_script_hashes=pins,
                )
            helper.write_bytes((ROOT.parent / relative).with_name(helper.name).read_bytes())
            run_stage._run_allowlisted_command(
                root, "data-validate-canonical", "1_DATA", live_script_hashes=pins,
            )
        self.assertEqual(pins[relative], run_stage._sha256(script))
        run_stage._assert_only_owner_changed(before, root, "1_DATA")
        self.assertFalse(run_stage._handoff_path(root, 1).exists())

    def test_plain_invocation_reports_posture_without_execution(self) -> None:
        with mock.patch.object(run_stage, "_run_isolated") as isolated, mock.patch.object(run_stage, "_run_live") as live:
            status, record = invoke("--contract", DATA_CONTRACT, "--version", VERSION)
        isolated.assert_not_called()
        live.assert_not_called()
        self.assertEqual(0, status)
        self.assertEqual({"result": "posture", "mode": "posture-only", "contract_id": DATA_CONTRACT,
                          "contract_version": VERSION, "ops_contract_version": "1.9.0", "lock_retired": True}, record)

    def test_isolated_root_validation_rejects_missing_file_and_live_domain(self) -> None:
        missing = pathlib.Path("/private/tmp/no-such-ge16-isolated-root")
        status, record = invoke("--contract", DATA_CONTRACT, "--version", VERSION, "--dry-run", "--isolated-root", str(missing))
        self.assertEqual(1, status)
        self.assertEqual("validation-error", record["result"])

        # OPS itself is a live control domain, even though it is not one of
        # the five pipeline domains.  A fixture may never overlap it.
        status, record = invoke("--contract", DATA_CONTRACT, "--version", VERSION, "--dry-run", "--isolated-root", str(ROOT))
        self.assertEqual(1, status)
        self.assertEqual("validation-error", record["result"])
        with tempfile.NamedTemporaryFile(dir="/private/tmp") as file_handle:
            status, record = invoke("--contract", DATA_CONTRACT, "--version", VERSION, "--dry-run", "--isolated-root", file_handle.name)
            self.assertEqual(1, status)
            self.assertEqual("validation-error", record["result"])
        status, record = invoke("--contract", DATA_CONTRACT, "--version", VERSION, "--dry-run", "--isolated-root", str(ROOT.parent / "1_DATA"))
        self.assertEqual(1, status)
        self.assertEqual("validation-error", record["result"])

    def test_isolated_root_rejects_domain_symlink(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            root = pathlib.Path(temporary)
            (root / "1_DATA").symlink_to(ROOT.parent / "1_DATA", target_is_directory=True)
            status, record = invoke("--contract", DATA_CONTRACT, "--version", VERSION, "--dry-run", "--isolated-root", temporary)
        self.assertEqual(1, status)
        self.assertEqual("validation-error", record["result"])

    def test_stage_one_phase_a_never_writes_handoff_and_phase_b_emits_it(self) -> None:
        temporary = fixture_root()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        status, record = invoke("--contract", DATA_CONTRACT, "--version", VERSION, "--dry-run", "--isolated-root", str(root), "--stage", "1", "--phase", "a")
        self.assertEqual(0, status, record)
        self.assertFalse((root / "1_DATA/manifest/ge16-data-handoff.json").exists())
        status, record = invoke("--contract", DATA_CONTRACT, "--version", VERSION, "--dry-run", "--isolated-root", str(root), "--stage", "1", "--phase", "b")
        self.assertEqual(0, status, record)
        manifest = json.loads((root / "1_DATA/manifest/ge16-data-handoff.json").read_text(encoding="utf-8"))
        gates = json.loads((root / "1_DATA/manifest/ge16-data-handoff.gates.json").read_text(encoding="utf-8"))
        self.assertEqual(MANIFEST_FIELDS, set(manifest))
        self.assertTrue(manifest["input_manifest_refs"][0].startswith("1_DATA/canonical-data-provenance.json#sha256:"))
        self.assertEqual(3, len(gates))
        self.assertTrue(all(set(gate) == GATE_FIELDS and gate["status"] == "passed" for gate in gates))
        self.assertFalse((root / "2_ANALYTICS/manifest/ge16-analytics-handoff.json").exists())

    def test_stage_one_phase_b_requires_an_explicit_phase(self) -> None:
        temporary = fixture_root()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        status, record = invoke("--contract", DATA_CONTRACT, "--version", VERSION, "--dry-run", "--isolated-root", str(root))
        self.assertEqual(1, status)
        self.assertEqual("execution-error", record["result"])
        self.assertFalse((root / "1_DATA/manifest/ge16-data-handoff.json").exists())

    def test_stage_one_phase_b_fails_closed_when_candidates_are_stale(self) -> None:
        temporary = fixture_root()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        candidates = root / run_stage.STAGE_ONE_CANDIDATES_PATH
        os.utime(str(candidates), (1, 1))
        status, record = invoke("--contract", DATA_CONTRACT, "--version", VERSION, "--dry-run", "--isolated-root", str(root), "--phase", "b")
        self.assertNotEqual(0, status)
        self.assertEqual("execution-error", record["result"])
        self.assertFalse((root / "1_DATA/manifest/ge16-data-handoff.json").exists())

    def test_stage_one_fails_closed_for_stale_evidence(self) -> None:
        temporary = fixture_root()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        evidence = root / "1_DATA/canonical-data-provenance.json"
        os.utime(str(evidence), (1, 1))
        status, record = invoke("--contract", DATA_CONTRACT, "--version", VERSION, "--dry-run", "--isolated-root", str(root), "--phase", "b")
        self.assertNotEqual(0, status)
        self.assertEqual("execution-error", record["result"])
        self.assertFalse((root / "1_DATA/manifest/ge16-data-handoff.json").exists())

    def test_stage_one_fails_closed_for_validation_command_failure(self) -> None:
        temporary = fixture_root()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        write_script(root, "1_DATA/scripts/validate_canonical_data.py", "raise SystemExit(1)")
        status, record = invoke("--contract", DATA_CONTRACT, "--version", VERSION, "--dry-run", "--isolated-root", str(root), "--phase", "b")
        self.assertNotEqual(0, status)
        self.assertEqual("execution-error", record["result"])
        self.assertFalse((root / "1_DATA/manifest/ge16-data-handoff.json").exists())

    def test_stage_two_refuses_without_passed_stage_one_handoff(self) -> None:
        temporary = fixture_root()
        self.addCleanup(temporary.cleanup)
        status, record = invoke("--contract", ANALYTICS_CONTRACT, "--version", VERSION, "--dry-run", "--isolated-root", temporary.name, "--stage", "2")
        self.assertNotEqual(0, status)
        self.assertEqual("execution-error", record["result"])

    def test_stage_two_happy_path_emits_exact_manifest(self) -> None:
        temporary = fixture_root()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        self.assertEqual(0, invoke("--contract", DATA_CONTRACT, "--version", VERSION, "--dry-run", "--isolated-root", str(root), "--phase", "b")[0])
        status, record = invoke("--contract", ANALYTICS_CONTRACT, "--version", VERSION, "--dry-run", "--isolated-root", str(root))
        self.assertEqual(0, status, record)
        manifest = json.loads((root / "2_ANALYTICS/manifest/ge16-analytics-handoff.json").read_text(encoding="utf-8"))
        self.assertEqual(MANIFEST_FIELDS, set(manifest))
        gates = json.loads((root / "2_ANALYTICS/manifest/ge16-analytics-handoff.gates.json").read_text(encoding="utf-8"))
        self.assertEqual(4, len(gates))
        self.assertTrue(all(set(gate) == GATE_FIELDS for gate in gates))

    def test_stages_three_and_four_require_immediate_lineage_and_emit_exact_records(self) -> None:
        temporary = fixture_root()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        for contract in (DATA_CONTRACT, ANALYTICS_CONTRACT, OUTPUTS_CONTRACT, DELIVERY_CONTRACT):
            status, record = invoke("--contract", contract, "--version", VERSION, "--dry-run", "--isolated-root", str(root), *data_phase(contract))
            self.assertEqual(0, status, record)
        for relative, expected_gates in (
            ("3_OUTPUTS/manifest/ge16-release-seal.json", 3),
            ("4_DELIVERY/manifest/ge16-delivery.json", 3),
        ):
            manifest_path = root / relative
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            gates = json.loads(manifest_path.with_name(manifest_path.stem + ".gates.json").read_text(encoding="utf-8"))
            self.assertEqual(MANIFEST_FIELDS, set(manifest))
            self.assertEqual(expected_gates, len(gates))
            self.assertTrue(all(set(gate) == GATE_FIELDS and gate["run_id"] == manifest["run_id"] for gate in gates))

    def test_stage_three_rejects_symlinked_final_upstream_manifest(self) -> None:
        temporary = fixture_root()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        self.assertEqual(0, invoke("--contract", DATA_CONTRACT, "--version", VERSION, "--dry-run", "--isolated-root", str(root), "--phase", "b")[0])
        self.assertEqual(0, invoke("--contract", ANALYTICS_CONTRACT, "--version", VERSION, "--dry-run", "--isolated-root", str(root))[0])
        manifest = root / "2_ANALYTICS/manifest/ge16-analytics-handoff.json"
        target = root / "2_ANALYTICS/manifest/real-handoff.json"
        manifest.rename(target)
        manifest.symlink_to(target.name)
        status, record = invoke("--contract", OUTPUTS_CONTRACT, "--version", VERSION, "--dry-run", "--isolated-root", str(root))
        self.assertEqual(1, status)
        self.assertEqual("execution-error", record["result"])
        self.assertFalse((root / "3_OUTPUTS/manifest/ge16-release-seal.json").exists())

    def test_stage_five_is_a_non_deploying_release_gate_with_exact_authorization(self) -> None:
        temporary = fixture_root()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        for contract in (DATA_CONTRACT, ANALYTICS_CONTRACT, OUTPUTS_CONTRACT, DELIVERY_CONTRACT):
            self.assertEqual(0, invoke("--contract", contract, "--version", VERSION, "--dry-run", "--isolated-root", str(root), *data_phase(contract))[0])
        delivery = root / "4_DELIVERY/manifest/ge16-delivery.json"
        delivery_sha = "sha256:" + run_stage._sha256(delivery)
        (root / "5_WEBSITES/vercel-manifest.json").write_text(json.dumps({"git_sha": "b" * 40, "delivery_manifest_sha256": delivery_sha}) + "\n", encoding="utf-8")
        authorization = run_stage.fixture_release_authorization(
            json.loads(delivery.read_text(encoding="utf-8"))["run_id"],
            delivery_sha,
        )
        write_stage_five_authorization(root, authorization)
        status, record = invoke("--contract", WEBSITES_CONTRACT, "--version", WEBSITES_VERSION, "--dry-run", "--isolated-root", str(root))
        self.assertEqual(0, status, record)
        manifest = root / "5_WEBSITES/manifest/ge16-release-gate.json"
        self.assertTrue(manifest.exists())
        handoff = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(MANIFEST_FIELDS, set(handoff))
        self.assertEqual(
            ["4_DELIVERY/manifest/ge16-delivery.json#sha256:" + delivery_sha.removeprefix("sha256:"), "5_WEBSITES/vercel-manifest.json#sha256:" + run_stage._sha256(root / "5_WEBSITES/vercel-manifest.json")],
            handoff["input_manifest_refs"],
        )
        self.assertEqual(authorization["gate_evidence"], json.loads(manifest.with_name("ge16-release-gate.gates.json").read_text(encoding="utf-8")))
        self.assertEqual(6, len(authorization["gate_evidence"]))
        self.assertTrue(all("deploy" not in command for command in run_stage.STAGE_ADAPTERS["websites-stage-5"][2]))

    def test_stage_five_rejects_mismatched_delivery_authorization(self) -> None:
        temporary = fixture_root()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        for contract in (DATA_CONTRACT, ANALYTICS_CONTRACT, OUTPUTS_CONTRACT, DELIVERY_CONTRACT):
            self.assertEqual(0, invoke("--contract", contract, "--version", VERSION, "--dry-run", "--isolated-root", str(root), *data_phase(contract))[0])
        delivery = root / "4_DELIVERY/manifest/ge16-delivery.json"
        actual_delivery_sha = "sha256:" + run_stage._sha256(delivery)
        (root / "5_WEBSITES/vercel-manifest.json").write_text(json.dumps({"git_sha": "b" * 40, "delivery_manifest_sha256": actual_delivery_sha}) + "\n", encoding="utf-8")
        authorization = run_stage.fixture_release_authorization(
            json.loads((root / "4_DELIVERY/manifest/ge16-delivery.json").read_text(encoding="utf-8"))["run_id"],
            "sha256:" + "0" * 64,
        )
        write_stage_five_authorization(root, authorization)
        status, record = invoke("--contract", WEBSITES_CONTRACT, "--version", WEBSITES_VERSION, "--dry-run", "--isolated-root", str(root))
        self.assertEqual(1, status)
        self.assertEqual("execution-error", record["result"])

    def test_stage_five_rejects_arbitrary_sha_valid_evidence(self) -> None:
        temporary = fixture_root()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        for contract in (DATA_CONTRACT, ANALYTICS_CONTRACT, OUTPUTS_CONTRACT, DELIVERY_CONTRACT):
            self.assertEqual(0, invoke("--contract", contract, "--version", VERSION, "--dry-run", "--isolated-root", str(root), *data_phase(contract))[0])
        delivery = root / "4_DELIVERY/manifest/ge16-delivery.json"
        delivery_sha = "sha256:" + run_stage._sha256(delivery)
        (root / "5_WEBSITES/vercel-manifest.json").write_text(json.dumps({"git_sha": "b" * 40, "delivery_manifest_sha256": delivery_sha}) + "\n", encoding="utf-8")
        authorization = run_stage.fixture_release_authorization(json.loads(delivery.read_text(encoding="utf-8"))["run_id"], delivery_sha)
        write_stage_five_authorization(root, authorization)
        gate = authorization["gate_evidence"][0]
        arbitrary_evidence = run_stage.fixture_stage_five_evidence(
            authorization["delivery_id"], authorization["delivery_manifest_sha256"],
            authorization["vercel_git_sha"], gate["gate_id"], gate["status"],
        )
        arbitrary_evidence["github_repo"] = "example.invalid/arbitrary"
        evidence_path = root / gate["evidence_ref"].partition("#")[0]
        evidence_path.write_bytes(run_stage._canonical_json_bytes(arbitrary_evidence))
        gate["evidence_ref"] = evidence_path.relative_to(root).as_posix() + "#sha256:" + run_stage._sha256(evidence_path)
        (root / "5_WEBSITES/release-authorization.json").write_text(json.dumps(authorization), encoding="utf-8")
        status, record = invoke("--contract", WEBSITES_CONTRACT, "--version", WEBSITES_VERSION, "--dry-run", "--isolated-root", str(root))
        self.assertEqual(1, status)
        self.assertEqual("execution-error", record["result"])
        self.assertFalse((root / "5_WEBSITES/manifest/ge16-release-gate.json").exists())

    def test_stage_five_rejects_malformed_evidence_anchor_without_index_error(self) -> None:
        with self.assertRaises(run_stage.IsolatedRunError):
            run_stage._validate_stage_five_evidence(
                pathlib.Path("/private/tmp"), "5_WEBSITES/evidence.json#sha256:", "fixture evidence",
                "H-20260905-01", "sha256:" + "a" * 64, "b" * 40,
                "website-build-passed", "passed",
            )

    def test_stage_five_uses_post_validation_vercel_manifest_hash(self) -> None:
        temporary = fixture_root()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        for contract in (DATA_CONTRACT, ANALYTICS_CONTRACT, OUTPUTS_CONTRACT, DELIVERY_CONTRACT):
            self.assertEqual(0, invoke("--contract", contract, "--version", VERSION, "--dry-run", "--isolated-root", str(root), *data_phase(contract))[0])
        delivery = root / "4_DELIVERY/manifest/ge16-delivery.json"
        delivery_sha = "sha256:" + run_stage._sha256(delivery)
        vercel = root / "5_WEBSITES/vercel-manifest.json"
        vercel.write_text(json.dumps({"git_sha": "b" * 40, "delivery_manifest_sha256": delivery_sha}) + "\n", encoding="utf-8")
        authorization = run_stage.fixture_release_authorization(json.loads(delivery.read_text(encoding="utf-8"))["run_id"], delivery_sha)
        write_stage_five_authorization(root, authorization)
        pre_adapter_hash = run_stage._sha256(vercel)
        mutated_manifest = json.dumps({"git_sha": "b" * 40, "delivery_manifest_sha256": delivery_sha}, indent=2) + "\n"
        write_script(root, "5_WEBSITES/scripts/verify_release_gate.py", "pathlib.Path('5_WEBSITES/vercel-manifest.json').write_text(%r, encoding='utf-8')" % mutated_manifest)
        status, record = invoke("--contract", WEBSITES_CONTRACT, "--version", WEBSITES_VERSION, "--dry-run", "--isolated-root", str(root))
        self.assertEqual(0, status, record)
        final_hash = run_stage._sha256(vercel)
        self.assertNotEqual(pre_adapter_hash, final_hash)
        handoff = json.loads((root / "5_WEBSITES/manifest/ge16-release-gate.json").read_text(encoding="utf-8"))
        self.assertEqual("5_WEBSITES/vercel-manifest.json#sha256:" + final_hash, handoff["input_manifest_refs"][1])
        self.assertEqual(authorization["gate_evidence"], json.loads((root / "5_WEBSITES/manifest/ge16-release-gate.gates.json").read_text(encoding="utf-8")))

    def test_contract_adapter_name_must_be_allowlisted_and_is_not_shell(self) -> None:
        temporary = fixture_root()
        self.addCleanup(temporary.cleanup)
        contract = json.loads((ROOT / "ops-contract.json").read_text(encoding="utf-8"))
        contract["cron_contracts"][0]["isolated_adapter"] = "data;touch /private/tmp/should-not-exist"
        altered = pathlib.Path(temporary.name) / "altered-contract.json"
        altered.write_text(json.dumps(contract), encoding="utf-8")
        marker = pathlib.Path("/private/tmp/should-not-exist")
        marker.unlink(missing_ok=True)
        with mock.patch.object(run_stage, "CONTRACT_PATH", altered):
            status, record = invoke("--contract", DATA_CONTRACT, "--version", VERSION, "--dry-run", "--isolated-root", temporary.name)
        self.assertEqual(1, status)
        self.assertEqual("validation-error", record["result"])
        self.assertFalse(marker.exists())

    def test_cross_domain_write_detection_fails_before_handoff(self) -> None:
        temporary = fixture_root()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        write_script(root, "1_DATA/scripts/validate_canonical_data.py", "pathlib.Path('2_ANALYTICS/wrong-domain.txt').write_text('bad', encoding='utf-8')")
        status, record = invoke("--contract", DATA_CONTRACT, "--version", VERSION, "--dry-run", "--isolated-root", str(root), "--phase", "b")
        self.assertNotEqual(0, status)
        self.assertEqual("execution-error", record["result"])
        self.assertFalse((root / "2_ANALYTICS/wrong-domain.txt").exists())
        self.assertFalse((root / "1_DATA/manifest/ge16-data-handoff.json").exists())

    def test_arbitrary_ops_sibling_write_is_detected(self) -> None:
        temporary = fixture_root()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        write_script(root, "1_DATA/scripts/validate_canonical_data.py", "pathlib.Path('OPS/escaped.txt').parent.mkdir(); pathlib.Path('OPS/escaped.txt').write_text('bad')")
        status, record = invoke("--contract", DATA_CONTRACT, "--version", VERSION, "--dry-run", "--isolated-root", str(root), "--phase", "b")
        self.assertEqual(1, status)
        self.assertEqual("execution-error", record["result"])
        self.assertFalse((root / "OPS").exists())
        self.assertFalse((root / "1_DATA/manifest/ge16-data-handoff.json").exists())

    @unittest.skipUnless(NATIVE_SANDBOX_AVAILABLE, "native sandbox-exec unavailable or sandbox_apply prohibited")
    def test_dir_fd_escape_is_blocked_by_native_sandbox(self) -> None:
        temporary = fixture_root()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        marker = root.parent / (root.name + "-dirfd-escape")
        marker.unlink(missing_ok=True)
        write_script(root, "1_DATA/scripts/validate_canonical_data.py", "import os\nfd = os.open('..', os.O_RDONLY)\ntry:\n    out = os.open(%r, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600, dir_fd=fd)\n    os.write(out, b'bad')\n    os.close(out)\nfinally:\n    os.close(fd)" % marker.name)
        status, record = invoke("--contract", DATA_CONTRACT, "--version", VERSION, "--dry-run", "--isolated-root", str(root), "--phase", "b")
        self.assertEqual(1, status)
        self.assertEqual("execution-error", record["result"])
        self.assertFalse(marker.exists())

    def test_missing_handoff_gate_evidence_is_rejected(self) -> None:
        temporary = fixture_root()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        self.assertEqual(0, invoke("--contract", DATA_CONTRACT, "--version", VERSION, "--dry-run", "--isolated-root", str(root), "--phase", "b")[0])
        gates_path = root / "1_DATA/manifest/ge16-data-handoff.gates.json"
        gates = json.loads(gates_path.read_text(encoding="utf-8"))
        gates[0]["evidence_ref"] = "1_DATA/missing-evidence.json#sha256:" + "0" * 64
        gates_path.write_text(json.dumps(gates), encoding="utf-8")
        status, record = invoke("--contract", ANALYTICS_CONTRACT, "--version", VERSION, "--dry-run", "--isolated-root", str(root))
        self.assertEqual(1, status)
        self.assertEqual("execution-error", record["result"])

    def test_stage_five_rejects_mismatched_vercel_commit(self) -> None:
        temporary = fixture_root()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        for contract in (DATA_CONTRACT, ANALYTICS_CONTRACT, OUTPUTS_CONTRACT, DELIVERY_CONTRACT):
            self.assertEqual(0, invoke("--contract", contract, "--version", VERSION, "--dry-run", "--isolated-root", str(root), *data_phase(contract))[0])
        delivery = root / "4_DELIVERY/manifest/ge16-delivery.json"
        delivery_sha = "sha256:" + run_stage._sha256(delivery)
        (root / "5_WEBSITES/vercel-manifest.json").write_text(json.dumps({"git_sha": "c" * 40, "delivery_manifest_sha256": delivery_sha}) + "\n", encoding="utf-8")
        auth = run_stage.fixture_release_authorization(json.loads(delivery.read_text())["run_id"], delivery_sha)
        write_stage_five_authorization(root, auth)
        status, record = invoke("--contract", WEBSITES_CONTRACT, "--version", WEBSITES_VERSION, "--dry-run", "--isolated-root", str(root))
        self.assertEqual(1, status)
        self.assertEqual("execution-error", record["result"])

    def test_stage_five_rejects_aila_authorization_field(self) -> None:
        temporary = fixture_root()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        for contract in (DATA_CONTRACT, ANALYTICS_CONTRACT, OUTPUTS_CONTRACT, DELIVERY_CONTRACT):
            self.assertEqual(0, invoke("--contract", contract, "--version", VERSION, "--dry-run", "--isolated-root", str(root), *data_phase(contract))[0])
        delivery = root / "4_DELIVERY/manifest/ge16-delivery.json"
        delivery_sha = "sha256:" + run_stage._sha256(delivery)
        (root / "5_WEBSITES/vercel-manifest.json").write_text(json.dumps({"git_sha": "b" * 40, "delivery_manifest_sha256": delivery_sha}) + "\n", encoding="utf-8")
        auth = run_stage.fixture_release_authorization(json.loads(delivery.read_text())["run_id"], delivery_sha)
        auth["aila_git_sha"] = "a" * 40
        write_stage_five_authorization(root, auth)
        status, record = invoke("--contract", WEBSITES_CONTRACT, "--version", WEBSITES_VERSION, "--dry-run", "--isolated-root", str(root))
        self.assertEqual(1, status)
        self.assertEqual("execution-error", record["result"])

    def test_absolute_path_write_is_blocked_by_the_child_boundary(self) -> None:
        temporary = fixture_root()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        marker = pathlib.Path(tempfile.gettempdir()) / "ge16-absolute-write-marker"
        marker.unlink(missing_ok=True)
        write_script(root, "1_DATA/scripts/validate_canonical_data.py", "pathlib.Path(%r).write_text('bad', encoding='utf-8')" % str(marker))
        status, record = invoke("--contract", DATA_CONTRACT, "--version", VERSION, "--dry-run", "--isolated-root", str(root), "--phase", "b")
        self.assertEqual(1, status)
        self.assertEqual("execution-error", record["result"])
        self.assertFalse(marker.exists())


class BoundedLiveStageTests(FixtureSandboxTests):
    """Live dispatch is exercised only against a patched temporary repository."""

    def setUp(self):
        super().setUp()
        temporary = fixture_root()
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
        self.pin_scripts()
        self.auth = self.authorization(1)
        self.save_auth()

    def pin_scripts(self):
        """Explicitly authorize fixture content, never real domain scripts."""
        contract = json.loads(self.contract_path.read_text())
        pins = contract["migration"]["bounded_live_execution"]["script_sha256"]
        for relative in pins:
            pins[relative] = run_stage._sha256(self.root / relative)
        self.contract_path.write_text(json.dumps(contract))
        if hasattr(self, "auth"):
            self.auth["contract_sha256"] = run_stage._sha256(self.contract_path)
            self.save_auth()

    def authorization(self, stage, phase=None):
        _, _, contract_id, version = run_stage.HANDOFFS[stage]
        contract = run_stage._load_validated_request(contract_id, version)
        now = run_stage.dt.datetime.now(run_stage.dt.timezone.utc).replace(microsecond=0)
        stamp = lambda value: value.isoformat().replace("+00:00", "Z")
        if phase is None and isinstance(contract.get("isolated_adapter"), list):
            # Two-phase stages default to phase b, matching args().
            phase = "b"
        record = {
            "mode": "bounded-live", "approval_status": "approved",
            "execution_mode": "manual-one-stage", "contract_id": contract_id,
            "contract_version": version,
            "scheduler_job_id": contract["scheduler_binding"]["migration_job_id"],
            "stage_number": stage, "one_shot": True,
            "ops_contract_version": "1.9.0", "bounded_live_policy_version": "1.3.0",
            "scheduler_job_enabled": False, "scheduler_job_state": "paused",
            "manual_one_shot_allowed": True,
            "scheduler_registry_sha256": run_stage._sha256(self.registry_path),
            "contract_sha256": run_stage._sha256(run_stage.CONTRACT_PATH),
            "canonical_jobs_sha256": run_stage._sha256(run_stage.JOBS_PATH),
            "authorized_at_utc": stamp(now - run_stage.dt.timedelta(seconds=1)),
            "expires_at_utc": stamp(now + run_stage.dt.timedelta(minutes=5)),
            "allow_deploy": False, "allow_gateway_start": False,
            "allow_schedule_enablement": False, "authorized_by": "owner",
        }
        if phase is not None:
            record["stage_phase"] = phase
        return record

    def save_auth(self):
        self.auth_path.write_text(json.dumps(self.auth), encoding="utf-8")
        self.auth_path.chmod(0o600)

    def args(self, stage=1, phase=None, include_stage=False):
        _, _, contract_id, version = run_stage.HANDOFFS[stage]
        result = ["--contract", contract_id, "--version", version, "--live-run", "--authorization", str(self.auth_path)]
        if include_stage:
            result += ["--stage", str(stage)]
        if stage == 1:
            # Stage 1 is two-phase; default to phase b, which writes the
            # handoff, so the many generic security tests below (script hash
            # mutation, lock contention, replay, environment scrubbing, ...)
            # keep exercising a full, handoff-producing execution unchanged.
            result += ["--phase", phase or "b"]
        return result

    def test_phase_a_authorization_cannot_be_redirected_to_phase_b(self):
        """An approval to collect must not be reusable for the phase that publishes.

        Phase a only collects. Phase b commits the agent's judged output and is the
        only phase that emits the Stage 1 handoff. Without binding the approval to
        its exact phase, one owner approval would be fungible between them.
        """
        # An owner approval granted for phase a...
        self.auth = self.authorization(1, phase="a")
        self.save_auth()
        # ...presented for phase b must fail closed, and must not run any command.
        with mock.patch.object(run_stage, "_run_allowlisted_command") as child:
            status, record = invoke(*self.args(1, phase="b"))
        self.assertEqual(1, status)
        self.assertEqual("validation-error", record["result"])
        child.assert_not_called()
        self.assertFalse(run_stage._handoff_path(self.root, 1).exists())
        # The reverse redirection is equally refused.
        self.auth = self.authorization(1, phase="b")
        self.save_auth()
        with mock.patch.object(run_stage, "_run_allowlisted_command") as child:
            self.assertEqual(1, invoke(*self.args(1, phase="a"))[0])
            child.assert_not_called()

    def test_valid_authorization_runs_one_stage_and_cannot_be_replayed(self):
        original = self.auth_path.read_bytes()
        before_ops = run_stage._inventory(self.root / "OPS")
        with mock.patch.object(run_stage, "_validated_isolated_root", side_effect=AssertionError("live used isolated validation")):
            status, record = invoke(*self.args(1, include_stage=True))
        self.assertEqual(0, status, record)
        manifest = json.loads(run_stage._handoff_path(self.root, 1).read_text())
        self.assertEqual({"result": "completed", "mode": "bounded-live", "stage": 1,
                          "contract_id": DATA_CONTRACT, "contract_version": VERSION,
                          "run_id": manifest["run_id"]}, record)
        self.assertEqual(MANIFEST_FIELDS, set(manifest))
        self.assertEqual(original, self.auth_path.read_bytes())
        self.assertEqual(before_ops, run_stage._inventory(self.root / "OPS"))
        self.assertFalse(run_stage._handoff_path(self.root, 2).exists())
        # Canonical content, not path/whitespace, defines the one-shot claim.
        self.auth_path = self.auth_path.with_name("copy.json")
        self.save_auth()
        with mock.patch.object(run_stage, "_run_allowlisted_command") as child:
            self.assertEqual(1, invoke(*self.args())[0])
            child.assert_not_called()

    def test_authorization_schema_bindings_and_time_fail_closed(self):
        for key, value in [
            ("expires_at_utc", "2000-01-01T00:00:00Z"),
            ("authorized_at_utc", "2099-01-01T00:00:00Z"),
            ("authorized_at_utc", "invalid"),
            ("authorized_at_utc", "2000-01-01T00:00:00Z"),
            ("expires_at_utc", "2099-01-01T00:00:00Z"),
            ("contract_sha256", "0" * 64), ("canonical_jobs_sha256", "0" * 64),
            ("contract_id", ANALYTICS_CONTRACT), ("contract_version", "0.0.0"),
            ("scheduler_job_id", "2b0a9111c836"), ("stage_number", 2),
            ("stage_number", True), ("stage_number", 1.0),
            ("unknown_secret", "do-not-print-this"),
            ("allow_deploy", True), ("allow_gateway_start", True),
            ("allow_schedule_enablement", True), ("allow_deploy", 0),
            ("one_shot", 1), ("one_shot", False), ("mode", "dry-run"),
            ("approval_status", "pending"), ("execution_mode", "scheduler"),
            ("authorized_by", "someone-else"),
            ("ops_contract_version", "1.5.0"), ("bounded_live_policy_version", "0.0.0"),
            ("scheduler_job_enabled", True), ("scheduler_job_enabled", 0),
            ("scheduler_job_state", "disabled"), ("manual_one_shot_allowed", False),
            ("manual_one_shot_allowed", 1), ("scheduler_registry_sha256", "0" * 64),
        ]:
            with self.subTest(field=key, value=value):
                self.auth = self.authorization(1)
                self.auth[key] = value
                self.save_auth()
                with mock.patch.object(run_stage, "_run_allowlisted_command") as child:
                    status, record = invoke(*self.args())
                self.assertEqual(1, status)
                self.assertEqual("validation-error", record["result"])
                self.assertNotIn("do-not-print-this", json.dumps(record))
                child.assert_not_called()
        for key in self.authorization(1):
            self.auth = self.authorization(1)
            del self.auth[key]
            self.save_auth()
            self.assertEqual(1, invoke(*self.args())[0], key)

    def test_invalid_authorization_json_and_duplicate_keys_are_rejected(self):
        for raw in ("[]", "null", "{secret", json.dumps(self.auth)[:-1] + ', "one_shot": true}'):
            self.auth_path.write_text(raw)
            status, record = invoke(*self.args())
            self.assertEqual(1, status)
            self.assertEqual("validation-error", record["result"])

    def test_authorization_path_must_be_safe_external_regular_file(self):
        original = self.auth_path
        inside = self.root / "1_DATA/auth.json"
        inside.write_bytes(original.read_bytes())
        link = original.with_name("link.json")
        link.symlink_to(original)
        parent_link = original.parent / "linked"
        parent_link.symlink_to(original.parent, target_is_directory=True)
        hardlink = original.with_name("hard.json")
        os.link(original, hardlink)
        for path in (inside, link, parent_link / original.name, original.parent,
                     original.with_name("missing"), hardlink, pathlib.Path("relative.json")):
            with self.subTest(path=path):
                self.auth_path = path
                self.assertEqual(1, invoke(*self.args())[0])
        hardlink.unlink()
        self.auth_path = original
        original.chmod(0o666)
        self.assertEqual(1, invoke(*self.args())[0])

    def test_live_root_rejects_alternates_overlap_files_and_links(self):
        for path in (self.root.parent, self.root / "1_DATA", self.auth_path,
                     self.root / "missing", self.auth_path.parent):
            with self.subTest(path=path), self.assertRaises(ValueError):
                run_stage._validated_live_root(path)
        link = self.auth_path.parent / "repo-link"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            run_stage._validated_live_root(link)
        for kind in ("missing", "file", "symlink"):
            domain = self.root / "5_WEBSITES"
            domain.rename(self.root / "saved")
            if kind == "file": domain.write_text("bad")
            if kind == "symlink": domain.symlink_to(self.root / "saved", target_is_directory=True)
            self.assertEqual(1, invoke(*self.args())[0], kind)
            if domain.exists() or domain.is_symlink(): domain.unlink()
            (self.root / "saved").rename(domain)

    def test_exact_argument_vectors_and_regular_posture(self):
        args = self.args()
        for vector in (args[:-1], args + ["extra"], args + ["--root", str(self.root)],
                       args[:-2] + ["--stage", "0"] + args[-2:],
                       # --stage must precede --phase; placing it after the default
                       # "--phase b" tail fails argument ordering before ever
                       # reaching the requested-stage mismatch check.
                       args[:-2] + ["--stage", "2"] + args[-2:],
                       args + ["--stage", "1", "--stage", "1"],
                       args + ["--dry-run", "--isolated-root", str(self.root)],
                       ["--live-run"] + args[:4] + args[5:]):
            with self.subTest(vector=vector):
                self.assertEqual(1, invoke(*vector)[0])
        self.assertEqual((0, {"result": "posture", "mode": "posture-only", "contract_id": DATA_CONTRACT,
                              "contract_version": VERSION, "ops_contract_version": "1.9.0", "lock_retired": True}), invoke(*args[:4]))
        self.assertEqual(1, invoke(*args[:4], "--dry-run", "--isolated-root", str(self.root))[0])

    def test_owner_write_allowed_and_sibling_ops_writes_prevented_in_both_modes(self):
        for live in (False, True):
            for relative in ("2_ANALYTICS/escaped.txt", "OPS/escaped.txt"):
                with self.subTest(live=live, relative=relative):
                    self.auth["expires_at_utc"] = (
                        run_stage._parse_utc(self.auth["expires_at_utc"], "test") - run_stage.dt.timedelta(seconds=1)
                    ).isoformat().replace("+00:00", "Z")
                    self.save_auth()
                    write_script(self.root, run_stage.COMMAND_ALLOWLIST["data-validate-canonical"][0],
                                 "pathlib.Path(%r).write_text('bad')" % relative)
                    self.pin_scripts()
                    with mock.patch.object(run_stage, "_write_handoff") as handoff, \
                            mock.patch.object(run_stage, "_run_allowlisted_command", wraps=run_stage._run_allowlisted_command) as child:
                        if live:
                            status, record = invoke(*self.args())
                        else:
                            # Exercise the common executor without dry-run root dispatch.
                            with self.assertRaises(run_stage.IsolatedRunError):
                                run_stage._run_isolated(run_stage._load_validated_request(DATA_CONTRACT, VERSION), self.root, 1, phase="b")
                            status = 1
                    self.assertEqual(1, status)
                    self.assertFalse((self.root / relative).exists())
                    handoff.assert_not_called()
                    self.assertEqual(child.call_args.args, (self.root, "data-validate-canonical", "1_DATA"))
                    self.assertEqual([call.args[1] for call in child.call_args_list], list(run_stage.STAGE_ADAPTERS["data-stage-1b"][2]))
        write_script(self.root, run_stage.COMMAND_ALLOWLIST["data-validate-canonical"][0],
                     "pathlib.Path('1_DATA/allowed.txt').write_text('ok')")
        run_stage._run_isolated(run_stage._load_validated_request(DATA_CONTRACT, VERSION), self.root, 1, phase="b")
        self.assertEqual("ok", (self.root / "1_DATA/allowed.txt").read_text())

    def test_child_failure_output_does_not_disclose_authorization(self):
        write_script(self.root, run_stage.COMMAND_ALLOWLIST["data-validate-canonical"][0],
                     "import sys\nprint(pathlib.Path(%r).read_text(), file=sys.stderr)\nraise SystemExit(1)" % str(self.auth_path))
        self.pin_scripts()
        status, record = invoke(*self.args())
        self.assertEqual(1, status)
        self.assertEqual("execution-error", record["result"])
        self.assertNotIn("authorized_by", json.dumps(record))
        self.assertNotIn(str(self.auth_path), json.dumps(record))

    def test_handoff_symlink_cannot_mutate_sibling(self):
        manifest = run_stage._handoff_path(self.root, 1)
        manifest.parent.mkdir(exist_ok=True)
        target = self.root / "2_ANALYTICS/protected.json"
        target.write_text("original")
        manifest.symlink_to(target)
        status, record = invoke(*self.args())
        self.assertEqual(1, status)
        self.assertEqual("original", target.read_text())

    def test_revocation_blocks_before_authorization_claim_or_child(self):
        contract = json.loads(self.contract_path.read_text())
        contract["migration"]["bounded_live_execution"]["manual_execution_enabled"] = False
        self.contract_path.write_text(json.dumps(contract))
        with mock.patch.object(run_stage, "_validate_live_authorization") as approval, \
                mock.patch.object(run_stage, "_claim_live_authorization") as claim, \
                mock.patch.object(run_stage, "_run_isolated") as executor, \
                mock.patch.object(run_stage, "_run_allowlisted_command") as child:
            with self.assertRaisesRegex(run_stage.LiveValidationError, "^bounded-live-execution-revoked$"):
                run_stage._run_live_locked(DATA_CONTRACT, VERSION, self.auth_path, 1, "b")
        approval.assert_not_called()
        claim.assert_not_called()
        executor.assert_not_called()
        child.assert_not_called()

    def test_stage_five_live_execution_is_forbidden_before_authorization_or_child(self):
        with mock.patch.object(run_stage, "_validate_live_authorization") as approval, \
                mock.patch.object(run_stage, "_claim_live_authorization") as claim, \
                mock.patch.object(run_stage, "_run_allowlisted_command") as child:
            status, record = invoke(*self.args(5))
        self.assertEqual((1, {"result": "validation-error", "reason": "stage-5-live-execution-forbidden"}), (status, record))
        approval.assert_not_called()
        claim.assert_not_called()
        child.assert_not_called()

    def test_claim_is_protected_from_child_and_failed_attempt_stays_consumed(self):
        write_script(self.root, run_stage.COMMAND_ALLOWLIST["data-validate-canonical"][0],
                     "next(pathlib.Path('1_DATA/.bounded-live-claims').iterdir()).unlink()")
        self.pin_scripts()
        status, record = invoke(*self.args())
        self.assertEqual(1, status)
        self.assertEqual("execution-error", record["result"])
        self.assertEqual(1, len(list((self.root / "1_DATA/.bounded-live-claims").iterdir())))
        with mock.patch.object(run_stage, "_run_allowlisted_command") as child:
            self.assertEqual(1, invoke(*self.args())[0])
            child.assert_not_called()

    def test_old_lock_or_missing_clause_cannot_reach_authorization_or_child(self):
        original = json.loads(self.contract_path.read_text())
        for old_version in (False, True):
            contract = copy.deepcopy(original)
            del contract["migration"]["bounded_live_execution"]
            if old_version:
                contract["contract_version"] = "1.5.0"
            self.contract_path.write_text(json.dumps(contract))
            with mock.patch.object(run_stage, "_validate_live_authorization") as approval, \
                    mock.patch.object(run_stage, "_run_allowlisted_command") as child:
                status, record = invoke(*self.args())
            self.assertEqual(1, status)
            self.assertEqual("validation-error", record["result"])
            approval.assert_not_called()
            child.assert_not_called()

    def test_stage_three_and_four_scripts_hash_pinned_and_runnable(self):
        canonical_pins = json.loads((ROOT / "ops-contract.json").read_text())["migration"]["bounded_live_execution"]["script_sha256"]
        for stage in (3, 4):
            name = "outputs-intake-seal" if stage == 3 else "delivery-publish"
            relative = run_stage.COMMAND_ALLOWLIST[name][0]
            self.assertEqual(canonical_pins[relative], run_stage._sha256(ROOT.parent / relative))
            self.auth = self.authorization(stage)
            self.save_auth()
            pins = json.loads(self.contract_path.read_text())["migration"]["bounded_live_execution"]["script_sha256"]
            self.assertEqual(pins[relative], run_stage._sha256(self.root / relative))
            # Supply upstream fixture state and stop at the child boundary:
            # authorization and script-pin checks remain real; no Git or child runs.
            with mock.patch.object(run_stage, "_stage_input", return_value=(["fixture"], "H-20260921-01")), \
                    mock.patch.object(run_stage, "_commit_upstream_manifests", return_value={"1_DATA": "a" * 40, "2_ANALYTICS": "b" * 40}), \
                    mock.patch.object(run_stage, "_local_repository"), \
                    mock.patch.object(run_stage, "_parent_git", return_value=""), \
                    mock.patch.object(run_stage, "_release_binding", return_value=(None, {"sealed_commit": "c" * 40})), \
                    mock.patch.object(run_stage, "_run_allowlisted_command", side_effect=run_stage.IsolatedRunError("sandbox boundary reached")) as child:
                status, record = invoke(*self.args(stage))
            self.assertEqual((1, {"result": "execution-error", "reason": "bounded-live-execution-failed"}), (status, record))
            child.assert_called_once()
            self.assertEqual((self.root, name, relative.split("/")[0]), child.call_args.args)
            self.assertEqual(pins, child.call_args.kwargs["live_script_hashes"])

    def test_scheduler_target_must_be_exact_disabled_and_paused(self):
        original = json.loads(self.registry_path.read_text())
        variants = []
        for field, value in (("id", "wrong"), ("enabled", True), ("enabled", 0),
                             ("enabled", "false"), ("state", "disabled"), ("state", "running")):
            registry = copy.deepcopy(original)
            registry["jobs"][0][field] = value
            variants.append(registry)
        for field in ("id", "enabled", "state"):
            registry = copy.deepcopy(original)
            del registry["jobs"][0][field]
            variants.append(registry)
        duplicate = copy.deepcopy(original)
        duplicate["jobs"].append(copy.deepcopy(duplicate["jobs"][0]))
        variants.extend([duplicate, {"jobs": []}, {"jobs": [None]}])
        for registry in variants:
            with self.subTest(registry=registry):
                self.registry_path.write_text(json.dumps(registry))
                self.auth["scheduler_registry_sha256"] = run_stage._sha256(self.registry_path)
                self.save_auth()
                with mock.patch.object(run_stage, "_claim_live_authorization") as claim, \
                        mock.patch.object(run_stage, "_run_allowlisted_command") as child:
                    status, record = invoke(*self.args())
                self.assertEqual(1, status)
                self.assertEqual("validation-error", record["result"])
                claim.assert_not_called()
                child.assert_not_called()

    def test_scheduler_change_after_approval_is_rejected(self):
        self.registry_path.write_text(self.registry_path.read_text() + " ")
        with mock.patch.object(run_stage, "_run_allowlisted_command") as child:
            self.assertEqual(1, invoke(*self.args())[0])
        child.assert_not_called()

    def test_script_mutation_is_rejected_before_child(self):
        script = self.root / run_stage.COMMAND_ALLOWLIST["data-validate-canonical"][0]
        script.write_text(script.read_text() + "\nprint('mutation')\n")
        with mock.patch.object(run_stage.subprocess, "run") as child:
            status, record = invoke(*self.args())
        self.assertEqual((1, {"result": "validation-error", "reason": "bounded-live-script-hash-mismatch"}), (status, record))
        child.assert_not_called()
        self.assertFalse(run_stage._handoff_path(self.root, 1).exists())

    def test_each_stage_two_command_is_rehashed_after_prior_command_mutation(self):
        run_stage._run_isolated(run_stage._load_validated_request(DATA_CONTRACT, VERSION), self.root, 1, phase="b")
        self.auth = self.authorization(2)
        self.save_auth()
        original_child = run_stage._run_allowlisted_command
        calls = []
        def mutate_next(root, name, owner, **kwargs):
            calls.append(name)
            original_child(root, name, owner, **kwargs)
            if name == "analytics-forecast":
                write_script(root, run_stage.COMMAND_ALLOWLIST["analytics-report-en"][0], "print('changed')")
        with mock.patch.object(run_stage, "_run_allowlisted_command", side_effect=mutate_next):
            status, record = invoke(*self.args(2))
        self.assertEqual(["analytics-forecast", "analytics-report-en"], calls)
        self.assertEqual((1, {"result": "validation-error", "reason": "bounded-live-script-hash-mismatch"}), (status, record))
        self.assertFalse(run_stage._handoff_path(self.root, 2).exists())

    def test_child_rejects_script_swapped_after_parent_hash_check(self):
        actual_run = run_stage.subprocess.run
        def swap_then_start(command, **kwargs):
            # Swap precisely the child selected after its parent hash check.
            write_script(self.root, command[10],
                         "pathlib.Path('1_DATA/swapped-code-ran').write_text('bad')")
            return actual_run(command, **kwargs)
        with mock.patch.object(run_stage.subprocess, "run", side_effect=swap_then_start):
            status, record = invoke(*self.args())
        self.assertEqual(1, status)
        self.assertEqual("execution-error", record["result"])
        self.assertFalse((self.root / "1_DATA/swapped-code-ran").exists())
        self.assertFalse(run_stage._handoff_path(self.root, 1).exists())

    def test_registry_is_rechecked_after_command_before_handoff(self):
        actual_child = run_stage._run_allowlisted_command
        def change_registry(*args, **kwargs):
            actual_child(*args, **kwargs)
            registry = json.loads(self.registry_path.read_text())
            registry["jobs"][0]["state"] = "running"
            self.registry_path.write_text(json.dumps(registry))
        with mock.patch.object(run_stage, "_run_allowlisted_command", side_effect=change_registry):
            status, record = invoke(*self.args())
        self.assertEqual(1, status)
        self.assertEqual("validation-error", record["result"])
        self.assertFalse(run_stage._handoff_path(self.root, 1).exists())

    def test_unsafe_lock_inode_is_rejected_without_claim(self):
        path = run_stage.LIVE_LOCK_PATH
        path.write_text("")
        for mode in (0o644, 0o666):
            path.chmod(mode)
            self.assertEqual(1, invoke(*self.args())[0])
        path.chmod(0o600)
        alias = path.with_name("alias.lock")
        os.link(path, alias)
        self.assertEqual(1, invoke(*self.args())[0])
        alias.unlink()
        path.unlink()
        path.symlink_to(self.auth_path)
        self.assertEqual(1, invoke(*self.args())[0])
        self.assertFalse((self.root / "1_DATA/.bounded-live-claims").exists())

    def test_child_gets_only_scrubbed_environment_and_output_is_discarded(self):
        write_script(self.root, run_stage.COMMAND_ALLOWLIST["data-validate-canonical"][0],
                     "import os, sys\nassert 'GE16_SENTINEL_SECRET' not in os.environ\n"
                     "assert os.environ['PATH'] == '/usr/bin:/bin'\n"
                     "assert os.environ['PYTHONDONTWRITEBYTECODE'] == '1'\n"
                     "print('secret child stdout')\nprint('secret child stderr', file=sys.stderr)")
        self.pin_scripts()
        with mock.patch.dict(os.environ, {"GE16_SENTINEL_SECRET": "sentinel-secret-value", "PATH": "/secret/path"}), \
                mock.patch.object(run_stage.subprocess, "run", wraps=run_stage.subprocess.run) as child:
            status, record = invoke(*self.args())
        self.assertEqual(0, status, record)
        self.assertEqual(run_stage.CHILD_ENVIRONMENT, child.call_args.kwargs["env"])
        self.assertEqual(subprocess.DEVNULL, child.call_args.kwargs["stdout"])
        self.assertEqual(subprocess.DEVNULL, child.call_args.kwargs["stderr"])
        self.assertNotIn("secret", json.dumps(record))

    def test_lock_contention_blocks_before_claim_and_releases_after_failure(self):
        with run_stage._exclusive_live_lock():
            with mock.patch.object(run_stage, "_claim_live_authorization") as claim:
                status, record = invoke(*self.args())
            self.assertEqual((1, {"result": "validation-error", "reason": "bounded-live-lock-already-held"}), (status, record))
            claim.assert_not_called()
        # Exercise actual OS lock contention across processes, without live dispatch.
        with run_stage._exclusive_live_lock():
            result = subprocess.Popen([
                __import__("sys").executable, "-c",
                "import fcntl,sys; f=open(sys.argv[1], 'r+'); fcntl.flock(f, fcntl.LOCK_EX|fcntl.LOCK_NB)",
                str(run_stage.LIVE_LOCK_PATH),
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.assertNotEqual(0, result.wait(timeout=10))
        with self.assertRaises(RuntimeError):
            with run_stage._exclusive_live_lock():
                raise RuntimeError("fixture failure")
        with run_stage._exclusive_live_lock():
            self.assertEqual(0o600, run_stage.LIVE_LOCK_PATH.stat().st_mode & 0o777)

    def test_lock_is_held_through_child_and_handoff_then_released(self):
        original_child = run_stage._run_allowlisted_command
        original_handoff = run_stage._write_handoff
        def assert_locked(call, *args, **kwargs):
            with self.assertRaisesRegex(run_stage.LiveValidationError, "already-held"):
                with run_stage._exclusive_live_lock():
                    self.fail("nested lock acquired")
            return call(*args, **kwargs)
        with mock.patch.object(run_stage, "_run_allowlisted_command", side_effect=lambda *a, **k: assert_locked(original_child, *a, **k)), \
                mock.patch.object(run_stage, "_write_handoff", side_effect=lambda *a, **k: assert_locked(original_handoff, *a, **k)):
            self.assertEqual(0, invoke(*self.args())[0])
        with run_stage._exclusive_live_lock():
            pass

    def test_native_sandbox_failure_has_no_production_fallback(self):
        with mock.patch.object(run_stage.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "sandbox_apply: Operation not permitted")) as child:
            status, record = invoke(*self.args())
        self.assertEqual(1, status)
        self.assertEqual("execution-error", record["result"])
        child.assert_called_once()
        self.assertFalse(run_stage._handoff_path(self.root, 1).exists())

    def test_child_cannot_spawn_processes_or_connect_to_network(self):
        for body in (
            "import subprocess, sys\nsubprocess.run([sys.executable, '-c', 'pass'])",
            "import socket\nsocket.socket().connect(('127.0.0.1', 9))",
        ):
            write_script(self.root, run_stage.COMMAND_ALLOWLIST["websites-build-gate"][0], body)
            with self.assertRaises(run_stage.IsolatedRunError):
                run_stage._run_allowlisted_command(self.root, "websites-build-gate", "5_WEBSITES")

    def test_execution_surface_is_only_allowlisted_python_sandbox(self):
        tree = ast.parse(pathlib.Path(run_stage.__file__).read_text())
        assert_execution_imports(self, tree)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                 and node.func.value.id == "subprocess"]
        # Exactly two execution sites: the sandboxed Python child payload in
        # _run_allowlisted_command and the parent Git steps in _parent_git.
        self.assertEqual(["run", "run"], [node.func.attr for node in calls])
        self.assertTrue({"Popen", "check_output", "check_call"}.isdisjoint(
            node.func.attr for node in calls
        ))
        self.assertEqual({"_run_allowlisted_command", "_parent_git"}, {
            function.name for function in tree.body if isinstance(function, ast.FunctionDef)
            if any(node in calls for node in ast.walk(function))
        })
        self.assertEqual({"SyncError", "validate_canonical_config"}, {
            alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
            and node.module == "cron.sync_jobs" for alias in node.names
        })
        for command in run_stage.COMMAND_ALLOWLIST.values():
            self.assertTrue(command[0].endswith(".py"))
            self.assertTrue(all("deploy" not in element.lower() for element in command))
        live_paths = set()
        for stage, _, names in run_stage.STAGE_ADAPTERS.values():
            if stage == 5:
                continue
            for name in names:
                command = run_stage.COMMAND_ALLOWLIST[name]
                live_paths.add(command[0])
                for element in command:
                    self.assertFalse(any(marker in element.lower() for marker in
                                         ("deploy", "vercel", "netlify", "wrangler", "firebase")))
        self.assertNotIn(run_stage.COMMAND_ALLOWLIST["websites-build-gate"][0], live_paths)
        clause = json.loads(self.contract_path.read_text())["migration"]["bounded_live_execution"]
        self.assertEqual(set(clause["script_sha256"]), set(clause["modules_pinned"]) | {
            run_stage.COMMAND_ALLOWLIST[name][0]
            for stage, _, names in run_stage.STAGE_ADAPTERS.values()
            if stage in clause["allowed_stages"] for name in names
        })

    @unittest.skipUnless(NATIVE_SANDBOX_AVAILABLE, "native sandbox-exec unavailable or sandbox_apply prohibited")
    def test_native_fence_blocks_sibling_ops_and_claim_writes_without_audit_hook(self):
        # libc bypasses Python's audit hook: only the OS profile can stop this.
        for relative in ("2_ANALYTICS/escaped.txt", "OPS/escaped.txt", "1_DATA/.bounded-live-claims/tampered"):
            with self.subTest(relative=relative):
                (self.root / "1_DATA/.bounded-live-claims").mkdir(exist_ok=True)
                write_script(self.root, run_stage.COMMAND_ALLOWLIST["data-validate-canonical"][0],
                             "import ctypes, os\nlibc = ctypes.CDLL(None, use_errno=True)\n"
                             "fd = libc.open(%r, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)\n"
                             "raise SystemExit(1 if fd < 0 else 0)" % relative.encode())
                with self.assertRaises(run_stage.IsolatedRunError):
                    run_stage._run_allowlisted_command(self.root, "data-validate-canonical", "1_DATA")
                self.assertFalse((self.root / relative).exists())
