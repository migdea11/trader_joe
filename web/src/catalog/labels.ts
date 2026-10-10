// Display words for the catalog's enum columns and filter options (tj-grna9p.32). The design canvas
// Datasets board shows "Alpaca", "Alpaca · IEX", "1d", "Bulk", "Daily"; where the canvas has no word
// for a value (IB_API, MANUAL_ENTRY, the monthly width) the word here is provisional, listed as an
// open question on the bead.
import { DataSource, DataType, Feed, Granularity, UpdateType } from '@generated/trader_joe/proto/market/v1/enums_pb'

import { MISSING } from '@/format/formatters'

const SOURCE_LABELS: Partial<Record<DataSource, string>> = {
  [DataSource.ALPACA_API]: 'Alpaca',
  [DataSource.IB_API]: 'IBKR',
  [DataSource.MANUAL_ENTRY]: 'Manual',
}

const FEED_LABELS: Partial<Record<Feed, string>> = {
  [Feed.IEX]: 'IEX',
  [Feed.SIP]: 'SIP',
}

const GRANULARITY_LABELS: Partial<Record<Granularity, string>> = {
  [Granularity.ONE_MINUTE]: '1m',
  [Granularity.FIVE_MINUTES]: '5m',
  [Granularity.THIRTY_MINUTES]: '30m',
  [Granularity.ONE_HOUR]: '1h',
  [Granularity.ONE_DAY]: '1d',
  [Granularity.ONE_WEEK]: '1w',
  [Granularity.ONE_MONTH]: '1mo',
}

// The canvas calls a STATIC (fixed-range) dataset Bulk.
const UPDATE_LABELS: Partial<Record<UpdateType, string>> = {
  [UpdateType.STATIC]: 'Bulk',
  [UpdateType.DAILY]: 'Daily',
  [UpdateType.STREAM]: 'Stream',
}

/** Every bar width in ascending order, for the Viewer's granularity switch. */
export const GRANULARITY_OPTIONS: readonly Granularity[] = [
  Granularity.ONE_MINUTE,
  Granularity.FIVE_MINUTES,
  Granularity.THIRTY_MINUTES,
  Granularity.ONE_HOUR,
  Granularity.ONE_DAY,
  Granularity.ONE_WEEK,
  Granularity.ONE_MONTH,
]

/** Bars narrower than a day show a time of day as well as the date. */
export function isIntraday(granularity: Granularity): boolean {
  return (
    granularity === Granularity.ONE_MINUTE ||
    granularity === Granularity.FIVE_MINUTES ||
    granularity === Granularity.THIRTY_MINUTES ||
    granularity === Granularity.ONE_HOUR
  )
}

// The canvas names a market-activity dataset's content "bars" in the Viewer title (VFV · bars · 1d).
const DATA_TYPE_LABELS: Partial<Record<DataType, string>> = {
  [DataType.MARKET_ACTIVITY]: 'bars',
  [DataType.QUOTE]: 'quotes',
  [DataType.TRADE]: 'trades',
}

export function dataTypeLabel(dataType: DataType): string {
  return DATA_TYPE_LABELS[dataType] ?? MISSING
}

export function sourceLabel(source: DataSource): string {
  return SOURCE_LABELS[source] ?? MISSING
}

export function updateTypeLabel(updateType: UpdateType): string {
  return UPDATE_LABELS[updateType] ?? MISSING
}

export function granularityLabel(granularity: Granularity): string {
  return GRANULARITY_LABELS[granularity] ?? MISSING
}

/** "Alpaca · IEX"; the feed is left out where it does not apply, as on the canvas. */
export function sourceFeedLabel(source: DataSource, feed: Feed): string {
  const feedLabel = FEED_LABELS[feed]
  return feedLabel === undefined ? sourceLabel(source) : `${sourceLabel(source)} · ${feedLabel}`
}
