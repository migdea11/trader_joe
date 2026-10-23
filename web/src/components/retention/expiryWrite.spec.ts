import { describe, expect, it } from 'vitest'

import type { WriteOutcome } from '@/stores/datasetDetail'

import { refusalNotice, tomorrowText } from './expiryWrite'

// tj-grna9p.62: what the Retention card and the Usage tab's Set Expiry dialog share. The card's own
// behaviour with these is pinned in views/data/DatasetDetail.spec.ts (tj-grna9p.89).
describe('tomorrowText', () => {
  it('is the next calendar day in the display zone, as YYYY/MM/DD', () => {
    // 22:00 on 2026-10-14 in Toronto, already 2026-10-15 in UTC.
    const evening = Date.UTC(2026, 9, 15, 2)
    expect(tomorrowText('America/Toronto', evening)).toBe('2026/10/15')
    expect(tomorrowText('UTC', evening)).toBe('2026/10/16')
  })

  it('rolls over months and years by the calendar, not by 24 hours', () => {
    expect(tomorrowText('UTC', Date.UTC(2026, 11, 31, 12))).toBe('2027/01/01')
    expect(tomorrowText('UTC', Date.UTC(2028, 1, 28, 12))).toBe('2028/02/29')
    // The DST change in Toronto (2026-11-01): still the next day.
    expect(tomorrowText('America/Toronto', Date.UTC(2026, 10, 1, 12))).toBe('2026/11/02')
  })
})

describe('refusalNotice', () => {
  const error = { message: 'Request failed (HTTP 500)', errorId: 'e-1' }

  it.each([
    [{ kind: 'override-required' }, { text: 'This dataset is held by another owner. Give a reason and confirm to continue.' }],
    [
      { kind: 'unavailable', error },
      { text: 'The database is not ready for this change, so nothing was changed. Run make migrate, then try again.', errorId: 'e-1' },
    ],
    [
      { kind: 'invalid', error },
      { text: 'The server refused the request as invalid. This is a bug in the UI; nothing was changed.', errorId: 'e-1' },
    ],
    [{ kind: 'error', error }, { text: 'Could not complete the change. Request failed (HTTP 500) Nothing was changed.', errorId: 'e-1' }],
  ] as [WriteOutcome, unknown][])('%o is shown as fixed words with the error id', (outcome, notice) => {
    expect(refusalNotice(outcome)).toEqual(notice)
  })

  it.each([[{ kind: 'ok' }], [{ kind: 'stale' }], [{ kind: 'not-found' }]] as [WriteOutcome][])(
    '%o is not a refusal shown in place',
    (outcome) => {
      expect(refusalNotice(outcome)).toBeNull()
    },
  )
})
