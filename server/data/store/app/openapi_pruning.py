"""Prune OpenAPI components that no ``$ref`` reaches.

FastAPI's ``get_openapi()`` walks Pydantic core schemas to build ``components.schemas``, so a
component can land in the document because the underlying model still carries it in its core
schema, independent of whether any operation still resolves a ``$ref`` to it. tj-1b3aer: after
999fc4c switched the store's request/response fields to field-local string-enum annotations
(``ExpiryTypeByName`` / ``UpdateTypeByName``), FastAPI kept ``ExpiryType`` and ``UpdateType`` in
``components.schemas`` as their original integer enums -- leftovers from
``common.enums.data_stock``'s core schema that nothing in the served document points at anymore.
Left in place, a stale component reaches every client generated from this document, including the
private repo's typed SDK, which emits one type per component regardless of whether a route uses it.

This module is general on purpose: it drops any component nothing reaches, not a hard-coded pair,
so a future core-schema leftover doesn't need a second pass here.
"""

from typing import Any


def _collect_schema_refs(node: Any) -> set[str]:
    """Component-schema names referenced by ``$ref`` anywhere inside `node`.

    Args:
        node: Any JSON-like fragment of an OpenAPI document -- a dict, a list, or a scalar.

    Returns:
        set[str]: The referenced component names, e.g. ``{'ExpiryType'}`` for a
            ``{'$ref': '#/components/schemas/ExpiryType'}`` found at any depth of `node`.
    """
    refs: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == '$ref' and isinstance(value, str) and value.startswith('#/components/schemas/'):
                refs.add(value.rsplit('/', 1)[1])
            else:
                refs |= _collect_schema_refs(value)
    elif isinstance(node, list):
        for item in node:
            refs |= _collect_schema_refs(item)
    return refs


def prune_unreferenced_schemas(spec: dict[str, Any]) -> None:
    """Drop every ``components.schemas`` entry no ``$ref`` in `spec` reaches, in place.

    Reachability starts from every ``$ref`` found outside ``components.schemas`` itself --
    paths, parameters, request and response bodies, and any other ``components`` section such
    as ``responses`` or ``parameters`` -- then closes over refs found inside the schemas that
    are kept, since one schema may itself ``$ref`` another. A schema the closure never reaches
    is exactly the kind of core-schema leftover tj-1b3aer found: dropping it keeps the served
    document, and every client generated from it, honest about what the API actually accepts
    and returns.

    Args:
        spec: An OpenAPI document as `FastAPI.openapi()` builds it. Mutated in place:
            `components.schemas` is replaced with the reachable subset, or removed
            entirely if no schema is reachable. Left untouched if `spec` has no
            `components.schemas` to prune.
    """
    components = spec.get('components')
    if not isinstance(components, dict):
        return
    schemas = components.get('schemas')
    if not isinstance(schemas, dict):
        return

    roots = _collect_schema_refs({key: value for key, value in spec.items() if key != 'components'})
    roots |= _collect_schema_refs({key: value for key, value in components.items() if key != 'schemas'})

    reachable: set[str] = set()
    frontier = roots & schemas.keys()
    while frontier:
        reachable |= frontier
        newly_seen = _collect_schema_refs({name: schemas[name] for name in frontier}) & schemas.keys()
        frontier = newly_seen - reachable

    pruned = {name: definition for name, definition in schemas.items() if name in reachable}
    if pruned:
        components['schemas'] = pruned
    else:
        del components['schemas']
