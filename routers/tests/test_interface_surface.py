import importlib
import inspect
import pkgutil
import sys
from enum import Enum
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi import APIRouter
from fastapi.routing import APIRoute
from pydantic import BaseModel

from common.kafka.kafka_rpc_factory import KafkaRpcFactory


# THE INTERFACE MANIFEST (tj-ru24i2, ADR tj-fdb9gz).
#
# This file enumerates every interface the three router packages expose -- from the LIVE CODE,
# by importing them -- and asserts that the enumerated set EQUALS the committed manifest under
# routers/tests/interface_manifest/. Adding a route therefore also costs a line in the manifest,
# in the same diff. THAT FRICTION IS THE POINT, and it is a ruling, not an accident: the user was
# offered the cheaper version -- enumerate the routes and assert nothing against a manifest --
# had the trade-off re-explained on 2026-09-23, and ruled "Keep the manifest". Enumeration without
# the manifest keeps the smoke test and loses the only thing that stops the interface inventory
# drifting silently out of date. If the friction is ever judged not worth it, THAT is the fallback;
# there is no third option to invent.
#
# The manifest doubles as the machine-readable interface inventory that tj-dwkjg9 (S6) builds the
# router beads from, which is why each line carries the implementing file and symbol and the
# request/response schemas by fully qualified name: S6 must not have to re-read the code.
#
# THERE IS DELIBERATELY NO REGENERATE COMMAND, and this file does not grow one. A generator would
# turn "the test went red" into a keystroke, which is exactly the friction the ruling asked for.
# Edit the manifest by hand; the failure message below names the lines to add or remove verbatim.

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_DIR = Path(__file__).resolve().parent / 'interface_manifest'

# One manifest file per component, named for the component marker. The split is not cosmetic: it
# keeps a data_store change out of data_ingest's diff, and it means a component whose manifest
# file goes missing FAILS (FileNotFoundError) rather than quietly comparing against nothing.
COMPONENT_PACKAGES = {
    'common': 'routers.common',
    'data_store': 'routers.data_store',
    'data_ingest': 'routers.data_ingest',
}

# kind | address | file | symbol | request | response | touches
FIELD_COUNT = 7
SEPARATOR = ' | '
EMPTY = '-'

# http          a FastAPI route registered on a module-scope APIRouter while the module body ran.
# rpc           a handler registered through KafkaRpcFactory.add_server() while the module body ran.
# unbound-path  a path declared in an interface enum with NO route bound to it at import time.
#
# `unbound-path` exists because the import-time surface is not the whole declared surface, and the
# gap was invisible before this file. routers/common/latency.py builds its APIRouter INSIDE
# initialize_latency_client()/initialize_latency_server(), so /latency and /latency_internal are
# registered only when LATENCY_TEST_ENABLED is set and those functions are called with a live Kafka
# factory -- nothing a no-external test may do. routers/data_ingest declares a REST path and
# registers no route for it at all. Recording those as their own kind keeps them in the inventory
# S6 reads, and keeps them under the same equality assertion as everything else: implement one, and
# the `unbound-path` line has to become an `http` line in the same diff.
KINDS = ('http', 'rpc', 'unbound-path')


def _module_relpath(module: ModuleType) -> str:
    """Return the module's path relative to the repository root.

    Args:
        module (ModuleType): Module to locate.

    Returns:
        str: Repository-relative POSIX path.
    """
    module_file = getattr(module, '__file__', None)
    if module_file is None:
        raise AssertionError(f'{module.__name__} has no __file__, so its interfaces cannot be attributed to a file')
    return Path(module_file).resolve().relative_to(REPO_ROOT).as_posix()


def _defining_module(function: Any) -> ModuleType:
    """Return the module a function's body was defined in.

    The endpoint itself is asked, never the module the router object happens to be reachable
    from: a router that is imported into a second module would otherwise be attributed twice,
    to two different files.

    Args:
        function (Any): Endpoint or handler function.

    Returns:
        ModuleType: Module that defines it.
    """
    module = sys.modules.get(function.__module__)
    if module is None:
        raise AssertionError(f'{function.__qualname__} claims module {function.__module__}, which is not imported')
    return module


def _unwrap_annotated(annotation: Any) -> Any:
    """Strip Annotated[...] metadata down to the underlying type.

    Args:
        annotation (Any): Possibly annotated type.

    Returns:
        Any: The underlying type.
    """
    while hasattr(annotation, '__metadata__'):
        annotation = annotation.__origin__
    return annotation


def _render_type(annotation: Any) -> str:
    """Render a type as a fully qualified, stable string.

    Args:
        annotation (Any): Type, typing construct, or None.

    Returns:
        str: Fully qualified name, or EMPTY when there is no type.
    """
    annotation = _unwrap_annotated(annotation)
    if annotation is None or annotation is type(None):
        return EMPTY
    if annotation is inspect.Parameter.empty:
        return 'unannotated'
    origin = getattr(annotation, '__origin__', None)
    args = getattr(annotation, '__args__', ())
    if origin is not None and args:
        rendered_args = ', '.join(_render_type(arg) for arg in args)
        return f'{_render_type(origin)}[{rendered_args}]'
    if isinstance(annotation, type):
        if annotation.__module__ == 'builtins':
            return annotation.__qualname__
        return f'{annotation.__module__}.{annotation.__qualname__}'
    return str(annotation)


def _join(values: list[str]) -> str:
    """Render a field holding zero or more type names.

    Sorted and de-duplicated: this is an inventory of what an interface takes, not a call
    signature, so reordering two parameters must not turn the manifest red for nothing.

    Args:
        values (list[str]): Rendered type names.

    Returns:
        str: Comma-separated field value, or EMPTY.
    """
    return ', '.join(sorted(set(values))) or EMPTY


def _split_parameters(function: Any) -> tuple[str, str]:
    """Split a callable's parameters into its request schemas and what it touches.

    A parameter annotated with a Pydantic model IS the request shape -- every cross-boundary
    schema in this repository is one. Everything else is injected plumbing: an AsyncSession says
    the route reaches Postgres, KafkaRpcFactory.RpcClients says it reaches Kafka, and ADR
    tj-fdb9gz records those outbound boundaries as the "what it touches" field of the interface
    that crosses them rather than as interfaces of their own, because that field is what decides
    the tier of the eventual test.

    Args:
        function (Any): Endpoint or handler function.

    Returns:
        tuple[str, str]: The request field and the touches field.
    """
    request: list[str] = []
    touches: list[str] = []
    for parameter in inspect.signature(function).parameters.values():
        annotation = _unwrap_annotated(parameter.annotation)
        rendered = _render_type(annotation)
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            request.append(rendered)
        else:
            touches.append(rendered)
    return _join(request), _join(touches)


def _line(kind: str, address: str, file: str, symbol: str, request: str, response: str, touches: str) -> str:
    """Render one manifest line.

    Args:
        kind (str): One of KINDS.
        address (str): Method and path, or RPC topic, or declared path.
        file (str): Repository-relative implementing file.
        symbol (str): Implementing symbol.
        request (str): Request schemas, fully qualified.
        response (str): Response schema, fully qualified.
        touches (str): Injected dependencies the interface reaches through.

    Returns:
        str: The canonical line.
    """
    fields = (kind, address, file, symbol, request, response, touches)
    if len(fields) != FIELD_COUNT:
        raise AssertionError('FIELD_COUNT and _line() disagree')
    return SEPARATOR.join(fields)


def _component_modules(component: str) -> list[ModuleType]:
    """Import every module of a component's router package.

    Importing IS the measurement. routers/data_ingest/get_dataset_request.py registers its Kafka
    RPC handler with a decorator at module scope, so the handler is enumerable only if that
    registration really ran -- and an import that raises fails this test rather than yielding a
    shorter list, which is why nothing here catches ImportError.

    Args:
        component (str): Component name.

    Returns:
        list[ModuleType]: The package and every module under it.
    """

    def _reraise(name: str) -> None:
        raise

    package_name = COMPONENT_PACKAGES[component]
    package = importlib.import_module(package_name)
    modules = [
        importlib.import_module(info.name)
        for info in pkgutil.walk_packages(package.__path__, prefix=f'{package_name}.', onerror=_reraise)
    ]
    if not modules:
        # Guards the vacuous pass: an empty walk enumerates nothing, and nothing compares equal to
        # nothing only because the manifest happens to be non-empty. Say so here instead.
        raise AssertionError(f'{package_name} yielded no modules to enumerate')
    return [package, *modules]


def _http_lines(component: str, modules: list[ModuleType]) -> tuple[set[str], set[str]]:
    """Enumerate the FastAPI routes registered on the component's module-scope routers.

    Args:
        component (str): Component name.
        modules (list[ModuleType]): The component's imported modules.

    Returns:
        tuple[set[str], set[str]]: The manifest lines, and the set of bound paths.
    """
    lines: set[str] = set()
    bound_paths: set[str] = set()
    package_name = COMPONENT_PACKAGES[component]
    for module in modules:
        for router in vars(module).values():
            if not isinstance(router, APIRouter):
                continue
            for route in router.routes:
                if not isinstance(route, APIRoute):
                    continue
                defining_module = _defining_module(route.endpoint)
                if not defining_module.__name__.startswith(package_name):
                    raise AssertionError(
                        f'{route.path} is registered on a {component} router but implemented in '
                        f'{defining_module.__name__}, so its component is ambiguous'
                    )
                request, touches = _split_parameters(route.endpoint)
                response = _render_type(route.response_model)
                bound_paths.add(route.path)
                for method in route.methods:
                    lines.add(
                        _line(
                            'http',
                            f'{method} {route.path}',
                            _module_relpath(defining_module),
                            route.endpoint.__qualname__,
                            request,
                            response,
                            touches,
                        )
                    )
    return lines, bound_paths


def _rpc_lines(component: str, modules: list[ModuleType]) -> set[str]:
    """Enumerate the Kafka RPC handlers registered on the component's module-scope factories.

    Reaches into KafkaRpcFactory._rpc_servers because the factory exposes no public view of what
    has been registered; data/ingest/tests/test_app_import.py already does the same. A public
    accessor would be an improvement to the factory, not to this file.

    Args:
        component (str): Component name.
        modules (list[ModuleType]): The component's imported modules.

    Returns:
        set[str]: The manifest lines.
    """
    lines: set[str] = set()
    package_name = COMPONENT_PACKAGES[component]
    for module in modules:
        for factory in vars(module).values():
            if not isinstance(factory, KafkaRpcFactory):
                continue
            for server in factory._rpc_servers:
                handler = server._rpc_function
                defining_module = _defining_module(handler)
                if not defining_module.__name__.startswith(package_name):
                    raise AssertionError(
                        f'a {component} RPC factory registered a handler implemented in '
                        f'{defining_module.__name__}, so its component is ambiguous'
                    )
                _, touches = _split_parameters(handler)
                lines.add(
                    _line(
                        'rpc',
                        server.endpoint.topic.value,
                        _module_relpath(defining_module),
                        handler.__qualname__,
                        _render_type(server.endpoint.request_model),
                        _render_type(server.endpoint.response_model),
                        touches,
                    )
                )
    return lines


def _unbound_path_lines(modules: list[ModuleType], bound_paths: set[str]) -> set[str]:
    """Enumerate paths declared in an interface enum that no import-time route serves.

    An interface enum is a module-scope Enum, defined in that module, all of whose members are
    strings beginning with '/'. Matching is by path and not by method, because a member carries
    no method: DELETE /store/{id} therefore counts GET_STORE_ASSET_DATASET_BY_ID as bound too.
    The coarseness is the honest limit of what the declaration says.

    Args:
        modules (list[ModuleType]): The component's imported modules.
        bound_paths (set[str]): Paths that an import-time route serves.

    Returns:
        set[str]: The manifest lines.
    """
    lines: set[str] = set()
    for module in modules:
        for name, declared in vars(module).items():
            if not (isinstance(declared, type) and issubclass(declared, Enum)):
                continue
            if declared.__module__ != module.__name__ or not declared.__members__:
                continue
            members = list(declared)
            if not all(isinstance(member.value, str) and member.value.startswith('/') for member in members):
                continue
            for member in members:
                if member.value in bound_paths:
                    continue
                lines.add(
                    _line(
                        'unbound-path',
                        member.value,
                        _module_relpath(module),
                        f'{name}.{member.name}',
                        EMPTY,
                        EMPTY,
                        EMPTY,
                    )
                )
    return lines


def enumerate_interface_surface(component: str) -> set[str]:
    """Enumerate one component's whole interface surface from the live code.

    Args:
        component (str): Component name.

    Returns:
        set[str]: Manifest lines, as the code says they should be.
    """
    modules = _component_modules(component)
    http, bound_paths = _http_lines(component, modules)
    return http | _rpc_lines(component, modules) | _unbound_path_lines(modules, bound_paths)


def load_manifest(component: str) -> list[str]:
    """Read and validate one component's committed manifest.

    Every structural complaint is raised here rather than tested separately: a malformed,
    unsorted, duplicated or empty manifest makes its component's assertion ERROR with the file
    and line number, which is louder than a second test nobody reads.

    Args:
        component (str): Component name.

    Returns:
        list[str]: Canonical manifest lines, in file order.
    """
    path = MANIFEST_DIR / f'{component}.manifest'
    lines: list[str] = []
    for number, raw in enumerate(path.read_text().splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith('#'):
            continue
        fields = [field.strip() for field in stripped.split('|')]
        where = f'{path.name}:{number}'
        if len(fields) != FIELD_COUNT:
            raise ValueError(f'{where}: expected {FIELD_COUNT} fields separated by "|", found {len(fields)}')
        if not all(fields):
            raise ValueError(f'{where}: a field is empty; write "{EMPTY}" where an entry has no value')
        if fields[0] not in KINDS:
            raise ValueError(f'{where}: unknown kind {fields[0]!r}, expected one of {", ".join(KINDS)}')
        lines.append(SEPARATOR.join(fields))
    if not lines:
        raise ValueError(f'{path.name} declares no interfaces; an empty manifest asserts nothing')
    duplicates = sorted({line for line in lines if lines.count(line) > 1})
    if duplicates:
        raise ValueError(f'{path.name} repeats a line:\n' + '\n'.join(duplicates))
    if lines != sorted(lines):
        raise ValueError(f'{path.name} is not sorted; keep it sorted so a new interface is a one-line diff')
    return lines


def _describe(component: str, missing: set[str], unexpected: set[str]) -> str:
    """Build the failure message, as lines to paste into the manifest or delete from it.

    Args:
        component (str): Component name.
        missing (set[str]): Live interfaces the manifest does not declare.
        unexpected (set[str]): Manifest lines no live interface matches.

    Returns:
        str: The message.
    """
    report = [f'{component} interface surface does not match {component}.manifest.']
    if missing:
        report.append('\nIn the code, NOT in the manifest -- add these lines (keep the file sorted):')
        report.extend(f'  {line}' for line in sorted(missing))
    if unexpected:
        report.append('\nIn the manifest, NOT in the code -- remove these lines, or restore the interface:')
        report.extend(f'  {line}' for line in sorted(unexpected))
    return '\n'.join(report)


# Marked per component, never `routers`: routers is a LAYER inside every component, not a
# component of its own, and pytest.ini declares no marker for it. Parametrizing rather than
# splitting the file into three modules keeps one copy of the enumerator honest for all three.
@pytest.mark.parametrize(
    'component',
    [
        pytest.param('common', marks=pytest.mark.common),
        pytest.param('data_store', marks=pytest.mark.data_store),
        pytest.param('data_ingest', marks=pytest.mark.data_ingest),
    ],
)
def test_the_interface_surface_equals_the_committed_manifest(component: str):
    enumerated = enumerate_interface_surface(component)
    declared = set(load_manifest(component))
    assert enumerated == declared, _describe(component, enumerated - declared, declared - enumerated)
