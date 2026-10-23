// @vitest-environment node
// Node, not the suite's jsdom: this boots a real Vite dev server from web/vite.config.ts.
//
// The dev proxy edge end to end (bead tj-grna9p.134): a real Vite dev server, built from the real config
// file, in front of an in-process FAKE data_store that records every request it receives. It shows what
// devProxyEdge.spec.ts cannot from the exported pieces: that Vite stops on the bypass's answer (no
// request reaches the upstream, no SPA page follows), that the upgrade path answers a refused WebSocket
// 404, and that the header the upstream sees is the dev server's value and never the browser's.
//
// NEVER A REAL SECRET: INSTANCE_WRITE_SECRET is set to a fake value for the run and put back after; the
// dev server's logger output is captured and checked for that fake value.
import { createServer as createHttpServer, request, type IncomingHttpHeaders, type Server } from 'node:http'
import { connect, type AddressInfo } from 'node:net'
import { fileURLToPath } from 'node:url'
import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { createServer, type Logger, type ViteDevServer } from 'vite'

const WEB_ROOT = fileURLToPath(new URL('../../', import.meta.url))
const SECRET_ENV = 'INSTANCE_WRITE_SECRET'
const FAKE = 'dummy-not-a-secret'
const UUID = '0f8fad5b-d9cb-469f-a165-70867728950e'
const DATASETS = '/api/store/ui/v1/datasets'

interface Seen {
  method: string
  url: string
  headers: IncomingHttpHeaders
  upgrade: boolean
}

const seen: Seen[] = []
const logged: string[] = []
let upstream: Server
let vite: ViteDevServer
let port = 0
const ORIGINAL_SECRET = process.env[SECRET_ENV]
const ORIGINAL_TARGET = process.env.VITE_DEV_PROXY_TARGET

function capturingLogger(): Logger {
  const record = (msg: string) => void logged.push(msg)
  return {
    info: record,
    warn: record,
    warnOnce: record,
    error: (msg: string) => record(msg),
    clearScreen: () => {},
    hasErrorLogged: () => false,
    hasWarned: false,
  }
}

beforeAll(async () => {
  upstream = createHttpServer((req, res) => {
    seen.push({ method: req.method ?? '', url: req.url ?? '', headers: req.headers, upgrade: false })
    res.writeHead(200, { 'Content-Type': 'text/plain' }).end('from upstream')
  })
  upstream.on('upgrade', (req, socket) => {
    seen.push({ method: req.method ?? '', url: req.url ?? '', headers: req.headers, upgrade: true })
    socket.end('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n')
  })
  await new Promise<void>((resolve) => upstream.listen(0, '127.0.0.1', resolve))

  process.env.VITE_DEV_PROXY_TARGET = `http://127.0.0.1:${(upstream.address() as AddressInfo).port}`
  process.env[SECRET_ENV] = FAKE
  vite = await createServer({
    configFile: `${WEB_ROOT}vite.config.ts`,
    root: WEB_ROOT,
    customLogger: capturingLogger(),
    optimizeDeps: { noDiscovery: true, include: [] },
    server: { host: '127.0.0.1', port: 0, strictPort: false, hmr: false, watch: null },
  })
  await vite.listen()
  port = (vite.httpServer?.address() as AddressInfo).port
}, 60_000)

afterAll(async () => {
  await vite?.close()
  await new Promise<void>((resolve) => (upstream ? upstream.close(() => resolve()) : resolve()))
  if (ORIGINAL_SECRET === undefined) delete process.env[SECRET_ENV]
  else process.env[SECRET_ENV] = ORIGINAL_SECRET
  if (ORIGINAL_TARGET === undefined) delete process.env.VITE_DEV_PROXY_TARGET
  else process.env.VITE_DEV_PROXY_TARGET = ORIGINAL_TARGET
})

beforeEach(() => {
  seen.length = 0
  process.env[SECRET_ENV] = FAKE
})

interface Reply {
  status: number
  body: string
  upgraded: boolean
}

function send(method: string, path: string, headers: Record<string, string> = {}): Promise<Reply> {
  return new Promise((resolve, reject) => {
    const req = request({ host: '127.0.0.1', port, method, path, headers }, (res) => {
      let body = ''
      res.setEncoding('utf-8')
      res.on('data', (chunk: string) => (body += chunk))
      res.on('end', () => resolve({ status: res.statusCode ?? 0, body, upgraded: false }))
    })
    req.on('upgrade', (res, socket) => {
      socket.destroy()
      resolve({ status: res.statusCode ?? 0, body: '', upgraded: true })
    })
    req.on('error', reject)
    req.end()
  })
}

// A request written byte for byte on a socket: node's client refuses some request targets (a tab, a
// space) before they leave, so what the dev server's own parser does with them is only seen this way.
function rawSend(method: string, target: string, extra = ''): Promise<string> {
  return new Promise((resolve, reject) => {
    const socket = connect(port, '127.0.0.1', () => {
      socket.write(`${method} ${target} HTTP/1.1\r\nHost: 127.0.0.1:${port}\r\n${extra}Connection: close\r\n\r\n`)
    })
    let data = ''
    socket.setEncoding('latin1')
    socket.on('data', (chunk: string) => (data += chunk))
    socket.on('end', () => resolve(data))
    socket.on('close', () => resolve(data))
    socket.on('error', reject)
  })
}

const upgrade = (path: string, headers: Record<string, string> = {}) =>
  send('GET', path, { Connection: 'Upgrade', Upgrade: 'websocket', 'Sec-WebSocket-Version': '13', ...headers })

describe('a real Vite dev server in front of a fake data_store', () => {
  it.each([
    ['GET', '/api/store/ui/v1/config', '/ui/v1/config'],
    ['HEAD', '/api/store/ui/v1/config', '/ui/v1/config'],
    ['PATCH', `${DATASETS}/${UUID}/expiry`, `/ui/v1/datasets/${UUID}/expiry`],
    ['DELETE', `${DATASETS}/${UUID.toUpperCase()}`, `/ui/v1/datasets/${UUID.toUpperCase()}`],
    ['GET', `${DATASETS}/${UUID}/bars?start=1`, `/ui/v1/datasets/${UUID}/bars?start=1`],
  ])('forwards %s %s, stripped, with the dev server secret in place of the browser one', async (method, path, upstreamPath) => {
    const reply = await send(method, path, { 'x-instance-secret': 'browser-sent' })
    expect(reply.status).toBe(200)
    expect(seen).toHaveLength(1)
    expect(seen[0]).toMatchObject({ method, url: upstreamPath })
    expect(seen[0].headers['x-instance-secret']).toBe(FAKE)
  })

  it('forwards no header at all when the dev server value is empty, the browser one dropped', async () => {
    process.env[SECRET_ENV] = ''
    const reply = await send('PATCH', `${DATASETS}/${UUID}/expiry`, { 'X-Instance-Secret': 'browser-sent' })
    expect(reply.status).toBe(200)
    expect(seen).toHaveLength(1)
    expect(seen[0].headers).not.toHaveProperty('x-instance-secret')
  })

  it.each([
    ['PATCH', `${DATASETS}/${UUID}/bars`],
    ['POST', DATASETS],
    ['PUT', `${DATASETS}/${UUID}/expiry`],
    ['DELETE', `${DATASETS}/${UUID}/expiry`],
    ['PATCH', `${DATASETS}/not-a-uuid/expiry`],
    ['DELETE', `${DATASETS}/${UUID}/`],
    ['GET', `/api/store/store/datasets/${UUID}`],
    ['GET', '/api/store/store/x'],
    ['GET', '/api/store/internal/asset-data'],
  ])('answers %s %s 404 not found itself, data_store never asked', async (method, path) => {
    const reply = await send(method, path, { 'x-instance-secret': 'browser-sent' })
    expect(reply).toMatchObject({ status: 404, body: 'not found' })
    expect(seen).toEqual([])
  })

  // The one known divergence from Caddy (which answers 404): Vite's own CORS middleware runs before the
  // proxy and answers every OPTIONS 204 itself. data_store is still never asked. The UI is same-origin and
  // sends no preflight, so nothing it does differs; pinned so a change in either direction is seen.
  it('answers OPTIONS from Vite CORS (204, not Caddy 404), data_store never asked', async () => {
    const reply = await send('OPTIONS', `${DATASETS}/${UUID}`, { 'x-instance-secret': 'browser-sent' })
    expect(reply.status).toBe(204)
    expect(seen).toEqual([])
  })

  it.each([
    ['GET', '/api/store/ui/v1/../store/datasets'],
    ['DELETE', `${DATASETS}/${UUID}/..`],
    ['GET', '/api/store/ui/v1/a..b'],
  ])('answers %s %s 400 bad request, data_store never asked', async (method, path) => {
    const reply = await send(method, path)
    expect(reply).toMatchObject({ status: 400, body: 'bad request' })
    expect(seen).toEqual([])
  })

  it('upgrades the events socket through to data_store with the dev server secret', async () => {
    const reply = await upgrade('/api/store/ui/v1/events', { 'x-instance-secret': 'browser-sent' })
    expect(reply.upgraded).toBe(true)
    expect(seen).toHaveLength(1)
    expect(seen[0]).toMatchObject({ method: 'GET', url: '/ui/v1/events', upgrade: true })
    expect(seen[0].headers['x-instance-secret']).toBe(FAKE)
  })

  it.each(['/api/store/store/events', '/api/store/ui/v2/events', '/api/store/ui/v1/../events'])(
    'refuses an upgrade on %s with a bare 404, data_store never asked',
    async (path) => {
      const reply = await upgrade(path)
      expect(reply).toMatchObject({ status: 404, upgraded: false })
      expect(seen).toEqual([])
    },
  )

  // THE DECODED, CLEANED PATH (round 2 of tj-grna9p.134, 0c0543a). http-proxy resolves the url WHATWG's
  // way (%2e%2e is '..'), and Caddy decodes and cleans before it matches, so the dev edge decides on the
  // decoded path: each of these spellings is a 400 here and a bare 404 on an upgrade, and data_store never
  // sees the request or the secret. Round 1 showed the first three reaching /store, /internal and /docs.
  const UUID_UPPER = UUID.toUpperCase()
  const REFUSED_SPELLINGS: [string, string][] = [
    ['encoded-dots', '/api/store/ui/v1/%2e%2e/%2e%2e/store/datasets/x'],
    ['upper-encoded-dots', '/api/store/ui/v1/%2E%2E/%2E%2E/internal/asset-data'],
    ['half-encoded-dots', '/api/store/ui/v1/.%2e/.%2e/docs'],
    ['mixed-case-encoded-dots', '/api/store/ui/v1/%2E%2e/%2e%2E/store/datasets/x'],
    ['double-encoded-dots', '/api/store/ui/v1/%252e%252e/%252e%252e/store/datasets/x'],
    ['encoded-slash', `/api/store/ui/v1/datasets%2f${UUID}`],
    ['upper-encoded-slash', '/api/store/ui/v1/x%2F..%2F..%2Fstore'],
    ['encoded-backslash', '/api/store/ui/v1/config%5cx'],
    ['backslash', '/api/store/ui/v1/..\\..\\store/datasets/x'],
    ['backslash-no-dots', '/api/store/ui/v1/config\\x'],
    ['malformed-escape', '/api/store/ui/v1/%zz'],
    ['truncated-escape', '/api/store/ui/v1/config%2'],
    ['still-changing-after-three-decodes', '/api/store/ui/v1/%25252541'],
    ['doubled-slash', '/api/store/ui/v1//config'],
    ['dot-segment', '/api/store/ui/v1/./config'],
    ['encoded-letter', '/api/store/ui/v1/%63onfig'],
    ['encoded-uuid-digit', `${DATASETS}/%30${UUID.slice(1)}`],
  ]

  it.each(REFUSED_SPELLINGS)('answers GET %s 400 bad request, data_store never asked', async (_id, path) => {
    const reply = await send('GET', path, { 'x-instance-secret': 'browser-sent' })
    expect(reply).toMatchObject({ status: 400, body: 'bad request' })
    expect(seen).toEqual([])
  })

  it.each([
    ['PATCH', `${DATASETS}/${UUID}/%2e/expiry`],
    ['PATCH', `${DATASETS}/${UUID}//expiry`],
    ['DELETE', `${DATASETS}/%30${UUID.slice(1)}`],
    ['DELETE', `${DATASETS}/./${UUID}`],
  ])('answers a write %s %s 400, data_store never asked', async (method, path) => {
    const reply = await send(method, path, { 'x-instance-secret': 'browser-sent' })
    expect(reply).toMatchObject({ status: 400, body: 'bad request' })
    expect(seen).toEqual([])
  })

  it.each(REFUSED_SPELLINGS)('refuses an upgrade on %s with a bare 404, data_store never asked', async (_id, path) => {
    const reply = await upgrade(path.replace('store/datasets/x', 'store/events'))
    expect(reply).toMatchObject({ status: 404, upgraded: false })
    expect(seen).toEqual([])
  })

  it.each([
    ['PATCH', `${DATASETS}/${UUID_UPPER}/expiry`, `/ui/v1/datasets/${UUID_UPPER}/expiry`],
    ['DELETE', `${DATASETS}/${UUID}`, `/ui/v1/datasets/${UUID}`],
    ['GET', `${DATASETS}/${UUID}/bars?start=%2e%2e&x=a%2fb`, `/ui/v1/datasets/${UUID}/bars?start=%2e%2e&x=a%2fb`],
  ])('still forwards %s %s (an escape in the query is not the path)', async (method, path, upstreamPath) => {
    const reply = await send(method, path)
    expect(reply.status).toBe(200)
    expect(seen).toHaveLength(1)
    expect(seen[0]).toMatchObject({ method, url: upstreamPath })
    expect(seen[0].headers['x-instance-secret']).toBe(FAKE)
  })

  // A control character in the target never reaches the bypass: node's own parser answers 400. Pinned
  // because the bypass alone would pass '.<TAB>.' (no '..', nothing to decode) and WHATWG drops the tab.
  it.each([
    ['tab-between-dots', '/api/store/ui/v1/.\t./.\t./store/datasets/x'],
    ['carriage-return', '/api/store/ui/v1/.\r./x'],
    ['raw-newline', '/api/store/ui/v1/config\n'],
  ])('a raw %s in the target is refused by the server, data_store never asked', async (_id, path) => {
    const reply = await rawSend('GET', path)
    expect(reply.split('\r\n')[0]).toBe('HTTP/1.1 400 Bad Request')
    expect(seen).toEqual([])
  })

  it('logged nothing that holds the secret value', () => {
    expect(logged.filter((line) => line.includes(FAKE))).toEqual([])
  })
})

// tj-grna9p.177: the dev edge forwards PATCH on one dataset's /collecting, exactly as /expiry, and nothing
// else on that path. (The Caddyfile's matching rule is builder-shared's, a separate bead; devProxyEdge.spec.ts
// compares the two files and gains its collecting row when that rule lands.)
describe('the collecting PATCH through the real dev server', () => {
  const COLLECTING = `${DATASETS}/${UUID}/collecting`

  it.each([
    ['a lower-case uuid', COLLECTING, `/ui/v1/datasets/${UUID}/collecting`],
    ['an upper-case uuid', `${DATASETS}/${UUID.toUpperCase()}/collecting`, `/ui/v1/datasets/${UUID.toUpperCase()}/collecting`],
  ])('forwards PATCH on %s, stripped, with the dev server secret in place of the browser one', async (_name, path, upstreamPath) => {
    const reply = await send('PATCH', path, { 'x-instance-secret': 'browser-sent' })
    expect(reply.status).toBe(200)
    expect(seen).toHaveLength(1)
    expect(seen[0]).toMatchObject({ method: 'PATCH', url: upstreamPath })
    expect(seen[0].headers['x-instance-secret']).toBe(FAKE)
  })

  it('a raw lower-case patch on /collecting is the server own 400, never forwarded', async () => {
    const reply = await rawSend('patch', COLLECTING)
    // Node's parser takes only upper-case methods: a lower-case one is the server's own 400, never forwarded.
    expect(reply.split('\r\n')[0]).toBe('HTTP/1.1 400 Bad Request')
    expect(seen).toEqual([])
  })

  it.each([
    ['PUT', COLLECTING],
    ['POST', COLLECTING],
    ['DELETE', COLLECTING],
    // An unknown extra method on /collecting.
    ['PROPPATCH', COLLECTING],
    ['MKCOL', COLLECTING],
    ['PATCH', `${DATASETS}/${UUID}/collecting/`],
    ['PATCH', `${DATASETS}/${UUID}/collecting/x`],
    ['PATCH', `${DATASETS}/${UUID}/COLLECTING`],
    ['PATCH', `${DATASETS}/${UUID}/collect`],
    ['PATCH', `${DATASETS}/${UUID}/collectingx`],
    ['PATCH', `${DATASETS}/not-a-uuid/collecting`],
    ['PATCH', `${DATASETS}/collecting`],
    ['PATCH', `/api/store/ui/v1/collecting`],
    ['PATCH', `/api/store/store/datasets/${UUID}/collecting`],
  ])('answers %s %s 404 not found itself, data_store never asked', async (method, path) => {
    const reply = await send(method, path, { 'x-instance-secret': 'browser-sent' })
    expect(reply).toMatchObject({ status: 404, body: 'not found' })
    expect(seen).toEqual([])
  })

  it.each([
    ['PATCH', `${DATASETS}/${UUID}/%2e/collecting`],
    ['PATCH', `${DATASETS}/${UUID}//collecting`],
    ['PATCH', `${DATASETS}/${UUID}/%63ollecting`],
    ['PATCH', `${DATASETS}/${UUID}/../${UUID}/collecting`],
  ])('answers a write %s %s 400, data_store never asked', async (method, path) => {
    const reply = await send(method, path, { 'x-instance-secret': 'browser-sent' })
    expect(reply).toMatchObject({ status: 400, body: 'bad request' })
    expect(seen).toEqual([])
  })

  it('the expiry PATCH and the delete still forward beside it', async () => {
    expect((await send('PATCH', `${DATASETS}/${UUID}/expiry`)).status).toBe(200)
    expect((await send('DELETE', `${DATASETS}/${UUID}`)).status).toBe(200)
    expect(seen.map((s) => [s.method, s.url])).toEqual([
      ['PATCH', `/ui/v1/datasets/${UUID}/expiry`],
      ['DELETE', `/ui/v1/datasets/${UUID}`],
    ])
  })
})
