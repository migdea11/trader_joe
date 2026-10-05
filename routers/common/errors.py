"""problem+json at the HTTP edge: the one renderer and the one handler set (ADR tj-fa1rpu D1(c), D5, D8, U3).

Every non-2xx a service answers over HTTP is an RFC 9457 problem details object, served as
application/problem+json. This module defines that body, renders a TraderJoeError into it, and provides the
handlers that turn everything else an app can raise into one too. It INSTALLS NOTHING: each app calls
install_error_handlers(app) and passes PROBLEM_RESPONSES as FastAPI(responses=...) in its own task.

THE BODY (U3 as amended 2026-10-02, the user's ruling on Q-URI tj-3mk3u5.37.2):
    type      "about:blank" for every error. No type URI is derived or published.
    title     the HTTP status phrase, because RFC 9457 s4.2.1 says so when the type is about:blank.
    status    the HTTP status, repeated in the body.
    detail    the error's human sentence. Clients never parse it (RFC 9457 s3.1.4).
    reason    the Reason value. CLIENTS BRANCH ON THIS, never on type, title or detail (D4, D10).
    domain    ERROR_DOMAIN, "trader-joe". (domain, reason) is the error's identity.
    error_id  the id the failure is logged under. Always present: the error's own when it carries one, else one
              minted here with new_error_id (D8, as its 16:22 UTC 2026-10-02 addendum reads it through).
    and, where the error carries them, the other METADATA_KEYS members: retry_after and reset_at (with a
    Retry-After header) for an error that knows when it clears, colliding_ids and the other allowlisted keys.
    Per-reason prose belongs in the generated docs/errors.md (tj-3mk3u5.37.10), never in title.

THE HANDLERS (D1(c): one set per app):
    TraderJoeError          rendered by render(). The one log line names the error_id. It carries the cause
                            chain only when the id was minted here: an error that arrives with an id was logged,
                            chain and all, by whoever set it.
    RequestValidationError  422 INVALID_REQUEST, with an error_id, and an errors member giving each problem's loc,
                            msg and type. Never the input or ctx: the request body is never echoed (D8).
    HTTPException           about:blank with the exception's own status, detail and headers. This covers
                            routing's 404 and 405 and require_instance_secret's fixed 401, unconverted.
    Exception               a bug (D5): 500 carrying ONLY an error_id. The traceback goes to the log under the
                            same id, and never to the wire (D8).

WHY HERE AND NOT IN common/: like instance_secret.py, this is an HTTP transport concern. common/errors stays
standard-library only and knows no protocol; the gRPC renderer in common/rpc is the other reader of it. What
is NOT a transport concern lives there and is called from here: own_error_id and has_cause_chain ask nothing
about HTTP, and both edges must answer them identically or the same failure is named by two different ids in
the two services' logs (tj-zxqn4r).
"""

import http
import logging
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.constants import REF_PREFIX
from fastapi.responses import JSONResponse, Response
from pydantic import AwareDatetime, BaseModel, Field, NonNegativeInt
from starlette.exceptions import HTTPException

from common.errors.vocabulary import (
    ERROR_DOMAIN,
    METADATA_KEYS,
    REASONS,
    Disposition,
    InvalidRequestError,
    Reason,
    TraderJoeError,
    has_cause_chain,
    new_error_id,
    own_error_id,
)
from common.logging import get_logger


log = get_logger(__name__)

_PROBLEM_MEDIA_TYPE: Final = 'application/problem+json'

# RFC 9457 s4.2.1: the type that says nothing beyond the HTTP status. Every error uses it (Q-URI ruling).
_ABOUT_BLANK: Final = 'about:blank'

_INTERNAL_SERVER_ERROR: Final = http.HTTPStatus.INTERNAL_SERVER_ERROR

# The one sentence a validation failure carries as its detail. What failed is in the errors member.
_VALIDATION_DETAIL: Final = 'The request does not match its declared schema; each entry in errors names one problem.'

# How loudly the edge logs a TraderJoeError it renders, from the reason's disposition (D4). One line, naming the
# error_id, and carrying the cause chain only when the edge minted that id.
_LOG_LEVELS: Final[Mapping[Disposition, int]] = MappingProxyType(
    {Disposition.PAGE: logging.ERROR, Disposition.RECORD: logging.WARNING, Disposition.CLIENT_FIX: logging.INFO}
)

# The METADATA_KEYS key D8's correlation id travels under, on the error and on the wire.
_ERROR_ID_KEY: Final = 'error_id'

# The value of a metadata member. common/errors lets every allowlisted key hold a str or a sequence of str
# (TE-1, tj-3mk3u5.37.3), so each member takes both: a str renders as a string, and the tuple the error stores
# renders as a list. Narrower, and render() would raise for an error TE-1 lets be built.
_MetadataValue = str | list[str]


class ValidationIssue(BaseModel):
    """One problem FastAPI's request validation found. Deliberately without the input or its context (D8)."""

    loc: list[str | int] = Field(
        description="Where the problem is: the request part ('body', 'query', 'path', 'header'), then the field path."
    )
    msg: str = Field(description='What is wrong with the value, for a human.')
    type: str = Field(description="The validator's error type, such as 'missing' or 'int_parsing'.")


class ProblemDetails(BaseModel):
    """The body of every non-2xx answer: RFC 9457 problem details, served as application/problem+json.

    A member that does not apply is absent, never null. Clients branch on reason and must tolerate a reason, or
    a member, they have never seen (D10; RFC 9457 s3.2): neither is declared closed here. Each metadata member
    is a string or a list of strings, whichever the error was built with.
    """

    # 'type' is declared a str and not the constant 'about:blank', so that a derived type URI, should one ever be
    # added, stays an additive change for a generated client (U3 addendum, 2026-10-02: reversible).
    type: str = Field(
        default=_ABOUT_BLANK,
        description='Always "about:blank": the problem has no semantics beyond its status. Branch on reason instead.',
    )
    title: str = Field(description='The HTTP status phrase, such as "Not Found". Never parsed.')
    status: int = Field(description='The HTTP status code of this response.')
    detail: str | None = Field(default=None, description='What happened this time, for a human. Never parsed.')
    # A str and not an enum of today's reasons, so that a client generated from this document survives a reason
    # added after it was built (D10).
    reason: str | None = Field(
        default=None,
        description='Why the request failed: an UPPER_SNAKE value from the closed Reason vocabulary, catalogued in '
        'docs/errors.md. Absent when the failure is not one of ours (a route that does not exist, a refused write '
        'secret, an internal error).',
    )
    domain: str | None = Field(default=None, description='The vocabulary reason belongs to: "trader-joe".')
    retry_after: NonNegativeInt | None = Field(
        default=None,
        description='Whole seconds until the condition is expected to clear, as in the Retry-After header. Present '
        'exactly when reset_at is.',
    )
    reset_at: AwareDatetime | None = Field(
        default=None, description='When the condition is expected to clear, in RFC 3339 UTC.'
    )
    error_id: _MetadataValue | None = Field(
        default=None, description='The id an operator finds this failure under in the logs. Quote it when reporting.'
    )
    colliding_ids: _MetadataValue | None = Field(
        default=None, description="The ids of the caller's own datasets that the request collides with."
    )
    asset_symbol: _MetadataValue | None = Field(default=None, description='The asset symbol the failure concerns.')
    range_start: _MetadataValue | None = Field(default=None, description='The start of the range the failure concerns.')
    range_end: _MetadataValue | None = Field(default=None, description='The end of the range the failure concerns.')
    feed: _MetadataValue | None = Field(default=None, description='The market-data feed the failure concerns.')
    granularity: _MetadataValue | None = Field(default=None, description='The bar granularity the failure concerns.')
    vendor: _MetadataValue | None = Field(default=None, description='The market-data vendor the failure concerns.')
    errors: list[ValidationIssue] | None = Field(
        default=None, description='Each problem request validation found. Present only with reason INVALID_REQUEST.'
    )


def _check_members() -> None:
    """Refuse at import a body that cannot carry every allowlisted metadata key, so no build can drop one.

    Raises:
        ValueError: If a METADATA_KEYS key is not a ProblemDetails member, or a disposition has no log level.
    """
    missing = sorted(METADATA_KEYS - set(ProblemDetails.model_fields))
    if missing:
        raise ValueError(f'ProblemDetails has no member for the metadata keys {missing}')
    unlevelled = sorted(disposition.value for disposition in Disposition if disposition not in _LOG_LEVELS)
    if unlevelled:
        raise ValueError(f'_LOG_LEVELS has no level for {unlevelled}')


_check_members()


class _ProblemResponse(JSONResponse):
    media_type = _PROBLEM_MEDIA_TYPE


def _status_phrase(status: int) -> str:
    try:
        return http.HTTPStatus(status).phrase
    except ValueError:
        # Only an HTTPException raised with a status the standard library does not know reaches this.
        return f'HTTP {status}'


def _respond(problem: ProblemDetails, headers: Mapping[str, str] | None = None) -> JSONResponse:
    return _ProblemResponse(
        problem.model_dump(mode='json', exclude_none=True), status_code=problem.status, headers=headers
    )


def _describe(
    error: TraderJoeError, error_id: str | None = None, errors: list[ValidationIssue] | None = None
) -> tuple[ProblemDetails, dict[str, str]]:
    status = REASONS[error.reason].http_status
    headers: dict[str, str] = {}
    # Read once: the header and the member must say the same number, and each read of retry_after is derived
    # afresh from the clock.
    retry_after = error.retry_after
    if retry_after is not None:
        headers['Retry-After'] = str(retry_after)
    metadata = dict(error.metadata)
    if own_error_id(error) is None:
        metadata[_ERROR_ID_KEY] = error_id or new_error_id()
    problem = ProblemDetails(
        title=_status_phrase(status),
        status=status,
        detail=error.detail,
        reason=error.reason.value,
        domain=ERROR_DOMAIN,
        retry_after=retry_after,
        reset_at=error.reset_at,
        errors=errors,
        **metadata,
    )
    return problem, headers


def render(error: TraderJoeError, *, error_id: str | None = None) -> JSONResponse:
    """Render a TraderJoeError as problem+json, with the status its reason's row names.

    type is about:blank and title is the status phrase. detail, reason, domain and error_id always appear. Every
    metadata key the error carries becomes a member of the same name. When the error knows when it clears, the
    body carries retry_after and reset_at and the response carries a Retry-After header in delta-seconds, all
    from one reading of the derived retry_after. A REFUSED reason never carries reset_at (TE-1), so a 4xx refusal
    never carries Retry-After.

    The error_id member is the error's own when its metadata holds one, and otherwise the error_id argument. A
    metadata error_id that names nothing ('', or a sequence with no non-empty str) is none. Rendering logs
    nothing: a caller that means to log under the id mints it with new_error_id and passes it in. Without an
    argument, an id is minted here, so the body carries one all the same.

    Args:
        error: The error to render.
        error_id: The id to carry when the error has none of its own.

    Returns:
        JSONResponse: An application/problem+json response.
    """
    return _respond(*_describe(error, error_id))


def _answer(request: Request, error: TraderJoeError, errors: list[ValidationIssue] | None = None) -> Response:
    # D8 at the edge: the error's own id, else one minted here, on the wire and in the one log line. The line carries
    # the cause chain only under an id minted here. An error that arrived with an id was logged, chain and all, by
    # whoever set it, so logging the chain again would only repeat it.
    own_id = own_error_id(error)
    error_id = new_error_id() if own_id is None else own_id
    response = _respond(*_describe(error, error_id, errors=errors))
    log.log(
        _LOG_LEVELS[REASONS[error.reason].disposition],
        f'{request.method} {request.url.path} -> {response.status_code} {error.reason}: {error.detail}; '
        f'error_id {error_id}',
        exc_info=error if own_id is None and has_cause_chain(error) else None,
    )
    return response


async def _on_trader_joe_error(request: Request, exc: TraderJoeError) -> Response:
    return _answer(request, exc)


async def _on_request_validation_error(request: Request, exc: RequestValidationError) -> Response:
    # Built here and never raised, so it has no cause chain to log: the line names the id and carries no traceback.
    # Not exc's chain either, whose text can carry the request's input.
    error = InvalidRequestError(Reason.INVALID_REQUEST, _VALIDATION_DETAIL)
    # loc, msg and type only. Never the input, which would echo the request body, and never ctx (D8).
    issues = [ValidationIssue(loc=list(item['loc']), msg=item['msg'], type=item['type']) for item in exc.errors()]
    return _answer(request, error, errors=issues)


async def _on_http_exception(request: Request, exc: HTTPException) -> Response:
    if exc.status_code < 200 or exc.status_code in {204, 205, 304}:
        # These statuses cannot carry a body, so there is no problem to render; Starlette does the same.
        return Response(status_code=exc.status_code, headers=exc.headers)
    # A detail that is not a str is not rendered: it could be any structure. Starlette's own HTTPException only
    # takes a str, and every raise site that routes through this handler passes one.
    detail = exc.detail if isinstance(exc.detail, str) else None
    problem = ProblemDetails(title=_status_phrase(exc.status_code), status=exc.status_code, detail=detail)
    return _respond(problem, exc.headers)


async def _on_unhandled_exception(request: Request, exc: Exception) -> Response:
    # A bug (D5). The caller gets an id and nothing else; the operator gets the traceback under the same id.
    error_id = new_error_id()
    log.error(
        f'Unhandled {type(exc).__name__} on {request.method} {request.url.path}; error_id {error_id}', exc_info=exc
    )
    problem = ProblemDetails(
        title=_INTERNAL_SERVER_ERROR.phrase, status=_INTERNAL_SERVER_ERROR.value, error_id=error_id
    )
    return _respond(problem)


def install_error_handlers(app: FastAPI) -> None:
    """Register the four problem+json exception handlers on an app (D1(c): one set per app).

    TraderJoeError renders through render(). RequestValidationError becomes 422 INVALID_REQUEST with an errors
    member. Both carry an error_id, the error's own or one minted here, and log one line naming it, with the cause
    chain only under a minted id. Starlette's HTTPException, FastAPI's included, keeps its status, detail and
    headers under type about:blank, with no error_id: it is not one of ours. Any other Exception is a bug: 500
    with only an error_id, and the traceback logged under it. Starlette still re-raises a bug after answering, so
    the server logs it too.

    Args:
        app: The application to install the handlers on.
    """
    app.add_exception_handler(TraderJoeError, _on_trader_joe_error)
    app.add_exception_handler(RequestValidationError, _on_request_validation_error)
    app.add_exception_handler(HTTPException, _on_http_exception)
    app.add_exception_handler(Exception, _on_unhandled_exception)


def _declared(description: str | None = None) -> dict[str, Any]:
    # FastAPI files a response 'model' under the route's own media type, application/json, and offers no way to
    # choose another (fastapi/openapi/utils.py, the additional-responses loop). The model is still needed: it is
    # what puts ProblemDetails into components.schemas. The problem+json entry then points at that component, so
    # the document lists application/problem+json first and application/json beside it, with the same schema.
    response: dict[str, Any] = {
        'model': ProblemDetails,
        'content': {_PROBLEM_MEDIA_TYPE: {'schema': {'$ref': f'{REF_PREFIX}{ProblemDetails.__name__}'}}},
    }
    if description is not None:
        response['description'] = description
    return response


# Pass as FastAPI(responses=PROBLEM_RESPONSES) so OpenAPI declares every non-2xx as ProblemDetails: each status a
# reason renders as, 500 for a bug, and 'default' for anything else an HTTPException carries, such as the 401 of
# require_instance_secret or routing's 405. Listing 422 also replaces FastAPI's own HTTPValidationError.
PROBLEM_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    **{
        status: _declared()
        for status in sorted({spec.http_status for spec in REASONS.values()} | {_INTERNAL_SERVER_ERROR.value})
    },
    'default': _declared('Any other failure, as RFC 9457 problem details.'),
}
