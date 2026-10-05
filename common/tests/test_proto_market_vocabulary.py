"""market/v1 IS common/enums, spelled for the wire (tj-3mk3u5.27 ENUMS; decision tj-3mk3u5.42 F1 rule 9).

ADR tj-8konfu D1 keeps Python canonical and the .proto the wire's source of truth, with NEITHER
generated from the other. That buys independence and costs exactly one thing: the two can drift, and
nothing in either file says so. A member added to ``common.enums`` and not to ``market/v1`` is
unencodable; a member added only to the proto decodes to a name no Python enum has. Both surface far
from the edit, inside a mapper, as a lookup that fails on one value.

So the mirror is asserted here, by NAME, which is what crosses the wire (tj-vhboky.30). Strip the
``<ENUM_NAME>_`` prefix, drop the zero ``_UNSPECIFIED``, and what is left must equal the Python member
names exactly, both ways.

The marker is ``common``: these mirror ``common/enums``, which is the shared library's own surface.
``market/v1`` is shared vocabulary rather than any one service's contract -- the UI imports it too
(tj-grna9p.4 section 5) -- so it is pinned here and not beside the fetch contract.
"""

import pytest

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity, UpdateType
from common.tests.proto_descriptors import file_named


pytestmark = pytest.mark.common

ENUMS = 'trader_joe/proto/market/v1/enums.proto'
BAR = 'trader_joe/proto/market/v1/bar.proto'

# Each proto enum in market/v1 with the common/enums type it mirrors. Both directions of this map are
# checked below, so neither a proto enum nor a Python one can be added without the other.
MIRRORS: dict[str, type] = {
    'DataSource': DataSource,
    'AssetType': AssetType,
    'DataType': DataType,
    'Granularity': Granularity,
    'Feed': Feed,
    'UpdateType': UpdateType,
}


def _upper_snake(name: str) -> str:
    """The <ENUM_NAME_UPPER_SNAKE> prefix protoc's sibling scoping forces onto every value.

    Args:
        name: A proto enum's CamelCase name, e.g. ``DataSource``.

    Returns:
        str: Its upper-snake form, e.g. ``DATA_SOURCE``.
    """
    return ''.join(f'_{char}' if index and char.isupper() else char for index, char in enumerate(name)).upper()


def _proto_enums() -> dict[str, list[str]]:
    """Every enum declared in market/v1's enums.proto, with its value names in declaration order."""
    return {enum.name: [value.name for value in enum.value] for enum in file_named(ENUMS).enum_type}


def test_the_vocabulary_file_declares_exactly_the_enums_that_mirror_common_enums():
    """Both directions, so neither side can gain a type quietly.

    An enum added to market/v1 with no Python mirror is a vocabulary nothing in this repository can
    produce; a Python enum that reaches the wire with no proto mirror cannot be encoded at all.
    """
    assert set(_proto_enums()) == set(MIRRORS)


@pytest.mark.parametrize('enum_name', sorted(MIRRORS))
def test_every_value_carries_its_enums_name_as_a_prefix(enum_name: str):
    """F1 rule 9, and buf's STANDARD lint: the prefix is NOT optional.

    protoc scopes an enum's values as siblings of the enum WITHIN THE PACKAGE, not inside the enum, so
    two enums in market/v1 could not both declare ``UNSPECIFIED`` -- the file would not compile. The
    prefix is therefore load-bearing rather than stylistic, and pinning it here says so at the point
    where someone would be tempted to drop it for brevity. ``buf lint`` catches it too; this catches
    it without buf on PATH, and names the rule it is enforcing.

    Args:
        enum_name: The proto enum under test.
    """
    prefix = f'{_upper_snake(enum_name)}_'
    values = _proto_enums()[enum_name]

    unprefixed = [value for value in values if not value.startswith(prefix)]
    assert not unprefixed, f'{enum_name} values must start with {prefix!r}: {unprefixed}'


@pytest.mark.parametrize('enum_name', sorted(MIRRORS))
def test_the_zero_value_is_unspecified_and_has_no_python_counterpart(enum_name: str):
    """proto3 demands a zero value; tj-vhboky.1 forbids a "we do not know" member in Python.

    Both hold at once because the zero is a wire artefact and nothing else: it exists to satisfy
    proto3 and buf, the mapper refuses it on decode wherever the field is required, and it never
    reaches a domain model. ``Feed.UNKNOWN`` was removed by the user's ruling -- "a value that should
    never be written is better expressed as an ERROR than as an enum member" -- and an ``UNSPECIFIED``
    appearing in the Python enum would be that value walking back in through the wire's door.

    Args:
        enum_name: The proto enum under test.
    """
    values = _proto_enums()[enum_name]

    assert values[0] == f'{_upper_snake(enum_name)}_UNSPECIFIED', 'the zero value is first in declaration order'
    assert 'UNSPECIFIED' not in MIRRORS[enum_name].__members__


@pytest.mark.parametrize('enum_name', sorted(MIRRORS))
def test_the_proto_members_are_the_python_member_names_one_for_one(enum_name: str):
    """THE MIRROR ITSELF: the drift check D1's independence costs and nothing else pays.

    Compared as SETS of names, because declaration order carries no meaning on either side -- the
    proto's field numbers do, and the Python members' values do, and neither is this. Names are what
    cross the wire (tj-vhboky.30): ``DataSource.IB_API``'s Python VALUE is the shorter ``'IB'`` and
    ``UpdateType``'s are integers, so a test comparing values would be comparing the wrong thing and
    would pass or fail for reasons that have nothing to do with the contract.

    Args:
        enum_name: The proto enum under test.
    """
    prefix = f'{_upper_snake(enum_name)}_'
    on_the_wire = {value.removeprefix(prefix) for value in _proto_enums()[enum_name]} - {'UNSPECIFIED'}

    assert on_the_wire == set(MIRRORS[enum_name].__members__)


def test_the_shared_bar_imports_the_vocabulary_rather_than_restating_it():
    """market/v1 is one package in two files so a consumer can take the vocabulary without the bar.

    The UI needs ``UpdateType`` and no bar (tj-grna9p.4 section 5). Splitting the files is what makes
    that possible; Bar importing enums.proto by canonical path rather than declaring its own Feed is
    what keeps the split from becoming two vocabularies.
    """
    bar_file = file_named(BAR)

    assert bar_file.package == 'trader_joe.proto.market.v1'
    assert sorted(bar_file.dependency) == ['google/protobuf/timestamp.proto', 'trader_joe/proto/market/v1/enums.proto']
    assert not bar_file.enum_type, (
        f'Bar restates vocabulary instead of importing it: {[e.name for e in bar_file.enum_type]}'
    )

    bar = {message.name: message for message in bar_file.message_type}['Bar']
    assert [field.name for field in bar.field] == [
        'bar_start',
        'open',
        'high',
        'low',
        'close',
        'volume',
        'trade_count',
        'vwap',
        'feed',
    ]


def test_the_bars_optional_fields_have_real_presence():
    """``trade_count`` and ``vwap`` are proto3 ``optional``, so None round-trips without a sentinel.

    A vendor that reports neither is not reporting zero of each, and a double with no presence cannot
    tell those apart -- 0.0 and "absent" are the same bytes. The domain twin declares them
    ``| None = None`` for the same reason.
    """
    bar = {message.name: message for message in file_named(BAR).message_type}['Bar']
    presence = {field.name: field.proto3_optional for field in bar.field}

    assert presence['trade_count'] is True
    assert presence['vwap'] is True
    assert not [name for name, optional in presence.items() if optional and name not in {'trade_count', 'vwap'}]
