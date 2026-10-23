"""ferry.agent.mcp_server - stdio MCP server exposing the ferry loop to agents.

Tools map 1:1 onto CLI verbs: profile, plan, run, verify. Agents operate the loop:
profile -> (agent writes plan) -> run --dry-run -> run -> verify -> amendment patches.
"""

from __future__ import annotations

import json
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from sqlalchemy.engine import make_url

mcp = FastMCP("ferry")


def _workspace_path(value: str) -> Path:
    """Resolve an MCP file argument while keeping it inside the current workspace."""
    root = Path.cwd().resolve()
    path = Path(value)
    resolved = (root / path).resolve() if not path.is_absolute() else path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as e:
        raise ValueError(f"path must be inside the current working directory: {value!r}") from e
    return resolved


def _validate_database_url(url: str) -> None:
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite" or not parsed.database or parsed.database == ":memory:":
        return
    database = parsed.database
    if database.startswith("file:"):
        database = database[5:].split("?", 1)[0]
    if database in ("", ":memory:"):
        return
    _workspace_path(database)


@mcp.tool()
def profile(url: str, schemas: list[str] | None = None, sample: int = 0, out: str | None = None) -> str:
    """Profile a database (schema, rowcounts, FK graph, optional null-rate sampling) for migration mapping."""
    from ferry.engine.connect import make_engine
    from ferry.engine.introspect import profile_engine

    _validate_database_url(url)
    eng = make_engine(url)
    p = profile_engine(eng, schemas=schemas, sample=sample)
    if out:
        output_path = _workspace_path(out)
        with output_path.open("w", encoding="utf-8") as f:
            json.dump(p, f, indent=1, default=str)
        return f"profiling evidence written: {output_path}"
    return json.dumps(p, indent=1, default=str)


@mcp.tool()
def plan(source_url: str, target_url: str, out: str = "migration_plan.yaml") -> str:
    """Draft a reviewable migration_plan.yaml skeleton (same-name tables pass through, rest flagged)."""
    from ferry.engine.connect import make_engine
    from ferry.engine.introspect import profile_engine
    from ferry.engine.plan import draft_plan, dump_draft

    _validate_database_url(source_url)
    _validate_database_url(target_url)
    draft, comments = draft_plan(profile_engine(make_engine(source_url)),
                                 profile_engine(make_engine(target_url)), source_url, target_url)
    output_path = _workspace_path(out)
    dump_draft(draft, comments, output_path)
    return f"draft plan written: {output_path}; REVIEW before ferry_run"


@mcp.tool()
def run(plan_path: str, resume: bool = False, dry_run: bool = False) -> str:
    """Execute (or dry-run) a migration plan; deterministic waves with checkpoints."""
    from ferry.engine.executor import Executor
    from ferry.engine.plan import Plan

    plan = Plan.load(_workspace_path(plan_path))
    _validate_database_url(plan.source_url)
    _validate_database_url(plan.target_url)
    report = Executor(plan, resume=resume, dry_run=dry_run, echo=False).run()
    return json.dumps(report, indent=1, default=str)


@mcp.tool()
def verify(plan_path: str) -> str:
    """Verify a completed run: mapping count checks + FK orphan sweep."""
    from ferry.engine.plan import Plan
    from ferry.engine.verify import verify_plan

    plan = Plan.load(_workspace_path(plan_path))
    _validate_database_url(plan.source_url)
    _validate_database_url(plan.target_url)
    report = verify_plan(plan, report_dir=plan.state_dir())
    return json.dumps(report, indent=1, default=str)


def serve():
    mcp.run()
