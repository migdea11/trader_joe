"""The manifest, <revision>.json: what a seed holds, so a test compares against it instead of re-deriving.

Decision tj-vhboky.55 (SEED FORMAT): revision, producer, date, per-table row counts, and per-table
per-column digests. Keys are sorted and the file ends in one newline, so equal content is equal bytes.

THE REVISION is read from alembic_version by the producer, then checked against head_revision(), the
head of the revision chain in data/store/migrations/versions (read without alembic, from each
file's module-level `revision` and `down_revision`). A mismatch, or more than one head, is refused.

THE DIGEST ALGORITHM (recompute it with column_digest_sql, or the same query by hand). For one
table and one column, with the session at TIME ZONE 'UTC' and extra_float_digits 1:

    rows = the table's rows ordered by id ascending (both tables have an id primary key; an entry id
           is a hash of its natural key, a bar id is its rank, so the order is the same in any
           database holding the same seed);
    item = 'n' when the column is NULL, else 'v' followed by the column cast to text;
    digest = lower-case hex SHA-256 of the UTF-8 bytes of the items joined by a single newline
             (the empty string for a table with no rows).

Columns are keyed by NAME and listed alphabetically, never by position, so a table whose columns
were reordered by a downgrade (the tj-uxl817 hazard) digests the same. An item containing a newline
could in principle collide with two items; a synthetic seed has none.
"""

import ast
import json
from pathlib import Path


PRODUCER = 'data.store.seeds'

# The session settings the digests assume; the producer sends them as PGOPTIONS.
SESSION_OPTIONS = '-c TimeZone=UTC -c extra_float_digits=1'


class HeadRevisionError(Exception):
    """The revision chain has no single head."""


def _module_assignments(path: Path) -> dict[str, object]:
    """The module-level `name = literal` and `name: T = literal` assignments of a source file."""
    values: dict[str, object] = {}
    for node in ast.parse(path.read_text(encoding='utf-8')).body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            targets = [node.target.id]
        elif isinstance(node, ast.Assign):
            targets = [target.id for target in node.targets if isinstance(target, ast.Name)]
        else:
            continue
        try:
            value = ast.literal_eval(node.value)
        except ValueError:
            continue
        for name in targets:
            values[name] = value
    return values


def head_revision(versions_dir: Path) -> str:
    """The single head of the revision chain in a versions directory.

    Args:
        versions_dir (Path): data/store/migrations/versions.

    Returns:
        str: The revision id no other revision names as its down_revision.

    Raises:
        HeadRevisionError: If there are no revision files, or not exactly one head.
    """
    revisions: set[str] = set()
    parents: set[str] = set()
    for path in sorted(versions_dir.glob('*.py')):
        values = _module_assignments(path)
        revision = values.get('revision')
        if not isinstance(revision, str):
            continue
        revisions.add(revision)
        down = values.get('down_revision')
        if isinstance(down, str):
            parents.add(down)
        elif isinstance(down, tuple):
            parents.update(item for item in down if isinstance(item, str))
    heads = revisions - parents
    if len(heads) != 1:
        raise HeadRevisionError(f'expected exactly one head in {versions_dir}, found {len(heads)}')
    return next(iter(heads))


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def column_digest_sql(table: str, columns: list[str]) -> str:
    """One query returning `column|digest` for each column of a table, per the module docstring.

    Args:
        table (str): Table name, unquoted.
        columns (list[str]): Its column names.

    Returns:
        str: SQL, one row per column, sorted by column name.
    """
    selects = [
        f"SELECT '{column.replace(chr(39), chr(39) * 2)}' AS name, "  # nosec B608 -- table and column names come from information_schema, quoted
        f'encode(sha256(convert_to(coalesce(string_agg('
        f"CASE WHEN {_quote(column)} IS NULL THEN 'n' ELSE 'v' || {_quote(column)}::text END, E'\\n' ORDER BY id"
        f"), ''), 'UTF8')), 'hex') AS digest FROM {_quote(table)}"
        for column in sorted(columns)
    ]
    return ' UNION ALL '.join(selects) + ' ORDER BY name;'


def build_manifest(
    revision: str, date: str, row_counts: dict[str, int], digests: dict[str, dict[str, str]]
) -> dict[str, object]:
    """Assemble the manifest.

    Args:
        revision (str): The revision the seed was produced at.
        date (str): UTC date, YYYY-MM-DD.
        row_counts (dict[str, int]): Rows per table.
        digests (dict[str, dict[str, str]]): Per table, column name to digest.

    Returns:
        dict[str, object]: The manifest.
    """
    return {'revision': revision, 'producer': PRODUCER, 'date': date, 'row_counts': row_counts, 'digests': digests}


def render_manifest(manifest: dict[str, object]) -> str:
    """The manifest as committed text: sorted keys, two-space indent, one trailing newline."""
    return json.dumps(manifest, indent=2, sort_keys=True) + '\n'
