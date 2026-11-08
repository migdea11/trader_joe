"""The session grant (ADR tj-4rr0la addendum 34; addendum 33 for the absence of any counter; tj-ivuy8r.4).

Every STATE-CHANGING verb needs an open, unexpired grant whose mode and verb list cover it, checked at
ADMISSION: after 'unknown verb' and 'busy', before the argument check and before every rail. The five read
verbs and the three session verbs need none. migrate, restart and down take the mode their guard resolves (an
empty project counts as DEV), so their mode is checked right after that guard. A grant only adds a condition:
it never relaxes, skips or reorders a rail, and there is no counter anywhere.

Every rig here is made with covering_grant=False: no grant exists until a test opens one through the real
stack_session_open_test / stack_session_open_dev verbs. Every expectation is spelled from literals, never
read from the module under test, so a pin cannot agree with any change to it.
"""

import asyncio
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tools.agent_mcp import runner, stack
from tools.agent_mcp.tests.harness import (
    DEV_MODE_LINE,
    TEST_MODE_LINE,
    VERB_SAMPLES,
    Rig,
    assert_no_budget_wording,
    assert_no_call_counter,
    is_mode_guard,
    make_rig,
    response_for,
    system_suite_response,
)


pytestmark = pytest.mark.build_infra

# THE CLASSIFICATION, from literals (addendum 34).
READ_VERBS = frozenset({'ps', 'logs', 'migrate_status', 'db_tables', 'http_get'})
SESSION_VERBS = frozenset({'stack_session_open_test', 'stack_session_open_dev', 'stack_session_close'})
FREE_VERBS = READ_VERBS | SESSION_VERBS
STATE_CHANGING = frozenset(
    {
        'migrate',
        'run_system_tests',
        'seed_dump',
        'test_reset',
        'test_stop',
        'test_start',
        'test_env',
        'down',
        'restart',
        'build',
        'up',
        'tools',
        'test_build',
        'test_up',
    }
)
TEST_VERBS = (
    'test_build',
    'test_up',
    'test_stop',
    'test_start',
    'test_env',
    'test_reset',
    'run_system_tests',
    'seed_dump',
    'migrate',
    'restart',
    'down',
)
PRESET_VERBS = tuple(verb for verb in TEST_VERBS if verb != 'down')
DEV_VERBS = ('build', 'up', 'down', 'restart', 'migrate', 'tools')
MODE_DEPENDENT = frozenset({'migrate', 'restart', 'down'})
FIXED_MODE = {
    **{verb: 'TEST' for verb in TEST_VERBS if verb not in MODE_DEPENDENT},
    **{verb: 'DEV' for verb in DEV_VERBS if verb not in MODE_DEPENDENT},
}
OPENER = {'TEST': 'stack_session_open_test', 'DEV': 'stack_session_open_dev'}
# A label no dev or test target starts: a prod launch, refused by the mode guard under any grant.
PROD_MODE_LINE = b'/r/docker-compose.yaml\n'
MIXED_MODE_LINES = DEV_MODE_LINE + TEST_MODE_LINE

BEAD = 'tj-ab12cd.4'
PURPOSE = 'grant tests: test mode verbs over the fake daemon'
OPEN_TEST = {'bead': BEAD, 'verbs': 'test-campaign', 'purpose': PURPOSE}
UTC_STAMP = r'\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC'
GRANT_RECORD_KEYS = {'bead', 'mode', 'verbs', 'expires_at', 'purpose'}


class Clock:
    """The runner's monotonic clock, moved by hand."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _rig(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode_line: bytes | None = TEST_MODE_LINE,
    clock: Callable[[], float] | None = None,
) -> Rig:
    """A rig with NO grant, its mode guard answering MODE_LINE (TEST by default; None for the rig's own answer).

    Every other step is answered as system_suite_response, so a test-mode verb's rails hold, and the service
    lookups find one running container each.
    """
    rig = make_rig(tmp_path, monkeypatch, covering_grant=False, clock=clock or Clock())

    def respond(step: stack.Step) -> runner.ProcessResult:
        if mode_line is not None and is_mode_guard(step):
            return runner.ProcessResult(0, mode_line, b'')
        return response_for('test_stop', rig.layout, system_suite_response(rig.layout))(step)

    rig.docker.respond = respond
    return rig


def _args(verb: str) -> dict[str, Any]:
    return dict(VERB_SAMPLES[verb])


def _open(rig: Rig, mode: str = 'TEST', **overrides: Any) -> dict[str, Any]:
    arguments = {'bead': BEAD, 'verbs': 'test-campaign' if mode == 'TEST' else list(DEV_VERBS), 'purpose': PURPOSE}
    arguments.update(overrides)
    return rig.call(OPENER[mode], arguments)


def _summary(bead: str, mode: str, verbs: tuple[str, ...] | list[str], word: str = 'expires') -> str:
    """The regex of a grant as refusals and ps name it: `<bead> <MODE> [verbs] expires <UTC>`."""
    return re.escape(f'{bead} {mode} [{", ".join(verbs)}] {word} ') + UTC_STAMP


def _audit(rig: Rig) -> list[dict[str, Any]]:
    return [json.loads(line) for line in rig.audit_lines()]


def _session_line(rig: Rig) -> str:
    result = rig.call('ps', {})
    assert result['status'] == 'ok', result
    return result['message'].splitlines()[-1]


# =============================================================================================================
# THE CLASSIFICATION: which verbs need a grant, and what each mode's grant may list
# =============================================================================================================


def test_the_free_list_is_exactly_the_five_reads_and_the_three_session_verbs():
    assert runner.GRANT_FREE_VERBS == FREE_VERBS
    agent = runner.AgentStack(settings=None)  # type: ignore[arg-type] -- only the verb table is read
    assert set(agent.verbs) - FREE_VERBS == STATE_CHANGING
    assert len(STATE_CHANGING) == 14


def test_each_mode_has_its_closed_verb_set_and_the_preset_leaves_out_down():
    assert stack.GRANT_VERBS_TEST == TEST_VERBS
    assert stack.GRANT_VERBS_DEV == DEV_VERBS
    assert stack.GRANT_PRESET_TEST == 'test-campaign'
    assert stack.GRANT_PRESET_TEST_VERBS == PRESET_VERBS
    # Every state-changing verb can be granted in some mode, and nothing else can.
    assert set(TEST_VERBS) | set(DEV_VERBS) == STATE_CHANGING
    assert stack.GRANT_FIXED_MODE == FIXED_MODE


def test_a_new_verb_off_the_free_list_is_refused_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A verb added without a grant decision is refused, under no grant and under a TEST preset; it never runs."""
    rig = _rig(tmp_path, monkeypatch)
    ran = []

    async def new_verb(call: runner._Call) -> tuple[str, str]:
        ran.append(True)
        return 'ok', 'the new verb ran'

    monkeypatch.setattr(rig.agent, '_handlers', {**rig.agent._handlers, 'zz_new_verb': new_verb})
    monkeypatch.setitem(runner.VERB_SCHEMAS, 'zz_new_verb', {'properties': {}, 'required': []})
    monkeypatch.setitem(runner.VERB_TIMEOUT_SECONDS, 'zz_new_verb', 60)
    bare = rig.call('zz_new_verb', {})
    assert bare['status'] == 'refused', bare
    assert bare['message'] == (
        'refused: no session grant is open; ask the owner to approve stack_session_open_test or stack_session_open_dev'
    )
    assert _open(rig)['status'] == 'ok'
    covered = rig.call('zz_new_verb', {})
    assert covered['status'] == 'refused' and covered['message'].startswith('refused: no grant covers zz_new_verb ')
    assert ran == [] and rig.docker.steps == []


# =============================================================================================================
# NO GRANT: every state-changing verb refused with the exact opener named, no docker; the free verbs run
# =============================================================================================================


@pytest.mark.parametrize('verb', sorted(STATE_CHANGING))
def test_without_a_grant_each_state_changing_verb_is_refused_and_runs_no_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
):
    rig = _rig(tmp_path, monkeypatch)
    result = rig.call(verb, _args(verb))
    assert result['status'] == 'refused', result
    if verb in MODE_DEPENDENT:
        expected = (
            'refused: no session grant is open; '
            'ask the owner to approve stack_session_open_test or stack_session_open_dev'
        )
    else:
        mode = FIXED_MODE[verb]
        expected = (
            f'refused: no session grant is open; {verb} acts in {mode} mode: '
            f'ask the owner to approve {OPENER[mode]} with {verb} in verbs'
        )
    assert result['message'] == expected
    assert rig.docker.steps == [] and result['steps'] == []
    [line] = _audit(rig)
    assert line['verb'] == verb and line['status'] == 'refused' and line['grant'] is None
    # The grant check runs before the argument check, so nothing was validated.
    assert line['arguments'] is None


@pytest.mark.parametrize('verb', sorted(READ_VERBS))
def test_the_read_verbs_run_with_no_grant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str):
    rig = _rig(tmp_path, monkeypatch, mode_line=None)
    result = rig.call(verb, _args(verb))
    assert result['status'] == 'ok', result
    assert rig.docker.steps, 'a read verb reached its docker steps'
    assert _audit(rig)[-1]['grant'] is None


def test_the_session_verbs_run_with_no_grant_and_touch_no_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch)
    closed = rig.call('stack_session_close', {})
    assert closed == {**closed, 'status': 'ok', 'message': 'no session grant was open'}
    assert _open(rig)['status'] == 'ok'
    assert _open(rig, 'DEV')['status'] == 'ok'
    assert rig.call('stack_session_close', {})['status'] == 'ok'
    assert rig.docker.steps == []
    assert [line['verb'] for line in _audit(rig)] == [
        'stack_session_close',
        'stack_session_open_test',
        'session_replaced',
        'stack_session_open_dev',
        'stack_session_close',
    ]


def test_without_a_grant_the_grant_refusal_comes_before_the_argument_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig = _rig(tmp_path, monkeypatch)
    for verb, arguments in (('test_reset', {'x': 1}), ('restart', {}), ('test_env', {'service': 'postgres'})):
        result = rig.call(verb, arguments)
        assert result['status'] == 'refused' and 'no session grant is open' in result['message'], result
    assert rig.docker.steps == []
    assert [line['arguments'] for line in _audit(rig)] == [None, None, None]
    # With a covering grant the same calls reach the argument check, and its own refusal answers.
    assert _open(rig)['status'] == 'ok'
    result = rig.call('test_reset', {'x': 1})
    assert result['status'] == 'refused' and result['message'] == "test_reset takes no argument named 'x'", result


def test_without_a_grant_the_grant_refusal_comes_before_every_rail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A steering file and a prod launch would each refuse; with no grant the grant refusal answers first, no step."""
    rig = _rig(tmp_path, monkeypatch, mode_line=PROD_MODE_LINE)
    (rig.layout.stack_dir / 'docker-compose.yaml').write_text('services: {}\n')
    for verb in ('down', 'test_reset', 'build'):
        result = rig.call(verb, _args(verb))
        assert result['status'] == 'refused' and 'no session grant is open' in result['message'], result
    assert rig.docker.steps == []


# =============================================================================================================
# ADMISSION AND SCOPE
# =============================================================================================================


@pytest.mark.parametrize('verb', sorted(STATE_CHANGING))
def test_a_grant_listing_the_verb_in_its_mode_admits_it_to_its_rails_and_act(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
):
    """The rig's own answers (DEV for migrate, restart and down), as the parameterised sweeps run each verb."""
    rig = make_rig(tmp_path, monkeypatch, covering_grant=False)
    rig.docker.respond = response_for(verb, rig.layout)
    mode = FIXED_MODE.get(verb, 'DEV')
    assert rig.call(OPENER[mode], {'bead': BEAD, 'verbs': [verb], 'purpose': PURPOSE})['status'] == 'ok'
    result = rig.call(verb, _args(verb))
    assert result['status'] == 'ok', result
    assert rig.docker.steps
    assert _audit(rig)[-1]['grant']['verbs'] == [verb]


@pytest.mark.parametrize('verb', PRESET_VERBS)
def test_the_test_campaign_preset_admits_each_test_verb_on_a_test_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
):
    rig = _rig(tmp_path, monkeypatch)
    opened = _open(rig)
    assert opened['status'] == 'ok', opened
    if verb == 'test_start':
        rig.docker.respond = response_for('test_start', rig.layout)
    result = rig.call(verb, _args(verb))
    assert result['status'] == 'ok', result


@pytest.mark.parametrize('verb', ['build', 'up', 'tools', 'down'])
def test_the_test_campaign_preset_refuses_the_dev_verbs_and_down_with_no_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
):
    rig = _rig(tmp_path, monkeypatch)
    assert _open(rig)['status'] == 'ok'
    result = rig.call(verb, _args(verb))
    assert result['status'] == 'refused', result
    where, mode = (' in DEV mode', 'DEV') if verb in FIXED_MODE else ('', 'TEST')
    pattern = (
        re.escape(f'refused: no grant covers {verb}{where} (open grant: ')
        + _summary(BEAD, 'TEST', PRESET_VERBS)
        + re.escape(f'); ask the owner to approve {OPENER[mode]} with {verb} in verbs')
    )
    assert re.fullmatch(pattern, result['message']), result['message']
    assert rig.docker.steps == []


def test_down_named_on_its_own_in_a_test_grant_takes_the_test_stack_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig = _rig(tmp_path, monkeypatch)
    assert _open(rig, verbs=['down'])['status'] == 'ok'
    result = rig.call('down', {})
    assert result['status'] == 'ok', result
    assert rig.docker.steps[-1].argv[-1] == 'down'


@pytest.mark.parametrize('verb', ['test_reset', 'test_build', 'run_system_tests', 'test_env'])
def test_a_dev_grant_refuses_the_test_verbs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str):
    rig = _rig(tmp_path, monkeypatch)
    assert _open(rig, 'DEV')['status'] == 'ok'
    result = rig.call(verb, _args(verb))
    pattern = (
        re.escape(f'refused: no grant covers {verb} in TEST mode (open grant: ')
        + _summary(BEAD, 'DEV', DEV_VERBS)
        + re.escape(f'); ask the owner to approve stack_session_open_test with {verb} in verbs')
    )
    assert re.fullmatch(pattern, result['message']), result['message']
    assert rig.docker.steps == []


@pytest.mark.parametrize(('verb', 'wrong'), [('test_reset', 'DEV'), ('build', 'TEST')])
def test_a_grant_of_the_wrong_mode_is_refused_even_if_it_lists_a_fixed_mode_verb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, wrong: str
):
    """Defence in depth: a hand-made grant of the wrong mode is refused at admission, with no step.

    The openers cannot build such a grant (each mode's set holds only its own fixed verbs), so this is the only
    way to reach the admission's mode test; without this test the mode test is an equivalent mutant.
    """
    rig = _rig(tmp_path, monkeypatch, mode_line=DEV_MODE_LINE if wrong == 'TEST' else TEST_MODE_LINE)
    rig.agent._grant = stack.Grant(
        bead=BEAD,
        mode=wrong,
        verbs=(verb,),
        purpose=PURPOSE,
        expires=rig.agent._clock() + 600,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    result = rig.call(verb, _args(verb))
    assert result['status'] == 'refused', result
    assert result['message'].startswith(f'refused: no grant covers {verb} in {FIXED_MODE[verb]} mode (open grant: ')
    assert rig.docker.steps == []


def test_a_grant_admits_only_the_verbs_it_lists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch)
    assert _open(rig, verbs=['test_reset'])['status'] == 'ok'
    assert rig.call('test_reset', {})['status'] == 'ok'
    steps = len(rig.docker.steps)
    result = rig.call('test_up', {})
    assert result['status'] == 'refused' and result['message'].startswith(
        'refused: no grant covers test_up in TEST mode (open grant: tj-ab12cd.4 TEST [test_reset] expires '
    ), result
    assert len(rig.docker.steps) == steps


# =============================================================================================================
# THE MODE CHECK OF migrate, restart AND down: against the mode the guard resolves, right after the guard
# =============================================================================================================


@pytest.mark.parametrize('verb', sorted(MODE_DEPENDENT))
def test_a_test_grant_on_a_dev_stack_is_refused_right_after_the_mode_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
):
    rig = _rig(tmp_path, monkeypatch, mode_line=DEV_MODE_LINE)
    verbs = ['migrate', 'restart', 'down']
    assert _open(rig, verbs=verbs)['status'] == 'ok'
    result = rig.call(verb, _args(verb))
    pattern = (
        re.escape(f'refused: no grant covers {verb} in DEV mode (open grant: ')
        + _summary(BEAD, 'TEST', verbs)
        + re.escape(f'); ask the owner to approve stack_session_open_dev with {verb} in verbs')
    )
    assert re.fullmatch(pattern, result['message']), result['message']
    assert [is_mode_guard(step) for step in rig.docker.steps] == [True], 'only the guard ran'


@pytest.mark.parametrize('verb', sorted(MODE_DEPENDENT))
def test_a_dev_grant_on_a_test_stack_is_refused_right_after_the_mode_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
):
    rig = _rig(tmp_path, monkeypatch, mode_line=TEST_MODE_LINE)
    assert _open(rig, 'DEV')['status'] == 'ok'
    result = rig.call(verb, _args(verb))
    assert result['status'] == 'refused', result
    assert result['message'].startswith(f'refused: no grant covers {verb} in TEST mode (open grant: tj-ab12cd.4 DEV ')
    assert result['message'].endswith(f'ask the owner to approve stack_session_open_test with {verb} in verbs')
    assert [is_mode_guard(step) for step in rig.docker.steps] == [True]


@pytest.mark.parametrize('verb', ['restart', 'down'])
def test_an_empty_project_counts_as_dev(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str):
    rig = _rig(tmp_path, monkeypatch, mode_line=b'')
    assert _open(rig, verbs=[verb])['status'] == 'ok'
    refused = rig.call(verb, _args(verb))
    assert refused['status'] == 'refused' and f'no grant covers {verb} in DEV mode' in refused['message'], refused
    assert [is_mode_guard(step) for step in rig.docker.steps] == [True]
    assert _open(rig, 'DEV', verbs=[verb])['status'] == 'ok'
    assert rig.call(verb, _args(verb))['status'] == 'ok'


def test_migrate_refuses_an_empty_project_at_its_own_guard_under_either_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Migrate's own guard refuses an empty project before any grant mode check: the grant relaxes nothing."""
    for mode in ('TEST', 'DEV'):
        rig = _rig(tmp_path / mode, monkeypatch, mode_line=b'')
        assert _open(rig, mode, verbs=['migrate'])['status'] == 'ok'
        result = rig.call('migrate', {})
        assert result['status'] == 'refused' and 'no grant covers' not in result['message'], result


@pytest.mark.parametrize('verb', sorted(MODE_DEPENDENT))
@pytest.mark.parametrize('mode_line', [PROD_MODE_LINE, MIXED_MODE_LINES], ids=['prod', 'mixed'])
def test_prod_and_mixed_are_refused_by_the_guard_under_any_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, mode_line: bytes
):
    for mode in ('TEST', 'DEV'):
        rig = _rig(tmp_path / mode, monkeypatch, mode_line=mode_line)
        assert _open(rig, mode, verbs=['migrate', 'restart', 'down'])['status'] == 'ok'
        result = rig.call(verb, _args(verb))
        assert result['status'] == 'refused', result
        assert 'no grant covers' not in result['message']
        assert 'prod launch' in result['message'] or 'dev and test containers together' in result['message']
        assert [is_mode_guard(step) for step in rig.docker.steps] == [True]


# =============================================================================================================
# A GRANT NEVER RELAXES A RAIL
# =============================================================================================================


def test_a_covering_grant_leaves_the_steering_file_refusal_in_place(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch, mode_line=DEV_MODE_LINE)
    steering = rig.layout.stack_dir / 'docker-compose.yaml'
    steering.write_text('services: {}\n')
    assert _open(rig, 'DEV')['status'] == 'ok'
    result = rig.call('down', {})
    assert result == {
        **result,
        'status': 'refused',
        'message': f'{steering} would steer compose: remove it, or run the make target yourself',
    }
    # Order unchanged: the guard ran, then the steering check refused; no compose step.
    assert [is_mode_guard(step) for step in rig.docker.steps] == [True]


def test_a_covering_grant_leaves_the_test_mode_guard_in_place(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch, mode_line=DEV_MODE_LINE)
    assert _open(rig)['status'] == 'ok'
    result = rig.call('test_reset', {})
    assert result['status'] == 'refused' and 'no grant covers' not in result['message'], result
    assert 'test mode' in result['message'] or 'test_up' in result['message'], result


def test_a_covering_grant_leaves_the_argument_check_in_place(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch)
    assert _open(rig)['status'] == 'ok'
    result = rig.call('test_stop', {'service': 'postgres'})
    assert result['status'] == 'refused' and 'no grant covers' not in result['message'], result
    assert rig.docker.steps == []


# =============================================================================================================
# OPENING: validation, defaults and maxima (refused over the maximum, never trimmed)
# =============================================================================================================


def _expires_at(rig: Rig) -> datetime:
    line = _session_line(rig)
    stamp = re.search(r' expires (' + UTC_STAMP + r') - ', line)
    assert stamp, line
    return datetime.strptime(stamp.group(1), '%Y-%m-%d %H:%M UTC').replace(tzinfo=UTC)


@pytest.mark.parametrize(('mode', 'default'), [('TEST', 240), ('DEV', 60)])
def test_the_default_minutes_apply_and_expire_on_the_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, default: int
):
    clock = Clock(1000.0)
    rig = _rig(tmp_path, monkeypatch, mode_line=DEV_MODE_LINE if mode == 'DEV' else TEST_MODE_LINE, clock=clock)
    verb = 'test_reset' if mode == 'TEST' else 'tools'
    before = datetime.now(UTC)
    assert _open(rig, mode, verbs=[verb])['status'] == 'ok'
    assert _audit(rig)[-1]['arguments']['minutes'] == default
    shown = _expires_at(rig)
    assert before + timedelta(minutes=default - 1) <= shown <= datetime.now(UTC) + timedelta(minutes=default)
    clock.now = 1000.0 + default * 60 - 0.001
    assert rig.call(verb, _args(verb))['status'] == 'ok'
    clock.now = 1000.0 + default * 60
    expired = rig.call(verb, _args(verb))
    assert expired['status'] == 'refused' and ' expired ' in expired['message'], expired


@pytest.mark.parametrize(('mode', 'maximum'), [('TEST', 480), ('DEV', 120)])
def test_the_maximum_opens_and_one_more_is_refused_leaving_the_open_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, maximum: int
):
    clock = Clock(1000.0)
    rig = _rig(tmp_path, monkeypatch, mode_line=DEV_MODE_LINE if mode == 'DEV' else TEST_MODE_LINE, clock=clock)
    verb = 'test_reset' if mode == 'TEST' else 'tools'
    assert _open(rig, mode, verbs=[verb], minutes=maximum)['status'] == 'ok'
    before = _session_line(rig)
    over = _open(rig, mode, bead='tj-zz99zz', minutes=maximum + 1)
    assert over['status'] == 'refused', over
    assert over['message'] == (
        f'{maximum + 1} minutes is over the {mode} maximum of {maximum}; nothing was opened '
        '(a longer campaign needs a fresh approval after this one expires)'
    )
    assert _session_line(rig) == before and 'tj-zz99zz' not in before
    # The maximum is what was granted: admitted one tick before it ends, refused at it.
    clock.now = 1000.0 + maximum * 60 - 0.001
    assert rig.call(verb, _args(verb))['status'] == 'ok'
    clock.now = 1000.0 + maximum * 60
    assert rig.call(verb, _args(verb))['status'] == 'refused'


BAD_OPENS = [
    ('TEST', {'bead': 'TJ-AB12CD'}, 'bead must be a task id'),
    ('TEST', {'bead': 'tj_ab12cd'}, 'bead must be a task id'),
    ('TEST', {'bead': 'tj-ab12cd\n'}, 'bead must be a task id'),
    ('TEST', {'bead': ''}, 'bead must be a task id'),
    ('TEST', {'bead': 7}, 'bead must be a task id'),
    ('TEST', {'purpose': ''}, 'purpose must be one printable line'),
    ('TEST', {'purpose': '   '}, 'purpose must be one printable line'),
    ('TEST', {'purpose': 'two\nlines'}, 'purpose must be one printable line'),
    ('TEST', {'purpose': 'x' * 201}, 'purpose is 201 characters; the limit is 200'),
    ('TEST', {'verbs': ['test_reset', 'build']}, "'build' is not a TEST grant verb"),
    ('TEST', {'verbs': ['nope']}, "'nope' is not a TEST grant verb"),
    ('TEST', {'verbs': []}, 'verbs must be a non-empty list of TEST verbs'),
    ('TEST', {'verbs': 'test_reset'}, 'verbs is a list of TEST verbs'),
    ('TEST', {'minutes': 0}, 'minutes must be a whole number of at least 1'),
    ('TEST', {'minutes': True}, 'minutes must be a whole number of at least 1'),
    ('TEST', {'minutes': '60'}, 'minutes must be a whole number of at least 1'),
    ('TEST', {'minutes': 1.5}, 'minutes must be a whole number of at least 1'),
    ('DEV', {'verbs': 'test-campaign'}, 'there is no preset in DEV mode'),
    ('DEV', {'verbs': ['test_reset']}, "'test_reset' is not a DEV grant verb"),
    ('TEST', {'extra': 1}, 'stack_session_open_test takes no argument named'),
]


@pytest.mark.parametrize(
    ('mode', 'bad', 'says'), BAD_OPENS, ids=[f'{m}-{next(iter(b))}-{i}' for i, (m, b, _) in enumerate(BAD_OPENS)]
)
def test_a_bad_open_is_refused_and_leaves_the_open_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, bad: dict[str, Any], says: str
):
    rig = _rig(tmp_path, monkeypatch)
    assert _open(rig, verbs=['test_reset'])['status'] == 'ok'
    before = _session_line(rig)
    result = _open(rig, mode, **bad)
    assert result['status'] == 'refused' and says in result['message'], result
    assert _session_line(rig) == before
    assert 'session_replaced' not in [line['verb'] for line in _audit(rig)]
    refused = [line for line in _audit(rig) if line['status'] == 'refused']
    assert refused and refused[-1]['arguments'] is None
    assert rig.call('test_reset', {})['status'] == 'ok'


def test_a_missing_required_open_argument_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch)
    for missing in ('bead', 'verbs', 'purpose'):
        arguments = {key: value for key, value in OPEN_TEST.items() if key != missing}
        result = rig.call('stack_session_open_test', arguments)
        assert result == {**result, 'status': 'refused', 'message': f'stack_session_open_test needs {missing}'}
    assert _session_line(rig) == 'session: none'


def test_a_two_hundred_character_purpose_and_a_duplicate_verb_are_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig = _rig(tmp_path, monkeypatch)
    result = _open(rig, purpose='p' * 200, verbs=['test_reset', 'test_reset', 'down'])
    assert result['status'] == 'ok', result
    assert _audit(rig)[-1]['grant']['verbs'] == ['test_reset', 'down']


# =============================================================================================================
# EXPIRY, CLOSE AND REPLACE
# =============================================================================================================


def test_a_grant_expiring_mid_verb_lets_that_verb_finish_and_refuses_the_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    clock = Clock(1000.0)
    rig = _rig(tmp_path, monkeypatch, clock=clock)
    assert _open(rig, verbs=['test_reset'], minutes=10)['status'] == 'ok'

    def expire(step: stack.Step) -> None:
        clock.now = 1000.0 + 10 * 60 + 5

    rig.docker.hook = expire
    finished = rig.call('test_reset', {})
    assert finished['status'] == 'ok', finished
    rig.docker.hook = None
    steps = len(rig.docker.steps)
    refused = rig.call('test_reset', {})
    pattern = (
        re.escape('refused: the session grant ')
        + _summary(BEAD, 'TEST', ['test_reset'], word='expired')
        + re.escape(
            '; test_reset acts in TEST mode: ask the owner to approve stack_session_open_test with test_reset in verbs'
        )
    )
    assert re.fullmatch(pattern, refused['message']), refused['message']
    assert len(rig.docker.steps) == steps
    lines = _audit(rig)
    assert [line['verb'] for line in lines] == [
        'stack_session_open_test',
        'test_reset',
        'session_expired',
        'test_reset',
    ]
    assert lines[1]['grant']['bead'] == BEAD and lines[2]['grant']['bead'] == BEAD and lines[3]['grant'] is None
    assert _session_line(rig) == 'session: none'


def test_a_mode_dependent_verb_admitted_before_expiry_passes_its_mode_check_after_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The mode check after the guard uses the grant the verb was admitted under; expiry in between unadmits nothing."""
    clock = Clock(1000.0)
    rig = _rig(tmp_path, monkeypatch, mode_line=DEV_MODE_LINE, clock=clock)
    assert _open(rig, 'DEV', verbs=['down'], minutes=1)['status'] == 'ok'

    def expire_at_guard(step: stack.Step) -> None:
        if is_mode_guard(step):
            clock.now = 1000.0 + 120

    rig.docker.hook = expire_at_guard
    assert rig.call('down', {})['status'] == 'ok'
    rig.docker.hook = None
    assert ' expired ' in rig.call('down', {})['message']


def test_a_close_mid_verb_lets_that_verb_finish_and_refuses_the_next(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch, mode_line=DEV_MODE_LINE)
    assert _open(rig, 'DEV', verbs=['restart'])['status'] == 'ok'

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        closes: list[dict[str, Any]] = []

        async def close_at_guard(step: stack.Step) -> None:
            if is_mode_guard(step) and not closes:
                closes.append(await rig.agent.call('stack_session_close', {}))

        rig.docker.hook = close_at_guard
        done = await rig.agent.call('restart', {'service': 'data_store'})
        return closes[0], done

    closed, done = asyncio.run(scenario())
    rig.docker.hook = None
    assert closed['status'] == 'ok' and closed['message'].startswith('session grant closed: tj-ab12cd.4 DEV [restart]')
    assert done['status'] == 'ok', done
    assert rig.call('restart', {'service': 'data_store'})['message'].startswith('refused: no session grant is open')


def test_close_ends_the_grant_and_records_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch)
    assert _open(rig)['status'] == 'ok'
    closed = rig.call('stack_session_close', {})
    assert re.fullmatch(re.escape('session grant closed: ') + _summary(BEAD, 'TEST', PRESET_VERBS), closed['message'])
    result = rig.call('test_reset', {})
    assert result['message'].startswith('refused: no session grant is open;'), result
    close_line = _audit(rig)[-2]
    assert close_line['verb'] == 'stack_session_close' and close_line['grant']['bead'] == BEAD
    assert close_line['arguments'] == {}
    assert _session_line(rig) == 'session: none'


def test_open_replaces_the_open_grant_and_its_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch)
    assert _open(rig, verbs=['test_reset'])['status'] == 'ok'
    replaced = _open(rig, bead='tj-new001', verbs=['test_up'])
    pattern = (
        re.escape('session grant open: ')
        + _summary('tj-new001', 'TEST', ['test_up'])
        + re.escape(f' - {PURPOSE}; it replaced the grant ')
        + _summary(BEAD, 'TEST', ['test_reset'])
    )
    assert re.fullmatch(pattern, replaced['message']), replaced['message']
    old = rig.call('test_reset', {})
    assert old['message'].startswith('refused: no grant covers test_reset in TEST mode (open grant: tj-new001 TEST'), (
        old
    )
    assert rig.call('test_up', {})['status'] == 'ok'
    lines = _audit(rig)
    names = [line['verb'] for line in lines]
    assert names[:3] == ['stack_session_open_test', 'session_replaced', 'stack_session_open_test']
    assert lines[1]['grant']['bead'] == BEAD and lines[1]['grant']['verbs'] == ['test_reset']
    assert lines[2]['grant']['bead'] == 'tj-new001'


def test_a_test_grant_replaced_by_a_dev_grant_admits_dev_verbs_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch, mode_line=DEV_MODE_LINE)
    assert _open(rig)['status'] == 'ok'
    assert _open(rig, 'DEV', verbs=['tools'])['status'] == 'ok'
    assert rig.call('tools', {})['status'] == 'ok'
    assert 'no grant covers test_reset in TEST mode' in rig.call('test_reset', {})['message']


def test_a_restarted_server_holds_no_grant_and_none_was_written_to_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig = _rig(tmp_path, monkeypatch)
    assert _open(rig)['status'] == 'ok'
    assert rig.call('test_reset', {})['status'] == 'ok'
    files = sorted(path.relative_to(rig.layout.stack_dir) for path in rig.layout.stack_dir.rglob('*') if path.is_file())
    assert files == [Path(stack.AUDIT_LOG_NAME)], files
    restarted = runner.AgentStack(rig.layout.settings, run=rig.docker, clock=Clock())
    result = asyncio.run(restarted.call('test_reset', {}))
    assert result['message'].startswith('refused: no session grant is open;'), result


# =============================================================================================================
# THE VERB LOCK, AND NO COUNTER (addendum 33)
# =============================================================================================================


def test_fifty_calls_under_one_grant_are_all_admitted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch)
    assert _open(rig, verbs=['test_reset'])['status'] == 'ok'
    statuses = [rig.call('test_reset', {})['status'] for _ in range(50)]
    assert statuses == ['ok'] * 50
    assert_no_call_counter(rig)


def test_busy_still_fires_under_a_grant_and_the_session_verbs_ignore_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig = _rig(tmp_path, monkeypatch)
    assert _open(rig, verbs=['test_reset'])['status'] == 'ok'

    async def scenario() -> dict[str, dict[str, Any]]:
        hold = asyncio.Event()
        reached = asyncio.Event()

        async def wait(step: stack.Step) -> None:
            reached.set()
            await hold.wait()

        rig.docker.hook = wait
        running = asyncio.create_task(rig.agent.call('test_reset', {}))
        await reached.wait()
        out = {
            'busy': await rig.agent.call('test_reset', {}),
            'ps': await rig.agent.call('ps', {}),
            'open': await rig.agent.call('stack_session_open_dev', {'bead': BEAD, 'verbs': ['up'], 'purpose': PURPOSE}),
            'reopen': await rig.agent.call('stack_session_open_test', OPEN_TEST),
            'close': await rig.agent.call('stack_session_close', {}),
        }
        rig.docker.hook = None
        hold.set()
        out['running'] = await running
        return out

    out = asyncio.run(scenario())
    for result in out.values():
        assert_no_budget_wording(result['message'])
    assert out['busy']['status'] == 'busy' and out['ps']['status'] == 'busy'
    assert out['open']['status'] == out['reopen']['status'] == out['close']['status'] == 'ok'
    assert out['running']['status'] == 'ok', out['running']


def test_with_no_grant_a_held_lock_still_answers_busy_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch)
    rig.agent._running = 'test_reset'
    result = rig.call('test_up', {})
    assert result['status'] == 'busy', result
    rig.agent._running = None


# =============================================================================================================
# PS AND THE AUDIT LOG
# =============================================================================================================


def test_ps_prints_the_open_grant_or_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch)
    assert _session_line(rig) == 'session: none'
    assert _open(rig, verbs=['test_reset', 'down'])['status'] == 'ok'
    line = _session_line(rig)
    pattern = re.escape('session: ') + _summary(BEAD, 'TEST', ['test_reset', 'down']) + re.escape(f' - {PURPOSE}')
    assert re.fullmatch(pattern, line), line
    rig.call('stack_session_close', {})
    assert _session_line(rig) == 'session: none'


def test_every_audit_line_carries_the_grant_it_was_made_under(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch)
    rig.call('ps', {})
    _open(rig, verbs=['test_reset'])
    rig.call('ps', {})
    rig.call('test_reset', {})
    rig.call('test_up', {})
    rig.call('no_such_verb', {})
    rig.call('stack_session_close', {})
    rig.call('ps', {})
    lines = _audit(rig)
    assert [line['verb'] for line in lines] == [
        'ps',
        'stack_session_open_test',
        'ps',
        'test_reset',
        'test_up',
        'no_such_verb',
        'stack_session_close',
        'ps',
    ]
    assert all('grant' in line for line in lines)
    assert lines[0]['grant'] is None and lines[-1]['grant'] is None
    for line in lines[1:-1]:
        grant = line['grant']
        assert set(grant) == GRANT_RECORD_KEYS, line
        assert grant['bead'] == BEAD and grant['mode'] == 'TEST' and grant['verbs'] == ['test_reset']
        assert grant['purpose'] == PURPOSE
        assert datetime.fromisoformat(grant['expires_at']).tzinfo is not None
    assert lines[1]['arguments'] == {
        'bead': BEAD,
        'mode': 'TEST',
        'verbs': ['test_reset'],
        'minutes': 240,
        'purpose': PURPOSE,
    }
    assert lines[4]['status'] == 'refused'


def test_the_open_line_carries_the_new_grant_and_a_refused_open_the_old(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig = _rig(tmp_path, monkeypatch)
    _open(rig, verbs=['test_reset'])
    _open(rig, bead='tj-new001', verbs=['test_up'], minutes=999)
    lines = _audit(rig)
    assert lines[0]['grant']['bead'] == BEAD
    assert lines[1]['status'] == 'refused' and lines[1]['grant']['bead'] == BEAD and lines[1]['arguments'] is None


# =============================================================================================================
# THE DESCRIPTIONS
# =============================================================================================================


@pytest.mark.parametrize('verb', sorted(STATE_CHANGING))
def test_each_state_changing_description_says_it_needs_a_grant_and_names_the_opener(verb: str):
    text = runner.AgentStack(settings=None).describe(verb)  # type: ignore[arg-type]
    assert f'NEEDS A SESSION GRANT: an open, unexpired grant that lists {verb}' in text, text
    if verb in MODE_DEPENDENT:
        assert f'stack_session_open_test or stack_session_open_dev with {verb} in verbs' in text
        assert 'DEV also when it is empty' in text
    else:
        mode = FIXED_MODE[verb]
        assert f'is a {mode} grant' in text and f'approve {OPENER[mode]} with {verb} in verbs' in text
    assert 'never relaxes any other refusal' in text
    assert 'NEEDS NO SESSION GRANT' not in text
    assert_no_budget_wording(text)


@pytest.mark.parametrize('verb', sorted(READ_VERBS))
def test_each_read_description_says_it_needs_no_grant(verb: str):
    text = runner.AgentStack(settings=None).describe(verb)  # type: ignore[arg-type]
    assert 'NEEDS NO SESSION GRANT (a read verb)' in text and 'NEEDS A SESSION GRANT' not in text, text
    assert_no_budget_wording(text)


@pytest.mark.parametrize('verb', sorted(SESSION_VERBS))
def test_each_session_description_needs_no_grant_and_takes_no_lock(verb: str):
    text = runner.AgentStack(settings=None).describe(verb)  # type: ignore[arg-type]
    assert 'needs no grant' in text.lower() and 'no verb lock' in text, text
    assert 'NEEDS A SESSION GRANT' not in text
    assert_no_budget_wording(text)
    for schema_text in json.dumps(runner.VERB_SCHEMAS[verb]).split('"'):
        assert_no_budget_wording(schema_text)
