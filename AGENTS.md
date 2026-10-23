AGENTS.md

# ferry

Agent-assisted, plan-driven database migration tool. Python 3.10+; SQLAlchemy 2 Core only (no ORM).

## Commands
- Setup: `python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"`
- Tests (always run before claiming done): `.venv/bin/pytest -q`
- Lint: `.venv/bin/ruff check ferry tests`
- Smoke: `.venv/bin/pytest tests/test_roundtrip.py -q`

## Conventions
- Engine is deterministic: it executes plans, never invents them. All judgment (mapping, ids, retire lists, dummy-data
  policies) lives in `migration_plan.yaml` produced by the agent + human review.
- State artifacts go under `<plan-stem>.ferry/` next to the plan (profile.json, checkpoints, reports). Never mutate
  a plan in place during a run.
- Target-side bookkeeping lives in a `ferry_meta` schema (PG targets only; graceful skip elsewhere): run_log, migration_log, repair_log.
- Execution order: reverse-topological DELETE pass, then topological INSERT pass, one transaction per table, checkpoint after each.
- self-referencing FKs: single table, last in wave; multi-table FK cycles: plan must break them explicitly (engine errors with the cycle list).
- Agent entry point: `ferry mcp` (stdio MCP server) exposing profile/plan/run/verify. Same verbs as the CLI; MCP tools must
  not read/write anything outside the working dir.
