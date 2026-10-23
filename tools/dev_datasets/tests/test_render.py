"""The rendered POST bodies, exact per row, and the headers each call carries (validator, tj-grna9p.145).

tj-grna9p.148: the vendor-depth ranges (r5, r6, r7, r10, r11) and r14; seed passes --no-reset here, because these
pin the plan's POSTs and the reset has its own file (test_reset.py). tj-grna9p.150: r6 starts 2020-08-01.
"""

from datetime import date

import pytest

from tools.dev_datasets import request as tool
from tools.dev_datasets.tests.doubles import (
    NO_RESET,
    NOW,
    SECRET,
    ExplodingHttp,
    exploding_connect,
    run_tool,
    store_for,
)


pytestmark = pytest.mark.build_infra


def z(day: str | None) -> str | None:
    return f'{day}T00:00:00Z' if day else None


# D = 2026-10-20, worked by hand: (owner, granularity, start, end, update_type, expiry_type, expiry).
EXPECTED = {
    'r1': ('dev-datasets', '1min', '2026-10-12', '2026-10-15', 'STATIC', 'BULK', '2026-10-22'),
    'r2': ('dev-datasets', '5min', '2026-09-10', '2026-10-10', 'STATIC', 'BULK', None),
    'r3': ('dev-datasets', '30min', '2026-07-12', '2026-10-10', 'STATIC', 'BULK', '2026-11-19'),
    'r4': ('dev-datasets', '1hour', '2025-10-15', '2026-10-10', 'STATIC', 'BULK', None),
    'r5': ('dev-datasets', '1week', '2020-08-03', '2026-10-19', 'STATIC', 'BULK', None),
    'r6': ('dev-datasets', '1month', '2020-08-01', '2026-10-19', 'STATIC', 'BULK', '2027-10-20'),
    'r7': ('operator', '1day', '2020-08-03', None, 'DAILY', 'ROLLING', None),
    'r8': ('operator', '30min', '2026-09-20', None, 'DAILY', 'ROLLING', None),
    'r9': ('dev-datasets', '1hour', '2026-09-26', '2026-10-10', 'STATIC', 'BULK', '2026-10-19'),
    'r10': ('dev-datasets', '1day', '2021-01-04', '2023-01-03', 'STATIC', 'BULK', None),
    'r11': ('dev-datasets', '1week', '2020-08-03', '2026-10-19', 'STATIC', 'BUFFER_1K', None),
    'r12': ('dev-degraded', '30min', '2026-08-21', '2026-10-15', 'STATIC', 'BULK', None),
    'r13': ('dev-degraded', '5min', '2026-09-30', '2026-10-15', 'STATIC', 'BULK', None),
    'r14': ('dev-degraded', '1week', '2016-01-04', '2021-01-04', 'STATIC', 'BULK', None),
}
# Decision tj-hhy3dk (tj-grna9p.178): the collect intent is sent only when the plan sets it, and only r8 sets it.
# Every other body has exactly the eight fields it had before collect existed.
COLLECT = {'r8': False}
BASE_FIELDS = {'owner', 'source', 'granularity', 'start', 'end', 'update_type', 'expiry_type', 'expiry'}


def expected_body(row_id: str) -> dict:
    owner, granularity, start, end, update_type, expiry_type, expiry = EXPECTED[row_id]
    body = {
        'owner': owner,
        'source': 'ALPACA',
        'granularity': granularity,
        'start': z(start),
        'end': z(end),
        'update_type': update_type,
        'expiry_type': expiry_type,
        'expiry': z(expiry),
    }
    if row_id in COLLECT:
        body['collect'] = COLLECT[row_id]
    return body


@pytest.mark.parametrize('row_id', list(EXPECTED))
def test_the_rendered_body_is_exact(row_id):
    row = next(row for row in tool.load_plan() if row.id == row_id)
    assert tool.render_body(row, date(2026, 10, 20), NOW) == expected_body(row_id)


def test_no_body_carries_a_feed_a_secret_or_a_request_only_field():
    # The one request field allowed beyond the eight is collect, and only on the rows the plan sets it on (r8).
    for row in tool.load_plan():
        body = tool.render_body(row, date(2026, 10, 20), NOW)
        allowed = BASE_FIELDS | {'collect'} if row.id in COLLECT else BASE_FIELDS
        assert set(body) == allowed, row.id


def test_collect_is_omitted_when_unset_and_present_and_false_on_r8():
    bodies = {row.id: tool.render_body(row, date(2026, 10, 20), NOW) for row in tool.load_plan()}
    assert {row_id: body['collect'] for row_id, body in bodies.items() if 'collect' in body} == COLLECT
    assert bodies['r8']['collect'] is False
    assert bodies['r8']['expiry'] is None
    # The body's key order is pinned too: collect is appended last, after the eight fields.
    assert list(bodies['r8']) == [*expected_body('r7'), 'collect']


def test_no_static_row_ever_renders_collect():
    # Mutation-friendly and independent of the EXPECTED table: every STATIC row of the shipped plan, on both
    # backends, renders no collect key at all (the store answers 422 to collect on STATIC).
    static = [row for row in tool.load_plan() if row.update_type == 'STATIC']
    assert len(static) == 12
    for row in static:
        for label in ('dev', 'test'):
            assert 'collect' not in tool.render_body(row.on_backend(label), date(2026, 10, 20), NOW), (row.id, label)


def test_the_test_mapping_of_r8_renders_the_same_collect_false():
    r8 = next(row for row in tool.load_plan() if row.id == 'r8')
    body = tool.render_body(r8.on_backend('test'), date(2026, 10, 20), NOW)
    assert body == expected_body('r8')
    assert body['collect'] is False


def test_seed_posts_each_rendered_body_to_its_symbol_with_the_secret_header_only():
    store = store_for()
    outcome = run_tool(['seed', NO_RESET], http=store)
    assert outcome.status == 0
    posts = store.posts()
    plan = {row.id: row for row in tool.load_plan()}
    assert [p.body for p in posts] == [expected_body(row_id) for row_id in EXPECTED]
    for post, row_id in zip(posts, EXPECTED, strict=True):
        assert post.path == f'/store/stock/market-activity/{plan[row_id].symbol}'
        assert post.headers == {'X-Instance-Secret': SECRET}
        assert post.timeout == tool.SEED_TIMEOUT_SECONDS


def test_no_get_carries_the_secret_header():
    store = store_for(present={'r4', 'r7', 'r11'})
    assert run_tool(['seed', NO_RESET], http=store).status == 0
    assert run_tool(['reads'], http=store).status == 0
    gets = [c for c in store.calls if c.method == 'GET']
    assert gets
    for call in gets:
        assert 'X-Instance-Secret' not in call.headers
        assert SECRET not in call.path


def test_dry_run_prints_every_body_without_the_secret():
    # --no-reset: no reset, so the dry run needs neither the rail nor the database (test_reset.py has the other).
    outcome = run_tool(['seed', '--dry-run', NO_RESET], http=ExplodingHttp(), connect=exploding_connect)
    assert outcome.status == 0
    import json

    printed = [json.loads(line) for line in outcome.lines if line.startswith('{')]
    assert printed == [expected_body(row_id) for row_id in EXPECTED]
    assert any('X-Instance-Secret: <from INSTANCE_WRITE_SECRET>' in line for line in outcome.lines)
