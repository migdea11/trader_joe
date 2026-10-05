"""The internal FetchDataset contract, message by message: proto <-> domain, both directions.

ONE FUNCTION PER MESSAGE, AND NO CATCH-ALL. ADR tj-8konfu D1 keeps proto/trader_joe/proto/... and
schemas/data_ingest/fetch_dataset.py as two source-of-truth artefacts with this mapping written by hand
between them. A loop over DESCRIPTOR fields would make that mapping total by construction and useless as a
review surface: the point of writing it out is that adding a field to the .proto and forgetting it here is
VISIBLE -- and tj-3mk3u5.29's descriptor-driven control turns that from visible into red.

WHAT IS MAPPED AND WHAT IS NOT. Every message of the contract has a pair of functions here, except
FetchDatasetResponse: the stream's envelope is pure transport, it has no domain twin by design (the domain
module says so), and the oneof arm is dispatched by the client seam and by the server-side encoder. The
module-level ENVELOPE_MESSAGES in this package's __init__ records that exclusion so a control can assert
the table is otherwise total.

FIELD NAMES ARE IDENTICAL ON BOTH SIDES, deliberately, so there is no name table to keep. The domain
module's own docstring commits to that: a field renamed to read better in Python would cost an entry in a
translation table that does not exist today.

THE STREAM-LEVEL RULES ARE NOT HERE. A Bar maps its own feed faithfully in both directions, because
market/v1 is shared vocabulary and a bar reaching the UI or an external stream has no fetch envelope to read
a feed from. That every bar of ONE FETCH carries the ack's resolved feed is a rule about the stream, not
about the message, and it lives one layer up: on the encode side in fetch_stream.py, on the decode side in
common/rpc/clients/ingest_fetch.py.
"""

from pydantic import BaseModel, ValidationError

from common.rpc.mapping.values import (
    ASSET_TYPE,
    DATA_SOURCE,
    DATA_TYPE,
    FEED,
    GRANULARITY,
    UPDATE_TYPE,
    ProtoMappingError,
    timestamp_field,
    to_timestamp,
)
from schemas.data_ingest import fetch_dataset as domain
from trader_joe.proto.internal.ingest.v1 import ingest_pb2
from trader_joe.proto.market.v1 import bar_pb2


# FetchDatasetRequest.feed is the one field on this contract whose zero is meaningful: there is deliberately
# no `optional` keyword on it, so "unset" and "FEED_UNSPECIFIED" are one state, and that state means "the
# deployment decides". Every other enum field is required and refuses the zero.
_FEED_UNSPECIFIED: int = 0


def _built[M: BaseModel](model: type[M], **fields: object) -> M:
    # One exception type leaves this module. A domain model refusing what the wire carried -- a naive
    # datetime, a negative bar_count, an ack with both oneof arms -- is the same event as an unmappable
    # enum, and the seam above should not have to catch two unrelated types to say so.
    try:
        return model(**fields)
    except ValidationError as error:
        raise ProtoMappingError(f'the wire values do not make a valid {model.__name__}: {error}') from error


def request_to_proto(request: domain.FetchDatasetRequest) -> ingest_pb2.FetchDatasetRequest:
    """Encode the fetch request.

    Args:
        request: The domain request.

    Returns:
        ingest_pb2.FetchDatasetRequest: The same request on the wire. An absent end stays absent, and an
        absent feed is the FEED_UNSPECIFIED zero, which means "the deployment decides".

    Raises:
        ProtoMappingError: If a value cannot be put on the wire.
    """
    message = ingest_pb2.FetchDatasetRequest(
        owner=request.owner,
        source=DATA_SOURCE.to_proto(request.source),
        asset_symbol=request.asset_symbol,
        asset_type=ASSET_TYPE.to_proto(request.asset_type),
        data_types=[DATA_TYPE.to_proto(data_type) for data_type in request.data_types],
        granularity=GRANULARITY.to_proto(request.granularity),
        update_type=UPDATE_TYPE.to_proto(request.update_type),
    )
    message.start.CopyFrom(to_timestamp(request.start))
    if request.end is not None:
        message.end.CopyFrom(to_timestamp(request.end))
    if request.feed is not None:
        message.feed = FEED.to_proto(request.feed)
    return message


def request_to_domain(message: ingest_pb2.FetchDatasetRequest) -> domain.FetchDatasetRequest:
    """Decode the fetch request.

    Args:
        message: The request as it arrived.

    Returns:
        domain.FetchDatasetRequest: The domain request. An absent end stays None rather than becoming the
        epoch, and FEED_UNSPECIFIED stays None rather than being defaulted to a tape.

    Raises:
        ProtoMappingError: If the message cannot be mapped -- an unspecified required enum, an unset start,
            or values the domain model refuses.
    """
    return _built(
        domain.FetchDatasetRequest,
        owner=message.owner,
        source=DATA_SOURCE.to_domain(message.source),
        asset_symbol=message.asset_symbol,
        asset_type=ASSET_TYPE.to_domain(message.asset_type),
        data_types=[DATA_TYPE.to_domain(data_type) for data_type in message.data_types],
        granularity=GRANULARITY.to_domain(message.granularity),
        start=timestamp_field(message, 'start'),
        end=timestamp_field(message, 'end') if message.HasField('end') else None,
        update_type=UPDATE_TYPE.to_domain(message.update_type),
        feed=None if message.feed == _FEED_UNSPECIFIED else FEED.to_domain(message.feed),
    )


def bar_to_proto(bar: domain.Bar) -> bar_pb2.Bar:
    """Encode one bar.

    Args:
        bar: The domain bar, carrying its own feed.

    Returns:
        bar_pb2.Bar: The bar on the wire. trade_count and vwap stay absent when the vendor reported
        neither; proto3 `optional` gives them real presence, so absence is not zero.

    Raises:
        ProtoMappingError: If a value cannot be put on the wire, including a naive bar_start.
    """
    message = bar_pb2.Bar(
        open=bar.open, high=bar.high, low=bar.low, close=bar.close, volume=bar.volume, feed=FEED.to_proto(bar.feed)
    )
    message.bar_start.CopyFrom(to_timestamp(bar.bar_start))
    if bar.trade_count is not None:
        message.trade_count = bar.trade_count
    if bar.vwap is not None:
        message.vwap = bar.vwap
    return message


def bar_to_domain(message: bar_pb2.Bar) -> domain.Bar:
    """Decode one bar.

    Args:
        message: The bar as it arrived.

    Returns:
        domain.Bar: The domain bar, its bar_start aware UTC and its absent optionals still None.

    Raises:
        ProtoMappingError: If the message cannot be mapped, including a FEED_UNSPECIFIED feed -- a bar whose
            tape nobody resolved is an error, not a value (tj-vhboky.1).
    """
    return _built(
        domain.Bar,
        bar_start=timestamp_field(message, 'bar_start'),
        open=message.open,
        high=message.high,
        low=message.low,
        close=message.close,
        volume=message.volume,
        trade_count=message.trade_count if message.HasField('trade_count') else None,
        vwap=message.vwap if message.HasField('vwap') else None,
        feed=FEED.to_domain(message.feed),
    )


def page_to_proto(page: domain.BarPage) -> ingest_pb2.BarPage:
    """Encode one page of bars.

    Args:
        page: The domain page.

    Returns:
        ingest_pb2.BarPage: The page on the wire.

    Raises:
        ProtoMappingError: If a bar cannot be put on the wire.
    """
    return ingest_pb2.BarPage(bars=[bar_to_proto(bar) for bar in page.bars])


def page_to_domain(message: ingest_pb2.BarPage) -> domain.BarPage:
    """Decode one page of bars.

    Args:
        message: The page as it arrived.

    Returns:
        domain.BarPage: The domain page.

    Raises:
        ProtoMappingError: If a bar cannot be mapped.
    """
    return _built(domain.BarPage, bars=[bar_to_domain(bar) for bar in message.bars])


def accepted_to_proto(accepted: domain.FetchAccepted) -> ingest_pb2.FetchAccepted:
    """Encode the accepted arm of the ack: the resolved feed.

    Args:
        accepted: The domain acceptance.

    Returns:
        ingest_pb2.FetchAccepted: The acceptance on the wire.

    Raises:
        ProtoMappingError: If the feed cannot be put on the wire.
    """
    return ingest_pb2.FetchAccepted(feed=FEED.to_proto(accepted.feed))


def accepted_to_domain(message: ingest_pb2.FetchAccepted) -> domain.FetchAccepted:
    """Decode the accepted arm of the ack.

    Args:
        message: The acceptance as it arrived.

    Returns:
        domain.FetchAccepted: The domain acceptance.

    Raises:
        ProtoMappingError: If the feed is FEED_UNSPECIFIED -- the resolved feed is the one value only ingest
            may decide, and a feed nobody resolved is an error (tj-vhboky.1).
    """
    return _built(domain.FetchAccepted, feed=FEED.to_domain(message.feed))


def refused_to_proto(refused: domain.FetchRefused) -> ingest_pb2.FetchRefused:
    """Encode the refused arm of the ack, which mirrors google.rpc.ErrorInfo.

    Args:
        refused: The domain refusal.

    Returns:
        ingest_pb2.FetchRefused: The refusal on the wire.
    """
    return ingest_pb2.FetchRefused(
        reason=refused.reason, domain=refused.domain, detail=refused.detail, metadata=dict(refused.metadata)
    )


def refused_to_domain(message: ingest_pb2.FetchRefused) -> domain.FetchRefused:
    """Decode the refused arm of the ack.

    The reason stays a STRING here and is not looked up in the Reason vocabulary: a client must survive a
    reason it has never heard of, so recognising it is the seam's job, one layer up, where an unrecognised
    one becomes PEER_PROTOCOL_ERROR instead of a message that would not decode.

    Args:
        message: The refusal as it arrived.

    Returns:
        domain.FetchRefused: The domain refusal.

    Raises:
        ProtoMappingError: If the domain model refuses the values.
    """
    return _built(
        domain.FetchRefused,
        reason=message.reason,
        domain=message.domain,
        detail=message.detail,
        metadata=dict(message.metadata),
    )


def ack_to_proto(ack: domain.FetchAck) -> ingest_pb2.FetchAck:
    """Encode the ack, whose oneof holds exactly one arm.

    Args:
        ack: The domain ack. Its own validator has already refused both arms or neither.

    Returns:
        ingest_pb2.FetchAck: The ack on the wire.

    Raises:
        ProtoMappingError: If an arm cannot be put on the wire.
    """
    if ack.accepted is not None:
        return ingest_pb2.FetchAck(accepted=accepted_to_proto(ack.accepted))
    if ack.refused is not None:
        return ingest_pb2.FetchAck(refused=refused_to_proto(ack.refused))
    raise ProtoMappingError('a FetchAck sets exactly one of accepted or refused, and this one sets neither')


def ack_to_domain(message: ingest_pb2.FetchAck) -> domain.FetchAck:
    """Decode the ack.

    Args:
        message: The ack as it arrived.

    Returns:
        domain.FetchAck: The domain ack, exactly one arm set.

    Raises:
        ProtoMappingError: If no arm is set -- a oneof with nothing in it is a message this release cannot
            read, not an empty ack -- or if the arm cannot be mapped.
    """
    arm = message.WhichOneof('outcome')
    if arm == 'accepted':
        return _built(domain.FetchAck, accepted=accepted_to_domain(message.accepted))
    if arm == 'refused':
        return _built(domain.FetchAck, refused=refused_to_domain(message.refused))
    raise ProtoMappingError('a FetchAck arrived with neither accepted nor refused set; its oneof holds one arm')


def served_range_to_proto(served_range: domain.ServedRange) -> ingest_pb2.ServedRange:
    """Encode the served window.

    Args:
        served_range: The domain window.

    Returns:
        ingest_pb2.ServedRange: The window on the wire.

    Raises:
        ProtoMappingError: If either bound cannot be put on the wire.
    """
    message = ingest_pb2.ServedRange()
    message.start.CopyFrom(to_timestamp(served_range.start))
    message.end.CopyFrom(to_timestamp(served_range.end))
    return message


def served_range_to_domain(message: ingest_pb2.ServedRange) -> domain.ServedRange:
    """Decode the served window.

    Args:
        message: The window as it arrived.

    Returns:
        domain.ServedRange: The domain window, both bounds aware UTC.

    Raises:
        ProtoMappingError: If either bound is unset -- the served window is never open -- or unmappable.
    """
    return _built(domain.ServedRange, start=timestamp_field(message, 'start'), end=timestamp_field(message, 'end'))


def done_to_proto(done: domain.FetchDone) -> ingest_pb2.FetchDone:
    """Encode the terminator of an accepted stream, with its served provenance.

    Args:
        done: The domain terminator.

    Returns:
        ingest_pb2.FetchDone: The terminator on the wire.

    Raises:
        ProtoMappingError: If a value cannot be put on the wire.
    """
    message = ingest_pb2.FetchDone(bar_count=done.bar_count, served_range=served_range_to_proto(done.served_range))
    message.as_of.CopyFrom(to_timestamp(done.as_of))
    return message


def done_to_domain(message: ingest_pb2.FetchDone) -> domain.FetchDone:
    """Decode the terminator of an accepted stream.

    Args:
        message: The terminator as it arrived.

    Returns:
        domain.FetchDone: The domain terminator.

    Raises:
        ProtoMappingError: If served_range or as_of is unset, or a value cannot be mapped. A served window
            is what ADR tj-fa1rpu D2's provenance IS, so a done without one is not a done.
    """
    if not message.HasField('served_range'):
        raise ProtoMappingError(
            'a FetchDone arrived with no served_range; the window actually served is the provenance the '
            'message exists to carry (ADR tj-fa1rpu D2)'
        )
    return _built(
        domain.FetchDone,
        bar_count=message.bar_count,
        served_range=served_range_to_domain(message.served_range),
        as_of=timestamp_field(message, 'as_of'),
    )
