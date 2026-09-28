"""Validate the deterministic ANALYTICS Phase 1.1 migration inventory."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


REQUIRED_RECORD_FIELDS = {
    "path",
    "classification",
    "responsible_writer",
    "intended_destination",
    "inbound_dependencies",
    "outbound_dependencies",
}

PROVISIONAL_LANGUAGE_PATTERNS = {
    "tbd": re.compile(r"(?:^|[^a-z0-9])tbd(?:$|[^a-z0-9])", re.IGNORECASE),
    "owner-decision": re.compile(r"owner[-_ ]+decision", re.IGNORECASE),
    "future-outputs": re.compile(r"future[-_ ]+outputs", re.IGNORECASE),
    "future-ops": re.compile(r"future[-_ ]+ops", re.IGNORECASE),
}


def validate_manifest(manifest: dict[str, Any], repo_root: Path) -> list[str]:
    """Return deterministic validation errors; an empty list is a valid inventory."""
    errors: list[str] = []
    manifest_text = json.dumps(manifest, sort_keys=True)
    for label, pattern in PROVISIONAL_LANGUAGE_PATTERNS.items():
        if pattern.search(manifest_text):
            errors.append("provisional inventory language: {}".format(label))
    allowed = set(manifest.get("allowed_classifications", []))
    if not allowed:
        errors.append("missing allowed_classifications")

    top_level_records = manifest.get("top_level_paths", [])
    explicit_records = manifest.get("explicit_nested_paths", [])
    environmental_exclusions = manifest.get("validator_environmental_top_level_exclusions", [])
    if not isinstance(top_level_records, list):
        return errors + ["top_level_paths must be a list"]
    if not isinstance(explicit_records, list):
        return errors + ["explicit_nested_paths must be a list"]
    if not isinstance(environmental_exclusions, list) or not all(
        isinstance(path, str) and path and "/" not in path
        for path in environmental_exclusions
    ):
        return errors + ["validator_environmental_top_level_exclusions must be a list of top-level paths"]

    def validate_records(records: list[Any], label: str, require_nested: bool) -> set[str]:
        paths: set[str] = set()
        for record in records:
            if not isinstance(record, dict):
                errors.append("invalid {} record".format(label))
                continue
            path = record.get("path")
            if not isinstance(path, str) or not path:
                errors.append("missing path in {} record".format(label))
                continue
            if path in paths:
                errors.append("duplicate {} path: {}".format(label, path))
            paths.add(path)
            if require_nested and "/" not in path:
                errors.append("explicit nested path is not nested: {}".format(path))
            if not require_nested and "/" in path:
                errors.append("top-level path is nested: {}".format(path))
            for field in REQUIRED_RECORD_FIELDS - {"path", "classification"}:
                if field not in record or record[field] in (None, ""):
                    errors.append("missing {} for {} path: {}".format(field, label, path))
            classification = record.get("classification")
            if classification in (None, ""):
                errors.append("unclassified {} path: {}".format(label, path))
            elif classification not in allowed:
                errors.append("invalid classification for {}: {}".format(path, classification))
            if not (repo_root / path).exists():
                errors.append("manifest path does not exist: {}".format(path))
        return paths

    recorded_top_level = validate_records(top_level_records, "top-level", False)
    validate_records(explicit_records, "explicit nested", True)
    discovered_top_level = {
        entry.name for entry in repo_root.iterdir() if entry.name not in environmental_exclusions
    }

    for path in sorted(discovered_top_level - recorded_top_level):
        errors.append("unclassified top-level path: {}".format(path))
    for path in sorted(recorded_top_level - discovered_top_level):
        errors.append("manifest top-level path does not exist: {}".format(path))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    repository_root = Path(__file__).resolve().parents[1]
    parser.add_argument(
        "--manifest",
        type=Path,
        default=repository_root / "08_HANDOFF" / "phase_1_1_migration_inventory.json",
    )
    parser.add_argument("--repo-root", type=Path, default=repository_root)
    args = parser.parse_args()
    errors = validate_manifest(json.loads(args.manifest.read_text(encoding="utf-8")), args.repo_root)
    if errors:
        print("INVALID")
        print("\n".join(errors))
        return 1
    print("VALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
