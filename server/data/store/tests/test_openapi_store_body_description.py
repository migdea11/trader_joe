"""The public description of POST /store's body says what ready_by and best_effort do today (validator, tj-c11nih item 1).

StoreAssetDatasetBody's docstring is rendered into the OpenAPI document the private repo's SDK is generated from.
It used to say the two request fields were stored on a request log and inert under a plain FIFO queue; the owner
deferred the request log and ratified a scheduler with no store queue (tj-pd41mt ADDENDUM 1, 2026-10-12), so that
text was false. Pinned on the SERVED document (GET /openapi.json), not the class: what a reader of the API sees.
"""

import re

import pytest
from fastapi.testclient import TestClient

from data.store.app.main import app


pytestmark = pytest.mark.data_store

SCHEMA = 'StoreAssetDatasetBody'
# The claims the old text made that are false now: the fields kept somewhere, a log, a queue. ('stored' alone is
# not one: the STREAM paragraph rightly speaks of an entry already stored.)
FALSE_CLAIMS = (r'request[ -]log', r'\bqueue', r'\bFIFO\b', r'\bare stored\b', r'\binert\b')


@pytest.fixture(scope='module')
def description() -> str:
    # No context manager: the lifespan (Postgres, data_ingest) never runs for a document read.
    document = TestClient(app).get('/openapi.json').json()
    return document['components']['schemas'][SCHEMA]['description']


def test_the_body_description_names_both_request_fields(description):
    assert 'ready_by' in description and 'best_effort' in description, description


@pytest.mark.parametrize('claim', FALSE_CLAIMS)
def test_the_body_description_claims_no_request_log_queue_or_storage(description, claim):
    assert not re.search(claim, description, re.IGNORECASE), f'{claim!r} in the public description: {description}'


def test_the_body_description_says_the_fields_are_validated_and_ignored(description):
    assert re.search(r'\bignored\b', description, re.IGNORECASE), description
    assert re.search(r'\bvalidated\b', description, re.IGNORECASE), description
    assert 'naive ready_by is a 422' in description, description
