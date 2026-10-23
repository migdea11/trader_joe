"""build_infra pins for the database rail's marker: cluster_name is dev in dev, test in test mode, absent in prod.

tj-grna9p.194 (the owner's rule that seed data never reaches prod; the architect's option 3). tools/dev_datasets reads
the pg_settings row of the built-in cluster_name on every connection it opens and refuses unless its source is
'command line' and its value is dev or test and equals the store's label. That is only a rail if the value reaches the
server's command line where it should and nowhere else: the dev override puts -c cluster_name=dev on postgres's
command, the fake overlay (test mode) -c cluster_name=test, and docker-compose.yaml and every file of the prod set
(docker-compose.web.yaml included) never mention it, so a prod server answers its default and is refused.

Docker-free, like every build_infra pin: the compose files are parsed with yaml.safe_load and merged by compose_model
(never grepped for the marker's placement), the Makefile's sets are expanded by make itself, and every launch set is
the one test_service_pythonpath reads from the four places a set is spelled. The setting's name is read from the tool,
so a rename on either side goes red here.
"""

import shlex
from collections.abc import Iterator
from pathlib import Path

import pytest

from common.tests.compose_model import BASE_FILE, FAKE_FILE, load, merge
from common.tests.test_ci_invariants import REPO_ROOT, _compose_calls, _expanded_make_variable, _subprocess_env
from common.tests.test_service_pythonpath import _launch_sets
from tools.dev_datasets import request as tool


pytestmark = pytest.mark.build_infra

OVERRIDE_FILE = REPO_ROOT / 'docker-compose.override.yaml'
# The design's values, spelled out (the name is checked against the tool's below).
DESIGN_SETTING = 'cluster_name'
DESIGN_SOURCE = 'command line'
DEV_COMMAND = ['postgres', '-c', 'cluster_name=dev']
TEST_COMMAND = ['postgres', '-c', 'cluster_name=test']
WEB_FILE = REPO_ROOT / 'docker-compose.web.yaml'


def _strings(node: object) -> Iterator[str]:
    """Every key and string value of a parsed compose document."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield str(key)
            yield from _strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _strings(item)


def _prod_files() -> list[Path]:
    [(files, _)] = _compose_calls(_expanded_make_variable('PROD_COMPOSE', REPO_ROOT, _subprocess_env()))
    return [REPO_ROOT / name for name in files]


def _marker(command: object) -> dict[str, str]:
    """The -c name=value options of a postgres command (list or string form), by name."""
    words = shlex.split(command) if isinstance(command, str) else [str(word) for word in command or []]
    options = {}
    for index, word in enumerate(words):
        if word == '-c' and index + 1 < len(words):
            name, _, value = words[index + 1].partition('=')
            options[name] = value
        elif word.startswith('-c') and len(word) > 2:
            name, _, value = word[2:].partition('=')
            options[name] = value
    return options


def _stack_of(files: tuple[Path, ...]) -> str | None:
    postgres = merge([load(path) for path in files])['services'].get('postgres') or {}
    return _marker(postgres.get('command')).get(DESIGN_SETTING)


# ---- the name is the tool's ----------------------------------------------------------------------------------------


def test_the_setting_the_compose_files_set_is_the_one_the_tool_reads():
    assert tool.STACK_SETTING == DESIGN_SETTING
    assert (
        ' '.join(tool.STACK_SQL.split()) == f"SELECT setting, source FROM pg_settings WHERE name = '{DESIGN_SETTING}'"
    )
    # 'command line' is the source Postgres reports for a -c option on the server's command line, which is exactly
    # how the two files pass it (a command word, never an environment variable or a configuration file).
    assert tool.STACK_SOURCE == DESIGN_SOURCE
    for path in (OVERRIDE_FILE, FAKE_FILE):
        postgres = load(path)['services']['postgres']
        assert list(_marker(postgres['command'])) == [tool.STACK_SETTING], (path.name, postgres['command'])
        assert not [text for text in _strings(postgres.get('environment') or {}) if DESIGN_SETTING in text], path.name


# ---- prod never carries it -----------------------------------------------------------------------------------------


def test_the_prod_set_is_the_base_file_and_the_web_file():
    # So the pin below covers what prod loads; a new prod file joins it by joining PROD_COMPOSE.
    names = [path.name for path in _prod_files()]
    assert BASE_FILE.name in names and WEB_FILE.name in names, names
    assert OVERRIDE_FILE.name not in names and FAKE_FILE.name not in names, names


def test_no_prod_file_mentions_the_setting():
    files = {BASE_FILE, WEB_FILE, *_prod_files()}
    assert len(files) >= 2, files
    for path in sorted(files):
        parsed = load(path)
        assert not [text for text in _strings(parsed) if DESIGN_SETTING in text], path.name
        # Not even a comment: a prod file that talks about the marker is one edit from setting it.
        assert DESIGN_SETTING not in path.read_text(encoding='utf-8'), path.name


def test_the_merged_prod_postgres_leaves_the_command_to_the_image():
    postgres = merge([load(path) for path in _prod_files()])['services']['postgres']
    assert 'command' not in postgres and 'entrypoint' not in postgres, postgres
    assert _stack_of(tuple(_prod_files())) is None


# ---- dev carries dev, test mode carries test, exactly ---------------------------------------------------------------


def test_the_dev_override_starts_postgres_with_the_dev_marker():
    postgres = load(OVERRIDE_FILE)['services']['postgres']
    # The image's default command restated (a command: key replaces it), then the one option.
    assert postgres['command'] == DEV_COMMAND, postgres['command']
    assert 'entrypoint' not in postgres


def test_the_fake_overlay_starts_postgres_with_the_test_marker():
    postgres = load(FAKE_FILE)['services']['postgres']
    assert postgres == {'command': TEST_COMMAND}, postgres


def test_the_base_postgres_sets_no_command_for_the_markers_to_drop():
    # Overriding command: replaces the base's; there is none, so the image's CMD (postgres) is all it restates.
    postgres = load(BASE_FILE)['services']['postgres']
    assert 'command' not in postgres and 'entrypoint' not in postgres, postgres
    assert str(postgres['image']).startswith('postgres:'), postgres['image']


def test_every_launch_set_marks_its_postgres_by_its_mode():
    """Dev for a set with the override, test for a set with the fake overlay, nothing for any other (prod's)."""
    sets = _launch_sets()
    assert len(sets) >= 10, f'found only {sorted(sets)}; a parsing slip must not empty this check'
    seen = set()
    for label, files in sets.items():
        names = {path.name for path in files}
        assert not {OVERRIDE_FILE.name, FAKE_FILE.name} <= names, label
        if 'postgres' not in (merge([load(path) for path in files])['services']):
            continue
        expected = 'dev' if OVERRIDE_FILE.name in names else 'test' if FAKE_FILE.name in names else None
        assert _stack_of(files) == expected, (label, _stack_of(files))
        seen.add(expected)
    assert seen == {'dev', 'test', None}, seen
