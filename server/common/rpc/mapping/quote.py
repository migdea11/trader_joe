"""The internal Quote call, message by message: proto <-> domain, both directions (tj-grna9p.44).

The same rules as fetch_dataset.py: one function per message, no catch-all, field names identical on both
sides. QuoteRequest has NO domain twin on purpose -- it wraps the one FetchDatasetRequest -- so what is here
is a builder for it from the domain fetch request, and it is named in ENVELOPE_MESSAGES.
"""

from pydantic import BaseModel, ValidationError

from common.rpc.mapping.fetch_dataset import request_to_proto
from common.rpc.mapping.values import ProtoMappingError, timestamp_field, to_timestamp
from schemas.data_ingest import fetch_dataset as fetch_domain
from schemas.data_ingest import quote as domain
from trader_joe.proto.internal.ingest.v1 import ingest_pb2


def _built[M: BaseModel](model: type[M], **fields: object) -> M:
    # One exception type leaves this module, as in fetch_dataset.py.
    try:
        return model(**fields)
    except ValidationError as error:
        raise ProtoMappingError(f'the wire values do not make a valid {model.__name__}: {error}') from error


def quote_request_to_proto(request: fetch_domain.FetchDatasetRequest) -> ingest_pb2.QuoteRequest:
    """Encode a quote request: the one fetch it asks about.

    Args:
        request: The fetch to quote.

    Returns:
        ingest_pb2.QuoteRequest: The request on the wire.

    Raises:
        ProtoMappingError: If the fetch cannot be put on the wire.
    """
    return ingest_pb2.QuoteRequest(fetch=request_to_proto(request))


def scope_capacity_to_proto(scope: domain.ScopeCapacity) -> ingest_pb2.ScopeCapacity:
    """Encode one scope's capacity.

    Args:
        scope: The domain capacity.

    Returns:
        ingest_pb2.ScopeCapacity: The capacity on the wire. An absent reset_at stays unset.

    Raises:
        ProtoMappingError: If a value cannot be put on the wire.
    """
    message = ingest_pb2.ScopeCapacity(
        quota_key=scope.quota_key, cap=scope.cap, remaining=scope.remaining, window_seconds=scope.window_seconds
    )
    if scope.reset_at is not None:
        message.reset_at.CopyFrom(to_timestamp(scope.reset_at))
    return message


def scope_capacity_to_domain(message: ingest_pb2.ScopeCapacity) -> domain.ScopeCapacity:
    """Decode one scope's capacity.

    Args:
        message: The capacity as it arrived.

    Returns:
        domain.ScopeCapacity: The domain capacity. An unset reset_at stays None rather than the epoch.

    Raises:
        ProtoMappingError: If the domain model refuses the values.
    """
    return _built(
        domain.ScopeCapacity,
        quota_key=message.quota_key,
        cap=message.cap,
        remaining=message.remaining,
        window_seconds=message.window_seconds,
        reset_at=timestamp_field(message, 'reset_at') if message.HasField('reset_at') else None,
    )


def quote_to_proto(result: domain.QuoteResult) -> ingest_pb2.QuoteResponse:
    """Encode a quote answer.

    Args:
        result: The domain answer.

    Returns:
        ingest_pb2.QuoteResponse: The answer on the wire.

    Raises:
        ProtoMappingError: If a scope cannot be put on the wire.
    """
    return ingest_pb2.QuoteResponse(
        estimated_vendor_calls=result.estimated_vendor_calls,
        quota_key=result.quota_key,
        scopes=[scope_capacity_to_proto(scope) for scope in result.scopes],
    )


def quote_to_domain(message: ingest_pb2.QuoteResponse) -> domain.QuoteResult:
    """Decode a quote answer.

    Args:
        message: The answer as it arrived.

    Returns:
        domain.QuoteResult: The domain answer.

    Raises:
        ProtoMappingError: If a scope cannot be mapped or the domain model refuses the values.
    """
    return _built(
        domain.QuoteResult,
        estimated_vendor_calls=message.estimated_vendor_calls,
        quota_key=message.quota_key,
        scopes=[scope_capacity_to_domain(scope) for scope in message.scopes],
    )
