"""common/logging.py's cap on the grpc logger (tj-3mk3u5.45): grpc's per-call DEBUG goes, its WARNINGs stay.

grpc.aio logs '[_cygrpc] Loaded running loop' at DEBUG from grpc._cython.cygrpc on every call, on the
client and the server side (grpcio 1.81.1). get_logger() configures the root logger at DEBUG, so with
no cap every call through common.rpc writes a log line in both services. That buries real output and
adds per-call formatting and I/O cost to the path the latency harness's gRPC arm measures
(tj-3mk3u5.8). The cap must not hide grpc's WARNINGs: a GOAWAY or too_many_pings warning is exactly
what ADR tj-8konfu D6.2 wants seen.

Everything runs through the real code: a GrpcServerHost on 127.0.0.1, a channel from create_channel,
and Ping through common.rpc.ping, never the generated stubs. Under pytest the root logger already has
pytest's handlers, so get_logger()'s basicConfig is a no-op here; caplog at DEBUG on the root stands
in for it. The last test needs basicConfig itself, so it runs a fresh interpreter.
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from common.rpc.channel import create_channel
from common.rpc.ping import ping, ping_service
from common.rpc.server import BindAddress, GrpcServerHost
from common.tests.image_path import image_pythonpath


pytestmark = pytest.mark.common

REPO_ROOT = Path(__file__).resolve().parents[2]
LOOPBACK = '127.0.0.1'
GUARD_S = 10.0
CALLS = 3
GRPC = 'grpc'
# The logger the per-call DEBUG line comes from on grpcio 1.81.1, and where a transport warning starts.
CYGRPC = 'grpc._cython.cygrpc'


def _in_grpc(name: str) -> bool:
    return name == GRPC or name.startswith(f'{GRPC}.')


def _grpc_below_warning(records: list[logging.LogRecord]) -> list[str]:
    return [
        f'{r.name} {r.levelname}: {r.getMessage()}' for r in records if _in_grpc(r.name) and r.levelno < logging.WARNING
    ]


async def _ping_round_trips(calls: int = CALLS) -> None:
    """Serve Ping on loopback and call it `calls` times through common.rpc; the server stops whatever happens."""
    async with GrpcServerHost(BindAddress(LOOPBACK, 0), [ping_service()]) as host:
        channel = create_channel(f'{LOOPBACK}:{host.port}')
        try:
            for index in range(calls):
                message = f'quiet {index}'
                assert await asyncio.wait_for(ping(channel, message, timeout_s=5), GUARD_S) == message
        finally:
            await channel.close()


@pytest.mark.asyncio
async def test_a_ping_through_common_rpc_logs_nothing_below_warning_from_grpc(caplog: pytest.LogCaptureFixture):
    """The cap: with the root at DEBUG, as get_logger() sets it, no grpc record below WARNING is emitted."""
    caplog.set_level(logging.DEBUG)
    await _ping_round_trips()
    assert _grpc_below_warning(caplog.records) == [], 'grpc logged below WARNING on a call through common.rpc'


@pytest.mark.asyncio
async def test_the_same_ping_does_log_grpc_debug_once_the_cap_is_lifted(caplog: pytest.LogCaptureFixture):
    """The premise of the test above. Without it, a grpcio that stopped logging would leave that test asserting nothing.

    If this fails, grpc no longer logs below WARNING on a call. The cap may then be unnecessary, and
    the test above passes whether or not the cap is there.
    """
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger=GRPC)  # restored to the cap when the test ends
    await _ping_round_trips()
    assert _grpc_below_warning(caplog.records), 'grpc logged nothing below WARNING even with its logger at DEBUG'


@pytest.mark.parametrize('name', [GRPC, CYGRPC])
@pytest.mark.parametrize('level', [logging.WARNING, logging.ERROR], ids=['WARNING', 'ERROR'])
def test_a_grpc_warning_still_reaches_the_root(caplog: pytest.LogCaptureFixture, name: str, level: int):
    """The cap stops at WARNING: a transport warning from the grpc hierarchy still propagates to the root's handlers."""
    caplog.set_level(logging.DEBUG)
    logging.getLogger(name).log(level, 'GOAWAY received: too_many_pings')
    assert [(r.name, r.levelno) for r in caplog.records] == [(name, level)], (
        f'a grpc {logging.getLevelName(level)} was dropped'
    )


# Run as a fresh interpreter, the way a service starts: get_logger() runs basicConfig for real and its
# stderr handler is the one the records reach. Prints what the root's handlers received, as JSON.
_SERVICE_PROBE = """
import asyncio, json, logging, sys

from common.logging import get_logger
from common.tests.test_logging import _ping_round_trips

log = get_logger('tj45.service')
received = []


class _Received(logging.Handler):
    def emit(self, record):
        received.append([record.name, record.levelno, record.getMessage()])


logging.getLogger().addHandler(_Received())
asyncio.run(_ping_round_trips())
log.debug(sys.argv[1])
logging.getLogger(sys.argv[3]).warning(sys.argv[2])
json.dump(received, sys.stdout)
"""


def test_the_service_logging_setup_drops_grpc_debug_and_keeps_its_own_debug_and_grpc_warnings():
    """Through get_logger()'s real basicConfig: the cap is grpc's alone, and the root level is not what silenced it.

    The service's own DEBUG line still logs, so the root stays at DEBUG (tj-3mk3u5.45 acceptance).
    If the root level is changed on purpose, change the own-DEBUG assertion with it.
    """
    own_debug, grpc_warning = 'service debug still logs', 'GOAWAY received: too_many_pings'
    env = {name: value for name, value in os.environ.items() if name != 'PYTEST_ADDOPTS'}
    # The image's path model: common.rpc.ping imports trader_joe.proto, which a fresh interpreter
    # finds only on the image's second PYTHONPATH entry (decision tj-3mk3u5.42 F1).
    env['PYTHONPATH'] = image_pythonpath(REPO_ROOT)
    result = subprocess.run(
        [sys.executable, '-c', _SERVICE_PROBE, own_debug, grpc_warning, CYGRPC],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    received = [tuple(record) for record in json.loads(result.stdout)]
    assert ('tj45.service', logging.DEBUG, own_debug) in received, 'the service lost its own DEBUG output'
    assert [r for r in received if _in_grpc(r[0]) and r[1] < logging.WARNING] == [], 'grpc logged below WARNING'
    assert (CYGRPC, logging.WARNING, grpc_warning) in received, 'a grpc WARNING did not reach the root'
    assert grpc_warning in result.stderr, "a grpc WARNING never reached basicConfig's own handler"
