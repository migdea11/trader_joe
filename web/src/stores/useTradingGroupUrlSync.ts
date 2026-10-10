// Keeps the trading-group selection and the URL query `group` in step, both ways. Call once, from
// the shell. The router guard (router/index.ts) carries `group` across navigations; this writes the
// effective selection back with router.replace, so a disallowed or missing value in the URL is
// corrected to the group in effect once the config has loaded.
import { watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { GROUP_QUERY_PARAM, useTradingGroupStore } from './tradingGroup'

export function useTradingGroupUrlSync(): void {
  const route = useRoute()
  const router = useRouter()
  const store = useTradingGroupStore()

  watch(
    () => route.query[GROUP_QUERY_PARAM],
    (value) => store.setFromQuery(value),
    { immediate: true },
  )

  watch(
    () => [store.selected, route.query[GROUP_QUERY_PARAM]] as const,
    ([selected, inUrl]) => {
      if (selected === null || inUrl === selected) return
      void router.replace({ query: { ...route.query, [GROUP_QUERY_PARAM]: selected } })
    },
    { immediate: true },
  )
}
