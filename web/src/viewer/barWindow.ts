// The bounded bar load behind the Viewer's chart (tj-grna9p.33).
//
// STRATEGY. The chart gets the bars of the selected window by following the keyset cursor in
// CHART_PAGE_SIZE pages, in order, until the last page or until MAX_CHART_BARS bars are held. It
// never asks for more than the cap: the final request's limit is the room left. A window with more
// bars than the cap shows its FIRST MAX_CHART_BARS (the cursor only walks forward) and reports
// `truncated`, which the screen says out loud and answers with "narrow the range": the From and To
// inputs set a smaller window, and the table below still pages the whole window. At most
// MAX_CHART_BARS / CHART_PAGE_SIZE = 5 requests, one after another (a cursor needs its predecessor).
import type { Bar } from '@generated/trader_joe/proto/market/v1/bar_pb'

/** The most bars the chart holds: 5 full pages of the server's maximum page size. */
export const MAX_CHART_BARS = 50_000
/** The page size the chart load asks for: the server's maximum (10,000). */
export const CHART_PAGE_SIZE = 10_000

/** Rows per page of the bar table: its grid block size. */
export const TABLE_PAGE_SIZE = 500

export interface BarPageResult {
  bars: readonly Bar[]
  /** Empty on the last page. */
  nextCursor: string
}

export type FetchBarPage = (
  cursor: string | undefined,
  limit: number,
  signal: AbortSignal,
) => Promise<BarPageResult>

export interface BarWindowResult {
  bars: Bar[]
  /** More bars exist in the window than were loaded. */
  truncated: boolean
  /** Requests made. */
  pages: number
}

export interface BarWindowOptions {
  fetchPage: FetchBarPage
  signal: AbortSignal
  /** Defaults to MAX_CHART_BARS. */
  cap?: number
  /** Defaults to CHART_PAGE_SIZE. */
  pageSize?: number
}

/** Load the window's bars, bounded by the cap. Rejects as the fetch does (and on abort). */
export async function loadBarWindow(options: BarWindowOptions): Promise<BarWindowResult> {
  const cap = options.cap ?? MAX_CHART_BARS
  const pageSize = options.pageSize ?? CHART_PAGE_SIZE
  const bars: Bar[] = []
  let cursor: string | undefined
  let pages = 0
  for (;;) {
    const limit = Math.min(pageSize, cap - bars.length)
    const page = await options.fetchPage(cursor, limit, options.signal)
    pages += 1
    for (const bar of page.bars) bars.push(bar)
    if (page.nextCursor === '') return { bars, truncated: false, pages }
    if (bars.length >= cap) return { bars, truncated: true, pages }
    cursor = page.nextCursor
  }
}
