"""No verb opens a live env file (ADR tj-4rr0la D5; addendum 28: the MCP never reads the env file).

Moved from test_env_files.py when the agent stack's generated env files were removed (tj-iee6sn.21). That
pin drove stack_up only; the property belongs to the server, so it now drives every registered verb. The
D5 preflight may lstat the root env file, and up's wrapper reads it in the CHILD, which the fake docker
never starts: neither is an open by this process.
"""

import builtins
import io
import os
from pathlib import Path

import pytest

from tools.agent_mcp import stack
from tools.agent_mcp.tests.harness import LIVE_ENV_SENTINEL, VERB_SAMPLES, make_rig, plant_live_env_files, response_for


def _live(name: object) -> bool:
    base = os.path.basename(os.fsdecode(name)) if isinstance(name, (str, bytes, os.PathLike)) else ''
    return base == '.env' or (base.startswith('.env.') and base != '.env.default')


@pytest.mark.parametrize('verb', sorted(VERB_SAMPLES))
def test_no_verb_opens_a_live_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str):
    """Every open route is watched through an admitted call of VERB; none may touch a live env file."""
    rig = make_rig(tmp_path, monkeypatch)
    rig.docker.respond = response_for(verb, rig.layout)
    planted = plant_live_env_files(rig.layout.repo) + plant_live_env_files(rig.layout.worktree)
    opened: list[str] = []
    real_open, real_io_open, real_os_open = builtins.open, io.open, os.open
    real_path_open, real_read_text = Path.open, Path.read_text

    def spy_open(file, *args, **kwargs):
        if _live(file):
            opened.append(f'open {file}')
        return real_open(file, *args, **kwargs)

    def spy_os_open(path, *args, **kwargs):
        if _live(path):
            opened.append(f'os.open {path}')
        return real_os_open(path, *args, **kwargs)

    def spy_path_open(self, *args, **kwargs):
        if _live(self):
            opened.append(f'Path.open {self}')
        return real_path_open(self, *args, **kwargs)

    def spy_read_text(self, *args, **kwargs):
        if _live(self):
            opened.append(f'Path.read_text {self}')
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(builtins, 'open', spy_open)
    monkeypatch.setattr(
        io, 'open', lambda file, *a, **k: spy_open(file, *a, **k) if _live(file) else real_io_open(file, *a, **k)
    )
    monkeypatch.setattr(os, 'open', spy_os_open)
    monkeypatch.setattr(Path, 'open', spy_path_open)
    monkeypatch.setattr(Path, 'read_text', spy_read_text)

    result = rig.call(verb, VERB_SAMPLES[verb])

    assert result['status'] == 'ok', result
    assert opened == [], f'{verb} opened live env files: {opened}'
    assert all(path.exists() for path in planted)
    assert LIVE_ENV_SENTINEL not in repr(result)
    assert LIVE_ENV_SENTINEL not in real_read_text(rig.layout.stack_dir / stack.AUDIT_LOG_NAME)
    if rig.layout.snapshot.exists():
        for path in rig.layout.snapshot.rglob('*'):
            if path.is_file():
                assert LIVE_ENV_SENTINEL.encode() not in path.read_bytes(), f'{path} carries a live env value'
