// Query-string building for the REST client.

type QueryScalar = string | number | boolean | Date

/** An array is a repeated parameter: one `key=value` pair per element. */
export type QueryValue = QueryScalar | readonly QueryScalar[] | null | undefined

/**
 * Serialise a parameter map to a query string, with its leading `?`, or '' when nothing is set.
 *
 * Undefined, null and empty-string values are omitted (the server's defaults then apply). A Date
 * becomes an RFC 3339 instant with an explicit offset (`...Z`), because the server rejects naive
 * datetimes. An array repeats the key per element (an empty one sends nothing). Keys keep insertion
 * order.
 */
export function buildQuery(params: Readonly<Record<string, QueryValue>>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    for (const item of Array.isArray(value) ? value : [value]) {
      search.append(key, item instanceof Date ? item.toISOString() : String(item))
    }
  }
  const text = search.toString()
  return text === '' ? '' : `?${text}`
}
