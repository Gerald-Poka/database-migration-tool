"""ferry.engine.introspect - source/target schema profiling evidence."""

from __future__ import annotations

from sqlalchemy import func, inspect, select, text
from sqlalchemy.engine import Engine

from .sql import SCHEMA_DIALECTS, reflect_table, table_metadata

DEFAULT_SKIP_SCHEMAS = {"information_schema", "pg_catalog", "performance_schema", "sys", "mysql"}


def profile_engine(eng: Engine, schemas: list[str] | None = None, sample: int = 0) -> dict:
    """Build the profiling evidence dict for one engine.

    schemas: restrict inspection (PG); None -> every user schema (system schemas skipped).
    sample: if > 0, per table read up to N rows to add null_rate per column and a tiny
    sample of values (used by agents to spot dummies/epochs/messy columns).
    """
    insp = inspect(eng)
    dialect = eng.dialect.name
    if schemas is None:
        if dialect == "postgresql":
            with eng.connect() as conn:
                rows = conn.execute(text(
                    "SELECT nspname FROM pg_namespace "
                    "WHERE nspname NOT IN ('information_schema','pg_catalog') "
                    "AND nspname NOT LIKE 'pg_%' AND nspname NOT LIKE 'ferry_%'")).fetchall()
            schemas = [r[0] for r in rows]
        elif dialect == "sqlite":
            schemas = [None]
        elif dialect in ("mysql", "mariadb"):
            schemas = [None]
        elif dialect == "duckdb":
            with eng.connect() as conn:
                rows = conn.execute(text(
                    "SELECT schema_name FROM information_schema.schemata "
                    "WHERE schema_name NOT IN ('information_schema', 'pg_catalog')"
                )).fetchall()
            schemas = [row[0] for row in rows]
        else:
            try:
                schemas = [schema for schema in insp.get_schema_names()
                           if schema.lower() not in DEFAULT_SKIP_SCHEMAS]
            except Exception:
                schemas = [None]

    profile: dict = {"dialect": dialect, "schemas": {}, "url_redacted": _redact(eng.url)}
    for schema in schemas:
        tables: dict[str, dict] = {}
        try:
            names = insp.get_table_names(schema=schema)
        except Exception:
            names = insp.get_table_names()
        for tname in names:
            t: dict = {}
            metadata = table_metadata(
                eng, f"{schema}.{tname}" if schema and dialect in SCHEMA_DIALECTS else tname)
            cols = metadata["columns"]
            t["rowcount"] = _row_count(eng, schema, tname)
            t["columns"] = [
                {"name": c["name"], "type": str(c["type"]), "nullable": c.get("nullable", True)}
                for c in cols
            ]
            t["primary_key"] = metadata["primary_key"]
            t["foreign_keys"] = [
                {
                    "name": fk["name"],
                    "columns": fk["constrained_columns"],
                    "ref_table": fk["referred_table"],
                    "ref_columns": fk["referred_columns"],
                    "ref_schema": fk.get("referred_schema", schema),
                }
                for fk in metadata["foreign_keys"]
            ]
            t["unique_constraints"] = [
                {
                    "name": u["name"],
                    "columns": u.get("column_names", u.get("constrained_columns", [])),
                }
                for u in metadata["unique_constraints"]
            ]
            if sample > 0:
                _add_sample(eng, schema, tname, [c["name"] for c in cols], t, sample)
            tables[tname] = t
        profile["schemas"][schema or "_default"] = tables
    return profile


def _row_count(eng: Engine, schema, table: str) -> int:
    try:
        table_name = f"{schema}.{table}" if schema and eng.dialect.name in SCHEMA_DIALECTS else table
        reflected = reflect_table(eng, table_name)
        with eng.connect() as conn:
            return int(conn.execute(select(func.count()).select_from(reflected)).scalar_one())
    except Exception:
        return -1


def _add_sample(eng: Engine, schema, table, col_names, t: dict, n: int) -> None:
    try:
        table_name = f"{schema}.{table}" if schema and eng.dialect.name in SCHEMA_DIALECTS else table
        reflected = reflect_table(eng, table_name)
        statement = select(*(reflected.c[column] for column in col_names)).limit(int(n))
        with eng.connect() as conn:
            rows = conn.execute(statement).fetchall()
        nulls: dict[str, int] = {c: 0 for c in col_names}
        for row in rows:
            for c, v in zip(col_names, row, strict=True):
                if v is None:
                    nulls[c] += 1
        t["null_rate"] = {c: round(nulls[c] / len(rows), 3) if rows else 0.0 for c in col_names}
        t["sample"] = [
            {c: (None if v is None else str(v)[:60]) for c, v in zip(col_names, row, strict=True)}
            for row in rows[:3]
        ]
    except Exception as e:
        t["null_rate"] = {}
        t["sample"] = []
        t["sample_error"] = repr(e)


def _redact(url) -> str:
    s = str(url)
    if "@" in s:
        scheme = s.split("://", maxsplit=1)[0]
        host = s.split("@", 1)[1]
        return f"{scheme}://***@{host}"
    return s
