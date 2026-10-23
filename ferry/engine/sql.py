"""SQLAlchemy Core helpers shared by supported database adapters."""

from __future__ import annotations

from sqlalchemy import (
    Column,
    MetaData,
    Table,
    func,
    inspect,
    literal,
    select,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.sql import sqltypes

SCHEMA_DIALECTS = {
    "postgresql", "cockroachdb", "mysql", "mariadb", "mssql", "oracle", "duckdb",
    "clickhouse", "clickhousedb",
}
SOURCE_DIALECTS = SCHEMA_DIALECTS | {"sqlite"}
TARGET_DIALECTS = SOURCE_DIALECTS - {"clickhouse", "clickhousedb"}


def table_parts(engine: Engine, name: str) -> tuple[str | None, str]:
    if "." in name and engine.dialect.name in SCHEMA_DIALECTS:
        schema, _, table = name.rpartition(".")
        return schema or None, table
    return None, name


def reflect_table(engine: Engine, name: str) -> Table:
    schema, table_name = table_parts(engine, name)
    if engine.dialect.name == "duckdb":
        return _reflect_duckdb_table(engine, table_name, schema)
    return Table(table_name, MetaData(), schema=schema, autoload_with=engine)


def table_metadata(engine: Engine, name: str) -> dict:
    schema, table_name = table_parts(engine, name)
    if engine.dialect.name == "duckdb":
        table = reflect_table(engine, name)
        return table.info["ferry_metadata"]
    inspector = inspect(engine)
    inspector_schema = schema if engine.dialect.name in SCHEMA_DIALECTS else None
    return {
        "columns": inspector.get_columns(table_name, schema=inspector_schema),
        "primary_key": inspector.get_pk_constraint(
            table_name, schema=inspector_schema).get("constrained_columns") or [],
        "unique_constraints": inspector.get_unique_constraints(
            table_name, schema=inspector_schema),
        "foreign_keys": inspector.get_foreign_keys(table_name, schema=inspector_schema),
    }


def _reflect_duckdb_table(engine: Engine, name: str, schema: str | None) -> Table:
    schema_name = schema or "main"
    with engine.connect() as conn:
        columns = conn.execute(text(
            "SELECT column_name, data_type, is_nullable, column_default, is_identity "
            "FROM information_schema.columns "
            "WHERE table_schema = :schema AND table_name = :table "
            "ORDER BY ordinal_position"
        ), {"schema": schema_name, "table": name}).mappings().all()
        constraints = conn.execute(text(
            "SELECT constraint_type, constraint_column_names, referenced_table, "
            "referenced_column_names, constraint_name FROM duckdb_constraints() "
            "WHERE schema_name = :schema AND table_name = :table"
        ), {"schema": schema_name, "table": name}).mappings().all()
    if not columns:
        raise ValueError(f"DuckDB table {schema_name}.{name} does not exist")

    primary_key = next((list(row["constraint_column_names"]) for row in constraints
                        if row["constraint_type"] == "PRIMARY KEY"), [])
    unique_constraints = [
        {"name": row["constraint_name"], "column_names": list(row["constraint_column_names"])}
        for row in constraints if row["constraint_type"] == "UNIQUE"
    ]
    foreign_keys = [
        {
            "name": row["constraint_name"],
            "constrained_columns": list(row["constraint_column_names"]),
            "referred_table": row["referenced_table"],
            "referred_schema": schema_name,
            "referred_columns": list(row["referenced_column_names"]),
        }
        for row in constraints if row["constraint_type"] == "FOREIGN KEY"
    ]
    info = {
        "columns": [
            {
                "name": row["column_name"],
                "type": _duckdb_type(row["data_type"]),
                "nullable": row["is_nullable"] == "YES",
                "default": row["column_default"],
                "identity": row["is_identity"] == "YES",
                "autoincrement": row["is_identity"] == "YES",
                "computed": False,
            }
            for row in columns
        ],
        "primary_key": primary_key,
        "unique_constraints": unique_constraints,
        "foreign_keys": foreign_keys,
    }
    metadata = MetaData()
    table = Table(name, metadata, *[
        Column(
            column["name"],
            column["type"],
            nullable=column["nullable"],
            server_default=text(column["default"]) if column["default"] else None,
            primary_key=column["name"] in primary_key,
            autoincrement=column["autoincrement"],
        )
        for column in info["columns"]
    ], schema=schema)
    table.info["ferry_metadata"] = info
    return table


def _duckdb_type(data_type: str):
    normalized = data_type.upper()
    if normalized in ("BOOLEAN", "BOOL"):
        return sqltypes.Boolean()
    if normalized in ("TINYINT", "SMALLINT", "INTEGER", "INT", "UTINYINT", "USMALLINT", "UINTEGER"):
        return sqltypes.Integer()
    if normalized in ("BIGINT", "UBIGINT", "HUGEINT", "UHUGEINT"):
        return sqltypes.BigInteger()
    if normalized.startswith(("DECIMAL", "NUMERIC")):
        return sqltypes.Numeric()
    if normalized in ("FLOAT", "REAL", "DOUBLE"):
        return sqltypes.Float()
    if normalized == "DATE":
        return sqltypes.Date()
    if normalized.startswith("TIMESTAMP"):
        return sqltypes.DateTime(timezone="TZ" in normalized or "WITH TIME ZONE" in normalized)
    if normalized.startswith("TIME"):
        return sqltypes.Time(timezone="TZ" in normalized or "WITH TIME ZONE" in normalized)
    if normalized in ("BLOB", "BYTEA", "BIT"):
        return sqltypes.LargeBinary()
    if normalized == "JSON":
        return sqltypes.JSON()
    if normalized in ("UUID",):
        return sqltypes.Uuid()
    if normalized.startswith(("VARCHAR", "CHAR", "TEXT", "STRING")):
        return sqltypes.Text()
    return sqltypes.NullType()


def source_select(table: Table, columns: list[str], where: str | None = None,
                  limit: int | None = None):
    statement = select(*(table.c[name] for name in columns))
    if where:
        statement = statement.where(text(where))
    if limit is not None:
        statement = statement.limit(limit)
    return statement


def source_count(engine: Engine, table: Table, where: str | None = None,
                 limit: int | None = None) -> int:
    selected_rows = select(literal(1)).select_from(table)
    if where:
        selected_rows = selected_rows.where(text(where))
    if limit is not None:
        selected_rows = selected_rows.limit(limit)
    statement = select(func.count()).select_from(selected_rows.subquery())
    with engine.connect() as conn:
        return int(conn.execute(statement).scalar_one())
