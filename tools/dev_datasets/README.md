# Dev showcase datasets

A versioned generator for the datasets the UI is developed against. `plan.json` is the set, `request.py` builds the
requests and runs them. It is for a **dev or test stack only** and is not part of any image.

**Seeding always starts clean.** `seed` is a reset of the store's data, then the plan. It is used by developers and,
in test mode, by system tests; a run never depends on what an earlier run left behind.

The set covers what the nine long-lived `claude-seed` reference rows do not: one symbol across resolutions, DAILY
subscriptions, an expiry spread (past, near, far, never), operator-owned rows, two owners on one series, an old
ended range, a non-BULK expiry type, usage reads, and deliberately damaged datasets. It uses only SPY, QQQ and DIA
(large index ETFs, no rename or delisting risk) on dev, ZZDS-prefixed synthetic symbols on test, and never the owner
`claude-seed`, so it cannot touch a reference row. Ingest serves raw bars, so a rerun returns the same history.

## The set

Dates are relative to **D**, the run's UTC date (`--date YYYY-MM-DD` overrides it). Ranges are UTC `[start, end)`.
`end` of `-` is open-ended. Expiry `-` is Never. The vendor's history on the dev plan is not the same for every resolution: monthly bars begin 2020-07 and daily bars
2020-07-27, and the depth was not stable between runs (a first run returned a first monthly bar of 2018-11-01 for the
same series). Every healthy row therefore starts after 2020-07-27 (r6 on 2020-08-01).

| id | symbol | granularity | start .. end | update | expiry type | expiry | owner | reads | exercises |
|---|---|---|---|---|---|---|---|---|---|
| r1 | SPY | 1min | D-8 .. D-5 | STATIC | BULK | D+2 | dev-datasets | | 1min paging; Expiring Soon; the override on the card; a deletion date that passes at D+2 |
| r2 | SPY | 5min | D-40 .. D-10 | STATIC | BULK | - | dev-datasets | | Never expires; intraday paging |
| r3 | SPY | 30min | D-100 .. D-10 | STATIC | BULK | D+30 | dev-datasets | | a mid-range expiry |
| r4 | SPY | 1hour | D-370 .. D-10 | STATIC | BULK | - | dev-datasets | UI | a big intraday set |
| r5 | SPY | 1week | 2020-08-03 .. D-1 | STATIC | BULK | - | dev-datasets | | weekly bars (every week has its bar, so completeness reads COMPLETE) |
| r6 | SPY | 1month | 2020-08-01 .. D-1 | STATIC | BULK | D+365 | dev-datasets | | a far expiry |
| r7 | DIA | 1day | 2020-08-03 .. - | DAILY | ROLLING | - | operator | UI | the refresh loop; FRESH or LATE; writes need no override |
| r8 | QQQ | 30min | D-30 .. - | DAILY | ROLLING | - | operator | | a stopped subscription (`collect` false, no expiry): the Stopped views |
| r9 | QQQ | 1hour | D-24 .. D-10 | STATIC | BULK | D-1 | dev-datasets | | reaper pending: the deletion date has passed (dry-run keeps it) |
| r10 | DIA | 1day | 2021-01-04 .. 2023-01-03 | STATIC | BULK | - | dev-datasets | | a fixed, ended range; a second owner on r7's series |
| r11 | QQQ | 1week | 2020-08-03 .. D-1 | STATIC | BUFFER_1K | - | dev-datasets | SDK | a non-BULK label; an SDK read |
| r12 | DIA | 30min | D-60 .. D-5 | STATIC | BULK | - | **dev-degraded** | | **intentional degrade**: GAPS and the gap strip |
| r13 | QQQ | 5min | D-20 .. D-5 | STATIC | BULK | - | **dev-degraded** | | **intentional degrade**: GAPS, then the optional FAILED repair |
| r14 | DIA | 1week | 2016-01-04 .. 2021-01-04 | STATIC | BULK | - | **dev-degraded** | | **intentional degrade**: leading GAPS, the range starts before the vendor's history |

Two rules of the store shaped the table. A DAILY row must be open-ended and may not use BULK, so r7 and r8 are
`ROLLING`, and r11 uses `BUFFER_1K` so the set shows a second non-BULK label.

**Expiry is the deletion date only.** It never stops a subscription; whether a DAILY (or STREAM) dataset is
collecting is its own `collect` intent. A plan row may carry an optional boolean `collect`, sent in the POST body
only when set (omitted means the server default, true). It is for subscriptions only: the plan refuses it on a
STATIC row, as the store does. **r8 is the Stopped example**: `collect` false and no expiry, so the refresh skips it
and nothing ever deletes it. On the test backend r8's `ZZDS` mapping carries the same `collect` false.

**The reaper trial.** r9 is the past-expiry row (a STATIC row whose deletion date was D-1). r1's expiry, D+2, is
also only a deletion date: it is not past at seed time, becomes past-expiry at D+2, and is then reaped like r9 when
`REAPER_DELETES_ENABLED=true` (the reaper is dry-run otherwise). No `dev-degraded` row has an expiry, and every
past-expiry row belongs to `dev-datasets` or `operator`.

## These rows are intentionally damaged

The only intended negatives are the `dev-degraded` rows (the grid's Owner column shows the owner), and each one
carries its reason in `plan.json`:

| Row | Why it is damaged | How | Repair |
|---|---|---|---|
| `r12` | whole sessions from D-33 up to D-27 are deleted so the dataset reads GAPS | `degrade` | `repair` |
| `r13` | whole sessions from D-16 up to D-11 are deleted so the dataset reads GAPS (test: its first fetch fails, see below) | `degrade` | `repair` |
| `r14` | the range starts before the vendor's history, so the leading part is GAPS **on purpose** (test: a `GAPS_` symbol, every other bar missing) | the request itself | none: nothing is deleted and nothing can be fetched |

r12 and r13 lose whole trading sessions after they are fetched, so they read `GAPS` with a gap window on the
coverage strip. This is deliberate, not a bug. Repair them with `repair`, which re-sends each row's identical seed
body (an exact repeat refetches the whole declared range and the bars upsert idempotently): one more SUCCEEDED run,
status COMPLETE, gap count 0. There is no Retry Now button yet, so this is the retry. r14 is left alone by `degrade`
and `repair`.

Completeness counts sessions, and a session is covered when a bar covers its period: the Monday-Sunday week for
1week, the calendar month for 1month, in the trading calendar's time zone. A weekly or monthly row reads COMPLETE
when every expected period has its bar, so r5 and r11 read COMPLETE. A period is expected when its first day is in
`[start, end)` and it has ended; gap counts and windows count only the sessions inside `[start, end)`. r14 reads
GAPS on purpose: its range starts before the vendor's history, so its leading periods have no bar.

## Running it

From the repo root, in the agent container, with a dev or test stack up (started through `make agent-up`, which
provides the two credentials). Use the venv that `make` synced, `.venv-devcontainer/bin/python` in the devcontainer
(`.venv/bin/python` on a host); a bare default `uv sync` lacks the database driver.

    .venv-devcontainer/bin/python -m tools.dev_datasets.request <mode> [--yes-reset | --no-reset] [--dry-run] [--date YYYY-MM-DD] [--row r4 --row r7]

| Mode | What it does |
|---|---|
| `check` | Reports `secret`, `db password` (set, EMPTY or PLACEHOLDER, never the value), GET /ping and GET /ui/v1/config. |
| `seed` (the default) | **Resets first (see below), then** POSTs each row to `/store/stock/market-activity/{symbol}` as its owner. A row already present is skipped, so a rerun with `--no-reset` never rewrites an expiry. A 409 `OWN_OVERLAP_CONFLICT` counts as already there; any other 4xx stops; 429, 503 and 504 stop and say wait. |
| `reset` (`--reset`) | The reset alone, without the plan. |
| `reads` (`--reads`) | One UI read (`X-Client-Id: operator`, no cursor) for r4 and r7, one SDK read (`X-Client-Id: dev-datasets-sdk`) for r11, so Usage has something to show. |
| `degrade` (`--degrade`) | One SQL transaction deleting the bars of r12 and r13 between New York midnights (whole sessions). Touches no audit, usage or run table. |
| `repair` (`--repair`) | Re-sends r12 and r13's seed bodies. |

Order: `check`, `seed --yes-reset`, `reads`, `degrade`, then (optionally) the r13 step below, then `repair`.
`--dry-run` on `reads`, `degrade` and `repair` prints what would be sent or deleted and makes no network or database
call. On `seed` and `reset` it asks the rail (a read) which backend this is, reads the row counts a reset would
remove (a read), prints the request bodies, and changes nothing.

Seeding takes minutes on dev: roughly 15-20k bars and about 20 vendor calls. Ranges must end in the past.

Pass the **same `--date`** to `seed`, `degrade` and `repair`. `degrade` computes its window from its own D, so on a
later D than the seed it can miss the seeded range (r13's, for one) and delete nothing.

`seed`, `reset` and `degrade` talk to Postgres through `psycopg2`, which sits only in the data-store dependency
group, not the default one. Run them from a venv synced through `make` (`make init`, which `make test` also runs).

## The reset

`reset` empties **exactly these seven tables, in one transaction**, with one `TRUNCATE ... RESTART IDENTITY` (no
`CASCADE`: the one foreign key among them, `stock_market_activity.dataset_id`, has both ends in the statement):

`stock_market_activity`, `store_dataset_entry`, `ingest_run`, `dataset_read`, `dataset_read_daily`,
`dataset_override_log`, `dataset_deletion`.

It keeps the schema, `alembic_version` and every other table, and runs no DDL. A failure rolls the whole transaction
back. It prints the row count of each table before and after, never the password or the connection string.

- **Reset a quiet stack** (no Viewer open, no request running). The reset refuses, changing nothing and exiting 1,
  while any `ingest_run` row is `RUNNING` (it counts them in the same transaction, before the TRUNCATE, and names the
  count). After the commit it waits two usage flush intervals (10 s; the store flushes every 5 s) and recounts
  `ingest_run` and `dataset_read`; a non-zero count prints `NOT CLEAN` with the counts and exits 1. The guarantee is
  exactly that: clean at commit, verified after one flush interval.
- **`--yes-reset` is required.** Without it `seed` and `reset` print the counts they would remove, change nothing
  and exit 1.
- **`--no-reset`** (seed only) skips the reset, for incremental use: add a row, rerun a `--row`, seed on top of what is
  there. `--row` needs it, because the reset is not row-bound. `--yes-reset` with `--no-reset` is a usage error.
- **`--dry-run`** shows the counts and the request bodies and changes nothing.

**The two audit tables are append-only by convention only.** `dataset_override_log` and `dataset_deletion` are written
only by code paths that append; nothing in the database protects them (no trigger, rule or `REVOKE`), so the database
permits the reset. The reset is a **deliberate environment reset** and is **not recorded** anywhere, `dataset_deletion`
included: a clean start means empty. Use it on a dev or test database only; it removes every dataset, run, usage and
audit row.

## Two backends, one plan

`GET /ui/v1/config` reports the stack's `deploymentLabel`. The tool refuses unless it is `dev` or `test`, and the label
picks the backend:

| label | set by | bars | symbols |
|---|---|---|---|
| `dev` | `docker-compose.override.yaml` | real Alpaca | SPY, QQQ, DIA |
| `test` | `docker-compose.fake.yaml` (the test-mode overlay) | the fake broker (`tests/fakes/market_data.py`) | `ZZDS` + the dev symbol (`ZZDSSPY`, `ZZDSQQQ`, `ZZDSDIA`) |

The synthetic symbols mean a public-repo seed never shows fake prices under a real ticker. A row may carry an optional
`test` object `{symbol, why}` to pick a fake-broker scenario by prefix; the symbol must be an optional prefix, `ZZDS`,
then the dev symbol:

| Row | Test symbol | Why |
|---|---|---|
| `r13` | `FAILONCE_ZZDSQQQ` | the first fetch fails (a FAILED run in history), the repeat succeeds; replaces stopping `data_ingest` |
| `r14` | `GAPS_ZZDSDIA` | the fake serves every other bar, so the dataset reads GAPS |

Every other row is `ZZDS` + its dev symbol and takes the fake's default bars. `--degrade` works in both backends (SQL on
the row's own bars).

On test, `seed` reports r13's first fetch as `failed once as designed (FAILONCE_)` (HTTP 503, no entry, a FAILED run)
and goes on; `degrade` skips r13 (it holds no bars yet); `repair --row r13` then repeats the request and it succeeds.
Test mode runs one `data_ingest` worker, so the fake's memory of the first failure is not split. That memory lives in
the `data_ingest` process (`tests/fakes/market_data.py`), so a second `seed` on the same test stack with the same date
gets **no** failed run for r13 (the first fetch succeeds); restart `data_ingest` or use another `--date` to see it
again. Only a 503 with reason `VENDOR_UNAVAILABLE` counts as the designed failure; any other 503 stops the seed.

### The r13 step on dev (on the host)

For a genuine FAILED run in run history on dev: after `degrade`, stop `data_ingest` on the host, run
`repair --row r13` (it fails `NOT_READY` through the production path), start `data_ingest` again, and run
`repair --row r13` once more to fix it.

## Safety

- **Every writing mode (seed, reset, reads, degrade, repair) refuses unless `GET /ui/v1/config` reports `deploymentLabel`
  `dev` or `test`.** A prod stack answers an empty label and is refused before any SQL or request is sent.
- **Credentials come only from the environment**: `INSTANCE_WRITE_SECRET` (sent only in the `X-Instance-Secret`
  header) and `POSTGRES_PASS` (the database password). Neither is printed, logged, or put in an exception, and a
  failure is reported by the exception's type name only. If either holds the placeholder
  `UNSET-start-with-make-agent-up-from-the-repo-root`, every mode but `check` stops, except that every `--dry-run`
  skips the credential check (it sends no secret and opens no database with a placeholder).
- Other settings: `DEV_DATASETS_STORE_URL` (default `http://data_store`), `DATABASE_NAME` (default `db`),
  `POSTGRES_USER` (default `postgres`), `POSTGRES_DB_NAME` (default `postgres`).
- `degrade` deletes only bars whose `dataset_id` is a `dev-degraded` entry named in the plan, and refuses a row
  whose lookup does not find exactly one entry.
