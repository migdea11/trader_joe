import { describe, expect, it } from 'vitest'
import { create } from '@bufbuild/protobuf'
import { timestampFromMs } from '@bufbuild/protobuf/wkt'

import { BarSchema, type Bar } from '@generated/trader_joe/proto/market/v1/bar_pb'

import { TABLE_PAGE_SIZE } from './barWindow'
import { CSV_HEADER, barsToCsv, barsToCsvRows } from './csv'
import {
  displayDay,
  parseDay,
  parseRangeQuery,
  rangeProblem,
  toApiWindow,
  toRangeQuery,
  zonedDayStart,
} from './range'

// tj-grna9p.33: the Viewer's half-open range and its CSV text. The bounded chart load
// (loadBarWindow, the 50,000-bar cap and its truncated flag) is gone: the chart holds one page of the
// server's page index (chartPages.spec.ts, decision tj-eozqhi). barWindow.ts keeps only the table's
// page size.

const bar = (ms: number, close = 1): Bar =>
  create(BarSchema, { barStart: timestampFromMs(ms), open: 1, high: 2, low: 0.5, close, volume: 10 })

describe('barWindow', () => {
  it('holds only the table page size; the chart load and its cap are gone', async () => {
    expect(TABLE_PAGE_SIZE).toBe(500)
    const module = await import('./barWindow')
    expect(Object.keys(module)).toEqual(['TABLE_PAGE_SIZE'])
  })
})

describe('range days', () => {
  it.each([
    ['2026/09/01', '2026-09-01'],
    ['2026-09-01', '2026-09-01'],
    [' 2024/02/29 ', '2024-02-29'],
  ])('parses %j', (text, day) => {
    expect(parseDay(text)).toBe(day)
  })

  it.each(['2026/02/30', '2025/02/29', '2026/13/01', '2026/9/1', '01/09/2026', '', 'today'])('refuses %j', (text) => {
    expect(parseDay(text)).toBeNull()
  })

  it('displays YYYY/MM/DD and nothing for an unset day', () => {
    expect(displayDay('2026-09-01')).toBe('2026/09/01')
    expect(displayDay(undefined)).toBe('')
  })

  it('reads the URL, ignoring malformed days, and writes it back keeping other keys', () => {
    expect(parseRangeQuery({ from: '2026-09-01', to: 'soon' })).toEqual({ from: '2026-09-01', to: undefined })
    expect(parseRangeQuery({ from: ['2026-09-01', '2026-10-01'] })).toEqual({ from: '2026-09-01', to: undefined })
    expect(toRangeQuery({ from: '2026-09-01' }, { group: 'paper', to: '2026-09-09' })).toEqual({
      group: 'paper',
      from: '2026-09-01',
    })
  })
})

describe('half-open window in the viewer zone', () => {
  it.each([
    ['2026-09-01', 'America/Toronto', '2026-09-01T04:00:00.000Z'],
    ['2026-01-15', 'America/Toronto', '2026-01-15T05:00:00.000Z'],
    // DST starts 2026-03-08 at 02:00 local: that midnight is still EST.
    ['2026-03-08', 'America/Toronto', '2026-03-08T05:00:00.000Z'],
    ['2026-03-09', 'America/Toronto', '2026-03-09T04:00:00.000Z'],
    // DST ends 2026-11-01 at 02:00 local: that midnight is still EDT.
    ['2026-11-01', 'America/Toronto', '2026-11-01T04:00:00.000Z'],
    ['2026-11-02', 'America/Toronto', '2026-11-02T05:00:00.000Z'],
    // East of UTC the UTC midnight falls after the change, so the first guess takes the wrong offset:
    // Sydney leaves DST 2026-04-05 03:00 (that midnight is still +11) and enters it 2026-10-04 02:00
    // (that midnight is still +10).
    ['2026-04-05', 'Australia/Sydney', '2026-04-04T13:00:00.000Z'],
    ['2026-10-04', 'Australia/Sydney', '2026-10-03T14:00:00.000Z'],
    ['2026-09-01', 'Asia/Tokyo', '2026-08-31T15:00:00.000Z'],
    ['2026-09-01', 'UTC', '2026-09-01T00:00:00.000Z'],
  ])('%s starts in %s at %s', (day, zone, iso) => {
    expect(zonedDayStart(day, zone).toISOString()).toBe(iso)
  })

  it('From and To are both day starts: To is excluded', () => {
    const window = toApiWindow({ from: '2026-09-01', to: '2026-09-08' }, 'America/Toronto')
    expect(window.start!.toISOString()).toBe('2026-09-01T04:00:00.000Z')
    expect(window.end!.toISOString()).toBe('2026-09-08T04:00:00.000Z')
    expect(toApiWindow({}, 'UTC')).toEqual({ start: undefined, end: undefined })
  })

  it('end at or before start is a problem, an open side is not', () => {
    expect(rangeProblem({ from: '2026-09-01', to: '2026-09-01' })).toBe('To must be after From (To is not included).')
    expect(rangeProblem({ from: '2026-09-02', to: '2026-09-01' })).toBeDefined()
    expect(rangeProblem({ from: '2026-09-01', to: '2026-09-02' })).toBeUndefined()
    expect(rangeProblem({ to: '2026-09-01' })).toBeUndefined()
  })
})

describe('barsToCsv', () => {
  const t1 = Date.UTC(2026, 8, 1, 4)
  // Already September 2 in UTC, still September 1 in Toronto: only the zone tells the dates apart.
  const t2 = Date.UTC(2026, 8, 2, 3, 59)

  it('writes exactly the given bars with YYYY/MM/DD dates in the zone', () => {
    const bars = [
      create(BarSchema, { barStart: timestampFromMs(t1), open: 1.5, high: 2.25, low: 1, close: 2, volume: 1200 }),
      create(BarSchema, { barStart: timestampFromMs(t2), open: 2, high: 3, low: 1.75, close: 2.5, volume: 0 }),
    ]
    expect(barsToCsv(bars, 'America/Toronto', false)).toBe(
      `${CSV_HEADER}\n2026/09/01,1.5,2.25,1,2,1200\n2026/09/01,2,3,1.75,2.5,0\n`,
    )
    expect(CSV_HEADER).toBe('date,open,high,low,close,volume')
  })

  it('adds HH:mm for intraday bars, in the zone', () => {
    const csv = barsToCsv([bar(t2, 3)], 'America/Toronto', true)
    expect(csv.split('\n')[1]).toBe('2026/09/01 23:59,1,2,0.5,3,10')
  })

  it('skips a bar with no start and an empty list is the header alone', () => {
    expect(barsToCsv([create(BarSchema, { close: 1 })], 'UTC', false)).toBe(`${CSV_HEADER}\n`)
    expect(barsToCsv([], 'UTC', false)).toBe(`${CSV_HEADER}\n`)
  })
})

// tj-grna9p.126: the export writes a file in chunks, one per page read. The header plus the chunks
// must be the same bytes barsToCsv writes for the same bars in one go.
describe('barsToCsvRows', () => {
  const bars = [
    create(BarSchema, { barStart: timestampFromMs(Date.UTC(2026, 8, 1, 13, 30)), open: 1.5, high: 2.25, low: 1, close: 2, volume: 1200 }),
    create(BarSchema, { close: 9 }),
    create(BarSchema, { barStart: timestampFromMs(Date.UTC(2026, 8, 2, 3, 59)), open: 2, high: 3, low: 1.75, close: 2.5, volume: 0 }),
    create(BarSchema, { barStart: timestampFromMs(Date.UTC(2026, 8, 3, 14)), open: 3, high: 4, low: 2.5, close: 3.5, volume: 7 }),
  ]

  it.each([false, true])('header + rows of any split is byte-identical to barsToCsv (intraday %s)', (intraday) => {
    const whole = barsToCsv(bars, 'America/Toronto', intraday)
    for (const cut of [0, 1, 2, 3, 4]) {
      const pieced =
        `${CSV_HEADER}\n` +
        barsToCsvRows(bars.slice(0, cut), 'America/Toronto', intraday) +
        barsToCsvRows(bars.slice(cut), 'America/Toronto', intraday)
      expect(pieced).toBe(whole)
    }
  })

  it('every row ends in a newline, carries no header, and no bars is the empty string', () => {
    expect(barsToCsvRows(bars.slice(0, 1), 'UTC', true)).toBe('2026/09/01 13:30,1.5,2.25,1,2,1200\n')
    expect(barsToCsvRows([], 'UTC', false)).toBe('')
    expect(barsToCsvRows(bars.slice(1, 2), 'UTC', false)).toBe('')
  })
})
