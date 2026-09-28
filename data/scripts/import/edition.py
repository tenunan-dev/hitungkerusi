"""ge16.edition.v1 manifest writer — one immutable snapshot per import run.

Owner-ruled cadence (P2.2 brief decision 4): every import run writes
``data/canonical/editions/edition-<UTCts>.json`` recording every input file
path + sha256, row counts, and the prior edition id. A deterministic re-import
leaves the inputs byte-identical, so the edition chain documents repetition
instead of duplicating rows (P2.10 test 1).
"""
import datetime
import json
import os
import time

EDITION_SCHEMA = "ge16.edition.v1"


def utc_timestamps(now=None):
    """(edition_id, created_at) — compact UTC id, full ISO created_at."""
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    moment = moment.replace(microsecond=0)
    return moment.strftime("%Y%m%dT%H%M%SZ"), moment.isoformat()


def prior_edition_id(editions_dir):
    """Latest edition id already on disk (sorted), or None for the first run."""
    if not os.path.isdir(editions_dir):
        return None
    ids = sorted(name[len("edition-"):-len(".json")]
                 for name in os.listdir(editions_dir)
                 if name.startswith("edition-") and name.endswith(".json"))
    return ids[-1] if ids else None


def write_edition(editions_dir, inputs, row_counts, note="", prior=None, now=None):
    """Write one edition manifest; returns (edition_id, path).

    ``inputs``: list of {file, sha256, disposition}. ``prior`` overrides the
    on-disk lookup (used by tests and chained runs). When ``now`` is pinned and
    the id already exists (two runs inside one second), the write fails loudly
    instead of silently overwriting; unpinned runs wait for the next second.
    """
    os.makedirs(editions_dir, exist_ok=True)
    while True:
        edition_id, created_at = utc_timestamps(now)
        if prior is None:
            prior = prior_edition_id(editions_dir)
        manifest = {
            "schema": EDITION_SCHEMA,
            "edition_id": edition_id,
            "created_at": created_at,
            "content_hashes": {entry["file"]: entry["sha256"] for entry in inputs},
            "lineage": {"prior_edition": prior, "inputs": inputs},
            "row_counts": row_counts,
            "note": note,
        }
        path = os.path.join(editions_dir, f"edition-{edition_id}.json")
        if not os.path.exists(path):
            break
        if now is not None:
            raise ValueError(f"edition already exists: {path} (pinned clock collision)")
        time.sleep(0.05)
        now = None
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
    return edition_id, path
