"""FakeIngest's re-stated intakes cannot drift from AlpacaHandle's (P53v, tj-7zjov1.159; the P14 gate, tj-7zjov1.24).

tests/fakes/ingest.py builds its handle from production parts but writes the asset-list and corporate-actions intakes
out again, because AlpacaHandle has no seam for FakeIngest's injectable clocks. A re-statement drifts silently, so this
file runs both on the SAME requests and requires the same answer: the ack (accepted, its quota key, its feed), the
request queued (its scope, its work type and priority, its tape, its deadline and count), and every refusal (the error
type, the reason and the metadata). The refusal detail is not compared: the fake says "The fake lists stocks only",
deliberately, where Alpaca's names Alpaca.

The kind's missing-body refusal cannot be reached through submit (a SubmitRequest's kind is the body it carries), so
those two rules are compared by calling the intakes themselves; AlpacaHandle's are private static methods, reached by
their mangled names because nothing else reaches them.

WHAT IS REAL. The production AlpacaHandle and BrokerHandle, and FakeIngest as P14 and P26 use it. Pass-through spies on
each handle's own scheduler only record what was queued. No vendor is called: nothing is started.
"""

from datetime import UTC, datetime

import pytest

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity, UpdateType
from common.errors.vocabulary import TraderJoeError
from data.ingest.app.brokers.alpaca import broker_api
from data.ingest.app.brokers.alpaca.handle import AlpacaHandle
from data.ingest.app.brokers.handle import BrokerHandle
from data.ingest.app.scheduler.queue import ScheduledRequest
from schemas.data_ingest.submit import (
    AssetListRequest,
    AssetListStatus,
    BarsRequest,
    CorporateActionsRequest,
    SubmitRequest,
)
from tests.fakes import ingest as fake_ingest
from tests.fakes.ingest import FakeIngest


pytestmark = pytest.mark.data_ingest

SIP_VAR = 'ALPACA_SIP_ENABLED'
READY_BY = datetime(2026, 3, 2, tzinfo=UTC)

FEEDS = pytest.mark.parametrize(
    ('setting', 'feed'), [pytest.param('false', Feed.IEX, id='iex'), pytest.param('true', Feed.SIP, id='sip')]
)


def listing(asset_type: AssetType, status: AssetListStatus = AssetListStatus.ACTIVE) -> AssetListRequest:
    return AssetListRequest(source=DataSource.ALPACA_API, asset_type=asset_type, status=status)


ACTIONS = CorporateActionsRequest(
    source=DataSource.ALPACA_API,
    asset_symbol='VFV',
    start=datetime(2026, 1, 1, tzinfo=UTC),
    end=datetime(2026, 2, 1, tzinfo=UTC),
)
BARS = BarsRequest(
    source=DataSource.ALPACA_API,
    asset_symbol='VFV',
    asset_type=AssetType.STOCK,
    data_types=[DataType.MARKET_ACTIVITY],
    granularity=Granularity.ONE_HOUR,
    start=datetime(2026, 1, 2, 14, tzinfo=UTC),
    end=datetime(2026, 1, 2, 17, tzinfo=UTC),
    update_type=UpdateType.STATIC,
)

# Requests both intakes must accept, with the fields that reach the queue varied.
ACCEPTED = pytest.mark.parametrize(
    'body',
    [
        pytest.param({'asset_list': listing(AssetType.STOCK, AssetListStatus.ACTIVE)}, id='asset-list-active'),
        pytest.param({'asset_list': listing(AssetType.STOCK, AssetListStatus.INACTIVE)}, id='asset-list-inactive'),
        pytest.param({'asset_list': listing(AssetType.STOCK), 'ready_by': READY_BY}, id='asset-list-ready-by'),
        pytest.param({'corporate_actions': ACTIONS}, id='corporate-actions'),
        pytest.param({'corporate_actions': ACTIONS, 'ready_by': READY_BY, 'attempt_count': 2}, id='actions-counted'),
    ],
)
# Asset lists both intakes must refuse: every asset type that is not a stock.
REFUSED_TYPES = pytest.mark.parametrize(
    'asset_type', [asset_type for asset_type in AssetType if asset_type is not AssetType.STOCK]
)


class NoStore:
    """A store nothing here reaches: neither handle is started."""

    async def deliver(self, attempt):
        raise AssertionError('nothing is started, so nothing is delivered')

    async def register_instance(self, request):
        raise AssertionError('nothing is started, so nothing is announced')


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.delenv('ALPACA_RATE_LIMIT_PER_MINUTE', raising=False)
    for name in (*broker_api.ALPACA_CREDENTIAL_VARS, broker_api.ALPACA_TRADING_HOST_VAR):
        monkeypatch.delenv(name, raising=False)


def queued_by(handle: BrokerHandle) -> list[ScheduledRequest]:
    """Record every request the handle queues, passing each through to its own scheduler unchanged."""
    queued: list[ScheduledRequest] = []
    submit = handle.scheduler.submit

    async def recorded(request: ScheduledRequest) -> None:
        queued.append(request)
        await submit(request)

    handle.scheduler.submit = recorded
    return queued


def both(monkeypatch, setting: str, feed: Feed) -> tuple[BrokerHandle, BrokerHandle]:
    """AlpacaHandle and FakeIngest's handle on the same account feed."""
    monkeypatch.setenv(SIP_VAR, setting)
    alpaca = AlpacaHandle(calls_per_minute=60)
    fake = FakeIngest(NoStore(), feed=feed, calls_per_minute=60).handle
    assert alpaca.feed is fake.feed is feed
    return alpaca, fake


def queued_view(request: ScheduledRequest) -> dict:
    """Everything an intake decides about a queued request, and what the shared tail copies from the SubmitRequest."""
    return {
        'request_id': request.request_id,
        'quota_scope': request.quota_scope,
        'payload_type': type(request.payload),
        'payload': request.payload,
        'feed': request.feed,
        'ready_by': request.ready_by,
        'attempt_count': request.attempt_count,
    }


def refusal_view(error: BaseException) -> dict:
    assert isinstance(error, TraderJoeError), f'expected a typed refusal, got {error!r}'
    return {'type': type(error), 'reason': error.reason, 'metadata': dict(error.metadata)}


@pytest.mark.asyncio
@FEEDS
@ACCEPTED
async def test_both_intakes_accept_and_queue_the_same_request_the_same_way(monkeypatch, setting, feed, body):
    alpaca, fake = both(monkeypatch, setting, feed)
    alpaca_queued, fake_queued = queued_by(alpaca), queued_by(fake)
    request = SubmitRequest(request_id='same', owner='world-manager', **body)

    alpaca_ack = await alpaca.submit(request)
    fake_ack = await fake.submit(request)

    assert fake_ack == alpaca_ack, 'FakeIngest acks this kind otherwise than AlpacaHandle'
    # The no-tape rule, on both: a kind with no tape takes none of the account's feed.
    assert alpaca_ack.accepted is not None
    assert alpaca_ack.accepted.feed is None
    assert [queued_view(q) for q in fake_queued] == [queued_view(q) for q in alpaca_queued], (
        'FakeIngest queues this kind otherwise than AlpacaHandle'
    )
    [queued] = alpaca_queued
    assert queued.feed is None


@pytest.mark.asyncio
@FEEDS
@REFUSED_TYPES
async def test_both_intakes_refuse_an_asset_list_that_is_not_stocks_alike_and_queue_nothing(
    monkeypatch, setting, feed, asset_type
):
    alpaca, fake = both(monkeypatch, setting, feed)
    alpaca_queued, fake_queued = queued_by(alpaca), queued_by(fake)
    request = SubmitRequest(request_id='not-stocks', owner='world-manager', asset_list=listing(asset_type))

    with pytest.raises(TraderJoeError) as alpaca_refusal:
        await alpaca.submit(request)
    with pytest.raises(TraderJoeError) as fake_refusal:
        await fake.submit(request)

    assert refusal_view(fake_refusal.value) == refusal_view(alpaca_refusal.value)
    assert alpaca_queued == fake_queued == []
    assert alpaca.active == fake.active == 0


# Each kind's intake, AlpacaHandle's and FakeIngest's, beside a request carrying another kind's body.
MISSING_BODY = pytest.mark.parametrize(
    ('alpaca_intake', 'fake_intake', 'other_kind'),
    [
        pytest.param(
            AlpacaHandle._AlpacaHandle__asset_list_intake,
            fake_ingest._asset_list_intake,
            {'corporate_actions': ACTIONS},
            id='asset-list-given-actions',
        ),
        pytest.param(
            AlpacaHandle._AlpacaHandle__asset_list_intake,
            fake_ingest._asset_list_intake,
            {'bars': BARS},
            id='asset-list-given-bars',
        ),
        pytest.param(
            AlpacaHandle._AlpacaHandle__corporate_actions_intake,
            fake_ingest._corporate_actions_intake,
            {'asset_list': listing(AssetType.STOCK)},
            id='actions-given-asset-list',
        ),
        pytest.param(
            AlpacaHandle._AlpacaHandle__corporate_actions_intake,
            fake_ingest._corporate_actions_intake,
            {'bars': BARS},
            id='actions-given-bars',
        ),
    ],
)


@MISSING_BODY
def test_both_intakes_refuse_a_request_without_their_kinds_body_alike(alpaca_intake, fake_intake, other_kind):
    request = SubmitRequest(request_id='wrong-body', owner='world-manager', **other_kind)

    with pytest.raises(TraderJoeError) as alpaca_refusal:
        alpaca_intake(request)
    with pytest.raises(TraderJoeError) as fake_refusal:
        fake_intake(request)

    assert refusal_view(fake_refusal.value) == refusal_view(alpaca_refusal.value)
    assert fake_refusal.value.detail == alpaca_refusal.value.detail
    # Equal alone would pass if both drifted together; the literal pins the wording on both sides (tj-7zjov1.171).
    assert alpaca_refusal.value.detail == f'invalid {request.kind.value} request: no body for its kind'


@pytest.mark.asyncio
@FEEDS
async def test_both_handles_serve_the_same_request_kinds(monkeypatch, setting, feed):
    # A kind registered on one and not the other is a drift too: one answers, the other refuses it as not served.
    alpaca, fake = both(monkeypatch, setting, feed)
    for n, body in enumerate(
        ({'bars': BARS}, {'asset_list': listing(AssetType.STOCK)}, {'corporate_actions': ACTIONS})
    ):
        request = SubmitRequest(request_id=f'kind-{n}', owner='o', **body)
        alpaca_ack = await alpaca.submit(request)
        fake_ack = await fake.submit(request)
        assert fake_ack == alpaca_ack, f'{request.kind.value}: {fake_ack!r} != {alpaca_ack!r}'
