// Typed functions for the phase 1 /ui/v1 routes (server/routers/data_store/ui_datasets.py and
// ui_config.py). Parameter names and defaults are the server's; an omitted parameter takes its default.

import { DataSource, UpdateType } from '@generated/trader_joe/proto/market/v1/enums_pb'
import {
  BarPageIndexSchema,
  ClientKind,
  UsageFilter,
  BarPageSchema,
  DatasetFacetsSchema,
  DatasetPageSchema,
  DatasetSummarySchema,
  IngestRunPageSchema,
} from '@generated/trader_joe/proto/ui/v1/data_pb'
import type {
  BarPage,
  BarPageIndex,
  DatasetFacets,
  DatasetPage,
  DatasetSummary,
  IngestRunPage,
} from '@generated/trader_joe/proto/ui/v1/data_pb'
import { UiConfigSchema } from '@generated/trader_joe/proto/ui/v1/shell_pb'
import type { UiConfig } from '@generated/trader_joe/proto/ui/v1/shell_pb'
import { CoverageStripSchema, ReadsPerDaySchema } from '@generated/trader_joe/proto/ui/v1/stats_pb'
import type { CoverageStrip, ReadsPerDay } from '@generated/trader_joe/proto/ui/v1/stats_pb'

import { API_BASE, deleteRequest, getMessage, patchMessage } from './client'
import { buildQuery } from './query'

/** The status words the server filters by (a grouping of FreshnessStatus). */
export type StatusGroup = 'healthy' | 'late' | 'failed' | 'retired'
export type DatasetSort = 'symbol' | 'expires' | 'last_read'

/** The filters the list and the facets share. */
export interface DatasetFilters {
  /** Case-insensitive symbol prefix. */
  assetSymbol?: string
  source?: DataSource
  updateType?: UpdateType
  status?: StatusGroup
  /** Only late and failed datasets. */
  needsAttention?: boolean
  /** Only datasets with a collection run in flight (orthogonal to `status`). */
  running?: boolean
  /** Whose reads the usage figures and the usage filter count; the server's default is SDK. */
  clientKinds?: readonly ClientKind[]
  /** Only datasets in this usage group under the client filter. */
  usage?: UsageFilter
  /** The threshold of the Stale group, in days. */
  staleDays?: number
  /** Only datasets expiring before this instant, plus those the reaper would delete. */
  expiresBefore?: Date
  signal?: AbortSignal
}

export interface ListDatasetsParams extends DatasetFilters {
  /** `symbol` by default. */
  sort?: DatasetSort
  /** The previous page's nextCursor. */
  cursor?: string
  /** Server default 100, at most 500. */
  limit?: number
}

export interface GetBarsParams {
  /** Include bars at or after this instant. */
  start?: Date
  /** Exclude bars at or after this instant (half-open window). */
  end?: Date
  cursor?: string
  /** Server default 1000, at most 50000. */
  limit?: number
  signal?: AbortSignal
}

// The server reads an enum filter from the member name, with or without the wire prefix.
export function enumName(names: Record<number, string>, value: number | undefined): string | undefined {
  return value === undefined ? undefined : names[value]
}

function filterQuery(filters: DatasetFilters) {
  return {
    asset_symbol: filters.assetSymbol,
    source: enumName(DataSource, filters.source),
    update_type: enumName(UpdateType, filters.updateType),
    status: filters.status,
    needs_attention: filters.needsAttention,
    running: filters.running,
    client_kind: filters.clientKinds?.map((kind) => ClientKind[kind]),
    usage: enumName(UsageFilter, filters.usage),
    stale_days: filters.staleDays,
    expires_before: filters.expiresBefore,
  }
}

function listQuery(params: ListDatasetsParams) {
  return { ...filterQuery(params), sort: params.sort, cursor: params.cursor, limit: params.limit }
}

/** GET /ui/v1/datasets: one page of the catalog. */
export function listDatasets(params: ListDatasetsParams = {}): Promise<DatasetPage> {
  return getMessage(DatasetPageSchema, '/ui/v1/datasets', {
    query: listQuery(params),
    signal: params.signal,
  })
}

/** The request listDatasets sends for these parameters, as the browser's path and query (Show Request). */
export function listDatasetsRequest(params: ListDatasetsParams = {}): string {
  return `${API_BASE}/ui/v1/datasets${buildQuery(listQuery(params))}`
}

/** GET /ui/v1/datasets/facets: the sidebar counts under the same filters. */
export function getDatasetFacets(filters: DatasetFilters = {}): Promise<DatasetFacets> {
  return getMessage(DatasetFacetsSchema, '/ui/v1/datasets/facets', {
    query: filterQuery(filters),
    signal: filters.signal,
  })
}

/**
 * Whose reads a dataset's usage figures count. A selection, never a filter on the dataset: the
 * server counts the chosen clients' reads and returns the dataset either way. Client ids win over
 * kinds, so a caller sends one or the other.
 */
export interface ClientSelection {
  clientKinds?: readonly ClientKind[]
  clientIds?: readonly string[]
}

/** The query parameters of a client selection (the usage routes and the dataset detail share them). */
export function clientQuery(client: ClientSelection = {}) {
  const ids = client.clientIds ?? []
  return {
    client_id: ids.length > 0 ? ids : undefined,
    client_kind: ids.length > 0 ? undefined : client.clientKinds?.map((kind) => ClientKind[kind]),
  }
}

/** GET /ui/v1/datasets/{id}: one dataset with its siblings, freshness and usage under `client`. */
export function getDataset(id: string, signal?: AbortSignal, client: ClientSelection = {}): Promise<DatasetSummary> {
  return getMessage(DatasetSummarySchema, `/ui/v1/datasets/${encodeURIComponent(id)}`, {
    query: clientQuery(client),
    signal,
  })
}

/** The operator's acknowledgement for acting on a dataset another owner holds. */
export interface OperatorOverride {
  reason: string
  confirmed: true
}

/**
 * PATCH /ui/v1/datasets/{id}/expiry: set the expiry, or clear it when `expiry` is undefined. Answers
 * the updated dataset. `override` is sent only for another owner's dataset.
 */
export function setDatasetExpiry(
  id: string,
  params: { expiry?: Date; override?: OperatorOverride; signal?: AbortSignal },
): Promise<DatasetSummary> {
  return patchMessage(DatasetSummarySchema, `/ui/v1/datasets/${encodeURIComponent(id)}/expiry`, {
    body: {
      ...(params.expiry === undefined ? {} : { expiry: params.expiry.toISOString() }),
      ...(params.override === undefined ? {} : { override: params.override }),
    },
    signal: params.signal,
  })
}

/** DELETE /ui/v1/datasets/{id}: delete the dataset and its bars. `override` as for the expiry write. */
export function deleteDataset(id: string, params: { override?: OperatorOverride; signal?: AbortSignal } = {}): Promise<void> {
  return deleteRequest(`/ui/v1/datasets/${encodeURIComponent(id)}`, {
    body: params.override === undefined ? undefined : { override: params.override },
    signal: params.signal,
  })
}

export interface GetBarPagesParams {
  /** Include bars at or after this instant. */
  start?: Date
  /** Exclude bars at or after this instant (half-open window). */
  end?: Date
  /** Bars per page; the server default and cap are its own (1000). */
  size?: number
  /** The look-ahead: bars of the next block a page shows (it does not move the offsets). */
  overlap?: number
  signal?: AbortSignal
}

function barPagesQuery(params: GetBarPagesParams) {
  return { start: params.start, end: params.end, size: params.size, overlap: params.overlap }
}

function barsQuery(params: GetBarsParams) {
  return { start: params.start, end: params.end, cursor: params.cursor, limit: params.limit }
}

/** GET /ui/v1/datasets/{id}/bars: one page of bars in the half-open window [start, end). */
export function getDatasetBars(id: string, params: GetBarsParams = {}): Promise<BarPage> {
  return getMessage(BarPageSchema, `/ui/v1/datasets/${encodeURIComponent(id)}/bars`, {
    query: barsQuery(params),
    signal: params.signal,
  })
}

/** The request getDatasetBars sends for these parameters, as the browser's path and query (Show Request). */
export function getDatasetBarsRequest(id: string, params: GetBarsParams = {}): string {
  return `${API_BASE}/ui/v1/datasets/${encodeURIComponent(id)}/bars${buildQuery(barsQuery(params))}`
}

/** GET /ui/v1/datasets/{id}/bar-pages: where each chart page of the half-open window [start, end) starts. */
export function getDatasetBarPages(id: string, params: GetBarPagesParams = {}): Promise<BarPageIndex> {
  return getMessage(BarPageIndexSchema, `/ui/v1/datasets/${encodeURIComponent(id)}/bar-pages`, {
    query: barPagesQuery(params),
    signal: params.signal,
  })
}

/** The request getDatasetBarPages sends for these parameters, as the browser's path and query (Show Request). */
export function getDatasetBarPagesRequest(id: string, params: GetBarPagesParams = {}): string {
  return `${API_BASE}/ui/v1/datasets/${encodeURIComponent(id)}/bar-pages${buildQuery(barPagesQuery(params))}`
}

export interface GetRunsParams {
  /** The previous page's nextCursor. */
  cursor?: string
  /** Server default 50, at most 500. */
  limit?: number
  signal?: AbortSignal
}

/** GET /ui/v1/datasets/{id}/runs: one page of the dataset's collection runs, newest first. */
export function getDatasetRuns(id: string, params: GetRunsParams = {}): Promise<IngestRunPage> {
  return getMessage(IngestRunPageSchema, `/ui/v1/datasets/${encodeURIComponent(id)}/runs`, {
    query: { cursor: params.cursor, limit: params.limit },
    signal: params.signal,
  })
}

/** GET /ui/v1/datasets/{id}/coverage: the bucketed coverage strip and the gap windows, half-open [start, end). */
export function getDatasetCoverage(id: string, buckets: number, signal?: AbortSignal): Promise<CoverageStrip> {
  return getMessage(CoverageStripSchema, `/ui/v1/datasets/${encodeURIComponent(id)}/coverage`, {
    query: { buckets },
    signal,
  })
}

export interface GetReadsPerDayParams extends ClientSelection {
  /** The window in UTC days ending today: a parameter, not a filter (server default 30, 1..365). */
  days?: number
  /** Only this dataset's reads: a filter. */
  datasetId?: string
  /** Only this source's datasets: a filter, sent only when chosen. */
  source?: DataSource
  signal?: AbortSignal
}

/** GET /ui/v1/stats/usage/reads-per-day: reads per UTC day and client, from the selected clients. */
export function getReadsPerDay(params: GetReadsPerDayParams = {}): Promise<ReadsPerDay> {
  return getMessage(ReadsPerDaySchema, '/ui/v1/stats/usage/reads-per-day', {
    query: {
      days: params.days,
      dataset_id: params.datasetId,
      source: enumName(DataSource, params.source),
      ...clientQuery(params),
    },
    signal: params.signal,
  })
}

/** GET /ui/v1/config: what the shell needs to know about this deployment. */
export function getUiConfig(signal?: AbortSignal): Promise<UiConfig> {
  return getMessage(UiConfigSchema, '/ui/v1/config', { signal })
}
