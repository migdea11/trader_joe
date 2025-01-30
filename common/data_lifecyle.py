

from datetime import datetime

from common.enums.data_stock import ExpiryType, Granularity


def expiry_inc(expiry: datetime, expiry_type: ExpiryType, granularity: Granularity) -> datetime:
    """Calculate the expiry increment based on the expiry type and granularity.

    Args:
        expiry (datetime): Expiry of previous data point
        expiry_type (ExpiryType): Type of expiry scheme
        granularity (Granularity): Time granularity of the data

    Returns:
        datetime: Expiry for the next data point
    """
    if expiry is None:
        return None
    if expiry_type == ExpiryType.ROLLING:
        return expiry + granularity.offset
    return expiry
