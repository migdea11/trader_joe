"""The seed_dump verb end to end over FakeDocker, and the runner pieces it added.

tj-irhy0a.22 V1, V4, V5 and the response's details. Design: ADR tj-4rr0la addenda 10, 11 R2, 14
and 15; decision tj-vhboky.55 S9-S11.

V1  the argv: --date only when given and valid; a bad date refused with zero git, copy or docker.
V4  no file content in the response or the audit line: a sentinel in the bundle appears in neither.
V5  over-cap stdout -> 'failed', nothing written; the producer's exit 3 -> 'refused'.
And: a refused bundle or a failed write -> 'failed' with nothing written; success -> details with
the two paths relative to the share root, their sizes in bytes and the manifest's row counts; the
capped read in run_process, against a real child process.
"""

import ast
import asyncio
import errno
import json
import sys
from pathlib import Path

import pytest

from tools.agent_mcp import runner, seeds, stack
from tools.agent_mcp.tests.harness import (
    SEED_MANIFEST,
    SEED_REVISION,
    SEED_SQL,
    SERVER_ROOT,
    WORKTREE_NAME,
    FakeDocker,
    default_response,
    is_seed_producer,
    make_rig,
    step_prefix_length,
    tree_digest,
)


pytestmark = pytest.mark.build_infra

SENTINEL = 'SEEDCONTENTSENTINEL'
SEEDS_DIR = 'agent_mcp_seeds'
# Spelled out (ADR tj-4rr0la addendum 10 (1)); test_commands pins the same words in the tails.
PRODUCER_RUN = ['run', '--rm', '-T', '--entrypoint', '/code/.venv/bin/python', 'test_client', '-m', 'data.store.seeds']


def _stdout(sql: str = SEED_SQL, manifest: str = SEED_MANIFEST, revision: str = SEED_REVISION) -> bytes:
    line = json.dumps({'bundle': 'trader_joe-seed/1', 'revision': revision, 'sql': sql, 'manifest': manifest})
    return (line + '\n').encode('utf-8')


def _producer_answers(result: runner.ProcessResult):
    """A FakeDocker respond: the producer's run answers RESULT, everything else as default."""

    def respond(step: stack.Step) -> runner.ProcessResult:
        return result if is_seed_producer(step) else default_response(step)

    return respond


def _rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, respond=default_response):
    return make_rig(tmp_path, monkeypatch, FakeDocker(respond=respond))


def _tails(rig) -> list[list[str]]:
    return [list(step.argv[step_prefix_length() :]) for step in rig.docker.steps if step.argv[1] == 'compose']


def _seeds_dir(rig) -> Path:
    return rig.layout.home / SEEDS_DIR


# --- V1: the date ----------------------------------------------------------------------------------


@pytest.mark.parametrize('date', ['2026-01-02', '2024-02-29'])
def test_a_valid_date_is_forwarded_as_date_after_the_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, date):
    rig = _rig(tmp_path, monkeypatch)
    result = rig.call('seed_dump', {'worktree': WORKTREE_NAME, 'date': date})
    assert result['status'] == 'ok', result
    assert _tails(rig)[-2:] == [['build', 'test_client'], [*PRODUCER_RUN, '--date', date]]
    [audit] = rig.audit_lines()
    assert json.loads(audit)['arguments'] == {'worktree': WORKTREE_NAME, 'date': date}


def test_no_date_runs_the_producer_with_no_date_argument(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch)
    assert rig.call('seed_dump', {'worktree': WORKTREE_NAME})['status'] == 'ok'
    assert _tails(rig)[-1] == PRODUCER_RUN
    assert json.loads(rig.audit_lines()[0])['arguments'] == {'worktree': WORKTREE_NAME}


def _digits_from(zero: int) -> str:
    """'2026-01-02' spelled in the digit block whose zero is the code point ZERO."""
    return ''.join(chr(zero + int(char)) if char.isdigit() else char for char in '2026-01-02')


_BAD_DATES = {
    'not-a-day': '2023-02-29',
    'month-13': '2026-13-01',
    'day-00': '2026-01-00',
    'one-digit-month': '2026-1-02',
    'two-digit-year': '26-01-02',
    'slashes': '2026/01/02',
    'trailing-newline': '2026-01-02\n',
    'leading-space': ' 2026-01-02',
    # Non-ASCII digits that Python's \d and int() both accept, built from code points.
    'fullwidth-digits': _digits_from(0xFF10),
    'arabic-indic-digits': _digits_from(0x0660),
    'an-option': '--help',
    'empty': '',
    'an-integer': 20260102,
    'a-list': ['2026-01-02'],
}


@pytest.mark.parametrize('date', list(_BAD_DATES.values()), ids=list(_BAD_DATES))
def test_a_bad_date_is_refused_before_any_git_copy_or_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, date):
    """V1: refused with zero git calls, no snapshot and no docker step; the refusal does not echo the value."""
    rig = _rig(tmp_path, monkeypatch)
    result = rig.call('seed_dump', {'worktree': WORKTREE_NAME, 'date': date})
    assert result['status'] == 'refused', result
    assert rig.git.calls == [] and rig.docker.steps == [] and not rig.layout.snapshot.exists()
    if isinstance(date, str) and date.strip():
        assert date.strip() not in result['message'], result['message']
    assert not _seeds_dir(rig).exists()
    assert json.loads(rig.audit_lines()[0])['arguments'] is None


def test_check_seed_date_returns_none_or_the_value():
    assert stack.check_seed_date(None) is None
    assert stack.check_seed_date('2026-10-01') == '2026-10-01'
    with pytest.raises(stack.Refused, match='real calendar date'):
        stack.check_seed_date('2026-02-30')
    # The shape is ASCII digits: a non-ASCII digit is refused by the format check itself, not left to
    # whatever date.fromisoformat makes of it.
    for spelled in (_digits_from(0xFF10), _digits_from(0x0660)):
        with pytest.raises(stack.Refused, match=r'^date must be YYYY-MM-DD$'):
            stack.check_seed_date(spelled)


def test_the_schema_takes_an_optional_date_and_the_timeout_is_the_system_suites():
    """Item 4: date optional; the timeout raised from 30 s to what a build and the scenario need."""
    schema = runner.VERB_SCHEMAS['seed_dump']
    assert set(schema['properties']) == {'worktree', 'date'} and schema['required'] == ['worktree']
    assert schema['properties']['date']['type'] == 'string' and schema['additionalProperties'] is False
    assert runner.VERB_TIMEOUT_SECONDS['seed_dump'] == runner.VERB_TIMEOUT_SECONDS['run_system_tests'] == 1800


def test_seed_dump_steps_caps_the_producers_stdout_and_nothing_else():
    """Step.stdout_cap marks the bundle as data: on the producer's run only, at the bundle cap."""
    build, run = stack.seed_dump_steps(Path('/stack'), Path('/stack/agent_stack.env'), None)
    assert build.stdout_cap is None and run.stdout_cap == seeds.MAX_BUNDLE_BYTES == 8 * 1024 * 1024
    assert build.builds and run.builds, 'both can build, so the bases are ensured before the first'
    assert stack.Step(('x',), Path('/')).stdout_cap is None
    assert list(stack.SEED_PRODUCER_RUN) == PRODUCER_RUN


def test_the_refused_exit_status_is_the_producers():
    """SEED_EXIT_REFUSED is the producer's EXIT_REFUSED (data/store/seeds/__main__.py), read by ast."""
    # SERVER_ROOT: the seed producer travels with the service trees (tj-iontkq.4).
    tree = ast.parse((SERVER_ROOT / 'data' / 'store' / 'seeds' / '__main__.py').read_text(encoding='utf-8'))
    [value] = [
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign) and [ast.unparse(target) for target in node.targets] == ['EXIT_REFUSED']
    ]
    assert runner.SEED_EXIT_REFUSED == value == 3


# --- success: the files and the details ------------------------------------------------------------


def test_success_writes_both_files_and_answers_paths_sizes_and_row_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig = _rig(tmp_path, monkeypatch)
    result = rig.call('seed_dump', {'worktree': WORKTREE_NAME})
    assert result['status'] == 'ok' and result['exit_status'] == 0, result
    relative = f'{SEEDS_DIR}/{WORKTREE_NAME}'
    assert result['details'] == {
        'relative_to': str(rig.layout.home),
        'sql': {'path': f'{relative}/{SEED_REVISION}.sql', 'bytes': len(SEED_SQL.encode('utf-8'))},
        'manifest': {'path': f'{relative}/{SEED_REVISION}.json', 'bytes': len(SEED_MANIFEST.encode('utf-8'))},
        'row_counts': {'store_dataset_entry': 1},
    }
    assert (rig.layout.home / result['details']['sql']['path']).read_text() == SEED_SQL
    assert (rig.layout.home / result['details']['manifest']['path']).read_text() == SEED_MANIFEST
    assert SEED_REVISION in result['message']


def test_only_seed_dump_carries_details(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch)
    assert 'details' not in rig.call('ps', {})


def test_the_bases_are_ensured_before_the_build_and_the_producer_runs_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Addendum 14: the step list starts with the ensure-bases step, then the build, then the run."""
    rig = _rig(tmp_path, monkeypatch)
    rig.call('seed_dump', {'worktree': WORKTREE_NAME})
    kinds = ['base' if step.argv[1] == 'image' else step.argv[step_prefix_length()] for step in rig.docker.steps]
    assert kinds == ['base'] * len(stack.BASE_IMAGES) + ['build', 'run'], kinds


# --- V4: no content in the response or the audit line ---------------------------------------------


def test_the_response_and_the_audit_line_carry_no_file_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """V4: a sentinel row in the bundle reaches the files and nowhere else -- not steps, message or audit."""
    sql = f"INSERT INTO public.store_dataset_entry (id) VALUES ('{SENTINEL}');\n"
    manifest = json.dumps({'revision': SEED_REVISION, 'row_counts': {'store_dataset_entry': 1}, 'n': SENTINEL})
    stdout = _stdout(sql, manifest + '\n')
    rig = _rig(tmp_path, monkeypatch, _producer_answers(runner.ProcessResult(0, stdout, b'')))
    result = rig.call('seed_dump', {'worktree': WORKTREE_NAME})
    assert result['status'] == 'ok', result
    assert SENTINEL in (rig.layout.home / result['details']['sql']['path']).read_text(), 'the row was not written'
    assert SENTINEL not in json.dumps(result)
    assert all(SENTINEL not in line for line in rig.audit_lines())
    produced = result['steps'][-1]
    assert produced['stdout']['text'] == '' and f'{len(stdout)} bytes' in produced['stdout']['note'], produced


def test_a_row_counts_key_that_is_not_a_table_name_is_not_echoed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    manifest = json.dumps({'revision': SEED_REVISION, 'row_counts': {SENTINEL: 1}}) + '\n'
    respond = _producer_answers(runner.ProcessResult(0, _stdout(manifest=manifest), b''))
    rig = _rig(tmp_path, monkeypatch, respond)
    result = rig.call('seed_dump', {'worktree': WORKTREE_NAME})
    assert result['status'] == 'ok' and result['details']['row_counts'] is None, result
    assert SENTINEL not in json.dumps(result)


def test_withhold_gives_the_size_and_never_the_bytes():
    held = runner.withhold(SENTINEL.encode())
    assert held == {'text': '', 'truncated': False, 'note': f'withheld: {len(SENTINEL)} bytes of data, not output'}


def test_the_producers_stderr_stays_under_the_tail_rule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    respond = _producer_answers(runner.ProcessResult(0, _stdout(), b'seed produced: 1 row\n'))
    rig = _rig(tmp_path, monkeypatch, respond)
    result = rig.call('seed_dump', {'worktree': WORKTREE_NAME})
    assert result['steps'][-1]['stderr'] == runner.truncate(b'seed produced: 1 row\n')


# --- V5 and the other failures: nothing written ---------------------------------------------------


def test_over_cap_stdout_fails_and_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """V5: what the capped read keeps of a runaway producer (cap + 1 bytes) is 'failed', nothing written."""
    over = b'x' * (seeds.MAX_BUNDLE_BYTES + 1)
    rig = _rig(tmp_path, monkeypatch, _producer_answers(runner.ProcessResult(0, over, b'')))
    result = rig.call('seed_dump', {'worktree': WORKTREE_NAME})
    assert result['status'] == 'failed' and 'cap' in result['message'], result['message']
    assert not _seeds_dir(rig).exists() and 'details' not in result


def test_exit_3_is_refused_with_its_stderr_and_nothing_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """V5: the producer's refusal (exit 3) is 'refused', its stderr in the steps, even with a bundle on stdout."""
    stderr = b'seed refused: the schema is not at head\n'
    rig = _rig(tmp_path, monkeypatch, _producer_answers(runner.ProcessResult(3, _stdout(), stderr)))
    result = rig.call('seed_dump', {'worktree': WORKTREE_NAME})
    assert result['status'] == 'refused' and result['exit_status'] == 3, result
    assert result['steps'][-1]['stderr']['text'] == stderr.decode()
    assert not _seeds_dir(rig).exists() and 'details' not in result


@pytest.mark.parametrize('exit_status', [1, 2, 125])
def test_any_other_non_zero_exit_fails_and_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_status):
    rig = _rig(tmp_path, monkeypatch, _producer_answers(runner.ProcessResult(exit_status, _stdout(), b'boom\n')))
    result = rig.call('seed_dump', {'worktree': WORKTREE_NAME})
    assert result['status'] == 'failed' and str(exit_status) in result['message'], result
    assert not _seeds_dir(rig).exists()


def test_a_failed_build_stops_before_the_producer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def respond(step: stack.Step) -> runner.ProcessResult:
        if step.argv[1] == 'compose' and step.argv[step_prefix_length()] == 'build':
            return runner.ProcessResult(1, b'', b'build failed\n')
        return default_response(step)

    rig = _rig(tmp_path, monkeypatch, respond)
    result = rig.call('seed_dump', {'worktree': WORKTREE_NAME})
    assert result['status'] == 'failed', result
    assert not [step for step in rig.docker.steps if is_seed_producer(step)]
    assert not _seeds_dir(rig).exists()


def test_a_refused_bundle_fails_naming_no_content_and_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    stdout = _stdout(sql=f'{SENTINEL} without a final newline')
    rig = _rig(tmp_path, monkeypatch, _producer_answers(runner.ProcessResult(0, stdout, b'')))
    result = rig.call('seed_dump', {'worktree': WORKTREE_NAME})
    assert result['status'] == 'failed' and 'refused' in result['message'], result
    assert SENTINEL not in json.dumps(result)
    assert not _seeds_dir(rig).exists()


def test_a_symlinked_worktree_directory_in_the_share_fails_and_its_target_is_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """V3 through the verb: the writer's refusal is 'failed', the link's target byte-identical."""
    rig = _rig(tmp_path, monkeypatch)
    victim = rig.layout.root / 'victim'
    victim.mkdir()
    (victim / 'secret').write_text('never written\n')
    (_seeds_dir(rig)).mkdir()
    (_seeds_dir(rig) / WORKTREE_NAME).symlink_to(victim)
    before = tree_digest(victim)
    result = rig.call('seed_dump', {'worktree': WORKTREE_NAME})
    assert result['status'] == 'failed' and 'details' not in result, result
    assert tree_digest(victim) == before


def test_an_os_failure_writing_fails_naming_the_errno_not_the_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _rig(tmp_path, monkeypatch)

    def full(*args, **kwargs):
        raise OSError(errno.ENOSPC, 'No space left on device', '/agent_mcp_share/secret-path')

    monkeypatch.setattr(runner.seeds, 'write_seed', full)
    result = rig.call('seed_dump', {'worktree': WORKTREE_NAME})
    assert result['status'] == 'failed' and 'ENOSPC' in result['message'], result
    assert 'secret-path' not in result['message']


# --- run_process: the capped read, against a real child ------------------------------------------


def _printer(count: int) -> tuple[str, ...]:
    """A real child that writes COUNT bytes to stdout in chunks, and a line to stderr."""
    script = (
        'import sys\n'
        f'left = {count}\n'
        'while left:\n'
        '    n = min(left, 65536)\n'
        "    sys.stdout.buffer.write(b'y' * n)\n"
        '    left -= n\n'
        "sys.stderr.write('done\\n')\n"
    )
    return (sys.executable, '-c', script)


@pytest.mark.parametrize('extra', [0, 1, 300_000])
def test_run_process_keeps_at_most_the_cap_plus_one_byte_and_reads_to_the_end(tmp_path: Path, extra: int):
    """A capped step's stdout is read to EOF (the child is not blocked) but kept to cap + 1 bytes."""
    cap = 100_000
    step = stack.Step(_printer(cap + extra), tmp_path, stdout_cap=cap)
    result = asyncio.run(asyncio.wait_for(runner.run_process(step), 30))
    assert result.exit_status == 0 and result.stderr == b'done\n'
    assert len(result.stdout) == cap + min(extra, 1)


def test_run_process_without_a_cap_keeps_everything(tmp_path: Path):
    step = stack.Step(_printer(300_000), tmp_path)
    result = asyncio.run(asyncio.wait_for(runner.run_process(step), 30))
    assert len(result.stdout) == 300_000
