"""The instance write secret: a FastAPI dependency for dataset WRITE routes (tj-vhboky.1 s5).

>>> WHAT THIS IS, AND -- MORE IMPORTANTLY -- WHAT IT IS NOT <<<

A SINGLE SHARED SECRET IN THE ENVIRONMENT AUTHENTICATES THE DEPLOYMENT, NOT THE CALLER. With one
key there is effectively one principal, so "only the owner may edit a dataset" is NOT enforced
against anyone holding the key: whoever has it may declare any principal they like and write to
any dataset. It is enforced against MISTAKES. The caller declares its principal on the request,
the server checks the dataset's owner matches it, and an honest caller cannot accidentally clobber
another strategy's dataset. That is the whole of what this buys, and it is what was asked for --
the user, choosing it over per-principal keys: "just a key in an env should be enough at least
short term."

Per-principal keys are the later version of the same mechanism -- same comparison, more rows --
and they are cheap only because the owner field and the declared principal exist from the start.

THIS DOES NOT CLOSE tj-glqs4r (services publish on all interfaces with default or no auth). It
guards the dataset write path and nothing else. Every other surface is exactly as exposed as it
was, and both services are still reachable on every interface they bind.

FAIL CLOSED. An unset or empty secret REJECTS every write; it never means "allow everything". The
variable ships empty in .env.default, so unconfigured is the normal state until someone generates
one -- which makes this the ordinary path through the code, not an edge case.

READS ARE DELIBERATELY OPEN. Apply this to write routes one at a time, as a per-route dependency.
Never put it in a router-wide ``dependencies=[...]`` list, where a GET added later would silently
inherit it and break the open-read design without anyone editing this file.

THE DECLARED PRINCIPAL HAS EXACTLY ONE SOURCE: the ``owner`` field on the request body, already in
the schema (tj-vhboky.2). There is deliberately NO principal header and therefore no precedence
rule to get wrong -- two sources for one fact is how a caller ends up authenticating as one
principal and writing as another. This dependency does not read the principal at all; matching the
declared owner against the stored dataset's owner is a crud concern and lives in data_store.

NEVER LOG THE SECRET. Nothing below interpolates the header value or the environment value into a
log record, an error body or an exception message, and nothing added here may start: this project
has leaked a credential into a public build log twice (tj-d5jjtm, tj-10jczr).

WHY THIS LIVES IN routers/common AND NOT IN common/: it raises HTTPException and reads a request
header, which are HTTP transport concerns. routers/ already imports common/ and fastapi, so this
placement adds no import direction. Putting it in common/ would push an HTTP status code into the
library that the gRPC layer, the SQLAlchemy layer and the migrations all import, for no gain.
"""

import hmac
from typing import Annotated

from fastapi import Header, HTTPException, status

from common.environment import get_env_var
from common.logging import get_logger


log = get_logger(__name__)

# Named once, here, so the router that applies this dependency and the tests that exercise it
# cannot disagree about the spelling. The ``X-`` prefix is deprecated by RFC 6648, and is kept
# anyway: it is the prevailing spelling for a bespoke credential header, and it makes clear at a
# glance that this is not Authorization and not any standard scheme.
# The value is the header's NAME -- public, and sent on every request. bandit's B105 fires only
# because the constant's name contains SECRET and the value is a literal; nothing is compared
# against it. The suppression names B105 rather than being bare, so any other bandit check on
# this line still fires. A literal secret assigned here would NOT be caught -- that is exactly
# B105 -- which is why the value must stay a header name.
INSTANCE_SECRET_HEADER = 'X-Instance-Secret'  # nosec B105

# Reserved, empty, in .env.default. Read LAZILY inside the dependency (see below), never at import.
# The value is the VARIABLE's name, not the secret it holds; the secret itself is read from the
# environment inside require_instance_secret and appears as a literal nowhere in this tree. B105
# as above.
INSTANCE_SECRET_ENV_VAR = 'INSTANCE_WRITE_SECRET'  # nosec B105

# ONE message for every cause of rejection -- secret not configured, header absent, header wrong.
# Distinguishing them in the response would turn the endpoint into an oracle that tells a caller
# whether the deployment has a secret at all, and how close a guess was. The operator gets the
# distinction in the logs instead, where the cause is useful and the caller cannot see it.
# The value is the text sent to a rejected caller -- deliberately public, and deliberately the
# same for every cause. B105 as above.
INSTANCE_SECRET_REJECTION_DETAIL = 'Invalid or missing instance write secret'  # nosec B105


async def require_instance_secret(
    instance_secret: Annotated[str | None, Header(alias=INSTANCE_SECRET_HEADER)] = None,
) -> None:
    """Reject a write request that does not carry the deployment's instance secret.

    Read the module docstring before relying on this: it authenticates THE DEPLOYMENT, NOT THE
    CALLER, and so protects against mistakes rather than against anyone holding the key.

    The environment read is LAZY, inside the function body. Reading it at import time would make
    every module that imports this one -- and every test that imports those -- unimportable
    without the variable set, which common/CLAUDE.md records as this component's pitfall 1. It
    also means a rotated secret takes effect on the next request rather than on the next restart.

    The comparison is ``hmac.compare_digest`` and not ``==``. A plain ``==`` on a credential
    short-circuits at the first differing byte, which leaks the shared prefix by timing and lets
    it be recovered a byte at a time. Both sides are encoded to bytes first: ``compare_digest``
    raises TypeError on a str containing non-ASCII, and the header is attacker-controlled.
    Length is still observable -- that is inherent to the primitive, not an oversight here.

    Args:
        instance_secret (str | None): The secret as sent in the header, or None when absent.

    Raises:
        HTTPException: 401, with a fixed detail, when the secret is unset, empty, absent from the
            request, or does not match. The secret itself never appears in the response.
    """
    expected_secret = get_env_var(INSTANCE_SECRET_ENV_VAR, default='')
    if not expected_secret:
        # Fail closed, and say why -- to the operator, in the logs. An unconfigured deployment
        # that silently accepted writes is the failure this whole dependency exists to prevent.
        log.error(
            f'{INSTANCE_SECRET_ENV_VAR} is unset or empty: rejecting every dataset write. '
            f'Generate one and set it in .env before this deployment can write.'
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=INSTANCE_SECRET_REJECTION_DETAIL)

    if instance_secret is None or not hmac.compare_digest(instance_secret.encode(), expected_secret.encode()):
        # No value, no length, no prefix: a rejected credential is still a credential, and this
        # line is the one most likely to be "helpfully" given the header value by a later reader.
        log.warning(f'Dataset write rejected: {INSTANCE_SECRET_HEADER} was absent or did not match')
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=INSTANCE_SECRET_REJECTION_DETAIL)
