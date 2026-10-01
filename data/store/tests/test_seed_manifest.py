"""The seed manifest (data/store/seeds/manifest.py): the head revision, the digest query, the rendering.

WHY THIS FILE EXISTS (validator, tj-vhboky.60). head_revision() is the producer's refusal to write a
seed under the wrong name: a seed filed under a revision it was not produced at passes the seed
guard (tj-irhy0a.4) and fails Sys-8 (tj-vhboky.62) far from the cause. It reads the revision files
without alembic, so it is checked here AGAINST alembic, on the real chain and on hand-made ones.

WHAT TIER THIS IS. Pure. column_digest_sql's text is checked for the properties the documented
algorithm depends on (by-name, sorted, quoted, NULL-marked, id-ordered); the query itself cannot run
here -- NOT RUN until the MCP sitting tj-c4mosr.6. NOT PROVED HERE: that Postgres computes the digest
the module docstring describes (Sys-8 recomputes it against a loaded seed).
"""

import json
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory

from data.store.seeds.manifest import (
    PRODUCER,
    SESSION_OPTIONS,
    HeadRevisionError,
    build_manifest,
    column_digest_sql,
    head_revision,
    render_manifest,
)
from data.store.seeds.producer import REPO_ROOT, VERSIONS_DIR


pytestmark = pytest.mark.data_store

MIGRATIONS_DIR = REPO_ROOT / 'data' / 'store' / 'migrations'


# ---------------------------------------------------------------------------------------------
# head_revision
# ---------------------------------------------------------------------------------------------


def test_the_producer_reads_the_real_versions_directory():
    assert VERSIONS_DIR == MIGRATIONS_DIR / 'versions'
    assert VERSIONS_DIR.is_dir()


def test_the_head_agrees_with_alembic():
    """A relationship, not a literal: whatever the chain's head is when this runs."""
    assert [head_revision(VERSIONS_DIR)] == list(ScriptDirectory(str(MIGRATIONS_DIR)).get_heads())


def _revision_file(directory: Path, name: str, revision: str, down: str) -> None:
    (directory / f'{name}.py').write_text(
        f'from typing import Union\n\nrevision: str = {revision!r}\ndown_revision: Union[str, None] = {down}\n',
        encoding='utf-8',
    )


def test_a_linear_chain_has_its_last_revision_as_head(tmp_path):
    _revision_file(tmp_path, 'a', 'aaa', 'None')
    _revision_file(tmp_path, 'b', 'bbb', "'aaa'")
    _revision_file(tmp_path, 'c', 'ccc', "'bbb'")
    assert head_revision(tmp_path) == 'ccc'


def test_plain_assignments_count_too(tmp_path):
    (tmp_path / 'a.py').write_text("revision = 'aaa'\ndown_revision = None\n", encoding='utf-8')
    (tmp_path / 'b.py').write_text("revision = 'bbb'\ndown_revision = 'aaa'\n", encoding='utf-8')
    assert head_revision(tmp_path) == 'bbb'


def test_a_branched_chain_is_refused(tmp_path):
    _revision_file(tmp_path, 'a', 'aaa', 'None')
    _revision_file(tmp_path, 'b', 'bbb', "'aaa'")
    _revision_file(tmp_path, 'c', 'ccc', "'aaa'")
    with pytest.raises(HeadRevisionError, match='found 2'):
        head_revision(tmp_path)


def test_a_merge_revision_closes_the_branch(tmp_path):
    """down_revision as a tuple names both parents."""
    _revision_file(tmp_path, 'a', 'aaa', 'None')
    _revision_file(tmp_path, 'b', 'bbb', "'aaa'")
    _revision_file(tmp_path, 'c', 'ccc', "'aaa'")
    _revision_file(tmp_path, 'm', 'mmm', "('bbb', 'ccc')")
    assert head_revision(tmp_path) == 'mmm'


def test_an_empty_directory_is_refused(tmp_path):
    with pytest.raises(HeadRevisionError, match='found 0'):
        head_revision(tmp_path)


def test_a_cycle_has_no_head_and_is_refused(tmp_path):
    _revision_file(tmp_path, 'a', 'aaa', "'bbb'")
    _revision_file(tmp_path, 'b', 'bbb', "'aaa'")
    with pytest.raises(HeadRevisionError):
        head_revision(tmp_path)


def test_files_without_a_literal_revision_are_ignored(tmp_path):
    _revision_file(tmp_path, 'a', 'aaa', 'None')
    (tmp_path / '__init__.py').write_text('', encoding='utf-8')
    (tmp_path / 'helper.py').write_text('revision = compute()\nX = 1\n', encoding='utf-8')
    (tmp_path / 'notes.txt').write_text("revision = 'zzz'\n", encoding='utf-8')
    assert head_revision(tmp_path) == 'aaa'


# ---------------------------------------------------------------------------------------------
# column_digest_sql
# ---------------------------------------------------------------------------------------------


def _selects(sql: str) -> list[str]:
    assert sql.endswith(' ORDER BY name;')
    return sql.removesuffix(' ORDER BY name;').split(' UNION ALL ')


def test_one_select_per_column_sorted_by_name_whatever_the_input_order():
    """By NAME, alphabetically: a downgrade reorders columns (tj-uxl817) and the digest must not move."""
    forward = column_digest_sql('stock_market_activity', ['id', 'close', 'dataset_id'])
    backward = column_digest_sql('stock_market_activity', ['dataset_id', 'close', 'id'])
    assert forward == backward
    names = [select.split("'")[1] for select in _selects(forward)]
    assert names == ['close', 'dataset_id', 'id']


def test_each_select_follows_the_documented_algorithm():
    (select,) = _selects(column_digest_sql('store_dataset_entry', ['end']))
    assert select.startswith("SELECT 'end' AS name, ")
    # 'n' for NULL, else 'v' plus the text cast; newline-joined in id order; '' for no rows; hex sha256.
    assert """CASE WHEN "end" IS NULL THEN 'n' ELSE 'v' || "end"::text END""" in select
    assert "E'\\n' ORDER BY id)" in select
    assert 'coalesce(string_agg(' in select and ", '')" in select
    assert 'encode(sha256(convert_to(' in select and "'UTF8')), 'hex')" in select
    assert select.endswith(' FROM "store_dataset_entry"')


def test_identifiers_are_quoted_and_literals_escaped():
    """Names come from information_schema, not from a constant: a quote in one must not escape."""
    (select,) = _selects(column_digest_sql('we"ird', ['o\'dd"col']))
    assert "SELECT 'o''dd\"col' AS name" in select
    assert '"o\'dd""col"' in select
    assert select.endswith(' FROM "we""ird"')


def test_the_session_options_fix_the_zone_and_the_float_format():
    """The digest casts timestamptz and float to text; both renderings depend on these settings."""
    assert '-c TimeZone=UTC' in SESSION_OPTIONS
    assert '-c extra_float_digits=1' in SESSION_OPTIONS


# ---------------------------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------------------------


def test_the_manifest_carries_exactly_the_designed_fields():
    """tj-vhboky.55 SEED FORMAT: revision, producer, date, per-table row counts, per-table digests."""
    manifest = build_manifest('rev', '2026-09-30', {'t': 1}, {'t': {'c': 'd'}})
    assert manifest == {
        'revision': 'rev',
        'producer': PRODUCER,
        'date': '2026-09-30',
        'row_counts': {'t': 1},
        'digests': {'t': {'c': 'd'}},
    }


def test_rendering_is_insertion_order_independent_and_ends_in_one_newline():
    a = build_manifest('rev', 'd', {'b': 2, 'a': 1}, {'b': {'y': '2', 'x': '1'}, 'a': {}})
    b = build_manifest('rev', 'd', {'a': 1, 'b': 2}, {'a': {}, 'b': {'x': '1', 'y': '2'}})
    assert render_manifest(a) == render_manifest(b)
    text = render_manifest(a)
    assert text.endswith('}\n') and not text.endswith('\n\n')
    assert json.loads(text) == a


def test_the_date_is_the_only_field_a_rerun_changes():
    """What tj-vhboky.65's determinism check compares: pin --date and the manifests are equal bytes."""
    first = render_manifest(build_manifest('rev', '2026-09-30', {'t': 1}, {'t': {'c': 'd'}}))
    second = render_manifest(build_manifest('rev', '2026-10-01', {'t': 1}, {'t': {'c': 'd'}}))
    differing = [pair for pair in zip(first.splitlines(), second.splitlines(), strict=True) if pair[0] != pair[1]]
    assert differing == [('  "date": "2026-09-30",', '  "date": "2026-10-01",')]
