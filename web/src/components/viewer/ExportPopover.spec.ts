import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { create } from '@bufbuild/protobuf'
import { timestampFromMs } from '@bufbuild/protobuf/wkt'

import type { Bar } from '@generated/trader_joe/proto/market/v1/bar_pb'
import { DataSource, DataType, Feed, Granularity, UpdateType } from '@generated/trader_joe/proto/market/v1/enums_pb'
import { DatasetSummarySchema } from '@generated/trader_joe/proto/ui/v1/data_pb'

import { ApiError } from '@/api'
import { useSettingsStore } from '@/stores/settings'
import { CSV_HEADER } from '@/viewer/csv'
import ExportPopover from './ExportPopover.vue'

// tj-grna9p.126, owner rulings of 2026-10-13 on tj-grna9p.118 (canvas Export board): Export CSV opens a
// POPOVER whose From and To default to the Viewer's window; 'Earliest' beside From and 'Latest' beside
// To lock that side to the stored extent and GREY OUT the field; Export walks the bars by cursor at the
// server's 50,000 maximum with a progress bar and Stop; an error shows in place with its Error ID and
// Try Again; no size line, no hard limit. Builder deviations recorded on the bead: a ticked side OMITS
// the bound (the file reaches the last stored bar) and shows the first / last bar's day; a 340 px
// non-modal dialog.
//
// WHAT THE STUBS HIDE: the API is mocked at the typed client; URL.createObjectURL and the anchor click
// that starts the download are stubbed (the spec reads the Blob and the file name). jsdom has no layout,
// so the "equal width" of the date fields is checked as structure (one shared grid), not as pixels.

const api = vi.hoisted(() => ({ getDatasetBars: vi.fn() }))
vi.mock('@/api', async (importOriginal) => ({ ...(await importOriginal<typeof import('@/api')>()), ...api }))

const DAY = 86_400_000
const T0 = Date.UTC(2026, 8, 1, 4) // 2026/09/01 00:00 in Toronto

function barAt(i: number): Bar {
  return {
    barStart: { seconds: BigInt((T0 + i * DAY) / 1000), nanos: 0 },
    open: i,
    high: i + 1,
    low: i - 1,
    close: i + 0.5,
    volume: 10,
  } as unknown as Bar
}

const dataset = create(DatasetSummarySchema, {
  id: 'ds-1',
  assetSymbol: 'VFV',
  dataType: DataType.MARKET_ACTIVITY,
  source: DataSource.ALPACA_API,
  feed: Feed.IEX,
  granularity: Granularity.ONE_DAY,
  updateType: UpdateType.DAILY,
  barCount: 120_000n,
  firstBar: timestampFromMs(T0),
  lastBar: timestampFromMs(T0 + 40 * DAY), // 2026/10/11
})

// The bars route over `total` daily bars: [start, end), from the cursor, at most `limit`.
const held: Array<() => void> = []
function serve(total: number, holdCursor?: string): void {
  api.getDatasetBars.mockImplementation(
    async (_id: string, p: { start?: Date; end?: Date; cursor?: string; limit?: number; signal?: AbortSignal }) => {
      if (holdCursor !== undefined && p.cursor === holdCursor) {
        await new Promise<void>((resolve, reject) => {
          held.push(resolve)
          p.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
        })
      }
      const a = p.start ? Math.ceil((p.start.getTime() - T0) / DAY) : 0
      const b = p.end ? Math.min(total, Math.ceil((p.end.getTime() - T0) / DAY)) : total
      const offset = p.cursor === undefined ? Math.max(a, 0) : Number(p.cursor)
      const count = Math.max(0, Math.min(p.limit ?? 1000, b - offset))
      return {
        bars: Array.from({ length: count }, (_, i) => barAt(offset + i)),
        nextCursor: offset + count < b ? String(offset + count) : '',
      }
    },
  )
}

const calls = () =>
  api.getDatasetBars.mock.calls as Array<[string, { start?: Date; end?: Date; cursor?: string; limit?: number; signal: AbortSignal }]>

const blobs: Blob[] = []
const names: string[] = []
const mounted: VueWrapper[] = []

beforeEach(() => {
  setActivePinia(createPinia())
  useSettingsStore().setTimeZoneOverride('America/Toronto')
  api.getDatasetBars.mockReset()
  held.length = 0
  blobs.length = 0
  names.length = 0
  Object.assign(URL, {
    createObjectURL: vi.fn((blob: Blob) => {
      blobs.push(blob)
      return 'blob:test'
    }),
    revokeObjectURL: vi.fn(),
  })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
    names.push(this.download)
  })
  serve(30)
})

afterEach(() => {
  mounted.splice(0).forEach((w) => w.unmount())
  vi.restoreAllMocks()
})

function popover(range: { from?: string; to?: string } = { from: '2026-09-02', to: '2026-09-05' }, disabled = false): VueWrapper {
  const wrapper = mount(ExportPopover, { props: { dataset, range, disabled }, attachTo: document.body })
  mounted.push(wrapper)
  return wrapper
}

async function opened(range?: { from?: string; to?: string }): Promise<VueWrapper> {
  const w = popover(range)
  await w.find('[data-testid="export-csv"]').trigger('click')
  await nextTick()
  return w
}

const field = (w: VueWrapper, which: 'from' | 'to') => w.find(`[data-testid="export-${which}"]`)
const fieldEl = (w: VueWrapper, which: 'from' | 'to') => field(w, which).element as HTMLInputElement
const tick = (w: VueWrapper, which: 'earliest' | 'latest') => w.find(`[data-testid="export-${which}"]`)

describe('opening', () => {
  it('the button opens a dialog popover whose From and To are the Viewer\'s window', async () => {
    const w = popover()
    const trigger = w.find('[data-testid="export-csv"]')
    expect(trigger.attributes('aria-haspopup')).toBe('dialog')
    expect(trigger.attributes('aria-expanded')).toBe('false')
    expect(w.find('[data-testid="export-popover"]').exists()).toBe(false)
    await trigger.trigger('click')
    await nextTick()
    const pop = w.find('[data-testid="export-popover"]')
    expect(pop.attributes('role')).toBe('dialog')
    expect(pop.find('h2').text()).toBe('Export Range')
    expect(trigger.attributes('aria-expanded')).toBe('true')
    expect(fieldEl(w, 'from').value).toBe('2026/09/02')
    expect(fieldEl(w, 'to').value).toBe('2026/09/05')
    expect((tick(w, 'earliest').element as HTMLInputElement).checked).toBe(false)
    expect((tick(w, 'latest').element as HTMLInputElement).checked).toBe(false)
    expect(fieldEl(w, 'from').disabled).toBe(false)
    expect(document.activeElement).toBe(fieldEl(w, 'from'))
    expect(w.text()).not.toMatch(/\bMB\b|\bKB\b|rows|bars will/i) // no size line
  })

  it('an open side of the window starts ticked: Earliest / Latest, the field locked on the stored day', async () => {
    const w = await opened({})
    expect((tick(w, 'earliest').element as HTMLInputElement).checked).toBe(true)
    expect((tick(w, 'latest').element as HTMLInputElement).checked).toBe(true)
    expect(fieldEl(w, 'from').disabled).toBe(true)
    expect(fieldEl(w, 'from').value).toBe('2026/09/01')
    expect(fieldEl(w, 'to').value).toBe('2026/10/11')
    expect(w.find('.export__note').text()).toContain('Everything stored for this dataset')
  })

  it('the trigger is disabled when the dataset has nothing stored', () => {
    const w = popover(undefined, true)
    expect(w.find('[data-testid="export-csv"]').attributes('disabled')).toBeDefined()
  })

  it('the two date fields share one grid, each with its tick beside it', async () => {
    const w = await opened()
    const grid = w.find('.export__grid').element
    expect(fieldEl(w, 'from').parentElement).toBe(grid)
    expect(fieldEl(w, 'to').parentElement).toBe(grid)
    expect([...grid.children].map((el) => el.textContent?.trim() || (el as HTMLInputElement).dataset.testid)).toEqual([
      'From',
      'export-from',
      'Earliest',
      'To',
      'export-to',
      'Latest',
    ])
  })
})

describe('Earliest and Latest', () => {
  it('ticking Earliest greys and locks From on the first stored day; unticking restores the typed day', async () => {
    const w = await opened()
    await field(w, 'from').setValue('2026/09/03')
    await tick(w, 'earliest').setValue(true)
    expect(fieldEl(w, 'from').disabled).toBe(true)
    expect(field(w, 'from').classes()).toContain('is-locked')
    expect(fieldEl(w, 'from').value).toBe('2026/09/01')
    await tick(w, 'earliest').setValue(false)
    expect(fieldEl(w, 'from').disabled).toBe(false)
    expect(field(w, 'from').classes()).not.toContain('is-locked')
    expect(fieldEl(w, 'from').value).toBe('2026/09/03')
  })

  it('ticking Latest greys and locks To on the last stored day', async () => {
    const w = await opened()
    await tick(w, 'latest').setValue(true)
    expect(fieldEl(w, 'to').disabled).toBe(true)
    expect(field(w, 'to').classes()).toContain('is-locked')
    expect(fieldEl(w, 'to').value).toBe('2026/10/11')
  })

  it('Earliest OMITS the start bound and Latest the end bound: the file is everything stored', async () => {
    const w = await opened()
    await tick(w, 'earliest').setValue(true)
    await tick(w, 'latest').setValue(true)
    await w.find('[data-testid="export-run"]').trigger('click')
    await flushPromises()
    expect(calls()).toHaveLength(1)
    expect(calls()[0]![1].start).toBeUndefined()
    expect(calls()[0]![1].end).toBeUndefined()
    expect(names).toEqual(['VFV_1d_2026-09-01_2026-10-11.csv'])
    const lines = (await blobs[0]!.text()).split('\n')
    expect(lines).toHaveLength(32) // header, all 30 bars, trailing newline
  })

  it('Earliest alone keeps the typed To as the end', async () => {
    const w = await opened()
    await tick(w, 'earliest').setValue(true)
    await w.find('[data-testid="export-run"]').trigger('click')
    await flushPromises()
    expect(calls()[0]![1].start).toBeUndefined()
    expect(calls()[0]![1].end!.toISOString()).toBe('2026-09-05T04:00:00.000Z')
    expect(names).toEqual(['VFV_1d_2026-09-01_2026-09-05.csv'])
  })
})

// tj-grna9p.127: the note under the fields follows the ticks. A typed To is exclusive (the app's
// half-open rule) and the note says so; with Latest ticked the export omits the end bound and reaches the
// last stored bar, which IS included, so that note must not say "not included".
describe('the note follows the ticks', () => {
  const note = (w: VueWrapper) => w.find('.export__note').text()
  const BETWEEN = 'Exports the bars between these dates at this granularity. To is not included.'
  const TO_LAST = 'Exports the bars from this date to the last stored bar at this granularity.'
  const EVERYTHING = 'Everything stored for this dataset. A long export can take a while; you can cancel.'

  it('neither ticked: between these dates, To not included', async () => {
    const w = await opened()
    expect(note(w)).toBe(BETWEEN)
  })

  it('Earliest only: the same, the typed To is still exclusive', async () => {
    const w = await opened()
    await tick(w, 'earliest').setValue(true)
    expect(note(w)).toBe(BETWEEN)
  })

  it('Latest only: to the last stored bar, and never "not included"', async () => {
    const w = await opened()
    await tick(w, 'latest').setValue(true)
    expect(note(w)).toBe(TO_LAST)
    expect(note(w)).not.toMatch(/not included/i)
    await tick(w, 'latest').setValue(false)
    expect(note(w)).toBe(BETWEEN)
  })

  it('both ticked: everything stored', async () => {
    const w = await opened()
    await tick(w, 'earliest').setValue(true)
    await tick(w, 'latest').setValue(true)
    expect(note(w)).toBe(EVERYTHING)
  })

  it('busy: the download note, whatever the ticks', async () => {
    // From 2026/09/02 is bar 1, so the second request's cursor is 50,001: held there, mid-walk.
    serve(120_000, '50001')
    const w = await opened({ from: '2026-09-02' }) // Latest starts ticked: an open To
    expect(note(w)).toBe(TO_LAST)
    await w.find('[data-testid="export-run"]').trigger('click')
    await flushPromises()
    expect(w.find('[data-testid="export-progress"]').exists()).toBe(true)
    expect(note(w)).toBe('The file downloads when it is ready.')
    await w.find('[data-testid="export-stop"]').trigger('click')
    await flushPromises()
    expect(note(w)).toBe(TO_LAST)
  })
})

describe('validation', () => {
  it.each([
    ['from', '2026/02/30', 'From must be a date as YYYY/MM/DD.'],
    ['to', 'next week', 'To must be a date as YYYY/MM/DD.'],
    ['to', '2026/09/02', 'To must be after From (To is not included).'],
  ] as const)('a bad %s (%j) is refused in place with the sidebar wording and nothing is read', async (which, text, message) => {
    const w = await opened()
    await field(w, which).setValue(text)
    await w.find('[data-testid="export-run"]').trigger('click')
    await flushPromises()
    expect(w.find('[data-testid="export-problem"]').text()).toBe(message)
    expect(w.find('[data-testid="export-problem"]').attributes('role')).toBe('alert')
    expect(w.find('[data-testid="export-popover"]').exists()).toBe(true)
    expect(calls()).toHaveLength(0)
    expect(names).toEqual([])
  })

  it('Enter in a field runs the export', async () => {
    const w = await opened()
    await field(w, 'to').trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(names).toEqual(['VFV_1d_2026-09-02_2026-09-05.csv'])
  })
})

describe('the export run', () => {
  it('walks the window at 50,000 bars a request, writes [From, To) in order and downloads it named with the range', async () => {
    const w = await opened()
    await w.find('[data-testid="export-run"]').trigger('click')
    await flushPromises()
    expect(calls().map(([id, p]) => [id, p.limit, p.cursor, p.start!.toISOString(), p.end!.toISOString()])).toEqual([
      ['ds-1', 50_000, undefined, '2026-09-02T04:00:00.000Z', '2026-09-05T04:00:00.000Z'],
    ])
    expect(names).toEqual(['VFV_1d_2026-09-02_2026-09-05.csv'])
    expect(await blobs[0]!.text()).toBe(`${CSV_HEADER}\n2026/09/02,1,2,0,1.5,10\n2026/09/03,2,3,1,2.5,10\n2026/09/04,3,4,2,3.5,10\n`)
    // Done: the popover closes and focus goes back to the button.
    expect(w.find('[data-testid="export-popover"]').exists()).toBe(false)
    await nextTick()
    expect(document.activeElement).toBe(w.find('[data-testid="export-csv"]').element)
  })

  it('a long walk shows progress and Stop; Stop aborts, drops the partial file and reads no further', async () => {
    serve(120_000, '50000')
    const w = await opened({})
    await w.find('[data-testid="export-run"]').trigger('click')
    await flushPromises()
    expect(calls().map(([, p]) => [p.cursor, p.limit])).toEqual([
      [undefined, 50_000],
      ['50000', 50_000],
    ])
    const bar = w.find('[data-testid="export-progress"]')
    expect(bar.attributes('role')).toBe('progressbar')
    expect(Number(bar.attributes('aria-valuenow'))).toBeGreaterThan(0)
    expect(w.find('[data-testid="export-run"]').attributes('disabled')).toBeDefined()
    expect(w.find('[data-testid="export-run"]').text()).toBe('Exporting')
    expect(fieldEl(w, 'from').disabled).toBe(true)
    expect((tick(w, 'latest').element as HTMLInputElement).disabled).toBe(true)
    expect(w.find('[data-testid="export-cancel"]').exists()).toBe(false)
    const signal = calls()[1]![1].signal
    await w.find('[data-testid="export-stop"]').trigger('click')
    await flushPromises()
    expect(signal.aborted).toBe(true)
    expect(calls()).toHaveLength(2)
    expect(names).toEqual([])
    expect(blobs).toEqual([])
    expect(w.find('[data-testid="export-progress"]').exists()).toBe(false)
    expect(w.find('[data-testid="export-popover"]').exists()).toBe(true)
    expect(w.find('[data-testid="export-run"]').text()).toBe('Export')
  })

  it('no bars between the dates says so and downloads nothing', async () => {
    const w = await opened({ from: '2027-01-01', to: '2027-02-01' })
    await w.find('[data-testid="export-run"]').trigger('click')
    await flushPromises()
    expect(w.find('[data-testid="export-problem"]').text()).toBe('There are no bars between these dates.')
    expect(names).toEqual([])
    expect(w.find('[data-testid="export-popover"]').exists()).toBe(true)
  })

  it('a failure shows in place with its Error ID, and Try Again runs it again', async () => {
    api.getDatasetBars.mockRejectedValueOnce(new ApiError({ status: 503, errorId: 'x-1' }))
    const w = await opened()
    await w.find('[data-testid="export-run"]').trigger('click')
    await flushPromises()
    const error = w.find('[data-testid="export-error"]')
    expect(error.attributes('role')).toBe('alert')
    expect(error.text()).toContain('Could not export.')
    expect(error.text()).toContain('Error ID: x-1')
    const again = w.find('[data-testid="export-run"]')
    expect(again.text()).toBe('Try Again')
    await again.trigger('click')
    await flushPromises()
    expect(calls()).toHaveLength(2)
    expect(names).toEqual(['VFV_1d_2026-09-02_2026-09-05.csv'])
  })
})

describe('closing', () => {
  it('Escape closes and focus returns to the button', async () => {
    const w = await opened()
    await field(w, 'from').trigger('keydown', { key: 'Escape' })
    expect(w.find('[data-testid="export-popover"]').exists()).toBe(false)
    await nextTick()
    expect(document.activeElement).toBe(w.find('[data-testid="export-csv"]').element)
  })

  it('Cancel closes and focus returns to the button', async () => {
    const w = await opened()
    await w.find('[data-testid="export-cancel"]').trigger('click')
    expect(w.find('[data-testid="export-popover"]').exists()).toBe(false)
    await nextTick()
    expect(document.activeElement).toBe(w.find('[data-testid="export-csv"]').element)
  })

  it('a click outside closes an idle popover; a click inside does not', async () => {
    const w = await opened()
    w.find('[data-testid="export-popover"]').element.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true }))
    await nextTick()
    expect(w.find('[data-testid="export-popover"]').exists()).toBe(true)
    document.body.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true }))
    await nextTick()
    expect(w.find('[data-testid="export-popover"]').exists()).toBe(false)
  })

  it('while exporting a click outside does not close it; Escape closes and aborts the run', async () => {
    serve(120_000, '50000')
    const w = await opened({})
    await w.find('[data-testid="export-run"]').trigger('click')
    await flushPromises()
    document.body.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true }))
    await nextTick()
    expect(w.find('[data-testid="export-popover"]').exists()).toBe(true)
    const signal = calls()[1]![1].signal
    await w.find('[data-testid="export-stop"]').trigger('keydown', { key: 'Escape' })
    await flushPromises()
    expect(signal.aborted).toBe(true)
    expect(w.find('[data-testid="export-popover"]').exists()).toBe(false)
    expect(names).toEqual([])
  })

  it('re-opening starts again from the Viewer\'s window', async () => {
    const w = await opened()
    await field(w, 'from').setValue('2026/09/04')
    await tick(w, 'latest').setValue(true)
    await w.find('[data-testid="export-cancel"]').trigger('click')
    await w.setProps({ range: { from: '2026-09-10', to: '2026-09-20' } })
    await w.find('[data-testid="export-csv"]').trigger('click')
    await nextTick()
    expect(fieldEl(w, 'from').value).toBe('2026/09/10')
    expect(fieldEl(w, 'to').value).toBe('2026/09/20')
    expect((tick(w, 'latest').element as HTMLInputElement).checked).toBe(false)
  })
})
