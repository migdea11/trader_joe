"""Static invariants over the repository's CI and compose configuration.

These assert properties of committed YAML, not of a running system. They need no docker
daemon, no broker and no network, which is exactly why they are worth having: the two
rules below were bought by tj-6g25vo and tj-nbhgtf and are currently defended only by a
comment at the top of a file. A comment does not fail a build.

What these tests deliberately do NOT cover: whether `docker compose up --wait` actually
returns non-zero on a broken service. That requires a daemon and is tracked in tj-5zep48.
A green run here means the configuration still says the right thing, nothing more.
"""

import ast
import configparser
import copy
import fnmatch
import importlib
import ipaddress
import json
import os
import posixpath
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
from typing import Annotated
from zoneinfo import ZoneInfo

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from common.environment import get_env_var
from common.tests.compose_model import InterpolationRefused, interpolate


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
#
# common/rpc/generated/ is protoc's committed output, excluded by ADR tj-8konfu D3 (re-homed by
# addendum A1) as generated code, the same way the revisions are. `make proto` writes it and nothing
# else does, so it is never hand-edited or reformatted. CI's staleness step regenerates it and fails
# on any difference, so a hand-written file slipped in there shows up as one `make proto` deletes.
# The prefix stops at generated/. The hand-written modules beside it in common/rpc/ stay linted.
RUFF_UNLINTED_SOURCE_PREFIXES = ('data/store/migrations/versions/', 'common/rpc/generated/')


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

SYSTEM_SECRET_KEYS = ('POSTGRES_PASS', 'INSTANCE_WRITE_SECRET')

# THE SYSTEM-TEST CLIENT (decision record tj-q9ae5u addendum 1 items 4' and 6'; N3 tj-ijpys9.10).
# make test-system no longer runs pytest on the host: it runs test_client, a container on the
# stack's own networks, and the env contract is set in that service's environment block.
TEST_CLIENT_FILE = REPO_ROOT / 'docker-compose.test-client.yaml'
TEST_CLIENT_SERVICE = 'test_client'
# store_api for data_store's API, store_db because the suite seeds and checks through Postgres (the
# documented harness-only exception; a strategy client joins store_api alone).
TEST_CLIENT_NETWORKS = frozenset({'store_api', 'store_db'})
TEST_CLIENT_GROUPS = frozenset({'base', 'data-store', 'testing'})
# Allowed in the client's environment beside the contract, and nothing else.
TEST_CLIENT_EXTRA_ENV = frozenset({'PYTHONDONTWRITEBYTECODE'})
# The contract values the suite cannot default: compose interpolates each from the project env
# file, with no default and no :? guard, and conftest's _contract fails naming a missing one.
SYSTEM_INTERPOLATED_KEYS = frozenset({'POSTGRES_USER', 'POSTGRES_DB_NAME', 'POSTGRES_PASS', 'INSTANCE_WRITE_SECRET'})
SYSTEM_CONFTEST = REPO_ROOT / 'tests' / 'system' / 'conftest.py'
# Makefile variables naming the other compose sets. None may load the client file.
OTHER_COMPOSE_VARIABLES = ('PROD_COMPOSE', 'DEV_COMPOSE', 'TOOLS_COMPOSE', 'AGENT_COMPOSE')
TEST_CLIENT_COMPOSE_VARIABLE = 'TEST_CLIENT_COMPOSE'
# A loopback address in any spelling a URL or a curl would use.
_LOOPBACK = re.compile(r'127\.0\.0\.1|\blocalhost\b|\[::1\]|0\.0\.0\.0')
# `docker compose run` options that take a value, so the service can be told from a value.
_COMPOSE_RUN_VALUE_OPTIONS = frozenset(
    {'--entrypoint', '-e', '--env', '-w', '--workdir', '-u', '--user', '-v', '--volume', '-p', '--publish'}
    | {'-l', '--label', '--name', '--env-from-file', '--pull', '--cap-add', '--cap-drop'}
)

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


def _expanded_make_variable(name: str, cwd: Path, env: dict[str, str]) -> str:
    """The value make gives a variable once the whole Makefile is parsed, under `env`, from `cwd`.

    Unlike _make_variable, which returns the assignment's source text, this is what make itself
    sees: `$(VENV_DIR)/...` resolved through UV_PROJECT_ENVIRONMENT, `$(DEV_COMPOSE) -f ...` spelled
    out. Nothing runs but the one print rule.
    """
    assert shutil.which('make'), 'make is not on PATH, so the Makefile cannot be exercised'
    command = ['make', '-s', '--no-print-directory', '-C', str(cwd), '-f', str(MAKEFILE)]
    command += ['--eval', 'print-value-%: ; @: $(info $($*))', f'print-value-{name}']
    result = subprocess.run(command, capture_output=True, text=True, env=env, check=False)
    assert result.returncode == 0, f'make could not evaluate {name}: {result.stderr}'
    return result.stdout.rstrip('\n')


def _run_make(cwd: Path, *arguments: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    """Run the repository Makefile from `cwd`, never remaking the venv marker.

    `-o` treats the marker as up to date, so no `uv sync` runs from a directory that has no
    pyproject.toml, and the recipe under test is the only thing make executes. The marker is
    passed as make EXPANDS it: since tj-3t2axg it is `$(VENV_DIR)/...`, and that source text,
    passed to -o, names no target at all and protects nothing.
    """
    assert shutil.which('make'), 'make is not on PATH, so the Makefile target cannot be exercised'
    marker = _expanded_make_variable('VENV_MARKER', cwd, env)
    command = ['make', '--no-print-directory', '-C', str(cwd), '-f', str(MAKEFILE), '-o', marker]
    return subprocess.run([*command, *arguments], capture_output=True, text=True, env=env, check=False)


def _system_recipe_lines(cwd: Path, *arguments: str) -> list[str]:
    """The test-system recipe as make expands it, one logical shell line per entry, not executed."""
    result = _run_make(cwd, '-n', SYSTEM_TARGET, *arguments, env=_subprocess_env(**{SYSTEM_GUARD: None}))
    assert result.returncode == 0, f'`make -n {SYSTEM_TARGET}` failed: {result.stderr}'
    folded = re.sub(r'\\\n\t?', ' ', result.stdout)
    lines = [line.strip() for line in folded.splitlines() if line.strip()]
    assert lines, f'`make -n {SYSTEM_TARGET}` printed no recipe'
    return lines


def _command_name(command: list[str]) -> str:
    """The command a simple command runs, past any compound-command keyword in front of it."""
    words = [word for word in command if word not in _SHELL_KEYWORDS]
    return PurePosixPath(words[0]).name if words else ''


def _compose_calls(line: str) -> list[tuple[list[str], list[str]]]:
    """Each docker compose invocation on a line, as (its -f files, the words after its options).

    The second item starts with the subcommand.
    """
    calls = []
    for match in _COMPOSE_INVOCATION.finditer(line):
        words = line[match.end() :].split()
        files, position = [], 0
        while position < len(words) and words[position].startswith('-'):
            option, equals, value = words[position].partition('=')
            if option in ('-f', '--file'):
                if not equals:
                    position += 1
                    value = words[position] if position < len(words) else ''
                files.append(value.strip('\'"'))
            elif option in _COMPOSE_VALUE_OPTIONS and not equals:
                position += 1
            position += 1
        calls.append((files, words[position:]))
    return calls


def _compose_projects(line: str) -> list[str | None]:
    """The -p / --project-name of each docker compose invocation on a line, None where it sets none."""
    projects = []
    for match in _COMPOSE_INVOCATION.finditer(line):
        words, project, position = line[match.end() :].split(), None, 0
        while position < len(words) and words[position].startswith('-'):
            option, equals, value = words[position].partition('=')
            if option in ('-p', '--project-name'):
                if not equals:
                    position += 1
                    value = words[position] if position < len(words) else ''
                project = value.strip('\'"')
            elif option in _COMPOSE_VALUE_OPTIONS and not equals:
                position += 1
            position += 1
        projects.append(project)
    return projects


def _compose_service(rest: list[str]) -> tuple[str, list[str], list[str]]:
    """Split a compose subcommand's words into (service, its options, the words after the service)."""
    options, position = [], 1
    while position < len(rest) and rest[position].startswith('-'):
        options.append(rest[position])
        if rest[position] in _COMPOSE_RUN_VALUE_OPTIONS:
            position += 1
            options.append(rest[position] if position < len(rest) else '')
        position += 1
    service = rest[position] if position < len(rest) else ''
    return service, options, rest[position + 1 :]


def _test_client() -> dict:
    services = _load_yaml(TEST_CLIENT_FILE).get('services') or {}
    assert list(services) == [TEST_CLIENT_SERVICE], (
        f'{TEST_CLIENT_FILE.name} must define {TEST_CLIENT_SERVICE} and nothing else, found {list(services)}'
    )
    return services[TEST_CLIENT_SERVICE]


def _service_networks(document: dict, service: str) -> set[str]:
    """The network keys a compose service joins, list or mapping form."""
    networks = ((document.get('services') or {}).get(service) or {}).get('networks') or []
    return {str(name) for name in networks}


def _client_file_pair() -> list[str]:
    return [COMPOSE_FILE.name, TEST_CLIENT_FILE.name]


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
def test_run_make_never_remakes_the_venv(tmp_path: Path):
    """The -o in _run_make must name the marker make actually builds, or it protects nothing.

    Every behavioural make test here runs from an empty directory. Were the marker remade there,
    a test that runs a target for real would `uv venv` and `uv sync` into that directory. `make -n
    test` (which depends on the marker) is the probe: with the guard working it prints pytest and
    no sync at all.
    """
    result = _run_make(tmp_path, '-n', 'test', env=_subprocess_env())
    assert result.returncode == 0, f'`make -n test` failed: {result.stderr}'
    assert 'uv sync' not in result.stdout and 'uv venv' not in result.stdout, (
        f'_run_make let make remake the venv marker from an empty directory:\n{result.stdout}'
    )
    assert 'pytest' in result.stdout, f'`make -n test` printed no pytest, so this probe saw nothing:\n{result.stdout}'


@pytest.mark.build_infra
def test_test_system_reads_nothing_from_env_and_never_loads_it(tmp_path: Path):
    """N5 re-pin of tj-vhboky.48 item 2c, after N3: .env only has to EXIST.

    Compose interpolates the credentials into test_client itself, so the recipe has no reason to
    open .env at all. It may test for the file -- one `[ -f .env ]` -- and nothing else: no grep, no
    redirect, no --env-file, and never `.`, `source`, `eval`, `export` or `set -a`, which would pull
    POSTGRES_PASS and the write secret into make's shell where a stray trace prints them.
    """
    lines = _system_recipe_lines(tmp_path)
    offenders, existence_checks = [], 0
    for line in lines:
        for command in _commands(line):
            name = _command_name(command)
            words = [word for word in command if word not in _SHELL_KEYWORDS]
            if words[:4] == ['[', '-f', '.env', ']']:
                existence_checks += 1
                continue
            allexport = name == 'set' and any(
                (word.startswith(('-', '+')) and not word.startswith('--') and 'a' in word) or word == 'allexport'
                for word in words[1:]
            )
            touches = [
                word for word in words if word == '.env' or word.endswith('/.env') or word.startswith('--env-file')
            ]
            if name in ('.', 'source', 'eval', 'export') or allexport or touches:
                offenders.append(' '.join(command))
    assert not offenders, f'the {SYSTEM_TARGET} recipe reads or loads .env beyond its existence check: {offenders}'
    assert existence_checks == 1, (
        f'the {SYSTEM_TARGET} recipe must check once that .env exists (compose interpolates from it), '
        f'found {existence_checks} checks'
    )


@pytest.mark.build_infra
def test_test_system_never_names_or_prints_a_secret(tmp_path: Path):
    """N5 re-pin of tj-vhboky.48 item 2c: no recipe line names POSTGRES_PASS or INSTANCE_WRITE_SECRET.

    The secrets reach test_client from .env through compose and never pass through make's shell,
    so the recipe has no business naming either -- not as an assignment, not as `-e NAME` on the
    compose command line, not in the banner. And no shell tracing, which would print whatever the
    shell expands.
    """
    lines = _system_recipe_lines(tmp_path)
    named = [line for line in lines if any(key in line for key in SYSTEM_SECRET_KEYS)]
    assert not named, f'the {SYSTEM_TARGET} recipe names a secret variable: {named}'
    tracing = [
        ' '.join(command)
        for line in lines
        for command in _commands(line)
        if _command_name(command) in ('set', 'bash', 'sh')
        and any(
            (word.startswith('-') and not word.startswith('--') and ('x' in word or 'v' in word))
            or word in ('xtrace', 'verbose')
            for word in command[1:]
        )
    ]
    assert not tracing, f'the {SYSTEM_TARGET} recipe traces the shell: {tracing}'


def _conftest_contract_names() -> set[str]:
    """The CONTRACT_* names tests/system/conftest.py reads through _contract, by ast -- never imported."""
    tree = ast.parse(SYSTEM_CONFTEST.read_text(encoding='utf-8'))
    names = {
        node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id.startswith('CONTRACT_')
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    assert names, f'{SYSTEM_CONFTEST.name} defines no CONTRACT_* names'
    return names


def _conftest_contract_function():
    """Conftest's _contract, compiled on its own from the ast.

    The module is NEVER imported: it lives outside the gate, and importing it would drag in its
    fixtures and their imports.
    """
    tree = ast.parse(SYSTEM_CONFTEST.read_text(encoding='utf-8'))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_contract']
    assert len(functions) == 1, f'{SYSTEM_CONFTEST.name} must define one _contract, found {len(functions)}'
    module = ast.Module(body=functions, type_ignores=[])
    namespace = {'os': os, 'pytest': pytest}
    exec(compile(module, str(SYSTEM_CONFTEST), 'exec'), namespace)
    return namespace['_contract']


@pytest.mark.build_infra
def test_every_interpolated_contract_value_is_plain_and_read_through_contract():
    """N5 re-pin of tj-vhboky.48 item 2b, static half: the values the suite cannot default.

    test_client interpolates exactly these from .env, each as plain ${NAME}: a `:-default` would
    hand the suite a made-up credential, and a `:?` guard is ruled out on purpose -- CI blanks
    INSTANCE_WRITE_SECRET mid-job and still needs the client. Every ${...} anywhere in the block is
    plain. And each interpolated name is one conftest reads through _contract, which is what fails.
    """
    environment = _compose_service_environment(TEST_CLIENT_FILE, TEST_CLIENT_SERVICE)
    guarded = [
        f'{key}={value}'
        for key, value in environment.items()
        for inner in re.findall(r'\$\{([^}]*)\}', value or '')
        if not re.fullmatch(r'\w+', inner)
    ]
    assert not guarded, f'{TEST_CLIENT_SERVICE} interpolates with a default or a guard: {guarded}'
    interpolated = {key for key, value in environment.items() if value == f'${{{key}}}'}
    assert interpolated == SYSTEM_INTERPOLATED_KEYS, (
        f'{TEST_CLIENT_SERVICE} interpolates {sorted(interpolated)} as ${{NAME}}, expected {sorted(SYSTEM_INTERPOLATED_KEYS)}'
    )
    contract_names = _conftest_contract_names()
    assert contract_names <= SYSTEM_ENV_CONTRACT, (
        f'{SYSTEM_CONFTEST.name} reads {sorted(contract_names - SYSTEM_ENV_CONTRACT)} outside the contract'
    )
    unread = sorted(SYSTEM_INTERPOLATED_KEYS - contract_names)
    assert not unread, (
        f'{sorted(unread)} are interpolated but not read through _contract, so a missing one fails nowhere'
    )


@pytest.mark.build_infra
@pytest.mark.parametrize('key', sorted(SYSTEM_INTERPOLATED_KEYS))
@pytest.mark.parametrize('state', ['missing', 'empty'])
def test_a_missing_contract_value_fails_naming_it(monkeypatch: pytest.MonkeyPatch, key: str, state: str):
    """N5 re-pin of tj-vhboky.48 item 2b, behavioural half: missing or empty FAILS, naming the variable.

    The recipe used to check each value non-empty before pytest; that check now lives in conftest's
    _contract, run here as written. A skip would be the silent green pytest.ini forbids, and a
    message that did not name the variable would send the reader to the wrong place.
    """
    contract = _conftest_contract_function()
    if state == 'missing':
        monkeypatch.delenv(key, raising=False)
    else:
        monkeypatch.setenv(key, '')
    with pytest.raises(pytest.fail.Exception) as failure:
        contract(key)
    assert key in str(failure.value), f'_contract failed on a {state} {key} without naming it: {failure.value}'

    monkeypatch.setenv(key, 'present')
    assert contract(key) == 'present', f'_contract did not return a set {key}'


@pytest.mark.build_infra
@pytest.mark.parametrize(
    ('arguments', 'suite'),
    [
        ([], SYSTEM_SUITE_DIR),
        ([f'SYSTEM_PATHS={SYSTEM_SUITE_DIR}/test_http_bars.py'], f'{SYSTEM_SUITE_DIR}/test_http_bars.py'),
    ],
    ids=['default', 'scoped'],
)
def test_test_system_ends_on_the_client_run_over_the_system_suite(tmp_path: Path, arguments: list[str], suite: str):
    """N5 re-pin of tj-vhboky.48 item 2d: the recipe ends on ONE compose run of test_client.

    Over exactly docker-compose.yaml and docker-compose.test-client.yaml -- not the dev override,
    so the client behaves the same against a dev stack and in CI -- removed on exit, no deps, the
    suite (or SYSTEM_PATHS) last, and nothing after it: a `|| true`, a `; exit 0` or a `| tee` would
    turn pytest's exit 5 on an empty selection, and every red, into a green make.
    """
    lines = _system_recipe_lines(tmp_path, *arguments)
    last = lines[-1]
    assert len(_commands(last)) == 1, f'the last line of {SYSTEM_TARGET} is more than one command: {last}'
    calls = _compose_calls(last)
    assert len(calls) == 1, f'{SYSTEM_TARGET} does not end on one docker compose invocation: {last}'
    files, rest = calls[0]
    assert files == _client_file_pair(), f'{SYSTEM_TARGET} runs compose over {files}, expected {_client_file_pair()}'
    assert rest[:1] == ['run'], f'{SYSTEM_TARGET} ends on compose {rest[:1]}, not run: {last}'
    service, options, trailing = _compose_service(rest)
    assert service == TEST_CLIENT_SERVICE, f'{SYSTEM_TARGET} runs {service!r}, not {TEST_CLIENT_SERVICE}: {last}'
    missing = {'--rm', '--no-deps'} - set(options)
    assert not missing, f'the client run lacks {sorted(missing)}: {last}'
    assert trailing == [suite], f'the client run is handed {trailing}, expected [{suite!r}]'


@pytest.mark.build_infra
def test_the_client_entrypoint_checks_tz_then_execs_pytest_with_its_arguments():
    """The container half of item 2d: pytest is the container's main process, handed "$@".

    The compose run's exit status is the container's, so the entrypoint must END by exec'ing pytest
    -- anything after, or a pytest that is not exec'd, puts a shell's status in between. And the TZ
    check that left the recipe (N3) runs here first. Run for real under sh, with the exec pointed
    at nothing: a UTC zone must stop before it, naming TZ; a real non-UTC zone must reach it.
    """
    client = _test_client()
    entrypoint = client.get('entrypoint')
    assert isinstance(entrypoint, list) and entrypoint[1:2] == ['-c'] and len(entrypoint) == 4, (
        f'{TEST_CLIENT_SERVICE} entrypoint must be [shell, -c, script, $0]: {entrypoint}'
    )
    shell, _, script, zero = entrypoint
    script_lines = [line.strip() for line in script.strip().splitlines() if line.strip()]
    final = shlex.split(script_lines[-1])
    assert (
        final[:1] == ['exec'] and PurePosixPath(final[1]).name == 'python' and final[2:] == ['-m', 'pytest', '$$@']
    ), f'the {TEST_CLIENT_SERVICE} entrypoint must end on `exec <venv python> -m pytest "$$@"`: {script_lines[-1]!r}'
    assert client.get('command') == [SYSTEM_SUITE_DIR], f'{TEST_CLIENT_SERVICE} command is {client.get("command")}'

    # Compose unescapes $$ to $; the exec is pointed at a marker so a zone that passes is visible.
    runnable = script.replace('$$', '$').replace(final[1], 'echo REACHED-EXEC')
    for zone, reaches in (('UTC', False), ('America/Toronto', True)):
        result = subprocess.run(
            [shell, '-c', runnable, zero, SYSTEM_SUITE_DIR],
            capture_output=True,
            text=True,
            env={**_subprocess_env(), 'TZ': zone},
            check=False,
        )
        if reaches:
            assert result.returncode == 0 and 'REACHED-EXEC -m pytest tests/system' in result.stdout, (
                f'TZ={zone} did not reach pytest with its arguments (exit {result.returncode}; is tzdata '
                f'installed here?): {result.stdout}{result.stderr}'
            )
        else:
            assert result.returncode != 0 and 'REACHED-EXEC' not in result.stdout and 'TZ' in result.stderr, (
                f'TZ={zone} was not refused before pytest, naming TZ (exit {result.returncode}): '
                f'{result.stdout}{result.stderr}'
            )


@pytest.mark.build_infra
def test_the_client_hands_the_suite_the_env_contract():
    """N5 re-pin of tj-vhboky.48 item 2b: the contract, set in test_client's environment block.

    - Exactly the contract's names, plus PYTHONDONTWRITEBYTECODE at most.
    - Postgres and data_store are reached by compose SERVICE name, each a service of
      docker-compose.yaml that shares a network with test_client; never loopback, which reaches
      nothing from inside a container and nothing at all in prod, which publishes no port.
    - DATABASE_PORT is the container port, 5432: the client is on the network.
    - The secrets are ${NAME} interpolation, never literals.
    - TZ is off UTC in both January and July, so a naive-to-timestamptz shift can go red.
    - PYTHONPATH is the working directory: tests/system has no package chain to provide the root.
    - The driver flags match $(PYTEST_ENV), as for every host test target.
    """
    client = _test_client()
    environment = _compose_service_environment(TEST_CLIENT_FILE, TEST_CLIENT_SERVICE)
    assert set(environment) - TEST_CLIENT_EXTRA_ENV == SYSTEM_ENV_CONTRACT, (
        f'{TEST_CLIENT_SERVICE} sets {sorted(environment)}, expected exactly {sorted(SYSTEM_ENV_CONTRACT)} '
        f'plus at most {sorted(TEST_CLIENT_EXTRA_ENV)}. Change the contract here and in the Makefile comment together.'
    )

    base = _load_yaml(COMPOSE_FILE)
    client_networks = {str(name) for name in client.get('networks') or []}
    url = re.fullmatch(r'http://([\w.-]+):\$\{APP_INTERNAL_PORT\}/?', environment['SYSTEM_TEST_DATA_STORE_URL'] or '')
    assert url, (
        f'SYSTEM_TEST_DATA_STORE_URL is {environment["SYSTEM_TEST_DATA_STORE_URL"]!r}, not http://<service>:${{APP_INTERNAL_PORT}}'
    )
    for key, host in (('DATABASE_NAME', environment['DATABASE_NAME']), ('SYSTEM_TEST_DATA_STORE_URL', url.group(1))):
        assert host in (base.get('services') or {}), (
            f'{key} points at {host!r}, which is not a service of {COMPOSE_FILE.name}: a client reaches the stack by service name'
        )
        shared = _service_networks(base, host) & client_networks
        assert shared, (
            f'{key} points at {host!r}, which shares no network with {TEST_CLIENT_SERVICE} ({sorted(client_networks)})'
        )
    assert environment['DATABASE_PORT'] == '5432', (
        f'DATABASE_PORT is {environment["DATABASE_PORT"]!r}, not the container port'
    )
    assert int(environment['DATABASE_CONN_TIMEOUT'] or 0) > 0, (
        'DATABASE_CONN_TIMEOUT must be a positive number of seconds'
    )
    for key in SYSTEM_SECRET_KEYS:
        assert environment[key] == f'${{{key}}}', (
            f'{key} is {environment[key]!r}: a secret is interpolated, never literal'
        )
    assert environment['PYTHONPATH'] == client.get('working_dir'), (
        f'PYTHONPATH {environment["PYTHONPATH"]!r} is not the working_dir {client.get("working_dir")!r}'
    )
    for word in _make_variable('PYTEST_ENV').split():
        name, _, value = word.partition('=')
        assert environment.get(name) == value, f'{TEST_CLIENT_SERVICE} lost $(PYTEST_ENV)`s {word}'

    zone = ZoneInfo(environment['TZ'])
    for month in (1, 7):
        offset = datetime(2026, month, 15, 12, tzinfo=UTC).astimezone(zone).utcoffset()
        assert offset, f'TZ={environment["TZ"]} is UTC in month {month}, where a naive shift is invisible'


@pytest.mark.build_infra
def test_test_system_runs_only_the_client(tmp_path: Path):
    """N5 re-pin of tj-vhboky.48 item 2: which database gets touched stays the runner's decision.

    The only docker invocation in the recipe is that one run of test_client: no up, down, start or
    exec, no alembic, no migration script, no nested make.
    """
    lines = _system_recipe_lines(tmp_path)
    docker = [
        ' '.join(command)
        for line in lines
        for command in _commands(line)
        if {'docker', 'docker-compose'} & {PurePosixPath(word).name for word in command}
    ]
    assert len(docker) == 1 and docker[0] == lines[-1], (
        f'the {SYSTEM_TARGET} recipe must invoke docker exactly once, as its last line: {docker}'
    )
    forbidden = {'alembic', MIGRATIONS_SCRIPT.name, 'make'}
    offenders = [
        ' '.join(command)
        for line in lines
        for command in _commands(line)
        if forbidden & {PurePosixPath(word).name for word in command}
    ]
    assert not offenders, f'the {SYSTEM_TARGET} recipe migrates or runs make: {offenders}'


def _env_file_directories() -> set[PurePosixPath]:
    """The directories that hold an env file any service of docker-compose.yaml loads.

    Each entry is RESOLVED before it is normalised (tj-c4mosr.5, addendum-2 pin (4)): since
    tj-c4mosr.7 the entries read ${ROOT_ENV_FILE:-.env} and the like, and normalising the raw string
    derived '${STORE_ENV_FILE:-./data/store' -- a directory nothing is ever under -- so the check that
    uses this guarded nothing and stayed green. An entry that still holds a '$' once its default is
    taken fails here, loudly, rather than being judged as a path.
    """
    directories = set()
    for name, spec in (_load_yaml(COMPOSE_FILE).get('services') or {}).items():
        env_files = (spec or {}).get('env_file') or []
        for entry in [env_files] if isinstance(env_files, str) else env_files:
            path = str(entry.get('path') if isinstance(entry, dict) else entry)
            try:
                resolved = interpolate(path, {})
            except InterpolationRefused as refused:
                raise AssertionError(f'{name} loads env file {path!r}, which has no default to judge') from refused
            assert resolved and '$' not in resolved, f'{name} loads env file {path!r}, which does not resolve to a path'
            directories.add(PurePosixPath(os.path.normpath(resolved)).parent)
    assert directories, f'no service in {COMPOSE_FILE.name} loads an env file, so this check would guard nothing'
    return directories


@pytest.mark.build_infra
def test_the_test_client_service_is_shaped_as_the_design_says():
    """tj-q9ae5u addendum 1 item 4', N3 item 2: networks, no ports, no env_file, read-only source.

    - Networks exactly store_api and store_db, keys the base file declares: never devnet, never
      ingest_store (unauthenticated data_ingest and the plaintext broker live there).
    - No ports: nothing reaches into the client. No env_file: the secrets arrive by interpolation,
      and an env_file would hand it pgAdmin's credentials or the ingest broker keys too.
    - Every mount read-only, and none is the repository root, an env file, a directory holding one
      (an env directory) or a virtual environment.
    - no-new-privileges, like every service.
    - The build target exists, copies no source (it is mounted) and syncs exactly the base,
      data-store and testing groups, frozen, with curl for CI's probes and tzdata for TZ.
    """
    client = _test_client()
    declared = set(_load_yaml(COMPOSE_FILE).get('networks') or {})
    networks = {str(name) for name in client.get('networks') or []}
    assert networks == TEST_CLIENT_NETWORKS, (
        f'{TEST_CLIENT_SERVICE} joins {sorted(networks)}, expected {sorted(TEST_CLIENT_NETWORKS)}'
    )
    assert networks <= declared, (
        f'{TEST_CLIENT_SERVICE} joins {sorted(networks - declared)}, undeclared in {COMPOSE_FILE.name}'
    )
    assert 'ports' not in client, f'{TEST_CLIENT_SERVICE} publishes ports: {client.get("ports")}'
    assert 'env_file' not in client, f'{TEST_CLIENT_SERVICE} loads env files: {client.get("env_file")}'
    assert 'no-new-privileges:true' in (client.get('security_opt') or []), (
        f'{TEST_CLIENT_SERVICE} lacks no-new-privileges'
    )

    volumes = client.get('volumes') or []
    assert volumes, f'{TEST_CLIENT_SERVICE} mounts nothing, so it has no suite to run'
    env_directories = _env_file_directories()
    offenders = []
    for volume in volumes:
        if isinstance(volume, dict):
            source, read_only = str(volume.get('source', '')), volume.get('read_only') is True
        else:
            parts = str(volume).split(':')
            source, read_only = parts[0], len(parts) == 3 and 'ro' in parts[2].split(',')
        normal = PurePosixPath(os.path.normpath(source))
        if not read_only:
            offenders.append(f'{volume} (writable)')
        if normal == PurePosixPath('.') or normal.name.startswith(('.env', '.venv')):
            offenders.append(f'{volume} (the repository root, an env file or a virtual environment)')
        if any(normal == directory or normal in directory.parents for directory in env_directories):
            offenders.append(f'{volume} (contains an env file)')
    assert not offenders, f'{TEST_CLIENT_SERVICE} mounts: {offenders}'

    target = (client.get('build') or {}).get('target')
    stages = _dockerfile_stages()
    assert target in stages, f'{TEST_CLIENT_SERVICE} builds target {target!r}, which the Dockerfile does not define'
    body = stages[target][1]
    copies = [line.strip() for line in body.splitlines() if line.strip().upper().startswith(('COPY', 'ADD'))]
    assert not copies, (
        f'{target} copies files in; the source is bind-mounted, so a stale image could run old tests: {copies}'
    )
    syncs = [line for line in body.splitlines() if 'uv sync' in line]
    assert len(syncs) == 1, f'{target} must run one uv sync, found {syncs}'
    groups = set(re.findall(r'--only-group\s+(\S+)', syncs[0]))
    assert groups == TEST_CLIENT_GROUPS and '--frozen' in syncs[0], (
        f'{target} syncs {sorted(groups)} ({syncs[0].strip()}), expected {sorted(TEST_CLIENT_GROUPS)}, frozen'
    )
    for package in ('curl', 'tzdata'):
        assert re.search(rf'apt-get install[^\n]*\b{package}\b', body), f'{target} does not install {package}'


@pytest.mark.build_infra
def test_no_other_compose_set_loads_the_client_file():
    """tj-q9ae5u addendum 1 item 4': the client file is loaded ONLY by make test-system and CI's client steps.

    Never by PROD_COMPOSE, DEV_COMPOSE, TOOLS_COMPOSE, the agent's set or run_migrations.sh: a prod
    or dev launch that picked it up would start a container on store_db beside the real stack.
    TEST_CLIENT_COMPOSE is exactly the base file plus the client file, and only test-system uses it.
    """
    env = _subprocess_env()
    loading = [
        name
        for name in OTHER_COMPOSE_VARIABLES
        if TEST_CLIENT_FILE.name in _expanded_make_variable(name, REPO_ROOT, env)
    ]
    assert not loading, f'{loading} load {TEST_CLIENT_FILE.name}'
    assert TEST_CLIENT_FILE.name not in MIGRATIONS_SCRIPT.read_text(encoding='utf-8'), (
        f'{MIGRATIONS_SCRIPT.name} loads {TEST_CLIENT_FILE.name}'
    )
    expanded = _expanded_make_variable(TEST_CLIENT_COMPOSE_VARIABLE, REPO_ROOT, env)
    calls = _compose_calls(expanded)
    assert len(calls) == 1 and calls[0][0] == _client_file_pair() and not calls[0][1], (
        f'{TEST_CLIENT_COMPOSE_VARIABLE} is {expanded!r}, expected docker compose over exactly {_client_file_pair()}'
    )
    recipe_lines = [
        line.strip()
        for line in MAKEFILE.read_text(encoding='utf-8').replace('\\\n', ' ').splitlines()
        if line.startswith('\t')
        and not line.strip().startswith('#')
        and (TEST_CLIENT_FILE.name in line or f'$({TEST_CLIENT_COMPOSE_VARIABLE})' in line)
    ]
    system_recipe = _make_recipe(SYSTEM_TARGET)
    elsewhere = [line for line in recipe_lines if line.lstrip('@-+').strip() not in system_recipe]
    assert recipe_lines and not elsewhere, (
        f'the client set is used outside {SYSTEM_TARGET}: {elsewhere or "(nowhere at all)"}'
    )


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
# The bandit release _bandit_scanned_files was cross-checked against (tj-vhboky.74). A bandit bump
# in uv.lock reds test_the_locked_bandit_is_the_modelled_one on the same diff, which is the only
# signal that the model may have gone stale: bandit is not installed where this runs.
MODELLED_BANDIT_VERSION = '1.9.4'
BANDIT_DISTRIBUTION = 'bandit'
_MAKE_REFERENCE = re.compile(r'\$\((\w+)\)')
_WORKFLOW_ENV_REFERENCE = re.compile(r'\$\{\{\s*env\.(\w+)\s*\}\}')


def _run_lines(run: str) -> list[str]:
    """The logical lines of a `run:` script: continuations folded, blanks and comment lines dropped."""
    folded = run.replace('\\\n', ' ')
    return [line.strip() for line in folded.splitlines() if line.strip() and not line.strip().startswith('#')]


def _runs(scanner: str, words: list[str]) -> bool:
    return any(PurePosixPath(word).name == scanner for word in words)


def _makefile_scanner_invocation(scanner: str) -> list[str]:
    """The security target's `scanner` command as argv, with every $(VAR) expanded from the Makefile.

    Fails closed: a renamed or emptied target raises in _make_recipe, and a recipe with no line (or
    more than one line) running `scanner` raises here, so parity is never judged on an empty argv.
    """
    lines = [line for line in _make_recipe('security') if _runs(scanner, shlex.split(line))]
    assert len(lines) == 1, f'expected one {scanner} line in the security target, found {lines}'
    return shlex.split(_MAKE_REFERENCE.sub(lambda match: _make_variable(match.group(1)), lines[0]))


def _workflow_scanner_invocations(scanner: str) -> dict[str, list[str]]:
    """Every `scanner` command in every workflow, as argv with ${{ env.X }} expanded, keyed by where it is."""
    found = {}
    for path in _workflow_files():
        document = _load_yaml(path) or {}
        for job_id, job in (document.get('jobs') or {}).items():
            for index, step in enumerate((job or {}).get('steps') or []):
                env = {**(document.get('env') or {}), **(job.get('env') or {}), **(step.get('env') or {})}
                for line in _run_lines(step.get('run') or ''):
                    expanded = _WORKFLOW_ENV_REFERENCE.sub(lambda match, env=env: str(env[match.group(1)]), line)
                    words = shlex.split(expanded)
                    if _runs(scanner, words):
                        found[f'{path.name} {job_id} step {index} ({step.get("name")})'] = words
    return found


def _makefile_bandit_invocation() -> list[str]:
    return _makefile_scanner_invocation('bandit')


def _workflow_bandit_invocations() -> dict[str, list[str]]:
    return _workflow_scanner_invocations('bandit')


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


def _locked_versions(lockfile: Path, distribution: str) -> list[str | None]:
    """Every version `lockfile` locks for `distribution`, read as TOML; bandit itself is never run."""
    with lockfile.open('rb') as handle:
        lock = tomllib.load(handle)
    name = canonicalize_name(distribution)
    return [package.get('version') for package in lock.get('package', []) if canonicalize_name(package['name']) == name]


def _bandit_model_drift(lockfile: Path) -> str | None:
    """Why the bandit locked in `lockfile` is not the one the discovery model follows, or None if it is."""
    versions = _locked_versions(lockfile, BANDIT_DISTRIBUTION)
    if versions == [MODELLED_BANDIT_VERSION]:
        return None
    return (
        f'{lockfile.name} locks {BANDIT_DISTRIBUTION} {versions}, but _bandit_scanned_files models bandit '
        f'{MODELLED_BANDIT_VERSION} (MODELLED_BANDIT_VERSION). Before changing the constant, re-run the '
        f'cross-check under the new bandit: its own BanditManager.discover_files against _bandit_scanned_files, '
        f"from the repository root, on both exclude forms ('*/tests/*' and 'tests/'), and get the same file set. "
        f'Update the model if discovery changed, then the constant, and put the evidence in the commit body.'
    )


@pytest.mark.build_infra
def test_the_locked_bandit_is_the_modelled_one():
    """tj-vhboky.74: a bandit bump in uv.lock reds the gate until the discovery model is re-checked.

    The scanned-file-set tests above trust a model of bandit 1.9.4, because bandit is not in the
    gate's venv and a skip is forbidden. The model's one blind spot is a bandit release that changes
    discovery; this turns that release into a red on the same diff that locks it.
    """
    drift = _bandit_model_drift(LOCKFILE)
    assert drift is None, drift


@pytest.mark.build_infra
def test_the_bandit_tripwire_fires_on_a_different_locked_version(tmp_path: Path):
    """Guard the guard: a copy of uv.lock with bandit at another version is reported, naming it."""
    lock = LOCKFILE.read_text(encoding='utf-8')
    entry = re.compile(rf'(^name = "{BANDIT_DISTRIBUTION}"\nversion = ")([^"]+)(")', re.MULTILINE)
    assert len(entry.findall(lock)) == 1, f'{LOCKFILE.name} does not hold exactly one {BANDIT_DISTRIBUTION} entry'
    bumped = tmp_path / LOCKFILE.name
    bumped.write_text(entry.sub(r'\g<1>999.0.0\g<3>', lock), encoding='utf-8')
    drift = _bandit_model_drift(bumped)
    assert drift is not None, 'a lock with bandit 999.0.0 passed the tripwire'
    assert '999.0.0' in drift and MODELLED_BANDIT_VERSION in drift, drift


# ---------------------------------------------------------------------------------------
# SEMGREP'S GATE (tj-vhboky.18, tj-1mtrlh.2, tj-cg2i9p)
#
# Without --error semgrep exits 0 whatever it finds, so the CI step and `make security` only proved
# that semgrep ran; 23 blocking findings sat under a green tick (tj-cg2i9p). Two properties keep the
# gate real, and each catches what the other cannot:
#   - parity: CI runs exactly the Makefile's semgrep argv, so neither side can narrow its scan (an
#     extra --exclude) or drop the gate flag alone. Parity passes when BOTH sides drop --error.
#   - the gate flag: the Makefile's (and so, with parity, CI's) invocation carries --error. It
#     passes when one side adds an --exclude the other lacks.
# The flag list is not pinned as a literal (tj-1mtrlh.2 item 4): the sources must agree, and the
# one flag whose absence makes the gate vacuous must be present.
SEMGREP = 'semgrep'
SEMGREP_GATE_FLAG = '--error'


def _makefile_semgrep_invocation() -> list[str]:
    return _makefile_scanner_invocation(SEMGREP)


def _workflow_semgrep_invocations() -> dict[str, list[str]]:
    """Every workflow semgrep command; fails closed when no workflow step runs semgrep at all."""
    found = _workflow_scanner_invocations(SEMGREP)
    assert found, 'no workflow step runs semgrep, so CI scans nothing and a Makefile parity check would be vacuous'
    return found


def _semgrep_arguments(invocation: list[str]) -> list[str]:
    return invocation[next(i for i, word in enumerate(invocation) if PurePosixPath(word).name == SEMGREP) + 1 :]


def _carries_gate_flag(invocation: list[str]) -> bool:
    return any(
        word == SEMGREP_GATE_FLAG or word.startswith(f'{SEMGREP_GATE_FLAG}=') for word in _semgrep_arguments(invocation)
    )


@pytest.mark.build_infra
def test_the_ci_semgrep_invocation_is_the_makefiles():
    """tj-1mtrlh.2 item 2: CI's semgrep command equals `make security`'s, flags and target both.

    Compared as argv, mirroring the bandit parity test, so a quoting-only difference is not a
    failure and a flag, exclude or target changed on one side is.
    """
    expected = _makefile_semgrep_invocation()
    invocations = _workflow_semgrep_invocations()
    drifted = {where: words for where, words in invocations.items() if words != expected}
    assert not drifted, (
        f'the workflow runs semgrep differently from the Makefile security target ({shlex.join(expected)}): '
        f'{ {where: shlex.join(words) for where, words in drifted.items()} }. Change both or neither.'
    )


@pytest.mark.build_infra
@pytest.mark.parametrize('source', ['Makefile', 'workflow'])
def test_semgrep_fails_on_findings(source: str):
    """tj-vhboky.18: every semgrep invocation carries --error, so a finding exits non-zero.

    Without it semgrep exits 0 on any number of findings and the gate is green by construction
    (tj-cg2i9p). The parity test cannot see this when both sides drop the flag together.
    """
    invocations = (
        {'Makefile security target': _makefile_semgrep_invocation()}
        if source == 'Makefile'
        else _workflow_semgrep_invocations()
    )
    ungated = {where: shlex.join(words) for where, words in invocations.items() if not _carries_gate_flag(words)}
    assert not ungated, (
        f'semgrep runs without {SEMGREP_GATE_FLAG}, so it exits 0 whatever it finds: {ungated}. '
        f'Put {SEMGREP_GATE_FLAG} back on both the Makefile and the workflow line.'
    )


# ---------------------------------------------------------------------------------------
# THE SYSTEM TESTING JOB (Sys-5 tj-vhboky.52, pinned by Sys-6 tj-vhboky.53)
#
# The job brings the stack up from docker-compose.yaml alone, migrates it to head and proves the
# recorded revision is the single head, checks one container-level env value, runs the system
# suite through `make test-system`, then checks the instance write secret's lifecycle. Every
# property below fails silently when lost: a job that swallows the suite's exit status, loads an
# overlay, downgrades a database or prints a secret before masking it still shows a green tick.
#
# Read from the parsed YAML. Inside a `run:` script the shell text is judged line by line, with
# comment lines dropped, because a comment is the one place a forbidden word may legitimately
# appear -- the Migrate step's comment explains why it runs no downgrade.
#
# Already pinned elsewhere and not repeated here: `make test-system` refuses without the
# disposable-database attestation (test_test_system_refuses_without_the_disposable_database_attestation,
# a real make run) and the PR gate never collects tests/system (test_the_gate_does_not_collect_a_test_
# under_tests_system, a real pytest --collect-only).
SYSTEM_JOB_NAME = 'System Testing'
STAGE_STEP = 'Stage Pipeline Configs'
MIGRATE_STEP = 'Migrate Database'
SYSTEM_TESTS_STEP = 'System Tests'
LIFECYCLE_STEP = 'Instance Secret Lifecycle'
DUMP_STEP = 'Dump Container Logs'
STOP_STEP = 'Stop System'
BUILD_CLIENT_STEP = 'Build Test Client'
SMOKE_STEP = 'Smoke Test'
LOCKDOWN_STEP = 'Check Network Lockdown'
# tj-3mk3u5.49 (T3a): data_ingest's gRPC bind name resolves to its ingest_store address alone. After
# the stack is up; its script's pass/fail logic is exercised in test_grpc_bind_network.py.
GRPC_BIND_STEP = 'Check gRPC Bind Network'
# tj-3mk3u5.25 (T3b): data_store calls data_ingest's gRPC health over ingest_store and needs SERVING.
# After the bind check and before the lockdown; stack-only. Its script, and the lockdown step's gRPC
# half, are run under bash with docker stubbed in test_grpc_peer_reach.py.
PEER_STEP = 'Check gRPC Peer Reach'
START_STEP = 'Start System'
# tj-irhy0a.1 / tj-irhy0a.2: the fake-mode banner check, and the head seed's dump and upload.
FAKE_CHECK_STEP = 'Check Fake Broker'
SEED_DUMP_STEP = 'Seed Dump'
UPLOAD_SEED_STEP = 'Upload Head Seed'
SYSTEM_JOB_STEP_ORDER = (
    STAGE_STEP,
    BUILD_CLIENT_STEP,
    START_STEP,
    FAKE_CHECK_STEP,
    MIGRATE_STEP,
    SMOKE_STEP,
    GRPC_BIND_STEP,
    PEER_STEP,
    LOCKDOWN_STEP,
    'Check Container Env',
    SYSTEM_TESTS_STEP,
    # After the suite (decision tj-vhboky.55) and BEFORE the lifecycle step, which blanks the write
    # secret the producer authenticates with (architect note of 04:45 UTC 2026-09-30 on tj-irhy0a.1).
    SEED_DUMP_STEP,
    UPLOAD_SEED_STEP,
    LIFECYCLE_STEP,
    DUMP_STEP,
    STOP_STEP,
)
# The fake-mode overlay (decision tj-j4wknb R4; tj-vhboky.61). In System Testing it is loaded with the
# base file, in that order, by every `up` -- a container created without it runs data_ingest on the
# production entrypoint (tj-irhy0a.1 item 1).
FAKE_OVERLAY_FILE = REPO_ROOT / 'docker-compose.fake.yaml'
CONTAINER_CREATING_SUBCOMMANDS = frozenset({'up', 'create'})
# The steps that may, and must, carry SYSTEM_TEST_DISPOSABLE_DB=1: each runs a make target behind the
# disposable-database guard (test-system, system-launch, seed-dump), and nothing else may inherit it.
ATTESTING_STEPS = frozenset({SYSTEM_TESTS_STEP, START_STEP, SEED_DUMP_STEP})
# Steps that must reach the stack, so an empty compose-invocation list cannot pass the file check.
SYSTEM_JOB_COMPOSE_STEPS = (
    START_STEP,
    FAKE_CHECK_STEP,
    MIGRATE_STEP,
    GRPC_BIND_STEP,
    PEER_STEP,
    'Check Container Env',
    LIFECYCLE_STEP,
    DUMP_STEP,
    STOP_STEP,
)
# Steps that drive the stack and nothing else: they must never load the client file, so starting,
# migrating, inspecting and stopping the stack cannot depend on it.
SYSTEM_JOB_STACK_ONLY_STEPS = (
    START_STEP,
    FAKE_CHECK_STEP,
    MIGRATE_STEP,
    GRPC_BIND_STEP,
    PEER_STEP,
    'Check Container Env',
    DUMP_STEP,
    STOP_STEP,
)
# Steps that must send at least one request from test_client (tj-q9ae5u addendum 1 item 5').
SYSTEM_JOB_CLIENT_STEPS = (BUILD_CLIENT_STEP, SMOKE_STEP, LOCKDOWN_STEP, LIFECYCLE_STEP)
# The one compose subcommands a client invocation may run.
CLIENT_SUBCOMMANDS = frozenset({'build', 'run'})
CLIENT_RUN_OPTIONS = frozenset({'--rm', '--no-deps', '-T'})
IMAGE_BUILD_JOB_NAME = 'Image Build'
RENDER_STEP = 'Check Compose Renders'
# The Makefile's compose sets the render check must cover, each in the Makefile's own file order.
# Re-pinned for tj-c4mosr.5: the agent-stack set (tj-c4mosr.8) and the MCP's own project (tj-c4mosr.4,
# ADR tj-4rr0la addendum 11 R5 (c)) are rendered too, each under the project the Makefile gives it.
RENDERED_COMPOSE_VARIABLES = (
    'PROD_COMPOSE',
    'DEV_COMPOSE',
    'TOOLS_COMPOSE',
    TEST_CLIENT_COMPOSE_VARIABLE,
    'AGENT_STACK_COMPOSE',
    'AGENT_MCP_COMPOSE',
    # The fake-mode stack make system-launch starts (tj-vhboky.61), default project. Its render line,
    # and the fake overlay as the agent-stack line's fourth file, landed with tj-irhy0a.24.
    'SYSTEM_COMPOSE',
)
TEARDOWN_CONDITIONS = frozenset({'failure()', 'always()'})
# The staged project env file the job writes and reads back. Named through the template's stem so
# this module never spells a path that holds real credentials.
STAGED_ENV_FILE = ENV_DEFAULT_FILE.stem
# The write routes the lifecycle step must refuse with the secret blank, and how it probes each.
LIFECYCLE_ROUTE_COUNT = 3
LIFECYCLE_HEADER_VARIANTS = 4
# docker compose global options that take a value, so the subcommand can be told from a value.
_COMPOSE_VALUE_OPTIONS = frozenset(
    {'-f', '--file', '-p', '--project-name', '--env-file', '--profile', '--project-directory', '--ansi', '--progress'}
)
_COMPOSE_INVOCATION = re.compile(r'\bdocker(?:\s+compose|-compose)(?=\s|$)')
_SWALLOWED_STATUS = re.compile(r'\|\|\s*(?:true|:)\s*(?:$|[;)}])|\|\|\s*exit\s+0\b|\bset\s+\+e\b')
_BROKER_NAME = re.compile(r'\b(?:' + '|'.join(re.escape(prefix) for prefix in BROKER_SECRET_PREFIXES) + r')\w+')
_GENERATED_VALUE = re.compile(r'^(\w+)="?\$\(openssl rand\b')
_FUNCTION_HEAD = re.compile(r'^(\w+)\(\)\s*\{$')
_CAPTURED_CALL = re.compile(r'^(\w+)="\$\((\w+) (\w+)\)"$')


MIGRATION_VERSIONS_DIR = REPO_ROOT / 'data' / 'store' / 'migrations' / 'versions'
# What common/database/postgres_tools.py printed at import before tj-ijpys9.20: stray stdout of
# the kind awk's first field would have read as revisions, had anything in alembic's chain imported it.
_IMPORT_TIME_STDOUT = 'Postgres async is enabled.\nPostgres sync is enabled.\n'


def _migration_revision_ids() -> list[str]:
    """Every `revision` a migration script declares -- the ids alembic heads/current print."""
    ids = []
    for path in sorted(MIGRATION_VERSIONS_DIR.glob('*.py')):
        for node in ast.parse(path.read_text(encoding='utf-8')).body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
            elif isinstance(node, ast.AnnAssign):
                target = node.target
            else:
                continue
            if isinstance(target, ast.Name) and target.id == 'revision' and isinstance(node.value, ast.Constant):
                ids.append(node.value.value)
    assert len(ids) >= 2, f'found {ids} in {MIGRATION_VERSIONS_DIR}; the id checks below need at least two'
    return ids


def _grep_invocation(command: list[str]) -> tuple[list[str], str] | None:
    """A grep simple command as (its option words, its pattern), or None if it is not grep."""
    words = [word for word in command if word not in _SHELL_KEYWORDS]
    if not words or PurePosixPath(words[0]).name != 'grep':
        return None
    flags, position = [], 1
    while position < len(words) and words[position].startswith('-') and words[position] != '--':
        if words[position] in ('-e', '--regexp'):
            return flags, words[position + 1] if position + 1 < len(words) else ''
        flags.append(words[position])
        position += 1
    if position < len(words) and words[position] == '--':
        position += 1
    return (flags, words[position]) if position < len(words) else None


def _grep_letters(flags: list[str]) -> str:
    """The short-option letters among grep option words: ['-Eo'] -> 'Eo'."""
    return ''.join(flag[1:] for flag in flags if flag.startswith('-') and not flag.startswith('--'))


def _grep(flags: list[str], pattern: str, text: str) -> str:
    """Run the real grep with a workflow's own options and pattern; exit 1 (no match) is an answer."""
    assert shutil.which('grep'), 'grep is not on PATH, so the workflow regex cannot be exercised'
    result = subprocess.run(['grep', *flags, '--', pattern], input=text, capture_output=True, text=True, check=False)
    assert result.returncode in (0, 1), f'grep {flags} {pattern!r} failed: {result.stderr}'
    return result.stdout


def _assert_extracts_revision_ids(flags: list[str], pattern: str, ids: list[str]) -> None:
    """The extractor yields exactly the id of every line an id LEADS, as a whole word, and nothing else."""
    last = ids[-1]
    cases = {f'`{revision} (head)`': (f'{revision} (head)\n', f'{revision}\n') for revision in ids}
    cases |= {
        'import-time prints ahead of the head line': (f'{_IMPORT_TIME_STDOUT}{last} (head)\n', f'{last}\n'),
        'an id that does not lead its line': (f'Rev: {last} (head)\n', ''),
        'a longer hex word': (f'{last}0 (head)\n', ''),
        'no revision at all': ('\n', ''),
    }
    wrong = {name: got for name, (text, want) in cases.items() if (got := _grep(flags, pattern, text)) != want}
    assert not wrong, f'grep {" ".join(flags)} {pattern!r} is not a revision-id extractor; it yields {wrong}'


def _assert_counts_revision_ids(flags: list[str], pattern: str, ids: list[str]) -> None:
    """The counter counts revision ids, one per line, and never a line that is not one."""
    cases = {
        'one id': (f'{ids[0]}\n', '1'),
        'every id': (''.join(f'{revision}\n' for revision in ids), str(len(ids))),
        'nothing (printf of an empty heads)': ('\n', '0'),
        'stray stdout beside one id': (f'{_IMPORT_TIME_STDOUT}{ids[0]}\n', '1'),
        'stray stdout alone': (_IMPORT_TIME_STDOUT, '0'),
    }
    wrong = {name: got for name, (text, want) in cases.items() if (got := _grep(flags, pattern, text).strip()) != want}
    assert not wrong, f'grep {" ".join(flags)} {pattern!r} does not count revision ids; it counts {wrong}'


def _system_job() -> dict:
    jobs = (_load_yaml(TESTING_WORKFLOW) or {}).get('jobs') or {}
    matches = [job for job in jobs.values() if (job or {}).get('name') == SYSTEM_JOB_NAME]
    assert len(matches) == 1, (
        f'expected one job named {SYSTEM_JOB_NAME!r} in {TESTING_WORKFLOW.name}, found {len(matches)}'
    )
    return matches[0]


def _system_steps() -> list[dict]:
    steps = _system_job().get('steps') or []
    assert steps, f'the {SYSTEM_JOB_NAME} job has no steps'
    return steps


def _system_step(name: str) -> dict:
    matches = [step for step in _system_steps() if step.get('name') == name]
    assert len(matches) == 1, f'expected one {name!r} step in {SYSTEM_JOB_NAME}, found {len(matches)}'
    return matches[0]


def _step_lines(step: dict) -> list[str]:
    return _run_lines(step.get('run') or '')


def _compose_files(line: str) -> list[list[str]]:
    """For each docker compose invocation on a line, the compose files it names with -f/--file."""
    return [files for files, _ in _compose_calls(line)]


@pytest.mark.build_infra
def test_system_job_steps_run_in_order_with_teardown_last():
    """tj-vhboky.52 items 6 and 7: the suite before the lifecycle, the log dump before the stop.

    The lifecycle step must follow the suite so its log scan sees every request the suite made;
    Dump Container Logs (on failure) must precede Stop System (always), whose `down -v` destroys
    the containers the dump reads. Everything after the lifecycle step is teardown, and nothing
    before it is conditional, so no check on the way can be skipped.

    tj-irhy0a.2 re-pin (tj-irhy0a.1): Check Fake Broker DIRECTLY follows Start System, so nothing talks
    to a stack whose data_ingest has not been shown to be the fake; Seed Dump then Upload Head Seed
    sit after System Tests and before the lifecycle step, whose blanked secret the producer would
    fail on (exit 1).
    """
    steps = _system_steps()
    names = [step.get('name') for step in steps]
    for name in SYSTEM_JOB_STEP_ORDER:
        assert names.count(name) == 1, f'{SYSTEM_JOB_NAME} has {names.count(name)} {name!r} step(s): {names}'
    positions = [names.index(name) for name in SYSTEM_JOB_STEP_ORDER]
    assert positions == sorted(positions), (
        f'{SYSTEM_JOB_NAME} steps are out of order: expected {list(SYSTEM_JOB_STEP_ORDER)} as a subsequence of {names}'
    )
    assert names[names.index(START_STEP) + 1] == FAKE_CHECK_STEP, (
        f'{FAKE_CHECK_STEP} must directly follow {START_STEP}: {names}'
    )
    lifecycle = names.index(LIFECYCLE_STEP)
    assert names[lifecycle + 1 : lifecycle + 3] == [DUMP_STEP, STOP_STEP], (
        f'{DUMP_STEP} then {STOP_STEP} must directly follow {LIFECYCLE_STEP}: {names}'
    )
    assert _system_step(DUMP_STEP).get('if') == 'failure()', f'{DUMP_STEP} must run on failure only'
    assert _system_step(STOP_STEP).get('if') == 'always()', f'{STOP_STEP} must always run'
    conditional = [step.get('name') for step in steps[: lifecycle + 1] if 'if' in step]
    assert not conditional, f'steps up to {LIFECYCLE_STEP} must be unconditional: {conditional}'
    not_teardown = [step.get('name') for step in steps[lifecycle + 1 :] if step.get('if') not in TEARDOWN_CONDITIONS]
    assert not not_teardown, f'steps after {LIFECYCLE_STEP} must be failure() or always() teardown: {not_teardown}'
    stop = names.index(STOP_STEP)
    late_docker = [
        step.get('name')
        for step in steps[stop + 1 :]
        if any('docker' in shlex.split(line, comments=True) for line in _step_lines(step))
    ]
    assert not late_docker, f'steps after {STOP_STEP} still drive docker: {late_docker}'


@pytest.mark.build_infra
@pytest.mark.parametrize('workflow', _workflow_files(), ids=lambda p: p.name)
def test_no_workflow_runs_a_downgrade(workflow: Path):
    """tj-vhboky.53 (5), user ruling tj-vhboky.47: no workflow downgrades a database.

    A downgrade over the empty database the job has just migrated proves nothing; the round trip
    with data moved to the fake-broker PR (epic tj-irhy0a). Every run line of every step is
    judged, comments excepted, so a downgrade through alembic, run_migrations.sh or make is caught.
    """
    document = _load_yaml(workflow) or {}
    offenders = [
        f'{job_id} / {step.get("name")}: {line}'
        for job_id, job in (document.get('jobs') or {}).items()
        for step in (job or {}).get('steps') or []
        for line in _step_lines(step)
        if 'downgrade' in line.lower()
    ]
    assert not offenders, f'{workflow.name} runs a downgrade: {offenders}'


@pytest.mark.build_infra
def test_no_makefile_recipe_runs_a_downgrade():
    """tj-vhboky.53: the host must never downgrade a real database, so no make target can."""
    offenders = [
        line.strip()
        for line in MAKEFILE.read_text(encoding='utf-8').replace('\\\n', ' ').splitlines()
        if line.startswith('\t') and not line.strip().startswith('#') and 'downgrade' in line.lower()
    ]
    assert not offenders, f'{MAKEFILE.name} has recipe lines that downgrade: {offenders}'


@pytest.mark.build_infra
def test_system_job_migrates_only_up_to_head_and_asserts_current_is_head():
    """tj-vhboky.52 item 3: upgrade head, then the recorded revision equals the single head.

    alembic runs only in the Migrate step. Its one mutating command is `upgrade head`; the rest
    read (`heads`, `current`), through the step's wrapper function, and the step compares them.

    tj-ijpys9.20 re-pin: "exactly one head" is a count of revision IDS, not of lines. The wrapper
    extracts ids with a grep -o over a revision-id regex (never awk's first field, which took any
    stray stdout line for a revision), and the head count is a grep -c of ids in ${heads}, then
    `-ne 1`. Both regexes are run through grep itself against the real migration ids.
    """
    offenders = [
        f'{step.get("name")}: {line}'
        for step in _system_steps()
        if step.get('name') != MIGRATE_STEP
        for line in _step_lines(step)
        for command in _commands(line)
        if _alembic_subcommand(command) is not None
    ]
    assert not offenders, f'alembic runs outside {MIGRATE_STEP}: {offenders}'

    lines = _step_lines(_system_step(MIGRATE_STEP))
    wrappers, upgrades, others = set(), [], []
    bodies: dict[str, list[list[str]]] = {}
    current_function = None
    for line in lines:
        if head := _FUNCTION_HEAD.match(line):
            current_function = head.group(1)
            continue
        if line == '}':
            current_function = None
            continue
        if current_function:
            bodies.setdefault(current_function, []).extend(_commands(line))
        for command in _commands(line):
            subcommand = _alembic_subcommand(command)
            if subcommand is None:
                continue
            if subcommand == '$1' and current_function:
                wrappers.add(current_function)
            elif subcommand == 'upgrade':
                upgrades.append(command[command.index('upgrade') + 1 :])
            else:
                others.append(' '.join(command))
    assert upgrades == [['head']], f'{MIGRATE_STEP} must run exactly one `upgrade head`, found {upgrades}'
    assert not others, f'{MIGRATE_STEP} runs alembic subcommands other than upgrade head: {others}'
    assert wrappers, f'{MIGRATE_STEP} has no function wrapping `alembic "$1"`, so it reads neither heads nor current'

    captured = {
        match.group(1): match.group(3)
        for line in lines
        if (match := _CAPTURED_CALL.match(line)) and match.group(2) in wrappers
    }
    assert sorted(captured.values()) == ['current', 'heads'], (
        f'{MIGRATE_STEP} must read alembic heads and current through its wrapper, and nothing else: {captured}'
    )
    by_role = {role: variable for variable, role in captured.items()}
    heads, current = by_role['heads'], by_role['current']
    comparison = re.compile(rf'\[\s*"\$\{{(?:{current}|{heads})\}}"\s*!=\s*"\$\{{(?:{current}|{heads})\}}"\s*\]')
    compared = [line for line in lines if comparison.search(line) and current in line and heads in line]
    assert compared, f'{MIGRATE_STEP} never compares ${{{current}}} with ${{{heads}}}'

    revision_ids = _migration_revision_ids()
    for wrapper in sorted(wrappers):
        body = bodies.get(wrapper, [])
        awk = [' '.join(command) for command in body if _command_name(command) == 'awk']
        assert not awk, f'{wrapper}() still reads revisions with awk, so any stray stdout line is one: {awk}'
        extractors = [grep for command in body if (grep := _grep_invocation(command)) and 'o' in _grep_letters(grep[0])]
        assert len(extractors) == 1, (
            f'{wrapper}() must extract revision ids with one `grep -o` over a revision-id regex, found {extractors}'
        )
        _assert_extracts_revision_ids(*extractors[0], revision_ids)

    counters = {
        match.group(1): grep
        for line in lines
        if (match := re.match(r'^(\w+)="\$\((.*)\)"$', line)) and f'${{{heads}}}' in match.group(2)
        for command in _commands(match.group(2))
        if (grep := _grep_invocation(command)) and 'c' in _grep_letters(grep[0])
    }
    assert len(counters) == 1, (
        f'{MIGRATE_STEP} must count the ids in ${{{heads}}} with one `grep -c` over a revision-id regex, found {counters}'
    )
    ((count, (flags, pattern)),) = counters.items()
    _assert_counts_revision_ids(flags, pattern, revision_ids)
    single_head = [line for line in lines if re.search(rf'\[\s*"\$\{{{count}\}}"\s+-ne\s+1\s*\]', line)]
    assert single_head, f'{MIGRATE_STEP} does not assert exactly one migration head (`[ "${{{count}}}" -ne 1 ]`)'
    by_lines = [line for line in lines if heads in line and re.search(r'\bwc\b', line)]
    assert not by_lines, f'{MIGRATE_STEP} still counts ${{{heads}}} by lines, not ids: {by_lines}'


@pytest.mark.build_infra
def test_system_job_loads_the_stack_alone_and_the_client_only_as_a_pair():
    """N5 re-pin of the tj-vhboky.53 re-scope, after N4 (tj-q9ae5u addendum 1 item 5').

    Two spellings and only two. The STACK: docker-compose.yaml alone -- a bare `docker compose`
    would also load the dev override, and any other overlay is one the job must not have. The
    CLIENT: docker-compose.yaml then docker-compose.test-client.yaml, and only ever to build or run
    test_client, a run always removed on exit, with no deps and no TTY. So anything that starts,
    blanks, recreates, inspects or stops the stack names the base file alone, and the stack-only
    steps never load the client file at all. COMPOSE_FILE would do all this behind the flags.

    tj-irhy0a.2 RE-PIN (tj-irhy0a.1 item 1, decision tj-j4wknb R4): a THIRD spelling, the FAKE-MODE
    STACK -- docker-compose.yaml then docker-compose.fake.yaml, SYSTEM_COMPOSE's set -- and it is the
    only one an `up` (or `create`) may use. The job starts the stack through make system-launch, so a
    container created or recreated on the base file alone would bring data_ingest back on the
    production entrypoint; the lifecycle step's data_store recreate is the one `up` written out in
    the job, and it must carry the overlay. The base file alone stays the spelling for everything
    that creates no service container (run --no-deps of a one-off, exec, logs, ps, down); the overlay
    is loaded for nothing but `up`; the client pair is unchanged.
    """
    fake_stack = [COMPOSE_FILE.name, FAKE_OVERLAY_FILE.name]
    seen, client_steps, fake_ups, offenders = set(), set(), set(), []
    for step in _system_steps():
        name = step.get('name')
        for line in _step_lines(step):
            for files, rest in _compose_calls(line):
                seen.add(name)
                if files == fake_stack:
                    if rest[:1] != ['up']:
                        offenders.append(f'{name}: the fake-mode set runs {rest[:1]}, not up: {line}')
                    fake_ups.add(name)
                    continue
                if files == [COMPOSE_FILE.name]:
                    if rest[:1] and rest[0] in CONTAINER_CREATING_SUBCOMMANDS:
                        offenders.append(f'{name}: `{rest[0]}` on the base file alone drops the fake overlay: {line}')
                    continue
                if files != _client_file_pair():
                    offenders.append(f'{name}: -f {files} in {line}')
                    continue
                client_steps.add(name)
                service, options, _ = _compose_service(rest)
                if name in SYSTEM_JOB_STACK_ONLY_STEPS:
                    offenders.append(f'{name}: a stack-only step loads the client file: {line}')
                if (rest[:1] and rest[0] not in CLIENT_SUBCOMMANDS) or service != TEST_CLIENT_SERVICE:
                    offenders.append(
                        f'{name}: the client set runs {rest[:1]} on {service!r}, not build/run {TEST_CLIENT_SERVICE}: {line}'
                    )
                if rest[:1] == ['run'] and not set(options) >= CLIENT_RUN_OPTIONS:
                    offenders.append(f'{name}: a client run lacks {sorted(CLIENT_RUN_OPTIONS - set(options))}: {line}')
    missing = sorted(set(SYSTEM_JOB_COMPOSE_STEPS) - seen)
    assert not missing, f'no docker compose invocation found in {missing}, so this check saw less than the job runs'
    assert not offenders, f'{SYSTEM_JOB_NAME} loads compose files outside the three spellings: {offenders}'
    assert LIFECYCLE_STEP in fake_ups, f'{LIFECYCLE_STEP} recreates data_store without the fake-mode overlay'
    without_client = sorted(set(SYSTEM_JOB_CLIENT_STEPS) - client_steps)
    assert not without_client, f'{without_client} send nothing from {TEST_CLIENT_SERVICE}'

    document = _load_yaml(TESTING_WORKFLOW) or {}
    scopes = [document.get('env') or {}, _system_job().get('env') or {}]
    scopes += [step.get('env') or {} for step in _system_steps()]
    assert not any('COMPOSE_FILE' in scope for scope in scopes), 'COMPOSE_FILE is set for the System Testing job'
    assert not any('COMPOSE_FILE' in line for step in _system_steps() for line in _step_lines(step)), (
        'a System Testing step sets or reads COMPOSE_FILE'
    )


@pytest.mark.build_infra
def test_build_test_client_builds_only_the_client():
    """N4 item 3: one build of test_client from the job's checkout, over the client pair.

    Its place -- after Stage (the base file cannot interpolate without the staged POSTGRES_PASS),
    before Start System -- is pinned by SYSTEM_JOB_STEP_ORDER.
    """
    calls = [call for line in _step_lines(_system_step(BUILD_CLIENT_STEP)) for call in _compose_calls(line)]
    assert len(calls) == 1, f'{BUILD_CLIENT_STEP} must run one compose invocation, found {calls}'
    files, rest = calls[0]
    service, _, trailing = _compose_service(rest)
    assert files == _client_file_pair() and rest[:1] == ['build'] and service == TEST_CLIENT_SERVICE and not trailing, (
        f'{BUILD_CLIENT_STEP} must build {TEST_CLIENT_SERVICE} over {_client_file_pair()}: -f {files} {rest}'
    )


@pytest.mark.build_infra
def test_smoke_test_reaches_each_service_by_name_and_never_by_loopback():
    """N4 item 4: prod publishes no host port, so the Smoke Test uses none.

    data_store is asked from test_client (store_api, the strategy clients' path) and data_ingest
    from inside data_store (proving ingest_store carries HTTP), each at http://<service>:
    ${APP_INTERNAL_PORT}. A loopback URL here would test a topology prod does not have -- and
    would only pass at all if some overlay put the publishes back.
    """
    lines = _step_lines(_system_step(SMOKE_STEP))
    loopback = [line for line in lines if _LOOPBACK.search(line)]
    assert not loopback, f'{SMOKE_STEP} still uses a loopback address: {loopback}'
    hosts = {host for line in lines for host in re.findall(r'http://([\w.-]+):\$\{APP_INTERNAL_PORT\}', line)}
    assert hosts == {'data_store', 'data_ingest'}, (
        f'{SMOKE_STEP} requests {sorted(hosts)} by name, expected data_store and data_ingest'
    )

    calls = [call for line in lines for call in _compose_calls(line)]
    client_curl = [
        rest
        for files, rest in calls
        if files == _client_file_pair()
        and rest[:1] == ['run']
        and _compose_service(rest)[0] == TEST_CLIENT_SERVICE
        and 'curl' in _compose_service(rest)[1]
    ]
    assert client_curl, f'{SMOKE_STEP} sends no curl from {TEST_CLIENT_SERVICE}'
    from_data_store = [
        rest for files, rest in calls if files == [COMPOSE_FILE.name] and rest[:1] == ['exec'] and 'data_store' in rest
    ]
    assert from_data_store, f'{SMOKE_STEP} sends nothing from inside data_store, so ingest_store HTTP is unproven'
    assert any('\'{"message":"pong"}\'' in line for line in lines) and any("'[]'" in line for line in lines), (
        f'{SMOKE_STEP} lost its exact-body assertions'
    )


@pytest.mark.build_infra
def test_network_lockdown_checks_non_resolution_and_no_egress():
    """N4 item 5: the network model as the RUNNING stack enforces it, not as the file declares it.

    - From test_client, a positive lookup of data_store comes first -- a client with no working DNS
      would otherwise make every negative pass -- then the kafka container name and data_ingest
      must NOT resolve, told apart from a failed run by getent's own not-found status, 2.
    - From postgres and from data_store, a TCP connect to a public LITERAL address (no DNS
      involved) must fail, told apart from a failed probe by a distinct status, 3.
    The name probed as kafka is the one kafka's container_name interpolates.

    tj-3mk3u5.25 extension (the T3a gate's N1): data_ingest's gRPC bind alias -- the host its
    environment names, read from the compose file -- is among the names test_client must not
    resolve, and the step also probes the gRPC port BY ADDRESS from test_client through the image
    interpreter, since the name will not resolve there. That half's pass and fail behaviour, its
    data_store positive control and its exit-3 convention are exercised by running the step itself in
    test_grpc_peer_reach.py.
    """
    lines = _step_lines(_system_step(LOCKDOWN_STEP))
    calls = [call for line in lines for call in _compose_calls(line)]
    getent = [
        rest
        for files, rest in calls
        if files == _client_file_pair()
        and rest[:1] == ['run']
        and _compose_service(rest)[0] == TEST_CLIENT_SERVICE
        and 'getent' in _compose_service(rest)[1]
    ]
    assert getent, f'{LOCKDOWN_STEP} resolves nothing from {TEST_CLIENT_SERVICE}'

    looked_up = [match.group(1) for line in lines for match in re.finditer(r'\blookup\s+"?([^")\s]+)"?\)', line)]
    loops = [match for line in lines if (match := re.match(r'^for (\w+) in (.+); do$', line))]
    assert looked_up[:1] == ['data_store'], f'{LOCKDOWN_STEP} must look up data_store first, looked up {looked_up}'
    negatives = {
        word
        for match in loops
        if f'lookup "${{{match.group(1)}}}"' in ' '.join(lines)
        for word in shlex.split(match.group(2))
    }
    kafka_name = (_load_yaml(COMPOSE_FILE)['services']['kafka'] or {}).get('container_name')
    kafka_service = 'kafka'
    assert kafka_name and kafka_name in negatives and 'data_ingest' in negatives, (
        f'{LOCKDOWN_STEP} must show {kafka_name} (kafka) and data_ingest do not resolve; it loops over {sorted(negatives)}'
    )
    assert kafka_service in negatives, (
        f'{LOCKDOWN_STEP} must also show the service name {kafka_service!r} does not resolve, not only the '
        f'container name {kafka_name!r}; it loops over {sorted(negatives)}'
    )
    grpc_alias = _compose_service_environment(COMPOSE_FILE, 'data_ingest').get('APP_INTERNAL_GRPC_HOST')
    assert grpc_alias, f'{COMPOSE_FILE.name} no longer names data_ingest APP_INTERNAL_GRPC_HOST'
    assert grpc_alias in negatives, (
        f'{LOCKDOWN_STEP} must show data_ingest gRPC bind alias {grpc_alias!r} does not resolve from '
        f'{TEST_CLIENT_SERVICE}; it loops over {sorted(negatives)}'
    )
    by_address = [
        rest
        for files, rest in calls
        if files == _client_file_pair()
        and rest[:1] == ['run']
        and _compose_service(rest)[0] == TEST_CLIENT_SERVICE
        and '/code/.venv/bin/python' in _compose_service(rest)[1]
    ]
    assert by_address, f'{LOCKDOWN_STEP} never probes data_ingest gRPC port by address from {TEST_CLIENT_SERVICE}'
    assert any(re.search(r'-eq 2\b', line) for line in lines), f'{LOCKDOWN_STEP} never requires getent not-found (2)'

    execs = {
        _compose_service(rest)[0] for files, rest in calls if files == [COMPOSE_FILE.name] and rest[:1] == ['exec']
    }
    assert {'postgres', 'data_store'} <= execs, (
        f'{LOCKDOWN_STEP} probes egress from {sorted(execs)}, expected postgres and data_store'
    )
    addresses = [match.group(1) for line in lines if (match := re.match(r'^PUBLIC_ADDRESS="?([^"\s]+)"?$', line))]
    assert len(addresses) == 1, f'{LOCKDOWN_STEP} must set one PUBLIC_ADDRESS literal, found {addresses}'
    assert ipaddress.ip_address(addresses[0]).is_global, f'{addresses[0]} is not a public literal address'
    assert any(re.search(r'-eq 3\b', line) for line in lines), f'{LOCKDOWN_STEP} never requires a refused connect (3)'


@pytest.mark.build_infra
def test_lifecycle_probes_go_from_the_client_by_service_name():
    """N4 item 7: only probe() changed -- each request is curl inside test_client to data_store by name.

    The scan, blank, recreate and route loop stay pinned by the lifecycle tests above; this pins
    where the request comes from, so a probe cannot drift back to a loopback port prod lacks.
    """
    lines = _step_lines(_system_step(LIFECYCLE_STEP))
    loopback = [line for line in lines if _LOOPBACK.search(line)]
    assert not loopback, f'{LIFECYCLE_STEP} still uses a loopback address: {loopback}'
    bases = [match.group(1) for line in lines if (match := re.match(r'^base="([^"]+)"$', line))]
    assert bases == ['http://data_store:${APP_INTERNAL_PORT}'], f'{LIFECYCLE_STEP} probes base {bases}'
    sends = [
        line
        for line in lines
        for files, rest in _compose_calls(line)
        if files == _client_file_pair()
        and _compose_service(rest)[0] == TEST_CLIENT_SERVICE
        and 'curl' in _compose_service(rest)[1]
        and '${base}${path}' in line
    ]
    assert len(sends) == 1, f'probe() must send "${{base}}${{path}}" by curl from {TEST_CLIENT_SERVICE}, found {sends}'


def _image_build_job() -> dict:
    jobs = (_load_yaml(TESTING_WORKFLOW) or {}).get('jobs') or {}
    matches = [job for job in jobs.values() if (job or {}).get('name') == IMAGE_BUILD_JOB_NAME]
    assert len(matches) == 1, f'expected one {IMAGE_BUILD_JOB_NAME!r} job, found {len(matches)}'
    return matches[0]


@pytest.mark.build_infra
def test_image_build_renders_every_compose_set_quietly():
    """N4 item 8: `config --quiet` over every compose set the Makefile names, after Build Images.

    Each set exactly as the Makefile spells it -- the files in order AND the project -- so the check
    cannot drift from what make loads. A broken override or tools file otherwise surfaces only on a
    developer's machine. ALWAYS quiet: without it `config` prints the interpolated model, POSTGRES_PASS
    inside DATABASE_URI included. Re-pinned for tj-c4mosr.5: the agent-stack set under
    trader_joe_agent_stack and the MCP's file under trader_joe_agent_mcp are rendered too; a
    workflow line that drops its -p renders a different project and goes red.
    """
    steps = _image_build_job().get('steps') or []
    names = [step.get('name') for step in steps]
    assert names.count(RENDER_STEP) == 1 and 'Build Images' in names, f'{IMAGE_BUILD_JOB_NAME} steps: {names}'
    assert names.index('Build Images') < names.index(RENDER_STEP), (
        f'{RENDER_STEP} must follow Build Images (env files staged)'
    )
    step = steps[names.index(RENDER_STEP)]
    assert 'if' not in step and 'continue-on-error' not in step, f'{RENDER_STEP} must be unconditional and blocking'

    calls = [call for line in _step_lines(step) for call in _compose_calls(line)]
    loud = [rest for _, rest in calls if rest[:1] != ['config'] or not {'--quiet', '-q'} & set(rest)]
    assert not loud, f'{RENDER_STEP} runs compose other than `config --quiet`: {loud}'
    env = _subprocess_env()
    expected = []
    for name in RENDERED_COMPOSE_VARIABLES:
        expanded = _expanded_make_variable(name, REPO_ROOT, env)
        (files, rest), project = _compose_calls(expanded)[0], _compose_projects(expanded)[0]
        assert not rest, f'{name} is {expanded!r}; a compose set names files, not a subcommand'
        # AGENT_MCP_COMPOSE names its file under the root checkout's absolute path; CI renders from
        # the checkout root, so the file's name is what the two must agree on.
        expected.append((tuple(PurePosixPath(file).name if file.startswith('/') else file for file in files), project))
    rendered = [
        (tuple(files), project)
        for line in _step_lines(step)
        for (files, _), project in zip(_compose_calls(line), _compose_projects(line), strict=True)
    ]
    assert sorted(rendered, key=str) == sorted(expected, key=str), (
        f'{RENDER_STEP} renders {rendered}, expected the Makefile sets {dict(zip(RENDERED_COMPOSE_VARIABLES, expected, strict=True))}'
    )


@pytest.mark.build_infra
def test_every_compose_render_failure_fails_the_step():
    """tj-irhy0a.24: a render that fails, fails Check Compose Renders, whichever line it is.

    The pin above keeps the step unconditional and without continue-on-error, which says nothing
    about the lines inside it: one `|| true` or `set +e` lets a broken compose set through as a
    green step, and without errexit only the closing echo's status would be the step's.
    """
    lines = _step_lines(next(step for step in _image_build_job().get('steps') or [] if step.get('name') == RENDER_STEP))
    first_render = next(index for index, line in enumerate(lines) if _compose_calls(line))
    errexit = [line for line in lines[:first_render] if re.match(r'^set\s+-\w*e', line)]
    assert errexit, f'{RENDER_STEP} does not set errexit before its first render'
    swallowed = [line for line in lines if _SWALLOWED_STATUS.search(line)]
    assert not swallowed, f'{RENDER_STEP} swallows a failure: {swallowed}'


@pytest.mark.build_infra
def test_the_agent_stack_render_names_the_env_files_build_images_staged():
    """Each *_ENV_FILE placeholder on the agent-stack render line names a staged file.

    tj-c4mosr.5 (01:50 item 2, optional, taken): the placeholder names a file Build Images copies from its .env.default before the render step. A rename on
    either side otherwise surfaces only when CI's `config` refuses a missing env_file.
    """
    steps = {step.get('name'): step for step in _image_build_job().get('steps') or []}
    staged = set()
    for line in _step_lines(steps['Build Images']):
        words = shlex.split(line)
        if words[:1] == ['cp'] and len(words) == 3 and words[1] == f'{words[2]}.default':
            staged.add(posixpath.normpath(words[2]))
    placeholders = {}
    for line in _step_lines(steps[RENDER_STEP]):
        for variable, value in re.findall(r'\b(ROOT_ENV_FILE|STORE_ENV_FILE|INGEST_ENV_FILE)="([^"]*)"', line):
            placeholders[variable] = posixpath.normpath(value.removeprefix('${PWD}/'))
    assert set(placeholders) == {'ROOT_ENV_FILE', 'STORE_ENV_FILE', 'INGEST_ENV_FILE'}, placeholders
    assert set(placeholders.values()) <= staged, (
        f'{RENDER_STEP} names {placeholders}; Build Images stages {sorted(staged)}'
    )


@pytest.mark.build_infra
def test_system_job_never_swallows_a_failure():
    """tj-vhboky.52 item 7: no continue-on-error, no `|| true`, and the suite's status is the step's.

    The suite step is `make test-system` under errexit and ends there, so pytest's exit status --
    exit 5 on an empty collection included -- is the step's. No step in the job runs pytest
    itself: the target is the one definition of the invocation, and a copy would drift from it.
    """
    job = _system_job()
    assert 'continue-on-error' not in job, f'{SYSTEM_JOB_NAME} sets continue-on-error at job level'
    lenient = [step.get('name') for step in _system_steps() if 'continue-on-error' in step]
    assert not lenient, f'steps set continue-on-error: {lenient}'
    swallowed = [
        f'{step.get("name")}: {line}'
        for step in _system_steps()
        for line in _step_lines(step)
        if _SWALLOWED_STATUS.search(line)
    ]
    assert not swallowed, f'{SYSTEM_JOB_NAME} discards an exit status: {swallowed}'

    lines = _step_lines(_system_step(SYSTEM_TESTS_STEP))
    assert lines, f'{SYSTEM_TESTS_STEP} runs nothing'
    assert shlex.split(lines[-1]) == ['make', SYSTEM_TARGET], (
        f'{SYSTEM_TESTS_STEP} must end on `make {SYSTEM_TARGET}` alone, so its status is the step status: {lines[-1]!r}'
    )
    errexit = [line for line in lines[:-1] if re.match(r'^set\s+-\w*e', line)]
    assert errexit, f'{SYSTEM_TESTS_STEP} does not set errexit before running the suite'
    direct = [
        f'{step.get("name")}: {line}' for step in _system_steps() for line in _step_lines(step) if 'pytest' in line
    ]
    assert not direct, f'{SYSTEM_JOB_NAME} runs pytest directly instead of make {SYSTEM_TARGET}: {direct}'


@pytest.mark.build_infra
def test_only_the_guarded_target_steps_attest_a_disposable_database():
    """tj-vhboky.52 item 2: SYSTEM_TEST_DISPOSABLE_DB=1 on the guarded-target steps and nowhere else.

    The attestation is what lets the suite write to a database. Set on the step, nothing else in
    the job inherits it; set at job or workflow level, or exported by a script, it would.

    tj-irhy0a.2 RE-PIN (tj-irhy0a.1 item 1 and its 08:35 UTC note; item 3): make system-launch and
    make seed-dump sit behind the same guard as make test-system (test_fake_overlay.py,
    test_seed_dump_make.py), so Start System and Seed Dump carry it too. The property is unchanged:
    STEP-level only, on exactly the steps that run a guarded target -- ATTESTING_STEPS -- and never
    on the job, the workflow, another step, a run line or a `with:`.
    """
    for name in sorted(ATTESTING_STEPS):
        step = _system_step(name)
        assert str((step.get('env') or {}).get(SYSTEM_GUARD)) == '1', (
            f'{name} does not set {SYSTEM_GUARD}=1 in its env: {step.get("env")}'
        )
    elsewhere = []
    for path in _workflow_files():
        document = _load_yaml(path) or {}
        if SYSTEM_GUARD in (document.get('env') or {}):
            elsewhere.append(f'{path.name} workflow env')
        for job_id, job in (document.get('jobs') or {}).items():
            if SYSTEM_GUARD in ((job or {}).get('env') or {}):
                elsewhere.append(f'{path.name} {job_id} env')
            for other in (job or {}).get('steps') or []:
                where = f'{path.name} {job_id} / {other.get("name")}'
                is_attesting_step = (
                    path == TESTING_WORKFLOW
                    and (job or {}).get('name') == SYSTEM_JOB_NAME
                    and other.get('name') in ATTESTING_STEPS
                )
                if not is_attesting_step and SYSTEM_GUARD in (other.get('env') or {}):
                    elsewhere.append(f'{where} env')
                if any(SYSTEM_GUARD in line for line in _step_lines(other)):
                    elsewhere.append(f'{where} run')
                if any(SYSTEM_GUARD in scalar for scalar in _walk_scalars(other.get('with') or {})):
                    elsewhere.append(f'{where} with')
    assert not elsewhere, f'{SYSTEM_GUARD} is set outside {sorted(ATTESTING_STEPS)}: {elsewhere}'


@pytest.mark.build_infra
def test_generated_secrets_are_masked_before_any_use():
    """tj-vhboky.52 item 2: each throwaway secret is masked on the line after it is generated.

    POSTGRES_PASS and INSTANCE_WRITE_SECRET are generated with openssl in the Stage step and
    written to the staged project env file. `::add-mask::` must be the very next line, before the
    value is written, printed or handed to anything; nothing else may print it, and no step may
    switch on shell tracing, which would print it expanded. The Stage step precedes every other
    step that mentions either name.
    """
    stage = _system_step(STAGE_STEP)
    lines = _step_lines(stage)
    generated = {match.group(1): index for index, line in enumerate(lines) if (match := _GENERATED_VALUE.match(line))}
    written = {}
    for line in lines:
        words = shlex.split(line)
        if words[:1] == ['printf'] and words[-2:] == ['>>', STAGED_ENV_FILE]:
            key = re.search(r'([A-Z][A-Z0-9_]*)=%s', words[1])
            value = re.fullmatch(r'\$\{?(\w+)\}?', words[2]) if len(words) > 3 else None
            if key and value:
                written[key.group(1)] = value.group(1)
    for key in SYSTEM_SECRET_KEYS:
        variable = written.get(key)
        assert variable, f'{STAGE_STEP} writes no generated {key} to the staged env file: {written}'
        assert variable in generated, f'{key} is written from ${variable}, which is not generated in {STAGE_STEP}'
        mask = lines[generated[variable] + 1] if generated[variable] + 1 < len(lines) else ''
        assert mask == f'echo "::add-mask::${{{variable}}}"', (
            f'the line after generating {key} (${variable}) must mask it, found {mask!r}'
        )
        printed = [
            line
            for line in lines
            if line != mask
            and re.match(r'^(?:echo|printf|cat|tee)\b', line)
            and _mentions_variable(line, variable)
            and not line.endswith(f'>> {STAGED_ENV_FILE}')
        ]
        assert not printed, f'{STAGE_STEP} prints ${variable} ({key}): {printed}'

    tracing = [
        f'{step.get("name")}: {line}'
        for step in _system_steps()
        for line in _step_lines(step)
        if re.match(r'^set\s+(?:-\w*[xv]|-o\s+(?:xtrace|verbose))', line)
    ]
    assert not tracing, f'{SYSTEM_JOB_NAME} switches on shell tracing: {tracing}'

    names = [step.get('name') for step in _system_steps()]
    first_use = next(
        (
            index
            for index, step in enumerate(_system_steps())
            if step.get('name') != STAGE_STEP
            and any(key in line for line in _step_lines(step) for key in SYSTEM_SECRET_KEYS)
        ),
        len(names),
    )
    assert names.index(STAGE_STEP) < first_use, (
        f'{names[first_use]} uses a secret before {STAGE_STEP} generates and masks it'
    )


@pytest.mark.build_infra
def test_the_testing_workflow_stays_under_the_broker_credential_rule():
    """tj-59cce6 still covers the job: the workflow is branch-triggered, and the job names no broker value.

    test_branch_triggered_workflow_holds_no_broker_credential skips a workflow outside the branch
    reach, so this first pins that the testing workflow is inside it. Then the System Testing job
    -- every key and value, run scripts included -- names no ALPACA_/IBKR_/QUESTRADE_ variable at
    all, credential or not: it runs without a broker. Since tj-irhy0a.1 the job runs data_ingest on
    the fake-mode overlay, which blanks the broker keys itself (docker-compose.fake.yaml, pinned in
    test_fake_overlay.py); the workflow still names none.
    """
    assert TESTING_WORKFLOW.name in _branch_reach(), (
        f'{TESTING_WORKFLOW.name} is no longer branch-triggered, so the tj-59cce6 test skips it'
    )
    named = sorted(
        {match.group(0) for scalar in _walk_scalars(_system_job()) for match in _BROKER_NAME.finditer(scalar)}
    )
    assert not named, f'{SYSTEM_JOB_NAME} names broker variables {named}; it must run without any (tj-59cce6)'
    references = sorted(set(_secret_references(_system_job())))
    assert references == ['GITHUB_TOKEN'], f'{SYSTEM_JOB_NAME} references secrets {references}; only GITHUB_TOKEN'


@pytest.mark.build_infra
def test_instance_secret_lifecycle_scans_logs_then_proves_a_blank_secret_refuses_writes():
    """tj-vhboky.52 item 6: (f) the log scan, then (e) blank, recreate data_store alone, probe.

    The scan comes first because recreating data_store discards the logs of the whole suite, and it
    counts matches rather than printing them. Then every write route is probed with each header
    variant -- none, empty, arbitrary, the former secret -- and each must answer 401.
    """
    lines = _step_lines(_system_step(LIFECYCLE_STEP))

    def first(predicate) -> int:
        index = next((i for i, line in enumerate(lines) if predicate(line)), None)
        assert index is not None, f'{LIFECYCLE_STEP} lacks an expected line'
        return index

    scan = first(lambda line: 'docker logs' in line)
    blank = first(
        lambda line: line.startswith('sed ') and 'INSTANCE_WRITE_SECRET=/' in line and STAGED_ENV_FILE in line
    )
    recreate = first(lambda line: '--force-recreate' in line)
    assert scan < blank < recreate, (
        f'{LIFECYCLE_STEP} must scan logs ({scan}), blank ({blank}), then recreate ({recreate})'
    )

    grep = re.search(r'\bgrep\s+-(\w+)', lines[scan])
    assert grep and ({'c', 'q'} & set(grep.group(1))), (
        f'the log scan must count or test matches, never print them: {lines[scan]}'
    )

    recreated = shlex.split(lines[recreate])
    assert '--no-deps' in recreated and '--wait' in recreated and recreated[-1] == 'data_store', (
        f'the recreate must be data_store alone, waited on healthy: {lines[recreate]}'
    )

    loops = [command for line in lines for command in _commands(line) if command[:3] == ['for', 'route', 'in']]
    assert len(loops) == 1, f'{LIFECYCLE_STEP} must probe from one route loop, found {loops}'
    routes = loops[0][3:]
    assert len(routes) == LIFECYCLE_ROUTE_COUNT, f'expected {LIFECYCLE_ROUTE_COUNT} write routes, probed {routes}'
    assert {route.split()[0] for route in routes} == {'POST', 'DELETE'}, routes
    probes = [line for line in lines if line.startswith('probe ')]
    assert len(probes) == LIFECYCLE_HEADER_VARIANTS, f'expected one probe per header variant, found {probes}'
    assert any('!= "401"' in line for line in lines), f'{LIFECYCLE_STEP} never requires HTTP 401'


# ---------------------------------------------------------------------------------------
# THE SECRET-GUARDED WRITE ROUTES (tj-vhboky.75)
#
# The instance secret is a security control, so which routes prove they refuse without it must
# be derived, not remembered. The Instance Secret Lifecycle step probes a hand-listed set of
# write routes with the secret blank, and tests/system/test_http_write_secret.py covers a
# hand-listed set over real HTTP. A fourth route carrying require_instance_secret would be guarded
# and proven by neither. So the guarded set is read from data_store's app itself -- every
# (method, path) whose dependency tree reaches require_instance_secret -- and each hand list must
# equal it, both ways round.
#
# The app is imported (routes only; no lifespan runs). The workflow's probes are parsed from the
# YAML and resolved to route templates by the app's own router matching, so a probe that hits a
# different route than intended is caught too. The system module is read with ast and NEVER
# imported: it lives under tests/system, outside the gate, and importing it would drag in its
# fixtures. Only the production interface enums its paths are built from are imported.
SECRET_PROBE_MODULE = REPO_ROOT / 'tests' / 'system' / 'test_http_write_secret.py'
SECRET_PROBE_ENUM = 'WriteRoute'
SECRET_PROBE_PARAMETER = 'route'
SECRET_PROBE_CLIENT = 'data_store'
_HTTP_CLIENT_METHODS = frozenset({'get', 'post', 'put', 'patch', 'delete'})
_INTERFACE_PACKAGE = 'routers.'
SECRET_PROBE_SOURCES = ('workflow', 'system-suite')


def _data_store_app():
    """data_store's FastAPI app, imported lazily so a broken import reds these tests, not the module."""
    from data.store.app.main import app

    return app


def _reaches(dependant, call) -> bool:
    return any(child.call is call or _reaches(child, call) for child in dependant.dependencies)


def _api_routes(app) -> list:
    """Every API route on `app` as FastAPI serves it: included routers flattened, prefixes applied.

    Since FastAPI 0.141 include_router no longer copies routes onto the app; app.routes holds one
    wrapper per included router. iter_route_contexts is FastAPI's public flattening, and each
    context carries the EFFECTIVE path and dependant -- router-level include dependencies merged
    in -- so a guard added with include_router(dependencies=[...]) is seen too.
    """
    from fastapi.routing import APIRoute, iter_route_contexts

    return [context for context in iter_route_contexts(app.routes) if isinstance(context.original_route, APIRoute)]


def _guarded_routes(app) -> set[tuple[str, str]]:
    """Every (METHOD, path template) on `app` whose dependency tree includes require_instance_secret."""
    from routers.common.instance_secret import require_instance_secret

    return {
        (method, route.path)
        for route in _api_routes(app)
        if _reaches(route.dependant, require_instance_secret)
        for method in route.methods
    }


def _route_templates(app, method: str, url: str) -> list[str]:
    """The path template of every route on `app` that fully matches `method url`, by the app's own matching."""
    from starlette.routing import Match

    scope = {'type': 'http', 'method': method, 'path': url.partition('?')[0], 'root_path': ''}
    return [route.path for route in _api_routes(app) if route.matches(scope)[0] == Match.FULL]


def _lifecycle_probe_loop(document: dict) -> tuple[dict, list[str]]:
    """The Instance Secret Lifecycle step of a parsed workflow, and the words of its one route loop."""
    jobs = [job for job in (document.get('jobs') or {}).values() if (job or {}).get('name') == SYSTEM_JOB_NAME]
    assert len(jobs) == 1, f'expected one {SYSTEM_JOB_NAME!r} job, found {len(jobs)}'
    steps = [step for step in jobs[0].get('steps') or [] if step.get('name') == LIFECYCLE_STEP]
    assert len(steps) == 1, f'expected one {LIFECYCLE_STEP!r} step in {SYSTEM_JOB_NAME}, found {len(steps)}'
    loops = [
        command
        for line in _step_lines(steps[0])
        for command in _commands(line)
        if command[:3] == ['for', SECRET_PROBE_PARAMETER, 'in']
    ]
    assert len(loops) == 1, f'{LIFECYCLE_STEP} must probe from one route loop, found {loops}'
    return steps[0], loops[0]


def _lifecycle_probed_routes(workflow: Path, app) -> set[tuple[str, str]]:
    """The (METHOD, path template) of every route the Instance Secret Lifecycle step's loop probes."""
    _, loop = _lifecycle_probe_loop(_load_yaml(workflow) or {})
    probed = set()
    for entry in loop[3:]:
        method, _, url = entry.partition(' ')
        templates = _route_templates(app, method, url)
        assert len(templates) == 1, (
            f'{LIFECYCLE_STEP} probes {entry!r}, which data_store routes to {templates}; expected exactly one route'
        )
        probed.add((method, templates[0]))
    return probed


def _interface_template(expression: ast.expr, local: dict[str, ast.expr], helpers: dict, imports: dict) -> str:
    """Resolve a request's path expression to the interface enum value it is formatted from.

    Follows a local name to its assignment, a module helper to its return value and `.format(...)`
    to its receiver, until it reaches `<Interface>.<MEMBER>`. The interface is imported from the
    module the probe module imports it from, which must be production code under routers/.
    """
    if isinstance(expression, ast.Name) and expression.id in local:
        return _interface_template(local[expression.id], local, helpers, imports)
    if isinstance(expression, ast.Call) and isinstance(expression.func, ast.Name) and expression.func.id in helpers:
        returns = [node.value for node in ast.walk(helpers[expression.func.id]) if isinstance(node, ast.Return)]
        assert len(returns) == 1 and returns[0] is not None, f'{expression.func.id} must return one path expression'
        return _interface_template(returns[0], {}, helpers, imports)
    if (
        isinstance(expression, ast.Call)
        and isinstance(expression.func, ast.Attribute)
        and expression.func.attr == 'format'
    ):
        return _interface_template(expression.func.value, local, helpers, imports)
    if (
        isinstance(expression, ast.Attribute)
        and isinstance(expression.value, ast.Name)
        and expression.value.id in imports
    ):
        module = imports[expression.value.id]
        assert module.startswith(_INTERFACE_PACKAGE), (
            f'{expression.value.id} comes from {module}; a probe path must be built from a routers/ interface enum'
        )
        return str(getattr(getattr(importlib.import_module(module), expression.value.id), expression.attr))
    raise AssertionError(f'cannot resolve the request path {ast.unparse(expression)!r} to an interface route')


def _is_route_parametrize(decorator: ast.expr) -> bool:
    return (
        isinstance(decorator, ast.Call)
        and ast.unparse(decorator.func) == 'pytest.mark.parametrize'
        and bool(decorator.args)
        and isinstance(decorator.args[0], ast.Constant)
        and decorator.args[0].value == SECRET_PROBE_PARAMETER
    )


def _client_sends(body: list[ast.stmt]) -> list[ast.Call]:
    """Every `data_store.<http method>(...)` call anywhere in `body`."""
    return [
        node
        for statement in body
        for node in ast.walk(statement)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == SECRET_PROBE_CLIENT
        and node.func.attr in _HTTP_CLIENT_METHODS
    ]


def _system_suite_covered_routes(module: Path) -> set[tuple[str, str]]:
    """The (METHOD, path template) each WriteRoute member sends, read from `module` by ast, never imported.

    Every test that parametrises `route` must do so over list(WriteRoute), and the one match on
    `route` that sends requests must have exactly one case per member, each sending exactly one
    request through the data_store client.
    """
    tree = ast.parse(module.read_text(encoding='utf-8'))
    imports = {
        alias.asname or alias.name: node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module
        for alias in node.names
    }
    helpers = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == SECRET_PROBE_ENUM]
    assert len(classes) == 1, f'{module.name} defines {len(classes)} {SECRET_PROBE_ENUM} classes, expected 1'
    members = [
        target.id
        for node in classes[0].body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    ]
    assert members, f'{SECRET_PROBE_ENUM} in {module.name} has no members'

    everything = f'list({SECRET_PROBE_ENUM})'
    parametrised = [
        (node.name, ast.unparse(decorator.args[1]) if len(decorator.args) > 1 else None)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name.startswith('test_')
        for decorator in node.decorator_list
        if _is_route_parametrize(decorator)
    ]
    assert parametrised, f'no test in {module.name} parametrises {SECRET_PROBE_PARAMETER!r}'
    partial = [(name, values) for name, values in parametrised if values != everything]
    assert not partial, (
        f'tests in {module.name} parametrise {SECRET_PROBE_PARAMETER!r} over less than {everything}: {partial}'
    )

    senders = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Match)
        and ast.unparse(node.subject) == SECRET_PROBE_PARAMETER
        and any(_client_sends(case.body) for case in node.cases)
    ]
    assert len(senders) == 1, (
        f'expected one match on {SECRET_PROBE_PARAMETER!r} that sends requests in {module.name}, found {len(senders)}'
    )

    covered, handled = set(), []
    for case in senders[0].cases:
        pattern = ast.unparse(case.pattern)
        assert isinstance(case.pattern, ast.MatchValue) and pattern.startswith(f'{SECRET_PROBE_ENUM}.'), (
            f'unexpected case pattern {pattern!r} in the sending match'
        )
        member = pattern.removeprefix(f'{SECRET_PROBE_ENUM}.')
        handled.append(member)
        calls = _client_sends(case.body)
        assert len(calls) == 1, f'case {member} must send exactly one request, sends {len(calls)}'
        local = {
            target.id: node.value
            for node in case.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        covered.add((calls[0].func.attr.upper(), _interface_template(calls[0].args[0], local, helpers, imports)))
    assert sorted(handled) == sorted(members), (
        f'the sending match handles {sorted(handled)}, but {SECRET_PROBE_ENUM} has {sorted(members)}'
    )
    return covered


def _secret_probe_gaps(guarded: set[tuple[str, str]], covered: set[tuple[str, str]], where: str) -> list[str]:
    """Both directions of disagreement between the guarded routes and one source's covered routes."""
    unproven = [f'{method} {path}' for method, path in sorted(guarded - covered)]
    unguarded = [f'{method} {path}' for method, path in sorted(covered - guarded)]
    gaps = []
    if unproven:
        gaps.append(f'guarded by require_instance_secret but not covered by {where}: {unproven}')
    if unguarded:
        gaps.append(f'covered by {where} but not guarded by require_instance_secret: {unguarded}')
    return gaps


def _secret_probe_coverage(source: str, app) -> tuple[set[tuple[str, str]], str]:
    if source == 'workflow':
        return _lifecycle_probed_routes(TESTING_WORKFLOW, app), f'the {LIFECYCLE_STEP} step in {TESTING_WORKFLOW.name}'
    return _system_suite_covered_routes(SECRET_PROBE_MODULE), SECRET_PROBE_MODULE.relative_to(REPO_ROOT).as_posix()


@pytest.mark.build_infra
@pytest.mark.parametrize('source', SECRET_PROBE_SOURCES)
def test_every_secret_guarded_route_is_proven_to_refuse(source: str):
    """tj-vhboky.75: the routes proven to refuse without the secret are exactly the guarded ones.

    Derived from data_store's app, so a new route carrying require_instance_secret reds the gate
    until both the CI lifecycle probe and the system suite cover it -- and a probe or case left on
    a route that lost the guard reds it too.
    """
    app = _data_store_app()
    guarded = _guarded_routes(app)
    assert guarded, 'data_store has no route guarded by require_instance_secret, so this comparison is vacuous'
    covered, where = _secret_probe_coverage(source, app)
    gaps = _secret_probe_gaps(guarded, covered, where)
    assert not gaps, '; '.join(gaps)


@pytest.mark.build_infra
def test_the_guarded_route_derivation_follows_every_dependency_form():
    """Guard the guard: guarded through a decorator list, a parameter, a sub-dependency or an include.

    The included router is the shape data_store actually has, so this also pins that routes behind
    include_router are seen at all, under their prefixed path.
    """
    from fastapi import APIRouter, Depends, FastAPI

    from routers.common.instance_secret import require_instance_secret

    async def wraps_the_guard(_: Annotated[None, Depends(require_instance_secret)]) -> None:
        return None

    scratch = FastAPI()

    @scratch.post('/listed', dependencies=[Depends(require_instance_secret)])
    async def by_list() -> None:
        return None

    @scratch.patch('/parameter')
    async def by_parameter(_: Annotated[None, Depends(require_instance_secret)]) -> None:
        return None

    @scratch.delete('/nested')
    async def by_sub_dependency(_: Annotated[None, Depends(wraps_the_guard)]) -> None:
        return None

    @scratch.get('/open')
    async def open_read() -> None:
        return None

    included = APIRouter()

    @included.put('/by-include')
    async def by_include() -> None:
        return None

    @included.get('/also-open')
    async def included_read() -> None:
        return None

    scratch.include_router(included, prefix='/wide', dependencies=[Depends(require_instance_secret)])
    scratch.include_router(included, prefix='/open-include')

    assert _guarded_routes(scratch) == {
        ('POST', '/listed'),
        ('PATCH', '/parameter'),
        ('DELETE', '/nested'),
        ('PUT', '/wide/by-include'),
        ('GET', '/wide/also-open'),
    }


@pytest.mark.build_infra
@pytest.mark.parametrize('source', SECRET_PROBE_SOURCES)
def test_a_new_guarded_route_without_a_probe_is_named(source: str):
    """Guard the guard: data_store's routes plus one more guarded write, and each source names it."""
    from fastapi import Depends, FastAPI

    from routers.common.instance_secret import require_instance_secret

    app = _data_store_app()
    scratch = FastAPI()
    scratch.router.routes.extend(app.routes)

    @scratch.post('/store/unprobed-write', dependencies=[Depends(require_instance_secret)])
    async def unprobed_write() -> None:
        return None

    guarded = _guarded_routes(scratch)
    assert guarded - _guarded_routes(app) == {('POST', '/store/unprobed-write')}, guarded
    covered, where = _secret_probe_coverage(source, scratch)
    gaps = _secret_probe_gaps(guarded, covered, where)
    unproven = f'guarded by require_instance_secret but not covered by {where}: '
    assert any(gap.startswith(unproven) and "'POST /store/unprobed-write'" in gap for gap in gaps), gaps


@pytest.mark.build_infra
def test_a_dropped_lifecycle_probe_is_named(tmp_path: Path):
    """Guard the guard: a copy of the workflow with the loop's first probe removed names that route."""
    app = _data_store_app()
    document = _load_yaml(TESTING_WORKFLOW)
    step, loop = _lifecycle_probe_loop(document)
    dropped = loop[3]
    assert step['run'].count(f'"{dropped}"') == 1, f'the probe {dropped!r} is not quoted exactly once in the step'
    step['run'] = step['run'].replace(f'"{dropped}"', '')
    copy_path = tmp_path / TESTING_WORKFLOW.name
    copy_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding='utf-8')

    method, _, url = dropped.partition(' ')
    (template,) = _route_templates(app, method, url)
    probed = _lifecycle_probed_routes(TESTING_WORKFLOW, app)
    gaps = _secret_probe_gaps(probed, _lifecycle_probed_routes(copy_path, app), 'the copy')
    assert gaps == [f"guarded by require_instance_secret but not covered by the copy: ['{method} {template}']"], gaps


@pytest.mark.build_infra
@pytest.mark.parametrize('position', [0, -1], ids=['first-member', 'last-member'])
def test_a_route_dropped_from_the_system_suite_is_named(tmp_path: Path, position: int):
    """Guard the guard: a copy of the system module without one WriteRoute member and its case names that route."""
    tree = ast.parse(SECRET_PROBE_MODULE.read_text(encoding='utf-8'))
    enum = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == SECRET_PROBE_ENUM)
    assignment = [node for node in enum.body if isinstance(node, ast.Assign)][position]
    member = assignment.targets[0].id
    enum.body.remove(assignment)
    removed_cases = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Match):
            kept = [case for case in node.cases if ast.unparse(case.pattern) != f'{SECRET_PROBE_ENUM}.{member}']
            removed_cases += len(node.cases) - len(kept)
            node.cases = kept
    assert removed_cases, f'no match case handles {SECRET_PROBE_ENUM}.{member}, so this copy drops nothing'
    copy_path = tmp_path / SECRET_PROBE_MODULE.name
    copy_path.write_text(ast.unparse(tree), encoding='utf-8')

    full = _system_suite_covered_routes(SECRET_PROBE_MODULE)
    reduced = _system_suite_covered_routes(copy_path)
    (lost,) = full - reduced
    gaps = _secret_probe_gaps(full, reduced, 'the copy')
    assert gaps == [f"guarded by require_instance_secret but not covered by the copy: ['{lost[0]} {lost[1]}']"], gaps


# ---------------------------------------------------------------------------------------
# A CONTAINER TEST JOB BRINGS ITS OWN GIT AND MAKE (tj-ijpys9.16)
#
# A job that runs in `container:` gets the image's toolchain, not the runner's. The unit-test job
# runs in debian:bookworm-slim, which has neither git nor make, and 26 tests failed closed in CI:
# 22 drive the Makefile, 4 run git from the checkout root. git has to be there BEFORE
# actions/checkout, not merely before pytest: with no git on PATH checkout falls back to a REST
# API tarball with no .git, and installing git afterwards cannot bring the repository back.
#
# Derived from the YAML, not from a job name: every job in every workflow that runs in a container
# and has a step running pytest or `make test` is judged, so a second container job is held to the
# same rule the day it is added.
#
# safe.directory is pinned with it. The runner creates the workspace as its own user and the
# container runs as root, so git refuses the checkout for dubious ownership; the builder's reading
# of actions/checkout (tj-ijpys9.16 notes) is that checkout's own safe.directory entry lives in a
# temporary global config deleted when its step ends. Without the entry the same four git tests
# fail closed, which is the failure this bead exists to remove, so it is part of the contract, not
# an extra. Accepted at --system or --global scope (a later step's HOME is the real one either
# way), for the workspace or for '*', in any step before the suite runs.
CONTAINER_TEST_TOOLS = frozenset({'git', 'make'})
CHECKOUT_ACTION = 'actions/checkout'
# (package manager, subcommand) pairs that install packages. A manager missing from here makes a
# correct job read as installing nothing, which fails loudly rather than passing.
_PACKAGE_INSTALLS = frozenset(
    {('apt-get', 'install'), ('apt', 'install'), ('apk', 'add'), ('dnf', 'install'), ('yum', 'install')}
)
SAFE_DIRECTORY_SCOPES = frozenset({'--system', '--global'})
_WORKSPACE_VALUE = re.compile(r'^(?:\$GITHUB_WORKSPACE|\$\{GITHUB_WORKSPACE\}|\$\{\{\s*github\.workspace\s*\}\}|\*)$')


def _step_commands(step: dict) -> list[list[str]]:
    """Every simple command in a step's `run:` script, as argv."""
    return [command for line in _step_lines(step) for command in _commands(line)]


def _is_checkout(step: dict) -> bool:
    uses = str(step.get('uses') or '')
    return uses == CHECKOUT_ACTION or uses.startswith(f'{CHECKOUT_ACTION}@')


def _runs_the_suite(step: dict) -> bool:
    """True when a step runs pytest (directly, through uv or python -m) or `make test`."""
    for command in _step_commands(step):
        if any(PurePosixPath(word).name == 'pytest' for word in command):
            return True
        for index, word in enumerate(command):
            if PurePosixPath(word).name == 'make' and 'test' in command[index + 1 :]:
                return True
    return False


def _installed_packages(step: dict) -> set[str]:
    """The package names a step's run script installs, version pins stripped."""
    packages: set[str] = set()
    for command in _step_commands(step):
        for index in range(len(command) - 1):
            if (PurePosixPath(command[index]).name, command[index + 1]) in _PACKAGE_INSTALLS:
                arguments = command[index + 2 :]
                packages |= {re.split(r'[=<>]', word)[0] for word in arguments if not word.startswith('-')}
                break
    return packages


def _marks_workspace_safe(step: dict) -> bool:
    """True when a step adds the workspace (or '*') to safe.directory at system or global scope."""
    for command in _step_commands(step):
        if not command or PurePosixPath(command[0]).name != 'git' or 'config' not in command:
            continue
        if 'safe.directory' not in command or not SAFE_DIRECTORY_SCOPES & set(command):
            continue
        value = ' '.join(command[command.index('safe.directory') + 1 :])
        if _WORKSPACE_VALUE.match(value):
            return True
    return False


def _container_test_jobs() -> list[tuple[str, dict]]:
    """(label, job) for every workflow job that runs in a container and runs the unit suite."""
    found = []
    for workflow in _workflow_files():
        for job_id, job in ((_load_yaml(workflow) or {}).get('jobs') or {}).items():
            spec = job or {}
            if spec.get('container') and any(_runs_the_suite(step) for step in spec.get('steps') or []):
                found.append((f'{workflow.name}:{job_id}', spec))
    return found


def _container_toolchain_gaps(job: dict) -> list[str]:
    """What a container test job lacks: git and make installed before checkout, and a safe workspace."""
    steps = job.get('steps') or []
    checkouts = [index for index, step in enumerate(steps) if _is_checkout(step)]
    if not checkouts:
        return [f'no {CHECKOUT_ACTION} step, so there is nothing to install {sorted(CONTAINER_TEST_TOOLS)} before']
    checkout = checkouts[0]
    gaps = []
    before = set().union(*(_installed_packages(step) for step in steps[:checkout]))
    after = set().union(*(_installed_packages(step) for step in steps[checkout:]))
    missing = sorted(CONTAINER_TEST_TOOLS - before)
    if missing:
        late = sorted(set(missing) & after)
        gaps.append(
            f'{missing} not installed before {CHECKOUT_ACTION} (step {checkout})'
            + (f'; {late} installed only after it, too late for checkout to clone a .git' if late else '')
        )
    suite = next(index for index, step in enumerate(steps) if _runs_the_suite(step))
    if not any(_marks_workspace_safe(step) for step in steps[:suite]):
        gaps.append(
            f'no step before the suite (step {suite}) runs `git config --system|--global --add '
            f'safe.directory "${{GITHUB_WORKSPACE}}"`, so git refuses the root-run container checkout'
        )
    return gaps


_CONTAINER_TEST_JOBS = _container_test_jobs()


@pytest.mark.build_infra
def test_some_workflow_job_runs_the_suite_in_a_container():
    """Guard the guard: the derivation must find the container unit-test job.

    If _runs_the_suite or the container detection stopped matching, the per-job test below would
    parametrize over nothing. No job name is named: any container job running the suite satisfies it.
    """
    assert _CONTAINER_TEST_JOBS, (
        'no workflow job both runs in `container:` and runs pytest or `make test`: either the unit '
        'job left its container (then retire this section in the same diff) or the detection is blind'
    )


@pytest.mark.build_infra
@pytest.mark.parametrize(
    'job', [job for _, job in _CONTAINER_TEST_JOBS], ids=[label for label, _ in _CONTAINER_TEST_JOBS]
)
def test_container_test_job_installs_git_and_make_before_checkout(job: dict):
    """tj-ijpys9.16: a container job running the suite installs git and make before checkout and trusts the workspace."""
    gaps = _container_toolchain_gaps(job)
    assert gaps == [], '; '.join(gaps)


_CHECKOUT = {'uses': 'actions/checkout@0123456789abcdef'}
_INSTALL = {'run': 'apt-get update\napt-get install -y --no-install-recommends git make ca-certificates'}
_SAFE = {'run': 'git config --system --add safe.directory "${GITHUB_WORKSPACE}"'}
_SUITE = {'run': 'uv run pytest -s'}
_TOOLCHAIN_ACCEPTED = {
    'install-then-checkout': [_INSTALL, _SAFE, _CHECKOUT, _SUITE],
    'one-step-with-safe-directory': [{'run': _INSTALL['run'] + '\n' + _SAFE['run']}, _CHECKOUT, _SUITE],
    'global-scope-expression-make-test': [
        _INSTALL,
        _CHECKOUT,
        {'run': 'git config --global --add safe.directory "${{ github.workspace }}"'},
        {'run': 'make test PATHS=common'},
    ],
    'pinned-version-and-env-prefix': [
        {'run': 'DEBIAN_FRONTEND=noninteractive apt-get install -y git=1:2.39.5-0 make'},
        _SAFE,
        _CHECKOUT,
        _SUITE,
    ],
}
_TOOLCHAIN_REJECTED = {
    'install-after-checkout': [_CHECKOUT, _INSTALL, _SAFE, _SUITE],
    'make-dropped': [{'run': 'apt-get install -y git ca-certificates'}, _SAFE, _CHECKOUT, _SUITE],
    'git-dropped': [{'run': 'apt-get install -y make ca-certificates'}, _SAFE, _CHECKOUT, _SUITE],
    'no-install-step': [_SAFE, _CHECKOUT, _SUITE],
    'no-safe-directory': [_INSTALL, _CHECKOUT, _SUITE],
    'safe-directory-after-suite': [_INSTALL, _CHECKOUT, _SUITE, _SAFE],
    'safe-directory-local-scope': [
        _INSTALL,
        _CHECKOUT,
        {'run': 'git config --local --add safe.directory "$GITHUB_WORKSPACE"'},
        _SUITE,
    ],
    'safe-directory-other-path': [
        {'run': _INSTALL['run'] + '\ngit config --system --add safe.directory /tmp'},
        _CHECKOUT,
        _SUITE,
    ],
    'no-checkout': [_INSTALL, _SAFE, _SUITE],
}


@pytest.mark.build_infra
@pytest.mark.parametrize('steps', list(_TOOLCHAIN_ACCEPTED.values()), ids=list(_TOOLCHAIN_ACCEPTED))
def test_container_toolchain_rule_accepts_a_correct_job(steps: list[dict]):
    """A correct container job passes, including shapes the real workflow does not use today."""
    assert _container_toolchain_gaps({'container': 'debian:bookworm-slim', 'steps': steps}) == []


@pytest.mark.build_infra
@pytest.mark.parametrize('steps', list(_TOOLCHAIN_REJECTED.values()), ids=list(_TOOLCHAIN_REJECTED))
def test_container_toolchain_rule_rejects_a_broken_job(steps: list[dict]):
    """Each of these leaves the suite without git, make or a trusted checkout, and must be named."""
    assert _container_toolchain_gaps({'container': 'debian:bookworm-slim', 'steps': steps}) != []


@pytest.mark.build_infra
def test_only_container_jobs_that_run_the_suite_are_judged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A host job has the runner's git and make; a container job that runs no tests needs neither."""
    workflow = {
        'jobs': {
            'host-tests': {'steps': [_CHECKOUT, _SUITE]},
            'container-lint': {
                'container': 'debian:bookworm-slim',
                'steps': [_CHECKOUT, {'run': 'uv run ruff check .'}],
            },
            'container-system': {
                'container': 'debian:bookworm-slim',
                'steps': [_CHECKOUT, {'run': 'make test-system'}],
            },
            'container-tests': {'container': {'image': 'debian:bookworm-slim'}, 'steps': [_CHECKOUT, _SUITE]},
        }
    }
    (tmp_path / 'ci.yml').write_text(yaml.safe_dump(workflow), encoding='utf-8')
    monkeypatch.setattr(sys.modules[__name__], 'WORKFLOW_DIR', tmp_path)
    assert [label for label, _ in _container_test_jobs()] == ['ci.yml:container-tests']


# ---------------------------------------------------------------------------------------
# ... AND THE TOOL THE SUITE ITSELF RUNS: JQ (tj-3mk3u5.40)
#
# common/tests/test_harness_hooks.py (tj-qenrpk) runs the PreToolUse Bash hooks in
# .claude/settings.json verbatim, and each hook pipeline starts with jq, which debian:bookworm-slim
# does not ship. Without jq a hook falls through its `|| true` and allows every command, so those
# tests check for jq at run time and fail (pytest.ini: never skip). That check fires only where the
# suite runs, and the agent image installs jq (.devcontainer/Dockerfile), so a job that lost jq would
# go red in CI alone, on a push no agent makes. This pins it where an agent's own run sees it.
#
# BEFORE THE SUITE, NOT BEFORE CHECKOUT, which is why jq is not in CONTAINER_TEST_TOOLS. git has to
# precede checkout for checkout to clone a repository; nothing earlier than pytest runs jq, so a job
# that installs it in any step before the suite is correct and must not be named. Adding jq to that
# set would also have turned every accepted fixture above red, and made the fixtures that drop git or
# make fail for jq as well, so they would stop proving their own detection. Judged per step, like the
# safe.directory check above: jq is installed in a step before the first one that runs the suite.
# Same derivation as above, so a second container job running the suite is held to it too.
#
# grep -P, the hooks' other tool, is deliberately not pinned. It comes from the image, not from this
# repository: bookworm's grep is Essential and pre-depends on libpcre2-8-0, so no line here installs
# it and no edit here can lose it. The one lever is `container:` itself, and a new image is checked
# on its first run by the hook tests' own grep -P probe, which runs the binary a static pin could
# only guess about.
CONTAINER_SUITE_TOOLS = frozenset({'jq'})


def _container_suite_tool_gaps(job: dict) -> list[str]:
    """What a container test job lacks: each of CONTAINER_SUITE_TOOLS installed in a step before the suite."""
    steps = job.get('steps') or []
    suite = next(index for index, step in enumerate(steps) if _runs_the_suite(step))
    before = set().union(*(_installed_packages(step) for step in steps[:suite]))
    missing = sorted(CONTAINER_SUITE_TOOLS - before)
    if not missing:
        return []
    late = sorted(set(missing) & set().union(*(_installed_packages(step) for step in steps[suite:])))
    return [
        f'{missing} not installed in a step before the suite (step {suite}), and the harness-hook tests run it'
        + (f'; {late} installed only in or after that step' if late else '')
    ]


@pytest.mark.build_infra
@pytest.mark.parametrize(
    'job', [job for _, job in _CONTAINER_TEST_JOBS], ids=[label for label, _ in _CONTAINER_TEST_JOBS]
)
def test_container_test_job_installs_jq_before_the_suite(job: dict):
    """tj-3mk3u5.40: a container job running the suite installs jq, which the harness-hook tests run, first."""
    gaps = _container_suite_tool_gaps(job)
    assert gaps == [], '; '.join(gaps)


_INSTALL_WITH_JQ = {'run': 'apt-get update\napt-get install -y --no-install-recommends git make jq ca-certificates'}
_INSTALL_JQ_ALONE = {'run': 'apt-get install -y --no-install-recommends jq'}
_SUITE_TOOLS_ACCEPTED = {
    'one-install-line': [_INSTALL_WITH_JQ, _SAFE, _CHECKOUT, _SUITE],
    'own-step-after-checkout': [_INSTALL, _SAFE, _CHECKOUT, _INSTALL_JQ_ALONE, _SUITE],
    'pinned-version-and-make-test': [
        _INSTALL,
        _CHECKOUT,
        {'run': 'DEBIAN_FRONTEND=noninteractive apt-get install -y jq=1.6-2.1+deb12u2'},
        _SAFE,
        {'run': 'make test PATHS=common'},
    ],
}
# Each of these is correct by the git-and-make rule above (the test asserts it), so jq is the only
# thing any of them can be named for.
_SUITE_TOOLS_REJECTED = {
    'jq-dropped': [_INSTALL, _SAFE, _CHECKOUT, _SUITE],
    'jq-after-the-suite': [_INSTALL, _SAFE, _CHECKOUT, _SUITE, _INSTALL_JQ_ALONE],
    'jq-commented-out': [{'run': _INSTALL['run'] + '\n# apt-get install -y jq'}, _SAFE, _CHECKOUT, _SUITE],
    'jq-run-never-installed': [_INSTALL, _SAFE, _CHECKOUT, {'run': 'jq --version'}, _SUITE],
    'jq-removed-not-installed': [{'run': _INSTALL['run'] + '\napt-get purge -y jq'}, _SAFE, _CHECKOUT, _SUITE],
}


@pytest.mark.build_infra
@pytest.mark.parametrize('steps', list(_SUITE_TOOLS_ACCEPTED.values()), ids=list(_SUITE_TOOLS_ACCEPTED))
def test_container_suite_tool_rule_accepts_a_correct_job(steps: list[dict]):
    """A correct container job passes both rules, including jq in its own step after checkout."""
    job = {'container': 'debian:bookworm-slim', 'steps': steps}
    assert _container_suite_tool_gaps(job) == []
    assert _container_toolchain_gaps(job) == []


@pytest.mark.build_infra
@pytest.mark.parametrize('steps', list(_SUITE_TOOLS_REJECTED.values()), ids=list(_SUITE_TOOLS_REJECTED))
def test_container_suite_tool_rule_rejects_a_broken_job(steps: list[dict]):
    """Each of these reaches the suite without jq installed, and must be named for it."""
    job = {'container': 'debian:bookworm-slim', 'steps': steps}
    assert _container_toolchain_gaps(job) == [], 'the fixture is broken for a reason other than jq'
    gaps = _container_suite_tool_gaps(job)
    assert len(gaps) == 1 and gaps[0].startswith("['jq'] not installed in a step before the suite"), gaps


@pytest.mark.build_infra
def test_jq_installed_after_the_suite_is_named_as_late():
    """The diagnosis says where jq went, so a reordered job reads as reordered, not as jq dropped."""
    job = {'container': 'debian:bookworm-slim', 'steps': _SUITE_TOOLS_REJECTED['jq-after-the-suite']}
    assert _container_suite_tool_gaps(job) == [
        "['jq'] not installed in a step before the suite (step 3), and the harness-hook tests run it; "
        "['jq'] installed only in or after that step"
    ]


# ---------------------------------------------------------------------------------------
# THE BUILD CONTEXT (tj-ijpys9.17)
#
# Every host bind-mount data directory compose declares must stay out of the Docker build context.
# The Postgres directory is mode 0700 and owned by the container's uid, so a build started while a
# stack is up -- make test-system's --build in CI, after Start System -- fails sending the context
# with "permission denied". The other half: what the images run must be sent, or an image builds
# without its source and fails only at runtime.
#
# That half is checked twice. (1) Nothing under a Dockerfile COPY source may be excluded, with one
# named exception: a file whose path RELATIVE TO THAT SOURCE has a directory segment exactly `tests`
# (common/tests/..., routers/tests/interface_manifest/...). No image reads a tests/ directory --
# tests run on the host venv, and the dev image and test_client bind-mount the source -- so keeping
# them out of the prod image (tj-v82dvm) is allowed. The COPY source itself, a non-test file,
# `tests_util/`, `testsuite/` and `tests.py` stay offenders. tj-ijpys9.17 first pinned "every
# tracked file under a COPY source is sent", which was a proxy for the purpose and stricter than it.
# (2) The purpose, directly: every module each service app imports, transitively (TYPE_CHECKING and
# function-level imports included), and each package __init__.py on its path, is sent. The apps are
# the COPY sources the Dockerfile builds from build args, so a new service is covered by the parse.
# (2) is what catches a production import of a tests/ module that (1) now lets be excluded.
# (3) The reverse of (1), so the exception is used and stays used: every `tests` directory under a
# COPY source that holds a git-tracked file IS excluded, file by file (tj-v82dvm). The directories
# are derived from the COPY parse and git, so a new suite under a copied package is covered.
# (4) Sent is not copied: every file of each app's closure lies under a static COPY source (one no
# build arg names) or the app's own source -- never another service's app or an uncopied package
# (tj-bhzf6b). An ancestor package __init__.py of those sources (data/__init__.py) is not copied
# and imports as a namespace package in the image; it is allowed only while it has no statement
# beyond a module docstring (tj-zcd9ar), since the host runs its code and the image does not. It is
# walked like every package __init__.py on a reached file's path, so what it imports must be copied too.
#
# The data directories are derived from the compose YAML, not listed: a mount is data when its host
# side starts with ${DATA_DIR...}, or with the literal fallback such an interpolation names. Each
# one is resolved against BOTH the DATA_DIR in .env.default and the compose fallback, because either
# can be in force: .env.default is what every environment copies, and the fallback is what compose
# uses when DATA_DIR is unset.
#
# .dockerignore is matched with a faithful subset of Docker's own semantics (moby patternmatcher):
# patterns are path-cleaned and anchored at the context root, so `volumes/` and `/volumes` both mean
# the top-level volumes; `*` and `?` stay within one path segment; `**` spans any number of them; a
# pattern that matches a parent directory excludes everything under it; `!` re-includes; the last
# matching pattern wins. Not implemented, because no line here uses them: escape sequences and
# Windows separators. A green run means the files agree; it does not start a daemon.
DOCKERIGNORE_FILE = REPO_ROOT / '.dockerignore'
DOCKERFILE = REPO_ROOT / 'Dockerfile'
DATA_DIR_VARIABLE = 'DATA_DIR'
# A representative file inside a data directory, for the "is its content sent?" half of the check.
DATA_DIR_SAMPLE_FILE = 'PG_VERSION'
# What the Dockerfile COPYs today. A floor for the parse below, so a parser regression that finds
# nothing cannot pass the "no COPY source is excluded" check vacuously.
KNOWN_COPY_SOURCES = frozenset({'common', 'routers', 'schemas', 'data/store/app', 'data/ingest/app'})
# The one directory name under a COPY source that .dockerignore may exclude (tj-v82dvm).
EXCLUDABLE_TEST_DIR = 'tests'
# Non-vacuity for the reverse pin: the test directories under a COPY source when it was written.
KNOWN_COPY_SOURCE_TEST_DIRS = frozenset({'common/tests', 'routers/tests', 'schemas/tests'})
# Non-vacuity for the import closure: a module each service app is known to import, and a floor on
# how many module files its closure reaches (61 for ingest and 74 for store when this was written).
KNOWN_APP_IMPORTS = {'data/ingest/app': 'routers/common/ping.py', 'data/store/app': 'routers/common/ping.py'}
APP_CLOSURE_FLOOR = 40

# ${DATA_DIR}, ${DATA_DIR:-default}, ${DATA_DIR-default}, ${DATA_DIR:?message}, ${DATA_DIR?message} or
# $DATA_DIR, then the rest of the path. The guarded forms carry no default and resolve like a bare
# ${DATA_DIR}; before tj-c4mosr.5 they were not parsed at all, and the agent-stack overlay's two data
# mounts were skipped in silence. test_every_data_dir_mount_is_parsed now fails on any it cannot read.
_DATA_DIR_INTERPOLATION = re.compile(
    rf'^(?:\$\{{{DATA_DIR_VARIABLE}(?::?-(?P<default>[^}}]*)|:?\?[^}}]*)?\}}|\${DATA_DIR_VARIABLE}(?!\w))(?P<rest>.*)$'
)

_IgnoreRules = list[tuple[bool, re.Pattern]]


def _dockerignore_pattern_regex(pattern: str) -> re.Pattern:
    """Compile one cleaned .dockerignore pattern the way moby's patternmatcher does."""
    out = ''
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if pattern.startswith('**/', index):
            # `**/` matches zero or more whole directories.
            out += '(?:.*/)?'
            index += 3
            continue
        if pattern.startswith('**', index):
            out += '.*'
            index += 2
            continue
        if char == '*':
            out += '[^/]*'
        elif char == '?':
            out += '[^/]'
        elif char == '[' and pattern.find(']', index + 1) != -1:
            close = pattern.find(']', index + 1)
            body = pattern[index + 1 : close]
            out += '[' + ('^' + body[1:] if body.startswith(('!', '^')) else body) + ']'
            index = close
        else:
            out += re.escape(char)
        index += 1
    return re.compile(f'^{out}$')


def _dockerignore_rules(text: str) -> _IgnoreRules:
    """Return (is_exception, compiled pattern) for each rule of a .dockerignore, in file order."""
    rules = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        exception = stripped.startswith('!')
        if exception:
            stripped = stripped[1:].strip()
        cleaned = posixpath.normpath(stripped).lstrip('/')
        if cleaned in ('', '.'):
            continue
        rules.append((exception, _dockerignore_pattern_regex(cleaned)))
    return rules


def _committed_dockerignore_rules() -> _IgnoreRules:
    return _dockerignore_rules(DOCKERIGNORE_FILE.read_text(encoding='utf-8'))


def _is_excluded_from_context(path: str, rules: _IgnoreRules) -> bool:
    """Is `path`, relative to the context root, left out of the build context?

    moby's MatchesOrParentMatches: a rule matches a path when it matches the path itself or any of
    its parent directories, and the last matching rule decides.
    """
    parts = PurePosixPath(path).parts
    candidates = ['/'.join(parts[: depth + 1]) for depth in range(len(parts))]
    excluded = False
    for exception, compiled in rules:
        if any(compiled.match(candidate) for candidate in candidates):
            excluded = not exception
    return excluded


def _context_relative(host_path: str, base: Path) -> str | None:
    """Return `host_path` relative to the build context (the repo root), or None when outside it."""
    resolved = Path(os.path.normpath(base / host_path))
    try:
        relative = resolved.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return None
    return None if relative in ('', '.') else relative


def _every_compose_file() -> list[Path]:
    """Every compose file in the repository, globbed so a new one is covered without an edit here."""
    files = sorted(REPO_ROOT.glob('docker-compose*.yaml')) + sorted(REPO_ROOT.glob('docker-compose*.yml'))
    return files + sorted((REPO_ROOT / '.devcontainer').glob('compose*.y*ml'))


def _compose_services() -> Iterator[tuple[Path, str, dict]]:
    for path in _every_compose_file():
        for name, service in (_load_yaml(path).get('services') or {}).items():
            yield path, name, service or {}


def _short_volume_source(entry: str) -> str:
    """The host side of a short-form volume, `source:target[:mode]`.

    Split on the first colon outside a ${...} interpolation: in `${DATA_DIR:-./volume_data}/x:/y`
    the first colon belongs to the fallback, and a plain split(':') reads the source as `${DATA_DIR`.
    """
    depth = 0
    for index, char in enumerate(entry):
        if entry.startswith('${', index):
            depth += 1
        elif char == '}' and depth:
            depth -= 1
        elif char == ':' and not depth:
            return entry[:index]
    return entry


def _bind_sources(service: dict) -> list[str]:
    """The host side of each of a service's volume entries, short or long form."""
    sources = []
    for entry in service.get('volumes') or []:
        if isinstance(entry, str):
            sources.append(_short_volume_source(entry))
        elif isinstance(entry, dict) and entry.get('type', 'bind') == 'bind' and entry.get('source'):
            sources.append(str(entry['source']))
    return sources


def _compose_data_dir_defaults() -> set[str]:
    """Every literal fallback a compose file gives DATA_DIR, e.g. ./volume_data."""
    defaults = set()
    for _, _, service in _compose_services():
        for source in _bind_sources(service):
            match = _DATA_DIR_INTERPOLATION.match(source)
            if match and match.group('default'):
                defaults.add(match.group('default'))
    return defaults


def _under_literal_default(source: str, defaults: set[str]) -> bool:
    normalized = posixpath.normpath(source)
    return any(
        normalized == posixpath.normpath(default) or normalized.startswith(posixpath.normpath(default) + '/')
        for default in defaults
    )


def _compose_data_mounts() -> list[tuple[str, str, list[str]]]:
    """Return (label, host source, [context-relative resolutions]) for every data mount.

    A resolution outside the repository is dropped: it is not in the build context, so it cannot be
    sent. An empty list therefore means "never in the context", which is a pass.
    """
    env_data_dir = _env_file_values(ENV_DEFAULT_FILE).get(DATA_DIR_VARIABLE)
    assert env_data_dir, f'{ENV_DEFAULT_FILE.name} sets no {DATA_DIR_VARIABLE}; nothing to resolve against'
    compose_defaults = _compose_data_dir_defaults()

    mounts = []
    for path, name, service in _compose_services():
        for source in _bind_sources(service):
            match = _DATA_DIR_INTERPOLATION.match(source)
            if match:
                own_default = match.group('default')
                bases = {env_data_dir} | ({own_default} if own_default else compose_defaults)
                host_paths = {f'{base}/{match.group("rest")}' for base in bases}
            elif _under_literal_default(source, compose_defaults):
                host_paths = {source}
            else:
                continue
            resolutions = {_context_relative(host, path.parent) for host in host_paths} - {None}
            mounts.append((f'{path.relative_to(REPO_ROOT)}:{name}', source, sorted(resolutions)))
    return mounts


def _data_mounts_in_context(rules: _IgnoreRules) -> list[str]:
    """Name every data-mount resolution whose directory, or a file inside it, would be sent."""
    offenders = []
    for label, source, resolutions in _compose_data_mounts():
        for relative in resolutions:
            for probe in (relative, f'{relative}/{DATA_DIR_SAMPLE_FILE}'):
                if not _is_excluded_from_context(probe, rules):
                    offenders.append(f'{label} mounts {source}; {probe} is in the build context')
    return offenders


def _compose_build_args() -> list[dict[str, str]]:
    """The build args of every compose service that declares any."""
    return [
        {str(key): str(value) for key, value in service['build']['args'].items()}
        for _, _, service in _compose_services()
        if isinstance(service.get('build'), dict) and isinstance(service['build'].get('args'), dict)
    ]


def _dockerfile_copy_sources_by_origin() -> set[tuple[str, bool]]:
    """(context path, named a build arg?) for every source a COPY or ADD in the Dockerfile reads.

    A COPY --from reads another stage or image, not the context, and is skipped. A source naming a
    build arg is expanded once per compose service's args, and dropped when no service's args
    expand it -- the KNOWN_COPY_SOURCES floor is what notices a source lost that way.
    """
    text = DOCKERFILE.read_text(encoding='utf-8').replace('\\\n', ' ')
    raw = []
    for line in text.splitlines():
        if line.strip().split(maxsplit=1)[:1] not in (['COPY'], ['ADD']):
            continue
        words = shlex.split(line, comments=True)
        if any(word.startswith('--from') for word in words[1:]):
            continue
        operands = [word for word in words[1:] if not word.startswith('--')]
        raw.extend(operands[:-1])

    sources = set()
    for source in raw:
        if '$' not in source:
            sources.add((posixpath.normpath(source), False))
            continue
        for args in _compose_build_args():
            expanded = re.sub(r'\$\{(\w+)\}|\$(\w+)', lambda m, a=args: a.get(m[1] or m[2], m[0]), source)
            if '$' not in expanded:
                sources.add((posixpath.normpath(expanded), True))
    return sources


def _dockerfile_copy_sources() -> set[str]:
    """Every context path a COPY or ADD in the Dockerfile reads, build args expanded from compose."""
    return {source for source, _ in _dockerfile_copy_sources_by_origin()}


def _dockerfile_service_apps() -> set[str]:
    """The service apps: the COPY sources the Dockerfile names through build args, one per service."""
    return {source for source, from_args in _dockerfile_copy_sources_by_origin() if from_args}


def _under_excludable_test_dir(path: str, source: str) -> bool:
    """Does `path`, relative to COPY source `source`, have a directory segment exactly `tests`?

    Only the directories between the source and the file count: the file name is not a directory
    (so `tests.py` is not one), and a `tests` segment inside the source itself is not relative to it.
    """
    relative = PurePosixPath(path).relative_to(source)
    return EXCLUDABLE_TEST_DIR in relative.parts[:-1]


def _copy_sources_excluded(rules: _IgnoreRules) -> list[str]:
    """Name every COPY source, or git-tracked file under one, that the build context would leave out.

    A tracked file under a `tests` directory of its COPY source may be left out (tj-v82dvm); whether
    anything the apps import is left out is the import closure's question, not this one's.
    """
    offenders = []
    for source in sorted(_dockerfile_copy_sources()):
        if _is_excluded_from_context(source, rules):
            offenders.append(f'COPY source {source} itself')
        offenders.extend(
            f'{tracked} (under COPY source {source})'
            for tracked in _run('git', 'ls-files', '--', source)
            if _is_excluded_from_context(tracked, rules) and not _under_excludable_test_dir(tracked, source)
        )
    return offenders


def _copy_source_test_dirs() -> dict[str, list[str]]:
    """Every `tests` directory under a COPY source that holds a git-tracked file, with those files.

    The same rule _under_excludable_test_dir applies: only directories between the source and the
    file count. A nested tests/.../tests/ yields both directories.
    """
    test_dirs: dict[str, list[str]] = {}
    for source in sorted(_dockerfile_copy_sources()):
        for tracked in _run('git', 'ls-files', '--', source):
            parts = PurePosixPath(tracked).relative_to(source).parts[:-1]
            for depth, part in enumerate(parts):
                if part == EXCLUDABLE_TEST_DIR:
                    directory = PurePosixPath(source, *parts[: depth + 1]).as_posix()
                    test_dirs.setdefault(directory, []).append(tracked)
    return test_dirs


def _copy_source_test_dirs_sent(rules: _IgnoreRules, test_dirs: dict[str, list[str]]) -> dict[str, list[str]]:
    """Per `tests` directory, the tracked files under it the build context would still send."""
    sent = {
        directory: sorted(tracked for tracked in files if not _is_excluded_from_context(tracked, rules))
        for directory, files in test_dirs.items()
    }
    return {directory: files for directory, files in sorted(sent.items()) if files}


def _first_party_module_file(module: str) -> Path | None:
    """The repo source file a dotted module name resolves to, or None when it is not first-party."""
    base = REPO_ROOT.joinpath(*module.split('.'))
    for candidate in (base.with_suffix('.py'), base / '__init__.py'):
        if candidate.is_file():
            return candidate
    return None


def _imported_module_names(path: Path) -> list[str]:
    """Every module a source file imports, anywhere in it, relative imports resolved to absolute names.

    ast.walk reaches every node, so an import under `if TYPE_CHECKING:`, inside a function or behind
    a flag is found like a top-level one. `from x import y` yields both x and x.y, because y may be a
    submodule rather than a name defined in x.
    """
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    package = list(path.relative_to(REPO_ROOT).parent.parts)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)]
                module = '.'.join([*base, *([node.module] if node.module else [])])
            else:
                module = node.module or ''
            found.append(module)
            found.extend(f'{module}.{alias.name}' for alias in node.names)
    return found


def _package_inits(path: Path) -> set[Path]:
    """Each package __init__.py between the repo root and `path`, where one exists."""
    relative = path.relative_to(REPO_ROOT)
    return {
        init
        for depth in range(1, len(relative.parts))
        if (init := REPO_ROOT.joinpath(*relative.parts[:depth], '__init__.py')).is_file()
    }


def _app_import_closure(app: str) -> set[Path]:
    """Every first-party file a service app loads: its own modules, what they import, transitively.

    Test packages are followed like any other: the point is to find a production import of one.
    Each package __init__.py on a reached file's path runs when that file is imported, so it is
    walked like any other reached file, not merely added: what it imports is loaded too.
    """
    pending = sorted((REPO_ROOT / app).rglob('*.py'))
    reached: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in reached:
            continue
        reached.add(path)
        pending.extend(_package_inits(path))
        for module in _imported_module_names(path):
            target = _first_party_module_file(module)
            if target is not None:
                pending.append(target)
    return reached


def _is_under(path: str, source: str) -> bool:
    """Is context path `path` the COPY source `source` itself, or inside it?"""
    return path == source or path.startswith(f'{source}/')


def _dockerfile_static_copy_sources() -> set[str]:
    """The COPY sources every service image shares: those the Dockerfile names without a build arg."""
    return {source for source, from_args in _dockerfile_copy_sources_by_origin() if not from_args}


_UNCOPIED_INIT_WITH_CODE = (
    'an uncopied ancestor package __init__.py with statements: the host runs them, the image never '
    'does. COPY it, or move its code into a copied module'
)


def _has_statements(path: Path) -> bool:
    """Does the module do anything on import? A lone module docstring, comments and blank lines do not."""
    body = ast.parse(path.read_text(encoding='utf-8'), filename=str(path)).body
    first = body[0] if body else None
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
        body = body[1:]
    return bool(body)


def _app_imports_outside_image(app: str, static_sources: set[str]) -> list[str]:
    """Every file of `app`'s import closure that its own image does not COPY.

    The image holds the shared static sources and this app's own source -- never another service's
    app. One carve-out: a package __init__.py whose directory is a strict ancestor of one of those
    sources (data/__init__.py above data/store/app) is not copied, and Python imports that package
    as a namespace package inside the image. That is equivalent to the host only while the file does
    nothing, so the carve-out holds only for one that is empty or a lone docstring; one with any
    statement is named, annotated with why. The closure walks such an __init__.py either way, so
    anything it imports must itself be copied.
    """
    own = static_sources | {app}
    ancestors = {parent.as_posix() for source in own for parent in PurePosixPath(source).parents} - {'.'}
    offenders = []
    for path in _app_import_closure(app):
        relative = path.relative_to(REPO_ROOT).as_posix()
        if any(_is_under(relative, source) for source in own):
            continue
        if path.name == '__init__.py' and PurePosixPath(relative).parent.as_posix() in ancestors:
            if _has_statements(path):
                offenders.append(f'{relative} ({_UNCOPIED_INIT_WITH_CODE})')
            continue
        offenders.append(relative)
    return sorted(offenders)


def _app_imports_excluded(rules: _IgnoreRules) -> dict[str, list[str]]:
    """Per service app, every file of its import closure the build context would leave out."""
    return {
        app: sorted(
            relative
            for path in _app_import_closure(app)
            if _is_excluded_from_context(relative := path.relative_to(REPO_ROOT).as_posix(), rules)
        )
        for app in sorted(_dockerfile_service_apps())
    }


# Docker's own semantics, pinned so the matcher the checks below rely on cannot drift into something
# laxer. Each row: (.dockerignore text, path relative to the context, excluded?).
_DOCKERIGNORE_CASES = {
    'dir-rule-excludes-the-dir': ('volumes/', 'volumes', True),
    'dir-rule-excludes-a-descendant': ('volumes/', 'volumes/trader_joe/postgres/PG_VERSION', True),
    'anchored-at-the-root': ('volumes/', 'data/volumes/x', False),
    'leading-slash-is-the-root': ('/volumes', 'volumes/x', True),
    'star-stays-in-one-segment': ('*/postgres', 'volumes/trader_joe/postgres', False),
    'star-matches-one-segment': ('volumes/*/postgres', 'volumes/trader_joe/postgres', True),
    'double-star-any-depth': ('**/postgres', 'volumes/trader_joe/postgres/base', True),
    'double-star-zero-depth': ('**/postgres', 'postgres', True),
    'a-prefix-is-not-a-match': ('volume', 'volume_data/x', False),
    'question-mark-is-one-char': ('volume?data', 'volume_data', True),
    'character-class': ('**/*.py[cod]', 'common/x.pyc', True),
    'negation-re-includes': ('volumes/\n!volumes/keep', 'volumes/keep', False),
    'last-match-wins': ('!volumes/keep\nvolumes/', 'volumes/keep', True),
    'comment-is-not-a-rule': ('# volumes/', 'volumes', False),
}

# The short-volume split, including the fallback colon that a plain split(':') gets wrong.
_SHORT_VOLUME_CASES = {
    'fallback-colon-kept': (
        '${DATA_DIR:-./volume_data}/postgres:/var/lib/postgresql/data',
        '${DATA_DIR:-./volume_data}/postgres',
    ),
    'plain-with-mode': ('./common:/code/common:ro', './common'),
    'bare-variable': ('${DATA_DIR}/kafka:/var/lib/kafka/', '${DATA_DIR}/kafka'),
    'named-volume-only': ('pgdata', 'pgdata'),
}


@pytest.mark.build_infra
@pytest.mark.parametrize(('entry', 'source'), list(_SHORT_VOLUME_CASES.values()), ids=list(_SHORT_VOLUME_CASES))
def test_short_volume_source_splits_outside_interpolation(entry: str, source: str):
    assert _short_volume_source(entry) == source


@pytest.mark.build_infra
@pytest.mark.parametrize(
    ('text', 'path', 'excluded'), list(_DOCKERIGNORE_CASES.values()), ids=list(_DOCKERIGNORE_CASES)
)
def test_dockerignore_matcher_follows_docker_semantics(text: str, path: str, excluded: bool):
    """The emulation the build-context checks rely on agrees with Docker on each rule shape."""
    assert _is_excluded_from_context(path, _dockerignore_rules(text)) is excluded


@pytest.mark.build_infra
def test_compose_declares_data_mounts_under_data_dir():
    """Non-vacuity: the derivation finds data mounts, the database's among them, and a fallback."""
    labels = {label for label, _, _ in _compose_data_mounts()}
    assert 'docker-compose.yaml:postgres' in labels, (
        f'no DATA_DIR bind mount found for postgres in docker-compose.yaml (found {sorted(labels)}), '
        f'so the build-context check would judge nothing'
    )
    assert _compose_data_dir_defaults(), 'no compose file gives DATA_DIR a fallback; nothing to resolve against'


@pytest.mark.build_infra
def test_every_data_dir_mount_is_parsed():
    """tj-c4mosr.5 (00:55 gap (2)): a DATA_DIR-sourced mount the parser cannot read fails, never skips.

    The agent-stack overlay mounts ${DATA_DIR:?...}/postgres and /kafka; the build-context check below
    judges only the mounts _compose_data_mounts yields, so an unparsed spelling was a silent pass.
    """
    unparsed = [
        f'{path.relative_to(REPO_ROOT)}:{name} {source}'
        for path, name, service in _compose_services()
        for source in _bind_sources(service)
        if re.match(rf'^\$\{{?{DATA_DIR_VARIABLE}(?!\w)', source) and not _DATA_DIR_INTERPOLATION.match(source)
    ]
    assert not unparsed, f'DATA_DIR mounts the build-context check cannot read, so it would skip them: {unparsed}'
    labels = {label for label, _, _ in _compose_data_mounts()}
    assert {'docker-compose.agent-stack.yaml:postgres', 'docker-compose.agent-stack.yaml:kafka'} <= labels, sorted(
        labels
    )


@pytest.mark.build_infra
def test_dockerignore_excludes_every_compose_data_mount():
    """Every host data directory, under either DATA_DIR resolution, stays out of the build context."""
    offenders = _data_mounts_in_context(_committed_dockerignore_rules())
    assert not offenders, (
        f'{DOCKERIGNORE_FILE.name} lets host data into the build context, and a build started while a '
        f'stack is up fails with "permission denied" on the 0700 database directory: {offenders}. '
        f'Add the directory to {DOCKERIGNORE_FILE.name}.'
    )


@pytest.mark.build_infra
def test_dockerignore_excludes_no_dockerfile_copy_source():
    """What the Dockerfile COPYs from the context is sent, all of it."""
    sources = _dockerfile_copy_sources()
    assert sources >= KNOWN_COPY_SOURCES, f'the COPY parse found {sorted(sources)}; it lost a source it used to find'
    missing = sorted(source for source in sources if not (REPO_ROOT / source).exists())
    assert not missing, f'COPY sources that do not exist in the checkout: {missing}'
    offenders = _copy_sources_excluded(_committed_dockerignore_rules())
    assert not offenders, (
        f'{DOCKERIGNORE_FILE.name} keeps what the image COPYs out of the build context: {offenders}. '
        f'Only files under a {EXCLUDABLE_TEST_DIR}/ directory of a COPY source may be excluded.'
    )


# The one exception the COPY-source check allows, pinned at its edges. Each row: (COPY source,
# tracked path, may be excluded?). A near-miss name is the failure worth pinning: a pattern that
# excludes `tests_util/` or `testsuite/` would take production code with it.
_EXCLUDABLE_TEST_DIR_CASES = {
    'tests-dir-under-source': ('common', 'common/tests/x.py', True),
    'nested-tests-dir-deep-file': ('routers', 'routers/tests/interface_manifest/data_store.manifest', True),
    'tests-dir-in-a-subpackage': ('common', 'common/kafka/tests/x.py', True),
    'tests-prefix-dir-is-not-tests': ('common', 'common/tests_util/x.py', False),
    'tests-suffix-dir-is-not-tests': ('common', 'common/testsuite/x.py', False),
    'tests-module-is-not-a-dir': ('common', 'common/tests.py', False),
    'production-file': ('common', 'common/database/sql_alchemy_table.py', False),
    'file-source-itself': ('pyproject.toml', 'pyproject.toml', False),
    'tests-segment-inside-the-source-does-not-count': ('common/tests', 'common/tests/x.py', False),
}


@pytest.mark.build_infra
@pytest.mark.parametrize(
    ('source', 'path', 'excludable'), list(_EXCLUDABLE_TEST_DIR_CASES.values()), ids=list(_EXCLUDABLE_TEST_DIR_CASES)
)
def test_only_a_tests_dir_under_a_copy_source_may_be_excluded(source: str, path: str, excludable: bool):
    assert _under_excludable_test_dir(path, source) is excludable


@pytest.mark.build_infra
def test_the_copy_source_check_allows_a_tests_dir_and_nothing_else():
    """The exception, through the check itself: a tests/ rule passes, a production directory does not.

    Judged against these rules alone, not the committed file, so this pins the check, not the file.
    """
    assert not _copy_sources_excluded(_dockerignore_rules('common/tests/'))
    offenders = _copy_sources_excluded(_dockerignore_rules('common/database/'))
    assert offenders, 'excluding common/database/ went unnoticed'
    assert all(offender.startswith('common/database/') for offender in offenders), offenders


@pytest.mark.build_infra
def test_the_service_apps_are_derived_from_the_dockerfile_and_their_closures_are_real():
    """Non-vacuity for the import-closure check: it finds both apps, and each closure leaves its app."""
    apps = _dockerfile_service_apps()
    assert apps >= set(KNOWN_APP_IMPORTS), f'the COPY parse found service apps {sorted(apps)}'
    for app, known in KNOWN_APP_IMPORTS.items():
        closure = {path.relative_to(REPO_ROOT).as_posix() for path in _app_import_closure(app)}
        assert known in closure, f'the import closure of {app} does not reach {known}'
        assert len(closure) >= APP_CLOSURE_FLOOR, f'the import closure of {app} reached only {len(closure)} files'


@pytest.mark.build_infra
def test_the_import_closure_follows_guarded_and_deferred_imports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The walk reads imports a runtime check would miss, wherever they sit.

    Under TYPE_CHECKING, inside a function, and in a package __init__.py that runs only because a
    module under it is imported.
    """
    (tmp_path / 'app').mkdir()
    (tmp_path / 'lib').mkdir()
    (tmp_path / 'pkg').mkdir()
    (tmp_path / 'lib' / '__init__.py').write_text('', encoding='utf-8')
    for name in ('guarded', 'lazy', 'relative'):
        (tmp_path / 'lib' / f'{name}.py').write_text('', encoding='utf-8')
    (tmp_path / 'lib' / 'entry.py').write_text('from . import relative\n', encoding='utf-8')
    # Nothing imports pkg.hidden but pkg/__init__.py, which the app never names.
    (tmp_path / 'pkg' / '__init__.py').write_text('from . import hidden\n', encoding='utf-8')
    (tmp_path / 'pkg' / 'hidden.py').write_text('', encoding='utf-8')
    (tmp_path / 'pkg' / 'used.py').write_text('', encoding='utf-8')
    (tmp_path / 'app' / 'main.py').write_text(
        'from typing import TYPE_CHECKING\n'
        'import lib.entry\n'
        'import pkg.used\n'
        'if TYPE_CHECKING:\n'
        '    from lib import guarded\n'
        'def later():\n'
        '    import lib.lazy\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(sys.modules[__name__], 'REPO_ROOT', tmp_path)
    closure = {path.relative_to(tmp_path).as_posix() for path in _app_import_closure('app')}
    assert closure == {
        'app/main.py',
        'lib/__init__.py',
        'lib/entry.py',
        'lib/guarded.py',
        'lib/lazy.py',
        'lib/relative.py',
        'pkg/__init__.py',
        'pkg/hidden.py',
        'pkg/used.py',
    }


@pytest.mark.build_infra
def test_the_copy_coverage_check_names_what_the_image_does_not_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Only the shared sources and the app's own source are in its image, never another service's app.

    Synthetic tree: two service apps under svc/, one shared source. An ancestor package __init__.py
    of the app's own source is allowed (a namespace package in the image) while it is empty or a
    lone docstring (svc/__init__.py); one with a statement (svc/a/__init__.py) is named, and still
    walked, so the stray it imports is named too. A copied __init__.py may do anything.
    """
    files = {
        'shared/__init__.py': 'X = 1\n',
        'shared/util.py': '',
        'svc/__init__.py': '"""Services."""\n# a comment is not a statement\n',
        'svc/a/__init__.py': 'import stray_from_init\n',
        'svc/a/app/__init__.py': '',
        'svc/a/app/main.py': 'import shared.util\nimport stray\nfrom svc.b.app import api\n',
        'svc/b/__init__.py': '',
        'svc/b/app/__init__.py': '',
        'svc/b/app/api.py': 'from shared import util\n',
        'stray.py': '',
        'stray_from_init.py': '',
    }
    for relative, text in files.items():
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text(text, encoding='utf-8')
    monkeypatch.setattr(sys.modules[__name__], 'REPO_ROOT', tmp_path)
    assert _app_imports_outside_image('svc/a/app', {'shared'}) == [
        'stray.py',
        'stray_from_init.py',
        f'svc/a/__init__.py ({_UNCOPIED_INIT_WITH_CODE})',
        'svc/b/__init__.py',
        'svc/b/app/__init__.py',
        'svc/b/app/api.py',
    ]
    assert _app_imports_outside_image('svc/b/app', {'shared'}) == []


# An uncopied ancestor package __init__.py, by content. Each row: (its text, allowed?). Allowed
# means the image, importing the package as a namespace package, behaves as the host does.
_ANCESTOR_INIT_CASES = {
    'empty': ('', True),
    'comments-and-blank-lines': ('# nothing here\n\n', True),
    'docstring-only': ('"""The package."""\n', True),
    'docstring-and-comment': ('"""The package."""\n# trailing comment\n', True),
    'assignment': ('X = 1\n', False),
    'docstring-then-assignment': ('"""The package."""\nX = 1\n', False),
    'third-party-import': ('import os\n', False),
    'a-second-string-is-a-statement': ('"""The package."""\n"""Not a docstring."""\n', False),
    'a-leading-non-string-constant': ('1\n', False),
}


@pytest.mark.build_infra
@pytest.mark.parametrize(('text', 'allowed'), list(_ANCESTOR_INIT_CASES.values()), ids=list(_ANCESTOR_INIT_CASES))
def test_an_uncopied_ancestor_init_is_allowed_only_without_statements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str, allowed: bool
):
    """The carve-out, through the check itself: a namespace package in the image runs none of this."""
    files = {
        'shared/__init__.py': '',
        'svc/__init__.py': text,
        'svc/a/app/__init__.py': '',
        'svc/a/app/main.py': 'import shared\n',
    }
    for relative, content in files.items():
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text(content, encoding='utf-8')
    monkeypatch.setattr(sys.modules[__name__], 'REPO_ROOT', tmp_path)
    expected = [] if allowed else [f'svc/__init__.py ({_UNCOPIED_INIT_WITH_CODE})']
    assert _app_imports_outside_image('svc/a/app', {'shared'}) == expected


@pytest.mark.build_infra
def test_each_service_app_imports_only_what_its_image_copies():
    """The other half of the build-context purpose: what each app loads is COPYed into its image.

    Sent (the .dockerignore check) is not enough: a file the Dockerfile never COPYs -- another
    service's app, a new top-level package -- is in the context and still missing at import.
    """
    static_sources = _dockerfile_static_copy_sources()
    apps = _dockerfile_service_apps()
    assert static_sources >= KNOWN_COPY_SOURCES - apps, f'the COPY parse found static sources {sorted(static_sources)}'
    assert apps >= set(KNOWN_APP_IMPORTS), f'the COPY parse found service apps {sorted(apps)}'
    assert not static_sources & apps, f'a service app is also a static COPY source: {sorted(static_sources & apps)}'
    outside = {app: files for app in sorted(apps) if (files := _app_imports_outside_image(app, static_sources))}
    assert not outside, (
        f'service apps import files their image does not COPY (only {sorted(static_sources)} and the '
        f"app's own source are copied), so the image builds and fails at import: {outside}"
    )


@pytest.mark.build_infra
def test_dockerignore_sends_every_module_the_service_apps_import():
    """The purpose behind the COPY-source check: what each app loads is in the build context."""
    excluded = {app: files for app, files in _app_imports_excluded(_committed_dockerignore_rules()).items() if files}
    assert not excluded, (
        f'{DOCKERIGNORE_FILE.name} keeps modules the service apps import out of the build context, so the '
        f'image builds and fails at import: {excluded}'
    )


@pytest.mark.build_infra
def test_the_test_dirs_under_the_copy_sources_are_derived_and_real():
    """Non-vacuity for the reverse pin: the derivation finds at least the known test directories."""
    found = set(_copy_source_test_dirs())
    assert found >= KNOWN_COPY_SOURCE_TEST_DIRS, (
        f'the COPY parse and git ls-files found test directories {sorted(found)}; '
        f'expected at least {sorted(KNOWN_COPY_SOURCE_TEST_DIRS)}'
    )


@pytest.mark.build_infra
def test_dockerignore_excludes_every_tests_dir_under_a_copy_source():
    """The reverse of the COPY-source check: no test suite under a copied package ships in an image."""
    sent = _copy_source_test_dirs_sent(_committed_dockerignore_rules(), _copy_source_test_dirs())
    assert not sent, (
        f'{DOCKERIGNORE_FILE.name} sends test directories under a Dockerfile COPY source into every '
        f'service image: {sorted(sent)} ({sent}). Add each as `<dir>/` to {DOCKERIGNORE_FILE.name} (tj-v82dvm).'
    )


@pytest.mark.build_infra
@pytest.mark.parametrize('dropped', sorted(KNOWN_COPY_SOURCE_TEST_DIRS))
def test_the_test_dir_check_names_the_one_dir_left_in(dropped: str):
    """The check against rules built here: leave one directory in, or re-include a file, and it is named.

    Every found directory excluded is clean; dropping one names exactly that directory; re-including
    one tracked file under it names exactly that file.
    """
    test_dirs = _copy_source_test_dirs()
    assert not _copy_source_test_dirs_sent(_dockerignore_rules('\n'.join(f'{d}/' for d in test_dirs)), test_dirs)
    left_in = _dockerignore_rules('\n'.join(f'{d}/' for d in test_dirs if d != dropped))
    assert set(_copy_source_test_dirs_sent(left_in, test_dirs)) == {dropped}
    re_included = _dockerignore_rules('\n'.join([*(f'{d}/' for d in test_dirs), f'!{test_dirs[dropped][0]}']))
    assert _copy_source_test_dirs_sent(re_included, test_dirs) == {dropped: [test_dirs[dropped][0]]}


# ---------------------------------------------------------------------------------------
# CAPTURED OUTPUT (tj-ijpys9.20). Wherever a workflow reads text back -- a value from the staged
# env file, the stdout of a compose run -- the text must be exactly what it is taken for: the
# last value for a key, all of it; a container's output with no pull progress mixed in. And
# nothing in common/ may print at import, since whatever imports it inside such a run writes
# into the capture (the Migrate Database failure, tj-ijpys9.19).
# ---------------------------------------------------------------------------------------

# Every spelling of the checkout root a step can put in front of an env file: none, `./`, $PWD and
# $GITHUB_WORKSPACE (braced or not), and the `${{ github.workspace }}` expression (tj-0pobey.4).
_CHECKOUT_ROOT = (
    r'(?:\./|\$\{PWD\}/|\$PWD/|\$\{GITHUB_WORKSPACE\}/|\$GITHUB_WORKSPACE/|\$\{\{\s*github\.workspace\s*\}\}/)?'
)
# An env file the finder can place, as a whole shell word: the staged root file, or a service's
# own (`data/store/`, `data/ingest/`), under any root spelling. The `service` group is set for the
# latter. Never `.env.default`; never a path under some other directory.
_KNOWN_ENV_PATH = re.compile(
    rf'(?<![^\s"\'=(<>|;&]){_CHECKOUT_ROOT}(?P<service>data/(?:store|ingest)/)?'
    rf'{re.escape(STAGED_ENV_FILE)}(?![\w.-])'
)
# The env file's name as a path component, however it is prefixed: what _KNOWN_ENV_PATH must account for.
_ANY_ENV_PATH = re.compile(rf'(?<![\w.-]){re.escape(STAGED_ENV_FILE)}(?![\w.-])')
# The text before a word that makes the word the value of a shell assignment, `NAME=` or `NAME="`.
_ASSIGNED_VALUE = re.compile(r'(?:^|[\s;&|(])[A-Za-z_]\w*=$')
# Commands that name the staged file only to create, replace or remove it.
_STAGED_ENV_WRITERS = frozenset({'cp', 'mv', 'rm', 'shred', 'touch', 'chmod', 'install'})
# Where one simple command ends and the next begins, for the text around a word.
_COMMAND_BOUNDARY = re.compile(r'\|\||&&|\||;|\$\(|(?<![$\w])\{\s|\(')
_PIPELINE_END = re.compile(r'\|\||&&|;|\)')
_ONE_LINE_FUNCTION = re.compile(r'^(\w+)\(\)\s*\{\s.*;\s*\}$')
_CUT_OPTIONS = re.compile(r'(?<![\w-])cut\s+([^|;)]*)')


def _without_single_quoted(text: str) -> str:
    """Text with every closed single-quoted string emptied. Double quotes are kept: `"$(...)"` runs."""
    return re.sub(r"'[^']*'", "''", text)


def _unquoted(text: str) -> str:
    """Text with every closed quoted string emptied, so an operator inside quotes is not one."""
    return re.sub(r'"[^"]*"', '""', _without_single_quoted(text))


def _cut_fields(options: str) -> tuple[str | None, str | None]:
    """(delimiter, field list) of a cut command's option text: `-d= -f2-` -> ('=', '2-').

    The text may end inside a quote it did not open -- `"$(... | cut -d= -f2)"` -- so words are
    split by whitespace and stripped of quotes when shlex cannot balance them.
    """
    try:
        words = shlex.split(options)
    except ValueError:
        words = [word.strip('\'"') for word in options.split()]
    delimiter = fields = None
    for position, word in enumerate(words):
        following = words[position + 1] if position + 1 < len(words) else None
        for short, long in (('-d', '--delimiter'), ('-f', '--fields')):
            value = None
            if word in (short, long):
                value = following
            elif word.startswith(f'{long}='):
                value = word[len(long) + 1 :]
            elif word.startswith(short):
                value = word[len(short) :]
            if value is not None:
                if short == '-d':
                    delimiter = value
                else:
                    fields = value
    return delimiter, fields


def _unplaced_env_paths(line: str) -> list[str]:
    """Each word on a line naming an env file under a directory _KNOWN_ENV_PATH does not recognise."""
    placed = {match.end() for match in _KNOWN_ENV_PATH.finditer(line)}
    return [
        re.split(r'[\s=(<>|;&]', line[: match.end()])[-1].strip('\'"')
        for match in _ANY_ENV_PATH.finditer(line)
        if match.end() not in placed
    ]


def _staged_env_reads(lines: list[str]) -> list[tuple[str, str]]:
    """Each read of the staged env file, as (its line, the pipeline its output flows through).

    The file is found under every spelling of the checkout root. A word that is only the value of
    a `NAME=` assignment is not a read (a `$(...)` on the right of `=` still is), and neither is a
    redirection target or the operand of a writer. An env file under any directory the finder
    cannot place fails here, naming the line and the word, rather than passing as a non-read
    (tj-0pobey.4: that silence is how directory-prefixed reads went unseen).
    """
    reads = []
    for line in lines:
        unplaced = _unplaced_env_paths(line)
        assert not unplaced, (
            f'{unplaced} name an env file under a directory the staged-{STAGED_ENV_FILE} finder does not '
            f'recognise, so it cannot tell whether this reads the staged file: {line!r}. Spell the checkout '
            f'root as the finder does, or teach _KNOWN_ENV_PATH the new spelling.'
        )
        for match in _KNOWN_ENV_PATH.finditer(line):
            if match.group('service'):
                continue
            before, after = line[: match.start()], line[match.end() :]
            if before[-1:] in ('"', "'"):
                quote, before = before[-1], before[:-1]
                after = after.removeprefix(quote)
            if _ASSIGNED_VALUE.search(before) or before.rstrip().endswith('>'):
                continue
            command = shlex.split(_COMMAND_BOUNDARY.split(_unquoted(before))[-1] or ':')
            name = _command_name(command)
            if name in _STAGED_ENV_WRITERS or (name == 'sed' and any(word.startswith('-i') for word in command)):
                continue
            reads.append((line, _PIPELINE_END.split(_unquoted(after), maxsplit=1)[0]))
    return reads


def _takes_last_line_and_full_value(pipeline: str) -> bool:
    """Whether a read's downstream pipeline keeps only the last line and cuts everything after `=`."""
    stages = [stage.strip() for stage in pipeline.split('|')[1:]]
    last_line = any(re.fullmatch(r'tail\s+(?:-n\s*1|-1|--lines[= ]1)', stage) for stage in stages)
    full_value = any(
        _cut_fields(match.group(1)) == ('=', '2-') for stage in stages if (match := _CUT_OPTIONS.match(stage))
    )
    return last_line and full_value


def _every_workflow_step() -> Iterator[tuple[str, dict]]:
    for workflow in _workflow_files():
        for job_id, job in ((_load_yaml(workflow) or {}).get('jobs') or {}).items():
            for step in (job or {}).get('steps') or []:
                yield f'{workflow.name} {job_id} / {step.get("name")}', step


_ENV_VALUE_CASES = {
    'the last line for a key wins': ('KEY=first\nKEY=second\n', 'second'),
    'the value is everything after the first =': ('KEY=a=b=c\n', 'a=b=c'),
    'a longer key sharing the prefix is not the key': ('KEY=right\nKEY_OTHER=wrong\n', 'right'),
}


@pytest.mark.build_infra
def test_every_staged_env_read_takes_the_last_line_and_the_full_value(tmp_path: Path):
    """tj-ijpys9.20 item 3: every read of the staged env file is last-wins and full-value.

    Compose takes the LAST line for a key, and a value may itself hold `=` (a base64 secret
    does). A step reading the file must agree with compose, or it tests a different value from
    the one the stack runs with: `tail -n 1` then `cut -d= -f2-`. Reads are found by the file's
    name as a word in any step of any workflow, writes (redirection, cp, sed -i, shred, rm)
    excepted. A one-line helper that does the read is also RUN, by bash, against a synthetic
    file in a scratch directory, so the property is shown, not only spelled.
    """
    reads, offenders, helpers = [], [], {}
    for where, step in _every_workflow_step():
        for line, pipeline in _staged_env_reads(_step_lines(step)):
            reads.append(where)
            if not _takes_last_line_and_full_value(pipeline):
                offenders.append(f'{where}: {line}')
            elif match := _ONE_LINE_FUNCTION.match(line):
                helpers.setdefault(line, (where, match.group(1)))
    assert reads, f'no step reads the staged {STAGED_ENV_FILE}, so this check judged nothing'
    assert not offenders, (
        f'these read the staged {STAGED_ENV_FILE} without `tail -n 1 | cut -d= -f2-`, so a repeated key or '
        f'a value holding `=` reads differently from compose: {offenders}'
    )
    assert helpers, f'no step reads the staged {STAGED_ENV_FILE} through a one-line helper to exercise'

    assert shutil.which('bash'), 'bash is not on PATH, so the env helper cannot be exercised'
    wrong = []
    for definition, (where, function) in helpers.items():
        for case, (content, want) in _ENV_VALUE_CASES.items():
            (tmp_path / STAGED_ENV_FILE).write_text(content, encoding='utf-8')
            result = subprocess.run(
                ['bash', '-c', f'set -euo pipefail\n{definition}\n{function} KEY'],
                cwd=tmp_path,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0 or result.stdout.rstrip('\n') != want:
                wrong.append(f'{where} {function}(), {case}: got {result.stdout!r} (exit {result.returncode})')
    assert not wrong, f'the staged-env helper does not read as compose does: {wrong}'


@pytest.mark.build_infra
def test_no_step_reads_an_env_value_with_the_second_field_alone():
    """tj-ijpys9.20 item 3: `cut -d= -f2` truncates a value at its second `=`; nothing may use it."""
    offenders = [
        f'{where}: {line}'
        for where, step in _every_workflow_step()
        for line in _step_lines(step)
        if _cuts_second_field(line)
    ]
    assert not offenders, f'these cut an env value at its second `=`; use `cut -d= -f2-`: {offenders}'


def _cuts_second_field(line: str) -> bool:
    """Whether a line runs `cut -d= -f2` (the second field alone), inside a substitution or not."""
    return any(_cut_fields(match.group(1)) == ('=', '2') for match in _CUT_OPTIONS.finditer(line))


@pytest.mark.build_infra
@pytest.mark.parametrize(
    ('line', 'truncates'),
    [
        ('X="$(grep -E \'^X=\' env | cut -d= -f2)"', True),
        ('f() { grep -E "^$1=" env | tail -n 1 | cut -d= -f2; }', True),
        ("cut -d '=' -f 2 < env", True),
        ('f() { grep -E "^$1=" env | tail -n 1 | cut -d= -f2-; }', False),
        ('cut -d: -f2 /etc/passwd', False),
    ],
    ids=['substituted', 'in-a-helper', 'spaced', 'full-value', 'other-delimiter'],
)
def test_the_second_field_rule_finds_every_spelling(line: str, truncates: bool):
    """The rule above, on synthetic lines: a `-f2` inside `"$(...)"` is found, however it is spaced."""
    assert _cuts_second_field(line) is truncates


@pytest.mark.build_infra
@pytest.mark.parametrize(
    ('pipeline', 'accepted'),
    [
        (' | tail -n 1 | cut -d= -f2-', True),
        (' | tail -n 1 | cut -d = -f 2-', True),
        (' | cut -d= -f2', False),
        (' | tail -n 1 | cut -d= -f2', False),
        (' | cut -d= -f2-', False),
        (' | head -n 1 | cut -d= -f2-', False),
        ('', False),
    ],
    ids=['canonical', 'spaced-options', 'old-first-match', 'last-but-truncated', 'full-but-first', 'head', 'bare'],
)
def test_the_last_wins_rule_judges_the_pipeline(pipeline: str, accepted: bool):
    """The rule above, on synthetic pipelines, so a weakened parser cannot pass the workflow vacuously."""
    assert _takes_last_line_and_full_value(pipeline) is accepted


@pytest.mark.build_infra
def test_writes_to_the_staged_env_file_are_not_taken_for_reads():
    """The reader finder skips what only creates, edits or removes the file, and finds a real read."""
    lines = [
        f'cp artifact/.env.default ./{STAGED_ENV_FILE}',
        f'printf \'KEY=%s\\n\' "${{value}}" >> {STAGED_ENV_FILE}',
        f"sed -i 's/^KEY=.*/KEY=/' {STAGED_ENV_FILE}",
        f'shred -u {STAGED_ENV_FILE} || rm -f {STAGED_ENV_FILE}',
        f'cp artifact/data/store/.env.default data/store/{STAGED_ENV_FILE}',
        f'X="$(grep -E \'^X=\' {STAGED_ENV_FILE} | cut -d= -f2)"',
    ]
    reads = _staged_env_reads(lines)
    assert [line for line, _ in reads] == [lines[-1]], f'reads found: {reads}'
    assert not _takes_last_line_and_full_value(reads[0][1])


# Every spelling of the checkout root a workflow step can put in front of the staged file (the
# steps run at the root, with no working-directory default). Spelled out here, not taken from the
# finder, so a finder that forgets one cannot also forget to test it (tj-0pobey.4).
_ROOT_SPELLINGS = {
    'bare': '',
    'dot': './',
    'braced-pwd': '${PWD}/',
    'pwd': '$PWD/',
    'braced-workspace': '${GITHUB_WORKSPACE}/',
    'workspace': '$GITHUB_WORKSPACE/',
    'workspace-expression': '${{ github.workspace }}/',
}
_QUOTINGS = {'unquoted': '', 'double-quoted': '"', 'single-quoted': "'"}


@pytest.mark.build_infra
@pytest.mark.parametrize('quote', _QUOTINGS.values(), ids=_QUOTINGS.keys())
@pytest.mark.parametrize('prefix', _ROOT_SPELLINGS.values(), ids=_ROOT_SPELLINGS.keys())
def test_every_spelling_of_the_checkout_root_is_a_read(prefix: str, quote: str):
    """tj-0pobey.4 item 1: a directory prefix does not hide a read, and the pipeline is still judged.

    The finder used to see only `.env` and `./.env`, so `grep K "${PWD}/.env" | cut -d= -f2` --
    first match, truncated value -- passed the last-wins check unseen.
    """
    path = f'{quote}{prefix}{STAGED_ENV_FILE}{quote}'
    truncating = f'grep -E "^KEY=" {path} | cut -d= -f2'
    canonical = f'grep -E "^KEY=" {path} | tail -n 1 | cut -d= -f2-'
    reads = _staged_env_reads([truncating, canonical])
    assert [line for line, _ in reads] == [truncating, canonical], f'reads found: {reads}'
    assert [_takes_last_line_and_full_value(pipeline) for _, pipeline in reads] == [False, True]


@pytest.mark.build_infra
@pytest.mark.parametrize(
    'line',
    [
        f'ROOT_ENV_FILE=./{STAGED_ENV_FILE} docker compose -f docker-compose.yaml config --quiet',
        f'ROOT_ENV_FILE="${{PWD}}/{STAGED_ENV_FILE}" docker compose -f docker-compose.yaml config --quiet',
        f"ROOT_ENV_FILE='${{{{ github.workspace }}}}/{STAGED_ENV_FILE}' docker compose config --quiet",
        f'export ROOT_ENV_FILE={STAGED_ENV_FILE}',
        f'ROOT_ENV_FILE="$GITHUB_WORKSPACE/{STAGED_ENV_FILE}"',
        f'DATA_DIR=/tmp/x ROOT_ENV_FILE="${{PWD}}/{STAGED_ENV_FILE}" STORE_ENV_FILE="${{PWD}}/data/store/'
        f'{STAGED_ENV_FILE}" INGEST_ENV_FILE="${{PWD}}/data/ingest/{STAGED_ENV_FILE}" docker compose config',
    ],
    ids=['dot-prefix', 'pwd-quoted', 'workspace-single-quoted', 'export', 'standalone', 'agent-stack-render'],
)
def test_an_assignment_names_the_staged_env_file_without_reading_it(line: str):
    """tj-0pobey.4 item 2: `NAME=path` hands a path on; nothing reads the file, so nothing is judged."""
    assert _staged_env_reads([line]) == []


@pytest.mark.build_infra
@pytest.mark.parametrize(
    ('line', 'accepted'),
    [
        (f'VAR="$(grep K ${{PWD}}/{STAGED_ENV_FILE} | tail -1 | cut -d= -f2-)"', True),
        (f'VAR="$(grep K "${{PWD}}/{STAGED_ENV_FILE}" | tail -n 1 | cut -d= -f2-)"', True),
        (f'VAR="$(grep K ./{STAGED_ENV_FILE} | cut -d= -f2-)"', False),
    ],
    ids=['pwd-unquoted', 'pwd-quoted-inside', 'first-match'],
)
def test_a_substitution_on_the_right_of_an_assignment_is_still_a_read(line: str, accepted: bool):
    """tj-0pobey.4 item 2: only the assigned word itself is exempt; a `$(...)` that reads is judged."""
    reads = _staged_env_reads([line])
    assert [read for read, _ in reads] == [line], f'reads found: {reads}'
    assert _takes_last_line_and_full_value(reads[0][1]) is accepted


@pytest.mark.build_infra
@pytest.mark.parametrize(
    'line',
    [
        f'printf \'KEY=%s\\n\' "${{value}}" >> "${{PWD}}/{STAGED_ENV_FILE}"',
        f'rm -f "$GITHUB_WORKSPACE/{STAGED_ENV_FILE}"',
        f"sed -i 's/^KEY=.*/KEY=/' ${{PWD}}/{STAGED_ENV_FILE}",
        f'cp .env.default "${{{{ github.workspace }}}}/{STAGED_ENV_FILE}"',
        f'grep K data/store/{STAGED_ENV_FILE} | cut -d= -f2-',
        f'grep K "${{PWD}}/data/ingest/{STAGED_ENV_FILE}" | cut -d= -f2-',
    ],
    ids=['append-quoted', 'rm-workspace', 'sed-in-place', 'cp-expression', 'store-service', 'ingest-service'],
)
def test_prefixed_writes_and_service_env_files_are_not_staged_reads(line: str):
    """The writer and redirect exclusions hold under a directory prefix; a service's own env file is not the root one."""
    assert _staged_env_reads([line]) == []


@pytest.mark.build_infra
@pytest.mark.parametrize(
    'word',
    [
        f'/opt/x/{STAGED_ENV_FILE}',
        f'$HOME/{STAGED_ENV_FILE}',
        f'"${{RUNNER_TEMP}}/{STAGED_ENV_FILE}"',
        f'artifact/{STAGED_ENV_FILE}',
        f'../{STAGED_ENV_FILE}',
    ],
    ids=['absolute', 'home', 'runner-temp', 'relative-dir', 'parent'],
)
def test_an_unrecognised_spelling_of_the_env_file_fails_loudly(word: str):
    """tj-0pobey.4 item 3: a path the finder cannot place is an error naming it, never a silent non-read.

    Silence is how the directory-prefixed read went unseen: the finder did not know the spelling,
    so it judged nothing and the check passed.
    """
    line = f'grep -E "^KEY=" {word} | cut -d= -f2'
    with pytest.raises(AssertionError, match=re.escape(word.strip('"'))):
        _staged_env_reads([line])


# A command substitution whose command is a plain word, and one whose command is a variable.
_SUBSTITUTED_CALL = re.compile(r'\$\(\s*(\w+)\b')
_DYNAMIC_SUBSTITUTED_CALL = re.compile(r'\$\(\s*"?\$\{?\w+')
# Where a command's stdout goes next: a pipe, or the end of the command.
_COMMAND_END = re.compile(r'(?<!\|)\|(?!\|)|\|\||&&|;|\)')
# Minimum count of captured compose runs the testing workflow holds: the bead's four (the alembic
# wrapper, from_client, lookup, probe). A derivation that finds fewer has gone blind, not clean.
CAPTURED_COMPOSE_RUN_FLOOR = 4


def _brace_delta(line: str) -> int:
    """How many brace groups a logical line opens, net of those it closes."""
    opens = len(re.findall(r'(?:^|(?<=\s))\{(?=\s|$)', line))
    closes = len(re.findall(r'(?:^|(?<=[\s;]))\}(?=\s|$|[;)"])', line))
    return opens - closes


def _function_spans(lines: list[str]) -> dict[str, range]:
    """Each multi-line shell function, as the indices of its lines, head and closing brace included."""
    spans = {}
    for start, line in enumerate(lines):
        if head := _FUNCTION_HEAD.match(line):
            depth, end = 1, start + 1
            while end < len(lines) and depth > 0:
                depth += _brace_delta(lines[end])
                end += 1
            spans[head.group(1)] = range(start, end)
    return spans


def _piped(after: str) -> bool:
    """Whether the command whose remaining text is `after` sends its stdout into a pipe."""
    end = _COMMAND_END.search(_unquoted(after))
    return bool(end) and end.group(0) == '|'


def _output_is_captured(name: str, span: range, lines: list[str]) -> bool:
    """Whether a function's stdout is read back: substituted, piped, or handed to a caller that substitutes it."""
    outside = [line for index, line in enumerate(lines) if index not in span]
    if any(name in _SUBSTITUTED_CALL.findall(line) for line in outside):
        return True
    called = re.compile(rf'(?:^|[;&|{{(]\s*)(?:if\s+|!\s+)?{name}\b')
    if any((match := called.search(line)) and _piped(line[match.end() :]) for line in outside):
        return True
    dynamic = any(_DYNAMIC_SUBSTITUTED_CALL.search(line) for line in lines)
    return dynamic and any(re.search(rf'\s{name}(?=\s|$)', line) for line in outside)


def _compose_runs(run: str) -> list[tuple[str, list[str], bool]]:
    """Each `docker compose run` in a script, as (its line, its words from `run`, whether its stdout is captured).

    Captured means read back by the script: the run sits inside a `$(...)`, its stdout is piped,
    or it is the body of a function whose own stdout is captured in one of those ways.
    """
    lines = _run_lines(run)
    spans = _function_spans(lines)
    runs = []
    for index, line in enumerate(lines):
        for match, (_, rest) in zip(_COMPOSE_INVOCATION.finditer(line), _compose_calls(line), strict=True):
            if rest[:1] != ['run']:
                continue
            before = re.sub(r"'[^']*'", "''", line[: match.start()])
            substituted = before.count('$(') > before.count(')')
            in_captured_function = any(
                index in span and _output_is_captured(name, span, lines) for name, span in spans.items()
            )
            runs.append((line, rest, substituted or _piped(line[match.end() :]) or in_captured_function))
    return runs


def _refuses_to_pull(rest: list[str]) -> bool:
    _, options, _ = _compose_service(rest)
    return any(
        option == '--pull=never' or (option == '--pull' and following == 'never')
        for option, following in zip(options, [*options[1:], ''], strict=True)
    )


@pytest.mark.build_infra
def test_every_captured_compose_run_refuses_to_pull():
    """tj-ijpys9.20 item 4: a compose run whose stdout the script reads back carries --pull never.

    Otherwise a missing image is pulled on the spot, and the pull's progress lines land in the
    very text the step then parses -- a revision list, a status line, a response body. With
    `--pull never` a missing image fails the run loudly instead. The set is derived from every
    step of every workflow, not listed by name: a run inside `$(...)`, piped, or inside a function
    whose own output is captured. A run whose stdout only goes to the log (upgrade head) is free.
    """
    captured, offenders = [], []
    for where, step in _every_workflow_step():
        for line, rest, is_captured in _compose_runs(step.get('run') or ''):
            if is_captured:
                captured.append(where)
                if not _refuses_to_pull(rest):
                    offenders.append(f'{where}: {line}')
    assert len(captured) >= CAPTURED_COMPOSE_RUN_FLOOR, (
        f'found {len(captured)} captured compose runs ({captured}), fewer than the {CAPTURED_COMPOSE_RUN_FLOOR} '
        f'tj-ijpys9.20 hardened, so the derivation has lost some'
    )
    assert not offenders, f'these compose runs are captured but may pull, mixing progress into the capture: {offenders}'


_COMPOSE_RUN_CAPTURE_CASES = {
    'substituted': ('x="$(docker compose -f a.yaml run --rm svc echo hi)"', [True]),
    'piped': ('docker compose -f a.yaml run --rm svc cat | grep -c x', [True]),
    'or-list, pipe only inside quotes': ('docker compose run --rm svc true || echo "a | b"', [False]),
    'bare, output to the log': ('docker compose -f a.yaml run --rm svc alembic upgrade head', [False]),
    'function substituted': ('f() {\ndocker compose run --rm svc cat\n}\nx="$(f arg)"', [True]),
    'function piped': ('f() {\ndocker compose run --rm svc cat\n}\nf arg | tail -n 1', [True]),
    'function handed to a substituting caller': (
        'f() {\ndocker compose run --rm svc cat\n}\ng() {\nout="$("$1")"\n}\ng f',
        [True],
    ),
    'function called bare': ('f() {\ndocker compose run --rm svc true\n}\nf', [False]),
    'exec is not run': ('x="$(docker compose exec -T svc cat)"', []),
    'continued lines': ('x="$(docker compose -f a.yaml \\\n  run --rm svc cat)"', [True]),
}


@pytest.mark.build_infra
@pytest.mark.parametrize(
    ('run', 'expected'), _COMPOSE_RUN_CAPTURE_CASES.values(), ids=_COMPOSE_RUN_CAPTURE_CASES.keys()
)
def test_the_capture_derivation_tells_captured_runs_from_logged_ones(run: str, expected: list[bool]):
    """The derivation above, on synthetic scripts, so it cannot pass the workflow by seeing nothing."""
    assert [captured for _, _, captured in _compose_runs(run)] == expected


@pytest.mark.build_infra
@pytest.mark.parametrize(
    ('options', 'refuses'),
    [
        (['--rm', '--pull', 'never'], True),
        (['--rm', '--pull=never'], True),
        (['--rm', '--pull', 'missing'], False),
        (['--rm', '-T'], False),
    ],
    ids=['spaced', 'equals', 'missing-policy', 'absent'],
)
def test_the_pull_rule_reads_the_run_options(options: list[str], refuses: bool):
    """`--pull never` in either spelling is a refusal to pull; any other policy, or none, is not."""
    assert _refuses_to_pull(['run', *options, 'svc', 'cmd']) is refuses


def _import_time_prints(source: str) -> list[int]:
    """The line of every print() call that runs when a module is imported.

    That is everything outside a function or lambda body: module statements, the bodies of
    module-level if/try/with/for, class bodies, decorators and default values. A block under
    `if __name__ == '__main__':` does not run at import and is left out.
    """
    found: list[int] = []

    class Visitor(ast.NodeVisitor):
        def _visit_defaults(self, arguments: ast.arguments) -> None:
            for child in [*arguments.defaults, *arguments.kw_defaults]:
                if child is not None:
                    self.visit(child)

        def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            for decorator in node.decorator_list:
                self.visit(decorator)
            self._visit_defaults(node.args)

        visit_FunctionDef = visit_AsyncFunctionDef = _visit_function

        def visit_Lambda(self, node: ast.Lambda) -> None:
            self._visit_defaults(node.args)

        def visit_If(self, node: ast.If) -> None:
            test = node.test
            is_main_guard = (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id == '__name__'
                and len(test.comparators) == 1
                and isinstance(test.comparators[0], ast.Constant)
                and test.comparators[0].value == '__main__'
            )
            for child in node.orelse if is_main_guard else [node.test, *node.body, *node.orelse]:
                self.visit(child)

        def visit_Call(self, node: ast.Call) -> None:
            if isinstance(node.func, ast.Name) and node.func.id == 'print':
                found.append(node.lineno)
            self.generic_visit(node)

    Visitor().visit(ast.parse(source))
    return sorted(found)


COMMON_PACKAGE = 'common'


@pytest.mark.common
def test_no_common_module_prints_at_import():
    """tj-ijpys9.20 item 2: importing anything in common/ writes nothing to stdout.

    SCOPE: every git-tracked production module under common/ (tests excluded), not only the
    database modules. common/ is imported by both services, by alembic's env.py through the
    store's models, and by the client's suite; which modules any one import chain reaches is
    not something to pin by name, and the two prints tj-ijpys9.20 removed sat in
    common/database/postgres_tools.py only by accident of history. Output inside a function
    runs only when called and is the caller's business; this pins import time, where a stray
    line lands in `alembic heads` or any other captured run. data/, schemas/ and routers/ are
    outside the bead and not judged here.
    """
    modules = [path for path in _run('git', 'ls-files', '--', f'{COMMON_PACKAGE}/*.py') if not _is_test_path(path)]
    assert modules, f'git tracks no production module under {COMMON_PACKAGE}/, so this check judged nothing'
    offenders = [
        f'{path}:{line}'
        for path in modules
        for line in _import_time_prints((REPO_ROOT / path).read_text(encoding='utf-8'))
    ]
    assert not offenders, f'these print() at import, into any captured stdout that imports them: {offenders}'


_IMPORT_TIME_PRINT_CASES = {
    'module level': ("print('x')\n", [1]),
    'inside a module-level if': ("import os\nif os.environ.get('X'):\n    print('x')\n", [3]),
    'inside a module-level try': ("try:\n    print('x')\nexcept Exception:\n    pass\n", [2]),
    'a class body': ("class C:\n    print('x')\n", [2]),
    'a decorator argument': ("def d(x):\n    return x\n@d(print('x'))\ndef f():\n    pass\n", [3]),
    'a default value': ("def f(x=print('x')):\n    pass\n", [1]),
    'a function body': ("def f():\n    print('x')\n", []),
    'a method body': ("class C:\n    def m(self):\n        print('x')\n", []),
    'a lambda': ("f = lambda: print('x')\n", []),
    'the main guard': ("if __name__ == '__main__':\n    print('x')\n", []),
    'the else of the main guard': ("if __name__ == '__main__':\n    pass\nelse:\n    print('x')\n", [4]),
    'a logger call': ("import logging\nlogging.getLogger().info('x')\n", []),
}


@pytest.mark.common
@pytest.mark.parametrize(('source', 'lines'), _IMPORT_TIME_PRINT_CASES.values(), ids=_IMPORT_TIME_PRINT_CASES.keys())
def test_the_import_time_print_finder_follows_what_runs_at_import(source: str, lines: list[int]):
    """The finder above, on synthetic modules, so it cannot pass common/ by looking nowhere."""
    assert _import_time_prints(source) == lines


# ---------------------------------------------------------------------------------------
# THE gRPC TOOLCHAIN (ADR tj-8konfu D1 and D3, re-homed by addendum A1; tj-3mk3u5.23)
#
# Committing protoc's output is safe only with three controls around it. Each one fails silently
# when it goes, because nothing reports a check that no longer runs.
#
# * THE STALENESS STEP. CI's unit job regenerates common/rpc/generated/ through `make proto` and
#   fails on ANY difference from the commit: a changed, deleted or untracked file. D3 says that
#   without it committing is strictly worse than generating at build time. It has to run before
#   the linters and the suite. Its script is pinned by RUNNING it against a scratch repository
#   with a stand-in `make`, because the property is what it detects. A `git diff` in place of the
#   `git status` reads the same at a glance and misses every untracked file.
# * THE SEAM. Nothing outside common/rpc/ imports common.rpc.generated (ruff TID251). Pinned by
#   asking ruff itself, over stdin, with the project's own configuration. That exercises the
#   select entry, the banned-api table and the per-file-ignore together: remove any one of them,
#   or widen the ignore, and a case here changes.
# * THE PINS. The generator writes its version into every file it emits, so an unpinned
#   grpcio-tools would fail the staleness step on a new release rather than on a contract change.
#
# Deliberately NOT pinned here: that `make proto` reproduces the committed tree. That IS the
# staleness step, which D3 makes the control. A copy in the suite would be a second definition of
# the protoc invocation to keep in step with the Makefile, which is what the step avoids.
GENERATED_GRPC_TREE = PurePosixPath('common/rpc/generated')
GENERATED_GRPC_MODULE = GENERATED_GRPC_TREE / 'trader_joe' / 'ping' / 'v1' / 'ping_pb2.py'


def _runs_make_target(command: list[str], target: str) -> bool:
    return bool(command) and PurePosixPath(command[0]).name == 'make' and target in command[1:]


def _runs_ruff(step: dict) -> bool:
    return any(PurePosixPath(word).name == 'ruff' for command in _step_commands(step) for word in command)


def _proto_regeneration_steps() -> list[tuple[str, int, dict]]:
    """(job id, step index, step) for every step of the testing workflow that runs `make proto`."""
    found = []
    for job_id, job in ((_load_yaml(TESTING_WORKFLOW) or {}).get('jobs') or {}).items():
        for index, step in enumerate((job or {}).get('steps') or []):
            if any(_runs_make_target(command, 'proto') for command in _step_commands(step)):
                found.append((job_id, index, step))
    return found


def _staleness_step() -> dict:
    steps = _proto_regeneration_steps()
    assert len(steps) == 1, (
        f'expected exactly one step in {TESTING_WORKFLOW.name} that runs `make proto`, found '
        f'{[(job_id, step.get("name")) for job_id, _, step in steps]}'
    )
    return steps[0][2]


@pytest.mark.build_infra
def test_ci_checks_the_generated_grpc_tree_before_it_lints_or_tests():
    """D3: the staleness step exists, in the job that runs the suite, ahead of ruff and pytest.

    Ahead, so a stale tree is reported as stale rather than as whatever lint or import error it
    happens to cause first.
    """
    _staleness_step()
    ((job_id, index, _),) = _proto_regeneration_steps()
    steps = _load_yaml(TESTING_WORKFLOW)['jobs'][job_id]['steps']
    gated = [later for later, step in enumerate(steps) if _runs_the_suite(step) or _runs_ruff(step)]
    assert any(_runs_the_suite(step) for step in steps), (
        f'the `make proto` step is in {job_id}, which does not run the unit suite'
    )
    assert gated and min(gated) > index, (
        f'in {job_id}, the `make proto` step is step {index}, but ruff or pytest runs at steps {gated}; '
        f'it has to come first'
    )


_STALENESS_CASES = {
    'regeneration is a no-op': ('', 0),
    'a generated module changed': (f"printf '# drift\\n' >> {GENERATED_GRPC_MODULE}", 1),
    'a generated module deleted': (f'rm {GENERATED_GRPC_MODULE}', 1),
    'a generated module untracked': (
        f'mkdir -p {GENERATED_GRPC_TREE}/trader_joe/probe/v1 && '
        f"printf 'x = 1\\n' > {GENERATED_GRPC_TREE}/trader_joe/probe/v1/probe_pb2.py",
        1,
    ),
    'a change outside the generated tree': ("printf 'drift\\n' >> README.md", 0),
}


@pytest.mark.build_infra
@pytest.mark.parametrize(('regeneration', 'status'), _STALENESS_CASES.values(), ids=_STALENESS_CASES.keys())
def test_the_staleness_step_fails_on_any_drift_in_the_generated_tree(tmp_path: Path, regeneration: str, status: int):
    """D3: the step's own script, run where `make proto` leaves the generated tree in each possible state.

    The stand-in `make` records its arguments and applies the case's change, so the step is shown to
    regenerate through `make proto` and to judge what that leaves. Run under the shell GitHub uses for
    a `run:` with no `shell:`. git is configured from nothing, so the runner's config cannot leak in.
    """
    script = _staleness_step().get('run') or ''
    assert '${{' not in script, 'the step now uses a workflow expression, which this test cannot evaluate'

    repo = tmp_path / 'repo'
    (repo / GENERATED_GRPC_MODULE).parent.mkdir(parents=True)
    (repo / GENERATED_GRPC_TREE / '__init__.py').write_text('', encoding='utf-8')
    (repo / GENERATED_GRPC_MODULE).write_text('DESCRIPTOR = None\n', encoding='utf-8')
    (repo / 'README.md').write_text('readme\n', encoding='utf-8')
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env |= {
        'GIT_CONFIG_NOSYSTEM': '1',
        'GIT_CONFIG_GLOBAL': os.devnull,
        'GIT_AUTHOR_NAME': 'validator',
        'GIT_AUTHOR_EMAIL': 'validator@example.invalid',
        'GIT_COMMITTER_NAME': 'validator',
        'GIT_COMMITTER_EMAIL': 'validator@example.invalid',
    }
    for command in (['git', 'init', '-q'], ['git', 'add', '-A'], ['git', 'commit', '-q', '-m', 'generated tree']):
        subprocess.run(command, cwd=repo, env=env, check=True, capture_output=True)

    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    make_log = tmp_path / 'make.args'
    fake_make = bin_dir / 'make'
    fake_make.write_text(
        f'#!/usr/bin/env bash\nset -eu\nprintf "%s\\n" "$*" >> {shlex.quote(str(make_log))}\n{regeneration}\n',
        encoding='utf-8',
    )
    fake_make.chmod(0o755)
    step_script = tmp_path / 'step.sh'
    step_script.write_text(script, encoding='utf-8')
    env['PATH'] = f'{bin_dir}{os.pathsep}{env.get("PATH", "")}'

    result = subprocess.run(
        ['bash', '--noprofile', '--norc', '-e', str(step_script)], cwd=repo, env=env, capture_output=True, text=True
    )

    assert make_log.read_text(encoding='utf-8').splitlines() == ['proto'], 'the step must regenerate via `make proto`'
    assert result.returncode == status, (
        f'the staleness step exited {result.returncode}, expected {status}.\n'
        f'stdout:\n{result.stdout}\nstderr:\n{result.stderr}'
    )


SEAM_VIOLATIONS = (
    'import common.rpc.generated.trader_joe.ping.v1.ping_pb2_grpc',
    'from common.rpc.generated.trader_joe.ping.v1 import ping_pb2',
    'from common.rpc.generated.trader_joe.ping.v1.ping_pb2 import PingRequest',
    'from common.rpc import generated',
)
OUTSIDE_THE_SEAM = (
    'common/probe.py',
    'common/tests/test_probe.py',
    'routers/common/probe.py',
    'schemas/common/probe.py',
    'data/store/app/probe.py',
    'data/ingest/app/probe.py',
)
INSIDE_THE_SEAM = ('common/rpc/probe.py', 'common/rpc/nested/probe.py')


def _ruff_codes_by_line(filename: str, source: str) -> dict[int, set[str]]:
    """Lint `source` as though it lived at `filename`, under the project's ruff configuration."""
    result = subprocess.run(
        [
            sys.executable,
            '-m',
            'ruff',
            'check',
            '--no-cache',
            '--output-format',
            'json',
            '--stdin-filename',
            filename,
            '-',
        ],
        cwd=REPO_ROOT,
        input=source,
        capture_output=True,
        text=True,
    )
    assert result.returncode in (0, 1), f'ruff did not run: exit {result.returncode}\n{result.stderr}'
    found: dict[int, set[str]] = {}
    for diagnostic in json.loads(result.stdout or '[]'):
        found.setdefault(diagnostic['location']['row'], set()).add(diagnostic['code'])
    return found


@pytest.mark.build_infra
@pytest.mark.parametrize('filename', OUTSIDE_THE_SEAM)
def test_generated_grpc_code_cannot_be_imported_outside_common_rpc(filename: str):
    """D3's seam: every form of importing the generated package is TID251 outside common/rpc, tests included."""
    flagged = _ruff_codes_by_line(filename, '\n'.join(SEAM_VIOLATIONS) + '\n')
    missed = [line for row, line in enumerate(SEAM_VIOLATIONS, start=1) if 'TID251' not in flagged.get(row, set())]
    assert not missed, f'in {filename}, ruff lets these through: {missed}. Reach generated code through common/rpc.'


@pytest.mark.build_infra
def test_a_relative_import_of_generated_grpc_code_is_caught_too():
    flagged = _ruff_codes_by_line('common/probe.py', 'from .rpc.generated.trader_joe.ping.v1 import ping_pb2\n')
    assert 'TID251' in flagged.get(1, set()), f'a relative import of the generated package is not TID251: {flagged}'


@pytest.mark.build_infra
@pytest.mark.parametrize('filename', INSIDE_THE_SEAM)
def test_common_rpc_may_import_its_generated_code(filename: str):
    """Guard the guard: TID251 above comes from the ban, not from a rule that flags every import."""
    flagged = _ruff_codes_by_line(filename, '\n'.join(SEAM_VIOLATIONS) + '\n')
    assert not any('TID251' in codes for codes in flagged.values()), f'{filename}: {flagged}'


# Where each half of the toolchain belongs: the runtime in base, which both services install, and
# the generator in dev only, so the prod images never carry it.
GRPC_PIN_GROUPS = {'grpcio': 'base', 'grpcio-health-checking': 'base', 'grpcio-tools': 'dev'}


def _dependency_group_requirements() -> dict[str, dict[str, Requirement]]:
    with PYPROJECT.open('rb') as handle:
        groups = tomllib.load(handle).get('dependency-groups', {})
    parsed: dict[str, dict[str, Requirement]] = {}
    for group, entries in groups.items():
        requirements = [Requirement(entry) for entry in entries if isinstance(entry, str)]
        parsed[group] = {canonicalize_name(requirement.name): requirement for requirement in requirements}
    return parsed


def _exact_version(requirement: Requirement) -> str | None:
    specifiers = list(requirement.specifier)
    if len(specifiers) == 1 and specifiers[0].operator == '==' and '*' not in specifiers[0].version:
        return specifiers[0].version
    return None


@pytest.mark.build_infra
def test_the_grpc_toolchain_is_pinned_exactly_and_moves_together():
    """D1/D3: the grpc toolchain is pinned with ==, at one version, each package in its own group.

    grpcio-tools writes its version into every file it generates, and grpcio and
    grpcio-health-checking move with it.
    """
    groups = _dependency_group_requirements()
    versions: dict[str, str] = {}
    for name, group in GRPC_PIN_GROUPS.items():
        requirement = groups.get(group, {}).get(name)
        assert requirement is not None, f'{name} is not declared in the {group!r} dependency group'
        version = _exact_version(requirement)
        assert version is not None, f'{name} is declared as {str(requirement)!r}; it must be pinned with =='
        versions[name] = version
    assert len(set(versions.values())) == 1, f'the grpc pins have to move together, but read {versions}'
    elsewhere = sorted(
        group for group, requirements in groups.items() if group != 'dev' and 'grpcio-tools' in requirements
    )
    assert not elsewhere, f'grpcio-tools is also declared in {elsewhere}; the generator belongs in dev only'
