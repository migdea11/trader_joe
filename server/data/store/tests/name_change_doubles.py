"""Doubles for the name_change clamp (P51, tj-7zjov1.146, ADR tj-26jt9w addendum 19). Not a test module.

SINCE P51 EVERY POST SENDS A CORPORATE-ACTIONS REQUEST FIRST. store_market_activity_worker writes, submits and awaits one
corporate-actions request for the symbol, reads the stored name_change records naming it, and only then runs the
own-overlap check and writes the bars request. So every double that stands in for ingest under a POST needs an answer
for that query as well as for the bars, and every SQLite world needs an answer for the read:

  * NameChangeBook stands in for name_changes_naming (crud/corporate_action.py). Its query is Postgres-only (JSONB
    astext, DISTINCT ON), so the SQLite tiers answer it from the book; the real statement is compiled in
    test_name_change_clamp.py and run against Postgres in tests/system/test_name_change_clamp_pg.py. The book answers
    what the real read would: the records naming the symbol as old or new symbol, whichever request delivered them.
  * actions_answer builds the corporate-actions report ingest sends (an empty success by default: the vendor held no
    action in the window; the book, not the delivery, decides the clamp, because the dedupe stores no new row for an
    observation already seen and the read is never limited to this POST's request).
  * Plays delivers scripted reports INSIDE the waiter's sleep, so no two sessions interleave on a StaticPool SQLite
    connection; a corporate-actions Submit takes the actions scripts and a bars Submit the bars scripts.
"""

import functools
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from common.enums.data_stock import DataSource
from data.store.app.database.crud.corporate_action import NameChange
from data.store.app.database.models.ingest_request import IngestRequestKind
from data.store.app.ingest import data_action_request
from data.store.tests.delivery_reports import bar_page, report, with_trailer
from schemas.data_ingest.delivery import AttemptDelivery, DeliveryOutcome
from schemas.data_ingest.fetch_dataset import ServedRange
from schemas.data_ingest.submit import SubmitRequest


def name_change(vendor_action_id: str, old: str | None, new: str | None, process_date: datetime | None) -> NameChange:
    """One stored name_change as the read returns it."""
    return NameChange(vendor_action_id=vendor_action_id, process_date=process_date, old_symbol=old, new_symbol=new)


def vendor_date(year: int, month: int, day: int) -> datetime:
    """A vendor process date as stored: the date at UTC midnight (Alpaca serves a date, the column is timestamptz)."""
    return datetime(year, month, day, tzinfo=UTC)


class NameChangeBook:
    """name_changes_naming's double: the stored name_change records, answered for the symbol, every call recorded.

    Attributes:
        changes: Every stored record, whatever symbol it names; edit it between POSTs to model a later observation.
        calls: (source, symbol, extra keyword arguments) per read. The read takes the source and the symbol only: a
            request id among the extras would be the read limited to one request, which the ruling forbids.
    """

    def __init__(self, changes: Sequence[NameChange] = ()) -> None:
        self.changes: list[NameChange] = list(changes)
        self.calls: list[tuple[DataSource, str, dict[str, Any]]] = []

    async def __call__(self, _db: Any, source: DataSource, symbol: str, *args: Any, **extra: Any) -> list[NameChange]:
        self.calls.append((source, symbol, {**dict(enumerate(args)), **extra}))
        return [change for change in self.changes if symbol in (change.old_symbol, change.new_symbol)]


def install_book(monkeypatch: pytest.MonkeyPatch, changes: Sequence[NameChange] = ()) -> NameChangeBook:
    """Point the POST's read at a book (empty by default: no record, so no clamp)."""
    book = NameChangeBook(changes)
    monkeypatch.setattr(data_action_request, 'name_changes_naming', book)
    return book


def is_query(request: SubmitRequest) -> bool:
    """Whether a Submit is the POST's corporate-actions query."""
    return request.corporate_actions is not None


def bars_of(sent: Sequence[SubmitRequest]) -> list[SubmitRequest]:
    return [request for request in sent if request.bars is not None]


def queries_of(sent: Sequence[SubmitRequest]) -> list[SubmitRequest]:
    return [request for request in sent if is_query(request)]


def actions_answer(
    request_id: str, outcome: DeliveryOutcome = DeliveryOutcome.SUCCEEDED, **kwargs: Any
) -> AttemptDelivery:
    """Ingest's report for a corporate-actions request: by default a success carrying no record."""
    return report(request_id, outcome, kind=IngestRequestKind.CORPORATE_ACTIONS, parts=[], **kwargs)


def empty_actions(request: SubmitRequest) -> AttemptDelivery:
    return actions_answer(request.request_id)


def served_bars(request: SubmitRequest) -> AttemptDelivery:
    """A bars success that served exactly the window the request asked for (ingest serves the row's window)."""
    end = request.bars.end or request.bars.start + timedelta(days=5)
    return with_trailer(
        report(request.request_id, parts=[bar_page(0, 1)]), served_range=ServedRange(start=request.bars.start, end=end)
    )


Script = Callable[[SubmitRequest], AttemptDelivery]


class Plays:
    """ingest, played: each Submit queues its kind's scripted reports, delivered inside the waiter's next sleep.

    Args:
        deliver: The intake's deliver (Harness.deliver, World.intake.deliver).
        monkeypatch: Patches data_action_request.await_verdict to sleep through this double.
        client: The Submit double whose on_submit this takes over (FakeSubmit, or any with an on_submit hook).
        bars: The reports a bars Submit is answered with, in order; () delivers nothing (the request stays open).
        actions: The same for a corporate-actions Submit; () leaves the query open.
    """

    def __init__(
        self,
        deliver: Callable[[AttemptDelivery], Any],
        monkeypatch: pytest.MonkeyPatch,
        client: Any,
        *,
        bars: Sequence[Script] = (served_bars,),
        actions: Sequence[Script] = (empty_actions,),
    ) -> None:
        self.deliver = deliver
        self.client = client
        self.bars = list(bars)
        self.actions = list(actions)
        self.queued: list[AttemptDelivery] = []
        self.sleeps = 0
        self.delivered: list[Any] = []

        async def on_submit(request: SubmitRequest) -> None:
            scripts = self.actions if is_query(request) else self.bars
            self.queued.extend(script(request) for script in scripts)

        async def sleep(_seconds: float) -> None:
            self.sleeps += 1
            while self.queued:
                self.delivered.append(await self.deliver(self.queued.pop(0)))

        client.on_submit = on_submit
        monkeypatch.setattr(
            data_action_request, 'await_verdict', functools.partial(data_action_request.await_verdict, sleep=sleep)
        )
