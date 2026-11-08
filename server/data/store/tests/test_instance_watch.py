"""The instance watcher (P35, tj-7zjov1.28): app/ingest/instance_watch.py, over SQLite.

THE DESIGN: the bead's acceptance (the watcher on run_periodic, kill switch and dry run, re-sends PENDING rows, and on
a new instance id marks the old instance's open requests NOT_DELIVERED and re-submits them flagged recovered with
their attempt count), architect ruling C (the re-submission waits DEFAULT_DELIVERY_DEADLINE_S, 60 s, plus a margin
from the supersession, and reads the attempt count UNDER THE REQUEST LOCK), note F (QUEUED under a superseded
instance is treated exactly like NOT_DELIVERED), ADR tj-26jt9w addenda 5 and 9, and the store CLAUDE.md watcher line
and environment table (INSTANCE_WATCH_ENABLED true, INSTANCE_WATCH_DRY_RUN false, interval 15, PENDING after 30,
margin 15).

The clock is injected (InstanceWatcher(clock=)), so every age is set relative to NOW on the rows themselves. The two
Postgres races (the old instance's late report against the new instance's count, and a request QUEUED under an
instance superseded just before) are tests/system/test_resubmit_races_pg.py's.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity, UpdateType
from common.errors.vocabulary import ExogenousError, Reason
from common.rpc.clients.store_delivery import DEFAULT_DELIVERY_DEADLINE_S
from data.store.app.database.crud import ingest_request as request_crud
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
from data.store.tests.request_path_harness import INSTANCE_A, INSTANCE_B, START, FakeSubmit, Harness, make_engine


pytestmark = [pytest.mark.data_store, pytest.mark.asyncio]

NOW = datetime(2026, 3, 2, 15, tzinfo=UTC)
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
        else {}
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


async def test_a_pending_request_of_another_kind_or_missing_a_field_is_skipped_never_sent(harness):
    await _request(harness, IngestRequestState.PENDING, age_s=60, kind=IngestRequestKind.ASSET_LIST)
    broken = await _request(harness, IngestRequestState.PENDING, age_s=60)
    await harness.set_request(broken, granularity=None)
    client = FakeSubmit(INSTANCE_B)

    result = await _watcher(client).run_pass()

    assert result == WatchResult(skipped=2)
    assert client.sent == []


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


async def test_an_abandoned_request_of_another_kind_is_skipped(harness):
    await _superseded(harness, seconds_ago=90)
    await _request(harness, IngestRequestState.NOT_DELIVERED, holder=INSTANCE_A, kind=IngestRequestKind.ASSET_LIST)
    client = FakeSubmit(INSTANCE_B)

    result = await _watcher(client).run_pass()

    assert result == WatchResult(skipped=1) and client.sent == []


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
