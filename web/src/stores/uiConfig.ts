// The deployment's UiConfig (GET /ui/v1/config on data_store, via the typed client). It drives the
// trading-group toggle (allowed groups, tj-0rpt9t: allowed groups come from the SERVER) and the
// header's deployment tag. Display strings may arrive empty; the fallbacks below say so honestly
// rather than inventing a name.
import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { AccountGroup } from '@generated/trader_joe/proto/ui/v1/shell_pb'

import { getUiConfig } from '@/api'
import type { AccountGroupName } from '@/theme/semantics'

export type UiConfigStatus = 'idle' | 'loading' | 'ready' | 'error'

/** Shown for an empty deployment_label: the server named nothing. */
export const NO_DEPLOYMENT_LABEL = 'Unlabelled'
/** Shown for an empty server_version. */
export const UNKNOWN_VERSION = 'Unknown'

/** The UI's own version, from package.json at build time (vite.config.ts define). */
export const UI_VERSION: string = typeof __UI_VERSION__ === 'string' && __UI_VERSION__ !== '' ? __UI_VERSION__ : ''

// The generated enum's members drop the ACCOUNT_GROUP_ prefix (AccountGroup.SIMULATION), so map the
// values explicitly to the wire names semantics.ts keys on. UNSPECIFIED, and any value a newer
// server adds that this build does not know, maps to nothing and is not shown as allowed.
const GROUP_NAMES: Partial<Record<AccountGroup, AccountGroupName>> = {
  [AccountGroup.SIMULATION]: 'ACCOUNT_GROUP_SIMULATION',
  [AccountGroup.PAPER]: 'ACCOUNT_GROUP_PAPER',
  [AccountGroup.LIVE]: 'ACCOUNT_GROUP_LIVE',
}

function groupName(value: AccountGroup): AccountGroupName | null {
  return GROUP_NAMES[value] ?? null
}

export const useUiConfigStore = defineStore('uiConfig', () => {
  const status = ref<UiConfigStatus>('idle')
  const allowedGroups = ref<AccountGroupName[]>([])
  const deploymentLabel = ref('')
  const serverVersion = ref('')
  let inflight: AbortController | null = null

  /** Fetch the config. Safe to call again (a retry); an in-flight call is cancelled. */
  async function load(): Promise<void> {
    inflight?.abort()
    const controller = new AbortController()
    inflight = controller
    status.value = 'loading'
    try {
      const config = await getUiConfig(controller.signal)
      if (controller.signal.aborted) return
      allowedGroups.value = config.allowedGroups.flatMap((g) => {
        const name = groupName(g)
        return name === null ? [] : [name]
      })
      deploymentLabel.value = config.deploymentLabel.trim()
      serverVersion.value = config.serverVersion.trim()
      status.value = 'ready'
    } catch {
      if (controller.signal.aborted) return
      // Failing closed: with no config nothing is allowed, so every group renders greyed.
      allowedGroups.value = []
      status.value = 'error'
    }
  }

  /** The tag text: the label, or an explicit "none set" state, never a made-up name. */
  const deploymentTag = computed(() => (deploymentLabel.value === '' ? NO_DEPLOYMENT_LABEL : deploymentLabel.value))
  const hasDeploymentLabel = computed(() => deploymentLabel.value !== '')
  const serverVersionText = computed(() => (serverVersion.value === '' ? UNKNOWN_VERSION : serverVersion.value))
  const uiVersionText = computed(() => (UI_VERSION === '' ? UNKNOWN_VERSION : UI_VERSION))

  return {
    status,
    allowedGroups,
    deploymentLabel,
    serverVersion,
    deploymentTag,
    hasDeploymentLabel,
    serverVersionText,
    uiVersionText,
    load,
  }
})
