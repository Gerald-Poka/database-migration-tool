# ferry

**ferry** is a plan-driven database data-migration tool. It profiles databases, helps draft a
reviewable YAML mapping plan, executes that plan, and reports verification evidence. The plan
contains migration decisions; the engine does not infer business rules while copying data.

This project is licensed under the [BSD 3-Clause License](./LICENSE).

## Contributors

- **Gerald Ndyamukama** — project author and maintainer.
- **OpenAI Codex** — AI-assisted code and documentation contributions, made in collaboration
  with the project maintainer.
- **GitHub Copilot** — AI-assisted code contributions, made in collaboration with the project
  maintainer.

AI-assisted contributions are reviewed and maintained by the project maintainer. See
[CONTRIBUTORS.md](./CONTRIBUTORS.md) for the contributor list.

It was shaped by a large MySQL-to-PostgreSQL migration. That experience motivated features such as
foreign-key ordering, chunked loads, explicit ID transforms, resumable table checkpoints, and
row-count/orphan checks. Those features reduce risk, but cannot guarantee that every database,
schema, plan, or migration will succeed without testing against the actual systems.

## Database support

| Database/dialect | Status | Notes |
|---|---|---|
| PostgreSQL (`postgresql+psycopg`) | Source and target; live-tested | PostgreSQL target bookkeeping is enabled by default. |
| MySQL (`mysql+pymysql`) | Source and target; live-tested | Native MySQL upsert path; MySQL-to-PostgreSQL is the first-class migration path. |
| MariaDB (`mariadb+pymysql`) | Source and target; live-tested | Uses the MySQL-compatible SQLAlchemy driver path. |
| SQLite (`sqlite`) | Source and target; tested | Useful for local development, tests, and small migrations. |
| CockroachDB (`cockroachdb+psycopg`) | Source and target; live-tested | Install the `cockroach` extra. Use its dedicated SQLAlchemy dialect rather than `postgresql+psycopg`. |
| DuckDB (`duckdb`) | Source and target; live-tested | Install the `duckdb` extra. DuckDB catalog reflection uses Ferry's adapter. |
| ClickHouse (`clickhousedb+connect`) | Source only; live-tested | Install the `clickhouse` extra. ClickHouse is rejected as a target. |
| SQL Server (`mssql+pyodbc`) | Adapter available; not live-tested | Requires `pyodbc` and a compatible system ODBC driver. |
| Oracle (`oracle+oracledb`) | Adapter available; not live-tested | Uses python-oracledb thin mode by default. |

“Live-tested” means the checked-in suite has exercised basic migration against the indicated
engine(s); it does not certify every release, type, schema feature, scale, or production topology.
SQL Server and Oracle need live integration coverage before relying on them for important data.
Schema/DDL migration is out of scope: the target schema must already exist.

## What ferry does

The workflow separates evidence collection, migration decisions, deterministic execution, and
verification:

```text
profile -> review/mapping -> draft or edit plan -> dry run -> execute -> verify
```

1. **Profile** — inspect schemas, columns, row counts, primary/foreign/unique keys, and optionally
   small row samples and null rates.
2. **Map** — decide which tables and columns to migrate, how IDs relate, which transforms and
   filters to use, and whether each target is refreshed or merged. These are human/agent decisions.
3. **Plan** — generate a YAML skeleton from same-name tables, then review and edit it. Ambiguous
   same-name tables are omitted from the draft and called out for explicit schema selection.
4. **Dry run** — inspect target dependency waves and selected source row counts without moving data.
5. **Execute** — apply the plan in FK dependency order, checkpoint table results, and log run
   information on PostgreSQL targets.
6. **Verify** — compare source/target counts according to strategy and check foreign-key orphans
   for mapped target tables.

## Install

Python 3.10 or newer is required.

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/ferry --help
```

Install a database driver's extra when needed, for example `.venv/bin/pip install -e ".[duckdb]"`.
The `all-sql` extra installs the Python adapters for the listed SQL databases. SQL Server also
requires an OS-level ODBC driver.

For the optional MCP server:

```bash
.venv/bin/pip install -e ".[mcp]"
.venv/bin/ferry mcp
```

The MCP server exposes `profile`, `plan`, `run`, and `verify` over stdio. File paths passed to MCP
tools are restricted to the current working directory. Local SQLite database files used through
MCP are restricted there too.

## Database URLs

Use SQLAlchemy URLs. Examples:

```text
mysql+pymysql://USER:PASSWORD@HOST:3306/DATABASE
postgresql+psycopg://USER:PASSWORD@HOST:5432/DATABASE
sqlite:////absolute/path/to/database.db
sqlite:///relative/path/to/database.db
cockroachdb+psycopg://USER@HOST:26257/DATABASE?sslmode=disable
duckdb:////absolute/path/to/database.duckdb
clickhousedb+connect://USER:PASSWORD@HOST:8123/DATABASE
```

URL passwords may need URL encoding for reserved characters. Do not commit real credentials or
credential-bearing plans. Use local-only plan files or an appropriate secrets mechanism, and
protect profile output because samples can contain sensitive data.

## Commands

### Profile a database

```bash
.venv/bin/ferry profile "$SOURCE_URL" --sample 400 --out source-profile.json
```

`--sample` requests a bounded sample for per-column null-rate and value probes. Review the output
and handle it as potentially sensitive.

### Draft a plan

```bash
.venv/bin/ferry plan "$SOURCE_URL" "$TARGET_URL" migration_plan.yaml
```

This creates a **draft**, not an approved migration. It intersects unambiguous same-name tables;
it does not infer renames, ID policies, retire lists, transforms, or business meaning. Review every
mapping and resolve draft comments before running it.

### Dry-run, execute, and resume

```bash
.venv/bin/ferry run migration_plan.yaml --dry-run
.venv/bin/ferry run migration_plan.yaml
.venv/bin/ferry run migration_plan.yaml --resume
```

`--dry-run` reports dependency waves and source counts; it does not move rows. `--resume` selects
the most recently updated checkpoint under the plan's state directory and skips mappings marked
done. A failed/incomplete table is retried; completed tables are not recopied. Review the selected
checkpoint before resuming if several runs exist.

### Verify

```bash
.venv/bin/ferry verify migration_plan.yaml
```

The command exits non-zero when reported checks find a mismatch or error. Verification is useful
evidence, not a complete proof of equivalence: it does not compare every column value, detect
business-rule errors, or prove that an ID transform preserved all intended relationships.

## Migration plan basics

A plan is one YAML file. State artifacts are kept beside it under
`migration_plan.yaml.ferry/<run_id>/` (reports and checkpoints); execution does not rewrite the
plan.

```yaml
version: 1
source:
  url: "mysql+pymysql://USER:PASSWORD@HOST:3306/legacy"
target:
  url: "postgresql+psycopg://USER:PASSWORD@HOST:5432/app"
meta:
  schema: ferry_meta  # PostgreSQL target bookkeeping; set false to disable
mappings:
  - source: legacy_user
    target: app.users
    columns:
      legacy_user_id: id
      full_name: name
    skip: [unused_legacy_field]
    transforms:
      id: "offset:800000"
      email: lower
    strategy: truncate
    where: "is_deleted = 0"
    chunk: 20000
```

See [docs/PLAN_SPEC.md](./docs/PLAN_SPEC.md) for all supported plan keys and transform details.

### Mapping options

- `source`, `target`: table names; schema qualification depends on the selected dialect.
- `columns`: source-column to target-column renames.
- `skip`: source columns not loaded.
- `transforms`: target-column transform specifications.
- `where`: source-side SQL condition. It is inserted into the SELECT as written; only use reviewed,
  trusted plan files.
- `limit`: maximum selected source rows after `where`. No ordering is implied, so a limited
  selection is not guaranteed to be repeatable unless the source provides a stable selection.
- `chunk`: rows fetched per batch.
- `strategy: truncate`: delete all rows in each mapped target before loading the selected source
  rows. This is destructive; take a backup and review the plan first.
- `strategy: merge`: insert/update using the chosen conflict target. Existing target-only rows are
  preserved.
- `conflict`: merge conflict target: `primary`, `unique:<constraint-name>`,
  `columns:<a,b>`, or `off`.
- `on_error: abort|continue`: default is `abort`; `continue` attempts later mappings after a table
  error and still reports a failed run.
- `order`: explicit full target order override. It must contain exactly every mapped target.

Built-in transforms include `strip`, `upper`, `lower`, `null_if_empty`, `safe_date`, `safe_json`,
`int`, `float`, `bool_int`, `epoch_to_ts`, and `offset:<n>`. Custom Python transform modules must
live beside the plan file; treat them as executable trusted code.

## Execution, checkpoints, and safety

- Target schemas and tables must already exist. ferry migrates data; it does not create or alter
  application schemas.
- Without `order`, mappings are ordered from the target's foreign keys. Self-references are handled
  as one table; multi-table FK cycles require a reviewed explicit order or a schema change.
- `truncate` targets are deleted in reverse dependency order before loading begins. A run can
  therefore leave targets empty or partially loaded if it fails after deletion.
- Rows are written in chunks and committed chunk by chunk. Checkpoints are recorded after a mapping
  completes, not after each row. Resume retries an incomplete mapping; it is not a distributed
  transaction or a rollback mechanism.
- For `merge`, choose a correct unique conflict key. Count verification only checks that target
  count is at least the selected source count, because target-only rows are allowed; this does not
  prove every source row matched or every value is correct.
- Ferry does not currently promise to advance PostgreSQL identity/serial sequences after loading.
  Check and, if needed, repair sequences before applications resume writes.
- PostgreSQL targets get `ferry_meta` run and mapping logs by default; other targets do not. Set
  `meta.schema: false` to disable bookkeeping.
- SQL Server and Oracle merge use a portable update-then-insert fallback. It is not atomic against
  concurrent writers; serialize writes or use truncate when that behavior is not acceptable.
- Back up data, review URLs/filters/transforms/ID contracts, run a dry run, and test on a staging
  copy before production. A dry run validates useful configuration but does not prove the load will
  succeed.

## Agent operation

Install the MCP extra and run `ferry mcp` from the intended working directory. The engine remains
plan-driven: an agent may help interpret profiles and draft a plan, but a person should review
mapping choices, filters, transforms, ID offsets, and destructive strategy before execution. See
[docs/AGENT_LOOP.md](./docs/AGENT_LOOP.md).

## Development checks

```bash
.venv/bin/pytest -q
.venv/bin/ruff check ferry tests
```

Optional live tests use the URLs in these environment variables:

```bash
docker compose -f tests/integration/compose.yaml up -d --wait
FERRY_TEST_POSTGRES_URL='postgresql+psycopg://postgres:ferrytest@127.0.0.1:55432/ferrytest' \
FERRY_TEST_MYSQL_URL='mysql+pymysql://root:ferrytest@127.0.0.1:53306/ferrytest' \
FERRY_TEST_MARIADB_URL='mariadb+pymysql://root:ferrytest@127.0.0.1:53307/ferrytest' \
FERRY_TEST_CLICKHOUSE_URL='clickhousedb+connect://default:ferrytest@127.0.0.1:58123/default' \
  .venv/bin/pytest tests/test_sql_integration.py -q
docker compose -f tests/integration/compose.yaml down
```

The live test module also accepts `FERRY_TEST_COCKROACH_URL`, for example
`cockroachdb+psycopg://root@127.0.0.1:26257/defaultdb?sslmode=disable`. DuckDB is tested locally
by `tests/test_duckdb.py`; PostgreSQL, MySQL, MariaDB, CockroachDB, and ClickHouse tests are opt-in
and skip when their URLs are absent. Always rehearse with representative production versions and
data.

## Current limitations

- No schema/DDL migration or schema-diff generation.
- No value-by-value checksum or semantic equivalence verification.
- Multi-table FK cycles need manual handling.
- No automatic PostgreSQL sequence advancement after explicit ID loads.
- Non-PostgreSQL targets do not receive `ferry_meta` bookkeeping.
- SQL Server and Oracle adapters have not yet been exercised against live services in this
  project's integration suite.
