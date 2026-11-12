"""The internal Submit and Instance contract, message by message: proto <-> domain, both directions (tj-7zjov1.15).

The same rules as fetch_dataset.py: one function per message, no catch-all, field names identical on both
sides.

THE FEED RULE (architect ruling at the P07 gate, tj-7zjov1.13). The resolved feed is a fact about BARS only,
so the SubmitResponse mapper must know the kind of the request it answers:

    bars                          accepted must carry a feed; FEED_UNSPECIFIED is refused, exactly as
                                  accepted_to_domain refuses it. An ack that is refused is fine.
    asset_list, corporate_actions accepted must carry FEED_UNSPECIFIED, which maps to None; any other value
                                  is refused, because a nominal feed would record a false fact. A refused
                                  ack is a protocol error.

The kind is a KEYWORD-ONLY argument, defaulting to BARS (the strict case) so the one-argument shape that
MESSAGE_MAPPERS and the descriptor-driven control call still works. Real callers pass it.

THE TWO DIRECTIONS ENFORCE THE SAME RULE, so a server cannot encode what a client would refuse.

attempt_count IS A uint32. The encoder refuses a count outside 0 .. 2**32 - 1 before building the message,
rather than letting protobuf raise a ValueError of its own.

ready_by IS PRESENCE, NOT A VALUE: None is left unset on the wire and decodes back to None, never the epoch.
"""

from typing import Final

from pydantic import BaseModel, ValidationError

from common.enums.data_stock import Feed
from common.rpc.mapping.fetch_dataset import refused_to_domain, refused_to_proto
from common.rpc.mapping.values import (
    ASSET_TYPE,
    DATA_SOURCE,
    DATA_TYPE,
    FEED,
    GRANULARITY,
    UPDATE_TYPE,
    ProtoMappingError,
    enum_mapping,
    timestamp_field,
    to_timestamp,
)
from schemas.data_ingest import submit as domain
from schemas.data_ingest.submit import MAX_ATTEMPT_COUNT, RequestKind
from trader_joe.proto.internal.ingest.v1 import ingest_pb2


_FEED_UNSPECIFIED: int = 0

# Required on the wire: to_domain refuses the zero, so a request that names no list is refused.
ASSET_LIST_STATUS: Final = enum_mapping(ingest_pb2.AssetListStatus.DESCRIPTOR, domain.AssetListStatus)


def _built[M: BaseModel](model: type[M], **fields: object) -> M:
    # One exception type leaves this module, as in fetch_dataset.py.
    try:
        return model(**fields)
    except ValidationError as error:
        raise ProtoMappingError(f'the wire values do not make a valid {model.__name__}: {error}') from error


def bars_request_to_proto(request: domain.BarsRequest) -> ingest_pb2.BarsRequest:
    """Encode the bars kind.

    Args:
        request: The domain request.

    Returns:
        ingest_pb2.BarsRequest: The request on the wire. An absent end stays unset; an absent feed is the
        FEED_UNSPECIFIED zero.

    Raises:
        ProtoMappingError: If a value cannot be put on the wire.
    """
    message = ingest_pb2.BarsRequest(
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


def bars_request_to_domain(message: ingest_pb2.BarsRequest) -> domain.BarsRequest:
    """Decode the bars kind.

    Args:
        message: The request as it arrived.

    Returns:
        domain.BarsRequest: The domain request. An absent end stays None and FEED_UNSPECIFIED stays None.

    Raises:
        ProtoMappingError: If a required enum is unspecified, start is unset, or the model refuses a value.
    """
    return _built(
        domain.BarsRequest,
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


def asset_list_request_to_proto(request: domain.AssetListRequest) -> ingest_pb2.AssetListRequest:
    """Encode the asset-list kind.

    Args:
        request: The domain request.

    Returns:
        ingest_pb2.AssetListRequest: The request on the wire.

    Raises:
        ProtoMappingError: If a value cannot be put on the wire.
    """
    return ingest_pb2.AssetListRequest(
        source=DATA_SOURCE.to_proto(request.source),
        asset_type=ASSET_TYPE.to_proto(request.asset_type),
        status=ASSET_LIST_STATUS.to_proto(request.status),
    )


def asset_list_request_to_domain(message: ingest_pb2.AssetListRequest) -> domain.AssetListRequest:
    """Decode the asset-list kind.

    Args:
        message: The request as it arrived.

    Returns:
        domain.AssetListRequest: The domain request.

    Raises:
        ProtoMappingError: If an enum is unspecified, the status included.
    """
    return _built(
        domain.AssetListRequest,
        source=DATA_SOURCE.to_domain(message.source),
        asset_type=ASSET_TYPE.to_domain(message.asset_type),
        status=ASSET_LIST_STATUS.to_domain(message.status),
    )


def corporate_actions_request_to_proto(request: domain.CorporateActionsRequest) -> ingest_pb2.CorporateActionsRequest:
    """Encode the corporate-actions kind.

    Args:
        request: The domain request.

    Returns:
        ingest_pb2.CorporateActionsRequest: The request on the wire, both bounds set.

    Raises:
        ProtoMappingError: If a value cannot be put on the wire.
    """
    message = ingest_pb2.CorporateActionsRequest(
        source=DATA_SOURCE.to_proto(request.source), asset_symbol=request.asset_symbol
    )
    message.start.CopyFrom(to_timestamp(request.start))
    message.end.CopyFrom(to_timestamp(request.end))
    return message


def corporate_actions_request_to_domain(message: ingest_pb2.CorporateActionsRequest) -> domain.CorporateActionsRequest:
    """Decode the corporate-actions kind.

    Args:
        message: The request as it arrived.

    Returns:
        domain.CorporateActionsRequest: The domain request.

    Raises:
        ProtoMappingError: If the source is unspecified or either bound is unset.
    """
    return _built(
        domain.CorporateActionsRequest,
        source=DATA_SOURCE.to_domain(message.source),
        asset_symbol=message.asset_symbol,
        start=timestamp_field(message, 'start'),
        end=timestamp_field(message, 'end'),
    )


def submit_request_to_proto(request: domain.SubmitRequest) -> ingest_pb2.SubmitRequest:
    """Encode one Submit request.

    Args:
        request: The domain request.

    Returns:
        ingest_pb2.SubmitRequest: The request on the wire. An absent ready_by is unset, an absent client is
        the empty string.

    Raises:
        ProtoMappingError: If attempt_count is outside the uint32 range (a negative count included), exactly
            one kind is not set, or a value cannot be put on the wire.
    """
    # Checked here as well as on the model: model_construct and attribute assignment bypass validation.
    if not 0 <= request.attempt_count <= MAX_ATTEMPT_COUNT:
        raise ProtoMappingError(
            f'attempt_count {request.attempt_count} is outside 0..{MAX_ATTEMPT_COUNT}; it is a uint32 on the wire'
        )
    message = ingest_pb2.SubmitRequest(
        request_id=request.request_id,
        owner=request.owner,
        client=request.client or '',
        best_effort=request.best_effort,
        recovered=request.recovered,
        attempt_count=request.attempt_count,
    )
    if request.ready_by is not None:
        message.ready_by.CopyFrom(to_timestamp(request.ready_by))
    arms = [arm for arm in (request.bars, request.asset_list, request.corporate_actions) if arm is not None]
    if len(arms) != 1:
        raise ProtoMappingError('a SubmitRequest sets exactly one of bars, asset_list or corporate_actions')
    if request.bars is not None:
        message.bars.CopyFrom(bars_request_to_proto(request.bars))
    elif request.asset_list is not None:
        message.asset_list.CopyFrom(asset_list_request_to_proto(request.asset_list))
    elif request.corporate_actions is not None:
        message.corporate_actions.CopyFrom(corporate_actions_request_to_proto(request.corporate_actions))
    return message


def submit_request_to_domain(message: ingest_pb2.SubmitRequest) -> domain.SubmitRequest:
    """Decode one Submit request.

    Args:
        message: The request as it arrived.

    Returns:
        domain.SubmitRequest: The domain request. An unset ready_by is None, never the epoch; an empty
        client is None.

    Raises:
        ProtoMappingError: If no kind is set, an arm cannot be mapped, or the model refuses a value (an
            empty request_id included).
    """
    arm = message.WhichOneof('kind')
    if arm is None:
        raise ProtoMappingError('a SubmitRequest arrived with no kind set; its oneof holds one arm')
    return _built(
        domain.SubmitRequest,
        request_id=message.request_id,
        owner=message.owner,
        client=message.client or None,
        ready_by=timestamp_field(message, 'ready_by') if message.HasField('ready_by') else None,
        best_effort=message.best_effort,
        recovered=message.recovered,
        attempt_count=message.attempt_count,
        bars=bars_request_to_domain(message.bars) if arm == 'bars' else None,
        asset_list=asset_list_request_to_domain(message.asset_list) if arm == 'asset_list' else None,
        corporate_actions=(
            corporate_actions_request_to_domain(message.corporate_actions) if arm == 'corporate_actions' else None
        ),
    )


def _feed_to_domain(message: ingest_pb2.FetchAccepted, kind: RequestKind) -> Feed | None:
    if kind is RequestKind.BARS:
        return FEED.to_domain(message.feed)
    if message.feed != _FEED_UNSPECIFIED:
        raise ProtoMappingError(
            f'a {kind.value} Submit was accepted with a feed ({message.feed}); only bars have a tape, and a '
            'nominal feed would record a false fact'
        )
    return None


def submit_response_to_domain(
    message: ingest_pb2.SubmitResponse, *, kind: RequestKind = RequestKind.BARS
) -> domain.SubmitResponse:
    """Decode the answer to a Submit, knowing which kind of request it answers.

    Args:
        message: The answer as it arrived.
        kind: The kind of the request this answers. Defaults to BARS, the strict case.

    Returns:
        domain.SubmitResponse: The domain answer. Accepted carries the feed for bars and None for the others.

    Raises:
        ProtoMappingError: If no ack arm is set; for bars, if the accepted feed is FEED_UNSPECIFIED; for
            asset_list or corporate_actions, if the accepted feed is anything but FEED_UNSPECIFIED or if the
            ack is refused; or if the model refuses a value (an empty instance_id included).
    """
    arm = message.ack.WhichOneof('outcome')
    if arm == 'accepted':
        ack = _built(
            domain.SubmitAck,
            accepted=_built(
                domain.SubmitAccepted,
                feed=_feed_to_domain(message.ack.accepted, kind),
                quota_key=message.ack.accepted.quota_key,
            ),
        )
    elif arm == 'refused':
        if kind is not RequestKind.BARS:
            raise ProtoMappingError(f'a {kind.value} Submit was refused in its ack; only bars can be refused there')
        ack = _built(domain.SubmitAck, refused=refused_to_domain(message.ack.refused))
    else:
        raise ProtoMappingError('a SubmitResponse arrived with neither accepted nor refused set')
    return _built(domain.SubmitResponse, ack=ack, instance_id=message.instance_id)


def submit_response_to_proto(
    response: domain.SubmitResponse, *, kind: RequestKind = RequestKind.BARS
) -> ingest_pb2.SubmitResponse:
    """Encode the answer to a Submit, under the same feed rule the decoder enforces.

    Args:
        response: The domain answer.
        kind: The kind of the request this answers. Defaults to BARS.

    Returns:
        ingest_pb2.SubmitResponse: The answer on the wire.

    Raises:
        ProtoMappingError: If the feed or the refusal disagrees with the kind, or no arm is set.
    """
    accepted, refused = response.ack.accepted, response.ack.refused
    if accepted is not None:
        if kind is RequestKind.BARS:
            if accepted.feed is None:
                raise ProtoMappingError('a bars Submit is accepted with its resolved feed, and this has none')
            feed = FEED.to_proto(accepted.feed)
        else:
            if accepted.feed is not None:
                raise ProtoMappingError(f'a {kind.value} Submit has no tape, so its accepted ack carries no feed')
            feed = _FEED_UNSPECIFIED
        ack = ingest_pb2.FetchAck(accepted=ingest_pb2.FetchAccepted(feed=feed, quota_key=accepted.quota_key))
    elif refused is not None:
        if kind is not RequestKind.BARS:
            raise ProtoMappingError(f'a {kind.value} Submit cannot be refused in its ack; only bars can be')
        ack = ingest_pb2.FetchAck(refused=refused_to_proto(refused))
    else:
        raise ProtoMappingError('a SubmitResponse sets exactly one of accepted or refused, and this sets neither')
    return ingest_pb2.SubmitResponse(ack=ack, instance_id=response.instance_id)


def instance_request_to_proto(request: domain.InstanceRequest) -> ingest_pb2.InstanceRequest:
    """Encode the Instance probe, which carries nothing.

    Args:
        request: The domain probe.

    Returns:
        ingest_pb2.InstanceRequest: The empty message.
    """
    return ingest_pb2.InstanceRequest()


def instance_request_to_domain(message: ingest_pb2.InstanceRequest) -> domain.InstanceRequest:
    """Decode the Instance probe.

    Args:
        message: The probe as it arrived.

    Returns:
        domain.InstanceRequest: The empty probe.
    """
    return domain.InstanceRequest()


def instance_response_to_proto(response: domain.InstanceResponse) -> ingest_pb2.InstanceResponse:
    """Encode the Instance answer.

    Args:
        response: The domain answer.

    Returns:
        ingest_pb2.InstanceResponse: The answer on the wire.

    Raises:
        ProtoMappingError: If started_at is naive.
    """
    message = ingest_pb2.InstanceResponse(instance_id=response.instance_id)
    message.started_at.CopyFrom(to_timestamp(response.started_at))
    return message


def instance_response_to_domain(message: ingest_pb2.InstanceResponse) -> domain.InstanceResponse:
    """Decode the Instance answer.

    Args:
        message: The answer as it arrived.

    Returns:
        domain.InstanceResponse: The domain answer, started_at aware UTC.

    Raises:
        ProtoMappingError: If started_at is unset or the instance_id is empty.
    """
    return _built(
        domain.InstanceResponse, instance_id=message.instance_id, started_at=timestamp_field(message, 'started_at')
    )
