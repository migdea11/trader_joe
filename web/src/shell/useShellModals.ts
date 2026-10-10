// Open state for the shell's two modals, shared so any component can open them: the Settings panel
// (time zone control, Settings button) and the About and Credits modal (footer link, Settings panel).
// Module-level refs: one shell per page, and nothing persists.
import { ref } from 'vue'

const settingsOpen = ref(false)
const aboutOpen = ref(false)

export function useShellModals() {
  return {
    settingsOpen,
    aboutOpen,
    openSettings: (): void => {
      settingsOpen.value = true
    },
    closeSettings: (): void => {
      settingsOpen.value = false
    },
    /** The About and Credits entry point. tj-grna9p.54 fills AboutContent.vue; this hook stays. */
    openAbout: (): void => {
      aboutOpen.value = true
    },
    closeAbout: (): void => {
      aboutOpen.value = false
    },
  }
}
