// The typed error every failed API call surfaces as (ADR tj-fa1rpu problem+json, RFC 9457).
//
// Clients branch on `reason` (never on title or detail) and quote `errorId` when reporting. The
// response body's text -- detail, title, validation messages -- is never copied into a message the
// UI shows (tj-fa1rpu D8): `message` is built from the status alone.

export class ApiError extends Error {
  readonly status: number
  /** The Reason value (UPPER_SNAKE), absent when the failure is not one of ours (routing 404, 401, ...). */
  readonly reason: string | undefined
  /** The id the failure is logged under; quote it when reporting. */
  readonly errorId: string | undefined

  constructor(init: { status: number; reason?: string; errorId?: string }) {
    super(`Request failed (HTTP ${init.status})`)
    this.name = 'ApiError'
    this.status = init.status
    this.reason = init.reason
    this.errorId = init.errorId
  }
}

function text(value: unknown): string | undefined {
  // error_id may be a string or a list of strings (problem+json metadata); the first names the failure.
  if (typeof value === 'string' && value !== '') return value
  if (Array.isArray(value) && typeof value[0] === 'string' && value[0] !== '') return value[0]
  return undefined
}

/** Build an ApiError from a non-2xx response. Reads only status, reason and error_id from the body. */
export async function apiErrorFromResponse(response: Response): Promise<ApiError> {
  let reason: string | undefined
  let errorId: string | undefined
  const type = response.headers.get('content-type') ?? ''
  if (type.includes('json')) {
    try {
      const body: unknown = await response.json()
      if (typeof body === 'object' && body !== null) {
        const members = body as Record<string, unknown>
        reason = text(members.reason)
        errorId = text(members.error_id)
      }
    } catch {
      // A body that is not JSON carries nothing we may use; the status still names the failure.
    }
  }
  return new ApiError({ status: response.status, reason, errorId })
}
