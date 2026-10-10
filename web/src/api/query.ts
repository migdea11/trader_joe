// Query-string building for the REST client.

export type QueryValue = string | number | boolean | Date | null | undefined

/**
 * Serialise a parameter map to a query string, with its leading `?`, or '' when nothing is set.
 *
 * Undefined, null and empty-string values are omitted (the server's defaults then apply). A Date
 * becomes an RFC 3339 instant with an explicit offset (`...Z`), because the server rejects naive
 * datetimes. Keys keep insertion order.
 */
export function buildQuery(params: Readonly<Record<string, QueryValue>>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    search.append(key, value instanceof Date ? value.toISOString() : String(value))
  }
  const text = search.toString()
  return text === '' ? '' : `?${text}`
}
