"""common.scheduling's AfterTradingEvent over the store's real XNYS adapter (validator, gating tj-grna9p.130).

WHY HERE AND NOT IN common/tests. The design (tj-grna9p.130) says callers pass calendar_for_source(source) to
AfterTradingEvent unchanged, because ExchangeCalendarAdapter already satisfies common's SessionCalendar protocol
structurally. Checking that needs the adapter, and the adapter needs exchange_calendars and pandas, which only the
data-store group carries. common/tests/test_scheduling.py pins the schedule against a hand-built calendar; this file
pins it against the exchange's own sessions, holidays and early closes, through the adapter the service uses.

THE DATES, all 2026, UTC: Fri 6 Mar close 21:00Z (EST), Mon 9 Mar close 20:00Z (EDT); Fri 30 Oct close 20:00Z (EDT),
Mon 2 Nov close 21:00Z (EST); Wed 25 Nov close 21:00Z, Thu 26 Nov Thanksgiving, Fri 27 Nov early close 18:00Z.
"""

import inspect
from datetime import datetime, timedelta

import pytest

from common.enums.data_stock import DataSource
from common.scheduling import AfterTradingEvent, SessionCalendar, TradingEvent
from data.store.app.freshness import ExchangeCalendarAdapter, calendar_for_source


pytestmark = pytest.mark.data_store

XNYS = calendar_for_source(DataSource.ALPACA_API)
CLOSE, OPEN = TradingEvent.CLOSE, TradingEvent.OPEN
M = timedelta(minutes=1)


def z(text: str) -> datetime:
    """An instant from an ISO string ending in Z."""
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


PROTOCOL_METHODS = ('session_open', 'session_close', 'next_session', 'last_completed_session')


def test_the_protocol_names_exactly_the_four_methods_the_design_lists():
    # typing.Protocol adds its own __init__; the protocol's surface is the non-dunder methods.
    declared = {
        name for name, value in vars(SessionCalendar).items() if inspect.isfunction(value) and not name.startswith('__')
    }
    assert declared == set(PROTOCOL_METHODS)


@pytest.mark.parametrize('method', PROTOCOL_METHODS)
def test_the_store_adapter_matches_the_protocol_method_for_method(method):
    """Same parameter names, kinds and annotations: a keyword call written against the protocol works on the adapter."""
    assert isinstance(XNYS, ExchangeCalendarAdapter)
    protocol = inspect.signature(getattr(SessionCalendar, method), eval_str=True)
    adapter = inspect.signature(getattr(ExchangeCalendarAdapter, method), eval_str=True)
    assert [(p.name, p.kind, p.annotation) for p in adapter.parameters.values()] == [
        (p.name, p.kind, p.annotation) for p in protocol.parameters.values()
    ]
    assert adapter.return_annotation == protocol.return_annotation


@pytest.mark.parametrize(
    'event, offset, now, expected',
    [
        pytest.param(CLOSE, 30 * M, '2026-10-14T12:00Z', '2026-10-14T20:30Z', id='close+30 normal day'),
        pytest.param(CLOSE, 30 * M, '2026-10-14T20:30Z', '2026-10-15T20:30Z', id='now exactly at the fire'),
        pytest.param(CLOSE, 30 * M, '2026-10-16T20:31Z', '2026-10-19T20:30Z', id='friday skips to monday'),
        pytest.param(CLOSE, 30 * M, '2026-03-06T12:00Z', '2026-03-06T21:30Z', id='march before dst'),
        pytest.param(CLOSE, 30 * M, '2026-03-06T21:30Z', '2026-03-09T20:30Z', id='march across dst'),
        pytest.param(CLOSE, 30 * M, '2026-10-30T21:00Z', '2026-11-02T21:30Z', id='november across dst'),
        pytest.param(CLOSE, 30 * M, '2026-11-25T21:30Z', '2026-11-27T18:30Z', id='thanksgiving skipped, half day'),
        pytest.param(OPEN, -15 * M, '2026-10-14T13:20Z', '2026-10-15T13:15Z', id='open-15'),
        pytest.param(CLOSE, 600 * M, '2026-10-17T01:00Z', '2026-10-17T06:00Z', id='close+10h on saturday'),
        pytest.param(CLOSE, 36 * 60 * M, '2026-10-31T09:00Z', '2026-11-01T08:00Z', id='close+36h over fall'),
    ],
)
def test_after_trading_event_over_xnys(event, offset, now, expected):
    assert AfterTradingEvent(XNYS, event, offset).next_fire(z(now), None) == z(expected)
