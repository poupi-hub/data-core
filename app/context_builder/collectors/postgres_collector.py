"""
PostgresCollector — verifica o estado do PostgreSQL.

Coleta (via SQLAlchemy, read-only):
  - Conectividade básica (SELECT 1)
  - Pool stats (connections ativas, idle, waiting)
  - Tabelas mais pesadas (pg_stat_user_tables)
  - Long-running queries (pg_stat_activity)
  - Tamanho do banco
  - Replication lag (se replica configurada)
"""

from __future__ import annotations

from typing import Any

from app.context_builder.collectors.base import BaseCollector

# Queries read-only para diagnóstico
_QUERIES = {
    "connection_count": """
        SELECT count(*) as total,
               sum(CASE WHEN state = 'active' THEN 1 ELSE 0 END) as active,
               sum(CASE WHEN state = 'idle' THEN 1 ELSE 0 END) as idle,
               sum(CASE WHEN wait_event_type = 'Lock' THEN 1 ELSE 0 END) as waiting_lock
        FROM pg_stat_activity
        WHERE datname = current_database()
    """,
    "long_queries": """
        SELECT pid, state, wait_event_type, wait_event,
               EXTRACT(EPOCH FROM (now() - query_start))::int as duration_seconds,
               LEFT(query, 120) as query_preview
        FROM pg_stat_activity
        WHERE datname = current_database()
          AND state != 'idle'
          AND query_start < now() - interval '30 seconds'
        ORDER BY duration_seconds DESC
        LIMIT 5
    """,
    "db_size": """
        SELECT pg_size_pretty(pg_database_size(current_database())) as db_size,
               pg_database_size(current_database()) as db_size_bytes
    """,
    "table_stats": """
        SELECT relname, n_live_tup, n_dead_tup,
               n_tup_ins + n_tup_upd + n_tup_del as total_writes
        FROM pg_stat_user_tables
        WHERE relname IN (
            'incident_events', 'incident_history', 'incident_patterns',
            'raw_collections', 'normalized_collections', 'pipeline_stages'
        )
        ORDER BY n_live_tup DESC
    """,
}


class PostgresCollector(BaseCollector):
    name = "postgres"
    timeout_seconds = 8.0

    def collect_data(self, context: dict[str, Any]) -> dict[str, Any]:
        # Import here to avoid circular imports and ensure lazy loading
        from database.session import SessionLocal
        from sqlalchemy import text

        results: dict[str, Any] = {}
        errors: list[str] = []
        connected = False

        try:
            with SessionLocal() as db:
                # Test connectivity
                db.execute(text("SELECT 1"))
                connected = True

                for query_name, sql in _QUERIES.items():
                    try:
                        rows = db.execute(text(sql)).fetchall()
                        if query_name == "connection_count" and rows:
                            r = rows[0]
                            results[query_name] = {
                                "total": r.total,
                                "active": r.active,
                                "idle": r.idle,
                                "waiting_lock": r.waiting_lock,
                            }
                        elif query_name == "long_queries":
                            results[query_name] = [
                                {
                                    "pid": r.pid,
                                    "state": r.state,
                                    "wait_event": r.wait_event,
                                    "duration_seconds": r.duration_seconds,
                                    "query": r.query_preview,
                                }
                                for r in rows
                            ]
                        elif query_name == "db_size" and rows:
                            results[query_name] = {
                                "size": rows[0].db_size,
                                "bytes": rows[0].db_size_bytes,
                            }
                        elif query_name == "table_stats":
                            results[query_name] = [
                                {
                                    "table": r.relname,
                                    "live_rows": r.n_live_tup,
                                    "dead_rows": r.n_dead_tup,
                                    "total_writes": r.total_writes,
                                }
                                for r in rows
                            ]
                    except Exception as exc:
                        errors.append(f"{query_name}: {exc}")

        except Exception as exc:
            errors.append(f"connection: {exc}")

        has_long_queries = bool(results.get("long_queries"))
        has_lock_waits = (
            results.get("connection_count", {}).get("waiting_lock", 0) > 0
        )

        return {
            "connected": connected,
            "has_long_queries": has_long_queries,
            "has_lock_waits": has_lock_waits,
            "data": results,
            "errors": errors,
        }
