import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Dev proxy mirrors deploy/web/Caddyfile (builder-shared's file, tj-grna9p.7): only
// /api/store/* is proxied. There is no /api/ingest route -- every UI call goes through
// data_store (tj-grna9p.5 addendum, 2026-10-01). The WebSocket upgrade for the events
// endpoint (/api/store/ui/v1/events) needs ws: true alongside the plain HTTP routes.
const DATA_STORE_TARGET = 'http://localhost:8000'
const PROTOBUF_ES = fileURLToPath(new URL('./node_modules/@bufbuild/protobuf/dist/esm/', import.meta.url))

export default defineConfig({
  plugins: [vue()],
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
