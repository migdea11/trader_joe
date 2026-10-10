// Browser-stored UI settings (decision tj-grna9p.58, option A: settings stay in this browser, no
// server settings call). One localStorage key holds a versioned object. Every storage read and
// write is wrapped: unavailable or throwing storage, unreadable JSON or an out-of-range value
// falls back to the defaults (browser zone, 90 days), which then live in memory for the session.
import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

export const SETTINGS_STORAGE_KEY = 'trader_joe.ui.settings.v1'
const SETTINGS_VERSION = 1

export const STALE_DAYS_DEFAULT = 90
export const STALE_DAYS_MIN = 1
export const STALE_DAYS_MAX = 3650

interface StoredSettings {
  v: number
  timeZone: string | null
  staleDays: number
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

export function isValidStaleDays(value: unknown): value is number {
  return (
    typeof value === 'number' &&
    Number.isInteger(value) &&
    value >= STALE_DAYS_MIN &&
    value <= STALE_DAYS_MAX
  )
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
  staleDays: number
  /** False when storage could not be read at all (as opposed to simply holding nothing yet). */
  storageOk: boolean
}

// Each field is validated on its own, so one bad value does not discard the other.
function load(): Loaded {
  const defaults: Loaded = { timeZone: null, staleDays: STALE_DAYS_DEFAULT, storageOk: true }
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
    return {
      timeZone: isValidTimeZone(stored.timeZone) ? stored.timeZone : null,
      staleDays: isValidStaleDays(stored.staleDays) ? stored.staleDays : STALE_DAYS_DEFAULT,
      storageOk: true,
    }
  } catch {
    return defaults
  }
}

export const useSettingsStore = defineStore('settings', () => {
  const loaded = load()
  /** The zone chosen in Settings; null means "use the browser zone". */
  const timeZoneOverride = ref<string | null>(loaded.timeZone)
  const staleDays = ref<number>(loaded.staleDays)
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
    const body: StoredSettings = {
      v: SETTINGS_VERSION,
      timeZone: timeZoneOverride.value,
      staleDays: staleDays.value,
    }
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

  /** Set the stale threshold. Anything but an integer in 1..3650 is rejected (false). */
  function setStaleDays(days: number): boolean {
    if (!isValidStaleDays(days)) return false
    staleDays.value = days
    persist()
    return true
  }

  return {
    timeZoneOverride,
    staleDays,
    storageOk,
    timeZone,
    setTimeZoneOverride,
    useBrowserZone,
    setStaleDays,
  }
})
