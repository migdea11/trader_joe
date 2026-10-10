import { readFileSync } from 'node:fs'
import { fileURLToPath, URL } from 'node:url'

import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

// Dev proxy mirrors deploy/web/Caddyfile (builder-shared's file, tj-grna9p.7): only
// /api/store/* is proxied. There is no /api/ingest route -- every UI call goes through
// data_store (tj-grna9p.5 addendum, 2026-10-01). The WebSocket upgrade for the events
// endpoint (/api/store/ui/v1/events) needs ws: true alongside the plain HTTP routes.
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
      },
    },
  },
})
