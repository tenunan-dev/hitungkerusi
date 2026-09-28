"""States + the federation as first-class graph nodes (T1.5 packet, Sep 2026).

Owner bug report: searching "melaka" in the Graph Explorer's graph view returned
``seat:P138 Kota Melaka`` instead of the state. Cause: ``findNode`` scored node
id/name only, and the knowledge graph had no state node at all (0 state-type
nodes among 4,370). Owner requirement, literally: "Add states and federal as
part of the database."

This suite pins the fixed shape:

  * the builder adds exactly 14 jurisdiction nodes — 13 ``state:<Name>`` +
    1 ``federal:MY`` — and removes none (``PRE_PACKET_TYPE_FLOOR``);
  * the state vocabulary is the canonical one on disk (sibling 1_DATA folders
    ``research/states/DUN <State>``), the 3 Federal Territories are not states;
  * OWNER ADDITION (Sep 2026): "link federal seats to its appropriate states
    AND federal territories too." The 3 Federal Territories (Kuala Lumpur,
    Putrajaya, Labuan) therefore become their own jurisdiction family —
    3 ``federal-territory:<Name>`` nodes with ids, codes (KL-FT/PJY-FT/LBN-FT)
    and labels taken from master-list-222 and derived/ge15-results-by-state.csv,
    never from guesswork. They are NOT rows in the state set, because this
    project's canonical data does not treat them as states: STATE_META in
    02_FORECAST/engine/state_report_builder.py holds 13 keys and sibling
    1_DATA/research/states/ holds 13 ``DUN <State>`` folders.
  * every one of the 824 seat nodes ends up wired to exactly one jurisdiction —
    ``seat_state`` (811 seats inside a state) or ``seat_territory`` (the 13
    Federal Territory seats, each to its own territory) — and carries a
    non-null ``state``; all 222 parliamentary seats are covered, so
    ``seat_state`` ∪ ``seat_territory`` is the jurisdiction layer while
    ``seat_federal`` stays the federation pivot for the 13 FT seats;
  * the alias-aware search in the tracked page resolves "melaka" (and
    "malacca", "melaka state", "NS", "penang", "parlimen") to the jurisdiction
    node, while seat-code and seat-name queries keep resolving to the seat.

The search test drives the REAL functions extracted from the page over the REAL
shipped bundle, in node — the same harness style as
test_graph_explorer_layout_nonblocking.py — so it cannot drift from the app.
"""

import csv
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = REPOSITORY_ROOT.parent / "1_DATA"
PAGE = REPOSITORY_ROOT / "tools" / "graph-explorer" / "index.html"
STAGED_DIR = REPOSITORY_ROOT / "work" / "graph-explorer"
APP_DIR = REPOSITORY_ROOT / "GE16-Graph-Explorer"
GRAPH_ARTIFACT = REPOSITORY_ROOT / "work" / "graph" / "ge16-knowledge-graph.json"

#: The canonical 13 states, spelled exactly like the DUN datasets on disk.
STATE_NAMES = (
    "Perlis", "Kedah", "Kelantan", "Terengganu", "Pulau Pinang", "Perak",
    "Pahang", "Selangor", "Negeri Sembilan", "Melaka", "Johor", "Sabah",
    "Sarawak",
)

#: DUN seats per state = rows in 1_DATA/research/states/DUN <State>/
#: dun-election-results-latest.csv (re-checked against the files themselves in
#: test_state_dun_seat_counts_match_the_canonical_files).
EXPECTED_DUN_SEATS = {
    "Perlis": 15, "Kedah": 36, "Kelantan": 45, "Terengganu": 32,
    "Pulau Pinang": 40, "Perak": 59, "Pahang": 42, "Selangor": 56,
    "Negeri Sembilan": 36, "Melaka": 28, "Johor": 56, "Sabah": 73,
    "Sarawak": 82,
}

#: Node-type counts immediately before the state/federal layer landed. The
#: change only ADDS nodes, so no type may shrink below its pre-packet count
#: (a floor, not a target: data growth above it is expected and allowed).
PRE_PACKET_TYPE_FLOOR = {
    "news": 2367, "person": 1107, "seat": 824, "party": 30, "cluster": 28,
    "scenario": 14,
}

#: Edge types this packet introduces. None of them may dangle.
NEW_EDGE_TYPES = ("seat_state", "seat_territory", "seat_federal", "federal_seat",
                  "state_federal", "territory_federal", "news->state")

#: Federal Territory seats: parliamentary seats that belong to no state.
FT_MARKER = "(FT)"

#: The 3 Federal Territories — a jurisdiction family of their own, NOT three
#: extra rows in the state set (the project's canonical data has no state
#: assembly for a territory). Parliament-seat counts are the master-list-222
#: rows whose state cell carries the "(FT)" marker.
TERRITORY_NAMES = ("Kuala Lumpur", "Putrajaya", "Labuan")
EXPECTED_TERRITORY_SEATS = {"Kuala Lumpur": 11, "Putrajaya": 1, "Labuan": 1}
TERRITORY_CODES = {"Kuala Lumpur": "KL-FT", "Putrajaya": "PJY-FT",
                   "Labuan": "LBN-FT"}

MASTER_LIST = (DATA_ROOT / "research" / "derived" /
               "master-list-222-parliamentary-seats.csv")
GE15_BY_STATE = DATA_ROOT / "research" / "derived" / "ge15-results-by-state.csv"
STATE_META_SOURCE = (REPOSITORY_ROOT / "02_FORECAST" / "engine" /
                     "state_report_builder.py")

# The synchronous full-layout pattern that froze the UI (T2 packet). Dropping it
# again is not this packet's business, but this packet must not undo it.
SYNC_FULL_LAYOUT = re.compile(r"layoutOnce\(\s*\)\s*;\s*draw\(\s*\)\s*;")
BARE_LAYOUT_ONCE_CALL = re.compile(r"layoutOnce\(\s*\)")


def _load_graph_artifact():
    with open(GRAPH_ARTIFACT, encoding="utf-8") as handle:
        return json.load(handle)


def _load_bundle(directory):
    with open(directory / "graph.json", encoding="utf-8") as handle:
        return json.load(handle)


def _page_source():
    return PAGE.read_text(encoding="utf-8")


def _inline_script(source):
    blocks = re.findall(r"<script>(.*?)</script>", source, re.S)
    if len(blocks) != 1:
        raise AssertionError("expected exactly one inline script block, got %d"
                             % len(blocks))
    return blocks[0]


def _extract_function(script, name):
    """The text of a top-level ``function <name>(...)`` in the page script."""
    marker = "function %s(" % name
    if marker not in script:
        raise AssertionError(
            "the tracked page no longer defines %s() — the alias-aware search "
            "was refactored or removed" % name)
    start = script.index(marker)
    tail = script[start:]
    nxt = re.search(r"\nfunction \w+\(", tail)
    return tail if nxt is None else tail[:nxt.start()]


def _extract_var(script, name):
    """The declaration line of a top-level ``var <name>=...;``."""
    match = re.search(r"^var %s=.*;$" % re.escape(name), script, re.M)
    if match is None:
        raise AssertionError("the tracked page no longer declares var %s" % name)
    return match.group(0)


SEARCH_FUNCTION_NAMES = ("queryVariants", "nodeScore", "nodeExactMatch",
                         "bestNodeMatches")

NODE_HARNESS = """
var NODES = __NODES__;
var QUERIES = __QUERIES__;
__SEARCH_CODE__
var out = QUERIES.map(function(q){
  var best = bestNodeMatches(q, NODES);
  return [q, best ? best.id : null, best ? best.name : null];
});
process.stdout.write(JSON.stringify(out));
"""


def run_search_harness(queries, nodes):
    """Resolve every query with the page's own search code, in node."""
    script = _inline_script(_page_source())
    if not shutil.which("node"):
        raise unittest.SkipTest("node is not installed")
    code = "\n".join(
        [_extract_var(script, "QUERY_NOISE")]
        + [_extract_function(script, name) for name in SEARCH_FUNCTION_NAMES]
    )
    harness = (NODE_HARNESS
               .replace("__NODES__", json.dumps(nodes, ensure_ascii=False))
               .replace("__QUERIES__", json.dumps(list(queries), ensure_ascii=False))
               .replace("__SEARCH_CODE__", code))
    handle = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8")
    try:
        handle.write(harness)
        handle.close()
        result = subprocess.run(["node", handle.name], capture_output=True,
                                text=True, timeout=300)
    finally:
        os.unlink(handle.name)
    if result.returncode != 0:
        raise AssertionError("search harness failed: %s" % result.stderr[:2000])
    return {row[0]: {"id": row[1], "name": row[2]}
            for row in json.loads(result.stdout)}


@unittest.skipUnless(GRAPH_ARTIFACT.exists(),
                     "knowledge graph artifact is missing (run the builder)")
class StateAndFederalNodeTests(unittest.TestCase):
    """The new first-class jurisdiction nodes: 13 states + 1 federal + 3 FTs."""

    @classmethod
    def setUpClass(cls):
        cls.graph = _load_graph_artifact()
        cls.nodes = cls.graph["nodes"]
        cls.by_type = {}
        for node in cls.nodes.values():
            cls.by_type.setdefault(node["type"], []).append(node)

    def test_exactly_thirteen_state_nodes_and_one_federal_node_exist(self):
        states = self.by_type.get("state", [])
        federal = self.by_type.get("federal", [])
        self.assertEqual(13, len(states), "expected 13 state nodes")
        self.assertEqual(1, len(federal), "expected exactly one federal node")
        for node in states:
            self.assertTrue(node["id"].startswith("state:"), node["id"])
        self.assertEqual("federal:MY", federal[0]["id"])
        self.assertEqual(["federal:MY"], sorted(n["id"] for n in federal))

    def test_state_vocabulary_matches_the_canonical_data_folders(self):
        """The state list is not invented: it is the DUN dataset vocabulary."""
        folders = sorted(p.name for p in (DATA_ROOT / "research" / "states").glob("DUN *"))
        self.assertEqual(13, len(folders), folders)
        on_disk = {name[len("DUN "):] for name in folders}
        self.assertEqual(set(STATE_NAMES), on_disk)
        state_names = sorted(n["name"] for n in self.by_type["state"])
        self.assertEqual(sorted(STATE_NAMES), state_names)

    def test_state_dun_seat_counts_match_the_canonical_files(self):
        for node in self.by_type["state"]:
            with self.subTest(state=node["name"]):
                self.assertEqual(EXPECTED_DUN_SEATS[node["name"]], node["dun_seats"])
                self.assertGreater(node["parliament_seats"], 0)
                self.assertTrue(node["ge15_label"])

    def test_alias_tables_are_exact_and_unambiguous(self):
        expected = {
            "Melaka": ["Malacca"],
            "Pulau Pinang": ["Penang", "Pinang"],
            "Negeri Sembilan": ["NS", "N9"],
        }
        seen = {}
        for node in self.by_type["state"]:
            with self.subTest(state=node["name"]):
                self.assertIsInstance(node["aliases"], list)
                self.assertEqual(expected.get(node["name"], []), node["aliases"])
                for alias in node["aliases"]:
                    lowered = alias.lower()
                    self.assertNotIn(lowered, seen,
                                     "%r already aliases %s" % (alias, seen.get(lowered)))
                    seen[lowered] = node["name"]
                    self.assertNotIn(alias, STATE_NAMES,
                                     "%r is a canonical state, not an alias" % alias)

    def test_federal_node_describes_the_federation(self):
        federal = self.by_type["federal"][0]
        self.assertEqual("Federal", federal["name"])
        for alias in ("Parlimen", "Parliament", "Federal Government"):
            self.assertIn(alias, federal["aliases"])
        self.assertEqual(222, federal["parliament_seats"],
                         "the 222 parliamentary seats of master-list-222")

    # --- OWNER ADDITION (Sep 2026): the federal territories -----------------
    # "link federal seats to its appropriate states AND federal territories
    # too": the 3 FTs are their own entities, additive to the 14 jurisdiction
    # nodes above. The total this packet adds is therefore 14 + 3 = 17 nodes
    # (plus 811 seat_state + 13 seat_territory + 13 seat_federal + 222
    # federal_seat + 13 state_federal + 3 territory_federal edges).

    def test_exactly_three_federal_territory_nodes_exist(self):
        territories = self.by_type.get("federal_territory", [])
        self.assertEqual(3, len(territories), "Kuala Lumpur + Putrajaya + Labuan")
        by_name = {n["name"]: n for n in territories}
        self.assertEqual(sorted(TERRITORY_NAMES), sorted(by_name))
        for name, node in sorted(by_name.items()):
            with self.subTest(territory=name):
                self.assertEqual("federal-territory:%s" % name, node["id"])
                self.assertEqual(TERRITORY_CODES[name], node["code"])
                self.assertEqual("%s (FT)" % name, node["ft_label"])
                self.assertNotIn(node["id"], ["state:" + n for n in STATE_NAMES])

    def test_territory_parliament_seats_match_master_list_222(self):
        """11 + 1 + 1 = the 13 "(FT)" seats of the canonical master list."""
        territories = {n["name"]: n for n in self.by_type["federal_territory"]}
        total = 0
        for name, expected in sorted(EXPECTED_TERRITORY_SEATS.items()):
            with self.subTest(territory=name):
                self.assertEqual(expected, territories[name]["parliament_seats"])
                total += territories[name]["parliament_seats"]
        self.assertEqual(13, total)
        with open(MASTER_LIST, encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(222, len(rows))
        ft_rows = [r for r in rows if FT_MARKER in r["state"]]
        self.assertEqual(13, len(ft_rows))
        counted = {}
        for row in ft_rows:
            counted[row["state"].strip()] = counted.get(row["state"].strip(), 0) + 1
        self.assertEqual({t["ft_label"]: t["parliament_seats"]
                          for t in self.by_type["federal_territory"]}, counted)

    def test_territory_vocabulary_is_not_invented(self):
        """Every territory field traces to a canonical DATA file on disk."""
        territories = {n["name"]: n for n in self.by_type["federal_territory"]}
        # derived/ge15-results-by-state.csv: 16 rows = the 13 states + the 3 FTs
        with open(GE15_BY_STATE, encoding="utf-8") as handle:
            labels = [r["state_std"].strip() for r in csv.DictReader(handle)]
        self.assertEqual(16, len(labels), labels)
        # That file is the project's 16-way jurisdiction vocabulary (13 states +
        # 3 territories), spelled its own way: it says "Penang"/"Malacca" where
        # the DUN datasets say "Pulau Pinang"/"Melaka". The ge15_label carried
        # by each jurisdiction node is exactly that file's own spelling.
        graph_labels = {n["ge15_label"] for n in self.by_type["state"]}
        graph_labels |= {n["ge15_label"] for n in self.by_type["federal_territory"]}
        self.assertEqual(sorted(set(labels)), sorted(graph_labels))
        for name, node in sorted(territories.items()):
            with self.subTest(territory=name):
                self.assertIn(node["ge15_label"], labels)
                self.assertIn(name, node["ge15_label"])
        # ... and they are NOT states: no DUN folder, no STATE_META key
        folders = sorted(p.name for p in
                         (DATA_ROOT / "research" / "states").glob("DUN *"))
        for name in TERRITORY_NAMES:
            self.assertNotIn("DUN " + name, folders)
        keys = []
        inside = False
        for line in STATE_META_SOURCE.read_text(encoding="utf-8").splitlines():
            if line.startswith("STATE_META = {"):
                inside = True
                continue
            if inside:
                if line == "}":
                    break
                matched = re.match(r'    "([^"]+)": \{', line)
                if matched:
                    keys.append(matched.group(1))
        self.assertEqual(13, len(keys), keys)
        self.assertEqual(sorted(STATE_NAMES), sorted(keys))
        for name in TERRITORY_NAMES:
            self.assertNotIn(name, keys,
                             "%s is a territory, not a STATE_META state" % name)

    def test_territory_aliases_are_unambiguous(self):
        """Every territory alias resolves to exactly one node, and vice versa."""
        expected = {
            "Kuala Lumpur": ["Kuala Lumpur (FT)", "KL-FT",
                             "Wilayah Persekutuan Kuala Lumpur"],
            "Putrajaya": ["Putrajaya (FT)", "PJY-FT",
                          "Wilayah Persekutuan Putrajaya"],
            "Labuan": ["Labuan (FT)", "LBN-FT", "Wilayah Persekutuan Labuan"],
        }
        seen = {}
        for node in self.by_type["federal_territory"]:
            with self.subTest(territory=node["name"]):
                self.assertEqual(expected[node["name"]], node["aliases"])
                for alias in node["aliases"]:
                    lowered = alias.lower()
                    self.assertNotIn(lowered, seen,
                                     "%r already aliases %s" % (alias, seen.get(lowered)))
                    seen[lowered] = node["name"]
                    self.assertNotIn(alias, STATE_NAMES)
        # No alias may collide with a state node's own name or alias either.
        state_words = set()
        for node in self.by_type["state"]:
            state_words.add(node["name"].lower())
            state_words.update(a.lower() for a in node["aliases"])
        self.assertEqual(set(), state_words & set(seen))

    def test_no_pre_existing_node_type_shrank(self):
        """'Never delete existing nodes' — enforced as a floor per type."""
        for node_type, floor in PRE_PACKET_TYPE_FLOOR.items():
            with self.subTest(node_type=node_type):
                self.assertGreaterEqual(len(self.by_type.get(node_type, [])), floor)


@unittest.skipUnless(GRAPH_ARTIFACT.exists(),
                     "knowledge graph artifact is missing (run the builder)")
class SeatJurisdictionEdgeTests(unittest.TestCase):
    """Every seat belongs to exactly one jurisdiction: a state, or an FT."""

    @classmethod
    def setUpClass(cls):
        cls.graph = _load_graph_artifact()
        cls.nodes = cls.graph["nodes"]
        cls.edges = cls.graph["edges"]
        cls.seats = [n for n in cls.nodes.values() if n["type"] == "seat"]
        cls.by_type = {etype: [e for e in cls.edges if e["type"] == etype]
                       for etype in NEW_EDGE_TYPES}

    def test_seat_state_edges_point_at_real_state_nodes(self):
        state_ids = {n["id"] for n in self.nodes.values() if n["type"] == "state"}
        edges = self.by_type["seat_state"]
        self.assertEqual(824 - 13, len(edges),
                         "824 seats minus the 13 Federal Territory seats")
        for edge in edges:
            with self.subTest(src=edge["src"]):
                self.assertIn(edge["dst"], state_ids)
                self.assertIn(edge["src"], self.nodes)
                self.assertEqual("seat", self.nodes[edge["src"]]["type"])
                self.assertEqual(1.0, edge["weight"])

    def test_federal_territory_seats_hang_off_the_federal_node(self):
        edges = self.by_type["seat_federal"]
        self.assertEqual(13, len(edges), "Kuala Lumpur 11 + Labuan 1 + Putrajaya 1")
        for edge in edges:
            with self.subTest(src=edge["src"]):
                self.assertEqual("federal:MY", edge["dst"])
                self.assertIn(FT_MARKER, self.nodes[edge["src"]]["state"])

    # --- OWNER ADDITION (Sep 2026): federal seats -> states AND territories --

    def test_seat_territory_edges_point_at_real_territory_nodes(self):
        """Each FT seat links to its OWN territory node, not only to federal:MY."""
        territory_ids = {n["id"] for n in self.nodes.values()
                         if n["type"] == "federal_territory"}
        self.assertEqual(3, len(territory_ids))
        edges = self.by_type["seat_territory"]
        self.assertEqual(13, len(edges),
                         "Kuala Lumpur 11 + Putrajaya 1 + Labuan 1")
        counts = {}
        for edge in edges:
            with self.subTest(src=edge["src"]):
                self.assertIn(edge["dst"], territory_ids)
                self.assertEqual("seat", self.nodes[edge["src"]]["type"])
                self.assertEqual("federal", self.nodes[edge["src"]]["kind"])
                self.assertIn(FT_MARKER, self.nodes[edge["src"]]["state"])
                self.assertEqual(1.0, edge["weight"])
                counts[edge["dst"]] = counts.get(edge["dst"], 0) + 1
        self.assertEqual({"federal-territory:Kuala Lumpur": 11,
                          "federal-territory:Putrajaya": 1,
                          "federal-territory:Labuan": 1}, counts)

    def test_seat_state_attribute_agrees_with_its_territory(self):
        """One canonical spelling: seat.state is the territory's own label."""
        edges = {e["src"]: e["dst"] for e in self.by_type["seat_territory"]}
        for seat_id, dst in edges.items():
            with self.subTest(seat=seat_id):
                self.assertEqual(self.nodes[dst]["ft_label"],
                                 self.nodes[seat_id]["state"])

    def test_territory_federal_edges(self):
        edges = self.by_type["territory_federal"]
        self.assertEqual(3, len(edges), "each territory belongs to the federation")
        for edge in edges:
            with self.subTest(src=edge["src"]):
                self.assertEqual("federal:MY", edge["dst"])
                self.assertEqual("federal_territory",
                                 self.nodes[edge["src"]]["type"])
                self.assertEqual(1.0, edge["weight"])

    def test_all_222_federal_seats_resolve_to_one_jurisdiction(self):
        """The owner addendum, enforced: state OR territory — never both, never none."""
        federal = [n["id"] for n in self.seats if n.get("kind") == "federal"]
        self.assertEqual(222, len(federal), "the P-code parliamentary seats")
        linked = {}
        for etype in ("seat_state", "seat_territory"):
            for edge in self.by_type[etype]:
                linked.setdefault(edge["src"], []).append(etype)
        for seat_id in federal:
            with self.subTest(seat=seat_id):
                self.assertEqual(1, len(linked.get(seat_id, [])),
                                 "a parliamentary seat needs exactly one jurisdiction")
        by_jurisdiction = {}
        for seat_id in federal:
            by_jurisdiction.setdefault(linked[seat_id][0], []).append(seat_id)
        self.assertEqual(209, len(by_jurisdiction.get("seat_state", [])),
                         "222 - the 13 Federal Territory seats")
        self.assertEqual(13, len(by_jurisdiction.get("seat_territory", [])))

    def test_every_seat_has_exactly_one_jurisdiction_edge(self):
        """824 seats: seat_state (a state) or seat_territory (an FT) — once each."""
        linked = {}
        for etype in ("seat_state", "seat_territory"):
            for edge in self.by_type[etype]:
                linked.setdefault(edge["src"], []).append(etype)
        self.assertEqual(len(self.seats), len(linked),
                         "every seat node needs a jurisdiction edge")
        for seat in self.seats:
            with self.subTest(seat=seat["id"]):
                self.assertEqual(1, len(linked.get(seat["id"], [])),
                                 "seats must not be double-wired")
        # seat_federal is the federation pivot, deliberately NOT a jurisdiction
        # edge (an FT seat has both): see
        # test_federal_territory_seats_hang_off_the_federal_node.
        self.assertEqual(13, len(self.by_type["seat_federal"]))

    def test_no_seat_is_left_without_a_state(self):
        """The 602 DUN seats that carried state=null now resolve to a state."""
        null_states = [n["id"] for n in self.seats if not n.get("state")]
        self.assertEqual([], null_states)
        state_names = set(STATE_NAMES)
        for seat in self.seats:
            with self.subTest(seat=seat["id"]):
                if FT_MARKER in seat["state"]:
                    self.assertEqual("federal", seat["kind"])
                else:
                    self.assertIn(seat["state"], state_names)

    def test_seat_state_attribute_agrees_with_its_edge(self):
        """One canonical spelling: seat.state is the state node it links to."""
        edges = {e["src"]: e["dst"] for e in self.by_type["seat_state"]}
        for seat_id, dst in edges.items():
            with self.subTest(seat=seat_id):
                self.assertEqual(dst, "state:%s" % self.nodes[seat_id]["state"])

    def test_federal_pivot_edges(self):
        federal_seat = self.by_type["federal_seat"]
        self.assertEqual(222, len(federal_seat))
        for edge in federal_seat:
            with self.subTest(dst=edge["dst"]):
                self.assertEqual("federal:MY", edge["src"])
                self.assertEqual("federal", self.nodes[edge["dst"]]["kind"])
        state_federal = self.by_type["state_federal"]
        self.assertEqual(13, len(state_federal))
        for edge in state_federal:
            with self.subTest(src=edge["src"]):
                self.assertEqual("federal:MY", edge["dst"])
                self.assertEqual("state", self.nodes[edge["src"]]["type"])

    def test_new_edge_types_never_dangle(self):
        """(The pre-existing seat->party dangling ids are not this packet's.)"""
        for etype in NEW_EDGE_TYPES:
            for edge in self.by_type[etype]:
                with self.subTest(etype=etype, src=edge["src"]):
                    self.assertIn(edge["src"], self.nodes)
                    self.assertIn(edge["dst"], self.nodes)

    def test_news_state_edges_come_from_real_state_tags(self):
        edges = self.by_type["news->state"]
        self.assertGreater(len(edges), 0, "the news tag join found nothing")
        for edge in edges:
            with self.subTest(src=edge["src"]):
                self.assertEqual("news", self.nodes[edge["src"]]["type"])
                self.assertEqual("state", self.nodes[edge["dst"]]["type"])


@unittest.skipUnless(PAGE.exists(), "versioned explorer page is missing")
class AliasSearchTests(unittest.TestCase):
    """The page's own search code resolves states/aliases — and still seats."""

    #: Queries that must keep resolving to a seat after the alias layer landed
    #: (two-word seat names must not be hijacked by a state alias).
    SEAT_QUERIES = {"p138": "seat:P138", "bangi": "seat:P102",
                    "p102": "seat:P102", "kota melaka": "seat:P138",
                    "johor jaya": "seat:N.42 JOHOR JAYA"}

    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise unittest.SkipTest("node is not installed")
        bundle = STAGED_DIR / "graph.json"
        if not bundle.exists():
            raise unittest.SkipTest("staged graph bundle is missing")
        with open(bundle, encoding="utf-8") as handle:
            cls.nodes = json.load(handle)["nodes"]
        by_type = {}
        for node in cls.nodes:
            by_type.setdefault(node["type"], []).append(node)
        cls.states = {n["name"]: n for n in by_type.get("state", [])}
        cls.federal = by_type.get("federal", [])
        cls.territories = {n["name"]: n for n in by_type.get("federal_territory", [])}
        queries = {}
        for name, node in cls.states.items():
            queries[name] = node["id"]                  # as typed by the owner
            queries[name.lower()] = node["id"]
            for alias in node["aliases"]:
                queries[alias] = node["id"]
                queries[alias.lower()] = node["id"]
        for name, node in cls.territories.items():
            queries[name] = node["id"]                  # "Labuan", "Putrajaya"…
            queries[name.lower()] = node["id"]
            for alias in node["aliases"]:
                queries[alias] = node["id"]
                queries[alias.lower()] = node["id"]
        queries.update({
            "Melaka state": "state:Melaka",
            "penang state": "state:Pulau Pinang",
            "federal": "federal:MY",
            "Parlimen": "federal:MY",
            "parliament": "federal:MY",
            "federal government": "federal:MY",
            "persekutuan": "federal:MY",
        })
        queries.update(cls.SEAT_QUERIES)
        cls.queries = queries
        cls.hits = run_search_harness(sorted(queries), cls.nodes)

    def _assert_hits(self, expected):
        for query, node_id in sorted(expected.items()):
            with self.subTest(query=query):
                self.assertEqual(node_id, self.hits[query]["id"])

    def test_the_bundle_actually_carries_the_new_nodes(self):
        self.assertEqual(len(STATE_NAMES), len(self.states))
        self.assertEqual(["federal:MY"], [n["id"] for n in self.federal])
        self.assertEqual(sorted(TERRITORY_NAMES), sorted(self.territories),
                         "the 3 Federal Territories are first-class nodes too")

    def test_federal_territory_names_and_aliases_resolve_to_their_nodes(self):
        """Owner addition: a territory is more than a "(FT)" string on a seat."""
        self._assert_hits({
            "kuala lumpur": "federal-territory:Kuala Lumpur",
            "Kuala Lumpur": "federal-territory:Kuala Lumpur",
            "kuala lumpur (ft)": "federal-territory:Kuala Lumpur",
            "KL-FT": "federal-territory:Kuala Lumpur",
            "wilayah persekutuan kuala lumpur":
                "federal-territory:Kuala Lumpur",
            "putrajaya": "federal-territory:Putrajaya",
            "labuan": "federal-territory:Labuan",
        })

    def test_owner_bug_is_fixed_melaka_is_the_state(self):
        self._assert_hits({"melaka": "state:Melaka", "malacca": "state:Melaka",
                           "Melaka": "state:Melaka",
                           "Melaka state": "state:Melaka"})

    def test_every_state_name_and_alias_resolves_to_its_state_node(self):
        self._assert_hits(self.queries)

    def test_federal_node_is_reachable_by_its_aliases(self):
        self._assert_hits({"federal": "federal:MY", "Parlimen": "federal:MY",
                           "parliament": "federal:MY",
                           "federal government": "federal:MY",
                           "persekutuan": "federal:MY"})

    def test_seat_queries_still_resolve_to_seats(self):
        self._assert_hits(self.SEAT_QUERIES)


@unittest.skipUnless(PAGE.exists(), "versioned explorer page is missing")
class ShippedCopyTests(unittest.TestCase):
    """Staged bundle, served bundle and page copies stay in lockstep."""

    def test_all_three_page_copies_are_byte_identical(self):
        copies = [PAGE, STAGED_DIR / "index.html", APP_DIR / "index.html"]
        present = [p for p in copies if p.exists()]
        self.assertEqual(len(copies), len(present),
                         "expected all three page copies on disk")
        payloads = {p: p.read_bytes() for p in present}
        first = payloads[present[0]]
        for path, blob in payloads.items():
            with self.subTest(page=str(path)):
                self.assertEqual(first, blob)

    def test_staged_and_app_bundles_carry_the_same_nodes(self):
        for directory in (STAGED_DIR, APP_DIR):
            if not (directory / "graph.json").exists():
                self.skipTest("bundle missing under %s" % directory)
        staged = _load_bundle(STAGED_DIR)
        served = _load_bundle(APP_DIR)
        self.assertEqual(staged["node_count"], len(staged["nodes"]))
        self.assertEqual(staged["nodes"], served["nodes"])
        for bundle in (staged, served):
            types = {}
            for node in bundle["nodes"]:
                types.setdefault(node["type"], []).append(node["id"])
            self.assertEqual(13, len(types.get("state", [])))
            self.assertEqual(3, len(types.get("federal_territory", [])),
                             "Kuala Lumpur, Putrajaya, Labuan")
            self.assertEqual(["federal:MY"], types.get("federal", []))

    def test_no_synchronous_full_layout_came_back(self):
        for path in [p for p in (PAGE, STAGED_DIR / "index.html",
                                 APP_DIR / "index.html") if p.exists()]:
            with self.subTest(page=str(path)):
                source = path.read_text(encoding="utf-8")
                self.assertEqual([], SYNC_FULL_LAYOUT.findall(source))
                self.assertEqual([], BARE_LAYOUT_ONCE_CALL.findall(source))


if __name__ == "__main__":
    unittest.main()
