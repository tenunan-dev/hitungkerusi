"""Contract tests for the GE16 baseline rebuild orchestrator.

Hermetic: every pipeline command is stubbed through the injectable command runner
and every artifact lives in a temp directory — no network, no real collector, no
real vector model. The stub mimics the documented file contracts (candidate
payloads, judge batches, judged batches, merge-only commits, the events sqlite
schema, the graph json and the VDB meta/npy pairs) so the orchestrator's ordering,
verify-then-swap, idempotence and merge-only guarantees are exercised for real.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.baseline import rebuild_baseline as rb  # noqa: E402

COLLECTION = "2026-09-26T12:00:00+00:00"
VDB_NAMES = ("parties", "personnel", "figures", "seats", "news", "scenarios", "clusters")
SEED_ACCEPTED = 1095


def sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, payload) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def seed_accepted(count=SEED_ACCEPTED) -> list:
    months = ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06",
              "2026-07", "2026-08", "2026-09"]
    items = []
    for index in range(count):
        month = months[index % len(months)]
        day = (index % 27) + 1
        items.append({"query": f"seed-{index}", "title": f"Seed accepted item {index}",
                      "date": f"{month}-{day:02d}T08:00:00", "source": f"src{index % 9}",
                      "link": f"https://example.test/seed/{index}", "lang": "en",
                      "category": "election", "blocs": [], "parties": [], "seats": [],
                      "score": 0.6, "judged_at": "2026-09-23T01:00:00+00:00"})
    items[0]["date"] = "2026-01-02T08:00:00"
    items[1]["date"] = "2026-09-23T08:00:00"
    return items


class StubPipeline:
    """The whole pipeline as one stubbed command runner (deterministic, offline)."""

    def __init__(self, analytics_root, *, per_pipeline_items=4, events=512, entities=2141,
                 sources=315, stories=117, fail_events_floor=False, drop_accepted=0,
                 polls_found=2, fail_parties_builder=False):
        self.analytics = Path(analytics_root)
        self.trackers = self.analytics.parent / "1_DATA" / "research" / "trackers"
        self.figures = self.analytics / "work" / "figures"
        self.events_dir = self.analytics / "work" / "events"
        self.graph_dir = self.analytics / "work" / "graph"
        self.per_pipeline_items = per_pipeline_items
        self.events = events
        self.entities = entities
        self.sources = sources
        self.stories = stories
        self.fail_events_floor = fail_events_floor
        self.drop_accepted = drop_accepted
        self.polls_found = polls_found
        self.fail_parties_builder = fail_parties_builder
        self.build_seq = 0
        self.calls: list = []

    # -- helpers -------------------------------------------------------------
    def _out(self, env, tracker_dir=None):
        """Where the collector writes its tracker files.

        Mirrors the real collectors' ``GE16_TRACKER_OUT_DIR`` knob: the orchestrator
        sets it so a dry run writes nothing live and a merge lands in the run stage
        first (MED-1/MED-2). The judge script has no knob — it writes into the
        ``--tracker-dir`` it was given, which is the stage during a dry run. Unset and
        no stage -> the live tracker directory, as before.
        """
        staged = (env or {}).get("GE16_TRACKER_OUT_DIR")
        if staged:
            return Path(staged)
        if tracker_dir:
            return Path(tracker_dir)
        return self.trackers

    def _read_path(self, env, name):
        """Reads prefer this run's staged copy, else the live file (collector rule)."""
        staged = (env or {}).get("GE16_TRACKER_OUT_DIR")
        if staged and (Path(staged) / Path(name).name).exists():
            return Path(staged) / Path(name).name
        return self.trackers / Path(name).name

    def _candidates(self, pipeline):
        name = ("ge16-news-backfill-candidates.json" if pipeline == "backfill"
                else "ge16-news-candidates.json")
        return self.trackers / name

    def _judge_manifest(self, pipeline):
        name = ("ge16-news-backfill-judge-manifest.json" if pipeline == "backfill"
                else "ge16-news-judge-manifest.json")
        return self.trackers / name

    def _batch_name(self, pipeline, number):
        fmt = ("ge16-news-backfill-judge-batch-%d.json" if pipeline == "backfill"
               else "ge16-news-judge-batch-%d.json")
        return self.trackers / (fmt % number)

    def _judged_name(self, pipeline, number):
        fmt = ("ge16-news-backfill-judged-batch-%d.json" if pipeline == "backfill"
               else "ge16-news-judged-batch-%d.json")
        return self.trackers / (fmt % number)

    def _candidate_items(self, pipeline):
        return [{"query": f"{pipeline}:{index}", "title": f"{pipeline} new item {index}",
                 "desc": f"description {index}", "date": "2026-09-25T09:00:00",
                 "source": "stubsrc", "link": f"https://example.test/{pipeline}/{index}"}
                for index in range(self.per_pipeline_items)]

    def _collect(self, pipeline, env=None):
        items = self._candidate_items(pipeline)
        out = self._out(env)
        out.mkdir(parents=True, exist_ok=True)
        write_json(out / self._candidates(pipeline).name, {
            "schema": "ge16.news-backfill-candidates.v1", "generated_at": COLLECTION,
            "collection_id": COLLECTION, "count": len(items), "items": items})

    def _judge_input(self, pipeline, env=None):
        items = json.loads(self._read_path(env, self._candidates(pipeline).name)
                           .read_text())["items"]
        out = self._out(env)
        out.mkdir(parents=True, exist_ok=True)
        size = 50
        batches = []
        for index in range(0, len(items), size):
            number = index // size + 1
            chunk = items[index:index + size]
            write_json(out / self._batch_name(pipeline, number).name, {
                "schema": "ge16.news-backfill-judge-batch.v1", "batch": number,
                "collection_id": COLLECTION, "count": len(chunk), "items": chunk})
            batches.append({"batch": number, "file": self._batch_name(pipeline, number).name,
                            "count": len(chunk),
                            "judged_file": self._judged_name(pipeline, number).name})
        write_json(out / self._judge_manifest(pipeline).name, {
            "schema": "ge16.news-backfill-judge-manifest.v1", "generated_at": COLLECTION,
            "collection_id": COLLECTION, "batch_size": size, "total": len(items),
            "batch_count": len(batches), "batches": batches})

    def _judge(self, pipeline, stats_path, env=None, tracker_dir=None):
        """Judges the batches found in ``tracker_dir`` (the judge's --tracker-dir).

        Writes follow the orchestrator's write dir (the staged one when set).
        """
        read_dir = Path(tracker_dir) if tracker_dir and \
            (Path(tracker_dir) / self._judge_manifest(pipeline).name).exists() \
            else self.trackers
        manifest = json.loads((read_dir / self._judge_manifest(pipeline).name).read_text())
        out = self._out(env, tracker_dir)
        out.mkdir(parents=True, exist_ok=True)
        calls = 0
        for entry in manifest["batches"]:
            number = entry["batch"]
            batch_path = read_dir / self._batch_name(pipeline, number).name
            batch = json.loads(batch_path.read_text())
            accepted = [{**item, "category": "election", "blocs": ["PN"], "parties": [],
                         "seats": [], "lang": "en", "score": 0.7,
                         "judged_by": "deepseek", "judge_model": "deepseek-chat"}
                        for item in batch["items"]]
            write_json(out / self._judged_name(pipeline, number).name, {
                "batch": number, "collection_id": COLLECTION, "count": batch["count"],
                "accepted_count": len(accepted), "rejected_count": 0,
                "source_sha256": sha(batch_path), "accepted": accepted})
            calls += 1
        if pipeline == "tracker":
            items = []
            for entry in manifest["batches"]:
                items.extend(json.loads((out / self._judged_name(pipeline, entry["batch"])
                                         .name).read_text())["accepted"])
            write_json(out / "ge16-news-judged.json", {
                "generated_at": COLLECTION, "count": len(items), "accepted": items})
        if stats_path:
            write_json(stats_path, {"calls": calls, "retries": 1, "failures": 0,
                                    "model": "deepseek-chat", "batches": len(manifest["batches"]),
                                    "judged": len(manifest["batches"]), "already_judged": 0,
                                    "consolidated": None})

    def _commit(self, pipeline, env=None):
        accepted_path = self._read_path(env, "ge16-news-accepted.json")
        current = json.loads(accepted_path.read_text())
        items = current.get("items", [])
        if self.drop_accepted:
            items = items[:len(items) - self.drop_accepted]
        titles = {(item.get("title") or "")[:150] for item in items}
        if pipeline == "backfill":
            judged = []
            manifest = json.loads(self._read_path(env, self._judge_manifest(pipeline).name)
                                  .read_text())
            for entry in manifest["batches"]:
                judged.extend(json.loads(
                    self._read_path(env, self._judged_name(pipeline, entry["batch"]).name)
                    .read_text())["accepted"])
        else:
            judged = json.loads(
                self._read_path(env, "ge16-news-judged.json").read_text())["accepted"]
        added = []
        for item in judged:
            title = (item.get("title") or "")[:150]
            if not title or title in titles:
                continue
            titles.add(title)
            added.append({key: item.get(key) for key in
                          ("query", "title", "date", "source", "link", "lang", "category",
                           "blocs", "parties", "seats", "score")})
        merged = added + items
        out = self._out(env)
        out.mkdir(parents=True, exist_ok=True)
        write_json(out / "ge16-news-accepted.json",
                   {"generated_at": COLLECTION if pipeline == "tracker"
                    else current.get("generated_at", COLLECTION),
                    "count": len(merged), "items": merged})
        write_json(out / "ge16-news-feed.json", {
            "schema": "ge16-news-feed v1", "generated_at": COLLECTION,
            "count": len(merged), "items": merged})

    def _events_db(self, out_path):
        out_path = Path(out_path)
        if out_path.exists():  # the orchestrator seeds this path with the prior db
            out_path.unlink()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        self.build_seq += 1  # a rebuild always stamps its own built_at
        conn = sqlite3.connect(out_path)
        conn.executescript(
            "CREATE TABLE events (event_id TEXT PRIMARY KEY, event_date TEXT);"
            "CREATE TABLE entities (entity_id TEXT PRIMARY KEY);"
            "CREATE TABLE sources (source_id INTEGER PRIMARY KEY, url TEXT);"
            "CREATE TABLE stories (story_id TEXT PRIMARY KEY, status TEXT, event_count INT);"
            "CREATE TABLE story_events (story_id TEXT, event_id TEXT);"
            "CREATE TABLE claim_reviews (claim_id TEXT PRIMARY KEY);"
            "CREATE TABLE event_links (relation TEXT);"
            "CREATE TABLE dossier_notes (body TEXT);"
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT);")
        events = 400 if self.fail_events_floor else self.events
        conn.executemany("INSERT INTO events VALUES (?, ?)",
                         [(f"e{i}", "2026-05-01") for i in range(events)])
        conn.executemany("INSERT INTO entities VALUES (?)",
                         [(f"p{i}",) for i in range(self.entities)])
        conn.executemany("INSERT INTO sources VALUES (?, ?)",
                         [(i, f"https://example.test/s{i}") for i in range(self.sources)])
        conn.executemany("INSERT INTO stories VALUES (?, ?, ?)",
                         [(f"s{i}", "open", 1) for i in range(self.stories)])
        conn.execute("INSERT INTO schema_meta VALUES ('built_at', ?)",
                     (f"2026-09-26T12:00:{self.build_seq:02d}+00:00",))
        conn.commit()
        conn.close()

    def _write_vdbs(self, out_dir, only=None):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        for index, name in enumerate(VDB_NAMES):
            if only is not None and name not in only:
                continue
            rows = 30 + index
            np.save(out_dir / f"ge16-{name}-vectors.npy",
                    np.zeros((rows, 4), dtype="float32"))
            write_json(out_dir / f"ge16-{name}-meta.json",
                       {"model": "stub", "dim": 4, "count": rows,
                        "items": [{"id": f"{name}-{i}"} for i in range(rows)]})
        for extra in ("ge16-parties.json", "ge16-personnel.json", "ge16-key-figures.json"):
            write_json(out_dir / extra, {"items": []})

    # -- the runner ----------------------------------------------------------
    def __call__(self, argv, env, cwd):
        argv = list(argv)
        if argv[1:2] == ["-m"]:
            module = argv[2]
        else:
            module = Path(argv[1]).name
        args = argv[3:] if argv[1:2] == ["-m"] else argv[2:]
        self.calls.append({"module": module, "args": list(args),
                           "figures_out": env.get("GE16_FIGURES_OUT_DIR"),
                           "graph_out": env.get("GE16_GRAPH_OUT_DIR"),
                           "tracker_out": env.get("GE16_TRACKER_OUT_DIR"),
                           "has_deepseek_key": bool(env.get("DEEPSEEK_API_KEY")),
                           "has_deepseek_model": bool(env.get("DEEPSEEK_MODEL")),
                           "has_selfheal_state": bool(env.get("GE16_SELFHEAL_STATE")),
                           "caps": (env.get("GE16_BACKFILL_SOURCE_CAP"),
                                    env.get("GE16_BACKFILL_TOTAL_CAP")),
                           "window": (env.get("GE16_BACKFILL_START"),
                                      env.get("GE16_BACKFILL_END"))})
        if module in ("track_ge16_news.py", "ge16_news_backfill.py"):
            pipeline = "tracker" if module == "track_ge16_news.py" else "backfill"
            if "--judge-input" in args:
                self._judge_input(pipeline, env)
            elif "--commit" in args:
                self._commit(pipeline, env)
            else:
                self._collect(pipeline, env)
            return 0, f"{pipeline} stub ok", ""
        if module == "ge16_judge_deepseek.py":
            pipeline = args[args.index("--pipeline") + 1]
            stats_path = args[args.index("--stats") + 1]
            tracker_dir = args[args.index("--tracker-dir") + 1]
            self._judge(pipeline, stats_path, env, tracker_dir)
            return 0, json.dumps({"judge_status": "ok"}), ""
        if module == "track_ge16_polls.py":
            out = self._out(env)
            out.mkdir(parents=True, exist_ok=True)
            db = out / "ge16-polls-tracked.json"
            live = self.trackers / "ge16-polls-tracked.json"
            source = db if db.exists() else live
            payload = json.loads(source.read_text()) if source.exists() else {"seen": []}
            found = 0
            for index in range(self.polls_found):
                key = f"stub-poll-{index}"
                if key not in payload["seen"]:
                    payload["seen"].append(key)
                    found += 1
            write_json(db, payload)
            (out / "ge16-poll-tracker-log.md").write_text("scan\n")
            if not found:
                return 0, "No new GE16 poll items detected since last scan.", ""
            return 0, f"NEW GE16 POLL ITEMS FOUND ({found}):", ""
        if module == "tools.figures.update_parties_from_cron":
            # the real builder reads <out>/ge16-personnel.json (compute_from_personnel)
            # and the wrapper swallows a builder failure into "REBUILD FAILED"
            if self.fail_parties_builder or not (
                    Path(env["GE16_FIGURES_OUT_DIR"]) / "ge16-personnel.json").exists():
                return 0, "REBUILD FAILED (rc=1)", ""
            self._write_vdbs(env["GE16_FIGURES_OUT_DIR"], only={"parties"})
            return 0, "party update files OK", ""
        if module == "tools.figures.update_personnel_from_cron":
            self._write_vdbs(env["GE16_FIGURES_OUT_DIR"], only={"personnel"})
            return 0, "personnel update files OK", ""
        if module == "tools.figures.build_figures_vdb":
            self._write_vdbs(env["GE16_FIGURES_OUT_DIR"], only={"figures"})
            return 0, "vdbs written", ""
        if module == "tools.figures.build_remaining_vdbs":
            self._write_vdbs(env["GE16_FIGURES_OUT_DIR"],
                             only={"seats", "news", "scenarios", "clusters"})
            return 0, "vdbs written", ""
        if module == "tools.figures.build_vec_map":
            path = self.analytics / "work" / "graph-explorer" / "vec_map_data.js"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("window.VEC_MAP = {};\n")
            return 0, "vec map written", ""
        if module == "tools.events.build_events_db":
            out = Path(args[args.index("--out") + 1])
            self._events_db(out)
            return 0, json.dumps({"counts": {"events": self.events}}), ""
        if module == "tools.graph.build_knowledge_graph":
            out_dir = Path(env["GE16_GRAPH_OUT_DIR"])
            out_dir.mkdir(parents=True, exist_ok=True)
            nodes = {f"n{i}": {"id": f"n{i}"} for i in range(2810)}
            edges = [{"src": "n0", "dst": "n1"}] * 95721
            write_json(out_dir / "ge16-knowledge-graph.json", {
                "schema": "ge16-knowledge-graph v1", "generated_at": "2026-09-26T12:00:00",
                "node_count": len(nodes), "edge_count": len(edges),
                "nodes": nodes, "edges": edges})
            return 0, "graph written", ""
        raise AssertionError(f"unexpected command: {argv}")


class BaselineFixture:
    """A throwaway analytics + 1_DATA tree with a seeded baseline."""

    def __init__(self, tmp_path, **stub_options):
        self.tmp = Path(tmp_path)
        self.analytics = self.tmp / "2_ANALYTICS"
        self.data = self.tmp / "1_DATA"
        self.trackers = self.data / "research" / "trackers"
        self.figures = self.analytics / "work" / "figures"
        self.drafts = self.figures / "_draft"
        self.events_dir = self.analytics / "work" / "events"
        self.graph_dir = self.analytics / "work" / "graph"
        for path in (self.trackers, self.drafts, self.events_dir / "_draft", self.graph_dir,
                     self.analytics / "work" / "scenarios", self.analytics / "work" / "baseline"):
            path.mkdir(parents=True, exist_ok=True)
        write_json(self.drafts / "party-updates-2026-08-09.json",
                   {"layer": "party_updates", "date": "2026-08-09",
                    "parties": [{"name": "SEED"}]})
        write_json(self.drafts / "personnel-updates-2026-08-09.json",
                   {"layer": "cron_updates", "date": "2026-08-09",
                    "persons": [{"name": "Seed Person", "roles": []}]})
        write_json(self.trackers / "ge16-news-accepted.json",
                   {"generated_at": "2026-09-23T01:24:01+00:00",
                    "count": SEED_ACCEPTED, "items": seed_accepted()})
        write_json(self.trackers / "ge16-news-feed.json",
                   {"schema": "ge16-news-feed v1", "generated_at": COLLECTION,
                    "count": 100, "items": []})
        write_json(self.trackers / "ge16-polls-tracked.json", {"seen": ["existing"]})
        write_json(self.trackers / "ge16-general-news-tracked.json", {"seen": []})
        (self.trackers / "ge16-poll-tracker-log.md").write_text("seed\n")
        # a live baseline to regress against
        stub = StubPipeline(self.analytics, **stub_options)
        stub._events_db(self.events_dir / "ge16-events.db")
        stub._write_vdbs(self.figures)
        write_json(self.graph_dir / "ge16-knowledge-graph.json",
                   {"schema": "ge16-knowledge-graph v1", "generated_at": "2026-09-24",
                    "node_count": 2810, "edge_count": 95721,
                    "nodes": {f"n{i}": {} for i in range(2810)},
                    "edges": [{}] * 95721})
        self.stub = stub

    def rebuild(self, **kwargs):
        kwargs.setdefault("analytics_root", self.analytics)
        kwargs.setdefault("data_root", self.data)
        kwargs.setdefault("command_runner", self.stub)
        kwargs.setdefault("log", lambda *parts: None)
        return rb.Rebuild(**kwargs)


class LayerOrderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fixture = BaselineFixture(self.tmp.name)

    def test_layers_run_in_dependency_order_and_are_recorded(self):
        manifest = self.fixture.rebuild().run(["news", "polls", "figures", "events", "graph", "vdbs"])
        self.assertEqual(manifest["status"], "ok")
        self.assertEqual(manifest["order"],
                         ["news", "polls", "figures", "events", "graph", "vdbs"])
        self.assertEqual([name for name in manifest["layers"]],
                         ["news", "polls", "figures", "events", "graph", "vdbs", "verify"])
        modules = [call["module"] for call in self.fixture.stub.calls]
        first_seen = {}
        for index, module in enumerate(modules):
            first_seen.setdefault(module, index)
        self.assertLess(first_seen["ge16_news_backfill.py"], first_seen["track_ge16_polls.py"])
        self.assertLess(first_seen["track_ge16_polls.py"],
                        first_seen["tools.figures.build_figures_vdb"])
        self.assertLess(first_seen["tools.figures.build_figures_vdb"],
                        first_seen["tools.events.build_events_db"])
        self.assertLess(first_seen["tools.events.build_events_db"],
                        first_seen["tools.graph.build_knowledge_graph"])
        self.assertLess(first_seen["tools.graph.build_knowledge_graph"],
                        first_seen["tools.figures.build_remaining_vdbs"])
        # the backfill window is passed through the documented env knobs
        window = [call for call in self.fixture.stub.calls
                  if call["module"] == "ge16_news_backfill.py"][0]["window"]
        self.assertEqual(window[0], "2026-01-01")
        self.assertRegex(window[1], r"^\d{4}-\d{2}-\d{2}$")

    def test_personnel_vdb_is_built_before_the_parties_vdb(self):
        """build_parties_vdb reads <out>/ge16-personnel.json, so order is contract."""
        manifest = self.fixture.rebuild().run(["figures"])
        self.assertEqual(manifest["status"], "ok")
        modules = [call["module"] for call in self.fixture.stub.calls]
        self.assertLess(modules.index("tools.figures.update_personnel_from_cron"),
                        modules.index("tools.figures.update_parties_from_cron"))
        self.assertLess(modules.index("tools.figures.update_parties_from_cron"),
                        modules.index("tools.figures.build_figures_vdb"))
        self.assertEqual(manifest["layers"]["figures"]["status"], "ok")

    def test_swallowed_builder_failure_fails_the_layer_and_keeps_live_vdbs(self):
        """The update wrappers print REBUILD FAILED and still exit 0 — never trust it."""
        self.fixture.stub.fail_parties_builder = True
        live = self.fixture.figures / "ge16-parties-meta.json"
        before = live.read_bytes()
        manifest = self.fixture.rebuild().run(["figures"])
        self.assertEqual(manifest["status"], "failed")
        self.assertIn("figures_parties_builder_ok", manifest["layers"]["figures"]["error"])
        self.assertEqual(live.read_bytes(), before)

    def test_backfill_sweeps_repeat_until_a_sweep_adds_nothing(self):
        """The collector re-derives its slice every collect, so sweeps may be needed."""
        rebuild = self.fixture.rebuild(news_sweeps=3)
        manifest = rebuild.run(["news"])
        self.assertEqual(manifest["status"], "ok")
        sweeps = manifest["news_sweeps"]
        self.assertEqual([entry["sweep"] for entry in sweeps], [1, 2])
        self.assertEqual(sweeps[0]["added"], self.fixture.stub.per_pipeline_items)
        self.assertEqual(sweeps[1]["added"], 0)
        self.assertEqual(manifest["layers"]["news"]["counts_after"]["count"],
                         SEED_ACCEPTED + self.fixture.stub.per_pipeline_items * 2)

    def test_deep_sweep_widens_the_collector_caps_through_env(self):
        self.fixture.rebuild(news_sweeps=1, news_depth="deep").run(["news"])
        backfill = [call for call in self.fixture.stub.calls
                    if call["module"] == "ge16_news_backfill.py"]
        self.assertEqual(backfill[0]["caps"], ("120", "1200"))
        default = BaselineFixture(self.tmp.name).rebuild(news_sweeps=1).run(["news"])
        self.assertIsNotNone(default)

    def test_requested_subset_is_still_dependency_ordered(self):
        manifest = self.fixture.rebuild().run(["graph", "news"])
        self.assertEqual(manifest["order"], ["news", "graph"])
        self.assertNotIn("events", manifest["layers"])

    def test_manifest_records_judge_backend_and_counts(self):
        manifest = self.fixture.rebuild(judge_backend="deepseek").run(["news"])
        layer = manifest["layers"]["news"]
        self.assertEqual(layer["judge"]["backfill"]["backend"], "deepseek")
        self.assertEqual(layer["judge"]["backfill"]["model"], "deepseek-chat")
        self.assertGreater(layer["counts_after"]["count"], layer["counts_before"]["count"])
        self.assertEqual(layer["counts_after"]["count"],
                         layer["counts_after"]["envelope_count"])
        self.assertIn("prior_set_sha256", layer)


class VerifyThenSwapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_failed_invariant_keeps_live_artifact_and_records_failure(self):
        fixture = BaselineFixture(self.tmp.name, fail_events_floor=True, events=512)
        live = fixture.events_dir / "ge16-events.db"
        before = sha(live)
        rebuild = fixture.rebuild()
        rebuild.stage_dir = fixture.analytics / "work" / "baseline" / "stage" / "t1"
        manifest = rebuild.run(["events"])
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["aborted_at"], "events")
        self.assertEqual(manifest["layers"]["events"]["status"], "failed")
        self.assertIn("events_floor", manifest["layers"]["events"]["error"])
        self.assertEqual(sha(live), before, "live events db must stay byte-identical")
        self.assertTrue(rebuild.events_staged.exists(), "the rejected staging file stays put")
        failed = [check for check in manifest["checks_failed"]]
        self.assertTrue(any(check["check"] == "events_floor" for check in failed))

    def test_strict_mode_aborts_remaining_layers(self):
        fixture = BaselineFixture(self.tmp.name, fail_events_floor=True)
        manifest = fixture.rebuild().run(["events", "graph", "vdbs"])
        self.assertEqual(manifest["aborted_at"], "events")
        self.assertEqual(sorted(manifest["layers"]), ["events"])

    def test_continue_on_error_keeps_rebuilding(self):
        fixture = BaselineFixture(self.tmp.name, fail_events_floor=True)
        manifest = fixture.rebuild(strict=False).run(["events", "graph"])
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["layers"]["events"]["status"], "failed")
        self.assertEqual(manifest["layers"]["graph"]["status"], "ok")

    def test_graph_regression_keeps_live_graph(self):
        fixture = BaselineFixture(self.tmp.name)
        graph = fixture.graph_dir / "ge16-knowledge-graph.json"
        before = sha(graph)
        # a graph that shrinks must be refused
        def shrinking_graph(argv, env, cwd):
            out_dir = Path(env["GE16_GRAPH_OUT_DIR"])
            out_dir.mkdir(parents=True, exist_ok=True)
            write_json(out_dir / "ge16-knowledge-graph.json",
                       {"nodes": {"a": {}}, "edges": [], "node_count": 1, "edge_count": 0})
            return 0, "", ""
        rebuild = fixture.rebuild(command_runner=shrinking_graph)
        manifest = rebuild.run(["graph"])
        self.assertEqual(manifest["layers"]["graph"]["status"], "failed")
        self.assertIn("graph_nodes_no_regression", manifest["layers"]["graph"]["error"])
        self.assertEqual(sha(graph), before)

    def test_vdb_meta_row_mismatch_is_refused(self):
        fixture = BaselineFixture(self.tmp.name)

        def bad_vdb(argv, env, cwd):
            out = Path(env["GE16_FIGURES_OUT_DIR"])
            out.mkdir(parents=True, exist_ok=True)
            for name in VDB_NAMES:
                np.save(out / f"ge16-{name}-vectors.npy", np.zeros((5, 4), dtype="float32"))
                write_json(out / f"ge16-{name}-meta.json", {"count": 9, "dim": 4, "items": []})
            return 0, "", ""
        rebuild = fixture.rebuild(command_runner=bad_vdb)
        manifest = rebuild.run(["vdbs"])
        self.assertEqual(manifest["layers"]["vdbs"]["status"], "failed")
        self.assertIn("meta_rows", manifest["layers"]["vdbs"]["error"])
        # the live VDBs were never replaced
        self.assertEqual(rb.vdb_counts(fixture.figures, "seats")["rows"], 33)


class MergeOnlyNewsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_prior_accepted_set_is_a_subset_after_the_merge(self):
        fixture = BaselineFixture(self.tmp.name)
        before = json.loads((fixture.trackers / "ge16-news-accepted.json").read_text())
        manifest = fixture.rebuild().run(["news"])
        layer = manifest["layers"]["news"]
        self.assertEqual(layer["status"], "ok")
        after = json.loads((fixture.trackers / "ge16-news-accepted.json").read_text())
        self.assertEqual(after["count"], len(after["items"]))
        self.assertGreater(after["count"], before["count"])
        before_signatures = {rb.accept_signature(item) for item in before["items"]}
        after_signatures = {rb.accept_signature(item) for item in after["items"]}
        self.assertTrue(before_signatures <= after_signatures)
        self.assertEqual(layer["prior_set_sha256"],
                         rb.sha256_bytes("\n".join(sorted(before_signatures)).encode()))
        self.assertTrue(layer["checks"] and all(check["ok"] for check in layer["checks"]))

    def test_deleted_accepted_item_fails_the_layer_and_keeps_the_owner_record(self):
        fixture = BaselineFixture(self.tmp.name, drop_accepted=7)
        accepted = fixture.trackers / "ge16-news-accepted.json"
        before = sha(accepted)
        manifest = fixture.rebuild().run(["news"])
        self.assertEqual(manifest["layers"]["news"]["status"], "failed")
        self.assertIn("news_merge_only", manifest["layers"]["news"]["error"])
        self.assertEqual(sha(accepted), before, "the owner record must stay byte-identical")

    def test_accepted_below_the_floor_fails(self):
        fixture = BaselineFixture(self.tmp.name)
        write_json(fixture.trackers / "ge16-news-accepted.json",
                   {"generated_at": COLLECTION, "count": 10, "items": seed_accepted(10)})
        manifest = fixture.rebuild().run(["news"])
        self.assertEqual(manifest["layers"]["news"]["status"], "failed")
        self.assertIn("news_floor", manifest["layers"]["news"]["error"])

    def test_envelope_is_preserved(self):
        fixture = BaselineFixture(self.tmp.name)
        fixture.rebuild().run(["news"])
        payload = json.loads((fixture.trackers / "ge16-news-accepted.json").read_text())
        self.assertEqual(sorted(payload), ["count", "generated_at", "items"])


class IdempotenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_second_run_with_no_new_source_data_is_a_noop(self):
        fixture = BaselineFixture(self.tmp.name)
        first = fixture.rebuild(run_id="first").run(["news", "polls", "figures", "events",
                                                     "graph", "vdbs"])
        self.assertEqual(first["status"], "ok")
        self.assertFalse(first["noop"])
        second = fixture.rebuild(run_id="second").run(["news", "polls", "figures", "events",
                                                       "graph", "vdbs"])
        self.assertEqual(second["status"], "ok")
        self.assertTrue(second["noop"], second["counts"])
        for name, layer in second["layers"].items():
            if name == "verify":
                continue
            for value in (layer["added"] or {}).values():
                self.assertEqual(value, 0, f"{name} moved {value}")

    def test_identical_staged_content_is_not_rewritten(self):
        fixture = BaselineFixture(self.tmp.name)
        fixture.rebuild(run_id="first").run(["graph"])
        graph = fixture.graph_dir / "ge16-knowledge-graph.json"
        before_stat = graph.stat()
        second = fixture.rebuild(run_id="second").run(["graph"])
        artifact = second["layers"]["graph"]["artifacts"][0]
        self.assertFalse(artifact["swapped"])
        self.assertTrue(artifact["identical_to_live"])
        self.assertEqual(graph.stat().st_mtime, before_stat.st_mtime)


class DryRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_dry_run_swaps_nothing_and_predicts_the_merge(self):
        fixture = BaselineFixture(self.tmp.name)
        watched = {
            "accepted": fixture.trackers / "ge16-news-accepted.json",
            "feed": fixture.trackers / "ge16-news-feed.json",
            "events": fixture.events_dir / "ge16-events.db",
            "graph": fixture.graph_dir / "ge16-knowledge-graph.json",
            "seats": fixture.figures / "ge16-seats-vectors.npy",
        }
        before = {name: sha(path) for name, path in watched.items()}
        manifest = fixture.rebuild(dry_run=True).run(["news", "figures", "events", "graph", "vdbs"])
        self.assertTrue(manifest["dry_run"])
        for name, path in watched.items():
            self.assertEqual(sha(path), before[name], f"{name} changed during a dry run")
        prediction = manifest["layers"]["news"]["dry_run_prediction"]
        self.assertEqual(prediction["added"],
                         prediction["backfill_added"] + prediction["tracker_added"])
        self.assertGreater(prediction["added"], 0)
        self.assertFalse(any(artifact.get("swapped")
                             for artifact in manifest["artifacts"]))
        self.assertTrue(all(call["args"].count("--commit") == 0 for call in fixture.stub.calls
                            if call["module"].endswith(".py")), "no commit may run in dry-run")


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_manifest_is_written_and_pointer_rotates(self):
        fixture = BaselineFixture(self.tmp.name)
        for index in range(10):
            fixture.rebuild(run_id=f"run-{index:02d}").run(["graph"])
        manifests = sorted((fixture.analytics / "work" / "baseline").glob("rebuild-manifest-*.json"))
        self.assertEqual(len(manifests), rb.KEEP_MANIFESTS)
        latest = json.loads((fixture.analytics / "work" / "baseline" / "latest.json").read_text())
        self.assertEqual(latest["manifest"], manifests[-1].name)
        self.assertEqual(latest["status"], "ok")

    def test_manifest_carries_layer_sequence_and_artifact_hashes(self):
        fixture = BaselineFixture(self.tmp.name)
        manifest = fixture.rebuild().run(["news", "events", "graph"])
        payload = json.loads(Path(manifest["manifest_path"]).read_text())
        self.assertEqual(payload["order"], ["news", "events", "graph"])
        for name in ("news", "events", "graph"):
            layer = payload["layers"][name]
            self.assertIn("duration_seconds", layer)
            self.assertIn("sha256", json.dumps(layer["artifacts"]))
        self.assertEqual(payload["layers"]["events"]["artifacts"][0]["swapped"], True)

    def test_status_line_is_machine_readable_and_cli_exits_zero(self):
        fixture = BaselineFixture(self.tmp.name)
        rebuild = fixture.rebuild()
        manifest = rebuild.run(["graph"])
        line = rebuild.status_line(manifest)
        self.assertTrue(line.startswith("REBUILD_STATUS "))
        payload = json.loads(line[len("REBUILD_STATUS "):])
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["layers"]["graph"]["status"], "ok")

        recorded = {}
        original = rb.Rebuild

        def factory(**kwargs):
            kwargs["command_runner"] = fixture.stub
            kwargs["log"] = lambda *parts: None
            recorded["rebuild"] = original(**kwargs)
            return recorded["rebuild"]

        rb.Rebuild = factory
        try:
            code = rb.main(["--all", "--graph", "--analytics-root", str(fixture.analytics),
                            "--data-root", str(fixture.data)])
        finally:
            rb.Rebuild = original
        self.assertEqual(code, 0)

    def test_cli_exit_code_is_one_on_failure(self):
        fixture = BaselineFixture(self.tmp.name, fail_events_floor=True)
        original = rb.Rebuild

        def factory(**kwargs):
            kwargs["command_runner"] = fixture.stub
            kwargs["log"] = lambda *parts: None
            return original(**kwargs)

        rb.Rebuild = factory
        try:
            code = rb.main(["--events", "--analytics-root", str(fixture.analytics),
                            "--data-root", str(fixture.data)])
        finally:
            rb.Rebuild = original
        self.assertEqual(code, 1)


class JudgeBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_file_backend_quarantines_stale_judged_evidence(self):
        fixture = BaselineFixture(self.tmp.name)
        # a judged batch file from an older collection with no source sha
        write_json(fixture.trackers / "ge16-news-judge-manifest.json",
                   {"collection_id": COLLECTION,
                    "batches": [{"batch": 1, "file": "ge16-news-judge-batch-1.json",
                                 "count": 1, "judged_file": "ge16-news-judged-batch-1.json"}]})
        write_json(fixture.trackers / "ge16-news-judge-batch-1.json",
                   {"batch": 1, "collection_id": COLLECTION, "count": 1,
                    "items": [{"title": "today"}]})
        write_json(fixture.trackers / "ge16-news-judged-batch-1.json",
                   {"batch": 1, "collection_id": "2026-09-23T00:00:00+00:00",
                    "accepted": [{"title": "yesterday"}]})
        rebuild = fixture.rebuild(judge_backend="file")
        quarantined = rebuild._quarantine_stale_judged("tracker", "news")
        self.assertEqual(len(quarantined), 1)
        self.assertFalse((fixture.trackers / "ge16-news-judged-batch-1.json").exists())
        self.assertTrue(Path(quarantined[0]["to"]).exists())

    def test_pending_batches_are_visible_and_dry_run_judges_but_never_commits(self):
        fixture = BaselineFixture(self.tmp.name)
        rebuild = fixture.rebuild(dry_run=True)
        fixture.stub._collect("tracker")
        fixture.stub._judge_input("tracker")
        pending = rebuild._pending_batches("tracker")
        self.assertEqual(pending["batches"], 1)
        self.assertEqual(pending["pending"], [1])
        result = rebuild._judge("tracker", "news")
        self.assertEqual(result["backend"], "deepseek")
        self.assertTrue(result["dry_run"])
        # the judgment ran (evidence written) but nothing was merged, and the evidence
        # went to the run stage — MED-1: a dry run must not write the live tracker dir
        self.assertTrue(rebuild._tracker_stage("ge16-news-judged.json").exists(),
                        "the dry-run judgment evidence is staged")
        self.assertFalse((fixture.trackers / "ge16-news-judged.json").exists(),
                         "a dry run must not write judgment evidence into the live tracker dir")
        accepted = json.loads((fixture.trackers / "ge16-news-accepted.json").read_text())
        self.assertEqual(accepted["count"], SEED_ACCEPTED)
        self.assertFalse(any("--commit" in call["args"] for call in fixture.stub.calls))


class StagedMergeAndHygieneTests(unittest.TestCase):
    """MED-1 / MED-2 / LOW fixes from the APPROVE review (26 Sep 2026).

    * MED-1 — ``--dry-run`` is side-effect-free: a composite dry run changes no byte
      outside work/baseline/** (the collectors' staging root).
    * MED-2 — the owner-record merge is staged, verified on the STAGED blob and only
      then swapped: a forced invariant failure leaves the live accepted history
      byte-identical and keeps the rejected blob for diagnosis.
    * LOW   — judge_model is never null, run ids never collide, and the DeepSeek
      credential reaches the judge subprocess only.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fixture = BaselineFixture(self.tmp.name)

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _tree_state(root) -> dict:
        """path -> (bytes, sha256) for every file under root (byte-level walk)."""
        state = {}
        for path in sorted(Path(root).rglob("*")):
            if path.is_file():
                state[str(path)] = (path.stat().st_size, sha(path))
        return state

    def _diff(self, before, after) -> list:
        changed = sorted(set(before) ^ set(after))
        changed += sorted(name for name in set(before) & set(after) if before[name] != after[name])
        return changed

    # -- MED-1: dry-run purity ------------------------------------------------
    def test_composite_dry_run_changes_no_live_bytes(self):
        fixture = self.fixture
        watched = {
            "1_DATA": fixture.data,
            "work/events": fixture.events_dir,
            "work/figures": fixture.figures,
            "work/graph": fixture.graph_dir,
            "work/scenarios": fixture.analytics / "work" / "scenarios",
        }
        before = {name: self._tree_state(root) for name, root in watched.items()}
        manifest = fixture.rebuild(dry_run=True).run(
            ["news", "polls", "figures", "events", "graph", "vdbs"])
        self.assertTrue(manifest["dry_run"])
        for name, root in watched.items():
            self.assertEqual([], self._diff(before[name], self._tree_state(root)),
                             f"{name} changed during a composite dry run")
        # every collector write landed under work/baseline/stage/<run_id>/trackers/
        baseline = fixture.analytics / "work" / "baseline"
        staged = [str(path) for path in baseline.rglob("*") if path.is_file()]
        self.assertTrue(any("trackers" in path for path in staged),
                        "the collector's dry-run writes are staged")
        self.assertTrue(all(not artifact.get("swapped")
                            for artifact in manifest["artifacts"]),
                        "a dry run swaps nothing")

    def test_dry_run_does_not_write_the_live_poll_db(self):
        fixture = self.fixture
        db = fixture.trackers / "ge16-polls-tracked.json"
        log = fixture.trackers / "ge16-poll-tracker-log.md"
        before = (sha(db), sha(log))
        manifest = fixture.rebuild(dry_run=True).run(["polls"])
        # the reviewer's symptom: the polls layer wrote the LIVE db under --dry-run
        self.assertEqual((sha(db), sha(log)), before,
                         "MED-1: the live polls DB/log changed under --dry-run")
        self.assertEqual(manifest["layers"]["polls"]["added"]["seen"], 2,
                         "the dry run still measures the staged merge")
        self.assertTrue((fixture.analytics / "work" / "baseline").exists())
        staged = list((fixture.analytics / "work" / "baseline").rglob(
            "ge16-polls-tracked.json"))
        self.assertEqual(len(staged), 1, "the poll DB is written into the run stage")

    def test_dry_run_quarantines_nothing(self):
        fixture = self.fixture
        write_json(fixture.trackers / "ge16-news-judge-manifest.json",
                   {"collection_id": COLLECTION,
                    "batches": [{"batch": 1, "file": "ge16-news-judge-batch-1.json",
                                 "count": 1, "judged_file": "ge16-news-judged-batch-1.json"}]})
        write_json(fixture.trackers / "ge16-news-judge-batch-1.json",
                   {"batch": 1, "collection_id": COLLECTION, "count": 1,
                    "items": [{"title": "today"}]})
        stale = fixture.trackers / "ge16-news-judged-batch-1.json"
        write_json(stale, {"batch": 1, "collection_id": "2026-09-23T00:00:00+00:00",
                           "accepted": [{"title": "yesterday"}]})
        before = sha(stale)
        quarantined = fixture.rebuild(judge_backend="file", dry_run=True) \
            ._quarantine_stale_judged("tracker", "news")
        self.assertEqual(len(quarantined), 1)
        self.assertTrue(quarantined[0]["dry_run"])
        self.assertEqual(quarantined[0]["would_move"], str(stale))
        self.assertTrue(stale.exists() and sha(stale) == before,
                        "MED-1: a dry run must not move live evidence")

    # -- MED-2: staged owner record ------------------------------------------
    def test_live_merge_is_verified_in_the_stage_before_the_swap(self):
        fixture = self.fixture
        manifest = fixture.rebuild().run(["news"])
        self.assertEqual(manifest["layers"]["news"]["status"], "ok")
        artifacts = manifest["layers"]["news"]["artifacts"]

        def by_name(name):
            return [record for record in artifacts if Path(record["path"]).name == name]

        accepted = [record for record in by_name("ge16-news-accepted.json")
                    if record.get("staged")]
        self.assertEqual(len(accepted), 2, "both staged merges swap the owner record")
        self.assertTrue(all(record["swapped"] for record in accepted))
        self.assertEqual({Path(record["live_path"]) for record in accepted},
                         {fixture.trackers / "ge16-news-accepted.json"})
        # os.replace consumed the staged blob: the stage no longer holds it
        self.assertFalse(fixture.rebuild()._tracker_stage("ge16-news-accepted.json").exists())
        merged = json.loads((fixture.trackers / "ge16-news-accepted.json").read_text())
        self.assertGreater(merged["count"], SEED_ACCEPTED)
        # the exchange set is explicit: the feed swapped, the untouched tracker state
        # files are recorded as such instead of faking a swap
        feed = [record for record in by_name("ge16-news-feed.json") if record.get("staged")]
        self.assertEqual(len(feed), 2, "both merges swap the rebuilt feed")
        self.assertTrue(all(record.get("swapped") for record in feed))
        tracked = by_name("ge16-general-news-tracked.json")
        self.assertEqual(len(tracked), 2)
        self.assertFalse(any(record.get("swapped") for record in tracked),
                         "an artifact the collector did not write is not faked into a swap")
        self.assertTrue(all(record.get("note") for record in tracked),
                        "a skipped artifact is explained in the record")

    def test_rejected_staged_merge_leaves_the_owner_record_byte_identical(self):
        fixture = BaselineFixture(self.tmp.name, drop_accepted=7)
        accepted = fixture.trackers / "ge16-news-accepted.json"
        before = sha(accepted)
        manifest = fixture.rebuild().run(["news"])
        layer = manifest["layers"]["news"]
        self.assertEqual(layer["status"], "failed")
        self.assertIn("news_merge_only", layer["error"])
        self.assertEqual(sha(accepted), before,
                         "the owner record must be byte-identical at rest")
        rollback = layer["rollback"]
        self.assertEqual(rollback["restored"], [])
        self.assertIn("never touched", rollback["note"])
        staged = Path(rollback["staged_owner_record"])
        self.assertTrue(staged.exists(), "the rejected blob stays in the stage")
        evidence = fixture.trackers / "ge16-news-backfill-judged-batch-1.json"
        self.assertTrue(evidence.exists(),
                        "judge evidence is consumed only after a VERIFIED merge")

    # -- LOW: manifest + credential hygiene ----------------------------------
    def test_manifest_always_records_a_resolved_judge_model(self):
        manifest = self.fixture.rebuild(judge_backend="file").run(["graph"])
        self.assertIsNotNone(manifest["judge_model"])
        self.assertEqual(manifest["judge_model"], rb.DEFAULT_JUDGE_MODEL)
        self.assertIn(manifest["judge_model_source"], ("default", "env", "cli"))

    def test_run_id_never_collides_inside_the_same_second(self):
        from datetime import datetime, timezone
        stamp = datetime(2026, 9, 26, 5, 23, 13, tzinfo=timezone.utc)
        ids = {rb.default_run_id(stamp) for _ in range(50)}
        self.assertEqual(len(ids), 50)
        self.assertTrue(all(value.startswith("2026-09-26T052313Z") for value in ids))
        self.assertNotEqual(self.fixture.rebuild().run_id, self.fixture.rebuild().run_id)

    def test_dry_run_stages_the_collector_env_end_to_end(self):
        """MED-1, pinned at the env level: a dry run stages the tracker dir AND the
        self-heal ledger for every collector it runs, so no collector pass can reach
        the live tracker directory — the same env the 1_DATA outdir suite exercises."""
        fixture = self.fixture
        fixture.rebuild(dry_run=True).run(["news"])
        collectors = [call for call in fixture.stub.calls
                      if call["module"] in ("ge16_news_backfill.py", "track_ge16_news.py")]
        self.assertTrue(collectors)
        self.assertTrue(all(call["tracker_out"] for call in collectors),
                        "every collector pass runs against the run stage")
        news = [call for call in collectors if call["module"] == "track_ge16_news.py"]
        self.assertTrue(news)
        self.assertTrue(all(call["has_selfheal_state"] for call in news),
                        "a staged news pass must not write the live self-heal ledger")

    def test_deepseek_credentials_reach_only_the_judge_subprocess(self):
        fixture = self.fixture
        rebuild = fixture.rebuild(
            base_env={"PATH": "/usr/bin:/bin", "DEEPSEEK_API_KEY": "stub-key",
                      "DEEPSEEK_MODEL": "stub-model"})
        manifest = rebuild.run(["news", "polls"])
        self.assertEqual(manifest["status"], "ok")
        judged = [call for call in fixture.stub.calls
                  if call["module"] == "ge16_judge_deepseek.py"]
        self.assertTrue(judged, "the judge ran")
        self.assertTrue(all(call["has_deepseek_key"] for call in judged))
        others = [call for call in fixture.stub.calls
                  if call["module"] != "ge16_judge_deepseek.py"]
        self.assertTrue(others)
        self.assertFalse(any(call["has_deepseek_key"] for call in others),
                         "the DeepSeek key must not reach any other layer subprocess")
        self.assertFalse(any(call["has_deepseek_model"] for call in others))
        self.assertTrue(manifest["credential_present"])


if __name__ == "__main__":
    unittest.main()


class StageTwoArgvParses(unittest.TestCase):
    """The cron's exact stage-2 argv must parse under the real CLI (reviewer BLOCK fix)."""
    def test_stage_two_argv_is_acceptable_to_parse_args(self):
        import tools.baseline.rebuild_baseline as rb
        argv = ["--events", "--graph", "--vdbs", "--figures"]
        parser = rb.build_arg_parser() if hasattr(rb, "build_arg_parser") else None
        if parser is None:
            import argparse, inspect
            # fall back: call parse_args via the module-level flow guard
            self.assertTrue(hasattr(rb, "parse_args"))
            args = rb.parse_args(argv)
            self.assertTrue(args.events and args.graph and args.vdbs and args.figures)
        else:
            args = parser.parse_args(argv)
            self.assertTrue(args.events and args.graph and args.vdbs and args.figures)
