"""Superset enums assembled from narrower ones, and the conversion back down.

THE SHAPE THIS EXISTS FOR (tj-vhboky.1, the user's per-market ruling). One vocabulary is stored
and transported -- the SUPERSET -- while callers reason in the narrow vocabulary that applies to
them, a SUBSET. The superset's member list is never hand-written: it is composed from its subsets
at import time, so adding a market means adding one subset enum and one name to a compose() call,
and no list anywhere has to be kept in step by hand.

WHY THE MEMBER LIST MUST NOT BE HAND-MAINTAINED, stated because the cheap version looks fine for
exactly as long as there is one subset: two lists that must agree can agree perfectly while both
are wrong, and nothing detects the day one of them is edited alone. Composing removes the second
list rather than testing it.

This generalises the pattern already in common/kafka/topics.py, where StaticTopic is extended from
RpcEndpointTopic by a module-level extend_enum loop and RpcEndpointTopic.request/.response convert
upward by constructing StaticTopic(...). That module is NOT retrofitted onto this base -- see
compose() for why -- but it is where the pattern was proven.

WHAT A COMPOSED SUPERSET IS SAFE FOR, verified rather than assumed:
  * aenum.Enum subclasses stdlib enum.Enum, so Pydantic, FastAPI and OpenAPI treat a composed
    superset as an ordinary string enum: an unrecognised value is a 422, and the generated schema
    carries the full composed member list.
  * SQLAlchemy's sa.Enum(<superset>, name=...) reads the member list off the class at the moment
    the column is declared. Composition happens at import time, before any model or migration
    module body runs, so the database type is built from the composed list and cannot drift from
    the Python one. The migration MUST pass the class, never a literal list of strings.
"""

from enum import Enum, StrEnum
from typing import Self

from aenum import Enum as AEnum
from aenum import extend_enum


class SupersetEnum(AEnum):
    """Base for an enum whose members are contributed by other enums rather than declared.

    Subclasses declare NO members of their own. They call compose() at module level, once, with
    every subset that feeds them.
    """

    @classmethod
    def compose(cls, *subsets: type[Enum]) -> None:
        """Add every member of each subset to this superset.

        Re-contributing a member that is already present is allowed and does nothing, so a value
        shared by several subsets -- NOT_APPLICABLE across markets, say -- may be declared in each
        of them without the superset caring which one arrived first.

        Args:
            *subsets (type[Enum]): The enums whose members become members of this superset.

        Raises:
            ValueError: Two subsets gave the same member NAME with different VALUES. That is a
                genuine collision: one name would have to mean two things in the stored
                vocabulary, and silently keeping the first would make the meaning depend on
                import order.
        """
        for subset in subsets:
            for member in subset:
                existing = cls.__members__.get(member.name)
                if existing is not None:
                    if existing.value != member.value:
                        raise ValueError(
                            f'{cls.__name__} cannot compose {subset.__name__}.{member.name}='
                            f'{member.value!r}: the name is already contributed with value '
                            f'{existing.value!r}.'
                        )
                    continue
                extend_enum(cls, member.name, member.value)


class StrSupersetEnum(str, SupersetEnum):
    """A SupersetEnum whose members are also strings, for values that cross a wire or a column.

    __str__ IS NOT DECORATION. A (str, Enum) mixin is not a StrEnum: without this line
    str(Member) and f'{Member}' both return 'ClassName.MEMBER', not the value. That is the shape
    of bug that reaches a vendor query string or a log line and is only noticed downstream. The
    stdlib StrEnum cannot be used instead because aenum's extend_enum needs the aenum base.
    """

    __str__ = str.__str__


class SubsetStrEnum(StrEnum):
    """Base for a narrow enum that contributes its members to a superset.

    Members are declared normally. What this base adds is the downward conversion, which is the
    half that can fail and therefore the half worth having one implementation of.
    """

    @classmethod
    def from_superset(cls, value: Enum | str) -> Self:
        """Narrow a superset value to this subset.

        The upward direction needs no helper: compose() guarantees every member of this enum is a
        member of the superset, so <Superset>(<subset member>) is total and cannot raise. Downward
        is partial -- that is the point of the subsets -- so it is spelled out here and it RAISES
        rather than returning None, because a caller that got a value outside its vocabulary has
        been handed something it has no correct way to interpret.

        Args:
            value (Enum | str): A superset member, or its bare string value.

        Raises:
            ValueError: The value is not in this subset's vocabulary.

        Returns:
            Self: The matching member of this enum.
        """
        raw = value.value if isinstance(value, Enum) else value
        try:
            return cls(raw)
        except ValueError:
            raise ValueError(
                f'{raw!r} is not a {cls.__name__}. Valid values: {[member.value for member in cls]}.'
            ) from None
