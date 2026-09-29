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
import json
import pkgutil
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel, ValidationError
from pydantic.fields import FieldInfo

import common.enums.data_stock
import schemas.data_store
from common.enums.data_stock import Feed, NoTapeFeed, UsEquityFeed
from schemas.data_store.asset_data_interface import (
    AssetData,
    AssetDataCreate,
    AssetDataPath,
    AssetDataQuery,
    AssetDataUpdate,
    BatchAssetDataCreate,
    _AssetDataType,
    _AssetIdentifier,
    _AssetIdentifierQuery,
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
    StockDataMarketActivityQuery,
    StockDataMarketActivityUpdate,
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

# The generic models in asset_data_interface are used here UNPARAMETRISED, which binds DT to Any,
# so `data` accepts anything. The parametrised bindings are what the stock models test below, and
# that difference is the point of test_generic_parameter_actually_binds.
#
# THERE IS NO QT ANY MORE. This comment used to name a second type parameter, QT, binding the
# nested `query` field of the bars query. a8218d5 (tj-vhboky.21, under the user ruling of
# 2026-09-27 on tj-vhboky.20) removed that field and with it the parameter, so the query models
# are no longer generic at all -- test_the_bar_query_is_no_longer_generic pins that.
_OPAQUE_DATA: dict[str, Any] = {'anything': 1}

# BARS ARE RAW (tj-vhboky.1 section 6): no split_factor, no dividends_factor. Corporate actions
# live in their own events table and are applied server-side at read time, so an adjusted value is
# derived and disposable. The two columns that used to sit here were hard-coded to 1.0 by the
# Alpaca adapter and nothing ever wrote a real factor.
_BAR: dict[str, Any] = {'open': 1.0, 'high': 2.0, 'low': 0.5, 'close': 1.5, 'volume': 100, 'trade_count': 7}

_IDENTIFIER: dict[str, Any] = {'asset_symbol': 'VFV', 'source': 'ALPACA', 'granularity': '1day'}

# THE BAR'S IDENTITY PAIR, CARRIED BY THE CREATE PATHS AND THE READ MODEL AND BY NOTHING ELSE.
# `dataset_id` says which dataset the bar belongs to; `feed` says which tape served it. Both are
# identity on the bar and BOTH ARE REQUIRED WITH NO DEFAULT (77f3a6c implementing tj-rh4b7f for the
# create paths, 8240133 implementing tj-5dvgaa for the read model).
#
# THIS USED TO BE `_CREATE_IDENTITY`, AND THE RENAME IS THE tj-5dvgaa CHANGE rather than a tidy-up.
# The comment here used to say the pair was what "only the CREATE paths carry", and listed the
# AssetData read model among the models that declare no feed. That stopped being true: the read
# model could not report which tape served a bar the store had already recorded one for, which is
# the gap tj-5dvgaa closed. The pair is now create-and-read.
#
# IT STILL SITS HERE AND NOT IN _IDENTIFIER, and the reason narrowed rather than went away.
# _IDENTIFIER is also the base of AssetDataUpdate, which declares no feed -- an update addresses a
# row by id -- and it is `extra='forbid'`, so handing it a feed would RAISE rather than be ignored.
# The bars QUERY used to be named here alongside the update as a second model with no feed. It now
# carries an OPTIONAL one (a8218d5, tj-vhboky.21): a filter, not the required identity pair, so it
# still does not belong in this dict.
#
# feed has NO DEFAULT on purpose: the enum no longer has an UNKNOWN member to default to (32438a9,
# tj-vhboky.1 ruling of 2026-09-25). An adapter that cannot resolve a tape has failed, and a
# missing feed must fail at the call site rather than be written as a sentinel into an identity
# column that a later correction could not rewrite. On the READ side the same absence of a default
# means a reader that cannot supply one fails loudly instead of reporting None for a value the
# NOT NULL column actually holds.
_BAR_IDENTITY: dict[str, Any] = {'dataset_id': DATASET_ID, 'feed': Feed.IEX}

# THE LIFETIME BELONGS TO THE DATASET, NOT THE BAR (tj-vhboky.1 section 9). `expiry` is gone from
# this level entirely: it was a per-FETCH attribute stored on a per-BAR row, which is how it
# acquired the same last-write-wins defect as dataset_id. It is now a column on the dataset entry.
_DATA_FIELDS: dict[str, Any] = {'timestamp': WHEN}

# EVERY FIELD IS IDENTITY (tj-vhboky.1 section 2), so `owner` and `start` are required with no
# default. `owner` has no default deliberately: a default principal would put every strategy that
# forgot to name itself onto one shared dataset. `start` is required by the ruling closing the
# record's open question 2 -- an open start would mean "from the beginning of time", which no
# vendor serves. `expiry`, `expiry_type` and `update_type` all carry defaults, which is why they
# are absent from a MINIMAL payload.
#
# NO `feed` KEY, AND ITS ABSENCE IS THE CONTRACT rather than an omission from the fixture. An
# earlier revision of this list carried one, because the body declared `Feed | None = None`.
# tj-rh4b7f (2026-09-25) DEFERRED feed off the entry entirely: the store writes the entry row
# BEFORE it calls ingest, so at that moment no feed has been resolved, and feed is identity -- a
# placeholder written now and corrected later would mutate identity in place. Feed moved DOWN to
# the bar, where the adapter knows it. test_the_entry_carries_no_feed_and_the_bar_requires_one
# pins both halves of that move.
_DATASET_BODY: dict[str, Any] = {'owner': 'rebalancer', 'source': 'ALPACA', 'granularity': '1day', 'start': WHEN}
_DATASET_PATH: dict[str, Any] = {'asset_type': 'stock', 'data_type': 'market-activity', 'asset_symbol': 'VFV'}

# Every public model whose empty payload is REJECTED, with a minimal valid payload. Minimal means:
# every required field and nothing else.
CONSTRUCT_CASES: list[tuple[type[BaseModel], dict[str, Any]]] = [
    (AssetDataPath, {'asset_type': 'stock', 'data_type': 'market-activity'}),
    (AssetDataCreate, _IDENTIFIER | _DATA_FIELDS | _BAR_IDENTITY | {'data': _OPAQUE_DATA}),
    (BatchAssetDataCreate, _IDENTIFIER | _BAR_IDENTITY | {'dataset': {}}),
    (AssetDataUpdate, _IDENTIFIER | _DATA_FIELDS | {'data': _OPAQUE_DATA, 'dataset_id': DATASET_ID, 'id': 1}),
    (
        AssetData,
        _IDENTIFIER
        | _DATA_FIELDS
        | _BAR_IDENTITY
        | {'data': _OPAQUE_DATA, 'id': 1, 'created_at': WHEN, 'updated_at': WHEN},
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
    (StockDataMarketActivityCreate, _IDENTIFIER | _DATA_FIELDS | _BAR_IDENTITY | {'data': _BAR}),
    (BatchStockDataMarketActivityCreate, _IDENTIFIER | _BAR_IDENTITY | {'dataset': {}}),
    (StockDataMarketActivityUpdate, _IDENTIFIER | _DATA_FIELDS | {'data': _BAR, 'dataset_id': DATASET_ID, 'id': 1}),
    (
        StockDataMarketActivity,
        _IDENTIFIER | _DATA_FIELDS | _BAR_IDENTITY | {'data': _BAR, 'id': 1, 'created_at': WHEN, 'updated_at': WHEN},
    ),
]

# Models whose empty payload is LEGITIMATELY VALID -- every field is optional with a default -- so
# the bead's fallback applies: reject a payload that violates a declared constraint, and name the
# constraint here rather than leaving a reader to infer it.
#
# THE QUERY MODELS MOVED HERE FROM THE CONSTRUCT LIST, and the move was the contract change rather
# than a tidy-up (tj-vhboky.1 section 8, tj-6z03hd's escalation). Their fields were annotated
# `| None` with NO default, which in Pydantic v2 means required-but-nullable: a caller had to pass
# every one explicitly, so the object could not represent an unfiltered request and could not serve
# as an optional FastAPI query dependency. For the dataset search that is still the point of it,
# so an empty-payload REJECTION here would pin the opposite of the ruling.
#
# THE BARS QUERY HAS LEFT THIS LIST (F1b, tj-vhboky.26, ebb2439). It sat here with the same
# `{'source': 'NOT_A_BROKER'}` case until the user ruled on 2026-09-27 (tj-vhboky.20, 21:41 UTC,
# ruling 5) that an EMPTY bars query must be impossible to construct, and ebb2439 put a
# model_validator on the shared AssetDataQuery refusing one that names neither dataset_id nor
# asset_symbol. `{}` is no longer valid for it, so it cannot be an all-optional model. It moved to
# SELECTOR_CASES below, and the assertion these entries made -- a bad `source` on an otherwise empty
# query is reported at `source` and nowhere else -- moved with it, strengthened, into
# test_a_field_error_on_an_empty_bar_query_is_not_masked_by_the_refusal.
CONSTRAINT_CASES: list[tuple[type[BaseModel], dict[str, Any], str, str]] = [
    (
        StoreAssetDatasetQuery,
        {'source': 'NOT_A_BROKER'},
        'source',
        '`source` is `DataSource | None`, so a value outside the DataSource enum is rejected even '
        'though omitting the field entirely is fine.',
    )
]

# Models whose fields are each optional but which REFUSE the empty payload with a MODEL-LEVEL error,
# not a missing-field one, so they fit neither list above: test_model_rejects_empty_payload counts
# `missing` errors and would find none, and test_all_optional_model_accepts_empty_... would find `{}`
# refused. The payload is the smallest valid one -- a single selector. What the refusal looks like
# is pinned by test_an_empty_bar_query_is_refused; this list keeps both classes in the package-wide
# coverage and strict-receiver sweeps.
SELECTOR_CASES: list[tuple[type[BaseModel], dict[str, Any]]] = [
    (AssetDataQuery, {'dataset_id': DATASET_ID}),
    (StockDataMarketActivityQuery, {'dataset_id': DATASET_ID}),
]

# THE NO_CONSTRAINT_CASES LIST AND ITS TEST ARE GONE, because their only member is. The list held
# models with no declared constraint to violate, and its one entry was StockMarketActivityDataQuery,
# the zero-field type argument of the bars query's nested `query` field. a8218d5 (tj-vhboky.21)
# deleted both under the user ruling of 2026-09-27 ("sounds dangerous"; tj-vhboky.25 item 2). An
# empty list would have left test_no_constraint_model_constructs parametrised over nothing, which
# pytest reports as a skip -- a test that can never run. The deletion is audited by absence instead,
# in test_the_nested_query_submodel_is_gone, the way DELETED_PSEUDO_MODELS audits tj-9dqfjo. A future
# zero-field model still cannot slip through: test_every_public_model_is_covered fails on it until
# someone decides where it belongs.
DELETED_QUERY_MODELS = frozenset({'StockMarketActivityDataQuery'})

# Public classes in the package that are NOT Pydantic models. EMPTY, AND IT MUST STAY EMPTY.
#
# It held `AssetDataDeleteById` and `StockDataMarketActivityDeleteById`, deleted by tj-vhboky.2
# item I -- the ruled disposition for tj-9dqfjo. Both inherited `ABC` alone rather than
# `BaseModel` while declaring `dataset_id: UUID = Field(...)`, so `Field()` was never processed,
# `dataset_id` was a class attribute holding a `FieldInfo`, and nothing on the delete path was
# ever validated. Deleting was chosen over adding a `BaseModel` base because nothing serves a
# delete-by-id route for bars.
#
# This set is the escape hatch in test_every_public_model_is_covered: a name listed here is
# exempt from needing a construct or constraint case. Leaving it as an open list would let the
# next unvalidated pseudo-model be waved through by adding a string to it, so
# test_no_public_class_declares_fields_without_being_a_model below guards the SHAPE rather than
# the two names, and this set is kept empty on purpose.
KNOWN_NON_MODELS: frozenset[str] = frozenset()

# The full list tj-vhboky.2 item I enumerated before deleting, recorded here so the deletion is
# auditable from the tests rather than only from a commit message. The task required enumerating
# EVERY class in the package with this shape rather than assuming it was exactly two;
# test_no_public_class_declares_fields_without_being_a_model is what actually proves the
# enumeration was complete, by finding no survivor of the same shape.
DELETED_PSEUDO_MODELS = frozenset({'AssetDataDeleteById', 'StockDataMarketActivityDeleteById'})


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
        | {model.__name__ for model, _ in SELECTOR_CASES}
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


@pytest.mark.parametrize(
    ('model', 'payload'),
    # The constraint cases contribute `{}`, not their listed payload: that payload is the one that
    # VIOLATES their constraint, so adding an unknown key to it would raise two errors and the
    # assertion below would be measuring the violation as much as the unknown field.
    #
    # The SELECTOR_CASES contribute their one-selector payload, not `{}`: an empty bars query is
    # refused by its model_validator, and that refusal would be a second error beside the unknown key.
    CONSTRUCT_CASES + [(model, {}) for model, _, _, _ in CONSTRAINT_CASES] + SELECTOR_CASES,
    ids=_case_id,
)
def test_every_model_in_the_package_rejects_an_unknown_field(model: type[BaseModel], payload: dict[str, Any]):
    """The strict-receiver ruling, pinned across the WHOLE package rather than one model.

    ``schemas/inbound_contract.py`` (32438a9, tj-vhboky.1 ruling of 2026-09-25) sets
    ``extra='forbid'`` once, on a base every received contract inherits. The value of doing it on a
    base is that no model can be added to this package having quietly kept Pydantic's default, so
    the test that matches is one that sweeps every model rather than naming the interesting ones.
    It fails on a NEW model that forgot the base, which a per-model test never would.

    The CONSTRAINT_CASES models are swept too. Their valid payload is ``{}`` -- they are the
    all-optional query models -- and "accepts anything" is the failure mode strictness matters most
    for there: an unfiltered query and a query filtered on a misspelt field must not be the same
    request.

    WHAT THIS COSTS, recorded so it is not rediscovered during a release: a strict receiver creates
    a DEPLOY-ORDERING CONSTRAINT. Adding a field to a contract means the RECEIVING side deploys
    first, or the sender is rejected outright. That is the price of the guarantee, not a defect in
    it -- the same strictness that catches a field we deleted catches one we have not added yet.

    Args:
        model: The model class.
        payload: A payload it accepts, which this adds one unknown key to.
    """
    with pytest.raises(ValidationError) as excinfo:
        model(**payload | {'a_field_no_contract_declares': 1})
    errors = excinfo.value.errors()
    assert [error['loc'] for error in errors] == [('a_field_no_contract_declares',)]
    assert [error['type'] for error in errors] == ['extra_forbidden']


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


@pytest.mark.parametrize('name', sorted(DELETED_QUERY_MODELS))
def test_the_nested_query_submodel_is_gone(name: str):
    """Audit the removal of the bars query's nested submodel by absence from every module.

    REPLACES test_no_constraint_model_constructs, which is RETIRED rather than edited, because the
    one model it was parametrised over no longer exists. That test asserted that
    StockMarketActivityDataQuery constructed from nothing, declared no field and refused an unknown
    key: the right assertions about a zero-field type argument whose existence nobody questioned.
    The user ruling of 2026-09-27 (tj-vhboky.20, 21:21 and 21:41 UTC) questioned it and removed the
    nested ``query`` field it typed -- "sounds dangerous" -- and tj-vhboky.25 item 2 records why: a
    nested model cannot be an HTTP query parameter, nothing read it, and an ``extra='forbid'``
    contract that accepts a field and ignores it only looks validating.

    STRONGER THAN WHAT IT REPLACES, not weaker: the old test passed on any tree where the class
    existed and was empty; this one fails on any tree where the class exists at all, in any module
    of the package. A deleted class is invisible to every other test here, and coming back in a
    merge is exactly how it would reappear.

    Args:
        name: One deleted class name, checked against every module in the package.
    """
    for module_name in sorted(EXPECTED_MODULES):
        module = importlib.import_module(module_name)
        assert not hasattr(module, name), (
            f'{module_name}.{name} is back. It was deleted by tj-vhboky.21 with the nested `query` '
            f'field it typed. Asset-type-specific filters are designed with a reader, as flat fields.'
        )


@pytest.mark.parametrize('model', [AssetDataQuery, StockDataMarketActivityQuery], ids=_case_id)
def test_the_bar_query_is_no_longer_generic(model: type[BaseModel]):
    """The QT type parameter went with the nested field, so the query cannot be re-parametrised.

    Pinned separately from the field set because the two can come back independently: a QT with no
    ``query`` field would be dead generic machinery that invites the field back, and it would pass
    every field-set assertion in this file.

    Args:
        model: The shared bars query, or its stock binding.
    """
    assert 'query' not in model.model_fields
    assert model.__pydantic_generic_metadata__['parameters'] == ()
    with pytest.raises(TypeError):
        _ = model[int]


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


@pytest.mark.parametrize('name', sorted(DELETED_PSEUDO_MODELS))
def test_the_unvalidated_delete_contract_is_gone(name: str):
    """Audit the tj-9dqfjo deletion by absence from the package namespace.

    The predecessor of this test asserted that ``AssetDataDeleteById`` and
    ``StockDataMarketActivityDeleteById`` were NOT validating models, pinning the bug so its fix
    would be visible. tj-vhboky.2 item I applied the ruled disposition -- delete them, rather than
    give them a ``BaseModel`` base -- so the classes the assertion named no longer exist and the
    module stopped importing. That is what this replaces.

    The absence is asserted rather than assumed because a deleted class is invisible: nothing else
    in this suite fails if one comes back in a later merge, and coming back is exactly what would
    happen if someone restores a delete-by-id route by reaching for the old name.

    Args:
        name: One deleted class name, checked against every module in the package.
    """
    for module_name in sorted(EXPECTED_MODULES):
        module = importlib.import_module(module_name)
        assert not hasattr(module, name), (
            f'{module_name}.{name} is back. It was deleted by tj-vhboky.2 item I because it '
            f'inherited ABC alone and its Field() was never processed. If a delete-by-id contract '
            f'is genuinely wanted, it inherits BaseModel -- do not restore this shape.'
        )


def test_no_public_class_declares_fields_without_being_a_model():
    """Guard the DEFECT CLASS that tj-9dqfjo was one instance of, not the two deleted names.

    The property worth protecting was never "these two classes are broken". It was that this
    package can contain a class that LOOKS like a validating model -- it declares
    ``dataset_id: UUID = Field(..., description=...)`` -- and is not one, because it inherits
    ``ABC`` alone. ``Field()`` is then never processed, the annotation is never enforced, and the
    attribute is a ``FieldInfo`` object sitting on the class. Nothing raises; the contract is
    simply unenforced, which is the worst available failure mode for a cross-service schema.

    Deleting the two instances removed the instances and would have removed the only detector
    with them. ``noqa: B024`` went with them too, so ruff's own complaint about an ABC with no
    abstract method is no longer suppressed anywhere -- but ruff never caught this defect, and
    would not catch a recurrence: B024 fires on the ABC, not on the unprocessed ``Field()``.

    A ``FieldInfo`` on a non-model class is the signature, and it is cheap to look for.
    """
    offenders = {}
    for module_name in sorted(EXPECTED_MODULES):
        module = importlib.import_module(module_name)
        for name, obj in vars(module).items():
            if not (inspect.isclass(obj) and obj.__module__ == module.__name__):
                continue
            if issubclass(obj, BaseModel):
                continue
            stray = {attr for attr, value in vars(obj).items() if isinstance(value, FieldInfo)}
            if stray:
                offenders[f'{module_name}.{name}'] = sorted(stray)
    assert offenders == {}, (
        f'These classes declare pydantic Field() but do not inherit BaseModel, so the fields are '
        f'never validated: {offenders}. This is tj-9dqfjo recurring. Give the class a BaseModel '
        f'base, or drop the Field() -- do not add it to KNOWN_NON_MODELS.'
    )


def test_feed_has_one_home_and_data_store_does_not_redeclare_it():
    """Pin the import location, because a duplicate enum compares unequal at runtime only.

    Two ``Feed`` enums with identical members are different types: ``other.Feed.IEX ==
    common.Feed.IEX`` is False, a value validated against one is rejected by the other, and
    nothing about the failure says "there are two enums". The abandoned membership branch did
    declare its own in ``data/store/app/database/models/feed.py``; this asserts the value the
    schemas actually use is the shared one.
    """
    assert Feed is common.enums.data_stock.Feed
    assert Feed.__module__ == 'common.enums.data_stock'

    # THE WITNESS MOVED, and the move is the tj-rh4b7f deferral rather than a test repair. This
    # used to witness on `asset_dataset_store`, because the ENTRY body declared a feed. It no
    # longer does, so that module legitimately no longer imports Feed and witnessing there would
    # assert the opposite of the ruling. The bar create schemas are where feed lives now, so that
    # is where the shared-enum identity has to hold.
    imported = importlib.import_module('schemas.data_store.asset_data_interface')
    assert imported.Feed is Feed
    # The enum is imported into the schema module, never defined there.
    assert 'Feed' not in _public_classes(imported)

    # And the module it left declares none of its own on the way out -- a local re-declaration is
    # exactly the duplicate-enum failure above, and removing the import is what must have happened
    # rather than replacing it with a private copy.
    vacated = importlib.import_module('schemas.data_store.asset_dataset_store')
    assert not hasattr(vacated, 'Feed')


def test_feed_has_no_sentinel_for_a_tape_nobody_resolved():
    """THREE members, and NO ``UNKNOWN``. This assertion is the reverse of the one it replaces.

    IT WAS INVERTED DELIBERATELY, not repaired (tj-vhboky.1, user ruling of 2026-09-25, landed in
    32438a9). The earlier version of this test pinned FOUR members and pinned ``UNKNOWN`` as the
    entry body's default, with a docstring arguing that a forgotten feed should read as an
    unanswered question. The ruling went the other way, and the reasoning is worth keeping because
    it is what makes the two cases different:

      NOT_APPLICABLE  STAYS. It is the FINAL, CORRECT answer for a source with no tape distinction
                      at all -- IB_API, MANUAL_ENTRY. It never becomes anything else, so it is a
                      value, not a gap.
      UNKNOWN         GONE. It named a gap, and a gap in an IDENTITY column is not a value to
                      record -- it is a failure to report. Left in the vocabulary it invites use
                      as a default in exactly the place a decision was wanted, and a row written
                      with it could only be corrected by rewriting identity, which is the one
                      column set a migration cannot quietly rewrite. An adapter that cannot
                      determine the feed has FAILED and must say so.

    The narrowing direction is asserted too, because it is the consequence a caller feels:
    ``UsEquityFeed.from_superset(Feed.NOT_APPLICABLE)`` RAISES, which is the correct answer to
    "which US tape served this row" for a row that has none.
    """
    assert {member.name for member in Feed} == {'NOT_APPLICABLE', 'IEX', 'SIP'}
    assert 'UNKNOWN' not in Feed.__members__
    with pytest.raises(ValueError, match='UNKNOWN'):
        Feed('UNKNOWN')

    # PARITY WITH THE SUBSETS, which line 552 cannot give. That line pins the three names
    # absolutely, so it is the anchor; this one pins the RELATIONSHIP, and catches the drift
    # compose() exists to remove -- a subset gaining or losing a member that never reaches the
    # superset, in either direction. What neither assertion catches on its own is a Feed that
    # re-declares the same three names by hand; the two together are what make that visible,
    # because a hand-listed Feed can satisfy one only by being kept in step with the other.
    assert {member.name for member in Feed} == {member.name for member in UsEquityFeed} | {
        member.name for member in NoTapeFeed
    }
    assert UsEquityFeed.from_superset(Feed.IEX) is UsEquityFeed.IEX
    with pytest.raises(ValueError, match='NOT_APPLICABLE'):
        UsEquityFeed.from_superset(Feed.NOT_APPLICABLE)


def test_the_entry_carries_no_feed_and_the_bar_requires_one():
    """Pin BOTH halves of the tj-rh4b7f move, because either half alone is the bug it prevents.

    The store writes the dataset entry row and only then calls ingest, so at the moment the entry
    is written nothing has resolved a tape. feed is identity, so a placeholder written there and
    corrected afterwards would MUTATE identity in place -- the defect the whole per-dataset model
    exists to remove. So feed left the entry and landed on the bar, where the adapter knows it.

    Asserting only the absence would pass on a tree that dropped feed everywhere, and asserting
    only the presence would pass on a tree that declared it in both places. Both are asserted
    here, in one test, for that reason.

    REQUIRED WITH NO DEFAULT is the other half of the removed ``UNKNOWN``: with no sentinel to fall
    back to, a missing feed has to fail loudly at the boundary.

    THE READ MODEL MOVED FROM THE ABSENCE LIST TO THE PRESENCE LIST, and the move is tj-5dvgaa
    (8240133) rather than a repair of this test. This test previously asserted that ``AssetData``
    declared NO feed, citing its own docstring's "known gap"; the whole point of that bead is that
    the gap was a gap. The stored bar has a NOT NULL feed column, so a read model without the field
    could not report which tape served a row the database had already recorded one for -- an answer
    withheld, not an answer that did not exist. The bead's own reasoning is why the field had to be
    REQUIRED rather than optional on arrival: optional would report ``None`` for a column that is
    NOT NULL, a wrong answer rather than a missing one, and the removed ``UNKNOWN`` under a new
    name.

    WHAT IS STILL ABSENT, AND WHY THAT IS NOT AN OVERSIGHT: ``AssetDataUpdate``. An update
    addresses an existing row by id, so obliging a caller to restate identity it is not changing
    would invite a mismatch between the feed sent and the feed stored. The field therefore sits on
    the concrete create and read models and NOT on the shared ``_AssetIdentifier`` mixin, which is
    the structural fact this test's three lists together pin.

    THE QUERY HALF OF THE ABSENCE LIST MOVED OUT, INVERTED, to
    test_the_bar_query_carries_an_optional_feed_filter. This test used to assert that
    ``AssetDataQuery`` and ``StockDataMarketActivityQuery`` declared NO feed, reasoning that a
    required feed would make an unfiltered read impossible. That reasoning was about a REQUIRED
    feed and was sound; the conclusion it was used for -- no feed at all -- left the read unable
    to select one tape (tj-p78ng6). The user ruled on 2026-09-27 (tj-vhboky.20, 21:41 UTC, ruling
    3) to add feed as an optional FILTER, and a8218d5 (tj-vhboky.21) put it on
    ``_AssetIdentifierQuery``. The half that survives here is the one the ruling kept: feed is
    still not on ``_AssetIdentifier`` and not on the update.
    """
    for model in (StoreAssetDatasetBody, StoreAssetDatasetQuery, AssetDatasetStoreCreate, AssetDatasetStore):
        assert 'feed' not in model.model_fields, f'{model.__name__} declares a feed again'

    # The create paths, which supply the tape, and the READ models, which report it. Same three
    # assertions for both, because "required, no default, typed as the shared enum" is one contract
    # whichever direction the value is travelling.
    for model in (
        AssetDataCreate,
        BatchAssetDataCreate,
        StockDataMarketActivityCreate,
        BatchStockDataMarketActivityCreate,
        AssetData,
        StockDataMarketActivity,
    ):
        field = model.model_fields['feed']
        assert field.is_required(), f'{model.__name__}.feed acquired a default'
        assert field.default_factory is None, f'{model.__name__}.feed acquired a default factory'
        assert field.annotation is Feed

    # NOT on the shared mixin, which is what keeps a required feed off the update path. The query
    # models used to be in this loop; see the docstring for where they went and why.
    assert 'feed' not in _AssetIdentifier.model_fields
    for model in (AssetDataUpdate, StockDataMarketActivityUpdate):
        assert 'feed' not in model.model_fields, f'{model.__name__} declares a feed again'

    # The behavioural half, on a create path and on a read model: omitting feed is reported against
    # `feed` and nothing else. A field-set assertion alone would pass on a model that declared the
    # field and then never enforced it.
    with pytest.raises(ValidationError) as excinfo:
        StockDataMarketActivityCreate(**_IDENTIFIER | _DATA_FIELDS | {'data': _BAR, 'dataset_id': DATASET_ID})
    assert [error['loc'] for error in excinfo.value.errors()] == [('feed',)]

    with pytest.raises(ValidationError) as excinfo:
        StockDataMarketActivity(
            **_IDENTIFIER
            | _DATA_FIELDS
            | {'data': _BAR, 'dataset_id': DATASET_ID, 'id': 1, 'created_at': WHEN, 'updated_at': WHEN}
        )
    assert [error['loc'] for error in excinfo.value.errors()] == [('feed',)]

    # And the value survives the round trip rather than merely being accepted: a read model that
    # declared the field but dropped it on validation would satisfy every assertion above.
    #
    # ONE MEMBER ONLY, ON PURPOSE. The value-level property across EVERY Feed member -- that the
    # stored row's own tape is what the read model reports -- is pinned in
    # data/store/tests/test_bar_batch_guard.py::test_to_schema_reports_the_tape_that_served_the_row,
    # parametrised over list(Feed). A schema-side coercion of one tape into another would be caught
    # there and NOT by a schemas-only test run (tj-ibf2vo).
    built = StockDataMarketActivity(
        **_IDENTIFIER | _DATA_FIELDS | _BAR_IDENTITY | {'data': _BAR, 'id': 1, 'created_at': WHEN, 'updated_at': WHEN}
    )
    assert built.feed is Feed.IEX
    assert built.model_dump()['feed'] is Feed.IEX


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
    # `*_` because the default factory now calls `datetime.now(UTC)`: the expiry column is
    # timestamptz, and a naive local default made "when does this data die" environment-dependent
    # (tj-vhboky.2 item J). The assertions below are unchanged -- this adapts the fake clock's
    # signature, not what the test pins.
    monkeypatch.setattr(
        'schemas.data_store.asset_dataset_store.datetime', SimpleNamespace(now=lambda *_: next(instants))
    )

    first = StoreAssetDatasetBody(**_DATASET_BODY)
    second = StoreAssetDatasetBody(**_DATASET_BODY)

    assert first.expiry == datetime(2026, 1, 2, tzinfo=UTC)
    assert second.expiry == datetime(2026, 6, 2, tzinfo=UTC)
    # Cheap second line pinning the mechanism; it is not a substitute for the behaviour above.
    assert StoreAssetDatasetBody.model_fields['expiry'].default_factory is not None


def test_expiry_default_is_an_aware_utc_instant():
    """Pin tzinfo, which the fake-clock test above cannot: it supplies its own instants.

    This is the contract half of the UTC fix. The default was ``datetime.now()`` with no tzinfo,
    bound for a column the model side stores as ``timestamptz``, which makes "when does this data
    die" depend on the host's local zone. A naive default does not fail -- it is coerced, quietly,
    against whatever the database believes local is.
    """
    expiry = StoreAssetDatasetBody(**_DATASET_BODY).expiry
    assert expiry is not None
    assert expiry.tzinfo is not None
    assert expiry.utcoffset() == datetime.now(UTC).utcoffset()


# The three create-body fields bound for timestamptz columns, each with an OFFSET-LESS instant that
# is otherwise valid beside the rest of the payload (end after start, expiry between them). Naive on
# purpose: these are the values the user ruling on tj-1bl90i (2026-09-27) says must be REFUSED --
# not converted -- because Postgres would read them in the session timezone.
_NAIVE_INSTANTS: dict[str, datetime] = {
    'start': datetime(2026, 1, 1),
    'end': datetime(2026, 3, 1),
    'expiry': datetime(2026, 2, 1),
}

_WRITE_MODELS = [StoreAssetDatasetBody, AssetDatasetStoreCreate, AssetDatasetStoreUpdate]


def _write_payload(model: type[BaseModel]) -> dict[str, Any]:
    """A minimal valid payload for one of the write models, whose only datetime is an aware start.

    Args:
        model: The body, or one of the two models that inherit its datetime fields.

    Returns:
        dict[str, Any]: The payload, ready to have one field replaced.
    """
    payload = dict(_DATASET_BODY)
    if model is not StoreAssetDatasetBody:
        payload |= _DATASET_PATH
    if model is AssetDatasetStoreUpdate:
        payload |= {'id': str(DATASET_ID)}
    return payload


def _as_json(payload: dict[str, Any]) -> str:
    """Serialise a payload the way a caller would send it, datetimes as ISO-8601 text.

    Args:
        payload: A payload whose datetime values are rendered with ``isoformat()``.

    Returns:
        str: The JSON body.
    """
    return json.dumps(
        {key: value.isoformat() if isinstance(value, datetime) else value for key, value in payload.items()}
    )


@pytest.mark.parametrize('model', _WRITE_MODELS)
@pytest.mark.parametrize('field', sorted(_NAIVE_INSTANTS))
def test_a_naive_datetime_is_refused_at_its_own_field(field: str, model: type[BaseModel]):
    """REFUSE, per field, per write model: the user ruling on tj-1bl90i, 2026-09-27.

    Before a29e690 all three fields were plain ``datetime``, so a value with no tzinfo validated and
    upsert_entry bound it unchanged into a timestamptz column, where Postgres reads it in the SESSION
    timezone -- an environment-dependent instant, silently. Converting to UTC was offered and
    declined, so the only passing answer is a ValidationError.

    The error list is asserted EXACTLY -- one error, at this field, of type ``timezone_aware`` -- so
    that it cannot pass on a refusal raised for some other reason (a missing field, the model
    validator's end/update_type rule), and so that a CONVERT implementation (an after-validator
    attaching UTC) reds it rather than slipping through as "no error at start, but one somewhere".

    The two models that inherit these fields are included because they are what the store actually
    hands to upsert_entry (data/store/app/ingest/data_action_request.py builds
    AssetDatasetStoreCreate); a subclass that re-declared one of the fields as ``datetime`` would
    reopen the hole below the route while the body still refused.

    Args:
        field: The datetime field sent naive.
        model: The write model under test.
    """
    with pytest.raises(ValidationError) as excinfo:
        model(**_write_payload(model) | {field: _NAIVE_INSTANTS[field]})
    assert [(error['loc'], error['type']) for error in excinfo.value.errors()] == [((field,), 'timezone_aware')]


@pytest.mark.parametrize('field', sorted(_NAIVE_INSTANTS))
def test_an_offset_less_json_string_is_refused_at_its_own_field(field: str):
    """The wire form of the case above, which is how a real caller sends it.

    A caller never sends a Python datetime; it sends text. ``'2026-01-01T00:00:00'`` parsed from
    JSON is the naive value that actually arrives, and a string-side coercion (a before-validator
    that parsed text and attached a zone) could refuse the object form while accepting this one.

    Args:
        field: The datetime field sent as offset-less text.
    """
    text = _NAIVE_INSTANTS[field].isoformat()
    assert text == _NAIVE_INSTANTS[field].strftime('%Y-%m-%dT%H:%M:%S'), 'the fixture must carry no offset'

    with pytest.raises(ValidationError) as excinfo:
        StoreAssetDatasetBody.model_validate_json(_as_json(_write_payload(StoreAssetDatasetBody) | {field: text}))
    assert [(error['loc'], error['type']) for error in excinfo.value.errors()] == [((field,), 'timezone_aware')]


@pytest.mark.parametrize(
    ('suffix', 'zone'),
    [('Z', UTC), ('+00:00', UTC), ('-05:00', timezone(timedelta(hours=-5)))],
    ids=['zulu', 'utc-offset', 'nonzero-offset'],
)
@pytest.mark.parametrize('field', sorted(_NAIVE_INSTANTS))
def test_an_offset_bearing_json_string_is_accepted_as_the_instant_it_names(field: str, suffix: str, zone: timezone):
    """The success half: a refusal test alone is satisfied by a field that refuses EVERYTHING.

    'Z' is what our own ingest path and test_store_dataset_entry_route.py's REQUEST_BODY send, so a
    guard that refused it would break the only caller. The non-zero offset is asserted as the INSTANT
    it names, which is what separates honouring the offset from dropping it: were ``-05:00`` read as
    UTC, the value would compare five hours early.

    Args:
        field: The datetime field sent with an offset.
        suffix: The offset designator appended to the ISO text.
        zone: The zone that designator names.
    """
    text = _NAIVE_INSTANTS[field].isoformat() + suffix

    body = StoreAssetDatasetBody.model_validate_json(_as_json(_write_payload(StoreAssetDatasetBody) | {field: text}))

    value = getattr(body, field)
    assert value.tzinfo is not None
    assert value == _NAIVE_INSTANTS[field].replace(tzinfo=zone)


def test_the_default_expiry_survives_a_round_trip_through_its_own_annotation():
    """The default expiry is still aware, measured the way the new annotation measures it.

    A default_factory's output is NOT validated (pydantic's validate_default is off), so the
    AwareDatetime annotation never inspects the default: a factory that went back to naive
    ``datetime.now()`` would construct without complaint. test_expiry_default_is_an_aware_utc_instant
    checks tzinfo directly; this checks the consequence that matters under the ruling -- a body the
    store itself builds with the default, sent on as JSON (as data_action_request.py's downstream
    requests are), must be one the same contract accepts rather than 422s.
    """
    body = StoreAssetDatasetBody(**_DATASET_BODY)
    assert 'expiry' not in body.model_fields_set, 'the default was not the one exercised'

    again = StoreAssetDatasetBody.model_validate_json(body.model_dump_json())

    assert again.expiry.tzinfo is not None
    assert again.expiry == body.expiry


def test_owner_is_required_with_no_default():
    """Pin the absence of a default, which the missing-field test alone does not distinguish.

    A field can be reported missing today and grow a default tomorrow without any construct or
    reject case noticing, because a payload that supplies every field passes either way. The
    default is the decision: a default principal would put every strategy that forgot to name
    itself onto ONE shared dataset -- the problem owner-scoped writes exist to prevent, wearing a
    different hat. ``owner`` is identity (tj-vhboky.1 section 5), so there is no safe value to
    invent on a caller's behalf.

    Note what the field does and does not buy, because the code and the docs are required to say
    so rather than softening it later: the single instance secret authenticates THE DEPLOYMENT,
    not the caller. With one key there is effectively one principal, so "only the owner may edit"
    is enforced against MISTAKES, not against anyone holding the key.
    """
    field = StoreAssetDatasetBody.model_fields['owner']
    assert field.is_required()
    assert field.default_factory is None
    with pytest.raises(ValidationError) as excinfo:
        StoreAssetDatasetBody(**{key: value for key, value in _DATASET_BODY.items() if key != 'owner'})
    assert [error['loc'] for error in excinfo.value.errors()] == [('owner',)]


@pytest.mark.parametrize('model', [StoreAssetDatasetBody, AssetDatasetStoreCreate, AssetDatasetStoreUpdate])
def test_owner_rejects_an_explicit_null(model: type[BaseModel]):
    """The explicit-null half of "owner never reaches the database as NULL" (tj-vhboky.11 item 4).

    test_owner_is_required_with_no_default pins that OMITTING owner fails. It does not pin this:
    ``owner: str | None`` with no default is still required, so a missing owner still fails -- but
    an explicit ``"owner": null`` is then accepted, and upsert_entry's ``exclude_none=True`` drops
    the None from the insert. The column's server_default then writes 'unassigned', and every
    caller that sent null lands on ONE shared dataset -- the exact problem owner-scoped writes
    exist to prevent. Each write model is checked, since the crud layer receives the Create and
    Update models rather than the body.

    Args:
        model: A write model carrying the owner field.
    """
    payload = _DATASET_BODY | {'owner': None}
    if model is not StoreAssetDatasetBody:
        payload |= {'asset_type': 'stock', 'data_type': 'market-activity', 'asset_symbol': 'AAPL'}
    if model is AssetDatasetStoreUpdate:
        payload |= {'id': DATASET_ID}
    with pytest.raises(ValidationError) as excinfo:
        model(**payload)
    assert [error['loc'] for error in excinfo.value.errors()] == [('owner',)]


@pytest.mark.parametrize('field', ['expiry', 'expiry_type', 'update_type'])
def test_a_policy_field_rejects_an_explicit_null_rather_than_defaulting_it(field: str):
    """One assertion, TWO DIFFERENT HAZARDS, and the docstring has to say which field carries which.

    All three fields have defaults, so OMITTING them is fine and the minimal payload leaves them
    out. Sending ``null`` explicitly is a different request and must be rejected in every case.
    What a null would COST differs by field, and naming only one reason for three parameters would
    be a test whose stated purpose no longer matches what it checks:

    ``expiry_type`` and ``update_type`` -- THE CONTRACT HALF OF THE NULL-IN-A-UNIQUE-KEY HAZARD
    (tj-vhboky.1 section 2 pins the other). Both land in a UNIQUE constraint, and Postgres treats
    NULL as distinct from NULL in a unique index. A NULL written into a key column means the ON
    CONFLICT never fires against that row, and "an exact repeat returns the existing id" silently
    becomes "an exact repeat creates a second row" -- the duplication the whole identity model
    exists to make impossible.

    ``expiry`` -- A DIFFERENT HAZARD AT A DIFFERENT SEAM, and deliberately not the one above:
    expiry is NOT part of the identity key (expiry_TYPE is; the value itself is a policy the
    request asks for, not a thing that makes one dataset different from another), so nothing about
    the unique index applies to it. The cost is downstream. ``BaseGetDatasetRequest.expiry``
    (schemas/data_ingest/get_dataset_request.py) is a REQUIRED, non-optional datetime, and
    data/store/app/ingest/data_action_request.py builds that request by splatting this model's
    ``model_dump()``. So an explicit ``"expiry": null`` passed body validation, carried None
    through the splat, and blew up as a ValidationError on GetDatasetRequest -- a 500 on
    caller-shaped input, which is the one class of failure a declared request schema must never
    produce. It now 422s at the edge, naming the field.

    THE REGRESSION TO CATCH ON expiry is a widening BACK to ``datetime | None``, and it looks
    reasonable from two directions: StoreDatasetEntry.expiry is nullable=True and
    AssetDatasetStore.expiry is ``datetime | None``. Both of those are correct and must STAY -- the
    column is nullable for rows predating the default, and the READ model must be able to represent
    them. It is the WRITE body that must not accept null. The other plausible wrong fix is making
    the field required, which would break every caller that legitimately omits it; the positives
    are pinned separately by test_expiry_default_is_computed_per_instance and
    test_expiry_default_is_an_aware_utc_instant so that this test cannot invite it.

    WHAT ACTUALLY PRODUCES THE REJECTION DIFFERS BY FIELD, and this paragraph used to say the
    annotation did it in all three cases. It does not, and the difference decides what this test can
    be read as pinning -- measured by mutating each guard alone, not inferred from the annotations.

    ``expiry``: the non-Optional annotation IS the only mechanism. Nothing coerces this field before
    the annotation is consulted, so re-widening it to ``datetime | None`` reds this case -- and reds
    only this case, with the rest of the suite green. That is the regression described above.

    ``expiry_type`` and ``update_type``: DOUBLY GUARDED, so this case pins neither guard on its own
    and must not be read as pinning the annotation for them. Each has a ``mode='before'`` field
    validator (``ExpiryType.validate`` / ``UpdateType.validate``) that raises on a null before the
    annotation is ever reached -- ``NamedIntEnum.validate`` falls through its str/int/enum branches to
    ``raise ValueError(f'Invalid type for enum_field: {type(value)}')`` (common/enums/
    pydantic_enums.py). Measured: widening either annotation to ``| None`` is green across the whole
    suite, and making the before-validator tolerate a null with the annotations untouched is green
    too; the case reds only when BOTH are relaxed for the same field. A ``| None`` annotation with a
    non-None default and no before-validator would accept the null and carry it, which is what
    ``expiry`` shows and what these two are protected from twice over.

    Args:
        field: The policy field being sent as null.
    """
    assert not StoreAssetDatasetBody.model_fields[field].is_required()
    with pytest.raises(ValidationError) as excinfo:
        StoreAssetDatasetBody(**_DATASET_BODY | {field: None})
    assert [error['loc'] for error in excinfo.value.errors()] == [(field,)]


@pytest.mark.parametrize('dead_field', ['split_factor', 'dividends_factor'])
def test_the_bar_no_longer_declares_a_corporate_action_factor(dead_field: str):
    """Assert the absence by the declared field set, and pin what a stale caller gets: REJECTION.

    THE SECOND ASSERTION IS THE REVERSE OF WHAT IT WAS, and the reversal is the ruling, not a
    repair. tj-vhboky.13 required the choice between "ignored" and "rejected" to be NAMED rather
    than inherited from a default, and the earlier version of this test pinned `ignored` while
    recording that the question was open -- its own docstring said the assertion would change with
    the ruling deliberately rather than drift. The ruling landed (tj-vhboky.1, 2026-09-25,
    implemented in 32438a9 as schemas/inbound_contract.py) and it chose REJECTED.

    WHY, in the user's terms: "an external caller sending junk is sloppy, but an internal service
    still sending a field we deleted means we removed something and nothing told us the sender had
    not noticed. That is a bug shipped to ourselves." This model was the live instance -- the
    Alpaca adapter went on sending both factors on every bar after the contract dropped them, and
    every call succeeded because extra='ignore' swallowed them. Under the strict base that call
    fails at the call site, which is what forced the adapter fix in 7ab712b.

    Args:
        dead_field: A factor field that must no longer be declared.
    """
    assert dead_field not in StockDataMarketActivityData.model_fields
    assert StockDataMarketActivityData(**_BAR).model_dump() == _BAR

    with pytest.raises(ValidationError) as excinfo:
        StockDataMarketActivityData(**_BAR | {dead_field: 1.0})
    errors = excinfo.value.errors()
    assert [error['loc'] for error in errors] == [(dead_field,)]
    # The error TYPE is asserted, not just the location: a rejection for some unrelated reason --
    # a coercion failure, say -- would satisfy the location alone and would not be this contract.
    assert [error['type'] for error in errors] == ['extra_forbidden']


def test_no_level_of_the_data_interface_carries_a_per_item_expiry():
    """Assert the absence by CONSTRUCTING the models, not by reading the module source.

    A field that comes back in a later merge is invisible to a source-reading check and to every
    other test here: a payload that omits ``expiry`` constructs just as happily against a model
    that declares it optional. Constructing and inspecting the field set is what fails on the
    reappearance.

    ``append_data`` is included because its signature is the ripple that reaches ingest: it lost
    its fourth parameter, and a caller still passing four positional arguments gets a TypeError
    rather than a validation error.
    """
    for model in (_AssetDataType, AssetDataCreate, AssetDataUpdate, AssetData, StockDataMarketActivity):
        assert 'expiry' not in model.model_fields, f'{model.__name__} carries a per-item expiry again'

    # `_BAR_IDENTITY` rather than a bare `dataset_id`: the read model requires a feed as of
    # tj-5dvgaa, so this construction has to supply one. What the test asserts is unchanged -- a
    # payload that omits expiry constructs either way, and the field set is still the thing under
    # test.
    built = AssetData(
        **_IDENTIFIER
        | _DATA_FIELDS
        | _BAR_IDENTITY
        | {'data': _OPAQUE_DATA, 'id': 1}
        | {'created_at': WHEN, 'updated_at': WHEN}
    )
    assert 'expiry' not in built.model_dump()

    assert list(inspect.signature(BatchAssetDataCreate.append_data).parameters) == [
        'self',
        'data_type',
        'data',
        'timestamp',
    ]


def test_asset_data_requires_its_dataset_id():
    """A bar belongs to exactly one dataset and dies with it, so the link is not optional.

    Made explicit because ``dataset_id`` is the field whose last-write-wins defect motivated the
    whole per-dataset model: an optional or defaulted link is how a bar ends up claiming a
    dataset it was not fetched for.
    """
    assert AssetData.model_fields['dataset_id'].is_required()
    # The payload carries `feed` -- required on the read model as of tj-5dvgaa -- so that the sole
    # reported error is the one this test is about. Widening the assertion to accept a second
    # missing field instead would have stopped pinning "dataset_id, and dataset_id alone".
    with pytest.raises(ValidationError) as excinfo:
        AssetData(
            **_IDENTIFIER
            | _DATA_FIELDS
            | {'data': _OPAQUE_DATA, 'feed': Feed.IEX, 'id': 1, 'created_at': WHEN, 'updated_at': WHEN}
        )
    assert [error['loc'] for error in excinfo.value.errors()] == [('dataset_id',)]


def test_a_flat_row_cannot_validate_into_the_bar_read_schema():
    """FLAG C, pinned at the level where it is true: the nested submodel, not ``from_attributes``.

    ``read_market_activity_data`` -- the dataset-scoped filtering read the whole duplication
    argument turns on, and the read the one endpoint is to be wired to -- ends in
    ``StockDataMarketActivity.model_validate(obj)`` on a SQLAlchemy ORM instance. It has never
    returned a row, and it has no caller: the GET route calls the unfiltered variant, which uses
    ``to_schema()``.

    THAT PARTICULAR CALL SITE IS NOW FIXED -- ``read_market_activity_data`` returns
    ``[obj.to_schema() for obj in ...]`` as of e907bc6 -- and this test is deliberately NOT retired
    with it. The LIVE instance of the same defect shape is ``get_entry_by_id``
    (``data/store/app/database/crud/stock/store_dataset_entry.py``), filed as tj-b2uqfl, which
    passes a ``Row`` to ``model_validate``. This test pins the schema-side property that makes every
    such call fail, so it keeps standing guard over the shape rather than over one function.

    TWO things stop it, and only one of them is the obvious one. Missing ``from_attributes`` is
    the first. The second survives fixing the first: ``data`` is a NESTED submodel and a flat row
    has no ``data`` attribute at all, so enabling ``from_attributes`` moves the failure rather
    than removing it. This test constructs a flat, ORM-shaped object with every bar column on it
    and shows that ``data`` is still reported missing WITH ``from_attributes`` in force.

    THE FLAT ROW GAINED A ``feed`` ATTRIBUTE, RATHER THAN THE ASSERTION GAINING A SECOND EXPECTED
    ERROR (tj-5dvgaa). ``feed`` is a real NOT NULL column on ``BaseMarketActivity``, so a faithful
    ORM-shaped stand-in has to carry one now that the read model declares it -- this row is meant to
    be "every bar column on it". Relaxing the assertion to ``[('data',), ('feed',)]`` instead would
    have been the wrong repair twice over: it would pin an omission from this fixture as though it
    were part of the contract, and it would blunt what this test exists to say. The SINGLE expected
    error is load-bearing -- it is what makes ``data`` the surviving blocker rather than one
    complaint among several. If someone later adds ``from_attributes`` and a ``data`` property and
    declares the read fixed, a two-error assertion would still be red, for a feed on a row that
    never had one, and the real signal would be lost inside it.

    WHY THIS ASSERTION DOES NOT NEED INVERTING WHEN THE BUG IS FIXED, unlike the delete-contract
    test it sits beside: it pins a property of the SCHEMA -- a nested submodel is not a flat row
    -- which stays true afterwards. The fix belongs in the store's crud return path, which must
    build the nested shape via ``to_schema()``. If someone instead "fixes" it by adding
    ``from_attributes`` to the schema and declares the read working, this test is what says the
    read still cannot return a row.
    """
    flat_row = SimpleNamespace(
        id=1,
        dataset_id=DATASET_ID,
        asset_symbol='VFV',
        source='ALPACA',
        granularity='1day',
        feed=Feed.IEX,
        timestamp=WHEN,
        created_at=WHEN,
        updated_at=WHEN,
        **_BAR,
    )

    # `from_attributes=True` passed per call rather than configured on a subclass: it is the
    # stronger form of the assertion, since it grants the schema the very config the obvious fix
    # would add, and the validation still fails.
    with pytest.raises(ValidationError) as excinfo:
        StockDataMarketActivity.model_validate(flat_row, from_attributes=True)
    assert [error['loc'] for error in excinfo.value.errors()] == [('data',)]


# THE BARS QUERY CONTRACT, PART 1 (tj-vhboky.21, a8218d5), under the user rulings of 2026-09-27 on
# tj-vhboky.20 and decision tj-vhboky.25 with its addenda. Both query classes are checked: the shared
# AssetDataQuery that F1b's model_validator sits on (ebb2439), and the stock binding the route and
# read_market_activity_data actually take. A subclass that re-declared a field would reopen a hole
# below the shared model while the shared model still looked right.
_BAR_QUERIES = [AssetDataQuery, StockDataMarketActivityQuery]

# Every query below names a dataset_id so that it stays NON-EMPTY: F1b (tj-vhboky.26) refuses an
# empty query, and a query without a selector would report that refusal beside the field under test.
_SCOPED_QUERY: dict[str, Any] = {'dataset_id': DATASET_ID}

_NAIVE_BOUNDS: dict[str, datetime] = {'start': datetime(2026, 1, 1), 'end': datetime(2026, 3, 1)}


@pytest.mark.parametrize('model', _BAR_QUERIES, ids=_case_id)
def test_the_bar_query_carries_an_optional_feed_filter(model: type[BaseModel]):
    """INVERTS the query half of test_the_entry_carries_no_feed_and_the_bar_requires_one.

    SUPERSEDED DESIGN, kept here so the file carries the history: that test asserted
    ``'feed' not in model.model_fields`` for both query classes, because every query field is
    optional (tj-vhboky.1 section 8) and a REQUIRED feed would have made an unfiltered read
    impossible. The premise holds; the conclusion drawn from it -- no feed at all -- left the
    filtering read unable to select one tape (tj-p78ng6). The user ruled on 2026-09-27
    (tj-vhboky.20, 21:41 UTC, ruling 3: "FILTERS: add source and feed"), and a8218d5
    (tj-vhboky.21) added ``feed: Feed | None = None`` to ``_AssetIdentifierQuery``. The design
    change is recorded in the ruling and the task, not only in the commit that rewrites this.

    STRENGTHENED, not merely flipped. The old assertion was one absence. This one pins: WHERE the
    field lives (the query mixin, and still not ``_AssetIdentifier``, whose required feed would
    burden the update path); that it is OPTIONAL with a None default, which is the half of the old
    reasoning that survives; that it is the SHARED enum, not a local copy that would compare
    unequal; that a value is carried through, in both enum and wire (string) form, since the route
    will bind it with Query(); that omitting it leaves it unset rather than defaulting a tape; and
    that a value outside the enum is refused AT ``feed`` rather than read as "no filter".

    Args:
        model: The shared bars query, or its stock binding.
    """
    assert 'feed' in _AssetIdentifierQuery.model_fields
    assert 'feed' not in _AssetIdentifier.model_fields

    field = model.model_fields['feed']
    assert not field.is_required(), (
        f'{model.__name__}.feed became required; an unfiltered-by-tape read needs it optional'
    )
    assert field.default is None
    assert field.annotation == Feed | None

    omitted = model(**_SCOPED_QUERY)
    assert omitted.feed is None
    assert 'feed' not in omitted.model_fields_set

    assert model(**_SCOPED_QUERY | {'feed': Feed.SIP}).feed is Feed.SIP
    assert model(**_SCOPED_QUERY | {'feed': 'SIP'}).feed is Feed.SIP

    with pytest.raises(ValidationError) as excinfo:
        model(**_SCOPED_QUERY | {'feed': 'NOT_A_TAPE'})
    assert [error['loc'] for error in excinfo.value.errors()] == [('feed',)]


@pytest.mark.parametrize('form', ['object', 'text'])
@pytest.mark.parametrize('field', sorted(_NAIVE_BOUNDS))
@pytest.mark.parametrize('model', _BAR_QUERIES, ids=_case_id)
def test_a_naive_query_bound_is_refused_at_its_own_field(model: type[BaseModel], field: str, form: str):
    """REFUSE, per bound, on the read side too: the user ruling D2 = (A), tj-vhboky.20, 2026-09-27.

    Before a8218d5 ``start`` and ``end`` were plain ``datetime``, so a value with no offset
    validated and was compared against the timestamptz ``timestamp`` column in the SESSION
    timezone -- the same query returning different bars on differently configured hosts. The
    create body got the same refusal in a29e690 (tj-1bl90i); this extends it to the filtering
    read. Converting to UTC was offered and declined on tj-1bl90i, so only a ValidationError passes.

    The error list is asserted EXACTLY -- one error, at this field, of type ``timezone_aware`` -- so
    a refusal for some other reason cannot satisfy it, and a CONVERT implementation (an
    after-validator attaching UTC) reds it rather than slipping through. The ``text`` form is what
    a query string actually delivers once the route binds the model with Query(); a before-validator
    that parsed text and attached a zone could refuse the object and accept the string.

    Args:
        model: The shared bars query, or its stock binding.
        field: The bound sent without an offset.
        form: A Python datetime, or its offset-less ISO text.
    """
    naive = _NAIVE_BOUNDS[field]
    value = naive if form == 'object' else naive.isoformat()
    if form == 'text':
        assert value == naive.strftime('%Y-%m-%dT%H:%M:%S'), 'the fixture must carry no offset'

    with pytest.raises(ValidationError) as excinfo:
        model(**_SCOPED_QUERY | {field: value})
    assert [(error['loc'], error['type']) for error in excinfo.value.errors()] == [((field,), 'timezone_aware')]


@pytest.mark.parametrize(
    ('suffix', 'zone'), [('Z', UTC), ('-05:00', timezone(timedelta(hours=-5)))], ids=['zulu', 'nonzero-offset']
)
@pytest.mark.parametrize('field', sorted(_NAIVE_BOUNDS))
@pytest.mark.parametrize('model', _BAR_QUERIES, ids=_case_id)
def test_an_offset_bearing_query_bound_is_accepted_as_the_instant_it_names(
    model: type[BaseModel], field: str, suffix: str, zone: timezone
):
    """The success half: the refusal above is also satisfied by a bound that refuses EVERYTHING.

    The non-zero offset is compared as the INSTANT it names, which separates honouring the offset
    from dropping it: read as UTC, ``-05:00`` would filter five hours early.

    Args:
        model: The shared bars query, or its stock binding.
        field: The bound sent with an offset.
        suffix: The offset designator appended to the ISO text.
        zone: The zone that designator names.
    """
    built = model(**_SCOPED_QUERY | {field: _NAIVE_BOUNDS[field].isoformat() + suffix})

    value = getattr(built, field)
    assert value.tzinfo is not None
    assert value == _NAIVE_BOUNDS[field].replace(tzinfo=zone)


@pytest.mark.parametrize(('dead_field', 'value'), [('expiry', WHEN), ('query', {})], ids=['expiry', 'query'])
@pytest.mark.parametrize('model', _BAR_QUERIES, ids=_case_id)
def test_a_removed_query_field_is_refused_rather_than_ignored(model: type[BaseModel], dead_field: str, value: Any):
    """``expiry`` and ``query`` left the bars query in a8218d5, and a sender of either gets a 422.

    Why each went (user rulings of 2026-09-27 on tj-vhboky.20, 21:41 UTC; tj-vhboky.25 item 2):
    ``expiry`` because a bar carries no expiry (tj-vhboky Ruling 1), so a bar-expiry filter means
    nothing and nothing read it (tj-wdjpmq); ``query`` because a nested model cannot be an HTTP
    query parameter, nothing read it, and the user judged it "dangerous". A contract that accepted
    either and ignored it would be the "looks validating, is not" shape.

    Absence from the field set alone is not the contract: a model with ``extra='ignore'`` would lack
    the field and swallow it silently. So the error is asserted exactly, at the dead field, of type
    ``extra_forbidden`` -- the interface change the builder named in its handback.

    Args:
        model: The shared bars query, or its stock binding.
        dead_field: A field the query no longer declares.
        value: An otherwise-plausible value for it.
    """
    assert dead_field not in model.model_fields

    with pytest.raises(ValidationError) as excinfo:
        model(**_SCOPED_QUERY | {dead_field: value})
    errors = excinfo.value.errors()
    assert [error['loc'] for error in errors] == [(dead_field,)]
    assert [error['type'] for error in errors] == ['extra_forbidden']


# Queries that name NEITHER selector. The last one filters on every other column there is and is
# still refused: narrowed by source, tape, granularity and a time range, it is every symbol in that
# window, which is the unbounded read the ruling forbids -- the rule is about the two selectors, not
# about the query being literally empty.
_SELECTORLESS_QUERIES: dict[str, dict[str, Any]] = {
    'empty': {},
    'explicit-none': {'dataset_id': None, 'asset_symbol': None},
    'every-other-filter': {
        'source': 'ALPACA',
        'feed': 'SIP',
        'granularity': '1day',
        'start': '2026-01-01T00:00:00Z',
        'end': '2026-03-01T00:00:00Z',
    },
}

_SELECTORS: dict[str, dict[str, Any]] = {
    'dataset_id': {'dataset_id': DATASET_ID},
    'asset_symbol': {'asset_symbol': 'AAPL'},
    'both': {'dataset_id': DATASET_ID, 'asset_symbol': 'AAPL'},
}


@pytest.mark.parametrize('payload', _SELECTORLESS_QUERIES.values(), ids=_SELECTORLESS_QUERIES.keys())
@pytest.mark.parametrize('model', _BAR_QUERIES, ids=_case_id)
def test_an_empty_bar_query_is_refused(model: type[BaseModel], payload: dict[str, Any]):
    """A bars query naming neither dataset_id nor asset_symbol cannot be constructed (F1b, ebb2439).

    RENAMED FROM test_an_empty_bar_query_still_constructs_until_f1b, and INVERTED, as that test's
    docstring directed (tj-8fxxfb iii).

    SUPERSEDED DESIGN, kept so the file carries the history. At a8218d5 every field of the bars query
    was individually optional with a None default and an empty query constructed. That was pinned
    deliberately, as all fields None and none set: the route's placeholder
    (routers/data_store/internal_asset_data.py, a bare ``StockDataMarketActivityQuery()``) built
    exactly that object on every GET, and a refusal landing before F2 removed the line would have
    turned every GET into a 500 rather than a 422 (tj-vhboky.25, architect addendum at dccd2c5).
    Behind it stood tj-vhboky.1 section 8: the query model must be able to represent "no filter".

    THE DESIGN NOW: the user ruled on 2026-09-27 (tj-vhboky.20, 21:21 UTC D1 = (A); 21:41 UTC ruling
    5) that an unbounded read -- every symbol, all time -- must be IMPOSSIBLE TO CONSTRUCT anywhere,
    and rejected section 8 as a reason to permit one. The refusal sits on the shared AssetDataQuery
    as a model_validator, "not in the route and not a route-only subclass", which is why both
    classes are checked: the stock binding must inherit it, not shadow it. The per-field half of
    section 8 stands and is pinned by test_a_bar_query_naming_one_selector_constructs.

    STRENGTHENED, not merely flipped. The old test had one payload; this one has three, and the third
    carries every OTHER filter, so a rule keyed on "the query is empty" rather than on the two
    selectors reds it. The refusal is asserted EXACTLY -- one error, model-level (loc ``()``), type
    ``value_error`` -- so a refusal for any other reason cannot satisfy it, and its message must
    name both selectors, which the task required so a caller learns how to fix the request. Over
    HTTP the same refusal is a 422 at loc ['query']; that is F3's subject (tj-vhboky.23), not this
    file's.

    Args:
        model: The shared bars query, or its stock binding.
        payload: A query naming no selector.
    """
    with pytest.raises(ValidationError) as excinfo:
        model(**payload)
    errors = excinfo.value.errors()
    assert [(error['loc'], error['type']) for error in errors] == [((), 'value_error')]
    assert 'dataset_id' in errors[0]['msg']
    assert 'asset_symbol' in errors[0]['msg']


@pytest.mark.parametrize('selector', _SELECTORS.values(), ids=_SELECTORS.keys())
@pytest.mark.parametrize('model', _BAR_QUERIES, ids=_case_id)
def test_a_bar_query_naming_one_selector_constructs(model: type[BaseModel], selector: dict[str, Any]):
    """The positive half of the refusal above: EITHER selector alone is enough, and so are both.

    A rule that demanded dataset_id (or demanded both) would pass every refusal case and break the
    symbol-scoped read the ruling left open. The rest of the query is asserted as ALL None and NOT
    SET, carrying forward the strengthening the superseded empty-query test made: a default quietly
    narrowing the read (a default granularity, say) is caught here too.

    Args:
        model: The shared bars query, or its stock binding.
        selector: dataset_id alone, asset_symbol alone, or both.
    """
    built = model(**selector)
    assert built.model_fields_set == set(selector)
    assert built.model_dump() == dict.fromkeys(model.model_fields) | selector


@pytest.mark.parametrize(
    ('payload', 'field', 'error_type'),
    [({'source': 'NOT_A_BROKER'}, 'source', 'enum'), ({'start': '2026-01-01T00:00:00'}, 'start', 'timezone_aware')],
    ids=['bad-enum', 'naive-bound'],
)
@pytest.mark.parametrize('model', _BAR_QUERIES, ids=_case_id)
def test_a_field_error_on_an_empty_bar_query_is_not_masked_by_the_refusal(
    model: type[BaseModel], payload: dict[str, Any], field: str, error_type: str
):
    """A bad field on a selector-less query is reported at that field, and ONLY there.

    CARRIES THE ASSERTION the bars query's two CONSTRAINT_CASES entries made before F1b moved them
    out: a ``source`` outside the enum, on a query with nothing else set, was reported at ``source``
    and nowhere else. It still is, because the refusal is ``mode='after'`` and Pydantic runs an
    after-validator only once every field has validated. A ``mode='before'`` or ``'wrap'`` refusal
    would run first and report the empty query instead, hiding the field the caller got wrong --
    the task names this as the reason for ``mode='after'``.

    Strengthened with a naive bound beside the bad enum: two kinds of field error, since a refusal
    that happened to defer to enum errors alone would pass the first.

    Args:
        model: The shared bars query, or its stock binding.
        payload: One invalid field and no selector.
        field: The field the error must be reported against.
        error_type: The Pydantic error type for that field.
    """
    with pytest.raises(ValidationError) as excinfo:
        model(**payload)
    assert [(error['loc'], error['type']) for error in excinfo.value.errors()] == [((field,), error_type)]


# A BLANK asset_symbol NAMES NO SYMBOL (F1c, tj-vhboky.28, 1331ef5). The user ruled on 2026-09-28
# (00:23 UTC): a blank symbol gets "the same error as the empty search", and the architect confirmed
# at 00:24 UTC that this means the same MODEL-LEVEL error (loc (), type value_error) and that it
# holds EVEN WHEN dataset_id IS GIVEN. The bead body's field-level loc ('asset_symbol',) is
# superseded. Each blank is a str that is not None, so the pre-F1c "is None" selector rule let it
# through as a symbol filter that matches no rows: a silent empty 200 for a malformed request.
_BLANK_SYMBOLS: dict[str, str] = {'empty': '', 'spaces': '  ', 'tab': '\t'}

_WITH_AND_WITHOUT_DATASET: dict[str, dict[str, Any]] = {'symbol-only': {}, 'with-dataset_id': _SCOPED_QUERY}


@pytest.mark.parametrize('scope', _WITH_AND_WITHOUT_DATASET.values(), ids=_WITH_AND_WITHOUT_DATASET.keys())
@pytest.mark.parametrize('blank', _BLANK_SYMBOLS.values(), ids=_BLANK_SYMBOLS.keys())
@pytest.mark.parametrize('model', _BAR_QUERIES, ids=_case_id)
def test_a_blank_symbol_is_refused_with_the_empty_query_error(
    model: type[BaseModel], blank: str, scope: dict[str, Any]
):
    """A blank asset_symbol is refused at the MODEL, like a query naming nothing, with or without dataset_id.

    The error list is asserted EXACTLY -- one error, loc ``()``, type ``value_error`` -- because the
    loc is the ruling: a field-level check (a raise in the ``asset_symbol`` field validator, or
    ``Field(min_length=1)``) would report ``('asset_symbol',)``, the superseded design. The
    with-dataset_id half is the case the architect named: treating a blank as absent would drop the
    symbol filter and return the whole dataset, and keeping it would return the silent empty read.

    The message must name ``asset_symbol`` and say it is blank, so the caller learns which value is
    wrong. It must NOT be the no-selector message ("an unbounded read is refused"): with dataset_id
    present that would be false (architect item 3, 00:24 UTC). The no-selector message itself is
    pinned by test_an_empty_bar_query_is_refused and is unchanged.

    Args:
        model: The shared bars query, or its stock binding.
        blank: An empty or whitespace-only symbol.
        scope: No other selector, or a dataset_id.
    """
    with pytest.raises(ValidationError) as excinfo:
        model(**scope | {'asset_symbol': blank})
    errors = excinfo.value.errors()
    assert [(error['loc'], error['type']) for error in errors] == [((), 'value_error')]
    assert 'asset_symbol' in errors[0]['msg']
    assert 'blank' in errors[0]['msg']
    assert 'unbounded' not in errors[0]['msg']


@pytest.mark.parametrize('scope', _WITH_AND_WITHOUT_DATASET.values(), ids=_WITH_AND_WITHOUT_DATASET.keys())
@pytest.mark.parametrize('model', _BAR_QUERIES, ids=_case_id)
def test_a_padded_symbol_is_upper_cased_and_not_trimmed(model: type[BaseModel], scope: dict[str, Any]):
    """A padded but NON-blank symbol constructs and normalises exactly as before 1331ef5.

    The blank refusal tests ``strip()`` but must not trim: the ruling changed what a BLANK symbol
    does and nothing else, and trimming would silently rewrite what a caller sent (the bead body
    already named ' AAPL ' normalisation as a contract choice to ask about, not to make). So the
    value is upper-cased, with its padding intact, whether or not dataset_id is given.

    Args:
        model: The shared bars query, or its stock binding.
        scope: No other selector, or a dataset_id.
    """
    built = model(**scope | {'asset_symbol': ' aapl '})
    assert built.asset_symbol == ' AAPL '


@pytest.mark.parametrize(
    'payload',
    [{'dataset_id': DATASET_ID}, {'dataset_id': DATASET_ID, 'asset_symbol': None}],
    ids=['symbol-omitted', 'symbol-explicit-none'],
)
@pytest.mark.parametrize('model', _BAR_QUERIES, ids=_case_id)
def test_a_dataset_id_query_without_a_symbol_is_unaffected_by_the_blank_rule(
    model: type[BaseModel], payload: dict[str, Any]
):
    """None is "not given", not blank: a dataset_id-only query still constructs, symbol None.

    The explicit-None case separates the blank check from one written as ``not (symbol or '').strip()``,
    which would treat None as blank and refuse a valid dataset read.

    Args:
        model: The shared bars query, or its stock binding.
        payload: dataset_id with the symbol omitted or explicitly None.
    """
    built = model(**payload)
    assert built.asset_symbol is None
    assert built.dataset_id == DATASET_ID


# ---------------------------------------------------------------------------------------------
# The dataset search refuses naive bounds too (F4, tj-vhboky.24)
# ---------------------------------------------------------------------------------------------

# StoreAssetDatasetQuery's four time filters, each with an OFFSET-LESS instant. The search filters
# GET /store/{asset_type}/{data_type}/{asset_symbol} against timestamptz columns, where a naive
# bound is read in the SESSION timezone -- the tj-1bl90i defect on the read side. User ruling
# D2 = (A) on tj-vhboky.20 (2026-09-27, 21:21 UTC) says REFUSE, not convert; fa1d7ee applied it.
# The model is all-optional, so no selector is needed beside the field under test: {} is valid.
_NAIVE_SEARCH_BOUNDS: dict[str, datetime] = {
    'start': datetime(2026, 1, 1),
    'end': datetime(2026, 3, 1),
    'created_at': datetime(2026, 2, 1, 9, 30),
    'updated_at': datetime(2026, 2, 2, 16, 45),
}


@pytest.mark.parametrize('form', ['object', 'text'])
@pytest.mark.parametrize('field', sorted(_NAIVE_SEARCH_BOUNDS))
def test_a_naive_dataset_search_bound_is_refused_at_its_own_field(field: str, form: str):
    """REFUSE, per field, on the dataset search: user ruling D2 = (A), tj-vhboky.20, 2026-09-27.

    Before fa1d7ee all four were plain ``datetime | None``, so an offset-less value validated and
    search_entries compared it against a timestamptz column in the Postgres SESSION timezone -- the
    same search returning different datasets on differently configured hosts.

    The error list is asserted EXACTLY -- one error, at this field, of type ``timezone_aware`` -- so a
    refusal for another reason cannot satisfy it, and a CONVERT implementation (an after-validator
    attaching UTC) reds it. The ``text`` form is what the route actually receives: FastAPI binds this
    model with Query(), so every value arrives as a string, and a before-validator that parsed text
    and attached a zone could refuse the object while accepting the string.

    Args:
        field: The time filter sent without an offset.
        form: A Python datetime, or its offset-less ISO text.
    """
    naive = _NAIVE_SEARCH_BOUNDS[field]
    value = naive if form == 'object' else naive.isoformat()
    if form == 'text':
        assert value == naive.strftime('%Y-%m-%dT%H:%M:%S'), 'the fixture must carry no offset'

    with pytest.raises(ValidationError) as excinfo:
        StoreAssetDatasetQuery(**{field: value})
    assert [(error['loc'], error['type']) for error in excinfo.value.errors()] == [((field,), 'timezone_aware')]


@pytest.mark.parametrize(
    ('suffix', 'zone'), [('Z', UTC), ('-05:00', timezone(timedelta(hours=-5)))], ids=['zulu', 'nonzero-offset']
)
@pytest.mark.parametrize('field', sorted(_NAIVE_SEARCH_BOUNDS))
def test_an_offset_bearing_dataset_search_bound_is_accepted_as_the_instant_it_names(
    field: str, suffix: str, zone: timezone
):
    """The success half: the refusal above is also satisfied by a field that refuses EVERYTHING.

    The non-zero offset is compared as the INSTANT it names, which separates honouring the offset
    from dropping it: read as UTC, ``-05:00`` would filter five hours early. The offset itself is
    asserted too, so a guard that normalised to UTC on the way in would be seen rather than
    tolerated by the instant comparison alone.

    Args:
        field: The time filter sent with an offset.
        suffix: The offset designator appended to the ISO text.
        zone: The zone that designator names.
    """
    built = StoreAssetDatasetQuery(**{field: _NAIVE_SEARCH_BOUNDS[field].isoformat() + suffix})

    value = getattr(built, field)
    assert value.tzinfo is not None
    assert value == _NAIVE_SEARCH_BOUNDS[field].replace(tzinfo=zone)
    assert value.utcoffset() == zone.utcoffset(None)
    assert built.model_fields_set == {field}


@pytest.mark.parametrize('field', sorted(_NAIVE_SEARCH_BOUNDS))
def test_a_dataset_search_bound_stays_optional_and_none_means_no_constraint(field: str):
    """Tightening to AwareDatetime must not have tightened away the None default.

    An absent time filter is "no constraint on that column" (tj-vhboky.1 section 8), and
    search_entries skips every None value. An explicit null must be accepted as the same thing, not
    refused as a value that is not aware.

    Args:
        field: The time filter.
    """
    field_info = StoreAssetDatasetQuery.model_fields[field]
    assert not field_info.is_required()
    assert field_info.default is None
    assert getattr(StoreAssetDatasetQuery(), field) is None
    assert getattr(StoreAssetDatasetQuery(**{field: None}), field) is None
