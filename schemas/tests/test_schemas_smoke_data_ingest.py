"""Smoke tests for ``schemas/data_ingest`` -- the request contract the ingest service is driven by.

Scope, per tj-goxb2r and ADR tj-fdb9gz: every module under the package imports, and every public
model constructs from a minimal valid payload and rejects an empty one. Reachability and shape,
never business correctness.

The marker is ``data_ingest`` and not ``schemas``: there is deliberately no ``schemas`` marker,
because ``schemas`` is a layer inside every component rather than a component of its own
(pytest.ini). A test carries the marker of the component whose interface it drives, whatever
directory it sits in. A single file wearing all three markers would be selected by every component
selection and would therefore tell ``make test-component COMPONENT=data_ingest`` nothing.

Why this exists now: the Phase 1 restructure (tj-55cczk) re-paths every one of these modules, and
these tests are what tells you whether the move broke an import.

No external resource. Nothing here is marked ``external`` and nothing skips.
"""

import importlib
import inspect
import json
import pkgutil
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel, ValidationError

import schemas.data_ingest
from common.enums.data_stock import ExpiryType, Feed, UpdateType
from schemas.data_ingest.get_dataset_request import (
    BaseGetDatasetRequest,
    CryptoDatasetRequest,
    GetDatasetRequest,
    OptionDatasetRequest,
    StockDatasetRequest,
)


pytestmark = pytest.mark.data_ingest


# Committed module inventory. Discovery on its own is vacuous -- a package that lost every module
# would still 'import all of them'. Asserting the discovered set EQUALS this one is what makes the
# import test fail on a module that was moved, renamed or deleted by the Phase 1 re-path.
EXPECTED_MODULES = frozenset({'schemas.data_ingest.get_dataset_request'})

DATASET_ID = UUID('00000000-0000-0000-0000-000000000001')
WHEN = datetime(2026, 1, 1, tzinfo=UTC)

# Every field on BaseGetDatasetRequest is required, `end` included -- it is `datetime | None` with
# no default, so the key must be present even when the value is null.
#
# `owner` is CARRIED THROUGH from the store request rather than originating here, and it is
# required because it is identity on the dataset entry (tj-vhboky.1 section 2): the fetch may not
# invent a principal.
#
# NO `feed` KEY, and this list changed to remove one. An earlier revision carried `'feed': 'IEX'`
# and a comment claiming feed had no default here because "by the time a fetch is dispatched the
# tape has been decided". That was wrong in both halves by 77f3a6c. The field is now
# `Feed | None = None`, so it is not required and including it here would put a key in the
# minimal payload that test_model_rejects_empty_payload then expects in the missing-set. What it
# actually is -- declared, optional and INERT -- is pinned by its own test below rather than by a
# fixture key that reads as "required".
_BASE_PAYLOAD: dict[str, Any] = {
    'dataset_id': DATASET_ID,
    'owner': 'rebalancer',
    'source': 'ALPACA',
    'granularity': '1day',
    'start': WHEN,
    'end': None,
    'expiry': WHEN,
    'expiry_type': ExpiryType.BULK,
    'update_type': UpdateType.STATIC,
}

# The asset fields the three concrete request shapes add. StockDatasetRequest re-declares
# asset_symbol and data_types; asset_type is inherited and still REQUIRED -- the payload proves
# it. It also USED to set `extra = 'ignore'` with a comment about ignoring asset_type, which never
# did that: asset_type was already a declared field, so the override only ever relaxed the model
# against genuinely unknown keys. 32438a9 removed it rather than repairing it.
_ASSET_PAYLOAD: dict[str, Any] = {'asset_symbol': 'VFV', 'asset_type': 'stock', 'data_types': ['market-activity']}

# Every public model in the package, with a minimal valid payload. Minimal means: every required
# field and nothing else.
CONSTRUCT_CASES: list[tuple[type[BaseModel], dict[str, Any]]] = [
    (BaseGetDatasetRequest, _BASE_PAYLOAD),
    (GetDatasetRequest, _BASE_PAYLOAD | _ASSET_PAYLOAD),
    (StockDatasetRequest, _BASE_PAYLOAD | _ASSET_PAYLOAD),
    (CryptoDatasetRequest, _BASE_PAYLOAD | _ASSET_PAYLOAD),
    (OptionDatasetRequest, _BASE_PAYLOAD | _ASSET_PAYLOAD),
]

# Public classes in the package that are NOT Pydantic models, and so are covered by a dedicated
# test rather than by the construct/reject pair. Empty here; see the data_store module for the
# case this list exists for.
KNOWN_NON_MODELS: frozenset[str] = frozenset()


def _public_classes(module) -> set[str]:
    """Return the names of classes defined in ``module``, excluding private ones.

    ``name.isidentifier()`` filters out Pydantic's concrete generic aliases: parametrising a
    generic model injects a key like ``AssetDataCreate[StockDataMarketActivityData]`` into the
    defining module's namespace, whose ``__module__`` is that module. Those are the same class
    under a different binding, not a new part of the contract.

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
    """Name each parametrized case after its model class, letting pytest label the payload.

    Args:
        value: One argument of a parametrized case.

    Returns:
        The class name, or ``None`` to fall back to pytest's own representation.
    """
    return value.__name__ if isinstance(value, type) else None


def test_module_inventory_matches_the_package():
    discovered = {name for _, name, _ in pkgutil.walk_packages(schemas.data_ingest.__path__, 'schemas.data_ingest.')}
    assert discovered == EXPECTED_MODULES


@pytest.mark.parametrize('module_name', sorted(EXPECTED_MODULES))
def test_module_imports(module_name: str):
    assert importlib.import_module(module_name) is not None


def test_every_public_model_is_covered():
    """Fail on a model added to the package that no case below exercises.

    Without this the construct/reject pair silently stops being a smoke test of the package and
    becomes a smoke test of whatever someone last remembered to list.
    """
    covered = {model.__name__ for model, _ in CONSTRUCT_CASES} | KNOWN_NON_MODELS
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


@pytest.mark.parametrize(('model', 'payload'), CONSTRUCT_CASES, ids=_case_id)
def test_model_rejects_an_unknown_field(model: type[BaseModel], payload: dict[str, Any]):
    """Every request shape here is RECEIVED, so every one rejects a field it does not declare.

    These models are the far end of the store->ingest Kafka RPC, which makes them the exact case
    ``schemas/inbound_contract.py`` was written for (32438a9, tj-vhboky.1 ruling of 2026-09-25):
    an internal sender still putting a field on the wire after the contract dropped it used to
    succeed silently. ``StockDatasetRequest`` was the worst of them -- it carried an explicit
    ``extra = 'ignore'`` override, so it relaxed the guarantee for the three concrete shapes that
    inherit from it.

    Args:
        model: The model class.
        payload: Its minimal valid payload, which this adds one unknown key to.
    """
    with pytest.raises(ValidationError) as excinfo:
        model(**payload | {'a_field_no_contract_declares': 1})
    errors = excinfo.value.errors()
    assert [error['loc'] for error in errors] == [('a_field_no_contract_declares',)]
    assert [error['type'] for error in errors] == ['extra_forbidden']


def test_the_request_feed_is_declared_optional_and_is_read_by_nothing():
    """Pin feed here as DECLARED BUT INERT, which is neither "required" nor "gone".

    It is a landing site, not a working field, and both halves need pinning because each alone
    reads as the opposite of the truth:

      OPTIONAL     ``Feed | None = None``. tj-rh4b7f (2026-09-25) deferred caller-selected feed,
                   so ``StoreAssetDatasetBody`` has no feed to forward and the store's
                   ``model_dump()`` splat leaves this at its default. A required field here would
                   make every dispatch fail today.
      READ BY
      NOTHING      The one resolution site in data/ingest consults a deployment env var and never
                   the request. A request naming a tape is accepted and ignored -- which is
                   precisely the failure the strict base above exists to prevent, arriving through
                   a declared field rather than past one.

    THE SECOND HALF IS PINNED IN data/ingest/tests/test_broker_api.py, not here, by driving the
    adapter with a request that names SIP against a deployment configured for IEX and watching the
    vendor call get IEX anyway. A schemas test can only assert the declaration; asserting "nothing
    reads it" from this layer would mean grepping the adapter's source, which passes on a rename
    and would import data/ingest into a schemas test to do it.

    The field survives anyway because it is the designated landing site for the deferred transport
    work: it is the store->ingest channel a feed would be resolved over.
    """
    field = BaseGetDatasetRequest.model_fields['feed']
    assert not field.is_required()
    assert field.default is None
    assert BaseGetDatasetRequest(**_BASE_PAYLOAD).feed is None

    # Declared, so a caller naming a tape is ACCEPTED rather than rejected as an unknown field.
    # This is the one thing that distinguishes "inert" from "gone", and it is what makes the
    # deferred transport work an ingest-side change rather than a contract change.
    assert BaseGetDatasetRequest(**_BASE_PAYLOAD | {'feed': 'SIP'}).feed is Feed.SIP

    # And it is a value the SHARED enum can express -- a second Feed enum declared anywhere would
    # compare unequal to this one and fail only at runtime.
    assert Feed.SIP in set(Feed)


# ---------------------------------------------------------------------------------------------
# The RPC contract refuses naive times (F4, tj-vhboky.24)
# ---------------------------------------------------------------------------------------------

# BaseGetDatasetRequest's three time fields, each with an OFFSET-LESS instant that is otherwise
# valid beside the rest of the payload. User ruling D2 = (A) on tj-vhboky.20 (2026-09-27, 21:21
# UTC) says REFUSE, not convert -- the tj-1bl90i rule, applied to the store->ingest Kafka RPC
# contract. fa1d7ee made them AwareDatetime.
_NAIVE_RPC_TIMES: dict[str, datetime] = {
    'start': datetime(2026, 1, 1),
    'end': datetime(2026, 3, 1),
    'expiry': datetime(2026, 2, 1),
}

_REQUEST_MODELS = [model for model, _ in CONSTRUCT_CASES]
_PAYLOADS = dict(CONSTRUCT_CASES)

_MINUS_FIVE = timezone(timedelta(hours=-5))


def _wire(model: type[BaseModel], overrides: dict[str, Any]) -> str:
    """The JSON the RPC would carry for ``model``'s minimal payload, with some keys replaced.

    Built from the model's own ``model_dump(mode='json')`` rather than by hand, so the enum and UUID
    keys are exactly what the sender puts on the wire; only the overridden keys are ours.

    Args:
        model: The request model.
        overrides: Keys to replace in the dumped payload, already in their wire (JSON) form.

    Returns:
        str: The JSON text.
    """
    return json.dumps(model(**_PAYLOADS[model]).model_dump(mode='json') | overrides)


@pytest.mark.parametrize('form', ['object', 'wire-text'])
@pytest.mark.parametrize('field', sorted(_NAIVE_RPC_TIMES))
@pytest.mark.parametrize('model', _REQUEST_MODELS, ids=_case_id)
def test_a_naive_rpc_time_is_refused_at_its_own_field(model: type[BaseModel], field: str, form: str):
    """REFUSE, per field, per request shape: user ruling D2 = (A), tj-vhboky.20, 2026-09-27.

    Before fa1d7ee all three were plain ``datetime``, so a sender that put an offset-less time on the
    Kafka topic had it accepted and handed to the vendor fetch as an instant in whatever zone the
    ingest host assumed.

    The error list is asserted EXACTLY -- one error, at this field, of type ``timezone_aware`` -- so a
    refusal for another reason cannot satisfy it, and a CONVERT implementation (an after-validator
    attaching UTC) reds it. ``wire-text`` is how the value actually arrives: the RPC server validates
    the message with ``model_validate_json`` (common/kafka/rpc/kafka_rpc_server.py), so the naive
    value is offset-less ISO text inside JSON. Every request shape is included because the ingest
    router rebuilds the concrete ones from the base (routers/data_ingest/get_dataset_request.py); a
    subclass that re-declared a field as ``datetime`` would reopen the hole below the base.

    Args:
        model: The request shape under test.
        field: The time field sent without an offset.
        form: A Python datetime passed to the constructor, or offset-less ISO text in JSON.
    """
    naive = _NAIVE_RPC_TIMES[field]
    with pytest.raises(ValidationError) as excinfo:
        if form == 'object':
            model(**_PAYLOADS[model] | {field: naive})
        else:
            text = naive.isoformat()
            assert text == naive.strftime('%Y-%m-%dT%H:%M:%S'), 'the fixture must carry no offset'
            model.model_validate_json(_wire(model, {field: text}))
    assert [(error['loc'], error['type']) for error in excinfo.value.errors()] == [((field,), 'timezone_aware')]


@pytest.mark.parametrize(('suffix', 'zone'), [('Z', UTC), ('-05:00', _MINUS_FIVE)], ids=['zulu', 'nonzero-offset'])
@pytest.mark.parametrize('field', sorted(_NAIVE_RPC_TIMES))
def test_an_offset_bearing_rpc_time_is_accepted_as_the_instant_it_names(field: str, suffix: str, zone: timezone):
    """The success half: the refusal above is also satisfied by a field that refuses EVERYTHING.

    'Z' is what the store's own create body carries, so a guard that refused it would break the only
    in-tree sender. The non-zero offset is compared as the INSTANT it names -- read as UTC, ``-05:00``
    would fetch five hours early -- and the offset itself is asserted, so normalising on the way in
    would be seen.

    Args:
        field: The time field sent with an offset.
        suffix: The offset designator appended to the ISO text.
        zone: The zone that designator names.
    """
    text = _NAIVE_RPC_TIMES[field].isoformat() + suffix

    request = BaseGetDatasetRequest.model_validate_json(_wire(BaseGetDatasetRequest, {field: text}))

    value = getattr(request, field)
    assert value.tzinfo is not None
    assert value == _NAIVE_RPC_TIMES[field].replace(tzinfo=zone)
    assert value.utcoffset() == zone.utcoffset(None)


def test_the_rpc_end_stays_required_but_nullable():
    """Tightening ``end`` to AwareDatetime must keep ``| None`` AND keep it without a default.

    Null is the open-ended dataset, and it is what data_action_request.py forwards whenever the
    create body left ``end`` out -- so a refusal of None would break every open-ended fetch. Absent,
    on the other hand, stays a ``missing`` error at ``end``: the sender must say "open-ended"
    explicitly rather than by forgetting the key.
    """
    assert BaseGetDatasetRequest(**_BASE_PAYLOAD | {'end': None}).end is None
    assert BaseGetDatasetRequest.model_validate_json(_wire(BaseGetDatasetRequest, {'end': None})).end is None

    field = BaseGetDatasetRequest.model_fields['end']
    assert field.is_required()

    without_end = {key: value for key, value in _BASE_PAYLOAD.items() if key != 'end'}
    with pytest.raises(ValidationError) as excinfo:
        BaseGetDatasetRequest(**without_end)
    assert [(error['loc'], error['type']) for error in excinfo.value.errors()] == [(('end',), 'missing')]


@pytest.mark.parametrize('model', _REQUEST_MODELS, ids=_case_id)
@pytest.mark.parametrize('end', [datetime(2026, 3, 1, tzinfo=timezone(timedelta(hours=9, minutes=30))), None])
def test_a_json_round_trip_keeps_every_offset(model: type[BaseModel], end: datetime | None):
    """The RPC's own serialisation must deliver an aware request the receiver still accepts.

    The store sends ``request.model_dump_json()`` and the ingest server re-validates it with
    ``model_validate_json`` (common/kafka/rpc/kafka_rpc_client.py and kafka_rpc_server.py). If the
    dump dropped an offset, tightening the receiver would 422 -- more exactly, raise inside the RPC
    server -- on every request the store itself builds. Three different offsets, one per field, so a
    serialiser that kept only the first, or normalised all of them to one zone, is caught by the
    per-field offset assertion rather than hidden by instant equality.

    This drives the model's round trip, not the ``RpcRequest`` envelope around it; the envelope is
    common/kafka's, and nothing here claims it.

    Args:
        model: The request shape under test.
        end: A non-UTC aware end, or the open-ended null.
    """
    times = {'start': datetime(2026, 1, 1, tzinfo=_MINUS_FIVE), 'end': end, 'expiry': datetime(2026, 2, 1, tzinfo=UTC)}
    sent = model(**_PAYLOADS[model] | times)

    received = model.model_validate_json(sent.model_dump_json())

    assert received == sent
    for field, value in times.items():
        if value is None:
            assert getattr(received, field) is None
        else:
            assert getattr(received, field).utcoffset() == value.utcoffset(), f'{field} lost its offset'
