"""Import, location, and portability guards for Task 4.3 Batch 2."""

import importlib
import re
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
VERSIONED_MODULES = {
    "tools.figures": (
        "build_figures_vdb", "build_parties_vdb", "build_personnel_vdb",
        "build_remaining_vdbs", "build_vec_map", "update_parties_from_cron",
        "update_personnel_from_cron",
    ),
    "tools.graph": ("build_knowledge_graph",),
    "automation.qa": (
        "audit_dun_composition", "audit_ms_en_sections", "audit_state_number_parity",
    ),
    "automation.reports": (
        "archive_superseded_reports", "md2docx", "state_deepdives",
    ),
}


class VersionedBatch2Tests(unittest.TestCase):
    def test_versioned_modules_import_from_their_tracked_packages(self):
        for package, module_names in VERSIONED_MODULES.items():
            package_root = REPOSITORY_ROOT / package.replace(".", "/")
            for module_name in module_names:
                module = importlib.import_module(package + "." + module_name)
                self.assertTrue(Path(module.__file__).resolve().is_relative_to(package_root))

    def test_versioned_sources_do_not_retain_user_absolute_paths(self):
        absolute_user_path = re.compile("/" + "Users/")
        for package, module_names in VERSIONED_MODULES.items():
            package_root = REPOSITORY_ROOT / package.replace(".", "/")
            for module_name in module_names:
                source = (package_root / (module_name + ".py")).read_text(encoding="utf-8")
                self.assertIsNone(absolute_user_path.search(source), package + "." + module_name)


if __name__ == "__main__":
    unittest.main()
