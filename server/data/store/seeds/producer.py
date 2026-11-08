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
     symbol or owner. The store's own boot-time asset snapshot is not foreign: scheduler ASSET_LIST rows and the
     fake broker's listed symbols are skipped (tj-7zjov1.195); the reset deletes them. Then the COLLISION REFUSAL: at least one
     (asset_symbol, source, granularity, timestamp) group must hold more than one bar row, the old
     four-column bar key collision the head seed exists to carry (decision tj-vhboky.55, E4).
  3b. Rebuild the three ingest tables, corporate_action and the three asset-list tables (beads tj-7zjov1.100,
     tj-7zjov1.31, tj-7zjov1.30; the cast also holds asset-list requests and their snapshots). Since the push path
     the scenario's own POSTs write request and attempt rows (random ids, trigger REQUEST), so the tables are
     emptied AFTER the POSTs, immediately before the cast of ingest_records.py, which is written through the
     store's own code (create_request, mark_queued, DeliveryIntake, and four legacy-shaped runs through the legacy
     recorder's crud) and then counted: the seed holds the cast
     alone, and a seed whose request states, attempt rows, instance marker or observations differ from it is
     refused, naming the counts found and expected (never a value), and every cast request is judged by its id
     against its own expected state and instance (EXPECTED_REQUESTS). The emptying is also done before the POSTs.
     The store's own loops can still write in the window: the instance watcher's probe notes the live ingest's
     instance id, which the emptying made a first sighting, so it supersedes the cast's live instance and moves its
     QUEUED request to NOT_DELIVERED (tj-7zjov1.166). A cast that drifted is a CastDrifted refusal; the whole cast
     (emptying, cast, normalise, dump) is then run again, up to CAST_ATTEMPTS times, and refused if it never settles.
  4. Normalise the tables (dump.py: why), then, in ONE REPEATABLE READ snapshot, repeat the refusals and render
     the tables, count them and take their digests. The checks and the dump therefore judge the same rows: a row the
     store's loops change after the snapshot opens is in neither, and one changed before it is refused.
  5. Build the manifest and return the bundle (bundle.py: the contract), last, and only when every
     earlier step passed. __main__ prints it as the one stdout line.

The database is the caller's scratch database, named by test_client's environment. Step 4 rewrites
the two market tables in it. Never point this at a stack that holds data you want.

Where the files go is not the producer's business: `python -m data.store.seeds.bundle` (make
seed-dump) and the MCP's own reader write them from the bundle.
"""

import asyncio
import contextlib
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
    CORPORATE_ACTION_TABLE,
    ENTRY_TABLE,
    INSTANCE_TABLE,
    NORMALISE_SQL,
    REQUEST_TABLE,
    RUN_TABLE,
    SEED_TABLES,
    SNAPSHOT_EVENT_TABLE,
    SNAPSHOT_ROW_TABLE,
    SNAPSHOT_TABLE,
    check_no_meta_commands,
    render_table_sql,
    with_sequence_position,
)
from data.store.seeds.ingest_records import (
    EXPECTED_ASSET_LIST_REQUESTS,
    EXPECTED_ATTEMPT_ROWS,
    EXPECTED_EMPTY_SNAPSHOTS,
    EXPECTED_EVENTS,
    EXPECTED_INSTANCE_SUPERSEDED,
    EXPECTED_INSTANCES,
    EXPECTED_INTERRUPTED_RUNS,
    EXPECTED_LEGACY_RUNS,
    EXPECTED_LEGACY_STATES,
    EXPECTED_OBSERVATIONS,
    EXPECTED_REQUEST_STATES,
    EXPECTED_REQUESTS,
    EXPECTED_REVISED_ACTIONS,
    EXPECTED_RUN_ROWS,
    EXPECTED_SNAPSHOT_ROWS,
    EXPECTED_SNAPSHOTS,
    INSTANCES,
    seed_ingest_records,
)
from data.store.seeds.manifest import build_manifest, column_digest_sql, head_revision, render_manifest
from data.store.seeds.scenario import OWNER_PATTERN, SEED_REQUESTS, SeedRequest, is_synthetic
from data.store.seeds.stack import Stack, StackError
from routers.common.instance_secret import INSTANCE_SECRET_HEADER
from tests.fakes.market_data import EMPTY_PREFIX, FAKE_DELISTED_SYMBOLS, FAKE_LISTED_SYMBOLS


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


# How many times the whole cast is run before the producer gives up on a stack whose own loops keep changing it.
CAST_ATTEMPTS = 5

# The snapshot the checks and the dump share. READ ONLY: nothing in it writes, and the rollback at its end is the close.
SNAPSHOT_BEGIN = 'BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;'
SNAPSHOT_END = 'ROLLBACK;'


class SeedRefused(Exception):
    """The producer will not produce a seed. The message never carries a row value."""


class CastDrifted(SeedRefused):
    """The rows the cast left are not the cast: something else wrote in the window. The run may be repeated."""


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
    # The ingest tables carry the same labels: a request's client_label and a run's actor are the owner. A request or run
    # with no symbol (an asset-list request, its attempt row) has no symbol to check: kind 'l' judges its owner alone,
    # while a bars or corporate-actions request with a NULL symbol still reads as foreign. ingest_instance has no label;
    # a corporate_action row has only the symbol its request asked for; the asset-list tables carry the vendor's symbols
    # ('a' rows, 'v' events, 'p' the previous symbol of a flip) and no owner.
    # Two kinds of row are the store's own boot-time asset snapshot (P47), not data a person put there, and are not
    # foreign: a kind 'l' row whose owner is exactly SCHEDULER_ACTOR (the ASSET_LIST request and its attempt row; a
    # scheduler BARS or corporate-actions row is still judged by its symbol as 'q'/'r'), and a kind 'a', 'v' or 'p'
    # row whose symbol is one the fake broker lists. SYMBOL_PATTERN, OWNER_PATTERN and is_synthetic are unchanged,
    # so a real symbol still counts and a database holding a real vendor list is still refused. The reset that
    # follows deletes these rows; any written mid-cast move the cast's counts and read as CastDrifted.
    # Imported here, not at the top: the app package pulls in the database layer, as ingest_records.py's imports do.
    from data.store.app.daily_refresh import SCHEDULER_ACTOR

    fake_listed = {*FAKE_LISTED_SYMBOLS, *FAKE_DELISTED_SYMBOLS}
    sql = (
        f"SELECT 'e', asset_symbol, owner, count(*) FROM {ENTRY_TABLE} GROUP BY asset_symbol, owner "  # nosec B608 -- module constants only
        f"UNION ALL SELECT 'b', asset_symbol, '', count(*) FROM {BAR_TABLE} GROUP BY asset_symbol "
        f"UNION ALL SELECT 'q', coalesce(asset_symbol, ''), client_label, count(*) FROM {REQUEST_TABLE} "
        "WHERE kind <> 'ASSET_LIST' GROUP BY asset_symbol, client_label "
        f"UNION ALL SELECT 'l', '', client_label, count(*) FROM {REQUEST_TABLE} WHERE kind = 'ASSET_LIST' "
        'GROUP BY client_label '
        f"UNION ALL SELECT 'r', asset_symbol, actor, count(*) FROM {RUN_TABLE} WHERE asset_symbol IS NOT NULL "
        'GROUP BY asset_symbol, actor '
        f"UNION ALL SELECT 'l', '', actor, count(*) FROM {RUN_TABLE} WHERE asset_symbol IS NULL GROUP BY actor "
        f"UNION ALL SELECT 'c', coalesce(requesting_symbol, ''), '', count(*) FROM {CORPORATE_ACTION_TABLE} "
        'GROUP BY requesting_symbol '
        f"UNION ALL SELECT 'a', symbol, '', count(*) FROM {SNAPSHOT_ROW_TABLE} GROUP BY symbol "
        f"UNION ALL SELECT 'v', symbol, '', count(*) FROM {SNAPSHOT_EVENT_TABLE} GROUP BY symbol "
        f"UNION ALL SELECT 'p', previous_symbol, '', count(*) FROM {SNAPSHOT_EVENT_TABLE} "
        'WHERE previous_symbol IS NOT NULL GROUP BY previous_symbol;'
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
        if kind == 'l':
            # No symbol to judge: the owner alone.
            if owner != SCHEDULER_ACTOR and not OWNER_PATTERN.fullmatch(owner):
                foreign += count
        elif kind in ('a', 'v', 'p') and symbol in fake_listed:
            continue
        elif not is_synthetic(symbol, owner if kind in ('e', 'q', 'r') else None):
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


def _render_counts(counts: dict[str, Any]) -> str:
    """Counts per state as 'STATE=n' pairs in name order; the keys are enum names, the values counts.

    A value that is not an integer (a count that came back as text) is shown with its type and value (found
    SUCCEEDED=str '3', expected SUCCEEDED=3), as test_seed_producer.py pins for the state counts. These come only from the
    cast's own GROUP BY count queries, whose keys are enum names; _query_count, which can hold any single value, is
    value-free.
    """
    return (
        ', '.join(
            f'{name}={counts[name]}'
            if isinstance(counts[name], int) and not isinstance(counts[name], bool)
            else f'{name}={type(counts[name]).__name__} {counts[name]!r}'
            for name in sorted(counts)
        )
        or 'none'
    )


def _instance_label(instance: str | None) -> str:
    """A request's instance as the message names it: the cast's own ids, else only that it is another one."""
    if instance is None:
        return 'none'
    return instance if instance in INSTANCES else 'another instance'


def _refuse_wrong_requests(stack: Stack) -> None:
    """Refuse unless every cast request, by its id, holds its own expected state and instance.

    Counts per state cannot tell a request that flipped from one that never was (the cast holds QUEUED 1 and
    NOT_DELIVERED 1; a flip moves one from each, and a second flip elsewhere would cancel it). The message names the
    cast's own ids (5eed...), their states (enum names) and, for an instance, a cast id or 'another instance'; the
    count of requests outside the cast. Never a symbol, an owner or an instance value of the stack's own.
    """
    found: dict[str, tuple[str, str | None]] = {}
    for row in stack.query(f'SELECT id::text, state::text, instance_id FROM {REQUEST_TABLE};', 'request rows'):  # nosec B608
        if len(row) != 3 or not isinstance(row[0], str) or not isinstance(row[1], str):
            raise StackError('request rows: a row is not (id, state, instance)')
        if row[2] is not None and not isinstance(row[2], str):
            raise StackError('request rows: a row is not (id, state, instance)')
        found[row[0]] = (row[1], row[2])
    wrong: list[str] = []
    for request_id, (state, instance) in sorted(EXPECTED_REQUESTS.items()):
        got = found.get(request_id)
        if got is None:
            wrong.append(f'{request_id} is missing')
        elif got != (state, instance):
            wrong.append(
                f'{request_id} holds {got[0]} under {_instance_label(got[1])}, '
                f'expected {state} under {_instance_label(instance)}'
            )
    outside = sum(1 for request_id in found if request_id not in EXPECTED_REQUESTS)
    if wrong or outside:
        raise SeedRefused(
            f'the seeded ingest requests are not the expected cast ({len(wrong)} cast request(s) differ, '
            f'{outside} outside the cast): {"; ".join(wrong) or "none"}; no seed written'
        )


def _refuse_wrong_instances(stack: Stack) -> None:
    """Refuse unless the instance marker holds exactly the cast's two instances, A superseded and B live."""
    found: dict[str, Any] = {}
    for row in stack.query(f'SELECT instance_id, superseded_at IS NOT NULL FROM {INSTANCE_TABLE};', 'instance rows'):  # nosec B608
        if len(row) != 2 or not isinstance(row[0], str) or not isinstance(row[1], bool):
            raise StackError('instance rows: a row is not (instance, superseded)')
        found[row[0]] = row[1]
    outside = sum(1 for instance in found if instance not in EXPECTED_INSTANCE_SUPERSEDED)
    wrong = [
        f'{instance} is {"missing" if instance not in found else "superseded" if found[instance] else "live"}, '
        f'expected {"superseded" if expected else "live"}'
        for instance, expected in sorted(EXPECTED_INSTANCE_SUPERSEDED.items())
        if found.get(instance) is not expected
    ]
    if wrong or outside:
        raise SeedRefused(
            f'the instance marker is not the expected cast ({outside} instance(s) outside the cast): '
            f'{"; ".join(wrong) or "none"}; no seed written'
        )


def _refuse_wrong_ingest_records(stack: Stack) -> None:
    """Refuse unless the ingest tables hold exactly the cast ingest_records.py writes, each request judged by its id.

    Raises:
        CastDrifted: If they do not (a SeedRefused naming counts and cast ids, never a value); the cast may be run again.
    """
    try:
        _check_cast_counts(stack)
        _refuse_wrong_requests(stack)
        _refuse_wrong_instances(stack)
    except SeedRefused as refusal:
        raise CastDrifted(str(refusal)) from None


def _check_cast_counts(stack: Stack) -> None:
    """The count checks of the cast (attempt rows, legacy runs, instances, observations, asset lists).

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
            'the seeded ingest requests are not the expected cast (counts per state differ); '
            f'found {_render_counts(states)}, expected {_render_counts(EXPECTED_REQUEST_STATES)}; no seed written'
        )
    attempts = _query_count(
        stack,
        f'SELECT count(*) FROM {RUN_TABLE} WHERE request_id IS NOT NULL AND attempt_no IS NOT NULL;',  # nosec B608
        'attempt rows',
    )
    if attempts != EXPECTED_ATTEMPT_ROWS:
        raise SeedRefused(f'{attempts} attempt rows were written; expected {EXPECTED_ATTEMPT_ROWS}; no seed written')
    legacy_states = {
        str(row[0]): row[1]
        for row in stack.query(
            f'SELECT state::text, count(*) FROM {RUN_TABLE} WHERE request_id IS NULL AND attempt_no IS NULL GROUP BY state;',  # nosec B608
            'legacy run states',
        )
    }
    if legacy_states != EXPECTED_LEGACY_STATES:
        raise SeedRefused(
            f'the seeded legacy runs are not the expected cast ({sum(legacy_states.values())} found, '
            f'{EXPECTED_LEGACY_RUNS} expected); counts per state: found {_render_counts(legacy_states)}, '
            f'expected {_render_counts(EXPECTED_LEGACY_STATES)}; no seed written'
        )
    interrupted = _query_count(
        stack,
        f"SELECT count(*) FROM {RUN_TABLE} WHERE request_id IS NULL AND error_reason = 'INTERRUPTED';",  # nosec B608
        'interrupted runs',
    )
    if interrupted != EXPECTED_INTERRUPTED_RUNS:
        raise SeedRefused(
            f'{interrupted} interrupted legacy runs were written; expected {EXPECTED_INTERRUPTED_RUNS}; no seed written'
        )
    runs = _query_count(stack, f'SELECT count(*) FROM {RUN_TABLE};', 'run rows')  # nosec B608
    if runs != EXPECTED_RUN_ROWS:
        raise SeedRefused(f'{runs} ingest_run rows were written; expected {EXPECTED_RUN_ROWS}; no seed written')
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
    observations = _query_count(stack, f'SELECT count(*) FROM {CORPORATE_ACTION_TABLE};', 'observation count')  # nosec B608
    if observations != EXPECTED_OBSERVATIONS:
        raise SeedRefused(
            f'{observations} corporate-action observations were written; expected {EXPECTED_OBSERVATIONS}; no seed written'
        )
    revised_sql = (
        'SELECT count(*) FROM (SELECT 1 FROM '  # nosec B608 -- module constant
        f'{CORPORATE_ACTION_TABLE} GROUP BY source, vendor_action_id HAVING count(*) > 1) AS revised;'
    )
    revised = _query_count(stack, revised_sql, 'revised action count')
    if revised != EXPECTED_REVISED_ACTIONS:
        raise SeedRefused(
            f'{revised} corporate actions hold more than one payload; expected {EXPECTED_REVISED_ACTIONS}; no seed written'
        )
    _refuse_wrong_asset_lists(stack)


def _counts_by(stack: Stack, sql: str, what: str) -> dict[str, Any]:
    """A (label, count) query as a dict, the counts as the stack returned them (a text count stays text)."""
    return {str(row[0]): row[1] for row in stack.query(sql, what)}


def _refuse_wrong_asset_lists(stack: Stack) -> None:
    """Refuse unless the asset-list tables hold exactly the cast.

    Checked: requests and snapshots per list, the empty snapshot, the asset rows and the events per kind. Each refusal
    names what was found and what was expected (counts only).
    """
    requests = _counts_by(
        stack,
        f"SELECT asset_list_status::text, count(*) FROM {REQUEST_TABLE} WHERE kind = 'ASSET_LIST' GROUP BY asset_list_status;",  # nosec B608
        'asset-list requests',
    )
    if requests != EXPECTED_ASSET_LIST_REQUESTS:
        raise SeedRefused(
            'the seeded asset-list requests are not the expected cast (counts per list differ); '
            f'found {_render_counts(requests)}, expected {_render_counts(EXPECTED_ASSET_LIST_REQUESTS)}; no seed written'
        )
    snapshots = _counts_by(
        stack,
        f'SELECT asset_list::text, count(*) FROM {SNAPSHOT_TABLE} GROUP BY asset_list;',  # nosec B608
        'asset-list snapshots',
    )
    if snapshots != EXPECTED_SNAPSHOTS:
        raise SeedRefused(
            'the seeded asset-list snapshots are not the expected cast (counts per list differ); '
            f'found {_render_counts(snapshots)}, expected {_render_counts(EXPECTED_SNAPSHOTS)}; no seed written'
        )
    empty = _query_count(stack, f'SELECT count(*) FROM {SNAPSHOT_TABLE} WHERE row_count = 0;', 'empty snapshots')  # nosec B608
    if empty != EXPECTED_EMPTY_SNAPSHOTS:
        raise SeedRefused(
            f'found {empty} empty asset-list snapshots, expected {EXPECTED_EMPTY_SNAPSHOTS}; no seed written'
        )
    rows = _query_count(stack, f'SELECT count(*) FROM {SNAPSHOT_ROW_TABLE};', 'snapshot rows')  # nosec B608
    if rows != EXPECTED_SNAPSHOT_ROWS:
        raise SeedRefused(f'found {rows} asset-list rows, expected {EXPECTED_SNAPSHOT_ROWS}; no seed written')
    stray = _query_count(
        stack,
        f'SELECT count(*) FROM {SNAPSHOT_TABLE} s WHERE s.row_count <> (SELECT count(*) FROM {SNAPSHOT_ROW_TABLE} r WHERE r.snapshot_id = s.id);',  # nosec B608
        'snapshots whose row_count differs from their rows',
    )
    if stray:
        raise SeedRefused(
            f'found {stray} snapshot(s) whose row_count differs from the rows they hold, expected 0; no seed written'
        )
    events = _counts_by(
        stack,
        f'SELECT kind::text, count(*) FROM {SNAPSHOT_EVENT_TABLE} GROUP BY kind;',  # nosec B608
        'asset-list events',
    )
    if events != EXPECTED_EVENTS:
        raise SeedRefused(
            'the seeded asset-list events are not the expected cast (counts per kind differ); '
            f'found {_render_counts(events)}, expected {_render_counts(EXPECTED_EVENTS)}; no seed written'
        )


def _reset_ingest_records(stack: Stack) -> None:
    """Empty the three ingest tables and corporate_action.

    Every row left is synthetic when this runs (the foreign-row refusal passed first). The instance marker has no
    label to check; a scratch stack's own ingest instance is dropped here and re-announces itself.
    """
    stack.script(
        f'DELETE FROM {RUN_TABLE}; DELETE FROM {REQUEST_TABLE}; DELETE FROM {INSTANCE_TABLE}; '  # nosec B608 -- module constants
        f'DELETE FROM {CORPORATE_ACTION_TABLE}; DELETE FROM {SNAPSHOT_EVENT_TABLE}; DELETE FROM {SNAPSHOT_ROW_TABLE}; '
        f'DELETE FROM {SNAPSHOT_TABLE};',
        'ingest reset',
    )


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


def _cast_and_dump(stack: Stack, seed_records: SeedRecords) -> tuple[dict[str, int], str, dict[str, dict[str, str]]]:
    """One attempt: empty the tables, write the cast, normalise, then check and dump in one snapshot.

    Returns:
        tuple: The row count per table, the rendered .sql, and the column digests per table, all read from the same
            snapshot the refusals ran in.

    Raises:
        CastDrifted: If the cast the store's own loops disturbed is not the cast, before or after the rewrite.
        SeedRefused: If a row outside the synthetic patterns appeared.
    """
    # The entry is looked up per attempt: a normalise of an earlier attempt rewrote its id.
    dataset_id, symbol = _dataset_to_write_under(stack)
    # The POSTs (or an earlier attempt) wrote request and attempt rows of their own. Empty the tables NOW, right before
    # the cast, so the seed holds the cast alone and the store's loops have the shortest window to add a row of theirs.
    _reset_ingest_records(stack)
    # The ingest records, through the store's own code (ingest_records.py), before anything is rewritten.
    asyncio.run(seed_records(stack.async_url(), dataset_id, symbol))
    _refuse_wrong_ingest_records(stack)
    _refuse_foreign_rows(stack)
    stack.script(NORMALISE_SQL, 'normalise')
    # The dump is read in ONE snapshot and the refusals run in it: a row the store's loops change after the snapshot
    # opens is in neither the checks nor the dump, one changed before it is refused here (tj-7zjov1.166).
    stack.script(SNAPSHOT_BEGIN, 'dump snapshot begin')
    try:
        _refuse_foreign_rows(stack)
        _refuse_wrong_ingest_records(stack)
        row_counts = {
            table: _query_count(stack, f'SELECT count(*) FROM {table};', f'{table} count')  # nosec B608 -- table is a module constant
            for table in SEED_TABLES
        }
        sql_text = _render_dump(stack, row_counts[BAR_TABLE])
        digests = {table: _column_digests(stack, table) for table in SEED_TABLES}
    except BaseException:
        # The failure is the report; a rollback that fails too must not replace it.
        with contextlib.suppress(StackError):
            stack.script(SNAPSHOT_END, 'dump snapshot end')
        raise
    stack.script(SNAPSHOT_END, 'dump snapshot end')
    return row_counts, sql_text, digests


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
            no old-key bar collision, a cast that never settled (CastDrifted on every one of CAST_ATTEMPTS
            attempts, the last refusal re-raised unchanged), or a bundle over the size cap.
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
    _reset_ingest_records(stack)
    for index, request in enumerate(SEED_REQUESTS):
        reply = stack.post(request.path, request.body(), INSTANCE_SECRET_HEADER)
        if reply.get('status') != 200:
            raise SeedRefused(f'request {index} (POST {request.path}) answered {reply.get("status")}')
        _check_stored(index, request, reply)

    _refuse_foreign_rows(stack)
    _refuse_no_bar_collision(stack)
    last_drift: CastDrifted | None = None
    for _attempt in range(CAST_ATTEMPTS):
        try:
            row_counts, sql_text, digests = _cast_and_dump(stack, seed_records)
        except CastDrifted as drift:
            last_drift = drift
            continue
        break
    else:
        # The last refusal, unchanged: its message names what differed in the final attempt, and a cast that drifted
        # every time is that refusal repeated CAST_ATTEMPTS times.
        raise last_drift
    manifest = build_manifest(revision, date or datetime.now(UTC).date().isoformat(), row_counts, digests)

    bundle = Bundle(revision, sql_text, render_manifest(manifest))
    try:
        render_bundle(bundle)
    except BundleRefused as refusal:
        raise SeedRefused(str(refusal)) from None
    return SeedResult(bundle, row_counts)
