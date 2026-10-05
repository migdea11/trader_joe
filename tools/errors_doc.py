"""Generate docs/errors.md from the one Reason table in common/errors (ADR tj-fa1rpu, Q-URI addendum).

The catalogue is the human-readable documentation RFC 9457 associates with a type URI. This platform
publishes no type URI -- the problem+json type is about:blank for every error -- so readers reach the
prose through the reason instead, and nothing links into the generated file.

STANDARD LIBRARY ONLY, and one first-party import: common.errors. The vocabulary it reads is itself
standard-library only, so this script runs anywhere a checkout does, with no service and no transport.

THE make proto PATTERN (ADR tj-8konfu D3; the Makefile's proto target). The output is committed, a
make target is the one way it is written, and a CI step regenerates and fails when the committed file
differs. A committed copy can go stale; the check is what makes committing it safe.

    python -m tools.errors_doc            write docs/errors.md
    python -m tools.errors_doc --check    write nothing, exit non-zero when the file is stale
    python -m tools.errors_doc --path P   use P instead of docs/errors.md, for either mode

DETERMINISM IS THE POINT OF --check. A check that can pass or fail depending on the machine is worse
than no check, so the rendering depends on nothing but the table: rows in Reason declaration order,
metadata keys sorted (METADATA_KEYS is a frozenset, whose iteration order varies between processes),
reserved names in their declared order, no timestamp, no git SHA, no environment-dependent text, LF
line endings and a trailing newline.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from common.errors.vocabulary import ERROR_DOMAIN, METADATA_KEYS, REASONS, RESERVED_REASONS, Reason, ReasonSpec


# The target named in the staleness message, so a reader is told the one command that fixes it.
MAKE_TARGET: Final = 'make errors-doc'

# tools/errors_doc.py -> tools/ -> the repository root.
REPO_ROOT: Final = Path(__file__).resolve().parent.parent
DOC_PATH: Final = REPO_ROOT / 'docs' / 'errors.md'

# The two fixed texts for a row whose grpc_code is None (tj-3mk3u5.37.10). FEED_NOT_AVAILABLE travels
# in-band on the FetchDataset ack (tj-3mk3u5.22 Q5); every other None is simply never a status.
FEED_NOT_AVAILABLE_GRPC_TEXT: Final = 'in-band on the FetchDataset ack, never a status'
NO_GRPC_STATUS_TEXT: Final = 'not rendered as a status'

_TABLE_COLUMNS: Final = ('Reason', 'Outcome', 'Disposition', 'HTTP status', 'gRPC code', 'Summary')


def _cell(text: str) -> str:
    """Return text safe to put in a table cell: a pipe would end the cell and shift every later column.

    No summary carries one today. This exists so that one added later renders as a row rather than as a
    silently malformed table.

    Args:
        text: The cell's text.

    Returns:
        str: The text with every pipe escaped.
    """
    return text.replace('|', r'\|')


def _grpc_cell(reason: Reason, spec: ReasonSpec) -> str:
    """Return the gRPC column for one row: the status code name, or why the reason is never a status.

    Args:
        reason: The row's reason.
        spec: Its row in REASONS.

    Returns:
        str: The code name, or one of the two fixed texts for a row with no code.
    """
    if spec.grpc_code is not None:
        return spec.grpc_code
    return FEED_NOT_AVAILABLE_GRPC_TEXT if reason is Reason.FEED_NOT_AVAILABLE else NO_GRPC_STATUS_TEXT


def _row(cells: Sequence[str]) -> str:
    return f'| {" | ".join(_cell(cell) for cell in cells)} |'


def _preamble() -> list[str]:
    """Return the fixed preamble, the contract a reader needs before the table (ADR tj-fa1rpu U3 as amended).

    Returns:
        list[str]: The preamble's lines, without a trailing blank.
    """
    return [
        '## The contract',
        '',
        'The problem+json `type` is always `about:blank`. No type URI namespace is published, and nothing',
        'publishes a link into this file, so it promises no anchors and no URLs.',
        '',
        'The `title` is the HTTP status phrase, which is what RFC 9457 asks for when the type is',
        '`about:blank`. Per-reason prose lives here, never in a member a client parses.',
        '',
        f"An error's identity is (`domain`, `reason`), and `domain` is always `{ERROR_DOMAIN}`. Clients branch",
        "on `reason`, and on nothing else; retryability is the reason's outcome in the table below, not a",
        'field. A client must survive a reason it has never heard of.',
        '',
        'The `detail` member is human text. It is never parsed.',
        '',
        '`Retry-After`, `retry_after` and `reset_at` are present whenever the error carries a reset time.',
        '`retry_after` is derived from `reset_at` when the answer is written, so a relayed error never',
        'carries a delay that went stale on the way.',
    ]


def _reason_table() -> list[str]:
    """Return the one table: a row per Reason, in declaration order.

    Returns:
        list[str]: The table's lines, header first.
    """
    lines = [_row(_TABLE_COLUMNS), _row(['---'] * len(_TABLE_COLUMNS))]
    lines.extend(
        _row(
            [
                reason.value,
                REASONS[reason].outcome.value,
                REASONS[reason].disposition.value,
                str(REASONS[reason].http_status),
                _grpc_cell(reason, REASONS[reason]),
                REASONS[reason].summary,
            ]
        )
        for reason in Reason
    )
    return lines


def render_document() -> str:
    """Render the whole of docs/errors.md from the vocabulary.

    Returns:
        str: The file's text, LF-terminated lines with a trailing newline.
    """
    lines = [
        f'<!-- GENERATED by `{MAKE_TARGET}` from common/errors. Do not edit by hand. -->',
        '',
        '# Error catalogue',
        '',
        f'Every failure this platform returns on purpose, generated by `{MAKE_TARGET}` from the one `Reason`',
        'table in `common/errors`. Change the table and regenerate in the same commit; CI fails on a stale',
        'file.',
        '',
        *_preamble(),
        '',
        '## Reasons',
        '',
        *_reason_table(),
        '',
        '## Extension members',
        '',
        'Beside `type`, `title`, `status` and `detail`, a problem body always carries `domain` and',
        '`reason`, and carries these allowlisted members where the error has them -- and nothing else:',
        '',
        *(f'- `{key}`' for key in sorted(METADATA_KEYS)),
        '',
        '## Reserved reasons',
        '',
        'These names are fixed for later work and are **not** members today. Nothing raises them, and no',
        'member may take one of them to mean something else:',
        '',
        *(f'- `{name}`' for name in RESERVED_REASONS),
        '',
    ]
    return '\n'.join(lines)


def write_document(path: Path = DOC_PATH) -> None:
    """Write the rendered catalogue, replacing whatever is there.

    Args:
        path: Where to write it. The default is the committed docs/errors.md.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_document(), encoding='utf-8', newline='\n')


def is_current(path: Path = DOC_PATH) -> bool:
    """Report whether the file on disk is byte-for-byte what this module would write.

    Reads without newline translation, so a file whose lines end CRLF is stale rather than equal.

    Args:
        path: The file to compare. The default is the committed docs/errors.md.

    Returns:
        bool: True when the file exists and matches; False when it differs or cannot be read.
    """
    try:
        # Path.open, not Path.read_text: read_text only grew a newline argument in 3.13, and this runs on 3.12.
        with path.open(encoding='utf-8', newline='') as handle:
            current = handle.read()
    except OSError:
        return False
    return current == render_document()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the generator.

    Args:
        argv: The command line, without the program name. The default reads sys.argv.

    Returns:
        int: 0 on success; 1 when --check finds the file stale or unreadable.
    """
    parser = argparse.ArgumentParser(
        prog='python -m tools.errors_doc', description='Generate docs/errors.md from the Reason table in common/errors.'
    )
    parser.add_argument(
        '--check',
        action='store_true',
        help='Write nothing; exit non-zero when the file on disk differs from what would be written.',
    )
    parser.add_argument(
        '--path', type=Path, default=DOC_PATH, help=f'The file to write or check (default: {DOC_PATH}).'
    )
    args = parser.parse_args(argv)
    if args.check:
        if is_current(args.path):
            return 0
        print(
            f'{args.path} is not what the Reason table renders; run {MAKE_TARGET} and commit the result.',
            file=sys.stderr,
        )
        return 1
    write_document(args.path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
