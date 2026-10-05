"""Entry point: python -m data.store.seeds [--date YYYY-MM-DD]. Prints one bundle line on stdout.

Runs inside test_client, launched per call, reading the stack's settings from the environment it
already carries (stack.py). Writes nothing to disk: on success stdout holds exactly ONE line, the
bundle (bundle.py is the contract), and everything else goes to stderr. Turn the bundle into files
with `python -m data.store.seeds.bundle --out DIR`.

Exit status: 0 the bundle was printed; 3 the producer refused (a revision mismatch or an
alembic_version that does not hold exactly one row, a non-synthetic row, a failed POST, a POST whose
data_points does not match its symbol, no old four-column bar key collision, a bundle over the size
cap); 1 anything else failed (a setting missing from the environment, a call against the stack, a
query returning anything but the shape asked for such as a count that is not one non-negative
integer, a line starting with a backslash in the dump, a --date that is not a real YYYY-MM-DD
calendar date, checked before anything connects). Run it from the repository root.
"""

import argparse
import re
import sys
from collections.abc import Sequence
from datetime import date

from data.store.seeds.bundle import render_bundle
from data.store.seeds.dump import DumpRefused
from data.store.seeds.producer import SeedRefused, produce
from data.store.seeds.stack import Stack, StackError


EXIT_FAILED = 1
EXIT_REFUSED = 3

DATE_PATTERN = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')


def build_parser() -> argparse.ArgumentParser:
    """The command line: the manifest date. The stack comes from the environment."""
    parser = argparse.ArgumentParser(prog='python -m data.store.seeds', description=__doc__.splitlines()[0])
    parser.add_argument('--date', help="the manifest's UTC date, YYYY-MM-DD; default today")
    return parser


def _valid_date(text: str) -> bool:
    """Strictly YYYY-MM-DD in ASCII digits, and a real calendar date."""
    if not DATE_PATTERN.fullmatch(text):
        return False
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def main(argv: Sequence[str] | None = None) -> int:
    """Run the producer.

    Args:
        argv (Sequence[str] | None): Arguments; sys.argv when None.

    Returns:
        int: The exit status.
    """
    args = build_parser().parse_args(argv)
    if args.date is not None and not _valid_date(args.date):
        # Names the flag, never the value: the value is caller text and is not echoed.
        print('seed failed: --date is not a real YYYY-MM-DD date', file=sys.stderr)
        return EXIT_FAILED
    try:
        with Stack() as stack:
            result = produce(stack, date=args.date)
        line = render_bundle(result.bundle)
    except SeedRefused as refusal:
        print(f'seed refused: {refusal}', file=sys.stderr)
        return EXIT_REFUSED
    except (StackError, DumpRefused) as failure:
        print(f'seed failed: {failure}', file=sys.stderr)
        return EXIT_FAILED
    counts = ', '.join(f'{table}={count}' for table, count in result.row_counts.items())
    print(f'seed produced for revision {result.bundle.revision} ({counts})', file=sys.stderr)
    print(line)
    return 0


if __name__ == '__main__':
    sys.exit(main())
