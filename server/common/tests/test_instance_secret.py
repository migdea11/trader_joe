"""The instance write secret dependency: the four properties it was specified to have.

WHY THIS FILE EXISTS (validator, gating tj-vhboky.3). Commit ee57267 added
routers/common/instance_secret.py and no test -- 112 insertions in one file. The builder verified
the four properties by execution and reported the output, which is the right thing to do and is
not the same thing as leaving a regression detector behind. Under ADR tj-8fxxfb the tests are the
validator's, so they are here rather than sent back.

DRIVEN THROUGH A REAL APP, NOT BY CALLING THE FUNCTION. require_instance_secret is a FastAPI
dependency, and half of what it promises is about the HTTP boundary: the status code, the response
body carrying no secret, and a hostile header producing 401 rather than 500. Calling the coroutine
directly would assert none of that and would not exercise the Header() binding, which is where an
alias typo would live.

WHAT IS STILL NOT PROVEN HERE, and it is the operationally important half: that a real
unauthenticated write against the running service is refused. No route is wired to this dependency
yet -- tj-vhboky.8 does that -- so there is nothing end-to-end to drive. That belongs to
tj-vhboky.14. This file proves the mechanism; it does not prove the mechanism is installed.

THE SECRETS BELOW ARE TEST FIXTURES and deliberately look nothing like a real one.
"""

import logging

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from routers.common import instance_secret as instance_secret_module
from routers.common.instance_secret import (
    INSTANCE_SECRET_ENV_VAR,
    INSTANCE_SECRET_HEADER,
    INSTANCE_SECRET_REJECTION_DETAIL,
    require_instance_secret,
)


pytestmark = pytest.mark.common

CONFIGURED_SECRET = 'test-secret-not-a-real-credential'
WRONG_SECRET = 'test-secret-not-a-real-credentiaL'  # one byte different, at the END


@pytest.fixture
def client() -> TestClient:
    """One write route carrying the dependency, and one read route without it.

    The read route is not decoration: "reads are deliberately open" is a design property of this
    dependency, and a route-level dependency that somehow became app-wide would still pass every
    other test in this file.
    """
    app = FastAPI()

    @app.post('/write', dependencies=[Depends(require_instance_secret)])
    async def write() -> dict[str, str]:
        return {'result': 'written'}

    @app.get('/read')
    async def read() -> dict[str, str]:
        return {'result': 'read'}

    return TestClient(app, raise_server_exceptions=False)


def test_an_unset_secret_rejects_the_write(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """Fail closed, property 1. Absent must never mean allow.

    This is the ordinary path, not an edge case: .env.default ships INSTANCE_WRITE_SECRET empty,
    so an unconfigured deployment is the normal state until someone generates one.
    """
    monkeypatch.delenv(INSTANCE_SECRET_ENV_VAR, raising=False)

    response = client.post('/write', headers={INSTANCE_SECRET_HEADER: CONFIGURED_SECRET})

    assert response.status_code == 401
    assert response.json()['detail'] == INSTANCE_SECRET_REJECTION_DETAIL


def test_an_empty_secret_rejects_the_write(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """Fail closed, property 1, the shape .env.default actually ships.

    Set-but-empty is a DIFFERENT code path from unset -- get_env_var returns '' rather than the
    default -- so an implementation checking only `is None` would pass the test above and fail here.
    """
    monkeypatch.setenv(INSTANCE_SECRET_ENV_VAR, '')

    response = client.post('/write', headers={INSTANCE_SECRET_HEADER: CONFIGURED_SECRET})

    assert response.status_code == 401


def test_the_matching_secret_is_accepted(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """The negative control. Without this, a dependency that rejected everything would pass."""
    monkeypatch.setenv(INSTANCE_SECRET_ENV_VAR, CONFIGURED_SECRET)

    response = client.post('/write', headers={INSTANCE_SECRET_HEADER: CONFIGURED_SECRET})

    assert response.status_code == 200
    assert response.json() == {'result': 'written'}


def test_a_missing_header_rejects_the_write(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """A configured deployment with no credential presented."""
    monkeypatch.setenv(INSTANCE_SECRET_ENV_VAR, CONFIGURED_SECRET)

    response = client.post('/write')

    assert response.status_code == 401


def test_a_wrong_secret_rejects_the_write(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """Differs from the real secret only in its LAST byte.

    A comparison that short-circuits still returns the right answer here -- the point is that a
    near-miss is rejected at all, and that the fixture does not accidentally pass by differing in
    length, which is the one thing compare_digest does not hide.
    """
    monkeypatch.setenv(INSTANCE_SECRET_ENV_VAR, CONFIGURED_SECRET)

    response = client.post('/write', headers={INSTANCE_SECRET_HEADER: WRONG_SECRET})

    assert response.status_code == 401


def test_a_non_ascii_header_is_rejected_rather_than_crashing(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """The one thing the implementation did BEYOND its brief, and the reason it was right.

    hmac.compare_digest raises TypeError on a str containing non-ASCII. The header is
    attacker-controlled and Starlette decodes it as latin-1, so a single 0xFF byte becomes a
    non-ASCII str. Comparing the strs directly would turn that into an unhandled TypeError and a
    500 -- which both breaks the fixed-response rule (500 vs 401 distinguishes this input class
    from a wrong guess) and hands an unauthenticated caller a way to raise an exception inside the
    dependency. Encoding both sides first is what makes this a 401.

    Sent as raw bytes: an ASCII-only str would not reach the branch under test.
    """
    monkeypatch.setenv(INSTANCE_SECRET_ENV_VAR, CONFIGURED_SECRET)

    response = client.post('/write', headers={INSTANCE_SECRET_HEADER: b'\xff\xfe-not-utf8'})

    assert response.status_code == 401, 'a hostile header produced something other than a clean 401'
    assert response.json()['detail'] == INSTANCE_SECRET_REJECTION_DETAIL


def test_the_comparison_goes_through_compare_digest_on_two_bytes_values(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """Property 3, the timing one, and the ONLY property in this file that no behaviour can show.

    WHY A SPY AND NOT AN OBSERVATION. Every other test here asserts an outcome, which is the better
    instrument when one exists. For constant-time comparison there is none: replacing
    hmac.compare_digest(a.encode(), b.encode()) with a plain `a == b` passes all eleven of the other
    tests in this file. The near-miss case returns 401 either way -- that is what a near miss means
    -- and the non-ASCII case stays green too, because `==` accepts a non-ASCII str where
    compare_digest raises. So the timing property rested on code review alone, and a
    readability-motivated `==` refactor would have removed it silently.

    A WALL-CLOCK MEASUREMENT IS NOT THE ALTERNATIVE. Timing two comparisons and asserting they are
    close is flaky on shared CI and proves nothing about a short secret anyway. The primitive being
    called is the property worth pinning; constant-time-ness is the standard library's job.

    TWO PROPERTIES IN ONE ASSERTION SET, both of them decisions:
      1. the comparison goes through compare_digest at all -- red on a `==` refactor;
      2. BOTH sides are encoded to bytes first -- red on dropping either .encode(). Today that is
         caught only indirectly, by the non-ASCII header producing 401 instead of a 500.

    The spy delegates to the real primitive, so this test drives the genuine accept path rather
    than a stubbed one, and the 200 below is the guard's real answer.

    ASCII-ONLY SECRETS ARE THE DOCUMENTED INTENT, not an oversight: Starlette decodes the header as
    latin-1 while this comparison encodes UTF-8, so a non-ASCII configured secret can never
    authenticate -- it fails closed (the deployment cannot write) rather than accepting the wrong
    value, and widening the comparison to match would widen what a credential check accepts.
    """
    monkeypatch.setenv(INSTANCE_SECRET_ENV_VAR, CONFIGURED_SECRET)

    calls: list[tuple[object, object]] = []
    real_compare_digest = instance_secret_module.hmac.compare_digest

    def spying_compare_digest(presented: object, expected: object) -> bool:
        calls.append((presented, expected))
        return real_compare_digest(presented, expected)

    # Patched where the dependency looks the name up. A refactor to `from hmac import compare_digest`
    # removes this attribute and errors here, which is also a red -- the name is part of the pin.
    monkeypatch.setattr(instance_secret_module.hmac, 'compare_digest', spying_compare_digest)

    response = client.post('/write', headers={INSTANCE_SECRET_HEADER: CONFIGURED_SECRET})

    assert response.status_code == 200, 'the spy changed the outcome, so it is not measuring the real path'
    assert len(calls) == 1, f'the credential comparison did not go through hmac.compare_digest: {len(calls)} calls'
    presented, expected = calls[0]
    assert isinstance(presented, bytes), f'the presented header was not encoded to bytes: {type(presented).__name__}'
    assert isinstance(expected, bytes), f'the expected secret was not encoded to bytes: {type(expected).__name__}'


def test_every_rejection_gives_the_identical_response(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """The response must not be an oracle, property 4's public half.

    Three different causes -- not configured, header absent, header wrong. If any of them answered
    differently, a caller could learn whether the deployment has a secret at all, and how close a
    guess was. Compared as whole (status, body) pairs rather than per-field.
    """
    monkeypatch.delenv(INSTANCE_SECRET_ENV_VAR, raising=False)
    not_configured = client.post('/write', headers={INSTANCE_SECRET_HEADER: CONFIGURED_SECRET})

    monkeypatch.setenv(INSTANCE_SECRET_ENV_VAR, CONFIGURED_SECRET)
    header_absent = client.post('/write')
    header_wrong = client.post('/write', headers={INSTANCE_SECRET_HEADER: WRONG_SECRET})

    answers = {(r.status_code, r.text) for r in (not_configured, header_absent, header_wrong)}
    assert len(answers) == 1, f'the rejection distinguishes its causes to the caller: {answers}'


def test_the_secret_never_reaches_a_log_record(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """Property 4. This project has leaked a credential into a public build log twice.

    Both values are checked: the CONFIGURED one (which the code holds) and the SUPPLIED one (which
    a "helpful" later edit would most likely add to the rejection warning). Asserted over the
    formatted message, not over args, because a leak via %-args formats into the output all the
    same.
    """
    monkeypatch.setenv(INSTANCE_SECRET_ENV_VAR, CONFIGURED_SECRET)

    with caplog.at_level(logging.DEBUG):
        client.post('/write', headers={INSTANCE_SECRET_HEADER: WRONG_SECRET})

    assert caplog.records, 'nothing was logged at all -- this test would pass vacuously'
    logged = '\n'.join(record.getMessage() for record in caplog.records)
    assert CONFIGURED_SECRET not in logged, 'the configured secret reached a log record'
    assert WRONG_SECRET not in logged, 'the supplied credential reached a log record'


def test_the_secret_never_reaches_the_response_body(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """The other half of property 4: not in an error body, not in an exception message."""
    monkeypatch.setenv(INSTANCE_SECRET_ENV_VAR, CONFIGURED_SECRET)

    response = client.post('/write', headers={INSTANCE_SECRET_HEADER: WRONG_SECRET})

    assert CONFIGURED_SECRET not in response.text
    assert WRONG_SECRET not in response.text


def test_the_environment_is_read_per_request_not_once(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """Property 2, stated as the behaviour laziness buys rather than as "the read is indented".

    Two requests, different environment, no re-import and no new app. An import-time read would
    serve the first value to both -- and would additionally make every importer of this module
    unimportable without the variable set (common/CLAUDE.md pitfall 1). It also means a rotated
    secret takes effect on the next request instead of the next restart.
    """
    monkeypatch.setenv(INSTANCE_SECRET_ENV_VAR, CONFIGURED_SECRET)
    assert client.post('/write', headers={INSTANCE_SECRET_HEADER: CONFIGURED_SECRET}).status_code == 200

    monkeypatch.setenv(INSTANCE_SECRET_ENV_VAR, 'test-secret-rotated-to-something-else')
    assert client.post('/write', headers={INSTANCE_SECRET_HEADER: CONFIGURED_SECRET}).status_code == 401


def test_the_read_route_is_untouched_by_the_write_guard(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """Open reads are a design property of this dependency, so they get an assertion.

    Worth pinning even though this app is the test's own: the failure mode being guarded against
    is someone moving the dependency into a router-wide dependencies=[...] list, where a GET added
    later inherits it silently. A test that only ever drove the write route would not notice.
    """
    monkeypatch.delenv(INSTANCE_SECRET_ENV_VAR, raising=False)

    response = client.get('/read')

    assert response.status_code == 200, 'the write guard is being applied to reads'
