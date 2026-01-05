"""
Downloader Service - Refactored for Single-Torrent Downloads

Key principles:
1. ONE torrent per media item at a time
2. Use database tracking (TorrentDownload) for state persistence
3. Proper error handling with categorized errors
4. Clean separation between cache checking and torrent addition
"""

from datetime import datetime, timedelta
from typing import Generator, List, Optional

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
        
        with db.Session() as session:
            # Step 1: Check for existing active download
            active_download = TorrentDownload.get_active_for_item(session, item.id)
            
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
            
            # Step 2: No active download, find best stream
            if not item.streams:
                logger.debug(f"No streams available for {item.log_string}")
                yield item
                return
            
            # Get failed hashes to skip
            failed_hashes = TorrentDownload.get_failed_hashes_for_item(session, item.id)
            blacklisted_hashes = {s.infohash for s in item.blacklisted_streams}
            skip_hashes = set(failed_hashes) | blacklisted_hashes
            
            # Find best available stream
            best_stream = self._find_best_stream(item.streams, skip_hashes)
            
            if not best_stream:
                logger.debug(f"No available streams for {item.log_string} (all failed/blacklisted)")
                yield item
                return
            
            # Step 3: Try to download the best stream
            result = self._start_download(session, item, best_stream)
            session.commit()
            
            if result:
                logger.log("DEBRID", f"Download completed for {item.log_string}")
            
        yield item

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
                # Torrent was deleted from RD
                logger.warning(f"Torrent not found in RD for {item.log_string}")
                download.mark_failed("Torrent removed from Real-Debrid")
            else:
                logger.error(f"Error checking torrent status: {e}")
                download.mark_failed(str(e))
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
        1. Check if cached first (no torrent addition)
        2. If cached: Add to RD and process immediately
        3. If not cached: Add to RD and track for later
        
        Returns:
            True if completed immediately (cached), False if downloading/failed
        """
        infohash = stream.infohash
        logger.log("DEBRID", f"Attempting download: {stream.raw_title} [{infohash[:8]}...] (rank: {stream.rank})")
        
        # Create tracking record
        download = TorrentDownload.create_for_item(
            session=session,
            media_item_id=item.id,
            infohash=infohash,
            raw_title=stream.raw_title,
            rank=stream.rank
        )
        
        try:
            # Step 1: Check if already exists in RD
            existing = self.service.find_existing_torrent(infohash)
            
            if existing:
                torrent_id, status = existing
                download.torrent_id = torrent_id
                
                if status.is_cached:
                    # Already cached! Process immediately
                    return self._process_cached_torrent(session, item, download, torrent_id)
                
                elif status.is_downloading:
                    # Already downloading - just track it
                    download.mark_downloading(torrent_id)
                    logger.log("DEBRID", f"Already downloading in RD: {stream.raw_title}")
                    return False
                
                elif status.is_error:
                    # Existing torrent is in error state - try fresh add
                    logger.debug(f"Existing torrent in error state, will add fresh")
            
            # Step 2: Check cache before adding (for new torrents)
            is_cached = self.service.check_cache(infohash)
            
            # Step 3: Add torrent to RD
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
                # Instantly cached! Process immediately
                return self._process_cached_torrent(session, item, download, torrent_id)
            else:
                # Not cached - mark as downloading
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
