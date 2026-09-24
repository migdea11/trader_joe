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