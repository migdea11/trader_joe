"""The name_change clamp at the first request (P51, tj-7zjov1.146): the start a POST asks for, moved past a rename away.

THE DESIGN: ADR tj-26jt9w addendum 19 (the architect's ruling A of 2026-10-27 01:29 UTC on tj-7zjov1.146, with the
answer to the builder's question), the bead's acceptance (FB from 2015 starts at 2022-06-09; META from 2015 is not
clamped; no record, no clamp; no re-query on a later refresh; the clamp reported), the owner's approval of 2026-10-26
(D9 tj-7zjov1.142: the LATEST process date when several qualify) and the epic's 17:25 UTC note (the query's window
is wide, from the earliest supported date to now). In short, as ruled:

  (1) the clamp runs on EVERY POST (a bars request with no dataset id), BEFORE the own-overlap check and BEFORE the
      bars row is written; the clamped start replaces the body's start for the overlap key, requested_start and so
      the entry: it is the entry's IDENTITY, so an exact repeat clamps to the same start and extends its entry. The
      DAILY loop and every re-send never clamp and never query.
  (2) one corporate-actions request for the symbol over CALENDAR_START to now, written, submitted and awaited.
  (3) the read: every stored name_change naming the symbol as old or new symbol, from ANY request, the newest
      observation per vendor action id; no process date is ignored with one WARNING count; a candidate (old symbol =
      the symbol) is dropped when a STRICTLY later row makes the symbol a new symbol; the clamp is the latest
      remaining process date at the local midnight of the source's calendar (UTC midnight without one), applied
      only when after the body's start.
  (4) clamped start at or after the end: INVALID_REQUEST naming the symbol and date, nothing else written or sent.
  (5) fail closed: the query failing, expiring or missing the deadline fails the POST with that error and no bars
      row; one deadline covers both waits.
  (6) reporting: served_range.start, the route message, one INFO line with both starts and both request ids.
  (7) the watcher re-sends corporate-actions rows (test_instance_watch.py).
  COST ACCEPTED, pinned as built: a later name_change can turn a repeat into a 409 own overlap.

THE TIERS. name_change_clamp (the rule) is pure and driven directly. name_changes_naming (the read) is Postgres-only
(JSONB astext, DISTINCT ON): its statement is compiled here and RUN in tests/system/test_name_change_clamp_pg.py,
which pins "any request" and "newest observation per id" against real rows. The worker runs over the SQLite request
path (request_path_harness.py) with the read answered from a NameChangeBook (name_change_doubles.py) and ingest played
inside the waiter's sleep; the route over post_path.py's world.
"""

import logging
import types
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from common.errors.vocabulary import REASONS, ExogenousError, InvalidRequestError, Reason
from data.store.app import corporate_actions as corporate_actions_module
from data.store.app import daily_refresh
from data.store.app.corporate_actions import name_change_clamp
from data.store.app.daily_refresh import SCHEDULER_ACTOR, DailyRefresher, _Planned
from data.store.app.database.crud import corporate_action as crud
from data.store.app.database.crud.corporate_action import NameChange, name_changes_naming
from data.store.app.database.crud.stock.store_dataset_entry import OwnOverlapConflict
from data.store.app.database.models.ingest_request import IngestRequestKind, IngestRequestState
from data.store.app.database.models.ingest_run import IngestRunTrigger
from data.store.app.ingest import data_action_request
from data.store.app.ingest.data_action_request import StartClamp, store_market_activity_worker
from data.store.app.trading_calendar import UnknownCalendarError, calendar_for_source, local_midnight
from data.store.tests.delivery_reports import report
from data.store.tests.name_change_doubles import Plays, actions_answer, bars_of, name_change, queries_of, vendor_date
from data.store.tests.post_path import Delivery, PostStore, QueryAnswer, SubmitDouble, post
from data.store.tests.post_path import post_store as _post_store  # noqa: F401 (a fixture)
from data.store.tests.problem_body import problem
from data.store.tests.request_path_harness import OWNER, FakeSubmit, Harness, make_engine
from data.store.tests.test_ui_datasets_route import aware_sqlite_datetimes  # noqa: F401 (a fixture)
from schemas.data_ingest.delivery import DeliveryErrorClass, DeliveryOutcome
from schemas.data_ingest.fetch_dataset import ServedRange
from schemas.data_store.asset_dataset_store import StoreAssetDatasetBody, StoreAssetDatasetPath


pytestmark = [pytest.mark.data_store]

XNYS = calendar_for_source(DataSource.ALPACA_API)
FB = StoreAssetDatasetPath(asset_type=AssetType.STOCK, data_type=DataType.MARKET_ACTIVITY, asset_symbol='FB')
META = StoreAssetDatasetPath(asset_type=AssetType.STOCK, data_type=DataType.MARKET_ACTIVITY, asset_symbol='META')
ASKED = datetime(2015, 1, 2, tzinfo=UTC)
# The bead's acceptance: FB from 2015 starts at 2022-06-09, at the local midnight of the source's calendar.
RENAMED = date(2022, 6, 9)
CLAMPED = local_midnight(RENAMED, XNYS)
FB_TO_META = name_change('fb-to-meta', 'FB', 'META', vendor_date(2022, 6, 9))
META_TO_METV = name_change('meta-to-metv', 'META', 'METV', vendor_date(2022, 1, 31))
READY_BY = datetime(2026, 3, 9, 13, 30, tzinfo=UTC)
# The route tier's body: post_path.py's path is AAPL.
ROUTE_BODY = {'owner': 'clamp-route-owner', 'source': 'ALPACA', 'granularity': '1day', 'start': ASKED.isoformat()}


def body(**overrides) -> StoreAssetDatasetBody:
    values = {
        'owner': OWNER,
        'source': DataSource.ALPACA_API,
        'granularity': Granularity.ONE_DAY,
        'start': ASKED,
        'update_type': UpdateType.DAILY,
        'expiry_type': ExpiryType.ROLLING,
        'ready_by': READY_BY,
    }
    return StoreAssetDatasetBody(**{**values, **overrides})


def static(end: datetime, **overrides) -> StoreAssetDatasetBody:
    return body(update_type=UpdateType.STATIC, expiry_type=ExpiryType.BULK, end=end, **overrides)


@pytest.fixture
def store(_post_store: PostStore) -> PostStore:  # noqa: F811 (the imported fixture, under its own name here)
    """post_path.py's SQLite world, for the route cases."""
    return _post_store


@pytest_asyncio.fixture
async def harness(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Harness]:
    engine = await make_engine(monkeypatch)
    try:
        yield Harness(engine, monkeypatch)
    finally:
        await engine.dispose()


async def _post(harness: Harness, plays: Plays, payload: StoreAssetDatasetBody, path=FB, deadline_s: float = 10):
    async with AsyncSession(harness.engine, expire_on_commit=False) as db:
        return await store_market_activity_worker(path, payload, db, plays.client, deadline_s=deadline_s)


def _plays(harness: Harness, monkeypatch: pytest.MonkeyPatch, **scripts) -> Plays:
    return Plays(harness.deliver, monkeypatch, FakeSubmit(), **scripts)


# =============================================================================================== (3) the rule, pure


def test_fb_from_2015_is_clamped_to_its_rename_away_on_2022_06_09():
    assert name_change_clamp([FB_TO_META, META_TO_METV], 'FB') == vendor_date(2022, 6, 9)


def test_meta_is_not_clamped_because_fb_made_it_a_new_symbol_strictly_later():
    """The rename-and-back exclusion: META to METV (2022-01-31) is dropped by FB to META (2022-06-09)."""
    assert name_change_clamp([FB_TO_META, META_TO_METV], 'META') is None


def test_a_symbol_with_no_record_is_not_clamped():
    assert name_change_clamp([], 'AAPL') is None
    assert name_change_clamp([FB_TO_META], 'AAPL') is None, 'a record naming other symbols only'


def test_a_new_symbol_on_the_same_day_does_not_exclude_the_candidate():
    """STRICTLY later excludes; the same process date does not (the ruling's word, both ways)."""
    same_day = [
        name_change('x-away', 'XX', 'YY', vendor_date(2020, 3, 2)),
        name_change('x-back', 'ZZ', 'XX', vendor_date(2020, 3, 2)),
    ]
    assert name_change_clamp(same_day, 'XX') == vendor_date(2020, 3, 2)
    one_day_later = [same_day[0], name_change('x-back', 'ZZ', 'XX', vendor_date(2020, 3, 3))]
    assert name_change_clamp(one_day_later, 'XX') is None


def test_an_earlier_arrival_does_not_exclude_a_later_rename_away():
    """XX became a new symbol in 2010 and was renamed away in 2015: the 2015 rename stands."""
    changes = [
        name_change('arrive', 'OLD', 'XX', vendor_date(2010, 5, 3)),
        name_change('leave', 'XX', 'NEW', vendor_date(2015, 7, 1)),
    ]
    assert name_change_clamp(changes, 'XX') == vendor_date(2015, 7, 1)


def test_the_latest_remaining_candidate_wins_and_an_arrival_drops_only_the_candidates_before_it():
    """Owner ruling D9 question 2: the LATEST process date. An arrival in 2014 drops 2012's rename, not 2018's."""
    changes = [
        name_change('leave-2012', 'XX', 'A1', vendor_date(2012, 1, 3)),
        name_change('arrive-2014', 'B1', 'XX', vendor_date(2014, 1, 3)),
        name_change('leave-2018', 'XX', 'C1', vendor_date(2018, 1, 3)),
    ]
    assert name_change_clamp(changes, 'XX') == vendor_date(2018, 1, 3)
    assert name_change_clamp(changes[:2], 'XX') is None, 'the only candidate was dropped by the later arrival'
    assert name_change_clamp([changes[0], changes[2]], 'XX') == vendor_date(2018, 1, 3), 'latest, not first'


def test_a_record_with_no_process_date_is_ignored_on_both_sides_with_one_warning_count(caplog):
    """No process date cannot be placed in time: neither a candidate nor an exclusion, one WARNING with the count."""
    caplog.set_level(logging.WARNING, logger=corporate_actions_module.__name__)
    changes = [
        name_change('undated-leave', 'XX', 'A1', None),
        name_change('undated-arrive', 'B1', 'XX', None),
        name_change('dated-leave', 'XX', 'C1', vendor_date(2019, 4, 1)),
    ]

    assert name_change_clamp(changes, 'XX') == vendor_date(2019, 4, 1)
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1, [record.getMessage() for record in warnings]
    assert warnings[0].getMessage().startswith('2 name_change record(s)'), warnings[0].getMessage()
    assert 'XX' not in warnings[0].getMessage()


def test_only_undated_records_clamp_nothing():
    assert name_change_clamp([name_change('undated', 'XX', 'A1', None)], 'XX') is None


# =============================================================================================== (3) the read, compiled


class _RecordingSession:
    """Takes the one statement the read builds, and answers the rows a Postgres DISTINCT ON would return."""

    def __init__(self, rows: list[tuple]) -> None:
        self.statements: list = []
        self._rows = rows

    async def execute(self, statement):
        self.statements.append(statement)
        return types.SimpleNamespace(all=lambda: self._rows)


@pytest.mark.asyncio
async def test_the_read_takes_every_request_newest_observation_per_vendor_id_naming_the_symbol_either_way():
    """The statement production sends, compiled for Postgres (it RUNS in tests/system/test_name_change_clamp_pg.py).

    Pinned: name_change of the source only; the symbol as old_symbol OR new_symbol; one row per vendor action id
    (DISTINCT ON) chosen NEWEST first by as_of, then received_at, then id; and NO filter on the request that delivered
    it or the symbol that asked (the dedupe stores no new row for an observation already seen, so an action another
    request delivered first must still be read). A SELECT, never a write.
    """
    session = _RecordingSession([('fb-to-meta', vendor_date(2022, 6, 9), 'FB', 'META')])

    changes = await name_changes_naming(session, DataSource.ALPACA_API, 'FB')

    assert changes == [NameChange('fb-to-meta', vendor_date(2022, 6, 9), 'FB', 'META')]
    (statement,) = session.statements
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = ' '.join(str(compiled).split())
    assert sql.startswith('SELECT DISTINCT ON (corporate_action.vendor_action_id)'), sql
    assert (
        'ORDER BY corporate_action.vendor_action_id, corporate_action.as_of DESC, '
        'corporate_action.received_at DESC, corporate_action.id DESC'
    ) in sql, sql
    where = sql.split(' WHERE ', 1)[1].split(' ORDER BY ', 1)[0]
    assert 'request_id' not in where and 'requesting_symbol' not in where, f'the read is filtered by request: {where}'
    assert '->> %(identifiers_1)s) = %(param_1)s OR (corporate_action.identifiers ->> %(identifiers_2)s)' in where, (
        where
    )
    params = compiled.params
    assert sorted(value for key, value in params.items() if key.startswith('identifiers')) == [
        'new_symbol',
        'old_symbol',
    ]
    assert sorted(value for key, value in params.items() if key.startswith('param')) == ['FB', 'FB']
    assert 'name_change' in params.values()
    assert DataSource.ALPACA_API in params.values()


def test_the_repository_exports_the_read_and_still_no_update_or_delete():
    """The one new public pair is the read and its row; nothing in the module updates or deletes (append-only)."""
    assert crud.NAME_CHANGE == 'name_change'
    assert (crud.OLD_SYMBOL, crud.NEW_SYMBOL) == ('old_symbol', 'new_symbol')


# =============================================================================================== (1)(2)(6) the POST


@pytest.mark.asyncio
async def test_fb_from_2015_is_requested_written_and_entered_from_2022_06_09_local_midnight(harness, monkeypatch):
    """The acceptance end to end: the query first, then the bars request, its row and the entry all at CLAMPED.

    ONE corporate-actions request for the symbol, trigger REQUEST, the caller's label and ready_by, over CALENDAR_START
    to now, written and sent BEFORE the bars; the bars Submit, requested_start and the entry carry CLAMPED (local
    midnight of 2022-06-09 in XNYS, not UTC midnight); the answer's served range is the window served for that row.
    """
    harness.book.changes = [FB_TO_META, META_TO_METV]
    plays = _plays(harness, monkeypatch)
    before = datetime.now(UTC)

    stored = await _post(harness, plays, body())

    after = datetime.now(UTC)
    query, bars = plays.client.sent
    assert query.corporate_actions is not None and bars.bars is not None, 'the query was not sent before the bars'
    window = query.corporate_actions
    assert (window.source, window.asset_symbol, window.start) == (
        DataSource.ALPACA_API,
        'FB',
        datetime(1990, 1, 1, tzinfo=UTC),
    )
    assert before <= window.end <= after, 'the query window does not end at now'
    assert (query.owner, query.ready_by) == (OWNER, READY_BY)
    (query_row,) = await harness.query_requests()
    assert (query_row.trigger, query_row.client_label, query_row.dataset_id) == (IngestRunTrigger.REQUEST, OWNER, None)
    assert query_row.state is IngestRequestState.EMPTY, 'the query was not awaited to its answer'

    assert bars.bars.start == CLAMPED and datetime(2022, 6, 9, tzinfo=UTC) != CLAMPED
    (bars_row,) = await harness.bars_requests()
    assert bars_row.requested_start == CLAMPED, 'requested_start keeps the asked start: the clamp is wire-only'
    (entry,) = await harness.entries()
    assert entry.start == CLAMPED, 'the entry does not declare the clamped start (addendum 19: it is identity)'
    assert stored.clamp == StartClamp('FB', vendor_date(2022, 6, 9), ASKED, CLAMPED)
    assert stored.served_range.start == CLAMPED
    assert harness.book.calls == [(DataSource.ALPACA_API, 'FB', {})], (
        "the read was not the symbol alone: it must take records from ANY request, never this POST's only"
    )


@pytest.mark.asyncio
async def test_meta_from_2015_and_a_symbol_with_no_record_are_not_clamped(harness, monkeypatch):
    harness.book.changes = [FB_TO_META, META_TO_METV]
    plays = _plays(harness, monkeypatch)

    meta = await _post(harness, plays, body(), path=META)

    assert meta.clamp is None
    (sent,) = bars_of(plays.client.sent)
    assert sent.bars.start == ASKED
    harness.book.changes = []
    aapl = StoreAssetDatasetPath(asset_type=AssetType.STOCK, data_type=DataType.MARKET_ACTIVITY, asset_symbol='AAPL')
    assert (await _post(harness, plays, body(), path=aapl)).clamp is None
    assert [row.requested_start for row in await harness.bars_requests()] == [ASKED, ASKED]
    assert len(queries_of(plays.client.sent)) == 2, 'every POST queries, clamped or not'


@pytest.mark.asyncio
async def test_a_record_first_stored_by_an_earlier_request_still_clamps(harness, monkeypatch):
    """A record the query's own answer does not carry still clamps.

    The dedupe stored the observation under an earlier request, so this POST's answer carries nothing new; the
    stored record clamps all the same, because the read is never limited to this POST's request.
    """
    harness.book.changes = [FB_TO_META]
    plays = _plays(harness, monkeypatch, actions=(lambda request: actions_answer(request.request_id),))

    stored = await _post(harness, plays, body())

    assert stored.clamp is not None and stored.clamp.clamped_start == CLAMPED


@pytest.mark.parametrize(
    ('start', 'clamped'),
    [
        pytest.param(CLAMPED - timedelta(microseconds=1), True, id='just-before-the-clamp-is-moved'),
        pytest.param(CLAMPED, False, id='at-the-clamp-is-not'),
        pytest.param(datetime(2023, 1, 3, tzinfo=UTC), False, id='after-the-clamp-is-not'),
    ],
)
@pytest.mark.asyncio
async def test_the_clamp_applies_only_when_after_the_bodys_start(harness, monkeypatch, start, clamped):
    harness.book.changes = [FB_TO_META]
    plays = _plays(harness, monkeypatch)

    stored = await _post(harness, plays, body(start=start))

    (row,) = await harness.bars_requests()
    assert row.requested_start == (CLAMPED if clamped else start)
    assert (stored.clamp is not None) is clamped, f'clamp reported: {stored.clamp}'


@pytest.mark.asyncio
async def test_a_source_with_no_calendar_clamps_to_utc_midnight(harness, monkeypatch):
    def no_calendar(_source):
        raise UnknownCalendarError('no calendar for this source')

    monkeypatch.setattr(data_action_request, 'calendar_for_source', no_calendar)
    harness.book.changes = [FB_TO_META]
    plays = _plays(harness, monkeypatch)

    stored = await _post(harness, plays, body())

    utc_midnight = datetime(2022, 6, 9, tzinfo=UTC)
    assert stored.clamp.clamped_start == utc_midnight
    (row,) = await harness.bars_requests()
    assert row.requested_start == utc_midnight


@pytest.mark.asyncio
async def test_the_clamp_runs_before_the_overlap_check_and_before_the_bars_row(harness, monkeypatch):
    """Order (addendum 19 (1)): the read precedes the overlap check, which precedes the bars request row."""
    harness.book.changes = [FB_TO_META]
    order: list[str] = []
    real_read, real_check = harness.book.__call__, data_action_request._check_overlap

    async def read(*args, **kwargs):
        order.append('read')
        return await real_read(*args, **kwargs)

    async def check(db, path, payload):
        order.append(f'overlap from {payload.start.isoformat()}')
        return await real_check(db, path, payload)

    monkeypatch.setattr(data_action_request, 'name_changes_naming', read)
    monkeypatch.setattr(data_action_request, '_check_overlap', check)
    plays = _plays(harness, monkeypatch)

    await _post(harness, plays, body())

    assert order == ['read', f'overlap from {CLAMPED.isoformat()}'], order


@pytest.mark.asyncio
async def test_an_exact_repeat_clamps_to_the_same_start_and_extends_its_entry_with_no_409(harness, monkeypatch):
    """The overlap key is the CLAMPED start: a repeat of the same body is the same entry, never its own overlap."""
    harness.book.changes = [FB_TO_META]
    plays = _plays(harness, monkeypatch)

    first = await _post(harness, plays, body())
    second = await _post(harness, plays, body())

    assert first.clamp == second.clamp and second.clamp.clamped_start == CLAMPED
    (entry,) = await harness.entries()
    assert entry.start == CLAMPED
    rows = await harness.bars_requests()
    assert [row.requested_start for row in rows] == [CLAMPED, CLAMPED]
    assert {row.dataset_id for row in rows} == {entry.id}
    assert len(await harness.query_requests()) == 2, 'a repeat queries again (accepted cost)'


@pytest.mark.asyncio
async def test_a_later_name_change_turns_a_repeat_into_a_409_own_overlap_as_built(harness, monkeypatch):
    """COST ACCEPTED in addendum 19: the record arrives after the first POST, so the repeat clamps elsewhere."""
    plays = _plays(harness, monkeypatch)
    await _post(harness, plays, body())
    harness.book.changes = [FB_TO_META]

    with pytest.raises(OwnOverlapConflict):
        await _post(harness, plays, body())

    assert [row.requested_start for row in await harness.bars_requests()] == [ASKED], 'the 409 wrote a bars row'
    assert len(bars_of(plays.client.sent)) == 1, 'the 409 sent a bars request'
    assert len(await harness.query_requests()) == 2


@pytest.mark.asyncio
async def test_an_overlap_of_the_clamped_window_is_still_refused_with_nothing_past_the_query(harness, monkeypatch):
    await harness.entry(
        owner=OWNER, asset_symbol='FB', start=CLAMPED + timedelta(days=30), end=CLAMPED + timedelta(days=60)
    )
    harness.book.changes = [FB_TO_META]
    plays = _plays(harness, monkeypatch)

    with pytest.raises(OwnOverlapConflict):
        await _post(harness, plays, body())

    assert bars_of(plays.client.sent) == [] and await harness.bars_requests() == []
    (query,) = await harness.query_requests()
    assert query.state is IngestRequestState.EMPTY


@pytest.mark.asyncio
async def test_one_info_line_names_the_symbol_both_starts_and_both_request_ids(harness, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    harness.book.changes = [FB_TO_META]
    plays = _plays(harness, monkeypatch)

    await _post(harness, plays, body())

    (query_row,) = await harness.query_requests()
    (bars_row,) = await harness.bars_requests()
    lines = [record for record in caplog.records if 'clamped' in record.getMessage()]
    assert len(lines) == 1, [record.getMessage() for record in lines]
    (line,) = lines
    message = line.getMessage()
    assert line.levelno == logging.INFO
    for part in ('FB', ASKED.isoformat(), CLAMPED.isoformat(), str(query_row.id), str(bars_row.id), '2022-06-09'):
        assert part in message, f'{part!r} missing from {message!r}'
    assert OWNER not in message


@pytest.mark.asyncio
async def test_no_clamp_logs_no_clamp_line(harness, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    plays = _plays(harness, monkeypatch)

    stored = await _post(harness, plays, body())

    assert stored.clamp is None
    assert [record for record in caplog.records if 'clamped' in record.getMessage()] == []


# =============================================================================================== (4) the whole window


@pytest.mark.parametrize(
    'end',
    [pytest.param(CLAMPED, id='end-at-the-clamp'), pytest.param(CLAMPED - timedelta(days=200), id='end-before-it')],
)
@pytest.mark.asyncio
async def test_a_window_wholly_before_the_clamp_is_invalid_request_with_nothing_written_or_sent(
    harness, monkeypatch, end
):
    harness.book.changes = [FB_TO_META]
    plays = _plays(harness, monkeypatch)

    with pytest.raises(InvalidRequestError) as caught:
        await _post(harness, plays, static(end))

    assert caught.value.reason is Reason.INVALID_REQUEST
    assert 'FB' in str(caught.value.detail) and '2022-06-09' in str(caught.value.detail), caught.value.detail
    assert bars_of(plays.client.sent) == [] and await harness.bars_requests() == []
    assert await harness.entries() == []


@pytest.mark.asyncio
async def test_a_window_ending_one_microsecond_after_the_clamp_is_served_from_it(harness, monkeypatch):
    harness.book.changes = [FB_TO_META]
    plays = _plays(harness, monkeypatch)
    end = CLAMPED + timedelta(microseconds=1)

    await _post(harness, plays, static(end))

    (sent,) = bars_of(plays.client.sent)
    assert (sent.bars.start, sent.bars.end) == (CLAMPED, end)


# =============================================================================================== (5) fail closed


_QUERY_FAILURES = [
    pytest.param(
        lambda request: report(
            request.request_id,
            DeliveryOutcome.FAILED,
            kind=IngestRequestKind.CORPORATE_ACTIONS,
            error_class=DeliveryErrorClass.PERMANENT,
        ),
        Reason.VENDOR_REJECTED,
        id='refused-for-good',
    ),
    pytest.param(
        lambda request: report(
            request.request_id, DeliveryOutcome.FAILED, kind=IngestRequestKind.CORPORATE_ACTIONS, last_attempt=True
        ),
        Reason.VENDOR_UNAVAILABLE,
        id='last-attempt-failed',
    ),
    pytest.param(
        lambda request: report(request.request_id, DeliveryOutcome.EXHAUSTED, kind=IngestRequestKind.CORPORATE_ACTIONS),
        Reason.VENDOR_UNAVAILABLE,
        id='exhausted',
    ),
    pytest.param(
        lambda request: report(request.request_id, DeliveryOutcome.EXPIRED, kind=IngestRequestKind.CORPORATE_ACTIONS),
        Reason.DEADLINE,
        id='expired',
    ),
]


@pytest.mark.parametrize(('answer', 'reason'), _QUERY_FAILURES)
@pytest.mark.asyncio
async def test_a_failed_query_fails_the_post_with_its_error_and_no_bars_row(harness, monkeypatch, answer, reason):
    """FAIL CLOSED (addendum 19 (5)): an unclamped first fetch would store the earlier holder's bars for good."""
    plays = _plays(harness, monkeypatch, actions=(answer,))

    with pytest.raises(ExogenousError) as caught:
        await _post(harness, plays, body())

    assert caught.value.reason is reason
    assert bars_of(plays.client.sent) == [] and await harness.bars_requests() == []
    (query,) = await harness.query_requests()
    assert query.state is IngestRequestState.FAILED


@pytest.mark.asyncio
async def test_a_query_unanswered_by_the_deadline_fails_the_post_and_keeps_running(harness, monkeypatch):
    plays = _plays(harness, monkeypatch, actions=())

    with pytest.raises(ExogenousError) as caught:
        await _post(harness, plays, body(), deadline_s=0.2)

    assert caught.value.reason is Reason.DEADLINE
    assert bars_of(plays.client.sent) == [] and await harness.bars_requests() == []
    (query,) = await harness.query_requests()
    assert query.state is IngestRequestState.QUEUED, 'the query is left running for the watcher and its answer'


@pytest.mark.asyncio
async def test_a_query_whose_submit_fails_fails_the_post_and_stays_pending(harness, monkeypatch):
    plays = _plays(harness, monkeypatch)
    plays.client.raises = ExogenousError(Reason.PEER_UNAVAILABLE, 'ingest is down')

    with pytest.raises(ExogenousError) as caught:
        await _post(harness, plays, body())

    assert caught.value.reason is Reason.PEER_UNAVAILABLE
    assert len(plays.client.sent) == 1 and queries_of(plays.client.sent) == plays.client.sent
    assert await harness.bars_requests() == []
    (query,) = await harness.query_requests()
    assert query.state is IngestRequestState.PENDING


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


@pytest.mark.parametrize(('query_takes', 'bars_gets'), [(7.0, 3.0), (10.0, None), (12.5, None)])
@pytest.mark.asyncio
async def test_one_deadline_covers_both_waits(harness, monkeypatch, query_takes, bars_gets):
    """The bars wait gets what the query's wait left of the one deadline; nothing left fails with no bars row."""
    clock = _Clock()
    monkeypatch.setattr(data_action_request, 'time', types.SimpleNamespace(monotonic=clock.monotonic))
    plays = _plays(harness, monkeypatch)
    real_wait = data_action_request.await_verdict
    given: list[float] = []

    async def timed_wait(request_id, *, deadline_s=None, **kwargs):
        given.append(deadline_s)
        answer = await real_wait(request_id, deadline_s=deadline_s, **kwargs)
        if len(given) == 1:
            clock.now += query_takes
        return answer

    monkeypatch.setattr(data_action_request, 'await_verdict', timed_wait)

    if bars_gets is None:
        with pytest.raises(ExogenousError) as caught:
            await _post(harness, plays, body(), deadline_s=10)
        assert caught.value.reason is Reason.DEADLINE
        assert given == [10] and await harness.bars_requests() == []
    else:
        await _post(harness, plays, body(), deadline_s=10)
        assert given == [10, bars_gets]


# =============================================================================================== the DAILY loop


@pytest.mark.asyncio
async def test_the_daily_loop_never_clamps_and_never_queries(harness, monkeypatch):
    """Acceptance "no re-query on a later refresh": the refresh sends its tail from the entry, as stored.

    The entry FB was created before P51 from 2015, and the store now holds FB's rename away: the DAILY request still
    asks from its tail, reads no name_change and writes no corporate-actions request. A planted clamp here would wait
    for its query; the short deadline makes that a fast failure rather than a five-minute one.
    """
    monkeypatch.setenv(data_action_request.VERDICT_DEADLINE_ENV, '0.2')
    monkeypatch.setattr(daily_refresh, 'own_session', harness.session)
    harness.book.changes = [FB_TO_META]
    dataset_id = await harness.entry(asset_symbol='FB', start=ASKED)
    tail = datetime(2016, 3, 1, tzinfo=UTC)
    planned = _Planned(
        types.SimpleNamespace(id=dataset_id), FB, body(owner='the-entrys-owner', feed=Feed.IEX), tail, None
    )
    client = FakeSubmit()

    await DailyRefresher(client)._refresh(planned, READY_BY)

    (sent,) = client.sent
    assert sent.bars is not None and sent.bars.start == tail
    assert harness.book.calls == [], 'the DAILY loop read name_change records'
    assert await harness.query_requests() == [], 'the DAILY loop wrote a corporate-actions request'
    (row,) = await harness.bars_requests()
    assert (row.trigger, row.client_label, row.dataset_id, row.requested_start) == (
        IngestRunTrigger.SCHEDULE,
        SCHEDULER_ACTOR,
        dataset_id,
        tail,
    )


@pytest.mark.asyncio
async def test_a_refresh_of_a_clamped_entry_never_reaches_before_its_clamped_start(harness, monkeypatch):
    """The clamped start is the entry's declared start, so the refresh's floor is it (the reason for ruling A)."""
    harness.book.changes = [FB_TO_META]
    plays = _plays(harness, monkeypatch)
    await _post(harness, plays, body())
    (entry,) = await harness.entries()

    assert entry.start == CLAMPED
    assert [row.requested_start for row in await harness.bars_requests()] == [CLAMPED]


# =============================================================================================== (6) the route


@pytest.mark.asyncio
async def test_the_route_names_the_clamp_and_its_served_range_starts_at_it(store: PostStore, monkeypatch):
    store.book.changes = [name_change('aapl-away', 'AAPL', 'AAPZ', vendor_date(2022, 6, 9))]
    served = ServedRange(start=CLAMPED, end=CLAMPED + timedelta(days=3))
    client = SubmitDouble(store, Delivery(served=served))

    response = await post(store, client, ROUTE_BODY, monkeypatch)

    assert response.status_code == 200, response.text
    (sent,) = client.submitted
    assert sent.bars.start == CLAMPED, 'the store did not ask for the clamped window'
    payload = response.json()
    assert datetime.fromisoformat(payload['served_range']['start']) == CLAMPED
    message = payload['message']
    for part in ('AAPL', '2022-06-09', ASKED.isoformat(), CLAMPED.isoformat()):
        assert part in message, f'{part!r} missing from the message {message!r}'
    assert message != 'Data stored'
    assert ROUTE_BODY['owner'] not in message


@pytest.mark.asyncio
async def test_the_route_message_is_unchanged_without_a_clamp(store: PostStore, monkeypatch):
    client = SubmitDouble(store)

    response = await post(store, client, ROUTE_BODY, monkeypatch)

    assert response.status_code == 200, response.text
    assert response.json()['message'] == 'Data stored'
    assert client.submitted[0].bars.start == ASKED


@pytest.mark.asyncio
async def test_the_route_renders_a_whole_window_before_the_clamp_as_invalid_request(store: PostStore, monkeypatch):
    store.book.changes = [name_change('aapl-away', 'AAPL', 'AAPZ', vendor_date(2022, 6, 9))]
    client = SubmitDouble(store)
    window = ROUTE_BODY | {'end': '2020-01-01T00:00:00Z'}

    response = await post(store, client, window, monkeypatch)

    body_ = problem(response, status=REASONS[Reason.INVALID_REQUEST].http_status, reason='INVALID_REQUEST')
    assert 'AAPL' in body_['detail'] and '2022-06-09' in body_['detail']
    assert client.submitted == [] and await store.bars_requests() == []
    assert len(client.queries) == 1


@pytest.mark.parametrize(
    ('answer', 'raises', 'status', 'reason'),
    [
        pytest.param(
            QueryAnswer(DeliveryOutcome.FAILED, error_class=DeliveryErrorClass.PERMANENT),
            None,
            422,
            'VENDOR_REJECTED',
            id='query-refused-for-good',
        ),
        pytest.param(QueryAnswer(DeliveryOutcome.EXPIRED), None, 504, 'DEADLINE', id='query-expired'),
        pytest.param(
            QueryAnswer(),
            ExogenousError(Reason.PEER_UNAVAILABLE, 'ingest is down'),
            503,
            'PEER_UNAVAILABLE',
            id='query-call-failed',
        ),
    ],
)
@pytest.mark.asyncio
async def test_the_route_fails_closed_on_the_query(store: PostStore, monkeypatch, answer, raises, status, reason):
    client = SubmitDouble(store, query=answer, query_raises=raises)

    response = await post(store, client, ROUTE_BODY, monkeypatch)

    problem(response, status=status, reason=reason)
    assert client.submitted == [] and await store.bars_requests() == [] and store.created == []
