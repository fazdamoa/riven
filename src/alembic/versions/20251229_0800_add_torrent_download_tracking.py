"""Add TorrentDownload tracking table

Revision ID: a1b2c3d4e5f6
Revises: 834cba7d26b4
Create Date: 2025-12-29 08:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '834cba7d26b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    # Check if table already exists (idempotent migration)
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if 'TorrentDownload' in inspector.get_table_names():
        return
    
    # Create the TorrentDownload table for tracking active downloads
    op.create_table(
        'TorrentDownload',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('media_item_id', sa.String(), sa.ForeignKey('MediaItem.id', ondelete='CASCADE'), nullable=False),
        sa.Column('infohash', sa.String(40), nullable=False),
        sa.Column('torrent_id', sa.String(), nullable=True),  # RD's torrent ID
        sa.Column('status', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('raw_title', sa.String(), nullable=True),
        sa.Column('rank', sa.Integer(), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('last_checked_at', sa.DateTime(), nullable=True),
        sa.Column('error_message', sa.String(), nullable=True),
        sa.Column('retry_after', sa.DateTime(), nullable=True),
        sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='1'),
    )
    
    # Create indexes for efficient queries
    op.create_index('ix_torrentdownload_media_item_id', 'TorrentDownload', ['media_item_id'])
    op.create_index('ix_torrentdownload_infohash', 'TorrentDownload', ['infohash'])
    op.create_index('ix_torrentdownload_status', 'TorrentDownload', ['status'])
    op.create_index('ix_torrentdownload_retry_after', 'TorrentDownload', ['retry_after'])


def downgrade():
    op.drop_index('ix_torrentdownload_retry_after', 'TorrentDownload')
    op.drop_index('ix_torrentdownload_status', 'TorrentDownload')
    op.drop_index('ix_torrentdownload_infohash', 'TorrentDownload')
    op.drop_index('ix_torrentdownload_media_item_id', 'TorrentDownload')
    op.drop_table('TorrentDownload')
