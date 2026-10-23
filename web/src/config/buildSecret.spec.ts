// @vitest-environment node
import { mkdtempSync, readdirSync, readFileSync, rmSync, statSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { afterAll, describe, expect, it } from 'vitest'
import { build } from 'vite'

// tj-grna9p.93 (carried from tj-grna9p.134, which checked it by hand): a PRODUCTION build made while
// the build environment holds the instance secret carries neither its value nor its name. The browser
// never holds the secret: only the dev server (vite.config.ts, in node) and Caddy add it.
// devProxyEdge.spec.ts pins the source side (no VITE_ secret name, process.env only); this pins the
// built output, the thing that actually reaches a browser.
const WEB_ROOT = fileURLToPath(new URL('../..', import.meta.url))
const SECRET_ENV = 'INSTANCE_WRITE_SECRET'
const FAKE = 'fake-build-secret-0f3c9a7e51d24b8c'

function filesUnder(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name)
    return statSync(path).isDirectory() ? filesUnder(path) : [path]
  })
}

const outDir = mkdtempSync(join(tmpdir(), 'tj-web-build-'))

afterAll(() => {
  rmSync(outDir, { recursive: true, force: true })
})

describe('a production build with the secret in its environment', () => {
  it('emits no file holding the secret value or its name', async () => {
    const previous = { secret: process.env[SECRET_ENV] }
    process.env[SECRET_ENV] = FAKE
    try {
      await build({
        root: WEB_ROOT,
        configFile: join(WEB_ROOT, 'vite.config.ts'),
        mode: 'production',
        logLevel: 'silent',
        build: { outDir, emptyOutDir: true, write: true },
      })
    } finally {
      if (previous.secret === undefined) delete process.env[SECRET_ENV]
      else process.env[SECRET_ENV] = previous.secret
    }
    const files = filesUnder(outDir)
    expect(files.some((f) => f.endsWith('index.html'))).toBe(true)
    expect(files.filter((f) => f.endsWith('.js')).length).toBeGreaterThan(0)
    const leaking = files.filter((f) => {
      const text = readFileSync(f).toString('latin1')
      return text.includes(FAKE) || text.includes(SECRET_ENV) || text.includes('X-Instance-Secret')
    })
    expect(leaking).toEqual([])
  }, 120_000)
})
