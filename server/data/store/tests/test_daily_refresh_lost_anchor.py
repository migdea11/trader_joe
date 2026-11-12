"""The DAILY tail's lost anchor: the newest COUNTED real bar, read when the newest real bar does not count (validator).

DESIGN: task tj-7zjov1.122, decision tj-7zjov1.120 ruling 2 and ADDENDUM 1 (rulings 5 to 7), and the architect's
residual note on tj-7zjov1.122 (the placeholder lookback hangs from min(stamp, write time)). As built in 245b6f3:
    - when the newest real bar does not count and its write time cannot bound the tail (outside the declared range,
      or no write time), the pass reads the newest counted real bar (newest_counted_real_bar: not a placeholder,
      inside [start, end), written at or after its own stamp; ONE statement, only in that case) and _fetch_start uses
      it in place of the newest real bar: the anchor of the run and the hole check, with its own provisional test;
    - no counted real bar at all (LostAnchor(None)) is a pads-only dataset: the tail follows the counted pads;
    - _fetch_start called without lost_anchor keeps the declared-start fallback;
    - the written-early case (ruling 7, the floor at the write period) is unchanged and never reads the anchor;
    - the placeholder lookback hangs from the anchor's stamp in the lost case, otherwise from min(stamp, write time)
      of the newest real bar, so a phantom far-future bar does not leave unsettled pads unread.

The lost-anchor judgement (_needs_lost_anchor) is made WITHOUT now. Its edges, including the two where that differs
from _fetch_start's own judgement (no write time and stamped after now; a write time at or after a stamp that is
itself at or after now, a clock skew), are pinned here as built, so a change is noticed; both fall back to the
declared start, never to a gap.

TIERS as in test_daily_refresh_stored_tail.py, whose helpers and SQLite Sim this file reuses: the pure rule with the
real XNYS calendar, the crud read and the placeholder read over SQLite, and the real pass.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_stock import UpdateType
from data.store.app.daily_refresh import (
    PLACEHOLDER_LOOKBACK,
    LostAnchor,
    PlaceholderTail,
    _fetch_start,
    _needs_lost_anchor,
    _read_placeholder_tails,
    _request_for,
)
from data.store.app.database.crud.stock import dataset_catalog as catalog_repo
from data.store.app.database.crud.stock.dataset_catalog import CatalogRow, CountedBar, PlaceholderBar
from data.store.tests.test_daily_refresh_stored_tail import (
    LONG_AFTER,
    SEPT_1,
    TUE_14_OCT,
    XNYS,
    Sim,
    catalog_row,
    daily,
    pads,
    shaped,
    z,
)
from data.store.tests.test_daily_refresh_stored_tail import sim as _stored_tail_sim  # noqa: F401 (a fixture)
from data.store.tests.test_ui_datasets_route import (  # noqa: F401 (aware_sqlite_datetimes is a fixture)
    aware_sqlite_datetimes,
    entry,
)


pytestmark = pytest.mark.data_store

OCT_12 = z('2026-10-12T04:00:00Z')


@pytest.fixture
def sim(_stored_tail_sim: Sim) -> Sim:  # noqa: F811 (the imported fixture, under its own name here)
    """test_daily_refresh_stored_tail.py's SQLite world: the real pass and delivery intake, an injected clock."""
    return _stored_tail_sim


def lost_row(
    last_bar: str = '2026-10-12T04:00:00Z', *, end: datetime | None = OCT_12, written=LONG_AFTER
) -> CatalogRow:
    """A row whose newest real bar does NOT count by the range test alone: by default it is stamped AT the end."""
    return replace(catalog_row(last_bar=last_bar, written=written), end=end)


def counted(at: str, written: str | datetime = LONG_AFTER) -> LostAnchor:
    return LostAnchor(CountedBar(timestamp=z(at), written_at=z(written) if isinstance(written, str) else written))


# ------------------------------------------------------------------------------------------------- the pure rule

PURE_CASES = {
    # (row, placeholders, now, lost_anchor, expected tail start)
    'outside the range, counted 5 Oct final: after the anchor': (
        lost_row(),
        [],
        TUE_14_OCT,
        counted('2026-10-05T04:00:00Z'),
        '2026-10-06T04:00:00Z',
    ),
    'outside the range, no anchor read: the declared start (a direct call)': (
        lost_row(),
        [],
        TUE_14_OCT,
        None,
        '2026-09-01T04:00:00Z',
    ),
    'the anchor is provisional (written part-way through its session): the anchor itself': (
        lost_row(),
        [],
        TUE_14_OCT,
        counted('2026-10-05T04:00:00Z', '2026-10-05T15:00:00Z'),
        '2026-10-05T04:00:00Z',
    ),
    'the anchor pairs with the pads after it: Fri 2 Oct is a hole after Thu 1 Oct': (
        lost_row(),
        pads(('2026-10-05T04:00:00Z', LONG_AFTER), ('2026-10-06T04:00:00Z', LONG_AFTER)),
        TUE_14_OCT,
        counted('2026-10-01T04:00:00Z'),
        '2026-10-02T04:00:00Z',
    ),
    'the anchor with settled pads after it and no hole: after the newest pad': (
        lost_row(),
        pads(('2026-10-02T04:00:00Z', LONG_AFTER), ('2026-10-05T04:00:00Z', LONG_AFTER)),
        TUE_14_OCT,
        counted('2026-10-01T04:00:00Z'),
        '2026-10-06T04:00:00Z',
    ),
    'pads only (LostAnchor(None)): after the newest counted pad, no hole before the first pad': (
        lost_row(),
        pads(('2026-10-02T04:00:00Z', LONG_AFTER), ('2026-10-05T04:00:00Z', LONG_AFTER)),
        TUE_14_OCT,
        LostAnchor(None),
        '2026-10-06T04:00:00Z',
    ),
    'pads only with no pad either: the declared start': (
        lost_row(),
        [],
        TUE_14_OCT,
        LostAnchor(None),
        '2026-09-01T04:00:00Z',
    ),
    'pads only, an unsettled pad: reached back to': (
        lost_row(),
        pads(('2026-10-02T04:00:00Z', '2026-10-02T15:00:00Z'), ('2026-10-05T04:00:00Z', LONG_AFTER)),
        TUE_14_OCT,
        LostAnchor(None),
        '2026-10-02T04:00:00Z',
    ),
    'no write time, stamped after now, an anchor handed in: after the anchor': (
        replace(catalog_row(last_bar='2026-10-19T04:00:00Z'), last_bar_written_at=None),
        [],
        TUE_14_OCT,
        counted('2026-10-09T04:00:00Z'),
        '2026-10-10T04:00:00Z',
    ),
    'the newest real bar counts: an anchor handed in is not consulted': (
        catalog_row(last_bar='2026-10-09T04:00:00Z'),
        [],
        TUE_14_OCT,
        counted('2026-10-01T04:00:00Z'),
        '2026-10-10T04:00:00Z',
    ),
    'written early inside the range: still the period of its write, the anchor not consulted': (
        catalog_row(last_bar='2026-10-19T04:00:00Z', written='2026-10-13T15:00:00Z'),
        [],
        TUE_14_OCT,
        counted('2026-10-01T04:00:00Z'),
        '2026-10-13T04:00:00Z',
    ),
}


@pytest.mark.parametrize(('row', 'placeholders', 'now', 'anchor', 'expected'), PURE_CASES.values(), ids=PURE_CASES)
def test_the_lost_anchor_stands_in_for_the_newest_real_bar(row, placeholders, now, anchor, expected):
    assert _fetch_start(row, XNYS, placeholders, now, lost_anchor=anchor) == z(expected)


def test_a_direct_call_without_lost_anchor_still_returns_the_declared_start():
    """The keyword's default is None: an existing caller that never reads the anchor keeps the old fallback."""
    row = lost_row()

    assert _fetch_start(row, XNYS, [], TUE_14_OCT) == row.start
    assert _fetch_start(replace(row, start=z('2026-09-15T04:00:00Z')), XNYS, [], TUE_14_OCT) == z(
        '2026-09-15T04:00:00Z'
    )


def test_request_for_hands_the_anchor_to_the_tail_rule():
    row = replace(catalog_row(last_bar='2026-10-19T04:00:00Z'), last_bar_written_at=None)

    _path, _body, with_anchor = _request_for(row, XNYS, [], TUE_14_OCT, lost_anchor=counted('2026-10-09T04:00:00Z'))
    _path, _body, without = _request_for(row, XNYS, [], TUE_14_OCT)

    assert (with_anchor, without) == (z('2026-10-10T04:00:00Z'), row.start)


# -------------------------------------------------------------------------------------- when the anchor is read

NEEDS_CASES = {
    # (row, needs the read)
    'no bars at all': (catalog_row(), False),
    'the newest real bar counts': (catalog_row(last_bar='2026-10-09T04:00:00Z'), False),
    'written exactly at its stamp counts (ruling 5)': (
        catalog_row(last_bar='2026-10-09T04:00:00Z', written='2026-10-09T04:00:00Z'),
        False,
    ),
    'written early inside the range: the write-period floor, no read': (
        catalog_row(last_bar='2026-10-19T04:00:00Z', written='2026-10-13T15:00:00Z'),
        False,
    ),
    'stamped at the declared end': (lost_row(), True),
    'stamped before the declared start': (replace(catalog_row(last_bar='2026-08-31T04:00:00Z'), start=SEPT_1), True),
    'written early AND outside the range': (lost_row(written='2026-10-11T15:00:00Z'), True),
    'no write time, outside the range': (replace(lost_row(), last_bar_written_at=None), True),
    'no write time, inside the range': (
        replace(catalog_row(last_bar='2026-10-09T04:00:00Z'), last_bar_written_at=None),
        False,
    ),
    # Judged without now (the builder's claim): the two cases where that differs from _fetch_start's own judgement.
    # Neither is producible by the catalog read in normal operation (updated_at is NOT NULL, and Postgres now() is
    # not ahead of the pass's clock), and both fall back to the declared start: a full re-fetch, never a gap.
    'no write time, stamped after now: not read (declared start at the pass)': (
        replace(catalog_row(last_bar='2026-10-19T04:00:00Z'), last_bar_written_at=None),
        False,
    ),
    'written after a stamp that is after now (clock skew): not read (declared start at the pass)': (
        catalog_row(last_bar='2026-10-19T04:00:00Z', written='2026-10-19T05:00:00Z'),
        False,
    ),
}


@pytest.mark.parametrize(('row', 'needs'), NEEDS_CASES.values(), ids=NEEDS_CASES)
def test_when_the_lost_anchor_is_read(row, needs):
    assert _needs_lost_anchor(row, XNYS) is needs


def test_the_two_judged_without_now_cases_fall_back_to_the_declared_start_at_the_pass():
    """What the pass does with them: no anchor is read, so _fetch_start answers the declared start, never later."""
    for name in ('no write time, stamped after now', 'written after a stamp that is after now'):
        row, _ = next(value for key, value in NEEDS_CASES.items() if key.startswith(name))
        assert _fetch_start(row, XNYS, [], TUE_14_OCT, lost_anchor=None) == row.start, name


# ---------------------------------------------------------------------------------------- newest_counted_real_bar


@pytest.mark.asyncio
async def test_newest_counted_real_bar_reads_only_a_counted_real_bar_of_this_dataset(sim: Sim):
    """Each excluded bar is newer than the answer, so dropping any one condition changes the answer.

    Answer: Tue 6 Oct, written EXACTLY at its stamp (equality counts, ruling 5). Newer and excluded: Wed 7 Oct
    written a second before its stamp; Thu 8 Oct a placeholder; Fri 9 Oct another dataset's bar; Tue 20 Oct at the
    declared end. Older and excluded: Mon 31 Aug before the declared start. Mon 5 Oct counts and is older.
    """
    end = z('2026-10-20T04:00:00Z')
    mine = entry('NCB', update_type=UpdateType.DAILY, start=SEPT_1, end=end)
    # Same symbol, source, feed and granularity, another declared start: only the dataset id tells them apart.
    other = entry('NCB', update_type=UpdateType.DAILY, start=z('2026-09-02T04:00:00Z'), end=end)
    await sim.add(
        mine,
        shaped(mine, z('2026-08-31T04:00:00Z'), real=True, written=LONG_AFTER),
        shaped(mine, z('2026-10-05T04:00:00Z'), real=True, written=LONG_AFTER),
        shaped(mine, z('2026-10-06T04:00:00Z'), real=True, written=z('2026-10-06T04:00:00Z')),
        shaped(mine, z('2026-10-07T04:00:00Z'), real=True, written=z('2026-10-07T03:59:59Z')),
        shaped(mine, z('2026-10-08T04:00:00Z'), real=False, written=LONG_AFTER),
        shaped(mine, end, real=True, written=LONG_AFTER),
    )
    await sim.add(other, shaped(other, z('2026-10-09T04:00:00Z'), real=True, written=LONG_AFTER))
    row = await sim.catalog(mine)
    assert row.end == end and row.last_bar == end, 'the newest real bar is the one at the end: the lost case'

    async with AsyncSession(sim.engine) as db:
        found = await catalog_repo.newest_counted_real_bar(db, row)

    assert found == CountedBar(timestamp=z('2026-10-06T04:00:00Z'), written_at=z('2026-10-06T04:00:00Z'))


@pytest.mark.asyncio
async def test_newest_counted_real_bar_is_none_with_no_counted_real_bar(sim: Sim):
    """Pads and early-written real bars only: nothing counts."""
    dataset = daily('NCN')
    await sim.add(
        dataset,
        shaped(dataset, z('2026-10-05T04:00:00Z'), real=False, written=LONG_AFTER),
        shaped(dataset, z('2026-10-06T04:00:00Z'), real=True, written=z('2026-10-05T21:00:00Z')),
    )
    row = await sim.catalog(dataset)

    async with AsyncSession(sim.engine) as db:
        assert await catalog_repo.newest_counted_real_bar(db, row) is None


# -------------------------------------------------------------------------------------- the placeholder read


@contextmanager
def statements(sim: Sim) -> Iterator[list[str]]:
    seen: list[str] = []

    def count(_conn, _cursor, statement, _params, _context, _many):
        seen.append(statement)

    event.listen(sim.engine.sync_engine, 'before_cursor_execute', count)
    try:
        yield seen
    finally:
        event.remove(sim.engine.sync_engine, 'before_cursor_execute', count)


async def _tail(sim: Sim, row: CatalogRow) -> tuple[PlaceholderTail, int]:
    with statements(sim) as seen:
        tails = await _read_placeholder_tails([row], XNYS)
    return tails[row.id], len(seen)


@pytest.mark.asyncio
async def test_exactly_one_extra_statement_and_only_in_the_lost_anchor_case(sim: Sim):
    """Three datasets with the same stored shape but for the newest real bar: only the lost one reads the anchor."""
    end = z('2026-10-20T04:00:00Z')
    lost = entry('XLA', update_type=UpdateType.DAILY, start=SEPT_1, end=end)
    counts, early = daily('XLB'), daily('XLC')
    await sim.add(
        lost,
        counts,
        early,
        shaped(lost, z('2026-10-05T04:00:00Z'), real=True, written=LONG_AFTER),
        shaped(lost, end, real=True, written=LONG_AFTER),
        shaped(counts, z('2026-10-05T04:00:00Z'), real=True, written=LONG_AFTER),
        shaped(early, z('2026-10-05T04:00:00Z'), real=True, written=LONG_AFTER),
        shaped(early, z('2026-10-19T04:00:00Z'), real=True, written=z('2026-10-13T15:00:00Z')),
    )

    lost_tail, lost_count = await _tail(sim, await sim.catalog(lost))
    counts_tail, counts_count = await _tail(sim, await sim.catalog(counts))
    early_tail, early_count = await _tail(sim, await sim.catalog(early))

    # Absolute, not only relative: a read added for EVERY dataset keeps the differences and must still red.
    assert counts_count == 1, 'a counting newest real bar: the one placeholder page and nothing else'
    assert early_count == 1, 'the written-early case is bounded by its write time: no read'
    assert lost_count == 2, 'the placeholder page plus ONE statement for the lost anchor'
    assert lost_tail.lost_anchor == counted('2026-10-05T04:00:00Z')
    assert (counts_tail.lost_anchor, early_tail.lost_anchor) == (None, None)


@pytest.mark.asyncio
async def test_a_newest_real_bar_with_no_write_time_outside_the_range_reads_the_anchor(sim: Sim):
    """No write time cannot come from the catalog (updated_at is NOT NULL); the read takes it as the rule says."""
    end = z('2026-10-20T04:00:00Z')
    dataset = entry('XNW', update_type=UpdateType.DAILY, start=SEPT_1, end=end)
    await sim.add(
        dataset,
        shaped(dataset, z('2026-10-05T04:00:00Z'), real=True, written=LONG_AFTER),
        shaped(dataset, end, real=True, written=LONG_AFTER),
    )
    row = replace(await sim.catalog(dataset), last_bar_written_at=None)

    tail, _ = await _tail(sim, row)

    assert tail.lost_anchor == counted('2026-10-05T04:00:00Z')
    assert _fetch_start(row, XNYS, tail.bars, TUE_14_OCT, lost_anchor=tail.lost_anchor) == z('2026-10-06T04:00:00Z')


@pytest.mark.asyncio
async def test_the_lost_case_hangs_the_lookback_from_the_anchor(sim: Sim):
    """Anchor Thu 29 Oct, newest real bar at the end Fri 20 Nov: the floor is 29 Oct minus 42 days, 17 Sep.

    The unsettled pad of 17 Sep is read and the tail reaches back to it; one of 16 Sep is not read. Hung from the
    newest real bar instead (20 Nov, written after it), the floor would be 9 Oct and 17 Sep would be lost.
    """
    end = z('2026-11-20T05:00:00Z')
    dataset = entry('XLK', update_type=UpdateType.DAILY, start=z('2026-08-03T04:00:00Z'), end=end)
    await sim.add(
        dataset,
        shaped(dataset, z('2026-10-29T04:00:00Z'), real=True, written=z('2026-10-29T21:00:00Z')),
        shaped(dataset, end, real=True, written=z('2026-11-21T21:00:00Z')),
        shaped(dataset, z('2026-09-16T04:00:00Z'), real=False, written=z('2026-09-16T15:00:00Z')),
        shaped(dataset, z('2026-09-17T04:00:00Z'), real=False, written=z('2026-09-17T15:00:00Z')),
    )
    row = await sim.catalog(dataset)

    tail, _ = await _tail(sim, row)

    assert tail.lost_anchor == counted('2026-10-29T04:00:00Z', '2026-10-29T21:00:00Z')
    assert tail.bars == [PlaceholderBar(z('2026-09-17T04:00:00Z'), z('2026-09-17T15:00:00Z'))]
    now = z('2026-11-03T22:00:00Z')
    assert _fetch_start(row, XNYS, tail.bars, now, lost_anchor=tail.lost_anchor) == z('2026-09-17T04:00:00Z')


@pytest.mark.asyncio
async def test_pads_only_reads_the_whole_history(sim: Sim):
    """No counted real bar: no anchor stamp to hang a floor from, so every placeholder is read, the oldest too."""
    dataset = daily('XPO', start=z('2026-08-03T04:00:00Z'))
    await sim.add(
        dataset,
        shaped(dataset, z('2026-07-31T04:00:00Z'), real=True, written=LONG_AFTER),
        shaped(dataset, z('2026-08-03T04:00:00Z'), real=False, written=LONG_AFTER),
        shaped(dataset, z('2026-10-30T04:00:00Z'), real=False, written=LONG_AFTER),
    )

    tail, _ = await _tail(sim, await sim.catalog(dataset))

    assert tail.lost_anchor == LostAnchor(None)
    assert [bar.timestamp for bar in tail.bars] == [z('2026-10-30T04:00:00Z'), z('2026-08-03T04:00:00Z')]


# ------------------------------------------------------------------------------------------------------ the pass


@pytest.mark.asyncio
async def test_a_pads_only_dataset_whose_real_bars_precede_the_declared_start_is_not_refetched_whole(sim: Sim):
    """Real bars on 29 and 30 Sep, declared start Thu 1 Oct, settled pads 1, 2 and 5 Oct: tail Tue 6 Oct.

    The newest real bar is before the declared start, so it does not count and its write time cannot bound the
    tail. Before 245b6f3 the tail was the declared start every pass; with the read it follows the pads.
    """
    dataset = daily('XPS', start=z('2026-10-01T04:00:00Z'))
    await sim.add(
        dataset,
        shaped(dataset, z('2026-09-29T04:00:00Z'), real=True, written=LONG_AFTER),
        shaped(dataset, z('2026-09-30T04:00:00Z'), real=True, written=LONG_AFTER),
        shaped(dataset, z('2026-10-01T04:00:00Z'), real=False, written=LONG_AFTER),
        shaped(dataset, z('2026-10-02T04:00:00Z'), real=False, written=LONG_AFTER),
        shaped(dataset, z('2026-10-05T04:00:00Z'), real=False, written=LONG_AFTER),
    )

    await sim.run(z('2026-10-13T22:00:00Z'))

    assert sim.vendor.starts('XPS') == [z('2026-10-06T04:00:00Z')]


@pytest.mark.asyncio
async def test_a_phantom_future_bar_hangs_the_lookback_from_its_write_time(sim: Sim):
    """A real bar stamped Mon 14 Dec but written Wed 7 Oct 15:00Z (a phantom), and an unsettled pad of Thu 27 Aug.

    The lookback floor is min(stamp, write time) minus 42 days, 26 Aug 15:00Z, so the pad is read and the tail
    (ruling 7: the write period, 7 Oct, or an earlier unsettled counted pad) reaches back to it. Hung from the stamp,
    the floor would be 2 Nov, the pad unread, and the tail 7 Oct: the pad never asked for again.
    """
    dataset = daily('XPH', start=z('2026-08-03T04:00:00Z'))
    await sim.add(
        dataset,
        shaped(dataset, z('2026-08-27T04:00:00Z'), real=False, written=z('2026-08-27T15:00:00Z')),
        shaped(dataset, z('2026-10-06T04:00:00Z'), real=True, written=z('2026-10-06T21:00:00Z')),
        shaped(dataset, z('2026-12-14T05:00:00Z'), real=True, written=z('2026-10-07T15:00:00Z')),
    )

    await sim.run(z('2026-10-13T22:00:00Z'))

    assert sim.vendor.starts('XPH') == [z('2026-08-27T04:00:00Z')]


@pytest.mark.asyncio
async def test_the_written_early_case_through_the_pass_is_unchanged(sim: Sim):
    """Ruling 7 as before: newest real bar Mon 12 Oct written Wed 7 Oct, no pad: the tail is the write period."""
    dataset = daily('XWE')
    await sim.add(
        dataset,
        shaped(dataset, z('2026-10-06T04:00:00Z'), real=True, written=z('2026-10-06T21:00:00Z')),
        shaped(dataset, z('2026-10-12T04:00:00Z'), real=True, written=z('2026-10-07T15:00:00Z')),
    )

    await sim.run(z('2026-10-13T22:00:00Z'))

    assert sim.vendor.starts('XWE') == [z('2026-10-07T04:00:00Z')]


def test_the_lookback_this_file_assumes():
    assert timedelta(days=42) == PLACEHOLDER_LOOKBACK
