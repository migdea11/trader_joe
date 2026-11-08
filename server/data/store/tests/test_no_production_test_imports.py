"""No module the data_store image ships imports the test tree or the seed producer (decision tj-j4wknb R4).

R4: production code carries no test instrumentation. data/store/seeds is a sibling of
data/store/app that the image never copies, and it imports tests.fakes.market_data -- R4 permits
that, because neither ships. An import of data.store.seeds, or of tests.*, from anything the image
does ship is either a crash in prod or -- worse, if someone "fixes" the Dockerfile -- fakes and the
seed producer shipped in prod.

This is the data_store counterpart of data/ingest/tests/test_no_production_test_imports.py, which
scans the data_ingest image only. THE SCAN IS STATIC, over the source, for the same reason: an import
under TYPE_CHECKING, inside a function or behind a flag is exactly what a runtime check misses.

SCOPE: every module under PRODUCTION_ROOTS, which mirror the source COPY lines of the Dockerfile's
service_build_image stage resolved with the data_store build args (docker-compose.yaml passes
SERVICE_PATH=data, SERVICE_NAME=store): common, routers, schemas, gen/proto/python (the committed
generated gRPC code, decision tj-3mk3u5.42 F1) and data/store/app. The roots are
hard-coded, not parsed from the Dockerfile, so the scan and its oracle are not the same code;
test_the_roots_are_the_dockerfile_copy_sources asserts the mirror both ways. As a second backstop,
the scan checks that it reaches every first-party module data/store/app imports, transitively. Test
directories inside the roots are skipped; they are the test tree.

SHARED CODE: the parsing helpers -- the import resolver, the test-tree detector, the module
resolver and the Dockerfile COPY parser -- are imported from the ingest guard, not forked, so the
two images are held to one parser. That module's own tests pin the parser's rejection of COPY forms
it does not know. Only what is specific to this image -- its roots, its build args, the forbidden
seed package -- is defined here.

THE SHIPPED REVISIONS (tj-r8ed1a; bead tj-7zjov1.10): the image's app is not all data_store runs in
prod. alembic runs migrations/env.py and versions/*.py from /code, and alembic.ini decides which scripts
it loads. They used to be bind-mounted from the checkout; since tj-7zjov1.10 they are COPYed into the
image through the scratch stage migrations_data_store, which service_build_image copies whole into /code
through the selector stage migrations_selected (`COPY --from=migrations_selected / /code/`, the selector
being `FROM migrations_${SERVICE_PATH}_${SERVICE_NAME}`; tj-7zjov1.95). The ingest guard's
source_stage_copies follows the selector under this image's build args. They are production code on the
prod path, so the same two families are forbidden in them. They are a SEPARATE tuple, REVISION_ROOTS
(with ALEMBIC_INI), because their oracle is a different part of the Dockerfile: PRODUCTION_ROOTS mirror
the source stage's plain COPY sources, REVISION_ROOTS mirror the revision stage's. Each list is
hard-coded and pinned to its own COPY lines both ways, so a new COPY in either goes red rather than
unscanned, and neither pin can absorb the other's drift. alembic.ini carries no imports; its
script_location and version_locations are pinned to resolve inside a scanned revision root. The base
compose file is pinned to bind-mount nothing into data_store, so no checkout copy can shadow them.

RELATIVE IMPORTS IN MIGRATIONS: inside the container the directory is /code/migrations, not the
package data.store.migrations, and alembic does not import it as a package at all. It loads
env.py and each revision by file path under a synthetic, package-less module id (alembic's
load_python_file derives it from the file name, e.g. env_py), so any relative import there raises
ImportError at runtime. The resolver reads a relative import against the file's REPO location, as
it does for the image: `from ..seeds import x` in data/store/migrations/env.py is data.store.seeds,
`from .. import tests` is data.store.tests. That is the module the author named, and both are
caught; an absolute import of tests.* or data.store.seeds is caught as written.

DYNAMIC IMPORTS (tj-ovig5s): every scan here -- the image scan and the revision scan -- reads a
module through the ingest guard's scanned_imports, so besides import statements it reads a module
name passed to import_module or __import__ as a literal, or as the literal head of an f-string
(env.py already loads models that way). One helper, so the image and the mounts are held to R4 at
one strength. The reach walks follow static imports only.

MARKER: data_store -- the component whose production image this guards.
"""

import configparser
import re
from pathlib import Path, PurePosixPath

import pytest

from common.tests.roots import REPO_ROOT, repo_relative, resolve_tree
from data.ingest.tests.test_no_production_test_imports import (
    GENERATED_ROOT,
    SOURCE_STAGE,
    TEST_PACKAGE,
    imported_modules,
    is_test_tree,
    module_file,
    scanned_imports,
    source_stage_copies,
)


pytestmark = pytest.mark.data_store

# BOTH ROOTS (tj-iontkq.2), as in the ingest module this one borrows from. Everything named as a
# repository-relative TREE -- PRODUCTION_ROOTS, REVISION_ROOTS, ALEMBIC_INI, the seed package and the
# compose mount sources -- goes through resolve_tree(), because those names span the server root
# (data/store/*, routers, common, schemas) and the repository root (gen/proto/python). The two files
# read by name below, Dockerfile and docker-compose.yaml, stay at the top of the repository and so
# take REPO_ROOT directly.
#
# AND THEY ARE SPELLED AS THE BUILD CONTEXT SPELLS THEM (tj-iontkq.4), because the mirror tests
# compare these names with the Dockerfile's COPY sources and compose's bind sources as STRINGS:
# those read server/common and ./server/data/store/migrations since the service trees moved, so
# these must too. resolve_tree() keys on the first component and resolves either spelling, and
# repo_relative() keeps naming a server file by its import path, so nothing else here changes.
APP_ROOT = 'server/data/store/app'
PRODUCTION_ROOTS = (APP_ROOT, 'server/routers', 'server/common', 'server/schemas', GENERATED_ROOT)
# The DOTTED module name, which the move did not touch: PYTHONPATH points at the server root, so
# the package is still data.store.seeds. It is what the import ban below matches on.
SEED_PACKAGE = 'data.store.seeds'
# The same tree as a BUILD-CONTEXT PATH, which is no longer the dotted name with dots swapped for
# slashes. test_the_image_does_not_copy_the_seed_producer compares it against the COPY sources with
# == and startswith, so a bare data/store/seeds could never match after the move and the guard
# would pass WITHOUT GUARDING -- a seed producer copied into the image would stop being caught
# (architect finding S1 on tj-iontkq.4). Derived from SEED_PACKAGE so the two cannot drift.
SEED_DIRECTORY = f'server/{SEED_PACKAGE.replace(".", "/")}'
DOCKERFILE = REPO_ROOT / 'Dockerfile'
# The build args docker-compose.yaml passes for the data_store service.
STORE_BUILD_ARGS = {'SERVICE_PATH': 'data', 'SERVICE_NAME': 'store'}


def production_modules() -> list[Path]:
    """Every Python source file under the production roots, test directories excluded.

    Returns:
        list[Path]: Source files, repo-relative order.
    """
    return sorted(
        path
        for root in PRODUCTION_ROOTS
        for path in resolve_tree(root).rglob('*.py')
        if TEST_PACKAGE not in repo_relative(path).parts
    )


def is_seed_package(module: str) -> bool:
    """Whether a dotted module name is, or lies inside, the seed producer package."""
    return module == SEED_PACKAGE or module.startswith(f'{SEED_PACKAGE}.')


def is_forbidden(module: str) -> bool:
    """Whether the data_store image may not import a module: the test tree or the seed producer."""
    return is_test_tree(module) or is_seed_package(module)


def test_the_scan_reads_every_production_root():
    # A root that moved or emptied would make the scan below pass having read nothing.
    modules = production_modules()
    for root in PRODUCTION_ROOTS:
        assert any(path.is_relative_to(resolve_tree(root)) for path in modules), f'{root} has no modules'


def dockerfile_copy_directories() -> set[str]:
    """The directory sources of the source stage's plain COPY lines, resolved for the data_store image."""
    sources, _ = source_stage_copies(STORE_BUILD_ARGS)
    # Non-vacuous: a stage renamed or emptied of COPY lines must not pin against nothing.
    assert sources, f'{SOURCE_STAGE} has no COPY lines'
    directories: set[str] = set()
    for source in sources:
        path = resolve_tree(source)
        assert path.exists(), f'{SOURCE_STAGE} copies {source}, which does not exist'
        if path.is_dir():
            directories.add(source)
    return directories


def revision_copies() -> dict[str, str]:
    """What the data_store image takes from its revision stage: {build-context source: path under /code}."""
    _, copies = source_stage_copies(STORE_BUILD_ARGS)
    for source in copies:
        assert resolve_tree(source).exists(), f'the revision stage copies {source}, which does not exist'
    return copies


def test_the_roots_are_the_dockerfile_copy_sources():
    # A new source directory copied into the image would ship unscanned while every other test here
    # stayed green; a root the image does not copy is a mirror gone stale.
    copied = dockerfile_copy_directories()
    roots = set(PRODUCTION_ROOTS)

    unscanned = sorted(copied - roots)
    assert not unscanned, f'{SOURCE_STAGE} copies directories the scan does not read: {unscanned}'
    stale = sorted(roots - copied)
    assert not stale, f'PRODUCTION_ROOTS names directories {SOURCE_STAGE} does not copy: {stale}'


def test_the_image_does_not_copy_the_seed_producer():
    # The premise of forbidding the import: seeds is not shipped. If a COPY ever brought it in, the
    # rule here would be the wrong one, and that must be decided, not discovered in prod.
    assert resolve_tree(SEED_DIRECTORY).is_dir(), f'{SEED_DIRECTORY} is gone; revisit this guard'
    copied = dockerfile_copy_directories() | set(revision_copies())
    assert not any(source == SEED_DIRECTORY or SEED_DIRECTORY.startswith(f'{source}/') for source in copied), (
        f'{SOURCE_STAGE} or its revision stage copies {SEED_DIRECTORY} into the data_store image'
    )


def test_the_scan_reaches_every_module_the_app_imports():
    # A root narrower than what the image loads -- routers/data_store where the app also imports
    # routers.common and routers.data_ingest -- would leave a shipped module unscanned while every
    # root still had modules.
    scanned = set(production_modules())
    pending = [path for path in scanned if path.is_relative_to(resolve_tree(APP_ROOT))]
    reached: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in reached:
            continue
        reached.add(path)
        for _, module in imported_modules(path):
            target = module_file(module)
            if target is not None and not is_forbidden(module):
                pending.append(target)

    unscanned = sorted(str(repo_relative(path)) for path in reached - scanned)
    # Non-vacuous: the walk left the app, or a resolver that matched nothing would pass trivially.
    assert any(not path.is_relative_to(resolve_tree(APP_ROOT)) for path in reached), 'the walk never left the app'
    assert not unscanned, 'the app imports modules outside PRODUCTION_ROOTS:\n' + '\n'.join(unscanned)


def test_the_scan_recognises_a_forbidden_import():
    # The detector itself, against each name it must catch and the near misses it must not -- so a
    # scan that silently stopped matching could not keep the test below green.
    assert is_forbidden('tests')
    assert is_forbidden('tests.fakes.market_data')
    assert is_forbidden('data.store.tests.test_http_smoke')
    assert is_forbidden('data.store.seeds')
    assert is_forbidden('data.store.seeds.producer')
    assert not is_forbidden('data.store')
    assert not is_forbidden('data.store.app.main')
    assert not is_forbidden('data.store.seedsx')
    assert not is_forbidden('testsuite')


@pytest.mark.parametrize(
    ('source', 'expected'),
    [
        ('from data.store.seeds.producer import run\n', 'data.store.seeds.producer'),
        ('from data.store import seeds\n', 'data.store.seeds'),
        ('from ..seeds import scenario\n', 'data.store.seeds'),
        ('from .. import seeds\n', 'data.store.seeds'),
        ('import data.store.seeds.dump\n', 'data.store.seeds.dump'),
        ('if TYPE_CHECKING:\n    from tests.fakes import market_data\n', 'tests.fakes'),
        ('def f():\n    import tests.fakes.market_data\n', 'tests.fakes.market_data'),
        ("importlib.import_module('data.store.seeds')\n", 'data.store.seeds'),
        ("importlib.import_module(f'data.store.seeds.{name}')\n", 'data.store.seeds.'),
        ("def f():\n    __import__('tests.fakes.market_data')\n", 'tests.fakes.market_data'),
    ],
)
def test_the_resolver_names_each_import_form_from_inside_the_app(monkeypatch, tmp_path, source, expected):
    # Relative imports resolve against the file's package, so the probe must be read as if it sat
    # where an app module does, data/store/app, without writing into the tree the scan reads: the
    # probe path is never created, and reading it returns the stand-in's source.
    probe = resolve_tree(APP_ROOT) / '_r4_probe.py'
    stand_in = tmp_path / 'probe.py'
    stand_in.write_text(source, encoding='utf-8')
    original = Path.read_text
    monkeypatch.setattr(
        Path, 'read_text', lambda self, *args, **kwargs: original(stand_in if self == probe else self, *args, **kwargs)
    )
    modules = [module for _, module in scanned_imports(probe)]
    monkeypatch.undo()

    assert expected in modules
    assert any(is_forbidden(module) for module in modules)


def test_no_shipped_module_imports_the_test_tree_or_the_seed_producer():
    offenders = [
        f'{repo_relative(path)}:{line} imports {module}'
        for path in production_modules()
        for line, module in scanned_imports(path)
        if is_forbidden(module)
    ]

    assert not offenders, (
        'the data_store image imports the test tree or data.store.seeds (decision tj-j4wknb R4):\n'
        + '\n'.join(offenders)
    )


# --- The shipped revisions (tj-r8ed1a, bead tj-7zjov1.10) -------------------------------------------

COMPOSE_FILE = REPO_ROOT / 'docker-compose.yaml'
COMPOSE_SERVICE = 'data_store'
# Build-context spellings, as PRODUCTION_ROOTS above: these are compared with the revision stage's COPY
# sources, which read ./server/data/store/... (tj-iontkq.4).
MIGRATIONS_ROOT = 'server/data/store/migrations'
REVISION_ROOTS = (MIGRATIONS_ROOT,)
ALEMBIC_INI = 'server/data/store/alembic.ini'
# A named volume: a bare identifier, which carries no source from the checkout.
NAMED_VOLUME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*')


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(' '))


def _significant(lines: list[str]) -> list[tuple[int, str]]:
    """(indent, stripped text) for each line that is neither blank nor a whole-line comment."""
    return [(_indent(line), line.strip()) for line in lines if line.strip() and not line.strip().startswith('#')]


def _block(lines: list[tuple[int, str]], key: str, at: int) -> list[tuple[int, str]]:
    """The lines nested under the first `<key>:` at indent `at`, up to the next line at that indent or less."""
    for index, (indent, text) in enumerate(lines):
        if indent == at and text == f'{key}:':
            body: list[tuple[int, str]] = []
            for child_indent, child in lines[index + 1 :]:
                # A block sequence may sit at the key's own indent (`volumes:\n- a`).
                if child_indent < indent or (child_indent == indent and not child.startswith('- ')):
                    break
                body.append((child_indent, child))
            return body
    raise AssertionError(f'{COMPOSE_FILE.name} has no {key}: where one is expected')


def compose_bind_mounts(text: str, service: str) -> dict[str, str]:
    """The checkout bind mounts of one compose service, as {repo-relative source: container target}.

    Plain string handling, not a YAML parser, so the pin and the file are read by different code.
    Only the short `- ./src:/target[:mode]` form is recognised. A named or anonymous volume is
    skipped -- it carries nothing from the checkout. Anything else -- the long form, a variable, an absolute or
    parent-relative host path -- fails, so a new way to mount code cannot slip past by being skipped.

    Args:
        text (str): Compose file source.
        service (str): Service name under services:.

    Returns:
        dict[str, str]: Source without its leading ./, mapped to the container path.
    """
    services = _block(_significant(text.splitlines()), 'services', at=0)
    ours = _block(services, service, at=services[0][0])
    if (ours[0][0], 'volumes:') not in ours:
        # No volumes key of its own: the service mounts nothing (docker-compose.yaml's data_store since
        # bead tj-7zjov1.10). A `volumes:` nested deeper, under another key, is not the service's.
        return {}
    volumes = _block(ours, 'volumes', at=ours[0][0])

    mounts: dict[str, str] = {}
    for indent, item in volumes:
        assert indent == volumes[0][0] and item.startswith('- '), f'unrecognised volume form, extend the pin: {item}'
        entry = item[2:].split(' #')[0].strip().strip('\'"')
        source, _, rest = entry.partition(':')
        target = rest.split(':')[0]
        if (not rest and source.startswith('/')) or (NAMED_VOLUME.fullmatch(source) and target.startswith('/')):
            # An anonymous volume (a container path alone) or a named one: nothing from the checkout.
            continue
        recognised = source.startswith('./') and '$' not in source and '..' not in source and target.startswith('/')
        assert recognised, f'unrecognised volume form on {service}, extend the pin: {item}'
        mounts[source.removeprefix('./').rstrip('/')] = target.rstrip('/')
    return mounts


def data_store_bind_mounts() -> dict[str, str]:
    """The data_store service's checkout bind mounts in the base compose file, each checked to exist."""
    mounts = compose_bind_mounts(COMPOSE_FILE.read_text(encoding='utf-8'), COMPOSE_SERVICE)
    for source in mounts:
        assert resolve_tree(source).exists(), f'{COMPOSE_SERVICE} mounts {source}, which does not exist'
    return mounts


def revision_modules() -> list[Path]:
    """Every Python source file under the revision roots, test directories excluded."""
    return sorted(
        path
        for root in REVISION_ROOTS
        for path in resolve_tree(root).rglob('*.py')
        if TEST_PACKAGE not in repo_relative(path).parts
    )


def test_the_revision_roots_are_the_revision_stage_copies():
    # A new source copied into the revision stage would ship in data_store unscanned; a revision root
    # the stage no longer copies is a mirror gone stale. The targets are where alembic and env.py look.
    copies = revision_copies()
    ours = {*REVISION_ROOTS, ALEMBIC_INI}

    unscanned = sorted(set(copies) - ours)
    assert not unscanned, f'the data_store revision stage copies sources the scan does not read: {unscanned}'
    stale = sorted(ours - set(copies))
    assert not stale, f'REVISION_ROOTS/ALEMBIC_INI name sources the revision stage does not copy: {stale}'
    assert copies == {ALEMBIC_INI: '/code/alembic.ini', MIGRATIONS_ROOT: '/code/migrations'}, copies


def test_the_base_compose_file_mounts_nothing_from_the_checkout_into_data_store():
    # Since tj-7zjov1.10 the revisions are in the image. A checkout bind mount on data_store, in the
    # file every deployment loads, would shadow them with whatever the host holds, and run unscanned.
    assert data_store_bind_mounts() == {}, f'{COMPOSE_FILE.name} bind-mounts into {COMPOSE_SERVICE}'


def test_the_compose_parser_reads_only_forms_it_knows():
    # The pin is only as strict as the parser: every form it cannot resolve must fail, not vanish.
    text = (
        'services:\n'
        '  other:\n'
        '    volumes:\n'
        '      - ./elsewhere:/code/elsewhere\n'
        '  data_store:\n'
        '    image: x\n'
        '    volumes:\n'
        '      # a comment\n'
        '      - ./data/store/alembic.ini:/code/alembic.ini\n'
        '      - "./data/store/migrations/:/code/migrations:ro"\n'
        '      - cache:/code/cache\n'
        '      - /code/scratch\n'
        '    env_file:\n'
        '      - .env\n'
    )
    assert compose_bind_mounts(text, 'data_store') == {
        'data/store/alembic.ini': '/code/alembic.ini',
        'data/store/migrations': '/code/migrations',
    }
    # No volumes key is no mounts; a volumes: elsewhere (the top-level one) is not the service's own.
    assert (
        compose_bind_mounts('services:\n  data_store:\n    image: x\n    env_file:\n      - .env\n', 'data_store') == {}
    )
    assert compose_bind_mounts('services:\n  data_store:\n    image: x\nvolumes:\n  cache:\n', 'data_store') == {}
    for unknown in (
        '      - ${SRC}:/code/x\n',
        '      - /abs/path:/code/x\n',
        '      - ../outside:/code/x\n',
        '      - type: bind\n        source: ./x\n        target: /code/x\n',
    ):
        with pytest.raises(AssertionError):
            compose_bind_mounts(f'services:\n  data_store:\n    volumes:\n{unknown}', 'data_store')


def test_the_scan_reads_every_revision_root():
    # Non-vacuous: env.py and at least one revision -- a moved or emptied versions/ must not pass
    # having scanned nothing alembic runs.
    modules = revision_modules()
    for root in REVISION_ROOTS:
        assert any(path.is_relative_to(resolve_tree(root)) for path in modules), f'{root} has no modules'
    assert resolve_tree(MIGRATIONS_ROOT) / 'env.py' in modules, 'the scan does not read migrations/env.py'
    revisions = [path for path in modules if path.parent == resolve_tree(MIGRATIONS_ROOT) / 'versions']
    assert revisions, 'the scan reads no migration revision under migrations/versions'


def _container_to_source(container_path: PurePosixPath, mounts: dict[str, str]) -> str | None:
    """The repo source behind a container path, through the revision copies, or None when none covers it."""
    for source, target in mounts.items():
        if container_path.is_relative_to(target):
            return str(PurePosixPath(source) / container_path.relative_to(target))
    return None


def test_alembic_loads_scripts_only_from_a_scanned_revision_root():
    # alembic.ini has no imports of its own, but it chooses the scripts alembic runs: a
    # script_location or version_locations pointed at tests/ or seeds/ would run unscanned code.
    # Relative locations resolve against the working directory, which is the image's WORKDIR
    # /code (run_migrations.sh runs /code/.venv/bin/alembic there); %(here)s is the ini's directory,
    # /code, where the revision stage puts it.
    mounts = revision_copies()
    ini_target = PurePosixPath(mounts[ALEMBIC_INI])
    config = configparser.ConfigParser(interpolation=None)
    config.read_string(resolve_tree(ALEMBIC_INI).read_text(encoding='utf-8'))
    section = config['alembic']
    workdir = PurePosixPath('/code')

    def resolve(location: str) -> PurePosixPath:
        location = location.replace('%(here)s', str(ini_target.parent))
        return PurePosixPath(location) if location.startswith('/') else workdir / location

    script_location = resolve(section['script_location'])
    raw_versions = section.get('version_locations', '').split('#')[0].strip()
    version_locations = [resolve(part) for part in re.split(r'[\s,:;]+', raw_versions) if part] or [
        script_location / 'versions'
    ]

    for location in [script_location, *version_locations]:
        source = _container_to_source(location, mounts)
        assert source is not None, f'{ALEMBIC_INI} points alembic at {location}, which the image does not hold'
        assert any(PurePosixPath(source).is_relative_to(root) for root in REVISION_ROOTS), (
            f'{ALEMBIC_INI} points alembic at {location} ({source}), outside REVISION_ROOTS'
        )


def test_the_revision_scan_reaches_every_module_the_migrations_import():
    # What env.py and the revisions import must be scanned too: the image roots, or the revisions. A
    # first-party module outside both would run in prod unscanned (and crash, as it is not shipped).
    scanned = set(production_modules()) | set(revision_modules())
    pending = list(revision_modules())
    reached: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in reached:
            continue
        reached.add(path)
        for _, module in imported_modules(path):
            target = module_file(module)
            if target is not None and not is_forbidden(module):
                pending.append(target)

    unscanned = sorted(str(repo_relative(path)) for path in reached - scanned)
    # Non-vacuous: env.py imports common and data.store.app, so the walk must leave the migrations.
    assert any(not path.is_relative_to(resolve_tree(MIGRATIONS_ROOT)) for path in reached), (
        'the walk never left the migrations'
    )
    assert not unscanned, 'the migrations import modules no scan reads:\n' + '\n'.join(unscanned)


@pytest.mark.parametrize(
    ('location', 'source', 'expected'),
    [
        ('', 'if TYPE_CHECKING:\n    from tests.fakes import market_data\n', 'tests.fakes'),
        ('', 'from data.store.seeds.producer import run\n', 'data.store.seeds.producer'),
        ('', 'from ..seeds import scenario\n', 'data.store.seeds'),
        ('', 'from .. import seeds\n', 'data.store.seeds'),
        ('', 'from .. import tests\n', 'data.store.tests'),
        ('', "importlib.import_module('tests.fakes.market_data')\n", 'tests.fakes.market_data'),
        ('', "importlib.import_module(f'data.store.seeds.{name}')\n", 'data.store.seeds.'),
        ('versions', 'from ...seeds import producer\n', 'data.store.seeds'),
        ('versions', 'from data.store import seeds\n', 'data.store.seeds'),
        ('versions', 'def upgrade():\n    import tests.fakes.market_data\n', 'tests.fakes.market_data'),
        ('versions', "def upgrade():\n    __import__('tests.fakes')\n", 'tests.fakes'),
    ],
)
def test_the_resolver_names_each_import_form_from_inside_the_migrations(
    monkeypatch, tmp_path, location, source, expected
):
    # As for the app: the probe is read as if it sat in migrations/ or migrations/versions/, so
    # relative imports resolve against the repo location, without writing into the scanned tree.
    probe = resolve_tree(MIGRATIONS_ROOT) / location / '_r4_probe.py'
    stand_in = tmp_path / 'probe.py'
    stand_in.write_text(source, encoding='utf-8')
    original = Path.read_text
    monkeypatch.setattr(
        Path, 'read_text', lambda self, *args, **kwargs: original(stand_in if self == probe else self, *args, **kwargs)
    )
    modules = [module for _, module in scanned_imports(probe)]
    monkeypatch.undo()

    assert expected in modules
    assert any(is_forbidden(module) for module in modules)


def test_no_shipped_migration_imports_the_test_tree_or_the_seed_producer():
    offenders = [
        f'{repo_relative(path)}:{line} imports {module}'
        for path in revision_modules()
        for line, module in scanned_imports(path)
        if is_forbidden(module)
    ]

    assert not offenders, (
        'a migration shipped in the data_store image imports the test tree or data.store.seeds '
        '(decision tj-j4wknb R4):\n' + '\n'.join(offenders)
    )
