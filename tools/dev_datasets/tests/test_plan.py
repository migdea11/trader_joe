"""plan.json against the approved set (tj-grna9p.143 r1-r13, with the schema corrections on tj-grna9p.145) (validator).

tj-grna9p.148 (5), folding in tj-grna9p.147: the healthy rows move inside the vendor's depth (r10 2021-01-04 ..
2023-01-03; r5, r7, r11 from 2020-08-03), and r14 is one DELIBERATE negative row (DIA 1week 2016-01-04 ..
2021-01-04, dev-degraded, a recorded why, no drop days). Rows may carry an optional test object.

tj-grna9p.150 (architect 12:35 UTC): the vendor's first SPY 1month bar is 2020-07-01, so r6 starts 2020-08-01, inside
the depth like every other healthy row: no healthy row of ANY resolution starts before 2020-07-27.

THE TABLE BELOW IS THE DESIGN, written out by hand from the two beads, not read from plan.json. A change to the plan
that the design did not approve turns this red. The parser's own refusals are tested on mutated copies of the plan,
because the shipped plan breaks none of them and so cannot show that they work.
"""

import copy
import json
import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from tools.dev_datasets import request as tool


pytestmark = pytest.mark.build_infra

PLAN_FILE = Path(tool.__file__).with_name('plan.json')
README = Path(tool.__file__).with_name('README.md')

FIELDS = (
    'id',
    'symbol',
    'granularity',
    'start',
    'end',
    'update_type',
    'expiry_type',
    'expiry',
    'owner',
    'degrade',
    'reads',
    'purpose',
)
# The one optional field (tj-grna9p.148 (4)), last when present, and only on the two scenario rows.
OPTIONAL_TEST_FIELD = 'test'
ROWS_WITH_TEST = {'r13': 'FAILONCE_ZZDSQQQ', 'r14': 'GAPS_ZZDSDIA'}
# The second optional field (decision tj-hhy3dk, tj-grna9p.178): the collect intent, written right after owner, and
# only on r8. The written-out key order of a row that carries it.
OPTIONAL_COLLECT_FIELD = 'collect'
ROWS_WITH_COLLECT = {'r8': False}
R14_WHY = "starts before the vendor's history: shows leading GAPS on purpose"

# (id, symbol, granularity, start, end, update_type, expiry_type, expiry, owner, reads), per tj-grna9p.143 section
# (4) and the .145 corrections: r7 and r8 end null and ROLLING, r11 BUFFER_1K; and the .148 (5) depth fixes and r14.
APPROVED = [
    ('r1', 'SPY', '1min', 'D-8', 'D-5', 'STATIC', 'BULK', 'D+2', 'dev-datasets', []),
    ('r2', 'SPY', '5min', 'D-40', 'D-10', 'STATIC', 'BULK', None, 'dev-datasets', []),
    ('r3', 'SPY', '30min', 'D-100', 'D-10', 'STATIC', 'BULK', 'D+30', 'dev-datasets', []),
    ('r4', 'SPY', '1hour', 'D-370', 'D-10', 'STATIC', 'BULK', None, 'dev-datasets', ['ui']),
    ('r5', 'SPY', '1week', '2020-08-03', 'D-1', 'STATIC', 'BULK', None, 'dev-datasets', []),
    ('r6', 'SPY', '1month', '2020-08-01', 'D-1', 'STATIC', 'BULK', 'D+365', 'dev-datasets', []),
    ('r7', 'DIA', '1day', '2020-08-03', None, 'DAILY', 'ROLLING', None, 'operator', ['ui']),
    # Decision tj-hhy3dk (tj-grna9p.178): r8 is the Stopped example, collect false and no expiry.
    ('r8', 'QQQ', '30min', 'D-30', None, 'DAILY', 'ROLLING', None, 'operator', []),
    ('r9', 'QQQ', '1hour', 'D-24', 'D-10', 'STATIC', 'BULK', 'D-1', 'dev-datasets', []),
    ('r10', 'DIA', '1day', '2021-01-04', '2023-01-03', 'STATIC', 'BULK', None, 'dev-datasets', []),
    ('r11', 'QQQ', '1week', '2020-08-03', 'D-1', 'STATIC', 'BUFFER_1K', None, 'dev-datasets', ['sdk']),
    ('r12', 'DIA', '30min', 'D-60', 'D-5', 'STATIC', 'BULK', None, 'dev-degraded', []),
    ('r13', 'QQQ', '5min', 'D-20', 'D-5', 'STATIC', 'BULK', None, 'dev-degraded', []),
    ('r14', 'DIA', '1week', '2016-01-04', '2021-01-04', 'STATIC', 'BULK', None, 'dev-degraded', []),
]


def raw_plan() -> list[dict]:
    return json.loads(PLAN_FILE.read_text(encoding='utf-8'))


def write_plan(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / 'plan.json'
    path.write_text(json.dumps(rows), encoding='utf-8')
    return path


# ---- the shipped plan -------------------------------------------------------------------------------------------


def test_the_plan_is_exactly_the_approved_fourteen_rows_in_order():
    rows = tool.load_plan()
    got = [
        (r.id, r.symbol, r.granularity, r.start, r.end, r.update_type, r.expiry_type, r.expiry, r.owner, list(r.reads))
        for r in rows
    ]
    assert got == APPROVED


def test_every_row_carries_every_field_and_no_other():
    for raw in raw_plan():
        expected = FIELDS
        if raw['id'] in ROWS_WITH_COLLECT:
            owner_at = FIELDS.index('owner') + 1
            expected = (*FIELDS[:owner_at], OPTIONAL_COLLECT_FIELD, *FIELDS[owner_at:])
        if raw['id'] in ROWS_WITH_TEST:
            expected = (*expected, OPTIONAL_TEST_FIELD)
        assert tuple(raw) == expected, raw.get('id')
    assert ROWS_WITH_COLLECT.keys().isdisjoint(ROWS_WITH_TEST)


def test_only_r13_and_r14_name_a_test_symbol_and_each_says_why():
    tests = {raw['id']: raw['test'] for raw in raw_plan() if 'test' in raw}
    assert {row_id: given['symbol'] for row_id, given in tests.items()} == ROWS_WITH_TEST
    for given in tests.values():
        assert set(given) == {'symbol', 'why'}
        assert isinstance(given['why'], str) and given['why'].strip()
    assert tests['r13']['why'].startswith('FAILONCE_')
    assert tests['r14']['why'].startswith('GAPS_')


# ---- the vendor's depth (tj-grna9p.147, folded into .148 (5)) ---------------------------------------------------

VENDOR_DAILY_HISTORY_BEGINS = '2020-07-27'


def test_the_healthy_rows_lie_inside_the_vendor_depth_as_the_design_names_them():
    raw = {row['id']: row for row in raw_plan()}
    assert (raw['r10']['start'], raw['r10']['end']) == ('2021-01-04', '2023-01-03')
    for row_id in ('r5', 'r7', 'r11'):
        assert raw[row_id]['start'] >= '2020-08-03', row_id
    # tj-grna9p.150: the first monthly bar the vendor returns is 2020-07-01; r6 starts at the first month after it.
    assert raw['r6']['start'] == '2020-08-01'


def test_no_healthy_row_of_any_resolution_starts_before_the_vendors_history():
    # A fixed ISO start before the vendor's history reads leading GAPS: allowed only on a dev-degraded row (r14).
    # tj-grna9p.150: monthly is no exception any more (r6 2019-01-01 read GAPS 377).
    fixed = [raw for raw in raw_plan() if re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw['start'])]
    assert {raw['granularity'] for raw in fixed} >= {'1day', '1week', '1month'}
    early = [
        raw['id'] for raw in fixed if raw['owner'] != 'dev-degraded' and raw['start'] < VENDOR_DAILY_HISTORY_BEGINS
    ]
    assert early == []


def test_r5s_purpose_no_longer_says_weekly_reads_gaps():
    # tj-grna9p.151 made weekly completeness read by period; the old 'documented limit' is false.
    r5 = next(raw for raw in raw_plan() if raw['id'] == 'r5')
    assert 'gaps by design' not in r5['purpose']
    assert 'documented limit' not in r5['purpose']
    assert 'COMPLETE' in r5['purpose']
    assert 'gaps by design' not in PLAN_FILE.read_text(encoding='utf-8')


def test_r14_is_the_deliberate_leading_gap_row():
    r14 = next(raw for raw in raw_plan() if raw['id'] == 'r14')
    assert r14['owner'] == 'dev-degraded'
    assert r14['degrade'] == {'drop_days': None, 'why': R14_WHY}
    assert r14['start'] < VENDOR_DAILY_HISTORY_BEGINS < r14['end']
    parsed = next(row for row in tool.load_plan() if row.id == 'r14')
    assert parsed.degrade is not None and not parsed.degrade.drops_bars
    assert (parsed.degrade.first_day, parsed.degrade.last_day, parsed.degrade.why) == (None, None, R14_WHY)


def test_every_intended_negative_row_is_dev_degraded_and_every_degrade_says_why():
    for row in tool.load_plan():
        if row.degrade is not None:
            assert row.owner == 'dev-degraded', row.id
            assert row.degrade.why.strip(), row.id
        if row.owner == 'dev-degraded':
            assert row.degrade is not None, row.id
        if row.test is not None and row.test.symbol.startswith(('EMPTY_', 'GAPS_', 'FAILONCE_')):
            assert row.owner == 'dev-degraded', f'{row.id}: a scenario row is a negative and must be dev-degraded'


def test_symbols_are_only_spy_qqq_dia():
    assert {raw['symbol'] for raw in raw_plan()} == {'SPY', 'QQQ', 'DIA'}


def test_owners_are_only_the_generators_three_and_never_claude_seed():
    assert {raw['owner'] for raw in raw_plan()} == {'dev-datasets', 'operator', 'dev-degraded'}
    assert 'claude-seed' not in PLAN_FILE.read_text(encoding='utf-8')


def test_every_value_is_from_the_store_vocabulary():
    for raw in raw_plan():
        assert raw['granularity'] in {'1min', '5min', '30min', '1hour', '1day', '1week', '1month'}
        assert raw['update_type'] in {'STATIC', 'DAILY'}
        assert raw['expiry_type'] in {'BULK', 'BUFFER_1K', 'BUFFER_10K', 'BUFFER_100K', 'ROLLING'}
        assert set(raw['reads']) <= {'ui', 'sdk'}
        assert isinstance(raw['purpose'], str) and raw['purpose']


def test_daily_rows_are_open_ended_and_never_bulk():
    daily = [raw for raw in raw_plan() if raw['update_type'] == 'DAILY']
    assert [raw['id'] for raw in daily] == ['r7', 'r8']
    for raw in daily:
        assert raw['end'] is None
        assert raw['expiry_type'] == 'ROLLING'


def test_r11_shows_the_buffer_1k_label():
    assert next(raw for raw in raw_plan() if raw['id'] == 'r11')['expiry_type'] == 'BUFFER_1K'


def test_only_r12_r13_and_r14_are_degraded_and_each_says_why():
    degraded = [raw for raw in raw_plan() if raw['degrade'] is not None]
    assert [raw['id'] for raw in degraded] == ['r12', 'r13', 'r14']
    for raw in degraded:
        assert raw['owner'] == 'dev-degraded'
        assert set(raw['degrade']) == {'drop_days', 'why'}
        assert raw['degrade']['why'].strip()
    for raw in degraded[:2]:
        assert len(raw['degrade']['drop_days']) == 2
        assert 'INTENTIONAL' in raw['degrade']['why'] and '--repair' in raw['degrade']['why']
    assert degraded[2]['degrade']['drop_days'] is None


def test_each_degraded_row_is_the_only_dev_degraded_entry_of_its_symbol_and_granularity():
    # The degrade lookup keys on (owner, symbol, granularity) and refuses anything but one match.
    keys = [(r.owner, r.symbol, r.granularity) for r in tool.load_plan() if r.owner == 'dev-degraded']
    assert len(keys) == len(set(keys))


def test_no_row_reuses_a_reference_rows_symbol_and_granularity():
    # The nine claude-seed rows (tj-grna9p.143 (1)); the set reuses none of their (symbol, resolution) pairs.
    reference = {
        ('SPY', '1day'),
        ('QQQ', '1day'),
        ('NVDA', '1day'),
        ('VTI', '1week'),
        ('TLT', '1month'),
        ('AAPL', '1hour'),
        ('MSFT', '30min'),
        ('IWM', '5min'),
        ('GLD', '1min'),
    }
    assert not {(r.symbol, r.granularity) for r in tool.load_plan()} & reference


def _readme_set_table() -> list[list[str]]:
    """The README's 'The set' table: its rows have ten columns (the narrower tables also start '| r')."""
    text = README.read_text(encoding='utf-8')
    cells = [[cell.strip() for cell in line.split('|')[1:-1]] for line in text.splitlines() if line.startswith('| r')]
    return [row for row in cells if len(row) == 10]


def _readme_value(value: str | None) -> str:
    return '-' if value is None else value


def test_the_readme_table_names_the_same_rows_and_symbols():
    text = README.read_text(encoding='utf-8')
    table = _readme_set_table()
    assert [line[0] for line in table] == [row[0] for row in APPROVED]
    assert [line[1] for line in table] == [row[1] for row in APPROVED]
    assert 'these rows are intentionally damaged' in text.lower()
    assert 'claude-seed' not in [line[7] for line in table]


def test_the_readme_table_matches_the_plan_column_by_column():
    by_id = {row[0]: row for row in APPROVED}
    for line in _readme_set_table():
        row_id, symbol, granularity, span, update, expiry_type, expiry, owner = line[:8]
        _, p_symbol, p_gran, p_start, p_end, p_update, p_expiry_type, p_expiry, p_owner, _ = by_id[row_id]
        assert (symbol, granularity, update, expiry_type) == (p_symbol, p_gran, p_update, p_expiry_type), row_id
        assert span == f'{p_start} .. {_readme_value(p_end)}', row_id
        assert expiry == _readme_value(p_expiry), row_id
        assert owner.strip('*') == p_owner, row_id


def test_the_readme_names_the_negative_rows_and_says_the_audit_tables_are_append_only_by_convention_only():
    lines = README.read_text(encoding='utf-8').splitlines()
    text = ' '.join(' '.join(lines).split())
    damaged = [line.split('|')[1].strip() for line in lines if line.startswith('| `r')]
    assert damaged[:3] == ['`r12`', '`r13`', '`r14`']
    assert 'The two audit tables are append-only by convention only.' in text
    assert 'not recorded' in text


def _readme_text() -> str:
    return ' '.join(README.read_text(encoding='utf-8').split())


@pytest.mark.parametrize(
    'sentence',
    [
        # tj-grna9p.150 (2) and the 12:35 UTC plan fix: the vendor depth, and r6 inside it.
        'monthly bars begin 2020-07 and daily bars 2020-07-27',
        'Every healthy row therefore starts after 2020-07-27 (r6 on 2020-08-01).',
        # (5): the quiet-stack guarantee.
        '**Reset a quiet stack** (no Viewer open, no request running).',
        'while any `ingest_run` row is `RUNNING` (it counts them in the same transaction, before the TRUNCATE, and '
        'names the count)',
        'After the commit it waits two usage flush intervals (10 s; the store flushes every 5 s) and recounts '
        '`ingest_run` and `dataset_read`; a non-zero count prints `NOT CLEAN` with the counts and exits 1.',
        'The guarantee is exactly that: clean at commit, verified after one flush interval.',
        # (4) and (3): r13's fail-once memory, and which 503 is the designed one.
        'That memory lives in the `data_ingest` process',
        'a second `seed` on the same test stack with the same date gets **no** failed run for r13',
        'restart `data_ingest` or use another `--date` to see it again.',
        'Only a 503 with reason `VENDOR_UNAVAILABLE` counts as the designed failure; any other 503 stops the seed.',
        # (1): every dry run skips the credential check.
        'every mode but `check` stops, except that every `--dry-run` skips the credential check (it sends no secret '
        'and opens no database with a placeholder).',
    ],
)
def test_the_readme_states_the_tj_grna9p_150_guarantees(sentence):
    assert sentence in _readme_text()


def test_the_readme_no_longer_claims_the_old_depth_or_the_narrow_dry_run_exception():
    text = _readme_text()
    assert (
        "The vendor's daily history on the dev plan begins 2020-07-27, so every healthy row starts inside it."
        not in text
    )
    assert '`reads/degrade/repair --dry-run`' not in text
    assert '2019-01-01' not in text


def test_the_tool_docstring_says_every_dry_run_skips_the_credential_check():
    doc = ' '.join((tool.__doc__ or '').split())
    assert 'stops every mode except check and every dry run' in doc


# ---- the reaper's row and the negative rows are apart (tj-grna9p.165) -------------------------------------------
#
# The reaper's pending set is expiry <= now (server/data/store/app/reaper.py). A row past expiry is the reaper's
# demonstration and belongs to a healthy owner; a dev-degraded row is a negative test and must never be one the
# reaper takes. Past is read with the plan's own resolver on the seed date D and again at D+10, when r1 (D+2) has
# passed too. r8 has no expiry since decision tj-hhy3dk (tj-grna9p.178): stopping is collect, not an expiry, so it
# is never past-expiry and never reaped.

RUN_DATES = (date(2026, 10, 21), date(2024, 2, 28), date(2023, 12, 31), date(2025, 6, 30))
HEALTHY_OWNERS = {'dev-datasets', 'operator'}
# (days after D, the rows whose expiry is at or before that day), from the APPROVED table above.
PAST_EXPIRY = ((0, {'r9'}), (10, {'r1', 'r9'}))


def _past_expiry(rows, seeded: date, on: date) -> dict[str, str]:
    return {r.id: r.owner for r in rows if r.expiry is not None and tool.resolve_day(r.expiry, seeded) <= on}


def test_no_dev_degraded_row_has_an_expiry():
    degraded = [row for row in tool.load_plan() if row.owner == 'dev-degraded']
    assert [row.id for row in degraded] == ['r12', 'r13', 'r14']
    assert {row.id: row.expiry for row in degraded} == {'r12': None, 'r13': None, 'r14': None}


@pytest.mark.parametrize('seeded', RUN_DATES)
@pytest.mark.parametrize(('days_after', 'expected'), PAST_EXPIRY)
def test_every_past_expiry_row_is_healthy_never_dev_degraded(seeded, days_after, expected):
    past = _past_expiry(tool.load_plan(), seeded, seeded + timedelta(days=days_after))
    assert set(past) == expected
    assert set(past.values()) <= HEALTHY_OWNERS, past


# ---- the collect intent (decision tj-hhy3dk, tj-grna9p.178) -----------------------------------------------------
#
# collect is a subscription's intent: optional, boolean only, never on a STATIC row (the store refuses it there with
# a 422). The plan has no STREAM update type (the parser refuses STREAM outright, above), so DAILY is the only row
# type that may carry it. r8 is the Stopped example: collect false and no expiry. No other row sets it.


def test_only_r8_sets_collect_and_it_is_false_with_no_expiry():
    given = {raw['id']: raw['collect'] for raw in raw_plan() if 'collect' in raw}
    assert given == ROWS_WITH_COLLECT
    assert given['r8'] is False
    parsed = {row.id: row for row in tool.load_plan()}
    assert {row_id: row.collect for row_id, row in parsed.items() if row.collect is not None} == ROWS_WITH_COLLECT
    assert parsed['r8'].collect is False
    assert parsed['r8'].expiry is None
    assert parsed['r8'].update_type == 'DAILY'


def test_no_static_row_carries_collect_in_the_plan_or_parsed():
    for raw in raw_plan():
        if raw['update_type'] == 'STATIC':
            assert 'collect' not in raw, raw['id']
    for row in tool.load_plan():
        if row.update_type == 'STATIC':
            assert row.collect is None, row.id


def test_a_row_without_collect_parses_to_none_not_true():
    # None means 'not sent': the server default applies. A parser defaulting to True would send it on every row.
    for row in tool.load_plan():
        if row.id not in ROWS_WITH_COLLECT:
            assert row.collect is None, row.id


@pytest.mark.parametrize('value', [True, False])
def test_the_parser_accepts_collect_as_a_boolean_on_a_daily_row(tmp_path, value):
    for row_id in ('r7', 'r8'):
        rows = tool.load_plan(write_plan(tmp_path, mutated(row_id, collect=value)))
        assert next(row for row in rows if row.id == row_id).collect is value


@pytest.mark.parametrize('row_id', ['r1', 'r9', 'r12', 'r14'])
@pytest.mark.parametrize('value', [True, False])
def test_the_parser_refuses_collect_on_a_static_row(tmp_path, row_id, value):
    with pytest.raises(tool.DevDatasetsError, match=re.escape('collect is for a subscription')):
        tool.load_plan(write_plan(tmp_path, mutated(row_id, collect=value)))


@pytest.mark.parametrize('value', [None, 0, 1, 'false', 'true', [], {}])
def test_the_parser_refuses_a_collect_that_is_not_a_boolean(tmp_path, value):
    with pytest.raises(tool.DevDatasetsError, match=re.escape('collect is a boolean when present')):
        tool.load_plan(write_plan(tmp_path, mutated('r8', collect=value)))


def test_the_test_mapping_of_r8_carries_collect_false():
    r8 = next(row for row in tool.load_plan() if row.id == 'r8')
    mapped = r8.on_backend('test')
    assert mapped.symbol == 'ZZDSQQQ'
    assert mapped.collect is False
    assert mapped.expiry is None


@pytest.mark.parametrize(
    'sentence',
    [
        '**Expiry is the deletion date only.** It never stops a subscription;',
        'the plan refuses it on a STATIC row, as the store does.',
        '**r8 is the Stopped example**: `collect` false and no expiry, so the refresh skips it and nothing ever '
        'deletes it.',
        "On the test backend r8's `ZZDS` mapping carries the same `collect` false.",
        'No `dev-degraded` row has an expiry, and every past-expiry row belongs to `dev-datasets` or `operator`.',
    ],
)
def test_the_readme_states_the_collect_and_deletion_date_rules(sentence):
    assert sentence in _readme_text()


def test_the_readme_and_plan_no_longer_say_an_expiry_retires_a_subscription():
    text = _readme_text()
    assert 'retires it at once' not in text
    assert 'RETIRED state' not in text
    assert 'RETIRED' not in PLAN_FILE.read_text(encoding='utf-8')


# ---- the parser's refusals, on mutated copies -------------------------------------------------------------------


def mutated(row_id: str, **changes) -> list[dict]:
    rows = copy.deepcopy(raw_plan())
    for raw in rows:
        if raw['id'] == row_id:
            raw.update(changes)
    return rows


@pytest.mark.parametrize(
    ('row_id', 'changes', 'message'),
    [
        ('r1', {'owner': 'claude-seed'}, 'owner is not allowed'),
        ('r12', {'owner': 'claude-seed'}, 'owner is not allowed'),
        ('r1', {'owner': 'someone-else'}, 'owner is not allowed'),
        ('r1', {'symbol': 'AAPL'}, 'symbol outside'),
        ('r1', {'symbol': 'XIC.TO'}, 'symbol outside'),
        ('r1', {'symbol': 'spy'}, 'symbol outside'),
        ('r1', {'granularity': '2min'}, 'unknown granularity'),
        ('r1', {'update_type': 'STREAM'}, 'STATIC or DAILY'),
        ('r1', {'expiry_type': 'FOREVER'}, 'unknown expiry_type'),
        ('r7', {'end': 'D-1'}, 'open-ended'),
        ('r7', {'expiry_type': 'BULK'}, 'may not use BULK'),
        ('r1', {'end': None}, 'needs an end'),
        ('r1', {'reads': ['ui', 'tv']}, 'reads must be'),
        ('r1', {'degrade': {'drop_days': ['D-7', 'D-6'], 'why': 'x'}}, 'only dev-degraded rows'),
        ('r12', {'degrade': None}, 'must name its degrade'),
        ('r12', {'degrade': {'drop_days': ['D-7'], 'why': 'x'}}, 'drop_days is [from, to]'),
        ('r12', {'degrade': {'drop_days': ['D-33', 'D-27'], 'why': '  '}}, 'a degrade must say why'),
        ('r14', {'degrade': {'drop_days': None}}, 'a degrade must say why'),
        # The optional test object (tj-grna9p.148 (4)): an optional scenario prefix, ZZDS, then the row's dev symbol.
        ('r13', {'test': {'symbol': 'QQQ', 'why': 'x'}}, 'test.symbol must be'),
        ('r13', {'test': {'symbol': 'FAILONCE_QQQ', 'why': 'x'}}, 'test.symbol must be'),
        ('r13', {'test': {'symbol': 'ZZDSSPY', 'why': 'x'}}, 'test.symbol must be'),
        ('r13', {'test': {'symbol': 'FAIL_ZZDSQQQ', 'why': 'x'}}, 'test.symbol must be'),
        ('r13', {'test': {'symbol': 'RATELIMIT_ZZDSQQQ', 'why': 'x'}}, 'test.symbol must be'),
        ('r13', {'test': {'symbol': 'zzdsqqq', 'why': 'x'}}, 'test.symbol must be'),
        ('r13', {'test': {'symbol': 'ZZDSQQQX', 'why': 'x'}}, 'test.symbol must be'),
        ('r13', {'test': {'symbol': 'XZZDSQQQ', 'why': 'x'}}, 'test.symbol must be'),
        ('r13', {'test': {'symbol': 'FAILONCE_EMPTY_ZZDSQQQ', 'why': 'x'}}, 'test.symbol must be'),
        ('r13', {'test': {'symbol': 7, 'why': 'x'}}, 'test.symbol must be'),
        ('r13', {'test': {'symbol': 'ZZDSQQQ', 'why': ' '}}, 'test.why must say why'),
        ('r13', {'test': {'symbol': 'ZZDSQQQ', 'why': None}}, 'test.why must say why'),
        ('r13', {'test': {'symbol': 'ZZDSQQQ'}}, 'test is {symbol, why}'),
        ('r13', {'test': {'symbol': 'ZZDSQQQ', 'why': 'x', 'extra': 1}}, 'test is {symbol, why}'),
        ('r13', {'test': 'ZZDSQQQ'}, 'test is {symbol, why}'),
    ],
)
def test_the_parser_refuses_a_row_that_breaks_the_schema(tmp_path, row_id, changes, message):
    with pytest.raises(tool.DevDatasetsError, match=re.escape(message)):
        tool.load_plan(write_plan(tmp_path, mutated(row_id, **changes)))


@pytest.mark.parametrize('symbol', ['ZZDSQQQ', 'EMPTY_ZZDSQQQ', 'GAPS_ZZDSQQQ', 'FAILONCE_ZZDSQQQ'])
def test_the_parser_accepts_each_scenario_prefix_or_none(tmp_path, symbol):
    rows = tool.load_plan(write_plan(tmp_path, mutated('r13', test={'symbol': symbol, 'why': 'x'})))
    assert next(row for row in rows if row.id == 'r13').test == tool.TestMapping(symbol, 'x')


def test_the_parser_refuses_a_missing_field(tmp_path):
    rows = raw_plan()
    del rows[0]['purpose']
    with pytest.raises(tool.DevDatasetsError, match='missing'):
        tool.load_plan(write_plan(tmp_path, rows))


def test_the_parser_refuses_an_unknown_field(tmp_path):
    rows = raw_plan()
    rows[0]['feed'] = 'iex'
    with pytest.raises(tool.DevDatasetsError, match='unknown fields'):
        tool.load_plan(write_plan(tmp_path, rows))


def test_the_unknown_fields_message_names_only_the_unknown_fields(tmp_path):
    rows = raw_plan()
    rows[0]['feed'] = 'iex'
    rows[0]['zeta'] = 1
    with pytest.raises(tool.DevDatasetsError) as raised:
        tool.load_plan(write_plan(tmp_path, rows))
    assert str(raised.value) == "plan row r1: unknown fields ['feed', 'zeta']"


@pytest.mark.parametrize('reads', [None, 'ui', 7, {'ui': 1}])
def test_reads_that_are_not_a_list_are_a_plan_error_not_a_crash(tmp_path, reads):
    path = write_plan(tmp_path, mutated('r4', reads=reads))
    with pytest.raises(tool.DevDatasetsError, match='reads must be ui and sdk'):
        tool.load_plan(path)


def test_reads_null_stops_the_run_with_the_plan_error(tmp_path):
    from tools.dev_datasets.tests.doubles import ExplodingHttp, run_tool

    outcome = run_tool(
        ['seed', '--dry-run'], http=ExplodingHttp(), plan_path=write_plan(tmp_path, mutated('r4', reads=None))
    )
    assert outcome.status == 1
    assert outcome.lines == ['plan row r4: reads must be ui and sdk']


def test_the_readme_says_to_pass_the_same_date_to_seed_degrade_and_repair():
    text = ' '.join(README.read_text(encoding='utf-8').split())
    assert 'Pass the **same `--date`** to `seed`, `degrade` and `repair`' in text


def test_the_parser_refuses_duplicate_ids(tmp_path):
    rows = raw_plan()
    rows[1]['id'] = rows[0]['id']
    with pytest.raises(tool.DevDatasetsError, match='duplicate'):
        tool.load_plan(write_plan(tmp_path, rows))


def test_a_refused_plan_stops_the_run_before_anything_is_sent(tmp_path):
    from tools.dev_datasets.tests.doubles import ExplodingHttp, exploding_connect, run_tool

    http = ExplodingHttp()
    outcome = run_tool(
        ['seed'],
        http=http,
        connect=exploding_connect,
        plan_path=write_plan(tmp_path, mutated('r3', owner='claude-seed')),
    )
    assert outcome.status == 1
    assert http.calls == []
    assert 'owner is not allowed' in outcome.text
