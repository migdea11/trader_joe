"""The problem+json envelope every non-2xx answer carries, asserted in one place (TE-7).

WHY THIS IS A MODULE AND NOT A CONFTEST FIXTURE. These are constants and a plain assertion
helper, not per-test state, and the system tests already import shared code by its full dotted
path (tests.fakes.market_data, data.store.tests.fetch_double). Putting them in conftest.py would
mean importing conftest by dotted path, which pytest loads a second time under its own name and
is a known way to end up with two copies of a module's state.

WHAT CHANGED AND WHY THESE EXIST AT ALL. Before TE-6 (caf68f5) a failure from data_store was
FastAPI's default {"detail": ...} -- for a 422, a LIST of validation errors. Every one of these
system tests read that shape. TE-6 made every failure render as RFC 9457 problem details, so the
envelope is now type/title/status/detail/reason/domain/error_id with the 422's per-field issues
moved to an `errors` member. The assertions had to move with it, and the hazard in moving them is
that a test repointed from one shape to another can end up asserting THAT PROBLEM+JSON EXISTS
while no longer asserting what it originally guarded. The helper below exists so the envelope is
checked identically everywhere and each test's own assertions stay visible beside it.

WHAT THIS HELPER CANNOT CATCH, said here rather than discovered later. It checks that the
envelope's members are PRESENT and CORRECT; it does not assert that nothing else is there. A
renderer that grew a new member would not red anything through this function. That is the right
trade for a failure body a client is told to tolerate unknown members in (the ProblemDetails
docstring says exactly that) -- but it is the wrong trade in one place, so one caller does not
use this helper at all: test_http_write_secret's 401 keeps WHOLE-BODY EQUALITY, because what
that test exists to prove is that the response carries NOTHING ELSE, no secret and no echo of
the request. Absence is the assertion there, so it cannot be delegated to a membership check.

THE TWO RULINGS THIS ENCODES:
  * `detail` is prose a client never parses (tj-8feral). It is not asserted here, and tests
    should pin what a caller branches on instead. The 409 guard settled the pattern: assert the
    sentence NAMES every colliding id, never its rendering.
  * A reason's row can change the status while the reason stays the same, and a client branches
    on the reason. So reason is a required argument here rather than an optional extra.
"""

from typing import Any


# RESTATED, NOT IMPORTED, and deliberately. routers/common/errors.py keeps these in a private
# _PROBLEM_MEDIA_TYPE and in ERROR_DOMAIN; importing them would make these tests agree with the
# renderer by construction and prove nothing about the wire. These literals are what a
# non-Python client actually reads, so they are written out as that client would see them.
PROBLEM_JSON = 'application/problem+json'
ERROR_DOMAIN = 'trader-joe'


def assert_problem(response: Any, *, status: int, reason: str, title: str, describe: Any = None) -> dict:
    """Assert the problem+json envelope of a failure and return the body for per-case assertions.

    Args:
        response: The httpx response.
        status: The HTTP status the reason's row names.
        reason: The exact reason the caller branches on.
        title: The HTTP status phrase, which RFC 9457 s4.2.1 requires when type is about:blank.
        describe: Optional callable returning a redacted rendering of the response, used as the
            assertion message. Passed rather than imported so this module needs no fixture.

    Returns:
        dict: The parsed body, so a caller can go on to assert its own members.
    """
    context = describe(response) if describe else f'HTTP {response.status_code}: {response.text}'
    assert response.status_code == status, context
    assert response.headers['content-type'] == PROBLEM_JSON, context
    body = response.json()
    assert body['type'] == 'about:blank', context
    assert body['title'] == title, context
    assert body['status'] == status, context
    assert body['reason'] == reason, context
    assert body['domain'] == ERROR_DOMAIN, context
    return body
