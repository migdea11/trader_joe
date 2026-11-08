"""The internal result-path contract, message by message: proto <-> domain, both directions (tj-7zjov1.16).

The same rules as fetch_dataset.py and submit.py: one function per message, no catch-all, field names
identical on both sides. The contract is store.proto (DeliverAttempt and RegisterInstance); data_store
serves it and data_ingest calls it.

THE ONE MESSAGE WITHOUT A TWIN is DeliverAttemptRequest, the stream's envelope (see ENVELOPE_MESSAGES in
this package's __init__). What replaces it is the pair at the bottom of this file: attempt_to_messages
splits one AttemptDelivery into the stream's messages, and AttemptAssembler reads them back and enforces
the stream's grammar.

THE STREAM'S GRAMMAR (store.proto): ONE header, then zero or more payload messages all of one kind, then
ONE trailer. A stream that ends without a trailer is incomplete and the store writes nothing for it.

SPLITTING A LIST ACROSS MESSAGES. An asset list is thousands of records, and the wire guard is 4 MiB per
message (common/rpc/config.py, MAX_MESSAGE_BYTES) while ADR tj-8konfu D6.3 targets about 1 MiB for a bulk
chunk. Bars are bounded as BarPage is (MAX_PAGE_BARS). Asset and corporate-action records are bounded two
ways, whichever bites first:
    MAX_RECORDS_PER_MESSAGE  1000. A count cap on its own cannot bound bytes, because raw_payload is the
                             vendor's record and its size is the vendor's. 1000 is a round number that keeps
                             a typical vendor record (a few hundred bytes to about 1 KiB of JSON) at or
                             under the byte budget, so the byte budget is the guard and not the usual cause.
    MAX_PART_BYTES           1 MiB (D6.3's target) of serialized records. The guarantee: however large the
                             vendor's payloads are, a message stays a quarter of the guard.
A single record larger than the guard cannot be split and is refused as a ProtoMappingError (an ingest
defect, never a silent drop). An empty list is sent as NO payload message at all, so an attempt with no
records is the header and the trailer, and the trailer says whether it succeeded.

THE ASSET FIELDS active AND tradable ARE PRESENCE: unset on the wire is None here, never False. The vendor
asset id is carried as an opaque string and never parsed.
"""

from collections.abc import Iterator, Sequence
from datetime import datetime
from typing import Final

from google.protobuf.message import Message
from pydantic import BaseModel, ValidationError

from common.rpc.config import MAX_MESSAGE_BYTES
from common.rpc.mapping.fetch_dataset import bar_to_domain, bar_to_proto
from common.rpc.mapping.values import FEED, ProtoMappingError, enum_mapping, timestamp_field, to_timestamp
from schemas.data_ingest import delivery as domain
from schemas.data_ingest.fetch_dataset import BarPage, ServedRange
from trader_joe.proto.internal.store.v1 import store_pb2


# THE PAGE SIZE, which the wire cannot express and the server must honour (store.proto, BarPage). The number
# lives in a .proto comment, where no Python caller can reach it, so whoever chunks bars reads it from here
# rather than writing 5000 again. MEASURED: a full page is 345000 bytes, 337 KiB, and 385000 bytes with every
# optional field populated -- about a third of the ~1 MiB ADR tj-8konfu D6.3 targets for a bulk chunk and
# 9.2 % of the 4 MiB ceiling in common/rpc/config.py. Moved here from fetch_stream.py (tj-7zjov1.274) when the
# FetchDataset stream was deleted (P37).
MAX_PAGE_BARS: Final = 5000

DELIVERY_OUTCOME: Final = enum_mapping(store_pb2.DeliveryOutcome.DESCRIPTOR, domain.DeliveryOutcome)
DELIVERY_ERROR_CLASS: Final = enum_mapping(store_pb2.DeliveryErrorClass.DESCRIPTOR, domain.DeliveryErrorClass)
DELIVERY_VERDICT: Final = enum_mapping(store_pb2.DeliveryVerdict.DESCRIPTOR, domain.DeliveryVerdict)

MAX_RECORDS_PER_MESSAGE: Final = 1000
MAX_PART_BYTES: Final = 1024 * 1024

# The per-record framing a repeated message field adds: one tag byte and a length varint (at most 5 bytes
# for a length under 4 GiB, so 6 in all). Counted so the budget bounds the MESSAGE, not just the records.
_RECORD_OVERHEAD_BYTES: Final = 6

_FEED_UNSPECIFIED: Final = 0


def _built[M: BaseModel](model: type[M], **fields: object) -> M:
    # One exception type leaves this module, as in fetch_dataset.py.
    try:
        return model(**fields)
    except ValidationError as error:
        raise ProtoMappingError(f'the wire values do not make a valid {model.__name__}: {error}') from error


def _optional_instant(message: Message, field: str) -> datetime | None:
    return timestamp_field(message, field) if message.HasField(field) else None


def _set_optional_instant(message: Message, field: str, value: datetime | None) -> None:
    if value is not None:
        getattr(message, field).CopyFrom(to_timestamp(value))


def _checked_uint(name: str, value: int, maximum: int) -> int:
    # Refused before the message is built, rather than letting protobuf raise a ValueError of its own.
    if not 0 <= value <= maximum:
        raise ProtoMappingError(f'{name} {value} is outside 0..{maximum}; it is unsigned on the wire')
    return value


# ---------------------------------------------------------------------------------------------
# HEADER AND TRAILER


def header_to_proto(header: domain.DeliveryHeader) -> store_pb2.DeliveryHeader:
    """Encode the header.

    Args:
        header: The domain header.

    Returns:
        store_pb2.DeliveryHeader: The header on the wire. An absent feed is the FEED_UNSPECIFIED zero.

    Raises:
        ProtoMappingError: If attempt_no is outside the uint32 range or the feed cannot be written.
    """
    return store_pb2.DeliveryHeader(
        request_id=header.request_id,
        instance_id=header.instance_id,
        attempt_no=_checked_uint('attempt_no', header.attempt_no, domain.MAX_UINT32),
        quota_key=header.quota_key,
        feed=_FEED_UNSPECIFIED if header.feed is None else FEED.to_proto(header.feed),
        vendor_asset_id=header.vendor_asset_id,
    )


def header_to_domain(message: store_pb2.DeliveryHeader) -> domain.DeliveryHeader:
    """Decode the header.

    Args:
        message: The header as it arrived.

    Returns:
        domain.DeliveryHeader: The domain header. FEED_UNSPECIFIED is None.

    Raises:
        ProtoMappingError: If the request or instance id is empty or the attempt number is 0.
    """
    return _built(
        domain.DeliveryHeader,
        request_id=message.request_id,
        instance_id=message.instance_id,
        attempt_no=message.attempt_no,
        quota_key=message.quota_key,
        feed=None if message.feed == _FEED_UNSPECIFIED else FEED.to_domain(message.feed),
        vendor_asset_id=message.vendor_asset_id,
    )


def served_range_to_proto(served_range: ServedRange) -> store_pb2.ServedRange:
    """Encode the served window.

    Args:
        served_range: The domain window.

    Returns:
        store_pb2.ServedRange: The window on the wire.

    Raises:
        ProtoMappingError: If a bound is naive.
    """
    message = store_pb2.ServedRange()
    message.start.CopyFrom(to_timestamp(served_range.start))
    message.end.CopyFrom(to_timestamp(served_range.end))
    return message


def served_range_to_domain(message: store_pb2.ServedRange) -> ServedRange:
    """Decode the served window.

    Args:
        message: The window as it arrived.

    Returns:
        ServedRange: The domain window.

    Raises:
        ProtoMappingError: If either bound is unset; the served window is never open.
    """
    return _built(ServedRange, start=timestamp_field(message, 'start'), end=timestamp_field(message, 'end'))


def vendor_reported_to_proto(reported: domain.VendorReported) -> store_pb2.VendorReported:
    """Encode what the vendor said about its limit.

    Args:
        reported: The domain value.

    Returns:
        store_pb2.VendorReported: The message; an absent limit or remaining is unset, never zero.

    Raises:
        ProtoMappingError: If a count is outside the uint64 range or reset_at is naive.
    """
    message = store_pb2.VendorReported()
    if reported.limit is not None:
        message.limit = _checked_uint('limit', reported.limit, domain.MAX_UINT64)
    if reported.remaining is not None:
        message.remaining = _checked_uint('remaining', reported.remaining, domain.MAX_UINT64)
    _set_optional_instant(message, 'reset_at', reported.reset_at)
    return message


def vendor_reported_to_domain(message: store_pb2.VendorReported) -> domain.VendorReported:
    """Decode what the vendor said about its limit.

    Args:
        message: The message as it arrived.

    Returns:
        domain.VendorReported: Absent fields stay None.

    Raises:
        ProtoMappingError: If a timestamp cannot be read.
    """
    return _built(
        domain.VendorReported,
        limit=message.limit if message.HasField('limit') else None,
        remaining=message.remaining if message.HasField('remaining') else None,
        reset_at=_optional_instant(message, 'reset_at'),
    )


def vendor_stats_to_proto(stats: domain.VendorCallStats) -> store_pb2.VendorCallStats:
    """Encode the cost of one attempt.

    Args:
        stats: The domain stats.

    Returns:
        store_pb2.VendorCallStats: The stats on the wire. Unset times and vendor_reported stay unset.

    Raises:
        ProtoMappingError: If a count is outside the uint64 range or a time is naive.
    """
    message = store_pb2.VendorCallStats(
        calls=_checked_uint('calls', stats.calls, domain.MAX_UINT64),
        pages=_checked_uint('pages', stats.pages, domain.MAX_UINT64),
        throttled_429=_checked_uint('throttled_429', stats.throttled_429, domain.MAX_UINT64),
        retries=_checked_uint('retries', stats.retries, domain.MAX_UINT64),
    )
    _set_optional_instant(message, 'first_call_at', stats.first_call_at)
    _set_optional_instant(message, 'last_call_at', stats.last_call_at)
    if stats.vendor_reported is not None:
        message.vendor_reported.CopyFrom(vendor_reported_to_proto(stats.vendor_reported))
    return message


def vendor_stats_to_domain(message: store_pb2.VendorCallStats) -> domain.VendorCallStats:
    """Decode the cost of one attempt.

    Args:
        message: The stats as they arrived.

    Returns:
        domain.VendorCallStats: The domain stats; unset optionals are None.

    Raises:
        ProtoMappingError: If a timestamp cannot be read.
    """
    return _built(
        domain.VendorCallStats,
        calls=message.calls,
        pages=message.pages,
        throttled_429=message.throttled_429,
        retries=message.retries,
        first_call_at=_optional_instant(message, 'first_call_at'),
        last_call_at=_optional_instant(message, 'last_call_at'),
        vendor_reported=(
            vendor_reported_to_domain(message.vendor_reported) if message.HasField('vendor_reported') else None
        ),
    )


def trailer_to_proto(trailer: domain.DeliveryTrailer) -> store_pb2.DeliveryTrailer:
    """Encode the trailer, last_attempt included.

    Args:
        trailer: The domain trailer.

    Returns:
        store_pb2.DeliveryTrailer: The trailer on the wire. An absent error_class is the UNSPECIFIED zero;
        absent stats, range and as_of are unset.

    Raises:
        ProtoMappingError: If a value cannot be put on the wire.
    """
    message = store_pb2.DeliveryTrailer(
        outcome=DELIVERY_OUTCOME.to_proto(trailer.outcome), detail=trailer.detail, last_attempt=trailer.last_attempt
    )
    if trailer.error_class is not None:
        message.error_class = DELIVERY_ERROR_CLASS.to_proto(trailer.error_class)
    if trailer.vendor_stats is not None:
        message.vendor_stats.CopyFrom(vendor_stats_to_proto(trailer.vendor_stats))
    if trailer.served_range is not None:
        message.served_range.CopyFrom(served_range_to_proto(trailer.served_range))
    _set_optional_instant(message, 'as_of', trailer.as_of)
    return message


def trailer_to_domain(message: store_pb2.DeliveryTrailer) -> domain.DeliveryTrailer:
    """Decode the trailer, last_attempt included.

    Args:
        message: The trailer as it arrived.

    Returns:
        domain.DeliveryTrailer: The domain trailer. The UNSPECIFIED error class is None.

    Raises:
        ProtoMappingError: If the outcome is unspecified or a nested message cannot be read.
    """
    return _built(
        domain.DeliveryTrailer,
        outcome=DELIVERY_OUTCOME.to_domain(message.outcome),
        error_class=None if message.error_class == 0 else DELIVERY_ERROR_CLASS.to_domain(message.error_class),
        detail=message.detail,
        vendor_stats=vendor_stats_to_domain(message.vendor_stats) if message.HasField('vendor_stats') else None,
        served_range=served_range_to_domain(message.served_range) if message.HasField('served_range') else None,
        as_of=_optional_instant(message, 'as_of'),
        last_attempt=message.last_attempt,
    )


# ---------------------------------------------------------------------------------------------
# PAYLOADS


def bar_page_to_proto(page: BarPage) -> store_pb2.BarPage:
    """Encode one page of bars.

    Args:
        page: The domain page.

    Returns:
        store_pb2.BarPage: The page on the wire.

    Raises:
        ProtoMappingError: If a bar cannot be written. A page over MAX_PAGE_BARS is refused: the split is
            attempt_to_messages's job, so an oversized page here is a caller's defect.
    """
    if len(page.bars) > MAX_PAGE_BARS:
        raise ProtoMappingError(f'a BarPage carries at most {MAX_PAGE_BARS} bars and this one carries {len(page.bars)}')
    return store_pb2.BarPage(bars=[bar_to_proto(bar) for bar in page.bars])


def bar_page_to_domain(message: store_pb2.BarPage) -> BarPage:
    """Decode one page of bars.

    Args:
        message: The page as it arrived.

    Returns:
        BarPage: The domain page.

    Raises:
        ProtoMappingError: If a bar cannot be read.
    """
    return _built(BarPage, bars=[bar_to_domain(bar) for bar in message.bars])


def asset_record_to_proto(record: domain.AssetRecord) -> store_pb2.AssetRecord:
    """Encode one vendor asset.

    Args:
        record: The domain record.

    Returns:
        store_pb2.AssetRecord: The record. active and tradable are written only when set, so an unset one
        stays unset on the wire and is never read as False.
    """
    message = store_pb2.AssetRecord(
        vendor_asset_id=record.vendor_asset_id,
        symbol=record.symbol,
        raw_payload=record.raw_payload,
        exchange=record.exchange,
        name=record.name,
    )
    if record.active is not None:
        message.active = record.active
    if record.tradable is not None:
        message.tradable = record.tradable
    return message


def asset_record_to_domain(message: store_pb2.AssetRecord) -> domain.AssetRecord:
    """Decode one vendor asset.

    Args:
        message: The record as it arrived.

    Returns:
        domain.AssetRecord: active and tradable are None when unset, never False.

    Raises:
        ProtoMappingError: If the model refuses a value.
    """
    return _built(
        domain.AssetRecord,
        vendor_asset_id=message.vendor_asset_id,
        symbol=message.symbol,
        raw_payload=message.raw_payload,
        active=message.active if message.HasField('active') else None,
        tradable=message.tradable if message.HasField('tradable') else None,
        exchange=message.exchange,
        name=message.name,
    )


def asset_records_to_proto(records: domain.AssetRecords) -> store_pb2.AssetRecords:
    """Encode one bounded message of asset records.

    Args:
        records: The domain records. Not split here; see attempt_to_messages.

    Returns:
        store_pb2.AssetRecords: The message.
    """
    return store_pb2.AssetRecords(records=[asset_record_to_proto(record) for record in records.records])


def asset_records_to_domain(message: store_pb2.AssetRecords) -> domain.AssetRecords:
    """Decode one message of asset records.

    Args:
        message: The message as it arrived.

    Returns:
        domain.AssetRecords: The domain records.

    Raises:
        ProtoMappingError: If a record cannot be read.
    """
    return _built(domain.AssetRecords, records=[asset_record_to_domain(record) for record in message.records])


def corporate_action_record_to_proto(record: domain.CorporateActionRecord) -> store_pb2.CorporateActionRecord:
    """Encode one vendor corporate action.

    Args:
        record: The domain record.

    Returns:
        store_pb2.CorporateActionRecord: The record; an absent date stays unset.

    Raises:
        ProtoMappingError: If a date is naive.
    """
    message = store_pb2.CorporateActionRecord(
        vendor_action_id=record.vendor_action_id,
        action_type=record.action_type,
        identifiers=record.identifiers,
        raw_payload=record.raw_payload,
    )
    _set_optional_instant(message, 'ex_date', record.ex_date)
    _set_optional_instant(message, 'record_date', record.record_date)
    _set_optional_instant(message, 'payable_date', record.payable_date)
    _set_optional_instant(message, 'process_date', record.process_date)
    _set_optional_instant(message, 'effective_date', record.effective_date)
    return message


def corporate_action_record_to_domain(message: store_pb2.CorporateActionRecord) -> domain.CorporateActionRecord:
    """Decode one vendor corporate action.

    Args:
        message: The record as it arrived.

    Returns:
        domain.CorporateActionRecord: An unset date is None, never the epoch.

    Raises:
        ProtoMappingError: If a date cannot be read.
    """
    return _built(
        domain.CorporateActionRecord,
        vendor_action_id=message.vendor_action_id,
        action_type=message.action_type,
        ex_date=_optional_instant(message, 'ex_date'),
        record_date=_optional_instant(message, 'record_date'),
        payable_date=_optional_instant(message, 'payable_date'),
        process_date=_optional_instant(message, 'process_date'),
        effective_date=_optional_instant(message, 'effective_date'),
        identifiers=dict(message.identifiers),
        raw_payload=message.raw_payload,
    )


def corporate_action_records_to_proto(records: domain.CorporateActionRecords) -> store_pb2.CorporateActionRecords:
    """Encode one bounded message of corporate-action records.

    Args:
        records: The domain records. Not split here; see attempt_to_messages.

    Returns:
        store_pb2.CorporateActionRecords: The message.

    Raises:
        ProtoMappingError: If a date is naive.
    """
    return store_pb2.CorporateActionRecords(
        records=[corporate_action_record_to_proto(record) for record in records.records]
    )


def corporate_action_records_to_domain(message: store_pb2.CorporateActionRecords) -> domain.CorporateActionRecords:
    """Decode one message of corporate-action records.

    Args:
        message: The message as it arrived.

    Returns:
        domain.CorporateActionRecords: The domain records.

    Raises:
        ProtoMappingError: If a record cannot be read.
    """
    return _built(
        domain.CorporateActionRecords, records=[corporate_action_record_to_domain(record) for record in message.records]
    )


# ---------------------------------------------------------------------------------------------
# THE ANSWER AND THE INSTANCE ANNOUNCEMENT


def response_to_proto(response: domain.DeliverAttemptResponse) -> store_pb2.DeliverAttemptResponse:
    """Encode the acknowledgement.

    Args:
        response: The domain acknowledgement.

    Returns:
        store_pb2.DeliverAttemptResponse: The acknowledgement on the wire.

    Raises:
        ProtoMappingError: If attempt_count is outside the uint32 range.
    """
    return store_pb2.DeliverAttemptResponse(
        verdict=DELIVERY_VERDICT.to_proto(response.verdict),
        attempt_count=_checked_uint('attempt_count', response.attempt_count, domain.MAX_UINT32),
    )


def response_to_domain(message: store_pb2.DeliverAttemptResponse) -> domain.DeliverAttemptResponse:
    """Decode the acknowledgement.

    Args:
        message: The acknowledgement as it arrived.

    Returns:
        domain.DeliverAttemptResponse: The domain acknowledgement.

    Raises:
        ProtoMappingError: If the verdict is unspecified: an acknowledgement that says nothing is not one.
    """
    return _built(
        domain.DeliverAttemptResponse,
        verdict=DELIVERY_VERDICT.to_domain(message.verdict),
        attempt_count=message.attempt_count,
    )


def register_instance_request_to_proto(request: domain.RegisterInstanceRequest) -> store_pb2.RegisterInstanceRequest:
    """Encode the instance announcement.

    Args:
        request: The domain announcement.

    Returns:
        store_pb2.RegisterInstanceRequest: The announcement on the wire.

    Raises:
        ProtoMappingError: If started_at is naive.
    """
    message = store_pb2.RegisterInstanceRequest(instance_id=request.instance_id)
    message.started_at.CopyFrom(to_timestamp(request.started_at))
    return message


def register_instance_request_to_domain(message: store_pb2.RegisterInstanceRequest) -> domain.RegisterInstanceRequest:
    """Decode the instance announcement.

    Args:
        message: The announcement as it arrived.

    Returns:
        domain.RegisterInstanceRequest: The domain announcement, started_at aware UTC.

    Raises:
        ProtoMappingError: If the id is empty or started_at is unset.
    """
    return _built(
        domain.RegisterInstanceRequest,
        instance_id=message.instance_id,
        started_at=timestamp_field(message, 'started_at'),
    )


def register_instance_response_to_proto(
    response: domain.RegisterInstanceResponse,
) -> store_pb2.RegisterInstanceResponse:
    """Encode the empty answer to the announcement.

    Args:
        response: The domain answer.

    Returns:
        store_pb2.RegisterInstanceResponse: The empty message.
    """
    return store_pb2.RegisterInstanceResponse()


def register_instance_response_to_domain(
    message: store_pb2.RegisterInstanceResponse,
) -> domain.RegisterInstanceResponse:
    """Decode the empty answer to the announcement.

    Args:
        message: The answer as it arrived.

    Returns:
        domain.RegisterInstanceResponse: The empty answer.
    """
    return domain.RegisterInstanceResponse()


# ---------------------------------------------------------------------------------------------
# THE STREAM: split on the way out, assemble on the way in


def _chunks[M: Message](records: Sequence[M]) -> Iterator[list[M]]:
    # Greedy, in order: close the current chunk when the next record would break the count cap or the byte
    # budget. A record over the budget on its own gets a chunk to itself, unless it is over the guard.
    chunk: list[M] = []
    chunk_bytes = 0
    for record in records:
        size = record.ByteSize() + _RECORD_OVERHEAD_BYTES
        if size > MAX_MESSAGE_BYTES:
            raise ProtoMappingError(
                f'one {record.DESCRIPTOR.name} serialises to {size} bytes, over the {MAX_MESSAGE_BYTES} byte '
                'message guard, and a record cannot be split'
            )
        if chunk and (len(chunk) >= MAX_RECORDS_PER_MESSAGE or chunk_bytes + size > MAX_PART_BYTES):
            yield chunk
            chunk, chunk_bytes = [], 0
        chunk.append(record)
        chunk_bytes += size
    if chunk:
        yield chunk


def _payload_messages(part: domain.DeliveryPayload) -> Iterator[store_pb2.DeliverAttemptRequest]:
    if isinstance(part, BarPage):
        for start in range(0, len(part.bars), MAX_PAGE_BARS):
            page = BarPage(bars=part.bars[start : start + MAX_PAGE_BARS])
            yield store_pb2.DeliverAttemptRequest(bar_page=bar_page_to_proto(page))
    elif isinstance(part, domain.AssetRecords):
        for chunk in _chunks([asset_record_to_proto(record) for record in part.records]):
            yield store_pb2.DeliverAttemptRequest(asset_records=store_pb2.AssetRecords(records=chunk))
    else:
        for chunk in _chunks([corporate_action_record_to_proto(record) for record in part.records]):
            yield store_pb2.DeliverAttemptRequest(
                corporate_action_records=store_pb2.CorporateActionRecords(records=chunk)
            )


def attempt_to_messages(attempt: domain.AttemptDelivery) -> list[store_pb2.DeliverAttemptRequest]:
    """Encode one attempt as the stream's messages: the header, the payload split to the bounds, the trailer.

    Every bar page holds at most MAX_PAGE_BARS bars, and every asset or corporate-action message at most
    MAX_RECORDS_PER_MESSAGE records and MAX_PART_BYTES of serialized records, whatever the domain parts
    hold. The order of records is kept. A part with no records yields no message.

    Args:
        attempt: The whole attempt.

    Returns:
        list[store_pb2.DeliverAttemptRequest]: Header first, trailer last. A list and not an iterator, so
        an unmappable attempt fails here, before any call is made, and a redelivery re-encodes identically.

    Raises:
        ProtoMappingError: If any value cannot be put on the wire, or a single record is over the guard.
    """
    messages = [store_pb2.DeliverAttemptRequest(header=header_to_proto(attempt.header))]
    for part in attempt.parts:
        messages.extend(_payload_messages(part))
    messages.append(store_pb2.DeliverAttemptRequest(trailer=trailer_to_proto(attempt.trailer)))
    return messages


class AttemptAssembler:
    """Reads one DeliverAttempt stream's messages, in order, and returns the attempt they make.

    One instance per call. It enforces the grammar: a header first and only first, payload messages all of
    one kind between, a trailer last and nothing after it. Every departure is a ProtoMappingError.
    """

    def __init__(self) -> None:
        self._header: domain.DeliveryHeader | None = None
        self._trailer: domain.DeliveryTrailer | None = None
        self._bars: list[BarPage] = []
        self._assets: list[domain.AssetRecord] = []
        self._actions: list[domain.CorporateActionRecord] = []
        self._kind: str | None = None

    def feed(self, message: store_pb2.DeliverAttemptRequest) -> None:
        """Take the next message of the stream.

        Args:
            message: The message as it arrived.

        Raises:
            ProtoMappingError: If the message breaks the grammar or will not map.
        """
        arm = message.WhichOneof('part')
        if arm is None:
            raise ProtoMappingError('a DeliverAttemptRequest arrived with no part set; its oneof holds one arm')
        if self._trailer is not None:
            raise ProtoMappingError(f'a {arm} arrived after the trailer, which terminates the stream')
        if arm == 'header':
            if self._header is not None:
                raise ProtoMappingError('a second header arrived; the header is the first message and only that')
            self._header = header_to_domain(message.header)
            return
        if self._header is None:
            raise ProtoMappingError(f'the stream began with a {arm}; the header is always the first message')
        if arm == 'trailer':
            self._trailer = trailer_to_domain(message.trailer)
            return
        if self._kind is not None and self._kind != arm:
            raise ProtoMappingError(f'a {arm} arrived in a stream that carries {self._kind}; one attempt, one kind')
        self._kind = arm
        if arm == 'bar_page':
            self._bars.append(bar_page_to_domain(message.bar_page))
        elif arm == 'asset_records':
            self._assets.append(asset_records_to_domain(message.asset_records))
        else:
            self._actions.append(corporate_action_records_to_domain(message.corporate_action_records))

    def finish(self) -> domain.AttemptDelivery:
        """Return the attempt, once the stream has ended.

        Returns:
            domain.AttemptDelivery: The header, the payload messages in arrival order, the trailer.

        Raises:
            ProtoMappingError: If the stream ended without a header or without a trailer. A stream with no
                trailer is incomplete: the store writes nothing for it.
        """
        if self._header is None:
            raise ProtoMappingError('the stream ended without a header')
        if self._trailer is None:
            raise ProtoMappingError('the stream ended without a trailer; an incomplete attempt writes nothing')
        parts: list[domain.DeliveryPayload] = [*self._bars, *self._assets, *self._actions]
        return _built(domain.AttemptDelivery, header=self._header, parts=parts, trailer=self._trailer)


__all__ = [
    'DELIVERY_ERROR_CLASS',
    'DELIVERY_OUTCOME',
    'DELIVERY_VERDICT',
    'MAX_PAGE_BARS',
    'MAX_PART_BYTES',
    'MAX_RECORDS_PER_MESSAGE',
    'AttemptAssembler',
    'asset_record_to_domain',
    'asset_record_to_proto',
    'asset_records_to_domain',
    'asset_records_to_proto',
    'attempt_to_messages',
    'bar_page_to_domain',
    'bar_page_to_proto',
    'corporate_action_record_to_domain',
    'corporate_action_record_to_proto',
    'corporate_action_records_to_domain',
    'corporate_action_records_to_proto',
    'header_to_domain',
    'header_to_proto',
    'register_instance_request_to_domain',
    'register_instance_request_to_proto',
    'register_instance_response_to_domain',
    'register_instance_response_to_proto',
    'response_to_domain',
    'response_to_proto',
    'served_range_to_domain',
    'served_range_to_proto',
    'trailer_to_domain',
    'trailer_to_proto',
    'vendor_reported_to_domain',
    'vendor_reported_to_proto',
    'vendor_stats_to_domain',
    'vendor_stats_to_proto',
]
