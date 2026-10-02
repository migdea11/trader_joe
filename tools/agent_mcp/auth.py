"""Bearer-token auth for the MCP endpoint: the token file, and an ASGI gate in front of the whole app.

Pure ASGI and the standard library, no MCP import, so the dev venv can test it. The token is never
logged, never echoed in a response, and compared in constant time.
"""

import hmac
import os
import secrets
import stat
from collections.abc import Awaitable, Callable, MutableMapping
from pathlib import Path
from typing import Any


Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

# secrets.token_urlsafe(32) is 43 characters; anything much shorter was not written by this module.
MIN_TOKEN_LENGTH = 32


class TokenFileError(Exception):
    """The token file exists but cannot be trusted; the server refuses to start."""


def load_or_create_token(path: Path) -> str:
    """Return the bearer token, generating it at first start.

    A new token is secrets.token_urlsafe(32), written 0600 with O_EXCL so a file that appeared in
    the meantime is never overwritten. An existing file must be a regular file (not a symlink),
    readable by its owner only, and hold a token of plausible length.
    """
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return _read_token(path)
    token = secrets.token_urlsafe(32)
    with os.fdopen(descriptor, 'w') as handle:
        handle.write(token + '\n')
    return token


def _read_token(path: Path) -> str:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise TokenFileError(f'{path} is not a regular file')
    if info.st_mode & 0o077:
        raise TokenFileError(f'{path} is readable by others; it must be 0600')
    token = path.read_text().strip()
    if len(token) < MIN_TOKEN_LENGTH:
        raise TokenFileError(f'{path} does not hold a token; delete it to have one generated')
    return token


class BearerTokenMiddleware:
    """Refuse every HTTP request without `Authorization: Bearer <token>` -- before the app sees it.

    A refused request reaches nothing behind this gate, so it has no side effect. The token is
    accepted in the Authorization header only: not a query parameter, not another header. Lifespan
    events pass through; any other scope (websocket) is closed.
    """

    def __init__(self, app: ASGIApp, token: str):
        if len(token) < MIN_TOKEN_LENGTH:
            raise ValueError('refusing to guard the server with a short token')
        self.app = app
        self._expected = f'Bearer {token}'.encode()

    def authorized(self, scope: Scope) -> bool:
        """True when the request carries exactly one Authorization header, equal to the token's."""
        values = [value for name, value in scope.get('headers', []) if name.lower() == b'authorization']
        return len(values) == 1 and hmac.compare_digest(values[0], self._expected)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] == 'lifespan':
            await self.app(scope, receive, send)
            return
        if scope['type'] != 'http':
            await send({'type': 'websocket.close', 'code': 1008})
            return
        if not self.authorized(scope):
            await _unauthorized(send)
            return
        await self.app(scope, receive, send)


async def _unauthorized(send: Send) -> None:
    body = b'{"error": "unauthorized"}'
    await send(
        {
            'type': 'http.response.start',
            'status': 401,
            'headers': [
                (b'content-type', b'application/json'),
                (b'content-length', str(len(body)).encode()),
                (b'www-authenticate', b'Bearer'),
            ],
        }
    )
    await send({'type': 'http.response.body', 'body': body})
