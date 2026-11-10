"""The /ui/v1 panel routes (tj-7zjov1.36, P24) on the running data_store and the real, migrated Postgres.

WRITTEN, NOT YET RUN where it was written (validator P24v tj-7zjov1.241, 2026-11-01: no stack budget). It runs with the
rest of the suite (make test-system, or the agent-stack run_system_tests after test_up) on a stack migrated to the
alpha baseline (ADR tj-kg521d). PR1 also round-trips a settings version with NO reason (ADR tj-46jkey): stored NULL on
Postgres, absent from R2, R3 and R5.

THE DESIGN: the ROUTE CONTRACT and READ RULES on tj-7zjov1.36 and ADR tj-26jt9w addenda 20 to 22. WHAT THIS PROVES that
server/data/store/tests/test_panel_ui_reads.py and test_ui_panel_writes_route.py cannot, because SQLite has no Postgres
enums, no varchar[], no row or advisory locks, one writer, and stores uuids as text:
  PR1 every read R1 to R8 and every write W1 to W3 compiles and round-trips on asyncpg through the real image's uvicorn:
      the panel_cadence, panel_pass_kind, panel_transition, asset_list_status and asset_list_event_kind enums, the
      varchar[] allow and deny lists, the window functions of the effective-pass and minted-dataset reads, and the
      UNION subquery of panel_catalog_rows;
  PR2 W1 answers 201 with the PanelDetail the contract names, and W2 appends version 2 whose filters read back from R3;
      no dataset_override_log row is written by either (the version row is the audit);
  PR3 CONCURRENT creates of ONE name: exactly one 201, every other answer a 422 INVALID_REQUEST (the contract: every
      store refusal is a 422), and one panel row. EXPECTED RED until create_panel changes: its name check is a read
      before the INSERT with no lock, so a loser of the race hits the unique index, and write_transaction turns that
      IntegrityError into DATABASE_INTEGRITY, a 409 that pages an operator (vocabulary.py). Raised on P24v's verdict;
      the fix is in crud/panel_settings.py, outside P24's file scope;
  PR4 R8's keyset over (as_of, id) on Postgres uuid ordering: rows tied on as_of page by id descending with no loss and
      no repeat;
  PR5 R7 on the ALPACA source reads the stack's own snapshots: one day per XNYS session in the window, ascending, each
      a known state, changed_count 0 unless SAVED.

ISOLATION. Panels are created DISABLED and on MANUAL_ENTRY where the source does not matter, so the panel manager never
runs a pass for them (it skips a source with no trading calendar, and a disabled panel). Panel names carry the run id.
Source versions are written only for MANUAL_ENTRY when it has none, and every row a test writes is deleted at its end, a
test-only delete (the modules themselves are append-only).
"""

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from common.database.sql_alchemy_nullable_datetime import NullableDateTime
from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from data.store.app.database.models.corporate_action import CorporateAction
from data.store.app.database.models.dataset_override_log import DatasetOverrideLog
from data.store.app.database.models.panel import Panel, PanelPass, PanelSettingsVersion, PanelSourceSettingsVersion
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from tests.system.problem_json import assert_problem


pytestmark = [pytest.mark.data_store]

PANELS = Panel.__table__
SETTINGS = PanelSettingsVersion.__table__
SOURCE_SETTINGS = PanelSourceSettingsVersion.__table__
PASSES = PanelPass.__table__
OVERRIDES = DatasetOverrideLog.__table__
ACTIONS = CorporateAction.__table__
ENTRIES = StoreDatasetEntry.__table__
MANUAL = 'DATA_SOURCE_MANUAL_ENTRY'
CONCURRENT = 6
RATES = {'rampDatasetsPerHour': 1, 'requestCapPerHour': 2}


class Written:
    """The ids one test wrote, deleted at its end."""

    def __init__(self) -> None:
        self.panels: list[uuid.UUID] = []
        self.source_versions: list[uuid.UUID] = []
        self.datasets: list[uuid.UUID] = []
        self.actions: list[uuid.UUID] = []


@pytest.fixture
def written(pg_engine: Engine) -> Iterator[Written]:
    ids = Written()
    yield ids
    with pg_engine.begin() as conn:
        if ids.panels:
            conn.execute(sa.delete(PASSES).where(PASSES.c.panel_id.in_(ids.panels)))
            conn.execute(sa.delete(SETTINGS).where(SETTINGS.c.panel_id.in_(ids.panels)))
            conn.execute(sa.delete(PANELS).where(PANELS.c.id.in_(ids.panels)))
        if ids.source_versions:
            conn.execute(sa.delete(SOURCE_SETTINGS).where(SOURCE_SETTINGS.c.id.in_(ids.source_versions)))
        if ids.actions:
            conn.execute(sa.delete(ACTIONS).where(ACTIONS.c.id.in_(ids.actions)))
        if ids.datasets:
            conn.execute(sa.delete(ENTRIES).where(ENTRIES.c.id.in_(ids.datasets)))


def _override_rows(pg_engine: Engine) -> int:
    with pg_engine.connect() as conn:
        return conn.execute(sa.select(sa.func.count()).select_from(OVERRIDES)).scalar_one()


def _create(data_store, pg_engine: Engine, written: Written, name: str, source: str = MANUAL) -> httpx.Response:
    """W1 for a DISABLED panel, giving the source's first settings only when the source has none yet."""
    has_version = data_store.get(f'/ui/v1/panel-sources/{source}/settings-versions').json().get('items')
    body = {
        'name': name,
        'source': source,
        'granularity': 'GRANULARITY_ONE_DAY',
        'enabled': False,
        'reason': 'system test panel',
    }
    response = data_store.post('/ui/v1/panels', json=body | ({} if has_version else RATES), auth='right')
    if response.status_code == 201:
        written.panels.append(uuid.UUID(response.json()['id']))
        if not has_version:
            with pg_engine.connect() as conn:
                written.source_versions += conn.execute(
                    sa.select(SOURCE_SETTINGS.c.id).where(SOURCE_SETTINGS.c.source == DataSource[source[12:]])
                ).scalars()
    return response


# --------------------------------------------------------------------------------------------- PR1 PR2


def test_every_panel_route_round_trips_on_postgres(data_store, pg_engine, written, run_identity):
    overrides = _override_rows(pg_engine)
    created = _create(data_store, pg_engine, written, f'system-test-{run_identity.run_id}-routes')
    assert created.status_code == 201, data_store.describe(created)
    detail = created.json()
    panel = detail['id']
    assert detail['currentVersion']['versionNo'] == 1 and 'effectivePass' not in detail['currentVersion']
    assert detail['currentVersion']['settings']['cadence'] == 'PANEL_CADENCE_MONTHLY'
    assert detail.get('counts', {}) == {}

    saved = data_store.post(
        f'/ui/v1/panels/{panel}/settings-versions',
        json={
            'enabled': False,
            'cadence': 'PANEL_CADENCE_WEEKLY',
            'allowSymbols': ['ZZA', 'ZZB'],
            'denySymbols': ['ZZC'],
            'assetClass': 'ASSET_TYPE_STOCK',
            # No reason: optional since ADR tj-46jkey. Stored NULL (the not-blank CHECK passes a NULL on Postgres),
            # served as '' (absent from canonical JSON) by R2, R3 and R5 below.
            'reason': '  ',
        },
        auth='right',
    )
    assert saved.status_code == 201 and saved.json()['versionNo'] == 2, data_store.describe(saved)
    assert 'reason' not in saved.json() and 'size' not in saved.json()['settings']
    with pg_engine.connect() as conn:
        stored = conn.execute(
            sa.select(SETTINGS.c.reason).where(SETTINGS.c.panel_id == uuid.UUID(panel), SETTINGS.c.version_no == 2)
        ).scalar_one()
    assert stored is None, f'a blank reason was stored as {stored!r}, not NULL'
    assert _override_rows(pg_engine) == overrides, 'a panel write added an override-log row'

    listed = [item for item in data_store.get('/ui/v1/panels').json()['items'] if item['id'] == panel]
    assert len(listed) == 1 and not listed[0].get('enabled', False)
    current = data_store.get(f'/ui/v1/panels/{panel}').json()['currentVersion']
    assert current['versionNo'] == 2 and 'reason' not in current
    versions = data_store.get(f'/ui/v1/panels/{panel}/settings-versions', params={'limit': 1}).json()
    assert versions['items'][0]['settings']['allowSymbols'] == ['ZZA', 'ZZB'] and versions['nextCursor']
    assert 'reason' not in versions['items'][0]
    rest = data_store.get(
        f'/ui/v1/panels/{panel}/settings-versions', params={'limit': 1, 'cursor': versions['nextCursor']}
    ).json()
    assert [item['versionNo'] for item in rest['items']] == [1]
    assert data_store.get(f'/ui/v1/panel-sources/{MANUAL}/settings-versions').json()['items']
    now = datetime.now(UTC)
    window = {'start': (now - timedelta(days=1)).isoformat(), 'end': (now + timedelta(days=1)).isoformat()}
    markers = data_store.get(f'/ui/v1/panels/{panel}/boundaries', params=window).json()['markers']
    panel_markers = [m for m in markers if m['kind'] == 'PANEL_MARKER_KIND_PANEL_SETTINGS']
    assert [m['versionNo'] for m in panel_markers] == [1, 2]
    assert panel_markers[0]['reason'] == 'system test panel' and 'reason' not in panel_markers[1]
    assert data_store.get(f'/ui/v1/panels/{panel}/members').json() == {}
    history = data_store.get(f'/ui/v1/panels/{panel}/asset-list-history', params=window)
    assert_problem(history, status=422, reason='INVALID_REQUEST', title='Unprocessable Entity')  # no calendar
    missing = data_store.get(f'/ui/v1/datasets/{uuid.uuid4()}/corporate-actions')
    assert_problem(missing, status=404, reason='NOT_FOUND', title='Not Found')


# --------------------------------------------------------------------------------------------- PR3 concurrent create


@pytest.mark.asyncio
async def test_concurrent_creates_of_one_name_leave_one_panel_and_answer_422_otherwise(
    data_store, data_store_url, pg_engine, written, run_identity
):
    name = f'system-test-{run_identity.run_id}-race-{uuid.uuid4().hex[:6]}'
    # Make sure MANUAL_ENTRY has a version first, so no create carries source settings (that race is PS8's).
    first = _create(data_store, pg_engine, written, f'{name}-seed')
    assert first.status_code == 201, data_store.describe(first)
    body = {'name': name, 'source': MANUAL, 'granularity': 'GRANULARITY_ONE_DAY', 'enabled': False, 'reason': 'race'}
    headers = data_store._headers('right')

    async with httpx.AsyncClient(base_url=data_store_url, timeout=30.0) as client:
        answers = await asyncio.gather(
            *(client.post('/ui/v1/panels', json=body, headers=headers) for _ in range(CONCURRENT))
        )

    statuses = sorted(answer.status_code for answer in answers)
    with pg_engine.connect() as conn:
        ids = list(conn.execute(sa.select(PANELS.c.id).where(PANELS.c.name == name)).scalars())
    written.panels += ids
    assert statuses == [201] + [422] * (CONCURRENT - 1), statuses
    assert len(ids) == 1
    for answer in answers:
        if answer.status_code == 422:
            assert answer.json()['reason'] == 'INVALID_REQUEST'


# --------------------------------------------------------------------------------------------- PR4 R8 keyset on uuids


def test_corporate_action_keyset_breaks_an_as_of_tie_by_uuid_descending_on_postgres(
    data_store, pg_engine, written, own_symbol
):
    dataset = uuid.uuid4()
    instant = datetime(2026, 9, 15, tzinfo=UTC)
    with pg_engine.begin() as conn:
        conn.execute(
            ENTRIES.insert().values(
                id=dataset,
                owner='panel',
                source=DataSource.ALPACA_API,
                feed=Feed.IEX,
                asset_symbol=own_symbol,
                asset_type=AssetType.STOCK,
                data_type=DataType.MARKET_ACTIVITY,
                granularity=Granularity.ONE_DAY,
                start=datetime(2026, 9, 1, tzinfo=UTC),
                end=NullableDateTime.EPOCH,
                expiry_type=ExpiryType.ROLLING.value,
                update_type=UpdateType.DAILY.value,
                collect=True,
            )
        )
        written.datasets.append(dataset)
        for n in range(7):
            action_id = uuid.uuid4()
            conn.execute(
                ACTIONS.insert().values(
                    id=action_id,
                    source=DataSource.ALPACA_API,
                    vendor_action_id=f'sys-{n}',
                    action_type='cash_dividend',
                    raw_payload=action_id.bytes,
                    payload_hash=action_id.hex,
                    request_id=uuid.uuid4(),
                    attempt_no=1,
                    requesting_symbol=own_symbol,
                    as_of=instant if n < 5 else instant - timedelta(days=n),
                )
            )
            written.actions.append(action_id)
        expected = list(
            conn.execute(
                sa.select(ACTIONS.c.vendor_action_id)
                .where(ACTIONS.c.requesting_symbol == own_symbol)
                .order_by(ACTIONS.c.as_of.desc(), ACTIONS.c.id.desc())
            ).scalars()
        )

    seen: list[str] = []
    cursor = None
    for _ in range(6):  # bounded: four pages are due
        params = {'limit': 2} | ({'cursor': cursor} if cursor else {})
        page = data_store.get(f'/ui/v1/datasets/{dataset}/corporate-actions', params=params).json()
        seen += [item['vendorActionId'] for item in page.get('items', [])]
        cursor = page.get('nextCursor')
        if not cursor:
            break
    else:
        pytest.fail(f'the corporate-action cursor never ended: {seen}')
    assert seen == expected and len(seen) == 7


# --------------------------------------------------------------------------------------------- PR5 R7 on XNYS


def test_the_alpaca_asset_list_history_reads_one_day_per_session(data_store, pg_engine, written, run_identity):
    created = _create(
        data_store, pg_engine, written, f'system-test-{run_identity.run_id}-history', source='DATA_SOURCE_ALPACA_API'
    )
    assert created.status_code == 201, data_store.describe(created)
    end = datetime.now(UTC)
    start = end - timedelta(days=30)
    body = data_store.get(
        f'/ui/v1/panels/{created.json()["id"]}/asset-list-history',
        params={'start': start.isoformat(), 'end': end.isoformat()},
    ).json()
    days = [day['day'] for day in body['days']]
    assert days == sorted(days) and 18 <= len(days) <= 23
    states = {'SAVED', 'EMPTY', 'MISSING', 'NOT_DUE'}
    for day in body['days']:
        state = day['state'].removeprefix('ASSET_LIST_DAY_STATE_')
        assert state in states
        assert state == 'SAVED' or day.get('changedCount', 0) == 0
