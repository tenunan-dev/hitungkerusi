#!/usr/bin/env python3
"""Validate the immutable 3_OUTPUTS repository contract."""

import argparse
import json
import re
import sys
from pathlib import Path


REQUIRED_PROVENANCE = {
    "analytics_commit_sha",
    "data_commit_sha",
    "run_id",
    "artifact_manifest_sha256",
}
REQUIRED_CATEGORIES = {"forecast", "report", "social", "tracking", "state-composition"}
REQUIRED_ARTIFACT_FIELDS = {"path", "category", "sha256", "bytes"}
REQUIRED_SEMANTIC_FIELDS = {
    "role", "source_path", "category", "destination_path", "content_type",
    "content_schema", "content_version",
}
REQUIRED_RECEIPT_FIELDS = {"schema", "release_id", "manifest_sha256", "sha256sums_sha256", "artifacts"}
REQUIRED_RECEIPT_ARTIFACT_FIELDS = {"path", "sha256", "bytes"}
REQUIRED_RELEASE_ROOT_ENTRIES = {"manifest.json", "SHA256SUMS", "receipt.json", "artifacts"}
REQUIRED_STAGING_CHECKS = {"structure", "provenance", "hashes", "categories", "semantics", "symlinks", "root-files", "paths"}
MUTABLE_ALIASES = {"current", "latest"}
STATE_IDENTIFIERS = [
    "johor", "kedah", "kelantan", "melaka", "negeri-sembilan", "pahang",
    "perak", "perlis", "pulau-pinang", "sabah", "sarawak", "selangor",
    "terengganu",
]
STATE_COMPOSITION_IDENTIFIERS = ["melaka", "pahang", "perak", "perlis", "sarawak"]
FORBIDDEN_PATH = re.compile(
    r"(?:^|[\\/])(?:5_)?websites?(?:[\\/]|$)|(?:^|[\\/])(?:4_)?delivery[\\/]", re.IGNORECASE
)


def strings_in(value):
    """Yield every string value from a JSON-compatible tree."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from strings_in(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings_in(item)


def fail(errors, condition, message):
    if not condition:
        errors.append(message)


def mutable_alias_counts(value):
    """Count case-insensitive standalone mutable aliases in contract values."""
    counts = {alias: 0 for alias in MUTABLE_ALIASES}
    for item in strings_in(value):
        for segment in re.split(r"[\\/]", item):
            alias = segment.casefold()
            if alias in counts:
                counts[alias] += 1
    return counts


def validate(contract):
    """Return validation errors; an empty list means the contract is valid."""
    errors = []
    if not isinstance(contract, dict):
        return ["contract must be a JSON object"]

    fail(errors, contract.get("schema") == "outputs.output-contract.v1", "invalid schema")
    fail(errors, contract.get("repository") == "3_OUTPUTS", "repository must be 3_OUTPUTS")
    fail(
        errors,
        contract.get("allowed_inputs") == [
            {"producer": "2_ANALYTICS", "content": "validated-results-only"}
        ],
        "allowed inputs must be 2_ANALYTICS validated results only",
    )

    layout = contract.get("release_layout")
    fail(errors, isinstance(layout, dict), "release_layout must be an object")
    if isinstance(layout, dict):
        fail(errors, layout.get("root") == "releases", "release root must be releases")
        fail(
            errors,
            layout.get("path_template") == "releases/{release_id}/",
            "release path must be releases/{release_id}/",
        )
        fail(
            errors,
            layout.get("artifacts_directory") == "releases/{release_id}/artifacts/",
            "artifacts must be stored under a release directory",
        )
        fail(errors, layout.get("artifact_manifest") == "manifest.json", "artifact manifest must be manifest.json")
        fail(errors, layout.get("hash_manifest") == "SHA256SUMS", "hash manifest must be SHA256SUMS")

    provenance = contract.get("provenance")
    fail(errors, isinstance(provenance, dict), "provenance must be an object")
    if isinstance(provenance, dict):
        fields = provenance.get("required_fields")
        fail(
            errors,
            isinstance(fields, list) and set(fields) == REQUIRED_PROVENANCE and len(fields) == len(REQUIRED_PROVENANCE),
            "provenance must require analytics SHA, 1_DATA SHA, run id, and manifest hash",
        )

    intake = contract.get("release_intake")
    fail(errors, isinstance(intake, dict), "release_intake must be an object")
    if isinstance(intake, dict):
        fail(
            errors,
            intake.get("manifest_schema") == "outputs.release-manifest.v1",
            "release intake must require the release manifest schema",
        )
        fail(
            errors,
            intake.get("manifest_path") == "releases/{release_id}/manifest.json",
            "release intake manifest must be under releases/{release_id}/",
        )
        fail(
            errors,
            intake.get("hash_manifest_path") == "releases/{release_id}/SHA256SUMS",
            "release intake hash manifest must be SHA256SUMS",
        )
        root_entries = intake.get("allowed_root_entries")
        fail(
            errors,
            isinstance(root_entries, list)
            and set(root_entries) == REQUIRED_RELEASE_ROOT_ENTRIES
            and len(root_entries) == len(REQUIRED_RELEASE_ROOT_ENTRIES),
            "release intake must allow only manifest.json, SHA256SUMS, receipt.json, and artifacts at release root",
        )
        fail(
            errors,
            intake.get("receipt_schema") == "outputs.release-receipt.v1",
            "release intake must require the receipt consistency-record schema",
        )
        fail(
            errors,
            intake.get("receipt_path") == "releases/{release_id}/receipt.json",
            "release intake receipt must be under releases/{release_id}/",
        )
        receipt = intake.get("receipt")
        fail(errors, isinstance(receipt, dict), "release intake receipt must be an object")
        if isinstance(receipt, dict):
            fields = receipt.get("required_fields")
            fail(
                errors,
                isinstance(fields, list) and set(fields) == REQUIRED_RECEIPT_FIELDS and len(fields) == len(REQUIRED_RECEIPT_FIELDS),
                "release receipt must require its complete consistency baseline",
            )
            artifact_fields = receipt.get("artifact_required_fields")
            fail(
                errors,
                isinstance(artifact_fields, list)
                and set(artifact_fields) == REQUIRED_RECEIPT_ARTIFACT_FIELDS
                and len(artifact_fields) == len(REQUIRED_RECEIPT_ARTIFACT_FIELDS),
                "release receipt artifacts must require path, SHA-256, and bytes",
            )
        intake_provenance = intake.get("provenance")
        fail(errors, isinstance(intake_provenance, dict), "release intake provenance must be an object")
        if isinstance(intake_provenance, dict):
            fields = intake_provenance.get("required_fields")
            fail(
                errors,
                isinstance(fields, list) and set(fields) == REQUIRED_PROVENANCE and len(fields) == len(REQUIRED_PROVENANCE),
                "release intake must require all provenance fields",
            )
            fail(
                errors,
                intake_provenance.get("artifact_manifest_sha256_target") == "SHA256SUMS",
                "artifact manifest SHA must identify SHA256SUMS",
            )
        artifacts = intake.get("artifacts")
        fail(errors, isinstance(artifacts, dict), "release intake artifacts must be an object")
        if isinstance(artifacts, dict):
            fail(
                errors,
                artifacts.get("directory") == "releases/{release_id}/artifacts/",
                "release intake artifacts must be under the release directory",
            )
            fields = artifacts.get("required_fields")
            fail(
                errors,
                isinstance(fields, list) and set(fields) == REQUIRED_ARTIFACT_FIELDS and len(fields) == len(REQUIRED_ARTIFACT_FIELDS),
                "release intake artifacts must require path, category, SHA-256, and bytes",
            )
            categories = artifacts.get("required_categories")
            fail(
                errors,
                isinstance(categories, list) and set(categories) == REQUIRED_CATEGORIES and len(categories) == len(REQUIRED_CATEGORIES),
                "release intake must require forecast, report, social, tracking, and state-composition artifacts",
            )
            fail(
                errors,
                artifacts.get("additional_properties") is False,
                "release intake artifacts must not allow mutable undeclared fields",
            )
        semantics = intake.get("delivery_semantics")
        fail(errors, isinstance(semantics, dict), "release intake delivery_semantics must be an object")
        if isinstance(semantics, dict):
            fail(
                errors,
                semantics.get("manifest_section") == "delivery_semantics"
                and semantics.get("schema") == "outputs.delivery-semantics.v1",
                "release intake must require the delivery_semantics manifest section and schema",
            )
            fields = semantics.get("entry_required_fields")
            fail(
                errors,
                isinstance(fields, list) and set(fields) == REQUIRED_SEMANTIC_FIELDS and len(fields) == len(REQUIRED_SEMANTIC_FIELDS),
                "delivery_semantics entries must require complete semantic mapping fields",
            )
            fail(
                errors,
                semantics.get("source_path") == {
                    "prefix": "artifacts/", "must_match_exactly_one_declared_artifact": True,
                },
                "delivery semantics sources must match exactly one declared artifact",
            )
            fail(
                errors,
                semantics.get("destination_path") == {
                    "kind": "safe-relative-delivery-path", "must_be_unique": True,
                    "forbid_reserved_domains": True,
                },
                "delivery semantics destinations must be unique safe relative paths",
            )
            coverage = semantics.get("coverage")
            fail(errors, isinstance(coverage, dict), "delivery semantics coverage must be an object")
            if isinstance(coverage, dict):
                fail(
                    errors,
                    coverage.get("data_payloads") == ["app-data", "forecast", "forecast-engine", "scenarios"],
                    "delivery semantics must cover all generated data payloads",
                )
                fail(
                    errors,
                    coverage.get("federal_report_languages") == ["en", "ms"]
                    and coverage.get("state_report_languages") == ["en", "ms"],
                    "delivery semantics must cover English and Malay reports",
                )
                fail(
                    errors,
                    coverage.get("state_identifiers") == STATE_IDENTIFIERS,
                    "delivery semantics must declare exactly the 13 state identifiers",
                )
                fail(
                    errors,
                    coverage.get("state_composition_identifiers") == STATE_COMPOSITION_IDENTIFIERS,
                    "delivery semantics must declare downstream state-composition coverage",
                )
                fail(
                    errors,
                    coverage.get("required_payload_roles") == ["social.payload", "tracking.payload"],
                    "delivery semantics must require social and tracking payload roles",
                )
            fail(
                errors,
                semantics.get("json_metadata") == {
                    "content_type": "application/json", "require_content_schema": True,
                    "require_content_version": True,
                },
                "delivery semantics must require JSON schema and version metadata",
            )
            fail(
                errors,
                semantics.get("artifact_coverage") == "every-declared-artifact-exactly-once",
                "delivery semantics must map every declared artifact exactly once",
            )

    release_validation = contract.get("release_validation")
    fail(errors, isinstance(release_validation, dict), "release_validation must be an object")
    if isinstance(release_validation, dict):
        staging = release_validation.get("staging")
        fail(errors, isinstance(staging, dict), "staging validation must be an object")
        if isinstance(staging, dict):
            fail(errors, staging.get("required") is True, "staging validation must be required")
            checks = staging.get("checks")
            fail(
                errors,
                isinstance(checks, list) and set(checks) == REQUIRED_STAGING_CHECKS and len(checks) == len(REQUIRED_STAGING_CHECKS),
                "staging validation must check structure, provenance, hashes, categories, symlinks, root files, and paths",
            )

        sealed = release_validation.get("sealed")
        fail(errors, isinstance(sealed, dict), "sealed validation must be an object")
        if isinstance(sealed, dict):
            fail(errors, sealed.get("required") is True, "sealed validation must be required")
            fail(
                errors,
                sealed.get("commit") == {"type": "local-git-commit-object", "sha_format": "lowercase-40-hex"},
                "sealed validation must require an exact local Git commit object",
            )
            fail(
                errors,
                sealed.get("release_tree") == {"scope": "entire-release-directory", "comparison": "exact-path-and-byte"},
                "sealed validation must compare the entire release directory path-for-path and byte-for-byte",
            )
            fail(
                errors,
                sealed.get("uncommitted_release") == {
                    "may_pass_staging": True,
                    "immutable": False,
                    "delivery_consumable": False,
                },
                "uncommitted releases must be staging-only, non-immutable, and unavailable to 4_DELIVERY",
            )
            fail(
                errors,
                sealed.get("delivery") == {"requires_sealed_validation": True},
                "4_DELIVERY must require sealed validation",
            )

    immutability = contract.get("immutability")
    fail(errors, isinstance(immutability, dict), "immutability must be an object")
    if isinstance(immutability, dict):
        fail(errors, immutability.get("sealed_releases_append_only") is True, "sealed releases must be append-only")
        fail(errors, immutability.get("unsealed_staging_releases_immutable") is False, "unsealed staging releases must not be declared immutable")
        fail(errors, immutability.get("overwrite_sealed_release") is False, "sealed release overwrite must be disabled")
        forbidden = immutability.get("mutable_aliases_forbidden")
        fail(
            errors,
            isinstance(forbidden, list) and set(forbidden) == MUTABLE_ALIASES and len(forbidden) == len(MUTABLE_ALIASES),
            "current and latest aliases must be forbidden",
        )

    access = contract.get("access")
    fail(errors, isinstance(access, dict), "access must be an object")
    if isinstance(access, dict):
        website_access = access.get("website_access")
        fail(
            errors,
            website_access == {"read": False, "write": False},
            "website reads and writes must be prohibited",
        )
        fail(errors, access.get("direct_deployment") is False, "direct deployment must be prohibited")
        fail(
            errors,
            access.get("allowed_downstream_readers") == ["4_DELIVERY"],
            "4_DELIVERY must be the sole downstream reader",
        )

    for value in strings_in(contract):
        fail(errors, FORBIDDEN_PATH.search(value) is None, "website and 4_DELIVERY paths are prohibited")
    alias_counts = mutable_alias_counts(contract)
    for alias, count in alias_counts.items():
        fail(
            errors,
            count == 1,
            f"mutable release alias {alias!r} is prohibited outside mutable_aliases_forbidden",
        )
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("contract", nargs="?", default="output-contract.json")
    parser.add_argument("--stdin", action="store_true", help="read a contract from standard input")
    args = parser.parse_args()
    try:
        contract = json.load(sys.stdin) if args.stdin else json.loads(Path(args.contract).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"invalid contract input: {exc}", file=sys.stderr)
        return 2

    errors = validate(contract)
    if errors:
        for error in errors:
            print(f"invalid output contract: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
