"""stack_wipe: the agent stack's data directory and nothing else.

tj-c4mosr.5 body bullet 3 (only the configured data directory; a symlinked or mismatched path refused
and nothing removed), (4)/W (a service directory the server cannot read is still removed once
emptied; one left non-empty is 'failed' naming it, never 'error'), T3 (is_plain_dir by lstat; a
data/<service> symlink gets no clear step and is reported, its target untouched) and the 02:14 (4)
extras. Design: ADR tj-4rr0la section 3 and addendum 6 (D5, INFO; L-series via tj-c4mosr.10).
"""

import dataclasses
import os
from pathlib import Path

import pytest

from tools.agent_mcp import stack
from tools.agent_mcp.tests.harness import clearing_hook, make_rig, populate_data, step_prefix_length, tree_digest


pytestmark = pytest.mark.build_infra


def _wipe_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, clear: bool = True):
    rig = make_rig(tmp_path, monkeypatch)
    assert rig.call('ps', {})['status'] == 'ok'  # generates the env files and an empty snapshot
    populate_data(rig.layout)
    if clear:
        rig.docker.hook = clearing_hook(rig.layout)
    rig.docker.steps.clear()
    return rig


def _bystanders(rig) -> dict[str, dict[str, str]]:
    """Everything a wipe must leave alone: the stack directory less data/, the repository, a sibling."""
    sibling = rig.layout.root / 'sibling_data'
    sibling.mkdir(exist_ok=True)
    (sibling / 'keep').write_text('keep\n')
    stack_rest = {k: v for k, v in tree_digest(rig.layout.stack_dir).items() if not k.startswith('data')}
    return {'stack': stack_rest, 'repo': tree_digest(rig.layout.repo), 'sibling': tree_digest(sibling)}


def _bystanders_after(rig) -> dict[str, dict[str, str]]:
    stack_rest = {k: v for k, v in tree_digest(rig.layout.stack_dir).items() if not k.startswith('data')}
    stack_rest.pop(stack.AUDIT_LOG_NAME, None)
    return {
        'stack': stack_rest,
        'repo': tree_digest(rig.layout.repo),
        'sibling': tree_digest(rig.layout.root / 'sibling_data'),
    }


def test_wipe_removes_the_data_directory_and_nothing_else(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _wipe_rig(tmp_path, monkeypatch)
    before = _bystanders(rig)
    before['stack'].pop(stack.AUDIT_LOG_NAME, None)
    result = rig.call('stack_wipe', {})
    assert result['status'] == 'ok' and 'deleted' in result['message'], result
    assert not os.path.lexists(rig.layout.data_dir)
    assert _bystanders_after(rig) == before
    tails = [step.argv[step_prefix_length()] for step in rig.docker.steps]
    # One `run` per DATA_MOUNTS entry, and `down` before all of them. Spelled from DATA_MOUNTS
    # rather than counted by hand so removing a data mount (tj-3mk3u5.15 removed kafka's) cannot
    # leave this asserting a count nothing produces.
    assert tails == ['down', *['run'] * len(stack.DATA_MOUNTS)], 'the stack must be down before its data is cleared'


def test_wipe_with_no_data_directory_is_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = make_rig(tmp_path, monkeypatch)
    result = rig.call('stack_wipe', {})
    assert result['status'] == 'ok' and 'no data directory' in result['message']


def test_a_symlinked_data_directory_is_refused_and_nothing_is_stopped_or_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig = _wipe_rig(tmp_path, monkeypatch)
    elsewhere = rig.layout.root / 'precious'
    rig.layout.data_dir.rename(elsewhere)
    rig.layout.data_dir.symlink_to(elsewhere, target_is_directory=True)
    before = tree_digest(elsewhere)
    result = rig.call('stack_wipe', {})
    assert result['status'] == 'refused' and 'symlink' in result['message'], result
    assert rig.docker.steps == [], 'the stack was stopped for a refused wipe'
    assert tree_digest(elsewhere) == before and rig.layout.data_dir.is_symlink()


def test_a_data_path_that_resolves_elsewhere_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A symlink ABOVE data/ (the configured stack path is itself a link) is a mismatch: refused."""
    rig = _wipe_rig(tmp_path, monkeypatch)
    link = rig.layout.root / 'stack_link'
    link.symlink_to(rig.layout.stack_dir, target_is_directory=True)
    rig.agent.settings = dataclasses.replace(rig.layout.settings, stack_dir=link)
    before = tree_digest(rig.layout.data_dir)
    result = rig.call('stack_wipe', {})
    assert result['status'] == 'refused' and 'does not resolve to its configured path' in result['message'], result
    assert rig.docker.steps == [] and tree_digest(rig.layout.data_dir) == before


def test_a_data_path_that_is_a_file_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = make_rig(tmp_path, monkeypatch)
    rig.layout.data_dir.write_text('not a directory\n')
    result = rig.call('stack_wipe', {})
    assert result['status'] == 'refused' and rig.docker.steps == []
    assert rig.layout.data_dir.read_text() == 'not a directory\n'


def test_an_emptied_service_directory_the_server_cannot_read_is_still_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """W / (4): Postgres leaves PGDATA 999:0700; mode 000 on a directory we own reproduces that for us."""
    rig = _wipe_rig(tmp_path, monkeypatch)
    postgres = rig.layout.data_dir / 'postgres'
    real_hook = rig.docker.hook

    def clear_then_lock(step: stack.Step) -> None:
        real_hook(step)
        if 'run' in step.argv and 'postgres' in step.argv:
            postgres.chmod(0o000)

    rig.docker.hook = clear_then_lock
    try:
        result = rig.call('stack_wipe', {})
    finally:
        if postgres.exists():
            postgres.chmod(0o700)
    assert result['status'] == 'ok', result
    assert not os.path.lexists(rig.layout.data_dir)


def test_a_service_directory_left_non_empty_is_failed_naming_it_and_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """W: 'failed', never 'error', and the data kept."""
    rig = _wipe_rig(tmp_path, monkeypatch, clear=False)
    before = tree_digest(rig.layout.data_dir)
    result = rig.call('stack_wipe', {})
    assert result['status'] == 'failed' and 'data/postgres' in result['message']
    assert tree_digest(rig.layout.data_dir) == before


def test_a_service_symlink_gets_no_clear_step_and_is_reported_with_its_target_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """T3 / 02:14 (4): the daemon would follow data/postgres as root, so it is never cleared through."""
    rig = _wipe_rig(tmp_path, monkeypatch)
    target = rig.layout.root / 'not_the_stack'
    (rig.layout.data_dir / 'postgres').rename(target)
    (rig.layout.data_dir / 'postgres').symlink_to(target, target_is_directory=True)
    before = tree_digest(target)
    result = rig.call('stack_wipe', {})
    assert result['status'] == 'failed' and 'data/postgres' in result['message'], result
    cleared = [step.argv[step_prefix_length() + 7] for step in rig.docker.steps if 'run' in step.argv]
    # Postgres is the only data mount since tj-3mk3u5.15 removed kafka's, so this is an empty list
    # rather than "every other service was still cleared". The test below restores the control that
    # went with the second mount; this one keeps the property as the REAL inventory produces it.
    assert cleared == [], f'clear steps ran for {cleared}'
    assert tree_digest(target) == before and (rig.layout.data_dir / 'postgres').is_symlink()


def test_a_service_symlink_is_skipped_while_another_service_is_still_cleared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The positive control the test above lost on tj-3mk3u5.15: the skip is TARGETED.

    With postgres the only data mount, "no clear step ran" and "the skip is a blanket failure that
    cleared nothing" produce the identical empty list, so the test above can no longer tell them
    apart. That control went with kafka's mount, and no second real mount is coming back.

    SO THE SECOND MOUNT HERE IS SYNTHETIC, and that is legitimate rather than a dodge, because the
    property under test is wipe's own LOGIC -- does it skip exactly the symlinked entry? -- and not
    which services the overlay happens to declare. The real inventory is pinned separately and by
    equality, in test_commands.py: DATA_MOUNTS must match the overlay's mounts, and must be exactly
    {'postgres'}. If a second real mount ever returns, that test reds and this one keeps working.

    populate_data and clearing_hook both read stack.DATA_MOUNTS when _wipe_rig calls them, so
    patching the constant first gives a consistent two-mount world: both directories are created,
    and the hook can map either back from its container target.
    """
    monkeypatch.setattr(stack, 'DATA_MOUNTS', (*stack.DATA_MOUNTS, ('ledger', '/var/lib/ledger')))
    rig = _wipe_rig(tmp_path, monkeypatch)
    target = rig.layout.root / 'not_the_stack'
    (rig.layout.data_dir / 'postgres').rename(target)
    (rig.layout.data_dir / 'postgres').symlink_to(target, target_is_directory=True)

    result = rig.call('stack_wipe', {})

    assert result['status'] == 'failed' and 'data/postgres' in result['message'], result
    cleared = [step.argv[step_prefix_length() + 7] for step in rig.docker.steps if 'run' in step.argv]
    assert cleared == ['ledger'], (
        f'clear steps ran for {cleared}; the symlinked postgres must be skipped and the OTHER '
        'service still cleared, which is what distinguishes a targeted skip from clearing nothing'
    )


def test_a_stray_entry_in_data_is_failed_naming_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = _wipe_rig(tmp_path, monkeypatch)
    (rig.layout.data_dir / 'stray').write_text('who put this here\n')
    result = rig.call('stack_wipe', {})
    assert result['status'] == 'failed' and 'data/stray' in result['message'], result
    assert (rig.layout.data_dir / 'stray').exists()


def test_is_plain_dir_is_by_lstat(tmp_path: Path):
    """T3: only a real directory counts; a link to one, a dangling link, a file and nothing do not."""
    real = tmp_path / 'real'
    real.mkdir()
    (tmp_path / 'dirlink').symlink_to(real, target_is_directory=True)
    (tmp_path / 'dangling').symlink_to(tmp_path / 'gone')
    (tmp_path / 'file').write_text('x')
    assert stack.is_plain_dir(real) is True
    for name in ('dirlink', 'dangling', 'file', 'missing'):
        assert stack.is_plain_dir(tmp_path / name) is False, name
