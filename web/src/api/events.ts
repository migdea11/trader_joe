// The events client: ONE shared socket to the store's live-event endpoint (ADR tj-grna9p.5).
//
// Each text frame is protobuf canonical JSON of UiEvent, decoded with fromJson like every other wire
// shape. An event is a hint to refetch over REST, never the only copy of anything, so this client
// holds no state beyond the socket: it delivers typed messages to subscribers, and emits a `reload`
// signal after every RE-open so a screen refetches what it may have missed while the socket was
// down. The first open emits none: screens fetch on mount, and a reload then would double every fetch.
//
// The socket opens lazily with the first subscriber and is kept for the app's life. A reconnect
// backs off with setTimeout (setInterval is banned in web/src).

import { fromJson } from '@bufbuild/protobuf'
import { UiEventSchema } from '@generated/trader_joe/proto/ui/v1/events_pb'
import type { UiEvent } from '@generated/trader_joe/proto/ui/v1/events_pb'
import { readonly, ref } from 'vue'
import type { Ref } from 'vue'

import { API_BASE } from './client'

/** The path under API_BASE of the store's events socket. */
export const EVENTS_PATH = '/ui/v1/events'

/** Where the socket is: not yet open, open, or down and waiting to retry. */
export type EventsConnectionState = 'connecting' | 'open' | 'reconnecting'

/** A UiEvent's oneof cases: the kinds a subscriber can ask for. */
export type UiEventKind = NonNullable<UiEvent['event']['case']>

/** The message a subscriber to `K` receives: the oneof member's value. */
export type UiEventMessage<K extends UiEventKind> = Extract<UiEvent['event'], { case: K }>['value']

export type UiEventHandler<K extends UiEventKind> = (message: UiEventMessage<K>, event: UiEvent) => void

/** The socket constructor the client needs; a test passes a mock. */
export type WebSocketFactory = (url: string) => WebSocket

export interface EventsClientOptions {
  /** The socket URL; default: same-origin API_BASE + EVENTS_PATH, ws: or wss: from the page. */
  url?: () => string
  /** Default: the browser's WebSocket. */
  createSocket?: WebSocketFactory
  /** First reconnect delay in ms; doubles per consecutive failure up to `maxDelayMs`. */
  initialDelayMs?: number
  maxDelayMs?: number
}

export interface EventsClient {
  /** Deliver each `kind` event to `handler`. Opens the socket if it is the first subscriber. Returns the unsubscribe. */
  subscribe: <K extends UiEventKind>(kind: K, handler: UiEventHandler<K>) => () => void
  /** Call `handler` once per successful re-open, never on the first. Opens the socket if needed. Returns the unsubscribe. */
  onReload: (handler: () => void) => () => void
  /** Connecting before the first open, open, or reconnecting after a drop. */
  state: Readonly<Ref<EventsConnectionState>>
}

const INITIAL_DELAY_MS = 1_000
const MAX_DELAY_MS = 30_000

function defaultUrl(): string {
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${scheme}//${location.host}${API_BASE}${EVENTS_PATH}`
}

type AnyHandler = (message: never, event: UiEvent) => void

/** Build an events client. The app uses the shared one below; a test builds its own over a mock socket. */
export function createEventsClient(options: EventsClientOptions = {}): EventsClient {
  const url = options.url ?? defaultUrl
  const createSocket = options.createSocket ?? ((target: string) => new WebSocket(target))
  const initialDelay = options.initialDelayMs ?? INITIAL_DELAY_MS
  const maxDelay = options.maxDelayMs ?? MAX_DELAY_MS

  const state = ref<EventsConnectionState>('connecting')
  const byKind = new Map<UiEventKind, Set<AnyHandler>>()
  const reloaders = new Set<() => void>()
  let socket: WebSocket | undefined
  let retryTimer: ReturnType<typeof setTimeout> | undefined
  let failures = 0
  let hasOpened = false
  let started = false

  function guarded(call: () => void): void {
    // One failing subscriber must not starve the rest, nor close the socket.
    try {
      call()
    } catch (error) {
      console.error('events: a subscriber threw', error)
    }
  }

  function dispatch(data: unknown): void {
    let event: UiEvent
    try {
      if (typeof data !== 'string') throw new TypeError('not a text frame')
      event = fromJson(UiEventSchema, JSON.parse(data), { ignoreUnknownFields: true })
    } catch (error) {
      console.error('events: dropped a frame that did not decode', error)
      return
    }
    const kind = event.event.case
    if (kind === undefined) return
    // Copy: a handler may unsubscribe itself while we iterate.
    for (const handler of [...(byKind.get(kind) ?? [])]) {
      guarded(() => (handler as (message: unknown, event: UiEvent) => void)(event.event.value, event))
    }
  }

  function scheduleReconnect(): void {
    state.value = 'reconnecting'
    if (retryTimer !== undefined) return
    const delay = Math.min(initialDelay * 2 ** failures, maxDelay)
    failures += 1
    retryTimer = setTimeout(() => {
      retryTimer = undefined
      connect()
    }, delay)
  }

  function connect(): void {
    let current: WebSocket
    try {
      current = createSocket(url())
    } catch (error) {
      console.error('events: could not open the socket', error)
      scheduleReconnect()
      return
    }
    socket = current
    current.addEventListener('open', () => {
      if (socket !== current) return
      failures = 0
      state.value = 'open'
      const reconnected = hasOpened
      hasOpened = true
      if (reconnected) for (const handler of [...reloaders]) guarded(handler)
    })
    current.addEventListener('message', (message) => {
      if (socket === current) dispatch(message.data)
    })
    // An error is always followed by a close, so the close alone schedules the retry (including a
    // 1013 from a slow-client drop and a connect that never opened).
    current.addEventListener('close', () => {
      if (socket !== current) return
      socket = undefined
      scheduleReconnect()
    })
  }

  function ensureStarted(): void {
    if (started) return
    started = true
    connect()
  }

  return {
    subscribe(kind, handler) {
      let handlers = byKind.get(kind)
      if (handlers === undefined) {
        handlers = new Set()
        byKind.set(kind, handlers)
      }
      const stored = handler as unknown as AnyHandler
      handlers.add(stored)
      ensureStarted()
      return () => {
        handlers.delete(stored)
      }
    },
    onReload(handler) {
      reloaders.add(handler)
      ensureStarted()
      return () => {
        reloaders.delete(handler)
      }
    },
    state: readonly(state),
  }
}

const shared = createEventsClient()

/** Subscribe to one event kind on the app's shared socket. Returns the unsubscribe. */
export const subscribeEvent: EventsClient['subscribe'] = shared.subscribe

/** Call `handler` once per reconnect on the shared socket (not on the first open). Returns the unsubscribe. */
export const onEventsReload: EventsClient['onReload'] = shared.onReload

/** The shared socket's connection state, for the shell's indicator. */
export const eventsState: EventsClient['state'] = shared.state
