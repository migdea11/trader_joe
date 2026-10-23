"""tools/dev_instance_secret.sh: the dev web container's instance secret (bead tj-grna9p.133, round two).

The Makefile's DEV_COMPOSE runs it in the recipe's shell: the shell's INSTANCE_WRITE_SECRET if non-empty,
else the LAST INSTANCE_WRITE_SECRET= line of the env file (./.env, or $1), else the literal "unused".
Its .env reading is modelled on compose's own parser; what it does NOT mirror (${VAR} interpolation,
quoted values spanning lines) is not pinned here.

NEVER THE REAL SECRET. Every run gets a SCRUBBED environment (PATH only, plus what the case sets) and a
temporary directory with a FAKE env file, so neither the agent's own INSTANCE_WRITE_SECRET nor a
checkout's root .env can reach the helper, an assertion message or a log. Every value below is fake.
The helper is run as the Makefile runs it: `sh tools/dev_instance_secret.sh`, from the project directory.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

from common.tests.roots import REPO_ROOT


pytestmark = pytest.mark.build_infra

HELPER = REPO_ROOT / 'tools' / 'dev_instance_secret.sh'
SECRET = 'INSTANCE_WRITE_SECRET'
_CLEAN_ENV = {'PATH': os.environ.get('PATH', '/usr/bin:/bin')}


def _resolve(tmp_path: Path, env_file: str | None, shell: dict[str, str] | None = None, *args: str) -> str:
    """The helper's stdout, run by sh in TMP_PATH, ENV_FILE written there as .env byte for byte if given."""
    if env_file is not None:
        (tmp_path / '.env').write_bytes(env_file.encode('utf-8'))
    assert SECRET not in _CLEAN_ENV
    result = subprocess.run(
        ['sh', str(HELPER), *args], cwd=tmp_path, env=_CLEAN_ENV | (shell or {}), capture_output=True, check=True
    )
    assert result.stderr == b'', result.stderr
    return result.stdout.decode('utf-8')


def test_the_scrub_is_real(tmp_path: Path):
    """Guard the guard: the child sees no INSTANCE_WRITE_SECRET unless a case sets one."""
    result = subprocess.run(['sh', '-c', 'echo "${INSTANCE_WRITE_SECRET-UNSET}"'], env=_CLEAN_ENV, capture_output=True)
    assert result.stdout == b'UNSET\n'


@pytest.mark.parametrize(
    ('env_file', 'expected'),
    [
        (None, 'unused'),
        ('', 'unused'),
        ('OTHER=fake-other\n', 'unused'),
        (f'{SECRET}=fake-plain\n', 'fake-plain'),
        (f'{SECRET}=fake-no-newline', 'fake-no-newline'),
        (f'{SECRET}=fake-crlf\r\nOTHER=x\r\n', 'fake-crlf'),
        (f'export {SECRET}=fake-exported\n', 'fake-exported'),
        (f'   {SECRET}=fake-indented\n', 'fake-indented'),
        (f'{SECRET} = fake-spaced\n', 'fake-spaced'),
        (f'{SECRET}=fake-trailing-blanks   \t\n', 'fake-trailing-blanks'),
        (f'{SECRET}=\'fake-single $HOME #x \\n "q"\' # comment\n', 'fake-single $HOME #x \\n "q"'),
        (f'{SECRET}="fake\\\\back\\"quote\\$dollar\\nline\\rcr" # comment\n', 'fake\\back"quote$dollar\nline\rcr'),
        (f'{SECRET}="fake # not a comment"\n', 'fake # not a comment'),
        (f'{SECRET}=fake-value # an inline comment\n', 'fake-value'),
        (f'{SECRET}=fake-value\t# a tab-led comment\n', 'fake-value'),
        (f'{SECRET}=fake#hash\n', 'fake#hash'),
        (f'{SECRET}=fake$dollar#hash\n', 'fake$dollar#hash'),
        (f'{SECRET}=fake=equals"dq\'sq\n', 'fake=equals"dq\'sq'),
        (f'{SECRET}=fake with inner spaces\n', 'fake with inner spaces'),
        (f'{SECRET}=-n\n', '-n'),
        (f'{SECRET}=-e fake\\nnot-a-newline\n', '-e fake\\nnot-a-newline'),
        (f'{SECRET}=#starts-with-hash\n', 'unused'),
        (f'# {SECRET}=fake-commented-out\n', 'unused'),
        (f'#{SECRET}=fake-commented-out\n', 'unused'),
        (f'{SECRET}_OLD=fake-other-key\n', 'unused'),
        (f'X{SECRET}=fake-other-key\n', 'unused'),
        (f'{SECRET}=fake-first\n{SECRET}=fake-last\n', 'fake-last'),
        (f'{SECRET}=fake-first\n{SECRET}=\n', 'unused'),
        (f'{SECRET}=fake-first\n{SECRET}=""\n', 'unused'),
        (f'{SECRET}=fake-first\n# {SECRET}=fake-commented\nOTHER=x\n', 'fake-first'),
    ],
    ids=[
        'missing-file',
        'empty-file',
        'no-such-line',
        'plain',
        'no-final-newline',
        'crlf',
        'export',
        'leading-whitespace',
        'spaces-around-equals',
        'trailing-whitespace-trimmed',
        'single-quotes-literal',
        'double-quotes-unescape',
        'hash-inside-double-quotes',
        'inline-comment',
        'inline-comment-after-tab',
        'hash-without-whitespace-kept',
        'dollar-and-hash-kept',
        'equals-and-quotes-kept',
        'inner-spaces-kept',
        'leading-dash',
        'dash-e-and-backslash-n-literal',
        'value-that-is-a-comment',
        'commented-line',
        'commented-line-no-space',
        'longer-key',
        'prefixed-key',
        'last-line-wins',
        'empty-last-line-wins',
        'empty-quoted-last-line-wins',
        'comment-after-does-not-count',
    ],
)
def test_the_env_file_value_with_no_shell_value(tmp_path: Path, env_file: str | None, expected: str):
    assert _resolve(tmp_path, env_file) == expected


@pytest.mark.parametrize(
    'shell_value',
    ['fake-from-shell', '-n', '-e fake\\n', 'fake $dollar #hash "q"'],
    ids=['plain', 'dash-n', 'dash-e', 'specials'],
)
def test_a_non_empty_shell_value_wins_and_is_printed_exactly(tmp_path: Path, shell_value: str):
    """Printf '%s', never echo: a leading dash or a backslash is printed as it is, with no newline added."""
    assert _resolve(tmp_path, f'{SECRET}=fake-from-file\n', {SECRET: shell_value}) == shell_value


def test_an_empty_shell_value_falls_through_to_the_env_file(tmp_path: Path):
    assert _resolve(tmp_path, f'{SECRET}=fake-from-file\n', {SECRET: ''}) == 'fake-from-file'


def test_the_first_argument_names_another_env_file(tmp_path: Path):
    """$1 overrides ./.env; the default file is then not read."""
    (tmp_path / 'other.env').write_text(f'{SECRET}=fake-from-other\n', encoding='utf-8')
    assert _resolve(tmp_path, f'{SECRET}=fake-from-default\n', None, 'other.env') == 'fake-from-other'
    assert _resolve(tmp_path, f'{SECRET}=fake-from-default\n', None, 'missing.env') == 'unused'


def test_an_unreadable_env_file_falls_back_rather_than_failing(tmp_path: Path):
    """A directory where the file should be: exit 0 and the placeholder on stdout.

    awk reports the read error on stderr (the helper tests `-r`, which a directory passes); that names
    the file, never a value, and is a non-blocking finding on tj-grna9p.133, not pinned here.
    """
    (tmp_path / '.env').mkdir()
    result = subprocess.run(['sh', str(HELPER)], cwd=tmp_path, env=_CLEAN_ENV, capture_output=True, check=True)
    assert result.stdout == b'unused'


def test_the_helper_prints_nothing_but_the_value():
    """No echo, set -x or other stdout/stderr writer in the script besides the printf of the result."""
    code = [
        line.strip() for line in HELPER.read_text(encoding='utf-8').splitlines() if not line.strip().startswith('#')
    ]
    assert not [line for line in code if re.match(r'(echo|printenv|env|set -x|set -o xtrace)\b(?!_)', line)], code
    printers = [line for line in code if line.startswith('printf')]
    assert printers == ['printf \'%s\' "$INSTANCE_WRITE_SECRET"', 'printf \'%s\' "$value"', "printf '%s' unused"], (
        printers
    )
