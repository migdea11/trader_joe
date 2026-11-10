"""The /ui/v1 panel reads R1 to R8 (tj-7zjov1.36, P24), validated by P24v tj-7zjov1.241, over SQLite.

THE DESIGN is the ROUTE CONTRACT and READ RULES ruled on tj-7zjov1.36 (03:05 UTC 2026-11-01), ADR tj-26jt9w addenda 20 to
22 and panels.proto. What is pinned, rule by rule:
  R1  every panel by name then id; enabled from the LATEST settings version; a stray query parameter is a 422.
  R2  the current settings version and the current source version, each with its effective pass; the SOURCE version's
      effective pass comes from THIS panel's passes only (the builder's choice, the boundary read's rule); counts from
      latest_state: members = latest ENTERED, CREATED, DEFERRED, REFUSED or DRAINING; collecting = those of ENTERED,
      CREATED or DRAINING whose dataset collects (a DEFERRED or REFUSED member counts as a member, NEVER as collecting,
      even with a collecting dataset); draining = DRAINING; lagging = members whose dataset is LATE or OVERDUE. The
      member's dataset is the transition's own dataset_id when set, else its CREATED request's dataset. Dry-run ignored.
  R3  settings versions newest first, keyset on version_no, the FULL settings (filters, allow and deny lists) and the
      effective pass with its KIND (the first non-dry-run pass naming the version, by started_at).
  R4  a source's versions newest first; the effective pass is ANY panel's on the source; an empty page, never a 404,
      for a source with none; {source} is the DataSource NAME, with or without the DATA_SOURCE_ prefix.
  R5  markers in [start, end), oldest first: settings and source versions and the override log's collect stops
      (new_value 'false') and resumes ('true'); a row with no new_value is left out; start < end, aware, both required.
  R6  every vendor asset id with a latest transition (EXITED and DRAINED included), keyset over (symbol, vendor id);
      appeared_on is the source-calendar day of the latest APPEARED on the ACTIVE list; vanished_on that of the latest
      VANISHED when later than it, OR WHEN THERE IS NO APPEARED (the builder's choice); both empty for a source with no
      trading calendar (the builder's choice). Health from the catalog's own evaluation at the request clock.
  R7  one day per session whose local midnight is in [start, end), ascending; SAVED, EMPTY, MISSING (close plus
      DAILY_REFRESH_SETTLE_MINUTES passed) and NOT_DUE from the ACTIVE list; changed_count the distinct vendor ids with
      an event of any kind on either list dated that day, 0 unless SAVED; at most 366 days; no calendar is a 422.
  R8  every observation with the dataset's source and requesting_symbol equal to its symbol, whichever request
      delivered it, NOT filtered by any window; newest as_of first, keyset (as_of, id); a vendor revision its own item.
  ALL limit 1 to 500 (default 100); cursors opaque and bound to their route and parent; a constant statement count per
      request, never one per panel, member or version.

NOT PINNED HERE, ON PURPOSE: the health of a manager-created dataset. Its start is point in time since ADR tj-iov38l
(the local midnight of the member's ENTERED date, no longer 1990-01-01), and its health on the real XNYS calendar is
pinned in test_panel_dataset_start.py (b). Every dataset here starts 1 Sep 2026, a valid calendar date.
"""

import base64
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.sql import Select

from common.enums.data_select import AssetType
from data.store.app import panel_views
from data.store.app.database.crud.panel_reads import MemberAssetEvents
from data.store.app.database.database import async_db
from data.store.app.database.models.asset_list import AssetListEventKind
from data.store.app.database.models.dataset_override_log import OverrideAction
from data.store.app.database.models.panel import PanelCadence, PanelPassKind, PanelTransition
from data.store.app.main import app
from data.store.app.trading_calendar import calendar_for_source
from data.store.tests.panel_ui_harness import (
    NO_CALENDAR_SOURCE,
    NOW,
    SETTINGS,
    SOURCE,
    SOURCE_SETTINGS,
    World,
    created,
    entered,
    make_world,
    moved,
    ny_midnight,
    ordered,
)
from data.store.tests.problem_body import problem
from routers.data_store.ui_datasets import utc_now
from schemas.data_ingest.submit import AssetListStatus


pytestmark = [pytest.mark.data_store, pytest.mark.asyncio]

DAY = timedelta(days=1)
T0 = datetime(2026, 10, 2, 14, tzinfo=UTC)
T1 = T0 + DAY
C, D, R = PanelTransition.CREATED, PanelTransition.DEFERRED, PanelTransition.REFUSED
X, DRAINING, DRAINED = PanelTransition.EXITED, PanelTransition.DRAINING, PanelTransition.DRAINED


@pytest_asyncio.fixture
async def world(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[World]:
    state = await make_world(monkeypatch)
    monkeypatch.delenv('DAILY_REFRESH_SETTLE_MINUTES', raising=False)

    async def pinned_now() -> datetime:
        return NOW

    app.dependency_overrides[async_db] = state.request_session
    app.dependency_overrides[utc_now] = pinned_now
    try:
        yield state
    finally:
        app.dependency_overrides.clear()
        await state.engine.dispose()


@pytest_asyncio.fixture
async def client(world: World) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://store') as http:
        yield http


async def get(client: httpx.AsyncClient, path: str, **params: Any) -> dict:
    response = await client.get(path, params=params)
    assert response.status_code == 200, response.text
    assert response.headers['content-type'].startswith('application/json')
    return response.json()


async def statements_of(world: World, client: httpx.AsyncClient, path: str, **params: Any) -> int:
    world.reset_recording()
    response = await client.get(path, params=params)
    assert response.status_code == 200, response.text
    return len(world.statements)


def iso(at: datetime) -> str:
    return at.isoformat()


def stamp(at: datetime) -> str:
    """An instant as canonical JSON renders a Timestamp."""
    return at.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def forged(payload: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':')).encode()).decode().rstrip('=')


# --------------------------------------------------------------------------------------------- the member world


def sym(vendor: str) -> str:
    return vendor.upper() * 3


class Members:
    """One ALPACA panel whose ledger covers every latest status, with the datasets and asset-list events it reads."""

    def __init__(self) -> None:
        self.datasets: dict[str, uuid.UUID] = {}


@pytest_asyncio.fixture
async def members(world: World) -> Members:
    out = Members()
    out.seeded = await world.panel('members')
    ds = out.datasets
    ds['a'] = await world.dataset('AAA', bars=(ny_midnight(5), ny_midnight(6)))  # FRESH
    ds['b'] = await world.dataset('BBB', bars=(ny_midnight(5),))  # LATE
    ds['c'] = await world.dataset('CCC', bars=(ny_midnight(6),))  # FRESH, collecting, but the member is DEFERRED
    ds['d'] = await world.dataset('DDD', bars=(ny_midnight(6),))  # FRESH, collecting, but the member is REFUSED
    ds['e_minted'] = await world.dataset(
        'EEE', bars=(ny_midnight(6),), start=ny_midnight(2, 9)
    )  # what e's CREATED request minted: FRESH
    ds['e'] = await world.dataset('EEE', bars=(ny_midnight(1),))  # what e's DRAINING row names directly: OVERDUE
    ds['f'] = await world.dataset('FFF', bars=(ny_midnight(1),))  # OVERDUE, but the member EXITED
    ds['i'] = await world.dataset('III', bars=(ny_midnight(1),), collect=False)  # stopped: RETIRED
    requests = {vendor: await world.request(ds.get(vendor)) for vendor in 'abcdfi'}
    requests['e'] = await world.request(ds['e_minted'])
    requests['h'] = await world.request(None)  # not delivered yet: no dataset
    first = []
    for vendor in 'abcdefhi':
        first += [entered(vendor, T0, sym(vendor)), created(vendor, T0, requests[vendor], sym(vendor))]
    first += [entered('g', T0, sym('g')), entered('zz-a', T0, symbol='AAA')]
    await world.open(out.seeded, started_at=T0, rows=tuple(first))
    await world.open(
        out.seeded,
        started_at=T1,
        rows=(
            moved('c', D, T1, cause='ramp_rate', symbol='CCC'),
            moved('d', R, T1, cause='own_overlap', symbol='DDD'),
            moved('e', DRAINING, T1, dataset_id=ds['e'], symbol='EEE'),
            moved('f', X, T1, symbol='FFF'),
            moved('g', DRAINED, T1, symbol='GGG'),
        ),
    )
    # A dry-run pass: every read ignores it (a stays CREATED, 'dry' is never listed).
    await world.open(
        out.seeded, dry_run=True, started_at=T1 + DAY, rows=(entered('dry', T1 + DAY), moved('a', X, T1 + DAY))
    )
    kinds = AssetListEventKind
    await world.asset_event(kinds.APPEARED, 'a', datetime(2026, 9, 20, 15, tzinfo=UTC))
    await world.asset_event(kinds.APPEARED, 'a', datetime(2026, 10, 2, 2, tzinfo=UTC))  # 1 Oct, 22:00 in New York
    await world.asset_event(kinds.APPEARED, 'b', datetime(2026, 10, 1, 15, tzinfo=UTC))
    await world.asset_event(kinds.VANISHED, 'b', datetime(2026, 10, 3, 15, tzinfo=UTC))
    await world.asset_event(kinds.STATUS_FLIP, 'b', datetime(2026, 10, 5, 15, tzinfo=UTC))
    await world.asset_event(kinds.VANISHED, 'c', datetime(2026, 9, 30, 15, tzinfo=UTC))
    await world.asset_event(kinds.APPEARED, 'c', datetime(2026, 10, 2, 15, tzinfo=UTC))
    await world.asset_event(kinds.APPEARED, 'd', datetime(2026, 10, 1, 15, tzinfo=UTC))
    await world.asset_event(kinds.VANISHED, 'd', datetime(2026, 10, 1, 15, tzinfo=UTC))  # same instant: not later
    await world.asset_event(kinds.VANISHED, 'e', datetime(2026, 10, 2, 15, tzinfo=UTC))  # no APPEARED at all
    await world.asset_event(kinds.APPEARED, 'h', T0, asset_list=AssetListStatus.INACTIVE)
    await world.asset_event(kinds.APPEARED, 'i', T0, source=NO_CALENDAR_SOURCE)
    return out


def path(seeded: Any, tail: str = '') -> str:
    return f'/ui/v1/panels/{seeded.panel_id}{tail}'


# --------------------------------------------------------------------------------------------- R1 the panel list


async def test_r1_lists_every_panel_by_name_then_id_with_the_latest_versions_enabled_flag(world, client):
    # Names are lower case so the order is the same under SQLite's binary collation and a Postgres locale. Names are
    # unique, so the id tie-break cannot be reached through the table.
    later = await world.panel('beta', panel_id=ordered(1))
    await world.settings_version(later.panel_id, enabled=False, reason='paused')
    await world.panel('gamma', NO_CALENDAR_SOURCE, panel_id=ordered(2))
    enabled_again = await world.panel('alpha', panel_id=ordered(3), enabled=False)
    await world.settings_version(enabled_again.panel_id, enabled=True, reason='resumed')

    body = await get(client, '/ui/v1/panels')

    assert [(item['name'], item.get('enabled', False), item['source']) for item in body['items']] == [
        ('alpha', True, 'DATA_SOURCE_ALPACA_API'),
        ('beta', False, 'DATA_SOURCE_ALPACA_API'),
        ('gamma', True, 'DATA_SOURCE_IB_API'),
    ]
    assert body['items'][1]['id'] == str(ordered(1))


async def test_r1_with_no_panels_is_an_empty_list_and_a_stray_parameter_is_a_422(world, client):
    assert await get(client, '/ui/v1/panels') == {}
    problem(await client.get('/ui/v1/panels', params={'limit': 5}), status=422, reason='INVALID_REQUEST')


async def test_r1_is_one_statement_whatever_the_number_of_panels(world, client):
    await world.panel('one')
    assert await statements_of(world, client, '/ui/v1/panels') == 1
    for n in range(6):
        await world.panel(f'more-{n}')
    assert await statements_of(world, client, '/ui/v1/panels') == 1


# --------------------------------------------------------------------------------------------- R2 the detail


async def test_r2_counts_members_collecting_draining_and_lagging_by_the_ruled_rules(world, client, members):
    body = await get(client, path(members.seeded))

    # members: a, zz-a, b, c (DEFERRED), d (REFUSED), e (DRAINING), h (no dataset), i (stopped); not f, g or 'dry'.
    # collecting: a, b and e only: c and d have collecting datasets but are DEFERRED / REFUSED; h has none; i stopped.
    # lagging: b (LATE) and e, judged on the dataset its DRAINING row names (OVERDUE), not the FRESH one it minted;
    # f's OVERDUE dataset does not count, f has EXITED.
    assert body['counts'] == {'members': '8', 'collecting': '3', 'draining': '1', 'lagging': '2'}


async def test_r2_carries_the_current_versions_and_takes_the_source_versions_pass_from_this_panel_only(world, client):
    seeded = await world.panel('mine')
    other = await world.panel('other', new_source_version=False)
    newest_source = await world.source_version(SOURCE, reason='rates raised')
    v2 = await world.settings_version(seeded.panel_id, reason='narrower', allow_symbols=['ZZZ'])
    mine = await world.open(seeded, kind=PanelPassKind.RELEASE, settings_id=v2, started_at=T0)
    # Another panel ran under the newest source version: that is ITS effective-from, not this panel's.
    await world.open(other, source_settings_id=newest_source, started_at=T0 - DAY)

    body = await get(client, path(seeded))

    assert body['id'] == str(seeded.panel_id) and body['name'] == 'mine' and body['source'] == 'DATA_SOURCE_ALPACA_API'
    current = body['currentVersion']
    assert current['versionNo'] == 2 and current['reason'] == 'narrower'
    assert current['settings']['allowSymbols'] == ['ZZZ'] and 'size' not in current['settings']
    assert current['effectivePass'] == {'passId': str(mine), 'kind': 'PANEL_PASS_KIND_RELEASE', 'startedAt': stamp(T0)}
    source = body['currentSourceVersion']
    assert source['versionNo'] == 2 and source['reason'] == 'rates raised'
    assert 'effectivePass' not in source
    assert body.get('counts', {}) == {}


async def test_r2_and_r3_carry_a_stored_include_otc_true_and_leave_false_absent(world, client):
    """ADR tj-ed4t8m D2: the read must carry include_otc, the field a client re-save would otherwise lose.

    The plain bool makes a dropped true read as false, silently. Off is the proto default, so it is absent from the
    canonical JSON.
    """
    seeded = await world.panel('otc')
    await world.settings_version(seeded.panel_id, include_otc=True, reason='include otc')

    detail = await get(client, path(seeded))
    versions = await get(client, path(seeded, '/settings-versions'))

    assert detail['currentVersion']['versionNo'] == 2
    assert detail['currentVersion']['settings']['includeOtc'] is True
    newest, original = versions['items']
    assert (newest['versionNo'], newest['settings']['includeOtc']) == (2, True)
    assert original['versionNo'] == 1 and 'includeOtc' not in original['settings']


async def test_r2_is_a_404_for_an_unknown_panel_and_a_422_for_a_malformed_id(world, client):
    problem(await client.get(f'/ui/v1/panels/{uuid.uuid4()}'), status=404, reason='NOT_FOUND')
    problem(await client.get('/ui/v1/panels/not-a-uuid'), status=422, reason='INVALID_REQUEST')


async def test_r2_statement_count_does_not_grow_with_the_members(world, client, members):
    small = await world.panel('small', new_source_version=False)
    request = await world.request(await world.dataset('SSS', bars=(ny_midnight(6),)))
    await world.open(small, started_at=T0, rows=(entered('s', T0), created('s', T0, request)))

    assert await statements_of(world, client, path(small)) == await statements_of(world, client, path(members.seeded))


# --------------------------------------------------------------------------------------------- R3 settings versions


async def test_r3_pages_newest_first_with_the_full_settings_and_the_kind_of_the_first_real_pass(world, client):
    seeded = await world.panel('versions')
    v2 = await world.settings_version(
        seeded.panel_id,
        cadence=PanelCadence.WEEKLY,
        allow_symbols=['AAA', 'BBB'],
        deny_symbols=['CCC'],
        asset_class=AssetType.STOCK,
        exchange='NYSE',
        active=True,
        tradable=False,
        reason='filtered',
    )
    await world.settings_version(seeded.panel_id, enabled=False, reason='paused')
    # v1: a dry-run pass first (ignored), then a RELEASE before a MEMBERSHIP: the first real one by started_at wins.
    await world.open(seeded, dry_run=True, started_at=T0 - 2 * DAY)
    release = await world.open(seeded, kind=PanelPassKind.RELEASE, started_at=T0 - DAY)
    await world.open(seeded, started_at=T0)
    membership = await world.open(seeded, settings_id=v2, started_at=T1)

    first = await get(client, path(seeded, '/settings-versions'), limit=2)
    rest = await get(client, path(seeded, '/settings-versions'), limit=2, cursor=first['nextCursor'])

    assert [item['versionNo'] for item in first['items']] == [3, 2]
    assert [item['versionNo'] for item in rest['items']] == [1] and 'nextCursor' not in rest
    paused, filtered = first['items']
    assert 'effectivePass' not in paused and paused['reason'] == 'paused' and paused['actor'] == 'operator'
    assert filtered['settings'] == {
        'enabled': True,
        'cadence': 'PANEL_CADENCE_WEEKLY',
        'allowSymbols': ['AAA', 'BBB'],
        'denySymbols': ['CCC'],
        'assetClass': 'ASSET_TYPE_STOCK',
        'exchange': 'NYSE',
        'active': True,
        'tradable': False,
    }
    assert filtered['effectivePass']['passId'] == str(membership)
    assert filtered['effectivePass']['kind'] == 'PANEL_PASS_KIND_MEMBERSHIP'
    original = rest['items'][0]
    assert original['effectivePass'] == {
        'passId': str(release),
        'kind': 'PANEL_PASS_KIND_RELEASE',
        'startedAt': stamp(T0 - DAY),
    }
    # An absent optional filter is absent on the wire, never false; and a panel has no size (ADR tj-46jkey).
    assert set(original['settings']) == {'enabled', 'cadence'}


async def test_r3_a_full_last_page_has_no_cursor_and_the_default_limit_is_100(world, client):
    seeded = await world.panel('exact')
    await world.settings_version(seeded.panel_id)
    page = await get(client, path(seeded, '/settings-versions'), limit=2)
    assert len(page['items']) == 2 and 'nextCursor' not in page
    for _ in range(100):
        await world.settings_version(seeded.panel_id)
    default = await get(client, path(seeded, '/settings-versions'))
    assert len(default['items']) == 100 and default['nextCursor']
    assert default['items'][0]['versionNo'] == 102


async def test_r3_statement_count_does_not_grow_with_the_versions(world, client):
    few = await world.panel('few')
    many = await world.panel('many', new_source_version=False)
    for n in range(8):
        version = await world.settings_version(many.panel_id)
        await world.open(many, settings_id=version, started_at=T0 + n * DAY)
    assert await statements_of(world, client, path(few, '/settings-versions')) == await statements_of(
        world, client, path(many, '/settings-versions')
    )


async def test_r3_is_a_404_for_an_unknown_panel(world, client):
    problem(await client.get(f'/ui/v1/panels/{uuid.uuid4()}/settings-versions'), status=404, reason='NOT_FOUND')


# --------------------------------------------------------------------------------------------- R4 source versions


async def test_r4_pages_a_sources_versions_with_any_panels_pass_as_the_effective_one(world, client):
    first = await world.panel('first')
    second = await world.panel('second', new_source_version=False)
    newest = await world.source_version(SOURCE, reason='newest')
    other_panels = await world.open(second, source_settings_id=newest, started_at=T1)
    await world.open(first, started_at=T0)

    page = await get(client, '/ui/v1/panel-sources/DATA_SOURCE_ALPACA_API/settings-versions', limit=1)
    rest = await get(client, '/ui/v1/panel-sources/ALPACA_API/settings-versions', cursor=page['nextCursor'])

    (top,) = page['items']
    assert top['versionNo'] == 2 and top['source'] == 'DATA_SOURCE_ALPACA_API' and top['reason'] == 'newest'
    assert top['effectivePass']['passId'] == str(other_panels)
    assert top['settings'] == {
        'rampDatasetsPerHour': 5,
        'requestCapPerHour': 60,
        'corporateActionWindowPreDays': 90,
        'corporateActionWindowPostDays': 90,
    }
    assert [item['versionNo'] for item in rest['items']] == [1] and 'nextCursor' not in rest
    assert rest['items'][0]['effectivePass']['kind'] == 'PANEL_PASS_KIND_MEMBERSHIP'


@pytest.mark.parametrize(
    ('ramp', 'cap'), [(None, 60), (5, None), (None, None)], ids=['ramp-unset', 'cap-unset', 'both-unset']
)
async def test_r4_and_the_detail_leave_an_unset_rate_unset_never_zero(world, client, ramp, cap):
    """ADR tj-2ayia2 D3: a NULL rate is NO LIMIT and has NO KEY in the canonical JSON (explicit presence).

    0 never appears, since 0 is a refused value and would read as a rate.
    """
    seeded = await world.panel('first')
    await world.source_version(SOURCE, ramp_datasets_per_hour=ramp, request_cap_per_hour=cap, reason='no limit')

    (top, _) = (await get(client, '/ui/v1/panel-sources/DATA_SOURCE_ALPACA_API/settings-versions'))['items']
    detail = await get(client, f'/ui/v1/panels/{seeded.panel_id}')

    expected = {'corporateActionWindowPreDays': 90, 'corporateActionWindowPostDays': 90}
    expected |= {} if ramp is None else {'rampDatasetsPerHour': ramp}
    expected |= {} if cap is None else {'requestCapPerHour': cap}
    assert top['versionNo'] == 2 and top['settings'] == expected
    assert detail['currentSourceVersion']['versionNo'] == 2
    assert detail['currentSourceVersion']['settings'] == expected


async def test_the_protobuf_constructor_leaves_an_optional_field_given_none_unset_and_zero_set():
    """The mapping passes the record's None straight to the PanelSourceSettings constructor (flagged by the builder).

    That is correct only because the runtime treats a keyword of None as not given for a proto3 optional scalar; and a
    0, by contrast, is PRESENT and printed. Pinned so a protobuf upgrade that changes either cannot pass silently.
    """
    from google.protobuf import json_format

    from routers.data_store import ui_mapping

    panels_pb2 = ui_mapping.panels_pb2  # the very module the mapping constructs from (generated code is not imported)
    unset = panels_pb2.PanelSourceSettings(ramp_datasets_per_hour=None, request_cap_per_hour=None)
    zero = panels_pb2.PanelSourceSettings(ramp_datasets_per_hour=0, request_cap_per_hour=0)
    assert not unset.HasField('ramp_datasets_per_hour') and not unset.HasField('request_cap_per_hour')
    assert zero.HasField('ramp_datasets_per_hour') and zero.HasField('request_cap_per_hour')
    assert json_format.MessageToDict(unset) == {}
    assert json_format.MessageToDict(zero) == {'rampDatasetsPerHour': 0, 'requestCapPerHour': 0}
    # The read side of the same presence: an absent key parses unset, so a round trip keeps "no limit".
    assert not json_format.Parse('{}', panels_pb2.PanelSourceSettings()).HasField('ramp_datasets_per_hour')


async def test_r4_is_an_empty_page_for_a_source_with_no_version_never_a_404(world, client):
    assert await get(client, '/ui/v1/panel-sources/DATA_SOURCE_IB_API/settings-versions') == {}


@pytest.mark.parametrize('spelling', ['data_source_alpaca_api', 'alpaca_api', 'DATA_SOURCE_ALPACA_API'])
async def test_r4_reads_the_source_by_its_proto_name_in_any_case(world, client, spelling):
    await world.source_version(SOURCE)
    page = await get(client, f'/ui/v1/panel-sources/{spelling}/settings-versions')
    assert len(page['items']) == 1


@pytest.mark.parametrize('spelling', ['DATA_SOURCE_UNSPECIFIED', 'UNSPECIFIED', 'ALPACA', '2', 'DATA_SOURCE_NOPE'])
async def test_r4_refuses_unspecified_a_domain_value_or_an_unknown_name(world, client, spelling):
    response = await client.get(f'/ui/v1/panel-sources/{spelling}/settings-versions')
    problem(response, status=422, reason='INVALID_REQUEST')


# --------------------------------------------------------------------------------------------- R5 boundaries


async def test_r5_serves_every_marker_in_the_half_open_window_oldest_first(world, client, members):
    seeded = members.seeded
    ds = members.datasets
    start, end = datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC)
    # Version 1 saved exactly at start (included), version 3 exactly at end (excluded).
    await world.put(SETTINGS.update().where(SETTINGS.c.id == seeded.settings_id).values(saved_at=start))
    await world.settings_version(seeded.panel_id, reason='v2', saved_at=start + 5 * DAY)
    await world.settings_version(seeded.panel_id, reason='at end', saved_at=end)
    await world.put(
        SOURCE_SETTINGS.update()
        .where(SOURCE_SETTINGS.c.id == seeded.source_settings_id)
        .values(saved_at=start + 2 * DAY)
    )
    await world.override(ds['a'], at=start + 3 * DAY, new_value='false', reason='stop a')
    await world.override(ds['a'], at=start + 4 * DAY, new_value='true', reason='resume a')
    await world.override(ds['b'], at=start + 4 * DAY + timedelta(hours=1), new_value=None, reason='says neither')
    await world.override(ds['b'], at=start + 6 * DAY, new_value='false', action=OverrideAction.SET_EXPIRY)
    await world.override(uuid.uuid4(), at=start + 7 * DAY, new_value='false', reason='not this panel')

    body = await get(client, path(seeded, '/boundaries'), start=iso(start), end=iso(end))

    assert body['start'] == stamp(start) and body['end'] == stamp(end)
    assert [
        (m['kind'].removeprefix('PANEL_MARKER_KIND_'), m.get('versionNo', 0), m['reason'], m.get('label', ''))
        for m in body['markers']
    ] == [
        ('PANEL_SETTINGS', 1, 'settings 1', ''),
        ('SOURCE_SETTINGS', 1, 'rates 1', ''),
        ('COLLECT_STOP', 0, 'stop a', 'AAA 1day'),
        ('COLLECT_RESUME', 0, 'resume a', 'AAA 1day'),
        ('PANEL_SETTINGS', 2, 'v2', ''),
    ]
    stop = body['markers'][2]
    assert stop['datasetId'] == str(ds['a']) and stop['actor'] == 'operator' and stop['at'] == stamp(start + 3 * DAY)
    assert 'datasetId' not in body['markers'][0]


async def test_a_version_with_no_reason_reads_as_none_on_every_route_that_serves_it(world, client):
    """A NULL reason (stored when none was given, ADR tj-46jkey) reads as '' on the wire: R2, R3, R4 and R5 alike.

    Proto3 canonical JSON omits an empty string, so the field is ABSENT, never null and never a placeholder text; the
    actor is still served. A NULL also passes the reason's not-blank CHECK, which the version rows here go through.
    """
    seeded = await world.panel('reasonless')
    source_version = await world.source_version(SOURCE, reason=None, saved_at=T0)
    version = await world.settings_version(seeded.panel_id, reason=None, saved_at=T0 + timedelta(hours=1))
    for table, row_id in ((SETTINGS, version), (SOURCE_SETTINGS, source_version)):
        (stored,) = await world.rows(table, table.c.id == row_id)
        assert stored.reason is None

    current = (await get(client, path(seeded)))['currentVersion']
    assert current['versionNo'] == 2 and 'reason' not in current and current['actor'] == 'operator'
    (newest, *_) = (await get(client, path(seeded, '/settings-versions')))['items']
    assert newest['versionNo'] == 2 and 'reason' not in newest
    (top, *_) = (await get(client, '/ui/v1/panel-sources/DATA_SOURCE_ALPACA_API/settings-versions'))['items']
    assert top['versionNo'] == 2 and 'reason' not in top and top['actor'] == 'operator'
    body = await get(client, path(seeded, '/boundaries'), start=iso(T0 - DAY), end=iso(T0 + DAY))
    reasonless = [m for m in body['markers'] if m.get('versionNo') == 2]
    assert {m['kind'] for m in reasonless} == {'PANEL_MARKER_KIND_PANEL_SETTINGS', 'PANEL_MARKER_KIND_SOURCE_SETTINGS'}
    assert all('reason' not in m and m['actor'] == 'operator' for m in reasonless), reasonless


async def test_r5_labels_a_stop_on_a_deleted_dataset_with_nothing(world, client):
    seeded = await world.panel('gone')
    vanished = uuid.uuid4()
    await world.open(seeded, started_at=T0, rows=(entered('q', T0), moved('q', DRAINING, T0, dataset_id=vanished)))
    await world.override(vanished, at=T0, new_value='false')
    body = await get(client, path(seeded, '/boundaries'), start=iso(T0 - DAY), end=iso(T0 + DAY))
    (stop,) = [m for m in body['markers'] if m['kind'] == 'PANEL_MARKER_KIND_COLLECT_STOP']
    assert stop['datasetId'] == str(vanished) and 'label' not in stop


@pytest.mark.parametrize(
    'params',
    [
        {'start': '2026-10-02T00:00:00Z', 'end': '2026-10-02T00:00:00Z'},
        {'start': '2026-10-03T00:00:00Z', 'end': '2026-10-02T00:00:00Z'},
        {'start': '2026-10-02T00:00:00', 'end': '2026-10-03T00:00:00Z'},
        {'start': '2026-10-02T00:00:00Z'},
        {'end': '2026-10-02T00:00:00Z'},
        {},
    ],
)
async def test_r5_refuses_a_window_that_is_empty_inverted_naive_or_incomplete(world, client, params):
    seeded = await world.panel('window')
    problem(await client.get(path(seeded, '/boundaries'), params=params), status=422, reason='INVALID_REQUEST')


async def test_r5_is_a_404_for_an_unknown_panel(world, client):
    response = await client.get(f'/ui/v1/panels/{uuid.uuid4()}/boundaries', params={'start': iso(T0), 'end': iso(T1)})
    problem(response, status=404, reason='NOT_FOUND')


# --------------------------------------------------------------------------------------------- R6 members


async def test_r6_lists_every_latest_transition_by_symbol_then_vendor_id(world, client, members):
    body = await get(client, path(members.seeded, '/members'))
    assert [
        (m['symbol'], m['vendorAssetId'], m['status'].removeprefix('PANEL_TRANSITION_')) for m in body['items']
    ] == [
        ('AAA', 'a', 'CREATED'),
        ('AAA', 'zz-a', 'ENTERED'),
        ('BBB', 'b', 'CREATED'),
        ('CCC', 'c', 'DEFERRED'),
        ('DDD', 'd', 'REFUSED'),
        ('EEE', 'e', 'DRAINING'),
        ('FFF', 'f', 'EXITED'),
        ('GGG', 'g', 'DRAINED'),
        ('HHH', 'h', 'CREATED'),
        ('III', 'i', 'CREATED'),
    ]
    assert 'nextCursor' not in body


async def test_r6_resolves_each_members_dataset_health_last_bar_and_cause(world, client, members):
    ds = members.datasets
    body = await get(client, path(members.seeded, '/members'))
    by_id = {m['vendorAssetId']: m for m in body['items']}

    def seen(vendor: str) -> tuple:
        member = by_id[vendor]
        return (
            member.get('datasetId', ''),
            member.get('health', '').removeprefix('DATASET_HEALTH_STATUS_'),
            member.get('cause', ''),
        )

    assert seen('a') == (str(ds['a']), 'FRESH', '')
    assert seen('b') == (str(ds['b']), 'LATE', '')
    assert seen('c') == (str(ds['c']), 'FRESH', 'ramp_rate')
    assert seen('d') == (str(ds['d']), 'FRESH', 'own_overlap')
    assert seen('e') == (str(ds['e']), 'OVERDUE', '')  # the DRAINING row's own dataset wins over the minted one
    assert seen('f') == (str(ds['f']), 'OVERDUE', '')
    assert seen('g') == ('', '', '')
    assert seen('h') == ('', '', '')  # its request was never delivered
    assert seen('i') == (str(ds['i']), 'RETIRED', '')
    assert seen('zz-a') == ('', '', '')
    assert by_id['a']['lastBar'] == stamp(ny_midnight(6)) and 'lastBar' not in by_id['h']
    assert by_id['c']['asOf'] == stamp(T1) and by_id['a']['asOf'] == stamp(T0) and by_id['a']['recordedAt']


async def test_r6_appeared_and_vanished_days_follow_the_latest_active_list_events(world, client, members):
    body = await get(client, path(members.seeded, '/members'))
    days = {m['vendorAssetId']: (m.get('appearedOn', ''), m.get('vanishedOn', '')) for m in body['items']}
    assert days == {
        'a': ('2026-10-01', ''),  # the LATEST APPEARED, on its New York day (02:00Z on the 2nd)
        'zz-a': ('', ''),
        'b': ('2026-10-01', '2026-10-03'),  # vanished after it appeared; the STATUS_FLIP is neither
        'c': ('2026-10-02', ''),  # vanished BEFORE it appeared again
        'd': ('2026-10-01', ''),  # vanished at the same instant: not later
        'e': ('', '2026-10-02'),  # vanished with no APPEARED at all (an original member)
        'f': ('', ''),
        'g': ('', ''),
        'h': ('', ''),  # an INACTIVE-list event is not counted
        'i': ('', ''),  # another source's event is not counted
    }


async def test_r6_the_minted_dataset_is_the_newest_real_created_requests(world, client):
    """Found by P24v's mutation pass: both the dry-run filter and the newest-first rank survived every other case.

    k: its real CREATED request was never delivered; a LATER dry-run CREATED names a delivered one: k has no dataset.
    m: created twice for real (an older request, then a newer one): the newer request's dataset is m's.
    """
    seeded = await world.panel('minted')
    older = await world.dataset('MMM', bars=(ny_midnight(1),), start=ny_midnight(2, 9))
    newer = await world.dataset('MMM', bars=(ny_midnight(6),))
    dry_minted = await world.dataset('KKK', bars=(ny_midnight(6),))
    undelivered = await world.request(None)
    first, second = await world.request(older), await world.request(newer)
    await world.open(
        seeded,
        started_at=T0,
        rows=(entered('k', T0, 'KKK'), created('k', T0, undelivered, 'KKK'), entered('m', T0, 'MMM')),
    )
    await world.open(seeded, started_at=T0 + timedelta(hours=1), rows=(created('m', T0, first, 'MMM'),))
    await world.open(seeded, started_at=T1, rows=(created('m', T1, second, 'MMM'),))
    await world.open(
        seeded,
        dry_run=True,
        started_at=T1 + DAY,
        rows=(created('k', T1 + DAY, await world.request(dry_minted), 'KKK'),),
    )

    body = await get(client, path(seeded, '/members'))

    by_id = {m['vendorAssetId']: m for m in body['items']}
    assert 'datasetId' not in by_id['k'] and 'health' not in by_id['k']
    assert by_id['m']['datasetId'] == str(newer) and by_id['m']['health'] == 'DATASET_HEALTH_STATUS_FRESH'
    assert (await get(client, path(seeded)))['counts'] == {'members': '2', 'collecting': '1'}


async def test_r6_a_source_with_no_trading_calendar_has_no_days_and_no_health(world, client):
    seeded = await world.panel('no-calendar', NO_CALENDAR_SOURCE)
    dataset = await world.dataset('NNN', bars=(ny_midnight(6),), source=NO_CALENDAR_SOURCE)
    request = await world.request(dataset, source=NO_CALENDAR_SOURCE)
    await world.open(seeded, started_at=T0, rows=(entered('n', T0), created('n', T0, request)))
    await world.asset_event(AssetListEventKind.APPEARED, 'n', T0, source=NO_CALENDAR_SOURCE)
    await world.asset_event(AssetListEventKind.VANISHED, 'n', T1, source=NO_CALENDAR_SOURCE)

    (member,) = (await get(client, path(seeded, '/members')))['items']

    assert member['datasetId'] == str(dataset) and member['lastBar'] == stamp(ny_midnight(6))
    assert 'appearedOn' not in member and 'vanishedOn' not in member and 'health' not in member
    assert (await get(client, path(seeded)))['counts'] == {'members': '1', 'collecting': '1'}


@pytest.mark.parametrize('limit', [1, 3, 4, 9])
async def test_r6_keyset_pages_serve_every_member_once_in_order(world, client, members, limit):
    whole = [m['vendorAssetId'] for m in (await get(client, path(members.seeded, '/members')))['items']]
    seen: list[str] = []
    cursor = None
    for _ in range(len(whole) + 1):  # bounded: a cursor that does not advance fails here instead of hanging
        params = {'limit': limit} | ({'cursor': cursor} if cursor else {})
        page = await get(client, path(members.seeded, '/members'), **params)
        assert len(page['items']) <= limit
        seen += [m['vendorAssetId'] for m in page['items']]
        cursor = page.get('nextCursor')
        if not cursor:
            break
    else:
        pytest.fail(f'the member cursor never ended: {seen}')
    assert seen == whole


async def test_r6_member_statement_count_does_not_grow_with_the_page(world, client, members):
    one = await statements_of(world, client, path(members.seeded, '/members'), limit=1)
    assert await statements_of(world, client, path(members.seeded, '/members'), limit=10) == one


async def test_r6_an_empty_panel_is_an_empty_page_and_an_unknown_one_a_404(world, client):
    seeded = await world.panel('empty')
    assert await get(client, path(seeded, '/members')) == {}
    problem(await client.get(f'/ui/v1/panels/{uuid.uuid4()}/members'), status=404, reason='NOT_FOUND')


# --------------------------------------------------------------------------------------------- cursors and limits


@pytest.mark.parametrize('limit', [0, -1, 501, 'x'])
@pytest.mark.parametrize('tail', ['/settings-versions', '/members'])
async def test_a_limit_outside_1_to_500_is_a_422(world, client, tail, limit):
    seeded = await world.panel('limits')
    problem(await client.get(path(seeded, tail), params={'limit': limit}), status=422, reason='INVALID_REQUEST')


async def test_a_limit_of_500_is_served(world, client):
    seeded = await world.panel('max')
    await get(client, path(seeded, '/members'), limit=500)
    await get(client, path(seeded, '/settings-versions'), limit=500)
    await get(client, '/ui/v1/panel-sources/ALPACA_API/settings-versions', limit=500)


async def test_a_cursor_is_bound_to_its_route_and_its_panel(world, client, members):
    seeded = members.seeded
    other = await world.panel('other', new_source_version=False)
    for _ in range(2):
        await world.settings_version(seeded.panel_id)
        await world.settings_version(other.panel_id)
    versions = (await get(client, path(seeded, '/settings-versions'), limit=1))['nextCursor']
    member_cursor = (await get(client, path(seeded, '/members'), limit=1))['nextCursor']
    await world.source_version(SOURCE)
    source_cursor = (await get(client, '/ui/v1/panel-sources/ALPACA_API/settings-versions', limit=1))['nextCursor']

    scope = str(seeded.panel_id)
    refused = [
        (path(other, '/settings-versions'), versions),  # another panel
        (path(seeded, '/members'), versions),  # another route
        (path(seeded, '/settings-versions'), member_cursor),
        (path(seeded, '/settings-versions'), source_cursor),
        ('/ui/v1/panel-sources/ALPACA_API/settings-versions', versions),
        ('/ui/v1/panel-sources/IB_API/settings-versions', source_cursor),  # another source
        (path(seeded, '/members'), 'not-a-cursor!'),
        (path(seeded, '/members'), forged({'o': 'panel-members', 's': scope, 'k': ['A']})),  # a short key
        (path(seeded, '/members'), forged({'o': 'panel-members', 's': scope, 'k': ['A', 1]})),
        (path(seeded, '/settings-versions'), forged({'o': 'panel-settings-versions', 's': scope, 'k': ['2']})),
        (path(seeded, '/settings-versions'), forged({'o': 'panel-settings-versions', 's': scope, 'k': [True]})),
        (path(seeded, '/settings-versions'), forged(['panel-settings-versions', scope, [2]])),
    ]
    for route, cursor in refused:
        problem(await client.get(route, params={'cursor': cursor}), status=422, reason='INVALID_REQUEST')
    # Each is accepted on its own route and parent.
    await get(client, path(seeded, '/settings-versions'), cursor=versions)
    await get(client, path(seeded, '/members'), cursor=member_cursor)


async def test_a_cursor_of_another_route_is_refused_even_with_the_same_parent_id_and_key_width(world, client):
    """The route name is checked on its own: a corporate-actions cursor is two strings, like a member cursor.

    A panel and a dataset that share one UUID make the parent check pass, so only the route check refuses it (found by
    P24v's mutation pass: without this case dropping the route check survived every other test).
    """
    shared = uuid.uuid4()
    seeded = await world.panel('twin', panel_id=shared)
    await world.dataset('TWN', dataset_id=shared)
    for n in range(2):
        await world.action('TWN', T0 + n * DAY, vendor_action_id=f't{n}')
    await world.open(seeded, started_at=T0, rows=(entered('t1', T0, 'TTA'), entered('t2', T0, 'TTB')))
    actions_cursor = (await get(client, f'/ui/v1/datasets/{shared}/corporate-actions', limit=1))['nextCursor']
    members_cursor = (await get(client, path(seeded, '/members'), limit=1))['nextCursor']

    response = await client.get(path(seeded, '/members'), params={'cursor': actions_cursor})
    problem(response, status=422, reason='INVALID_REQUEST')
    response = await client.get(f'/ui/v1/datasets/{shared}/corporate-actions', params={'cursor': members_cursor})
    problem(response, status=422, reason='INVALID_REQUEST')


# --------------------------------------------------------------------------------------------- R7 asset-list history


def at(day: int, hour: int = 21, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, tzinfo=UTC)


@pytest_asyncio.fixture
async def history(world: World) -> Any:
    seeded = await world.panel('history')
    other = NO_CALENDAR_SOURCE
    inactive = AssetListStatus.INACTIVE
    kinds = AssetListEventKind
    # Mon 28 Sep: saved; three distinct vendors changed (one twice, one on the inactive list); another source's not.
    await world.snapshot(at(28, month=9))
    await world.asset_event(kinds.APPEARED, 'p', at(28, month=9))
    await world.asset_event(kinds.STATUS_FLIP, 'p', at(28, month=9))
    await world.asset_event(kinds.VANISHED, 'q', at(28, month=9))
    await world.asset_event(kinds.STATUS_FLIP, 'r', at(28, month=9), asset_list=inactive)
    await world.asset_event(kinds.APPEARED, 's', at(28, month=9), source=other)
    # Tue 29 Sep: only an empty snapshot (an event that day still counts 0: the day is not SAVED).
    await world.snapshot(at(29, month=9), row_count=0)
    await world.asset_event(kinds.APPEARED, 't', at(29, month=9))
    # Wed 30 Sep: an empty and a non-empty one: SAVED.
    await world.snapshot(at(30, 15, month=9), row_count=0)
    await world.snapshot(at(30, month=9))
    # Thu 1 Oct: only another source's snapshot. Fri 2 Oct: only the inactive list's. Both MISSING.
    await world.snapshot(at(1), source=other)
    await world.snapshot(at(2), asset_list=inactive)
    # Mon 5 Oct: saved at 02:00Z on the 6th, which is the 5th in New York. Tue 6 Oct: nothing, due at 20:30Z.
    await world.snapshot(datetime(2026, 10, 6, 2, tzinfo=UTC))
    await world.asset_event(kinds.APPEARED, 'u', datetime(2026, 10, 6, 2, tzinfo=UTC))
    return seeded


async def test_r7_reads_each_expected_day_of_the_source_calendar(world, client, history):
    body = await get(
        client, path(history, '/asset-list-history'), start=iso(ny_midnight(28, 9)), end=iso(ny_midnight(8))
    )
    assert [
        (d['day'], d['state'].removeprefix('ASSET_LIST_DAY_STATE_'), d.get('changedCount', 0)) for d in body['days']
    ] == [
        ('2026-09-28', 'SAVED', 3),
        ('2026-09-29', 'EMPTY', 0),
        ('2026-09-30', 'SAVED', 0),
        ('2026-10-01', 'MISSING', 0),
        ('2026-10-02', 'MISSING', 0),
        ('2026-10-05', 'SAVED', 1),
        ('2026-10-06', 'MISSING', 0),
        ('2026-10-07', 'NOT_DUE', 0),
    ]
    assert body['asOf'] == stamp(NOW) and body['start'] == stamp(ny_midnight(28, 9))


async def test_r7_a_day_is_in_the_window_iff_its_local_midnight_is(world, client, history):
    tick = timedelta(microseconds=1)
    body = await get(
        client, path(history, '/asset-list-history'), start=iso(ny_midnight(28, 9) + tick), end=iso(ny_midnight(2))
    )
    assert [d['day'] for d in body['days']] == ['2026-09-29', '2026-09-30', '2026-10-01']
    body = await get(client, path(history, '/asset-list-history'), start=iso(ny_midnight(3)), end=iso(ny_midnight(5)))
    assert body.get('days', []) == []  # a weekend holds no session


async def test_r7_missing_starts_exactly_at_the_close_plus_the_settle_margin(world, history, monkeypatch):
    calendar = calendar_for_source(SOURCE)
    due = calendar.session_close(ny_midnight(6).astimezone(calendar.tz).date()) + timedelta(minutes=30)

    async def state_at(now: datetime) -> str:
        async with world.session() as db:
            view = await panel_views.asset_list_history(db, history.panel_id, ny_midnight(6), ny_midnight(7), now)
        (day,) = view.days
        return day.state.value

    assert await state_at(due) == 'MISSING'
    assert await state_at(due - timedelta(microseconds=1)) == 'NOT_DUE'
    monkeypatch.setenv('DAILY_REFRESH_SETTLE_MINUTES', '90')
    assert await state_at(due) == 'NOT_DUE'
    assert await state_at(due + timedelta(minutes=60)) == 'MISSING'


async def test_r7_serves_366_days_and_refuses_one_microsecond_more(world, client, history):
    start = datetime(2025, 10, 6, tzinfo=UTC)
    body = await get(
        client, path(history, '/asset-list-history'), start=iso(start), end=iso(start + timedelta(days=366))
    )
    assert len(body['days']) > 240
    response = await client.get(
        path(history, '/asset-list-history'),
        params={'start': iso(start), 'end': iso(start + timedelta(days=366, microseconds=1))},
    )
    problem(response, status=422, reason='INVALID_REQUEST')


@pytest.mark.parametrize(
    'params',
    [
        {'start': '2026-10-02T00:00:00Z', 'end': '2026-10-02T00:00:00Z'},
        {'start': '2026-10-03T00:00:00Z', 'end': '2026-10-02T00:00:00Z'},
        {'start': '2026-10-02T00:00:00', 'end': '2026-10-03T00:00:00Z'},
        {'end': '2026-10-02T00:00:00Z'},
        {'start': '1989-12-01T00:00:00Z', 'end': '1990-01-05T00:00:00Z'},  # before the calendar starts
    ],
)
async def test_r7_refuses_a_bad_window(world, client, history, params):
    problem(await client.get(path(history, '/asset-list-history'), params=params), status=422, reason='INVALID_REQUEST')


async def test_r7_is_a_422_for_a_source_with_no_calendar_and_a_404_for_an_unknown_panel(world, client):
    seeded = await world.panel('no-calendar', NO_CALENDAR_SOURCE)
    window = {'start': iso(ny_midnight(1)), 'end': iso(ny_midnight(6))}
    problem(await client.get(path(seeded, '/asset-list-history'), params=window), status=422, reason='INVALID_REQUEST')
    response = await client.get(f'/ui/v1/panels/{uuid.uuid4()}/asset-list-history', params=window)
    problem(response, status=404, reason='NOT_FOUND')


async def test_r7_statement_count_does_not_grow_with_the_days(world, client, history):
    short = await statements_of(
        world, client, path(history, '/asset-list-history'), start=iso(ny_midnight(5)), end=iso(ny_midnight(6))
    )
    long = await statements_of(
        world, client, path(history, '/asset-list-history'), start=iso(ny_midnight(1, 1)), end=iso(ny_midnight(8))
    )
    assert short == long


# --------------------------------------------------------------------------------------------- R8 corporate actions


async def test_r8_serves_every_observation_of_the_datasets_source_and_symbol_newest_first(world, client):
    dataset = await world.dataset('AAA', bars=(ny_midnight(6),))
    early, middle, late = (datetime(2026, 9, d, tzinfo=UTC) for d in (1, 10, 20))
    long_ago = datetime(1995, 3, 1, tzinfo=UTC)
    await world.action('AAA', early, action_id=ordered(10), ex_date=long_ago)  # far outside the dataset: still served
    await world.action('AAA', middle, action_id=ordered(20))
    await world.action('AAA', middle, action_id=ordered(21), vendor_action_id='act-2')
    await world.action('AAA', late, action_id=ordered(30), payload=b'revised')  # act-1 again: a vendor revision
    await world.action('AAA', late, action_id=ordered(31), source=NO_CALENDAR_SOURCE)
    await world.action('AAAB', late, action_id=ordered(32))
    await world.action('aaa', late, action_id=ordered(33))
    await world.action(None, late, action_id=ordered(34))

    body = await get(client, f'/ui/v1/datasets/{dataset}/corporate-actions')

    assert [(item['vendorActionId'], item['asOf']) for item in body['items']] == [
        ('act-1', stamp(late)),
        ('act-2', stamp(middle)),
        ('act-1', stamp(middle)),
        ('act-1', stamp(early)),
    ]
    oldest = body['items'][-1]
    assert oldest['exDate'] == stamp(long_ago) and oldest['actionType'] == 'cash_dividend'
    assert 'recordDate' not in oldest and 'payableDate' not in oldest
    assert 'nextCursor' not in body


async def test_r8_keyset_breaks_an_as_of_tie_by_id_descending_across_pages(world, client):
    dataset = await world.dataset('TIE')
    instant = datetime(2026, 9, 15, tzinfo=UTC)
    for n in (5, 1, 4, 2, 3):
        await world.action('TIE', instant, action_id=ordered(n), vendor_action_id=f'act-{n}')
    await world.action('TIE', instant - DAY, action_id=ordered(9), vendor_action_id='act-older')
    seen: list[str] = []
    cursor = None
    for _ in range(5):  # bounded: three pages are due; a cursor that does not advance fails instead of hanging
        params = {'limit': 2} | ({'cursor': cursor} if cursor else {})
        page = await get(client, f'/ui/v1/datasets/{dataset}/corporate-actions', **params)
        seen += [item['vendorActionId'] for item in page.get('items', [])]
        cursor = page.get('nextCursor')
        if not cursor:
            break
    else:
        pytest.fail(f'the corporate-action cursor never ended: {seen}')
    assert seen == ['act-5', 'act-4', 'act-3', 'act-2', 'act-1', 'act-older']


async def test_r8_errors(world, client):
    dataset = await world.dataset('ERR')
    other = await world.dataset('OTH')
    for n in range(3):
        await world.action('ERR', datetime(2026, 9, 1 + n, tzinfo=UTC), vendor_action_id=f'e{n}')
    cursor = (await get(client, f'/ui/v1/datasets/{dataset}/corporate-actions', limit=1))['nextCursor']

    problem(await client.get(f'/ui/v1/datasets/{uuid.uuid4()}/corporate-actions'), status=404, reason='NOT_FOUND')
    problem(await client.get('/ui/v1/datasets/nope/corporate-actions'), status=422, reason='INVALID_REQUEST')
    bad_time = forged({'o': 'corporate-actions', 's': str(dataset), 'k': ['not a time', str(uuid.uuid4())]})
    bad_id = forged({'o': 'corporate-actions', 's': str(dataset), 'k': [T0.isoformat(), 'not-a-uuid']})
    for target, bad in ((other, cursor), (dataset, bad_time), (dataset, bad_id)):
        response = await client.get(f'/ui/v1/datasets/{target}/corporate-actions', params={'cursor': bad})
        problem(response, status=422, reason='INVALID_REQUEST')
    await get(client, f'/ui/v1/datasets/{dataset}/corporate-actions', cursor=cursor)
    problem(
        await client.get(f'/ui/v1/datasets/{dataset}/corporate-actions', params={'limit': 501}),
        status=422,
        reason='INVALID_REQUEST',
    )


async def test_r8_statement_count_does_not_grow_with_the_observations(world, client):
    dataset = await world.dataset('CNT')
    await world.action('CNT', T0)
    one = await statements_of(world, client, f'/ui/v1/datasets/{dataset}/corporate-actions')
    for n in range(12):
        await world.action('CNT', T0 + n * DAY, vendor_action_id=f'c{n}')
    assert await statements_of(world, client, f'/ui/v1/datasets/{dataset}/corporate-actions') == one


# --------------------------------------------------------------------------------------------- the pure rule and the read module


@pytest.mark.parametrize(
    ('appeared', 'vanished', 'expected'),
    [
        (None, None, ('', '')),
        (T0, None, ('2026-10-02', '')),
        (None, T0, ('', '2026-10-02')),
        (T0, T1, ('2026-10-02', '2026-10-03')),
        (T1, T0, ('2026-10-03', '')),
        (T0, T0, ('2026-10-02', '')),
    ],
)
async def test_member_days_rule(appeared, vanished, expected):
    calendar = calendar_for_source(SOURCE)
    assert panel_views.member_days(MemberAssetEvents(appeared, vanished), calendar) == expected
    assert panel_views.member_days(MemberAssetEvents(appeared, vanished), None) == ('', '')
    assert panel_views.member_days(None, calendar) == ('', '')


async def test_the_read_module_sends_only_selects(world, client, members, history):
    """Drive every read, then judge every statement the request sessions sent: each is a SELECT, none commits."""
    seeded = members.seeded
    world.reset_recording()
    commits = world.commits
    dataset = members.datasets['a']
    await world.action('AAA', T0)
    for route, params in (
        ('/ui/v1/panels', {}),
        (path(seeded), {}),
        (path(seeded, '/settings-versions'), {}),
        ('/ui/v1/panel-sources/ALPACA_API/settings-versions', {}),
        (path(seeded, '/boundaries'), {'start': iso(T0 - 30 * DAY), 'end': iso(NOW)}),
        (path(seeded, '/members'), {}),
        (path(history, '/asset-list-history'), {'start': iso(ny_midnight(28, 9)), 'end': iso(ny_midnight(8))}),
        (f'/ui/v1/datasets/{dataset}/corporate-actions', {}),
    ):
        await get(client, route, **params)
    assert world.statements and all(isinstance(statement, Select) for statement in world.statements)
    assert world.commits == commits


async def test_the_corporate_action_read_never_selects_the_raw_payload(world, client):
    dataset = await world.dataset('RAW')
    await world.action('RAW', T0, payload=b'secret-ish vendor bytes')
    world.reset_recording()
    body = await get(client, f'/ui/v1/datasets/{dataset}/corporate-actions')
    assert 'secret-ish' not in json.dumps(body)
    selected = {column.name for statement in world.statements for column in statement.selected_columns}
    assert 'raw_payload' not in selected and 'identifiers' not in selected and 'payload_hash' not in selected
