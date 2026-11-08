"""The delivery intake's cross-field rules and its verdict rule, as pure functions (tj-7zjov1.27, P34).

THE DESIGN: the P32 gate note on tj-7zjov1.27 (rules a to f: the carrying layer has no cross-field validators,
so the store refuses every contradiction), the P36 gate note (EXHAUSTED keeps EXPIRED's rules), the P36b note
(rule e enforced with NO tolerance for a bars EXPIRED or EXHAUSTED report with an empty feed), ruling 6 and C2 of
the escalation rulings (rule g: a counted attempt names a quota key; rule h: a SUCCEEDED report carries vendor
stats, a FAILED one need not), and the verdict rule of the bead description (FINAL on a permanent refusal, the last
attempt, a superseded request; else CONTINUE; EXPIRED FINAL with cause EXPIRED; EXHAUSTED FINAL with cause
ATTEMPTS_EXHAUSTED). P35 (tj-7zjov1.28): note G's rule (i), a SUCCEEDED bars report must carry a served range (an
asset list need not), and the drop rule's verdict (FINAL, EMPTY, cause DISCARDED).

Each rule is red and green: every red case in delivery_reports.RED_CASES breaks one rule from a baseline that is
asserted green here, so a case can only be red because of the field it changes. That the intake writes NOTHING on
each refusal is test_delivery_intake.py's, over SQLite.
"""

import pytest

from common.enums.data_stock import Feed
from common.errors.vocabulary import InvalidRequestError, Reason
from data.store.app.database.models.ingest_request import (
    IngestRequestFailureCause,
    IngestRequestKind,
    IngestRequestState,
)
from data.store.app.ingest.delivery_intake import decide, validate_report
from data.store.tests.delivery_reports import (
    NO_CALL,
    QUOTA_KEY,
    RED_CASES,
    VENDOR_ASSET_ID,
    report,
    with_header,
    with_trailer,
)
from schemas.data_ingest.delivery import DeliveryErrorClass, DeliveryOutcome, DeliveryVerdict


pytestmark = pytest.mark.data_store

REQUEST_ID = '6f1c2b3a-0d4e-4f5a-9b8c-7d6e5f4a3b2c'


def _refused(attempt, kind) -> InvalidRequestError:
    with pytest.raises(InvalidRequestError) as caught:
        validate_report(attempt, kind)
    return caught.value


# --------------------------------------------------------------------------------------------- green


@pytest.mark.parametrize('kind', list(IngestRequestKind))
@pytest.mark.parametrize('outcome', list(DeliveryOutcome))
def test_the_baseline_report_of_every_kind_and_outcome_is_accepted(kind, outcome):
    """The green half of every rule: a red case below differs from one of these by one field only."""
    validate_report(report(REQUEST_ID, outcome, kind=kind), kind)


@pytest.mark.parametrize('outcome', NO_CALL)
def test_an_expired_or_exhausted_report_with_an_empty_quota_key_is_accepted(outcome):
    """Rule g names counted attempts only: a report that made no call was charged to no quota."""
    attempt = report(REQUEST_ID, outcome)
    assert attempt.header.quota_key == ''
    validate_report(attempt, IngestRequestKind.BARS)


@pytest.mark.parametrize('error_class', list(DeliveryErrorClass))
def test_a_failed_report_without_vendor_stats_is_accepted(error_class):
    """Rule h's other half (ruling 6): a FAILED report with no stats is accepted.

    The attempt runner's crash path sends one, and refusing it would stall that failure for ever.
    """
    attempt = with_trailer(report(REQUEST_ID, DeliveryOutcome.FAILED, error_class=error_class), vendor_stats=None)
    validate_report(attempt, IngestRequestKind.BARS)


@pytest.mark.parametrize('error_class', list(DeliveryErrorClass))
def test_a_failed_report_with_last_attempt_is_accepted(error_class):
    validate_report(
        report(REQUEST_ID, DeliveryOutcome.FAILED, error_class=error_class, last_attempt=True), IngestRequestKind.BARS
    )


def test_a_succeeded_bars_report_with_no_bars_is_accepted():
    validate_report(report(REQUEST_ID, parts=[]), IngestRequestKind.BARS)


# --------------------------------------------------------------------------------------------- red


@pytest.mark.parametrize(
    ('rule', 'kind', 'outcome', 'breaks'),
    [pytest.param(rule, kind, outcome, breaks, id=f'{rule}-{name}') for rule, name, kind, outcome, breaks in RED_CASES],
)
def test_each_cross_field_rule_refuses_its_contradiction_as_invalid_request(rule, kind, outcome, breaks):
    baseline = report(REQUEST_ID, outcome, kind=kind)
    validate_report(baseline, kind)

    error = _refused(breaks(baseline), kind)

    assert error.reason is Reason.INVALID_REQUEST
    # The detail names the rule and NEVER a value: neither the quota key nor the vendor asset id.
    assert QUOTA_KEY not in error.detail and VENDOR_ASSET_ID not in error.detail


def test_the_red_cases_cover_every_rule():
    assert {case[0] for case in RED_CASES} == set('abcdefgh')


@pytest.mark.parametrize('parts', [None, []], ids=['bars', 'no-bars'])
def test_i_a_succeeded_bars_report_without_a_served_range_is_refused_naming_the_rule(parts):
    """Rule i: a SUCCEEDED bars report must carry a served range.

    Note G (tj-7zjov1.28): the waiter answers from the attempt row's served range, so a SUCCEEDED bars report with
    none is a sender defect, refused INVALID_REQUEST with a detail naming the rule. The baseline is green.
    """
    baseline = report(REQUEST_ID, parts=parts)
    validate_report(baseline, IngestRequestKind.BARS)

    error = _refused(with_trailer(baseline, served_range=None), IngestRequestKind.BARS)

    assert error.reason is Reason.INVALID_REQUEST
    assert error.detail == 'a SUCCEEDED bars report carries no served range'


def test_i_an_asset_list_success_without_a_served_range_is_still_accepted():
    """An asset list carries no served range (note G): rule i holds bars to it, never the other kinds."""
    validate_report(
        with_trailer(report(REQUEST_ID, kind=IngestRequestKind.ASSET_LIST), served_range=None),
        IngestRequestKind.ASSET_LIST,
    )


@pytest.mark.parametrize('outcome', [DeliveryOutcome.FAILED, *NO_CALL])
def test_i_a_bars_report_that_did_not_succeed_needs_no_served_range(outcome):
    """Rule d already forbids one there; rule i adds nothing to the other outcomes."""
    attempt = report(REQUEST_ID, outcome)
    assert attempt.trailer.served_range is None
    validate_report(attempt, IngestRequestKind.BARS)


@pytest.mark.parametrize('outcome', list(DeliveryOutcome))
def test_the_feed_rule_is_judged_against_the_request_kind_not_the_payload(outcome):
    """Rule e is judged against the stored kind: a FAILED or EXPIRED attempt has no payload to judge by.

    The same feed-less report is refused for a bars request and accepted for an asset-list one.
    """
    attempt = with_header(report(REQUEST_ID, outcome, kind=IngestRequestKind.ASSET_LIST), feed=None)
    validate_report(attempt, IngestRequestKind.ASSET_LIST)
    assert _refused(attempt, IngestRequestKind.BARS).reason is Reason.INVALID_REQUEST


@pytest.mark.parametrize('feed', [Feed.IEX, Feed.SIP])
def test_a_bars_report_with_any_resolved_feed_passes_the_feed_rule(feed):
    validate_report(with_header(report(REQUEST_ID, DeliveryOutcome.EXHAUSTED), feed=feed), IngestRequestKind.BARS)


# --------------------------------------------------------------------------------------------- the verdict


def _decide(attempt, *, bars_written=0, has_records=False, superseded=False):
    return decide(attempt, bars_written=bars_written, has_records=has_records, superseded=superseded)


@pytest.mark.parametrize('superseded', [False, True])
def test_a_success_is_final_succeeded_with_records_and_empty_without(superseded):
    attempt = report(REQUEST_ID)
    written = _decide(attempt, bars_written=2, has_records=True, superseded=superseded)
    empty = _decide(attempt, superseded=superseded)

    assert (written.verdict, written.state, written.cause) == (
        DeliveryVerdict.FINAL,
        IngestRequestState.SUCCEEDED,
        None,
    )
    assert (empty.verdict, empty.state, empty.cause) == (DeliveryVerdict.FINAL, IngestRequestState.EMPTY, None)


@pytest.mark.parametrize('superseded', [False, True])
@pytest.mark.parametrize('last_attempt', [False, True])
def test_a_permanent_refusal_is_final_failed_permanent_whoever_reports_it(superseded, last_attempt):
    attempt = report(
        REQUEST_ID, DeliveryOutcome.FAILED, error_class=DeliveryErrorClass.PERMANENT, last_attempt=last_attempt
    )
    decision = _decide(attempt, superseded=superseded)
    assert (decision.verdict, decision.state, decision.cause) == (
        DeliveryVerdict.FINAL,
        IngestRequestState.FAILED,
        IngestRequestFailureCause.PERMANENT,
    )


@pytest.mark.parametrize('error_class', [DeliveryErrorClass.TRANSIENT, DeliveryErrorClass.RATE_LIMITED])
def test_a_retryable_failure_continues_and_moves_nothing(error_class):
    decision = _decide(report(REQUEST_ID, DeliveryOutcome.FAILED, error_class=error_class))
    assert (decision.verdict, decision.state, decision.cause) == (DeliveryVerdict.CONTINUE, None, None)


@pytest.mark.parametrize('error_class', [DeliveryErrorClass.TRANSIENT, DeliveryErrorClass.RATE_LIMITED])
def test_the_last_attempt_is_final_failed_attempts_exhausted(error_class):
    decision = _decide(report(REQUEST_ID, DeliveryOutcome.FAILED, error_class=error_class, last_attempt=True))
    assert (decision.verdict, decision.state, decision.cause) == (
        DeliveryVerdict.FINAL,
        IngestRequestState.FAILED,
        IngestRequestFailureCause.ATTEMPTS_EXHAUSTED,
    )


@pytest.mark.parametrize('last_attempt', [False, True])
@pytest.mark.parametrize('error_class', [DeliveryErrorClass.TRANSIENT, DeliveryErrorClass.RATE_LIMITED])
def test_a_superseded_instances_retryable_failure_is_final_and_ends_nothing(error_class, last_attempt):
    """The old process stops; the live instance holds the budget, so its last_attempt flag is ignored."""
    attempt = report(REQUEST_ID, DeliveryOutcome.FAILED, error_class=error_class, last_attempt=last_attempt)
    decision = _decide(attempt, superseded=True)
    assert (decision.verdict, decision.state, decision.cause) == (DeliveryVerdict.FINAL, None, None)


@pytest.mark.parametrize('superseded', [False, True])
def test_a_dropped_success_is_final_empty_with_the_discarded_cause(superseded):
    """A dropped delivery is FINAL, the request EMPTY with cause DISCARDED.

    P35's drop rule: the dataset was deleted or stopped before the delivery landed. Not a failure: the request ends
    EMPTY, its cause DISCARDED and a detail saying nothing was stored, whoever reported it.
    """
    decision = decide(report(REQUEST_ID), bars_written=0, has_records=True, superseded=superseded, dropped=True)
    assert (decision.verdict, decision.state, decision.cause) == (
        DeliveryVerdict.FINAL,
        IngestRequestState.EMPTY,
        IngestRequestFailureCause.DISCARDED,
    )
    assert decision.detail and 'nothing was stored' in decision.detail


@pytest.mark.parametrize(
    ('outcome', 'cause'),
    [
        (DeliveryOutcome.EXPIRED, IngestRequestFailureCause.EXPIRED),
        (DeliveryOutcome.EXHAUSTED, IngestRequestFailureCause.ATTEMPTS_EXHAUSTED),
    ],
)
@pytest.mark.parametrize('superseded', [False, True])
def test_expired_and_exhausted_are_final_failed_with_their_own_cause_never_a_vendor_failure(outcome, cause, superseded):
    decision = _decide(report(REQUEST_ID, outcome), superseded=superseded)
    assert (decision.verdict, decision.state, decision.cause) == (
        DeliveryVerdict.FINAL,
        IngestRequestState.FAILED,
        cause,
    )
    assert decision.cause is not IngestRequestFailureCause.PERMANENT
