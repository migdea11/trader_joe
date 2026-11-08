// panelModel.ts (tj-7zjov1.37): the words for the panel enums, the settings draft and its conversion to a
// request, the marker placement and the join of the boundary read's markers with the settings versions'
// effective passes (by version number; a version no pass has run under is pending, not a boundary), and
// the words a refused panel write is shown in (the server's text is never shown, tj-fa1rpu D8).
import { create, toJson } from '@bufbuild/protobuf'
import { timestampFromMs } from '@bufbuild/protobuf/wkt'
import { describe, expect, it } from 'vitest'

import { AssetType, DataSource } from '@generated/trader_joe/proto/market/v1/enums_pb'
import { DatasetHealthStatus } from '@generated/trader_joe/proto/ui/v1/data_pb'
import {
  AssetListDayState,
  EffectivePassSchema,
  PanelBoundariesSchema,
  PanelCadence,
  PanelMarkerKind,
  PanelMarkerSchema,
  PanelPassKind,
  PanelSettingsSchema,
  PanelSettingsVersionSchema,
  PanelSourceSettingsVersionSchema,
  PanelTransition,
  SavePanelSettingsRequestSchema,
} from '@generated/trader_joe/proto/ui/v1/panels_pb'

import { PANEL_REFUSAL } from '@/theme/copy'
import { HEALTH_BADGES } from '@/theme/semantics'

import {
  CADENCE_CHOICES,
  DEFAULT_CADENCE_LABEL,
  cadenceLabel,
  dayStateLabel,
  createFieldErrors,
  draftFieldErrors,
  draftFromSettings,
  draftProblem,
  draftToSettingsInit,
  effectivePassText,
  emptyDraft,
  markerLabel,
  markerPosition,
  memberHealthBadge,
  panelWriteNotice,
  parseCount,
  parseSymbols,
  settingsInitWithEnabled,
  stripMarkers,
  transitionLabel,
} from './panelModel'
import type { CreateInput, PanelDraft } from './panelModel'

const DAY = 86_400_000
const START = Date.UTC(2026, 7, 3)
const END = START + 100 * DAY

describe('the label helpers', () => {
  it('cadence: Daily, Weekly, Monthly; UNSPECIFIED is Unknown on a read (a create shows the default label)', () => {
    expect(CADENCE_CHOICES.map((c) => [c.value, c.label])).toEqual([
      [PanelCadence.DAILY, 'Daily'],
      [PanelCadence.WEEKLY, 'Weekly'],
      [PanelCadence.MONTHLY, 'Monthly'],
    ])
    expect(cadenceLabel(PanelCadence.MONTHLY)).toBe('Monthly')
    expect(cadenceLabel(PanelCadence.UNSPECIFIED)).toBe('Unknown')
    expect(DEFAULT_CADENCE_LABEL).toBe('Monthly (default)')
  })

  it.each([
    [PanelTransition.ENTERED, 'Entered'],
    [PanelTransition.CREATED, 'Created'],
    [PanelTransition.DEFERRED, 'Deferred'],
    [PanelTransition.REFUSED, 'Refused'],
    [PanelTransition.EXITED, 'Exited'],
    [PanelTransition.DRAINING, 'Draining'],
    [PanelTransition.DRAINED, 'Drained'],
    [PanelTransition.UNSPECIFIED, 'Unknown'],
  ])('transition %i reads %s', (status, label) => {
    expect(transitionLabel(status)).toBe(label)
  })

  it.each([
    [AssetListDayState.SAVED, 'Saved'],
    [AssetListDayState.MISSING, 'Missing'],
    [AssetListDayState.EMPTY, 'Empty'],
    [AssetListDayState.NOT_DUE, 'Not due'],
    [AssetListDayState.UNSPECIFIED, 'Unknown'],
  ])('day state %i reads %s', (state, label) => {
    expect(dayStateLabel(state)).toBe(label)
  })

  it.each([
    [PanelMarkerKind.PANEL_SETTINGS, 'Panel settings'],
    [PanelMarkerKind.SOURCE_SETTINGS, 'Source settings'],
    [PanelMarkerKind.COLLECT_STOP, 'Collect stopped'],
    [PanelMarkerKind.COLLECT_RESUME, 'Collect resumed'],
    [PanelMarkerKind.UNSPECIFIED, 'Marker'],
  ])('marker kind %i reads %s', (kind, label) => {
    expect(markerLabel(kind)).toBe(label)
  })

  it('effectivePassText names the pass kind and, when known, its start', () => {
    const at = Date.UTC(2026, 9, 30, 14)
    const fmt = (instant: number) => `@${instant}`
    expect(effectivePassText(create(EffectivePassSchema, { kind: PanelPassKind.MEMBERSHIP, startedAt: timestampFromMs(at) }), fmt)).toBe(
      `membership pass, @${at}`,
    )
    expect(effectivePassText(create(EffectivePassSchema, { kind: PanelPassKind.RELEASE }), fmt)).toBe('release pass')
    expect(effectivePassText(create(EffectivePassSchema, { kind: PanelPassKind.UNSPECIFIED }), fmt)).toBe('pass')
  })
})

describe('memberHealthBadge: the shared dataset health badges', () => {
  it.each([
    [DatasetHealthStatus.FRESH, 'DATASET_HEALTH_STATUS_FRESH'],
    [DatasetHealthStatus.LATE, 'DATASET_HEALTH_STATUS_LATE'],
    [DatasetHealthStatus.OVERDUE, 'DATASET_HEALTH_STATUS_OVERDUE'],
    [DatasetHealthStatus.COMPLETE, 'DATASET_HEALTH_STATUS_COMPLETE'],
    [DatasetHealthStatus.GAPS, 'DATASET_HEALTH_STATUS_GAPS'],
    [DatasetHealthStatus.RETIRED, 'DATASET_HEALTH_STATUS_RETIRED'],
  ])('%i is the %s badge', (status, name) => {
    expect(memberHealthBadge(status)).toEqual(HEALTH_BADGES[name as keyof typeof HEALTH_BADGES])
  })

  it('a member without health (no dataset yet, UNSPECIFIED) shows Unknown', () => {
    expect(memberHealthBadge(DatasetHealthStatus.UNSPECIFIED).label).toBe('Unknown')
    expect(memberHealthBadge(999 as DatasetHealthStatus).label).toBe('Unknown')
  })

  it('reads Healthy, Late, Failed and Stopped, never a proto name', () => {
    expect(memberHealthBadge(DatasetHealthStatus.FRESH).label).toBe('Healthy')
    expect(memberHealthBadge(DatasetHealthStatus.LATE).label).toBe('Late')
    expect(memberHealthBadge(DatasetHealthStatus.OVERDUE).label).toBe('Failed')
    expect(memberHealthBadge(DatasetHealthStatus.RETIRED).label).toBe('Stopped')
  })
})

describe('parseSymbols and parseCount', () => {
  it.each([
    ['AAPL, msft vfv', ['AAPL', 'msft', 'vfv']],
    ['  ,, AAPL,,\n\tMSFT  ', ['AAPL', 'MSFT']],
    ['', []],
    ['   ', []],
    ['BRK.B', ['BRK.B']],
  ])('parseSymbols(%j) is %j', (text, symbols) => {
    expect(parseSymbols(text)).toEqual(symbols)
  })

  it.each([
    ['0', 0],
    ['50', 50],
    [' 7 ', 7],
    ['007', 7],
    ['999999999', 999_999_999],
  ])('parseCount(%j) is %i', (text, count) => {
    expect(parseCount(text)).toBe(count)
  })

  it.each(['', '  ', '-1', '1.5', '1e3', '12a', '+3', '0x10', '1 000', '1000000000'])('parseCount(%j) is null', (text) => {
    expect(parseCount(text)).toBeNull()
  })
})

const SAVED = create(PanelSettingsSchema, {
  enabled: true,
  cadence: PanelCadence.WEEKLY,
  size: 40,
  allowSymbols: ['AAPL', 'MSFT'],
  denySymbols: ['TSLA'],
  assetClass: AssetType.STOCK,
  exchange: 'NASDAQ',
  active: true,
  tradable: false,
})

describe('the settings draft', () => {
  it('emptyDraft: enabled, no cadence named, every filter absent', () => {
    expect(emptyDraft()).toEqual({
      enabled: true,
      cadence: PanelCadence.UNSPECIFIED,
      size: '',
      allowSymbols: '',
      denySymbols: '',
      assetClass: AssetType.UNSPECIFIED,
      exchange: '',
      active: '',
      tradable: '',
    })
    expect(emptyDraft()).not.toBe(emptyDraft())
  })

  it('draftFromSettings words the saved settings as the form edits them', () => {
    expect(draftFromSettings(SAVED)).toEqual({
      enabled: true,
      cadence: PanelCadence.WEEKLY,
      size: '40',
      allowSymbols: 'AAPL, MSFT',
      denySymbols: 'TSLA',
      assetClass: AssetType.STOCK,
      exchange: 'NASDAQ',
      active: 'true',
      tradable: 'false',
    })
  })

  it('draftFromSettings: absent filters are absent in the draft', () => {
    const bare = create(PanelSettingsSchema, { enabled: false, cadence: PanelCadence.DAILY, size: 0 })
    expect(draftFromSettings(bare)).toMatchObject({ enabled: false, size: '0', assetClass: AssetType.UNSPECIFIED, exchange: '', active: '', tradable: '' })
  })

  it('draftToSettingsInit: the filters set are sent, the absent ones left out, the size parsed', () => {
    expect(draftToSettingsInit(draftFromSettings(SAVED))).toEqual({
      enabled: true,
      cadence: PanelCadence.WEEKLY,
      size: 40,
      allowSymbols: ['AAPL', 'MSFT'],
      denySymbols: ['TSLA'],
      assetClass: AssetType.STOCK,
      exchange: 'NASDAQ',
      active: true,
      tradable: false,
    })
  })

  it('draftToSettingsInit: an empty draft sends no filter, a blank exchange is absent, the cadence as drafted', () => {
    const init = draftToSettingsInit({ ...emptyDraft(), size: '5', exchange: '   ' })
    expect(init).toEqual({
      enabled: true,
      cadence: PanelCadence.UNSPECIFIED,
      size: 5,
      allowSymbols: [],
      denySymbols: [],
      assetClass: undefined,
      exchange: undefined,
      active: undefined,
      tradable: undefined,
    })
  })

  it('draftToSettingsInit trims the exchange and keeps a false enabled and a false filter', () => {
    const init = draftToSettingsInit({ ...emptyDraft(), enabled: false, size: '1', exchange: ' NYSE ', active: 'false', tradable: 'true' })
    expect(init).toMatchObject({ enabled: false, exchange: 'NYSE', active: false, tradable: true })
  })

  it('ordinary saved settings round-trip through the draft unchanged (the edit form starts from them)', () => {
    const back = create(PanelSettingsSchema, draftToSettingsInit(draftFromSettings(SAVED)))
    expect(back).toEqual(SAVED)
  })

  // The draft is the operator's text, so it is lossy for some saved values: Enable or Disable must not
  // go through it (tj-7zjov1.248). These pin the losses the toggle avoids, so the reason stays visible.
  it.each([
    ['a size of 10 digits parses to 0', { size: 2_147_483_647 }, { size: 0 }],
    ['an exchange is trimmed', { exchange: ' X ' }, { exchange: 'X' }],
    ['an empty-but-present exchange is dropped', { exchange: '' }, { exchange: undefined }],
    ['a present UNSPECIFIED asset class is dropped', { assetClass: AssetType.UNSPECIFIED }, { assetClass: undefined }],
  ])('the draft round trip is lossy: %s', (_label, saved, sent) => {
    const init = draftToSettingsInit(draftFromSettings(create(PanelSettingsSchema, { ...SAVED, ...saved })))
    expect(init).toMatchObject(sent)
  })

  const ok: PanelDraft = { ...emptyDraft(), cadence: PanelCadence.MONTHLY, size: '10' }

  it('draftProblem: nothing in the way', () => {
    expect(draftProblem(ok, 'because', { cadenceRequired: true })).toBe('')
  })

  it.each(['', ' ', '-1', 'ten', '1.5'])('draftProblem: a size of %j stops the save', (size) => {
    expect(draftProblem({ ...ok, size }, 'because', { cadenceRequired: true })).toBe('Give the size as a whole number, 0 or more.')
  })

  it('draftProblem: a save needs a cadence; a create does not (the store defaults it)', () => {
    const none = { ...ok, cadence: PanelCadence.UNSPECIFIED }
    expect(draftProblem(none, 'because', { cadenceRequired: true })).toBe('Choose a cadence.')
    expect(draftProblem(none, 'because', { cadenceRequired: false })).toBe('')
  })

  it.each(['', '   ', '\n\t'])('draftProblem: a blank reason (%j) stops it', (reason) => {
    expect(draftProblem(ok, reason, { cadenceRequired: true })).toBe('Give a reason for the change.')
    expect(draftProblem(ok, reason, { cadenceRequired: false })).toBe('Give a reason for the change.')
  })

  it('draftProblem: the edit form refuses a 10-digit size in words (it does not rewrite it)', () => {
    expect(draftProblem(draftFromSettings(create(PanelSettingsSchema, { ...SAVED, size: 2_147_483_647 })), 'r', { cadenceRequired: true })).toBe(
      'Give the size as a whole number, 0 or more.',
    )
    expect(draftProblem({ ...ok, size: '999999999' }, 'r', { cadenceRequired: true })).toBe('')
  })

  it('draftProblem: with every problem at once it names size, then cadence, then reason (unchanged order)', () => {
    const none = { ...ok, cadence: PanelCadence.UNSPECIFIED }
    expect(draftProblem({ ...none, size: '' }, '', { cadenceRequired: true })).toBe('Give the size as a whole number, 0 or more.')
    expect(draftProblem(none, '', { cadenceRequired: true })).toBe('Choose a cadence.')
    expect(draftProblem({ ...none, size: 'x' }, '', { cadenceRequired: false })).toBe('Give the size as a whole number, 0 or more.')
    expect(draftProblem(none, '', { cadenceRequired: false })).toBe('Give a reason for the change.')
  })
})

// tj-crl7pm: every problem at once, per field. The settings form still reads draftProblem (the first one);
// the create form reads createFieldErrors, which drives both its field marks and its button.
describe('draftFieldErrors: the settings draft problems per field', () => {
  const ok: PanelDraft = { ...emptyDraft(), cadence: PanelCadence.MONTHLY, size: '10' }
  const SIZE = 'Give the size as a whole number, 0 or more.'
  const CADENCE = 'Choose a cadence.'
  const REASON = 'Give a reason for the change.'

  it('nothing in the way: no field', () => {
    expect(draftFieldErrors(ok, 'because', { cadenceRequired: true })).toEqual({})
    expect(draftFieldErrors(ok, 'because', { cadenceRequired: false })).toEqual({})
  })

  it.each(['', ' ', '-1', 'ten', '1.5', '2147483648'])('a size of %j marks size only', (size) => {
    expect(draftFieldErrors({ ...ok, size }, 'because', { cadenceRequired: true })).toStrictEqual({ size: SIZE })
  })

  it('cadence is marked only when required', () => {
    const none = { ...ok, cadence: PanelCadence.UNSPECIFIED }
    expect(draftFieldErrors(none, 'because', { cadenceRequired: true })).toStrictEqual({ cadence: CADENCE })
    expect(draftFieldErrors(none, 'because', { cadenceRequired: false })).toStrictEqual({})
  })

  it.each(['', '   ', '\n\t'])('a blank reason (%j) marks reason only', (reason) => {
    expect(draftFieldErrors(ok, reason, { cadenceRequired: true })).toStrictEqual({ reason: REASON })
  })

  it('every problem at once: all three, in the order size, cadence, reason', () => {
    const errors = draftFieldErrors({ ...ok, size: '', cadence: PanelCadence.UNSPECIFIED }, ' ', { cadenceRequired: true })
    expect(Object.entries(errors)).toEqual([
      ['size', SIZE],
      ['cadence', CADENCE],
      ['reason', REASON],
    ])
  })
})

describe('createFieldErrors: every empty or invalid create field at once', () => {
  const COUNT = 'Give a whole number, 0 or more.'
  const READY: CreateInput = {
    name: 'Large caps',
    nameTaken: false,
    source: DataSource.ALPACA_API,
    sourceProblem: false,
    sourceNeedsSettings: false,
    draft: { ...emptyDraft(), size: '10' },
    reason: 'because',
    ramp: '',
    cap: '',
    pre: '',
    post: '',
  }
  const NEEDS: CreateInput = { ...READY, sourceNeedsSettings: true, ramp: '5', cap: '200' }

  it('a ready form: no field', () => {
    expect(createFieldErrors(READY)).toEqual({})
    expect(createFieldErrors(NEEDS)).toEqual({})
  })

  it('the form as opened: name, source, size and reason, in that order, with their words', () => {
    const opened: CreateInput = {
      ...READY,
      name: '',
      source: DataSource.UNSPECIFIED,
      sourceNeedsSettings: null,
      draft: emptyDraft(),
      reason: '',
    }
    expect(Object.entries(createFieldErrors(opened))).toEqual([
      ['name', 'Give the panel a name.'],
      ['source', 'Choose a source.'],
      ['size', 'Give the size as a whole number, 0 or more.'],
      ['reason', 'Give a reason for the change.'],
    ])
  })

  it('every field at once on a source with no settings: all eight, in form order', () => {
    const all: CreateInput = {
      ...NEEDS,
      name: ' ',
      sourceProblem: true,
      draft: { ...emptyDraft(), size: 'x' },
      reason: '',
      ramp: '',
      cap: '-1',
      pre: 'x',
      post: '1.5',
    }
    expect(Object.keys(createFieldErrors(all))).toEqual(['name', 'source', 'size', 'reason', 'ramp', 'cap', 'pre', 'post'])
  })

  it.each(['', '   '])('a blank name (%j) is asked for, taken or not', (name) => {
    expect(createFieldErrors({ ...READY, name })).toStrictEqual({ name: 'Give the panel a name.' })
    expect(createFieldErrors({ ...READY, name, nameTaken: true })).toStrictEqual({ name: 'Give the panel a name.' })
  })

  it('a taken name is refused on the name field, by its trimmed name', () => {
    expect(createFieldErrors({ ...READY, name: '  Large caps ', nameTaken: true })).toStrictEqual({
      name: PANEL_REFUSAL.nameTaken('Large caps'),
    })
  })

  it('the source: unchosen, then a failed read, then a read still out, in that precedence', () => {
    const unchosen = { ...READY, source: DataSource.UNSPECIFIED, sourceProblem: true, sourceNeedsSettings: null }
    expect(createFieldErrors(unchosen)).toStrictEqual({ source: 'Choose a source.' })
    expect(createFieldErrors({ ...READY, sourceProblem: true, sourceNeedsSettings: null })).toStrictEqual({
      source: 'The source settings could not be read. Choose the source again.',
    })
    expect(createFieldErrors({ ...READY, sourceProblem: true })).toStrictEqual({
      source: 'The source settings could not be read. Choose the source again.',
    })
    expect(createFieldErrors({ ...READY, sourceNeedsSettings: null })).toStrictEqual({ source: 'Reading the source settings.' })
  })

  it.each(['', ' ', '-1', 'ten', '1.5'])('a size of %j marks size only', (size) => {
    expect(createFieldErrors({ ...READY, draft: { ...READY.draft, size } })).toStrictEqual({
      size: 'Give the size as a whole number, 0 or more.',
    })
  })

  it.each(['', '  ', '\n'])('a blank reason (%j) marks reason only', (reason) => {
    expect(createFieldErrors({ ...READY, reason })).toStrictEqual({ reason: 'Give a reason for the change.' })
  })

  it('cadence is never marked: the store defaults it', () => {
    expect(createFieldErrors({ ...READY, draft: { ...READY.draft, cadence: PanelCadence.UNSPECIFIED } })).toEqual({})
  })

  it.each(['', ' ', '-1', 'x', '1.5'])('a source with no settings: a ramp or cap of %j is marked on that field', (bad) => {
    expect(createFieldErrors({ ...NEEDS, ramp: bad })).toStrictEqual({ ramp: COUNT })
    expect(createFieldErrors({ ...NEEDS, cap: bad })).toStrictEqual({ cap: COUNT })
  })

  it('a ramp or cap of 0 is a count', () => {
    expect(createFieldErrors({ ...NEEDS, ramp: '0', cap: '0' })).toEqual({})
  })

  it.each(['-1', 'x', '1.5'])('a source with no settings: a window day of %j is marked on that field', (bad) => {
    expect(createFieldErrors({ ...NEEDS, pre: bad })).toStrictEqual({ pre: COUNT })
    expect(createFieldErrors({ ...NEEDS, post: bad })).toStrictEqual({ post: COUNT })
  })

  it.each(['', '   ', '0', '30'])('a window day of %j is not marked: blank is the store default', (fine) => {
    expect(createFieldErrors({ ...NEEDS, pre: fine, post: fine })).toEqual({})
  })

  it.each([false, null])('ramp, cap and window days are never marked unless the source has no settings (%s)', (needs) => {
    const input = { ...READY, sourceNeedsSettings: needs, ramp: 'x', cap: '', pre: 'x', post: '-1' }
    expect(Object.keys(createFieldErrors(input)).filter((k) => ['ramp', 'cap', 'pre', 'post'].includes(k))).toEqual([])
  })
})

// Enable and Disable (tj-7zjov1.248, SavePanelSettingsRequest in panels.proto): the same write as Save
// Settings with only `enabled` changed. The write replaces the settings as sent, so the body is the saved
// PanelSettings as read: every field, an optional field present exactly when it was saved, no trim, no
// parse. Each row is checked twice: as the init object (key presence included, toStrictEqual), and on the
// wire, where the request's canonical JSON must equal the saved message's with `enabled` flipped.
describe('settingsInitWithEnabled: the saved settings with only enabled flipped', () => {
  const BASE = { enabled: true, cadence: PanelCadence.WEEKLY, size: 40, allowSymbols: ['AAPL'], denySymbols: [] as string[] }

  const ROWS: [string, Record<string, unknown>, Record<string, unknown>][] = [
    ['every optional field absent', {}, {}],
    [
      'every field set',
      { size: 40, allowSymbols: ['AAPL', 'MSFT'], denySymbols: ['TSLA'], assetClass: AssetType.STOCK, exchange: 'NASDAQ', active: true, tradable: false },
      { assetClass: AssetType.STOCK, exchange: 'NASDAQ', active: true, tradable: false },
    ],
    ['the int32 maximum size', { size: 2_147_483_647 }, {}],
    ['a size of 1,000,000,000 (10 digits)', { size: 1_000_000_000 }, {}],
    ['a size of 0', { size: 0 }, {}],
    ['an exchange with spaces round it', { exchange: ' X ' }, { exchange: ' X ' }],
    ['an empty-but-present exchange', { exchange: '' }, { exchange: '' }],
    ['a present UNSPECIFIED asset class', { assetClass: AssetType.UNSPECIFIED }, { assetClass: AssetType.UNSPECIFIED }],
    ['a set asset class', { assetClass: AssetType.CRYPTO }, { assetClass: AssetType.CRYPTO }],
    ['active false, tradable absent', { active: false }, { active: false }],
    ['active true, tradable false', { active: true, tradable: false }, { active: true, tradable: false }],
    ['tradable true, active absent', { tradable: true }, { tradable: true }],
    ['both false', { active: false, tradable: false }, { active: false, tradable: false }],
    ['symbols as saved, not re-parsed', { allowSymbols: [' aapl ', 'B,C', ''], denySymbols: ['x y'] }, {}],
    ['no cadence named', { cadence: PanelCadence.UNSPECIFIED }, {}],
    ['a disabled panel', { enabled: false }, {}],
  ]

  it.each(ROWS)('%s: the init is every saved field, optionals only if saved', (_label, fields, optionals) => {
    const saved = create(PanelSettingsSchema, { ...BASE, ...fields })
    const init = settingsInitWithEnabled(saved, !saved.enabled)
    expect(init).toStrictEqual({
      enabled: !saved.enabled,
      cadence: saved.cadence,
      size: saved.size,
      allowSymbols: saved.allowSymbols,
      denySymbols: saved.denySymbols,
      ...optionals,
    })
    for (const key of ['assetClass', 'exchange', 'active', 'tradable'] as const) {
      expect(Object.hasOwn(init, key), key).toBe(saved[key] !== undefined)
    }
  })

  it.each(ROWS)('%s: on the wire the body equals the saved settings with enabled flipped', (_label, fields) => {
    const saved = create(PanelSettingsSchema, { ...BASE, ...fields })
    const body = toJson(SavePanelSettingsRequestSchema, create(SavePanelSettingsRequestSchema, { ...settingsInitWithEnabled(saved, !saved.enabled), reason: 'r' }), {
      alwaysEmitImplicit: true,
    })
    const expected = { ...(toJson(PanelSettingsSchema, saved, { alwaysEmitImplicit: true }) as object), enabled: !saved.enabled, reason: 'r' }
    expect(body).toStrictEqual(expected)
    // And read back as settings, the message is the saved one with only enabled changed.
    const back = create(PanelSettingsSchema, settingsInitWithEnabled(saved, !saved.enabled))
    expect(back).toEqual(create(PanelSettingsSchema, { ...BASE, ...fields, enabled: !saved.enabled }))
  })

  it('sends the enabled it is given, not the saved one, either way', () => {
    const saved = create(PanelSettingsSchema, { ...BASE, enabled: true })
    expect(settingsInitWithEnabled(saved, false).enabled).toBe(false)
    expect(settingsInitWithEnabled(saved, true).enabled).toBe(true)
  })

  it('copies the symbol lists: the body does not alias the saved message', () => {
    const saved = create(PanelSettingsSchema, { ...BASE, allowSymbols: ['A'], denySymbols: ['D'] })
    const init = settingsInitWithEnabled(saved, false)
    init.allowSymbols!.push('Z')
    init.denySymbols!.push('Z')
    expect(saved.allowSymbols).toEqual(['A'])
    expect(saved.denySymbols).toEqual(['D'])
  })
})

describe('markerPosition', () => {
  it.each([
    [START, 0],
    [END, 100],
    [START + 25 * DAY, 25],
    [START + 50 * DAY, 50],
    [START - DAY, 0],
    [END + DAY, 100],
  ])('at %i is %d%%', (at, position) => {
    expect(markerPosition(at, START, END)).toBeCloseTo(position, 10)
  })

  it('an empty or inverted window places everything at 0', () => {
    expect(markerPosition(START, START, START)).toBe(0)
    expect(markerPosition(START, END, START)).toBe(0)
  })
})

describe('stripMarkers: the boundary read joined with the settings versions by version number', () => {
  const pass = (ms: number) => create(EffectivePassSchema, { passId: `p${ms}`, kind: PanelPassKind.MEMBERSHIP, startedAt: timestampFromMs(ms) })
  const panelVersions = [
    create(PanelSettingsVersionSchema, { versionNo: 3 }),
    create(PanelSettingsVersionSchema, { versionNo: 2, effectivePass: pass(START + 20 * DAY) }),
    create(PanelSettingsVersionSchema, { versionNo: 1, effectivePass: pass(START + 5 * DAY) }),
  ]
  const sourceVersions = [
    create(PanelSourceSettingsVersionSchema, { versionNo: 2, effectivePass: pass(START + 61 * DAY) }),
    create(PanelSourceSettingsVersionSchema, { versionNo: 3 }),
  ]
  const marker = (kind: PanelMarkerKind, versionNo: number, at?: number) =>
    create(PanelMarkerSchema, { kind, versionNo, actor: 'operator', reason: 'r', at: at === undefined ? undefined : timestampFromMs(at) })
  const window = { start: START, end: END }

  it('a panel-settings marker takes its own version pass, not the source version of the same number', () => {
    const [entry] = stripMarkers(create(PanelBoundariesSchema, { markers: [marker(PanelMarkerKind.PANEL_SETTINGS, 2, START + 10 * DAY)] }), panelVersions, sourceVersions, window)
    expect(entry).toMatchObject({ position: 10, pending: false })
    expect(entry!.effectivePass?.passId).toBe(`p${START + 20 * DAY}`)
  })

  it('a source-settings marker takes the source version pass, not the panel version of the same number', () => {
    const [entry] = stripMarkers(create(PanelBoundariesSchema, { markers: [marker(PanelMarkerKind.SOURCE_SETTINGS, 2, START + 60 * DAY)] }), panelVersions, sourceVersions, window)
    expect(entry).toMatchObject({ position: 60, pending: false })
    expect(entry!.effectivePass?.passId).toBe(`p${START + 61 * DAY}`)
  })

  it('a version without an effective pass is pending, with no pass', () => {
    const entries = stripMarkers(
      create(PanelBoundariesSchema, {
        markers: [marker(PanelMarkerKind.PANEL_SETTINGS, 3, START + 90 * DAY), marker(PanelMarkerKind.SOURCE_SETTINGS, 3, START + 95 * DAY)],
      }),
      panelVersions,
      sourceVersions,
      window,
    )
    expect(entries.map((e) => [e.pending, e.effectivePass])).toEqual([
      [true, undefined],
      [true, undefined],
    ])
  })

  it('a version the pages do not hold is pending too (never another version pass)', () => {
    const [entry] = stripMarkers(create(PanelBoundariesSchema, { markers: [marker(PanelMarkerKind.PANEL_SETTINGS, 9, START)] }), panelVersions, sourceVersions, window)
    expect(entry).toMatchObject({ pending: true, effectivePass: undefined })
  })

  it('a collect marker is never pending and carries no pass', () => {
    const entries = stripMarkers(
      create(PanelBoundariesSchema, { markers: [marker(PanelMarkerKind.COLLECT_STOP, 1, START + 30 * DAY), marker(PanelMarkerKind.COLLECT_RESUME, 2, START + 40 * DAY)] }),
      panelVersions,
      sourceVersions,
      window,
    )
    expect(entries.map((e) => [e.position, e.pending, e.effectivePass])).toEqual([
      [30, false, undefined],
      [40, false, undefined],
    ])
  })

  it('keeps the read order and the marker itself; a marker without an instant sits at the window start', () => {
    const markers = [marker(PanelMarkerKind.PANEL_SETTINGS, 1, START + 70 * DAY), marker(PanelMarkerKind.PANEL_SETTINGS, 2)]
    const entries = stripMarkers(create(PanelBoundariesSchema, { markers }), panelVersions, sourceVersions, window)
    expect(entries.map((e) => e.marker)).toEqual(markers)
    expect(entries.map((e) => e.position)).toEqual([70, 0])
  })

  it('no markers, no entries', () => {
    expect(stripMarkers(create(PanelBoundariesSchema), panelVersions, sourceVersions, window)).toEqual([])
  })
})

describe('panelWriteNotice: the words a refused write is shown in', () => {
  const error = { message: 'Server said no.', errorId: 'e-1' }

  // tj-7zjov1.246: a 422 nothing re-read could explain reads fixed copy for its route (copy.ts), never the
  // server's message, and keeps the error id.
  it.each([
    ['create', 'The store refused the panel, so nothing was created. Check the fields.'],
    ['settings', 'The store refused these settings, so nothing was saved. Check the fields.'],
    ['source', 'The store refused these source settings, so nothing was saved. Check the fields.'],
  ] as const)('invalid (422) on %s is its fixed copy: never the server message, the error id kept', (route, words) => {
    const notice = panelWriteNotice({ kind: 'invalid', error: { message: 'name "x" is taken', errorId: 'e-422' } }, route)
    expect(notice).toEqual({ text: words, errorId: 'e-422' })
    expect(notice?.text).not.toContain('taken')
  })

  it('each route reads its own fixed copy, and the route is required', () => {
    const invalid = { kind: 'invalid', error: { message: 'm', errorId: 'e' } } as const
    const texts = (['create', 'settings', 'source'] as const).map((route) => panelWriteNotice(invalid, route)?.text)
    expect(texts).toEqual([PANEL_REFUSAL.create, PANEL_REFUSAL.settings, PANEL_REFUSAL.source])
    expect(new Set(texts).size).toBe(3)
    // tj-7zjov1.254 F1: no default route. Omitting it must not compile (typecheck reds an unused directive),
    // and at run time a routeless call reads no route's copy, so a forgotten route never passes as settings.
    // @ts-expect-error the route is required
    const routeless = panelWriteNotice(invalid)
    expect(routeless?.text).not.toBe(PANEL_REFUSAL.settings)
  })

  // tj-7zjov1.254 F2 F3: the taken name travels on the outcome, and every moved-on notice keeps the 422's id.
  const refused = { message: 'm', errorId: 'e-moved' }
  it.each([
    [
      'name-taken',
      { kind: 'moved-on', what: 'name-taken', name: 'Large caps', error: refused },
      'create',
      'A panel named "Large caps" was created in the meantime, so nothing was created. Choose another name.',
    ],
    [
      'source-has-settings',
      { kind: 'moved-on', what: 'source-has-settings', error: refused },
      'create',
      'This source has settings now, so nothing was created. Its settings fields are gone: check the form and create again.',
    ],
    [
      'settings',
      { kind: 'moved-on', what: 'settings', error: refused },
      'settings',
      "This panel's settings changed in the meantime, so nothing was saved. The page shows the current settings.",
    ],
    [
      'source-settings',
      { kind: 'moved-on', what: 'source-settings', error: refused },
      'source',
      'These source settings changed in the meantime, so nothing was saved. The page shows the current settings.',
    ],
  ] as const)('moved-on %s names what changed and keeps the error id', (_what, outcome, route, words) => {
    expect(panelWriteNotice(outcome, route)).toEqual({ text: words, errorId: 'e-moved' })
  })

  it('a moved-on notice shows the error id of its own 422', () => {
    for (const errorId of ['e-a', 'e-b']) {
      const error = { message: 'm', errorId }
      for (const [outcome, route] of [
        [{ kind: 'moved-on', what: 'name-taken', name: 'n', error }, 'create'],
        [{ kind: 'moved-on', what: 'source-has-settings', error }, 'create'],
        [{ kind: 'moved-on', what: 'settings', error }, 'settings'],
        [{ kind: 'moved-on', what: 'source-settings', error }, 'source'],
      ] as const) {
        expect(panelWriteNotice(outcome, route)?.errorId, outcome.what).toBe(errorId)
      }
    }
  })

  it('name-taken names the name on its outcome, whichever it is, on every route; never an empty name', () => {
    for (const name of ['Large caps', 'Small caps']) {
      for (const route of ['create', 'settings', 'source'] as const) {
        const text = panelWriteNotice({ kind: 'moved-on', what: 'name-taken', name, error: refused }, route)?.text
        expect(text).toBe(PANEL_REFUSAL.createNameTakenSince(name))
        expect(text).toContain(`"${name}"`)
        expect(text).not.toContain('named ""')
      }
    }
  })

  // "fresh" covers the dataset health's retired name (tj-grna9p.168), spelt short so its guard spec passes.
  it('the refusal copy holds no world, universe or fresh word', () => {
    const copy = Object.values(PANEL_REFUSAL).map((value) => (typeof value === 'function' ? value('n') : value))
    expect(copy).toHaveLength(8)
    expect(copy.join('\n').match(/world|universe|fresh/gi) ?? []).toEqual([])
  })

  it('unavailable (503) names make migrate and keeps the error id', () => {
    const notice = panelWriteNotice({ kind: 'unavailable', error }, 'settings')
    expect(notice?.text).toContain('make migrate')
    expect(notice?.errorId).toBe('e-1')
  })

  it('not-found says the panel is gone', () => {
    expect(panelWriteNotice({ kind: 'not-found' }, 'settings')?.text).toBe('This panel no longer exists, so nothing was changed. Reload the page.')
  })

  it('override-required is worded as unexpected', () => {
    expect(panelWriteNotice({ kind: 'override-required' }, 'settings')?.text).toContain('a panel write never needs')
  })

  it('error carries the described message (not the response body) and the id', () => {
    expect(panelWriteNotice({ kind: 'error', error }, 'settings')).toEqual({
      text: 'Could not complete the change. Server said no. Nothing was changed.',
      errorId: 'e-1',
    })
  })

  it('ok, created and stale show nothing', () => {
    for (const route of ['create', 'settings', 'source'] as const) {
      expect(panelWriteNotice({ kind: 'ok' }, route)).toBeNull()
      expect(panelWriteNotice({ kind: 'stale' }, route)).toBeNull()
      expect(panelWriteNotice({ kind: 'created', detail: {} as never }, route)).toBeNull()
    }
  })

  it('every refusal says nothing was changed', () => {
    for (const outcome of [
      { kind: 'unavailable', error },
      { kind: 'not-found' },
      { kind: 'override-required' },
      { kind: 'error', error },
    ] as const) {
      for (const route of ['create', 'settings', 'source'] as const) {
        expect(panelWriteNotice(outcome, route)?.text.toLowerCase(), `${outcome.kind} ${route}`).toContain('nothing was changed')
      }
    }
  })

  it('a refused 422, fixed or moved-on, says nothing was created (create) or nothing was saved (the saves)', () => {
    const invalid = { kind: 'invalid', error } as const
    expect(panelWriteNotice(invalid, 'create')?.text).toContain('nothing was created')
    expect(panelWriteNotice(invalid, 'settings')?.text).toContain('nothing was saved')
    expect(panelWriteNotice(invalid, 'source')?.text).toContain('nothing was saved')
    expect(panelWriteNotice({ kind: 'moved-on', what: 'name-taken', name: 'n', error }, 'create')?.text).toContain('nothing was created')
    expect(panelWriteNotice({ kind: 'moved-on', what: 'source-has-settings', error }, 'create')?.text).toContain('nothing was created')
    expect(panelWriteNotice({ kind: 'moved-on', what: 'settings', error }, 'settings')?.text).toContain('nothing was saved')
    expect(panelWriteNotice({ kind: 'moved-on', what: 'source-settings', error }, 'source')?.text).toContain('nothing was saved')
  })
})
