// The Panels page's state (tj-7zjov1.37, ADR tj-26jt9w addendum 20): the panel selector, the selected
// panel's detail, its settings versions (and its source's), the history window (asset-list days and the
// boundary markers), the member page, and the three panel writes (create, save settings, save source
// settings). Panels are UI-only: the server decides every count, state and health, and the page maps
// them to words.
//
// Nothing polls. A load happens on mount, on a selection change, after a write and once per reconnect of
// the events socket (there is no panel event type, so a write is followed by a REST re-read, never by
// building state from the answer alone). A late answer for a panel that is no longer selected is dropped.
import { computed, ref, shallowRef } from 'vue'
import { defineStore } from 'pinia'
import type { DataSource } from '@generated/trader_joe/proto/market/v1/enums_pb'
import type {
  AssetListHistory,
  PanelBoundaries,
  PanelDetail,
  PanelListItem,
  PanelMember,
  PanelSettingsVersion,
  PanelSourceSettingsVersion,
} from '@generated/trader_joe/proto/ui/v1/panels_pb'

import {
  createPanel,
  getAssetListHistory,
  getPanel,
  getPanelBoundaries,
  getPanelMembers,
  getPanelSettingsVersions,
  getSourceSettingsVersions,
  listPanels,
  savePanelSettings,
  saveSourceSettings,
} from '@/api'
import type { CreatePanelParams, SavePanelSettingsParams, SaveSourceSettingsParams } from '@/api'
import { describeError } from '@/stores/datasetCatalog'
import type { CatalogError } from '@/stores/datasetCatalog'
import { writeOutcome } from '@/stores/datasetDetail'
import type { WriteOutcome } from '@/stores/datasetDetail'

/** The history window, in days back from now: the server allows at most 366 days in one read. */
export const HISTORY_DAYS = 90
/** How many members a page asks for. */
export const MEMBERS_PAGE_SIZE = 100
/** The most settings versions read to give a marker its effective pass. */
const VERSIONS_LIMIT = 500

const DAY_MS = 86_400_000

export type PanelLoadStatus = 'idle' | 'loading' | 'ready' | 'not-found' | 'error'

/** What a panel write came to; the refusal kinds are the Retention card's (datasetDetail.ts). */
export type PanelWriteOutcome =
  | WriteOutcome
  | { kind: 'created'; detail: PanelDetail }
  /** A 422 after which a re-read showed the condition it rests on changed: say which. */
  | ({ kind: 'moved-on'; error: CatalogError } & PanelMovedOn)

/**
 * What a re-read after a refused write found different from what the form was built on; the taken name
 * travels with its outcome. The 422's error is kept on the outcome so its id stays on the notice.
 */
export type PanelMovedOn =
  | { what: 'name-taken'; name: string }
  | { what: 'source-has-settings' | 'settings' | 'source-settings' }

/** The sections of the selected panel that load on their own, so each fails on its own. */
export type PanelSection = 'detail' | 'versions' | 'history' | 'markers' | 'members'

/** The window read for the strip: [now - HISTORY_DAYS, now + 1 day), so today's block is in it. */
export function historyWindow(now: number = Date.now()): { start: Date; end: Date } {
  return { start: new Date(now - HISTORY_DAYS * DAY_MS), end: new Date(now + DAY_MS) }
}

export const usePanelsStore = defineStore('panels', () => {
  const panels = shallowRef<readonly PanelListItem[]>([])
  const listStatus = ref<PanelLoadStatus>('idle')
  const listError = ref<CatalogError | null>(null)

  const selectedId = ref<string | null>(null)
  const detail = shallowRef<PanelDetail | null>(null)
  const detailStatus = ref<PanelLoadStatus>('idle')
  const panelVersions = shallowRef<readonly PanelSettingsVersion[]>([])
  const sourceVersions = shallowRef<readonly PanelSourceSettingsVersion[]>([])
  const history = shallowRef<AssetListHistory | null>(null)
  const boundaries = shallowRef<PanelBoundaries | null>(null)
  const members = shallowRef<readonly PanelMember[]>([])
  const membersCursor = ref('')
  const loadingMoreMembers = ref(false)
  const errors = ref<Partial<Record<PanelSection, CatalogError>>>({})
  const writing = ref(false)
  /** Bumped when a load settles, so the footer's "Updated" ticks. */
  const loadedTick = ref(0)

  let selectionInflight: AbortController | null = null
  // Bumped by every selection and reload; a members page that returns under an older value is dropped.
  let membersToken = 0
  let membersInflight: AbortController | null = null
  let listInflight: AbortController | null = null

  const selected = computed(() => panels.value.find((item) => item.id === selectedId.value) ?? null)
  const hasMoreMembers = computed(() => membersCursor.value !== '')

  function fail(section: PanelSection, error: unknown): void {
    errors.value = { ...errors.value, [section]: describeError(error) }
  }

  /** Drop a Load More still out: abort it and free the flag, so a Load More on the new walk can start. */
  function dropMoreMembers(): void {
    membersToken += 1
    membersInflight?.abort()
    membersInflight = null
    loadingMoreMembers.value = false
  }

  function clearSelection(): void {
    dropMoreMembers()
    detail.value = null
    panelVersions.value = []
    sourceVersions.value = []
    history.value = null
    boundaries.value = null
    members.value = []
    membersCursor.value = ''
    errors.value = {}
  }

  /** Read the selector's panels. Keeps the selection when the panel is still there, else takes the first. */
  async function loadList(): Promise<void> {
    listInflight?.abort()
    const controller = new AbortController()
    listInflight = controller
    if (panels.value.length === 0) listStatus.value = 'loading'
    try {
      const result = await listPanels(controller.signal)
      if (controller.signal.aborted) return
      panels.value = result.items
      listStatus.value = 'ready'
      listError.value = null
      if (!result.items.some((item) => item.id === selectedId.value)) {
        const first = result.items[0]
        selectedId.value = first === undefined ? null : first.id
        clearSelection()
        detailStatus.value = 'idle'
      }
    } catch (error) {
      if (controller.signal.aborted) return
      listStatus.value = 'error'
      listError.value = describeError(error)
    }
    loadedTick.value += 1
  }

  /** Read everything for the selected panel; the sections fail separately and the page stays up. */
  async function loadSelected(): Promise<void> {
    const id = selectedId.value
    dropMoreMembers()
    selectionInflight?.abort()
    if (id === null) {
      clearSelection()
      detailStatus.value = 'idle'
      return
    }
    const controller = new AbortController()
    selectionInflight = controller
    const signal = controller.signal
    const keep = detail.value?.id === id
    if (!keep) {
      clearSelection()
      detailStatus.value = 'loading'
    } else {
      errors.value = {}
    }

    const { start, end } = historyWindow()
    const read = await Promise.allSettled([
      getPanel(id, signal),
      getPanelSettingsVersions(id, { limit: VERSIONS_LIMIT, signal }),
      getAssetListHistory(id, { start, end, signal }),
      getPanelBoundaries(id, { start, end, signal }),
      getPanelMembers(id, { limit: MEMBERS_PAGE_SIZE, signal }),
    ])
    if (signal.aborted) return
    const [detailRead, versionsRead, historyRead, markersRead, membersRead] = read

    if (detailRead.status === 'fulfilled') {
      detail.value = detailRead.value
      detailStatus.value = 'ready'
      // The source's versions need the panel's source, which only the detail names.
      try {
        const page = await getSourceSettingsVersions(detailRead.value.source, { limit: VERSIONS_LIMIT, signal })
        if (signal.aborted) return
        sourceVersions.value = page.items
      } catch (error) {
        if (signal.aborted) return
        fail('versions', error)
      }
    } else {
      const error = detailRead.reason as unknown
      detailStatus.value = isNotFound(error) ? 'not-found' : 'error'
      fail('detail', error)
    }
    if (versionsRead.status === 'fulfilled') panelVersions.value = versionsRead.value.items
    else fail('versions', versionsRead.reason)
    if (historyRead.status === 'fulfilled') history.value = historyRead.value
    else fail('history', historyRead.reason)
    if (markersRead.status === 'fulfilled') boundaries.value = markersRead.value
    else fail('markers', markersRead.reason)
    if (membersRead.status === 'fulfilled') {
      members.value = membersRead.value.items
      membersCursor.value = membersRead.value.nextCursor
    } else {
      fail('members', membersRead.reason)
    }
    loadedTick.value += 1
  }

  function isNotFound(error: unknown): boolean {
    return writeOutcome(error).kind === 'not-found'
  }

  /** Choose a panel (a no-op for the one already chosen) and read it. */
  async function select(id: string): Promise<void> {
    if (id === selectedId.value) return
    selectedId.value = id
    await loadSelected()
  }

  /** The first read of the page: the selector, then the selected panel. */
  async function load(): Promise<void> {
    await loadList()
    await loadSelected()
  }

  /** Append the next page of members. */
  async function loadMoreMembers(): Promise<void> {
    const id = selectedId.value
    if (id === null || membersCursor.value === '' || loadingMoreMembers.value) return
    loadingMoreMembers.value = true
    const token = membersToken
    const controller = new AbortController()
    membersInflight = controller
    try {
      const page = await getPanelMembers(id, {
        cursor: membersCursor.value,
        limit: MEMBERS_PAGE_SIZE,
        signal: controller.signal,
      })
      // A selection or a reload since the call started makes this page stale, even for the same panel.
      if (selectedId.value !== id || token !== membersToken) return
      members.value = [...members.value, ...page.items]
      membersCursor.value = page.nextCursor
    } catch (error) {
      if (controller.signal.aborted) return
      if (selectedId.value === id && token === membersToken) fail('members', error)
    } finally {
      // Only the current request frees the flag; a stale one's flag was already cleared and may be a new call's.
      if (token === membersToken) {
        loadingMoreMembers.value = false
        membersInflight = null
      }
    }
  }

  async function write(run: () => Promise<PanelWriteOutcome>): Promise<PanelWriteOutcome> {
    if (writing.value) return { kind: 'stale' }
    writing.value = true
    try {
      return await run()
    } catch (error) {
      return writeOutcome(error)
    } finally {
      writing.value = false
    }
  }

  /**
   * Create a panel (W1). On success the new panel is selected and re-read: the write does not run the
   * first pass, so the detail comes back with effective_pass unset ("first pass pending") until the
   * store's panel manager has run one.
   */
  function create(params: CreatePanelParams): Promise<PanelWriteOutcome> {
    return write(async () => {
      let created: PanelDetail
      try {
        created = await createPanel(params)
      } catch (error) {
        const outcome = writeOutcome(error)
        if (outcome.kind !== 'invalid') throw error
        return (await createMovedOn(params, outcome.error)) ?? outcome
      }
      await loadList()
      selectedId.value = created.id
      detail.value = created
      detailStatus.value = 'ready'
      await loadSelected()
      return { kind: 'created', detail: created }
    })
  }

  /**
   * After a refused create: re-read the list (R1) and, when the request carried source settings, the
   * source's first version (R4), and name the condition that changed. Null when neither did (or a read failed).
   */
  async function createMovedOn(params: CreatePanelParams, error: CatalogError): Promise<PanelWriteOutcome | null> {
    await loadList()
    const { name } = params
    if (name !== undefined && panels.value.some((item) => item.name === name)) {
      return { kind: 'moved-on', what: 'name-taken', name, error }
    }
    if (params.rampDatasetsPerHour === undefined || params.source === undefined) return null
    try {
      const page = await getSourceSettingsVersions(params.source, { limit: 1 })
      if (page.items.length > 0) return { kind: 'moved-on', what: 'source-has-settings', error }
    } catch {
      // The generic copy stands.
    }
    return null
  }

  /**
   * Run a settings write; on a 422 re-read the panel and say so when its saved version is not the one the
   * form saw. A selection that changed during the re-read answers stale, as the success path does.
   */
  async function refusedAfterRead(
    error: unknown,
    seen: () => number,
    what: 'settings' | 'source-settings',
    stillHere: () => boolean,
  ): Promise<PanelWriteOutcome> {
    const outcome = writeOutcome(error)
    if (outcome.kind !== 'invalid') return outcome
    const before = seen()
    await loadList()
    await loadSelected()
    if (!stillHere()) return { kind: 'stale' }
    return seen() !== before ? { kind: 'moved-on', what, error: outcome.error } : outcome
  }

  /** Save a new settings version of the selected panel (W2); enable and disable is this write. */
  function saveSettings(params: SavePanelSettingsParams): Promise<PanelWriteOutcome> {
    const id = selectedId.value
    if (id === null) return Promise.resolve({ kind: 'stale' })
    return write(async () => {
      const seen = (): number => (detail.value?.id === id ? (detail.value.currentVersion?.versionNo ?? 0) : -1)
      try {
        await savePanelSettings(id, params)
      } catch (error) {
        return refusedAfterRead(error, seen, 'settings', () => selectedId.value === id && detail.value?.id === id)
      }
      if (selectedId.value !== id) return { kind: 'stale' }
      await loadList()
      await loadSelected()
      return { kind: 'ok' }
    })
  }

  /** Save a new settings version of a source (W3), then re-read the selected panel. */
  function saveSource(source: DataSource, params: SaveSourceSettingsParams): Promise<PanelWriteOutcome> {
    const id = selectedId.value
    return write(async () => {
      const seen = (): number => detail.value?.currentSourceVersion?.versionNo ?? 0
      try {
        await saveSourceSettings(source, params)
      } catch (error) {
        return refusedAfterRead(error, seen, 'source-settings', () => selectedId.value === id)
      }
      await loadSelected()
      return { kind: 'ok' }
    })
  }

  return {
    panels,
    listStatus,
    listError,
    selectedId,
    selected,
    detail,
    detailStatus,
    panelVersions,
    sourceVersions,
    history,
    boundaries,
    members,
    hasMoreMembers,
    loadingMoreMembers,
    errors,
    writing,
    loadedTick,
    load,
    loadList,
    loadSelected,
    select,
    loadMoreMembers,
    create,
    saveSettings,
    saveSource,
  }
})
