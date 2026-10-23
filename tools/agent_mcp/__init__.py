"""The agent-stack MCP server (ADR tj-4rr0la, addenda 1-5; build task tj-c4mosr.3).

Agents have no Docker socket and no docker CLI, by standing rule. This server runs in its own
container (tools/agent_mcp/Dockerfile, started by make agent-mcp-up) and gives them FIXED VERBS over
ONE isolated compose project, trader_joe_agent_stack:

    stack_up(worktree)  stack_down()  stack_wipe()  migrate()  migrate_status()  migrate_check()
    run_system_tests(worktree, paths)  seed_dump(worktree)  logs(service, tail)  ps()

plus TWO READ-ONLY VERBS over a second project, the user's own dev stack (ADR tj-4rr0la addendum 18;
tj-kzy7w2, approved on tj-wpnr7o (b)):

    dev_ps()  dev_logs(service, tail)

and THREE READ-ONLY EXEC VERBS into that project (ADR tj-4rr0la addendum 22; the proxy's EXEC section):

    dev_db_tables()  dev_migrate_status()  dev_http_get(name)

It finds the dev postgres container by label (exactly one running, else it refuses) and execs one
fixed psql script in it as user postgres in a read-only transaction: which of the store's tables
exist, and the alembic revision. No agent argument reaches the argv; stack.check_dev_exec_argv()
re-checks it as built, against a table that binds each whole command to its one option set. The script
redacts the container's own secret variables in-container. dev_migrate_status() does the same in the
running data_store, running alembic current and history, and refuses when data_store is not running.
dev_http_get(name) does one GET of one of three fixed paths (name is a closed enum; no URL, path or
header) in the running data_store, reads at most 64 KB of the body and redacts it in-container.

and TWO STATE-CHANGING LABEL-ONLY VERBS over the same project (tj-iee6sn.7):

    dev_down()  dev_restart(service)       service: data_store, data_ingest or web, never postgres

dev_down is `docker compose -p trader_joe down`: containers and project networks removed, never a
volume, an image or an orphan (the database is a host bind and survives). dev_restart restarts one
existing container. Neither loads a compose file or an env file. Each refuses, running nothing after
the refusal, unless the D7 guard finds every project container started from the dev files; dev_restart
also refuses a symlinked bind source (D6); both refuse a compose file or dotenv file in the stack
directory or a parent (compose would search there) and are limited to 12 state-changing dev calls an hour.

and ONE FILE-BASED BUILD VERB over it (tj-iee6sn.8):

    dev_build()

dev_build is make dev-build's build: the three :dev images from the checkout, as one plain call of up to
30 minutes (not a job). It loads the project from the MCP image's baked copies of the dev compose files
(stack.dev_file_prefix) with the repository's host path as the build context, reads no env file (a
placeholder step environment, COMPOSE_ENV_FILES=/dev/null), and stamps the images with digests from
the baked source_digest.sh run with --context over the checkout. It starts no container, so it has no
prod guard; it refuses a stale baked copy (stack.check_trusted_copies_current) and missing or old
generated code, and is limited by the same hourly budget.

and ONE MORE STATE-CHANGING VERB, make dev-up (tj-iee6sn.9):

    dev_up()

dev_up stops the dev project (label-only down, so there is only ever one instance), makes sure the
trader_joe_devnet network exists, and starts postgres, data_store, data_ingest and web detached from the
baked compose files, waiting up to 300 s for healthy; a plain call of up to 10 minutes. It never builds
(the :dev images must exist: call dev_build). Compose reads the env files itself under the lstat
preflight; the instance write secret comes from the baked dev_instance_secret.sh run inside a fixed
`sh -c` wrapper (stack.DEV_UP_WRAPPER_SCRIPT) that receives the compose argv as positional arguments, so
the value lives only in the child process and the compose argv is never script text.

and ONE MORE, make dev-tools (tj-iee6sn.14):

    dev_tools()

dev_tools makes sure the trader_joe_devnet network exists and starts pgAdmin detached (`up -d --wait
pgadmin`; compose starts postgres through depends_on), from the baked dev compose files plus the baked
docker-compose.tools.yaml (stack.dev_tools_prefix), in dev_up's secret wrapper under the pinned
environment. A plain call of up to 5 minutes; it never builds, stops or removes anything. PGADMIN_EMAIL
and PGADMIN_PASS are read by compose from the root env file: this server neither reads nor passes them,
so no credential reaches an argv, the audit line or the output; when one is unset compose refuses before
creating the container. The D7, S6, D6 and D5 checks and the budget are dev_up's. dev_down removes pgAdmin
with the rest of the project (label-only).

and TWO MIGRATION VERBS over the running dev instance (tj-iee6sn.10; owner ruling D4):

    dev_migrate()  dev_migrate_check()

dev_migrate is make migrate: `alembic upgrade head` in a one-off data_store container (file-based
`run --rm --no-deps`, the baked compose files, dev_up's secret wrapper) against the postgres that is
running now. It WRITES the dev database; the project permission for it is meant to be "ask" (the user's
setting, not this server's). dev_migrate_check is make migrate-check: `alembic check`, refused unless the
data_store :dev image's source stamp equals the checkout's digest (there is NO escape hatch; the fix is
dev_build), and it writes an empty alembic_version table on a never-migrated database. Both are refused
by the D7 guard on a non-dev project, with no or several postgres or several data_store containers, with
no revision file in the checkout, without the :dev image, and on the S6/D6/D5 checks of dev_up; neither
builds.

An agent passes a worktree NAME, test paths, a service from a closed set and a line count -- never a
compose argument, an image, a container id, a command line or a host path.

THE SECOND PROJECT, and why it costs less than it looks. The dev verbs run
`docker compose -p <stack.DEV_PROJECT> ps|logs` and nothing else: no -f, no --project-directory, no
--env-file. compose v2 loads no project when a project NAME is given and no file is, so these two
work off the containers' com.docker.compose.project label -- with no compose file there is no build
context, no bind mount and no service to act on, so nothing that creates, changes or removes
anything is constructible, and the user's root .env is never opened. The project is a CONSTANT, not
an argument, and stack.check_dev_argv() re-checks every argv before it is run. Nothing else in the
server names any project but stack.PROJECT.

MODULES
    settings.py  the server's own settings (host paths, port), from its container environment.
    stack.py     constants, argument validation, the snapshot, the generated env files, GUARD 2,
                 command builders.
    runner.py    AgentStack: the verbs, the one-at-a-time lock, timeouts, truncation, the audit log.
    auth.py      the bearer token file and the ASGI gate in front of everything.
    server.py    the MCP endpoint (the only module importing the MCP SDK).
Everything but server.py imports the standard library only, so the dev venv -- which does not install
the agent-mcp group -- can import and test it.

COMMANDS. Every docker command but the base-image pair below and the two dev-project commands above
is stack.compose_prefix() plus a fixed tail:
    /usr/local/bin/docker compose -p trader_joe_agent_stack --project-directory <stack dir>/source
        --env-file <stack dir>/agent_stack.env -f <each of stack.COMPOSE_FILES>
stack.PROJECT and stack.COMPOSE_FILES mirror the Makefile's AGENT_STACK_PROJECT and
AGENT_STACK_COMPOSE. The -f files are the MCP IMAGE's own copies (stack.TRUSTED_COMPOSE_DIR), never
the worktree's, which is agent-writable, and the overlay builds every service with the image's own
Dockerfile (stack.TRUSTED_DOCKERFILE). No argv ever carries a repository path.
Subprocesses take argument lists, never a shell, with a fixed environment (runner.DOCKER_ENV).
Before the first step of a verb that can build (stack.builds: `build`, any `--build`, `up`, or a `run`
of a stack.BUILT_SERVICES service, since compose builds a missing image), the runner has the DAEMON
make each stack.BASE_IMAGES ref present -- `docker image inspect <ref>`, then `docker pull <ref>` only
if absent -- and a failed pull stops the verb, naming the ref, before any build (ADR addenda 14-15).
The trusted Dockerfile pins those refs by digest; no build passes --pull.

THE SNAPSHOT (ADR addendum 5). The Docker daemon never resolves a path an agent can change. Before
stack_up, migrate, migrate_status, migrate_check, run_system_tests and seed_dump,
stack.refresh_snapshot() copies the named (or recorded) worktree's stack.SNAPSHOT_SOURCES
into <stack dir>/source -- no symlink, no special file,
capped in bytes and files, built beside the old copy, verified, swapped in by rename -- and that copy
is the project directory: the build context and every relative bind source. stack.check_mount_sources,
stack.resolve_test_paths and the migrations guard run on it. stack_down, stack_wipe, logs and ps read
no worktree, and the two dev verbs use no snapshot at all. The premise, checked by settings: nothing but the MCP writes under the stack directory.
The swap orphans a running container's snapshot binds, so stack_up force-recreates
stack.SNAPSHOT_BOUND_SERVICES on every call: the long-running services run the code of the LAST
stack_up, and run_system_tests rebuilds and recreates only test_client -- after editing anything a
running service loads, tests/fakes included, call stack_up first (tj-zgq5v2).

migrate_check IS THE ONE RUN THAT PASSES --build, AND THE ASYMMETRY WITH make migrate-check IS
DELIBERATE (decision tj-yb1bxj, closing section; tj-yuz1e8). `alembic check` compares the models it
imports, and migrations/env.py imports them from /code -- the IMAGE -- while only alembic.ini and
migrations/ are bind-mounted. compose builds a MISSING image on `run` but never a STALE one, so
without the flag the check would compare the last stack_up's models. The agent stack exists to
reflect the worktree under test and has no deployed image to be faithful to, so it BUILDS and the
answer is about the worktree. The deploy path wants the opposite -- there the image actually
deployed IS the subject -- so data/store/run_migrations.sh verifies the image's digest stamp and
refuses a mismatch instead. Nothing in this server routes through that script or inherits that
guard, and nothing here should: a build and a stamp refusal answer different questions.

THE GENERATED ENV FILES (stack.ensure_env_files; ADR addendum 2). Three files in the agent stack's own
directory, outside the repository, 0600, generated once from the committed .env.default files read at
the main checkout's HEAD (git cat-file; no live env file is ever opened):
    agent_stack.env         root: the --env-file AND ROOT_ENV_FILE. Random POSTGRES_PASS and
                            INSTANCE_WRITE_SECRET; the agent stack's DATABASE_NAME,
                            STORE_API_NETWORK and DATA_DIR; ROOT_ENV_FILE, STORE_ENV_FILE and
                            INGEST_ENV_FILE as absolute paths.
    agent_stack_store.env   data/store's committed variables.
    agent_stack_ingest.env  data/ingest's committed variables, ALPACA_API_KEY and ALPACA_API_SECRET
                            present and EMPTY (and absent from the other two files).
THE F1 RULE: DATABASE_NAME and STORE_API_NETWORK (and every other
stack.ROOT_ONLY_VARIABLES name) are set in the ROOT file only. env_file order is root then service
file, so a service-file value would win in the container while compose interpolated the root value --
the apps would dial a host other than the container name.

GUARD 2 IS stack.check_env_file_paths(). Every verb that hands compose an --env-file calls it
(through AgentStack._guarded_env) before its first docker subprocess -- that is every verb but the
two dev ones, which pass no env file, no bind source and no project directory, and are held instead
by stack.check_dev_argv() on every argv they build: ROOT_ENV_FILE, STORE_ENV_FILE and
INGEST_ENV_FILE must be absolute, symlink-free paths of regular files directly inside the agent stack's
directory and under no worktree; plus the F1 rule, DATA_DIR, the empty broker credentials, and no
COMPOSE_* or DOCKER_* key in any of the three files (refused, also at generation). (Guard 1 is the
overlay's ':?' labels.) The only subprocesses that may run before it are the read-only git plumbing
calls in stack.run_git(), and a worktree NAME is checked before those.

WHAT ELSE HOLDS
    stack_wipe deletes stack_dir/data only: stack.check_data_dir() refuses a symlink or any path that
        does not resolve to the configured one, before anything is stopped or deleted. After the
        in-container clear, it removes the emptied service directories and then data/ with os.rmdir;
        anything left is status 'failed' naming it, the data kept.
    Blocking work (git, the snapshot, the removal) runs off the event loop (asyncio.to_thread).
    One verb at a time: a second caller gets status 'busy' naming the running verb, never a queue.
    Every verb has a timeout (runner.VERB_TIMEOUT_SECONDS), reported as status 'timeout'.
    Output: exit status plus stdout/stderr per step, each cut to its last runner.OUTPUT_CAP_BYTES
        with the cut stated. The env files are never printed.
    Audit: one JSON line per call in <stack dir>/audit.log -- UTC time, verb, known, validated
        arguments, status, exit status, duration; an unknown verb by its name cut to 64 characters,
        known: false. No output, no env value, and never the token.
    Auth: auth.BearerTokenMiddleware refuses any request without the token, before the MCP app.

DOCKER API SECTIONS THE VERBS NEED -- the socket proxy (tj-c4mosr.4) enables exactly these, as
docker-socket-proxy's variables, everything else off:
    PING, VERSION, INFO    the CLI's and compose's handshake.
    CONTAINERS             create/start/stop/remove/inspect/logs/wait/attach: up, down, run, logs, ps.
    IMAGES                 pull postgres and the BASE_IMAGES, tag and inspect images.
    NETWORKS, VOLUMES      the project's networks; compose inspects volumes on up and down.
    BUILD, SESSION, GRPC   image builds through BuildKit (compose build, run --build).
    EVENTS                 compose up --wait and run follow container events.
    EXEC                   start and inspect an exec: the read-only dev diagnostics (dev_db_tables,
                           dev_migrate_status; ADR tj-4rr0la addendum 22). Creating one is POST under CONTAINERS.
    POST=1                 every one of the above that writes.
NOT needed, keep off: AUTH, SECRETS, CONFIGS, PLUGINS, SYSTEM, SWARM, NODES, SERVICES, TASKS,
DISTRIBUTION, COMMIT. The list is read from what compose calls for these verbs and is confirmed at
the host sitting (tj-c4mosr.6); a verb failing with 403 from the proxy names the missing section.
THE DEV VERBS WIDEN NOTHING (tj-kzy7w2 asked this be checked rather than assumed): `compose ps` of a
project is GET /containers/json filtered on the project label and `compose logs` adds GET
/containers/{id}/logs, both CONTAINERS, already on for the agent stack's own ps and logs. The proxy
is not what bounds these verbs in any case -- it has no notion of a compose project, and never did;
the fixed verbs are, which is ADR tj-4rr0la addendum 1 (b) and is now load-bearing for a second
project as well.
"""
