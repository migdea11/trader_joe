"""Static invariants over the repository's CI and compose configuration.

These assert properties of committed YAML, not of a running system. They need no docker
daemon, no broker and no network, which is exactly why they are worth having: the two
rules below were bought by tj-6g25vo and tj-nbhgtf and are currently defended only by a
comment at the top of a file. A comment does not fail a build.

What these tests deliberately do NOT cover: whether `docker compose up --wait` actually
returns non-zero on a broken service. That requires a daemon and is tracked in tj-5zep48.
A green run here means the configuration still says the right thing, nothing more.
"""

import re
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

import pytest
import yaml

from common.environment import get_env_var


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / '.github' / 'workflows'
COMPOSE_FILE = REPO_ROOT / 'docker-compose.yaml'
OVERRIDE_FILE = REPO_ROOT / 'docker-compose.override.yaml'
ENV_DEFAULT_FILE = REPO_ROOT / '.env.default'
MAKEFILE = REPO_ROOT / 'Makefile'

# tj-8mt207. The harness is a load generator, not a feature: when it is on, data_store and
# data_ingest each create the latency Kafka topics and an RPC consumer at startup, called or
# not. It must be on in dev -- tj-3mk3u5.8 needs the REST vs Kafka vs gRPC comparison before
# the Kafka arm can be deleted -- and off everywhere else.
LATENCY_FLAG = 'LATENCY_TEST_ENABLED'

# Both halves, always. routers/common/latency.py guards the client (initialize_latency_client,
# served by data_store) and the server (initialize_latency_server, answered by data_ingest) on
# the same flag, read once at import. Enabling one alone is the failure worth a test: the stack
# still boots, every healthcheck stays green, and GET /latency hangs until LATENCY_TEST_TIMEOUT.
LATENCY_SERVICES = ('data_store', 'data_ingest')

# Prefixes that identify a secret authenticating to a broker or brokerage account.
# tj-59cce6 states the rule in full; this is its machine-checkable form. Extend this
# tuple when an adapter for a new broker lands -- that is the point of the test.
BROKER_SECRET_PREFIXES = ('ALPACA_', 'IBKR_', 'QUESTRADE_')

# Broker-prefixed names known to be plain settings rather than credentials, so a workflow may
# set them to a literal. Everything else carrying a broker prefix is treated as a credential:
# the list fails closed. The opposite rule -- guessing from a _KEY/_SECRET/_TOKEN suffix -- passes
# the credential nobody thought to name that way, and a false negative is the failure that
# matters here; a false positive costs one line added below in a reviewed diff. Names mirror
# the data_ingest settings of the same spelling.
NON_SECRET_BROKER_SETTINGS = frozenset(
    {
        'ALPACA_ADJUSTMENT',
        'ALPACA_BACKFILL_RESERVE',
        'ALPACA_RATE_BURST',
        'ALPACA_RATE_LIMIT_PER_SEC',
        'ALPACA_SIP_ENABLED',
    }
)

# The right-hand sides that are a reference to a value held elsewhere, not the value itself:
# one `${{ secrets.X }}` or `${{ env.X }}` expression, or a bare shell expansion `$X` / `${X}`.
# `${{ vars.X }}` is deliberately absent -- repository variables are plaintext and unmasked in
# logs, so a credential kept there is as exposed as one typed inline.
_REFERENCE = re.compile(r'\$\{\{\s*(?:secrets|env)\.\w+\s*\}\}|\$\w+|\$\{\w+\}')

# `NAME=value` inside a scalar, e.g. a `run:` script or an `echo ... >> $GITHUB_ENV`. The value
# is a quoted string, an unquoted `${{ ... }}` expression (which may contain spaces), or a run
# of characters that ends at whitespace, a shell operator, a quote or a redirect. `(?!=)` keeps
# a `==` comparison from reading as an assignment.
#
# The unquoted run stops at a quote because the assignment is often itself inside a quoted
# argument: in `echo "ALPACA_API_KEY=$ALPACA_API_KEY" >> .env` the closing `"` belongs to the
# echo, not the value, and swallowing it turned a correct pass-through into a "literal". It
# stops at `<`/`>` for the same reason in `echo ALPACA_API_KEY=${ALPACA_API_KEY}>>.env`.
_SHELL_ASSIGNMENT = re.compile(r'(?<!\w)([A-Za-z_]\w*)=(?!=)("[^"]*"|\'[^\']*\'|\$\{\{.*?\}\}|[^\s;&|)"\'<>]*)')

# Triggers that run a workflow against code on an unreviewed branch.
BRANCH_TRIGGERS = ('push', 'pull_request', 'pull_request_target')

# Distinguishes "the key is absent" from "the key is present and None". `permissions:` with
# no value parses to None, and a check that cannot tell the two apart passes when the key is
# deleted outright -- which is how the deny-by-default test below used to be vacuous.
_ABSENT = object()


def _load_yaml(path: Path) -> dict:
    with path.open(encoding='utf-8') as handle:
        return yaml.safe_load(handle)


def _workflow_files() -> list[Path]:
    if not WORKFLOW_DIR.is_dir():
        return []
    return sorted(p for p in WORKFLOW_DIR.iterdir() if p.suffix in ('.yml', '.yaml'))


def _own_triggers(document: dict) -> set[str]:
    """Return the event names in a parsed workflow's own `on:` block."""
    # `on` is the YAML 1.1 boolean True once parsed, which is why this looks odd.
    triggers = document.get('on', document.get(True, {})) or {}
    if isinstance(triggers, str):
        return {triggers}
    return set(triggers)


def _local_workflow_calls(document: dict) -> set[str]:
    """Return the file names of the workflows this one calls from its own jobs.

    Only a JOB-level `uses:` calls a workflow; a step-level `uses: ./path` runs an action and
    is not an edge. Only the local form `./.github/workflows/<file>` is resolved -- a remote
    `owner/repo/.github/workflows/<file>@ref` names a file this repository cannot scan.
    """
    called = set()
    for job in (document.get('jobs') or {}).values():
        uses = (job or {}).get('uses')
        if not isinstance(uses, str):
            continue
        path = PurePosixPath(uses.removeprefix('./'))
        if uses.startswith('./') and path.parent == PurePosixPath('.github/workflows'):
            called.add(path.name)
    return called


def _branch_reach() -> dict[str, tuple[list[str], list[str]]]:
    """Map each workflow that runs on a branch trigger to (those triggers, one call chain).

    A reusable workflow has no trigger of its own: under `on: workflow_call` it runs with its
    caller's event and, under `secrets: inherit` or an explicit `secrets:` map, with its
    caller's secrets. So judging a file by its own `on:` alone passes a broker credential
    held one call away from a push (tj-uitlk4). The fix is to resolve the call graph first:
    every file reachable from a branch-triggered one through job-level `uses:` edges runs on
    that branch trigger, however many hops away.

    Propagation follows every local edge, not only edges into files that declare
    `workflow_call`. GitHub refuses a call into a file that does not, so the extra
    strictness can only cost a failure in a workflow that would not run anyway. The callee
    is judged whatever `secrets:` its caller passes: a credential in a callee that is not
    handed one today is one caller edit away from being live.

    Files that are not branch-triggered, directly or through a caller, are absent.
    """
    documents = {path.name: _load_yaml(path) for path in _workflow_files()}
    reach: dict[str, tuple[set[str], list[str]]] = {}
    queue = []
    for name, document in documents.items():
        own = _own_triggers(document) & set(BRANCH_TRIGGERS)
        if own:
            reach[name] = (own, [name])
            queue.append(name)

    while queue:
        caller = queue.pop(0)
        triggers, chain = reach[caller]
        for callee in sorted(_local_workflow_calls(documents[caller])):
            if callee not in documents:
                continue  # a call to a missing file fails the caller at GitHub; nothing to scan
            known_triggers, known_chain = reach.get(callee, (set(), []))
            if triggers <= known_triggers:
                continue  # nothing new to propagate -- also what terminates a cycle
            reach[callee] = (known_triggers | triggers, known_chain or [*chain, callee])
            queue.append(callee)

    return {name: (sorted(triggers), chain) for name, (triggers, chain) in reach.items()}


def _walk_scalars(node: object) -> Iterator[str]:
    """Yield every string in a parsed YAML document, keys as well as values."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _walk_scalars(key)
            yield from _walk_scalars(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_scalars(item)


def _secret_references(document: object) -> list[str]:
    """Return every `secrets.NAME` referenced anywhere in a parsed workflow document.

    Every scalar is scanned and no structure is assumed, because a `${{ secrets.X }}`
    expression can appear in a job `env:`, a step `with:`, a `run:` script, a key of a
    reusable-workflow `secrets:` block, or somewhere not yet invented.

    Scanning the parsed tree rather than the raw text is also what makes comment handling
    correct instead of a guess. yaml drops comments for us, and a comment is the one place
    a `secrets.` reference cannot be a real one: GitHub interpolates expressions in the
    parsed workflow and never in a comment, so the rule's own documentation at the top of
    trader_joe_testing.yml does not trip the test. The previous line-oriented
    `line.split('#', 1)[0]` got the converse wrong -- it truncated at a `#` inside a quoted
    string, which is not a comment, so `run: echo "ticket #42 ${{ secrets.ALPACA_API_KEY }}"`
    read as clean. A false negative in this check is the failure that matters.
    """
    found = []
    marker = 'secrets.'
    for scalar in _walk_scalars(document):
        start = 0
        while (index := scalar.find(marker, start)) != -1:
            start = index + len(marker)
            name = ''
            for char in scalar[start:]:
                if char.isalnum() or char == '_':
                    name += char
                else:
                    break
            if name:
                found.append(name)
    return found


def _is_broker_credential(name: str) -> bool:
    upper = name.upper()
    return upper.startswith(BROKER_SECRET_PREFIXES) and upper not in NON_SECRET_BROKER_SETTINGS


def _is_literal(value: str) -> bool:
    """True when an assigned value is the value itself rather than a reference to one."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
        value = value[1:-1].strip()
    return bool(value) and not _REFERENCE.fullmatch(value)


def _walk_mappings(node: object) -> Iterator[tuple[object, object]]:
    """Yield every (key, value) pair of every mapping in a parsed YAML document."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key, value
            yield from _walk_mappings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_mappings(item)


def _literal_broker_credentials(document: object) -> list[str]:
    """Return a description of every broker credential assigned anything but a reference.

    Two assignment forms, because a workflow has two places to put one: a mapping key --
    `env:`, `with:`, a reusable workflow's `secrets:` -- and `NAME=value` inside any scalar,
    which covers `run:` scripts, `echo NAME=... >> $GITHUB_ENV` and heredocs alike. Scanned on
    the parsed tree for the reason _secret_references gives: comments are dropped for us.

    The descriptions name the variable and where it was found, never the value. A failure
    message that echoed the value would print the very credential it had just caught into
    every CI log that ran the suite.
    """
    found = []
    for key, value in _walk_mappings(document):
        if not isinstance(key, str) or not _is_broker_credential(key) or value is None:
            continue
        if isinstance(value, (dict, list)):
            continue  # a mapping keyed by the name, not an assignment to it
        if _is_literal(str(value)):
            found.append(f'{key} (mapping value)')
    for scalar in _walk_scalars(document):
        for match in _SHELL_ASSIGNMENT.finditer(scalar):
            name, value = match.groups()
            if _is_broker_credential(name) and _is_literal(value):
                found.append(f'{name} (inline {name}=...)')
    return found


def _dockerfile_stages() -> dict[str, tuple[str, str]]:
    """Map each named build stage to its (parent, body).

    `parent` is the image or stage the stage's own `FROM` names; `body` is every instruction
    in it, with line continuations folded so a `RUN apt-get update &&` continued onto an
    `apt-get install` line reads as the one command it is. Enough of a parse to answer "what
    is in this stage and where did it come from"; not a Dockerfile parser, and not trying to
    be one.
    """
    text = (REPO_ROOT / 'Dockerfile').read_text(encoding='utf-8')
    folded = text.replace('\\\n', ' ')
    stages: dict[str, tuple[str, list[str]]] = {}
    current = None
    for line in folded.splitlines():
        words = line.strip().split()
        if words[:1] == ['FROM']:
            # FROM <parent> [AS <name>]; an unnamed stage cannot be a build target here.
            parent = words[1] if len(words) > 1 else ''
            name = words[3] if len(words) > 3 and words[2].upper() == 'AS' else ''
            current = name
            if name:
                stages[name] = (parent, [])
        elif current and current in stages:
            stages[current][1].append(line)
    return {name: (parent, '\n'.join(body)) for name, (parent, body) in stages.items()}


def _stage_ancestry(target: str) -> list[str]:
    """Return `target` and every stage it is built `FROM`, nearest first.

    `FROM` ancestry only, deliberately: a `COPY --from=<stage>` brings across the paths it
    names and nothing else, so what a build stage apt-installs into its own root filesystem
    never reaches an image that only copies /code out of it.
    """
    stages = _dockerfile_stages()
    chain = []
    name = target
    while name in stages and name not in chain:
        chain.append(name)
        name = stages[name][0]
    return chain


def _env_file_values(path: Path) -> dict[str, str]:
    """Return the `NAME=value` pairs of an env file, in file order.

    Deliberately not a shell parser. An env file read by `env_file:` is not sourced: compose
    takes the whole of the rest of the line as the value, so there is no quote removal and no
    inline-comment stripping to do here either. A line with no `=` is not an assignment.
    """
    values = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#') or '=' not in stripped:
            continue
        name, _, value = stripped.partition('=')
        values[name.strip()] = value
    return values


def _compose_service_environment(path: Path, service: str) -> dict[str, str | None]:
    """Return a compose service's `environment:` block as a mapping, whichever form it is written in.

    Compose accepts both, and this repository uses both -- postgres is written as a mapping
    (`POSTGRES_DB: ${...}`) and data_store as a list (`- DATABASE_URI=...`) in the same file --
    so a check that understood only one would silently pass the other by finding nothing.

    None is the value of a list entry with no `=`, which is not an assignment at all: it passes
    the variable through from the host environment. Mapping to None rather than '' keeps that
    distinguishable from an explicit assignment to the empty string.
    """
    spec = (_load_yaml(path).get('services') or {}).get(service) or {}
    environment = spec.get('environment')
    if environment is None:
        return {}
    if isinstance(environment, dict):
        return {str(name): None if value is None else str(value) for name, value in environment.items()}
    values: dict[str, str | None] = {}
    for entry in environment:
        name, separator, value = str(entry).partition('=')
        values[name.strip()] = value if separator else None
    return values


def _reads_as_enabled(value: str | None, monkeypatch: pytest.MonkeyPatch) -> bool:
    """Return what the application would make of `value`, using the application's own caster.

    The question is never "is the committed string 'false'" -- it is "does the app read this as
    on". Those differ: `1` is on and `False` is off, and a string comparison gets at least one of
    them wrong. common.environment.get_env_var is the only thing that decides, and
    routers/common/latency.py:17 calls it with exactly these arguments, so this asks it.
    """
    if value is None:
        monkeypatch.delenv(LATENCY_FLAG, raising=False)
    else:
        monkeypatch.setenv(LATENCY_FLAG, value)
    return get_env_var(LATENCY_FLAG, default=False, cast_type=bool)


def _make_variable(name: str) -> str:
    """Return the right-hand side of a `NAME := value` assignment in the Makefile."""
    match = re.search(rf'^{re.escape(name)}\s*:?=\s*(.*)$', MAKEFILE.read_text(encoding='utf-8'), re.MULTILINE)
    assert match is not None, f'{MAKEFILE.name} defines no {name}'
    return match.group(1).strip()


def test_workflow_directory_is_not_empty():
    """Guard the guard: every other test here passes vacuously on an empty directory."""
    assert _workflow_files(), f'no workflow files found under {WORKFLOW_DIR}'


@pytest.mark.parametrize('workflow', _workflow_files(), ids=lambda p: p.name)
def test_branch_triggered_workflow_holds_no_broker_credential(workflow: Path):
    """tj-59cce6: no push/pull_request workflow may reference a broker credential.

    The question this answers: is a broker SECRET handed to a run of unreviewed code? It sees
    `secrets.NAME` expressions only, and is right to -- that is the only way a GitHub secret
    reaches a job. It does not ask whether a credential VALUE is committed in the file; see
    test_workflow_assigns_no_literal_broker_credential for that.

    The blast radius, not the probability, is the thing being controlled: anything in
    such a job sees the secret, including code on the unreviewed branch and every
    third-party action. Today the key is an Alpaca paper key; the same job shape at
    Phase 2 holds one that can place real orders in a registered account.

    "Branch-triggered" includes a reusable workflow called, at any depth, from one that is:
    see _branch_reach. A workflow_call file that no branch-triggered workflow reaches is
    exempt, exactly as a workflow_dispatch-only one is.
    """
    parsed = _load_yaml(workflow)

    branch_triggered, chain = _branch_reach().get(workflow.name, ([], []))
    if not branch_triggered:
        pytest.skip(
            f'{workflow.name} is not branch-triggered: its own triggers are '
            f'{sorted(_own_triggers(parsed))} and no branch-triggered workflow calls it'
        )

    via = '' if len(chain) == 1 else f' through the call chain {" -> ".join(chain)}'
    offenders = [name for name in _secret_references(parsed) if name.upper().startswith(BROKER_SECRET_PREFIXES)]
    assert not offenders, (
        f'{workflow.name} is triggered by {branch_triggered}{via} and references broker '
        f'credentials {sorted(set(offenders))}. This violates tj-59cce6. A credentialed '
        f'run belongs in a separate workflow_dispatch-only workflow behind a GitHub '
        f'Environment with required reviewers.'
    )


@pytest.mark.parametrize('workflow', _workflow_files(), ids=lambda p: p.name)
def test_workflow_assigns_no_literal_broker_credential(workflow: Path):
    """tj-kh6joa: no workflow may assign a broker credential a literal value.

    The question this answers: is a credential VALUE committed to the repository? That is a
    different question from the one test_branch_triggered_workflow_holds_no_broker_credential
    answers, and that test cannot see this failure at all: `ALPACA_API_KEY=<a key typed
    inline>` in a `run:` block contains no `secrets.` expression. It is the classic mistake,
    and the one that never shows up on the repository's secrets settings page.

    Every workflow is checked, whatever its trigger. A literal in a public repository has
    leaked the moment it is committed; whether the workflow ever runs is beside the point.
    What counts as a credential, and what counts as a reference rather than a literal, is
    fixed by NON_SECRET_BROKER_SETTINGS and _REFERENCE above.

    KNOWN LIMITS. This is a rule about what is assigned to a broker-prefixed NAME, so anything
    that hides the name or the value from that shape is outside it. These are not covered, and
    a green run says nothing about them:
    - laundering through an unprefixed variable: `K=<lit>; ALPACA_API_KEY=$K` passes, because
      `$K` is a reference and `K` carries no broker prefix;
    - a default-value expansion: `${ALPACA_API_KEY:=<lit>}` passes, because it contains no
      `NAME=` assignment for the pattern to find;
    - a YAML-style `ALPACA_API_KEY: <lit>` written inside a heredoc passes, because only
      `NAME=value` is matched inside a string, not `NAME: value`;
    - and one false positive: `printf 'ALPACA_API_KEY=%s' "$K"` fails, because the `%s`
      placeholder reads as a literal. Pass the reference inline instead.
    """
    offenders = _literal_broker_credentials(_load_yaml(workflow))
    assert not offenders, (
        f'{workflow.name} assigns broker credentials a value that is not a secrets or env '
        f'reference: {sorted(set(offenders))}. If it is a literal, treat it as leaked and rotate '
        f'it -- it is in the git history now. Pass a credential in by reference: '
        f'`${{{{ secrets.NAME }}}}`, `${{{{ env.NAME }}}}`, or a shell `$NAME` set from one. '
        f'NON_SECRET_BROKER_SETTINGS is only for broker settings that are not secret, such as a '
        f'rate limit; adding a credential to it hides the credential from this check.'
    )


# The regex shapes the test above depends on, pinned directly. The one real workflow holds no
# broker assignment at all, so without these a regression in _SHELL_ASSIGNMENT or _REFERENCE
# would pass the suite in either direction. Each case is a parsed-workflow fragment, and the
# assertion is on the production helper, not a re-implementation of it.
_PASS_THROUGH = {
    # F1 and F6 (tj-kh6joa RE:): the closing quote of the echo argument and the `>>` redirect
    # were swallowed into the value, so a correct pass-through was judged a literal.
    'F1-quoted-echo-to-env-file': {'run': 'echo "ALPACA_API_KEY=$ALPACA_API_KEY" >> .env'},
    'F6-unspaced-redirect': {'run': 'echo ALPACA_API_KEY=${ALPACA_API_KEY}>>.env'},
    'secrets-expression-in-echo': {'run': 'echo "ALPACA_API_KEY=${{ secrets.ALPACA_API_KEY }}" >> $GITHUB_ENV'},
    'env-expression-in-echo': {'run': 'echo "ALPACA_API_SECRET=${{ env.ALPACA_API_SECRET }}" >> .env'},
    'docker-e-flag': {'run': 'docker run -e ALPACA_API_KEY="$ALPACA_API_KEY" image'},
    'env-mapping-secrets-expression': {'env': {'ALPACA_API_KEY': '${{ secrets.ALPACA_API_KEY }}'}},
    'allowlisted-settings-as-literals': {'env': {'ALPACA_RATE_BURST': 5}, 'run': 'ALPACA_SIP_ENABLED=true ./x'},
}
_MUST_FAIL = {
    'literal-in-run': {'run': 'ALPACA_API_KEY=pk-literal ./x'},
    'literal-in-env-mapping': {'env': {'ALPACA_API_KEY': 'pk-literal'}},
    'secrets-expression-with-literal-fallback': {'env': {'ALPACA_API_KEY': "${{ secrets.K || 'pk-literal' }}"}},
    'lowercase-name': {'run': 'alpaca_api_key=pk-literal ./x'},
    'unlisted-questrade-token': {'env': {'QUESTRADE_REFRESH_TOKEN': 'rt-literal'}},
    'allowlist-name-as-prefix-only': {'env': {'ALPACA_SIP_ENABLED_KEY': 'pk-literal'}},
    'literal-inside-quoted-echo': {'run': 'echo "ALPACA_API_KEY=pk-literal" >> .env'},
}


@pytest.mark.parametrize('step', list(_PASS_THROUGH.values()), ids=list(_PASS_THROUGH))
def test_literal_credential_rule_accepts_references(step: dict):
    """A credential handed through by reference is the correct form and must not fail.

    A false positive here is not harmless: the failure message's only in-test way out is the
    allowlist, which would hide a real credential from the check for good.
    """
    assert _literal_broker_credentials({'jobs': {'j': {'steps': [step]}}}) == []


@pytest.mark.parametrize('step', list(_MUST_FAIL.values()), ids=list(_MUST_FAIL))
def test_literal_credential_rule_rejects_literals(step: dict):
    """Each of these commits a credential value, or a fallback to one, and must be caught."""
    assert _literal_broker_credentials({'jobs': {'j': {'steps': [step]}}}) != []


@pytest.mark.parametrize('workflow', _workflow_files(), ids=lambda p: p.name)
def test_workflow_permissions_are_deny_by_default(workflow: Path):
    """tj-6g25vo: the workflow denies at workflow level and every job asks for its own.

    The failure this prevents is a workflow-level grant -- `packages: write`, or the
    `write-all` shorthand -- handing a registry-push token to every job in the file,
    including one that runs a third-party action. A top-level grant is inherited and cannot
    be narrowed by a job, so `permissions: {}` plus per-job declarations is the only shape
    that expresses "this job and no other". There is no scope a job is entitled to that its
    own `permissions:` block cannot state, which is why the empty mapping is required
    rather than merely preferred: any weaker rule is non-monotone, and the version this
    replaced passed `write-all` while failing the strictly narrower `packages: write`.
    """
    parsed = _load_yaml(workflow)
    top_level = parsed.get('permissions', _ABSENT)
    jobs = parsed.get('jobs') or {}

    assert top_level is not _ABSENT, (
        f'{workflow.name} declares no top-level `permissions:`, so every job inherits the '
        f'repository default token scopes. Declare `permissions: {{}}` and let each job ask '
        f'for what it needs.'
    )
    # `write-all` / `read-all` are strings, and the check this replaced tested `'packages'
    # not in <str>` against them -- a substring test, trivially true. Reject the shorthand
    # forms by type before looking inside.
    assert isinstance(top_level, dict), (
        f'{workflow.name} sets top-level `permissions: {top_level!r}`, which grants every '
        f'job in the file a blanket token scope. Use the mapping form `permissions: {{}}`.'
    )
    assert top_level == {}, (
        f'{workflow.name} grants {sorted(top_level)} at workflow level, which hands those '
        f'scopes to every job in it. Declare them on the job that needs them instead.'
    )

    undeclared = [name for name, job in jobs.items() if 'permissions' not in (job or {})]
    assert not undeclared, (
        f'{workflow.name} denies by default at workflow level but these jobs declare '
        f'no permissions of their own and so receive none: {undeclared}'
    )


def test_every_compose_service_declares_a_healthcheck():
    """tj-nbhgtf: `up --wait` can only wait on services that report health.

    A service with no healthcheck is one `--wait` treats as ready the moment it is
    running, which is the false green this was written to remove. Adding a service
    without a healthcheck silently reverts that for the whole stack.
    """
    services = _load_yaml(COMPOSE_FILE)['services']
    missing = sorted(name for name, spec in services.items() if 'healthcheck' not in (spec or {}))
    assert not missing, (
        f'{COMPOSE_FILE.name} services without a healthcheck: {missing}. '
        f'`docker compose up --wait` cannot wait on these.'
    )


def test_every_compose_healthcheck_declares_its_timings():
    """A healthcheck without a start_period fails `--wait` spuriously on a cold start.

    Kafka in KRaft mode takes tens of seconds to format and start; the apps block in
    lifespan on database.initialize() and wait_for_kafka(). A start_period shorter than
    real startup is how `--wait` earns a reputation for flakiness and gets deleted.
    """
    services = _load_yaml(COMPOSE_FILE)['services']
    required = {'test', 'interval', 'timeout', 'retries', 'start_period'}
    for name, spec in services.items():
        healthcheck = (spec or {}).get('healthcheck')
        if healthcheck is None:
            continue  # reported by the test above; do not fail twice for one cause
        missing = sorted(required - set(healthcheck))
        assert not missing, f'service {name!r} healthcheck is missing {missing}'


def test_every_compose_dependency_waits_for_health():
    """tj-nbhgtf: the short `depends_on` list form waits for "started", not "ready".

    Postgres "started" means the container exists, not that it accepts connections.
    data_store survived that only because nothing in its boot path touched the database
    immediately, and that stopped being true when lifespan began calling
    database.initialize().
    """
    services = _load_yaml(COMPOSE_FILE)['services']
    violations = []
    for name, spec in services.items():
        depends_on = (spec or {}).get('depends_on')
        if depends_on is None:
            continue
        if isinstance(depends_on, list):
            violations.append(f'{name}: short list form {depends_on}')
            continue
        for dependency, options in depends_on.items():
            condition = (options or {}).get('condition')
            if condition != 'service_healthy':
                violations.append(f'{name} -> {dependency}: condition={condition!r}')
    assert not violations, (
        'depends_on entries that do not wait on health: '
        + '; '.join(violations)
        + '. Use the long form with `condition: service_healthy`.'
    )


def test_compose_app_probes_use_an_interpreter_the_image_actually_has():
    """The deploy image is debian:bookworm-slim plus the venv -- no curl, no wget.

    A probe is the one command in the file nobody runs by hand, so a probe naming a
    binary the image does not ship fails permanently on a perfectly healthy service and
    looks like a broken app. This pins the probe to the interpreter uvicorn itself runs
    under, and fails if anyone reaches for curl.
    """
    services = _load_yaml(COMPOSE_FILE)['services']
    stages = _dockerfile_stages()

    for name in ('data_store', 'data_ingest'):
        # The stage this service's image is actually built from, and its FROM ancestry --
        # not the whole Dockerfile. An `apt-get install` in base_build_image cannot change
        # what the probe finds, because base_deploy_image is a separate FROM
        # debian:bookworm-slim that copies only /code across. Failing on that would make a
        # legitimate build-stage change red under a test named for compose probes, which is
        # how an invariant gets deleted by the first person it inconveniences.
        target = services[name]['build']['target']
        installing = [stage for stage in _stage_ancestry(target) if 'apt-get install' in stages[stage][1]]
        assert not installing, (
            f'service {name!r} builds target {target!r}, whose stages {installing} now install '
            f'packages; re-check whether the venv-interpreter probe in {COMPOSE_FILE.name} is '
            f'still the right choice before relaxing this test.'
        )

        test_cmd = services[name]['healthcheck']['test']
        joined = ' '.join(test_cmd) if isinstance(test_cmd, list) else str(test_cmd)
        assert 'curl' not in joined and 'wget' not in joined, (
            f'service {name!r} probes with curl/wget, which the deploy image does not contain: {joined}'
        )
        assert '/code/.venv/bin/python' in joined, (
            f'service {name!r} does not probe with the venv interpreter: {joined}'
        )


def test_latency_harness_is_off_in_the_env_default(monkeypatch: pytest.MonkeyPatch):
    """tj-8mt207: .env.default is copied into every environment, so the harness must be off in it.

    This is the file CI copies to .env (trader_joe_testing.yml, "Stage Pipeline Configs") and the
    file a prod deployment is seeded from. Shipping it on is how a load generator ended up running
    in production: nothing fails, no probe goes red, both services just permanently hold a Kafka
    RPC consumer and a set of topics nobody asked for. A regression here is silent, which is the
    whole reason it is worth a test rather than a comment.
    """
    values = _env_file_values(ENV_DEFAULT_FILE)
    assert LATENCY_FLAG in values, (
        f'{ENV_DEFAULT_FILE.name} no longer assigns {LATENCY_FLAG}. Leaving it unset happens to be '
        f'off today, because routers/common/latency.py defaults it False -- but the point of naming '
        f'it here is that the value is a decision on the record. Set it to false explicitly.'
    )
    assert not _reads_as_enabled(values[LATENCY_FLAG], monkeypatch), (
        f'{ENV_DEFAULT_FILE.name} sets {LATENCY_FLAG}={values[LATENCY_FLAG]!r}, which the app reads '
        f'as ON. Every environment is copied from this file, prod and CI included. Turn the harness '
        f'on in {OVERRIDE_FILE.name}, which only the dev stack loads.'
    )


@pytest.mark.parametrize('service', LATENCY_SERVICES)
def test_latency_harness_is_on_for_both_services_in_the_dev_override(service: str, monkeypatch: pytest.MonkeyPatch):
    """tj-8mt207: the dev override turns the harness on, and must do it for the client and the server.

    Parametrized per service on purpose: the failure this exists to catch is someone removing or
    missing ONE of the two entries. data_store is the client -- it serves GET /latency -- and
    data_ingest is the server that answers it over REST and Kafka RPC. Half a pair is worse than
    neither half, because it looks configured: the stack comes up healthy and the endpoint hangs
    until LATENCY_TEST_TIMEOUT with nothing in the logs to say why.

    The harness has to survive in dev because tj-3mk3u5.8 needs the REST vs Kafka vs gRPC
    measurement before any deletion task in that epic can run, and that measurement is only
    possible while all three transports exist.
    """
    environment = _compose_service_environment(OVERRIDE_FILE, service)
    assert LATENCY_FLAG in environment, (
        f'{OVERRIDE_FILE.name} does not set {LATENCY_FLAG} for {service!r}. Both {LATENCY_SERVICES} '
        f'need it: this is the only file that turns the harness on, and enabling one service alone '
        f'leaves a client with no server.'
    )
    assert _reads_as_enabled(environment[LATENCY_FLAG], monkeypatch), (
        f'{OVERRIDE_FILE.name} sets {LATENCY_FLAG}={environment[LATENCY_FLAG]!r} for {service!r}, '
        f'which the app reads as OFF. `make dev-launch` then cannot run the transport comparison '
        f'tj-3mk3u5.8 is blocked on.'
    )


def test_prod_compose_does_not_enable_the_latency_harness(monkeypatch: pytest.MonkeyPatch):
    """tj-8mt207: the harness is turned on in the dev override and nowhere else.

    Flipping .env.default buys nothing if the next person re-enables the harness in the base
    compose file instead, and that is the easy mistake to make -- it is the file everything
    loads, which is exactly why it is the wrong place. Any assignment here reaches prod.
    """
    offenders = []
    for service in _load_yaml(COMPOSE_FILE)['services']:
        value = _compose_service_environment(COMPOSE_FILE, service).get(LATENCY_FLAG, _ABSENT)
        if value is not _ABSENT and _reads_as_enabled(value, monkeypatch):
            offenders.append(f'{service}={value!r}')
    assert not offenders, (
        f'{COMPOSE_FILE.name} enables {LATENCY_FLAG} for {offenders}. This file is loaded by every '
        f'stack including prod. The harness belongs in {OVERRIDE_FILE.name}, which PROD_COMPOSE '
        f'never loads.'
    )


def test_prod_compose_command_does_not_load_the_dev_override():
    """tj-8mt207: the premise every other latency check rests on -- PROD_COMPOSE excludes the override.

    "Turned back on for dev only" is only true while the prod command does not load the file it is
    turned on in. Makefile already states that as a comment ("PROD_COMPOSE must never grow the
    override"); a comment does not fail a build, and adding one more `-f` is a plausible edit that
    would quietly hand prod the dev image, the source bind mounts and the harness at once.
    """
    prod_compose = _make_variable('PROD_COMPOSE')
    assert OVERRIDE_FILE.name not in prod_compose, (
        f'PROD_COMPOSE is `{prod_compose}`, which loads {OVERRIDE_FILE.name}. That override exists '
        f'to hold dev-only settings -- the dev image, source bind mounts, debug logging and the '
        f'latency harness -- and none of them belong in a production stack.'
    )
    # The converse: dev must load it, or the harness cannot be reached at all and the entries
    # asserted above are dead config.
    dev_compose = _make_variable('DEV_COMPOSE')
    assert OVERRIDE_FILE.name in dev_compose, (
        f'DEV_COMPOSE is `{dev_compose}`, which does not load {OVERRIDE_FILE.name}, so nothing '
        f'turns the latency harness on and tj-3mk3u5.8 has no way to run its comparison.'
    )
