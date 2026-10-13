import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { Bar } from '@generated/trader_joe/proto/market/v1/bar_pb'

import { CSV_HEADER, barsToCsv } from './csv'
import { EXPORT_PAGE_SIZE, exportBarsCsv } from './exportCsv'
import { exportFileName, planExport } from './exportRange'
import type { ExportRangeInput } from './exportRange'

// tj-grna9p.126: what the Export CSV popover asks for (planExport, exportFileName) and the cursor walk
// that writes the file (exportBarsCsv). Owner rulings 2026-10-13 on tj-grna9p.118: the popover's From
// and To default to the Viewer's window; Earliest / Latest LOCK that side to the stored extent, which
// the builder implemented as OMITTING the bound (no start / no end) so the last stored bar is
// included; the walk reads by cursor at the server's 50,000-bar maximum (tj-grna9p.125), with no size
// limit assumed and Stop aborting it.

const api = vi.hoisted(() => ({ getDatasetBars: vi.fn() }))
vi.mock('@/api', async (importOriginal) => ({ ...(await importOriginal<typeof import('@/api')>()), ...api }))

const DAY = 86_400_000
const ZONE = 'America/Toronto'
const FIRST = Date.UTC(2016, 0, 4, 5) // 2016/01/04 in Toronto
const LAST = Date.UTC(2026, 9, 9, 4) // 2026/10/09 in Toronto

function input(extra: Partial<ExportRangeInput> = {}): ExportRangeInput {
  return {
    fromText: '2026/09/01',
    toText: '2026/09/08',
    earliest: false,
    latest: false,
    timeZone: ZONE,
    firstBar: FIRST,
    lastBar: LAST,
    ...extra,
  }
}

describe('planExport', () => {
  it('two typed days are the half-open window of their zone day starts, and name the file', () => {
    const result = planExport(input())
    expect(result).toEqual({
      ok: true,
      plan: {
        start: new Date('2026-09-01T04:00:00.000Z'),
        end: new Date('2026-09-08T04:00:00.000Z'),
        spanStart: Date.parse('2026-09-01T04:00:00.000Z'),
        spanEnd: Date.parse('2026-09-08T04:00:00.000Z'),
        fromTag: '2026-09-01',
        toTag: '2026-09-08',
      },
    })
    expect(planExport(input({ fromText: '2026-09-01' }))).toEqual(result)
  })

  it('Earliest OMITS the start, whatever the locked field holds; the extent names the file', () => {
    const result = planExport(input({ earliest: true, fromText: 'not a day' }))
    expect(result.ok).toBe(true)
    if (!result.ok) return
    expect(result.plan.start).toBeUndefined()
    expect(result.plan.end).toEqual(new Date('2026-09-08T04:00:00.000Z'))
    expect(result.plan.spanStart).toBe(FIRST)
    expect(result.plan.fromTag).toBe('2016-01-04')
  })

  it('Latest OMITS the end, so the last stored bar is in the file', () => {
    const result = planExport(input({ latest: true, toText: '' }))
    expect(result.ok).toBe(true)
    if (!result.ok) return
    expect(result.plan.start).toEqual(new Date('2026-09-01T04:00:00.000Z'))
    expect(result.plan.end).toBeUndefined()
    expect(result.plan.spanEnd).toBe(LAST)
    expect(result.plan.toTag).toBe('2026-10-09')
  })

  it('both ticks are the whole dataset: no bound at all', () => {
    const result = planExport(input({ earliest: true, latest: true }))
    expect(result.ok && result.plan.start === undefined && result.plan.end === undefined).toBe(true)
  })

  it('refuses a malformed day with the sidebar\'s own wording', () => {
    expect(planExport(input({ fromText: '2026/02/30' }))).toEqual({ ok: false, message: 'From must be a date as YYYY/MM/DD.' })
    expect(planExport(input({ toText: 'soon' }))).toEqual({ ok: false, message: 'To must be a date as YYYY/MM/DD.' })
    expect(planExport(input({ toText: '2026/09/01' }))).toEqual({ ok: false, message: 'To must be after From (To is not included).' })
  })

  it('an unticked empty field leaves its bound off; an unknown extent leaves no tag', () => {
    const result = planExport(input({ fromText: '  ', firstBar: undefined }))
    expect(result.ok).toBe(true)
    if (!result.ok) return
    expect(result.plan.start).toBeUndefined()
    expect(result.plan.spanStart).toBeUndefined()
    expect(result.plan.fromTag).toBe('')
  })
})

describe('exportFileName', () => {
  it('is SYMBOL_granularity_from_to.csv, with no slash in it', () => {
    const plan = { fromTag: '2026-09-01', toTag: '2026-09-08' }
    expect(exportFileName('VFV', '1d', plan)).toBe('VFV_1d_2026-09-01_2026-09-08.csv')
  })

  it('drops a tag that is unknown', () => {
    expect(exportFileName('VFV', '1m', { fromTag: '', toTag: '2026-09-08' })).toBe('VFV_1m_2026-09-08.csv')
  })
})

// A store of `total` daily bars from T0 that answers the cursor walk the way the bars route does: at
// most `limit` bars from the cursor, a next cursor while more remain. Bars are bare objects: the CSV
// writer reads only their start and the five values.
const T0 = Date.UTC(2020, 0, 1, 5)
function barAt(i: number): Bar {
  return {
    barStart: { seconds: BigInt((T0 + i * DAY) / 1000), nanos: 0 },
    open: i,
    high: i + 1,
    low: i - 1,
    close: i + 0.5,
    volume: i * 10,
  } as unknown as Bar
}

function serve(total: number): void {
  api.getDatasetBars.mockImplementation(async (_id: string, params: { cursor?: string; limit?: number; signal?: AbortSignal }) => {
    const offset = params.cursor === undefined ? 0 : Number(params.cursor)
    const count = Math.max(0, Math.min(params.limit ?? 1000, total - offset))
    const bars = Array.from({ length: count }, (_, i) => barAt(offset + i))
    return { bars, nextCursor: offset + count < total ? String(offset + count) : '' }
  })
}

const calls = () => api.getDatasetBars.mock.calls as Array<[string, Record<string, unknown>]>

beforeEach(() => {
  api.getDatasetBars.mockReset()
})

describe('exportBarsCsv', () => {
  const base = { datasetId: 'ds-1', timeZone: 'UTC', intraday: false }

  it('walks the cursor at 50,000 bars a request and writes header + every bar, in order', async () => {
    expect(EXPORT_PAGE_SIZE).toBe(50_000)
    serve(120_000)
    const start = new Date(T0)
    const end = new Date(T0 + 200_000 * DAY)
    const signal = new AbortController().signal
    const result = await exportBarsCsv({ ...base, start, end, signal })
    expect(calls().map(([id, p]) => [id, p.cursor, p.limit, p.start, p.end, p.signal])).toEqual([
      ['ds-1', undefined, 50_000, start, end, signal],
      ['ds-1', '50000', 50_000, start, end, signal],
      ['ds-1', '100000', 50_000, start, end, signal],
    ])
    expect(result!.bars).toBe(120_000)
    expect(result!.blob.type).toBe('text/csv;charset=utf-8')
    const text = await result!.blob.text()
    const lines = text.split('\n')
    expect(lines[0]).toBe(CSV_HEADER)
    expect(lines).toHaveLength(120_002) // header, 120,000 rows, and the empty string after the last newline
    expect(lines[1]).toBe('2020/01/01,0,1,-1,0.5,0')
    expect(lines[120_000]).toBe(barsToCsv([barAt(119_999)], 'UTC', false).split('\n')[1])
  })

  it('is byte-identical to barsToCsv over the same bars', async () => {
    serve(7)
    const result = await exportBarsCsv({ ...base, intraday: true, timeZone: ZONE, signal: new AbortController().signal })
    const all = Array.from({ length: 7 }, (_, i) => barAt(i))
    expect(await result!.blob.text()).toBe(barsToCsv(all, ZONE, true))
  })

  it('an open range sends no start and no end', async () => {
    serve(3)
    await exportBarsCsv({ ...base, signal: new AbortController().signal })
    expect(calls()[0]![1]).toMatchObject({ start: undefined, end: undefined, limit: 50_000 })
  })

  it('reports progress as the time reached across the span, never going back, ending at 1', async () => {
    serve(120_000)
    const seen: number[] = []
    await exportBarsCsv({
      ...base,
      spanStart: T0,
      spanEnd: T0 + 120_000 * DAY,
      signal: new AbortController().signal,
      onProgress: (f) => seen.push(f),
    })
    expect(seen).toHaveLength(4)
    expect(seen[0]).toBeCloseTo(49_999 / 120_000, 6)
    expect(seen[1]).toBeCloseTo(99_999 / 120_000, 6)
    expect(seen.at(-1)).toBe(1)
    expect([...seen].sort((a, b) => a - b)).toEqual(seen)
  })

  it('no bars in the range is an empty file of the header alone, with zero bars', async () => {
    serve(0)
    const result = await exportBarsCsv({ ...base, signal: new AbortController().signal })
    expect(result!.bars).toBe(0)
    expect(await result!.blob.text()).toBe(`${CSV_HEADER}\n`)
  })

  it('Stop aborts the walk: no further request and the partial file is dropped (null)', async () => {
    serve(120_000)
    const controller = new AbortController()
    const inner = api.getDatasetBars.getMockImplementation()!
    api.getDatasetBars.mockImplementation(async (id: string, params: { cursor?: string; signal: AbortSignal }) => {
      if (params.cursor === '50000') {
        controller.abort()
        throw new DOMException('aborted', 'AbortError')
      }
      return inner(id, params)
    })
    const result = await exportBarsCsv({ ...base, signal: controller.signal })
    expect(result).toBeNull()
    expect(calls()).toHaveLength(2)
  })

  it('an abort that lands after a page answered still drops the file', async () => {
    serve(120_000)
    const controller = new AbortController()
    const inner = api.getDatasetBars.getMockImplementation()!
    api.getDatasetBars.mockImplementation(async (id: string, params: object) => {
      const page = await inner(id, params)
      controller.abort()
      return page
    })
    expect(await exportBarsCsv({ ...base, signal: controller.signal })).toBeNull()
    expect(calls()).toHaveLength(1)
  })

  it('a failed request rejects with its error', async () => {
    const failure = new Error('bars down')
    api.getDatasetBars.mockRejectedValue(failure)
    await expect(exportBarsCsv({ ...base, signal: new AbortController().signal })).rejects.toBe(failure)
  })

  it('a cursor the server repeats ends the walk rather than looping', async () => {
    api.getDatasetBars.mockResolvedValue({ bars: [barAt(0)], nextCursor: 'same' })
    const result = await exportBarsCsv({ ...base, signal: new AbortController().signal })
    expect(calls()).toHaveLength(2)
    expect(result!.bars).toBe(2)
  })
})
