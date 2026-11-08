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
not reported -- the limit of a static scan. The data_store guard and its shipped-migrations
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
import re
from pathlib import Path, PurePosixPath

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
#
# AND THEY ARE SPELLED AS THE BUILD CONTEXT SPELLS THEM (tj-iontkq.4), because
# test_the_roots_are_the_dockerfile_copy_sources compares this tuple with the Dockerfile's COPY
# sources as STRINGS: those read server/common since the service trees moved, so these must too.
# resolve_tree() keys on the first component and resolves either spelling, and repo_relative()
# keeps naming a server file by its import path, so nothing else in this file changes.
APP_ROOT = 'server/data/ingest/app'
GENERATED_ROOT = 'gen/proto/python'
PRODUCTION_ROOTS = (APP_ROOT, 'server/routers', 'server/common', 'server/schemas', GENERATED_ROOT)
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


# THE REVISION STAGE (bead tj-7zjov1.10; selected through a FROM since tj-7zjov1.95). service_build_image
# pulls one stage whole into /code with a FIXED name: `COPY --from=migrations_selected / /code/`, because
# Docker refuses a build arg in --from ("variable expansion is not supported for --from"). That stage is a
# SELECTOR, `FROM migrations_${SERVICE_PATH}_${SERVICE_NAME} AS migrations_selected`, which a FROM may
# expand only from ARGs declared before the first FROM; it is whichever per-service scratch stage the
# build args name. What that stage holds ships in the image as surely as a plain COPY does, so it is read
# here, not skipped: stage_copy recognises exactly the one whole-stage form and refuses an expanded --from
# (anything else with --from still fails in copy_source), selected_stage follows the selector to the stage
# it names, and scratch_stage_copies reads that stage, which must be `FROM scratch` with plain COPYs only.
WHOLE_STAGE_COPY = re.compile(r'COPY --from=(?P<stage>\S+) / /code/')
IMAGE_ROOT = PurePosixPath('/code')
ARG_REFERENCE = re.compile(r'\$\{(\w+)\}|\$(\w+)')


def stage_copy(instruction: str) -> str | None:
    """The stage a `COPY --from=<stage> / /code/` copies whole into /code.

    A --from that names a build arg fails: Docker does not expand one there, so the build would stop.

    Args:
        instruction (str): One logical instruction from the source stage.

    Returns:
        str | None: The stage name, or None when the instruction is not that exact form.
    """
    match = WHOLE_STAGE_COPY.fullmatch(instruction)
    if match is None:
        return None
    stage = match.group('stage')
    assert '$' not in stage, f'COPY --from cannot expand a build arg, select the stage with a FROM: {instruction}'
    return stage


def global_args(instructions: list[str]) -> dict[str, str]:
    """The ARGs declared before the first FROM, {name: default}: the only ones a FROM line may expand."""
    declared: dict[str, str] = {}
    for instruction in instructions:
        words = instruction.split()
        if words[0].upper() == 'FROM':
            break
        if words[0].upper() == 'ARG':
            name, _, default = words[1].partition('=')
            declared[name] = default
    return declared


def selected_stage(instructions: list[str], stage: str, build_args: dict[str, str]) -> str:
    """The scratch stage a whole-stage COPY ends up copying: `stage` itself, or the one its selector names.

    A selector is `FROM <name built from build args> AS <stage>` with no instruction of its own, so it is
    exactly the stage it expands to. Every arg it names must be declared before the first FROM (no other
    reaches a FROM line) and given in build_args, and the expansion must be a stage defined BEFORE the
    selector. A selector that adds an instruction would ship more than the stage it picks, so it fails, as
    does any parent that is neither scratch nor a build-arg selection.

    Args:
        instructions (list[str]): The Dockerfile's logical instructions.
        stage (str): The stage a COPY --from names.
        build_args (dict[str, str]): The image's build args.

    Returns:
        str: The name of the scratch stage whose contents the image receives.
    """
    headers = [
        index
        for index, instruction in enumerate(instructions)
        if instruction.startswith('FROM ') and instruction.split()[-1] == stage
    ]
    assert len(headers) == 1, f'expected one stage named {stage}, found {len(headers)}'
    index = headers[0]
    words = instructions[index].split()
    assert len(words) == 4 and words[2].upper() == 'AS', f'unrecognised FROM form: {instructions[index]}'
    parent = words[1]
    if parent == 'scratch':
        return stage
    assert not stage_instructions(instructions, stage), f'{stage} selects {parent} and adds instructions of its own'
    names = [braced or bare for braced, bare in ARG_REFERENCE.findall(parent)]
    assert names, f'{stage} is FROM {parent}: neither scratch nor a stage selected by build arg'
    declared = global_args(instructions)
    for name in names:
        assert name in declared, f'{stage} expands {name}, which is not declared before the first FROM'
        assert name in build_args, f'{stage} expands {name}, which the image is not built with'
        parent = parent.replace(f'${{{name}}}', build_args[name]).replace(f'${name}', build_args[name])
    earlier = {line.split()[-1] for line in instructions[:index] if line.startswith('FROM ') and ' AS ' in line}
    assert parent in earlier, f'{stage} selects {parent}, which is not a stage defined before it'
    return selected_stage(instructions[:index], parent, build_args)


def scratch_stage_copies(instructions: list[str], stage: str) -> dict[str, str]:
    """What a scratch stage puts in the image when copied whole into /code: {source: container path}.

    The stage must be `FROM scratch AS <stage>` (nothing but what it copies) and hold plain one-source
    COPYs only, read with copy_source, so a new form fails here exactly as it would in the source stage.

    Args:
        instructions (list[str]): The Dockerfile's logical instructions.
        stage (str): The stage name.

    Returns:
        dict[str, str]: Build-context source mapped to its path under /code; empty for an empty stage.
    """
    header = [
        instruction
        for instruction in instructions
        if instruction.split()[-1] == stage and instruction.startswith('FROM ')
    ]
    assert header == [f'FROM scratch AS {stage}'], f'{stage} is not one scratch stage: {header}'
    copies: dict[str, str] = {}
    for instruction in stage_instructions(instructions, stage):
        source = copy_source(instruction, {})
        target = instruction.split()[2]
        assert target.startswith('/') and '..' not in target, f'{stage} copies to {target}: {instruction}'
        copies[source] = str(IMAGE_ROOT / target.lstrip('/')).rstrip('/')
    return copies


def source_stage_copies(build_args: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    """(plain COPY sources, the revision stage's copies) of the source stage, for one image's build args.

    Every COPY or ADD of the stage is one or the other; any other form fails. Exactly one whole-stage
    COPY is expected: two would be a second way into the image this guard does not model. The stage it
    names is followed through its selector (selected_stage) under these build args.
    """
    instructions = dockerfile_instructions(DOCKERFILE.read_text(encoding='utf-8'))
    sources: list[str] = []
    stages: list[str] = []
    for instruction in stage_instructions(instructions, SOURCE_STAGE):
        if instruction.split()[0].upper() not in ('COPY', 'ADD'):
            continue
        stage = stage_copy(instruction)
        if stage is None:
            sources.append(copy_source(instruction, build_args))
        else:
            stages.append(stage)
    assert len(stages) == 1, f'{SOURCE_STAGE} copies {len(stages)} whole stages, expected one: {stages}'
    return sources, scratch_stage_copies(instructions, selected_stage(instructions, stages[0], build_args))


def dockerfile_copy_directories() -> set[str]:
    """The directory sources of the source stage's COPY lines, resolved for the data_ingest image."""
    sources, _ = source_stage_copies(INGEST_BUILD_ARGS)
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


def test_the_whole_stage_copy_parser_reads_one_form_only():
    # The one --from form the source stage may use is read; every near miss is not that form (None, so it
    # falls to copy_source and fails there), and a --from naming a build arg fails outright, because
    # Docker stops the build on it ("variable expansion is not supported for --from", tj-7zjov1.95).
    assert stage_copy('COPY --from=migrations_selected / /code/') == 'migrations_selected'
    assert stage_copy('COPY ./server/common /code/common') is None
    for near_miss in (
        'COPY --from=other /code /code',
        'COPY --from=other / /code',
        'COPY --from=other /migrations /code/migrations',
        'COPY --from=other --chown=appuser / /code/',
    ):
        assert stage_copy(near_miss) is None, near_miss
    for expanded in (
        'COPY --from=migrations_${SERVICE_PATH}_${SERVICE_NAME} / /code/',
        'COPY --from=migrations_$SERVICE_PATH / /code/',
        'COPY --from=migrations_${OTHER} / /code/',
    ):
        with pytest.raises(AssertionError):
            stage_copy(expanded)


SELECTOR = 'FROM migrations_${SERVICE_PATH}_${SERVICE_NAME} AS migrations_selected'
SELECTOR_DOCKERFILE = [
    'ARG SERVICE_PATH=none',
    'ARG SERVICE_NAME=none',
    'FROM debian AS base',
    'FROM scratch AS migrations_data_store',
    'COPY ./a.ini /a.ini',
    'FROM scratch AS migrations_data_ingest',
    SELECTOR,
    'FROM base AS service_build_image',
]
STORE_ARGS = {'SERVICE_PATH': 'data', 'SERVICE_NAME': 'store'}


def test_the_selector_reader_follows_a_global_arg_from_to_the_earlier_scratch_stage():
    # The good shape resolves per service, and a stage that is scratch already is itself.
    assert selected_stage(SELECTOR_DOCKERFILE, 'migrations_selected', STORE_ARGS) == 'migrations_data_store'
    assert selected_stage(SELECTOR_DOCKERFILE, 'migrations_selected', INGEST_BUILD_ARGS) == 'migrations_data_ingest'
    assert selected_stage(SELECTOR_DOCKERFILE, 'migrations_data_store', STORE_ARGS) == 'migrations_data_store'
    assert global_args(SELECTOR_DOCKERFILE) == {'SERVICE_PATH': 'none', 'SERVICE_NAME': 'none'}

    selector_at = SELECTOR_DOCKERFILE.index(SELECTOR)
    without_global_name = [line for line in SELECTOR_DOCKERFILE if line != 'ARG SERVICE_NAME=none']
    # The ARG only inside a stage: a FROM cannot see it.
    in_stage_only = [*without_global_name[:2], 'ARG SERVICE_NAME=none', *without_global_name[2:]]
    selector_first = [
        *SELECTOR_DOCKERFILE[:3],
        SELECTOR,
        *[line for line in SELECTOR_DOCKERFILE[3:] if line != SELECTOR],
    ]
    with_body = [
        *SELECTOR_DOCKERFILE[: selector_at + 1],
        'COPY ./extra /extra',
        *SELECTOR_DOCKERFILE[selector_at + 1 :],
    ]
    for bad, args in (
        (without_global_name, STORE_ARGS),
        (in_stage_only, STORE_ARGS),
        (selector_first, STORE_ARGS),
        (with_body, STORE_ARGS),
        (SELECTOR_DOCKERFILE, {'SERVICE_PATH': 'data'}),
        (SELECTOR_DOCKERFILE, {'SERVICE_PATH': 'data', 'SERVICE_NAME': 'web'}),
        ([*SELECTOR_DOCKERFILE[:3], 'FROM debian AS migrations_selected'], STORE_ARGS),
    ):
        with pytest.raises(AssertionError):
            selected_stage(bad, 'migrations_selected', args)


def test_the_ingest_image_selects_its_own_revision_stage():
    # The real Dockerfile, under this image's build args: the selector names the empty ingest stage.
    instructions = dockerfile_instructions(DOCKERFILE.read_text(encoding='utf-8'))
    copied = [stage_copy(line) for line in stage_instructions(instructions, SOURCE_STAGE)]
    assert [stage for stage in copied if stage is not None] == ['migrations_selected'], copied
    assert selected_stage(instructions, 'migrations_selected', INGEST_BUILD_ARGS) == 'migrations_data_ingest'


def test_the_scratch_stage_reader_refuses_a_stage_that_is_not_plain_scratch_copies():
    good = ['FROM scratch AS revisions', 'COPY ./a.ini /a.ini', 'COPY ./tree /tree', 'FROM base AS next']
    assert scratch_stage_copies(good, 'revisions') == {'a.ini': '/code/a.ini', 'tree': '/code/tree'}
    assert scratch_stage_copies(['FROM scratch AS empty', 'FROM base AS next'], 'empty') == {}
    for bad in (
        ['FROM debian AS revisions', 'COPY ./a /a'],
        ['FROM scratch AS revisions', 'COPY --from=elsewhere /a /a'],
        ['FROM scratch AS revisions', 'ADD ./a /a'],
        ['FROM scratch AS revisions', 'COPY ./a relative/a'],
    ):
        with pytest.raises(AssertionError):
            scratch_stage_copies(bad, 'revisions')


def test_the_ingest_image_takes_nothing_from_its_revision_stage():
    # migrations_data_ingest is empty by design (bead tj-7zjov1.10): anything put there would ship in
    # the data_ingest image outside PRODUCTION_ROOTS, unscanned.
    _, revisions = source_stage_copies(INGEST_BUILD_ARGS)
    assert revisions == {}, f'the data_ingest image takes {revisions} from its revision stage'


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
