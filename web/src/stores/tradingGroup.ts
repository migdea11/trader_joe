// The Simulation | Paper | Live toggle's state (tj-u3r3mo). The allowed groups come from the server
// (UiConfig, tj-0rpt9t); a group that is not allowed stays in the list, greyed in place. The
// selection lives here and in the URL query (`?group=paper`, kept in step by
// useTradingGroupUrlSync). Data pages ignore it: market data is shared across groups (tj-yq0htn).
import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { GROUP_SPECS, type AccountGroupName } from '@/theme/semantics'
import { useUiConfigStore } from './uiConfig'

/** The toggle's segments, left to right, and their URL values. */
export const GROUP_OPTIONS = [
  { key: 'simulation', group: 'ACCOUNT_GROUP_SIMULATION' },
  { key: 'paper', group: 'ACCOUNT_GROUP_PAPER' },
  { key: 'live', group: 'ACCOUNT_GROUP_LIVE' },
] as const satisfies readonly { key: string; group: AccountGroupName }[]

export type GroupKey = (typeof GROUP_OPTIONS)[number]['key']

export const GROUP_QUERY_PARAM = 'group'

export function isGroupKey(value: unknown): value is GroupKey {
  return GROUP_OPTIONS.some((option) => option.key === value)
}

export const useTradingGroupStore = defineStore('tradingGroup', () => {
  const config = useUiConfigStore()
  /** What the URL or the user asked for; may be a group the server does not allow. */
  const requested = ref<GroupKey | null>(null)

  /** Every segment, always all three, with whether the server allows it. */
  const options = computed(() =>
    GROUP_OPTIONS.map((option) => ({
      key: option.key,
      group: option.group,
      label: GROUP_SPECS[option.group].label,
      allowed: config.allowedGroups.includes(option.group),
    })),
  )

  /**
   * The group in effect: the requested one when allowed, else the first allowed one (Simulation
   * first), else null (config not loaded, failed, or allows nothing).
   */
  const selected = computed<GroupKey | null>(() => {
    const allowed = options.value.filter((o) => o.allowed)
    const match = allowed.find((o) => o.key === requested.value)
    return (match ?? allowed[0])?.key ?? null
  })

  /** Select a group. A group that is not allowed is ignored (false). */
  function select(key: GroupKey): boolean {
    if (!options.value.some((o) => o.key === key && o.allowed)) return false
    requested.value = key
    return true
  }

  /** Take the group from a URL query value; anything unknown clears the request. */
  function setFromQuery(value: unknown): void {
    const raw = Array.isArray(value) ? value[0] : value
    requested.value = isGroupKey(raw) ? raw : null
  }

  return { requested, options, selected, select, setFromQuery }
})
