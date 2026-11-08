"""The producer run: seed a scratch stack, refuse anything not synthetic, normalise, render, return one bundle.

Decision tj-vhboky.55 (SEED PRODUCTION), ruling tj-vhboky.56, addendum S9. Runs inside test_client
over the network (stack.py); writes nothing to disk. In order:
  1. Read the schema revision from alembic_version and check it against the head of the chain in
     data/store/migrations; a mismatch is refused (a seed is only valid at the revision it names).
  2. Refuse a database that already holds a non-synthetic row, then POST every SEED_REQUESTS entry
     to data_store's /store route (the fake-mode stack's ingest answers with fake bars). Each reply's
     data_points is checked against the symbol: a symbol without the EMPTY_ prefix must store bars,
     an EMPTY_ symbol must store none, and a missing or non-integer count is refused (a 200 that
     stored nothing is what a stack not in fake mode gives).
  3. THE SYNTHETIC-ONLY REFUSAL. Query both tables; if any row's asset_symbol or owner falls
     outside the scenario's patterns, report the COUNTS only, never a value, and refuse. This runs
     before anything is rewritten, so a seed cannot come from a database that ever held a real
     symbol or owner. Then the COLLISION REFUSAL: at least one
     (asset_symbol, source, granularity, timestamp) group must hold more than one bar row, the old
     four-column bar key collision the head seed exists to carry (decision tj-vhboky.55, E4).
  3b. Rebuild the three ingest tables (bead tj-7zjov1.100): they are emptied before the POSTs (the POSTs write
     ten attempt-less ingest_run rows) and the cast of ingest_records.py is written after them through the
     store's own code (create_request, mark_queued, DeliveryIntake), then counted: a seed whose request states,
     attempt rows or instance marker differ from the cast is refused.
  4. Normalise the five tables (dump.py: why) and render them (dump.py: the format).
  5. Build the manifest and return the bundle (bundle.py: the contract), last, and only when every
     earlier step passed. __main__ prints it as the one stdout line.

The database is the caller's scratch database, named by test_client's environment. Step 4 rewrites
the two market tables in it. Never point this at a stack that holds data you want.

Where the files go is not the producer's business: `python -m data.store.seeds.bundle` (make
seed-dump) and the MCP's own reader write them from the bundle.
"""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.engine import URL

from common.tests.roots import SERVER_ROOT
from data.store.seeds.bundle import Bundle, BundleRefused, render_bundle
from data.store.seeds.dump import (
    BAR_TABLE,
    ENTRY_TABLE,
    INSTANCE_TABLE,
    NORMALISE_SQL,
    REQUEST_TABLE,
    RUN_TABLE,
    SEED_TABLES,
    check_no_meta_commands,
    render_table_sql,
    with_sequence_position,
)
from data.store.seeds.ingest_records import (
    EXPECTED_ATTEMPT_ROWS,
    EXPECTED_INSTANCES,
    EXPECTED_REQUEST_STATES,
    seed_ingest_records,
)
from data.store.seeds.manifest import build_manifest, column_digest_sql, head_revision, render_manifest
from data.store.seeds.scenario import SEED_REQUESTS, SeedRequest, is_synthetic
from data.store.seeds.stack import Stack, StackError
from routers.common.instance_secret import INSTANCE_SECRET_HEADER
from tests.fakes.market_data import EMPTY_PREFIX


# SERVER_ROOT, not REPO_ROOT (tj-2bsw0k; epic tj-iontkq risk R-1). The migration revisions live
# under <server>/data/store/migrations, where <server> is the directory holding common/, routers/
# and schemas/ -- SERVER_ROOT's definition, not the true repository root bundle.py's REPO_ROOT
# names. The two coincide today; after the service trees move under server/ (epic tj-iontkq) they
# stop coinciding, and the old `REPO_ROOT / 'data' / ...` here would have resolved VERSIONS_DIR to
# the repository root instead of server/, where no revisions exist.
#
# Imported from common.tests.roots rather than reproduced locally, unlike bundle.py: this package
# never ships (see the package docstring) and already imports tests.fakes, so the stdlib-only
# constraint that forced bundle.py's own marker search does not apply here, and importing the one
# sentinel shared by every other site keeps this module from drifting out of step with it.
VERSIONS_DIR: Path = SERVER_ROOT / 'data' / 'store' / 'migrations' / 'versions'


SeedRecords = Callable[[URL, uuid.UUID, str], Awaitable[None]]


class SeedRefused(Exception):
    """The producer will not produce a seed. The message never carries a row value."""


@dataclass(frozen=True)
class SeedResult:
    """What a run produced.

    Args:
        bundle (Bundle): The seed, ready to print.
        row_counts (dict[str, int]): Rows per table.
    """

    bundle: Bundle
    row_counts: dict[str, int]


def _current_revision(stack: Stack) -> str:
    rows = stack.query('SELECT version_num FROM alembic_version;', 'alembic_version read')
    if len(rows) != 1:
        raise SeedRefused(f'alembic_version holds {len(rows)} rows; expected exactly one')
    if len(rows[0]) != 1 or not isinstance(rows[0][0], str):
        raise StackError('alembic_version read: the row is not one text value')
    return rows[0][0]


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _query_count(stack: Stack, sql: str, what: str) -> int:
    """Run a count query and require one row holding one non-negative integer.

    Raises:
        StackError: If the result is anything else. The message names the step and never echoes the result.
    """
    rows = stack.query(sql, what)
    if len(rows) != 1 or len(rows[0]) != 1 or not _is_count(rows[0][0]):
        raise StackError(f'{what}: the query did not return a single non-negative integer')
    return rows[0][0]


def _count_foreign_rows(stack: Stack) -> tuple[int, bool]:
    """Rows whose symbol or owner is outside the scenario's patterns, over both tables; a count only.

    Returns:
        tuple[int, bool]: The count, and whether an unreadable group was counted as ONE row (so the
            count is a lower bound).
    """
    # The ingest tables carry the same labels: a request's client_label and a run's actor are the owner. A NULL
    # symbol (an asset-list request) is no synthetic symbol, so it reads as foreign. ingest_instance has no label.
    sql = (
        f"SELECT 'e', asset_symbol, owner, count(*) FROM {ENTRY_TABLE} GROUP BY asset_symbol, owner "  # nosec B608 -- module constants only
        f"UNION ALL SELECT 'b', asset_symbol, '', count(*) FROM {BAR_TABLE} GROUP BY asset_symbol "
        f"UNION ALL SELECT 'q', coalesce(asset_symbol, ''), client_label, count(*) FROM {REQUEST_TABLE} "
        'GROUP BY asset_symbol, client_label '
        f"UNION ALL SELECT 'r', asset_symbol, actor, count(*) FROM {RUN_TABLE} GROUP BY asset_symbol, actor;"
    )
    foreign = 0
    unreadable = False
    for row in stack.query(sql, 'synthetic check'):
        # A row that is not (kind, symbol, owner, count) with a text symbol and owner and a non-negative int
        # count holds a value no synthetic row has; count it as one foreign row rather than guess at its size.
        if len(row) != 4 or not _is_count(row[3]) or not isinstance(row[1], str) or not isinstance(row[2], str):
            foreign += 1
            unreadable = True
            continue
        kind, symbol, owner, count = row
        if not is_synthetic(symbol, owner if kind != 'b' else None):
            foreign += count
    return foreign, unreadable


def _refuse_foreign_rows(stack: Stack) -> None:
    foreign, unreadable = _count_foreign_rows(stack)
    if foreign:
        bound = 'at least ' if unreadable else ''
        raise SeedRefused(
            f'{bound}{foreign} row(s) hold a symbol or owner outside the synthetic patterns; no seed written'
        )


def _check_stored(index: int, request: SeedRequest, reply: dict[str, object]) -> None:
    """Refuse a reply whose data_points does not match what the request's symbol should store.

    Args:
        index (int): The request's position in SEED_REQUESTS.
        request (SeedRequest): The request that was POSTed.
        reply (dict[str, object]): The stack's {'status', 'body'} reply.

    Raises:
        SeedRefused: If data_points is missing, not an integer or negative, if a symbol without the
            EMPTY_ prefix stored none, or if an EMPTY_ symbol stored any.
    """
    try:
        stored = json.loads(str(reply.get('body')))['data_points']
    except (ValueError, KeyError, TypeError):
        stored = None
    if isinstance(stored, bool) or not isinstance(stored, int) or stored < 0:
        raise SeedRefused(f'request {index} ({request.path}): the reply carries no valid data_points count')
    expects_empty = request.symbol.startswith(EMPTY_PREFIX)
    if expects_empty and stored > 0:
        raise SeedRefused(
            f'request {index} ({request.path}): an {EMPTY_PREFIX} symbol stored {stored} bar(s); '
            'the stack is not the fake-mode stack the scenario assumes'
        )
    if not expects_empty and stored == 0:
        raise SeedRefused(
            f'request {index} ({request.path}): stored 0 bars; the stack is not in fake mode or the fake failed'
        )


def _refuse_no_bar_collision(stack: Stack) -> None:
    """Refuse unless the bar table holds a group sharing the old four-column bar key."""
    sql = (
        'SELECT count(*) FROM (SELECT 1 FROM '  # nosec B608 -- table is a module constant
        f'{BAR_TABLE} GROUP BY asset_symbol, source, granularity, timestamp HAVING count(*) > 1) AS collisions;'
    )
    collisions = _query_count(stack, sql, 'bar collision check')
    if collisions == 0:
        raise SeedRefused(
            'no bar shares an (asset_symbol, source, granularity, timestamp) key with another; '
            'the seed would not carry the old four-column bar key collision; no seed written'
        )


def _dataset_to_write_under(stack: Stack) -> tuple[uuid.UUID, str]:
    """The entry the seeded requests write bars under: the first scenario request's dataset, and its symbol.

    Raises:
        SeedRefused: If the scenario's first dataset is not exactly one entry.
    """
    first = SEED_REQUESTS[0]
    sql = (
        f'SELECT id::text FROM {ENTRY_TABLE} '  # nosec B608 -- module constant, scenario literals
        f"WHERE asset_symbol = '{first.symbol}' AND owner = '{first.owner}' "
        f"AND start = TIMESTAMPTZ '{first.start.isoformat()}' AND \"end\" = TIMESTAMPTZ '{first.end.isoformat()}';"
    )
    rows = stack.query(sql, 'seed dataset lookup')
    if len(rows) != 1 or len(rows[0]) != 1 or not isinstance(rows[0][0], str):
        raise SeedRefused(f"the scenario's first dataset is {len(rows)} entries; expected exactly one")
    return uuid.UUID(rows[0][0]), first.symbol


def _refuse_wrong_ingest_records(stack: Stack) -> None:
    """Refuse unless the three ingest tables hold exactly the cast ingest_records.py writes, plus the POSTed runs.

    Counts only, never a value.
    """
    states = {
        str(row[0]): row[1]
        for row in stack.query(
            f'SELECT state::text, count(*) FROM {REQUEST_TABLE} GROUP BY state;',  # nosec B608 -- module constant
            'request states',
        )
    }
    if states != EXPECTED_REQUEST_STATES:
        raise SeedRefused(
            'the seeded ingest requests are not the expected cast (counts per state differ); no seed written'
        )
    attempts = _query_count(
        stack,
        f'SELECT count(*) FROM {RUN_TABLE} WHERE request_id IS NOT NULL AND attempt_no IS NOT NULL;',  # nosec B608
        'attempt rows',
    )
    if attempts != EXPECTED_ATTEMPT_ROWS:
        raise SeedRefused(f'{attempts} attempt rows were written; expected {EXPECTED_ATTEMPT_ROWS}; no seed written')
    stray = _query_count(
        stack,
        f'SELECT count(*) FROM {RUN_TABLE} WHERE (request_id IS NULL) <> (attempt_no IS NULL);',  # nosec B608
        'half-attempt rows',
    )
    if stray:
        raise SeedRefused('a run has a request id without an attempt number or the reverse; no seed written')
    instances = _query_count(stack, f'SELECT count(*) FROM {INSTANCE_TABLE};', 'instance count')  # nosec B608
    if instances != EXPECTED_INSTANCES:
        raise SeedRefused(f'{instances} ingest instances are held; expected {EXPECTED_INSTANCES}; no seed written')
    superseded_sql = f'SELECT count(*) FROM {INSTANCE_TABLE} WHERE superseded_at IS NOT NULL;'  # nosec B608
    superseded = _query_count(stack, superseded_sql, 'superseded count')
    if superseded != EXPECTED_INSTANCES - 1:
        raise SeedRefused('the instance marker did not supersede exactly the older instance; no seed written')


def _column_names(stack: Stack, table: str) -> list[str]:
    sql = (
        'SELECT column_name FROM information_schema.columns '  # nosec B608 -- table is a module constant
        f"WHERE table_schema = 'public' AND table_name = '{table}' ORDER BY column_name;"
    )
    names = [row[0] for row in stack.query(sql, f'{table} columns')]
    if not names or not all(isinstance(name, str) for name in names):
        raise StackError(f'{table} columns: the query did not return column names')
    return names


def _render_dump(stack: Stack, bar_count: int) -> str:
    """The seed's .sql: each table's INSERT lines rendered by Postgres, then the sequence position.

    Raises:
        StackError: If a rendered row is not one text value.
        DumpRefused: If a rendered line starts with a backslash.
    """
    lines: list[str] = []
    for table in SEED_TABLES:
        for row in stack.query(render_table_sql(table, _column_names(stack, table)), f'{table} render'):
            if len(row) != 1 or not isinstance(row[0], str):
                raise StackError(f'{table} render: a row is not one text value')
            lines.append(row[0])
    text = ''.join(f'{line}\n' for line in lines)
    check_no_meta_commands(text)
    return with_sequence_position(text, bar_count)


def _column_digests(stack: Stack, table: str) -> dict[str, str]:
    rows = stack.query(column_digest_sql(table, _column_names(stack, table)), f'{table} digests')
    digests: dict[str, str] = {}
    for row in rows:
        if len(row) != 2 or not all(isinstance(item, str) for item in row):
            raise StackError(f'{table} digests: a row is not a (column, digest) pair')
        digests[row[0]] = row[1]
    return digests


def produce(stack: Stack, date: str | None = None, seed_records: SeedRecords = seed_ingest_records) -> SeedResult:
    """Run the whole producer against a stack and return the seed as a bundle.

    Args:
        stack (Stack): The scratch stack.
        date (str | None): The manifest's UTC date, YYYY-MM-DD; today's when None.
        seed_records (SeedRecords): Writes the ingest records through the store's own code; a test injects its own.

    Returns:
        SeedResult: The bundle and the row counts. Nothing is written.

    Raises:
        SeedRefused: On a revision mismatch or an alembic_version that does not hold exactly one row,
            a non-synthetic row, a failed POST, a POST whose data_points does not match its symbol,
            no old-key bar collision, or a bundle over the size cap.
        StackError: If a call against the stack fails, or a query returns anything but the shape
            asked for (a count that is not one non-negative integer, a row that is not the expected
            values). A missing setting raises the same when the stack is built.
        DumpRefused: If the rendered dump holds a line starting with a backslash.
    """
    revision = _current_revision(stack)
    head = head_revision(VERSIONS_DIR)
    if revision != head:
        raise SeedRefused(f'the database is at revision {revision}, the checkout head is {head}; migrate first')

    # Before the first POST as well as before the rewrite: a database that already holds a real symbol or owner
    # is not touched at all, not even by the synthetic requests.
    _refuse_foreign_rows(stack)
    # The three ingest tables are rebuilt from nothing, so a run on a scratch database that was seeded before
    # gives the same rows (every row left is synthetic: the refusal above passed). The instance marker has no
    # label to check; a scratch stack's own ingest instance is dropped here and re-announces itself.
    stack.script(f'DELETE FROM {RUN_TABLE}; DELETE FROM {REQUEST_TABLE}; DELETE FROM {INSTANCE_TABLE};', 'ingest reset')  # nosec B608
    for index, request in enumerate(SEED_REQUESTS):
        reply = stack.post(request.path, request.body(), INSTANCE_SECRET_HEADER)
        if reply.get('status') != 200:
            raise SeedRefused(f'request {index} (POST {request.path}) answered {reply.get("status")}')
        _check_stored(index, request, reply)

    _refuse_foreign_rows(stack)
    _refuse_no_bar_collision(stack)
    # The ingest records, through the store's own code (ingest_records.py), before anything is rewritten.
    dataset_id, symbol = _dataset_to_write_under(stack)
    asyncio.run(seed_records(stack.async_url(), dataset_id, symbol))
    _refuse_wrong_ingest_records(stack)
    _refuse_foreign_rows(stack)
    stack.script(NORMALISE_SQL, 'normalise')
    row_counts = {
        table: _query_count(stack, f'SELECT count(*) FROM {table};', f'{table} count')  # nosec B608 -- table is a module constant
        for table in SEED_TABLES
    }
    sql_text = _render_dump(stack, row_counts[BAR_TABLE])
    digests = {table: _column_digests(stack, table) for table in SEED_TABLES}
    manifest = build_manifest(revision, date or datetime.now(UTC).date().isoformat(), row_counts, digests)

    bundle = Bundle(revision, sql_text, render_manifest(manifest))
    try:
        render_bundle(bundle)
    except BundleRefused as refusal:
        raise SeedRefused(str(refusal)) from None
    return SeedResult(bundle, row_counts)
