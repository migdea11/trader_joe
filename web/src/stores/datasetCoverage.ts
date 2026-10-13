// Coverage strips for the rows on screen (tj-grna9p.77), fetched lazily and one per dataset.
//
// A row's Coverage cell `acquire`s its strip when it mounts and `release`s it when it unmounts; AG
// Grid only mounts the cells of rows in (or just beside) the viewport, so "per visible row" falls
// out of that: a row that scrolls away aborts its request if still in flight, and the strip stays
// cached for when it scrolls back. Several cells holding the same strip share one request.
//
// Requests go out at most MAX_CONCURRENT at a time: a screenful of rows is a screenful of calls and
// the store computes each over the calendar. An event (DatasetChanged, IngestRunChanged) or a
// Refresh drops the cached strip, and a strip still on screen is fetched again.
import { shallowReactive } from 'vue'
import { defineStore } from 'pinia'

import type { CoverageStrip } from '@generated/trader_joe/proto/ui/v1/stats_pb'

import { ApiError, getDatasetCoverage } from '@/api'

/** What a cell shows: the strip, "no coverage" (no trading calendar), or "unavailable" (a failure). */
export type CoverageEntry =
  | { state: 'ready'; strip: CoverageStrip }
  | { state: 'none' }
  | { state: 'unavailable' }

/** Concurrent coverage requests. */
export const MAX_CONCURRENT = 4

interface Want {
  id: string
  buckets: number
  holders: number
  controller: AbortController | null
  queued: boolean
}

function keyOf(id: string, buckets: number): string {
  return `${id}|${buckets}`
}

function toEntry(error: unknown): CoverageEntry {
  // 422: no trading calendar covers the dataset. That is a fact about the dataset, not a failure.
  if (error instanceof ApiError && error.status === 422) return { state: 'none' }
  return { state: 'unavailable' }
}

export const useDatasetCoverageStore = defineStore('datasetCoverage', () => {
  // Shallow: the protobuf messages inside are not made deeply reactive.
  const entries = shallowReactive(new Map<string, CoverageEntry>())
  const wanted = new Map<string, Want>()
  const queue: string[] = []
  let running = 0

  /** The cached outcome for a strip, or undefined while it is not fetched or still loading. */
  function entry(id: string, buckets: number): CoverageEntry | undefined {
    return entries.get(keyOf(id, buckets))
  }

  async function start(key: string, want: Want): Promise<void> {
    const controller = new AbortController()
    want.controller = controller
    want.queued = false
    running += 1
    try {
      const strip = await getDatasetCoverage(want.id, want.buckets, controller.signal)
      if (want.controller === controller) entries.set(key, { state: 'ready', strip })
    } catch (error) {
      // An aborted request (row left view, or its strip was invalidated) records nothing.
      if (want.controller === controller && !controller.signal.aborted) entries.set(key, toEntry(error))
    } finally {
      running -= 1
      if (want.controller === controller) want.controller = null
      pump()
    }
  }

  function pump(): void {
    while (running < MAX_CONCURRENT && queue.length > 0) {
      const key = queue.shift() as string
      const want = wanted.get(key)
      if (want === undefined || want.controller !== null || entries.has(key)) {
        if (want !== undefined) want.queued = false
        continue
      }
      void start(key, want)
    }
  }

  function enqueue(key: string, want: Want): void {
    if (want.queued || want.controller !== null || entries.has(key)) return
    want.queued = true
    queue.push(key)
  }

  /** A cell for this strip is on screen: fetch it unless it is cached or already on its way. */
  function acquire(id: string, buckets: number): void {
    const key = keyOf(id, buckets)
    let want = wanted.get(key)
    if (want === undefined) {
      want = { id, buckets, holders: 0, controller: null, queued: false }
      wanted.set(key, want)
    }
    want.holders += 1
    enqueue(key, want)
    pump()
  }

  /** A cell went away. The last one out aborts a request still in flight; the cache stays. */
  function release(id: string, buckets: number): void {
    const key = keyOf(id, buckets)
    const want = wanted.get(key)
    if (want === undefined) return
    want.holders -= 1
    if (want.holders > 0) return
    wanted.delete(key)
    const at = queue.indexOf(key)
    if (at >= 0) queue.splice(at, 1)
    want.controller?.abort()
  }

  function refetch(key: string): void {
    entries.delete(key)
    const want = wanted.get(key)
    if (want === undefined) return
    // A request still in flight answers a question asked before the change: drop it and ask again.
    want.controller?.abort()
    want.controller = null
    enqueue(key, want)
  }

  /** The dataset changed (an event): drop its strips, and fetch again the ones still on screen. */
  function invalidate(id: string): void {
    const keys = new Set([...entries.keys(), ...wanted.keys()].filter((key) => key.startsWith(`${id}|`)))
    keys.forEach(refetch)
    pump()
  }

  /** Everything may have changed (Refresh, a reconnect): the same, for every strip. */
  function invalidateAll(): void {
    const keys = new Set([...entries.keys(), ...wanted.keys()])
    keys.forEach(refetch)
    pump()
  }

  return { entry, acquire, release, invalidate, invalidateAll }
})
