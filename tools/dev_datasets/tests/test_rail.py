"""The dev-or-test rail, the credential placeholder, --dry-run and check (validator, tj-grna9p.145, re-pinned .148).

The rail: every writing mode (seed, reset, reads, degrade, repair) asks GET /ui/v1/config first and refuses unless
deploymentLabel is exactly 'dev' or 'test' (tj-grna9p.148 (3)); an empty label (prod's) and every other label are
refused. After a refusal NOTHING is sent: no POST, no further GET, no database connection, so no SQL.

--dry-run of reads, degrade and repair still makes no network or database call at all. --dry-run of seed and reset
(orchestrator ruling, tj-grna9p.148) may make exactly two kinds of READ: the rail's GET and the reset's count SELECTs,
so it can show real counts; test_reset.py pins that it writes nothing.
"""

import pytest

from tools.dev_datasets import request as tool
from tools.dev_datasets.tests.doubles import (
    GOOD_ENV,
    NO_RESET,
    TEST_CONFIG,
    WRITING_MODES,
    ExplodingHttp,
    FakeDb,
    exploding_connect,
    run_tool,
    store_for,
)


pytestmark = pytest.mark.build_infra

NOT_DEV_OR_TEST = [
    tool.Response(200, {'deploymentLabel': 'prod'}),
    tool.Response(200, {'deploymentLabel': ''}),
    tool.Response(200, {'deploymentLabel': 'Dev'}),
    tool.Response(200, {'deploymentLabel': 'dev '}),
    tool.Response(200, {'deploymentLabel': 'Test'}),
    tool.Response(200, {'deploymentLabel': ' test'}),
    tool.Response(200, {'deploymentLabel': 'testing'}),
    tool.Response(200, {'deploymentLabel': 'staging'}),
    tool.Response(200, {'deploymentLabel': 'live'}),
    tool.Response(200, {'deploymentLabel': None}),
    tool.Response(200, {'deploymentLabel': ['dev']}),
    tool.Response(200, {'deployment_label': 'dev'}),
    tool.Response(200, {}),
    tool.Response(200, None),
    tool.Response(200, ['dev']),
    tool.Response(500, {'deploymentLabel': 'dev'}),
    tool.Response(404, None),
]


# Every way a writing mode is spelled, the reset-bearing ones included: seed resets unless --no-reset, and a reset
# needs --yes-reset to get past its own refusal, so each resetting form is tried with it.
WRITING_ARGVS = {
    'seed': ['seed', '--yes-reset'],
    'seed-default': ['--yes-reset'],
    'seed-no-reset': ['seed', NO_RESET],
    'seed-dry-run': ['seed', '--dry-run'],
    'reset': ['reset', '--yes-reset'],
    'reset-flag': ['--reset', '--yes-reset'],
    'reset-unconfirmed': ['reset'],
    'reset-dry-run': ['reset', '--dry-run'],
    'reads': ['reads'],
    'reads-flag': ['--reads'],
    'degrade': ['degrade'],
    'degrade-flag': ['--degrade'],
    'repair': ['repair'],
    'repair-flag': ['--repair'],
}


@pytest.mark.parametrize('argv', list(WRITING_ARGVS.values()), ids=list(WRITING_ARGVS))
@pytest.mark.parametrize('config', NOT_DEV_OR_TEST, ids=lambda r: f'{r.status}-{r.body}')
def test_a_writing_mode_refuses_anything_but_dev_or_test_and_sends_nothing_after(argv, config):
    store = store_for(present={'r4', 'r7', 'r11'}, config=config)
    outcome = run_tool(argv, http=store, connect=exploding_connect)
    assert outcome.status == 1
    assert [(c.method, c.route) for c in store.calls] == [('GET', '/ui/v1/config')]
    assert 'refused: this is not a dev or test stack' in outcome.text


@pytest.mark.parametrize('argv', list(WRITING_ARGVS.values()), ids=list(WRITING_ARGVS))
@pytest.mark.parametrize('config', [store_for().config, TEST_CONFIG], ids=['dev', 'test'])
def test_the_rail_is_the_first_call_of_every_writing_mode_and_dev_and_test_both_pass(argv, config):
    store = store_for(present={'r4', 'r7', 'r11'}, config=config)
    db = FakeDb.with_plan_entries(config.body['deploymentLabel'])
    outcome = run_tool(argv, http=store, connect=db.connect)
    # reset without --yes-reset passes the rail, then refuses on its own (test_reset.py).
    assert outcome.status == (1 if argv == ['reset'] else 0), outcome.text
    assert (store.calls[0].method, store.calls[0].route) == ('GET', '/ui/v1/config')
    assert store.calls[0].headers == {}
    assert 'refused: this is not a dev or test stack' not in outcome.text


@pytest.mark.parametrize(
    ('config', 'line'),
    [
        (tool.Response(200, {'deploymentLabel': 'dev'}), "rail: deploymentLabel is 'dev' (real Alpaca backend)"),
        (TEST_CONFIG, "rail: deploymentLabel is 'test' (fake broker backend)"),
    ],
)
def test_the_rail_says_which_label_and_backend_it_found(config, line):
    outcome = run_tool(['reads'], http=store_for(config=config))
    assert outcome.lines[0] == line


@pytest.mark.parametrize(
    'argv', [['degrade'], ['reset', '--yes-reset'], ['seed', '--yes-reset'], ['seed', '--dry-run']]
)
def test_a_mode_refused_by_the_rail_opens_no_database_connection(argv):
    store = store_for(config=tool.Response(200, {'deploymentLabel': ''}))
    db = FakeDb.with_plan_entries()
    assert run_tool(argv, http=store, connect=db.connect).status == 1
    assert db.connects == 0
    assert db.statements == []


@pytest.mark.parametrize(
    ('config', 'shown'),
    [
        (tool.Response(200, {'deploymentLabel': 'staging'}), "(HTTP 200, deploymentLabel 'staging')"),
        (tool.Response(200, {'deploymentLabel': ''}), "(HTTP 200, deploymentLabel 'none')"),
        (tool.Response(200, {}), "(HTTP 200, deploymentLabel 'none')"),
        (tool.Response(503, {'deploymentLabel': 'dev'}), "(HTTP 503, deploymentLabel 'none')"),
    ],
)
def test_the_refusal_names_the_label_it_saw(config, shown):
    outcome = run_tool(['seed', '--yes-reset'], http=store_for(config=config), connect=exploding_connect)
    assert outcome.lines[-1] == f'refused: this is not a dev or test stack {shown}'


# ---- the credential placeholder ---------------------------------------------------------------------------------

PLACEHOLDER = 'UNSET-start-with-make-agent-up-from-the-repo-root'


@pytest.mark.parametrize('variable', ['INSTANCE_WRITE_SECRET', 'POSTGRES_PASS'])
@pytest.mark.parametrize(
    'argv', [*([mode] for mode in WRITING_MODES), ['seed', '--yes-reset'], ['reset', '--yes-reset']]
)
def test_the_placeholder_in_either_credential_stops_every_writing_mode_before_any_call(argv, variable):
    http = ExplodingHttp()
    outcome = run_tool(argv, {**GOOD_ENV, variable: PLACEHOLDER}, http=http, connect=exploding_connect)
    assert outcome.status == 1
    assert http.calls == []
    assert 'make agent-up from the repo root' in outcome.text
    assert PLACEHOLDER not in outcome.text


# The dry runs that need no rail: reads, degrade, repair, and seed with --no-reset. seed and reset with a reset make
# their read-only calls even then (test_reset.py pins what they do with a placeholder).
OFFLINE_DRY_RUNS = ('reads', 'degrade', 'repair')


@pytest.mark.parametrize('variable', ['INSTANCE_WRITE_SECRET', 'POSTGRES_PASS'])
@pytest.mark.parametrize('mode', OFFLINE_DRY_RUNS)
def test_dry_run_still_works_with_the_placeholder(mode, variable):
    outcome = run_tool(
        [mode, '--dry-run'], {**GOOD_ENV, variable: PLACEHOLDER}, http=ExplodingHttp(), connect=exploding_connect
    )
    assert outcome.status == 0
    assert outcome.lines[0] == f'dry run: {mode}; nothing is sent'


EVERY_DRY_RUN = {
    'seed': ['seed', '--dry-run'],
    'seed-default': ['--dry-run'],
    'seed-no-reset': ['seed', '--dry-run', NO_RESET],
    'reset': ['reset', '--dry-run'],
    'reset-flag': ['--reset', '--dry-run'],
    'reads': ['reads', '--dry-run'],
    'degrade': ['degrade', '--dry-run'],
    'repair': ['repair', '--dry-run'],
    'check': ['check', '--dry-run'],
}
RESETTING_DRY_RUNS = {'seed', 'seed-default', 'reset', 'reset-flag'}


@pytest.mark.parametrize('value', [PLACEHOLDER, ''], ids=['placeholder', 'empty'])
@pytest.mark.parametrize('variable', ['INSTANCE_WRITE_SECRET', 'POSTGRES_PASS'])
@pytest.mark.parametrize('name', list(EVERY_DRY_RUN))
def test_every_dry_run_skips_the_credential_check(name, variable, value):
    # tj-grna9p.150 (1): a dry run sends no secret and opens no database with a placeholder, so no mode's dry run
    # stops on a credential. A resetting dry run still asks the rail (a read, no header); with an unusable password
    # it opens no database at all.
    resets = name in RESETTING_DRY_RUNS
    store = store_for() if resets else None
    http = store if resets else ExplodingHttp()
    db = FakeDb()
    connect = exploding_connect if (variable == 'POSTGRES_PASS' or not resets) else db.connect
    outcome = run_tool(EVERY_DRY_RUN[name], {**GOOD_ENV, variable: value}, http=http, connect=connect)
    assert outcome.status == 0, outcome.text
    assert 'make agent-up' not in outcome.text
    assert ' is empty' not in outcome.text
    assert PLACEHOLDER not in outcome.text
    assert outcome.sleeps == []
    if resets:
        assert [(c.method, c.route, c.headers) for c in store.calls] == [('GET', '/ui/v1/config', {})]
        assert db.writes() == []
        if variable == 'POSTGRES_PASS':
            assert 'reset counts unavailable: the database password is not set' in outcome.lines
    else:
        assert http.calls == []


@pytest.mark.parametrize('variable', ['INSTANCE_WRITE_SECRET', 'POSTGRES_PASS'])
def test_check_reports_the_placeholder_and_still_runs(variable):
    store = store_for()
    outcome = run_tool(['check'], {**GOOD_ENV, variable: PLACEHOLDER}, http=store)
    assert outcome.status == 0
    name = 'secret' if variable == 'INSTANCE_WRITE_SECRET' else 'db password'
    assert f'{name}: PLACEHOLDER' in outcome.lines
    assert PLACEHOLDER not in outcome.text


@pytest.mark.parametrize('mode', ['seed', 'repair'])
def test_an_empty_secret_stops_the_posting_modes_before_any_call(mode):
    http = ExplodingHttp()
    environ = {k: v for k, v in GOOD_ENV.items() if k != 'INSTANCE_WRITE_SECRET'}
    outcome = run_tool([mode], environ, http=http, connect=exploding_connect)
    assert outcome.status == 1
    assert http.calls == []
    assert 'INSTANCE_WRITE_SECRET is empty' in outcome.text


@pytest.mark.parametrize('argv', [['degrade'], ['reset', '--yes-reset'], ['reset'], ['seed', '--yes-reset'], ['seed']])
def test_an_empty_db_password_stops_degrade_and_every_reset_before_any_call(argv):
    http = ExplodingHttp()
    outcome = run_tool(argv, {**GOOD_ENV, 'POSTGRES_PASS': ''}, http=http, connect=exploding_connect)
    assert outcome.status == 1
    assert http.calls == []
    assert 'POSTGRES_PASS is empty' in outcome.text


@pytest.mark.parametrize('value', ['two words', 'line\nbreak', 'tab\there', 'café'])
def test_a_secret_a_header_cannot_carry_is_refused_without_being_shown(value):
    store = store_for()
    outcome = run_tool(['seed', NO_RESET, '--row', 'r1'], {**GOOD_ENV, 'INSTANCE_WRITE_SECRET': value}, http=store)
    assert outcome.status == 1
    assert store.posts() == []
    assert 'characters a header cannot carry' in outcome.text
    assert value not in outcome.text


# ---- --dry-run and check make no write --------------------------------------------------------------------------


OFFLINE_DRY_ARGVS = [[mode, '--dry-run'] for mode in (*OFFLINE_DRY_RUNS, 'check')] + [['seed', '--dry-run', NO_RESET]]


@pytest.mark.parametrize('argv', OFFLINE_DRY_ARGVS, ids=lambda argv: '-'.join(argv))
def test_dry_run_makes_no_network_or_database_call(argv):
    http = ExplodingHttp()
    outcome = run_tool(argv, http=http, connect=exploding_connect)
    assert outcome.status == 0, outcome.text
    assert http.calls == []


def test_dry_run_never_builds_the_real_transport(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError('--dry-run built an HttpClient')

    monkeypatch.setattr(tool, 'HttpClient', refuse)
    for argv in OFFLINE_DRY_ARGVS:
        assert run_tool(argv).status == 0, argv


def test_check_makes_only_two_gets_and_no_write():
    store = store_for()
    outcome = run_tool(['check'], http=store, connect=exploding_connect)
    assert outcome.status == 0
    assert [(c.method, c.route, c.headers) for c in store.calls] == [('GET', '/ping', {}), ('GET', '/ui/v1/config', {})]
    assert outcome.lines == [
        'secret: set',
        'db password: set',
        'ping: HTTP 200',
        'config: HTTP 200 deploymentLabel=dev',
    ]


def test_check_reports_empty_credentials_without_values():
    outcome = run_tool(['check', '--dry-run'], {}, http=ExplodingHttp())
    assert outcome.lines == ['secret: EMPTY', 'db password: EMPTY']


@pytest.mark.parametrize(('label', 'shown'), [('test', 'test'), ('prod', 'prod'), ('', 'none')])
def test_check_on_any_stack_only_reports_it(label, shown):
    store = store_for(config=tool.Response(200, {'deploymentLabel': label}))
    outcome = run_tool(['check'], http=store, connect=exploding_connect)
    assert outcome.status == 0
    assert outcome.lines[-1] == f'config: HTTP 200 deploymentLabel={shown}'
    assert store.writes() == []


def test_dry_run_of_degrade_prints_the_bound_window_and_the_sql():
    outcome = run_tool(['degrade', '--dry-run'], http=ExplodingHttp(), connect=exploding_connect)
    assert outcome.status == 0
    text = '\n'.join(outcome.lines)
    assert 'r12 owner=dev-degraded DIA 30min' in text
    assert 'r13 owner=dev-degraded QQQ 5min' in text
    from datetime import date

    for row in tool.load_plan():
        if row.degrade is not None and row.degrade.drops_bars:
            first, last = tool.degrade_window(row.degrade, date(2026, 10, 20))
            assert f'window {first.isoformat()} .. {last.isoformat()}' in text
            assert first.utcoffset().total_seconds() == -4 * 3600
    assert tool.DELETE_SQL in text and tool.LOOKUP_SQL in text
    # r14 has no window: degrade leaves it alone, and its dry run says nothing about it.
    assert 'r14' not in text


def test_dry_run_of_reads_names_each_read_and_its_reader():
    outcome = run_tool(['--reads', '--dry-run'], http=ExplodingHttp())
    assert outcome.lines[1:] == [
        'r4 GET bars page, no cursor header X-Client-Id: operator',
        'r7 GET bars page, no cursor header X-Client-Id: operator',
        f'r11 GET {tool.ASSET_DATA_PATH} header X-Client-Id: dev-datasets-sdk',
    ]


# ---- the command line -------------------------------------------------------------------------------------------


@pytest.mark.parametrize('mode', ['reads', 'degrade', 'repair', 'check'])
def test_a_mode_is_the_same_as_positional_or_flag(mode):
    positional = run_tool([mode, '--dry-run'], http=ExplodingHttp())
    flag = run_tool([f'--{mode}', '--dry-run'], http=ExplodingHttp())
    both = run_tool([mode, f'--{mode}', '--dry-run'], http=ExplodingHttp())
    assert positional.lines == flag.lines == both.lines
    assert positional.status == 0


def test_seed_is_the_default_mode():
    assert run_tool(['--dry-run', NO_RESET], http=ExplodingHttp()).lines[0] == 'dry run: seed; nothing is sent'


@pytest.mark.parametrize('argv', [['seed', '--reads'], ['--reads', '--degrade'], ['check', '--repair']])
def test_two_modes_at_once_are_refused_before_anything_is_sent(argv):
    http = ExplodingHttp()
    outcome = run_tool(argv, http=http, connect=exploding_connect)
    assert outcome.status == 1
    assert 'one mode at a time' in outcome.text
    assert http.calls == []


@pytest.mark.parametrize('argv', [['wipe'], ['--seed'], ['seed', '--force']])
def test_an_unknown_mode_or_flag_is_a_usage_error(argv):
    http = ExplodingHttp()
    assert run_tool(argv, http=http, connect=exploding_connect).status == 2
    assert http.calls == []


@pytest.mark.parametrize('value', ['2026-02-30', '2026-13-01', 'tomorrow', '2026/10/20'])
@pytest.mark.parametrize('mode', [*WRITING_MODES, 'check'])
def test_a_malformed_date_is_a_usage_error_before_anything_is_sent(mode, value):
    http = ExplodingHttp()
    outcome = run_tool([mode, '--date', value], http=http, connect=exploding_connect)
    assert outcome.status == 2
    assert outcome.lines == ['--date must be YYYY-MM-DD']
    assert http.calls == []


def test_row_limits_seed_to_the_named_rows_in_plan_order():
    store = store_for()
    assert run_tool(['seed', NO_RESET, '--row', 'r9', '--row', 'r2'], http=store).status == 0
    assert [c.body for c in store.posts()] == [store.bodies['r2'], store.bodies['r9']]


def test_an_unknown_row_is_refused_before_anything_is_sent():
    http = ExplodingHttp()
    outcome = run_tool(['seed', NO_RESET, '--row', 'r15'], http=http)
    assert outcome.status == 1
    assert 'no such plan row: r15' in outcome.text
    assert http.calls == []
