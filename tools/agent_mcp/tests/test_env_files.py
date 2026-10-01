"""The generated env files and GUARD 2 (ADR tj-4rr0la addendum 2; tj-c4mosr.3 item 3 as amended).

tj-c4mosr.5 body bullet 5 (ALPACA_* present and empty; POSTGRES_PASS and INSTANCE_WRITE_SECRET random
and different across two generations; the user's env file never opened), addendum-2 pin (3) (three
files, the root one naming all three by absolute path in the stack directory; a verb whose paths
resolve outside it or under a worktree refused before any subprocess), (E) the F1 rule, the steering
keys at generation and in guard 2 (E, G6).

Every refusal here is driven through a real verb (ps, which otherwise runs one docker step), so
"refused before any subprocess" is observed on the fake docker runner, not inferred.
"""

import builtins
import io
import os
import stat
from pathlib import Path

import pytest

from tools.agent_mcp import stack
from tools.agent_mcp.tests.harness import (
    LIVE_ENV_SENTINEL,
    WORKTREE_NAME,
    FakeGit,
    committed_defaults,
    make_layout,
    make_rig,
    plant_live_env_files,
)


pytestmark = pytest.mark.build_infra

KINDS = ('root', 'store', 'ingest')
FILE_NAMES = {'root': 'agent_stack.env', 'store': 'agent_stack_store.env', 'ingest': 'agent_stack_ingest.env'}


def _generate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defaults: dict[str, str] | None = None):
    layout = make_layout(tmp_path)
    git = FakeGit(worktree_paths=[layout.repo], defaults=defaults or committed_defaults())
    monkeypatch.setattr(stack, 'run_git', git)
    root = stack.ensure_env_files(layout.settings)
    values = {kind: stack.parse_env_text((layout.stack_dir / FILE_NAMES[kind]).read_text()) for kind in KINDS}
    return layout, root, values


def test_three_files_0600_in_the_stack_dir_the_root_one_naming_all_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Addendum-2 pin (3): the root file is the --env-file AND ROOT_ENV_FILE, and names the others absolutely."""
    layout, root, values = _generate(tmp_path, monkeypatch)
    assert root == layout.stack_dir / 'agent_stack.env'
    for kind in KINDS:
        path = layout.stack_dir / FILE_NAMES[kind]
        assert stat.S_ISREG(path.lstat().st_mode) and stat.S_IMODE(path.lstat().st_mode) == 0o600, path
    for kind, variable in (('root', 'ROOT_ENV_FILE'), ('store', 'STORE_ENV_FILE'), ('ingest', 'INGEST_ENV_FILE')):
        assert values['root'][variable] == str(layout.stack_dir / FILE_NAMES[kind])
    assert values['root']['DATA_DIR'] == str(layout.stack_dir / 'data')


def test_broker_credentials_are_empty_in_the_ingest_file_and_absent_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Body bullet 5: ALPACA_API_KEY / ALPACA_API_SECRET present and EMPTY -- even when a committed default set them."""
    defaults = committed_defaults()
    for source in defaults:
        defaults[source] += f'\nALPACA_API_KEY={LIVE_ENV_SENTINEL}\nALPACA_API_SECRET={LIVE_ENV_SENTINEL}\n'
    layout, _, values = _generate(tmp_path, monkeypatch, defaults)
    assert values['ingest']['ALPACA_API_KEY'] == '' and values['ingest']['ALPACA_API_SECRET'] == ''
    for kind in ('root', 'store'):
        assert not {'ALPACA_API_KEY', 'ALPACA_API_SECRET'} & set(values[kind]), kind
    for kind in KINDS:
        assert LIVE_ENV_SENTINEL not in (layout.stack_dir / FILE_NAMES[kind]).read_text()


def test_the_secrets_are_random_hex_and_differ_between_generations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _, _, first = _generate(tmp_path / 'one', monkeypatch)
    _, _, second = _generate(tmp_path / 'two', monkeypatch)
    committed = stack.parse_env_text(committed_defaults()['.env.default'])
    for name, length in (('POSTGRES_PASS', 48), ('INSTANCE_WRITE_SECRET', 64)):
        assert first['root'][name] != second['root'][name], f'{name} repeated across two generations'
        for values in (first, second):
            assert len(values['root'][name]) == length and int(values['root'][name], 16) >= 0
            assert values['root'][name] != committed.get(name)
            assert name not in values['store'] and name not in values['ingest']


def test_the_agent_stack_names_are_its_own_and_in_the_root_file_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """(E), the F1 rule: DATABASE_NAME, BROKER_NAME, STORE_API_NETWORK distinct from the committed ones, root only."""
    _, _, values = _generate(tmp_path, monkeypatch)
    committed = stack.parse_env_text(committed_defaults()['.env.default'])
    user_names = {'DATABASE_NAME': committed['DATABASE_NAME'], 'BROKER_NAME': committed['BROKER_NAME']}
    user_names['STORE_API_NETWORK'] = 'trader_joe_store_api'
    for name, user_value in user_names.items():
        assert values['root'][name] and values['root'][name] != user_value, (name, values['root'][name])
    for kind in ('store', 'ingest'):
        leaked = set(stack.ROOT_ONLY_VARIABLES) & set(values[kind])
        assert not leaked, f'{FILE_NAMES[kind]} sets {leaked}, which belong in the root file only'


def test_existing_files_are_reused_and_a_missing_service_file_is_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Postgres keeps the password it was initialised with: the root file is never regenerated while it exists."""
    layout, _, first = _generate(tmp_path, monkeypatch)
    (layout.stack_dir / FILE_NAMES['ingest']).unlink()
    stack.ensure_env_files(layout.settings)
    root = stack.parse_env_text((layout.stack_dir / FILE_NAMES['root']).read_text())
    assert root['POSTGRES_PASS'] == first['root']['POSTGRES_PASS']
    assert (layout.stack_dir / FILE_NAMES['ingest']).exists()


def test_no_live_env_file_is_ever_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Body bullet 5: the committed defaults come through git cat-file; the user's env files are never opened.

    Live env files holding a sentinel are planted in the main checkout and the agent worktree, and every
    open route -- builtins.open, io.open, os.open (dir_fd-relative too), Path.open and Path.read_text --
    is watched through a full stack_up. None may touch a live env file, and the sentinel may reach no
    generated file, no snapshot file, no response and no audit line.
    """
    rig = make_rig(tmp_path, monkeypatch)
    planted = plant_live_env_files(rig.layout.repo) + plant_live_env_files(rig.layout.worktree)
    opened: list[str] = []

    def live(name: object) -> bool:
        base = os.path.basename(os.fsdecode(name)) if isinstance(name, (str, bytes, os.PathLike)) else ''
        return base == '.env' or (base.startswith('.env.') and base != '.env.default')

    real_open, real_io_open, real_os_open = builtins.open, io.open, os.open
    real_path_open, real_read_text = Path.open, Path.read_text

    def spy_open(file, *args, **kwargs):
        if live(file):
            opened.append(f'open {file}')
        return real_open(file, *args, **kwargs)

    def spy_os_open(path, *args, **kwargs):
        if live(path):
            opened.append(f'os.open {path}')
        return real_os_open(path, *args, **kwargs)

    def spy_path_open(self, *args, **kwargs):
        if live(self):
            opened.append(f'Path.open {self}')
        return real_path_open(self, *args, **kwargs)

    def spy_read_text(self, *args, **kwargs):
        if live(self):
            opened.append(f'Path.read_text {self}')
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(builtins, 'open', spy_open)
    monkeypatch.setattr(
        io, 'open', lambda file, *a, **k: spy_open(file, *a, **k) if live(file) else real_io_open(file, *a, **k)
    )
    monkeypatch.setattr(os, 'open', spy_os_open)
    monkeypatch.setattr(Path, 'open', spy_path_open)
    monkeypatch.setattr(Path, 'read_text', spy_read_text)
    for worktree in ('root', WORKTREE_NAME):
        result = rig.call('stack_up', {'worktree': worktree})
        assert result['status'] == 'ok', result
        snapshot_text = ''.join(
            path.read_bytes().decode(errors='replace') for path in rig.layout.snapshot.rglob('*') if path.is_file()
        )
        assert LIVE_ENV_SENTINEL not in snapshot_text, f'a live env file reached the snapshot of {worktree}'
        assert LIVE_ENV_SENTINEL not in repr(result)
    assert opened == [], f'the server opened live env files: {opened}'
    assert all(path.exists() for path in planted)
    for name in (*FILE_NAMES.values(), stack.AUDIT_LOG_NAME):
        assert LIVE_ENV_SENTINEL not in real_read_text(rig.layout.stack_dir / name)
    assert (rig.layout.snapshot / 'common' / '.env.default').exists(), 'the committed template is part of the source'


# --- GUARD 2: every verb refuses before its first docker step --------------------------------------


def _guarded_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = make_rig(tmp_path, monkeypatch)
    assert rig.call('ps', {})['status'] == 'ok'
    rig.docker.steps.clear()
    return rig


def _edit(path: Path, **changes: str | None) -> None:
    lines = [line for line in path.read_text().splitlines() if line.split('=', 1)[0] not in changes]
    lines += [f'{key}={value}' for key, value in changes.items() if value is not None]
    path.write_text('\n'.join(lines) + '\n')


def _assert_refused(rig, match: str) -> None:
    for verb, arguments in (('ps', {}), ('logs', {'service': 'kafka'}), ('stack_down', {})):
        result = rig.call(verb, arguments)
        assert result['status'] == 'refused', result
        assert match in result['message'], result['message']
    assert rig.docker.steps == [], f'docker ran after a guard-2 refusal: {rig.docker.steps}'


_ROOT_EDITS = {
    'ROOT_ENV_FILE relative': ({'ROOT_ENV_FILE': 'agent_stack.env'}, 'ROOT_ENV_FILE must be set to an absolute path'),
    'ROOT_ENV_FILE empty': ({'ROOT_ENV_FILE': ''}, 'ROOT_ENV_FILE must be set'),
    'ROOT_ENV_FILE unset': ({'ROOT_ENV_FILE': None}, 'ROOT_ENV_FILE must be set'),
    'STORE_ENV_FILE names the ingest file': (
        {'STORE_ENV_FILE': '{stack}/agent_stack_ingest.env'},
        'STORE_ENV_FILE must name',
    ),
    "INGEST_ENV_FILE is the user's": ({'INGEST_ENV_FILE': '{repo}/data/ingest/.env'}, 'INGEST_ENV_FILE must name'),
    'ROOT_ENV_FILE outside the stack dir': ({'ROOT_ENV_FILE': '{root}/agent_stack.env'}, 'ROOT_ENV_FILE must name'),
    'DATA_DIR elsewhere': ({'DATA_DIR': '{repo}/volumes'}, 'DATA_DIR'),
    "DATABASE_NAME the user's": ({'DATABASE_NAME': 'db'}, 'DATABASE_NAME'),
    "BROKER_NAME the user's": ({'BROKER_NAME': 'kafka'}, 'BROKER_NAME'),
    "STORE_API_NETWORK the user's": ({'STORE_API_NETWORK': 'trader_joe_store_api'}, 'STORE_API_NETWORK'),
    'a broker credential in the root file': ({'ALPACA_API_KEY': ''}, 'broker credential'),
}


@pytest.mark.parametrize(('changes', 'match'), list(_ROOT_EDITS.values()), ids=list(_ROOT_EDITS))
def test_guard_2_refuses_an_edited_root_file_before_any_docker_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changes: dict, match: str
):
    rig = _guarded_rig(tmp_path, monkeypatch)
    layout = rig.layout
    spelled = {
        key: None if value is None else value.format(stack=layout.stack_dir, repo=layout.repo, root=layout.root)
        for key, value in changes.items()
    }
    (layout.root / 'agent_stack.env').write_text((layout.stack_dir / 'agent_stack.env').read_text())
    _edit(layout.stack_dir / 'agent_stack.env', **spelled)
    _assert_refused(rig, match)


_SERVICE_EDITS = {
    'store file sets DATABASE_NAME (F1)': (
        'store',
        {'DATABASE_NAME': 'trader_joe_agent_stack_postgres'},
        'root file only',
    ),
    'ingest file sets STORE_API_NETWORK (F1)': ('ingest', {'STORE_API_NETWORK': 'x'}, 'root file only'),
    'store file sets a broker credential': ('store', {'ALPACA_API_SECRET': ''}, 'broker credential'),
    'ingest file carries a key': ('ingest', {'ALPACA_API_KEY': 'PKLIVE'}, 'empty'),
    'ingest file lacks the secret': ('ingest', {'ALPACA_API_SECRET': None}, 'empty'),
}


@pytest.mark.parametrize(('kind', 'changes', 'match'), list(_SERVICE_EDITS.values()), ids=list(_SERVICE_EDITS))
def test_guard_2_refuses_an_edited_service_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, changes: dict, match: str
):
    rig = _guarded_rig(tmp_path, monkeypatch)
    _edit(rig.layout.stack_dir / FILE_NAMES[kind], **changes)
    _assert_refused(rig, match)


@pytest.mark.parametrize('kind', KINDS)
def test_guard_2_refuses_a_symlinked_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str):
    rig = _guarded_rig(tmp_path, monkeypatch)
    path = rig.layout.stack_dir / FILE_NAMES[kind]
    moved = rig.layout.root / f'moved_{kind}.env'
    path.rename(moved)
    path.symlink_to(moved)
    _assert_refused(rig, f'{FILE_NAMES[kind]} is missing, or is not a regular file')


def test_guard_2_refuses_env_files_under_a_worktree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Addendum-2 pin (3): even the stack's own files are refused when a worktree git knows lies above them."""
    rig = make_rig(tmp_path, monkeypatch)
    rig.git.worktree_paths.append(rig.layout.root)
    result = rig.call('ps', {})
    assert result['status'] == 'refused' and 'lies under a worktree' in result['message'], result
    assert rig.docker.steps == []


_STEERING_SPELLINGS = {
    'plain COMPOSE_FILE': 'COMPOSE_FILE=/elsewhere/compose.yaml',
    'export': 'export COMPOSE_FILE=/elsewhere/compose.yaml',
    'spaces around =': 'COMPOSE_FILE = /elsewhere/compose.yaml',
    'colon': 'COMPOSE_FILE: /elsewhere/compose.yaml',
    'COMPOSE_PROJECT_NAME': 'COMPOSE_PROJECT_NAME=trader_joe',
    'a DOCKER key': 'DOCKER_HOST=unix:///var/run/docker.sock',
    'DOCKER_CONFIG with export': 'export DOCKER_CONFIG=/elsewhere',
}


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('line', list(_STEERING_SPELLINGS.values()), ids=list(_STEERING_SPELLINGS))
def test_guard_2_refuses_a_steering_key_in_any_spelling_compose_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, line: str
):
    """(E) and G6: compose reads COMPOSE_* from --env-file in looser spellings than KEY=VALUE."""
    rig = _guarded_rig(tmp_path, monkeypatch)
    path = rig.layout.stack_dir / FILE_NAMES[kind]
    path.write_text(path.read_text() + line + '\n')
    _assert_refused(rig, 'would steer compose')


@pytest.mark.parametrize('source', ['.env.default', 'data/store/.env.default', 'data/ingest/.env.default'])
@pytest.mark.parametrize('key', ['COMPOSE_FILE', 'COMPOSE_PROFILES', 'DOCKER_HOST'])
def test_a_committed_default_with_a_steering_key_is_refused_at_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str, key: str
):
    """(E): refused, never dropped -- dropping would hide a committed change that tries to steer compose."""
    rig = make_rig(tmp_path, monkeypatch)
    rig.git.defaults[source] += f'\n{key}=steered\n'
    result = rig.call('ps', {})
    assert result['status'] == 'refused' and 'would steer compose' in result['message'] and source in result['message']
    assert rig.docker.steps == []
    assert not any((rig.layout.stack_dir / name).exists() for name in FILE_NAMES.values()), (
        'a refused generation wrote files'
    )
