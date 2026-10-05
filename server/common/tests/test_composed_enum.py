"""Tests for the superset-composition machinery itself, not for any one composed enum.

WHY THIS FILE EXISTS SEPARATELY FROM the Feed pins in schemas/tests. Those assert the OUTCOME --
Feed has exactly three members, equal to the union of its subsets. This file asserts the
MACHINERY that produces that outcome, because two of its guarantees fail silently when broken:

  * compose() raising on a name contributed twice with different values is what makes the
    module's whole argument safe ("no list is hand-maintained, so no two lists can drift"). If
    that branch is ever removed or moved, the superset silently keeps whichever value imported
    first and the stored vocabulary starts depending on import order.
  * StrSupersetEnum.__str__ is a fix for an OBSERVED bug, not a hypothetical one: the identical
    defect is recorded live in common/kafka/topics.py (tj-09rtle). A (str, Enum) mixin is not a
    StrEnum -- drop the one line and str(Feed.IEX) becomes 'Feed.IEX' in every log line, query
    string and vendor request the value reaches, with nothing failing at the point of the change.

Both were previously confirmed by READING THE SOURCE. A source read passes on a rename and on a
refactor that relocates the check, which is exactly the class of change that would reintroduce
these bugs.

THE COLLISION CASES USE THROWAWAY ENUMS DECLARED IN THE TEST, never Feed. compose() mutates the
superset class in place via aenum's extend_enum, so composing into the real Feed here would leak
into every other test in the session.
"""

import pytest

from common.enums.composed_enum import StrSupersetEnum, SubsetStrEnum
from common.enums.data_stock import Feed, UsEquityFeed


pytestmark = pytest.mark.common


def test_compose_rejects_a_name_contributed_twice_with_different_values():
    """A name meaning two things in the stored vocabulary is a collision, not a merge.

    Silently keeping the first arrival would make the meaning depend on import order -- the
    superset would be correct or wrong according to which module happened to be imported first,
    which is undebuggable from the failure it eventually produces downstream.
    """

    class _FirstMarket(SubsetStrEnum):
        CONSOLIDATED = 'SIP'

    class _SecondMarket(SubsetStrEnum):
        CONSOLIDATED = 'CTA'

    class _Superset(StrSupersetEnum):
        pass

    with pytest.raises(ValueError, match='CONSOLIDATED'):
        _Superset.compose(_FirstMarket, _SecondMarket)


def test_compose_accepts_the_same_name_and_value_from_two_subsets():
    """A value shared across markets may be declared in each of them.

    NOT_APPLICABLE is the real motivating case, so this is not speculative -- but it lives in
    exactly one subset today, so nothing in the production enums exercises the re-contribution
    path. The superset must end up with ONE member, not two and not a raise.
    """

    class _FirstMarket(SubsetStrEnum):
        NOT_APPLICABLE = 'NOT_APPLICABLE'
        IEX = 'IEX'

    class _SecondMarket(SubsetStrEnum):
        NOT_APPLICABLE = 'NOT_APPLICABLE'
        LSE = 'LSE'

    class _Superset(StrSupersetEnum):
        pass

    _Superset.compose(_FirstMarket, _SecondMarket)

    assert {member.name for member in _Superset} == {'NOT_APPLICABLE', 'IEX', 'LSE'}
    assert _Superset('NOT_APPLICABLE') is _Superset.NOT_APPLICABLE


def test_a_composed_str_member_stringifies_to_its_value_in_both_forms():
    """str() and f-string interpolation fail INDEPENDENTLY, so both are pinned.

    tj-09rtle demonstrates that on the live defect in common/kafka/topics.py. A value that
    stringifies as 'Feed.IEX' reaches a vendor query string or a log line intact and is only
    noticed downstream, well away from the change that caused it.

    THE REAL-ENUM HALF IS WEAKER THAN IT LOOKS, AND THAT IS WHY THE THROWAWAY IS HERE
    (tj-1njw7c). Every Feed member's name equals its value today -- IEX, SIP, NOT_APPLICABLE --
    so `str(Feed.IEX) == 'IEX'` cannot tell the VALUE from the NAME. It catches __str__ being
    dropped, which is the observed bug, but a __str__ returning `self.name` passes it. The
    throwaway below is composed from a member whose name and value differ, which is the only
    shape that distinguishes them. Feed is still asserted on too: the throwaway proves the
    machinery and Feed proves the class that actually crosses the wire.
    """
    assert str(Feed.IEX) == 'IEX'
    assert f'{Feed.IEX}' == 'IEX'

    class _NameDiffersFromValue(SubsetStrEnum):
        CONSOLIDATED = 'SIP'

    class _Superset(StrSupersetEnum):
        pass

    _Superset.compose(_NameDiffersFromValue)

    assert str(_Superset.CONSOLIDATED) == 'SIP'
    assert f'{_Superset.CONSOLIDATED}' == 'SIP'


def test_narrowing_upward_to_the_superset_is_total():
    """compose() guarantees every subset member is a superset member, so this cannot raise.

    The module docstring makes exactly this claim to justify having no upward helper. One line
    holds it to that.
    """
    assert Feed(UsEquityFeed.IEX) is Feed.IEX
