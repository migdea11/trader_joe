"""A scriptable IngestFetchClient double for data_store's dataset path, shared by four test files.

WHY A SHARED DOUBLE AND NOT ONE PER FILE. The Kafka client it replaces had a one-method surface
(``send_request`` -> one batch), so three files each spelled their own four-line fake and nothing
was lost. The FetchDataset seam is a STREAM with an order (ack, then pages, then done) and three
failure shapes (a refusal before the ack, a break mid-stream, a call that never completes), and a
per-file fake of that is a per-file chance to fake it WRONG -- most of all by yielding the events
eagerly from a list, which makes an implementation that accumulates the whole stream before writing
indistinguishable from one that writes page by page. This double is a real async generator, so the
consumer pulls each event, and it journals every yield into a list the session fake also writes to.
That shared journal is what lets a test assert the INTERLEAVING rather than the counts.

WHAT IT IS NOT. It is not a server and it speaks no gRPC: ``IngestFetchClient`` is the interface
precisely so a test double and a replay backend are ordinary implementations of it (ADR tj-8konfu
D3), and the gRPC implementation's own wire behaviour is common/tests/rpc/'s subject, not this
directory's. It also does not re-police the stream grammar the seam already enforces
(tj-tkm4tn D4): a script here can emit an illegal order on purpose, because what the WRITE side
does when handed one is a thing worth pinning.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from common.enums.data_stock import Feed
from schemas.data_ingest import fetch_dataset


# The instant the first scripted bar opens. Fixed rather than "now" so a journal or a failure
# message reads the same on every run, and so two pages built independently never collide.
FIRST_BAR_START = datetime(2026, 1, 2, tzinfo=UTC)

# The tape the double resolves unless a test names another. IEX and not SIP so that a test
# asserting the ack's feed reached the bars cannot pass against a hard-coded SIP default
# somewhere: SIP is what data/store/tests' other batch builders use.
DEFAULT_FEED = Feed.IEX


def bar(minute: int, *, feed: Feed = DEFAULT_FEED) -> fetch_dataset.Bar:
    """One bar, opening ``minute`` minutes after FIRST_BAR_START.

    The OHLC values are derived from the minute rather than constant, so a test that needs to prove
    a specific bar reached the database can recover its position from what was written.

    Args:
        minute: Minutes after FIRST_BAR_START at which this bar opens; also its identity.
        feed: The tape to stamp. The seam refuses a page whose bars disagree with the ack, so a
            test exercising that refusal is the only caller that passes something else.

    Returns:
        fetch_dataset.Bar: The bar.
    """
    return fetch_dataset.Bar(
        bar_start=FIRST_BAR_START + timedelta(minutes=minute),
        open=100.0 + minute,
        high=101.0 + minute,
        low=99.0 + minute,
        close=100.5 + minute,
        volume=1000.0 + minute,
        trade_count=10 + minute,
        feed=feed,
    )


def page(minutes: list[int], *, feed: Feed = DEFAULT_FEED) -> fetch_dataset.BarPage:
    """A page carrying one bar per minute offset given.

    Args:
        minutes: The minute offsets, which become the bars' identities.
        feed: The tape to stamp on every bar.

    Returns:
        fetch_dataset.BarPage: The page.
    """
    return fetch_dataset.BarPage(bars=[bar(minute, feed=feed) for minute in minutes])


def done(bar_count: int, *, last_minute: int | None = None) -> fetch_dataset.FetchDone:
    """The terminator, carrying a served range that covers the scripted bars.

    Args:
        bar_count: How many bars the stream served.
        last_minute: The minute offset of the last bar; the served range ends one minute after it,
            since ServedRange.end is exclusive. Defaults to ``bar_count``.

    Returns:
        fetch_dataset.FetchDone: The done event.
    """
    end_minute = (bar_count if last_minute is None else last_minute) + 1
    return fetch_dataset.FetchDone(
        bar_count=bar_count,
        served_range=fetch_dataset.ServedRange(
            start=FIRST_BAR_START, end=FIRST_BAR_START + timedelta(minutes=end_minute)
        ),
        as_of=FIRST_BAR_START + timedelta(minutes=end_minute),
    )


def accepted_stream(pages: list[list[int]] | None = None, *, feed: Feed = DEFAULT_FEED) -> list:
    """The ordinary happy stream: an accepted ack, the given pages, then a matching done.

    Args:
        pages: One list of minute offsets per page. None or an empty list is the genuinely-served-
            but-empty window, which ADR tj-fa1rpu D2 makes a success and not an error.
        feed: The resolved tape, stamped on the ack and on every bar so the two agree.

    Returns:
        list: The events, in contract order.
    """
    pages = pages or []
    bar_count = sum(len(minutes) for minutes in pages)
    last_minute = max((minute for minutes in pages for minute in minutes), default=0)
    return [
        fetch_dataset.FetchAccepted(feed=feed),
        *(page(minutes, feed=feed) for minutes in pages),
        done(bar_count, last_minute=last_minute),
    ]


def served_range_of(script: 'FetchScript') -> fetch_dataset.ServedRange:
    """The served_range of the FetchDone a script ends with.

    Read off the script rather than restated in a test, so that the expected value and the value
    the double actually yields cannot drift apart -- which is the whole failure mode a test of
    "the response copies the served range" has to avoid.

    Args:
        script: The script whose terminator to read.

    Returns:
        fetch_dataset.ServedRange: The window the scripted fetch was answered for.

    Raises:
        AssertionError: If the script does not end with a FetchDone.
    """
    done_event = script.events[-1] if script.events else None
    assert isinstance(done_event, fetch_dataset.FetchDone), (
        f'this script ends with {type(done_event).__name__}, so it has no served range to copy'
    )
    return done_event.served_range


def assert_body_served_range(body: dict, expected: fetch_dataset.ServedRange) -> None:
    """The response body's served_range is the given window, compared as instants.

    AS INSTANTS, not as text: the body carries RFC 3339 strings and the domain object carries
    datetimes, so a string comparison would be asserting a serialisation format in a test whose
    subject is whether the value was COPIED. '...+00:00' and '...Z' are the same instant and either
    is a correct answer here; which one this service emits is pinned where that is the subject.

    Args:
        body: The parsed 200 body.
        expected: The window the fetch was answered for.

    Raises:
        AssertionError: If served_range is absent or names a different window.
    """
    served = body.get('served_range')
    assert served is not None, f'the 200 body carries no served_range: {body}'
    actual = (datetime.fromisoformat(served['start']), datetime.fromisoformat(served['end']))
    assert actual == (expected.start, expected.end), (
        f'the response served_range is {actual}, not the window the fetch was answered for '
        f'({expected.start}, {expected.end}). It is COPIED out of FetchDone, never rebuilt from the '
        f'request and never clamped again at the store.'
    )


@dataclass
class FetchScript:
    """What one ``fetch`` call does: yield these events, then optionally fail.

    The two halves are independent on purpose. ``raises`` with no events is a refusal before the
    ack, where nothing may be written. ``raises`` after some events is a stream that broke
    mid-flight, where the writes already issued must be abandoned. Events with no ``raises`` is the
    ordinary completion.
    """

    events: list = field(default_factory=list)
    # Raised AFTER every listed event has been yielded and consumed. None completes normally.
    raises: BaseException | None = None


class RecordingFetchClient:
    """An IngestFetchClient that records its requests and yields a script, one event at a time.

    LAZY BY CONSTRUCTION, which is the whole reason this is a class with an async generator method
    rather than a function returning a list. ``fetch`` suspends at every yield until the caller asks
    for the next event, so the journal below interleaves the double's yields with whatever the
    caller did between them. A double that handed back a pre-built list would make "writes each page
    as it arrives" and "buffers the stream and writes once at the end" produce identical evidence --
    the exact fake-pass the architect found on the ingest side's paging tests.
    """

    def __init__(self, script: FetchScript | None = None, *, journal: list[str] | None = None):
        """Bind the double to a script and, optionally, to a journal it shares with a session fake.

        Args:
            script: What the one fetch does. Defaults to an accepted, empty, completed stream.
            journal: The shared order-of-events list. One is created when none is given, which is
                what a test that only cares about the recorded request wants.
        """
        self.requests: list[fetch_dataset.FetchDatasetRequest] = []
        self.script = script if script is not None else FetchScript(events=accepted_stream())
        self.journal: list[str] = journal if journal is not None else []

    async def fetch(self, request: fetch_dataset.FetchDatasetRequest):
        """Record the request and play the script.

        Args:
            request: What the worker asked for; appended to ``requests`` before anything is yielded.

        Yields:
            The scripted events, in order.

        Raises:
            BaseException: The script's ``raises``, after its events have been consumed.
        """
        self.requests.append(request)
        for event in self.script.events:
            self.journal.append(f'yield {type(event).__name__}')
            yield event
        if self.script.raises is not None:
            self.journal.append(f'raise {type(self.script.raises).__name__}')
            raise self.script.raises
