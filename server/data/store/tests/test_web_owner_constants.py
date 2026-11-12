"""The web UI's copies of the store's owner labels equal the store's own (tj-7zjov1.267, pins tj-7zjov1.266).

THE DESIGN: ADR tj-26jt9w addendum 23 rule B and the owner's ruling A on tj-7zjov1.263. The UI withholds the four
retention writes for a dataset whose `DatasetSummary.owner` equals `PANEL_OWNER` in `web/src/api/usage.ts`, which
mirrors `PANEL_OWNER` in `server/data/store/app/client_label.py`. Nothing on the wire carries the constant, so if the
two drift the UI silently offers the writes again (or withholds them from another owner's datasets). The same holds
for the operator's label: `OPERATOR_CLIENT_ID` in the web is what the server's `OPERATOR_PRINCIPAL` must equal for
a read to count as a UI read and for the operator's own dataset to need no override.

THE PIN. The server side is the imported constant, the web side the literal parsed from the TypeScript source,
which must declare it exactly once as `export const <NAME> = '<literal>'`. A change to either alone fails here. No
earlier cross-language pin of these labels existed (searched server/*/tests, tests/ and web/src specs on
2026-11-02); the web's own value is also pinned by web/src/components/retention/panelOwned.spec.ts.
"""

import re

import pytest

from common.tests.roots import REPO_ROOT
from data.store.app.client_label import OPERATOR_PRINCIPAL, PANEL_OWNER


USAGE_TS = REPO_ROOT / 'web' / 'src' / 'api' / 'usage.ts'


def web_string_constant(name: str) -> str:
    """The single-quoted literal NAME is exported as in web/src/api/usage.ts; exactly one declaration."""
    source = USAGE_TS.read_text(encoding='utf-8')
    found = re.findall(rf"^export const {re.escape(name)}\s*=\s*'([^'\\]*)'\s*$", source, flags=re.MULTILINE)
    assert len(found) == 1, f'{USAGE_TS} should declare {name} once as a single-quoted literal, found {len(found)}'
    # Any other assignment of the name (a let, a re-declaration, a double-quoted form) would escape the regex.
    assert len(re.findall(rf'\b{re.escape(name)}\s*=', source)) == 1, f'{name} is assigned more than once'
    return found[0]


def test_the_web_source_is_present():
    assert USAGE_TS.is_file(), f'{USAGE_TS} is missing: the web owner constants cannot be compared'


@pytest.mark.parametrize(
    ('web_name', 'server_value'),
    [('PANEL_OWNER', PANEL_OWNER), ('OPERATOR_CLIENT_ID', OPERATOR_PRINCIPAL)],
    ids=['panel-owner', 'operator'],
)
def test_the_web_constant_equals_the_server_constant(web_name: str, server_value: str):
    assert web_string_constant(web_name) == server_value


def test_the_panel_owner_is_the_literal_panel_on_both_sides():
    # The pair moving together would pass the equality; the name is ADR tj-26jt9w addendum 20 (3).
    assert PANEL_OWNER == 'panel'
    assert web_string_constant('PANEL_OWNER') == 'panel'
