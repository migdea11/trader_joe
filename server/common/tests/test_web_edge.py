"""build_infra pins for the web container: the Caddyfile, the Dockerfile's web stages, the compose overlay.

Bead tj-grna9p.26; decisions tj-grna9p.5, .6 and .7 as ruled. The ruled shape, each line of which is a
silent failure if lost:

* THE PROXY (tj-grna9p.5 addendum, .26 phase-1 note). One reverse_proxy, to data_store only, for the
  UI's read routes: /api/store/ui/v1/*, GET and HEAD, the /api/store prefix stripped. Nothing reaches
  data_ingest. Every other /api path is a 404 answered by Caddy, never the SPA's index.html.
* THE SECRET (tj-grna9p.6). The proxy sets X-Instance-Secret from the container environment; the
  browser never holds it, so the Caddyfile carries a placeholder and never a value, and the access log
  drops the header.
* THE EDGE. Admin API off, automatic HTTPS off, the security headers set.
* THE IMAGE. The Node pin in web_build_image mirrors the Makefile (as the devcontainer's does,
  test_agent_image.py), and is executed here with every command stubbed. prod_image stays the LAST
  stage. No COPY reads web/ or deploy/ from the context (the service digests and reach scans derive
  their roots from the COPY list); the sources arrive as named contexts. web_image is FROM the pinned
  caddy digest and runs as a non-root user.
* THE OVERLAY. Its own file: the base docker-compose.yaml never holds the web service, and the agent
  stack never loads the overlay. The make compose sets DO load it (owner ruling 2026-10-06: the web
  service is built and started by the existing make targets), so that is deliberately not pinned
  against. No env_file (the root .env holds the database and broker credentials), store_api plus its
  own bridge and nothing that reaches data_ingest, one loopback port, the secret REQUIRED (:?),
  read-only and no-new-privileges, and data_store healthy first.
* THE PrimeUI LICENCE KEY (d02c9a3, tj-cwerrn). BUILD TIME only: an empty placeholder in .env.default,
  a compose build arg, an ARG and ENV in web_build_image before `npm run build`. Never in the web
  container's runtime environment, and still no env_file.

Not provable here, and NOT RUN: the image build, `docker compose config` and a container smoke test
(the agent container has no Docker), and Caddy's own parse of the file (no caddy binary here; the
builder validated it with caddy 2.11.7 outside the repository). In particular the ORDER in which Caddy
evaluates the `handle` blocks is Caddy's, not this file's, and is not pinned here.
"""

import json
import os
import posixpath
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from common.tests import compose_model
from common.tests.roots import REPO_ROOT
from common.tests.test_agent_image import ROOT_USERS, _make_pin, _run_stubbed
from common.tests.test_ci_invariants import (
    MAKEFILE,
    RENDER_STEP,
    _compose_calls,
    _dockerfile_stages,
    _env_file_values,
    _image_build_job,
    _make_recipe,
    _make_variable,
    _step_lines,
)
from common.tests.test_grpc_bind_network import _COMPOSE_SET, _set_files
from common.tests.test_network_model import _make_prerequisites
from tools.agent_mcp import stack


pytestmark = pytest.mark.build_infra

CADDYFILE = REPO_ROOT / 'deploy' / 'web' / 'Caddyfile'
DOCKERFILE = REPO_ROOT / 'Dockerfile'
WEB_COMPOSE = REPO_ROOT / 'docker-compose.web.yaml'
ENV_DEFAULT = REPO_ROOT / '.env.default'
STORE_UI_PATH = '/api/store/ui/v1/*'
SECRET_HEADER = 'X-Instance-Secret'
SECRET_PLACEHOLDER = '{$INSTANCE_WRITE_SECRET}'
UPSTREAM_PLACEHOLDER = '{$DATA_STORE_UPSTREAM}'


# ---------------------------------------------------------------------------------------------------
# THE CADDYFILE, read as blocks
#
# Enough of a parse for these pins, not a Caddyfile parser: comment lines dropped, a line ending in
# '{' opens a block whose words are the rest of the line, a lone '}' closes it, any other line is a
# leaf. Words are split as a shell would, so a quoted value is one word with its quotes removed.


@dataclass
class Block:
    words: list[str]
    children: list['Block'] = field(default_factory=list)

    def named(self, *words: str) -> list['Block']:
        """The direct children whose words START with WORDS."""
        return [child for child in self.children if child.words[: len(words)] == list(words)]

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


def _caddy_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith('#')]


def _parse_caddyfile(text: str) -> Block:
    root = Block(['<root>'])
    stack_ = [root]
    for line in _caddy_lines(text):
        if line == '}':
            assert len(stack_) > 1, 'an unmatched } in the Caddyfile'
            stack_.pop()
        elif line.endswith('{'):
            block = Block(shlex.split(line[:-1]))
            stack_[-1].children.append(block)
            stack_.append(block)
        else:
            stack_[-1].children.append(Block(shlex.split(line)))
    assert len(stack_) == 1, 'the Caddyfile ends inside an open block'
    return root


@pytest.fixture(scope='module')
def caddy() -> Block:
    return _parse_caddyfile(CADDYFILE.read_text(encoding='utf-8'))


@pytest.fixture(scope='module')
def site(caddy: Block) -> Block:
    sites = [block for block in caddy.children if block.words and block.words[0].startswith(':')]
    assert len(sites) == 1, f'expected one site block, found {[block.words for block in sites]}'
    return sites[0]


def _the_proxy(site: Block) -> tuple[Block, Block]:
    """(the handle block holding the reverse_proxy, the reverse_proxy block)."""
    found = [
        (handle, proxy)
        for handle in site.children
        if handle.words[:1] == ['handle']
        for proxy in handle.walk()
        if proxy.words[:1] == ['reverse_proxy']
    ]
    assert len(found) == 1, f'expected the one reverse_proxy inside a handle, found {len(found)}'
    return found[0]


def test_the_parse_reads_the_whole_file(caddy: Block):
    """Guard the guard: every non-comment line of the file is a node, so no pin below reads a fragment."""
    lines = _caddy_lines(CADDYFILE.read_text(encoding='utf-8'))
    nodes = sum(1 for _ in caddy.walk()) - 1
    closes = sum(1 for line in lines if line == '}')
    assert nodes == len(lines) - closes, (nodes, len(lines), closes)


def test_there_is_exactly_one_reverse_proxy_and_its_upstream_is_data_store_from_the_environment(caddy: Block):
    """The only upstream is DATA_STORE_UPSTREAM, which the overlay sets to data_store on store_api."""
    proxies = [block for block in caddy.walk() if block.words[:1] == ['reverse_proxy']]
    assert len(proxies) == 1, [block.words for block in proxies]
    assert proxies[0].words == ['reverse_proxy', UPSTREAM_PLACEHOLDER], proxies[0].words


def test_the_proxied_route_is_the_store_ui_read_routes_with_the_prefix_stripped(site: Block):
    """GET and HEAD on /api/store/ui/v1/* only; data_store sees /ui/v1/... (the prefix is the SPA's)."""
    handle, _ = _the_proxy(site)
    assert len(handle.words) == 2 and handle.words[1].startswith('@'), (
        f'the proxy handle is matched by {handle.words[1:]}, not one named matcher'
    )
    matchers = site.named(handle.words[1])
    assert len(matchers) == 1, f'{handle.words[1]} is defined {len(matchers)} times'
    conditions = sorted(child.words for child in matchers[0].children)
    assert conditions == [['method', 'GET', 'HEAD'], ['path', STORE_UI_PATH]], conditions
    assert handle.named('uri') == [Block(['uri', 'strip_prefix', '/api/store'])], [b.words for b in handle.children]


def test_the_secret_is_set_from_the_environment_and_never_written_down(caddy: Block, site: Block):
    """header_up sets it from INSTANCE_WRITE_SECRET; the header appears nowhere else but the log's delete."""
    _, proxy = _the_proxy(site)
    assert proxy.named('header_up', SECRET_HEADER) == [Block(['header_up', SECRET_HEADER, SECRET_PLACEHOLDER])]
    mentions = [block.words for block in caddy.walk() if any(SECRET_HEADER in word for word in block.words)]
    assert mentions == [
        [f'request>headers>{SECRET_HEADER}', 'delete'],
        ['header_up', SECRET_HEADER, SECRET_PLACEHOLDER],
    ], mentions


def test_the_access_log_deletes_the_secret_header(site: Block):
    """A client that sends the header itself must not get it written to the container log."""
    deletes = [
        block.words
        for log in site.named('log')
        for block in log.walk()
        if block.words == [f'request>headers>{SECRET_HEADER}', 'delete']
    ]
    assert len(deletes) == 1, [block.words for block in site.named('log')]


def test_nothing_in_the_caddyfile_names_data_ingest():
    """No route, upstream or matcher for data_ingest (tj-grna9p.5 addendum (3)). Comments may explain why."""
    code = '\n'.join(_caddy_lines(CADDYFILE.read_text(encoding='utf-8')))
    assert 'ingest' not in code.lower(), [line for line in code.splitlines() if 'ingest' in line.lower()]


def test_every_other_api_path_is_a_404_from_caddy(site: Block):
    """Not the SPA's index.html: a wrong method or another data_store route must not read as a page."""
    handles = site.named('handle', '/api/*')
    assert len(handles) == 1, [block.words for block in site.children]
    assert [child.words for child in handles[0].children] == [['respond', 'not found', '404']]


def test_the_admin_api_and_automatic_https_are_off(caddy: Block):
    """Global options: no config endpoint to reach, and no certificate machinery on a plain-HTTP listener."""
    globals_ = [block for block in caddy.children if block.words == []]
    assert len(globals_) == 1, 'no global options block'
    options = [child.words for child in globals_[0].children]
    assert ['admin', 'off'] in options and ['auto_https', 'off'] in options, options


@pytest.mark.parametrize(
    ('header', 'value'),
    [
        ('X-Content-Type-Options', 'nosniff'),
        ('X-Frame-Options', 'DENY'),
        ('Referrer-Policy', 'no-referrer'),
        ('Cross-Origin-Opener-Policy', 'same-origin'),
        ('Cross-Origin-Resource-Policy', 'same-origin'),
        ('Permissions-Policy', 'camera=(), microphone=(), geolocation=(), payment=(), usb=()'),
    ],
)
def test_the_security_headers_are_set(site: Block, header: str, value: str):
    (headers,) = site.named('header')
    assert headers.named(header) == [Block([header, value])], [child.words for child in headers.children]


def test_the_server_header_is_removed_and_no_hsts_is_sent_over_plain_http(site: Block):
    """-Server hides the software banner; Strict-Transport-Security means nothing on a plain-HTTP listener."""
    (headers,) = site.named('header')
    words = [child.words for child in headers.children]
    assert ['-Server'] in words, words
    assert not [w for w in words if w[0].lstrip('-+').lower() == 'strict-transport-security'], words


def test_the_content_security_policy_keeps_scripts_and_framing_locked(site: Block):
    """script-src 'self' with no unsafe-* (only style-src carries PrimeVue's inline style), no framing."""
    (headers,) = site.named('header')
    (csp,) = headers.named('Content-Security-Policy')
    directives = {part.split()[0]: part.split()[1:] for part in csp.words[1].split(';') if part.strip()}
    assert directives['default-src'] == ["'self'"], directives
    assert directives['script-src'] == ["'self'"], directives
    assert directives['frame-ancestors'] == ["'none'"], directives
    assert directives['object-src'] == ["'none'"], directives


# ---------------------------------------------------------------------------------------------------
# THE DOCKERFILE'S WEB STAGES


def _stage_instructions(stage: str) -> list[tuple[str, str]]:
    """One stage's (INSTRUCTION, arguments), continuations folded and comment lines dropped."""
    _, body = _dockerfile_stages()[stage]
    instructions = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        keyword, _, arguments = stripped.partition(' ')
        instructions.append((keyword.upper(), arguments.strip()))
    return instructions


def _all_instructions() -> list[tuple[str, str]]:
    folded = DOCKERFILE.read_text(encoding='utf-8').replace('\\\n', ' ')
    out = []
    for line in folded.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith('#'):
            keyword, _, arguments = stripped.partition(' ')
            out.append((keyword.upper(), arguments.strip()))
    return out


def _arg_defaults(stage: str) -> dict[str, str]:
    defaults = {}
    for keyword, arguments in _stage_instructions(stage):
        if keyword == 'ARG' and '=' in arguments:
            name, _, value = arguments.partition('=')
            defaults[name.strip()] = value.strip()
    return defaults


def test_prod_image_is_still_the_last_stage():
    """A target-less `docker build` makes the last stage; the web stages sit before the deploy stages."""
    stages = list(_dockerfile_stages())
    assert stages[-1] == 'prod_image', stages
    assert stages.index('web_image') < stages.index('base_deploy_image'), stages


@pytest.mark.parametrize('pin', ['NODE_VERSION', 'NODE_SHA256_X86_64', 'NODE_SHA256_AARCH64'])
def test_the_web_build_node_pin_equals_the_makefiles(pin: str):
    """The second mirror of the Makefile's Node pin (the devcontainer's is test_agent_image.py)."""
    assert _arg_defaults('web_build_image').get(pin) == _make_pin(pin), _arg_defaults('web_build_image')


def _node_run() -> str:
    found = [
        args for keyword, args in _stage_instructions('web_build_image') if keyword == 'RUN' and 'nodejs.org' in args
    ]
    assert len(found) == 1, f'expected one RUN that fetches node in web_build_image, found {len(found)}'
    return found[0]


WEB_NODE_STUBBED = ('dpkg', 'curl', 'sha256sum', 'tar', 'rm', 'node')


def _run_web_node(tmp_path: Path, arch: str, **variables: str):
    """The web_build_image node RUN under /bin/sh, its ARG defaults in the environment as docker passes them."""
    environment = _arg_defaults('web_build_image') | {'NODE_REPORTS': f'v{_make_pin("NODE_VERSION")}'} | variables
    return _run_stubbed(_node_run(), WEB_NODE_STUBBED, tmp_path, arch, **environment)


@pytest.mark.parametrize(
    ('arch', 'node_arch', 'checksum'),
    [('amd64', 'x64', 'NODE_SHA256_X86_64'), ('arm64', 'arm64', 'NODE_SHA256_AARCH64')],
)
def test_the_web_build_fetches_and_checks_the_makefiles_node_for_each_arch(
    tmp_path: Path, arch: str, node_arch: str, checksum: str
):
    version = _make_pin('NODE_VERSION')
    result, calls, stdin = _run_web_node(tmp_path, arch)
    assert result.returncode == 0, f'{result.stdout}\n{result.stderr}'
    assert [call[0] for call in calls] == ['dpkg', 'curl', 'sha256sum', 'tar', 'rm', 'node'], calls
    curl = calls[1][1:]
    assert curl[-1] == f'https://nodejs.org/dist/v{version}/node-v{version}-linux-{node_arch}.tar.gz', curl
    downloaded = curl[curl.index('-o') + 1]
    assert stdin == f'{_make_pin(checksum)}  {downloaded}\n', stdin


@pytest.mark.parametrize(
    ('arch', 'variables', 'reached'),
    [
        ('riscv64', {}, ['dpkg']),
        ('amd64', {'SHA256SUM_EXIT': '1'}, ['dpkg', 'curl', 'sha256sum']),
        ('amd64', {'NODE_REPORTS': 'v1.0.0'}, ['dpkg', 'curl', 'sha256sum', 'tar', 'rm', 'node']),
    ],
    ids=['an-unpinned-arch', 'a-checksum-mismatch', 'another-version'],
)
def test_the_web_build_fails_on_an_unpinned_arch_a_mismatch_or_another_version(
    tmp_path: Path, arch: str, variables: dict[str, str], reached: list[str]
):
    result, calls, _ = _run_web_node(tmp_path, arch, **variables)
    assert result.returncode != 0, f'{result.stdout}\n{result.stderr}'
    assert [call[0] for call in calls] == reached, calls


def _copy_sources(arguments: str) -> tuple[list[str], list[str]]:
    """(flags, sources) of one COPY: every word but the last that is not a --flag."""
    words = shlex.split(arguments)
    flags = [word for word in words if word.startswith('--')]
    operands = [word for word in words if not word.startswith('--')]
    return flags, operands[:-1]


def test_no_copy_reads_web_or_deploy_from_the_build_context():
    """source_digest.sh and the reach scans take their roots from the COPY list: web/ must never join it."""
    offending = []
    for keyword, arguments in _all_instructions():
        if keyword != 'COPY':
            continue
        flags, sources = _copy_sources(arguments)
        if any(flag.startswith('--from=') for flag in flags):
            continue
        for source in sources:
            top = source.removeprefix('./').split('/')[0]
            if top in ('web', 'deploy'):
                offending.append(arguments)
    assert not offending, offending


def test_the_web_stages_copy_only_from_stages_and_mount_only_the_named_contexts():
    """COPY --from names a stage; the sources come in through RUN --mount of named contexts compose declares.

    web_src and web_deploy at least; a further context (a generated tree, say) is allowed as long as
    the overlay declares it, since an undeclared one fails the build.
    """
    stages = set(_dockerfile_stages())
    froms = set()
    for stage in ('web_build_image', 'web_image'):
        for keyword, arguments in _stage_instructions(stage):
            if keyword == 'COPY':
                flags, _ = _copy_sources(arguments)
                for flag in flags:
                    if flag.startswith('--from='):
                        assert flag.removeprefix('--from=') in stages, arguments
            froms.update(re.findall(r'--mount=type=bind,from=([\w-]+)', arguments))
    contexts = compose_model.load(WEB_COMPOSE)['services']['web']['build']['additional_contexts']
    assert {'web_src', 'web_deploy'} <= froms <= set(contexts) | stages, (froms, contexts)
    assert (contexts['web_src'], contexts['web_deploy']) == ('./web', './deploy/web'), contexts


def test_web_image_is_from_the_pinned_caddy_digest_in_base_images():
    """Digest-pinned, and the same ref the agent stack pre-pulls (stack.BASE_IMAGES)."""
    parent, _ = _dockerfile_stages()['web_image']
    assert parent.startswith('caddy:') and '@sha256:' in parent, parent
    assert parent in stack.BASE_IMAGES, stack.BASE_IMAGES


def test_web_image_runs_as_a_non_root_user():
    """The last USER of the stage is not root, and nothing after it switches back."""
    users = [arguments for keyword, arguments in _stage_instructions('web_image') if keyword == 'USER']
    assert users, 'web_image never sets USER, so Caddy runs as root'
    assert users[-1].split(':')[0] not in ROOT_USERS, users


def test_the_web_build_runs_npm_as_a_non_root_user():
    """The npm ci and npm run build RUNs execute package code (scripts skipped, bundlers not): never as root."""
    user = None
    npm_users = []
    for keyword, arguments in _stage_instructions('web_build_image'):
        if keyword == 'USER':
            user = arguments.split(':')[0]
        elif keyword == 'RUN' and re.search(r'\bnpm (ci|run)\b', arguments):
            npm_users.append(user)
    assert len(npm_users) == 2, npm_users
    assert all(user is not None and user not in ROOT_USERS for user in npm_users), npm_users


def test_web_image_serves_the_built_dist_with_the_repository_caddyfile():
    instructions = _stage_instructions('web_image')
    assert ('COPY', '--from=web_build_image /web/dist /srv') in instructions, instructions
    runs = [arguments for keyword, arguments in instructions if keyword == 'RUN']
    assert any('from=web_deploy,source=Caddyfile' in run and '/etc/caddy/Caddyfile' in run for run in runs), runs


# ---------------------------------------------------------------------------------------------------
# THE COMPOSE OVERLAY


@pytest.fixture(scope='module')
def web() -> dict:
    document = compose_model.load(WEB_COMPOSE)
    assert set(document['services']) == {'web'}, set(document['services'])
    return document['services']['web']


def test_the_web_service_has_no_env_file(web: dict):
    """The root .env holds the database password and broker keys; the proxy needs one secret."""
    assert 'env_file' not in web, web.get('env_file')


def test_the_web_service_gets_exactly_the_upstream_and_the_secret(web: dict):
    assert set(web['environment']) == {'DATA_STORE_UPSTREAM', 'INSTANCE_WRITE_SECRET'}, web['environment']


@pytest.mark.parametrize('environment', [{}, {'INSTANCE_WRITE_SECRET': ''}], ids=['unset', 'empty'])
def test_the_secret_is_required(web: dict, environment: dict[str, str]):
    """`:?`: unset or empty refuses, so no proxy starts that forwards an empty secret."""
    with pytest.raises(compose_model.InterpolationRefused) as refused:
        compose_model.interpolate_tree(web['environment'], environment)
    assert refused.value.variable == 'INSTANCE_WRITE_SECRET'
    rendered = compose_model.interpolate_tree(web['environment'], {'INSTANCE_WRITE_SECRET': 's3cret'})
    assert rendered['INSTANCE_WRITE_SECRET'] == 's3cret'


def test_the_web_service_is_on_store_api_and_its_own_bridge_only(web: dict):
    assert sorted(compose_model.service_networks(web)) == ['store_api', 'web_edge'], web['networks']


def test_the_web_service_shares_no_network_with_data_ingest(web: dict):
    """The Caddyfile's "no data_ingest route" rests on this: no route could work if one were added."""
    base = compose_model.load(compose_model.BASE_FILE)
    ingest = set(compose_model.service_networks(base['services']['data_ingest']))
    assert ingest and not ingest & set(compose_model.service_networks(web)), ingest


def test_the_web_service_publishes_one_port_on_loopback(web: dict):
    rendered = compose_model.interpolate_tree(web['ports'], {})
    assert len(rendered) == 1, rendered
    host_ip, _, container_port = rendered[0].split(':')
    assert host_ip == '127.0.0.1' and container_port == '8080', rendered


def test_the_compose_default_port_is_the_env_default(web: dict):
    """WEB_PORT is set in .env.default, and the compose fallback agrees with it."""
    values = dict(
        line.split('=', 1)
        for line in ENV_DEFAULT.read_text(encoding='utf-8').splitlines()
        if line.startswith('WEB_PORT=')
    )
    assert values.get('WEB_PORT', '').isdigit(), values
    assert compose_model.interpolate_tree(web['ports'], {})[0].split(':')[1] == values['WEB_PORT']


def test_the_web_service_is_read_only_and_cannot_gain_privileges(web: dict):
    assert web.get('read_only') is True, web.get('read_only')
    assert 'no-new-privileges:true' in web.get('security_opt', []), web.get('security_opt')
    assert web.get('cap_drop') == ['ALL'], web.get('cap_drop')


def test_the_web_service_waits_for_a_healthy_data_store(web: dict):
    assert web['depends_on'] == {'data_store': {'condition': 'service_healthy'}}, web['depends_on']


def test_the_web_service_builds_the_web_image_stage(web: dict):
    assert web['build']['target'] == 'web_image' and web['build']['dockerfile'] == 'Dockerfile', web['build']


def test_the_web_edge_network_is_an_ordinary_bridge():
    """An internal network cannot publish a port; store_api, from the base file, stays internal."""
    networks = compose_model.load(WEB_COMPOSE)['networks']
    assert not (networks['web_edge'] or {}).get('internal'), networks
    assert compose_model.load(compose_model.BASE_FILE)['networks']['store_api']['internal'] is True


def test_the_base_file_has_no_web_service():
    """The web service lives in its overlay only; the make sets load that overlay by ruling (2026-10-06)."""
    assert 'web' not in compose_model.load(compose_model.BASE_FILE)['services']


def test_the_agent_stack_never_loads_the_web_overlay():
    """The agent stack's compose set, in the Makefile, in tools/agent_mcp and in the MCP image's copies."""
    agent_set = _make_variable('AGENT_STACK_COMPOSE')
    assert agent_set.startswith('docker compose') and 'docker-compose.web.yaml' not in agent_set, agent_set
    assert 'docker-compose.web.yaml' not in stack.COMPOSE_FILES, stack.COMPOSE_FILES
    mcp_dockerfile = (REPO_ROOT / 'tools' / 'agent_mcp' / 'Dockerfile').read_text(encoding='utf-8')
    assert 'docker-compose.web.yaml' not in mcp_dockerfile
    for name in stack.COMPOSE_FILES:
        assert 'web' not in (compose_model.load(REPO_ROOT / name).get('services') or {}), name


# ---------------------------------------------------------------------------------------------------
# WHAT THE WEB BUILD'S npm SCRIPTS NEED, AND WHETHER THE STAGE HAS IT
#
# The image build is NOT RUN here, so this follows the build command the way npm would: `npm run X`
# runs preX first unless --ignore-scripts, and a script that is `npm run Y` runs Y the same way. Any
# step that is `buf generate <dir>` needs buf in the stage and <dir>, resolved from the WORKDIR, put
# there by a mount or a COPY. Nothing generated is committed, so a build that does not generate
# needs the generated tree (gen/proto/ts, the @generated alias in web/vite.config.ts) provided instead.

PACKAGE_JSON = REPO_ROOT / 'web' / 'package.json'


def _npm_steps(script: str, scripts: dict[str, str], run_pre: bool) -> list[str]:
    """The shell commands `npm run SCRIPT` executes, in order, with nested `npm run` expanded."""
    steps = []
    for name in ([f'pre{script}'] if run_pre and f'pre{script}' in scripts else []) + [script]:
        body = scripts[name]
        nested = re.fullmatch(r'npm run (\S+)', body.strip())
        if nested:
            steps.extend(_npm_steps(nested.group(1), scripts, run_pre))
        else:
            steps.append(body)
    return steps


def _stage_provided_paths(stage: str) -> set[str]:
    """Absolute paths a stage puts in place by a RUN --mount target or a COPY destination."""
    provided = set()
    for keyword, arguments in _stage_instructions(stage):
        if keyword == 'RUN':
            provided.update(re.findall(r'--mount=[^\s]*target=([^,\s]+)', arguments))
        if keyword == 'COPY':
            provided.add(shlex.split(arguments)[-1])
    return {path.rstrip('/') for path in provided}


def _workdir(stage: str) -> str:
    dirs = [arguments for keyword, arguments in _stage_instructions(stage) if keyword == 'WORKDIR']
    assert dirs, f'{stage} sets no WORKDIR'
    return dirs[-1]


def test_the_web_build_stage_has_what_its_npm_build_runs():
    """A build that generates needs buf and proto/ in the stage; one that does not needs gen/proto/ts mounted."""
    scripts = json.loads(PACKAGE_JSON.read_text(encoding='utf-8'))['scripts']
    builds = [
        arguments
        for keyword, arguments in _stage_instructions('web_build_image')
        if keyword == 'RUN' and re.search(r'\bnpm run build\b', arguments)
    ]
    assert len(builds) == 1, builds
    steps = _npm_steps('build', scripts, run_pre='--ignore-scripts' not in builds[0])
    workdir = _workdir('web_build_image')
    provided = _stage_provided_paths('web_build_image')
    runs = ' '.join(arguments for keyword, arguments in _stage_instructions('web_build_image') if keyword == 'RUN')
    generators = [step for step in steps if step.split()[:2] == ['buf', 'generate']]
    for step in generators:
        assert 'bufbuild/buf' in runs, f'the build runs {step!r}, and web_build_image installs no buf'
        source = posixpath.normpath(posixpath.join(workdir, step.split()[2]))
        assert source in provided, f'the build runs {step!r}, and nothing in web_build_image puts {source} there'
    if not generators:
        generated = posixpath.normpath(posixpath.join(workdir, '../gen/proto/ts'))
        assert generated in provided, f'the build generates nothing, and nothing provides {generated}'


# ---------------------------------------------------------------------------------------------------
# THE PrimeUI LICENCE KEY (d02c9a3, tj-cwerrn): build time only

LICENCE_KEY = 'VITE_PRIMEUI_LICENSE_KEY'


def test_the_licence_key_placeholder_in_env_default_is_empty():
    """.env.default is public: the key goes in .env, never here."""
    text = ENV_DEFAULT.read_text(encoding='utf-8')
    lines = [line for line in text.splitlines() if line.startswith(f'{LICENCE_KEY}=')]
    assert lines == [f'{LICENCE_KEY}='], lines


def test_the_licence_key_is_a_build_arg_and_never_runtime_environment(web: dict):
    """Interpolated from .env or the shell into build.args, empty when unset; not in environment, no env_file."""
    args = web['build']['args']
    assert set(args) == {LICENCE_KEY}, args
    assert compose_model.interpolate_tree(args, {}) == {LICENCE_KEY: ''}
    assert compose_model.interpolate_tree(args, {LICENCE_KEY: 'k'}) == {LICENCE_KEY: 'k'}
    assert LICENCE_KEY not in web['environment'] and 'env_file' not in web


def test_the_web_build_stage_reads_the_licence_key_before_the_build():
    """ARG then ENV (Vite reads the process environment), both before `npm run build`."""
    instructions = _stage_instructions('web_build_image')
    build = next(i for i, (keyword, args) in enumerate(instructions) if keyword == 'RUN' and 'npm run build' in args)
    arg = instructions.index(('ARG', f'{LICENCE_KEY}='))
    env = instructions.index(('ENV', f'{LICENCE_KEY}=${LICENCE_KEY}'))
    assert arg < env < build, (arg, env, build)


def test_the_served_image_carries_no_licence_key_setting():
    """web_image starts FROM caddy and copies only dist/: no ARG or ENV of the key reaches the runtime image."""
    assert not [args for _, args in _stage_instructions('web_image') if LICENCE_KEY in args]


# ---------------------------------------------------------------------------------------------------
# THE GENERATED TYPESCRIPT, HOST-MADE (bc1565e, 1b4cfd1; the .26 RE: fix, option (a))
#
# make gen-proto-ts writes gen/proto/ts on the host; compose hands it to the build as the named context
# web_gen; the build RUN mounts it where the @generated alias resolves from WORKDIR /web, and runs the
# build with --ignore-scripts so the buf prebuild never runs. Nothing in the web stages runs buf.

GEN_CONTEXT = 'web_gen'
GEN_ALIAS_TARGET = '../gen/proto/ts'
VITE_CONFIG = REPO_ROOT / 'web' / 'vite.config.ts'
TSCONFIG_APP = REPO_ROOT / 'web' / 'tsconfig.app.json'


def _build_run() -> str:
    builds = [
        arguments
        for keyword, arguments in _stage_instructions('web_build_image')
        if keyword == 'RUN' and re.search(r'\bnpm run build\b', arguments)
    ]
    assert len(builds) == 1, builds
    return builds[0]


def _alias_mount_target() -> str:
    """Where, inside web_build_image, ../gen/proto/ts resolves from the stage's WORKDIR."""
    return posixpath.normpath(posixpath.join(_workdir('web_build_image'), GEN_ALIAS_TARGET))


def test_the_web_build_skips_the_npm_pre_scripts():
    """--ignore-scripts on the build command itself: prebuild is `npm run gen:proto`, i.e. buf generate."""
    words = shlex.split(_build_run())
    npm = words.index('npm')
    assert words[npm : npm + 3] == ['npm', 'run', 'build'] and '--ignore-scripts' in words[npm + 3 :], words


def test_the_web_build_mounts_the_host_generated_tree_where_the_alias_resolves():
    """RUN --mount=type=bind,from=web_gen,target=<WORKDIR>/../gen/proto/ts on the build RUN, and only there."""
    mounts = re.findall(r'--mount=(\S+)', _build_run())
    gen = [dict(part.split('=', 1) for part in mount.split(',')) for mount in mounts if f'from={GEN_CONTEXT}' in mount]
    assert gen == [{'type': 'bind', 'from': GEN_CONTEXT, 'target': _alias_mount_target()}], mounts
    assert _alias_mount_target() == '/gen/proto/ts', _workdir('web_build_image')


def test_the_alias_the_mount_serves_is_the_one_vite_and_tsconfig_resolve():
    """The mount target is derived from ../gen/proto/ts; hold vite.config.ts and tsconfig.app.json to that path."""
    vite = VITE_CONFIG.read_text(encoding='utf-8')
    aliases = re.findall(r"find:\s*'@generated',\s*replacement:\s*fileURLToPath\(new URL\('([^']+)'", vite)
    assert aliases == [GEN_ALIAS_TARGET], aliases
    tsconfig = TSCONFIG_APP.read_text(encoding='utf-8')
    paths = re.findall(r'"@generated/\*"\s*:\s*\[\s*"([^"]+)"\s*\]', tsconfig)
    assert paths == [f'{GEN_ALIAS_TARGET}/*'], paths


def test_the_overlay_declares_the_generated_context(web: dict):
    contexts = web['build']['additional_contexts']
    assert contexts.get(GEN_CONTEXT) == './gen/proto/ts', contexts


def test_no_web_stage_runs_buf():
    """No buf install, no buf generate: the image has neither buf nor proto/ (the host generates)."""
    offending = [
        arguments
        for stage in ('web_build_image', 'web_image')
        for keyword, arguments in _stage_instructions(stage)
        if keyword == 'RUN' and re.search(r'\bbuf\b|bufbuild/buf|gen:proto', arguments)
    ]
    assert not offending, offending


# ---------------------------------------------------------------------------------------------------
# THE DEV WEB SERVICE: docker-compose.web.dev.yaml (bead tj-mcrwrd, owner-ruled 2026-10-06)
#
# The Vite dev server with hot reload, the same service merged over the prod definition: web_build_image
# as its image, vite run directly (never `npm run dev`, whose predev is buf generate), the same one
# loopback port, read-only source and generated mounts, the licence key interpolated and never written,
# the secret blanked, no env_file, the proxy target derived from the same variables as Caddy's upstream.
# Loaded by DEV_COMPOSE and TOOLS_COMPOSE only. Compose's own render (`docker compose config`) and a
# running dev server are NOT RUN here: no Docker in the agent container.

WEB_DEV_COMPOSE = REPO_ROOT / 'docker-compose.web.dev.yaml'
VITE_COMMAND = ['./node_modules/.bin/vite', '--host', '0.0.0.0', '--port', '8080', '--strictPort']
PROXY_TARGET = 'VITE_DEV_PROXY_TARGET'
UPSTREAM = 'DATA_STORE_UPSTREAM'
WEB_DEV_SETS = frozenset({'DEV_COMPOSE', 'TOOLS_COMPOSE'})
# Interpolation environments the proxy target is compared under: compose defaults, the committed
# template, and both variables moved.
_UPSTREAM_ENVS = {
    'unset': {},
    'env-default': _env_file_values(ENV_DEFAULT),
    'moved': {'DATA_STORE_NAME': 'store-x', 'APP_INTERNAL_PORT': '9123'},
}


@pytest.fixture(scope='module')
def web_dev() -> dict:
    document = compose_model.load(WEB_DEV_COMPOSE)
    assert set(document['services']) == {'web'}, set(document['services'])
    return document['services']['web']


@pytest.fixture(scope='module')
def dev_model() -> dict:
    """The web service as DEV_COMPOSE merges it, in the Makefile's own file order."""
    return compose_model.merge([compose_model.load(path) for path in _set_files('DEV_COMPOSE')])['services']['web']


def test_the_dev_web_service_builds_the_node_build_stage(web: dict, web_dev: dict, dev_model: dict):
    """Its own tag, so a dev build never overwrites the prod :latest image the same service name builds."""
    assert web_dev['build'] == {'target': 'web_build_image'}, web_dev['build']
    assert dev_model['build']['target'] == 'web_build_image' and dev_model['build']['dockerfile'] == 'Dockerfile'
    assert web_dev['image'] == 'trader_joe_web:dev' and web['image'] == 'trader_joe_web:latest', (web_dev, web)


def test_the_dev_web_service_runs_vite_directly_and_nothing_else(web_dev: dict):
    """An exec-form command that IS vite: no shell, no npm (predev runs buf), no background process."""
    assert web_dev['command'] == VITE_COMMAND, web_dev['command']
    assert 'entrypoint' not in web_dev, web_dev.get('entrypoint')


def test_the_dev_web_service_publishes_the_prod_port_entry_and_no_other(web_dev: dict, dev_model: dict):
    """No ports: in the dev file, so the merge keeps exactly the prod loopback entry; vite listens on its 8080."""
    assert 'ports' not in web_dev, web_dev['ports']
    assert dev_model['ports'] == ['127.0.0.1:${WEB_PORT:-8088}:8080'], dev_model['ports']
    assert VITE_COMMAND[VITE_COMMAND.index('--port') + 1] == dev_model['ports'][0].rsplit(':', 1)[1]


def test_the_dev_web_service_mounts_source_and_generated_code_read_only(web_dev: dict):
    """./web/src over the baked /web/src, and gen/proto's PARENT so a regenerated ts/ stays visible."""
    volumes = [compose_model.volume(entry) for entry in web_dev['volumes']]
    assert volumes == [
        {'type': 'bind', 'source': './web/src', 'target': '/web/src', 'read_only': True},
        {'type': 'bind', 'source': './gen/proto', 'target': '/gen/proto', 'read_only': True},
    ], volumes
    assert posixpath.dirname(_alias_mount_target()) == '/gen/proto', _alias_mount_target()


def test_the_dev_web_service_reads_the_licence_key_by_interpolation_only(web_dev: dict):
    """environment:, never a literal and never an env_file: '' when unset, the caller's value when set."""
    value = web_dev['environment'][LICENCE_KEY]
    assert value == '${VITE_PRIMEUI_LICENSE_KEY:-}', value
    assert compose_model.interpolate(value, {}) == ''
    assert compose_model.interpolate(value, {LICENCE_KEY: 'k'}) == 'k'


def test_the_dev_web_service_blanks_the_secret(web_dev: dict, dev_model: dict):
    """The dev proxy injects no secret, so the container gets none, overriding the prod file's required one."""
    assert web_dev['environment']['INSTANCE_WRITE_SECRET'] == '', web_dev['environment']
    assert dev_model['environment']['INSTANCE_WRITE_SECRET'] == '', dev_model['environment']


def test_the_dev_web_service_has_no_env_file(web_dev: dict, dev_model: dict):
    assert 'env_file' not in web_dev and 'env_file' not in dev_model


def test_the_dev_web_service_gets_exactly_these_settings(web_dev: dict):
    assert set(web_dev['environment']) == {LICENCE_KEY, PROXY_TARGET, 'INSTANCE_WRITE_SECRET'}, web_dev['environment']


def test_the_dev_proxy_target_is_the_upstream_expression_with_a_scheme(web: dict, web_dev: dict):
    """The host:port expression is written twice; the two must be the same text, or they drift."""
    target, upstream = web_dev['environment'][PROXY_TARGET], web['environment'][UPSTREAM]
    assert target == f'http://{upstream}', (target, upstream)


@pytest.mark.parametrize('environment', list(_UPSTREAM_ENVS.values()), ids=list(_UPSTREAM_ENVS))
def test_the_dev_proxy_target_renders_to_the_data_store_caddy_reaches(
    web: dict, dev_model: dict, environment: dict[str, str]
):
    """Rendered, it is http://<Caddy's upstream>, the data_store service on store_api, under every env."""
    target = compose_model.interpolate(dev_model['environment'][PROXY_TARGET], environment)
    upstream = compose_model.interpolate(web['environment'][UPSTREAM], environment)
    assert target == f'http://{upstream}', (target, upstream)
    host = upstream.rsplit(':', 1)[0]
    assert host == environment.get('DATA_STORE_NAME', 'data_store'), upstream
    assert 'store_api' in compose_model.service_networks(dev_model), dev_model['networks']


def test_only_the_dev_and_tools_sets_load_the_dev_overlay_after_the_web_overlay():
    """Not PROD_COMPOSE and not the agent stack; where loaded, after the override and the web overlay."""
    loading = set()
    for variable in sorted(set(_COMPOSE_SET.findall(MAKEFILE.read_text(encoding='utf-8')))):
        names = [path.name for path in _set_files(variable)]
        if WEB_DEV_COMPOSE.name in names:
            loading.add(variable)
            dev = names.index(WEB_DEV_COMPOSE.name)
            assert names.index('docker-compose.override.yaml') < names.index(WEB_COMPOSE.name) < dev, names
    assert loading == WEB_DEV_SETS, sorted(loading)
    assert WEB_DEV_COMPOSE.name not in stack.COMPOSE_FILES, stack.COMPOSE_FILES
    mcp_dockerfile = (REPO_ROOT / 'tools' / 'agent_mcp' / 'Dockerfile').read_text(encoding='utf-8')
    assert WEB_DEV_COMPOSE.name not in mcp_dockerfile


# ---------------------------------------------------------------------------------------------------
# THE MAKE TARGETS THAT CARRY WEB (bead tj-mcrwrd)

PLACEHOLDER_SECRET = 'INSTANCE_WRITE_SECRET=unused'


@pytest.mark.parametrize('target', ['prod-build', 'prod-build-clean', 'dev-build'])
def test_the_image_builds_generate_the_typescript_first(target: str):
    """web_gen is ./gen/proto/ts, never committed: a build without gen-proto-ts has no context to read."""
    assert {'proto', 'gen-proto-ts'} <= set(_make_prerequisites(target)), _make_prerequisites(target)


def test_prod_launch_starts_web_with_the_services_and_waits_for_healthy():
    assert _make_recipe('prod-launch') == ['$(PROD_UP) data_store data_ingest web'], _make_recipe('prod-launch')
    assert '--wait' in _make_variable('PROD_UP').split(), _make_variable('PROD_UP')


def test_prod_logs_follows_web():
    words = ' '.join(_make_recipe('prod-logs')).split()
    assert words[words.index('logs') :] == ['logs', '-f', 'data_store', 'data_ingest', 'web'], words


def test_dev_launch_starts_web():
    assert _make_recipe('dev-launch') == ['$(DEV_COMPOSE) up data_store data_ingest web'], _make_recipe('dev-launch')


def test_prod_down_passes_the_secret_placeholder():
    """The web overlay's ':?' guard is evaluated on every command through PROD_COMPOSE, a down included."""
    (recipe,) = _make_recipe('prod-down')
    prefix = recipe.split('$(PROD_COMPOSE)', 1)[0].split()
    assert PLACEHOLDER_SECRET in prefix, recipe


# THE DEV SETS CARRY THEIR OWN FALLBACK (tj-grna9p.104). compose interpolates every file before merging,
# so the prod web overlay's ':?' rule fires on DEV_COMPOSE too, although docker-compose.web.dev.yaml blanks
# the value afterwards; a dev .env that leaves the secret empty (its shipped state) failed dev-build,
# dev-deps, dev-tools and dev-launch. DEV_COMPOSE now opens with a SHELL-DEFAULT placeholder, so a value
# the shell already holds still wins, and TOOLS_COMPOSE inherits it. PROD_COMPOSE carries none: prod keeps
# the required rule, because there the proxy injects the real secret.
DEV_TARGETS = ('dev-build', 'dev-deps', 'dev-tools', 'dev-launch', 'dev-down')
SECRET = 'INSTANCE_WRITE_SECRET'


def _dev_compose_prefix() -> str:
    """DEV_COMPOSE's text before `docker compose`, with make's `$$` turned into the shell's `$`."""
    value = _make_variable('DEV_COMPOSE')
    assert ' docker compose ' in f' {value} ', value
    return value.split('docker compose', 1)[0].replace('$$', '$').strip()


@pytest.mark.parametrize('target', DEV_TARGETS)
def test_every_dev_target_reaches_compose_through_the_set_that_carries_the_fallback(target: str):
    """Each dev target's compose call is $(DEV_COMPOSE) or $(TOOLS_COMPOSE), and sets no secret of its own.

    A recipe that wrote its own INSTANCE_WRITE_SECRET= would be a second copy of the rule, and a bare
    `docker compose` would skip the fallback and fail on the shipped dev .env again.
    """
    recipe = ' '.join(_make_recipe(target))
    assert '$(DEV_COMPOSE)' in recipe or '$(TOOLS_COMPOSE)' in recipe, recipe
    assert f'{SECRET}=' not in recipe, f'{target} sets the secret itself instead of through DEV_COMPOSE: {recipe}'


def test_the_tools_set_extends_the_dev_set():
    """TOOLS_COMPOSE gets the fallback only by starting with $(DEV_COMPOSE)."""
    assert _make_variable('TOOLS_COMPOSE').startswith('$(DEV_COMPOSE) '), _make_variable('TOOLS_COMPOSE')


@pytest.mark.parametrize(
    ('environment', 'expected'),
    [({}, 'unused'), ({SECRET: ''}, 'unused'), ({SECRET: 's3cret'}, 's3cret')],
    ids=['unset-falls-back', 'empty-falls-back', 'shell-value-wins'],
)
def test_the_dev_set_passes_a_fallback_that_a_shell_value_outranks(environment: dict[str, str], expected: str):
    """DEV_COMPOSE's prefix, run by sh: the placeholder when the shell has no value, the shell's when it has.

    Executed rather than read, so the claim is the shell's evaluation of the `:-` default: a plain
    `INSTANCE_WRITE_SECRET=unused` would overwrite a real value (the shell-value-wins case reds), and
    dropping the prefix leaves the variable unset (the fallback cases red).
    """
    prefix = _dev_compose_prefix()
    assert prefix, 'DEV_COMPOSE carries no assignment before docker compose'
    base = {key: value for key, value in os.environ.items() if key != SECRET}
    result = subprocess.run(
        ['/bin/sh', '-c', f'{prefix} env'], env=base | environment, capture_output=True, text=True, check=True
    )
    seen = dict(line.split('=', 1) for line in result.stdout.splitlines() if line.startswith(f'{SECRET}='))
    assert seen.get(SECRET) == expected, f'{prefix!r} under {environment} gave {seen.get(SECRET)!r}'


def test_the_prod_set_carries_no_fallback_and_the_overlay_keeps_the_rule_required():
    """Prod never gets a placeholder: PROD_COMPOSE names no secret, and the web overlay's value stays ':?'.

    test_the_secret_is_required shows the ':?' refuses an unset or empty value; this pins that nothing in
    PROD_COMPOSE pre-empts it the way DEV_COMPOSE's fallback does for dev.
    """
    assert SECRET not in _make_variable('PROD_COMPOSE'), _make_variable('PROD_COMPOSE')
    value = compose_model.load(WEB_COMPOSE)['services']['web']['environment'][SECRET]
    assert value.startswith('${INSTANCE_WRITE_SECRET:?'), value


def test_every_ci_render_of_a_set_with_the_web_overlay_passes_the_secret_placeholder():
    """`config` evaluates the ':?' guard too; which sets are rendered is pinned by test_ci_invariants.py."""
    step = next(step for step in _image_build_job().get('steps') or [] if step.get('name') == RENDER_STEP)
    with_web = []
    for line in _step_lines(step):
        if any(WEB_COMPOSE.name in files for files, _ in _compose_calls(line)):
            words = shlex.split(line)
            prefix = words[: words.index('docker')]
            with_web.append(prefix)
            assert PLACEHOLDER_SECRET in prefix, line
    assert len(with_web) == 3, with_web
