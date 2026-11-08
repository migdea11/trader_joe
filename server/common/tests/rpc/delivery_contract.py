"""The domain side of one DeliverAttempt of each kind, and a StoreService peer on a real GrpcServerHost (tj-7zjov1.16).

Shared by test_rpc_delivery_mapping.py, test_rpc_store_servicer.py and test_rpc_store_delivery_client.py, so
every test states only what it varies.

NOTHING HERE IMPORTS trader_joe.proto (TID251). Wire messages are built through the production encoders or
through ``fetch_contract.message_class``, so a stream the production splitter would never write -- a second
header, a payload before the header, an empty oneof -- is still reachable. The peer is the REAL server
machinery: GrpcServerHost, with its error boundary and health service, bound to 127.0.0.1 on an ephemeral
port, serving either the production ``store_service(handler)`` registration or a scripted generic one.

THE HANDLER DOUBLE INHERITS NOTHING. DeliveryHandler is a structural Protocol; data_store must conform by
shape, so a double that subclassed it would prove the opposite of the rule.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta

import grpc
from google.protobuf.message import Message

from common.enums.data_stock import Feed
from common.rpc.mapping import CONTRACT_FILES
from common.rpc.server import BindAddress, GrpcServerHost, ServiceRegistration
from common.rpc.store import store_service
from common.tests.rpc.fetch_contract import LOOPBACK, a_bar, message_class
from schemas.data_ingest import delivery as domain
from schemas.data_ingest.fetch_dataset import BarPage, ServedRange


WHEN = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)

_STORE_FILE = next(file for file in CONTRACT_FILES if file.name.endswith('internal/store/v1/store.proto'))
STORE_SERVICE = _STORE_FILE.services_by_name['StoreService']
DELIVER_PATH = f'/{STORE_SERVICE.full_name}/DeliverAttempt'
REGISTER_PATH = f'/{STORE_SERVICE.full_name}/RegisterInstance'

Envelope = message_class('DeliverAttemptRequest')
ResponseMessage = message_class('DeliverAttemptResponse')
RegisterRequestMessage = message_class('RegisterInstanceRequest')
RegisterResponseMessage = message_class('RegisterInstanceResponse')


# ---------------------------------------------------------------------------------------------
# THE DOMAIN SIDE


def a_header(**overrides: object) -> domain.DeliveryHeader:
    """One bars header with every field set off its default.

    Args:
        **overrides: Fields to replace.

    Returns:
        domain.DeliveryHeader: The header.
    """
    fields: dict[str, object] = {
        'request_id': 'req-7',
        'instance_id': 'ingest-1',
        'attempt_no': 3,
        'quota_key': 'alpaca-a',
        'feed': Feed.IEX,
        'vendor_asset_id': 'b0b6dd9d-8b9b-48a9-ba46-b9d54906e415',
    }
    fields.update(overrides)
    return domain.DeliveryHeader(**fields)


def a_trailer(**overrides: object) -> domain.DeliveryTrailer:
    """One SUCCEEDED trailer with its optional members set.

    Args:
        **overrides: Fields to replace.

    Returns:
        domain.DeliveryTrailer: The trailer.
    """
    fields: dict[str, object] = {
        'outcome': domain.DeliveryOutcome.SUCCEEDED,
        'detail': 'ok',
        'vendor_stats': domain.VendorCallStats(
            calls=2,
            pages=2,
            throttled_429=0,
            retries=0,
            first_call_at=WHEN,
            last_call_at=WHEN + timedelta(seconds=3),
            vendor_reported=domain.VendorReported(limit=200, remaining=198, reset_at=WHEN + timedelta(minutes=1)),
        ),
        'served_range': ServedRange(start=WHEN, end=WHEN + timedelta(days=1)),
        'as_of': WHEN + timedelta(seconds=3),
    }
    fields.update(overrides)
    return domain.DeliveryTrailer(**fields)


def an_asset(index: int, *, payload_bytes: int = 0, **overrides: object) -> domain.AssetRecord:
    """One asset record, distinct per index.

    Args:
        index: Makes the record distinct, so a lost or reordered record is visible.
        payload_bytes: Pad raw_payload to at least this many bytes.
        **overrides: Fields to replace.

    Returns:
        domain.AssetRecord: The record.
    """
    raw = f'{{"id":"asset-{index}"}}'.encode()
    fields: dict[str, object] = {
        'vendor_asset_id': f'asset-{index}',
        'symbol': f'SYM{index}',
        'raw_payload': raw + b' ' * max(0, payload_bytes - len(raw)),
        'active': True,
        'tradable': False,
        'exchange': 'NYSE',
    }
    fields.update(overrides)
    return domain.AssetRecord(**fields)


def an_action(index: int, **overrides: object) -> domain.CorporateActionRecord:
    """One corporate-action record, distinct per index.

    Args:
        index: Makes the record distinct.
        **overrides: Fields to replace.

    Returns:
        domain.CorporateActionRecord: The record.
    """
    fields: dict[str, object] = {
        'vendor_action_id': f'ca-{index}',
        'action_type': 'cash_dividend',
        'ex_date': WHEN,
        'record_date': WHEN + timedelta(days=1),
        'payable_date': WHEN + timedelta(days=2),
        'identifiers': {'cusip': f'92206C{index}', 'isin': f'US92206C{index}'},
        'raw_payload': f'{{"id":"ca-{index}"}}'.encode(),
    }
    fields.update(overrides)
    return domain.CorporateActionRecord(**fields)


def an_attempt(*parts: domain.DeliveryPayload, **overrides: object) -> domain.AttemptDelivery:
    """One whole attempt: the header, the given parts, the trailer.

    Args:
        *parts: The payload parts, all of one kind.
        **overrides: header= or trailer= to replace.

    Returns:
        domain.AttemptDelivery: The attempt.
    """
    return domain.AttemptDelivery(
        header=overrides.get('header', a_header()), parts=list(parts), trailer=overrides.get('trailer', a_trailer())
    )


def bars(count: int) -> BarPage:
    """Returns: BarPage: COUNT distinct bars, one minute apart, in one domain part (unsplit).

    Args:
        count: How many bars.
    """
    return BarPage(bars=[a_bar(minute=minute) for minute in range(count)])


def records_of(attempt: domain.AttemptDelivery) -> list[object]:
    """Every record of every part, in order: the payload as a consumer reads it, whatever the split.

    Args:
        attempt: The attempt.

    Returns:
        list[object]: The bars, assets or actions, flattened.
    """
    flat: list[object] = []
    for part in attempt.parts:
        flat.extend(part.bars if isinstance(part, BarPage) else part.records)
    return flat


def a_response(verdict: domain.DeliveryVerdict = domain.DeliveryVerdict.CONTINUE, count: int = 3):
    """Returns: domain.DeliverAttemptResponse: The store's acknowledgement.

    Args:
        verdict: CONTINUE or FINAL.
        count: The durable attempt count.
    """
    return domain.DeliverAttemptResponse(verdict=verdict, attempt_count=count)


def a_registration() -> domain.RegisterInstanceRequest:
    """Returns: domain.RegisterInstanceRequest: The instance announcement."""
    return domain.RegisterInstanceRequest(instance_id='ingest-1', started_at=WHEN)


# ---------------------------------------------------------------------------------------------
# THE HANDLER DOUBLE: no base class, keyed on (request_id, attempt_no) as the real store must be


class Handler:
    """A DeliveryHandler by SHAPE ONLY. Answers a duplicate (request_id, attempt_no) from its stored row.

    Attributes:
        delivered: Every attempt the servicer handed over, duplicates included, in call order.
        written: The attempts actually written: one per distinct (request_id, attempt_no).
        registered: Every announcement handed over.
    """

    def __init__(
        self,
        *,
        answer: domain.DeliverAttemptResponse | None = None,
        raising: BaseException | None = None,
        gate: asyncio.Event | None = None,
    ) -> None:
        """Script the handler.

        Args:
            answer: What a first delivery is answered with.
            raising: Raised by every call instead of answering.
            gate: When given, deliver waits on it before answering, so a test can hold the ack back.
        """
        self._answer = answer or a_response()
        self._raising = raising
        self._gate = gate
        self.entered = asyncio.Event()
        self.delivered: list[domain.AttemptDelivery] = []
        self.written: dict[tuple[str, int], domain.DeliverAttemptResponse] = {}
        self.registered: list[domain.RegisterInstanceRequest] = []

    async def deliver(self, attempt: domain.AttemptDelivery) -> domain.DeliverAttemptResponse:
        """Record the attempt, then answer, or answer a duplicate from the stored row.

        Args:
            attempt: The assembled attempt.

        Returns:
            domain.DeliverAttemptResponse: The stored or scripted answer.

        Raises:
            BaseException: The scripted error.
        """
        self.delivered.append(attempt)
        self.entered.set()
        if self._raising is not None:
            raise self._raising
        if self._gate is not None:
            await self._gate.wait()
        key = (attempt.header.request_id, attempt.header.attempt_no)
        if key not in self.written:
            self.written[key] = self._answer
        return self.written[key]

    async def register_instance(self, request: domain.RegisterInstanceRequest) -> None:
        """Record the announcement.

        Args:
            request: The announcement.

        Raises:
            BaseException: The scripted error.
        """
        self.registered.append(request)
        if self._raising is not None:
            raise self._raising


# ---------------------------------------------------------------------------------------------
# PEERS ON A REAL GrpcServerHost


@contextlib.asynccontextmanager
async def hosted(registration: ServiceRegistration) -> AsyncIterator[grpc.aio.Channel]:
    """Serve REGISTRATION on a real GrpcServerHost on loopback and yield a channel to it.

    Args:
        registration: What to serve.

    Yields:
        grpc.aio.Channel: A channel to the host. Both are torn down on the way out.
    """
    async with GrpcServerHost(BindAddress(LOOPBACK, 0), [registration], stop_grace_s=0) as host:
        channel = grpc.aio.insecure_channel(f'{LOOPBACK}:{host.port}')
        try:
            yield channel
        finally:
            await channel.close()


def store_peer(handler: Handler) -> contextlib.AbstractAsyncContextManager[grpc.aio.Channel]:
    """The PRODUCTION servicer, registered the production way, on a real host.

    Args:
        handler: The handler double.

    Returns:
        The context manager yielding a channel.
    """
    return hosted(store_service(handler))


type StreamPeer = Callable[[AsyncIterator[Message], grpc.aio.ServicerContext], Awaitable[Message]]


def scripted_registration(deliver: StreamPeer) -> ServiceRegistration:
    """A StoreService registration whose DeliverAttempt does whatever DELIVER does, at the wire.

    Args:
        deliver: The scripted method.

    Returns:
        ServiceRegistration: For ``hosted``.
    """
    handlers: Mapping[str, grpc.RpcMethodHandler] = {
        'DeliverAttempt': grpc.stream_unary_rpc_method_handler(
            deliver,
            request_deserializer=Envelope.FromString,
            response_serializer=lambda response: response.SerializeToString(),
        )
    }
    return ServiceRegistration(
        name=STORE_SERVICE.full_name,
        add_to_server=lambda server: server.add_generic_rpc_handlers(
            (grpc.method_handlers_generic_handler(STORE_SERVICE.full_name, handlers),)
        ),
    )
