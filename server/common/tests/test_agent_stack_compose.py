"""build_infra pins for the agent stack: the overlay, the merged model, the base file's defaults, who loads it.

tj-c4mosr.5: the body's build_infra bullets 1-4, the validator's 00:55 gaps (1) and (3), the architect's
(A)-(D) and addendum-2 pins (1)/(1a), (2)/(2a)/(2b) and (4)'s companion. Design: ADR tj-4rr0la
section 1 and 5(b), addenda 1-3 and 5 (O1, O2).

A separate module from test_ci_invariants.py and test_network_model.py so the agent stack's pins read
as one design. The merged model is common/tests/compose_model.py's: base, test client, the overlay,
then the fake-mode overlay LAST (tj-vhboky.61), as AGENT_STACK_COMPOSE loads them -- a property of the
merged model cannot be dodged by an edit to one file the others override.
"""

import re
import shlex
import subprocess

import pytest

from common.tests.compose_model import (
    AGENT_MCP_FILE,
    AGENT_STACK_FILE,
    BASE_FILE,
    FAKE_FILE,
    TEST_CLIENT_FILE,
    InterpolationRefused,
    NotModelled,
    agent_stack_model,
    client_model,
    interpolate,
    interpolate_tree,
    load,
    merge,
    prod_model,
    service_networks,
    volume,
)
from common.tests.test_ci_invariants import (
    MAKEFILE,
    MIGRATIONS_SCRIPT,
    REPO_ROOT,
    _compose_calls,
    _expanded_make_variable,
    _load_yaml,
    _make_variable,
    _run_lines,
    _subprocess_env,
    _workflow_files,
)
from tools.agent_mcp import stack


pytestmark = pytest.mark.build_infra

AGENT_STACK_PROJECT = 'trader_joe_agent_stack'
# The compose sets that must never load the agent-stack overlay (or the MCP's own file).
OTHER_COMPOSE_VARIABLES = ('PROD_COMPOSE', 'DEV_COMPOSE', 'TOOLS_COMPOSE', 'TEST_CLIENT_COMPOSE', 'AGENT_COMPOSE')
ENV_FILE_VARIABLES = {'ROOT_ENV_FILE': 'root', 'STORE_ENV_FILE': 'store', 'INGEST_ENV_FILE': 'ingest'}
# The six values the overlay reads, every one through ':?' with no default (ADR addendum 2).
OVERLAY_VARIABLES = frozenset({'DATABASE_NAME', 'STORE_API_NETWORK', 'DATA_DIR', *ENV_FILE_VARIABLES})
# Today's env_file lists with the three variables unset, in order: root first, then the service
# file -- the order decides which value wins in the container (ADR F1). Spelled out, not derived.
PROD_ENV_FILES = {
    'postgres': ['.env', './data/store/.env'],
    'data_store': ['.env', './data/store/.env'],
    'data_ingest': ['.env', './data/ingest/.env'],
}
ENV_DEFAULTS = (
    REPO_ROOT / '.env.default',
    REPO_ROOT / 'data' / 'store' / '.env.default',
    REPO_ROOT / 'data' / 'ingest' / '.env.default',
)
# A sample of what the MCP's generated root env supplies, for rendering the overlay in-process.
AGENT_ENV = {
    'DATABASE_NAME': 'trader_joe_agent_stack_postgres',
    'STORE_API_NETWORK': 'trader_joe_agent_stack_store_api',
    'DATA_DIR': '/stack/data',
    'ROOT_ENV_FILE': '/stack/agent_stack.env',
    'STORE_ENV_FILE': '/stack/agent_stack_store.env',
    'INGEST_ENV_FILE': '/stack/agent_stack_ingest.env',
}


def _overlay_text_without_comments() -> str:
    return '\n'.join(
        line.split(' #')[0]
        for line in AGENT_STACK_FILE.read_text(encoding='utf-8').splitlines()
        if not line.lstrip().startswith('#')
    )


# --- who loads the overlay --------------------------------------------------------------------


@pytest.mark.parametrize('variable', OTHER_COMPOSE_VARIABLES)
def test_no_other_compose_set_loads_the_agent_stack_or_mcp_files(variable: str):
    """Body bullets 1 and 8, 00:55 (3): PROD/DEV/TOOLS/TEST_CLIENT/AGENT sets never load either file."""
    expanded = _expanded_make_variable(variable, REPO_ROOT, _subprocess_env())
    for path in (AGENT_STACK_FILE, AGENT_MCP_FILE):
        assert path.name not in expanded, f'{variable} ({expanded}) loads {path.name} (ADR tj-4rr0la 5(b))'


def test_run_migrations_never_names_the_agent_files():
    text = MIGRATIONS_SCRIPT.read_text(encoding='utf-8')
    assert AGENT_STACK_FILE.name not in text and AGENT_MCP_FILE.name not in text


def test_the_agent_stack_set_is_its_project_then_base_client_overlay_then_fake_last():
    """00:55 (3), addendum 3 (3), tj-vhboky.61: -p trader_joe_agent_stack; base, client, overlay, fake LAST.

    The fake-mode overlay goes after the agent-stack overlay, so the agent stack's data_ingest always
    runs FakeRead and nothing the agent-stack overlay sets is overridden by an earlier file.
    """
    expanded = _expanded_make_variable('AGENT_STACK_COMPOSE', REPO_ROOT, _subprocess_env())
    calls = _compose_calls(expanded)
    assert len(calls) == 1 and calls[0][1] == [], expanded
    assert calls[0][0] == [BASE_FILE.name, TEST_CLIENT_FILE.name, AGENT_STACK_FILE.name, FAKE_FILE.name], expanded
    words = shlex.split(expanded)
    assert words[words.index('-p') + 1] == AGENT_STACK_PROJECT and words.count('-p') == 1
    assert 'docker-compose.override.yaml' not in expanded and 'docker-compose.tools.yaml' not in expanded


def _makefile_recipe_lines() -> list[str]:
    text = MAKEFILE.read_text(encoding='utf-8').replace('\\\n', ' ')
    return [line.strip() for line in text.splitlines() if line.startswith('\t')]


def test_no_make_target_loads_the_agent_stack_set():
    """The MCP server is AGENT_STACK_COMPOSE's only reader: no recipe starts the agent stack."""
    using = [
        line for line in _makefile_recipe_lines() if '$(AGENT_STACK_COMPOSE)' in line or AGENT_STACK_FILE.name in line
    ]
    assert not using, f'Makefile recipes load the agent stack: {using}'


def _workflow_run_lines() -> list[tuple[str, str, str]]:
    found = []
    for path in _workflow_files():
        for job in ((_load_yaml(path) or {}).get('jobs') or {}).values():
            for step in (job or {}).get('steps') or []:
                for line in _run_lines(step.get('run') or ''):
                    found.append((path.name, str(step.get('name')), line))
    return found


def _compose_project(line: str) -> list[str | None]:
    """The -p / --project-name of each docker compose call on a line (None when it sets none)."""
    projects = []
    for match in re.finditer(r'\bdocker(?:\s+compose|-compose)(?=\s|$)', line):
        words = shlex.split(line[match.end() :])
        project = None
        for index, word in enumerate(words):
            if not word.startswith('-'):
                break
            if word in ('-p', '--project-name') and index + 1 < len(words):
                project = words[index + 1]
            elif word.startswith('--project-name='):
                project = word.partition('=')[2]
        projects.append(project)
    return projects


def test_no_workflow_starts_the_agent_stack_it_only_renders_it():
    """Body bullet 1 as amended by (F): the one workflow line naming the overlay is Check Compose Renders' `config --quiet`."""
    naming = [(name, step, line) for name, step, line in _workflow_run_lines() if AGENT_STACK_FILE.name in line]
    assert len(naming) == 1, f'workflow lines naming {AGENT_STACK_FILE.name}: {naming}'
    _, step, line = naming[0]
    calls = _compose_calls(line)
    assert step == 'Check Compose Renders' and len(calls) == 1, naming
    assert calls[0][1][:1] == ['config'] and {'--quiet', '-q'} & set(calls[0][1]), line
    assert _compose_project(line) == [AGENT_STACK_PROJECT], line


# --- the project name ------------------------------------------------------------------------


def _normalised_project(name: str) -> str:
    """Compose's default project name for a directory: lower-cased, [a-z0-9_-] only."""
    return re.sub(r'[^a-z0-9_-]', '', name.lower())


_PROJECT_SETTING = re.compile(
    rf'(?:(?:\s-p|--project-name)[\s=]+["\']?(?:{AGENT_STACK_PROJECT}|\$\(AGENT_STACK_PROJECT\))(?![\w])'
    rf'|COMPOSE_PROJECT_NAME\s*[=:]\s*["\']?{AGENT_STACK_PROJECT}(?![\w])'
    rf'|^name:\s*["\']?{AGENT_STACK_PROJECT}\s*$)',
    re.MULTILINE,
)


def test_the_agent_stack_project_is_not_the_default_and_is_set_only_by_the_makefile_and_the_server():
    """Body bullet 2: never the default project (the checkout's directory name) that prod and dev use.

    And the only places that put anything under that project: the Makefile's AGENT_STACK_COMPOSE,
    the server (tools/agent_mcp) and CI's render of that same set.
    """
    assert _make_variable('AGENT_STACK_PROJECT') == stack.PROJECT == AGENT_STACK_PROJECT
    defaults = {_normalised_project(REPO_ROOT.name), 'trader_joe', 'workspace'}
    assert AGENT_STACK_PROJECT not in defaults
    for path in (BASE_FILE, TEST_CLIENT_FILE, AGENT_STACK_FILE):
        assert 'name' not in load(path), f'{path.name} sets a top-level project name'
    for path in ENV_DEFAULTS:
        assert 'COMPOSE_PROJECT_NAME' not in path.read_text(encoding='utf-8'), path
    listing = subprocess.run(['git', 'ls-files'], cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    tracked = [REPO_ROOT / line for line in listing.stdout.splitlines()]
    setting = []
    for path in tracked:
        relative = path.relative_to(REPO_ROOT).as_posix()
        if '/tests/' in f'/{relative}' or not path.is_file() or path.suffix in ('.lock', '.json', '.jpg', '.png'):
            continue
        try:
            text = path.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            continue
        for match in _PROJECT_SETTING.finditer(text):
            line = text[text.rfind('\n', 0, match.start()) + 1 : text.find('\n', match.end())].strip()
            setting.append((relative, line))
    allowed = [
        (relative, line)
        for relative, line in setting
        if relative.startswith('tools/agent_mcp/')
        or (relative == 'Makefile' and line.startswith('AGENT_STACK_COMPOSE :='))
        or (
            relative == '.github/workflows/trader_joe_testing.yml'
            and line.startswith(f'docker compose -p {AGENT_STACK_PROJECT} -f')
        )
    ]
    assert setting and sorted(setting) == sorted(allowed), (
        f'the agent stack project is set outside the Makefile and the server: {sorted(set(setting) - set(allowed))}'
    )


# --- the base file's defaults: prod renders as it always did -----------------------------------


def test_the_base_env_files_resolve_to_todays_lists_with_the_variables_unset():
    """Addendum-2 pins (1) and (1a): same services, same entries, SAME ORDER, plain short-form strings."""
    services = load(BASE_FILE)['services']
    actual = {}
    for name, spec in services.items():
        entries = spec.get('env_file')
        if entries is None:
            continue
        assert isinstance(entries, list) and all(isinstance(entry, str) for entry in entries), (
            f'{name}: env_file must stay short form, plain strings (a missing prod env file still stops compose)'
        )
        actual[name] = [interpolate(entry, {}) for entry in entries]
    assert actual == PROD_ENV_FILES, f'the base env_file lists drifted from what prod has always loaded: {actual}'


def test_each_base_env_file_entry_reads_its_own_variable_with_todays_path_as_default():
    services = load(BASE_FILE)['services']
    service_variable = {'postgres': 'STORE_ENV_FILE', 'data_store': 'STORE_ENV_FILE', 'data_ingest': 'INGEST_ENV_FILE'}
    for name, expected in PROD_ENV_FILES.items():
        spelled = services[name]['env_file']
        assert spelled == [f'${{ROOT_ENV_FILE:-{expected[0]}}}', f'${{{service_variable[name]}:-{expected[1]}}}'], (
            name,
            spelled,
        )


def test_no_committed_env_default_sets_a_variable_the_overlay_uses_as_a_sentinel():
    """(D): the overlay header's sentinel claim depends on no committed default setting these."""
    for path in ENV_DEFAULTS:
        keys = {
            line.split('=', 1)[0].strip()
            for line in path.read_text(encoding='utf-8').splitlines()
            if '=' in line and not line.lstrip().startswith('#')
        }
        leaked = keys & {'STORE_API_NETWORK', *ENV_FILE_VARIABLES}
        assert not leaked, f'{path.relative_to(REPO_ROOT)} sets {leaked}'


# --- the overlay's guards -------------------------------------------------------------------------


def test_every_overlay_interpolation_is_colon_question_with_no_default():
    """00:55 (3), (2) and (2a): every value the overlay reads is ${VAR:?...}, the COLON form, never a default."""
    text = _overlay_text_without_comments()
    expressions = re.findall(r'\$\{([^}]*)\}', text)
    assert expressions, 'the overlay interpolates nothing, so this pin would guard nothing'
    wrong = [expression for expression in expressions if not re.fullmatch(r'[A-Za-z_]\w*:\?.+', expression)]
    assert not wrong, (
        f'overlay interpolations without the ":?" guard (a default here could only be the user\'s): {wrong}'
    )
    assert {expression.split(':?')[0] for expression in expressions} == OVERLAY_VARIABLES
    assert not re.search(r'\$[A-Za-z_]', text), 'a bare $VAR in the overlay has no guard'


@pytest.mark.parametrize('variable', sorted(OVERLAY_VARIABLES))
@pytest.mark.parametrize('state', ['unset', 'empty'])
def test_the_overlay_refuses_to_render_with_any_value_unset_or_empty(variable: str, state: str):
    """(2)/(2a): a generated 'ROOT_ENV_FILE=' line must stop compose as surely as a missing one."""
    env = dict(AGENT_ENV)
    if state == 'unset':
        del env[variable]
    else:
        env[variable] = ''
    with pytest.raises(InterpolationRefused) as refused:
        interpolate_tree(load(AGENT_STACK_FILE), env)
    assert refused.value.variable == variable
    interpolate_tree(load(AGENT_STACK_FILE), AGENT_ENV)


def test_the_overlay_names_store_api_only_through_its_guard():
    """00:55 (1): the overlay's store_api name is ':?'-guarded with no default."""
    name = load(AGENT_STACK_FILE)['networks']['store_api']['name']
    assert re.fullmatch(r'\$\{STORE_API_NETWORK:\?[^}]+\}', name), name
    assert interpolate(name, AGENT_ENV) != interpolate(load(BASE_FILE)['networks']['store_api']['name'], {})


def test_each_service_labels_exactly_the_env_files_its_base_entries_read():
    """(2b): label key <kind>_env_file interpolates <KIND>_ENV_FILE; label set == the base service's env-file set."""
    base = load(BASE_FILE)['services']
    overlay = load(AGENT_STACK_FILE)['services']
    for name, spec in base.items():
        variables = set()
        for entry in spec.get('env_file') or []:
            variables |= set(re.findall(r'\$\{(\w+)', entry))
        expected = {
            f'trader_joe.agent_stack.{ENV_FILE_VARIABLES[variable]}_env_file': variable for variable in variables
        }
        labels = (overlay.get(name) or {}).get('labels') or {}
        actual = {
            key: re.findall(r'\$\{(\w+)', value)
            for key, value in labels.items()
            if key.startswith('trader_joe.agent_stack.')
        }
        assert actual == {key: [variable] for key, variable in expected.items()}, (
            f'{name}: labels {labels} must guard exactly the env files it loads, {sorted(variables)}'
        )


# --- the merged agent model: separate, no reach, no egress ----------------------------------------


def test_the_merged_agent_model_publishes_nothing_and_joins_no_devnet():
    """Body bullet 3: no port, no devnet, no external network."""
    model = agent_stack_model()
    assert not [name for name, spec in model['services'].items() if spec.get('ports')], (
        'the agent stack publishes a port'
    )
    for name, spec in model['services'].items():
        assert 'devnet' not in service_networks(spec), name
    for name, network in model['networks'].items():
        assert not (network or {}).get('external'), f'{name} is external: the agent stack would join another project'
        assert 'devnet' not in str((network or {}).get('name', '')), name


def test_every_network_data_ingest_joins_in_the_agent_model_is_internal():
    """(A), addendum 3 (1): the PROPERTY on the merged model -- no route out for data_ingest."""
    model = agent_stack_model()
    joined = service_networks(model['services']['data_ingest'])
    assert joined and 'default' not in joined
    open_networks = [name for name in joined if not (model['networks'].get(name) or {}).get('internal')]
    assert not open_networks, f'data_ingest joins {open_networks}, which are not internal, in the agent stack'


def test_every_built_image_is_tagged_apart_from_prod_and_the_client_set():
    """(B), addendum 3 (2): a build from a worktree must never retag an image prod-launch starts."""
    agent, prod, client = agent_stack_model()['services'], prod_model()['services'], client_model()['services']
    built = [name for name, spec in agent.items() if spec.get('build')]
    assert set(built) == {'data_store', 'data_ingest', 'test_client'}
    for name in built:
        tag = agent[name].get('image')
        assert tag, f'{name} has no image tag in the agent model, so compose derives one'
        for other in (prod, client):
            if name in other:
                assert tag != other[name].get('image'), f"{name} is tagged {tag} in the agent stack AND the user's set"


def test_the_overlay_covers_every_container_name_and_every_data_dir_mount():
    """(C): container_name overridden wherever the base sets one; every DATA_DIR mount replaced by TARGET."""
    base, overlay = load(BASE_FILE)['services'], load(AGENT_STACK_FILE)['services']
    for name, spec in base.items():
        if 'container_name' in spec:
            assert 'container_name' in (overlay.get(name) or {}), (
                f'{name}: the base sets container_name, the overlay does not'
            )
        for entry in spec.get('volumes') or []:
            mount = volume(entry)
            if mount['source'].startswith('${DATA_DIR'):
                targets = [volume(other)['target'] for other in (overlay.get(name) or {}).get('volumes') or []]
                assert mount['target'] in targets, (
                    f'{name}: the base mounts DATA_DIR at {mount["target"]!r}; the overlay must use the SAME target '
                    f"(it has {targets}) or compose ADDS a mount of the user's data instead of replacing it"
                )


def test_no_service_built_from_agent_source_has_a_writable_bind():
    """O1, addendum 5: the only writable bind is postgres's DATA_DIR one."""
    writable = {}
    for name, spec in agent_stack_model()['services'].items():
        for entry in spec.get('volumes') or []:
            mount = volume(entry)
            if mount['type'] == 'bind' and not mount['read_only']:
                writable[(name, mount['target'])] = mount['source']
    assert set(writable) == {('postgres', '/var/lib/postgresql/data')}, f'writable binds in the agent stack: {writable}'
    assert all(source.startswith('${DATA_DIR:?') for source in writable.values()), writable


def test_every_built_service_uses_the_trusted_dockerfile():
    """O2, addendum 5 ruling 2: build.dockerfile is the MCP image's own copy, never the snapshot's."""
    for name, spec in agent_stack_model()['services'].items():
        if spec.get('build'):
            assert (
                spec['build'].get('dockerfile') == str(stack.TRUSTED_DOCKERFILE) == '/opt/agent_mcp/compose/Dockerfile'
            ), (name, spec['build'])


def test_the_overlay_parses_as_plain_yaml_and_adds_no_service():
    """No !reset / !override (the invariants read compose files with yaml.safe_load); nothing new is started."""
    overlay = load(AGENT_STACK_FILE)
    base_and_client = set(load(BASE_FILE)['services']) | set(load(TEST_CLIENT_FILE)['services'])
    assert set(overlay['services']) <= base_and_client


# --- the model refuses what it does not model (architect gate R2 on 6b4ef02) ------------------------


@pytest.mark.parametrize(
    'document',
    [
        {'include': ['other.yaml'], 'services': {}},
        {'services': {'data_ingest': {'extends': {'service': 'data_store'}}}},
        {'services': {'data_ingest': {'profiles': ['x']}}},
    ],
    ids=['include', 'extends', 'profiles'],
)
def test_merge_refuses_include_extends_and_profiles(document: dict):
    """Each changes what compose renders; copied through, the model would disagree with compose silently."""
    with pytest.raises(NotModelled):
        merge([load(BASE_FILE), document])


def test_every_merged_model_is_built_from_files_the_model_can_read():
    """The three models the pins read load without refusal: no committed file uses include, extends or profiles."""
    for model in (prod_model, client_model, agent_stack_model):
        assert model()['services'], model.__name__
