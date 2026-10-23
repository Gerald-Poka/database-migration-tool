"""DuckDB adapter regression coverage."""

import pytest
import yaml
from sqlalchemy import create_engine, text

pytest.importorskip("duckdb_engine")

from ferry.engine.executor import Executor
from ferry.engine.introspect import profile_engine
from ferry.engine.plan import Plan
from ferry.engine.sql import reflect_table, table_metadata
from ferry.engine.verify import verify_plan


def test_duckdb_roundtrip_and_metadata(tmp_path):
    source_path = tmp_path / "source.duckdb"
    target_path = tmp_path / "target.duckdb"
    source_url = f"duckdb:///{source_path}"
    target_url = f"duckdb:///{target_path}"
    source = create_engine(source_url)
    target = create_engine(target_url)
    for engine in (source, target):
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE departments (id INTEGER PRIMARY KEY, name VARCHAR UNIQUE)"))
            conn.execute(text(
                "CREATE TABLE employees (id INTEGER PRIMARY KEY, department_id INTEGER "
                "REFERENCES departments(id), name VARCHAR NOT NULL)"
            ))
    with source.begin() as conn:
        conn.execute(text("INSERT INTO departments VALUES (1, 'Engineering')"))
        conn.execute(text("INSERT INTO employees VALUES (10, 1, 'Ada')"))

    plan_data = {
        "version": 1,
        "source": {"url": source_url},
        "target": {"url": target_url},
        "meta": {"schema": False},
        "mappings": [
            {"source": "departments", "target": "departments", "strategy": "truncate"},
            {"source": "employees", "target": "employees", "strategy": "truncate"},
        ],
    }
    plan_path = tmp_path / "migration_plan.yaml"
    plan_path.write_text(yaml.safe_dump(plan_data, sort_keys=False), encoding="utf-8")

    metadata = table_metadata(target, "employees")
    assert metadata["primary_key"] == ["id"]
    assert metadata["foreign_keys"][0]["constrained_columns"] == ["department_id"]
    assert [column.name for column in reflect_table(source, "employees").columns] == [
        "id", "department_id", "name"
    ]
    assert "employees" in profile_engine(source)["schemas"]["main"]

    report = Executor(Plan.load(plan_path)).run()
    assert report["summary"]["status"] == "ok"
    assert verify_plan(Plan.load(plan_path))["status"] == "ok"

    with target.connect() as conn:
        assert conn.execute(text("SELECT id, name FROM departments")).all() == [
            (1, "Engineering")
        ]
        assert conn.execute(text("SELECT id, department_id, name FROM employees")).all() == [
            (10, 1, "Ada")
        ]

    with source.begin() as conn:
        conn.execute(text("UPDATE employees SET name = 'Grace' WHERE id = 10"))
    plan_data["mappings"] = [
        {"source": "employees", "target": "employees", "strategy": "merge", "conflict": "primary"}
    ]
    plan_path.write_text(yaml.safe_dump(plan_data, sort_keys=False), encoding="utf-8")
    report = Executor(Plan.load(plan_path)).run()
    assert report["summary"]["status"] == "ok"
    with target.connect() as conn:
        assert conn.execute(text("SELECT name FROM employees WHERE id = 10")).scalar_one() == "Grace"
