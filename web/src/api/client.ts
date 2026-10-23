// The fetch wrapper: same-origin GETs that decode protobuf canonical JSON with fromJson(Schema)
// (ADR tj-grna9p.4) and surface every failure as an ApiError.

import { fromJson } from '@bufbuild/protobuf'
import type { DescMessage, MessageShape } from '@bufbuild/protobuf'

import { ApiError, apiErrorFromResponse } from './errors'
import { buildQuery } from './query'
import { OPERATOR_CLIENT_ID } from './usage'
import type { QueryValue } from './query'

/**
 * The browser's base path for the data store. The browser calls same-origin paths only: Caddy in
 * production and the Vite dev proxy (vite.config.ts) both route `/api/store/*` to data_store with
 * the prefix stripped, so `/api/store/ui/v1/datasets` reaches the store's `/ui/v1/datasets`.
 */
export const API_BASE = '/api/store'

export interface GetOptions {
  query?: Readonly<Record<string, QueryValue>>
  signal?: AbortSignal
}

/**
 * GET `path` (under API_BASE) and decode the 2xx body as `schema`'s message.
 *
 * Unknown JSON fields are ignored, so a server that adds a field (additive proto change) does not
 * break an older UI build.
 *
 * @throws ApiError for a non-2xx response, a network failure (status 0) or a body that does not
 *   decode as the message (status of the response).
 */
export async function getMessage<Desc extends DescMessage>(
  schema: Desc,
  path: string,
  options: GetOptions = {},
): Promise<MessageShape<Desc>> {
  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}${buildQuery(options.query ?? {})}`, {
      method: 'GET',
      headers: { Accept: 'application/json', 'X-Client-Id': OPERATOR_CLIENT_ID },
      signal: options.signal,
    })
  } catch (error) {
    // A cancelled request is the caller's own doing, not an API failure.
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new ApiError({ status: 0 })
  }
  if (!response.ok) throw await apiErrorFromResponse(response)
  try {
    return fromJson(schema, await response.json(), { ignoreUnknownFields: true })
  } catch {
    throw new ApiError({ status: response.status, reason: 'MALFORMED_RESPONSE' })
  }
}

/** A write's JSON body, in protobuf canonical JSON (a Timestamp is an RFC 3339 string). */
export type WriteBody = Readonly<Record<string, unknown>>

export interface WriteOptions {
  /** The request body; omitted for a write that carries none. */
  body?: WriteBody
  signal?: AbortSignal
}

async function send(method: 'PATCH' | 'DELETE', path: string, options: WriteOptions): Promise<Response> {
  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      headers: {
        Accept: 'application/json',
        'X-Client-Id': OPERATOR_CLIENT_ID,
        ...(options.body === undefined ? {} : { 'Content-Type': 'application/json' }),
      },
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
    })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new ApiError({ status: 0 })
  }
  if (!response.ok) throw await apiErrorFromResponse(response)
  return response
}

/**
 * PATCH `path` (under API_BASE) with a JSON body and decode the 2xx body as `schema`'s message.
 *
 * @throws ApiError as getMessage does.
 */
export async function patchMessage<Desc extends DescMessage>(
  schema: Desc,
  path: string,
  options: WriteOptions = {},
): Promise<MessageShape<Desc>> {
  const response = await send('PATCH', path, options)
  try {
    return fromJson(schema, await response.json(), { ignoreUnknownFields: true })
  } catch {
    throw new ApiError({ status: response.status, reason: 'MALFORMED_RESPONSE' })
  }
}

/** DELETE `path` (under API_BASE), with an optional JSON body, for a write that answers 204. */
export async function deleteRequest(path: string, options: WriteOptions = {}): Promise<void> {
  await send('DELETE', path, options)
}
