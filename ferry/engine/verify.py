"""ferry.engine.verify - post-migration verification evidence."""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import and_, func, select

from .sql import reflect_table, source_count, table_metadata
from .connect import make_engine, split_target
from .plan import Plan


def verify_plan(plan: Plan, report_dir: Path | None = None) -> dict:
    """Two checks:
    1. source/target count checks per mapping (respecting where/limit and strategy);
    2. FK orphan sweep across every FK of the target tables in the plan.
    Returns {counts: [...], orphans: [...], status: ok|error} and saves an evidence file.
    """
    src = make_engine(plan.source_url)
    tgt = make_engine(plan.target_url)
    counts, orphans = [], []

    for m in plan.mappings:
        try:
            source_table = reflect_table(src, m["source"])
        except Exception as e:  # noqa: BLE001 - evidence gatherer must not die on one table
            counts.append({"mapping": m["target"], "source": m["source"], "error": repr(e),
                           "target_count": None})
            continue
        try:
            src_n = source_count(src, source_table, m.get("where"), m.get("limit"))
        except Exception as e:
            src_n = f"error: {e!r}"
        try:
            target_table = reflect_table(tgt, m["target"])
            with tgt.connect() as conn:
                tgt_n = conn.execute(select(func.count()).select_from(target_table)).scalar_one()
        except Exception as e:
            tgt_n = f"error: {e!r}"
        entry = {"mapping": m["target"], "source": m["source"], "source_count": src_n,
                 "target_count": tgt_n}
        if isinstance(src_n, int) and isinstance(tgt_n, int):
            if m.get("strategy", "truncate") == "merge":
                entry["comparison"] = "target_count >= source_count"
                entry["match"] = tgt_n >= src_n
            else:
                entry["comparison"] = "target_count == source_count"
                entry["match"] = tgt_n == src_n
        counts.append(entry)

    for m in plan.mappings:
        try:
            fks = table_metadata(tgt, m["target"])["foreign_keys"]
        except Exception as e:
            orphans.append({"child": m["target"], "fk": None, "ref": None,
                            "error": f"could not inspect target foreign keys: {e!r}"})
            continue
        for fk in fks:
            if not fk.get("constrained_columns") or not fk.get("referred_columns"):
                orphans.append({"child": m["target"], "fk": fk.get("name"), "ref": fk.get("referred_table"),
                                "error": "foreign key metadata has no constrained or referred columns"})
                continue
            _, tschema = split_target(m["target"])
            ref_schema = fk.get("referred_schema", tschema)
            try:
                child = reflect_table(tgt, m["target"])
                ref_name = (f"{ref_schema}.{fk['referred_table']}" if ref_schema
                            else fk["referred_table"])
                parent = reflect_table(tgt, ref_name).alias("ferry_fk_parent")
                join = and_(*(child.c[child_col] == parent.c[parent_col]
                              for child_col, parent_col in zip(
                                  fk["constrained_columns"], fk["referred_columns"], strict=True)))
                all_child_values_present = and_(
                    *(child.c[column].is_not(None) for column in fk["constrained_columns"]))
                stmt = (select(func.count())
                        .select_from(child.outerjoin(parent, join))
                        .where(all_child_values_present,
                               parent.c[fk["referred_columns"][0]].is_(None)))
                with tgt.connect() as conn:
                    n = conn.execute(stmt).scalar_one()
                orphans.append({"child": m["target"], "fk": fk["name"], "ref": fk["referred_table"],
                                "orphans": n})
            except Exception as e:
                orphans.append({"child": m["target"], "fk": fk["name"], "ref": fk["referred_table"],
                                "orphans": f"error: {e!r}"})
    status = "ok"
    for c in counts:
        if c.get("match") is False or c.get("error") or any(
                isinstance(c.get(key), str) and c[key].startswith("error:")
                for key in ("source_count", "target_count")):
            status = "error"
    for o in orphans:
        n = o.get("orphans")
        if (isinstance(n, int) and n > 0) or (isinstance(n, str) and n.startswith("error")) or o.get("error"):
            status = "error"
    report = {"status": status, "counts": counts, "fk_orphans": orphans}
    if report_dir:
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "verify.json").write_text(json.dumps(report, indent=1))
    return report
