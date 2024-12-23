

from datetime import datetime

from common.enums.data_stock import ExpiryType, Granularity


def expiry_inc(expiry: datetime, expiry_type: ExpiryType, granularity: Granularity) -> datetime:
    """
    Calculate the next expiry date based on the expiry type.
    """
    if expiry is None:
        return None
    if expiry_type == ExpiryType.ROLLING:
        return expiry + granularity.offset
    return expiry
