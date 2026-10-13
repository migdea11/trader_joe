"""The events hub and WS /ui/v1/events (tj-grna9p.73, ADR tj-grna9p.5), validated by tj-grna9p.79.

The design is tj-grna9p.73's architect re-baseline of 2026-10-11 and its ACCEPTANCE:

* a connected client gets IngestRunChanged frames -- canonical JSON of UiEvent, server_time set -- and
  DatasetChanged after a /store write commits (POST sends UPDATED; DELETE sends DELETED);
* a stalled client is closed (1013) and publish still returns at once: the per-client queue is BOUNDED and
  publish uses put_nowait, so one slow socket never delays the publisher;
* after N connect and disconnect cycles no subscriber is left behind;
* a rolled-back write publishes nothing;
* no SettingsChanged exists (pinned on the proto in common/tests/test_proto_ui_phase_two.py).

THREE TIERS IN ONE FILE. The hub on its own; the route driven DIRECTLY with a scripted socket, which is the only
way to make a client stall on purpose (a test client's transport never pushes back); and one round trip over the
real ASGI WebSocket with Starlette's TestClient, publishing on the app's own loop through the session's portal.
The frame is compared as the JSON the UI parses, since a test may not import trader_joe.proto (TID251).
"""

import asyncio
import json
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity, UpdateType
from data.store.app import events
from data.store.app.database.crud.ingest_run import IngestRunRecord
from data.store.app.database.models.ingest_run import IngestRunState, IngestRunTrigger
from data.store.app.events import (
    DEFAULT_QUEUE_SIZE,
    QUEUE_SIZE_ENV,
    DatasetChange,
    DatasetChanged,
    EventHub,
    IngestRunChanged,
    ReaperPendingChanged,
)
from data.store.app.main import app
from routers.data_store import ui_events


pytestmark = pytest.mark.data_store

WHEN = datetime(2026, 10, 6, 21, 55, tzinfo=UTC)
GUARD_S = 5.0


def a_run(state: IngestRunState = IngestRunState.RUNNING) -> IngestRunRecord:
    return IngestRunRecord(
        id=uuid.UUID('9a6f8a52-0000-4000-8000-000000000001'),
        dataset_id=uuid.UUID('1b4e28ba-2fa1-11d2-883f-0016d3cca427'),
        request_id=None,
        trigger=IngestRunTrigger.REQUEST,
        actor='operator',
        asset_symbol='VFV',
        asset_type=AssetType.STOCK,
        data_type=DataType.MARKET_ACTIVITY,
        source=DataSource.ALPACA_API,
        granularity=Granularity.ONE_DAY,
        update_type=UpdateType.STATIC,
        feed=Feed.IEX,
        requested_start=datetime(2026, 1, 2, tzinfo=UTC),
        requested_end=None,
        state=state,
        bars_written=0 if state is IngestRunState.RUNNING else 2**40,
        error_domain=None,
        error_reason=None,
        error_message=None,
        error_id=None,
        started_at=WHEN,
        finished_at=None if state is IngestRunState.RUNNING else WHEN,
    )


# ------------------------------------------------------------------------------------------------- the hub


@pytest.mark.asyncio
async def test_every_subscriber_gets_every_event_in_order():
    hub = EventHub(queue_size=8)
    first, second = hub.subscribe(), hub.subscribe()
    sent = [DatasetChanged(uuid.uuid4(), DatasetChange.UPDATED), IngestRunChanged(a_run()), ReaperPendingChanged()]

    for event in sent:
        hub.publish(event)

    for subscription in (first, second):
        assert [await asyncio.wait_for(subscription.get(), GUARD_S) for _ in sent] == sent


def test_unsubscribe_removes_a_client_and_is_idempotent():
    hub = EventHub(queue_size=8)
    subscription = hub.subscribe()
    assert hub.subscriber_count == 1
    subscription.close()
    subscription.close()
    assert hub.subscriber_count == 0
    hub.publish(ReaperPendingChanged())  # nothing to deliver to, and nothing raises


@pytest.mark.asyncio
async def test_a_full_queue_drops_only_that_client_and_publish_never_waits():
    """Bounded, put_nowait: a client that never reads is dropped and removed; a reading client is untouched."""
    hub = EventHub(queue_size=3)
    stalled, reading = hub.subscribe(), hub.subscribe()
    received = []

    started = time.monotonic()
    for index in range(10_000):
        hub.publish(DatasetChanged(uuid.UUID(int=index), DatasetChange.UPDATED))
        while not reading._queue.empty():
            received.append(reading._queue.get_nowait())
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f'publishing to a stalled client took {elapsed:.2f}s: publish must never wait'
    assert stalled.dropped and not reading.dropped
    assert hub.subscriber_count == 1, 'the dropped client is still subscribed'
    assert await asyncio.wait_for(stalled.get(), GUARD_S) is None, 'a dropped client must wake with None at once'
    assert len(received) == 10_000, 'the reading client lost events because another one stalled'


@pytest.mark.asyncio
async def test_a_dropped_client_receives_none_of_the_backlog():
    """Stale events behind a full queue are discarded: the client reloads over REST instead."""
    hub = EventHub(queue_size=2)
    subscription = hub.subscribe()
    for _ in range(3):
        hub.publish(ReaperPendingChanged())
    assert await asyncio.wait_for(subscription.get(), GUARD_S) is None
    hub.publish(ReaperPendingChanged())
    assert subscription._queue.empty(), 'a dropped client keeps receiving'


def test_the_queue_size_comes_from_the_environment_with_a_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(QUEUE_SIZE_ENV, raising=False)
    assert EventHub().subscribe()._queue.maxsize == DEFAULT_QUEUE_SIZE == 256
    monkeypatch.setenv(QUEUE_SIZE_ENV, '7')
    assert EventHub().subscribe()._queue.maxsize == 7


# ------------------------------------------------------------------------------------------------- the route, scripted


class ScriptedSocket:
    """The WebSocket surface ui_events uses, under the test's control.

    send_text can be held, which is a client that stopped reading; receive returns a disconnect once
    `leave` is set, which is a client that went away.
    """

    def __init__(self) -> None:
        self.accepted = False
        self.frames: list[str] = []
        self.closed_with: int | None = None
        self.sending = asyncio.Event()
        self.sending.set()
        self.leave = asyncio.Event()
        self.client_state = type('State', (), {'name': 'CONNECTED'})()

    async def accept(self) -> None:
        self.accepted = True

    async def receive(self) -> dict:
        await self.leave.wait()
        self.client_state.name = 'DISCONNECTED'
        return {'type': 'websocket.disconnect', 'code': 1000}

    async def send_text(self, text: str) -> None:
        await self.sending.wait()
        self.frames.append(text)

    async def close(self, code: int = 1000) -> None:
        self.closed_with = code


@pytest.fixture
def hub(monkeypatch: pytest.MonkeyPatch) -> EventHub:
    """A small hub, wired where the route and every publisher look for it."""
    small = EventHub(queue_size=4)
    monkeypatch.setattr(events, 'hub', small)
    monkeypatch.setattr(ui_events, 'hub', small)
    return small


async def _until(predicate, what: str) -> None:
    deadline = time.monotonic() + GUARD_S
    while not predicate():
        assert time.monotonic() < deadline, f'timed out waiting for {what}'
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_a_stalled_client_is_closed_1013_and_the_publisher_never_waits_on_it(hub):
    socket = ScriptedSocket()
    socket.sending.clear()  # the client stops reading after the first frame is handed over
    route = asyncio.create_task(ui_events.ui_events(socket))
    await _until(lambda: hub.subscriber_count == 1, 'the route to subscribe')

    started = time.monotonic()
    for _ in range(50):
        events.publish(ReaperPendingChanged())
    assert time.monotonic() - started < 0.5, 'publish waited on a socket'
    assert hub.subscriber_count == 0, 'the stalled client was not dropped'

    socket.sending.set()
    await asyncio.wait_for(route, GUARD_S)
    assert socket.closed_with == status.WS_1013_TRY_AGAIN_LATER
    assert len(socket.frames) <= 1, 'a dropped client kept receiving its stale backlog'


@pytest.mark.asyncio
async def test_no_subscriber_is_left_behind_after_many_connect_and_disconnect_cycles(hub):
    for cycle in range(25):
        socket = ScriptedSocket()
        route = asyncio.create_task(ui_events.ui_events(socket))
        await _until(lambda: hub.subscriber_count == 1, f'cycle {cycle} to subscribe')
        if cycle % 2:
            events.publish(ReaperPendingChanged())
            await _until(lambda frames=socket.frames: len(frames) == 1, f'cycle {cycle} to forward')
        socket.leave.set()
        await asyncio.wait_for(route, GUARD_S)
        assert socket.accepted and socket.closed_with is None
        assert hub.subscriber_count == 0, f'cycle {cycle} left a subscriber behind'


@pytest.mark.asyncio
async def test_a_client_leaving_while_a_frame_is_pending_unsubscribes_too(hub):
    socket = ScriptedSocket()
    route = asyncio.create_task(ui_events.ui_events(socket))
    await _until(lambda: hub.subscriber_count == 1, 'the route to subscribe')
    socket.sending.clear()
    events.publish(ReaperPendingChanged())
    socket.leave.set()
    socket.sending.set()
    await asyncio.wait_for(route, GUARD_S)
    assert hub.subscriber_count == 0


# ------------------------------------------------------------------------------------------------- the frame, over ASGI

RUN_JSON = {
    'id': '9a6f8a52-0000-4000-8000-000000000001',
    'datasetId': '1b4e28ba-2fa1-11d2-883f-0016d3cca427',
    'label': 'VFV 1day',
    'trigger': 'RUN_TRIGGER_REQUEST',
    'actor': 'operator',
    'start': '2026-01-02T00:00:00Z',
    'state': 'INGEST_RUN_STATE_SUCCEEDED',
    'barsWritten': '1099511627776',
    'startedAt': '2026-10-06T21:55:00Z',
    'finishedAt': '2026-10-06T21:55:00Z',
}


def _frame(text: str) -> dict:
    body = json.loads(text)
    sent_at = datetime.fromisoformat(body.pop('serverTime').replace('Z', '+00:00'))
    assert abs((datetime.now(UTC) - sent_at).total_seconds()) < 60, f'serverTime is not the send time: {sent_at}'
    return body


def test_frames_cross_the_real_socket_as_canonical_ui_event_json(hub):
    """IngestRunChanged and DatasetChanged as the UI parses them, server_time set at the send."""
    dataset_id = uuid.uuid4()
    with TestClient(app).websocket_connect('/ui/v1/events') as socket:
        deadline = time.monotonic() + GUARD_S
        while hub.subscriber_count != 1:
            assert time.monotonic() < deadline, 'the socket never subscribed'
            time.sleep(0.01)
        socket.portal.call(events.publish, IngestRunChanged(a_run(IngestRunState.SUCCEEDED)))
        socket.portal.call(events.publish, DatasetChanged(dataset_id, DatasetChange.DELETED))
        socket.portal.call(events.publish, ReaperPendingChanged())

        assert _frame(socket.receive_text()) == {'ingestRunChanged': {'run': RUN_JSON}}
        assert _frame(socket.receive_text()) == {
            'datasetChanged': {'id': str(dataset_id), 'change': 'DATASET_CHANGE_DELETED'}
        }
        assert _frame(socket.receive_text()) == {'reaperPendingChanged': {}}
    deadline = time.monotonic() + GUARD_S
    while hub.subscriber_count:
        assert time.monotonic() < deadline, 'closing the socket left its subscriber behind'
        time.sleep(0.01)


def test_a_failed_run_frame_carries_its_error_and_a_running_one_has_none():
    from routers.common.proto_json import to_proto_json
    from routers.data_store.ui_mapping import ui_event_message

    failed = replace(
        a_run(IngestRunState.FAILED), error_domain='trader-joe', error_reason='PEER_UNAVAILABLE', error_message='down'
    )
    rendered = json.loads(to_proto_json(ui_event_message(IngestRunChanged(failed), WHEN)))
    assert rendered['serverTime'] == '2026-10-06T21:55:00Z'
    assert rendered['ingestRunChanged']['run']['error'] == {
        'domain': 'trader-joe',
        'reason': 'PEER_UNAVAILABLE',
        'message': 'down',
    }
    running = json.loads(to_proto_json(ui_event_message(IngestRunChanged(a_run()), WHEN)))
    assert 'error' not in running['ingestRunChanged']['run'] and 'finishedAt' not in running['ingestRunChanged']['run']


def test_the_dataset_change_members_mirror_the_wire_enum():
    """The hub's DatasetChange maps by name onto DATASET_CHANGE_*: a member on one side only is a frame that fails."""
    from routers.common.proto_json import to_proto_json
    from routers.data_store.ui_mapping import ui_event_message

    for change in DatasetChange:
        frame = json.loads(to_proto_json(ui_event_message(DatasetChanged(uuid.uuid4(), change), WHEN)))
        assert frame['datasetChanged']['change'] == f'DATASET_CHANGE_{change.name}'
