"""Contract tests for the Phase 1.1 ANALYTICS migration inventory validator."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
VALIDATOR_PATH = REPOSITORY_ROOT / "automation" / "migration_inventory_validator.py"
MANIFEST_PATH = REPOSITORY_ROOT / "08_HANDOFF" / "phase_1_1_migration_inventory.json"


def load_validator(path=VALIDATOR_PATH):
    spec = importlib.util.spec_from_file_location("migration_inventory_validator", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class MigrationInventoryValidatorTests(unittest.TestCase):
    def test_validator_is_relocated_and_inventory_covers_current_top_level_paths(self):
        validator = load_validator()
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

        self.assertEqual(validator.validate_manifest(manifest, REPOSITORY_ROOT), [])

    def test_default_root_resolution_is_basename_independent(self):
        """A copied validator must validate identical inventory under every final name."""
        source_manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        source_validator = VALIDATOR_PATH.read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory() as temp_dir:
            for repository_name in ("2_ANALYTICS", "renamed-analytics-sandbox"):
                root = Path(temp_dir) / repository_name
                validator_path = root / "automation" / "migration_inventory_validator.py"
                validator_path.parent.mkdir(parents=True)
                validator_path.write_text(source_validator, encoding="utf-8")
                manifest_path = root / "08_HANDOFF" / "phase_1_1_migration_inventory.json"
                manifest_path.parent.mkdir(parents=True)
                manifest_path.write_text(json.dumps(source_manifest), encoding="utf-8")

                for record in source_manifest["top_level_paths"] + source_manifest["explicit_nested_paths"]:
                    source_path = REPOSITORY_ROOT / record["path"]
                    target_path = root / record["path"]
                    if source_path.is_dir():
                        target_path.mkdir(parents=True, exist_ok=True)
                    else:
                        target_path.parent.mkdir(parents=True, exist_ok=True)
                        target_path.touch()

                validator = load_validator(validator_path)
                self.assertEqual(validator.validate_manifest(source_manifest, root), [])
                original_argv = sys.argv
                try:
                    sys.argv = [str(validator_path)]
                    self.assertEqual(validator.main(), 0)
                finally:
                    sys.argv = original_argv

    def test_fails_when_a_discovered_top_level_path_is_unclassified(self):
        validator = load_validator()
        manifest = {
            "schema": "analytics.phase-1.1.migration-inventory.v1",
            "allowed_classifications": ["analytics_code"],
            "top_level_paths": [
                {
                    "path": "known",
                    "classification": "analytics_code",
                    "responsible_writer": "test",
                    "intended_destination": "2_ANALYTICS/known",
                    "inbound_dependencies": ["test"],
                    "outbound_dependencies": [],
                },
            ],
            "explicit_nested_paths": [],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "known").mkdir()
            (root / "unclassified").mkdir()

            errors = validator.validate_manifest(manifest, root)

        self.assertIn("unclassified top-level path: unclassified", errors)

    def test_ignores_only_manifest_declared_environmental_top_level_paths(self):
        validator = load_validator()
        manifest = {
            "schema": "analytics.phase-1.1.migration-inventory.v1",
            "allowed_classifications": ["analytics_code"],
            "validator_environmental_top_level_exclusions": [".DS_Store"],
            "top_level_paths": [
                {
                    "path": "known",
                    "classification": "analytics_code",
                    "responsible_writer": "test",
                    "intended_destination": "2_ANALYTICS/known",
                    "inbound_dependencies": ["test"],
                    "outbound_dependencies": [],
                },
            ],
            "explicit_nested_paths": [],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "known").mkdir()
            (root / ".DS_Store").touch()

            self.assertEqual(validator.validate_manifest(manifest, root), [])

    def test_fails_when_an_explicit_nested_path_has_no_classification(self):
        validator = load_validator()
        manifest = {
            "schema": "analytics.phase-1.1.migration-inventory.v1",
            "allowed_classifications": ["analytics_code"],
            "top_level_paths": [
                {
                    "path": "known",
                    "classification": "analytics_code",
                    "responsible_writer": "test",
                    "intended_destination": "2_ANALYTICS/known",
                    "inbound_dependencies": ["test"],
                    "outbound_dependencies": [],
                },
            ],
            "explicit_nested_paths": [
                {
                    "path": "known/nested",
                    "responsible_writer": "test",
                    "intended_destination": "2_ANALYTICS/known/nested",
                    "inbound_dependencies": ["test"],
                    "outbound_dependencies": [],
                },
            ],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "known" / "nested").mkdir(parents=True)

            errors = validator.validate_manifest(manifest, root)

        self.assertIn("unclassified explicit nested path: known/nested", errors)

    def test_rejects_placeholder_data_repository_destination(self):
        validator = load_validator()
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        records = manifest["top_level_paths"] + manifest["explicit_nested_paths"]
        destinations = [record["intended_destination"] for record in records]

        errors = validator.validate_manifest(manifest, REPOSITORY_ROOT)

        self.assertFalse(
            any("DATA_REPOSITORY_TBD" in destination for destination in destinations),
            "inventory must reject DATA_REPOSITORY_TBD destinations",
        )
        self.assertEqual(errors, [])

    def test_rejects_provisional_output_and_control_plane_placeholders(self):
        validator = load_validator()
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        manifest["destination_conventions"] = dict(manifest["destination_conventions"])
        manifest["destination_conventions"].update(
            {
                "generated_output": "Future-OUTPUTS location is TBD",
                "ops_config": "future OPS requires an OWNER-DECISION",
            }
        )

        errors = validator.validate_manifest(manifest, REPOSITORY_ROOT)

        self.assertIn("provisional inventory language: tbd", errors)
        self.assertIn("provisional inventory language: owner-decision", errors)
        self.assertIn("provisional inventory language: future-outputs", errors)
        self.assertIn("provisional inventory language: future-ops", errors)


if __name__ == "__main__":
    unittest.main()
