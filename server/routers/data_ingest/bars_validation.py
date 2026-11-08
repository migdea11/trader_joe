"""What is knowable about a bars request before any vendor call, shared by FetchDataset and Submit.

An ack is a promise the request is viable, so nothing after the ack may fail for a reason that was already
decidable here (tj-3mk3u5.9). Both paths call this before they resolve a feed or touch a reader.
"""

from collections.abc import Sequence

from common.enums.data_select import AssetType, DataType
from common.errors.vocabulary import InvalidRequestError, Reason


def validate_bars_request(data_types: Sequence[DataType], asset_type: AssetType) -> None:
    """Refuse a bars request this service cannot serve, before the ack.

    Args:
        data_types (Sequence[DataType]): What the request asks for.
        asset_type (AssetType): The kind of instrument.

    Raises:
        InvalidRequestError: UNSUPPORTED_ASSET_TYPE for QUOTE or TRADE (still out of scope; bars only) or a
            non-STOCK asset_type (tj-msd6qo; stock only, independent of which reader is installed), or
            INVALID_REQUEST for an empty data_types.
    """
    if not data_types:
        raise InvalidRequestError(Reason.INVALID_REQUEST, 'data_types must not be empty')
    if DataType.QUOTE in data_types:
        raise InvalidRequestError(Reason.UNSUPPORTED_ASSET_TYPE, 'Quotes are not supported yet: data_type=QUOTE')
    if DataType.TRADE in data_types:
        raise InvalidRequestError(Reason.UNSUPPORTED_ASSET_TYPE, 'Trades are not supported yet: data_type=TRADE')
    if asset_type is not AssetType.STOCK:
        # tj-msd6qo: the Kafka path refused crypto and option at METHOD DISPATCH, before any reader saw the
        # request. Checked here rather than left to the reader: BrokerRead's contract does not require a reader
        # to refuse an asset_type it does not serve (AlpacaRead happens to; FakeRead does not, deliberately), so
        # leaving this to the reader would make the answer depend on which reader is installed.
        raise InvalidRequestError(
            Reason.UNSUPPORTED_ASSET_TYPE,
            f'{asset_type.name.capitalize()} datasets are not supported yet: asset_type={asset_type.name}',
        )
