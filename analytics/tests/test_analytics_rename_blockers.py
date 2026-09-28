"""Regression coverage for the final 2_ANALYTICS rename blockers."""

import json
import subprocess
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EXPLORER_ROOT = REPOSITORY_ROOT / "GE16-Graph-Explorer"
HARD_CODED_HERMES_PATH = (
    "/Users/faisal.muthalib/Documents/HermesWorkFolder/"
    "Malaysia General Election v2/HERMES"
)

def _working_tree_paths():
    completed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    )
    return [Path(path) for path in completed.stdout.decode("utf-8").split("\0") if path]


def _active_executable_or_configuration_paths():
    for relative_path in _working_tree_paths():
        path = REPOSITORY_ROOT / relative_path
        if not path.is_file():
            continue
        if relative_path.parts[0] == ".git":
            continue
        # Scanning every active text file is deliberately broader than only
        # executable/config/test suffixes, so new extensionless text cannot
        # become a rename escape hatch.
        if b"\0" not in path.read_bytes():
            yield relative_path


def test_graph_explorer_launchers_derive_their_locations():
    """Launchers must work when the repository directory is renamed 2_ANALYTICS."""
    launch_script = (EXPLORER_ROOT / "launch.sh").read_text(encoding="utf-8")
    apple_script = (EXPLORER_ROOT / "explorer-launcher.applescript").read_text(
        encoding="utf-8"
    )

    assert HARD_CODED_HERMES_PATH not in launch_script
    assert "BASH_SOURCE[0]" in launch_script
    assert "dirname" in launch_script

    assert HARD_CODED_HERMES_PATH not in apple_script
    assert "path to me" in apple_script
    assert "container of (path to me)" in apple_script


def test_migration_inventory_adopts_this_repository_as_analytics():
    """Governance records the settled repository identity and origin."""
    inventory_path = REPOSITORY_ROOT / "08_HANDOFF/phase_1_1_migration_inventory.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))

    assert inventory["destination_conventions"]["analytics_code"] == (
        "this repository is the adopted 2_ANALYTICS repository "
        "(origin: https://github.com/tenunan-dev/ge16-analytics.git)"
    )
    assert not any(
        "owner decision" in json.dumps(blocker, sort_keys=True).lower()
        for blocker in inventory["blockers"]
    )


def test_migration_inventory_records_settled_cross_repository_contracts():
    """1_DATA, 3_OUTPUTS, OPS, and retained local history have final dispositions."""
    inventory_path = REPOSITORY_ROOT / "08_HANDOFF/phase_1_1_migration_inventory.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    contracts = inventory["runtime_contracts"]

    assert contracts["canonical_data"]["repository"] == "sibling 1_DATA"
    assert contracts["canonical_data"]["runtime_status"] == "canonical runtime input"
    assert contracts["local_01_research"]["runtime_status"] == (
        "ignored legacy input tree; never canonical"
    )
    assert contracts["local_01_research"]["filesystem_evidence"] == {
        "exists": True,
        "git_ignore_rule": ".gitignore:8:01_RESEARCH/",
        "tracked_paths": 0,
    }
    assert contracts["local_01_research"]["versioned_reader_scan"][
        "zero_versioned_readers"
    ] is False
    assert contracts["outputs"]["contract"] == "sealed immutable intake/delivery-semantics"
    assert contracts["ops"]["contract"] == "versioned locked control-plane"
    assert contracts["archival_manual_remnants"]["runtime_status"] == "non-runtime"
    assert contracts["archival_manual_remnants"]["disposition"] == "retained"
    assert inventory["blockers"], "unpassed rename gates must remain explicit"
    assert {
        blocker["gate"] for blocker in inventory["blockers"]
    } >= {
        "1_DATA reads",
        "archive and duplicate disposition",
        "staging contract",
        "Git parity",
    }


def test_agents_document_the_only_controlled_delivery_promotion_exception():
    instructions = (REPOSITORY_ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert "Do not directly edit ignored legacy inputs" in instructions
    assert "forecast/report" in instructions
    assert "output trees" in instructions
    assert "Ordinary 2_ANALYTICS tasks cannot mutate 4_DELIVERY" in instructions
    assert "Hermes/OPS release-controller" in instructions
    assert "exact sealed 3_OUTPUTS release ID" in instructions
    assert "40-character sealed 3_OUTPUTS commit" in instructions
    assert "rollback/hash gates" in instructions
    assert "may not read mutable 2_ANALYTICS-generated artifacts directly" in instructions
    assert "may never write 5_WEBSITES, deployments, or cron" in instructions


def test_agents_document_the_never_write_domains():
    instructions = (REPOSITORY_ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert "You MUST NEVER write to:" in instructions
    assert "sibling `1_DATA`" in instructions
    assert "sibling `3_OUTPUTS`" in instructions
    assert "sibling `4_DELIVERY`" in instructions
    assert "sibling `5_WEBSITES`" in instructions
    assert "live cron" in instructions
    assert "stop and report" in instructions


def test_no_active_executable_or_configuration_hard_codes_current_root():
    """The fixture is pieced together, so all active text may be scanned."""
    offenders = []
    for relative_path in _active_executable_or_configuration_paths():
        contents = (REPOSITORY_ROOT / relative_path).read_text(
            encoding="utf-8", errors="ignore"
        )
        if HARD_CODED_HERMES_PATH in contents:
            offenders.append(str(relative_path))

    assert not offenders, "active files hard-code the current root: {}".format(offenders)


def test_hard_coded_path_scan_includes_this_test_file():
    assert Path("automation/tests/test_analytics_rename_blockers.py") in set(
        _active_executable_or_configuration_paths()
    )


def test_no_active_claude_configuration_or_instructions_remain_tracked():
    active_working_tree_paths = {
        str(path) for path in _working_tree_paths() if (REPOSITORY_ROOT / path).exists()
    }

    assert "CLAUDE.md" not in active_working_tree_paths
    assert not any(
        path == ".claude" or path.startswith(".claude/")
        for path in active_working_tree_paths
    )
    assert not (REPOSITORY_ROOT / "CLAUDE.md").exists()
