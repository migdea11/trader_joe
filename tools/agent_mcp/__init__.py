"""The agent-stack MCP server (ADR tj-4rr0la, addenda 1-5; build task tj-c4mosr.3).

Agents have no Docker socket and no docker CLI, by standing rule. This server runs in its own
container (tools/agent_mcp/Dockerfile, started by make agent-mcp-up) and gives them FIXED VERBS over
ONE isolated compose project, trader_joe_agent_stack:

    stack_up(worktree)  stack_down()  stack_wipe()  migrate()  migrate_status()
    run_system_tests(worktree, paths)  seed_dump(worktree)  logs(service, tail)  ps()

An agent passes a worktree NAME, test paths, a service from a closed set and a line count -- never a
compose argument, an image, a container id, a command line or a host path.

MODULES
    settings.py  the server's own settings (host paths, port), from its container environment.
    stack.py     constants, argument validation, the snapshot, the generated env files, GUARD 2,
                 command builders.
    runner.py    AgentStack: the verbs, the one-at-a-time lock, timeouts, truncation, the audit log.
    auth.py      the bearer token file and the ASGI gate in front of everything.
    server.py    the MCP endpoint (the only module importing the MCP SDK).
Everything but server.py imports the standard library only, so the dev venv -- which does not install
the agent-mcp group -- can import and test it.

COMMANDS. Every docker command but the base-image pair below is stack.compose_prefix() plus a fixed tail:
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
stack_up, migrate, migrate_status and run_system_tests, stack.refresh_snapshot() copies the named (or
recorded) worktree's stack.SNAPSHOT_SOURCES into <stack dir>/source -- no symlink, no special file,
capped in bytes and files, built beside the old copy, verified, swapped in by rename -- and that copy
is the project directory: the build context and every relative bind source. stack.check_mount_sources,
stack.resolve_test_paths and the migrations guard run on it. stack_down, stack_wipe, logs and ps read
no worktree. The premise, checked by settings: nothing but the MCP writes under the stack directory.
The swap orphans a running container's snapshot binds, so stack_up force-recreates
stack.SNAPSHOT_BOUND_SERVICES on every call: the long-running services run the code of the LAST
stack_up, and run_system_tests rebuilds and recreates only test_client -- after editing anything a
running service loads, tests/fakes included, call stack_up first (tj-zgq5v2).

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

GUARD 2 IS stack.check_env_file_paths(). Every verb that runs docker calls it (through
AgentStack._guarded_env) before its first docker subprocess: ROOT_ENV_FILE, STORE_ENV_FILE and
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
    POST=1                 every one of the above that writes.
NOT needed, keep off: EXEC, AUTH, SECRETS, CONFIGS, PLUGINS, SYSTEM, SWARM, NODES, SERVICES, TASKS,
DISTRIBUTION, COMMIT. The list is read from what compose calls for these verbs and is confirmed at
the host sitting (tj-c4mosr.6); a verb failing with 403 from the proxy names the missing section.
"""
