# Alpaca recorded responses

Bodies answering alpaca-py's HTTP in `data/ingest/tests/test_alpaca_read_recorded.py`, through the
harness in `data/ingest/tests/alpaca_recorded.py`. One response per file:

```json
{"provenance": {"kind": "documented" | "recorded", ...}, "status": 200, "body": { ... }}
```

## Recorded

Recorded 2026-10-01 at the host sitting (tj-irhy0a.3, item H4) by `tests/fakes/record_alpaca.py`,
with paper keys, from `GET https://data.alpaca.markets/v2/stocks/bars`, symbol `AAPL`, feed `iex`
(the tape paper keys are entitled to). Each file is the recorder's output byte for byte; its
`provenance` carries `kind: recorded`, `recorded_at` and the endpoint. The request each one answers
is the matching `Spec` in the recorder.

| File | Status | What the recording settled |
|---|---|---|
| `bars_1Day_page1.json`, `bars_1Day_page2.json` | 200 | Paging: `limit=3` over five trading days; page 2 was requested with page 1's token. |
| `bars_1Min.json`, `bars_5Min.json`, `bars_30Min.json`, `bars_1Hour.json`, `bars_1Day.json` | 200 | One body per timeframe. Daily bars are stamped at 05:00Z (midnight New York, EST). |
| `bars_1Week.json` | 200 | A weekly bar is stamped with its Monday at 05:00Z. |
| `bars_1Month.json` | 200 | A monthly bar is stamped with the 1st at 05:00Z. |
| `bars_range_boundary.json` | 200 | Alpaca's end is inclusive: a bar at the requested start and one at the requested end both come back. |
| `bars_empty_absent_symbol.json` | 200 | The real empty-range body (a weekend) is `{"bars": {}, "next_page_token": null}`: the symbol key is absent. `bars` is an empty object, not `null`. |
| `error_400.json` | 400 | The error body is `{"message": "..."}` alone. There is no `code` field, which the documented body had guessed at. The tests never read `code`. |

The bodies hold market data only: prices, volumes, trade counts, timestamps, an opaque page token
and an error message. They contain no header, credential, query string or account identifier.

## Documented, not recorded

These are built by hand from Alpaca's published response shape,
<https://docs.alpaca.markets/us/reference/stockbars>, and the wire contract pinned in research
tj-vhboky.57. Prices and volumes are illustrative. Each file's `provenance.kind` says `documented`.

| File | Why it is not recorded |
|---|---|
| `error_429.json`, `error_504.json`, `error_500.json` | You cannot provoke a rate limit, a gateway timeout or a server error on purpose. They keep the documented `{code, message}` body. The recorded 400 suggests that real error bodies may carry only `message`. Nothing reads `code`: the reader classifies on the HTTP status alone, and a test fails if anything on the path reads it. |
| `bars_empty_list.json` | This is the other empty-range shape (`{"bars": {"AAPL": []}}`). Alpaca sent the absent-key shape when recorded, but the reader must still handle both, so the tests keep both. Both are served as an empty window. |
| `bars_null.json` | A 200 whose `bars` is JSON `null`. It has never been recorded on this endpoint. It is inferred from alpaca-py 0.44.0 `common/rest.py:395` and two forum threads, which its `provenance` names. It pins one property: such a body is never served as a window. |

## Headers

No fixture records a response header, because the recorder writes none. Which headers a real 429
carries (`X-RateLimit-Reset`, `Retry-After`, or neither) is unknown. A test that needs one attaches
it with `with_headers()` from the harness, and its file says the headers are constructed.
| `bars_1Day_fractional_trade_count.json`, `bars_1Day_null_trade_count.json` | Alpaca cannot be made to send either. Each one is FAKES-2's documented daily body with the second bar's `n` changed, as the table below shows. They do not follow the recorded `bars_1Day.json`, and the null-trade-count test pins their own values. |

| File | Second bar's `n` | What it pins |
|---|---|---|
| `bars_1Day_fractional_trade_count.json` | `831423.5` | A trade count that is not a whole number fails the fetch with ValueError. It is never truncated. |
| `bars_1Day_null_trade_count.json` | `null` | A missing trade count reaches `Bar.trade_count` as None. |

## Re-recording

Run `tests/fakes/record_alpaca.py` by hand on the host, never in CI, with `--out` pointing at a
directory outside the repo. Read the output before copying it in. The recorder reads
`ALPACA_API_KEY` and `ALPACA_API_SECRET` from the environment and calls only
`GET /v2/stocks/bars`. It writes only the response status and body, with a provenance block. It
never writes a header, a credential or a query string. The tests derive every query bound from the
bodies, so a re-recorded file drops in without a test edit. Recording on 2026-10-01 confirmed this:
no test changed.

Never put a credential in a file here.
