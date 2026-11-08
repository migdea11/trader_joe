// @vitest-environment node
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

import { DatasetHealthSchema, DatasetSummarySchema } from '@generated/trader_joe/proto/ui/v1/data_pb'

// tj-grna9p.168 (owner ruling 2026-10-30, option B; names confirmed 2026-10-31): the word
// 'freshness' is dropped for dataset state. The umbrella is dataset health; FRESH stays as the one
// recency-satisfied state. These pins keep the old vocabulary from creeping back into the UI source
// or into a fixture, where a fixture on the old key decodes as "no health" and still passes against
// Unknown. Nothing here asserts a badge word: those stay pinned in theme.spec.ts and the screens.

const SRC = fileURLToPath(new URL('..', import.meta.url))
const SELF = relative(SRC, fileURLToPath(import.meta.url))

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name)
    return statSync(path).isDirectory() ? files(path) : [path]
  })
}

const ALL = files(SRC).map((p) => relative(SRC, p))
const isTest = (p: string) => p.endsWith('.spec.ts') || p.split('/').includes('__tests__')
const PRODUCTION = ALL.filter((p) => !isTest(p))
const TESTS = ALL.filter(isTest)
const read = (p: string) => readFileSync(join(SRC, p), 'utf8')
const carrying = (paths: string[], pattern: RegExp) => paths.filter((p) => pattern.test(read(p))).sort()

// Any spelling of the old dataset-state vocabulary: freshness, Freshness, FreshnessStatus,
// FRESHNESS_STATUS_*, FRESHNESS_BADGES, freshnessBadge.
const OLD_WORD = /freshness/i

// Production files that may still say 'freshness', each with its reason. Empty since H4
// (tj-7zjov1.206) renamed theme/README.md. An entry that goes stale (the file gone, or no longer
// saying the word) fails below, so an allowance cannot outlive its reason; it may not grow without one.
const PRODUCTION_FRESHNESS_ALLOWED: Readonly<Record<string, string>> = {}

// The bare word 'fresh' (not 'refresh', which is a different word) in production, each with its reason.
const PRODUCTION_FRESH_ALLOWED: Readonly<Record<string, string>> = {
  'catalog/facets.ts': 'the enum value DatasetHealthStatus.FRESH, kept by the owner ruling of 2026-10-31',
  'theme/semantics.ts': 'the value DATASET_HEALTH_STATUS_FRESH, kept by the owner ruling of 2026-10-31',
  'views/data/DatasetDetailView.vue': "a local `fresh` for a just-cached coverage entry: the English word, not dataset state",
}

// Test files that may name the old vocabulary, and only to prove the old spelling is NOT honoured.
const TEST_FRESHNESS_ALLOWED: Readonly<Record<string, string>> = {
  'api/api.spec.ts': 'decodes a payload still on the pre-rename `freshness` key and expects no health',
  'theme/theme.spec.ts': 'feeds pre-rename FRESHNESS_STATUS_* values to healthBadge and expects Unknown',
  [SELF]: 'this file: the pattern and the allow-lists',
}

describe("dataset state is named health, never 'freshness' (tj-grna9p.168)", () => {
  it('the scan reaches the source tree and the specs', () => {
    expect(PRODUCTION).toEqual(expect.arrayContaining(['theme/semantics.ts', 'catalog/rows.ts', 'components/catalog/StatusCell.vue']))
    expect(TESTS).toEqual(expect.arrayContaining(['api/api.spec.ts', 'catalog/catalog.spec.ts', SELF]))
  })

  it('no non-spec file under web/src says freshness, outside the allow-list', () => {
    const offenders = carrying(PRODUCTION, OLD_WORD).filter((p) => !(p in PRODUCTION_FRESHNESS_ALLOWED))
    expect(offenders).toEqual([])
  })

  it('no spec or fixture says freshness, except the negative tests that prove the old spelling is ignored', () => {
    expect(carrying(TESTS, OLD_WORD).filter((p) => !(p in TEST_FRESHNESS_ALLOWED))).toEqual([])
  })

  it('the bare word fresh appears only where the allow-list gives a reason', () => {
    expect(carrying(PRODUCTION, /\bfresh\b/i)).toEqual(Object.keys(PRODUCTION_FRESH_ALLOWED).sort())
  })

  it('every allow-list entry names a file that exists and carries a reason', () => {
    for (const table of [PRODUCTION_FRESHNESS_ALLOWED, PRODUCTION_FRESH_ALLOWED, TEST_FRESHNESS_ALLOWED]) {
      for (const [path, reason] of Object.entries(table)) {
        expect(ALL).toContain(path)
        expect(reason.length).toBeGreaterThan(10)
      }
    }
  })

  it('every freshness allowance still says freshness, so a stale entry cannot linger', () => {
    for (const table of [PRODUCTION_FRESHNESS_ALLOWED, TEST_FRESHNESS_ALLOWED]) {
      const listed = Object.keys(table).sort()
      expect(listed.filter((p) => !ALL.includes(p))).toEqual([])
      expect(carrying(listed.filter((p) => ALL.includes(p)), OLD_WORD)).toEqual(listed)
    }
  })
})

describe('the wire field the UI reads is DatasetSummary.health', () => {
  it('field 18 is health, with JSON name health; no field is named freshness', () => {
    const field = DatasetSummarySchema.fields.find((f) => f.number === 18)!
    expect(field.name).toBe('health')
    expect(field.jsonName).toBe('health')
    expect(field.message?.typeName).toBe(DatasetHealthSchema.typeName)
    expect(DatasetSummarySchema.fields.filter((f) => OLD_WORD.test(f.name) || OLD_WORD.test(f.jsonName))).toEqual([])
  })

  it('the catalog row mapping reads row.health', () => {
    expect(read('catalog/rows.ts')).toMatch(/\brow\.health\?\.status\b/)
  })
})
