// Status, health and trading-group label and colour maps: the one module every badge reads
// (tj-mujie8, tj-grna9p.60). Words and colours are from the design canvas Data boards and the
// architect's mapping on tj-grna9p.60.
//
// The keys are the proto enum value names as the server's JSON carries them (proto ui/v1
// data.proto, shell.proto). They are plain strings, not imports from the generated client, which
// is written in tj-grna9p.29; a later change may tighten them to the generated types.
import { colors } from './tokens'

export interface BadgeSpec {
  label: string
  // A token colour: the dot and the label text. Meaning is carried by the label, never colour alone.
  color: string
}

// --- Status (DatasetHealthStatus) -------------------------------------------------------

export const DATASET_HEALTH_STATUS_VALUES = [
  'DATASET_HEALTH_STATUS_UNSPECIFIED',
  'DATASET_HEALTH_STATUS_FRESH',
  'DATASET_HEALTH_STATUS_LATE',
  'DATASET_HEALTH_STATUS_OVERDUE',
  'DATASET_HEALTH_STATUS_COMPLETE',
  'DATASET_HEALTH_STATUS_GAPS',
  'DATASET_HEALTH_STATUS_RETIRED',
] as const

export type DatasetHealthStatusName = (typeof DATASET_HEALTH_STATUS_VALUES)[number]

const HEALTHY: BadgeSpec = { label: 'Healthy', color: colors.status.ok }
const LATE: BadgeSpec = { label: 'Late', color: colors.status.warn }
const FAILED: BadgeSpec = { label: 'Failed', color: colors.status.fail }
const RETIRED: BadgeSpec = { label: 'Stopped', color: colors.status.retired }
// A dataset can have no health at all (e.g. a source without a calendar): the server leaves
// health, or its status, unset. That is shown as its own state, never as Healthy. The spec
// gives no word or colour for it; "Unknown" in the neutral text3 token is the provisional choice
// (open question on tj-grna9p.60).
const UNKNOWN: BadgeSpec = { label: 'Unknown', color: colors.text3 }

// Health to UI label (architect choice, tj-grna9p.60): FRESH and COMPLETE are Healthy, LATE is
// Late, OVERDUE and GAPS are Failed, RETIRED is Stopped (collect off), unset is Unknown.
export const HEALTH_BADGES: Readonly<Record<DatasetHealthStatusName, BadgeSpec>> = {
  DATASET_HEALTH_STATUS_UNSPECIFIED: UNKNOWN,
  DATASET_HEALTH_STATUS_FRESH: HEALTHY,
  DATASET_HEALTH_STATUS_LATE: LATE,
  DATASET_HEALTH_STATUS_OVERDUE: FAILED,
  DATASET_HEALTH_STATUS_COMPLETE: HEALTHY,
  DATASET_HEALTH_STATUS_GAPS: FAILED,
  DATASET_HEALTH_STATUS_RETIRED: RETIRED,
}

function isHealthStatusName(value: string): value is DatasetHealthStatusName {
  return Object.hasOwn(HEALTH_BADGES, value)
}

// The badge for a dataset's health status. An unset status (null, undefined, empty, or
// UNSPECIFIED) is Unknown. A value this build does not know renders as Unknown too, rather than
// guessing a health.
export function healthBadge(status: string | null | undefined): BadgeSpec {
  if (status == null || !isHealthStatusName(status)) return UNKNOWN
  return HEALTH_BADGES[status]
}

// Needs Attention = LATE, OVERDUE or GAPS (data.proto DatasetFacets.needs_attention).
const NEEDS_ATTENTION: ReadonlySet<string> = new Set<DatasetHealthStatusName>([
  'DATASET_HEALTH_STATUS_LATE',
  'DATASET_HEALTH_STATUS_OVERDUE',
  'DATASET_HEALTH_STATUS_GAPS',
])

export function needsAttention(status: string | null | undefined): boolean {
  return status != null && NEEDS_ATTENTION.has(status)
}

// A RUNNING run overlays Running on the collection badge.
export const RUNNING_BADGE: BadgeSpec = { label: 'Running', color: colors.status.running }

export function collectionBadge(
  status: string | null | undefined,
  options: { running?: boolean } = {},
): BadgeSpec {
  return options.running ? RUNNING_BADGE : healthBadge(status)
}

// --- Usage staleness ---------------------------------------------------------------------------

// Unread for the threshold. A SEPARATE badge from collection health and never shares a colour
// meaning with it: it takes the neutral text2 token, not a status colour. (The canvas draws the
// stale text in the warn amber, which is Late's colour; that conflicts with this rule and is an
// open question on tj-grna9p.60.)
export const STALE_BADGE: BadgeSpec = { label: 'Stale', color: colors.text2 }

// --- Request and run state ---------------------------------------------------------------------

export const REQUEST_BADGES = {
  queued: { label: 'Queued', color: colors.status.info },
  running: RUNNING_BADGE,
  done: { label: 'Done', color: colors.status.ok },
  failed: FAILED,
} as const satisfies Record<string, BadgeSpec>

export type RequestStateKey = keyof typeof REQUEST_BADGES

// A state with no entry (REFUSED is never written in PR 4, but could arrive) renders with the
// fail colour and its raw label (architect amendment on tj-grna9p.60).
export function requestBadge(state: string): BadgeSpec {
  return Object.hasOwn(REQUEST_BADGES, state)
    ? REQUEST_BADGES[state as RequestStateKey]
    : { label: state, color: colors.status.fail }
}

// --- Events connection (api/events.ts EventsConnectionState) -----------------------------------

// The shell indicator's words. "Live" is avoided on purpose: it is a trading group and owns its
// colour. A dropped socket reads Reconnecting (the client retries on its own, so it is never
// a dead end); the data on screen is a refetch away either way.
export const EVENTS_CONNECTION_BADGES = {
  connecting: { label: 'Connecting', color: colors.status.info },
  open: { label: 'Connected', color: colors.status.ok },
  reconnecting: { label: 'Reconnecting', color: colors.status.warn },
} as const satisfies Record<string, BadgeSpec>

// What the indicator's tooltip says, as a sentence.
export const EVENTS_CONNECTION_HINTS: Readonly<Record<keyof typeof EVENTS_CONNECTION_BADGES, string>> = {
  connecting: 'Opening the live updates connection',
  open: 'Receiving live updates',
  reconnecting: 'Live updates dropped; retrying. Screens reload when it returns',
}

// --- Trading groups (AccountGroup) -------------------------------------------------------------

export const ACCOUNT_GROUP_VALUES = [
  'ACCOUNT_GROUP_SIMULATION',
  'ACCOUNT_GROUP_PAPER',
  'ACCOUNT_GROUP_LIVE',
] as const

export type AccountGroupName = (typeof ACCOUNT_GROUP_VALUES)[number]

export interface GroupSpec {
  label: string
  // Tag outline and text colour in tables.
  color: string
  // Fill of the selected toggle segment, and the text on it. Simulation and Paper select with
  // the accent FILL (white text); only Live selects with orange, and carries DARK text because
  // white on that orange fails contrast.
  selectedFill: string
  onSelected: string
}

export const GROUP_SPECS: Readonly<Record<AccountGroupName, GroupSpec>> = {
  ACCOUNT_GROUP_SIMULATION: {
    label: 'Simulation',
    color: colors.group.simulation,
    selectedFill: colors.accentFill,
    onSelected: colors.onAccent,
  },
  ACCOUNT_GROUP_PAPER: {
    label: 'Paper',
    color: colors.group.paper,
    selectedFill: colors.accentFill,
    onSelected: colors.onAccent,
  },
  ACCOUNT_GROUP_LIVE: {
    label: 'Live',
    color: colors.group.live,
    selectedFill: colors.group.live,
    onSelected: colors.onLive,
  },
}
