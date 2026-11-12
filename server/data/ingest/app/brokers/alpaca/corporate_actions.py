"""Alpaca's corporate actions, read RAW off the data API and sliced into broker-neutral CorporateActions.

READ THE BODY AS SERVED, NOT THROUGH THE SDK MODEL (tj-7zjov1.23; the same hazard as the asset list, assets.py). A
page is {"corporate_actions": {"<group>": [record, ...], ...}, "next_page_token": <string or null>}, the records
grouped by the vendor's own type wording. The body is walked here as plain JSON and each record's own bytes are kept as
its raw_payload, never a re-serialisation, so what was published is what is stored.

NOTHING IS INTERPRETED AND NOTHING IS FILTERED. The group name is the action_type as the vendor worded it. The id is
the vendor_action_id. The five dates the contract names are copied when the vendor gave them (a 'YYYY-MM-DD' day, read as
midnight UTC) and are None otherwise. Every string field that names an instrument is kept in identifiers under the
vendor's own field name: the fields `symbol`, `cusip` and `isin` and every field ending `_symbol`, `_cusip` or `_isin`
(old_symbol, new_cusip, acquirer_symbol, acquiree_cusip, source_symbol and so on). Everything else stays in the raw
payload only. A record of a type nobody here has heard of is carried like any other.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from json import JSONDecodeError
from typing import Final

from data.ingest.app.brokers.interface import CorporateAction


_WHITESPACE: Final = ' \t\r\n'
_IDENTIFIER_FIELDS: Final = frozenset({'symbol', 'cusip', 'isin'})
_IDENTIFIER_SUFFIXES: Final = ('_symbol', '_cusip', '_isin')
_ACTIONS_KEY: Final = 'corporate_actions'
_TOKEN_KEY: Final = 'next_page_token'
_DAY_SHAPE: Final = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')
_NOT_VALID: Final = 'The vendor answered a corporate-actions page that is not valid JSON of the expected shape'


@dataclass(frozen=True)
class CorporateActionsPage:
    """One decoded page: its actions in the order served and the token of the next page.

    Attributes:
        actions (list[CorporateAction]): The page's records, group by group in the order served.
        next_page_token (str | None): The vendor's token for the next page, or None when this is the last.
    """

    actions: list[CorporateAction]
    next_page_token: str | None


def action_from_record(group: str, record: object, raw: bytes) -> CorporateAction:
    """Build a CorporateAction from one decoded vendor record, the group it was served under and its bytes.

    Args:
        group (str): The vendor's group name for the record, kept as its action_type.
        record (object): The decoded JSON element.
        raw (bytes): That element's bytes exactly as served.

    Returns:
        CorporateAction: The record with its neutral fields filled from what the vendor gave.

    Raises:
        ValueError: If the element is not an object, carries no non-empty string id, or carries a date field that is
            not a 'YYYY-MM-DD' day, which is vendor corruption and never a guessed record. The message holds no vendor
            body.
    """
    if not isinstance(record, dict):
        raise ValueError('The vendor answered a corporate action that is not a JSON object')
    vendor_action_id = record.get('id')
    if not isinstance(vendor_action_id, str) or not vendor_action_id:
        raise ValueError('The vendor answered a corporate action without a string id')
    return CorporateAction(
        vendor_action_id=vendor_action_id,
        action_type=group,
        raw_payload=raw,
        ex_date=_day(record, 'ex_date'),
        record_date=_day(record, 'record_date'),
        payable_date=_day(record, 'payable_date'),
        process_date=_day(record, 'process_date'),
        effective_date=_day(record, 'effective_date'),
        identifiers={
            name: value
            for name, value in record.items()
            if isinstance(value, str) and value and _is_identifier_field(name)
        },
    )


def parse_corporate_actions_page(content: bytes) -> CorporateActionsPage:
    """Parse one page of the corporate-actions answer, each record sliced out byte for byte.

    Args:
        content (bytes): The response body as served.

    Returns:
        CorporateActionsPage: The records in the order served (empty when the vendor listed none) and the next page
            token, or None when the vendor gave none or null.

    Raises:
        ValueError: If the body is not UTF-8 JSON of one object whose corporate_actions member is an object of arrays
            (or null) of record objects, whose token is not a string or null, or that has anything but whitespace after
            the closing brace.
    """
    try:
        text = content.decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError('The vendor answered a corporate-actions page that is not UTF-8') from None
    actions: list[CorporateAction] = []
    token: list[str | None] = [None]

    def top(key: str, start: int) -> int:
        if key == _ACTIONS_KEY:
            if text[start : start + 1] == '{':
                return _walk_object(text, start, group_of)
            return _skip_null(text, start)
        if key == _TOKEN_KEY:
            value, end = _decode(text, start)
            if value is not None and not isinstance(value, str):
                raise ValueError(_NOT_VALID)
            token[0] = value if value else None
            return end
        return _decode(text, start)[1]

    def group_of(group: str, start: int) -> int:
        if text[start : start + 1] != '[':
            return _skip_null(text, start)

        def element(position: int) -> int:
            record, end = _decode(text, position)
            actions.append(action_from_record(group, record, text[position:end].encode('utf-8')))
            return end

        return _walk_array(text, start, element)

    position = _skip(text, 0)
    if text[position : position + 1] != '{':
        raise ValueError(_NOT_VALID)
    end = _walk_object(text, position, top)
    if _skip(text, end) < len(text):
        raise ValueError(_NOT_VALID)
    return CorporateActionsPage(actions, token[0])


def _is_identifier_field(name: str) -> bool:
    """Report whether a vendor field names an instrument: symbol, cusip, isin, or a role-named one ending so."""
    return name in _IDENTIFIER_FIELDS or name.endswith(_IDENTIFIER_SUFFIXES)


def _day(record: dict, name: str) -> datetime | None:
    """Read a vendor day field as midnight UTC, None when absent or null."""
    value = record.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f'The vendor answered a corporate action whose {name} is not a string')
    try:
        # fromisoformat also takes 20220214 and 2022-W07-1, so the shape is checked first. fullmatch, ASCII digits only.
        if _DAY_SHAPE.fullmatch(value) is None:
            raise ValueError(value)
        day = date.fromisoformat(value)
    except ValueError:
        raise ValueError(f'The vendor answered a corporate action whose {name} is not a YYYY-MM-DD day') from None
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _decode(text: str, position: int) -> tuple[object, int]:
    """Decode the JSON value starting at position and give it with the index just after it."""
    try:
        return json.JSONDecoder().raw_decode(text, position)
    except JSONDecodeError:
        raise ValueError(_NOT_VALID) from None


def _skip_null(text: str, position: int) -> int:
    """Pass a JSON null at position and return the index after it; anything else is the wrong shape."""
    value, end = _decode(text, position)
    if value is not None:
        raise ValueError(_NOT_VALID)
    return end


def _walk_object(text: str, position: int, visit: Callable[[str, int], int]) -> int:
    """Walk the JSON object at position, calling visit(key, value_start) for each member.

    Args:
        text (str): The whole body.
        position (int): The index of the object's opening brace.
        visit (Callable[[str, int], int]): Consumes the member's value and returns the index just after it.

    Returns:
        int: The index just after the closing brace.
    """
    position = _skip(text, position + 1)
    if text[position : position + 1] == '}':
        return position + 1
    while True:
        key, after_key = _decode(text, position)
        if not isinstance(key, str):
            raise ValueError(_NOT_VALID)
        colon = _skip(text, after_key)
        if text[colon : colon + 1] != ':':
            raise ValueError(_NOT_VALID)
        position = _skip(text, visit(key, _skip(text, colon + 1)))
        mark = text[position : position + 1]
        if mark == ',':
            position = _skip(text, position + 1)
            continue
        if mark == '}':
            return position + 1
        raise ValueError(_NOT_VALID)


def _walk_array(text: str, position: int, visit: Callable[[int], int]) -> int:
    """Walk the JSON array at position, calling visit(element_start) for each element.

    Args:
        text (str): The whole body.
        position (int): The index of the array's opening bracket.
        visit (Callable[[int], int]): Consumes the element and returns the index just after it.

    Returns:
        int: The index just after the closing bracket.
    """
    position = _skip(text, position + 1)
    if text[position : position + 1] == ']':
        return position + 1
    while True:
        position = _skip(text, visit(position))
        mark = text[position : position + 1]
        if mark == ',':
            position = _skip(text, position + 1)
            continue
        if mark == ']':
            return position + 1
        raise ValueError(_NOT_VALID)


def _skip(text: str, position: int) -> int:
    """Return the index of the first non-whitespace character at or after position."""
    while position < len(text) and text[position] in _WHITESPACE:
        position += 1
    return position
