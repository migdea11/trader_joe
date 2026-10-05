"""Reading a problem+json answer in data_store's tests, with the envelope asserted on the way through.

WHY A SHARED READER AND NOT ``response.json()['errors']`` IN FORTY PLACES (validator, gating
tj-3mk3u5.37.8). TE-6 turned every non-2xx this service answers into RFC 9457 problem details, so
forty-four assertions that read ``response.json()['detail']`` as a list of validation errors had to
move to ``['errors']``. Moving an accessor forty-four times is the cheapest possible repair and it
would have thrown away the actual news: that the ENVELOPE around those errors is now a declared
contract. A test that only reaches for the errors array cannot tell problem+json from the FastAPI
body it replaced -- same list, different wrapper -- so every such test would have gone on passing
if the handlers were uninstalled and ``detail`` renamed.

So the accessor asserts the envelope. Each of the repointed cases now also pins, for free and in one
place, that the answer is served as application/problem+json, that it names reason INVALID_REQUEST
with its domain, that status in the body agrees with the status on the response, and that it carries
an error_id an operator can quote. Uninstalling the handler set reds all of them.

WHAT IT DELIBERATELY DOES NOT ASSERT: the detail sentence's wording, which is prose and is pinned
once in routers/tests, and the errors array's contents, which is each caller's own subject.
"""

from typing import Any


# RFC 9457. The media type is the half of D1(c) that a body-shape assertion cannot see: a service
# answering the right JSON under application/json is still not serving problem details.
PROBLEM_MEDIA_TYPE = 'application/problem+json'

# The vocabulary every reason of ours belongs to. A body without it is not one of our errors.
TRADER_JOE_DOMAIN = 'trader-joe'


def problem(response: Any, *, status: int, reason: str | None = None) -> dict:
    """The problem+json body of a non-2xx answer, with the envelope checked.

    Args:
        response: The httpx response from a TestClient call.
        status: The HTTP status the answer must carry, in the body and on the response.
        reason: The Reason value the body must name, or None to assert only that the envelope is
            well formed. None is for answers that are deliberately reason-less -- a route that does
            not exist, a refused write secret -- which ProblemDetails documents as absent, not null.

    Returns:
        dict: The parsed body.
    """
    assert response.status_code == status, f'answered {response.status_code}, not {status}: {response.text}'
    media_type = response.headers.get('content-type', '')
    assert media_type.startswith(PROBLEM_MEDIA_TYPE), (
        f'the answer is served as {media_type!r}, not {PROBLEM_MEDIA_TYPE!r}. Every non-2xx this '
        f'service answers is RFC 9457 problem details (ADR tj-fa1rpu D1(c)), and the media type is '
        f"how a client knows to parse it as one rather than as the endpoint's own error shape."
    )
    body = response.json()
    assert body['status'] == status, f'the body says status {body["status"]} and the response says {status}: {body}'
    assert body['title'], f'the body carries no title: {body}'
    if reason is not None:
        assert body.get('reason') == reason, f'the body names reason {body.get("reason")!r}, not {reason!r}: {body}'
        assert body.get('domain') == TRADER_JOE_DOMAIN, f'the body names domain {body.get("domain")!r}: {body}'
        assert body.get('error_id'), (
            f'the body carries no error_id, so an operator reading this answer has nothing to quote '
            f'and no way to find the failure in the logs (D8): {body}'
        )
    return body


def validation_errors(response: Any) -> list[dict]:
    """The ``errors`` array of a 422, with the problem+json envelope checked.

    The replacement for ``response.json()['detail']``, which under FastAPI's native 422 WAS the list
    of validation problems and is now the human sentence. Reading the array through here is what
    makes the forty-odd repointed cases assert the new envelope rather than merely survive it.

    Args:
        response: The httpx response from a TestClient call.

    Returns:
        list[dict]: One entry per problem, each with loc, msg and type.
    """
    body = problem(response, status=422, reason='INVALID_REQUEST')
    errors = body.get('errors')
    assert isinstance(errors, list) and errors, (
        f'a 422 carries no errors array, so a caller cannot see WHICH field is wrong: {body}'
    )
    # D8: the input and its validation context are deliberately not carried. FastAPI's native 422
    # includes `input`, which echoes the caller's value back -- and on this service a rejected body
    # can hold an owner. The leak is invisible to a test that only reads loc/msg/type, so it is
    # asserted here, once, over every caller of this helper.
    leaked = sorted({key for error in errors for key in error} - {'loc', 'msg', 'type'})
    assert leaked == [], (
        f'the 422 errors carry {leaked}, beyond the loc/msg/type a ValidationIssue declares. '
        f"FastAPI's native 422 echoes the rejected `input` back to the caller, and on this service "
        f'that can be an owner (D8).'
    )
    return errors
