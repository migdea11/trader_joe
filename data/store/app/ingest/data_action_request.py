"""THE DATASET PATH: one FetchDataset stream, one transaction (tj-3mk3u5.10, the Kafka cutover).

THE ORDER IS THE POINT, and it is the whole reason this file is shaped the way it is:

    0. refuse an own-overlap FIRST, before the stream is opened (tj-hywf7w). It is one local
       SELECT over this store's own rows and it depends on nothing the fetch returns, while the
       stream's first step is a live vendor call holding a rate-budget slot: ingest awaits
       reader.get_bars BEFORE it yields the ack. Checking it inside the entry upsert, where it
       used to live, meant a request the store already knew it would refuse had bought a vendor
       fetch and blocked a concurrent legitimate fetch for the same key to find that out.
    1. open the stream. A refusal arrives INSIDE the ack and the call still ends OK, so the seam
       raises it as the domain error BEFORE yielding anything -- and nothing is written.
    2. on the ACCEPTED ack, upsert the dataset entry. The ack carries the resolved feed, which is
       the one value only ingest may decide.
    3. write each PAGE AS IT ARRIVES, stamping this store's dataset_id on its bars, through the
       chunked bar write. The stream is never accumulated: holding a whole backfill to write it
       once would reinstate the single-message memory profile this migration removes (tj-rh4b7f's
       third volume ceiling).
    4. on FetchDone, commit.

ALL-OR-NOTHING PER FETCH. The entry upsert and every page share ONE transaction, committed only
when the stream completes, so a fetch that breaks, times out or is cancelled leaves neither a new
entry nor partial bars. That is the honest state until the coverage ledger exists (tj-3mk3u5.34):
without a ledger there is nowhere to record "these bars arrived and those did not", so a partial
write would be a dataset entry claiming coverage it does not have. An entry that ALREADY EXISTED
before the call is untouched by the rollback -- the upsert only refreshes its expiry and
updated_at, and the rollback undoes that refresh.

THE SEAM'S FAILURE AND THIS TRANSACTION ARE TWO HALVES OF ONE DESIGN. common/rpc/clients/
ingest_fetch.py fails a page whose bars disagree with the ack's feed at the offending page and
does not recall pages already yielded; that is safe only because the write those earlier pages
went into is abandoned here.

A TraderJoeError out of the seam PROPAGATES, and that is now the whole mechanism rather than a
placeholder. It is caught neither here nor in the route (D6: no route catches and swallows), so it
leaves this transaction -- which rolls back, writing nothing -- and the handler set installed in
data/store/app/main.py renders it as problem+json with the status its reason's row names: a refused
ack as 422 FEED_NOT_AVAILABLE, a rate limit as 429 with Retry-After and reset_at, a vendor or peer
that is not there as 503, a deadline as 504, and a peer that answered INTERNAL or unintelligibly as
502. A FAILED FETCH IS THEREFORE DISTINGUISHABLE FROM AN EMPTY ONE, which is tj-fe19tu's criterion:
a served window with no bars is a 200 with data_points 0, never a failure. There is no automatic
retry here either -- the writes are idempotent (tj-3mk3u5.3), so the caller's own retry is safe, and
ledger-driven reconnect belongs with the ledger.

THE FETCH DOES NOT CARRY A dataset_id. Correlation belongs to the transport and the store stamps
its own rows (ADR tj-8konfu D7.2); nothing here builds a GetDatasetRequest any more.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_select import DataType
from common.enums.data_stock import Feed
from common.environment import get_env_var
from common.logging import get_logger
from common.rpc.clients.ingest_fetch import IngestFetchClient
from data.store.app.database.crud.stock.asset_market_activity import write_market_activity_in_transaction
from data.store.app.database.crud.stock.store_dataset_entry import (
    OverlapKey,
    check_own_overlap,
    upsert_entry_in_transaction,
)
from data.store.app.database.transaction import write_transaction
from schemas.data_ingest import fetch_dataset
from schemas.data_store.asset_dataset_store import AssetDatasetStoreCreate, StoreAssetDatasetBody, StoreAssetDatasetPath
from schemas.data_store.stock.market_activity_data import (
    BatchStockDataMarketActivityCreate,
    StockDataMarketActivityData,
)


if TYPE_CHECKING:
    from datetime import datetime

log = get_logger(__name__)

MARKET_ACTIVITY_BATCH_SIZE = get_env_var('MARKET_ACTIVITY_BATCH_SIZE', cast_type=int)


@dataclass(frozen=True)
class StoredDataset:
    """What one completed fetch wrote, and the window it was actually answered for.

    THE served_range IS CARRIED OUT OF THE TRANSACTION so the route can put it in the response
    (user ruling 2026-10-02; schemas/data_store/asset_dataset_store.py StoreAssetDatasetResponse).
    The worker used to return a bare row count, which left the one piece of provenance the caller
    needs -- what the vendor answered for, as opposed to what was asked -- reachable only in a log
    line. A misspelled symbol is served, empty, for the window asked (tj-lldllr), and that is now
    visible to the client rather than only to us.

    COPIED UNCHANGED, never rebuilt from the request and never clamped again here: the clamp is
    ingest's (end = min(requested end, as_of)), and a store that recomputed it would publish its own
    guess as the vendor's answer.

    Attributes:
        data_points: How many bars were written.
        served_range: The window the fetch's FetchDone named, exactly as it arrived.
    """

    data_points: int
    served_range: fetch_dataset.ServedRange


def _fetch_request(
    request_path: StoreAssetDatasetPath, request_body: StoreAssetDatasetBody
) -> fetch_dataset.FetchDatasetRequest:
    """Build the fetch from the caller's request.

    TRANSPORT PARITY: this asks for the range the caller asked for, exactly as the Kafka request
    did. Driving the request from the coverage ledger -- "ask for exactly what is missing" -- is
    tj-3mk3u5.34, which needs a ledger that does not exist yet.

    Spelled out field by field rather than splatted. FetchDatasetRequest is an InboundContract, so
    it forbids extras, and the body carries three fields it does not declare (expiry, expiry_type
    and the path's data_type, which becomes the data_types list).

    THE BODY'S feed IS WHAT TRAVELS, AND IT IS A PREFERENCE, NOT A VALUE WRITTEN. tj-3mk3u5.30 made
    it an accepted create-request field, so a caller can now name one; None still means "the
    deployment decides" and is what almost every caller sends. Ingest resolves the tape it is
    entitled to and can only CHECK a named feed against it, never be steered by one (tj-3mk3u5.22
    Q5), so a preference it cannot honour comes back as a refused ack rather than as a different
    tape. The value the ENTRY records is the other one -- the RESOLVED feed off FetchAccepted --
    and the two must never be confused; see AssetDatasetStoreCreate's docstring.

    Args:
        request_path: The asset and data type being stored.
        request_body: What the caller asked for.

    Returns:
        fetch_dataset.FetchDatasetRequest: The fetch, in domain terms.
    """
    return fetch_dataset.FetchDatasetRequest(
        owner=request_body.owner,
        source=request_body.source,
        feed=request_body.feed,
        asset_symbol=request_path.asset_symbol,
        asset_type=request_path.asset_type,
        data_types=[request_path.data_type],
        granularity=request_body.granularity,
        start=request_body.start,
        end=request_body.end,
        update_type=request_body.update_type,
    )


def _entry_on_ack(
    overlap_key: OverlapKey, expiry: 'datetime', accepted: fetch_dataset.FetchAccepted
) -> AssetDatasetStoreCreate:
    """Build the dataset entry from the ack, at the one moment a tape exists to write.

    THE REQUEST BODY IS NOT A PARAMETER, AND THAT IS THE ENTIRE POINT OF THIS FUNCTION EXISTING.
    StoreAssetDatasetBody.feed and AssetDatasetStoreCreate.feed share a name AND a type, so nothing
    at the schema layer can tell a caller's PREFERENCE from the RESOLVED tape -- both are valid Feed
    members. The failure that follows from mixing them up is not the loud one it looks like: a body
    that named NO tape makes the wrong construction raise, so the common case fails visibly, but a
    body that DID name one validates cleanly and silently records the preference on an identity
    column, where no later correction can rewrite it (flagged by the validator at the tj-3mk3u5.30
    gate). Scope is the only guard available, so this function takes the ack and the overlap key and
    nothing else: `request_body` is not reachable from in here, and `accepted.feed` is the only Feed
    that is.

    It is a strict EXTENSION of the overlap key (tj-xn3qa6 D3), never a second splat of
    model_dump() and never model_copy(update=...), which does not re-validate (D4).

    Args:
        overlap_key: What check_own_overlap was given, before the stream was opened.
        expiry: When this dataset's data dies. Not identity, so not on the key.
        accepted: The acknowledgement, carrying the tape ingest resolved.

    Returns:
        AssetDatasetStoreCreate: The entry, as it will be written.
    """
    return overlap_key.extend(expiry=expiry, feed=accepted.feed)


def _page_batch(
    request_path: StoreAssetDatasetPath,
    request_body: StoreAssetDatasetBody,
    dataset_id: UUID,
    feed: Feed,
    page: fetch_dataset.BarPage,
) -> BatchStockDataMarketActivityCreate:
    """Turn one page of the stream into the batch the bar writer takes.

    THE STORE STAMPS dataset_id, not ingest: the page carries none, because a data_store row id
    must not cross that wire (ADR tj-8konfu D7.2).

    THE FEED COMES FROM THE ACK, not from the bars. The seam has already refused any page whose
    bars disagree with it, so the two cannot differ here; reading it off the ack keeps this the
    one resolved value for the whole fetch, the way the batch model declares it.

    volume and trade_count are forwarded as the contract gives them, which is what the Kafka path
    did: StockDataMarketActivityData declares both as required ints, so a vendor bar with no trade
    count fails validation exactly as it does today (data/ingest/app/brokers/interface.py already
    types it int | None). Parity, deliberately -- inventing a zero here would put a sentinel on a
    real bar, which is the thing the "no sentinel for we do not know" ruling removed.

    Args:
        request_path: The asset being stored.
        request_body: The identity fields the entry was written under.
        dataset_id: The entry the ack's upsert returned.
        feed: The resolved feed from the ack.
        page: The page of bars.

    Returns:
        BatchStockDataMarketActivityCreate: The page, ready for the bar writer.
    """
    batch = BatchStockDataMarketActivityCreate(
        dataset_id=dataset_id,
        asset_symbol=request_path.asset_symbol,
        source=request_body.source,
        granularity=request_body.granularity,
        feed=feed,
        dataset={},
    )
    for bar in page.bars:
        batch.append_data(
            DataType.MARKET_ACTIVITY,
            StockDataMarketActivityData(
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                trade_count=bar.trade_count,
            ),
            # bar_start, the instant the bar OPENS. The stored column is named timestamp; the
            # contract refuses that name on purpose, so the data time and a response time can
            # never collapse into one (ADR tj-r6vcgv B1).
            bar.bar_start,
        )
    return batch


async def store_market_activity_worker(
    request_path: StoreAssetDatasetPath,
    request_body: StoreAssetDatasetBody,
    db: AsyncSession,
    fetch_client: IngestFetchClient,
) -> StoredDataset:
    """Fetch one dataset over the FetchDataset seam and write it, entry and bars, in one transaction.

    See this module's docstring for the order and why it is the order.

    THE GRAMMAR IS NOT RE-POLICED HERE. The seam enforces it: the ack is always first, a stream
    that ends without a FetchDone raises rather than returning a short stream as a success, and
    every departure is PEER_PROTOCOL_ERROR (tj-tkm4tn D4 -- a state machine here would be a
    second, weaker copy of that one). So the ack has set dataset_id and feed before any page can
    arrive. A test double that broke that rule would fail model validation on the page below,
    inside this transaction, and write nothing.

    Args:
        request_path: The asset and data type being stored.
        request_body: What the caller asked for; also the entry's identity fields.
        db: The request's session. This function owns the transaction on it.
        fetch_client: The seam, as the INTERFACE -- a replay backend or a test double is an
            ordinary implementation of it (ADR tj-8konfu D3).

    Returns:
        StoredDataset: How many bars were written, and the window the fetch was answered for.

    Raises:
        TraderJoeError: A refused ack (before anything is written), a failed call, a stream that
            breaks the contract, or a database failure out of write_transaction. Deliberately not
            caught here or in the route: the edge renders it as problem+json (D6).
        OwnOverlapConflict: If the entry overlaps one of the same owner's datasets; the edge
            renders it as a 409 carrying colliding_ids. Raised before the stream is opened, so no
            vendor call is made for it (step 0 of the module docstring's order).
    """
    request = _fetch_request(request_path, request_body)
    # ONE OVERLAP KEY FOR THE WHOLE FETCH, built here and EXTENDED into the entry on the ack
    # (tj-xn3qa6 D3). What the check below and the write on the ack share is now exactly the part
    # that must agree -- the ten identity fields minus the tape -- rather than a whole entry that
    # the check only keyed on a subset of. The entry cannot be built here at all: its feed is
    # required and RESOLVED, and nothing has resolved one until FetchAccepted arrives.
    overlap_key = OverlapKey(
        owner=request_body.owner,
        asset_type=request_path.asset_type,
        asset_symbol=request_path.asset_symbol,
        data_type=request_path.data_type,
        source=request_body.source,
        granularity=request_body.granularity,
        expiry_type=request_body.expiry_type,
        update_type=request_body.update_type,
        start=request_body.start,
        end=request_body.end,
    )
    # Set by the ack, read by every page after it. Declared here only so the two are visibly one
    # fetch's state and not rebuilt per page.
    dataset_id: UUID | None = None
    feed: Feed | None = None
    item_count = 0
    # The stream's terminator, kept so its served_range leaves the transaction with the row count.
    done: fetch_dataset.FetchDone | None = None

    async with write_transaction(db, 'fetch and store dataset'):
        # Step 0, INSIDE the transaction but BEFORE the stream: inside, so the refusal rolls back
        # on the way out like every other failure on this path and nothing is left open; before,
        # so the fetch is never issued. fetch() below is an async generator -- it does no work
        # until the first pull -- so raising here means ingest is never asked at all.
        await check_own_overlap(db, overlap_key)

        async for event in fetch_client.fetch(request):
            match event:
                case fetch_dataset.FetchAccepted():
                    feed = event.feed
                    log.debug(f'Fetch accepted on feed {feed}; upserting the dataset entry')
                    # THE ENTRY IS BUILT HERE, AND ONLY HERE, because this is the first moment a
                    # tape exists to write (tj-3mk3u5.31, closing tj-f2qz44). feed is identity on
                    # the entry now, so a placeholder written earlier and corrected later would
                    # MUTATE identity and silently merge two datasets that asked for different
                    # tapes -- which is exactly why tj-rh4b7f deferred the column until an
                    # acknowledgement could carry the resolved value.
                    #
                    # EXTENDING THE OVERLAP KEY, not splatting model_dump() a second time: the
                    # entry is a STRICT EXTENSION of what the check above was given, so the two
                    # cannot disagree about the dataset by construction. Not model_copy either --
                    # it does not re-validate, and feed is the one field this task exists to make
                    # trustworthy (tj-xn3qa6 D3, D4).
                    #
                    # THE ACK IS HANDED OVER WHOLE, so the tape is read inside a function the
                    # request body cannot be reached from: the body's feed is a PREFERENCE of the
                    # same type and would validate silently here. See _entry_on_ack.
                    entry = _entry_on_ack(overlap_key, request_body.expiry, event)
                    dataset_id = await upsert_entry_in_transaction(db, entry)
                case fetch_dataset.BarPage():
                    item_count += await write_market_activity_in_transaction(
                        db,
                        _page_batch(request_path, request_body, dataset_id, feed, event),
                        requested_chunk_size=MARKET_ACTIVITY_BATCH_SIZE,
                    )
                case fetch_dataset.FetchDone():
                    done = event
                    # The commit is the block's, on the way out. ONE INFO LINE PER SERVED FETCH
                    # (Q-EMPTY, tj-3mk3u5.37.1), carrying the SERVED range -- what was actually
                    # fetched rather than what was asked for -- with as_of, the resolved feed and
                    # the row count. That is what makes a served-but-empty answer diagnosable from
                    # the logs alone. THE OWNER IS NEVER LOGGED (D8): it is not in this line and
                    # must not be added to it.
                    log.info(
                        f'Fetch served {event.bar_count} bar(s) over '
                        f'[{event.served_range.start}, {event.served_range.end}) as of {event.as_of} '
                        f'on feed {feed}; {item_count} written'
                    )

    # done is set: the seam raises rather than ending a stream without a FetchDone (tj-tkm4tn D4),
    # so reaching here with None would be a defect in the seam and surfaces as the bug it is.
    return StoredDataset(data_points=item_count, served_range=done.served_range)
