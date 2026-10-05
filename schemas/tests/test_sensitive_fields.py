"""The sensitive fields on the schema models stay out of rendered text (tj-w6bpjm, layer M2).

Design: tj-vhboky.41 ADDENDUM 1, D3. Sensitive values are redacted where they are RENDERED, and
everything else stays in the logs. The user's list (tj-vhboky.45) is `owner` and nothing else for
now. Layer M2 is the pydantic half: common/sensitive.py's SensitiveStr and OptionalSensitiveStr
carry Field(repr=False), and every owner declaration in schemas/ uses one of them.

What is pinned, for each of the four declaration sites:
- owner is absent from repr() and str(), which is what an f-string of a model in a log line
  renders (StoreAssetDatasetBody's validate_fields logs `{self}` at debug);
- model_dump and model_dump_json still carry the value unchanged, because the open GET and the
  request bodies must still serialise it;
- the JSON schema is identical to the one the plain `str` / `str | None = None` declaration
  produced, so the OpenAPI contract did not move;
- importing the schema modules and building their JSON schemas raises no pydantic warning. The
  form this guards against, `SensitiveStr | None`, warns and silently keeps the value in the repr.

And for every model anywhere in schemas/: a field named on the sensitive list has repr=False,
so a new model declaring owner as a plain str fails here.

Layer M1, the SQL bind-parameter half (RedactedStr, SensitiveString), is not built yet: its
asyncpg spike moved to host run tj-vhboky.14. Nothing here covers it.
"""

import importlib
import inspect
import json
import pkgutil
import subprocess
import sys
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel, create_model

import schemas
from common.enums.data_stock import ExpiryType, UpdateType
from common.tests.roots import SERVER_ROOT
from schemas.data_ingest.get_dataset_request import BaseGetDatasetRequest
from schemas.data_store.asset_dataset_store import (
    AssetDatasetStoreDelete,
    StoreAssetDatasetBody,
    StoreAssetDatasetQuery,
)


# THE SERVER ROOT (tj-iontkq.2): the only use is the cwd of a fresh interpreter that imports
# common.sensitive and the schemas.* modules, so it must be the IMPORT root. SERVER_ROOT.

# The user's list (tj-vhboky.45). A field added to it is checked on every model in schemas/.
SENSITIVE_FIELDS = ('owner',)

# Distinctive enough that finding it in a repr cannot be a coincidence.
SECRET = 'owner-sentinel-5b1f9e'

DATASET_ID = UUID('00000000-0000-0000-0000-000000000001')
WHEN = datetime(2026, 1, 1, tzinfo=UTC)

_REQUIRED = ...

# (model, minimal payload carrying the secret, the declaration owner had before the alias, its
# default). The old declaration is what the JSON schema is compared against. Two use the required
# alias and two the optional one, so both aliases are covered.
DECLARATION_CASES = [
    pytest.param(
        StoreAssetDatasetBody,
        {'owner': SECRET, 'source': 'ALPACA', 'granularity': '1day', 'start': WHEN},
        str,
        _REQUIRED,
        id='StoreAssetDatasetBody',
        marks=pytest.mark.data_store,
    ),
    pytest.param(
        StoreAssetDatasetQuery,
        {'owner': SECRET},
        str | None,
        None,
        id='StoreAssetDatasetQuery',
        marks=pytest.mark.data_store,
    ),
    pytest.param(
        AssetDatasetStoreDelete,
        {'id': DATASET_ID, 'owner': SECRET},
        str | None,
        None,
        id='AssetDatasetStoreDelete',
        marks=pytest.mark.data_store,
    ),
    pytest.param(
        BaseGetDatasetRequest,
        {
            'dataset_id': DATASET_ID,
            'owner': SECRET,
            'source': 'ALPACA',
            'granularity': '1day',
            'start': WHEN,
            'end': None,
            'expiry': WHEN,
            'expiry_type': ExpiryType.BULK,
            'update_type': UpdateType.STATIC,
        },
        str,
        _REQUIRED,
        id='BaseGetDatasetRequest',
        marks=pytest.mark.data_ingest,
    ),
]
DECLARING_MODELS = frozenset(case.values[0] for case in DECLARATION_CASES)


def _schema_modules() -> list[str]:
    """Every module under schemas/, tests excluded, found by walking the package."""
    return sorted(
        info.name
        for info in pkgutil.walk_packages(schemas.__path__, prefix='schemas.')
        if '.tests' not in info.name and not info.name.endswith('.tests')
    )


def _schema_models() -> list[type[BaseModel]]:
    """Every pydantic model defined in a schemas/ module, each once, in a stable order."""
    found = {}
    for name in _schema_modules():
        module = importlib.import_module(name)
        for _, value in inspect.getmembers(module, inspect.isclass):
            if issubclass(value, BaseModel) and value.__module__ == name:
                found[f'{name}.{value.__qualname__}'] = value
    return [found[key] for key in sorted(found)]


def _plain_twin(model: type[BaseModel], annotation: Any, default: Any) -> type[BaseModel]:
    """The model with owner declared the way it was before the alias: same name, same everything else.

    A subclass redeclaring one field, which pydantic resolves as a fresh field, so the twin
    carries no repr=False. Named and placed like the original so its JSON schema titles match.
    """
    return create_model(
        model.__name__, __base__=model, __module__=model.__module__, __doc__=model.__doc__, owner=(annotation, default)
    )


@pytest.mark.parametrize(('model', 'payload', 'plain', 'default'), DECLARATION_CASES)
def test_owner_is_absent_from_repr_and_str(model: type[BaseModel], payload: dict, plain: Any, default: Any):
    """The value, and the field name with it, are left out of the rendered model."""
    instance = model.model_validate(payload)
    for rendered in (repr(instance), str(instance), f'{instance}'):
        assert SECRET not in rendered, f'{model.__name__} renders the owner value: {rendered}'
        assert 'owner=' not in rendered, f'{model.__name__} renders an owner field: {rendered}'

    # The control: with the old plain declaration the same payload DOES render the value. So the
    # assertion above is about the alias, not about a repr that never showed owner anyway.
    twin = _plain_twin(model, plain, default).model_validate(payload)
    assert SECRET in repr(twin), f'the plain-str twin of {model.__name__} does not render owner: {twin!r}'


@pytest.mark.parametrize(('model', 'payload', 'plain', 'default'), DECLARATION_CASES)
def test_owner_is_still_serialised_unchanged(model: type[BaseModel], payload: dict, plain: Any, default: Any):
    """model_dump and model_dump_json carry the value as given. Only rendering is affected."""
    instance = model.model_validate(payload)
    assert instance.owner == SECRET
    assert instance.model_dump()['owner'] == SECRET
    assert json.loads(instance.model_dump_json())['owner'] == SECRET
    assert model.model_validate_json(instance.model_dump_json()).owner == SECRET


@pytest.mark.parametrize(('model', 'payload', 'plain', 'default'), DECLARATION_CASES)
def test_the_json_schema_is_what_the_plain_declaration_produced(
    model: type[BaseModel], payload: dict, plain: Any, default: Any
):
    """repr=False is invisible to the contract: the whole JSON schema matches the plain-str twin."""
    for mode in ('validation', 'serialization'):
        assert model.model_json_schema(mode=mode) == _plain_twin(model, plain, default).model_json_schema(mode=mode)


@pytest.mark.common
def test_every_sensitive_field_in_schemas_is_kept_out_of_the_repr():
    """tj-w6bpjm: a model in schemas/ that declares a listed field without an alias fails here.

    Found by introspection over every schemas/ module, not from a list, so a model added later is
    covered. Inherited declarations are checked too, since each subclass carries its own copy of
    the field.
    """
    models = _schema_models()
    offenders = [
        f'{model.__module__}.{model.__qualname__}.{field}'
        for model in models
        for field in SENSITIVE_FIELDS
        if field in model.model_fields and model.model_fields[field].repr is not False
    ]
    assert not offenders, (
        f'these sensitive fields render in their model repr: {offenders}. Declare them with '
        f'common.sensitive.SensitiveStr or OptionalSensitiveStr, never `SensitiveStr | None`.'
    )
    # Guard the guard: the walk has to find at least the four declaration sites pinned above.
    declaring = {model for model in models if any(field in model.__annotations__ for field in SENSITIVE_FIELDS)}
    missing = sorted(model.__name__ for model in DECLARING_MODELS - declaring)
    assert not missing, f'the walk over schemas/ did not find the declarations of {missing}'


# Run in a fresh interpreter: the schema classes are built when their modules are first imported,
# which in this process happened long before any test could watch. Every warning is recorded,
# and the ones pydantic raises are reported.
_WARNING_PROBE = """
import importlib, json, warnings
from pydantic import BaseModel

modules = json.loads(input())
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter('always')
    for name in modules:
        module = importlib.import_module(name)
        for value in vars(module).values():
            if isinstance(value, type) and issubclass(value, BaseModel) and value.__module__ == name:
                if not value.__pydantic_generic_metadata__['parameters']:
                    value.model_json_schema()
print(json.dumps([
    f'{w.category.__module__}.{w.category.__name__}: {w.message}'
    for w in caught if w.category.__module__.startswith('pydantic')
]))
"""


@pytest.mark.common
def test_the_schema_modules_raise_no_pydantic_warning():
    """The optional alias is its own Annotated, so no model build warns.

    `SensitiveStr | None` is the tempting spelling. pydantic 2.13 answers it with
    UnsupportedFieldAttributeWarning and drops the Field(repr=False), leaving the value in the
    repr. A warning at import is the only signal it gives, so no warning is the contract.
    """
    modules = ['common.sensitive', *_schema_modules()]
    result = subprocess.run(
        [sys.executable, '-c', _WARNING_PROBE],
        input=json.dumps(modules),
        cwd=SERVER_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f'the warning probe failed to run: {result.stderr}'
    warned = json.loads(result.stdout.strip().splitlines()[-1])
    assert warned == [], f'building the schema models raised pydantic warnings: {warned}'
