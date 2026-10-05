"""The filtering bar read: GET /internal/asset-data and read_market_activity_data (tj-vhboky.23, F3).

tj-vhboky.11 items 21-23, the acceptance tests of tj-6z03hd and tj-p78ng6, and the coverage the F2
gate (tj-vhboky.22, fe0d683) handed over. The design is the USER RULINGS on tj-vhboky.20 and decision
tj-vhboky.25 with its 21:41 addendum.

TWO TIERS, AND WHAT NEITHER PROVES.

1. THE STATEMENT. read_market_activity_data is driven against a recording fake session and the
   statement it executes is taken apart: its WHERE clause into (column, operator, value) triples,
   its ORDER BY compiled for the Postgres dialect. Every predicate is a plain column-op-literal
   comparison -- _predicates() refuses anything else -- so a row can be tested against the
   conjunction in Python with the comparison's own operator (_satisfies). That is how "a bar at
   exactly start is included" and "no SIP row satisfies feed=IEX" are stated here.
2. THE ROUTE. The crud function is replaced on its module (the route reaches it as
   crud_stock_market_activity.read_market_activity_data), async_db is overridden, and requests go
   through a TestClient that is never entered as a context manager, so no lifespan, Postgres or
   Kafka is needed (the same seam test_http_smoke.py documents). What the crud RECEIVED is asserted.

NO DATABASE RUNS HERE (the agent container has no Docker). What Postgres returns for these
statements -- the row counts, the inclusive edges on a timestamptz column, one tape's rows and not
the other's -- is proved on real rows at tj-vhboky.14 stage 2 item 9 (ADR tj-fdb9gz: a test that
concedes what it cannot prove names where it is proved).

THE MODEL-LEVEL HALF of #23 is NOT here: schemas/tests/test_schemas_smoke_data_store.py
(test_an_empty_bar_query_is_refused and its two neighbours, b87db45). This file is the HTTP half.
"""

import operator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Column
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.elements import BinaryExpression, BindParameter, BooleanClauseList

from common.enums.data_stock import DataSource, Feed, Granularity
from data.store.app.database.crud.stock import asset_market_activity as crud_module
from data.store.app.database.database import async_db
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.main import app
from data.store.tests.problem_body import validation_errors
from schemas.data_store.stock.market_activity_data import StockDataMarketActivityQuery


pytestmark = pytest.mark.data_store

DATASET_ID = UUID('8f41c2d7-a3b9-4d1e-9c2f-0a1b2c3d4e5f')
START = datetime(2026, 1, 2, tzinfo=UTC)
END = datetime(2026, 1, 9, tzinfo=UTC)

# One value per filterable field, as the query model holds it after validation (asset_symbol is
# upper-cased by the model, so it is given upper-case here).
FIELD_VALUES: dict[str, Any] = {
    'dataset_id': DATASET_ID,
    'asset_symbol': 'AAPL',
    'source': DataSource.ALPACA_API,
    'feed': Feed.IEX,
    'granularity': Granularity.ONE_DAY,
    'start': START,
    'end': END,
}

# The ONE predicate each field must add: (column, comparison, value). Bounds are inclusive.
EXPECTED_PREDICATE: dict[str, tuple[str, Any, Any]] = {
    'dataset_id': ('dataset_id', operator.eq, DATASET_ID),
    'asset_symbol': ('asset_symbol', operator.eq, 'AAPL'),
    'source': ('source', operator.eq, DataSource.ALPACA_API),
    'feed': ('feed', operator.eq, Feed.IEX),
    'granularity': ('granularity', operator.eq, Granularity.ONE_DAY),
    'start': ('timestamp', operator.ge, START),
    'end': ('timestamp', operator.le, END),
}

ORDER_BY_EXACTLY = f'{StockMarketActivity.__tablename__}.timestamp, {StockMarketActivity.__tablename__}.dataset_id'

BARS_URL = '/internal/asset-data/stock/market-activity'


# ------------------------------------------------------------------------------------------------
# The statement tier
# ------------------------------------------------------------------------------------------------


class _NoRows:
    def scalars(self) -> '_NoRows':
        return self

    def all(self) -> list:
        return []


class _RecordingSession:
    """Records what read_market_activity_data executes; returns no rows."""

    def __init__(self):
        self.statements: list = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _NoRows()


async def _executed_statement(query: StockDataMarketActivityQuery):
    db = _RecordingSession()
    await crud_module.read_market_activity_data(db, query)
    assert len(db.statements) == 1, f'the read executed {len(db.statements)} statements, not one'
    return db.statements[0]


def _predicates(statement) -> list[tuple[str, Any, Any]]:
    """The WHERE clause as (column, comparison, value) triples, in no particular order.

    Refuses anything that is not a conjunction of column-op-literal comparisons, so _satisfies
    below can never be handed a shape it would evaluate wrongly (an OR, a function call, an IN).
    """
    where = statement.whereclause
    if where is None:
        return []
    clauses = list(where.clauses) if isinstance(where, BooleanClauseList) else [where]
    if isinstance(where, BooleanClauseList):
        assert where.operator is operator.and_, f'the WHERE clause is not a plain conjunction: {where}'
    triples = []
    for clause in clauses:
        assert isinstance(clause, BinaryExpression), f'not a simple comparison: {clause!r}'
        assert isinstance(clause.left, Column), f'left side is not a column: {clause.left!r}'
        assert isinstance(clause.right, BindParameter), f'right side is not a bound literal: {clause.right!r}'
        assert clause.left.table is StockMarketActivity.__table__, f'{clause.left} is not a bar column'
        triples.append((clause.left.key, clause.operator, clause.right.value))
    return triples


def _satisfies(row: dict[str, Any], predicates: list[tuple[str, Any, Any]]) -> bool:
    """Whether a row passes every predicate, each evaluated with its own comparison operator."""
    return all(compare(row[column], value) for column, compare, value in predicates)


def _order_by(statement) -> str:
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert compiled.count('ORDER BY') == 1, f'expected one ORDER BY clause in:\n{compiled}'
    return compiled.split('ORDER BY', 1)[1].strip()


def _query(**fields: Any) -> StockDataMarketActivityQuery:
    return StockDataMarketActivityQuery(**fields)


def _bar_row(**overrides: Any) -> dict[str, Any]:
    """A bar that satisfies every FIELD_VALUES predicate, as the columns the WHERE clause reads."""
    row = {
        'dataset_id': DATASET_ID,
        'asset_symbol': 'AAPL',
        'source': DataSource.ALPACA_API,
        'feed': Feed.IEX,
        'granularity': Granularity.ONE_DAY,
        'timestamp': START + timedelta(days=1),
    }
    row.update(overrides)
    return row


def test_the_read_everything_function_is_gone():
    """tj-vhboky.11 item 21. Ruling 2 of the epic: there is ONE read path.

    An attribute check on the crud module, not a grep: a re-export or an alias under the old name
    is caught too.
    """
    assert not hasattr(crud_module, 'read_all_asset_market_activity_data'), (
        'read_all_asset_market_activity_data is back on the crud module; the filtering read is the only read path'
    )


def _selector_for(field: str) -> str:
    """A query must name dataset_id or asset_symbol (tj-vhboky.26); pick one that is not `field`."""
    return 'asset_symbol' if field == 'dataset_id' else 'dataset_id'


@pytest.mark.asyncio
@pytest.mark.parametrize('field', list(FIELD_VALUES))
async def test_each_field_adds_exactly_its_own_predicate(field: str):
    """tj-vhboky.11 item 22: supplying a field adds its predicate, and omitting the others adds none.

    Each field is driven with the one selector the model requires, so the expected WHERE clause
    is exactly two predicates. Set equality covers both halves: a missing predicate (the field is
    silently unfiltered -- source was, before fe0d683) and an extra one (an omitted field filtering
    anyway) are both red. Bounds are asserted as >= and <=, the inclusive edges.
    """
    selector = _selector_for(field)
    statement = await _executed_statement(_query(**{selector: FIELD_VALUES[selector], field: FIELD_VALUES[field]}))

    assert sorted(_predicates(statement), key=repr) == sorted(
        [EXPECTED_PREDICATE[selector], EXPECTED_PREDICATE[field]], key=repr
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('selector', ['dataset_id', 'asset_symbol'])
async def test_a_selector_alone_is_the_only_predicate(selector: str):
    statement = await _executed_statement(_query(**{selector: FIELD_VALUES[selector]}))

    assert _predicates(statement) == [EXPECTED_PREDICATE[selector]]


@pytest.mark.asyncio
async def test_every_field_together_gives_all_seven_predicates():
    statement = await _executed_statement(_query(**FIELD_VALUES))

    assert sorted(_predicates(statement), key=repr) == sorted(EXPECTED_PREDICATE.values(), key=repr)


BLANK_SYMBOLS = {'empty': '', 'spaces': '  ', 'tab': '\t'}


@pytest.mark.parametrize('with_dataset', [True, False], ids=['with-dataset_id', 'symbol-only'])
@pytest.mark.parametrize('blank', BLANK_SYMBOLS.values(), ids=BLANK_SYMBOLS.keys())
def test_a_blank_symbol_cannot_be_built_into_a_read_query(blank: str, with_dataset: bool):
    """A blank asset_symbol cannot be built into a query, so no read ever filters on one (F1c).

    RENAMED FROM test_a_falsy_but_set_symbol_still_filters, and INVERTED (tj-8fxxfb iii).

    SUPERSEDED DESIGN, kept so the file carries the history. That test was F2 coverage item 6: each
    predicate tests `is not None`, not truthiness, and asset_symbol was the ONLY field where the two
    differed, because '' was a valid, falsy str. It built dataset_id plus asset_symbol='' and asserted
    the read filtered on ('asset_symbol', ==, ''): a filter that matches no rows, so the caller got a
    silent empty 200 for a malformed request.

    THE DESIGN NOW: user ruling of 2026-09-28 00:23 UTC on tj-vhboky.28, confirmed by the architect at
    00:24 UTC -- a blank symbol gets "the same error as the empty search": a MODEL-LEVEL
    ValidationError (loc (), value_error), even when dataset_id is given. 1331ef5 put it in
    AssetDataQuery.require_dataset_or_symbol. So '' can no longer be falsy-but-set on a built query,
    and the truthiness distinction item 6 guarded is unobservable for every field
    (test_no_other_filter_value_is_falsy).

    STRENGTHENED, not merely flipped. The old test had one blank ('') and one scope; this one has
    three blanks (whitespace too, which the 'is not None' rule also let through) in both scopes, and
    asserts the refusal exactly, at the model, never at ('asset_symbol',). What this tier can prove
    is that the query the read takes cannot be BUILT, so no caller can hand the read a blank symbol;
    it does not claim the read itself rejects one (read_market_activity_data does no validation of
    its own). The padded symbol is the next test; the HTTP half is test_a_blank_symbol_is_refused_with_422.
    """
    scope = {'dataset_id': DATASET_ID} if with_dataset else {}

    with pytest.raises(ValidationError) as excinfo:
        _query(**scope, asset_symbol=blank)

    assert [(error['loc'], error['type']) for error in excinfo.value.errors()] == [((), 'value_error')]
    assert 'blank' in excinfo.value.errors()[0]['msg']


@pytest.mark.asyncio
async def test_a_padded_symbol_filters_as_sent():
    """The surviving edge of the old falsy-symbol test: a padded, non-blank symbol still filters.

    The blank rule tests strip() but must not trim; the read filters on the upper-cased value with
    its padding intact, exactly as before 1331ef5. Model-level twin:
    schemas/tests/test_schemas_smoke_data_store.py::test_a_padded_symbol_is_upper_cased_and_not_trimmed.
    """
    statement = await _executed_statement(_query(dataset_id=DATASET_ID, asset_symbol=' aapl '))

    assert ('asset_symbol', operator.eq, ' AAPL ') in _predicates(statement)


def test_no_other_filter_value_is_falsy():
    """The premise F2 coverage item 6 rests on: no filter value can be falsy, so truthiness is moot.

    Before 1331ef5 asset_symbol='' was the one falsy value and test_a_falsy_but_set_symbol_still_filters
    pinned it. A blank symbol is now refused at the model (test_a_blank_symbol_cannot_be_built_into_a_read_query),
    so this covers the remaining six. If it goes red, add a statement-tier case for the new falsy
    value: `if request.<field>:` would then silently stop filtering on it.
    """
    falsy = [member for enum in (DataSource, Feed, Granularity) for member in enum if not member]
    assert falsy == [], f'falsy enum members now exist; a truthiness check would drop them: {falsy}'


@pytest.mark.asyncio
async def test_the_read_is_ordered_by_timestamp_then_dataset_id_and_nothing_else():
    """User ruling (4), tj-vhboky.20 21:41: ORDER BY timestamp, then dataset_id -- nothing else.

    The whole clause is compared, so adding id or feed, dropping dataset_id, reordering, or adding
    DESC are all red.
    """
    statement = await _executed_statement(_query(dataset_id=DATASET_ID))

    assert _order_by(statement) == ORDER_BY_EXACTLY


@pytest.mark.asyncio
async def test_the_bounds_are_inclusive_at_both_edges():
    """F2 coverage item 5: a bar at exactly start and a bar at exactly end are both included.

    Evaluated against the statement's own predicates. On a real timestamptz column this is
    tj-vhboky.14 stage 2 item 9.
    """
    predicates = _predicates(await _executed_statement(_query(dataset_id=DATASET_ID, start=START, end=END)))

    assert _satisfies(_bar_row(timestamp=START), predicates), 'a bar at exactly start was excluded'
    assert _satisfies(_bar_row(timestamp=END), predicates), 'a bar at exactly end was excluded'
    tick = timedelta(microseconds=1)
    assert not _satisfies(_bar_row(timestamp=START - tick), predicates), 'a bar before start was included'
    assert not _satisfies(_bar_row(timestamp=END + tick), predicates), 'a bar after end was included'


@pytest.mark.asyncio
@pytest.mark.parametrize(('asked', 'other'), [(Feed.IEX, Feed.SIP), (Feed.SIP, Feed.IEX)])
async def test_a_feed_query_selects_one_tape(asked: Feed, other: Feed):
    """tj-p78ng6 acceptance, tape separability: a feed query admits one tape's bars only.

    A feed=IEX query compiles a predicate no SIP row can satisfy, and the reverse.

    tj-6z03hd acceptance ("a filtered request returns fewer rows than an unfiltered one"): under
    user ruling (5) there is no unfiltered request any more, so the comparison is a symbol-only
    query against the same symbol narrowed by feed. The symbol-only query admits both tapes' bars;
    the narrowed one admits one. The row COUNT against Postgres is tj-vhboky.14 stage 2 item 9.
    """
    symbol_only = _predicates(await _executed_statement(_query(asset_symbol='AAPL')))
    narrowed = _predicates(await _executed_statement(_query(asset_symbol='AAPL', feed=asked)))
    asked_bar, other_bar = _bar_row(feed=asked), _bar_row(feed=other)

    assert _satisfies(asked_bar, symbol_only) and _satisfies(other_bar, symbol_only)
    assert _satisfies(asked_bar, narrowed), f'a {asked} bar does not satisfy a feed={asked} query'
    assert not _satisfies(other_bar, narrowed), f'a {other} bar satisfies a feed={asked} query'


@pytest.mark.asyncio
async def test_a_dataset_id_narrows_a_symbol_query():
    """The other half of the tj-6z03hd real-row property: symbol-only versus symbol and dataset_id.

    Count against Postgres: tj-vhboky.14 stage 2 item 9.
    """
    other_dataset = UUID('00000000-0000-0000-0000-0000000000ff')
    narrowed = _predicates(await _executed_statement(_query(asset_symbol='AAPL', dataset_id=DATASET_ID)))

    assert _satisfies(_bar_row(), narrowed)
    assert not _satisfies(_bar_row(dataset_id=other_dataset), narrowed)


# ------------------------------------------------------------------------------------------------
# The route tier
# ------------------------------------------------------------------------------------------------


class _SessionSentinel:
    """Stands in for the AsyncSession async_db would yield. Nothing may call it: the crud is replaced."""


@pytest.fixture
def received(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, StockDataMarketActivityQuery]]:
    """Replace the crud read and record every (db, query) it is called with."""
    calls: list[tuple[Any, StockDataMarketActivityQuery]] = []

    async def recording_read(db, request):
        calls.append((db, request))
        return []

    monkeypatch.setattr(crud_module, 'read_market_activity_data', recording_read)
    return calls


@pytest.fixture
def client():
    """A TestClient over the store app, NOT entered as a context manager (no lifespan runs)."""
    sentinel = _SessionSentinel()
    app.dependency_overrides[async_db] = lambda: sentinel
    try:
        test_client = TestClient(app)
        test_client.session_sentinel = sentinel
        yield test_client
    finally:
        app.dependency_overrides.clear()


def _get(client: TestClient, params: dict[str, str]):
    return client.get(BARS_URL, params=params)


def _errors(response) -> list[dict[str, Any]]:
    # problem+json since TE-6 (tj-3mk3u5.37.8). The per-field list moved from `detail` to `errors`;
    # validation_errors reads it and pins the envelope every case here now shares.
    return validation_errors(response)


ALL_PARAMS = {
    'dataset_id': str(DATASET_ID),
    'asset_symbol': 'aapl',
    'source': 'ALPACA',
    'feed': 'IEX',
    'granularity': '1day',
    'start': '2026-01-02T00:00:00Z',
    'end': '2026-01-09T00:00:00Z',
}


def test_the_bound_query_reaches_the_crud_unchanged(client: TestClient, received: list):
    """F2 coverage item 8, and the route half of item 22: the bound query reaches the crud.

    Every query parameter reaches read_market_activity_data, on the query object the route bound
    -- not a freshly built one -- together with the session async_db supplied.
    """
    response = _get(client, ALL_PARAMS)

    assert response.status_code == 200, response.text
    assert response.json() == []
    assert len(received) == 1
    db, query = received[0]
    assert db is client.session_sentinel
    assert isinstance(query, StockDataMarketActivityQuery)
    assert query.model_dump() == _query(**FIELD_VALUES).model_dump()


@pytest.mark.parametrize('selector', ['dataset_id', 'asset_symbol'])
def test_a_selector_alone_is_accepted_over_http(selector: str, client: TestClient, received: list):
    """#23, HTTP half: either selector alone is a complete request."""
    response = _get(client, {selector: ALL_PARAMS[selector]})

    assert response.status_code == 200, response.text
    assert len(received) == 1
    assert getattr(received[0][1], selector) == FIELD_VALUES[selector]


@pytest.mark.parametrize(
    'params',
    [{}, {k: v for k, v in ALL_PARAMS.items() if k not in ('dataset_id', 'asset_symbol')}],
    ids=['no-parameters', 'every-field-but-a-selector'],
)
def test_a_query_naming_no_selector_is_refused_with_422(params: dict, client: TestClient, received: list):
    """#23, HTTP half (user ruling 5, tj-vhboky.20): no selector, no read.

    The refusal is a model-level error, so it carries loc ['query'] and no field name (measured on
    FastAPI 0.141.1, tj-vhboky.25 addendum); the message names both selectors. The crud is never
    reached.
    """
    errors = _errors(_get(client, params))

    assert len(errors) == 1, errors
    assert errors[0]['loc'] == ['query']
    assert errors[0]['type'] == 'value_error'
    assert 'dataset_id' in errors[0]['msg'] and 'asset_symbol' in errors[0]['msg']
    assert received == []


@pytest.mark.parametrize('with_dataset', [True, False], ids=['with-dataset_id', 'symbol-only'])
@pytest.mark.parametrize('blank', BLANK_SYMBOLS.values(), ids=BLANK_SYMBOLS.keys())
def test_a_blank_symbol_is_refused_with_422(blank: str, with_dataset: bool, client: TestClient, received: list):
    """F1c, HTTP half: ?asset_symbol= (or whitespace) is the same 422 as a query naming nothing.

    User ruling 2026-09-28 on tj-vhboky.28: loc ['query'], type value_error -- NOT the superseded
    field-level ['query', 'asset_symbol'] -- with or without dataset_id. Before 1331ef5 this was an
    empty 200 with the crud called on a symbol that matches nothing. The crud is never reached.
    """
    params = {'asset_symbol': blank} | ({'dataset_id': str(DATASET_ID)} if with_dataset else {})

    errors = _errors(_get(client, params))

    assert [(error['loc'], error['type']) for error in errors] == [(['query'], 'value_error')]
    assert 'asset_symbol' in errors[0]['msg'] and 'blank' in errors[0]['msg']
    assert received == []


def test_a_field_error_is_not_masked_by_the_selector_refusal(client: TestClient, received: list):
    """A bad source with no selector reports source, not the refusal.

    The model_validator is mode='after' and does not run over a field error. Model-level twin:
    schemas/tests/test_schemas_smoke_data_store.py (b87db45).
    """
    errors = _errors(_get(client, {'source': 'NOT-A-SOURCE'}))

    assert [error['loc'] for error in errors] == [['query', 'source']]
    assert errors[0]['type'] == 'enum'
    assert received == []


@pytest.mark.parametrize('unknown', ['bogus', 'expiry', 'query'])
def test_an_unknown_or_removed_parameter_is_refused_with_422(unknown: str, client: TestClient, received: list):
    """Query() over Depends() (tj-vhboky.25 item 1): an unknown parameter is refused, not dropped.

    Under Depends() the model's extra='forbid' never sees the query string, so a mistyped filter
    name silently becomes "no filter" -- tj-6z03hd again. expiry and query are the two fields F1
    removed (tj-vhboky.20 21:41 rulings 1 and 2); they are unknown now, and must stay refused.
    """
    errors = _errors(_get(client, {'dataset_id': str(DATASET_ID), unknown: '1'}))

    assert [(error['loc'], error['type']) for error in errors] == [(['query', unknown], 'extra_forbidden')]
    assert received == []


@pytest.mark.parametrize('bound', ['start', 'end'])
def test_a_naive_bound_is_refused_with_422(bound: str, client: TestClient, received: list):
    """tj-vhboky.20 D2 = (A): a naive bound is refused, not converted.

    Compared against a timestamptz column, a naive bound would be read in the session timezone.
    """
    errors = _errors(_get(client, {'dataset_id': str(DATASET_ID), bound: '2026-01-02T00:00:00'}))

    assert [(error['loc'], error['type']) for error in errors] == [(['query', bound], 'timezone_aware')]
    assert received == []


@pytest.mark.parametrize('bound', ['start', 'end'])
def test_an_offset_bound_is_the_instant_it_names(bound: str, client: TestClient, received: list):
    """The tj-1bl90i style: +05:00 is accepted and reaches the crud as the instant it names."""
    response = _get(client, {'dataset_id': str(DATASET_ID), bound: '2026-01-02T05:00:00+05:00'})

    assert response.status_code == 200, response.text
    assert getattr(received[0][1], bound) == START
