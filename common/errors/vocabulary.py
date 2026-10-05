"""The error vocabulary, defined once: the exception hierarchy, the closed Reason enum and its one table.

ADR tj-fa1rpu D3, D4, D5, D8 and D10 with U1 = A, plus its two 2026-10-02 addenda: Q-URI amends U3 and
Q-EMPTY amends C1/D3(b). U1 makes Python canonical. The gRPC renderer in common/rpc and the problem+json
renderer in routers/common both read this module, and nothing here knows either protocol.

STANDARD LIBRARY ONLY. No grpc, no FastAPI, no pydantic and no first-party import. The docs/errors.md
generator (tj-3mk3u5.37.10) is a stdlib-only script that imports this module, and a reason must stay
recordable where no transport reaches, such as the coverage ledger's Postgres enum.

THE HIERARCHY (D5) has two levels below the base and no more:

    TraderJoeError              the base, never raised itself
        ExogenousError          expected, outside our control, and must be signalled
        InvalidRequestError     the caller asked for something that cannot be asked
            <a leaf>            defined beside the code that raises it, subclassing ONE branch directly

Nothing subclasses a leaf, and nothing but the two branches subclasses the base. A class that tries is
refused when it is defined: a deeper tree invites an except clause that catches an interior node, which
collapses again the failures this vocabulary exists to tell apart. Every reason belongs to one branch, the
branch column of REASONS, and an error built with the other branch's reason is refused, so the class and
the table cannot disagree. Anything raised that is not a TraderJoeError is a bug, and no boundary
converts it to a reason.

THE IDENTITY is (ERROR_DOMAIN, reason), and clients branch on the reason (tj-8konfu D6.4). Retryability
is not a field. It is the reason's outcome in REASONS (D4). There is no problem+json type URI here: the
type is about:blank for every error (U3 as amended 2026-10-02), and the problem+json renderer writes it.

THE ERROR ID that ties an answer to its cause chain in the log (D8) is minted by new_error_id and nothing
else, so both edges and every raise site spell it the same way. It is read back by own_error_id and the
chain decision is made by has_cause_chain, here for the same reason: an edge that asked the question its
own way would answer it differently from the other edge, and the one id a human quotes would stop being
one id.
"""

import math
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Self


# One generic constant, because this repository is public (D4; tj-8konfu D6.4). Ruled on Q-URI
# tj-3mk3u5.37.2: the ErrorInfo domain on the gRPC hop and the 'domain' member of problem+json.
ERROR_DOMAIN: Final = 'trader-joe'


class Reason(StrEnum):
    """Why a request failed: the proximate cause, from a CLOSED set (D4).

    Each member's name is its value, UPPER_SNAKE as AIP-193 requires of an ErrorInfo reason. Adding a
    member is a reviewable change that needs its row in REASONS. Renaming or removing one breaks every
    client that branches on it (D4), and the SDK must survive a reason it has never heard of (D10).
    """

    # REFUSED: permanent for the request as asked.
    INVALID_REQUEST = 'INVALID_REQUEST'
    UNSUPPORTED_ASSET_TYPE = 'UNSUPPORTED_ASSET_TYPE'
    UNSUPPORTED_INSTRUMENT = 'UNSUPPORTED_INSTRUMENT'
    FEED_NOT_AVAILABLE = 'FEED_NOT_AVAILABLE'
    VENDOR_INVALID_REQUEST = 'VENDOR_INVALID_REQUEST'
    RANGE_IN_FUTURE = 'RANGE_IN_FUTURE'
    VENDOR_REJECTED = 'VENDOR_REJECTED'
    NOT_FOUND = 'NOT_FOUND'
    OWNER_MISMATCH = 'OWNER_MISMATCH'
    OWN_OVERLAP_CONFLICT = 'OWN_OVERLAP_CONFLICT'
    RANGE_COLLISION = 'RANGE_COLLISION'
    RANGE_SHRINK = 'RANGE_SHRINK'
    DUPLICATE_BAR_TIMESTAMP = 'DUPLICATE_BAR_TIMESTAMP'
    DATABASE_INTEGRITY = 'DATABASE_INTEGRITY'
    # NOT_READY: the answer could exist later.
    RATE_BUDGET = 'RATE_BUDGET'
    VENDOR_RATE_LIMITED = 'VENDOR_RATE_LIMITED'
    VENDOR_UNAVAILABLE = 'VENDOR_UNAVAILABLE'
    VENDOR_AUTH = 'VENDOR_AUTH'
    DEADLINE = 'DEADLINE'
    PEER_UNAVAILABLE = 'PEER_UNAVAILABLE'
    PEER_INTERNAL = 'PEER_INTERNAL'
    PEER_PROTOCOL_ERROR = 'PEER_PROTOCOL_ERROR'
    DATABASE_UNAVAILABLE = 'DATABASE_UNAVAILABLE'
    DATABASE_CONFLICT = 'DATABASE_CONFLICT'


# Names that records have already fixed for later PRs. They are NOT members, so nothing can raise them
# yet, and no member may take one of these names to mean anything else. The generated catalogue lists
# them as reserved. Where each name comes from:
#   OUTSIDE_LOOKBACK     tj-r6vcgv's UNAVAILABLE (outside the vendor's lookback), rendered OUT_OF_RANGE.
#   COVERAGE_GAP         the coverage-ledger PR; named in tj-8konfu D6.4 beside RATE_BUDGET and DEADLINE.
#   ADMISSION_REFUSED    the streaming PR; tj-8konfu D6.4, rendered RESOURCE_EXHAUSTED.
#   SUBSCRIBER_TOO_SLOW  the streaming PR; tj-8konfu D6.4, rendered RESOURCE_EXHAUSTED.
RESERVED_REASONS: Final[tuple[str, ...]] = (
    'OUTSIDE_LOOKBACK',
    'COVERAGE_GAP',
    'ADMISSION_REFUSED',
    'SUBSCRIBER_TOO_SLOW',
)


class Outcome(StrEnum):
    """The failure outcomes of D3. The third outcome, SERVED, is a success and never an error's."""

    # Permanent for the request as asked: do not retry it unchanged.
    REFUSED = 'REFUSED'
    # The answer could exist later. Carries reset_at where the server can name one.
    NOT_READY = 'NOT_READY'


class Disposition(StrEnum):
    """What the platform does about a failure, decided by its reason (D4)."""

    # Operational: page a human.
    PAGE = 'PAGE'
    # Expected: record it and move on.
    RECORD = 'RECORD'
    # The caller's input was wrong, and the caller fixes it.
    CLIENT_FIX = 'CLIENT_FIX'


# The CLOSED allowlist of metadata keys (D8), each matching AIP-193's [a-z][a-zA-Z0-9-_]+. D8 names the
# first seven. This vocabulary adds three, each for a recorded reason:
#   reset_at       when a rate-limit window resets: the user's requirement (tj-0pobey.2, 20:31 UTC 2026-10-01).
#   colliding_ids  the caller's own entry ids, which tj-vhboky.8's 409 body requires.
#   error_id       the id that ties the wire to the cause chain in the log (D8).
# NEVER a key, and never a value under one: a DSN or credential, an account identifier, a raw vendor body,
# a stack trace, a strategy name, a target, or a jurisdiction-specific label. This repository is public.
METADATA_KEYS: Final = frozenset(
    {
        'asset_symbol',
        'range_start',
        'range_end',
        'feed',
        'granularity',
        'vendor',
        'retry_after',
        'reset_at',
        'colliding_ids',
        'error_id',
    }
)

# Allowlisted, but carried by the reset_at attribute rather than the metadata argument. Each renderer writes
# both from it. retry_after is derived at render time and never stored, because a stored copy goes stale.
_DERIVED_METADATA_KEYS: Final = frozenset({'reset_at', 'retry_after'})


def new_error_id() -> str:
    """Return a new error_id, fresh on every call: the text of a uuid4.

    The one spelling an id is minted in, by either transport's edge or by a raise site that logs its own cause
    chain. Every typed answer carries an error_id, the error's own or one its edge mints, and the log line that
    holds the cause chain names the same id (D8, as its 16:22 UTC 2026-10-02 addendum reads it through).

    Returns:
        str: The id, such as '0f8c2a4e-5b1d-4c3a-9e7f-2d6b8a1c4e5f'.
    """
    return str(uuid.uuid4())


_ONE_SECOND: Final = timedelta(seconds=1)

# Filled while the two branch classes below are defined, and never again.
_BRANCHES: set[type] = set()


def _utc_now() -> datetime:
    return datetime.now(UTC)


class TraderJoeError(Exception):
    """The base of every error the platform raises on purpose. Never raised itself: raise a branch or a leaf.

    It carries a reason, a human detail, allowlisted metadata, and when the condition is expected to clear.

    detail is for a human and is never parsed (RFC 9457 s3.1.4). It never holds a secret, a DSN, a raw
    vendor body, an account identifier or a stack trace (D8). The cause chain reaches the log through
    `raise ... from e`, and never reaches the wire.

    retry_after is not stored. It is derived from reset_at on every read, through the clock the error was
    built with, so an error relayed across a hop never carries a delay that went stale on the way.
    """

    def __init_subclass__(cls, *, _branch: bool = False, **kwargs: object) -> None:
        """Refuse a class that would deepen the hierarchy past a branch and its leaves (D5).

        Only this module declares a branch. Every other subclass is a leaf, and a leaf subclasses exactly one
        branch directly.

        Args:
            _branch: Set only on ExogenousError and InvalidRequestError.
            **kwargs: Passed on to the next __init_subclass__.

        Raises:
            TypeError: If the class subclasses a leaf, the base itself, or both branches.
        """
        super().__init_subclass__(**kwargs)
        if _branch and cls.__module__ == __name__:
            _BRANCHES.add(cls)
            return
        parents = [base for base in cls.__bases__ if issubclass(base, TraderJoeError)]
        if len(parents) == 1 and parents[0] in _BRANCHES:
            return
        raise TypeError(
            f'{cls.__qualname__} subclasses {", ".join(base.__qualname__ for base in parents)}; a leaf subclasses '
            'exactly one of ExogenousError or InvalidRequestError directly, and nothing subclasses a leaf '
            '(ADR tj-fa1rpu D5)'
        )

    def __init__(
        self,
        reason: Reason,
        detail: str,
        *,
        metadata: Mapping[str, str | Sequence[str]] | None = None,
        reset_at: datetime | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        """Build an error for one reason, refusing anything the vocabulary does not allow.

        Args:
            reason: Why the request failed. It must belong to this class's branch in REASONS.
            detail: Human-facing text, never parsed, never carrying anything D8 forbids.
            metadata: Machine-readable context. Keys come from METADATA_KEYS, except reset_at and
                retry_after, which come from the reset_at argument. Each value is a str or a sequence of
                str: the raiser formats it, so every renderer writes the same text.
            reset_at: When the condition is expected to clear, timezone aware. It is stored in UTC. A
                reason whose row sets requires_reset_at must have one, and a REFUSED reason must not,
                since no wait cures a permanent refusal (D3).
            clock: The source of now from which retry_after is derived, injectable for tests.

        Raises:
            TypeError: If this is TraderJoeError itself, the reason is not a Reason, the reason belongs to
                the other branch, or detail, a metadata value or reset_at has the wrong type.
            ValueError: If a metadata key is not allowed here, reset_at is naive, or reset_at is missing
                where the reason requires it or present where the reason is REFUSED.
        """
        if type(self) is TraderJoeError:
            raise TypeError(
                'TraderJoeError is never raised itself; raise ExogenousError, InvalidRequestError or a leaf'
            )
        if not isinstance(reason, Reason):
            raise TypeError(f'reason must be a Reason member, not {type(reason).__name__}')
        spec = REASONS[reason]
        if not isinstance(self, spec.branch):
            raise TypeError(f'{reason} belongs to {spec.branch.__name__}, so {type(self).__qualname__} cannot carry it')
        if not isinstance(detail, str):
            raise TypeError(f'detail must be a str, not {type(detail).__name__}')
        super().__init__(reason, detail)
        self._reason = reason
        self._detail = detail
        self._metadata = _checked_metadata(metadata)
        self._reset_at = _checked_reset_at(reason, spec, reset_at)
        self._clock = clock

    @classmethod
    def from_retry_after(
        cls,
        reason: Reason,
        detail: str,
        retry_after: float,
        *,
        metadata: Mapping[str, str | Sequence[str]] | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> Self:
        """Build an error that clears retry_after seconds from now, so reset_at = now + retry_after.

        For a delay given relative to now, such as an HTTP Retry-After header. Only for a class that keeps
        this base's constructor signature.

        Args:
            reason: As for the constructor.
            detail: As for the constructor.
            retry_after: Seconds from now until the condition is expected to clear, at least 0.
            metadata: As for the constructor.
            clock: The source of now, for both reset_at and the retry_after later derived from it.

        Returns:
            Self: The error, with reset_at fixed at construction.

        Raises:
            ValueError: If retry_after is negative, NaN or infinite, or as the constructor raises.
        """
        if not math.isfinite(retry_after) or retry_after < 0:
            raise ValueError(f'retry_after must be a finite number of seconds, at least 0, not {retry_after!r}')
        reset_at = clock() + timedelta(seconds=retry_after)
        return cls(reason, detail, metadata=metadata, reset_at=reset_at, clock=clock)

    @property
    def reason(self) -> Reason:
        """Reason: Why the request failed."""
        return self._reason

    @property
    def detail(self) -> str:
        """str: Human-facing text, never parsed."""
        return self._detail

    @property
    def metadata(self) -> Mapping[str, str | tuple[str, ...]]:
        """Mapping[str, str | tuple[str, ...]]: The allowlisted context, read-only, without reset_at or retry_after."""
        return self._metadata

    @property
    def reset_at(self) -> datetime | None:
        """Datetime | None: When the condition is expected to clear, in UTC, or None where nobody can say."""
        return self._reset_at

    @property
    def retry_after(self) -> int | None:
        """Int | None: Whole seconds until reset_at, rounded up and never negative, or None without reset_at.

        Derived on every read from the error's clock, never stored.
        """
        if self._reset_at is None:
            return None
        remaining = self._reset_at - self._clock()
        # Rounded up in whole microseconds rather than through a float, so the delay is never short.
        return max(0, -(-remaining // _ONE_SECOND))

    def __str__(self) -> str:
        """The reason and the detail, for the log."""
        return f'{self._reason}: {self._detail}'


class ExogenousError(TraderJoeError, _branch=True):
    """Expected, outside our control, and must be signalled (D5).

    A vendor or peer that cannot be reached or that refuses, a rate budget that cannot admit the call in
    time, a database that fails: it could always happen, whatever the caller does.
    """


class InvalidRequestError(TraderJoeError, _branch=True):
    """The caller asked for something that cannot be asked (D5)."""


# The METADATA_KEYS key D8's correlation id travels under, on an error and on either transport's wire.
_ERROR_ID_KEY: Final = 'error_id'

# The ONE separator a sequence-valued metadata item is flattened with, wherever one string is needed: the
# error_id in a log line, below, and every sequence-valued ErrorInfo metadata value on the gRPC hop, whose
# _SEQUENCE_SEPARATOR is this constant rather than a second spelling of it. A comma and nothing else, because
# that join is the one wire-visible one, and the id in each service's log must be the id on the wire.
METADATA_SEQUENCE_SEPARATOR: Final = ','


def own_error_id(error: TraderJoeError) -> str | None:
    """Return the error_id the error already carries, as one line of text for the log, or None where it has none.

    An error has an id of its own because a raise site that logged its cause chain set it, or because an edge
    kept a peer's id when it converted that peer's answer back. BOTH TRANSPORTS ASK THIS ONE FUNCTION, so an
    error relayed from one to the other is judged the same way on both and is named by the same string in
    either service's log -- which is the correlation D8 exists to give an operator.

    A value that names nothing is no id: '', an empty sequence, or a sequence of nothing but '', all three of
    which TraderJoeError accepts. A sequence that does name something is joined with
    METADATA_SEQUENCE_SEPARATOR, the spelling the gRPC hop writes on the wire.

    This is each edge's ONE test of 'has its own id': the body or status it sends, the line it logs and its
    decision whether to log the cause chain all ask this 'is None', so the id sent and the id named are always
    the same, and the chain is logged exactly when the edge minted the id.

    Args:
        error: The error to read.

    Returns:
        str | None: The id, or None when the error carries none.
    """
    own = error.metadata.get(_ERROR_ID_KEY)
    if isinstance(own, str):
        return own or None
    if own is None or not any(own):
        return None
    return METADATA_SEQUENCE_SEPARATOR.join(own)


def has_cause_chain(exc: BaseException) -> bool:
    """Return whether a traceback logged under this exception would show how it arose.

    Both edges decide exc_info with this, so neither can come to answer it differently from the other.
    'raise ... from e' sets __cause__. A raise inside an except block sets __context__, unless 'from None'
    suppressed it.

    Args:
        exc: The exception to inspect.

    Returns:
        bool: True when the exception has a cause chain worth logging.
    """
    return exc.__cause__ is not None or (exc.__context__ is not None and not exc.__suppress_context__)


# Every canonical gRPC status code name a failure can render as: all of them but OK.
_GRPC_FAILURE_CODES: Final = frozenset(
    {
        'CANCELLED',
        'UNKNOWN',
        'INVALID_ARGUMENT',
        'DEADLINE_EXCEEDED',
        'NOT_FOUND',
        'ALREADY_EXISTS',
        'PERMISSION_DENIED',
        'RESOURCE_EXHAUSTED',
        'FAILED_PRECONDITION',
        'ABORTED',
        'OUT_OF_RANGE',
        'UNIMPLEMENTED',
        'INTERNAL',
        'UNAVAILABLE',
        'DATA_LOSS',
        'UNAUTHENTICATED',
    }
)


@dataclass(frozen=True)
class ReasonSpec:
    """One row of REASONS: everything the platform decides from a reason, in one place (D4).

    Attributes:
        branch: The one class whose errors may carry the reason, ExogenousError or InvalidRequestError.
            REASONS[reason].branch(reason, detail) builds an error of the right branch.
        outcome: REFUSED or NOT_READY (D3). This is the retry instruction; there is no retryable flag.
        disposition: Page an operator, record and move on, or leave the caller to fix the request.
        grpc_code: The canonical gRPC status code NAME it renders as, or None where it is never rendered as
            a status. A name, not grpc.StatusCode, because this module imports no grpc. Never UNAVAILABLE.
        http_status: The HTTP status it renders as at the edge.
        summary: One plain-English line: what happened and what the caller should do. It feeds the
            generated docs/errors.md (tj-3mk3u5.37.10) and is never put on the wire.
        requires_reset_at: Whether every error with this reason carries reset_at. True for the two rate
            limits, which always say when the window resets.
    """

    branch: type[ExogenousError] | type[InvalidRequestError]
    outcome: Outcome
    disposition: Disposition
    grpc_code: str | None
    http_status: int
    summary: str
    requires_reset_at: bool = False

    def __post_init__(self) -> None:
        """Refuse a row that a renderer or the catalogue could not use as written.

        Raises:
            ValueError: If the branch is not one of the two branches, grpc_code is UNAVAILABLE or not a
                canonical failure code, http_status is not a 4xx or 5xx, the summary is empty or more than
                one line, or a REFUSED reason requires reset_at.
        """
        if self.branch not in (ExogenousError, InvalidRequestError):
            raise ValueError(f'branch must be ExogenousError or InvalidRequestError, not {self.branch!r}')
        if self.grpc_code == 'UNAVAILABLE':
            raise ValueError(
                'no reason renders as UNAVAILABLE: the server never returns it deliberately (tj-8konfu D6.4)'
            )
        if self.grpc_code is not None and self.grpc_code not in _GRPC_FAILURE_CODES:
            raise ValueError(f'grpc_code {self.grpc_code!r} is not a canonical gRPC failure code name')
        if not 400 <= self.http_status <= 599:
            raise ValueError(f'http_status {self.http_status} is not a 4xx or 5xx status')
        if not self.summary.strip() or len(self.summary.splitlines()) != 1:
            raise ValueError(f'summary must be one non-empty line, not {self.summary!r}')
        if self.requires_reset_at and self.outcome is Outcome.REFUSED:
            raise ValueError('a REFUSED reason names no wait, so it cannot require reset_at')


def _checked_metadata(metadata: Mapping[str, str | Sequence[str]] | None) -> Mapping[str, str | tuple[str, ...]]:
    checked: dict[str, str | tuple[str, ...]] = {}
    for key, value in (metadata or {}).items():
        if key not in METADATA_KEYS:
            raise ValueError(f'metadata key {key!r} is not in METADATA_KEYS (ADR tj-fa1rpu D8)')
        if key in _DERIVED_METADATA_KEYS:
            raise ValueError(f'metadata key {key!r} is set from reset_at, never through metadata')
        if isinstance(value, str):
            checked[key] = value
        elif isinstance(value, Sequence) and all(isinstance(item, str) for item in value):
            checked[key] = tuple(value)
        else:
            raise TypeError(f'metadata {key!r} must be a str or a sequence of str, not {type(value).__name__}')
    return MappingProxyType(checked)


def _checked_reset_at(reason: Reason, spec: ReasonSpec, reset_at: datetime | None) -> datetime | None:
    if reset_at is None:
        if spec.requires_reset_at:
            raise ValueError(f'{reason} always carries reset_at, the time its window resets')
        return None
    if not isinstance(reset_at, datetime):
        raise TypeError(f'reset_at must be a datetime, not {type(reset_at).__name__}')
    if reset_at.utcoffset() is None:
        raise ValueError('reset_at must be timezone aware; a naive datetime names no instant')
    if spec.outcome is Outcome.REFUSED:
        raise ValueError(f'{reason} is REFUSED, which no wait cures, so it carries no reset_at (ADR tj-fa1rpu D3)')
    return reset_at.astimezone(UTC)


# THE ONE TABLE (D4): one row per Reason, reviewable in a diff. The comment above each row names what
# raises it and the task that wires the raise. A grpc_code of None is never rendered as a gRPC status:
# FEED_NOT_AVAILABLE travels in-band on the FetchDataset ack (tj-3mk3u5.22 Q5), the PEER_* reasons and
# DEADLINE are produced on the client side of a hop, and the DATABASE_* reasons arise in data_store, which
# answers over HTTP. No row is UNAVAILABLE (tj-8konfu D6.4).
REASONS: Final[Mapping[Reason, ReasonSpec]] = MappingProxyType(
    {
        # REFUSED
        # FastAPI request validation (TE-3).
        Reason.INVALID_REQUEST: ReasonSpec(
            branch=InvalidRequestError,
            outcome=Outcome.REFUSED,
            disposition=Disposition.CLIENT_FIX,
            grpc_code='INVALID_ARGUMENT',
            http_status=422,
            summary='The request did not match its declared schema; correct the fields the error lists and send it again.',
        ),
        # Both UnsupportedAssetType copies, collapsed (C5, TE-6); ingest's crypto, option, QUOTE and TRADE stubs (TE-4).
        Reason.UNSUPPORTED_ASSET_TYPE: ReasonSpec(
            branch=InvalidRequestError,
            outcome=Outcome.REFUSED,
            disposition=Disposition.CLIENT_FIX,
            grpc_code='INVALID_ARGUMENT',
            http_status=422,
            summary='The asset type or data type asked for is not supported; ask for a supported one instead.',
        ),
        # BrokerUnsupportedError for a currency or exchange the broker cannot serve (TE-4).
        Reason.UNSUPPORTED_INSTRUMENT: ReasonSpec(
            branch=InvalidRequestError,
            outcome=Outcome.REFUSED,
            disposition=Disposition.CLIENT_FIX,
            grpc_code='INVALID_ARGUMENT',
            http_status=422,
            summary='The broker cannot serve the instrument as specified, such as its currency or exchange; change the instrument and ask again.',
        ),
        # The reader refusing a named feed (TE-4). In-band on the FetchDataset ack, never a status (tj-3mk3u5.22 Q5).
        Reason.FEED_NOT_AVAILABLE: ReasonSpec(
            branch=InvalidRequestError,
            outcome=Outcome.REFUSED,
            disposition=Disposition.CLIENT_FIX,
            grpc_code=None,
            http_status=422,
            summary='This deployment cannot serve the market-data feed asked for; ask for a feed it serves, or leave the feed unset.',
        ),
        # A vendor 400 naming a request parameter invalid, such as Alpaca's 'end should not be before start'. The
        # vendor's message becomes the detail and is never parsed (TE-4; Q-EMPTY).
        Reason.VENDOR_INVALID_REQUEST: ReasonSpec(
            branch=InvalidRequestError,
            outcome=Outcome.REFUSED,
            disposition=Disposition.CLIENT_FIX,
            grpc_code='INVALID_ARGUMENT',
            http_status=422,
            summary='The market-data vendor refused the request as invalid, such as an end before its start; correct the request and send it again.',
        ),
        # Ingest's pre-check: the range starts at or after the reader's clock. Refused before any vendor call or
        # rate token (TE-4; Q-EMPTY).
        Reason.RANGE_IN_FUTURE: ReasonSpec(
            branch=InvalidRequestError,
            outcome=Outcome.REFUSED,
            disposition=Disposition.CLIENT_FIX,
            grpc_code='INVALID_ARGUMENT',
            http_status=422,
            summary='The range asked for starts at or after the current time, so there is no history to fetch; check the dates and their time zone.',
        ),
        # A vendor 4xx other than 400, 401, 403 and 429. Alpaca documents none, so one is a surprise worth a look (TE-4).
        Reason.VENDOR_REJECTED: ReasonSpec(
            branch=ExogenousError,
            outcome=Outcome.REFUSED,
            disposition=Disposition.PAGE,
            grpc_code='INVALID_ARGUMENT',
            http_status=422,
            summary='The market-data vendor refused the request for a reason it does not document; do not retry it unchanged until an operator has looked.',
        ),
        # EntryNotFound (TE-6).
        Reason.NOT_FOUND: ReasonSpec(
            branch=InvalidRequestError,
            outcome=Outcome.REFUSED,
            disposition=Disposition.CLIENT_FIX,
            grpc_code='NOT_FOUND',
            http_status=404,
            summary='The entity named in the request does not exist; check the identifier, or create the entity first.',
        ),
        # OwnerMismatch (TE-6).
        Reason.OWNER_MISMATCH: ReasonSpec(
            branch=InvalidRequestError,
            outcome=Outcome.REFUSED,
            disposition=Disposition.CLIENT_FIX,
            grpc_code='PERMISSION_DENIED',
            http_status=403,
            summary='The entity is not owned by the principal the request declares; act only on entities that principal owns.',
        ),
        # OwnOverlapConflict, with colliding_ids (TE-6).
        Reason.OWN_OVERLAP_CONFLICT: ReasonSpec(
            branch=InvalidRequestError,
            outcome=Outcome.REFUSED,
            disposition=Disposition.CLIENT_FIX,
            grpc_code='ALREADY_EXISTS',
            http_status=409,
            summary='The request overlaps datasets the same owner already holds, listed in colliding_ids; extend one of those instead.',
        ),
        # RangeCollision, with colliding_ids (TE-6).
        Reason.RANGE_COLLISION: ReasonSpec(
            branch=InvalidRequestError,
            outcome=Outcome.REFUSED,
            disposition=Disposition.CLIENT_FIX,
            grpc_code='ALREADY_EXISTS',
            http_status=409,
            summary='Growing the range would make the dataset identical to another the same owner holds, listed in colliding_ids; use that one instead.',
        ),
        # RangeShrink (TE-6).
        Reason.RANGE_SHRINK: ReasonSpec(
            branch=InvalidRequestError,
            outcome=Outcome.REFUSED,
            disposition=Disposition.CLIENT_FIX,
            grpc_code='FAILED_PRECONDITION',
            http_status=409,
            summary="The update would shrink a dataset's range, and ranges only grow; delete the dataset and create a smaller one.",
        ),
        # DuplicateBatchTimestamp (TE-6).
        Reason.DUPLICATE_BAR_TIMESTAMP: ReasonSpec(
            branch=InvalidRequestError,
            outcome=Outcome.REFUSED,
            disposition=Disposition.CLIENT_FIX,
            grpc_code='INVALID_ARGUMENT',
            http_status=422,
            summary='The batch carries two bars with the same timestamp; remove the duplicate and send the batch again.',
        ),
        # IntegrityError through write_transaction (TE-6).
        Reason.DATABASE_INTEGRITY: ReasonSpec(
            branch=ExogenousError,
            outcome=Outcome.REFUSED,
            disposition=Disposition.PAGE,
            grpc_code=None,
            http_status=409,
            summary='The database refused the write with a constraint violation the service did not foresee; do not retry it unchanged until an operator has looked.',
        ),
        # NOT_READY
        # The service's own rate budget cannot admit the call within the caller's deadline, U4 (TE-4).
        Reason.RATE_BUDGET: ReasonSpec(
            branch=ExogenousError,
            outcome=Outcome.NOT_READY,
            disposition=Disposition.RECORD,
            grpc_code='FAILED_PRECONDITION',
            http_status=429,
            summary="The service's own rate budget for the vendor cannot admit the request within its deadline; retry once reset_at has passed.",
            requires_reset_at=True,
        ),
        # A vendor 429 after the SDK's own retries (TE-4).
        Reason.VENDOR_RATE_LIMITED: ReasonSpec(
            branch=ExogenousError,
            outcome=Outcome.NOT_READY,
            disposition=Disposition.PAGE,
            grpc_code='FAILED_PRECONDITION',
            http_status=429,
            summary='The market-data vendor rate-limited the request even after retrying it; retry once reset_at has passed.',
            requires_reset_at=True,
        ),
        # A vendor 5xx or 504, a connection error, a vendor timeout, or a connection cut while the body is read (TE-4).
        Reason.VENDOR_UNAVAILABLE: ReasonSpec(
            branch=ExogenousError,
            outcome=Outcome.NOT_READY,
            disposition=Disposition.RECORD,
            grpc_code='FAILED_PRECONDITION',
            http_status=503,
            summary='The market-data vendor could not be reached, timed out, failed on its side, or its answer was cut off; retry later.',
        ),
        # MissingCredentialsError, or a vendor 401 or 403 (TE-4).
        Reason.VENDOR_AUTH: ReasonSpec(
            branch=ExogenousError,
            outcome=Outcome.NOT_READY,
            disposition=Disposition.PAGE,
            grpc_code='FAILED_PRECONDITION',
            http_status=503,
            summary="The service's credentials for the market-data vendor are missing or were refused; retry after an operator has fixed them.",
        ),
        # Client side: the transport's DEADLINE_EXCEEDED (TE-2).
        Reason.DEADLINE: ReasonSpec(
            branch=ExogenousError,
            outcome=Outcome.NOT_READY,
            disposition=Disposition.RECORD,
            grpc_code='FAILED_PRECONDITION',
            http_status=504,
            summary='A call to another service did not finish within its deadline; retry later, asking for less at once if it keeps happening.',
        ),
        # Client side: the transport's UNAVAILABLE (TE-2).
        Reason.PEER_UNAVAILABLE: ReasonSpec(
            branch=ExogenousError,
            outcome=Outcome.NOT_READY,
            disposition=Disposition.PAGE,
            grpc_code=None,
            http_status=503,
            summary='Another service this request needs could not be reached, usually because it is down or restarting; retry later.',
        ),
        # Client side: the peer answered INTERNAL (TE-2).
        Reason.PEER_INTERNAL: ReasonSpec(
            branch=ExogenousError,
            outcome=Outcome.NOT_READY,
            disposition=Disposition.PAGE,
            grpc_code=None,
            http_status=502,
            summary='Another service this request needs failed with an internal error, logged under error_id; retry later, and quote error_id if it persists.',
        ),
        # Client side: a reply that will not map, including an unknown reason (tj-fa1rpu C8; TE-2, tj-3mk3u5.28).
        Reason.PEER_PROTOCOL_ERROR: ReasonSpec(
            branch=ExogenousError,
            outcome=Outcome.NOT_READY,
            disposition=Disposition.PAGE,
            grpc_code=None,
            http_status=502,
            summary='Another service sent a reply this one could not interpret, usually a version mismatch between them; retry once both run the same release.',
        ),
        # OperationalError, InterfaceError or a disconnect through write_transaction (TE-6).
        Reason.DATABASE_UNAVAILABLE: ReasonSpec(
            branch=ExogenousError,
            outcome=Outcome.NOT_READY,
            disposition=Disposition.PAGE,
            grpc_code=None,
            http_status=503,
            summary='The database could not be reached or dropped the connection; retry later.',
        ),
        # A serialization failure or deadlock, SQLSTATE 40001 or 40P01, through write_transaction (TE-6).
        Reason.DATABASE_CONFLICT: ReasonSpec(
            branch=ExogenousError,
            outcome=Outcome.NOT_READY,
            disposition=Disposition.RECORD,
            grpc_code=None,
            http_status=503,
            summary='The write collided with a concurrent transaction and was rolled back; retry it.',
        ),
    }
)


# AIP-193: a reason matches [A-Z][A-Z0-9_]+[A-Z0-9] in at most 63 characters, and a metadata key matches
# [a-z][a-zA-Z0-9-_]+ (the hyphen is moved last here so the class reads unambiguously).
_REASON_PATTERN: Final = re.compile(r'[A-Z][A-Z0-9_]+[A-Z0-9]')
_REASON_MAX_LENGTH: Final = 63
_METADATA_KEY_PATTERN: Final = re.compile(r'[a-z][a-zA-Z0-9_-]+')


def _check_vocabulary() -> None:
    """Refuse at import a vocabulary that breaks its own rules, so no build can ship one.

    Raises:
        ValueError: If a reason's name is not its value or not AIP-193 shaped, the table is not total over
            Reason, a reserved name is malformed, repeated or taken by a member, or a metadata key is
            malformed.
    """
    for reason in Reason:
        if reason.name != reason.value:
            raise ValueError(f'Reason.{reason.name} has the value {reason.value!r}; a reason is named by its value')
        if not _REASON_PATTERN.fullmatch(reason.value) or len(reason.value) > _REASON_MAX_LENGTH:
            raise ValueError(f'reason {reason.value!r} is not AIP-193 UPPER_SNAKE of at most {_REASON_MAX_LENGTH}')
    missing = [reason.value for reason in Reason if reason not in REASONS]
    if missing:
        raise ValueError(f'REASONS has no row for {", ".join(missing)}; the table is total over Reason')
    if len(set(RESERVED_REASONS)) != len(RESERVED_REASONS):
        raise ValueError('RESERVED_REASONS names a reason twice')
    for name in RESERVED_REASONS:
        if not _REASON_PATTERN.fullmatch(name) or len(name) > _REASON_MAX_LENGTH:
            raise ValueError(f'reserved reason {name!r} is not AIP-193 UPPER_SNAKE of at most {_REASON_MAX_LENGTH}')
        if name in Reason.__members__:
            raise ValueError(f'{name} is reserved for a later PR and cannot be a Reason member yet')
    malformed = sorted(key for key in METADATA_KEYS if not _METADATA_KEY_PATTERN.fullmatch(key))
    if malformed:
        raise ValueError(f'metadata keys {malformed} do not match AIP-193 [a-z][a-zA-Z0-9-_]+')


_check_vocabulary()
