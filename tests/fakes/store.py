"""FakeStore: the store's side of the push path in memory, and FakeStoreServer, that side over real gRPC.

Bead tj-7zjov1.24 (P14). Design: ADR tj-26jt9w addenda 4 and 5 (every attempt reported and acknowledged before any
retry; idempotent on (request_id, attempt_no); the restart intake: a new instance id, and every request the old
instance held re-submitted flagged recovered with its durable attempt count) and the owner rulings of 2026-10-24
(push; expiry at dequeue).

WHAT IT IS. FakeStore conforms BY SHAPE to two interfaces at once: common.rpc.store.DeliveryHandler, so the production
StoreServicer can serve it (FakeStoreServer below), and common.rpc.clients.store_delivery.StoreDeliveryClient, so a
broker handle can deliver to it in process with no network. It also plays the store's REQUESTING side, which the real
store does in P35 (tj-7zjov1.28): it submits requests to an ingest submit callable, remembers which are open, and
re-submits them flagged recovered when told an ingest restart happened (recover). Its state is a durable store's: a
store restart (go_down, come_up, or FakeStoreServer.restart) keeps every record, as Postgres would.

THE VERDICT follows the real intake's rule (data/store/CLAUDE.md, Delivery intake), simplified to what ingest can see:

* A duplicate (request_id, attempt_no) writes nothing and is answered from the stored answer. An EXPIRED or EXHAUSTED
  report, which has no attempt row, is a duplicate once its request is finished, and is answered FINAL.
* FINAL on SUCCEEDED, on a PERMANENT failure, on last_attempt, on EXPIRED and EXHAUSTED, on a request already
  finished, and on any report from a SUPERSEDED instance (one older than the latest instance the store has seen).
  CONTINUE otherwise. A FINAL verdict finishes the request, except a superseded instance's inconclusive failure, which
  leaves it to the live instance.
* The durable count: a SUCCEEDED or FAILED report raises it to its attempt number; EXPIRED and EXHAUSTED leave it.

WHAT IT IS NOT: no data is judged or stored beyond the report itself, nothing is written to a database, and no rule
of the store's arms (bars, asset list, corporate actions) is modelled. Every attempt it acknowledges is kept whole in
`written`, so a test reads what reached the store.

IMPORT DISCIPLINE: this package runs in the production image (tests/fakes/__init__.py), so this module imports only
the standard library and first-party code.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from common.errors.vocabulary import ExogenousError, Reason
from common.rpc.server import BindAddress, GrpcServerHost
from common.rpc.store import store_service
from schemas.data_ingest.delivery import (
    AttemptDelivery,
    DeliverAttemptResponse,
    DeliveryErrorClass,
    DeliveryOutcome,
    DeliveryVerdict,
    RegisterInstanceRequest,
    RegisterInstanceResponse,
)
from schemas.data_ingest.submit import SubmitAck, SubmitRequest


# The outcomes that are not attempts: no vendor call was made, so the durable count does not move.
NOT_ATTEMPTS = frozenset({DeliveryOutcome.EXPIRED, DeliveryOutcome.EXHAUSTED})

IngestSubmit = Callable[[SubmitRequest], Awaitable[SubmitAck]]


@dataclass(frozen=True)
class Answered:
    """One delivery the store wrote and the answer it gave.

    Attributes:
        attempt (AttemptDelivery): The report as it arrived.
        answer (DeliverAttemptResponse): The acknowledgement.
    """

    attempt: AttemptDelivery
    answer: DeliverAttemptResponse


class FakeStore:
    """The store's delivery intake and request side, in memory. See the module docstring."""

    def __init__(self) -> None:
        self.__up = True
        self.__gate: asyncio.Event | None = None
        # Every delivery that reached the store while it was up, duplicates included, in arrival order.
        self.__received: list[AttemptDelivery] = []
        # Every delivery the store wrote (never a duplicate), in order, with its answer.
        self.__written: list[Answered] = []
        self.__answers: dict[tuple[str, int], DeliverAttemptResponse] = {}
        self.__counts: dict[str, int] = {}
        self.__finished: set[str] = set()
        self.__requests: dict[str, SubmitRequest] = {}
        self.__instances: list[str] = []
        self.__announced: list[RegisterInstanceRequest] = []
        self.__refused_while_down = 0

    # -- what a test reads --------------------------------------------------------------------

    @property
    def received(self) -> tuple[AttemptDelivery, ...]:
        """tuple[AttemptDelivery, ...]: Every delivery that reached the store while up, duplicates included."""
        return tuple(self.__received)

    @property
    def written(self) -> tuple[Answered, ...]:
        """tuple[Answered, ...]: Every delivery the store wrote, once each, in order, with its answer."""
        return tuple(self.__written)

    @property
    def announced(self) -> tuple[RegisterInstanceRequest, ...]:
        """tuple[RegisterInstanceRequest, ...]: Every RegisterInstance the store acknowledged, in order."""
        return tuple(self.__announced)

    @property
    def refused_while_down(self) -> int:
        """int: Calls refused because the store was down: deliveries and announcements alike."""
        return self.__refused_while_down

    @property
    def live_instance(self) -> str | None:
        """Str | None: The latest ingest instance the store has seen, or None before any."""
        return self.__instances[-1] if self.__instances else None

    def count(self, request_id: str) -> int:
        """The request's durable attempt count.

        Args:
            request_id (str): The request.

        Returns:
            int: The count, 0 for a request with no counted attempt.
        """
        return self.__counts.get(request_id, 0)

    def is_finished(self, request_id: str) -> bool:
        """Whether the store has ended the request (a FINAL verdict that finished it).

        Args:
            request_id (str): The request.

        Returns:
            bool: True once finished.
        """
        return request_id in self.__finished

    def open_requests(self) -> list[SubmitRequest]:
        """The requests the store submitted that it has not finished, in submission order.

        Returns:
            list[SubmitRequest]: The open requests, as first submitted.
        """
        return [request for request_id, request in self.__requests.items() if request_id not in self.__finished]

    # -- what a test drives -------------------------------------------------------------------

    def hold(self) -> None:
        """Hold every answer from now on: a delivery is written, then waits for release() before it is answered."""
        if self.__gate is None or self.__gate.is_set():
            self.__gate = asyncio.Event()

    def release(self) -> None:
        """Answer every held delivery, and stop holding."""
        if self.__gate is not None:
            self.__gate.set()

    def go_down(self) -> None:
        """Become unreachable: every call is refused PEER_UNAVAILABLE and nothing is received. State is kept."""
        self.__up = False

    def come_up(self) -> None:
        """Become reachable again, with every record the store held before it went down."""
        self.__up = True

    # -- the requesting side (the real store's P35) -------------------------------------------

    async def submit(self, ingest: IngestSubmit, request: SubmitRequest) -> SubmitAck:
        """Record a request as open and submit it to ingest.

        Args:
            ingest (IngestSubmit): Ingest's submit: a broker handle's, or a Submit client's.
            request (SubmitRequest): The request.

        Returns:
            SubmitAck: Ingest's answer. A refused request is finished at once.
        """
        self.__requests.setdefault(request.request_id, request)
        ack = await ingest(request)
        if ack.refused is not None:
            self.__finished.add(request.request_id)
        return ack

    async def recover(self, ingest: IngestSubmit) -> list[SubmitAck]:
        """Re-submit every open request to a restarted ingest, flagged recovered, with its durable count.

        Args:
            ingest (IngestSubmit): The new instance's submit.

        Returns:
            list[SubmitAck]: Ingest's answers, in submission order.
        """
        acks = []
        for request in self.open_requests():
            again = request.model_copy(update={'recovered': True, 'attempt_count': self.count(request.request_id)})
            acks.append(await ingest(again))
        return acks

    # -- the delivery side: DeliveryHandler and StoreDeliveryClient by shape --------------------

    async def deliver(self, attempt: AttemptDelivery) -> DeliverAttemptResponse:
        """Write one reported attempt, then answer: CONTINUE or FINAL with the durable count.

        Args:
            attempt (AttemptDelivery): The whole attempt.

        Returns:
            DeliverAttemptResponse: The acknowledgement; for a duplicate, the stored one.

        Raises:
            ExogenousError: PEER_UNAVAILABLE while the store is down. Nothing is received or written.
        """
        self.__refuse_when_down()
        self.__received.append(attempt)
        answer = self.__write(attempt)
        gate = self.__gate
        if gate is not None and not gate.is_set():
            await gate.wait()
        self.__refuse_when_down()
        return answer

    async def register_instance(self, request: RegisterInstanceRequest) -> RegisterInstanceResponse:
        """Record an announced instance. Idempotent: an id already held is answered again.

        Args:
            request (RegisterInstanceRequest): The instance id and when it started.

        Returns:
            RegisterInstanceResponse: The empty answer.

        Raises:
            ExogenousError: PEER_UNAVAILABLE while the store is down.
        """
        self.__refuse_when_down()
        self.__announced.append(request)
        self.__sighted(request.instance_id)
        return RegisterInstanceResponse()

    def __refuse_when_down(self) -> None:
        if not self.__up:
            self.__refused_while_down += 1
            raise ExogenousError(Reason.PEER_UNAVAILABLE, 'the fake store is down')

    def __sighted(self, instance_id: str) -> None:
        if instance_id not in self.__instances:
            self.__instances.append(instance_id)

    def __write(self, attempt: AttemptDelivery) -> DeliverAttemptResponse:
        header, trailer = attempt.header, attempt.trailer
        request_id = header.request_id
        key = (request_id, header.attempt_no)
        stored = self.__answers.get(key)
        if stored is not None:
            return stored
        if trailer.outcome in NOT_ATTEMPTS and request_id in self.__finished:
            return DeliverAttemptResponse(verdict=DeliveryVerdict.FINAL, attempt_count=self.count(request_id))

        self.__sighted(header.instance_id)
        superseded = header.instance_id != self.live_instance
        already_finished = request_id in self.__finished
        if trailer.outcome not in NOT_ATTEMPTS:
            self.__counts[request_id] = max(self.count(request_id), header.attempt_no)
        conclusive = trailer.outcome in NOT_ATTEMPTS or (
            trailer.outcome is DeliveryOutcome.SUCCEEDED
            or trailer.error_class is DeliveryErrorClass.PERMANENT
            or (trailer.last_attempt and not superseded)
        )
        final = conclusive or already_finished or superseded
        if conclusive:
            self.__finished.add(request_id)
        answer = DeliverAttemptResponse(
            verdict=DeliveryVerdict.FINAL if final else DeliveryVerdict.CONTINUE, attempt_count=self.count(request_id)
        )
        self.__answers[key] = answer
        self.__written.append(Answered(attempt, answer))
        return answer


class FakeStoreServer:
    """A FakeStore served over real gRPC by the production StoreServicer, on one loopback port it keeps.

    Each start builds a new production GrpcServerHost (a host starts once) on the same port, so a store restart is
    stop() then start(): the address ingest dials does not move, and the FakeStore's records survive.

    Args:
        store (FakeStore): The store to serve.
        host (str): The interface to bind; a specific one, never a wildcard.
        port (int): The port, or 0 for one the OS picks on the first start and kept after it.
    """

    def __init__(self, store: FakeStore, host: str = '127.0.0.1', port: int = 0) -> None:
        self.__store = store
        self.__host = host
        self.__port = port
        self.__server: GrpcServerHost | None = None

    @property
    def store(self) -> FakeStore:
        """FakeStore: The store served."""
        return self.__store

    @property
    def target(self) -> str:
        """str: host:port to dial. Known once the server has started at least once.

        Raises:
            RuntimeError: Before the first start with port 0.
        """
        if self.__port == 0:
            raise RuntimeError('the fake store server has not been started')
        return f'{self.__host}:{self.__port}'

    @property
    def serving(self) -> bool:
        """bool: Whether the server is started and not stopped."""
        return self.__server is not None

    async def start(self) -> None:
        """Serve the store on the kept port.

        Raises:
            RuntimeError: If it is already serving, or the port cannot be bound.
        """
        if self.__server is not None:
            raise RuntimeError('the fake store server is already serving')
        server = GrpcServerHost(BindAddress(self.__host, self.__port), [store_service(self.__store)], stop_grace_s=0)
        await server.start()
        self.__server = server
        self.__port = server.port

    async def stop(self) -> None:
        """Stop serving. Safe when not serving. The store's records are kept."""
        server, self.__server = self.__server, None
        if server is not None:
            await server.stop()

    async def restart(self) -> None:
        """Stop and start again on the same port: a store restart."""
        await self.stop()
        await self.start()

    async def __aenter__(self) -> 'FakeStoreServer':
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()
