"""Chunking the bar upsert, and the atomicity that makes chunking safe (tj-rpyv5u, tj-vhboky.7).

WHY THIS FILE EXISTS (validator, gating 8eea874). The builder named its own coverage gap rather
than answering `none`: nothing in the suite sent more than one chunk's worth of bars through
batch_create_market_activity_data, so the entire chunking path was reached by no test. The gap was
real and invisible -- test_bar_batch_guard.py's two-bar batch asserts `execute.await_count == 1`,
which chunking leaves true, so that file goes on passing whether the loop exists or not.

THE PROPERTY THAT MATTERS MOST IS NOT "THE WRITE IS CHUNKED". It is "a failure on any chunk leaves
nothing persisted". Chunking without one transaction is strictly WORSE than the bug it fixes:
today an oversized batch fails loudly and writes nothing, whereas a per-chunk commit turns one
failed request into a half-written dataset -- a store_dataset_entry claiming coverage for bars that
were never stored, which is the failure class this epic exists to remove. A test that counted
statements would not see the difference, so the atomicity tests below assert the ORDER of
execute/commit/rollback and that no commit precedes a failure, not just a call count.

WHAT IS MEASURED, AND WHY IT IS THE COMPILED STATEMENT. The bind-parameter count and the rows in
each chunk are read off the statement compiled for the postgresql dialect, not off the values list
handed in. The defect was about what reaches the wire, and the compiler is the authority on how
many bind parameters a row actually costs -- a column whose default is an inline SQL expression
costs none, one whose default is a Python callable costs one per row. Asserting against the
compiler is what makes bind_params_per_row()'s derivation a tested claim rather than a comment;
re-running its own column comprehension inside a test would only prove the comprehension agrees
with itself.

NOT PROVEN HERE, and it is the operationally important half. That an oversized batch genuinely
SUCCEEDS against Postgres, and that Postgres actually rejects the pre-fix single statement, need a
live database: a mocked session accepts any statement the wire protocol would refuse. That belongs
to the host-verified tier (tj-vhboky.14). This file proves we never hand the wire a statement we
know it would reject, and that a partial failure cannot leave data behind.
"""

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import Insert

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity
from data.store.app.database.crud.stock.asset_market_activity import (
    POSTGRES_MAX_BIND_PARAMETERS,
    DuplicateBatchTimestamp,
    _resolve_chunk_size,
    batch_create_market_activity_data,
    build_market_activity_upsert,
)
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.ingest import data_action_request
from schemas.data_store.asset_dataset_store import StoreAssetDatasetBody, StoreAssetDatasetPath
from schemas.data_store.stock.market_activity_data import (
    BatchStockDataMarketActivityCreate,
    StockDataMarketActivityData,
)


pytestmark = pytest.mark.data_store

FIRST_TIMESTAMP = datetime(2026, 1, 2, tzinfo=UTC)

# The chunk ceiling production actually uses, taken from the resolver rather than recomputed here.
# A test that recomputed `65535 // 12` would agree with a broken resolver, and the module constant
# is what the write path consults. It is safe to anchor the rest of the file on this because
# test_the_derived_ceiling_is_the_largest_chunk_the_protocol_accepts pins this exact value against
# the compiler and the protocol cap, independently of the resolver that produced it.
DERIVED_CEILING = _resolve_chunk_size(None)


def _batch_of(bar_count: int) -> BatchStockDataMarketActivityCreate:
    """A batch of bar_count bars, one minute apart.

    Timestamps are distinct and ascending so the whole-batch duplicate guard passes and so the
    position of a bar within the batch is recoverable from its timestamp -- which is what lets the
    partition tests below prove every bar was sent exactly once rather than merely counting rows.
    """
    batch = BatchStockDataMarketActivityCreate(
        dataset_id=uuid4(),
        asset_symbol='AAPL',
        source=DataSource.ALPACA_API,
        feed=Feed.SIP,
        granularity=Granularity.ONE_DAY,
        dataset={},
    )
    for index in range(bar_count):
        batch.append_data(
            DataType.MARKET_ACTIVITY,
            StockDataMarketActivityData(open=1.0, high=2.0, low=0.5, close=1.5, volume=100, trade_count=10),
            FIRST_TIMESTAMP + timedelta(minutes=index),
        )
    return batch


def _batch_at_minutes(minute_offsets: list[int]) -> BatchStockDataMarketActivityCreate:
    """A batch whose timestamps are given explicitly, duplicates allowed.

    _batch_of() cannot express this case: it walks range(bar_count) and so its timestamps are
    strictly ascending by construction, which means every batch it builds passes the
    duplicate-timestamp guard no matter where the chunk boundaries fall. Pinning that the guard is
    whole-batch rather than per-chunk requires a repeat at a chosen POSITION, so the position list
    is the parameter here rather than a count.
    """
    batch = BatchStockDataMarketActivityCreate(
        dataset_id=uuid4(),
        asset_symbol='AAPL',
        source=DataSource.ALPACA_API,
        feed=Feed.SIP,
        granularity=Granularity.ONE_DAY,
        dataset={},
    )
    for minute_offset in minute_offsets:
        batch.append_data(
            DataType.MARKET_ACTIVITY,
            StockDataMarketActivityData(open=1.0, high=2.0, low=0.5, close=1.5, volume=100, trade_count=10),
            FIRST_TIMESTAMP + timedelta(minutes=minute_offset),
        )
    return batch


def _upsert_for(bar_count: int) -> Insert:
    """The statement the write path would build for a single chunk of bar_count rows."""
    return build_market_activity_upsert(StockMarketActivity.from_batch_create(_batch_of(bar_count)))


def _bind_params(statement: Insert) -> dict[str, object]:
    """Every bind parameter the statement will send, as Postgres would receive them.

    This is the quantity the 65,535 cap applies to, so it is the quantity asserted on.
    """
    return statement.compile(dialect=postgresql.dialect()).params


def _timestamps_sent(statement: Insert) -> list[datetime]:
    """The timestamps one statement binds, in row order.

    SQLAlchemy names a multi-row INSERT's binds `<column>_m<row index>`, so the suffix recovers
    the row order. Reading the timestamps back out of the compiled statement -- rather than trusting
    the list the loop sliced -- is what makes "every bar exactly once, in order" an assertion about
    what would reach the database.
    """
    bound = {key: value for key, value in _bind_params(statement).items() if key.startswith('timestamp_m')}
    return [bound[f'timestamp_m{index}'] for index in range(len(bound))]


def _statements_sent(session) -> list[Insert]:
    return [await_call.args[0] for await_call in session.execute.await_args_list]


def _expected_timestamps(bar_count: int) -> list[datetime]:
    return [FIRST_TIMESTAMP + timedelta(minutes=index) for index in range(bar_count)]


@pytest.fixture
def call_order() -> list[str]:
    """The order in which the write path touched the session.

    A COUNT OF COMMITS IS NOT ENOUGH to tell one transaction from several. On a single-chunk batch
    a commit inside the loop also counts exactly one, and even on a multi-chunk batch a count says
    nothing about whether the commit landed between chunks. The order does, in one assertion.
    """
    return []


@pytest.fixture
def session(call_order: list[str]):
    """An async session that records what it was asked to do and reaches no database.

    execute/commit/rollback are the three the write path uses. Each records itself in call_order
    while still recording its arguments on the mock, so a test can assert on the sequence and on
    the statements in the same run.
    """
    db = MagicMock()
    db.execute = AsyncMock(side_effect=lambda statement: call_order.append('execute'))
    db.commit = AsyncMock(side_effect=lambda: call_order.append('commit'))
    db.rollback = AsyncMock(side_effect=lambda: call_order.append('rollback'))
    return db


# ---------------------------------------------------------------------------------------
# THE DERIVATION. bind_params_per_row() reads the table's own columns and excludes the primary key
# and anything carrying a default. Deriving is right -- a hard-coded twelve would go stale, and
# this table gained feed and lost two factor columns inside two days -- but a derivation has its
# own failure mode, and these are the tests for that rather than for the arithmetic downstream.


@pytest.mark.parametrize('row_count', [1, 2, 7])
def test_bind_params_per_row_matches_what_the_compiler_actually_binds(row_count: int):
    """The derivation's real failure mode: a column the rule predicts wrongly.

    The exclusion rule is a PREDICTION about how SQLAlchemy compiles each column -- that a column
    with a default costs no bind parameter because the default is an inline SQL expression. That
    holds for created_at/updated_at's func.now() and would NOT hold for a Python-callable default
    such as `default=datetime.utcnow`, which compiles to one bind per row. A future column of that
    shape, or a new server default, would silently shift the true count while the comprehension
    went on returning the old one, and the chunk size would quietly stop being safe.

    Compiling the real statement and counting what it binds is the only assertion that notices,
    because it checks the derivation against the compiler instead of against itself. Several row
    counts, so a derivation that happened to be right for one row is not mistaken for a per-row
    figure.

    Args:
        row_count: Rows in the single statement whose bind parameters are counted.
    """
    assert len(_bind_params(_upsert_for(row_count))) == StockMarketActivity.bind_params_per_row() * row_count


def test_the_derivation_excludes_the_surrogate_key_and_the_server_stamped_columns():
    """The exclusion rule itself, named by hand in the one place that should cost a decision.

    The rule is invisible in the count: bind_params_per_row() returning 12 says nothing about WHICH
    12. A derivation that excluded the wrong column could still return a plausible number, and
    test_bind_params_per_row_matches_what_the_compiler_actually_binds would not care -- it compares
    the derivation with the compiler, and both would move together if a column gained a default.

    So the three columns the caller does NOT bind are listed here. Equality, not containment: a NEW
    excluded column means either a new server default or a new Python-side default, and both change
    how many rows fit in a statement. That should go red and be ruled on, not absorbed. This is the
    same shape, and the same reasoning, as test_bar_write_path.py's
    test_the_required_set_is_not_vacuous.
    """
    bound_columns = {key.removesuffix('_m0') for key in _bind_params(_upsert_for(1))}
    all_columns = {column.name for column in StockMarketActivity.__table__.columns}

    assert all_columns - bound_columns == {'id', 'created_at', 'updated_at'}
    assert len(bound_columns) == StockMarketActivity.bind_params_per_row()


def test_the_derived_ceiling_is_the_largest_chunk_the_protocol_accepts():
    """The boundary, against the compiler and the protocol cap rather than against the arithmetic.

    BOTH HALVES ARE LOAD-BEARING. That a chunk at the ceiling fits is satisfied by any ceiling at
    all, including an absurdly small one -- so on its own it proves nothing about the division. That
    one more row does NOT fit is what makes the ceiling maximal, and it is the half a rounding error
    breaks: `math.ceil` in place of `//`, or an off-by-one, reds here and nowhere else in this file.

    This is also the test that lets the rest of the file trust DERIVED_CEILING, which is read off
    the resolver: whatever the resolver returned, this proves that value is exactly the protocol's
    largest safe chunk.
    """
    assert len(_bind_params(_upsert_for(DERIVED_CEILING))) <= POSTGRES_MAX_BIND_PARAMETERS
    assert len(_bind_params(_upsert_for(DERIVED_CEILING + 1))) > POSTGRES_MAX_BIND_PARAMETERS


# ---------------------------------------------------------------------------------------
# THE RESOLVER. Clamp-and-log on unset, non-positive and over-limit. The builder exercised these by
# hand against the real model and reported the numbers; hand-running is not a test.


def test_an_unset_setting_falls_back_to_the_derived_ceiling():
    """None is a REACHABLE production state, not a defensive branch.

    get_env_var('MARKET_ACTIVITY_BATCH_SIZE', cast_type=int) returns None when the variable is
    unset -- it casts only when the value is not None (common/environment.py) -- so the module
    constant is None and that None is what the write path receives. A resolver that assumed an int
    would raise a TypeError inside the write path's try block and turn a missing setting into a
    lost batch.

    The arithmetic is restated here deliberately, and this is the only place it is: it pins the
    FLOOR division specifically, which is a claim about the operator rather than about the value.
    The value itself is anchored independently, against the compiler, by
    test_the_derived_ceiling_is_the_largest_chunk_the_protocol_accepts.
    """
    assert _resolve_chunk_size(None) == POSTGRES_MAX_BIND_PARAMETERS // StockMarketActivity.bind_params_per_row()


@pytest.mark.parametrize('requested', [0, -1, -5461])
def test_a_non_positive_chunk_size_is_clamped_to_the_ceiling(requested: int):
    """A misconfigured setting must not become a zero-row chunk.

    Zero matters beyond tidiness: `range(0, n, 0)` raises ValueError, so an unclamped zero would
    fail every batch outright, and a negative step would loop the wrong way. Both are plausible
    contents of an environment variable nobody validated.

    Args:
        requested: A configured chunk size that cannot be used as a step.
    """
    assert _resolve_chunk_size(requested) == DERIVED_CEILING


@pytest.mark.parametrize('requested', [DERIVED_CEILING + 1, 1_000_000])
def test_an_over_limit_chunk_size_is_clamped_to_the_ceiling(requested: int):
    """The setting governs the chunk size, but it does not get to exceed the protocol.

    DERIVED_CEILING + 1 is the case that matters -- the smallest over-limit value, which an
    off-by-one in the comparison would wave through into a statement Postgres rejects.

    Args:
        requested: A configured chunk size larger than the protocol allows.
    """
    assert _resolve_chunk_size(requested) == DERIVED_CEILING


@pytest.mark.parametrize('requested', [1, 2, 10, DERIVED_CEILING - 1, DERIVED_CEILING])
def test_a_chunk_size_within_the_limit_is_used_verbatim(requested: int):
    """THE ACCEPTANCE CRITERION: the setting governs rather than being ignored.

    A resolver that returned the ceiling unconditionally would satisfy every clamping test above
    and reinstate the original defect in a new form -- the setting read and discarded. Ten is here
    because it is the value data/store/.env.default actually ships, so the configured production
    path is a tested one.

    Args:
        requested: A configured chunk size the protocol permits, which must survive untouched.
    """
    assert _resolve_chunk_size(requested) == requested


def test_the_ceiling_itself_is_not_reported_as_clamped(caplog: pytest.LogCaptureFixture):
    """The one boundary where the RETURNED VALUE cannot tell right from wrong.

    `>=` in place of `>` returns the ceiling here just as `>` does, so no assertion on the return
    value distinguishes them -- test_a_chunk_size_within_the_limit_is_used_verbatim passes either
    way. The log is the only observable difference: one spelling reports a clamp that did not
    happen, and an operator who then lowered the setting to silence a spurious warning would be
    acting on a false signal.
    """
    with caplog.at_level(logging.WARNING):
        assert _resolve_chunk_size(DERIVED_CEILING) == DERIVED_CEILING

    assert caplog.records == [], 'the largest legal chunk size was reported as over the limit'


@pytest.mark.parametrize('requested', [None, 0, -1, DERIVED_CEILING + 1])
def test_clamping_says_so(requested: int | None, caplog: pytest.LogCaptureFixture):
    """Clamping and SAYING SO is one instruction in the bead, so the log is asserted, not assumed.

    Silently substituting a different chunk size than the one configured is how a setting comes to
    be believed and wrong. The effective size has to appear in the message, or the operator reading
    it still does not know what the write path is doing.

    Args:
        requested: A configured chunk size that must be both clamped and reported.
        caplog: Captures the warning the resolver is required to emit.
    """
    with caplog.at_level(logging.WARNING):
        _resolve_chunk_size(requested)

    assert len(caplog.records) == 1, 'clamping was silent'
    assert str(DERIVED_CEILING) in caplog.records[0].getMessage(), 'the log does not name the size actually used'


# ---------------------------------------------------------------------------------------
# THE PARTITION. That the loop covers the batch exactly once -- no dropped tail, no repeated row.


@pytest.mark.parametrize(
    'bar_count, requested_chunk_size, expected_chunk_sizes',
    [
        (5, 2, [2, 2, 1]),  # a partial final chunk: the dropped-tail case
        (4, 2, [2, 2]),  # an exact multiple: no empty trailing statement
        (1, 2, [1]),  # fewer bars than one chunk
        (3, 1, [1, 1, 1]),  # the smallest legal chunk
        (2, None, [2]),  # unset setting: one statement, the shape test_bar_batch_guard.py assumes
    ],
)
@pytest.mark.asyncio
async def test_the_chunks_cover_every_bar_exactly_once_in_order(
    session, bar_count: int, requested_chunk_size: int | None, expected_chunk_sizes: list[int]
):
    """The arithmetic, asserted on the rows sent rather than on the number of statements.

    COUNTING STATEMENTS IS THE WEAK VERSION and would miss the failures that matter. A slice that
    dropped the final partial chunk, one that re-sent a row on each boundary, and one that skipped
    a row between chunks can all produce the expected NUMBER of statements. Reconstructing the
    timestamps out of the compiled statements and comparing against the whole ascending batch
    catches a gap, an overlap and a truncation alike, because each shows up as a different list.

    The exact-multiple row is here for the empty-trailing-statement bug specifically: `range` gets
    that right, a hand-rolled `while` with a `<=` does not.

    Args:
        session: The recording session; its awaited statements are the evidence.
        bar_count: Bars in the batch.
        requested_chunk_size: The configured chunk size, or None for unset.
        expected_chunk_sizes: Rows expected in each statement, in order.
    """
    written = await batch_create_market_activity_data(
        session, _batch_of(bar_count), requested_chunk_size=requested_chunk_size
    )

    assert written == bar_count, 'the reported count no longer matches the batch'
    statements = _statements_sent(session)
    assert [len(_timestamps_sent(statement)) for statement in statements] == expected_chunk_sizes
    sent_in_order = [timestamp for statement in statements for timestamp in _timestamps_sent(statement)]
    assert sent_in_order == _expected_timestamps(bar_count), 'the chunks dropped, repeated or reordered a bar'


# ---------------------------------------------------------------------------------------
# THE ATOMICITY. One transaction across every chunk -- the property the task title is emphatic
# about, and the one whose absence would be worse than the bug being fixed.


@pytest.mark.asyncio
async def test_a_multi_chunk_batch_commits_once_after_the_last_chunk(session, call_order: list[str]):
    """One commit, and it comes AFTER the loop rather than inside it.

    The sequence is the assertion. A `db.commit()` moved inside the loop produces
    execute/commit/execute/commit/execute/commit -- three transactions, the same three statements,
    and the same final state on a successful run, which is exactly why a count or a row check
    cannot see it. It only becomes visible when a chunk fails, and by then it has already written
    half a dataset. Pinning the order here means the mistake is caught on the GREEN path, before
    it costs anyone a partial write.
    """
    await batch_create_market_activity_data(session, _batch_of(5), requested_chunk_size=2)

    assert call_order == ['execute', 'execute', 'execute', 'commit']


@pytest.mark.parametrize('failing_chunk', [1, 2, 3])
@pytest.mark.asyncio
async def test_a_failure_on_any_chunk_persists_nothing(session, call_order: list[str], failing_chunk: int):
    """THE PROPERTY, stated the way it actually matters: a failed batch leaves no bars behind.

    Not "the write is chunked" -- chunking is the mechanism, and chunking alone is a REGRESSION.
    Before this fix an oversized batch failed loudly and stored nothing; a chunked write that
    committed per chunk would store the chunks that happened to succeed and leave a
    store_dataset_entry asserting coverage for the rest. That is the half-written dataset the epic
    exists to remove, and it is harder to detect than an outright failure because the entry looks
    complete.

    All three chunk positions are exercised. The LAST one is the subtlest and the reason this is
    parametrised rather than written once: every preceding chunk has already been sent, so a
    per-chunk commit would have durably stored all but one, and the request would still report
    failure. Each case asserts that no commit was reached at all and that the loop stopped where it
    failed rather than sending the remaining chunks.

    Args:
        session: The recording session, made to fail on one chunk.
        call_order: The sequence of session calls, which is where a premature commit would show.
        failing_chunk: Which statement raises -- first, middle or last of three.
    """
    attempts = 0

    def fail_on_the_nominated_chunk(statement):
        nonlocal attempts
        attempts += 1
        call_order.append('execute')
        if attempts == failing_chunk:
            raise RuntimeError(f'the database rejected chunk {failing_chunk}')

    session.execute.side_effect = fail_on_the_nominated_chunk

    with pytest.raises(RuntimeError):
        await batch_create_market_activity_data(session, _batch_of(5), requested_chunk_size=2)

    assert 'commit' not in call_order, 'a chunk was committed before the whole batch had landed'
    assert call_order == ['execute'] * failing_chunk + ['rollback']


@pytest.mark.asyncio
async def test_a_batch_over_the_protocol_limit_is_chunked_within_one_transaction(session, call_order: list[str]):
    """tj-rpyv5u's acceptance criterion, taken as far as it can be taken without a database.

    This is the test that would have gone red before 8eea874: the pre-fix path built ONE statement
    for the whole batch, and at DERIVED_CEILING + 1 rows that statement carries more bind
    parameters than the wire protocol accepts. The batch size is left unset so the derived ceiling
    governs, which is the configuration the bug report describes.

    The tail chunk is a single row, so this doubles as the dropped-final-chunk case at the real
    boundary rather than at a convenient small one.

    NOT PROVEN, and it is the half that matters operationally: that Postgres ACCEPTS these two
    statements. A mocked session accepts anything, including the oversized statement this file
    asserts is no longer built. Only a live database settles it, and that is tj-vhboky.14's. What
    is proven here is that no statement we build exceeds the documented cap.
    """
    bar_count = DERIVED_CEILING + 1

    written = await batch_create_market_activity_data(session, _batch_of(bar_count))

    assert written == bar_count
    statements = _statements_sent(session)
    assert [len(_timestamps_sent(statement)) for statement in statements] == [DERIVED_CEILING, 1]
    for statement in statements:
        assert len(_bind_params(statement)) <= POSTGRES_MAX_BIND_PARAMETERS, (
            'a chunk still exceeds the wire-protocol bind-parameter limit'
        )
    assert call_order == ['execute', 'execute', 'commit'], 'the oversized batch did not land in one transaction'


# ---------------------------------------------------------------------------------------
# THE GUARD'S SCOPE. The duplicate-timestamp guard runs over the WHOLE batch, before the first
# chunk is built. This is the requirement chunking can quietly break -- fusing the guard into the
# chunk loop as a single-pass tidy-up is an ordinary-looking edit -- and the one the database does
# not backstop.


@pytest.mark.parametrize(
    'minute_offsets',
    [
        # The repeat of t0 is in the second chunk, its first occurrence in the first: no chunk
        # holds a duplicate, so a per-chunk guard raises NOTHING and both rows are sent.
        pytest.param([0, 1, 0, 3], id='the-pair-straddles-the-boundary'),
        # Both copies of t2 sit in the second chunk: a per-chunk guard does raise, but only after
        # the first chunk has gone to the database. This is the case the await counts catch.
        pytest.param([0, 1, 2, 2], id='the-pair-sits-wholly-past-the-first-chunk'),
    ],
)
@pytest.mark.asyncio
async def test_a_duplicate_beyond_the_first_chunk_is_rejected_before_anything_is_sent(
    session, call_order: list[str], minute_offsets: list[int]
):
    """The guard is whole-batch, so a repeat past the first chunk boundary is still caught.

    WHY NO EXISTING CASE REACHES THIS. Every other batch in this file comes from _batch_of(), whose
    timestamps ascend by construction, so no chunk boundary can ever have a duplicate near it.
    Moving `seen_timestamps` inside the chunk loop -- resetting the set per chunk -- leaves this
    whole file and test_bar_batch_guard.py green, because the guard file's duplicate sits inside a
    single chunk, where a per-chunk guard still sees it. That mutation was run against the suite as
    it stood: zero new reds out of 409. Hence this test.

    WHY IT IS NOT MERELY TIDINESS. The single-statement case is self-defending: Postgres raises
    21000, "ON CONFLICT DO UPDATE cannot affect row a second time", when one statement's VALUES
    list repeats a conflict key. Chunking REVERSES that. Two rows at the same timestamp in
    different statements of one transaction do not collide -- the second DO UPDATE overwrites the
    row the first inserted, no error is raised, one row exists where two were counted, and the
    write path still returns len(batch_market_activity). The caller then writes a
    store_dataset_entry claiming coverage it does not have: this epic's stated failure class,
    arriving silently instead of loudly. (That asymmetry is reasoning about the protocol, not
    something this suite can observe -- no database is reachable here. Confirming it on the wire
    belongs to tj-vhboky.14. What is pinned below is the application-level guard, which is the part
    that is actually reachable and the part that has to hold either way.)

    WHY TWO CASES, AND WHY THE await COUNTS ARE NOT DECORATION. Each parameter kills a different
    half of the mutation, and this was measured rather than assumed. On the straddling batch the
    per-chunk guard raises nothing at all -- neither chunk repeats a timestamp internally, so both
    copies of t0 are sent in different statements and the write path returns 4 -- and the
    `pytest.raises` is what goes red. On the second batch the per-chunk guard DOES raise the same
    exception with the same rollback, one chunk too late, and only `execute.await_count == 0` can
    tell that apart from correct behaviour. Asserting the exception alone would have left the
    second half of the requirement -- reject BEFORE anything is built or sent -- unpinned in the
    same way the whole requirement was unpinned before this test existed.

    Args:
        session: The recording session; its await counts are the evidence nothing was sent.
        call_order: The sequence of session calls, expected to hold the rollback and nothing else.
        minute_offsets: Where the repeat falls relative to the chunk boundary at chunk_size=2.
    """
    duplicate_past_the_boundary = _batch_at_minutes(minute_offsets)

    with pytest.raises(DuplicateBatchTimestamp):
        await batch_create_market_activity_data(session, duplicate_past_the_boundary, requested_chunk_size=2)

    assert session.execute.await_count == 0, 'a chunk was sent before the whole batch had been checked for duplicates'
    assert session.commit.await_count == 0, 'a chunk was committed before the whole batch had been checked'
    assert call_order == ['rollback'], 'the rejected batch did not roll back cleanly without touching the database'


# ---------------------------------------------------------------------------------------
# THE WIRING. The literal defect in tj-rpyv5u was not a missing loop, it was a setting READ AND
# DISCARDED at the call site. Every test above would pass with the crud layer perfect and the
# caller still passing nothing.


@pytest.mark.asyncio
async def test_the_worker_hands_the_configured_batch_size_to_the_write_path(monkeypatch: pytest.MonkeyPatch):
    """The defect as tj-rpyv5u actually words it: read at line 16 and then never applied.

    WHY THIS IS A SEPARATE TEST AND NOT COVERED BY THE ONES ABOVE. Delete
    `requested_chunk_size=MARKET_ACTIVITY_BATCH_SIZE` from the call in data_action_request.py and
    nothing else in this file moves: the crud layer keeps its parameter, keeps its resolver, keeps
    its loop, and silently falls back to the derived ceiling on every write. The configured setting
    would once again be read at import and thrown away -- the original bug, restored, with a full
    green suite over it. The seam between the two files is the thing that was broken, so the seam
    gets its own assertion.

    The batch size is monkeypatched to a distinctive in-range value rather than left at its real
    one. The real one is None in a bare pytest run (nothing sets the environment variable and there
    is no conftest), and None is also what the parameter defaults to -- so asserting against None
    would pass against a call that omitted the argument entirely, which is precisely the defect.

    Args:
        monkeypatch: Replaces the module-level setting, already read at import, and the two
            collaborators either side of the call under test.
    """
    configured_batch_size = 1234
    monkeypatch.setattr(data_action_request, 'MARKET_ACTIVITY_BATCH_SIZE', configured_batch_size)
    monkeypatch.setattr(data_action_request, 'upsert_entry', AsyncMock(return_value=uuid4()))
    write_path = AsyncMock(return_value=7)
    monkeypatch.setattr(data_action_request, 'batch_create_market_activity_data', write_path)

    reply = _batch_of(3)
    rpc_client = MagicMock()
    rpc_client.send_request = AsyncMock(return_value=reply)
    rpc_clients = MagicMock()
    rpc_clients.get_client = MagicMock(return_value=rpc_client)

    written = await data_action_request.store_market_activity_worker(
        StoreAssetDatasetPath(asset_type=AssetType.STOCK, data_type=DataType.MARKET_ACTIVITY, asset_symbol='AAPL'),
        StoreAssetDatasetBody(
            owner='a-strategy', source=DataSource.ALPACA_API, granularity=Granularity.ONE_DAY, start=FIRST_TIMESTAMP
        ),
        MagicMock(),
        rpc_clients,
    )

    assert written == 7, 'the worker no longer reports what the write path stored'
    assert write_path.await_args.kwargs['requested_chunk_size'] == configured_batch_size, (
        'the worker is not passing MARKET_ACTIVITY_BATCH_SIZE to the write path -- the setting is '
        'read and discarded again'
    )
