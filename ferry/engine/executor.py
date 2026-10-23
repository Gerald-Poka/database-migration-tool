"""ferry.engine.executor - deterministic plan execution.

Execution model:
- waves are topologically ordered on target FKs (plan `order:` overrides).
- truncate strategy: reverse-order DELETE pass over all truncate-mapped targets,
  then topological streaming INSERT batches.
- merge strategy: conflict-aware INSERT ... ON CONFLICT / ON DUPLICATE / OR REPLACE.
- re-runs resume from <plan>.ferry/<run_id>/checkpoint.json (done tables skipped).
- every run writes run_log/migration_log in the target's `ferry_meta` schema (PG).
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import and_, delete, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .connect import make_engine
from .meta import MetaLog
from .plan import Plan, PlanError
from .sql import (
    SOURCE_DIALECTS,
    TARGET_DIALECTS,
    reflect_table,
    source_count,
    source_select,
    table_metadata,
)
from .transforms import compile_transforms
from .waves import plan_waves

OK, ERR = "ok", "error"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Executor:
    def __init__(self, plan: Plan, resume: bool = False, run_id: str | None = None,
                 dry_run: bool = False, echo: bool = False):
        self.plan = plan
        self.echo = echo
        self.dry_run = dry_run
        if run_id is None and resume:
            state_dir = plan.state_dir()
            candidates = [p for p in state_dir.iterdir()
                          if p.is_dir() and (p / "checkpoint.json").is_file()] if state_dir.exists() else []
            if candidates:
                run_id = max(candidates,
                             key=lambda p: (p / "checkpoint.json").stat().st_mtime_ns).name
        self.run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.state_dir = plan.state_dir() / self.run_id
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_file = self.state_dir / "checkpoint.json"
        self.checkpoints: dict = {}
        if resume and self.checkpoint_file.exists():
            self.checkpoints = json.loads(self.checkpoint_file.read_text())
        self.source = make_engine(plan.source_url)
        self.target = make_engine(plan.target_url)
        self.meta = MetaLog(lambda: self.target.raw_connection(), self.target.dialect.name,
                            plan.meta_schema, plan.meta_enabled)
        self.report: dict = {"run_id": self.run_id, "started_at": _now(), "mappings": {}}

    # -------------------------------------------------------------- infra helpers

    def _log_magline(self, *a):
        if self.echo:
            print(*a)

    def _write_checkpoint(self):
        tmp = self.checkpoint_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.checkpoints, indent=1))
        tmp.replace(self.checkpoint_file)

    def _write_report(self):
        (self.state_dir / "report.json").write_text(json.dumps(self.report, indent=1, default=str))

    # -------------------------------------------------------------- waves

    def _waves(self) -> list[list[dict]]:
        """Attach target evidence to each mapping and return waves (lists of mappings)."""
        for m in self.plan.mappings:
            try:
                target_table = reflect_table(self.target, m["target"])
            except Exception as e:
                raise PlanError(
                    f"target table {m['target']!r} not found on target (engine assumes the "
                    f"target schema is pre-created: {e!r})") from e
            evidence = table_metadata(self.target, m["target"])
            cols = evidence["columns"]
            m["_target_table"] = target_table
            m["_target_cols"] = [c["name"] for c in cols]
            m["_target_column_info"] = {c["name"]: c for c in cols}
            m["_target_pk"] = evidence["primary_key"]
            m["_target_uniques"] = evidence["unique_constraints"]
            m["_target_fks"] = [
                {
                    "name": fk["name"],
                    "columns": fk["constrained_columns"],
                    "ref_table": (
                        f"{fk['referred_schema']}.{fk['referred_table']}"
                        if fk.get("referred_schema") else fk["referred_table"]
                    ),
                }
                for fk in evidence["foreign_keys"]
            ]
        if self.plan.order:
            order = self.plan.order
            if sorted(order) != sorted(self.plan.targets()):
                raise PlanError("plan.order must list exactly the plan's mapping targets")
            return [[m for m in self.plan.mappings if m["target"] == t] for t in order]
        try:
            return plan_waves(self.plan.mappings)[0]
        except ValueError as e:
            raise PlanError(str(e)) from e

    def _inspect_sources(self) -> dict:
        tables_by_source = {}
        out = {}
        for m in self.plan.mappings:
            src = m["source"]
            if src not in tables_by_source:
                tables_by_source[src] = reflect_table(self.source, src)
            source_table = tables_by_source[src]
            count = source_count(self.source, source_table, m.get("where"), m.get("limit"))
            out[m["target"]] = {
                "columns": [column.name for column in source_table.columns],
                "count": count,
                "_table": source_table,
            }
        return out

    # -------------------------------------------------------------- main loop

    def run(self) -> dict:
        self._log_magline(f"ferry {self.run_id}: {self.plan.summary()}")
        if (self.source.dialect.name not in SOURCE_DIALECTS
                or self.target.dialect.name not in TARGET_DIALECTS):
            raise PlanError(
                "supported source dialects are PostgreSQL, MySQL/MariaDB, SQLite, SQL Server, "
                "Oracle, CockroachDB, and DuckDB, plus ClickHouse (clickhousedb) as a source. "
                "ClickHouse is not "
                "a supported target. "
                f"(got source={self.source.dialect.name!r}, target={self.target.dialect.name!r})")
        waves = self._waves()
        sources = self._inspect_sources()
        for m in self.plan.mappings:
            self._preflight_mapping(m, sources[m["target"]])
        if self.dry_run:
            self.report["waves"] = [
                {"wave": wi, "target": m["target"], "strategy": m["strategy"],
                 "source": m["source"], "source_count": sources[m["target"]]["count"]}
                for wi, wave in enumerate(waves) for m in wave
            ]
            self._write_report()
            return self.report
        self.meta.ensure_schema()
        self.meta.run_start(self.run_id, self.plan.source_url, self.plan.target_url)
        flat = [m for wave in waves for m in wave]
        for m in reversed(flat):
            if self.checkpoints.get(m["target"], {}).get("status") == "done":
                continue
            if m["strategy"] == "truncate":
                self._truncate(m["target"])
        summary = {"waves": len(waves), "tables_migrated": 0, "rows_copied": 0, "errors": []}
        status = OK
        for wi, wave in enumerate(waves):
            for m in wave:
                done = self.checkpoints.get(m["target"], {}).get("status") == "done"
                if done:
                    self._log_magline(f" wave {wi}: {m['target']} resume-skip (done)")
                    summary["tables_migrated"] += 1
                    checkpoint = self.checkpoints[m["target"]]
                    summary["rows_copied"] += checkpoint.get("rows_written", 0)
                    self.report["mappings"][m["target"]] = checkpoint
                    continue
                started = time.monotonic()
                try:
                    n = self._migrate(wi, m, sources[m["target"]])
                except Exception as e:
                    status = "error" if status == OK else status
                    summary["errors"].append(f"{m['target']}: {e!r}")
                    self.meta.migration(self.run_id, wi, m["target"], m["strategy"],
                                        sources[m["target"]]["count"], None, ERR, repr(e))
                    self._write_checkpoint()
                    if self.plan.data.get("on_error", "abort") == "abort":
                        self.report["summary"] = summary
                        self._write_report()
                        self.meta.run_finish(self.run_id, "aborted", self.report)
                        raise PlanError(f"wave {wi} {m['target']} failed; use plan on_error: continue "
                                        f"to keep going or fix the plan - {e!r}") from e
                    continue
                secs = round(time.monotonic() - started, 2)
                summary["tables_migrated"] += 1
                summary["rows_copied"] += n
                self.checkpoints[m["target"]] = {"status": "done", "wave": wi,
                                                 "rows_written": n, "seconds": secs,
                                                 "strategy": m["strategy"], "finished_at": _now()}
                self.report["mappings"][m["target"]] = self.checkpoints[m["target"]]
                self.meta.migration(self.run_id, wi, m["target"], m["strategy"],
                                    sources[m["target"]]["count"], n, OK)
                self._write_checkpoint()
                self._log_magline(f" wave {wi}: {m['target']} {m['strategy']} rows={n} ({secs}s)")
        summary["status"] = status
        self.report["summary"] = summary
        self._write_report()
        self.meta.run_finish(self.run_id, status, self.report)
        return self.report

    # -------------------------------------------------------------- delete pass

    def _truncate(self, target: str):
        with self.target.begin() as txn:
            txn.execute(delete(reflect_table(self.target, target)))

    # -------------------------------------------------------------- per mapping

    def _cols(self, m: dict, sin: dict) -> tuple[list[str], list[str]]:
        skip = set(m.get("skip") or [])
        col_map = m.get("columns") or {}
        bad = {k: v for k, v in col_map.items() if not isinstance(v, str)}
        if bad:
            raise PlanError(f"{m['target']}: columns values must be target col names (bad: {bad})")
        src_cols, tgt_cols = [], []
        for sc in sin["columns"]:
            if sc in skip:
                continue
            src_cols.append(sc)
            tgt_cols.append(col_map.get(sc, sc))
        if not src_cols:
            raise PlanError(f"{m['target']}: mapping selects no source columns")
        unknown = sorted(col_map.keys() - set(sin["columns"]))
        if unknown:
            raise PlanError(f"{m['target']}: columns references source cols missing in source: {unknown}")
        bad_targets = sorted(set(tgt_cols) - set(m["_target_cols"]))
        if bad_targets:
            raise PlanError(f"{m['target']}: columns target cols not present in target table: {bad_targets}")
        if len(set(tgt_cols)) != len(tgt_cols):
            raise PlanError(f"{m['target']}: multiple source columns map to the same target column")
        return src_cols, tgt_cols

    def _preflight_mapping(self, m: dict, source_info: dict) -> None:
        _, tgt_cols = self._cols(m, source_info)
        supplied = set(tgt_cols)
        required_missing = []
        for name, column in m["_target_column_info"].items():
            if name in supplied or column.get("nullable", True) or column.get("default") is not None:
                continue
            auto_generated = column.get("identity") or column.get("computed")
            auto_generated = auto_generated or column.get("autoincrement") in (True, "auto")
            if auto_generated:
                continue
            if (self.target.dialect.name == "sqlite" and name in m["_target_pk"]
                    and len(m["_target_pk"]) == 1 and str(column["type"]).upper() == "INTEGER"):
                continue
            required_missing.append(name)
        if required_missing:
            raise PlanError(
                f"{m['target']}: required target columns have no source mapping or default: "
                f"{sorted(required_missing)}")
        transforms = m.get("transforms") or {}
        unknown_transforms = sorted(set(transforms) - set(tgt_cols))
        if unknown_transforms:
            raise PlanError(
                f"{m['target']}: transforms reference target columns not loaded: {unknown_transforms}")
        try:
            m["_compiled_transforms"] = compile_transforms(transforms, self.plan.path.parent)
        except (ImportError, OSError, AttributeError, TypeError, ValueError) as e:
            raise PlanError(f"{m['target']}: invalid transform configuration: {e}") from e
        if m["strategy"] == "merge":
            conflict_cols = _conflict_cols(
                m["conflict"], m["_target_pk"], m["_target_uniques"], m["target"])
            if conflict_cols:
                invalid = sorted(set(conflict_cols) - set(m["_target_cols"]))
                if invalid:
                    raise PlanError(
                        f"{m['target']}: conflict columns not present in target: {invalid}")

    def _migrate(self, wi: int, m: dict, sin: dict) -> int:
        src_cols, tgt_cols = self._cols(m, sin)
        statement = source_select(sin["_table"], src_cols, m.get("where"), m.get("limit"))
        tf = m["_compiled_transforms"]
        conflict_cols = None
        if m["strategy"] == "merge":
            conflict_cols = _conflict_cols(m["conflict"], m["_target_pk"], m["_target_uniques"], m["target"])
        target_table = m["_target_table"]
        total = 0
        with self.source.connect().execution_options(
                stream_results=True, yield_per=m["chunk"]) as source_conn:
            result = source_conn.execute(statement)
            try:
                while rows := result.fetchmany(m["chunk"]):
                    batch = []
                    for row in rows:
                        values = {}
                        for source_col, target_col in zip(src_cols, tgt_cols, strict=True):
                            value = row._mapping[source_col]
                            transform = tf.get(target_col)
                            values[target_col] = transform(value) if transform is not None else value
                        batch.append(values)
                    with self.target.begin() as target_conn:
                        if conflict_cols:
                            self._merge_batch(target_conn, target_table, batch, conflict_cols)
                        else:
                            target_conn.execute(target_table.insert(), batch)
                    total += len(batch)
            finally:
                result.close()
        return total

    def _merge_batch(self, conn, table, rows: list[dict], conflict_cols: list[str]) -> None:
        dialect = self.target.dialect.name
        if not set(conflict_cols).issubset(rows[0]):
            raise PlanError(
                f"{table.fullname}: merge conflict columns must be included in the loaded columns")
        if dialect in ("postgresql", "cockroachdb", "sqlite", "duckdb"):
            if dialect == "sqlite":
                insert_factory = sqlite_insert
            elif dialect == "duckdb":
                try:
                    from duckdb_engine import insert as insert_factory
                except ImportError as e:
                    raise PlanError("DuckDB merge requires the duckdb-engine package") from e
            else:
                insert_factory = postgresql_insert
            statement = insert_factory(table)
            updates = {key: statement.excluded[key] for key in rows[0] if key not in conflict_cols}
            statement = (statement.on_conflict_do_update(
                index_elements=[table.c[key] for key in conflict_cols], set_=updates)
                if updates else statement.on_conflict_do_nothing(
                    index_elements=[table.c[key] for key in conflict_cols]))
            conn.execute(statement, rows)
            return
        if dialect in ("mysql", "mariadb"):
            statement = mysql_insert(table)
            updates = {key: statement.inserted[key] for key in rows[0] if key not in conflict_cols}
            if not updates:
                key = conflict_cols[0]
                updates[key] = table.c[key]
            conn.execute(statement.on_duplicate_key_update(**updates), rows)
            return

        mutable_cols = [key for key in rows[0] if key not in conflict_cols]
        for row in rows:
            predicate = and_(*(table.c[key] == row[key] for key in conflict_cols))
            exists = conn.execute(select(*[table.c[key] for key in conflict_cols])
                                  .where(predicate).limit(1)).first()
            if exists:
                if mutable_cols:
                    conn.execute(update(table).where(predicate).values(
                        {key: row[key] for key in mutable_cols}))
            else:
                conn.execute(table.insert().values(row))


def _conflict_cols(conflict, pk: list[str], uniques: list[dict], target: str) -> list[str] | None:
    if conflict in (None, "off"):
        return None
    if conflict == "primary":
        if not pk:
            raise PlanError(f"{target}: conflict=primary but target has no PRIMARY KEY")
        return list(pk)
    if isinstance(conflict, str) and conflict.startswith("columns:"):
        cols = [c.strip() for c in conflict.split(":", 1)[1].split(",") if c.strip()]
        if not cols or len(cols) != len(set(cols)):
            raise PlanError(f"{target}: conflict=columns requires distinct, non-empty column names")
        return cols
    if isinstance(conflict, str) and conflict.startswith("unique:"):
        name = conflict.split(":", 1)[1]
        for u in uniques:
            if u["name"] == name:
                cols = u.get("columns") or u.get("column_names") or []
                if cols:
                    return list(cols)
        raise PlanError(f"{target}: no unique constraint named {name!r}")
    raise PlanError(f"{target}: unknown conflict spec {conflict!r} "
                    "(primary | unique:<name> | columns:<a,b> | off)")
