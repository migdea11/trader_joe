"""Two backends, one plan, chosen by the rail's label (validator, tj-grna9p.148 (4)).

dev: real Alpaca bars under SPY, QQQ and DIA. test: the fake broker (tests/fakes/market_data.py) under synthetic
symbols, ZZDS + the dev symbol, so a public-repo seed never shows fake prices under a real ticker; r13 is
FAILONCE_ZZDSQQQ (its first fetch fails, a FAILED run in history; the repeat succeeds) and r14 GAPS_ZZDSDIA.

On test: seed reports r13's first 503 as 'failed once as designed' and carries on; degrade skips r13 (no bars yet);
repair --row r13 repeats the request and succeeds. The symbols below are written out (doubles.TESTMODE_SYMBOLS), not
computed by the tool.
"""

from datetime import date

import pytest

from tools.dev_datasets import request as tool
from tools.dev_datasets.tests.doubles import NO_RESET, TESTMODE_SYMBOLS, FakeDb, fake_broker_store, run_tool, store_for


pytestmark = pytest.mark.build_infra

REAL_TICKERS = {'SPY', 'QQQ', 'DIA'}
ALL = [f'r{n}' for n in range(1, 15)]
VENDOR_UNAVAILABLE = tool.Response(503, {'reason': 'VENDOR_UNAVAILABLE', 'error_id': 'e503'})
FAILED_ONCE_LINE = 'r13 503 reason=VENDOR_UNAVAILABLE error_id=e503 failed once as designed (FAILONCE_): run repair --row r13 to repeat it'


def route_symbols(store) -> list[str]:
    return [call.route.rsplit('/', 1)[-1] for call in store.calls if call.route.startswith(tool.STORE_PATH + '/')]


# ---- the mapping ----------------------------------------------------------------------------------------------


def test_on_test_every_row_maps_to_its_synthetic_symbol():
    rows = tool.load_plan()
    assert {row.id: row.on_backend('test').symbol for row in rows} == TESTMODE_SYMBOLS


def test_on_dev_every_row_keeps_its_real_symbol_and_is_otherwise_unchanged():
    for row in tool.load_plan():
        assert row.on_backend('dev') == row


def test_on_test_only_the_symbol_changes():
    for row in tool.load_plan():
        mapped = row.on_backend('test')
        assert {**mapped.__dict__, 'symbol': row.symbol} == row.__dict__


def test_no_test_symbol_is_or_ends_as_a_bare_real_ticker():
    for row_id, symbol in TESTMODE_SYMBOLS.items():
        assert symbol not in REAL_TICKERS, row_id
        assert 'ZZDS' in symbol and symbol.endswith(tuple(REAL_TICKERS)), row_id
        prefix = symbol[: symbol.index('ZZDS')]
        assert prefix in ('', 'EMPTY_', 'GAPS_', 'FAILONCE_'), row_id


def test_only_a_failonce_symbol_fails_first():
    rows = {row.id: row for row in tool.load_plan()}
    assert [row_id for row_id, row in rows.items() if row.on_backend('test').fails_first] == ['r13']
    assert not any(row.fails_first for row in rows.values())


# ---- seed on each backend -------------------------------------------------------------------------------------


def test_seed_on_dev_posts_under_the_real_symbols_only():
    store = store_for()
    assert run_tool(['seed', NO_RESET], http=store).status == 0
    assert set(route_symbols(store)) == REAL_TICKERS


def test_seed_on_test_never_sends_a_real_ticker():
    store = fake_broker_store()
    store.post_answers = {'r13': VENDOR_UNAVAILABLE}
    outcome = run_tool(['seed', NO_RESET], http=store)
    assert outcome.status == 0, outcome.text
    assert set(route_symbols(store)) & REAL_TICKERS == set()
    assert [call.route.rsplit('/', 1)[-1] for call in store.posts()] == [TESTMODE_SYMBOLS[r] for r in ALL]
    for call in store.calls:
        for ticker in REAL_TICKERS:
            assert f'/{ticker}' not in call.path and f'={ticker}&' not in call.path + '&', call.path


def test_the_test_bodies_are_the_dev_bodies():
    dev, test = store_for(), fake_broker_store()
    assert run_tool(['seed', NO_RESET], http=dev).status == 0
    assert run_tool(['seed', NO_RESET], http=test).status == 0
    assert [call.body for call in test.posts()] == [call.body for call in dev.posts()]


def test_on_test_r13s_first_503_is_reported_as_designed_and_the_run_carries_on():
    store = fake_broker_store()
    store.post_answers = {'r13': VENDOR_UNAVAILABLE}
    outcome = run_tool(['seed', NO_RESET], http=store)
    assert outcome.status == 0, outcome.text
    assert FAILED_ONCE_LINE in outcome.lines
    # r14 still goes out after it.
    assert route_symbols(store)[-2:] == ['GAPS_ZZDSDIA', 'GAPS_ZZDSDIA']
    assert outcome.lines[-1] == 'r14 200 data_points=42'


@pytest.mark.parametrize('status', [429, 504, 500, 409])
def test_on_test_any_other_failure_of_r13_still_stops(status):
    store = fake_broker_store()
    store.post_answers = {'r13': tool.Response(status, {'reason': 'X', 'error_id': 'e'})}
    outcome = run_tool(['seed', NO_RESET], http=store)
    assert outcome.status == 1
    assert 'failed once as designed' not in outcome.text


@pytest.mark.parametrize(
    'body',
    [
        {'reason': 'DATABASE_UNAVAILABLE', 'error_id': 'e503'},
        {'reason': 'PEER_UNAVAILABLE', 'error_id': 'e503'},
        {'reason': 'vendor_unavailable', 'error_id': 'e503'},
        {'reason': 'VENDOR_UNAVAILABLE ', 'error_id': 'e503'},
        {'reason': ['VENDOR_UNAVAILABLE'], 'error_id': 'e503'},
        {'error_id': 'e503'},
        {'reason': None, 'error_id': 'e503'},
        {},
        None,
        'VENDOR_UNAVAILABLE',
    ],
    ids=['db', 'peer', 'lower', 'trailing-space', 'list', 'no-reason', 'null-reason', 'empty', 'no-body', 'text-body'],
)
def test_on_test_a_503_on_r13_without_reason_vendor_unavailable_stops_as_busy(body):
    # tj-grna9p.150 (3): only the fake broker's VENDOR_UNAVAILABLE is the designed failure; any other 503 is real.
    store = fake_broker_store()
    store.post_answers = {'r13': tool.Response(503, body)}
    outcome = run_tool(['seed', NO_RESET], http=store)
    assert outcome.status == 1
    assert 'failed once as designed' not in outcome.text
    assert outcome.lines[-1].startswith('r13 503 ')
    assert outcome.lines[-1].endswith(': the store or the vendor is busy; wait, then rerun')
    # The run stops at r13: r14 is never requested.
    assert 'GAPS_ZZDSDIA' not in [call.route.rsplit('/', 1)[-1] for call in store.posts()]


def test_the_designed_failure_is_exactly_503_with_reason_vendor_unavailable():
    assert (tool.FAILONCE_STATUS, tool.FAILONCE_REASON) == (503, 'VENDOR_UNAVAILABLE')


def test_on_test_a_503_on_another_row_still_stops_and_says_wait():
    store = fake_broker_store()
    store.post_answers = {'r12': VENDOR_UNAVAILABLE}
    outcome = run_tool(['seed', NO_RESET], http=store)
    assert outcome.status == 1
    assert 'wait' in outcome.lines[-1]
    assert 'failed once as designed' not in outcome.text


def test_on_dev_r13s_503_is_not_excused():
    store = store_for()
    store.post_answers = {'r13': VENDOR_UNAVAILABLE}
    outcome = run_tool(['seed', NO_RESET], http=store)
    assert outcome.status == 1
    assert 'failed once as designed' not in outcome.text


# ---- degrade and repair on test -------------------------------------------------------------------------------


def test_degrade_on_test_skips_r13_and_r14_and_deletes_r12_under_its_synthetic_symbol():
    db = FakeDb.with_plan_entries('test')
    outcome = run_tool(['degrade'], http=fake_broker_store(), connect=db.connect)
    assert outcome.status == 0, outcome.text
    assert 'r13 skipped: its first fetch fails by design (FAILONCE_), so it holds no bars to delete' in outcome.lines
    lookups = [params for sql, params in db.statements if sql == tool.LOOKUP_SQL]
    assert lookups == [('dev-degraded', 'ZZDSDIA', 'THIRTY_MINUTES')]
    assert db.commits == 1
    assert not any('r14' in line for line in outcome.lines)


def test_repair_r13_on_test_repeats_the_identical_request_and_succeeds():
    store = fake_broker_store()
    outcome = run_tool(['repair', '--row', 'r13'], http=store)
    assert outcome.status == 0, outcome.text
    [post] = store.posts()
    assert post.route == '/store/stock/market-activity/FAILONCE_ZZDSQQQ'
    assert post.body == store.bodies['r13']
    assert outcome.lines[-1] == 'r13 200 data_points=42'


def test_a_second_failure_of_r13_on_repair_stops():
    # FAILONCE_ fails ONCE: a repair that fails again is a real failure, not the scenario.
    store = fake_broker_store()
    store.post_answers = {'r13': VENDOR_UNAVAILABLE}
    outcome = run_tool(['repair', '--row', 'r13'], http=store)
    assert outcome.status == 1
    assert 'failed once as designed' not in outcome.text


def test_repair_on_test_reposts_r12_and_r13_and_leaves_r14():
    store = fake_broker_store()
    assert run_tool(['repair'], http=store).status == 0
    assert route_symbols(store) == ['ZZDSDIA', 'FAILONCE_ZZDSQQQ']


def test_reads_on_test_look_up_and_read_the_synthetic_symbols():
    store = fake_broker_store(present=set(ALL))
    outcome = run_tool(['reads'], http=store)
    assert outcome.status == 0, outcome.text
    sdk = [call for call in store.calls if call.route == tool.ASSET_DATA_PATH]
    assert [call.query['asset_symbol'] for call in sdk] == ['ZZDSQQQ']
    assert set(route_symbols(store)) == {'ZZDSSPY', 'ZZDSDIA', 'ZZDSQQQ'}


def test_the_test_degrade_window_is_the_dev_one():
    dev_db, test_db = FakeDb.with_plan_entries(), FakeDb.with_plan_entries('test')
    assert run_tool(['degrade', '--row', 'r12'], http=store_for(), connect=dev_db.connect).status == 0
    assert run_tool(['degrade', '--row', 'r12'], http=fake_broker_store(), connect=test_db.connect).status == 0
    dev_delete = [params for sql, params in dev_db.statements if sql == tool.DELETE_SQL]
    test_delete = [params for sql, params in test_db.statements if sql == tool.DELETE_SQL]
    assert [p[1:3] for p in dev_delete] == [p[1:3] for p in test_delete]
    first, _ = tool.degrade_window(next(r for r in tool.load_plan() if r.id == 'r12').degrade, date(2026, 10, 20))
    assert dev_delete[0][1] == first
