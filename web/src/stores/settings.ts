// Browser-stored UI settings (decision tj-grna9p.58, option A: settings stay in this browser, no
// server settings call). One localStorage key holds a versioned object. Every storage read and
// write is wrapped: unavailable or throwing storage, unreadable JSON or an out-of-range value
// falls back to the default (the browser zone), which then lives in memory for the session.
import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

export const SETTINGS_STORAGE_KEY = 'trader_joe.ui.settings.v1'
const SETTINGS_VERSION = 1

interface StoredSettings {
  v: number
  timeZone: string | null
}

/** True when `zone` is an IANA zone name this browser's Intl knows. */
export function isValidTimeZone(zone: unknown): zone is string {
  if (typeof zone !== 'string' || zone === '') return false
  try {
    new Intl.DateTimeFormat('en-CA', { timeZone: zone })
    return true
  } catch {
    return false
  }
}

/** The zone the browser reports; UTC when it reports none. */
export function browserTimeZone(): string {
  try {
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone
    return isValidTimeZone(zone) ? zone : 'UTC'
  } catch {
    return 'UTC'
  }
}

// Reading `window.localStorage` itself can throw (blocked site data), so even the lookup is wrapped.
function storage(): Storage | null {
  try {
    return window.localStorage
  } catch {
    return null
  }
}

interface Loaded {
  timeZone: string | null
  /** False when storage could not be read at all (as opposed to simply holding nothing yet). */
  storageOk: boolean
}

function load(): Loaded {
  const defaults: Loaded = { timeZone: null, storageOk: true }
  const store = storage()
  if (store === null) return { ...defaults, storageOk: false }
  let raw: string | null
  try {
    raw = store.getItem(SETTINGS_STORAGE_KEY)
  } catch {
    return { ...defaults, storageOk: false }
  }
  if (raw === null) return defaults
  try {
    const parsed: unknown = JSON.parse(raw)
    if (typeof parsed !== 'object' || parsed === null) return defaults
    const stored = parsed as Partial<StoredSettings>
    if (stored.v !== SETTINGS_VERSION) return defaults
    return { timeZone: isValidTimeZone(stored.timeZone) ? stored.timeZone : null, storageOk: true }
  } catch {
    return defaults
  }
}

export const useSettingsStore = defineStore('settings', () => {
  const loaded = load()
  /** The zone chosen in Settings; null means "use the browser zone". */
  const timeZoneOverride = ref<string | null>(loaded.timeZone)
  /** False once any read or write of the browser storage has failed. */
  const storageOk = ref<boolean>(loaded.storageOk)

  /** The zone every formatter uses. */
  const timeZone = computed(() => timeZoneOverride.value ?? browserTimeZone())

  function persist(): void {
    const store = storage()
    if (store === null) {
      storageOk.value = false
      return
    }
    const body: StoredSettings = { v: SETTINGS_VERSION, timeZone: timeZoneOverride.value }
    try {
      store.setItem(SETTINGS_STORAGE_KEY, JSON.stringify(body))
      storageOk.value = true
    } catch {
      storageOk.value = false
    }
  }

  /** Set the override. An unknown zone is rejected (false) and nothing changes. */
  function setTimeZoneOverride(zone: string): boolean {
    if (!isValidTimeZone(zone)) return false
    timeZoneOverride.value = zone
    persist()
    return true
  }

  /** "Use Browser Zone": clear the override. */
  function useBrowserZone(): void {
    timeZoneOverride.value = null
    persist()
  }

  return {
    timeZoneOverride,
    storageOk,
    timeZone,
    setTimeZoneOverride,
    useBrowserZone,
  }
})
