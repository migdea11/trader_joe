"""test_stop, test_start and test_env: one service of TEST mode, stopped, started or re-environed (tj-7zjov1.268).

Design: ADR tj-4rr0la addendum 32 (owner ruling 2026-11-02 on tj-7zjov1.38), in test_reset's lineage (tj-w0rddi).
Validator bead tj-7zjov1.270; the validator owns these tests by the owner's ruling A on tj-7zjov1.269.

WHAT IS PINNED
    the allow-list   the literal, name by name: every allowed name's boundary values accepted and one past each end
                     refused; every rejected value SHAPE (sign, exponent, whitespace, newline, '$', quote, unicode
                     digits, case, a non-string) refused before its range; more than 8 names; an unknown name; each
                     never-allowed name and pattern (a forbidden word INSIDE an allowed-looking name included);
                     a name of one service refused for the other; a refusal names the name and never the value
    service          data_store and data_ingest only: postgres, test_client, web, unknown and non-string refused
    the overlay      rendered by the YAML serializer, read back only when byte-identical to what this server would
                     write (re-indented, a trailing newline, re-quoted, anchors and aliases, flow style, extra keys,
                     a wrong service, a bad generation: each refused); mode 0600; temp + rename, no partial file
                     after an injected failure; the whole set replaced; {} drops the service, and the file when empty
    the file sets    the test set gains the overlay only when it exists, LAST; the argv check accepts exactly that
                     path there and nothing else; no dev set can carry it; a plain test_up deletes it before its down
    the verbs        whole step order: the rails, then ONE compose step on the named service only
    the rails        each refusal runs nothing after it: stale MCP image, DEV/PROD/mixed/empty,
                     the postgres mount, a stale stamp on either service, running AND stopped, zero or two
                     containers, start on a running one, stop on a stopped one, a tampered overlay (test_env)
    no budget        the withdrawn 12-an-hour budget is absent (ADR tj-4rr0la addendum 33): CALL_VOLUME admitted
                     calls of each verb all act, none is refused, and a failed act leaves the next call unaffected
    ps               the report from the containers' labels and the MCP's own file, 'pending' on a generation
                     mismatch, never an inspect (so never .Config.Env)
    permissions      none of the three verbs is on any allow entry of the committed client settings

Expected values are literals, never read from stack.py, except where the test is ABOUT the module's own list (the
structural pins): a test that read its expectation from the module under test would agree with any change to it.
"""

import json
import os
import stat
from pathlib import Path

import pytest
import yaml

from tools.agent_mcp import runner, stack
from tools.agent_mcp.tests.harness import (
    CALL_VOLUME,
    DEV_CONTAINER_ID,
    DEV_INGEST_CONTAINER_ID,
    OLD_BUDGET_REFUSAL,
    REPO_ROOT,
    TEST_MODE_LINE,
    Rig,
    assert_no_budget_wording,
    assert_no_call_counter,
    default_response,
    is_agent_env_label_read,
    is_container_stamp_read,
    is_dev_digest,
    is_dev_lookup,
    is_mode_guard,
    is_postgres_mount_read,
    is_service_lookup,
    make_rig,
    postgres_test_source,
    service_of,
    system_suite_response,
)
from tools.agent_mcp.tests.test_dev_label_verbs import DEV_LINE, GUARD_REFUSAL, MIXED_REFUSAL, PROD_LINE


pytestmark = pytest.mark.build_infra

DOCKER = '/usr/local/bin/docker'
TRUSTED = '/opt/agent_mcp/compose'
PROJECT = 'trader_joe_dev'
OVERLAY = 'docker-compose.agent-env.yaml'
VERBS = ('test_stop', 'test_start', 'test_env')
SERVICES = ('data_store', 'data_ingest')
MODE_REFUSAL = 'test_stop, test_start and test_env act only in test mode: call test_up first'
STALE_STACK = 'stale test stack: call test_build, then test_up'
BAD_FILE = f'{OVERLAY} is not what this server wrote: call test_up, which deletes it'
SENTINEL = 'VALUE_SENTINEL_7f3a'

# THE ALLOW-LIST, from addendum 32 (2), spelled out: name -> (accepted, refused-by-range).
ALLOW = {
    'data_store': {
        'PANEL_MANAGER_ENABLED': (('true', 'false'), ()),
        'PANEL_MANAGER_DRY_RUN': (('true', 'false'), ()),
        'INSTANCE_WATCH_INTERVAL_SECONDS': (('1', '2', '14', '15'), ('0', '16', '99')),
        'INSTANCE_WATCH_RESUBMIT_MARGIN_SECONDS': (('0', '1', '15'), ('16', '99')),
    },
    'data_ingest': {
        'ALPACA_OUTGOING_BUFFER_SIZE': (('1', '2', '63', '64'), ('0', '65', '99')),
        'ALPACA_STALL_TIMEOUT_S': (
            ('1', '1.5', '1.000', '299.999', '300', '300.0', '300.000'),
            ('0', '0.999', '300.001', '300.5', '301', '999'),
        ),
        'ALPACA_MAX_ATTEMPTS': (('1', '2', '5'), ('0', '6', '9')),
    },
}
# Shapes no value may have, whatever the name: each must fail the full-match grammar, BEFORE any range check.
BAD_SHAPES = {
    'empty': '',
    'plus sign': '+5',
    'minus sign': '-1',
    'exponent': '1e1',
    'upper exponent': '1E1',
    'leading space': ' 5',
    'trailing space': '5 ',
    'trailing newline': '5\n',
    'embedded newline': '5\n6',
    'tab': '\t5',
    'dollar': '$5',
    'interpolation': '${X}',
    'single quote': "'5'",
    'double quote': '"5"',
    # Built with chr() so the source stays ASCII: \d would match the first two, and Decimal() parses them.
    'arabic-indic digit': chr(0x0665),
    'fullwidth digit': chr(0xFF15),
    'superscript digit': chr(0x00B2),
    'underscore separator': '1_0',
    'hex': '0x5',
    'too many digits': '0005',
    'decimal point alone': '5.',
    'leading point': '.5',
    'four fraction digits': '1.2345',
    'comma': '1,5',
    'nan': 'nan',
    'infinity': 'Infinity',
    'nul': '5\x00',
}
BAD_BOOLS = ('True', 'TRUE', 'False', 'yes', 'no', '1', '0', 'on', ' true', 'true ', 'true\n', 'tru', 'truefalse', '')
# Names that must never be accepted for either service (addendum 32 (2)), with the pattern each one exercises.
NEVER = (
    'INSTANCE_WRITE_SECRET',
    'POSTGRES_PASSWORD',
    'POSTGRES_PASS',
    'POSTGRES_USER',
    'DATABASE_URI',
    'DATABASE_HOST',
    'ALPACA_API_KEY',
    'ALPACA_API_SECRET',
    'ALPACA_API_BASE_URL',
    'ALPACA_TRADING_BASE_URL',
    'ALPACA_SIP_ENABLED',
    'APP_INTERNAL_GRPC_PORT',
    'DATA_STORE_GRPC_TARGET',
    'SERVICE_WORKERS',
    'REAPER_DELETES_ENABLED',
    'DEPLOYMENT_LABEL',
    'DATA_DIR',
    # A forbidden word INSIDE an otherwise allowed-looking name: refused as never-allowed, not merely unknown.
    'PANEL_MANAGER_KEY',
    'PANEL_MANAGER_SECRET_ENABLED',
    'ALPACA_PASSPHRASE',
    'INSTANCE_WATCH_TOKEN_SECONDS',
    'ALPACA_CREDENTIAL_FILE',
    'panel_manager_api_key',
    'reaper_deletes_enabled',
)
NEVER_WORDS = ('KEY', 'SECRET', 'PASS', 'TOKEN', 'CREDENTIAL')


def _refusal(service: object, overrides: object) -> str:
    with pytest.raises(stack.Refused) as caught:
        stack.validate_agent_env(service, overrides)
    return str(caught.value)


# =============================================================================================================
# THE ALLOW-LIST
# =============================================================================================================


def test_the_allow_list_is_exactly_the_literal_of_addendum_32():
    assert {service: set(rules) for service, rules in stack.AGENT_ENV_ALLOW.items()} == {
        service: set(names) for service, names in ALLOW.items()
    }
    assert stack.AGENT_ENV_MAX_NAMES == 8
    assert stack.AGENT_ENV_SERVICES == SERVICES


def test_no_allowed_name_contains_a_forbidden_word_or_is_never_allowed():
    """The structural pin: no edit of the literal can put a secret-shaped or never-allowed name on it."""
    for rules in stack.AGENT_ENV_ALLOW.values():
        for name in rules:
            assert not [word for word in NEVER_WORDS if word in name.upper()], name
            assert not stack.env_name_never_allowed(name), name


@pytest.mark.parametrize(
    ('service', 'name', 'value'),
    [(s, n, v) for s, rules in ALLOW.items() for n, (good, _) in rules.items() for v in good],
)
def test_every_boundary_value_is_accepted(service: str, name: str, value: str):
    assert stack.validate_agent_env(service, {name: value}) == {name: value}


@pytest.mark.parametrize(
    ('service', 'name', 'value'),
    [(s, n, v) for s, rules in ALLOW.items() for n, (_, bad) in rules.items() for v in bad],
)
def test_one_past_each_end_is_refused_naming_the_name_only(service: str, name: str, value: str):
    message = _refusal(service, {name: value})
    assert name in message and 'not accepted' in message, message
    assert f'{name}={value}' not in message and repr(value) not in message


_NUMERIC = [(s, n) for s, rules in ALLOW.items() for n, (good, _) in rules.items() if good[0] != 'true']


@pytest.mark.parametrize('shape', list(BAD_SHAPES), ids=list(BAD_SHAPES))
@pytest.mark.parametrize(('service', 'name'), _NUMERIC, ids=[n for _, n in _NUMERIC])
def test_every_bad_shape_is_refused_for_every_numeric_name(service: str, name: str, shape: str):
    with pytest.raises(stack.Refused):
        stack.validate_agent_env(service, {name: BAD_SHAPES[shape]})


@pytest.mark.parametrize('shape', list(BAD_SHAPES), ids=list(BAD_SHAPES))
@pytest.mark.parametrize(('service', 'name'), _NUMERIC, ids=[n for _, n in _NUMERIC])
def test_the_grammar_refuses_each_bad_shape_before_any_range_is_consulted(service: str, name: str, shape: str):
    """Each rule's pattern alone refuses the shape: the range never sees a sign, an exponent or a foreign digit."""
    rule = stack.AGENT_ENV_ALLOW[service][name]
    assert rule.pattern.fullmatch(BAD_SHAPES[shape]) is None


@pytest.mark.parametrize('value', ['1e1', '+5', chr(0x0665), '5 '])
def test_a_shape_the_decimal_parser_would_take_is_still_refused(value: str):
    """Decimal() parses each of these; only the full-match grammar run first keeps them out."""
    from decimal import Decimal

    assert Decimal(value.strip()) is not None
    with pytest.raises(stack.Refused):
        stack.validate_agent_env('data_ingest', {'ALPACA_STALL_TIMEOUT_S': value})


@pytest.mark.parametrize('value', BAD_BOOLS)
@pytest.mark.parametrize('name', ['PANEL_MANAGER_ENABLED', 'PANEL_MANAGER_DRY_RUN'])
def test_a_boolean_is_exactly_true_or_false(name: str, value: str):
    with pytest.raises(stack.Refused):
        stack.validate_agent_env('data_store', {name: value})


@pytest.mark.parametrize('value', [5, 5.0, True, None, ['5'], {'v': '5'}, b'5'])
def test_a_value_that_is_not_a_string_is_refused(value):
    message = _refusal('data_store', {'INSTANCE_WATCH_INTERVAL_SECONDS': value})
    assert 'INSTANCE_WATCH_INTERVAL_SECONDS' in message


@pytest.mark.parametrize(
    'name', ['panel_manager_enabled', 'Panel_Manager_Enabled', 'PANEL_MANAGER_ENABLED ', ' PANEL_MANAGER_ENABLED']
)
def test_a_name_differing_in_case_or_whitespace_is_not_the_allowed_name(name: str):
    message = _refusal('data_store', {name: 'true'})
    assert 'not on the data_store allow-list' in message, message


@pytest.mark.parametrize('name', NEVER)
@pytest.mark.parametrize('service', SERVICES)
def test_every_never_allowed_name_is_refused_as_never_and_the_value_is_not_echoed(service: str, name: str):
    message = _refusal(service, {name: SENTINEL})
    assert message == f'{name!r} can never be set through test_env', message
    assert SENTINEL not in message


@pytest.mark.parametrize('name', ['UNKNOWN_NAME', 'PATH', 'LD_PRELOAD', 'PYTHONPATH', 'COMPOSE_FILE', 'RUN_MODE'])
@pytest.mark.parametrize('service', SERVICES)
def test_an_unknown_name_is_refused_naming_it_and_the_list(service: str, name: str):
    message = _refusal(service, {name: SENTINEL})
    assert message.startswith(f'{name!r} is not on the {service} allow-list: '), message
    assert SENTINEL not in message


@pytest.mark.parametrize(
    ('service', 'other'), [('data_store', 'data_ingest'), ('data_ingest', 'data_store')], ids=['store', 'ingest']
)
def test_a_name_of_one_service_is_refused_for_the_other(service: str, other: str):
    for name, (good, _) in ALLOW[other].items():
        message = _refusal(service, {name: good[0]})
        assert f'is not on the {service} allow-list' in message, message


def test_more_than_eight_names_are_refused_before_any_name_is_read():
    overrides = {f'NAME_{index}': SENTINEL for index in range(9)}
    assert _refusal('data_store', overrides) == 'at most 8 names per call'


def test_eight_names_pass_the_count_and_then_meet_the_list():
    overrides = {f'NAME_{index}': 'x' for index in range(8)}
    assert 'at most' not in _refusal('data_store', overrides)


def test_a_refusal_for_a_bad_value_never_names_the_value_of_any_pair():
    overrides = {'PANEL_MANAGER_ENABLED': 'true', 'INSTANCE_WATCH_INTERVAL_SECONDS': SENTINEL}
    message = _refusal('data_store', overrides)
    assert 'INSTANCE_WATCH_INTERVAL_SECONDS' in message and SENTINEL not in message and "'true'" not in message


def test_a_name_that_is_not_a_string_is_refused():
    assert _refusal('data_store', {1: 'true'}) == 'an override name must be a string'


@pytest.mark.parametrize('overrides', [[], 'PANEL_MANAGER_ENABLED=true', None, 5, (('a', 'b'),)])
def test_overrides_must_be_an_object(overrides):
    assert 'overrides must be an object' in _refusal('data_store', overrides)


def test_the_result_is_sorted_and_an_empty_object_is_valid():
    assert stack.validate_agent_env('data_store', {}) == {}
    checked = stack.validate_agent_env(
        'data_store', {'PANEL_MANAGER_ENABLED': 'true', 'INSTANCE_WATCH_INTERVAL_SECONDS': '3'}
    )
    assert list(checked) == ['INSTANCE_WATCH_INTERVAL_SECONDS', 'PANEL_MANAGER_ENABLED']


@pytest.mark.parametrize(
    'service', ['postgres', 'test_client', 'web', 'pgadmin', 'DATA_STORE', 'data_store ', '', 7, None]
)
def test_only_the_two_services_are_accepted(service):
    with pytest.raises(stack.Refused) as caught:
        stack.validate_agent_env_service(service)
    assert str(caught.value) == 'service must be one of: data_store, data_ingest'


# =============================================================================================================
# THE OVERLAY: render, the canonical read-back, the write
# =============================================================================================================

STORE = stack.AgentEnvEntry(3, (('INSTANCE_WATCH_INTERVAL_SECONDS', '5'), ('PANEL_MANAGER_ENABLED', 'false')))
INGEST = stack.AgentEnvEntry(4, (('ALPACA_MAX_ATTEMPTS', '2'),))
# The text, from literals: block style, sorted keys, two-space indent, every scalar a single-quoted string where
# YAML would otherwise read it as another type, no document markers, one trailing newline.
CANONICAL = (
    'services:\n'
    '  data_ingest:\n'
    '    environment:\n'
    "      ALPACA_MAX_ATTEMPTS: '2'\n"
    '    labels:\n'
    "      trader_joe.agent.env_generation: '4'\n"
    '      trader_joe.agent.env_names: ALPACA_MAX_ATTEMPTS\n'
    '  data_store:\n'
    '    environment:\n'
    "      INSTANCE_WATCH_INTERVAL_SECONDS: '5'\n"
    "      PANEL_MANAGER_ENABLED: 'false'\n"
    '    labels:\n'
    "      trader_joe.agent.env_generation: '3'\n"
    '      trader_joe.agent.env_names: INSTANCE_WATCH_INTERVAL_SECONDS,PANEL_MANAGER_ENABLED\n'
)


def test_render_is_the_canonical_yaml_text():
    assert stack.render_agent_env({'data_store': STORE, 'data_ingest': INGEST}) == CANONICAL


def test_the_rendered_text_is_yaml_with_exactly_the_pairs_and_the_two_labels():
    document = yaml.safe_load(CANONICAL)
    assert document == {
        'services': {
            'data_ingest': {
                'environment': {'ALPACA_MAX_ATTEMPTS': '2'},
                'labels': {'trader_joe.agent.env_generation': '4', 'trader_joe.agent.env_names': 'ALPACA_MAX_ATTEMPTS'},
            },
            'data_store': {
                'environment': {'INSTANCE_WATCH_INTERVAL_SECONDS': '5', 'PANEL_MANAGER_ENABLED': 'false'},
                'labels': {
                    'trader_joe.agent.env_generation': '3',
                    'trader_joe.agent.env_names': 'INSTANCE_WATCH_INTERVAL_SECONDS,PANEL_MANAGER_ENABLED',
                },
            },
        }
    }


def test_parse_round_trips_render():
    entries = {'data_store': STORE, 'data_ingest': INGEST}
    assert stack.parse_agent_env(stack.render_agent_env(entries)) == entries
    assert stack.parse_agent_env(stack.render_agent_env({'data_ingest': INGEST})) == {'data_ingest': INGEST}


def test_render_quotes_every_value_that_yaml_would_otherwise_retype():
    """'true', '5' and '1.5' must reach compose as strings: a bare true is a YAML boolean."""
    entry = stack.AgentEnvEntry(1, (('ALPACA_STALL_TIMEOUT_S', '1.5'),))
    text = stack.render_agent_env({'data_ingest': entry})
    assert "ALPACA_STALL_TIMEOUT_S: '1.5'" in text
    loaded = yaml.safe_load(text)['services']['data_ingest']
    assert all(isinstance(value, str) for value in (*loaded['environment'].values(), *loaded['labels'].values()))


_NOT_CANONICAL = {
    're-indented': CANONICAL.replace('\n  ', '\n    ').replace('\n      ', '\n        '),
    'a second trailing newline': CANONICAL + '\n',
    'no trailing newline': CANONICAL.rstrip('\n'),
    're-quoted with double quotes': CANONICAL.replace("'2'", '"2"'),
    'an unquoted generation': CANONICAL.replace("generation: '4'", 'generation: 4'),
    'a comment': '# hand edit\n' + CANONICAL,
    'a document start marker': '---\n' + CANONICAL,
    'a document end marker': CANONICAL + '...\n',
    'keys out of order': CANONICAL.replace(
        "      INSTANCE_WATCH_INTERVAL_SECONDS: '5'\n      PANEL_MANAGER_ENABLED: 'false'\n",
        "      PANEL_MANAGER_ENABLED: 'false'\n      INSTANCE_WATCH_INTERVAL_SECONDS: '5'\n",
    ),
    'flow style': (
        "services: {data_ingest: {environment: {ALPACA_MAX_ATTEMPTS: '2'}, labels: "
        "{trader_joe.agent.env_generation: '4', trader_joe.agent.env_names: ALPACA_MAX_ATTEMPTS}}}\n"
    ),
    'an anchor and an alias': (
        'services:\n'
        '  data_ingest:\n'
        '    environment: &env\n'
        "      ALPACA_MAX_ATTEMPTS: '2'\n"
        '    labels:\n'
        "      trader_joe.agent.env_generation: '4'\n"
        '      trader_joe.agent.env_names: ALPACA_MAX_ATTEMPTS\n'
        '  data_store:\n'
        '    environment: *env\n'
        '    labels:\n'
        "      trader_joe.agent.env_generation: '3'\n"
        '      trader_joe.agent.env_names: ALPACA_MAX_ATTEMPTS\n'
    ),
    'a top-level key besides services': CANONICAL + 'version: "3"\n',
    'a service key besides environment and labels': CANONICAL.replace(
        '  data_store:\n    environment:', '  data_store:\n    image: busybox\n    environment:'
    ),
    'a third label': CANONICAL + "      com.example.extra: 'x'\n",
    'the names label lying about the names': CANONICAL.replace(
        'trader_joe.agent.env_names: ALPACA_MAX_ATTEMPTS', 'trader_joe.agent.env_names: DATA_DIR'
    ),
    'a wrong service': CANONICAL.replace('data_ingest', 'postgres'),
    'a service as a list item': 'services:\n- data_store\n',
    'a generation of zero': CANONICAL.replace("generation: '4'", "generation: '0'"),
    'a generation with a leading zero': CANONICAL.replace("generation: '4'", "generation: '04'"),
    'a negative generation': CANONICAL.replace("generation: '4'", "generation: '-4'"),
    'no generation': CANONICAL.replace("      trader_joe.agent.env_generation: '4'\n", ''),
    'a value off the allow-list': CANONICAL.replace("ALPACA_MAX_ATTEMPTS: '2'", "ALPACA_MAX_ATTEMPTS: '9'"),
    'a never-allowed name': CANONICAL.replace("ALPACA_MAX_ATTEMPTS: '2'", "ALPACA_API_KEY: '2'"),
    'a name of the other service': CANONICAL.replace("ALPACA_MAX_ATTEMPTS: '2'", "PANEL_MANAGER_ENABLED: 'true'"),
    'a duplicate key': CANONICAL.replace(
        "      ALPACA_MAX_ATTEMPTS: '2'\n", "      ALPACA_MAX_ATTEMPTS: '2'\n      ALPACA_MAX_ATTEMPTS: '3'\n"
    ),
    'a python tag': "services: !!python/object/apply:os.system ['true']\n",
    'not yaml': 'services: [\n',
    'an empty file': '',
    'a bare list': '- services\n',
}


@pytest.mark.parametrize('text', list(_NOT_CANONICAL.values()), ids=list(_NOT_CANONICAL))
def test_a_file_that_is_not_byte_for_byte_what_the_server_writes_is_refused(text: str):
    with pytest.raises(stack.Refused) as caught:
        stack.parse_agent_env(text)
    assert str(caught.value) == BAD_FILE


def test_the_canonical_text_itself_is_accepted():
    """The refusals above are not vacuous: the unmodified text parses."""
    assert stack.parse_agent_env(CANONICAL) == {'data_store': STORE, 'data_ingest': INGEST}


def test_render_never_writes_an_anchor_even_for_equal_sets():
    same = stack.AgentEnvEntry(1, ())
    text = stack.render_agent_env({'data_store': same, 'data_ingest': stack.AgentEnvEntry(2, ())})
    assert '&' not in text and '*' not in text, text


def test_replace_swaps_the_whole_set_and_moves_the_generation_past_every_one():
    current = {'data_store': STORE, 'data_ingest': INGEST}
    replaced = stack.replace_agent_env(current, 'data_store', {'PANEL_MANAGER_DRY_RUN': 'true'})
    assert replaced == {
        'data_store': stack.AgentEnvEntry(5, (('PANEL_MANAGER_DRY_RUN', 'true'),)),
        'data_ingest': INGEST,
    }
    assert stack.replace_agent_env(current, 'data_store', {}) == {'data_ingest': INGEST}
    assert stack.replace_agent_env({}, 'data_ingest', {'ALPACA_MAX_ATTEMPTS': '1'})['data_ingest'].generation == 1


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_write_is_mode_0600_whatever_the_umask(tmp_path: Path):
    previous = os.umask(0)
    try:
        stack.write_agent_env(tmp_path, {'data_store': STORE})
    finally:
        os.umask(previous)
    assert _mode(tmp_path / OVERLAY) == 0o600
    assert (tmp_path / OVERLAY).read_text() == stack.render_agent_env({'data_store': STORE})


def test_write_over_an_existing_world_readable_file_leaves_0600(tmp_path: Path):
    target = tmp_path / OVERLAY
    target.write_text('old\n')
    target.chmod(0o644)
    stack.write_agent_env(tmp_path, {'data_store': STORE})
    assert _mode(target) == 0o600


def test_write_with_no_entries_removes_the_file(tmp_path: Path):
    stack.write_agent_env(tmp_path, {'data_store': STORE})
    stack.write_agent_env(tmp_path, {})
    assert not (tmp_path / OVERLAY).exists()
    stack.write_agent_env(tmp_path, {})


@pytest.mark.parametrize('failing', ['replace', 'fsync'])
def test_a_failed_write_leaves_the_old_file_whole_and_no_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
):
    stack.write_agent_env(tmp_path, {'data_store': STORE})
    before = (tmp_path / OVERLAY).read_bytes()

    def boom(*args, **kwargs):
        raise OSError(28, 'No space left on device')

    monkeypatch.setattr(stack.os, failing, boom)
    with pytest.raises(OSError):
        stack.write_agent_env(tmp_path, {'data_store': STORE, 'data_ingest': INGEST})
    assert (tmp_path / OVERLAY).read_bytes() == before
    assert sorted(path.name for path in tmp_path.iterdir()) == [OVERLAY]


def test_the_write_goes_through_a_temporary_and_a_rename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    seen = []
    real = os.replace

    def spy(source, destination):
        seen.append((Path(source).name, Path(destination).name, _mode(Path(source))))
        real(source, destination)

    monkeypatch.setattr(stack.os, 'replace', spy)
    stack.write_agent_env(tmp_path, {'data_store': STORE})
    assert seen == [(f'.{OVERLAY}.tmp', OVERLAY, 0o600)]


def test_read_refuses_a_link_and_a_directory_and_reads_nothing_through_them(tmp_path: Path):
    elsewhere = tmp_path / 'elsewhere.yaml'
    elsewhere.write_text(stack.render_agent_env({'data_store': STORE}))
    stack_dir = tmp_path / 'stack'
    stack_dir.mkdir()
    (stack_dir / OVERLAY).symlink_to(elsewhere)
    with pytest.raises(stack.Refused, match='not a regular file'):
        stack.read_agent_env(stack_dir)
    (stack_dir / OVERLAY).unlink()
    (stack_dir / OVERLAY).mkdir()
    with pytest.raises(stack.Refused, match='not a regular file'):
        stack.read_agent_env(stack_dir)


def test_read_of_no_file_is_no_entries(tmp_path: Path):
    assert stack.read_agent_env(tmp_path) == {}
    assert stack.agent_env_path(tmp_path) == tmp_path / OVERLAY


# =============================================================================================================
# THE FILE SETS
# =============================================================================================================

HOST = Path('/home/someone/trader_joe')
TEST_SET = [
    DOCKER,
    'compose',
    '-p',
    PROJECT,
    '--project-directory',
    str(HOST),
    '-f',
    f'{TRUSTED}/docker-compose.yaml',
    '-f',
    f'{TRUSTED}/docker-compose.fake.yaml',
]


def test_the_test_set_gains_the_overlay_only_when_it_exists_and_last(tmp_path: Path):
    assert stack.testmode_prefix(HOST) == TEST_SET
    assert stack.testmode_prefix(HOST, tmp_path) == TEST_SET
    stack.write_agent_env(tmp_path, {'data_store': STORE})
    assert stack.testmode_prefix(HOST, tmp_path) == [*TEST_SET, '-f', str(tmp_path / OVERLAY)]
    assert stack.testmode_prefix(HOST) == TEST_SET, 'without the stack directory (test_up, migrate) never'


def test_no_dev_file_set_carries_the_overlay_even_when_it_exists(tmp_path: Path):
    stack.write_agent_env(tmp_path, {'data_store': STORE})
    for prefix in (
        stack.dev_file_prefix(HOST),
        stack.dev_tools_prefix(HOST),
        stack.testmode_build_prefix(HOST),
        stack.testmode_prefix(HOST),
        stack.dev_compose_prefix(),
    ):
        assert not [word for word in prefix if OVERLAY in word], prefix


_START = ['up', '-d', '--wait', '--wait-timeout', '300', '--no-deps', '--no-recreate', 'data_store']
_ENV = ['up', '-d', '--wait', '--wait-timeout', '300', '--no-deps', '--force-recreate', 'data_ingest']


@pytest.mark.parametrize('tail', [_START, _ENV], ids=['start', 'env'])
def test_the_argv_check_accepts_the_overlay_last_from_the_named_stack_directory(tmp_path: Path, tail):
    stack.check_dev_lifecycle_argv([*TEST_SET, *tail], HOST, tmp_path)
    stack.check_dev_lifecycle_argv([*TEST_SET, '-f', str(tmp_path / OVERLAY), *tail], HOST, tmp_path)


def _bad_overlay_argvs(stack_dir: Path) -> dict[str, list[str]]:
    overlay = str(stack_dir / OVERLAY)
    return {
        'no stack directory given to the check': [*TEST_SET, '-f', overlay, *_START],
        'another stack directory': [*TEST_SET, '-f', f'/elsewhere/{OVERLAY}', *_START],
        'a relative path': [*TEST_SET, '-f', OVERLAY, *_START],
        'another file in the stack directory': [*TEST_SET, '-f', str(stack_dir / 'x.yaml'), *_START],
        'before the fake file': [*TEST_SET[:-2], '-f', overlay, *TEST_SET[-2:], *_START],
        'twice': [*TEST_SET, '-f', overlay, '-f', overlay, *_START],
        'after the tail': [*TEST_SET, *_START, '-f', overlay],
        'attached': [*TEST_SET, f'-f{overlay}', *_START],
        'long option': [*TEST_SET, '--file', overlay, *_START],
        'on the dev set': [*stack.dev_file_prefix(HOST), '-f', overlay, *_START],
        'on the build set': [*stack.testmode_build_prefix(HOST), '-f', overlay, 'build', 'data_store', 'data_ingest'],
        'up of postgres': [*TEST_SET, '-f', overlay, *_START[:-1], 'postgres'],
        'up of web': [*TEST_SET, '-f', overlay, *_START[:-1], 'web'],
        'up of both': [*TEST_SET, '-f', overlay, *_START, 'data_ingest'],
        'up without --no-deps': [*TEST_SET, '-f', overlay, 'up', '-d', '--wait', '--force-recreate', 'data_store'],
        'stop under the file set': [*TEST_SET, '-f', overlay, 'stop', 'data_store'],
        'down under the file set': [*TEST_SET, '-f', overlay, 'down'],
    }


@pytest.mark.parametrize('case', list(_bad_overlay_argvs(Path('/s'))))
def test_the_argv_check_refuses_the_overlay_anywhere_else(tmp_path: Path, case: str):
    argv = _bad_overlay_argvs(tmp_path)[case]
    stack_dir = None if case == 'no stack directory given to the check' else tmp_path
    with pytest.raises(stack.Refused):
        stack.check_dev_lifecycle_argv(argv, HOST, stack_dir)


@pytest.mark.parametrize(
    'tail',
    [['stop', 'postgres'], ['stop', 'web'], ['stop', 'test_client'], ['stop'], ['stop', 'data_store', 'data_ingest']],
)
def test_the_argv_check_refuses_a_stop_of_anything_but_one_named_service(tail):
    with pytest.raises(stack.Refused):
        stack.check_dev_lifecycle_argv([DOCKER, 'compose', '-p', PROJECT, *tail])


@pytest.mark.parametrize('service', SERVICES)
def test_the_step_builders_name_only_the_service(tmp_path: Path, service: str):
    stop = stack.test_stop_step(tmp_path, service)
    assert list(stop.argv) == [DOCKER, 'compose', '-p', PROJECT, 'stop', service] and stop.builds is False
    start = stack.test_start_step(tmp_path, HOST, service)
    assert list(start.argv) == [*TEST_SET, *_START[:-1], service] and start.builds is True
    assert dict(start.env)['DATA_DIR'] == './volume_data/test'
    stack.write_agent_env(tmp_path, {service: stack.AgentEnvEntry(1, ())})
    env = stack.test_env_step(tmp_path, HOST, service)
    assert list(env.argv) == [*TEST_SET, '-f', str(tmp_path / OVERLAY), *_ENV[:-1], service]
    assert dict(env.env)['DATA_DIR'] == './volume_data/test' and env.cwd == tmp_path


@pytest.mark.parametrize('builder', ['test_stop', 'test_start', 'test_env'])
@pytest.mark.parametrize('service', ['postgres', 'web', 'test_client'])
def test_the_step_builders_refuse_any_other_service(tmp_path: Path, builder: str, service: str):
    with pytest.raises(stack.Refused):
        if builder == 'test_stop':
            stack.test_stop_step(tmp_path, service)
        else:
            getattr(stack, f'{builder}_step')(tmp_path, HOST, service)


# =============================================================================================================
# THE VERBS THROUGH THE SERVER: rails, order, the act
# =============================================================================================================


class Daemon:
    """The rig's answer: TEST mode, the test mount, and per service a list of (id, state) for the any-state lookup."""

    def __init__(self, layout, containers: dict[str, list[tuple[str, str]]], labels: bytes = b'') -> None:
        self.base = system_suite_response(layout)
        self.containers = containers
        self.labels = labels

    def __call__(self, step: stack.Step) -> runner.ProcessResult:
        if is_service_lookup(step):
            rows = ''.join(f'{cid} {state}\n' for cid, state in self.containers[service_of(step)])
            return runner.ProcessResult(0, rows.encode(), b'')
        if is_agent_env_label_read(step):
            return runner.ProcessResult(0, self.labels, b'')
        return self.base(step)


_STATE_FOR = {'test_stop': 'running', 'test_start': 'exited', 'test_env': 'running'}


def _rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str = 'test_env', **states) -> Rig:
    rig = make_rig(tmp_path, monkeypatch)
    containers = {
        'data_store': [(DEV_CONTAINER_ID, states.get('data_store', _STATE_FOR[verb]))],
        'data_ingest': [(DEV_INGEST_CONTAINER_ID, states.get('data_ingest', _STATE_FOR[verb]))],
    }
    rig.docker.respond = Daemon(rig.layout, containers)
    return rig


def _args(verb: str, service: str = 'data_store', overrides=None) -> dict:
    if verb == 'test_env':
        return {'service': service, 'overrides': {'PANEL_MANAGER_ENABLED': 'false'} if overrides is None else overrides}
    return {'service': service}


def _kind(step: stack.Step) -> str:
    if is_mode_guard(step):
        return 'guard'
    if is_dev_lookup(step):
        return 'lookup:postgres' if service_of(step) == 'postgres' else f'lookup:{service_of(step)}'
    if is_postgres_mount_read(step):
        return 'mount'
    if is_dev_digest(step):
        return 'digest'
    if is_service_lookup(step):
        return f'containers:{service_of(step)}'
    if is_container_stamp_read(step):
        return f'stamp:{step.argv[-1]}'
    if step.argv[1:3] in (('image', 'inspect'), ('pull',)) or step.argv[1:2] == ('pull',):
        return 'base'
    if step.argv[1:2] == ('compose',):
        return 'compose:' + ' '.join(step.argv[-2:])
    return f'other:{step.argv[:3]}'


_RAILS = [
    'guard',
    'lookup:postgres',
    'mount',
    'digest',
    'digest',
    'containers:data_store',
    f'stamp:{DEV_CONTAINER_ID}',
    'containers:data_ingest',
    f'stamp:{DEV_INGEST_CONTAINER_ID}',
]
_ACT = {
    ('test_stop', 'data_store'): ['compose:stop data_store'],
    ('test_stop', 'data_ingest'): ['compose:stop data_ingest'],
    ('test_start', 'data_store'): ['base', 'base', 'compose:--no-recreate data_store'],
    ('test_start', 'data_ingest'): ['base', 'base', 'compose:--no-recreate data_ingest'],
    ('test_env', 'data_store'): ['base', 'base', 'compose:--force-recreate data_store'],
    ('test_env', 'data_ingest'): ['base', 'base', 'compose:--force-recreate data_ingest'],
}


def _composes(rig: Rig) -> list[stack.Step]:
    return [step for step in rig.docker.steps if step.argv[1:2] == ('compose',)]


def _expected_kinds(verb: str, service: str = 'data_store') -> list[str]:
    """The whole step list of one admitted call: the rails, the bases (unless test_stop), then the act."""
    bases = len(stack.BASE_IMAGES)
    expected_act = [k for k in _ACT[(verb, service)] if k != 'base']
    return [*_RAILS, *(['base'] * bases if verb != 'test_stop' else []), *expected_act]


@pytest.mark.parametrize('service', SERVICES)
@pytest.mark.parametrize('verb', VERBS)
def test_an_admitted_call_runs_the_rails_in_order_then_one_compose_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, service: str
):
    rig = _rig(tmp_path, monkeypatch, verb)
    overrides = {'ALPACA_MAX_ATTEMPTS': '2'} if service == 'data_ingest' else None
    result = rig.call(verb, _args(verb, service, overrides))
    assert result['status'] == 'ok', result
    kinds = [_kind(step) for step in rig.docker.steps]
    assert kinds == _expected_kinds(verb, service), kinds
    assert_no_call_counter(rig)
    assert rig.git.calls == [] and not rig.layout.snapshot.exists(), 'no worktree is read'


@pytest.mark.parametrize('verb', VERBS)
def test_the_act_follows_the_last_rail_with_no_step_between(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str):
    """The order is the rails, then the act: the budget step that once sat between them is gone (addendum 33)."""
    rig = _rig(tmp_path, monkeypatch, verb)
    assert rig.call(verb, _args(verb))['status'] == 'ok'
    kinds = [_kind(step) for step in rig.docker.steps]
    assert kinds[: len(_RAILS)] == _RAILS, kinds
    assert kinds[len(_RAILS)] == ('compose:stop data_store' if verb == 'test_stop' else 'base'), kinds
    assert_no_call_counter(rig)


@pytest.mark.parametrize('verb', VERBS)
def test_call_volume_admitted_calls_all_act_and_none_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
):
    """Past the withdrawn 12 an hour, inside one hour: every call runs every rail and its compose step."""
    rig = _rig(tmp_path, monkeypatch, verb)
    for index in range(CALL_VOLUME):
        before = len(rig.docker.steps)
        result = rig.call(verb, _args(verb))
        assert result['status'] == 'ok' and result['message'] != OLD_BUDGET_REFUSAL, (index, result)
        assert [_kind(step) for step in rig.docker.steps[before:]] == _expected_kinds(verb), index
    assert len(_composes(rig)) == CALL_VOLUME
    assert_no_call_counter(rig)


@pytest.mark.parametrize('verb', VERBS)
def test_every_call_across_calls_runs_the_same_steps_as_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str):
    rig = _rig(tmp_path, monkeypatch, verb)
    per_call = []
    for _ in range(CALL_VOLUME):
        before = len(rig.docker.steps)
        assert rig.call(verb, _args(verb))['status'] == 'ok'
        per_call.append([step.argv for step in rig.docker.steps[before:]])
    assert all(steps == per_call[0] for steps in per_call), 'a later call ran different steps from the first'


@pytest.mark.parametrize('verb', VERBS)
def test_a_failed_compose_step_is_failed_and_the_next_call_acts_normally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
):
    """A failure after admission leaves no state that affects the next call (addendum 32 (5) as amended by 33)."""
    rig = _rig(tmp_path, monkeypatch, verb)
    daemon = rig.docker.respond
    rig.docker.respond = lambda step: (
        runner.ProcessResult(1, b'', b'boom') if step.argv[1:2] == ('compose',) else daemon(step)
    )
    for index in range(CALL_VOLUME):
        result = rig.call(verb, _args(verb))
        assert result['status'] == 'failed', (index, result)
    rig.docker.respond = daemon
    before = len(rig.docker.steps)
    result = rig.call(verb, _args(verb))
    assert result['status'] == 'ok', result
    assert [_kind(step) for step in rig.docker.steps[before:]] == _expected_kinds(verb)
    assert_no_call_counter(rig)


def _refused_before_any_act(rig: Rig, result: dict, message: str | None = None) -> None:
    assert result['status'] == 'refused', result
    if message is not None:
        assert result['message'] == message or result['message'].endswith(message), result
    assert _composes(rig) == [], [step.argv for step in rig.docker.steps]
    assert not [step for step in rig.docker.steps if _kind(step) == 'base']
    assert_no_call_counter(rig)
    assert not (rig.layout.stack_dir / OVERLAY).exists(), 'a refused call writes nothing'


# --- refused before ANY docker step -------------------------------------------------------------------------


@pytest.mark.parametrize('service', ['postgres', 'test_client', 'web', 'pgadmin', 'unknown', 7, None, ['data_store']])
@pytest.mark.parametrize('verb', VERBS)
def test_any_other_service_is_refused_before_any_docker_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, service
):
    rig = _rig(tmp_path, monkeypatch, verb)
    result = rig.call(verb, _args(verb, service))
    assert result == {**result, 'status': 'refused', 'message': 'service must be one of: data_store, data_ingest'}
    assert rig.docker.steps == []
    assert_no_call_counter(rig)


@pytest.mark.parametrize(
    'overrides',
    [
        {'DATA_DIR': SENTINEL},
        {'INSTANCE_WRITE_SECRET': SENTINEL},
        {'POSTGRES_PASSWORD': SENTINEL},
        {'ALPACA_API_KEY': SENTINEL},
        {'PANEL_MANAGER_ENABLED': SENTINEL},
        {'INSTANCE_WATCH_INTERVAL_SECONDS': SENTINEL},
        {'UNKNOWN_NAME': SENTINEL},
        {f'NAME_{index}': SENTINEL for index in range(9)},
    ],
    ids=['DATA_DIR', 'secret', 'postgres', 'alpaca key', 'bad bool', 'bad number', 'unknown', 'nine names'],
)
def test_a_refused_override_runs_nothing_and_its_value_is_nowhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict
):
    rig = _rig(tmp_path, monkeypatch)
    result = rig.call('test_env', {'service': 'data_store', 'overrides': overrides})
    assert result['status'] == 'refused', result
    assert rig.docker.steps == []
    assert_no_call_counter(rig)
    assert not (rig.layout.stack_dir / OVERLAY).exists()
    assert SENTINEL not in json.dumps(result)
    assert rig.audit_lines() and all(SENTINEL not in line for line in rig.audit_lines())


@pytest.mark.parametrize('verb', VERBS)
def test_a_stale_mcp_image_refuses_before_any_docker_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str):
    rig = _rig(tmp_path, monkeypatch, verb)
    (rig.layout.repo / 'docker-compose.yaml').write_text('services: {}\n# changed after the image was baked\n')
    result = rig.call(verb, _args(verb))
    assert result['status'] == 'refused' and 'the MCP image is stale' in result['message'], result
    assert rig.docker.steps == []
    assert_no_call_counter(rig)


@pytest.mark.parametrize('verb', VERBS)
def test_a_compose_file_in_the_stack_directory_refuses_before_any_docker_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
):
    rig = _rig(tmp_path, monkeypatch, verb)
    (rig.layout.stack_dir / 'docker-compose.yaml').write_text('services: {}\n')
    result = rig.call(verb, _args(verb))
    assert result['status'] == 'refused', result
    assert rig.docker.steps == []
    assert_no_call_counter(rig)


# --- the mode guard -----------------------------------------------------------------------------------------

_MODES = {
    'nothing is up': (b'', MODE_REFUSAL),
    'dev mode': (DEV_LINE, MODE_REFUSAL),
    'prod': (PROD_LINE, GUARD_REFUSAL),
    'prod beside test': (TEST_MODE_LINE + PROD_LINE, GUARD_REFUSAL),
    'dev and test mixed': (DEV_LINE + TEST_MODE_LINE, MIXED_REFUSAL),
}


@pytest.mark.parametrize(('guard', 'message'), list(_MODES.values()), ids=list(_MODES))
@pytest.mark.parametrize('verb', VERBS)
def test_every_mode_but_test_refuses_at_the_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, guard: bytes, message: str
):
    rig = _rig(tmp_path, monkeypatch, verb)
    daemon = rig.docker.respond
    rig.docker.respond = lambda step: runner.ProcessResult(0, guard, b'') if is_mode_guard(step) else daemon(step)
    result = rig.call(verb, _args(verb))
    _refused_before_any_act(rig, result, message)
    assert [_kind(step) for step in rig.docker.steps] == ['guard']


def test_the_default_dev_rig_refuses_every_verb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for index, verb in enumerate(VERBS):
        rig = make_rig(tmp_path / str(index), monkeypatch)
        rig.docker.respond = default_response
        result = rig.call(verb, _args(verb))
        assert result['status'] == 'refused' and result['message'] == MODE_REFUSAL, result


# --- the postgres mount, the stamps, the containers ------------------------------------------------------------


@pytest.mark.parametrize(
    'mount', ['{host}/volume_data/postgres', '/srv/postgres', '{repo}/volume_data/test/postgres', '{test}/', '']
)
@pytest.mark.parametrize('verb', VERBS)
def test_any_postgres_mount_but_the_test_directory_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, mount: str
):
    rig = _rig(tmp_path, monkeypatch, verb)
    text = mount.format(host=rig.layout.repo_host_path, repo=rig.layout.repo, test=postgres_test_source(rig.layout))
    daemon = rig.docker.respond
    rig.docker.respond = lambda step: (
        runner.ProcessResult(0, f'{text}\n'.encode() if text else b'', b'')
        if is_postgres_mount_read(step)
        else daemon(step)
    )
    _refused_before_any_act(rig, rig.call(verb, _args(verb)))
    assert _kind(rig.docker.steps[-1]) == 'mount'


_STAMPS = {
    'data_store stale': (DEV_CONTAINER_ID, b'f' * 64 + b'\n', 0),
    'data_ingest stale': (DEV_INGEST_CONTAINER_ID, b'f' * 64 + b'\n', 0),
    'data_store unstamped': (DEV_CONTAINER_ID, b'\n', 0),
    'data_ingest unreadable': (DEV_INGEST_CONTAINER_ID, b'', 1),
}


@pytest.mark.parametrize('state', ['running', 'exited'])
@pytest.mark.parametrize(('container', 'stamp', 'status'), list(_STAMPS.values()), ids=list(_STAMPS))
@pytest.mark.parametrize('verb', VERBS)
def test_a_stale_stamp_on_either_service_refuses_running_or_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, container: str, stamp: bytes, status: int, state: str
):
    """The stamp of EVERY container of both services is read, a stopped one too, and a stale one refuses."""
    rig = _rig(tmp_path, monkeypatch, verb, data_store=state, data_ingest=state)
    daemon = rig.docker.respond
    rig.docker.respond = lambda step: (
        runner.ProcessResult(status, stamp, b'')
        if is_container_stamp_read(step) and step.argv[-1] == container
        else daemon(step)
    )
    result = rig.call(verb, _args(verb))
    _refused_before_any_act(rig, result, None if status else STALE_STACK)
    assert _kind(rig.docker.steps[-1]) == f'stamp:{container}'


def test_the_other_services_stopped_container_is_stamped_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch, 'test_env', data_store='running', data_ingest='exited')
    assert rig.call('test_env', _args('test_env'))['status'] == 'ok'
    assert f'stamp:{DEV_INGEST_CONTAINER_ID}' in [_kind(step) for step in rig.docker.steps]


@pytest.mark.parametrize('verb', VERBS)
def test_no_container_of_the_named_service_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str):
    rig = _rig(tmp_path, monkeypatch, verb)
    rig.docker.respond.containers['data_store'] = []
    result = rig.call(verb, _args(verb))
    _refused_before_any_act(rig, result, 'no data_store container in the trader_joe_dev project: call test_up')


@pytest.mark.parametrize('verb', VERBS)
def test_two_containers_of_the_named_service_refuse_and_both_are_stamped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
):
    rig = _rig(tmp_path, monkeypatch, verb)
    rig.docker.respond.containers['data_store'] = [(DEV_CONTAINER_ID, _STATE_FOR[verb]), ('c0ffee0000aa', 'exited')]
    result = rig.call(verb, _args(verb))
    _refused_before_any_act(rig, result, 'refusing to guess which one')
    assert 'c0ffee0000aa' not in result['message']
    assert 'stamp:c0ffee0000aa' in [_kind(step) for step in rig.docker.steps]


@pytest.mark.parametrize('state', ['exited', 'created', 'paused', 'restarting', 'dead'])
def test_test_stop_needs_a_running_container(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str):
    rig = _rig(tmp_path, monkeypatch, 'test_stop', data_store=state)
    _refused_before_any_act(rig, rig.call('test_stop', _args('test_stop')), 'test_stop needs a running one')


@pytest.mark.parametrize('state', ['running', 'paused', 'restarting', 'dead'])
def test_test_start_needs_a_stopped_container(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str):
    rig = _rig(tmp_path, monkeypatch, 'test_start', data_store=state)
    _refused_before_any_act(rig, rig.call('test_start', _args('test_start')), 'test_start needs a stopped one')


@pytest.mark.parametrize('state', ['exited', 'created'])
def test_test_start_accepts_both_stopped_states(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str):
    rig = _rig(tmp_path, monkeypatch, 'test_start', data_store=state)
    assert rig.call('test_start', _args('test_start'))['status'] == 'ok'


@pytest.mark.parametrize('state', ['running', 'exited', 'created'])
def test_test_env_takes_a_container_in_any_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str):
    rig = _rig(tmp_path, monkeypatch, 'test_env', data_store=state)
    assert rig.call('test_env', _args('test_env'))['status'] == 'ok'


@pytest.mark.parametrize('stdout', [b'not a container line\n', b'C0FFEE000001 running\n', b'c0ffee000001\n'])
def test_a_lookup_line_that_is_not_an_id_and_a_state_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: bytes
):
    rig = _rig(tmp_path, monkeypatch)
    daemon = rig.docker.respond
    rig.docker.respond = lambda step: runner.ProcessResult(0, stdout, b'') if is_service_lookup(step) else daemon(step)
    _refused_before_any_act(rig, rig.call('test_env', _args('test_env')))


def test_a_tampered_overlay_refuses_test_env_before_the_act(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch)
    (rig.layout.stack_dir / OVERLAY).write_text(CANONICAL + '# edited\n')
    result = rig.call('test_env', _args('test_env'))
    assert result['status'] == 'refused' and result['message'] == BAD_FILE, result
    assert _composes(rig) == []
    assert_no_call_counter(rig)
    assert (rig.layout.stack_dir / OVERLAY).read_text() == CANONICAL + '# edited\n', 'left as found'


# --- what test_env writes ------------------------------------------------------------------------------------


def _file(rig: Rig) -> dict:
    return yaml.safe_load((rig.layout.stack_dir / OVERLAY).read_text())


def test_test_env_writes_the_overlay_then_recreates_from_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch)
    seen = []
    rig.docker.hook = lambda step: (
        seen.append((rig.layout.stack_dir / OVERLAY).exists()) if step.argv[1:2] == ('compose',) else None
    )
    result = rig.call('test_env', _args('test_env', overrides={'INSTANCE_WATCH_INTERVAL_SECONDS': '2'}))
    assert result['status'] == 'ok' and result['message'].endswith('INSTANCE_WATCH_INTERVAL_SECONDS'), result
    assert seen == [True], 'the file is written before the recreate'
    assert _mode(rig.layout.stack_dir / OVERLAY) == 0o600
    assert _file(rig) == {
        'services': {
            'data_store': {
                'environment': {'INSTANCE_WATCH_INTERVAL_SECONDS': '2'},
                'labels': {
                    'trader_joe.agent.env_generation': '1',
                    'trader_joe.agent.env_names': 'INSTANCE_WATCH_INTERVAL_SECONDS',
                },
            }
        }
    }
    (step,) = _composes(rig)
    assert step.argv[-10:-8] == ('-f', str(rig.layout.stack_dir / OVERLAY)) and step.argv[-8] == 'up'


def test_test_env_replaces_the_whole_set_and_keeps_the_other_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch)
    store = {'PANEL_MANAGER_ENABLED': 'false', 'PANEL_MANAGER_DRY_RUN': 'true'}
    assert rig.call('test_env', _args('test_env', 'data_store', store))['status'] == 'ok'
    assert rig.call('test_env', _args('test_env', 'data_ingest', {'ALPACA_MAX_ATTEMPTS': '1'}))['status'] == 'ok'
    assert (
        rig.call('test_env', _args('test_env', 'data_store', {'INSTANCE_WATCH_INTERVAL_SECONDS': '3'}))['status']
        == 'ok'
    )
    services = _file(rig)['services']
    assert services['data_store']['environment'] == {'INSTANCE_WATCH_INTERVAL_SECONDS': '3'}
    assert services['data_ingest']['environment'] == {'ALPACA_MAX_ATTEMPTS': '1'}
    assert services['data_ingest']['labels']['trader_joe.agent.env_generation'] == '2'
    assert services['data_store']['labels']['trader_joe.agent.env_generation'] == '3'


def test_an_empty_set_drops_the_service_and_then_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch)
    assert rig.call('test_env', _args('test_env', 'data_store', {'PANEL_MANAGER_ENABLED': 'true'}))['status'] == 'ok'
    assert rig.call('test_env', _args('test_env', 'data_ingest', {'ALPACA_MAX_ATTEMPTS': '1'}))['status'] == 'ok'
    result = rig.call('test_env', _args('test_env', 'data_store', {}))
    assert result['status'] == 'ok' and 'cleared' in result['message'], result
    assert set(_file(rig)['services']) == {'data_ingest'}
    assert rig.call('test_env', _args('test_env', 'data_ingest', {}))['status'] == 'ok'
    assert not (rig.layout.stack_dir / OVERLAY).exists()
    # With no file, the recreate runs over the plain test set.
    assert OVERLAY not in ' '.join(_composes(rig)[-1].argv)


def test_test_start_carries_the_overlay_so_a_recreate_could_not_lose_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig = _rig(tmp_path, monkeypatch, 'test_start')
    stack.write_agent_env(rig.layout.stack_dir, {'data_store': STORE})
    assert rig.call('test_start', _args('test_start'))['status'] == 'ok'
    (step,) = _composes(rig)
    assert list(step.argv[:-8]) == [
        *stack.testmode_prefix(rig.layout.repo_host_path),
        '-f',
        str(rig.layout.stack_dir / OVERLAY),
    ]
    assert step.argv[-2:] == ('--no-recreate', 'data_store')


def test_test_stop_never_names_a_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch, 'test_stop')
    stack.write_agent_env(rig.layout.stack_dir, {'data_store': STORE})
    assert rig.call('test_stop', _args('test_stop'))['status'] == 'ok'
    (step,) = _composes(rig)
    assert list(step.argv) == [DOCKER, 'compose', '-p', PROJECT, 'stop', 'data_store']


def test_the_audit_records_the_names_and_no_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch)
    assert (
        rig.call('test_env', _args('test_env', overrides={'INSTANCE_WATCH_RESUBMIT_MARGIN_SECONDS': '13'}))['status']
        == 'ok'
    )
    [line] = rig.audit_lines()
    audit = json.loads(line)
    assert audit['arguments'] == {'service': 'data_store', 'names': ['INSTANCE_WATCH_RESUBMIT_MARGIN_SECONDS']}
    assert '13' not in json.dumps(audit['arguments'])


def test_no_step_reads_an_env_file_or_a_containers_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for index, verb in enumerate(VERBS):
        rig = _rig(tmp_path / str(index), monkeypatch, verb)
        assert rig.call(verb, _args(verb))['status'] == 'ok'
        for step in rig.docker.steps:
            words = ' '.join(step.argv)
            assert '--env-file' not in step.argv and '.Config.Env' not in words and 'printenv' not in words, step


# --- test_up clears -------------------------------------------------------------------------------------------


def test_a_plain_test_up_deletes_the_overlay_before_its_down(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = make_rig(tmp_path, monkeypatch)
    stack.write_agent_env(rig.layout.stack_dir, {'data_store': STORE})
    seen = []
    rig.docker.hook = lambda step: seen.append((_kind(step), (rig.layout.stack_dir / OVERLAY).exists()))
    assert rig.call('test_up', {})['status'] == 'ok'
    composes = [index for index, (kind, _) in enumerate(seen) if kind.startswith('compose:')]
    assert composes and all(not exists for _, exists in seen[composes[0] :]), seen
    assert not (rig.layout.stack_dir / OVERLAY).exists()
    assert all(OVERLAY not in ' '.join(step.argv) for step in rig.docker.steps)


def test_a_refused_test_up_leaves_the_overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Clearing is part of the act: a test_up refused at a rail (here the stale MCP image) changes nothing."""
    rig = make_rig(tmp_path, monkeypatch)
    stack.write_agent_env(rig.layout.stack_dir, {'data_store': STORE})
    (rig.layout.repo / 'docker-compose.yaml').write_text('services: {}\n# changed after the image was baked\n')
    result = rig.call('test_up', {})
    assert result['status'] == 'refused' and 'the MCP image is stale' in result['message'], result
    assert (rig.layout.stack_dir / OVERLAY).read_text() == stack.render_agent_env({'data_store': STORE})


def test_dev_up_never_loads_the_overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = make_rig(tmp_path, monkeypatch)
    stack.write_agent_env(rig.layout.stack_dir, {'data_store': STORE})
    assert rig.call('up', {})['status'] == 'ok'
    assert all(OVERLAY not in ' '.join(step.argv) for step in rig.docker.steps)


# =============================================================================================================
# ps: what is active
# =============================================================================================================

LABEL_READ = [
    DOCKER,
    'ps',
    '-a',
    '--filter',
    f'label=com.docker.compose.project={PROJECT}',
    '--filter',
    'label=com.docker.compose.oneoff=False',
    '--format',
    '{{.Label "com.docker.compose.service"}}|{{.Label "trader_joe.agent.env_generation"}}|'
    '{{.Label "trader_joe.agent.env_names"}}',
]


def _ps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, labels: bytes, entries=None) -> tuple[Rig, dict]:
    rig = make_rig(tmp_path, monkeypatch)
    rig.docker.respond = Daemon(rig.layout, {}, labels)
    if entries is not None:
        stack.write_agent_env(rig.layout.stack_dir, entries)
    return rig, rig.call('ps', {})


def test_ps_reads_the_labels_with_one_fixed_argv_and_never_an_inspect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig, result = _ps(tmp_path, monkeypatch, b'')
    assert result['status'] == 'ok', result
    assert [list(step.argv) for step in rig.docker.steps] == [
        [DOCKER, 'compose', '-p', PROJECT, 'ps', '--all'],
        LABEL_READ,
    ]
    assert stack.agent_env_labels_argv() == tuple(LABEL_READ)
    for step in rig.docker.steps:
        assert 'inspect' not in step.argv and 'exec' not in step.argv and 'Env' not in ' '.join(step.argv)


@pytest.mark.parametrize(
    'argv',
    [
        [*LABEL_READ[:-1], '{{json .}}'],
        [*LABEL_READ[:-1], '{{.Label "com.docker.compose.service"}}|{{.Config.Env}}'],
        [DOCKER, 'inspect', '--format', '{{.Config.Env}}', DEV_CONTAINER_ID],
        LABEL_READ[:4] + LABEL_READ[6:],
        [*LABEL_READ, 'extra'],
    ],
    ids=['json', 'env', 'inspect', 'no oneoff filter', 'extra word'],
)
def test_the_label_read_check_refuses_any_other_argv(argv):
    with pytest.raises(stack.Refused):
        stack.check_agent_env_labels_argv(argv)


def test_ps_reports_the_active_names_and_the_files_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    labels = b'data_store|3|INSTANCE_WATCH_INTERVAL_SECONDS,PANEL_MANAGER_ENABLED\ndata_ingest|4|ALPACA_MAX_ATTEMPTS\n'
    _, result = _ps(tmp_path, monkeypatch, labels, {'data_store': STORE, 'data_ingest': INGEST})
    lines = result['message'].splitlines()
    assert lines == [
        'containers of the trader_joe_dev compose project',
        'agent env data_store: container: generation 3: INSTANCE_WATCH_INTERVAL_SECONDS,PANEL_MANAGER_ENABLED; '
        'file: generation 3: INSTANCE_WATCH_INTERVAL_SECONDS=5, PANEL_MANAGER_ENABLED=false',
        'agent env data_ingest: container: generation 4: ALPACA_MAX_ATTEMPTS; file: generation 4: ALPACA_MAX_ATTEMPTS=2',
    ]


def test_ps_says_pending_when_the_file_and_the_container_differ(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    labels = b'data_store|2|PANEL_MANAGER_ENABLED\ndata_ingest||\n'
    _, result = _ps(tmp_path, monkeypatch, labels, {'data_store': STORE, 'data_ingest': INGEST})
    store, ingest = result['message'].splitlines()[1:]
    assert 'pending' in store and 'pending' in ingest, result['message']
    assert ingest.startswith('agent env data_ingest: container: no overrides; file: generation 4')


def test_ps_says_pending_when_a_container_still_runs_a_cleared_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _, result = _ps(tmp_path, monkeypatch, b'data_store|2|PANEL_MANAGER_ENABLED\n')
    store = result['message'].splitlines()[1]
    assert 'file: no overrides' in store and 'pending' in store, store


def test_ps_is_not_pending_when_neither_has_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _, result = _ps(tmp_path, monkeypatch, b'data_store||\ndata_ingest||\n')
    assert 'pending' not in result['message'], result['message']
    assert result['message'].splitlines()[1] == 'agent env data_store: container: no overrides; file: no overrides'


def test_ps_reports_an_unreadable_file_and_still_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = make_rig(tmp_path, monkeypatch)
    rig.docker.respond = Daemon(rig.layout, {}, b'data_store||\n')
    (rig.layout.stack_dir / OVERLAY).write_text(CANONICAL + '\n')
    result = rig.call('ps', {})
    assert result['status'] == 'ok' and 'file: unreadable' in result['message'], result


@pytest.mark.parametrize('labels', [b'data_store|x|A\n', b'data_store|3|lower_case\n', b'data_store|3|A;B\n'])
def test_ps_reports_unreadable_labels_rather_than_echoing_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, labels: bytes
):
    _, result = _ps(tmp_path, monkeypatch, labels)
    assert 'labels unreadable' in result['message'] and 'lower_case' not in result['message'], result['message']


def test_ps_does_not_compare_two_containers_of_one_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _, result = _ps(tmp_path, monkeypatch, b'data_store|1|A\ndata_store|2|A\n', {'data_store': STORE})
    store = result['message'].splitlines()[1]
    assert '2 containers, not compared' in store and 'pending' not in store, store


def test_ps_is_unchanged_after_call_volume_state_changing_calls_and_has_no_budget_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Ps reports no budget, charge or window line, and CALL_VOLUME state-changing calls change nothing in it."""
    rig = _rig(tmp_path, monkeypatch, 'test_stop')
    first = rig.call('ps', {})
    assert first['status'] == 'ok', first
    for _ in range(CALL_VOLUME):
        assert rig.call('test_stop', _args('test_stop'))['status'] == 'ok'
    after = rig.call('ps', {})
    assert after == {**after, 'status': 'ok', 'message': first['message']}, (first, after)
    for line in after['message'].splitlines():
        assert_no_budget_wording(line)
        assert 'charge' not in line.lower() and 'window' not in line.lower(), line
    assert_no_call_counter(rig)


# =============================================================================================================
# THE SCHEMAS, THE DESCRIPTIONS, THE PERMISSIONS
# =============================================================================================================


def test_the_schemas_are_closed_and_the_service_an_enum():
    for verb in VERBS:
        schema = runner.VERB_SCHEMAS[verb]
        assert schema['additionalProperties'] is False and schema['type'] == 'object'
        assert schema['properties']['service'] == {'type': 'string', 'enum': ['data_store', 'data_ingest']}
    assert runner.VERB_SCHEMAS['test_env']['required'] == ['service', 'overrides']
    overrides = runner.VERB_SCHEMAS['test_env']['properties']['overrides']
    assert overrides['type'] == 'object' and overrides['maxProperties'] == 8
    assert overrides['additionalProperties'] == {'type': 'string'}
    assert runner.VERB_TIMEOUT_SECONDS['test_stop'] == 300
    assert runner.VERB_TIMEOUT_SECONDS['test_start'] == runner.VERB_TIMEOUT_SECONDS['test_env'] == 600


@pytest.mark.parametrize('verb', VERBS)
def test_the_descriptions_name_test_mode_and_the_rails(verb: str):
    text = runner.AgentStack(settings=None).describe(verb)  # type: ignore[arg-type]
    for fragment in (
        'TEST MODE ONLY',
        'data_store',
        'data_ingest',
        'Runs nothing unless',
        'Returns ok or failed itself.',
    ):
        assert fragment in text, fragment
    assert text.endswith('Returns ok or failed itself.'), text
    assert_no_budget_wording(text)
    assert 'charge' not in text.lower(), text
    if verb == 'test_env':
        for name in (n for rules in ALLOW.values() for n in rules):
            assert name in text, name
        assert 'never the value' in text and 'test_up DELETES' in text


def test_no_committed_permission_entry_allows_the_three_verbs():
    """Addendum 32 (7): the three stay on 'ask', like test_reset; an allow entry is the owner's, never committed."""
    settings = json.loads((REPO_ROOT / '.claude' / 'settings.json').read_text(encoding='utf-8'))
    allow = settings.get('permissions', {}).get('allow', [])
    offenders = [
        entry
        for entry in allow
        if entry.startswith('mcp__agent-stack')
        and (entry in {'mcp__agent-stack', 'mcp__agent-stack__*'} or any(verb in entry for verb in VERBS))
    ]
    assert offenders == [], offenders
