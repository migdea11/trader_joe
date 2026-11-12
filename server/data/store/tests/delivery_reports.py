"""Report builders for the delivery intake tests (tj-7zjov1.27, P34). Not a test module.

Every builder returns a report that the intake ACCEPTS for its kind (the green baseline); a test makes one field
wrong with model_copy(update=...), which does not validate, exactly as a crafted report off the wire would arrive.
"""

import uuid
from datetime import UTC, datetime, timedelta

from common.enums.data_stock import Feed
from data.store.app.database.models.ingest_request import IngestRequestKind
from schemas.data_ingest.delivery import (
    AssetRecord,
    AssetRecords,
    AttemptDelivery,
    CorporateActionRecord,
    CorporateActionRecords,
    DeliveryErrorClass,
    DeliveryHeader,
    DeliveryOutcome,
    DeliveryTrailer,
    VendorCallStats,
    VendorReported,
)
from schemas.data_ingest.fetch_dataset import Bar, BarPage, ServedRange


# Sentinels: a test asserts neither ever reaches a log line or a refusal detail.
QUOTA_KEY = 'quota-key-sentinel-91f3'
VENDOR_ASSET_ID = 'vendor-asset-sentinel-5c2e'
INSTANCE_A = 'instance-a'
INSTANCE_B = 'instance-b'

FIRST_CALL = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
LAST_CALL = FIRST_CALL + timedelta(seconds=3)
AS_OF = LAST_CALL + timedelta(seconds=1)
BAR_START = datetime(2026, 2, 2, 5, tzinfo=UTC)

NO_CALL = (DeliveryOutcome.EXPIRED, DeliveryOutcome.EXHAUSTED)
COUNTED = (DeliveryOutcome.SUCCEEDED, DeliveryOutcome.FAILED)
NON_BARS = (IngestRequestKind.ASSET_LIST, IngestRequestKind.CORPORATE_ACTIONS)


def stats(calls: int = 3, pages: int = 2, throttled_429: int = 1, retries: int = 1) -> VendorCallStats:
    return VendorCallStats(
        calls=calls,
        pages=pages,
        throttled_429=throttled_429,
        retries=retries,
        first_call_at=FIRST_CALL,
        last_call_at=LAST_CALL,
        vendor_reported=VendorReported(limit=200, remaining=150, reset_at=AS_OF + timedelta(minutes=1)),
    )


def bar(offset_days: int = 0, feed: Feed = Feed.IEX, **overrides) -> Bar:
    values = {
        'bar_start': BAR_START + timedelta(days=offset_days),
        'open': 10.0,
        'high': 12.0,
        'low': 9.0,
        'close': 11.0,
        'volume': 1000.0,
        'trade_count': 42,
        'feed': feed,
    }
    return Bar(**{**values, **overrides})


def bar_page(*offsets: int, feed: Feed = Feed.IEX) -> BarPage:
    return BarPage(bars=[bar(offset, feed=feed) for offset in offsets])


def asset_records() -> AssetRecords:
    return AssetRecords(records=[AssetRecord(vendor_asset_id='a1', symbol='VFV', raw_payload=b'{}')])


def action_records() -> CorporateActionRecords:
    return CorporateActionRecords(
        records=[CorporateActionRecord(vendor_action_id='c1', action_type='split', raw_payload=b'{}')]
    )


def payload_for(kind: IngestRequestKind):
    return {
        IngestRequestKind.BARS: bar_page(0, 1),
        IngestRequestKind.ASSET_LIST: asset_records(),
        IngestRequestKind.CORPORATE_ACTIONS: action_records(),
    }[kind]


def header(
    request_id: uuid.UUID | str,
    *,
    kind: IngestRequestKind = IngestRequestKind.BARS,
    outcome: DeliveryOutcome = DeliveryOutcome.SUCCEEDED,
    attempt_no: int = 1,
    instance_id: str = INSTANCE_A,
) -> DeliveryHeader:
    counted = outcome in COUNTED
    return DeliveryHeader(
        request_id=str(request_id),
        instance_id=instance_id,
        attempt_no=attempt_no,
        quota_key=QUOTA_KEY if counted else '',
        feed=Feed.IEX if kind is IngestRequestKind.BARS else None,
        vendor_asset_id=VENDOR_ASSET_ID if counted and kind is IngestRequestKind.BARS else '',
    )


def report(
    request_id: uuid.UUID | str,
    outcome: DeliveryOutcome = DeliveryOutcome.SUCCEEDED,
    *,
    kind: IngestRequestKind = IngestRequestKind.BARS,
    attempt_no: int = 1,
    instance_id: str = INSTANCE_A,
    parts: list | None = None,
    error_class: DeliveryErrorClass = DeliveryErrorClass.TRANSIENT,
    last_attempt: bool = False,
    vendor_stats: VendorCallStats | None = None,
    detail: str = '',
) -> AttemptDelivery:
    """A report the intake accepts for its kind and outcome. parts defaults to two bars on a bars success."""
    head = header(request_id, kind=kind, outcome=outcome, attempt_no=attempt_no, instance_id=instance_id)
    if outcome is DeliveryOutcome.SUCCEEDED:
        trailer = DeliveryTrailer(
            outcome=outcome,
            vendor_stats=vendor_stats or stats(),
            served_range=ServedRange(start=BAR_START, end=BAR_START + timedelta(days=5)),
            as_of=AS_OF,
            detail=detail,
        )
        if parts is None:
            parts = [bar_page(0, 1)] if kind is IngestRequestKind.BARS else []
    elif outcome is DeliveryOutcome.FAILED:
        trailer = DeliveryTrailer(
            outcome=outcome,
            error_class=error_class,
            vendor_stats=vendor_stats or stats(),
            as_of=AS_OF,
            last_attempt=last_attempt,
            detail=detail or 'the vendor answered 503',
        )
        parts = parts or []
    else:
        trailer = DeliveryTrailer(outcome=outcome, detail=detail)
        parts = parts or []
    return AttemptDelivery(header=head, parts=parts, trailer=trailer)


def with_header(attempt: AttemptDelivery, **changes) -> AttemptDelivery:
    """The report with header fields replaced, unvalidated, as a crafted report would arrive."""
    return attempt.model_copy(update={'header': attempt.header.model_copy(update=changes)})


def with_trailer(attempt: AttemptDelivery, **changes) -> AttemptDelivery:
    """The report with trailer fields replaced, unvalidated."""
    return attempt.model_copy(update={'trailer': attempt.trailer.model_copy(update=changes)})


def with_parts(attempt: AttemptDelivery, parts: list) -> AttemptDelivery:
    return attempt.model_copy(update={'parts': parts})


# ---------------------------------------------------------------------------------------------
# THE RED CASES: every cross-field rule broken once, from a green baseline. (rule, kind, outcome, how to break it)


def _no_call_cases() -> list[tuple]:
    cases = []
    for outcome in NO_CALL:
        for kind in IngestRequestKind:
            name = f'{outcome.lower()}-{kind.lower()}'
            cases += [
                ('a', f'{name}-vendor-stats', kind, outcome, lambda r: with_trailer(r, vendor_stats=stats())),
                ('a', f'{name}-as-of', kind, outcome, lambda r: with_trailer(r, as_of=AS_OF)),
                (
                    'a',
                    f'{name}-served-range',
                    kind,
                    outcome,
                    lambda r: with_trailer(r, served_range=ServedRange(start=BAR_START, end=AS_OF)),
                ),
                ('a', f'{name}-payload', kind, outcome, lambda r, k=kind: with_parts(r, [payload_for(k)])),
                ('a', f'{name}-quota-key', kind, outcome, lambda r: with_header(r, quota_key=QUOTA_KEY)),
                (
                    'a',
                    f'{name}-vendor-asset-id',
                    kind,
                    outcome,
                    lambda r: with_header(r, vendor_asset_id=VENDOR_ASSET_ID),
                ),
                (
                    'b',
                    f'{name}-error-class',
                    kind,
                    outcome,
                    lambda r: with_trailer(r, error_class=DeliveryErrorClass.PERMANENT),
                ),
                ('c', f'{name}-last-attempt', kind, outcome, lambda r: with_trailer(r, last_attempt=True)),
            ]
    return cases


def _bars_cases() -> list[tuple]:
    bars, ok, failed = IngestRequestKind.BARS, DeliveryOutcome.SUCCEEDED, DeliveryOutcome.FAILED
    cases = [
        ('b', 'succeeded-error-class', bars, ok, lambda r: with_trailer(r, error_class=DeliveryErrorClass.TRANSIENT)),
        ('b', 'failed-no-error-class', bars, failed, lambda r: with_trailer(r, error_class=None)),
        ('b', 'failed-with-payload', bars, failed, lambda r: with_parts(r, [bar_page(0)])),
        ('c', 'succeeded-last-attempt', bars, ok, lambda r: with_trailer(r, last_attempt=True)),
        (
            'd',
            'failed-served-range',
            bars,
            failed,
            lambda r: with_trailer(r, served_range=ServedRange(start=BAR_START, end=AS_OF)),
        ),
        ('g', 'succeeded-empty-quota-key', bars, ok, lambda r: with_header(r, quota_key='')),
        ('g', 'failed-empty-quota-key', bars, failed, lambda r: with_header(r, quota_key='')),
        ('h', 'succeeded-no-vendor-stats', bars, ok, lambda r: with_trailer(r, vendor_stats=None)),
        ('f', 'bars-request-asset-payload', bars, ok, lambda r: with_parts(r, [asset_records()])),
        ('f', 'bars-request-action-payload', bars, ok, lambda r: with_parts(r, [action_records()])),
    ]
    # (e) THE FEED RULE: a bars report of ANY outcome needs a resolved feed (P36b ruling: no tolerance for the
    # interim ingest behaviour of an empty feed on EXPIRED and EXHAUSTED).
    for outcome in DeliveryOutcome:
        cases.append(('e', f'bars-{outcome.lower()}-no-feed', bars, outcome, lambda r: with_header(r, feed=None)))
    # ... and the other kinds carry none, whatever the outcome.
    for kind in NON_BARS:
        for outcome in DeliveryOutcome:
            cases.append(
                ('e', f'{kind.lower()}-{outcome.lower()}-feed', kind, outcome, lambda r: with_header(r, feed=Feed.IEX))
            )
    cases.append(
        (
            'f',
            'asset-list-request-bar-payload',
            IngestRequestKind.ASSET_LIST,
            ok,
            lambda r: with_parts(r, [bar_page(0)]),
        )
    )
    cases.append(
        (
            'f',
            'corporate-actions-request-asset-payload',
            IngestRequestKind.CORPORATE_ACTIONS,
            ok,
            lambda r: with_parts(r, [asset_records()]),
        )
    )
    cases.append(
        (
            'f',
            'corporate-actions-request-bar-payload',
            IngestRequestKind.CORPORATE_ACTIONS,
            ok,
            lambda r: with_parts(r, [bar_page(0)]),
        )
    )
    # (i) A SUCCEEDED corporate-actions report names its served range (tj-7zjov1.31, the 15:14 UTC note), as a bars
    # one does; with records and without.
    for name, parts in (('records', [action_records()]), ('empty', [])):
        cases.append(
            (
                'i',
                f'corporate-actions-succeeded-{name}-no-served-range',
                IngestRequestKind.CORPORATE_ACTIONS,
                ok,
                lambda r, p=parts: with_trailer(with_parts(r, p), served_range=None),
            )
        )
    return cases


RED_CASES: list[tuple] = _no_call_cases() + _bars_cases()
