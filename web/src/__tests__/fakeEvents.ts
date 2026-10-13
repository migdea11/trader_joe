// Test fixture, not a spec (tj-grna9p.80): stands in for the app's ONE shared events socket
// (src/api/events.ts) so a spec that mounts the shell or a live screen never opens a real WebSocket.
//
// A spec installs it with:
//
//   vi.mock('@/api/events', async (importOriginal) =>
//     (await import('@/__tests__/fakeEvents')).fakeEventsModule(await importOriginal()),
//   )
//
// and drives it through `fakeEvents`: `emit` delivers a UiEvent to the subscribers of its kind,
// `reconnect` is the client's reload signal after a re-open, and `state` is the connection state the
// shell's indicator reads. `createEventsClient` stays the real one (src/api/events.spec.ts tests it
// over a mock socket); only the shared instance's three exports are replaced. '@/api' re-exports
// from './events', which resolves to the same mocked module.
import { create } from '@bufbuild/protobuf'
import type { MessageInitShape } from '@bufbuild/protobuf'
import { readonly, ref } from 'vue'

import { UiEventSchema } from '@generated/trader_joe/proto/ui/v1/events_pb'
import type { UiEvent } from '@generated/trader_joe/proto/ui/v1/events_pb'

import type { EventsClient, EventsConnectionState, UiEventKind } from '@/api/events'

type Handler = (message: unknown, event: UiEvent) => void

const state = ref<EventsConnectionState>('connecting')
const byKind = new Map<UiEventKind, Set<Handler>>()
const reloaders = new Set<() => void>()

const subscribe = ((kind: UiEventKind, handler: Handler) => {
  let handlers = byKind.get(kind)
  if (handlers === undefined) {
    handlers = new Set()
    byKind.set(kind, handlers)
  }
  handlers.add(handler)
  return () => {
    handlers.delete(handler)
  }
}) as unknown as EventsClient['subscribe']

const onReload: EventsClient['onReload'] = (handler) => {
  reloaders.add(handler)
  return () => {
    reloaders.delete(handler)
  }
}

export const fakeEvents = {
  state,
  /** Deliver one event to the subscribers of its kind, as the real client does after decoding a frame. */
  emit(init: MessageInitShape<typeof UiEventSchema>): void {
    const event = create(UiEventSchema, init)
    const kind = event.event.case
    if (kind === undefined) return
    for (const handler of [...(byKind.get(kind) ?? [])]) handler(event.event.value, event)
  },
  /** The socket re-opened: state goes open and every reload handler runs once. */
  reconnect(): void {
    state.value = 'open'
    for (const handler of [...reloaders]) handler()
  },
  /** How many handlers are subscribed to `kind` (zero after the subscriber's scope is disposed). */
  subscribers(kind: UiEventKind): number {
    return byKind.get(kind)?.size ?? 0
  },
  /** How many reload handlers are registered with the client (the shell registers one). */
  reloadHandlers(): number {
    return reloaders.size
  },
  reset(): void {
    state.value = 'connecting'
    byKind.clear()
    reloaders.clear()
  },
}

/** The mocked '@/api/events' module: the real one with the shared instance's exports replaced. */
export function fakeEventsModule<T extends object>(actual: T): T {
  return {
    ...actual,
    subscribeEvent: subscribe,
    onEventsReload: onReload,
    eventsState: readonly(state),
  }
}
