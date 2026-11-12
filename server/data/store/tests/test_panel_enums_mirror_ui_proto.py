"""The store's panel enums and ui/v1 panels.proto name the same members (tj-7zjov1.35, P23).

panels.proto says PanelCadence, PanelTransition and PanelPassKind mirror the store's enums by member NAME, the rule
ExpiryType follows (test_proto_ui_vocabulary.py). The common suite cannot import the store, so the mirror is pinned
here: a member on one side only is a ledger row or a settings version the UI route cannot render, or a value the UI
could send that the store has no word for. Read from protoc's descriptors (common/tests/proto_descriptors.py), never
from the generated package (TID251).
"""

from enum import StrEnum

import pytest

from common.tests.proto_descriptors import file_named
from data.store.app.database.models.panel import PanelCadence, PanelPassKind, PanelTransition


pytestmark = pytest.mark.data_store

PANELS = 'trader_joe/proto/ui/v1/panels.proto'
MIRRORS = [
    ('PanelCadence', 'PANEL_CADENCE_', PanelCadence),
    ('PanelTransition', 'PANEL_TRANSITION_', PanelTransition),
    ('PanelPassKind', 'PANEL_PASS_KIND_', PanelPassKind),
]


@pytest.mark.parametrize(('enum', 'prefix', 'store'), MIRRORS, ids=[name for name, _, _ in MIRRORS])
def test_each_ui_panel_enum_mirrors_the_store_enum_by_member_name(enum: str, prefix: str, store: type[StrEnum]):
    """After the UNSPECIFIED zero, the proto's members are the store's, in the same order.

    Args:
        enum: The ui/v1 enum.
        prefix: Its value prefix.
        store: The store's StrEnum it mirrors.
    """
    values = [value.name for value in {e.name: e for e in file_named(PANELS).enum_type}[enum].value]
    assert values[0] == f'{prefix}UNSPECIFIED'
    assert [value.removeprefix(prefix) for value in values[1:]] == [member.value for member in store]
