"""Usage on the /ui/v1 catalog: the parts the PR gate can prove without Postgres (validator, gating tj-grna9p.85).

Design: tj-grna9p.85, the architect's 2026-10-14 CONSOLIDATED BUILD BRIEF. What is here:
  * step 0, the UTC guard of the usage window: a naive now is refused before any statement, and the same instant
    in any zone gives the same first day;
  * the client filter's echo rule (ClientFilter) and the membership tests of the usage groups and Expiring Soon,
    at their boundaries;
  * step 1, the recording of a bar read: one per /bars call WITHOUT a cursor, labelled by X-Client-Id, none with a
    cursor and none from any other route, and the read never fails or changes because of the recorder;
  * the zero usage view and the echo on list, detail and facets, and that a label never reaches a log line.

WHAT IS NOT HERE. The usage arithmetic (UTC days, the 30-day window, STALE, NEVER_READ, sort=last_read, the facet
totals, the statement budget with the usage tables read) runs only on Postgres: CAST(timezone(...) AS DATE) is an
integer on SQLite and the to_regclass probe fails there, so every catalog read below sees the zero view. That
arithmetic is pinned in tests/system/test_http_ui_dataset_usage.py.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy.exc import OperationalError

from data.store.app import dataset_catalog as catalog_module
from data.store.app import usage_recorder as recorder_module
from data.store.app.client_label import CLIENT_ID_HEADER, OPERATOR_PRINCIPAL, UNATTRIBUTED, ClientLabel
from data.store.app.database.crud import dataset_read
from data.store.app.database.crud.dataset_read import DatasetUsage
from data.store.app.database.models.dataset_read import ReadClientKind
from data.store.app.dataset_catalog import ClientFilter, UsageFilter
from data.store.app.main import app
from data.store.tests.problem_body import validation_errors

# The route tier's fixtures, reused: pytest finds a fixture by the module attribute's name, so they are imported
# under their own names, and each test taking one carries `noqa: F811` for ruff's view of that parameter.
from data.store.tests.test_ui_datasets_route import (  # noqa: F401
    NOW,
    aware_sqlite_datetimes,
    bars,
    catalog,
    client,
    engine,
)
from routers.data_store import ui_datasets as ui_datasets_module
from routers.data_store.app_endpoints import UiDatasetsInterface


pytestmark = pytest.mark.data_store

SDK, UI = 'CLIENT_KIND_SDK', 'CLIENT_KIND_UI'


# ------------------------------------------------------------------------------------------------- step 0


class _Untouchable:
    """A session that fails the test if a statement is sent: the guard must refuse before any SQL."""

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError('a statement was sent for a naive now')


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('reader', 'kwargs'),
    [
        (dataset_read.read_dataset_usage, {'window_days': 30}),
        (dataset_read.read_daily_reads, {'days': 30}),
        (dataset_read.read_readers, {'window_days': 30}),
        (dataset_read.read_client_datasets, {'window_days': 30}),
    ],
    ids=['read_dataset_usage', 'read_daily_reads', 'read_readers', 'read_client_datasets'],
)
async def test_every_usage_window_refuses_a_naive_now_before_any_statement(reader, kwargs):
    with pytest.raises(ValueError, match='timezone-aware'):
        await reader(_Untouchable(), [uuid.uuid4()], now=datetime(2026, 10, 6, 22, 0), **kwargs)


@pytest.mark.parametrize('zone', ['Pacific/Kiritimati', 'America/New_York', 'Asia/Kolkata', 'UTC'])
def test_the_window_s_first_day_is_the_utc_day_whatever_zone_now_carries(zone):
    """22:00Z on 6 Oct is 7 Oct in +14:00: the window still starts on the UTC day 30 days earlier, 6 Sep."""
    now = NOW.astimezone(ZoneInfo(zone))
    assert dataset_read._window_start_day(now, 30) == datetime(2026, 9, 6).date()
    assert dataset_read._window_start_day(now, 0) == NOW.date()


# ------------------------------------------------------------------------------------------------- the filters


def test_the_client_filter_defaults_to_sdk_and_ids_win_over_kinds():
    assert (ClientFilter().applied_ids, ClientFilter().applied_kinds) == ((), (ReadClientKind.SDK,))
    kinds = ClientFilter(client_kinds=(ReadClientKind.SDK, ReadClientKind.UI, ReadClientKind.SDK))
    assert kinds.applied_kinds == (ReadClientKind.UI, ReadClientKind.SDK), 'once each, in the enum order'
    both = ClientFilter(client_ids=('b', 'a', 'b'), client_kinds=(ReadClientKind.UI,))
    assert (both.applied_ids, both.applied_kinds) == (('b', 'a'), ()), 'ids win; the kinds are not echoed'
    assert 'b' not in repr(both), 'a label reached a repr'


def _row(expiry: datetime | None = None) -> Any:
    """The two fields the membership tests read of a CatalogRow."""
    return SimpleNamespace(id=uuid.uuid4(), expiry=expiry)


def _usage(row: Any, last_read_at: datetime | None, reads: int) -> dict[uuid.UUID, DatasetUsage]:
    return {row.id: DatasetUsage(row.id, last_read_at, reads)}


def _in(group: UsageFilter, last_read_at: datetime | None, *, stale_days: int = 90, reads: int = 0, now=NOW) -> bool:
    """Whether a dataset last read at `last_read_at` (None: never) is in `group`."""
    row = _row()
    usage = {} if last_read_at is None else _usage(row, last_read_at, reads)
    return catalog_module._usage_predicate(group, usage, now, stale_days)(row)


# THE DAY-ALIGNED RULE (tj-grna9p.85 round 2, architect's gate note): whole UTC calendar days, 0 = today. NOW is
# 2026-10-06T22:00Z, so "within 30 days" is a last read dated 2026-09-06 or later, whatever its time of day.
FIRST_DAY_IN_30 = datetime(2026, 9, 6, tzinfo=UTC)


def test_the_reproducer_a_read_at_the_first_moment_of_day_30_is_read_30d_and_not_stale():
    """Round 1's finding (a): this read was both READ_30D and STALE at stale_days 30. It is now only READ_30D."""
    assert _in(UsageFilter.READ_30D, FIRST_DAY_IN_30)
    assert not _in(UsageFilter.STALE, FIRST_DAY_IN_30, stale_days=30)
    one_second_earlier = FIRST_DAY_IN_30 - timedelta(seconds=1)
    assert not _in(UsageFilter.READ_30D, one_second_earlier)
    assert _in(UsageFilter.STALE, one_second_earlier, stale_days=30)
    # The last moment of the same UTC day as the boundary is the same day: still in.
    assert _in(UsageFilter.READ_30D, FIRST_DAY_IN_30 + timedelta(hours=23, minutes=59, seconds=59))


def _last_reads() -> list[datetime | None]:
    """Never, and a last read at three times of day on every UTC day from today back 40 days."""
    today = datetime(2026, 10, 6, tzinfo=UTC)
    moments: list[datetime | None] = [None]
    for back in range(41):
        for at in (timedelta(0), timedelta(hours=12), timedelta(hours=23, minutes=59, seconds=59)):
            moment = today - timedelta(days=back) + at
            if moment <= NOW:
                moments.append(moment)
    return moments


def test_read_30d_and_stale_at_30_days_are_exact_opposites_for_every_last_read():
    for last_read_at in _last_reads():
        assert _in(UsageFilter.READ_30D, last_read_at) is not _in(UsageFilter.STALE, last_read_at, stale_days=30), (
            last_read_at
        )


@pytest.mark.parametrize(
    ('stale_days', 'both', 'neither'),
    [
        (29, {datetime(2026, 9, 6).date()}, set()),  # one day early: day 30 is read in 30 days AND stale at 29
        (30, set(), set()),
        (31, set(), {datetime(2026, 9, 5).date()}),  # one day late: day 31 is neither read in 30 nor stale at 31
    ],
)
def test_the_stale_threshold_moves_by_whole_utc_days(stale_days, both, neither):
    """READ_30D is fixed at 30, so at 29 and 31 the two groups are opposites on every day but exactly one."""
    seen_both, seen_neither = set(), set()
    for last_read_at in _last_reads():
        read, stale = (
            _in(UsageFilter.READ_30D, last_read_at),
            _in(UsageFilter.STALE, last_read_at, stale_days=stale_days),
        )
        if read and stale:
            seen_both.add(last_read_at.date())
        if not read and not stale:
            seen_neither.add(last_read_at.date())
    assert (seen_both, seen_neither) == (both, neither)


def test_the_day_is_today_s_utc_date_whatever_zone_now_carries():
    kiritimati = NOW.astimezone(ZoneInfo('Pacific/Kiritimati'))  # already 7 October there
    assert _in(UsageFilter.READ_30D, FIRST_DAY_IN_30, now=kiritimati)
    assert not _in(UsageFilter.STALE, FIRST_DAY_IN_30, stale_days=30, now=kiritimati)


def test_stale_includes_the_never_read_and_never_read_is_no_last_read():
    assert _in(UsageFilter.STALE, None) and _in(UsageFilter.NEVER_READ, None)
    row = _row()
    no_last_read = _usage(row, None, 0)
    assert catalog_module._usage_predicate(UsageFilter.NEVER_READ, no_last_read, NOW, 90)(row)
    assert catalog_module._usage_predicate(UsageFilter.STALE, no_last_read, NOW, 90)(row)
    assert not catalog_module._usage_predicate(UsageFilter.READ_30D, no_last_read, NOW, 90)(row)
    assert not _in(UsageFilter.STALE, NOW - timedelta(days=89)) and _in(UsageFilter.STALE, NOW - timedelta(days=91))
    assert not _in(UsageFilter.STALE, NOW - timedelta(days=1), stale_days=1), 'yesterday is within 1 day'
    assert _in(UsageFilter.STALE, NOW - timedelta(days=2), stale_days=1)


def test_a_stream_session_only_read_counts_as_read_though_reads_30d_is_zero():
    """last_read_at includes stream sessions; reads_in_window does not. The groups follow last_read_at."""
    this_morning = datetime(2026, 10, 6, 8, tzinfo=UTC)
    assert _in(UsageFilter.READ_30D, this_morning, reads=0)
    assert not _in(UsageFilter.NEVER_READ, this_morning, reads=0)
    assert not _in(UsageFilter.STALE, this_morning, reads=0, stale_days=1)


def test_expiring_is_strictly_before_the_bound_or_pending():
    bound = NOW + timedelta(days=7)
    pending_row, at_bound, before, forever = (
        _row(NOW + timedelta(days=99)),
        _row(bound),
        _row(bound - timedelta(microseconds=1)),
        _row(),
    )
    test = catalog_module._expiring_predicate(bound, frozenset({pending_row.id}))
    assert [test(row) for row in (pending_row, at_bound, before, forever)] == [True, False, True, False]
    assert catalog_module.EXPIRING_SOON_DAYS == 7 and catalog_module.DEFAULT_STALE_DAYS == 90


# ------------------------------------------------------------------------------------------------- step 1


class _Recorder:
    def __init__(self) -> None:
        self.reads: list[tuple[uuid.UUID, ClientLabel, str, int]] = []

    def record_read(self, dataset_id: uuid.UUID, label: ClientLabel, route: str, bars_returned: int, **_: Any) -> None:
        self.reads.append((dataset_id, label, route, bars_returned))


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    fake = _Recorder()
    monkeypatch.setattr(ui_datasets_module, 'usage_recorder', fake)
    return fake


@pytest.mark.asyncio
async def test_one_read_per_bars_call_without_a_cursor_and_none_with_one(client, bars, recorder):  # noqa: F811
    url = f'/ui/v1/datasets/{bars.dataset.id}/bars'
    first = (await client.get(url, params={'limit': 3}, headers={CLIENT_ID_HEADER: OPERATOR_PRINCIPAL})).json()
    cursor = first['nextCursor']
    for _ in range(3):
        page = await client.get(
            url, params={'limit': 3, 'cursor': cursor}, headers={CLIENT_ID_HEADER: OPERATOR_PRINCIPAL}
        )
        assert page.status_code == 200
        cursor = page.json().get('nextCursor') or cursor
    assert (await client.get(url)).status_code == 200
    assert (await client.get(url, headers={CLIENT_ID_HEADER: 'robot-7'})).status_code == 200
    assert (await client.get(url, headers={CLIENT_ID_HEADER: '_forged'})).status_code == 200

    route = UiDatasetsInterface.GET_UI_DATASET_BARS.value
    assert recorder.reads == [
        (bars.dataset.id, ClientLabel(OPERATOR_PRINCIPAL, ReadClientKind.UI), route, 3),
        (bars.dataset.id, ClientLabel(UNATTRIBUTED, ReadClientKind.SDK), route, 10),
        (bars.dataset.id, ClientLabel('robot-7', ReadClientKind.SDK), route, 10),
        (bars.dataset.id, ClientLabel(UNATTRIBUTED, ReadClientKind.SDK), route, 10),
    ]


@pytest.mark.asyncio
async def test_no_other_route_records_and_a_404_or_422_records_nothing(client, bars, catalog, recorder):  # noqa: F811
    dataset = bars.dataset.id
    for path, params in (
        ('/ui/v1/datasets', {}),
        ('/ui/v1/datasets/facets', {}),
        (f'/ui/v1/datasets/{dataset}', {}),
        (f'/ui/v1/datasets/{dataset}/bar-pages', {}),
        (f'/ui/v1/datasets/{dataset}/runs', {}),
        (f'/ui/v1/datasets/{catalog.ddd.id}/coverage', {}),
        ('/ui/v1/reaper/pending', {}),
        (f'/ui/v1/datasets/{uuid.uuid4()}/bars', {}),
        (f'/ui/v1/datasets/{dataset}/bars', {'limit': 0}),
        (f'/ui/v1/datasets/{dataset}/bars', {'cursor': 'not-a-cursor'}),
    ):
        response = await client.get(path, params=params, headers={CLIENT_ID_HEADER: OPERATOR_PRINCIPAL})
        assert response.status_code < 500, (path, response.text)
    assert recorder.reads == []


@pytest.mark.asyncio
async def test_the_store_get_owner_filter_is_never_recorded_as_the_reader(client, catalog, monkeypatch):  # noqa: F811
    """OPTION R: the /store GET `owner` query is a search filter; that GET answers entries and records no read."""
    recorded: list[Any] = []
    monkeypatch.setattr(recorder_module.UsageRecorder, 'record_read', lambda self, *a, **k: recorded.append(a))
    monkeypatch.setattr(recorder_module.UsageRecorder, '_append', lambda self, *a, **k: recorded.append(a))
    for headers in ({}, {CLIENT_ID_HEADER: 'robot-7'}):
        response = await client.get(
            '/store/stock/market-activity/AAA', params={'owner': catalog.aaa_day.owner}, headers=headers
        )
        assert response.status_code == 200 and response.json(), response.text
    assert recorded == []


@pytest.mark.asyncio
async def test_an_empty_window_is_still_one_read_of_zero_bars(client, bars, recorder):  # noqa: F811
    url = f'/ui/v1/datasets/{bars.dataset.id}/bars'
    response = await client.get(url, params={'start': '2026-10-05T14:03:00Z', 'end': '2026-10-05T14:03:00Z'})
    assert (response.status_code, response.json()) == (200, {})
    assert [read[3] for read in recorder.reads] == [0]


@pytest.mark.asyncio
async def test_a_recorder_that_fails_never_fails_or_changes_the_read(client, bars, monkeypatch):  # noqa: F811
    """The real recorder catches its own failure (record_read never raises); the page is the same either way."""
    url = f'/ui/v1/datasets/{bars.dataset.id}/bars'
    before = (await client.get(url, params={'limit': 4})).json()
    from data.store.app import usage_recorder as recorder_module

    real = recorder_module.UsageRecorder(flush_batch_size=1000)

    def broken(read: Any) -> None:
        raise RuntimeError('buffer is broken')

    monkeypatch.setattr(real, '_append', broken)
    monkeypatch.setattr(ui_datasets_module, 'usage_recorder', real)
    response = await client.get(url, params={'limit': 4})
    assert response.status_code == 200 and response.json() == before


# ------------------------------------------------------------------------------------------------- the zero view


@pytest.mark.asyncio
async def test_usage_is_always_set_with_the_echo_on_list_detail_and_facets(client, catalog):  # noqa: F811
    items = (await client.get('/ui/v1/datasets', params={'limit': 500})).json()['items']
    assert {str(item['usage']) for item in items} == {str({'clientKinds': [SDK]})}
    detail = (await client.get(f'/ui/v1/datasets/{catalog.aaa_day.id}')).json()
    assert detail['usage'] == {'clientKinds': [SDK]}
    # Round 2 (finding b): the detail takes the client filter, with the list's parsing, precedence and echo.
    detail_url = f'/ui/v1/datasets/{catalog.aaa_day.id}'
    ui = (await client.get(detail_url, params={'client_kind': 'ui'})).json()
    assert ui['usage'] == {'clientKinds': [UI]}
    both = (await client.get(detail_url, params=[('client_kind', 'SDK'), ('client_kind', 'CLIENT_KIND_UI')])).json()
    assert both['usage'] == {'clientKinds': [UI, SDK]}
    wins = (await client.get(detail_url, params=[('client_kind', 'UI'), ('client_id', 'b'), ('client_id', 'b')])).json()
    assert wins['usage'] == {'clientIds': ['b']}, 'client_id wins and is echoed once; the kinds are not echoed'

    kinds = (
        await client.get('/ui/v1/datasets', params=[('client_kind', 'ui'), ('client_kind', 'CLIENT_KIND_SDK')])
    ).json()
    assert {tuple(item['usage']['clientKinds']) for item in kinds['items']} == {(UI, SDK)}
    ids = (
        await client.get('/ui/v1/datasets', params=[('client_id', 'b'), ('client_kind', 'UI'), ('client_id', 'a')])
    ).json()
    assert {str(item['usage']) for item in ids['items']} == {str({'clientIds': ['b', 'a']})}

    facets = (await client.get('/ui/v1/datasets/facets', params=[('client_id', 'b'), ('stale_days', 30)])).json()
    assert (facets['clientIds'], facets.get('clientKinds'), facets['staleDays']) == (['b'], None, 30)
    assert facets['expiresBefore'] == '2026-10-13T22:00:00Z', 'now + EXPIRING_SOON_DAYS'
    default = (await client.get('/ui/v1/datasets/facets')).json()
    assert (default['clientKinds'], default['staleDays']) == ([SDK], 90)


@pytest.mark.asyncio
async def test_a_label_never_reaches_a_log_line_or_an_error_body(client, catalog, bars, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    label = 'secret-client-label-0042'
    await client.get(f'/ui/v1/datasets/{bars.dataset.id}/bars', headers={CLIENT_ID_HEADER: label})
    await client.get('/ui/v1/datasets', params={'client_id': label, 'usage': 'STALE'})
    await client.get('/ui/v1/datasets/facets', params={'client_id': label})
    refused = await client.get('/ui/v1/datasets', params={'client_id': label + 'x' * 128})
    assert refused.status_code == 422 and label not in refused.text, 'the refused label is echoed back'
    ours = [r.getMessage() for r in caplog.records if r.name.startswith(('data.', 'common.', 'routers.'))]
    assert all(label not in line for line in ours), [line for line in ours if label in line]


# ------------------------------------------------------------------------------------------------- round 2: detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('params', 'field'),
    [
        ({'owner': 'operator'}, 'owner'),
        ({'usage': 'STALE'}, 'usage'),  # the list's filters are not the detail's
        ({'stale_days': 30}, 'stale_days'),
        ({'client_kind': 'BOT'}, 'client_kind'),
        ({'client_id': ''}, 'client_id'),
    ],
)
async def test_the_detail_refuses_an_unknown_or_bad_parameter_with_a_422_naming_it(client, catalog, params, field):  # noqa: F811
    response = await client.get(f'/ui/v1/datasets/{catalog.aaa_day.id}', params=params)
    assert response.status_code == 422, response.text
    assert any(field in error['loc'] for error in validation_errors(response))


# ------------------------------------------------------------------------------------------------- round 2: probe


@pytest.mark.asyncio
async def test_a_failing_probe_surfaces_its_real_error_without_a_migrate_warning(
    client,  # noqa: F811
    catalog,  # noqa: F811
    monkeypatch,
    caplog,
):
    """Round 2 (finding d): no catch. The real type reaches the error handler; nothing says 'make migrate'."""

    async def failing_probe(_db: Any) -> bool:
        raise OperationalError('SELECT to_regclass(...)', {}, ConnectionError('the server closed the connection'))

    monkeypatch.setattr(catalog_module, 'usage_tables_exist', failing_probe)
    monkeypatch.setattr(catalog_module, '_warned_usage_missing', False)
    caplog.set_level(logging.DEBUG)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url='http://store') as http:
        for path in ('/ui/v1/datasets', '/ui/v1/datasets/facets', f'/ui/v1/datasets/{catalog.aaa_day.id}'):
            assert (await http.get(path)).status_code == 500, path
    ours = [r.getMessage() for r in caplog.records if r.name.startswith(('data.', 'common.', 'routers.'))]
    assert sum('Unhandled OperationalError' in line for line in ours) == 3, ours
    assert not [line for line in ours if 'make migrate' in line], 'a failing probe was reported as unmigrated'
    assert catalog_module._warned_usage_missing is False, 'a failing probe used up the warn-once'


@pytest.mark.asyncio
async def test_an_unmigrated_database_still_reads_zeros_with_exactly_one_warning(client, catalog, monkeypatch, caplog):  # noqa: F811
    """The SQLite route tier has no usage tables: the conftest's to_regclass answers NULL, as Postgres does."""
    monkeypatch.setattr(catalog_module, '_warned_usage_missing', False)
    caplog.set_level(logging.DEBUG, logger=catalog_module.log.name)
    for path in (
        '/ui/v1/datasets',
        '/ui/v1/datasets/facets',
        f'/ui/v1/datasets/{catalog.aaa_day.id}',
        '/ui/v1/datasets',
    ):
        assert (await client.get(path)).status_code == 200, path
    detail = (await client.get(f'/ui/v1/datasets/{catalog.aaa_day.id}')).json()
    assert detail['usage'] == {'clientKinds': [SDK]}
    facets = (await client.get('/ui/v1/datasets/facets')).json()
    assert facets['stale'] == facets['neverRead'] == facets['all'] and 'read30d' not in facets
    warnings = [r for r in caplog.records if r.name == catalog_module.log.name and r.levelno >= logging.WARNING]
    assert len(warnings) == 1 and 'make migrate' in warnings[0].getMessage(), [r.getMessage() for r in warnings]
    assert catalog_module._warned_usage_missing is True
