"""What an error may carry: detail, allowlisted metadata and reset_at (TE-1 tj-3mk3u5.37.3; ADR tj-fa1rpu D3, D4, D8).

This repository is public, so D8's allowlist is a closed set: a key outside it is refused, and so is a key
added to the caller's dict after the error was built. reset_at is an aware instant stored in UTC. The two
rate limits always carry it, and a REFUSED reason never does, because no wait cures a permanent refusal (D3)
and a delay is attached only where the server can name one (D4). retry_after is derived from reset_at and
never travels through metadata, where a stored copy would go stale.
"""

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from common.errors.vocabulary import METADATA_KEYS, REASONS, ExogenousError, Outcome, Reason


pytestmark = pytest.mark.common

NOW = datetime(2001, 2, 3, 4, 5, 6, tzinfo=UTC)

# The metadata rules do not depend on the reason; this one is NOT_READY and may carry reset_at.
ANY_REASON = Reason.VENDOR_UNAVAILABLE

# Allowlisted, but set from the reset_at argument: retry_after is derived and never stored.
DERIVED_KEYS = ('reset_at', 'retry_after')

# The two reasons the bead says ALWAYS carry reset_at.
RESET_AT_REQUIRED = frozenset({'RATE_BUDGET', 'VENDOR_RATE_LIMITED'})

NOT_READY_REASONS = [reason for reason in Reason if REASONS[reason].outcome is Outcome.NOT_READY]
REFUSED_REASONS = [reason for reason in Reason if REASONS[reason].outcome is Outcome.REFUSED]


def _error(**kwargs: object) -> ExogenousError:
    return ExogenousError(ANY_REASON, 'detail', **kwargs)


@pytest.mark.parametrize('key', sorted(METADATA_KEYS - set(DERIVED_KEYS)))
def test_each_allowlisted_key_is_carried(key: str):
    """D8: a key from METADATA_KEYS is accepted and read back unchanged."""
    assert dict(_error(metadata={key: 'value'}).metadata) == {key: 'value'}


@pytest.mark.parametrize(
    'key', ['account_id', 'dsn', 'password', 'strategy', 'target', 'stack_trace', 'vendor_body', 'Feed', 'feed ', '']
)
def test_a_key_outside_the_allowlist_is_refused(key: str):
    """D8: any key not in METADATA_KEYS is refused, including near-misses of an allowed one."""
    with pytest.raises(ValueError):
        _error(metadata={key: 'value'})


@pytest.mark.parametrize('key', DERIVED_KEYS)
def test_reset_at_and_retry_after_never_arrive_through_metadata(key: str):
    """retry_after is DERIVED, never stored; both keys come from the reset_at argument, never from metadata."""
    with pytest.raises(ValueError):
        _error(metadata={key: '5'}, reset_at=NOW)


@pytest.mark.parametrize(
    'value',
    [5, 1.5, None, b'raw body', {'nested': 'x'}, ['id', 5], [b'id']],
    ids=['int', 'float', 'none', 'bytes', 'mapping', 'list-with-int', 'list-of-bytes'],
)
def test_a_metadata_value_that_is_not_str_or_a_list_of_str_is_refused(value: object):
    """Metadata values are text, formatted by the raiser, so every renderer writes the same thing."""
    with pytest.raises(TypeError):
        _error(metadata={'vendor': value})


@pytest.mark.parametrize('value', [['a', 'b'], ('a', 'b'), []], ids=['list', 'tuple', 'empty'])
def test_a_list_of_str_is_carried_as_a_tuple(value: list[str] | tuple[str, ...]):
    """colliding_ids-style values are accepted and frozen, so the error cannot change after it is built."""
    carried = _error(metadata={'colliding_ids': value}).metadata['colliding_ids']
    assert carried == tuple(value)
    assert isinstance(carried, tuple)


def test_metadata_is_a_snapshot_the_caller_cannot_change_later():
    """D8 holds after construction: a key added to the caller's dict, or an id appended to its list, never arrives."""
    ids = ['a']
    given: dict[str, object] = {'colliding_ids': ids}
    error = _error(metadata=given)
    given['account_id'] = 'leaked'
    ids.append('b')
    assert dict(error.metadata) == {'colliding_ids': ('a',)}


def test_metadata_cannot_be_edited_on_the_error():
    """The allowlist is checked once, at construction, so the mapping the error exposes must be read-only."""
    error = _error(metadata={'vendor': 'v'})
    with pytest.raises(TypeError):
        error.metadata['account_id'] = 'x'  # type: ignore[index]


def test_no_metadata_reads_as_an_empty_mapping():
    """An error built without metadata carries none."""
    assert dict(_error().metadata) == {}


@pytest.mark.parametrize('detail', [b'raw vendor body', {'message': 'x'}, 42], ids=['bytes', 'mapping', 'int'])
def test_detail_must_be_a_str(detail: object):
    """The bead: detail is a human-facing str. A raw body or a structure is refused rather than rendered."""
    with pytest.raises(TypeError):
        ExogenousError(ANY_REASON, detail)  # type: ignore[arg-type]


@pytest.mark.parametrize('reason', NOT_READY_REASONS, ids=str)
def test_a_naive_reset_at_is_refused(reason: Reason):
    """The bead: reset_at is an aware UTC datetime; a naive value names no instant and is refused."""
    with pytest.raises(ValueError):
        REASONS[reason].branch(reason, 'detail', reset_at=datetime(2001, 2, 3, 4, 5, 6))


@pytest.mark.parametrize(
    'value', ['2001-02-03T04:05:06Z', 981173106.0, date(2001, 2, 3)], ids=['iso-str', 'epoch', 'date']
)
def test_a_reset_at_that_is_not_a_datetime_is_refused(value: object):
    """reset_at is a datetime, never a string or a number a renderer would have to guess the meaning of."""
    with pytest.raises(TypeError):
        _error(reset_at=value)


@pytest.mark.parametrize(
    'offset', [timedelta(hours=5), timedelta(hours=-7, minutes=-30), timedelta(0)], ids=['+05:00', '-07:30', 'utc']
)
def test_an_aware_reset_at_is_stored_as_the_same_instant_in_utc(offset: timedelta):
    """The bead: reset_at is UTC. An aware value in another zone is converted, keeping its instant."""
    error = _error(reset_at=NOW.astimezone(timezone(offset)))
    assert error.reset_at == NOW
    assert error.reset_at.utcoffset() == timedelta(0)
    assert error.reset_at.replace(tzinfo=None) == NOW.replace(tzinfo=None)


@pytest.mark.parametrize('name', sorted(RESET_AT_REQUIRED))
def test_a_rate_limit_without_reset_at_is_refused(name: str):
    """The bead: RATE_BUDGET and VENDOR_RATE_LIMITED ALWAYS carry reset_at."""
    reason = Reason(name)
    with pytest.raises(ValueError):
        REASONS[reason].branch(reason, 'detail')


@pytest.mark.parametrize('reason', [r for r in NOT_READY_REASONS if r.value not in RESET_AT_REQUIRED], ids=str)
def test_any_other_not_ready_reason_may_omit_reset_at(reason: Reason):
    """D4: a delay is attached only where the server can name one; elsewhere it is omitted, never invented."""
    error = REASONS[reason].branch(reason, 'detail')
    assert error.reset_at is None
    assert error.retry_after is None


@pytest.mark.parametrize('reason', NOT_READY_REASONS, ids=str)
def test_every_not_ready_reason_may_carry_reset_at(reason: Reason):
    """D3: NOT_READY carries a delay where one can be named, whichever NOT_READY reason it is."""
    assert REASONS[reason].branch(reason, 'detail', reset_at=NOW).reset_at == NOW


@pytest.mark.parametrize('reason', REFUSED_REASONS, ids=str)
def test_a_refused_reason_carries_no_reset_at(reason: Reason):
    """D3: REFUSED is permanent for the request as asked, so no wait cures it and it names none."""
    with pytest.raises(ValueError):
        REASONS[reason].branch(reason, 'detail', reset_at=NOW)
