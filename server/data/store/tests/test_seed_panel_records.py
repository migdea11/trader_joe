"""The seed's panel cast (data/store/seeds/panel_records.py): what it hands the store's panel crud, and its constants.

WHY THIS FILE EXISTS (validator, tj-7zjov1.197; builder bead tj-7zjov1.196). The producer refuses a seed whose panel
tables do not hold the EXPECTED_* counts (test_seed_producer.py pins that refusal against a fake stack). Nothing there
says the cast panel_records.py writes IS those counts, or that it goes through the crud at all: a cast that dropped a
transition label and a constant edited to match would both pass. This file drives seed_panel_records with the crud
replaced by recorders and pins the calls: one panel, its versions, five passes and fourteen transitions whose counts
are the EXPECTED constants, every label present, the one same-pass ENTERED + CREATED tie, synthetic names only.

WHAT TIER THIS IS. No Postgres. The crud functions and the session are fakes that record their arguments; NewTransition,
PanelSettings and SourceSettings are the real dataclasses, so their own validation (a CREATED row must name a dataset
and a request) still runs. NOT PROVED HERE: that the real crud accepts this cast, which the orchestrator's seed_dump run
on the stack proves (tj-7zjov1.197 phase 2), and what Postgres stamps (dump.NORMALISE_SQL, test_seed_dump.py).
"""

import asyncio
import inspect
import uuid
from collections import Counter
from dataclasses import dataclass

import pytest
import sqlalchemy.ext.asyncio as sqlalchemy_asyncio

from common.enums.data_stock import DataSource
from data.store.app.database.crud import panel_ledger, panel_settings
from data.store.app.database.models.panel import PanelCadence, PanelPassKind, PanelTransition
from data.store.seeds import ingest_records, panel_records
from data.store.seeds.ingest_records import _AL_ASSETS, REQUEST_IDS
from data.store.seeds.panel_records import (
    ABSENT_DATASET_ID,
    EXPECTED_DRY_RUN_PASSES,
    EXPECTED_PANELS,
    EXPECTED_PASS_KINDS,
    EXPECTED_PASSES,
    EXPECTED_SETTINGS_VERSIONS,
    EXPECTED_SOURCE_SETTINGS_VERSIONS,
    EXPECTED_TIED_MEMBERS,
    EXPECTED_TRANSITION_ROWS,
    EXPECTED_TRANSITIONS,
    EXPECTED_UNFINISHED_PASSES,
    PANEL_ACTOR,
    PANEL_NAME,
    PANEL_NAME_PATTERN,
    seed_panel_records,
)
from data.store.seeds.scenario import OWNER_PATTERN, SYMBOL_PATTERN


pytestmark = pytest.mark.data_store

DATASET_ID = uuid.UUID('7ceff30a-af80-1760-5057-d346cc334f4d')
URL = 'postgresql+asyncpg://seed@scratch/seed'
PANEL_ID = uuid.UUID('00000000-0000-4000-8000-0000000000a1')
SETTINGS_IDS = [uuid.UUID(f'00000000-0000-4000-8000-0000000001{n:02d}') for n in (1, 2)]
SOURCE_IDS = [uuid.UUID(f'00000000-0000-4000-8000-0000000002{n:02d}') for n in (1, 2)]


def snapshot_id(request_name: str) -> uuid.UUID:
    """The id the fake session answers for the snapshot of the asset-list request of that name."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f'snapshot/{request_name}')


REQUEST_NAMES = {request_id: name for name, request_id in REQUEST_IDS.items()}


@dataclass
class _Record:
    id: uuid.UUID


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class Recorder:
    """The crud, the session and the engine, recording every call in order as (name, arguments)."""

    def __init__(self, missing_snapshot: str | None = None, already_finished: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self.passes: list[uuid.UUID] = []
        self.versions = iter(SETTINGS_IDS[1:])
        self.sources = iter(SOURCE_IDS[1:])
        self.missing_snapshot = missing_snapshot
        self.already_finished = already_finished
        self.disposed = False

    # -- the engine and the session --

    def create_async_engine(self, url, **kwargs):
        self.calls.append(('engine', {'url': url, **kwargs}))
        return self

    async def dispose(self):
        self.disposed = True

    def session(self, engine, **kwargs):
        assert engine is self
        self.calls.append(('session', kwargs))
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement):
        request_id = statement.whereclause.right.value
        name = REQUEST_NAMES[request_id]
        self.calls.append(('snapshot lookup', {'request': name}))
        return _Result(None if name == self.missing_snapshot else snapshot_id(name))

    async def commit(self):
        self.calls.append(('commit', {}))

    # -- the panel crud --

    async def create_panel(self, db, **kwargs):
        assert db is self
        self.calls.append(('create_panel', kwargs))
        return _Record(PANEL_ID), _Record(SETTINGS_IDS[0])

    async def current_source_settings(self, db, source):
        self.calls.append(('current_source_settings', {'source': source}))
        return _Record(SOURCE_IDS[0])

    async def append_source_settings_version(self, db, **kwargs):
        self.calls.append(('append_source_settings_version', kwargs))
        return _Record(next(self.sources))

    async def append_settings_version(self, db, **kwargs):
        self.calls.append(('append_settings_version', kwargs))
        return _Record(next(self.versions))

    async def open_pass(self, db, **kwargs):
        pass_id = uuid.UUID(int=len(self.passes) + 1)
        self.passes.append(pass_id)
        self.calls.append(('open_pass', {'pass_id': pass_id, **kwargs}))
        return pass_id

    async def append_transitions(self, db, pass_id, rows):
        self.calls.append(('append_transitions', {'pass_id': pass_id, 'rows': list(rows)}))

    async def finish_pass(self, db, pass_id, counts):
        self.calls.append(('finish_pass', {'pass_id': pass_id, 'counts': dict(counts)}))
        return not self.already_finished

    # -- reads of the record --

    def named(self, name: str) -> list[dict]:
        return [arguments for called, arguments in self.calls if called == name]

    def rows(self) -> list[tuple[uuid.UUID, panel_ledger.NewTransition]]:
        return [(call['pass_id'], row) for call in self.named('append_transitions') for row in call['rows']]


@pytest.fixture
def recorder(monkeypatch):
    return _install(monkeypatch, Recorder())


def _install(monkeypatch, fake: Recorder) -> Recorder:
    monkeypatch.setattr(sqlalchemy_asyncio, 'create_async_engine', fake.create_async_engine)
    monkeypatch.setattr(sqlalchemy_asyncio, 'AsyncSession', fake.session)
    for name in (
        'create_panel',
        'current_source_settings',
        'append_source_settings_version',
        'append_settings_version',
    ):
        monkeypatch.setattr(panel_settings, name, getattr(fake, name))
    for name in ('open_pass', 'append_transitions', 'finish_pass'):
        monkeypatch.setattr(panel_ledger, name, getattr(fake, name))
    return fake


def _run(fake: Recorder) -> Recorder:
    asyncio.run(seed_panel_records(URL, DATASET_ID))
    return fake


# ---------------------------------------------------------------------------------------------
# The constants: the counts the producer refuses by, and the synthetic names
# ---------------------------------------------------------------------------------------------


def test_the_expected_constants_are_the_beads_cast():
    assert (EXPECTED_PANELS, EXPECTED_SETTINGS_VERSIONS, EXPECTED_SOURCE_SETTINGS_VERSIONS) == (1, 2, 2)
    assert (EXPECTED_PASSES, EXPECTED_DRY_RUN_PASSES, EXPECTED_UNFINISHED_PASSES) == (5, 1, 1)
    assert EXPECTED_PASS_KINDS == {'MEMBERSHIP': 4, 'RELEASE': 1}
    assert sum(EXPECTED_PASS_KINDS.values()) == EXPECTED_PASSES
    assert EXPECTED_TRANSITIONS == {
        'ENTERED': 5,
        'CREATED': 2,
        'DEFERRED': 2,
        'REFUSED': 1,
        'EXITED': 2,
        'DRAINING': 1,
        'DRAINED': 1,
    }
    assert EXPECTED_TRANSITION_ROWS == sum(EXPECTED_TRANSITIONS.values()) == 14
    assert EXPECTED_TIED_MEMBERS == 1


def test_every_label_of_the_enums_is_in_the_expected_cast():
    """The bead: every panel_transition label at least once, both pass kinds. A label added to the enum must join."""
    assert set(EXPECTED_TRANSITIONS) == {transition.value for transition in PanelTransition}
    assert all(count >= 1 for count in EXPECTED_TRANSITIONS.values())
    assert set(EXPECTED_PASS_KINDS) == {kind.value for kind in PanelPassKind}


def test_the_panel_name_and_actor_are_synthetic():
    assert PANEL_NAME == 'zz-seed-panel' and PANEL_NAME_PATTERN.fullmatch(PANEL_NAME)
    assert PANEL_ACTOR == 'seed-owner-p' and OWNER_PATTERN.fullmatch(PANEL_ACTOR)


@pytest.mark.parametrize(
    ('name', 'synthetic'),
    [
        ('zz-seed-panel', True),
        ('zz-seed-panel-2', True),
        ('zz-seed-panelx9', True),
        ('zz-seed-pane', False),
        ('ZZ-SEED-PANEL', False),
        ('zz-seed-panel ', False),
        ('my-zz-seed-panel', False),
        ('zz-seed-panel/', False),
        ('momentum', False),
    ],
)
def test_the_panel_name_pattern_is_anchored(name, synthetic):
    assert bool(PANEL_NAME_PATTERN.fullmatch(name)) is synthetic


def test_the_absent_dataset_is_none_of_the_casts():
    assert ABSENT_DATASET_ID != DATASET_ID
    assert ABSENT_DATASET_ID not in REQUEST_IDS.values()


# ---------------------------------------------------------------------------------------------
# What the cast hands the crud
# ---------------------------------------------------------------------------------------------


def test_the_cast_runs_on_its_own_engine_and_session_and_disposes_the_engine(recorder):
    _run(recorder)
    assert recorder.calls[0] == ('engine', {'url': URL, 'connect_args': {'timeout': 10}})
    assert recorder.calls[1] == ('session', {'expire_on_commit': False})
    assert recorder.disposed


def test_one_panel_is_created_with_version_1_and_the_source_settings(recorder):
    _run(recorder)
    (created,) = recorder.named('create_panel')
    assert created['name'] == PANEL_NAME and created['source'] is DataSource.ALPACA_API
    assert created['actor'] == PANEL_ACTOR and created['reason'].strip()
    settings = created['settings']
    assert settings.enabled is True and settings.cadence is None, 'version 1 names no cadence: the default applies'
    assert created['source_settings'] is not None, 'the source gets its first version with the panel'
    assert len(recorder.named('create_panel')) == EXPECTED_PANELS


def test_the_second_versions_disable_the_panel_and_change_the_source(recorder):
    _run(recorder)
    (source,) = recorder.named('append_source_settings_version')
    (version,) = recorder.named('append_settings_version')
    assert source['source'] is DataSource.ALPACA_API and source['actor'] == PANEL_ACTOR and source['reason'].strip()
    assert version['panel_id'] == PANEL_ID and version['actor'] == PANEL_ACTOR and version['reason'].strip()
    assert version['settings'].enabled is False, 'a disable is a version too'
    assert version['settings'].cadence is PanelCadence.WEEKLY
    # Version 1 comes with create_panel, version 2 is this one; the source likewise.
    assert 1 + len(recorder.named('append_settings_version')) == EXPECTED_SETTINGS_VERSIONS
    assert 1 + len(recorder.named('append_source_settings_version')) == EXPECTED_SOURCE_SETTINGS_VERSIONS
    # The versions are written between the first pass and the second, so the passes name both.
    names = [called for called, _ in recorder.calls]
    first_commit = names.index('commit')
    assert first_commit < names.index('append_source_settings_version') < names.index('open_pass', first_commit)


def test_five_passes_by_kind_dry_run_and_finish(recorder):
    _run(recorder)
    passes = recorder.named('open_pass')
    assert len(passes) == EXPECTED_PASSES
    assert dict(Counter(call['kind'].value for call in passes)) == EXPECTED_PASS_KINDS
    assert sum(call['dry_run'] for call in passes) == EXPECTED_DRY_RUN_PASSES
    finished = {call['pass_id'] for call in recorder.named('finish_pass')}
    unfinished = [call for call in passes if call['pass_id'] not in finished]
    assert len(unfinished) == EXPECTED_UNFINISHED_PASSES
    assert unfinished[0]['kind'] is PanelPassKind.RELEASE, 'the unfinished pass is the RELEASE'
    assert all(call['panel_id'] == PANEL_ID for call in passes)


def test_each_pass_names_the_versions_in_force_and_the_casts_snapshots(recorder):
    _run(recorder)
    passes = recorder.named('open_pass')
    assert [(p['settings_version_id'], p['source_settings_version_id']) for p in passes] == [
        (SETTINGS_IDS[0], SOURCE_IDS[0]),
        *[(SETTINGS_IDS[1], SOURCE_IDS[1])] * 4,
    ]
    assert [p['active_snapshot_id'] for p in passes] == [
        snapshot_id('AL_ACTIVE_1'),
        snapshot_id('AL_ACTIVE_2'),
        *[snapshot_id('AL_ACTIVE_EMPTY')] * 3,
    ]
    assert [p['inactive_snapshot_id'] for p in passes] == [
        snapshot_id('AL_INACTIVE_1'),
        snapshot_id('AL_INACTIVE_2'),
        None,
        snapshot_id('AL_INACTIVE_2'),
        snapshot_id('AL_INACTIVE_2'),
    ]


def test_the_transitions_are_the_expected_counts_per_kind(recorder):
    _run(recorder)
    rows = [row for _, row in recorder.rows()]
    assert len(rows) == EXPECTED_TRANSITION_ROWS
    assert dict(Counter(row.transition.value for row in rows)) == EXPECTED_TRANSITIONS


def test_exactly_one_member_has_a_same_pass_entered_and_created(recorder):
    """The tie only seq orders: ENTERED appended before CREATED, at one as_of, in one pass."""
    _run(recorder)
    groups: dict[tuple, list] = {}
    for pass_id, row in recorder.rows():
        groups.setdefault((pass_id, row.vendor_asset_id, row.as_of), []).append(row.transition)
    tied = [kinds for kinds in groups.values() if {PanelTransition.ENTERED, PanelTransition.CREATED} <= set(kinds)]
    assert len(tied) == EXPECTED_TIED_MEMBERS
    (kinds,) = tied
    assert kinds.index(PanelTransition.ENTERED) < kinds.index(PanelTransition.CREATED), 'ENTERED is appended first'


def test_every_transition_is_of_a_cast_asset_and_a_synthetic_symbol(recorder):
    _run(recorder)
    cast_assets = set(_AL_ASSETS.values())
    for _, row in recorder.rows():
        assert (row.vendor_asset_id, row.symbol) in cast_assets
        assert row.vendor_asset_id.startswith('seed-asset-') and SYMBOL_PATTERN.fullmatch(row.symbol)
        assert row.as_of.tzinfo is not None


def test_every_symbol_a_settings_version_lists_is_synthetic(recorder):
    _run(recorder)
    (created,) = recorder.named('create_panel')
    (version,) = recorder.named('append_settings_version')
    listed = [*created['settings'].allow_symbols, *created['settings'].deny_symbols]
    listed += [*version['settings'].allow_symbols, *version['settings'].deny_symbols]
    assert listed, 'the cast lists symbols, so the foreign-row check of them is exercised'
    assert all(SYMBOL_PATTERN.fullmatch(symbol) for symbol in listed)


def test_the_created_rows_name_the_casts_dataset_and_requests_and_the_drained_one_an_absent_dataset(recorder):
    _run(recorder)
    rows = [row for _, row in recorder.rows()]
    created = [row for row in rows if row.transition is PanelTransition.CREATED]
    assert {row.dataset_id for row in created} == {DATASET_ID}
    assert {row.request_id for row in created} == {REQUEST_IDS['SUCCEEDED'], REQUEST_IDS['EMPTY']}
    (drained,) = [row for row in rows if row.transition is PanelTransition.DRAINED]
    assert drained.dataset_id == ABSENT_DATASET_ID
    named = {row.dataset_id for row in rows if row.dataset_id is not None}
    assert named == {DATASET_ID, ABSENT_DATASET_ID}


def test_a_deferred_or_refused_row_carries_its_cause(recorder):
    _run(recorder)
    for _, row in recorder.rows():
        if row.transition in (PanelTransition.DEFERRED, PanelTransition.REFUSED):
            assert row.cause
    causes = {row.cause for _, row in recorder.rows() if row.transition is PanelTransition.DEFERRED}
    assert causes == {'ramp_rate', 'request_rate'}


def test_a_finished_pass_is_finished_with_its_own_counts_per_kind(recorder):
    _run(recorder)
    rows_of: dict[uuid.UUID, list] = {}
    for pass_id, row in recorder.rows():
        rows_of.setdefault(pass_id, []).append(row)
    for finish in recorder.named('finish_pass'):
        assert set(finish['counts']) == set(panel_ledger.COUNT_KEYS)
        made = Counter(row.transition.value.lower() for row in rows_of.get(finish['pass_id'], []))
        assert finish['counts'] == {key: made.get(key, 0) for key in panel_ledger.COUNT_KEYS}


def test_each_pass_is_opened_appended_finished_and_committed_in_that_order(recorder):
    """One transaction per pass: the ledger never commits, the cast does, once per pass."""
    _run(recorder)
    ledger = [
        (called, arguments.get('pass_id'))
        for called, arguments in recorder.calls
        if called in ('open_pass', 'append_transitions', 'finish_pass', 'commit')
    ]
    expected = []
    for index, pass_id in enumerate(recorder.passes):
        expected += [('open_pass', pass_id), ('append_transitions', pass_id)]
        if index != 3:  # the RELEASE pass is left unfinished
            expected.append(('finish_pass', pass_id))
        expected.append(('commit', None))
    assert ledger == expected


def test_a_missing_snapshot_raises_and_the_engine_is_disposed(monkeypatch):
    fake = _install(monkeypatch, Recorder(missing_snapshot='AL_ACTIVE_2'))
    with pytest.raises(RuntimeError, match='seed asset-list snapshot of request AL_ACTIVE_2 is missing'):
        _run(fake)
    assert fake.disposed
    assert len(fake.named('open_pass')) == 1, 'nothing is opened on a snapshot that is not there'


def test_a_pass_already_finished_raises_and_nothing_more_is_written(monkeypatch):
    fake = _install(monkeypatch, Recorder(already_finished=True))
    with pytest.raises(RuntimeError, match='the seed pass was already finished'):
        _run(fake)
    assert fake.disposed
    assert len(fake.named('open_pass')) == 1 and not fake.named('commit')


def test_the_cast_reads_no_clock_and_draws_no_random_id():
    """The module docstring's promise: every instant a literal, every id the crud's or Postgres's."""
    source = inspect.getsource(panel_records).replace(panel_records.__doc__, '')
    for forbidden in ('.now(', 'utcnow', 'time.time', 'uuid4', 'import random', 'random.'):
        assert forbidden not in source, forbidden


def test_two_casts_hand_the_crud_identical_arguments(monkeypatch):
    """What the seed's bytes rest on, above what NORMALISE_SQL rewrites: the cast's own values never vary."""
    first = _run(_install(monkeypatch, Recorder()))
    second = _run(_install(monkeypatch, Recorder()))
    assert first.calls == second.calls


def test_the_ingest_cast_writes_the_panel_cast_last_with_its_url_and_dataset():
    """seed_ingest_records needs Postgres, so its call is pinned by source.

    After the asset-list cast and the legacy runs, inside the try that restores the intake's hooks, with the url and
    dataset id it was handed.
    """
    source = inspect.getsource(ingest_records.seed_ingest_records)
    call = 'await seed_panel_records(url, dataset_id)'
    assert source.count(call) == 1
    body = source[: source.index('finally:')]
    assert call in body, 'inside the try'
    assert body.index(call) > body.index('await _seed_legacy_runs(engine)')
    assert body.index(call) > body.rindex('await deliver_list(')
    assert body[body.index(call) + len(call) :].strip() == '', 'the last write of the cast'
