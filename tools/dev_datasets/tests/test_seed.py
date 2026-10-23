"""seed and repair: the lookup first, idempotence, and when a run continues or stops (validator, tj-grna9p.145).

AN EXACT REPEAT IS AN UPSERT (store_dataset_entry.py: a repeat sets expiry from its body), so a seed that re-POSTed a
row already there would rewrite its expiry. The rule pinned here: look the row up first and skip it when present,
whatever its stored expiry is. A 409 OWN_OVERLAP_CONFLICT means the owner already holds an overlapping range, which
for the generator's own owners is 'already there'; every other refusal stops the run.

tj-grna9p.148: seed now resets first unless --no-reset (test_reset.py pins the reset). These tests are about the
plan's POST loop, so they pass --no-reset; the plan has fourteen rows (r14 added).
"""

import pytest

from tools.dev_datasets import request as tool
from tools.dev_datasets.tests.doubles import NO_RESET, dataset_id, entry_for, run_tool, store_for


pytestmark = pytest.mark.build_infra

ALL = [f'r{n}' for n in range(1, 15)]


def posted_rows(store) -> list[str]:
    return [row_id for c in store.posts() for row_id, body in store.bodies.items() if body == c.body]


def test_every_post_is_preceded_by_its_own_lookup():
    store = store_for()
    assert run_tool(['seed', NO_RESET], http=store).status == 0
    calls = [c for c in store.calls if c.route != '/ui/v1/config']
    assert len(calls) == 2 * len(ALL)
    plan = {row.id: row for row in tool.load_plan()}
    for index, row_id in enumerate(ALL):
        lookup, post = calls[2 * index], calls[2 * index + 1]
        row = plan[row_id]
        assert (lookup.method, lookup.route) == ('GET', f'/store/stock/market-activity/{row.symbol}')
        assert lookup.query == {
            'owner': row.owner,
            'source': 'ALPACA',
            'granularity': row.granularity,
            'update_type': row.update_type,
            'expiry_type': row.expiry_type,
        }
        assert (post.method, post.body) == ('POST', store.bodies[row_id])


def test_a_present_row_is_skipped_and_never_reposted():
    store = store_for(present={'r1', 'r7', 'r12'})
    outcome = run_tool(['seed', NO_RESET], http=store)
    assert outcome.status == 0
    assert posted_rows(store) == [r for r in ALL if r not in {'r1', 'r7', 'r12'}]
    assert 'r1 present (not re-requested)' in outcome.lines
    assert 'r7 present (not re-requested)' in outcome.lines


def test_a_rerun_against_a_full_store_posts_nothing():
    store = store_for(present=set(ALL))
    outcome = run_tool(['seed', NO_RESET], http=store)
    assert outcome.status == 0
    assert store.posts() == []


def test_an_existing_expiry_is_never_rewritten_whatever_it_is():
    # The operator moved r1's expiry and cleared r3's in the UI. The rerun must leave both alone.
    store = store_for()
    store.lookup_extra = {
        'r1': [entry_for(store.bodies['r1'], 'r1', expiry='2030-01-01T00:00:00+00:00')],
        'r3': [entry_for(store.bodies['r3'], 'r3', expiry=None)],
        'r8': [entry_for(store.bodies['r8'], 'r8', expiry=None)],
    }
    assert run_tool(['seed', NO_RESET], http=store).status == 0
    assert not {'r1', 'r3', 'r8'} & set(posted_rows(store))


def test_the_lookup_matches_the_range_as_instants_not_strings():
    store = store_for()
    body = store.bodies['r2']
    # The store answers in UTC with an offset, or with another offset for the same instant.
    store.lookup_extra = {'r2': [entry_for(body, 'r2', start=body['start'].replace('T00:00:00Z', 'T02:00:00+02:00'))]}
    assert run_tool(['seed', NO_RESET, '--row', 'r2'], http=store).status == 0
    assert store.posts() == []


def test_an_entry_with_another_range_is_not_a_match():
    store = store_for()
    body = store.bodies['r2']
    store.lookup_extra = {'r2': [entry_for(body, 'r2', end='2026-10-11T00:00:00+00:00')]}
    assert run_tool(['seed', NO_RESET, '--row', 'r2'], http=store).status == 0
    assert posted_rows(store) == ['r2']


def test_an_open_ended_row_matches_only_an_open_ended_entry():
    store = store_for()
    body = store.bodies['r7']
    store.lookup_extra = {'r7': [entry_for(body, 'r7', end='2026-10-19T00:00:00+00:00')]}
    assert run_tool(['seed', NO_RESET, '--row', 'r7'], http=store).status == 0
    assert posted_rows(store) == ['r7']


def test_a_409_own_overlap_counts_as_already_there_and_the_run_continues():
    store = store_for()
    store.post_answers = {'r3': tool.Response(409, {'reason': 'OWN_OVERLAP_CONFLICT', 'error_id': 'e9'})}
    outcome = run_tool(['seed', NO_RESET], http=store)
    assert outcome.status == 0
    assert posted_rows(store) == ALL
    assert 'r3 409 already there (OWN_OVERLAP_CONFLICT)' in outcome.lines


@pytest.mark.parametrize(
    ('status', 'reason'),
    [
        (409, 'OVERRIDE_REQUIRED'),
        (409, None),
        (400, 'INVALID_REQUEST'),
        (403, 'FORBIDDEN'),
        (422, 'INVALID_REQUEST'),
        (404, 'NOT_FOUND'),
        (401, 'UNAUTHORIZED'),
        (500, 'INTERNAL'),
        (302, None),
    ],
)
def test_any_other_refusal_stops_the_run_at_that_row(status, reason):
    store = store_for()
    store.post_answers = {'r3': tool.Response(status, {'reason': reason, 'error_id': 'e7'} if reason else None)}
    outcome = run_tool(['seed', NO_RESET], http=store)
    assert outcome.status == 1
    assert posted_rows(store) == ['r1', 'r2', 'r3']
    assert f'r3 {status}' in outcome.lines[-1] and 'stopped' in outcome.lines[-1]


@pytest.mark.parametrize('status', [429, 503, 504])
def test_busy_stops_the_run_and_says_wait(status):
    store = store_for()
    store.post_answers = {'r2': tool.Response(status, {'reason': 'RATE_LIMITED', 'error_id': 'e3', 'detail': 'x'})}
    outcome = run_tool(['seed', NO_RESET], http=store)
    assert outcome.status == 1
    assert posted_rows(store) == ['r1', 'r2']
    assert 'wait' in outcome.lines[-1]


def test_a_failed_lookup_stops_before_the_post():
    store = store_for(lookup_status=503)
    outcome = run_tool(['seed', NO_RESET], http=store)
    assert outcome.status == 1
    assert store.posts() == []
    assert 'lookup failed' in outcome.lines[-1]


def test_a_success_line_names_the_row_status_and_data_points_only():
    store = store_for()
    store.post_answers = {'r1': tool.Response(201, {'message': 'm', 'data_points': 1234, 'served_range': {}})}
    outcome = run_tool(['seed', NO_RESET, '--row', 'r1'], http=store)
    assert outcome.lines[-1] == 'r1 201 data_points=1234'


def test_a_refusal_line_carries_reason_and_error_id_but_never_the_detail():
    store = store_for()
    store.post_answers = {
        'r1': tool.Response(422, {'reason': 'INVALID_REQUEST', 'error_id': 'abc', 'detail': 'DETAIL'})
    }
    outcome = run_tool(['seed', NO_RESET], http=store)
    assert 'reason=INVALID_REQUEST error_id=abc' in outcome.lines[-1]
    assert 'DETAIL' not in outcome.text


# ---- repair --------------------------------------------------------------------------------------------------


def test_repair_reposts_only_the_degraded_rows_with_their_identical_seed_bodies():
    store = store_for(present=set(ALL))
    outcome = run_tool(['repair'], http=store)
    assert outcome.status == 0
    assert [c.body for c in store.posts()] == [store.bodies['r12'], store.bodies['r13']]
    assert [c.route for c in store.posts()] == ['/store/stock/market-activity/DIA', '/store/stock/market-activity/QQQ']


def test_repair_leaves_r14_alone_it_has_no_window_and_nothing_to_refetch():
    store = store_for(present=set(ALL))
    assert run_tool(['repair', '--row', 'r14'], http=store).status == 0
    assert store.posts() == []


def test_repair_with_row_limits_to_that_row():
    store = store_for()
    assert run_tool(['--repair', '--row', 'r13'], http=store).status == 0
    assert posted_rows(store) == ['r13']


def test_repair_of_a_row_without_a_degrade_sends_nothing():
    store = store_for()
    assert run_tool(['repair', '--row', 'r4'], http=store).status == 0
    assert store.posts() == []


@pytest.mark.parametrize('status', [503, 409, 422])
def test_a_failed_repair_stops(status):
    store = store_for()
    store.post_answers = {'r12': tool.Response(status, {'reason': 'X', 'error_id': 'e'})}
    outcome = run_tool(['repair'], http=store)
    assert outcome.status == 1
    assert posted_rows(store) == ['r12']


def test_reads_resolve_each_dataset_id_through_the_lookup():
    store = store_for(present=set(ALL))
    outcome = run_tool(['reads'], http=store)
    assert outcome.status == 0
    reads = [c for c in store.calls if c.route.startswith('/ui/v1/datasets/') or c.route == tool.ASSET_DATA_PATH]
    assert [c.path for c in reads] == [
        f'/ui/v1/datasets/{dataset_id("r4")}/bars?limit=100',
        f'/ui/v1/datasets/{dataset_id("r7")}/bars?limit=100',
        tool.ASSET_DATA_PATH
        + f'?dataset_id={dataset_id("r11")}&asset_symbol=QQQ&granularity=1week'
        + '&start=2020-08-03T00%3A00%3A00Z&end=2026-10-19T00%3A00%3A00Z',
    ]
    assert [c.headers for c in reads] == [
        {'X-Client-Id': 'operator'},
        {'X-Client-Id': 'operator'},
        {'X-Client-Id': 'dev-datasets-sdk'},
    ]
    assert 'cursor' not in ''.join(c.path for c in reads)
    assert outcome.lines[1:] == ['r4 ui 200 bars=3', 'r7 ui 200 bars=3', 'r11 sdk 200 bars=2']


def test_reads_of_a_row_not_seeded_yet_say_so_and_read_nothing():
    store = store_for(present={'r7'})
    outcome = run_tool(['reads'], http=store)
    assert outcome.status == 0
    assert 'r4 missing (seed it first)' in outcome.lines
    assert 'r11 missing (seed it first)' in outcome.lines
    reads = [c for c in store.calls if c.route.startswith('/ui/v1/datasets/') or c.route == tool.ASSET_DATA_PATH]
    assert [c.path for c in reads] == [f'/ui/v1/datasets/{dataset_id("r7")}/bars?limit=100']
    assert store.posts() == []
