from uuid import UUID

from pydantic import AwareDatetime

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
    # KEPT, BUT INERT TODAY, AND THE COMMENT THAT USED TO SIT HERE OVERSTATED IT. It claimed this
    # was "the only model where that optionality is meant to end" -- that the adapter would take
    # the caller's selection when there is one. NOTHING IN data/ingest READS THIS FIELD. The one
    # resolution site (alpaca/broker_api.py, `'sip' if sip_enabled() else 'iex'`) consults a
    # deployment env var and never the request, so a request naming a tape is accepted and
    # ignored -- exactly the failure schemas/inbound_contract.py exists to prevent, arriving
    # through a field rather than past one.
    #
    # WHY IT SURVIVES ANYWAY, while StoreAssetDatasetBody.feed did not: nothing can populate it
    # now. tj-rh4b7f (2026-09-25) deferred caller-selected feed, so the store body has no feed to
    # forward and data_action_request.py's model_dump() splat leaves this at its default. This is
    # the store->ingest channel the deferred transport work resolves a feed OVER, so it is the
    # designated landing site rather than dead weight. It stays declared and honestly described;
    # it does not stay described as working.
    #
    # It cannot be resolved from the vendor's answer either: Alpaca's bars response has no feed
    # field at any level, so the ruling's middle branch does not exist for the one vendor there is.
    feed: Feed | None = None

    granularity: Granularity
    # AwareDatetime, REFUSE not convert (user ruling D2 = A on tj-vhboky.20, the tj-1bl90i rule).
    # The one in-tree sender, data/store/app/ingest/data_action_request.py, builds this from
    # StoreAssetDatasetBody, whose start/end/expiry are already aware, and the RPC carries it as
    # model_dump_json(), which keeps the offset -- so tightening this receiver breaks no sender.
    start: AwareDatetime
    end: AwareDatetime | None

    expiry: AwareDatetime
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
