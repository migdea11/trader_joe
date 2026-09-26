from datetime import datetime
from uuid import UUID

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from schemas.inbound_contract import InboundContract


class BaseGetDatasetRequest(InboundContract):
    dataset_id: UUID
    # Carried through from the store request so the fetch knows which principal it is acting for
    # and which tape to ask the vendor for. owner is identity on the entry (tj-vhboky.1 section 2)
    # and may not be invented here.
    owner: str
    source: DataSource
    # OPTIONAL, AND THIS IS THE ONLY MODEL WHERE THAT OPTIONALITY IS MEANT TO END. None is the
    # caller declining to choose a tape, forwarded unchanged from the store body; it is the
    # ADAPTER's job to turn it into a concrete Feed -- the caller's selection when there is one,
    # otherwise the constant for a vendor with a single tape -- and an adapter that cannot is a
    # failure, not a row with a placeholder in it (tj-vhboky.1, ruling of 2026-09-25).
    # It cannot be resolved from the vendor's answer: Alpaca's bars response has no feed field at
    # any level, so the ruling's middle branch does not exist for the one vendor there is.
    feed: Feed | None = None

    granularity: Granularity
    start: datetime
    end: datetime | None

    expiry: datetime
    expiry_type: ExpiryType
    update_type: UpdateType


class GetDatasetRequest(BaseGetDatasetRequest):
    asset_symbol: str
    asset_type: AssetType
    data_types: list[DataType]


class StockDatasetRequest(GetDatasetRequest):
    # Adding path params except asset_type
    asset_symbol: str
    data_types: list[DataType]

    # This carried `extra = 'ignore'` with the comment "ignore asset_type", which never did that:
    # asset_type is a DECLARED field inherited from GetDatasetRequest, so it was already accepted
    # and the override only ever relaxed the model against genuinely unknown keys. It is removed
    # rather than repaired -- the router builds this from GetDatasetRequest.model_dump(), whose
    # keys are exactly this model's fields, so there is nothing for it to have been ignoring.


class CryptoDatasetRequest(StockDatasetRequest):
    pass


class OptionDatasetRequest(StockDatasetRequest):
    pass
