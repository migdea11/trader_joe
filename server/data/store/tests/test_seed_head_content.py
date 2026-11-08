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
P20s2 (tj-7zjov1.196) put the panel cast into the b6d1f93a7c42 seed: panel_problems reads the five panel tables.

PR-GATE TIER: files only, no database. That the seed loads and upgrades is tests/system's.
"""

import hashlib
import json
import re
import uuid
from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timedelta

import pytest

from common.tests.roots import REPO_ROOT
from data.store.app.database.models.asset_list import AssetListEvent, AssetListRow, AssetListSnapshot
from data.store.app.database.models.corporate_action import CorporateAction
from data.store.app.database.models.ingest_instance import IngestInstance
from data.store.app.database.models.ingest_request import IngestRequest
from data.store.app.database.models.ingest_run import IngestRun
from data.store.app.database.models.panel import (
    Panel,
    PanelMemberTransition,
    PanelPass,
    PanelSettingsVersion,
    PanelSourceSettingsVersion,
)
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from data.store.seeds.dump import SEED_TIMESTAMP
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
from data.store.seeds.panel_records import (
    ABSENT_DATASET_ID,
    EXPECTED_DRY_RUN_PASSES,
    EXPECTED_PANELS,
    EXPECTED_PASS_KINDS,
    EXPECTED_PASSES,
    EXPECTED_SETTINGS_VERSIONS,
    EXPECTED_SOURCE_SETTINGS_VERSIONS,
    EXPECTED_TIED_MEMBERS,
    EXPECTED_TRANSITION_ROWS,
    EXPECTED_TRANSITIONS,
    EXPECTED_UNFINISHED_PASSES,
    PANEL_NAME_PATTERN,
)
from data.store.seeds.producer import VERSIONS_DIR
from data.store.seeds.scenario import OWNER_PATTERN, SYMBOL_PATTERN


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
    # tj-7zjov1.30: the asset-list cast is written through new_asset_list_request, which writes trigger SCHEDULE (the
    # world manager's), so those rows carry it and every other request still holds NULL.
    asset_lists = [row for row in rows[IngestRequest.TABLE_NAME] if row.get('kind') == 'ASSET_LIST']
    if held := sorted({row.get('trigger') for row in asset_lists} - {'SCHEDULE'}, key=str):
        problems.append(f'ingest_request.trigger on an asset-list row: {held}, expected SCHEDULE')
    if any(row.get('expiry') is not None or row.get('collect') is not None for row in asset_lists):
        problems.append('ingest_request: an asset-list row carries a lifecycle field')
    for table, columns in NULL_AT_HEAD.items():
        judged = [row for row in rows[table] if row.get('kind') != 'ASSET_LIST']
        if not judged:
            problems.append(f'{table}: no row in the seed')
        for column in columns:
            if any(column not in row for row in rows[table]):
                problems.append(f'{table}: the seed has no {column} column')
            elif held := sorted({row[column] for row in judged if row[column] is not None}):
                problems.append(f'{table}.{column}: values nothing writes yet: {held}')
    requests = rows[IngestRequest.TABLE_NAME]
    asset_list_ids = {row['id'] for row in asset_lists}
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
        if row['request_id'] in asset_list_ids:
            # An asset list has no window (the 21:56 UTC ruling on tj-7zjov1.30): no served range on its attempt rows.
            if span != (None, None):
                problems.append(f'ingest_run {row["id"]}: a served range on an asset-list attempt')
            continue
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


# --------------------------------------------------------------------------------------------- the asset-list cast
#
# tj-7zjov1.30 (P17, revision a3f8d2c6e914). The cast (seeds/ingest_records.py) writes five asset-list requests through
# new_asset_list_request and delivers each with the report AssetListAttemptRunner builds: active 1 (a1 a2 a3), inactive
# 1 (i1, sharing a1's symbol: the s1 shape), active 2 (a1 a4), inactive 2 (i1, a2 renamed, i2), and a served empty
# active list. The events the design derives from that, by vendor id, are written out here from the DESIGN (not read off
# the producer): appeared a4 on active 2 and i2 on inactive 2; vanished a2 and a3 on active 2; one status_flip of a2 on
# inactive 2 carrying both symbols; nothing from a first snapshot of a list or from the empty one.
ASSET_LIST_MODELS = {model.TABLE_NAME: model.__table__ for model in (AssetListSnapshot, AssetListRow, AssetListEvent)}
EXPECTED_ASSET_LIST_EVENTS = {
    # (request name of the snapshot, kind, vendor id, symbol, previous symbol, request name of the base)
    ('AL_ACTIVE_2', 'appeared', 'seed-asset-a4', 'ZZSEEDXD', None, 'AL_ACTIVE_1'),
    ('AL_ACTIVE_2', 'vanished', 'seed-asset-a2', 'ZZSEEDXB', None, 'AL_ACTIVE_1'),
    ('AL_ACTIVE_2', 'vanished', 'seed-asset-a3', 'ZZSEEDXC', None, 'AL_ACTIVE_1'),
    ('AL_INACTIVE_2', 'appeared', 'seed-asset-i2', 'ZZSEEDXF', None, 'AL_INACTIVE_1'),
    ('AL_INACTIVE_2', 'status_flip', 'seed-asset-a2', 'ZZSEEDXE', 'ZZSEEDXB', 'AL_ACTIVE_1'),
}
EXPECTED_SNAPSHOT_SHAPE = {
    # request name -> (list, row count)
    'AL_ACTIVE_1': ('active', 3),
    'AL_INACTIVE_1': ('inactive', 1),
    'AL_ACTIVE_2': ('active', 2),
    'AL_INACTIVE_2': ('inactive', 3),
    'AL_ACTIVE_EMPTY': ('active', 0),
}


def asset_list_problems(sql: str, manifest: dict) -> list[str]:
    """The asset-list requests, their attempt rows, snapshots, raw rows and events, as the design derives them."""
    rows = seed_rows(sql, (*ASSET_LIST_MODELS, IngestRequest.TABLE_NAME, IngestRun.TABLE_NAME))
    if not rows[AssetListSnapshot.TABLE_NAME]:
        return [f'{AssetListSnapshot.TABLE_NAME}: no row in the seed']
    problems = []
    for table, model in ASSET_LIST_MODELS.items():
        declared = {column.name for column in model.columns}
        for seen in {frozenset(row) for row in rows[table]}:
            if seen != declared:
                problems.append(f'{table} INSERT: extra {sorted(seen - declared)}, missing {sorted(declared - seen)}')
        if set(manifest['digests'].get(table, {})) != declared:
            problems.append(f'{table} manifest: the digests are not the model columns')
    name_of = {str(request_id): name for name, request_id in REQUEST_IDS.items()}
    requests = {row['id']: row for row in rows[IngestRequest.TABLE_NAME] if row.get('kind') == 'ASSET_LIST'}
    lists = {name_of.get(request_id): row.get('asset_list_status') for request_id, row in requests.items()}
    wanted_lists = {name: shape[0] for name, shape in EXPECTED_SNAPSHOT_SHAPE.items()}
    if lists != wanted_lists:
        problems.append(f'ingest_request: the asset-list requests and their lists are {lists}')
    if any(row['asset_symbol'] is not None or row['asset_type'] != 'STOCK' for row in requests.values()):
        problems.append('ingest_request: an asset-list request with a symbol, or without the STOCK asset type')
    if any(
        row.get('asset_list_status') is not None for row in rows[IngestRequest.TABLE_NAME] if row['id'] not in requests
    ):
        problems.append('ingest_request: a list on a request of another kind')
    for run in rows[IngestRun.TABLE_NAME]:
        if run['request_id'] in requests:
            labels = (run['asset_symbol'], run['asset_type'], run['data_type'], run['granularity'], run['update_type'])
            if labels != (None,) * 5 or (run['served_start'], run['served_end'], run['vendor_asset_id']) != (None,) * 3:
                problems.append(f'ingest_run {run["id"]}: an asset-list attempt row with a label, range or vendor id')
    snapshots = {row['id']: row for row in rows[AssetListSnapshot.TABLE_NAME]}
    by_name = {name_of.get(row['request_id']): row for row in snapshots.values()}
    shapes = {name: (row['asset_list'], int(row['row_count'])) for name, row in by_name.items()}
    if shapes != EXPECTED_SNAPSHOT_SHAPE:
        problems.append(f'asset_list_snapshot: lists and row counts {shapes}')
    held = Counter(row['snapshot_id'] for row in rows[AssetListRow.TABLE_NAME])
    if any(held[snapshot_id] != int(row['row_count']) for snapshot_id, row in snapshots.items()):
        problems.append('asset_list_row: a snapshot holds other than row_count rows, or a row names no snapshot')
    for row in rows[AssetListRow.TABLE_NAME]:
        payload = _bytes_of(row['raw_payload']) or b''
        if row['vendor_asset_id'].encode() not in payload or row['symbol'].encode() not in payload:
            problems.append(f'asset_list_row {row["vendor_asset_id"]}: the raw payload is not the served record')
    named = {snapshot_id: name_of.get(row['request_id']) for snapshot_id, row in snapshots.items()}
    events = set()
    for event in rows[AssetListEvent.TABLE_NAME]:
        snapshot = snapshots.get(event['snapshot_id'])
        base = snapshots.get(event['previous_snapshot_id'])
        if snapshot is None or base is None:
            problems.append(f'asset_list_event {event["vendor_asset_id"]}: a snapshot id that names no snapshot')
            continue
        if (event['as_of'], event['previous_as_of'], event['asset_list']) != (
            snapshot['as_of'],
            base['as_of'],
            snapshot['asset_list'],
        ):
            problems.append(f'asset_list_event {event["vendor_asset_id"]}: instants or list differ from its snapshots')
        events.add(
            (
                named[event['snapshot_id']],
                event['kind'],
                event['vendor_asset_id'],
                event['symbol'],
                event['previous_symbol'],
                named[event['previous_snapshot_id']],
            )
        )
    if events != EXPECTED_ASSET_LIST_EVENTS:
        problems.append(
            f'asset_list_event: extra {sorted(events - EXPECTED_ASSET_LIST_EVENTS, key=str)}, '
            f'missing {sorted(EXPECTED_ASSET_LIST_EVENTS - events, key=str)}'
        )
    return problems


def test_the_head_seed_holds_the_asset_list_cast_and_exactly_the_events_the_design_derives():
    sql, manifest = _read(HEAD)
    assert asset_list_problems(sql, manifest) == []


def test_the_asset_list_check_is_red_on_the_parent_seed_and_on_a_wrong_event():
    parent_sql, parent_manifest = _read('c7e19a4d5b28')
    assert asset_list_problems(parent_sql, parent_manifest) == ['asset_list_snapshot: no row in the seed']

    sql, manifest = _read(HEAD)
    tampered = sql.replace("'status_flip'", "'appeared'", 1)
    assert any(problem.startswith('asset_list_event: extra') for problem in asset_list_problems(tampered, manifest))


# ---------------------------------------------------------------------------------------------
# The panel cast (P20s2 tj-7zjov1.196, validator tj-7zjov1.197): the five panel tables as Postgres rendered them
# ---------------------------------------------------------------------------------------------
#
# The first seed holding them was produced on the real stack at a23fdad. Read here: the columns against the models,
# the counts against panel_records.py's EXPECTED constants, every reference resolving to a seed row, the absent dataset
# absent, and every id and instant exactly what dump.NORMALISE_SQL's comments say it derives -- so a builder change to
# the derivation that the real run would contradict reds here, not only on the next re-dump.

PANEL_MODELS = {
    model.TABLE_NAME: model.__table__
    for model in (Panel, PanelSettingsVersion, PanelSourceSettingsVersion, PanelPass, PanelMemberTransition)
}
_SEED_INSTANT = datetime.fromisoformat(SEED_TIMESTAMP)


def _md5_uuid(text: str) -> str:
    return str(uuid.UUID(hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()))


def _instant(text: str | None) -> datetime | None:
    return None if text is None else datetime.fromisoformat(text)


def panel_problems(sql: str, manifest: dict) -> list[str]:
    """The panel tables of a seed against the models, the cast's counts and NORMALISE_SQL's derivations."""
    rows = seed_rows(sql, (*PANEL_MODELS, StoreDatasetEntry.TABLE_NAME, AssetListSnapshot.TABLE_NAME))
    if not rows[Panel.TABLE_NAME]:
        return [f'{Panel.TABLE_NAME}: no row in the seed']
    problems = []
    for table, model in PANEL_MODELS.items():
        declared = {column.name for column in model.columns}
        for seen in {frozenset(row) for row in rows[table]}:
            if seen != declared:
                problems.append(f'{table} INSERT: extra {sorted(seen - declared)}, missing {sorted(declared - seen)}')
        if set(manifest['digests'].get(table, {})) != declared:
            problems.append(f'{table} manifest: the digests are not the model columns')
    for table, expected in (
        (Panel.TABLE_NAME, EXPECTED_PANELS),
        (PanelSettingsVersion.TABLE_NAME, EXPECTED_SETTINGS_VERSIONS),
        (PanelSourceSettingsVersion.TABLE_NAME, EXPECTED_SOURCE_SETTINGS_VERSIONS),
        (PanelPass.TABLE_NAME, EXPECTED_PASSES),
        (PanelMemberTransition.TABLE_NAME, EXPECTED_TRANSITION_ROWS),
    ):
        if len(rows[table]) != expected or manifest['row_counts'].get(table) != expected:
            problems.append(
                f'{table}: {len(rows[table])} rows, manifest {manifest["row_counts"].get(table)}, cast {expected}'
            )

    panels = {row['id']: row for row in rows[Panel.TABLE_NAME]}
    versions = {row['id']: row for row in rows[PanelSettingsVersion.TABLE_NAME]}
    sources = {row['id']: row for row in rows[PanelSourceSettingsVersion.TABLE_NAME]}
    passes = {row['id']: row for row in rows[PanelPass.TABLE_NAME]}
    transitions = rows[PanelMemberTransition.TABLE_NAME]
    entries = {row['id'] for row in rows[StoreDatasetEntry.TABLE_NAME]}
    snapshots = {row['id'] for row in rows[AssetListSnapshot.TABLE_NAME]}

    # Synthetic names only.
    if any(not PANEL_NAME_PATTERN.fullmatch(row['name']) for row in panels.values()):
        problems.append('panel: a name outside PANEL_NAME_PATTERN')
    if any(not OWNER_PATTERN.fullmatch(row['actor']) for row in (*versions.values(), *sources.values())):
        problems.append('panel settings: an actor outside OWNER_PATTERN')
    listed = [
        s for row in versions.values() for s in (row['allow_symbols'] + row['deny_symbols']).strip('{}').split(',') if s
    ]
    if any(not SYMBOL_PATTERN.fullmatch(symbol) for symbol in [*listed, *(row['symbol'] for row in transitions)]):
        problems.append('panel: a symbol outside SYMBOL_PATTERN')

    # The ids NORMALISE_SQL derives: the panel from its name, a version from (panel, number) or (source, number).
    for panel_id, row in panels.items():
        if panel_id != _md5_uuid(f'{Panel.TABLE_NAME}|{row["name"]}'):
            problems.append('panel: the id is not the md5 of its name')
        if _instant(row['created_at']) != _SEED_INSTANT:
            problems.append('panel: created_at is not the seed timestamp')
    for table, held, key in (
        (PanelSettingsVersion.TABLE_NAME, versions, 'panel_id'),
        (PanelSourceSettingsVersion.TABLE_NAME, sources, 'source'),
    ):
        for version_id, row in held.items():
            if version_id != _md5_uuid(f'{table}|{row[key]}|{row["version_no"]}'):
                problems.append(f'{table} v{row["version_no"]}: the id is not the md5 of its unique key')
            if _instant(row['saved_at']) != _SEED_INSTANT + timedelta(hours=int(row['version_no'])):
                problems.append(f'{table} v{row["version_no"]}: saved_at is not the seed timestamp plus its number')
        if sorted(int(row['version_no']) for row in held.values()) != list(range(1, len(held) + 1)):
            problems.append(f'{table}: the version numbers are not 1..n')
    if any(row['panel_id'] not in panels for row in versions.values()):
        problems.append('panel_settings_version: a panel id that names no panel')

    # A pass: the md5 of its rank, started at the seed timestamp plus the rank in hours, finished ten minutes later.
    by_rank = {}
    for pass_id, row in passes.items():
        started = _instant(row['started_at'])
        rank = round((started - _SEED_INSTANT) / timedelta(hours=1))
        by_rank[rank] = row
        if pass_id != _md5_uuid(f'{PanelPass.TABLE_NAME}|{rank}') or started != _SEED_INSTANT + timedelta(hours=rank):
            problems.append(f'panel_pass rank {rank}: the id or the start is not derived from its rank')
        finished = _instant(row['finished_at'])
        if (finished is None) != (row['counts'] is None) or (
            finished is not None and finished != started + timedelta(minutes=10)
        ):
            problems.append(f'panel_pass rank {rank}: finished_at and counts are not set together, ten minutes in')
        if row['panel_id'] not in panels or row['settings_version_id'] not in versions:
            problems.append(f'panel_pass rank {rank}: a panel or settings version that names no row')
        if row['source_settings_version_id'] not in sources:
            problems.append(f'panel_pass rank {rank}: a source settings version that names no row')
        if row['active_snapshot_id'] not in snapshots or row['inactive_snapshot_id'] not in (*snapshots, None):
            problems.append(f'panel_pass rank {rank}: a snapshot id that names no snapshot')
    if sorted(by_rank) != list(range(1, len(passes) + 1)):
        problems.append('panel_pass: the ranks are not 1..n')
    if dict(Counter(row['kind'] for row in passes.values())) != EXPECTED_PASS_KINDS:
        problems.append('panel_pass: the counts per kind are not the cast')
    if sum(row['dry_run'] == 'true' for row in passes.values()) != EXPECTED_DRY_RUN_PASSES:
        problems.append('panel_pass: the dry-run count is not the cast')
    if sum(row['finished_at'] is None for row in passes.values()) != EXPECTED_UNFINISHED_PASSES:
        problems.append('panel_pass: the unfinished count is not the cast')
    if not any(row['inactive_snapshot_id'] is None for row in passes.values()):
        problems.append('panel_pass: no pass without an inactive snapshot')

    # A transition: the md5 of (pass, seq), recorded at its pass's start plus seq seconds; seq 1..n in every pass.
    seqs: dict[str, list[int]] = {}
    for row in transitions:
        seq = int(row['seq'])
        seqs.setdefault(row['pass_id'], []).append(seq)
        owner = passes.get(row['pass_id'])
        if owner is None or row['panel_id'] not in panels:
            problems.append(f'panel_member_transition seq {seq}: a pass or panel that names no row')
            continue
        if row['id'] != _md5_uuid(f'{PanelMemberTransition.TABLE_NAME}|{row["pass_id"]}|{seq}'):
            problems.append(f'panel_member_transition seq {seq}: the id is not the md5 of (pass, seq)')
        if _instant(row['recorded_at']) != _instant(owner['started_at']) + timedelta(seconds=seq):
            problems.append(f'panel_member_transition seq {seq}: recorded_at is not its pass start plus seq seconds')
    if any(sorted(found) != list(range(1, len(found) + 1)) for found in seqs.values()):
        problems.append('panel_member_transition: a pass whose seq is not 1..n')
    if dict(Counter(row['transition'] for row in transitions)) != EXPECTED_TRANSITIONS:
        problems.append('panel_member_transition: the counts per kind are not the cast')
    kinds: dict[tuple, set] = {}
    for row in transitions:
        kinds.setdefault((row['pass_id'], row['vendor_asset_id'], row['as_of']), set()).add(row['transition'])
    if sum({'ENTERED', 'CREATED'} <= found for found in kinds.values()) != EXPECTED_TIED_MEMBERS:
        problems.append('panel_member_transition: the same-pass ENTERED and CREATED tie is not the cast')
    # The datasets and requests a transition names: the cast's entry and requests, and the one absent dataset.
    request_ids = {str(request_id) for request_id in REQUEST_IDS.values()}
    for row in transitions:
        if row['request_id'] is not None and row['request_id'] not in request_ids:
            problems.append('panel_member_transition: a request id outside the cast')
        if row['dataset_id'] is not None and row['dataset_id'] not in entries | {str(ABSENT_DATASET_ID)}:
            problems.append('panel_member_transition: a dataset id that is neither a seed entry nor the absent one')
        if (row['transition'] == 'CREATED') != (row['request_id'] is not None):
            problems.append('panel_member_transition: a request id on other than a CREATED row, or a CREATED without')
    if str(ABSENT_DATASET_ID) in entries or str(ABSENT_DATASET_ID) not in {row['dataset_id'] for row in transitions}:
        problems.append('panel_member_transition: the absent dataset is in the seed, or no transition names it')
    return problems


def test_the_head_seed_holds_the_panel_cast_as_normalise_derives_it():
    sql, manifest = _read(HEAD)
    assert panel_problems(sql, manifest) == []


def test_the_panel_check_is_red_on_the_parent_seed_and_on_tampered_rows():
    parent_sql, parent_manifest = _read('a3f8d2c6e914')
    assert panel_problems(parent_sql, parent_manifest) == [f'{Panel.TABLE_NAME}: no row in the seed']

    sql, manifest = _read(HEAD)
    # A transition label dropped from the cast (the bead's planted mutation, here on the seed itself).
    draining = next(line for line in sql.splitlines() if "'DRAINING', 'seed-asset-" in line)
    problems = panel_problems(sql.replace(draining + '\n', ''), manifest)
    assert f'{PanelMemberTransition.TABLE_NAME}: 13 rows, manifest 14, cast 14' in problems
    assert 'panel_member_transition: the counts per kind are not the cast' in problems
    # recorded_at left as stamped (a NORMALISE_SQL that forgot it).
    stamped = re.sub(r"'2000-01-01 01:00:01\+00'", "'2026-10-30 03:12:00.123456+00'", sql, count=1)
    assert any('recorded_at is not its pass start' in problem for problem in panel_problems(stamped, manifest))
    # A pass id left random.
    first_pass = _md5_uuid(f'{PanelPass.TABLE_NAME}|1')
    assert f"'{first_pass}'" in sql
    random_id = str(uuid.UUID(int=7))
    assert any('rank 1' in problem for problem in panel_problems(sql.replace(first_pass, random_id), manifest))
    # A real panel name.
    assert 'panel: a name outside PANEL_NAME_PATTERN' in panel_problems(
        sql.replace("'zz-seed-panel'", "'momentum'"), manifest
    )
