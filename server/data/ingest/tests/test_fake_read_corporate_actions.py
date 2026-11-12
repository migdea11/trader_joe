"""FakeRead's corporate-actions read (tj-7zjov1.23: the validator brings the read fake to the extended interface).

FakeRead (tests/fakes/market_data.py) stands for BrokerRead in tests and the fake-mode stack. Its corporate-actions
read serves NO action for any query and never fails: no prefix scenario applies, and the fake invents no action the
store would judge as published. Actions are served only by a fake built with a world (P14, tj-7zjov1.24), which
test_fake_read_world.py covers.
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from data.ingest.app.brokers.interface import CallTally, CorporateActionsQuery, CorporateActionsResponse
from data.ingest.app.brokers.rate_budget import RequestPriority
from tests.fakes.market_data import FAIL_PREFIX, RATELIMIT_PREFIX, SLOW_PREFIX, FakeRead


pytestmark = pytest.mark.data_ingest

# A clock in another offset, so as_of is shown converted to UTC.
NOW = datetime(2026, 10, 26, 10, 0, tzinfo=timezone(timedelta(hours=-4)))
START = datetime(2026, 1, 1, tzinfo=UTC)
END = datetime(2026, 2, 1, tzinfo=UTC)


async def never_sleep(seconds: float) -> None:
    raise AssertionError(f'the fake slept {seconds}s for a corporate-actions read')


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('symbol', 'start', 'end'),
    [
        pytest.param('VFV', START, END, id='a-window'),
        pytest.param(f'{FAIL_PREFIX}X', START, END, id='fail-prefix'),
        pytest.param(f'{RATELIMIT_PREFIX}X', START, END, id='ratelimit-prefix'),
        pytest.param(f'{SLOW_PREFIX}X', START, END, id='slow-prefix'),
        pytest.param('', START, END, id='no-symbol'),
        pytest.param('VFV', END, START, id='reversed-window'),
    ],
)
async def test_every_query_is_served_with_no_actions_stamped_with_the_fakes_clock_in_utc(symbol, start, end):
    tally = CallTally()
    query = CorporateActionsQuery(symbol, start, end, priority=RequestPriority.BACKFILL, tally=tally)

    outcome = await FakeRead(sleep=never_sleep, clock=lambda: NOW).get_corporate_actions(query)

    assert outcome == CorporateActionsResponse(actions=[], as_of=datetime(2026, 10, 26, 14, 0, tzinfo=UTC))
    assert outcome.as_of.tzinfo is UTC
    assert (tally.calls, tally.pages) == (0, 0), 'the fake counted a vendor call it never made'
