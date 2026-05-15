"""
Downloader Service - Refactored for Single-Torrent Downloads

Key principles:
1. ONE torrent per media item at a time
2. Use database tracking (TorrentDownload) for state persistence
3. Proper error handling with categorized errors
4. Clean separation between cache checking and torrent addition
"""

import time
from datetime import datetime, timedelta
from typing import Dict, Generator, List, Optional

from loguru import logger
from sqlalchemy.orm import Session

from program.db.db import db
from program.media.item import Episode, MediaItem, Movie, Season, Show
from program.media.state import States
from program.media.stream import Stream
from program.services.downloaders.models import (
    DebridFile,
    DownloadedTorrent,
    InvalidDebridFileException,
    NoMatchingFilesException,
    NotCachedException,
    ParsedFileData,
    TorrentContainer,
    TorrentInfo,
)
from program.services.downloaders.shared import parse_filename
from program.services.downloaders.torrent_download import TorrentDownload, TorrentDownloadStatus

from .alldebrid import AllDebridDownloader
from .realdebrid import RealDebridDownloader, RealDebridError, RealDebridErrorType
from .torbox import TorBoxDownloader

# In-memory rate limiting to prevent infinite loops
_download_attempts: Dict[str, tuple] = {}  # item_id -> (attempt_count, last_attempt_time)
_MAX_ATTEMPTS_PER_MINUTE = 3
_HARD_COOLDOWN_MINUTES = 5


class Downloader:
    """
    Downloader service with single-torrent-at-a-time logic.
    
    Flow:
    1. Check if item already has an active download
    2. If yes: Check status and process if ready
    3. If no: Find best stream, add ONE torrent, track it
    4. Handle errors gracefully with proper categorization
    """

    def __init__(self):
        self.key = "downloader"
        self.initialized = False
        self.services = {
            RealDebridDownloader: RealDebridDownloader(),
            AllDebridDownloader: AllDebridDownloader(),
            TorBoxDownloader: TorBoxDownloader(),
        }
        self.service = next((service for service in self.services.values() if service.initialized), None)
        self.initialized = self.validate()

    def validate(self) -> bool:
        if self.service is None:
            logger.error("No downloader service initialized. Please initialize a downloader service.")
            return False
        return True

    def run(self, item: MediaItem) -> Generator[MediaItem, None, None]:
        """
        Main download logic for a media item.
        
        Flow:
        1. Check for existing active download -> process if ready
        2. If no active download, find best available stream
        3. Add ONE torrent and track it
        4. Handle errors and move to next stream if needed
        """
        # HARD RATE LIMIT - Prevent infinite loops at the source
        global _download_attempts
        item_id = str(item.id)
        now = datetime.now()
        
        if item_id in _download_attempts:
            attempt_count, first_attempt_time = _download_attempts[item_id]
            time_since_first = (now - first_attempt_time).total_seconds()
            
            # Reset counter if more than a minute has passed
            if time_since_first > 60:
                _download_attempts[item_id] = (1, now)
            elif attempt_count >= _MAX_ATTEMPTS_PER_MINUTE:
                # Too many attempts - apply hard cooldown
                cooldown_until = first_attempt_time + timedelta(minutes=_HARD_COOLDOWN_MINUTES)
                if now < cooldown_until:
                    remaining = (cooldown_until - now).total_seconds()
                    # Only log once per cooldown period
                    if attempt_count == _MAX_ATTEMPTS_PER_MINUTE:
                        logger.warning(
                            f"Rate limited {item.log_string}: {attempt_count} attempts in {time_since_first:.0f}s, "
                            f"cooldown for {remaining:.0f}s"
                        )
                    _download_attempts[item_id] = (attempt_count + 1, first_attempt_time)
                    # DON'T yield the item - this prevents state_transition from re-queuing it
                    # The item will be picked up again on the next scheduler cycle
                    return
                else:
                    # Cooldown expired, reset
                    _download_attempts[item_id] = (1, now)
            else:
                _download_attempts[item_id] = (attempt_count + 1, first_attempt_time)
        else:
            _download_attempts[item_id] = (1, now)
        
        logger.debug(f"Starting download process for {item.log_string} ({item.id})")
        
        # Skip if already completed
        if item.last_state in [States.Completed, States.Symlinked, States.Downloaded]:
            logger.debug(f"Skipping {item.log_string}: already in state {item.last_state}")
            yield item
            return
        
        # Skip if blocked/paused
        if item.is_parent_blocked():
            logger.debug(f"Skipping {item.log_string}: parent is blocked")
            yield item
            return
        
        try:
            with db.Session() as session:
                # Step 0: Check for cooldown (all streams exhausted)
                try:
                    cooldown = session.query(TorrentDownload).filter(
                        TorrentDownload.media_item_id == item.id,
                        TorrentDownload.raw_title == "__NO_STREAMS_COOLDOWN__"
                    ).first()
                except Exception as e:
                    # Table might not exist yet (migration not run)
                    logger.debug(f"Could not query TorrentDownload table: {e}")
                    cooldown = None
                
                if cooldown and cooldown.retry_after and cooldown.retry_after > datetime.now():
                    remaining = (cooldown.retry_after - datetime.now()).total_seconds() / 60
                    if remaining > 5:  # Only log if more than 5 minutes remaining
                        logger.debug(
                            f"Skipping {item.log_string}: in cooldown for {remaining:.0f} more minutes "
                            f"(attempt {cooldown.attempt_count})"
                        )
                    yield item
                    return
                
                # Step 1: Check for existing active download
                try:
                    active_download = TorrentDownload.get_active_for_item(session, item.id)
                except Exception as e:
                    logger.debug(f"Could not check active downloads: {e}")
                    active_download = None
                
                if active_download:
                    # Process existing download
                    result = self._process_existing_download(session, item, active_download)
                    if result:
                        # Download completed successfully
                        yield item
                        return
                    elif active_download.status == TorrentDownloadStatus.READY.value:
                        # Ready but couldn't process - something's wrong
                        logger.warning(f"Download ready but couldn't process for {item.log_string}")
                        active_download.mark_failed("Could not process completed download")
                        session.commit()
                    elif active_download.is_active:
                        # Still downloading - yield and check again later
                        logger.debug(f"Download still in progress for {item.log_string}")
                        yield item
                        return
                
                # Step 2: No active download, check for streams
                if not item.streams:
                    # No streams at all - apply cooldown to prevent loop
                    logger.debug(f"No streams for {item.log_string}, applying cooldown")
                    self._handle_no_available_streams(session, item, 0)
                    yield item
                    return
                
                # Get failed hashes to skip
                try:
                    failed_hashes = TorrentDownload.get_failed_hashes_for_item(session, item.id)
                except Exception:
                    failed_hashes = []
                blacklisted_hashes = {s.infohash for s in item.blacklisted_streams}
                skip_hashes = set(failed_hashes) | blacklisted_hashes
                
                # Find best available stream
                best_stream = self._find_best_stream(item.streams, skip_hashes)
                
                if not best_stream:
                    # All streams exhausted - apply backoff to prevent infinite loop
                    logger.debug(f"All streams exhausted for {item.log_string}, applying cooldown")
                    self._handle_no_available_streams(session, item, len(skip_hashes))
                    yield item
                    return
                
                # Found a stream! Clear any existing cooldown
                if cooldown:
                    session.delete(cooldown)
                    session.commit()
                
                # Step 3: Try to download the best stream
                result = self._start_download(session, item, best_stream)
                session.commit()
                
                if result:
                    logger.log("DEBRID", f"Download completed for {item.log_string}")
                
        except Exception as e:
            logger.error(f"Error in download process for {item.log_string}: {e}")
        
        yield item

    def _handle_no_available_streams(self, session: Session, item: MediaItem, failed_count: int):
        """
        Handle the case when all streams are failed/blacklisted.
        Creates a cooldown record to prevent immediate retry loops.
        """
        # Check if there's already a cooldown record
        existing_cooldown = session.query(TorrentDownload).filter(
            TorrentDownload.media_item_id == item.id,
            TorrentDownload.raw_title == "__NO_STREAMS_COOLDOWN__"
        ).first()
        
        if existing_cooldown:
            # Count how many times we've tried
            attempt_count = existing_cooldown.attempt_count
            
            # Check if cooldown is still active
            if existing_cooldown.retry_after and existing_cooldown.retry_after > datetime.now():
                remaining = (existing_cooldown.retry_after - datetime.now()).total_seconds() / 60
                logger.debug(
                    f"No available streams for {item.log_string}, "
                    f"cooldown active for {remaining:.1f} more minutes"
                )
                return
            
            # Cooldown expired, increment attempt count
            attempt_count += 1
            existing_cooldown.attempt_count = attempt_count
            
            # Check if we should give up entirely (after 10 attempts)
            MAX_RETRIES = 10
            if attempt_count >= MAX_RETRIES:
                logger.warning(
                    f"Giving up on {item.log_string} after {attempt_count} attempts "
                    f"with no available streams ({failed_count} failed/blacklisted). "
                    f"Remove from library or reset to try again."
                )
                # Set a very long cooldown (7 days)
                existing_cooldown.retry_after = datetime.now() + timedelta(days=7)
                existing_cooldown.error_message = f"All streams exhausted after {attempt_count} attempts"
                session.commit()
                return
            
            # Calculate exponential backoff: 2, 4, 8, 16, 32, 64, 128, 256, 512 minutes
            backoff_minutes = min(2 ** attempt_count, 512)
            existing_cooldown.retry_after = datetime.now() + timedelta(minutes=backoff_minutes)
            existing_cooldown.error_message = f"No streams available (attempt {attempt_count})"
            session.commit()
            
            logger.debug(
                f"No available streams for {item.log_string} "
                f"(attempt {attempt_count}/{MAX_RETRIES}, {failed_count} failed/blacklisted, "
                f"next retry in {backoff_minutes} minutes)"
            )
        else:
            # First time hitting no streams - create cooldown record
            cooldown = TorrentDownload(
                media_item_id=item.id,
                infohash="0" * 40,  # Dummy hash
                raw_title="__NO_STREAMS_COOLDOWN__",
                status=TorrentDownloadStatus.FAILED.value,
                rank=0,
                started_at=datetime.now(),
                attempt_count=1,
                retry_after=datetime.now() + timedelta(minutes=2),
                error_message=f"No streams available ({failed_count} failed/blacklisted)"
            )
            session.add(cooldown)
            session.commit()
            
            logger.debug(
                f"No available streams for {item.log_string} "
                f"({failed_count} failed/blacklisted, next retry in 2 minutes)"
            )

    def _process_existing_download(
        self, 
        session: Session, 
        item: MediaItem, 
        download: TorrentDownload
    ) -> bool:
        """
        Process an existing active download.
        
        Returns:
            True if download completed successfully, False otherwise
        """
        logger.debug(f"Checking existing download {download.infohash[:8]}... for {item.log_string}")
        
        # Get current status from Real-Debrid
        if not download.torrent_id:
            # Torrent wasn't added yet - this shouldn't happen but handle it
            logger.warning(f"Active download has no torrent_id: {download.id}")
            download.mark_failed("No torrent ID")
            return False
        
        try:
            status = self.service.get_torrent_status(download.torrent_id)
            download.last_checked_at = datetime.now()
            
            if status.is_cached:
                # Download complete! Process it
                logger.log("DEBRID", f"Torrent ready for {item.log_string}")
                download.mark_ready()
                
                # Process the completed torrent
                container = self.service.process_completed_torrent(
                    download.torrent_id,
                    download.infohash,
                    item.type
                )
                
                if container and self._update_item_attributes(item, container, download):
                    download.mark_completed()
                    session.commit()
                    return True
                else:
                    logger.warning(f"Could not match files for {item.log_string}")
                    download.mark_skipped("No matching files found")
                    session.commit()
                    return False
            
            elif status.is_downloading:
                # Still downloading - update status
                download.status = TorrentDownloadStatus.DOWNLOADING.value
                logger.debug(f"Download in progress for {item.log_string}: {status.progress}%")
                return False
            
            elif status.needs_file_selection:
                # Select files and continue
                if self.service.select_video_files(download.torrent_id):
                    download.status = TorrentDownloadStatus.DOWNLOADING.value
                else:
                    download.mark_failed("Could not select video files")
                return False
            
            elif status.is_error:
                # Torrent failed
                logger.warning(f"Torrent error for {item.log_string}: {status.status}")
                download.mark_failed(f"Torrent error: {status.status}")
                return False
            
            else:
                # Unknown status
                logger.debug(f"Unknown torrent status for {item.log_string}: {status.status}")
                return False
                
        except RealDebridError as e:
            if e.error_type == RealDebridErrorType.NOT_FOUND:
                # Torrent was deleted from RD - treat as skipped so the infohash
                # is added to failed_hashes and the next stream is tried promptly
                logger.warning(f"Torrent not found in RD for {item.log_string}, marking as skipped")
                download.mark_skipped("Torrent removed from Real-Debrid")
            else:
                logger.error(f"Error checking torrent status: {e}")
                download.mark_failed(str(e))
            session.commit()
            return False

    def _find_best_stream(self, streams: List[Stream], skip_hashes: set) -> Optional[Stream]:
        """
        Find the best available stream, skipping failed/blacklisted ones.
        
        Streams are already sorted by rank (highest first).
        """
        for stream in streams:
            if stream.infohash not in skip_hashes:
                return stream
        return None

    def _start_download(
        self, 
        session: Session, 
        item: MediaItem, 
        stream: Stream
    ) -> bool:
        """
        Start a new download for a stream.
        
        Flow:
        1. Create and immediately flush a PENDING tracking record so concurrent
           calls to get_active_for_item() see it before any API work begins.
        2. Add the torrent to RD.
        3. Handle file selection if needed.
        4. If RD reports it cached/downloaded, process immediately.
           Otherwise mark as downloading and let check_active_downloads poll.
        
        Returns:
            True if completed immediately (cached), False if downloading/failed
        """
        infohash = stream.infohash
        logger.log("DEBRID", f"Attempting download: {stream.raw_title} [{infohash[:8]}...] (rank: {stream.rank})")
        
        # Create and flush tracking record immediately so that any concurrent
        # invocation of run() for the same item sees an active download and
        # exits early, rather than racing into _start_download again.
        download = TorrentDownload.create_for_item(
            session=session,
            media_item_id=item.id,
            infohash=infohash,
            raw_title=stream.raw_title,
            rank=stream.rank
        )
        session.flush()  # persist to DB within this transaction immediately
        
        try:
            # Add torrent to RD — RD deduplicates by hash, so if it already
            # exists in the account we just get back the existing torrent ID.
            torrent_id, status = self.service.add_torrent(infohash)
            download.torrent_id = torrent_id
            
            # Handle file selection if needed
            if status.needs_file_selection:
                if not self.service.select_video_files(torrent_id):
                    download.mark_skipped("No video files found")
                    return False
                status = self.service.get_torrent_status(torrent_id)
            
            # Check final status
            if status.is_cached:
                return self._process_cached_torrent(session, item, download, torrent_id)
            else:
                # Queued or actively downloading — come back later via check_active_downloads
                download.mark_downloading(torrent_id)
                logger.log("DEBRID", f"Started download (not cached): {stream.raw_title}")
                return False
                
        except RealDebridError as e:
            self._handle_download_error(download, e, stream)
            return False
        except Exception as e:
            logger.error(f"Unexpected error starting download: {e}")
            download.mark_failed(str(e))
            return False

    def _process_cached_torrent(
        self, 
        session: Session, 
        item: MediaItem, 
        download: TorrentDownload,
        torrent_id: str
    ) -> bool:
        """
        Process a cached/completed torrent.
        
        Returns:
            True if processed successfully, False otherwise
        """
        download.mark_ready()
        
        container = self.service.process_completed_torrent(
            torrent_id,
            download.infohash,
            item.type
        )
        
        if container and self._update_item_attributes(item, container, download):
            download.mark_completed()
            logger.log("DEBRID", f"Downloaded (cached): {download.raw_title}")
            return True
        else:
            download.mark_skipped("No matching files found")
            logger.debug(f"Cached torrent has no matching files for {item.log_string}")
            return False

    def _handle_download_error(
        self, 
        download: TorrentDownload, 
        error: RealDebridError,
        stream: Stream
    ):
        """Handle download errors with proper categorization."""
        
        if error.error_type == RealDebridErrorType.LEGAL_BLOCKED:
            # 451 - Legal block, don't retry
            download.mark_legal_error(error.message)
            logger.warning(f"Torrent blocked (451): {stream.raw_title}")
        
        elif error.error_type == RealDebridErrorType.RATE_LIMITED:
            # 429 - Rate limited, retry after cooldown
            download.mark_failed(error.message, cooldown_hours=1)
            logger.warning(f"Rate limited, will retry in 1 hour: {stream.raw_title}")
        
        elif error.error_type == RealDebridErrorType.NOT_FOUND:
            # 404 - Torrent not found
            download.mark_failed(error.message, cooldown_hours=24)
            logger.debug(f"Torrent not found: {stream.raw_title}")
        
        elif error.error_type == RealDebridErrorType.INVALID_TORRENT:
            # 400 - Invalid torrent
            download.mark_skipped(error.message)
            logger.debug(f"Invalid torrent: {stream.raw_title}")
        
        elif error.error_type == RealDebridErrorType.SERVICE_ERROR:
            # 5xx - Server error, retry later
            download.mark_failed(error.message, cooldown_hours=6)
            logger.warning(f"Service error, will retry in 6 hours: {stream.raw_title}")
        
        else:
            # Unknown error
            download.mark_failed(error.message, cooldown_hours=24)
            logger.error(f"Unknown error: {error.message}")

    def _update_item_attributes(
        self, 
        item: MediaItem, 
        container: TorrentContainer,
        download: TorrentDownload
    ) -> bool:
        """
        Update item attributes with downloaded files.
        
        Returns:
            True if files were matched and item updated, False otherwise
        """
        if not container or not container.files:
            return False
        
        # Get torrent info for folder name
        try:
            torrent_info = self.service.get_torrent_info(download.torrent_id)
        except Exception as e:
            logger.error(f"Failed to get torrent info: {e}")
            return False
        
        # Build downloaded torrent object
        downloaded = DownloadedTorrent(
            id=download.torrent_id,
            infohash=download.infohash,
            container=container,
            info=torrent_info
        )
        
        # Match files to item
        found = self._match_files_to_item(item, downloaded)
        
        if found:
            # Set active_stream for legacy compatibility
            item.active_stream = {
                "infohash": download.infohash,
                "id": download.torrent_id
            }
        
        return found

    def _match_files_to_item(self, item: MediaItem, downloaded: DownloadedTorrent) -> bool:
        """Match downloaded files to the media item."""
        
        # Handle shows - need to get the parent show
        show: Optional[Show] = None
        episode_cap: Optional[int] = None
        
        if item.type in ("show", "season", "episode"):
            if item.type == "show":
                show = item
            elif item.type == "season":
                show = item.parent
            else:  # episode
                show = item.parent.parent
            
            # Calculate episode cap
            method_1 = sum(len(season.episodes) for season in show.seasons)
            try:
                method_2 = show.seasons[-1].episodes[-1].number
            except IndexError:
                method_2 = show.seasons[-2].episodes[-1].number if len(show.seasons) > 1 else 0
            episode_cap = max(method_1, method_2)
        
        found = False
        
        for file in downloaded.container.files:
            file_data: ParsedFileData = parse_filename(file.filename)
            
            # Skip files without episode info for shows
            if item.type in ("show", "season", "episode"):
                if not file_data.episodes:
                    continue
                if 0 in file_data.episodes and len(file_data.episodes) == 1:
                    continue
                if file_data.season == 0:
                    continue
            
            # Match file to item
            if item.type == "movie" and file_data.item_type == "movie":
                self._update_media_item(item, file, downloaded)
                found = True
            
            elif item.type in ("show", "season", "episode") and show:
                season_number = file_data.season
                
                for file_episode in file_data.episodes:
                    if episode_cap and file_episode > episode_cap:
                        continue
                    
                    episode = show.get_episode(file_episode, season_number)
                    if not episode:
                        continue
                    
                    if episode.file:
                        continue
                    
                    if episode.state not in [States.Completed, States.Symlinked, States.Downloaded]:
                        self._update_media_item(episode, file, downloaded)
                        found = True
                
                # Update parent active_stream for shows/seasons
                if found and item.type in ("show", "season"):
                    item.active_stream = {
                        "infohash": downloaded.infohash,
                        "id": downloaded.info.id
                    }
        
        return found

    def _update_media_item(
        self, 
        item: MediaItem, 
        file: DebridFile, 
        downloaded: DownloadedTorrent
    ):
        """Update a single media item with file info."""
        item.file = file.filename
        item.folder = downloaded.info.name
        item.alternative_folder = downloaded.info.alternative_filename
        item.active_stream = {
            "infohash": downloaded.infohash,
            "id": downloaded.info.id
        }

    # =========================================================================
    # BACKGROUND STATUS CHECKER
    # =========================================================================
    
    def check_active_downloads(self) -> int:
        """
        Check status of all active downloads.
        Called periodically by scheduler.
        
        Returns:
            Number of downloads processed
        """
        processed = 0
        
        with db.Session() as session:
            active_downloads = TorrentDownload.get_all_active(session)
            
            for download in active_downloads:
                try:
                    # Get the media item
                    from program.db.db_functions import get_item_by_id
                    item = get_item_by_id(download.media_item_id, session=session)
                    
                    if not item:
                        logger.warning(f"Media item not found for download {download.id}")
                        download.mark_failed("Media item not found")
                        continue
                    
                    # Process the download
                    if self._process_existing_download(session, item, download):
                        # Trigger state transition
                        from program.managers.event_manager import EventManager
                        from program.types import Event
                        
                        item.store_state()
                        # The item will be picked up by the event loop
                    
                    processed += 1
                    
                except Exception as e:
                    logger.error(f"Error checking download {download.id}: {e}")
            
            session.commit()
        
        if processed > 0:
            logger.debug(f"Checked {processed} active downloads")
        
        return processed

    def cleanup_old_downloads(self, days: int = 30) -> int:
        """
        Clean up old completed/failed download records.
        
        Returns:
            Number of records deleted
        """
        with db.Session() as session:
            deleted = TorrentDownload.cleanup_old_records(session, days)
            session.commit()
            
        if deleted > 0:
            logger.info(f"Cleaned up {deleted} old download records")
        
        return deleted

    # =========================================================================
    # LEGACY COMPATIBILITY - Methods for backward compatibility
    # =========================================================================
    
    def get_instant_availability(self, infohash: str, item_type: str) -> Optional[TorrentContainer]:
        """Legacy method - Check if torrent is cached."""
        return self.service.get_instant_availability(infohash, item_type)

    def get_instant_availability_or_download(self, infohash: str, item_type: str) -> Optional[TorrentContainer]:
        """Legacy method - For backward compatibility."""
        return self.service.get_instant_availability_or_download(infohash, item_type)

    def check_instant_availability_quick(self, infohash: str) -> bool:
        """Check if torrent is cached (doesn't add torrent)."""
        if hasattr(self.service, 'check_cache'):
            return self.service.check_cache(infohash)
        return False

    def add_torrent(self, infohash: str) -> str:
        """Legacy method - Add a torrent by infohash."""
        if hasattr(self.service, 'add_torrent_legacy'):
            return self.service.add_torrent_legacy(infohash)
        return self.service.add_torrent(infohash)[0]

    def get_torrent_info(self, torrent_id: str) -> TorrentInfo:
        """Legacy method - Get information about a torrent."""
        return self.service.get_torrent_info(torrent_id)

    def select_files(self, torrent_id: str, file_ids: list) -> None:
        """Legacy method - Select files from a torrent."""
        self.service.select_files(torrent_id, file_ids)

    def delete_torrent(self, torrent_id: str) -> None:
        """Legacy method - Delete a torrent."""
        self.service.delete_torrent(torrent_id)

    def validate_stream(self, stream: Stream, item: MediaItem) -> Optional[TorrentContainer]:
        """
        Legacy method - Validate a stream by checking cache.
        NOTE: Does NOT add torrents - just checks cache status.
        """
        if not self.check_instant_availability_quick(stream.infohash):
            return None
        
        # For cache checking only, we don't need to add the torrent
        # Return a minimal container to indicate it's cached
        return TorrentContainer(infohash=stream.infohash, files=[])
