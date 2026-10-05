"""The SERVER side of a FetchDataset stream: the helper the ingest servicer encodes its events with.

ADR tj-8konfu D3 says no caller touches a generated stub, and the TID251 exemption covers only
common/rpc/**. So the servicer (tj-3mk3u5.9) builds no FetchDatasetResponse of its own: it hands domain
objects to this module and gets wire messages back, and the generated symbols stay behind the seam.

WHY THIS IS A STREAM-LEVEL OBJECT AND NOT SIX LOOSE FUNCTIONS -- THE FEED INVARIANT. market/v1's Bar
carries its own feed, because a bar reaching the UI or an external stream has no fetch envelope to read one
from. On THIS contract the feed is also settled once, in FetchAccepted, and bar.proto line 41 asserts the
two must agree. Nothing enforced that: the wire can express disagreement, and data_store writes its dataset
entry from the ACK while writing its rows from the BARS, so a disagreeing page would make the ledger record
one feed series identity (tj-u12tjo.11) and the rows contradict it, silently and with no error anywhere.
The architect ruled the invariant real and enforced in two places (tj-3mk3u5.28, 02:28 UTC 2026-10-03).
This is the ENCODE half: an encoder is built for one resolved feed and every bar it emits carries that
feed.

IT COMPARES AND RAISES; IT DOES NOT OVERWRITE. The domain Bar's feed is REQUIRED and independently sourced
-- the reader populates it upstream -- so a bar arriving here with a different feed is an INGEST DEFECT.
Overwriting it would hide exactly the bug the stamping exists to make impossible, so page() refuses the
page. A ProtoMappingError out of a servicer is our bug and the error boundary answers INTERNAL with an
error_id (common/rpc/errors.py), which is the correct treatment: nothing a caller sent can cause it.

WHAT THIS DELIBERATELY DOES NOT POLICE: the ORDER of the events. The grammar -- ack first, then pages, then
done -- is enforced where it protects somebody, on the decode side in common/rpc/clients/ingest_fetch.py,
which sees every peer's stream and not only ours. Making the encoder stateful would add a second, weaker
copy of that check and a new way for a servicer to fail half way through a stream.

THE REFUSED ACK IS THE ONE IN-BAND FAILURE. FEED_NOT_AVAILABLE is answered in the ack and never as a status
(user ruling tj-3mk3u5.22 Q5); its REASONS row has no grpc_code, so common/rpc/errors.py's render() refuses
it outright. Every other failure on this hop is a gRPC status. refused_response() therefore accepts that
one reason and nothing else.
"""

from collections.abc import Sequence
from typing import Final

from common.enums.data_stock import Feed
from common.errors.vocabulary import ERROR_DOMAIN, REASONS, Reason, TraderJoeError, new_error_id
from common.rpc.mapping.fetch_dataset import bar_to_proto, done_to_proto
from common.rpc.mapping.values import FEED, ProtoMappingError
from schemas.data_ingest import fetch_dataset as domain
from trader_joe.proto.internal.ingest.v1 import ingest_pb2


# THE PAGE SIZE, which the wire cannot express and the server must honour (ingest.proto, BarPage). The
# number lives in a .proto comment, where no Python caller can reach it, so the servicer that chunks a
# fetch reads it from here rather than writing 5000 again. MEASURED on this contract: a full page is
# 345000 bytes, 337 KiB, and 385000 bytes with every optional field populated -- about a third of the
# ~1 MiB ADR tj-8konfu D6.3 targets for a bulk chunk and 9.2 % of the 4 MiB ceiling in common/rpc/config.py.
MAX_PAGE_BARS: Final = 5000

# The one encoding for a sequence-valued metadata item on this hop. common/errors lets an allowlisted key
# hold a str or a sequence of str, while an ErrorInfo metadata value is a string; common/rpc/errors.py
# states the encoding -- the items joined by a comma and nothing else, never re-split on the way back,
# because a comma inside one item is indistinguishable from the separator. FetchRefused MIRRORS ErrorInfo,
# so it is the same encoding, spelled here because errors.py keeps its copy private. Nothing carries a
# sequence across this hop in PR 2.
_SEQUENCE_SEPARATOR: Final = ','

# The METADATA_KEYS key D8's correlation id travels under. Spelled as in common/rpc/errors.py and
# routers/common/errors.py, because it is the same key on every transport.
_ERROR_ID_KEY: Final = 'error_id'


def _wire_value(value: str | tuple[str, ...]) -> str:
    return value if isinstance(value, str) else _SEQUENCE_SEPARATOR.join(value)


def _carried_error_id(error: TraderJoeError) -> str | None:
    # The error_id the error already carries, read exactly as common/rpc/errors.py reads it, so an error
    # answered in the ack is judged the same way as one answered by a status. A value that names nothing is
    # no id: '', an empty sequence, or a sequence of nothing but ''.
    carried = error.metadata.get(_ERROR_ID_KEY)
    if isinstance(carried, str):
        return carried or None
    if carried is None or not any(carried):
        return None
    return _SEQUENCE_SEPARATOR.join(carried)


def refused_response(error: TraderJoeError, *, error_id: str | None = None) -> ingest_pb2.FetchDatasetResponse:
    """Encode a refused ack: the stream's only event, and the hop's one in-band failure.

    The fields mirror google.rpc.ErrorInfo so the client seam rebuilds the SAME TraderJoeError a REFUSED
    status would have produced, and data_store's HTTP edge then renders it as problem+json like any other.

    The error_id follows render() in common/rpc/errors.py exactly, so the two ways a typed error leaves this
    service behave alike: the error's own id when its metadata holds one, otherwise the error_id argument,
    otherwise one minted here so the ack carries one all the same. A caller that means to LOG under the id
    settles it first and passes it in -- this function is pure and logs nothing, and an id that names no log
    line helps nobody.

    Args:
        error: The refusal. Its reason must be FEED_NOT_AVAILABLE.
        error_id: The id to carry when the error has none of its own.

    Returns:
        ingest_pb2.FetchDatasetResponse: The ack, refused arm set, ready to yield as the stream's only
        message.

    Raises:
        ProtoMappingError: If the reason is anything but FEED_NOT_AVAILABLE. Every other failure on this hop
            is a gRPC status, and a reason that reaches the ack instead is a servicer bug.
    """
    if error.reason is not Reason.FEED_NOT_AVAILABLE:
        raise ProtoMappingError(
            f'{error.reason} is not answered in the FetchDataset ack: the one in-band refusal is an '
            'unservable feed (tj-3mk3u5.22 Q5), and every other failure is a gRPC status through '
            f'common/rpc/errors.py, whose REASONS row renders it as {REASONS[error.reason].grpc_code}'
        )
    metadata = {key: _wire_value(value) for key, value in error.metadata.items()}
    if _carried_error_id(error) is None:
        metadata[_ERROR_ID_KEY] = error_id or new_error_id()
    refused = ingest_pb2.FetchRefused(
        reason=error.reason.value, domain=ERROR_DOMAIN, detail=error.detail, metadata=metadata
    )
    return ingest_pb2.FetchDatasetResponse(ack=ingest_pb2.FetchAck(refused=refused))


class FetchStreamEncoder:
    """Encodes one ACCEPTED FetchDataset stream, stamping every bar it emits with the resolved feed.

    Built once per fetch, after ingest has resolved the tape it is entitled to. The resolved feed is not an
    argument to each event, so the server cannot emit an ack and a page that disagree.
    """

    def __init__(self, feed: Feed) -> None:
        """Fix the resolved feed for this stream.

        Args:
            feed: The tape this deployment resolved for this fetch, the one value only ingest may decide.

        Raises:
            ProtoMappingError: If the feed has no value on the wire.
        """
        # Encoded once, here, so a feed that cannot cross is refused before the ack goes out rather than on
        # the first page.
        self._wire_feed = FEED.to_proto(feed)
        self._feed = feed

    @property
    def feed(self) -> Feed:
        """Feed: The resolved feed every event of this stream carries."""
        return self._feed

    def accepted(self) -> ingest_pb2.FetchDatasetResponse:
        """Encode the accepted ack, ALWAYS the first message of the stream.

        Returns:
            ingest_pb2.FetchDatasetResponse: The ack, accepted arm set, carrying the resolved feed. The
            store writes its dataset entry, feed included, on this.
        """
        accepted = ingest_pb2.FetchAccepted(feed=self._wire_feed)
        return ingest_pb2.FetchDatasetResponse(ack=ingest_pb2.FetchAck(accepted=accepted))

    def page(self, bars: Sequence[domain.Bar]) -> ingest_pb2.FetchDatasetResponse:
        """Encode one bounded page of bars, every one of them on this stream's resolved feed.

        Args:
            bars: The page's bars, at most MAX_PAGE_BARS of them. Each one's feed must already be the
                resolved feed; the reader populates it upstream.

        Returns:
            ingest_pb2.FetchDatasetResponse: The page.

        Raises:
            ProtoMappingError: If a bar carries a different feed from the ack's -- an ingest defect, and
                overwriting it would conceal the very bug the single resolved feed exists to prevent -- or
                if the page holds more bars than the contract allows, which the wire cannot express and
                nothing downstream would catch.
        """
        if len(bars) > MAX_PAGE_BARS:
            raise ProtoMappingError(
                f'a BarPage carries at most {MAX_PAGE_BARS} bars and this one carries {len(bars)}; the page '
                "size is the server's to honour, because the wire cannot express it (ingest.proto, BarPage)"
            )
        disagreeing = {bar.feed for bar in bars if bar.feed is not self._feed}
        if disagreeing:
            raise ProtoMappingError(
                f'this fetch resolved the feed {self._feed}, and the page carries bars on '
                f'{sorted(str(feed) for feed in disagreeing)}; a bar is never re-stamped, because a feed '
                'that disagrees with the ack is an ingest defect and data_store would record one series '
                'identity while writing rows from another (tj-u12tjo.11)'
            )
        return ingest_pb2.FetchDatasetResponse(page=ingest_pb2.BarPage(bars=[bar_to_proto(bar) for bar in bars]))

    def done(self, done: domain.FetchDone) -> ingest_pb2.FetchDatasetResponse:
        """Encode the terminator, so a stream that merely stops is distinguishable from one that finished.

        Args:
            done: The bar count, the window actually served and when the vendor answered. An empty but
                genuinely served window ends here with bar_count 0 and never with a failure.

        Returns:
            ingest_pb2.FetchDatasetResponse: The done.

        Raises:
            ProtoMappingError: If a value cannot be put on the wire.
        """
        return ingest_pb2.FetchDatasetResponse(done=done_to_proto(done))
