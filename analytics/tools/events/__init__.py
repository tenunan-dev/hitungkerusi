"""GE16 events-database tooling (SQLite + FTS5).

Build:  python tools/events/build_events_db.py
Query:  python tools/events/query_events.py --help

The SQLite store is the structured event layer of the GE16 analytics stack: the
knowledge graph holds entity relationships and the vector databases hold
similarity, while this database holds dated events with their actors, sources,
numeric payloads, event-to-event links and the sweep's baseline corrections.
"""
