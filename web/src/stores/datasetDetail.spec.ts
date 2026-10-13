import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { create } from '@bufbuild/protobuf'
import type { MessageInitShape } from '@bufbuild/protobuf'
import { timestampFromMs } from '@bufbuild/protobuf/wkt'

import {
  DatasetSummarySchema,
  IngestRunPageSchema,
  IngestRunSchema,
  IngestRunState,
} from '@generated/trader_joe/proto/ui/v1/data_pb'
import type { IngestRun } from '@generated/trader_joe/proto/ui/v1/data_pb'

import { ApiError } from '@/api'
import { RUNS_PAGE_SIZE, mergeRuns, useDatasetDetailStore } from './datasetDetail'

// tj-grna9p.34: the Dataset detail store. The API is mocked at the typed client (@/api); the store is
// the real one. Fixtures are create(Schema) from the generated ui/v1 messages.
const mocks = vi.hoisted(() => ({ getDataset: vi.fn(), getDatasetRuns: vi.fn() }))
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
const page = (items: IngestRun[], nextCursor = '') => create(IngestRunPageSchema, { items, nextCursor })
const ids = (runs: readonly IngestRun[]) => runs.map((r) => r.id)

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  mocks.getDataset.mockResolvedValue(DATASET)
  mocks.getDatasetRuns.mockResolvedValue(page([run('r2', 2), run('r1', 1)]))
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
    expect(mocks.getDataset).toHaveBeenCalledWith('ds-1', expect.any(AbortSignal))
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
