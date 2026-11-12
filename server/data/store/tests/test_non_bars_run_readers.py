"""The readers of ingest_run's label columns handle NULL (tj-7zjov1.31, P18).

THE DESIGN is the architect's 20:17 UTC note on tj-7zjov1.31 (from the P34 escalation rulings): revision c7e19a4d5b28
makes asset_symbol, asset_type, data_type, granularity and update_type nullable, and in the same bead every reader of
those columns handles NULL: IngestRunRecord's fields are optional, the UI's run label drops what is absent, and the
DAILY refresh's history reads only bars attempts. A corporate-actions attempt row keeps its symbol and NULL for the
other four; an asset-list attempt (P17) will hold NULL in all five. Never a sentinel.

Here: IngestRunRecord.from_row over a row holding NULL labels, and ingest_run_message's label for each shape. The
refresh history is test_finished_scheduled_runs_unattempted.py's (a scheduled corporate-actions attempt row); the intake
writing the row is test_delivery_intake.py's.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity, UpdateType
from data.store.app.database.crud.ingest_run import IngestRunRecord
from data.store.app.database.models.ingest_run import IngestRun, IngestRunState, IngestRunTrigger
from routers.data_store.ui_mapping import ingest_run_message


pytestmark = pytest.mark.data_store

STARTED = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
LABEL_COLUMNS = ('asset_symbol', 'asset_type', 'data_type', 'granularity', 'update_type')


def _row(**overrides: Any) -> SimpleNamespace:
    """An ingest_run row as a RETURNING row reads it: every model column by name, a bars attempt by default."""
    values = {column.name: None for column in IngestRun.__table__.columns}
    values.update(
        id=uuid.uuid4(),
        request_id=uuid.uuid4(),
        trigger=IngestRunTrigger.SCHEDULE.value,
        actor='scheduler',
        asset_symbol='VFV',
        asset_type=AssetType.STOCK,
        data_type=DataType.MARKET_ACTIVITY,
        source=DataSource.ALPACA_API,
        granularity=Granularity.ONE_DAY,
        update_type=UpdateType.DAILY.value,
        feed=Feed.IEX,
        state=IngestRunState.SUCCEEDED.value,
        bars_written=0,
        started_at=STARTED,
        finished_at=STARTED,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_the_label_columns_the_readers_must_handle_are_exactly_the_nullable_ones():
    """The five the design relaxes are nullable on the model, and source, the sixth label column, is not."""
    table = IngestRun.__table__
    assert all(table.c[name].nullable for name in LABEL_COLUMNS)
    assert not table.c.source.nullable


@pytest.mark.parametrize(
    ('symbol', 'case'),
    [pytest.param('XLNX', 'corporate-actions', id='corporate-actions'), pytest.param(None, 'asset-list', id='none')],
)
def test_from_row_reads_null_labels_as_none_never_a_sentinel(symbol, case):
    row = _row(asset_symbol=symbol, asset_type=None, data_type=None, granularity=None, update_type=None, feed=None)

    record = IngestRunRecord.from_row(row)

    assert (record.asset_symbol, record.asset_type, record.data_type) == (symbol, None, None), case
    assert (record.granularity, record.update_type, record.feed) == (None, None, None)
    assert record.source is DataSource.ALPACA_API


def test_from_row_still_types_a_bars_rows_update_type():
    record = IngestRunRecord.from_row(_row())

    assert type(record.update_type) is UpdateType and record.update_type is UpdateType.DAILY
    assert record.granularity is Granularity.ONE_DAY


@pytest.mark.parametrize(
    ('symbol', 'granularity', 'label'),
    [
        pytest.param('VFV', Granularity.ONE_DAY, 'VFV 1day', id='bars'),
        pytest.param('XLNX', None, 'XLNX', id='corporate-actions'),
        pytest.param(None, None, '', id='asset-list'),
    ],
)
def test_the_ui_run_label_drops_the_absent_labels(symbol, granularity, label):
    """The symbol and the bar width, each only when present: no 'None', no stray space."""
    record = IngestRunRecord.from_row(
        _row(
            asset_symbol=symbol,
            granularity=granularity,
            asset_type=None if granularity is None else AssetType.STOCK,
            data_type=None if granularity is None else DataType.MARKET_ACTIVITY,
            update_type=None if granularity is None else UpdateType.DAILY.value,
        )
    )

    message = ingest_run_message(record)

    assert message.label == label
    assert 'None' not in message.label
