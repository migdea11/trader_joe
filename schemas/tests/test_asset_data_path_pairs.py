"""AssetDataPath refuses every asset_type/data_type pair no route serves (tj-vhboky.68, schema half).

WHY THIS FILE EXISTS (validator, gating 726e995). AssetDataPath validated each enum on its own, so a
pair like crypto/quote constructed, and both internal asset-data routes then fell to ``case _`` and
raised a bare ValueError, an unhandled 500. 726e995 adds SUPPORTED_ASSET_DATA_PAIRS and a
``mode='after'`` model_validator that refuses any pair outside it.

What is pinned here, at the SCHEMA level only:
  * every unsupported pair is refused, with a message naming that pair. The pairs are GENERATED from
    the AssetType x DataType product, never listed by hand, so a member added to either enum is
    covered the day it lands;
  * the supported pair constructs;
  * a bad enum value is still reported against its own field, which is what ``mode='after'`` buys;
  * SUPPORTED_ASSET_DATA_PAIRS equals the set of pairs the routes' match statements serve.

WHAT "UNSUPPORTED" IS MEASURED AGAINST. The refusal cases are the enum product MINUS the pairs the
ROUTES serve, read from their source, not minus the constant under test. Subtracting the constant
would make the refusal cases shrink to match whatever the constant says, so a pair wrongly added to it
would simply stop being tested. Measured against the routes, that pair stays in the refusal list and
goes red.

WHY THE ROUTES ARE READ AS SOURCE (the least brittle honest option, per the bead). Each route decides
what it serves in a ``match (asset_path.asset_type, asset_path.data_type)`` statement, and that match
is the only place the fact lives. Calling the handlers cannot enumerate it: an unserved pair is now
refused by the model before a handler could see it. So the match statements are parsed with ``ast``
and every ``case`` is resolved to real enum members. The parse is strict: a case of any shape other
than ``(AssetType.X, DataType.Y)`` or the ``_`` wildcard fails the test rather than being skipped, and
the number of match statements must equal the number of handlers that take an AssetDataPath. If the
routes are restructured (a dispatch table, say), this test goes red and must be rewritten against the
new structure; it cannot go quietly vacuous.

NOT PINNED HERE: the HTTP status. Since e418c99 (tj-vhboky.72) the routes bind AssetDataPath with
``Path()`` instead of ``Depends()``, so the refusal answers 422, not 500. That status is pinned in
data/store/tests/test_asset_data_path_binding.py, not here.
"""

import ast
import importlib.util
import itertools
from pathlib import Path

import pytest
from pydantic import ValidationError

from common.enums.data_select import AssetType, DataType
from schemas.data_store.asset_data_interface import SUPPORTED_ASSET_DATA_PAIRS, AssetDataPath


pytestmark = pytest.mark.data_store

ROUTE_MODULE = 'routers.data_store.internal_asset_data'
_ENUMS = {'AssetType': AssetType, 'DataType': DataType}


def _route_source() -> tuple[Path, ast.Module]:
    # find_spec locates the file without importing it, so this schema test pulls in no app or database.
    spec = importlib.util.find_spec(ROUTE_MODULE)
    assert spec is not None and spec.origin is not None, f'{ROUTE_MODULE} not found'
    path = Path(spec.origin)
    return path, ast.parse(path.read_text(), filename=str(path))


def _enum_member(node: ast.pattern, where: str) -> AssetType | DataType:
    assert isinstance(node, ast.MatchValue), f'{where}: a case element is not a value pattern: {ast.dump(node)}'
    value = node.value
    assert isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name) and value.value.id in _ENUMS, (
        f'{where}: a case element is not AssetType.X or DataType.Y: {ast.unparse(value)}'
    )
    return _ENUMS[value.value.id][value.attr]


def _takes_asset_data_path(func: ast.AsyncFunctionDef | ast.FunctionDef) -> bool:
    return any('AssetDataPath' in ast.unparse(arg.annotation) for arg in func.args.args if arg.annotation is not None)


def _served_pairs_per_route() -> dict[str, frozenset[tuple[AssetType, DataType]]]:
    """Map each handler that takes an AssetDataPath to the pairs its match statement serves."""
    path, tree = _route_source()
    served: dict[str, frozenset[tuple[AssetType, DataType]]] = {}
    for func in ast.walk(tree):
        if not isinstance(func, ast.AsyncFunctionDef | ast.FunctionDef) or not _takes_asset_data_path(func):
            continue
        matches = [node for node in ast.walk(func) if isinstance(node, ast.Match)]
        where = f'{path.name}:{func.name}'
        assert len(matches) == 1, f'{where}: expected one match statement, found {len(matches)}'
        (match,) = matches
        subject = ast.unparse(match.subject)
        assert subject == '(asset_path.asset_type, asset_path.data_type)', (
            f'{where}: the match is on {subject}, not (asset_type, data_type) in that order'
        )
        pairs = set()
        for case in match.cases:
            pattern = case.pattern
            if isinstance(pattern, ast.MatchAs) and pattern.pattern is None and pattern.name is None:
                continue  # the `case _` fallback, which serves nothing
            assert case.guard is None, f'{where}: a guarded case cannot be read statically'
            assert isinstance(pattern, ast.MatchSequence) and len(pattern.patterns) == 2, (
                f'{where}: a case is not a two-element (asset_type, data_type) pattern: {ast.unparse(pattern)}'
            )
            asset, data = (_enum_member(element, where) for element in pattern.patterns)
            assert isinstance(asset, AssetType) and isinstance(data, DataType), (
                f'{where}: case {ast.unparse(pattern)} has its elements in the wrong order'
            )
            pairs.add((asset, data))
        served[func.name] = frozenset(pairs)
    return served


def _route_served_pairs() -> frozenset[tuple[AssetType, DataType]]:
    per_route = _served_pairs_per_route()
    # The read and the write route, both of which bind AssetDataPath. Fewer means the parse missed one,
    # and would be comparing the constant against a partial picture.
    assert set(per_route) == {'create_stock_market_activity_data', 'read_stock_market_activity_data'}, (
        f'the handlers taking AssetDataPath are now {sorted(per_route)}; review this test against them'
    )
    return frozenset().union(*per_route.values())


ROUTE_SERVED = _route_served_pairs()
ALL_PAIRS = list(itertools.product(AssetType, DataType))
UNSUPPORTED = [
    pytest.param(asset, data, id=f'{asset}/{data}') for asset, data in ALL_PAIRS if (asset, data) not in ROUTE_SERVED
]


def test_the_refusal_cases_are_not_vacuous():
    """The product is 3 x 3 today and one pair is served, so eight are refused. Zero would prove nothing."""
    assert ROUTE_SERVED, 'the parse found no served pair at all'
    assert UNSUPPORTED, 'every pair in the enum product is served, so nothing here is being refused'


@pytest.mark.parametrize(('asset_type', 'data_type'), UNSUPPORTED)
def test_an_unsupported_pair_is_refused_naming_the_pair(asset_type: AssetType, data_type: DataType):
    """Exactly one error, model-level (loc ()), of type value_error, whose message names the pair.

    Constructed from the enum VALUES, as a path arrives, rather than from members.
    """
    with pytest.raises(ValidationError) as excinfo:
        AssetDataPath(asset_type=asset_type.value, data_type=data_type.value)

    errors = excinfo.value.errors()
    assert [(error['loc'], error['type']) for error in errors] == [((), 'value_error')], errors
    message = errors[0]['msg']
    assert f'{asset_type.value}/{data_type.value}' in message, f'the refusal does not name the pair: {message!r}'


@pytest.mark.parametrize(
    ('asset_type', 'data_type'), [pytest.param(a, d, id=f'{a}/{d}') for a, d in sorted(ROUTE_SERVED)]
)
def test_a_served_pair_constructs(asset_type: AssetType, data_type: DataType):
    built = AssetDataPath(asset_type=asset_type.value, data_type=data_type.value)

    assert (built.asset_type, built.data_type) == (asset_type, data_type)


def test_the_supported_pair_is_stock_market_activity():
    """The one pair the bead names, stated literally once, so the route parse is itself cross-checked."""
    assert {(AssetType.STOCK, DataType.MARKET_ACTIVITY)} == ROUTE_SERVED
    AssetDataPath(asset_type='stock', data_type='market-activity')


def test_the_supported_pairs_constant_equals_what_the_routes_serve():
    """Enabling a pair means editing the constant AND the routes; this is what holds the two in step.

    A pair in the constant but not in a route would be accepted by the model and then fall to the
    route's ``case _``, the exact 500 this bead removes. A pair in a route but not in the constant is
    dead code the model makes unreachable. Each route is compared on its own, as well as the union, so
    a pair served by the write route alone cannot hide behind the read route.
    """
    per_route = _served_pairs_per_route()
    for route, pairs in per_route.items():
        assert pairs == SUPPORTED_ASSET_DATA_PAIRS, (
            f'{route} serves {sorted(pairs)}, the constant says {sorted(SUPPORTED_ASSET_DATA_PAIRS)}'
        )


def test_a_bad_enum_value_is_still_reported_against_its_own_field():
    """mode='after': the pair rule runs only once both fields are valid, so it cannot mask a field error."""
    with pytest.raises(ValidationError) as excinfo:
        AssetDataPath(asset_type='bogus', data_type='quote')

    assert [(error['loc'], error['type']) for error in excinfo.value.errors()] == [(('asset_type',), 'enum')]
