"""Alpaca's asset records, read RAW off the trading API and sliced into broker-neutral Assets.

READ THE LIST AS SERVED, NOT THROUGH THE SDK MODEL (tj-7zjov1.22, the P27a s1 facts). alpaca-py 0.44.0 declares
Asset.exchange as a closed enum without TXSE, and the active list holds TXSE rows, so parsing it raises on the whole
list; a bump does not remove the hazard, because any new venue would break a closed enum again. The body is decoded
here as plain JSON and each element's own bytes are kept as the record's raw_payload.

NOTHING IS INTERPRETED. The neutral fields are copies of what the vendor said: active is the vendor's status equal
to 'active', tradable is its flag, exchange and name are its strings as given (an empty name stays empty). A field the
vendor did not give is None or empty, never false. easy_to_borrow, borrow_status and every other key stay in the raw
payload only. No cusip is read: the vendor serves none on an asset record.

THE LIST IS GUARDED (tj-7zjov1.218). parse_asset_list checks the body's envelope, every record's documented required
fields and the vendor ids' uniqueness, and refuses the whole list with AssetListMalformed on any violation. Types and
the requested class and status are checked; exchange values, attribute values and unknown keys never are. The
by-symbol lookup keeps its own, looser rules.
"""

import json
import uuid
from json import JSONDecodeError

from data.ingest.app.brokers.interface import Asset


_ACTIVE_STATUS = 'active'
_WHITESPACE = ' \t\r\n'

# The checks of the list read's body, record and list arms (tj-7zjov1.218 D3). Fixed words: they reach the run history.
CHECK_ENVELOPE = 'G4 body is one UTF-8 JSON array'
CHECK_RECORD_OBJECT = 'G7 element is a JSON object'
CHECK_RECORD_FIELDS = 'G5 record fields'
CHECK_DUPLICATE_ID = 'G6 duplicate vendor id'

_STRING_FIELDS = ('symbol', 'exchange')
_BOOLEAN_FIELDS = ('tradable', 'marginable', 'shortable', 'fractionable')


class AssetListMalformed(ValueError):
    """The vendor's asset list failed an integrity check and is refused whole (tj-7zjov1.218).

    Attributes:
        check (str): Which check failed, a fixed phrase naming its G number.
        count (int): How many records the check counted: those that violated it, or for the envelope the records read
            before the body stopped being a well-formed array. Never a vendor value.
    """

    def __init__(self, check: str, count: int) -> None:
        self.check = check
        self.count = count
        super().__init__(f'{check} ({count})')


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


def parse_asset_list(content: bytes, status: str, asset_class: str) -> list[Asset]:
    """Parse the answer of the list read: a JSON array, each element sliced out byte for byte.

    One decoding pass: raw_decode gives each element and where it ends, so the slice between is the element's
    own text, not a re-serialisation. Every violation of G4 to G7 raises AssetListMalformed and the list is refused
    whole. A duplicate symbol is not a violation; a duplicate vendor id is.

    Args:
        content (bytes): The response body as served.
        status (str): The status list that was requested; every record must carry it.
        asset_class (str): The asset class that was requested; every record must carry it.

    Returns:
        list[Asset]: The assets in the order served; empty for an empty array.

    Raises:
        AssetListMalformed: If the body is not UTF-8 JSON, or not one array of JSON objects with only whitespace
            after its closing bracket, or a record lacks a documented field or carries the wrong type, class or
            status, or two records share a vendor id.
    """
    try:
        text = content.decode('utf-8')
    except UnicodeDecodeError:
        raise AssetListMalformed(CHECK_ENVELOPE, 0) from None
    decoder = json.JSONDecoder()
    length = len(text)
    position = _skip(text, 0)
    if position >= length or text[position] != '[':
        raise AssetListMalformed(CHECK_ENVELOPE, 0)
    position = _skip(text, position + 1)
    if position < length and text[position] == ']':
        _require_end(text, position + 1, 0)
        return []
    records: list[tuple[object, bytes]] = []
    while True:
        try:
            record, end = decoder.raw_decode(text, position)
        except JSONDecodeError:
            raise AssetListMalformed(CHECK_ENVELOPE, len(records)) from None
        records.append((record, text[position:end].encode('utf-8')))
        position = _skip(text, end)
        if position < length and text[position] == ',':
            position = _skip(text, position + 1)
            continue
        if position < length and text[position] == ']':
            _require_end(text, position + 1, len(records))
            break
        raise AssetListMalformed(CHECK_ENVELOPE, len(records))
    not_objects = sum(1 for record, _ in records if not isinstance(record, dict))
    if not_objects:
        raise AssetListMalformed(CHECK_RECORD_OBJECT, not_objects)
    malformed = sum(1 for record, _ in records if not _record_is_well_formed(record, status, asset_class))
    if malformed:
        raise AssetListMalformed(CHECK_RECORD_FIELDS, malformed)
    ids = [record['id'] for record, _ in records if isinstance(record, dict)]
    duplicates = len(ids) - len(set(ids))
    if duplicates:
        raise AssetListMalformed(CHECK_DUPLICATE_ID, duplicates)
    return [asset_from_record(record, raw) for record, raw in records]


def _record_is_well_formed(record: object, status: str, asset_class: str) -> bool:
    """Check one list element against the documented required fields and their types (G5); values are not judged.

    The one exception is the id, which must be a UUID in canonical form (tj-7zjov1.225).

    Only the requested class and the requested status are compared, because the request itself fixed them.
    """
    if not isinstance(record, dict):
        return False
    vendor_asset_id = record.get('id')
    if not isinstance(vendor_asset_id, str):
        return False
    if not _is_canonical_uuid(vendor_asset_id):
        return False
    if any(not isinstance(record.get(field), str) or not record.get(field) for field in _STRING_FIELDS):
        return False
    if not isinstance(record.get('name'), str):
        return False
    if record.get('class') != asset_class or record.get('status') != status:
        return False
    return all(isinstance(record.get(field), bool) for field in _BOOLEAN_FIELDS)


def _is_canonical_uuid(value: str) -> bool:
    """Return True only for the canonical spelling: lowercase, hyphenated 8-4-4-4-12 (tj-7zjov1.225).

    The store keys listings by the id string as served, so a changed spelling would read as every listing vanishing
    and reappearing. uuid.UUID alone also accepts upper case, braces, a urn:uuid: prefix and no hyphens.
    """
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _require_end(text: str, position: int, records: int) -> None:
    """Raise unless only whitespace follows position, so a list cut short by trailing content cannot pass."""
    if _skip(text, position) < len(text):
        raise AssetListMalformed(CHECK_ENVELOPE, records)


def _skip(text: str, position: int) -> int:
    """Return the index of the first non-whitespace character at or after position."""
    while position < len(text) and text[position] in _WHITESPACE:
        position += 1
    return position
