"""The usage recorder's own logic: the buffer, the flush and its wake, the cap, best effort and redaction (tj-grna9p.46).

The design (tj-grna9p.46 body and the architect's notes of 2026-10-13, which win over it):

* record_read BUFFERS in process -- no await, no session, no statement -- so the read path issues no INSERT;
* the flush writes the buffer in batches on its own session, through common/scheduling.run_periodic on an Every
  schedule, is WOKEN EARLY when the buffer reaches the batch size, and runs once more on shutdown;
* best effort: a failed flush is logged and its rows retried; it never raises; the buffer is bounded;
* an UNMIGRATED database: one warning naming make migrate, rows dropped, no log per flush (the run-sweep pattern);
* at most 32 distinct client ids per dataset per UTC day, from the ids already stored; later ids are '_overflow';
  the operator and the reserved values are exempt;
* the client id is never logged and never in a repr.

TIER. The crud functions the recorder calls are replaced by fakes that record what they were handed, so this file
pins the recorder's decisions. That the crud's SQL does what the fakes stand in for -- N rows in one INSERT, the cap
read back from Postgres, the rollup's sums and its atomicity, the unmigrated probe -- is
tests/system/test_usage_record.py's, over a real Postgres.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from common.scheduling import Every
from data.store.app import usage_recorder as module
from data.store.app.client_label import OPERATOR_PRINCIPAL, OVERFLOW, UNATTRIBUTED, ClientLabel
from data.store.app.database.crud.dataset_read import NewRead
from data.store.app.database.models.dataset_read import DatasetRead, DatasetReadKind, ReadClientKind
from data.store.app.database.models.dataset_read_daily import DatasetReadDaily
from data.store.app.usage_recorder import MAX_IDS_PER_DATASET_DAY, UsageRecorder


pytestmark = pytest.mark.data_store

SENTINEL = 'client-sentinel-0b7e'
ROUTE = '/internal/asset-data/{asset_type}/{data_type}'
DATASET = uuid.UUID('00000000-0000-0000-0000-0000000000d1')
OTHER_DATASET = uuid.UUID('00000000-0000-0000-0000-0000000000d2')
NOON = datetime(2026, 3, 2, 12, tzinfo=UTC)


def _sdk(client_id: str = SENTINEL) -> ClientLabel:
    return ClientLabel(client_id, ReadClientKind.SDK)


class FakeStore:
    """Stands in for own_session and the crud the recorder calls, journalling every call."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.inserted: list[list[NewRead]] = []
        self.insert_calls = 0
        self.sessions = 0
        self.tables_exist = True
        self.probes = 0
        self.fail_inserts = 0
        self.known: dict[tuple[uuid.UUID, date], set[str]] = {}
        self.known_calls: list[tuple[set[uuid.UUID], datetime, datetime]] = []
        self.folds: list[datetime] = []
        self.fold_result = 0
        self.closed: list[tuple[uuid.UUID, datetime, int]] = []
        self.insert_gate: asyncio.Event | None = None
        self.insert_started = asyncio.Event()

        @asynccontextmanager
        async def own_session() -> AsyncIterator[object]:
            self.sessions += 1
            yield object()

        async def usage_tables_exist(session) -> bool:
            self.probes += 1
            return self.tables_exist

        async def insert_reads(session, reads) -> int:
            self.insert_calls += 1
            self.insert_started.set()
            if self.insert_gate is not None:
                await self.insert_gate.wait()
            if self.fail_inserts:
                self.fail_inserts -= 1
                raise RuntimeError(f'insert failed for {SENTINEL}')
            batch = list(reads)
            self.inserted.append(batch)
            return len(batch)

        async def known_client_ids(session, dataset_ids, since, until):
            self.known_calls.append((set(dataset_ids), since, until))
            return {key: set(ids) for key, ids in self.known.items()}

        async def fold_reads_before(session, cutoff) -> int:
            self.folds.append(cutoff)
            return self.fold_result

        async def close_session(session, session_id, closed_at, bars_returned) -> None:
            self.closed.append((session_id, closed_at, bars_returned))

        for name, fake in {
            'own_session': own_session,
            'usage_tables_exist': usage_tables_exist,
            'insert_reads': insert_reads,
            'known_client_ids': known_client_ids,
            'fold_reads_before': fold_reads_before,
            'close_session': close_session,
        }.items():
            monkeypatch.setattr(module, name, fake)

    @property
    def rows(self) -> list[NewRead]:
        return [row for batch in self.inserted for row in batch]


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    return FakeStore(monkeypatch)


@pytest.fixture
def all_logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.DEBUG)
    return caplog


def _no_sentinel_logged(caplog: pytest.LogCaptureFixture) -> None:
    assert not any(SENTINEL in record.getMessage() for record in caplog.records)
    assert not any(SENTINEL in (record.exc_text or '') for record in caplog.records)


# ---------------------------------------------------------------------------------------------
# record_read: the read path.


def test_record_read_is_synchronous_and_touches_no_database(store: FakeStore):
    recorder = UsageRecorder(flush_batch_size=10)
    result = recorder.record_read(DATASET, _sdk(), ROUTE, 5, read_at=NOON)

    assert result is None, 'record_read returns nothing to await'
    assert (store.sessions, store.insert_calls, store.probes) == (0, 0, 0)
    assert recorder.buffered == 1


@pytest.mark.asyncio
async def test_n_reads_are_one_batch_of_n_rows_on_one_session(store: FakeStore):
    recorder = UsageRecorder(flush_batch_size=100)
    for bars in range(7):
        recorder.record_read(DATASET, _sdk(), ROUTE, bars, read_at=NOON)

    assert await recorder.flush() == 7

    assert store.insert_calls == 1 and store.sessions == 1
    assert [
        (row.dataset_id, row.client_id, row.client_kind, row.route, row.bars_returned, row.kind) for row in store.rows
    ] == [(DATASET, SENTINEL, ReadClientKind.SDK, ROUTE, bars, DatasetReadKind.READ) for bars in range(7)]
    assert recorder.buffered == 0
    assert await recorder.flush() == 0
    assert store.insert_calls == 1, 'an empty buffer opens nothing'
    assert store.sessions == 1


def test_read_at_defaults_to_now_in_utc(store: FakeStore):
    recorder = UsageRecorder()
    before = datetime.now(UTC)
    recorder.record_read(DATASET, _sdk(), ROUTE, 1)
    (row,) = recorder._buffer
    assert before <= row.read_at <= datetime.now(UTC)
    assert row.read_at.tzinfo is not None


def test_the_wake_is_set_exactly_when_the_buffer_reaches_the_batch_size(store: FakeStore):
    recorder = UsageRecorder(flush_batch_size=3)
    recorder.record_read(DATASET, _sdk(), ROUTE, 1)
    recorder.record_read(DATASET, _sdk(), ROUTE, 1)
    assert not recorder.wake.is_set()
    recorder.record_read(DATASET, _sdk(), ROUTE, 1)
    assert recorder.wake.is_set()


@pytest.mark.asyncio
async def test_the_buffer_is_bounded_dropping_the_oldest_and_saying_how_many(store: FakeStore, all_logs):
    recorder = UsageRecorder(flush_batch_size=2, buffer_max=4)
    for bars in range(7):
        recorder.record_read(DATASET, _sdk(), ROUTE, bars)
    assert recorder.buffered == 4

    assert await recorder.flush() == 4
    assert [row.bars_returned for row in store.rows] == [3, 4, 5, 6]
    assert 'Dropped 3 dataset read(s): the usage buffer was full' in [r.getMessage() for r in all_logs.records]
    _no_sentinel_logged(all_logs)


def test_the_bound_is_never_below_the_batch_size():
    assert UsageRecorder(flush_batch_size=50, buffer_max=10).buffer_max == 50


def test_record_read_never_raises(store: FakeStore, monkeypatch: pytest.MonkeyPatch, all_logs):
    recorder = UsageRecorder()

    def broken(read):
        raise ValueError(SENTINEL)

    monkeypatch.setattr(recorder, '_append', broken)
    recorder.record_read(DATASET, _sdk(), ROUTE, 1)
    assert 'Could not buffer a dataset read: ValueError' in [r.getMessage() for r in all_logs.records]
    _no_sentinel_logged(all_logs)


# ---------------------------------------------------------------------------------------------
# flush: best effort.


@pytest.mark.asyncio
async def test_a_failed_flush_never_raises_and_its_rows_go_first_in_the_next(store: FakeStore, all_logs):
    recorder = UsageRecorder(flush_batch_size=100)
    recorder.record_read(DATASET, _sdk(), ROUTE, 1)
    recorder.record_read(DATASET, _sdk(), ROUTE, 2)
    store.fail_inserts = 1

    assert await recorder.flush() == 0
    assert recorder.buffered == 2
    recorder.record_read(DATASET, _sdk(), ROUTE, 3)
    assert await recorder.flush() == 3
    assert [row.bars_returned for row in store.rows] == [1, 2, 3]

    errors = [r.getMessage() for r in all_logs.records if r.levelno >= logging.ERROR]
    assert errors == ['Could not flush 2 dataset read(s): RuntimeError; will retry']
    _no_sentinel_logged(all_logs)


@pytest.mark.asyncio
async def test_a_flush_cancelled_mid_write_puts_its_rows_back_for_the_final_flush(store: FakeStore):
    recorder = UsageRecorder(flush_batch_size=100)
    recorder.record_read(DATASET, _sdk(), ROUTE, 1)
    store.insert_gate = asyncio.Event()
    flushing = asyncio.create_task(recorder.flush())
    await store.insert_started.wait()
    recorder.record_read(DATASET, _sdk(), ROUTE, 2)
    flushing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await flushing

    assert [row.bars_returned for row in recorder._buffer] == [1, 2], 'the cancelled batch goes back first'
    store.insert_gate = None
    assert await recorder.flush() == 2


# ---------------------------------------------------------------------------------------------
# The unmigrated database.


@pytest.mark.asyncio
async def test_an_unmigrated_database_warns_once_and_drops_the_rows(store: FakeStore, all_logs):
    store.tables_exist = False
    recorder = UsageRecorder()
    for _ in range(3):
        recorder.record_read(DATASET, _sdk(), ROUTE, 1)
        assert await recorder.flush() == 0
    assert await recorder.roll_up(now=NOON) == 0
    assert await recorder.record_session_open(DATASET, _sdk(), '/stream') is None

    assert store.insert_calls == 0 and store.folds == []
    assert recorder.buffered == 0
    lines = [r for r in all_logs.records if r.name == module.log.name]
    assert [(r.levelname, r.getMessage()) for r in lines] == [
        (
            'WARNING',
            f'Usage recording skipped: table {DatasetRead.__tablename__} does not exist; '
            'the database has not been migrated, run make migrate',
        )
    ]

    store.tables_exist = True
    recorder.record_read(DATASET, _sdk(), ROUTE, 1)
    assert await recorder.flush() == 1


@pytest.mark.asyncio
async def test_a_migrated_database_is_probed_once_not_per_flush(store: FakeStore):
    recorder = UsageRecorder()
    for _ in range(3):
        recorder.record_read(DATASET, _sdk(), ROUTE, 1)
        await recorder.flush()
    assert store.probes == 1


# ---------------------------------------------------------------------------------------------
# The per-dataset-per-day cap.


@pytest.mark.asyncio
async def test_the_cap_keeps_32_ids_per_dataset_day_and_overflows_the_rest_in_arrival_order(store: FakeStore):
    recorder = UsageRecorder(flush_batch_size=1000)
    for index in range(MAX_IDS_PER_DATASET_DAY + 2):
        recorder.record_read(DATASET, _sdk(f'c{index:02d}'), ROUTE, 1, read_at=NOON)
    recorder.record_read(DATASET, _sdk('c00'), ROUTE, 1, read_at=NOON)  # known: kept
    recorder.record_read(DATASET, _sdk('c33'), ROUTE, 1, read_at=NOON)  # overflowed before: still overflows
    recorder.record_read(OTHER_DATASET, _sdk('c33'), ROUTE, 1, read_at=NOON)  # another dataset
    recorder.record_read(DATASET, _sdk('c33'), ROUTE, 1, read_at=NOON + timedelta(days=1))  # another day

    await recorder.flush()

    ids = [row.client_id for row in store.rows]
    assert ids[:MAX_IDS_PER_DATASET_DAY] == [f'c{index:02d}' for index in range(MAX_IDS_PER_DATASET_DAY)]
    assert ids[MAX_IDS_PER_DATASET_DAY:] == [OVERFLOW, OVERFLOW, 'c00', OVERFLOW, 'c33', 'c33']
    assert {row.client_kind for row in store.rows if row.client_id == OVERFLOW} == {ReadClientKind.SDK}
    # Everything else about an overflowed read is kept.
    overflowed = store.rows[MAX_IDS_PER_DATASET_DAY]
    assert (overflowed.dataset_id, overflowed.read_at, overflowed.bars_returned) == (DATASET, NOON, 1)


@pytest.mark.asyncio
async def test_the_cap_counts_the_ids_already_stored(store: FakeStore):
    """A restart forgets nothing: the stored ids of the day count toward the cap, the reserved ones do not."""
    day = NOON.date()
    store.known = {
        (DATASET, day): {f's{index:02d}' for index in range(MAX_IDS_PER_DATASET_DAY - 1)}
        | {OPERATOR_PRINCIPAL, UNATTRIBUTED, OVERFLOW}
    }
    recorder = UsageRecorder()
    recorder.record_read(DATASET, _sdk('new-one'), ROUTE, 1, read_at=NOON)
    recorder.record_read(DATASET, _sdk('new-two'), ROUTE, 1, read_at=NOON)
    recorder.record_read(DATASET, _sdk('s00'), ROUTE, 1, read_at=NOON)

    await recorder.flush()

    assert [row.client_id for row in store.rows] == ['new-one', OVERFLOW, 's00']
    ((datasets, since, until),) = store.known_calls
    assert datasets == {DATASET}
    assert (since, until) == (datetime(2026, 3, 2, tzinfo=UTC), datetime(2026, 3, 3, tzinfo=UTC))


@pytest.mark.asyncio
async def test_the_cap_reads_the_whole_utc_span_of_the_batch(store: FakeStore):
    recorder = UsageRecorder()
    toronto_evening = datetime(2026, 3, 4, 22, tzinfo=UTC).astimezone(ZoneInfo('America/Toronto'))
    recorder.record_read(DATASET, _sdk('a'), ROUTE, 1, read_at=NOON)
    recorder.record_read(OTHER_DATASET, _sdk('b'), ROUTE, 1, read_at=toronto_evening)
    await recorder.flush()
    ((datasets, since, until),) = store.known_calls
    assert datasets == {DATASET, OTHER_DATASET}
    assert (since, until) == (datetime(2026, 3, 2, tzinfo=UTC), datetime(2026, 3, 5, tzinfo=UTC))


@pytest.mark.asyncio
async def test_operator_and_reserved_labels_are_exempt_and_never_replaced(store: FakeStore):
    store.known = {(DATASET, NOON.date()): {f's{index:02d}' for index in range(MAX_IDS_PER_DATASET_DAY)}}
    recorder = UsageRecorder()
    for label in (ClientLabel(OPERATOR_PRINCIPAL, ReadClientKind.UI), _sdk(UNATTRIBUTED), _sdk(OVERFLOW)):
        recorder.record_read(DATASET, label, ROUTE, 1, read_at=NOON)
    await recorder.flush()

    assert [(row.client_id, row.client_kind) for row in store.rows] == [
        (OPERATOR_PRINCIPAL, ReadClientKind.UI),
        (UNATTRIBUTED, ReadClientKind.SDK),
        (OVERFLOW, ReadClientKind.SDK),
    ]
    assert store.known_calls == [], 'a batch with nothing capped reads nothing'


# ---------------------------------------------------------------------------------------------
# Stream sessions and the rollup.


@pytest.mark.asyncio
async def test_a_stream_session_is_written_at_once_and_closed_by_its_id(store: FakeStore):
    recorder = UsageRecorder()
    session_id = await recorder.record_session_open(DATASET, _sdk(), '/stream', opened_at=NOON)
    (row,) = store.rows
    assert (row.id, row.kind, row.session_opened_at, row.read_at, row.bars_returned) == (
        session_id,
        DatasetReadKind.STREAM_SESSION,
        NOON,
        NOON,
        0,
    )
    await recorder.record_session_close(session_id, 9, closed_at=NOON + timedelta(minutes=1))
    assert store.closed == [(session_id, NOON + timedelta(minutes=1), 9)]
    await recorder.record_session_close(None, 9)
    assert len(store.closed) == 1, 'a failed open closes nothing'


@pytest.mark.asyncio
async def test_a_failed_session_open_returns_none_and_logs_the_type_only(store: FakeStore, all_logs):
    store.fail_inserts = 1
    assert await UsageRecorder().record_session_open(DATASET, _sdk(), '/stream') is None
    assert 'Could not record a stream session open: RuntimeError' in [r.getMessage() for r in all_logs.records]
    _no_sentinel_logged(all_logs)


@pytest.mark.asyncio
async def test_the_rollup_cutoff_is_the_window_back_from_now(store: FakeStore, monkeypatch: pytest.MonkeyPatch):
    store.fold_result = 4
    recorder = UsageRecorder()
    assert await recorder.roll_up(now=NOON, after_days=10) == 4
    monkeypatch.setenv(module.ROLLUP_AFTER_DAYS_ENV, '30')
    await recorder.roll_up(now=NOON)
    monkeypatch.delenv(module.ROLLUP_AFTER_DAYS_ENV)
    await recorder.roll_up(now=NOON)
    assert store.folds == [NOON - timedelta(days=10), NOON - timedelta(days=30), NOON - timedelta(days=90)]


@pytest.mark.asyncio
async def test_a_failed_rollup_never_raises(store: FakeStore, monkeypatch: pytest.MonkeyPatch, all_logs):
    async def broken(session, cutoff):
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(module, 'fold_reads_before', broken)
    assert await UsageRecorder().roll_up(now=NOON) == 0
    assert 'Could not roll up dataset reads: RuntimeError' in [r.getMessage() for r in all_logs.records]
    _no_sentinel_logged(all_logs)


# ---------------------------------------------------------------------------------------------
# The loops: run_periodic with Every, the wake, and the final flush.


def test_the_loops_use_run_periodic_with_every_and_the_flush_takes_the_wake(monkeypatch: pytest.MonkeyPatch):
    started: list[tuple[str, Any, dict]] = []

    def fake_run_periodic(name, schedule, job, **kwargs):
        started.append((name, schedule, kwargs))
        return name

    monkeypatch.setattr(module, 'run_periodic', fake_run_periodic)
    monkeypatch.setenv(module.FLUSH_INTERVAL_ENV, '7')
    monkeypatch.setenv(module.ROLLUP_INTERVAL_ENV, '120')

    assert module.start_usage_loops() == ['usage-flush', 'usage-rollup']
    (_, flush_schedule, flush_kwargs), (_, rollup_schedule, rollup_kwargs) = started
    assert flush_schedule == Every(timedelta(seconds=7))
    assert flush_kwargs['wake'] is module.usage_recorder.wake
    assert rollup_schedule == Every(timedelta(seconds=120))
    assert 'wake' not in rollup_kwargs


@pytest.mark.parametrize('value', ['0', '-5'])
def test_a_non_positive_interval_falls_back_to_the_default(monkeypatch: pytest.MonkeyPatch, value: str):
    started = []
    monkeypatch.setattr(module, 'run_periodic', lambda name, schedule, job, **kw: started.append(schedule))
    monkeypatch.setenv(module.FLUSH_INTERVAL_ENV, value)
    monkeypatch.delenv(module.ROLLUP_INTERVAL_ENV, raising=False)
    module.start_usage_loops()
    assert started == [
        Every(timedelta(seconds=module.DEFAULT_FLUSH_INTERVAL_SECONDS)),
        Every(timedelta(seconds=module.DEFAULT_ROLLUP_INTERVAL_SECONDS)),
    ]


def test_from_env_reads_the_batch_size_and_the_bound(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(module.FLUSH_BATCH_SIZE_ENV, '11')
    monkeypatch.setenv(module.BUFFER_MAX_ENV, '99')
    recorder = UsageRecorder.from_env()
    assert (recorder.flush_batch_size, recorder.buffer_max) == (11, 99)


@pytest.mark.asyncio
async def test_the_wake_flushes_early_and_stop_flushes_what_is_left(store: FakeStore, monkeypatch: pytest.MonkeyPatch):
    """Through the real run_periodic: an hour's interval, yet a full batch is written at once; stop writes the rest."""
    recorder = UsageRecorder(flush_batch_size=3)
    monkeypatch.setattr(module, 'usage_recorder', recorder)
    monkeypatch.setenv(module.FLUSH_INTERVAL_ENV, '3600')
    monkeypatch.setenv(module.ROLLUP_INTERVAL_ENV, '3600')
    tasks = module.start_usage_loops()
    try:
        # Both loops fire once at start (Every with no first): let them, then the flush sleeps an hour.
        for _ in range(50):
            await asyncio.sleep(0)
        assert len(store.folds) == 1
        written_at_start = len(store.rows)

        for _ in range(3):
            recorder.record_read(DATASET, _sdk(), ROUTE, 1)
        for _ in range(200):
            if len(store.rows) == written_at_start + 3:
                break
            await asyncio.sleep(0.01)
        assert len(store.rows) == written_at_start + 3, 'the batch size woke the flush'

        recorder.record_read(DATASET, _sdk(), ROUTE, 2)
    finally:
        await module.stop_usage_loops(tasks)
    assert all(task.done() for task in tasks)
    assert store.rows[-1].bars_returned == 2, 'the final flush wrote the remainder'
    assert recorder.buffered == 0


# ---------------------------------------------------------------------------------------------
# Redaction of the value objects.


def test_no_repr_carries_the_client_id():
    read = NewRead(
        dataset_id=DATASET,
        client_id=SENTINEL,
        client_kind=ReadClientKind.SDK,
        route=ROUTE,
        bars_returned=1,
        read_at=NOON,
    )
    assert SENTINEL not in repr(read)
    assert SENTINEL not in repr(DatasetRead(dataset_id=DATASET, client_id=SENTINEL, kind=DatasetReadKind.READ))
    assert SENTINEL not in repr(DatasetReadDaily(dataset_id=DATASET, client_id=SENTINEL, day=NOON.date(), reads=1))
