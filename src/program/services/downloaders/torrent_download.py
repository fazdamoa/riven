"""TorrentDownload model for tracking active Real-Debrid downloads."""

from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, List, TYPE_CHECKING

import sqlalchemy
from sqlalchemy import Index, and_
from sqlalchemy.orm import Mapped, mapped_column, relationship

from program.db.db import db

if TYPE_CHECKING:
    from program.media.item import MediaItem


class TorrentDownloadStatus(str, Enum):
    """Status of a torrent download in Real-Debrid."""
    PENDING = "pending"           # Added to RD, waiting for processing
    DOWNLOADING = "downloading"   # Actively downloading in RD
    READY = "ready"              # Download complete, ready for symlink
    COMPLETED = "completed"       # Fully processed and symlinked
    FAILED = "failed"            # Failed after retries exhausted
    LEGAL_ERROR = "legal_error"  # 451 - blocked for legal reasons
    SKIPPED = "skipped"          # Skipped (e.g., no valid files)


class TorrentDownload(db.Model):
    """
    Tracks the state of a single torrent download for a media item.
    
    Design principles:
    - ONE active download per media item at a time
    - Clear status transitions
    - Retry logic with cooldowns
    - Historical record of failed attempts
    """
    __tablename__ = "TorrentDownload"
    
    id: Mapped[int] = mapped_column(sqlalchemy.Integer, primary_key=True, autoincrement=True)
    
    # Link to the media item (movie, episode, etc.)
    media_item_id: Mapped[str] = mapped_column(
        sqlalchemy.String, 
        sqlalchemy.ForeignKey("MediaItem.id", ondelete="CASCADE"),
        nullable=False
    )
    
    # Torrent identification
    infohash: Mapped[str] = mapped_column(sqlalchemy.String(40), nullable=False)
    torrent_id: Mapped[Optional[str]] = mapped_column(sqlalchemy.String, nullable=True)  # RD's torrent ID
    
    # Status tracking
    status: Mapped[str] = mapped_column(sqlalchemy.String(20), nullable=False, default=TorrentDownloadStatus.PENDING.value)
    
    # Torrent metadata (for logging/debugging)
    raw_title: Mapped[Optional[str]] = mapped_column(sqlalchemy.String, nullable=True)
    rank: Mapped[Optional[int]] = mapped_column(sqlalchemy.Integer, nullable=True)
    
    # Timestamps
    started_at: Mapped[Optional[datetime]] = mapped_column(sqlalchemy.DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(sqlalchemy.DateTime, nullable=True)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(sqlalchemy.DateTime, nullable=True)
    
    # Error handling
    error_message: Mapped[Optional[str]] = mapped_column(sqlalchemy.String, nullable=True)
    retry_after: Mapped[Optional[datetime]] = mapped_column(sqlalchemy.DateTime, nullable=True)
    attempt_count: Mapped[int] = mapped_column(sqlalchemy.Integer, nullable=False, default=1)
    
    # Relationship back to MediaItem
    media_item: Mapped["MediaItem"] = relationship("MediaItem", backref="torrent_downloads", foreign_keys=[media_item_id])
    
    __table_args__ = (
        Index("ix_torrentdownload_media_item_id", "media_item_id"),
        Index("ix_torrentdownload_infohash", "infohash"),
        Index("ix_torrentdownload_status", "status"),
        Index("ix_torrentdownload_retry_after", "retry_after"),
    )
    
    def __repr__(self) -> str:
        return f"<TorrentDownload {self.id}: {self.infohash[:8]}... status={self.status}>"
    
    @property
    def is_active(self) -> bool:
        """Check if this download is currently active (not terminal state)."""
        return self.status in [
            TorrentDownloadStatus.PENDING.value,
            TorrentDownloadStatus.DOWNLOADING.value,
            TorrentDownloadStatus.READY.value,
        ]
    
    @property
    def is_terminal(self) -> bool:
        """Check if this download has reached a terminal state."""
        return self.status in [
            TorrentDownloadStatus.COMPLETED.value,
            TorrentDownloadStatus.FAILED.value,
            TorrentDownloadStatus.LEGAL_ERROR.value,
            TorrentDownloadStatus.SKIPPED.value,
        ]
    
    @property
    def can_retry(self) -> bool:
        """Check if this download can be retried."""
        if self.status not in [TorrentDownloadStatus.FAILED.value]:
            return False
        if self.retry_after and datetime.now() < self.retry_after:
            return False
        return True
    
    def mark_downloading(self, torrent_id: str) -> None:
        """Mark download as actively downloading."""
        self.status = TorrentDownloadStatus.DOWNLOADING.value
        self.torrent_id = torrent_id
        self.last_checked_at = datetime.now()
        if not self.started_at:
            self.started_at = datetime.now()
    
    def mark_ready(self) -> None:
        """Mark download as ready for processing (cached/downloaded in RD)."""
        self.status = TorrentDownloadStatus.READY.value
        self.last_checked_at = datetime.now()
    
    def mark_completed(self) -> None:
        """Mark download as fully completed (symlinked)."""
        self.status = TorrentDownloadStatus.COMPLETED.value
        self.completed_at = datetime.now()
    
    def mark_failed(self, error_message: str, cooldown_hours: int = 24) -> None:
        """Mark download as failed with a retry cooldown."""
        self.status = TorrentDownloadStatus.FAILED.value
        self.error_message = error_message
        self.retry_after = datetime.now() + timedelta(hours=cooldown_hours)
        self.last_checked_at = datetime.now()
    
    def mark_legal_error(self, error_message: str) -> None:
        """Mark download as blocked for legal reasons (451 error)."""
        self.status = TorrentDownloadStatus.LEGAL_ERROR.value
        self.error_message = error_message
        self.last_checked_at = datetime.now()
    
    def mark_skipped(self, reason: str) -> None:
        """Mark download as skipped (e.g., no valid files)."""
        self.status = TorrentDownloadStatus.SKIPPED.value
        self.error_message = reason
        self.last_checked_at = datetime.now()
    
    @classmethod
    def get_active_for_item(cls, session, media_item_id: str) -> Optional["TorrentDownload"]:
        """Get the active download for a media item, if any."""
        return session.query(cls).filter(
            and_(
                cls.media_item_id == media_item_id,
                cls.status.in_([
                    TorrentDownloadStatus.PENDING.value,
                    TorrentDownloadStatus.DOWNLOADING.value,
                    TorrentDownloadStatus.READY.value,
                ])
            )
        ).first()
    
    @classmethod
    def get_failed_hashes_for_item(cls, session, media_item_id: str) -> List[str]:
        """Get list of failed/legal-error infohashes for a media item."""
        results = session.query(cls.infohash).filter(
            and_(
                cls.media_item_id == media_item_id,
                cls.status.in_([
                    TorrentDownloadStatus.FAILED.value,
                    TorrentDownloadStatus.LEGAL_ERROR.value,
                    TorrentDownloadStatus.SKIPPED.value,
                ])
            )
        ).all()
        return [r[0] for r in results]
    
    @classmethod
    def get_all_active(cls, session) -> List["TorrentDownload"]:
        """Get all active downloads across all items."""
        return session.query(cls).filter(
            cls.status.in_([
                TorrentDownloadStatus.PENDING.value,
                TorrentDownloadStatus.DOWNLOADING.value,
            ])
        ).all()
    
    @classmethod
    def get_ready_downloads(cls, session) -> List["TorrentDownload"]:
        """Get all downloads that are ready for processing."""
        return session.query(cls).filter(
            cls.status == TorrentDownloadStatus.READY.value
        ).all()
    
    @classmethod
    def create_for_item(
        cls,
        session,
        media_item_id: str,
        infohash: str,
        raw_title: str = None,
        rank: int = None
    ) -> "TorrentDownload":
        """Create a new download tracking entry."""
        download = cls(
            media_item_id=media_item_id,
            infohash=infohash,
            status=TorrentDownloadStatus.PENDING.value,
            raw_title=raw_title,
            rank=rank,
            started_at=datetime.now(),
        )
        session.add(download)
        return download
    
    @classmethod
    def cleanup_old_records(cls, session, days: int = 30) -> int:
        """Remove old completed/failed records older than specified days."""
        cutoff = datetime.now() - timedelta(days=days)
        deleted = session.query(cls).filter(
            and_(
                cls.status.in_([
                    TorrentDownloadStatus.COMPLETED.value,
                    TorrentDownloadStatus.FAILED.value,
                    TorrentDownloadStatus.LEGAL_ERROR.value,
                    TorrentDownloadStatus.SKIPPED.value,
                ]),
                cls.completed_at < cutoff
            )
        ).delete(synchronize_session=False)
        return deleted
