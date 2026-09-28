#!/usr/bin/env python3
"""Contract-first tests for the OUTPUTS boundary."""

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "output-contract.json"
VALIDATOR = ROOT / "scripts" / "validate_output_contract.py"
INTAKE_VALIDATOR = ROOT / "scripts" / "validate_release_intake.py"

SHA256 = "a" * 64
GIT_SHA = "b" * 40
STATES = (
    ("johor", "Johor"), ("kedah", "Kedah"), ("kelantan", "Kelantan"),
    ("melaka", "Melaka"), ("negeri-sembilan", "Negeri Sembilan"),
    ("pahang", "Pahang"), ("perak", "Perak"), ("perlis", "Perlis"),
    ("pulau-pinang", "Pulau Pinang"), ("sabah", "Sabah"),
    ("sarawak", "Sarawak"), ("selangor", "Selangor"),
    ("terengganu", "Terengganu"),
)
PRN_STATES = ("melaka", "pahang", "perak", "perlis", "sarawak")


def semantic_entry(role, source_path, category, destination_path, content_type, content_schema=None, content_version=None):
    return {
        "role": role,
        "source_path": source_path,
        "category": category,
        "destination_path": destination_path,
        "content_type": content_type,
        "content_schema": content_schema,
        "content_version": content_version,
    }


def delivery_entries():
    entries = [
        semantic_entry("app-data", "artifacts/data/app-data.json", "forecast", "data/app-data.json", "application/json", "hermes.delivery.app-data", 1),
        semantic_entry("forecast", "artifacts/data/forecast.json", "forecast", "data/forecast.json", "application/json", "hermes.delivery.forecast", 1),
        semantic_entry("forecast-engine", "artifacts/data/ge16-forecast-latest.json", "forecast", "data/ge16-forecast-latest.json", "application/json", "hermes.forecast.engine-output", 1),
        semantic_entry("scenarios", "artifacts/data/scenarios.json", "forecast", "data/scenarios.json", "application/json", "hermes.delivery.scenarios", 1),
        semantic_entry("report.federal.en", "artifacts/reports/federal/GE16_Malaysia_General_Election_Report.md", "report", "reports/federal/GE16_Malaysia_General_Election_Report.md", "text/markdown"),
        semantic_entry("report.federal.ms", "artifacts/reports/federal/GE16_Malaysia_General_Election_Report_MS.md", "report", "reports/federal/GE16_Malaysia_General_Election_Report_MS.md", "text/markdown"),
        semantic_entry("social.payload", "artifacts/social/social-payload.json", "social", "social/social-payload.json", "application/json", "hermes.delivery.social-payload", 1),
        semantic_entry("tracking.payload", "artifacts/tracking/tracking-payload.json", "tracking", "tracking/tracking-payload.json", "application/json", "hermes.delivery.tracking-payload", 1),
    ]
    for state_id, state_name in STATES:
        root = "reports/states/DUN {}".format(state_name)
        entries.extend((
            semantic_entry("report.state.{}.en".format(state_id), "artifacts/{}/GE16_{}_Report.md".format(root, state_name), "report", "{}/GE16_{}_Report.md".format(root, state_name), "text/markdown"),
            semantic_entry("report.state.{}.ms".format(state_id), "artifacts/{}/GE16_{}_Report_MS.md".format(root, state_name), "report", "{}/GE16_{}_Report_MS.md".format(root, state_name), "text/markdown"),
            semantic_entry("report.state-summary.{}.en".format(state_id), "artifacts/{}/dun-election-summary.md".format(root), "report", "{}/dun-election-summary.md".format(root), "text/markdown"),
            semantic_entry("report.state-summary.{}.ms".format(state_id), "artifacts/{}/dun-election-summary_MS.md".format(root), "report", "{}/dun-election-summary_MS.md".format(root), "text/markdown"),
            semantic_entry("report.state-deepdive.{}".format(state_id), "artifacts/{}/ge16-battleground-deepdive.md".format(root), "report", "{}/ge16-battleground-deepdive.md".format(root), "text/markdown"),
        ))
    for state_id, state_name in STATES:
        if state_id in PRN_STATES:
            root = "reports/states/DUN {}".format(state_name)
            entries.append(semantic_entry("report.prn-projection.{}".format(state_id), "artifacts/{}/{}-prn-projection-report.md".format(root, state_id), "report", "{}/{}-prn-projection-report.md".format(root, state_id), "text/markdown"))
            entries.append(semantic_entry("state-composition.{}".format(state_id), "artifacts/state-composition/{}.json".format(state_id), "state-composition", "state-composition/{}.json".format(state_id), "application/json", "hermes.delivery.state-composition", 1))
    return entries


def validate(contract):
    return subprocess.run(
        [sys.executable, str(VALIDATOR), "--stdin"],
        input=json.dumps(contract),
        text=True,
        capture_output=True,
        check=False,
    )


def validate_intake(manifest):
    return subprocess.run(
        [sys.executable, str(INTAKE_VALIDATOR), "--stdin"],
        input=json.dumps(manifest),
        text=True,
        capture_output=True,
        check=False,
    )


class OutputContractValidatorTest(unittest.TestCase):
    def setUp(self):
        self.contract = json.loads(CONTRACT.read_text(encoding="utf-8"))

    def test_release_artifacts_are_narrowly_marked_non_text_for_git(self):
        attributes = ROOT / ".gitattributes"
        lines = [
            line.strip()
            for line in attributes.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        rules = [line.split() for line in lines]

        self.assertIn(["releases/**", "-text"], rules)
        self.assertIn(["releases/**", "-whitespace"], rules)
        self.assertFalse(
            any(
                pattern in {"*", "**", "/*"}
                and ({"-text", "-whitespace", "binary"} & set(attributes))
                for pattern, *attributes in rules),
            "byte-preservation handling must be confined to immutable release payloads",
        )

    def test_repository_contract_is_valid(self):
        result = validate(self.contract)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_contracts_are_rejected(self):
        cases = []

        missing_provenance = copy.deepcopy(self.contract)
        missing_provenance["provenance"]["required_fields"].remove("data_commit_sha")
        cases.append(("missing provenance", missing_provenance))

        missing_intake = copy.deepcopy(self.contract)
        del missing_intake["release_intake"]
        cases.append(("missing release intake", missing_intake))

        missing_sealed_validation = copy.deepcopy(self.contract)
        del missing_sealed_validation["release_validation"]
        cases.append(("missing sealed validation", missing_sealed_validation))

        website_path = copy.deepcopy(self.contract)
        website_path["release_layout"]["root"] = "5_WEBSITES/published-output"
        cases.append(("WEBSITE path", website_path))

        delivery_write = copy.deepcopy(self.contract)
        delivery_write["release_layout"]["delivery_write_path"] = "4_DELIVERY/publish"
        cases.append(("DELIVERY write path", delivery_write))

        mutable_current_alias = copy.deepcopy(self.contract)
        mutable_current_alias["immutability"]["mutable_aliases_forbidden"].remove("current")
        mutable_current_alias["release_layout"]["current_alias"] = "current"
        cases.append(("mutable current alias", mutable_current_alias))

        for label, invalid_contract in cases:
            with self.subTest(label=label):
                result = validate(invalid_contract)
                self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_contract_declares_release_intake_requirements(self):
        intake = self.contract["release_intake"]
        self.assertEqual(intake["manifest_path"], "releases/{release_id}/manifest.json")
        self.assertEqual(intake["receipt_path"], "releases/{release_id}/receipt.json")
        self.assertEqual(intake["receipt_schema"], "outputs.release-receipt.v1")
        self.assertEqual(
            set(intake["allowed_root_entries"]),
            {"manifest.json", "SHA256SUMS", "receipt.json", "artifacts"},
        )
        self.assertEqual(
            set(intake["provenance"]["required_fields"]),
            {"analytics_commit_sha", "data_commit_sha", "run_id", "artifact_manifest_sha256"},
        )
        self.assertEqual(
            set(intake["artifacts"]["required_categories"]),
            {"forecast", "report", "social", "tracking", "state-composition"},
        )
        self.assertEqual(
            set(intake["artifacts"]["required_fields"]),
            {"path", "category", "sha256", "bytes"},
        )
        self.assertEqual(
            set(self.contract["release_validation"]["staging"]["checks"]),
            {"structure", "provenance", "hashes", "categories", "semantics", "symlinks", "root-files", "paths"},
        )
        semantics = intake["delivery_semantics"]
        self.assertEqual(semantics["schema"], "outputs.delivery-semantics.v1")
        self.assertEqual(semantics["coverage"]["state_identifiers"], [state_id for state_id, _ in STATES])
        sealed = self.contract["release_validation"]["sealed"]
        self.assertEqual(sealed["commit"], {"type": "local-git-commit-object", "sha_format": "lowercase-40-hex"})
        self.assertEqual(sealed["release_tree"], {"scope": "entire-release-directory", "comparison": "exact-path-and-byte"})
        self.assertEqual(
            sealed["uncommitted_release"],
            {"may_pass_staging": True, "immutable": False, "delivery_consumable": False},
        )

    def test_case_insensitive_mutable_aliases_are_rejected(self):
        contract_alias = copy.deepcopy(self.contract)
        contract_alias["release_layout"]["alias"] = "CuRrEnT"
        result = validate(contract_alias)
        self.assertNotEqual(result.returncode, 0, result.stdout)


class ReleaseIntakeValidatorTest(unittest.TestCase):
    def valid_manifest(self):
        entries = delivery_entries()
        return {
            "schema": "outputs.release-manifest.v1",
            "release_id": "ge16-20260905-run-001",
            "provenance": {
                "analytics_commit_sha": GIT_SHA,
                "data_commit_sha": GIT_SHA,
                "run_id": "ge16-20260905-run-001",
                "artifact_manifest_sha256": SHA256,
            },
            "artifacts": [
                {"path": entry["source_path"], "category": entry["category"], "sha256": SHA256, "bytes": index + 1}
                for index, entry in enumerate(entries)
            ],
            "delivery_semantics": {
                "schema": "outputs.delivery-semantics.v1",
                "entries": entries,
            },
        }

    def test_complete_release_manifest_is_accepted(self):
        result = validate_intake(self.valid_manifest())
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_delivery_semantics_omissions_and_mismatches_are_rejected(self):
        missing = self.valid_manifest()
        del missing["delivery_semantics"]
        result = validate_intake(missing)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("delivery_semantics", result.stderr)

        mismatch = self.valid_manifest()
        mismatch["delivery_semantics"]["entries"][0]["category"] = "report"
        result = validate_intake(mismatch)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("category", result.stderr)

    def test_delivery_semantics_rejects_duplicate_destinations_and_incomplete_state_coverage(self):
        duplicate_destination = self.valid_manifest()
        entries = duplicate_destination["delivery_semantics"]["entries"]
        entries[1]["destination_path"] = entries[0]["destination_path"]
        result = validate_intake(duplicate_destination)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("destination", result.stderr)

        missing_state_pair = self.valid_manifest()
        entries = missing_state_pair["delivery_semantics"]["entries"]
        missing_state_pair["delivery_semantics"]["entries"] = [
            entry for entry in entries if entry["role"] != "report.state.johor.ms"
        ]
        result = validate_intake(missing_state_pair)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("johor", result.stderr)

    def test_missing_required_provenance_is_rejected(self):
        manifest = self.valid_manifest()
        del manifest["provenance"]["data_commit_sha"]
        result = validate_intake(manifest)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("data_commit_sha", result.stderr)

    def test_missing_artifact_category_is_rejected(self):
        manifest = self.valid_manifest()
        manifest["artifacts"] = [item for item in manifest["artifacts"] if item["category"] != "social"]
        result = validate_intake(manifest)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("social", result.stderr)

    def test_mutable_aliases_and_forbidden_paths_are_rejected(self):
        cases = []

        mutable_release = self.valid_manifest()
        mutable_release["release_id"] = "latest"
        cases.append(("mutable release", mutable_release))

        case_variant_alias = self.valid_manifest()
        case_variant_alias["release_id"] = "LaTeSt"
        cases.append(("case variant mutable release", case_variant_alias))

        website_path = self.valid_manifest()
        website_path["artifacts"][0]["path"] = "artifacts/5_WEBSITES/forecast.json"
        cases.append(("website path", website_path))

        delivery_path = self.valid_manifest()
        delivery_path["artifacts"][0]["path"] = "artifacts/4_DELIVERY/forecast.json"
        cases.append(("delivery path", delivery_path))

        for label, manifest in cases:
            with self.subTest(label=label):
                result = validate_intake(manifest)
                self.assertNotEqual(result.returncode, 0, result.stdout)

    def materialize_release(self, root, include_receipt=True):
        artifacts = root / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        manifest = self.valid_manifest()
        manifest["release_id"] = root.name
        manifest["provenance"]["run_id"] = root.name
        for artifact in manifest["artifacts"]:
            path = root / artifact["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = (artifact["path"] + "\n").encode("utf-8")
            path.write_bytes(payload)
            artifact["sha256"] = hashlib.sha256(payload).hexdigest()
            artifact["bytes"] = len(payload)

        sums = "".join(
            f"{artifact['sha256']}  {artifact['path']}\n" for artifact in manifest["artifacts"]
        )
        (root / "SHA256SUMS").write_text(sums, encoding="utf-8")
        manifest["provenance"]["artifact_manifest_sha256"] = hashlib.sha256(
            sums.encode("utf-8")
        ).hexdigest()
        manifest_bytes = json.dumps(manifest, sort_keys=True).encode("utf-8")
        (root / "manifest.json").write_bytes(manifest_bytes)
        if include_receipt:
            receipt = {
                "schema": "outputs.release-receipt.v1",
                "release_id": manifest["release_id"],
                "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "sha256sums_sha256": hashlib.sha256(sums.encode("utf-8")).hexdigest(),
                "artifacts": [
                    {key: artifact[key] for key in ("path", "sha256", "bytes")}
                    for artifact in manifest["artifacts"]
                ],
            }
            (root / "receipt.json").write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
        return manifest

    def validate_release_dir(self, root, sealed_commit=None):
        command = [sys.executable, str(INTAKE_VALIDATOR), "--release-dir", str(root)]
        if sealed_commit is not None:
            command.extend(("--sealed-commit", sealed_commit))
        return subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
        )

    def initialize_git_repository(self, repository):
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        subprocess.run(["git", "-C", str(repository), "config", "user.email", "tests@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(repository), "config", "user.name", "OUTPUTS validator tests"], check=True)

    def seal_release(self, repository, release_dir, message="seal release"):
        relative_release = release_dir.relative_to(repository)
        subprocess.run(["git", "-C", str(repository), "add", "--", str(relative_release)], check=True)
        subprocess.run(["git", "-C", str(repository), "commit", "-q", "-m", message], check=True)
        return subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

    def test_artifacts_must_be_immutable_and_complete(self):
        cases = []

        missing_hash = self.valid_manifest()
        del missing_hash["artifacts"][0]["sha256"]
        cases.append(("missing hash", missing_hash))

        negative_bytes = self.valid_manifest()
        negative_bytes["artifacts"][0]["bytes"] = -1
        cases.append(("negative bytes", negative_bytes))

        duplicate_path = self.valid_manifest()
        duplicate_path["artifacts"][1]["path"] = duplicate_path["artifacts"][0]["path"]
        cases.append(("duplicate artifact", duplicate_path))

        for label, manifest in cases:
            with self.subTest(label=label):
                result = validate_intake(manifest)
                self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_release_directory_hashes_and_sizes_are_verified(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "releases" / "ge16-20260905-run-001"
            self.materialize_release(root)
            result = self.validate_release_dir(root)
            self.assertEqual(result.returncode, 0, result.stderr)

            (root / "artifacts" / "data" / "forecast.json").write_bytes(b"tampered\n")
            result = self.validate_release_dir(root)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("hash", result.stderr)

    def test_release_directory_rejects_undeclared_root_files(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "releases" / "ge16-20260905-run-001"
            self.materialize_release(root)
            (root / "notes.txt").write_text("not declared", encoding="utf-8")
            result = self.validate_release_dir(root)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("undeclared release root entry", result.stderr)

    def test_release_directory_rejects_symlinked_control_and_artifact_files(self):
        cases = ("manifest.json", "SHA256SUMS", "receipt.json", "artifacts", "artifacts/data/forecast.json")
        for target in cases:
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory) / "releases" / "ge16-20260905-run-001"
                self.materialize_release(root)
                file_path = root / target
                replacement = root / f"real-{file_path.name}"
                file_path.rename(replacement)
                os.symlink(replacement.name, file_path)
                result = self.validate_release_dir(root)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("symlink", result.stderr)

    def test_receipt_is_required_and_staging_allows_coherent_rewrites(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "releases" / "ge16-20260905-run-001"
            self.materialize_release(root, include_receipt=False)
            result = self.validate_release_dir(root)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("receipt.json is required", result.stderr)

            invalid_root = Path(temporary_directory) / "releases" / "ge16-20260905-run-invalid"
            self.materialize_release(invalid_root)
            (invalid_root / "receipt.json").write_text("{}", encoding="utf-8")
            result = self.validate_release_dir(invalid_root)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("receipt", result.stderr)

            manifest = self.materialize_release(
                Path(temporary_directory) / "releases" / "ge16-20260905-run-002"
            )
            rewritten_root = Path(temporary_directory) / "releases" / "ge16-20260905-run-002"
            artifact = manifest["artifacts"][0]
            payload = b"coherently rewritten\n"
            (rewritten_root / artifact["path"]).write_bytes(payload)
            artifact["sha256"] = hashlib.sha256(payload).hexdigest()
            artifact["bytes"] = len(payload)
            sums = "".join(
                f"{entry['sha256']}  {entry['path']}\n" for entry in manifest["artifacts"]
            )
            (rewritten_root / "SHA256SUMS").write_text(sums, encoding="utf-8")
            manifest["provenance"]["artifact_manifest_sha256"] = hashlib.sha256(
                sums.encode("utf-8")
            ).hexdigest()
            manifest_bytes = json.dumps(manifest, sort_keys=True).encode("utf-8")
            (rewritten_root / "manifest.json").write_bytes(manifest_bytes)
            receipt = {
                "schema": "outputs.release-receipt.v1",
                "release_id": manifest["release_id"],
                "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "sha256sums_sha256": hashlib.sha256(sums.encode("utf-8")).hexdigest(),
                "artifacts": [
                    {key: entry[key] for key in ("path", "sha256", "bytes")}
                    for entry in manifest["artifacts"]
                ],
            }
            (rewritten_root / "receipt.json").write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
            result = self.validate_release_dir(rewritten_root)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_sealed_validation_uses_a_git_commit_as_the_immutable_anchor(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "outputs"
            self.initialize_git_repository(repository)
            root = repository / "releases" / "ge16-20260905-run-001"
            manifest = self.materialize_release(root)
            seal = self.seal_release(repository, root)

            result = self.validate_release_dir(root, seal)
            self.assertEqual(result.returncode, 0, result.stderr)

            artifact = manifest["artifacts"][0]
            payload = b"coherently rewritten after sealing\n"
            (root / artifact["path"]).write_bytes(payload)
            artifact["sha256"] = hashlib.sha256(payload).hexdigest()
            artifact["bytes"] = len(payload)
            sums = "".join(f"{entry['sha256']}  {entry['path']}\n" for entry in manifest["artifacts"])
            (root / "SHA256SUMS").write_text(sums, encoding="utf-8")
            manifest["provenance"]["artifact_manifest_sha256"] = hashlib.sha256(sums.encode("utf-8")).hexdigest()
            manifest_bytes = json.dumps(manifest, sort_keys=True).encode("utf-8")
            (root / "manifest.json").write_bytes(manifest_bytes)
            receipt = {
                "schema": "outputs.release-receipt.v1",
                "release_id": manifest["release_id"],
                "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "sha256sums_sha256": hashlib.sha256(sums.encode("utf-8")).hexdigest(),
                "artifacts": [{key: entry[key] for key in ("path", "sha256", "bytes")} for entry in manifest["artifacts"]],
            }
            (root / "receipt.json").write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")

            self.assertEqual(self.validate_release_dir(root).returncode, 0)
            result = self.validate_release_dir(root, seal)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("sealed", result.stderr)

    def test_sealed_validation_requires_an_exact_local_commit_object(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "outputs"
            self.initialize_git_repository(repository)
            root = repository / "releases" / "ge16-20260905-run-001"
            self.materialize_release(root)
            seal = self.seal_release(repository, root)
            tree = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", f"{seal}^{{tree}}"],
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            for label, candidate in (("branch", "HEAD"), ("abbreviation", seal[:12]), ("missing", "0" * 40), ("tree", tree)):
                with self.subTest(label=label):
                    result = self.validate_release_dir(root, candidate)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn("sealed", result.stderr)

    def test_sealed_validation_rejects_a_commit_without_the_release(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "outputs"
            self.initialize_git_repository(repository)
            (repository / "README").write_text("temporary test repository\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repository), "add", "README"], check=True)
            subprocess.run(["git", "-C", str(repository), "commit", "-q", "-m", "base"], check=True)
            base_commit = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "HEAD"],
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            root = repository / "releases" / "ge16-20260905-run-001"
            self.materialize_release(root)

            result = self.validate_release_dir(root, base_commit)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("does not contain release", result.stderr)

    def test_sealed_validation_rejects_extra_and_missing_commit_tree_paths(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "outputs"
            self.initialize_git_repository(repository)
            root = repository / "releases" / "ge16-20260905-run-001"
            self.materialize_release(root)
            seal = self.seal_release(repository, root)

            missing = root / "artifacts" / "data" / "forecast.json"
            missing.unlink()
            result = self.validate_release_dir(root, seal)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("sealed", result.stderr)

            self.materialize_release(root)
            (root / "artifacts" / "extra.bin").write_bytes(b"extra\n")
            result = self.validate_release_dir(root, seal)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("sealed", result.stderr)


if __name__ == "__main__":
    unittest.main()
