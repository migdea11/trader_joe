"""The store's vendor asset id stop rule on the real, migrated Postgres (tj-7zjov1.144, P50; tests tj-7zjov1.145).

THE DESIGN: P50's description (the architect's ruling of 2026-10-26, ADR tj-26jt9w addendum 13), its 21:51 UTC P18
gate note (the comparison base reads only bars attempts of the same dataset) and the P50v pin list. A SUCCEEDED bars
delivery whose vendor asset id differs from the dataset's last ACCEPTED one (SUCCEEDED or EMPTY, a bars attempt of the
dataset, with an id), or is empty after a set one, writes no bar; the same transaction sets the entry's collect false
and stopped_cause VENDOR_ASSET_CHANGED, writes the attempt row FAILED with the marker (error_domain NULL, the delivered
id as received) and the request FAILED with the same cause and sentence, and answers FINAL; RETIRED follows the
commit. A refused attempt is never a base, so a Resume is a re-check.

WHAT THIS PROVES that server/data/store/tests/test_delivery_intake.py cannot (SQLite, stand-in bar writer and entry
upsert): the REAL bar upsert writes nothing for the refused attempt (no stock_market_activity row at its timestamps);
the REAL entry upsert of a POST /store repeat clears stopped_cause on an explicit collect true and leaves it on false
(ON CONFLICT ON CONSTRAINT); the REAL operator_set_collecting clears it on a Resume and never sets it on a Stop; the
Postgres CHECK ck_store_dataset_entry_stopped_cause_needs_stop refuses a cause on a collecting row; and the comparison
base read compiles and runs on Postgres with a corporate-actions attempt row and a FAILED attempt in the history.

HOW: test_delivery_intake_pg.py's harness (DeliveryIntake on the stack's Postgres through asyncpg, its own_session
pointed at that engine, its publish journalled; the cleanup fixture deleting exactly this test's requests, attempt
rows and instances; entries registered with entry_registry, whose cleanup cascades to their bars).
"""

import uuid
from datetime import timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_stock import ExpiryType, Feed, Granularity, UpdateType
from data.store.app.client_label import OPERATOR_PRINCIPAL
from data.store.app.database.crud.ingest_run import last_accepted_vendor_asset_id
from data.store.app.database.crud.stock import store_dataset_entry as entry_crud
from data.store.app.database.models.ingest_request import IngestRequestFailureCause, IngestRequestState
from data.store.app.database.models.ingest_run import IngestRunState, IngestRunTrigger
from data.store.app.events import DatasetChange, DatasetChanged
from schemas.data_ingest.delivery import CorporateActionRecord, DeliveryOutcome
from schemas.data_ingest.fetch_dataset import BarPage
from tests.system import test_delivery_intake_pg as pg
from tests.system.test_delivery_intake_pg import (
    AS_OF,
    BAR_START,
    ENTRIES_NAME,
    FINAL,
    RUNS,
    _bars,
    _ca_request,
    _entry_of_request,
    _request,
    _request_row,
    _runs,
    bar_page,
    ca_report,
    report,
)


# A first sighting supersedes the stack's live ingest too; the fixture makes it live again (tj-7zjov1.233).
pytestmark = [pytest.mark.data_store, pytest.mark.asyncio, pytest.mark.usefixtures('live_instances_kept')]

# The sibling file's fixtures, reused by name.
created = pg.created
intake = pg.intake
instance_id = pg.instance_id

MARKER = 'VENDOR_ASSET_CHANGED'
CHECK = 'ck_store_dataset_entry_stopped_cause_needs_stop'
ID_A = 'PG-Vendor-Asset-A  '  # mixed case and a trailing space: stored and compared as received
ID_B = 'pg-vendor-asset-b'


def _delivery(request_id: uuid.UUID, holder: str, vendor_asset_id: str, hours: int, *days: int, **kwargs):
    """A SUCCEEDED (or other) bars report naming `vendor_asset_id`, finishing `hours` after AS_OF, bars on `days`."""
    parts = [bar_page(*days)] if days else [BarPage(bars=[])]
    attempt = report(request_id, instance_id=holder, parts=parts, **kwargs)
    attempt = attempt.model_copy(
        update={'header': attempt.header.model_copy(update={'vendor_asset_id': vendor_asset_id})}
    )
    return attempt.model_copy(
        update={'trailer': attempt.trailer.model_copy(update={'as_of': AS_OF + timedelta(hours=hours)})}
    )


async def _entry(engine, entry_id: uuid.UUID) -> sa.Row:
    async with engine.connect() as conn:
        return (await conn.execute(sa.text(f'SELECT * FROM {ENTRIES_NAME} WHERE id = :id'), {'id': entry_id})).one()  # nosec B608


async def _bar_days(engine, entry_id: uuid.UUID) -> list[int]:
    return [(row.timestamp - BAR_START).days for row in await _bars(engine, entry_id)]


async def _assert_stopped(engine, entry_id: uuid.UUID, request_id: uuid.UUID, answer, earlier: str, delivered: str):
    """The stop, every field, as Postgres holds it (the enum labels read back as text)."""
    assert answer.verdict is FINAL and answer.attempt_count == 1
    entry = await _entry(engine, entry_id)
    assert (entry.collect, entry.stopped_cause) == (False, MARKER)
    (run,) = await _runs(engine, request_id)
    assert (run.state, run.bars_written, run.verdict, run.dataset_id) == (IngestRunState.FAILED, 0, 'FINAL', entry_id)
    assert (run.error_domain, run.error_reason, run.vendor_asset_id) == (None, MARKER, delivered)
    assert f"'{earlier}'" in run.error_message and f"'{delivered}'" in run.error_message, run.error_message
    row = await _request_row(engine, request_id)
    assert (row.state, row.failure_cause, row.failure_detail) == (IngestRequestState.FAILED, MARKER, run.error_message)
    async with engine.connect() as conn:
        raw = (
            await conn.execute(
                sa.text(
                    'SELECT r.state::text AS state, run.state::text AS run_state FROM ingest_request r '
                    'JOIN ingest_run run ON run.request_id = r.id WHERE r.id = :id'
                ),
                {'id': request_id},
            )
        ).one()
    assert (raw.state, raw.run_state) == ('FAILED', 'FAILED')


async def test_pg_p50_the_fb_case_stops_with_no_bar_the_check_holds_and_a_post_repeat_resumes(
    intake, created, run_identity, own_symbol, instance_id, entry_registry, attempt
):
    """The FB case end to end through the real upsert, the real bar upsert and the CHECK, then the POST Resume."""
    holder = instance_id('p50fb')
    await intake.register(holder)
    post = {
        'dataset_id': None,
        'requested_end': None,
        'expiry_type': ExpiryType.ROLLING,
        'trigger': IngestRunTrigger.REQUEST,
    }

    async def post_request(collect):
        return await _request(intake.engine, created, run_identity, own_symbol, holder=holder, collect=collect, **post)

    # 1. A POST creates the dataset from its first delivery, id A, bars on days 0 and 1.
    first = await post_request(None)
    await intake.deliver(_delivery(first, holder, ID_A, 1, 0, 1))
    entry = await _entry_of_request(intake, first)
    entry_registry.add(entry.id)
    assert await _bar_days(intake.engine, entry.id) == [0, 1]

    # 2. The DAILY refresh delivers id B with bars on days 2 and 3: refused, the dataset stopped, no bar written.
    refresh = await _request(
        intake.engine, created, run_identity, own_symbol, holder=holder, dataset_id=entry.id, expiry_type=None
    )
    intake.published.clear()
    answer = await intake.deliver(_delivery(refresh, holder, ID_B, 2, 2, 3))
    await _assert_stopped(intake.engine, entry.id, refresh, answer, ID_A, ID_B)
    assert await _bar_days(intake.engine, entry.id) == [0, 1], 'a bar of the refused attempt is stored'
    assert intake.published[-1] == DatasetChanged(entry.id, DatasetChange.RETIRED)

    # 3. The CHECK: collect true with the cause still set is refused by Postgres.
    error = attempt(sa.text(f'UPDATE {ENTRIES_NAME} SET collect = true WHERE id = :id').bindparams(id=entry.id))  # nosec B608
    assert error is not None and CHECK in str(error.orig), error
    assert (await _entry(intake.engine, entry.id)).stopped_cause == MARKER

    # 4. A POST repeat with an explicit collect false (a zero-bar success, id A) leaves the cause.
    repeat_false = await post_request(False)
    await intake.deliver(_delivery(repeat_false, holder, ID_A, 3))
    stored = await _entry(intake.engine, entry.id)
    assert (stored.collect, stored.stopped_cause) == (False, MARKER)

    # 5. A POST repeat with explicit collect true clears the cause, and is still judged: id B stops it again.
    repeat_b = await post_request(True)
    answer = await intake.deliver(_delivery(repeat_b, holder, ID_B, 4, 5, 6))
    await _assert_stopped(intake.engine, entry.id, repeat_b, answer, ID_A, ID_B)
    assert await _bar_days(intake.engine, entry.id) == [0, 1], 'the refused base held: B is still a new holder'

    # 6. A POST repeat with explicit collect true and the earlier id A resumes the series.
    intake.published.clear()
    repeat_a = await post_request(True)
    await intake.deliver(_delivery(repeat_a, holder, ID_A, 5, 7))
    stored = await _entry(intake.engine, entry.id)
    assert (stored.collect, stored.stopped_cause) == (True, None)
    assert await _bar_days(intake.engine, entry.id) == [0, 1, 7]
    (run,) = await _runs(intake.engine, repeat_a)
    assert (run.state, run.error_reason, run.vendor_asset_id) == (IngestRunState.SUCCEEDED, None, ID_A)
    assert intake.published[-1] == DatasetChanged(entry.id, DatasetChange.RESUMED)


async def test_pg_p50_the_operator_resume_clears_the_cause_and_is_a_recheck(
    intake, created, run_identity, insert_entry, own_symbol, instance_id
):
    """Resume through the real operator_set_collecting: a new id stops again, the earlier id runs on; a Stop sets none."""
    entry = insert_entry(
        asset_symbol=own_symbol,
        owner=OPERATOR_PRINCIPAL,
        granularity=Granularity.ONE_DAY,
        feed=Feed.IEX,
        update_type=UpdateType.DAILY.value,
        expiry_type=ExpiryType.ROLLING.value,
        collect=True,
    )
    holder = instance_id('p50resume')
    await intake.register(holder)

    async def refresh(vendor_asset_id: str, hours: int, *days: int):
        request_id = await _request(
            intake.engine, created, run_identity, own_symbol, holder=holder, dataset_id=entry.id
        )
        return request_id, await intake.deliver(_delivery(request_id, holder, vendor_asset_id, hours, *days))

    async def set_collecting(collect: bool):
        async with AsyncSession(intake.engine, expire_on_commit=False) as db:
            return await entry_crud.operator_set_collecting(db, entry.id, collect, None)

    await refresh(ID_A, 1, 0)
    request_id, answer = await refresh(ID_B, 2, 1)
    await _assert_stopped(intake.engine, entry.id, request_id, answer, ID_A, ID_B)

    assert await set_collecting(True) is True
    stored = await _entry(intake.engine, entry.id)
    assert (stored.collect, stored.stopped_cause) == (True, None)

    request_id, answer = await refresh(ID_B, 3, 2)
    await _assert_stopped(intake.engine, entry.id, request_id, answer, ID_A, ID_B)
    assert await _bar_days(intake.engine, entry.id) == [0]

    assert await set_collecting(True) is True
    request_id, _ = await refresh(ID_A, 4, 3)
    (run,) = await _runs(intake.engine, request_id)
    assert (run.state, run.error_reason) == (IngestRunState.SUCCEEDED, None)
    assert await _bar_days(intake.engine, entry.id) == [0, 3]

    # An operator Stop never sets a cause.
    assert await set_collecting(False) is False
    stored = await _entry(intake.engine, entry.id)
    assert (stored.collect, stored.stopped_cause) == (False, None)


async def test_pg_p50_the_base_skips_corporate_actions_rows_failed_attempts_and_other_datasets(
    intake, created, run_identity, insert_entry, own_symbol, instance_id
):
    """The architect's P18 note on Postgres: only SUCCEEDED or EMPTY bars attempts of this dataset are the base."""
    entry = insert_entry(
        asset_symbol=own_symbol,
        granularity=Granularity.ONE_DAY,
        feed=Feed.IEX,
        update_type=UpdateType.DAILY.value,
        expiry_type=ExpiryType.ROLLING.value,
        collect=True,
    )
    other = insert_entry(
        asset_symbol=own_symbol,
        granularity=Granularity.ONE_HOUR,
        feed=Feed.IEX,
        update_type=UpdateType.DAILY.value,
        expiry_type=ExpiryType.ROLLING.value,
        collect=True,
    )
    holder = instance_id('p50base')
    await intake.register(holder)

    async def bars_request(dataset_id, granularity=Granularity.ONE_DAY):
        return await _request(
            intake.engine,
            created,
            run_identity,
            own_symbol,
            holder=holder,
            dataset_id=dataset_id,
            granularity=granularity,
        )

    await intake.deliver(_delivery(await bars_request(entry.id), holder, ID_A, 1, 0))
    # Newer than A, all naming another id: a FAILED attempt of this dataset, an accepted attempt of the other
    # dataset, and a corporate-actions attempt row moved onto this dataset (its id NULL by kind).
    failed = await bars_request(entry.id)
    await intake.deliver(_delivery(failed, holder, ID_B, 2, outcome=DeliveryOutcome.FAILED))
    await intake.deliver(_delivery(await bars_request(other.id, Granularity.ONE_HOUR), holder, ID_B, 3, 0))
    assert (await _entry(intake.engine, other.id)).collect is True, 'the other dataset accepted its own first id'
    ca_id = await _ca_request(intake.engine, created, run_identity, holder=holder)
    record = CorporateActionRecord(
        vendor_action_id=f'p50-{ca_id.hex}', action_type='name_change', raw_payload=ca_id.hex.encode()
    )
    await intake.deliver(ca_report(ca_id, [record], instance_id=holder, vendor_asset_id=ID_B))
    async with intake.engine.begin() as conn:
        await conn.execute(
            sa.update(RUNS)
            .where(RUNS.c.request_id == ca_id)
            .values(dataset_id=entry.id, finished_at=AS_OF + timedelta(hours=10))
        )
    (ca_run,) = await _runs(intake.engine, ca_id)
    assert (ca_run.granularity, ca_run.vendor_asset_id) == (None, None)

    request_id = await bars_request(entry.id)
    answer = await intake.deliver(_delivery(request_id, holder, ID_A, 20, 1))

    assert answer.verdict is FINAL
    (run,) = await _runs(intake.engine, request_id)
    assert (run.state, run.error_reason) == (IngestRunState.SUCCEEDED, None)
    assert (await _entry(intake.engine, entry.id)).collect is True
    assert await _bar_days(intake.engine, entry.id) == [0, 1]


async def test_pg_p50f_a_refresh_dropped_while_stopped_stores_no_id_and_is_never_the_base(
    intake, created, run_identity, insert_entry, own_symbol, instance_id
):
    """P50f (tj-7zjov1.163), ADR tj-26jt9w addendum 16, on Postgres: a dropped bars attempt row stores NULL id.

    An operator Stop, then a refresh already in flight delivers the new holder's id: the drop rule runs first, so it
    is dropped unjudged, written EMPTY with its dataset id and NULL vendor_asset_id, and the REAL base query still
    answers the last accepted id. After a Resume the new holder's delivery stops the dataset naming the earlier id, and
    after another Resume the earlier id runs on.
    """
    entry = insert_entry(
        asset_symbol=own_symbol,
        owner=OPERATOR_PRINCIPAL,
        granularity=Granularity.ONE_DAY,
        feed=Feed.IEX,
        update_type=UpdateType.DAILY.value,
        expiry_type=ExpiryType.ROLLING.value,
        collect=True,
    )
    holder = instance_id('p50fdrop')
    await intake.register(holder)

    async def refresh(vendor_asset_id: str, hours: int, *days: int):
        request_id = await _request(
            intake.engine, created, run_identity, own_symbol, holder=holder, dataset_id=entry.id
        )
        return request_id, await intake.deliver(_delivery(request_id, holder, vendor_asset_id, hours, *days))

    async def set_collecting(collect: bool):
        async with AsyncSession(intake.engine, expire_on_commit=False) as db:
            return await entry_crud.operator_set_collecting(db, entry.id, collect, None)

    async def base() -> str | None:
        async with AsyncSession(intake.engine) as db:
            return await last_accepted_vendor_asset_id(db, entry.id)

    await refresh(ID_A, 1, 0)
    assert await set_collecting(False) is False

    # The refresh in flight when the entry was stopped: dropped, nothing compared, no bar.
    dropped, answer = await refresh(ID_B, 2, 1)

    assert answer.verdict is FINAL and answer.attempt_count == 1
    (run,) = await _runs(intake.engine, dropped)
    assert (run.state, run.bars_written, run.verdict, run.dataset_id) == (IngestRunState.EMPTY, 0, 'FINAL', entry.id)
    assert run.vendor_asset_id is None, 'a dropped (unjudged) delivery stored its vendor asset id'
    async with intake.engine.connect() as conn:
        raw = (
            await conn.execute(
                sa.text(
                    'SELECT state::text AS state, vendor_asset_id IS NULL AS no_id, dataset_id FROM ingest_run '
                    'WHERE request_id = :id'
                ),
                {'id': dropped},
            )
        ).one()
    assert (raw.state, raw.no_id, raw.dataset_id) == ('EMPTY', True, entry.id)
    row = await _request_row(intake.engine, dropped)
    assert (row.state, row.failure_cause) == (IngestRequestState.EMPTY, IngestRequestFailureCause.DISCARDED.value)
    assert await _bar_days(intake.engine, entry.id) == [0]
    assert await base() == ID_A, 'the real base query read the dropped attempt'

    # Resume: a re-check against the last accepted id. The new holder stops the dataset again.
    assert await set_collecting(True) is True
    request_id, answer = await refresh(ID_B, 3, 2)
    await _assert_stopped(intake.engine, entry.id, request_id, answer, ID_A, ID_B)
    assert await _bar_days(intake.engine, entry.id) == [0], 'the new holder reached the old series'

    # Resume again: the earlier id is accepted.
    assert await set_collecting(True) is True
    request_id, _ = await refresh(ID_A, 4, 3)
    (run,) = await _runs(intake.engine, request_id)
    assert (run.state, run.error_reason, run.vendor_asset_id) == (IngestRunState.SUCCEEDED, None, ID_A)
    assert await _bar_days(intake.engine, entry.id) == [0, 3]
