"""Logging helpers: the logger factory, the registered-value scrub and the broker log sink.

Design: tj-vhboky.41 ADDENDUM 2 (M5), tj-7zjov1.74.

THE SCRUB. register_secret() adds a value (a vendor credential read from the environment) to a
process-wide registry. ScrubFormatter wraps a handler's existing formatter and replaces every
registered form in the FINAL formatted text -- message, traceback and stack included -- with the
REDACTED marker from common.sensitive. install_scrub() wraps the formatter of every handler on
the root logger, on uvicorn and uvicorn.access, and on the broker sink. get_logger() and
startup_logs() both call it, so a server started some other way is covered too.
Scrubbing the final text is deliberate: a record filter misses the traceback, and a filter on a
logger misses records propagated from a child.

Forms registered per value: raw, stripped, the repr-escaped inner text, the JSON-escaped inner
text, and the bytes-repr inner text of its UTF-8 encoding (httpx and httpcore print bytes).

LOG LEVEL. The root logger and the broker sink handler are INFO unless the environment variable
LOG_LEVEL is DEBUG (any other value is INFO). Read when the logger or the sink is configured.

THE BROKER SINK. The third-party HTTP and broker libraries (BROKER_LOGGERS) get one handler of
their own with propagate False, so their records leave the root handler. With no path the sink is a
stderr stream (the same container stream, separate in routing and format only); with a path it is
a size-bounded RotatingFileHandler created mode 0600. Child loggers such as
urllib3.connectionpool need no entry. Records of OUR loggers about a broker failure (scheduler
executor log.exception, the common.rpc.errors boundary, instance) are NOT taken by the sink: they
stay on the root handler, where the scrub covers them.

WHAT THIS DOES NOT COVER:
- Literal forms only: base64, split or truncated values pass.
- Values registered before the record is FORMATTED only: a record emitted before register_secret
  ran is not scrubbed.
- Handlers added after install_scrub ran (a later handler, a test capture handler) are not wrapped.
- HTTP bodies, gRPC status details, delivery trailer details and database rows.
- Fields that flow through, such as owner (tj-7zjov1.69 R5b).
- pytest attaches its capture handlers only to loggers that are already non-propagating when a
  test phase starts, so whether caplog misses the broker loggers depends on timing; absence
  assertions through caplog on them are unreliable. caplog's own handler is never wrapped by the
  scrub, so it holds unscrubbed text.
- At LOG_LEVEL=DEBUG the httpcore trace double-escapes backslashes, so a value with a CR or LF in
  the middle leaves both halves visible. get_client refuses such a value before any client is
  built, and no httpx sender in the repo now carries a vendor credential. A new sender that did
  would need the same check_credential gate first.
- This module is no longer stdlib-only: it imports common.sensitive for the marker.
"""

import json
import logging
import logging.handlers
import os
import threading
from typing import Any

from common.sensitive import REDACTED


# Suppress grpc logs: grpc.aio logs DEBUG on every call, which would also bias the latency harness's gRPC timings
logging.getLogger('grpc').setLevel(logging.WARNING)

LOG_LEVEL_VAR = 'LOG_LEVEL'
"""Environment variable that switches the root logger and the broker sink to DEBUG (default INFO)."""

BROKER_LOGGERS = ('alpaca', 'requests', 'urllib3', 'httpx', 'httpcore')
"""Third-party loggers routed to the broker sink."""

_SCRUBBED_LOGGERS = ('', 'uvicorn', 'uvicorn.access', *BROKER_LOGGERS)
_SINK_FORMAT = 'BROKER %(asctime)s %(levelname)-8s %(name)s  %(message)s'
_SINK_MAX_BYTES = 10 * 1024 * 1024
_SINK_BACKUPS = 5

_lock = threading.Lock()
_forms: set[str] = set()
_ordered: tuple[str, ...] = ()
_sink: logging.Handler | None = None


def _level() -> int:
    # Read from os.environ: common.environment imports this module, so it cannot be used here.
    return logging.DEBUG if os.environ.get(LOG_LEVEL_VAR, '').strip().upper() == 'DEBUG' else logging.INFO


def register_secret(value: str) -> None:
    """Register a value to be removed from every formatted log line.

    Idempotent. An empty or whitespace-only value registers nothing. Nothing about the value is
    logged, returned or counted.

    Args:
        value (str): The secret to scrub.
    """
    global _ordered
    if not value.strip():
        return
    forms = {
        value,
        value.strip(),
        repr(value)[1:-1],
        json.dumps(value)[1:-1],
        repr(value.encode('utf-8', 'backslashreplace'))[2:-1],
    }
    with _lock:
        _forms.update(form for form in forms if form.strip())
        _ordered = tuple(sorted(_forms, key=len, reverse=True))


def _scrub(text: str) -> str:
    for form in _ordered:
        if form in text:
            text = text.replace(form, REDACTED)
    return text


class ScrubFormatter(logging.Formatter):
    """Formatter that wraps another and removes every registered form from its output."""

    def __init__(self, inner: logging.Formatter | None = None) -> None:
        """Wrap a formatter.

        Args:
            inner (logging.Formatter | None): The handler's existing formatter. None means the
                handler had none, so logging.Formatter() renders what logging itself would: the
                plain message.
        """
        super().__init__()
        self.inner = inner or logging.Formatter()

    def format(self, record: logging.LogRecord) -> str:
        """Format with the inner formatter, then scrub the full text."""
        return _scrub(self.inner.format(record))


def install_scrub() -> None:
    """Wrap the formatter of every handler on root, uvicorn, uvicorn.access and the broker loggers.

    Idempotent: a handler already wrapped is left alone.
    """
    for name in _SCRUBBED_LOGGERS:
        for handler in logging.getLogger(name).handlers:
            if not isinstance(handler.formatter, ScrubFormatter):
                handler.setFormatter(ScrubFormatter(handler.formatter))


def configure_broker_sink(path: str | None) -> logging.Handler:
    """Attach the broker sink to each of BROKER_LOGGERS and stop their propagation to root.

    Idempotent: later calls return the existing handler, whatever the path.

    Args:
        path (str | None): Rotating log file (created mode 0600), or None for a stderr stream.

    Returns:
        logging.Handler: The sink handler.
    """
    global _sink
    with _lock:
        if _sink is not None:
            return _sink
        handler: logging.Handler
        if path:
            old_umask = os.umask(0o077)
            try:
                handler = logging.handlers.RotatingFileHandler(
                    path, maxBytes=_SINK_MAX_BYTES, backupCount=_SINK_BACKUPS, encoding='utf-8'
                )
            finally:
                os.umask(old_umask)
        else:
            handler = logging.StreamHandler()
        handler.setFormatter(ScrubFormatter(logging.Formatter(_SINK_FORMAT)))
        handler.setLevel(_level())
        for name in BROKER_LOGGERS:
            broker_logger = logging.getLogger(name)
            broker_logger.addHandler(handler)
            broker_logger.propagate = False
        _sink = handler
        return handler


def get_logger(name: str) -> logging.Logger:
    """Get the logger.

    Args:
        name (str): Logger name (usually __name__).

    Returns:
        logging.Logger: Logger instance.
    """
    logging.basicConfig(level=_level(), format='%(levelname)-8s [%(filename)s]  %(message)s')
    install_scrub()
    return logging.getLogger(name)


def limit(message: Any, limit: int = 200) -> str:
    """Util to truncate a message to a certain limit.

    Args:
        message (Any): The message to truncate.
        limit (int, optional): Max message length. Defaults to 200.

    Returns:
        str: _description_
    """
    if isinstance(message, str) is False:
        message = str(message)
    if len(message) > limit:
        return message[: (limit - 3)] + '...'
    return message
