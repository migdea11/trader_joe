import { create, fromJson, isFieldSet, toJson } from '@bufbuild/protobuf'
import { describe, expect, expectTypeOf, it } from 'vitest'

import { SetCollectingRequestSchema } from '@generated/trader_joe/proto/ui/v1/data_pb'
import type { SetCollectingRequest } from '@generated/trader_joe/proto/ui/v1/data_pb'

// tj-grna9p.182 (finding F1 on tj-grna9p.174): SetCollectingRequest.collect is `optional bool collect = 1`,
// so the UI can only send a stop by writing collect false, and an empty body (no collect key) is what the
// route refuses with 422. Before F1 a false was the proto3 default and dropped on the wire, so a stop and a
// forgotten body were the same JSON.
describe('SetCollectingRequest.collect has presence', () => {
  const field = SetCollectingRequestSchema.field.collect

  it('is field 1 and the generated type marks it optional', () => {
    expect(field.number).toBe(1)
    expectTypeOf<SetCollectingRequest['collect']>().toEqualTypeOf<boolean | undefined>()
    expect(create(SetCollectingRequestSchema).collect).toBeUndefined()
  })

  it('writes an explicit false and an explicit true, and omits an unset collect', () => {
    expect(toJson(SetCollectingRequestSchema, create(SetCollectingRequestSchema, { collect: false }))).toEqual({ collect: false })
    expect(toJson(SetCollectingRequestSchema, create(SetCollectingRequestSchema, { collect: true }))).toEqual({ collect: true })
    expect(toJson(SetCollectingRequestSchema, create(SetCollectingRequestSchema))).toEqual({})
  })

  it('reads presence back: a parsed false is set, a parsed empty body is not', () => {
    const stop = fromJson(SetCollectingRequestSchema, { collect: false })
    expect(isFieldSet(stop, field)).toBe(true)
    expect(stop.collect).toBe(false)
    expect(isFieldSet(fromJson(SetCollectingRequestSchema, {}), field)).toBe(false)
  })
})
