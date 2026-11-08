"""The domain side of one Submit of each kind, and its answers on and off the wire (tj-7zjov1.15).

Shared by test_rpc_submit_mapping.py and test_rpc_submit_client.py so every test states only what it varies.
Raw wire answers are built from the contract's descriptors (``fetch_contract.message_class``), so a value the
production encoder would never write -- the zero feed on bars, a feed on asset_list -- is still reachable.
"""

from datetime import UTC, datetime, timedelta

from google.protobuf.message import Message

from common.enums.data_stock import Feed
from common.rpc.mapping import bars_request_to_proto, submit_response_to_proto
from common.tests.rpc.fetch_contract import message_class
from schemas.data_ingest import submit as domain
from schemas.data_ingest.fetch_dataset import FetchRefused
from schemas.data_ingest.submit import RequestKind


WHEN = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)

SubmitResponseMessage = message_class('SubmitResponse')

NON_BARS = [RequestKind.ASSET_LIST, RequestKind.CORPORATE_ACTIONS]


def a_bars(**overrides: object) -> domain.BarsRequest:
    """One ordinary bars kind.

    Args:
        **overrides: Fields to replace.

    Returns:
        domain.BarsRequest: The kind.
    """
    return domain.BarsRequest(
        **{
            'source': 'ALPACA',
            'asset_symbol': 'VFV',
            'asset_type': 'stock',
            'data_types': ['market-activity'],
            'granularity': '1day',
            'start': WHEN,
            'end': WHEN + timedelta(days=5),
            'update_type': 2,
            'feed': Feed.IEX,
            **overrides,
        }
    )


def an_asset_list() -> domain.AssetListRequest:
    """Returns: domain.AssetListRequest: The asset-list kind."""
    return domain.AssetListRequest(source='ALPACA', asset_type='stock')


def a_corporate_actions() -> domain.CorporateActionsRequest:
    """Returns: domain.CorporateActionsRequest: The corporate-actions kind over an explicit window."""
    return domain.CorporateActionsRequest(
        source='ALPACA', asset_symbol='VFV', start=WHEN - timedelta(days=90), end=WHEN + timedelta(days=90)
    )


#: The arm fields for a request of each kind, for ``a_submit(**KINDS[kind])``.
KINDS: dict[RequestKind, dict[str, object]] = {
    RequestKind.BARS: {'bars': a_bars()},
    RequestKind.ASSET_LIST: {'bars': None, 'asset_list': an_asset_list()},
    RequestKind.CORPORATE_ACTIONS: {'bars': None, 'corporate_actions': a_corporate_actions()},
}


def a_submit(**overrides: object) -> domain.SubmitRequest:
    """One Submit with every envelope field set off its default, carrying the bars kind unless overridden.

    Args:
        **overrides: Fields to replace.

    Returns:
        domain.SubmitRequest: The request.
    """
    fields: dict[str, object] = {
        'request_id': 'req-7',
        'owner': 'rebalancer',
        'client': 'world-manager',
        'ready_by': WHEN + timedelta(hours=3),
        'best_effort': True,
        'recovered': True,
        'attempt_count': 4,
        'bars': a_bars(),
    }
    fields.update(overrides)
    return domain.SubmitRequest(**fields)


def feed_number(feed: Feed) -> int:
    """The proto number of a real tape, read through the production encoder rather than hard-coded.

    Args:
        feed: The tape.

    Returns:
        int: Its Feed enum number on the wire.
    """
    return bars_request_to_proto(a_bars(feed=feed)).feed


def accepted(feed: Feed | None, quota_key: str = 'alpaca-a') -> domain.SubmitResponse:
    """An accepted answer in domain terms.

    Args:
        feed: The resolved feed, or None.
        quota_key: The quota scope.

    Returns:
        domain.SubmitResponse: Accepted, from instance ingest-1.
    """
    return domain.SubmitResponse(
        ack=domain.SubmitAck(accepted=domain.SubmitAccepted(feed=feed, quota_key=quota_key)), instance_id='ingest-1'
    )


def refused() -> domain.SubmitResponse:
    """Returns: domain.SubmitResponse: Refused for its feed, as only bars can be, from instance ingest-1."""
    return domain.SubmitResponse(
        ack=domain.SubmitAck(
            refused=FetchRefused(
                reason='FEED_NOT_AVAILABLE',
                domain='trader-joe',
                detail='this deployment serves IEX only',
                metadata={'feed': 'SIP'},
            )
        ),
        instance_id='ingest-1',
    )


def raw_accepted(feed: int) -> Message:
    """An accepted answer straight off the wire.

    Args:
        feed: The proto number of FetchAccepted.feed; 0 is FEED_UNSPECIFIED.

    Returns:
        Message: The SubmitResponse, quota key alpaca-a, instance ingest-1.
    """
    message = SubmitResponseMessage(instance_id='ingest-1')
    message.ack.accepted.quota_key = 'alpaca-a'
    message.ack.accepted.feed = feed
    return message


def raw_refused() -> Message:
    """Returns: Message: A refused SubmitResponse off the wire, built by the production bars encoder."""
    return submit_response_to_proto(refused(), kind=RequestKind.BARS)
