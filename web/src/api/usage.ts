// The constants the usage and retention features of Datasets share (tj-grna9p.88). One module, so a
// later control for any of them is a small change.
import { ClientKind } from '@generated/trader_joe/proto/ui/v1/data_pb'

/** The UI's own identity, sent as X-Client-Id on every data_store call (the server derives kind UI from it). */
export const OPERATOR_CLIENT_ID = 'operator'

/** Unread for this many days is Stale. Fixed: no Settings control (owner ruling, tj-utqd77 Q3). */
export const STALE_DAYS = 90

/** Expiring Soon looks this many days ahead. The server's own default is the same. */
export const EXPIRING_SOON_DAYS = 7

/** Whose reads the usage figures count when the Read By filter is untouched. */
export const DEFAULT_CLIENT_KINDS: readonly ClientKind[] = [ClientKind.SDK]
