import { describe, expect, it } from 'vitest'
import { create } from '@bufbuild/protobuf'

import { ClientKind } from '@generated/trader_joe/proto/ui/v1/data_pb'
import { ReadsPerDaySchema } from '@generated/trader_joe/proto/ui/v1/stats_pb'

import { readsPerDayModel } from './readsPerDay'

// tj-grna9p.89: the Reads per Day chart's data. The server counts the reads (one entry per UTC day
// and client); the model only lays them over a continuous day axis, one stacked series per client.
// Fixtures are create(ReadsPerDaySchema), the generated message the route answers with.
const NOW = Date.UTC(2026, 9, 14, 6, 30)

function reads(entries: [day: string, clientId: string, count: bigint][]) {
  return create(ReadsPerDaySchema, {
    days: entries.map(([day, clientId, count]) => ({ day, clientId, clientKind: ClientKind.SDK, reads: count })),
    clientKinds: [ClientKind.SDK],
  })
}

describe('readsPerDayModel', () => {
  it('a window of UTC days ending today, oldest first, with no entries is an axis and no series', () => {
    const model = readsPerDayModel(reads([]), 3, NOW)
    expect(model.days).toEqual(['2026-10-12', '2026-10-13', '2026-10-14'])
    expect(model.series).toEqual([])
  })

  it('the axis is 30 continuous days for the 30-day window', () => {
    const model = readsPerDayModel(reads([]), 30, NOW)
    expect(model.days).toHaveLength(30)
    expect(model.days[0]).toBe('2026-09-15')
    expect(model.days.at(-1)).toBe('2026-10-14')
  })

  it('one series per client; a day with no entry is a zero; int64 counts become numbers', () => {
    const model = readsPerDayModel(
      reads([
        ['2026-10-12', 'bot-1', 4n],
        ['2026-10-14', 'bot-1', 1n],
        ['2026-10-13', 'operator', 2n],
      ]),
      3,
      NOW,
    )
    expect(model.series).toEqual([
      { clientId: 'bot-1', reads: [4, 0, 1], total: 5 },
      { clientId: 'operator', reads: [0, 2, 0], total: 2 },
    ])
  })

  it('the biggest client first (the base of the stack); a tie is ordered by client id', () => {
    const model = readsPerDayModel(
      reads([
        ['2026-10-14', 'zed', 1n],
        ['2026-10-14', 'abe', 1n],
        ['2026-10-13', 'big', 9n],
      ]),
      3,
      NOW,
    )
    expect(model.series.map((s) => s.clientId)).toEqual(['big', 'abe', 'zed'])
  })

  it('entries outside the window, or with a malformed day, are left out rather than invented', () => {
    const model = readsPerDayModel(
      reads([
        ['2026-09-01', 'old', 5n],
        ['not-a-day', 'bad', 5n],
        ['2026-10-14', 'bot-1', 1n],
      ]),
      3,
      NOW,
    )
    expect(model.series.map((s) => s.clientId)).toEqual(['bot-1'])
    expect(model.days.at(-1)).toBe('2026-10-14')
  })

  it('a server day after the client clock ends the axis there, so the newest reads are never dropped', () => {
    const model = readsPerDayModel(reads([['2026-10-15', 'bot-1', 2n]]), 3, NOW)
    expect(model.days).toEqual(['2026-10-13', '2026-10-14', '2026-10-15'])
    expect(model.series[0]!.reads).toEqual([0, 0, 2])
  })

  it('two entries for the same day and client add up', () => {
    const model = readsPerDayModel(
      reads([
        ['2026-10-14', 'bot-1', 2n],
        ['2026-10-14', 'bot-1', 3n],
      ]),
      2,
      NOW,
    )
    expect(model.series[0]).toEqual({ clientId: 'bot-1', reads: [0, 5], total: 5 })
  })
})
