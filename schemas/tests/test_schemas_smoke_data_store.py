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
# _IDENTIFIER is also the base of AssetDataUpdate and AssetDataQuery, which still declare no feed --
# an update addresses a row by id and a query filters rather than reports -- and those models are
# `extra='forbid'`, so handing either one a feed would RAISE rather than be ignored.
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
# THE TWO QUERY MODELS MOVED HERE FROM THE CONSTRUCT LIST, and the move is the contract change
# rather than a tidy-up (tj-vhboky.1 section 8, tj-6z03hd's escalation). Their fields were
# annotated `| None` with NO default, which in Pydantic v2 means required-but-nullable: a caller
# had to pass every one explicitly, so the object could not represent an unfiltered request and
# could not serve as an optional FastAPI query dependency. Being constructible from `{}` is now
# the point of them, so an empty-payload REJECTION would pin the opposite of the ruling.
CONSTRAINT_CASES: list[tuple[type[BaseModel], dict[str, Any], str, str]] = [
    (
        StoreAssetDatasetQuery,
        {'source': 'NOT_A_BROKER'},
        'source',
        '`source` is `DataSource | None`, so a value outside the DataSource enum is rejected even '
        'though omitting the field entirely is fine.',
    ),
    (
        AssetDataQuery,
        {'source': 'NOT_A_BROKER'},
        'source',
        '`source` is `DataSource | None`. Every field defaults to unset so `{}` is valid, but a '
        'value outside the DataSource enum is still rejected -- "no filter" and "any filter" are '
        'not the same thing.',
    ),
    (
        StockDataMarketActivityQuery,
        {'source': 'NOT_A_BROKER'},
        'source',
        'The parametrised stock binding inherits the same all-optional shape, including the '
        'nested `query` submodel, which is what lets the ONE filtering read path take it as an '
        'optional dependency.',
    ),
]

# Models with NO declared constraint to violate, so neither an empty-payload rejection nor a
# constraint case can be written honestly. Listing one here is a finding, not a gap in the tests.
#
# StockMarketActivityDataQuery declares zero fields, so there is no DECLARED constraint to
# violate and no field a rejection could be reported against. The only assertion worth making is
# that it constructs and is empty, which is what test_no_constraint_model_constructs does.
#
# THE REASON RECORDED HERE USED TO BE THE WRONG ONE. It said no payload at all could be invalid,
# because Pydantic v2 ignores extras by default. That stopped being true in 32438a9: the class is
# an InboundContract, so `extra='forbid'` applies and an unknown key IS rejected even with no
# fields declared. It stays on this list because zero fields still means zero constraints to
# violate, but the strict half is now pinned -- by test_no_constraint_model_constructs below.
NO_CONSTRAINT_CASES: list[type[BaseModel]] = [StockMarketActivityDataQuery]

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


@pytest.mark.parametrize(
    ('model', 'payload'),
    # The constraint cases contribute `{}`, not their listed payload: that payload is the one that
    # VIOLATES their constraint, so adding an unknown key to it would raise two errors and the
    # assertion below would be measuring the violation as much as the unknown field.
    CONSTRUCT_CASES + [(model, {}) for model, _, _, _ in CONSTRAINT_CASES],
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


@pytest.mark.parametrize('model', NO_CONSTRAINT_CASES, ids=_case_id)
def test_no_constraint_model_constructs(model: type[BaseModel]):
    """Construct a model that declares no field, and assert exactly that and nothing more.

    There is no CONSTRAINT case to write: with no fields declared there is nothing whose declared
    constraint could be violated. Asserting the field set is empty is the honest assertion -- it
    fails the day the model gains a field, which is the day a constraint case becomes writable.

    An unknown key is still rejected, and that is asserted here rather than left implicit: a model
    with no fields is the one place a reader would assume anything goes, and under
    ``schemas/inbound_contract.py`` it is instead the strictest thing in the package.

    Args:
        model: The model class.
    """
    assert model.model_fields == {}
    assert model().model_dump() == {}
    with pytest.raises(ValidationError) as excinfo:
        model(a_field_no_contract_declares=1)
    assert [error['type'] for error in excinfo.value.errors()] == ['extra_forbidden']


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

    WHAT IS STILL ABSENT, AND WHY THAT IS NOT AN OVERSIGHT: ``AssetDataUpdate`` and
    ``AssetDataQuery``. An update addresses an existing row by id, so obliging a caller to restate
    identity it is not changing would invite a mismatch between the feed sent and the feed stored.
    A query FILTERS rather than reports, and every query field is optional by the tj-vhboky.1
    section 8 ruling, so a required feed there would make an unfiltered read impossible. The field
    therefore sits on the concrete create and read models and NOT on the shared ``_AssetIdentifier``
    mixin, which is the structural fact this test's three lists together pin.
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

    # NOT on the shared mixin, which is what keeps a required feed off the update and query paths.
    assert 'feed' not in _AssetIdentifier.model_fields
    for model in (AssetDataUpdate, AssetDataQuery, StockDataMarketActivityUpdate, StockDataMarketActivityQuery):
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


@pytest.mark.parametrize('field', ['expiry_type', 'update_type'])
def test_a_policy_field_rejects_an_explicit_null_rather_than_defaulting_it(field: str):
    """THE CONTRACT HALF OF THE NULL-IN-A-UNIQUE-KEY HAZARD. tj-vhboky.1 section 2 pins the other.

    These fields have defaults, so OMITTING them is fine and the minimal payload leaves them out.
    Sending ``null`` explicitly is a different request and must be rejected, because the value
    lands in a UNIQUE constraint and Postgres treats NULL as distinct from NULL in a unique index.
    A NULL written into a key column means the ON CONFLICT never fires against that row, and "an
    exact repeat returns the existing id" silently becomes "an exact repeat creates a second row"
    -- the duplication the whole identity model exists to make impossible.

    Making the annotation non-Optional is what produces the rejection. A ``| None`` annotation
    with a non-None default would accept the null and store it.

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
