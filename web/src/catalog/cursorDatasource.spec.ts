import { describe, expect, it, vi } from 'vitest'

import { createCursorDatasource } from './cursorDatasource'

// Scaffolding smoke only (tj-grna9p.32): the keyset chain, with each cursor fetched once. The full
// case list belongs to the validator (tj-grna9p.40).
describe('cursor datasource smoke', () => {
  it('chains cursors across blocks and fetches each page once', async () => {
    const fetchPage = vi.fn(async (cursor: string | undefined) => {
      if (cursor === undefined) return { items: ['a', 'b'], nextCursor: 'c1' }
      return { items: ['c'], nextCursor: '' }
    })
    const source = createCursorDatasource<string>({ fetchPage, pageSize: 2 })
    const rows: string[][] = []
    const request = (startRow: number) =>
      new Promise<number | undefined>((resolve) => {
        source.getRows({
          startRow,
          endRow: startRow + 2,
          successCallback: (block: string[], lastRow?: number) => {
            rows.push(block)
            resolve(lastRow)
          },
          failCallback: () => resolve(-1),
        } as never)
      })

    // Asking for the second block first still walks the chain from the first page.
    expect(await request(2)).toBe(3)
    expect(await request(0)).toBeUndefined()
    expect(rows).toEqual([['c'], ['a', 'b']])
    expect(fetchPage.mock.calls.map(([cursor]) => cursor)).toEqual([undefined, 'c1'])
  })
})
