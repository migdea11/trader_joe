"""THE INGEST CONTRACT'S MESSAGES for tests, reached through the DESCRIPTORS and not a generated module.

WHY DESCRIPTORS. TID251 bans ``trader_joe.proto`` everywhere but ``common/rpc``, and a test is not that seam
(architect, 05:11 UTC 2026-10-02): no per-file-ignore and no noqa is granted here. So nothing in this package
may import ``ingest_pb2`` or ``ingest_pb2_grpc``. Every message below is reached instead through the public
surface ``common.rpc.mapping`` exposes for exactly this purpose -- ``CONTRACT_FILES``, the FileDescriptors --
plus ``google.protobuf.message_factory``, which turns a descriptor into the message class protoc already
registered, so a rename on the .proto reaches these tests as a failure rather than as a stale constant.

WHAT IS LEFT. The module kept its name when the PR 4 FetchDataset pull stream was deleted (P37, tj-7zjov1.280):
FetchDatasetRequest itself survives as Quote's argument, and the Submit, Quote and delivery tests share these
helpers. The stream's peer server, its response envelope and the done event went with it.

Every server a test builds binds 127.0.0.1 on an ephemeral port (``LOOPBACK``) and bounds its awaits by
``GUARD_S``, so a hang fails one test instead of the run.
"""

from datetime import UTC, datetime, timedelta

from google.protobuf import message_factory
from google.protobuf.message import Message

from common.enums.data_stock import Feed
from common.rpc.mapping import CONTRACT_FILES
from schemas.data_ingest import fetch_dataset as domain


LOOPBACK = '127.0.0.1'

# How long any network await in these tests may take before the test fails instead of hanging.
GUARD_S = 5.0


def message_class(name: str) -> type[Message]:
    """One contract message class by its SIMPLE name, resolved through the descriptors.

    Args:
        name: e.g. ``'FetchAccepted'``, ``'Bar'``, ``'SubmitResponse'``.

    Returns:
        type[Message]: The generated class, from protoc's own registry.

    Raises:
        AssertionError: If no contract file declares a message by that name, which a rename causes.
    """
    for file in CONTRACT_FILES:
        descriptor = file.message_types_by_name.get(name)
        if descriptor is not None:
            return message_factory.GetMessageClass(descriptor)
    declared = sorted(name for file in CONTRACT_FILES for name in file.message_types_by_name)
    raise AssertionError(f'no contract message is named {name!r}; the contract declares {declared}')


# ---------------------------------------------------------------------------------------------
# THE DOMAIN SIDE OF ONE ORDINARY REQUEST, so every test states only what it varies.

#: The tape these tests resolve. A real member, never a sentinel: Feed has no "we do not know" member by
#: design (tj-vhboky.1).
ACK_FEED = Feed.IEX

WHEN = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)


def a_request(**overrides: object) -> domain.FetchDatasetRequest:
    """One ordinary fetch request (Quote's argument).

    Args:
        **overrides: Fields to replace.

    Returns:
        domain.FetchDatasetRequest: The request.
    """
    return domain.FetchDatasetRequest(
        **{
            'owner': 'rebalancer',
            'source': 'ALPACA',
            'asset_symbol': 'VFV',
            'asset_type': 'stock',
            'data_types': ['market-activity'],
            'granularity': '1min',
            'start': WHEN,
            'end': WHEN + timedelta(hours=1),
            'update_type': 2,
            'feed': None,
            **overrides,
        }
    )


def a_bar(*, minute: int = 0, feed: Feed = ACK_FEED) -> domain.Bar:
    """One bar, distinct per minute so a page's bars are never interchangeable.

    Args:
        minute: How far into the window the bar opens.
        feed: The tape the bar carries.

    Returns:
        domain.Bar: The bar.
    """
    return domain.Bar(
        bar_start=WHEN + timedelta(minutes=minute),
        open=100.0 + minute,
        high=101.0 + minute,
        low=99.0 + minute,
        close=100.5 + minute,
        volume=1000.0 + minute,
        trade_count=10 + minute,
        vwap=100.25 + minute,
        feed=feed,
    )
