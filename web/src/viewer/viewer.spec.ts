import { describe, expect, it, vi } from 'vitest'
import { create } from '@bufbuild/protobuf'
import { timestampFromMs } from '@bufbuild/protobuf/wkt'

import { BarSchema, type Bar } from '@generated/trader_joe/proto/market/v1/bar_pb'

import { CHART_PAGE_SIZE, MAX_CHART_BARS, TABLE_PAGE_SIZE, loadBarWindow } from './barWindow'
import type { BarPageResult } from './barWindow'
import { CSV_HEADER, barsToCsv } from './csv'
import {
  displayDay,
  parseDay,
  parseRangeQuery,
  rangeProblem,
  toApiWindow,
  toRangeQuery,
  zonedDayStart,
} from './range'

// tj-grna9p.33: the Viewer's bounded bar load, its half-open range and its CSV export.

const bar = (ms: number, close = 1): Bar =>
  create(BarSchema, { barStart: timestampFromMs(ms), open: 1, high: 2, low: 0.5, close, volume: 10 })

// A server that holds `total` bars and answers each page with at most `limit` of them.
function server(total: number) {
  return vi.fn(async (cursor: string | undefined, limit: number): Promise<BarPageResult> => {
    const offset = cursor === undefined ? 0 : Number(cursor)
    const count = Math.max(0, Math.min(limit, total - offset))
    const bars = Array.from({ length: count }, (_, i) => bar(offset + i))
    const next = offset + count
    return { bars, nextCursor: next < total ? String(next) : '' }
  })
}

describe('loadBarWindow', () => {
  it('caps the chart at 50,000 bars in five 10,000-bar pages and reports truncated', async () => {
    expect(MAX_CHART_BARS).toBe(50_000)
    expect(CHART_PAGE_SIZE).toBe(10_000)
    expect(TABLE_PAGE_SIZE).toBe(500)
    const fetchPage = server(73_000)
    const result = await loadBarWindow({ fetchPage, signal: new AbortController().signal })
    expect(result.bars).toHaveLength(50_000)
    expect(result.truncated).toBe(true)
    expect(result.pages).toBe(5)
    expect(fetchPage.mock.calls.map(([cursor, limit]) => [cursor, limit])).toEqual([
      [undefined, 10_000],
      ['10000', 10_000],
      ['20000', 10_000],
      ['30000', 10_000],
      ['40000', 10_000],
    ])
  })

  it('the last request asks only for the room left under the cap', async () => {
    const fetchPage = server(100)
    const result = await loadBarWindow({ fetchPage, signal: new AbortController().signal, cap: 25, pageSize: 10 })
    expect(fetchPage.mock.calls.map(([, limit]) => limit)).toEqual([10, 10, 5])
    expect(result.bars).toHaveLength(25)
    expect(result.truncated).toBe(true)
  })

  it('a window that ends exactly at the cap is not truncated', async () => {
    const result = await loadBarWindow({ fetchPage: server(50_000), signal: new AbortController().signal })
    expect(result.bars).toHaveLength(50_000)
    expect(result.truncated).toBe(false)
  })

  it('a short window loads in one request; an empty one is empty and not truncated', async () => {
    const short = await loadBarWindow({ fetchPage: server(7), signal: new AbortController().signal })
    expect(short).toMatchObject({ truncated: false, pages: 1 })
    expect(short.bars).toHaveLength(7)
    const empty = await loadBarWindow({ fetchPage: server(0), signal: new AbortController().signal })
    expect(empty).toEqual({ bars: [], truncated: false, pages: 1 })
  })

  it('passes the signal through and rejects as the fetch does', async () => {
    const controller = new AbortController()
    const fetchPage = vi.fn().mockRejectedValue(new DOMException('aborted', 'AbortError'))
    await expect(loadBarWindow({ fetchPage, signal: controller.signal })).rejects.toThrow('aborted')
    expect(fetchPage).toHaveBeenCalledWith(undefined, CHART_PAGE_SIZE, controller.signal)
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
