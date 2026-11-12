"""The instance watcher (P35, tj-7zjov1.28): app/ingest/instance_watch.py, over SQLite.

THE DESIGN: the bead's acceptance (the watcher on run_periodic, kill switch and dry run, re-sends PENDING rows, and on
a new instance id marks the old instance's open requests NOT_DELIVERED and re-submits them flagged recovered with
their attempt count), architect ruling C (the re-submission waits DEFAULT_DELIVERY_DEADLINE_S, 60 s, plus a margin
from the supersession, and reads the attempt count UNDER THE REQUEST LOCK), note F (QUEUED under a superseded
instance is treated exactly like NOT_DELIVERED), ADR tj-26jt9w addenda 5 and 9, and the store CLAUDE.md watcher line
and environment table (INSTANCE_WATCH_ENABLED true, INSTANCE_WATCH_DRY_RUN false, interval 15, PENDING after 30,
margin 15). Since P17f (tj-7zjov1.164, ADR tj-26jt9w addendum 18 g) ASSET_LIST rows are re-sent and re-submitted
exactly as BARS rows are. Since P51 (tj-7zjov1.146, ADR tj-26jt9w addendum 19 (7)) CORPORATE_ACTIONS rows are too: a
POST's name_change query is re-sent and re-submitted as stored, and a re-send never clamps and never queries.

The clock is injected (InstanceWatcher(clock=)), so every age is set relative to NOW on the rows themselves. The two
Postgres races (the old instance's late report against the new instance's count, and a request QUEUED under an
instance superseded just before) are tests/system/test_resubmit_races_pg.py's.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity, UpdateType
from common.errors.vocabulary import ExogenousError, Reason
from common.rpc.clients.store_delivery import DEFAULT_DELIVERY_DEADLINE_S
from data.store.app.database.crud import ingest_request as request_crud
from data.store.app.database.models.asset_list import AssetListEvent, AssetListRow, AssetListSnapshot
from data.store.app.database.models.ingest_request import IngestRequestKind, IngestRequestState
from data.store.app.ingest import instance_watch
from data.store.app.ingest.instance_watch import (
    DRY_RUN_ENV,
    ENABLED_ENV,
    INTERVAL_ENV,
    PENDING_RESEND_AFTER_ENV,
    RESUBMIT_MARGIN_ENV,
    InstanceWatcher,
    WatchResult,
    interval_seconds,
    is_dry_run,
    is_enabled,
    pending_resend_after,
    resubmit_delay,
    start_instance_watch_loop,
    stop_instance_watch_loop,
)
from data.store.tests.name_change_doubles import actions_answer, name_change, vendor_date
from data.store.tests.request_path_harness import INSTANCE_A, INSTANCE_B, START, FakeSubmit, Harness, make_engine
from schemas.data_ingest.delivery import (
    AssetRecord,
    AssetRecords,
    AttemptDelivery,
    DeliveryHeader,
    DeliveryOutcome,
    DeliveryTrailer,
    DeliveryVerdict,
    VendorCallStats,
)
from schemas.data_ingest.submit import AssetListStatus


pytestmark = [pytest.mark.data_store, pytest.mark.asyncio]

NOW = datetime(2026, 3, 2, 15, tzinfo=UTC)
# A name_change query's window as the POST writes it: the calendar's first day to the POST's now.
QUERY_START = datetime(1990, 1, 1, tzinfo=UTC)
QUERY_END = NOW - timedelta(minutes=2)
WATCH_ENVS = (ENABLED_ENV, DRY_RUN_ENV, INTERVAL_ENV, PENDING_RESEND_AFTER_ENV, RESUBMIT_MARGIN_ENV)


@pytest.fixture(autouse=True)
def unset_watch_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in WATCH_ENVS:
        monkeypatch.delenv(name, raising=False)


@pytest_asyncio.fixture
async def harness(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Harness]:
    engine = await make_engine(monkeypatch)
    try:
        yield Harness(engine, monkeypatch)
    finally:
        await engine.dispose()


async def _request(
    harness: Harness,
    state: IngestRequestState,
    *,
    holder: str | None = None,
    age_s: float = 3600,
    kind: IngestRequestKind = IngestRequestKind.BARS,
    **values,
) -> uuid.UUID:
    bars = (
        {
            'asset_symbol': 'VFV',
            'asset_type': AssetType.STOCK,
            'data_types': (DataType.MARKET_ACTIVITY.value,),
            'granularity': Granularity.ONE_DAY,
            'update_type': UpdateType.DAILY,
            'feed': Feed.IEX,
            'requested_start': START,
        }
        if kind is IngestRequestKind.BARS
        # An asset-list row names its list (the table's CHECK since tj-7zjov1.30), as new_asset_list_request writes it.
        else {'asset_type': AssetType.STOCK, 'asset_list_status': AssetListStatus.INACTIVE}
        if kind is IngestRequestKind.ASSET_LIST
        # A corporate-actions row is a POST's name_change query (P51), as new_corporate_actions_request writes it.
        else {'asset_symbol': 'FB', 'requested_start': QUERY_START, 'requested_end': QUERY_END}
    )
    async with AsyncSession(harness.engine, expire_on_commit=False) as db:
        record = await request_crud.create_request(
            db, request_crud.NewIngestRequest(kind=kind, source=DataSource.ALPACA_API, client_label='owner', **bars)
        )
    await harness.set_request(
        record.id, state=state, instance_id=holder, created_at=NOW - timedelta(seconds=age_s), **values
    )
    return record.id


async def _superseded(harness: Harness, *, seconds_ago: float) -> None:
    """A held the requests; B, the live instance, was first seen `seconds_ago` before NOW."""
    await harness.register(INSTANCE_A)
    await harness.register(INSTANCE_B)
    await harness.set_instance(INSTANCE_A, superseded_at=NOW - timedelta(seconds=seconds_ago))


def _watcher(client: FakeSubmit) -> InstanceWatcher:
    return InstanceWatcher(client, clock=lambda: NOW)


# --------------------------------------------------------------------------------------------- the settings


async def test_the_watcher_is_on_unless_explicitly_false(monkeypatch):
    assert is_enabled() is True
    for value in ('false', '0', 'no', 'off', ' OFF ', 'False'):
        monkeypatch.setenv(ENABLED_ENV, value)
        assert is_enabled() is False, value
    for value in ('true', '1', 'yes', 'anything'):
        monkeypatch.setenv(ENABLED_ENV, value)
        assert is_enabled() is True, value


async def test_dry_run_is_off_unless_explicitly_true(monkeypatch):
    assert is_dry_run() is False
    for value in ('true', '1', 'yes', 'on', ' TRUE '):
        monkeypatch.setenv(DRY_RUN_ENV, value)
        assert is_dry_run() is True, value
    for value in ('false', '0', 'maybe'):
        monkeypatch.setenv(DRY_RUN_ENV, value)
        assert is_dry_run() is False, value


async def test_the_intervals_default_as_documented(monkeypatch):
    assert interval_seconds() == 15
    assert pending_resend_after() == timedelta(seconds=30)
    assert DEFAULT_DELIVERY_DEADLINE_S == 60
    assert resubmit_delay() == timedelta(seconds=75), 'the delivery deadline plus the 15 s margin (ruling C)'
    monkeypatch.setenv(INTERVAL_ENV, '0')
    monkeypatch.setenv(PENDING_RESEND_AFTER_ENV, '-1')
    monkeypatch.setenv(RESUBMIT_MARGIN_ENV, '-3')
    assert (interval_seconds(), pending_resend_after(), resubmit_delay()) == (
        15,
        timedelta(seconds=30),
        timedelta(seconds=75),
    )
    monkeypatch.setenv(RESUBMIT_MARGIN_ENV, '0')
    assert resubmit_delay() == timedelta(seconds=60), 'the margin is added to, never replaces, the delivery deadline'


async def test_the_loop_does_not_start_when_the_kill_switch_is_off(monkeypatch):
    monkeypatch.setenv(ENABLED_ENV, 'false')
    assert start_instance_watch_loop(FakeSubmit()) is None
    await stop_instance_watch_loop(None)


async def test_the_loop_starts_on_run_periodic_and_stops(monkeypatch):
    started: list = []

    def fake_run_periodic(name, schedule, job, *, log):
        started.append((name, schedule))
        return 'task'

    monkeypatch.setattr(instance_watch, 'run_periodic', fake_run_periodic)
    monkeypatch.setenv(INTERVAL_ENV, '9')

    assert start_instance_watch_loop(FakeSubmit()) == 'task'
    ((name, schedule),) = started
    assert name == 'instance-watch' and schedule.interval == timedelta(seconds=9)


# --------------------------------------------------------------------------------------------- the probe


async def test_each_pass_probes_ingest_and_notes_the_answering_instance(harness):
    client = FakeSubmit(INSTANCE_B)

    await _watcher(client).run_pass()

    assert client.probes == 1
    assert await harness.instance(INSTANCE_B) is not None


async def test_a_failed_probe_is_logged_and_the_pass_carries_on(harness):
    request_id = await _request(harness, IngestRequestState.PENDING, age_s=31)
    client = FakeSubmit(INSTANCE_B)
    client.probe_raises = ExogenousError(Reason.PEER_UNAVAILABLE, 'ingest is down')

    result = await _watcher(client).run_pass()

    assert result.resent == 1
    assert (await harness.request_row(request_id)).state is IngestRequestState.QUEUED


async def test_a_probe_that_finds_a_new_instance_marks_the_old_ones_requests_not_delivered(harness):
    await harness.register(INSTANCE_A)
    held = await _request(harness, IngestRequestState.QUEUED, holder=INSTANCE_A)

    await _watcher(FakeSubmit(INSTANCE_B)).run_pass()

    row = await harness.request_row(held)
    assert row.state is IngestRequestState.NOT_DELIVERED, 'noticed, and not re-submitted before the delay'


# --------------------------------------------------------------------------------------------- PENDING


async def test_a_pending_request_older_than_30_seconds_is_re_sent_as_it_is(harness):
    old = await _request(harness, IngestRequestState.PENDING, age_s=31, attempts=0)
    young = await _request(harness, IngestRequestState.PENDING, age_s=29)
    client = FakeSubmit(INSTANCE_B)

    result = await _watcher(client).run_pass()

    assert result == WatchResult(resent=1)
    (sent,) = client.sent
    assert (sent.request_id, sent.recovered, sent.attempt_count) == (str(old), False, 0)
    assert (await harness.request_row(old)).state is IngestRequestState.QUEUED
    assert (await harness.request_row(young)).state is IngestRequestState.PENDING, 'its own caller is still submitting'


async def test_a_pending_re_send_that_fails_leaves_the_request_pending_for_the_next_pass(harness):
    request_id = await _request(harness, IngestRequestState.PENDING, age_s=60)
    client = FakeSubmit(INSTANCE_B)
    client.raises = ExogenousError(Reason.PEER_UNAVAILABLE, 'ingest is down')

    result = await _watcher(client).run_pass()

    assert result == WatchResult(failed=1)
    assert (await harness.request_row(request_id)).state is IngestRequestState.PENDING


async def test_a_pending_request_missing_a_field_is_skipped_never_sent(harness):
    """A bars row missing its granularity and a corporate-actions row missing its symbol: skipped, never sent.

    Since P51 a corporate-actions row IS sent (the tests below); one that cannot be built is skipped like any other.
    """
    actions = await _request(harness, IngestRequestState.PENDING, age_s=60, kind=IngestRequestKind.CORPORATE_ACTIONS)
    await harness.set_request(actions, asset_symbol=None)
    broken = await _request(harness, IngestRequestState.PENDING, age_s=60)
    await harness.set_request(broken, granularity=None)
    client = FakeSubmit(INSTANCE_B)

    result = await _watcher(client).run_pass()

    assert result == WatchResult(skipped=2)
    assert client.sent == []


@pytest.mark.parametrize('status', list(AssetListStatus), ids=lambda status: status.value)
async def test_a_pending_asset_list_request_is_re_sent_with_its_own_list(harness, status):
    """P17f (tj-7zjov1.164, ADR tj-26jt9w addendum 18 g): an ASSET_LIST row PENDING past 30 s is re-sent like a bars one.

    Not recovered, with its count, carrying the request row's own list; QUEUED under the answering instance.
    """
    request_id = await _request(
        harness,
        IngestRequestState.PENDING,
        age_s=31,
        kind=IngestRequestKind.ASSET_LIST,
        asset_list_status=status,
        attempts=0,
    )
    young = await _request(harness, IngestRequestState.PENDING, age_s=29, kind=IngestRequestKind.ASSET_LIST)
    client = FakeSubmit(INSTANCE_B)

    result = await _watcher(client).run_pass()

    assert result == WatchResult(resent=1)
    (sent,) = client.sent
    assert (sent.request_id, sent.recovered, sent.attempt_count) == (str(request_id), False, 0)
    assert sent.asset_list.status is status
    row = await harness.request_row(request_id)
    assert (row.state, row.instance_id) == (IngestRequestState.QUEUED, INSTANCE_B)
    assert (await harness.request_row(young)).state is IngestRequestState.PENDING, 'its own caller is still submitting'


async def test_one_pending_pass_sends_bars_asset_lists_and_corporate_actions(harness, monkeypatch):
    """All three kinds are sent (P51, ADR tj-26jt9w addendum 19 (7): a POST's name_change query is re-sent too).

    Before P51 a CORPORATE_ACTIONS row was skipped before the sender; now it is offered, sent as stored (its window
    unchanged, not recovered) and QUEUED under the answering instance, so a stuck query is delivered and stored raw
    even after its POST failed closed.
    """
    bars = await _request(harness, IngestRequestState.PENDING, age_s=60)
    asset_list = await _request(harness, IngestRequestState.PENDING, age_s=60, kind=IngestRequestKind.ASSET_LIST)
    actions = await _request(harness, IngestRequestState.PENDING, age_s=60, kind=IngestRequestKind.CORPORATE_ACTIONS)
    offered: list[IngestRequestKind] = []
    real_submit = instance_watch.submit_record

    async def spy(record, client, **kwargs):
        offered.append(record.kind)
        return await real_submit(record, client, **kwargs)

    monkeypatch.setattr(instance_watch, 'submit_record', spy)
    client = FakeSubmit(INSTANCE_B)

    result = await _watcher(client).run_pass()

    assert result == WatchResult(resent=3)
    assert sorted(offered) == sorted(
        [IngestRequestKind.BARS, IngestRequestKind.ASSET_LIST, IngestRequestKind.CORPORATE_ACTIONS]
    )
    assert {sent.request_id for sent in client.sent} == {str(bars), str(asset_list), str(actions)}
    (query,) = [sent for sent in client.sent if sent.request_id == str(actions)]
    assert (query.corporate_actions.asset_symbol, query.corporate_actions.start, query.corporate_actions.end) == (
        'FB',
        QUERY_START,
        QUERY_END,
    ), 'the query was not re-sent as stored'
    assert (query.recovered, query.attempt_count) == (False, 0)
    row = await harness.request_row(actions)
    assert (row.state, row.instance_id) == (IngestRequestState.QUEUED, INSTANCE_B)


async def test_a_watcher_re_send_never_clamps_and_never_queries(harness):
    """Addendum 19 (1): every re-send sends the row as stored. The clamp ran once, at the POST, into requested_start.

    A bars row for a symbol the store holds a name_change for (FB renamed away on 2022-06-09) is re-sent with its
    stored start: the watcher reads no name_change and writes no corporate-actions request.
    """
    harness.book.changes = [name_change('fb-meta', 'FB', 'META', vendor_date(2022, 6, 9))]
    early = datetime(2015, 1, 2, tzinfo=UTC)
    request_id = await _request(harness, IngestRequestState.PENDING, age_s=60, asset_symbol='FB', requested_start=early)
    client = FakeSubmit(INSTANCE_B)

    result = await _watcher(client).run_pass()

    assert result == WatchResult(resent=1)
    (sent,) = client.sent
    assert (sent.request_id, sent.bars.asset_symbol, sent.bars.start) == (str(request_id), 'FB', early)
    assert harness.book.calls == [], 'the watcher read name_change records'
    assert await harness.query_requests() == [], 'the watcher wrote a corporate-actions request'


@pytest.mark.parametrize('stuck', ['pending', 'abandoned'])
async def test_a_stuck_query_once_sent_again_and_delivered_is_finished(harness, stuck):
    """The point of re-sending the query (addendum 19 (7)): its answer is delivered and stored, the row closes.

    A query left PENDING (its POST's Submit failed) or abandoned on a superseded instance is sent again; ingest's
    report then finishes it through the intake like any request. Before P51 such a row stayed open for good.
    """
    if stuck == 'pending':
        request_id = await _request(
            harness, IngestRequestState.PENDING, age_s=60, kind=IngestRequestKind.CORPORATE_ACTIONS
        )
    else:
        await _superseded(harness, seconds_ago=90)
        request_id = await _request(
            harness, IngestRequestState.NOT_DELIVERED, holder=INSTANCE_A, kind=IngestRequestKind.CORPORATE_ACTIONS
        )
    client = FakeSubmit(INSTANCE_B)

    await _watcher(client).run_pass()
    (sent,) = client.sent
    assert sent.recovered is (stuck == 'abandoned')
    attempt_no = 1
    answer = actions_answer(sent.request_id, instance_id=INSTANCE_B, attempt_no=attempt_no)
    response = await harness.deliver(answer)

    assert response.verdict is DeliveryVerdict.FINAL
    row = await harness.request_row(request_id)
    assert row.state is IngestRequestState.EMPTY, 'the re-sent query, answered with no record, did not finish'
    (run,) = await harness.runs(request_id)
    assert run.asset_symbol == 'FB'


async def test_a_pending_asset_list_re_send_that_fails_leaves_it_pending_for_the_next_pass(harness):
    request_id = await _request(harness, IngestRequestState.PENDING, age_s=60, kind=IngestRequestKind.ASSET_LIST)
    client = FakeSubmit(INSTANCE_B)
    client.raises = ExogenousError(Reason.PEER_UNAVAILABLE, 'ingest is down')

    result = await _watcher(client).run_pass()

    assert result == WatchResult(failed=1)
    assert (await harness.request_row(request_id)).state is IngestRequestState.PENDING


# --------------------------------------------------------------------------------------------- ABANDONED (ruling C,
# note F)


@pytest.mark.parametrize('state', [IngestRequestState.NOT_DELIVERED, IngestRequestState.QUEUED])
async def test_c_f_an_abandoned_request_is_re_submitted_recovered_with_its_durable_count_after_the_delay(
    harness, state
):
    """An abandoned request is re-submitted recovered, with its count, after the delay.

    NOT_DELIVERED, and QUEUED under a superseded instance (note F: the same case), once the old instance was
    superseded at least 60 s + 15 s ago: re-submitted to the live instance, recovered, with the durable count, and
    QUEUED under the live instance.
    """
    await _superseded(harness, seconds_ago=76)
    request_id = await _request(harness, state, holder=INSTANCE_A, attempts=2)
    client = FakeSubmit(INSTANCE_B)

    result = await _watcher(client).run_pass()

    assert result == WatchResult(resubmitted=1)
    (sent,) = client.sent
    assert (sent.request_id, sent.recovered, sent.attempt_count) == (str(request_id), True, 2)
    row = await harness.request_row(request_id)
    assert (row.state, row.instance_id, row.attempts) == (IngestRequestState.QUEUED, INSTANCE_B, 2)


@pytest.mark.parametrize('state', [IngestRequestState.NOT_DELIVERED, IngestRequestState.QUEUED])
@pytest.mark.parametrize('seconds_ago', [74, 60, 1])
async def test_c_an_abandoned_request_is_not_re_submitted_before_the_delay(harness, state, seconds_ago):
    """Inside the delivery deadline plus the margin, the old instance's last report may still land: wait."""
    await _superseded(harness, seconds_ago=seconds_ago)
    request_id = await _request(harness, state, holder=INSTANCE_A, attempts=1)
    client = FakeSubmit(INSTANCE_B)

    result = await _watcher(client).run_pass()

    assert client.sent == [] and result == WatchResult()
    assert (await harness.request_row(request_id)).state is state


async def test_c_the_delay_follows_the_margin_setting(harness, monkeypatch):
    monkeypatch.setenv(RESUBMIT_MARGIN_ENV, '100')
    await _superseded(harness, seconds_ago=150)
    await _request(harness, IngestRequestState.NOT_DELIVERED, holder=INSTANCE_A)
    client = FakeSubmit(INSTANCE_B)

    await _watcher(client).run_pass()

    assert client.sent == [], '150 s is inside 60 s + 100 s'


async def test_c_the_count_sent_is_the_one_read_under_the_lock_not_the_one_listed(harness, monkeypatch):
    """The count sent is the one read under the request lock.

    The old instance's late report lands between the listing and the claim: the re-submission carries the count the
    claim read under the request lock, so the new instance never reuses that attempt number.
    """
    await _superseded(harness, seconds_ago=90)
    request_id = await _request(harness, IngestRequestState.NOT_DELIVERED, holder=INSTANCE_A, attempts=1)
    real_claim = instance_watch.claim_abandoned

    async def late_report_then_claim(db, claimed_id, cutoff):
        await harness.set_request(claimed_id, attempts=2)
        return await real_claim(db, claimed_id, cutoff)

    monkeypatch.setattr(instance_watch, 'claim_abandoned', late_report_then_claim)
    client = FakeSubmit(INSTANCE_B)

    await _watcher(client).run_pass()

    (sent,) = client.sent
    assert (sent.request_id, sent.attempt_count) == (str(request_id), 2)


async def test_a_request_finished_between_the_listing_and_the_claim_is_not_re_submitted(harness, monkeypatch):
    await _superseded(harness, seconds_ago=90)
    request_id = await _request(harness, IngestRequestState.NOT_DELIVERED, holder=INSTANCE_A)
    real_claim = instance_watch.claim_abandoned

    async def finished_then_claim(db, claimed_id, cutoff):
        await harness.set_request(claimed_id, state=IngestRequestState.SUCCEEDED)
        return await real_claim(db, claimed_id, cutoff)

    monkeypatch.setattr(instance_watch, 'claim_abandoned', finished_then_claim)
    client = FakeSubmit(INSTANCE_B)

    result = await _watcher(client).run_pass()

    assert client.sent == [] and result == WatchResult()
    assert (await harness.request_row(request_id)).state is IngestRequestState.SUCCEEDED


async def test_a_request_queued_under_the_live_instance_is_left_alone(harness):
    await _superseded(harness, seconds_ago=500)
    request_id = await _request(harness, IngestRequestState.QUEUED, holder=INSTANCE_B)
    client = FakeSubmit(INSTANCE_B)

    await _watcher(client).run_pass()

    assert client.sent == []
    assert (await harness.request_row(request_id)).state is IngestRequestState.QUEUED


async def test_a_re_submission_that_fails_leaves_the_request_not_delivered_for_the_next_pass(harness):
    await _superseded(harness, seconds_ago=90)
    request_id = await _request(harness, IngestRequestState.QUEUED, holder=INSTANCE_A)
    client = FakeSubmit(INSTANCE_B)
    client.raises = ExogenousError(Reason.PEER_UNAVAILABLE, 'ingest is down')

    result = await _watcher(client).run_pass()

    assert result == WatchResult(failed=1)
    assert (await harness.request_row(request_id)).state is IngestRequestState.NOT_DELIVERED


@pytest.mark.parametrize('state', [IngestRequestState.NOT_DELIVERED, IngestRequestState.QUEUED])
async def test_an_abandoned_corporate_actions_request_is_re_submitted_recovered_as_stored(harness, state):
    """Addendum 19 (7): an abandoned name_change query is re-submitted RECOVERED with its count and its own window."""
    await _superseded(harness, seconds_ago=90)
    request_id = await _request(harness, state, holder=INSTANCE_A, kind=IngestRequestKind.CORPORATE_ACTIONS, attempts=2)
    client = FakeSubmit(INSTANCE_B)

    result = await _watcher(client).run_pass()

    assert result == WatchResult(resubmitted=1)
    (sent,) = client.sent
    assert (sent.request_id, sent.recovered, sent.attempt_count) == (str(request_id), True, 2)
    assert (sent.corporate_actions.asset_symbol, sent.corporate_actions.start, sent.corporate_actions.end) == (
        'FB',
        QUERY_START,
        QUERY_END,
    )
    row = await harness.request_row(request_id)
    assert (row.state, row.instance_id, row.attempts) == (IngestRequestState.QUEUED, INSTANCE_B, 2)


async def test_an_abandoned_corporate_actions_request_missing_a_field_is_skipped(harness):
    await _superseded(harness, seconds_ago=90)
    request_id = await _request(
        harness, IngestRequestState.NOT_DELIVERED, holder=INSTANCE_A, kind=IngestRequestKind.CORPORATE_ACTIONS
    )
    await harness.set_request(request_id, requested_end=None)
    client = FakeSubmit(INSTANCE_B)

    result = await _watcher(client).run_pass()

    assert result == WatchResult(skipped=1) and client.sent == []


@pytest.mark.parametrize('state', [IngestRequestState.NOT_DELIVERED, IngestRequestState.QUEUED])
async def test_an_abandoned_asset_list_request_is_re_submitted_recovered_with_its_list(harness, state):
    """P17f (3): an abandoned asset list is re-submitted RECOVERED with its durable count and its own list.

    NOT_DELIVERED, or QUEUED under a superseded instance, past 60 s + 15 s; then QUEUED under the live instance with
    the count unchanged.
    """
    await _superseded(harness, seconds_ago=76)
    request_id = await _request(harness, state, holder=INSTANCE_A, kind=IngestRequestKind.ASSET_LIST, attempts=3)
    client = FakeSubmit(INSTANCE_B)

    result = await _watcher(client).run_pass()

    assert result == WatchResult(resubmitted=1)
    (sent,) = client.sent
    assert (sent.request_id, sent.recovered, sent.attempt_count) == (str(request_id), True, 3)
    assert sent.asset_list.status is AssetListStatus.INACTIVE
    row = await harness.request_row(request_id)
    assert (row.state, row.instance_id, row.attempts) == (IngestRequestState.QUEUED, INSTANCE_B, 3)


@pytest.mark.parametrize('state', [IngestRequestState.NOT_DELIVERED, IngestRequestState.QUEUED])
async def test_an_abandoned_asset_list_request_is_not_re_submitted_before_the_delay(harness, state):
    await _superseded(harness, seconds_ago=74)
    request_id = await _request(harness, state, holder=INSTANCE_A, kind=IngestRequestKind.ASSET_LIST, attempts=1)
    client = FakeSubmit(INSTANCE_B)

    result = await _watcher(client).run_pass()

    assert client.sent == [] and result == WatchResult()
    assert (await harness.request_row(request_id)).state is state


async def test_one_abandoned_pass_re_submits_bars_asset_lists_and_corporate_actions(harness, monkeypatch):
    await _superseded(harness, seconds_ago=90)
    bars = await _request(harness, IngestRequestState.NOT_DELIVERED, holder=INSTANCE_A, attempts=1)
    asset_list = await _request(
        harness, IngestRequestState.NOT_DELIVERED, holder=INSTANCE_A, kind=IngestRequestKind.ASSET_LIST, attempts=2
    )
    actions = await _request(
        harness,
        IngestRequestState.NOT_DELIVERED,
        holder=INSTANCE_A,
        kind=IngestRequestKind.CORPORATE_ACTIONS,
        attempts=3,
    )
    offered: list[tuple[IngestRequestKind, bool]] = []
    real_submit = instance_watch.submit_record

    async def spy(record, client, **kwargs):
        offered.append((record.kind, kwargs.get('recovered', False)))
        return await real_submit(record, client, **kwargs)

    monkeypatch.setattr(instance_watch, 'submit_record', spy)
    client = FakeSubmit(INSTANCE_B)

    result = await _watcher(client).run_pass()

    assert result == WatchResult(resubmitted=3)
    assert sorted(offered) == sorted(
        [
            (IngestRequestKind.BARS, True),
            (IngestRequestKind.ASSET_LIST, True),
            (IngestRequestKind.CORPORATE_ACTIONS, True),
        ]
    )
    assert {(sent.request_id, sent.recovered, sent.attempt_count) for sent in client.sent} == {
        (str(bars), True, 1),
        (str(asset_list), True, 2),
        (str(actions), True, 3),
    }


async def _with_asset_list_tables(harness: Harness) -> None:
    """The request-path engine lacks the asset-list tables; the models' own are added so the intake can file one."""
    metadata = sa.MetaData()
    for table in (AssetListSnapshot.__table__, AssetListRow.__table__, AssetListEvent.__table__):
        table.to_metadata(metadata)
    async with harness.engine.begin() as connection:
        await connection.run_sync(metadata.create_all)


async def _open(harness: Harness, status: AssetListStatus) -> bool:
    async with AsyncSession(harness.engine) as db:
        return await request_crud.open_asset_list_request_exists(db, DataSource.ALPACA_API, status)


def _asset_list_success(request_id: uuid.UUID, *, instance_id: str, attempt_no: int) -> AttemptDelivery:
    """A SUCCEEDED asset-list report as the runner builds it: one record, no feed, no served range."""
    record = AssetRecord(vendor_asset_id='w-1', symbol='WWW', raw_payload=b'{"id":"w-1"}', active=False)
    return AttemptDelivery(
        header=DeliveryHeader(
            request_id=str(request_id), instance_id=instance_id, attempt_no=attempt_no, quota_key='quota'
        ),
        parts=[AssetRecords(records=[record])],
        trailer=DeliveryTrailer(
            outcome=DeliveryOutcome.SUCCEEDED,
            vendor_stats=VendorCallStats(
                calls=1, pages=1, throttled_429=0, retries=0, first_call_at=NOW, last_call_at=NOW
            ),
            as_of=NOW,
        ),
    )


@pytest.mark.parametrize('stuck', ['pending', 'abandoned'])
async def test_once_sent_again_and_delivered_the_list_is_no_longer_open(harness, stuck):
    """The P47 hazard of addendum 18 g: a stuck row kept open_asset_list_request_exists True for its list for good.

    Re-sent (PENDING) or re-submitted recovered (abandoned), the request stays open while ingest works on it, and the
    next attempt's delivery through the real intake finishes it: the list is open no longer, the other list never was.
    """
    await _with_asset_list_tables(harness)
    if stuck == 'pending':
        request_id = await _request(harness, IngestRequestState.PENDING, age_s=60, kind=IngestRequestKind.ASSET_LIST)
    else:
        await _superseded(harness, seconds_ago=90)
        request_id = await _request(
            harness, IngestRequestState.NOT_DELIVERED, holder=INSTANCE_A, kind=IngestRequestKind.ASSET_LIST, attempts=1
        )
    assert await _open(harness, AssetListStatus.INACTIVE) is True
    client = FakeSubmit(INSTANCE_B)

    await _watcher(client).run_pass()

    (sent,) = client.sent
    assert await _open(harness, AssetListStatus.INACTIVE) is True, 'QUEUED under the live instance: in flight'
    answer = await harness.deliver(
        _asset_list_success(request_id, instance_id=INSTANCE_B, attempt_no=sent.attempt_count + 1)
    )

    assert answer.verdict is DeliveryVerdict.FINAL
    assert (await harness.request_row(request_id)).state is IngestRequestState.SUCCEEDED
    assert await _open(harness, AssetListStatus.INACTIVE) is False
    assert await _open(harness, AssetListStatus.ACTIVE) is False


# --------------------------------------------------------------------------------------------- dry run


async def test_a_dry_run_probes_nothing_sends_nothing_and_changes_nothing(harness, monkeypatch):
    monkeypatch.setenv(DRY_RUN_ENV, 'true')
    await _superseded(harness, seconds_ago=90)
    pending = await _request(harness, IngestRequestState.PENDING, age_s=60)
    queued = await _request(harness, IngestRequestState.QUEUED, holder=INSTANCE_A)
    before = [tuple(row) for row in await harness.requests()]
    client = FakeSubmit(INSTANCE_B)

    result = await _watcher(client).run_pass()

    assert result == WatchResult(skipped=2), 'counts what it would have re-sent'
    assert client.sent == [] and client.probes == 0
    assert [tuple(row) for row in await harness.requests()] == before
    assert {pending, queued} == {row.id for row in await harness.requests()}
