"""THE ORDER AND THE ONE TRANSACTION: what tj-3mk3u5.10 actually buys, pinned (validator).

WHY THIS FILE EXISTS. The cutover's description words its requirement as an ORDER, not as a set of
calls: "1. open the stream; a refusal before the ack becomes the domain error, and NOTHING is
written; 2. on the ACK: upsert the entry; 3. write each PAGE AS IT ARRIVES... Never accumulate the
stream into one batch; 4. on DONE, commit. ALL-OR-NOTHING PER FETCH." Every one of those clauses is
about WHEN something happens relative to something else, and a test that counts calls cannot see a
single one of them. The builder could not reach them through the suite as it stood and proved them
with a throwaway script instead; this file is that evidence, made permanent.

THE ONE CLAIM MOST LIKELY TO BE FAKE-PASSED is "never accumulate". The architect already found that
on the ingest side neither paging test distinguished chunk-as-you-go from accumulate-then-slice --
both arrangements produce identical page shapes, so both went green. The write side has the same
trap: a worker that collected every page into a list and wrote once at the end issues the same
statements, in the same order, with the same contents. WHAT SEPARATES THEM IS INTERLEAVING, and the
only way to observe it is to make the stream lazy and record both sides on one timeline. That is
what data/store/tests/fetch_double.py's journal is for: the double appends to it as it yields and
the session below appends to it as it executes, so a buffered implementation reds
test_every_page_is_written_before_the_next_one_is_pulled with its pages bunched at the front.
Measured, not assumed -- see the mutation log in the verdict note on tj-3mk3u5.10.

THE ORDER MOVED ONCE SINCE, AND THE JOURNALS MOVED WITH IT (validator, gating tj-hywf7w). The
architect gate on tj-3mk3u5.10 priced a cost the cutover's order had introduced: the own-overlap
check lived inside the entry upsert, which runs on the ack, and ingest performs its vendor call
BEFORE yielding that ack -- so a request the store already knew it would refuse had bought a live
fetch and a single-flight slot to find out. 12e1251 hoists the check ahead of the stream, so
``execute Select`` is now the FIRST entry in every journal here rather than the second. These
expectations were changed because the DESIGN changed (tj-8fxxfb iii) and not to make them pass:
each was re-derived from the hoisted order and then re-measured against the defects it originally
guarded -- an inner commit, a lazily-upserted entry, a buffered stream -- all of which still red it.
The superseded order is kept in the case docstrings rather than deleted.

WHAT TIER THIS IS. The session is a recording fake, so commit and rollback are CALLS and not
outcomes. This file proves the worker asks for one commit, after the done, and asks for a rollback
on every failure path -- it does NOT prove Postgres rolled anything back, because nothing here
reaches Postgres. The builder's own scratch script carried the same caveat and it is kept rather
than quietly dropped: a real round trip is CI's System Testing job and the user's host check in
tj-3mk3u5.16, and no assertion below should be read as having done it.
"""

import dataclasses
import inspect
import uuid
from datetime import UTC, datetime
from itertools import pairwise
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import Select
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import Insert as PostgresInsert

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity
from common.errors.vocabulary import ExogenousError, InvalidRequestError, Reason, TraderJoeError
from data.store.app import app_depends
from data.store.app.database.crud.stock import store_dataset_entry as crud
from data.store.app.ingest import data_action_request
from data.store.tests.fetch_double import (
    DEFAULT_FEED,
    FetchScript,
    RecordingFetchClient,
    accepted_stream,
    page,
    served_range_of,
)
from schemas.data_ingest import fetch_dataset
from schemas.data_store.asset_dataset_store import StoreAssetDatasetBody, StoreAssetDatasetPath


pytestmark = pytest.mark.data_store

REQUEST_PATH = StoreAssetDatasetPath(
    asset_type=AssetType.STOCK, data_type=DataType.MARKET_ACTIVITY, asset_symbol='AAPL'
)
REQUEST_BODY = StoreAssetDatasetBody(
    owner='strategy-that-asked',
    source=DataSource.ALPACA_API,
    granularity=Granularity.ONE_DAY,
    start=datetime(2026, 1, 1, tzinfo=UTC),
)


class JournalingSession:
    """An async session that writes every call onto a journal it shares with the fetch double.

    CLASSIFIED BY STATEMENT TYPE, as test_store_dataset_entry_route.py's fake is and for the same
    reason: the question is which KIND of statement was sent and when, and a type check cannot be
    fooled by a formatting change. The overlap probe is the Select, the entry upsert and every bar
    chunk are Inserts.

    It answers the two results the entry upsert reads -- an empty overlap row set, then the id from
    the RETURNING clause -- and an empty result for everything after. A bar chunk's result is never
    read, so there is nothing to make faithful there.
    """

    def __init__(self, journal: list[str], *, entry_id: uuid.UUID):
        self.journal = journal
        self.entry_id = entry_id
        self.statements: list = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement):
        """Record the statement and answer whatever the create path reads next.

        Args:
            statement: The SQLAlchemy statement.

        Returns:
            _Result: The canned answer.
        """
        self.statements.append(statement)
        if isinstance(statement, Select):
            self.journal.append('execute Select')
            return _Result(rows=[])
        if isinstance(statement, PostgresInsert):
            self.journal.append('execute Insert')
            return _Result(scalar=self.entry_id)
        raise AssertionError(f'the dataset path sent an unexpected {type(statement).__name__}')

    async def commit(self) -> None:
        self.commits += 1
        self.journal.append('commit')

    async def rollback(self) -> None:
        self.rollbacks += 1
        self.journal.append('rollback')

    async def close(self) -> None:
        return None


class _Result:
    """One canned answer, with the two accessors the entry upsert uses."""

    def __init__(self, *, rows: list | None = None, scalar: uuid.UUID | None = None):
        self._rows = rows or []
        self._scalar = scalar

    def all(self) -> list:
        return self._rows

    def scalar_one(self) -> uuid.UUID:
        assert self._scalar is not None, 'the create path read an id back from a result that was not given one'
        return self._scalar


def _drive(script: FetchScript) -> tuple[JournalingSession, RecordingFetchClient, list[str]]:
    """Build a session, a fetch double and the journal they share.

    Args:
        script: What the one fetch does.

    Returns:
        tuple: The session, the fetch client and the journal, in that order.
    """
    journal: list[str] = []
    session = JournalingSession(journal, entry_id=uuid.uuid4())
    fetch_client = RecordingFetchClient(script, journal=journal)
    return session, fetch_client, journal


async def _store(session: JournalingSession, fetch_client: RecordingFetchClient) -> data_action_request.StoredDataset:
    """Run the real worker against the two doubles.

    Args:
        session: The recording session; the worker owns the transaction on it.
        fetch_client: The seam.

    Returns:
        StoredDataset: What the worker reports written, and the window it was answered for. It
        returned a bare int until TE-6 (tj-3mk3u5.37.8), which needed the served range to leave
        the transaction so the route could publish it.
    """
    return await data_action_request.store_market_activity_worker(REQUEST_PATH, REQUEST_BODY, session, fetch_client)


def _refusal() -> TraderJoeError:
    """The error the seam raises for a refused ack, before it yields anything.

    Built on the branch the reason table assigns FEED_NOT_AVAILABLE, which is how
    common/rpc/clients/ingest_fetch.py builds it too (``REASONS[reason].branch(...)``): a double that
    raised the base class would be raising something the vocabulary forbids and could pass against a
    worker catching a type production never sees.

    Returns:
        TraderJoeError: A FEED_NOT_AVAILABLE refusal, which ADR tj-fa1rpu makes permanent for the
        request as asked.
    """
    return InvalidRequestError(Reason.FEED_NOT_AVAILABLE, 'the deployment is not entitled to the tape asked for')


# ---------------------------------------------------------------------------------------------
# The order, end to end
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_whole_fetch_is_one_transaction_committed_only_after_the_done():
    """THE CENTRAL CLAIM OF tj-3mk3u5.10, as one readable timeline.

    The journal below is the bead's numbered steps written out: the overlap probe runs first, the ack
    arrives, the entry is upserted, each page is written as it arrives, and the single commit lands
    after the done and after everything else. Asserting the WHOLE sequence rather than its parts is
    deliberate -- every individual clause has a mutation that keeps the parts true and breaks the
    order, and the four that matter most are:
      * moving ``await db.commit()`` inside the page branch: same statements, same counts, but a
        broken stream now leaves a half-written dataset, which is the failure class this epic exists
        to remove;
      * upserting the entry lazily on the first page instead of on the ack: identical for a fetch that
        serves bars, and for an EMPTY fetch it writes no entry at all, so a legitimately empty window
        silently records nothing;
      * buffering the pages (see the next case, which names that one);
      * putting the overlap probe back behind the stream (tj-hywf7w, below).

    THE SELECT MOVED TO INDEX 0, AND THAT IS THE WHOLE POINT OF tj-hywf7w -- it is not a journal
    rewritten to match whatever the code now does. Until the hoist, the probe ran inside
    ``upsert_entry_in_transaction``, which runs on the ack; ingest awaits ``reader.get_bars`` BEFORE it
    yields that ack, so an own-overlap 409 had already bought a live vendor call and held a
    single-flight slot by the time the store could refuse it. The probe depends on nothing the ack
    carries, so it belongs ahead of the stream. Sliding it back to just after ``yield FetchAccepted``
    -- which is what reverting the hoist does -- reds exactly this index, and the two cases below, and
    the two 409 cases in test_store_dataset_entry_route.py that assert ingest was never asked at all.

    EXACTLY ONE COMMIT AND NO ROLLBACK is asserted separately from the sequence, because the sequence
    is an equality and an equality failure names the first divergence rather than the count.
    """
    session, fetch_client, journal = _drive(FetchScript(events=accepted_stream([[0, 1, 2], [3, 4, 5]])))

    written = await _store(session, fetch_client)

    assert written.data_points == 6, f'the worker reported {written.data_points} bars for a stream carrying six'
    assert written.served_range == served_range_of(fetch_client.script), (
        'the StoredDataset does not carry the served range out of the transaction'
    )
    assert journal == [
        'execute Select',
        'yield FetchAccepted',
        'execute Insert',
        'yield BarPage',
        'execute Insert',
        'yield BarPage',
        'execute Insert',
        'yield FetchDone',
        'commit',
    ], f'the fetch did not run in the order tj-3mk3u5.10 specifies. Actual timeline: {journal}'
    assert session.commits == 1, f'the fetch committed {session.commits} times; one fetch is one transaction'
    assert session.rollbacks == 0, 'a fetch that completed rolled its transaction back'


@pytest.mark.parametrize(
    'pages',
    [
        pytest.param([[0], [1], [2], [3]], id='four-single-bar-pages'),
        pytest.param([[0, 1, 2], [3, 4], [5]], id='three-pages-of-decreasing-size'),
    ],
)
@pytest.mark.asyncio
async def test_every_page_is_written_before_the_next_one_is_pulled(pages: list[list[int]]):
    """NEVER ACCUMULATE THE STREAM -- the property, not an arrangement that happens to satisfy it.

    THIS IS THE CLAIM THE PROJECT HAS ALREADY FAKE-PASSED ONCE, one service over: the architect gate
    on the ingest side found that neither paging test could tell chunk-as-you-go from
    accumulate-then-slice, because both produce the same pages. The write side's version of that trap
    is a worker that appends each page to a list and writes the list after the loop. It issues the
    same inserts, with the same rows, in the same order, inside the same single transaction -- and the
    previous case's journal is the only thing in the repository that would notice, which is one
    assertion carrying a load-bearing design decision. This case names the property so the failure
    message says what was lost.

    THE ASSERTION IS A GAP ANALYSIS, not an equality: between any two yielded pages there must be at
    least one write. A buffering worker yields every page first and writes afterwards, so its journal
    has pages adjacent to pages, which is exactly what this detects. It is parametrized over two page
    shapes so that a single-page special case cannot satisfy it.

    WHY IT MATTERS, in the words of the bead the design came from: accumulating "would reinstate the
    single-message memory profile this migration removes" (tj-rh4b7f's third volume ceiling). A
    backfill is the case where it bites, and a backfill is the case no unit test runs.

    Args:
        pages: The scripted page shapes, as minute offsets per page.
    """
    session, fetch_client, journal = _drive(FetchScript(events=accepted_stream(pages)))

    await _store(session, fetch_client)

    page_positions = [index for index, entry in enumerate(journal) if entry == 'yield BarPage']
    assert len(page_positions) == len(pages), f'not every scripted page reached the worker: {journal}'
    for first, second in pairwise(page_positions):
        assert 'execute Insert' in journal[first + 1 : second], (
            f'two pages were pulled off the stream with no write between them, so the stream is being '
            f'ACCUMULATED rather than written page by page. That reinstates the whole-dataset memory '
            f'profile the paged transport exists to remove. Timeline: {journal}'
        )
    assert journal[-1] == 'commit', f'the commit is not the last thing that happened: {journal}'
    assert journal.index('commit') > page_positions[-1], 'the fetch committed before its last page arrived'


# ---------------------------------------------------------------------------------------------
# All or nothing: the three ways a fetch fails
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_refusal_before_the_ack_writes_nothing_at_all():
    """Step 1 of the bead: "a refusal before the ack becomes the domain error, and NOTHING is written".

    The refusal arrives as a TraderJoeError raised by the seam before it yields anything -- on the
    wire it is the ack's refused arm and the call still ends OK (tj-tkm4tn D3), which is why it cannot
    be observed as a transport failure here and is scripted as the raise it becomes.

    NO WRITE STATEMENT is the assertion that matters, and it is stronger than "no commit". A worker
    that upserted the entry before opening the stream would still roll back here and would still
    answer an error, so a commit-count assertion alone would pass -- while a real deployment,
    against a real database, would have written a row for a fetch that was refused outright. The
    rollback is asserted too, because the transaction is opened before the stream is and something
    has to close it.

    "NOTHING IS WRITTEN" NOW MEANS NO WRITE STATEMENT, NOT NO STATEMENT (validator ruling, gating
    tj-hywf7w). This case used to assert ``session.statements == []``, and that spelling is no longer
    available: the own-overlap probe is hoisted ahead of the stream on purpose, so it runs before the
    refusal is known and it runs for a request that is about to be refused. The pin is REFINED rather
    than weakened, and the refinement is the honest reading of the clause it came from -- the bead
    says "NOTHING IS WRITTEN", and a SELECT writes nothing. What it costs is bounded and stated: this
    case no longer notices a read added ahead of the stream. What it keeps is the whole of what it
    was for, because the defect it guards is an INSERT, and the two assertions below are strictly
    tighter than the old one was on that defect:
      * no PostgresInsert at all -- the entry upsert moved ahead of the stream reds here, exactly as
        it did before;
      * the one statement that IS sent is the hoisted Select and nothing else, so a second read, a
        third, or any statement the dataset path has no business issuing reds here too. An
        unconditional ``== []`` could not have said that; it could only have said "none".
    """
    session, fetch_client, journal = _drive(FetchScript(raises=_refusal()))

    with pytest.raises(TraderJoeError) as raised:
        await _store(session, fetch_client)

    assert raised.value.reason is Reason.FEED_NOT_AVAILABLE, 'the refusal reached the caller as a different error'
    written = [statement for statement in session.statements if isinstance(statement, PostgresInsert)]
    assert written == [], (
        f'a fetch refused before its ack still sent {len(written)} WRITE statement(s) to the database. '
        f'Nothing may be written for a fetch that was never accepted.'
    )
    assert [type(statement).__name__ for statement in session.statements] == [Select.__name__], (
        f'a refused fetch sent {[type(s).__name__ for s in session.statements]}. Exactly one statement '
        f'is expected and it is the hoisted own-overlap Select (tj-hywf7w), which is a read.'
    )
    assert session.commits == 0, 'a refused fetch committed'
    assert journal == ['execute Select', 'raise InvalidRequestError', 'rollback'], (
        f'unexpected timeline for a refusal: {journal}'
    )


@pytest.mark.asyncio
async def test_a_stream_that_breaks_after_a_page_commits_nothing_and_rolls_back():
    """ALL-OR-NOTHING PER FETCH: the entry and the bars already written are abandoned together.

    This is the case the bead calls "the honest state until the ledger exists": without a coverage
    ledger there is nowhere to record that some bars arrived and others did not, so a partial write
    would leave a dataset entry claiming coverage it does not have. It is also, as the commit message
    says, what makes the seam's per-page feed check safe -- the seam fails at the offending page
    without recalling earlier ones, and this rollback is what abandons them.

    THE STATEMENTS ARE ASSERTED TO HAVE HAPPENED, which is the half that keeps the case honest: a
    worker that wrote nothing until the done would pass a no-commit assertion trivially and would
    also violate the page-by-page requirement. The point is that writes DID go out and were then
    thrown away, not that none went out.
    """
    # PEER_PROTOCOL_ERROR on the ExogenousError branch, which is exactly what the seam's
    # _protocol_error() raises for "the FetchDataset stream ended without a done".
    broken = ExogenousError(Reason.PEER_PROTOCOL_ERROR, 'the FetchDataset stream ended without a done')
    events = [fetch_dataset.FetchAccepted(feed=DEFAULT_FEED), page([0, 1])]
    session, fetch_client, journal = _drive(FetchScript(events=events, raises=broken))

    with pytest.raises(TraderJoeError):
        await _store(session, fetch_client)

    assert journal == [
        'execute Select',
        'yield FetchAccepted',
        'execute Insert',
        'yield BarPage',
        'execute Insert',
        'raise ExogenousError',
        'rollback',
    ], f'a broken stream did not abandon its work in the expected order: {journal}'
    assert session.commits == 0, 'a stream that broke before its done committed what it had written'
    assert session.rollbacks == 1, 'a broken stream did not roll its transaction back exactly once'


@pytest.mark.asyncio
async def test_an_accepted_but_empty_window_still_writes_its_entry_and_commits():
    """A served-but-empty fetch is a SUCCESS carrying provenance, not a failure (ADR tj-fa1rpu D2).

    It is the case that separates "the entry is upserted on the ack" from "the entry is upserted
    lazily when the first page arrives" -- the second is indistinguishable from the first on every
    other case in this file, and here it writes no entry at all. A caller that asked for a window the
    vendor genuinely has no bars for gets a dataset entry recording that it asked, which is what makes
    the answer idempotent on a retry.
    """
    session, fetch_client, journal = _drive(FetchScript(events=accepted_stream()))

    written = await _store(session, fetch_client)

    assert written.data_points == 0, f'an empty window reported {written.data_points} bars written'
    assert written.served_range == served_range_of(fetch_client.script), (
        'a served-but-empty window must still carry the range the vendor answered for: it is the '
        'only thing distinguishing it from a window nobody fetched (tj-lldllr)'
    )
    assert journal == ['execute Select', 'yield FetchAccepted', 'execute Insert', 'yield FetchDone', 'commit'], (
        f'an empty but accepted fetch did not write and commit its entry: {journal}'
    )


# ---------------------------------------------------------------------------------------------
# What the store stamps on the bars, and what it does not send
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_bars_carry_the_acks_feed_and_the_entry_id_the_store_itself_minted(monkeypatch: pytest.MonkeyPatch):
    """Two values that cross from different sides, and neither may be invented locally.

    THE FEED COMES FROM THE ACK. It is "the one value only ingest may decide" and the store has no
    business defaulting it: a worker that stamped ``Feed.SIP`` because that is what the other test
    builders use would write IEX bars under the SIP tape, and feed is part of the bar's natural key
    precisely because an IEX bar and a SIP bar for the same minute are different numbers (tj-u12tjo.11).
    The ack is scripted with IEX so a hard-coded SIP reds this.

    THE dataset_id COMES FROM THE STORE'S OWN UPSERT, and it is asserted to be the id the upsert
    RETURNED rather than merely a uuid. A data_store row id must not cross the wire to ingest (ADR
    tj-8konfu D7.2), so there is no id on the page to copy and the only correct source is the entry
    the ack's upsert just created. A fresh ``uuid4()`` per page would satisfy the model and orphan
    every bar from its entry.

    The write collaborator is replaced so the batch can be read as the worker built it; what the crud
    layer then does with a batch is test_bar_batch_chunking.py's subject.

    Args:
        monkeypatch: Replaces the worker's two write collaborators.
    """
    entry_id = uuid.uuid4()
    monkeypatch.setattr(data_action_request, 'upsert_entry_in_transaction', AsyncMock(return_value=entry_id))
    write_path = AsyncMock(return_value=2)
    monkeypatch.setattr(data_action_request, 'write_market_activity_in_transaction', write_path)

    session, fetch_client, _ = _drive(FetchScript(events=accepted_stream([[0, 1]], feed=Feed.IEX)))
    await _store(session, fetch_client)

    batch = write_path.await_args.args[1]
    assert batch.feed is Feed.IEX, (
        f'the bars were written under feed {batch.feed!r} rather than under the tape the ack resolved '
        f'({Feed.IEX!r}). feed is part of the bar natural key, so this silently collides two tapes.'
    )
    assert batch.dataset_id == entry_id, (
        f'the bars were stamped with {batch.dataset_id!r} rather than with the id the entry upsert '
        f'returned ({entry_id!r}), so they belong to no entry this fetch created'
    )
    assert len(batch.dataset[DataType.MARKET_ACTIVITY]) == 2, 'the page did not reach the write path whole'


# ---------------------------------------------------------------------------------------------
# THE TWO FEEDS: the caller's PREFERENCE on the body, and the RESOLVED tape off the ack
# (tj-3mk3u5.31, closing tj-f2qz44; decision tj-xn3qa6)
#
# THESE TWO VALUES SHARE A NAME AND A TYPE, which is the whole hazard. StoreAssetDatasetBody.feed is
# `Feed | None` and is what the caller ASKED FOR; AssetDatasetStoreCreate.feed is required and is the
# tape ingest RESOLVED, read off FetchAccepted. Both are valid Feed members, so nothing at the schema
# layer can tell them apart, and the entry's feed is an IDENTITY column -- a preference written there
# cannot be corrected later without merging two datasets.
#
# WHY EVERY TEST HERE MAKES THEM DISAGREE. A case where the body and the ack name the same tape
# cannot distinguish a correct implementation from one that writes the preference: both produce the
# same row. So the body prefers IEX and the ack resolves SIP throughout this section, and each test
# says which of the two it expects to see where.
#
# THE REGRESSION IS NOT THE LOUD ONE IT LOOKS LIKE. A body naming NO tape -- which is almost every
# caller -- makes the wrong construction raise a ValidationError, so the common case fails visibly.
# A body that DID name one validates cleanly and records the preference silently. The visible half is
# already covered by every other test in this file, whose REQUEST_BODY carries no feed; this section
# is the invisible half.
# ---------------------------------------------------------------------------------------------

# The caller's PREFERENCE. IEX, against the SIP the ack resolves below.
PREFERRED_FEED = Feed.IEX
# The tape the ack resolves. SIP, so it cannot coincide with the preference OR with
# fetch_double.DEFAULT_FEED, which is IEX.
RESOLVED_FEED = Feed.SIP

BODY_PREFERRING_A_TAPE = StoreAssetDatasetBody(
    owner='strategy-that-asked',
    source=DataSource.ALPACA_API,
    granularity=Granularity.ONE_DAY,
    start=datetime(2026, 1, 1, tzinfo=UTC),
    feed=PREFERRED_FEED,
)


def _entry_insert_params(session: JournalingSession) -> dict:
    """The bound values of the ENTRY insert, as Python objects.

    The entry upsert is the FIRST Insert the path sends -- it runs on the ack, before any page -- and
    taking the first rather than the only one is deliberate: a bar chunk is an Insert too, so
    requiring exactly one would make this helper depend on the fetch being empty.

    Args:
        session: The recording session the worker wrote through.

    Returns:
        dict: Column name to bound value.
    """
    inserts = [statement for statement in session.statements if isinstance(statement, PostgresInsert)]
    assert inserts, 'the dataset path sent no insert at all, so there is no entry to read a tape off'
    return inserts[0].compile(dialect=postgresql.dialect()).params


@pytest.mark.asyncio
async def test_the_entry_records_the_acks_resolved_tape_and_not_the_bodys_preference():
    """THE TEST tj-f2qz44 TURNS ON, and the one a same-tape case cannot be.

    The body prefers IEX and the ack resolves SIP. SIP is what the entry row must carry, because the
    entry records WHAT WAS SERVED and not what was wanted: ingest resolves the tape it is entitled
    to and can only CHECK a named preference against it, never be steered by one (tj-3mk3u5.22 Q5),
    so a preference it cannot honour comes back as a REFUSED ACK rather than as a different tape. An
    entry carrying IEX here would be a coverage ledger claiming an IEX window that holds SIP bars --
    the exact lie tj-f2qz44 was filed about, one table over from the one it was fixed on.

    THE REAL upsert RUNS. The feed is read off the bound values of the statement the path actually
    sends, so this covers the whole chain -- OverlapKey.extend, AssetDatasetStoreCreate's required
    feed, StoreDatasetEntry.get_fields and the insert's VALUES -- rather than a collaborator's
    arguments. A mocked upsert would be green against a worker that built the entry correctly and
    then bound the wrong column.

    NOT PROVED HERE: that Postgres stores it. This whole file is statement-level (see the module
    docstring); the live half ran as `alembic upgrade head` plus a seed dump at the gate, and CI's
    System Testing job is what keeps it true.
    """
    session, fetch_client, _ = _drive(FetchScript(events=accepted_stream([[0, 1]], feed=RESOLVED_FEED)))

    await data_action_request.store_market_activity_worker(REQUEST_PATH, BODY_PREFERRING_A_TAPE, session, fetch_client)

    bound = _entry_insert_params(session)
    assert 'feed' in bound, 'the entry insert binds no feed at all, so the row cannot say which tape covered it'
    assert bound['feed'] is RESOLVED_FEED, (
        f'the entry records {bound["feed"]!r}, which is the body PREFERENCE, not the {RESOLVED_FEED!r} '
        f'the ack RESOLVED. feed is an identity column, so this merges two datasets that asked for '
        f'different tapes and no later correction can undo it (tj-f2qz44, tj-xn3qa6 D1).'
    )


@pytest.mark.asyncio
async def test_the_bodys_preference_travels_outward_on_the_fetch_request():
    """The other half of the same claim, and the reason the first one is not just "ignore the body".

    The preference is NOT dead weight: it legitimately travels OUT, on FetchDatasetRequest, where
    ingest checks it against the tape it is entitled to. So the correct behaviour is a one-way
    split -- the preference goes out, the resolution comes back -- and a worker that simply dropped
    the body's feed would pass the test above while silently making a named tape unrequestable.

    ASSERTED ON THE SAME RUN SHAPE AS THE TEST ABOVE, with the two values disagreeing, so neither
    assertion can be satisfied by the other's value.
    """
    session, fetch_client, _ = _drive(FetchScript(events=accepted_stream([[0, 1]], feed=RESOLVED_FEED)))

    await data_action_request.store_market_activity_worker(REQUEST_PATH, BODY_PREFERRING_A_TAPE, session, fetch_client)

    (request,) = fetch_client.requests
    assert request.feed is PREFERRED_FEED, (
        f'the fetch request carried {request.feed!r} rather than the caller preference '
        f'{PREFERRED_FEED!r}, so a caller naming a tape is not asking ingest for it at all'
    )


@pytest.mark.asyncio
async def test_a_body_naming_no_tape_sends_none_outward_and_still_records_the_resolved_one():
    """The ordinary case, kept adjacent so the split above is not mistaken for special handling.

    None on the way out means "the deployment decides", which is what almost every caller sends and
    the only thing it could mean while ingest alone resolves the tape. The entry still records a
    real tape, because the ack carries one either way. Without this case, an implementation that
    forwarded the preference ONLY when present and defaulted the entry from the same field would
    look correct in both directions above.
    """
    session, fetch_client, _ = _drive(FetchScript(events=accepted_stream([[0]], feed=RESOLVED_FEED)))

    await _store(session, fetch_client)

    (request,) = fetch_client.requests
    assert REQUEST_BODY.feed is None, 'this file REQUEST_BODY now names a tape, so this case proves nothing'
    assert request.feed is None, f'a body naming no tape sent {request.feed!r} outward instead of None'
    assert _entry_insert_params(session)['feed'] is RESOLVED_FEED, (
        'a body naming no tape did not record the tape the ack resolved'
    )


def test_the_entry_builder_cannot_reach_the_request_body():
    """THE STRUCTURAL GUARD, which is what actually closed the hazard rather than the tests above.

    Scope is the only guard available here: the two feeds are the same type, so no signature, no
    validator and no type checker can tell a preference from a resolution. The builder's answer was
    to put the entry's construction inside a helper the body is NOT A PARAMETER OF -- `accepted.feed`
    is then the only Feed reachable from in there, and writing the preference onto an identity
    column stops being a thing a careless edit can do and becomes a thing that needs a new argument.

    SO THIS IS THE TEST THAT HOLDS WHEN THE OTHERS ARE DELETED, and it is cheap for what it buys:
    the behavioural cases above pin one build of the worker, while this pins the property that makes
    the build hard to get wrong. Asserted as "no parameter of this helper is the body type", not as
    an exact parameter list, so a legitimate new argument does not redden it.
    """
    parameters = inspect.signature(data_action_request._entry_on_ack).parameters

    assert 'request_body' not in parameters, (
        'the entry builder takes the request body again, so the caller PREFERENCE is in scope at the '
        'one place the RESOLVED tape is written onto an identity column (tj-3mk3u5.31)'
    )
    body_typed = [
        name
        for name, parameter in parameters.items()
        if parameter.annotation in (StoreAssetDatasetBody, 'StoreAssetDatasetBody')
    ]
    assert body_typed == [], f'the entry builder can reach a request body through {body_typed}'
    assert 'accepted' in parameters, (
        'the entry builder no longer takes the acknowledgement, so it cannot be reading the resolved tape off it'
    )


def test_the_overlap_key_the_worker_builds_carries_no_tape():
    """Why the entry cannot simply be built before the stream, pinned at the seam that decides it.

    The worker builds an OverlapKey before it opens the stream and EXTENDS it into the entry on the
    ack (tj-xn3qa6 D3). That shape exists because the refusal must run before the vendor call
    (tj-hywf7w) while the entry's feed does not exist until the ack -- three things that could not
    all hold, and this is which one gave way.

    A FEED FIELD ON OverlapKey WOULD UNDO BOTH HALVES AT ONCE: the refusal would start keying on a
    value only FetchAccepted carries, so it could no longer run before the stream, and the entry
    would gain a second place its tape could come from. data/store/tests/test_dataset_entry_identity.
    py pins the type; this pins that the WORKER is the caller relying on it.
    """
    key_fields = {field.name for field in dataclasses.fields(crud.OverlapKey)}

    assert 'feed' not in key_fields, (
        'the overlap key carries a tape, so the pre-stream refusal depends on a value only the ack '
        'carries (tj-xn3qa6 D1)'
    )
    assert 'start' in key_fields and 'end' in key_fields, (
        f'the overlap key has lost the range, so the assertion above is about some other type: {sorted(key_fields)}'
    )


# ---------------------------------------------------------------------------------------------
# The dependency the route resolves the seam through
# ---------------------------------------------------------------------------------------------


def test_get_ingest_fetch_client_hands_out_the_client_the_lifespan_built(monkeypatch: pytest.MonkeyPatch):
    """The new FastAPI dependency reads the global the lifespan sets, and not some other one.

    WHY IT NEEDS ITS OWN CASE. Every other test of the POST route OVERRIDES this dependency, which is
    the correct way to drive a route and also means none of them executes a line of it. The lifespan
    that sets the global cannot run here either -- it opens a channel to data_ingest and waits for
    Kafka -- so test_http_smoke.py only ever observes the un-initialised ``None``. Between those two,
    the function could return the Kafka clients, or a fresh ``GrpcIngestFetchClient`` per call, or
    ``None`` unconditionally, and the suite would not move. In production that is the difference
    between the route having a peer and the route raising ``AttributeError`` on ``None``.

    The global is set directly rather than by running the lifespan: private name mangling applies
    inside class bodies only, so at module scope the name is literally ``__INGEST_FETCH_CLIENT``;
    ``raising=True`` (the default) is what makes that a claim rather than an assumption, since a
    misspelling would create a new attribute nobody reads and the case would go green on the ``None``
    it started with. monkeypatch restores it, and running the lifespan is what this case exists to
    avoid.

    Args:
        monkeypatch: Sets and restores the module global.
    """
    assert app_depends.get_ingest_fetch_client() is None, (
        'the store lifespan has already run in this process, so this case cannot tell what the dependency reads'
    )

    sentinel = RecordingFetchClient()
    monkeypatch.setattr(app_depends, '__INGEST_FETCH_CLIENT', sentinel)

    assert app_depends.get_ingest_fetch_client() is sentinel, (
        'get_ingest_fetch_client does not read the module global the lifespan sets, so the route '
        'would not be handed the client the application built'
    )
