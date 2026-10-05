"""The closed Reason set and its one table, against TE-1 tj-3mk3u5.37.3 and ADR tj-fa1rpu D4 and D8.

Two kinds of test live here. PLANNED is the bead's INITIAL MEMBERSHIP transcribed as data. The reason set
and what each row says are a published, versioned contract (D4; tj-d2mhru), and changing what an existing
reason means is breaking once the SDK ships, so such a change has to turn this file red and be made here on
purpose. Every other test enumerates Reason or the table and checks a rule the design states, so a reason
added later meets the same rules without anyone listing it.
"""

import importlib
import pkgutil
import re
from dataclasses import FrozenInstanceError
from enum import StrEnum
from http import HTTPStatus

import grpc
import pytest

import common.errors
from common.errors.vocabulary import (
    ERROR_DOMAIN,
    METADATA_KEYS,
    REASONS,
    RESERVED_REASONS,
    Disposition,
    ExogenousError,
    InvalidRequestError,
    Outcome,
    Reason,
    ReasonSpec,
)


pytestmark = pytest.mark.common

EXO, INV = ExogenousError, InvalidRequestError
REFUSED, NOT_READY = Outcome.REFUSED, Outcome.NOT_READY
PAGE, RECORD, CLIENT_FIX = Disposition.PAGE, Disposition.RECORD, Disposition.CLIENT_FIX

# reason: (branch, outcome, disposition, grpc_code, http_status), from the bead as amended 2026-10-02 by the
# Q-URI and Q-EMPTY rulings. 24 members.
PLANNED = {
    'INVALID_REQUEST': (INV, REFUSED, CLIENT_FIX, 'INVALID_ARGUMENT', 422),
    'UNSUPPORTED_ASSET_TYPE': (INV, REFUSED, CLIENT_FIX, 'INVALID_ARGUMENT', 422),
    'UNSUPPORTED_INSTRUMENT': (INV, REFUSED, CLIENT_FIX, 'INVALID_ARGUMENT', 422),
    'FEED_NOT_AVAILABLE': (INV, REFUSED, CLIENT_FIX, None, 422),
    'VENDOR_INVALID_REQUEST': (INV, REFUSED, CLIENT_FIX, 'INVALID_ARGUMENT', 422),
    'RANGE_IN_FUTURE': (INV, REFUSED, CLIENT_FIX, 'INVALID_ARGUMENT', 422),
    'VENDOR_REJECTED': (EXO, REFUSED, PAGE, 'INVALID_ARGUMENT', 422),
    'NOT_FOUND': (INV, REFUSED, CLIENT_FIX, 'NOT_FOUND', 404),
    'OWNER_MISMATCH': (INV, REFUSED, CLIENT_FIX, 'PERMISSION_DENIED', 403),
    'OWN_OVERLAP_CONFLICT': (INV, REFUSED, CLIENT_FIX, 'ALREADY_EXISTS', 409),
    'RANGE_COLLISION': (INV, REFUSED, CLIENT_FIX, 'ALREADY_EXISTS', 409),
    'RANGE_SHRINK': (INV, REFUSED, CLIENT_FIX, 'FAILED_PRECONDITION', 409),
    'DUPLICATE_BAR_TIMESTAMP': (INV, REFUSED, CLIENT_FIX, 'INVALID_ARGUMENT', 422),
    'DATABASE_INTEGRITY': (EXO, REFUSED, PAGE, None, 409),
    'RATE_BUDGET': (EXO, NOT_READY, RECORD, 'FAILED_PRECONDITION', 429),
    'VENDOR_RATE_LIMITED': (EXO, NOT_READY, PAGE, 'FAILED_PRECONDITION', 429),
    'VENDOR_UNAVAILABLE': (EXO, NOT_READY, RECORD, 'FAILED_PRECONDITION', 503),
    'VENDOR_AUTH': (EXO, NOT_READY, PAGE, 'FAILED_PRECONDITION', 503),
    'DEADLINE': (EXO, NOT_READY, RECORD, 'FAILED_PRECONDITION', 504),
    'PEER_UNAVAILABLE': (EXO, NOT_READY, PAGE, None, 503),
    'PEER_INTERNAL': (EXO, NOT_READY, PAGE, None, 502),
    'PEER_PROTOCOL_ERROR': (EXO, NOT_READY, PAGE, None, 502),
    'DATABASE_UNAVAILABLE': (EXO, NOT_READY, PAGE, None, 503),
    'DATABASE_CONFLICT': (EXO, NOT_READY, RECORD, None, 503),
}

# Fixed by records for later PRs; the bead names each one's source.
PLANNED_RESERVED = {'OUTSIDE_LOOKBACK', 'COVERAGE_GAP', 'ADMISSION_REFUSED', 'SUBSCRIBER_TOO_SLOW'}

# D8 as written, then the three keys TE-1 adds, each for a recorded reason.
D8_METADATA_KEYS = {'asset_symbol', 'range_start', 'range_end', 'feed', 'granularity', 'vendor', 'retry_after'}
TE1_METADATA_KEYS = {'reset_at', 'colliding_ids', 'error_id'}

# AIP-193: a reason is [A-Z][A-Z0-9_]+[A-Z0-9] in at most 63 characters; a metadata key is [a-z][a-zA-Z0-9-_]+.
AIP_193_REASON = re.compile(r'[A-Z][A-Z0-9_]+[A-Z0-9]')
AIP_193_REASON_MAX = 63
AIP_193_METADATA_KEY = re.compile(r'[a-z][a-zA-Z0-9_-]+')

# Every canonical gRPC status code name but OK, read from grpc itself rather than restated.
GRPC_FAILURE_CODES = frozenset(code.name for code in grpc.StatusCode if code is not grpc.StatusCode.OK)

# The start of an absolute URI: a scheme and a colon (RFC 3986 s3.1), which about:blank and any http(s) base have.
URI_SCHEME = re.compile(r'[A-Za-z][A-Za-z0-9+.-]*:')


def _row(spec: ReasonSpec) -> tuple:
    return (spec.branch, spec.outcome, spec.disposition, spec.grpc_code, spec.http_status)


def test_reason_is_the_planned_closed_set():
    """D4: exactly the 24 planned members. Adding one is a reviewed change; renaming or removing one breaks clients."""
    actual = {reason.value for reason in Reason}
    assert actual == set(PLANNED), (
        f'not in the plan: {sorted(actual - set(PLANNED))}; planned but missing: {sorted(set(PLANNED) - actual)}'
    )


def test_every_row_says_what_the_plan_says():
    """D4: a reason's branch, outcome, disposition, gRPC code and HTTP status are its published meaning."""
    assert {reason.value: _row(REASONS[reason]) for reason in Reason} == PLANNED


def test_reason_is_a_str_enum():
    """The bead: a StrEnum, so a member is its own wire spelling."""
    assert issubclass(Reason, StrEnum)


@pytest.mark.parametrize('reason', list(Reason), ids=str)
def test_each_reason_is_named_by_its_value_in_aip_193_form(reason: Reason):
    """The bead: member name equals value, UPPER_SNAKE per AIP-193, at most 63 characters."""
    assert reason.name == reason.value
    assert AIP_193_REASON.fullmatch(reason.value), f'{reason.value!r} is not AIP-193 UPPER_SNAKE'
    assert len(reason.value) <= AIP_193_REASON_MAX


def test_outcome_holds_only_the_two_failure_outcomes():
    """D3: REFUSED and NOT_READY. SERVED is a success and never an error's outcome."""
    assert {outcome.value for outcome in Outcome} == {'REFUSED', 'NOT_READY'}


def test_disposition_holds_exactly_the_three_dispositions():
    """D4: PAGE, RECORD or CLIENT_FIX."""
    assert {disposition.value for disposition in Disposition} == {'PAGE', 'RECORD', 'CLIENT_FIX'}


def test_the_table_is_total_over_reason_and_keyed_by_its_members():
    """The bead: REASONS is TOTAL over Reason, and every key is a member, never a str that merely equals one."""
    missing = [reason.value for reason in Reason if reason not in REASONS]
    assert not missing, f'REASONS has no row for {missing}'
    assert len(REASONS) == len(Reason)
    assert all(type(key) is Reason for key in REASONS), 'a str key compares equal to a member but is not one'
    assert all(isinstance(spec, ReasonSpec) for spec in REASONS.values())


@pytest.mark.parametrize('reason', list(Reason), ids=str)
def test_no_row_renders_as_unavailable(reason: Reason):
    """tj-8konfu D6.4: the server never deliberately returns UNAVAILABLE, so no reason maps to it."""
    assert REASONS[reason].grpc_code != 'UNAVAILABLE'


@pytest.mark.parametrize('reason', list(Reason), ids=str)
def test_each_grpc_code_is_none_or_a_canonical_failure_code_name(reason: Reason):
    """The bead: grpc_code is the canonical status NAME as a str, or None for never rendered as a status."""
    code = REASONS[reason].grpc_code
    assert code is None or code in GRPC_FAILURE_CODES, f'{code!r} is not a gRPC failure status name'


@pytest.mark.parametrize('reason', list(Reason), ids=str)
def test_each_http_status_is_a_real_error_status(reason: Reason):
    """An error renders as a 4xx or 5xx that HTTP defines."""
    status = REASONS[reason].http_status
    assert status in set(HTTPStatus), f'{status} is not an HTTP status'
    assert 400 <= status <= 599


@pytest.mark.parametrize('reason', list(Reason), ids=str)
def test_each_summary_is_one_non_empty_line(reason: Reason):
    """The bead: every row has a non-empty, single-line summary, which the generated catalogue prints."""
    summary = REASONS[reason].summary
    assert isinstance(summary, str)
    assert summary.strip(), 'the summary is empty'
    assert len(summary.splitlines()) == 1, f'the summary is not one line: {summary!r}'


def test_the_table_cannot_be_edited_at_runtime():
    """D4: the one table is what the diff shows. Neither a row nor a field of one can be replaced while running."""
    spec = REASONS[Reason.NOT_FOUND]
    with pytest.raises(TypeError):
        REASONS[Reason.NOT_FOUND] = spec  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        spec.http_status = 500  # type: ignore[misc]


def test_reserved_reasons_are_the_four_names_the_records_fix():
    """The bead: RESERVED_REASONS is a tuple of plain str naming the four reserved names, each once."""
    assert isinstance(RESERVED_REASONS, tuple)
    assert all(type(name) is str for name in RESERVED_REASONS)
    assert len(RESERVED_REASONS) == len(set(RESERVED_REASONS)), f'a name is repeated: {RESERVED_REASONS}'
    assert set(RESERVED_REASONS) == PLANNED_RESERVED


def test_no_reserved_name_is_a_reason():
    """The bead: RESERVED_REASONS is disjoint from Reason, by value and by member name."""
    taken = set(RESERVED_REASONS) & ({reason.value for reason in Reason} | set(Reason.__members__))
    assert not taken, f'reserved for a later PR but already a member: {sorted(taken)}'


def test_the_error_domain_is_the_ruled_constant():
    """Q-URI tj-3mk3u5.37.2: one generic domain, because this repository is public."""
    assert ERROR_DOMAIN == 'trader-joe'


def test_the_package_names_no_type_uri():
    """Q-URI amends U3: the type is about:blank, rendered by TE-3. common/errors exports no URI and no helper for one."""
    modules = [common.errors] + [
        importlib.import_module(info.name)
        for info in pkgutil.walk_packages(common.errors.__path__, prefix=f'{common.errors.__name__}.')
    ]
    for module in modules:
        public = {name: value for name, value in vars(module).items() if not name.startswith('_')}
        assert 'problem_type_uri' not in public, module.__name__
        named = [name for name in public if re.search('uri|url', name, re.IGNORECASE)]
        assert not named, f'{module.__name__} exports {named}'
        uris = [name for name, value in public.items() if isinstance(value, str) and URI_SCHEME.match(value)]
        assert not uris, f'{module.__name__} exports a URI constant: {uris}'


def test_metadata_keys_are_d8_plus_the_three_recorded_additions():
    """D8's closed allowlist plus reset_at, colliding_ids and error_id, and nothing else: this repository is public."""
    assert isinstance(METADATA_KEYS, frozenset)
    assert set(METADATA_KEYS) == D8_METADATA_KEYS | TE1_METADATA_KEYS, (
        f'not allowed by D8 or TE-1: {sorted(METADATA_KEYS - D8_METADATA_KEYS - TE1_METADATA_KEYS)}; '
        f'missing: {sorted((D8_METADATA_KEYS | TE1_METADATA_KEYS) - METADATA_KEYS)}'
    )


@pytest.mark.parametrize('key', sorted(METADATA_KEYS))
def test_each_metadata_key_is_aip_193_shaped(key: str):
    """D8: keys match AIP-193's [a-z][a-zA-Z0-9-_]+."""
    assert AIP_193_METADATA_KEY.fullmatch(key)
