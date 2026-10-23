"""What the tool sends, checked against the store's REAL models and the exchange's REAL calendar (validator).

WHY A SUBPROCESS. The models live under server/ (schemas, data.store.app.trading_calendar), which a tools-scoped pytest run
does not put on sys.path, and nothing in this repository changes sys.path (pytest.ini). So this file records what the
tool sends, through its own entry point and a scripted store, and hands it to a child interpreter whose PYTHONPATH
names the server root. The child validates each body and query with the pydantic model the route binds, and counts
the XNYS sessions in each degrade window. A schema change that the tool does not follow turns this red.

The models, by route: POST /store/... binds StoreAssetDatasetBody; GET /store/... binds StoreAssetDatasetQuery
(extra=forbid, so an unknown filter is a 422); GET /internal/asset-data/... binds StockDataMarketActivityQuery
(extra=forbid).
"""

import json
import os
import re
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from tools.dev_datasets import request as tool
from tools.dev_datasets.tests.doubles import (
    NO_RESET,
    TEST_CONFIG,
    TESTMODE_SYMBOLS,
    FakeStore,
    rendered_bodies,
    run_tool,
)


pytestmark = pytest.mark.build_infra

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVER_ROOT = REPO_ROOT / 'server'

# Days D chosen to land the ranges and windows on awkward dates: a leap day, both DST changes, a year end, a
# holiday Monday, the fixture's own date. All on or before the fixture clock, since a range must end in the past.
DAYS = ['2026-10-20', '2024-03-01', '2024-02-29', '2026-03-09', '2025-11-03', '2026-01-01', '2026-01-20', '2025-07-07']

CHILD = r"""
import json, sys
from datetime import datetime
from pydantic import ValidationError
from schemas.data_store.asset_dataset_store import StoreAssetDatasetBody, StoreAssetDatasetQuery
from schemas.data_store.stock.market_activity_data import StockDataMarketActivityQuery

job = json.load(sys.stdin)
errors = []
def check(model, payload, where):
    try:
        model.model_validate(payload)
    except ValidationError as exc:
        errors.append(f'{where}: {exc.errors(include_url=False)}')
for where, body in job['bodies']:
    check(StoreAssetDatasetBody, body, where)
    if 'feed' in body:
        errors.append(f'{where}: the body names a feed')
    unknown = set(body) - set(StoreAssetDatasetBody.model_fields)
    if unknown:
        errors.append(f'{where}: fields the model does not declare: {sorted(unknown)}')
for where, query in job['lookups']:
    check(StoreAssetDatasetQuery, query, where)
for where, query in job['sdk']:
    check(StockDataMarketActivityQuery, query, where)
sessions = []
if job['windows']:
    from common.enums.data_stock import DataSource
    from data.store.app.trading_calendar import calendar_for_source
    cal = calendar_for_source(DataSource.ALPACA_API)
    for row_id, day, first, last in job['windows']:
        count = len(cal.sessions_overlapping(datetime.fromisoformat(first), datetime.fromisoformat(last)))
        sessions.append([row_id, day, count])
labels = {}
if job.get('labels'):
    from sqlalchemy import Enum
    from common.enums.data_stock import Granularity
    stored = Enum(Granularity).enums
    for wire in job['labels']:
        name = Granularity(wire).name
        labels[wire] = name if name in stored else None
print(json.dumps({'errors': errors, 'sessions': sessions, 'labels': labels}))
"""


def child(job: dict) -> dict:
    assert (SERVER_ROOT / 'schemas' / '__init__.py').is_file(), f'no server root at {SERVER_ROOT}'
    env = {
        **os.environ,
        'PYTHONPATH': os.pathsep.join([str(SERVER_ROOT), str(REPO_ROOT), str(REPO_ROOT / 'gen' / 'proto' / 'python')]),
    }
    done = subprocess.run(  # nosec B603 -- this interpreter, a fixed script, no shell
        [sys.executable, '-c', CHILD],
        input=json.dumps(job),
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=300,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-4000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def recorded(day: str, label: str = 'dev') -> dict:
    """Run seed (--no-reset: the reset sends no HTTP) and reads on day D against a scripted store; collect what was sent.

    label 'test' answers the rail as a TEST stack, so the rows go out under their synthetic symbols.
    """
    bodies = rendered_bodies(day)
    extra = {'config': TEST_CONFIG, 'symbols': dict(TESTMODE_SYMBOLS)} if label == 'test' else {}
    seed = FakeStore(bodies=bodies, **extra)
    if label == 'test':
        # The fake broker's FAILONCE_ row answers 503 on its first POST, as the store does for VENDOR_UNAVAILABLE.
        seed.post_answers = {'r13': tool.Response(503, {'reason': 'VENDOR_UNAVAILABLE', 'error_id': 'e'})}
    assert run_tool(['seed', NO_RESET, '--date', day], http=seed).status == 0
    reads = FakeStore(bodies=bodies, present=set(bodies), **extra)
    assert run_tool(['reads', '--date', day], http=reads).status == 0
    lookups = [c for c in seed.calls + reads.calls if c.method == 'GET' and c.route.startswith(tool.STORE_PATH)]
    return {
        'bodies': [[f'{day} POST {c.route}', c.body] for c in seed.posts()],
        'lookups': [[f'{day} GET {c.path}', c.query] for c in lookups],
        'sdk': [[f'{day} GET {c.path}', c.query] for c in reads.calls if c.route == tool.ASSET_DATA_PATH],
        'windows': [],
    }


def test_every_body_query_and_sdk_read_validates_against_the_stores_own_models():
    job = {'bodies': [], 'lookups': [], 'sdk': [], 'windows': []}
    for day in DAYS:
        for label in ('dev', 'test'):
            for key, value in recorded(day, label).items():
                job[key].extend(value)
    assert len(job['bodies']) == 2 * 14 * len(DAYS)
    assert len(job['sdk']) == 2 * len(DAYS)
    assert {query['asset_symbol'] for _, query in job['sdk']} == {'QQQ', 'ZZDSQQQ'}
    assert child(job)['errors'] == []


def test_the_model_check_is_live_a_daily_row_with_an_end_is_refused_by_the_real_schema():
    # The control: proves the child is validating, not passing everything.
    body = dict(rendered_bodies()['r7'], end='2026-10-19T00:00:00Z')
    bulk = dict(rendered_bodies()['r8'], expiry_type='BULK')
    naive = dict(rendered_bodies()['r1'], start='2026-10-12T00:00:00')
    job = {'bodies': [['end', body], ['bulk', bulk], ['naive', naive]], 'lookups': [], 'sdk': [], 'windows': []}
    errors = child(job)['errors']
    assert [e.split(':')[0] for e in errors] == ['end', 'bulk', 'naive']


def test_r8s_collect_false_body_validates_and_collect_on_a_static_body_is_refused_by_the_real_schema():
    # Decision tj-hhy3dk (tj-grna9p.178): the store's StoreAssetDatasetBody declares collect and refuses it on
    # STATIC. r8's rendered body (collect false, no expiry) must pass; r1's body with collect of either value must
    # not, and neither must r8's with a non-boolean collect. The two passing controls prove the refusals are about
    # collect and not about the rest of the body.
    bodies = rendered_bodies()
    assert bodies['r8']['collect'] is False and bodies['r8']['expiry'] is None
    job = {
        'bodies': [
            ['r8', bodies['r8']],
            ['r1', bodies['r1']],
            ['static-true', dict(bodies['r1'], collect=True)],
            ['static-false', dict(bodies['r1'], collect=False)],
            ['r8-list', dict(bodies['r8'], collect=[False])],
        ],
        'lookups': [],
        'sdk': [],
        'windows': [],
    }
    errors = child(job)['errors']
    assert [e.split(':')[0] for e in errors] == ['static-true', 'static-false', 'r8-list']
    assert all('subscriptions only' in e for e in errors[:2]), errors


def test_r8s_test_mode_body_carries_collect_false_and_validates():
    test = recorded('2026-10-20', 'test')
    r8 = [body for where, body in test['bodies'] if where.endswith('/ZZDSQQQ') and body['granularity'] == '30min']
    assert len(r8) == 1
    assert r8[0]['collect'] is False
    with_collect = [where for where, body in test['bodies'] if 'collect' in body]
    assert len(with_collect) == 1
    assert child({'bodies': [['r8-test', r8[0]]], 'lookups': [], 'sdk': [], 'windows': []})['errors'] == []


def test_the_routes_the_tool_calls_are_the_routes_the_store_declares():
    endpoints = (SERVER_ROOT / 'routers' / 'data_store' / 'app_endpoints.py').read_text(encoding='utf-8')
    assert "POST_STORE_ASSET_DATASET = '/store/{asset_type}/{data_type}/{asset_symbol}'" in endpoints
    assert "GET_STORE_ASSET_DATASET = '/store/{asset_type}/{data_type}/{asset_symbol}'" in endpoints
    assert "GET_ASSET_DATA = '/internal/asset-data/{asset_type}/{data_type}'" in endpoints
    assert "GET_UI_DATASET_BARS = '/ui/v1/datasets/{dataset_id}/bars'" in endpoints
    assert "GET_UI_CONFIG = '/ui/v1/config'" in endpoints
    assert tool.STORE_PATH == '/store/stock/market-activity'
    assert tool.ASSET_DATA_PATH == '/internal/asset-data/stock/market-activity'
    header = (SERVER_ROOT / 'routers' / 'common' / 'instance_secret.py').read_text(encoding='utf-8')
    assert re.search(r"['\"]X-Instance-Secret['\"]", header)


def test_the_database_labels_are_what_the_granularity_column_stores():
    # The column is sqlalchemy Enum(Granularity) (models/store_dataset_entry.py), which stores member NAMES.
    model = (SERVER_ROOT / 'data' / 'store' / 'app' / 'database' / 'models' / 'store_dataset_entry.py').read_text(
        encoding='utf-8'
    )
    assert re.search(r'granularity = Column\(Enum\(Granularity\), nullable=False\)', model)
    job = {'bodies': [], 'lookups': [], 'sdk': [], 'windows': [], 'labels': tool.GRANULARITY_DB_LABELS}
    assert child(job)['labels'] == tool.GRANULARITY_DB_LABELS


# ---- the degrade windows against the exchange calendar ----------------------------------------------------------


def window_sessions() -> list[list]:
    # The rows degrade deletes from (r12, r13); r14 has no window (tj-grna9p.148 (5)).
    rows = [row for row in tool.load_plan() if row.degrade is not None and row.degrade.drops_bars]
    assert [row.id for row in rows] == ['r12', 'r13']
    # Two control windows, independent of the plan: Fri 18 .. Tue 22 Sep 2026 (a weekend inside: Fri and Mon), and
    # Fri 3 .. Tue 7 Jul 2026 (Independence Day observed on Fri 3 Jul, then a weekend: Mon only).
    windows = [
        [
            'control',
            'weekend',
            tool.market_midnight(date(2026, 9, 18)).isoformat(),
            tool.market_midnight(date(2026, 9, 22)).isoformat(),
        ],
        [
            'control',
            'holiday',
            tool.market_midnight(date(2026, 7, 3)).isoformat(),
            tool.market_midnight(date(2026, 7, 7)).isoformat(),
        ],
    ]
    day = date(2017, 1, 2)
    while day <= date(2026, 12, 31):
        for row in rows:
            first, last = tool.degrade_window(row.degrade, day)
            windows.append([row.id, day.isoformat(), first.isoformat(), last.isoformat()])
        day += timedelta(days=1)
    return child({'bodies': [], 'lookups': [], 'sdk': [], 'windows': windows})['sessions']


@pytest.fixture(scope='module')
def sessions() -> list[list]:
    return window_sessions()


def test_every_degrade_window_holds_at_least_two_sessions_on_any_run_date(sessions):
    # Weekends and holidays: a window of calendar days must still remove whole sessions for any D, or the row reads
    # GAPS on some days and not on others (one missing session) or not at all.
    short = [(row_id, day, count) for row_id, day, count in sessions if row_id != 'control' and count < 2]
    assert short == [], f'{len(short)} run dates leave fewer than two sessions, e.g. {short[:4]}'


def test_r12_drops_three_sessions_on_any_run_date_as_the_design_says(sessions):
    # tj-grna9p.143 (4): r12 'drop 3 sessions around D-30'.
    short = [(day, count) for row_id, day, count in sessions if row_id == 'r12' and count < 3]
    assert short == [], f'{len(short)} run dates leave r12 fewer than three sessions, e.g. {short[:4]}'


def test_the_session_count_is_live(sessions):
    # The control windows: the count sees weekends and holidays.
    assert ['control', 'weekend', 2] in sessions
    assert ['control', 'holiday', 1] in sessions
