"""Smoke tests for ``schemas/data_store`` -- the contract the store service is driven by.

Scope, per tj-goxb2r and ADR tj-fdb9gz: every module under the package imports, and every public
model constructs from a minimal valid payload and rejects an empty one -- or, where an empty
payload is legitimately valid, rejects a payload that violates a declared constraint, with the
constraint named per class. Reachability and shape, never business correctness.

The marker is ``data_store`` and not ``schemas``: there is deliberately no ``schemas`` marker,
because ``schemas`` is a layer inside every component rather than a component of its own
(pytest.ini). A test carries the marker of the component whose interface it drives, whatever
directory it sits in. A single file wearing all three markers would be selected by every component
selection and would therefore tell ``make test-component COMPONENT=data_store`` nothing.

Why this exists now: the Phase 1 restructure (tj-55cczk) re-paths every one of these modules, and
these tests are what tells you whether the move broke an import. This package is the one with a
genuinely fragile import -- it reaches into ``routers.data_store.app_endpoints`` for its field
descriptions, the wrong-direction dependency recorded in ``schemas/CLAUDE.md``, so an import here
drags a router module in with it.

No external resource. Nothing here is marked ``external`` and nothing skips.
"""

import importlib
import inspect
import pkgutil
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel, ValidationError
from pydantic.fields import FieldInfo

import schemas.data_store
from schemas.data_store.asset_data_interface import (
    AssetData,
    AssetDataCreate,
    AssetDataDeleteById,
    AssetDataPath,
    AssetDataQuery,
    AssetDataUpdate,
    BatchAssetDataCreate,
)
from schemas.data_store.asset_dataset_store import (
    AssetDatasetStore,
    AssetDatasetStoreCreate,
    AssetDatasetStoreDelete,
    AssetDatasetStoreGetById,
    AssetDatasetStoreUpdate,
    StoreAssetDatasetBody,
    StoreAssetDatasetPath,
    StoreAssetDatasetQuery,
)
from schemas.data_store.stock.market_activity_data import (
    BatchStockDataMarketActivityCreate,
    StockDataMarketActivity,
    StockDataMarketActivityCreate,
    StockDataMarketActivityData,
    StockDataMarketActivityDeleteById,
    StockDataMarketActivityQuery,
    StockDataMarketActivityUpdate,
    StockMarketActivityDataQuery,
)


pytestmark = pytest.mark.data_store


# Committed module inventory. Discovery on its own is vacuous -- a package that lost every module
# would still 'import all of them'. Asserting the discovered set EQUALS this one is what makes the
# import test fail on a module that was moved, renamed or deleted by the Phase 1 re-path.
EXPECTED_MODULES = frozenset(
    {
        'schemas.data_store.asset_data_interface',
        'schemas.data_store.asset_dataset_store',
        'schemas.data_store.stock',
        'schemas.data_store.stock.market_activity_data',
    }
)

DATASET_ID = UUID('00000000-0000-0000-0000-000000000001')
WHEN = datetime(2026, 1, 1, tzinfo=UTC)

# The generic models in asset_data_interface are used here UNPARAMETRISED, which binds DT and QT to
# Any, so `data` and `query` accept anything. The parametrised bindings are what the stock models
# test below, and that difference is the point of test_generic_parameter_actually_binds.
_OPAQUE_DATA: dict[str, Any] = {'anything': 1}

_BAR: dict[str, Any] = {
    'open': 1.0,
    'high': 2.0,
    'low': 0.5,
    'close': 1.5,
    'volume': 100,
    'trade_count': 7,
    'split_factor': 1.0,
    'dividends_factor': 1.0,
}

_IDENTIFIER: dict[str, Any] = {'asset_symbol': 'VFV', 'source': 'ALPACA', 'granularity': '1day'}

# `expiry` is `datetime | None` with no default, so the key must be present even when null.
_DATA_FIELDS: dict[str, Any] = {'timestamp': WHEN, 'expiry': None}

_QUERY_IDENTIFIER: dict[str, Any] = {
    'asset_symbol': 'VFV',
    'source': None,
    'granularity': None,
    'dataset_id': None,
    'start': None,
    'end': None,
    'expiry': None,
}

_DATASET_BODY: dict[str, Any] = {'source': 'ALPACA', 'granularity': '1day'}
_DATASET_PATH: dict[str, Any] = {'asset_type': 'stock', 'data_type': 'market-activity', 'asset_symbol': 'VFV'}

# Every public model whose empty payload is REJECTED, with a minimal valid payload. Minimal means:
# every required field and nothing else.
CONSTRUCT_CASES: list[tuple[type[BaseModel], dict[str, Any]]] = [
    (AssetDataPath, {'asset_type': 'stock', 'data_type': 'market-activity'}),
    (AssetDataCreate, _IDENTIFIER | _DATA_FIELDS | {'data': _OPAQUE_DATA, 'dataset_id': DATASET_ID}),
    (BatchAssetDataCreate, _IDENTIFIER | {'dataset_id': DATASET_ID, 'dataset': {}}),
    (AssetDataUpdate, _IDENTIFIER | _DATA_FIELDS | {'data': _OPAQUE_DATA, 'dataset_id': DATASET_ID, 'id': 1}),
    (AssetDataQuery, _QUERY_IDENTIFIER | {'query': None}),
    (
        AssetData,
        _IDENTIFIER
        | _DATA_FIELDS
        | {'data': _OPAQUE_DATA, 'dataset_id': DATASET_ID, 'id': 1, 'created_at': WHEN, 'updated_at': WHEN},
    ),
    (StoreAssetDatasetBody, _DATASET_BODY),
    (StoreAssetDatasetPath, _DATASET_PATH),
    (AssetDatasetStoreCreate, _DATASET_BODY | _DATASET_PATH),
    (AssetDatasetStoreUpdate, _DATASET_BODY | _DATASET_PATH | {'id': DATASET_ID}),
    (AssetDatasetStoreGetById, {'id': DATASET_ID}),
    (AssetDatasetStoreDelete, {'id': DATASET_ID}),
    (
        AssetDatasetStore,
        _DATASET_BODY | _DATASET_PATH | {'id': DATASET_ID, 'item_count': 0, 'created_at': WHEN, 'updated_at': WHEN},
    ),
    (StockDataMarketActivityData, _BAR),
    (StockDataMarketActivityCreate, _IDENTIFIER | _DATA_FIELDS | {'data': _BAR, 'dataset_id': DATASET_ID}),
    (BatchStockDataMarketActivityCreate, _IDENTIFIER | {'dataset_id': DATASET_ID, 'dataset': {}}),
    (StockDataMarketActivityUpdate, _IDENTIFIER | _DATA_FIELDS | {'data': _BAR, 'dataset_id': DATASET_ID, 'id': 1}),
    (StockDataMarketActivityQuery, _QUERY_IDENTIFIER | {'query': {}}),
    (
        StockDataMarketActivity,
        _IDENTIFIER
        | _DATA_FIELDS
        | {'data': _BAR, 'dataset_id': DATASET_ID, 'id': 1, 'created_at': WHEN, 'updated_at': WHEN},
    ),
]

# Models whose empty payload is LEGITIMATELY VALID -- every field is optional with a default -- so
# the bead's fallback applies: reject a payload that violates a declared constraint, and name the
# constraint here rather than leaving a reader to infer it.
CONSTRAINT_CASES: list[tuple[type[BaseModel], dict[str, Any], str, str]] = [
    (
        StoreAssetDatasetQuery,
        {'source': 'NOT_A_BROKER'},
        'source',
        '`source` is `DataSource | None`, so a value outside the DataSource enum is rejected even '
        'though omitting the field entirely is fine.',
    )
]

# Models with NO declared constraint to violate, so neither an empty-payload rejection nor a
# constraint case can be written honestly. Listing one here is a finding, not a gap in the tests.
#
# StockMarketActivityDataQuery is `class ...(BaseModel): pass` -- zero fields, and Pydantic v2
# ignores extras by default, so no payload at all can be invalid. The only assertion worth making
# is that it constructs and is empty, which is what test_no_constraint_model_constructs does. A
# manufactured rejection case here would assert Pydantic's behaviour, not this contract's.
NO_CONSTRAINT_CASES: list[type[BaseModel]] = [StockMarketActivityDataQuery]

# Public classes in the package that are NOT Pydantic models. Covered by
# test_delete_contract_is_not_a_validating_model, which pins the known bug rather than pretending
# these are models. See tj-9dqfjo.
KNOWN_NON_MODELS = frozenset({'AssetDataDeleteById', 'StockDataMarketActivityDeleteById'})


def _public_classes(module) -> set[str]:
    """Return the names of classes defined in ``module``, excluding private ones.

    ``name.isidentifier()`` filters out Pydantic's concrete generic aliases: parametrising a
    generic model injects a key like ``AssetDataCreate[StockDataMarketActivityData]`` into the
    defining module's namespace, whose ``__module__`` is that module. Those are the same class
    under a different binding, not a new part of the contract -- and whether they are present at
    all depends on which other module has been imported first, which would make this check
    order-dependent.

    Args:
        module: An imported module object.

    Returns:
        The set of public class names the module itself defines.
    """
    return {
        name
        for name, obj in vars(module).items()
        if name.isidentifier()
        and not name.startswith('_')
        and inspect.isclass(obj)
        and obj.__module__ == module.__name__
    }


def _case_id(value) -> str | None:
    """Name each parametrized case after its model class, letting pytest label the rest.

    Args:
        value: One argument of a parametrized case.

    Returns:
        The class name, or ``None`` to fall back to pytest's own representation.
    """
    return value.__name__ if isinstance(value, type) else None


def test_module_inventory_matches_the_package():
    discovered = {name for _, name, _ in pkgutil.walk_packages(schemas.data_store.__path__, 'schemas.data_store.')}
    assert discovered == EXPECTED_MODULES


@pytest.mark.parametrize('module_name', sorted(EXPECTED_MODULES))
def test_module_imports(module_name: str):
    assert importlib.import_module(module_name) is not None


def test_every_public_model_is_covered():
    """Fail on a model added to the package that no case above exercises.

    Without this the construct/reject pairs silently stop being a smoke test of the package and
    become a smoke test of whatever someone last remembered to list.
    """
    covered = (
        {model.__name__ for model, _ in CONSTRUCT_CASES}
        | {model.__name__ for model, _, _, _ in CONSTRAINT_CASES}
        | {model.__name__ for model in NO_CONSTRAINT_CASES}
        | KNOWN_NON_MODELS
    )
    declared = set()
    for module_name in sorted(EXPECTED_MODULES):
        declared |= _public_classes(importlib.import_module(module_name))
    assert declared == covered


@pytest.mark.parametrize(('model', 'payload'), CONSTRUCT_CASES, ids=_case_id)
def test_model_constructs_from_minimal_payload(model: type[BaseModel], payload: dict[str, Any]):
    instance = model(**payload)
    for field in payload:
        assert field in instance.model_fields_set


@pytest.mark.parametrize(('model', 'payload'), CONSTRUCT_CASES, ids=_case_id)
def test_model_rejects_empty_payload(model: type[BaseModel], payload: dict[str, Any]):
    """Reject ``{}``, and name the fields the rejection is about.

    Asserting only that ``ValidationError`` was raised would pass on a model that rejects the empty
    payload for some unrelated reason. The required-field set is the thing under test.

    Args:
        model: The model class.
        payload: Its minimal valid payload; its keys are the fields expected to be reported missing.
    """
    with pytest.raises(ValidationError) as excinfo:
        model()
    missing = {str(error['loc'][0]) for error in excinfo.value.errors() if error['type'] == 'missing'}
    assert missing == set(payload)


@pytest.mark.parametrize(('model', 'payload', 'field', 'constraint'), CONSTRAINT_CASES, ids=_case_id)
def test_all_optional_model_accepts_empty_and_rejects_a_constraint_violation(
    model: type[BaseModel], payload: dict[str, Any], field: str, constraint: str
):
    """Check the pair that replaces empty-payload rejection where ``{}`` is valid.

    Args:
        model: The model class.
        payload: A payload that violates the declared constraint.
        field: The field the violation is expected to be reported against.
        constraint: The constraint being violated, for the reader of a failure.
    """
    assert model().model_dump(exclude_unset=True) == {}
    with pytest.raises(ValidationError, match=field) as excinfo:
        model(**payload)
    assert {str(error['loc'][0]) for error in excinfo.value.errors()} == {field}, constraint


@pytest.mark.parametrize('model', NO_CONSTRAINT_CASES, ids=_case_id)
def test_no_constraint_model_constructs(model: type[BaseModel]):
    """Construct a model that declares no field, and assert exactly that and nothing more.

    There is no rejection case to write: with no fields and Pydantic's default of ignoring extras,
    no payload can be invalid. Asserting the field set is empty is the honest assertion -- it
    fails the day the model gains a field, which is the day a rejection case becomes writable.

    Args:
        model: The model class.
    """
    assert model.model_fields == {}
    assert model().model_dump() == {}


@pytest.mark.parametrize(
    ('model', 'field'),
    [
        (StockDataMarketActivityCreate, 'data'),
        (StockDataMarketActivityUpdate, 'data'),
        (StockDataMarketActivity, 'data'),
    ],
    ids=_case_id,
)
def test_generic_parameter_actually_binds(model: type[BaseModel], field: str):
    """Prove the stock models bind DT rather than inheriting an unparametrised ``Any``.

    ``AssetDataCreate[StockDataMarketActivityData]`` and ``AssetDataCreate`` construct identically
    from a valid payload, so the construct test above cannot tell them apart. Feeding an empty
    ``data`` can: bound, it reports the missing bar fields; unbound, it is accepted as ``Any``.

    Args:
        model: The parametrised stock model.
        field: The field carrying the generic parameter.
    """
    payload = _IDENTIFIER | _DATA_FIELDS | {'data': {}, 'dataset_id': DATASET_ID, 'id': 1}
    payload |= {'created_at': WHEN, 'updated_at': WHEN}
    with pytest.raises(ValidationError) as excinfo:
        model(**payload)
    nested = {error['loc'][1] for error in excinfo.value.errors() if error['loc'][0] == field}
    assert nested == set(_BAR)


def test_delete_contract_is_not_a_validating_model():
    """Pin tj-9dqfjo: the delete contract is a plain ABC, so its ``Field()`` is never processed.

    ``AssetDataDeleteById`` inherits ``ABC`` alone, not ``BaseModel``, while every sibling in the
    file inherits ``_AssetIdentifier -> BaseModel``. ``dataset_id`` is therefore a class attribute
    holding a ``FieldInfo``, and ``dataset_id`` is never validated on the way in.

    This test asserts what is true today rather than what should be, so the fix flips it visibly
    instead of leaving the delete path quietly unvalidated. When tj-9dqfjo is fixed, this test is
    the thing that must be rewritten -- and that is the point of it.
    """
    for model in (AssetDataDeleteById, StockDataMarketActivityDeleteById):
        assert not issubclass(model, BaseModel), f'{model.__name__} is now a model -- tj-9dqfjo fixed?'
        assert isinstance(model.dataset_id, FieldInfo)
        assert model() is not None


def test_expiry_default_is_computed_per_instance(monkeypatch: pytest.MonkeyPatch):
    """Pin tj-swean0: two bodies built at different times get different expiry defaults.

    A plain ``datetime.now() + timedelta(days=1)`` default is evaluated ONCE, at import, so every
    instance in a long-lived process would share an expiry frozen at process start. A wall-clock
    delta between two consecutive constructions cannot assert that -- the microseconds differ under
    either implementation -- so the model module's own ``datetime`` is replaced by a clock handing
    out two known, far-apart instants. The default factory's lambda resolves ``datetime`` from
    those module globals, which is what makes the substitution deterministic.

    Args:
        monkeypatch: Replaces ``datetime`` in ``schemas.data_store.asset_dataset_store``.
    """
    instants = iter((datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 6, 1, tzinfo=UTC)))
    monkeypatch.setattr('schemas.data_store.asset_dataset_store.datetime', SimpleNamespace(now=lambda: next(instants)))

    first = StoreAssetDatasetBody(**_DATASET_BODY)
    second = StoreAssetDatasetBody(**_DATASET_BODY)

    assert first.expiry == datetime(2026, 1, 2, tzinfo=UTC)
    assert second.expiry == datetime(2026, 6, 2, tzinfo=UTC)
    # Cheap second line pinning the mechanism; it is not a substitute for the behaviour above.
    assert StoreAssetDatasetBody.model_fields['expiry'].default_factory is not None
