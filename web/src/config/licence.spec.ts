import { execFileSync } from 'node:child_process'
import { readFileSync, statSync } from 'node:fs'
import { basename, resolve } from 'node:path'

import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'

import { PRIMEUI_LICENCE_ENV, primeUiLicenceKey } from './licence'

// tj-cwerrn: the PrimeUI licence key is read at build time, never committed, and the app runs
// without one.

const mocks = vi.hoisted(() => ({
  getUiConfig: vi.fn(),
  getDatasetFacets: vi.fn(),
  listDatasets: vi.fn(),
}))
vi.mock('@/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api')>()),
  ...mocks,
}))

describe('primeUiLicenceKey', () => {
  it('reads VITE_PRIMEUI_LICENSE_KEY and trims it', () => {
    expect(PRIMEUI_LICENCE_ENV).toBe('VITE_PRIMEUI_LICENSE_KEY')
    expect(primeUiLicenceKey({ VITE_PRIMEUI_LICENSE_KEY: '  abc.def  ' })).toBe('abc.def')
  })

  it.each([
    ['unset', {}],
    ['undefined', { VITE_PRIMEUI_LICENSE_KEY: undefined }],
    ['empty', { VITE_PRIMEUI_LICENSE_KEY: '' }],
    ['blank', { VITE_PRIMEUI_LICENSE_KEY: '   ' }],
    ['a number', { VITE_PRIMEUI_LICENSE_KEY: 42 }],
    ['a boolean', { VITE_PRIMEUI_LICENSE_KEY: true }],
    ['null', { VITE_PRIMEUI_LICENSE_KEY: null }],
  ])('%s gives the empty string', (_name, env) => {
    expect(primeUiLicenceKey(env)).toBe('')
  })
})

describe('the app with no key', () => {
  afterEach(() => {
    document.body.innerHTML = ''
  })

  it('starts through main.ts, passing PrimeVue an empty licence, and renders the shell', async () => {
    mocks.getUiConfig.mockResolvedValue({ allowedGroups: [], deploymentLabel: '', serverVersion: '' })
    mocks.getDatasetFacets.mockResolvedValue({ all: 0n, needsAttention: 0n, sources: [], updateTypes: [], statuses: [] })
    mocks.listDatasets.mockResolvedValue({ items: [], nextCursor: '' })
    vi.stubEnv('VITE_PRIMEUI_LICENSE_KEY', '')
    document.body.innerHTML = '<div id="app"></div>'
    await import('@/main')
    await flushPromises()
    const app = document.getElementById('app')!
    expect(app.querySelector('header.top-bar')).not.toBeNull()
    expect(app.textContent).toContain('Datasets')
    vi.unstubAllEnvs()
  })
})

describe('no key is committed', () => {
  // Key-shaped: the PrimeUI- prefix followed by a long token. The pattern itself, written here as a
  // character class, does not match.
  const KEY_SHAPE = /PrimeUI-[A-Za-z0-9+/=_.-]{16,}/
  const ROOT = resolve(import.meta.dirname, '../../..')
  const BINARY = /\.(woff2?|png|jpe?g|gif|ico|pdf|zip|gz)$/i

  it('no tracked file holds a key-shaped string', () => {
    const tracked = execFileSync('git', ['ls-files', '-z'], { cwd: ROOT, encoding: 'utf8' }).split('\0').filter(Boolean)
    // Local env files are never tracked; should one ever be, it is reported rather than read.
    const envFiles = tracked.filter((path) => basename(path).startsWith('.env') && basename(path) !== '.env.default')
    expect(envFiles).toEqual([])

    const scanned = tracked.filter((path) => !BINARY.test(path))
    expect(scanned).toEqual(expect.arrayContaining(['.env.default', 'web/package-lock.json', 'web/src/config/licence.ts']))
    const hits = scanned.filter((path) => {
      const full = resolve(ROOT, path)
      if (statSync(full, { throwIfNoEntry: false })?.isFile() !== true) return false
      return KEY_SHAPE.test(readFileSync(full, 'utf8'))
    })
    expect(hits).toEqual([])
  })

  it('the pattern would catch a key', () => {
    // Assembled from parts so this file does not hold a key-shaped string itself.
    const sample = ['PrimeUI', 'abcdefghijklmnop0123'].join('-')
    expect(KEY_SHAPE.test(`VITE_PRIMEUI_LICENSE_KEY=${sample}`)).toBe(true)
  })

  it('.env.default documents the variable with an empty value', () => {
    const lines = readFileSync(resolve(ROOT, '.env.default'), 'utf8').split('\n')
    expect(lines).toContain('VITE_PRIMEUI_LICENSE_KEY=')
  })
})
