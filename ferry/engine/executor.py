"""ferry.engine.executor - deterministic plan execution.

Execution model:
- waves are topologically ordered on target FKs (plan `order:` overrides).
- truncate strategy: reverse-order DELETE pass over all truncate-mapped targets,
  then topological COPY/INSERT. PG bulk loads use COPY TEXT (fast, streaming).
- merge strategy: conflict-aware INSERT ... ON CONFLICT / ON DUPLICATE / OR REPLACE.
- re-runs resume from <plan>.ferry/<run_id>/checkpoint.json (done tables skipped).
- every run writes run_log/migration_log in the target's `ferry_meta` schema (PG).
"""

from __future__ import annotations

import io
import json
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import inspect

from .connect import count_select_sql, make_engine, quote, scalar, split_target
from .meta import MetaLog
from .plan import Plan, PlanError
from .transforms import compile_transforms
from .waves import plan_waves

OK, ERR = "ok", "error"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _copy_text(v) -> str:
    """Encode one field for PG COPY TEXT; plain \\N is COPY's null marker."""
    if v is None:
        return "\\N"
    if isinstance(v, bool):
        return "t" if v else "f"
    if isinstance(v, float):
        if v != v:  # noqa: PLR0124 nan != nan by design
            return "NaN"
        if v == float("inf"):
            return "Infinity"
        if v == float("-inf"):
            return "-Infinity"
        return repr(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return "\\x" + v.hex().replace("\\", "\\\\")
    if isinstance(v, datetime):
        s = v.isoformat(sep=" ")
    elif hasattr(v, "isoformat"):
        s = v.isoformat()
    else:
        s = str(v)
    return s.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


def _ph(n: int, dialect: str) -> str:
    p = "?" if dialect == "sqlite" else "%s"
    return ", ".join([p] * n)


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
        self.meta = MetaLog(lambda: self._tconn(), self.target.dialect.name,
                            plan.meta_schema, plan.meta_enabled)
        self.report: dict = {"run_id": self.run_id, "started_at": _now(), "mappings": {}}

    # -------------------------------------------------------------- infra helpers

    def _tconn(self):
        """Pooled target connection proxy (psycopg / pymysql / sqlite3)."""
        return self.target.raw_connection()

    def _sconn(self):
        return self.source.raw_connection()

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
        tinsp = inspect(self.target)
        dialect = self.target.dialect.name
        for m in self.plan.mappings:
            schema, table = split_target(m["target"])
            try:
                cols = tinsp.get_columns(table, schema=schema if dialect == "postgresql" else None)
            except Exception as e:
                raise PlanError(
                    f"target table {m['target']!r} not found on target (engine assumes the "
                    f"target schema is pre-created: {e!r})") from e
            m["_target_cols"] = [c["name"] for c in cols]
            m["_target_column_info"] = {c["name"]: c for c in cols}
            m["_target_pk"] = tinsp.get_pk_constraint(table, schema=schema)["constrained_columns"]
            m["_target_uniques"] = tinsp.get_unique_constraints(table, schema=schema)
            m["_target_fks"] = [
                {
                    "name": fk["name"],
                    "columns": fk["constrained_columns"],
                    "ref_table": (
                        f"{fk['referred_schema']}.{fk['referred_table']}"
                        if fk.get("referred_schema") else fk["referred_table"]
                    ),
                }
                for fk in tinsp.get_foreign_keys(table, schema=schema)
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
        sin = inspect(self.source)
        sdialect = self.source.dialect.name
        columns_by_source = {}
        out = {}
        for m in self.plan.mappings:
            src = m["source"]
            if src not in columns_by_source:
                schema, table = None, src
                if "." in src and sdialect == "postgresql":
                    schema, _, table = src.rpartition(".")
                columns_by_source[src] = [c["name"] for c in sin.get_columns(table, schema=schema)]
            count_sql = count_select_sql(src, sdialect, m.get("where"), m.get("limit"))
            with self.source.connect() as conn:
                count = scalar(conn, count_sql)
            out[m["target"]] = {"columns": columns_by_source[src], "count": count}
        return out

    # -------------------------------------------------------------- main loop

    def run(self) -> dict:
        self._log_magline(f"ferry {self.run_id}: {self.plan.summary()}")
        supported = {"postgresql", "mysql", "sqlite"}
        if self.source.dialect.name not in supported or self.target.dialect.name not in supported:
            raise PlanError(
                "supported database dialects are PostgreSQL, MySQL, and SQLite "
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
        dialect = self.target.dialect.name
        with self.target.begin() as txn:
            txn.exec_driver_sql(f"DELETE FROM {quote(target, dialect)}")

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
        select = self._select_sql(src_cols, m)
        tf = m["_compiled_transforms"]
        conflict_cols = None
        if m["strategy"] == "merge":
            conflict_cols = _conflict_cols(m["conflict"], m["_target_pk"], m["_target_uniques"], m["target"])
        if self.target.dialect.name == "postgresql" and m["strategy"] == "truncate":
            return self._load_pg_copy(m["target"], tgt_cols, select, src_cols, tf, m["chunk"])
        return self._load_executemany(m, tgt_cols, select, src_cols, tf, conflict_cols, m["chunk"])

    def _select_sql(self, src_cols: list[str], m: dict) -> str:
        dialect = self.source.dialect.name
        cols = ", ".join(quote(c, dialect) for c in src_cols)
        cond = f" WHERE {m['where']}" if m.get("where") else ""
        limit = f" LIMIT {int(m['limit'])}" if m.get("limit") is not None else ""
        return f"SELECT {cols} FROM {quote(m['source'], dialect)}{cond}{limit}"

    # -------------------------------------------------------------- loaders

    def _load_pg_copy(self, target: str, tgt_cols: list[str], select: str,
                      src_cols: list[str], tf: dict, chunk: int) -> int:
        dconn = self._tconn()
        sconn = self._sconn()
        cur = None
        try:
            if self.source.dialect.name == "mysql":
                from pymysql.cursors import SSCursor
                cur = sconn.cursor(SSCursor)
            else:
                cur = sconn.cursor()
            cur.execute(select)
            table_qual = quote(target, "postgresql")
            cols_qual = ", ".join(quote(c, "postgresql") for c in tgt_cols)
            total = 0
            while True:
                rows = cur.fetchmany(chunk)
                if not rows:
                    break
                sio = io.StringIO()
                for row in rows:
                    vals = []
                    for i, tc in enumerate(tgt_cols):
                        v = row[i]  # SELECT order == tgt_cols order (aligned in _cols)
                        fn = tf.get(tc)
                        if fn is not None:
                            v = fn(v)
                        vals.append(v)
                    sio.write("\t".join(_copy_text(v) for v in vals) + "\n")
                with dconn.cursor() as pcur:
                    with pcur.copy(f"COPY {table_qual} ({cols_qual}) FROM STDIN") as cp:
                        cp.write(sio.getvalue().encode())
                dconn.commit()
                total += len(rows)
            return total
        except Exception:
            dconn.rollback()
            raise
        finally:
            try:
                if cur is not None:
                    cur.close()
            finally:
                try:
                    sconn.close()
                finally:
                    dconn.close()

    def _load_executemany(self, m: dict, tgt_cols: list[str], select: str,
                          src_cols: list[str], tf: dict, conflict_cols, chunk: int) -> int:
        dialect = self.target.dialect.name
        table_qual = quote(m["target"], dialect)
        or_kw, upsert = "", ""
        if conflict_cols:
            if dialect == "postgresql":
                sets = ", ".join(f"{quote(c, dialect)} = EXCLUDED.{quote(c, dialect)}"
                                 for c in tgt_cols if c not in conflict_cols)
                upsert = ("ON CONFLICT (" + ", ".join(quote(c, dialect) for c in conflict_cols) + ")"
                          + (f" DO UPDATE SET {sets}" if sets else " DO NOTHING"))
            elif dialect == "mysql":
                sets = ", ".join(f"{quote(c, dialect)} = VALUES({quote(c, dialect)})"
                                 for c in tgt_cols if c not in conflict_cols)
                no_op = f"{quote(conflict_cols[0], dialect)} = {quote(conflict_cols[0], dialect)}"
                upsert = f"ON DUPLICATE KEY UPDATE {sets or no_op}"
            elif dialect == "sqlite":
                sets = ", ".join(f"{quote(c, dialect)} = excluded.{quote(c, dialect)}"
                                 for c in tgt_cols if c not in conflict_cols)
                upsert = ("ON CONFLICT (" + ", ".join(quote(c, dialect) for c in conflict_cols) + ")"
                          + (f" DO UPDATE SET {sets}" if sets else " DO NOTHING"))
            else:
                raise PlanError(f"merge is not supported for target dialect {dialect!r}")
        insert = (f"INSERT{or_kw} INTO {table_qual} ({_qlist(tgt_cols, dialect)}) "
                  f"VALUES ({_ph(len(tgt_cols), dialect)}) {upsert}".strip())
        sconn = self._sconn()
        dconn = self._tconn()
        cur = None
        try:
            if self.source.dialect.name == "mysql":
                from pymysql.cursors import SSCursor
                cur = sconn.cursor(SSCursor)
            else:
                cur = sconn.cursor()
            cur.execute(select)
            total = 0
            while True:
                rows = cur.fetchmany(chunk)
                if not rows:
                    break
                batch = []
                for row in rows:
                    vals = []
                    for i, tc in enumerate(tgt_cols):
                        v = row[i]
                        fn = tf.get(tc)
                        if fn is not None:
                            v = fn(v)
                        vals.append(v)
                    batch.append(tuple(vals))
                pcur = dconn.cursor()
                try:
                    pcur.executemany(insert, batch)
                finally:
                    pcur.close()
                dconn.commit()
                total += len(rows)
            return total
        except Exception:
            dconn.rollback()
            raise
        finally:
            try:
                if cur is not None:
                    cur.close()
            finally:
                try:
                    sconn.close()
                finally:
                    dconn.close()


def _qlist(cols: list[str], dialect: str) -> str:
    return ", ".join(quote(c, dialect) for c in cols)


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
