"""OVERRIDE_REQUIRED on a gRPC hop: FAILED_PRECONDITION, and an ErrorInfo holding no metadata but error_id.

tj-grna9p.16, architect gate of 2026-10-14: no metadata, so the owner value is never carried (ADR tj-fa1rpu
D8, the OWNER_MISMATCH convention). The renderable sweep in test_rpc_errors_render.py reaches the code; this
pins that the reason, raised as its route raises it, carries nothing beyond the always-present error_id, and
no RetryInfo, since a refusal never says when to retry.
"""

import grpc
import pytest
from google.rpc import error_details_pb2

from common.errors.vocabulary import ERROR_DOMAIN, InvalidRequestError, Reason
from common.rpc.errors import render


pytestmark = pytest.mark.common


def test_it_renders_failed_precondition_with_only_the_error_id():
    status = render(InvalidRequestError(Reason.OVERRIDE_REQUIRED, 'another owner'), error_id='e-1')
    assert status.code == grpc.StatusCode.FAILED_PRECONDITION.value[0]
    assert len(status.details) == 1, 'a refusal carries the ErrorInfo and no RetryInfo'
    info = error_details_pb2.ErrorInfo()
    assert status.details[0].Unpack(info)
    assert (info.reason, info.domain) == ('OVERRIDE_REQUIRED', ERROR_DOMAIN)
    assert dict(info.metadata) == {'error_id': 'e-1'}
