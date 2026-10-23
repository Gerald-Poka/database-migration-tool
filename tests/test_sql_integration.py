"""Optional live integration checks for supported database adapters."""

import os
import uuid

import pytest
import yaml
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    inspect,
    insert,
    select,
    text,
    update,
)

from ferry.engine.executor import Executor
from ferry.engine.introspect import profile_engine
from ferry.engine.plan import Plan
from ferry.engine.connect import make_engine, quote
from ferry.engine.sql import SOURCE_DIALECTS, TARGET_DIALECTS


def test_clickhouse_dialect_alias_is_source_only():
    clickhouse_dialects = {"clickhouse", "clickhousedb"}
    assert clickhouse_dialects <= SOURCE_DIALECTS
    assert clickhouse_dialects.isdisjoint(TARGET_DIALECTS)


@pytest.mark.parametrize(
    ("source_env", "target_env"),
    [
        ("FERRY_TEST_POSTGRES_URL", "FERRY_TEST_MYSQL_URL"),
        ("FERRY_TEST_MYSQL_URL", "FERRY_TEST_POSTGRES_URL"),
        ("FERRY_TEST_POSTGRES_URL", "FERRY_TEST_MARIADB_URL"),
        ("FERRY_TEST_MARIADB_URL", "FERRY_TEST_POSTGRES_URL"),
        ("FERRY_TEST_COCKROACH_URL", "FERRY_TEST_MYSQL_URL"),
        ("FERRY_TEST_MYSQL_URL", "FERRY_TEST_COCKROACH_URL"),
    ],
)
def test_live_cross_backend_roundtrip(tmp_path, source_env, target_env):
    source_url = os.environ.get(source_env)
    target_url = os.environ.get(target_env)
    if not source_url or not target_url:
        pytest.skip(f"set {source_env} and {target_env} to run this live integration test")

    source = make_engine(source_url)
    target = make_engine(target_url)
    suffix = uuid.uuid4().hex[:12]
    source_name = f"ferry_it_source_{suffix}"
    target_name = f"ferry_it_target_{suffix}"
    meta_schema = f"ferry_it_meta_{suffix}" if target.dialect.name == "postgresql" else False
    source_table = Table(
        source_name, MetaData(),
        Column("id", Integer, primary_key=True),
        Column("label", String(120), nullable=False),
    )
    target_table = Table(
        target_name, MetaData(),
        Column("id", Integer, primary_key=True),
        Column("label", String(120), nullable=False),
    )
    try:
        source_table.create(source)
        target_table.create(target)
        with source.begin() as conn:
            conn.execute(insert(source_table), [
                {"id": 1, "label": "source row"},
                {"id": 2, "label": "second row"},
            ])
        plan_path = tmp_path / "integration_plan.yaml"
        plan_data = {
            "version": 1,
            "source": {"url": source_url},
            "target": {"url": target_url},
            "meta": {"schema": meta_schema},
            "mappings": [{
                "source": source_name,
                "target": target_name,
                "strategy": "truncate",
            }],
        }
        plan_path.write_text(yaml.safe_dump(plan_data, sort_keys=False), encoding="utf-8")

        report = Executor(Plan.load(plan_path)).run()
        assert report["summary"]["status"] == "ok"
        if meta_schema:
            assert set(inspect(target).get_table_names(schema=meta_schema)) == {
                "migration_log", "repair_log", "run_log"
            }
        with target.connect() as conn:
            assert conn.execute(
                select(target_table.c.id, target_table.c.label).order_by(target_table.c.id)
            ).all() == [(1, "source row"), (2, "second row")]

        with source.begin() as conn:
            conn.execute(update(source_table).where(source_table.c.id == 1).values(label="updated"))
        plan_data["mappings"][0]["strategy"] = "merge"
        plan_data["mappings"][0]["conflict"] = "primary"
        plan_path.write_text(yaml.safe_dump(plan_data, sort_keys=False), encoding="utf-8")
        report = Executor(Plan.load(plan_path)).run()
        assert report["summary"]["status"] == "ok"
        with target.connect() as conn:
            assert conn.execute(
                select(target_table.c.id, target_table.c.label).order_by(target_table.c.id)
            ).all() == [(1, "updated"), (2, "second row")]
    finally:
        target_table.drop(target, checkfirst=True)
        source_table.drop(source, checkfirst=True)
        if meta_schema:
            with target.begin() as conn:
                conn.execute(text(f"DROP SCHEMA IF EXISTS {quote(meta_schema, 'postgresql')} CASCADE"))
        source.dispose()
        target.dispose()


def test_live_clickhouse_source_to_sqlite(tmp_path):
    source_url = os.environ.get("FERRY_TEST_CLICKHOUSE_URL")
    if not source_url:
        pytest.skip("set FERRY_TEST_CLICKHOUSE_URL to run the ClickHouse source test")

    source = make_engine(source_url)
    target = create_engine(f"sqlite:///{tmp_path / 'target.db'}")
    suffix = uuid.uuid4().hex[:12]
    source_name = f"ferry_it_source_{suffix}"
    target_name = f"ferry_it_target_{suffix}"
    target_table = Table(
        target_name, MetaData(),
        Column("id", Integer, primary_key=True),
        Column("label", String(120), nullable=False),
    )
    try:
        with source.begin() as conn:
            conn.execute(text(
                f"CREATE TABLE {source_name} (id UInt64, label String) "
                "ENGINE = MergeTree ORDER BY id"
            ))
            conn.execute(text(
                f"INSERT INTO {source_name} (id, label) VALUES (1, 'clickhouse row')"
            ))
        assert source_name in profile_engine(source, schemas=["default"])["schemas"]["default"]
        target_table.create(target)
        plan_path = tmp_path / "clickhouse_plan.yaml"
        plan_path.write_text(yaml.safe_dump({
            "version": 1,
            "source": {"url": source_url},
            "target": {"url": str(target.url)},
            "meta": {"schema": False},
            "mappings": [{
                "source": source_name,
                "target": target_name,
                "strategy": "truncate",
            }],
        }, sort_keys=False), encoding="utf-8")

        report = Executor(Plan.load(plan_path)).run()
        assert report["summary"]["status"] == "ok"
        with target.connect() as conn:
            assert conn.execute(
                select(target_table.c.id, target_table.c.label)
            ).all() == [(1, "clickhouse row")]
    finally:
        with source.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {source_name}"))
        target_table.drop(target, checkfirst=True)
        source.dispose()
        target.dispose()
