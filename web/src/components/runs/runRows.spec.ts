import { describe, expect, it } from 'vitest'
import { create } from '@bufbuild/protobuf'
import { timestampFromMs } from '@bufbuild/protobuf/wkt'

import {
  IngestRunErrorSchema,
  IngestRunSchema,
  IngestRunState,
  RunTrigger,
} from '@generated/trader_joe/proto/ui/v1/data_pb'

import { RUNNING_BADGE } from '@/theme/semantics'
import { colors } from '@/theme/tokens'
import { describeRunError, humanizeToken, runDurationMs, runStateBadge, triggerLabel } from './runRows'

// tj-grna9p.34: what a collection run reads as in the Dataset detail's History table. The server
// decides the state, trigger and error; these helpers only put them into words.

const T0 = Date.UTC(2026, 9, 12, 14, 0, 0)

describe('triggerLabel', () => {
  it.each([
    [RunTrigger.REQUEST, 'Request'],
    [RunTrigger.SCHEDULE, 'Schedule'],
    [RunTrigger.RETRY, 'Retry'],
    [RunTrigger.UNSPECIFIED, '—'],
    [99 as RunTrigger, '—'],
  ])('%s reads %s', (trigger, label) => {
    expect(triggerLabel(trigger)).toBe(label)
  })
})

describe('runStateBadge', () => {
  it('Running is the collection Running badge', () => {
    expect(runStateBadge(IngestRunState.RUNNING)).toEqual(RUNNING_BADGE)
  })

  it.each([
    [IngestRunState.SUCCEEDED, 'Succeeded', colors.status.ok],
    [IngestRunState.FAILED, 'Failed', colors.status.fail],
    // Empty (the vendor held no bars) is neither a failure nor a success with data: neutral colour.
    [IngestRunState.EMPTY, 'Empty', colors.text2],
    [IngestRunState.UNSPECIFIED, 'Unknown', colors.text2],
    [42 as IngestRunState, 'Unknown', colors.text2],
  ])('%s reads %s', (state, label, color) => {
    expect(runStateBadge(state)).toEqual({ label, color })
  })
})

describe('humanizeToken', () => {
  it.each([
    ['RATE_LIMITED', 'Rate limited'],
    ['vendor', 'Vendor'],
    ['data-ingest.alpaca', 'Data ingest alpaca'],
    ['  NO__BARS  ', 'No bars'],
    ['', ''],
    ['___', ''],
  ])('%j reads %j', (token, words) => {
    expect(humanizeToken(token)).toBe(words)
  })
})

describe('describeRunError', () => {
  const error = (domain: string, reason: string, message = '') => create(IngestRunErrorSchema, { domain, reason, message })

  it('a failed run reads its reason, then its domain, in words, with the server message apart', () => {
    expect(describeRunError(error('VENDOR', 'RATE_LIMITED', '429 from Alpaca'))).toEqual({
      headline: 'Rate limited (Vendor)',
      message: '429 from Alpaca',
    })
  })

  it('only a reason, only a domain, or neither', () => {
    expect(describeRunError(error('', 'TIMEOUT'))!.headline).toBe('Timeout')
    expect(describeRunError(error('STORE', ''))!.headline).toBe('Store')
    expect(describeRunError(error('', ''))!.headline).toBe('Failed')
  })

  it('no error is null', () => {
    expect(describeRunError(undefined)).toBeNull()
  })
})

describe('runDurationMs', () => {
  it('a finished run is its wall time', () => {
    const run = create(IngestRunSchema, { startedAt: timestampFromMs(T0), finishedAt: timestampFromMs(T0 + 83_500) })
    expect(runDurationMs(run)).toBe(83_500)
  })

  it('a Running run has no finish, so no duration (the screen does not tick a clock)', () => {
    const run = create(IngestRunSchema, { state: IngestRunState.RUNNING, startedAt: timestampFromMs(T0) })
    expect(runDurationMs(run)).toBeNull()
  })

  it('a missing start, or a finish before the start, is no duration rather than a wrong one', () => {
    expect(runDurationMs(create(IngestRunSchema, { finishedAt: timestampFromMs(T0) }))).toBeNull()
    expect(
      runDurationMs(create(IngestRunSchema, { startedAt: timestampFromMs(T0), finishedAt: timestampFromMs(T0 - 1) })),
    ).toBeNull()
  })
})
