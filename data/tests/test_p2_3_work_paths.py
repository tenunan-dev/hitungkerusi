"""P2.3 run-scoped working paths: run identity, auto-wiring, promotion, guards.

Pinned here (packet deliverables 3.1-3.6):

  * run dir structure, run.json contents, run_id format;
  * auto-wiring — with GE16_RUN_DIR set, the REAL collector functions
    (ge16_tracker_outdir.out_dir, ge16_selfheal_state.state_path) resolve
    inside the run dir with zero collector changes;
  * unset env = byte-for-byte today's resolution (regression guard);
  * promotion lands staged files in canonical, writes an edition whose
    lineage carries run_id, and repeated promotion adds no duplicates;
  * guard: no module under scripts/collect/ writes into canonical while a
    run is active (canonical root patched to a tmp dir, real offline
    collector subprocess);
  * audit preservation: grep/AST-level no-deletion-under-canonical plus the
    behavioral double-promotion check.

Run from the repository root:
    python3 -m pytest data/tests/test_p2_3_work_paths.py -q
"""

import ast
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]   # data/
PROJECT_ROOT = REPOSITORY_ROOT.parent                  # V3 monorepo root
SCRIPTS_ROOT = REPOSITORY_ROOT / "scripts"
COLLECT_ROOT = SCRIPTS_ROOT / "collect"
CANONICAL_ROOT = REPOSITORY_ROOT / "canonical"
TRACKER_ROOT = CANONICAL_ROOT / "research" / "trackers"
SCHEMA_PATH = SCRIPTS_ROOT / "schemas" / "ge16_edition-promotion.schema.json"
FIXTURE_PATH = REPOSITORY_ROOT / "tests" / "fixtures" / "p2_3" / "edition-promotion.json"
RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load " + str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WP = load_module(SCRIPTS_ROOT / "work_paths.py", "work_paths_p23")
OUTDIR = load_module(COLLECT_ROOT / "ge16_tracker_outdir.py", "ge16_tracker_outdir_p23")
SELFHEAL = load_module(COLLECT_ROOT / "ge16_selfheal_state.py", "ge16_selfheal_state_p23")
REFRESH = load_module(SCRIPTS_ROOT / "refresh_canonical_data.py", "refresh_canonical_data_p23")
ALL_KNOBS = (WP.RUN_ENV, WP.TRACKER_OUT_ENV, WP.SELFHEAL_STATE_ENV, "HITUNGKERUSI_ROOT")


def snapshot(root: Path):
    """name -> sha256 for every file under root (recursive, deterministic)."""
    state = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            state[path.relative_to(root).as_posix()] = \
                hashlib.sha256(path.read_bytes()).hexdigest()
    return state


def seed_canonical(parent: Path):
    """A minimal canonical tree: trackers dir with one prior audit file."""
    canonical = parent / "canonical"
    trackers = canonical / "research" / "trackers"
    trackers.mkdir(parents=True, exist_ok=True)
    (trackers / "prior-audit-artifact.json").write_text('{"kept": true}\n', encoding="utf-8")
    (trackers / "ge16-news-candidates.json").write_text('{"items": []}\n', encoding="utf-8")
    return canonical


class EnvIsolation(unittest.TestCase):
    """Strip every root-override knob before each test; restore after."""

    def setUp(self):
        self._saved = {name: os.environ.get(name) for name in ALL_KNOBS}
        for name in ALL_KNOBS:
            os.environ.pop(name, None)

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class RunIdentityTests(EnvIsolation):
    def test_new_run_creates_structure_and_run_json(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            moment = datetime(2026, 9, 29, 7, 13, 42, tzinfo=timezone.utc)
            run = WP.new_run(label="p2.3 check", data_root=temp, now=moment, host="tester")
            self.assertEqual(run.root, Path(temp) / "work" / run.run_id)
            self.assertRegex(run.run_id, RUN_ID_RE.pattern)
            self.assertTrue(run.run_id.startswith("20260929T071342Z-"), run.run_id)
            for directory in run.subdirs():
                self.assertTrue(directory.is_dir(), directory)
            document = json.loads(run.run_json.read_text(encoding="utf-8"))
            self.assertEqual(
                {"run_id": run.run_id, "label": "p2.3 check",
                 "started_at": "2026-09-29T07:13:42+00:00", "host": "tester"},
                document)

    def test_run_ids_are_unique_within_the_same_second(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            moment = datetime(2026, 9, 29, 7, 13, 42, tzinfo=timezone.utc)
            first = WP.new_run(data_root=temp, now=moment)
            second = WP.new_run(data_root=temp, now=moment)
            self.assertNotEqual(first.run_id, second.run_id)
            self.assertTrue(first.root.is_dir() and second.root.is_dir())

    def test_new_run_touches_no_environment(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            WP.new_run(label="no env", data_root=temp)
            for name in ALL_KNOBS:
                self.assertNotIn(name, os.environ)

    def test_current_run_none_when_unset_and_resolved_when_set(self):
        self.assertIsNone(WP.current_run(env={}))
        self.assertIsNone(WP.current_run())
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            run = WP.new_run(label="resolve me", data_root=temp)
            resolved = WP.current_run(env={WP.RUN_ENV: str(run.root)})
            self.assertEqual((resolved.run_id, resolved.root), (run.run_id, run.root))
            self.assertEqual(resolved.trackers, run.trackers)
            self.assertEqual(resolved.selfheal_state, run.selfheal_state)
            # a hand-placed dir without run.json falls back to its name
            bare = Path(temp) / "work" / "20260929T070000Z-deadbeef"
            bare.mkdir(parents=True)
            self.assertEqual(WP.current_run(env={WP.RUN_ENV: str(bare)}).run_id,
                             "20260929T070000Z-deadbeef")


class AutoWiringTests(EnvIsolation):
    """apply_env must wire the EXISTING knobs; collectors stay untouched."""

    def test_apply_env_exports_collector_knobs_inside_the_run(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            run = WP.new_run(label="wiring", data_root=temp)
            env = {}
            active = WP.apply_env(run, env=env)
            self.assertEqual(active.run_id, run.run_id)
            self.assertEqual(env, {
                WP.RUN_ENV: str(run.root),
                WP.TRACKER_OUT_ENV: str(run.trackers),
                WP.SELFHEAL_STATE_ENV: str(run.selfheal_state),
            })
            self.assertTrue(env[WP.TRACKER_OUT_ENV].startswith(str(run.root) + os.sep))
            self.assertTrue(env[WP.SELFHEAL_STATE_ENV].startswith(str(run.root) + os.sep))

    def test_bare_apply_env_derives_knobs_from_ge16_run_dir(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            run = WP.new_run(label="bare", data_root=temp)
            env = {WP.RUN_ENV: str(run.root)}
            WP.apply_env(env=env)
            self.assertEqual(env[WP.TRACKER_OUT_ENV], str(run.trackers))
            self.assertEqual(env[WP.SELFHEAL_STATE_ENV], str(run.selfheal_state))

    def test_apply_env_without_a_run_is_a_noop(self):
        env = {"UNRELATED": "1"}
        self.assertIsNone(WP.apply_env(env=env))
        self.assertEqual(env, {"UNRELATED": "1"})

    def test_real_collector_functions_resolve_inside_the_run_dir(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            run = WP.new_run(label="real functions", data_root=temp)
            with mock.patch.dict(os.environ, {WP.RUN_ENV: str(run.root)}):
                WP.apply_env()  # orchestrator form: derive from GE16_RUN_DIR
                # the REAL functions from the collector modules, called as the
                # collectors call them (no reload needed: both read env live)
                self.assertEqual(OUTDIR.out_dir(), str(run.trackers))
                live = TRACKER_ROOT / "ge16-news-accepted.json"
                self.assertEqual(OUTDIR.w(live),
                                 str(run.trackers / "ge16-news-accepted.json"))
                self.assertEqual(OUTDIR.a(live),
                                 str(run.trackers / "ge16-news-accepted.json"))
                self.assertEqual(SELFHEAL.state_path(), str(run.selfheal_state))

    def test_unset_env_is_byte_for_byte_todays_resolution(self):
        # the staging knob: identity behavior pinned by its own suite; here we
        # pin it FROM the run layer: no run, no reroute, same strings as P2.2
        self.assertIsNone(OUTDIR.out_dir())
        self.assertIsNone(OUTDIR.staged("anything.json"))
        for live_name in ("ge16-news-accepted.json", "ge16-general-news-log.md"):
            live = TRACKER_ROOT / live_name
            self.assertEqual(OUTDIR.w(live), str(live))
            self.assertEqual(OUTDIR.a(live), str(live))
            self.assertEqual(OUTDIR.r(live), str(live))
        self.assertEqual(OUTDIR.g(str(TRACKER_ROOT / "ge16-news-accepted.json")),
                         [str(TRACKER_ROOT / "ge16-news-accepted.json")])
        # the self-heal knob: exactly the documented live state file
        self.assertEqual(
            SELFHEAL.state_path(),
            str(CANONICAL_ROOT / "research" / "trackers" / "ge16-selfheal-state.json"))
        # module-level constants agree with the live layout
        self.assertEqual(Path(OUTDIR.LIVE_DIR), TRACKER_ROOT)
        self.assertEqual(Path(SELFHEAL.STATE), TRACKER_ROOT / "ge16-selfheal-state.json")


class PromotionTests(EnvIsolation):
    def _seeded_run(self, parent: Path):
        canonical = seed_canonical(parent)
        run = WP.new_run(label="promotion", data_root=parent / "data")
        (run.trackers / "ge16-news-candidates.json").write_text('{"items": []}\n',
                                                                encoding="utf-8")
        (run.trackers / "ge16-news-judge-batch-1.json").write_text('{"batch": 1}\n',
                                                                   encoding="utf-8")
        return canonical, run

    def test_promotion_copies_staged_files_and_writes_edition_with_run_lineage(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            parent = Path(temp)
            canonical, run = self._seeded_run(parent)
            staged_digest = hashlib.sha256(
                (run.trackers / "ge16-news-judge-batch-1.json").read_bytes()).hexdigest()
            candidates_mtime = (canonical / "research/trackers/ge16-news-candidates.json") \
                .stat().st_mtime_ns

            promoted, unchanged = REFRESH.promote(run, canonical_root=canonical)

            self.assertEqual(["research/trackers/ge16-news-judge-batch-1.json"],
                             [entry["file"] for entry in promoted])
            self.assertEqual(promoted[0]["sha256"], staged_digest)
            self.assertEqual(promoted[0]["disposition"], "staged-tracker-output")
            # the byte-identical staged candidates file was skipped, not rewritten
            self.assertEqual(["research/trackers/ge16-news-candidates.json"],
                             [entry["file"] for entry in unchanged])
            self.assertEqual(
                (canonical / "research/trackers/ge16-news-candidates.json")
                .stat().st_mtime_ns, candidates_mtime)
            # the staged bytes landed in canonical
            self.assertEqual(
                (canonical / "research/trackers/ge16-news-judge-batch-1.json")
                .read_bytes(),
                (run.trackers / "ge16-news-judge-batch-1.json").read_bytes())

            edition_id, edition_path = REFRESH.write_promotion_edition(
                run, promoted, unchanged, canonical_root=canonical)
            manifest = json.loads(Path(edition_path).read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema"], "ge16.edition.promotion.v1")
            self.assertEqual(manifest["edition_id"], edition_id)
            self.assertEqual(manifest["lineage"]["run_id"], run.run_id)
            self.assertIsNone(manifest["lineage"]["prior_edition"])
            self.assertEqual(manifest["lineage"]["inputs"], promoted)
            self.assertEqual(manifest["content_hashes"],
                             {"research/trackers/ge16-news-judge-batch-1.json":
                              staged_digest})
            self.assertEqual(manifest["row_counts"],
                             {"promoted_files": 1, "unchanged_files": 1})
            self.assertEqual(Path(edition_path).parent, canonical / "editions")
            # the promotion edition validates against its schema
            import jsonschema
            jsonschema.validate(manifest, json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))

    def test_repeat_promotion_adds_no_duplicates_and_deletes_nothing(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            parent = Path(temp)
            canonical, run = self._seeded_run(parent)
            REFRESH.promote(run, canonical_root=canonical)
            first = snapshot(canonical)
            self.assertIn("research/trackers/prior-audit-artifact.json", first)

            promoted, unchanged = REFRESH.promote(run, canonical_root=canonical)

            self.assertEqual(promoted, [])
            self.assertEqual(sorted(entry["file"] for entry in unchanged),
                             ["research/trackers/ge16-news-candidates.json",
                              "research/trackers/ge16-news-judge-batch-1.json"])
            self.assertEqual(snapshot(canonical), first)  # names AND hashes stable
            self.assertEqual(
                sorted(path.name for path in (canonical / "research/trackers").iterdir()),
                ["ge16-news-candidates.json", "ge16-news-judge-batch-1.json",
                 "prior-audit-artifact.json"])  # no "copy" duplicates, audit file kept

    def test_two_identical_runs_promote_identical_file_sets(self):
        manifests = []
        for _ in range(2):
            with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
                parent = Path(temp)
                canonical, run = self._seeded_run(parent)
                promoted, unchanged = REFRESH.promote(run, canonical_root=canonical)
                _, edition_path = REFRESH.write_promotion_edition(
                    run, promoted, unchanged, canonical_root=canonical,
                    now=datetime(2026, 9, 29, 8, 0, 0, tzinfo=timezone.utc))
                manifest = json.loads(Path(edition_path).read_text(encoding="utf-8"))
                for volatile in ("run_id",):
                    manifest["lineage"].pop(volatile)
                manifests.append((promoted, unchanged, manifest,
                                  snapshot(canonical / "research/trackers")))
        (promoted_a, unchanged_a, manifest_a, files_a) = manifests[0]
        (promoted_b, unchanged_b, manifest_b, files_b) = manifests[1]
        self.assertEqual(promoted_a, promoted_b)
        self.assertEqual(unchanged_a, unchanged_b)
        self.assertEqual(files_a, files_b)
        # identical modulo run_id (popped above) and the edition timestamp
        manifest_a["edition_id"] = manifest_b["edition_id"] = ""
        manifest_a["created_at"] = manifest_b["created_at"] = ""
        self.assertEqual(manifest_a, manifest_b)

    def test_promotion_edition_chains_to_the_latest_prior_edition(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            parent = Path(temp)
            canonical, run = self._seeded_run(parent)
            editions = canonical / "editions"
            editions.mkdir()
            (editions / "edition-20260928T145304Z.json").write_text("{}\n", encoding="utf-8")
            promoted, unchanged = REFRESH.promote(run, canonical_root=canonical)
            _, edition_path = REFRESH.write_promotion_edition(
                run, promoted, unchanged, canonical_root=canonical)
            manifest = json.loads(Path(edition_path).read_text(encoding="utf-8"))
            self.assertEqual(manifest["lineage"]["prior_edition"], "20260928T145304Z")


class StagedRunGuardTests(EnvIsolation):
    """With a run active, collectors must not write one byte into canonical."""

    def test_offline_collector_writes_only_into_the_run_dir(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            sandbox = Path(temp) / "repo"
            sandbox_trackers = sandbox / "data" / "canonical" / "research" / "trackers"
            shutil.copytree(TRACKER_ROOT, sandbox_trackers)
            run = WP.new_run(label="guard", data_root=sandbox / "data")
            # The orchestrator env: root override + GE16_RUN_DIR, then the
            # knobs derived by apply_env (exactly what refresh --run exports).
            env = dict(os.environ)
            env["HITUNGKERUSI_ROOT"] = str(sandbox)
            env[WP.RUN_ENV] = str(run.root)
            WP.apply_env(env=env)

            before = snapshot(sandbox / "data" / "canonical")
            result = subprocess.run(
                [sys.executable, str(COLLECT_ROOT / "track_ge16_news.py"), "--judge-input"],
                cwd=str(PROJECT_ROOT), capture_output=True, text=True, env=env,
                timeout=300)
            self.assertEqual(result.returncode, 0,
                             "collector failed: %s" % result.stderr[-2000:])
            self.assertEqual(snapshot(sandbox / "data" / "canonical"), before,
                             "a collector wrote into canonical while a run was active")
            # and the run dir captured the outputs the guard protected
            self.assertTrue((run.trackers / "ge16-news-judge-manifest.json").is_file())
            self.assertTrue((run.trackers / "ge16-news-judge-batch-1.json").is_file())
            self.assertTrue(run.selfheal_state.is_file())
            self.assertEqual(run.selfheal_state.parent, run.state)

    def test_real_repository_work_tree_stays_byte_identical(self):
        """Same collector, same env shape, run dir in tmp: the REAL canonical
        trackers (the durable evidence) must not move either."""
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            run = WP.new_run(label="guard-live-tree", data_root=Path(temp))
            env = dict(os.environ)
            env[WP.RUN_ENV] = str(run.root)
            WP.apply_env(env=env)
            before = snapshot(TRACKER_ROOT)
            result = subprocess.run(
                [sys.executable, str(COLLECT_ROOT / "track_ge16_news.py"), "--judge-input"],
                cwd=str(PROJECT_ROOT), capture_output=True, text=True, env=env,
                timeout=300)
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            self.assertEqual(snapshot(TRACKER_ROOT), before)
            self.assertTrue((run.trackers / "ge16-news-judge-manifest.json").is_file())


class AuditPreservationTests(EnvIsolation):
    """No code path deletes anything under canonical (grep/AST + behavioral)."""

    DELETION_RE = re.compile(r"os\.remove\(|os\.unlink\(|os\.rmdir\(|\.unlink\(|\.rmdir\(")

    def _deletion_calls(self, path: Path):
        """AST Calls that delete filesystem entries (os.remove, os.unlink,
        os.rmdir, path.unlink/rmdir, shutil.rmtree) — not list.remove etc."""
        calls = []
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute):
                continue
            attribute, receiver = node.func.attr, node.func.value
            if attribute in ("unlink", "rmdir", "rmtree"):
                calls.append(node)  # no non-deleting meaning on paths/dirs
            elif attribute == "remove" and isinstance(receiver, ast.Name) \
                    and receiver.id == "os":
                calls.append(node)
        return calls

    def test_no_rmtree_anywhere_under_scripts(self):
        for source in sorted(SCRIPTS_ROOT.rglob("*.py")):
            self.assertNotIn("rmtree", source.read_text(encoding="utf-8"),
                             str(source))

    def test_work_paths_has_zero_deletion_calls(self):
        self.assertEqual(self._deletion_calls(SCRIPTS_ROOT / "work_paths.py"), [])

    def test_refresh_deletes_only_its_own_scratch_temporary(self):
        calls = self._deletion_calls(SCRIPTS_ROOT / "refresh_canonical_data.py")
        self.assertEqual(len(calls), 1, "expected only the pre-existing scratch cleanup")
        argument = calls[0].args[0]
        self.assertIsInstance(argument, ast.Name)
        self.assertEqual(argument.id, "temporary")

    def test_collector_deletions_are_scratch_or_outdir_g_scoped(self):
        """Every collector deletion is either a mkstemp scratch file or a path
        taken from outdir.g() — which, under a run, matches the STAGE only."""
        for source in sorted(COLLECT_ROOT.glob("*.py")):
            lines = source.read_text(encoding="utf-8").splitlines()
            for index, line in enumerate(lines):
                if not self.DELETION_RE.search(line):
                    continue
                context = "\n".join(lines[max(0, index - 8):index + 1])
                self.assertTrue(
                    "outdir.g(" in context or "temporary" in line,
                    "%s:%d deletes outside scratch/staging scope: %s"
                    % (source.name, index + 1, line.strip()))

    def test_double_promotion_keeps_every_prior_canonical_file(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            parent = Path(temp)
            canonical, run = self._seeded_audit_heavy_tree(parent)
            before = snapshot(canonical)
            for _ in range(2):
                REFRESH.promote(run, canonical_root=canonical)
            after = snapshot(canonical)
            self.assertEqual(after, before | {
                "research/trackers/ge16-news-judge-batch-1.json":
                    after["research/trackers/ge16-news-judge-batch-1.json"]})
            for name in before:  # every pre-existing file still present, unmodified
                self.assertEqual(after[name], before[name], name)

    def _seeded_audit_heavy_tree(self, parent: Path):
        canonical = seed_canonical(parent)
        extra = canonical / "research" / "raw"
        extra.mkdir(parents=True, exist_ok=True)
        (extra / "imported-v2-audit.csv").write_bytes(b"a,b\r\n1,2\r\n")
        editions = canonical / "editions"
        editions.mkdir(parents=True, exist_ok=True)
        (editions / "edition-20260928T145304Z.json").write_text("{}\n")
        run = WP.new_run(label="audit", data_root=parent / "data")
        (run.trackers / "ge16-news-judge-batch-1.json").write_text('{"batch": 1}\n',
                                                                   encoding="utf-8")
        return canonical, run


class StagedRefreshFlowTests(EnvIsolation):
    """The full orchestrator flow with a real (offline) collector subprocess."""

    def setUp(self):
        super().setUp()
        self._temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(self._temporary.cleanup)
        # sandbox mirrors the real layout <repo>/data/canonical so the
        # collectors' v3_paths resolution (HITUNGKERUSI_ROOT) lands here
        self.sandbox = Path(self._temporary.name) / "repo"
        self.data = self.sandbox / "data"
        self.canonical = self.data / "canonical"
        for relative_root in REFRESH.CANONICAL_ROOTS:
            (self.canonical / relative_root).mkdir(parents=True, exist_ok=True)
        original = json.loads((REPOSITORY_ROOT / "canonical" /
                               REFRESH.MANIFEST).read_text(encoding="utf-8"))
        original["files"] = [{
            "destination_path": "research/raw/existing.csv", "bytes": 1,
            "sha256": "0" * 64,
            "historic_source_path": "../HERMES/01_RESEARCH/data/raw/existing.csv",
        }]
        original["methodology_inputs"] = [{
            "canonical_path": "research/data/notes/method.md",
            "legacy_source_relative_path": "01_RESEARCH/data/notes/method.md",
            "sha256": "0" * 64, "bytes": 0,
            "classification": "canonical-methodology-input",
            "consumer_roles": ["methodology"],
        }]
        (self.canonical / REFRESH.MANIFEST).write_text(json.dumps(original))
        (self.canonical / "research/raw/existing.csv").write_bytes(b"x,y\r\n1,2\r\n")
        notes = self.canonical / "research/data/notes"
        notes.mkdir(parents=True)
        (notes / "method.md").write_bytes(b"methodology\n")
        # the offline collector reads the candidate list from the live tree
        (self.canonical / "research/trackers/ge16-news-candidates.json").write_bytes(
            (TRACKER_ROOT / "ge16-news-candidates.json").read_bytes())

    def _offline_news_collector(self):
        return [[sys.executable, str(COLLECT_ROOT / "track_ge16_news.py"),
                 "--judge-input"]]

    def test_staged_refresh_end_to_end(self):
        os.environ["HITUNGKERUSI_ROOT"] = str(self.sandbox)
        summary = REFRESH.staged_refresh(
            label="p2.3 e2e", canonical_root=self.canonical,
            collector_commands=self._offline_news_collector())

        run_root = Path(summary["run_dir"])
        self.assertEqual(run_root, self.data / "work" / summary["run_id"])
        self.assertTrue((run_root / "run.json").is_file())
        self.assertTrue((run_root / "trackers/ge16-news-judge-manifest.json").is_file())
        # self-heal ledger stayed in the run dir, never in canonical trackers
        self.assertTrue((run_root / "state/selfheal-state.json").is_file())
        self.assertFalse((self.canonical / "research/trackers/ge16-selfheal-state.json")
                         .exists())

        self.assertTrue(summary["promoted"])
        for entry in summary["promoted"]:
            promoted_file = self.canonical / entry["file"]
            self.assertTrue(promoted_file.is_file(), entry["file"])
            self.assertEqual(
                hashlib.sha256(promoted_file.read_bytes()).hexdigest(), entry["sha256"])

        manifest = json.loads(Path(summary["edition_path"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema"], "ge16.edition.promotion.v1")
        self.assertEqual(manifest["lineage"]["run_id"], summary["run_id"])
        self.assertIsNone(manifest["lineage"]["prior_edition"])

        provenance = json.loads(
            (self.canonical / REFRESH.MANIFEST).read_text(encoding="utf-8"))
        listed = {entry["destination_path"] for entry in provenance["files"]}
        self.assertIn("research/trackers/ge16-news-judge-batch-1.json", listed)
        self.assertEqual(summary["files"], len(listed))

    def test_stale_candidates_are_not_resurrected(self):
        """The run reads candidates from ITS sandbox live tree (root override),
        not the real repository: identical staged input twice -> same promotion."""
        os.environ["HITUNGKERUSI_ROOT"] = str(self.sandbox)
        first = REFRESH.staged_refresh(
            label="determinism", canonical_root=self.canonical,
            collector_commands=self._offline_news_collector())
        names_first = sorted(entry["file"] for entry in first["promoted"])
        # a second run over the SAME (now promoted) canonical tree: the
        # collector re-stages the same batches, promotion is idempotent
        second = REFRESH.staged_refresh(
            label="determinism", canonical_root=self.canonical,
            collector_commands=self._offline_news_collector())
        names_second = sorted(entry["file"] for entry in second["promoted"] + second["unchanged"])
        self.assertEqual(names_first, names_second)
        self.assertNotEqual(first["run_id"], second["run_id"])

    def test_failed_collector_refuses_promotion_and_keeps_the_run_dir(self):
        failing = [[sys.executable, "-c", "raise SystemExit(3)"]]
        with self.assertRaises(RuntimeError):
            REFRESH.staged_refresh(label="broken", canonical_root=self.canonical,
                                   collector_commands=failing)
        trackers = self.canonical / "research/trackers"
        self.assertEqual(
            [path.name for path in sorted(trackers.iterdir())],
            ["ge16-news-candidates.json"])  # nothing promoted
        self.assertFalse((self.canonical / "editions").exists())
        # the failed run's evidence is retained (never deleted) for audit
        runs = sorted((self.data / "work").iterdir())
        self.assertEqual(len(runs), 1)
        logs = sorted((runs[0] / "logs").glob("*.log"))
        self.assertEqual(len(logs), 1)
        self.assertIn("exit=3", logs[0].read_text(encoding="utf-8"))


class SchemaFixtureTests(EnvIsolation):
    def test_fixture_validates_against_the_promotion_schema(self):
        import jsonschema
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        jsonschema.validate(fixture, schema)
        # the lineage extension the packet requires is really in the schema
        self.assertIn("run_id", schema["properties"]["lineage"]["required"])

    def test_frozen_p22_edition_schema_is_untouched(self):
        p22 = json.loads((SCRIPTS_ROOT / "schemas/ge16_edition.schema.json")
                         .read_text(encoding="utf-8"))
        self.assertEqual(sorted(p22["properties"]["lineage"]["properties"]),
                         ["inputs", "prior_edition"])
        self.assertEqual(p22["properties"]["lineage"]["additionalProperties"], False)


if __name__ == "__main__":
    unittest.main()
