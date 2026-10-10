// The formatters bound to the settings store's zone. Components call this instead of passing the
// zone around; the functions read the zone at call time, so a template that calls them re-renders
// when the zone changes.
import { storeToRefs } from 'pinia'

import { useSettingsStore } from '@/stores/settings'
import { chartLocalization, chartTimeScale } from './chart'
import { formatDate, formatDateTime, formatTime, type Instant } from './formatters'

export function useFormatters() {
  const { timeZone } = storeToRefs(useSettingsStore())
  return {
    /** The active zone: the override, else the browser zone. */
    timeZone,
    date: (instant: Instant): string => formatDate(instant, timeZone.value),
    time: (instant: Instant, options: { seconds?: boolean } = {}): string =>
      formatTime(instant, timeZone.value, options),
    dateTime: (instant: Instant, options: { seconds?: boolean } = {}): string =>
      formatDateTime(instant, timeZone.value, options),
    /** Lightweight Charts `localization` option in the active zone. */
    chartLocalization: () => chartLocalization(timeZone.value),
    /** Lightweight Charts `timeScale` option in the active zone. */
    chartTimeScale: () => chartTimeScale(timeZone.value),
  }
}
