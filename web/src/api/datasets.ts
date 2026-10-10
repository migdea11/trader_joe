// Typed functions for the phase 1 /ui/v1 routes (server/routers/data_store/ui_datasets.py and
// ui_config.py). Parameter names and defaults are the server's; an omitted parameter takes its default.

import { DataSource, UpdateType } from '@generated/trader_joe/proto/market/v1/enums_pb'
import {
  BarPageSchema,
  DatasetFacetsSchema,
  DatasetPageSchema,
  DatasetSummarySchema,
} from '@generated/trader_joe/proto/ui/v1/data_pb'
import type {
  BarPage,
  DatasetFacets,
  DatasetPage,
  DatasetSummary,
} from '@generated/trader_joe/proto/ui/v1/data_pb'
import { UiConfigSchema } from '@generated/trader_joe/proto/ui/v1/shell_pb'
import type { UiConfig } from '@generated/trader_joe/proto/ui/v1/shell_pb'

import { API_BASE, getMessage } from './client'
import { buildQuery } from './query'

/** The status words the server filters by (a grouping of FreshnessStatus). */
export type StatusGroup = 'healthy' | 'late' | 'failed' | 'retired'
export type DatasetSort = 'symbol' | 'expires'

/** The filters the list and the facets share. */
export interface DatasetFilters {
  /** Case-insensitive symbol prefix. */
  assetSymbol?: string
  source?: DataSource
  updateType?: UpdateType
  status?: StatusGroup
  /** Only late and failed datasets. */
  needsAttention?: boolean
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
  /** Server default 1000, at most 10000. */
  limit?: number
  signal?: AbortSignal
}

// The server reads an enum filter from the member name, with or without the wire prefix.
function enumName(names: Record<number, string>, value: number | undefined): string | undefined {
  return value === undefined ? undefined : names[value]
}

function filterQuery(filters: DatasetFilters) {
  return {
    asset_symbol: filters.assetSymbol,
    source: enumName(DataSource, filters.source),
    update_type: enumName(UpdateType, filters.updateType),
    status: filters.status,
    needs_attention: filters.needsAttention,
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

/** GET /ui/v1/datasets/{id}: one dataset with its siblings and freshness. */
export function getDataset(id: string, signal?: AbortSignal): Promise<DatasetSummary> {
  return getMessage(DatasetSummarySchema, `/ui/v1/datasets/${encodeURIComponent(id)}`, { signal })
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

/** GET /ui/v1/config: what the shell needs to know about this deployment. */
export function getUiConfig(signal?: AbortSignal): Promise<UiConfig> {
  return getMessage(UiConfigSchema, '/ui/v1/config', { signal })
}
