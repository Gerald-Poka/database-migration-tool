"""End-to-end round trip: sqlite source -> sqlite target through plan/run/verify."""


import pytest
import yaml
from sqlalchemy import create_engine, text

from ferry.engine.connect import quote
from ferry.engine.executor import Executor
from ferry.engine.plan import Plan
from ferry.engine.verify import verify_plan


def _fmt_insert(conn, table, *rows_values):
    cols = list(rows_values[0].keys())
    vals = [tuple(r[c] for c in cols) for r in rows_values]
    if not vals:
        return
    ph = ", ".join(["?"] * len(cols))
    conn.execute(text(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({ph})"), vals)


def _make_source(path):
    eng = create_engine(f"sqlite:///{path}")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE department (id INTEGER PRIMARY KEY, name TEXT)"))
        c.execute(text("CREATE TABLE employee (id INTEGER PRIMARY KEY, name TEXT, "
                       "dept_id INTEGER REFERENCES department(id), "
                       "joined TEXT, deleted INTEGER, email TEXT)"))
        c.execute(text("CREATE TABLE audit_old (id INTEGER PRIMARY KEY, note TEXT)"))
    with eng.begin() as c:
        _fmt_insert(c.statement if False else c.sqlalchemy_conn if False else c, "dummy") if False else None
    return eng


def _seed(path):
    eng = create_engine(f"sqlite:///{path}")
    with eng.begin() as c:
        c.execute(text("INSERT INTO department (id, name) VALUES (1, 'Eng')"))
        c.execute(text("INSERT INTO department (id, name) VALUES (2, 'Ops')"))
        rows = [
            (1, "Alice", 1, "2020-03-01", 0, "ALICE@x.io"),
            (2, "Bob", 2, None, 1, "BOB@x.io"),
            (3, "Cara", None, "2000-01-00", 0, None),
        ]
        for r in rows:
            c.execute(text("INSERT INTO employee (id, name, dept_id, joined, deleted, email) "
                           "VALUES (:a,:b,:d,:j,:de,:e)"),
                      dict(zip(("a", "b", "d", "j", "de", "e"), r, strict=True)))
    return eng


def _make_target(path):
    eng = create_engine(f"sqlite:///{path}")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE department (id INTEGER PRIMARY KEY, name TEXT)"))
        c.execute(text("CREATE TABLE employee (id INTEGER PRIMARY KEY, full_name TEXT, "
                       "dept_id INTEGER REFERENCES department(id), "
                       "joined TEXT, is_active INTEGER, email TEXT, "
                       "UNIQUE (email))"))
    return eng


def test_identifier_quoting_escapes_quote_character():
    assert quote('odd"name', "sqlite") == '"odd""name"'
    assert quote("odd`name", "mysql") == "`odd``name`"


@pytest.fixture()
def dbs(tmp_path):
    sp, tp = tmp_path / "src.db", tmp_path / "tgt.db"
    _make_source(sp)
    _seed(sp)
    _make_target(tp)
    plan = {
        "version": 1,
        "source": {"url": f"sqlite:///{sp}"},
        "target": {"url": f"sqlite:///{tp}"},
        "meta": {"schema": False},
        "mappings": [
            {"source": "department", "target": "department", "strategy": "truncate"},
            {"source": "employee", "target": "employee",
             "columns": {"name": "full_name"},
             "skip": ["deleted"],
             "transforms": {"email": "lower", "joined": "safe_date"},
             "strategy": "truncate",
             "where": "deleted = 0"},
        ],
    }
    plan_path = tmp_path / "migration_plan.yaml"
    with open(plan_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(plan, f, sort_keys=False)
    return plan_path, tp


def test_roundtrip_truncate(dbs):
    plan_path, tp = dbs
    pl = Plan.load(plan_path)
    report = Executor(pl, dry_run=True).run()
    assert report["waves"] == [
        {"wave": 0, "target": "department", "strategy": "truncate",
         "source": "department", "source_count": 2},
        {"wave": 1, "target": "employee", "strategy": "truncate", "source": "employee",
         "source_count": 2},  # where deleted=0 keeps 2 rows
    ]
    report = Executor(pl, echo=False).run()
    assert report["summary"]["status"] == "ok"
    assert report["mappings"]["employee"]["rows_written"] == 2

    tgt = create_engine(f"sqlite:///{tp}")
    with tgt.connect() as conn:
        emps = conn.execute(text("SELECT id, full_name, dept_id, joined, email, is_active FROM employee "
                                 "ORDER BY id")).fetchall()
    # transforms applied: lower email, partial date dropped
    assert emps[0][1] == "Alice" and emps[0][4] == "alice@x.io"
    assert emps[1][1] == "Cara" and emps[1][3] is None
    # checkpoint written per state dir convention
    ckpts = list((pl.state_dir() / report["run_id"]).parent.glob("*/checkpoint.json"))
    assert ckpts


def test_resume_uses_latest_checkpoint(dbs):
    plan_path, tp = dbs
    pl = Plan.load(plan_path)
    initial = Executor(pl, echo=False).run()
    resumed = Executor(pl, resume=True, echo=False).run()
    assert resumed["run_id"] == initial["run_id"]
    assert resumed["summary"]["rows_copied"] == 4

    tgt = create_engine(f"sqlite:///{tp}")
    with tgt.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM department")).scalar_one() == 2
        assert conn.execute(text("SELECT COUNT(*) FROM employee")).scalar_one() == 2


def test_limit_counts_match_rows_selected(dbs, tmp_path):
    plan_path, _ = dbs
    data = yaml.safe_load(plan_path.read_text())
    data["mappings"][1]["limit"] = 1
    limited_path = tmp_path / "limited_plan.yaml"
    limited_path.write_text(yaml.safe_dump(data, sort_keys=False))
    pl = Plan.load(limited_path)

    dry_run = Executor(pl, dry_run=True, echo=False).run()
    employee_wave = next(w for w in dry_run["waves"] if w["target"] == "employee")
    assert employee_wave["source_count"] == 1

    Executor(pl, echo=False).run()
    verification = verify_plan(pl)
    employee_count = next(c for c in verification["counts"] if c["mapping"] == "employee")
    assert employee_count["source_count"] == 1
    assert employee_count["target_count"] == 1
    assert employee_count["match"] is True


def test_invalid_transform_does_not_truncate_target(dbs):
    plan_path, tp = dbs
    target = create_engine(f"sqlite:///{tp}")
    with target.begin() as conn:
        conn.execute(text("INSERT INTO department (id, name) VALUES (99, 'keep me')"))

    plan_data = yaml.safe_load(plan_path.read_text())
    plan_data["mappings"][1]["transforms"] = {"email": "unknown_transform"}
    plan_path.write_text(yaml.safe_dump(plan_data, sort_keys=False))

    from ferry.engine.plan import PlanError

    with pytest.raises(PlanError, match="unknown builtin transform"):
        Executor(Plan.load(plan_path), echo=False).run()

    with target.connect() as conn:
        assert conn.execute(text("SELECT name FROM department WHERE id = 99")).scalar_one() == "keep me"


def test_required_target_column_is_detected_before_truncate(dbs):
    plan_path, tp = dbs
    target = create_engine(f"sqlite:///{tp}")
    with target.begin() as conn:
        conn.execute(text("ALTER TABLE department ADD COLUMN required_value TEXT NOT NULL"))
        conn.execute(text(
            "INSERT INTO department (id, name, required_value) VALUES (99, 'preserve', 'yes')"))

    from ferry.engine.plan import PlanError

    with pytest.raises(PlanError, match="required target columns"):
        Executor(Plan.load(plan_path), echo=False).run()

    with target.connect() as conn:
        assert conn.execute(
            text("SELECT required_value FROM department WHERE id = 99")).scalar_one() == "yes"


def test_plan_rejects_bad_container_shapes(tmp_path):
    plan_data = {
        "source": {"url": "sqlite://"},
        "target": {"url": "sqlite://"},
        "mappings": {"source": "t", "target": "t"},
    }
    path = tmp_path / "bad_plan.yaml"
    path.write_text(yaml.safe_dump(plan_data))
    from ferry.engine.plan import PlanError

    with pytest.raises(PlanError, match="mappings must be a list"):
        Plan.load(path)


def test_verify_fails_on_missing_source_table(dbs, tmp_path):
    plan_path, _ = dbs
    data = yaml.safe_load(plan_path.read_text())
    data["mappings"][0]["source"] = "missing_source_table"
    path = tmp_path / "missing_source.yaml"
    path.write_text(yaml.safe_dump(data))

    report = verify_plan(Plan.load(path))
    assert report["status"] == "error"
    assert any("error" in count for count in report["counts"])


def test_roundtrip_merge_conflict_email(dbs, tmp_path):
    plan_path, tp = dbs
    pl = Plan.load(plan_path)
    # preload a stale row that must be updated by the merge
    tgt = create_engine(f"sqlite:///{tp}")
    with tgt.begin() as c:
        c.execute(text("INSERT INTO employee (id, full_name, dept_id, is_active) "
                       "VALUES (1, 'STALE', 99, 0)"))
    merge_plan = dict(pl.data)
    merge_plan["mappings"] = [
        {"source": "department", "target": "department", "strategy": "truncate"},
        {"source": "employee", "target": "employee",
         "columns": {"name": "full_name", "deleted": "is_active"},
         "transforms": {"email": "lower", "joined": "safe_date", "is_active": "bool_int"},
         "strategy": "merge",
         "conflict": "primary"},
    ]
    p2 = tmp_path / "merge_plan.yaml"
    with open(p2, "w", encoding="utf-8") as f:
        yaml.safe_dump(merge_plan, f, sort_keys=False)
    pl2 = Plan.load(p2)
    report = Executor(pl2, echo=False).run()
    assert report["summary"]["status"] == "ok"
    with tgt.connect() as c:
        row = c.execute(text("SELECT id, full_name, is_active FROM employee WHERE id = 1")).fetchone()
        n = c.execute(text("SELECT COUNT(*) FROM employee")).fetchone()[0]
    assert row[1] == "Alice"
    assert row[2] == 0
    assert n == 3
    with tgt.begin() as c:
        c.execute(text("INSERT INTO employee (id, full_name, is_active) VALUES (4, 'target-only', 1)"))
    verification = verify_plan(pl2)
    employee_count = next(c for c in verification["counts"] if c["mapping"] == "employee")
    assert employee_count["comparison"] == "target_count >= source_count"
    assert employee_count["match"] is True


def test_fk_cycle_error():
    import tempfile

    from sqlalchemy import create_engine
    with tempfile.TemporaryDirectory() as d:
        p = create_engine(f"sqlite:///{d}/x.db")
        with p.begin() as c:
            c.execute(text("CREATE TABLE a (id INTEGER PRIMARY KEY, b_id INTEGER "
                           "REFERENCES b(id))"))
            c.execute(text("CREATE TABLE b (id INTEGER PRIMARY KEY, a_id INTEGER "
                           "REFERENCES a(id))"))
        plan = {"version": 1, "source": {"url": f"sqlite:///{d}/x.db"},
                "target": {"url": f"sqlite:///{d}/x.db"},
                "mappings": [{"source": "a", "target": "a"},
                             {"source": "b", "target": "b"}]}
        pf = f"{d}/plan.yaml"
        with open(pf, "w", encoding="utf-8") as f:
            yaml.safe_dump(plan, f)
        from ferry.engine.plan import PlanError
        with pytest.raises(PlanError):
            Executor(Plan.load(pf), echo=False).run()


def test_draft_plan():
    import tempfile

    from ferry.engine.introspect import profile_engine
    from ferry.engine.plan import draft_plan, dump_draft
    with tempfile.TemporaryDirectory() as d:
        src = create_engine(f"sqlite:///{d}/s.db")
        with src.begin() as c:
            c.execute(text("CREATE TABLE t1 (id INTEGER PRIMARY KEY)"))
            c.execute(text("CREATE TABLE t2 (id INTEGER PRIMARY KEY)"))
        tgt = create_engine(f"sqlite:///{d}/t.db")
        with tgt.begin() as c:
            c.execute(text("CREATE TABLE t1 (id INTEGER PRIMARY KEY)"))
        draft, comments = draft_plan(profile_engine(src), profile_engine(tgt),
                                     "sqlite:///s.db", "sqlite:///t.db")
        assert [m["source"] for m in draft["mappings"]] == ["t1"]
        assert any("t2" in c for c in comments)
        out = dump_draft(draft, comments, __import__("pathlib").Path(d) / "p.yaml")
        loaded = yaml.safe_load(out.read_text())
        assert loaded["mappings"][0]["target"] == "t1"


def test_draft_plan_qualifies_postgres_source_and_omits_ambiguous_names():
    from ferry.engine.plan import draft_plan

    source = {
        "dialect": "postgresql",
        "schemas": {
            "public": {"users": {"rowcount": 3}, "shared": {"rowcount": 1}},
            "legacy": {"shared": {"rowcount": 2}},
        },
    }
    target = {
        "dialect": "postgresql",
        "schemas": {
            "app": {"users": {}, "shared": {}},
            "archive": {"old_users": {}},
        },
    }

    draft, comments = draft_plan(source, target, "postgresql://source", "postgresql://target")
    assert draft["mappings"] == [
        {"source": "public.users", "target": "app.users", "strategy": "truncate"}
    ]
    assert any("shared" in comment for comment in comments)
