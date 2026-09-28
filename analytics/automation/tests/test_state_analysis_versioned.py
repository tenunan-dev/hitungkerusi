"""Import and provenance guards for versioned state-analysis builders."""

import importlib
import re
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
STATE_ANALYSIS_ROOT = REPOSITORY_ROOT / "automation" / "state_analysis"
VERSIONED_MODULES = (
    "build_state_prn",
    "build_boundary_swings",
    "build_narrative_scenarios",
    "build_state_narrative_scenarios",
    "build_state_prn_trend",
)


class VersionedStateAnalysisTests(unittest.TestCase):
    def test_versioned_state_analysis_modules_import_from_the_tracked_subpackage(self):
        for module_name in VERSIONED_MODULES:
            module = importlib.import_module("automation.state_analysis." + module_name)
            self.assertTrue(Path(module.__file__).resolve().is_relative_to(STATE_ANALYSIS_ROOT))

    def test_versioned_state_analysis_sources_do_not_retain_user_absolute_paths(self):
        absolute_user_path = re.compile(r"/Users/")
        for module_name in VERSIONED_MODULES:
            source = (STATE_ANALYSIS_ROOT / (module_name + ".py")).read_text(encoding="utf-8")
            self.assertIsNone(absolute_user_path.search(source), module_name)
