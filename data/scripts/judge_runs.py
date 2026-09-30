#!/usr/bin/env python3
"""Resumable judgment-run primitive (P2.7).

A judgment run decides an ordered, fixed item list one item at a time,
checkpointing after every item so a killed run resumes exactly after the
last recorded decision. Run state lives under ``data/work/<run_id>/judge/``
(``work_paths.new_run`` scopes the directory; P2.3 precedent).

Two files per run:
  run-state.json   ``ge16.judgment-run.v1`` — counters + completion flag.
  decisions.jsonl  append-only log, one row per recorded decision. Each row
                   embeds a ``judgment`` object that is a schema-valid
                   ``ge16.judgment.v1`` row whenever the outcome is
                   promotable (``verified``/``rejected-stale``); an
                   ``unresolved`` outcome carries ``judgment: null`` — the
                   owner-ruled three-way policy (design brief §2.1) never
                   supersedes on a probe error/timeout, so no judgment row
                   exists to validate yet. ``judge`` identity is carried in
                   the embedded judgment's ``judge.model``/``judge.by``
                   fields (the frozen schema has no bare identity-string
                   slot); ``input_sha256`` reuses ``basis.source_sha256``
                   (nullable, already sha256-shaped, and unused by
                   ``orphaned-flag`` origin rows before this).

Promotion (orphaned-flag re-judge only; queue-evidence is scaffold-only,
see below) reuses the EXISTING refresh-run promotion layer
(``refresh_canonical_data.write_promotion_edition`` + ``post_promotion_gate``)
verbatim — this module does not open a second promotion path.

Blessed semantics (P2.8 F7, owner ruling design brief §4.4): on an
``orphaned-flag`` judgment row, ``basis.source_sha256`` is the sha256 of the
probe input actually judged (the serialized probe result), not a hash of
the article body; ``basis.verifiable: true`` means "publisher reachable at
judgment time" — a liveness check, not a content-accuracy claim.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_ROOT = SCRIPTS_ROOT.parent / "canonical"
SCHEMAS_DIR = SCRIPTS_ROOT / "schemas"

if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))
import work_paths  # noqa: E402  (sibling module; P2.3)

import jsonschema  # noqa: E402

RUN_STATE_SCHEMA = "ge16.judgment-run.v1"
JUDGMENT_SCHEMA = "ge16.judgment.v1"
STATE_FILE = "run-state.json"
DECISIONS_FILE = "decisions.jsonl"
REVIEW_QUEUE_FILE = "review-queue.jsonl"
EXCLUSIONS_FILE = "exclusions.json"
#: Ruling §2.3: checkpoint (run-state write) after every item; fsync the
#: decisions file once per this many appended rows.
FSYNC_BATCH = 20
VERDICTS = ("verified", "rejected-stale", "unresolved")
#: Wrapper hosts whose 200-OK is not publisher liveness (design brief §5).
WRAPPER_HOSTS = ("news.google.com",)


def _load_sibling(filename, module_name):
    """Load a helper module by path (repo convention; not a package import)."""
    path = SCRIPTS_ROOT / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_import_sibling(filename, module_name):
    path = SCRIPTS_ROOT / "import" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_IDENTITY = _load_import_sibling("identity.py", "p27_judge_runs_identity")


def _now_iso(now=None):
    moment = now or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _validator(schema_filename):
    schema = json.loads((SCHEMAS_DIR / schema_filename).read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema)


_RUN_STATE_VALIDATOR = None
_JUDGMENT_VALIDATOR = None


def run_state_validator():
    global _RUN_STATE_VALIDATOR
    if _RUN_STATE_VALIDATOR is None:
        _RUN_STATE_VALIDATOR = _validator("ge16_judgment-run.schema.json")
    return _RUN_STATE_VALIDATOR


def judgment_validator():
    global _JUDGMENT_VALIDATOR
    if _JUDGMENT_VALIDATOR is None:
        _JUDGMENT_VALIDATOR = _validator("ge16_judgment.schema.json")
    return _JUDGMENT_VALIDATOR


# ---------------------------------------------------------------- session

class JudgeSession:
    """One open judgment run: fixed item list + run-scoped paths + state."""

    __slots__ = ("run", "items", "state", "state_path", "decisions_path",
                 "review_queue_path", "_index_by_id", "_history_by_id",
                 "_append_count")

    def __init__(self, run, items, state):
        self.run = run
        self.items = items
        self.state = state
        self.state_path = run.judge / STATE_FILE
        self.decisions_path = run.judge / DECISIONS_FILE
        self.review_queue_path = run.judge / REVIEW_QUEUE_FILE
        self._index_by_id = {item["evidence_id"]: index for index, item in enumerate(items)}
        self._history_by_id = {}
        self._append_count = 0

    @property
    def run_id(self):
        return self.run.run_id

    def _write_state(self):
        self.state["updated_at"] = _now_iso()
        errors = list(run_state_validator().iter_errors(self.state))
        if errors:
            raise ValueError(f"run-state failed schema validation: {errors[0].message}")
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.state, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                       encoding="utf-8")
        os.replace(tmp, self.state_path)


def _read_jsonl_recover(path):
    """Read a JSONL file, discarding a torn (unparseable) LAST line only.

    A crash mid-write can only corrupt the line being written (JSONL appends
    are otherwise atomic per-line); anything but the last line failing to
    parse is a genuine corruption this function does not try to hide.
    Returns (rows, torn: bool).
    """
    if not path.is_file():
        return [], False
    lines = path.read_text(encoding="utf-8").splitlines()
    rows = []
    torn = False
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            if index == len(lines) - 1:
                torn = True
                continue
            raise ValueError(f"corrupt decisions row at line {index + 1} of {path} (not the last line)")
    return rows, torn


def _rewrite_clean(path, rows):
    tmp = path.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(tmp, path)


def open_run(items, judge, run=None, item_class="orphaned-flag", expectations=None,
             data_root=None, now=None):
    """Create or reopen a judgment-run session bound to ``items`` (ordered).

    ``run`` may be a pre-existing ``RunPaths`` (reuses its ``judge`` dir
    without creating a second run dir); ``None`` creates a fresh run via
    ``work_paths.new_run``. Reopening an existing run's state (same
    ``run.judge/run-state.json``) resumes it — see :func:`resume`.
    """
    if run is None:
        run = work_paths.new_run(label=f"judge:{item_class}", data_root=data_root, now=now)
    state_path = run.judge / STATE_FILE
    if state_path.is_file():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("total_items") != len(items):
            raise ValueError(
                f"resume item-count mismatch: run-state has {state.get('total_items')}, "
                f"caller passed {len(items)} items — item list must be identical across resumes")
    else:
        moment = now or datetime.now(timezone.utc)
        state = {
            "schema": RUN_STATE_SCHEMA,
            "run_id": run.run_id,
            "judge": judge,
            "item_class": item_class,
            "total_items": len(items),
            "decided": 0,
            "failed": 0,
            "next_index": 0,
            "started_at": _now_iso(moment),
            "updated_at": _now_iso(moment),
            "expectations": expectations or {"verified": None, "rejected-stale": None, "unresolved": None},
            "counts": {"verified": 0, "rejected-stale": 0, "unresolved": 0,
                       "pending": len(items), "pending-approval": 0},
            "complete": False,
        }
        errors = list(run_state_validator().iter_errors(state))
        if errors:
            raise ValueError(f"run-state failed schema validation: {errors[0].message}")
        run.judge.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                       encoding="utf-8")
        os.replace(tmp, state_path)
    session = JudgeSession(run=run, items=items, state=state)
    resume(session)
    return session


def resume(session):
    """Re-verify decisions.jsonl against the state counter; discard a torn
    tail; rebuild in-memory history; return the index to continue from.

    Idempotent: calling this on a session that was never interrupted is a
    cheap no-op re-derivation of the same next_index.
    """
    rows, torn = _read_jsonl_recover(session.decisions_path)
    if torn:
        _rewrite_clean(session.decisions_path, rows)
    history = {}
    for row in rows:
        evidence_id = row["evidence_id"]
        history.setdefault(evidence_id, []).append(row)
    session._history_by_id = history
    session._append_count = len(rows)
    # Re-derive counters from the recovered decision history (source of
    # truth) rather than trusting a possibly-stale run-state counter.
    counts = {"verified": 0, "rejected-stale": 0, "unresolved": 0,
              "pending": 0, "pending-approval": 0}
    next_index = 0
    decided_positions = set()
    for evidence_id, rows_for_item in history.items():
        index = session._index_by_id.get(evidence_id)
        if index is None:
            continue
        decided_positions.add(index)
        latest = rows_for_item[-1]
        counts[latest["verdict"]] += 1
    for index in range(len(session.items)):
        if index not in decided_positions:
            counts["pending"] += 1
    next_index = (max(decided_positions) + 1) if decided_positions else 0
    session.state["decided"] = len(decided_positions)
    session.state["next_index"] = next_index
    session.state["counts"] = counts
    session._write_state()
    return next_index


def _to_judgment_row(evidence_id, verdict, reason, input_sha256, judge, judged_at,
                     supersedes, decision_id, run_id=None):
    """Build the embedded ge16.judgment.v1 row for a promotable verdict.

    The frozen schema has no top-level ``supersedes`` slot for judgments
    (only evidence rows have one, ``ev``-patterned) — per the packet fence
    against schema edits, the supersede target is recorded in
    ``confidence_note`` text instead; ``decisions.jsonl``'s own envelope
    (the ``supersedes`` key one level up, see :func:`record_decision`)
    carries it in structured form for programmatic use.

    Blessed semantics (P2.8 F7): ``basis.source_sha256`` here is the sha256
    of the probe input actually judged (not the article body); ``basis.
    verifiable: true`` means "publisher reachable at judgment time".
    """
    mapped_verdict = "accept" if verdict == "verified" else "reject"
    note = f"P2.7 re-judge: {verdict} — {reason}"
    if supersedes:
        note += f" (supersedes {supersedes})"
    row = {
        "schema": JUDGMENT_SCHEMA,
        "judgment_id": decision_id,
        "evidence_id": evidence_id,
        "verdict": mapped_verdict,
        "judge": {"model": judge, "by": "judge_runs.py", "run_id": run_id},
        "judged_at": judged_at,
        "basis": {
            "origin": "orphaned-flag",
            "verifiable": verdict == "verified",
            "batch_file": None,
            "source_sha256": input_sha256,
        },
        "confidence_note": note,
    }
    errors = list(judgment_validator().iter_errors(row))
    if errors:
        raise ValueError(f"embedded judgment row failed schema validation: {errors[0].message}")
    return row


def record_decision(session, item, verdict, reason, input_sha256, judge=None, now=None,
                    supersedes_canonical_id=None):
    """Append one decision for ``item`` (must be a member of the session's
    item list). Per-item idempotent: recording the same item with the same
    ``input_sha256`` twice is a no-op on the second call. A changed
    ``input_sha256`` records a NEW decision row whose ``supersedes`` points
    at the prior decision's ``judgment_id`` for this evidence_id — both rows
    are retained (append-only).

    ``supersedes_canonical_id`` (P2.7 F1 remediation): for a first decision
    on an item that already has a CANONICAL judgment being superseded (the
    orphaned-flag re-judge case), pass the old canonical judgment id here —
    the decision row then links to it machine-readably even though no
    in-run prior exists. In-run history still wins when present (later
    decisions supersede the in-run row, never the canonical one twice).
    """
    if verdict not in VERDICTS:
        raise ValueError(f"unknown verdict {verdict!r}; expected one of {VERDICTS}")
    evidence_id = item["evidence_id"]
    if evidence_id not in session._index_by_id:
        raise ValueError(f"item {evidence_id} is not a member of this run's item list")
    prior_history = session._history_by_id.get(evidence_id, [])
    prior_last = prior_history[-1] if prior_history else None
    if prior_last and prior_last["input_sha256"] == input_sha256:
        return prior_last  # idempotent no-op

    moment = now or datetime.now(timezone.utc)
    judged_at = _now_iso(moment)
    judge_identity = judge or session.state["judge"]
    supersedes = prior_last["judgment_id"] if prior_last else supersedes_canonical_id
    # judgment_id must be re-derivable by integrity.verify_corpus, which
    # recomputes it from (evidence_id, judged_at, verdict) using the STORED
    # ge16.judgment.v1 verdict (accept/reject) — not the three-way probe
    # outcome — so promotable decisions hash on the mapped verdict.
    id_verdict = "accept" if verdict == "verified" else "reject" if verdict == "rejected-stale" else verdict
    decision_id = _IDENTITY.judgment_id(evidence_id, judged_at, id_verdict)

    judgment = None
    if verdict in ("verified", "rejected-stale"):
        judgment = _to_judgment_row(evidence_id, verdict, reason, input_sha256,
                                    judge_identity, judged_at, supersedes, decision_id,
                                    run_id=session.run_id)

    row = {
        "judgment_id": decision_id,
        "evidence_id": evidence_id,
        "verdict": verdict,
        "reason": reason,
        "input_sha256": input_sha256,
        "judge": judge_identity,
        "run_id": session.run_id,
        "decided_at": judged_at,
        "supersedes": supersedes,
        "judgment": judgment,
    }

    with open(session.decisions_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        session._append_count += 1
        if session._append_count % FSYNC_BATCH == 0:
            handle.flush()
            os.fsync(handle.fileno())

    is_first_decision = prior_last is None
    session._history_by_id.setdefault(evidence_id, []).append(row)
    index = session._index_by_id[evidence_id]
    counts = session.state["counts"]
    if is_first_decision:
        counts["pending"] -= 1
        counts[verdict] += 1
        session.state["decided"] += 1
        session.state["next_index"] = max(session.state["next_index"], index + 1)
    else:
        counts[prior_last["verdict"]] -= 1
        counts[verdict] += 1
    session._write_state()
    return row


def close_run(session, notes=None):
    """Finalize the run: write run-summary.json; complete is False whenever
    any item is pending or pending-approval (partial work never claims
    completion — acceptance criterion 3). An EMPTY run (zero items) also
    reports complete=False: a run that decided nothing has not demonstrated
    anything (F2 remediation — an empty queue scaffold against the live
    corpus previously reported complete=true)."""
    counts = session.state["counts"]
    complete = (counts["pending"] == 0 and counts["pending-approval"] == 0
                and session.state["failed"] == 0
                and session.state["total_items"] > 0)
    session.state["complete"] = complete
    session._write_state()
    summary = dict(session.state)
    if notes:
        (session.run.judge / "run-notes.json").write_text(
            json.dumps(notes, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8")
    (session.run.judge / "run-summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8")
    return summary


# ---------------------------------------------------------------- probing

def _http_request(url, method, timeout):
    request = urllib.request.Request(url, method=method,
                                     headers={"User-Agent": "ge16-p2.7-probe/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return {"http_status": response.status, "final_url": response.geturl(), "error": None}
    except urllib.error.HTTPError as exc:
        return {"http_status": exc.code, "final_url": exc.geturl() or url, "error": None}
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
        return {"http_status": None, "final_url": url, "error": str(exc)}


def default_prober(url, timeout=8):
    """One-hop HTTP HEAD, falling back to one GET when the server refuses
    HEAD (405/501) — the network seam. The timeout budget is split evenly
    across the two attempts so the worst case still respects ``timeout``.

    Real network I/O lives ONLY here; every caller that wants a stub passes
    its own ``prober(url, timeout) -> dict`` callable instead.
    """
    half = max(timeout / 2, 1)
    result = _http_request(url, "HEAD", half)
    if result["http_status"] in (405, 501):
        result = _http_request(url, "GET", half)
    return result


def _is_wrapper(url):
    hostname = urllib.parse.urlparse(url).hostname or ""
    return hostname in WRAPPER_HOSTS


def classify_probe(result):
    """Owner-ruled three-way mapping (design brief §2.1) from a raw probe
    result ({"http_status", "final_url", "error"}) to a verdict + reason."""
    if result.get("error"):
        return "unresolved", f"probe error: {result['error']}"
    status = result.get("http_status")
    final_url = result.get("final_url") or ""
    if status == 200 and not _is_wrapper(final_url):
        return "verified", f"publisher 200 at {final_url}"
    if status in (404, 410):
        return "rejected-stale", f"publisher {status} at {final_url}"
    if _is_wrapper(final_url):
        return "rejected-stale", f"wrapper-only, no publisher resolution (status={status}) at {final_url}"
    return "unresolved", f"ambiguous probe result (status={status}) at {final_url}"


def probe_item(url, prober=default_prober, timeout=8):
    result = prober(url, timeout)
    verdict, reason = classify_probe(result)
    input_sha256 = _sha256_text(json.dumps(result, sort_keys=True))
    return verdict, reason, input_sha256


# ------------------------------------------------------ orphaned-flag pass

NO_SURVIVING_BATCH_NOTE_PREFIX = "Backfill flag whose generating batch no longer exists"
DISAGREE_NOTE_PREFIX = "Backfill flag references a judged batch that no longer exists"


def _load_judgment_rows(canonical_root=CANONICAL_ROOT):
    directory = Path(canonical_root) / "judgments"
    rows = []
    for name in sorted(os.listdir(directory)):
        if not (name.startswith("judgment-") and name.endswith(".jsonl")):
            continue
        with open(directory / name, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows


def _load_evidence_index(canonical_root=CANONICAL_ROOT):
    directory = Path(canonical_root) / "evidence"
    index = {}
    for name in sorted(os.listdir(directory)):
        if not (name.startswith("evidence-") and name.endswith(".jsonl")):
            continue
        with open(directory / name, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    row = json.loads(line)
                    index[row["evidence_id"]] = row
    return index


_SUPERSEDES_RE = re.compile(r"\(supersedes (\S+)\)")


def _already_superseded_judgment_ids(rows):
    """judgment_ids that a LATER promoted row's ``confidence_note`` names as
    superseded. Per R2-N1 (binding): only promoted rows (``verified``/
    ``rejected-stale``) ever produce a judgment row with a supersede note —
    an ``unresolved`` probe outcome never writes one (module docstring) — so
    scanning judgment-row notes alone already excludes unresolved links;
    no separate verdict filter is needed here."""
    superseded = set()
    for row in rows:
        note = row.get("confidence_note", "") or ""
        match = _SUPERSEDES_RE.search(note)
        if match:
            superseded.add(match.group(1))
    return superseded


def select_orphaned_flag_subset(canonical_root=CANONICAL_ROOT, include_already_superseded=False):
    """Return (no_surviving_batch_items, total_orphaned_flag_count,
    disagree_count). ``no_surviving_batch_items`` is a list of
    ``{"evidence_id", "url", "judgment_id"}`` dicts — the 498-row re-judge
    target (design brief scoping correction; packet §1.2).

    ``include_already_superseded=False`` (P2.8 F4, default): excludes items
    whose old judgment_id already appears in some later promoted judgment
    row's supersede note — i.e. a probe decision already resolved it in a
    prior promotion. Internal consistency is asserted by the caller via
    ``subset_count + already_superseded_count == total_no_surviving_batch``,
    never a hardcoded literal (the corpus grows across runs)."""
    rows = _load_judgment_rows(canonical_root)
    evidence_index = _load_evidence_index(canonical_root)
    superseded_ids = _already_superseded_judgment_ids(rows) if not include_already_superseded else set()
    total = 0
    no_surviving_batch = []
    disagree = 0
    for row in rows:
        basis = row.get("basis", {})
        if basis.get("origin") != "orphaned-flag":
            continue
        total += 1
        note = row.get("confidence_note", "") or ""
        if note.startswith(NO_SURVIVING_BATCH_NOTE_PREFIX):
            if row["judgment_id"] in superseded_ids:
                continue
            evidence = evidence_index.get(row["evidence_id"], {})
            url = (evidence.get("payload") or {}).get("link") or (evidence.get("payload") or {}).get("url") or ""
            no_surviving_batch.append({
                "evidence_id": row["evidence_id"],
                "judgment_id": row["judgment_id"],
                "url": url,
            })
        elif note.startswith(DISAGREE_NOTE_PREFIX):
            disagree += 1
    return no_surviving_batch, total, disagree


def assert_orphaned_flag_counts_consistent(subset_count, disagree_count, total,
                                           already_superseded_count=0):
    """Internal-consistency count guard (P2.8 F4 remediation): derives its
    expectation from the corpus itself instead of a hardcoded literal —
    ``subset + disagree + already_superseded == total`` must hold, or the
    selector's partition logic has a bug. Raises on mismatch."""
    if subset_count + disagree_count + already_superseded_count != total:
        raise RuntimeError(
            "orphaned-flag count guard failed: subset(%d) + disagree(%d) + "
            "already_superseded(%d) != total(%d)"
            % (subset_count, disagree_count, already_superseded_count, total))


def run_orphaned_flag_pass(run=None, canonical_root=CANONICAL_ROOT, judge="probe/1.0",
                           prober=default_prober, data_root=None, now=None,
                           include_already_superseded=False):
    """Open (or resume) the no-surviving-batch re-judge run and probe every
    item that is still pending. Does NOT promote — call
    :func:`promote_orphaned_flag_run` separately once ``close_run`` reports
    the run complete."""
    items, total, disagree = select_orphaned_flag_subset(
        canonical_root, include_already_superseded=include_already_superseded)
    all_items, _, _ = select_orphaned_flag_subset(canonical_root, include_already_superseded=True)
    already_superseded_count = len(all_items) - len(items)
    assert_orphaned_flag_counts_consistent(len(items), disagree, total, already_superseded_count)
    expectations = {"verified": None, "rejected-stale": None, "unresolved": None}
    session = open_run(items, judge, run=run, item_class="orphaned-flag",
                       expectations=expectations, data_root=data_root, now=now)
    for index in range(session.state["next_index"], len(items)):
        item = items[index]
        verdict, reason, input_sha256 = probe_item(item["url"], prober=prober)
        record_decision(session, item, verdict, reason, input_sha256, judge=judge,
                        supersedes_canonical_id=item.get("judgment_id"))
    summary = close_run(session, notes={
        "disagree_rows_excluded": disagree,
        "disagree_note_prefix": DISAGREE_NOTE_PREFIX,
        "disagree_disposition": "unchanged: evidence already backed by a surviving judged-batch judgment",
        "target_subset": "no-surviving-batch (note prefix: %r)" % NO_SURVIVING_BATCH_NOTE_PREFIX,
    })
    return session, summary


def promote_orphaned_flag_run(session, canonical_root=CANONICAL_ROOT, prior=None, now=None,
                              disagree=None):
    """Promote a COMPLETE orphaned-flag run's promotable decisions into
    canonical/judgments/, reusing the refresh-run promotion + verifier gate
    layer verbatim (packet §1.2: 'Reuse it; do not build a second promotion
    path').

    ``disagree`` (P2.8 R1 MINOR-7): the derived disagree count the pass
    excluded; recorded in the edition note. Accepted as a parameter because
    the session state does not carry it — callers (run_orphaned_flag_pass)
    have it from the selector. Defaults to the sentinel derived from the
    notes file when possible, else 0.
    """
    if not session.state["complete"]:
        raise RuntimeError("refusing to promote: run is not complete (pending items remain)")
    if disagree is None:
        notes_path = session.run.judge / "run-notes.json"
        if notes_path.is_file():
            disagree = json.loads(notes_path.read_text(encoding="utf-8")).get(
                "disagree_rows_excluded", 0)
        else:
            disagree = 0
    refresh = _load_sibling("refresh_canonical_data.py", "p27_judge_runs_refresh")
    import_evidence = _load_import_sibling("import_evidence.py", "p27_judge_runs_import_evidence")

    rows, _ = _read_jsonl_recover(session.decisions_path)
    store = import_evidence.JudgmentStore(str(canonical_root))
    added = 0
    for row in rows:
        judgment = row.get("judgment")
        if judgment is None:
            continue
        if store.add(judgment):
            added += 1
    if added == 0:
        return None, None, []
    store.flush()

    promoted = []
    shard_nibbles = {row["judgment"]["judgment_id"][2] for row in rows if row.get("judgment")}
    judgments_dir = Path(canonical_root) / "judgments"
    for nibble in sorted(shard_nibbles):
        path = judgments_dir / f"judgment-{nibble}.jsonl"
        digest = refresh._sha256_file(path)
        relative = f"judgments/judgment-{nibble}.jsonl"
        promoted.append({"file": relative, "sha256": digest, "disposition": "judgment-run-promotion"})

    evidence_total = sum(1 for _ in _load_evidence_index(canonical_root))
    judgments_total = len(_load_judgment_rows(canonical_root))
    entity_candidates_path = Path(canonical_root) / "entities" / "entity-candidates.jsonl"
    entity_candidates_total = 0
    if entity_candidates_path.is_file():
        with open(entity_candidates_path, encoding="utf-8") as handle:
            entity_candidates_total = sum(1 for line in handle if line.strip())
    counts = session.state["counts"]
    edition_id, edition_path = refresh.write_promotion_edition(
        session.run, promoted, unchanged=[], canonical_root=canonical_root, prior=prior, now=now,
        extra_row_counts={"evidence_total": evidence_total, "judgments_total": judgments_total,
                          "entity_candidates_total": entity_candidates_total},
        note=("P2.7 orphaned-flag re-judge promotion: %d of %d no-surviving-batch rows superseded "
              "(verified=%d, rejected-stale=%d, unresolved=%d retained-flagged); "
              "%d disagree rows excluded (already backed by a surviving judged-batch judgment)."
              % (added, session.state["total_items"], counts["verified"],
                 counts["rejected-stale"], counts["unresolved"], disagree)))
    reports = refresh.post_promotion_gate(session.run, edition_id, canonical_root)
    return edition_id, edition_path, reports


# ------------------------------------------------------ queue-evidence scaffold

#: Tracker files whose evidence rows are queue-class (P2.1 content
#: classification; F2 remediation). Filename substring matching was wrong
#: twice over: no queue tracker file contains "queue" in its name, and the
#: project forbids filename logic anyway (RED-test-pinned). This set IS a
#: filename list, but a pinned explicit one — the same approach the
#: importer's disposition table takes — derived from P2.1/P2.2 facts:
#: seen-pending, candidates and backfill-candidates are the three queue-class
#: sources; judged batches and the accepted corpus are NOT (they carry or
#: produced judgments).
QUEUE_CLASS_TRACKER_FILES = frozenset({
    "ge16-news-seen-pending.json",
    "ge16-news-candidates.json",
    "ge16-news-backfill-candidates.json",
})


def select_queue_evidence_without_judgment(canonical_root=CANONICAL_ROOT):
    """Evidence rows of ``kind == 'news'`` sourced from a queue-class tracker
    file that have no judgment row yet (P2.7 §1.3 scaffold target). F2
    remediation: matches on the pinned queue-class file set above, not on a
    filename substring."""
    rows_dir = Path(canonical_root) / "evidence"
    judged_ids = {row["evidence_id"] for row in _load_judgment_rows(canonical_root)}
    items = []
    for name in sorted(os.listdir(rows_dir)):
        if not (name.startswith("evidence-") and name.endswith(".jsonl")):
            continue
        with open(rows_dir / name, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if row["evidence_id"] in judged_ids:
                    continue
                source_file = (row.get("source_ref") or {}).get("file", "")
                if os.path.basename(source_file) not in QUEUE_CLASS_TRACKER_FILES:
                    continue
                url = (row.get("payload") or {}).get("link") or (row.get("payload") or {}).get("url") or ""
                items.append({"evidence_id": row["evidence_id"], "url": url})
    return items


def run_queue_evidence_pass(run=None, canonical_root=CANONICAL_ROOT, judge="probe/1.0",
                            prober=default_prober, data_root=None, now=None):
    """Scaffold pass: probes queue-evidence rows and records decisions.
    ``rejected-stale`` outcomes are written to ``review-queue.jsonl`` and are
    NEVER applied here (no supersede target exists for a queue item —
    it has no prior judgment). Nothing is promoted to canonical in this
    phase; ``apply_review_queue`` marks owner-approved rows for a future
    promotion pass, which is out of scope for P2.7 (packet §1.3)."""
    items = select_queue_evidence_without_judgment(canonical_root)
    session = open_run(items, judge, run=run, item_class="queue-evidence",
                       data_root=data_root, now=now)
    review_rows = []
    for index in range(session.state["next_index"], len(items)):
        item = items[index]
        verdict, reason, input_sha256 = probe_item(item["url"], prober=prober)
        row = record_decision(session, item, verdict, reason, input_sha256, judge=judge)
        if verdict == "rejected-stale":
            review_rows.append(row)
    if review_rows:
        with open(session.review_queue_path, "a", encoding="utf-8") as handle:
            for row in review_rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    # Queue items never auto-apply: report every non-unresolved decision as
    # pending-approval so close_run cannot claim completion for this class.
    counts = session.state["counts"]
    counts["pending-approval"] = counts["verified"] + counts["rejected-stale"]
    session._write_state()
    summary = close_run(session)
    return session, summary


def apply_review_queue(session, approved_evidence_ids):
    """Mark owner-approved review-queue rows as applied (bookkeeping only —
    P2.7 builds no promotion path for queue-evidence judgments; that is
    P2.8's corpus-rebuild concern per the packet's scope fence)."""
    rows, _ = _read_jsonl_recover(session.review_queue_path)
    applied = [row for row in rows if row["evidence_id"] in approved_evidence_ids]
    for row in applied:
        session.state["counts"]["pending-approval"] -= 1
    session._write_state()
    return applied


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pass_name", choices=["orphaned-flags", "queue-scaffold"])
    parser.add_argument("--promote", action="store_true",
                        help="orphaned-flags only: promote after a complete run")
    args = parser.parse_args()

    if args.pass_name == "orphaned-flags":
        session, summary = run_orphaned_flag_pass()
        print(json.dumps(summary, indent=2, sort_keys=True))
        if args.promote:
            notes = session.run.judge / "run-notes.json"
            disagree = json.loads(notes.read_text(encoding="utf-8")).get(
                "disagree_rows_excluded", 0) if notes.is_file() else 0
            edition_id, edition_path, reports = promote_orphaned_flag_run(
                session, disagree=disagree)
            print(json.dumps({"edition_id": edition_id, "edition_path": edition_path}, indent=2))
    else:
        session, summary = run_queue_evidence_pass()
        print(json.dumps(summary, indent=2, sort_keys=True))
