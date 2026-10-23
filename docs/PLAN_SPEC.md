# migration_plan.yaml spec (v1)

```yaml
version: 1

source:
  url: "mysql+pymysql://user:pass@host:3306/db"     # any SQLAlchemy dialect string

target:
  url: "postgresql+psycopg://user@host/db"          # PostgreSQL first-class; SQLite supported for tests

meta:
  schema: ferry_meta          # set false to skip ferry_meta bookkeeping entirely
                              # (PG targets only; tables auto-created there)

order:                          # OPTIONAL: full wave order override (must list all targets)
- core.academic_years           # use for FK cycles the graph breaker cannot cut
- ...

on_error: continue              # optional; default abort (writes state then raises)

mappings:                       # one entry per table pair
- source: user_type             # source table (schema-qualified allowed for PG sources)
  target: iam.account_types     # target table; PG schema-qualified
  columns:                      # OPTIONAL source->target column renames
    user_type_id: account_type_id
  skip: [programme_type_id]     # OPTIONAL source columns dropped (or target cols absent)
  transforms:                   # OPTIONAL per target column (see below)
    id: "offset:800000"
  strategy: truncate            # truncate (delete-then-copy) | merge (upsert) - default truncate
  conflict: primary             # merge only: primary | unique:<name> | columns:<a,b> | off
  where: "is_deleted = 0"       # OPTIONAL source-side SQL filter
  limit: 100                    # OPTIONAL cap (rows, after where)
  chunk: 20000                  # OPTIONAL streaming chunk size
```

## Dialect support

The implemented dialect paths are PostgreSQL, MySQL, and SQLite. MySQL-to-PostgreSQL is the
first-class migration path; SQLite is used for local development and tests. Other SQLAlchemy
dialects are rejected by the executor until their identifier quoting, driver behavior, and
upsert semantics are implemented and tested.

## transforms
- builtin string: `pass strip upper lower null_if_empty safe_date safe_json int float bool_int epoch_to_ts`
- parametrized: `{"fn": "offset", "args": [800000]}` or shorthand `offset:800000`
- custom: `{"module": "helpers.py", "fn": "fix_phone", }` — helpers.py lives next to the plan file;
  the function takes the raw value and returns the migrated one; raise to fail that row.

## semantics worth knowing
- `truncate` strategy tables are deleted in REVERSE wave order before the topological copy pass.
- COPY TEXT is used for PG targets (fast path); other targets/dialects use parameterized batches.
- resume selects the most recently updated checkpoint unless a run ID is supplied through the API,
  then skips mappings with status `done` (state: `<plan>.yaml.ferry/<run_id>/`).
- `truncate` verification compares selected source count with target count; `merge` checks that target count
  is at least the selected source count because merge intentionally preserves target-only rows.
- FK orphan sweep inspects every FK of every mapped target table (children checked against refs).

## validating your plan
```bash
ferry run plan.yaml --dry-run     # waves + exact source counts, no data movement
ferry run plan.yaml               # real run; logs land in ferry_meta.* on the target
ferry verify plan.yaml            # strategy-aware count checks + orphan sweep; exit 2 on anomaly
```
