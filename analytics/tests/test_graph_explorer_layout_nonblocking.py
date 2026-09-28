"""Regression guard for the Graph Explorer freeze fix (T2 packet).

The explorer SPA once froze (tab buttons unclickable) because two code paths ran
a SYNCHRONOUS full force layout: `layoutOnce(); draw();` — the full O(n^2)
repulsion loop over ~4,370 nodes. Both sites now route through the debounced,
non-blocking `scheduleLayout()`, which coalesces triggers and hands off to the
batched `layoutProgressive()` stepper (30ms yields, incremental paint).

These tests pin the fixed shape of BOTH page copies, so a future edit cannot
quietly reintroduce a synchronous full-layout route:

  source of truth  tools/graph-explorer/index.html   (tracked, versioned)
  in-place app     GE16-Graph-Explorer/index.html    (gitignored, disk-only)

The two pages are also asserted byte-identical elsewhere (see
test_graph_explorer_databases.test_app_copy_and_versioned_page_stay_identical);
here we assert the layout wiring itself.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PAGE = REPOSITORY_ROOT / "tools" / "graph-explorer" / "index.html"
APP_PAGE = REPOSITORY_ROOT / "GE16-Graph-Explorer" / "index.html"

# The synchronous pattern that froze the UI. Must never come back.
SYNC_FULL_LAYOUT = re.compile(r"layoutOnce\(\s*\)\s*;\s*draw\(\s*\)\s*;")
# Any zero-argument call to layoutOnce() is a synchronous full layout by default
# (the only legitimate call site passes an explicit batch count).
BARE_LAYOUT_ONCE_CALL = re.compile(r"layoutOnce\(\s*\)")

# The two trigger sites that must feed scheduleLayout(): the tab-click handler
# and the filter/ego redraw.
TRIGGER_SITES = (
    "if(t.dataset.view==='graph' && graph.ready) { scheduleLayout(); }",
    "if(!graph.suppressLayout){ scheduleLayout(); }",
)

LAYOUT_FUNCTIONS = ("draw", "layoutOnce", "layoutProgressive", "scheduleLayout")

# ---------------------------------------------------------------------------
# Mutual exclusion (ad-work 1): ONE layout stepper at a time.
#
# `_layoutRunning` is the token held by the stepper that is mid-flight and
# `_layoutPending` is the newest request that arrived while it ran. A request
# that lands mid-flight must be QUEUED, never started, and replayed as the next
# stepper once the token is released — otherwise two steppers step over the same
# shared `graph.nodes` positions at once (double CPU, frame-thrash draws).
# ---------------------------------------------------------------------------
TOKEN = "_layoutRunning"
PENDING = "_layoutPending"
TOKEN_DECLARATION = "var %s=false, %s=null;" % (TOKEN, PENDING)
STEPPER_BLOCK_START = "var _layoutTimer=null;"
STEPPER_BLOCK_END = "\nfunction draw("


def stepper_block(source):
    """The debounce + stepper code block (scheduleLayout + layoutProgressive)."""
    start = source.index(STEPPER_BLOCK_START)
    return source[start:source.index(STEPPER_BLOCK_END, start)]


# Node harness: drives the REAL extracted stepper code with stubbed paint/step
# hooks. `layoutOnce` is tagged per stepper (1 = first run of 20 iterations of 1,
# 2 = the deferred run of 40 iterations of 2), so the recorded call order proves
# whether two steppers ever interleaved.
NODE_HARNESS = """
var LOAD={pct:100};                       // 100 → loadUI is skipped (not under test)
var seq=[], at120=null;
function loadUI(){}
function draw(){}
function layoutOnce(nIt){ seq.push(nIt); }
__STEPPER__
var onDoneA=false, onDoneB=0;
layoutProgressive(20, function(){ onDoneA=true; });          // stepper A: 20 x 1
var runningAfterStart=_layoutRunning;
scheduleLayout(40, function(){ onDoneB++; });                // request B mid-flight
scheduleLayout(40, function(){ onDoneB++; });                // burst → newest request only
var runningAfterTrigger=_layoutRunning;
setTimeout(function(){
  at120={running:_layoutRunning, pending:_layoutPending!==null, seq:seq.slice()};
}, 120);
setTimeout(function(){
  process.stdout.write(JSON.stringify({
    runningAfterStart:runningAfterStart, runningAfterTrigger:runningAfterTrigger,
    at120:at120, seq:seq, runningAtEnd:_layoutRunning, pendingAtEnd:_layoutPending!==null,
    onDoneA:onDoneA, onDoneB:onDoneB
  }));
}, 1500);
"""


def page_paths():
    """Every page copy that exists on disk (app copy is disk-only/gitignored)."""
    return [p for p in (PAGE, APP_PAGE) if p.exists()]


def read_page(path):
    return path.read_text(encoding="utf-8")


def inline_scripts(source):
    return re.findall(r"<script>(.*?)</script>", source, re.S)


def function_body(source, name):
    """Return the text of `function <name>(...)` up to the next top-level def."""
    start = source.index("function %s(" % name)
    tail = source[start:]
    nxt = re.search(r"\nfunction \w+\(", tail)
    return tail if not nxt else tail[: nxt.start()]


@unittest.skipUnless(PAGE.exists(), "versioned explorer page is missing")
class LayoutResponsivenessTests(unittest.TestCase):
    """The fixed, non-blocking layout wiring in every page copy."""

    def test_every_page_copy_defines_the_debounced_entry_point(self):
        for path in page_paths():
            with self.subTest(page=path.name):
                source = read_page(path)
                self.assertEqual(
                    1, source.count("function scheduleLayout("),
                    "expected exactly one scheduleLayout definition in %s" % path,
                )

    def test_no_page_copy_contains_a_synchronous_full_layout(self):
        for path in page_paths():
            with self.subTest(page=path):
                source = read_page(path)
                matches = SYNC_FULL_LAYOUT.findall(source)
                self.assertEqual([], matches, "synchronous layoutOnce(); draw(); is back")
                self.assertEqual(
                    [], BARE_LAYOUT_ONCE_CALL.findall(source),
                    "a zero-argument layoutOnce() call site (synchronous full "
                    "layout) is present",
                )

    def test_both_trigger_sites_route_through_schedule_layout(self):
        for path in page_paths():
            with self.subTest(page=path):
                source = read_page(path)
                for site in TRIGGER_SITES:
                    self.assertIn(site, source)

    def test_schedule_layout_debounces_into_the_progressive_stepper(self):
        for path in page_paths():
            with self.subTest(page=path):
                body = function_body(read_page(path), "scheduleLayout")
                # debounce, not a direct call
                self.assertIn("setTimeout(", body)
                self.assertIn("clearTimeout(", body)
                self.assertIn("}, 50);", body)
                self.assertIn("layoutProgressive(totalIter||80, onDone);", body)

    def test_progressive_stepper_keeps_batching_and_yielding(self):
        for path in page_paths():
            with self.subTest(page=path):
                source = read_page(path)
                body = function_body(source, "layoutProgressive")
                # the internal batched call is intentional and must stay
                self.assertIn("layoutOnce(nIt);", body)
                self.assertIn("setTimeout(step,30);", body)
                self.assertIn("totalIter=totalIter||80;", body)

    def test_draw_and_layout_functions_are_defined_once_per_page(self):
        for path in page_paths():
            with self.subTest(page=path):
                source = read_page(path)
                for name in LAYOUT_FUNCTIONS:
                    self.assertEqual(
                        1, source.count("function %s(" % name),
                        "duplicate definition of %s in %s" % (name, path),
                    )

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_inline_script_parses(self):
        for path in page_paths():
            with self.subTest(page=path):
                blocks = inline_scripts(read_page(path))
                self.assertEqual(1, len(blocks), "expected one inline script block")
                handle = tempfile.NamedTemporaryFile(
                    "w", suffix=".js", delete=False, encoding="utf-8")
                try:
                    handle.write(blocks[0])
                    handle.close()
                    result = subprocess.run(
                        ["node", "--check", handle.name],
                        capture_output=True, text=True, timeout=60,
                    )
                finally:
                    os.unlink(handle.name)
                self.assertEqual(0, result.returncode, result.stderr)


@unittest.skipUnless(PAGE.exists(), "versioned explorer page is missing")
class StepperMutualExclusionTests(unittest.TestCase):
    """(ad-work 1) One layout stepper at a time — no overlapping runs.

    The 50ms debounce alone was not enough: once the timer fired, `_layoutTimer`
    was nulled while the stepper ran ~600ms, so any trigger inside that window
    started a SECOND stepper over the same shared node positions. The run token
    makes a mid-flight trigger queue instead of start, and the queued request is
    replayed exactly once when the live stepper finishes.
    """

    def test_every_page_copy_declares_the_run_token(self):
        for path in page_paths():
            with self.subTest(page=path):
                source = read_page(path)
                self.assertIn(TOKEN_DECLARATION, source)
                self.assertGreaterEqual(
                    source.count(TOKEN), 3,
                    "the run token must be declared, claimed and released in %s" % path,
                )
                self.assertGreaterEqual(source.count(PENDING), 3)

    def test_token_is_checked_before_a_new_stepper_starts(self):
        for path in page_paths():
            with self.subTest(page=path):
                body = function_body(read_page(path), "layoutProgressive")
                guard = body.index("if(%s){" % TOKEN)          # checked...
                self.assertLess(guard, body.index("%s=true;" % TOKEN),
                                "the token must be checked before it is claimed")
                self.assertLess(guard, body.index("layoutOnce(nIt);"),
                                "the token must be checked before any stepping")
                # the mid-flight request is REMEMBERED (queued), not dropped
                self.assertIn("%s={totalIter:totalIter, onDone:onDone||null};" % PENDING,
                              body[guard:])
                # ...and replayed only after the live stepper releases the token
                release = body.index("function finish(){")
                tail = body[release:]
                self.assertIn("%s=false;" % TOKEN, tail)
                self.assertIn("var next=%s; %s=null;" % (PENDING, PENDING), tail)
                self.assertIn("if(next) layoutProgressive(next.totalIter, next.onDone);", tail)
                self.assertLess(
                    tail.index("%s=false;" % TOKEN),
                    tail.index("if(next) layoutProgressive("),
                    "the queued stepper must not start before the token is released",
                )

    def test_one_stepper_definition_and_no_sync_call_sites_per_copy(self):
        """(ii) + (iii): single definitions, zero synchronous full-layout sites."""
        for path in page_paths():
            with self.subTest(page=path):
                source = read_page(path)
                for name in ("scheduleLayout", "layoutProgressive"):
                    self.assertEqual(1, source.count("function %s(" % name),
                                     "expected exactly one %s in %s" % (name, path))
                self.assertEqual([], SYNC_FULL_LAYOUT.findall(source),
                                 "synchronous layoutOnce(); draw(); is back")
                self.assertEqual([], BARE_LAYOUT_ONCE_CALL.findall(source),
                                 "a zero-argument layoutOnce() call site is present")

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_second_stepper_never_overlaps_a_live_one(self):
        """Drive the real stepper code: a mid-flight trigger queues, replays once."""
        for path in page_paths():
            with self.subTest(page=path):
                script = NODE_HARNESS.replace(
                    "__STEPPER__", stepper_block(read_page(path)))
                handle = tempfile.NamedTemporaryFile(
                    "w", suffix=".js", delete=False, encoding="utf-8")
                try:
                    handle.write(script)
                    handle.close()
                    result = subprocess.run(
                        ["node", handle.name],
                        capture_output=True, text=True, timeout=90,
                    )
                finally:
                    os.unlink(handle.name)
                self.assertEqual(0, result.returncode, result.stderr)
                observed = json.loads(result.stdout)

                # stepper A (20 steps of 1 iteration) then the queued stepper B
                # (20 steps of 2 iterations) — never interleaved, never repeated.
                self.assertEqual([1] * 20 + [2] * 20, observed["seq"],
                                 "steppers interleaved or replayed the wrong count")

                self.assertTrue(observed["runningAfterStart"], "token not claimed")
                self.assertTrue(observed["runningAfterTrigger"],
                                "the token was dropped while a stepper was live")
                self.assertTrue(observed["at120"]["running"],
                                "no stepper was live 120ms in")
                self.assertTrue(observed["at120"]["pending"],
                                "the mid-flight trigger was not queued")
                self.assertEqual([1], sorted(set(observed["at120"]["seq"])),
                                 "a second stepper started while the first was live")
                self.assertTrue(observed["onDoneA"])
                self.assertEqual(1, observed["onDoneB"],
                                 "the queued request must run exactly once")
                self.assertFalse(observed["runningAtEnd"], "token leaked")
                self.assertFalse(observed["pendingAtEnd"], "queued request left behind")


if __name__ == "__main__":
    unittest.main()
