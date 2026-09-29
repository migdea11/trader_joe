# TraderJoe
framework for testing trading algos

## Design
![Design Image](docs/img/design.jpg)

## Getting Started
Take a look at the `Makefile` for all the major commands. Every compose target names the stack it
means — `dev-*` builds and runs the development images (live reload, debug logging, the debugger,
source bind-mounted from this checkout, and pgAdmin), `prod-*` the production ones. Never mix a
`dev-` build with a `prod-` launch; they produce different images.

```
# Development
make dev-build
make dev-launch     # foreground, so you see the reload output; Ctrl-C stops it

# Production
make prod-build
make prod-launch    # detached, and blocks until every service reports healthy
make migrate        # see below — nothing else creates the schema
make prod-logs      # follow the service logs
```

## Dependencies and the lockfile

`uv.lock` is **frozen by default**. The `Makefile` exports `UV_FROZEN=1`, so every `uv` it runs
installs the lock exactly as committed and never re-resolves it — editing `pyproject.toml` does not
change the lock, and a new dependency does not reach the virtualenv, until you re-lock on purpose:

```
make lock    # the one deliberate re-lock; commit uv.lock with the pyproject.toml change
```

Two guards sit around that. `pyproject.toml` sets an install cooldown (`exclude-newer = "7 days"`),
so a resolve never picks a release younger than a week. And every target that can write the lock
refuses to run under a `uv` older than the version pinned in the `Makefile`, because an older `uv`
can re-resolve and overwrite the lock — reverting pinned versions and the cooldown — while every
command still exits 0.

## Writing data: the instance secret

Every dataset **write** route needs a shared secret; reads need nothing. Set it once per deployment:

```
echo "INSTANCE_WRITE_SECRET=$(openssl rand -hex 32)" >> .env
```

and send it on each write as the `X-Instance-Secret` header. The check fails closed: while the
variable is unset or empty, **every write is rejected** with a 401, and the service logs why.

Be clear about what this is. **The secret authenticates the deployment, not the caller.** Each
dataset records an `owner` — the principal the caller declares on the request — and a write naming the
wrong owner is refused. But with one key there is effectively one principal: anyone holding the key
can declare any owner. The owner check protects against mistakes, not against a holder of the key, and
it does nothing for the services' other surfaces, which still listen on every interface with default
or no auth. `docs/API.md` has the full contract.

## How market data is stored

A **dataset** is one request for data, identified by every field it was requested with: owner,
symbol, asset type, data type, source, granularity, expiry type, update type, and the start and end of
its range. Two requests that differ in any of them are two datasets, and each holds **its own copy**
of its bars — a bar belongs to exactly one dataset and is deleted with it. That makes a dataset's
contents unambiguous and its deletion a cascade, at the cost of duplicate storage when datasets
overlap. Repeating a request exactly reuses the existing dataset; overlapping one of your own datasets
without matching it is refused with the colliding id.

Bars are stored **raw**. There are no split or dividend factors on a bar, and corporate-action
adjustment on read is decided but **not built** — everything returned today is unadjusted.

## Database migrations

Nothing in the stack creates the schema on its own — not the container entrypoint, not the
service at startup. A stack that reports healthy still has an **empty database** until the
migrations are applied.

```
make migrate
```

**Run this after every deploy**, and after any pull that brings new revisions. It is the only
supported spelling of this step: run it by hand today, and the server deploy script will call the
same target once it exists.

Start the database first — `make prod-deps` (or `make prod-launch`), then `make migrate`. The
target requires postgres to already be running and exits non-zero with a pointer if it is not. It
will not start the database itself on purpose: bringing the stack `up` recreates a container whose
config has changed, and a migration must never restart the database it is migrating. For the same
reason it is not wired into any launch target: a rollback re-runs `compose up`, and a migration
hanging off that would re-apply from whichever revision directory happened to be checked out.

`make migrate` is production-shaped — it applies the revisions through the production `data_store`
service definition. That is still the right command on a dev stack, since both stacks share the
one postgres container, but expect it to want the production image built (`make prod-build`).

The revisions applied are the ones in **this checkout**, not the ones baked into the deployed
image — `alembic.ini` and `data/store/migrations/` reach the container as bind mounts. Run it from
a checkout that matches the image you deployed.

### Revisions that are not additive

Migrations here are meant to be **additive**: a revision adds, and never destroys what an earlier
column meant. When one cannot be, it says so on a line of its own in its docstring:

```
# additive-exception: <decision record id>
```

The marker means *this revision destroys schema, and the repository owner agreed to that*. The
exception is granted by the owner, not by whoever writes the migration, and the id names the record
where it was granted. Two revisions carry it today, `8f41c2d7a3b9` and `eec8f88a7443`, and in
current practice both also state, in the same docstring, what is destroyed and what `downgrade()`
does and does not restore. No record makes that a rule yet; it is the pattern to follow.

**Nothing enforces this mechanically yet.** The CI gate that would reject an unmarked non-additive
revision (`tj-aw0tuk`) has not been built. Today the marker is read by reviewers, and by one test,
which checks only that the current head revision still carries its marker.

**`eec8f88a7443` expects empty tables.** It rebuilds both market-data tables for the per-dataset
model and adds the bar's `feed` column `NOT NULL` with no default, so on a bar table that still holds
rows it **fails with a constraint error** by design. Wipe `stock_market_activity` and
`store_dataset_entry` before applying it; the revision's docstring has the procedure, including why
`docker compose down -v` does not do it here. Its `downgrade()` restores the old schema and nothing
else — feed, owner and expiry values written under it are not recoverable.

**What has been checked on real Postgres.** On 2026-09-25 an earlier form of this revision upgraded
cleanly on the host, a second `make migrate` was a no-op, and `make migrate-status` reported it as
head. The revision as it stands now, with the NULL backfill for `expiry_type` and `update_type` added
after that run, has not been applied to a real database, and neither its downgrade nor its failure
on a non-empty bar table has been run yet.

### Checking what is applied

```
make migrate-status
```

Read-only: it runs `alembic current`, which reads the version table to report the revision the
database is actually at, and `alembic history`, which lists the revisions this checkout would
apply. Nothing in it writes. It goes through the same script as `make migrate`, so it needs
postgres running and fails the same way on an empty revisions directory.

Read the two answers together. Because the applied revisions come from the checkout while the
running code comes from the deployed image, the two can disagree with nothing saying so — a
database at a revision this checkout has never heard of means you are in the wrong checkout, and
a history that runs past `current` means the deploy is missing `make migrate`.

## Database shell

There is no make target for this, deliberately: it is a thin `psql` wrapper and a debugging step,
not part of the standard workflow, and a target would suggest otherwise.

```
docker compose -f docker-compose.yaml exec postgres \
    sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

The variables are expanded **inside the container**, which is why they are in single quotes: the
credentials come from `.env` via compose, and compose does not export them into your own shell.
`-f docker-compose.yaml` for the same reason `make migrate` pins it — a bare `docker compose` also
loads the dev override. The one postgres container is shared by both stacks, so this reaches the
same database either way.

`make dev-tools` starts pgAdmin as a GUI alternative, but it is reachable only through the
**development** stack: it lives in `docker-compose.tools.yaml`, which only `dev-tools` and
`dev-down` load and no `prod-*` target ever does, and it needs `PGADMIN_EMAIL`/`PGADMIN_PASS` set.
Running a production stack, the command above is your documented way in.