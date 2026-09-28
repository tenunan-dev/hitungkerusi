#!/usr/bin/env python3
"""GE16 baseline rebuild — ONE program that rebuilds the whole baseline from sources.

Owner directive (26 Sep 2026): *"i want to redo database as baseline so that the
cron from now on will build up from the right data. create one program to do
that. use all sources."*

This is that program. It replays every baseline layer from its primary sources,
in dependency order, and only ever replaces a live artifact after the staged
replacement has passed its invariant checks (verify-then-swap):

  news     backfill collect (archive window) -> judge -> merge-only commit;
           then the weekly tracker pass (collect -> judge -> commit, which also
           rebuilds the page feed). ge16-news-accepted.json is merge-only: the
           pre-run item set is hashed and asserted to be a SUBSET after.
  polls    fresh pass of the poll tracker (Merdeka/Ilham/GNews), merged into its
           own seen-database per the existing contract.
  figures  every dated party/personnel update packet in work/figures/_draft is
           applied in chronological order and the parties / personnel / figures
           vector DBs are rebuilt (staged).
  events   work/events/ge16-events.db rebuilt from scratch from the sweep
           dossiers + draft layers into .staged-ge16-events.db; the story ledger
           is clustered in the same build.
  graph    work/graph/ge16-knowledge-graph.json rebuilt (staged).
  vdbs     the remaining vector DBs (seats / news / scenarios / clusters) staged,
           then the 2-D vector map bundle is regenerated from the swapped VDBs.

Every layer writes a rebuild manifest entry (counts before/after, added, artifact
sha256s, judge backend/model, duration) into
``work/baseline/rebuild-manifest-<UTC>.json``; the last 8 manifests are rotated
and ``work/baseline/latest.json`` points at the newest. Manifests are append-only.

Fail-closed: an invariant regression aborts the layer, leaves the prior live
artifact byte-identical and records the failure. By default (strict) the whole
run stops there; ``--continue-on-error`` keeps going. A news-layer regression
also restores the snapshot of the owner record, feed, seen state and tombstone,
so a bad collector run cannot cost or corrupt a single accepted item. Nothing is
ever committed to git by this program and it never touches OPS cron, run_stage.py
or 3_OUTPUTS / 4_DELIVERY / 5_WEBSITES.

Judge backend: ``--judge-backend deepseek`` performs the news judgment itself by
invoking 1_DATA's ge16_judge_deepseek.py, keeping the collectors' file/batch
contract byte-compatible so ``--commit`` keeps working. ``file`` leaves the
judgment to an external agent and refuses to merge stale evidence.

Usage:
  ./.venv/bin/python -m tools.baseline.rebuild_baseline --all --dry-run
  ./.venv/bin/python -m tools.baseline.rebuild_baseline --all \\
      --judge-backend deepseek --env-file ~/.hermes/profiles/coding/.env
  # deeper one-off baseline: widen the collector's caps and keep sweeping until a
  # sweep adds nothing (the no-burn overflow is re-presented on every collect)
  ./.venv/bin/python -m tools.baseline.rebuild_baseline --all --news-depth deep \\
      --news-sweeps 3 --judge-backend deepseek --env-file <dotenv>
  ./.venv/bin/python -m tools.baseline.rebuild_baseline --figures --events --graph --vdbs

Cron-friendly: one command, deterministic output paths, and the last stdout line is
``REBUILD_STATUS {json}`` with the status, run id, manifest path and per-layer deltas.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

MANIFEST_SCHEMA = "ge16.baseline-rebuild-manifest.v1"
STATUS_SCHEMA = "ge16.baseline-rebuild-status.v1"
MANIFEST_PREFIX = "rebuild-manifest-"
KEEP_MANIFESTS = 8

#: dependency-driven layer order (news -> polls -> figures -> events -> graph -> vdbs)
DEFAULT_ORDER = ("news", "polls", "figures", "events", "graph", "vdbs")

#: vector databases that must exist with meta.count == npy rows after a rebuild
VDB_NAMES = ("parties", "personnel", "figures", "seats", "news", "scenarios", "clusters")
#: the three VDBs owned by the figures layer (seed + dated update packets)
FIGURE_LAYER_VDBS = ("parties", "personnel", "figures")
#: the four VDBs owned by the vdbs layer
REMAINING_VDBS = ("seats", "news", "scenarios", "clusters")

#: hard floors from the pre-rebuild baseline (a rebuild may only grow them)
FLOOR_EVENTS = 512
FLOOR_ENTITIES = 2141
FLOOR_SOURCES = 315
FLOOR_STORIES = 117
FLOOR_NEWS_ACCEPTED = 1095
#: the accepted history must keep covering at least this span
NEWS_SPAN_START = "2026-01-02"
NEWS_SPAN_END = "2026-09-23"

JUDGE_BACKENDS = ("deepseek", "file")

#: env keys that may travel to a judge subprocess (values are never printed)
CREDENTIAL_KEYS = ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL")

#: judge model recorded when neither --judge-model nor DEEPSEEK_MODEL resolves
DEFAULT_JUDGE_MODEL = "deepseek-chat"

#: owner-state files the collectors commit into (staged, verified, then swapped)
NEWS_EXCHANGE_SET = ("ge16-news-accepted.json", "ge16-news-feed.json",
                     "ge16-general-news-log.md", "ge16-general-news-tracked.json",
                     "ge16-news-seen-pending.json")


def default_run_id(now=None) -> str:
    """Timestamp run id + a short uuid suffix.

    Second-resolution ids collide when two runs start inside the same second, which
    would make them share a manifest name and a stage directory (LOW finding, 26 Sep
    review).
    """
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


class InvariantError(RuntimeError):
    """A staged artifact did not meet its baseline invariant: abort the layer."""


class LayerFailure(RuntimeError):
    """A layer command failed; the layer is recorded failed and nothing swapped."""


# --------------------------------------------------------------------- helpers --
def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def atomic_write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def file_artifact(path, staged=False, swapped=False, identical=None, note=None) -> dict:
    path = Path(path)
    exists = path.exists()
    record = {
        "path": str(path),
        "staged": bool(staged),
        "swapped": bool(swapped),
        "exists": exists,
    }
    if exists:
        record["sha256"] = sha256_file(path)
        record["bytes"] = path.stat().st_size
    if identical is not None:
        record["identical_to_live"] = bool(identical)
    if note:
        record["note"] = note
    return record


def env_file_values(path) -> dict:
    """Credential values from a dotenv file — only the keys the judge needs.

    Values are returned to be placed in a child process environment; they are
    never logged by this program.
    """
    values = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        key = key.strip()
        if key not in CREDENTIAL_KEYS:
            continue
        raw = raw.strip().strip('"').strip("'")
        if raw:
            values[key] = raw
    return values


# -------------------------------------------------------------------- counting --
def accept_signature(item) -> str:
    """Merge-only signature of one accepted news item (link + title prefix)."""
    link = (item.get("link") or "").strip().lower()
    title = (item.get("title") or "").strip()[:150].lower()
    return sha256_bytes(f"{link}\x1f{title}".encode("utf-8"))


def news_counts(path) -> dict:
    if not path.exists():
        return {"count": 0, "span_start": None, "span_end": None, "signatures": [],
                "set_sha256": sha256_bytes(b""), "by_month": {}}
    payload = load_json(path, {}) or {}
    items = payload.get("items") if isinstance(payload, dict) else payload
    items = items if isinstance(items, list) else []
    dates = sorted(str(entry.get("date") or "")[:10] for entry in items if entry.get("date"))
    signatures = sorted(accept_signature(entry) for entry in items)
    by_month = {}
    for date in dates:
        by_month[date[:7]] = by_month.get(date[:7], 0) + 1
    return {
        "count": len(items),
        "envelope_keys": sorted(payload.keys()) if isinstance(payload, dict) else [],
        "envelope_generated_at": payload.get("generated_at") if isinstance(payload, dict) else None,
        "envelope_count": payload.get("count") if isinstance(payload, dict) else None,
        "span_start": dates[0] if dates else None,
        "span_end": dates[-1] if dates else None,
        "signatures": signatures,
        "set_sha256": sha256_bytes("\n".join(signatures).encode("utf-8")),
        "by_month": dict(sorted(by_month.items())),
    }


def sqlite_counts(path) -> dict:
    if not path.exists():
        return {}
    counts = {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        for table in ("events", "entities", "sources", "stories", "story_events",
                      "claim_reviews", "event_links", "dossier_notes"):
            try:
                counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                counts[table] = None
        try:
            counts["built_at"] = dict(conn.execute("SELECT key, value FROM schema_meta")
                                      .fetchall()).get("built_at")
        except sqlite3.Error:
            counts["built_at"] = None
    finally:
        conn.close()
    return counts


def graph_counts(path) -> dict:
    if not path.exists():
        return {}
    payload = load_json(path, {}) or {}
    nodes = payload.get("nodes")
    edges = payload.get("edges")
    return {"nodes": len(nodes) if isinstance(nodes, dict) else len(nodes or []),
            "edges": len(edges or []),
            "node_count_field": payload.get("node_count"),
            "edge_count_field": payload.get("edge_count"),
            "generated_at": payload.get("generated_at")}


def vdb_counts(figures_dir, name) -> dict:
    meta_path = Path(figures_dir) / f"ge16-{name}-meta.json"
    vec_path = Path(figures_dir) / f"ge16-{name}-vectors.npy"
    meta = load_json(meta_path, None)
    record = {"meta": str(meta_path), "vectors": str(vec_path),
              "meta_count": meta.get("count") if isinstance(meta, dict) else None,
              "items": len(meta.get("items") or []) if isinstance(meta, dict) else None,
              "dim": meta.get("dim") if isinstance(meta, dict) else None,
              "rows": None, "shape": None}
    if vec_path.exists():
        try:
            import numpy as np  # local import: only needed when a VDB is present
            array = np.load(vec_path)
            record["rows"] = int(array.shape[0])
            record["shape"] = list(array.shape)
        except Exception as error:  # pragma: no cover - numpy missing/corrupt file
            record["read_error"] = f"{type(error).__name__}: {error}"
    return record


def all_vdb_counts(figures_dir) -> dict:
    return {name: vdb_counts(figures_dir, name) for name in VDB_NAMES}


def phase_counts(path) -> dict:
    if not path.exists():
        return {}
    payload = load_json(path, {}) or {}
    entries = payload.get("batches") or []
    return {"batches": len(entries),
            "total": payload.get("total"),
            "collection_id": payload.get("collection_id"),
            "files": [entry.get("file") for entry in entries if isinstance(entry, dict)]}


# ------------------------------------------------------------------------ core --
class Rebuild:
    """The baseline rebuild orchestrator (append-only manifests, verify-then-swap)."""

    ORDER = DEFAULT_ORDER

    def __init__(self, *, analytics_root=None, data_root=None, run_id=None, dry_run=False,
                 strict=True, judge_backend="deepseek", judge_model=None, stage_root=None,
                 manifest_dir=None, keep_manifests=KEEP_MANIFESTS, command_runner=None,
                 base_env=None, python=None, log=print, window_start=None, window_end=None,
                 env_file=None, today=None, news_sweeps=1, news_depth="standard"):
        self.analytics_root = Path(analytics_root or Path(__file__).resolve().parents[2])
        self.data_root = Path(data_root or self.analytics_root.parent / "1_DATA")
        self.run_id = run_id or default_run_id()
        self.dry_run = bool(dry_run)
        self.strict = bool(strict)
        if judge_backend not in JUDGE_BACKENDS:
            raise ValueError(f"unknown judge backend {judge_backend!r}")
        self.judge_backend = judge_backend
        self.judge_model = judge_model
        self.log = log
        self.window_start = window_start or "2026-01-01"
        self._today = today
        self.window_end = window_end or self._today_string()
        self.keep_manifests = int(keep_manifests)
        self.news_sweeps = max(1, int(news_sweeps))
        self.news_depth = news_depth
        self.python = python or sys.executable
        raw_env = dict(base_env if base_env is not None else os.environ)
        if env_file:
            for key, value in env_file_values(env_file).items():
                raw_env.setdefault(key, value)
        # LOW finding (26 Sep review): the DeepSeek credential/config keys were
        # injected into EVERY layer subprocess. They are held here and injected only
        # into the judge command that actually needs them.
        self.credentials = {key: raw_env[key] for key in CREDENTIAL_KEYS if raw_env.get(key)}
        self.base_env = {key: value for key, value in raw_env.items()
                         if key not in CREDENTIAL_KEYS}

        self.work = self.analytics_root / "work"
        self.trackers = self.data_root / "research" / "trackers"
        self.figures = self.work / "figures"
        self.drafts = self.figures / "_draft"
        self.events_dir = self.work / "events"
        self.graph_dir = self.work / "graph"
        self.scenarios_dir = self.work / "scenarios"
        self.baseline_dir = self.work / "baseline"
        self.stage_dir = Path(stage_root) if stage_root else self.baseline_dir / "stage" / self.run_id
        self.manifest_dir = Path(manifest_dir) if manifest_dir else self.baseline_dir
        self.events_db = self.events_dir / "ge16-events.db"
        # MED-1: the events database is staged beside the live file (a live run keeps
        # the packet-named work/events/.staged-ge16-events.db); a dry run stages it
        # inside the run stage so nothing outside work/baseline/** is written.
        self.events_staged = (self.stage_dir / "events" / ".staged-ge16-events.db"
                              if self.dry_run
                              else self.events_dir / ".staged-ge16-events.db")
        self.graph_json = self.graph_dir / "ge16-knowledge-graph.json"
        self.accepted = self.trackers / "ge16-news-accepted.json"
        self.feed = self.trackers / "ge16-news-feed.json"
        self.polls_db = self.trackers / "ge16-polls-tracked.json"
        self.candidates = self.trackers / "ge16-news-candidates.json"
        self.judged = self.trackers / "ge16-news-judged.json"
        self.collect_dir = self.data_root / "scripts" / "collect"
        self.judge_script = self.collect_dir / "ge16_judge_deepseek.py"
        self.tracker_script = self.collect_dir / "track_ge16_news.py"
        self.backfill_script = self.collect_dir / "ge16_news_backfill.py"
        self.polls_script = self.collect_dir / "track_ge16_polls.py"

        self._runner = command_runner or self._subprocess_runner
        self.checks = []
        self.checks_by_layer = {}
        self.commands_by_layer = {}
        self.rollbacks = {}
        self.sweep_reports = []

    # -- plumbing -------------------------------------------------------------
    def _today_string(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    @staticmethod
    def _subprocess_runner(argv, env, cwd):
        proc = subprocess.run(argv, env=env, cwd=str(cwd), capture_output=True, text=True)
        return proc.returncode, proc.stdout or "", proc.stderr or ""

    def check(self, name, ok, detail="", layer=None):
        record = {"check": name, "ok": bool(ok), "detail": str(detail), "layer": layer}
        self.checks.append(record)
        if layer:
            self.checks_by_layer.setdefault(layer, []).append(record)
        return bool(ok)

    def require(self, name, ok, detail="", layer=None):
        self.check(name, ok, detail, layer)
        if not ok:
            raise InvariantError(f"{name}: {detail}")

    def child_env(self, credentials=False, **overrides) -> dict:
        """The environment for one child process.

        ``credentials=True`` adds the DeepSeek keys — used by the judge command only.
        """
        env = dict(self.base_env)
        if credentials:
            env.update(self.credentials)
        env.update({key: str(value) for key, value in overrides.items() if value is not None})
        return env

    def resolved_judge_model(self) -> str:
        """The judge model this run will use — never None in a manifest (LOW fix)."""
        return (self.judge_model or self.credentials.get("DEEPSEEK_MODEL")
                or DEFAULT_JUDGE_MODEL)

    def judge_model_source(self) -> str:
        """Where the resolved judge model came from (cli | env | default)."""
        if self.judge_model:
            return "cli"
        if self.credentials.get("DEEPSEEK_MODEL"):
            return "env"
        return "default"

    def run_cmd(self, argv, layer, env_overrides=None, check=True, credentials=False):
        """Run one pipeline command through the (injectable) runner."""
        env = self.child_env(credentials=credentials, **(env_overrides or {}))
        started = time.time()
        rc, out, err = self._runner([str(part) for part in argv], env, self.analytics_root)
        record = {
            "argv": [str(part) for part in argv],
            "rc": rc,
            "seconds": round(time.time() - started, 2),
            "stdout_tail": (out or "").strip().splitlines()[-20:],
            "stderr_tail": (err or "").strip().splitlines()[-6:],
        }
        self.commands_by_layer.setdefault(layer, []).append(record)
        if check and rc != 0:
            raise LayerFailure(
                f"{Path(str(argv[1] if len(argv) > 1 else argv[0])).name} exited {rc}: "
                f"{(err or out or '').strip().splitlines()[-1:] or ['no output']}")
        return record

    def _run_python(self, script_or_module, args, layer, module=False, env_overrides=None,
                    check=True, credentials=False):
        argv = ([self.python, "-m", script_or_module] if module
                else [self.python, str(script_or_module)])
        return self.run_cmd(argv + [str(arg) for arg in args], layer,
                            env_overrides=env_overrides, check=check, credentials=credentials)

    def _json_run(self, script, args, layer, module=False, env_overrides=None,
                  credentials=False):
        """Run a command expected to print its JSON report on the last line."""
        record = self._run_python(script, args, layer, module=module,
                                  env_overrides=env_overrides, credentials=credentials)
        for line in reversed(record["stdout_tail"]):
            line = line.strip()
            if line.startswith("{"):
                try:
                    record["report"] = json.loads(line)
                    break
                except ValueError:
                    continue
        return record

    def _quarantine_move(self, path, target, quarantined, reason=None) -> None:
        """Move evidence aside — unless this is a dry run, which only reports.

        MED-1: a dry run must not rewrite live tracker files; quarantining is a live
        mutation, so it is reported (``would_move``) and skipped.
        """
        record = {"to": str(target)}
        if reason:
            record["reason"] = reason
        if self.dry_run:
            record.update({"would_move": str(path), "dry_run": True,
                           "note": "dry-run: live evidence is not touched"})
            quarantined.append(record)
            return
        shutil.move(str(path), str(target))
        record["moved"] = str(path)
        quarantined.append(record)

    def _quarantine_stale_judged(self, pipeline, layer):
        """Move judged evidence that does not belong to the CURRENT judge batches.

        Only used with ``--judge-backend file``: the deepseek backend rewrites the
        judged side itself, so this exists so a hand-run judge can never merge a
        previous collection's ruling onto today's candidates (the collectors read
        any judged batch file listed in their manifest).
        """
        contracts = {
            "backfill": ("ge16-news-backfill-judge-manifest.json",
                         "ge16-news-backfill-judge-batch-%d.json",
                         "ge16-news-backfill-judged-batch-%d.json"),
            "tracker": ("ge16-news-judge-manifest.json",
                        "ge16-news-judge-batch-%d.json",
                        "ge16-news-judged-batch-%d.json"),
        }
        manifest_name, batch_fmt, judged_fmt = contracts[pipeline]
        manifest = load_json(self._tracker_read(manifest_name), None)
        quarantined = []
        if isinstance(manifest, dict):
            collection = manifest.get("collection_id") or ""
            for entry in manifest.get("batches") or []:
                if not isinstance(entry, dict) or not isinstance(entry.get("batch"), int):
                    continue
                batch_path = self._tracker_read(batch_fmt % entry["batch"])
                judged_path = self._tracker_read(judged_fmt % entry["batch"])
                if not judged_path.exists() or not batch_path.exists():
                    continue
                judged = load_json(judged_path, None)
                recorded = judged.get("source_sha256") if isinstance(judged, dict) else None
                same_collection = (isinstance(judged, dict)
                                   and judged.get("collection_id") == collection)
                if recorded == sha256_file(batch_path) or same_collection:
                    continue
                target = judged_path.with_name(
                    judged_path.name + f".stale-{self.run_id}")
                self._quarantine_move(judged_path, target, quarantined)
        if quarantined:
            self.log(f"  [{layer}] quarantined {len(quarantined)} stale judged file(s) "
                     "that do not match the current judge batches")
        if pipeline == "tracker" and self.judged.exists():
            # the consolidated judged payload takes precedence over the batches, so
            # it must belong to the CURRENT collection or it would mask today's work
            candidates = load_json(self.candidates, {}) or {}
            collection = candidates.get("generated_at") or ""
            judged = load_json(self.judged, None)
            stamp = (judged.get("generated_at") if isinstance(judged, dict) else "") or ""
            if isinstance(judged, dict) and stamp and collection and stamp < collection:
                target = self.judged.with_name(self.judged.name + f".stale-{self.run_id}")
                self._quarantine_move(
                    self.judged, target, quarantined,
                    reason="consolidated judged payload predates the current candidate "
                           "collection")
                self.log(f"  [{layer}] quarantined a stale consolidated judged payload "
                         f"({stamp} < {collection})")
        return quarantined

    def _swap(self, staged, layer, label=None, note=None) -> dict:
        """Verify-then-swap: replace the live artifact only after it was staged.

        Returns the artifact record. Identical content is left in place (no
        pointless rewrite) and reported as such.
        """
        staged = Path(staged)
        live = Path(self._live_for(staged))
        artifact = file_artifact(staged, staged=True, swapped=False, note=note)
        artifact["live_path"] = str(live)
        if not staged.exists():
            raise InvariantError(f"staged artifact missing: {staged} "
                                 f"(layer {layer})")
        if live.exists() and sha256_file(live) == artifact["sha256"]:
            artifact["identical_to_live"] = True
            artifact["note"] = "staged content identical to live; live left untouched"
            return artifact
        artifact["identical_to_live"] = False
        if self.dry_run:
            artifact["note"] = "dry-run: swap suppressed"
            return artifact
        live.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, live)
        artifact["swapped"] = True
        if label:
            artifact["label"] = label
        return artifact

    def _live_for(self, staged) -> Path:
        """Map a staged path back to the live artifact it replaces."""
        staged = Path(staged)
        # run-stage paths first: a dry run stages even the events database inside the
        # stage (work/baseline/**), a live run stages it beside the live file
        # (packet-named work/events/.staged-ge16-events.db)
        stage = self.stage_dir.resolve()
        try:
            relative = staged.resolve().relative_to(stage)
        except ValueError:
            relative = None
        if relative is not None and relative.parts:
            if relative.parts[0] == "figures":
                return self.figures / Path(*relative.parts[1:])
            if relative.parts[0] == "graph":
                return self.graph_dir / Path(*relative.parts[1:])
            if relative.parts[0] == "events":
                return self.events_dir / "ge16-events.db"
            if relative.parts[0] == "trackers":
                return self.trackers / Path(*relative.parts[1:])
            return self.figures / Path(*relative.parts[1:])
        if staged.name.startswith(".staged-"):
            return staged.with_name(staged.name[len(".staged-"):])
        return staged

    @staticmethod
    def _delta(before, after) -> dict:
        delta = {}
        for key in sorted(set(before) | set(after)):
            if isinstance(after.get(key), int) and isinstance(before.get(key), int):
                delta[key] = after[key] - before[key]
        return delta

    def _layer_result(self, layer, started, before, after, *, added=None, artifacts=None,
                      judge=None, status="ok", error=None, notes=None, extra=None):
        result = {
            "layer": layer,
            "status": status,
            "counts_before": before,
            "counts_after": after,
            "delta": self._delta(before, after),
            "added": added if added is not None else self._delta(before, after),
            "artifacts": artifacts or [],
            "commands": self.commands_by_layer.get(layer, []),
            "judge": judge or {"backend": self.judge_backend,
                               "model": self.resolved_judge_model()},
            "checks": self.checks_by_layer.get(layer, []),
            "duration_seconds": round(time.time() - started, 2),
            "error": error,
            "notes": notes or [],
        }
        if extra:
            result.update(extra)
        return result

    def _stage_dir(self, *parts) -> Path:
        path = self.stage_dir.joinpath(*parts)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _tracker_stage_dir(self) -> Path:
        """Where the collectors' staged tracker writes land (inside the run stage)."""
        return self._stage_dir("trackers")

    def _tracker_stage(self, name) -> Path:
        return self._tracker_stage_dir() / name

    def _tracker_read(self, name) -> Path:
        """This run's staged tracker file when it exists, else the live one."""
        staged = self._tracker_stage(name)
        return staged if staged.exists() else self.trackers / name

    def _tracker_env(self) -> dict:
        """Route the collectors' tracker writes into the run stage.

        Always used for the merge (``--commit``), which writes the candidate blob to
        the stage so this orchestrator can verify it before the swap (MED-2). A dry
        run also routes the collect/judge cycle and the self-heal state, so it writes
        nothing live at all (MED-1).
        """
        env = {"GE16_TRACKER_OUT_DIR": str(self._tracker_stage_dir())}
        if self.dry_run:
            env["GE16_SELFHEAL_STATE"] = str(self._tracker_stage("ge16-selfheal-state.json"))
        return env

    def _collector_env(self) -> dict:
        """Env for the collect / --judge-input steps.

        Staged in a dry run only: a live run keeps its candidate and judge evidence in
        the live tracker directory exactly as before.
        """
        return self._tracker_env() if self.dry_run else {}

    def _seed_staged_selfheal(self) -> None:
        """Seed the staged self-heal state from the live one.

        A dry run records its outcomes there instead of live; seeding keeps the
        SELFHEAL banner faithful to the real outstanding work. Never swapped.
        """
        live = self.trackers / "ge16-selfheal-state.json"
        staged = self._tracker_stage("ge16-selfheal-state.json")
        if live.exists() and not staged.exists():
            try:
                shutil.copy2(live, staged)
            except OSError:  # pragma: no cover - staging must never break a dry run
                pass

    def _swap_optional(self, staged, layer, label=None, note=None) -> dict:
        """Swap an artifact the collector may not have written in this pass.

        A collector only touches the feed/log/seen/tombstone when it actually merged
        something, so a missing staged copy is recorded as a fact, not a failure.
        """
        staged = Path(staged)
        if not staged.exists():
            live = self._live_for(staged)
            return file_artifact(live, note=note or "the collector did not touch this artifact")
        return self._swap(staged, layer, label=label, note=note)

    def _staged_figures_dir(self) -> Path:
        """Stage root for figure artifacts, with the embedding cache linked in.

        The builders keep their cache under their output root (``.model_cache``);
        staging that root must not trigger a 240 MB model re-download, so the
        live cache is symlinked into the stage.
        """
        stage = self._stage_dir("figures")
        link = stage / ".model_cache"
        if not link.exists() and not link.is_symlink():
            try:
                link.symlink_to(self.figures / ".model_cache", target_is_directory=True)
            except OSError:  # fall back to the live cache path via env
                pass
        return stage

    def _backfill_env(self) -> dict:
        """The collector's own env knobs: a deeper baseline sweep, never forked logic."""
        env = {"GE16_BACKFILL_START": self.window_start,
               "GE16_BACKFILL_END": self.window_end}
        if self.news_depth == "deep":
            env.update({"GE16_BACKFILL_SOURCE_CAP": "120",
                        "GE16_BACKFILL_TOTAL_CAP": "1200",
                        "GE16_BACKFILL_OVERFLOW_STORE": "4000"})
        return env

    def _require_builder_ok(self, record, name, layer) -> None:
        """The update wrappers print REBUILD FAILED but still exit 0.

        Never trust that: a swallowed builder failure must fail the layer loudly
        instead of leaving the staged (or live) VDB half-written.
        """
        text = "\n".join(record.get("stdout_tail") or []) + "\n" + \
            "\n".join(record.get("stderr_tail") or [])
        self.require("figures_%s_builder_ok" % name, "REBUILD FAILED" not in text,
                     "the %s builder reported REBUILD FAILED" % name, layer)
        self.require("figures_%s_packets_valid" % name, "INVALID FILES" not in text,
                     "the %s update packets failed validation" % name, layer)

    def _snapshot(self, paths) -> dict:
        """Byte snapshot of the state the collectors may touch (rollback safety)."""
        snapshot = {}
        for entry in paths:
            entry = Path(entry)
            snapshot[str(entry)] = entry.read_bytes() if entry.exists() else None
        return snapshot

    def _restore(self, snapshot) -> list:
        """Put the snapshotted bytes back; returns the restored paths."""
        restored = []
        for name, payload in snapshot.items():
            path = Path(name)
            if payload is None:
                if path.exists():
                    path.unlink()
                    restored.append(name)
                continue
            if not path.exists() or path.read_bytes() != payload:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
                restored.append(name)
        return restored

    #: judge evidence files per pipeline (consumed after a verified merge)
    JUDGE_EVIDENCE = {
        "backfill": ("ge16-news-backfill-judge-manifest.json",
                     "ge16-news-backfill-judge-batch-*.json",
                     "ge16-news-backfill-judged-batch-*.json"),
        "tracker": ("ge16-news-judge-manifest.json",
                    "ge16-news-judge-batch-*.json",
                    "ge16-news-judged-batch-*.json"),
    }

    def _consume_judged_evidence(self, pipeline, layer) -> list:
        """Delete the live judge evidence a VERIFIED merge consumed.

        The collectors clear their own (staged) evidence; with a staged merge the live
        evidence is consumed here — after the merged owner record is durable, never
        before the invariant check (MED-2).
        """
        removed = []
        for pattern in self.JUDGE_EVIDENCE.get(pipeline, ()):
            for path in sorted(self.trackers.glob(pattern)):
                try:
                    path.unlink()
                    removed.append(path.name)
                except OSError:  # pragma: no cover - cleanup is never fatal
                    continue
        if pipeline == "tracker" and self.judged.exists():
            try:
                self.judged.unlink()
                removed.append(self.judged.name)
            except OSError:  # pragma: no cover - cleanup is never fatal
                pass
        if removed:
            self.log(f"  [{layer}] consumed {len(removed)} judge evidence file(s) after the "
                     "verified merge")
        return removed

    def _news_commit(self, script, pipeline, layer, prior_signatures) -> list:
        """Merge into the STAGE, verify the staged owner record, then swap (MED-2).

        The collector's ``--commit`` writes its candidate merged blob into
        ``work/baseline/stage/<run_id>/trackers/``; the merge-only / floor / span /
        envelope invariants run against THAT blob and only a passing blob is moved
        over the live files with ``os.replace``. A failing invariant (or a kill) leaves
        the live accepted history byte-identical, so the owner record is never
        observable in a merged-but-unverified state.
        """
        self._run_python(script, ["--commit"], layer, env_overrides=self._tracker_env())
        staged_accepted = self._tracker_stage("ge16-news-accepted.json")
        if not staged_accepted.exists():
            note = ("the collector committed nothing this pass (no judged evidence or no "
                    "new items); the live owner record was not touched")
            self.check("news_commit_staged_no_op", True, note, layer)
            return []
        staged = news_counts(staged_accepted)
        signatures = set(staged.pop("signatures", []))
        try:
            self.require("news_merge_only", prior_signatures <= signatures,
                         f"{len(prior_signatures - signatures)} accepted item(s) would have "
                         f"disappeared (staged {staged_accepted.name})", layer)
            self.require("news_floor", staged["count"] >= FLOOR_NEWS_ACCEPTED,
                         f"staged accepted={staged['count']} floor={FLOOR_NEWS_ACCEPTED}", layer)
            self.require("news_span_start", (staged["span_start"] or "") <= NEWS_SPAN_START,
                         f"staged span starts {staged['span_start']} "
                         f"(must cover {NEWS_SPAN_START})", layer)
            self.require("news_span_end", (staged["span_end"] or "") >= NEWS_SPAN_END,
                         f"staged span ends {staged['span_end']} "
                         f"(must cover {NEWS_SPAN_END})", layer)
            self.require("news_envelope",
                         set(staged["envelope_keys"]) >= {"generated_at", "count", "items"},
                         f"staged envelope keys {staged['envelope_keys']}", layer)
            self.require("news_envelope_count", staged["envelope_count"] == staged["count"],
                         f"staged envelope count {staged['envelope_count']} != "
                         f"items {staged['count']}", layer)
        except (InvariantError, LayerFailure) as error:
            self.rollbacks[layer] = {
                "restored": [],
                "staged_owner_record": str(staged_accepted),
                "error": f"{type(error).__name__}: {error}",
                "note": ("no revert needed: the merge was staged and the live owner record "
                         "was never touched (MED-2 staged-verify-then-swap)"),
            }
            self.log(f"[{layer}] STAGED MERGE REJECTED ({error}); the live owner record is "
                     "byte-identical and the rejected blob stays in the stage")
            raise
        artifacts = [self._swap(staged_accepted, layer, label="news-accepted",
                                note="merge-only baseline history (owner record)")]
        for name in NEWS_EXCHANGE_SET:
            if name == "ge16-news-accepted.json":
                continue
            artifacts.append(self._swap_optional(self._tracker_stage(name), layer,
                                                 label=f"news:{name}"))
        self.rollbacks.pop(layer, None)
        if not self.dry_run:
            self._consume_judged_evidence(pipeline, layer)
        return artifacts

    # -- layer: news ----------------------------------------------------------
    def _layer_news(self) -> dict:
        layer, started = "news", time.time()
        before = {**news_counts(self.accepted), "feed": (load_json(self.feed, {}) or {}).get("count")}
        prior_signatures = set(before.pop("signatures", []))
        notes, judge_stats = [], {}
        self.log(f"[news] accepted={before['count']} span {before['span_start']}.."
                 f"{before['span_end']} feed={before.get('feed')}")
        # MED-1/MED-2 (26 Sep review): every collector write is routed into the run
        # stage (GE16_TRACKER_OUT_DIR) and the merge is verified on the STAGED owner
        # record before it is swapped in — the live accepted history is never observable
        # half-merged, and a dry run changes nothing outside work/baseline/**.
        if self.dry_run:
            self._seed_staged_selfheal()
        swap_artifacts = []

        # 1) archive backfill over the whole window, merge-only. The sweep repeats
        #    because a collect re-derives its candidate slice from the same sources
        #    every time (the no-burn overflow beyond the caps is re-presented), so a
        #    deeper baseline needs more than one pass.
        sweeps = 1 if self.dry_run else self.news_sweeps
        for sweep in range(1, sweeps + 1):
            sweep_before = news_counts(self.accepted)["count"]
            self._run_python(self.backfill_script, ["--collect"], layer,
                             env_overrides={**self._backfill_env(),
                                            **self._collector_env()})
            self._run_python(self.backfill_script, ["--judge-input"], layer,
                             env_overrides=self._collector_env())
            sweep_stats = self._judge("backfill", layer)
            if judge_stats.get("backfill") is None:
                judge_stats["backfill"] = sweep_stats
            if self.dry_run:
                notes.append("dry-run: --commit suppressed (the owner record is untouched)")
            else:
                swap_artifacts.extend(
                    self._news_commit(self.backfill_script, "backfill", layer,
                                      prior_signatures))
            sweep_after = news_counts(self.accepted)["count"]
            record = {"sweep": sweep, "depth": self.news_depth,
                      "accepted_before": sweep_before, "accepted_after": sweep_after,
                      "added": sweep_after - sweep_before,
                      "judge": {key: sweep_stats.get(key) for key in ("calls", "judged")}}
            self.sweep_reports.append(record)
            notes.append("backfill sweep %d/%d (%s): +%d accepted"
                         % (sweep, sweeps, self.news_depth, record["added"]))
            if self.dry_run or record["added"] == 0:
                break

        # 2) weekly tracker pass (its commit rebuilds the page feed)
        self._run_python(self.tracker_script, [], layer, env_overrides=self._collector_env())
        self._run_python(self.tracker_script, ["--judge-input"], layer,
                         env_overrides=self._collector_env())
        judge_stats["tracker"] = self._judge("tracker", layer)
        if self.dry_run:
            prediction = self._predict_news_merge()
            notes.append(f"dry-run prediction: would merge {prediction['added']} new "
                         f"accepted item(s) ({prediction['backfill_added']} from the "
                         f"backfill, {prediction['tracker_added']} from the tracker); "
                         "feed rebuild suppressed")
            after_full = news_counts(self.accepted)
            after_signatures = set(after_full.pop("signatures", []))
            after = {**after_full, "feed": before.get("feed")}
            after["count"] = before["count"] + prediction["added"]
            after["envelope_count"] = after["count"]
            after["by_month"] = before.get("by_month")
            artifacts = [file_artifact(self.accepted,
                                       note="dry-run: live owner record untouched")]
            return self._layer_result(
                layer, started, before, after,
                added={"accepted": prediction["added"], "feed": 0},
                artifacts=artifacts, judge=judge_stats,
                notes=notes, extra={"dry_run_prediction": prediction,
                                    "prior_set_sha256": before["set_sha256"],
                                    "after_set_sha256": after["set_sha256"]})
        swap_artifacts.extend(self._news_commit(self.tracker_script, "tracker", layer,
                                                prior_signatures))

        after_full = news_counts(self.accepted)
        after_signatures = set(after_full.pop("signatures", []))
        after = {**after_full, "feed": (load_json(self.feed, {}) or {}).get("count")}

        # the staged blob was already verified inside _news_commit (merge-only, floor,
        # span, envelope) and only a passing blob was swapped in; these checks re-read
        # the LIVE post-swap state so the manifest reports real counts
        self.require("news_merge_only", prior_signatures <= after_signatures,
                     f"{len(prior_signatures - after_signatures)} accepted item(s) are "
                     "missing from the live owner record", layer)
        self.require("news_floor", after["count"] >= FLOOR_NEWS_ACCEPTED,
                     f"accepted={after['count']} floor={FLOOR_NEWS_ACCEPTED}", layer)
        self.require("news_envelope_count", after["envelope_count"] == after["count"],
                     f"envelope count {after['envelope_count']} != items {after['count']}",
                     layer)
        # the feed is a rebuilt rolling view (7-day window, per-source cap), so a
        # shrink is a normal roll-forward — recorded, never fatal
        self.check("news_feed_rebuilt", (after.get("feed") or 0) >= (before.get("feed") or 0),
                   f"feed {before.get('feed')} -> {after.get('feed')} "
                   "(informational: the feed is a rolling window, not a baseline)", layer)
        if after["count"] > before["count"]:
            notes.append(f"+{after['count'] - before['count']} accepted items "
                         f"(prior set sha {before['set_sha256'][:12]})")

        artifacts = [
            file_artifact(self.accepted, note="merge-only baseline history (owner record)"),
            file_artifact(self.feed),
            file_artifact(self.trackers / "ge16-general-news-log.md"),
        ] + swap_artifacts
        added = {"accepted": after["count"] - before["count"],
                 "feed": (after.get("feed") or 0) - (before.get("feed") or 0)}
        return self._layer_result(layer, started, before, after, added=added,
                                  artifacts=artifacts, judge=judge_stats,
                                  notes=notes,
                                  extra={"prior_set_sha256": before["set_sha256"],
                                         "after_set_sha256": after["set_sha256"]})

    def _judged_items(self, pipeline) -> list:
        """The judged items a collector's --commit would merge (read-only)."""
        contracts = {"backfill": ("ge16-news-backfill-judge-manifest.json",
                                  "ge16-news-backfill-judged-batch-%d.json"),
                     "tracker": ("ge16-news-judge-manifest.json",
                                 "ge16-news-judged-batch-%d.json")}
        manifest_name, judged_fmt = contracts[pipeline]
        manifest = load_json(self._tracker_read(manifest_name), None)
        items = []
        judged_path = self._tracker_read("ge16-news-judged.json")
        if pipeline == "tracker" and judged_path.exists():
            payload = load_json(judged_path, None)
            if isinstance(payload, dict) and isinstance(payload.get("accepted"), list):
                return payload["accepted"]
        for entry in (manifest or {}).get("batches") or []:
            if not isinstance(entry, dict) or not isinstance(entry.get("batch"), int):
                continue
            payload = load_json(self._tracker_read(judged_fmt % entry["batch"]), None)
            if isinstance(payload, dict) and isinstance(payload.get("accepted"), list):
                items.extend(payload["accepted"])
        return items

    def _predict_news_merge(self) -> dict:
        """What the two merge-only commits would add, without touching the owner
        record (dry-run evidence). Mirrors the collectors' dedupe rules:
        backfill keys on (link, title[:150]), the tracker on title[:150]."""
        current = load_json(self.accepted, {}) or {}
        existing = current.get("items") if isinstance(current, dict) else []
        existing = existing if isinstance(existing, list) else []
        titles = {(entry.get("title") or "").strip()[:150] for entry in existing}
        links = {(entry.get("link") or "").strip().lower() for entry in existing}
        backfill_added = tracker_added = 0
        for item in self._judged_items("backfill"):
            link = (item.get("link") or "").strip().lower()
            title = (item.get("title") or "").strip()[:150]
            if not title or link in links or title in titles:
                continue
            links.add(link)
            titles.add(title)
            backfill_added += 1
        for item in self._judged_items("tracker"):
            title = (item.get("title") or "").strip()[:150]
            if not title or title in titles:
                continue
            titles.add(title)
            tracker_added += 1
        return {"backfill_added": backfill_added, "tracker_added": tracker_added,
                "added": backfill_added + tracker_added, "prior_count": len(existing)}

    def _judge(self, pipeline, layer) -> dict:
        """Judgment step for one news pipeline (self-healing: only stale/missing
        batches are (re)judged; already-current batches are reported).

        In a dry run the judgment still runs — it writes judge evidence (batches,
        judged files), never the owner record, and it is what makes the dry-run's
        would-be merge prediction real.
        """
        if self.judge_backend == "file":
            quarantined = self._quarantine_stale_judged(pipeline, layer)
            return {"backend": "file", "model": None, "quarantined": quarantined,
                    "note": "judgment left to an external agent"}
        stats_path = self._stage_dir("judge") / f"judge-stats-{pipeline}.json"
        # a dry run judges its OWN staged batches (nothing live is read or written);
        # a live run judges in place — only its merge is staged (MED-1/MED-2)
        tracker_dir = self._tracker_stage_dir() if self.dry_run else self.trackers
        args = ["judge-batches", "--pipeline", pipeline,
                "--tracker-dir", str(tracker_dir), "--stats", str(stats_path)]
        if self.judge_model:
            args += ["--model", self.judge_model]
        record = self._json_run(self.judge_script, args, layer, credentials=True)
        stats = load_json(stats_path, {}) or {}
        report = (record.get("report") or {}).get("stats", {})
        stats = stats or report
        result = {"backend": "deepseek", "model": stats.get("model") or self.judge_model,
                  "calls": stats.get("calls"), "retries": stats.get("retries"),
                  "failures": stats.get("failures"), "batches": stats.get("batches"),
                  "judged": stats.get("judged"), "already_judged": stats.get("already_judged"),
                  "consolidated": stats.get("consolidated")}
        if self.dry_run:
            result["dry_run"] = True
            result["note"] = "dry-run: judgment performed, commit suppressed"
        return result

    def _pending_batches(self, pipeline) -> dict:
        contracts = {"tracker": ("ge16-news-judge-manifest.json",
                                 "ge16-news-judge-batch-%d.json",
                                 "ge16-news-judged-batch-%d.json"),
                     "backfill": ("ge16-news-backfill-judge-manifest.json",
                                  "ge16-news-backfill-judge-batch-%d.json",
                                  "ge16-news-backfill-judged-batch-%d.json")}
        manifest_name, batch_fmt, judged_fmt = contracts[pipeline]
        manifest = load_json(self._tracker_read(manifest_name), None)
        pending = []
        if isinstance(manifest, dict):
            for entry in manifest.get("batches") or []:
                if not isinstance(entry, dict) or not isinstance(entry.get("batch"), int):
                    continue
                number = entry["batch"]
                batch_path = self._tracker_read(batch_fmt % number)
                judged_path = self._tracker_read(judged_fmt % number)
                current = False
                if batch_path.exists() and judged_path.exists():
                    judged = load_json(judged_path, None)
                    current = (isinstance(judged, dict)
                               and judged.get("source_sha256") == sha256_file(batch_path))
                if not current:
                    pending.append(number)
        return {"batches": len((manifest or {}).get("batches") or []), "pending": pending}

    # -- layer: polls ---------------------------------------------------------
    def _layer_polls(self) -> dict:
        layer, started = "polls", time.time()
        # MED-1: the collector's DB + log writes are routed into the run stage (always,
        # so the merge is checked on the STAGED blob before the swap; in a dry run
        # nothing live is written at all). The live poll DB used to be written by the
        # collector directly under --dry-run (reviewer MED-1: seen 233 -> 234).
        polls_log = self.trackers / "ge16-poll-tracker-log.md"
        before = {"seen": len((load_json(self.polls_db, {}) or {}).get("seen") or []),
                  "log_bytes": polls_log.stat().st_size if polls_log.exists() else 0}
        record = self._run_python(self.polls_script, [], layer,
                                  env_overrides=self._tracker_env())
        new_items = 0
        for line in record["stdout_tail"]:
            match = re.search(r"NEW GE16 POLL ITEMS FOUND \((\d+)\)", line)
            if match:
                new_items = int(match.group(1))
                break
        staged_db = self._tracker_stage("ge16-polls-tracked.json")
        staged_log = self._tracker_stage("ge16-poll-tracker-log.md")
        # the staged blob is what the merge check and the diff must read
        counts_db = staged_db if staged_db.exists() else self.polls_db
        counts_log = staged_log if staged_log.exists() else polls_log
        after = {"seen": len((load_json(counts_db, {}) or {}).get("seen") or []),
                 "log_bytes": counts_log.stat().st_size if counts_log.exists() else 0}
        try:
            self.require("polls_merge_only", after["seen"] >= before["seen"],
                         f"seen {before['seen']} -> {after['seen']}",
                         layer)
        except InvariantError as error:
            self.rollbacks[layer] = {
                "restored": [],
                "staged_artifact": str(staged_db),
                "error": f"{type(error).__name__}: {error}",
                "note": ("no revert needed: the staged poll DB was never swapped in "
                         "(the live DB is byte-identical)"),
            }
            raise
        artifacts = [self._swap_optional(staged_db, layer, label="polls-db",
                                        note="poll history (owner record)"),
                     self._swap_optional(staged_log, layer, label="polls-log")]
        return self._layer_result(layer, started, before, after,
                                  added={"seen": after["seen"] - before["seen"],
                                         "found": new_items},
                                  artifacts=artifacts,
                                  notes=[f"{new_items} new poll item(s) in this pass"])

    # -- layer: figures -------------------------------------------------------
    def _draft_packets(self) -> dict:
        party = sorted(self.drafts.glob("party-updates-*.json"))
        personnel = sorted(self.drafts.glob("personnel-updates-*.json"))
        polls = sorted(self.drafts.glob("polls-update-*.json"))
        return {
            "party_updates": [{"file": path.name, "records": len(
                (load_json(path, {}) or {}).get("parties") or [])} for path in party],
            "personnel_updates": [{"file": path.name, "records": len(
                (load_json(path, {}) or {}).get("persons") or [])} for path in personnel],
            "polls_updates": [path.name for path in polls],
            "party_records": sum(len((load_json(path, {}) or {}).get("parties") or [])
                                 for path in party),
            "personnel_records": sum(len((load_json(path, {}) or {}).get("persons") or [])
                                     for path in personnel),
        }

    def _layer_figures(self) -> dict:
        layer, started = "figures", time.time()
        before = {name: vdb_counts(self.figures, name) for name in FIGURE_LAYER_VDBS}
        before_flat = {f"{name}_{key}": value.get(key)
                       for name, value in before.items() for key in ("meta_count", "rows")}
        packets = self._draft_packets()
        notes = [f"{len(packets['party_updates'])} party packet(s), "
                 f"{len(packets['personnel_updates'])} personnel packet(s) applied in "
                 "chronological order (seed -> 2026-09-24)"]
        stage = self._staged_figures_dir()
        env = {"GE16_FIGURES_OUT_DIR": str(stage)}

        # personnel FIRST: build_parties_vdb reads <out>/ge16-personnel.json
        # (compute_from_personnel) as an input, so in a staged output root the
        # personnel artifacts must already be there.
        personnel_record = self._run_python("tools.figures.update_personnel_from_cron", [],
                                           layer, module=True, env_overrides=env)
        self._require_builder_ok(personnel_record, "personnel", layer)
        parties_record = self._run_python("tools.figures.update_parties_from_cron", [],
                                         layer, module=True, env_overrides=env)
        self._require_builder_ok(parties_record, "parties", layer)
        figures_record = self._run_python("tools.figures.build_figures_vdb", [], layer,
                                         module=True, env_overrides=env)
        self._require_builder_ok(figures_record, "figures", layer)

        self.require("figures_packets_present",
                     bool(packets["party_updates"]) and bool(packets["personnel_updates"]),
                     "no dated update packets found in _draft", layer)
        artifacts, after_flat = [], {}
        for name in FIGURE_LAYER_VDBS:
            staged = {"meta_count": None, "rows": None}
            for suffix in (f"ge16-{name}-meta.json", f"ge16-{name}-vectors.npy"):
                staged_path = stage / suffix
                self.require(f"vdb_{name}_staged_{suffix.split('.')[-1]}",
                             staged_path.exists(), f"missing staged {staged_path}", layer)
            counts = vdb_counts(stage, name)
            staged["meta_count"], staged["rows"] = counts["meta_count"], counts["rows"]
            self.require(f"vdb_{name}_meta_rows", counts["meta_count"] == counts["rows"],
                         f"meta count {counts['meta_count']} != npy rows {counts['rows']}",
                         layer)
            self.require(f"vdb_{name}_grew",
                         (counts["rows"] or 0) >= (before[name]["rows"] or 0),
                         f"rows {before[name]['rows']} -> {counts['rows']}", layer)
            for suffix in (f"ge16-{name}-meta.json", f"ge16-{name}-vectors.npy"):
                artifacts.append(self._swap(stage / suffix, layer, label=f"vdb:{name}"))
            source_json = {"parties": "ge16-parties.json", "personnel": "ge16-personnel.json",
                           "figures": "ge16-key-figures.json"}[name]
            if (stage / source_json).exists():
                artifacts.append(self._swap(stage / source_json, layer, label=f"vdb-source:{name}"))
            after_flat[f"{name}_meta_count"] = counts["meta_count"]
            after_flat[f"{name}_rows"] = counts["rows"]
        return self._layer_result(layer, started, before_flat, after_flat,
                                  added=self._delta(before_flat, after_flat),
                                  artifacts=artifacts, notes=notes,
                                  extra={"packets": packets})

    # -- layer: events --------------------------------------------------------
    def _layer_events(self) -> dict:
        layer, started = "events", time.time()
        before = sqlite_counts(self.events_db)
        self.log(f"[events] before: events={before.get('events')} entities={before.get('entities')} "
                 f"sources={before.get('sources')} stories={before.get('stories')}")

        if self.events_staged.exists():
            self.events_staged.unlink()
        # inherit published story ids: the ledger seeds ids from the prior db
        if self.events_db.exists():
            self.events_staged.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.events_db, self.events_staged)
        report = self._json_run("tools.events.build_events_db", [
            "--out", str(self.events_staged),
            "--dossier-dir", str(self.events_dir / "_draft"),
            "--draft-dir", str(self.drafts),
            "--kg", str(self.graph_json),
            "--figures-dir", str(self.figures),
            "--data-root", str(self.data_root),
            "--quiet"], layer, module=True)

        staged = sqlite_counts(self.events_staged)
        self.require("events_floor", (staged.get("events") or 0) >= FLOOR_EVENTS,
                     f"events={staged.get('events')} floor={FLOOR_EVENTS}", layer)
        self.require("entities_floor", (staged.get("entities") or 0) >= FLOOR_ENTITIES,
                     f"entities={staged.get('entities')} floor={FLOOR_ENTITIES}", layer)
        self.require("sources_floor", (staged.get("sources") or 0) >= FLOOR_SOURCES,
                     f"sources={staged.get('sources')} floor={FLOOR_SOURCES}", layer)
        self.require("stories_floor", (staged.get("stories") or 0) >= FLOOR_STORIES,
                     f"stories={staged.get('stories')} floor={FLOOR_STORIES}", layer)
        self.require("events_no_regression",
                     all((staged.get(key) or 0) >= (before.get(key) or 0)
                         for key in ("events", "entities", "sources", "stories")),
                     f"before {before} -> staged {staged}", layer)
        artifact = self._swap(self.events_staged, layer, label="events-db")
        artifacts = [artifact, file_artifact(self.events_dir / "ge16-events-stats.json")]
        added = self._delta({key: before.get(key) for key in ("events", "entities", "sources", "stories")},
                            {key: staged.get(key) for key in ("events", "entities", "sources", "stories")})
        return self._layer_result(
            layer, started,
            {key: before.get(key) for key in ("events", "entities", "sources", "stories")},
            {key: staged.get(key) for key in ("events", "entities", "sources", "stories")},
            added=added, artifacts=artifacts,
            extra={"build_report": (report or {}).get("report", {}).get("counts", {}),
                   "staged_path": str(self.events_staged)})

    # -- layer: graph ---------------------------------------------------------
    def _layer_graph(self) -> dict:
        layer, started = "graph", time.time()
        before = graph_counts(self.graph_json)
        stage = self._stage_dir("graph")
        self._run_python("tools.graph.build_knowledge_graph", [], layer, module=True,
                         env_overrides={"GE16_GRAPH_OUT_DIR": str(stage)})
        staged_path = stage / "ge16-knowledge-graph.json"
        self.require("graph_staged", staged_path.exists(), f"missing {staged_path}", layer)
        staged = graph_counts(staged_path)
        self.require("graph_nodes_no_regression",
                     (staged.get("nodes") or 0) >= (before.get("nodes") or 0),
                     f"nodes {before.get('nodes')} -> {staged.get('nodes')}", layer)
        self.require("graph_edges_no_regression",
                     (staged.get("edges") or 0) >= (before.get("edges") or 0),
                     f"edges {before.get('edges')} -> {staged.get('edges')}", layer)
        self.require("graph_counts_consistent",
                     staged.get("node_count_field") == staged.get("nodes")
                     and staged.get("edge_count_field") == staged.get("edges"),
                     f"declared node/edge counts {staged.get('node_count_field')}/"
                     f"{staged.get('edge_count_field')} != actual "
                     f"{staged.get('nodes')}/{staged.get('edges')}", layer)
        artifact = self._swap(staged_path, layer, label="knowledge-graph")
        return self._layer_result(
            layer, started,
            {key: before.get(key) for key in ("nodes", "edges")},
            {key: staged.get(key) for key in ("nodes", "edges")},
            added=self._delta({key: before.get(key) for key in ("nodes", "edges")},
                              {key: staged.get(key) for key in ("nodes", "edges")}),
            artifacts=[artifact])

    # -- layer: vdbs ----------------------------------------------------------
    def _layer_vdbs(self) -> dict:
        layer, started = "vdbs", time.time()
        before = {f"{name}_rows": vdb_counts(self.figures, name)["rows"]
                  for name in REMAINING_VDBS}
        stage = self._staged_figures_dir()
        self._run_python("tools.figures.build_remaining_vdbs", [], layer, module=True,
                         env_overrides={"GE16_FIGURES_OUT_DIR": str(stage)})
        artifacts, after = [], {}
        for name in REMAINING_VDBS:
            counts = vdb_counts(stage, name)
            self.require(f"vdb_{name}_meta_rows", counts["meta_count"] == counts["rows"],
                         f"meta count {counts['meta_count']} != npy rows {counts['rows']}",
                         layer)
            self.require(f"vdb_{name}_present", (counts["rows"] or 0) > 0,
                         f"{name} has no rows", layer)
            if name != "news":
                self.require(f"vdb_{name}_grew",
                             (counts["rows"] or 0) >= (before[f"{name}_rows"] or 0),
                             f"rows {before[f'{name}_rows']} -> {counts['rows']}", layer)
            for suffix in (f"ge16-{name}-meta.json", f"ge16-{name}-vectors.npy"):
                artifacts.append(self._swap(stage / suffix, layer, label=f"vdb:{name}"))
            after[f"{name}_rows"] = counts["rows"]
        # the 2-D semantic map is derived from the swapped VDBs
        if not self.dry_run:
            self._run_python("tools.figures.build_vec_map", [], layer, module=True)
        artifacts.append(file_artifact(self.work / "graph-explorer" / "vec_map_data.js",
                                       note="derived 2-D vector map bundle"))
        return self._layer_result(layer, started, before, after,
                                  added=self._delta(before, after), artifacts=artifacts)

    # -- final verification ---------------------------------------------------
    def _verify_all(self) -> dict:
        layer, started = "verify", time.time()
        checks_before = len(self.checks)
        counts = all_vdb_counts(self.figures)
        flat = {}
        for name, record in counts.items():
            flat[f"{name}_meta_count"] = record["meta_count"]
            flat[f"{name}_rows"] = record["rows"]
            self.check(f"final_vdb_{name}_meta_rows", record["meta_count"] == record["rows"],
                       f"meta {record['meta_count']} rows {record['rows']}", layer)
            self.check(f"final_vdb_{name}_present", (record["rows"] or 0) > 0,
                       f"{name} rows={record['rows']}", layer)
        accepted = news_counts(self.accepted)
        accepted.pop("signatures", None)
        self.check("final_news_floor", accepted["count"] >= FLOOR_NEWS_ACCEPTED,
                   f"accepted={accepted['count']}", layer)
        self.check("final_news_span",
                   (accepted["span_start"] or "") <= NEWS_SPAN_START
                   and (accepted["span_end"] or "") >= NEWS_SPAN_END,
                   f"{accepted['span_start']}..{accepted['span_end']}", layer)
        events = sqlite_counts(self.events_db)
        self.check("final_events_floor", (events.get("events") or 0) >= FLOOR_EVENTS,
                   f"events={events.get('events')}", layer)
        self.check("final_entities_floor", (events.get("entities") or 0) >= FLOOR_ENTITIES,
                   f"entities={events.get('entities')}", layer)
        self.check("final_sources_floor", (events.get("sources") or 0) >= FLOOR_SOURCES,
                   f"sources={events.get('sources')}", layer)
        self.check("final_stories_floor", (events.get("stories") or 0) >= FLOOR_STORIES,
                   f"stories={events.get('stories')}", layer)
        graph = graph_counts(self.graph_json)
        self.check("final_graph_present", (graph.get("nodes") or 0) > 0 and (graph.get("edges") or 0) > 0,
                   f"nodes={graph.get('nodes')} edges={graph.get('edges')}", layer)
        failed = [record for record in self.checks[checks_before:] if not record["ok"]]
        return self._layer_result(
            layer, started, {}, flat, added={},
            artifacts=[file_artifact(self.accepted), file_artifact(self.events_db),
                       file_artifact(self.graph_json)],
            status="ok" if not failed else "failed",
            error=None if not failed else f"{len(failed)} final check(s) failed",
            extra={"vdb": counts, "accepted": accepted, "events": events, "graph": graph})

    # -- orchestration --------------------------------------------------------
    def layer_functions(self):
        return {
            "news": self._layer_news,
            "polls": self._layer_polls,
            "figures": self._layer_figures,
            "events": self._layer_events,
            "graph": self._layer_graph,
            "vdbs": self._layer_vdbs,
            "verify": self._verify_all,
        }

    def run(self, layers=None) -> dict:
        requested = tuple(layers or DEFAULT_ORDER)
        unknown = [name for name in requested if name not in DEFAULT_ORDER]
        if unknown:
            raise ValueError(f"unknown layer(s): {unknown}")
        order = tuple(name for name in DEFAULT_ORDER if name in requested)
        if requested != order:  # honour the dependency order, never the given one
            requested = order
        functions = self.layer_functions()
        generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        started = time.time()
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "run_id": self.run_id,
            "generated_at": generated_at,
            "dry_run": self.dry_run,
            "strict": self.strict,
            "judge_backend": self.judge_backend,
            # LOW fix (26 Sep review): never null in a manifest — the resolved config
            # value, refined below by the per-layer stats of a judge that actually ran
            "judge_model": self.resolved_judge_model(),
            "judge_model_source": self.judge_model_source(),
            "credential_present": bool(self.credentials.get("DEEPSEEK_API_KEY")) if
            self.judge_backend == "deepseek" else None,
            "order": list(requested),
            "layers": {},
            "notes": [
                "verify-then-swap: every staged artifact is validated before the live file "
                "is replaced; a failing invariant keeps the prior live artifact",
                "manifests are append-only; only the newest %d are kept" % self.keep_manifests,
            ],
        }
        status, aborted = "ok", None
        for name in requested:
            self.log(f"[{name}] running …")
            try:
                result = functions[name]()
            except (InvariantError, LayerFailure) as error:
                result = self._layer_result(name, started, {}, {}, status="failed",
                                            error=f"{type(error).__name__}: {error}",
                                            extra={"rollback": self.rollbacks.get(name)})
                self.log(f"[{name}] FAILED: {error}")
            except Exception as error:  # unexpected: still record honestly
                result = self._layer_result(name, started, {}, {}, status="failed",
                                            error=f"{type(error).__name__}: {error}",
                                            extra={"rollback": self.rollbacks.get(name)})
                self.log(f"[{name}] ERROR: {type(error).__name__}: {error}")
            manifest["layers"][name] = result
            self.log(f"[{name}] {result['status']} in {result['duration_seconds']}s "
                     f"delta={result.get('delta')}")
            if result["status"] != "ok":
                status = "failed"
                if self.strict:
                    aborted = name
                    break
        if not self.dry_run and aborted is None and status == "ok":
            verify = self._verify_all()
            manifest["layers"]["verify"] = verify
            if verify["status"] != "ok":
                status = "failed"
        manifest["status"] = status
        manifest["aborted_at"] = aborted
        manifest["duration_seconds"] = round(time.time() - started, 2)
        manifest["checks"] = self.checks
        manifest["checks_failed"] = [record for record in self.checks if not record["ok"]]
        manifest["news_sweeps"] = self.sweep_reports
        # the model actually used is known from the per-layer judge stats: it wins over
        # the resolved config value recorded above
        for layer_result in manifest["layers"].values():
            for stats in (layer_result.get("judge") or {}).values():
                if isinstance(stats, dict) and stats.get("model"):
                    manifest["judge_model"] = stats["model"]
                    manifest["judge_model_source"] = "judge-stats"
                    break
            else:
                continue
            break
        manifest["noop"] = self._is_noop(manifest)
        manifest["counts"] = self._count_table(manifest)
        manifest["artifacts"] = [artifact for layer in manifest["layers"].values()
                                 for artifact in layer.get("artifacts", [])]
        manifest["manifest_path"] = str(self._manifest_path())
        path = self._write_manifest(manifest)
        manifest["manifest_path"] = str(path)
        return manifest

    def _manifest_path(self) -> Path:
        stamp = self.run_id.replace(":", "")
        return self.manifest_dir / f"{MANIFEST_PREFIX}{stamp}.json"

    def _is_noop(self, manifest) -> bool:
        """A run is a no-op when no layer moved any count."""
        layers = {name: layer for name, layer in manifest["layers"].items()
                  if name != "verify" and layer["status"] == "ok"}
        if not layers:
            return False
        for layer in layers.values():
            if any(value for value in (layer.get("added") or {}).values()):
                return False
        return True

    def _count_table(self, manifest) -> dict:
        table = {}
        for name, layer in manifest["layers"].items():
            table[name] = {"before": layer.get("counts_before"), "after": layer.get("counts_after"),
                           "delta": layer.get("delta"), "added": layer.get("added")}
        return table

    def _write_manifest(self, manifest) -> Path:
        self.manifest_dir.mkdir(parents=True, exist_ok=True)
        path = self._manifest_path()
        payload = dict(manifest)
        payload["manifest_path"] = str(path)
        atomic_write_json(path, payload)
        atomic_write_json(self.manifest_dir / "latest.json", {
            "schema": MANIFEST_SCHEMA,
            "run_id": self.run_id,
            "generated_at": payload["generated_at"],
            "status": payload["status"],
            "noop": payload.get("noop"),
            "dry_run": payload.get("dry_run"),
            "manifest": path.name,
        })
        self._rotate_manifests()
        return path

    def _rotate_manifests(self):
        manifests = sorted(self.manifest_dir.glob(f"{MANIFEST_PREFIX}*.json"))
        for stale in manifests[:-self.keep_manifests] if self.keep_manifests > 0 else []:
            try:
                stale.unlink()
            except OSError:
                continue

    def status_line(self, manifest) -> str:
        payload = {
            "schema": STATUS_SCHEMA,
            "run_id": manifest["run_id"],
            "status": manifest["status"],
            "dry_run": manifest["dry_run"],
            "noop": manifest.get("noop"),
            "aborted_at": manifest.get("aborted_at"),
            "duration_seconds": manifest["duration_seconds"],
            "judge_backend": manifest["judge_backend"],
            "judge_model": manifest["judge_model"],
            "manifest": manifest.get("manifest_path"),
            "layers": {name: {"status": layer["status"], "added": layer.get("added")}
                       for name, layer in manifest["layers"].items()},
        }
        return "REBUILD_STATUS " + json.dumps(payload, ensure_ascii=False, sort_keys=True)


# ------------------------------------------------------------------------- cli --
def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Rebuild the GE16 baseline from sources (verify-then-swap).")
    parser.add_argument("--all", action="store_true", help="run every layer (default)")
    parser.add_argument("--news", action="store_true")
    parser.add_argument("--polls", action="store_true")
    parser.add_argument("--figures", action="store_true")
    parser.add_argument("--events", action="store_true")
    parser.add_argument("--graph", action="store_true")
    parser.add_argument("--vdbs", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="run everything except the final swap and print the diffs")
    parser.add_argument("--continue-on-error", action="store_true",
                        help="keep rebuilding the remaining layers after a failure")
    parser.add_argument("--judge-backend", choices=JUDGE_BACKENDS, default="deepseek")
    parser.add_argument("--judge-model", default=None,
                        help="judge model override (default: DEEPSEEK_MODEL/deepseek-chat)")
    parser.add_argument("--env-file", default=None,
                        help="dotenv file to read DEEPSEEK_* from (values never logged)")
    parser.add_argument("--analytics-root", default=None)
    parser.add_argument("--data-root", default=None)
    parser.add_argument("--stage-root", default=None)
    parser.add_argument("--manifest-dir", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--window-start", default=None, help="backfill window start date")
    parser.add_argument("--window-end", default=None, help="backfill window end date")
    parser.add_argument("--news-sweeps", type=int, default=1,
                        help="repeat the backfill collect->judge->commit cycle until a "
                             "sweep adds nothing (bounded; default 1)")
    parser.add_argument("--news-depth", choices=("standard", "deep"), default="standard",
                        help="deep widens the collector's own per-source/total caps")
    parser.add_argument("--keep-manifests", type=int, default=KEEP_MANIFESTS)
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    layers = [name for name in DEFAULT_ORDER
              if getattr(args, name) or (args.all and not any(
                  getattr(args, candidate) for candidate in DEFAULT_ORDER))]
    if not layers:
        layers = list(DEFAULT_ORDER)
    rebuild = Rebuild(
        analytics_root=args.analytics_root, data_root=args.data_root, run_id=args.run_id,
        dry_run=args.dry_run, strict=not args.continue_on_error,
        judge_backend=args.judge_backend, judge_model=args.judge_model,
        stage_root=args.stage_root, manifest_dir=args.manifest_dir,
        keep_manifests=args.keep_manifests, env_file=args.env_file,
        news_sweeps=args.news_sweeps, news_depth=args.news_depth,
        window_start=args.window_start, window_end=args.window_end,
        log=(lambda *parts: None) if args.quiet else print)
    try:
        manifest = rebuild.run(layers)
    except Exception as error:  # never leave the operator without a status line
        print(f"REBUILD_STATUS {json.dumps({'status': 'error', 'error': f'{type(error).__name__}: {error}'})}")
        return 2
    print(rebuild.status_line(manifest))
    if manifest["dry_run"]:
        print("\nDRY-RUN diff summary (live artifacts untouched):")
        for name, layer in manifest["layers"].items():
            print(f"  {name:8s} {layer['status']:6s} before={layer.get('counts_before')}")
            print(f"  {'':8s} {'':6s} after ={layer.get('counts_after')}")
    else:
        print(f"\nmanifest: {manifest['manifest_path']}")
    return 0 if manifest["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
