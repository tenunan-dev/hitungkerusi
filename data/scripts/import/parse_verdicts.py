"""Compact verdict-line parser — ported from V2 ``ge16_news_backfill.py``
``_compact_verdict`` / ``load_verdicts`` (read in the V2 tree, byte-for-byte
semantics preserved; V2 read-only).

Line format::

    batch:index:accept[:category[:blocs[:parties[:seats[:lang[:score]]]]]]

``'-'`` is an empty field, ``'+'`` joins list members, a bare score is tenths
(``7`` -> 0.7). ``accept`` carries ``1``/``0``. A wrong field count or
non-numeric batch/index is a judge typo and returns None — never a silent
re-slot. This module is the only parser; round-trip tests pin it
(P2.2 brief §3.3 rule 6).
"""
import json


def parse_compact_verdict(line):
    """Port of V2 ``_compact_verdict`` (L518-537). Returns dict or None."""
    parts = line.split(":")
    if len(parts) not in (3, 9) or not parts[0].strip().isdigit() \
            or not parts[1].strip().isdigit():
        return None  # a wrong field count is a judge typo, never a silent re-slot
    parts += [""] * (9 - len(parts))
    blocs, parties, seats = ([token for token in (field or "").split("+")
                              if token and token != "-"] for field in parts[4:7])
    score = parts[8].strip()
    try:
        score = float(score) if "." in score else (int(score) / 10.0 if score else 0.6)
    except ValueError:
        score = 0.6
    return {"b": int(parts[0]), "i": int(parts[1]), "a": parts[2].strip() == "1",
            "c": parts[3].strip(), "g": blocs, "p": parties, "s": seats,
            "l": parts[7].strip() or "en", "score": score}


def emit_compact_verdict(row):
    """Inverse of ``parse_compact_verdict``: dict -> a line that re-parses to
    the identical dict. Note the V2 parser keeps a literal ``'-'`` category as
    text (only list fields treat ``'-'`` as empty), so an empty category is
    emitted as an empty field, never as ``'-'``. The score re-emits as tenths
    when exact (``0.7`` -> ``"7"``, ``1.0`` -> ``"10"``), else as a decimal.
    """
    def joined(values):
        return "+".join(values) if values else "-"

    score = row["score"]
    tenths = round(score * 10)
    score_text = str(int(tenths)) if abs(tenths / 10.0 - score) < 1e-9 else repr(float(score))
    return ":".join([
        str(int(row["b"])), str(int(row["i"])), "1" if row["a"] else "0",
        row.get("c", ""), joined(row.get("g", [])), joined(row.get("p", [])),
        joined(row.get("s", [])), row.get("l") or "en", score_text,
    ])


def load_verdicts(path):
    """Port of V2 ``load_verdicts``: file -> {(batch, index): row}; bad lines counted.

    Comments (``#``), blank lines and full-JSON lines are handled the same way
    the V2 collector did: JSON lines are parsed as rows, unparsable non-compact
    lines count as bad.
    """
    rows, bad = {}, 0
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                row = json.loads(line)
            except ValueError:
                row = parse_compact_verdict(line)
            if not isinstance(row, dict) or "b" not in row or "i" not in row:
                bad += 1
                continue
            rows[(int(row["b"]), int(row["i"]))] = row
    return rows, bad
