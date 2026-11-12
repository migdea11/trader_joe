"""The schema of a Postgres database as its catalogue reports it, and a diff of two such snapshots (bead tj-7zjov1.99).

No test functions here. Read by the system suite's alpha baseline check (tests/system/test_alpha_baseline_schema_pg.py,
on Postgres: the schema `alembic upgrade head` builds on an empty database must equal the one metadata.create_all
builds, ADR tj-kg521d B5), by the one-time old-chain versus baseline equivalence proof (ADR tj-kg521d section 4, run
by hand from a scratch script, never committed), and by the PR-gate unit test of the diff
(server/data/store/tests/test_schema_catalog_diff.py, on synthetic snapshots). It sits in common/tests, not
data/store/tests, because the test client container mounts server/common and not server/data/store/tests
(docker-compose.test-client.yaml). It was written for the downgrade leg of the old migration chain, which went with
that chain; the AllowedDifference machinery stays for a later comparison that has documented differences.

THE CATALOGUE, NOT THE MODELS. Every fact is read from pg_catalog with Postgres's own renderers (format_type,
pg_get_expr, pg_get_constraintdef, pg_get_indexdef, pg_get_triggerdef), never by reflecting the SQLAlchemy models of
the code under test: the question is what the revisions BUILD, and a model-based comparison would compare the code
with itself. alembic check is no substitute either -- it ignores index predicates, enum labels, CHECKs and server
defaults, which are exactly what a hand-written revision gets wrong.

WHAT IS COMPARED, per category (CATEGORIES), each a mapping from an object's identity to its facts:
  tables       (name) -> relkind, for tables, partitioned tables, views, materialised views, foreign tables
  columns      (table, column) -> type with modifiers, NOT NULL, default expression, identity, generated
  constraints  (table, name) -> kind, definition
  indexes      (table, name) -> definition (uniqueness, columns, method), predicate
  enums        (type) -> labels in sort order
  sequences    (name) -> type, start, increment, min, max, cycle, owning table and column
  types        (type) -> kind, definition, for domains, ranges and standalone composite types
  triggers     (table, name) -> definition
  functions    (name, identity arguments) -> result, kind, a digest of the source
  extensions   (name) -> version
WHAT IS NOT, ON PURPOSE: a column's ordinal position (a downgrade that drops and re-adds a column legitimately moves
it, and nothing in the service reads by position); object OIDs; sequence values; row contents; and the
alembic_version table, which is alembic's bookkeeping, not the revision's schema, and does not exist at all on a
database never migrated (the base revision's parent).
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection


VERSION_TABLE = 'alembic_version'

# relkinds that carry columns: ordinary, partitioned, view, materialised view, foreign.
_RELKINDS = "('r', 'p', 'v', 'm', 'f')"

# category -> (number of leading columns forming the identity, query). Every query reads schema public only,
# leaves alembic_version out, and returns the identity columns first.
CATALOGUE_QUERIES: dict[str, tuple[int, str]] = {
    'tables': (
        1,
        'SELECT c.relname, c.relkind::text FROM pg_class c '
        f"WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN {_RELKINDS} AND c.relname <> :version",
    ),
    'columns': (
        2,
        'SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, '
        'pg_get_expr(d.adbin, d.adrelid), a.attidentity::text, a.attgenerated::text '
        'FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid '
        'LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum '
        f"WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN {_RELKINDS} AND c.relname <> :version "
        'AND a.attnum > 0 AND NOT a.attisdropped',
    ),
    'constraints': (
        2,
        'SELECT c.relname, con.conname, con.contype::text, pg_get_constraintdef(con.oid) '
        'FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid '
        "WHERE con.connamespace = 'public'::regnamespace AND c.relname <> :version",
    ),
    'indexes': (
        2,
        'SELECT t.relname, i.relname, pg_get_indexdef(x.indexrelid), pg_get_expr(x.indpred, x.indrelid) '
        'FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid JOIN pg_class t ON t.oid = x.indrelid '
        "WHERE t.relnamespace = 'public'::regnamespace AND t.relname <> :version",
    ),
    'enums': (
        1,
        'SELECT t.typname, array_agg(e.enumlabel::text ORDER BY e.enumsortorder) '
        'FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid '
        "WHERE t.typnamespace = 'public'::regnamespace GROUP BY t.typname",
    ),
    'sequences': (
        1,
        'SELECT s.relname, format_type(q.seqtypid, NULL), q.seqstart, q.seqincrement, q.seqmin, q.seqmax, '
        'q.seqcycle, owner_table.relname, owner_column.attname '
        'FROM pg_sequence q JOIN pg_class s ON s.oid = q.seqrelid '
        "LEFT JOIN pg_depend dep ON dep.objid = s.oid AND dep.classid = 'pg_class'::regclass "
        "AND dep.refclassid = 'pg_class'::regclass AND dep.deptype IN ('a', 'i') "
        'LEFT JOIN pg_class owner_table ON owner_table.oid = dep.refobjid '
        'LEFT JOIN pg_attribute owner_column ON owner_column.attrelid = dep.refobjid '
        'AND owner_column.attnum = dep.refobjsubid '
        "WHERE s.relnamespace = 'public'::regnamespace",
    ),
    'types': (
        1,
        'SELECT t.typname, t.typtype::text, '
        "CASE t.typtype WHEN 'd' THEN format_type(t.typbasetype, t.typtypmod) "
        "WHEN 'r' THEN format_type(r.rngsubtype, NULL) "
        "ELSE (SELECT string_agg(a.attname || ' ' || format_type(a.atttypid, a.atttypmod), ', ' ORDER BY a.attnum) "
        'FROM pg_attribute a WHERE a.attrelid = t.typrelid AND a.attnum > 0 AND NOT a.attisdropped) END, '
        't.typnotnull, t.typdefault '
        'FROM pg_type t LEFT JOIN pg_range r ON r.rngtypid = t.oid LEFT JOIN pg_class c ON c.oid = t.typrelid '
        "WHERE t.typnamespace = 'public'::regnamespace "
        "AND (t.typtype IN ('d', 'r') OR (t.typtype = 'c' AND c.relkind = 'c'))",
    ),
    'triggers': (
        2,
        'SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid '
        "WHERE NOT t.tgisinternal AND c.relnamespace = 'public'::regnamespace AND c.relname <> :version",
    ),
    'functions': (
        2,
        'SELECT p.proname, pg_get_function_identity_arguments(p.oid), pg_get_function_result(p.oid), '
        "p.prokind::text, md5(coalesce(p.prosrc, '')) FROM pg_proc p "
        "WHERE p.pronamespace = 'public'::regnamespace",
    ),
    'extensions': (1, 'SELECT e.extname, e.extversion FROM pg_extension e'),
}

CATEGORIES = tuple(CATALOGUE_QUERIES)

Catalogue = dict[str, dict[tuple[Any, ...], tuple[Any, ...]]]


def _plain(value: Any) -> Any:
    """A list (an aggregated array from the driver) as a tuple, so every fact is hashable and compares by value."""
    return tuple(value) if isinstance(value, list) else value


def catalogue_from_rows(rows_by_category: Mapping[str, Iterable[Iterable[Any]]]) -> Catalogue:
    """Build a snapshot from each category's rows: identity columns first, as CATALOGUE_QUERIES returns them.

    Every category must be present, none extra, and an identity may appear once only: a duplicate would mean the
    query joined something twice, and keeping the last row would hide the difference the diff exists to find.
    """
    missing, extra = set(CATEGORIES) - set(rows_by_category), set(rows_by_category) - set(CATEGORIES)
    assert not missing and not extra, f'catalogue categories: missing {sorted(missing)}, unknown {sorted(extra)}'
    catalogue: Catalogue = {}
    for category, (width, _) in CATALOGUE_QUERIES.items():
        facts: dict[tuple[Any, ...], tuple[Any, ...]] = {}
        for row in rows_by_category[category]:
            values = tuple(_plain(value) for value in row)
            key, value = values[:width], values[width:]
            assert key not in facts, f'catalogue {category}: {key} appears twice'
            facts[key] = value
        catalogue[category] = facts
    return catalogue


def read_catalogue(conn: Connection) -> Catalogue:
    """The live schema of the database `conn` is connected to, per CATEGORIES."""
    return catalogue_from_rows(
        {
            category: [tuple(row) for row in conn.execute(sa.text(query), {'version': VERSION_TABLE})]
            for category, (_, query) in CATALOGUE_QUERIES.items()
        }
    )


def diff_catalogues(
    expected: Catalogue, actual: Catalogue, *, expected_label: str = 'expected', actual_label: str = 'actual'
) -> list[str]:
    """Every difference between two snapshots, one line each, sorted; an empty list means they are identical.

    An object present on one side only is named with the side that has it; an object on both whose facts differ
    is named with both sets of facts.
    """
    lines: list[str] = []
    for category in CATEGORIES:
        want, got = expected.get(category, {}), actual.get(category, {})
        for key in sorted(set(want) | set(got), key=repr):
            if key not in got:
                lines.append(f'{category} {key}: only in {expected_label} {want[key]}')
            elif key not in want:
                lines.append(f'{category} {key}: only in {actual_label} {got[key]}')
            elif want[key] != got[key]:
                lines.append(f'{category} {key}: {expected_label} {want[key]} but {actual_label} {got[key]}')
    return lines


# The facts of a category by name, in the order CATALOGUE_QUERIES returns them after the identity. Only the
# categories an AllowedDifference may name are listed; widen it with the query, never apart from it.
FACT_NAMES: dict[str, tuple[str, ...]] = {'columns': ('type', 'not_null', 'default', 'identity', 'generated')}


@dataclass(frozen=True)
class AllowedDifference:
    """One fact of one object that is documented to differ: `expected` on the expected side, `actual` on the other.

    Both values are stated, not only the fact's name, so the allowance covers exactly that change: a NOT NULL that
    went from True to False, never "nullability may differ".
    """

    category: str
    key: tuple[Any, ...]
    fact: str
    expected: Any
    actual: Any


def allowed_difference_problems(allowed: Sequence[AllowedDifference]) -> list[str]:
    """Why a set of allowed differences is malformed, one line each; empty when it is usable.

    An empty set is malformed: an allowance with nothing in it would make the comparison an exact one under a
    misleading name. So is an entry naming an unknown fact, one whose two values are equal (it allows nothing),
    or the same fact twice.
    """
    if not allowed:
        return ['no allowed differences are listed']
    problems: list[str] = []
    seen: set[tuple[str, tuple[Any, ...], str]] = set()
    for item in allowed:
        names = FACT_NAMES.get(item.category, ())
        if item.fact not in names:
            problems.append(f'{item.category} {item.key}: {item.fact!r} is not a fact of {item.category!r} {names}')
        if item.expected == item.actual:
            problems.append(f'{item.category} {item.key} {item.fact}: both sides are {item.expected!r}')
        identity = (item.category, item.key, item.fact)
        if identity in seen:
            problems.append(f'{item.category} {item.key} {item.fact}: listed twice')
        seen.add(identity)
    return problems


def unexplained_differences(
    expected: Catalogue,
    actual: Catalogue,
    allowed: Sequence[AllowedDifference],
    *,
    expected_label: str = 'expected',
    actual_label: str = 'actual',
) -> list[str]:
    """Every way the two snapshots fail to differ by exactly `allowed`, one line each; empty means exactly that.

    Each allowed difference must be present as stated -- the object on both sides, the fact at `expected` on the
    expected side and at `actual` on the other -- or it is reported as not seen. Then the expected snapshot, with
    those facts set to their `actual` values, must equal `actual` with diff_catalogues: any other difference in any
    category, however small, is reported as diff_catalogues reports it.
    """
    problems = allowed_difference_problems(allowed)
    if problems:
        raise ValueError('malformed allowed differences: ' + '; '.join(problems))
    lines: list[str] = []
    patched: Catalogue = {category: dict(facts) for category, facts in expected.items()}
    for item in allowed:
        index = FACT_NAMES[item.category].index(item.fact)
        want = expected.get(item.category, {}).get(item.key)
        got = actual.get(item.category, {}).get(item.key)
        if want is None or got is None or want[index] != item.expected or got[index] != item.actual:
            lines.append(
                f'allowed {item.category} {item.key} {item.fact} {item.expected!r} -> {item.actual!r} not seen: '
                f'{expected_label} {want} {actual_label} {got}'
            )
            continue
        patched[item.category][item.key] = (*want[:index], item.actual, *want[index + 1 :])
    lines.extend(diff_catalogues(patched, actual, expected_label=expected_label, actual_label=actual_label))
    return lines
