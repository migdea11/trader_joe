"""DERIVE a property of a Pydantic contract model's fields, so a sweep over them cannot fall behind.

WHY THIS EXISTS. ``schemas/tests/test_fetch_dataset_contract.py`` sweeps every aware-datetime field of
the fetch twins and proves a naive value is refused at that field. The sweep was a HAND-WRITTEN list, and
it had already fallen behind by one: ``FetchDatasetRequest.end``, the one optional time field, was never
in it (architect, tj-3mk3u5.29 item 6, 12:46 UTC 2026-10-03). Nothing was red, and nothing would have
gone red if a later edit had widened that annotation to ``datetime | None`` -- the field-parity pin next
to it compares NAMES, not annotations.

So the sweep is DERIVED from the models instead, and the hand list survives only as the payloads, with
the derived set asserting that the two agree. A new aware field then has no way to arrive without a case.

This module is a HELPER, not a test file: it declares no test, and it is imported the way
``common/tests/proto_descriptors.py`` is -- by ``schemas/tests`` as well as by ``common/tests``, because
the same derivation gates the offsets round trip in ``common/tests/rpc``.
"""

from typing import get_args

from pydantic import AwareDatetime, BaseModel


def _annotation_members(annotation: object) -> set[object]:
    # Every type that appears anywhere in the annotation, the annotation itself included. `X | None`
    # must count as X: the one field this was written for is optional, and an optional time field is
    # exactly where awareness is easiest to drop unnoticed (its absence is already meaningful).
    members = {annotation}
    for argument in get_args(annotation):
        members |= _annotation_members(argument)
    return members


def aware_datetime_fields(model: type[BaseModel]) -> frozenset[str]:
    """The model's own fields whose annotation reaches ``pydantic.AwareDatetime``, optional ones included.

    DIRECT FIELDS ONLY: it does not descend into a nested model, because each twin on this contract is
    swept in its own right and a nested one would then be swept twice under two names.

    Args:
        model: A Pydantic model.

    Returns:
        frozenset[str]: The field names. A field widened from ``AwareDatetime`` to ``datetime`` drops out,
        which is the mutation this derivation exists to turn red.
    """
    return frozenset(
        name for name, field in model.model_fields.items() if AwareDatetime in _annotation_members(field.annotation)
    )
