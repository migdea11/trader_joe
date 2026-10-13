// Reload on reconnect: the shell side of the events client (api/events.ts).
//
// While the events socket is down a screen can miss events, and an event is only a hint to refetch.
// When the socket re-opens the client emits one `reload` signal (never on the first open, where
// screens have just fetched on mount). The shell turns that signal into the SAME thing a user
// action does: every mounted screen that registered here refetches over REST. It is NOT a page
// reload, and a screen keeps its own filters, selection and scroll.
//
// A data screen (Datasets, Viewer, Dataset detail) calls `useEventsReload(refetch)` in its setup. The
// handler is registered while the screen is mounted and dropped with it, so only the active screen
// refetches, once per reconnect. `useEventsConnection()` is the shell's one call: it opens the
// shared socket (the client is lazy) and fans each reconnect out to the registered screens.
import { onScopeDispose } from 'vue'

import { onEventsReload } from '@/api/events'

/** What a screen registers: refetch the screen's data. May be async; a rejection is logged, not thrown. */
export type EventsReloadHandler = () => void | Promise<void>

const screens = new Set<EventsReloadHandler>()

function runScreen(handler: EventsReloadHandler): void {
  // One failing screen must not starve another, and a rejected refetch must not go unhandled.
  try {
    void Promise.resolve(handler()).catch((error: unknown) => {
      console.error('events reload: a screen refetch failed', error)
    })
  } catch (error) {
    console.error('events reload: a screen refetch threw', error)
  }
}

/** Run every registered screen's refetch now. The shell calls it on a reconnect. */
export function reloadScreens(): void {
  for (const handler of [...screens]) runScreen(handler)
}

/** Register a refetch outside a component; returns the unregister. Prefer `useEventsReload`. */
export function registerEventsReload(handler: EventsReloadHandler): () => void {
  screens.add(handler)
  return () => {
    screens.delete(handler)
  }
}

/**
 * For a screen's setup: refetch with `handler` after the events socket reconnects, for as long as
 * the screen is mounted. `handler` should be the same function the screen runs on mount.
 */
export function useEventsReload(handler: EventsReloadHandler): void {
  onScopeDispose(registerEventsReload(handler))
}

/**
 * For the shell's setup, once: opens the shared socket and reloads the registered screens on every
 * reconnect. The indicator reads the state itself (EventsIndicator.vue).
 */
export function useEventsConnection(): void {
  onScopeDispose(onEventsReload(reloadScreens))
}
