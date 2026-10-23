"""The dev-project plumbing the file-based dev verbs stand on (tj-iee6sn.5; ADR tj-4rr0la addendum 23).

No verb uses these yet; the later beads' mirror tests (B5 build, B6 up, B8 tools) take
them as given, so what is pinned here is that they agree with the Makefile and with the MCP image:

  * stack.DEV_COMPOSE_FILES is the Makefile's DEV_COMPOSE -f list, in its order, then the MCP-only
    overlay docker-compose.agent-dev.yaml LAST;
  * that overlay sets build.dockerfile to the trusted Dockerfile for exactly the services the dev set
    builds, and nothing else, so the merged dev model differs from the Makefile's in that key only;
  * make dev-up is a down of the dev project through the dev file set, then the detached up of the
    four services, both naming the same project in any checkout, and neither line can grow a
    destructive option unnoticed; make dev-logs follows the three apps;
  * the MCP image bakes every file the dev verbs name, at the path they name, and its build context
    admits each one;
  * stack.dev_file_prefix() spells the trusted copies, the host path and no env file.

Expected values are literals, not read from stack.py: a test that took them from the module under test
would agree with any change to it.
"""

import inspect
import shlex
from itertools import pairwise
from pathlib import Path

import pytest

from common.tests import compose_model
from common.tests.test_ci_invariants import (
    _dockerignore_rules,
    _is_excluded_from_context,
    _made_variable,
    _make_recipe,
    _make_variable,
)
from common.tests.test_grpc_bind_network import _set_files
from tools.agent_mcp import stack
from tools.agent_mcp.tests.harness import REPO_ROOT
from tools.agent_mcp.tests.test_commands import (
    EXPECTED_DEV_BAKES,
    EXPECTED_DOCKER,
    EXPECTED_TRUSTED_DIR,
    MCP_DOCKERIGNORE,
    _baked_into_trusted,
    _dockerignore_patterns,
    _pruning_offenders,
)


pytestmark = pytest.mark.build_infra

MAKEFILE = REPO_ROOT / 'Makefile'
OVERLAY_NAME = 'docker-compose.agent-dev.yaml'
OVERLAY = REPO_ROOT / OVERLAY_NAME
# The MCP's DEV_PROJECT since tj-iee6sn.31 and the Makefile's since tj-grna9p.161 (ADR tj-4rr0la
# addendum 29, decision tj-oxxl8a option A): ONE name on both sides. Spelled out, not read from
# stack.py or the Makefile, so the oracle is not the file under test.
EXPECTED_DEV_PROJECT = 'trader_joe_dev'
MAKEFILE_DEV_PROJECT = 'trader_joe_dev'
# The Makefile's DEV_COMPOSE -f list (base, override, web, web dev), then the overlay.
EXPECTED_DEV_COMPOSE_FILES = (
    'docker-compose.yaml',
    'docker-compose.override.yaml',
    'docker-compose.web.yaml',
    'docker-compose.web.dev.yaml',
    OVERLAY_NAME,
)
# The three services the dev set builds: data_store and data_ingest from the base file, web from the
# web overlay. postgres and pgAdmin run stock images.
EXPECTED_BUILT_SERVICES = frozenset({'data_store', 'data_ingest', 'web'})
TRUSTED_DOCKERFILE = f'{EXPECTED_TRUSTED_DIR}/Dockerfile'

# Ruling R6 on tj-iee6sn.1, as the architect's 22:45 note on tj-iee6sn.5 spells it: "make dev-up = the
# dev-down recipe, then $(DEV_COMPOSE) up -d --wait --wait-timeout 300 postgres data_store data_ingest
# web". The down goes through the dev file set (TOOLS_COMPOSE, so pgAdmin goes too) and so through
# DEV_COMPOSE's one -p $(DEV_PROJECT): the down and the up name the same project by construction,
# so the down is guaranteed to stop the instance the up replaces, in any checkout. Its
# --remove-orphans is dropped (D2): with pgAdmin's file loaded there is no orphan it would need to catch.
# The PGADMIN placeholders are dev-down's: `down` creates no container, so no account is made from them.
EXPECTED_DEV_UP_RECIPE = [
    'PGADMIN_EMAIL=unused PGADMIN_PASS=unused $(TOOLS_COMPOSE) down',
    '$(DEV_COMPOSE) up -d --wait --wait-timeout 300 postgres data_store data_ingest web',
]
# Options that would set the project apart from compose's default. Neither set may carry one, so the
# down and the up of dev-up derive the same project name by construction.
PROJECT_OPTIONS = ('-p', '--project-name', '--project-directory')
EXPECTED_DEV_LOGS_RECIPE = ['$(DEV_COMPOSE) logs -f data_store data_ingest web']
# Never in a dev-up line (D2; addendum 23 (2) NEVER CONSTRUCTIBLE): the dev database survives a down,
# and pgAdmin, whose file DEV_COMPOSE does not load, is not deleted as an orphan.
DESTRUCTIVE_WORDS = ('-v', '--volumes', '--rmi', '--remove-orphans', 'prune', 'rm')


# --- the dev file set mirrors the Makefile ----------------------------------------------------------


def test_the_dev_file_set_is_the_makefile_dev_compose_list_then_the_overlay_last():
    """The first four are DEV_COMPOSE's -f list as make itself expands it, in order; the fifth is the overlay.

    A file added to, dropped from or reordered in DEV_COMPOSE turns this red, so the MCP's dev verbs
    cannot silently load a different project definition from the one `make dev-launch` loads.
    """
    makefile_files = [path.name for path in _set_files('DEV_COMPOSE')]
    assert tuple(stack.DEV_COMPOSE_FILES) == EXPECTED_DEV_COMPOSE_FILES, stack.DEV_COMPOSE_FILES
    assert list(stack.DEV_COMPOSE_FILES[:-1]) == makefile_files, (stack.DEV_COMPOSE_FILES, makefile_files)
    assert stack.DEV_COMPOSE_FILES[-1] == OVERLAY_NAME
    assert len(set(stack.DEV_COMPOSE_FILES)) == len(stack.DEV_COMPOSE_FILES), stack.DEV_COMPOSE_FILES


def test_every_dev_file_exists_in_the_checkout():
    """The image COPYs each from the checkout, and the stale-copy refusal (S6) compares each with it."""
    missing = [name for name in stack.DEV_COMPOSE_FILES if not (REPO_ROOT / name).is_file()]
    assert not missing, missing


# --- the overlay: build.dockerfile and nothing else -------------------------------------------------


def test_the_overlay_sets_only_build_dockerfile_to_the_trusted_dockerfile_for_the_three_built_services():
    """Exactly services.{data_store,data_ingest,web}.build.dockerfile, the absolute trusted path.

    Parsed as a whole document, so a second key anywhere (an image tag, a volume, a network, an
    environment entry, a build arg, a top-level key) is red, and so is a service added or dropped.
    """
    document = compose_model.load(OVERLAY)
    assert document == {
        'services': {service: {'build': {'dockerfile': TRUSTED_DOCKERFILE}} for service in EXPECTED_BUILT_SERVICES}
    }, document
    assert str(stack.TRUSTED_DOCKERFILE) == TRUSTED_DOCKERFILE


def test_the_overlay_covers_every_service_the_dev_set_builds():
    """Derived from the Makefile's dev set, so a fourth built service there is red until the overlay covers it.

    A built service the overlay missed would read the CHECKOUT's Dockerfile, which an agent can write.
    """
    model = compose_model.merge([compose_model.load(path) for path in _set_files('DEV_COMPOSE')])
    built = {name for name, service in model['services'].items() if 'build' in service}
    assert built == EXPECTED_BUILT_SERVICES, sorted(built)
    assert set(compose_model.load(OVERLAY)['services']) == built


def test_the_merged_dev_model_differs_from_the_makefiles_in_build_dockerfile_only():
    """The containers the MCP makes are the containers make dev-launch makes, but for the Dockerfile read.

    Compose merges build keys, so context, target and args stay from the files above it. Merged here
    the same way, and compared whole.
    """
    makefile_model = compose_model.merge([compose_model.load(path) for path in _set_files('DEV_COMPOSE')])
    mcp_model = compose_model.merge([compose_model.load(REPO_ROOT / name) for name in stack.DEV_COMPOSE_FILES])
    for service in EXPECTED_BUILT_SERVICES:
        assert mcp_model['services'][service]['build']['dockerfile'] == TRUSTED_DOCKERFILE, service
        assert makefile_model['services'][service]['build'].get('dockerfile', 'Dockerfile') == 'Dockerfile', service
        mcp_model['services'][service]['build'].pop('dockerfile')
        makefile_model['services'][service]['build'].pop('dockerfile', None)
    assert mcp_model == makefile_model


def test_no_makefile_line_and_no_compose_set_loads_the_overlay():
    """MCP-only: its dockerfile path exists only inside the MCP image, so loading it anywhere else fails."""
    code = [line for line in MAKEFILE.read_text(encoding='utf-8').splitlines() if not line.lstrip().startswith('#')]
    assert not [line for line in code if OVERLAY_NAME in line], 'a Makefile line names the MCP-only overlay'
    # Within the MCP, only the two dev sets load it; test mode's and the client's never do (re-pointed from
    # the retired agent-stack COMPOSE_FILES, tj-iee6sn.21).
    for files in (stack.TEST_COMPOSE_FILES, stack.TEST_BUILD_COMPOSE_FILES, stack.SYSTEM_CLIENT_COMPOSE_FILES):
        assert OVERLAY_NAME not in files, files
    assert OVERLAY_NAME in stack.DEV_COMPOSE_FILES and OVERLAY_NAME in stack.DEV_TOOLS_COMPOSE_FILES


# --- make dev-up and make dev-logs -------------------------------------------------------------------


def test_dev_up_downs_the_dev_project_then_ups_postgres_and_the_apps_detached():
    """R6: the two lines, exactly, in this order: the down runs BEFORE the up.

    Exact, so the B6 mirror test has one spelling to compare up's steps against, and so an option
    added to either line is red here before anything else sees it.
    """
    recipe = _make_recipe('dev-up')
    assert recipe == EXPECTED_DEV_UP_RECIPE, recipe
    down, up = (shlex.split(line) for line in recipe)
    assert down[-2:] == ['$(TOOLS_COMPOSE)', 'down'], f'the down is a bare down through the dev set: {down}'
    assert up[0] == '$(DEV_COMPOSE)' and up[1] == 'up', up


def test_dev_up_downs_and_ups_the_same_compose_project_in_any_checkout():
    """The down's set is the up's set plus pgAdmin's file, and both carry DEV_COMPOSE's one -p.

    Re-pinned for tj-grna9p.161 (ADR tj-4rr0la addendum 29): DEV_COMPOSE passes -p $(DEV_PROJECT),
    DEV_PROJECT is trader_joe_dev, and nothing built on it -- TOOLS_COMPOSE, either dev-up line --
    passes a project of its own. A second -p on the down alone would stop a different project from
    the one the up starts, leaving the instance dev-up exists to replace running; the one inherited
    -p makes both lines name trader_joe_dev in any checkout, and never the default project prod keeps.
    """
    tools_set = _make_variable('TOOLS_COMPOSE')
    assert shlex.split(tools_set)[0] == '$(DEV_COMPOSE)', tools_set
    assert [path.name for path in _set_files('TOOLS_COMPOSE')] == [
        *(path.name for path in _set_files('DEV_COMPOSE')),
        'docker-compose.tools.yaml',
    ]
    assert _made_variable('DEV_PROJECT') == MAKEFILE_DEV_PROJECT
    dev_words = shlex.split(_make_variable('DEV_COMPOSE'))
    assert [word for word in dev_words if word.partition('=')[0] in PROJECT_OPTIONS] == ['-p'], dev_words
    assert dev_words[dev_words.index('-p') + 1] == '$(DEV_PROJECT)', dev_words
    words = shlex.split(tools_set)
    assert not [word for word in words if word.partition('=')[0] in PROJECT_OPTIONS], ('TOOLS_COMPOSE', words)
    for line in _make_recipe('dev-up'):
        words = shlex.split(line)
        assert not [word for word in words if word.partition('=')[0] in PROJECT_OPTIONS], line
    # THE MCP AND MAKE NAME ONE PROJECT (tj-iee6sn.31): the MCP's label-only down and make dev-up's
    # down address the same project, read here from the Makefile as make itself defines it, so the
    # two can never drift apart again without this line going red.
    assert stack.DEV_PROJECT == EXPECTED_DEV_PROJECT == MAKEFILE_DEV_PROJECT == _made_variable('DEV_PROJECT')


def test_the_dev_up_down_sets_pgadmin_placeholders_and_the_up_sets_none():
    """dev-down's pattern: 'unused' satisfies pgAdmin's ':?' guards on a down that creates nothing.

    The up must never carry them: shell variables outrank the env file in compose interpolation.
    """
    down, up = (shlex.split(line) for line in _make_recipe('dev-up'))
    assert [word for word in down if '=' in word] == ['PGADMIN_EMAIL=unused', 'PGADMIN_PASS=unused'], down
    assert not [word for word in up if word.startswith('PGADMIN')], up


def _expanded_words(line: str) -> list[str]:
    """A recipe line's words with $(TOOLS_COMPOSE) and $(DEV_COMPOSE) replaced by their definitions."""
    words = []
    for word in shlex.split(line):
        if word in ('$(TOOLS_COMPOSE)', '$(DEV_COMPOSE)'):
            words += _expanded_words(_make_variable(word[2:-1]))
        else:
            words.append(word)
    return words


@pytest.mark.parametrize('line', range(len(EXPECTED_DEV_UP_RECIPE)), ids=['down', 'up'])
def test_no_dev_up_line_carries_a_destructive_option(line: int):
    """D2 and addendum 23 (2): no -v/--volumes/--rmi/--remove-orphans in either line, in any spelling.

    Scanned word by word over the recipe line AND over the compose sets it names, as they are
    written, so a destructive option added to a variable rather than the recipe is red too. Attached
    forms (--volumes=true, --rmi=all) are matched on the option name.
    """
    words = _expanded_words(_make_recipe('dev-up')[line])
    assert 'docker' in words and 'compose' in words, words
    offenders = [word for word in words if word.partition('=')[0] in DESTRUCTIVE_WORDS]
    assert not offenders, f'make dev-up line {line} carries {offenders}: {words}'


def test_dev_up_and_dev_logs_are_phony():
    """dev-up's devnet prerequisite is pinned with the other dev-side targets (test_network_model)."""
    text = MAKEFILE.read_text(encoding='utf-8')
    for target in ('dev-up', 'dev-logs'):
        assert f'.PHONY: {target}\n' in text, f'{target} is not declared .PHONY'


def test_dev_logs_follows_the_three_app_services():
    assert _make_recipe('dev-logs') == EXPECTED_DEV_LOGS_RECIPE, _make_recipe('dev-logs')


def test_dev_launch_stays_attached():
    """Ruling D1 (b): dev-up is the detached spelling; dev-launch is unchanged and stays in the foreground."""
    recipe = _make_recipe('dev-launch')
    assert recipe == ['$(DEV_COMPOSE) up data_store data_ingest web'], recipe


# --- the MCP image bakes what the dev verbs name ----------------------------------------------------


def test_the_image_bakes_every_dev_file_at_the_path_the_dev_prefix_names():
    """Every DEV_COMPOSE_FILES entry lands in TRUSTED_COMPOSE_DIR under its own name; the prefix names it there."""
    baked = _baked_into_trusted()
    for name in stack.DEV_COMPOSE_FILES:
        assert baked.get(name) == f'{EXPECTED_TRUSTED_DIR}/{name}', (name, baked.get(name))


def test_the_image_bakes_both_dev_scripts_where_the_stack_constants_point():
    """tools/ beside the trusted Dockerfile, the repository's own layout (source_digest.sh reads ../Dockerfile)."""
    baked = _baked_into_trusted()
    assert str(stack.TRUSTED_DEV_INSTANCE_SECRET) == f'{EXPECTED_TRUSTED_DIR}/tools/dev_instance_secret.sh'
    assert str(stack.TRUSTED_SOURCE_DIGEST) == f'{EXPECTED_TRUSTED_DIR}/tools/source_digest.sh'
    assert baked.get('tools/dev_instance_secret.sh') == str(stack.TRUSTED_DEV_INSTANCE_SECRET), baked
    assert baked.get('tools/source_digest.sh') == str(stack.TRUSTED_SOURCE_DIGEST), baked
    assert baked.get('Dockerfile') == f'{EXPECTED_TRUSTED_DIR}/Dockerfile', baked


def test_the_image_bakes_the_proto_digest_where_the_guard_runs_it():
    """tj-iee6sn.33: the generated-code guard runs the BAKED proto_digest.sh, beside the other two scripts."""
    baked = _baked_into_trusted()
    assert str(stack.TRUSTED_PROTO_DIGEST) == f'{EXPECTED_TRUSTED_DIR}/tools/proto_digest.sh'
    assert baked.get('tools/proto_digest.sh') == str(stack.TRUSTED_PROTO_DIGEST), baked


@pytest.mark.parametrize('source', sorted(EXPECTED_DEV_BAKES))
def test_the_mcp_build_context_admits_each_dev_bake(source: str):
    """Dockerfile.dockerignore is an allow-list: a COPY source it does not re-include fails the image build."""
    rules = _dockerignore_rules(MCP_DOCKERIGNORE.read_text(encoding='utf-8'))
    assert not _is_excluded_from_context(source, rules), f'{source} is kept out of the MCP build context'
    exceptions = {pattern for exception, pattern in _dockerignore_patterns(MCP_DOCKERIGNORE.read_text()) if exception}
    assert source in exceptions, f'{source} is not re-included by name: {sorted(exceptions)}'


def test_the_dev_re_includes_keep_pruning_on():
    """No '!' line carries a wildcard (tj-c4mosr.11): one would make BuildKit open the data directory."""
    assert _pruning_offenders(_dockerignore_patterns(MCP_DOCKERIGNORE.read_text(encoding='utf-8'))) == []


# --- dev_file_prefix ----------------------------------------------------------------------------------


HOST_PATH = Path('/home/someone/repos/trader_joe')


def test_the_file_prefix_takes_the_host_path_and_nothing_else():
    """tj-iee6sn.6 (architect, 05:04): an argv builder takes only what reaches the argv.

    The unused stack_dir was dropped: an ignored path parameter on a security-relevant builder invites
    a caller to believe it matters. A parameter added back, used or not, is red here.
    """
    assert list(inspect.signature(stack.dev_file_prefix).parameters) == ['repo_host_path']


def test_the_file_prefix_is_exactly_the_project_the_host_path_and_the_five_trusted_files():
    prefix = stack.dev_file_prefix(HOST_PATH)
    expected = [EXPECTED_DOCKER, 'compose', '-p', EXPECTED_DEV_PROJECT, '--project-directory', str(HOST_PATH)]
    for name in EXPECTED_DEV_COMPOSE_FILES:
        expected += ['-f', f'{EXPECTED_TRUSTED_DIR}/{name}']
    assert prefix == expected, prefix


def test_the_file_prefix_reads_no_env_file_and_names_no_file_outside_the_trusted_directory():
    """Addendum 23 (3): compose reads the root env file itself; the MCP passes none. Every -f is a trusted copy."""
    prefix = stack.dev_file_prefix(HOST_PATH)
    assert not [word for word in prefix if word.partition('=')[0] in ('--env-file', '--profile')], prefix
    files = [value for flag, value in pairwise(prefix) if flag in ('-f', '--file')]
    assert files and all(Path(value).parent == stack.TRUSTED_COMPOSE_DIR for value in files), files
    assert prefix.count('-f') == len(EXPECTED_DEV_COMPOSE_FILES), prefix
    assert prefix[: len(stack.dev_compose_prefix())] == stack.dev_compose_prefix()


def test_the_file_prefix_names_the_host_path_verbatim():
    """The daemon resolves bind sources against it on the HOST, so it is passed as given, not resolved here."""
    other = Path('/srv/checkouts/trader_joe')
    prefix = stack.dev_file_prefix(other)
    assert prefix[prefix.index('--project-directory') + 1] == str(other), prefix
