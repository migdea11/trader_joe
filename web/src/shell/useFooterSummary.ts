// The footer status line's left part, owned by the screen: "9 of 38 datasets · sorted by symbol".
// A screen sets it while mounted (setFooterSummary(text), and null on unmount); the shell renders it
// beside the current time and zone. Module-level ref: one shell per page.
import { ref } from 'vue'

const footerSummary = ref<string | null>(null)

export function useFooterSummary() {
  return {
    footerSummary,
    setFooterSummary: (text: string | null): void => {
      footerSummary.value = text
    },
  }
}
