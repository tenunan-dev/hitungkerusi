"""Contract coverage for the versioned Graph Explorer pipeline."""

import ast
import importlib.util
import re
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
GRAPH_TOOLS = REPOSITORY_ROOT / "tools" / "graph"
BUILDER = GRAPH_TOOLS / "build_knowledge_graph.py"
QUERY_TOOL = GRAPH_TOOLS / "query_graph.py"
FIGURE_TOOLS = REPOSITORY_ROOT / "tools" / "figures"
EXPLORER_BUILDER = REPOSITORY_ROOT / "tools" / "graph-explorer" / "build_data.py"
VEC_MAP_BUILDER = FIGURE_TOOLS / "build_vec_map.py"
VEC_MAP_PAGE = REPOSITORY_ROOT / "tools" / "graph-explorer" / "vec-map.html"
STATE_ANALYSIS = REPOSITORY_ROOT / "automation" / "state_analysis"
# The sole permitted legacy-path form is immutable source provenance, never runtime logic.
STATE_ANALYSIS_LEGACY_RESEARCH_ALLOWANCES = (
    re.compile(r"^# Provenance: original path 01_RESEARCH(?:/[^;]+)?;.*$"),
)
SCENARIO_WORK_ARTIFACT_FAMILIES = {
    "scenario configuration": (
        ("work/scenarios/projection_scenarios.json", (
            REPOSITORY_ROOT / "02_FORECAST" / "engine" / "forecast_engine.py",
            REPOSITORY_ROOT / "02_FORECAST" / "engine" / "report_builder.py",
            REPOSITORY_ROOT / "02_FORECAST" / "engine" / "state_report_builder.py",
            REPOSITORY_ROOT / "automation" / "state_analysis" / "build_state_prn.py",
            REPOSITORY_ROOT / "automation" / "delivery" / "build_app_data.py",
        )),
        ("work/scenarios/scenario_meta.json", (
            REPOSITORY_ROOT / "02_FORECAST" / "engine" / "forecast_engine.py",
            REPOSITORY_ROOT / "02_FORECAST" / "engine" / "report_builder.py",
            REPOSITORY_ROOT / "automation" / "state_analysis" / "build_state_prn.py",
            REPOSITORY_ROOT / "tools" / "figures" / "build_remaining_vdbs.py",
            REPOSITORY_ROOT / "automation" / "outputs" / "build_release_payloads.py",
            REPOSITORY_ROOT / "automation" / "delivery" / "build_app_data.py",
        )),
    ),
    "analyst narratives": (
        ("work/scenarios/narrative_scenarios.json", (
            REPOSITORY_ROOT / "automation" / "state_analysis" / "build_narrative_scenarios.py",
            REPOSITORY_ROOT / "tools" / "figures" / "build_remaining_vdbs.py",
            REPOSITORY_ROOT / "tools" / "graph" / "build_knowledge_graph.py",
        )),
    ),
    "state narratives": (
        ("work/scenarios/state-narrative-scenarios.json", (
            REPOSITORY_ROOT / "automation" / "state_analysis" / "build_state_narrative_scenarios.py",
            REPOSITORY_ROOT / "automation" / "state_analysis" / "build_state_prn.py",
        )),
    ),
    "state fingerprints": (
        ("work/figures/state_fingerprints.json", (REPOSITORY_ROOT / "02_FORECAST" / "engine" / "state_report_builder.py",)),
    ),
    "PRN history": (
        ("work/reports/prn-history", (REPOSITORY_ROOT / "automation" / "state_analysis" / "build_state_prn_trend.py",)),
    ),
}


def load_builder_module():
    spec = importlib.util.spec_from_file_location("graph_explorer_contract_builder", BUILDER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_query_module():
    spec = importlib.util.spec_from_file_location("graph_explorer_contract_query", QUERY_TOOL)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_explorer_builder_module():
    spec = importlib.util.spec_from_file_location("graph_explorer_data_builder", EXPLORER_BUILDER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_vec_map_builder_module():
    spec = importlib.util.spec_from_file_location("graph_explorer_vec_map_builder", VEC_MAP_BUILDER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _opens_for_write(source_path: Path):
    """Yield source snippets for function-local ``open(..., write-mode)`` calls."""
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(source_path))
    for function in (node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))):
        for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
            if not isinstance(call.func, ast.Name) or call.func.id != "open":
                continue
            mode = call.args[1] if len(call.args) > 1 else next(
                (keyword.value for keyword in call.keywords if keyword.arg == "mode"), None
            )
            if isinstance(mode, ast.Constant) and isinstance(mode.value, str) and any(
                flag in mode.value for flag in ("w", "a", "x", "+")
            ):
                yield source_path, ast.get_source_segment(source, call.args[0]) or ""


class GraphExplorerContractTests(unittest.TestCase):
    def test_explorer_bundles_stage_under_work(self):
        data_builder = load_explorer_builder_module()
        vec_map_builder = load_vec_map_builder_module()
        expected = (REPOSITORY_ROOT / "work" / "graph-explorer").resolve()
        self.assertEqual(Path(data_builder.OUT).resolve(), expected)
        self.assertEqual(Path(vec_map_builder.OUT).resolve(), expected / "vec_map_data.js")

    def test_explorer_builders_never_write_into_their_source_directory(self):
        source_dir = (REPOSITORY_ROOT / "tools" / "graph-explorer").resolve()
        for source_path in (EXPLORER_BUILDER, VEC_MAP_BUILDER):
            for _, target_expression in _opens_for_write(source_path):
                self.assertNotIn("__file__", target_expression)
                self.assertNotIn(str(source_dir), target_expression)
        self.assertNotIn(
            'OUT = os.path.dirname(os.path.abspath(__file__))',
            EXPLORER_BUILDER.read_text(encoding="utf-8"),
        )
        self.assertNotIn(
            '"GE16-Graph-Explorer", "vec_map_data.js"',
            VEC_MAP_BUILDER.read_text(encoding="utf-8"),
        )

    def test_explorer_builder_stages_static_pages_beside_bundles(self):
        builder = load_explorer_builder_module()
        original = builder.SRC_GRAPH, builder.SRC_FIG, builder.OUT
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                work_root = Path(temp_dir)
                graph_dir = work_root / "graph"
                figures_dir = work_root / "figures"
                out_dir = work_root / "graph-explorer"
                graph_dir.mkdir()
                figures_dir.mkdir()
                (graph_dir / "ge16-knowledge-graph.json").write_text(
                    '{"nodes": {"person:1": {"id": "person:1", "name": "Test"}}, "edges": []}',
                    encoding="utf-8",
                )
                meta = '{"items": [{"name": "Test", "roles": []}]}'
                for name in (
                    "figures", "parties", "personnel", "seats", "news", "scenarios", "clusters"
                ):
                    (figures_dir / f"ge16-{name}-meta.json").write_text(meta, encoding="utf-8")
                builder.SRC_GRAPH = str(graph_dir / "ge16-knowledge-graph.json")
                builder.SRC_FIG = str(figures_dir)
                builder.OUT = str(out_dir)
                builder.main()
                for page in ("index.html", "vec-map.html"):
                    self.assertEqual(
                        (out_dir / page).read_text(encoding="utf-8"),
                        (EXPLORER_BUILDER.parent / page).read_text(encoding="utf-8"),
                    )
        finally:
            builder.SRC_GRAPH, builder.SRC_FIG, builder.OUT = original

    def test_vec_map_escapes_all_generated_markup_values(self):
        source = VEC_MAP_PAGE.read_text(encoding="utf-8")
        self.assertRegex(source, r"function esc\(s\).*replace\(/&/g")
        markup_region = source[source.index("function buildLegend()") : source.index("function moveTip(")]
        for expression in ("kv", "p.name", "p.bloc", "p.party", "p.role"):
            self.assertIn(f"esc({expression})", markup_region)

    def test_builder_output_is_under_work_graph(self):
        builder = load_builder_module()
        self.assertEqual(
            Path(builder.OUT).resolve(),
            (REPOSITORY_ROOT / "work" / "graph").resolve(),
        )

    def test_builder_has_no_legacy_research_root_reference(self):
        self.assertNotIn("01_RESEARCH", BUILDER.read_text(encoding="utf-8"))

    def test_query_tool_reads_the_contracted_graph_artifact(self):
        query_tool = load_query_module()
        self.assertEqual(
            Path(query_tool.GRAPH).resolve(),
            (REPOSITORY_ROOT / "work" / "graph" / "ge16-knowledge-graph.json").resolve(),
        )

    def test_versioned_figure_producers_cover_working_graph_inputs(self):
        expected_outputs = {
            "build_personnel_vdb.py": "ge16-personnel.json",
            "build_parties_vdb.py": "ge16-parties.json",
        }
        for filename, output_name in expected_outputs.items():
            source = (REPOSITORY_ROOT / "tools" / "figures" / filename).read_text(encoding="utf-8")
            self.assertIn(' / "work" / "figures"', source)
            self.assertIn(output_name, source)
            self.assertIn("os.makedirs(OUT_DIR, exist_ok=True)", source)

    def test_graph_tools_never_open_sibling_data_for_writing(self):
        writes = list(_opens_for_write(path) for path in GRAPH_TOOLS.glob("*.py"))
        flattened = [item for file_writes in writes for item in file_writes]
        for source_path, target_expression in flattened:
            self.assertNotIn(
                "DATA_ROOT",
                target_expression,
                f"{source_path} opens sibling DATA for writing: {target_expression}",
            )

    def test_runtime_figure_and_graph_tools_have_no_legacy_research_references(self):
        """Allow only metadata records and provenance comments naming original paths."""
        sources = [
            *FIGURE_TOOLS.glob("*.py"),
            *GRAPH_TOOLS.glob("*.py"),
            FIGURE_TOOLS / "versioning_metadata.json",
            GRAPH_TOOLS / "versioning_metadata.json",
            EXPLORER_BUILDER,
        ]
        allowed_provenance = re.compile(
            r"^# Provenance: original path 01_RESEARCH(?:/[^;]+)?;.*$"
        )
        offenders = []
        for source_path in sources:
            for line_number, line in enumerate(source_path.read_text(encoding="utf-8").splitlines(), 1):
                is_versioning_metadata = source_path.name == "versioning_metadata.json"
                if "01_RESEARCH" in line and not (is_versioning_metadata or allowed_provenance.fullmatch(line)):
                    offenders.append(f"{source_path.relative_to(REPOSITORY_ROOT)}:{line_number}: {line}")
        self.assertEqual([], offenders, "Legacy runtime references remain:\n" + "\n".join(offenders))

    def test_state_analysis_runtime_has_no_legacy_research_references(self):
        """State-analysis code may retain only an original-path provenance comment."""
        offenders = []
        for source_path in sorted(STATE_ANALYSIS.glob("*.py")):
            for line_number, line in enumerate(source_path.read_text(encoding="utf-8").splitlines(), 1):
                allowed = any(pattern.fullmatch(line) for pattern in STATE_ANALYSIS_LEGACY_RESEARCH_ALLOWANCES)
                if "01_RESEARCH" in line and not allowed:
                    offenders.append(f"{source_path.relative_to(REPOSITORY_ROOT)}:{line_number}: {line}")
        self.assertEqual([], offenders, "Legacy state-analysis references remain:\n" + "\n".join(offenders))

    def test_scenario_artifact_families_use_the_work_contract(self):
        """Each live scenario family is fully detached from 05_AUTOMATION."""
        for family, destinations in SCENARIO_WORK_ARTIFACT_FAMILIES.items():
            with self.subTest(family=family):
                for destination, sources in destinations:
                    for source in sources:
                        text = source.read_text(encoding="utf-8")
                        parent, filename = destination.rsplit("/", 1)
                        self.assertTrue(
                            parent in text or all(f'"{part}"' in text for part in parent.split("/")),
                            source.relative_to(REPOSITORY_ROOT),
                        )
                        self.assertIn(filename, text, source.relative_to(REPOSITORY_ROOT))
                        self.assertNotIn("05_AUTOMATION/" + destination.rsplit("/", 1)[-1], text,
                                         source.relative_to(REPOSITORY_ROOT))


if __name__ == "__main__":
    unittest.main()
