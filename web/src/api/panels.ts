// Typed functions for the panel routes (server/routers/data_store/ui_panel.py, ADR tj-26jt9w addendum 22):
// eight reads (R1 to R8) and three writes (W1 to W3). Panels are UI-only: there is no client or SDK panel
// API. A write body is built from the generated request message and sent as protobuf canonical JSON, so
// an optional field's presence is exactly what the form set (an absent one is refused 422, never read as
// zero or false).

import { create, toJson } from '@bufbuild/protobuf'
import type { DescMessage, MessageInitShape } from '@bufbuild/protobuf'
import { DataSourceSchema } from '@generated/trader_joe/proto/market/v1/enums_pb'
import type { DataSource } from '@generated/trader_joe/proto/market/v1/enums_pb'
import {
  AssetListHistorySchema,
  CorporateActionEventPageSchema,
  CreatePanelRequestSchema,
  PanelBoundariesSchema,
  PanelDetailSchema,
  PanelListSchema,
  PanelMemberPageSchema,
  PanelSettingsVersionPageSchema,
  PanelSettingsVersionSchema,
  PanelSourceSettingsVersionPageSchema,
  PanelSourceSettingsVersionSchema,
  SavePanelSettingsRequestSchema,
  SaveSourceSettingsRequestSchema,
} from '@generated/trader_joe/proto/ui/v1/panels_pb'
import type {
  AssetListHistory,
  CorporateActionEventPage,
  PanelBoundaries,
  PanelDetail,
  PanelList,
  PanelMemberPage,
  PanelSettingsVersion,
  PanelSettingsVersionPage,
  PanelSourceSettingsVersion,
  PanelSourceSettingsVersionPage,
} from '@generated/trader_joe/proto/ui/v1/panels_pb'

import { getMessage, postMessage } from './client'
import type { WriteBody } from './client'

export type CreatePanelParams = MessageInitShape<typeof CreatePanelRequestSchema>
export type SavePanelSettingsParams = MessageInitShape<typeof SavePanelSettingsRequestSchema>
export type SaveSourceSettingsParams = MessageInitShape<typeof SaveSourceSettingsRequestSchema>

/** The proto enum NAME the source route takes (DATA_SOURCE_ALPACA_API), not the TS member name. */
export function dataSourceRouteName(source: DataSource): string {
  const value = DataSourceSchema.values.find((entry) => entry.number === source)
  return value?.name ?? 'DATA_SOURCE_UNSPECIFIED'
}

/** A request message as its canonical JSON body: an optional field is present only when the form set it. */
function bodyOf<Desc extends DescMessage>(schema: Desc, init: MessageInitShape<Desc>): WriteBody {
  return toJson(schema, create(schema, init)) as WriteBody
}

const panelPath = (id: string): string => `/ui/v1/panels/${encodeURIComponent(id)}`
const sourcePath = (source: DataSource): string =>
  `/ui/v1/panel-sources/${encodeURIComponent(dataSourceRouteName(source))}/settings-versions`

export interface PageParams {
  /** The previous page's nextCursor. */
  cursor?: string
  /** Server default 100, at most 500. */
  limit?: number
  signal?: AbortSignal
}

export interface WindowParams {
  /** Included. */
  start: Date
  /** Excluded; after start, at most 366 days on. */
  end: Date
  signal?: AbortSignal
}

/** GET /ui/v1/panels (R1): the selector's panels. */
export function listPanels(signal?: AbortSignal): Promise<PanelList> {
  return getMessage(PanelListSchema, '/ui/v1/panels', { signal })
}

/** GET /ui/v1/panels/{id} (R2): the panel with its current settings versions and membership counts. */
export function getPanel(id: string, signal?: AbortSignal): Promise<PanelDetail> {
  return getMessage(PanelDetailSchema, panelPath(id), { signal })
}

/** GET /ui/v1/panels/{id}/settings-versions (R3): the panel's own settings versions, newest first. */
export function getPanelSettingsVersions(id: string, params: PageParams = {}): Promise<PanelSettingsVersionPage> {
  return getMessage(PanelSettingsVersionPageSchema, `${panelPath(id)}/settings-versions`, {
    query: { cursor: params.cursor, limit: params.limit },
    signal: params.signal,
  })
}

/** GET /ui/v1/panel-sources/{source}/settings-versions (R4): a source's settings versions, newest first. */
export function getSourceSettingsVersions(
  source: DataSource,
  params: PageParams = {},
): Promise<PanelSourceSettingsVersionPage> {
  return getMessage(PanelSourceSettingsVersionPageSchema, sourcePath(source), {
    query: { cursor: params.cursor, limit: params.limit },
    signal: params.signal,
  })
}

/** GET /ui/v1/panels/{id}/boundaries (R5): the history strip's markers in the half-open window [start, end). */
export function getPanelBoundaries(id: string, params: WindowParams): Promise<PanelBoundaries> {
  return getMessage(PanelBoundariesSchema, `${panelPath(id)}/boundaries`, {
    query: { start: params.start, end: params.end },
    signal: params.signal,
  })
}

/** GET /ui/v1/panels/{id}/members (R6): one page of the panel's members, by symbol. */
export function getPanelMembers(id: string, params: PageParams = {}): Promise<PanelMemberPage> {
  return getMessage(PanelMemberPageSchema, `${panelPath(id)}/members`, {
    query: { cursor: params.cursor, limit: params.limit },
    signal: params.signal,
  })
}

/** GET /ui/v1/panels/{id}/asset-list-history (R7): one entry per expected day in [start, end), ascending. */
export function getAssetListHistory(id: string, params: WindowParams): Promise<AssetListHistory> {
  return getMessage(AssetListHistorySchema, `${panelPath(id)}/asset-list-history`, {
    query: { start: params.start, end: params.end },
    signal: params.signal,
  })
}

/** GET /ui/v1/datasets/{id}/corporate-actions (R8): one page of the dataset's corporate-action events. */
export function getCorporateActions(datasetId: string, params: PageParams = {}): Promise<CorporateActionEventPage> {
  return getMessage(
    CorporateActionEventPageSchema,
    `/ui/v1/datasets/${encodeURIComponent(datasetId)}/corporate-actions`,
    { query: { cursor: params.cursor, limit: params.limit }, signal: params.signal },
  )
}

/** POST /ui/v1/panels (W1): create a panel with a recorded reason. Answers 201 with the PanelDetail. */
export function createPanel(params: CreatePanelParams, signal?: AbortSignal): Promise<PanelDetail> {
  return postMessage(PanelDetailSchema, '/ui/v1/panels', {
    body: bodyOf(CreatePanelRequestSchema, params),
    signal,
  })
}

/** POST /ui/v1/panels/{id}/settings-versions (W2): save a new settings version (enable and disable included). */
export function savePanelSettings(
  id: string,
  params: SavePanelSettingsParams,
  signal?: AbortSignal,
): Promise<PanelSettingsVersion> {
  return postMessage(PanelSettingsVersionSchema, `${panelPath(id)}/settings-versions`, {
    body: bodyOf(SavePanelSettingsRequestSchema, params),
    signal,
  })
}

/** POST /ui/v1/panel-sources/{source}/settings-versions (W3): save a new source settings version. */
export function saveSourceSettings(
  source: DataSource,
  params: SaveSourceSettingsParams,
  signal?: AbortSignal,
): Promise<PanelSourceSettingsVersion> {
  return postMessage(PanelSourceSettingsVersionSchema, sourcePath(source), {
    body: bodyOf(SaveSourceSettingsRequestSchema, params),
    signal,
  })
}
