#!/usr/bin/env python3
"""Local-first vector store (P2.8 §1.3, design brief ruling §4.1).

One SQLite file, one table per collection, no ANN index (22k-row scale
doesn't need one — a pure-numpy cosine scan over the loaded matrix is fast
enough and keeps this module dependency-light). This is the ONE module that
owns vector storage; a future migration to a dedicated vector service only
has to change what's in here.

Each collection carries a metadata row: model name+version, the input field
list, the snapshot edition_id it was built from, row count, and built_at —
so a caller can always tell what produced the vectors it's querying.
"""
from __future__ import annotations

import json
import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path

try:
    import numpy as np
except ImportError:  # pragma: no cover - numpy is a project dependency
    np = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS collection_meta (
    collection    TEXT PRIMARY KEY,
    model_name    TEXT NOT NULL,
    model_version TEXT NOT NULL,
    input_fields  TEXT NOT NULL,
    edition_id    TEXT,
    row_count     INTEGER NOT NULL,
    dim           INTEGER NOT NULL,
    built_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vector_rows (
    collection  TEXT NOT NULL,
    row_id      TEXT NOT NULL,
    text        TEXT NOT NULL,
    vector      BLOB NOT NULL,
    PRIMARY KEY (collection, row_id)
);
"""


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pack(vector):
    return struct.pack(f"<{len(vector)}f", *[float(x) for x in vector])


def _unpack(blob):
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def _cosine(a, b):
    if np is not None:
        a, b = np.asarray(a, dtype="float64"), np.asarray(b, dtype="float64")
        denom = (np.linalg.norm(a) * np.linalg.norm(b)) or 1.0
        return float(np.dot(a, b) / denom)
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5 or 1.0
    norm_b = sum(y * y for y in b) ** 0.5 or 1.0
    return dot / (norm_a * norm_b)


class VectorStore:
    """One vectors.db file; collections are logical partitions of one table."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.path))
        self._connection.executescript(SCHEMA)
        self._connection.commit()

    def close(self):
        self._connection.close()

    def write_collection(self, collection, rows, embed_fn, model_name, model_version,
                         edition_id=None, input_fields=None, now=None):
        """``rows``: iterable of ``{"id": str, "text": str}``. ``embed_fn``:
        ``list[str] -> list[list[float]]`` (the sole embedding seam — tests
        pass a stub, never a real model or network call)."""
        rows = list(rows)
        texts = [row["text"] for row in rows]
        vectors = embed_fn(texts) if texts else []
        dim = len(vectors[0]) if vectors else 0
        cursor = self._connection.cursor()
        cursor.execute("DELETE FROM vector_rows WHERE collection = ?", (collection,))
        cursor.executemany(
            "INSERT INTO vector_rows (collection, row_id, text, vector) VALUES (?, ?, ?, ?)",
            [(collection, row["id"], row["text"], _pack(vector))
             for row, vector in zip(rows, vectors)])
        cursor.execute(
            "INSERT INTO collection_meta (collection, model_name, model_version, input_fields, "
            "edition_id, row_count, dim, built_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(collection) DO UPDATE SET model_name=excluded.model_name, "
            "model_version=excluded.model_version, input_fields=excluded.input_fields, "
            "edition_id=excluded.edition_id, row_count=excluded.row_count, dim=excluded.dim, "
            "built_at=excluded.built_at",
            (collection, model_name, model_version, json.dumps(input_fields or []),
             edition_id, len(rows), dim, now or _now_iso()))
        self._connection.commit()
        return len(rows)

    def collection_metadata(self, collection):
        cursor = self._connection.execute(
            "SELECT collection, model_name, model_version, input_fields, edition_id, row_count, "
            "dim, built_at FROM collection_meta WHERE collection = ?", (collection,))
        row = cursor.fetchone()
        if row is None:
            return None
        return {"collection": row[0], "model_name": row[1], "model_version": row[2],
                "input_fields": json.loads(row[3]), "edition_id": row[4], "row_count": row[5],
                "dim": row[6], "built_at": row[7]}

    def top_k(self, collection, query_vector, k=5):
        cursor = self._connection.execute(
            "SELECT row_id, text, vector FROM vector_rows WHERE collection = ?", (collection,))
        scored = []
        for row_id, text, blob in cursor.fetchall():
            vector = _unpack(blob)
            scored.append((_cosine(query_vector, vector), row_id, text))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [{"id": row_id, "text": text, "score": score} for score, row_id, text in scored[:k]]


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default=str(Path(__file__).resolve().parents[1] / "canonical" / "vectors" / "vectors.db"))
    parser.add_argument("--collection", required=True)
    args = parser.parse_args()
    store = VectorStore(args.path)
    meta = store.collection_metadata(args.collection)
    print(json.dumps(meta, indent=2))
