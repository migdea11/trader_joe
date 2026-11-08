// What the Panels page's components share: the words for the panel enums, the settings form's draft and
// its conversion to a request, the placement of a marker on the strip and the words a refused write is
// shown in. The server decides every count, state and health; this only words and places what came back.
//
// Copy follows theme/copy.ts: Title Case for labels and buttons, sentence case for sentences. The word is
// panel; nothing here names a market, a world or a universe.
import { AssetType, DataSource } from '@generated/trader_joe/proto/market/v1/enums_pb'
import { DatasetHealthStatusSchema } from '@generated/trader_joe/proto/ui/v1/data_pb'
import type { DatasetHealthStatus } from '@generated/trader_joe/proto/ui/v1/data_pb'
import {
  AssetListDayState,
  PanelCadence,
  PanelMarkerKind,
  PanelPassKind,
  PanelTransition,
} from '@generated/trader_joe/proto/ui/v1/panels_pb'
import type {
  EffectivePass,
  PanelBoundaries,
  PanelMarker,
  PanelSettings,
  PanelSettingsVersion,
  PanelSourceSettingsVersion,
} from '@generated/trader_joe/proto/ui/v1/panels_pb'

import type { SavePanelSettingsParams } from '@/api'
import { timestampToInstant } from '@/format/formatters'
import type { PanelWriteOutcome } from '@/stores/panels'
import { PANEL_REFUSAL } from '@/theme/copy'
import { healthBadge } from '@/theme/semantics'
import type { BadgeSpec } from '@/theme/semantics'

// --- words -------------------------------------------------------------------------------------------

/** Selectable cadences. Monthly is the store's default, sent as UNSPECIFIED unless the operator picks one. */
export const CADENCE_CHOICES: readonly { value: PanelCadence; label: string }[] = [
  { value: PanelCadence.DAILY, label: 'Daily' },
  { value: PanelCadence.WEEKLY, label: 'Weekly' },
  { value: PanelCadence.MONTHLY, label: 'Monthly' },
]

/** Shown for a create that names no cadence. */
export const DEFAULT_CADENCE_LABEL = 'Monthly (default)'

export function cadenceLabel(cadence: PanelCadence): string {
  return CADENCE_CHOICES.find((choice) => choice.value === cadence)?.label ?? 'Unknown'
}

const TRANSITION_LABELS: Partial<Record<PanelTransition, string>> = {
  [PanelTransition.ENTERED]: 'Entered',
  [PanelTransition.CREATED]: 'Created',
  [PanelTransition.DEFERRED]: 'Deferred',
  [PanelTransition.REFUSED]: 'Refused',
  [PanelTransition.EXITED]: 'Exited',
  [PanelTransition.DRAINING]: 'Draining',
  [PanelTransition.DRAINED]: 'Drained',
}

export function transitionLabel(status: PanelTransition): string {
  return TRANSITION_LABELS[status] ?? 'Unknown'
}

const DAY_STATE_LABELS: Partial<Record<AssetListDayState, string>> = {
  [AssetListDayState.SAVED]: 'Saved',
  [AssetListDayState.MISSING]: 'Missing',
  [AssetListDayState.EMPTY]: 'Empty',
  [AssetListDayState.NOT_DUE]: 'Not due',
}

export function dayStateLabel(state: AssetListDayState): string {
  return DAY_STATE_LABELS[state] ?? 'Unknown'
}

const MARKER_LABELS: Partial<Record<PanelMarkerKind, string>> = {
  [PanelMarkerKind.PANEL_SETTINGS]: 'Panel settings',
  [PanelMarkerKind.SOURCE_SETTINGS]: 'Source settings',
  [PanelMarkerKind.COLLECT_STOP]: 'Collect stopped',
  [PanelMarkerKind.COLLECT_RESUME]: 'Collect resumed',
}

export function markerLabel(kind: PanelMarkerKind): string {
  return MARKER_LABELS[kind] ?? 'Marker'
}

const PASS_KIND_LABELS: Partial<Record<PanelPassKind, string>> = {
  [PanelPassKind.MEMBERSHIP]: 'membership pass',
  [PanelPassKind.RELEASE]: 'release pass',
}

/** "membership pass, 2026-10-30": the pass a settings version first took effect in. */
export function effectivePassText(pass: EffectivePass, formatDateTime: (instant: number) => string): string {
  const kind = PASS_KIND_LABELS[pass.kind] ?? 'pass'
  return pass.startedAt === undefined ? kind : `${kind}, ${formatDateTime(timestampToInstant(pass.startedAt))}`
}

/** A member's dataset health as the shared badge (Healthy, Late, Failed, Stopped; Unknown when it has no dataset). */
export function memberHealthBadge(health: DatasetHealthStatus): BadgeSpec {
  const name = DatasetHealthStatusSchema.values.find((entry) => entry.number === health)?.name
  return healthBadge(name)
}

// --- the settings draft -----------------------------------------------------------------------------

/** A tri-state filter: absent means no filter. */
export type Tri = '' | 'true' | 'false'

/** The settings form's fields as the operator edits them. */
export interface PanelDraft {
  enabled: boolean
  /** UNSPECIFIED: not named (a create then takes the store's default). */
  cadence: PanelCadence
  allowSymbols: string
  denySymbols: string
  assetClass: AssetType
  exchange: string
  active: Tri
  tradable: Tri
}

export function emptyDraft(): PanelDraft {
  return {
    enabled: true,
    cadence: PanelCadence.UNSPECIFIED,
    allowSymbols: '',
    denySymbols: '',
    assetClass: AssetType.UNSPECIFIED,
    exchange: '',
    active: '',
    tradable: '',
  }
}

const triOf = (value: boolean | undefined): Tri => (value === undefined ? '' : value ? 'true' : 'false')

export function draftFromSettings(settings: PanelSettings): PanelDraft {
  return {
    enabled: settings.enabled,
    cadence: settings.cadence,
    allowSymbols: settings.allowSymbols.join(', '),
    denySymbols: settings.denySymbols.join(', '),
    assetClass: settings.assetClass ?? AssetType.UNSPECIFIED,
    exchange: settings.exchange ?? '',
    active: triOf(settings.active),
    tradable: triOf(settings.tradable),
  }
}

/** "AAPL, msft vfv" as ["AAPL", "msft", "vfv"]: commas and spaces separate, empties drop. */
export function parseSymbols(text: string): string[] {
  return text.split(/[\s,]+/).filter((symbol) => symbol !== '')
}

/** The largest value an int32 field on the wire takes. */
export const MAX_INT32 = 2147483647
/** Ramp: valid 1..MAX_INT32; zero is refused (pause a panel with Enabled off). */
export const MIN_RAMP_PER_HOUR = 1
/** Cap: valid 2..MAX_INT32; each new panel dataset costs two requests. */
export const MIN_REQUEST_CAP_PER_HOUR = 2

/** A count (ramp, cap, window days) as a non-negative whole number up to MAX_INT32, or null when the field is not one (blank included). */
export function parseCount(text: string): number | null {
  const trimmed = text.trim()
  if (!/^\d{1,10}$/.test(trimmed)) return null
  const value = Number(trimmed)
  return value > MAX_INT32 ? null : value
}

const boolOf = (value: Tri): boolean | undefined => (value === '' ? undefined : value === 'true')

/** The settings half of a save or create request. */
export type SettingsInit = Omit<SavePanelSettingsParams, '$typeName' | 'reason'>

/**
 * The saved settings as a request with only `enabled` changed: every field as read, an optional field
 * present only if it was saved, nothing trimmed or parsed. Enable and Disable use this, not the form's
 * text round trip, because a save replaces the settings as sent and any drift would change membership.
 */
export function settingsInitWithEnabled(saved: PanelSettings, enabled: boolean): SettingsInit {
  const init: SettingsInit = {
    enabled,
    cadence: saved.cadence,
    allowSymbols: [...saved.allowSymbols],
    denySymbols: [...saved.denySymbols],
  }
  if (saved.assetClass !== undefined) init.assetClass = saved.assetClass
  if (saved.exchange !== undefined) init.exchange = saved.exchange
  if (saved.active !== undefined) init.active = saved.active
  if (saved.tradable !== undefined) init.tradable = saved.tradable
  return init
}

/**
 * The settings fields of a request from the form. An absent
 * optional filter is left out, which the server reads as no filter (a save replaces the settings as sent).
 */
export function draftToSettingsInit(draft: PanelDraft): SettingsInit {
  const exchange = draft.exchange.trim()
  return {
    enabled: draft.enabled,
    cadence: draft.cadence,
    allowSymbols: parseSymbols(draft.allowSymbols),
    denySymbols: parseSymbols(draft.denySymbols),
    assetClass: draft.assetClass === AssetType.UNSPECIFIED ? undefined : draft.assetClass,
    exchange: exchange === '' ? undefined : exchange,
    active: boolOf(draft.active),
    tradable: boolOf(draft.tradable),
  }
}

/** The settings fields that can block a save or create. */
export type DraftField = 'cadence'

/** The settings form's own problems per field, in words. Empty when it can be sent. */
export function draftFieldErrors(
  draft: PanelDraft,
  options: { cadenceRequired: boolean },
): Partial<Record<DraftField, string>> {
  const errors: Partial<Record<DraftField, string>> = {}
  if (options.cadenceRequired && draft.cadence === PanelCadence.UNSPECIFIED) errors.cadence = 'Choose a cadence.'
  return errors
}

/** The settings form's own problems, in words: what stops Save. Empty when it can be sent. */
export function draftProblem(draft: PanelDraft, options: { cadenceRequired: boolean }): string {
  return draftFieldErrors(draft, options).cadence ?? ''
}

/** What the create form knows when it decides whether Create Panel can be pressed. */
export interface CreateInput {
  name: string
  nameTaken: boolean
  source: DataSource
  /** The source-settings read failed. */
  sourceProblem: boolean
  /** null: not known yet (no source chosen, or the read is out); true: the source has no settings version. */
  sourceNeedsSettings: boolean | null
  ramp: string
  cap: string
  pre: string
  post: string
}

/** A create form field that can carry an error. */
export type CreateField = 'name' | 'source' | 'ramp' | 'cap' | 'pre' | 'post'

const COUNT_ERROR = `Give a whole number, 0 to ${MAX_INT32}.`
const RAMP_ERROR = `Give a whole number, ${MIN_RAMP_PER_HOUR} to ${MAX_INT32}, or leave it empty for no limit.`
const CAP_ERROR = `Give a whole number, ${MIN_REQUEST_CAP_PER_HOUR} to ${MAX_INT32}, or leave it empty for no limit.`

/** A rate field's problem in words, or '' when it is empty (no limit) or a whole number in range. */
export function rateFieldError(text: string, field: 'ramp' | 'cap'): string {
  if (text.trim() === '') return ''
  const value = parseCount(text)
  const min = field === 'ramp' ? MIN_RAMP_PER_HOUR : MIN_REQUEST_CAP_PER_HOUR
  if (value !== null && value >= min) return ''
  return field === 'ramp' ? RAMP_ERROR : CAP_ERROR
}

/**
 * Every field of the create form that is empty or invalid, with the words for it. The one place the create
 * rules live: the button is disabled exactly when this is not empty, and each field's mark reads from it.
 */
export function createFieldErrors(input: CreateInput): Partial<Record<CreateField, string>> {
  const errors: Partial<Record<CreateField, string>> = {}
  const name = input.name.trim()
  if (name === '') errors.name = 'Give the panel a name.'
  else if (input.nameTaken) errors.name = PANEL_REFUSAL.nameTaken(name)
  if (input.source === DataSource.UNSPECIFIED) errors.source = 'Choose a source.'
  else if (input.sourceProblem) errors.source = 'The source settings could not be read. Choose the source again.'
  else if (input.sourceNeedsSettings === null) errors.source = 'Reading the source settings.'
  if (input.sourceNeedsSettings) {
    const ramp = rateFieldError(input.ramp, 'ramp')
    if (ramp !== '') errors.ramp = ramp
    const cap = rateFieldError(input.cap, 'cap')
    if (cap !== '') errors.cap = cap
    if (input.pre.trim() !== '' && parseCount(input.pre) === null) errors.pre = COUNT_ERROR
    if (input.post.trim() !== '' && parseCount(input.post) === null) errors.post = COUNT_ERROR
  }
  return errors
}

// --- the strip --------------------------------------------------------------------------------------

/** Where a marker sits on the strip, as a percentage of the window, clamped to the strip. */
export function markerPosition(at: number, start: number, end: number): number {
  if (end <= start) return 0
  return Math.min(100, Math.max(0, ((at - start) / (end - start)) * 100))
}

/** A strip marker with what the version reads say about it. */
export interface StripMarker {
  marker: PanelMarker
  /** 0 to 100 across the window. */
  position: number
  /** A settings version no pass has run under yet: pending, not a boundary. */
  pending: boolean
  /** The pass the version first took effect in; absent for a collect marker or a pending version. */
  effectivePass?: EffectivePass
}

/**
 * Join the boundary read's markers with the settings versions' effective passes (the markers carry the
 * version number, the version pages carry the pass). A version without a pass is pending.
 */
export function stripMarkers(
  boundaries: PanelBoundaries,
  panelVersions: readonly PanelSettingsVersion[],
  sourceVersions: readonly PanelSourceSettingsVersion[],
  window: { start: number; end: number },
): StripMarker[] {
  return boundaries.markers.map((marker) => {
    const at = marker.at === undefined ? window.start : timestampToInstant(marker.at)
    const position = markerPosition(at, window.start, window.end)
    if (marker.kind === PanelMarkerKind.PANEL_SETTINGS || marker.kind === PanelMarkerKind.SOURCE_SETTINGS) {
      const versions = marker.kind === PanelMarkerKind.PANEL_SETTINGS ? panelVersions : sourceVersions
      const pass = versions.find((version) => version.versionNo === marker.versionNo)?.effectivePass
      return { marker, position, pending: pass === undefined, effectivePass: pass }
    }
    return { marker, position, pending: false }
  })
}

// --- a refused write --------------------------------------------------------------------------------

export interface PanelWriteNotice {
  text: string
  errorId?: string
}

/** Which panel write a notice is for: create (W1), panel settings (W2), source settings (W3). */
export type PanelWriteRoute = 'create' | 'settings' | 'source'

const REFUSED: Record<PanelWriteRoute, string> = {
  create: PANEL_REFUSAL.create,
  settings: PANEL_REFUSAL.settings,
  source: PANEL_REFUSAL.source,
}

/**
 * The words a refused panel write is shown in; null for a write that went through or whose answer was
 * dropped. The response body's text is never shown (tj-fa1rpu D8): a 422 reads fixed copy for the route
 * (copy.ts), or the specific copy when the store re-read found what changed.
 */
export function panelWriteNotice(
  outcome: PanelWriteOutcome,
  route: PanelWriteRoute,
): PanelWriteNotice | null {
  switch (outcome.kind) {
    case 'moved-on': {
      const { errorId } = outcome.error
      switch (outcome.what) {
        case 'name-taken':
          return { text: PANEL_REFUSAL.createNameTakenSince(outcome.name), errorId }
        case 'source-has-settings':
          return { text: PANEL_REFUSAL.createSourceHasSettings, errorId }
        case 'settings':
          return { text: PANEL_REFUSAL.settingsMoved, errorId }
        case 'source-settings':
          return { text: PANEL_REFUSAL.sourceMoved, errorId }
      }
      return null
    }
    case 'invalid':
      return { text: REFUSED[route], errorId: outcome.error.errorId }
    case 'unavailable':
      return {
        text: 'The database is not ready for this change, so nothing was changed. Run make migrate, then try again.',
        errorId: outcome.error.errorId,
      }
    case 'not-found':
      return { text: 'This panel no longer exists, so nothing was changed. Reload the page.' }
    case 'override-required':
      return { text: 'The server asked for an override, which a panel write never needs. Nothing was changed.' }
    case 'error':
      return {
        text: `Could not complete the change. ${outcome.error.message} Nothing was changed.`,
        errorId: outcome.error.errorId,
      }
    default:
      return null
  }
}
