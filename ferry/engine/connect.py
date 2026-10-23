"""ferry.engine.connect - URL handling, engine construction, identifier helpers."""

from __future__ import annotations

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url


def make_engine(url: str, defer_fks: bool = False) -> Engine:
    """Create a SQLAlchemy 2 engine.

    defer_fks starts connections with FK enforcement disabled for cycle-breaking waves:
    PG uses session_replication_role=replica (needs superuser), MySQL uses FOREIGN_KEY_CHECKS=0.
    """
    if make_url(url).get_backend_name() in ("clickhouse", "clickhousedb"):
        try:
            import clickhouse_connect.cc_sqlalchemy  # noqa: F401
        except ImportError as e:
            raise ValueError("ClickHouse source support requires clickhouse-connect") from e
    eng = create_engine(url, future=True, pool_pre_ping=True)

    def _connect(dbapi_conn, _rec):  # pragma: no cover - exercised only on real conns
        if eng.dialect.name == "postgresql":
            cur = dbapi_conn.cursor()
            if defer_fks:
                cur.execute("SET session_replication_role = replica")
            cur.execute("SET TIME ZONE 'UTC'")
            cur.close()
        elif eng.dialect.name == "mysql":
            if defer_fks:
                cur = dbapi_conn.cursor()
                cur.execute("SET FOREIGN_KEY_CHECKS=0")
                cur.close()

    event.listen(eng, "connect", _connect)
    return eng


def quote(target: str, dialect: str) -> str:
    """Quote a possibly schema-qualified identifier for `dialect`."""
    if dialect in ("mysql", "mariadb"):
        left, right = "`", "`"
    elif dialect == "mssql":
        left, right = "[", "]"
    else:
        left, right = '"', '"'
    return ".".join(
        f"{left}{part.replace(right, right * 2)}{right}" for part in target.split(".")
    )


def split_target(target: str) -> tuple[str | None, str]:
    """'schema.table' -> ('schema', 'table'); 'table' -> (None, 'table')."""
    schema, _, table = target.rpartition(".")
    return (schema or None, table)


def scalar(conn, sql: str) -> int:
    return int(conn.execute(text(sql)).scalar_one())  # type: ignore[arg-type,call-overload]


def count_select_sql(table: str, dialect: str, where: str | None = None,
                     limit: int | None = None) -> str:
    """Build a source count query with the same filters and cap as a row selection."""
    cond = f" WHERE {where}" if where else ""
    if limit is not None:
        return (f"SELECT COUNT(*) FROM (SELECT 1 FROM {quote(table, dialect)}{cond} "
                f"LIMIT {limit}) AS ferry_source_rows")
    return f"SELECT COUNT(*) FROM {quote(table, dialect)}{cond}"
