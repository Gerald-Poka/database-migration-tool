# The agent loop

ferry separates **judgment** (agent + human) from **execution** (deterministic engine).
The agent never writes ad-hoc SQL against the target. It authors plan files; the engine replays them.

## Phases

### 1. Profile (engine collects, agent reads)
`ferry profile <url> [--schemas a,b] [--sample 400] --out profile.json`

Evidence per table: columns+types+nullability, rowcount (exact count), PK, FKs, uniques; with
`--sample N` also per-column null rate and 3 sample rows (truncated).
Agent uses it for:
- dependency hints when the target graph is unavailable
- dirty-data probes: epoch ints, partial dates (`2000-01-00`), double-encoded JSON, zero-filled ids
- dummy value detection (`example.com`, repeated current-cycle dates, placeholder names)
- legacy alias traps (same-name-but-not tables like `user` vs stale `users` copies)

### 2. Map (agent authors, human approves)
For each table decide:
- map/retire/empty policy (is this table even wanted in the target?)
- column mapping, `skip` list, per-column `transforms`
- identity contracts: if legacy stores overlap, pick offsets (`offset:800000`) and mirror them on FK chain
- `strategy`: `truncate` (full refresh, delete+copy; default) vs `merge` (upsert on PK/unique)
- `where` filters (e.g. `is_deleted = 0`) and `limit` for sample migrations

### 3. Plan (engine drafts, agent edits)
`ferry plan` drafts mappings for unambiguous same-name source/target tables. PostgreSQL source
tables are schema-qualified, and same-name collisions are omitted with comments for explicit review.
Everything else is deliberate work. The plan file is the single source of truth and lives next to
state artifacts.

### 4. Execute (engine only)
- waves from the target FK graph (`plan.order:` overrides for cycles)
- one transaction per table per chunk; `ferry_meta` run/migration logs on PG targets
- `--dry-run` shows waves+counts moving nothing; agent uses it to sanity-check before `run`
- `--resume` continues the most recently updated checkpoint and skips tables recorded `done`

### 5. Verify (engine reports, agent reacts)
`ferry verify` = strategy-aware count checks + FK orphan sweep. Non-zero exit on anomalies.

## Amendment patches
After a completed run, agents fix residual issues with **idempotent patch scripts** that:
- connect via the same URLs as the plan (no credentials in script)
- are re-runnable (UPDATE/INSERT ON CONFLICT semantics)
- log their own decision trail (count changed by rule, per type) — e.g. the "account type
  restoration" pattern: `UPDATE users SET account_type_id = ... WHERE id IN (SELECT ...)` with a
  summary logged into `ferry_meta.repair_log`

Real example from the birth migration: account-type restore (`fix_account_types.py` in the AEIS
repo) — 119,321 users re-typed from a canonical source column wave2 had missed, fully logged.

## Agent etiquette
- Always `--dry-run` first; show the wave table to the human.
- Never edit a plan or run mid-run.
- Treat verify failures as facts: reproduce, quantify, then patch.
- Keep secrets out of committed files: URLs with credentials belong in local files only.
