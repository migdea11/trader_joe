"""Every bar shape refuses a naive timestamp, and an aware one keeps its instant (tj-vhboky.70).

WHY THIS FILE EXISTS (validator, gating ad7aefe). ``_AssetDataType.timestamp`` was a plain ``datetime``,
so a bar with no offset constructed, and the single-bar POST bound it to a timestamptz column, where
Postgres reads it in the SESSION timezone. The timestamp is part of the bar's natural key, so a shifted
instant is a different bar. The user's rulings (tj-1bl90i; D2 = A on tj-vhboky.20) say REFUSE, never
convert, and the field is now ``AwareDatetime`` on the base every bar shape inherits.

Nothing in the suite built a bar with a naive timestamp before this, so the change from ``datetime`` to
``AwareDatetime`` could be reverted with every other test green. That is the gap pinned here.

What is pinned, per shape:
  * a naive Python ``datetime`` is refused with exactly one error, at loc ``('timestamp',)``, of type
    ``timezone_aware``;
  * offset-less TEXT is refused the same way. Over HTTP a timestamp arrives as text, so a check that
    covered only the ``datetime`` object would leave the form a caller actually sends unpinned;
  * a value at a non-UTC offset constructs and keeps its instant. A validator that "fixed" the naive
    case by stamping UTC onto everything, or by dropping the offset, fails here.

The shapes are the four the bead names: the single-bar create, the update, the read model, and the item
type of the batch that data_ingest sends. The batch item is pinned three ways, because a caller reaches
it three ways: constructed directly (the item type), nested inside the batch (where the loc is longer),
and built through ``append_data``, which is how the Alpaca adapter builds it.

ASSIGNMENT IS THE ONE PATH CONSTRUCTION DOES NOT GUARD. The models do not set ``validate_assignment``,
so ``bar.timestamp = naive`` stores the naive value unrefused. When this file was written,
``_AssetDataType.add_data`` did exactly that, with no callers; it was left unpinned and reported as a
finding (tj-vhboky.70). 30f8bd2 deleted it (tj-vhboky.73) rather than turning on
``validate_assignment``, which would have changed assignment semantics for every bar shape to protect
a method nothing called. Construction now validates on every path the code offers. What is pinned for
that, at the end of this file, is structural: no method defined on any bar shape, or on the batch that
builds them, assigns ``timestamp``. A re-added mutator of that kind goes red there. Assignment from
OUTSIDE the models is not pinned and cannot be without ``validate_assignment``.

The marker is ``data_store``: the store is the component whose contract these models are.
"""

import ast
import inspect
import textwrap
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel, ValidationError

from common.enums.data_select import DataType
from common.enums.data_stock import Feed
from schemas.data_store.asset_data_interface import _AssetDataType
from schemas.data_store.stock.market_activity_data import (
    BatchStockDataMarketActivityCreate,
    StockDataMarketActivity,
    StockDataMarketActivityCreate,
    StockDataMarketActivityData,
    StockDataMarketActivityUpdate,
)


pytestmark = pytest.mark.data_store

DATASET_ID = UUID('00000000-0000-0000-0000-000000000070')
WHEN = datetime(2026, 1, 2, tzinfo=UTC)

_BAR: dict[str, Any] = {'open': 1.0, 'high': 2.0, 'low': 0.5, 'close': 1.5, 'volume': 100, 'trade_count': 7}
_IDENTIFIER: dict[str, Any] = {'asset_symbol': 'VFV', 'source': 'ALPACA', 'granularity': '1day'}

# The batch item type, parametrised exactly as BatchStockDataMarketActivityCreate.dataset declares it.
BatchItem = _AssetDataType[StockDataMarketActivityData]

# Every bar shape, with every field it requires EXCEPT timestamp. Each is complete otherwise, so the one
# error a refusal reports can only be the timestamp.
SHAPES: list[Any] = [
    pytest.param(
        StockDataMarketActivityCreate,
        _IDENTIFIER | {'data': _BAR, 'dataset_id': DATASET_ID, 'feed': Feed.IEX},
        id='single-bar-create',
    ),
    pytest.param(
        StockDataMarketActivityUpdate, _IDENTIFIER | {'data': _BAR, 'dataset_id': DATASET_ID, 'id': 1}, id='update'
    ),
    pytest.param(
        StockDataMarketActivity,
        _IDENTIFIER
        | {'data': _BAR, 'dataset_id': DATASET_ID, 'feed': Feed.IEX, 'id': 1, 'created_at': WHEN, 'updated_at': WHEN},
        id='read-model',
    ),
    pytest.param(BatchItem, {'data': _BAR}, id='batch-item'),
]

# The same wall-clock reading twice: once as an object with no tzinfo, once as the text a JSON body
# carries. Neither says which instant it means.
NAIVE = [
    pytest.param(datetime(2026, 1, 2, 9, 30), id='naive-datetime'),
    pytest.param('2026-01-02T09:30:00', id='offset-less-text'),
]

# 09:30 at UTC-05:00 is 14:30 UTC. A non-UTC offset, so a value silently re-read as UTC would be off by
# five hours and fail the instant comparison.
EASTERN = timezone(timedelta(hours=-5))
AWARE = [
    pytest.param(datetime(2026, 1, 2, 9, 30, tzinfo=EASTERN), id='aware-datetime'),
    pytest.param('2026-01-02T09:30:00-05:00', id='offset-text'),
]
INSTANT = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)


def _errors(excinfo: pytest.ExceptionInfo[ValidationError]) -> list[tuple[tuple, str]]:
    return [(error['loc'], error['type']) for error in excinfo.value.errors()]


@pytest.mark.parametrize('naive', NAIVE)
@pytest.mark.parametrize(('model', 'payload'), SHAPES)
def test_each_bar_shape_refuses_a_naive_timestamp(model: type[BaseModel], payload: dict, naive: Any):
    """Exactly one error, at the timestamp, of type timezone_aware.

    The list is compared whole. A refusal that also reported some other field would mean the fixture
    is incomplete and the timestamp is not what failed; a refusal of another TYPE (datetime_parsing,
    say) would mean the text was rejected for a reason unrelated to the missing offset.
    """
    with pytest.raises(ValidationError) as excinfo:
        model(**payload, timestamp=naive)

    assert _errors(excinfo) == [(('timestamp',), 'timezone_aware')]


@pytest.mark.parametrize('aware', AWARE)
@pytest.mark.parametrize(('model', 'payload'), SHAPES)
def test_each_bar_shape_keeps_the_instant_of_an_offset_bearing_timestamp(
    model: type[BaseModel], payload: dict, aware: Any
):
    """The refusal is for naive values only; an aware one constructs and means the same instant.

    Compared as an aware-to-aware equality, which is instant equality in Python, and the value is
    asserted to still be aware, so a result that dropped its offset cannot pass by comparing unequal
    types.
    """
    built = model(**payload, timestamp=aware)

    assert built.timestamp.tzinfo is not None, 'the offset was dropped from an aware timestamp'
    assert built.timestamp == INSTANT, f'{aware!r} was stored as {built.timestamp!r}, not {INSTANT!r}'


@pytest.mark.parametrize('naive', NAIVE)
def test_a_batch_refuses_a_naive_timestamp_on_a_nested_item(naive: Any):
    """The batch data_ingest sends carries its bars nested, so the loc names the item's position."""
    with pytest.raises(ValidationError) as excinfo:
        BatchStockDataMarketActivityCreate(
            **_IDENTIFIER,
            dataset_id=DATASET_ID,
            feed=Feed.IEX,
            dataset={DataType.MARKET_ACTIVITY: [{'timestamp': naive, 'data': _BAR}]},
        )

    assert _errors(excinfo) == [(('dataset', 'market-activity', 0, 'timestamp'), 'timezone_aware')]


def _empty_batch() -> BatchStockDataMarketActivityCreate:
    return BatchStockDataMarketActivityCreate(**_IDENTIFIER, dataset_id=DATASET_ID, feed=Feed.IEX, dataset={})


@pytest.mark.parametrize('naive', NAIVE)
def test_append_data_refuses_a_naive_timestamp_and_appends_nothing(naive: Any):
    """append_data is how the Alpaca adapter builds the batch, one bar at a time.

    It constructs the item, so it refuses, and no bar is left in the batch. The check is on stored
    bars, not on the dict being empty: append_data creates the list for a new data type BEFORE it
    builds the item, so a refusal leaves an empty list behind. That list is harmless, and asserting on
    it would pin an accident of ordering.
    """
    batch = _empty_batch()

    with pytest.raises(ValidationError) as excinfo:
        batch.append_data(DataType.MARKET_ACTIVITY, StockDataMarketActivityData(**_BAR), naive)

    assert _errors(excinfo) == [(('timestamp',), 'timezone_aware')]
    assert not any(batch.dataset.values()), f'a refused bar was still appended: {batch.dataset}'


@pytest.mark.parametrize('aware', AWARE)
def test_append_data_keeps_the_instant_of_an_offset_bearing_timestamp(aware: Any):
    batch = _empty_batch()

    batch.append_data(DataType.MARKET_ACTIVITY, StockDataMarketActivityData(**_BAR), aware)

    (item,) = batch.dataset[DataType.MARKET_ACTIVITY]
    assert item.timestamp.tzinfo is not None, 'the offset was dropped from an aware timestamp'
    assert item.timestamp == INSTANT, f'{aware!r} was stored as {item.timestamp!r}, not {INSTANT!r}'


# ---------------------------------------------------------------------------------------------
# NO METHOD ASSIGNS THE FIELD (tj-vhboky.73). Every class the bar shapes and the batch inherit from
# that this repository defines, read as source. pydantic's own classes are excluded: they are not
# ours to pin and do not assign a field named timestamp.

_TIMESTAMP_CARRIERS = [
    StockDataMarketActivityCreate,
    StockDataMarketActivityUpdate,
    StockDataMarketActivity,
    BatchItem,
    BatchStockDataMarketActivityCreate,
]


def _own_classes() -> list[type]:
    classes = {klass for carrier in _TIMESTAMP_CARRIERS for klass in inspect.getmro(carrier)}
    return sorted(
        (klass for klass in classes if klass.__module__.startswith(('schemas.', 'common.'))),
        key=lambda klass: klass.__qualname__,
    )


def _assigns_timestamp(node: ast.AST) -> bool:
    """An assignment target, or a setattr / __setattr__ call, naming the attribute timestamp."""
    if isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return any(isinstance(target, ast.Attribute) and target.attr == 'timestamp' for target in targets)
    if isinstance(node, ast.Call):
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ''
        return name in {'setattr', '__setattr__'} and any(
            isinstance(arg, ast.Constant) and arg.value == 'timestamp' for arg in node.args
        )
    return False


def test_the_scan_reaches_the_bar_base():
    """The structural pin below is only as good as the classes it reads; _AssetDataType must be one."""
    assert _AssetDataType in _own_classes()


def test_no_method_on_a_bar_shape_assigns_the_timestamp():
    """Construction refuses a naive timestamp; a method that ASSIGNS one does not (no validate_assignment).

    add_data was such a method until 30f8bd2. Rather than rely on nobody writing another, every
    function defined on the classes above is parsed and searched for an assignment to an attribute
    named timestamp, on any object: ``self.timestamp = ...`` as add_data did, and ``item.timestamp =
    ...`` on a nested bar, both bypass the refusal. A method that needs to change a bar's timestamp
    should build a new one, which validates.
    """
    offenders = []
    for klass in _own_classes():
        for name, member in vars(klass).items():
            function = getattr(member, '__func__', member)
            if not inspect.isfunction(function):
                continue
            tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
            if any(_assigns_timestamp(node) for node in ast.walk(tree)):
                offenders.append(f'{klass.__qualname__}.{name}')

    assert offenders == [], f'these methods assign timestamp without validation: {offenders}'
