#!/usr/bin/env python3
"""Deterministic V2-artifact importer -> V3 canonical evidence + judgments.

Implements P2.2 brief §3.3 import rules under the owner-ruled decisions §4.
Selection is by CONTENT, never by filename (P1.7 review R1 revert stands):
every JSON/JSONL/Markdown file under the trackers directory is scanned,
hashed, and content-classified into a disposition; the RED test
(``ge16-news-backfill-anything.json``) pins that no name ever gates an import.
Dispositions mirror evidence/P2/P2.1-artifact-classification.json:

  accepted-corpus     items carry judgment fields            -> evidence + judgments
  judged-batch        schema ge16.news-backfill-judge-batch.v1 + accepted
                                                       (the provenance backbone) -> judgments
  live-judged         schema ge16.news-judged.v1            -> evidence ONLY (P2.1 Finding 2)
  queue               unjudged items / pending values       -> evidence ONLY
  tracked-list        {"seen": [...]} strings               -> tracker-note evidence (P0.5 caveat)
  verdicts-log        compact verdict lines                 -> recorded, not imported (unbound)
  narrative-log       markdown logs                         -> recorded, not imported (no rows)

Judgment provenance classes:
  judged-batch           basis {batch_file, batch_file_sha256,
                               embedded_source_sha256, source_hash_semantics};
                       verifiable — the binding an auditor can check is
                       batch_file_sha256 (import-time hash of the batch file);
                       embedded_source_sha256 is the batch doc's self-reported
                       hash of the judged INPUT generation of
                       ge16-news-backfill-candidates.json, which was overwritten
                       in V2 (P2.1 Finding 2) and is kept verbatim for audit
  accepted-corpus-inline basis {batch_file=accepted corpus, sha256}, verifiable
  orphaned-flag          backfill flag whose batch is gone (matched by the V2
                         merge key: URL + 150-char title prefix) — verifiable
                         false + a real HEAD liveness probe on the evidence row

Re-running with unchanged sources adds ZERO rows: ids are deterministic,
shards are append-only, and evidence rows (including probe results) are
immutable after write.
"""
import argparse
import concurrent.futures
import datetime
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import edition as edition_module  # noqa: E402
import entity_candidates  # noqa: E402
import identity  # noqa: E402
from normalize_link import NORMALIZER_VERSION, normalize_link  # noqa: E402
import parse_verdicts  # noqa: E402

EVIDENCE_SCHEMA = "ge16.evidence.v1"
JUDGMENT_SCHEMA = "ge16.judgment.v1"
TRACKERS_DEFAULT = os.path.join("canonical", "research", "trackers")
P05_CAVEAT = "P0.5: seen-key \u2260 verified poll/candidate"
ORPHAN_NOTE = ("Backfill flag references a judged batch that no longer exists "
               "(P2.1 Finding 3); source_recovery liveness probe recorded on the "
               "evidence row; full re-judge is P2.7.")
ORPHAN_DISAGREE_NOTE = ("Backfill flag whose generating batch no longer exists (P2.1 Finding 3): "
                        "a surviving batch holds the same merge key but with different judgment "
                        "fields, so it cannot be the source of this inline judgment. The "
                        "surviving batch's own judgment row (origin judged-batch) provides the "
                        "verifiable provenance for this evidence; this row is retained for audit "
                        "as unverifiable. Full re-judge is P2.7.")
INLINE_NOTE = "Weekly-corpus merged record; judge identity not retained by the V2 commit path."
SOURCE_HASH_SEMANTICS = ("embedded hash refers to the judged input generation of "
                         "ge16-news-backfill-candidates.json at judge time; input "
                         "generation was overwritten in V2 (P2.1 Finding 2). "
                         "Verifiable binding is batch_file_sha256.")
PROBE_TIMEOUT_S = 10
PROBE_CONCURRENCY = 4
PROBE_USER_AGENT = "GE16-evidence-recovery-probe/1.0 (liveness check)"


def utcnow_iso():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def serialize_row(row):
    return json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def merge_key(item):
    """V2 --commit merge identity (ge16_news_backfill.py merge_key): URL plus
    the 150-char title prefix. Orphan attribution must use the same key the
    merge used, or provenance claims would not line up with merge reality."""
    return ((item.get("link") or "").strip().lower(),
            (item.get("title") or "").strip()[:150].lower())


# --------------------------------------------------------------- classification

def has_judgment_fields(items):
    return any(item.get("judged_at") or item.get("category") for item in items)


def classify_document(doc):
    """Content-only disposition. No filename input, by design (review R1)."""
    if not isinstance(doc, dict):
        return "unrecognized"
    schema = doc.get("schema")
    if schema == "ge16.news-backfill-judge-batch.v1":
        if isinstance(doc.get("accepted"), list):
            return "judged-batch"
        if isinstance(doc.get("items"), list):
            return "queue"  # unjudged judge workfile (in-flight)
    if schema == "ge16.news-judged.v1":
        return "live-judged"
    if isinstance(doc.get("seen"), list):
        return "tracked-list"
    if schema == "ge16.news-seen-pending.v1":
        return "queue"
    if schema:
        # Any other schema-bearing envelope (e.g. the feed snapshot, whose items
        # happen to carry judgment fields) is working-state: evidence-only.
        return "queue" if isinstance(doc.get("items"), list) else "unrecognized"
    if isinstance(doc.get("items"), list) and doc["items"] and has_judgment_fields(doc["items"]):
        return "accepted-corpus"  # schema-less judged-item corpus (P2.1 canonical-input-candidate)
    if isinstance(doc.get("items"), list):
        return "queue"
    return "unrecognized"


def classify_jsonl(path):
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            return "verdicts-log" if parse_verdicts.parse_compact_verdict(line) else "unrecognized"
    return "unrecognized"


def scan_inputs(trackers_dir, repo_root):
    """Every file under the trackers dir, sorted; hash + disposition each."""
    scanned = []
    for current, directories, names in os.walk(trackers_dir):
        directories.sort()
        for name in sorted(names, key=os.fsencode):
            abspath = os.path.join(current, name)
            entry = {"file": os.path.relpath(abspath, repo_root), "abspath": abspath,
                     "sha256": sha256_file(abspath), "disposition": None}
            if name.endswith(".md"):
                entry["disposition"] = "narrative-log"
            elif name.endswith(".json"):
                with open(abspath, encoding="utf-8") as handle:
                    entry["disposition"] = classify_document(json.load(handle))
            elif name.endswith(".jsonl"):
                entry["disposition"] = classify_jsonl(abspath)
            else:
                entry["disposition"] = "unrecognized-suffix"
            scanned.append(entry)
    return scanned


# ------------------------------------------------------------------ row stores

class EvidenceStore:
    """Append-only evidence registry (existing shards + this run's rows)."""

    def __init__(self, canonical_dir):
        self.directory = os.path.join(canonical_dir, "evidence")
        self.existing_ids = set()
        self.new_rows = []
        self.index = {}
        os.makedirs(self.directory, exist_ok=True)
        for name in sorted(os.listdir(self.directory)):
            if not (name.startswith("evidence-") and name.endswith(".jsonl")):
                continue
            with open(os.path.join(self.directory, name), encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        row = json.loads(line)
                        self.existing_ids.add(row["evidence_id"])
                        self.index[row["evidence_id"]] = row

    def has(self, evidence_id):
        return evidence_id in self.index

    def get(self, evidence_id):
        return self.index.get(evidence_id)

    def add(self, row):
        if self.has(row["evidence_id"]):
            return False
        self.index[row["evidence_id"]] = row
        self.new_rows.append(row)
        return True

    def all_rows(self):
        return list(self.index.values())

    def flush(self):
        shards = {}
        for row in self.new_rows:
            shards.setdefault(row["evidence_id"][2], []).append(serialize_row(row))
        for nibble, lines in sorted(shards.items()):
            with open(os.path.join(self.directory, f"evidence-{nibble}.jsonl"),
                      "a", encoding="utf-8") as handle:
                for line in lines:
                    handle.write(line + "\n")


class JudgmentStore:
    def __init__(self, canonical_dir):
        self.directory = os.path.join(canonical_dir, "judgments")
        self.existing_ids = set()
        self.new_rows = []
        self.duplicate_ids = 0
        self.index = {}
        os.makedirs(self.directory, exist_ok=True)
        for name in sorted(os.listdir(self.directory)):
            if not (name.startswith("judgment-") and name.endswith(".jsonl")):
                continue
            with open(os.path.join(self.directory, name), encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        row = json.loads(line)
                        self.existing_ids.add(row["judgment_id"])
                        self.index[row["judgment_id"]] = row

    def has(self, judgment_id):
        return judgment_id in self.index

    def add(self, row):
        if self.has(row["judgment_id"]):
            self.duplicate_ids += 1
            return False
        self.index[row["judgment_id"]] = row
        self.new_rows.append(row)
        return True

    def all_rows(self):
        return list(self.index.values())

    def flush(self):
        shards = {}
        for row in self.new_rows:
            shards.setdefault(row["judgment_id"][2], []).append(serialize_row(row))
        for nibble, lines in sorted(shards.items()):
            with open(os.path.join(self.directory, f"judgment-{nibble}.jsonl"),
                      "a", encoding="utf-8") as handle:
                for line in lines:
                    handle.write(line + "\n")


# ------------------------------------------------------------------- importer

def http_liveness_probe(url, timeout=PROBE_TIMEOUT_S):
    """One polite HEAD probe. Real results only: network failures come back as
    ok:false with the error string — nothing is ever fabricated."""
    result = {"http_status": None, "final_url": "", "ok": False, "error": None}
    request = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": PROBE_USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result["http_status"] = response.status
            result["final_url"] = response.url
            result["ok"] = 200 <= response.status < 400
    except urllib.error.HTTPError as error:
        result["http_status"] = error.code
        result["final_url"] = getattr(error, "url", "") or url
        result["ok"] = False
        result["error"] = f"HTTP {error.code}"
    except (urllib.error.URLError, OSError, ValueError) as error:
        result["final_url"] = url
        result["ok"] = False
        result["error"] = f"{type(error).__name__}: {error}"
    return result


def run_probes(probe_set, evidence_store, probe_function, clock=None):
    """HEAD probes, 4 concurrent, 10 s timeout; attach source_recovery to the
    NEW rows only (existing rows are immutable after write, so re-imports of
    unchanged sources never re-probe and never rewrite history)."""
    timestamp = clock or utcnow_iso
    results = []
    todo = sorted(eid for eid in probe_set
                  if eid in evidence_store.index
                  and "source_recovery" not in evidence_store.index[eid])
    if not todo:
        return results
    with concurrent.futures.ThreadPoolExecutor(max_workers=PROBE_CONCURRENCY) as pool:
        futures = {}
        for eid in todo:
            url = evidence_store.index[eid].get("payload", {}).get("link", "")
            futures[pool.submit(probe_function, url)] = (eid, url)
        for future in concurrent.futures.as_completed(futures):
            eid, url = futures[future]
            try:
                outcome = future.result()
            except Exception as error:  # a probe crash is a recorded failure, never fatal
                outcome = {"http_status": None, "final_url": url, "ok": False,
                           "error": f"{type(error).__name__}: {error}"}
            outcome = dict(outcome)
            outcome["checked_at"] = timestamp()
            evidence_store.index[eid]["source_recovery"] = outcome
            results.append({"evidence_id": eid, **outcome})
    return results


class Importer:
    def __init__(self, data_root, trackers_rel, probe_function, now=None):
        self.data_root = data_root
        self.repo_root = os.path.abspath(os.path.join(data_root, os.pardir))
        self.trackers_dir = os.path.join(data_root, trackers_rel)
        self.canonical_dir = os.path.join(data_root, "canonical")
        self.probe_function = probe_function
        self.now = now
        for sub in ("evidence", "judgments", "links", "events", "editions"):
            os.makedirs(os.path.join(self.canonical_dir, sub), exist_ok=True)
        self.evidence = EvidenceStore(self.canonical_dir)
        self.judgments = JudgmentStore(self.canonical_dir)
        self.counts = {
            "files_by_disposition": {},
            "evidence": {"new": 0, "existing_before": len(self.evidence.existing_ids),
                         "by_kind": {}, "by_source_class": {}, "already_present": {}},
            "judgments": {"new": 0, "existing_before": len(self.judgments.existing_ids),
                          "by_origin": {}, "by_verdict": {}, "by_verifiable": {}},
            "collisions": [],
            "linkless_items": 0,
            "probes": [],
            "skipped_rows": {},
        }

    # -- evidence bookkeeping -------------------------------------------------

    def record_evidence(self, kind, payload, entry, collected_at, source_class,
                        offset=None, notes=None):
        """Add an evidence row keyed by normalized link/seen-key.

        Returns the evidence_id when a NEW row was created. A same-class link
        collision is recorded as a dupe_of candidate (never silently dropped);
        a cross-class overlap (e.g. a batch item whose evidence came from the
        accepted corpus) only counts as already-present corroboration.
        """
        link_or_key = payload.get("link") if isinstance(payload, dict) else None
        key = link_or_key if link_or_key else payload.get("seen")
        if not key:
            self.counts["linkless_items"] += 1
            return None
        eid = identity.evidence_id(normalize_link(key))
        row = {
            "schema": EVIDENCE_SCHEMA,
            "evidence_id": eid,
            "kind": kind,
            "payload": payload,
            "source_ref": {"file": entry["file"], "sha256": entry["sha256"]},
            "collected_at": collected_at or "",
            "normalizer_v": NORMALIZER_VERSION,
        }
        if offset is not None:
            row["source_ref"]["offset"] = offset
        if notes:
            row["notes"] = notes
        row["_source_class"] = source_class  # in-memory bookkeeping; stripped before writing
        if self.evidence.add(row):
            self.counts["evidence"]["by_kind"][kind] = \
                self.counts["evidence"]["by_kind"].get(kind, 0) + 1
            self.counts["evidence"]["by_source_class"][source_class] = \
                self.counts["evidence"]["by_source_class"].get(source_class, 0) + 1
            return eid
        keeper = self.evidence.get(eid)
        keeper_class = keeper.get("_source_class")
        if keeper_class == source_class:
            self.counts["collisions"].append({
                "evidence_id": eid,
                "normalized_key": normalize_link(key),
                "source_class": source_class,
                "kept": {"file": keeper["source_ref"]["file"],
                         "offset": keeper["source_ref"]["offset"]},
                "duplicate": {"file": entry["file"], "offset": offset},
                "dupe_of": eid,
                "status": "proposed",
            })
        else:
            self.counts["evidence"]["already_present"][source_class] = \
                self.counts["evidence"]["already_present"].get(source_class, 0) + 1
        return None

    def load(self, entry):
        with open(entry["abspath"], encoding="utf-8") as handle:
            return json.load(handle)

    # -- phases ----------------------------------------------------------------

    def phase_accepted_evidence(self, scanned):
        stash = []
        for entry in [e for e in scanned if e["disposition"] == "accepted-corpus"]:
            doc = self.load(entry)
            for offset, item in enumerate(doc.get("items") or []):
                if not isinstance(item, dict) or not item.get("link"):
                    if isinstance(item, dict):
                        self.counts["linkless_items"] += 1
                    continue
                eid = self.record_evidence("news", item, entry, doc.get("generated_at", ""),
                                           "accepted-corpus", offset)
                if eid is None:
                    eid = identity.evidence_id(normalize_link(item["link"]))
                stash.append((item, entry, eid))
        return stash

    def phase_batch_judgments(self, scanned):
        batch_merge_keys = {}
        batch_judged_eids = set()
        accepted_rows = rejected_indexes = 0
        for entry in [e for e in scanned if e["disposition"] == "judged-batch"]:
            doc = self.load(entry)
            envelope = {
                "judged_at": doc.get("judged_at") or doc.get("collection_id") or "",
                "judged_by": doc.get("judged_by"),
                "judge_model": doc.get("judge_model"),
                "run_id": doc.get("collection_id"),
                "source_sha256": doc.get("source_sha256"),
            }
            rejected_indexes += len(doc.get("rejected_indexes") or [])
            for offset, item in enumerate(doc.get("accepted") or []):
                if not isinstance(item, dict) or not item.get("link"):
                    self.counts["linkless_items"] += 1
                    continue
                accepted_rows += 1
                eid = self.record_evidence("news", item, entry, doc.get("collection_id", ""),
                                           "judged-batch", offset)
                if eid is None:
                    eid = identity.evidence_id(normalize_link(item["link"]))
                batch_judged_eids.add(eid)
                batch_merge_keys.setdefault(merge_key(item), []).append((envelope, item))
                self.judgments.add({
                    "schema": JUDGMENT_SCHEMA,
                    "judgment_id": identity.judgment_id(eid, envelope["judged_at"], "accept"),
                    "evidence_id": eid,
                    "verdict": "accept",
                    "category": item.get("category", ""),
                    "blocs": item.get("blocs") or [],
                    "parties": item.get("parties") or [],
                    "seats": item.get("seats") or [],
                    "score": float(item.get("score", 0.6)),
                    "lang": item.get("lang"),
                    "judge": {"model": envelope["judge_model"], "by": envelope["judged_by"],
                              "run_id": envelope["run_id"]},
                    "judged_at": envelope["judged_at"],
                    "basis": {"origin": "judged-batch", "verifiable": True,
                              "batch_file": entry["file"],
                              "batch_file_sha256": entry["sha256"],
                              "embedded_source_sha256": envelope["source_sha256"],
                              "source_hash_semantics": SOURCE_HASH_SEMANTICS,
                              "verdict_line": None},
                })
        self.counts["judgments"]["batch_accepted_rows_seen"] = accepted_rows
        self.counts["judgments"]["batch_rejected_indexes_seen"] = rejected_indexes
        return batch_merge_keys, batch_judged_eids

    def phase_inline_judgments(self, stash, batch_merge_keys, batch_judged_eids):
        batch_backed = disagree = orphan_flags = no_fields = 0
        probe_set = set()
        for item, entry, eid in stash:
            if not item.get("judged_at"):
                no_fields += 1
                continue
            row = {
                "schema": JUDGMENT_SCHEMA,
                "judgment_id": identity.judgment_id(eid, item["judged_at"], "accept"),
                "evidence_id": eid,
                "verdict": "accept",
                "category": item.get("category", ""),
                "blocs": item.get("blocs") or [],
                "parties": item.get("parties") or [],
                "seats": item.get("seats") or [],
                "score": float(item.get("score", 0.6)),
                "lang": item.get("lang"),
                "judge": {"model": item.get("judge_model"), "by": item.get("judged_by"),
                          "run_id": item.get("collection_id")},
                "judged_at": item["judged_at"],
            }
            matches = batch_merge_keys.get(merge_key(item)) if item.get("backfill") else None
            if matches:
                agrees = any(
                    batch_item.get("category") == row["category"]
                    and batch_item.get("blocs") == row["blocs"]
                    and batch_item.get("parties") == row["parties"]
                    and batch_item.get("seats") == row["seats"]
                    and batch_item.get("lang") == row["lang"]
                    and float(batch_item.get("score", 0.6)) == row["score"]
                    for _, batch_item in matches)
                if agrees:
                    # verbatim merge: the surviving batch IS this judgment's source
                    batch_backed += 1
                    continue
                # same merge key but different fields: this inline judgment came
                # from a deleted batch; the surviving batch's own row (already
                # added in phase_batch_judgments) keeps the evidence verifiable,
                # so no probe is needed — only this row is marked unverifiable.
                disagree += 1
                row["basis"] = {"origin": "orphaned-flag", "verifiable": False,
                                "batch_file": None, "source_sha256": None, "verdict_line": None}
                row["confidence_note"] = ORPHAN_DISAGREE_NOTE
            elif item.get("backfill"):
                orphan_flags += 1
                probe_set.add(eid)
                row["basis"] = {"origin": "orphaned-flag", "verifiable": False,
                                "batch_file": None, "source_sha256": None, "verdict_line": None}
                row["confidence_note"] = ORPHAN_NOTE
            else:
                row["basis"] = {"origin": "accepted-corpus-inline", "verifiable": True,
                                "batch_file": entry["file"], "source_sha256": entry["sha256"],
                                "verdict_line": None}
                row["confidence_note"] = INLINE_NOTE
            self.judgments.add(row)
        # Probe only evidence whose provenance is genuinely broken: an orphaned
        # flag whose evidence already carries a surviving judged-batch judgment
        # (same link, different title slot) needs no recovery probe — consistent
        # with the disagree case above.
        probe_set -= batch_judged_eids
        self.counts["judgments"]["inline_batch_backed_skipped"] = batch_backed
        self.counts["judgments"]["inline_batch_backed_disagree"] = disagree
        self.counts["judgments"]["orphaned_flag_rows"] = orphan_flags + disagree
        self.counts["judgments"]["orphaned_flag_evidence_probed"] = len(probe_set)
        self.counts["judgments"]["accepted_items_without_judgment_fields"] = no_fields
        return probe_set

    def phase_queues(self, scanned):
        for entry in [e for e in scanned if e["disposition"] in ("live-judged", "queue")]:
            doc = self.load(entry)
            if entry["disposition"] == "live-judged":
                rows = doc.get("accepted") or []
            elif isinstance(doc.get("pending"), dict):
                rows = [v for v in doc["pending"].values() if isinstance(v, dict)]
            else:
                rows = doc.get("items") or []
            collected = doc.get("collection_id") or doc.get("generated_at") \
                or doc.get("updated_at") or ""
            overflow = doc.get("overflow")
            if isinstance(overflow, list) and overflow:
                self.counts["skipped_rows"][entry["file"]] = {
                    "reason": "overflow backlog beyond the collector's per-run cap; retained in "
                              "the source file, not queue rows (content-based scope: the queue "
                              "class reads 'items'/'pending', never a filename)",
                    "rows": len(overflow)}
            for offset, item in enumerate(rows):
                if not isinstance(item, dict):
                    continue
                self.record_evidence("news", item, entry, collected, entry["disposition"], offset)

    def phase_tracked_lists(self, scanned):
        for entry in [e for e in scanned if e["disposition"] == "tracked-list"]:
            doc = self.load(entry)
            collected = doc.get("generated_at") or doc.get("last_commit_at") or ""
            for offset, seen in enumerate(doc.get("seen") or []):
                if not isinstance(seen, str):
                    self.counts["skipped_rows"][entry["file"]] = {
                        "reason": "non-string seen entry", "rows":
                        self.counts["skipped_rows"].get(entry["file"], {}).get("rows", 0) + 1}
                    continue
                self.record_evidence("tracker-note", {"seen": seen}, entry, collected,
                                     "tracked-list", offset, notes=P05_CAVEAT)

    def phase_verdict_logs(self, scanned):
        parsed = 0
        for entry in [e for e in scanned if e["disposition"] == "verdicts-log"]:
            rows, bad = parse_verdicts.load_verdicts(entry["abspath"])
            parsed = len(rows)
            self.counts["skipped_rows"][entry["file"]] = {
                "reason": "compact verdict lines with no verifiable item binding (P2.1: the "
                          "verdict file's mtime predates the judged outputs built from verdict "
                          "input, so slots cannot be bound to items); parser + round-trip "
                          "tests live in parse_verdicts.py",
                "rows": len(rows), "bad_lines": bad}
        self.counts["verdict_log_lines_parsed"] = parsed

    def finalize_counts(self):
        counts = self.counts
        counts["evidence"]["new"] = len(self.evidence.new_rows)
        counts["judgments"]["new"] = len(self.judgments.new_rows)
        counts["judgments"]["duplicate_ids_merged"] = self.judgments.duplicate_ids
        for row in self.judgments.all_rows():
            origin = row["basis"]["origin"]
            counts["judgments"]["by_origin"][origin] = \
                counts["judgments"]["by_origin"].get(origin, 0) + 1
            counts["judgments"]["by_verdict"][row["verdict"]] = \
                counts["judgments"]["by_verdict"].get(row["verdict"], 0) + 1
            key = "true" if row["basis"]["verifiable"] else "false"
            counts["judgments"]["by_verifiable"][key] = \
                counts["judgments"]["by_verifiable"].get(key, 0) + 1
        # strip the bookkeeping field before anything is serialized
        for row in self.evidence.all_rows():
            row.pop("_source_class", None)

    def write_outputs(self, scanned):
        self.evidence.flush()
        self.judgments.flush()
        # Merge-stable dupe_of sidecar: reruns see no new same-class collisions
        # (every source row hits an existing evidence id), so the union with the
        # previous file rewrites identical bytes.
        dupe_path = os.path.join(self.canonical_dir, "evidence", "dupe-of-candidates.json")
        merged = {}
        if os.path.exists(dupe_path):
            with open(dupe_path, encoding="utf-8") as handle:
                for candidate in json.load(handle).get("candidates") or []:
                    key = (candidate["evidence_id"], candidate["duplicate"]["file"],
                           candidate["duplicate"]["offset"])
                    merged[key] = candidate
        for candidate in self.counts["collisions"]:
            key = (candidate["evidence_id"], candidate["duplicate"]["file"],
                   candidate["duplicate"]["offset"])
            merged.setdefault(key, candidate)
        with open(dupe_path, "w", encoding="utf-8") as handle:
            json.dump({"schema": "ge16.dupe-of-candidates.v1",
                       "note": "Rows whose normalized link matched an earlier evidence row in "
                               "the same import class. dupe_of link edges are decided by a "
                               "judgment (P2.2 brief §3.1), so these stay candidates and are "
                               "never dropped.",
                       "candidates": sorted(merged.values(),
                                            key=lambda c: (c["evidence_id"],
                                                           c["duplicate"]["file"],
                                                           c["duplicate"]["offset"] or 0))},
                      handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")

        seed = entity_candidates.load_seed(self.data_root)
        entity_candidates.seed_entities_registry(self.data_root, seed)
        candidate_rows, _, candidate_counts = entity_candidates.run_scan(
            self.data_root, evidence_rows=self.evidence.all_rows(),
            judgment_rows=self.judgments.all_rows())
        self.counts["entity_candidates"] = candidate_counts

        edition_inputs = [{"file": e["file"], "sha256": e["sha256"],
                           "disposition": e["disposition"]} for e in scanned]
        seed_path = os.path.join(self.data_root, entity_candidates.ENTITIES_REL,
                                 "seed-vocabulary.json")
        edition_inputs.append({"file": os.path.relpath(seed_path, self.repo_root),
                               "sha256": sha256_file(seed_path),
                               "disposition": "entity-seed-vocabulary"})
        # Edition row_counts snapshot the CANONICAL STATE after this run (not the
        # delta), so a no-op rerun's edition still describes the full corpus.
        kind_totals, origin_totals = {}, {}
        for row in self.evidence.all_rows():
            kind_totals[row["kind"]] = kind_totals.get(row["kind"], 0) + 1
        for row in self.judgments.all_rows():
            origin = row["basis"]["origin"]
            origin_totals[origin] = origin_totals.get(origin, 0) + 1
        row_counts = {
            "evidence_total": len(self.evidence.all_rows()),
            "evidence_by_kind": dict(sorted(kind_totals.items())),
            "judgments_total": len(self.judgments.all_rows()),
            "judgments_by_origin": dict(sorted(origin_totals.items())),
            "entity_candidates_total": len(candidate_rows),
        }
        edition_id, _ = edition_module.write_edition(
            os.path.join(self.canonical_dir, "editions"), edition_inputs, row_counts,
            note="P2.2 import run (evidence/judgment canonicalization of V2 tracker artifacts).",
            now=self.now)
        return edition_id


def import_all(data_root, trackers_rel=TRACKERS_DEFAULT, probe_function=http_liveness_probe,
               report_dir=None, dry_run=False, now=None, clock=None):
    """Run the full import. Returns the summary dict (also printed by main())."""
    importer = Importer(data_root, trackers_rel, probe_function, now=now)
    scanned = scan_inputs(importer.trackers_dir, importer.repo_root)
    for entry in scanned:
        importer.counts["files_by_disposition"][entry["disposition"]] = \
            importer.counts["files_by_disposition"].get(entry["disposition"], 0) + 1

    stash = importer.phase_accepted_evidence(scanned)
    batch_merge_keys, batch_judged_eids = importer.phase_batch_judgments(scanned)
    probe_set = importer.phase_inline_judgments(stash, batch_merge_keys, batch_judged_eids)
    importer.phase_queues(scanned)
    importer.phase_tracked_lists(scanned)
    importer.phase_verdict_logs(scanned)

    probe_results = []
    importer.counts["probes_planned"] = len(probe_set)
    if not dry_run:
        probe_results = run_probes(probe_set, importer.evidence, probe_function, clock=clock)
        importer.counts["probes"] = [{"evidence_id": r["evidence_id"], "ok": r["ok"],
                                      "http_status": r["http_status"], "error": r["error"]}
                                     for r in probe_results]
    importer.finalize_counts()

    if dry_run:
        return {"edition_id": None, "counts": importer.counts, "inputs": scanned, "dry_run": True}

    edition_id = importer.write_outputs(scanned)
    summary = {
        "edition_id": edition_id,
        "counts": importer.counts,
        "inputs": [{k: v for k, v in entry.items() if k != "abspath"} for entry in scanned],
        "dry_run": False,
    }
    if report_dir:
        manifest = dict(summary)
        manifest["generated_at"] = utcnow_iso()
        manifest["task"] = "P2.2 import"
        # R2 ambiguity note: the edition content_hashes have one entry more
        # than the tracker inputs scanned here (the entity seed vocabulary is
        # appended in write_outputs, not scanned from trackers).
        manifest["inputs_note"] = (
            f"inputs lists the {len(scanned)} source tracker files consumed; the "
            f"edition content_hashes additionally include the seed vocabulary "
            f"({len(scanned) + 1} total).")
        manifest["probe_detail"] = probe_results
        os.makedirs(report_dir, exist_ok=True)
        with open(os.path.join(report_dir, "P2.2-import-manifest.json"), "w",
                  encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    default_root = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir))
    parser.add_argument("--data-root", default=default_root,
                        help="V3 data/ directory (default: %(default)s)")
    parser.add_argument("--trackers-rel", default=TRACKERS_DEFAULT)
    parser.add_argument("--report-dir", default=None,
                        help="Directory for P2.2-import-manifest.json (repo evidence/).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Classify and count only; write nothing.")
    args = parser.parse_args(argv)
    summary = import_all(args.data_root, args.trackers_rel,
                         report_dir=args.report_dir, dry_run=args.dry_run)
    counts = summary["counts"]
    print(f"files scanned: {len(summary['inputs'])} "
          f"(dispositions: {counts['files_by_disposition']})")
    print(f"evidence: +{counts['evidence']['new']} new "
          f"({counts['evidence']['existing_before']} existed) by kind: "
          f"{counts['evidence']['by_kind']}")
    print(f"judgments: +{counts['judgments']['new']} new "
          f"({counts['judgments']['existing_before']} existed) by origin: "
          f"{counts['judgments']['by_origin']}")
    print(f"orphaned-flag judgments: {counts['judgments']['orphaned_flag_rows']}; "
          f"probes: {sum(1 for p in counts['probes'] if p['ok'])} ok / "
          f"{sum(1 for p in counts['probes'] if not p['ok'])} failed")
    print(f"collisions recorded: {len(counts['collisions'])}")
    print(f"edition: {summary['edition_id']}")
    return summary


if __name__ == "__main__":
    main()
