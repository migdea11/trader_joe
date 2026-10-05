"""The exception hierarchy of ADR tj-fa1rpu D5, as TE-1 tj-3mk3u5.37.3 builds it.

A base, exactly two branches under it, and leaves that subclass one branch directly. Nothing goes deeper,
because an except clause on an interior node collapses again the failures the vocabulary tells apart. Every
reason belongs to one branch, and an error of the other branch cannot carry it, so the class and the table
never disagree. The leaf rule is checked when a class is DEFINED, which is what these tests do.
"""

from datetime import UTC, datetime

import pytest

from common.errors.vocabulary import (
    REASONS,
    RESERVED_REASONS,
    ExogenousError,
    InvalidRequestError,
    Reason,
    TraderJoeError,
)


pytestmark = pytest.mark.common

BRANCHES = (ExogenousError, InvalidRequestError)
BRANCH_IDS = [branch.__name__ for branch in BRANCHES]

# The two reasons the bead says ALWAYS carry reset_at; every other reason builds without one.
RESET_AT_REQUIRED = frozenset({'RATE_BUDGET', 'VENDOR_RATE_LIMITED'})
RESET_AT = datetime(2001, 2, 3, 4, 5, 6, tzinfo=UTC)


def _build(cls: type, reason: object) -> TraderJoeError:
    """Build cls for reason with what the design otherwise requires, so only the class-reason pairing is on trial."""
    if str(reason) in RESET_AT_REQUIRED:
        return cls(reason, 'detail', reset_at=RESET_AT)
    return cls(reason, 'detail')


def _other_branch(reason: Reason) -> type:
    return InvalidRequestError if REASONS[reason].branch is ExogenousError else ExogenousError


def test_the_tree_is_one_base_and_exactly_two_branches():
    """D5: TraderJoeError(Exception), with ExogenousError and InvalidRequestError directly beneath it."""
    assert TraderJoeError.__bases__ == (Exception,)
    assert ExogenousError.__bases__ == (TraderJoeError,)
    assert InvalidRequestError.__bases__ == (TraderJoeError,)


@pytest.mark.parametrize('reason', list(Reason), ids=str)
def test_each_reason_belongs_to_one_of_the_two_branches(reason: Reason):
    """The bead: every reason belongs to ONE branch, the table's branch column."""
    assert REASONS[reason].branch in BRANCHES


@pytest.mark.parametrize('branch', BRANCHES, ids=BRANCH_IDS)
def test_a_leaf_may_subclass_a_branch_directly(branch: type):
    """D5: leaf classes elsewhere subclass ExogenousError or InvalidRequestError directly."""

    class Leaf(branch):
        pass

    assert Leaf.__bases__ == (branch,)


@pytest.mark.parametrize('branch', BRANCHES, ids=BRANCH_IDS)
def test_a_subclass_of_a_leaf_is_refused_when_it_is_defined(branch: type):
    """D5: nothing subclasses a leaf, so there is no interior node below the two branches."""

    class Leaf(branch):
        pass

    with pytest.raises(TypeError):

        class LeafOfALeaf(Leaf):
            pass


def test_a_third_branch_under_the_base_is_refused():
    """D5: the base has exactly two branches; a class directly under it is refused."""
    with pytest.raises(TypeError):

        class ThirdBranch(TraderJoeError):
            pass


def test_a_branch_cannot_be_declared_outside_the_vocabulary():
    """The branch flag only works in common/errors, so a third branch cannot be added from anywhere else."""
    with pytest.raises(TypeError):

        class ThirdBranch(TraderJoeError, _branch=True):
            pass


def test_a_class_under_both_branches_is_refused():
    """The bead: every reason belongs to ONE branch, so no class may be both."""
    with pytest.raises(TypeError):

        class Both(ExogenousError, InvalidRequestError):
            pass


@pytest.mark.parametrize('reason', list(Reason), ids=str)
def test_the_base_itself_is_never_built(reason: Reason):
    """The base belongs to no branch, so building it would let a class and the table disagree."""
    with pytest.raises(TypeError):
        _build(TraderJoeError, reason)


@pytest.mark.parametrize('as_leaf', [False, True], ids=['branch', 'leaf'])
@pytest.mark.parametrize('reason', list(Reason), ids=str)
def test_each_reason_builds_under_its_own_branch(reason: Reason, as_leaf: bool):
    """The positive half of the pairing: the reason's own branch, or a leaf of it, carries it."""
    cls = REASONS[reason].branch
    if as_leaf:

        class Leaf(cls):
            pass

        cls = Leaf
    error = _build(cls, reason)
    assert error.reason is reason
    assert error.detail == 'detail'


@pytest.mark.parametrize('as_leaf', [False, True], ids=['branch', 'leaf'])
@pytest.mark.parametrize('reason', list(Reason), ids=str)
def test_a_reason_of_the_other_branch_is_refused(reason: Reason, as_leaf: bool):
    """The bead: an error whose reason belongs to the other branch is refused with a TypeError."""
    cls = _other_branch(reason)
    if as_leaf:

        class Leaf(cls):
            pass

        cls = Leaf
    with pytest.raises(TypeError):
        _build(cls, reason)


@pytest.mark.parametrize('name', [reason.value for reason in Reason])
def test_a_reason_must_be_a_member_not_a_str_equal_to_one(name: str):
    """A StrEnum value compares equal to its str, so only the type check keeps a bare str out."""
    with pytest.raises(TypeError):
        _build(REASONS[Reason(name)].branch, name)


@pytest.mark.parametrize('branch', BRANCHES, ids=BRANCH_IDS)
@pytest.mark.parametrize('name', RESERVED_REASONS)
def test_a_reserved_name_cannot_be_raised(name: str, branch: type):
    """The bead: reserved names are NOT members, so nothing can raise them yet."""
    with pytest.raises(TypeError):
        branch(name, 'detail')
