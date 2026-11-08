"""POST /store's push path over SQLite, for the route and request-path tests (validator, tj-7zjov1.28, P35). Not a test module.

WHAT CHANGED UNDER THE ROUTE. Before P35 the POST held one FetchDataset stream and wrote the entry and the bars in
one transaction on the request's session, so a recording fake session and a scripted stream were the whole world.
Now the POST writes an ingest_request row and commits it, SUBMITS it to ingest, notes the answering instance, marks
the request QUEUED (each on a session of its own, own_session), and then WAITS, polling the request row, until the
delivery intake has finished it; ingest pushes the result to that intake, which writes the entry, the bars and the
attempt row in one transaction. A fake session cannot answer that, so this harness is the smallest world that can:

  - REAL TABLES in a SQLite file (the entries, the bars, ingest_run, ingest_request, ingest_instance), the
    request's session (async_db) and every own_session pointed at it;
  - THE REAL SUBMIT PATH and THE REAL WAITER (data_action_request), and THE REAL DELIVERY INTAKE;
  - a Submit double (SubmitDouble) that answers as ingest does and then DELIVERS the attempt it was given a script
    for, through the intake, on the running loop: the waiter sees the request finish exactly as in production;
  - two Postgres-only statements replaced by recorders: the entry upsert (ON CONFLICT on the natural key; answered
    with PostStore.entry_id) and the bar upsert. lock_stored_collect is answered with PostStore.stored_collect, the
    stored intent the old fake session's second answer gave, so a test can stand a repeat POST on a stopped entry.

The waiter polls every millisecond here (STORE_VERDICT_POLL_SECONDS); a script with no delivery shortens the
deadline (STORE_VERDICT_DEADLINE_SECONDS) so the deadline answer is reached without waiting five minutes.

SINCE P51 (tj-7zjov1.146, ADR tj-26jt9w addendum 19) every POST first writes, submits and awaits one corporate-actions
request (the name_change clamp's query). The double answers it with an empty success unless told otherwise
(QueryAnswer), records it apart from the bars (SubmitDouble.queries, journal 'submit corporate_actions'), and the
clamp's Postgres-only read is answered from PostStore.book (name_change_doubles.py).
"""

import asyncio
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from common.enums.data_stock import Feed
from data.store.app.app_depends import get_ingest_submit_client
from data.store.app.database.database import async_db
from data.store.app.database.models.ingest_request import IngestRequest, IngestRequestKind
from data.store.app.database.models.ingest_run import IngestRun
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from data.store.app.ingest import data_action_request, delivery_intake
from data.store.app.main import app
from data.store.tests.delivery_reports import INSTANCE_A, report, stats, with_trailer
from data.store.tests.name_change_doubles import NameChangeBook, install_book
from data.store.tests.request_tables import create_request_tables
from data.store.tests.test_ui_datasets_route import _sqlite_timezone
from routers.common.instance_secret import INSTANCE_SECRET_ENV_VAR, INSTANCE_SECRET_HEADER
from schemas.data_ingest import fetch_dataset
from schemas.data_ingest.delivery import DeliveryErrorClass, DeliveryOutcome
from schemas.data_ingest.submit import SubmitAccepted, SubmitAck, SubmitRequest, SubmitResponse


# The instant the delivered window is answered for. Fixed, so a served_range assertion reads a literal.
SERVED_START = datetime(2026, 1, 2, tzinfo=UTC)
SERVED_END = SERVED_START + timedelta(minutes=3)


@dataclass
class Delivery:
    """What ingest reports for the one attempt it makes of a request it accepted.

    Attributes:
        outcome: SUCCEEDED (bars or none), FAILED, EXPIRED or EXHAUSTED.
        bars: The bars of a SUCCEEDED report, one page.
        served: The served window of a SUCCEEDED report.
        error_class: TRANSIENT or PERMANENT, for FAILED.
        last_attempt: For FAILED: ingest will not try again.
        feed: The header's resolved tape.
        detail: The trailer's human sentence.
    """

    outcome: DeliveryOutcome = DeliveryOutcome.SUCCEEDED
    bars: list[fetch_dataset.Bar] = field(default_factory=list)
    served: fetch_dataset.ServedRange | None = field(
        default_factory=lambda: fetch_dataset.ServedRange(start=SERVED_START, end=SERVED_END)
    )
    error_class: DeliveryErrorClass = DeliveryErrorClass.TRANSIENT
    last_attempt: bool = False
    feed: Feed = Feed.IEX
    detail: str = ''


class PostStore:
    """The SQLite world: its engine, a journal of statements, commits and Submits, and what the recorders saw."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.journal: list[str] = []
        self.events: list[Any] = []
        self.bar_batches: list[Any] = []
        self.created: list[tuple[Any, dict[str, Any]]] = []
        # The entry upsert statements the real crud function built, one per created entry.
        self.upserts: list[Any] = []
        self.entry_id = uuid.uuid4()
        self.stored_collect: bool | None = None
        self.intake = delivery_intake.DeliveryIntake(chunk_size=100)
        # The stored name_change records the POST's clamp reads (P51); set by the fixture, empty by default.
        self.book: NameChangeBook = NameChangeBook()

    async def requests(self) -> list[IngestRequest]:
        """Every request row, of every kind (since P51 a POST writes a corporate-actions row before its bars row)."""
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            return list((await db.execute(select(IngestRequest))).scalars())

    async def bars_requests(self) -> list[IngestRequest]:
        return [row for row in await self.requests() if IngestRequestKind(row.kind) is IngestRequestKind.BARS]

    async def query_requests(self) -> list[IngestRequest]:
        """The corporate-actions rows: one per POST, the name_change clamp's query."""
        return [
            row for row in await self.requests() if IngestRequestKind(row.kind) is IngestRequestKind.CORPORATE_ACTIONS
        ]

    async def runs(self) -> list[IngestRun]:
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            return list((await db.execute(select(IngestRun))).scalars())

    async def add(self, *rows: Any) -> None:
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            db.add_all(rows)
            await db.commit()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            yield db


@dataclass
class QueryAnswer:
    """What ingest reports for the POST's corporate-actions query (P51): by default a success carrying no record.

    The records themselves never travel here: the clamp reads the store (PostStore.book), because the dedupe stores no
    new row for an observation already seen and the read is never limited to this POST's request.

    Attributes:
        outcome: SUCCEEDED, FAILED, EXPIRED or EXHAUSTED.
        error_class: TRANSIENT or PERMANENT, for FAILED.
        last_attempt: For FAILED: ingest will not try again.
    """

    outcome: DeliveryOutcome = DeliveryOutcome.SUCCEEDED
    error_class: DeliveryErrorClass = DeliveryErrorClass.TRANSIENT
    last_attempt: bool = False


class SubmitDouble:
    """The Submit seam's double: records each Submit, answers it, then delivers the scripted attempt.

    SINCE P51 A POST SENDS TWO REQUESTS: first the corporate-actions query of the name_change clamp, then the bars. The
    two are kept apart so each pin reads the one it is about: `submitted` holds the BARS Submits (what every pin
    before P51 read), `queries` the corporate-actions ones. delivery, raises and refused script the BARS request;
    query, query_raises script the query, which by default is accepted and answered with an empty success.

    Args:
        store: The world the delivery is written into.
        delivery: What ingest reports after accepting the bars; None delivers nothing (the request stays open).
        raises: The bars call fails with this instead of answering (nothing is delivered).
        refused: Ingest answers the bars with this refusal instead of accepting (nothing is delivered).
        instance_id: The answering instance.
        query: What ingest reports for the corporate-actions query; None delivers nothing (the query stays open).
        query_raises: The query's call fails with this instead of answering.
    """

    def __init__(
        self,
        store: PostStore,
        delivery: Delivery | None = None,
        *,
        raises: BaseException | None = None,
        refused: fetch_dataset.FetchRefused | None = None,
        instance_id: str = INSTANCE_A,
        query: QueryAnswer | None = QueryAnswer(),  # noqa: B008 (frozen-by-convention default: never mutated)
        query_raises: BaseException | None = None,
    ) -> None:
        self.store = store
        self.delivery = Delivery() if delivery is None and raises is None and refused is None else delivery
        self.raises = raises
        self.refused = refused
        self.instance_id = instance_id
        self.query = query
        self.query_raises = query_raises
        self.submitted: list[SubmitRequest] = []
        self.queries: list[SubmitRequest] = []
        self.delivered: list[Any] = []
        self._tasks: list[asyncio.Task] = []

    @classmethod
    def silent(cls, store: PostStore) -> 'SubmitDouble':
        """A double that accepts the bars and never delivers them: the bars request stays QUEUED. The query is answered."""
        double = cls(store)
        double.delivery = None
        return double

    async def submit(self, request: SubmitRequest) -> SubmitResponse:
        if request.corporate_actions is not None:
            return await self._query(request)
        self.submitted.append(request)
        self.store.journal.append('submit')
        if self.raises is not None:
            raise self.raises
        if self.refused is not None:
            return SubmitResponse(ack=SubmitAck(refused=self.refused), instance_id=self.instance_id)
        if self.delivery is not None:
            self._tasks.append(asyncio.get_running_loop().create_task(self._deliver(request, self.delivery)))
        accepted = SubmitAccepted(feed=request.bars.feed or Feed.IEX, quota_key='quota-key')
        return SubmitResponse(ack=SubmitAck(accepted=accepted), instance_id=self.instance_id)

    async def instance(self) -> Any:
        raise AssertionError('the POST never probes the instance')

    async def _query(self, request: SubmitRequest) -> SubmitResponse:
        self.queries.append(request)
        self.store.journal.append('submit corporate_actions')
        if self.query_raises is not None:
            raise self.query_raises
        if self.query is not None:
            self._tasks.append(asyncio.get_running_loop().create_task(self._answer(request, self.query)))
        accepted = SubmitAccepted(feed=None, quota_key='quota-key')
        return SubmitResponse(ack=SubmitAck(accepted=accepted), instance_id=self.instance_id)

    async def _answer(self, request: SubmitRequest, answer: QueryAnswer) -> None:
        await asyncio.sleep(0)
        kind = IngestRequestKind.CORPORATE_ACTIONS
        if answer.outcome is DeliveryOutcome.SUCCEEDED:
            attempt = report(request.request_id, kind=kind, parts=[])
            window = fetch_dataset.ServedRange(start=request.corporate_actions.start, end=request.corporate_actions.end)
            attempt = with_trailer(attempt, served_range=window)
        elif answer.outcome is DeliveryOutcome.FAILED:
            attempt = report(
                request.request_id,
                DeliveryOutcome.FAILED,
                kind=kind,
                error_class=answer.error_class,
                last_attempt=answer.last_attempt,
            )
        else:
            attempt = report(request.request_id, answer.outcome, kind=kind)
        self.delivered.append(await self.store.intake.deliver(attempt))

    async def _deliver(self, request: SubmitRequest, delivery: Delivery) -> None:
        await asyncio.sleep(0)
        now = datetime.now(UTC)
        called = stats().model_copy(update={'first_call_at': now, 'last_call_at': now})
        if delivery.outcome is DeliveryOutcome.SUCCEEDED:
            parts = [fetch_dataset.BarPage(bars=delivery.bars)] if delivery.bars else []
            attempt = report(request.request_id, parts=parts, vendor_stats=called)
            attempt = with_trailer(attempt, served_range=delivery.served, as_of=now)
        elif delivery.outcome is DeliveryOutcome.FAILED:
            attempt = report(
                request.request_id,
                DeliveryOutcome.FAILED,
                error_class=delivery.error_class,
                last_attempt=delivery.last_attempt,
                vendor_stats=called,
                detail=delivery.detail,
            )
        else:
            attempt = report(request.request_id, delivery.outcome, detail=delivery.detail)
        attempt = attempt.model_copy(update={'header': attempt.header.model_copy(update={'feed': delivery.feed})})
        self.delivered.append(await self.store.intake.deliver(attempt))


def _classify(statement: str) -> str:
    """'SELECT store_dataset_entry', 'INSERT ingest_request', ... : the verb and the first table named."""
    verb = statement.split(None, 1)[0].upper()
    match = re.search(r'\b(?:FROM|INTO|UPDATE)\s+"?(\w+)', statement, re.IGNORECASE)
    return f'{verb} {match.group(1) if match else "?"}'


@pytest_asyncio.fixture
async def post_store(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[PostStore]:
    """The SQLite world with the POST path's sessions, the intake's writes and the waiter's pace pointed at it."""
    request.getfixturevalue('aware_sqlite_datetimes')
    monkeypatch.setattr(IngestRequest.__table__.c.data_types, 'type', sa.JSON())
    # A FILE, one connection per session, NOT the in-memory StaticPool the other SQLite tiers use: here the waiter's
    # session and the delivery's run CONCURRENTLY on the loop, and over one shared connection the waiter's session
    # closing rolls back the delivery's open transaction under it (measured: the attempt row vanished while the
    # request's later UPDATE committed). Separate connections give each its own transaction, as Postgres does.
    database = request.getfixturevalue('tmp_path') / 'post_path.sqlite'
    engine = create_async_engine(f'sqlite+aiosqlite:///{database}', poolclass=NullPool)

    @event.listens_for(engine.sync_engine, 'connect')
    def register(dbapi_connection, _record):
        dbapi_connection.run_async(lambda c: c.create_function('timezone', 2, _sqlite_timezone))
        dbapi_connection.run_async(lambda c: c.create_function('gen_random_uuid', 0, lambda: uuid.uuid4().hex))

    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: StoreDatasetEntry.metadata.create_all(
                sync, tables=[StoreDatasetEntry.__table__, StockMarketActivity.__table__, IngestRun.__table__]
            )
        )
        await connection.run_sync(create_request_tables)
    store = PostStore(engine)

    @event.listens_for(engine.sync_engine, 'before_cursor_execute')
    def journal_statement(_conn, _cursor, statement, _parameters, _context, _executemany):
        store.journal.append(_classify(statement))

    @event.listens_for(engine.sync_engine, 'commit')
    def journal_commit(_conn):
        store.journal.append('COMMIT')

    real_upsert = delivery_intake.upsert_entry_in_transaction

    class _CapturingSession:
        """Hands the REAL upsert's one statement back instead of running it (ON CONFLICT is Postgres-only)."""

        statement: Any = None

        async def execute(self, statement: Any) -> Any:
            self.statement = statement
            return SimpleNamespace(scalar_one=lambda: store.entry_id)

    async def upsert(_db: AsyncSession, created: Any, **kwargs: Any) -> uuid.UUID:
        # The real function builds the statement, so what a test reads from it (the ON CONFLICT SET clause) is the
        # statement production would send; only its execution is skipped.
        capture = _CapturingSession()
        entry_id = await real_upsert(capture, created, **kwargs)
        store.created.append((created, kwargs))
        store.upserts.append(capture.statement)
        return entry_id

    async def stored_collect(_db: AsyncSession, _entry: Any) -> bool | None:
        return store.stored_collect

    async def write_bars(_db: AsyncSession, batch: Any, requested_chunk_size: int | None = None) -> int:
        store.bar_batches.append(batch)
        return sum(len(points) for points in batch.dataset.values())

    monkeypatch.setattr(data_action_request, 'own_session', store.session)
    monkeypatch.setattr(delivery_intake, 'own_session', store.session)
    # The clamp's read is Postgres-only (JSONB, DISTINCT ON): the book answers it (name_change_doubles.py).
    store.book = install_book(monkeypatch)

    def publish(event_: Any) -> None:
        # On the journal too, so a test can read that an event followed the commit that wrote what it announces.
        store.journal.append(f'publish {type(event_).__name__}')
        store.events.append(event_)

    monkeypatch.setattr(delivery_intake, 'publish', publish)
    monkeypatch.setattr(delivery_intake, 'upsert_entry_in_transaction', upsert)
    monkeypatch.setattr(delivery_intake, 'lock_stored_collect', stored_collect)
    monkeypatch.setattr(delivery_intake, 'write_market_activity_in_transaction', write_bars)
    monkeypatch.setenv(data_action_request.VERDICT_POLL_ENV, '0.001')
    monkeypatch.delenv(data_action_request.VERDICT_DEADLINE_ENV, raising=False)
    try:
        yield store
    finally:
        await engine.dispose()


ROUTE_NAME = 'store_data'
PATH_PARAMS = {'asset_type': 'stock', 'data_type': 'market-activity', 'asset_symbol': 'AAPL'}
SECRET = 'post-path-instance-secret'


async def post(
    store: PostStore,
    client: SubmitDouble,
    body: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    raise_app_exceptions: bool = True,
    session: Any = None,
) -> httpx.Response:
    """Drive the real POST /store over ASGI on the running loop, so the double's delivery runs beside the waiter.

    Args:
        store: The SQLite world; the request's session is one of its sessions unless `session` is given.
        client: The Submit double the route is handed.
        body: The JSON body.
        monkeypatch: Sets the instance secret.
        raise_app_exceptions: False renders an unhandled error as the 500 a caller receives.
        session: A stand-in for the request's session (a fake that raises), or None for the store's.

    Returns:
        httpx.Response: The answer.
    """
    monkeypatch.setenv(INSTANCE_SECRET_ENV_VAR, SECRET)

    async def request_session() -> AsyncIterator[Any]:
        if session is not None:
            yield session
            return
        async with store.session() as db:
            yield db

    app.dependency_overrides[async_db] = request_session
    app.dependency_overrides[get_ingest_submit_client] = lambda: client
    try:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
        async with httpx.AsyncClient(transport=transport, base_url='http://store') as http:
            return await http.post(
                app.url_path_for(ROUTE_NAME, **PATH_PARAMS), json=body, headers={INSTANCE_SECRET_HEADER: SECRET}
            )
    finally:
        # `app` is a module-level singleton other test modules import.
        app.dependency_overrides.clear()
