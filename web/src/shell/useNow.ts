// "Now" for the footer status line, with no timer: no polling anywhere in web/src (tj-grna9p.29). It
// is re-read when the route changes and when the tab becomes visible again, so it is the time of
// the last interaction rather than a ticking clock.
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

export function useNow() {
  const router = useRouter()
  const now = ref(Date.now())
  const refresh = (): void => {
    now.value = Date.now()
  }
  const onVisible = (): void => {
    if (document.visibilityState === 'visible') refresh()
  }
  const stopAfterEach = router.afterEach(refresh)
  onMounted(() => document.addEventListener('visibilitychange', onVisible))
  onBeforeUnmount(() => {
    stopAfterEach()
    document.removeEventListener('visibilitychange', onVisible)
  })
  return { now, refresh }
}
