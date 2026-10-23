"""The reader's label on a bar read and the kind derived from it (tj-grna9p.46, OPTION R of tj-grna9p.131).

The design, from the architect's 2026-10-13 notes on tj-grna9p.46 and the addendum on tj-vhboky.1:

* the label is the X-Client-Id header, the caller's dataset owner string -- a usage LABEL, never an authority;
* valid is 1..128 characters, no control character, not starting with '_'; anything else (and a missing header)
  is '_unattributed'. NEVER TRUNCATED: truncation could merge two clients;
* the kind is UI iff the label equals OPERATOR_PRINCIPAL ('operator'), a constant introduced ONCE; else SDK,
  the reserved values included;
* REDACTION: the value is never in a log line or a repr; a refused label logs its reason and length only.

Pure functions, no database: the PR gate.
"""

import ast
import logging
from pathlib import Path

import pytest

from common.tests.roots import SERVER_ROOT
from data.store.app import client_label
from data.store.app.client_label import (
    MAX_CLIENT_ID_LENGTH,
    OPERATOR_PRINCIPAL,
    OVERFLOW,
    UNATTRIBUTED,
    ClientLabel,
    classify_client_label,
)
from data.store.app.database.models.dataset_read import ReadClientKind


pytestmark = pytest.mark.data_store

SENTINEL = 'label-sentinel-5e1f'


def test_the_design_constants():
    assert OPERATOR_PRINCIPAL == 'operator'
    assert (UNATTRIBUTED, OVERFLOW) == ('_unattributed', '_overflow')
    assert MAX_CLIENT_ID_LENGTH == 128
    assert client_label.CLIENT_ID_HEADER == 'X-Client-Id'


@pytest.mark.parametrize(
    'raw',
    [
        SENTINEL,
        'a',
        pytest.param('x' * MAX_CLIENT_ID_LENGTH, id='128-chars'),
        'owner with spaces',
        'é-unicode',
        'Operator',
    ],
)
def test_a_valid_label_is_kept_whole_and_is_an_sdk_read(raw: str):
    label = classify_client_label(raw)
    assert label == ClientLabel(raw, ReadClientKind.SDK)
    assert len(label.client_id) == len(raw), 'never truncated'


def test_the_operator_principal_is_the_one_ui_label():
    assert classify_client_label(OPERATOR_PRINCIPAL) == ClientLabel(OPERATOR_PRINCIPAL, ReadClientKind.UI)
    # Exact equality, not a prefix or a case-fold.
    assert classify_client_label('operator ').client_kind is ReadClientKind.SDK
    assert classify_client_label('operator2').client_kind is ReadClientKind.SDK


@pytest.mark.parametrize(
    ('raw', 'reason'),
    [
        (None, None),
        ('', 'empty'),
        pytest.param('x' * (MAX_CLIENT_ID_LENGTH + 1), 'too long', id='129-chars'),
        pytest.param('x' * 10_000, 'too long', id='10000-chars'),
        ('tab\there', 'control character'),
        ('nul\x00', 'control character'),
        ('unit\x1fsep', 'control character'),
        ('del\x7f', 'control character'),
        ('c1\x85next-line', 'control character'),
        ('c1-end\x9f', 'control character'),
        ('_unattributed', 'reserved prefix'),
        ('_overflow', 'reserved prefix'),
        ('_anything', 'reserved prefix'),
    ],
)
def test_a_missing_or_malformed_label_is_unattributed_sdk(raw, reason, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.DEBUG, logger=client_label.log.name)
    label = classify_client_label(raw)

    assert label == ClientLabel(UNATTRIBUTED, ReadClientKind.SDK)
    lines = [record.getMessage() for record in caplog.records if record.name == client_label.log.name]
    if reason is None:
        assert lines == [], 'an absent header is not a refusal'
    else:
        # The reason and the length only (D8).
        assert lines == [f'Refused a client label: {reason}, length {len(raw)}']


def test_a_refused_label_never_reaches_a_log_line(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.DEBUG)
    for raw in (f'_{SENTINEL}', f'{SENTINEL}\n', SENTINEL * 20):
        classify_client_label(raw)
    assert caplog.records, 'the refusals were logged'
    assert not any(SENTINEL in record.getMessage() for record in caplog.records)


def test_the_label_is_not_in_its_repr():
    label = classify_client_label(SENTINEL)
    assert SENTINEL not in repr(label)
    assert SENTINEL not in str(label)
    assert 'SDK' in repr(label)


def test_operator_principal_is_defined_once_in_the_store():
    """ONE definition (.86 reuses it): no other production module under the store or its routers spells 'operator'."""
    spellings = []
    for root in (SERVER_ROOT / 'data' / 'store' / 'app', SERVER_ROOT / 'routers' / 'data_store'):
        for path in sorted(root.rglob('*.py')):
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                if isinstance(node, ast.Constant) and node.value == OPERATOR_PRINCIPAL:
                    spellings.append(str(Path(path).relative_to(SERVER_ROOT)))
    assert spellings == ['data/store/app/client_label.py'], spellings
