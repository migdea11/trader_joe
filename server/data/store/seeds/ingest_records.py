"""The seed's ingest records: ingest_instance, ingest_request and ingest_run rows, written by the store's REAL code.

Bead tj-7zjov1.100 (P33d). The seed held only store_dataset_entry and stock_market_activity, so the migration
tests said nothing about the three tables of the request record across an upgrade or a downgrade. This module
fills them the way production does, not with raw SQL, so the seed proves the code and the schema agree:

  * ingest_instance: note_instance_seen (the instance marker's own statement) through DeliveryIntake.register_instance;
  * ingest_request: create_request (PENDING, as the submit path leaves it) and mark_queued;
  * ingest_run attempt rows, the request's count, spend and state: DeliveryIntake.deliver, the whole delivery
    transaction, bars written through the real bar writer.

(The scenario's own POSTs also write request and attempt rows since the push path, with random ids; the producer
empties the ingest tables and corporate_action AFTER the POSTs and before this cast, so the seed holds the cast alone.)

THE CAST, every id a literal so two runs agree. Instance A is announced first and is superseded when B arrives.
  PENDING        written, never answered                     no attempt
  QUEUED         answered by the live instance B              no attempt
  NOT_DELIVERED  queued by A, A reported a transient failure,
                 then B arrived and superseded A              one attempt row (instance A)
  SUCCEEDED      a transient failure, then a success with
                 two bars (the fake's own bars for two days
                 of ZZSEEDAA, so the bar table is unchanged)   two attempt rows, spend summed
  EMPTY          a success that held no bar                   one attempt row, no bars
  FAILED         PERMANENT refusal with vendor stats          one attempt row
  FAILED         PERMANENT refusal with NO stats              one attempt row, vendor columns NULL
  FAILED         a transient failure, then the last attempt   two attempt rows, ATTEMPTS_EXHAUSTED
  FAILED         EXPIRED                                      no attempt row
  SUCCEEDED      CORPORATE_ACTIONS (ZZSEEDCA), two records:
                 action A and action B                        one attempt row, NULL labels but the symbol
  SUCCEEDED      CORPORATE_ACTIONS over an overlapping window: A
                 again with the SAME bytes (the dedupe, writes
                 nothing) and A with OTHER bytes (a vendor
                 revision, a new row)                         one attempt row, NULL labels but the symbol
  SUCCEEDED x4   ASSET_LIST (tj-7zjov1.30), through new_asset_list_request: active 1 (a1 a2 a3), inactive 1 (i1, which
                 shares a1's symbol: the s1 shape), active 2 (a1 a4), inactive 2 (i1, a2 listed under another symbol, i2).
                 Four snapshots holding nine asset rows; events: appeared a4 and i2, vanished a2 and a3, status_flip
                 a2. The first snapshot of a list derives nothing.
  EMPTY          ASSET_LIST, active, a served empty list (no part): a snapshot of zero rows, no event.
                 Each asset-list request has one attempt row with every label column NULL.

THE LEGACY RUNS (bead tj-7zjov1.31). A database from before the push path holds ingest_run rows with no request id and no
attempt number, and the upgrade tests must keep exercising that shape, so four are written through the legacy recorder's
own crud (start_run, finish_run, fail_interrupted_runs), one symbol each (ZZSEEDLA..LD), in the states worth keeping:
  RUNNING swept to FAILED / INTERRUPTED   (ZZSEEDLA)
  SUCCEEDED with two bars                 (ZZSEEDLB)
  EMPTY                                   (ZZSEEDLC)
  FAILED with a typed reason              (ZZSEEDLD, VENDOR_UNAVAILABLE)
Their symbols differ, which keeps dump.NORMALISE_SQL's rank (request_id NULLS FIRST, then asset_symbol) stable and apart
from the attempt rows' ranks; their instants are fixed by the same statement (request_id IS NULL).

Three corporate_action rows result (A, B, A revised); the same-bytes repeat of A is the dedupe, so it leaves no row.
The dataset_id of a request that names none stays NULL. Every counted attempt carries the vendor asset id except the
FAILED one with no stats, whose header names none, so the seed holds both values of ingest_run.vendor_asset_id.

NOTHING HERE READS A CLOCK. Every instant is a literal; the instants Postgres stamps with now() (created_at,
finished_at, last_seen_at and the like) and the random attempt-row ids are rewritten by dump.NORMALISE_SQL.

The app modules are imported inside the function: they read the database environment at import, and this
package must stay importable without it.
"""

import uuid
from collections import Counter
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine


INSTANCE_A = 'seed-instance-a'
INSTANCE_B = 'seed-instance-b'
INSTANCES = (INSTANCE_A, INSTANCE_B)
QUOTA_KEY = 'seed-quota-key'
VENDOR_ASSET = 'seed-vendor-asset'
CLIENT_LABEL = 'seed-owner-a'
# A synthetic symbol that passes scenario.SYMBOL_PATTERN, as the requests' asset_symbol must.
CORPORATE_ACTION_SYMBOL = 'ZZSEEDCA'

# The request ids: literal, so the seed names them identically on every run.
REQUEST_IDS = {
    name: uuid.UUID(f'5eed0000-0000-4000-8000-{index:012d}')
    for index, name in enumerate(
        (
            'PENDING',
            'QUEUED',
            'NOT_DELIVERED',
            'SUCCEEDED',
            'EMPTY',
            'FAILED_PERMANENT',
            'FAILED_NO_STATS',
            'FAILED_EXHAUSTED',
            'FAILED_EXPIRED',
            'CA_FIRST',
            'CA_REVISED',
            'AL_ACTIVE_1',
            'AL_INACTIVE_1',
            'AL_ACTIVE_2',
            'AL_INACTIVE_2',
            'AL_ACTIVE_EMPTY',
        ),
        start=1,
    )
}

# What the seeded rows must come to; the producer refuses a seed that does not match.
# Each cast request's own final (state, instance id), by request name. The instance is the one the row names: B for every
# request that was answered by the live instance (finished ones keep it), A for the one A queued and B then superseded,
# none for the one never answered. The producer judges every request by its id against this table, not by counts.
_EXPECTED_REQUEST_BY_NAME: dict[str, tuple[str, str | None]] = {
    'PENDING': ('PENDING', None),
    'QUEUED': ('QUEUED', INSTANCE_B),
    'NOT_DELIVERED': ('NOT_DELIVERED', INSTANCE_A),
    'SUCCEEDED': ('SUCCEEDED', INSTANCE_B),
    'EMPTY': ('EMPTY', INSTANCE_B),
    'FAILED_PERMANENT': ('FAILED', INSTANCE_B),
    'FAILED_NO_STATS': ('FAILED', INSTANCE_B),
    'FAILED_EXHAUSTED': ('FAILED', INSTANCE_B),
    'FAILED_EXPIRED': ('FAILED', INSTANCE_B),
    'CA_FIRST': ('SUCCEEDED', INSTANCE_B),
    'CA_REVISED': ('SUCCEEDED', INSTANCE_B),
    'AL_ACTIVE_1': ('SUCCEEDED', INSTANCE_B),
    'AL_INACTIVE_1': ('SUCCEEDED', INSTANCE_B),
    'AL_ACTIVE_2': ('SUCCEEDED', INSTANCE_B),
    'AL_INACTIVE_2': ('SUCCEEDED', INSTANCE_B),
    'AL_ACTIVE_EMPTY': ('EMPTY', INSTANCE_B),
}
if set(_EXPECTED_REQUEST_BY_NAME) != set(REQUEST_IDS):
    raise RuntimeError('the expected request table and REQUEST_IDS name different requests')
# Keyed by the request id as Postgres renders it (id::text), the form the producer reads back.
EXPECTED_REQUESTS: dict[str, tuple[str, str | None]] = {
    str(REQUEST_IDS[name]): expected for name, expected in _EXPECTED_REQUEST_BY_NAME.items()
}
EXPECTED_REQUEST_STATES = dict(Counter(state for state, _ in EXPECTED_REQUESTS.values()))
# Whether each cast instance ends superseded: A by B's arrival, B (the live one) not.
EXPECTED_INSTANCE_SUPERSEDED = {INSTANCE_A: True, INSTANCE_B: False}
EXPECTED_ATTEMPT_ROWS = 15
# The legacy-shaped runs (no request id, no attempt number), per state; one of the FAILED is the swept INTERRUPTED one.
EXPECTED_LEGACY_STATES = {'EMPTY': 1, 'FAILED': 2, 'SUCCEEDED': 1}
EXPECTED_LEGACY_RUNS = sum(EXPECTED_LEGACY_STATES.values())
EXPECTED_INTERRUPTED_RUNS = 1
EXPECTED_RUN_ROWS = EXPECTED_ATTEMPT_ROWS + EXPECTED_LEGACY_RUNS
# Observations: action A, action B and A's revision. EXPECTED_REVISED_ACTIONS: vendor ids held under more than one payload.
EXPECTED_OBSERVATIONS = 3
EXPECTED_REVISED_ACTIONS = 1
EXPECTED_INSTANCES = len(INSTANCES)
# The asset-list cast (tj-7zjov1.30): requests and snapshots per list (active: two non-empty and one empty; inactive: two),
# the empty snapshots among them, the asset rows, and the events derived per kind. Counts only; the producer names found
# and expected when one differs.
EXPECTED_ASSET_LIST_REQUESTS = {'active': 3, 'inactive': 2}
EXPECTED_SNAPSHOTS = {'active': 3, 'inactive': 2}
EXPECTED_EMPTY_SNAPSHOTS = 1
EXPECTED_SNAPSHOT_ROWS = 9
EXPECTED_EVENTS = {'appeared': 2, 'status_flip': 1, 'vanished': 2}

_WINDOW_START = datetime(2024, 1, 1, tzinfo=UTC)
_WINDOW_END = datetime(2024, 2, 1, tzinfo=UTC)
_FIRST_CALL = datetime(2024, 4, 15, 14, 30, tzinfo=UTC)
_AS_OF = _FIRST_CALL + timedelta(seconds=4)
_STARTED = datetime(2024, 4, 15, 9, 0, tzinfo=UTC)
_READY_BY = datetime(2024, 4, 15, 12, 0, tzinfo=UTC)
# Two days the scenario's ZZSEEDAA dataset already holds, on the daily grid.
_BAR_DAYS = (datetime(2024, 1, 2, tzinfo=UTC), datetime(2024, 1, 3, tzinfo=UTC))
# The asset-list cast. Vendor ids and symbols are synthetic (ZZSEEDX*). AL_A1 and AL_I1 are the s1 shape: one symbol held by
# an active id and by an inactive id. AL_A2 leaves the active list for the inactive one, where it is listed under another
# symbol (one status_flip); AL_A3 is in neither list afterwards (purged); AL_A4 and AL_I2 are new to their lists.
_AL_DAY_0 = datetime(2024, 5, 1, 14, 0, tzinfo=UTC)
_AL_ASSETS = {
    'AL_A1': ('seed-asset-a1', 'ZZSEEDXA'),
    'AL_A2': ('seed-asset-a2', 'ZZSEEDXB'),
    'AL_A3': ('seed-asset-a3', 'ZZSEEDXC'),
    'AL_A4': ('seed-asset-a4', 'ZZSEEDXD'),
    'AL_I1': ('seed-asset-i1', 'ZZSEEDXA'),
    'AL_A2_RENAMED': ('seed-asset-a2', 'ZZSEEDXE'),
    'AL_I2': ('seed-asset-i2', 'ZZSEEDXF'),
}
# One symbol per legacy run, in the scenario's synthetic pattern: interrupted, succeeded, empty, failed.
LEGACY_SYMBOLS = ('ZZSEEDLA', 'ZZSEEDLB', 'ZZSEEDLC', 'ZZSEEDLD')
# The raw bytes of the synthetic vendor records: A in two versions (the revision), and B.
_ACTION_A_V1 = b'{"id":"seed-action-a","type":"forward_split","symbol":"ZZSEEDCA","ratio":2,"ex":"2024-01-10"}'
_ACTION_A_V2 = b'{"id":"seed-action-a","type":"forward_split","symbol":"ZZSEEDCA","ratio":2,"ex":"2024-01-12"}'
_ACTION_B_V1 = b'{"id":"seed-action-b","type":"name_change","symbol":"ZZSEEDCA","new":"ZZSEEDCB"}'
_CA_WINDOW_1 = (datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 31, tzinfo=UTC))
_CA_WINDOW_2 = (datetime(2024, 1, 11, tzinfo=UTC), datetime(2024, 2, 11, tzinfo=UTC))


async def seed_ingest_records(url: URL, dataset_id: uuid.UUID, symbol: str) -> None:
    """Write the cast through the store's own code.

    Args:
        url (URL): The asyncpg URL of the scratch database.
        dataset_id (uuid.UUID): The entry the successful requests write bars under.
        symbol (str): That entry's symbol; the bars delivered are the fake's for it.

    Raises:
        Exception: Whatever the store's code raises for a report it refuses; nothing is caught, a seed is not
            produced from a half-written cast.
    """
    from contextlib import asynccontextmanager

    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from common.enums.data_select import AssetType, DataType
    from common.enums.data_stock import DataSource, Feed, Granularity, UpdateType
    from data.store.app.database.crud import ingest_request as request_crud
    from data.store.app.database.models.ingest_request import IngestRequestKind
    from data.store.app.database.models.ingest_run import IngestRunTrigger
    from data.store.app.ingest import delivery_intake
    from data.store.app.ingest.data_action_request import new_asset_list_request
    from data.store.app.ingest.delivery_intake import DeliveryIntake
    from schemas.data_ingest.delivery import (
        AssetRecord,
        AssetRecords,
        AttemptDelivery,
        CorporateActionRecord,
        CorporateActionRecords,
        DeliveryErrorClass,
        DeliveryHeader,
        DeliveryOutcome,
        DeliveryTrailer,
        RegisterInstanceRequest,
        VendorCallStats,
        VendorReported,
    )
    from schemas.data_ingest.fetch_dataset import Bar, BarPage, ServedRange
    from schemas.data_ingest.submit import AssetListStatus
    from tests.fakes.market_data import fake_bar

    engine = create_async_engine(url, connect_args={'timeout': 10})

    @asynccontextmanager
    async def own_session():
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    def stats(calls: int = 3, pages: int = 2, throttled: int = 1, retries: int = 1) -> VendorCallStats:
        return VendorCallStats(
            calls=calls,
            pages=pages,
            throttled_429=throttled,
            retries=retries,
            first_call_at=_FIRST_CALL,
            last_call_at=_FIRST_CALL + timedelta(seconds=3),
            vendor_reported=VendorReported(limit=200, remaining=150, reset_at=_AS_OF + timedelta(minutes=1)),
        )

    def bars_page(days: Iterable[datetime]) -> BarPage:
        bars = []
        for day in days:
            made = fake_bar(symbol, day)
            bars.append(
                Bar(
                    bar_start=day,
                    open=made.open,
                    high=made.high,
                    low=made.low,
                    close=made.close,
                    volume=float(made.volume),
                    trade_count=made.trade_count,
                    feed=Feed.IEX,
                )
            )
        return BarPage(bars=bars)

    def report(
        name: str,
        instance: str,
        attempt_no: int,
        outcome: DeliveryOutcome,
        *,
        with_stats: bool = True,
        error_class: DeliveryErrorClass | None = None,
        last_attempt: bool = False,
        pages: list | None = None,
        vendor_asset: str | None = None,
    ) -> AttemptDelivery:
        counted = outcome in (DeliveryOutcome.SUCCEEDED, DeliveryOutcome.FAILED)
        header = DeliveryHeader(
            request_id=str(REQUEST_IDS[name]),
            instance_id=instance,
            attempt_no=attempt_no,
            quota_key=QUOTA_KEY if counted else '',
            feed=Feed.IEX,
            vendor_asset_id=(VENDOR_ASSET if vendor_asset is None else vendor_asset) if counted else '',
        )
        if outcome is DeliveryOutcome.SUCCEEDED:
            trailer = DeliveryTrailer(
                outcome=outcome,
                vendor_stats=stats(),
                served_range=ServedRange(start=_WINDOW_START, end=_WINDOW_END),
                as_of=_AS_OF,
            )
            return AttemptDelivery(header=header, parts=pages or [], trailer=trailer)
        if outcome is DeliveryOutcome.FAILED:
            trailer = DeliveryTrailer(
                outcome=outcome,
                error_class=error_class,
                vendor_stats=stats(calls=2, pages=1) if with_stats else None,
                as_of=_AS_OF,
                last_attempt=last_attempt,
                detail='the vendor answered 503'
                if error_class is DeliveryErrorClass.TRANSIENT
                else 'the vendor refused',
            )
            return AttemptDelivery(header=header, parts=[], trailer=trailer)
        return AttemptDelivery(header=header, parts=[], trailer=DeliveryTrailer(outcome=outcome))

    saved_session, saved_publish = delivery_intake.own_session, delivery_intake.publish
    delivery_intake.own_session = own_session
    delivery_intake.publish = lambda event: None
    try:
        intake = DeliveryIntake(chunk_size=2)

        async def create(
            name: str, *, dataset: uuid.UUID | None = None, ready_by: datetime | None = None, best_effort: bool = False
        ) -> None:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                await request_crud.create_request(
                    db,
                    request_crud.NewIngestRequest(
                        id=REQUEST_IDS[name],
                        kind=IngestRequestKind.BARS,
                        source=DataSource.ALPACA_API,
                        client_label=CLIENT_LABEL,
                        dataset_id=dataset,
                        asset_symbol=symbol,
                        asset_type=AssetType.STOCK,
                        data_types=(DataType.MARKET_ACTIVITY.value,),
                        granularity=Granularity.ONE_DAY,
                        update_type=UpdateType.STATIC,
                        feed=Feed.IEX,
                        requested_start=_WINDOW_START,
                        requested_end=_WINDOW_END,
                        ready_by=ready_by,
                        best_effort=best_effort,
                    ),
                )

        async def create_actions(name: str, window: tuple[datetime, datetime]) -> None:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                await request_crud.create_request(
                    db,
                    request_crud.NewIngestRequest(
                        id=REQUEST_IDS[name],
                        kind=IngestRequestKind.CORPORATE_ACTIONS,
                        source=DataSource.ALPACA_API,
                        client_label=CLIENT_LABEL,
                        asset_symbol=CORPORATE_ACTION_SYMBOL,
                        requested_start=window[0],
                        requested_end=window[1],
                    ),
                )

        def actions_report(
            name: str, window: tuple[datetime, datetime], records: list[CorporateActionRecord], as_of: datetime
        ):
            # No feed and no vendor asset id: neither belongs to this kind. The trailer carries the served range.
            return AttemptDelivery(
                header=DeliveryHeader(
                    request_id=str(REQUEST_IDS[name]), instance_id=INSTANCE_B, attempt_no=1, quota_key=QUOTA_KEY
                ),
                parts=[CorporateActionRecords(records=records)],
                trailer=DeliveryTrailer(
                    outcome=DeliveryOutcome.SUCCEEDED,
                    vendor_stats=stats(calls=1, pages=1, throttled=0, retries=0),
                    served_range=ServedRange(start=window[0], end=window[1]),
                    as_of=as_of,
                ),
            )

        def action(vendor_id: str, kind: str, raw: bytes, ex_day: int) -> CorporateActionRecord:
            return CorporateActionRecord(
                vendor_action_id=vendor_id,
                action_type=kind,
                ex_date=datetime(2024, 1, ex_day, tzinfo=UTC),
                identifiers={'symbol': CORPORATE_ACTION_SYMBOL},
                raw_payload=raw,
            )

        async def create_asset_list(name: str, status: AssetListStatus) -> None:
            # The store's own writer for the row, with the seed's literal id.
            new = new_asset_list_request(
                DataSource.ALPACA_API,
                AssetType.STOCK,
                status,
                trigger=IngestRunTrigger.SCHEDULE,
                client_label=CLIENT_LABEL,
            )
            async with AsyncSession(engine, expire_on_commit=False) as db:
                await request_crud.create_request(db, replace(new, id=REQUEST_IDS[name]))

        def asset(key: str, *, active: bool | None) -> AssetRecord:
            vendor_id, symbol = _AL_ASSETS[key]
            state = 'active' if active else 'inactive'
            return AssetRecord(
                vendor_asset_id=vendor_id,
                symbol=symbol,
                raw_payload=f'{{"id":"{vendor_id}","symbol":"{symbol}","status":"{state}"}}'.encode(),
                active=active,
                tradable=bool(active),
                exchange='SEEDX',
                name=f'Seed asset {symbol}',
            )

        def assets_report(name: str, records: list[AssetRecord], as_of: datetime) -> AttemptDelivery:
            # Exactly what AssetListAttemptRunner builds: no feed, no served range, no vendor asset id, the trading
            # quota key, vendor stats and as_of; an empty list carries no part at all.
            return AttemptDelivery(
                header=DeliveryHeader(
                    request_id=str(REQUEST_IDS[name]), instance_id=INSTANCE_B, attempt_no=1, quota_key=QUOTA_KEY
                ),
                parts=[AssetRecords(records=records)] if records else [],
                trailer=DeliveryTrailer(
                    outcome=DeliveryOutcome.SUCCEEDED,
                    vendor_stats=stats(calls=1, pages=1, throttled=0, retries=0),
                    as_of=as_of,
                ),
            )

        async def deliver_list(name: str, status: AssetListStatus, records: list[AssetRecord], as_of: datetime) -> None:
            await create_asset_list(name, status)
            await queue(name, INSTANCE_B)
            await intake.deliver(assets_report(name, records, as_of))

        async def queue(name: str, instance: str) -> None:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                if await request_crud.mark_queued(db, REQUEST_IDS[name], instance) is None:
                    raise RuntimeError(f'seed request {name} was not PENDING')

        async def register(instance: str) -> None:
            await intake.register_instance(RegisterInstanceRequest(instance_id=instance, started_at=_STARTED))

        transient = DeliveryErrorClass.TRANSIENT
        permanent = DeliveryErrorClass.PERMANENT

        # Instance A is the first the store sees. It queues one request and reports a transient failure on it.
        await register(INSTANCE_A)
        await create('NOT_DELIVERED')
        await queue('NOT_DELIVERED', INSTANCE_A)
        await intake.deliver(report('NOT_DELIVERED', INSTANCE_A, 1, DeliveryOutcome.FAILED, error_class=transient))
        # B arrives: A is superseded and its QUEUED request is marked NOT_DELIVERED.
        await register(INSTANCE_B)

        await create('PENDING')
        await create('QUEUED')
        await queue('QUEUED', INSTANCE_B)

        await create('SUCCEEDED', dataset=dataset_id)
        await queue('SUCCEEDED', INSTANCE_B)
        await intake.deliver(report('SUCCEEDED', INSTANCE_B, 1, DeliveryOutcome.FAILED, error_class=transient))
        await intake.deliver(
            report('SUCCEEDED', INSTANCE_B, 2, DeliveryOutcome.SUCCEEDED, pages=[bars_page(_BAR_DAYS)])
        )

        await create('EMPTY', dataset=dataset_id)
        await queue('EMPTY', INSTANCE_B)
        await intake.deliver(report('EMPTY', INSTANCE_B, 1, DeliveryOutcome.SUCCEEDED, pages=[BarPage(bars=[])]))

        await create('FAILED_PERMANENT')
        await queue('FAILED_PERMANENT', INSTANCE_B)
        await intake.deliver(report('FAILED_PERMANENT', INSTANCE_B, 1, DeliveryOutcome.FAILED, error_class=permanent))

        await create('FAILED_NO_STATS')
        await queue('FAILED_NO_STATS', INSTANCE_B)
        await intake.deliver(
            report(
                'FAILED_NO_STATS',
                INSTANCE_B,
                1,
                DeliveryOutcome.FAILED,
                error_class=permanent,
                with_stats=False,
                vendor_asset='',
            )
        )

        await create('FAILED_EXHAUSTED', best_effort=True)
        await queue('FAILED_EXHAUSTED', INSTANCE_B)
        await intake.deliver(report('FAILED_EXHAUSTED', INSTANCE_B, 1, DeliveryOutcome.FAILED, error_class=transient))
        await intake.deliver(
            report('FAILED_EXHAUSTED', INSTANCE_B, 2, DeliveryOutcome.FAILED, error_class=transient, last_attempt=True)
        )

        await create('FAILED_EXPIRED', ready_by=_READY_BY)
        await queue('FAILED_EXPIRED', INSTANCE_B)
        await intake.deliver(report('FAILED_EXPIRED', INSTANCE_B, 1, DeliveryOutcome.EXPIRED))

        # Corporate actions: A and B, then an overlapping fetch that repeats A byte for byte (the dedupe) and brings A
        # again with other bytes (a vendor revision).
        action_a = action('seed-action-a', 'forward_split', _ACTION_A_V1, 10)
        action_b = action('seed-action-b', 'name_change', _ACTION_B_V1, 11)
        await create_actions('CA_FIRST', _CA_WINDOW_1)
        await queue('CA_FIRST', INSTANCE_B)
        await intake.deliver(actions_report('CA_FIRST', _CA_WINDOW_1, [action_a, action_b], _AS_OF))
        revised = action('seed-action-a', 'forward_split', _ACTION_A_V2, 12)
        await create_actions('CA_REVISED', _CA_WINDOW_2)
        await queue('CA_REVISED', INSTANCE_B)
        await intake.deliver(
            actions_report('CA_REVISED', _CA_WINDOW_2, [action_a, revised], _AS_OF + timedelta(days=2))
        )
        # Asset lists: a first snapshot of each list (the s1 shape; no base, no event), the next active one (a4 appears,
        # a2 and a3 vanish), the next inactive one (a2 arrives renamed: a status_flip; i2 appears), then a served empty
        # active list (a recorded snapshot of zero rows, no event).
        active, inactive = AssetListStatus.ACTIVE, AssetListStatus.INACTIVE
        await deliver_list(
            'AL_ACTIVE_1',
            active,
            [asset('AL_A1', active=True), asset('AL_A2', active=True), asset('AL_A3', active=True)],
            _AL_DAY_0,
        )
        await deliver_list('AL_INACTIVE_1', inactive, [asset('AL_I1', active=False)], _AL_DAY_0 + timedelta(hours=1))
        await deliver_list(
            'AL_ACTIVE_2',
            active,
            [asset('AL_A1', active=True), asset('AL_A4', active=True)],
            _AL_DAY_0 + timedelta(days=1),
        )
        await deliver_list(
            'AL_INACTIVE_2',
            inactive,
            [asset('AL_I1', active=False), asset('AL_A2_RENAMED', active=False), asset('AL_I2', active=False)],
            _AL_DAY_0 + timedelta(days=2),
        )
        await deliver_list('AL_ACTIVE_EMPTY', active, [], _AL_DAY_0 + timedelta(days=3))
        await _seed_legacy_runs(engine)
    finally:
        delivery_intake.own_session, delivery_intake.publish = saved_session, saved_publish
        await engine.dispose()


async def _seed_legacy_runs(engine: AsyncEngine) -> None:
    """Write the four legacy-shaped runs through the legacy recorder's crud (no request id, no attempt number).

    The RUNNING row is started and swept FIRST: the sweep fails every RUNNING row, so no other run may be open then.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    from common.enums.data_select import AssetType, DataType
    from common.enums.data_stock import DataSource, Granularity, UpdateType
    from common.errors.vocabulary import ERROR_DOMAIN, Reason
    from data.store.app.database.crud import ingest_run as run_crud
    from data.store.app.database.models.ingest_run import IngestRunState, IngestRunTrigger

    def new_run(symbol: str) -> run_crud.NewRun:
        return run_crud.NewRun(
            trigger=IngestRunTrigger.REQUEST,
            actor=CLIENT_LABEL,
            asset_symbol=symbol,
            asset_type=AssetType.STOCK,
            data_type=DataType.MARKET_ACTIVITY,
            source=DataSource.ALPACA_API,
            granularity=Granularity.ONE_DAY,
            update_type=UpdateType.STATIC,
            requested_start=_WINDOW_START,
            requested_end=_WINDOW_END,
        )

    interrupted, succeeded, empty, failed = LEGACY_SYMBOLS
    async with AsyncSession(engine, expire_on_commit=False) as db:
        await run_crud.start_run(db, new_run(interrupted))
        if await run_crud.fail_interrupted_runs(db) != 1:
            raise RuntimeError('the legacy sweep did not fail exactly the one seeded RUNNING run')
        done = await run_crud.start_run(db, new_run(succeeded))
        await run_crud.finish_run(db, done.id, state=IngestRunState.SUCCEEDED, bars_written=len(_BAR_DAYS))
        done = await run_crud.start_run(db, new_run(empty))
        await run_crud.finish_run(db, done.id, state=IngestRunState.EMPTY, bars_written=0)
        done = await run_crud.start_run(db, new_run(failed))
        await run_crud.finish_run(
            db,
            done.id,
            state=IngestRunState.FAILED,
            bars_written=0,
            failure=run_crud.RunFailure(
                domain=ERROR_DOMAIN, reason=str(Reason.VENDOR_UNAVAILABLE), message='the vendor answered 503'
            ),
        )
