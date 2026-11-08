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
  filters, cuts the selection to `size` in symbol order, and records what happened to each member as ledger
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
`request_cap_per_hour`. A creation costs **two** requests (`REQUESTS_PER_CREATION`): the name-change clamp query and
the bars request. The ramp is checked first, and once a window is full the rest of the members in that pass share its
cause. This limits creation only: the daily bars of members are the DAILY refresh's, bounded by
`DAILY_REFRESH_MAX_PER_PASS`, so a large panel needs that cap raised to about the panel size.

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
as `actor`, and a required non-blank `reason`.

| Write | Route | Notes |
|---|---|---|
| Create a panel | `POST /ui/v1/panels` | Writes the panel and its settings version 1 in one transaction, answers the panel detail with `effective_pass` unset ("first pass pending"). It runs no pass: the next tick does, once the panel is enabled |
| Change a panel | `POST /ui/v1/panels/{panel_id}/settings-versions` | A new version replaces the settings as sent: an absent optional filter clears it. **Enable and disable are this same write with `enabled` changed** |
| Change a source's rates | `POST /ui/v1/panel-sources/{source}/settings-versions` | The per-source `ramp_datasets_per_hour`, `request_cap_per_hour` and the corporate-action window days. They apply to every panel on the source |

Rules that decide a create (all refusals are `INVALID_REQUEST`, 422, with the store's message, and write nothing):

- `enabled` and `size` are required (an absent value is never read as `false` or zero); `cadence` unnamed takes
  `MONTHLY`; `name` is required, trimmed and unique; `source` is required and fixed for the panel's life; `reason` is
  required.
- **The two hourly rates have no default.** A source with no source settings version cannot run a panel: give the
  first source settings in the create (both `ramp_datasets_per_hour` and `request_cap_per_hour` required; the window
  days default to 90), or the create is refused. Giving source settings for a source that already has a version is also
  refused: change them only through the source's own write. A ramp of 0 pauses creation. If the manager meets a
  panel whose source has no settings it logs once `Panel <id> is not runnable: source <source> has no settings` and
  skips it.
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
Six of them are writes and carry the secret. The panel rules had never been parsed by Caddy before the production
sitting (`tj-mx97hu`): before saving any panel setting, validate the Caddyfile, probe each of the seven rules, and
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
| `panel_settings_version` | A panel's versions, `version_no` ascending, with `enabled`, `cadence`, `size`, the filters, `actor`, `reason`, `saved_at` |
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
   an actor and a reason.
4. Raise `DAILY_REFRESH_MAX_PER_PASS` to about the panel size.
5. Confirm: an active and an inactive asset-list snapshot written within minutes; the first `panel_pass` row, naming
   settings version 1; and the first close-time pass showing in dataset Health.

If a pass behaves unexpectedly, set `PANEL_MANAGER_DRY_RUN=true` or `PANEL_MANAGER_ENABLED=false` and restart before
anything else; neither loses a row.
