import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { create, toJsonString } from '@bufbuild/protobuf'
import type { MessageInitShape } from '@bufbuild/protobuf'
import { timestampFromMs } from '@bufbuild/protobuf/wkt'

import { IngestRunState, RunTrigger } from '@generated/trader_joe/proto/ui/v1/data_pb'
import { DatasetChange, UiEventSchema } from '@generated/trader_joe/proto/ui/v1/events_pb'
import type { UiEvent } from '@generated/trader_joe/proto/ui/v1/events_pb'

import { API_BASE } from './client'
import { createEventsClient, EVENTS_PATH } from './events'
import type { EventsClient, EventsClientOptions } from './events'

// tj-grna9p.75: the events client. A mock socket stands in for the browser's WebSocket through the
// client's createSocket option; it is an EventTarget, so the client's addEventListener calls are the
// real ones and the spec fires open, message and close the way a browser would. Frames are built with
// create(UiEventSchema) and toJsonString, the canonical JSON the server's to_proto_json sends, never
// hand-written. Timers: only setTimeout and clearTimeout are faked (the client must not use anything
// else; setInterval is spied and must never run).
//
// WHAT THE MOCKS HIDE: the browser's own socket (handshake, the Caddy and Vite proxies' upgrade), and
// the server's frames over a real connection. None of that is exercised here.

class MockSocket extends EventTarget {
  readonly url: string
  readonly close = vi.fn()
  readonly send = vi.fn()

  constructor(url: string) {
    super()
    this.url = url
  }

  open(): void {
    this.dispatchEvent(new Event('open'))
  }

  frame(data: unknown): void {
    this.dispatchEvent(new MessageEvent('message', { data }))
  }

  error(): void {
    this.dispatchEvent(new Event('error'))
  }

  drop(code = 1006): void {
    this.dispatchEvent(new CloseEvent('close', { code }))
  }
}

let sockets: MockSocket[]
let consoleError: ReturnType<typeof vi.spyOn>

function client(options: EventsClientOptions = {}): EventsClient {
  return createEventsClient({
    url: () => 'ws://test/events',
    createSocket: (url) => {
      const socket = new MockSocket(url)
      sockets.push(socket)
      return socket as unknown as WebSocket
    },
    ...options,
  })
}

const last = (): MockSocket => sockets[sockets.length - 1]!

function frame(init: MessageInitShape<typeof UiEventSchema>): string {
  return toJsonString(UiEventSchema, create(UiEventSchema, init))
}

const SERVER_TIME = Date.UTC(2026, 9, 12, 14, 30)

const RUN_FRAME = frame({
  serverTime: timestampFromMs(SERVER_TIME),
  event: {
    case: 'ingestRunChanged',
    value: {
      run: {
        id: 'run-1',
        datasetId: 'ds-1',
        trigger: RunTrigger.REQUEST,
        state: IngestRunState.SUCCEEDED,
        // Beyond 2**53: a JSON number would round it; int64 arrives as a string and decodes to bigint.
        barsWritten: 2n ** 60n + 1n,
      },
    },
  },
})

const DATASET_FRAME = frame({ event: { case: 'datasetChanged', value: { id: 'ds-2', change: DatasetChange.DELETED } } })

beforeEach(() => {
  sockets = []
  consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('the socket', () => {
  it('opens nothing until the first subscriber, then ONE socket shared by every subscriber', () => {
    const events = client()
    expect(sockets).toHaveLength(0)
    events.subscribe('datasetChanged', vi.fn())
    expect(sockets).toHaveLength(1)
    events.subscribe('ingestRunChanged', vi.fn())
    events.subscribe('datasetChanged', vi.fn())
    events.onReload(vi.fn())
    expect(sockets).toHaveLength(1)
    expect(last().url).toBe('ws://test/events')
  })

  it('a reload handler alone also opens it', () => {
    const events = client()
    events.onReload(vi.fn())
    expect(sockets).toHaveLength(1)
  })

  it('is kept for the app life: never closed, even when every subscriber has left', () => {
    const events = client()
    const offs = [events.subscribe('datasetChanged', vi.fn()), events.onReload(vi.fn())]
    last().open()
    offs.forEach((off) => off())
    vi.advanceTimersByTime(120_000)
    expect(last().close).not.toHaveBeenCalled()
    expect(sockets).toHaveLength(1)
    // Nothing is ever sent: the socket is receive-only.
    expect(last().send).not.toHaveBeenCalled()
  })

  it('the default URL is same-origin: ws: under http, wss: under https, at API_BASE + EVENTS_PATH', () => {
    expect(EVENTS_PATH).toBe('/ui/v1/events')
    const urls: string[] = []
    const createSocket = (url: string) => {
      urls.push(url)
      return new MockSocket(url) as unknown as WebSocket
    }
    vi.stubGlobal('location', { protocol: 'http:', host: 'trader.local:8080' })
    createEventsClient({ createSocket }).subscribe('datasetChanged', vi.fn())
    vi.stubGlobal('location', { protocol: 'https:', host: 'trader.example' })
    createEventsClient({ createSocket }).subscribe('datasetChanged', vi.fn())
    expect(urls).toEqual([
      `ws://trader.local:8080${API_BASE}/ui/v1/events`,
      `wss://trader.example${API_BASE}/ui/v1/events`,
    ])
    expect(urls[0]).toBe('ws://trader.local:8080/api/store/ui/v1/events')
  })

  it('the shared client is lazy: importing the module opens no socket; the first subscriber opens one', async () => {
    const constructed: string[] = []
    vi.stubGlobal(
      'WebSocket',
      class {
        constructor(url: string) {
          constructed.push(url)
        }
        addEventListener(): void {}
      },
    )
    vi.resetModules()
    const shared = await import('./events')
    expect(constructed).toEqual([])
    expect(shared.eventsState.value).toBe('connecting')
    shared.subscribeEvent('datasetChanged', vi.fn())
    shared.onEventsReload(vi.fn())
    expect(constructed).toEqual([`ws://${location.host}/api/store/ui/v1/events`])
  })
})

describe('typed delivery', () => {
  it('an IngestRunChanged frame reaches ingestRunChanged subscribers typed, bigint intact, and no datasetChanged one', () => {
    const events = client()
    const runs = vi.fn()
    const datasets = vi.fn()
    events.subscribe('ingestRunChanged', runs)
    events.subscribe('datasetChanged', datasets)
    last().open()
    last().frame(RUN_FRAME)
    expect(datasets).not.toHaveBeenCalled()
    expect(runs).toHaveBeenCalledTimes(1)
    const [message, event] = runs.mock.calls[0]! as [UiEvent['event']['value'] & { run: { barsWritten: bigint } }, UiEvent]
    expect(message.$typeName).toBe('trader_joe.proto.ui.v1.IngestRunChanged')
    expect(message.run.barsWritten).toBe(2n ** 60n + 1n)
    expect(typeof message.run.barsWritten).toBe('bigint')
    expect(message).toMatchObject({ run: { id: 'run-1', datasetId: 'ds-1', state: IngestRunState.SUCCEEDED, trigger: RunTrigger.REQUEST } })
    // The second argument is the whole UiEvent, server time included.
    expect(event.$typeName).toBe('trader_joe.proto.ui.v1.UiEvent')
    expect(Number(event.serverTime!.seconds) * 1000).toBe(SERVER_TIME)
  })

  it('a DatasetChanged frame reaches datasetChanged subscribers with its id and change', () => {
    const events = client()
    const datasets = vi.fn()
    const runs = vi.fn()
    events.subscribe('datasetChanged', datasets)
    events.subscribe('ingestRunChanged', runs)
    last().frame(DATASET_FRAME)
    expect(runs).not.toHaveBeenCalled()
    expect(datasets.mock.calls[0]![0]).toMatchObject({ id: 'ds-2', change: DatasetChange.DELETED })
  })

  it('requestChanged and reaperPendingChanged are accepted kinds with no other consumer', () => {
    const events = client()
    const requests = vi.fn()
    const reaper = vi.fn()
    events.subscribe('requestChanged', requests)
    events.subscribe('reaperPendingChanged', reaper)
    last().frame(frame({ event: { case: 'requestChanged', value: { entry: {} } } }))
    last().frame(frame({ event: { case: 'reaperPendingChanged', value: {} } }))
    expect(requests).toHaveBeenCalledTimes(1)
    expect(reaper).toHaveBeenCalledTimes(1)
  })

  it('every subscriber of a kind receives the event; an unsubscribed one does not', () => {
    const events = client()
    const first = vi.fn()
    const second = vi.fn()
    const off = events.subscribe('ingestRunChanged', first)
    events.subscribe('ingestRunChanged', second)
    last().frame(RUN_FRAME)
    off()
    last().frame(RUN_FRAME)
    expect(first).toHaveBeenCalledTimes(1)
    expect(second).toHaveBeenCalledTimes(2)
  })

  it('a subscriber may unsubscribe itself while being called without skipping the others', () => {
    const events = client()
    const after = vi.fn()
    const off: () => void = events.subscribe('ingestRunChanged', () => off())
    events.subscribe('ingestRunChanged', after)
    last().frame(RUN_FRAME)
    last().frame(RUN_FRAME)
    expect(after).toHaveBeenCalledTimes(2)
  })

  it('unknown fields from a newer server are ignored, at the top and inside the message', () => {
    const events = client()
    const datasets = vi.fn()
    events.subscribe('datasetChanged', datasets)
    const json = JSON.parse(DATASET_FRAME) as Record<string, Record<string, unknown>>
    json.addedLater = { x: 1 } as never
    json.datasetChanged!.alsoNew = 'y'
    last().frame(JSON.stringify(json))
    expect(datasets).toHaveBeenCalledTimes(1)
    expect(datasets.mock.calls[0]![0]).toMatchObject({ id: 'ds-2' })
    expect(consoleError).not.toHaveBeenCalled()
  })

  it('an event with no kind set (an unknown oneof member from a newer server) reaches no subscriber', () => {
    const events = client()
    const datasets = vi.fn()
    events.subscribe('datasetChanged', datasets)
    last().frame(JSON.stringify({ serverTime: '2026-10-12T14:30:00Z', settingsChanged: {} }))
    last().frame('{}')
    expect(datasets).not.toHaveBeenCalled()
  })
})

describe('a bad frame or a bad subscriber', () => {
  it.each([
    ['not JSON', 'not json at all'],
    ['the wrong JSON shape', JSON.stringify({ datasetChanged: 'not an object' })],
    ['a JSON array', '[]'],
    ['a binary frame', new ArrayBuffer(4)],
  ])('%s is logged and dropped: no subscriber, the socket stays open, the next frame arrives', (_name, data) => {
    const events = client()
    const datasets = vi.fn()
    events.subscribe('datasetChanged', datasets)
    last().open()
    expect(() => last().frame(data)).not.toThrow()
    expect(datasets).not.toHaveBeenCalled()
    expect(consoleError).toHaveBeenCalledTimes(1)
    expect(String(consoleError.mock.calls[0]![0])).toContain('did not decode')
    expect(last().close).not.toHaveBeenCalled()
    expect(events.state.value).toBe('open')
    last().frame(DATASET_FRAME)
    expect(datasets).toHaveBeenCalledTimes(1)
    expect(sockets).toHaveLength(1)
  })

  it('a subscriber that throws is logged and does not keep the event from the others', () => {
    const events = client()
    const before = vi.fn()
    const after = vi.fn()
    events.subscribe('ingestRunChanged', before)
    events.subscribe('ingestRunChanged', () => {
      throw new Error('boom')
    })
    events.subscribe('ingestRunChanged', after)
    last().open()
    expect(() => last().frame(RUN_FRAME)).not.toThrow()
    expect(before).toHaveBeenCalledTimes(1)
    expect(after).toHaveBeenCalledTimes(1)
    expect(consoleError).toHaveBeenCalledTimes(1)
    expect(last().close).not.toHaveBeenCalled()
    expect(events.state.value).toBe('open')
  })

  it('a reload handler that throws does not keep the reload from the others', () => {
    const events = client()
    const after = vi.fn()
    events.onReload(() => {
      throw new Error('boom')
    })
    events.onReload(after)
    last().open()
    last().drop()
    vi.advanceTimersByTime(1_000)
    last().open()
    expect(after).toHaveBeenCalledTimes(1)
    expect(consoleError).toHaveBeenCalledTimes(1)
  })
})

describe('reconnect and reload', () => {
  /** The delays between a close and the next socket, for `drops` consecutive drops that never open. */
  function delaysFor(events: EventsClient, drops: number): number[] {
    const delays: number[] = []
    for (let i = 0; i < drops; i += 1) {
      const count = sockets.length
      last().drop()
      expect(events.state.value).toBe('reconnecting')
      let waited = 0
      while (sockets.length === count) {
        vi.advanceTimersByTime(250)
        waited += 250
        if (waited > 120_000) throw new Error('never reconnected')
      }
      delays.push(waited)
    }
    return delays
  }

  it('state: connecting before the first open, open, reconnecting after a drop, open again', () => {
    const events = client()
    expect(events.state.value).toBe('connecting')
    events.subscribe('datasetChanged', vi.fn())
    expect(events.state.value).toBe('connecting')
    last().open()
    expect(events.state.value).toBe('open')
    last().drop()
    expect(events.state.value).toBe('reconnecting')
    vi.advanceTimersByTime(1_000)
    // A new socket is on its way but not open: still reconnecting, never back to connecting.
    expect(events.state.value).toBe('reconnecting')
    last().open()
    expect(events.state.value).toBe('open')
  })

  it('the state ref is read-only to consumers', () => {
    const events = client()
    ;(events.state as { value: string }).value = 'open'
    expect(events.state.value).toBe('connecting')
  })

  it('backs off 1s, doubling per failure, to a 30s cap; the first wait is 1s', () => {
    const events = client()
    events.subscribe('datasetChanged', vi.fn())
    last().open()
    expect(delaysFor(events, 8)).toEqual([1_000, 2_000, 4_000, 8_000, 16_000, 30_000, 30_000, 30_000])
  })

  it('no retry fires before its delay', () => {
    const events = client()
    events.subscribe('datasetChanged', vi.fn())
    last().drop()
    vi.advanceTimersByTime(999)
    expect(sockets).toHaveLength(1)
    vi.advanceTimersByTime(1)
    expect(sockets).toHaveLength(2)
  })

  it('a successful open resets the backoff to 1s', () => {
    const events = client()
    events.subscribe('datasetChanged', vi.fn())
    last().open()
    expect(delaysFor(events, 4)).toEqual([1_000, 2_000, 4_000, 8_000])
    last().open()
    expect(delaysFor(events, 2)).toEqual([1_000, 2_000])
  })

  it('retries only on close: an error alone schedules nothing (a close always follows it)', () => {
    const events = client()
    events.subscribe('datasetChanged', vi.fn())
    last().open()
    last().error()
    vi.advanceTimersByTime(60_000)
    expect(sockets).toHaveLength(1)
    last().drop()
    vi.advanceTimersByTime(1_000)
    expect(sockets).toHaveLength(2)
  })

  it('a 1013 close (the server dropping a slow client) reconnects like any other', () => {
    const events = client()
    events.subscribe('datasetChanged', vi.fn())
    last().open()
    last().drop(1013)
    expect(events.state.value).toBe('reconnecting')
    vi.advanceTimersByTime(1_000)
    expect(sockets).toHaveLength(2)
  })

  it('a socket that cannot even be constructed is retried with the same backoff', () => {
    let attempts = 0
    const events = createEventsClient({
      url: () => 'ws://test/events',
      createSocket: (url) => {
        attempts += 1
        if (attempts <= 2) throw new SyntaxError('bad url')
        const socket = new MockSocket(url)
        sockets.push(socket)
        return socket as unknown as WebSocket
      },
    })
    events.subscribe('datasetChanged', vi.fn())
    expect(events.state.value).toBe('reconnecting')
    vi.advanceTimersByTime(999)
    expect(attempts).toBe(1)
    vi.advanceTimersByTime(1)
    expect(attempts).toBe(2)
    vi.advanceTimersByTime(2_000)
    expect(attempts).toBe(3)
    expect(sockets).toHaveLength(1)
  })

  it('reload fires exactly once per successful RE-open, and never on the first open', () => {
    const events = client()
    const reload = vi.fn()
    events.onReload(reload)
    last().open()
    expect(reload).not.toHaveBeenCalled()

    last().drop()
    vi.advanceTimersByTime(1_000)
    expect(reload).not.toHaveBeenCalled()
    last().open()
    expect(reload).toHaveBeenCalledTimes(1)

    // Two failed attempts in a row, then an open: still one reload for that reconnect.
    last().drop()
    vi.advanceTimersByTime(1_000)
    last().drop()
    vi.advanceTimersByTime(2_000)
    last().drop()
    vi.advanceTimersByTime(4_000)
    expect(reload).toHaveBeenCalledTimes(1)
    last().open()
    expect(reload).toHaveBeenCalledTimes(2)
  })

  it('a first connection that fails before ever opening emits no reload when it finally opens', () => {
    const events = client()
    const reload = vi.fn()
    events.onReload(reload)
    last().drop()
    vi.advanceTimersByTime(1_000)
    last().open()
    expect(reload).not.toHaveBeenCalled()
  })

  it('an unsubscribed reload handler is not called', () => {
    const events = client()
    const reload = vi.fn()
    const off = events.onReload(reload)
    events.subscribe('datasetChanged', vi.fn())
    last().open()
    off()
    last().drop()
    vi.advanceTimersByTime(1_000)
    last().open()
    expect(reload).not.toHaveBeenCalled()
  })

  it('a replaced socket is ignored: its late frames, opens and closes change nothing', () => {
    const events = client()
    const datasets = vi.fn()
    const reload = vi.fn()
    events.subscribe('datasetChanged', datasets)
    events.onReload(reload)
    const first = last()
    first.open()
    first.drop()
    vi.advanceTimersByTime(1_000)
    const second = last()
    second.open()
    expect(reload).toHaveBeenCalledTimes(1)
    first.frame(DATASET_FRAME)
    first.open()
    first.drop()
    expect(datasets).not.toHaveBeenCalled()
    expect(reload).toHaveBeenCalledTimes(1)
    expect(events.state.value).toBe('open')
    vi.advanceTimersByTime(60_000)
    expect(sockets).toHaveLength(2)
  })

  it('subscriptions survive a reconnect: the new socket delivers to the same subscribers', () => {
    const events = client()
    const datasets = vi.fn()
    events.subscribe('datasetChanged', datasets)
    last().open()
    last().drop()
    vi.advanceTimersByTime(1_000)
    last().open()
    last().frame(DATASET_FRAME)
    expect(datasets).toHaveBeenCalledTimes(1)
  })

  it('never polls: no setInterval across connect, drops, backoff and reconnects', () => {
    const interval = vi.spyOn(globalThis, 'setInterval')
    const events = client()
    events.subscribe('datasetChanged', vi.fn())
    events.onReload(vi.fn())
    last().open()
    delaysFor(events, 7)
    last().open()
    expect(interval).not.toHaveBeenCalled()
  })
})
