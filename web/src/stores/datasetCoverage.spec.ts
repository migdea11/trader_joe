import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { create } from '@bufbuild/protobuf'

import { CoverageStripSchema } from '@generated/trader_joe/proto/ui/v1/stats_pb'
import type { CoverageStrip } from '@generated/trader_joe/proto/ui/v1/stats_pb'

import { ApiError } from '@/api'
import { DETAIL_COVERAGE_BUCKETS } from '@/views/data/useDatasetDetailLive'
import { MAX_CONCURRENT, useDatasetCoverageStore } from './datasetCoverage'

// tj-grna9p.77: the coverage strips for the rows on screen. The API is mocked at the typed client
// (@/api); every call is held open until the spec answers it, so the queue, the concurrency limit and
// the aborts are observable. The store itself is the real one.
interface Call {
  id: string
  buckets: number
  signal: AbortSignal
  resolve: (strip: CoverageStrip) => void
  reject: (error: unknown) => void
}

const calls: Call[] = []
const getDatasetCoverage = vi.hoisted(() => vi.fn())
vi.mock('@/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api')>()),
  getDatasetCoverage,
}))

const STRIP = create(CoverageStripSchema, { gapCount: 2 })

function callsFor(id: string): Call[] {
  return calls.filter((c) => c.id === id)
}

beforeEach(() => {
  setActivePinia(createPinia())
  calls.length = 0
  getDatasetCoverage.mockReset()
  getDatasetCoverage.mockImplementation(
    (id: string, buckets: number, signal: AbortSignal) =>
      new Promise<CoverageStrip>((resolve, reject) => {
        calls.push({ id, buckets, signal, resolve, reject })
      }),
  )
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('fetching', () => {
  it('a cell on screen asks for its strip once, with the bucket count and an abort signal', async () => {
    const store = useDatasetCoverageStore()
    store.acquire('a', 60)
    expect(getDatasetCoverage).toHaveBeenCalledTimes(1)
    expect(calls[0]).toMatchObject({ id: 'a', buckets: 60 })
    expect(calls[0]!.signal).toBeInstanceOf(AbortSignal)
    expect(store.entry('a', 60)).toBeUndefined()
    calls[0]!.resolve(STRIP)
    await flushPromises()
    expect(store.entry('a', 60)).toEqual({ state: 'ready', strip: STRIP })
  })

  it('two cells holding the same strip share one request', () => {
    const store = useDatasetCoverageStore()
    store.acquire('a', 60)
    store.acquire('a', 60)
    expect(getDatasetCoverage).toHaveBeenCalledTimes(1)
  })

  // The grid cell asks for 60 (GRID_BUCKETS in CoverageCell.vue, not exported) and the Dataset detail
  // for DETAIL_COVERAGE_BUCKETS (20 since tj-grna9p.115). The key is id|buckets, so one dataset on
  // both screens holds two strips: the detail never draws the grid's 60 blocks, nor the grid 20.
  it('a different bucket count is a different strip: grid 60 and detail are two entries for one dataset', async () => {
    expect(DETAIL_COVERAGE_BUCKETS).not.toBe(60)
    const store = useDatasetCoverageStore()
    store.acquire('a', 60)
    store.acquire('a', DETAIL_COVERAGE_BUCKETS)
    expect(calls.map((c) => c.buckets)).toEqual([60, DETAIL_COVERAGE_BUCKETS])
    const grid = create(CoverageStripSchema, { gapCount: 60 })
    const detail = create(CoverageStripSchema, { gapCount: DETAIL_COVERAGE_BUCKETS })
    calls[0]!.resolve(grid)
    calls[1]!.resolve(detail)
    await flushPromises()
    expect(store.entry('a', 60)).toEqual({ state: 'ready', strip: grid })
    expect(store.entry('a', DETAIL_COVERAGE_BUCKETS)).toEqual({ state: 'ready', strip: detail })
    expect(store.entry('a', 60)).not.toEqual(store.entry('a', DETAIL_COVERAGE_BUCKETS))
  })

  it('a strip already fetched is served from the cache when its row scrolls back', async () => {
    const store = useDatasetCoverageStore()
    store.acquire('a', 60)
    calls[0]!.resolve(STRIP)
    await flushPromises()
    store.release('a', 60)
    store.acquire('a', 60)
    expect(getDatasetCoverage).toHaveBeenCalledTimes(1)
    expect(store.entry('a', 60)?.state).toBe('ready')
  })

  it('a 422 (no trading calendar) is "none", a neutral fact, not a failure', async () => {
    const store = useDatasetCoverageStore()
    store.acquire('a', 60)
    calls[0]!.reject(new ApiError({ status: 422, reason: 'NO_CALENDAR' }))
    await flushPromises()
    expect(store.entry('a', 60)).toEqual({ state: 'none' })
  })

  it.each([
    ['a 500', new ApiError({ status: 500 })],
    ['a 404', new ApiError({ status: 404 })],
    ['a network failure', new ApiError({ status: 0 })],
    ['a non-API error', new TypeError('x')],
  ])('%s is "unavailable"', async (_name, error) => {
    const store = useDatasetCoverageStore()
    store.acquire('a', 60)
    calls[0]!.reject(error)
    await flushPromises()
    expect(store.entry('a', 60)).toEqual({ state: 'unavailable' })
  })
})

describe('concurrency', () => {
  it('at most 4 requests are in flight; each answer lets the next queued one go', async () => {
    expect(MAX_CONCURRENT).toBe(4)
    const store = useDatasetCoverageStore()
    for (const id of ['a', 'b', 'c', 'd', 'e', 'f']) store.acquire(id, 60)
    expect(calls.map((c) => c.id)).toEqual(['a', 'b', 'c', 'd'])
    calls[1]!.resolve(STRIP)
    await flushPromises()
    expect(calls.map((c) => c.id)).toEqual(['a', 'b', 'c', 'd', 'e'])
    calls[0]!.reject(new ApiError({ status: 500 }))
    await flushPromises()
    expect(calls.map((c) => c.id)).toEqual(['a', 'b', 'c', 'd', 'e', 'f'])
    expect(calls.filter((c) => !c.signal.aborted)).toHaveLength(6)
  })

  it('a queued strip whose row leaves before its turn is never asked for', async () => {
    const store = useDatasetCoverageStore()
    for (const id of ['a', 'b', 'c', 'd', 'e', 'f']) store.acquire(id, 60)
    store.release('e', 60)
    calls[0]!.resolve(STRIP)
    calls[1]!.resolve(STRIP)
    await flushPromises()
    expect(calls.map((c) => c.id)).toEqual(['a', 'b', 'c', 'd', 'f'])
  })
})

describe('release', () => {
  it('the last holder leaving aborts a request still in flight, and nothing is recorded', async () => {
    const store = useDatasetCoverageStore()
    store.acquire('a', 60)
    store.acquire('a', 60)
    store.release('a', 60)
    expect(calls[0]!.signal.aborted).toBe(false)
    store.release('a', 60)
    expect(calls[0]!.signal.aborted).toBe(true)
    calls[0]!.reject(new DOMException('aborted', 'AbortError'))
    await flushPromises()
    expect(store.entry('a', 60)).toBeUndefined()
  })

  it('an aborted request frees its slot for the next queued strip', async () => {
    const store = useDatasetCoverageStore()
    for (const id of ['a', 'b', 'c', 'd', 'e']) store.acquire(id, 60)
    store.release('a', 60)
    calls[0]!.reject(new DOMException('aborted', 'AbortError'))
    await flushPromises()
    expect(calls.map((c) => c.id)).toEqual(['a', 'b', 'c', 'd', 'e'])
  })

  it('scrolling back after an abort asks again', async () => {
    const store = useDatasetCoverageStore()
    store.acquire('a', 60)
    store.release('a', 60)
    calls[0]!.reject(new DOMException('aborted', 'AbortError'))
    await flushPromises()
    store.acquire('a', 60)
    expect(callsFor('a')).toHaveLength(2)
  })

  it('releasing a strip nobody holds is a no-op', () => {
    const store = useDatasetCoverageStore()
    expect(() => store.release('nobody', 60)).not.toThrow()
  })
})

describe('invalidate', () => {
  it('drops the dataset strips and fetches again the ones still on screen', async () => {
    const store = useDatasetCoverageStore()
    // The grid's strip and the detail's, for one dataset: an event for it drops both.
    store.acquire('a', 60)
    store.acquire('a', DETAIL_COVERAGE_BUCKETS)
    calls.forEach((c) => c.resolve(STRIP))
    await flushPromises()
    store.invalidate('a')
    expect(store.entry('a', 60)).toBeUndefined()
    expect(store.entry('a', DETAIL_COVERAGE_BUCKETS)).toBeUndefined()
    expect(callsFor('a').map((c) => c.buckets)).toEqual([60, DETAIL_COVERAGE_BUCKETS, 60, DETAIL_COVERAGE_BUCKETS])
  })

  it('a strip not on screen is dropped from the cache and fetched only when it comes back', async () => {
    const store = useDatasetCoverageStore()
    store.acquire('a', 60)
    calls[0]!.resolve(STRIP)
    await flushPromises()
    store.release('a', 60)
    store.invalidate('a')
    expect(getDatasetCoverage).toHaveBeenCalledTimes(1)
    store.acquire('a', 60)
    expect(getDatasetCoverage).toHaveBeenCalledTimes(2)
  })

  it('an in-flight request answers a question asked before the change: aborted and asked again', async () => {
    const store = useDatasetCoverageStore()
    store.acquire('a', 60)
    const first = calls[0]!
    store.invalidate('a')
    expect(first.signal.aborted).toBe(true)
    expect(callsFor('a')).toHaveLength(2)
    first.resolve(create(CoverageStripSchema, { gapCount: 99 }))
    calls[1]!.resolve(STRIP)
    await flushPromises()
    expect(store.entry('a', 60)).toEqual({ state: 'ready', strip: STRIP })
  })

  it('touches only that dataset: an id that merely starts the same is kept', async () => {
    const store = useDatasetCoverageStore()
    store.acquire('a', 60)
    store.acquire('ab', 60)
    calls.forEach((c) => c.resolve(STRIP))
    await flushPromises()
    store.invalidate('a')
    expect(store.entry('ab', 60)?.state).toBe('ready')
    expect(callsFor('ab')).toHaveLength(1)
  })

  it('invalidateAll drops and refetches every strip on screen', async () => {
    const store = useDatasetCoverageStore()
    store.acquire('a', 60)
    store.acquire('b', 60)
    calls.forEach((c) => c.resolve(STRIP))
    await flushPromises()
    store.invalidateAll()
    expect(store.entry('a', 60)).toBeUndefined()
    expect(store.entry('b', 60)).toBeUndefined()
    expect(calls.map((c) => c.id)).toEqual(['a', 'b', 'a', 'b'])
  })

  it('invalidateAll of more strips than the limit still keeps 4 in flight', async () => {
    const store = useDatasetCoverageStore()
    for (const id of ['a', 'b', 'c', 'd', 'e', 'f']) store.acquire(id, 60)
    for (let i = 0; i < 6; i += 1) {
      calls[i]?.resolve(STRIP)
      await flushPromises()
    }
    calls.length = 0
    store.invalidateAll()
    expect(calls).toHaveLength(4)
  })
})
