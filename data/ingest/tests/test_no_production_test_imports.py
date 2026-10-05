"""No production module references the test tree (decision tj-j4wknb R4).

R4: production code carries no test instrumentation. Fakes live under tests/ and reach a running
service only through the test-only launcher (tests/fakes/ingest_launcher.py) and a compose
overlay, never because production imports them. The prod image copies common, routers, schemas
and <service>/app and nothing under tests/, so an import of the test tree from production is
either a crash in prod or -- worse, if someone "fixes" the Dockerfile -- fakes shipped in prod.

THE SCAN IS STATIC, over the source, not over sys.modules: an import that only runs on some path
(inside a function, under TYPE_CHECKING, behind a flag) is exactly the kind a runtime check
misses, and parsing finds it wherever it sits. Besides import statements, the scan reads a module
name passed to importlib.import_module or __import__ as a string literal, or as the literal head of
an f-string (scanned_imports). A name built from variables alone cannot be read statically and is
not reported -- the limit of a static scan. The data_store guard and its bind-mounted migrations
scan read through the same helper, so R4 is enforced at one strength in every scan.

SCOPE: every module under PRODUCTION_ROOTS, which mirror the source COPY lines of the Dockerfile's
service_build_image stage, the stage both the dev and prod images copy /code from:
    COPY ./common            -> common
    COPY ./routers           -> routers           (all of it, not only routers/data_ingest)
    COPY ./schemas           -> schemas
    COPY ./gen/proto/python  -> gen/proto/python  (committed protoc output, decision tj-3mk3u5.42 F1)
    COPY ./${SERVICE_PATH}/${SERVICE_NAME}/app  -> data/ingest/app for this image
The roots are hard-coded, not parsed from the Dockerfile -- reading them from it would make the
scan and its oracle the same code -- so a change to those COPY lines must be mirrored here by hand.
test_the_roots_are_the_dockerfile_copy_sources asserts the mirror: the directory sources of that
stage's COPY lines must equal PRODUCTION_ROOTS, both ways, and a COPY form the parser does not
recognise is a failure, never a skip. As a second backstop, the scan checks that it reaches every
first-party module data/ingest/app imports, transitively: a root narrowed below what the app
actually loads goes red there. Test directories inside the roots are skipped; they are the test
tree.

FIRST-PARTY MEANS ON THE IMAGE'S PATH. A dotted name resolves against each of the image's PYTHONPATH
entries in order (common/tests/image_path.py, pinned to the Dockerfile's ENV PYTHONPATH): the
repository root, then gen/proto/python, where trader_joe.proto lives. Resolved from the root alone,
generated code is invisible to the reach walk, and an app that imports it through common.rpc would
pass that walk with the generated tree unscanned and uncopied.

MARKER: data_ingest. Under pytest.ini's rule a test carries the marker of the component whose
interface it drives; this one drives none, so the marker names the component whose production
image it guards -- the roots scanned are what the data_ingest image is built from.
"""

import ast
from pathlib import Path

import pytest

from common.tests.image_path import image_import_roots
from common.tests.roots import REPO_ROOT, SERVER_ROOT, repo_relative, resolve_tree


pytestmark = pytest.mark.data_ingest

# THREE DIFFERENT NEEDS, which one name called REPO_ROOT could not tell apart (tj-iontkq.2):
#   * PRODUCTION_ROOTS below spans BOTH roots -- data/ingest/app, routers, common and schemas travel
#     with the services, gen/proto/python does not -- so every walk over it goes through
#     resolve_tree() and names its hits with repo_relative().
#   * DOCKERFILE is the repository's own Dockerfile, which stays at the top: REPO_ROOT.
#   * module_file() searches as the IMAGE's interpreter searches, so it is handed SERVER_ROOT, the
#     directory the image calls /code.
APP_ROOT = 'data/ingest/app'
GENERATED_ROOT = 'gen/proto/python'
PRODUCTION_ROOTS = (APP_ROOT, 'routers', 'common', 'schemas', GENERATED_ROOT)
TEST_PACKAGE = 'tests'
DYNAMIC_IMPORTERS = ('import_module', '__import__')


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


def package_of(path: Path) -> list[str]:
    """The dotted package a module sits in, as the parts a relative import resolves against."""
    return list(repo_relative(path).parent.parts)


def imported_modules(path: Path) -> list[tuple[int, str]]:
    """Every module a source file imports, relative imports resolved to absolute names.

    Args:
        path (Path): Source file.

    Returns:
        list[tuple[int, str]]: (line, absolute dotted module name) per import.
    """
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                package = package_of(path)
                base = package[: len(package) - (node.level - 1)]
                module = '.'.join([*base, *([node.module] if node.module else [])])
            else:
                module = node.module or ''
            found.append((node.lineno, module))
            # `from . import tests` names the package in the alias, not in node.module.
            found.extend((node.lineno, f'{module}.{alias.name}') for alias in node.names)
    return found


def dynamic_imports(path: Path) -> list[tuple[int, str]]:
    """Module names passed to import_module / __import__ as a literal, or as an f-string's literal head.

    A name built at runtime from variables alone cannot be read statically and is not reported.

    Args:
        path (Path): Source file.

    Returns:
        list[tuple[int, str]]: (line, module name or its literal head) per call.
    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'), filename=str(path))):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
        if name not in DYNAMIC_IMPORTERS:
            continue
        argument = node.args[0]
        if isinstance(argument, ast.JoinedStr) and argument.values:
            argument = argument.values[0]
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            found.append((node.lineno, argument.value))
    return found


def scanned_imports(path: Path) -> list[tuple[int, str]]:
    """What every R4 scan reads from a source file: its static imports and its literal dynamic ones.

    Args:
        path (Path): Source file.

    Returns:
        list[tuple[int, str]]: (line, module name) per import, static first.
    """
    return [*imported_modules(path), *dynamic_imports(path)]


def is_test_tree(module: str) -> bool:
    """Whether a dotted module name is, or lies inside, a test package: tests.*, or any *.tests.*."""
    return TEST_PACKAGE in module.split('.')


def test_the_scan_reads_every_production_root():
    # A root that moved or emptied would make the scan below pass having read nothing.
    for root in PRODUCTION_ROOTS:
        assert any(path.is_relative_to(resolve_tree(root)) for path in production_modules()), f'{root} has no modules'


DOCKERFILE = REPO_ROOT / 'Dockerfile'
SOURCE_STAGE = 'service_build_image'
# The build args docker-compose.yaml passes for the data_ingest service.
INGEST_BUILD_ARGS = {'SERVICE_PATH': 'data', 'SERVICE_NAME': 'ingest'}


def dockerfile_instructions(text: str) -> list[str]:
    """A Dockerfile's logical instructions: continuations joined, comments and blank lines dropped.

    Args:
        text (str): Dockerfile source.

    Returns:
        list[str]: One string per instruction, whitespace-collapsed.
    """
    instructions: list[str] = []
    pending: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.endswith('\\'):
            pending.append(line[:-1])
            continue
        instructions.append(' '.join(' '.join([*pending, line]).split()))
        pending = []
    assert not pending, f'Dockerfile ends inside a continuation: {pending}'
    return instructions


def stage_instructions(instructions: list[str], stage: str) -> list[str]:
    """The instructions of one build stage: after its `FROM ... AS <stage>`, up to the next FROM."""
    body: list[str] | None = None
    for instruction in instructions:
        words = instruction.split()
        if words[0].upper() == 'FROM':
            if body is not None:
                break
            if len(words) == 4 and words[2].upper() == 'AS' and words[3] == stage:
                body = []
        elif body is not None:
            body.append(instruction)
    assert body is not None, f'the Dockerfile has no stage named {stage}'
    return body


def copy_source(instruction: str, build_args: dict[str, str]) -> str:
    """The repo-relative source path of a `COPY <src> <dest>` instruction, build args resolved.

    Only the plain one-source form is recognised. Anything else -- a flag (--from, --chown, ...),
    the JSON form, several sources, a heredoc, a glob, an unresolved variable, ADD -- fails, so a
    new form of copy cannot slip past the pin by being skipped.

    Args:
        instruction (str): One logical instruction from the source stage.
        build_args (dict[str, str]): Values for the ${ARG} references in the source.

    Returns:
        str: The source, repo-relative, without a leading ./ or trailing /.
    """
    words = instruction.split()
    keyword = words[0].upper()
    assert keyword != 'ADD', f'unrecognised copy form in {SOURCE_STAGE}, extend the pin: {instruction}'
    assert keyword == 'COPY', f'not a COPY: {instruction}'
    recognised = (
        len(words) == 3
        and not words[1].startswith(('-', '['))
        and '<<' not in instruction
        and not any(char in words[1] for char in '*?[')
    )
    assert recognised, f'unrecognised COPY form in {SOURCE_STAGE}, extend the pin: {instruction}'
    source = words[1]
    for name, value in build_args.items():
        source = source.replace(f'${{{name}}}', value).replace(f'${name}', value)
    assert '$' not in source, f'unresolved build arg in {SOURCE_STAGE}: {instruction}'
    source = source.removeprefix('./').rstrip('/')
    assert source and not source.startswith(('/', '..')), f'source outside the build context: {instruction}'
    return source


def dockerfile_copy_directories() -> set[str]:
    """The directory sources of the source stage's COPY lines, resolved for the data_ingest image."""
    body = stage_instructions(dockerfile_instructions(DOCKERFILE.read_text(encoding='utf-8')), SOURCE_STAGE)
    sources = [
        copy_source(instruction, INGEST_BUILD_ARGS)
        for instruction in body
        if instruction.split()[0].upper() in ('COPY', 'ADD')
    ]
    # Non-vacuous: a stage renamed or emptied of COPY lines must not pin against nothing.
    assert sources, f'{SOURCE_STAGE} has no COPY lines'
    directories: set[str] = set()
    for source in sources:
        path = resolve_tree(source)
        assert path.exists(), f'{SOURCE_STAGE} copies {source}, which does not exist'
        if path.is_dir():
            directories.add(source)
    return directories


def test_the_dockerfile_parser_rejects_copy_forms_it_does_not_know():
    # The pin is only as strict as the parser: every form it cannot resolve must fail, not vanish.
    args = INGEST_BUILD_ARGS
    assert copy_source('COPY ./${SERVICE_PATH}/${SERVICE_NAME}/app /code/x', args) == 'data/ingest/app'
    assert copy_source('COPY ./common/ /code/common', args) == 'common'
    for unknown in (
        'COPY --chown=appuser ./sdk /code/sdk',
        'COPY --from=other /code /code',
        'COPY ["./sdk", "/code/sdk"]',
        'COPY ./sdk ./extra /code/',
        'COPY ./sdk*/ /code/',
        'COPY <<EOF /code/x',
        'COPY ./${OTHER}/app /code/app',
        'ADD ./sdk /code/sdk',
    ):
        with pytest.raises(AssertionError):
            copy_source(unknown, args)


def test_the_roots_are_the_dockerfile_copy_sources():
    # A new source directory copied into the image (COPY ./sdk /code/sdk) would ship unscanned while
    # every other test here stayed green; a root the image does not copy is a mirror gone stale.
    copied = dockerfile_copy_directories()
    roots = set(PRODUCTION_ROOTS)

    unscanned = sorted(copied - roots)
    assert not unscanned, f'{SOURCE_STAGE} copies directories the scan does not read: {unscanned}'
    stale = sorted(roots - copied)
    assert not stale, f'PRODUCTION_ROOTS names directories {SOURCE_STAGE} does not copy: {stale}'


def module_file(module: str) -> Path | None:
    """The repo source file a dotted module name resolves to, or None when it is not first-party.

    Searched as the image's interpreter searches: each PYTHONPATH entry in order, the repository root
    first, then gen/proto/python (see FIRST-PARTY MEANS ON THE IMAGE'S PATH above).
    """
    for root in image_import_roots(SERVER_ROOT):
        base = root.joinpath(*module.split('.'))
        for candidate in (base.with_suffix('.py'), base / '__init__.py'):
            if candidate.is_file():
                return candidate
    return None


def test_the_resolver_finds_generated_code_on_the_images_second_root():
    # Non-vacuous for the reach walk below: no app imports generated code yet (registered_services()
    # is empty), so that walk alone would pass with a resolver that never looks past the root. The
    # one importer today, common.rpc.ping, is followed here instead, as the walk will follow it.
    generated = resolve_tree(GENERATED_ROOT) / 'trader_joe' / 'proto' / 'ping' / 'v1'
    assert module_file('trader_joe.proto.ping.v1.ping_pb2') == generated / 'ping_pb2.py'
    assert module_file('trader_joe.proto.ping.v1.ping_pb2_grpc') == generated / 'ping_pb2_grpc.py'
    assert module_file('common.rpc.ping') == SERVER_ROOT / 'common' / 'rpc' / 'ping.py'
    reached = {module_file(module) for _, module in imported_modules(SERVER_ROOT / 'common' / 'rpc' / 'ping.py')}
    assert generated / 'ping_pb2.py' in reached, 'common.rpc.ping no longer reaches the generated modules'
    assert reached - {None} <= set(production_modules()), 'common.rpc.ping imports a module the scan does not read'


def test_the_scan_reaches_every_module_the_app_imports():
    # A root narrower than what the image loads -- routers/data_ingest where the app also imports
    # routers.common -- would leave a shipped module unscanned while every root still had modules.
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
            if target is not None and not is_test_tree(module):
                pending.append(target)

    unscanned = sorted(str(repo_relative(path)) for path in reached - scanned)
    # Non-vacuous: the walk left the app, or a resolver that matched nothing would pass trivially.
    assert any(not path.is_relative_to(resolve_tree(APP_ROOT)) for path in reached), 'the walk never left the app'
    assert not unscanned, 'the app imports modules outside PRODUCTION_ROOTS:\n' + '\n'.join(unscanned)


def test_the_scan_recognises_an_import_of_the_test_tree():
    # The detector itself, against each import form it must catch -- so a scan that silently
    # stopped matching could not keep the test below green.
    assert is_test_tree('tests')
    assert is_test_tree('tests.fakes.market_data')
    assert is_test_tree('data.ingest.tests.test_read_seam')
    assert not is_test_tree('data.ingest.app.brokers.interface')
    assert not is_test_tree('testsuite')


def read_as_app_module(monkeypatch, tmp_path, source: str) -> list[str]:
    """The modules scanned_imports reads from `source`, as if it sat in data/ingest/app.

    Relative imports resolve against the file's package, so the probe must be read where an app
    module sits, without writing into the tree the scan reads: the probe path is never created,
    and reading it returns the stand-in's source.
    """
    probe = resolve_tree(APP_ROOT) / '_r4_probe.py'
    stand_in = tmp_path / 'probe.py'
    stand_in.write_text(source, encoding='utf-8')
    original = Path.read_text
    monkeypatch.setattr(
        Path, 'read_text', lambda self, *args, **kwargs: original(stand_in if self == probe else self, *args, **kwargs)
    )
    try:
        return [module for _, module in scanned_imports(probe)]
    finally:
        monkeypatch.undo()


@pytest.mark.parametrize(
    ('source', 'expected'),
    [
        ('from tests.fakes import market_data\n', 'tests.fakes'),
        ('if TYPE_CHECKING:\n    from tests.fakes import market_data\n', 'tests.fakes'),
        ('def f():\n    import tests.fakes.market_data\n', 'tests.fakes.market_data'),
        ('from .. import tests\n', 'data.ingest.tests'),
        ('from ..tests import alpaca_recorded\n', 'data.ingest.tests'),
        ("importlib.import_module('tests.fakes.market_data')\n", 'tests.fakes.market_data'),
        ("import_module('tests.fakes')\n", 'tests.fakes'),
        ("def f():\n    __import__('tests.fakes.market_data')\n", 'tests.fakes.market_data'),
        ("importlib.import_module(f'tests.fakes.{name}')\n", 'tests.fakes.'),
        ("importlib.import_module(f'data.ingest.tests.{name}')\n", 'data.ingest.tests.'),
    ],
)
def test_the_resolver_names_each_import_form_from_inside_the_app(monkeypatch, tmp_path, source, expected):
    # Each form an app module could use to reach the test tree, static and literal dynamic alike.
    modules = read_as_app_module(monkeypatch, tmp_path, source)

    assert expected in modules
    assert any(is_test_tree(module) for module in modules)


@pytest.mark.parametrize(
    'source',
    [
        "importlib.import_module('data.ingest.app.main')\n",
        "importlib.import_module(f'data.ingest.app.brokers.{name}')\n",
        "loader.load('tests.fakes')\n",
        "log.info('tests.fakes')\n",
    ],
)
def test_the_dynamic_read_does_not_flag_what_is_not_a_test_tree_import(monkeypatch, tmp_path, source):
    # Precision: a production module name, and a string that only looks like one but is passed to
    # something other than an importer, must not become offenders.
    modules = read_as_app_module(monkeypatch, tmp_path, source)

    assert not any(is_test_tree(module) for module in modules)


def test_no_production_module_imports_the_test_tree():
    offenders = [
        f'{repo_relative(path)}:{line} imports {module}'
        for path in production_modules()
        for line, module in scanned_imports(path)
        if is_test_tree(module)
    ]

    assert not offenders, 'production code imports the test tree (decision tj-j4wknb R4):\n' + '\n'.join(offenders)
