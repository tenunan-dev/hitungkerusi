#!/usr/bin/env python3
"""Query the GE16 relational events database (read-only).

    python tools/events/query_events.py search "RoS PN show-cause"
    python tools/events/query_events.py timeline --type by_election
    python tools/events/query_events.py entity "Wong Chen"
    python tools/events/query_events.py event evt-1234abcd
    python tools/events/query_events.py links evt-1234abcd
    python tools/events/query_events.py claims --kind correction
    python tools/events/query_events.py metrics --metric "CPI yoy"
    python tools/events/query_events.py seats --code P104
    python tools/events/query_events.py stories [--all | --status open]
    python tools/events/query_events.py story story-1a2b3c4d5e6f
    python tools/events/query_events.py --stories            # same as `stories`
    python tools/events/query_events.py --story <story-id>   # same as `story`
    python tools/events/query_events.py stats
    python tools/events/query_events.py sql "SELECT event_type, COUNT(*) FROM events GROUP BY 1"

Every command accepts --json (machine-readable) and --limit. The database is
opened read-only, so a query can never mutate the store.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path


def resolve_repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


DEFAULT_DB = resolve_repository_root() / "work" / "events" / "ge16-events.db"
READ_ONLY_SQL = re.compile(r"^\s*(?:select|with)\b", re.IGNORECASE)
FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum|reindex)\b",
    re.IGNORECASE,
)
FTS_OPERATORS = re.compile(r'"|\*|\(|\)|\b(?:OR|AND|NOT|NEAR)\b')
FTS_UNSAFE_TOKEN = re.compile(r"[^\w]")


def sanitize_fts_query(query: str) -> str:
    """Quote bare tokens that FTS5 would read as syntax ('show-cause' → '"show-cause"').

    A query that already uses FTS5 operators ('"', '*', parentheses, AND/OR/NOT/NEAR)
    is passed through untouched so power users keep the full expression language.
    """
    query = query.strip()
    if not query or FTS_OPERATORS.search(query):
        return query
    tokens = []
    for token in query.split():
        tokens.append(f'"{token}"' if FTS_UNSAFE_TOKEN.search(token) else token)
    return " ".join(tokens)


def connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        print(f"events database not found: {path}\nbuild it with: python tools/events/build_events_db.py",
              file=sys.stderr)
        raise SystemExit(2)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def emit(rows, columns, as_json: bool, empty: str = "no rows") -> None:
    rows = [dict(row) for row in rows]
    if as_json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    if not rows:
        print(empty)
        return
    widths = {column: max(len(column), *(len(str(row.get(column, ""))) for row in rows)) for column in columns}
    print("  ".join(column.upper().ljust(widths[column]) for column in columns))
    print("  ".join("-" * widths[column] for column in columns))
    for row in rows:
        print("  ".join(str(row.get(column, "") or "")[:widths[column]].ljust(widths[column])
                        for column in columns))


EVENT_COLUMNS = ["event_date", "event_type", "event_id", "jurisdiction", "title"]


def cmd_search(conn, args) -> None:
    where = ["events_fts MATCH :query"]
    params: dict = {"query": sanitize_fts_query(args.query), "limit": args.limit}
    if args.type:
        where.append("e.event_type = :type")
        params["type"] = args.type
    if args.from_date:
        where.append("e.event_date >= :from_date")
        params["from_date"] = args.from_date
    if args.to_date:
        where.append("e.event_date <= :to_date")
        params["to_date"] = args.to_date
    if args.significance:
        where.append("e.significance = :significance")
        params["significance"] = args.significance
    sql = (
        "SELECT e.event_date, e.event_type, e.event_id, e.jurisdiction, e.title,"
        "       e.seat_code, s.publisher, s.url,"
        "       snippet(events_fts, 1, '[', ']', ' … ', 10) AS match_snippet"
        "  FROM events_fts"
        "  JOIN events e ON e.event_id = events_fts.event_id"
        "  LEFT JOIN sources s ON s.source_id = e.primary_source_id"
        f" WHERE {' AND '.join(where)}"
        " ORDER BY rank LIMIT :limit"
    )
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as error:
        print(f"invalid full-text query: {error}", file=sys.stderr)
        raise SystemExit(2)
    if args.json:
        emit(rows, [], True)
        return
    print(f"{len(rows)} match(es) for {args.query!r}")
    for row in rows:
        print(f"  {row['event_date'] or '----------'} [{row['event_type']}] {row['title']}")
        print(f"      {row['event_id']}  {row['jurisdiction']}  {row['publisher'] or 'no source'}")
        if row["match_snippet"]:
            print(f"      … {row['match_snippet']}")


def _event_filter(args) -> tuple[str, dict]:
    where, params = [], {"limit": args.limit}
    if getattr(args, "type", None):
        where.append("event_type = :type")
        params["type"] = args.type
    if getattr(args, "from_date", None):
        where.append("event_date >= :from_date")
        params["from_date"] = args.from_date
    if getattr(args, "to_date", None):
        where.append("event_date <= :to_date")
        params["to_date"] = args.to_date
    if getattr(args, "jurisdiction", None):
        where.append("jurisdiction LIKE :jurisdiction")
        params["jurisdiction"] = f"%{args.jurisdiction}%"
    if getattr(args, "significance", None):
        where.append("significance = :significance")
        params["significance"] = args.significance
    return (" AND ".join(where) or "1"), params


def cmd_timeline(conn, args) -> None:
    where, params = _event_filter(args)
    rows = conn.execute(
        "SELECT event_date, event_type, event_id, jurisdiction, significance, confidence, title"
        f"  FROM events WHERE event_date IS NOT NULL AND ({where})"
        " ORDER BY event_date, event_type LIMIT :limit", params).fetchall()
    emit(rows, EVENT_COLUMNS + ["significance"], args.json)


def cmd_entity(conn, args) -> None:
    needle = args.name.strip().lower()
    matches = conn.execute(
        "SELECT entity_id, entity_type, name, kg_node_id, event_count, first_event_date, last_event_date"
        "  FROM entities WHERE lower(name) LIKE :like OR lower(aliases_json) LIKE :like"
        " ORDER BY (lower(name) = :exact) DESC, event_count DESC LIMIT :limit",
        {"like": f"%{needle}%", "exact": needle, "limit": args.limit}).fetchall()
    if not matches:
        print(f"no entity matches {args.name!r}")
        return
    if args.json:
        payload = []
        for entity in matches:
            events = conn.execute(
                "SELECT e.event_date, e.event_type, e.event_id, ee.role, e.title FROM event_entities ee"
                " JOIN events e ON e.event_id = ee.event_id WHERE ee.entity_id = ?"
                " ORDER BY e.event_date LIMIT ?", (entity["entity_id"], args.limit)).fetchall()
            payload.append({**dict(entity), "events": [dict(row) for row in events]})
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    for entity in matches:
        header = f"\n{entity['name']}  [{entity['entity_type']}]  {entity['entity_id']}"
        if entity["kg_node_id"]:
            header += f"  kg:{entity['kg_node_id']}"
        print(header)
        print(f"  {entity['event_count']} event(s)"
              f"  {entity['first_event_date'] or '?'} → {entity['last_event_date'] or '?'}")
        for row in conn.execute(
            "SELECT e.event_date, e.event_type, e.event_id, ee.role, e.title FROM event_entities ee"
            " JOIN events e ON e.event_id = ee.event_id WHERE ee.entity_id = ?"
            " ORDER BY e.event_date LIMIT ?", (entity["entity_id"], args.limit)):
            print(f"    {row['event_date'] or '----------'} [{row['event_type']:<20}] {row['role']:<11}"
                  f" {row['title']}")


def cmd_event(conn, args) -> None:
    row = conn.execute("SELECT * FROM events WHERE event_id = ?", (args.event_id,)).fetchone()
    if row is None:
        print(f"no event {args.event_id!r}")
        return
    if args.json:
        print(json.dumps(dict(row), indent=2, ensure_ascii=False))
        return
    for key in ("event_id", "event_date", "event_date_end", "date_precision", "date_source", "event_type",
                "subtype", "jurisdiction", "state", "seat_code", "significance", "confidence",
                "window_class", "dossier", "section", "source_layer", "corroboration_count", "source_count"):
        print(f"  {key:<20} {row[key]}")
    print(f"  {'title':<20} {row['title']}")
    print(f"  {'detail':<20} {row['detail']}")
    print("  entities:")
    for entity in conn.execute(
        "SELECT en.entity_id, en.entity_type, en.name, ee.role, ee.mention FROM event_entities ee"
        " JOIN entities en ON en.entity_id = ee.entity_id WHERE ee.event_id = ?"
        " ORDER BY ee.role, en.name", (args.event_id,)):
        print(f"    {entity['role']:<12}{entity['name']}  ({entity['entity_id']})")
    print("  metrics:")
    for metric in conn.execute("SELECT metric, value_num, value_text, unit FROM event_metrics WHERE event_id = ?",
                               (args.event_id,)):
        print(f"    {metric['metric']:<34}{metric['value_text']} {metric['unit'] or ''}")
    print("  sources:")
    for source in conn.execute(
        "SELECT s.publisher, s.url, es.relation FROM event_sources es JOIN sources s ON s.source_id = es.source_id"
        " WHERE es.event_id = ?", (args.event_id,)):
        print(f"    {source['relation']:<14}{source['publisher']}: {source['url']}")


def cmd_links(conn, args) -> None:
    rows = conn.execute(
        "SELECT l.relation, l.basis, l.gap_days,"
        "       CASE WHEN l.from_event_id = :id THEN 'out' ELSE 'in' END AS direction,"
        "       e.event_id, e.event_date, e.event_type, e.title"
        "  FROM event_links l JOIN events e"
        "    ON e.event_id = CASE WHEN l.from_event_id = :id THEN l.to_event_id ELSE l.from_event_id END"
        " WHERE l.from_event_id = :id OR l.to_event_id = :id"
        " ORDER BY e.event_date", {"id": args.event_id}).fetchall()
    emit(rows, ["direction", "relation", "gap_days", "event_date", "event_type", "event_id", "title"],
         args.json, empty="no links for this event")


def cmd_claims(conn, args) -> None:
    where, params = [], {}
    if args.kind:
        where.append("kind = :kind")
        params["kind"] = args.kind
    if args.verdict:
        where.append("verdict = :verdict")
        params["verdict"] = args.verdict
    params["limit"] = args.limit
    rows = conn.execute(
        "SELECT c.claim_id, c.dossier, c.kind, c.verdict, c.confidence, c.claim_text, c.baseline_value,"
        "       c.verified_value, s.url FROM claim_reviews c LEFT JOIN sources s ON s.source_id = c.source_id"
        f" WHERE {' AND '.join(where) or '1'} ORDER BY c.dossier, c.claim_id LIMIT :limit", params).fetchall()
    emit(rows, ["claim_id", "dossier", "kind", "verdict", "confidence", "claim_text"], args.json)


def cmd_metrics(conn, args) -> None:
    where, params = [], {"limit": args.limit}
    if args.metric:
        where.append("m.metric = :metric")
        params["metric"] = args.metric
    rows = conn.execute(
        "SELECT e.event_date, m.metric, m.value_num, m.unit, e.event_id, e.title FROM event_metrics m"
        " JOIN events e ON e.event_id = m.event_id"
        f" WHERE {' AND '.join(where) or '1'} ORDER BY m.metric, e.event_date LIMIT :limit", params).fetchall()
    emit(rows, ["metric", "event_date", "value_num", "unit", "event_id"], args.json)


def cmd_seats(conn, args) -> None:
    where, params = [], {"limit": args.limit}
    if args.code:
        where.append("e.seat_code = :code")
        params["code"] = args.code
    rows = conn.execute(
        "SELECT e.seat_code, e.event_date, e.event_type, e.event_id, e.title FROM events e"
        f" WHERE {(' AND '.join(where) if where else 'e.seat_code IS NOT NULL')}"
        " ORDER BY e.seat_code, e.event_date LIMIT :limit", params).fetchall()
    emit(rows, ["seat_code", "event_date", "event_type", "event_id", "title"], args.json)


STORY_COLUMNS = ["story_id", "status", "first_seen", "last_update", "event_count", "headline"]


def cmd_stories(conn, args) -> None:
    """Open threads from the story ledger (v_story_current), or every thread."""
    if args.all:
        rows = conn.execute(
            "SELECT story_id, status, theme, anchor, first_seen, last_update, event_count,"
            " headline FROM stories ORDER BY last_update DESC, event_count DESC, story_id"
            " LIMIT :limit", {"limit": args.limit}).fetchall()
    elif args.status:
        rows = conn.execute(
            "SELECT story_id, status, theme, anchor, first_seen, last_update, event_count,"
            " headline FROM stories WHERE status = :status"
            " ORDER BY last_update DESC, event_count DESC, story_id LIMIT :limit",
            {"status": args.status, "limit": args.limit}).fetchall()
    else:
        rows = conn.execute(
            "SELECT story_id, status, theme, anchor, first_seen, last_update, event_count,"
            " headline FROM v_story_current LIMIT :limit", {"limit": args.limit}).fetchall()
    columns = (["story_id", "theme", "anchor", "first_seen", "last_update", "event_count", "headline"]
               if (args.all or args.status) else STORY_COLUMNS)
    emit(rows, columns, args.json, empty="no stories in the ledger")


def cmd_story(conn, args) -> None:
    """One thread: ledger header + its events in order (v_story_timeline)."""
    header = conn.execute(
        "SELECT story_id, headline, status, theme, anchor, first_seen, last_update,"
        " summary, entity_refs, event_count, created_at FROM stories WHERE story_id = :story_id",
        {"story_id": args.story_id}).fetchone()
    if header is None:
        print(f"story not found: {args.story_id}", file=sys.stderr)
        raise SystemExit(2)
    timeline = conn.execute(
        "SELECT seq, event_date, event_type, event_id, seat_code, event_entities, title"
        " FROM v_story_timeline WHERE story_id = :story_id ORDER BY seq LIMIT :limit",
        {"story_id": args.story_id, "limit": args.limit}).fetchall()
    if args.json:
        payload = dict(header)
        payload["events"] = [dict(row) for row in timeline]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    print(f"{header['headline']}")
    print(f"  story {header['story_id']}  status {header['status']}  theme {header['theme'] or '-'}"
          f"  anchor {header['anchor'] or '-'}")
    print(f"  first seen {header['first_seen'] or 'undated'}  last update {header['last_update'] or 'undated'}"
          f"  events {header['event_count']}")
    print(f"  {header['summary']}")
    emit(timeline, ["seq", "event_date", "event_type", "event_id", "seat_code", "title"],
         False, empty="no events in this thread")


def cmd_stats(conn, args) -> None:
    payload = {
        "events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        "entities": conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
        "event_entities": conn.execute("SELECT COUNT(*) FROM event_entities").fetchone()[0],
        "event_links": conn.execute("SELECT COUNT(*) FROM event_links").fetchone()[0],
        "metrics": conn.execute("SELECT COUNT(*) FROM event_metrics").fetchone()[0],
        "sources": conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
        "claims": conn.execute("SELECT COUNT(*) FROM claim_reviews").fetchone()[0],
        "notes": conn.execute("SELECT COUNT(*) FROM dossier_notes").fetchone()[0],
        "stories": conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0],
        "stories_by_status": {row[0]: row[1] for row in conn.execute(
            "SELECT status, COUNT(*) FROM stories GROUP BY 1 ORDER BY 2 DESC")},
        "by_type": {row[0]: row[1] for row in conn.execute(
            "SELECT event_type, COUNT(*) FROM events GROUP BY 1 ORDER BY 2 DESC")},
        "by_dossier": {row[0]: row[1] for row in conn.execute(
            "SELECT dossier, COUNT(*) FROM events GROUP BY 1 ORDER BY 2 DESC")},
        "by_window": {row[0]: row[1] for row in conn.execute(
            "SELECT window_class, COUNT(*) FROM events GROUP BY 1")},
        "by_significance": {row[0]: row[1] for row in conn.execute(
            "SELECT significance, COUNT(*) FROM events GROUP BY 1 ORDER BY 2 DESC")},
        "top_entities": [{"name": row[0], "type": row[1], "events": row[2]} for row in conn.execute(
            "SELECT en.name, en.entity_type, COUNT(*) n FROM event_entities ee JOIN entities en"
            " ON en.entity_id = ee.entity_id GROUP BY 1, 2 ORDER BY n DESC LIMIT 10")],
        "ingest_run": dict(conn.execute(
            "SELECT built_at, builder_sha256, schema_version, source_window, ok FROM ingest_runs"
            " ORDER BY run_id DESC LIMIT 1").fetchone() or {}),
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    for key, value in payload.items():
        if isinstance(value, dict):
            print(f"{key}:")
            for inner_key, inner in value.items():
                print(f"   {inner_key}: {inner}")
        elif isinstance(value, list):
            print(f"{key}: " + ", ".join(f"{item['name']} ({item['events']})" for item in value))
        else:
            print(f"{key}: {value}")


def cmd_sql(conn, args) -> None:
    statement = args.statement.strip().rstrip(";")
    if not READ_ONLY_SQL.match(statement) or FORBIDDEN_SQL.search(statement) or ";" in statement:
        print("only a single read-only SELECT/WITH statement is allowed", file=sys.stderr)
        raise SystemExit(2)
    rows = conn.execute(statement).fetchall()
    if args.json:
        print(json.dumps([dict(row) for row in rows], indent=2, ensure_ascii=False, default=str))
        return
    if not rows:
        print("no rows")
        return
    columns = list(rows[0].keys())
    emit(rows[:args.limit], columns, False)


def normalize_argv(argv):
    """Accept the convenience flags `--stories` / `--story ID` as subcommands.

    The packet's read path is `query_events.py --stories`, while the CLI is
    subcommand based; both spellings are supported so a script written against
    either one keeps working.
    """
    tokens = list(argv)
    for index, token in enumerate(tokens):
        if token == "--stories":
            tokens[index] = "stories"
            break
        if token == "--story":
            tokens[index] = "story"
            break
    return tokens


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Query the GE16 events database (read-only).")
    parser.add_argument("--db", default=os.environ.get("GE16_EVENTS_DB", str(DEFAULT_DB)))
    parser.add_argument("--json", action="store_true", help="emit JSON instead of tables")
    parser.add_argument("--limit", type=int, default=25)
    # the same two flags are accepted after the subcommand as well (SUPPRESS keeps the
    # top-level value when the subcommand copy is absent)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("--limit", type=int, default=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    search = sub.add_parser("search", parents=[common], help="full-text search over events (FTS5)")
    search.add_argument("query")
    search.add_argument("--type")
    search.add_argument("--from", dest="from_date")
    search.add_argument("--to", dest="to_date")
    search.add_argument("--significance")
    search.set_defaults(func=cmd_search)

    timeline = sub.add_parser("timeline", parents=[common], help="dated events in order")
    timeline.add_argument("--type")
    timeline.add_argument("--from", dest="from_date")
    timeline.add_argument("--to", dest="to_date")
    timeline.add_argument("--jurisdiction")
    timeline.add_argument("--significance")
    timeline.set_defaults(func=cmd_timeline)

    entity = sub.add_parser("entity", parents=[common], help="entity lookup + its events")
    entity.add_argument("name")
    entity.set_defaults(func=cmd_entity)

    event = sub.add_parser("event", parents=[common], help="one event with entities, metrics and citations")
    event.add_argument("event_id")
    event.set_defaults(func=cmd_event)

    links = sub.add_parser("links", parents=[common], help="event-to-event relations")
    links.add_argument("event_id")
    links.set_defaults(func=cmd_links)

    claims = sub.add_parser("claims", parents=[common], help="baseline corrections, unknowns, engine findings")
    claims.add_argument("--kind", choices=["correction", "unknown", "engine_finding"])
    claims.add_argument("--verdict", choices=["corrected", "confirmed", "unresolved", "do_not_use"])
    claims.set_defaults(func=cmd_claims)

    metrics = sub.add_parser("metrics", parents=[common], help="numeric metric payloads")
    metrics.add_argument("--metric")
    metrics.set_defaults(func=cmd_metrics)

    seats = sub.add_parser("seats", parents=[common], help="event history by seat code")
    seats.add_argument("--code")
    seats.set_defaults(func=cmd_seats)

    stories = sub.add_parser("stories", parents=[common],
                             help="story ledger: open threads (v_story_current)")
    stories.add_argument("--all", action="store_true", help="every thread, not only open ones")
    stories.add_argument("--status", choices=["open", "dormant", "closed"])
    stories.set_defaults(func=cmd_stories)

    story = sub.add_parser("story", parents=[common], help="one thread + its event timeline")
    story.add_argument("story_id")
    story.set_defaults(func=cmd_story)

    stats = sub.add_parser("stats", parents=[common], help="store summary")
    stats.set_defaults(func=cmd_stats)

    sql = sub.add_parser("sql", parents=[common], help="single read-only SELECT")
    sql.add_argument("statement")
    sql.set_defaults(func=cmd_sql)

    args = parser.parse_args(normalize_argv(argv if argv is not None else sys.argv[1:]))
    connection = connect(Path(args.db))
    try:
        args.func(connection, args)
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
