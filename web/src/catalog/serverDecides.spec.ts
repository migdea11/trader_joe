// @vitest-environment node
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

// tj-grna9p.93: web/CLAUDE.md 'Server decides', as a scan of the browser source (web/src, specs and
// test helpers excluded). What is stale is the server's call (the Stale view, group, tile and list,
// all from server counts and echoes); the browser shows ages and never compares one to a threshold.
// The per-screen specs pin the words on screen; this pins that no source file grows a client-side
// decision that a screen spec happens not to exercise. A new legitimate use is one more allow-list
// line here, with its reason.
const SRC = fileURLToPath(new URL('..', import.meta.url))

function filesUnder(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name)
    return statSync(path).isDirectory() ? filesUnder(path) : [path]
  })
}

const SOURCES = filesUnder(SRC).filter((p) => /\.(ts|vue)$/.test(p) && !p.endsWith('.spec.ts') && !p.includes('__tests__'))

/** Each source's code lines, comments dropped (line comments, JSDoc and block comment bodies). */
function codeLines(path: string): { file: string; line: string }[] {
  const file = relative(SRC, path)
  return readFileSync(path, 'utf8')
    .split('\n')
    .map((line) => line.replace(/\/\/.*$/, ''))
    .filter((line) => !/^\s*(\/\*|\*)/.test(line))
    .map((line) => ({ file, line: line.trim() }))
    .filter(({ line }) => line !== '')
}

const CODE = SOURCES.flatMap(codeLines)
const where = (pattern: RegExp) => CODE.filter(({ line }) => pattern.test(line)).map(({ file, line }) => `${file}: ${line}`)
const filesWhere = (pattern: RegExp) => [...new Set(CODE.filter(({ line }) => pattern.test(line)).map(({ file }) => file))].sort()

describe("'Server decides': no stale decision in the browser", () => {
  it('scans a real tree', () => {
    expect(SOURCES.length).toBeGreaterThan(50)
    expect(filesWhere(/\bSTALE_DAYS\b/)).toContain(join('api', 'usage.ts'))
  })

  it('nothing compares anything to STALE_DAYS', () => {
    expect(where(/(^|[^=])[<>]=?\s*STALE_DAYS\b|\bSTALE_DAYS\s*[<>]/)).toEqual([])
  })

  it('STALE_DAYS is only defined, sent as stale_days, or a label default for the server echo', () => {
    expect(filesWhere(/\bSTALE_DAYS\b/)).toEqual(
      [
        join('api', 'index.ts'), // re-export
        join('api', 'usage.ts'), // the one definition
        join('catalog', 'filters.ts'), // sent as stale_days with the tiles' facets request
        join('catalog', 'usage.ts'), // label defaults until the echo arrives
        join('stores', 'usage.ts'), // sent as stale_days on the Usage routes
        join('views', 'data', 'UsageView.vue'), // the Stale note's fallback until the echo arrives
      ].sort(),
    )
  })

  it('no read age is compared to a threshold: no days >= N, no daysSinceUtc(...) > N', () => {
    expect(where(/\bdays\s*(>=|>|<=|<)\s*([1-9]|[A-Z_]{2,})/)).toEqual([])
    expect(where(/daysSinceUtc\([^)]*\)\s*(>=|>|<=|<)/)).toEqual([])
    expect(where(/\blastReadAt\b[^;]*(>=|>|<=|<)\s*[A-Za-z0-9_]/).filter((l) => !l.includes('=>'))).toEqual([])
  })

  it("the word 'Stale' is written only for server-decided things: the echo label, the server's list title, the run badge token", () => {
    expect(filesWhere(/['"`]Stale\b|\bStale:/)).toEqual(
      [
        join('catalog', 'usage.ts'), // 'Stale: {n} d' from the facets/summary echo
        join('theme', 'semantics.ts'), // STALE_BADGE, a colour token for runs (runRows uses .color only)
        join('views', 'data', 'UsageView.vue'), // 'Stale Datasets', the title of the server's stale list
      ].sort(),
    )
  })

  it('STALE_BADGE is never rendered as a dataset status (only its colour for runs, and the token showcase)', () => {
    expect(filesWhere(/\bSTALE_BADGE\b(?!\.color)/).filter((f) => !f.endsWith('semantics.ts'))).toEqual([
      join('components', 'runs', 'runRows.ts'), // the import line
      join('views', 'dev', 'TokenShowcase.vue'),
    ])
    expect(where(/\bSTALE_BADGE\b(?!\.color)/).filter((l) => l.startsWith(join('components', 'runs')))).toEqual([
      `${join('components', 'runs', 'runRows.ts')}: import { RUNNING_BADGE, REQUEST_BADGES, STALE_BADGE } from '@/theme/semantics'`,
    ])
  })
})
