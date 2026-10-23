import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { create } from '@bufbuild/protobuf'
import type { MessageInitShape } from '@bufbuild/protobuf'
import { timestampFromMs } from '@bufbuild/protobuf/wkt'

import {
  ClientKind,
  DatasetSummarySchema,
  IngestRunPageSchema,
  IngestRunSchema,
  IngestRunState,
} from '@generated/trader_joe/proto/ui/v1/data_pb'
import type { IngestRun } from '@generated/trader_joe/proto/ui/v1/data_pb'
import { ReadsPerDaySchema } from '@generated/trader_joe/proto/ui/v1/stats_pb'

import { ApiError } from '@/api'
import { READS_WINDOW_DAYS, RUNS_PAGE_SIZE, mergeRuns, useDatasetDetailStore } from './datasetDetail'
import type { WriteOutcome } from './datasetDetail'

// tj-grna9p.34: the Dataset detail store. The API is mocked at the typed client (@/api); the store is
// the real one. Fixtures are create(Schema) from the generated ui/v1 messages.
const mocks = vi.hoisted(() => ({
  getDataset: vi.fn(),
  getDatasetRuns: vi.fn(),
  getReadsPerDay: vi.fn(),
  setDatasetExpiry: vi.fn(),
  deleteDataset: vi.fn(),
}))
vi.mock('@/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api')>()),
  ...mocks,
}))

const T0 = Date.UTC(2026, 9, 12, 14)

function run(id: string, minutes: number, extra: MessageInitShape<typeof IngestRunSchema> = {}): IngestRun {
  return create(IngestRunSchema, {
    id,
    datasetId: 'ds-1',
    state: IngestRunState.SUCCEEDED,
    startedAt: timestampFromMs(T0 + minutes * 60_000),
    ...extra,
  })
}

const DATASET = create(DatasetSummarySchema, { id: 'ds-1', assetSymbol: 'VFV', owner: 'operator' })
const HELD = create(DatasetSummarySchema, { id: 'ds-1', assetSymbol: 'VFV', owner: 'bot-7' })
const UPDATED = create(DatasetSummarySchema, { id: 'ds-1', assetSymbol: 'VFV', owner: 'operator', expiry: timestampFromMs(T0) })
const READS = create(ReadsPerDaySchema, { days: [{ day: '2026-10-12', clientId: 'bot-1', reads: 3n }] })
const OVERRIDE = { reason: 'cleanup', confirmed: true } as const
const page = (items: IngestRun[], nextCursor = '') => create(IngestRunPageSchema, { items, nextCursor })
const ids = (runs: readonly IngestRun[]) => runs.map((r) => r.id)

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  mocks.getDataset.mockResolvedValue(DATASET)
  mocks.getDatasetRuns.mockResolvedValue(page([run('r2', 2), run('r1', 1)]))
  mocks.getReadsPerDay.mockResolvedValue(READS)
  mocks.setDatasetExpiry.mockResolvedValue(UPDATED)
  mocks.deleteDataset.mockResolvedValue(undefined)
})

describe('mergeRuns', () => {
  it('upserts by id: an incoming run replaces the held one in place', () => {
    const held = [run('r2', 2, { state: IngestRunState.RUNNING }), run('r1', 1)]
    const merged = mergeRuns(held, [run('r2', 2, { state: IngestRunState.FAILED })])
    expect(ids(merged)).toEqual(['r2', 'r1'])
    expect(merged[0]!.state).toBe(IngestRunState.FAILED)
  })

  it('keeps newest first by start time, a new run on top, and breaks a tie by id so the order is stable', () => {
    const merged = mergeRuns([run('r1', 1), run('r0', 0)], [run('r3', 3), run('b', 2), run('a', 2)])
    expect(ids(merged)).toEqual(['r3', 'b', 'a', 'r1', 'r0'])
  })

  it('does not change what it was handed', () => {
    const held = [run('r1', 1)]
    mergeRuns(held, [run('r2', 2)])
    expect(ids(held)).toEqual(['r1'])
  })
})

describe('load', () => {
  it('fetches the dataset and the first runs page (no cursor, limit 50) in parallel', async () => {
    const store = useDatasetDetailStore()
    const done = store.load('ds-1')
    expect(store.status).toBe('loading')
    expect(store.runsStatus).toBe('loading')
    await done
    // The Read By choice (SDK by default) always goes with the dataset: it selects whose reads the
    // usage counts (tj-grna9p.89).
    expect(mocks.getDataset).toHaveBeenCalledWith('ds-1', expect.any(AbortSignal), { clientKinds: [ClientKind.SDK] })
    expect(mocks.getDatasetRuns).toHaveBeenCalledWith('ds-1', { limit: 50, signal: expect.any(AbortSignal) })
    expect(RUNS_PAGE_SIZE).toBe(50)
    expect(store.status).toBe('ready')
    expect(store.dataset).toBe(DATASET)
    expect(ids(store.runs)).toEqual(['r2', 'r1'])
    expect(store.hasMoreRuns).toBe(false)
  })

  it('an unknown id (404) is not-found, not an error to retry', async () => {
    mocks.getDataset.mockRejectedValue(new ApiError({ status: 404 }))
    const store = useDatasetDetailStore()
    await store.load('nope')
    expect(store.status).toBe('not-found')
  })

  it('any other failure is error, with its message and id', async () => {
    mocks.getDataset.mockRejectedValue(new ApiError({ status: 503, errorId: 'e-1' }))
    const store = useDatasetDetailStore()
    await store.load('ds-1')
    expect(store.status).toBe('error')
    expect(store.error).toEqual({ message: 'Request failed (HTTP 503)', errorId: 'e-1' })
  })

  it('a runs failure is the runs section own error; the overview still shows', async () => {
    mocks.getDatasetRuns.mockRejectedValue(new ApiError({ status: 500, errorId: 'r-1' }))
    const store = useDatasetDetailStore()
    await store.load('ds-1')
    expect(store.status).toBe('ready')
    expect(store.runsStatus).toBe('error')
    expect(store.runsError?.errorId).toBe('r-1')
    mocks.getDatasetRuns.mockResolvedValue(page([run('r1', 1)]))
    await store.reloadRuns()
    expect(store.runsStatus).toBe('ready')
    expect(ids(store.runs)).toEqual(['r1'])
  })

  it('a second load drops the first one answers', async () => {
    let answerFirst: (value: unknown) => void = () => undefined
    mocks.getDataset.mockImplementationOnce(() => new Promise((resolve) => (answerFirst = resolve)))
    const store = useDatasetDetailStore()
    const first = store.load('ds-old')
    await store.load('ds-1')
    answerFirst(create(DatasetSummarySchema, { id: 'ds-old' }))
    await first
    expect(store.dataset?.id).toBe('ds-1')
  })
})

describe('Load More (keyset)', () => {
  it('follows next_cursor, appends, and stops when the cursor is empty', async () => {
    mocks.getDatasetRuns.mockResolvedValueOnce(page([run('r9', 9), run('r8', 8)], 'k1'))
    const store = useDatasetDetailStore()
    await store.load('ds-1')
    expect(store.hasMoreRuns).toBe(true)
    mocks.getDatasetRuns.mockResolvedValueOnce(page([run('r7', 7)], 'k2'))
    await store.loadMoreRuns()
    expect(mocks.getDatasetRuns).toHaveBeenLastCalledWith('ds-1', { cursor: 'k1', limit: 50, signal: expect.any(AbortSignal) })
    mocks.getDatasetRuns.mockResolvedValueOnce(page([run('r6', 6)], ''))
    await store.loadMoreRuns()
    expect(mocks.getDatasetRuns).toHaveBeenLastCalledWith('ds-1', { cursor: 'k2', limit: 50, signal: expect.any(AbortSignal) })
    expect(ids(store.runs)).toEqual(['r9', 'r8', 'r7', 'r6'])
    expect(store.hasMoreRuns).toBe(false)
    await store.loadMoreRuns()
    expect(mocks.getDatasetRuns).toHaveBeenCalledTimes(3)
  })

  it('a click while a page is loading asks once', async () => {
    mocks.getDatasetRuns.mockResolvedValueOnce(page([run('r9', 9)], 'k1'))
    const store = useDatasetDetailStore()
    await store.load('ds-1')
    mocks.getDatasetRuns.mockResolvedValueOnce(page([run('r8', 8)]))
    const first = store.loadMoreRuns()
    expect(store.loadingMore).toBe(true)
    await store.loadMoreRuns()
    await first
    expect(mocks.getDatasetRuns).toHaveBeenCalledTimes(2)
    expect(store.loadingMore).toBe(false)
  })

  it('a failed page keeps the rows and the cursor, so it can be asked again', async () => {
    mocks.getDatasetRuns.mockResolvedValueOnce(page([run('r9', 9)], 'k1'))
    const store = useDatasetDetailStore()
    await store.load('ds-1')
    mocks.getDatasetRuns.mockRejectedValueOnce(new ApiError({ status: 500 }))
    await store.loadMoreRuns()
    expect(store.moreError?.message).toBe('Request failed (HTTP 500)')
    expect(ids(store.runs)).toEqual(['r9'])
    expect(store.runsCursor).toBe('k1')
  })
})

describe('live updates', () => {
  it('upsertRun puts this dataset run in place, newest first, without a fetch', async () => {
    const store = useDatasetDetailStore()
    await store.load('ds-1')
    store.upsertRun(run('r3', 3, { state: IngestRunState.RUNNING }))
    store.upsertRun(run('r2', 2, { state: IngestRunState.FAILED }))
    expect(ids(store.runs)).toEqual(['r3', 'r2', 'r1'])
    expect(store.runs[1]!.state).toBe(IngestRunState.FAILED)
    expect(mocks.getDatasetRuns).toHaveBeenCalledTimes(1)
  })

  it('upsertRun ignores a run of another dataset', async () => {
    const store = useDatasetDetailStore()
    await store.load('ds-1')
    store.upsertRun(run('x', 5, { datasetId: 'ds-2' }))
    expect(ids(store.runs)).toEqual(['r2', 'r1'])
  })

  it('a soft refresh keeps the data on screen until the new data replaces it', async () => {
    const store = useDatasetDetailStore()
    await store.load('ds-1')
    let answer: (value: unknown) => void = () => undefined
    mocks.getDataset.mockImplementationOnce(() => new Promise((resolve) => (answer = resolve)))
    const pending = store.refresh()
    expect(store.status).toBe('ready')
    expect(store.dataset).toBe(DATASET)
    const updated = create(DatasetSummarySchema, { id: 'ds-1', assetSymbol: 'VFV', barCount: 9n })
    answer(updated)
    await pending
    expect(store.dataset).toBe(updated)
  })

  it('a soft refresh that fails keeps the last data and raises the last-data notice', async () => {
    const store = useDatasetDetailStore()
    await store.load('ds-1')
    mocks.getDataset.mockRejectedValueOnce(new ApiError({ status: 503 }))
    await store.refresh()
    expect(store.status).toBe('ready')
    expect(store.dataset).toBe(DATASET)
    expect(store.refreshProblem?.message).toBe('Request failed (HTTP 503)')
    // The next good refresh clears the notice.
    await store.refresh()
    expect(store.refreshProblem).toBeNull()
  })

  it('a soft refresh that finds the dataset gone (404) marks it deleted', async () => {
    const store = useDatasetDetailStore()
    await store.load('ds-1')
    mocks.getDataset.mockRejectedValueOnce(new ApiError({ status: 404 }))
    await store.refresh()
    expect(store.deleted).toBe(true)
    expect(store.status).toBe('ready')
  })

  it('markDeleted is the DELETED event', async () => {
    const store = useDatasetDetailStore()
    await store.load('ds-1')
    store.markDeleted()
    expect(store.deleted).toBe(true)
  })

  it('reload (a reconnect) refetches softly and keeps loaded older pages and their cursor', async () => {
    mocks.getDatasetRuns.mockResolvedValueOnce(page([run('r9', 9), run('r8', 8)], 'k1'))
    const store = useDatasetDetailStore()
    await store.load('ds-1')
    mocks.getDatasetRuns.mockResolvedValueOnce(page([run('r7', 7), run('r6', 6)], 'k2'))
    await store.loadMoreRuns()
    // A new run started while the socket was down; the first page now leads with it.
    mocks.getDatasetRuns.mockResolvedValueOnce(page([run('r10', 10), run('r9', 9)], 'k1b'))
    await store.reload()
    expect(ids(store.runs)).toEqual(['r10', 'r9', 'r8', 'r7', 'r6'])
    expect(store.runsCursor).toBe('k2')
    expect(store.status).toBe('ready')
  })

  it('reload after a failed first load starts over', async () => {
    mocks.getDataset.mockRejectedValueOnce(new ApiError({ status: 500 }))
    const store = useDatasetDetailStore()
    await store.load('ds-1')
    expect(store.status).toBe('error')
    await store.reload()
    expect(store.status).toBe('ready')
  })

  it('reset forgets everything; a refresh after it does nothing', async () => {
    const store = useDatasetDetailStore()
    await store.load('ds-1')
    store.reset()
    expect(store.status).toBe('idle')
    expect(store.runs).toEqual([])
    await store.refresh()
    await store.reload()
    await flushPromises()
    expect(mocks.getDataset).toHaveBeenCalledTimes(1)
  })
})

// tj-grna9p.89: the Usage section's data under the Read By choice.
describe('usage (Read By and reads per day)', () => {
  it('load asks for this dataset reads over 30 days under the default SDK choice', async () => {
    const store = useDatasetDetailStore()
    await store.load('ds-1')
    expect(READS_WINDOW_DAYS).toBe(30)
    expect(store.clientKinds).toEqual([ClientKind.SDK])
    expect(mocks.getReadsPerDay).toHaveBeenCalledWith({
      days: 30,
      datasetId: 'ds-1',
      clientKinds: [ClientKind.SDK],
      signal: expect.any(AbortSignal),
    })
    expect(store.reads).toBe(READS)
    expect(store.readsError).toBeNull()
  })

  it('setClientKinds refetches the dataset softly and the reads, both under the new choice', async () => {
    const store = useDatasetDetailStore()
    await store.load('ds-1')
    vi.clearAllMocks()
    mocks.getDataset.mockResolvedValue(DATASET)
    mocks.getReadsPerDay.mockResolvedValue(READS)
    const both = [ClientKind.SDK, ClientKind.UI]
    const pending = store.setClientKinds(both)
    expect(store.status).toBe('ready')
    await pending
    expect(mocks.getDataset).toHaveBeenCalledWith('ds-1', expect.any(AbortSignal), { clientKinds: both })
    expect(mocks.getReadsPerDay).toHaveBeenCalledWith(expect.objectContaining({ datasetId: 'ds-1', clientKinds: both }))
    expect(mocks.getDatasetRuns).not.toHaveBeenCalled()
  })

  it('setClientKinds before the dataset is ready only records the choice; the load then uses it', async () => {
    const store = useDatasetDetailStore()
    await store.setClientKinds([ClientKind.UI])
    expect(mocks.getDataset).not.toHaveBeenCalled()
    await store.load('ds-1')
    expect(mocks.getDataset).toHaveBeenCalledWith('ds-1', expect.any(AbortSignal), { clientKinds: [ClientKind.UI] })
  })

  it('a reads failure is its own notice and keeps the chart already held', async () => {
    const store = useDatasetDetailStore()
    await store.load('ds-1')
    mocks.getReadsPerDay.mockRejectedValueOnce(new ApiError({ status: 500, errorId: 'rd-1' }))
    await store.setClientKinds([ClientKind.UI])
    expect(store.readsError).toEqual({ message: 'Request failed (HTTP 500)', errorId: 'rd-1' })
    expect(store.reads).toBe(READS)
    expect(store.status).toBe('ready')
  })

  it('reload (a reconnect) asks for the reads again too', async () => {
    const store = useDatasetDetailStore()
    await store.load('ds-1')
    await store.reload()
    expect(mocks.getReadsPerDay).toHaveBeenCalledTimes(2)
  })

  it('reset puts Read By back to the default and drops the reads', async () => {
    const store = useDatasetDetailStore()
    await store.load('ds-1')
    await store.setClientKinds([ClientKind.UI])
    store.reset()
    expect(store.clientKinds).toEqual([ClientKind.SDK])
    expect(store.reads).toBeNull()
    expect(store.writing).toBe(false)
  })
})

// tj-grna9p.89: the Retention writes. The override goes only with another owner's dataset; every
// refusal leaves the dataset as it was and comes back as a WriteOutcome the card words.
describe('setExpiry and deleteNow', () => {
  async function loaded(dataset = DATASET) {
    mocks.getDataset.mockResolvedValue(dataset)
    const store = useDatasetDetailStore()
    await store.load('ds-1')
    return store
  }

  it('setExpiry sends the day and re-renders from the dataset the 200 answers', async () => {
    const store = await loaded()
    const at = new Date(T0)
    const outcome = await store.setExpiry(at)
    expect(outcome).toEqual({ kind: 'ok' })
    expect(mocks.setDatasetExpiry).toHaveBeenCalledWith('ds-1', { expiry: at, override: undefined })
    expect(store.dataset).toBe(UPDATED)
  })

  it('Tomorrow then Clear round-trips: a set with the instant, then a clear with no expiry', async () => {
    const store = await loaded()
    const tomorrow = new Date(T0 + 86_400_000)
    await store.setExpiry(tomorrow)
    expect(store.dataset).toBe(UPDATED)
    mocks.setDatasetExpiry.mockResolvedValueOnce(DATASET)
    await store.setExpiry(undefined)
    expect(mocks.setDatasetExpiry.mock.calls.map((call) => call[1].expiry)).toEqual([tomorrow, undefined])
    expect(store.dataset).toBe(DATASET)
    expect(store.dataset?.expiry).toBeUndefined()
  })

  it('NEVER sends an override for the operator own dataset, even when one is handed in', async () => {
    const store = await loaded()
    await store.setExpiry(undefined, OVERRIDE)
    await store.deleteNow(OVERRIDE)
    expect(mocks.setDatasetExpiry.mock.calls[0]![1].override).toBeUndefined()
    expect(mocks.deleteDataset).toHaveBeenCalledWith('ds-1', { override: undefined })
  })

  it('sends the override for another owner dataset', async () => {
    const store = await loaded(HELD)
    mocks.setDatasetExpiry.mockResolvedValueOnce(HELD)
    await store.setExpiry(undefined, OVERRIDE)
    await store.deleteNow(OVERRIDE)
    expect(mocks.setDatasetExpiry.mock.calls[0]![1].override).toEqual(OVERRIDE)
    expect(mocks.deleteDataset).toHaveBeenCalledWith('ds-1', { override: OVERRIDE })
  })

  it('deleteNow answers ok on 204 and leaves the leaving to the caller', async () => {
    const store = await loaded()
    expect(await store.deleteNow()).toEqual({ kind: 'ok' })
    expect(store.status).toBe('ready')
  })

  const refusals: [string, unknown, WriteOutcome['kind']][] = [
    ['404', new ApiError({ status: 404, reason: 'NOT_FOUND' }), 'not-found'],
    ['409 OVERRIDE_REQUIRED', new ApiError({ status: 409, reason: 'OVERRIDE_REQUIRED' }), 'override-required'],
    ['409 with another reason', new ApiError({ status: 409, reason: 'CONFLICT' }), 'error'],
    ['422', new ApiError({ status: 422, reason: 'INVALID_REQUEST', errorId: 'e-422' }), 'invalid'],
    ['503 DATABASE_UNAVAILABLE', new ApiError({ status: 503, reason: 'DATABASE_UNAVAILABLE', errorId: 'e-503' }), 'unavailable'],
    ['503 with no reason', new ApiError({ status: 503 }), 'error'],
    ['a network failure', new ApiError({ status: 0 }), 'error'],
    ['a non-API error', new Error('boom'), 'error'],
  ]

  it.each(refusals)('a refusal (%s) is its outcome, and the dataset stays as it was', async (_name, error, kind) => {
    const store = await loaded()
    mocks.setDatasetExpiry.mockRejectedValueOnce(error)
    mocks.deleteDataset.mockRejectedValueOnce(error)
    const patch = await store.setExpiry(new Date(T0))
    const del = await store.deleteNow()
    expect(patch.kind).toBe(kind)
    expect(del.kind).toBe(kind)
    expect(store.dataset).toBe(DATASET)
    expect(store.deleted).toBe(false)
    expect(store.writing).toBe(false)
  })

  it('a 503 and a 422 carry the error id to quote', async () => {
    const store = await loaded()
    mocks.setDatasetExpiry.mockRejectedValueOnce(new ApiError({ status: 503, reason: 'DATABASE_UNAVAILABLE', errorId: 'e-503' }))
    const outcome = await store.setExpiry(undefined)
    expect(outcome).toEqual({ kind: 'unavailable', error: { message: 'Request failed (HTTP 503)', errorId: 'e-503' } })
  })

  it('one write at a time: writing is set while it is in flight and a second write is refused unsent', async () => {
    const store = await loaded()
    let answer: (value: unknown) => void = () => undefined
    mocks.setDatasetExpiry.mockImplementationOnce(() => new Promise((resolve) => (answer = resolve)))
    const first = store.setExpiry(undefined)
    expect(store.writing).toBe(true)
    expect((await store.deleteNow()).kind).toBe('error')
    expect((await store.setExpiry(undefined)).kind).toBe('error')
    expect(mocks.deleteDataset).not.toHaveBeenCalled()
    expect(mocks.setDatasetExpiry).toHaveBeenCalledTimes(1)
    answer(UPDATED)
    expect(await first).toEqual({ kind: 'ok' })
    expect(store.writing).toBe(false)
  })

  // The PATCH answer's usage is counted for the default filter (ui_dataset_writes.py builds it with
  // no client filter). Under another Read By choice the held usage stays; everything else (the
  // expiry, reaper_pending, state) comes from the answer.
  describe('the 200 answer under the Read By choice', () => {
    const HELD_USAGE = { lastReadAt: timestampFromMs(T0 - 3 * 86_400_000), readByClients30d: ['operator'] }
    const ANSWER = create(DatasetSummarySchema, {
      id: 'ds-1',
      assetSymbol: 'VFV',
      owner: 'operator',
      expiry: timestampFromMs(T0 + 86_400_000),
      reaperPending: true,
      usage: { readByClients30d: ['bot-sdk'] },
    })

    it.each([
      ['UI', [ClientKind.UI]],
      ['SDK and UI', [ClientKind.SDK, ClientKind.UI]],
    ])('under %s: the held usage object stays; expiry and reaperPending come from the answer', async (_name, kinds) => {
      mocks.getDataset.mockResolvedValue(create(DatasetSummarySchema, { id: 'ds-1', assetSymbol: 'VFV', owner: 'operator', usage: HELD_USAGE }))
      const store = useDatasetDetailStore()
      await store.load('ds-1')
      await store.setClientKinds(kinds)
      const heldUsage = store.dataset!.usage
      mocks.setDatasetExpiry.mockResolvedValueOnce(ANSWER)
      const calls = mocks.getDataset.mock.calls.length
      expect(await store.setExpiry(new Date(T0))).toEqual({ kind: 'ok' })
      expect(store.dataset!.usage).toBe(heldUsage)
      expect(store.dataset!.usage?.readByClients30d).toEqual(['operator'])
      expect(store.dataset!.expiry).toEqual(ANSWER.expiry)
      expect(store.dataset!.reaperPending).toBe(true)
      // No extra request: the held usage is already the one for this choice.
      expect(mocks.getDataset.mock.calls.length).toBe(calls)
      expect(ANSWER.usage?.readByClients30d).toEqual(['bot-sdk'])
    })

    it('under the default SDK: the answer is taken as it is, usage included', async () => {
      mocks.getDataset.mockResolvedValue(create(DatasetSummarySchema, { id: 'ds-1', owner: 'operator', usage: HELD_USAGE }))
      const store = useDatasetDetailStore()
      await store.load('ds-1')
      mocks.setDatasetExpiry.mockResolvedValueOnce(ANSWER)
      await store.setExpiry(undefined)
      expect(store.dataset).toBe(ANSWER)
    })
  })

  // A write answer for a dataset the page has left is dropped (the stale outcome).
  describe('late answers', () => {
    function hold(mock: typeof mocks.setDatasetExpiry) {
      const handle: { resolve: (value: unknown) => void; reject: (error: unknown) => void } = {
        resolve: () => undefined,
        reject: () => undefined,
      }
      mock.mockImplementationOnce(() => new Promise((resolve, reject) => Object.assign(handle, { resolve, reject })))
      return handle
    }
    const DS2 = create(DatasetSummarySchema, { id: 'ds-2', assetSymbol: 'XIU', owner: 'operator' })

    it('a Set Expiry answer for ds-1 arriving after load(ds-2) is dropped: ds-2 stays', async () => {
      const store = await loaded()
      const answer = hold(mocks.setDatasetExpiry)
      const pending = store.setExpiry(undefined)
      mocks.getDataset.mockResolvedValue(DS2)
      await store.load('ds-2')
      answer.resolve(UPDATED)
      expect(await pending).toEqual({ kind: 'stale' })
      expect(store.dataset).toBe(DS2)
    })

    it('a refusal for the left dataset is stale too, not a 404 for the new one', async () => {
      const store = await loaded()
      const answer = hold(mocks.setDatasetExpiry)
      const pending = store.setExpiry(undefined)
      mocks.getDataset.mockResolvedValue(DS2)
      await store.load('ds-2')
      answer.reject(new ApiError({ status: 404, reason: 'NOT_FOUND' }))
      expect(await pending).toEqual({ kind: 'stale' })
      expect(store.deleted).toBe(false)
    })

    it('a late Delete Now success is stale (the caller does not leave)', async () => {
      const store = await loaded()
      const answer = hold(mocks.deleteDataset)
      const pending = store.deleteNow()
      mocks.getDataset.mockResolvedValue(DS2)
      await store.load('ds-2')
      answer.resolve(undefined)
      expect(await pending).toEqual({ kind: 'stale' })
    })

    it('a reload of the SAME id (Try Again) also drops the earlier answer', async () => {
      const store = await loaded()
      const answer = hold(mocks.setDatasetExpiry)
      const pending = store.setExpiry(undefined)
      await store.load('ds-1')
      answer.resolve(UPDATED)
      expect(await pending).toEqual({ kind: 'stale' })
      expect(store.dataset).toBe(DATASET)
    })

    it('reset while a write is in flight drops it', async () => {
      const store = await loaded()
      const answer = hold(mocks.setDatasetExpiry)
      const pending = store.setExpiry(undefined)
      store.reset()
      answer.resolve(UPDATED)
      expect(await pending).toEqual({ kind: 'stale' })
      expect(store.dataset).toBeNull()
    })
  })

  it('nothing loaded: a write is refused and nothing is sent', async () => {
    const store = useDatasetDetailStore()
    expect((await store.setExpiry(undefined)).kind).toBe('error')
    expect((await store.deleteNow()).kind).toBe('error')
    expect(mocks.setDatasetExpiry).not.toHaveBeenCalled()
    expect(mocks.deleteDataset).not.toHaveBeenCalled()
  })
})
