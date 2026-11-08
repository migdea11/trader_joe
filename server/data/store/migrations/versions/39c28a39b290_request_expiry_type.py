"""Request expiry type: nullable ingest_request.expiry_type

Revision ID: 39c28a39b290
Revises: b8e4c1f7a923
Create Date: 2026-10-26 00:00:00.000000

PURELY ADDITIVE (tj-7zjov1.153, P35r). upgrade() adds one column. It reads, rewrites and deletes nothing that exists and
it backfills nothing. No existing column is altered, retyped or dropped, no index or constraint is touched, and no
enum type is created or changed.

WHAT IS ADDED.
  ingest_request.expiry_type   integer, nullable, NO server default. The POST body's expiry type as ExpiryType's integer
                               value, the same representation store_dataset_entry.expiry_type stores. It is the last
                               part of the entry's identity the request row lacked, so the dataset entry can be written
                               from the request row on the first delivered attempt.

WHY NULL, AND WHY NO DEFAULT. NULL means the request carries no expiry type: the DAILY loop (it names its dataset at
request time), an asset list, corporate actions, and every row written before this revision. A default would write a
guessed identity for every such row silently (the entry column's own default is the hazard its comment names), so a row
that does not say stays NULL. This revision writes no value; the one writer is the submit path (tj-7zjov1.28).

DOWNGRADE drops exactly that column. It loses only the values this revision introduces, touches no existing column and
drops no type. It is no recovery path; the store is forward-only and recovery is a restore.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '39c28a39b290'
down_revision: Union[str, None] = 'b8e4c1f7a923'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

REQUEST_TABLE = 'ingest_request'


def upgrade() -> None:
    op.add_column(REQUEST_TABLE, sa.Column('expiry_type', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column(REQUEST_TABLE, 'expiry_type')
