"""normalize_link.v1 — the versioned link normalizer behind evidence identity.

Spec (P2.2 brief §3.1, owner-ruled decision 1):
  1. One pass of HTML entity decoding (``&#038;`` / ``&amp;`` -> ``&``), so
     feed-encoded query separators split correctly.
  2. Non-URL input (tracker seen-keys such as ``Title|Source``) is returned
     unchanged after decoding — it still feeds the same sha256 identity space,
     just without URL parts to normalize.
  3. URLs: scheme and host lowercased, fragment dropped, tracking parameters
     (``utm_*``, ``fbclid``) removed from the query. Remaining non-tracking
     query parameters survive in their original order (article-id parameters
     must not be merged away); an emptied query disappears with its ``?``.
  4. Path is kept verbatim (no trailing-slash or percent-encoding munging).

Changing these rules means publishing ``normalize_link.v2`` and never
rewriting an existing evidence_id.
"""
import html
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

NORMALIZER_VERSION = "normalize_link.v1"
TRACKING_PREFIXES = ("utm_",)
TRACKING_NAMES = ("fbclid",)


def normalize_link(link):
    """Return the normalized form of ``link`` (pure function, no network)."""
    if not isinstance(link, str):
        raise TypeError(f"link must be str, got {type(link).__name__}")
    decoded = html.unescape(link)
    if "://" not in decoded:
        return decoded
    parts = urlsplit(decoded)
    kept = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if not name.lower().startswith(TRACKING_PREFIXES)
        and name.lower() not in TRACKING_NAMES
    ]
    query = urlencode(kept) if kept else ""
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, query, ""))
