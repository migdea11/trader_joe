"""OVERRIDE_REQUIRED at the HTTP edge: a 409 whose problem body carries no metadata and never the owner.

tj-grna9p.16, architect gate of 2026-10-14. An operator write (Set Expiry, delete) on a dataset another owner
holds, sent without an override carrying a non-blank reason and confirmed true, is refused with this reason.
The ruling: HTTP 409, FAILED_PRECONDITION, and NO metadata, because the owner value is never carried in an
error body (the OWNER_MISMATCH convention, ADR tj-fa1rpu D8). The UI already holds DatasetSummary.owner.

The sweeps in test_problem_json_reasons.py reach this reason with every other one; what they cannot say is
that its body is exactly the fixed members and nothing else, which is the half of the ruling a call site
could break by passing metadata. That is pinned here, along with the vocabulary guarantee underneath it:
no allowlisted key, and no ProblemDetails member, can carry an owner.
"""

import http

import pytest

from common.errors.vocabulary import METADATA_KEYS, REASONS, InvalidRequestError, Reason
from routers.common.errors import ProblemDetails
from routers.tests.problem_app import ABOUT_BLANK, PROBLEM_MEDIA_TYPE, answer_to


pytestmark = pytest.mark.common

DETAIL = 'The dataset belongs to another owner.'

# Every member a TraderJoeError's problem body carries whatever the reason: RFC 9457's four, the vocabulary
# pair, and the always-present error_id (D8). A body with any other member carries metadata.
FIXED_MEMBERS = {'type', 'title', 'status', 'detail', 'reason', 'domain', 'error_id'}

# The summary the architect's gate note fixed, word for word: it is the line docs/errors.md publishes.
RULED_SUMMARY = 'The dataset belongs to another owner: send override with a non-blank reason and confirmed true.'

# Spellings an owner could travel under. None may be an allowlisted key or a body member.
OWNER_SPELLINGS = ('owner', 'dataset_owner', 'owner_id', 'current_owner')


def test_the_row_is_the_ruled_one():
    """Branch, outcome, disposition, codes and summary as the gate note rules them."""
    spec = REASONS[Reason.OVERRIDE_REQUIRED]
    assert spec.branch is InvalidRequestError
    assert spec.http_status == 409
    assert spec.grpc_code == 'FAILED_PRECONDITION'
    assert spec.requires_reset_at is False
    assert spec.summary == RULED_SUMMARY


def test_it_answers_409_with_exactly_the_fixed_members():
    """As the route raises it, with no metadata: 409 Conflict, and the body holds the fixed members only."""
    response = answer_to(InvalidRequestError(Reason.OVERRIDE_REQUIRED, DETAIL))
    body = response.json()
    assert response.status_code == http.HTTPStatus.CONFLICT == 409
    assert response.headers['content-type'] == PROBLEM_MEDIA_TYPE
    assert set(body) == FIXED_MEMBERS, f'members beyond the fixed set: {sorted(set(body) - FIXED_MEMBERS)}'
    assert body['type'] == ABOUT_BLANK
    assert body['title'] == http.HTTPStatus.CONFLICT.phrase
    assert body['status'] == 409
    assert body['reason'] == 'OVERRIDE_REQUIRED'
    assert body['detail'] == DETAIL
    assert 'retry-after' not in response.headers


@pytest.mark.parametrize('spelling', OWNER_SPELLINGS)
def test_no_owner_can_be_carried(spelling: str):
    """The owner is not an allowlisted key, so the error refuses it and no body member exists for it (D8)."""
    assert spelling not in METADATA_KEYS
    assert spelling not in ProblemDetails.model_fields
    with pytest.raises(ValueError, match='not in METADATA_KEYS'):
        InvalidRequestError(Reason.OVERRIDE_REQUIRED, DETAIL, metadata={spelling: 'rebalancer'})
