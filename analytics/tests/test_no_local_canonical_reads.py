"""Regression guards for the Phase 4.4 canonical-data cutover."""

import ast
import importlib
import subprocess
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = REPOSITORY_ROOT.parent / "1_DATA"
MOVED_DATA_COLLECTORS = {
    "automation/collection/__init__.py": "scripts/collect/__init__.py",
    "automation/collection/track_ge16_candidates.py": "scripts/collect/track_ge16_candidates.py",
    "automation/collection/track_ge16_news.py": "scripts/collect/track_ge16_news.py",
    "automation/collection/track_ge16_polls.py": "scripts/collect/track_ge16_polls.py",
}
DEFERRED_CONFLICTS = {
    "automation/state_analysis/build_narrative_scenarios.py": {
        "DEFERRED_FIGURES_ROOT", "DEFERRED_GRAPH_PATH",
    },
    "automation/state_analysis/build_state_narrative_scenarios.py": {"DEFERRED_FIGURES_ROOT"},
    "tools/figures/build_figures_vdb.py": {"DEFERRED_FIGURES_ROOT"},
    "tools/figures/build_parties_vdb.py": {"DEFERRED_FIGURES_ROOT"},
    "tools/figures/build_personnel_vdb.py": {"DEFERRED_FIGURES_ROOT"},
    "tools/figures/build_remaining_vdbs.py": {"DEFERRED_FIGURES_ROOT", "DEFERRED_GRAPH_PATH"},
    "tools/figures/build_vec_map.py": {"DEFERRED_FIGURES_ROOT"},
    "tools/figures/search_figures.py": {"DEFERRED_FIGURES_ROOT"},
    "tools/figures/search_parties.py": {"DEFERRED_FIGURES_ROOT"},
    "tools/figures/search_personnel.py": {"DEFERRED_FIGURES_ROOT"},
    "tools/figures/update_parties_from_cron.py": {"DEFERRED_FIGURES_ROOT"},
    "tools/figures/update_personnel_from_cron.py": {"DEFERRED_FIGURES_ROOT"},
    "tools/graph/build_knowledge_graph.py": {"DEFERRED_FIGURES_ROOT", "DEFERRED_GRAPH_ROOT"},
    "tools/graph/query_graph.py": {"DEFERRED_GRAPH_PATH"},
}


def tracked_python_sources():
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", "automation", "tools"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    )
    for raw_path in result.stdout.split(b"\0"):
        if not raw_path:
            continue
        relative = raw_path.decode("utf-8")
        if relative.endswith(".py") and not relative.startswith("automation/tests/"):
            source = REPOSITORY_ROOT / relative
            if not source.exists():
                destination = MOVED_DATA_COLLECTORS.get(relative)
                if destination is None:
                    raise AssertionError(f"Tracked Python source is missing: {relative}")
                if not (DATA_ROOT / destination).is_file():
                    raise AssertionError(
                        f"Moved collector is missing from DATA: {relative} -> {destination}"
                    )
                continue
            yield relative, source


def deferred_literals(tree):
    allowed = {}
    for statement in tree.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        names = {target.id for target in targets if isinstance(target, ast.Name)}
        if not names:
            continue
        for child in ast.walk(statement.value):
            if isinstance(child, ast.Constant) and isinstance(child.value, str) and "01_RESEARCH" in child.value:
                allowed.setdefault(child.lineno, set()).update(names)
    return allowed


class NoLocalCanonicalReadTests(unittest.TestCase):
    def test_only_documented_deferred_constants_retain_local_research_literals(self):
        for relative, source in tracked_python_sources():
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=relative)
            allowed = deferred_literals(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                if "01_RESEARCH" not in node.value:
                    continue
                self.assertIn(relative, DEFERRED_CONFLICTS, (relative, node.value))
                self.assertTrue(
                    allowed.get(node.lineno, set()) & DEFERRED_CONFLICTS[relative],
                    f"{relative}:{node.lineno} is not a documented deferred constant",
                )

    def test_every_declared_canonical_data_input_exists_now(self):
        for _, source in tracked_python_sources():
            namespace = {}
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for statement in tree.body:
                if isinstance(statement, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == "REQUIRED_DATA_RELATIVES"
                    for target in statement.targets
                ):
                    namespace = ast.literal_eval(statement.value)
                    break
            for relative in namespace:
                self.assertTrue((DATA_ROOT / relative).exists(), f"Missing DATA input: {relative}")

    def test_state_analysis_required_data_paths_fail_loudly_when_data_is_missing(self):
        modules = (
            "build_boundary_swings",
            "build_narrative_scenarios",
            "build_state_narrative_scenarios",
            "build_state_prn",
        )
        missing_root = REPOSITORY_ROOT / "missing-data-root-for-test"
        for module_name in modules:
            module = importlib.import_module("automation.state_analysis." + module_name)
            original = module.DATA_ROOT
            try:
                module.DATA_ROOT = missing_root
                with self.assertRaises(FileNotFoundError):
                    module.canonical_path("derived", "not-present.csv")
            finally:
                module.DATA_ROOT = original


if __name__ == "__main__":
    unittest.main()
