#!/usr/bin/env python3
"""Source readers + classification for the GE16 events database.

Every reader returns Record objects (one dated fact each), ClaimReview objects
(baseline assertions the sweep checked) and Note objects (free-text context that
must not be dressed up as an event). Nothing is written here — build_events_db.py
does the entity resolution, dedupe and SQLite write.

Sources
-------
ge16-sweep-partA-federal.md   markdown dossier: bullets, 4 tables, corrections, unknowns
ge16-sweep-partB-states.json  per-state vacancies/byelections/composition/MB changes/polls
ge16-sweep-partC-polls-macro.json  national polls, approval, leader preference, DOSM/BNM/FX
party-updates-*.json          cron party facts (dated history lines + source URLs)
personnel-updates-*.json      cron personnel role changes (typed, dated, with notes)
polls-update-*.json           cron poll releases with numeric result payloads

Classification rules are deterministic and first-match-wins (TYPE_RULES), so a
rebuild of the same dossier always yields the same event types.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:  # package import (python -m tools.events.build_events_db)
    from .entity_index import URL_RE, mask_urls, slug
except ImportError:  # direct script execution (python tools/events/build_events_db.py)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from entity_index import URL_RE, mask_urls, slug

WINDOW_START = "2026-01-01"
WINDOW_END = "2026-09-24"

MONTH_NUM = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_M = r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"

# Ordered longest/most-specific first: a single left-to-right scan then yields
# non-overlapping hits with the right precedence at each position.
_DATE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(rf"\b(20\d\d)-(\d\d)-(\d\d)\s*(?:to|until|–|—|-)\s*(20\d\d)-(\d\d)-(\d\d)"), "iso_range"),
    (re.compile(rf"\b(20\d\d)-(\d\d)-(\d\d)\b"), "iso_day"),
    (re.compile(rf"\b(\d{{1,2}})\s+{_M}[a-z]*\.?\s*[–—-]\s*(\d{{1,2}})\s+{_M}[a-z]*\.?\s+(20\d\d)"), "d_dmy_dmy"),
    (re.compile(rf"\b(\d{{1,2}})\s*[–—-]\s*(\d{{1,2}})\s+{_M}[a-z]*\.?\s+(20\d\d)"), "dd_my"),
    (re.compile(rf"\b(\d{{1,2}})\s+{_M}[a-z]*\.?\s+(20\d\d)"), "d_my"),
    (re.compile(rf"\b{_M}[a-z]*\.?\s*[–—-]\s*{_M}[a-z]*\.?\s+(20\d\d)"), "m_m_y"),
    (re.compile(rf"\b{_M}[a-z]*\.?\s+(20\d\d)"), "m_y"),
    (re.compile(r"\bQ([1-4])\s*(20\d\d)\b"), "q_y"),
    (re.compile(r"\b(20\d\d)\s*-?\s*Q([1-4])\b"), "y_q"),
    (re.compile(r"\b(20(?:2[5-8]))\b"), "y"),
]


@dataclass
class DateHit:
    start: int
    iso: str
    iso_end: str | None
    precision: str


def extract_dates(text: str) -> list[DateHit]:
    """All date expressions in `text`, in order of first appearance."""
    consumed: list[tuple[int, int]] = []
    hits: list[DateHit] = []
    for pattern, kind in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            span = match.span()
            if any(span[0] < end and start < span[1] for start, end in consumed):
                continue
            groups = match.groups()
            try:
                hit = _to_hit(kind, groups, span[0])
            except (KeyError, ValueError):
                continue
            if hit is None:
                continue
            consumed.append(span)
            hits.append(hit)
    return sorted(hits, key=lambda h: h.start)


def _month(token: str) -> int:
    return MONTH_NUM[token[:3].lower()]


def _to_hit(kind: str, g: tuple, start: int) -> DateHit | None:
    if kind == "iso_range":
        return DateHit(start, f"{g[0]}-{g[1]}-{g[2]}", f"{g[3]}-{g[4]}-{g[5]}", "day")
    if kind == "iso_day":
        return DateHit(start, f"{g[0]}-{g[1]}-{g[2]}", None, "day")
    if kind == "d_dmy_dmy":
        year = int(g[4])
        return DateHit(start, f"{year:04d}-{_month(g[1]):02d}-{int(g[0]):02d}",
                       f"{year:04d}-{_month(g[3]):02d}-{int(g[2]):02d}", "day")
    if kind == "dd_my":
        return DateHit(start, f"{int(g[3]):04d}-{_month(g[2]):02d}-{int(g[0]):02d}",
                       f"{int(g[3]):04d}-{_month(g[2]):02d}-{int(g[1]):02d}", "day")
    if kind == "d_my":
        return DateHit(start, f"{int(g[2]):04d}-{_month(g[1]):02d}-{int(g[0]):02d}", None, "day")
    if kind == "m_m_y":
        return DateHit(start, f"{int(g[2]):04d}-{_month(g[0]):02d}-01",
                       f"{int(g[2]):04d}-{_month(g[1]):02d}-01", "month")
    if kind == "m_y":
        return DateHit(start, f"{int(g[1]):04d}-{_month(g[0]):02d}-01", None, "month")
    if kind == "q_y":
        quarter, year = int(g[0]), int(g[1])
        return DateHit(start, f"{year:04d}-{(quarter - 1) * 3 + 1:02d}-01", None, "quarter")
    if kind == "y_q":
        year, quarter = int(g[0]), int(g[1])
        return DateHit(start, f"{year:04d}-{(quarter - 1) * 3 + 1:02d}-01", None, "quarter")
    if kind == "y":
        return DateHit(start, f"{int(g[0]):04d}-01-01", None, "year")
    return None


def primary_date(text: str) -> tuple[str | None, str | None, str, list[str]]:
    """(iso, iso_end, precision, hit-lineage) for the date that names the event.

    The first in-window hit wins; otherwise the first hit at all. A date inside a
    bare year list ("MP since 1999") is never picked because year-only matches are
    restricted to 2025-2028.
    """
    hits = extract_dates(text)
    if not hits:
        return None, None, "unknown", []
    lineage = [h.iso for h in hits]
    chosen = None
    for hit in hits:
        if hit.iso[:4] in ("2025", "2026", "2027"):
            chosen = hit
            break
    if chosen is None:
        chosen = hits[0]
    return chosen.iso, chosen.iso_end, chosen.precision, lineage


def url_date(text: str) -> str | None:
    for match in URL_RE.finditer(text):
        found = re.search(r"/(20\d\d)[/-](\d{1,2})[/-](\d{1,2})", match.group(0))
        if found:
            year, month, day = (int(x) for x in found.groups())
            if 1 <= month <= 12 and 1 <= day <= 31:
                return f"{year:04d}-{month:02d}-{day:02d}"
    return None


def urls_in(text: str) -> list[str]:
    found: list[str] = []
    for match in URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;:)·*\"'")
        if url not in found:
            found.append(url)
    return found


def window_class(iso: str | None) -> str:
    if not iso:
        return "undated"
    if iso < WINDOW_START:
        return "antecedent"
    if iso > WINDOW_END:
        return "post_window"
    return "in_window"


# ------------------------------------------------------------ classification --

TYPE_RULES: list[tuple[str, str, str | None]] = [
    # (regex, event_type, subtype)
    # source/verification commentary is not an event — matched before anything else
    (r"^(?:wikipedia|source:|note:|electiondata\.my|the star|malay mail|malaysiakini)", "reference_note", None),
    (r"royal pardon|conditional pardon|pardons board|house arrest until", "royal_pardon", None),
    (r"show-cause|deregist|ros |registrar of societies|societies act", "ros_action", None),
    (r"\bdissolv", "dissolution", None),
    (r"by-?election", "by_election", None),
    (r"despite[^.]{0,60}(?:state polls|state election|\bprn\b)|having fought[^.]{0,40}(?:state polls|state election)", "statement", None),
    (r"state election|state polls|\bprn\b|\bse-\d", "state_election", None),
    (r"general election|ge-?16|snap poll", "ge16_signal", None),
    (r"\b(died|death|passed away|dead)\b", "death", None),
    (r"resign|resigned|letter of resignation", "resignation", None),
    (r"\bdefect|party switch(?:ed)?|switched to|joined (?:parti|bersama|wawasan|pejuang|grs|pn|ph|bn|umno|pas|pkr|dap|amanah)"
     r"|\bjoin(?:s|ed)?\s+(?:parti|bersama|wawasan|pejuang|grs|pn|ph|bn|umno|pas|pkr|dap|amanah|mca|mic)"
     r"|\bleft (?:pkr|dap|umno|pas|bersatu|amanah|pk|ph|bn|pn)\b|quits? (?:pkr|dap|umno|pas|bersatu|amanah)"
     r"|quit(?:s|ting)? pkr", "party_switch", None),
    (r"\bsack|expel|expelled|dismiss|dismissed from|removed from", "expulsion", None),
    (r"sworn in as (?:chief minister|menteri besar|mb)\b|new (?:chief minister|menteri besar)", "mb_change", None),
    (r"government formed|formed the government|coalition government assembled", "government_formation", None),
    (r"cabinet reshuffle|reshuffle|minister(s)? sworn|sworn in as .*minister|offers? to resign as .*minister"
     r"|re-appointed|reappointed|re-shuffle", "cabinet_change", None),
    (r"appointed|sworn in|named (?:as )?|announces? .* as |to lead|chairman of", "appointment", None),
    (r"non-executive chairman|\bchairman\b|chairmanship|executive chairman|\bchair\b|chief executive"
     r"|as (?:md|ceo|cto)|board member|heads? (?:the )?(?:glc|agency)", "appointment", None),
    (r"federal court|high court|court of appeal|judicial review|leave to appeal|struck out|tribunal|\bsued\b",
     "court_ruling", None),
    (r"election commission|\bec\b confirmed|\bec\b ruled|writ issued|no by-election|redelineation|delimitation", "ec_action", None),
    (r"pact|cooperation|alliance|electoral pact|memorandum of understanding|seat talks|seat negotiation"
     r"|admitted|component party|supreme council|leave (?:pn|the coalition)|exit the government|joins grs|join grs"
     r"|applied to join|accepted .* as .*component|expanding (?:to|the coalition)", "coalition_change", None),
    (r"no-confidence|motion of no confidence|\bbill\b|dewan rakyat|parliament(?:ary)? sitting|speaker",
     "parliamentary_proceeding", None),
    (r"\bpoll\b|pollster|survey|approval|satisfaction|right direction|vote intention", "poll_release", None),
    (r"seats? by party|bloc arithmetic|arithmetic|scorecard|seat count|composition at dissolution"
     r"|two-thirds|simple majority|majority remains|\bwon\b|\bdefeated\b|retained the seat"
     r"|majority of \d|by \d[\d,]* votes|victory", "election_result", None),
    (r"inflation|\bcpi\b|\bgdp\b|unemployment|trade|subsid|\bopr\b|ringgit|\bneer\b|reserves|economy", "economic_indicator", None),
    (r"membership|recruit|targets? \d", "membership_milestone", None),
    (r"will contest|contesting|contest .* seat|candidate|stand in ge16|contest ge16", "candidacy", None),
    (r"muktamar|national congress|national assembly|party congress|general assembly|\bagm\b"
     r"|annual general meeting|party convention|party assembly|convention", "party_assembly", None),
    (r"withdrew support|withdraws support|repair ties|member retention|stepped aside|assumed .* duties"
     r"|denounced|invites? |mooted|apolog|closes door|excluded|blocked from|debate over|profiles"
     r"|read .* as|exit clause|no seat lost|clashes|no longer", "statement", None),
    (r"^source:|^note:|wikipedia|electiondata\.my|do not use|treat as|aggregator|explainer"
     r"|no other federal vacancy", "reference_note", None),
    (r"warn|hint|say|said|says|call|urge|demand|deny|confirm|signal|reject|criticis|dismiss", "statement", None),
]

_COMPILED_TYPE_RULES = [(re.compile(pattern), etype, subtype) for pattern, etype, subtype in TYPE_RULES]

SIGNIFICANCE_BY_TYPE = {
    "dissolution": "critical", "state_election": "critical", "ge16_signal": "high",
    "election_result": "critical", "mb_change": "critical", "government_formation": "critical",
    "royal_pardon": "critical", "ros_action": "critical",
    "death": "high", "vacancy": "high", "by_election": "high", "court_ruling": "high",
    "coalition_change": "high", "party_switch": "high", "expulsion": "high",
    "cabinet_change": "high", "resignation": "high", "ec_action": "high",
    "parliamentary_proceeding": "high", "pact": "high",
    "appointment": "medium", "composition_change": "medium", "poll_release": "medium",
    "state_poll": "medium", "approval_rating": "medium", "leader_preference": "medium",
    "economic_indicator": "medium", "monetary_policy": "medium", "election_signal": "medium",
    "candidacy": "medium", "role_change": "medium", "vacancy_asserted": "medium",
    "fx_rate": "low", "statement": "low", "membership_milestone": "low", "note": "low",
    "reference_note": "low", "party_assembly": "low", "other": "low",
}

_ESCALATE_TO_CRITICAL = re.compile(
    r"federal court|constitutional amendment|two-thirds|30 days|deregist|house arrest|"
    r"prime minister|chief minister|menteri besar|suspended|pardon"
)
_UNVERIFIED_MARKERS = re.compile(
    r"unverified|year unknown|not verified|treat as|do not use|medium confidence|year not established",
    re.IGNORECASE,
)


# a text match on one of these beats the ingestion layer's own label for the row
STRONG_TYPES = {
    "ros_action", "court_ruling", "royal_pardon", "dissolution", "state_election",
    "by_election", "death", "expulsion", "mb_change", "government_formation",
}
# …but only when the layer label itself is generic: a layer that already says
# "resignation" or "vacancy" is reporting a specific fact and outranks the
# keyword classifier (whose matches are often incidental, e.g. a note about a
# by-election attached to a resignation role).
WEAK_LAYER_LABELS = {
    "role_change", "appointment", "statement", "candidacy", "policy_decision",
    "election_signal", "ge16_signal", "composition_change", "other",
}

# A keyword match is only evidence if the text asserts it: 'no by-election',
# 'seat NOT vacant', 'differs from the earlier by-election' describe the absence
# of the event (or a different, earlier one), so the rule is skipped and the next
# one gets a chance.
_NEGATED_BEFORE = re.compile(
    r"\b(?:no|not|never|without|denied|denies|rejects?|rejected|cancell?ed|fails?|failed"
    r"|rather than|instead of|unlikely|earlier|previous|prior|differs from|unlike"
    r"|pre-?window|nil)\b[^.;:]{0,28}$",
    re.IGNORECASE,
)


def _first_asserted_match(pattern: re.Pattern, lowered: str):
    for match in pattern.finditer(lowered):
        head = lowered[max(0, match.start() - 48):match.start()]
        if _NEGATED_BEFORE.search(head):
            continue
        return match
    return None


def classify(text: str, prefer: str | None = None) -> tuple[str, str | None, str]:
    """(event_type, subtype, basis).

    `prefer` is the ingestion layer's own label (a cron role type or a structured
    dossier field). It wins unless it is generic and the text itself names one of
    STRONG_TYPES, in which case that specific type is used and the layer label is
    kept as subtype ('layer:<label>').
    """
    lowered = mask_urls(text).lower()
    matched: tuple[str, str | None, str] | None = None
    for pattern, etype, subtype in _COMPILED_TYPE_RULES:
        if _first_asserted_match(pattern, lowered):
            matched = (etype, subtype, pattern.pattern[:40])
            break
    if prefer:
        if (matched and matched[0] in STRONG_TYPES and matched[0] != prefer
                and prefer in WEAK_LAYER_LABELS):
            return matched[0], f"layer:{prefer}", matched[2]
        return prefer, None, "structured"
    return matched or ("other", None, "no-rule")


def significance_for(event_type: str, text: str) -> str:
    base = SIGNIFICANCE_BY_TYPE.get(event_type, "low")
    if base == "high" and _ESCALATE_TO_CRITICAL.search(text):
        return "critical"
    if base == "medium" and _ESCALATE_TO_CRITICAL.search(text):
        return "high"
    return base


def confidence_for(urls: list[str], precision: str, text: str) -> str:
    if _UNVERIFIED_MARKERS.search(text) and not urls:
        return "low"
    if _UNVERIFIED_MARKERS.search(text):
        return "medium"
    if not urls:
        return "low" if precision in ("unknown", "year") else "medium"
    if precision in ("day", "month"):
        return "high"
    return "medium"


def _dedupe_words(text: str) -> str:
    """'GDP yoy H1 H1 2026' → 'GDP yoy H1 2026' (adjacent repeated words from joins)."""
    words = text.split()
    return " ".join(word for index, word in enumerate(words)
                    if index == 0 or word.lower() != words[index - 1].lower())


def clean_markdown(text: str) -> str:
    text = text.replace("\u00a0", " ")
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", text)
    text = text.replace("`", "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


_MONTH_WORD = r"[A-Z][a-z]{2,8}\.?"
_LEADING_DATE = re.compile(
    r"^\s*(?:on\s+|by\s+)?(?:"
    r"20\d\d-\d\d-\d\d"
    r"|\d{1,2}\s*[–—-]\s*\d{1,2}\s+" + _MONTH_WORD + r"\s+20\d\d"
    r"|\d{1,2}\s+" + _MONTH_WORD + r"\s*[–—-]\s*\d{1,2}\s+" + _MONTH_WORD + r"\s+20\d\d"
    r"|\d{1,2}\s+" + _MONTH_WORD + r"\s+20\d\d"
    r"|" + _MONTH_WORD + r"\s*[–—-]\s*" + _MONTH_WORD + r"\s+20\d\d"
    r"|" + _MONTH_WORD + r"\s+20\d\d"
    r"|\d{1,2}-\d{1,2}"
    r")\s*[:—–-]\s*"
)


def derive_title(text: str, fallback: str = "event") -> str:
    cleaned = clean_markdown(text)
    cleaned = URL_RE.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ·-—")
    cleaned = _dedupe_words(cleaned)
    cleaned = _LEADING_DATE.sub("", cleaned).strip()
    cleaned = re.sub(r"^(?:and|but|also)\s+", "", cleaned, flags=re.IGNORECASE)
    if not cleaned:
        return fallback
    for separator in (" — ", " – ", ". ", "; ", " · "):
        head, sep, _ = cleaned.partition(separator)
        if 25 <= len(head) <= 150:
            cleaned = (head + sep).strip(" .;")
            break
    truncated = False
    if len(cleaned) > 170:
        cleaned = cleaned[:170].rsplit(" ", 1)[0]
        truncated = True
    cleaned = cleaned.strip(" -–—:;,.")
    return (f"{cleaned}..." if truncated else cleaned) or fallback


# ------------------------------------------------------------------ records ---

@dataclass
class Record:
    dossier: str
    source_layer: str
    section: str | None
    text: str
    event_type: str = "other"
    subtype: str | None = None
    title: str = ""
    detail: str = ""
    event_date: str | None = None
    event_date_end: str | None = None
    date_precision: str = "unknown"
    date_source: str = "unknown"
    jurisdiction: str = "federal"
    state: str | None = None
    seat_code: str | None = None
    significance: str = "low"
    confidence: str = "medium"
    urls: list[str] = field(default_factory=list)
    source_title: str | None = None
    metrics: dict[str, tuple] = field(default_factory=dict)
    raw: dict = field(default_factory=dict)
    type_basis: str = ""
    date_lineage: list[str] = field(default_factory=list)

    def fingerprint_actor(self) -> str:
        return slug(self.text[:80])


@dataclass
class ClaimReview:
    dossier: str
    section: str | None
    kind: str
    claim_text: str
    baseline_value: str | None = None
    verified_value: str | None = None
    verdict: str = "corrected"
    confidence: str = "high"
    urls: list[str] = field(default_factory=list)


@dataclass
class Note:
    dossier: str
    section: str | None
    kind: str
    body: str
    urls: list[str] = field(default_factory=list)


@dataclass
class Extraction:
    records: list[Record] = field(default_factory=list)
    claims: list[ClaimReview] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def extend(self, other: "Extraction") -> None:
        self.records.extend(other.records)
        self.claims.extend(other.claims)
        self.notes.extend(other.notes)
        self.warnings.extend(other.warnings)


def make_record(dossier, layer, section, text, urls=None, prefer_type=None,
                date_override=None, date_precision=None, date_source=None,
                jurisdiction="federal", state=None, seat_code=None, metrics=None,
                raw=None, source_title=None) -> Record:
    urls = urls if urls is not None else urls_in(text)
    masked = mask_urls(text)
    etype, subtype, basis = classify(masked, prefer_type)
    lineage: list[str] = []
    if date_override:
        iso, end, precision = date_override, None, date_precision or "day"
        source = date_source or "field"
    else:
        iso, end, precision, lineage = primary_date(text)
        source = date_source or ("text" if iso else "unknown")
        if iso is None:
            slug_date = url_date(text)
            if slug_date:
                iso, end, precision, source = slug_date, None, "day", "url_slug"
    return Record(
        dossier=dossier, source_layer=layer, section=section, text=text,
        event_type=etype, subtype=subtype, title=derive_title(text),
        detail=clean_markdown(text), event_date=iso, event_date_end=end,
        date_precision=date_precision or precision, date_source=source,
        jurisdiction=jurisdiction, state=state, seat_code=seat_code,
        significance=significance_for(etype, masked), confidence=confidence_for(urls, precision, masked),
        urls=urls, source_title=source_title, metrics=metrics or {}, raw=raw or {},
        type_basis=basis, date_lineage=lineage,
    )


# -------------------------------------------------------------- partA reader --

_PART_A_HEADER = re.compile(r"^##\s+(\d+)\.")


def read_parta(path: Path) -> Extraction:
    """Parse the markdown federal dossier: bullets, tables, corrections."""
    result = Extraction()
    lines = path.read_text(encoding="utf-8").splitlines()
    section = ""
    subsection = ""
    bullet_lines: list[str] = []
    bullet_section = ""
    table_rows: list[list[str]] = []
    table_section = ""
    table_source_urls: list[str] = []
    table_kind = ""

    def flush_bullet():
        nonlocal bullet_lines
        if not bullet_lines:
            return
        body = " ".join(line.strip() for line in bullet_lines).strip()
        if len(clean_markdown(body)) < 20:
            bullet_lines = []          # structural fragments ("Type B.", "- Source: …")
            return
        section_label = bullet_section or subsection or section
        prefix = section.split(" ", 1)[0] if section else ""
        if prefix in ("9.", "10."):
            kind = "correction" if prefix == "9." else "unknown"
            result.claims.append(ClaimReview(
                dossier="partA-federal", section=section_label, kind=kind,
                claim_text=clean_markdown(re.sub(r"^\s*\d+\.\s*", "", body)),
                verdict="corrected" if kind == "correction" else "unresolved",
                confidence="high" if kind == "correction" else "unknown",
                urls=urls_in(body),
            ))
        elif prefix == "8.":
            result.claims.append(ClaimReview(
                dossier="partA-federal", section=section_label, kind="engine_finding",
                claim_text=clean_markdown(body), verdict="corrected", confidence="high",
                urls=urls_in(body),
            ))
        else:
            record = make_record("partA-federal", "dossier", section_label, body)
            result.records.append(record)
        bullet_lines = []

    def flush_table():
        nonlocal table_rows, table_source_urls, table_kind
        if not table_rows:
            return
        for row in table_rows[1:]:
            if len(row) < 2:
                continue
            cells = [clean_markdown(cell) for cell in row]
            row_urls = urls_in(" ".join(row))
            urls = row_urls + [u for u in table_source_urls if u not in row_urls]
            if table_kind == "claims" and len(cells) >= 2:
                result.claims.append(ClaimReview(
                    dossier="partA-federal", section=table_section or section, kind="correction",
                    claim_text=cells[0], baseline_value=cells[0], verified_value=cells[1],
                    verdict="corrected",
                    confidence={"high": "high", "medium-high": "medium", "medium": "medium"}.get(
                        (cells[2] if len(cells) > 2 else "").strip().lower(), "unknown"),
                    urls=urls,
                ))
                continue
            if table_kind == "byelection":
                title = f"{cells[0]} by-election: {cells[2]} won on {cells[1]}"
                record = make_record(
                    "partA-federal", "dossier", table_section or section,
                    f"{title}. Majority {cells[3]}; turnout {cells[4]}.", urls=urls,
                    prefer_type="by_election", jurisdiction="federal",
                )
                record.title = title
                record.metrics = _pct_metrics({
                    "majority": cells[3], "turnout": cells[4],
                })
                result.records.append(record)
                continue
            if table_kind == "appointments":
                title = cells[1]
                record = make_record(
                    "partA-federal", "dossier", table_section or section,
                    f"{cells[0]}: {title}", urls=urls, prefer_type="appointment",
                )
                record.title = derive_title(title)
                result.records.append(record)
                continue
            # generic table: date | figure | source(notes)
            text = " ".join(cell for cell in cells if cell and cell != "—")
            if not text.strip():
                continue
            result.records.append(make_record(
                "partA-federal", "dossier", table_section or section, text, urls=urls,
                prefer_type="approval_rating" if table_kind == "approval" else None,
            ))
        table_rows = []
        table_source_urls = []
        table_kind = ""

    for raw_line in lines:
        line = raw_line.rstrip()
        header = _PART_A_HEADER.match(line)
        if line.startswith("## ") or line.startswith("### "):
            flush_bullet()
            flush_table()
            subsection = ""
            if header:
                section = line[3:].strip()
            elif line.startswith("### "):
                subsection = line[4:].strip()
            continue
        if line.startswith("|"):
            flush_bullet()
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", cell) for cell in cells if cell):
                continue  # separator row
            if not table_rows:
                table_kind = _table_kind_for(subsection or section)
                table_section = subsection or section
            table_rows.append(cells)
            continue
        if table_rows and not line.startswith("|"):
            if line.lower().startswith("source:"):
                table_source_urls.extend(urls_in(line))
                continue
            flush_table()
        if re.match(r"^\*\*\d+\.\d+", line):
            flush_bullet()
            subsection = clean_markdown(line.strip("*").strip())
            continue
        if re.fullmatch(r"\*\*[^*]{3,120}\*\*", line.strip()):
            flush_bullet()
            subsection = clean_markdown(line.strip("*").strip())
            continue
        if line.startswith("- "):
            flush_bullet()
            bullet_section = subsection or section
            bullet_lines = [line[2:]]
            continue
        if re.match(r"^\d+\.\s+", line) and (section.startswith("9.")):
            flush_bullet()
            bullet_section = subsection or section
            bullet_lines = [line]
            continue
        if bullet_lines and (raw_line.startswith("  ") or raw_line.startswith("\t")):
            bullet_lines.append(line.strip())
            continue
        if not line.strip():
            flush_bullet()
            continue
        if bullet_lines:
            bullet_lines.append(line.strip())
    flush_bullet()
    flush_table()
    return result


def _table_kind_for(section: str) -> str:
    if section.startswith("0."):
        return "claims"
    if section.startswith("1.7"):
        return "byelection"
    if section.startswith("4."):
        return "appointments"
    if section.startswith("6."):
        return "approval"
    return "generic"


_NUM_PCT = re.compile(r"(-?\d[\d,]*)(?:\.\d+)?\s*%")
_NUM = re.compile(r"(-?\d[\d,]*(?:\.\d+)?)")


def _pct_metrics(cells: dict[str, str]) -> dict[str, tuple]:
    metrics: dict[str, tuple] = {}
    for name, value in cells.items():
        if not value:
            continue
        percent = _NUM_PCT.search(value)
        number = _NUM.search(value)
        numeric = None
        if percent:
            numeric = float(percent.group(1).replace(",", ""))
        elif number:
            numeric = float(number.group(1).replace(",", ""))
        metrics[name] = (numeric, value, "%" if percent else None)
    return metrics


# -------------------------------------------------------------- partB reader --

def read_partb(path: Path) -> Extraction:
    data = json.loads(path.read_text(encoding="utf-8"))
    result = Extraction()
    states = data.get("states", {})
    for key, state in states.items():
        name = state.get("state") or key.replace("_", " ").title()
        section = f"partB/{key}"
        jurisdiction = f"state:{name}"
        for vacancy in state.get("vacancies", []):
            seat = vacancy.get("seat", "")
            text = (f"{seat} declared vacant — {vacancy.get('former_rep', '')}. "
                    f"{vacancy.get('detail', '')}")
            period = vacancy.get("since") or vacancy.get("date") \
                or (vacancy.get("source") or {}).get("date")
            iso, precision = _period(period)
            record = make_record(
                "partB-states", "dossier", section, text, urls=_source_urls(vacancy.get("source")),
                prefer_type="vacancy", date_override=iso, date_precision=precision,
                date_source="field", jurisdiction=jurisdiction, state=name,
                raw={"state_key": key, "kind": "vacancy", "period_text": period},
            )
            record.title = f"{seat} declared vacant ({vacancy.get('former_rep', '')})".strip()
            result.records.append(_with_seat(record, seat, name))
        for by_election in state.get("byelections", []):
            seat = by_election.get("seat", "")
            text = (f"{seat} by-election — {by_election.get('status', '')}. "
                    f"{by_election.get('detail', '')}")
            # 'date' = polling day when held, 'date_expected' when not held
            period = by_election.get("date") or by_election.get("date_expected") \
                or (by_election.get("source") or {}).get("date")
            iso, precision = _period(period)
            record = make_record(
                "partB-states", "dossier", section, text, urls=_source_urls(by_election.get("source")),
                prefer_type="by_election", date_override=iso, date_precision=precision,
                date_source="field", jurisdiction=jurisdiction, state=name,
                raw={"state_key": key, "kind": "byelection", "period_text": period},
            )
            record.subtype = slug(by_election.get("status", ""))[:40] or None
            record.title = f"{seat} by-election: {by_election.get('status', '')}".strip()
            result.records.append(_with_seat(record, seat, name))
        for item in state.get("composition_change", []):
            record = make_record(
                "partB-states", "dossier", section,
                f"{item.get('event', '')}. {item.get('detail', '')}",
                urls=_source_urls(item.get("source")), prefer_type="composition_change",
                date_override=_iso_or_none(item.get("date")), jurisdiction=jurisdiction, state=name,
                raw={"state_key": key, "kind": "composition_change"},
            )
            record.title = derive_title(item.get("event", "")) or record.title
            result.records.append(record)
        for change in state.get("mb_changes", []):
            record = make_record(
                "partB-states", "dossier", section,
                f"Chief minister / menteri besar change: {change.get('from', '')} -> "
                f"{change.get('to', '')}. {change.get('detail', '')}",
                urls=_source_urls(change.get("source")), prefer_type="mb_change",
                date_override=_iso_or_none(change.get("date")), jurisdiction=jurisdiction, state=name,
                raw={"state_key": key, "kind": "mb_change"},
            )
            record.title = f"MB change in {name}: {change.get('from', '')} -> {change.get('to', '')}"
            result.records.append(record)
        for poll in state.get("polls", []):
            source = poll.get("source") or {}
            record = make_record(
                "partB-states", "dossier", section,
                f"{poll.get('pollster', '')} poll ({poll.get('fieldwork', '')}): {poll.get('finding', '')}",
                urls=_source_urls(source), prefer_type="state_poll",
                date_override=_iso_or_none(source.get("date")) or _iso_or_none(poll.get("fieldwork")),
                jurisdiction=jurisdiction, state=name,
                raw={"state_key": key, "kind": "poll"},
                source_title=source.get("title"),
            )
            record.title = f"{poll.get('pollster', '')} — {name} state poll ({poll.get('fieldwork', '')})"
            if poll.get("sample"):
                record.metrics = {"sample": (float(poll["sample"]), str(poll["sample"]), "respondents")}
            result.records.append(record)
        for index, correction in enumerate(state.get("corrections_to_baseline", []) or []):
            body = correction if isinstance(correction, str) else json.dumps(correction)
            result.claims.append(ClaimReview(
                dossier="partB-states", section=f"partB/{key}#corrections[{index}]",
                kind="correction", claim_text=clean_markdown(body), verdict="corrected",
                confidence="high", urls=urls_in(body),
            ))
        if state.get("notes"):
            result.notes.append(Note("partB-states", section, "state_note",
                                     clean_markdown(state["notes"])))
        if state.get("current_composition_estimate"):
            result.notes.append(Note(
                "partB-states", section, "composition_estimate",
                json.dumps(state["current_composition_estimate"], ensure_ascii=False)))

    sabah = data.get("sabah_prn_2026") or {}
    if sabah:
        result.notes.append(Note("partB-states", "partB/sabah_prn_2026", "headline",
                                 clean_markdown(sabah.get("headline", ""))))
        if sabah.get("cm_rotation"):
            result.notes.append(Note("partB-states", "partB/sabah_prn_2026", "cm_rotation",
                                     clean_markdown(sabah["cm_rotation"])))
        official = sabah.get("result_official") or {}
        if official:
            polling = _iso_or_none(sabah.get("polling_day"))
            seats = {key: value for key, value in official.items()
                     if isinstance(value, (int, float))}
            record = make_record(
                "partB-states", "dossier", "partB/sabah_prn_2026",
                f"{sabah.get('election', 'Sabah state election')} result: " +
                ", ".join(f"{k} {v}" for k, v in seats.items()),
                urls=urls_in(""), prefer_type="state_election", date_override=polling,
                jurisdiction="state:Sabah", state="Sabah",
                raw={"kind": "state_election_result"},
            )
            record.title = (f"{sabah.get('election', 'Sabah state election')} "
                            f"(polling {sabah.get('polling_day', '')}) — result")
            record.metrics = {f"seats_{key}": (float(value), str(value), "seats")
                              for key, value in seats.items()}
            result.records.append(record)
        formation = sabah.get("government_formation") or {}
        if formation:
            record = make_record(
                "partB-states", "dossier", "partB/sabah_prn_2026",
                f"Sabah government formed: {formation.get('chief_minister', '')} — "
                f"{formation.get('detail', '')}",
                urls=_source_urls(formation.get("source")), prefer_type="government_formation",
                date_override=_iso_or_none(formation.get("date")), jurisdiction="state:Sabah",
                state="Sabah", raw={"kind": "government_formation"},
            )
            record.title = f"Sabah government formed — {formation.get('chief_minister', '')}"
            result.records.append(record)
        for item in sabah.get("in_window_changes", []) or []:
            record = make_record(
                "partB-states", "dossier", "partB/sabah_prn_2026",
                f"{item.get('event', '')}. {item.get('detail', '')}".strip(),
                urls=_source_urls(item.get("source")),
                date_override=_iso_or_none(item.get("date")), jurisdiction="state:Sabah", state="Sabah",
                raw={"kind": "in_window_change"},
            )
            record.title = derive_title(item.get("event", "")) or record.title
            result.records.append(_with_seat(record, item.get("event", ""), "Sabah"))
        for index, correction in enumerate(sabah.get("corrections", []) or []):
            body = correction if isinstance(correction, str) else json.dumps(correction)
            result.claims.append(ClaimReview(
                dossier="partB-states", section=f"partB/sabah_prn_2026#corrections[{index}]",
                kind="correction", claim_text=clean_markdown(body), verdict="corrected",
                confidence="high", urls=urls_in(body),
            ))
        if sabah.get("current_composition"):
            result.notes.append(Note(
                "partB-states", "partB/sabah_prn_2026", "composition_estimate",
                json.dumps(sabah["current_composition"], ensure_ascii=False)))
    return result


def _with_seat(record: Record, seat_text: str, state: str | None) -> Record:
    match = re.search(r"\b([PN])\.?\s?(\d{1,3})\b", seat_text or "")
    if match:
        code = (f"P{int(match.group(2)):03d}" if match.group(1) == "P"
                else f"N.{int(match.group(2)):02d}")
        record.seat_code = code
    if state:
        record.state = state
        record.jurisdiction = f"state:{state}" if not record.jurisdiction.startswith("state:") else record.jurisdiction
    return record


def _source_urls(source) -> list[str]:
    if not source:
        return []
    if isinstance(source, str):
        return urls_in(source)
    if isinstance(source, list):
        urls: list[str] = []
        for item in source:
            urls.extend(_source_urls(item))
        return urls
    if isinstance(source, dict):
        urls = []
        for value in source.values():
            urls.extend(_source_urls(value))
        return urls
    return []


def _iso_or_none(value) -> str | None:
    """ISO date for an ISO value or a free-text period ('Jan 2026', '2026-Q1')."""
    return _period(value)[0]


def _period(value) -> tuple[str | None, str | None]:
    """(iso_date, precision) for an ISO date or a free-text period."""
    if not value or not isinstance(value, str):
        return None, None
    value = value.strip()
    if re.fullmatch(r"20\d\d-\d\d-\d\d", value):
        return value, "day"
    if re.fullmatch(r"20\d\d-\d\d", value):
        return f"{value}-01", "month"
    hits = extract_dates(value)
    if not hits:
        return None, None
    return hits[0].iso, hits[0].precision


def _source_title(source) -> str | None:
    if isinstance(source, dict):
        return source.get("title")
    return None


# -------------------------------------------------------------- partC reader --

def read_partc(path: Path) -> Extraction:
    data = json.loads(path.read_text(encoding="utf-8"))
    result = Extraction()
    meta = data.get("meta", {})
    if meta:
        result.notes.append(Note("partC-polls-macro", "partC/meta", "sweep_meta",
                                 json.dumps(meta, ensure_ascii=False)))

    def poll_record(item: dict, prefer: str, section: str, jurisdiction: str,
                    state: str | None, title: str, date_value, metrics: dict,
                    extra_text: str = "") -> None:
        iso, precision = _period(date_value)
        record = make_record(
            "partC-polls-macro", "dossier", section,
            f"{title}. {_results_text(item.get('results'))} {item.get('head_to_head') or ''} "
            f"{item.get('note') or ''} {extra_text}".strip(),
            urls=_source_urls(item.get("source")), prefer_type=prefer,
            date_override=iso, date_precision=precision, jurisdiction=jurisdiction, state=state,
            metrics=metrics, raw={"kind": prefer},
            source_title=item.get("title"),
        )
        record.title = title
        if item.get("mode"):
            record.raw["mode"] = item["mode"]
        if item.get("sample") or item.get("n"):
            record.metrics["sample"] = (float(item.get("sample") or item.get("n")),
                                        str(item.get("sample") or item.get("n")), "respondents")
        result.records.append(record)

    for poll in data.get("national_polls", []) or []:
        metrics = _numeric_metrics(poll.get("results") or {}, prefix="")
        poll_record(
            poll, "poll_release", "partC/national_polls", "national", None,
            f"{poll.get('pollster', '')}: {(poll.get('title') or poll.get('type') or 'national poll')}"
            f" (fieldwork {poll.get('fieldwork', '')}, published {poll.get('published', '')})",
            poll.get("published") or poll.get("fieldwork"), metrics,
        )
    for item in data.get("approval", []) or []:
        metrics = _numeric_metrics({k: v for k, v in item.items()
                                    if k.endswith("_pct")}, prefix="")
        poll_record(
            item, "approval_rating", "partC/approval", "national", None,
            f"{item.get('pollster', '')} approval {item.get('month', '')}: "
            f"Anwar {item.get('anwar_pct', '?')}%, government {item.get('govt_pct', '?')}%",
            item.get("month"), metrics,
        )
    for item in data.get("leader_pref", []) or []:
        metrics = _numeric_metrics(item.get("results_pct") or {}, prefix="leader_")
        poll_record(
            item, "leader_preference", "partC/leader_pref", "national", None,
            f"{item.get('pollster', '')} leader preference ({item.get('scope', '')})",
            item.get("published") or item.get("fieldwork"), metrics,
        )
    for item in data.get("state_polls", []) or []:
        scope = item.get("state", "")
        state = _state_from_text(scope)
        registered = _first_number(scope)
        outcome = item.get("result") or item.get("results")
        metrics = _numeric_metrics(outcome if isinstance(outcome, dict) else {})
        if registered[0] is not None:
            metrics["registered_voters"] = registered
        label = (scope.split(",")[0] or "").strip() or state or "state"
        jurisdiction = f"state:{state}" if state else "state:unknown"
        poll_record(
            item, "state_poll", "partC/state_polls", jurisdiction, state,
            f"{label} — {item.get('pollster') or 'state poll'}",
            item.get("published") or item.get("fieldwork") or item.get("date"),
            metrics, extra_text=_results_text(outcome),
        )
        for nested in item.get("polls") or []:
            if not isinstance(nested, dict):
                continue
            nested_metrics = _numeric_metrics(nested.get("results_pct") or {})
            if nested.get("n"):
                nested_metrics["sample"] = (float(nested["n"]), str(nested["n"]), "respondents")
            poll_record(
                nested, "state_poll", "partC/state_polls", jurisdiction, state,
                f"{label} — {nested.get('pollster') or 'poll'}",
                nested.get("published") or nested.get("fieldwork") or item.get("date"),
                nested_metrics,
            )
        for actual in ([item.get("result_actual")] if isinstance(item.get("result_actual"), dict) else []):
            poll_record(
                item, "state_poll", "partC/state_polls", jurisdiction, state,
                f"{label} — actual result", actual.get("date") or item.get("date"),
                _numeric_metrics(actual), extra_text=_results_text(actual),
            )
    for item in data.get("dosm", []) or []:
        period = item.get("period", "")
        iso, precision = _period(period)
        metrics = {}
        number = _num(item.get("value"))
        if number is not None:
            metrics[item.get("indicator", "value")] = (
                number, str(item["value"]), item.get("unit"))
        record = make_record(
            "partC-polls-macro", "dossier", "partC/dosm",
            f"DOSM {item.get('indicator', '')} for {period}: {item.get('value', '')} "
            f"{item.get('unit', '')}. {item.get('note', '')}",
            urls=_source_urls(item.get("source")), prefer_type="economic_indicator",
            date_override=iso, date_precision=precision, jurisdiction="national", metrics=metrics,
            raw={"kind": "dosm", "period": period},
        )
        record.title = f"DOSM {item.get('indicator', '')} {period}: {item.get('value', '')}"
        record.title = _dedupe_words(record.title)
        result.records.append(record)
    for item in data.get("bnm_opr", []) or []:
        iso, precision = _period(item.get("date"))
        opr = _num(item.get("opr_pct"))
        metrics = {"opr_pct": (opr, str(item["opr_pct"]), "pct")} if opr is not None else {}
        record = make_record(
            "partC-polls-macro", "dossier", "partC/bnm_opr",
            f"BNM OPR decision ({item.get('date', '')}): {item.get('decision', '')} at "
            f"{item.get('opr_pct', '')}%. {item.get('note', '')}",
            urls=_source_urls(item.get("source")), prefer_type="monetary_policy",
            date_override=iso, date_precision=precision, jurisdiction="national", metrics=metrics,
            raw={"kind": "bnm_opr"},
        )
        record.title = f"BNM {item.get('decision', '')} OPR at {item.get('opr_pct', '')}% ({item.get('date', '')})"
        result.records.append(record)
    for item in data.get("ringgit", []) or []:
        iso, precision = _period(item.get("date"))
        metrics = {}
        for key in ("myr_usd", "neer_qoq_pct", "neer_ytd_pct"):
            number = _num(item.get(key))
            if number is not None:
                metrics[key] = (number, str(item[key]),
                                "MYR/USD" if key == "myr_usd" else "pct")
        record = make_record(
            "partC-polls-macro", "dossier", "partC/ringgit",
            f"Ringgit/FX print {item.get('date', '')}: " +
            ", ".join(f"{k}={v}" for k, v in metrics.items()),
            urls=_source_urls(item.get("source")), prefer_type="fx_rate",
            date_override=iso, date_precision=precision, jurisdiction="national", metrics=metrics,
            raw={"kind": "ringgit", "note": item.get("note", "")},
        )
        record.title = f"Ringgit print {item.get('date', '')}" + (
            f" — MYR/USD {item['myr_usd']}" if item.get("myr_usd") else "")
        result.records.append(record)
    for index, correction in enumerate(data.get("corrections", []) or []):
        if isinstance(correction, dict):
            field = correction.get("field", f"correction[{index}]")
            claim = (f"Engine value {field}={correction.get('engine_value')} — "
                     f"{correction.get('issue', '')}. Verified: {correction.get('verified_latest', '')}. "
                     f"{correction.get('explanation', '')}")
            verdict = "corrected"
            urls = _source_urls(correction.get("source"))
            result.claims.append(ClaimReview(
                dossier="partC-polls-macro", section=f"partC/corrections[{index}]",
                kind="correction", claim_text=clean_markdown(claim),
                baseline_value=str(correction.get("engine_value")),
                verified_value=str(correction.get("verified_latest")),
                verdict=verdict, confidence="high", urls=urls,
            ))
        else:
            result.claims.append(ClaimReview(
                dossier="partC-polls-macro", section=f"partC/corrections[{index}]",
                kind="correction", claim_text=clean_markdown(str(correction)),
                verdict="corrected", confidence="high", urls=urls_in(str(correction)),
            ))
    for index, unknown in enumerate(data.get("unknowns", []) or []):
        body = unknown if isinstance(unknown, str) else json.dumps(unknown)
        result.claims.append(ClaimReview(
            dossier="partC-polls-macro", section=f"partC/unknowns[{index}]", kind="unknown",
            claim_text=clean_markdown(body), verdict="unresolved", confidence="unknown",
            urls=urls_in(body),
        ))
    if data.get("bnm_opr_note"):
        result.notes.append(Note("partC-polls-macro", "partC/bnm_opr", "monetary_note",
                                 clean_markdown(data["bnm_opr_note"])))
    return result


def _num(value) -> float | None:
    """Coerce a number or a numeric string ('4.0-5.0 (around 5.0)') to float."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d+(?:\.\d+)?", str(value))
    return float(match.group(0)) if match else None


def _numeric_metrics(payload, prefix: str = "") -> dict[str, tuple]:
    metrics: dict[str, tuple] = {}
    if not isinstance(payload, dict):
        return metrics
    for key, value in payload.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            unit = "pct" if key.endswith("_pct") else None
            metrics[f"{prefix}{key}"] = (float(value), str(value), unit)
        elif isinstance(value, dict):
            metrics.update(_numeric_metrics(value, prefix=f"{prefix}{key}."))
    return metrics


def _results_text(value) -> str:
    """Flatten a poll/result payload into readable text so nothing is lost to FTS."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_results_text(item) for item in value.values()).strip()
    if isinstance(value, list):
        return " ".join(_results_text(item) for item in value).strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value};"
    return ""


def _scope_of(value) -> str:
    """Best available place label for a poll payload ('Sarawak', 'P.104 Subang', …)."""
    if not isinstance(value, dict):
        return ""
    for key in ("state", "seat", "scope", "constituency", "jurisdiction"):
        found = value.get(key)
        if isinstance(found, str) and found.strip():
            return found.split(" - ")[0].split(" (")[0].strip()
    return ""


def _state_from_text(text: str) -> str | None:
    for state in ("Negeri Sembilan", "Pulau Pinang", "Johor", "Kedah", "Kelantan", "Melaka",
                  "Pahang", "Perak", "Perlis", "Sabah", "Sarawak", "Selangor", "Terengganu"):
        if state.lower() in (text or "").lower():
            return state
    return None


def _first_number(text: str) -> tuple:
    match = re.search(r"([\d,]{4,})", text or "")
    if not match:
        return None, None, None
    value = float(match.group(1).replace(",", ""))
    return value, match.group(1), None


# ------------------------------------------------------------- draft readers --

_ROLE_TYPES = {
    "resignation": "resignation", "party_switch": "party_switch", "appointment": "appointment",
    "candidacy": "candidacy", "minister": "cabinet_change", "deputy_minister": "cabinet_change",
    "sacked": "expulsion", "expelled": "expulsion", "death": "death", "arrest": "court_ruling",
    "court": "court_ruling", "mp": "role_change", "dun": "role_change",
    "political_secretary": "appointment", "senator": "appointment",
    "vacancy": "vacancy", "electoral": "ge16_signal", "policy": "policy_decision",
}


_MMDD_PREFIX = re.compile(r"^(\d{2})-(\d{2}):\s*")


def _expand_month_day(line: str, year: str) -> str:
    """'01-28: text' → '2026-01-28: text' when the line carries no year of its own."""
    match = _MMDD_PREFIX.match(line.strip())
    if not match or re.search(r"\b20\d\d\b", line):
        return line
    return f"{year}-{match.group(1)}-{match.group(2)}: {line.strip()[match.end():]}"


def read_drafts(draft_dir: Path) -> Extraction:
    """Cron update layers: party history lines, personnel roles, poll releases."""
    result = Extraction()
    draft_dir = Path(draft_dir)
    for path in sorted(draft_dir.glob("party-updates-*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        layer_year = str(data.get("date", ""))[:4] or "2026"
        for party in data.get("parties", []):
            name = party.get("name", "")
            for line in party.get("history", []) or []:
                line = _expand_month_day(str(line), layer_year)
                record = make_record("party-updates", "cron-update", name, line)
                record.title = f"{name}: {derive_title(line)}"
                result.records.append(record)
            if party.get("note"):
                result.notes.append(Note("party-updates", name, "party_note",
                                         clean_markdown(f"{name}: {party['note']}")))
    for path in sorted(draft_dir.glob("personnel-updates-*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for person in data.get("persons", []):
            name = person.get("name", "")
            for role in person.get("roles", []) or []:
                role_type = role.get("type", "")
                mapped = _ROLE_TYPES.get(role_type, "role_change")
                text = (f"{name} — {role_type} ({role.get('party', '')}"
                        f"{'/' + role['coalition'] if role.get('coalition') else ''}"
                        f"{', seat ' + role['seat'] if role.get('seat') else ''}). "
                        f"{role.get('note', '')}")
                record = make_record(
                    "personnel-updates", "cron-update", name, text,
                    urls=urls_in(role.get("note", "")), prefer_type=mapped,
                    date_override=_iso_or_none(role.get("start")),
                )
                record.subtype = role_type or None
                record.title = f"{name}: {role_type}" + (f" ({role['seat']})" if role.get("seat") else "")
                if role.get("seat") and re.fullmatch(r"[PN]\.?\s?\d{1,3}", str(role["seat"]).strip()):
                    record.seat_code = str(role["seat"]).strip()
                result.records.append(record)
    for path in sorted(draft_dir.glob("polls-update-*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for poll in data.get("polls", []) or []:
            results = poll.get("results")
            metrics = _numeric_metrics(results if isinstance(results, dict) else {})
            if poll.get("n"):
                metrics["sample"] = (float(poll["n"]), str(poll["n"]), "respondents")
            scope = _scope_of(results)
            headline = (poll.get("type") or "poll").replace("_", " ")
            parts = [part for part in (poll.get("pollster"), headline) if part]
            if poll.get("fieldwork"):
                parts.append(f"fieldwork {poll['fieldwork']}")
            if poll.get("published"):
                parts.append(f"published {poll['published']}")
            title = (f"{scope}: " if scope else "") + " · ".join(parts)
            record = make_record(
                "polls-update", "cron-update", poll.get("pollster", ""),
                f"{title}. {_results_text(results)}".strip(),
                urls=_source_urls(poll.get("source")), prefer_type="poll_release",
                date_override=_iso_or_none(poll.get("published")) or _iso_or_none(poll.get("fieldwork")),
                jurisdiction="national", metrics=metrics,
                raw={"kind": "poll_release", "scope": scope},
            )
            record.title = title
            result.records.append(record)
    return result
