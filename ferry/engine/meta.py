"""ferry.engine.meta - ferry_meta bookkeeping schema (PG targets)."""

from __future__ import annotations

from contextlib import contextmanager

import psycopg

from .connect import quote

DDL = """
CREATE TABLE IF NOT EXISTS {q}.run_log (
    run_id TEXT PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    status TEXT NOT NULL,
    summary JSONB
);
CREATE TABLE IF NOT EXISTS {q}.migration_log (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL,
    wave INTEGER,
    target_table TEXT NOT NULL,
    strategy TEXT NOT NULL,
    rows_source BIGINT,
    rows_written BIGINT,
    status TEXT NOT NULL,
    error TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS {q}.repair_log (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT,
    target_table TEXT NOT NULL,
    target_col TEXT,
    old_value TEXT,
    rule TEXT NOT NULL,
    row_key TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class MetaLog:
    """Writes run/migration logs into <schema> on a PG target. No-ops elsewhere."""

    def __init__(self, raw_conn_factory, dialect: str, schema: str = "ferry_meta", enabled: bool = True):
        self._raw = raw_conn_factory
        self.dialect = dialect
        self.schema = schema
        self.enabled = enabled and dialect == "postgresql"

    @contextmanager
    def _connection(self):
        conn = self._raw()
        try:
            yield conn
        finally:
            conn.close()

    def ensure_schema(self):
        if not self.enabled:
            return
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"CREATE SCHEMA IF NOT EXISTS {quote(self.schema, 'postgresql')}")
                cur.execute(DDL.format(q=quote(self.schema, "postgresql")))
            conn.commit()

    def run_start(self, run_id: str, source: str, target: str):
        if not self.enabled:
            return
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"INSERT INTO {self._q('run_log')} (run_id, source, target, status) "
                            "VALUES (%s, %s, %s, 'running') "
                            "ON CONFLICT (run_id) DO UPDATE SET status='running'",
                            (run_id, _redact(source), _redact(target)))
            conn.commit()

    def run_finish(self, run_id: str, status: str, summary: dict):
        if not self.enabled:
            return
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"UPDATE {self._q('run_log')} SET status=%s, finished_at=now(), summary=%s "
                            "WHERE run_id=%s",
                            (status, psycopg.types.json.Json(summary), run_id))
            conn.commit()

    def migration(self, run_id: str, wave: int | None, target: str, strategy: str, rows_source: int | None,
                  rows_written: int | None, status: str, error: str | None = None):
        if not self.enabled:
            return
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"INSERT INTO {self._q('migration_log')} "
                            "(run_id, wave, target_table, strategy, rows_source, rows_written, status, error)"
                            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                            (run_id, wave, target, strategy, rows_source, rows_written, status, error))
            conn.commit()

    def _q(self, table: str) -> str:
        return f"{quote(self.schema, 'postgresql')}.{table}"


def _redact(url: str) -> str:
    if "@" not in url:
        return url
    scheme = url.split("://", 1)[0]
    host = url.split("@", 1)[1]
    return f"{scheme}://***@{host}"
