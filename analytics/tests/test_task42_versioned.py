"""Import, location, portability, and path-agreement guards for Task 4.2."""

import importlib
import re
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SEARCH_MODULES = (
    "search_figures",
    "search_parties",
    "search_personnel",
)
ABSOLUTE_USER_PATH = re.compile("/" + "Users/")


class Task42VersionedTests(unittest.TestCase):
    def test_versioned_modules_import_from_their_tracked_packages(self):
        figures_root = REPOSITORY_ROOT / "tools" / "figures"
        graph_root = REPOSITORY_ROOT / "tools" / "graph"
        for module_name in SEARCH_MODULES:
            module = importlib.import_module("tools.figures." + module_name)
            self.assertTrue(Path(module.__file__).resolve().is_relative_to(figures_root))
        query_module = importlib.import_module("tools.graph.query_graph")
        self.assertTrue(Path(query_module.__file__).resolve().is_relative_to(graph_root))

    def test_versioned_sources_do_not_retain_user_absolute_paths(self):
        sources = [
            REPOSITORY_ROOT / "tools" / "figures" / (module_name + ".py")
            for module_name in SEARCH_MODULES
        ] + [REPOSITORY_ROOT / "tools" / "graph" / "query_graph.py"]
        for source in sources:
            self.assertIsNone(ABSOLUTE_USER_PATH.search(source.read_text(encoding="utf-8")), source)

    def test_search_and_graph_tools_agree_with_the_versioned_builder_paths(self):
        builder = importlib.import_module("tools.graph.build_knowledge_graph")
        query = importlib.import_module("tools.graph.query_graph")
        self.assertEqual(Path(query.ROOT), Path(builder.ROOT))
        self.assertEqual(Path(query.GRAPH).parent, Path(builder.OUT))
        for module_name in SEARCH_MODULES:
            search = importlib.import_module("tools.figures." + module_name)
            self.assertEqual(Path(search.ROOT), Path(builder.ROOT))
            self.assertEqual(Path(search.VEC).parent, Path(builder.FIGURES))
            self.assertEqual(Path(search.META).parent, Path(builder.FIGURES))


if __name__ == "__main__":
    unittest.main()
