"""The seed's ingest records: ingest_instance, ingest_request and ingest_run rows, written by the store's REAL code.

Bead tj-7zjov1.100 (P33d). The seed held only store_dataset_entry and stock_market_activity, so the migration
tests said nothing about the three tables of the request record across an upgrade or a downgrade. This module
fills them the way production does, not with raw SQL, so the seed proves the code and the schema agree:

  * ingest_instance: note_instance_seen (the instance marker's own statement) through DeliveryIntake.register_instance;
  * ingest_request: create_request (PENDING, as the submit path leaves it) and mark_queued;
  * ingest_run attempt rows, the request's count, spend and state: DeliveryIntake.deliver, the whole delivery
    transaction, bars written through the real bar writer.

(The ten ingest_run rows of the POSTed scenario are written by data_store itself, with no request id.)

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
The dataset_id of a request that names none stays NULL. Every counted attempt carries the vendor asset id except the
FAILED one with no stats, whose header names none, so the seed holds both values of ingest_run.vendor_asset_id.

NOTHING HERE READS A CLOCK. Every instant is a literal; the instants Postgres stamps with now() (created_at,
finished_at, last_seen_at and the like) and the random attempt-row ids are rewritten by dump.NORMALISE_SQL.

The app modules are imported inside the function: they read the database environment at import, and this
package must stay importable without it.
"""

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from sqlalchemy.engine import URL


INSTANCE_A = 'seed-instance-a'
INSTANCE_B = 'seed-instance-b'
INSTANCES = (INSTANCE_A, INSTANCE_B)
QUOTA_KEY = 'seed-quota-key'
VENDOR_ASSET = 'seed-vendor-asset'
CLIENT_LABEL = 'seed-owner-a'

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
        ),
        start=1,
    )
}

# What the seeded rows must come to; the producer refuses a seed that does not match.
EXPECTED_REQUEST_STATES = {'PENDING': 1, 'QUEUED': 1, 'NOT_DELIVERED': 1, 'SUCCEEDED': 1, 'EMPTY': 1, 'FAILED': 4}
EXPECTED_ATTEMPT_ROWS = 8
EXPECTED_INSTANCES = len(INSTANCES)

_WINDOW_START = datetime(2024, 1, 1, tzinfo=UTC)
_WINDOW_END = datetime(2024, 2, 1, tzinfo=UTC)
_FIRST_CALL = datetime(2024, 4, 15, 14, 30, tzinfo=UTC)
_AS_OF = _FIRST_CALL + timedelta(seconds=4)
_STARTED = datetime(2024, 4, 15, 9, 0, tzinfo=UTC)
_READY_BY = datetime(2024, 4, 15, 12, 0, tzinfo=UTC)
# Two days the scenario's ZZSEEDAA dataset already holds, on the daily grid.
_BAR_DAYS = (datetime(2024, 1, 2, tzinfo=UTC), datetime(2024, 1, 3, tzinfo=UTC))


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
    from data.store.app.ingest import delivery_intake
    from data.store.app.ingest.delivery_intake import DeliveryIntake
    from schemas.data_ingest.delivery import (
        AttemptDelivery,
        DeliveryErrorClass,
        DeliveryHeader,
        DeliveryOutcome,
        DeliveryTrailer,
        RegisterInstanceRequest,
        VendorCallStats,
        VendorReported,
    )
    from schemas.data_ingest.fetch_dataset import Bar, BarPage, ServedRange
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
    finally:
        delivery_intake.own_session, delivery_intake.publish = saved_session, saved_publish
        await engine.dispose()
