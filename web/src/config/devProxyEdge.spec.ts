// @vitest-environment node
// Node, not the suite's jsdom: vite.config.ts is a Node module whose import.meta.url must be a file: URL.
//
// The dev proxy mirrors the prod edge (web/vite.config.ts, bead tj-grna9p.134; the prod edge is
// deploy/web/Caddyfile, bead tj-grna9p.133). Under /api/store the dev server forwards exactly Caddy's
// rules: the read rule (GET and HEAD under /api/store/ui/v1/) and the six writes (PATCH on one dataset's
// /expiry, PATCH on one dataset's /collecting, DELETE on one dataset, and the three panel POSTs of
// tj-7zjov1.169: POST .../panels, POST .../panels/<uuid>/settings-versions, POST
// .../panel-sources/DATA_SOURCE_<NAME>/settings-versions), answers a '..' path 400 and anything else 404
// without reaching data_store, and sets
// X-Instance-Secret from its own process.env.INSTANCE_WRITE_SECRET, replacing any browser value.
//
// The allow-list table is the SAME cases as server/common/tests/test_web_edge.py (the Caddyfile's pins), so
// the two edges cannot drift silently; the write regexes are also checked against the Caddyfile's own text,
// read here, case by case. devProxyServer.spec.ts runs the same rules through a real Vite dev server.
//
// NEVER A REAL SECRET: every case sets INSTANCE_WRITE_SECRET to a fake value or deletes it, and the
// original is put back without ever being read into an assertion.
//
// The config is loaded through a computed URL, not a static import (see devProxyTarget.spec.ts: TS6307).
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { OutgoingMessage } from 'node:http'
import { fileURLToPath } from 'node:url'
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import type { ProxyOptions, UserConfig } from 'vite'

interface UpstreamHeaders {
  removeHeader(name: string): void
  setHeader(name: string, value: string): void
}

interface ViteConfigModule {
  default: UserConfig
  isAllowedStoreRequest: (method: string, path: string) => boolean
  isTraversalPath: (path: string) => boolean
  applyInstanceSecret: (upstream: UpstreamHeaders, env?: Readonly<Record<string, string | undefined>>) => void
}

const VITE_CONFIG_URL = new URL('../../vite.config.ts', import.meta.url).href
const WEB_ROOT = fileURLToPath(new URL('../../', import.meta.url))
const REPO_ROOT = fileURLToPath(new URL('../../../', import.meta.url))
const CADDYFILE = `${REPO_ROOT}deploy/web/Caddyfile`
const SECRET_ENV = 'INSTANCE_WRITE_SECRET'
const HEADER = 'X-Instance-Secret'
const FAKE = 'dummy-not-a-secret'

let mod: ViteConfigModule
let storeProxy: ProxyOptions
const ORIGINAL_SECRET = process.env[SECRET_ENV]
const ORIGINAL_TARGET = process.env.VITE_DEV_PROXY_TARGET

beforeAll(async () => {
  delete process.env.VITE_DEV_PROXY_TARGET
  vi.resetModules()
  mod = (await import(/* @vite-ignore */ VITE_CONFIG_URL)) as ViteConfigModule
  storeProxy = mod.default.server?.proxy?.['/api/store'] as ProxyOptions
})

afterAll(() => {
  if (ORIGINAL_TARGET === undefined) delete process.env.VITE_DEV_PROXY_TARGET
  else process.env.VITE_DEV_PROXY_TARGET = ORIGINAL_TARGET
})

afterEach(() => {
  if (ORIGINAL_SECRET === undefined) delete process.env[SECRET_ENV]
  else process.env[SECRET_ENV] = ORIGINAL_SECRET
  vi.restoreAllMocks()
})

// ---------------------------------------------------------------------------------------------------
// THE ALLOW-LIST: test_web_edge.py's cases, name for name

const LOWER_UUID = '0f8fad5b-d9cb-469f-a165-70867728950e'
const UPPER_UUID = '0F8FAD5B-D9CB-469F-A165-70867728950E'
const DATASETS = '/api/store/ui/v1/datasets'
const PANELS = '/api/store/ui/v1/panels'
const PANEL_SOURCES = '/api/store/ui/v1/panel-sources'
const SOURCE = 'DATA_SOURCE_ALPACA_API'
const PANEL_SETTINGS_PATH = `${PANELS}/${LOWER_UUID}/settings-versions`
const SOURCE_SETTINGS_PATH = `${PANEL_SOURCES}/${SOURCE}/settings-versions`

// test_the_ui_requests_are_forwarded_by_exactly_their_rule
const FORWARDED: [string, string, string][] = [
  ['patch-lower', 'PATCH', `${DATASETS}/${LOWER_UUID}/expiry`],
  ['patch-upper', 'PATCH', `${DATASETS}/${UPPER_UUID}/expiry`],
  ['collecting-lower', 'PATCH', `${DATASETS}/${LOWER_UUID}/collecting`],
  ['collecting-upper', 'PATCH', `${DATASETS}/${UPPER_UUID}/collecting`],
  ['delete-lower', 'DELETE', `${DATASETS}/${LOWER_UUID}`],
  ['delete-upper', 'DELETE', `${DATASETS}/${UPPER_UUID}`],
  ['panel-create', 'POST', PANELS],
  ['panel-settings-lower', 'POST', PANEL_SETTINGS_PATH],
  ['panel-settings-upper', 'POST', `${PANELS}/${UPPER_UUID}/settings-versions`],
  ['source-settings-alpaca', 'POST', SOURCE_SETTINGS_PATH],
  ['source-settings-ib', 'POST', `${PANEL_SOURCES}/DATA_SOURCE_IB_API/settings-versions`],
  ['get-read', 'GET', `${DATASETS}/${LOWER_UUID}/bars`],
  ['head-read', 'HEAD', '/api/store/ui/v1/config'],
  ['get-panels', 'GET', PANELS],
  ['get-panel-settings', 'GET', PANEL_SETTINGS_PATH],
  ['get-source-settings', 'GET', SOURCE_SETTINGS_PATH],
  ['head-panel-settings', 'HEAD', PANEL_SETTINGS_PATH],
]

// _REFUSED_WRITE_PATHS, each refused for PATCH and for DELETE
const REFUSED_WRITE_PATHS: Record<string, string> = {
  bars: `${DATASETS}/${LOWER_UUID}/bars`,
  coverage: `${DATASETS}/${LOWER_UUID}/coverage`,
  'bar-pages': `${DATASETS}/${LOWER_UUID}/bar-pages`,
  'not-a-uuid': `${DATASETS}/not-a-uuid`,
  'uuid-no-dashes': `${DATASETS}/${LOWER_UUID.replaceAll('-', '')}`,
  'uuid-short': `${DATASETS}/${LOWER_UUID.slice(0, -1)}`,
  'uuid-long': `${DATASETS}/${LOWER_UUID}0`,
  'uuid-non-hex': `${DATASETS}/${LOWER_UUID.slice(0, -1)}g`,
  'trailing-slash': `${DATASETS}/${LOWER_UUID}/`,
  'expiry-trailing-slash': `${DATASETS}/${LOWER_UUID}/expiry/`,
  'expiry-extra-segment': `${DATASETS}/${LOWER_UUID}/expiry/x`,
  'expiry-uppercase': `${DATASETS}/${LOWER_UUID}/EXPIRY`,
  'collecting-trailing-slash': `${DATASETS}/${LOWER_UUID}/collecting/`,
  'collecting-extra-segment': `${DATASETS}/${LOWER_UUID}/collecting/x`,
  'collecting-uppercase': `${DATASETS}/${LOWER_UUID}/COLLECTING`,
  'collecting-not-a-uuid': `${DATASETS}/not-a-uuid/collecting`,
  'trailing-newline': `${DATASETS}/${LOWER_UUID}\n`,
  'the-list': DATASETS,
  'the-list-slash': `${DATASETS}/`,
  'store-route': `/api/store/store/datasets/${LOWER_UUID}`,
  unprefixed: `/ui/v1/datasets/${LOWER_UUID}`,
  'prefix-in-front': `/x${DATASETS}/${LOWER_UUID}`,
  'other-version': `/api/store/ui/v2/datasets/${LOWER_UUID}`,
  // The panel look-alikes (tj-7zjov1.169): near each of the three POST shapes, and none of them. The four
  // spellings the dev bypass answers 400 (an encoded slash, an empty segment) are in REFUSED_WRITE_SPELLINGS.
  'panels-slash': `${PANELS}/`,
  'panels-uppercase': '/api/store/ui/v1/PANELS',
  'panels-title': '/api/store/ui/v1/Panels',
  'panels-singular': '/api/store/ui/v1/panel',
  'panels-longer': `${PANELS}x`,
  'panels-trailing-newline': `${PANELS}\n`,
  'panels-sub-path': `${PANELS}/x`,
  'panels-the-panel': `${PANELS}/${LOWER_UUID}`,
  'panels-the-panel-slash': `${PANELS}/${LOWER_UUID}/`,
  'panels-members': `${PANELS}/${LOWER_UUID}/members`,
  'panels-unprefixed': '/ui/v1/panels',
  'panels-other-version': '/api/store/ui/v2/panels',
  'panels-prefix-in-front': `/x${PANELS}`,
  'panels-store-route': '/api/store/store/panels',
  'panel-settings-not-a-uuid': `${PANELS}/not-a-uuid/settings-versions`,
  'panel-settings-uuid-no-dashes': `${PANELS}/${LOWER_UUID.replaceAll('-', '')}/settings-versions`,
  'panel-settings-uuid-short': `${PANELS}/${LOWER_UUID.slice(0, -1)}/settings-versions`,
  'panel-settings-uuid-long': `${PANELS}/${LOWER_UUID}0/settings-versions`,
  'panel-settings-uuid-non-hex': `${PANELS}/${LOWER_UUID.slice(0, -1)}g/settings-versions`,
  'panel-settings-a-source': `${PANELS}/${SOURCE}/settings-versions`,
  'panel-settings-bare': `${PANELS}/settings-versions`,
  'panel-settings-trailing-slash': `${PANEL_SETTINGS_PATH}/`,
  'panel-settings-extra-segment': `${PANEL_SETTINGS_PATH}/x`,
  'panel-settings-a-version': `${PANEL_SETTINGS_PATH}/1`,
  'panel-settings-uppercase': `${PANELS}/${LOWER_UUID}/SETTINGS-VERSIONS`,
  'panel-settings-underscore': `${PANELS}/${LOWER_UUID}/settings_versions`,
  'panel-settings-singular': `${PANELS}/${LOWER_UUID}/settings-version`,
  'panel-settings-trailing-newline': `${PANEL_SETTINGS_PATH}\n`,
  'panel-settings-prefix-in-front': `/x${PANEL_SETTINGS_PATH}`,
  'panel-settings-unprefixed': `/ui/v1/panels/${LOWER_UUID}/settings-versions`,
  'panel-settings-under-sources': `${PANEL_SOURCES}/${LOWER_UUID}/settings-versions`,
  'source-settings-not-a-source': `${PANEL_SOURCES}/NOT_A_SOURCE/settings-versions`,
  'source-settings-lowercase': `${PANEL_SOURCES}/${SOURCE.toLowerCase()}/settings-versions`,
  'source-settings-mixed-case': `${PANEL_SOURCES}/DATA_SOURCE_Alpaca_API/settings-versions`,
  'source-settings-prefix-only': `${PANEL_SOURCES}/DATA_SOURCE_/settings-versions`,
  'source-settings-no-prefix': `${PANEL_SOURCES}/ALPACA_API/settings-versions`,
  'source-settings-prefix-in-name': `${PANEL_SOURCES}/X_${SOURCE}/settings-versions`,
  'source-settings-dash': `${PANEL_SOURCES}/DATA_SOURCE_ALPACA-API/settings-versions`,
  'source-settings-dot': `${PANEL_SOURCES}/DATA_SOURCE_ALPACA.API/settings-versions`,
  'source-settings-space': `${PANEL_SOURCES}/DATA_SOURCE_ALPACA API/settings-versions`,
  'source-settings-two-segments': `${PANEL_SOURCES}/${SOURCE}/${SOURCE}/settings-versions`,
  'source-settings-bare': `${PANEL_SOURCES}/settings-versions`,
  'source-settings-the-source': `${PANEL_SOURCES}/${SOURCE}`,
  'source-settings-the-list': PANEL_SOURCES,
  'source-settings-the-list-slash': `${PANEL_SOURCES}/`,
  'source-settings-trailing-slash': `${SOURCE_SETTINGS_PATH}/`,
  'source-settings-extra-segment': `${SOURCE_SETTINGS_PATH}/x`,
  'source-settings-uppercase': `${PANEL_SOURCES}/${SOURCE}/SETTINGS-VERSIONS`,
  'source-settings-trailing-newline': `${SOURCE_SETTINGS_PATH}\n`,
  'source-settings-a-uuid': `${PANEL_SOURCES}/${LOWER_UUID}/settings-versions`,
  'source-settings-singular-root': `/api/store/ui/v1/panel-source/${SOURCE}/settings-versions`,
  'source-settings-under-panels': `${PANELS}/sources/${SOURCE}/settings-versions`,
  'source-settings-prefix-in-front': `/x${SOURCE_SETTINGS_PATH}`,
  'source-settings-unprefixed': `/ui/v1/panel-sources/${SOURCE}/settings-versions`,
  'source-settings-other-version': `/api/store/ui/v2/panel-sources/${SOURCE}/settings-versions`,
}
// test_a_write_on_any_other_path_is_not_forwarded: every write method, POST included, on every path.
const WRITE_METHODS = ['PATCH', 'DELETE', 'POST']
const REFUSED_WRITES = Object.entries(REFUSED_WRITE_PATHS).flatMap(([id, path]) =>
  WRITE_METHODS.map((method) => [`${method.toLowerCase()}-${id}`, method, path] as [string, string, string]),
)

// _REFUSED_WRITE_PATHS's spellings that Caddy cleans or decodes: the allow-list refuses them raw, as
// Caddy's regex does, and the dev bypass answers them 400 (stricter than prod), not 404.
const REFUSED_WRITE_SPELLINGS: Record<string, string> = {
  'collecting-encoded-slash': `${DATASETS}/${LOWER_UUID}%2Fcollecting`,
  'collecting-dot-segment': `${DATASETS}/${LOWER_UUID}/../${LOWER_UUID}/collecting`,
  'panel-settings-encoded-slash': `${PANELS}/${LOWER_UUID}%2Fsettings-versions`,
  'panel-settings-no-panel': `${PANELS}//settings-versions`,
  'source-settings-encoded-slash': `${PANEL_SOURCES}/${SOURCE}%2Fsettings-versions`,
  'source-settings-no-source': `${PANEL_SOURCES}//settings-versions`,
}
const REFUSED_WRITE_SPELLING_CASES = Object.entries(REFUSED_WRITE_SPELLINGS).flatMap(([id, path]) =>
  WRITE_METHODS.map((method) => [`${method.toLowerCase()}-${id}`, method, path] as [string, string, string]),
)

// test_on_each_write_path_only_its_method_reaches_a_write_rule, for the three panel POST paths: POST is
// the write; GET and HEAD are the read rule's; every other method is refused.
const PANEL_WRITE_PATHS: [string, string][] = [
  ['store_ui_panel_create', PANELS],
  ['store_ui_panel_settings', PANEL_SETTINGS_PATH],
  ['store_ui_source_settings', SOURCE_SETTINGS_PATH],
]
const PANEL_PATH_METHODS: [string, string, boolean][] = PANEL_WRITE_PATHS.flatMap(([, path]) =>
  (
    [
      ['POST', true],
      ['GET', true],
      ['HEAD', true],
      ['PATCH', false],
      ['DELETE', false],
      ['PUT', false],
      ['OPTIONS', false],
      ['CONNECT', false],
      ['TRACE', false],
    ] as [string, boolean][]
  ).map(([method, forwarded]) => [method, path, forwarded] as [string, string, boolean]),
)

// THE COLLECTING RULE (tj-grna9p.177 here, tj-grna9p.184 in the Caddyfile): test_web_edge.py's
// _COLLECTING_TABLE, name for name, each path's PATCH forwarded exactly when the Caddy regex accepts it.
const COLLECTING = `${DATASETS}/${LOWER_UUID}/collecting`
const MIXED_UUID = '0f8FAD5b-D9cb-469F-a165-70867728950E'
const COLLECTING_TABLE: [string, string, boolean][] = [
  ['lower', COLLECTING, true],
  ['upper-uuid', `${DATASETS}/${UPPER_UUID}/collecting`, true],
  ['mixed-uuid', `${DATASETS}/${MIXED_UUID}/collecting`, true],
  ['upper-suffix', `${DATASETS}/${LOWER_UUID}/COLLECTING`, false],
  ['title-suffix', `${DATASETS}/${LOWER_UUID}/Collecting`, false],
  ['trailing-slash', `${COLLECTING}/`, false],
  ['extra-segment', `${COLLECTING}/x`, false],
  ['suffix-longer', `${COLLECTING}x`, false],
  ['suffix-shorter', `${DATASETS}/${LOWER_UUID}/collectin`, false],
  ['double-slash', `${DATASETS}/${LOWER_UUID}//collecting`, false],
  ['segment-before', `${DATASETS}/x/${LOWER_UUID}/collecting`, false],
  ['not-a-uuid', `${DATASETS}/not-a-uuid/collecting`, false],
  ['uuid-no-dashes', `${DATASETS}/${LOWER_UUID.replaceAll('-', '')}/collecting`, false],
  ['uuid-short', `${DATASETS}/${LOWER_UUID.slice(0, -1)}/collecting`, false],
  ['uuid-long', `${DATASETS}/${LOWER_UUID}0/collecting`, false],
  ['uuid-non-hex', `${DATASETS}/${LOWER_UUID.slice(0, -1)}g/collecting`, false],
  ['encoded-slash', `${DATASETS}/${LOWER_UUID}%2Fcollecting`, false],
  ['encoded-suffix', `${DATASETS}/${LOWER_UUID}/%63ollecting`, false],
  ['dot-dot-segment', `${DATASETS}/${LOWER_UUID}/../${LOWER_UUID}/collecting`, false],
  ['dot-dot-after', `${COLLECTING}/../expiry`, false],
  ['encoded-dot-dot', `${DATASETS}/%2e%2e/${LOWER_UUID}/collecting`, false],
  ['dot-segment', `${DATASETS}/./${LOWER_UUID}/collecting`, false],
  ['trailing-newline', `${COLLECTING}\n`, false],
  ['prefix-in-front', `/x${COLLECTING}`, false],
  ['unprefixed', `/ui/v1/datasets/${LOWER_UUID}/collecting`, false],
  ['store-route', `/api/store/store/datasets/${LOWER_UUID}/collecting`, false],
  ['expiry', `${DATASETS}/${LOWER_UUID}/expiry`, false],
  ['the-dataset', `${DATASETS}/${LOWER_UUID}`, false],
]

// test_no_other_method_on_the_collecting_path_reaches_a_write_rule: GET and HEAD are the read rule's
// (data_store has no such read and answers it), every other method but PATCH is refused.
const COLLECTING_METHODS: [string, boolean][] = [
  ['PATCH', true],
  ['GET', true],
  ['HEAD', true],
  ['PUT', false],
  ['POST', false],
  ['DELETE', false],
  ['OPTIONS', false],
  ['CONNECT', false],
  ['TRACE', false],
]

// test_the_wrong_method_or_a_non_ui_route_is_not_forwarded
const REFUSED_OTHER: [string, string, string][] = [
  ['delete-on-expiry', 'DELETE', `${DATASETS}/${LOWER_UUID}/expiry`],
  ['patch-on-dataset', 'PATCH', `${DATASETS}/${LOWER_UUID}`],
  ['put-expiry', 'PUT', `${DATASETS}/${LOWER_UUID}/expiry`],
  ['put-dataset', 'PUT', `${DATASETS}/${LOWER_UUID}`],
  ['post-expiry', 'POST', `${DATASETS}/${LOWER_UUID}/expiry`],
  ['delete-on-collecting', 'DELETE', `${DATASETS}/${LOWER_UUID}/collecting`],
  ['put-collecting', 'PUT', `${DATASETS}/${LOWER_UUID}/collecting`],
  ['post-collecting', 'POST', `${DATASETS}/${LOWER_UUID}/collecting`],
  ['post-list', 'POST', DATASETS],
  ['options', 'OPTIONS', `${DATASETS}/${LOWER_UUID}`],
  ['get-store', 'GET', `/api/store/store/datasets/${LOWER_UUID}`],
  ['get-internal', 'GET', '/api/store/internal/asset-data'],
  ['post-dataset', 'POST', `${DATASETS}/${LOWER_UUID}`],
  ['put-panels', 'PUT', PANELS],
  ['patch-panels', 'PATCH', PANELS],
  ['delete-panels', 'DELETE', PANELS],
  ['put-panel-settings', 'PUT', PANEL_SETTINGS_PATH],
  ['patch-panel-settings', 'PATCH', PANEL_SETTINGS_PATH],
  ['delete-panel-settings', 'DELETE', PANEL_SETTINGS_PATH],
  ['put-source-settings', 'PUT', SOURCE_SETTINGS_PATH],
  ['patch-source-settings', 'PATCH', SOURCE_SETTINGS_PATH],
  ['delete-source-settings', 'DELETE', SOURCE_SETTINGS_PATH],
  ['patch-the-panel', 'PATCH', `${PANELS}/${LOWER_UUID}`],
  ['delete-the-panel', 'DELETE', `${PANELS}/${LOWER_UUID}`],
  ['put-the-panel', 'PUT', `${PANELS}/${LOWER_UUID}`],
  ['options-panels', 'OPTIONS', PANELS],
  // test_web_edge.py's lower-case-post-panels, title-case-post-panel-settings and
  // lower-case-post-source-settings are NOT here: Caddy compares the method as sent, the dev edge
  // upper-cases it (as for PATCH and DELETE, see DEV_FORWARDED), and Node's parser delivers only
  // upper-case methods anyway (devProxyServer.spec.ts: a raw lower-case method is the server's own 400).
]

// The dev edge's own additions, from the build brief: the events socket, the query string, the method's case.
const DEV_FORWARDED: [string, string, string][] = [
  ['events-upgrade-is-a-get', 'GET', '/api/store/ui/v1/events'],
  ['get-the-list', 'GET', DATASETS],
  ['head-on-a-dataset', 'HEAD', `${DATASETS}/${LOWER_UUID}`],
  ['get-with-a-query', 'GET', `${DATASETS}/${LOWER_UUID}/bars?start=2024-01-01&end=2024-02-01`],
  ['patch-with-a-query', 'PATCH', `${DATASETS}/${LOWER_UUID}/expiry?x=1`],
  ['delete-with-a-query', 'DELETE', `${DATASETS}/${UPPER_UUID}?x=/expiry`],
  ['lower-case-get', 'get', '/api/store/ui/v1/config'],
  ['lower-case-head', 'head', '/api/store/ui/v1/config'],
  ['lower-case-patch', 'patch', `${DATASETS}/${LOWER_UUID}/expiry`],
  ['lower-case-patch-collecting', 'patch', `${DATASETS}/${LOWER_UUID}/collecting`],
  ['patch-collecting-with-a-query', 'PATCH', `${DATASETS}/${UPPER_UUID}/collecting?x=1`],
  ['mixed-case-delete', 'Delete', `${DATASETS}/${LOWER_UUID}`],
  ['lower-case-post-panels', 'post', PANELS],
  ['title-case-post-panel-settings', 'Post', PANEL_SETTINGS_PATH],
  ['lower-case-post-source-settings', 'post', SOURCE_SETTINGS_PATH],
  ['post-panels-with-a-query', 'POST', `${PANELS}?x=/settings-versions`],
  ['post-source-settings-with-a-query', 'POST', `${SOURCE_SETTINGS_PATH}?cursor=a`],
]
const DEV_REFUSED: [string, string, string][] = [
  ['get-store-x', 'GET', '/api/store/store/x'],
  ['get-ui-v1-no-slash', 'GET', '/api/store/ui/v1'],
  ['get-store-root', 'GET', '/api/store'],
  ['get-ui-v1-in-the-query-only', 'GET', '/api/store/store/x?/api/store/ui/v1/config'],
  ['patch-expiry-in-the-query-only', 'PATCH', `${DATASETS}/${LOWER_UUID}?/expiry`],
  ['post-on-a-read-path', 'POST', '/api/store/ui/v1/config'],
  ['post-events', 'POST', '/api/store/ui/v1/events'],
  ['lower-case-put', 'put', `${DATASETS}/${LOWER_UUID}/expiry`],
  ['lower-case-put-collecting', 'put', `${DATASETS}/${LOWER_UUID}/collecting`],
  ['patch-collecting-in-the-query-only', 'PATCH', `${DATASETS}/${LOWER_UUID}?/collecting`],
  ['lower-case-post', 'post', DATASETS],
  ['patch-on-the-list', 'PATCH', DATASETS],
  ['delete-on-the-list', 'DELETE', DATASETS],
  ['empty-method', '', '/api/store/ui/v1/config'],
  ['post-panel-in-the-query-only', 'POST', `${PANELS}/${LOWER_UUID}?/settings-versions`],
  ['post-the-panel-members', 'POST', `${PANELS}/${LOWER_UUID}/members`],
  ['post-boundaries', 'POST', `${PANELS}/${LOWER_UUID}/boundaries`],
  ['post-asset-list-history', 'POST', `${PANELS}/${LOWER_UUID}/asset-list-history`],
  ['post-corporate-actions', 'POST', `${DATASETS}/${LOWER_UUID}/corporate-actions`],
  ['lower-case-put-panels', 'put', PANELS],
  ['lower-case-patch-panel-settings', 'patch', PANEL_SETTINGS_PATH],
]

describe('isAllowedStoreRequest: the Caddyfile cases (test_web_edge.py)', () => {
  it.each(FORWARDED)('forwards %s', (_id, method, path) => {
    expect(mod.isAllowedStoreRequest(method, path)).toBe(true)
  })

  it.each(REFUSED_WRITES)('refuses %s', (_id, method, path) => {
    expect(mod.isAllowedStoreRequest(method, path)).toBe(false)
  })

  it.each(REFUSED_OTHER)('refuses %s', (_id, method, path) => {
    expect(mod.isAllowedStoreRequest(method, path)).toBe(false)
  })

  it.each(REFUSED_WRITE_SPELLING_CASES)('refuses %s', (_id, method, path) => {
    expect(mod.isAllowedStoreRequest(method, path)).toBe(false)
  })

  // The table is the collecting regex's; its 'expiry' row is a PATCH the expiry rule forwards instead.
  it.each(COLLECTING_TABLE)('PATCH on the collecting table row %s (%s) is forwarded as the table says', (id, path, accepted) => {
    expect(mod.isAllowedStoreRequest('PATCH', path)).toBe(accepted || id === 'expiry')
  })

  it.each(COLLECTING_METHODS)('%s on one dataset /collecting is forwarded: %s', (method, forwarded) => {
    expect(mod.isAllowedStoreRequest(method, COLLECTING)).toBe(forwarded)
  })

  it.each(PANEL_PATH_METHODS)('%s on the panel write path %s is forwarded: %s', (method, path, forwarded) => {
    expect(mod.isAllowedStoreRequest(method, path)).toBe(forwarded)
  })

  // POST is the panel writes' alone: every POST row of the tables that is forwarded is one of the three shapes.
  it('POST forwards on exactly the three panel write shapes among every path of the tables', () => {
    const all = [...FORWARDED, ...REFUSED_WRITES, ...REFUSED_WRITE_SPELLING_CASES, ...REFUSED_OTHER, ...DEV_FORWARDED, ...DEV_REFUSED]
    const posted = [...new Set(all.map((c) => c[2].split('?')[0]!))].filter((p) => mod.isAllowedStoreRequest('POST', p))
    expect(posted.sort()).toEqual(
      [PANELS, PANEL_SETTINGS_PATH, `${PANELS}/${UPPER_UUID}/settings-versions`, SOURCE_SETTINGS_PATH, `${PANEL_SOURCES}/DATA_SOURCE_IB_API/settings-versions`].sort(),
    )
  })
})

describe('isAllowedStoreRequest: the dev edge additions', () => {
  it.each(DEV_FORWARDED)('forwards %s', (_id, method, path) => {
    expect(mod.isAllowedStoreRequest(method, path)).toBe(true)
  })

  it.each(DEV_REFUSED)('refuses %s', (_id, method, path) => {
    expect(mod.isAllowedStoreRequest(method, path)).toBe(false)
  })
})

// The write rules, evaluated from the Caddyfile's own text: each path_regexp compiled as written (RE2 and
// JavaScript agree on this syntax), over every path in the table. A regex changed on one side only reds.
describe('the write rules agree with the Caddyfile regexes, path by path', () => {
  const caddy = readFileSync(CADDYFILE, 'utf-8')
  // A matcher block's lines: from `@name {` to the first line that is a lone `}` (the regexes hold braces).
  const blockOf = (matcher: string): string[] => {
    const lines = caddy.split('\n').map((l) => l.trim())
    const start = lines.indexOf(`@${matcher} {`)
    expect(start, `no @${matcher} block in the Caddyfile`).toBeGreaterThanOrEqual(0)
    return lines.slice(start + 1, lines.indexOf('}', start))
  }
  const regexOf = (matcher: string): RegExp => {
    const source = blockOf(matcher)
      .find((l) => l.startsWith('path_regexp '))
      ?.split(' ')[1]
    expect(source, `@${matcher} carries no path_regexp`).toBeDefined()
    return new RegExp(source as string)
  }
  const paths = [
    ...new Set([
      ...[...FORWARDED, ...REFUSED_WRITES, ...REFUSED_WRITE_SPELLING_CASES, ...REFUSED_OTHER, ...DEV_FORWARDED, ...DEV_REFUSED].map(
        (c) => c[2],
      ),
      ...COLLECTING_TABLE.map((c) => c[1]),
    ]),
  ].map((p) => (p.includes('?') ? p.slice(0, p.indexOf('?')) : p))

  // A method's rules together decide it: PATCH is @store_ui_expiry or @store_ui_collecting.
  const PANEL_MATCHERS = ['store_ui_panel_create', 'store_ui_panel_settings', 'store_ui_source_settings']

  it.each([
    ['PATCH', ['store_ui_expiry', 'store_ui_collecting']],
    ['DELETE', ['store_ui_delete']],
    ['POST', PANEL_MATCHERS],
  ])('%s is decided by the Caddyfile regexes of %j exactly as isAllowedStoreRequest does', (method, matchers) => {
    const regexes = (matchers as string[]).map(regexOf)
    const disagreements = paths.filter((p) => regexes.some((r) => r.test(p)) !== mod.isAllowedStoreRequest(method as string, p))
    expect(disagreements).toEqual([])
  })

  // The create shape is one literal path (its look-alikes are refused), so it accepts exactly one.
  it.each([
    ['store_ui_expiry', 2],
    ['store_ui_collecting', 2],
    ['store_ui_delete', 2],
    ['store_ui_panel_create', 1],
    ['store_ui_panel_settings', 2],
    ['store_ui_source_settings', 2],
  ])('@%s accepts at least %i paths of the table', (matcher, least) => {
    expect(paths.filter((p) => regexOf(matcher).test(p)).length).toBeGreaterThanOrEqual(least as number)
  })

  it('no path is accepted by two POST rules, so none can stand in for another', () => {
    const regexes = PANEL_MATCHERS.map(regexOf)
    expect(paths.filter((p) => regexes.filter((r) => r.test(p)).length > 1)).toEqual([])
  })

  // The Caddyfile names POST in exactly the three panel matchers, each a POST-only rule; the dev edge's POST
  // branch is checked against those three above, so a fourth POST rule on either side reds.
  it('the Caddyfile names POST in exactly the three panel matchers, each with method POST alone', () => {
    const lines = caddy.split('\n').map((l) => l.trim())
    const posting = lines
      .map((line, index) => ({ line, index }))
      .filter(({ line }) => line.startsWith('method ') && line.split(/\s+/).includes('POST'))
      .map(({ index }) => lines.slice(0, index).reverse().find((l) => /^@\S+ \{$/.test(l)))
    expect(posting).toEqual(PANEL_MATCHERS.map((m) => `@${m} {`))
    for (const matcher of PANEL_MATCHERS) expect(blockOf(matcher)[0]).toBe('method POST')
  })

  it('no path is accepted by both PATCH rules, so neither can stand in for the other', () => {
    const [expiry, collecting] = [regexOf('store_ui_expiry'), regexOf('store_ui_collecting')]
    expect(paths.filter((p) => expiry.test(p) && collecting.test(p))).toEqual([])
  })

  it.each(COLLECTING_TABLE)('@store_ui_collecting decides the collecting row %s (%s) as the table says', (_id, path, accepted) => {
    expect(regexOf('store_ui_collecting').test(path)).toBe(accepted)
  })

  it('the collecting rule is PATCH on the write-order prefix, as the expiry rule is', () => {
    expect(blockOf('store_ui_collecting').filter((l) => !l.startsWith('path_regexp '))).toEqual(
      blockOf('store_ui_expiry').filter((l) => !l.startsWith('path_regexp ')),
    )
    expect(blockOf('store_ui_collecting')[0]).toBe('method PATCH')
  })

  // The literal claim of vite.config.ts ("character for character the Caddyfile's ... regexes"): each
  // vite write regex's template, its ${UUID} filled from vite's own UUID constant, is string-equal to the
  // matcher's path_regexp as the Caddyfile spells it.
  it.each([
    ['EXPIRY_PATH', 'store_ui_expiry'],
    ['COLLECTING_PATH', 'store_ui_collecting'],
    ['DELETE_PATH', 'store_ui_delete'],
    ['PANEL_SETTINGS_PATH', 'store_ui_panel_settings'],
  ])('vite.config.ts %s, its UUID filled in, is the Caddyfile @%s path_regexp character for character', (name, matcher) => {
    const vite = readFileSync(`${WEB_ROOT}vite.config.ts`, 'utf-8')
    const uuid = vite.match(/^const UUID = '([^']+)'$/m)?.[1]
    const template = vite.match(new RegExp(`^const ${name} = new RegExp\\(\`([^\`]+)\`\\)$`, 'm'))?.[1]
    expect(uuid, 'no UUID constant in vite.config.ts').toBeDefined()
    expect(template, `no ${name} in vite.config.ts`).toBeDefined()
    const caddySource = blockOf(matcher)
      .find((l) => l.startsWith('path_regexp '))
      ?.slice('path_regexp '.length)
    expect((template as string).replaceAll('${UUID}', uuid as string)).toBe(caddySource)
  })

  // The two panel regexes vite.config.ts writes as regex literals: the literal's body, its escaped
  // slashes unescaped (the only escape the literal is allowed), is the matcher's path_regexp as spelled.
  it.each([
    ['PANEL_CREATE_PATH', 'store_ui_panel_create'],
    ['PANEL_SOURCE_SETTINGS_PATH', 'store_ui_source_settings'],
  ])('vite.config.ts %s, a regex literal, is the Caddyfile @%s path_regexp character for character', (name, matcher) => {
    const vite = readFileSync(`${WEB_ROOT}vite.config.ts`, 'utf-8')
    const literal = vite.match(new RegExp(`^const ${name} = /(.+)/$`, 'm'))?.[1]
    expect(literal, `no ${name} regex literal in vite.config.ts`).toBeDefined()
    expect((literal as string).replaceAll('\\/', ''), 'an escape other than \\/').not.toContain('\\')
    const caddySource = blockOf(matcher)
      .find((l) => l.startsWith('path_regexp '))
      ?.slice('path_regexp '.length)
    expect((literal as string).replaceAll('\\/', '/')).toBe(caddySource)
  })

  it('vite.config.ts declares the six write regexes and its POST branch tests the three panel ones alone', () => {
    const vite = readFileSync(`${WEB_ROOT}vite.config.ts`, 'utf-8')
    const declared = [...vite.matchAll(/^const ([A-Z_]+_PATH) = /gm)].map((m) => m[1])
    expect(declared).toEqual([
      'EXPIRY_PATH',
      'COLLECTING_PATH',
      'DELETE_PATH',
      'PANEL_CREATE_PATH',
      'PANEL_SETTINGS_PATH',
      'PANEL_SOURCE_SETTINGS_PATH',
    ])
    const post = vite.split('\n').filter((l) => /\bm === 'POST'/.test(l))
    expect(post).toHaveLength(1)
    expect([...post[0]!.matchAll(/([A-Z_]+_PATH)\.test\(p\)/g)].map((m) => m[1])).toEqual([
      'PANEL_CREATE_PATH',
      'PANEL_SETTINGS_PATH',
      'PANEL_SOURCE_SETTINGS_PATH',
    ])
  })

  it('the read rule is GET and HEAD on the prefix the dev edge uses', () => {
    expect(blockOf('store_ui')).toEqual(['method GET HEAD', 'path /api/store/ui/v1/*'])
  })

  it('vite.config.ts writes the UUID class character for character as the Caddyfile', () => {
    const vite = readFileSync(`${WEB_ROOT}vite.config.ts`, 'utf-8')
    const uuid = vite.match(/const UUID = '([^']+)'/)?.[1]
    const uuidClass = '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
    expect(uuid).toBe(uuidClass)
    expect(caddy).toContain(`path_regexp ^/api/store/ui/v1/datasets/${uuidClass}/expiry$`)
    expect(caddy).toContain(`path_regexp ^/api/store/ui/v1/datasets/${uuidClass}/collecting$`)
    expect(caddy).toContain(`path_regexp ^/api/store/ui/v1/datasets/${uuidClass}$`)
    expect(vite).toContain('`^/api/store/ui/v1/datasets/${UUID}/expiry$`')
    expect(vite).toContain('`^/api/store/ui/v1/datasets/${UUID}/collecting$`')
    expect(vite).toContain('`^/api/store/ui/v1/datasets/${UUID}$`')
  })
})

describe('isTraversalPath: Caddy @traversal (path *..*)', () => {
  it.each([
    '/api/store/ui/v1/../store/datasets',
    '/api/store/ui/v1/datasets/..',
    `${DATASETS}/${LOWER_UUID}/../${UPPER_UUID}`,
    '/api/store/..',
    '/api/store/ui/v1/a..b',
  ])('a path containing .. is traversal (%s)', (path) => {
    expect(mod.isTraversalPath(path)).toBe(true)
  })

  it.each([
    '/api/store/ui/v1/config',
    `${DATASETS}/${LOWER_UUID}/bars?q=..`,
    `${DATASETS}/${LOWER_UUID}/bars?q=%2e%2e%2f%5c%zz`,
    '/api/store/ui/v1/a.b',
    '/api/store/ui/v1/%2e',
    '/api/store/ui/v1/%41',
    '/api/store/ui/v1/%2541',
    '/api/store/ui/v1/%252541',
  ])('a path that decodes, within three passes, to no .. is not, the query string aside (%s)', (path) => {
    expect(mod.isTraversalPath(path)).toBe(false)
  })

  // Decoded to a fixed point: the bound is three decodes to reach it, so one more level of %25 is refused.
  it.each([
    ['%2e%2e', '/api/store/ui/v1/%2e%2e'],
    ['%2E%2e mixed case', '/api/store/ui/v1/%2E%2e'],
    ['.%2e', '/api/store/ui/v1/.%2e/x'],
    ['%252e%252e double-encoded', '/api/store/ui/v1/%252e%252e'],
    ['%25252e%25252e triple-encoded', '/api/store/ui/v1/%25252e%25252e'],
    ['%2f encoded slash', '/api/store/ui/v1/a%2fb'],
    ['%2F encoded slash', '/api/store/ui/v1/a%2Fb'],
    ['%252f double-encoded slash', '/api/store/ui/v1/a%252fb'],
    ['%5c encoded backslash', '/api/store/ui/v1/a%5cb'],
    ['backslash', '/api/store/ui/v1/a\\b'],
    ['%zz malformed', '/api/store/ui/v1/%zz'],
    ['%2 truncated', '/api/store/ui/v1/a%2'],
    ['%E0%A4%A malformed UTF-8', '/api/store/ui/v1/%E0%A4%A'],
    ['still changing after three decodes', '/api/store/ui/v1/%25252541'],
  ])('a %s path is traversal', (_id, path) => {
    expect(mod.isTraversalPath(path)).toBe(true)
  })
})

// ---------------------------------------------------------------------------------------------------
// THE PROXY ENTRY'S bypass, through the resolved config (storeBypass itself is not exported)

interface FakeRes {
  statusCode: number
  headers: Record<string, string>
  body: string | undefined
  ended: boolean
  setHeader(n: string, v: string): void
  end(b?: string): void
}

function fakeRes(): FakeRes {
  return {
    statusCode: 200,
    headers: {},
    body: undefined,
    ended: false,
    setHeader(n, v) {
      this.headers[n.toLowerCase()] = v
    },
    end(b) {
      this.body = b
      this.ended = true
    },
  }
}

type Bypass = (req: { method?: string; url?: string }, res: FakeRes | undefined) => string | false | undefined

describe('the /api/store bypass', () => {
  const bypass = (): Bypass => storeProxy.bypass as unknown as Bypass

  it('is wired on the proxy entry, before its rewrite', () => {
    expect(typeof storeProxy.bypass).toBe('function')
  })

  it.each([...FORWARDED, ...DEV_FORWARDED])('forwards %s: undefined, res untouched', (_id, method, url) => {
    const res = fakeRes()
    expect(bypass()({ method, url }, res)).toBeUndefined()
    expect(res.ended).toBe(false)
  })

  it.each([...REFUSED_WRITES, ...REFUSED_OTHER, ...DEV_REFUSED])(
    'answers %s 404 not found itself, returning the url so Vite stops',
    (_id, method, url) => {
      const res = fakeRes()
      expect(bypass()({ method, url }, res)).toBe(url)
      expect(res).toMatchObject({ statusCode: 404, body: 'not found', ended: true })
      expect(res.headers['content-type']).toBe('text/plain')
    },
  )

  it.each([
    ['GET', '/api/store/ui/v1/../store/datasets'],
    ['DELETE', `${DATASETS}/${LOWER_UUID}/..`],
    ['POST', '/api/store/../internal/x'],
    ['GET', '/api/store/ui/v1/datasets/..?x=1'],
    // Not a dot segment, so cleaning keeps it; Caddy's `path *..*` still answers it 400.
    ['GET', '/api/store/ui/v1/a..b'],
  ])('answers a %s on a .. path 400 bad request, ahead of the allow-list (%s)', (method, url) => {
    const res = fakeRes()
    expect(bypass()({ method, url }, res)).toBe(url)
    expect(res).toMatchObject({ statusCode: 400, body: 'bad request', ended: true })
  })

  // THE DECODED, CLEANED PATH (round 2, 0c0543a). Caddy decodes and cleans the path before it matches, and
  // http-proxy resolves %2e%2e as '..' (devProxyServer.spec.ts showed round 1 forwarding these to /store,
  // /internal and /docs with the secret). The bypass now answers 400 for any spelling that is not its own
  // decoded, cleaned form, and false on an upgrade.
  const REFUSED_SPELLINGS: [string, string][] = [
    ['encoded-dots', '/api/store/ui/v1/%2e%2e/%2e%2e/store/datasets/x'],
    ['upper-encoded-dots', '/api/store/ui/v1/%2E%2E/internal/asset-data'],
    ['half-encoded-dots', '/api/store/ui/v1/.%2e/docs'],
    ['other-half-encoded-dots', '/api/store/ui/v1/%2e./docs'],
    ['mixed-case-encoded-dots', '/api/store/ui/v1/%2E%2e/%2e%2E/store'],
    ['double-encoded-dots', '/api/store/ui/v1/%252e%252e/%252e%252e/store/datasets/x'],
    ['triple-encoded-dots', '/api/store/ui/v1/%25252e%25252e/store'],
    ['encoded-slash', `${DATASETS}%2f${LOWER_UUID}`],
    ['upper-encoded-slash', '/api/store/ui/v1/x%2F..%2Fstore'],
    ['encoded-backslash', '/api/store/ui/v1/config%5cx'],
    ['upper-encoded-backslash', '/api/store/ui/v1/config%5Cx'],
    ['backslash', '/api/store/ui/v1/..\\..\\store'],
    ['backslash-no-dots', '/api/store/ui/v1/config\\x'],
    ['malformed-escape', '/api/store/ui/v1/%zz'],
    ['truncated-escape', '/api/store/ui/v1/config%2'],
    ['still-changing-after-three-decodes', '/api/store/ui/v1/%25252541'],
    ['doubled-slash', '/api/store/ui/v1//config'],
    ['doubled-slash-in-the-prefix', '/api/store//ui/v1/config'],
    ['dot-segment', '/api/store/ui/v1/./config'],
    ['trailing-dot-segment', '/api/store/ui/v1/config/.'],
    ['encoded-letter', '/api/store/ui/v1/%63onfig'],
    ['encoded-nul', '/api/store/ui/v1/config%00'],
    ['encoded-newline', `${DATASETS}/${LOWER_UUID}%0a`],
    ['encoded-uuid-digit', `${DATASETS}/%30${LOWER_UUID.slice(1)}`],
  ]

  it.each(REFUSED_SPELLINGS)('answers GET %s 400 bad request', (_id, url) => {
    const res = fakeRes()
    expect(bypass()({ method: 'GET', url }, res)).toBe(url)
    expect(res).toMatchObject({ statusCode: 400, body: 'bad request', ended: true })
  })

  it.each(REFUSED_SPELLINGS)('answers the same spelling (%s) 400 for every method', (_id, url) => {
    for (const method of ['HEAD', 'PATCH', 'DELETE', 'POST']) {
      const res = fakeRes()
      bypass()({ method, url }, res)
      expect(res.statusCode, method).toBe(400)
    }
  })

  it.each(REFUSED_SPELLINGS)('refuses an upgrade on %s with false', (_id, url) => {
    expect(bypass()({ method: 'GET', url }, undefined)).toBe(false)
  })

  it.each(REFUSED_WRITE_SPELLING_CASES)('answers %s 400 bad request, never forwarded', (_id, method, url) => {
    const res = fakeRes()
    expect(bypass()({ method, url }, res)).toBe(url)
    expect(res).toMatchObject({ statusCode: 400, body: 'bad request', ended: true })
  })

  it.each([
    ['PATCH', `${DATASETS}/${LOWER_UUID}/%2e/expiry`],
    ['PATCH', `${DATASETS}/${LOWER_UUID}//expiry`],
    ['PATCH', `${DATASETS}/${LOWER_UUID}/%2e/collecting`],
    ['PATCH', `${DATASETS}/${LOWER_UUID}//collecting`],
    ['PATCH', `${DATASETS}/${LOWER_UUID}/%63ollecting`],
    ['DELETE', `${DATASETS}/%30${LOWER_UUID.slice(1)}`],
    ['DELETE', `${DATASETS}/./${UPPER_UUID}`],
  ])('answers a write %s %s 400', (method, url) => {
    const res = fakeRes()
    bypass()({ method, url }, res)
    expect(res.statusCode).toBe(400)
  })

  it.each([
    ['trailing-newline stays a 404', 'DELETE', `${DATASETS}/${LOWER_UUID}\n`],
    ['trailing-slash stays a 404', 'DELETE', `${DATASETS}/${LOWER_UUID}/`],
    ['an escape in the query only is a 404 on the path', 'GET', '/api/store/store/x?a=%2e%2e%2f'],
  ])('%s', (_id, method, url) => {
    const res = fakeRes()
    expect(bypass()({ method, url }, res)).toBe(url)
    expect(res.statusCode).toBe(404)
  })

  it.each([
    ['GET', `${DATASETS}/${LOWER_UUID}/bars?start=%2e%2e&x=a%2fb%5c`],
    ['PATCH', `${DATASETS}/${UPPER_UUID}/expiry?x=%zz`],
  ])('an escape in the query string is not the path: %s %s forwards', (method, url) => {
    expect(bypass()({ method, url }, fakeRes())).toBeUndefined()
  })

  it('treats a missing method or url as refused', () => {
    const res = fakeRes()
    expect(bypass()({}, res)).toBe('')
    expect(res.statusCode).toBe(404)
  })

  // A WebSocket upgrade: Vite calls bypass with no res, and answers `false` with a bare 404 on the socket.
  it('passes the events socket upgrade', () => {
    expect(bypass()({ method: 'GET', url: '/api/store/ui/v1/events' }, undefined)).toBeUndefined()
  })

  it.each([
    ['GET', '/api/store/store/events'],
    ['GET', '/api/store/ui/v2/events'],
    ['POST', '/api/store/ui/v1/events'],
    ['GET', '/api/store/ui/v1/../events'],
  ])('refuses an upgrade %s %s with false', (method, url) => {
    expect(bypass()({ method, url }, undefined)).toBe(false)
  })
})

// ---------------------------------------------------------------------------------------------------
// THE SECRET

// A real node OutgoingMessage, the base of the ClientRequest http-proxy hands to proxyReq: its header
// names are case-insensitive as on the wire.
function upstream(headers: Record<string, string> = {}): OutgoingMessage {
  const message = new OutgoingMessage()
  for (const [name, value] of Object.entries(headers)) message.setHeader(name, value)
  return message
}

describe('applyInstanceSecret', () => {
  it.each(['X-Instance-Secret', 'x-instance-secret', 'X-INSTANCE-SECRET', 'x-Instance-SECRET'])(
    'replaces a browser-sent %s with the env value',
    (name) => {
      const message = upstream({ [name]: 'browser-sent', accept: 'application/json' })
      mod.applyInstanceSecret(message, { [SECRET_ENV]: FAKE })
      expect(message.getHeader(HEADER)).toBe(FAKE)
      expect(message.getHeaders()).toEqual({ accept: 'application/json', 'x-instance-secret': FAKE })
    },
  )

  it.each([
    ['unset', {}],
    ['undefined', { [SECRET_ENV]: undefined }],
    ['empty', { [SECRET_ENV]: '' }],
  ])('sets nothing when the value is %s, and still drops a browser-sent header', (_id, env) => {
    const message = upstream({ 'x-instance-secret': 'browser-sent', cookie: 'a=b' })
    mod.applyInstanceSecret(message, env)
    expect(message.hasHeader(HEADER)).toBe(false)
    expect(message.getHeaders()).toEqual({ cookie: 'a=b' })
  })

  it('sets the header on a request that carried none, touching no other header', () => {
    const message = upstream({ host: 'data_store', 'content-type': 'application/json', 'x-other': '1' })
    mod.applyInstanceSecret(message, { [SECRET_ENV]: FAKE })
    expect(message.getHeaders()).toEqual({
      host: 'data_store',
      'content-type': 'application/json',
      'x-other': '1',
      'x-instance-secret': FAKE,
    })
  })

  it('removes before it sets: the browser value never reaches setHeader', () => {
    const calls: string[] = []
    mod.applyInstanceSecret(
      {
        removeHeader: (n) => calls.push(`remove ${n}`),
        setHeader: (n, v) => calls.push(`set ${n} ${v}`),
      },
      { [SECRET_ENV]: FAKE },
    )
    expect(calls).toEqual([`remove ${HEADER}`, `set ${HEADER} ${FAKE}`])
  })

  it('reads process.env by default, at call time', () => {
    process.env[SECRET_ENV] = FAKE
    const set = upstream({ 'x-instance-secret': 'browser-sent' })
    mod.applyInstanceSecret(set)
    expect(set.getHeader(HEADER)).toBe(FAKE)
    delete process.env[SECRET_ENV]
    const unset = upstream({ 'x-instance-secret': 'browser-sent' })
    mod.applyInstanceSecret(unset)
    expect(unset.hasHeader(HEADER)).toBe(false)
  })

  it('logs nothing', () => {
    const spies = (['log', 'info', 'warn', 'error', 'debug', 'trace'] as const).map((m) =>
      vi.spyOn(console, m).mockImplementation(() => {}),
    )
    const out = vi.spyOn(process.stdout, 'write').mockImplementation(() => true)
    const err = vi.spyOn(process.stderr, 'write').mockImplementation(() => true)
    mod.applyInstanceSecret(upstream({ 'x-instance-secret': 'browser-sent' }), { [SECRET_ENV]: FAKE })
    mod.applyInstanceSecret(upstream(), {})
    for (const spy of [...spies, out, err]) expect(spy).not.toHaveBeenCalled()
  })
})

describe('the configure hook', () => {
  function hooks(): Record<string, (proxyReq: OutgoingMessage) => void> {
    const handlers: Record<string, (proxyReq: OutgoingMessage) => void> = {}
    const fakeProxy = { on: (event: string, handler: (proxyReq: OutgoingMessage) => void) => (handlers[event] = handler) }
    ;(storeProxy.configure as unknown as (proxy: typeof fakeProxy, options: ProxyOptions) => void)(fakeProxy, storeProxy)
    return handlers
  }

  it('registers proxyReq and proxyReqWs, and nothing else', () => {
    expect(Object.keys(hooks()).sort()).toEqual(['proxyReq', 'proxyReqWs'])
  })

  it.each(['proxyReq', 'proxyReqWs'])('%s replaces a browser value with the dev server env value', (event) => {
    process.env[SECRET_ENV] = FAKE
    const message = upstream({ 'X-Instance-Secret': 'browser-sent' })
    hooks()[event](message)
    expect(message.getHeader(HEADER)).toBe(FAKE)
  })

  it.each(['proxyReq', 'proxyReqWs'])('%s sets nothing when the env value is empty', (event) => {
    process.env[SECRET_ENV] = ''
    const message = upstream({ 'X-Instance-Secret': 'browser-sent' })
    hooks()[event](message)
    expect(message.hasHeader(HEADER)).toBe(false)
  })
})

// ---------------------------------------------------------------------------------------------------
// NO VITE_ SECRET NAME: Vite bundles VITE_* (and anything under import.meta.env or define) into the browser

function filesUnder(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = `${dir}/${name}`
    return statSync(path).isDirectory() ? filesUnder(path) : [path]
  })
}

describe('the secret never has a client-exposed name', () => {
  const SELF = fileURLToPath(import.meta.url)
  const scanned = [
    ...filesUnder(`${WEB_ROOT}src`).filter((p) => p !== SELF),
    `${WEB_ROOT}vite.config.ts`,
    `${WEB_ROOT}vitest.config.ts`,
    `${WEB_ROOT}env.d.ts`,
    `${WEB_ROOT}index.html`,
    `${WEB_ROOT}package.json`,
    `${REPO_ROOT}docker-compose.web.dev.yaml`,
    `${REPO_ROOT}docker-compose.web.yaml`,
  ]
  const text = (path: string) => readFileSync(path, 'utf-8')

  it('scans a real tree', () => {
    expect(scanned.length).toBeGreaterThan(50)
  })

  it('no VITE_-prefixed name mentions a secret or the instance', () => {
    const names = new Set(scanned.flatMap((p) => text(p).match(/\bVITE_[A-Za-z0-9_]+/g) ?? []))
    expect([...names].filter((n) => /SECRET|INSTANCE|WRITE/i.test(n))).toEqual([])
    expect(names.size).toBeGreaterThan(0)
  })

  it('no browser source names INSTANCE_WRITE_SECRET', () => {
    const offending = filesUnder(`${WEB_ROOT}src`)
      .filter((p) => p !== SELF && !p.endsWith('.spec.ts'))
      .filter((p) => text(p).includes(SECRET_ENV))
    expect(offending).toEqual([])
  })

  it('vite.config.ts reads the secret from process.env only: no import.meta.env, define or envPrefix route', () => {
    const vite = text(`${WEB_ROOT}vite.config.ts`)
    const isComment = (line: string) => /^(\/\/|\/\*|\*)/.test(line.trim())
    const mentions = vite.split('\n').filter((line) => line.includes(SECRET_ENV) && !isComment(line))
    expect(mentions.map((l) => l.trim())).toEqual(['const secret = env.INSTANCE_WRITE_SECRET'])
    expect(vite).toContain('env: Readonly<Record<string, string | undefined>> = process.env,')
    const code = vite.split('\n').filter((line) => !isComment(line))
    expect(code.filter((line) => /import\.meta\.env/.test(line))).toEqual([])
    expect(JSON.stringify(mod.default.define ?? {})).not.toMatch(/SECRET|INSTANCE/i)
    expect(mod.default.envPrefix).toBeUndefined()
  })

  it('a config loaded with the secret set holds no copy of its value', async () => {
    process.env[SECRET_ENV] = FAKE
    vi.resetModules()
    const loaded = (await import(/* @vite-ignore */ VITE_CONFIG_URL)) as ViteConfigModule
    expect(JSON.stringify(loaded.default)).not.toContain(FAKE)
    expect(JSON.stringify(loaded.default.define ?? {})).not.toContain(FAKE)
  })
})
