"""A real ui/v1 message for the proto JSON edge's tests (tj-grna9p.15), and the one place they import gen/ from.

routers.common.proto_json takes any protobuf Message, but what it exists for is the generated ui/v1 messages, so
the tests drive it with one: DatasetSummary, from `make proto`'s gen/proto/python (never committed; every pytest
target generates it first). It carries each canonical JSON rule the ADR names (tj-grna9p.4 section 2): a
snake_case field, an enum, an int64 and a Timestamp.

THE ONE TID251 EXEMPTION IN THE TESTS, and it is here on purpose. The seam bans trader_joe.proto outside
common/rpc so that production code reaches generated symbols through the hand-written API there. A test of the
helper that serves those symbols needs a real one, and nothing in common/rpc hands a ui/v1 message out yet (the
mapping arrives with the /ui/v1 routes, tj-grna9p.20). The suppression is named on the import line, in one file,
so it stays a single greppable site rather than a dynamic import that hides from the linter. Not a test module:
test_proto_json.py and test_interface_surface.py import it.
"""

from datetime import UTC, datetime
from typing import Final

from trader_joe.proto.market.v1 import enums_pb2  # noqa: TID251 -- see the module docstring
from trader_joe.proto.ui.v1 import data_pb2  # noqa: TID251 -- see the module docstring


Summary = data_pb2.DatasetSummary

# The full proto name x-proto-message must carry, written out rather than read from the descriptor, so a test
# that compares against it is not comparing the descriptor with itself.
SUMMARY_FULL_NAME: Final = 'trader_joe.proto.ui.v1.DatasetSummary'

# Over 2**32, so a renderer that wrote int64 as a JSON number would still be exact, and only the string rule tells.
BAR_COUNT: Final = 2**40 + 1
START: Final = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)


def summary() -> data_pb2.DatasetSummary:
    """Build the fixture message: a snake_case string field, an enum, an int64 and a Timestamp all set.

    Returns:
        data_pb2.DatasetSummary: The message.
    """
    message = data_pb2.DatasetSummary(
        id='ds-1', asset_symbol='AAPL', asset_type=enums_pb2.ASSET_TYPE_STOCK, bar_count=BAR_COUNT
    )
    message.start.FromDatetime(START)
    return message
