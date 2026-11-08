"""The instance write secret over real HTTP: tj-vhboky.14 items 7a-d (tj-vhboky.50, Sys-3).

Design: tj-vhboky.1 section 5 and tj-vhboky.8 (write routes carry the secret; reads are open),
routers/common/instance_secret.py. This is the one property the unit tier cannot approach: every
unit test overrides the dependency, so only a request through the running service shows the
header is actually checked on each write route, and that a rejection does not echo the secret.

THE MATRIX, for every write route -- the dataset POST, the dataset DELETE, the internal
single-bar POST, the /ui/v1 expiry PATCH and dataset DELETE (tj-grna9p.86, added by
tj-grna9p.136), and the /ui/v1 collecting PATCH (tj-grna9p.175):
  * 7a no header, and an EMPTY header value -> 401, and nothing written or removed;
  * 7b a wrong secret -> 401, and nothing written or removed;
  * 7c the right secret -> the handler runs (below);
  * 7d a GET with no secret, or with a wrong one, succeeds: reads are deliberately open.
Every 401 response, and every 7c response, is checked for the secret -- in the body and in every
raw header value -- before anything else is asserted about it. Every 401 body carries exactly the
fixed rejection detail, imported rather than retyped.

7c ON THE DATASET POST IS A 409, NOT A 2xx, AND ON PURPOSE. A 2xx there means data_store called
data_ingest and the broker, and this part never drives the broker (fake-broker plan, decision
tj-vhboky.54; the dataset POST's success paths are Sys-7, tj-vhboky.63). So each dataset POST here
overlaps an entry of the same owner seeded by SQL: with the right secret the handler runs and
refuses with 409 before the upsert and before the RPC (the own-overlap check,
data/store/app/database/crud/stock/store_dataset_entry.py). A 401 there means the secret was not
accepted; a 409 means it was. And a regression that let a rejected POST through would ALSO stop
at that 409 rather than reach ingest. Any entry a dataset POST might create is adopted into the
cleanup registry regardless.

THE /ui/v1 WRITES CARRY A CONFIRMED OVERRIDE, ON PURPOSE. The seeded entry is the run's owner's,
not the operator's, so without an override the handler would stop at 409 OVERRIDE_REQUIRED and a
regression that dropped the guard would change nothing either way -- the 401 test's "nothing
changed" would then be vacuous for these two routes. With the override, a request that got past
the guard really sets the expiry or really deletes the entry, so the 401 test's checks bite, and
7c is the write itself: PATCH 200 with the expiry stored, DELETE 204 with the entry gone, each
with its audit row. The audit rows are this module's to delete, by the seeded entry's id.

THE COLLECTING PATCH NEEDS A SUBSCRIPTION. The seeded entry is STATIC, and a STATIC dataset is a 422
on that route, so a regression that dropped its guard would stop at the 422 and change nothing: the
401 test's "nothing changed" would be vacuous. Its case therefore turns the seeded entry into a DAILY
subscription that is collecting (by SQL, before the one request), and the request stops it with a
confirmed override: past the guard, collect really goes false and an override row is written.

THE PANEL WRITES (tj-7zjov1.36 W1 to W3, added by tj-7zjov1.242): the panel create, a panel's settings
version and a source's settings version, all POST. Each request is one that WOULD write past the guard, so
the 401 test's "nothing written" bites: the row counts of panel, panel_settings_version and
panel_source_settings_version are taken before the request and compared after it. That needs a panel and a
source settings version to exist, so a fixture seeds both BY SQL (the create names no source settings; on a source
with no version it would write that source's version 1 itself, ADR tj-2ayia2, and the 7c row count would then
depend on what the source already held). The source is MANUAL_ENTRY on
purpose: it has no trading calendar, so the panel manager skips its panels, and the seeded panel is
disabled besides; ALPACA_API holds the seed cast's panel and is the one the manager runs on. 7c is the write
itself: 201, and exactly the rows it should add. Every row the fixture seeded or a request added carries
this run's name prefix or reason marker and is deleted by SQL at teardown (test-only deletes: the panel
tables are append-only). The 7d panel reads are the detail, its settings versions and the source's settings
versions.

NOT HERE: 7e (secret unset, restart -> every write 401) and 7f (no secret in the container logs)
need container control; they are shell steps in the CI job (Sys-5, tj-vhboky.52).

UVICORN 0.53 / STARLETTE (tj-jon3d1 carry-over): covered implicitly -- every request in this
suite goes through the prod image's uvicorn and starlette, so a broken request path fails every
test here. There is deliberately no separate test for it.

SECRET HYGIENE (this module's own rule): no test takes the secret as an argument, and no
assertion expression contains a response body from a write route. See DataStoreHttp in
conftest.py for how a failure is kept from printing it.
"""

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, UpdateType
from data.store.app.database.models.dataset_deletion import DatasetDeletion
from data.store.app.database.models.dataset_override_log import DatasetOverrideLog
from data.store.app.database.models.panel import (
    Panel,
    PanelCadence,
    PanelPass,
    PanelSettingsVersion,
    PanelSourceSettingsVersion,
)
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from routers.common.instance_secret import INSTANCE_SECRET_REJECTION_DETAIL
from routers.data_store.app_endpoints import (
    AssetDataInterface,
    AssetDatasetStoreInterface,
    UiDatasetWritesInterface,
    UiPanelsInterface,
    UiPanelWritesInterface,
)
from tests.system.problem_json import PROBLEM_JSON


pytestmark = pytest.mark.data_store

BAR_TABLE = StockMarketActivity.__table__
ENTRY_TABLE = StoreDatasetEntry.__table__
OVERRIDE_TABLE = DatasetOverrideLog.__table__
DELETION_TABLE = DatasetDeletion.__table__

# The seeded entry is another owner's from the operator's point of view; see the module docstring.
UI_OVERRIDE = {'reason': 'secret probe', 'confirmed': True}
UI_EXPIRY = datetime(2031, 1, 2, tzinfo=UTC)

SEED_START = datetime.fromisoformat('2001-01-02T14:30:00+00:00')
SEED_END = datetime.fromisoformat('2001-03-30T21:00:00+00:00')
# Overlaps the seed (Feb-Apr against Jan-Mar) without being an exact repeat of it.
POST_START = datetime.fromisoformat('2001-02-01T14:30:00+00:00')
POST_END = datetime.fromisoformat('2001-04-30T21:00:00+00:00')

PANEL_TABLE = Panel.__table__
PANEL_SETTINGS_TABLE = PanelSettingsVersion.__table__
SOURCE_SETTINGS_TABLE = PanelSourceSettingsVersion.__table__
PANEL_PASS_TABLE = PanelPass.__table__
# No trading calendar, so the panel manager never runs a pass on a panel here; see the module docstring.
PANEL_SOURCE = DataSource.MANUAL_ENTRY
# The {source} segment as the UI and the edge spell it: the proto enum name.
PANEL_SOURCE_NAME = f'DATA_SOURCE_{PANEL_SOURCE.name}'
# Complete settings: enabled has no default and the cadence is required on a new version (a panel has no size).
PANEL_SETTINGS_BODY = {'enabled': False, 'cadence': 'PANEL_CADENCE_MONTHLY'}
SOURCE_SETTINGS_BODY = {
    'ramp_datasets_per_hour': 1,
    'request_cap_per_hour': 2,
    'corporate_action_window_pre_days': 3,
    'corporate_action_window_post_days': 4,
}


class WriteRoute(StrEnum):
    DATASET_POST = 'dataset-post'
    DATASET_DELETE = 'dataset-delete'
    BAR_POST = 'bar-post'
    UI_EXPIRY_PATCH = 'ui-expiry-patch'
    UI_DATASET_DELETE = 'ui-dataset-delete'
    UI_COLLECTING_PATCH = 'ui-collecting-patch'
    UI_PANEL_CREATE = 'ui-panel-create'
    UI_PANEL_SETTINGS_POST = 'ui-panel-settings-post'
    UI_SOURCE_SETTINGS_POST = 'ui-source-settings-post'


REJECTED = ('absent', 'empty', 'wrong')


@dataclass(frozen=True)
class SeededPanel:
    """The panel the panel writes address, and the names its rows and a request's rows carry.

    Attributes:
        id: The seeded panel, version 1 only.
        source_version_no: The source settings version seeded for PANEL_SOURCE.
        created_name: The name the create request gives its new panel.
        reason: The reason every seeded row and every panel request carries, unique to the run.
    """

    id: uuid.UUID
    source_version_no: int
    created_name: str
    reason: str


def _dataset_path(symbol: str) -> str:
    return AssetDatasetStoreInterface.POST_STORE_ASSET_DATASET.format(
        asset_type=AssetType.STOCK.value, data_type=DataType.MARKET_ACTIVITY.value, asset_symbol=symbol
    )


def _bar_path() -> str:
    return AssetDataInterface.POST_ASSET_DATA.format(
        asset_type=AssetType.STOCK.value, data_type=DataType.MARKET_ACTIVITY.value
    )


def _bars_under(pg_engine: Engine, entry_id: Any) -> int:
    with pg_engine.connect() as conn:
        return conn.execute(
            sa.select(sa.func.count()).select_from(BAR_TABLE).where(BAR_TABLE.c.dataset_id == entry_id)
        ).scalar_one()


def _entry_exists(pg_engine: Engine, entry_id: Any) -> bool:
    with pg_engine.connect() as conn:
        return (
            conn.execute(
                sa.select(sa.func.count()).select_from(ENTRY_TABLE).where(ENTRY_TABLE.c.id == entry_id)
            ).scalar_one()
            == 1
        )


def _stored_expiry(pg_engine: Engine, entry_id: Any) -> Any:
    with pg_engine.connect() as conn:
        return conn.execute(sa.select(ENTRY_TABLE.c.expiry).where(ENTRY_TABLE.c.id == entry_id)).scalar_one()


def _stored_collect(pg_engine: Engine, entry_id: Any) -> Any:
    with pg_engine.connect() as conn:
        return conn.execute(sa.select(ENTRY_TABLE.c.collect).where(ENTRY_TABLE.c.id == entry_id)).scalar_one()


def _as_collecting_subscription(pg_engine: Engine, entry_id: Any) -> None:
    """The seeded STATIC entry as a collecting DAILY subscription, so the collecting PATCH can act on it."""
    with pg_engine.begin() as conn:
        conn.execute(
            sa.update(ENTRY_TABLE)
            .where(ENTRY_TABLE.c.id == entry_id)
            .values(update_type=UpdateType.DAILY.value, expiry_type=ExpiryType.ROLLING.value, collect=True)
        )


def _collect_before(route: 'WriteRoute', entry: sa.Row) -> Any:
    """The collect the entry holds when its one request is sent: the collecting case made it a collecting DAILY."""
    return True if route is WriteRoute.UI_COLLECTING_PATCH else entry.collect


def _audit_rows(pg_engine: Engine, entry_id: Any) -> tuple[int, int]:
    """(override-log rows, deletion rows) recorded against `entry_id`."""
    with pg_engine.connect() as conn:
        return tuple(
            conn.execute(
                sa.select(sa.func.count()).select_from(table).where(table.c.dataset_id == entry_id)
            ).scalar_one()
            for table in (OVERRIDE_TABLE, DELETION_TABLE)
        )


@pytest.fixture
def seeded_entry(insert_entry: Callable[..., sa.Row], own_symbol: str, pg_engine: Engine) -> Iterator[sa.Row]:
    """One entry of the run's owner, Jan-Mar, seeded by SQL: the target every write in a test addresses.

    Teardown deletes the audit rows a /ui/v1 write recorded against it (test infrastructure, as in
    test_http_ui_dataset_writes.py); the entry itself goes with conftest's registry.
    """
    entry = insert_entry(asset_symbol=own_symbol, start=SEED_START, end=SEED_END)
    yield entry
    with pg_engine.begin() as conn:
        for table in (OVERRIDE_TABLE, DELETION_TABLE):
            conn.execute(sa.delete(table).where(table.c.dataset_id == entry.id))


def _panel_rows(pg_engine: Engine) -> tuple[int, int, int]:
    """(panel, panel_settings_version, panel_source_settings_version) rows in the whole database."""
    with pg_engine.connect() as conn:
        return tuple(
            conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()
            for table in (PANEL_TABLE, PANEL_SETTINGS_TABLE, SOURCE_SETTINGS_TABLE)
        )


def _settings_version_nos(pg_engine: Engine, panel_id: Any) -> list[int]:
    with pg_engine.connect() as conn:
        return list(
            conn.execute(
                sa.select(PANEL_SETTINGS_TABLE.c.version_no)
                .where(PANEL_SETTINGS_TABLE.c.panel_id == panel_id)
                .order_by(PANEL_SETTINGS_TABLE.c.version_no)
            ).scalars()
        )


def _panels_named(pg_engine: Engine, name: str) -> list[Any]:
    with pg_engine.connect() as conn:
        return list(conn.execute(sa.select(PANEL_TABLE.c.id).where(PANEL_TABLE.c.name == name)).scalars())


def _source_versions_with_reason(pg_engine: Engine, reason: str) -> list[int]:
    with pg_engine.connect() as conn:
        return list(
            conn.execute(
                sa.select(SOURCE_SETTINGS_TABLE.c.version_no)
                .where(SOURCE_SETTINGS_TABLE.c.source == PANEL_SOURCE, SOURCE_SETTINGS_TABLE.c.reason == reason)
                .order_by(SOURCE_SETTINGS_TABLE.c.version_no)
            ).scalars()
        )


@pytest.fixture
def seeded_panel(pg_engine: Engine, run_identity) -> Iterator[SeededPanel]:
    """A disabled panel on PANEL_SOURCE with its version 1, on a source settings version of its own, seeded by SQL.

    Teardown deletes every panel named with this test's prefix (the seeded one and any a create made), their
    versions and passes, and every PANEL_SOURCE settings version carrying this test's reason.
    """
    tag = uuid.uuid4().hex[:8]
    prefix = f'system-test-{run_identity.run_id}-secret-{tag}'
    reason = f'secret probe {run_identity.run_id} {tag}'
    try:
        with pg_engine.begin() as conn:
            latest = conn.execute(
                sa.select(sa.func.coalesce(sa.func.max(SOURCE_SETTINGS_TABLE.c.version_no), 0)).where(
                    SOURCE_SETTINGS_TABLE.c.source == PANEL_SOURCE
                )
            ).scalar_one()
            conn.execute(
                sa.insert(SOURCE_SETTINGS_TABLE).values(
                    source=PANEL_SOURCE,
                    version_no=latest + 1,
                    # NULL is no limit (ADR tj-2ayia2): 0/0 is refused by the CHECKs now. The seeded panel is disabled
                    # on a source with no calendar, so no limit lets the manager do nothing it would not anyway.
                    ramp_datasets_per_hour=None,
                    request_cap_per_hour=None,
                    corporate_action_window_pre_days=90,
                    corporate_action_window_post_days=90,
                    actor='operator',
                    reason=reason,
                )
            )
            panel_id = conn.execute(
                sa.insert(PANEL_TABLE).values(name=f'{prefix}-seeded', source=PANEL_SOURCE).returning(PANEL_TABLE.c.id)
            ).scalar_one()
            conn.execute(
                sa.insert(PANEL_SETTINGS_TABLE).values(
                    panel_id=panel_id,
                    version_no=1,
                    enabled=False,
                    cadence=PanelCadence.MONTHLY,
                    actor='operator',
                    reason=reason,
                )
            )
        yield SeededPanel(id=panel_id, source_version_no=latest + 1, created_name=f'{prefix}-created', reason=reason)
    finally:
        with pg_engine.begin() as conn:
            panels = sa.select(PANEL_TABLE.c.id).where(PANEL_TABLE.c.name.like(f'{prefix}-%'))
            conn.execute(sa.delete(PANEL_PASS_TABLE).where(PANEL_PASS_TABLE.c.panel_id.in_(panels)))
            conn.execute(sa.delete(PANEL_SETTINGS_TABLE).where(PANEL_SETTINGS_TABLE.c.panel_id.in_(panels)))
            conn.execute(sa.delete(PANEL_TABLE).where(PANEL_TABLE.c.name.like(f'{prefix}-%')))
            conn.execute(
                sa.delete(SOURCE_SETTINGS_TABLE).where(
                    SOURCE_SETTINGS_TABLE.c.source == PANEL_SOURCE, SOURCE_SETTINGS_TABLE.c.reason == reason
                )
            )


@pytest.fixture
def send_write(
    data_store, run_identity, bar_create_body, adopt_entries, pg_engine, seeded_panel
) -> Callable[..., httpx.Response]:
    """Send one write on `route` against `entry` (or the seeded panel), carrying `auth`. The owner is the entry's own."""

    def _send(route: WriteRoute, entry: sa.Row, auth: str) -> httpx.Response:
        match route:
            case WriteRoute.DATASET_POST:
                body = {
                    'owner': run_identity.owner,
                    'source': entry.source.value,
                    'granularity': entry.granularity.value,
                    'start': POST_START.isoformat(),
                    'end': POST_END.isoformat(),
                }
                try:
                    return data_store.post(_dataset_path(entry.asset_symbol), json=body, auth=auth)
                finally:
                    adopt_entries(entry.asset_symbol)
            case WriteRoute.DATASET_DELETE:
                path = AssetDatasetStoreInterface.DELETE_STORE_ASSET_DATASET_BY_ID.format(id=entry.id)
                return data_store.delete(path, params={'owner': run_identity.owner}, auth=auth)
            case WriteRoute.BAR_POST:
                return data_store.post(_bar_path(), json=bar_create_body(entry, entry.start), auth=auth)
            case WriteRoute.UI_EXPIRY_PATCH:
                path = UiDatasetWritesInterface.PATCH_UI_DATASET_EXPIRY.format(dataset_id=entry.id)
                body = {'expiry': UI_EXPIRY.isoformat(), 'override': UI_OVERRIDE}
                return data_store.patch(path, json=body, auth=auth)
            case WriteRoute.UI_DATASET_DELETE:
                path = UiDatasetWritesInterface.DELETE_UI_DATASET.format(dataset_id=entry.id)
                return data_store.delete(path, json={'override': UI_OVERRIDE}, auth=auth)
            case WriteRoute.UI_COLLECTING_PATCH:
                _as_collecting_subscription(pg_engine, entry.id)
                path = UiDatasetWritesInterface.PATCH_UI_DATASET_COLLECTING.format(dataset_id=entry.id)
                return data_store.patch(path, json={'collect': False, 'override': UI_OVERRIDE}, auth=auth)
            case WriteRoute.UI_PANEL_CREATE:
                # No source settings: PANEL_SOURCE has the seeded version, so the create needs none.
                body = {
                    **PANEL_SETTINGS_BODY,
                    'name': seeded_panel.created_name,
                    'source': PANEL_SOURCE_NAME,
                    'reason': seeded_panel.reason,
                }
                return data_store.post(UiPanelWritesInterface.POST_UI_PANELS, json=body, auth=auth)
            case WriteRoute.UI_PANEL_SETTINGS_POST:
                path = UiPanelWritesInterface.POST_UI_PANEL_SETTINGS_VERSIONS.format(panel_id=seeded_panel.id)
                body = {**PANEL_SETTINGS_BODY, 'reason': seeded_panel.reason}
                return data_store.post(path, json=body, auth=auth)
            case WriteRoute.UI_SOURCE_SETTINGS_POST:
                path = UiPanelWritesInterface.POST_UI_PANEL_SOURCE_SETTINGS_VERSIONS.format(source=PANEL_SOURCE_NAME)
                body = {**SOURCE_SETTINGS_BODY, 'reason': seeded_panel.reason}
                return data_store.post(path, json=body, auth=auth)
        raise AssertionError(f'unhandled route {route!r}')

    return _send


@pytest.mark.parametrize('auth', REJECTED)
@pytest.mark.parametrize('route', list(WriteRoute))
def test_write_without_the_right_secret_is_401_and_changes_nothing(
    route: WriteRoute,
    auth: str,
    seeded_entry: sa.Row,
    send_write: Callable[..., httpx.Response],
    data_store,
    adopt_entries: Callable[[str], list],
    pg_engine: Engine,
) -> None:
    """7a/7b: absent, empty or wrong secret -> 401 with the fixed detail, no secret in the body, no change."""
    panel_rows = _panel_rows(pg_engine)
    response = send_write(route, seeded_entry, auth)

    # FIRST, and as a bool: if the body leaked the secret, no later assertion may print the body.
    # Body and raw header values both: response_leaks_secret explains why not str(response.headers).
    leaked = data_store.response_leaks_secret(response)
    assert not leaked, f'{route} {auth}: the 401 response carries the instance secret (value withheld)'
    assert response.status_code == 401, data_store.describe(response)
    assert response.headers['content-type'] == PROBLEM_JSON, data_store.describe(response)
    # WHOLE-BODY EQUALITY IS KEPT, and keeping it is the point (tj-3mk3u5.37.9). The old assertion
    # compared the body to exactly {'detail': ...}, which is what proved the 401 carries NOTHING
    # ELSE -- no secret, no echo of the request, no stray member. TE-6 (caf68f5) re-rendered it as
    # problem+json, so the literal moved; if it had been relaxed to a membership check at the same
    # time, this test would have kept its name and quietly stopped guarding the thing it exists
    # for. The 401 goes through the HTTPException handler, which is deliberately NOT one of ours:
    # no reason, no domain and no error_id, which is why those three are absent here and asserting
    # the whole body is what pins their absence.
    assert response.json() == {
        'type': 'about:blank',
        'title': 'Unauthorized',
        'status': 401,
        'detail': INSTANCE_SECRET_REJECTION_DETAIL,
    }, data_store.describe(response)

    # Nothing written, nothing removed.
    assert _entry_exists(pg_engine, seeded_entry.id), f'{route} {auth}: the seeded entry is gone after a 401'
    assert adopt_entries(seeded_entry.asset_symbol) == [seeded_entry.id], (
        f'{route} {auth}: an entry other than the seeded one exists after a 401'
    )
    bars = _bars_under(pg_engine, seeded_entry.id)
    assert bars == 0, f'{route} {auth}: {bars} bar(s) under the seeded entry after a 401'
    expiry = _stored_expiry(pg_engine, seeded_entry.id)
    assert expiry == seeded_entry.expiry, f'{route} {auth}: the expiry moved to {expiry} after a 401'
    collect = _stored_collect(pg_engine, seeded_entry.id)
    assert collect == _collect_before(route, seeded_entry), f'{route} {auth}: collect moved to {collect} after a 401'
    audit = _audit_rows(pg_engine, seeded_entry.id)
    assert audit == (0, 0), f'{route} {auth}: (override, deletion) audit rows {audit} after a 401'
    after = _panel_rows(pg_engine)
    assert after == panel_rows, f'{route} {auth}: (panel, settings, source settings) rows {panel_rows} -> {after}'


@pytest.mark.parametrize('route', list(WriteRoute))
def test_write_with_the_right_secret_reaches_the_handler(
    route: WriteRoute,
    seeded_entry: sa.Row,
    seeded_panel: SeededPanel,
    send_write: Callable[..., httpx.Response],
    data_store,
    adopt_entries: Callable[[str], list],
    pg_engine: Engine,
) -> None:
    """7c: the right secret is accepted and the handler does its work.

    Dataset POST: 409 own-overlap on the seeded entry -- the handler ran; see the module
    docstring for why this is the success signal here. DELETE: 200 and the entry is gone. Bar
    POST: 200 and the bar is stored. /ui/v1 PATCH: 200, the expiry stored and one override row.
    /ui/v1 DELETE: 204, the entry gone, one override row and one deletion row. /ui/v1 collecting
    PATCH: 200, collect stored false and one override row. Panel create: 201, one panel of the
    name sent with its version 1, no source version. Panel settings POST: 201 and the seeded panel's
    version 2. Source settings POST: 201 and one more PANEL_SOURCE version, numbered after the seeded one.
    """
    panel_rows = _panel_rows(pg_engine)
    response = send_write(route, seeded_entry, 'right')

    # FIRST, as in the 401 test. This is the one path where the RECEIVED header value IS the
    # secret, so a response that echoed a request header would leak here and nowhere else --
    # hence the headers, not only the body.
    leaked = data_store.response_leaks_secret(response)
    assert not leaked, f'{route}: the response carries the instance secret (value withheld)'
    match route:
        case WriteRoute.DATASET_POST:
            assert response.status_code == 409, data_store.describe(response)
            assert adopt_entries(seeded_entry.asset_symbol) == [seeded_entry.id], data_store.describe(response)
        case WriteRoute.DATASET_DELETE:
            assert response.status_code == 200, data_store.describe(response)
            assert not _entry_exists(pg_engine, seeded_entry.id), data_store.describe(response)
        case WriteRoute.BAR_POST:
            assert response.status_code == 200, data_store.describe(response)
            bars = _bars_under(pg_engine, seeded_entry.id)
            assert bars == 1, f'{bars} bar(s) under the seeded entry; {data_store.describe(response)}'
        case WriteRoute.UI_EXPIRY_PATCH:
            assert response.status_code == 200, data_store.describe(response)
            assert _stored_expiry(pg_engine, seeded_entry.id) == UI_EXPIRY, data_store.describe(response)
            assert _audit_rows(pg_engine, seeded_entry.id) == (1, 0), data_store.describe(response)
        case WriteRoute.UI_DATASET_DELETE:
            assert response.status_code == 204, data_store.describe(response)
            assert not _entry_exists(pg_engine, seeded_entry.id), data_store.describe(response)
            assert _audit_rows(pg_engine, seeded_entry.id) == (1, 1), data_store.describe(response)
        case WriteRoute.UI_COLLECTING_PATCH:
            assert response.status_code == 200, data_store.describe(response)
            assert _stored_collect(pg_engine, seeded_entry.id) is False, data_store.describe(response)
            assert _audit_rows(pg_engine, seeded_entry.id) == (1, 0), data_store.describe(response)
        case WriteRoute.UI_PANEL_CREATE:
            assert response.status_code == 201, data_store.describe(response)
            (created,) = _panels_named(pg_engine, seeded_panel.created_name)
            assert response.json()['id'] == str(created), data_store.describe(response)
            assert _settings_version_nos(pg_engine, created) == [1], data_store.describe(response)
            assert _delta(panel_rows, _panel_rows(pg_engine)) == (1, 1, 0), data_store.describe(response)
        case WriteRoute.UI_PANEL_SETTINGS_POST:
            assert response.status_code == 201, data_store.describe(response)
            assert _settings_version_nos(pg_engine, seeded_panel.id) == [1, 2], data_store.describe(response)
            assert _delta(panel_rows, _panel_rows(pg_engine)) == (0, 1, 0), data_store.describe(response)
        case WriteRoute.UI_SOURCE_SETTINGS_POST:
            assert response.status_code == 201, data_store.describe(response)
            seeded_no = seeded_panel.source_version_no
            versions = _source_versions_with_reason(pg_engine, seeded_panel.reason)
            assert versions == [seeded_no, seeded_no + 1], data_store.describe(response)
            assert _delta(panel_rows, _panel_rows(pg_engine)) == (0, 0, 1), data_store.describe(response)


def _delta(before: tuple[int, ...], after: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(now - then for then, now in zip(before, after, strict=True))


@pytest.mark.parametrize('auth', ['absent', 'wrong'])
def test_panel_reads_are_open_without_the_secret(auth: str, seeded_panel: SeededPanel, data_store) -> None:
    """7d for the panel writes: the detail, its settings versions and the source's versions answer 200 without it."""
    detail = data_store.get(UiPanelsInterface.GET_UI_PANEL.format(panel_id=seeded_panel.id), auth=auth)
    assert detail.status_code == 200, data_store.describe(detail)
    assert detail.json()['id'] == str(seeded_panel.id), data_store.describe(detail)

    versions = data_store.get(
        UiPanelsInterface.GET_UI_PANEL_SETTINGS_VERSIONS.format(panel_id=seeded_panel.id), auth=auth
    )
    assert versions.status_code == 200, data_store.describe(versions)
    assert [item['versionNo'] for item in versions.json()['items']] == [1], data_store.describe(versions)

    sources = data_store.get(
        UiPanelsInterface.GET_UI_PANEL_SOURCE_SETTINGS_VERSIONS.format(source=PANEL_SOURCE_NAME), auth=auth
    )
    assert sources.status_code == 200, data_store.describe(sources)
    newest = sources.json()['items'][0]
    assert (newest['versionNo'], newest['reason']) == (seeded_panel.source_version_no, seeded_panel.reason), (
        data_store.describe(sources)
    )


@pytest.mark.parametrize('auth', ['absent', 'wrong'])
def test_reads_are_open_without_the_secret(
    auth: str, seeded_entry: sa.Row, data_store, bar_create_body: Callable[..., dict[str, Any]]
) -> None:
    """7d: both GETs answer 200 with no secret, or a wrong one, and return what was written."""
    stored = data_store.post(_bar_path(), json=bar_create_body(seeded_entry, seeded_entry.start), auth='right')
    assert stored.status_code == 200, data_store.describe(stored)

    entries = data_store.get(_dataset_path(seeded_entry.asset_symbol), auth=auth)
    assert entries.status_code == 200, data_store.describe(entries)
    assert [entry['id'] for entry in entries.json()] == [str(seeded_entry.id)], data_store.describe(entries)

    bars = data_store.get(
        AssetDataInterface.GET_ASSET_DATA.format(
            asset_type=AssetType.STOCK.value, data_type=DataType.MARKET_ACTIVITY.value
        ),
        params={'asset_symbol': seeded_entry.asset_symbol},
        auth=auth,
    )
    assert bars.status_code == 200, data_store.describe(bars)
    assert [bar['id'] for bar in bars.json()] == [stored.json()['id']], data_store.describe(bars)
