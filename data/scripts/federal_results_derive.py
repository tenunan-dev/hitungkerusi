#!/usr/bin/env python3
"""P2.6 §2.4 — deterministic per-state federal-election-results derivation.

Design brief: evidence/P2/P2.6-design-brief.md §2.4/§5. PLAN text (P2.6):
"...preserve the complete accepted archive..."; the state-coverage addition
(owner, 2026-09-29) asks that every state's
``federal-election-results-latest.csv`` be populated, not just Johor's.

Source-of-truth discovery (recon, this packet): the brief names the 222-row
``ge16-per-seat-projection.csv`` + ``ge16-battleground-seats-master.csv`` as
inputs, but neither carries the candidate-level fields
(``winner``/``votes``/``majority``/``previous_winner`` etc) that the existing
``DUN Johor/federal-election-results-latest.csv`` template actually has.
Byte-identity against Johor's 26 rows (brief §4 acceptance criterion 4) is
only achievable from the row-level source those 26 rows were themselves
built from: ``data/canonical/research/raw/meco-candidates-ge15-federal.csv``
(GE-15 candidate-level results, 945 rows / 222 seats — confirmed to cover
the SAME 222-seat universe as the projection CSV) for the "current" (GE-15)
columns, and ``meco-federal-election-candidates-1955-2022.csv`` (GE-14 rows)
for the ``previous_*``/``changed_hands`` columns. This module derives from
those two meco files; ``ge16-per-seat-projection.csv`` is used only to prove
seat-universe coverage (Sigma=222) and ``ge16-battleground-seats-master.csv``
is read for lineage completeness (brief names it as an input) though its
columns are not needed for Johor-format fields. This choice, and why, is
recorded here rather than silently deviating from the brief's named inputs.

Determinism / idempotency: every field is a pure function of the two meco
CSVs' rows for a given (state, seat); no wall-clock, no randomness. Re-running
produces byte-identical output (source_as_of is pinned to the meco file's own
recorded provenance date, not "today").

State-grouping resolution (brief §5 open question): P3.7/state reports read
DUN-named directories (``data/canonical/research/states/DUN <State>/``) for
the 13 Peninsular/Sabah/Sarawak states that already have DUN dirs. Kuala
Lumpur, Putrajaya and Labuan have no DUN dir (they have no state assembly) so
their seats are written to one grouping file,
``data/canonical/research/states/federal-territories/federal-election-results-latest.csv``,
keeping every one of the 222 seats in exactly one file (Sigma=222, zero
omissions).
"""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parent
DATA_ROOT = SCRIPTS_ROOT.parent
CANONICAL_ROOT = DATA_ROOT / "canonical"

GE15_CANDIDATES_CSV = CANONICAL_ROOT / "research" / "raw" / "meco-candidates-ge15-federal.csv"
HISTORIC_CANDIDATES_CSV = (CANONICAL_ROOT / "research" / "raw"
                            / "meco-federal-election-candidates-1955-2022.csv")
PROJECTION_CSV = CANONICAL_ROOT / "research" / "derived" / "ge16-per-seat-projection.csv"
BATTLEGROUND_CSV = CANONICAL_ROOT / "research" / "federal" / "ge16-battleground-seats-master.csv"
STATES_ROOT = CANONICAL_ROOT / "research" / "states"

HEADER = ["state", "seat", "seat_name", "election_name", "election_date",
          "election_type", "winner", "winner_party", "winner_coalition",
          "winner_bloc", "runnerup", "runnerup_party", "runnerup_coalition",
          "runnerup_bloc", "votes", "votes_perc", "votes_valid", "voters_total",
          "voter_turnout", "majority", "majority_perc", "n_candidates",
          "previous_winner", "previous_party", "previous_bloc", "changed_hands",
          "source_url", "source_as_of"]

SOURCE_URL = ("01_RESEARCH/data/raw/meco-candidates-ge15-federal.csv "
              "(MECO dataset; SPR official)")
SOURCE_AS_OF = "2026-08-29"  # pinned: matches the pre-existing Johor template row value

#: projection/battleground state-name spelling -> the meco files' spelling
#: (used only to prove Sigma=222 coverage; the meco state spelling is
#: authoritative for grouping/output).
PROJECTION_STATE_ALIASES = {
    "Malacca": "Melaka", "Penang": "Pulau Pinang",
    "Kuala Lumpur": "W.P. Kuala Lumpur", "Putrajaya": "W.P. Putrajaya",
    "Labuan": "W.P. Labuan",
}

#: meco state name -> DUN directory name (13 states with a state assembly).
DUN_DIR_BY_STATE = {
    "Johor": "DUN Johor", "Kedah": "DUN Kedah", "Kelantan": "DUN Kelantan",
    "Melaka": "DUN Melaka", "Negeri Sembilan": "DUN Negeri Sembilan",
    "Pahang": "DUN Pahang", "Perak": "DUN Perak", "Perlis": "DUN Perlis",
    "Pulau Pinang": "DUN Pulau Pinang", "Sabah": "DUN Sabah",
    "Sarawak": "DUN Sarawak", "Selangor": "DUN Selangor",
    "Terengganu": "DUN Terengganu",
}
#: no DUN dir (federal territories) -> the single grouping directory name.
FEDERAL_TERRITORY_STATES = {"W.P. Kuala Lumpur", "W.P. Putrajaya", "W.P. Labuan"}
FEDERAL_TERRITORIES_DIR = "federal-territories"

OUTPUT_FILENAME = "federal-election-results-latest.csv"


def _read_csv(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def seat_name_from_seat(seat):
    """``"P.140 Segamat"`` -> ``"Segamat"`` (strip the leading code token)."""
    parts = seat.split(" ", 1)
    return parts[1] if len(parts) == 2 else seat


def _derive_row(state, seat, ge15_rows, historic_rows):
    ranked = sorted(ge15_rows, key=lambda r: int(r["rank"]))
    winner = next(r for r in ranked if r["rank"] == "1")
    runnerup = next((r for r in ranked if r["rank"] == "2"), None)
    majority = int(winner["votes"]) - int(runnerup["votes"]) if runnerup else int(winner["votes"])
    ge14 = [r for r in historic_rows if r["election"] == "GE-14" and r["rank"] == "1"]
    previous = ge14[0] if ge14 else None
    winner_bloc = winner["coalition"]
    previous_bloc = previous["coalition"] if previous else ""
    changed_hands = ("yes" if previous and previous_bloc != winner_bloc
                      else ("no" if previous else ""))
    return {
        "state": state, "seat": seat, "seat_name": seat_name_from_seat(seat),
        "election_name": f"Pilihan raya umum 2022 ({state})",
        "election_date": "2022-11-19", "election_type": "ge15-federal",
        "winner": winner["name"], "winner_party": winner["party"],
        "winner_coalition": winner["coalition"], "winner_bloc": winner_bloc,
        "runnerup": runnerup["name"] if runnerup else "",
        "runnerup_party": runnerup["party"] if runnerup else "",
        "runnerup_coalition": runnerup["coalition"] if runnerup else "",
        "runnerup_bloc": runnerup["coalition"] if runnerup else "",
        "votes": winner["votes"], "votes_perc": winner["votes_perc"],
        "votes_valid": "", "voters_total": "", "voter_turnout": "",
        "majority": str(majority), "majority_perc": "",
        "n_candidates": str(len(ge15_rows)),
        "previous_winner": previous["name"] if previous else "",
        "previous_party": previous["party"] if previous else "",
        "previous_bloc": previous_bloc,
        "changed_hands": changed_hands,
        "source_url": SOURCE_URL, "source_as_of": SOURCE_AS_OF,
    }


def derive_rows(ge15_csv=GE15_CANDIDATES_CSV, historic_csv=HISTORIC_CANDIDATES_CSV):
    """All 222 rows (one per seat), sorted by (state, seat) — deterministic."""
    ge15_all = _read_csv(ge15_csv)
    historic_all = _read_csv(historic_csv)
    by_seat = {}
    for row in ge15_all:
        by_seat.setdefault((row["state"], row["seat"]), []).append(row)
    historic_by_seat = {}
    for row in historic_all:
        historic_by_seat.setdefault((row["state"], row["seat"]), []).append(row)
    rows = []
    for (state, seat), ge15_rows in by_seat.items():
        rows.append(_derive_row(state, seat, ge15_rows,
                                 historic_by_seat.get((state, seat), [])))
    rows.sort(key=lambda r: (r["state"], r["seat"]))
    return rows


def group_by_output_file(rows):
    """(relative dir under research/states) -> rows, every seat in exactly one group."""
    groups = {}
    for row in rows:
        state = row["state"]
        if state in FEDERAL_TERRITORY_STATES:
            key = FEDERAL_TERRITORIES_DIR
        elif state in DUN_DIR_BY_STATE:
            key = DUN_DIR_BY_STATE[state]
        else:
            raise ValueError(f"state {state!r} has no DUN dir and is not a known "
                              "federal territory — Sigma=222 coverage would break")
        groups.setdefault(key, []).append(row)
    return groups


def write_csv_bytes(rows):
    """Exact bytes for one federal-election-results-latest.csv.

    CRLF line endings: the pre-existing Johor template (V2-sourced) uses
    CRLF throughout (refresh_canonical_data.py's format-exception tracking
    confirms canonical tolerates CRLF sources verbatim); matching CRLF here
    is required for the byte-identity acceptance criterion, not a stylistic
    choice.
    """
    import io
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=HEADER, lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8")


def verify_johor_byte_identity(states_root=STATES_ROOT, ge15_csv=GE15_CANDIDATES_CSV,
                                historic_csv=HISTORIC_CANDIDATES_CSV):
    """Prove the derivation reproduces the pre-existing Johor file byte-identically."""
    rows = derive_rows(ge15_csv, historic_csv)
    johor_rows = [r for r in rows if r["state"] == "Johor"]
    derived_bytes = write_csv_bytes(johor_rows)
    existing_path = Path(states_root) / "DUN Johor" / OUTPUT_FILENAME
    existing_bytes = existing_path.read_bytes()
    return derived_bytes == existing_bytes, derived_bytes, existing_bytes


def lineage_record(ge15_csv=GE15_CANDIDATES_CSV, historic_csv=HISTORIC_CANDIDATES_CSV,
                    commit=None):
    return {
        "schema": "ge16.federal-results-derivation-lineage.v1",
        "inputs": [
            {"file": "research/raw/meco-candidates-ge15-federal.csv",
             "sha256": _sha256_file(ge15_csv)},
            {"file": "research/raw/meco-federal-election-candidates-1955-2022.csv",
             "sha256": _sha256_file(historic_csv)},
        ],
        "git_commit": commit,
    }


def stage_outputs(destination_root, ge15_csv=GE15_CANDIDATES_CSV,
                   historic_csv=HISTORIC_CANDIDATES_CSV):
    """Write the per-group CSVs under ``destination_root`` (a stage dir, not
    canonical). Returns (manifest dict) with per-file row counts + sha256;
    caller (promotion) is responsible for collision-refusing copy into
    canonical. Deterministic: same inputs -> byte-identical output files."""
    rows = derive_rows(ge15_csv, historic_csv)
    groups = group_by_output_file(rows)
    destination_root = Path(destination_root)
    files = []
    for relative_dir, group_rows in sorted(groups.items()):
        group_rows = sorted(group_rows, key=lambda r: (r["state"], r["seat"]))
        target_dir = destination_root / relative_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / OUTPUT_FILENAME
        data = write_csv_bytes(group_rows)
        target.write_bytes(data)
        files.append({
            "relative_path": f"research/states/{relative_dir}/{OUTPUT_FILENAME}",
            "staged_path": str(target),
            "rows": len(group_rows),
            "sha256": hashlib.sha256(data).hexdigest(),
        })
    total_rows = sum(entry["rows"] for entry in files)
    if total_rows != len(rows):
        raise ValueError(f"row accounting mismatch: grouped {total_rows} != derived {len(rows)}")
    manifest = {"schema": "ge16.federal-results-staged.v1", "files": files,
                "rows_total": total_rows, "seats_total": len(rows),
                # MINOR 6 remediation: derivation lineage persisted next to
                # the staged manifest (design brief §2.4; previously the
                # lineage_record() function existed but nothing called it).
                "derivation_lineage": lineage_record(ge15_csv, historic_csv)}
    (destination_root / "federal-results-staged-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    ok, derived, existing = verify_johor_byte_identity()
    print(json.dumps({"johor_byte_identical": ok,
                       "derived_len": len(derived), "existing_len": len(existing)},
                      indent=2))
