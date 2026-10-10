// The catalog's filter state and its two mappings: to the page URL (so a reload keeps the filters)
// and to the /ui/v1/datasets query (tj-grna9p.32). Pure functions; the components own the router.
//
// The server takes ONE value per filter (status, source, update_type), so each sidebar group is a
// single choice. A dataset with no health (freshness unset) matches no status value; it is only in
// the unfiltered list.
import { DataSource, UpdateType } from '@generated/trader_joe/proto/market/v1/enums_pb'
import type { LocationQuery, LocationQueryRaw } from 'vue-router'

import type { DatasetFilters, StatusGroup } from '@/api'

export type CatalogView = 'all' | 'needs-attention'

export interface CatalogFilters {
  view: CatalogView
  status?: StatusGroup
  source?: DataSource
  updateType?: UpdateType
}

/** The URL query keys this screen owns. Others (the trading group) are not touched. */
export const QUERY_KEYS = ['view', 'status', 'source', 'update'] as const

export const STATUS_GROUPS: readonly StatusGroup[] = ['healthy', 'late', 'failed', 'retired']

// Stream arrives in a later phase; it is not a selectable value, so a URL naming it is ignored.
const UPDATE_TYPES_IN_PHASE_1: readonly UpdateType[] = [UpdateType.STATIC, UpdateType.DAILY]

function first(value: LocationQuery[string] | undefined): string | undefined {
  const text = Array.isArray(value) ? value[0] : value
  return typeof text === 'string' && text !== '' ? text : undefined
}

function sourceFromName(name: string): DataSource | undefined {
  const value = DataSource[name.toUpperCase() as keyof typeof DataSource]
  return typeof value === 'number' && value !== DataSource.UNSPECIFIED ? value : undefined
}

function updateTypeFromName(name: string): UpdateType | undefined {
  const value = UpdateType[name.toUpperCase() as keyof typeof UpdateType]
  return typeof value === 'number' && UPDATE_TYPES_IN_PHASE_1.includes(value) ? value : undefined
}

/** Read the filters from the URL query; anything unknown or malformed is ignored. */
export function parseCatalogQuery(query: LocationQuery): CatalogFilters {
  const status = first(query.status)
  const source = first(query.source)
  const update = first(query.update)
  return {
    view: first(query.view) === 'needs-attention' ? 'needs-attention' : 'all',
    status: STATUS_GROUPS.find((group) => group === status),
    source: source === undefined ? undefined : sourceFromName(source),
    updateType: update === undefined ? undefined : updateTypeFromName(update),
  }
}

/**
 * The query to navigate to: the base query with this screen's keys replaced by the filters. Keys
 * that are not set are removed, so the default view has a clean URL. Other keys are kept.
 */
export function toCatalogQuery(filters: CatalogFilters, base: LocationQuery = {}): LocationQueryRaw {
  const query: LocationQueryRaw = { ...base }
  for (const key of QUERY_KEYS) delete query[key]
  if (filters.view === 'needs-attention') query.view = 'needs-attention'
  if (filters.status !== undefined) query.status = filters.status
  if (filters.source !== undefined) query.source = DataSource[filters.source].toLowerCase()
  if (filters.updateType !== undefined) query.update = UpdateType[filters.updateType].toLowerCase()
  return query
}

/** The filters as the typed client takes them (the facets and the list share them). */
export function toApiFilters(filters: CatalogFilters): DatasetFilters {
  return {
    status: filters.status,
    source: filters.source,
    updateType: filters.updateType,
    needsAttention: filters.view === 'needs-attention' ? true : undefined,
  }
}

/** Whether any sidebar filter (not the view) is set: the Clear link shows then. */
export function hasActiveFilters(filters: CatalogFilters): boolean {
  return filters.status !== undefined || filters.source !== undefined || filters.updateType !== undefined
}

/** The same filters with the view set to the given one. */
export function withView(filters: CatalogFilters, view: CatalogView): CatalogFilters {
  return { ...filters, view }
}

/** Key for a filter-set: changes exactly when a re-query is needed. */
export function filtersKey(filters: CatalogFilters): string {
  return JSON.stringify([filters.view, filters.status, filters.source, filters.updateType])
}
