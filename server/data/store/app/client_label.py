"""THE READER'S LABEL on a dataset read, and the client kind derived from it (tj-grna9p.131 OPTION R).

A read carries no principal. The label is the existing dataset `owner` string the caller already uses, sent in
the X-Client-Id request header on a bar read. It is self-asserted, so it is a LABEL for usage telemetry and never
an authority: nothing may be allowed or refused on it.

THE `owner` QUERY FILTER ON THE /store GETs IS A SEARCH FILTER ('datasets of owner X') AND IS NEVER THE LABEL.

THE VALUE IS NEVER LOGGED OR REPRESENTED. ClientLabel.client_id is repr=False, and a refused header is reported
by its reason and length only. The header is read only on the read routes that record usage; a write route does
not accept or consult it.

RESERVED VALUES begin with '_' and are produced here or by the recorder, never accepted from a caller:
UNATTRIBUTED (missing, empty, over-long or malformed) and OVERFLOW (past the per-dataset-per-day id cap,
applied by the recorder at flush).
"""

from dataclasses import dataclass, field
from typing import Final

from common.logging import get_logger
from data.store.app.database.models.dataset_read import ReadClientKind


log = get_logger(__name__)

# The request header a bar read carries its label in.
CLIENT_ID_HEADER: Final = 'X-Client-Id'

# THE OPERATOR'S LABEL. The UI sends it; a read whose label equals it is a UI read, everything else an SDK read.
# The one definition: the /ui/v1 write beads reuse it for the actor they record.
OPERATOR_PRINCIPAL: Final = 'operator'

UNATTRIBUTED: Final = '_unattributed'
OVERFLOW: Final = '_overflow'
RESERVED_PREFIX: Final = '_'

MAX_CLIENT_ID_LENGTH: Final = 128


@dataclass(frozen=True)
class ClientLabel:
    """A read's label and its kind. client_id is repr=False: it is never logged."""

    client_id: str = field(repr=False)
    client_kind: ReadClientKind


UNATTRIBUTED_LABEL: Final = ClientLabel(UNATTRIBUTED, ReadClientKind.SDK)


def _has_control_character(value: str) -> bool:
    return any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in value)


def classify_client_label(raw: str | None) -> ClientLabel:
    """The label for a header value: itself when valid, else UNATTRIBUTED. Never truncates, never raises.

    Valid is 1..128 characters, no control characters, not starting with '_' (reserved). The kind is UI iff the
    value equals OPERATOR_PRINCIPAL, and SDK otherwise, reserved values included.

    Args:
        raw: The X-Client-Id header value, or None when it was absent.

    Returns:
        ClientLabel: The label to record.
    """
    if raw is None:
        return UNATTRIBUTED_LABEL
    reason = None
    if not raw:
        reason = 'empty'
    elif len(raw) > MAX_CLIENT_ID_LENGTH:
        reason = 'too long'
    elif _has_control_character(raw):
        reason = 'control character'
    elif raw.startswith(RESERVED_PREFIX):
        reason = 'reserved prefix'
    if reason is not None:
        # The reason and the length only, never the value (D8).
        log.debug(f'Refused a client label: {reason}, length {len(raw)}')
        return UNATTRIBUTED_LABEL
    kind = ReadClientKind.UI if raw == OPERATOR_PRINCIPAL else ReadClientKind.SDK
    return ClientLabel(raw, kind)
