# Runbook: the panel manager

The panel manager is the loop in `data_store` that decides which assets a **panel** holds and creates their datasets.
This runbook is for the person running it: what it does, how to switch it off or rehearse it, how to create a panel,
and how to read what it did. The code is `server/data/store/app/panel_manager.py`; the design is
[`server/data/store/CLAUDE.md`](../../server/data/store/CLAUDE.md) ("Panels and the panel manager"). Panels are
UI-only: they are made and edited in the web UI's Panels tab (`/data/panels`) through the `/ui/v1` panel routes in
[`API.md`](../API.md); there is no SDK panel API.

## What it does

A panel is a named selection of one source's assets, owned by the reserved owner `panel`, which `POST /store` refuses.
The manager keeps one dataset for each selected asset: DAILY, `1day`, `ROLLING`, open-ended, collecting.

**One loop, nothing stored.** The loop ticks every 5 minutes (`TICK_INTERVAL`, a constant, not a setting) and the
first tick runs at start. Each tick works out from the stored rows what is due, so a crash or restart resets nothing.
For each enabled panel of each source that has a trading calendar, a failure ends that panel's turn only; it logs
`Panel pass for panel <id> failed: <ErrorType>` and the next panel runs.

A tick runs, for each enabled panel, either:

- a **membership pass**, when one is due (below). It reads the newest active asset-list snapshot, applies the panel's
  filters, keeps every asset that passes (a panel has no size and no cut; the order is symbol, then vendor asset id), and records what happened to each member as ledger
  transitions: `ENTERED`, `EXITED`, `DRAINING`, `DRAINED`, `REFUSED`, and for a member it creates, `CREATED` (or
  `DEFERRED` when a rate window is full); or
- a **release pass**, between membership passes: it creates the members whose latest transition is `DEFERRED` while
  both hourly windows have room. It decides no membership and writes its pass row only at its first transition.

**When a membership pass is due.** The first pass runs at the panel's creation, on the newest snapshot without waiting.
After that the due instant is an interval from the last **finished** non-dry-run membership pass: `MONTHLY` one calendar
month (the default), `WEEKLY` seven days, `DAILY` the first session close after it plus the settle margin
(`DAILY_REFRESH_SETTLE_MINUTES`). A pass needs an active snapshot at or after the due time; it waits for one until the
next close plus settle, then runs on the older one and logs `Panel <id> pass runs on a snapshot older than its due
time (none arrived)`. With no active snapshot at all it logs once `Panel <id> waits: no active snapshot for <source>`.

**What it creates and how fast.** The rate is per source and per hour, read from the rows each time over a sliding 60
minutes: `CREATED` rows against `ramp_datasets_per_hour`, and every request the `panel` owner wrote against
`request_cap_per_hour`. A creation costs **two** requests against these limits (`REQUESTS_PER_CREATION`): the name-change clamp query and
the bars request. Ingest makes a third vendor call for each creation, the asset lookup by symbol on the trading API,
which counts against neither the request cap nor the market-data budget. The ramp is checked first, and once a window is full the rest of the members in that pass share its
cause. A rate that is empty (no limit) skips its check. This limits creation only: the daily bars of members are the DAILY refresh's, bounded by
`DAILY_REFRESH_MAX_PER_PASS`. That variable is the most **existing** collecting `DAILY` datasets on a source, the panel's members and every other owner's together, that one pass requests the missing tail for; new datasets are not counted, since the panel manager creates them under the source's hourly limits. The default of 15000 covers a source with up to that many; above it, raise the cap. With the default, one pass submits the tail of every owed dataset, one after another, without waiting for any. The vendor works through them at its rate limit: at the ceiling of 200 a minute, 15,000 one-page requests (a 1day tail fits in one page) take at least about 75 minutes, and longer when other requests share the limit or `ALPACA_RATE_LIMIT_PER_MINUTE` lowers it.

Requests that carry a deadline (`ready_by`) go ahead of undated ones, and the refresh's requests carry one (the next market open) while a panel's creations do not, so creations wait behind a large refresh.

**A refresh request still queued at the next market open expires unrun.** It is recorded as failed, cause expired, and the dataset is requested again at the next pass, a session late; no bar is lost. A pass that starts shortly before the open, such as the catch-up after a restart, can expire most of a large backlog this way.

**Warning: a first pass with no rates and no filter is very long.** With both rates empty and no filter, a panel
selects every row of the newest ACTIVE asset list (14,399 rows on the dev stack's STOCK list) and creates every member in one pass. There is no big transaction: each member gets its own small commits, so
the database is safe at that size. Time is what bounds it. Ingest allows at most 200 market-data requests a minute
(`ALPACA_RATE_LIMIT_PER_MINUTE`, which configuration can only lower), and the clamp and the bars request of each
creation both draw from that one bucket. The asset lookup draws from the trading scope. A clamp query whose answer
spans several pages costs one token per page (a panel's 1day bars request, about 9,200 sessions from 1990, fits one
10,000-bar page). As an observation from a dev run against the real vendor, not a guarantee: the pass made about 77
creations a minute (80.8 a minute over its first 48 seconds), so about 3 hours for 14,399 members, with the
market-data side near 155 of its 200 requests a minute and no 429 answers. A larger asset list or a slower vendor
takes longer. Manager runs never overlap, so for those hours no other panel on any source gets a pass. Ingest keeps one queue per broker, shared by both of its quota scopes (market data and trading): requests with a
ready-by go first, and undated work follows in arrival order. The panel manager's requests and the asset-list snapshot
carry no ready-by, so the snapshot queues behind the burst in arrival order; the daily refresh carries a ready-by and
is served ahead of it. The asset list draws its tokens from the trading scope, so it waits because it is queued, not
because tokens run out (`docs/scheduler.md`). A clamp that waits past 300 s becomes a deferral (`clamp_unavailable`) that a later pass picks up.
Set the rates, or a filter, if that matters.

**A member's dataset** starts at `panel_dataset_start`: the source calendar's local midnight of 1990-01-01
(`CALENDAR_START`), never UTC midnight, which would fall on the previous local date before the calendar's first day.
The name-change clamp can move it later. `CREATED` names the bars **request**, never a dataset: the delivery intake
mints the dataset on the first delivered attempt, as for any POST, and the manager never waits for the verdict.

## Switching it off, and rehearsing

Both settings are read when `data_store` starts, so a change needs a restart.

| Setting | Parsing | Effect |
|---|---|---|
| `PANEL_MANAGER_ENABLED` | Off only for `false`, `0`, `no` or `off` (any case, whitespace trimmed); anything else, unset included, is on | Off: no loop starts, and the log says `The panel manager is off (PANEL_MANAGER_ENABLED); no panel pass runs` once. This is the process-wide kill switch. Every panel is also off until a settings version enables it |
| `PANEL_MANAGER_DRY_RUN` | On only for `true`, `1`, `yes` or `on`; anything else, unset included, is off | On: a pass writes a ledger flagged `dry_run` and applies nothing: no dataset is created, none is stopped and no release tick runs. Every read ignores dry-run rows. The dry-run manager reads only dry-run passes for its own due rule, so a real crashed pass does not hold a dry run off |

To rehearse before a real run: set `PANEL_MANAGER_DRY_RUN=true`, restart, let one pass run, and read the decisions in
`panel_member_transition` (the pass is flagged `dry_run`). Then clear it and restart. To stop the manager at once, set
`PANEL_MANAGER_ENABLED=false` and restart; datasets it already created keep collecting, and nothing new is created.

Both scenarios are pinned on the fake stack: the kill switch writes no pass row and no panel-owned request
(`tests/system/phased/phase_s6a_panel_kill_switch.py`), and a dry run writes a finished dry-run membership pass whose
only recorded decision is `ENTERED` (`tests/system/phased/phase_s6b_panel_dry_run.py`).

## Creating a panel and changing it

Every write appends a version row; nothing is edited in place. Each version carries `saved_at`, the operator principal
as `actor`, and an optional `reason`: a blank one means none, is stored as NULL and reads back as an empty string.

| Write | Route | Notes |
|---|---|---|
| Create a panel | `POST /ui/v1/panels` | Writes the panel and its settings version 1 in one transaction, answers the panel detail with `effective_pass` unset ("first pass pending"). It runs no pass: the next tick does, once the panel is enabled |
| Change a panel | `POST /ui/v1/panels/{panel_id}/settings-versions` | A new version replaces the settings as sent: an absent optional filter clears it. **Enable and disable are this same write with `enabled` changed** |
| Change a source's rates | `POST /ui/v1/panel-sources/{source}/settings-versions` | The per-source `ramp_datasets_per_hour` (1 or more), `request_cap_per_hour` (2 or more) and the corporate-action window days (required, 0 or more). Both rates are optional: empty means no limit. They apply to every panel on the source |

Rules that decide a create (all refusals are `INVALID_REQUEST`, 422, with the store's message, and write nothing):

- `enabled` is required (an absent value is never read as `false`); `cadence` unnamed takes
  `MONTHLY`; `name` is required, trimmed and unique; `source` is required and fixed for the panel's life; `reason` is
  optional. A body that carries `size` is refused with a 422: the field was removed and an unknown field is refused.
- **The two hourly rates are optional; empty means no limit.** A set ramp is a whole number from 1 to 2147483647 and a
  set cap from 2 to 2147483647 (each new panel dataset costs two requests, so a cap of 1 could never create one); zero
  is refused, and so is anything outside that range. An empty rate is stored as NULL and read as unset. A create on a
  source with **no settings version** always writes that source's version 1 from whatever source settings fields it
  carries: an empty rate is no limit and empty window days are 90, so a create carrying none writes no limits and
  90/90. A create on a source that **already has a version** refuses any source settings field (422, nothing written):
  change them only through the source's own write. That includes the case where another create wrote the source's
  version 1 first while this one saw no version: save the settings through the source's own write instead. If the
  manager ever meets a panel whose source has no settings (a guard; a create always writes them) it logs once
  `Panel <id> is not runnable: source <source> has no settings` and skips it.
- **Pausing a panel is `enabled` false** (a new version). It stops that panel's passes only (no new creations, no exits, no releases): its members stay as
  recorded and their datasets keep collecting through the daily refresh. Stopping the datasets too is a backlog item
  ("Panel: disabling a panel should pause its members' datasets"), not built. To empty a panel, change its filters so
  they select nothing: every member then exits and its dataset is stopped, unless another enabled panel on the source
  still selects it.
- **The web create form** validates on click: nothing is marked until Create Panel has been pressed once, then every
  empty or invalid field is marked at once and the marks follow the fields. A name and a source are the only required
  fields. Ramp and cap are shown only while the source has no settings version and are both optional, with the
  hints "Leave empty for no limit." and, for the cap, "Minimum 2: each new panel dataset costs 2 requests." (the window
  days are optional too and default to 90); the reason is optional.
- Creates are serialised by name (a transaction-scoped advisory lock), so the loser of two concurrent creates of one
  name is refused as a duplicate; source settings are judged under a lock on the source.
- Effective-from has no column: a version takes effect at the `started_at` of the first non-dry-run pass of that panel
  that names it.

**The writes need the instance secret.** All three are behind it (a 401 comes before body validation, then 422, then
404 for an unknown panel). The web edge adds the secret for the browser, and the browser holds none. A panel write
leaves no override-log row and publishes no event: the version row is the audit.

**The seven edge rules.** The web edge (Caddy in production, the Vite dev server in development) forwards exactly seven
rules to `data_store` and answers 404 to anything else: GET and HEAD under `/api/store/ui/v1/`; PATCH on a dataset's
`expiry`; PATCH on a dataset's `collecting`; DELETE on a dataset; and POST on the three panel paths in the table above.
Six of them are writes and carry the secret. Before saving any panel setting, validate the Caddyfile, probe each of the seven rules, and
send one off-list POST that must answer 404.

## Exits and re-entry

- A member the filter no longer selects, whose row is still listed, goes `EXITED` and its collecting dataset is stopped
  (unless another enabled panel on the source still selects the symbol). The stop is marked `stopped_cause`
  `PANEL_MEMBER_LEFT`.
- A member gone from the vendor's list goes `DRAINING` while a panel-owned dataset exists, and becomes `DRAINED` (dataset
  stopped) once a fetch of that dataset has finished since the drain began. With no dataset it goes `EXITED` at once.
- A dataset minted after its member left is stopped too, but only if its request finished at or after the exit, so an
  operator's later Resume is left alone.
- **Re-entry.** A member that returns restarts its dataset only if the manager stopped it (`PANEL_MEMBER_LEFT`; an
  operator's stop has no cause and is left stopped) and its last accepted vendor asset id is empty or the member's. The
  restart is in the same commit as `ENTERED`, whose cause is `dataset_resumed`; the daily refresh then fills the gap.
- `RETIRED` and `RESUMED` events are published at the end of a pass. A pass that fails after an earlier commit has
  already committed its stops and restarts but sends no frame: the datasets are right, and a reload shows them.

## A crashed pass

A pass is written by one session from `open_pass` to `finish_pass`. A pass that never finishes (a crash between the two)
is left as written, because the ledger is append-only, and **satisfies nothing**: it does not count as the last pass. It
holds the next membership pass off for `UNFINISHED_PASS_RETRY` (one hour), so a pass that crashes every time retries at
most hourly, never at the tick rate. One WARNING names it, once per pass:
`Panel <panel id> membership pass <pass id> never finished`. To find them: `panel_pass` rows with `finished_at` null.
Do not edit or delete the row. Find out why it failed from the `Panel pass for panel <id> failed: <ErrorType>` line
beside it.

## Reading the ledger

| Table | Read it as |
|---|---|
| `panel` | One row per panel: `name` (unique), `source`, `created_at` |
| `panel_settings_version` | A panel's versions, `version_no` ascending, with `enabled`, `cadence`, the filters, `actor`, `reason` (NULL when none), `saved_at` |
| `panel_source_settings_version` | A source's rates and window days by `version_no`, shared by its panels |
| `panel_pass` | One row per pass: `kind` (`MEMBERSHIP` or `RELEASE`), the settings versions and the snapshots it ran under, `dry_run`, `started_at`, `finished_at` (null until it finishes) and `counts` |
| `panel_member_transition` | The membership ledger: `transition`, `symbol`, `vendor_asset_id`, `dataset_id` or `request_id` where one applies, `cause`, `as_of`, `recorded_at` |

Two ways to read it right:

- **A member's current state is its newest transition**, ordered by `as_of`, then the pass's `started_at`, then the
  pass, then `seq` (the append ordinal inside the pass: `id` is random and `recorded_at` is the transaction start, so
  a member's `ENTERED` and `CREATED` of one pass tie on everything else). Ignore rows of dry-run passes unless you are
  reading a rehearsal. The UI reads the same ledger (`GET /ui/v1/panels/{panel_id}/members` and `/boundaries`).
- **`as_of` is evidence, not the pass time.** Under the same settings version as the previous pass, a transition is
  dated at the first snapshot since that pass that shows the new outcome, so a symbol that entered the vendor's list
  between two passes is dated when it did. A settings-driven change is dated by the pass's snapshot.

The causes the manager writes (free strings, no CHECK in the schema):

| Cause | On | Meaning |
|---|---|---|
| `ramp_rate` | `DEFERRED` | The ramp window (`ramp_datasets_per_hour`) is full |
| `request_rate` | `DEFERRED` | The request window (`request_cap_per_hour`) cannot take another creation's two requests |
| `clamp_unavailable` | `DEFERRED` | The name-change clamp query failed: the member waits, it is not refused |
| `clamp_window` | `REFUSED` | The clamped start leaves no window (unreachable while panel datasets are open-ended) |
| `own_overlap` | `REFUSED` | The panel owner's own dataset collides with the new one (the same feed-blind check as `POST /store`) |
| `vendor_refused` | `REFUSED` | The creation request finished with a PERMANENT failure, so it is not retried while the member stays in |
| `dataset_resumed` | `ENTERED` | The member's manager-stopped dataset was restarted in the same commit |

`DEFERRED` with the same cause repeated is not a new row. A `DEFERRED` member is released by a later release pass, and a
raised rate is picked up at the next tick.

## Checks at a production sitting

Generic steps only; the panel's name, its filters and the operator values belong to the operator and stay out of this
repository.

1. Take the pre-migration database dump, then migrate straight to head.
2. Validate the Caddyfile and probe the seven edge rules and the off-list POST (above), **before any panel settings are
   saved**.
3. Create the panel with the `MONTHLY` cadence and, if the source has none, its source settings version 1, each with
   an actor (a reason is optional).
4. Check `DAILY_REFRESH_MAX_PER_PASS` (default 15000) against the number of collecting `DAILY` datasets on the source, this panel's selection included; raise it only if there are more.
5. Confirm: an active and an inactive asset-list snapshot written within minutes; the first `panel_pass` row, naming
   settings version 1; and the first close-time pass showing in dataset Health.

If a pass behaves unexpectedly, set `PANEL_MANAGER_DRY_RUN=true` or `PANEL_MANAGER_ENABLED=false` and restart before
anything else; neither loses a row.
