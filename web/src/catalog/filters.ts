// The catalog's filter state and its two mappings: to the page URL (so a reload keeps the filters)
// and to the /ui/v1/datasets query (tj-grna9p.32). Pure functions; the components own the router.
//
// The server takes ONE value per filter (status, source, update_type), so each sidebar group is a
// single choice. A dataset with no health (freshness unset) matches no status value; it is only in
// the unfiltered list.
import { DataSource, UpdateType } from '@generated/trader_joe/proto/market/v1/enums_pb'
import { ClientKind, UsageFilter } from '@generated/trader_joe/proto/ui/v1/data_pb'
import type { LocationQuery, LocationQueryRaw } from 'vue-router'

import { DEFAULT_CLIENT_KINDS, EXPIRING_SOON_DAYS, STALE_DAYS } from '@/api'
import { DAY_MS } from '@/format/days'
import type { DatasetFilters, DatasetSort, StatusGroup } from '@/api'

export type CatalogView = 'all' | 'needs-attention' | 'expiring-soon'

/** The Usage group's values, as the URL spells them. Stale is also a view in the sidebar. */
export type UsageGroup = 'read-30d' | 'stale' | 'never-read'

export const USAGE_GROUPS: readonly UsageGroup[] = ['read-30d', 'stale', 'never-read']

const USAGE_FILTER_OF: Record<UsageGroup, UsageFilter> = {
  'read-30d': UsageFilter.READ_30D,
  stale: UsageFilter.STALE,
  'never-read': UsageFilter.NEVER_READ,
}

/** The Read By checkboxes, in display order; the URL spells them by lower-case name. */
export const CLIENT_KIND_OPTIONS: readonly ClientKind[] = [ClientKind.SDK, ClientKind.UI]

export interface CatalogFilters {
  view: CatalogView
  status?: StatusGroup
  source?: DataSource
  updateType?: UpdateType
  /** Only datasets with a collection run in flight; independent of `status`. */
  running?: boolean
  /** The Usage group's value; independent of the view. */
  usage?: UsageGroup
  /** Whose reads the usage counts: Read By. Unset (or empty) means DEFAULT_CLIENT_KINDS. */
  clientKinds?: ClientKind[]
}

/** The URL query keys this screen owns. Others (the trading group) are not touched. */
export const QUERY_KEYS = ['view', 'status', 'source', 'update', 'running', 'usage', 'readby'] as const

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

/** The kinds in effect: the filter's, or the default when none is set. Always in display order. */
export function effectiveClientKinds(filters: Pick<CatalogFilters, 'clientKinds'>): ClientKind[] {
  const chosen = filters.clientKinds ?? []
  const kinds = CLIENT_KIND_OPTIONS.filter((kind) => chosen.includes(kind))
  return kinds.length === 0 ? [...DEFAULT_CLIENT_KINDS] : kinds
}

function isDefaultKinds(kinds: readonly ClientKind[]): boolean {
  return kinds.length === DEFAULT_CLIENT_KINDS.length && kinds.every((kind) => DEFAULT_CLIENT_KINDS.includes(kind))
}

// "sdk,ui" (any order, any case); unknown names are ignored and none valid leaves the default.
function kindsFromText(text: string | undefined): ClientKind[] | undefined {
  if (text === undefined) return undefined
  const names = text.toLowerCase().split(',')
  const kinds = CLIENT_KIND_OPTIONS.filter((kind) => names.includes(ClientKind[kind].toLowerCase()))
  return kinds.length === 0 || isDefaultKinds(kinds) ? undefined : kinds
}

/** Read the filters from the URL query; anything unknown or malformed is ignored. */
export function parseCatalogQuery(query: LocationQuery): CatalogFilters {
  const status = first(query.status)
  const source = first(query.source)
  const update = first(query.update)
  const view = first(query.view)
  const usage = first(query.usage)
  return {
    view: view === 'needs-attention' || view === 'expiring-soon' ? view : 'all',
    status: STATUS_GROUPS.find((group) => group === status),
    source: source === undefined ? undefined : sourceFromName(source),
    updateType: update === undefined ? undefined : updateTypeFromName(update),
    running: first(query.running) === 'true' ? true : undefined,
    usage: USAGE_GROUPS.find((group) => group === usage),
    clientKinds: kindsFromText(first(query.readby)),
  }
}

/**
 * The query to navigate to: the base query with this screen's keys replaced by the filters. Keys
 * that are not set are removed, so the default view has a clean URL. Other keys are kept.
 */
export function toCatalogQuery(filters: CatalogFilters, base: LocationQuery = {}): LocationQueryRaw {
  const query: LocationQueryRaw = { ...base }
  for (const key of QUERY_KEYS) delete query[key]
  if (filters.view !== 'all') query.view = filters.view
  if (filters.usage !== undefined) query.usage = filters.usage
  const kinds = effectiveClientKinds(filters)
  if (!isDefaultKinds(kinds)) query.readby = kinds.map((kind) => ClientKind[kind].toLowerCase()).join(',')
  if (filters.status !== undefined) query.status = filters.status
  if (filters.source !== undefined) query.source = DataSource[filters.source].toLowerCase()
  if (filters.running === true) query.running = 'true'
  if (filters.updateType !== undefined) query.update = UpdateType[filters.updateType].toLowerCase()
  return query
}

/** The window Expiring Soon looks into, from `now`: a filter on the store, so it is sent only in the Expiring Soon view. */
export function expiringBefore(now: number = Date.now()): Date {
  return new Date(now + EXPIRING_SOON_DAYS * DAY_MS)
}

/**
 * The filters as the typed client takes them (the facets and the list share them). The client kinds
 * and the stale threshold always go out, so the usage figures answer for the Read By choice. The
 * Expiring Soon bound is a filter: it goes out only in that view (elsewhere the server's default feeds the count).
 */
export function toApiFilters(filters: CatalogFilters): DatasetFilters {
  return {
    status: filters.status,
    source: filters.source,
    updateType: filters.updateType,
    needsAttention: filters.view === 'needs-attention' ? true : undefined,
    running: filters.running === true ? true : undefined,
    usage: filters.usage === undefined ? undefined : USAGE_FILTER_OF[filters.usage],
    expiresBefore: filters.view === 'expiring-soon' ? expiringBefore() : undefined,
    ...toUsageFilters(filters),
  }
}

/** The parameters the usage figures depend on, and nothing else: what the whole-catalog tiles send. */
export function toUsageFilters(filters: CatalogFilters): DatasetFilters {
  return { clientKinds: effectiveClientKinds(filters), staleDays: STALE_DAYS }
}

/** How the list is ordered under the filters: by last read in a usage group, soonest expiry in Expiring Soon; the server's symbol order otherwise (undefined). */
export function listSort(filters: CatalogFilters): DatasetSort | undefined {
  if (filters.view === 'expiring-soon') return 'expires'
  return filters.usage === undefined ? undefined : 'last_read'
}

/** Whether any sidebar filter (not the view) is set: the Clear link shows then. */
export function hasActiveFilters(filters: CatalogFilters): boolean {
  return (
    filters.status !== undefined ||
    filters.source !== undefined ||
    filters.updateType !== undefined ||
    filters.running === true ||
    filters.usage !== undefined ||
    !isDefaultKinds(effectiveClientKinds(filters))
  )
}

/** The same filters with the view set to the given one. */
export function withView(filters: CatalogFilters, view: CatalogView): CatalogFilters {
  return { ...filters, view }
}

/** Key for a filter-set: changes exactly when a re-query is needed. */
export function filtersKey(filters: CatalogFilters): string {
  return JSON.stringify([
    filters.view,
    filters.status,
    filters.source,
    filters.updateType,
    filters.running,
    filters.usage,
    effectiveClientKinds(filters),
  ])
}
