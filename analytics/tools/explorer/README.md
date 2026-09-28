# GE16 Graph & Vector Explorer (reference app — NOT for Aila)

Local, non-hosted visualiser for the GE16 research layer. Built per user request
(8 Aug 2026); extended (T2 packet, Sep 2026) to browse **all four database
families** the programme produces. It is a 2_ANALYTICS-owned reference tool: it
is not deployed anywhere and Aila is permanently excluded from it.

## What it shows — four tabs, four database families

**Tab 1 — Graph** (family 2: knowledge graph)

`work/graph/ge16-knowledge-graph.json` (today: 4,387 nodes / 567,289 edges),
converted to a browser bundle with news→news edges thinned to 4,000
(today: 4,387 nodes / 14,785 edges / 2.92 MB bundle). Node types are
colour-coded: person (blue), party (green), seat (purple), cluster (orange),
news (red), scenario (teal), state (lime), federal (pink), federal territory
(yellow — Kuala Lumpur, Putrajaya, Labuan).

- Search a node by name (e.g. "Anwar", "PAS", "P115") → ego view of its
  neighbours at 1–2 hops
- Filter node types and edge types via chips; click a node for its attributes
- Reset view returns to the full graph
- **Live count badges** (nodes · edges · news→news kept · edge types · bundle
  size · schema · generated-at) read straight from the loaded bundle

**Tab 2 — Vector Search** (family 3: the 7 vector DBs)

Semantic similarity over all seven stores, with a **store chooser** listing
every store and its live record count:

| store | records (today) | matchable fields |
|---|---|---|
| personnel | 1,109 | name · role · party · bloc · seat · constituency · state |
| figures | 134 | name · role · party · bloc · seat |
| parties | 30 | name · bloc |
| seats | 824 | name · seat code · constituency · state · kind |
| news | 2,367 | title · date · source · category · blocs · parties · seats |
| scenarios | 14 | name · template · logic · recommendation |
| clusters | 28 | name |

- Hybrid score = 55% cosine + 45% keyword match on name/role/party/seat text.
- Clicking a store chip switches store; the result header and every result card
  state **which store matched** (`store: personnel`), and the detail modal shows
  the database plus its record count.
- NOTE: the query is embedded with a deterministic hash pseudo-embedder that
  mirrors the build-time embedder (no in-browser model) — so pure-semantic
  queries return lower-confidence scores than keyword-backed ones. This is an
  exploration tool, not a production retrieval system.

**Tab 3 — Events** (family 1: events DB, read-only)

`work/events/ge16-events.db` exported to `events_data.js`: today 512 events /
117 stories / 2,141 entities / 315 sources / 52 event links, 564 KB. The page
never queries SQLite — it filters the preloaded slice with plain JS.

- Filters: free text over headline + detail + entity names, date range
  (from/to), level (federal/state, derived from jurisdiction), state, category
  (event_type), bloc, significance — each option carrying its event count
- Paginated 50 rows/page, newest first (date range today 2024-03-19 →
  2027-12-19, i.e. includes the constitutional-clock projections)
- Click a row for the full record: detail text, event id, category, precision,
  significance/confidence, window class, dossier + section, source layer,
  corroboration, blocs, entities with roles, the story it belongs to, and any
  event links (follows/precedes/supersedes)
- A Stories table lists all 117 stories with theme, anchor, status, event count
  and last update

**Tab 4 — Trackers** (family 4: 1_DATA trackers, read-only)

`1_DATA/research/trackers/` exported to `trackers_data.js` (1.5 MB):

- **Accepted news** — 2,367 items: search by title/source, filter by source,
  month (YYYY-MM), category, and bloc facet chips; each row shows date, source,
  category, blocs, language, judged score and the source link
- **Polls tracked** — 234 deduped items with pollster and the scan date joined
  from `ge16-poll-tracker-log.md` (13 scan runs), newest scan first
- **News feed** — the 120-item 7-day window as link cards

## Files

| File | Purpose |
|---|---|
| `index.html` | The app (self-contained, vanilla JS + canvas, no CDN, no framework) |
| `build_data.py` | Thin in-place shim over `../tools/graph-explorer/build_data.py` |
| `serve_no_cache.py` | Local server that forces `Cache-Control: no-store` |
| `launch.sh`, `GE16 Graph Explorer.app` | Launchers (double-click bundle) |
| `graph.json` / `graph_data.js` | Graph bundle (`window.GRAPH_DATA`) |
| `*_vecs.js` | The 7 vector stores (e.g. `window.PERSONNEL_VECS`) |
| `events_data.js` | Events DB slice (`window.EVENTS_DATA`) |
| `trackers_data.js` | Tracker slice (`window.TRACKERS_DATA`) |
| `vec-map.html` + `vec_map_data.js` | Separate 2-D PCA scatter of the stores |

## How to run

**Easiest — double-click `GE16 Graph Explorer.app`** (in this folder):

- Server not running → it asks "Start Server?" → click Start → the local server
  launches in the background (survives even after the app closes) and your
  browser opens http://localhost:8765 automatically.
- Server already running → it offers "Open in Browser" / "Stop Server".
- First launch may show a one-time macOS prompt ("Terminal wants to run a
  command") — click Allow.

**Manual alternative:**

```bash
cd "/Users/faisal.muthalib/Documents/HermesWorkFolder/Malaysia General Election v2/2_ANALYTICS/GE16-Graph-Explorer"
python3 serve_no_cache.py 8765      # or: python3 -m http.server 8765
# open http://localhost:8765
```

The app also works straight from `file://` — every bundle is loaded through a
`<script src="...">` tag, which browsers allow; `fetch()` of local JSON would
be blocked. The server is only needed for the no-cache guarantee and for
`graph.json` (the JSON fallback path).

Deep links: `index.html#graph`, `#vector`, `#events`, `#trackers` open straight
on a tab (handy for headless checks and bookmarking).

## How to refresh after a research-layer rebuild

```bash
cd "/Users/faisal.muthalib/Documents/HermesWorkFolder/Malaysia General Election v2/2_ANALYTICS"
./.venv/bin/python "GE16-Graph-Explorer/build_data.py"                  # all four families
./.venv/bin/python "GE16-Graph-Explorer/build_data.py" --only events,trackers   # fast subset
```

Re-run after `build_knowledge_graph.py`, the vector-DB builds, the events-DB
build, or any tracker update. The same script also refreshes the versioned
staging bundle when run from its tracked location:

```bash
./.venv/bin/python tools/graph-explorer/build_data.py     # -> work/graph-explorer/
```

Every input is read-only (SQLite via a `mode=ro` URI + `PRAGMA query_only`,
JSON/markdown via plain reads); the builders never mutate `1_DATA`.
