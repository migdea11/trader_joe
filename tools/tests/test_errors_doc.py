"""The error catalogue generator (tj-3mk3u5.37.10, items (a) to (e) of the bead's validator gate).

docs/errors.md is a COMMITTED BUILD PRODUCT, so the thing under test is not really the markdown: it is
that a committed copy and the one Reason table in common/errors cannot drift apart without something
going red. CI regenerates and reads git status; these tests are the in-suite twin of that step, and they
are what runs on a developer's machine before CI ever sees the branch.

A GENERATOR'S TEST SUITE IS UNUSUALLY EASY TO WRITE SO THAT IT CANNOT FAIL, which is the hazard this
file is built against:

* Comparing the generator's output to the generator's output proves only that it is a function. So the
  table is read back out of the rendered text and compared COLUMN BY COLUMN against REASONS, which is
  the source the generator is supposed to be faithful to -- never against a restated copy of the table.
* --check has a trivial implementation that passes a one-sided test: always return True. Both directions
  are therefore asserted, and the stale cases include the three that actually happen (hand-edited,
  CRLF-translated, absent).
* Determinism inside one process is nearly free and proves almost nothing. METADATA_KEYS is a frozenset,
  whose iteration order varies BETWEEN processes but is fixed within one, so the only test that can see
  a missing sort() is one that renders in a SECOND INTERPRETER under a different PYTHONHASHSEED.

WHY THE SUMMARY COLUMN IS CHECKED TOO (architect, widening (a) at the tj-3mk3u5.37.21 gate). The
round-trip test below proves committed == freshly generated, so it cannot see a generator that renders a
column wrongly: both sides agree with the mistake. And the summary is the one column no client branches
on, so nothing else in the repository would notice it dropped, truncated, or taken from the wrong row.
"""

import ast
import dataclasses
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from common.errors.vocabulary import ERROR_DOMAIN, METADATA_KEYS, REASONS, RESERVED_REASONS, Reason
from common.tests.roots import SERVER_ROOT
from tools import errors_doc
from tools.errors_doc import (
    _TABLE_COLUMNS,
    DOC_PATH,
    FEED_NOT_AVAILABLE_GRPC_TEXT,
    MAKE_TARGET,
    NO_GRPC_STATUS_TEXT,
    REPO_ROOT,
    is_current,
    main,
    render_document,
    write_document,
)


MODULE_PATH = REPO_ROOT / 'tools' / 'errors_doc.py'

# THE TWO ROOTS A FIRST-PARTY IMPORT CAN RESOLVE AGAINST (tj-iontkq.4). errors_doc.py's own
# REPO_ROOT is `tools/..`, which is still the true repository root because tools/ did not move --
# but common/ did, so the generator's two halves no longer share one import root. Order matters as
# it does on any PYTHONPATH: the server root first, as the service image has it.
_FIRST_PARTY_ROOTS = (SERVER_ROOT, REPO_ROOT)
_FIRST_PARTY_PYTHONPATH = os.pathsep.join(str(root) for root in _FIRST_PARTY_ROOTS)

# A markdown cell boundary: a pipe the generator did not escape. Splitting on a bare '|' would cut a
# cell whose text contains one in half, and the generator escapes exactly so that it does not have to.
_CELL_BOUNDARY = re.compile(r'(?<!\\)\|')

# The standard-library names this module may import, beside anything resolving inside the repository.
# Read from the interpreter rather than listed, so it cannot go stale against a Python upgrade.
_STDLIB = sys.stdlib_module_names


def table_rows(text: str) -> list[tuple[str, ...]]:
    """Read the reason table back out of a rendered document, undoing the generator's pipe escaping.

    Args:
        text (str): The rendered document.

    Returns:
        list[tuple[str, ...]]: One tuple of cells per row, header and separator included.
    """
    rows = []
    for line in text.splitlines():
        if not line.startswith('|'):
            continue
        cells = _CELL_BOUNDARY.split(line)[1:-1]
        rows.append(tuple(cell.strip().replace(r'\|', '|') for cell in cells))
    return rows


def expected_grpc_cell(reason: Reason) -> str:
    """The gRPC cell a row must carry, decided from REASONS rather than from the generator's output.

    The two fixed texts are imported rather than retyped, so a deliberate rewording is not a false red;
    WHICH of them a reason gets is the routing under test, and that is decided here independently.

    Args:
        reason (Reason): The row's reason.

    Returns:
        str: The expected cell.
    """
    code = REASONS[reason].grpc_code
    if code is not None:
        return code
    return FEED_NOT_AVAILABLE_GRPC_TEXT if reason is Reason.FEED_NOT_AVAILABLE else NO_GRPC_STATUS_TEXT


# ---------------------------------------------------------------------------------------------------
# (a) EVERY REASON, EXACTLY ONCE, IN DECLARATION ORDER, EVERY COLUMN FROM THE TABLE


def test_the_table_lists_every_reason_exactly_once_in_declaration_order():
    """Enumerate Reason; never hand-pick members, or the test stops covering a member added later.

    Comparing the whole column as a LIST rather than as a set is what makes this one assertion cover
    three failures at once: a missing reason, a duplicated one, and a reordering.
    """
    rows = table_rows(render_document())

    assert [row[0] for row in rows[2:]] == [reason.value for reason in Reason]


@pytest.mark.parametrize('reason', list(Reason), ids=lambda reason: reason.value)
def test_every_column_of_a_row_is_what_the_reason_table_says(reason: Reason):
    """Each cell against REASONS, the source of truth -- not against a second copy of it written here.

    The summary is included on the architect's widening of (a): the round-trip test cannot see a column
    rendered wrongly, because the committed file and the fresh generation agree with the mistake, and no
    client branches on the summary so nothing else in the repository would notice it go wrong.
    """
    spec = REASONS[reason]
    rows = {row[0]: row for row in table_rows(render_document())[2:]}

    assert reason.value in rows, f'{reason.value} has no row'
    _, outcome, disposition, http_status, grpc_cell, summary = rows[reason.value]
    assert outcome == spec.outcome.value
    assert disposition == spec.disposition.value
    assert http_status == str(spec.http_status)
    assert grpc_cell == expected_grpc_cell(reason)
    assert summary == spec.summary


def test_the_two_fixed_texts_for_a_reason_that_is_never_a_grpc_status_are_the_ones_the_bead_names():
    """The one place a literal string belongs: these two are specified in tj-3mk3u5.37.10's body.

    Every other assertion reads its expectation from the table or from these constants, so without this
    the two texts could be rewritten to anything and the suite would follow them silently.
    """
    assert FEED_NOT_AVAILABLE_GRPC_TEXT == 'in-band on the FetchDataset ack, never a status'
    assert NO_GRPC_STATUS_TEXT == 'not rendered as a status'

    rows = {row[0]: row for row in table_rows(render_document())[2:]}

    assert rows[Reason.FEED_NOT_AVAILABLE.value][4] == FEED_NOT_AVAILABLE_GRPC_TEXT
    others = [
        reason for reason in Reason if REASONS[reason].grpc_code is None and reason is not Reason.FEED_NOT_AVAILABLE
    ]
    assert others, 'no reason but FEED_NOT_AVAILABLE lacks a grpc_code, so NO_GRPC_STATUS_TEXT is unreachable'
    for reason in others:
        assert rows[reason.value][4] == NO_GRPC_STATUS_TEXT


def test_the_reserved_names_are_listed_as_reserved_and_are_not_rows():
    """Read from RESERVED_REASONS, never hard-coded (the bead says so explicitly).

    The second half is the one that matters: a reserved name appearing as a table row would read as a
    reason the platform raises, which is exactly what reserving it is meant to prevent.
    """
    document = render_document()
    reason_values = {row[0] for row in table_rows(document)[2:]}

    assert RESERVED_REASONS, 'nothing is reserved, so this test proves nothing'
    for name in RESERVED_REASONS:
        assert f'- `{name}`' in document
        assert name not in reason_values, f'{name} is reserved and must not be a row'


def test_a_summary_containing_a_pipe_still_renders_as_one_row(monkeypatch: pytest.MonkeyPatch):
    """The escaping in _cell, which NO test reached until a mutation showed it could be deleted unnoticed.

    No summary carries a pipe today, so the escaping and this file's own unescaping were both dead code:
    deleting the escaping line from _cell outright left the whole suite green. That is exactly the guard
    the generator's author wrote it as -- for a summary added later -- so the test has to supply that
    later summary rather than wait for one to arrive.

    An unescaped pipe does not corrupt one cell; it ENDS it, so every column after it shifts left and the
    row silently gains a seventh field. Both halves are asserted, because a test reading only the summary
    cell would pass on a row that had quietly become malformed.
    """
    reason = next(iter(Reason))
    piped = dataclasses.replace(REASONS[reason], summary='stop | and read this')
    monkeypatch.setattr(errors_doc, 'REASONS', dict(REASONS) | {reason: piped})

    rows = table_rows(errors_doc.render_document())[2:]

    assert len(rows) == len(list(Reason)), 'a pipe in a summary split the table into a different shape'
    row = next(candidate for candidate in rows if candidate[0] == reason.value)
    assert len(row) == len(_TABLE_COLUMNS), f'the row has {len(row)} cells, so a pipe ended one early'
    assert row[5] == 'stop | and read this'


def test_the_extension_members_are_the_metadata_keys_and_the_domain_is_the_constant():
    """METADATA_KEYS is a frozenset, so the document must list it sorted to be reproducible at all."""
    document = render_document()

    listed = [line.removeprefix('- `').removesuffix('`') for line in document.splitlines() if line.startswith('- `')]
    metadata_section = [name for name in listed if name not in RESERVED_REASONS]
    assert metadata_section == sorted(METADATA_KEYS)
    assert f'`{ERROR_DOMAIN}`' in document


# ---------------------------------------------------------------------------------------------------
# (b) DETERMINISM, AND THE COMMITTED FILE


def test_two_generations_in_one_process_are_byte_identical():
    """The cheapest half of determinism. It cannot see a frozenset ordering bug -- the next test can."""
    assert render_document() == render_document()


def test_the_committed_document_is_exactly_what_a_fresh_generation_writes():
    """The in-suite twin of the CI step: this is what goes red when someone edits the table and forgets.

    Read through Path.open with newline='' rather than read_text, for the same reason the generator does:
    read_text only grew a newline argument in 3.13 and this targets 3.12, so read_text would translate
    CRLF to LF and call a file with Windows endings current.
    """
    with DOC_PATH.open(encoding='utf-8', newline='') as handle:
        committed = handle.read()

    assert committed == render_document(), f'docs/errors.md is stale; run {MAKE_TARGET}'
    assert is_current(DOC_PATH)


def test_the_rendering_is_identical_under_a_different_hash_seed():
    """THE ONLY TEST THAT CAN SEE AN UNSORTED FROZENSET, because iteration order is fixed within a process.

    METADATA_KEYS is a frozenset. Two renders in this interpreter agree however the generator orders it,
    so the sort that makes --check meaningful is invisible to every in-process test. Two child
    interpreters under different PYTHONHASHSEEDs are what actually exercise it.
    """
    outputs = []
    for seed in ('1', '999'):
        done = subprocess.run(
            [sys.executable, '-c', 'from tools.errors_doc import render_document; print(render_document(), end="")'],
            cwd=REPO_ROOT,
            # The child imports tools.errors_doc, which imports common.errors, and those two now
            # live under different roots: tools/ stayed at the top of the repository and common/
            # moved under server/ (tj-iontkq.4). cwd alone used to supply both.
            env={**os.environ, 'PYTHONHASHSEED': seed, 'PYTHONPATH': _FIRST_PARTY_PYTHONPATH},
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 0, done.stderr
        outputs.append(done.stdout)

    assert outputs[0] == outputs[1]
    assert outputs[0] == render_document()


def test_the_document_has_lf_endings_a_trailing_newline_and_no_timestamp_or_sha():
    """A check that depends on the machine is worse than no check, so the output carries nothing variable."""
    document = render_document()

    assert '\r' not in document
    assert document.endswith('\n')
    assert not document.endswith('\n\n')
    # At least one digit: an all-letter run of hex characters is an English word ('defaced'), not a SHA.
    assert not re.search(r'\b(?=[0-9a-f]{7,40}\b)[a-f]*[0-9][0-9a-f]*\b', document), (
        'something that looks like a git SHA is in the output'
    )
    assert not re.search(r'\b(19|20)\d\d-\d\d-\d\d\b', document), 'something that looks like a date is in the output'


# ---------------------------------------------------------------------------------------------------
# (c) --check, IN BOTH DIRECTIONS
#
# Both directions, always. 'Return True' passes any one-sided version of these, and 'return False' passes
# the other, so either alone would be a test that cannot fail for the implementation it is guarding.


def test_check_exits_zero_on_a_freshly_written_file(tmp_path: Path, capsys: pytest.CaptureFixture):
    """The green direction, on a scratch copy rather than the committed file (--path exists for this)."""
    scratch = tmp_path / 'errors.md'
    write_document(scratch)

    assert main(['--check', '--path', str(scratch)]) == 0
    assert capsys.readouterr().err == ''


@pytest.mark.parametrize('kind', ['hand-edited', 'crlf', 'missing', 'truncated', 'empty'])
def test_check_exits_non_zero_on_a_stale_file_and_names_the_make_target(
    kind: str, tmp_path: Path, capsys: pytest.CaptureFixture
):
    """The red direction, over the ways a committed generated file actually goes wrong.

    'crlf' is the case the generator's own author hit: Path.read_text gained newline= only in 3.13, so a
    3.12 implementation reading through read_text translates the endings away and calls a CRLF file
    current. 'missing' is why the CI step reads git status rather than git diff -- an absent or untracked
    file is stale too, and git diff alone would not notice.
    """
    scratch = tmp_path / 'errors.md'
    if kind == 'hand-edited':
        scratch.write_text(render_document().replace('about:blank', 'https://example.test/errors'), newline='\n')
    elif kind == 'crlf':
        scratch.write_bytes(render_document().encode('utf-8').replace(b'\n', b'\r\n'))
    elif kind == 'truncated':
        scratch.write_text(render_document()[:-50], newline='\n')
    elif kind == 'empty':
        scratch.write_text('', newline='\n')

    assert main(['--check', '--path', str(scratch)]) == 1
    assert MAKE_TARGET in capsys.readouterr().err


def test_check_writes_nothing_even_when_the_file_is_stale(tmp_path: Path):
    """--check is read-only: a check that repaired what it found would never report a stale file twice."""
    scratch = tmp_path / 'errors.md'
    stale = 'this is not the catalogue\n'
    scratch.write_text(stale, newline='\n')

    assert main(['--check', '--path', str(scratch)]) == 1

    assert scratch.read_text() == stale, '--check rewrote the file it was asked only to inspect'


def test_check_leaves_the_committed_document_untouched_when_pointed_at_a_scratch_copy(tmp_path: Path):
    """The committed file is the thing every other test reads; a scratch run must not be able to disturb it."""
    before = DOC_PATH.read_bytes()
    scratch = tmp_path / 'errors.md'
    scratch.write_text('stale\n', newline='\n')

    main(['--check', '--path', str(scratch)])
    main(['--path', str(scratch)])

    assert DOC_PATH.read_bytes() == before


def test_writing_to_a_scratch_path_produces_a_file_check_then_accepts(tmp_path: Path):
    """The default run writes, and what it writes satisfies --check: the make target and CI step agree."""
    scratch = tmp_path / 'nested' / 'errors.md'

    assert main(['--path', str(scratch)]) == 0

    assert scratch.read_bytes() == render_document().encode('utf-8')
    assert is_current(scratch)


def test_is_current_is_false_for_a_path_that_cannot_be_read(tmp_path: Path):
    """A directory, not a file: is_current reports stale rather than raising out of the CI step."""
    assert is_current(tmp_path) is False
    assert is_current(tmp_path / 'nothing-here.md') is False


# ---------------------------------------------------------------------------------------------------
# (d) NOTHING OUTSIDE THE STANDARD LIBRARY AND THE REPOSITORY


def test_the_generator_imports_only_the_standard_library_and_the_repository():
    """The bead's 'standard library only' is what lets the generator run anywhere a checkout does.

    Scanned from the source, as test_no_production_test_imports.py does, rather than from sys.modules:
    importing this test file has already pulled pytest in, so a runtime check would be meaningless.
    """
    tree = ast.parse(MODULE_PATH.read_text(encoding='utf-8'))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split('.')[0])

    # BOTH ROOTS (tj-iontkq.4). A top-level importable package sits under the repository root
    # (tools, tests) or under the server root (common, data, routers, schemas); before the move
    # one iterdir() found them all, and a scan of the repository root alone would now report
    # `common` as a third-party import.
    first_party = {
        path.name for root in _FIRST_PARTY_ROOTS for path in root.iterdir() if (path / '__init__.py').exists()
    }
    assert roots, 'no imports were found, so this test is not reading the module it thinks it is'
    outside = sorted(root for root in roots if root not in _STDLIB and root not in first_party)
    assert outside == [], f'tools/errors_doc.py imports outside the standard library and the repository: {outside}'
    assert 'common' in roots, 'the generator must read the table from common.errors'


def test_the_generator_reads_the_table_and_does_not_restate_it():
    """A generator carrying its own copy of the rows would pass every rendering test and still be wrong.

    The point of TE-8 is ONE table. A hard-coded reason value, status code or reserved name in the script
    is a second copy that CI's staleness check cannot catch, because the generator and its output would
    agree with each other forever.
    """
    source = MODULE_PATH.read_text(encoding='utf-8')

    for name in RESERVED_REASONS:
        assert name not in source, f'{name} is hard-coded in the generator; it must come from RESERVED_REASONS'
    for reason in Reason:
        assert f"'{reason.value}'" not in source, f'{reason.value} is hard-coded in the generator'
    for key in METADATA_KEYS:
        assert f"'{key}'" not in source, f'the metadata key {key} is hard-coded in the generator'


# ---------------------------------------------------------------------------------------------------
# (e) about:blank, AND NO URL


def test_the_generated_document_is_tracked_by_git_and_not_ignored():
    """The one place the committed CI step is weaker than --check, closed here instead of in the workflow.

    The step runs `make errors-doc` and then reads `git status --porcelain --untracked-files=all` on
    docs/errors.md. That is stronger than the `git diff --exit-code` the bead permits, because it also
    catches a deleted or untracked file. But git status omits IGNORED paths unless asked for them: if
    docs/errors.md were ever gitignored, regeneration would recreate it, the step would print nothing
    and pass, and the repository would carry no catalogue at all. `--check` would have exited 1 on the
    absent file.

    Rather than ask for the workflow to change -- it is correct, and it mirrors the proto step exactly --
    this pins the assumption the step rests on. Measured when written: tracked, and not ignored.
    """
    tracked = subprocess.run(
        ['git', 'ls-files', '--error-unmatch', 'docs/errors.md'], cwd=REPO_ROOT, capture_output=True, text=True
    )
    assert tracked.returncode == 0, 'docs/errors.md is not tracked, so CI would regenerate it and see nothing'

    # check-ignore exits 0 when the path IS ignored and 1 when it is not.
    ignored = subprocess.run(
        ['git', 'check-ignore', '-q', 'docs/errors.md'], cwd=REPO_ROOT, capture_output=True, text=True
    )
    assert ignored.returncode == 1, 'docs/errors.md is gitignored, so the CI staleness step cannot see it'


def test_the_document_states_about_blank_and_promises_no_url():
    """No type URI namespace is published (ADR tj-fa1rpu, Q-URI addendum), so nothing may link into this file.

    A URL here would be a promise: RFC 9457 readers follow a type URI, and one that 404s later is worse
    than the about:blank the platform actually uses.
    """
    document = render_document()

    assert 'about:blank' in document
    assert not re.search(r'https?://', document), 'the catalogue promises no URLs'
