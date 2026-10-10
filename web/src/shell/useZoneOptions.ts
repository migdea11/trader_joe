// The time zones offered by the top bar's zone control and the Settings panel: every zone the
// browser knows, plus UTC, the browser's own zone and the current one, which Intl may omit.
import { computed } from 'vue'

import { browserTimeZone, useSettingsStore } from '@/stores/settings'

export function useZoneOptions() {
  const settings = useSettingsStore()
  const browserZone = browserTimeZone()

  const zoneOptions = computed(() => {
    let known: string[]
    try {
      known = Intl.supportedValuesOf('timeZone')
    } catch {
      known = []
    }
    return [...new Set(['UTC', browserZone, ...known, settings.timeZone])].sort()
  })

  return { zoneOptions, browserZone }
}
