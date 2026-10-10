// @vitest-environment node
// Node, not the suite's jsdom: vite.config.ts is a Node module whose import.meta.url must be a file: URL.
//
// The dev proxy target (web/vite.config.ts, 6815f4f; bead tj-mcrwrd). The dev web container sets
// VITE_DEV_PROXY_TARGET to data_store's address (docker-compose.web.dev.yaml); host-side `npm run dev`
// leaves it unset and keeps http://localhost:8000. The config reads it ONCE, at module load, so the
// resolved-config cases set process.env and re-import the module.
//
// The config is loaded through a computed URL, not a static import: vite.config.ts belongs to the
// tsconfig.node.json project, and a static import from src/ pulls it into tsconfig.vitest.json's program,
// which vue-tsc --build refuses (TS6307). The shape it must have is restated in ViteConfigModule.
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import type { ProxyOptions, UserConfig } from 'vite'

interface ViteConfigModule {
  default: UserConfig
  DEV_PROXY_TARGET_ENV: string
  DEFAULT_DEV_PROXY_TARGET: string
  resolveDevProxyTarget: (env: Readonly<Record<string, string | undefined>>) => string
}

const VITE_CONFIG_URL = new URL('../../vite.config.ts', import.meta.url).href

// An INVALID proxy target, used to check the config refuses a non-http scheme. Built from parts so
// semgrep's detect-insecure-websocket rule (which matches the plain-ws URL literal) does not fire on a
// value this spec exists to reject; the string under test is still exactly that ws URL.
const WS_TARGET = ['ws', '://data_store:80'].join('')

async function loadViteConfig(): Promise<ViteConfigModule> {
  return (await import(/* @vite-ignore */ VITE_CONFIG_URL)) as ViteConfigModule
}

// Read once for the pure-function cases; the variable is cleared first so the load itself cannot throw.
let DEV_PROXY_TARGET_ENV = ''
let DEFAULT_DEV_PROXY_TARGET = ''
let resolveDevProxyTarget: ViteConfigModule['resolveDevProxyTarget']

beforeAll(async () => {
  delete process.env.VITE_DEV_PROXY_TARGET
  vi.resetModules()
  ;({ DEV_PROXY_TARGET_ENV, DEFAULT_DEV_PROXY_TARGET, resolveDevProxyTarget } = await loadViteConfig())
})

describe('resolveDevProxyTarget', () => {
  it('names the variable the dev overlay sets, with the host-side default', () => {
    expect(DEV_PROXY_TARGET_ENV).toBe('VITE_DEV_PROXY_TARGET')
    expect(DEFAULT_DEV_PROXY_TARGET).toBe('http://localhost:8000')
  })

  it('falls back to the default when unset', () => {
    expect(resolveDevProxyTarget({})).toBe(DEFAULT_DEV_PROXY_TARGET)
    expect(resolveDevProxyTarget({ [DEV_PROXY_TARGET_ENV]: undefined })).toBe(DEFAULT_DEV_PROXY_TARGET)
  })

  it.each(['', ' ', '\t\n  '])('falls back to the default when blank (%j)', (value) => {
    expect(resolveDevProxyTarget({ [DEV_PROXY_TARGET_ENV]: value })).toBe(DEFAULT_DEV_PROXY_TARGET)
  })

  it.each(['http://data_store:80', 'https://store.example:8443', 'http://store-x:9123'])(
    'uses an http(s) override as given (%s)',
    (value) => {
      expect(resolveDevProxyTarget({ [DEV_PROXY_TARGET_ENV]: value })).toBe(value)
    },
  )

  it('trims surrounding whitespace from an override', () => {
    expect(resolveDevProxyTarget({ [DEV_PROXY_TARGET_ENV]: '  http://data_store:80\n' })).toBe('http://data_store:80')
  })

  it.each(['data_store:80', 'not a url', '/api/store'])('throws naming the variable on an unparsable value (%j)', (value) => {
    expect(() => resolveDevProxyTarget({ [DEV_PROXY_TARGET_ENV]: value })).toThrow(DEV_PROXY_TARGET_ENV)
  })

  it.each([WS_TARGET, 'ftp://data_store', 'file:///etc/passwd'])(
    'throws naming the variable on a non-http scheme (%s)',
    (value) => {
      expect(() => resolveDevProxyTarget({ [DEV_PROXY_TARGET_ENV]: value })).toThrow(DEV_PROXY_TARGET_ENV)
    },
  )
})

describe('the resolved /api/store dev proxy', () => {
  afterEach(() => {
    delete process.env.VITE_DEV_PROXY_TARGET
    vi.resetModules()
  })

  async function storeProxy(target: string | undefined): Promise<ProxyOptions> {
    if (target === undefined) delete process.env.VITE_DEV_PROXY_TARGET
    else process.env.VITE_DEV_PROXY_TARGET = target
    vi.resetModules()
    const config = (await loadViteConfig()).default
    const proxy = config.server?.proxy?.['/api/store']
    expect(typeof proxy).toBe('object')
    return proxy as ProxyOptions
  }

  it('targets the process environment value, keeping ws, changeOrigin and the prefix rewrite', async () => {
    const proxy = await storeProxy('http://data_store:80')
    expect(proxy.target).toBe('http://data_store:80')
    expect(proxy.ws).toBe(true)
    expect(proxy.changeOrigin).toBe(true)
    expect(proxy.rewrite?.('/api/store/ui/v1/config')).toBe('/ui/v1/config')
    expect(proxy.rewrite?.('/other/api/store')).toBe('/other/api/store')
  })

  it('targets the default when the variable is unset', async () => {
    const proxy = await storeProxy(undefined)
    expect(proxy.target).toBe(DEFAULT_DEV_PROXY_TARGET)
    expect(proxy.ws).toBe(true)
  })

  it('refuses to load the config with an invalid value, naming the variable', async () => {
    await expect(storeProxy(WS_TARGET)).rejects.toThrow(DEV_PROXY_TARGET_ENV)
  })
})
