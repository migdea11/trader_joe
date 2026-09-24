from datetime import datetime
from typing import NamedTuple

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Granularity


class VendorRequestKey(NamedTuple):
    """Content key identifying one vendor request.

    A data request is idempotent by content (tj-84ty47 section 1): the same tuple denotes
    the same answer whoever asks and whenever, so the request is its own key and nothing
    has to be asserted by the caller.

    FEED AND ADJUSTMENT ARE PART OF THE KEY, not metadata. IEX and SIP are different tapes
    and a raw bar is not a split-adjusted one, so leaving either out would collapse two
    requests for different answers into one (tj-84ty47 section 3.1 makes the same point
    about the coverage ledger's series identity).

    Deliberately excluded: dataset_id, expiry, expiry_type and update_type. Those describe
    what THIS caller wants done with the answer, not which answer it is, and folding them
    in would stop identical requests collapsing at all.
    """

    broker: DataSource
    feed: str
    asset_type: AssetType
    asset_symbol: str
    data_type: DataType
    granularity: Granularity
    range_start: datetime
    range_end: datetime | None
    adjustment: str
