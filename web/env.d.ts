/// <reference types="vite/client" />

// Injected by vite.config.ts `define`: package.json's version at build time.
declare const __UI_VERSION__: string

interface ImportMetaEnv {
  // PrimeUI licence key, build time, empty by default and never committed (src/config/licence.ts).
  readonly VITE_PRIMEUI_LICENSE_KEY?: string
  // Dev server only (vite.config.ts): the /api/store proxy target, default http://localhost:8000.
  readonly VITE_DEV_PROXY_TARGET?: string
}
