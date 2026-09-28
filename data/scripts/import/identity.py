"""Deterministic identity for canonical rows (P2.2 brief §3.1, decision 1).

  evidence_id  = "ev" + sha256(normalized_link)[:16]
  judgment_id  = "jg" + sha256(evidence_id + judged_at + verdict)[:12]

Timestamps never enter ID computation as anything other than the provenance
string the spec names (judged_at); the same bytes always produce the same ids,
which is what makes re-import add zero rows.
"""
import hashlib
import os
import sys

# This suite loads modules by file path (repo convention, see data/tests/conftest.py),
# so sibling modules need this directory importable directly.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from normalize_link import NORMALIZER_VERSION, normalize_link  # noqa: F401,E402  (re-export)


def evidence_id(normalized_link):
    digest = hashlib.sha256(normalized_link.encode("utf-8")).hexdigest()
    return "ev" + digest[:16]


def evidence_id_for_link(link):
    """Convenience: normalize then hash."""
    return evidence_id(normalize_link(link))


def judgment_id(evidence_id_value, judged_at, verdict):
    basis = f"{evidence_id_value}{judged_at}{verdict}"
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()
    return "jg" + digest[:12]


def candidate_id(candidate_string, proposed_type):
    basis = f"{candidate_string}:{proposed_type}"
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()
    return "cand-" + digest[:12]
