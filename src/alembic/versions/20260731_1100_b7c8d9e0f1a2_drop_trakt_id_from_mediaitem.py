"""drop trakt_id from mediaitem

Trakt has been replaced by TMDB as the metadata indexer, so nothing populates
this column any more. Item ids are now derived from the IMDb id rather than the
Trakt id - see MediaItem.__generate_composite_key.

Revision ID: b7c8d9e0f1a2
Revises: a1b2c3d4e5f6
Create Date: 2026-07-31 11:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b7c8d9e0f1a2'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('MediaItem', schema=None) as batch_op:
        batch_op.drop_column('trakt_id')


def downgrade() -> None:
    with op.batch_alter_table('MediaItem', schema=None) as batch_op:
        batch_op.add_column(sa.Column('trakt_id', sa.String(), nullable=True))
