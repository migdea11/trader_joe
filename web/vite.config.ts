import { readFileSync } from 'node:fs'
import { posix } from 'node:path'
import { fileURLToPath, URL } from 'node:url'

import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

// The dev server mirrors the prod proxy (deploy/web/Caddyfile, builder-shared's file) so that nothing
// reaches data_store in dev that prod would not forward, and the UI's writes work in both. It may
// answer 400 to a spelling prod forwards. A path the proxy does not own (/api/ingest/*, //api/...)
// falls through to Vite's SPA fallback, where prod answers 404 (known gap, follow-up tj-xc3laz).
// Under /api/store it:
//   1. forwards only the four Caddy rules (see isAllowedStoreRequest): GET and HEAD under
//      /api/store/ui/v1/ (the events WebSocket upgrade is a GET), PATCH on one dataset's /expiry,
//      PATCH on one dataset's /collecting, and DELETE on one dataset. Anything else answers 404 here without
//      reaching data_store; a path containing '..' answers 400 (stricter than Caddy, whose @traversal 400 never fires under /api:
//      there a traversal is cleaned onto the /api/* 404 and a literal a..b is forwarded). The decision is made on the
//      DECODED, CLEANED path, as Caddy matches (it decodes and cleans before it matches): http-proxy
//      resolves a percent-encoded dot segment (%2e%2e) as '..' itself, so a rule applied to the raw url
//      would forward /store/... or /internal/... with the secret. A path that is malformed, still
//      changing after three decode passes, hides a '/' or a backslash inside a segment, holds a '..',
//      or is spelled differently from its cleaned form answers 400, so nothing is forwarded in a
//      spelling the allow-list did not see;
//   2. sets X-Instance-Secret on the upstream request from this node process's
//      INSTANCE_WRITE_SECRET, replacing any value the browser sent (see applyInstanceSecret). The
//      secret is never read through import.meta.env or a VITE_ name, which Vite would bundle into
//      the browser, and is never logged. Unset or blank sets nothing, so data_store refuses writes.
// There is no /api/ingest route: every UI call goes through data_store (tj-grna9p.5 addendum).
//
// The target is VITE_DEV_PROXY_TARGET: unset or blank keeps the host-side `npm run dev` default, and the
// dev web container (docker-compose.web.dev.yaml) points it at the data_store container.
export const DEV_PROXY_TARGET_ENV = 'VITE_DEV_PROXY_TARGET'
export const DEFAULT_DEV_PROXY_TARGET = 'http://localhost:8000'

/** The dev proxy target for /api/store: the env value trimmed, the default when unset or blank; throws on a value that is not an http(s) URL. */
export function resolveDevProxyTarget(env: Readonly<Record<string, string | undefined>>): string {
  const raw = env[DEV_PROXY_TARGET_ENV]?.trim()
  if (!raw) return DEFAULT_DEV_PROXY_TARGET
  let url: URL
  try {
    url = new URL(raw)
  } catch {
    throw new Error(`${DEV_PROXY_TARGET_ENV} must be an absolute http(s) URL such as ${DEFAULT_DEV_PROXY_TARGET}, got "${raw}"`)
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw new Error(`${DEV_PROXY_TARGET_ENV} must use http: or https:, got "${raw}"`)
  }
  return raw
}

const UI_V1_PREFIX = '/api/store/ui/v1/'
const UUID = '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
// Character for character the Caddyfile's @store_ui_expiry, @store_ui_collecting and @store_ui_delete regexes.
const EXPIRY_PATH = new RegExp(`^/api/store/ui/v1/datasets/${UUID}/expiry$`)
const COLLECTING_PATH = new RegExp(`^/api/store/ui/v1/datasets/${UUID}/collecting$`)
const DELETE_PATH = new RegExp(`^/api/store/ui/v1/datasets/${UUID}$`)

function pathOnly(url: string): string {
  const q = url.indexOf('?')
  return q === -1 ? url : url.slice(0, q)
}

const MAX_DECODE_PASSES = 3
const ENCODED_SEPARATOR = /%2f|%5c/i

/**
 * The path percent-decoded to a fixed point (at most three passes, so %252e%252e cannot slip through), or
 * null when it cannot be trusted: a malformed escape, still changing after the bound, an encoded or
 * literal separator ('%2f', '%5c', a backslash) smuggled inside a segment. (An encoded control
 * character such as %0a decodes to something the raw path is not, so the bypass's raw-equals-cleaned
 * check refuses it.) The query string is excluded. Caddy is looser: it forwards %252e, %5c, %2f, a..b, // and
 * <uuid>%2fexpiry when they stay under /ui/v1, and data_store answers 404 or routes them to the same route, so
 * none reaches a write route other than the three intended (expiry, collecting, delete). This dev proxy refuses all of them with 400, so dev
 * is stricter than prod.
 */
function decodePath(path: string): string | null {
  let current = pathOnly(path)
  for (let pass = 0; pass <= MAX_DECODE_PASSES; pass++) {
    if (ENCODED_SEPARATOR.test(current) || current.includes('\\')) return null
    let next: string
    try {
      next = decodeURIComponent(current)
    } catch {
      return null
    }
    if (next === current) return current
    current = next
  }
  return null
}

/**
 * True for a path the dev server answers 400, like the shape of Caddy's @traversal rule (`path *..*`),
 * which in prod only fires off /api, but judged on the decoded form: a '..' after percent-decoding to a fixed point, an encoded or literal separator
 * inside a segment, a malformed escape, or a path still changing after three decodes. The query string
 * is not part of the path.
 */
export function isTraversalPath(path: string): boolean {
  const decoded = decodePath(path)
  return decoded === null || decoded.includes('..')
}

/**
 * Whether the prod proxy forwards this request: the Caddyfile's four rules, matched on the unstripped
 * path (the query string is ignored, the method is compared upper-cased).
 */
export function isAllowedStoreRequest(method: string, path: string): boolean {
  const p = pathOnly(path)
  const m = method.toUpperCase()
  if (m === 'GET' || m === 'HEAD') return p.startsWith(UI_V1_PREFIX)
  if (m === 'PATCH') return EXPIRY_PATH.test(p) || COLLECTING_PATH.test(p)
  if (m === 'DELETE') return DELETE_PATH.test(p)
  return false
}

interface UpstreamHeaders {
  removeHeader(name: string): void
  setHeader(name: string, value: string): void
}

/** Replace any browser-sent X-Instance-Secret with the dev server's INSTANCE_WRITE_SECRET; sets nothing when it is unset or blank. */
export function applyInstanceSecret(
  upstream: UpstreamHeaders,
  env: Readonly<Record<string, string | undefined>> = process.env,
): void {
  upstream.removeHeader('X-Instance-Secret')
  const secret = env.INSTANCE_WRITE_SECRET
  if (secret) upstream.setHeader('X-Instance-Secret', secret)
}

/**
 * The proxy entry's `bypass`: undefined forwards. A string result makes Vite skip the proxy, so a
 * refusal is written to `res` when there is one (an HTTP request) and `false` is returned otherwise
 * (a WebSocket upgrade, which Vite answers with a bare 404).
 */
function storeBypass(
  req: { method?: string; url?: string },
  res: { statusCode: number; setHeader(n: string, v: string): void; end(b?: string): void } | undefined,
): string | false | undefined {
  const url = req.url ?? ''
  const refuse = (status: number, body: string): string | false => {
    if (!res) return false
    res.statusCode = status
    res.setHeader('Content-Type', 'text/plain')
    res.end(body)
    return url
  }
  if (isTraversalPath(url)) return refuse(400, 'bad request')
  // The decode succeeded (isTraversalPath refused otherwise). Forward only a path spelled as the
  // allow-list sees it: the raw path must equal the decoded, cleaned one.
  const raw = pathOnly(url)
  const decoded = decodePath(url) ?? raw
  const cleaned = decoded === '' ? '' : posix.normalize(decoded)
  if (cleaned !== raw) return refuse(400, 'bad request')
  if (!isAllowedStoreRequest(req.method ?? '', cleaned)) return refuse(404, 'not found')
  return undefined
}

// process.env wins over web/.env files in loadEnv, so a compose `environment:` entry and a shell export both apply.
const DATA_STORE_TARGET = resolveDevProxyTarget(
  loadEnv(process.env.NODE_ENV || 'development', fileURLToPath(new URL('.', import.meta.url)), 'VITE_'),
)
const PROTOBUF_ES = fileURLToPath(new URL('./node_modules/@bufbuild/protobuf/dist/esm/', import.meta.url))

// The UI's own version, shown in the footer and the About modal: package.json's version at build time.
const UI_VERSION = (
  JSON.parse(readFileSync(fileURLToPath(new URL('./package.json', import.meta.url)), 'utf-8')) as {
    version: string
  }
).version

export default defineConfig({
  plugins: [vue()],
  define: {
    __UI_VERSION__: JSON.stringify(UI_VERSION),
  },
  resolve: {
    alias: [
      { find: '@generated', replacement: fileURLToPath(new URL('../gen/proto/ts', import.meta.url)) },
      { find: '@', replacement: fileURLToPath(new URL('./src', import.meta.url)) },
      // The generated tree (repo-root gen/proto/ts, written by `npm run gen:proto`, never
      // committed) sits outside web/, so its `@bufbuild/protobuf` imports would not find
      // web/node_modules. Same pin as tsconfig.app.json paths.
      { find: /^@bufbuild\/protobuf$/, replacement: PROTOBUF_ES + 'index.js' },
      { find: /^@bufbuild\/protobuf\/(.+)$/, replacement: PROTOBUF_ES + '$1/index.js' },
    ],
  },
  server: {
    proxy: {
      // The browser calls /api/store/ui/v1/...; the store serves /ui/v1/... with no prefix, so the
      // prefix is stripped here (Caddy does the same in production).
      '/api/store': {
        target: DATA_STORE_TARGET,
        changeOrigin: true,
        ws: true,
        rewrite: (path) => path.replace(/^\/api\/store/, ''),
        bypass: (req, res) => storeBypass(req, res),
        configure: (proxy) => {
          // proxyReq is a plain request, proxyReqWs a WebSocket upgrade; Caddy sets the header on both.
          proxy.on('proxyReq', (proxyReq) => applyInstanceSecret(proxyReq))
          proxy.on('proxyReqWs', (proxyReq) => applyInstanceSecret(proxyReq))
        },
      },
    },
  },
})
