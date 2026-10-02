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
SERVICE_PATH=data, SERVICE_NAME=store): common, routers, schemas and data/store/app. The roots are
hard-coded, not parsed from the Dockerfile, so the scan and its oracle are not the same code;
test_the_roots_are_the_dockerfile_copy_sources asserts the mirror both ways. As a second backstop,
the scan checks that it reaches every first-party module data/store/app imports, transitively. Test
directories inside the roots are skipped; they are the test tree.

SHARED CODE: the parsing helpers -- the import resolver, the test-tree detector, the module
resolver and the Dockerfile COPY parser -- are imported from the ingest guard, not forked, so the
two images are held to one parser. That module's own tests pin the parser's rejection of COPY forms
it does not know. Only what is specific to this image -- its roots, its build args, the forbidden
seed package -- is defined here.

MOUNTED ROOTS (tj-r8ed1a): the image is not all data_store runs in prod. The base
docker-compose.yaml -- the only file PROD_COMPOSE loads -- bind-mounts ./data/store/alembic.ini and
./data/store/migrations into /code in every deployment, and alembic runs migrations/env.py and
versions/*.py there. They are production code on the prod path, so the same two families are
forbidden in them. They are a SEPARATE tuple, MOUNTED_ROOTS, because their oracle is a different
file: PRODUCTION_ROOTS mirror the Dockerfile's COPY sources and must stay exactly that mirror,
while MOUNTED_ROOTS (with ALEMBIC_INI) mirror the data_store service's ./ bind sources in
docker-compose.yaml. Each list is hard-coded and pinned to its own file both ways, so a new COPY
or a new mount goes red rather than unscanned, and neither pin can absorb the other's drift.
alembic.ini carries no imports, but it decides which scripts alembic loads; its script_location
and version_locations are pinned to resolve inside a scanned mounted root.

RELATIVE IMPORTS IN MIGRATIONS: inside the container the directory is /code/migrations, not the
package data.store.migrations, and alembic does not import it as a package at all. It loads
env.py and each revision by file path under a synthetic, package-less module id (alembic's
load_python_file derives it from the file name, e.g. env_py), so any relative import there raises
ImportError at runtime. The resolver reads a relative import against the file's REPO location, as
it does for the image: `from ..seeds import x` in data/store/migrations/env.py is data.store.seeds,
`from .. import tests` is data.store.tests. That is the module the author named, and both are
caught; an absolute import of tests.* or data.store.seeds is caught as written.

DYNAMIC IMPORTS (tj-ovig5s): every scan here -- the image scan and the mounted scan -- reads a
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

from data.ingest.tests.test_no_production_test_imports import (
    REPO_ROOT,
    SOURCE_STAGE,
    TEST_PACKAGE,
    copy_source,
    dockerfile_instructions,
    imported_modules,
    is_test_tree,
    module_file,
    scanned_imports,
    stage_instructions,
)


pytestmark = pytest.mark.data_store

APP_ROOT = 'data/store/app'
PRODUCTION_ROOTS = (APP_ROOT, 'routers', 'common', 'schemas')
SEED_PACKAGE = 'data.store.seeds'
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
        for path in (REPO_ROOT / root).rglob('*.py')
        if TEST_PACKAGE not in path.relative_to(REPO_ROOT).parts
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
        assert any(path.is_relative_to(REPO_ROOT / root) for path in modules), f'{root} has no modules'


def dockerfile_copy_directories() -> set[str]:
    """The directory sources of the source stage's COPY lines, resolved for the data_store image."""
    body = stage_instructions(dockerfile_instructions(DOCKERFILE.read_text(encoding='utf-8')), SOURCE_STAGE)
    sources = [
        copy_source(instruction, STORE_BUILD_ARGS)
        for instruction in body
        if instruction.split()[0].upper() in ('COPY', 'ADD')
    ]
    # Non-vacuous: a stage renamed or emptied of COPY lines must not pin against nothing.
    assert sources, f'{SOURCE_STAGE} has no COPY lines'
    directories: set[str] = set()
    for source in sources:
        path = REPO_ROOT / source
        assert path.exists(), f'{SOURCE_STAGE} copies {source}, which does not exist'
        if path.is_dir():
            directories.add(source)
    return directories


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
    seed_directory = SEED_PACKAGE.replace('.', '/')
    assert (REPO_ROOT / seed_directory).is_dir(), f'{seed_directory} is gone; revisit this guard'
    copied = dockerfile_copy_directories()
    assert not any(seed_directory == source or seed_directory.startswith(f'{source}/') for source in copied), (
        f'{SOURCE_STAGE} copies {seed_directory} into the data_store image'
    )


def test_the_scan_reaches_every_module_the_app_imports():
    # A root narrower than what the image loads -- routers/data_store where the app also imports
    # routers.common and routers.data_ingest -- would leave a shipped module unscanned while every
    # root still had modules.
    scanned = set(production_modules())
    pending = [path for path in scanned if path.is_relative_to(REPO_ROOT / APP_ROOT)]
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

    unscanned = sorted(str(path.relative_to(REPO_ROOT)) for path in reached - scanned)
    # Non-vacuous: the walk left the app, or a resolver that matched nothing would pass trivially.
    assert any(not path.is_relative_to(REPO_ROOT / APP_ROOT) for path in reached), 'the walk never left the app'
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
    probe = REPO_ROOT / APP_ROOT / '_r4_probe.py'
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
        f'{path.relative_to(REPO_ROOT)}:{line} imports {module}'
        for path in production_modules()
        for line, module in scanned_imports(path)
        if is_forbidden(module)
    ]

    assert not offenders, (
        'the data_store image imports the test tree or data.store.seeds (decision tj-j4wknb R4):\n'
        + '\n'.join(offenders)
    )


# --- The bind-mounted migrations (tj-r8ed1a) -------------------------------------------------------

COMPOSE_FILE = REPO_ROOT / 'docker-compose.yaml'
COMPOSE_SERVICE = 'data_store'
MIGRATIONS_ROOT = 'data/store/migrations'
MOUNTED_ROOTS = (MIGRATIONS_ROOT,)
ALEMBIC_INI = 'data/store/alembic.ini'
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
        assert (REPO_ROOT / source).exists(), f'{COMPOSE_SERVICE} mounts {source}, which does not exist'
    return mounts


def mounted_modules() -> list[Path]:
    """Every Python source file under the mounted roots, test directories excluded."""
    return sorted(
        path
        for root in MOUNTED_ROOTS
        for path in (REPO_ROOT / root).rglob('*.py')
        if TEST_PACKAGE not in path.relative_to(REPO_ROOT).parts
    )


def test_the_mounted_roots_are_the_compose_bind_mounts():
    # A new checkout directory mounted into data_store would run in prod unscanned; a mounted root
    # compose no longer mounts is a mirror gone stale.
    mounted = set(data_store_bind_mounts())
    ours = {*MOUNTED_ROOTS, ALEMBIC_INI}

    unscanned = sorted(mounted - ours)
    assert not unscanned, (
        f'{COMPOSE_FILE.name} mounts sources into {COMPOSE_SERVICE} the scan does not read: {unscanned}'
    )
    stale = sorted(ours - mounted)
    assert not stale, f'MOUNTED_ROOTS/ALEMBIC_INI name sources {COMPOSE_FILE.name} does not mount: {stale}'


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
    for unknown in (
        '      - ${SRC}:/code/x\n',
        '      - /abs/path:/code/x\n',
        '      - ../outside:/code/x\n',
        '      - type: bind\n        source: ./x\n        target: /code/x\n',
    ):
        with pytest.raises(AssertionError):
            compose_bind_mounts(f'services:\n  data_store:\n    volumes:\n{unknown}', 'data_store')


def test_the_scan_reads_every_mounted_root():
    # Non-vacuous: env.py and at least one revision -- a moved or emptied versions/ must not pass
    # having scanned nothing alembic runs.
    modules = mounted_modules()
    for root in MOUNTED_ROOTS:
        assert any(path.is_relative_to(REPO_ROOT / root) for path in modules), f'{root} has no modules'
    assert REPO_ROOT / MIGRATIONS_ROOT / 'env.py' in modules, 'the scan does not read migrations/env.py'
    revisions = [path for path in modules if path.parent == REPO_ROOT / MIGRATIONS_ROOT / 'versions']
    assert revisions, 'the scan reads no migration revision under migrations/versions'


def _container_to_source(container_path: PurePosixPath, mounts: dict[str, str]) -> str | None:
    """The repo source behind a container path, through the bind mounts, or None when none covers it."""
    for source, target in mounts.items():
        if container_path.is_relative_to(target):
            return str(PurePosixPath(source) / container_path.relative_to(target))
    return None


def test_alembic_loads_scripts_only_from_a_scanned_mounted_root():
    # alembic.ini has no imports of its own, but it chooses the scripts alembic runs: a
    # script_location or version_locations pointed at tests/ or seeds/ would run unscanned code.
    # Relative locations resolve against the working directory, which is the image's WORKDIR
    # /code (run_migrations.sh runs /code/.venv/bin/alembic there); %(here)s is the ini's directory.
    mounts = data_store_bind_mounts()
    ini_target = PurePosixPath(mounts[ALEMBIC_INI])
    config = configparser.ConfigParser(interpolation=None)
    config.read_string((REPO_ROOT / ALEMBIC_INI).read_text(encoding='utf-8'))
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
        assert source is not None, f'{ALEMBIC_INI} points alembic at {location}, which no bind mount provides'
        assert any(PurePosixPath(source).is_relative_to(root) for root in MOUNTED_ROOTS), (
            f'{ALEMBIC_INI} points alembic at {location} ({source}), outside MOUNTED_ROOTS'
        )


def test_the_mounted_scan_reaches_every_module_the_migrations_import():
    # What env.py and the revisions import must be scanned too: the image roots, or the mounts. A
    # first-party module outside both would run in prod unscanned (and crash, as it is not shipped).
    scanned = set(production_modules()) | set(mounted_modules())
    pending = list(mounted_modules())
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

    unscanned = sorted(str(path.relative_to(REPO_ROOT)) for path in reached - scanned)
    # Non-vacuous: env.py imports common and data.store.app, so the walk must leave the migrations.
    assert any(not path.is_relative_to(REPO_ROOT / MIGRATIONS_ROOT) for path in reached), (
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
    probe = REPO_ROOT / MIGRATIONS_ROOT / location / '_r4_probe.py'
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


def test_no_mounted_migration_imports_the_test_tree_or_the_seed_producer():
    offenders = [
        f'{path.relative_to(REPO_ROOT)}:{line} imports {module}'
        for path in mounted_modules()
        for line, module in scanned_imports(path)
        if is_forbidden(module)
    ]

    assert not offenders, (
        'a migration bind-mounted into data_store imports the test tree or data.store.seeds '
        '(decision tj-j4wknb R4):\n' + '\n'.join(offenders)
    )
