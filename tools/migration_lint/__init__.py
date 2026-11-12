"""Migration lint: refuse a store revision that is not additive, unless it carries the marker (tj-aw0tuk).

Rollback moves code back, never schema, so old code must keep running against a newer schema. That holds
only while every revision is additive (expand/contract). This lint reads each revision file under
server/data/store/migrations/versions with the ast module (no database, no import of the revision) and
refuses, in everything but downgrade():

    drop_table, drop_column, drop_constraint, drop_index, rename_table
    alter_column that renames (new_column_name), changes the type (type_) or sets nullable=False
    add_column of a NOT NULL column (nullable=False or primary_key=True) without a server_default
    (an explicit server_default=None is no default)
    alter_column that removes a server_default (server_default=None) on a NOT NULL column, which is
    nullable=False or existing_nullable=False in the same call
    raw SQL (execute) that deletes, drops, truncates, renames, retypes or sets NOT NULL, adds a NOT NULL
    column without a DEFAULT, replaces a definition (CREATE OR REPLACE), or is not one of the additive
    forms (UPDATE, INSERT, CREATE, ALTER TYPE ADD VALUE, ALTER TABLE ADD COLUMN)

A raw ALTER TABLE is judged per clause, split on top-level commas: every clause must be an ADD COLUMN, and
each NOT NULL column needs a DEFAULT in its own clause. SQL comments (line and block) outside quotes are
replaced by a space before statements and clauses are split, so an apostrophe or comma in a comment cannot
hide a clause; text in quotes is left alone. An unterminated block comment or quote is refused. The
destructive denylist still searches the original text, comments included, which is the more conservative
reading; dollar-quoted strings are not read, so any raw SQL containing a dollar-quote delimiter is
refused; E-string backslash escapes and nested block comments are not understood either and can fail open.

THE MARKER is a line starting `# additive-exception: <bead-id>`. A revision carrying it is exempt; the
exception stays visible in the diff. The alpha baseline (ADR tj-kg521d) only creates, so it passes
unmarked and no marker exists in the tree; the machinery is for revisions after the alpha.

downgrade() is exempt: down-migrations are a non-goal (forward-only, recovery by restoring make migrate's
pre-migrate dump; the baseline's downgrade() raises, ADR tj-kg521d), and the inverse of an additive step is
a drop by construction.

RAW SQL: a statement the lint cannot read (a name it cannot resolve to a string) is refused, because a
gate that passes what it cannot see is the hole tj-aw0tuk was filed to close. An f-string with any part it
cannot resolve is unreadable as a whole; integer constants are resolved. Additive raw SQL (ALTER TYPE
ADD VALUE, UPDATE backfills) passes.

    python -m tools.migration_lint [PATH ...]    files or directories; default is the store's versions
"""

import argparse
import ast
import re
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final


REPO_ROOT: Final = Path(__file__).resolve().parent.parent.parent
VERSIONS_DIR: Final = REPO_ROOT / 'server' / 'data' / 'store' / 'migrations' / 'versions'

MARKER_RE: Final = re.compile(r'^# additive-exception: (\S+)', re.MULTILINE)

_ALWAYS_REFUSED: Final = {
    'drop_table': 'drops a table',
    'drop_column': 'drops a column',
    'drop_constraint': 'drops a constraint',
    'drop_index': 'drops an index',
    'rename_table': 'renames a table',
}

# Destructive or non-additive SQL. ALTER TYPE ... ADD VALUE and UPDATE are deliberately absent.
_DESTRUCTIVE_SQL: Final = re.compile(
    r'\b(?:DELETE\s+FROM|TRUNCATE|DROP\s+(?:TABLE|COLUMN|CONSTRAINT|TYPE|INDEX|SCHEMA|VIEW)'
    r'|RENAME\s+(?:TO|COLUMN|CONSTRAINT)|ALTER\s+COLUMN|SET\s+NOT\s+NULL|CREATE\s+OR\s+REPLACE)\b',
    re.IGNORECASE,
)

# A dollar-quote delimiter ($$ or $tag$). Positional parameters such as $1 do not match. Checked on the
# original text, before comment stripping, because a -- inside a dollar-quoted body would otherwise be
# read as a comment and swallow the closing delimiter.
_DOLLAR_QUOTE: Final = re.compile(r'\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$')

# Raw SQL is an allowlist, not only the denylist above: a statement must open as one of these additive
# forms, so a destructive verb nobody thought to list is refused by omission, not passed by it.
_ADDITIVE_SQL: Final = re.compile(
    r'\s*(?:UPDATE\b|INSERT\s+INTO\b|CREATE\b|ALTER\s+TYPE\s+\S+\s+ADD\s+VALUE\b|ALTER\s+TABLE\s+\S+\s+ADD\s+COLUMN\b)',
    re.IGNORECASE,
)
_ALTER_TABLE: Final = re.compile(r'\s*ALTER\s+TABLE\s+\S+\s+(?P<clauses>.*)', re.IGNORECASE | re.DOTALL)
_ADD_COLUMN: Final = re.compile(r'\s*ADD\s+COLUMN\b', re.IGNORECASE)
_NOT_NULL: Final = re.compile(r'\bNOT\s+NULL\b', re.IGNORECASE)
_DEFAULT: Final = re.compile(r'\bDEFAULT\b', re.IGNORECASE)

FIX_HINT: Final = (
    'add a server_default, split the change (expand now, contract in a later release), '
    'or mark the revision with "# additive-exception: <bead-id>"'
)


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    message: str

    def render(self) -> str:
        return f'{self.path}:{self.line}: {self.message}'


def marker_of(source: str) -> str | None:
    """Return the bead id of an additive-exception line, or None.

    A line that starts with the marker, wherever it sits, so a marker inside the module docstring counts
    as much as one written as a comment token.
    """
    found = MARKER_RE.search(source)
    return found.group(1) if found else None


def _kw(call: ast.Call, name: str) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def _is_const(node: ast.expr | None, value: object) -> bool:
    return isinstance(node, ast.Constant) and node.value is value


def _call_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else None


def _module_values(tree: ast.Module) -> dict[str, ast.expr]:
    return {
        target.id: node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }


def _fragment(node: ast.expr, names: dict[str, ast.expr], depth: int) -> str | None:
    """Text of one f-string part: a string as SQL text reads, a number or boolean constant as str()."""
    if depth > 5:
        return None
    if isinstance(node, ast.Name) and node.id in names:
        return _fragment(names[node.id], names, depth + 1)
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
        return str(node.value)
    return _sql_text(node, names, depth)


def _sql_text(node: ast.expr, names: dict[str, ast.expr], depth: int = 0) -> str | None:
    """Return the literal text of an SQL argument, or None when it cannot be read statically."""
    if depth > 5:
        return None
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        parts = []
        for part in node.values:
            if isinstance(part, ast.Constant):
                parts.append(str(part.value))
            elif isinstance(part, ast.FormattedValue):
                resolved = _fragment(part.value, names, depth + 1)
                if resolved is None:
                    return None  # one unreadable part makes the whole statement unreadable
                parts.append(resolved)
        return ''.join(parts)
    if isinstance(node, ast.Name) and node.id in names:
        return _sql_text(names[node.id], names, depth + 1)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _sql_text(node.left, names, depth + 1), _sql_text(node.right, names, depth + 1)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.Call) and _call_name(node) == 'text' and node.args:
        return _sql_text(node.args[0], names, depth + 1)
    return None


def _strip_sql_comments(text: str) -> str | None:
    """Replace each SQL comment outside quotes with one space; None when a comment or quote never ends.

    A line comment runs from two hyphens to the end of the line, a block comment from slash-star to the
    matching star-slash (not nested). Single-quoted text (a doubled quote is an escape, read as a close
    followed by an open) and double-quoted identifiers pass through untouched.
    """
    out, i, quote = [], 0, ''
    while i < len(text):
        pair = text[i : i + 2]
        if quote:
            quote = '' if text[i] == quote else quote
            out.append(text[i])
        elif text[i] in '\'"':
            quote = text[i]
            out.append(text[i])
        elif pair == '--':
            end = text.find('\n', i)
            i = len(text) if end == -1 else end
            out.append(' ')
            continue
        elif pair == '/*':
            end = text.find('*/', i + 2)
            if end == -1:
                return None
            i = end + 2
            out.append(' ')
            continue
        else:
            out.append(text[i])
        i += 1
    return None if quote else ''.join(out)


def _top_level_clauses(text: str) -> list[str]:
    """Split on commas outside parentheses and quotes, so a DEFAULT 'a,b' or numeric(10, 2) stays whole."""
    clauses, depth, quote, start = [], 0, '', 0
    for i, char in enumerate(text):
        if quote:
            quote = '' if char == quote else quote
        elif char in '\'"':
            quote = char
        elif char == '(':
            depth += 1
        elif char == ')':
            depth = max(depth - 1, 0)
        elif char == ',' and depth == 0:
            clauses.append(text[start:i])
            start = i + 1
    clauses.append(text[start:])
    return clauses


def _alter_table_problems(clauses: str) -> Iterator[str]:
    for clause in _top_level_clauses(clauses):
        if not _ADD_COLUMN.match(clause):
            yield 'raw SQL ALTER TABLE has a clause that is not ADD COLUMN'
        elif _NOT_NULL.search(clause) and not _DEFAULT.search(clause):
            yield 'raw SQL adds a NOT NULL column without a DEFAULT'


def _raw_sql_problems(text: str) -> Iterator[str]:
    stripped = _strip_sql_comments(text)
    if stripped is None:
        yield 'raw SQL has an unterminated comment or quote'
        return
    for statement in filter(str.strip, stripped.split(';')):
        if not _ADDITIVE_SQL.match(statement):
            yield 'raw SQL is not a recognised additive statement (UPDATE, INSERT, CREATE, ADD VALUE, ADD COLUMN)'
        elif alter := _ALTER_TABLE.match(statement):
            yield from _alter_table_problems(alter.group('clauses'))


def _check_call(call: ast.Call, names: dict[str, ast.expr]) -> Iterator[tuple[int, str]]:
    name = _call_name(call)
    if name in _ALWAYS_REFUSED:
        yield call.lineno, f'{name} {_ALWAYS_REFUSED[name]}'
    elif name == 'alter_column':
        if _kw(call, 'new_column_name') is not None:
            yield call.lineno, 'alter_column renames a column (new_column_name)'
        if _kw(call, 'type_') is not None:
            yield call.lineno, 'alter_column changes a type (type_), which may narrow it'
        if _is_const(_kw(call, 'nullable'), False):
            yield call.lineno, 'alter_column sets nullable=False'
        if _is_const(_kw(call, 'server_default'), None) and (
            _is_const(_kw(call, 'nullable'), False) or _is_const(_kw(call, 'existing_nullable'), False)
        ):
            yield call.lineno, 'alter_column removes the server_default of a NOT NULL column'
    elif name == 'add_column':
        for arg in [*call.args, *(k.value for k in call.keywords)]:
            if isinstance(arg, ast.Call) and _call_name(arg) == 'Column':
                not_null = _is_const(_kw(arg, 'nullable'), False) or _is_const(_kw(arg, 'primary_key'), True)
                default = _kw(arg, 'server_default')
                if not_null and (default is None or _is_const(default, None)):
                    yield arg.lineno, 'add_column of a NOT NULL column without a server_default'
    elif name in ('execute', 'exec_driver_sql') and call.args:
        text = _sql_text(call.args[0], names)
        if text is None:
            yield call.lineno, 'raw SQL the lint cannot read statically'
        elif _DOLLAR_QUOTE.search(text):
            yield (
                call.lineno,
                (
                    'raw SQL contains a dollar-quote delimiter, which the lint does not read; '
                    'rewrite it without dollar quoting or mark the revision'
                ),
            )
        elif found := _DESTRUCTIVE_SQL.search(text):
            yield call.lineno, f'raw SQL contains {" ".join(found.group(0).upper().split())}'
        else:
            yield from ((call.lineno, message) for message in _raw_sql_problems(text))


def lint_source(path: Path, source: str) -> list[Finding]:
    """Return the findings for one revision's source; empty when it is marked or additive."""
    if marker_of(source) is not None:
        return []
    tree = ast.parse(source, filename=str(path))
    names = _module_values(tree)
    exempt = {
        id(node)
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef) and fn.name == 'downgrade'
        for node in ast.walk(fn)
    }
    return [
        Finding(path, line, f'{message}; {FIX_HINT}')
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and id(node) not in exempt
        for line, message in _check_call(node, names)
    ]


def revision_files(paths: Sequence[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        files.extend(sorted(path.glob('*.py')) if path.is_dir() else [path])
    return [f for f in files if f.name != '__init__.py']


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='python -m tools.migration_lint', description=(__doc__ or '').splitlines()[0])
    parser.add_argument(
        'paths', nargs='*', type=Path, help='revision files or directories (default: the store versions)'
    )
    args = parser.parse_args(argv)
    files = revision_files(args.paths or [VERSIONS_DIR])
    if not files:
        print('migration_lint: no revision file found to lint', file=sys.stderr)
        return 1
    findings = [f for file in files for f in lint_source(file, file.read_text(encoding='utf-8'))]
    for finding in findings:
        print(finding.render(), file=sys.stderr)
    print(f'migration_lint: {len(files)} revision(s), {len(findings)} finding(s)')
    return 1 if findings else 0
