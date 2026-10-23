"""ferry.engine.plan - migration_plan.yaml model, validation, draft generation."""

from __future__ import annotations

from pathlib import Path

import yaml


class PlanError(Exception):
    pass


class Plan:
    def __init__(self, path: Path, data: dict):
        if not isinstance(data, dict):
            raise PlanError("plan document must be a mapping")
        self.path = path
        self.data = data
        source = data.get("source")
        target = data.get("target")
        if not isinstance(source, dict):
            raise PlanError("source must be a mapping containing url")
        if not isinstance(target, dict):
            raise PlanError("target must be a mapping containing url")
        self.source_url: str = source.get("url", "")
        self.target_url: str = target.get("url", "")
        if not isinstance(self.source_url, str) or not isinstance(self.target_url, str):
            raise PlanError("source.url and target.url must be strings")
        if not self.source_url:
            raise PlanError("source.url missing in plan")
        if not self.target_url:
            raise PlanError("target.url missing in plan")
        mappings = data.get("mappings")
        if not isinstance(mappings, list):
            raise PlanError("plan.mappings must be a list")
        self.mappings: list[dict] = mappings
        self.order: list[str] | None = data.get("order")
        if self.order is not None and (
                not isinstance(self.order, list) or not all(isinstance(t, str) for t in self.order)):
            raise PlanError("plan.order must be a list of target names")
        meta = data.get("meta", {})
        if not isinstance(meta, dict):
            raise PlanError("plan.meta must be a mapping")
        self.meta_schema = meta.get("schema", "ferry_meta")
        if self.meta_schema is not False and not isinstance(self.meta_schema, str):
            raise PlanError("meta.schema must be a string or false")
        self.meta_enabled = self.meta_schema is not False
        on_error = data.get("on_error", "abort")
        if on_error not in ("abort", "continue"):
            raise PlanError("plan.on_error must be 'abort' or 'continue'")
        self._validate()

    @classmethod
    def load(cls, path: str | Path) -> Plan:
        path = Path(path).resolve()
        if not path.exists():
            raise PlanError(f"plan file not found: {path}")
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls(path, data)

    def _validate(self):
        seen = set()
        for i, m in enumerate(self.mappings):
            if not isinstance(m, dict):
                raise PlanError(f"mappings[{i}] is not a dict")
            for key in ("source", "target"):
                if not isinstance(m.get(key), str) or not m.get(key):
                    raise PlanError(f"mappings[{i}] missing '{key}'")
            t = m["target"]
            if t in seen:
                raise PlanError(f"duplicate mapping target {t!r}")
            seen.add(t)
            strategy = m.get("strategy", "truncate")
            if strategy not in ("truncate", "merge"):
                raise PlanError(f"mappings[{i}]: unknown strategy {strategy!r} (truncate|merge)")
            m["strategy"] = strategy
            conf = m.get("conflict", "primary" if strategy == "merge" else None)
            m["conflict"] = conf
            chunk = _plan_integer(m.get("chunk", 20_000), f"mappings[{i}]: chunk")
            if chunk <= 0:
                raise PlanError(f"mappings[{i}]: chunk must be > 0")
            m["chunk"] = chunk
            if m.get("where") is not None and not isinstance(m["where"], str):
                raise PlanError(f"mappings[{i}]: where must be a string")
            if m.get("skip") is not None and (
                    not isinstance(m["skip"], list) or not all(isinstance(c, str) for c in m["skip"])):
                raise PlanError(f"mappings[{i}]: skip must be a list of source column names")
            if m.get("columns") is not None and not isinstance(m["columns"], dict):
                raise PlanError(f"mappings[{i}]: columns must be a source-to-target mapping")
            if m.get("columns") is not None and not all(
                    isinstance(src, str) and isinstance(tgt, str)
                    for src, tgt in m["columns"].items()):
                raise PlanError(f"mappings[{i}]: columns keys and values must be strings")
            if m.get("transforms") is not None and not isinstance(m["transforms"], dict):
                raise PlanError(f"mappings[{i}]: transforms must be a target-column mapping")
            if m.get("transforms") is not None and not all(
                    isinstance(column, str) for column in m["transforms"]):
                raise PlanError(f"mappings[{i}]: transform column names must be strings")
            if m.get("limit") is not None:
                limit = _plan_integer(m["limit"], f"mappings[{i}]: limit")
                if limit < 0:
                    raise PlanError(f"mappings[{i}]: limit must be an integer >= 0")
                m["limit"] = limit

    def state_dir(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".ferry")

    def targets(self) -> list[str]:
        return [m["target"] for m in self.mappings]

    def summary(self) -> str:
        strategies = {}
        for m in self.mappings:
            strategies[m["strategy"]] = strategies.get(m["strategy"], 0) + 1
        line = ", ".join(f"{k}={v}" for k, v in strategies.items()) or "none"
        return f"{len(self.mappings)} mappings ({line})"


def draft_plan(source_profile: dict, target_profile: dict, source_url: str, target_url: str) -> dict:
    """Draft unambiguous same-name mappings; flag schema collisions for review."""
    source_candidates: dict[str, list[str]] = {}
    source_is_pg = source_profile.get("dialect") == "postgresql"
    for schema, tables in source_profile.get("schemas", {}).items():
        for name, t in tables.items():
            if t.get("rowcount", -1) >= 0:
                source_name = f"{schema}.{name}" if source_is_pg and schema != "_default" else name
                source_candidates.setdefault(name, []).append(source_name)
    target_candidates: dict[str, list[str]] = {}
    for schema, tables in target_profile.get("schemas", {}).items():
        for name in tables:
            key = name if schema == "_default" else f"{schema}.{name}"
            target_candidates.setdefault(name, []).append(key)

    common = sorted(
        name for name in source_candidates.keys() & target_candidates.keys()
        if len(source_candidates[name]) == 1 and len(target_candidates[name]) == 1
    )
    src_only = sorted(source_candidates.keys() - target_candidates.keys())
    tgt_only = sorted(target_candidates.keys() - source_candidates.keys())

    mappings = [
        {
            "source": source_candidates[name][0],
            "target": target_candidates[name][0],
            "strategy": "truncate",
        }
        for name in common
    ]
    plan = {
        "version": 1,
        "source": {"url": source_url},
        "target": {"url": target_url},
        "meta": {"schema": "ferry_meta"},
        "mappings": mappings,
    }
    comments = []
    if src_only:
        comments.append("# source tables with no same-name target (map/retire deliberately): "
                        + ", ".join(src_only))
    if tgt_only:
        comments.append("# target tables with no same-name source app-data (leave empty?): "
                        + ", ".join(tgt_only))
    ambiguous = sorted(
        name for name in source_candidates.keys() & target_candidates.keys()
        if len(source_candidates[name]) != 1 or len(target_candidates[name]) != 1
    )
    if ambiguous:
        comments.append("# ambiguous same-name tables omitted; choose schemas explicitly: "
                        + ", ".join(ambiguous))
    return plan, comments


def dump_draft(plan: dict, comments: list[str], path: Path) -> Path:
    header = ["# draft plan generated by `ferry plan` - REVIEW before running.",
              "# edit transforms/columns/strategy per mapping; delete mappings you reject."]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(header) + "\n")
        if comments:
            f.write("\n".join(comments) + "\n\n")
        yaml.safe_dump(plan, f, sort_keys=False)
    return path


def _plan_integer(value, label: str) -> int:
    if isinstance(value, bool):
        raise PlanError(f"{label} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        candidate = value.strip()
        digits = candidate[1:] if candidate[:1] in ("+", "-") else candidate
        if digits.isdigit():
            return int(candidate)
    raise PlanError(f"{label} must be an integer")
