"""Every docker command the verbs run: the fixed prefixes, the snapshot, the trusted files -- and nothing else.

Re-pinned for the one project when the agent stack was retired (tj-iee6sn.21; ADR tj-4rr0la addendum 26):
no argv carries the retired project, overlay or generated env file, and the image no longer bakes it.

tj-c4mosr.5 body bullets 1 and 7, items D1 (01:40 (1)), S1, G1, (3), S2, O3, D3 (6), (13), and the
reviewed-suppression pin (02:32). Design: ADR tj-4rr0la section 3 and 5(a), addenda 1-6.

Every verb is RUN here, through AgentStack with a fake docker runner -- so what is pinned is the argv
the server really hands the daemon, not what a builder function returns when called in isolation.
"""

import ast
import os
import posixpath
import re
import shlex
from itertools import pairwise
from pathlib import Path

import pytest
import yaml

from common.tests.test_ci_invariants import (
    _dockerfile_copy_sources,
    _dockerignore_pattern_regex,
    _dockerignore_rules,
    _is_excluded_from_context,
    _short_volume_source,
)
from tools.agent_mcp import runner, stack
from tools.agent_mcp.tests.harness import (
    CLIENT_FILES,
    DEV_CONTAINER_ID,
    DEV_EXEC_VERBS,
    DEV_FILE_LIFECYCLE_VERBS,
    DEV_LABEL_LIFECYCLE_VERBS,
    DEV_MIGRATE_VERBS,
    DEV_PROJECT_VERBS,
    DEV_TOOLS_VERBS,
    DEV_UP_VERBS,
    DOCKERLESS_VERBS,
    REPO_ROOT,
    SYSTEM_SUITE_VERBS,
    TEST_MODE_VERBS,
    VERB_SAMPLES,
    WORKTREE_NAME,
    client_prefix_words,
    default_response,
    dev_up_compose_argv,
    is_base_step,
    is_client_step,
    is_dev_digest,
    is_dev_up_wrapper,
    make_rig,
    response_for,
)


pytestmark = pytest.mark.build_infra

MCP_DIR = REPO_ROOT / 'tools' / 'agent_mcp'
MCP_DOCKERFILE = MCP_DIR / 'Dockerfile'
MCP_DOCKERIGNORE = MCP_DIR / 'Dockerfile.dockerignore'
FAKE_OVERLAY = REPO_ROOT / 'docker-compose.fake.yaml'
# The RETIRED agent stack (ADR tj-4rr0la addendum 26; tj-iee6sn.21): its project, its overlay and its
# generated root env file. No argv any verb runs may carry one of them.
RETIRED_PROJECT = 'trader_joe_agent_stack'
RETIRED_OVERLAY = 'docker-compose.agent-stack.yaml'
RETIRED_ENV_FILE = 'agent_stack.env'
# Spelled out, not read from stack.py: a test that took the expected prefix from the module under
# test would agree with any change to it.
EXPECTED_TRUSTED_DIR = '/opt/agent_mcp/compose'
EXPECTED_DOCKER = '/usr/local/bin/docker'
# The compose files the MCP image bakes besides EXPECTED_DEV_BAKES (below): the base file, the system-test
# client's file and the test-mode overlay. The agent-stack overlay is no longer one (tj-iee6sn.21).
EXPECTED_BASE_BAKES = ('docker-compose.yaml', 'docker-compose.test-client.yaml', 'docker-compose.fake.yaml')


def _base_inspect_argv(ref: str) -> list[str]:
    """The one plain-docker inspect argv the ensure-bases step may run (ADR tj-4rr0la addendum 14 (3))."""
    return [EXPECTED_DOCKER, 'image', 'inspect', '--format', '{{.Id}}', ref]


def _bases_absent(step: stack.Step) -> runner.ProcessResult:
    """Every base inspect fails (the daemon lacks it), so ensure-bases pulls each ref; the rest as default.

    Only the BASE_IMAGES refs: up (tj-iee6sn.9) also inspects its three :dev images, which are there.
    """
    if list(step.argv[1:3]) == ['image', 'inspect'] and step.argv[-1] in stack.BASE_IMAGES:
        return runner.ProcessResult(1, b'', b'Error: No such image')
    return default_response(step)


def _run_verb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, respond=default_response):
    rig = make_rig(tmp_path, monkeypatch)
    # tj-iee6sn.20: the system-suite pair runs only in TEST mode on the test postgres mount.
    rig.docker.respond = response_for(verb, rig.layout, respond)
    result = rig.call(verb, dict(VERB_SAMPLES[verb]))
    return rig, result


def test_every_verb_has_a_sample():
    """The parameterised tests below run every verb: a new verb without a sample goes red here."""
    agent = runner.AgentStack(settings=None)  # type: ignore[arg-type] -- only the verb table is read
    assert set(agent.verbs) == set(VERB_SAMPLES), f'verbs {agent.verbs} vs samples {sorted(VERB_SAMPLES)}'
    assert set(runner.VERB_SCHEMAS) == set(VERB_SAMPLES) == set(runner.VERB_TIMEOUT_SECONDS)


@pytest.mark.parametrize('verb', sorted(VERB_SAMPLES))
def test_every_verb_runs_only_the_argv_heads_of_its_class(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str):
    """Body bullet 1, S1 and G1, re-pinned for the one project (ADR tj-4rr0la addendum 26; tj-iee6sn.21).

    Every verb is either a system-suite verb (the client prefix over the snapshot) or one of the one
    project's classes, each with its fixed argv heads; there is no third, agent-stack, prefix any more.
    Run with every base absent, so the pull form of the ensure-bases pair appears too. No argv word of
    any verb names the retired agent stack's project, overlay or generated env file.
    """
    rig, result = _run_verb(tmp_path, monkeypatch, verb, _bases_absent)
    layout = rig.layout
    assert result['status'] == 'ok', result
    words = {word for step in rig.docker.steps for word in step.argv}
    retired = [word for word in words if RETIRED_PROJECT in word or RETIRED_OVERLAY in word or RETIRED_ENV_FILE in word]
    assert not retired, f'{verb} hands the daemon a word of the retired agent stack: {retired}'
    if verb in SYSTEM_SUITE_VERBS:
        # tj-iee6sn.20 (ADR tj-4rr0la addenda 26-27): no agent-stack word at all. The rails' plain reads
        # (`ps`, `inspect`), the trusted digests, the bases, then client steps under the ONE client prefix
        # (whole argv pinned in _EXPECTED_SYSTEM_SUITE and test_system_suite.py). Every step runs from the
        # stack directory, and no word names the root checkout or the worktree: the snapshot stands in.
        clients = [step for step in rig.docker.steps if is_client_step(step)]
        assert clients, f'{verb} ran no client step'
        prefix = client_prefix_words(layout)
        for step in rig.docker.steps:
            argv = list(step.argv)
            assert step.cwd == layout.stack_dir, f'{verb} runs a step with cwd {step.cwd}'
            repository_paths = [word for word in argv if str(layout.repo) in word or str(layout.worktree) in word]
            assert not repository_paths, f'{verb} hands the daemon a repository or worktree path: {repository_paths}'
            if is_client_step(step):
                assert argv[: len(prefix)] == prefix, shlex.join(argv)
                assert argv.count('--env-file') == 1 and argv.count('-p') == 1, argv
            elif is_base_step(step) or is_dev_digest(step):
                continue
            else:
                assert argv[0] == EXPECTED_DOCKER and argv[1] in ('ps', 'inspect'), argv
        return
    if verb in DEV_PROJECT_VERBS:
        # Not this test's subject: the dev verbs reach the user's own project with a four-word
        # prefix that spells no file, no project directory and no env file. The pin that matters
        # for them -- that no other project and no mutating command is reachable -- is tj-tq2hn6's.
        assert rig.docker.steps, f'{verb} ran no docker step, so there is nothing to pin'
        # The exec verbs run plain docker: a label lookup (`ps`), then `exec` (ADR tj-4rr0la
        # addendum 22). Their whole argv is pinned in _EXPECTED_TAILS_DEV and test_dev_exec.py.
        # The label-only lifecycle pair (tj-iee6sn.7) runs the D7 guard's plain `ps`, then compose.
        heads = [['compose']]
        steps = rig.docker.steps
        if verb in DEV_EXEC_VERBS:
            heads = [['ps'], ['exec']]
        elif verb in DEV_LABEL_LIFECYCLE_VERBS:
            heads = [['ps'], ['compose']]
        elif verb in DEV_FILE_LIFECYCLE_VERBS:
            # tj-iee6sn.8: the trusted digest script under bash (its whole argv is pinned in
            # _EXPECTED_TAILS_DEV), then the ensure-bases pair, then compose.
            heads = [['image'], ['pull'], ['compose']]
            steps = [step for step in steps if not is_dev_digest(step)]
            assert len(steps) < len(rig.docker.steps), f'{verb} ran no digest step'
        elif verb in DEV_UP_VERBS:
            # tj-iee6sn.9: the guard's `ps`, the :dev image inspects, the digests (bash), the label-only
            # down, the devnet `network` pair, the bases, then the wrapped up, whose compose argv is
            # every word after the wrapper's six (its whole argv is pinned in _EXPECTED_TAILS_DEV).
            heads = [['ps'], ['image'], ['pull'], ['network'], ['compose']]
            wrapped = [step for step in steps if is_dev_up_wrapper(step)]
            assert len(wrapped) == 1, f'{verb} ran {len(wrapped)} wrapped steps'
            steps = [
                stack.Step(dev_up_compose_argv(step), step.cwd) if is_dev_up_wrapper(step) else step
                for step in steps
                if not is_dev_digest(step)
            ]
        elif verb in DEV_MIGRATE_VERBS:
            # tj-iee6sn.10: migrate in DEV mode (the rig's default): the guard's and the two lookups' `ps`,
            # the :dev image inspect, the bases, then the wrapped one-off alembic run, whose compose argv is
            # every word after the wrapper's six.
            heads = [['ps'], ['image'], ['pull'], ['compose']]
            wrapped = [step for step in steps if is_dev_up_wrapper(step)]
            assert len(wrapped) == 1, f'{verb} ran {len(wrapped)} wrapped steps'
            steps = [
                stack.Step(dev_up_compose_argv(step), step.cwd) if is_dev_up_wrapper(step) else step
                for step in steps
                if not is_dev_digest(step)
            ]
        elif verb in DEV_TOOLS_VERBS:
            # tj-iee6sn.14: the guard's `ps`, the devnet `network` pair, then the wrapped up of pgAdmin,
            # whose compose argv is every word after the wrapper's six. No image, pull or digest step.
            heads = [['ps'], ['network'], ['compose']]
            wrapped = [step for step in steps if is_dev_up_wrapper(step)]
            assert len(wrapped) == 1, f'{verb} ran {len(wrapped)} wrapped steps'
            steps = [
                stack.Step(dev_up_compose_argv(step), step.cwd) if is_dev_up_wrapper(step) else step for step in steps
            ]
        elif verb in TEST_MODE_VERBS:
            # tj-iee6sn.24: the digests (bash), the bases' `image`/`pull`, test_up's guard `ps` and its
            # :test inspects and stamp reads (`image`), then compose, UNWRAPPED (no secret wrapper, no
            # devnet). Their whole argv is pinned in _EXPECTED_TAILS_DEV.
            heads = [['ps'], ['image'], ['pull'], ['compose']]
            assert not [step for step in steps if is_dev_up_wrapper(step)], f'{verb} ran the secret wrapper'
            steps = [step for step in steps if not is_dev_digest(step)]
            assert len(steps) < len(rig.docker.steps), f'{verb} ran no digest step'
        assert all(list(step.argv[1:2]) in heads and step.argv[0] == EXPECTED_DOCKER for step in steps)
        return
    if verb in DOCKERLESS_VERBS:
        assert rig.docker.steps == [], f'{verb} ran docker: {rig.docker.steps}'
        return
    pytest.fail(f'{verb} is in no verb class: classify it in harness.py (there is no agent-stack prefix any more)')


def test_the_verb_classes_partition_the_verbs():
    """With the agent stack gone, every verb is a system-suite verb or a one-project verb, never both."""
    assert set(VERB_SAMPLES) == SYSTEM_SUITE_VERBS | DEV_PROJECT_VERBS | DOCKERLESS_VERBS
    assert not SYSTEM_SUITE_VERBS & DEV_PROJECT_VERBS


# The ensure-bases inspects, once per verb before its first step that can build (ADR tj-4rr0la
# addenda 14 (3) and 15). Bases present here (FakeDocker's default exit 0), so nothing is pulled.
_INSPECTS = [_base_inspect_argv(ref) for ref in stack.BASE_IMAGES]

# THE DEV PAIR, whole argv rather than a tail (tj-kzy7w2; ADR tj-4rr0la addendum 18). Spelled from
# literals, including the project name: a test that read stack.DEV_PROJECT would agree with any
# change to it, and the project name is the whole safety case here.
EXPECTED_DEV_PROJECT = 'trader_joe_dev'
_EXPECTED_DEV_GUARD = [
    EXPECTED_DOCKER,
    'ps',
    '-a',
    '--filter',
    f'label=com.docker.compose.project={EXPECTED_DEV_PROJECT}',
    # tj-iee6sn.24 (addendum 26 (2)): the mode guard classifies NON-one-off containers only.
    '--filter',
    'label=com.docker.compose.oneoff=False',
    '--format',
    '{{.Label "com.docker.compose.project.config_files"}}',
]
_EXPECTED_DEV_FILES = (
    'docker-compose.yaml',
    'docker-compose.override.yaml',
    'docker-compose.web.yaml',
    'docker-compose.web.dev.yaml',
    'docker-compose.agent-dev.yaml',
)


def _expected_dev_lookup(service: str) -> list[str]:
    """The label lookup of SERVICE's running, non-one-off containers in the dev project, ids only."""
    return [
        EXPECTED_DOCKER,
        'ps',
        '--filter',
        'status=running',
        '--filter',
        f'label=com.docker.compose.project={EXPECTED_DEV_PROJECT}',
        '--filter',
        f'label=com.docker.compose.service={service}',
        '--filter',
        'label=com.docker.compose.oneoff=False',
        '--format',
        '{{.ID}}',
    ]


def _expected_dev_wrapped(tail: list[str]) -> list[str]:
    """The up verb's six wrapper words, then the file-based dev compose prefix, then TAIL."""
    return [
        'sh',
        '-c',
        stack.DEV_UP_WRAPPER_SCRIPT,
        'sh',
        f'{EXPECTED_TRUSTED_DIR}/tools/dev_instance_secret.sh',
        '<HOST>/.env',
        EXPECTED_DOCKER,
        'compose',
        '-p',
        EXPECTED_DEV_PROJECT,
        '--project-directory',
        '<HOST>',
        *(word for name in _EXPECTED_DEV_FILES for word in ('-f', f'{EXPECTED_TRUSTED_DIR}/{name}')),
        *tail,
    ]


_EXPECTED_TAILS_DEV = {
    'ps': [[EXPECTED_DOCKER, 'compose', '-p', EXPECTED_DEV_PROJECT, 'ps', '--all']],
    'logs': [
        [EXPECTED_DOCKER, 'compose', '-p', EXPECTED_DEV_PROJECT, 'logs', '--no-color', '--tail', '50', 'postgres']
    ],
    # ADR tj-4rr0la addendum 22 (4): the label lookup, then ONE exec into the id it found. The
    # script word is the module constant: its content is pinned word by word in test_dev_exec.py.
    'db_tables': [
        [
            EXPECTED_DOCKER,
            'ps',
            '--filter',
            'status=running',
            '--filter',
            f'label=com.docker.compose.project={EXPECTED_DEV_PROJECT}',
            '--filter',
            'label=com.docker.compose.service=postgres',
            '--filter',
            'label=com.docker.compose.oneoff=False',
            '--format',
            '{{.ID}}',
        ],
        [
            EXPECTED_DOCKER,
            'exec',
            '-u',
            'postgres',
            '-e',
            'PGOPTIONS=-c default_transaction_read_only=on',
            DEV_CONTAINER_ID,
            'sh',
            '-c',
            stack.DEV_DB_TABLES_SCRIPT,
        ],
    ],
    # tj-iee6sn.13.5, folded into migrate_status by tj-iee6sn.10: the mode guard (DEV or TEST, the rig
    # answers DEV), the lookup of data_store, then ONE exec in /code and nothing else -- no user, no
    # environment variable. The script word is pinned by its parts and by running it in test_dev_exec.py.
    'migrate_status': [
        _EXPECTED_DEV_GUARD,
        [
            EXPECTED_DOCKER,
            'ps',
            '--filter',
            'status=running',
            '--filter',
            f'label=com.docker.compose.project={EXPECTED_DEV_PROJECT}',
            '--filter',
            'label=com.docker.compose.service=data_store',
            '--filter',
            'label=com.docker.compose.oneoff=False',
            '--format',
            '{{.ID}}',
        ],
        [EXPECTED_DOCKER, 'exec', '-w', '/code', DEV_CONTAINER_ID, 'sh', '-c', stack.DEV_MIGRATE_STATUS_SCRIPT],
    ],
    # tj-iee6sn.13.6: the lookup of data_store, then ONE exec with NO option at all -- no user, no
    # workdir, no variable -- running the fixed GET script of the sampled name (ping). The script word
    # is pinned by its parts and by running it against a stub server in test_dev_exec.py.
    'http_get': [
        [
            EXPECTED_DOCKER,
            'ps',
            '--filter',
            'status=running',
            '--filter',
            f'label=com.docker.compose.project={EXPECTED_DEV_PROJECT}',
            '--filter',
            'label=com.docker.compose.service=data_store',
            '--filter',
            'label=com.docker.compose.oneoff=False',
            '--format',
            '{{.ID}}',
        ],
        [EXPECTED_DOCKER, 'exec', DEV_CONTAINER_ID, 'sh', '-c', stack.DEV_HTTP_GET_TAILS['ping'][2]],
    ],
    # tj-iee6sn.7: the D7 guard's ONE `docker ps` over the config_files label (ruling D7, word for
    # word), then ONE label-only compose step: down with no -v, --rmi or --remove-orphans (D2, the
    # 04:31 amendment), or restart of the sampled service (data_store).
    'down': [_EXPECTED_DEV_GUARD, [EXPECTED_DOCKER, 'compose', '-p', EXPECTED_DEV_PROJECT, 'down']],
    'restart': [_EXPECTED_DEV_GUARD, [EXPECTED_DOCKER, 'compose', '-p', EXPECTED_DEV_PROJECT, 'restart', 'data_store']],
    # tj-iee6sn.8: the TRUSTED source_digest.sh over the repository's host path, once per stamped
    # image; the bases; then ONE file-based build of the three :dev images. HOST_MARK is the rig's
    # host path, filled in by the test.
    'build': [
        ['/usr/bin/bash', f'{EXPECTED_TRUSTED_DIR}/tools/source_digest.sh', '--context', '<HOST>', 'data', 'store'],
        ['/usr/bin/bash', f'{EXPECTED_TRUSTED_DIR}/tools/source_digest.sh', '--context', '<HOST>', 'data', 'ingest'],
        *_INSPECTS,
        [
            EXPECTED_DOCKER,
            'compose',
            '-p',
            EXPECTED_DEV_PROJECT,
            '--project-directory',
            '<HOST>',
            *(
                word
                for name in (
                    'docker-compose.yaml',
                    'docker-compose.override.yaml',
                    'docker-compose.web.yaml',
                    'docker-compose.web.dev.yaml',
                    'docker-compose.agent-dev.yaml',
                )
                for word in ('-f', f'{EXPECTED_TRUSTED_DIR}/{name}')
            ),
            'build',
            'data_store',
            'data_ingest',
            'web',
        ],
    ],
    # tj-iee6sn.9 (17:02 order, 17:33 gate ruling F-A): the D7 guard; one inspect per :dev image; the two
    # trusted digests; the bases (the up can build; ensured BEFORE anything is stopped, so a failed pull
    # leaves the project untouched); the label-only down; the devnet inspect (present, so no create); then
    # the up inside the fixed secret wrapper, with no second base inspect. The script word is the module
    # constant, pinned literally in test_dev_up.py; everything after the wrapper's six words is the
    # file-based up, exactly.
    'up': [
        _EXPECTED_DEV_GUARD,
        *(
            [EXPECTED_DOCKER, 'image', 'inspect', '--format', '{{.Id}}', f'trader_joe_{name}:dev']
            for name in ('data_store', 'data_ingest', 'web')
        ),
        ['/usr/bin/bash', f'{EXPECTED_TRUSTED_DIR}/tools/source_digest.sh', '--context', '<HOST>', 'data', 'store'],
        ['/usr/bin/bash', f'{EXPECTED_TRUSTED_DIR}/tools/source_digest.sh', '--context', '<HOST>', 'data', 'ingest'],
        *_INSPECTS,
        [EXPECTED_DOCKER, 'compose', '-p', EXPECTED_DEV_PROJECT, 'down'],
        [EXPECTED_DOCKER, 'network', 'inspect', '--format', '{{.Name}}', 'trader_joe_devnet'],
        [
            'sh',
            '-c',
            stack.DEV_UP_WRAPPER_SCRIPT,
            'sh',
            f'{EXPECTED_TRUSTED_DIR}/tools/dev_instance_secret.sh',
            '<HOST>/.env',
            EXPECTED_DOCKER,
            'compose',
            '-p',
            EXPECTED_DEV_PROJECT,
            '--project-directory',
            '<HOST>',
            *(
                word
                for name in (
                    'docker-compose.yaml',
                    'docker-compose.override.yaml',
                    'docker-compose.web.yaml',
                    'docker-compose.web.dev.yaml',
                    'docker-compose.agent-dev.yaml',
                )
                for word in ('-f', f'{EXPECTED_TRUSTED_DIR}/{name}')
            ),
            'up',
            '-d',
            '--wait',
            '--wait-timeout',
            '300',
            'postgres',
            'data_store',
            'data_ingest',
            'web',
        ],
    ],
    # tj-iee6sn.10, `make migrate` on the mode that is up (the rig answers DEV): the mode guard; the lookup
    # of postgres (exactly one running) and of data_store (at most one); the inspect of the data_store :dev
    # image; the bases (the one-off run of data_store is flagged as building); then the alembic run inside
    # up's fixed secret wrapper. The TEST path's whole run is pinned in test_dev_migrate.py.
    'migrate': [
        _EXPECTED_DEV_GUARD,
        *(_expected_dev_lookup(service) for service in ('postgres', 'data_store')),
        [EXPECTED_DOCKER, 'image', 'inspect', '--format', '{{.Id}}', 'trader_joe_data_store:dev'],
        *_INSPECTS,
        _expected_dev_wrapped(['run', '--rm', '--no-deps', 'data_store', '/code/.venv/bin/alembic', 'upgrade', 'head']),
    ],
    # tj-iee6sn.14, `make dev-tools`: the D7 guard; the devnet inspect (present, so no create); then the
    # up of pgAdmin alone inside up's fixed secret wrapper, over the dev files with the TRUSTED
    # docker-compose.tools.yaml inserted BEFORE the MCP-only overlay (which stays last). postgres is not
    # named (compose starts it through pgadmin's depends_on); no image inspect, no digest, no base.
    'tools': [
        _EXPECTED_DEV_GUARD,
        [EXPECTED_DOCKER, 'network', 'inspect', '--format', '{{.Name}}', 'trader_joe_devnet'],
        [
            'sh',
            '-c',
            stack.DEV_UP_WRAPPER_SCRIPT,
            'sh',
            f'{EXPECTED_TRUSTED_DIR}/tools/dev_instance_secret.sh',
            '<HOST>/.env',
            EXPECTED_DOCKER,
            'compose',
            '-p',
            EXPECTED_DEV_PROJECT,
            '--project-directory',
            '<HOST>',
            *(
                word
                for name in (
                    'docker-compose.yaml',
                    'docker-compose.override.yaml',
                    'docker-compose.web.yaml',
                    'docker-compose.web.dev.yaml',
                    'docker-compose.tools.yaml',
                    'docker-compose.agent-dev.yaml',
                )
                for word in ('-f', f'{EXPECTED_TRUSTED_DIR}/{name}')
            ),
            'up',
            '-d',
            '--wait',
            'pgadmin',
        ],
    ],
    # tj-iee6sn.24, `make test-build`: the two trusted digests, the bases, then ONE file-based build of the
    # two TEST images over the trusted base file, docker-compose.fake.yaml (which names the :test tags)
    # and the MCP-only docker-compose.agent-test.yaml LAST. No guard: it starts nothing.
    'test_build': [
        ['/usr/bin/bash', f'{EXPECTED_TRUSTED_DIR}/tools/source_digest.sh', '--context', '<HOST>', 'data', 'store'],
        ['/usr/bin/bash', f'{EXPECTED_TRUSTED_DIR}/tools/source_digest.sh', '--context', '<HOST>', 'data', 'ingest'],
        *_INSPECTS,
        [
            EXPECTED_DOCKER,
            'compose',
            '-p',
            EXPECTED_DEV_PROJECT,
            '--project-directory',
            '<HOST>',
            *(
                word
                for name in ('docker-compose.yaml', 'docker-compose.fake.yaml', 'docker-compose.agent-test.yaml')
                for word in ('-f', f'{EXPECTED_TRUSTED_DIR}/{name}')
            ),
            'build',
            'data_store',
            'data_ingest',
        ],
    ],
    # tj-iee6sn.24, `make system-launch`: the mode guard; one inspect per :test image; the two digests; the
    # stamp read of each :test image; the bases; the label-only down; then ONE plain up (no secret wrapper,
    # no devnet: the base and fake files need neither) over the trusted base file and the fake overlay.
    'test_up': [
        _EXPECTED_DEV_GUARD,
        *(
            [EXPECTED_DOCKER, 'image', 'inspect', '--format', '{{.Id}}', f'trader_joe_{name}:test']
            for name in ('data_store', 'data_ingest')
        ),
        ['/usr/bin/bash', f'{EXPECTED_TRUSTED_DIR}/tools/source_digest.sh', '--context', '<HOST>', 'data', 'store'],
        ['/usr/bin/bash', f'{EXPECTED_TRUSTED_DIR}/tools/source_digest.sh', '--context', '<HOST>', 'data', 'ingest'],
        *(
            [
                EXPECTED_DOCKER,
                'image',
                'inspect',
                '--format',
                '{{ with .Config.Labels }}{{ index . "trader_joe.source.digest" }}{{ end }}',
                f'trader_joe_{name}:test',
            ]
            for name in ('data_store', 'data_ingest')
        ),
        *_INSPECTS,
        [EXPECTED_DOCKER, 'compose', '-p', EXPECTED_DEV_PROJECT, 'down'],
        [
            EXPECTED_DOCKER,
            'compose',
            '-p',
            EXPECTED_DEV_PROJECT,
            '--project-directory',
            '<HOST>',
            *(
                word
                for name in ('docker-compose.yaml', 'docker-compose.fake.yaml')
                for word in ('-f', f'{EXPECTED_TRUSTED_DIR}/{name}')
            ),
            'up',
            '-d',
            '--wait',
            '--wait-timeout',
            '300',
        ],
    ],
}
HOST_MARK = '<HOST>'

# THE SYSTEM-SUITE PAIR (tj-iee6sn.20; ADR tj-4rr0la addenda 26-27), whole argv from literals: the rails in
# order -- the mode guard; the postgres lookup and the ONE fixed inspect of its data mount; the data_store
# lookup; the two trusted digests; the data_ingest lookup and each running container's stamp read -- then
# the bases, then the client steps, NO secret wrapper (5a38b7f): `docker compose -p trader_joe_dev
# --project-directory <stack>/source --env-file <host>/.env` and the three trusted client files, then the
# tail. STACK_MARK is the rig's stack directory. tj-irhy0a.22 V1: seed_dump's producer is the ONE
# invocation make seed-dump also spells (ADR tj-4rr0la addendum 10 (1)); --date only when given.
STACK_MARK = '<STACK>'
_EXPECTED_CLIENT_PREFIX = [
    EXPECTED_DOCKER,
    'compose',
    '-p',
    EXPECTED_DEV_PROJECT,
    '--project-directory',
    '<STACK>/source',
    '--env-file',
    '<HOST>/.env',
    *(
        word
        for name in ('docker-compose.yaml', 'docker-compose.test-client.yaml', 'docker-compose.agent-client.yaml')
        for word in ('-f', f'{EXPECTED_TRUSTED_DIR}/{name}')
    ),
]
_EXPECTED_SYSTEM_SUITE_RAILS = [
    _EXPECTED_DEV_GUARD,
    _expected_dev_lookup('postgres'),
    [
        EXPECTED_DOCKER,
        'inspect',
        '--type',
        'container',
        '--format',
        '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Source}}{{end}}{{end}}',
        DEV_CONTAINER_ID,
    ],
    _expected_dev_lookup('data_store'),
    ['/usr/bin/bash', f'{EXPECTED_TRUSTED_DIR}/tools/source_digest.sh', '--context', '<HOST>', 'data', 'store'],
    ['/usr/bin/bash', f'{EXPECTED_TRUSTED_DIR}/tools/source_digest.sh', '--context', '<HOST>', 'data', 'ingest'],
    _expected_dev_lookup('data_ingest'),
    *(
        [
            EXPECTED_DOCKER,
            'inspect',
            '--type',
            'container',
            '--format',
            '{{ with .Config.Labels }}{{ index . "trader_joe.source.digest" }}{{ end }}',
            container,
        ]
        for container in (DEV_CONTAINER_ID, 'c0ffee000002')
    ),
]
_EXPECTED_SYSTEM_SUITE = {
    'run_system_tests': [
        *_EXPECTED_SYSTEM_SUITE_RAILS,
        *_INSPECTS,
        [*_EXPECTED_CLIENT_PREFIX, 'run', '--rm', '--no-deps', '--build', 'test_client', 'tests/system/test_one.py'],
    ],
    'seed_dump': [
        *_EXPECTED_SYSTEM_SUITE_RAILS,
        *_INSPECTS,
        [*_EXPECTED_CLIENT_PREFIX, 'build', 'test_client'],
        [
            *_EXPECTED_CLIENT_PREFIX,
            'run',
            '--rm',
            '-T',
            '--entrypoint',
            '/code/.venv/bin/python',
            'test_client',
            '-m',
            'data.store.seeds',
        ],
    ],
}


def _fill_marks(argv: list[str], host: str, stack_dir: str) -> list[str]:
    """ARGV with a word that is, or starts with, HOST_MARK or STACK_MARK filled in from the rig."""
    filled = []
    for word in argv:
        if word.startswith(HOST_MARK):
            word = host + word[len(HOST_MARK) :]
        elif word.startswith(STACK_MARK):
            word = stack_dir + word[len(STACK_MARK) :]
        filled.append(word)
    return filled


@pytest.mark.parametrize('verb', sorted(_EXPECTED_SYSTEM_SUITE))
def test_the_system_suite_pair_runs_exactly_its_fixed_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str):
    """tj-iee6sn.20: the pair's WHOLE argv in order, rails included, with nothing of the agent stack."""
    rig, result = _run_verb(tmp_path, monkeypatch, verb)
    assert result['status'] == 'ok', result
    host, stack_dir = str(rig.layout.repo_host_path), str(rig.layout.stack_dir)
    expected = [_fill_marks(argv, host, stack_dir) for argv in _EXPECTED_SYSTEM_SUITE[verb]]
    assert [list(step.argv) for step in rig.docker.steps] == expected


# compose's global options that take a value, as the verbs spell them before the subcommand.
_COMPOSE_GLOBALS_WITH_VALUE = ('-p', '--project-name', '--project-directory', '--env-file', '-f', '--file')


def _compose_argv(step: stack.Step) -> list[str] | None:
    """The `docker compose ...` argv a step runs, unwrapped from the secret wrapper; None for plain docker."""
    argv = list(dev_up_compose_argv(step)) if is_dev_up_wrapper(step) else list(step.argv)
    return argv if argv[:2] == [EXPECTED_DOCKER, 'compose'] else None


def _compose_tail(argv: list[str]) -> list[str]:
    """The words of a compose argv from its subcommand on: every global option and its value skipped.

    Parsed rather than sliced by a prefix length, so the label-only, file-based, wrapped and client
    prefixes are all read the same way and none can hide its subcommand behind a longer prefix.
    """
    words, index = argv[2:], 0
    while index < len(words) and words[index].startswith('-'):
        index += 2 if words[index] in _COMPOSE_GLOBALS_WITH_VALUE else 1
    return words[index:]


@pytest.mark.parametrize('verb', sorted(_EXPECTED_TAILS_DEV))
def test_every_dev_verb_runs_exactly_its_fixed_read_only_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
):
    """The dev pair's WHOLE argv, spelled from literals: four prefix words, then a read-only tail.

    Whole rather than a tail, because for these verbs the prefix is the containment: a -f, a second
    -p or an --env-file appearing here is the failure the verbs were allowed on condition of
    preventing. The deeper pins -- that no other project is reachable by any accepted argument and
    that no mutating command is constructible -- are tj-tq2hn6's (B2).
    """
    rig, result = _run_verb(tmp_path, monkeypatch, verb)
    assert result['status'] == 'ok', result
    host = str(rig.layout.repo_host_path)
    # A word that IS the mark, or starts with it (up's `<HOST>/.env`), names the rig's host path.
    expected = [
        [host + word[len(HOST_MARK) :] if word.startswith(HOST_MARK) else word for word in argv]
        for argv in _EXPECTED_TAILS_DEV[verb]
    ]
    assert [list(step.argv) for step in rig.docker.steps] == expected


def test_every_verb_with_a_fixed_argv_is_covered():
    """A verb that runs docker and has no whole-argv entry would escape the argv pins."""
    assert set(_EXPECTED_TAILS_DEV) | set(_EXPECTED_SYSTEM_SUITE) == set(VERB_SAMPLES) - DOCKERLESS_VERBS
    assert set(_EXPECTED_TAILS_DEV) == DEV_PROJECT_VERBS
    assert set(_EXPECTED_SYSTEM_SUITE) == SYSTEM_SUITE_VERBS


# --- the constants agree with the files that define them ----------------------------------------


def test_every_file_based_prefix_names_only_the_trusted_copies():
    """D1: every -f any verb passes lies under TRUSTED_COMPOSE_DIR, never the worktree or the checkout."""
    assert str(stack.TRUSTED_COMPOSE_DIR) == EXPECTED_TRUSTED_DIR
    assert str(stack.TRUSTED_DOCKERFILE) == f'{EXPECTED_TRUSTED_DIR}/Dockerfile'
    assert stack.DOCKER == '/usr/local/bin/docker' and stack.DOCKER_HOST == 'tcp://socket_proxy:2375'
    files = {
        value
        for argvs in (*_EXPECTED_TAILS_DEV.values(), *_EXPECTED_SYSTEM_SUITE.values())
        for argv in argvs
        for flag, value in pairwise(argv)
        if flag in ('-f', '--file')
    }
    assert files, 'no expected argv passes a compose file, so this pin would guard nothing'
    assert all(posixpath.dirname(value) == EXPECTED_TRUSTED_DIR for value in files), sorted(files)
    assert f'{EXPECTED_TRUSTED_DIR}/{RETIRED_OVERLAY}' not in files


def _mcp_dockerfile_copies() -> list[tuple[list[str], str]]:
    """(sources, destination) of every context COPY in tools/agent_mcp/Dockerfile."""
    copies = []
    for line in MCP_DOCKERFILE.read_text(encoding='utf-8').replace('\\\n', ' ').splitlines():
        words = shlex.split(line, comments=True)
        if words[:1] != ['COPY'] or any(word.startswith('--from') for word in words[1:]):
            continue
        operands = [word for word in words[1:] if not word.startswith('--')]
        copies.append((operands[:-1], operands[-1]))
    return copies


# What tj-iee6sn.5 adds to the trusted directory for the USER'S DEV project (ADR tj-4rr0la addendum
# 23), spelled from literals: the dev set's files the agent stack does not already bake, pgAdmin's file
# for tools (B8), and the two scripts the dev verbs run instead of the checkout's.
EXPECTED_DEV_BAKES = {
    'docker-compose.override.yaml': f'{EXPECTED_TRUSTED_DIR}/docker-compose.override.yaml',
    'docker-compose.web.yaml': f'{EXPECTED_TRUSTED_DIR}/docker-compose.web.yaml',
    'docker-compose.web.dev.yaml': f'{EXPECTED_TRUSTED_DIR}/docker-compose.web.dev.yaml',
    'docker-compose.agent-dev.yaml': f'{EXPECTED_TRUSTED_DIR}/docker-compose.agent-dev.yaml',
    'docker-compose.tools.yaml': f'{EXPECTED_TRUSTED_DIR}/docker-compose.tools.yaml',
    # tj-iee6sn.24: test_build's MCP-only overlay (build.dockerfile for data_store and data_ingest).
    'docker-compose.agent-test.yaml': f'{EXPECTED_TRUSTED_DIR}/docker-compose.agent-test.yaml',
    # tj-iee6sn.20: the system-test client's MCP-only overlay (build.dockerfile for test_client alone).
    'docker-compose.agent-client.yaml': f'{EXPECTED_TRUSTED_DIR}/docker-compose.agent-client.yaml',
    'tools/dev_instance_secret.sh': f'{EXPECTED_TRUSTED_DIR}/tools/dev_instance_secret.sh',
    'tools/source_digest.sh': f'{EXPECTED_TRUSTED_DIR}/tools/source_digest.sh',
    # tj-iee6sn.33: the generated-code guard's content stamp, recomputed by the baked copy only.
    'tools/proto_digest.sh': f'{EXPECTED_TRUSTED_DIR}/tools/proto_digest.sh',
}


def _baked_into_trusted() -> dict[str, str]:
    """Source -> the file it lands at in the image, for every COPY into TRUSTED_COMPOSE_DIR or below it."""
    into_trusted = {}
    for sources, destination in _mcp_dockerfile_copies():
        if not (destination.rstrip('/') == EXPECTED_TRUSTED_DIR or destination.startswith(EXPECTED_TRUSTED_DIR + '/')):
            continue
        for source in sources:
            # COPY semantics: a destination ending in '/' is a directory, and the file keeps its basename.
            into_trusted[source] = (
                f'{destination.rstrip("/")}/{posixpath.basename(source)}'
                if destination.endswith('/') or destination.rstrip('/') == EXPECTED_TRUSTED_DIR
                else destination
            )
    return into_trusted


def test_the_mcp_image_bakes_in_exactly_the_trusted_compose_files_and_dockerfile():
    """D1 and O3: the image COPYs EXPECTED_BASE_BAKES and the root Dockerfile into TRUSTED_COMPOSE_DIR.

    tj-iee6sn.5 widened the set by exactly EXPECTED_DEV_BAKES, each at the path the dev verbs name, and
    tj-iee6sn.21 narrowed it by the agent-stack overlay, which no verb loads any more.
    """
    into_trusted = _baked_into_trusted()
    assert RETIRED_OVERLAY not in into_trusted, f'the image still bakes {RETIRED_OVERLAY}'
    assert set(into_trusted) == {*EXPECTED_BASE_BAKES, 'Dockerfile', *EXPECTED_DEV_BAKES}, into_trusted
    assert into_trusted['Dockerfile'] == str(stack.TRUSTED_DOCKERFILE)
    for source, landed in EXPECTED_DEV_BAKES.items():
        assert into_trusted[source] == landed, (source, into_trusted[source])


def test_the_mcp_image_bakes_in_the_fake_overlay_and_admits_it_but_never_the_fakes():
    """tj-vhboky.61, D1: the MCP image carries its own copy of docker-compose.fake.yaml, never tests/.

    test_up and test_build pass -f for it under TRUSTED_COMPOSE_DIR, so docker-compose.fake.yaml must be
    COPYd there and re-included by Dockerfile.dockerignore -- named literally, so dropping it from the
    module and the image together still goes red. The fakes themselves reach test_client only through
    the snapshot's :ro mount: the MCP image never COPYs anything under tests/, and its build context
    leaves tests/fakes out.
    """
    copied = {source: destination for sources, destination in _mcp_dockerfile_copies() for source in sources}
    assert copied.get(FAKE_OVERLAY.name, '').rstrip('/') == EXPECTED_TRUSTED_DIR, copied
    rules = _dockerignore_rules(MCP_DOCKERIGNORE.read_text(encoding='utf-8'))
    assert not _is_excluded_from_context(FAKE_OVERLAY.name, rules), 'the MCP build context leaves out the fake overlay'
    assert [source for source in copied if source == 'tests' or source.startswith(('tests/', './tests'))] == []
    fakes = sorted(str(path.relative_to(REPO_ROOT)) for path in (REPO_ROOT / 'tests' / 'fakes').glob('*.py'))
    assert 'tests/fakes/ingest_launcher.py' in fakes, fakes
    assert [path for path in fakes if not _is_excluded_from_context(path, rules)] == []


def test_the_mcp_build_context_admits_the_trusted_files_and_nothing_live():
    """D1 and O3: Dockerfile.dockerignore re-includes each file the image COPYs, and leaves out env files and tests."""
    rules = _dockerignore_rules(MCP_DOCKERIGNORE.read_text(encoding='utf-8'))
    assert _is_excluded_from_context(RETIRED_OVERLAY, rules), f'the MCP build context still admits {RETIRED_OVERLAY}'
    sent = [
        *EXPECTED_BASE_BAKES,
        *EXPECTED_DEV_BAKES,
        'Dockerfile',
        'pyproject.toml',
        'uv.lock',
        'tools/__init__.py',
        *(f'tools/agent_mcp/{path.name}' for path in MCP_DIR.glob('*.py')),
    ]
    assert [path for path in sent if _is_excluded_from_context(path, rules)] == []
    kept_out = [
        '.env',
        'server/data/store/.env',
        'server/data/ingest/.env',
        'volumes/trader_joe/postgres/PG_VERSION',
        'tools/agent_mcp/tests/test_commands.py',
        '.claude/worktrees/x/docker-compose.yaml',
        'server/common/__init__.py',
        '.git/config',
    ]
    assert [path for path in kept_out if not _is_excluded_from_context(path, rules)] == []


# --- the build-context WALK: what BuildKit opens, not only what it sends (tj-c4mosr.11) -----------
#
# Re-implements two upstream rules, so what is pinned is the walk BuildKit's sender performs over
# the real checkout -- the defect at 32abf36 left the include set exactly right and still opened
# every directory, including ${DATA_DIR}'s unreadable postgres directory.
#
# moby/patternmatcher ignorefile.ReadAll: '#' lines and blanks dropped, '!' marks an exception,
# the pattern is filepath.Clean-ed and a leading '/' stripped.
#
# tonistiigi/fsutil filter.go: an excluded DIRECTORY is skipped -- never opened -- only while
# onlyPrefixExcludeExceptions holds AND no exception pattern lies under it (pattern + '/' starts with
# dir + '/'). onlyPrefixExcludeExceptions is false as soon as one exception, after a trailing '/**'
# and then a trailing '/*' is stripped, still contains a character of "*[]?^\" (patternChars, with
# the backslash added on a '/'-separated platform).

_FSUTIL_PATTERN_CHARS = '*[]?^\\'


def _dockerignore_patterns(text: str) -> list[tuple[bool, str]]:
    """(is_exception, cleaned pattern) per line, as ignorefile.ReadAll yields them."""
    patterns = []
    for line in text.splitlines():
        if line.startswith('#'):
            continue
        stripped = line.strip()
        if not stripped:
            continue
        exception = stripped.startswith('!')
        if exception:
            stripped = stripped[1:].strip()
        cleaned = posixpath.normpath(stripped)
        if len(cleaned) > 1 and cleaned.startswith('/'):
            cleaned = cleaned[1:]
        patterns.append((exception, cleaned))
    return patterns


def _pruning_offenders(patterns: list[tuple[bool, str]]) -> list[str]:
    """The exception patterns that turn fsutil's onlyPrefixExcludeExceptions off -- empty when it holds."""
    offenders = []
    for exception, pattern in patterns:
        if not exception:
            continue
        stripped = pattern.removesuffix('/**').removesuffix('/*')
        if any(char in _FSUTIL_PATTERN_CHARS for char in stripped):
            offenders.append(f'!{pattern}')
    return offenders


def _walk_build_context(root: Path, text: str, may_open: set[str]) -> tuple[list[str], set[str]]:
    """The filtered walk fsutil performs over `root`: (directories opened, files sent).

    Stops at the first directory opened outside `may_open`: that is already the failure, and an
    unpruned walk of a real checkout would otherwise descend .venv, .git, every sibling worktree
    and the data directory -- where it meets EACCES exactly as the host build did.
    """
    patterns = _dockerignore_patterns(text)
    rules = [(exception, _dockerignore_pattern_regex(pattern)) for exception, pattern in patterns if pattern != '.']
    exceptions = [pattern for exception, pattern in patterns if exception]
    prune = not _pruning_offenders(patterns)
    opened, sent, pending = [], set(), ['.']
    while pending:
        directory = pending.pop()
        opened.append(directory)
        if directory not in may_open:
            break
        for entry in sorted(os.scandir(root / directory), key=lambda entry: entry.name):
            path = entry.name if directory == '.' else f'{directory}/{entry.name}'
            excluded = _is_excluded_from_context(path, rules)
            if entry.is_dir(follow_symlinks=False):
                under_an_exception = any(f'{pattern}/'.startswith(f'{path}/') for pattern in exceptions)
                if not (excluded and prune and not under_an_exception):
                    pending.append(path)
            elif not excluded:
                sent.add(path)
    return opened, sent


def _mcp_copy_sources_in_checkout() -> set[str]:
    """Every context COPY source of tools/agent_mcp/Dockerfile, globbed against the real checkout."""
    files = set()
    for sources, _ in _mcp_dockerfile_copies():
        for source in sources:
            matched = [path for path in REPO_ROOT.glob(source) if path.is_file()]
            assert matched, f'COPY source {source} matches nothing in the checkout'
            files |= {path.relative_to(REPO_ROOT).as_posix() for path in matched}
    return files


def test_no_exception_line_in_the_mcp_dockerignore_disables_pruning():
    """tj-c4mosr.11: one wildcard in one '!' line turns pruning off for the whole file.

    Then BuildKit's sender opens every directory of the checkout, and the host build died on
    'open .../volumes/trader_joe/postgres: permission denied'. A wildcard on an EXCLUDE line is
    fine; only exception lines govern pruning.
    """
    patterns = _dockerignore_patterns(MCP_DOCKERIGNORE.read_text(encoding='utf-8'))
    assert any(exception for exception, _ in patterns), 'no exception lines: this is no longer an allow-list'
    assert _pruning_offenders(patterns) == [], (
        'these exception lines keep a wildcard after the trailing-glob strip, so fsutil prunes no '
        'excluded directory and the build walks the data directory; re-include a directory instead'
    )


def test_the_mcp_build_context_walk_opens_only_the_copy_directories_and_sends_exactly_the_copy_sources():
    """tj-c4mosr.11 over the real checkout: the walk opens '.' and the COPY sources' directories, nothing else.

    Both sides are derived from tools/agent_mcp/Dockerfile, so a COPY source added to the Dockerfile
    and the dockerignore together stays green, and one added to only one of them goes red.
    """
    expected = _mcp_copy_sources_in_checkout()
    may_open = {'.'} | {posixpath.dirname(path) or '.' for path in expected}
    opened, sent = _walk_build_context(REPO_ROOT, MCP_DOCKERIGNORE.read_text(encoding='utf-8'), may_open)
    assert [directory for directory in opened if directory not in may_open] == [], (
        f'the build-context walk opens directories outside {sorted(may_open)}'
    )
    assert sent == expected, (
        f'sent but not COPYed: {sorted(sent - expected)}; COPYed but not sent: {sorted(expected - sent)}'
    )


def _relative_bind_sources(path: Path) -> set[str]:
    sources = set()
    for spec in (yaml.safe_load(path.read_text(encoding='utf-8')).get('services') or {}).values():
        for entry in (spec or {}).get('volumes') or []:
            source = _short_volume_source(entry) if isinstance(entry, str) else str(entry.get('source', ''))
            if source.startswith('.'):
                sources.add(os.path.normpath(source))
    return sources - {'.'}


def test_snapshot_sources_are_exactly_what_the_trusted_files_read_from_the_project_directory():
    """S2: SNAPSHOT_SOURCES == the Dockerfile's COPY sources UNION the client files' relative bind sources.

    Parsed, so a new COPY or relative mount without an entry here goes red -- and an entry nothing
    reads goes red too. The build context '.' is the snapshot root itself. Since tj-iee6sn.21 the
    snapshot is the project directory of the system-test client ALONE (CLIENT_FILES, spelled in
    harness.py from literals), so only those files' binds count.
    """
    expected = set(_dockerfile_copy_sources())
    for name in CLIENT_FILES:
        expected |= _relative_bind_sources(REPO_ROOT / name)
    # The service trees moved under server/ (tj-iontkq.4); tests/ and pytest.ini did not.
    floor = {'server/data/store/migrations', 'tests/system', 'tests/fakes', 'server/data/ingest/app', 'pytest.ini'}
    assert floor <= expected, expected
    assert set(stack.SNAPSHOT_SOURCES) == expected, (
        f'SNAPSHOT_SOURCES {sorted(stack.SNAPSHOT_SOURCES)} vs what the trusted files read {sorted(expected)}'
    )
    assert len(stack.SNAPSHOT_SOURCES) == len(set(stack.SNAPSHOT_SOURCES))


# --- only the launchers and restart start or recreate a service ----------------------------------
#
# The agent stack's stack_up was the one verb that force-recreated running services (tj-zgq5v2); it is gone
# (tj-iee6sn.21). What may start a service now: the mode launchers up, test_up and tools (each an
# `up`), and restart by owner ruling (tj-iee6sn.1 D1, bead tj-iee6sn.7). Every other verb starts,
# restarts, creates and recreates nothing: run_system_tests and seed_dump in particular must not restart
# test mode between a validator's focused runs -- they create test_client alone, by `run`.
_STARTING_VERBS = {'up': 'up', 'test_up': 'up', 'tools': 'up', 'restart': 'restart'}
_STARTING_SUBCOMMANDS = ('up', 'restart', 'create', 'start')


def _compose_tails(rig) -> list[tuple[stack.Step, list[str]]]:
    """Every compose step's words from its subcommand on, wrapped (the secret wrapper) or not."""
    tails = []
    for step in rig.docker.steps:
        argv = _compose_argv(step)
        if argv is not None:
            tails.append((step, _compose_tail(argv)))
    return tails


@pytest.mark.parametrize('verb', sorted(set(VERB_SAMPLES) - DOCKERLESS_VERBS))
def test_only_the_launchers_and_dev_restart_start_a_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str):
    """Each launcher runs exactly its one starting subcommand; no other verb runs any, nor a recreate flag."""
    rig, result = _run_verb(tmp_path, monkeypatch, verb)
    assert result['status'] == 'ok', result
    tails = _compose_tails(rig)
    if verb in SYSTEM_SUITE_VERBS:
        assert [step for step, _ in tails if is_client_step(step)], f'{verb}: no client step was swept'
    starting = [tail[0] for _, tail in tails if tail and tail[0] in _STARTING_SUBCOMMANDS]
    assert starting == ([_STARTING_VERBS[verb]] if verb in _STARTING_VERBS else []), f'{verb} runs {tails}'
    if verb not in _STARTING_VERBS:
        for _, tail in tails:
            assert '--force-recreate' not in tail and '--always-recreate-deps' not in tail, f'{verb} runs {tail}'


def test_the_compose_tail_reader_skips_every_prefix_the_verbs_spell():
    """The reader under the sweep above: the label-only, file-based and client prefixes all reach `up`."""
    prefixes = [
        [EXPECTED_DOCKER, 'compose', '-p', EXPECTED_DEV_PROJECT],
        [
            EXPECTED_DOCKER,
            'compose',
            '-p',
            EXPECTED_DEV_PROJECT,
            '--project-directory',
            '/h',
            '-f',
            '/a.yaml',
            '-f',
            '/b.yaml',
        ],
        [EXPECTED_DOCKER, 'compose', '-p', EXPECTED_DEV_PROJECT, '--project-directory', '/s', '--env-file', '/h/.env'],
    ]
    for prefix in prefixes:
        assert _compose_tail([*prefix, 'up', '-d', 'postgres']) == ['up', '-d', 'postgres'], prefix


# --- git: plumbing only, hardened ---------------------------------------------------------------


def test_run_git_is_hardened_plumbing_with_a_fixed_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """D3: --no-pager, fsmonitor off, hooksPath /dev/null, no system config, no HOME, argument list, no shell."""
    seen = {}

    class _Done:
        returncode, stdout, stderr = 0, 'out', ''

    def fake_run(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
        return _Done()

    monkeypatch.setattr(stack.subprocess, 'run', fake_run)
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('GIT_DIR', str(tmp_path / 'elsewhere'))
    assert stack.run_git(tmp_path, ['worktree', 'list', '--porcelain']) == 'out'
    assert seen['argv'] == [
        '/usr/bin/git',
        '--no-pager',
        '-c',
        'core.fsmonitor=false',
        '-c',
        'core.hooksPath=/dev/null',
        'worktree',
        'list',
        '--porcelain',
    ]
    assert seen['env'] == {
        'PATH': '/usr/bin:/bin',
        'GIT_CONFIG_NOSYSTEM': '1',
        'GIT_TERMINAL_PROMPT': '0',
        'GIT_PAGER': 'cat',
        'LC_ALL': 'C',
    }
    assert 'HOME' not in seen['env'] and not seen.get('shell')
    assert seen['cwd'] == tmp_path and seen['timeout'] and seen['check'] is False


def _run_git_calls() -> list[ast.Call]:
    calls = []
    for path in sorted(MCP_DIR.glob('*.py')):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(node, ast.Call) and getattr(node.func, 'attr', getattr(node.func, 'id', None)) == 'run_git':
                calls.append(node)
    return calls


def test_the_only_git_command_is_worktree_list():
    """D3: the one run_git call site passes `worktree list --porcelain`, spelled as literals.

    The committed-defaults `cat-file blob HEAD:<path>` and the second worktree-list site went with the
    agent stack's generated env files (tj-iee6sn.21); FakeGit fails any other git command at run time.
    """
    calls = _run_git_calls()
    assert len(calls) == 1, f'expected the one known run_git call site, found {[ast.unparse(c) for c in calls]}'
    args = calls[0].args[1]
    assert isinstance(args, ast.List), ast.unparse(calls[0])
    assert [ast.unparse(element) for element in args.elts] == ["'worktree'", "'list'", "'--porcelain'"]
    assert 'cat-file' not in (MCP_DIR / 'stack.py').read_text(encoding='utf-8')


# --- seed_dump and the error statuses ----------------------------------------------------------


def _server_error_statuses() -> set[str]:
    """server.ERROR_STATUSES, read by parsing: server.py imports the MCP SDK, which this venv omits."""
    tree = ast.parse((MCP_DIR / 'server.py').read_text(encoding='utf-8'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and [ast.unparse(target) for target in node.targets] == ['ERROR_STATUSES']:
            return set(ast.literal_eval(node.value.args[0]))
    raise AssertionError('server.py defines no ERROR_STATUSES')


def test_seed_dump_runs_and_answers_ok_and_the_error_statuses_are_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """(13), re-pinned by tj-irhy0a.22 item 4 ('Remove the not_available answer').

    seed_dump now runs docker and answers 'ok' on a valid bundle; the server's error statuses are
    still the five, so 'refused' (the producer's exit 3) and 'failed' (a refused bundle) read as
    errors and 'ok' does not.
    """
    rig = make_rig(tmp_path, monkeypatch)
    rig.docker.respond = response_for('seed_dump', rig.layout)
    result = rig.call('seed_dump', {'worktree': WORKTREE_NAME})
    assert result['status'] == 'ok' and rig.docker.steps, result
    statuses = _server_error_statuses()
    assert statuses == {'refused', 'busy', 'failed', 'timeout', 'error'}
    assert 'ok' not in statuses and 'not_available' not in statuses


# --- the one reviewed suppression --------------------------------------------------------------

_NOSEMGREP_RULE = 'python.lang.security.audit.insecure-file-permissions.insecure-file-permissions'


def test_stack_py_carries_exactly_the_one_reviewed_suppression_pair():
    """02:32 note / ADR tj-4rr0la addendum 6: one nosemgrep and one nosec B103, both on the snapshot-dir fchmod.

    The rule for any suppression in this repository: one line, one named rule id, an architect
    ruling in the owning ADR. This pins that stack.py has no other.
    """
    source = (MCP_DIR / 'stack.py').read_text(encoding='utf-8')
    lines = source.splitlines()
    nosemgrep = [index for index, line in enumerate(lines) if 'nosemgrep' in line]
    b103 = [index for index, line in enumerate(lines) if re.search(r'nosec\s+B103\b', line)]
    assert len(nosemgrep) == 1 and len(b103) == 1, (nosemgrep, b103)
    assert lines[nosemgrep[0]].strip() == f'# nosemgrep: {_NOSEMGREP_RULE}'
    fchmod = b103[0]
    assert nosemgrep[0] == fchmod - 1, 'the nosemgrep must sit on the line directly above the fchmod it covers'
    assert lines[fchmod].strip().startswith('os.fchmod(fd, 0o755)'), lines[fchmod]
    function = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == '_make_snapshot_dir'
    )
    assert function.lineno <= fchmod + 1 <= function.end_lineno, 'the suppressed line is outside _make_snapshot_dir'
    nosec = [line for line in lines if re.search(r'#\s*nosec\b', line)]
    assert all(re.search(r'#\s*nosec\s+B\d{3}\b', line) for line in nosec), f'a blanket nosec: {nosec}'
