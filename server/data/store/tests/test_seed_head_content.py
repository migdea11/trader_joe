"""The committed head seed matches the models it is loaded into: the P49 amend of f2b7d4a91c36 (tj-7zjov1.143).

THE DESIGN: the amend drops ingest_request.listing_id and adds a nullable ingest_run.vendor_asset_id, and the seed
cast (seeds/ingest_records.py) sends every counted attempt with the vendor asset id except the FAILED one with no
stats, so the seed holds both values of the new column. The seed guard (test_seed_guard.py) and the producer tests
check that a seed exists, is paired, names its revision and holds the cast's row counts; none of them reads the
columns, so a seed produced before a column change passed all of them. This module closes that gap.

RELATIONSHIPS, NOT CONSTANTS (ADR tj-8fxxfb): the column set of every ingest table in the seed, in the SQL and in the
manifest's digests, is compared with the model's own columns; the vendor asset id is compared with the cast's own
constant. The checks are plain functions over the seed text, so each one can be run against an older seed to see it
red.

P34c (tj-7zjov1.123) moved the head to b8e4c1f7a923: push_path_problems reads its seven columns off the head seed.
P35r (tj-7zjov1.153) moved it to 39c28a39b290: expiry_type_problems reads ingest_request.expiry_type off it.
P18 (tj-7zjov1.31) moved it to c7e19a4d5b28: corporate_action_problems reads the observations and their attempt rows.

PR-GATE TIER: files only, no database. That the seed loads and upgrades is tests/system's.
"""

import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable

import pytest

from common.tests.roots import REPO_ROOT
from data.store.app.database.models.corporate_action import CorporateAction
from data.store.app.database.models.ingest_instance import IngestInstance
from data.store.app.database.models.ingest_request import IngestRequest
from data.store.app.database.models.ingest_run import IngestRun
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from data.store.seeds.ingest_records import (
    CORPORATE_ACTION_SYMBOL,
    EXPECTED_ATTEMPT_ROWS,
    EXPECTED_INTERRUPTED_RUNS,
    EXPECTED_LEGACY_RUNS,
    EXPECTED_LEGACY_STATES,
    EXPECTED_OBSERVATIONS,
    EXPECTED_REQUEST_STATES,
    EXPECTED_REVISED_ACTIONS,
    EXPECTED_RUN_ROWS,
    REQUEST_IDS,
    VENDOR_ASSET,
)
from data.store.seeds.manifest import head_revision
from data.store.seeds.producer import VERSIONS_DIR


pytestmark = pytest.mark.data_store

HEAD = head_revision(VERSIONS_DIR)
SEEDS_DIR = REPO_ROOT / 'tests' / 'system' / 'seeds'
MODELS = {model.TABLE_NAME: model.__table__ for model in (IngestInstance, IngestRequest, IngestRun)}

# dump.py renders each row as INSERT INTO public.<table> (<columns>) VALUES (<quote_nullable values>);
_INSERT = re.compile(r'^INSERT INTO public\.(\w+) \(([^)]*)\) VALUES \((.*)\);$')
# quote_nullable gives NULL, '<text>' with '' for a quote, or E'<text>' when the text holds a backslash.
_VALUE = re.compile(r"NULL|E?'(?:[^']|'')*'")


def _values(text: str) -> list[str | None]:
    values = []
    position = 0
    while position < len(text):
        match = _VALUE.match(text, position)
        assert match, f'unreadable value at {position}: {text[position : position + 40]!r}'
        token = match.group(0)
        values.append(None if token == 'NULL' else token[token.index("'") + 1 : -1].replace("''", "'"))
        position = match.end()
        if text.startswith(', ', position):
            position += 2
    return values


def seed_rows(sql: str, tables: Iterable[str] = MODELS) -> dict[str, list[dict[str, str | None]]]:
    """The seed's rows of the named tables (the ingest tables by default), as column to value, from its INSERT lines."""
    rows: dict[str, list[dict[str, str | None]]] = {table: [] for table in tables}
    for line in sql.splitlines():
        match = _INSERT.match(line)
        if match and match[1] in rows:
            columns = match[2].split(', ')
            values = _values(match[3])
            assert len(columns) == len(values), line[:120]
            rows[match[1]].append(dict(zip(columns, values, strict=True)))
    return rows


def column_problems(sql: str, manifest: dict) -> list[str]:
    """Every ingest table's columns, in each INSERT and in the manifest's digests, against the model's."""
    problems = []
    for table, rows in seed_rows(sql).items():
        declared = {column.name for column in MODELS[table].columns}
        if not rows:
            problems.append(f'{table}: no row in the seed')
        for seen in {frozenset(row) for row in rows}:
            if seen != declared:
                problems.append(f'{table} INSERT: extra {sorted(seen - declared)}, missing {sorted(declared - seen)}')
        digested = set(manifest['digests'][table])
        if digested != declared:
            problems.append(
                f'{table} manifest: extra {sorted(digested - declared)}, missing {sorted(declared - digested)}'
            )
    return problems


def vendor_asset_problems(sql: str) -> list[str]:
    """Among the attempt rows, the cast's vendor asset id at least once and NULL at least once, and nothing else."""
    attempts = [row for row in seed_rows(sql)[IngestRun.TABLE_NAME] if row.get('attempt_no') is not None]
    if not attempts:
        return ['ingest_run: no attempt row in the seed']
    if any('vendor_asset_id' not in row for row in attempts):
        return ['ingest_run: the seed has no vendor_asset_id column']
    values = [row['vendor_asset_id'] for row in attempts]
    problems = []
    if VENDOR_ASSET not in values:
        problems.append(f'ingest_run: no attempt row holds {VENDOR_ASSET!r}')
    if None not in values:
        problems.append('ingest_run: no attempt row holds NULL (the cast sends one with no id)')
    if strays := sorted({value for value in values if value not in (VENDOR_ASSET, None)}):
        problems.append(f'ingest_run: vendor_asset_id values the cast never sends: {strays}')
    return problems


# P34c (tj-7zjov1.123, revision b8e4c1f7a923). The seed producer writes through store code, and nothing in it sets the
# request's trigger or lifecycle fields or the entry's stopped cause (ADR tj-26jt9w addendum 13: no seed value), so
# each is NULL on every row. The cast's one counted FAILED report with no stats is the FAILED_NO_STATS request.
NULL_AT_HEAD = {
    IngestRequest.TABLE_NAME: ('trigger', 'expiry', 'collect'),
    StoreDatasetEntry.TABLE_NAME: ('stopped_cause',),
}
SPEND_UNKNOWN_REQUEST = str(REQUEST_IDS['FAILED_NO_STATS'])
# The attempt-row states of a SUCCEEDED report (bars, or none); every other row's served range is NULL.
SERVED_STATES = ('SUCCEEDED', 'EMPTY')


def push_path_problems(sql: str) -> list[str]:
    """The b8e4c1f7a923 columns in the seed, each as the cast writes it.

    Present on every row, NULL where nothing writes them; the unknown-spend counter and the served range where the
    cast's deliveries write them.
    """
    rows = seed_rows(sql, (*MODELS, StoreDatasetEntry.TABLE_NAME))
    problems = []
    for table, columns in NULL_AT_HEAD.items():
        if not rows[table]:
            problems.append(f'{table}: no row in the seed')
        for column in columns:
            if any(column not in row for row in rows[table]):
                problems.append(f'{table}: the seed has no {column} column')
            elif held := sorted({row[column] for row in rows[table] if row[column] is not None}):
                problems.append(f'{table}.{column}: values nothing writes yet: {held}')
    requests = rows[IngestRequest.TABLE_NAME]
    if any('spend_unknown_attempts' not in row for row in requests):
        problems.append('ingest_request: the seed has no spend_unknown_attempts column')
    else:
        counted = {row['id']: row['spend_unknown_attempts'] for row in requests}
        expected = {request_id: '1' if request_id == SPEND_UNKNOWN_REQUEST else '0' for request_id in counted}
        if SPEND_UNKNOWN_REQUEST not in counted:
            problems.append('ingest_request: the FAILED_NO_STATS request is not in the seed')
        elif counted != expected:
            wrong = {key: value for key, value in counted.items() if value != expected[key]}
            problems.append(f'ingest_request.spend_unknown_attempts: {wrong}')
    by_id = {row['id']: row for row in requests}
    runs = rows[IngestRun.TABLE_NAME]
    if any('served_start' not in row or 'served_end' not in row for row in runs):
        problems.append('ingest_run: the seed has no served_start or served_end column')
        return problems
    served = 0
    for row in runs:
        span = (row['served_start'], row['served_end'])
        if row['attempt_no'] is not None and row['state'] in SERVED_STATES:
            served += 1
            # The cast's success serves exactly the window its request asked for.
            request = by_id.get(row['request_id'])
            wanted = (request['requested_start'], request['requested_end']) if request else None
            if span != wanted:
                problems.append(f'ingest_run attempt {row["attempt_no"]} of {row["request_id"]}: served {span}')
        elif span != (None, None):
            problems.append(f'ingest_run {row["id"]} ({row["state"]}): a served range on a row that is no success')
    if served == 0:
        problems.append('ingest_run: no SUCCEEDED or EMPTY attempt row, so the served range is never seeded')
    return problems


def _read(revision: str) -> tuple[str, dict]:
    sql = (SEEDS_DIR / f'{revision}.sql').read_text(encoding='utf-8')
    manifest = json.loads((SEEDS_DIR / f'{revision}.json').read_text(encoding='utf-8'))
    return sql, manifest


def test_the_head_seed_has_exactly_the_models_columns_in_every_ingest_table():
    """No listing_id in ingest_request, vendor_asset_id in ingest_run: in the SQL and in the manifest."""
    sql, manifest = _read(HEAD)
    assert column_problems(sql, manifest) == []
    assert 'listing_id' not in sql
    assert all('listing_id' not in columns for columns in manifest['digests'].values())


def test_the_head_seed_holds_both_values_of_the_attempt_vendor_asset_id():
    sql, _ = _read(HEAD)
    assert vendor_asset_problems(sql) == []


def test_the_head_seed_holds_the_push_path_columns_as_the_cast_writes_them():
    """b8e4c1f7a923: trigger, expiry, collect and stopped_cause NULL on every row; one unknown spend; served ranges.

    Red until the seed is regenerated at the new head (the head seed file does not exist before that).
    """
    sql, _ = _read(HEAD)
    assert push_path_problems(sql) == []


def test_the_push_path_check_is_red_on_the_parent_seed_and_on_wrong_values():
    """Run against the committed f2b7d4a91c36 seed (no such columns), then against one row of each wrong value."""
    parent_sql, _ = _read('f2b7d4a91c36')
    assert set(push_path_problems(parent_sql)) == {
        'ingest_request: the seed has no trigger column',
        'ingest_request: the seed has no expiry column',
        'ingest_request: the seed has no collect column',
        'store_dataset_entry: the seed has no stopped_cause column',
        'ingest_request: the seed has no spend_unknown_attempts column',
        'ingest_run: the seed has no served_start or served_end column',
    }

    other = '5eed0000-0000-4000-8000-000000000099'

    def insert(table: str, values: dict[str, str | None]) -> str:
        rendered = ', '.join('NULL' if value is None else f"'{value}'" for value in values.values())
        return f'INSERT INTO public.{table} ({", ".join(values)}) VALUES ({rendered});'

    request = {'trigger': 'SCHEDULE', 'expiry': None, 'collect': None, 'requested_start': 'a', 'requested_end': 'b'}
    sql = '\n'.join(
        [
            insert('store_dataset_entry', {'id': 'e1', 'stopped_cause': 'X'}),
            insert('ingest_request', {'id': SPEND_UNKNOWN_REQUEST, 'spend_unknown_attempts': '0', **request}),
            insert('ingest_request', {'id': other, 'spend_unknown_attempts': '1', **request}),
            insert(
                'ingest_run',
                {
                    'id': 'r1',
                    'request_id': other,
                    'attempt_no': '1',
                    'state': 'SUCCEEDED',
                    'served_start': 'a',
                    'served_end': 'c',
                },
            ),
            insert(
                'ingest_run',
                {
                    'id': 'r2',
                    'request_id': other,
                    'attempt_no': '2',
                    'state': 'FAILED',
                    'served_start': 'a',
                    'served_end': 'b',
                },
            ),
        ]
    )
    assert push_path_problems(sql) == [
        "ingest_request.trigger: values nothing writes yet: ['SCHEDULE']",
        "store_dataset_entry.stopped_cause: values nothing writes yet: ['X']",
        f"ingest_request.spend_unknown_attempts: {{'{SPEND_UNKNOWN_REQUEST}': '0', '{other}': '1'}}",
        f"ingest_run attempt 1 of {other}: served ('a', 'c')",
        'ingest_run r2 (FAILED): a served range on a row that is no success',
    ]


# P35r (tj-7zjov1.153, revision 39c28a39b290). The seed cast (seeds/ingest_records.py) is not changed by the bead and
# nothing before P35 writes the column, so the head seed holds expiry_type NULL on every ingest_request row.
EXPIRY_TYPE_COLUMN = 'expiry_type'


def expiry_type_problems(sql: str) -> list[str]:
    """ingest_request.expiry_type present on every request row of the seed and NULL on each."""
    requests = seed_rows(sql)[IngestRequest.TABLE_NAME]
    if not requests:
        return ['ingest_request: no row in the seed']
    if any(EXPIRY_TYPE_COLUMN not in row for row in requests):
        return [f'ingest_request: the seed has no {EXPIRY_TYPE_COLUMN} column']
    if held := sorted({row[EXPIRY_TYPE_COLUMN] for row in requests if row[EXPIRY_TYPE_COLUMN] is not None}):
        return [f'ingest_request.{EXPIRY_TYPE_COLUMN}: values nothing writes yet: {held}']
    return []


def test_the_head_seed_holds_expiry_type_null_on_every_request_row():
    """39c28a39b290 (tj-7zjov1.153): the column is in the head seed and NULL on every ingest_request row.

    Red until the seed is regenerated at the new head (the head seed file does not exist before that).
    """
    sql, _ = _read(HEAD)
    assert expiry_type_problems(sql) == []


def test_the_expiry_type_check_is_red_on_the_parent_seed_and_on_a_value():
    """Against the committed b8e4c1f7a923 seed (no such column), then one row holding a value and one with no rows."""
    parent_sql, _ = _read('b8e4c1f7a923')
    assert expiry_type_problems(parent_sql) == ['ingest_request: the seed has no expiry_type column']

    rows = [
        "INSERT INTO public.ingest_request (id, expiry_type) VALUES ('r1', NULL);",
        "INSERT INTO public.ingest_request (id, expiry_type) VALUES ('r2', '2');",
    ]
    assert expiry_type_problems('\n'.join(rows)) == ["ingest_request.expiry_type: values nothing writes yet: ['2']"]
    assert expiry_type_problems(rows[0]) == []
    assert expiry_type_problems('') == ['ingest_request: no row in the seed']


def test_the_value_parser_reads_null_quotes_and_escape_strings():
    """The checks above are only as good as this reading of dump.py's quote_nullable output."""
    assert _values("NULL, 'a, b', 'it''s', E'x\\\\y', ''") == [None, 'a, b', "it's", 'x\\\\y', '']


def test_the_checks_are_red_on_a_seed_from_before_the_amend():
    """A synthetic pre-amend seed: listing_id on the request, no vendor_asset_id on the run."""
    request = {column.name for column in MODELS[IngestRequest.TABLE_NAME].columns} | {'listing_id'}
    run = {column.name for column in MODELS[IngestRun.TABLE_NAME].columns} - {'vendor_asset_id'}

    def insert(table: str, columns: set[str]) -> str:
        ordered = sorted(columns)
        values = ', '.join("'1'" if name == 'attempt_no' else 'NULL' for name in ordered)
        return f'INSERT INTO public.{table} ({", ".join(ordered)}) VALUES ({values});'

    instance = {column.name for column in MODELS[IngestInstance.TABLE_NAME].columns}
    sql = '\n'.join(
        [
            insert(IngestInstance.TABLE_NAME, instance),
            insert(IngestRequest.TABLE_NAME, request),
            insert(IngestRun.TABLE_NAME, run),
        ]
    )
    manifest = {
        'digests': {
            IngestInstance.TABLE_NAME: dict.fromkeys(instance, ''),
            IngestRequest.TABLE_NAME: dict.fromkeys(request, ''),
            IngestRun.TABLE_NAME: dict.fromkeys(run, ''),
        }
    }

    assert column_problems(sql, manifest) == [
        "ingest_request INSERT: extra ['listing_id'], missing []",
        "ingest_request manifest: extra ['listing_id'], missing []",
        "ingest_run INSERT: extra [], missing ['vendor_asset_id']",
        "ingest_run manifest: extra [], missing ['vendor_asset_id']",
    ]
    assert vendor_asset_problems(sql) == ['ingest_run: the seed has no vendor_asset_id column']


# tj-7zjov1.31 (P18, revision c7e19a4d5b28). The cast (seeds/ingest_records.py) adds two corporate-actions requests on
# ZZSEEDCA: the first delivers action A and action B, the second (an overlapping window) A again with the SAME bytes
# (the dedupe: no row) and A with OTHER bytes (a vendor revision: a new row). So the head seed holds three observations,
# one vendor id under two payloads, and two corporate-actions attempt rows with the symbol and NULL for the rest.
ACTION_MODEL = CorporateAction.__table__
ACTION_LABELS_NULL = ('asset_type', 'data_type', 'granularity', 'update_type', 'vendor_asset_id', 'feed')


def _bytes_of(text: str | None) -> bytes | None:
    """A bytea value as dump.py renders it (an escape string of backslashes, x, then hex), as _values reads it."""
    if text is None:
        return None
    return bytes.fromhex(text.lstrip('\\').removeprefix('x'))


def corporate_action_problems(sql: str, manifest: dict) -> list[str]:
    """The observations and the corporate-actions attempt rows in the seed, as the cast writes them."""
    table = ACTION_MODEL.name
    rows = seed_rows(sql, (table, IngestRun.TABLE_NAME, IngestRequest.TABLE_NAME))
    observations = rows[table]
    if not observations:
        return [f'{table}: no row in the seed']
    problems = []
    declared = {column.name for column in ACTION_MODEL.columns}
    for seen in {frozenset(row) for row in observations}:
        if seen != declared:
            problems.append(f'{table} INSERT: extra {sorted(seen - declared)}, missing {sorted(declared - seen)}')
    digested = set(manifest['digests'].get(table, {}))
    if digested != declared:
        problems.append(f'{table} manifest: extra {sorted(digested - declared)}, missing {sorted(declared - digested)}')
    if problems:
        return problems
    if manifest['row_counts'].get(table) != EXPECTED_OBSERVATIONS or len(observations) != EXPECTED_OBSERVATIONS:
        problems.append(f'{table}: {len(observations)} rows; expected {EXPECTED_OBSERVATIONS}')
    for row in observations:
        raw = _bytes_of(row['raw_payload'])
        if raw is None or hashlib.sha256(raw).hexdigest() != row['payload_hash']:
            problems.append(f'{table} {row["vendor_action_id"]}: payload_hash is not the SHA-256 of raw_payload')
        if row['requesting_symbol'] != CORPORATE_ACTION_SYMBOL:
            problems.append(f'{table} {row["vendor_action_id"]}: requesting_symbol {row["requesting_symbol"]!r}')
    keys = [(row['source'], row['vendor_action_id'], row['payload_hash']) for row in observations]
    if len(set(keys)) != len(keys):
        problems.append(f'{table}: an observation is held twice (the dedupe key repeats)')
    per_id = Counter((row['source'], row['vendor_action_id']) for row in observations)
    if sum(1 for count in per_id.values() if count > 1) != EXPECTED_REVISED_ACTIONS:
        problems.append(f'{table}: payloads per vendor id {sorted(per_id.values())}; expected one revised action')
    actions_requests = {row['id'] for row in rows[IngestRequest.TABLE_NAME] if row['kind'] == 'CORPORATE_ACTIONS'}
    attempts = [row for row in rows[IngestRun.TABLE_NAME] if row['request_id'] in actions_requests]
    if not attempts or len(attempts) != len(actions_requests):
        problems.append(f'ingest_run: {len(attempts)} corporate-actions attempt rows for {len(actions_requests)}')
    for row in attempts:
        if row['asset_symbol'] != CORPORATE_ACTION_SYMBOL:
            problems.append(f'ingest_run {row["id"]}: asset_symbol {row["asset_symbol"]!r}')
        if held := [column for column in ACTION_LABELS_NULL if row[column] is not None]:
            problems.append(f'ingest_run {row["id"]}: not NULL on a corporate-actions attempt: {held}')
        if (row['state'], row['bars_written']) != ('SUCCEEDED', '0'):
            problems.append(f'ingest_run {row["id"]}: {row["state"]} with {row["bars_written"]} bars')
    return problems


def test_the_head_seed_holds_the_corporate_action_cast():
    """c7e19a4d5b28: three observations (A, B, A revised), hashes over the bytes, two attempt rows, NULL labels."""
    sql, manifest = _read(HEAD)
    assert corporate_action_problems(sql, manifest) == []


def test_the_head_seed_holds_the_cast_alone_in_states_attempt_rows_and_legacy_runs():
    """The producer empties the POSTs' own ingest rows before the cast, so the seed holds the cast's counts only.

    Ten attempt rows and, since e5d950e, four legacy-shaped runs (no request id, no attempt number; ZZSEEDLA to
    ZZSEEDLD): one swept to FAILED / INTERRUPTED, one SUCCEEDED, one EMPTY, one FAILED with a typed reason.
    """
    sql, manifest = _read(HEAD)
    rows = seed_rows(sql)
    assert dict(Counter(row['state'] for row in rows[IngestRequest.TABLE_NAME])) == EXPECTED_REQUEST_STATES
    runs = rows[IngestRun.TABLE_NAME]
    attempts = [row for row in runs if row['attempt_no'] is not None and row['request_id'] is not None]
    legacy = [row for row in runs if row['attempt_no'] is None and row['request_id'] is None]
    assert len(attempts) == EXPECTED_ATTEMPT_ROWS
    assert dict(Counter(row['state'] for row in legacy)) == EXPECTED_LEGACY_STATES
    assert len(legacy) == EXPECTED_LEGACY_RUNS
    assert sum(1 for row in legacy if row['error_reason'] == 'INTERRUPTED') == EXPECTED_INTERRUPTED_RUNS
    assert all(row['asset_symbol'].startswith('ZZSEEDL') for row in legacy)
    assert len(runs) == manifest['row_counts'][IngestRun.TABLE_NAME] == EXPECTED_RUN_ROWS


def test_the_corporate_action_check_is_red_on_the_parent_seed_and_on_a_wrong_hash():
    parent_sql, parent_manifest = _read('39c28a39b290')
    assert corporate_action_problems(parent_sql, parent_manifest) == ['corporate_action: no row in the seed']

    sql, manifest = _read(HEAD)
    first = next(line for line in sql.splitlines() if line.startswith('INSERT INTO public.corporate_action '))
    (row,) = seed_rows(first, (ACTION_MODEL.name,))[ACTION_MODEL.name]
    tampered = sql.replace(row['payload_hash'], '0' * 64, 1)
    assert f'corporate_action {row["vendor_action_id"]}: payload_hash is not the SHA-256 of raw_payload' in (
        corporate_action_problems(tampered, manifest)
    )
