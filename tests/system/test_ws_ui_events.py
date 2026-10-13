"""WS /ui/v1/events over the real stack: what a POST and a DELETE announce, in order (tj-grna9p.81).

Design: tj-grna9p.81's RESCOPED note of 2026-10-12, item (3) -- "a WS client on /ui/v1/events receives
IngestRunChanged (start, ack, finish) and DatasetChanged for that POST, in order, as canonical UiEvent" -- and
the hub's own rules (data/store/app/events.py, ADR tj-grna9p.5): one hub per process, publish only after a
commit, events are notifications a client reloads on. The single-worker pin is tj-vcgqw8.

WHAT ONLY THIS TIER SHOWS. The unit tier (server/data/store/tests/test_events_hub.py) drives the hub and the
route in one process over SQLite. Here the socket is a real WebSocket through the prod image's uvicorn, the
publisher is a real POST /store that crosses gRPC to data_ingest (FakeRead) and commits on Postgres, and the
sockets are separate TCP connections: with more than one uvicorn worker, a socket held by one worker would
never hear a POST served by another, and the fan-out test below would go red.

THE FRAME IS READ AS THE JSON THE UI PARSES (protobuf canonical JSON of trader_joe.proto.ui.v1.UiEvent), not
through the generated classes: a test may not import trader_joe.proto (TID251). Each frame's top-level members
are checked to be exactly serverTime plus one member of the event oneof.

FILTERING. The hub fans every event out to every socket, so each assertion keeps only the frames that name this
test's symbol (a run's label is '<symbol> <granularity>') or this test's dataset id. The suite runs serially, so
nothing else should be publishing, but the filter keeps the tests honest if something does.

NOT PROVED HERE: the upgrade through Caddy at /api/store/ui/v1/events. The agent stack has no web service
(ADR tj-4rr0la); that is a host step on tj-grna9p.81.

CLEANUP. ingest_run has no foreign key, so the entry cascade does not reach it: this module deletes the run rows
carrying this run's symbol at the end, as test_http_ingest_runs.py does.
"""

import json
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from websockets.sync.client import ClientConnection, connect

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity
from common.errors.vocabulary import ERROR_DOMAIN, Reason
from data.store.app.database.models.ingest_run import IngestRun
from routers.data_store.app_endpoints import AssetDatasetStoreInterface, UiDatasetsInterface, UiEventsInterface
from tests.fakes.market_data import FAIL_PREFIX


pytestmark = pytest.mark.data_store

RUNS = IngestRun.__table__
GRANULARITY = Granularity.ONE_MINUTE
START = datetime.fromisoformat('2001-02-05T14:30:00+00:00')
END = START + timedelta(minutes=30)

RUNNING = 'INGEST_RUN_STATE_RUNNING'
SUCCEEDED = 'INGEST_RUN_STATE_SUCCEEDED'
FAILED = 'INGEST_RUN_STATE_FAILED'
UPDATED = 'DATASET_CHANGE_UPDATED'
DELETED = 'DATASET_CHANGE_DELETED'
EVENT_MEMBERS = {'datasetChanged', 'ingestRunChanged', 'requestChanged', 'reaperPendingChanged'}

# How long a test waits for the frames it expects. A served POST through FakeRead answers in well under a second.
FRAME_TIMEOUT_SECONDS = 20.0
# After the last expected frame, how long to keep listening for a frame that must NOT come.
QUIET_SECONDS = 1.5
# The route subscribes to the hub right after the handshake; this lets that land before the test publishes.
SUBSCRIBE_SETTLE_SECONDS = 0.3


@pytest.fixture(scope='module', autouse=True)
def delete_this_runs_ingest_runs(pg_engine: Engine, run_identity) -> Iterator[None]:
    yield
    with pg_engine.begin() as conn:
        conn.execute(sa.delete(RUNS).where(RUNS.c.asset_symbol.contains(run_identity.symbol)))


@pytest.fixture(scope='session')
def events_url(data_store_url: str) -> str:
    scheme, rest = data_store_url.split('://', 1)
    return f'{"wss" if scheme == "https" else "ws"}://{rest}{UiEventsInterface.UI_EVENTS.value}'


class EventsSocket:
    """One client of /ui/v1/events that keeps every frame it has read, parsed."""

    def __init__(self, ws: ClientConnection) -> None:
        self._ws = ws
        self.frames: list[dict[str, Any]] = []

    def _read_one(self, timeout: float) -> bool:
        try:
            raw = self._ws.recv(timeout=timeout)
        except TimeoutError:
            return False
        assert isinstance(raw, str), f'a UiEvent frame is a TEXT frame, got {type(raw).__name__}'
        self.frames.append(json.loads(raw))
        return True

    def read_until(self, done: Callable[[list[dict[str, Any]]], bool], what: str) -> list[dict[str, Any]]:
        deadline = time.monotonic() + FRAME_TIMEOUT_SECONDS
        while not done(self.frames):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._read_one(remaining):
                raise AssertionError(f'no {what} within {FRAME_TIMEOUT_SECONDS} s; frames read: {self.frames}')
        return self.frames

    def read_quietly(self, seconds: float = QUIET_SECONDS) -> list[dict[str, Any]]:
        """Keep reading for `seconds`, so a frame that should not come has its chance to."""
        deadline = time.monotonic() + seconds
        while (remaining := deadline - time.monotonic()) > 0 and self._read_one(remaining):
            pass
        return self.frames


@pytest.fixture
def open_socket(events_url: str) -> Iterator[Callable[[], EventsSocket]]:
    """Open sockets on demand; every one is closed when the test ends (connect() is a context manager)."""
    with ExitStack() as stack:

        def _open() -> EventsSocket:
            return EventsSocket(stack.enter_context(connect(events_url, open_timeout=10, close_timeout=2)))

        yield _open


def _settle() -> None:
    time.sleep(SUBSCRIBE_SETTLE_SECONDS)


def _store_path(symbol: str) -> str:
    return AssetDatasetStoreInterface.POST_STORE_ASSET_DATASET.format(
        asset_type=AssetType.STOCK.value, data_type=DataType.MARKET_ACTIVITY.value, asset_symbol=symbol
    )


def _body(owner: str, **fields: Any) -> dict[str, Any]:
    return {
        'owner': owner,
        'source': DataSource.ALPACA_API.value,
        'granularity': GRANULARITY.value,
        'start': START.isoformat(),
        'end': END.isoformat(),
        **fields,
    }


def _assert_canonical(frame: dict[str, Any]) -> None:
    """A frame holds serverTime (an RFC 3339 instant) and exactly one member of the event oneof, nothing else."""
    members = set(frame) - {'serverTime'}
    assert 'serverTime' in frame and len(members) == 1 and members <= EVENT_MEMBERS, frame
    assert datetime.fromisoformat(frame['serverTime']).tzinfo is not None, frame


def _run_frames(frames: list[dict[str, Any]], symbol: str) -> list[dict[str, Any]]:
    """The IngestRun of every ingestRunChanged frame whose label names `symbol`, in arrival order."""
    label = f'{symbol} {GRANULARITY.value}'
    return [
        f['ingestRunChanged']['run']
        for f in frames
        if f.get('ingestRunChanged', {}).get('run', {}).get('label') == label
    ]


def _dataset_frames(frames: list[dict[str, Any]], dataset_id: UUID | None = None) -> list[dict[str, Any]]:
    changed = [f['datasetChanged'] for f in frames if 'datasetChanged' in f]
    return [c for c in changed if dataset_id is None or c.get('id') == str(dataset_id)]


def _index(frames: list[dict[str, Any]], predicate: Callable[[dict[str, Any]], bool]) -> int:
    return next(i for i, frame in enumerate(frames) if predicate(frame))


def test_a_served_post_announces_start_ack_finish_then_the_dataset_and_a_delete_announces_it_gone(
    own_symbol, run_identity, data_store, adopt_entries, open_socket
):
    """The run's three committed writes, in order, each a frame; then DatasetChanged UPDATED; then DELETED."""
    socket = open_socket()
    _settle()

    try:
        response = data_store.post(_store_path(own_symbol), json=_body(run_identity.owner), auth='right')
    finally:
        ids = adopt_entries(own_symbol)
    assert response.status_code == 200, data_store.describe(response)
    (dataset_id,) = ids

    frames = socket.read_until(lambda fs: bool(_dataset_frames(fs, dataset_id)), f'DatasetChanged for {dataset_id}')
    for frame in frames:
        _assert_canonical(frame)

    runs = _run_frames(frames, own_symbol)
    assert [run['state'] for run in runs] == [RUNNING, RUNNING, SUCCEEDED], runs
    assert len({run['id'] for run in runs}) == 1, f'one run, three frames: {runs}'
    # Canonical JSON omits an empty string: dataset_id is empty until the ack, then names the dataset.
    assert [run.get('datasetId', '') for run in runs] == ['', str(dataset_id), str(dataset_id)], runs
    assert 'finishedAt' not in runs[0] and 'finishedAt' not in runs[1] and 'finishedAt' in runs[2], runs
    assert int(runs[2].get('barsWritten', '0')) == response.json()['data_points'] > 0, runs
    assert all(run['trigger'] == 'RUN_TRIGGER_REQUEST' and 'error' not in run for run in runs), runs

    # The run the frames announced is the run the REST read returns: a client reloading on the event sees it.
    listed = data_store.get(UiDatasetsInterface.GET_UI_DATASET_RUNS.format(dataset_id=dataset_id))
    assert listed.status_code == 200, data_store.describe(listed)
    assert [item['id'] for item in listed.json()['items']] == [runs[0]['id']]

    assert _dataset_frames(frames, dataset_id) == [{'id': str(dataset_id), 'change': UPDATED}]
    finished_at = _index(frames, lambda f: f.get('ingestRunChanged', {}).get('run', {}).get('state') == SUCCEEDED)
    announced_at = _index(frames, lambda f: f.get('datasetChanged', {}).get('id') == str(dataset_id))
    assert finished_at < announced_at, f'the dataset is announced after its run finished: {frames}'

    deleted = data_store.delete(f'/store/{dataset_id}', params={'owner': run_identity.owner}, auth='right')
    assert deleted.status_code == 200, data_store.describe(deleted)
    frames = socket.read_until(lambda fs: len(_dataset_frames(fs, dataset_id)) == 2, f'DELETED for {dataset_id}')
    _assert_canonical(frames[-1])
    assert _dataset_frames(frames, dataset_id)[-1] == {'id': str(dataset_id), 'change': DELETED}


def test_a_failing_vendor_announces_a_failed_run_with_its_reason_and_no_dataset(
    own_symbol, run_identity, data_store, scenario_entries, open_socket
):
    socket = open_socket()
    _settle()
    symbol = f'{FAIL_PREFIX}{own_symbol}'

    try:
        response = data_store.post(_store_path(symbol), json=_body(run_identity.owner), auth='right')
    finally:
        ids = scenario_entries(symbol)
    assert response.status_code == 503, data_store.describe(response)
    assert ids == []

    socket.read_until(lambda fs: any(r['state'] == FAILED for r in _run_frames(fs, symbol)), 'the FAILED run')
    frames = socket.read_quietly()

    runs = _run_frames(frames, symbol)
    assert [run['state'] for run in runs] == [RUNNING, FAILED], runs
    assert all('datasetId' not in run for run in runs), f'no ack, so no dataset: {runs}'
    error = runs[-1]['error']
    assert (error['domain'], error['reason']) == (ERROR_DOMAIN, response.json()['reason']), runs
    assert _dataset_frames(frames) == [], f'nothing was written, so nothing is announced: {frames}'


def test_a_refused_ack_announces_a_failed_run_naming_feed_not_available(
    own_symbol, run_identity, data_store, adopt_entries, open_socket
):
    socket = open_socket()
    _settle()

    try:
        response = data_store.post(
            _store_path(own_symbol), json=_body(run_identity.owner, feed=Feed.SIP.value), auth='right'
        )
    finally:
        ids = adopt_entries(own_symbol)
    assert response.status_code == 422, data_store.describe(response)
    assert ids == []

    socket.read_until(lambda fs: any(r['state'] == FAILED for r in _run_frames(fs, own_symbol)), 'the FAILED run')
    frames = socket.read_quietly()

    runs = _run_frames(frames, own_symbol)
    assert [run['state'] for run in runs] == [RUNNING, FAILED], runs
    assert runs[-1]['error']['reason'] == Reason.FEED_NOT_AVAILABLE.value, runs
    assert _dataset_frames(frames) == [], frames


SOCKETS = 3


def test_every_connected_socket_hears_the_same_post(own_symbol, run_identity, data_store, adopt_entries, open_socket):
    """Fan-out across separate connections: what the single-worker pin (tj-vcgqw8) is for.

    With N uvicorn workers each holding its own hub, the sockets would be spread across workers and only those on
    the worker that served the POST would hear it.
    """
    sockets = [open_socket() for _ in range(SOCKETS)]
    _settle()

    try:
        response = data_store.post(_store_path(own_symbol), json=_body(run_identity.owner), auth='right')
    finally:
        ids = adopt_entries(own_symbol)
    assert response.status_code == 200, data_store.describe(response)
    (dataset_id,) = ids

    heard = []
    for socket in sockets:
        frames = socket.read_until(lambda fs: bool(_dataset_frames(fs, dataset_id)), f'DatasetChanged for {dataset_id}')
        heard.append(([run['state'] for run in _run_frames(frames, own_symbol)], _dataset_frames(frames, dataset_id)))

    expected = ([RUNNING, RUNNING, SUCCEEDED], [{'id': str(dataset_id), 'change': UPDATED}])
    assert heard == [expected] * SOCKETS, heard
