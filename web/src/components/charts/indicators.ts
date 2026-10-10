// Pure indicator maths for PriceChart. Plain numbers in, plain numbers out: nothing here touches
// protobuf messages or the chart library.

/**
 * Simple moving average of `values` over `period` points. Entry i is the mean of
 * values[i - period + 1 .. i]; the first period - 1 entries are NaN (no full window yet), which
 * the caller turns into whitespace.
 */
export function simpleMovingAverage(values: ArrayLike<number>, period: number): Float64Array {
  const n = values.length
  const out = new Float64Array(n).fill(NaN)
  if (!Number.isInteger(period) || period < 1 || period > n) return out
  let sum = 0
  for (let i = 0; i < n; i++) {
    sum += values[i]
    if (i >= period) sum -= values[i - period]
    if (i >= period - 1) out[i] = sum / period
  }
  return out
}
