"""Every target the Makefile defines is .PHONY, unless it is a file the Makefile makes (tj-3mk3u5.57).

tj-06uflo: a target that is not .PHONY is silenced by a file or directory of the same name. make calls it
up to date, runs nothing and exits 0, so `make test` with a test/ at the root reports success having run no
test. That fix declared every target of its day; per-target pins followed for a few of the targets added
since (test-system, system-launch, seed-dump, and test_make_lint.py's seven). This is the generic guard:
a target added tomorrow without .PHONY fails here, whichever bead adds it.

HOW THE TARGETS ARE READ: from GNU make's own database, never a regex over the Makefile's text. `make -p`
prints the database once the makefiles are read. -q (question mode) runs no recipe. -rR switches off the
built-in rules and variables, which add implicit rules and some seventy built-in suffix names (.c, .o,
.c.o, ...) to the dump but never a target, so what is left is what the Makefile defines. The goal is the
Makefile's own path, an existing file with no rule, so make is up to date at once whatever the default
goal is. It runs from a scratch directory, so nothing in the checkout can be
touched, with LC_ALL=C because make translates the dump's labels. The environment is also stripped of what
an outer make hands an inner one (under `make test`, MAKEFLAGS carries PATHS=...) and of MAKEFILES, which
would read in extra makefiles. The dump is what make itself resolved: every makefile it read, an included
one too, and .PHONY gathered across every .PHONY line with the variables in it expanded.

Its '# Files' section has one paragraph per name make knows. Not every name is a target:

* '# Not a target:' paragraphs are names with no rule: prerequisites only (pyproject.toml, uv.lock), the
  Makefile itself, make's own .DEFAULT and .SUFFIXES. With no rule there is nothing to declare.
* SPECIAL TARGETS (.PHONY itself and the rest of GNU make's built-in names, SPECIAL_TARGETS below) are
  directives, not commands. A dotted name outside that list is checked like any other target.
* PATTERN RULES never reach that section. make lists them under '# Implicit Rules', and a pattern names no
  file until a goal instantiates it. None exist today, and .PHONY cannot hold one anyway: `.PHONY: %.x`
  declares a literal file named '%.x'.

THE PROOF IS MEMBERSHIP: make's own phony flag on each target, '#  Phony target (prerequisite of .PHONY).'
It is not tj-06uflo's behavioural measurement (does a same-named directory turn the target into a no-op?),
because that measurement passes a non-phony target whose prerequisites are phony -- lint, lint-fix,
prod-launch, dev-launch, clean, agent-up among them. make remakes a target whenever a phony prerequisite is
remade, so a directory named lint/ does not silence an undeclared lint today. It would the day lint lost
its phony prerequisites, and the behavioural form would have missed the removal the whole time. Membership
is also the rule the Makefile states in its own header: every target except the two file targets is
declared .PHONY. The behaviour is still shown, once: the control test at the bottom of this file runs a
probe target each way on this Makefile, so the flag this file reads is the one that decides whether make
runs the recipe.

FILE TARGETS are FILE_TARGET_VARIABLES: an explicit allowlist by Makefile VARIABLE NAME, each expanded by
make in the same environment. It follows UV_PROJECT_ENVIRONMENT (.venv on the host, .venv-devcontainer in
the agent container) with no path spelled here. It cannot rot silently: every entry must still expand to a
target make knows, and that target must still not be phony. A renamed or deleted variable fails here
instead of being ignored, and a NEW file target fails the main test until someone adds it on purpose.

KNOWN LIMIT: the dump is the Makefile as parsed in this environment. A rule inside an ifeq or ifdef that is
false here is not seen. No rule is conditional today: the Makefile's conditionals wrap recipe lines and one
variable.

Not deleted, and named in the hand-off instead: the per-target .PHONY pins. Each belongs to another bead's
gate, and they pin layout this file does not, such as .PHONY directly above the recipe.
"""

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from common.tests.test_ci_invariants import MAKEFILE, _expanded_make_variable


pytestmark = pytest.mark.build_infra

MAKE_TIMEOUT_S = 60

# THE FILE TARGETS, by the Makefile variable that names each one. Every other target make knows is a command
# and must be .PHONY. An entry belongs here only when the target's name is a path the Makefile makes or
# checks, so that make comparing its timestamp is the point. Say which recipe makes it.
FILE_TARGET_VARIABLES = (
    # The sync marker, inside the environment. Its recipe syncs the environment and touches the marker, and
    # every target that runs uv depends on it. A phony marker would re-sync on every run (tj-3t2axg).
    'VENV_MARKER',
    # The environment's interpreter, a prerequisite of the marker. uv creates it during the marker's sync.
    # Its own recipe runs only when make finds it missing or dangling, and deletes the marker, forcing the
    # full sync before any `uv run` rebuilds the venv on its own terms (tj-3t2axg).
    'VENV_PYTHON',
)

# GNU make's special built-in target names (the manual's "Special Built-in Target Names", make 4.4). They are
# directives, so they carry no phony flag. Exact names only: any other dotted name is checked like a target.
SPECIAL_TARGETS = frozenset(
    {
        '.PHONY',
        '.SUFFIXES',
        '.DEFAULT',
        '.PRECIOUS',
        '.INTERMEDIATE',
        '.NOTINTERMEDIATE',
        '.SECONDARY',
        '.SECONDEXPANSION',
        '.DELETE_ON_ERROR',
        '.IGNORE',
        '.LOW_RESOLUTION_TIME',
        '.SILENT',
        '.EXPORT_ALL_VARIABLES',
        '.NOTPARALLEL',
        '.ONESHELL',
        '.POSIX',
        '.WAIT',
    }
)

# The dump's labels, as make 4.x prints them under LC_ALL=C.
_FILES_HEADER = '\n# Files\n'
_FILES_END = '\n# VPATH Search Paths'
_NOT_A_TARGET = '# Not a target:'
_PHONY_FLAG = '#  Phony target (prerequisite of .PHONY).'

# What an outer make passes an inner one, or what reads extra makefiles in. Under `make test PATHS=...` the
# outer make's MAKEFLAGS holds PATHS, and MAKEFILES, if set, is read before the Makefile.
_INHERITED = ('MAKEFLAGS', 'MFLAGS', 'GNUMAKEFLAGS', 'MAKELEVEL', 'MAKEFILES', 'PATHS', 'LANGUAGE')

# A probe pair for the control test: one target declared .PHONY and one not, both with a recipe that says it
# ran. They are added with --eval, so the Makefile on disk is never edited.
PROBE_DECLARED = 'phony-probe-declared'
PROBE_UNDECLARED = 'phony-probe-undeclared'
PROBE_RAN = 'phony-probe-recipe-ran:'
PROBE_RULES = (
    f'.PHONY: {PROBE_DECLARED}',
    f'{PROBE_DECLARED}: ; @echo {PROBE_RAN}$@',
    f'{PROBE_UNDECLARED}: ; @echo {PROBE_RAN}$@',
)


@dataclass(frozen=True)
class Entry:
    """One name in make's '# Files' section, with what make recorded about it.

    `named_lines` are its lines that begin with the name: the rule line, and one per target-specific variable.
    """

    name: str
    is_target: bool
    phony: bool
    named_lines: tuple[str, ...]


def _make_env() -> dict[str, str]:
    """This process's environment without what an outer make hands an inner one, with make's labels in C."""
    env = {name: value for name, value in os.environ.items() if name not in _INHERITED}
    env['LC_ALL'] = 'C'
    return env


def _make_database(cwd: Path, *evals: str) -> str:
    """GNU make's database for the repository Makefile, read from `cwd`. No recipe runs.

    Each of `evals` is passed as --eval: makefile text make reads alongside the Makefile.
    """
    assert shutil.which('make'), 'make is not on PATH, so the Makefile cannot be read the way make reads it'
    command = ['make', '--no-print-directory', '-p', '-q', '-r', '-R', '-C', str(cwd), '-f', str(MAKEFILE)]
    for text in evals:
        command += ['--eval', text]
    command.append(str(MAKEFILE))
    result = subprocess.run(
        command, capture_output=True, text=True, env=_make_env(), check=False, timeout=MAKE_TIMEOUT_S
    )
    assert result.returncode == 0, (
        f'make could not print its database (exit {result.returncode}; -q with the Makefile as the goal '
        f'should be up to date at once): {result.stderr}'
    )
    return result.stdout


def _files_section(database: str) -> str:
    """The '# Files' section of a make database dump."""
    assert _FILES_HEADER in database, f'no {_FILES_HEADER.strip()!r} section in the dump: not GNU make output'
    section = database.split(_FILES_HEADER, 1)[1]
    assert _FILES_END in section, f'no {_FILES_END.strip()!r} after the Files section: the dump format changed'
    return section.split(_FILES_END, 1)[0]


def _entries(database: str) -> dict[str, Entry]:
    """Every name in the Files section, keyed by name.

    A paragraph per name, blank-line separated. Its lines that are neither comments nor tab-indented recipe
    lines all begin `<name>:` -- the rule line, and one line per target-specific variable. A paragraph whose
    lines disagree on the name means the format is not what this parser reads, and it fails rather than
    guess. A double-colon rule prints one paragraph per rule; they merge.
    """
    entries: dict[str, Entry] = {}
    for paragraph in _files_section(database).split('\n\n'):
        lines = paragraph.splitlines()
        named = tuple(line for line in lines if line and not line.startswith(('#', '\t')))
        if not named:
            continue
        names = {line.split(':', 1)[0] for line in named}
        assert len(names) == 1, f'one paragraph of the dump names {sorted(names)}, not one name:\n{paragraph}'
        name = names.pop()
        is_target = _NOT_A_TARGET not in lines
        phony = _PHONY_FLAG in lines
        earlier = entries.get(name)
        if earlier is not None:
            is_target, phony = is_target or earlier.is_target, phony or earlier.phony
            named = earlier.named_lines + named
        entries[name] = Entry(name=name, is_target=is_target, phony=phony, named_lines=named)
    return entries


def _phony_prerequisites(entries: dict[str, Entry]) -> frozenset[str]:
    """The prerequisites of make's .PHONY, as make resolved them across every .PHONY line."""
    phony = entries.get('.PHONY')
    assert phony is not None, 'the dump has no .PHONY entry: the Makefile declares nothing .PHONY'
    assert len(phony.named_lines) == 1, f'.PHONY prints as more than one line: {phony.named_lines}'
    rule = phony.named_lines[0]
    assert rule.startswith('.PHONY:'), f'.PHONY prints as {rule!r}, not a rule line'
    return frozenset(rule.split(':', 1)[1].split())


def _undeclared(entries: dict[str, Entry], file_targets: frozenset[str]) -> list[str]:
    """Targets make knows that are not .PHONY, less the special targets and the allowlisted file targets."""
    exempt = SPECIAL_TARGETS | file_targets
    return sorted(
        entry.name for entry in entries.values() if entry.is_target and not entry.phony and entry.name not in exempt
    )


@pytest.fixture(scope='module')
def scratch(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """An empty directory to run make from, so nothing in the checkout is read as a target's file."""
    return tmp_path_factory.mktemp('makefile_phony')


@pytest.fixture(scope='module')
def entries(scratch: Path) -> dict[str, Entry]:
    """The repository Makefile's Files section as GNU make reads it."""
    assert shutil.which('make'), 'make is not on PATH'
    version = subprocess.run(['make', '--version'], capture_output=True, text=True, env=_make_env(), check=False)
    assert version.stdout.startswith('GNU Make'), f'make is not GNU make, whose database this reads: {version.stdout}'
    return _entries(_make_database(scratch))


@pytest.fixture(scope='module')
def file_targets(scratch: Path) -> dict[str, str]:
    """Each allowlisted variable, and the target name make expands it to."""
    return {name: _expanded_make_variable(name, scratch, _make_env()) for name in FILE_TARGET_VARIABLES}


def test_the_dump_reads_as_this_file_expects(entries: dict[str, Entry]) -> None:
    """The parse is not vacuous: make's phony flags agree with .PHONY's own prerequisites, and test is one.

    Without this a changed label in a future make would read every target as undeclared, which fails
    loudly, or -- worse -- a parse that found no targets at all would pass the test below.
    """
    targets = {entry.name for entry in entries.values() if entry.is_target}
    flagged = frozenset(entry.name for entry in entries.values() if entry.phony)
    declared = _phony_prerequisites(entries)

    assert 'test' in targets, f'the dump shows no `test` target; the parse found {sorted(targets)}'
    assert flagged == declared, (
        f"the targets make flags phony differ from .PHONY's prerequisites: only flagged "
        f'{sorted(flagged - declared)}, only in .PHONY {sorted(declared - flagged)}'
    )
    assert 'test' in flagged, '`test` is not flagged phony: the tj-06uflo target itself'


def test_every_target_that_is_not_a_file_is_phony(entries: dict[str, Entry], file_targets: dict[str, str]) -> None:
    """tj-06uflo, for every target: undeclared, a file or directory of its name makes it a silent no-op.

    A target listed here either needs `.PHONY: <target>` beside its recipe in the Makefile (builder-shared's
    file), or, if its name really is a path the Makefile makes, an entry in FILE_TARGET_VARIABLES.
    """
    undeclared = _undeclared(entries, frozenset(file_targets.values()))

    assert not undeclared, (
        f'{MAKEFILE.name} targets not declared .PHONY: {undeclared}. A file or directory named like one makes '
        f'make call it up to date, run nothing and exit 0 (tj-06uflo). Declare it `.PHONY: <target>` directly '
        f'above its recipe, or, if it is a file the Makefile makes, add its variable to FILE_TARGET_VARIABLES.'
    )


@pytest.mark.parametrize('variable', FILE_TARGET_VARIABLES)
def test_each_allowlisted_file_target_is_still_a_file_target(
    variable: str, entries: dict[str, Entry], file_targets: dict[str, str]
) -> None:
    """The allowlist cannot rot: each entry still names a target make knows, and that target is not phony.

    A variable that was renamed or deleted expands to nothing, or to a name with no rule, and fails here
    instead of quietly exempting nothing. A phony file target would be remade on every run, and so would
    everything depending on it -- for VENV_MARKER, a full `uv sync` before every target.
    """
    name = file_targets[variable]

    assert name, f'{variable} expands to nothing: remove it from FILE_TARGET_VARIABLES, or restore it'
    entry = entries.get(name)
    assert entry is not None and entry.is_target, (
        f'{variable} expands to {name!r}, which is not a target in {MAKEFILE.name}: remove it from '
        f'FILE_TARGET_VARIABLES, or restore its rule'
    )
    assert not entry.phony, (
        f'{variable} ({name}) is declared .PHONY, so make remakes it, and everything depending on it, on every '
        f'run; a file target must not be phony'
    )


def test_the_flag_read_here_is_the_one_that_decides_the_no_op(tmp_path: Path) -> None:
    """The control: on this Makefile, a target without the flag is silenced and is reported here.

    Two probe targets join the real Makefile through --eval, one declared .PHONY and one not, each with a
    directory of its own name beside it. Run for real, the undeclared one is tj-06uflo's false green: exit 0,
    recipe not run. The declared one runs. The parse reports the undeclared probe and not the declared one,
    so the main test catches a new target added without .PHONY, and this test is red whenever it cannot.
    It asserts nothing about the Makefile's own targets: that is the main test's job, and a Makefile
    regression reds that test alone.
    """
    database = _entries(_make_database(tmp_path, *PROBE_RULES))
    reported = _undeclared(database, frozenset())
    for probe in (PROBE_DECLARED, PROBE_UNDECLARED):
        (tmp_path / probe).mkdir()

    command = ['make', '--no-print-directory', '-C', str(tmp_path), '-f', str(MAKEFILE)]
    evals = [argument for rule in PROBE_RULES for argument in ('--eval', rule)]
    runs = {
        probe: subprocess.run(
            [*command, *evals, probe],
            capture_output=True,
            text=True,
            env=_make_env(),
            check=False,
            timeout=MAKE_TIMEOUT_S,
        )
        for probe in (PROBE_DECLARED, PROBE_UNDECLARED)
    }

    assert PROBE_UNDECLARED in reported, f'the parse misses a target added without .PHONY; it reports {reported}'
    assert PROBE_DECLARED not in reported, f'the parse reports a target declared .PHONY: {reported}'
    silenced = runs[PROBE_UNDECLARED]
    assert silenced.returncode == 0, f'{PROBE_UNDECLARED}: {silenced.stderr}'
    assert PROBE_RAN not in silenced.stdout, (
        f'a directory named {PROBE_UNDECLARED} did not silence the undeclared probe, so this control no longer '
        f'shows the hazard: {silenced.stdout}'
    )
    declared = runs[PROBE_DECLARED]
    assert declared.returncode == 0, f'{PROBE_DECLARED}: {declared.stderr}'
    assert f'{PROBE_RAN}{PROBE_DECLARED}' in declared.stdout, (
        f'the .PHONY probe did not run its recipe beside a directory of its name: {declared.stdout}'
    )
