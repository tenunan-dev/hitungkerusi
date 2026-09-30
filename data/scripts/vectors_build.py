#!/usr/bin/env python3
"""Build vector collections from the accepted V3 snapshot (P2.8 §1.3).

What embeds (design brief ruling §4.2): accepted news title+summary, event
descriptions, dossier notes. NOT tracker logs/seen-markers — a tracker note
payload is bookkeeping ({'seen': ...}), never embedded.

Embedding model: the existing local ONNX model under ``models/model_cache``
(qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q), served via
onnxruntime + the local ``tokenizers`` package — no network call, no model
download at build or test time (the model cache is gitignored and verified
out-of-band by ``models/verify_model_cache.py``). If the local model cannot
serve embeddings (wrong architecture, cache missing), this module raises
rather than silently falling back to a network API (local-first rule); a
caller wanting a stub for tests passes its own ``embed_fn``.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parent
CANONICAL_ROOT = SCRIPTS_ROOT.parent / "canonical"
MODELS_ROOT = SCRIPTS_ROOT.parents[1] / "models"
MODEL_SNAPSHOT = (MODELS_ROOT / "model_cache" /
                  "models--qdrant--paraphrase-multilingual-MiniLM-L12-v2-onnx-Q" / "snapshots" /
                  "faf4aa4225822f3bc6376869cb1164e8e3feedd0")
MODEL_NAME = "qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q"
MODEL_VERSION = "faf4aa4225822f3bc6376869cb1164e8e3feedd0"

import vector_store as VS  # noqa: E402


def _load_jsonl_dir(directory, prefix):
    rows = []
    for name in sorted(os.listdir(directory)):
        if not (name.startswith(prefix) and name.endswith(".jsonl")):
            continue
        with open(directory / name, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows


def local_onnx_embed_fn(snapshot_dir=MODEL_SNAPSHOT, batch_size=32):
    """Deterministic (temperature-0; no sampling) local embedder: mean-pooled,
    L2-normalized ``last_hidden_state`` over the attention mask — the
    standard sentence-transformers recipe for this model family."""
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer

    onnx_path = snapshot_dir / "model_optimized.onnx"
    tokenizer_path = snapshot_dir / "tokenizer.json"
    if not onnx_path.is_file() or not tokenizer_path.is_file():
        raise RuntimeError(f"local ONNX model not available at {snapshot_dir}; "
                           "run models/verify_model_cache.py")
    session = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
    tokenizer = Tokenizer.from_file(str(tokenizer_path))
    tokenizer.enable_padding()
    tokenizer.enable_truncation(max_length=256)

    def embed(texts):
        if not texts:
            return []
        vectors = []
        for start in range(0, len(texts), batch_size):
            batch = texts[start:start + batch_size]
            encodings = tokenizer.encode_batch(batch)
            max_len = max(len(e.ids) for e in encodings)
            input_ids = np.zeros((len(batch), max_len), dtype="int64")
            attention_mask = np.zeros((len(batch), max_len), dtype="int64")
            token_type_ids = np.zeros((len(batch), max_len), dtype="int64")
            for i, encoding in enumerate(encodings):
                n = len(encoding.ids)
                input_ids[i, :n] = encoding.ids
                attention_mask[i, :n] = encoding.attention_mask
            outputs = session.run(["last_hidden_state"], {
                "input_ids": input_ids, "attention_mask": attention_mask,
                "token_type_ids": token_type_ids})[0]
            mask = attention_mask[:, :, None].astype("float64")
            summed = (outputs.astype("float64") * mask).sum(axis=1)
            counts = np.clip(mask.sum(axis=1), 1e-9, None)
            pooled = summed / counts
            norms = np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-9, None)
            normalized = pooled / norms
            vectors.extend(normalized.tolist())
        return vectors

    return embed


def _news_rows(canonical_root):
    evidence_rows = _load_jsonl_dir(Path(canonical_root) / "evidence", "evidence-")
    judgment_rows = _load_jsonl_dir(Path(canonical_root) / "judgments", "judgment-")
    accepted_ids = {row["evidence_id"] for row in judgment_rows if row["verdict"] == "accept"}
    rows = []
    for row in evidence_rows:
        if row["kind"] != "news" or row["evidence_id"] not in accepted_ids:
            continue
        payload = row.get("payload", {})
        title = payload.get("title") or ""
        # P2.8 R1 MINOR-1: the accepted payload HAS no summary field today
        # (title/query/category/score...), but if a desc lands via a future
        # collector revision, embed it rather than duplicating the title.
        summary = payload.get("desc") or ""
        text = " ".join(part for part in (title, summary) if part).strip()
        if text:
            rows.append({"id": row["evidence_id"], "text": text})
    rows.sort(key=lambda row: row["id"])
    return rows


def _event_description_rows(events_db_path):
    import sqlite3
    if not Path(events_db_path).is_file():
        return []
    connection = sqlite3.connect(str(events_db_path))
    try:
        cursor = connection.execute("SELECT event_id, title, detail FROM events ORDER BY event_id")
        rows = []
        for event_id, title, detail in cursor.fetchall():
            text = (title or "") + " " + (detail or "")
            text = text.strip()
            if text:
                rows.append({"id": event_id, "text": text})
        return rows
    finally:
        connection.close()


def _dossier_note_rows(events_db_path):
    import sqlite3
    if not Path(events_db_path).is_file():
        return []
    connection = sqlite3.connect(str(events_db_path))
    try:
        cursor = connection.execute("SELECT note_id, body FROM dossier_notes ORDER BY note_id")
        return [{"id": f"note-{note_id}", "text": body} for note_id, body in cursor.fetchall() if body]
    finally:
        connection.close()


def build_vectors(canonical_root=CANONICAL_ROOT, store_path=None, embed_fn=None,
                  model_name=MODEL_NAME, model_version=MODEL_VERSION, edition_id=None):
    canonical_root = Path(canonical_root)
    store_path = Path(store_path) if store_path else (canonical_root / "vectors" / "vectors.db")
    embed_fn = embed_fn or local_onnx_embed_fn()
    events_db_path = canonical_root / "events" / "ge16-events.db"

    store = VS.VectorStore(store_path)
    try:
        stats = {}
        collections = {
            "news": (_news_rows(canonical_root), ["payload.title"]),
            "events": (_event_description_rows(events_db_path), ["events.title", "events.detail"]),
            "dossier_notes": (_dossier_note_rows(events_db_path), ["dossier_notes.body"]),
        }
        for name, (rows, fields) in collections.items():
            count = store.write_collection(name, rows, embed_fn, model_name, model_version,
                                           edition_id=edition_id, input_fields=fields)
            stats[name] = count
        return stats
    finally:
        store.close()


def _load_sibling(filename, module_name):
    import importlib.util
    path = SCRIPTS_ROOT / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _count_jsonl(directory, prefix):
    total = 0
    directory = Path(directory)
    if not directory.is_dir():
        return 0
    for name in sorted(os.listdir(directory)):
        if name.startswith(prefix) and name.endswith(".jsonl"):
            with open(directory / name, encoding="utf-8") as handle:
                total += sum(1 for line in handle if line.strip())
    return total


def promote_vectors(canonical_root=CANONICAL_ROOT, data_root=None, now=None, embed_fn=None):
    """Stage a vectors rebuild into a run dir and promote via the existing
    refresh promotion + gate layer."""
    import work_paths
    refresh = _load_sibling("refresh_canonical_data.py", "p28_vectors_refresh")

    canonical_root = Path(canonical_root)
    run = work_paths.new_run(label="vectors-rebuild", data_root=data_root, now=now)
    edition_id = refresh._edition_module().prior_edition_id(str(canonical_root / "editions")) or "unknown"
    staged = run.root / "vectors" / "vectors.db"
    stats = build_vectors(canonical_root=canonical_root, store_path=staged, embed_fn=embed_fn,
                          edition_id=edition_id)

    target = canonical_root / "vectors" / "vectors.db"
    digest = refresh._sha256_file(staged)
    if target.is_file() and refresh._sha256_file(target) == digest:
        return None, None, [], stats
    target.parent.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copy2(staged, target)
    promoted = [{"file": "vectors/vectors.db", "sha256": digest,
                "disposition": "vectors-rebuild-promotion"}]
    # P2.8 R1 MAJOR-1 remediation: publish corpus totals alongside derived
    # totals so the carry-forward baseline checks this edition.
    evidence_total = _count_jsonl(canonical_root / "evidence", "evidence-")
    judgments_total = _count_jsonl(canonical_root / "judgments", "judgment-")
    extra = {f"vectors_{name}_total": count for name, count in stats.items()}
    extra["evidence_total"] = evidence_total
    extra["judgments_total"] = judgments_total
    new_edition_id, edition_path = refresh.write_promotion_edition(
        run, promoted, unchanged=[], canonical_root=canonical_root, now=now,
        extra_row_counts=extra,
        note="P2.8 vector collections rebuild: %s." % ", ".join(
            f"{name}={count}" for name, count in stats.items()))
    reports = refresh.post_promotion_gate(run, new_edition_id, canonical_root)
    return new_edition_id, edition_path, reports, stats


if __name__ == "__main__":
    # P2.8 R1 MINOR-6: CLI stages into a run dir and promotes through the
    # refresh promotion + gate — never writes canonical directly.
    print(json.dumps(promote_vectors(), indent=2, default=str))
