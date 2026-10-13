// The Viewer's Export CSV (tj-grna9p.126, canvas board "Export CSV popover"): the bars of a
// half-open window [start, end) walked by cursor, serialised page by page into text chunks and joined
// into ONE Blob at the end. A page's decoded bars are dropped as soon as they are written, so the
// browser never holds the whole range as objects. The total is not known up front (the server counts
// nothing for a bar window), so progress is the TIME position of the last bar read inside the range.
import { getDatasetBars } from '@/api'
import { int64ToNumber } from '@/api/int64'

import { CSV_HEADER, barsToCsvRows } from './csv'

/** Bars asked per request: the server's maximum page size (50_000, tj-grna9p.125). */
export const EXPORT_PAGE_SIZE = 50_000

export interface ExportBarsParams {
  datasetId: string
  /** Include bars at or after this instant; unset reads from the first stored bar. */
  start?: Date
  /** Exclude bars at or after this instant; unset reads to the last stored bar. */
  end?: Date
  timeZone: string
  /** Bars narrower than a day write the time of day too. */
  intraday: boolean
  /** The range's extent in epoch ms, for progress: the window's bounds, or the stored bars' extent. */
  spanStart?: number
  spanEnd?: number
  signal: AbortSignal
  /** 0..1, after each page. Never goes back. */
  onProgress?: (fraction: number) => void
}

export interface ExportedCsv {
  blob: Blob
  bars: number
}

function barMs(bar: { barStart?: { seconds: bigint } }): number | null {
  return bar.barStart ? int64ToNumber(bar.barStart.seconds) * 1000 : null
}

/**
 * Read every bar of the window and build the file. Resolves null when `signal` aborted (the partial
 * file is dropped); rejects with the request's error otherwise.
 */
export async function exportBarsCsv(params: ExportBarsParams): Promise<ExportedCsv | null> {
  const { datasetId, start, end, timeZone, intraday, spanStart, spanEnd, signal, onProgress } = params
  const chunks: string[] = [`${CSV_HEADER}\n`]
  let count = 0
  let cursor: string | undefined
  let reached = 0
  try {
    for (;;) {
      const page = await getDatasetBars(datasetId, { start, end, cursor, limit: EXPORT_PAGE_SIZE, signal })
      if (signal.aborted) return null
      if (page.bars.length > 0) {
        chunks.push(barsToCsvRows(page.bars, timeZone, intraday))
        count += page.bars.length
        const last = barMs(page.bars[page.bars.length - 1]!)
        if (last !== null && spanStart !== undefined && spanEnd !== undefined && spanEnd > spanStart) {
          reached = Math.max(reached, Math.min(1, Math.max(0, (last - spanStart) / (spanEnd - spanStart))))
          onProgress?.(reached)
        }
      }
      // The last page has no cursor; a repeated one would never end, so it stops the walk too.
      if (page.nextCursor === '' || page.nextCursor === cursor || page.bars.length === 0) break
      cursor = page.nextCursor
    }
  } catch (caught) {
    if (signal.aborted) return null
    throw caught
  }
  onProgress?.(1)
  return { blob: new Blob(chunks, { type: 'text/csv;charset=utf-8' }), bars: count }
}
