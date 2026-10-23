"""The agent-stack MCP server (ADR tj-4rr0la, addenda 1-5 and 26-28; build task tj-c4mosr.3).

Agents have no Docker socket and no docker CLI, by standing rule. This server runs in its own
container (tools/agent_mcp/Dockerfile, started by make agent-mcp-up) and gives them FIXED VERBS over
ONE compose project, the owner's own (stack.DEV_PROJECT, trader_joe), in its DEV or TEST mode. There
is no second, isolated stack: the separate agent stack (its own compose project, generated env files
and data directory, and its own verbs stack_up, stack_down and stack_wipe, plus its separate
logs and ps, which the one-project logs and ps below replace) was retired in favour of modes of
the one project (addenda 26-28). The fifteen verbs:

    ps()  logs(service, tail)  db_tables()  http_get(name)  migrate_status()
    down()  restart(service)  build()  up()  tools()
    migrate()  test_build()  test_up()
    run_system_tests(worktree, paths)  seed_dump(worktree, date)

TWO READ-ONLY VERBS (ADR tj-4rr0la addendum 18; tj-kzy7w2, approved on tj-wpnr7o (b)):

    ps()  logs(service, tail)

They run `docker compose -p trader_joe ps|logs` and nothing else.

and THREE READ-ONLY EXEC VERBS into that project (ADR tj-4rr0la addendum 22; the proxy's EXEC section):

    db_tables()  migrate_status()  http_get(name)

It finds the dev postgres container by label (exactly one running, else it refuses) and execs one
fixed psql script in it as user postgres in a read-only transaction: which of the store's tables
exist, and the alembic revision. No agent argument reaches the argv; stack.check_dev_exec_argv()
re-checks it as built, against a table that binds each whole command to its one option set. The script
redacts the container's own secret variables in-container. migrate_status() does the same in the
running data_store of whichever mode is up, running alembic current and history, and refuses when
data_store is not running.
http_get(name) does one GET of one of three fixed paths (name is a closed enum; no URL, path or
header) in the running data_store, reads at most 64 KB of the body and redacts it in-container.

and TWO STATE-CHANGING LABEL-ONLY VERBS over the same project (tj-iee6sn.7):

    down()  restart(service)       service: data_store, data_ingest or web, never postgres

down is `docker compose -p trader_joe down`: containers and project networks removed, never a
volume, an image or an orphan (the database is a host bind and survives). restart restarts one
existing container. Neither loads a compose file or an env file. Each refuses, running nothing after
the refusal, unless the D7 guard finds every project container started from the dev files; restart
also refuses a symlinked bind source (D6); both refuse a compose file or dotenv file in the stack
directory or a parent (compose would search there) and are limited to 12 state-changing dev calls an hour.

and ONE FILE-BASED BUILD VERB over it (tj-iee6sn.8):

    build()

build is make dev-build's build: the three :dev images from the checkout, as one plain call of up to
30 minutes (not a job). It loads the project from the MCP image's baked copies of the dev compose files
(stack.dev_file_prefix) with the repository's host path as the build context, reads no env file (a
placeholder step environment, COMPOSE_ENV_FILES=/dev/null), and stamps the images with digests from
the baked source_digest.sh run with --context over the checkout. It starts no container, so it has no
prod guard; it refuses a stale baked copy (stack.check_trusted_copies_current) and missing or old
generated code, and is limited by the same hourly budget.

and ONE MORE STATE-CHANGING VERB, make dev-up (tj-iee6sn.9):

    up()

up stops the dev project (label-only down, so there is only ever one instance), makes sure the
trader_joe_devnet network exists, and starts postgres, data_store, data_ingest and web detached from the
baked compose files, waiting up to 300 s for healthy; a plain call of up to 10 minutes. It never builds
(the :dev images must exist: call build). Compose reads the env files itself under the lstat
preflight; the instance write secret comes from the baked dev_instance_secret.sh run inside a fixed
`sh -c` wrapper (stack.DEV_UP_WRAPPER_SCRIPT) that receives the compose argv as positional arguments, so
the value lives only in the child process and the compose argv is never script text.

and ONE MORE, make dev-tools (tj-iee6sn.14):

    tools()

tools makes sure the trader_joe_devnet network exists and starts pgAdmin detached (`up -d --wait
pgadmin`; compose starts postgres through depends_on), from the baked dev compose files plus the baked
docker-compose.tools.yaml (stack.dev_tools_prefix), in up's secret wrapper under the pinned
environment. A plain call of up to 5 minutes; it never builds, stops or removes anything. PGADMIN_EMAIL
and PGADMIN_PASS are read by compose from the root env file: this server neither reads nor passes them,
so no credential reaches an argv, the audit line or the output; when one is unset compose refuses before
creating the container. The D7, S6, D6 and D5 checks and the budget are up's. down removes pgAdmin
with the rest of the project (label-only).

and TWO MIGRATION VERBS over whichever mode of the one project is up (tj-iee6sn.10; owner ruling: ONE
migrate, make migrate's semantics):

    migrate()  migrate_status()

migrate is make migrate: it reads the mode from the mode guard (DEV or TEST; the message starts with the
definition it chose, as make migrate prints) and runs `alembic upgrade head` in a one-off data_store
container (file-based `run --rm --no-deps`, the baked compose files) against the postgres that is
running now. DEV: the dev files and the :dev image, through up's secret wrapper. TEST: the base file
and docker-compose.fake.yaml and the :test image (no wrapper), DATA_DIR pinned to ./volume_data/test; a
prod image bakes the code in, so it is refused unless the RUNNING data_store and data_ingest containers
carry a source stamp equal to the checkout's digest (stack.check_container_stamp: stale test stack, call
test_build then test_up). It WRITES the database of the mode that is up, the owner's own dev database in
DEV mode; the permission for it is the owner's client setting, not this server's. It is refused when
nothing is up, under a PROD container or both modes, with no or several postgres or several data_store
containers, with no revision file in the checkout, without the mode's data_store image, and on the
S6/D6/D5 checks of up; it builds nothing. migrate_status is read-only: alembic current and history by
exec in the running data_store of whichever mode is up, charging no budget. alembic check is not offered:
make migrate-check covers it on the host.

and TWO TEST-MODE VERBS over the same project (tj-iee6sn.24; ADR tj-4rr0la addenda 26-27):

    test_build()  test_up()

test_build is make test-build: the TEST-mode images (data_store and data_ingest, named by docker-compose.fake.yaml, the
prod target, stamped with the source digests) from the baked base file, docker-compose.fake.yaml and the
MCP-only docker-compose.agent-test.yaml, which sets build.dockerfile alone. No file set it can use names
:latest, so it never builds or tags that tag, which may be a real deployment's. test_up is make
system-launch: it stops the project, then starts postgres, data_store and data_ingest from the base and
fake files with DATA_DIR pinned to ./volume_data/test in the step environment (never the owner's dev
data), data_ingest on the fake broker. It never builds: both :test images must exist and carry a source
stamp equal to the checkout's digest (stack.check_dev_image_stamp), else it is refused naming test_build.

THE MODE GUARD. Every state-changing verb that acts on the project first lists its non-one-off containers
and classifies each by the basename of its compose files (stack.classify_mode, run_migrations.sh's
classifier): DEV (override, no fake), TEST (fake, no override), PROD (anything else, both together
included). Any PROD container, or DEV and TEST together, refuses the verb; tools acts on DEV (or an
empty project) only, and migrate and migrate_status on DEV or TEST (an empty project is refused). up and test_up act on any non-PROD state because they stop it.

An agent passes a worktree NAME, test paths, a service from a closed set and a line count -- never a
compose argument, an image, a container id, a command line or a host path.

HOW THE LABEL-ONLY VERBS ARE BOUNDED, and why it costs less than it looks. ps, logs and the
two label-only state-changing verbs run `docker compose -p <stack.DEV_PROJECT> ps|logs|down|restart`
and nothing else: no -f, no --project-directory, no --env-file. compose v2 loads no project when a
project NAME is given and no file is, so these work off the containers' com.docker.compose.project
label -- with no compose file there is no build context, no bind mount and no service to act on, so
nothing that creates a container is constructible, and the user's root .env is never opened. The
project is a CONSTANT, not an argument, and stack.check_dev_argv() and the lifecycle check re-check
every argv before it is run. Every verb here names stack.DEV_PROJECT and no other project.

MODULES
    settings.py  the server's own settings (host paths, port), from its container environment.
    stack.py     constants, argument validation, the snapshot, command builders and their argv checks.
    runner.py    AgentStack: the verbs, the one-at-a-time lock, timeouts, truncation, the audit log.
    auth.py      the bearer token file and the ASGI gate in front of everything.
    server.py    the MCP endpoint (the only module importing the MCP SDK).
Everything but server.py imports the standard library only, so the dev venv -- which does not install
the agent-mcp group -- can import and test it.

COMMANDS. Every docker command but the base-image pair below is `docker compose -p trader_joe` plus a
prefix and a fixed tail, built in stack.py and re-checked as built:
    label-only (ps, logs, down, restart): the four words, nothing else.
    file-based (build, up, tools, migrate, test_build, test_up): --project-directory is the
        repository's HOST path (settings.repo_host_path) and each -f is the MCP IMAGE's own copy
        (stack.TRUSTED_COMPOSE_DIR) of the dev, tools or test set, never the worktree's, which is
        agent-writable. The three MCP-only overlays (docker-compose.agent-dev.yaml, -test.yaml and
        -client.yaml) build with the image's own Dockerfile (stack.TRUSTED_DOCKERFILE).
    system-test client (run_system_tests, seed_dump): stack.testclient_prefix(), the snapshot as the
        project directory and the root env file as --env-file.
    exec (db_tables, http_get, migrate_status): `docker exec` into a container found by label.
No argv ever carries a repository path an agent chose.
Subprocesses take argument lists, never a shell, with a fixed environment (runner.DOCKER_ENV).
Before the first step of a verb that can build (stack.builds: `build`, any `--build`, `up`, or a `run`
of a stack.BUILT_SERVICES service, since compose builds a missing image), the runner has the DAEMON
make each stack.BASE_IMAGES ref present -- `docker image inspect <ref>`, then `docker pull <ref>` only
if absent -- and a failed pull stops the verb, naming the ref, before any build (ADR addenda 14-15).
The trusted Dockerfile pins those refs by digest; no build passes --pull.

THE SNAPSHOT (ADR addendum 5). The Docker daemon never resolves a path an agent can change. Before
run_system_tests and seed_dump, stack.refresh_snapshot() copies the named worktree's
stack.SNAPSHOT_SOURCES into <stack dir>/source -- no symlink, no special file, capped in bytes and
files, built beside the old copy, verified, swapped in by rename -- and that copy is the project
directory of test_client: its build context and every relative bind source. stack.check_mount_sources
and stack.resolve_test_paths run on it. No other verb reads a worktree or takes a snapshot (migrate
included: it runs from the baked files against the checkout's host path). The premise, checked by
settings: nothing but the MCP writes under the stack directory.
run_system_tests and seed_dump run test_client of the ONE project (trader_joe) against TEST mode,
started by test_up. The services there run the :test images of the ROOT checkout; test_client and
every mount it has come from the snapshot of the named worktree, so a worktree's unintegrated tests
reach the client. Rails, in order and each refusing before anything runs: only TEST mode is up (the
mode guard), the running postgres's data mount is <repo host path>/volume_data/test/postgres
(stack.check_test_postgres_mount), every running data_store and data_ingest container's source stamp
equals the checkout's digest; then the snapshot and one charge of the state-changing budget. The
compose command is stack.check_testclient_argv's. The client reads the instance write secret from the
root env file through compose (--env-file), exactly as data_store does; this server never opens that
file, and the secret is on no command line.

THE STACK DIRECTORY (settings.stack_dir, AGENT_MCP_STACK_DIR) holds the snapshot and the audit log and
nothing else: there are no generated env files and no data directory. The project's data is the
checkout's ./volume_data (dev) and ./volume_data/test (test mode).

WHAT ELSE HOLDS
    Blocking work (git, the snapshot) runs off the event loop (asyncio.to_thread).
    One verb at a time: a second caller gets status 'busy' naming the running verb, never a queue.
    Every verb has a timeout (runner.VERB_TIMEOUT_SECONDS), reported as status 'timeout'.
    Output: exit status plus stdout/stderr per step, each cut to its last runner.OUTPUT_CAP_BYTES
        with the cut stated. No env file is printed, and none is read by this server's Python.
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
    EXEC                   start and inspect an exec: the read-only dev diagnostics (db_tables,
                           migrate_status; ADR tj-4rr0la addendum 22). Creating one is POST under CONTAINERS.
    POST=1                 every one of the above that writes.
NOT needed, keep off: AUTH, SECRETS, CONFIGS, PLUGINS, SYSTEM, SWARM, NODES, SERVICES, TASKS,
DISTRIBUTION, COMMIT. The list is read from what compose calls for these verbs and is confirmed at
the host sitting (tj-c4mosr.6); a verb failing with 403 from the proxy names the missing section.
THE DEV VERBS WIDEN NOTHING (tj-kzy7w2 asked this be checked rather than assumed): `compose ps` of a
project is GET /containers/json filtered on the project label and `compose logs` adds GET
/containers/{id}/logs, both CONTAINERS, which the file-based verbs need anyway. The proxy is not
what bounds these verbs in any case -- it has no notion of a compose project, and never did; the
fixed verbs are, which is ADR tj-4rr0la addendum 1 (b).
"""
