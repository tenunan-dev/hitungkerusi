#!/usr/bin/env python3
"""Build the GE16 Graph Explorer data files IN PLACE (reference app, NOT for Aila).

This is the double-click app directory: 2_ANALYTICS/GE16-Graph-Explorer. The
pages here (index.html, vec-map.html) load their data through plain
<script src="..."> tags, because browsers block fetch() of local JSON under
file://. So this app's generated bundles MUST sit next to index.html and are
written in place by design.

The implementation is NOT duplicated here: this shim imports the tracked
builder 2_ANALYTICS/tools/graph-explorer/build_data.py (the versioned staging
entry point, which normally writes to work/graph-explorer/) and re-points its
OUT at this directory with STATIC_PAGES disabled — the pages already live here.
Both entry points therefore always produce byte-identical bundles.

Staging-dir note: because this directory is itself 2_ANALYTICS-owned and its
outputs are the app's own runtime artifacts (not migration staging), nothing is
written under work/ or work/graph-explorer-stage/. Every input is read-only:
the events DB through a mode=ro SQLite URI (+ PRAGMA query_only) and the
tracker files under sibling 1_DATA/research/trackers through plain reads. Live
tracker files are never mutated.

The four database families surfaced (same list as the tracked builder):

  1. events DB        work/events/ge16-events.db          -> events_data.js
  2. knowledge graph  work/graph/ge16-knowledge-graph.json -> graph.json,
                                                             graph_data.js
  3. 7 vector DBs     work/figures/ge16-*-meta.json       -> *_vecs.js
  4. 1_DATA trackers  1_DATA/research/trackers/*.json|md  -> trackers_data.js

Usage (run from anywhere):

  python3 "2_ANALYTICS/GE16-Graph-Explorer/build_data.py"            # all
  python3 "2_ANALYTICS/GE16-Graph-Explorer/build_data.py" --only events,trackers

Re-run after any graph / vector / events / tracker rebuild to refresh the app.
"""

import importlib.util
import os
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))
TRACKED_BUILDER = os.path.join(
    os.path.dirname(APP_DIR), "tools", "graph-explorer", "build_data.py")


def load_builder(path):
    spec = importlib.util.spec_from_file_location("ge16_graph_explorer_builder", path)
    if spec is None or spec.loader is None:
        raise SystemExit("cannot load builder: %s" % path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not os.path.exists(TRACKED_BUILDER):
        raise SystemExit("tracked builder not found: %s" % TRACKED_BUILDER)
    builder = load_builder(TRACKED_BUILDER)
    sections, out_dir = builder.parse_args(argv)
    # Write the bundles next to this app's index.html, and do not re-copy the
    # static pages onto themselves.
    setattr(builder, "OUT", out_dir or APP_DIR)
    setattr(builder, "STATIC_PAGES", ())
    print("GE16-Graph-Explorer in-place build -> %s" % builder.OUT)
    builder.main(sections)


if __name__ == "__main__":
    main()
