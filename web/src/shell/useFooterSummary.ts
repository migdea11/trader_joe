// The footer status line's content, owned by the screen. The footer shows only what exists nowhere
// else on the screen: the screen's own summary text ("9 of 38 datasets · sorted by symbol"), when
// its data was last loaded (the page does not poll, so staleness is real information), and a problem when a request failed. A screen sets what it has
// while mounted and clears it on unmount; with nothing set the footer is not rendered at all.
// Module-level refs: one shell per page.
import { ref } from 'vue'

const footerSummary = ref<string | null>(null)
const footerUpdatedAt = ref<number | null>(null)
const footerProblem = ref<string | null>(null)

export function useFooterSummary() {
  return {
    footerSummary,
    footerUpdatedAt,
    footerProblem,
    setFooterSummary: (text: string | null): void => {
      footerSummary.value = text
    },
    /** When the data on screen was loaded (epoch ms). */
    setFooterUpdated: (at: number | null): void => {
      footerUpdatedAt.value = at
    },
    /** A failed request, in words; null when nothing is failing. */
    setFooterProblem: (text: string | null): void => {
      footerProblem.value = text
    },
  }
}
