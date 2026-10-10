// protobuf-es decodes int64 (bar counts, facet counts, trade_count) as bigint. Nothing in the UI
// reads a bigint directly: this is the one place it becomes a number, used at the chart boundary
// (and wherever a count is formatted for display).

/**
 * Convert a decoded int64 to a number.
 *
 * @throws RangeError when the value is outside the safe-integer range, where a number would silently
 *   lose precision.
 */
export function int64ToNumber(value: bigint): number {
  if (value > BigInt(Number.MAX_SAFE_INTEGER) || value < BigInt(Number.MIN_SAFE_INTEGER)) {
    throw new RangeError(`int64 ${value} is outside the safe integer range`)
  }
  return Number(value)
}
