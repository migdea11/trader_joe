"""Static invariants over the repository's CI and compose configuration.

These assert properties of committed YAML, not of a running system. They need no docker
daemon, no broker and no network, which is exactly why they are worth having: the two
rules below were bought by tj-6g25vo and tj-nbhgtf and are currently defended only by a
comment at the top of a file. A comment does not fail a build.

What these tests deliberately do NOT cover: whether `docker compose up --wait` actually
returns non-zero on a broken service. That requires a daemon and is tracked in tj-5zep48.
A green run here means the configuration still says the right thing, nothing more.
"""

import configparser
import copy
import fnmatch
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

import pytest
import yaml
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from common.environment import get_env_var


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / '.github' / 'workflows'
COMPOSE_FILE = REPO_ROOT / 'docker-compose.yaml'
OVERRIDE_FILE = REPO_ROOT / 'docker-compose.override.yaml'
ENV_DEFAULT_FILE = REPO_ROOT / '.env.default'
MAKEFILE = REPO_ROOT / 'Makefile'
PYTEST_INI = REPO_ROOT / 'pytest.ini'

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


# tj-95ip1q. pytest.ini is the PR gate's own configuration, and its failure mode is the one this
# project keeps re-learning: a green run that asserted less than it appears to. The marker set is
# asserted by EQUALITY rather than containment so that adding a marker is a deliberate act which
# updates this test in the same diff -- the same friction tj-ru24i2's interface manifest uses.
DECLARED_MARKERS = frozenset({'common', 'data_store', 'data_ingest', 'build_infra', 'external'})
REQUIRED_ADDOPTS_FLAGS = ('--strict-markers', '--strict-config', '--continue-on-collection-errors')
GATE_EXPRESSION = 'not external'


def _pytest_ini() -> configparser.SectionProxy:
    parser = configparser.ConfigParser()
    read = parser.read(PYTEST_INI, encoding='utf-8')
    assert read, f'{PYTEST_INI} is missing or unreadable'
    assert parser.has_section('pytest'), f'{PYTEST_INI.name} declares no [pytest] section'
    return parser['pytest']


def _marker_names() -> set[str]:
    """The marker NAMES declared in pytest.ini, without their descriptions."""
    raw = _pytest_ini().get('markers', '')
    return {line.split(':', 1)[0].strip() for line in raw.splitlines() if line.strip()}


def _addopts_tokens() -> list[str]:
    """Split addopts as pytest itself does -- shlex, so a quoted expression stays one token."""
    return shlex.split(_pytest_ini().get('addopts', ''))


def _marker_expressions() -> list[str]:
    """Every -m expression in addopts, in order. A list, because the COUNT is the assertion."""
    expressions: list[str] = []
    tokens = _addopts_tokens()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == '-m':
            # `-m "not external"`: the expression is the following token, and its absence is a
            # malformed addopts rather than a missing gate -- report it as the empty expression
            # so the value assertion below names it instead of an IndexError hiding it.
            expressions.append(tokens[index + 1] if index + 1 < len(tokens) else '')
            index += 2
            continue
        # `-m"not external"` survives shlex as the single token `-mnot external`, and
        # `--deselect` etc. must not be swept up with it.
        if token.startswith('-m') and not token.startswith('--'):
            expressions.append(token[2:])
        index += 1
    return expressions


def test_pytest_ini_declares_exactly_the_component_markers():
    """tj-95ip1q: set equality, so a new marker cannot appear without a deliberate edit here.

    A DROPPED declaration is loud on its own -- under --strict-markers the first test carrying
    the marker is a collection error -- so that half needs no help. An ADDED one is silent, and
    a marker nobody declared a rule for is how a component selection starts drifting away from
    what the four markers in pytest.ini's prose say they mean.
    """
    assert _marker_names() == set(DECLARED_MARKERS), (
        f'{PYTEST_INI.name} declares markers {sorted(_marker_names())}, expected '
        f'{sorted(DECLARED_MARKERS)}. Adding or renaming a marker is a deliberate act: update '
        f'this test and pytest.ini`s MARKERS prose in the same diff, and say which component '
        f'the new marker names.'
    )


@pytest.mark.parametrize('flag', REQUIRED_ADDOPTS_FLAGS)
def test_pytest_ini_addopts_keeps_the_strict_flags(flag: str):
    """tj-95ip1q: every flag in this tuple is SILENT TO LOSE -- that is what earns it a guard.

    Without --strict-markers, `@pytest.mark.data_stor` is a no-op: the test still passes, and it
    is simply invisible to every component selection for as long as nobody notices. That is the
    misspelled-marker hole the flag exists to close, and nothing else in this repo closes it.

    tj-cx5wzy: --continue-on-collection-errors is here for the same reason and a sharper one.
    Delete it and this suite goes back to CANCELLING EVERY TEST on one collection error -- at
    fc377e4 that cost zero of 231 tests over a data_store manifest guard unrelated to any of
    them -- and the run stays green while it happens, because a suite that never ran reports no
    failures. It also carries a standing condition (pytest.ini: gates key on the EXIT CODE, never
    on the summary line) that is only coherent while the flag is present, so losing it silently
    invalidates a documented rule in another file.
    """
    assert flag in _addopts_tokens(), (
        f'{PYTEST_INI.name} addopts no longer carries {flag}. It reads: `{_pytest_ini().get("addopts", "")}`'
    )


def test_pytest_ini_gate_is_exactly_one_marker_expression():
    """tj-95ip1q: EXACTLY ONE -m in addopts, and its value is exactly "not external".

    This is the load-bearing one. addopts is prepended to the command line and the LAST -m wins,
    so appending a second, narrower expression -- `-m "not data_ingest"` -- drops a whole
    component out of the PR gate. Nothing errors. Nothing skips. The run is green, and the only
    evidence is the "N deselected" count that pytest.ini's own comment says nobody reads. A
    substring check for "not external" passes that very edit, which is why the count is asserted
    and not the presence.

    Widening the single expression in place -- `-m "not external and not data_ingest"` -- is the
    same false green through a different edit, and the exact-value assertion is what catches it.
    Both are tj-06uflo and tj-0qxnzw again: a result that asserted less than it appeared to.
    """
    expressions = _marker_expressions()
    assert len(expressions) == 1, (
        f'{PYTEST_INI.name} addopts carries {len(expressions)} -m expressions {expressions}, '
        f'expected exactly one. addopts is prepended to the command line and the last -m wins, '
        f'so a second expression silently deselects whatever it names -- a green PR gate that '
        f'never ran that component. Put a narrower selection in a make target, not in addopts.'
    )
    assert expressions[0] == GATE_EXPRESSION, (
        f'{PYTEST_INI.name} addopts gates on `-m "{expressions[0]}"`, expected exactly '
        f'`-m "{GATE_EXPRESSION}"`. Every term added to this expression deselects tests from '
        f'the PR gate without failing, skipping or erroring anything.'
    )


# tj-nedzts. Both the Makefile and the workflow named `./router` for months; the directory is
# `routers`. bandit is invoked as `bandit -r $(SOURCE_DIRS)` / `-r $SOURCE_PATHS`, and a root
# that does not exist is not an error to it -- it reports "Files skipped (1)", scans what is
# left and exits 0. So the externally reachable FastAPI handler layer went unscanned by a
# security tool that reported success every time. A Makefile-vs-CI PARITY test would not have
# caught it: both lists carried the same typo and agreed with each other perfectly.
#
# Existence is therefore the check, not agreement. The roots are relative to the repository
# root because that is where both invocations run.
SCANNER_ROOT_SOURCES = ('Makefile SOURCE_DIRS', 'workflow SOURCE_PATHS')


def _workflow_source_paths() -> dict[str, str]:
    """Map each workflow declaring a top-level `env.SOURCE_PATHS` to that value."""
    declared = {}
    for path in _workflow_files():
        value = ((_load_yaml(path) or {}).get('env') or {}).get('SOURCE_PATHS')
        if value is not None:
            declared[path.name] = str(value)
    return declared


def _scanner_roots(source: str) -> list[tuple[str, str]]:
    """Return (origin, root) for every scanner root named by `source`.

    Both halves fail closed. If the variable is renamed or deleted the helper raises rather
    than returning nothing, because a scanner-root test that silently checks an empty list is
    the same vacuous green this bead exists to close.
    """
    if source == 'Makefile SOURCE_DIRS':
        return [(MAKEFILE.name, root) for root in _make_variable('SOURCE_DIRS').split()]
    declared = _workflow_source_paths()
    assert declared, (
        'no workflow under .github/workflows declares a top-level env.SOURCE_PATHS. If the '
        'variable was renamed, rename it here too; if the bandit step was removed, remove this '
        'test with it rather than leaving it passing over an empty list.'
    )
    return [(name, root) for name, value in declared.items() for root in value.split()]


@pytest.mark.parametrize('source', SCANNER_ROOT_SOURCES)
def test_every_scanner_root_exists_as_a_directory(source: str):
    """tj-nedzts: every root handed to bandit must be a directory that is actually there.

    This is the check that catches the bug class. bandit exits 0 over a path that does not
    exist, so the typo bought nothing but a smaller scan and a green check mark -- the
    tj-06uflo shape, a tool reporting success having examined nothing. The spelling fix itself
    landed in ba498ad (./router -> ./routers in both files, confirmed by the scanned-LOC count
    going 3354 -> 3621 with "Files skipped" dropping from 1 to 0); this is the guard that keeps
    it fixed, and that catches the next root added with a typo or removed without being
    dropped from the list.
    """
    roots = _scanner_roots(source)
    assert roots, f'{source} is empty, so every assertion below passes over nothing'
    missing = [(origin, root) for origin, root in roots if not (REPO_ROOT / root).is_dir()]
    assert not missing, (
        f'{source} names {len(missing)} root(s) that are not directories in the repository: '
        f'{[f"{origin}: {root}" for origin, root in missing]}. bandit does not fail on a path '
        f'that does not exist -- it skips it, scans the rest and exits 0 -- so a misspelled '
        f'root silently removes that whole layer from the security scan.'
    )


# ---------------------------------------------------------------------------------------
# THE UV PIN AND THE INSTALL COOLDOWN (tj-jon3d1, tj-vhboky.17)
#
# `exclude-newer = "7 days"` is a SILENTLY INERT control below uv 0.9.17. That uv does not
# reject the relative form: it prints a parse warning during settings discovery and then
# resolves AS IF THE KEY WERE ABSENT, exit 0. So the key and the version are one control
# spread over two files, and the version is the half that can be lowered with nothing
# anywhere going red.
#
# The pin lives in FOUR places -- Makefile UV_VERSION, the project Dockerfile, the CI
# workflow and .devcontainer/Dockerfile -- and a bump that misses one leaves CI, the image
# and local resolving under different uv versions. Every one of those files carries a
# comment asserting the four agree; nothing until now checked it. A comment does not fail
# a build, which is the reason given at the top of this module for all of its other rules.
PROJECT_DOCKERFILE = REPO_ROOT / 'Dockerfile'
DEVCONTAINER_DOCKERFILE = REPO_ROOT / '.devcontainer' / 'Dockerfile'
PYPROJECT = REPO_ROOT / 'pyproject.toml'
TESTING_WORKFLOW = WORKFLOW_DIR / 'trader_joe_testing.yml'

# Below this, `exclude-newer = "7 days"` parses as nothing and the cooldown is off.
UV_COOLDOWN_FLOOR = (0, 9, 17)

# Each pin is read with the pattern its own file actually uses, not one loose pattern over all
# four. A pin that moves to a different spelling then reads as MISSING here and fails loudly,
# instead of matching some other version-shaped string on a nearby line and passing while the
# real pin drifts.
UV_PIN_SOURCES = {
    'Makefile': (MAKEFILE, r'^UV_VERSION\s*:?=\s*([0-9]+\.[0-9]+\.[0-9]+)'),
    'Dockerfile': (PROJECT_DOCKERFILE, r'^COPY --from=ghcr\.io/astral-sh/uv:([0-9]+\.[0-9]+\.[0-9]+)'),
    '.github/workflows/trader_joe_testing.yml': (TESTING_WORKFLOW, r'^\s*UV_VERSION:\s*"?([0-9]+\.[0-9]+\.[0-9]+)"?'),
    '.devcontainer/Dockerfile': (DEVCONTAINER_DOCKERFILE, r'astral-sh/uv/releases/download/([0-9]+\.[0-9]+\.[0-9]+)/'),
}


def _uv_pins() -> dict[str, str]:
    """Every uv version pin in the repository, keyed by the file that carries it.

    A pin that could not be found comes back as the empty string rather than raising, so the
    assertion below names the file instead of an AttributeError hiding which one moved.
    """
    return {
        name: (match.group(1) if (match := re.search(pattern, path.read_text(), re.MULTILINE)) else '')
        for name, (path, pattern) in UV_PIN_SOURCES.items()
    }


def test_every_uv_pin_in_the_repository_agrees():
    """One control, four files. A bump that misses one is invisible until something diverges.

    The failure this prevents is not a broken build -- each file stays individually valid -- it
    is CI resolving under a different uv than local, which is how a lock file starts being
    rewritten by whichever machine happened to run last.
    """
    pins = _uv_pins()
    assert len(set(pins.values())) == 1, (
        f'the uv pins disagree: {pins}. An empty value means the pin was not found at all, which '
        f'is a spelling change in that file rather than a missing pin -- fix that entry in '
        f'UV_PIN_SOURCES in the same diff. All four move together.'
    )


def test_the_uv_pin_is_new_enough_for_the_cooldown_to_be_active():
    """The half that fails OPEN, which is what earns it a test the other pins would not.

    Lowering the pin below 0.9.17 does not disable the cooldown, it makes it silently inert: uv
    warns during settings discovery, resolves as if `exclude-newer` were absent, and exits 0.
    Nothing downstream goes red. Resolving a package uploaded minutes ago is exactly what the
    key exists to prevent, and there would be no signal that it had stopped preventing it.
    """
    pinned = _uv_pins()['Makefile']
    assert pinned, f'no UV_VERSION found in {MAKEFILE.name}'
    floor = '.'.join(str(part) for part in UV_COOLDOWN_FLOOR)
    assert tuple(int(part) for part in pinned.split('.')) >= UV_COOLDOWN_FLOOR, (
        f'uv is pinned at {pinned}, below the {floor} floor at which `exclude-newer` became a '
        f'parsed setting. Below the floor the cooldown in pyproject.toml is not rejected -- it '
        f'is ignored, and every build resolves without it.'
    )


def test_the_install_cooldown_is_declared():
    """The other half of the same control, read from the parsed TOML rather than from the text.

    pyproject.toml's own comment explains that the key's PLACEMENT is load-bearing for semgrep,
    which keys off the literal table header. Parsing means this test keeps holding if that
    placement has to move again for the scanner's sake.
    """
    with PYPROJECT.open('rb') as handle:
        config = tomllib.load(handle)
    assert config.get('tool', {}).get('uv', {}).get('exclude-newer'), (
        'pyproject.toml declares no [tool.uv] exclude-newer. That is the dependency cooldown '
        'from tj-jon3d1: without it a release uploaded minutes ago -- compromised, or yanked '
        'an hour later -- reaches a build here with no window for anyone to catch it.'
    )


# tj-jon3d1's design value, matched by dependabot's cooldown default-days. A floor, not an exact
# value, so a deliberate lengthening is not flagged.
UV_COOLDOWN_MINIMUM = timedelta(days=7)

# The two relative spellings uv accepts: a friendly span ("7 days", "1 week", "2 weeks 3 days",
# "7d") and an ISO 8601 duration ("P7D", "P1W", "PT168H"). Deliberately NOT accepted, so they fail
# closed: a sign or "ago" (uv takes those as a negative span), calendar units (uv 0.12.19 rejects
# months and years with only a warning, which leaves the key inert), and absolute dates.
_FRIENDLY_UNIT_SECONDS = {
    **dict.fromkeys(('w', 'wk', 'wks', 'week', 'weeks'), 604800),
    **dict.fromkeys(('d', 'day', 'days'), 86400),
    **dict.fromkeys(('h', 'hr', 'hrs', 'hour', 'hours'), 3600),
    **dict.fromkeys(('m', 'min', 'mins', 'minute', 'minutes'), 60),
    **dict.fromkeys(('s', 'sec', 'secs', 'second', 'seconds'), 1),
}
_FRIENDLY_TERM = re.compile(r'\s*(\d+)\s*([a-z]+)\s*,?')
_ISO_DURATION = re.compile(r'P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?')


def _relative_cooldown(value: object) -> timedelta | None:
    """The cooldown as a duration, or None for any form that is not a positive relative span."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if (iso := _ISO_DURATION.fullmatch(text)) and text not in ('P', 'PT') and not text.endswith('T'):
        weeks, days, hours, minutes, seconds = (int(part or 0) for part in iso.groups())
        return timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds)
    total, position, lowered = 0, 0, text.lower()
    while position < len(lowered):
        term = _FRIENDLY_TERM.match(lowered, position)
        if not term or term.group(2) not in _FRIENDLY_UNIT_SECONDS:
            return None
        total += int(term.group(1)) * _FRIENDLY_UNIT_SECONDS[term.group(2)]
        position = term.end()
    return timedelta(seconds=total)


def test_the_install_cooldown_is_a_relative_span_of_at_least_seven_days():
    """Presence is not the control; a relative window of at least a week is (tj-bdmt24).

    "1 second" satisfies the presence check above and gives no window at all. An absolute
    timestamp satisfies it too, but that is a frozen resolution date: it stops aging, so it is a
    pin rather than a cooldown. semgrep keys on the key existing, so neither change trips it.
    Anything this parser does not recognise fails, rather than passing on a form nobody checked.
    """
    with PYPROJECT.open('rb') as handle:
        value = tomllib.load(handle).get('tool', {}).get('uv', {}).get('exclude-newer')
    cooldown = _relative_cooldown(value)
    assert cooldown is not None and cooldown >= UV_COOLDOWN_MINIMUM, (
        f'pyproject.toml [tool.uv] exclude-newer is {value!r}, which is '
        f'{"not a relative duration this test recognises" if cooldown is None else f"only {cooldown}"}. '
        f'The cooldown from tj-jon3d1 must be a relative span of at least {UV_COOLDOWN_MINIMUM.days} '
        f'days, such as "7 days" or "P7D". An absolute date freezes resolution instead of delaying '
        f'it, and a shorter span leaves too little time for a bad upload to be caught.'
    )


def test_sqlalchemy_is_declared_with_the_asyncio_extra():
    """tj-9848p1: the async store runs on greenlet, and nothing else declares it.

    Plain `sqlalchemy` pulls greenlet in only behind a platform_machine marker, so it lands in
    the lock incidentally and any re-resolution is free to drop it. Measured, not feared: a
    forced re-lock refreshed 61 packages, removed greenlet and took the suite to 1 failed,
    1 error, 250 passed. The extra is what names the requirement, and dropping it back to plain
    `sqlalchemy` would pass every other check in this repo.
    """
    with PYPROJECT.open('rb') as handle:
        config = tomllib.load(handle)
    declarations = config.get('dependency-groups', {}).get('data-store', [])
    assert any(declaration.startswith('sqlalchemy[asyncio]') for declaration in declarations), (
        f'the data-store group declares {declarations}, with no sqlalchemy[asyncio]. The async '
        f'layer imports sqlalchemy.ext.asyncio, which needs greenlet; without the extra nothing '
        f'in this repo requires greenlet and a re-lock may silently drop it.'
    )


# ---------------------------------------------------------------------------------------
# THE PERMANENT RUFF RULE SETS (tj-vhboky.30 item 3, tj-vhboky.34)
#
# DTZ and ASYNC went into the permanent select because both had no production findings, so
# they cost nothing to add and stop regressions. That guarantee has two ways to go quietly
# wrong, and `make lint` stays green through both. One: the family drops out of select.
# Two: an ignore grows, from one code on one path into a glob or a whole family, and hides
# the next production finding while the select line still looks right. So the families are
# pinned in select, and every suppression of a DTZ or ASYNC code is pinned by EQUALITY.
# Adding one then means editing this test in the same diff.
PERMANENT_RULE_FAMILIES = ('DTZ', 'ASYNC')

# The decision keeps these out on purpose. PERF's three PERF203 findings are intended
# try-in-retry-loop shapes. RET and PIE are style. PTH was never scanned and no finding
# motivates it. Adding one is a new decision, not a config tidy-up.
EXCLUDED_RULE_FAMILIES = ('PERF', 'RET', 'PIE', 'PTH')

# (path glob, code) for every DTZ/ASYNC suppression that may exist, and nothing else:
# - DTZ001 under tests only: the tests build naive datetimes on purpose, to prove they are
#   refused.
# - ASYNC109 on postgres_tools.py only: its `timeout` is a retry deadline, not an operation
#   timeout. The user ruled on 2026-09-28 that it stays as it is (tj-mvqbaf).
ALLOWED_PERMANENT_FAMILY_SUPPRESSIONS = frozenset(
    {('**/tests/**', 'DTZ001'), ('common/database/postgres_tools.py', 'ASYNC109')}
)


def _ruff_lint_config() -> dict:
    with PYPROJECT.open('rb') as handle:
        config = tomllib.load(handle)
    lint = config.get('tool', {}).get('ruff', {}).get('lint')
    assert isinstance(lint, dict), 'pyproject.toml has no [tool.ruff.lint] table'
    return lint


def _selected_rules(lint: dict) -> list[str]:
    return [*lint.get('select', []), *lint.get('extend-select', [])]


def _in_permanent_family(code: str) -> bool:
    """True when a rule selector names, or is contained in, a DTZ or ASYNC rule.

    Matching on the prefix means a family-wide `"ASYNC"`, a group like `"ASYNC1"` and a
    single `"ASYNC109"` all count. Each of them suppresses the same finding.
    """
    return code.startswith(PERMANENT_RULE_FAMILIES)


def _permanent_family_suppressions(lint: dict) -> set[tuple[str, str]]:
    """Every (scope, code) that switches off a DTZ or ASYNC rule anywhere in the lint config.

    A global ignore is reported with the scope '*' because it hides the finding in every file,
    production included. That is the widest widening there is, so it has to be seen here.
    """
    found = set()
    for key in ('ignore', 'extend-ignore'):
        found |= {('*', code) for code in lint.get(key, []) if _in_permanent_family(code)}
    for key in ('per-file-ignores', 'extend-per-file-ignores'):
        for glob, codes in lint.get(key, {}).items():
            found |= {(glob, code) for code in codes if _in_permanent_family(code)}
    return found


@pytest.mark.build_infra
@pytest.mark.parametrize('family', PERMANENT_RULE_FAMILIES)
def test_ruff_selects_the_permanent_rule_family(family: str):
    """tj-vhboky.30 item 3: DTZ and ASYNC are in the permanent select, as whole families."""
    selected = _selected_rules(_ruff_lint_config())
    assert family in selected, (
        f'[tool.ruff.lint] select no longer carries {family!r}: {selected}. tj-vhboky.30 made it '
        f'permanent so that a regression fails `make lint`. Without it that regression lands silently.'
    )


@pytest.mark.build_infra
@pytest.mark.parametrize('family', EXCLUDED_RULE_FAMILIES)
def test_ruff_does_not_select_an_excluded_rule_family(family: str):
    """tj-vhboky.30 item 3 kept PERF, RET, PIE and PTH out. Adding one needs its own ruling."""
    offenders = [code for code in _selected_rules(_ruff_lint_config()) if code.startswith(family)]
    assert not offenders, (
        f'[tool.ruff.lint] selects {offenders}, which tj-vhboky.30 item 3 rejected. If that has '
        f'changed, record the ruling and update EXCLUDED_RULE_FAMILIES in the same diff.'
    )


@pytest.mark.build_infra
def test_permanent_rule_families_are_suppressed_only_where_ruled():
    """Every DTZ/ASYNC ignore is one code on one path, and exactly the ones on record.

    Equality, not containment. A containment check passes the edits this exists to catch:
    `"common/**" = ["ASYNC109"]`, `"common/database/postgres_tools.py" = ["ASYNC"]`, or
    `"ASYNC109"` added to the global ignore. Each of those passes `make lint`, and each hides
    the next production finding.
    """
    found = _permanent_family_suppressions(_ruff_lint_config())
    assert found == set(ALLOWED_PERMANENT_FAMILY_SUPPRESSIONS), (
        f'DTZ/ASYNC suppressions are {sorted(found)}, expected exactly '
        f'{sorted(ALLOWED_PERMANENT_FAMILY_SUPPRESSIONS)}. A scope of "*" is a global ignore. '
        f'Widening an ignore hides production findings from a rule set that tj-vhboky.30 made '
        f'permanent. Fix the finding, or record a ruling and update this set in the same diff.'
    )


# ---------------------------------------------------------------------------------------
# MIGRATE-STATUS IS READ-ONLY (tj-08dlh8, tj-4yvsb2)
#
# The user approved `make migrate-status` on one condition: "I think migration status is fine,
# assuming it's readonly." Until this test the condition was a comment above the recipe. So
# the PROPERTY is asserted here, not the text: every alembic subcommand the recipe reaches is
# in the read-only set. A test that compared the recipe to an expected string would pass an
# edit that changed both, which is the parity trap the scanner-root guard above records.
#
# The recipe reaches alembic through data/store/run_migrations.sh, which forwards its
# arguments to alembic and falls back to `upgrade head` when given none. So a bare call to
# the script is an UPGRADE, and it is read as one here. The fallback is read from the script
# rather than restated, so the two cannot drift apart.
MIGRATIONS_SCRIPT = REPO_ROOT / 'data' / 'store' / 'run_migrations.sh'

# An allow list, so that it fails closed: a subcommand nobody has classified is refused.
# `current` reads alembic_version; `history`, `heads`, `branches` and `show` read the revision
# files. stamp, upgrade, downgrade, merge, revision, edit and ensure_version all write to the
# database or to the revision tree, and none of them is here.
READ_ONLY_ALEMBIC_COMMANDS = frozenset({'current', 'history', 'heads', 'branches', 'show'})

# alembic's global options that take a value. Skipping their values finds the subcommand in
# `alembic -c alembic.ini stamp head`, which would otherwise read `alembic.ini` as the command.
_ALEMBIC_VALUE_OPTIONS = frozenset({'-c', '--config', '-n', '--name', '-x'})

# Shell control operators. One recipe line may chain several commands, and each is judged.
_SHELL_OPERATORS = frozenset({'&&', '||', ';', '|', '&', '(', ')'})


def _make_recipe(target: str) -> list[str]:
    """Return the recipe lines of a Makefile target, continuations folded, prefixes stripped.

    Fails closed: a target that is missing or has an empty recipe raises, because a read-only
    check over an empty recipe passes having looked at nothing.
    """
    text = MAKEFILE.read_text(encoding='utf-8').replace('\\\n', ' ')
    lines = text.splitlines()
    header = re.compile(rf'^{re.escape(target)}\s*:(?!=)')
    start = next((index for index, line in enumerate(lines) if header.match(line)), None)
    assert start is not None, f'{MAKEFILE.name} defines no {target!r} target'
    recipe = []
    for line in lines[start + 1 :]:
        if not line.startswith('\t'):
            break
        command = line.strip().lstrip('@-+').strip()
        if command:
            recipe.append(command)
    assert recipe, f'the {target!r} target in {MAKEFILE.name} has an empty recipe'
    return recipe


def _script_default_arguments() -> list[str]:
    """The alembic arguments run_migrations.sh substitutes when it is called with none."""
    text = MIGRATIONS_SCRIPT.read_text(encoding='utf-8')
    match = re.search(r'if \[ \$\{#ALEMBIC_ARGS\[@\]\} -eq 0 \]; then\s*\n\s*ALEMBIC_ARGS=\(([^)]*)\)', text)
    assert match is not None, (
        f'{MIGRATIONS_SCRIPT.name} no longer has a recognisable no-argument fallback. If its '
        f'default changed shape, update _script_default_arguments in the same diff.'
    )
    return shlex.split(match.group(1))


def _commands(recipe_line: str) -> list[list[str]]:
    """Split one recipe line into its simple commands, at every shell control operator."""
    lexer = shlex.shlex(recipe_line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    commands: list[list[str]] = [[]]
    for token in lexer:
        if token in _SHELL_OPERATORS:
            commands.append([])
        else:
            commands[-1].append(token)
    return [command for command in commands if command]


def _alembic_subcommand(command: list[str]) -> str | None:
    """Return the alembic subcommand a simple command runs, or None if it does not run alembic.

    Two routes reach alembic: run_migrations.sh, whose arguments are alembic's and whose empty
    argument list means the script's default, and alembic itself. A command that runs the
    script or alembic with no subcommand to find returns the empty string, not None, so the
    caller refuses it rather than mistaking it for something unrelated.
    """
    for index, word in enumerate(command):
        name = PurePosixPath(word).name
        if name == MIGRATIONS_SCRIPT.name:
            arguments = command[index + 1 :] or _script_default_arguments()
        elif name == 'alembic':
            arguments = command[index + 1 :]
        else:
            continue
        position = 0
        while position < len(arguments):
            argument = arguments[position]
            if argument in _ALEMBIC_VALUE_OPTIONS:
                position += 2
            elif argument.startswith('-'):
                position += 1
            else:
                return argument
        return ''
    return None


@pytest.mark.build_infra
def test_migrate_status_runs_only_read_only_alembic_commands():
    """tj-08dlh8: the approval of `make migrate-status` was conditional on it being read-only.

    Every simple command in the recipe must run alembic, directly or through
    run_migrations.sh, with a subcommand in READ_ONLY_ALEMBIC_COMMANDS. A command that does
    not run alembic at all is refused too: `$(SOMETHING)` or a second script could reach a
    mutating command this test cannot see, and the target's whole job is to call alembic. If a
    new line is legitimate, the change to this test is where that gets decided.
    """
    offenders = []
    for line in _make_recipe('migrate-status'):
        for command in _commands(line):
            subcommand = _alembic_subcommand(command)
            if subcommand is None:
                offenders.append(f'`{" ".join(command)}` does not run alembic')
            elif subcommand not in READ_ONLY_ALEMBIC_COMMANDS:
                offenders.append(f'`{" ".join(command)}` runs `alembic {subcommand or "<none>"}`')
    assert not offenders, (
        f'the migrate-status recipe in {MAKEFILE.name} is no longer read-only: {offenders}. The '
        f'user approved it only on that condition (tj-4yvsb2). Read-only alembic commands are '
        f'{sorted(READ_ONLY_ALEMBIC_COMMANDS)}; a bare run_migrations.sh call is '
        f'`alembic {" ".join(_script_default_arguments())}`. A mutating step belongs behind its '
        f'own named target, the way `migrate` is.'
    )


@pytest.mark.build_infra
def test_migrate_still_defaults_to_upgrade_head():
    """tj-4yvsb2: adding the argument to run_migrations.sh must leave `make migrate` unchanged.

    `make migrate` is the single spelling of "apply the migrations", and it applies them by
    calling the script bare. So both halves are pinned: the recipe passes nothing, and the
    script's fallback for nothing is `upgrade head`. Either half changing on its own would
    turn the deploy step into something other than an upgrade while it still exits 0.
    """
    assert _script_default_arguments() == ['upgrade', 'head'], (
        f'{MIGRATIONS_SCRIPT.name} now falls back to `alembic {" ".join(_script_default_arguments())}` '
        f'when called with no arguments. `make migrate` and the deploy step call it that way and '
        f'expect `upgrade head`.'
    )
    recipe = _make_recipe('migrate')
    invocations = [
        command
        for line in recipe
        for command in _commands(line)
        if PurePosixPath(command[0]).name == MIGRATIONS_SCRIPT.name
    ]
    assert len(invocations) == 1 and invocations[0][1:] == [], (
        f'the migrate recipe in {MAKEFILE.name} should call {MIGRATIONS_SCRIPT.name} exactly '
        f'once with no arguments, so the script default applies. It reads: {recipe}'
    )


# ---------------------------------------------------------------------------------------
# RUFF STILL LINTS THE SOURCE TREES (tj-2ngid0)
#
# tj-2ngid0 widened [tool.ruff] exclude to every non-source top-level directory. A missing
# exclude fails loudly: the file count jumps, or a walk into unreadable volume state errors.
# The other direction is silent. An exclude that swallows source, whether a whole root or one
# service's app directory, makes `make lint` report fewer files and stay green.
#
# So ruff itself is asked which files it would lint, rather than its exclude globs being
# re-matched here. That covers exclude, extend-exclude, force-exclude and .gitignore alike,
# with ruff's own glob semantics. The tracked Python under each SOURCE_DIRS root has to be in
# ruff's list, except under the prefixes below.
#
# Alembic writes the revision files, and they were excluded before tj-2ngid0 widened the list.
# Adding a prefix here takes a source path out of the lint gate, so it is a decision.
RUFF_UNLINTED_SOURCE_PREFIXES = ('data/store/migrations/versions/',)


def _run(*command: str) -> list[str]:
    """Run a command at the repository root and return its non-empty output lines."""
    result = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _tracked_source_python() -> set[str]:
    """Every git-tracked .py file under a SOURCE_DIRS root, relative to the repository root."""
    roots = [PurePosixPath(root).as_posix() for root in _make_variable('SOURCE_DIRS').split()]
    assert roots, f'{MAKEFILE.name} SOURCE_DIRS is empty'
    tracked = set(_run('git', 'ls-files', '--', *(f'{root}/*.py' for root in roots)))
    assert tracked, f'git tracks no .py files under {roots}, so the check below would pass over nothing'
    return tracked


def _ruff_linted_files() -> set[str]:
    """The files `ruff check .` would lint from the repository root, as ruff itself resolves them."""
    listed = _run(sys.executable, '-m', 'ruff', 'check', '--no-cache', '--show-files', '.')
    return {Path(path).resolve().relative_to(REPO_ROOT).as_posix() for path in listed}


@pytest.mark.build_infra
def test_ruff_exclude_swallows_no_source_file():
    """tj-2ngid0: every tracked source file is still linted, apart from the recorded prefixes.

    This is the silent direction of an exclude list. `"routers"`, `"data/store/app"` or a
    `"*"` added to [tool.ruff] exclude passes `make lint` with a smaller file count, and
    nothing else notices.
    """
    unlinted = _tracked_source_python() - _ruff_linted_files()
    swallowed = sorted(path for path in unlinted if not path.startswith(RUFF_UNLINTED_SOURCE_PREFIXES))
    assert not swallowed, (
        f'ruff no longer lints {len(swallowed)} tracked source file(s): {swallowed[:10]}'
        f'{" ..." if len(swallowed) > 10 else ""}. Something in the ruff configuration (exclude, '
        f'extend-exclude) or a .gitignore now covers source under SOURCE_DIRS. Exclude only '
        f'tooling, state and docs; a new source exclusion goes in RUFF_UNLINTED_SOURCE_PREFIXES.'
    )


# ---------------------------------------------------------------------------------------
# THE LOCK IS FROZEN BY DEFAULT (tj-3zh7ss)
#
# A plain `uv run` or `uv sync` re-resolves uv.lock whenever pyproject.toml has moved, and one
# plain `make test` once moved ten packages that way, sqlalchemy to a pre-release among them.
# The fix is a setting rather than a habit: UV_FROZEN=1, exported by the Makefile and set in the
# workflow env, so every uv that either one starts installs the committed lock as-is. `make lock` is the
# one deliberate re-lock, and it removes UV_FROZEN for that single command because `uv lock`
# reads the variable as --check-exists.
#
# All of it fails silently. Drop the export and `make test` goes back to re-locking with exit 0.
# Drop the workflow env and CI is safe only while `uv sync --locked` happens to run first. Drop
# the `env -u` and `make lock` checks instead of locking, which is at least loud. Drop the shared
# pin check from `lock` and an old uv re-locks with nothing to stop it: that is tj-jon3d1 again,
# through the one door built for re-locking. Each half gets its own pin, so a red names the half.
#
# The spellings uv reads as true for a boolean variable. Anything else, "0" and "" included,
# leaves the lock unfrozen.
UV_TRUTHY = frozenset({'1', 'true', 'yes', 'on'})
_MAKE_EXPORTED_ASSIGNMENT = re.compile(r'^export\s+UV_FROZEN\s*[:?]?=\s*(\S*)\s*$', re.MULTILINE)
_MAKE_PLAIN_ASSIGNMENT = re.compile(r'^UV_FROZEN\s*[:?]?=\s*(\S*)\s*$', re.MULTILINE)
_MAKE_BARE_EXPORT = re.compile(r'^export\s+(?:[\w.-]+\s+)*UV_FROZEN(?:\s+[\w.-]+)*\s*$', re.MULTILINE)
_MAKE_UNEXPORT = re.compile(r'^unexport\b.*\bUV_FROZEN\b', re.MULTILINE)
_UV_PIN_CHECK_DEFINE = re.compile(r'^define UV_PIN_CHECK\s*$(.*?)^endef\s*$', re.MULTILINE | re.DOTALL)
UV_PIN_CHECK_REFERENCE = '$(UV_PIN_CHECK)'

# Every recipe that runs uv where the lock could be written, or that installs uv, runs the pin
# check before its first uv command. `lock` is the one that re-resolves. The venv recipe is the
# one that bootstraps uv on a bare checkout, and every other target goes through it.
UV_PIN_CHECKED_TARGETS = ('lock', '$(VENV_MARKER)')


def _makefile_uv_frozen_export() -> str | None:
    """The value the Makefile exports as UV_FROZEN to every recipe, or None if it exports none.

    Two spellings export it, `export UV_FROZEN := 1` and a plain assignment plus a separate
    `export UV_FROZEN`. An `unexport UV_FROZEN` anywhere cancels either one.
    """
    text = MAKEFILE.read_text(encoding='utf-8')
    if _MAKE_UNEXPORT.search(text):
        return None
    if exported := _MAKE_EXPORTED_ASSIGNMENT.search(text):
        return exported.group(1)
    plain = _MAKE_PLAIN_ASSIGNMENT.search(text)
    if plain and _MAKE_BARE_EXPORT.search(text):
        return plain.group(1)
    return None


def _uv_subcommand_index(command: list[str]) -> int | None:
    """The index of the word naming uv in a simple command, or None if the command runs no uv."""
    return next((index for index, word in enumerate(command) if PurePosixPath(word).name == 'uv'), None)


def _runs_uv_lock(command: list[str]) -> bool:
    """True when a simple command runs `uv lock`, whatever wrapper or options come before it."""
    index = _uv_subcommand_index(command)
    if index is None:
        return False
    arguments = [word for word in command[index + 1 :] if not word.startswith('-')]
    return arguments[:1] == ['lock']


def _unsets_uv_frozen(command: list[str]) -> bool:
    """True when a simple command removes UV_FROZEN from its own environment before running uv.

    `env -u UV_FROZEN`, `env --unset UV_FROZEN` and `env --unset=UV_FROZEN` all count. Setting it
    to an empty or false value does not: uv's reading of those is a question this test would then
    have to answer, and removing the variable is the form that leaves nothing to interpret.
    """
    before_uv = command[: _uv_subcommand_index(command)]
    if 'env' not in (PurePosixPath(word).name for word in before_uv):
        return False
    return any(flag in ('-u', '--unset') and name == 'UV_FROZEN' for flag, name in pairwise(before_uv)) or any(
        word in ('--unset=UV_FROZEN', '-uUV_FROZEN') for word in before_uv
    )


@pytest.mark.build_infra
def test_the_makefile_exports_a_frozen_lock_to_every_recipe():
    """tj-3zh7ss: every uv a make target starts installs the committed lock and never re-resolves.

    Exported, not just assigned. A make variable that is not exported never reaches the uv a
    recipe runs, so `UV_FROZEN := 1` without `export` reads as configured and does nothing.
    """
    value = _makefile_uv_frozen_export()
    assert value is not None, (
        f'{MAKEFILE.name} no longer exports UV_FROZEN, so `make test` and every other target run uv '
        f'unfrozen: a pyproject.toml edit makes the next `uv run` or `uv sync` re-resolve and '
        f'rewrite uv.lock with exit 0. Restore `export UV_FROZEN := 1`.'
    )
    assert value.lower() in UV_TRUTHY, (
        f'{MAKEFILE.name} exports UV_FROZEN={value!r}, which uv does not read as true, so the lock '
        f'is not frozen. Export UV_FROZEN := 1.'
    )


@pytest.mark.build_infra
def test_make_lock_removes_uv_frozen_for_its_relock():
    """tj-3zh7ss: `make lock` really re-locks, rather than checking the lock and exiting.

    With UV_FROZEN exported, a bare `uv lock` in the recipe reads the variable as --check-exists,
    so the one deliberate re-lock would stop changing the lock and nobody would notice until a
    dependency edit never reached it.
    """
    recipe = _make_recipe('lock')
    relocks = [command for line in recipe for command in _commands(line) if _runs_uv_lock(command)]
    assert relocks, f'the lock target in {MAKEFILE.name} runs no `uv lock`: {recipe}'
    frozen = [' '.join(command) for command in relocks if not _unsets_uv_frozen(command)]
    assert not frozen, (
        f'the lock target in {MAKEFILE.name} runs {frozen} with UV_FROZEN still set, and uv lock '
        f'reads that as --check-exists. Run it as `env -u UV_FROZEN uv lock`.'
    )


@pytest.mark.build_infra
def test_only_make_lock_touches_uv_frozen():
    """tj-3zh7ss: no recipe but `lock` sets, clears or overrides the frozen setting.

    The export makes every target safe. A `UV_FROZEN=0 uv sync` or an `env -u UV_FROZEN` in any
    other recipe opens that one target again, and make gives no sign of it.
    """
    lock_recipe = set(_make_recipe('lock'))
    folded = MAKEFILE.read_text(encoding='utf-8').replace('\\\n', ' ')
    offenders = []
    for line in folded.splitlines():
        # Recipe lines only. The top-level export is test_the_makefile_exports_a_frozen_lock_...'s
        # business, and one cause should make one test red.
        stripped = line.strip()
        if not line.startswith('\t') or 'UV_FROZEN' not in line or stripped.startswith('#'):
            continue
        if stripped.lstrip('@-+').strip() not in lock_recipe:
            offenders.append(stripped)
    assert not offenders, (
        f'{MAKEFILE.name} changes UV_FROZEN outside the lock target: {offenders}. Every other target '
        f'must install the committed lock as-is. A change to the lock goes through `make lock`.'
    )


@pytest.mark.build_infra
def test_the_uv_pin_check_is_one_shared_definition():
    """tj-3zh7ss: the guard is defined once, and it still refuses a uv older than the pin.

    It is shared so the guard on `lock` and the guard on the venv recipe cannot differ. That
    only holds while the shared definition still compares against UV_VERSION and still exits
    non-zero. A body reduced to a warning would pass every recipe that references it.
    """
    match = _UV_PIN_CHECK_DEFINE.search(MAKEFILE.read_text(encoding='utf-8'))
    assert match is not None, f'{MAKEFILE.name} no longer defines UV_PIN_CHECK'
    body = match.group(1)
    assert '$(UV_VERSION)' in body, 'UV_PIN_CHECK no longer compares against $(UV_VERSION)'
    assert 'exit 1' in body, (
        'UV_PIN_CHECK no longer exits non-zero on a uv older than the pin, so an old uv re-locks '
        'with exit 0 again (tj-jon3d1)'
    )


@pytest.mark.build_infra
@pytest.mark.parametrize('target', UV_PIN_CHECKED_TARGETS)
def test_recipe_runs_the_shared_uv_pin_check_before_uv(target: str):
    """tj-3zh7ss: `make lock` and the venv recipe run the same pin check, before any uv.

    `lock` is the one target that re-resolves, which is exactly what an old uv gets silently
    wrong: it drops settings it cannot parse, the install cooldown among them, and still
    exits 0. So the re-lock needs the guard more than anything else here does.
    """
    recipe = _make_recipe(target)
    checks = [index for index, line in enumerate(recipe) if UV_PIN_CHECK_REFERENCE in line]
    uv_lines = [
        index for index, line in enumerate(recipe) if any(_uv_subcommand_index(c) is not None for c in _commands(line))
    ]
    assert checks, (
        f'the {target!r} recipe in {MAKEFILE.name} no longer runs {UV_PIN_CHECK_REFERENCE}: {recipe}. '
        f'A uv older than the pin then runs it unchecked.'
    )
    assert not uv_lines or checks[0] < uv_lines[0], (
        f'the {target!r} recipe in {MAKEFILE.name} runs uv before {UV_PIN_CHECK_REFERENCE}: {recipe}'
    )


def _workflows_running_uv() -> list[Path]:
    """Every workflow with a scalar that runs uv, found by parsing rather than by file name."""
    return [
        path
        for path in _workflow_files()
        if any(re.search(r'(?<![\w-])uv\s+\w', scalar) for scalar in _walk_scalars(_load_yaml(path)))
    ]


@pytest.mark.build_infra
def test_the_testing_workflow_runs_uv():
    """Guard the guard: the workflow test below passes vacuously if no workflow is found to run uv."""
    assert TESTING_WORKFLOW in _workflows_running_uv(), (
        f'{TESTING_WORKFLOW.name} is not found to run uv, so the UV_FROZEN check covers nothing'
    )


@pytest.mark.build_infra
@pytest.mark.parametrize('workflow', _workflows_running_uv(), ids=lambda p: p.name)
def test_workflow_freezes_the_lock_at_workflow_level(workflow: Path):
    """tj-3zh7ss: CI is covered by a setting, not by the order of its steps.

    Before this, a CI `uv run` could re-lock too. CI was safe only because `uv sync --locked`
    ran first and failed on a stale lock. Workflow level, so a new job or step inherits it.
    A job or step that sets it false, or a script that unsets it, re-opens the door for that
    job alone, so both are refused too.
    """
    document = _load_yaml(workflow)
    value = (document.get('env') or {}).get('UV_FROZEN')
    assert value is not None and str(value).lower() in UV_TRUTHY, (
        f'{workflow.name} runs uv but its workflow-level env sets UV_FROZEN={value!r}. Set '
        f'UV_FROZEN: "1" there, so every uv in every job installs the committed lock as-is.'
    )
    overrides = [
        f'UV_FROZEN: {inner!r}'
        for key, inner in _walk_mappings(document.get('jobs') or {})
        if key == 'UV_FROZEN' and str(inner).lower() not in UV_TRUTHY
    ]
    scripts = [scalar for scalar in _walk_scalars(document.get('jobs') or {}) if 'UV_FROZEN' in scalar]
    assert not overrides and not scripts, (
        f'{workflow.name} overrides the workflow-level UV_FROZEN inside a job: {overrides + scripts}. '
        f'That job can re-lock again. A lock change is made with `make lock` and committed.'
    )


# ---------------------------------------------------------------------------------------
# THE RUNTIME CLOSURE HOLDS NO PRE-RELEASE (tj-w54ldu, companion to tj-bzcbb8)
#
# uv's default prerelease mode takes a pre-release whenever no stable release satisfies a
# requirement, and the install cooldown can create exactly that case: a floor younger than the
# window. It happened once. A re-lock picked sqlalchemy 2.1.0rc2, and what surfaced was three
# unrelated-looking data/store failures that had to be diagnosed by hand. tj-bzcbb8 capped that
# one package. This pins the OUTCOME, whatever the route: a cap removed, a global setting
# changed, or the next floor raised to a fresh release. The failure names the package.
#
# The closure is read from uv.lock, the thing that is actually installed, and not from
# pyproject. The runtime groups are every group the lock records EXCEPT the tool groups below,
# so a new service group is runtime from the moment it exists. The tool groups never reach a
# deploy image. security in particular holds the opentelemetry 0.58b0 betas, semgrep
# transitives from projects that publish only betas, and they are legitimate there.
#
# Markers are ignored on purpose. A dependency behind a platform marker is counted as if it
# applied everywhere. That can only over-report, and an over-report here is one line in a
# reviewed diff, where an under-report is a pre-release in an image.
LOCKFILE = REPO_ROOT / 'uv.lock'
NON_RUNTIME_GROUPS = frozenset({'security', 'dev', 'testing'})


def _load_lock() -> dict:
    with LOCKFILE.open('rb') as handle:
        return tomllib.load(handle)


def _project_lock_entry(lock: dict) -> dict:
    """The lock's entry for this project, which is where uv records each group's dependencies."""
    with PYPROJECT.open('rb') as handle:
        name = canonicalize_name(tomllib.load(handle)['project']['name'])
    entries = [package for package in lock.get('package', []) if canonicalize_name(package['name']) == name]
    assert len(entries) == 1, f'{LOCKFILE.name} holds {len(entries)} entries for the project {name!r}, expected 1'
    return entries[0]


def _runtime_groups(lock: dict) -> set[str]:
    return set(_project_lock_entry(lock).get('dev-dependencies', {})) - NON_RUNTIME_GROUPS


def _locked_closure(lock: dict, groups: set[str]) -> dict[str, str | None]:
    """Every package the named groups reach through the lock, mapped to its locked version.

    Walks `dependencies` and, for each extra a dependant asks for, that extra's
    `optional-dependencies`, so greenlet is reached through `sqlalchemy[asyncio]` the way uv
    installs it. The project's own `dependencies` are always included, since every group sits
    on top of them.
    """
    packages = {canonicalize_name(package['name']): package for package in lock.get('package', [])}
    project = _project_lock_entry(lock)
    roots = list(project.get('dependencies', []))
    for group in sorted(groups):
        roots.extend(project.get('dev-dependencies', {}).get(group, []))

    queue = [(canonicalize_name(entry['name']), extra) for entry in roots for extra in (None, *entry.get('extra', []))]
    seen: set[tuple[str, str | None]] = set()
    while queue:
        name, extra = queue.pop()
        if (name, extra) in seen:
            continue
        seen.add((name, extra))
        assert name in packages, f'{LOCKFILE.name} names a dependency on {name!r} but locks no package of that name'
        package = packages[name]
        edges = (
            package.get('dependencies', [])
            if extra is None
            else package.get('optional-dependencies', {}).get(extra, [])
        )
        queue.extend(
            (canonicalize_name(entry['name']), inner) for entry in edges for inner in (None, *entry.get('extra', []))
        )
    return {name: packages[name].get('version') for name, _ in seen}


def _runtime_prereleases(lock: dict) -> list[str]:
    """`name version` for every package in the runtime closure locked at a pre-release.

    packaging decides what a pre-release is, not a regex: a, b, rc and dev segments in every
    spelling PEP 440 allows. A version it cannot parse, or a package with no locked version, is
    reported too, since neither can be shown to be a final release.
    """
    offenders = []
    for name, version in sorted(_locked_closure(lock, _runtime_groups(lock)).items()):
        if version is None:
            offenders.append(f'{name} (no locked version)')
            continue
        try:
            parsed = Version(version)
        except InvalidVersion:
            offenders.append(f'{name} {version} (not a PEP 440 version)')
            continue
        if parsed.is_prerelease:
            offenders.append(f'{name} {version}')
    return offenders


def _runtime_roots(lock: dict) -> set[str]:
    """The packages the runtime groups declare directly, before any transitive dependency."""
    project = _project_lock_entry(lock)
    return {
        canonicalize_name(entry['name'])
        for group in _runtime_groups(lock)
        for entry in project['dev-dependencies'][group]
    }


def _synthetic_lock(lock: dict, versions: dict[str, str]) -> dict:
    """A copy of the lock with every version final, except the ones named. The real file is untouched.

    The dependency graph is the real one; only the versions are replaced. So the tests built on
    this pin how the check reads a graph, and stay independent of what the real lock holds today:
    a pre-release in it reds the test above and nothing else.
    """
    mutated = copy.deepcopy(lock)
    for package in mutated['package']:
        package['version'] = '1.0.0'
    for name, version in versions.items():
        matches = [package for package in mutated['package'] if canonicalize_name(package['name']) == name]
        assert matches, f'{LOCKFILE.name} locks no package named {name!r}'
        for package in matches:
            package['version'] = version
    return mutated


@pytest.mark.build_infra
def test_the_runtime_closure_is_not_empty():
    """Guard the guard: the pre-release check passes vacuously over a closure with nothing in it.

    `base` must be a runtime group, at least one service group must be too, and the walk must
    reach past the declared roots into their transitive dependencies.
    """
    lock = _load_lock()
    groups = _runtime_groups(lock)
    assert 'base' in groups, f'the runtime groups read from {LOCKFILE.name} are {sorted(groups)}, with no base'
    assert any(group.startswith('data-') for group in groups), f'no service group among {sorted(groups)}'
    roots = _runtime_roots(lock)
    closure = _locked_closure(lock, groups)
    assert roots < set(closure), f'the closure {sorted(closure)} does not extend past the roots {sorted(roots)}'


@pytest.mark.build_infra
def test_no_runtime_dependency_is_locked_at_a_pre_release():
    """tj-w54ldu: nothing that reaches a deploy image is a pre-release.

    If this is red after a re-lock, the resolver substituted a pre-release because no stable
    release satisfied a requirement, usually a floor raised above every release older than the
    install cooldown. Do not exempt the package. Lower the floor, cap the requirement below the
    pre-release's minor as tj-bzcbb8 did for sqlalchemy, or wait out the cooldown.
    """
    offenders = _runtime_prereleases(_load_lock())
    assert not offenders, (
        f'{LOCKFILE.name} locks pre-releases in the runtime closure (groups '
        f'{sorted(_runtime_groups(_load_lock()))}): {offenders}. uv took them because no stable release '
        f'satisfied a requirement. See tj-bzcbb8 for the mechanism and the fix.'
    )


@pytest.mark.build_infra
def test_the_prerelease_check_names_the_runtime_package_that_went_pre_release():
    """The case that happened, replayed on a copy of the lock: sqlalchemy locked at 2.1.0rc2.

    Also a transitive-only package, so the walk is shown to reach past the declared roots.
    """
    lock = _load_lock()
    assert _runtime_prereleases(_synthetic_lock(lock, {})) == [], 'the all-final baseline is not clean'
    assert _runtime_prereleases(_synthetic_lock(lock, {'sqlalchemy': '2.1.0rc2'})) == ['sqlalchemy 2.1.0rc2']

    transitive = sorted(set(_locked_closure(lock, _runtime_groups(lock))) - _runtime_roots(lock))
    assert transitive, 'the runtime closure has no transitive-only package to mutate'
    assert _runtime_prereleases(_synthetic_lock(lock, {transitive[0]: '1.0.dev3'})) == [f'{transitive[0]} 1.0.dev3']


@pytest.mark.build_infra
def test_the_prerelease_check_exempts_packages_only_the_tool_groups_reach():
    """The security, dev and testing closures may hold pre-releases, and must not trip the check.

    The real lock already holds the opentelemetry 0.58b0 betas under security, so the test
    above is green only if the exemption works. This makes that explicit and independent of
    whatever the lock happens to hold: every package reached ONLY through a tool group is set
    to a beta on a synthetic copy of the lock, and the check still reports nothing.
    """
    lock = _load_lock()
    runtime = set(_locked_closure(lock, _runtime_groups(lock)))
    tool_only = sorted(set(_locked_closure(lock, set(NON_RUNTIME_GROUPS))) - runtime)
    assert tool_only, 'no package is reached only through a tool group, so this exempts nothing'
    assert _runtime_prereleases(_synthetic_lock(lock, dict.fromkeys(tool_only, '0.58b0'))) == []


# ---------------------------------------------------------------------------------------
# THE SYSTEM SUITE HARNESS (tj-vhboky.48, ADR tj-fdb9gz sections 2, 3 and 7, ruling tj-vhboky.47 D1)
#
# tests/system/ writes to whatever database it is pointed at, and the machine that runs it also
# runs production. Two things keep that suite from running by accident, and both fail silently:
#
# 1. pytest.ini keeps tests/system out of the PR gate BY DIRECTORY (norecursedirs), not by a
#    marker. Drop the entry and the next `make test` or CI `uv run pytest` collects the suite; the
#    marker set is pinned by equality above, so a `system` marker cannot stand in for it.
# 2. `make test-system` refuses unless SYSTEM_TEST_DISPOSABLE_DB=1, reads .env with grep and never
#    sources it, never prints POSTGRES_PASS or INSTANCE_WRITE_SECRET, and ends on pytest so an
#    empty selection (exit 5) or a missing database fails the target.
#
# The recipe is read AS MAKE EXPANDS IT: `make -n` prints the shell text a run would execute
# without executing it. Reading the Makefile source instead would mean re-implementing make's
# variable expansion here, and `$(PYTEST_ENV)` splits into shell operators under shlex.
#
# Every make run here starts in an EMPTY temporary directory. That is what makes the behavioural
# guard test safe under a mutation that weakens the guard: with no .env beside it, the recipe
# stops at the .env check and cannot reach a database, whatever the developer's own .env says.
# .env itself is never read or written by this module.
SYSTEM_SUITE_DIR = 'tests/system'
SYSTEM_TARGET = 'test-system'
SYSTEM_GUARD = 'SYSTEM_TEST_DISPOSABLE_DB'

# The values the recipe reads out of .env, and the name the suite receives each one under. The
# names are data_store's own, so both sides of the mapping are the same key.
SYSTEM_ENV_FILE_KEYS = (
    'DATA_STORE_PORT',
    'DATABASE_PORT',
    'POSTGRES_USER',
    'POSTGRES_PASS',
    'POSTGRES_DB_NAME',
    'INSTANCE_WRITE_SECRET',
)
SYSTEM_SECRET_KEYS = ('POSTGRES_PASS', 'INSTANCE_WRITE_SECRET')

# The env contract the suite reads, by EQUALITY: a new variable in the contract is a deliberate
# edit to this set, in the same diff as the Makefile comment that documents it.
SYSTEM_ENV_CONTRACT = frozenset(
    {
        'POSTGRES_ASYNC',
        'POSTGRES_SYNC',
        'TZ',
        'PYTHONPATH',
        'SYSTEM_TEST_DATA_STORE_URL',
        'DATABASE_NAME',
        'DATABASE_PORT',
        'DATABASE_CONN_TIMEOUT',
        'POSTGRES_USER',
        'POSTGRES_PASS',
        'POSTGRES_DB_NAME',
        'INSTANCE_WRITE_SECRET',
    }
)

# Words that open a compound command rather than name the command run.
_SHELL_KEYWORDS = frozenset({'if', 'then', 'else', 'elif', 'fi', 'do', 'done', 'while', '{', '}', '!'})

# `name=$(helper KEY)`: a shell variable captured from the recipe's .env reader.
_ENV_CAPTURE = re.compile(r'^(\w+)=\$\((\w+) (\w+)\)$')

# `name() { body };` at the start of a logical line: a shell function definition.
_SHELL_FUNCTION = re.compile(r'^(\w+)\(\)\s*\{(.*?)\};')


def _subprocess_env(**overrides: str | None) -> dict[str, str]:
    """This process's environment with the named variables replaced, or removed when None."""
    env = dict(os.environ)
    env.pop('PYTEST_ADDOPTS', None)
    for name, value in overrides.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return env


def _run_make(cwd: Path, *arguments: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    """Run the repository Makefile from `cwd`, never remaking the venv marker.

    `-o` treats the marker as up to date, so no `uv sync` runs from a directory that has no
    pyproject.toml, and the recipe under test is the only thing make executes.
    """
    assert shutil.which('make'), 'make is not on PATH, so the Makefile target cannot be exercised'
    command = ['make', '--no-print-directory', '-C', str(cwd), '-f', str(MAKEFILE), '-o', _make_variable('VENV_MARKER')]
    return subprocess.run([*command, *arguments], capture_output=True, text=True, env=env, check=False)


def _system_recipe_lines(cwd: Path) -> list[str]:
    """The test-system recipe as make expands it, one logical shell line per entry, not executed."""
    result = _run_make(cwd, '-n', SYSTEM_TARGET, env=_subprocess_env(**{SYSTEM_GUARD: None}))
    assert result.returncode == 0, f'`make -n {SYSTEM_TARGET}` failed: {result.stderr}'
    folded = re.sub(r'\\\n\t?', ' ', result.stdout)
    lines = [line.strip() for line in folded.splitlines() if line.strip()]
    assert lines, f'`make -n {SYSTEM_TARGET}` printed no recipe'
    return lines


def _command_name(command: list[str]) -> str:
    """The command a simple command runs, past any compound-command keyword in front of it."""
    words = [word for word in command if word not in _SHELL_KEYWORDS]
    return PurePosixPath(words[0]).name if words else ''


def _assignments(command: list[str]) -> dict[str, str]:
    """The `NAME=value` prefix of a simple command, i.e. the environment it hands the command."""
    assigned = {}
    for word in command:
        name, separator, value = word.partition('=')
        if not separator or not re.fullmatch(r'[A-Za-z_]\w*', name):
            break
        assigned[name] = value
    return assigned


def _env_reader(lines: list[str]) -> tuple[str, str]:
    """The (name, body) of the shell function the recipe reads .env through. Exactly one."""
    readers = [
        (m.group(1), m.group(2)) for line in lines if (m := _SHELL_FUNCTION.match(line)) and '.env' in m.group(2)
    ]
    assert len(readers) == 1, f'expected one shell function reading .env in the {SYSTEM_TARGET} recipe, found {readers}'
    return readers[0]


def _env_captures(lines: list[str]) -> dict[str, str]:
    """Map each .env key the recipe reads to the shell variable it is captured in."""
    reader, _ = _env_reader(lines)
    captures = {}
    for line in lines:
        for command in _commands(line):
            for word in command:
                if (match := _ENV_CAPTURE.match(word)) and match.group(2) == reader:
                    captures[match.group(3)] = match.group(1)
    return captures


def _pytest_command(lines: list[str]) -> list[str]:
    commands = _commands(lines[-1])
    assert commands, f'the last line of the {SYSTEM_TARGET} recipe holds no command: {lines[-1]}'
    return commands[-1]


def _mentions_variable(word: str, variable: str) -> bool:
    return re.search(rf'\$(?:{re.escape(variable)}\b|\{{{re.escape(variable)}\}})', word) is not None


@pytest.mark.build_infra
def test_pytest_ini_keeps_the_system_suite_out_by_directory():
    """tj-vhboky.48 item 1: the exclusion is a norecursedirs entry, not a marker and not --ignore.

    The pytest.ini comment records why the alternatives were rejected: --ignore resolves against
    the invocation directory and stopped excluding from a subdirectory, and testpaths does not
    apply when a path is given, which `make test` always does.
    """
    entries = _pytest_ini().get('norecursedirs', '').split()
    assert SYSTEM_SUITE_DIR in entries, (
        f'{PYTEST_INI.name} norecursedirs is {entries}, without {SYSTEM_SUITE_DIR!r}. The PR gate then '
        f'collects the system suite, which writes to whatever database the environment points at.'
    )
    assert not any(token.startswith('--ignore') for token in _addopts_tokens()), (
        f'{PYTEST_INI.name} addopts carries an --ignore, which pytest resolves against the invocation '
        f'directory. The system suite is excluded by norecursedirs instead.'
    )


# (arguments, directory to start pytest in relative to the copy, collected?). The first three are
# the PR gate as make, CI and a run from a subdirectory start it; the last two are how
# `make test-system` names the suite, which must still collect.
_SYSTEM_COLLECTION_CASES = {
    'gate-whole-tree': (['.'], '.', False),
    'gate-no-arguments-as-ci': ([], '.', False),
    'gate-from-a-subdirectory': (['..'], 'gate', False),
    'named-directory': ([SYSTEM_SUITE_DIR], '.', True),
    'named-file': ([f'{SYSTEM_SUITE_DIR}/test_system_probe.py'], '.', True),
}


@pytest.mark.build_infra
@pytest.mark.parametrize(
    ('arguments', 'start', 'collected'), list(_SYSTEM_COLLECTION_CASES.values()), ids=list(_SYSTEM_COLLECTION_CASES)
)
def test_the_gate_does_not_collect_a_test_under_tests_system(
    tmp_path: Path, arguments: list[str], start: str, collected: bool
):
    """tj-vhboky.48 DONE WHEN: the gate collects nothing from tests/system; naming it collects it.

    Measured, not inferred from the ini text: the real pytest.ini is copied beside a probe under
    tests/system and a control test outside it, and pytest itself is asked what it collects. The
    control proves the run collected something, so a probe that is absent is absent because of
    the exclusion and not because collection broke.
    """
    shutil.copy(PYTEST_INI, tmp_path / PYTEST_INI.name)
    (tmp_path / SYSTEM_SUITE_DIR).mkdir(parents=True)
    (tmp_path / SYSTEM_SUITE_DIR / 'test_system_probe.py').write_text('def test_system_probe():\n    pass\n')
    (tmp_path / 'gate').mkdir()
    (tmp_path / 'gate' / 'test_gate_control.py').write_text('def test_gate_control():\n    pass\n')

    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '--collect-only', '-q', '-p', 'no:cacheprovider', *arguments],
        cwd=tmp_path / start,
        capture_output=True,
        text=True,
        env=_subprocess_env(),
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, f'collection in the copy failed (exit {result.returncode}):\n{output}'
    assert ('test_system_probe' in result.stdout) is collected, (
        f'pytest {" ".join(arguments) or "(no arguments)"} from {start!r} '
        f'{"did not collect" if collected else "COLLECTED"} the probe under {SYSTEM_SUITE_DIR}:\n{output}'
    )
    if not collected:
        assert 'test_gate_control' in result.stdout, (
            f'the control test outside {SYSTEM_SUITE_DIR} was not collected:\n{output}'
        )


@pytest.mark.build_infra
def test_test_system_is_phony():
    """tj-06uflo: undeclared, a file named test-system at the root makes the target a no-op exit 0."""
    assert re.search(
        rf'^\.PHONY:.*(?<![\w-]){re.escape(SYSTEM_TARGET)}(?![\w-])', MAKEFILE.read_text(), re.MULTILINE
    ), f'{MAKEFILE.name} does not declare {SYSTEM_TARGET} .PHONY'


# (how the guard is given, as make arguments, as environment). Everything but exactly "1" refuses.
_REFUSED_GUARDS = {
    'unset': ([], None),
    'zero-argument': ([f'{SYSTEM_GUARD}=0'], None),
    'yes-argument': ([f'{SYSTEM_GUARD}=yes'], None),
    'true-argument': ([f'{SYSTEM_GUARD}=true'], None),
    'empty-argument': ([f'{SYSTEM_GUARD}='], None),
    'zero-environment': ([], '0'),
}
_ACCEPTED_GUARDS = {'one-argument': ([f'{SYSTEM_GUARD}=1'], None), 'one-environment': ([], '1')}


@pytest.mark.build_infra
@pytest.mark.parametrize(('arguments', 'environment'), list(_REFUSED_GUARDS.values()), ids=list(_REFUSED_GUARDS))
def test_test_system_refuses_without_the_disposable_database_attestation(
    tmp_path: Path, arguments: list[str], environment: str | None
):
    """tj-vhboky.48 item 2a: the target refuses, says why, and refuses before it reads anything.

    The reason is part of the contract: someone who hits the refusal has to learn that the suite
    WRITES to the database and that production shares the machine, or the guard trains them to
    type =1 without thinking. "no .env" absent from the output shows the guard fired first.
    """
    result = _run_make(tmp_path, SYSTEM_TARGET, *arguments, env=_subprocess_env(**{SYSTEM_GUARD: environment}))
    assert result.returncode != 0, (
        f'make {SYSTEM_TARGET} {arguments} ran with the guard {environment!r}:\n{result.stdout}'
    )
    for phrase in ('REFUSED', 'WRITES to the database it is pointed at', 'production deployment', f'{SYSTEM_GUARD}=1'):
        assert phrase in result.stderr, f'the refusal does not say {phrase!r}:\n{result.stderr}'
    assert 'no .env' not in result.stderr, f'the recipe got past the guard before refusing:\n{result.stderr}'


@pytest.mark.build_infra
@pytest.mark.parametrize(('arguments', 'environment'), list(_ACCEPTED_GUARDS.values()), ids=list(_ACCEPTED_GUARDS))
def test_test_system_passes_the_guard_on_exactly_one(tmp_path: Path, arguments: list[str], environment: str | None):
    """The converse, so the refusal above is shown to be conditional rather than unconditional.

    Run from an empty directory, the recipe then stops at the missing .env and still fails --
    which also pins that a missing .env is a failure and never a skip.
    """
    result = _run_make(tmp_path, SYSTEM_TARGET, *arguments, env=_subprocess_env(**{SYSTEM_GUARD: environment}))
    assert 'REFUSED' not in result.stderr, f'the guard refused {arguments or environment!r}:\n{result.stderr}'
    assert result.returncode != 0 and 'no .env' in result.stderr, (
        f'with no .env beside it the target should fail naming the missing .env; exit {result.returncode}:\n'
        f'{result.stdout}{result.stderr}'
    )


@pytest.mark.build_infra
def test_test_system_reads_env_with_grep_and_never_sources_it(tmp_path: Path):
    """tj-vhboky.48 item 2c: .env is read one key at a time, never loaded into the shell.

    Sourcing it -- `. .env`, `source .env`, `set -a`, `eval` -- puts POSTGRES_PASS and the write
    secret into the shell where one stray `set -x` or echo prints them (the CI Smoke Test step's
    reasoning). So .env may appear in exactly two places: the existence check, and a grep inside
    the one reader function whose output is only ever captured into a variable.
    """
    lines = _system_recipe_lines(tmp_path)
    reader, body = _env_reader(lines)
    assert re.match(r'^\s*grep\b', body), f'the .env reader {reader}() does not start with grep: {body!r}'

    offenders = []
    for line in lines:
        # The reader's own definition is judged above; drop it so its grep is not judged twice.
        judged = _SHELL_FUNCTION.sub('', line, count=1) if line.startswith(f'{reader}()') else line
        for command in _commands(judged):
            name = _command_name(command)
            words = [word for word in command if word not in _SHELL_KEYWORDS]
            allexport = name == 'set' and any(
                (word.startswith(('-', '+')) and not word.startswith('--') and 'a' in word) or word == 'allexport'
                for word in words[1:]
            )
            loads = name in ('.', 'source', 'eval', 'export') or allexport
            reads_elsewhere = '.env' in words and words[:3] != ['[', '-f', '.env']
            if loads or reads_elsewhere:
                offenders.append(' '.join(command))
            elif name == reader:
                offenders.append(f'{" ".join(command)} (prints the value it reads)')
            elif any(f'$({reader} ' in word and not _ENV_CAPTURE.match(word) for word in words):
                offenders.append(f'{" ".join(command)} (reader output not captured into a variable)')
    assert not offenders, (
        f'the {SYSTEM_TARGET} recipe loads or reads .env outside the grep reader, or calls the reader '
        f'outside a $(...) capture: {offenders}'
    )
    assert set(_env_captures(lines)) == set(SYSTEM_ENV_FILE_KEYS), (
        f'the recipe reads {sorted(_env_captures(lines))} from .env, expected {sorted(SYSTEM_ENV_FILE_KEYS)}'
    )


@pytest.mark.build_infra
def test_test_system_never_prints_a_secret(tmp_path: Path):
    """tj-vhboky.48 item 2c: POSTGRES_PASS and INSTANCE_WRITE_SECRET never reach the output.

    The banner may NAME them; no echo or printf may expand the variables holding them, and no
    shell tracing may be switched on, since `set -x` prints the pytest line with values expanded.
    """
    lines = _system_recipe_lines(tmp_path)
    captures = _env_captures(lines)
    secrets = {key: captures.get(key) for key in SYSTEM_SECRET_KEYS}
    assert all(secrets.values()), f'the recipe captures no variable for {secrets}, so this check would see nothing'

    offenders = []
    for line in lines:
        for command in _commands(line):
            name = _command_name(command)
            words = [word for word in command if word not in _SHELL_KEYWORDS]
            tracing = name == 'set' and any(
                (word.startswith('-') and not word.startswith('--') and ('x' in word or 'v' in word))
                or word in ('xtrace', 'verbose')
                for word in words[1:]
            )
            if tracing:
                offenders.append(' '.join(command))
            if name in ('echo', 'printf', 'tee', 'cat'):
                leaked = [
                    key for key, variable in secrets.items() if any(_mentions_variable(w, variable) for w in words)
                ]
                if leaked:
                    offenders.append(f'{" ".join(command)} (prints {leaked})')
    assert not offenders, f'the {SYSTEM_TARGET} recipe prints a secret or traces the shell: {offenders}'


@pytest.mark.build_infra
def test_test_system_fails_on_every_missing_env_value(tmp_path: Path):
    """tj-vhboky.48 item 2b: each value read from .env is checked non-empty before pytest starts.

    One missing check means the suite starts with an empty password or port and fails somewhere
    downstream with a message that names neither.
    """
    lines = _system_recipe_lines(tmp_path)
    checked = {
        word.lstrip('$')
        for line in lines
        for command in _commands(line)
        if (words := [w for w in command if w not in _SHELL_KEYWORDS])[:2] == ['[', '-n']
        for word in words[2:3]
    }
    unchecked = sorted(key for key, variable in _env_captures(lines).items() if variable not in checked)
    assert not unchecked, f'the {SYSTEM_TARGET} recipe reads {unchecked} from .env without failing when empty'


@pytest.mark.build_infra
def test_test_system_ends_on_pytest_over_the_system_suite(tmp_path: Path):
    """tj-vhboky.48 item 2d: the recipe's last command IS pytest, so its exit status is make's.

    Anything after it -- `|| true`, `; exit 0`, a `| tee` -- turns pytest's exit 5 on an empty
    selection, and every red, into a green make. The default path must be the suite itself, and
    the PYTEST_ENV driver flags every test target sets must be there too.
    """
    lines = _system_recipe_lines(tmp_path)
    command = _pytest_command(lines)
    assigned = _assignments(command)
    invoked = command[len(assigned) :]
    assert invoked[:3] == ['uv', 'run', 'pytest'], (
        f'the {SYSTEM_TARGET} recipe does not end on `uv run pytest`: {command}'
    )
    assert invoked[3:] == [SYSTEM_SUITE_DIR], (
        f'the recipe runs pytest over {invoked[3:]}, expected [{SYSTEM_SUITE_DIR!r}]'
    )
    for word in _make_variable('PYTEST_ENV').split():
        name, _, value = word.partition('=')
        assert assigned.get(name) == value, f'the pytest command lost $(PYTEST_ENV)`s {word}: {assigned}'


@pytest.mark.build_infra
def test_test_system_hands_the_suite_the_env_contract(tmp_path: Path):
    """tj-vhboky.48 item 2b: the contract, in one place, with the values the bead requires.

    - Postgres and data_store are reached on loopback from the runner.
    - Each .env-backed name carries the value read for that same key, so a swap is caught.
    - The repository root is on PYTHONPATH: tests/system has no package chain to provide it.
    - TZ is off UTC in both January and July, so a naive-to-timestamptz shift can go red on a
      UTC runner in either season.
    """
    lines = _system_recipe_lines(tmp_path)
    assigned = _assignments(_pytest_command(lines))
    assert set(assigned) == set(SYSTEM_ENV_CONTRACT), (
        f'the pytest command receives {sorted(assigned)}, expected exactly {sorted(SYSTEM_ENV_CONTRACT)}. '
        f'Change the contract here and in the Makefile comment above {SYSTEM_TARGET} together.'
    )
    assert assigned['DATABASE_NAME'] == '127.0.0.1', (
        f'DATABASE_NAME (the Postgres host) is {assigned["DATABASE_NAME"]!r}'
    )
    assert assigned['SYSTEM_TEST_DATA_STORE_URL'].startswith('http://127.0.0.1:'), assigned[
        'SYSTEM_TEST_DATA_STORE_URL'
    ]
    assert Path(assigned['PYTHONPATH']).resolve() == tmp_path.resolve(), (
        f'PYTHONPATH is {assigned["PYTHONPATH"]!r}, not the directory make runs in ({tmp_path})'
    )

    captures = _env_captures(lines)
    for key in set(SYSTEM_ENV_FILE_KEYS) & set(SYSTEM_ENV_CONTRACT):
        assert assigned[key] == f'${captures[key]}', f'{key} is handed {assigned[key]!r}, not the value read from .env'
    assert _mentions_variable(assigned['SYSTEM_TEST_DATA_STORE_URL'], captures['DATA_STORE_PORT'])

    zone = ZoneInfo(assigned['TZ'])
    for month in (1, 7):
        offset = datetime(2026, month, 15, 12, tzinfo=UTC).astimezone(zone).utcoffset()
        assert offset, f'TZ={assigned["TZ"]} is UTC in month {month}, where a naive shift is invisible'


@pytest.mark.build_infra
def test_test_system_neither_starts_nor_migrates_the_stack(tmp_path: Path):
    """tj-vhboky.48 item 2: which database gets touched stays the decision of whoever runs it."""
    lines = _system_recipe_lines(tmp_path)
    forbidden = {'docker', 'docker-compose', 'alembic', MIGRATIONS_SCRIPT.name, 'make'}
    offenders = [
        ' '.join(command)
        for line in lines
        for command in _commands(line)
        if forbidden & {PurePosixPath(word).name for word in command}
    ]
    assert not offenders, f'the {SYSTEM_TARGET} recipe starts, stops or migrates something: {offenders}'


# ---------------------------------------------------------------------------------------
# BANDIT'S EXCLUDE (tj-vhboky.67)
#
# `--exclude tests/` passed for a year and then silently stopped excluding anything: bandit
# rewrites an exclude that names an EXISTING directory, relative to its cwd, into `<dir>/*`, and
# matches each discovered path by fnmatch OR by substring. The repository-root tests/ (the
# system suite) made `tests/` exist, so it became `tests/*`, which no `./common/tests/...` path
# matches either way, and bandit reported a thousand asserts in test files. Nothing about the
# exclude TEXT changed, which is why a string check cannot guard it: the file set bandit scans is
# the thing pinned here.
#
# bandit sits in the `security` group, which neither the PR gate's venv nor the CI unit-test job
# installs, and a test that skips without it would be the silent green pytest.ini forbids. So
# _bandit_scanned_files follows bandit 1.9.4's discover_files, _get_files_from_dir and
# _is_file_included (bandit/core/manager.py) step for step, over the invocation's own targets and
# excludes, read from the Makefile and the workflow rather than restated. It was cross-checked
# against bandit's own BanditManager.discover_files when it was written (tj-vhboky.67 notes): the
# same file set for '*/tests/*' and for 'tests/'. Only the flags the invocation uses are modelled;
# any other flag fails the test rather than being ignored.
BANDIT_TEST_SEGMENT = 'tests'
_BANDIT_MODELLED_FLAGS = frozenset({'-r', '--recursive'})
_BANDIT_EXCLUDE_OPTIONS = frozenset({'-x', '--exclude'})
_BANDIT_DEFAULT_INCLUDE = '*.py'  # bandit's `include` default; no bandit config file sets another
_MAKE_REFERENCE = re.compile(r'\$\((\w+)\)')
_WORKFLOW_ENV_REFERENCE = re.compile(r'\$\{\{\s*env\.(\w+)\s*\}\}')


def _run_lines(run: str) -> list[str]:
    """The logical lines of a `run:` script: continuations folded, blanks and comment lines dropped."""
    folded = run.replace('\\\n', ' ')
    return [line.strip() for line in folded.splitlines() if line.strip() and not line.strip().startswith('#')]


def _runs_bandit(words: list[str]) -> bool:
    return any(PurePosixPath(word).name == 'bandit' for word in words)


def _makefile_bandit_invocation() -> list[str]:
    """The security target's bandit command as argv, with every $(VAR) expanded from the Makefile."""
    lines = [line for line in _make_recipe('security') if _runs_bandit(shlex.split(line))]
    assert len(lines) == 1, f'expected one bandit line in the security target, found {lines}'
    return shlex.split(_MAKE_REFERENCE.sub(lambda match: _make_variable(match.group(1)), lines[0]))


def _workflow_bandit_invocations() -> dict[str, list[str]]:
    """Every bandit command in every workflow, as argv with ${{ env.X }} expanded, keyed by where it is."""
    found = {}
    for path in _workflow_files():
        document = _load_yaml(path) or {}
        for job_id, job in (document.get('jobs') or {}).items():
            for index, step in enumerate((job or {}).get('steps') or []):
                env = {**(document.get('env') or {}), **(job.get('env') or {}), **(step.get('env') or {})}
                for line in _run_lines(step.get('run') or ''):
                    expanded = _WORKFLOW_ENV_REFERENCE.sub(lambda match, env=env: str(env[match.group(1)]), line)
                    words = shlex.split(expanded)
                    if _runs_bandit(words):
                        found[f'{path.name} {job_id} step {index} ({step.get("name")})'] = words
    return found


def _bandit_arguments(invocation: list[str]) -> tuple[list[str], list[str]]:
    """Split a bandit argv into (targets, raw exclude entries), refusing any flag not modelled."""
    arguments = invocation[next(i for i, word in enumerate(invocation) if PurePosixPath(word).name == 'bandit') + 1 :]
    assert set(arguments) & _BANDIT_MODELLED_FLAGS, (
        f'bandit is not run recursively, so it scans no directory: {arguments}'
    )
    targets, excludes = [], []
    position = 0
    while position < len(arguments):
        word = arguments[position]
        option, equals, value = word.partition('=')
        if option in _BANDIT_EXCLUDE_OPTIONS:
            if not equals:
                position += 1
                value = arguments[position]
            excludes.extend(value.split(','))
        elif word in _BANDIT_MODELLED_FLAGS:
            pass
        else:
            assert not word.startswith('-'), (
                f'bandit is invoked with {word!r}, which _bandit_scanned_files does not model. Teach it the '
                f'flag (bandit/core/manager.py and cli/main.py) rather than dropping it from this list.'
            )
            targets.append(word)
        position += 1
    assert targets, f'bandit is given no target: {invocation}'
    return targets, excludes


def _bandit_scanned_files(invocation: list[str], cwd: Path) -> set[str]:
    """The .py files bandit would scan for this argv run from `cwd`, normalised relative to it.

    Mirrors bandit 1.9.4: an exclude naming an existing directory (relative to the cwd) becomes
    `<dir>/*`; then every file os.walk finds under a target is kept when it fnmatches the include
    glob and neither fnmatches nor contains any exclude.
    """
    targets, raw_excludes = _bandit_arguments(invocation)
    excludes = [os.path.join(entry, '*') if (cwd / entry).is_dir() else entry for entry in raw_excludes]
    previous = Path.cwd()
    os.chdir(cwd)
    try:
        scanned = set()
        for target in targets:
            assert os.path.isdir(target), f'bandit target {target!r} is not a directory under {cwd}'
            for root, _, names in os.walk(target):
                for name in names:
                    path = os.path.join(root, name)
                    if not fnmatch.fnmatch(path, _BANDIT_DEFAULT_INCLUDE):
                        continue
                    if any(fnmatch.fnmatch(path, glob) for glob in excludes) or any(x in path for x in excludes):
                        continue
                    scanned.add(os.path.normpath(path))
    finally:
        os.chdir(previous)
    return scanned


def _is_test_path(path: str) -> bool:
    return BANDIT_TEST_SEGMENT in PurePosixPath(path).parts[:-1]


@pytest.mark.build_infra
def test_the_ci_bandit_invocation_is_the_makefiles():
    """tj-vhboky.67, tj-1mtrlh.2: CI's bandit command equals `make security`'s, variables expanded.

    Compared as argv after each side expands its own variables ($(SOURCE_DIRS), ${{ env.SOURCE_PATHS }}),
    so a quoting-only difference is not a failure and a changed root, flag or exclude on one side is.
    """
    expected = _makefile_bandit_invocation()
    invocations = _workflow_bandit_invocations()
    assert invocations, 'no workflow step runs bandit, so the CI security job scans nothing'
    drifted = {where: words for where, words in invocations.items() if words != expected}
    assert not drifted, (
        f'the workflow runs bandit differently from the Makefile security target ({shlex.join(expected)}): '
        f'{ {where: shlex.join(words) for where, words in drifted.items()} }. Change both or neither.'
    )


@pytest.mark.build_infra
@pytest.mark.parametrize('source', ['Makefile', 'workflow'])
def test_bandit_scans_every_production_file_and_no_test_file(source: str):
    """tj-vhboky.67 acceptance: the exclude matches every nested tests dir and no production path.

    Judged on the file set bandit discovers from the repository root, not on the exclude text.
    Production is every tracked .py under SOURCE_DIRS outside a `tests` directory.
    """
    invocations = (
        [_makefile_bandit_invocation()] if source == 'Makefile' else list(_workflow_bandit_invocations().values())
    )
    assert invocations, f'no {source} bandit invocation found'
    tracked = _tracked_source_python()
    production = {path for path in tracked if not _is_test_path(path)}
    tests = tracked - production
    assert tests, 'git tracks no test file under SOURCE_DIRS, so the exclude side of this check is vacuous'
    for invocation in invocations:
        scanned = _bandit_scanned_files(invocation, REPO_ROOT)
        unscanned = sorted(production - scanned)
        assert not unscanned, (
            f'{shlex.join(invocation)} does not scan {len(unscanned)} production file(s): {unscanned[:10]}. '
            f'The exclude now matches source, which is the silent direction: bandit reports fewer lines and exits 0.'
        )
        scanned_tests = sorted(path for path in scanned if _is_test_path(path))
        assert not scanned_tests, (
            f'{shlex.join(invocation)} scans {len(scanned_tests)} file(s) in tests directories, e.g. '
            f'{scanned_tests[:5]}: B101 fires on every assert and make security goes red. bandit rewrites an '
            f'exclude naming an existing directory to `<dir>/*`; exclude `*/tests/*`, never a bare `tests/`.'
        )


@pytest.mark.build_infra
@pytest.mark.parametrize(
    ('exclude', 'root_tests_dir', 'nested_test_scanned'),
    [
        ('tests/', False, False),
        ('tests/', True, True),  # the tj-vhboky.67 regression
        ('*/tests/*', False, False),
        ('*/tests/*', True, False),
    ],
    ids=['bare-without-root-dir', 'bare-with-root-dir', 'glob-without-root-dir', 'glob-with-root-dir'],
)
def test_the_bandit_model_reproduces_the_directory_rewrite(
    tmp_path: Path, exclude: str, root_tests_dir: bool, nested_test_scanned: bool
):
    """Guard the guard: the model above goes red on exactly the shape that broke, and only then."""
    (tmp_path / 'pkg' / 'tests').mkdir(parents=True)
    (tmp_path / 'pkg' / 'module.py').write_text('')
    (tmp_path / 'pkg' / 'tests' / 'test_module.py').write_text('')
    if root_tests_dir:
        (tmp_path / 'tests').mkdir()
    scanned = _bandit_scanned_files(['bandit', '-r', './pkg', '--exclude', exclude], tmp_path)
    assert os.path.join('pkg', 'module.py') in scanned
    assert (os.path.join('pkg', 'tests', 'test_module.py') in scanned) is nested_test_scanned, scanned
