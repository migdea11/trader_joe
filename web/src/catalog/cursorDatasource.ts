// An AG Grid infinite-row-model datasource over a KEYSET-paged list (tj-grna9p.32, ADR tj-grna9p.2).
//
// A keyset cursor cannot random-access: the cursor for block N is the next_cursor of block N-1. The
// grid asks for blocks by row offset, so each block is a memoised promise built on its predecessor's:
//
//   - block 0 is fetched with no cursor;
//   - block N waits for block N-1 and fetches with its next_cursor, so a jump past the loaded rows
//     walks the pages in order instead of guessing a cursor;
//   - a block that was requested before (the grid may ask again after an eviction or a retry) is
//     served from the memo, so each cursor is fetched at most once per datasource;
//   - an empty next_cursor marks the last page, which tells the grid the real row count
//     (lastRow); until then the count is unknown and the grid grows one block at a time
//     (the grid options set cacheOverflowSize 1 and one concurrent request).
//
// A filter change or a refresh makes a NEW datasource (so a new, empty memo and no cursor survives),
// and destroy() aborts whatever the old one still had in flight.
import type { IDatasource, IGetRowsParams } from 'ag-grid-community'

export interface CursorPage<T> {
  items: readonly T[]
  /** Empty on the last page. */
  nextCursor: string
}

export type FetchCursorPage<T> = (
  cursor: string | undefined,
  limit: number,
  signal: AbortSignal,
) => Promise<CursorPage<T>>

export interface CursorDatasourceOptions<T> {
  fetchPage: FetchCursorPage<T>
  /** Rows per page, which is also the grid's cacheBlockSize. */
  pageSize: number
  /** A page arrived: the rows loaded so far and whether the end was reached. */
  onPage?: (info: { loadedRows: number; done: boolean }) => void
  /** A page failed. Aborted requests (destroy) never report. */
  onError?: (error: unknown, info: { loadedRows: number }) => void
}

export function createCursorDatasource<T>(options: CursorDatasourceOptions<T>): IDatasource {
  const { fetchPage, pageSize } = options
  const controller = new AbortController()
  const blocks = new Map<number, Promise<CursorPage<T>>>()
  const loadedByBlock = new Map<number, number>()

  const loadedRows = (): number => {
    let total = 0
    for (const count of loadedByBlock.values()) total += count
    return total
  }

  function block(start: number): Promise<CursorPage<T>> {
    let page = blocks.get(start)
    if (page === undefined) {
      page = load(start)
      blocks.set(start, page)
      // A failed block is forgotten so a retry fetches it again; the caller handles the rejection.
      page.catch(() => {
        if (blocks.get(start) === page) blocks.delete(start)
      })
    }
    return page
  }

  async function load(start: number): Promise<CursorPage<T>> {
    if (start === 0) return fetchPage(undefined, pageSize, controller.signal)
    const previous = await block(start - pageSize)
    // Nothing follows the last page: a request beyond it is empty.
    if (previous.nextCursor === '') return { items: [], nextCursor: '' }
    return fetchPage(previous.nextCursor, pageSize, controller.signal)
  }

  return {
    getRows(params: IGetRowsParams): void {
      // The grid requests whole blocks (cacheBlockSize = pageSize), so startRow is aligned.
      const start = params.startRow - (params.startRow % pageSize)
      block(start).then(
        (page) => {
          if (controller.signal.aborted) return
          loadedByBlock.set(start, page.items.length)
          const done = page.nextCursor === ''
          options.onPage?.({ loadedRows: loadedRows(), done })
          params.successCallback([...page.items], done ? start + page.items.length : undefined)
        },
        (error: unknown) => {
          if (controller.signal.aborted) return
          options.onError?.(error, { loadedRows: loadedRows() })
          params.failCallback()
        },
      )
    },
    destroy(): void {
      controller.abort()
    },
  }
}
