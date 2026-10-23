"""ferry.cli - human + agent entry points.

Commands:
  profile <url> [--schemas ...] [--sample N] [--out file]
  plan     (source url, target url) -> draft migration_plan.yaml at --out
  run <plan.yaml> [--resume] [--dry-run] [--order ...]
  verify <plan.yaml>
  mcp        (agent stdio server; pip install ferry[mcp])
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="ferry - agent-assisted, plan-driven database migration")


def _print_report(report: dict) -> None:
    try:
        from rich.console import Console
        from rich.table import Table
        console = Console()
        if "waves" in report and isinstance(report["waves"], list):
            t = Table(title="dry-run waves")
            for col in ("wave", "target", "strategy", "source", "source_count"):
                t.add_column(col)
            for w in report["waves"]:
                t.add_row(*[str(w.get(c)) for c in ("wave", "target", "strategy", "source", "source_count")])
            console.print(t)
        elif "mappings" in report:
            t = Table(title="run report")
            for col in ("table", "strategy", "rows", "seconds"):
                t.add_column(col)
            for k, v in report["mappings"].items():
                t.add_row(k, str(v.get("strategy")), str(v.get("rows_written")), str(v.get("seconds")))
            console.print(t)
        if "counts" in report:
            t = Table(title="verify")
            for col in ("mapping", "source", "source_count", "target_count", "match"):
                t.add_column(col)
            for c in report["counts"]:
                t.add_row(str(c.get("mapping")), str(c.get("source")), str(c.get("source_count")),
                          str(c.get("target_count")), str(c.get("match", "")))
            for o in report.get("fk_orphans", []):
                if isinstance(o.get("orphans"), int) and o["orphans"] > 0:
                    t.add_row(f"[red]ORPHANS[/red] {o['fk']}", o["child"], "", o["ref"], o["orphans"])
            console.print(t)
        for err in report.get("summary", {}).get("errors", []):
            console.print(f"[red]error[/red] {err}")
        console.print(f"status: {report.get('status') or report.get('summary', {}).get('status', '?')}")
    except ImportError:  # rich missing
        print(json.dumps(report, indent=1, default=str))


@app.command()
def profile(url: str,
            schemas: list[str] | None = typer.Option(None, help="PG schemas; default all user schemas"),
            sample: int = typer.Option(0, help="rows sampled per table for null-rate/value probes"),
            out: Path | None = typer.Option(None, help="write profiling evidence JSON here")):
    """Profile a source (or target) database and emit evidence for the Map phase."""
    from .engine.connect import make_engine
    from .engine.introspect import profile_engine
    eng = make_engine(url)
    p = profile_engine(eng, schemas=list(schemas) if schemas else None, sample=sample)
    if out:
        out.write_text(json.dumps(p, indent=1, default=str))
        typer.echo(f"profile written to {out}: {sum(len(t) for t in p['schemas'].values())} tables")
    else:
        typer.echo(json.dumps(p, indent=1, default=str))


@app.command()
def plan(source_url: str, target_url: str, out: Path = typer.Argument(..., help="plan yaml output path")):
    """Draft a migration_plan.yaml skeleton from live schema inspection (REVIEW before run)."""
    from .engine.connect import make_engine
    from .engine.introspect import profile_engine
    from .engine.plan import draft_plan, dump_draft

    sp = profile_engine(make_engine(source_url))
    tp = profile_engine(make_engine(target_url))
    draft, comments = draft_plan(sp, tp, source_url, target_url)
    dump_draft(draft, comments, out)
    typer.echo(f"draft plan written to {out} - REVIEW transforms/id contracts before `ferry run`")


@app.command()
def run(plan_path: Path,
        resume: bool = typer.Option(False, help="resume the most recently updated checkpoint"),
        dry_run: bool = typer.Option(False, help="list waves and source counts, move nothing"),
        echo: bool = typer.Option(True, "--echo/--quiet")):
    """Execute a reviewed plan deterministically (waves, checkpoints, ferry_meta logs)."""
    from .engine.executor import Executor
    from .engine.plan import Plan

    pl = Plan.load(plan_path)
    ex = Executor(pl, resume=resume, dry_run=dry_run, echo=echo)
    try:
        report = ex.run()
    except Exception as e:  # repr already inside report on abort
        typer.secho(f"run failed: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from e
    _print_report(report)
    if report.get("summary", {}).get("errors"):
        raise typer.Exit(2)


@app.command()
def verify(plan_path: Path):
    """Mapping count checks + FK orphan sweep; exits non-zero on anomalies."""
    from .engine.plan import Plan
    from .engine.verify import verify_plan

    pl = Plan.load(plan_path)
    report = verify_plan(pl, report_dir=pl.state_dir())
    _print_report(report)
    if report["status"] != "ok":
        raise typer.Exit(2)


@app.command()
def mcp():
    """Serve ferry verbs over stdio MCP for agent operation (pip install ferry[mcp])."""
    try:
        from .agent.mcp_server import serve
    except ImportError as e:
        typer.secho("MCP extra not installed: pip install 'ferry[mcp]'", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from e
    serve()
