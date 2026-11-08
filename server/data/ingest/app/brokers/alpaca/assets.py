"""Alpaca's asset records, read RAW off the trading API and sliced into broker-neutral Assets.

READ THE LIST AS SERVED, NOT THROUGH THE SDK MODEL (tj-7zjov1.22, the P27a s1 facts). alpaca-py 0.44.0 declares
Asset.exchange as a closed enum without TXSE, and the active list holds TXSE rows, so parsing it raises on the whole
list; a bump does not remove the hazard, because any new venue would break a closed enum again. The body is decoded
here as plain JSON and each element's own bytes are kept as the record's raw_payload.

NOTHING IS INTERPRETED. The neutral fields are copies of what the vendor said: active is the vendor's status equal
to 'active', tradable is its flag, exchange and name are its strings as given (an empty name stays empty). A field the
vendor did not give is None or empty, never false. easy_to_borrow, borrow_status and every other key stay in the raw
payload only. No cusip is read: the vendor serves none on an asset record.
"""

import json
from json import JSONDecodeError

from data.ingest.app.brokers.interface import Asset


_ACTIVE_STATUS = 'active'
_WHITESPACE = ' \t\r\n'


def asset_from_record(record: object, raw: bytes) -> Asset:
    """Build an Asset from one decoded vendor record and its bytes.

    Args:
        record (object): The decoded JSON element.
        raw (bytes): That element's bytes exactly as served.

    Returns:
        Asset: The record with its neutral fields filled from what the vendor gave.

    Raises:
        ValueError: If the element is not an object or carries no string id or symbol, which is vendor corruption
            and never a guessed record. The message holds no vendor body.
    """
    if not isinstance(record, dict):
        raise ValueError('The vendor answered an asset record that is not a JSON object')
    vendor_asset_id = record.get('id')
    symbol = record.get('symbol')
    if not isinstance(vendor_asset_id, str) or not vendor_asset_id or not isinstance(symbol, str) or not symbol:
        raise ValueError('The vendor answered an asset record without a string id and symbol')
    status = record.get('status')
    tradable = record.get('tradable')
    exchange = record.get('exchange')
    name = record.get('name')
    return Asset(
        vendor_asset_id=vendor_asset_id,
        symbol=symbol,
        raw_payload=raw,
        active=(status == _ACTIVE_STATUS) if isinstance(status, str) else None,
        tradable=tradable if isinstance(tradable, bool) else None,
        exchange=exchange if isinstance(exchange, str) else '',
        name=name if isinstance(name, str) else '',
    )


def parse_asset(content: bytes) -> Asset:
    """Parse the answer of the by-symbol read: one record, kept whole as its raw payload.

    Args:
        content (bytes): The response body as served.

    Returns:
        Asset: The record.

    Raises:
        ValueError: If the body is not UTF-8 JSON of one asset record.
    """
    try:
        record = json.loads(content.decode('utf-8'))
    except (UnicodeDecodeError, JSONDecodeError):
        raise ValueError('The vendor answered an asset that is not UTF-8 JSON') from None
    return asset_from_record(record, content)


def parse_asset_list(content: bytes) -> list[Asset]:
    """Parse the answer of the list read: a JSON array, each element sliced out byte for byte.

    One decoding pass: raw_decode gives each element and where it ends, so the slice between is the element's
    own text, not a re-serialisation.

    Args:
        content (bytes): The response body as served.

    Returns:
        list[Asset]: The assets in the order served; empty for an empty array.

    Raises:
        ValueError: If the body is not UTF-8 JSON, or not one array of asset records with only whitespace after
            its closing bracket.
    """
    try:
        text = content.decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError('The vendor answered an asset list that is not UTF-8') from None
    decoder = json.JSONDecoder()
    length = len(text)
    position = _skip(text, 0)
    if position >= length or text[position] != '[':
        raise ValueError('The vendor answered an asset list that is not a JSON array')
    position = _skip(text, position + 1)
    assets: list[Asset] = []
    if position < length and text[position] == ']':
        _require_end(text, position + 1)
        return assets
    while True:
        try:
            record, end = decoder.raw_decode(text, position)
        except JSONDecodeError:
            raise ValueError('The vendor answered an asset list that is not valid JSON') from None
        assets.append(asset_from_record(record, text[position:end].encode('utf-8')))
        position = _skip(text, end)
        if position < length and text[position] == ',':
            position = _skip(text, position + 1)
            continue
        if position < length and text[position] == ']':
            _require_end(text, position + 1)
            return assets
        raise ValueError('The vendor answered an asset list that is not valid JSON')


def _require_end(text: str, position: int) -> None:
    """Raise unless only whitespace follows position, so a list cut short by trailing content cannot pass."""
    if _skip(text, position) < len(text):
        raise ValueError('The vendor answered an asset list that is not valid JSON')


def _skip(text: str, position: int) -> int:
    """Return the index of the first non-whitespace character at or after position."""
    while position < len(text) and text[position] in _WHITESPACE:
        position += 1
    return position
