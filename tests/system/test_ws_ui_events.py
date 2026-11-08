"""WS /ui/v1/events over the real stack: what a POST and a DELETE announce, in order (tj-grna9p.81, tj-7zjov1.233).

Design: the hub's own rules (data/store/app/events.py, ADR tj-grna9p.5): one hub per process, publish only after a
commit, events are notifications a client reloads on. The single-worker pin is tj-vcgqw8. WHAT A POST ANNOUNCES is
the push path's (P35, tj-7zjov1.28; server/data/store/CLAUDE.md, "Collection record and live events" and "Delivery
intake"): POST /store no longer opens a RUNNING row (the legacy run recorder's start, ack and finish frames, which
tj-grna9p.81 item (3) pinned, are gone since 12768ab). Each REPORTED ATTEMPT is one ingest_run row written by the
delivery intake, and its IngestRunChanged is published after the delivery commits (delivery_intake.py, deliver), then
the DatasetChanged of a delivery that resolved a dataset. So:
  * a served POST is ONE bars frame, SUCCEEDED, already naming its dataset and request, then DatasetChanged UPDATED;
  * a failing vendor is one FAILED bars frame PER ATTEMPT (trigger REQUEST, then RETRY), no dataset, then the 503;
  * EVERY POST first sends its name_change clamp query (P51), a corporate-actions request whose attempt row is
    announced like any other: one frame labelled with the symbol alone (no bar width), EMPTY because FakeRead serves
    no actions without a world, before the bars request is sent;
  * a refused feed (FEED_NOT_AVAILABLE) is refused at Submit: the request is FAILED PERMANENT with NO attempt row, so
    NO bars run frame and no DatasetChanged; the clamp's frame is the POST's only announcement. Nothing publishes
    RequestChanged (reserved, docs/API.md U8). docs/API.md's U8 table still describes the legacy three frames.
Each test compares the frames with the attempt rows in ingest_run, by id and in attempt order: one frame per row.

WHAT ONLY THIS TIER SHOWS. The unit tier (server/data/store/tests/test_events_hub.py) drives the hub and the
route in one process over SQLite. Here the socket is a real WebSocket through the prod image's uvicorn, the
publisher is a real POST /store that crosses gRPC to data_ingest (FakeRead) and commits on Postgres, and the
sockets are separate TCP connections: with more than one uvicorn worker, a socket held by one worker would
never hear a POST served by another, and the fan-out test below would go red.

THE FRAME IS READ AS THE JSON THE UI PARSES (protobuf canonical JSON of trader_joe.proto.ui.v1.UiEvent), not
through the generated classes: a test may not import trader_joe.proto (TID251). Each frame's top-level members
are checked to be exactly serverTime plus one member of the event oneof.

FILTERING. The hub fans every event out to every socket, so each assertion keeps only the frames that name this
test's symbol (a bars run's label is '<symbol> <granularity>', the clamp's is '<symbol>') or this test's dataset id.
The suite runs serially, but the store's own loops (the asset-list schedule, the DAILY refresh) publish too, and the
filter keeps the tests honest about them.

THE LIVE INSTANCE. A delivery from a SUPERSEDED ingest instance is answered FINAL and a POST is never queued under
one, so these tests need the stack's ingest live; the modules that sight minted instance ids restore it after each
test (live_instances_kept, tests/system/live_instances.py).

NOT PROVED HERE: the upgrade through Caddy at /api/store/ui/v1/events. Test mode has no web service
(ADR tj-4rr0la addendum 27: base file plus the fake overlay); that is a host step on tj-grna9p.81.

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
from common.errors.vocabulary import Reason
from data.store.app.database.models.ingest_request import (
    IngestRequest,
    IngestRequestFailureCause,
    IngestRequestKind,
    IngestRequestState,
)
from data.store.app.database.models.ingest_run import IngestRun
from routers.data_store.app_endpoints import AssetDatasetStoreInterface, UiDatasetsInterface, UiEventsInterface
from tests.fakes.market_data import FAIL_PREFIX


pytestmark = pytest.mark.data_store

RUNS = IngestRun.__table__
REQUESTS = IngestRequest.__table__
GRANULARITY = Granularity.ONE_MINUTE
START = datetime.fromisoformat('2001-02-05T14:30:00+00:00')
END = START + timedelta(minutes=30)

SUCCEEDED = 'INGEST_RUN_STATE_SUCCEEDED'
EMPTY = 'INGEST_RUN_STATE_EMPTY'
FAILED = 'INGEST_RUN_STATE_FAILED'
REQUEST = 'RUN_TRIGGER_REQUEST'
RETRY = 'RUN_TRIGGER_RETRY'
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


def _labelled(frames: list[dict[str, Any]], label: str) -> list[dict[str, Any]]:
    return [
        f['ingestRunChanged']['run']
        for f in frames
        if f.get('ingestRunChanged', {}).get('run', {}).get('label') == label
    ]


def _run_frames(frames: list[dict[str, Any]], symbol: str) -> list[dict[str, Any]]:
    """The IngestRun of every bars attempt frame of `symbol` (label '<symbol> <granularity>'), in arrival order."""
    return _labelled(frames, f'{symbol} {GRANULARITY.value}')


def _clamp_frames(frames: list[dict[str, Any]], symbol: str) -> list[dict[str, Any]]:
    """The IngestRun of every attempt frame of the POST's name_change clamp query: labelled with the symbol alone."""
    return _labelled(frames, symbol)


def _attempt_rows(pg_engine: Engine, request_id: str) -> list[sa.Row]:
    """The attempt rows of one request, in attempt order: what the frames must announce, one each."""
    with pg_engine.connect() as conn:
        return list(
            conn.execute(
                sa.select(RUNS.c.id, RUNS.c.attempt_no, RUNS.c.state)
                .where(RUNS.c.request_id == UUID(request_id))
                .order_by(RUNS.c.attempt_no)
            )
        )


def _assert_one_frame_per_attempt(pg_engine: Engine, runs: list[dict[str, Any]]) -> list[sa.Row]:
    """All of `runs` belong to one request, and they are its attempt rows, one frame each, in attempt order."""
    request_ids = {run.get('requestId') for run in runs}
    assert len(request_ids) == 1 and None not in request_ids, f'one request, named by every frame: {runs}'
    rows = _attempt_rows(pg_engine, request_ids.pop())
    assert [run['id'] for run in runs] == [str(row.id) for row in rows], (runs, rows)
    assert [run['state'] for run in runs] == [f'INGEST_RUN_STATE_{row.state.value}' for row in rows], (runs, rows)
    return rows


def _assert_the_clamp_came_first(frames: list[dict[str, Any]], symbol: str) -> None:
    """The POST's corporate-actions query: one EMPTY attempt frame, no dataset, announced before any bars frame."""
    clamp = _clamp_frames(frames, symbol)
    assert [run['state'] for run in clamp] == [EMPTY], clamp
    assert 'datasetId' not in clamp[0] and clamp[0]['trigger'] == REQUEST, clamp
    bars_label = f'{symbol} {GRANULARITY.value}'
    clamp_at = _index(frames, lambda f: f.get('ingestRunChanged', {}).get('run', {}).get('id') == clamp[0]['id'])
    bars_at = [
        i for i, f in enumerate(frames) if f.get('ingestRunChanged', {}).get('run', {}).get('label') == bars_label
    ]
    assert all(clamp_at < i for i in bars_at), f'the clamp query is answered before the bars request is sent: {frames}'


def _dataset_frames(frames: list[dict[str, Any]], dataset_id: UUID | None = None) -> list[dict[str, Any]]:
    changed = [f['datasetChanged'] for f in frames if 'datasetChanged' in f]
    return [c for c in changed if dataset_id is None or c.get('id') == str(dataset_id)]


def _index(frames: list[dict[str, Any]], predicate: Callable[[dict[str, Any]], bool]) -> int:
    return next(i for i, frame in enumerate(frames) if predicate(frame))


def test_a_served_post_announces_its_attempt_then_the_dataset_and_a_delete_announces_it_gone(
    own_symbol, run_identity, data_store, adopt_entries, open_socket, pg_engine
):
    """The clamp's attempt, then ONE SUCCEEDED bars attempt naming the dataset; then DatasetChanged UPDATED; DELETED."""
    socket = open_socket()
    _settle()

    try:
        response = data_store.post(_store_path(own_symbol), json=_body(run_identity.owner), auth='right')
    finally:
        ids = adopt_entries(own_symbol)
    assert response.status_code == 200, data_store.describe(response)
    (dataset_id,) = ids

    frames = socket.read_until(lambda fs: bool(_dataset_frames(fs, dataset_id)), f'DatasetChanged for {dataset_id}')
    frames = socket.read_quietly()
    for frame in frames:
        _assert_canonical(frame)

    _assert_the_clamp_came_first(frames, own_symbol)
    runs = _run_frames(frames, own_symbol)
    assert [run['state'] for run in runs] == [SUCCEEDED], f'one delivered attempt, one frame: {runs}'
    _assert_one_frame_per_attempt(pg_engine, runs)
    (run,) = runs
    # The intake writes the entry on the first delivered attempt, in the same transaction: the frame names it already.
    assert run.get('datasetId') == str(dataset_id), run
    assert 'finishedAt' in run and run['trigger'] == REQUEST and 'error' not in run, run
    assert int(run.get('barsWritten', '0')) == response.json()['data_points'] > 0, run

    # The run the frames announced is the run the REST read returns: a client reloading on the event sees it.
    listed = data_store.get(UiDatasetsInterface.GET_UI_DATASET_RUNS.format(dataset_id=dataset_id))
    assert listed.status_code == 200, data_store.describe(listed)
    assert [item['id'] for item in listed.json()['items']] == [run['id']]

    assert _dataset_frames(frames, dataset_id) == [{'id': str(dataset_id), 'change': UPDATED}]
    finished_at = _index(frames, lambda f: f.get('ingestRunChanged', {}).get('run', {}).get('state') == SUCCEEDED)
    announced_at = _index(frames, lambda f: f.get('datasetChanged', {}).get('id') == str(dataset_id))
    assert finished_at < announced_at, f'the dataset is announced after its run finished: {frames}'

    deleted = data_store.delete(f'/store/{dataset_id}', params={'owner': run_identity.owner}, auth='right')
    assert deleted.status_code == 200, data_store.describe(deleted)
    frames = socket.read_until(lambda fs: len(_dataset_frames(fs, dataset_id)) == 2, f'DELETED for {dataset_id}')
    _assert_canonical(frames[-1])
    assert _dataset_frames(frames, dataset_id)[-1] == {'id': str(dataset_id), 'change': DELETED}


def test_a_failing_vendor_announces_each_failed_attempt_and_no_dataset(
    own_symbol, run_identity, data_store, scenario_entries, open_socket, pg_engine
):
    """FAIL_ fails every attempt: one FAILED frame per attempt row (REQUEST, then RETRY), no dataset, then the 503."""
    socket = open_socket()
    _settle()
    symbol = f'{FAIL_PREFIX}{own_symbol}'

    try:
        response = data_store.post(_store_path(symbol), json=_body(run_identity.owner), auth='right')
    finally:
        ids = scenario_entries(symbol)
    assert response.status_code == 503, data_store.describe(response)
    assert response.json()['reason'] == Reason.VENDOR_UNAVAILABLE.value, data_store.describe(response)
    assert ids == []

    # The POST answers once the request is finished, so every attempt row is committed; their frames follow each commit.
    socket.read_until(lambda fs: bool(_run_frames(fs, symbol)), 'a FAILED attempt')
    request_id = _run_frames(socket.frames, symbol)[0]['requestId']
    expected = len(_attempt_rows(pg_engine, request_id))
    socket.read_until(lambda fs: len(_run_frames(fs, symbol)) >= expected, f'{expected} attempt frames')
    frames = socket.read_quietly()
    for frame in frames:
        _assert_canonical(frame)

    _assert_the_clamp_came_first(frames, symbol)
    runs = _run_frames(frames, symbol)
    rows = _assert_one_frame_per_attempt(pg_engine, runs)
    assert len(rows) > 1 and {run['state'] for run in runs} == {FAILED}, f'retried, and every attempt failed: {runs}'
    assert [row.attempt_no for row in rows] == list(range(1, len(rows) + 1)), rows
    assert [run['trigger'] for run in runs] == [REQUEST] + [RETRY] * (len(runs) - 1), runs
    assert all('datasetId' not in run for run in runs), f'nothing delivered, so no dataset: {runs}'
    # The attempt row keeps the vendor's sentence; a typed (domain, reason) is the request's and the POST's (above).
    assert all(run['error'].get('message') for run in runs), runs
    assert _dataset_frames(frames) == [], f'nothing was written, so nothing is announced: {frames}'


def test_a_refused_feed_announces_no_bars_run_and_no_dataset(
    own_symbol, run_identity, data_store, adopt_entries, open_socket, pg_engine
):
    """FEED_NOT_AVAILABLE is refused at Submit: no attempt row, so no bars frame; the clamp's frame is all there is."""
    socket = open_socket()
    _settle()

    try:
        response = data_store.post(
            _store_path(own_symbol), json=_body(run_identity.owner, feed=Feed.SIP.value), auth='right'
        )
    finally:
        ids = adopt_entries(own_symbol)
    assert response.status_code == 422, data_store.describe(response)
    assert response.json()['reason'] == Reason.FEED_NOT_AVAILABLE.value, data_store.describe(response)
    assert ids == []

    # The clamp is answered before the bars request is sent, so its frame is already published when the POST answers.
    socket.read_until(lambda fs: bool(_clamp_frames(fs, own_symbol)), "the clamp query's attempt")
    frames = socket.read_quietly()
    for frame in frames:
        _assert_canonical(frame)

    _assert_the_clamp_came_first(frames, own_symbol)
    assert _run_frames(frames, own_symbol) == [], f'a refused Submit writes no attempt row, so no run frame: {frames}'
    assert _dataset_frames(frames) == [], frames
    # own_symbol is this test's alone, so its one bars request is the refused POST's.
    with pg_engine.connect() as conn:
        refused = conn.execute(
            sa.select(REQUESTS.c.id, REQUESTS.c.state, REQUESTS.c.failure_cause).where(
                REQUESTS.c.asset_symbol == own_symbol, REQUESTS.c.kind == IngestRequestKind.BARS
            )
        ).all()
    assert [(row.state, row.failure_cause) for row in refused] == [
        (IngestRequestState.FAILED, IngestRequestFailureCause.PERMANENT)
    ], refused
    assert _attempt_rows(pg_engine, str(refused[0].id)) == [], 'no attempt row behind the refusal'


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

    expected = ([SUCCEEDED], [{'id': str(dataset_id), 'change': UPDATED}])
    assert heard == [expected] * SOCKETS, heard
