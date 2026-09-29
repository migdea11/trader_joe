"""The store app's OpenAPI lists no component that nothing references, and loses nothing that is.

WHY THIS FILE EXISTS (validator, gating tj-1b3aer). After 999fc4c moved expiry_type/update_type to
field-local string-enum annotations, FastAPI still emitted ``components.schemas.ExpiryType`` and
``.UpdateType`` as integer enums that no ``$ref`` reached -- leftovers from the Pydantic core schema.
The private repo's typed SDK is generated from this document, and generators emit every component, so
the SDK would carry an integer ExpiryType the API never accepts. 2eabc20 wraps ``app.openapi`` with
``prune_unreferenced_schemas``, a reachability closure rooted at every ``$ref`` outside
``components.schemas``.

Two halves are pinned here:

  1. THE SERVED DOCUMENT. ``GET /openapi.json`` -- what a generator actually fetches -- has no
     component the paths do not reach, walking the whole document rather than naming a pair; and,
     against a freshly generated UNPRUNED document, the paths are identical, every kept component is
     identical, and no ``$ref`` dangles. That last trio is the "nothing referenced was lost" half: the
     refs outside components are unchanged because the paths are, the refs inside kept components are
     unchanged because those components are, so a referenced component that went missing would show
     up as a dangling ``$ref``. It is deliberately not a list of expected component names, which would
     go red on every new model.
  2. THE FUNCTION. ``prune_unreferenced_schemas`` on hand-built documents: a component reached only
     through another component is kept (the closure), an unreferenced one is dropped, and a document
     with no components is left alone.

The reachability oracle below is intentionally a different mechanism from the production walk -- a
regex over the serialized JSON rather than a recursive dict walk -- so the test is not the code under
test copied inline.
"""

import copy
import json
import re

import pytest
from fastapi.openapi.utils import get_openapi
from fastapi.testclient import TestClient

from data.store.app.main import app
from data.store.app.openapi_pruning import prune_unreferenced_schemas


pytestmark = pytest.mark.data_store

_SCHEMA_REF = re.compile(r'"\$ref":\s*"#/components/schemas/([^"]+)"')


def _referenced_names(fragment: object) -> set[str]:
    """Every component-schema name a ``$ref`` inside `fragment` points at, found in its JSON text.

    Args:
        fragment: Any JSON-serializable piece of an OpenAPI document.

    Returns:
        set[str]: The referenced component names.
    """
    return set(_SCHEMA_REF.findall(json.dumps(fragment)))


def _reachable_components(spec: dict) -> set[str]:
    """The component names reachable from every ``$ref`` outside ``components.schemas``, transitively.

    Args:
        spec: An OpenAPI document.

    Returns:
        set[str]: The names of the components some chain of ``$ref`` from outside the schemas reaches.
    """
    schemas = spec.get('components', {}).get('schemas', {})
    outside = {key: value for key, value in spec.items() if key != 'components'}
    outside['components'] = {key: value for key, value in spec.get('components', {}).items() if key != 'schemas'}
    reached: set[str] = set()
    pending = _referenced_names(outside)
    while pending:
        name = pending.pop()
        if name in reached or name not in schemas:
            continue
        reached.add(name)
        pending |= _referenced_names(schemas[name])
    return reached


def _served_openapi() -> dict:
    """The document the store app serves at its OpenAPI URL, as a client generator would fetch it.

    Returns:
        dict: The parsed ``GET /openapi.json`` body.
    """
    # Not entered as a context manager, so the lifespan (Postgres, Kafka) never starts.
    response = TestClient(app).get(app.openapi_url)
    assert response.status_code == 200, response.text
    return response.json()


def _unpruned_openapi() -> dict:
    """A freshly generated store OpenAPI with no pruning, for comparison.

    Built with FastAPI's own ``get_openapi`` over the app's routes, because the app's cached document
    is the pruned one: the wrapper prunes FastAPI's cached dict in place.

    Returns:
        dict: The document as FastAPI would serve it without the tj-1b3aer wrapper.
    """
    return get_openapi(
        title=app.title,
        version=app.version,
        openapi_version=app.openapi_version,
        routes=app.routes,
        separate_input_output_schemas=app.separate_input_output_schemas,
    )


# ---------------------------------------------------------------------------------------------
# The served document
# ---------------------------------------------------------------------------------------------


def test_the_served_store_openapi_has_no_unreferenced_component():
    """Every component in ``GET /openapi.json`` is reached by a ``$ref`` chain from outside the schemas."""
    spec = _served_openapi()
    components = set(spec.get('components', {}).get('schemas', {}))

    orphans = components - _reachable_components(spec)

    assert not orphans, f'the store OpenAPI lists components nothing references: {sorted(orphans)}'


def test_the_served_store_openapi_has_no_integer_expiry_or_update_type_component():
    """The bead's named acceptance: no integer ExpiryType/UpdateType component is published.

    The walk above already implies this; it is stated by name because it is the defect the SDK would
    otherwise inherit. A string-enum component under either name would not trip it.
    """
    schemas = _served_openapi().get('components', {}).get('schemas', {})

    integer_enums = [name for name in ('ExpiryType', 'UpdateType') if schemas.get(name, {}).get('type') == 'integer']

    assert not integer_enums, f'the store OpenAPI still publishes integer enum components {integer_enums}'


def test_pruning_leaves_the_paths_and_every_kept_component_unchanged():
    """Against an unpruned document: paths are identical, and each kept component is byte-identical."""
    served = _served_openapi()
    unpruned = _unpruned_openapi()
    served_schemas = served.get('components', {}).get('schemas', {})
    unpruned_schemas = unpruned.get('components', {}).get('schemas', {})

    assert served['paths'] == json.loads(json.dumps(unpruned['paths'])), 'pruning changed the documented paths'
    assert set(served_schemas) <= set(unpruned_schemas), (
        f'pruning invented components: {sorted(set(served_schemas) - set(unpruned_schemas))}'
    )
    changed = [
        name for name in served_schemas if served_schemas[name] != json.loads(json.dumps(unpruned_schemas[name]))
    ]
    assert not changed, f'pruning altered kept components: {changed}'


def test_no_ref_in_the_served_store_openapi_dangles():
    """Every ``$ref`` in the served document names a component that is still present.

    With the paths and kept components unchanged (the test above), this is what says no referenced
    component was dropped: a dropped one would leave its ``$ref`` pointing at nothing.
    """
    spec = _served_openapi()
    present = set(spec.get('components', {}).get('schemas', {}))

    dangling = _referenced_names(spec) - present

    assert not dangling, f'the store OpenAPI references components it does not define: {sorted(dangling)}'


# ---------------------------------------------------------------------------------------------
# prune_unreferenced_schemas
# ---------------------------------------------------------------------------------------------


def _ref(name: str) -> dict:
    """A ``$ref`` to one component schema.

    Args:
        name: The component name.

    Returns:
        dict: ``{'$ref': '#/components/schemas/<name>'}``.
    """
    return {'$ref': f'#/components/schemas/{name}'}


def _document(schemas: dict, *, root: str | None = 'Root', **components: dict) -> dict:
    """A minimal OpenAPI document with one operation whose 200 response refs `root`.

    Args:
        schemas: The ``components.schemas`` mapping.
        root: The component the operation references, or None for an operation with no ``$ref``.
        **components: Other ``components`` sections, such as ``responses``.

    Returns:
        dict: The document.
    """
    body = {'content': {'application/json': {'schema': _ref(root) if root else {'type': 'string'}}}}
    return {
        'openapi': '3.1.0',
        'info': {'title': 't', 'version': '0'},
        'paths': {'/thing': {'get': {'responses': {'200': {'description': 'ok', **body}}}}},
        'components': {'schemas': schemas, **components},
    }


def test_prune_keeps_a_component_reached_only_through_other_components():
    """Root -> Middle -> Leaf, three deep: the whole chain survives, so the prune is a closure.

    Three deep rather than two so that a prune that follows exactly one extra hop still goes red.
    """
    spec = _document(
        {
            'Root': {'type': 'object', 'properties': {'middle': _ref('Middle')}},
            'Middle': {'type': 'array', 'items': _ref('Leaf')},
            'Leaf': {'type': 'string'},
        }
    )

    prune_unreferenced_schemas(spec)

    assert set(spec['components']['schemas']) == {'Root', 'Middle', 'Leaf'}


def test_prune_drops_an_unreferenced_component_and_keeps_the_rest_as_they_were():
    """An orphan is dropped; the referenced component and the paths are untouched."""
    spec = _document({'Root': {'type': 'string'}, 'Orphan': {'type': 'integer', 'enum': [1, 2]}})
    before = copy.deepcopy(spec)

    prune_unreferenced_schemas(spec)

    assert spec['components']['schemas'] == {'Root': {'type': 'string'}}
    assert spec['paths'] == before['paths']


def test_prune_drops_orphans_that_only_reference_each_other():
    """A cycle nothing outside reaches is still unreferenced: roots are refs outside the schemas."""
    spec = _document(
        {
            'Root': {'type': 'string'},
            'CycleA': {'type': 'object', 'properties': {'b': _ref('CycleB')}},
            'CycleB': {'type': 'object', 'properties': {'a': _ref('CycleA')}},
        }
    )

    prune_unreferenced_schemas(spec)

    assert set(spec['components']['schemas']) == {'Root'}


def test_prune_roots_include_other_component_sections():
    """A schema referenced only from ``components.responses`` is reached, not dropped."""
    spec = _document(
        {'Root': {'type': 'string'}, 'ErrorBody': {'type': 'object'}},
        responses={'Error': {'description': 'e', 'content': {'application/json': {'schema': _ref('ErrorBody')}}}},
    )

    prune_unreferenced_schemas(spec)

    assert set(spec['components']['schemas']) == {'Root', 'ErrorBody'}


def test_prune_removes_the_schemas_section_when_nothing_is_referenced():
    """With no ``$ref`` anywhere, ``components.schemas`` goes, as the docstring states."""
    spec = _document({'Orphan': {'type': 'string'}}, root=None)

    prune_unreferenced_schemas(spec)

    assert 'schemas' not in spec['components']


@pytest.mark.parametrize(
    'spec',
    [
        {'openapi': '3.1.0', 'info': {'title': 't', 'version': '0'}, 'paths': {}},
        {'openapi': '3.1.0', 'info': {'title': 't', 'version': '0'}, 'paths': {}, 'components': {}},
        {'openapi': '3.1.0', 'paths': {}, 'components': {'responses': {'E': {'description': 'e'}}}},
    ],
    ids=['no-components', 'empty-components', 'components-without-schemas'],
)
def test_prune_leaves_a_document_without_schemas_alone(spec: dict):
    """No ``components`` or no ``components.schemas``: no crash, and the document is unchanged.

    Args:
        spec: A document with nothing to prune.
    """
    before = copy.deepcopy(spec)

    prune_unreferenced_schemas(spec)

    assert spec == before
