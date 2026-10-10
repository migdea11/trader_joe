"""exchange-calendars is declared for data_store only, locked, and answers XNYS offline (validator, gating tj-grna9p.17).

THE ACCEPTANCE HAS THREE HALVES and this file holds the two a venv can hold. Decision tj-grna9p.8 item 3 puts the
trading calendar in the data-store dependency group and in no other ("No other group gains it", tj-grna9p.17), and
the dependency is only useful if the calendar it brings knows the exchange: a session, a holiday and an early close
on XNYS, from the package's own offline data. That `import exchange_calendars` works INSIDE the data_store image is
the third half and needs docker (make prod-build); it is not run here and the verdict note says so.

What makes the declaration complete is test_declared_imports.py's subject, not this file's: it parametrises over
every import the tree makes, so freshness.py's exchange_calendars import is checked there by construction, and so
is its pandas import (a FINDINGS entry there until pandas is declared).
"""

import tomllib
from datetime import UTC, datetime

import pytest

from common.tests.roots import REPO_ROOT


pytestmark = pytest.mark.build_infra

DISTRIBUTION = 'exchange-calendars'


def _declared_in() -> set[str]:
    groups = tomllib.loads((REPO_ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['dependency-groups']
    return {
        group
        for group, entries in groups.items()
        for entry in entries
        if isinstance(entry, str) and entry.split('>')[0].split('=')[0].split('<')[0].strip() == DISTRIBUTION
    }


def test_the_calendar_is_declared_in_the_data_store_group_and_no_other():
    assert _declared_in() == {'data-store'}


def test_the_calendar_is_locked_for_the_data_store_group():
    lock = tomllib.loads((REPO_ROOT / 'uv.lock').read_text(encoding='utf-8'))
    (locked,) = [package for package in lock['package'] if package['name'] == DISTRIBUTION]
    assert locked['version'] == '4.13.2'
    (project,) = [package for package in lock['package'] if package['name'] == 'trader-joe']
    holders = {
        group for group, deps in project['dev-dependencies'].items() for dep in deps if dep['name'] == DISTRIBUTION
    }
    assert holders == {'data-store'}


def test_xnys_knows_a_session_a_holiday_and_an_early_close():
    # Imported here, not at module level: the venv installs data-store, but a collection error in a build_infra file
    # would hide every other case in it. Session labels are passed as strings, so this file needs no pandas import.
    import exchange_calendars

    xnys = exchange_calendars.get_calendar('XNYS')
    assert str(xnys.tz) == 'America/New_York'
    assert xnys.is_session('2026-10-05')
    assert not xnys.is_session('2026-11-26'), 'Thanksgiving'
    assert not xnys.is_session('2026-10-03'), 'a Saturday'
    assert xnys.session_open('2026-10-05').to_pydatetime() == datetime(2026, 10, 5, 13, 30, tzinfo=UTC)
    # The day after Thanksgiving closes at 13:00 ET.
    assert xnys.session_close('2026-11-27').to_pydatetime() == datetime(2026, 11, 27, 18, tzinfo=UTC)
