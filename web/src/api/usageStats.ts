// Typed functions for the Usage tab's /ui/v1/stats routes (server/routers/data_store/ui_stats.py).
// Reads per day is getReadsPerDay in datasets.ts (the Dataset detail uses it too).
//
// What each route takes is the server's, and presence matters: a client selection is always sent
// (client_id when clients are picked, else the Read By kinds), `stale_days` is a threshold, `source` is
// a filter and goes out only when the operator chose one. Storage takes no client parameters (a 422).
import { DataSource } from '@generated/trader_joe/proto/market/v1/enums_pb'
import {
  LastReadBucketsSchema,
  StaleDatasetPageSchema,
  StorageCurrentSchema,
  UsageSummarySchema,
} from '@generated/trader_joe/proto/ui/v1/stats_pb'
import type {
  LastReadBuckets,
  StaleDatasetPage,
  StorageCurrent,
  UsageSummary,
} from '@generated/trader_joe/proto/ui/v1/stats_pb'

import { getMessage } from './client'
import { clientQuery, enumName } from './datasets'
import type { ClientSelection } from './datasets'

export interface UsageStatsParams extends ClientSelection {
  /** The threshold of the stale figures, in days (the summary and the stale list). */
  staleDays?: number
  /** Only this source's datasets: a filter. */
  source?: DataSource
  signal?: AbortSignal
}

export interface GetStaleParams extends UsageStatsParams {
  /** The previous page's nextCursor. */
  cursor?: string
  /** Server default 100, at most 500. */
  limit?: number
}

/** GET /ui/v1/stats/usage/summary: the Usage tiles' counts under the client selection. */
export function getUsageSummary(params: UsageStatsParams = {}): Promise<UsageSummary> {
  return getMessage(UsageSummarySchema, '/ui/v1/stats/usage/summary', {
    query: {
      stale_days: params.staleDays,
      source: enumName(DataSource, params.source),
      ...clientQuery(params),
    },
    signal: params.signal,
  })
}

/** GET /ui/v1/stats/usage/last-read-buckets: datasets per whole-UTC-day bucket since their last read, and the never read. */
export function getLastReadBuckets(params: UsageStatsParams = {}): Promise<LastReadBuckets> {
  return getMessage(LastReadBucketsSchema, '/ui/v1/stats/usage/last-read-buckets', {
    query: { source: enumName(DataSource, params.source), ...clientQuery(params) },
    signal: params.signal,
  })
}

/** GET /ui/v1/stats/usage/stale: one page of the stale datasets, keyset paged. */
export function getStaleDatasets(params: GetStaleParams = {}): Promise<StaleDatasetPage> {
  return getMessage(StaleDatasetPageSchema, '/ui/v1/stats/usage/stale', {
    query: {
      stale_days: params.staleDays,
      source: enumName(DataSource, params.source),
      cursor: params.cursor,
      limit: params.limit,
      ...clientQuery(params),
    },
    signal: params.signal,
  })
}

/** GET /ui/v1/stats/storage/current: the estimated bytes held now, by source and feed. No client parameters. */
export function getStorageCurrent(params: { source?: DataSource; signal?: AbortSignal } = {}): Promise<StorageCurrent> {
  return getMessage(StorageCurrentSchema, '/ui/v1/stats/storage/current', {
    query: { source: enumName(DataSource, params.source) },
    signal: params.signal,
  })
}
