from typing import List, Optional, Union

from loguru import logger

from program.media.item import Episode, MediaItem, Movie, Season, Show
from program.media.state import States
from program.media.stream import Stream
from program.services.downloaders.models import (
    DebridFile,
    InvalidDebridFileException,
    DownloadedTorrent,
    NoMatchingFilesException,
    NotCachedException,
    ParsedFileData,
    TorrentContainer,
    TorrentInfo,
)
from program.services.downloaders.shared import parse_filename

from .alldebrid import AllDebridDownloader
from .realdebrid import RealDebridDownloader
from .torbox import TorBoxDownloader


class Downloader:
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

    def validate(self):
        if self.service is None:
            logger.error(
                "No downloader service is initialized. Please initialize a downloader service."
            )
            return False
        return True

    def run(self, item: MediaItem):
        logger.debug(f"Starting download process for {item.log_string} ({item.id})")

        if item.file or item.active_stream or item.last_state in [States.Completed, States.Symlinked, States.Downloaded]:
            # Check if item is stuck with active_stream but still in Scraped state
            if item.active_stream and item.last_state == States.Scraped:
                logger.log("DEBRID", f"Resetting stuck item {item.log_string} ({item.id}) - has active_stream but still in Scraped state")
                item.active_stream = None  # Reset the stuck state
                # Continue processing instead of skipping
            else:
                logger.debug(f"Skipping {item.log_string} ({item.id}) as it has already been downloaded by another download session. Details: file={bool(item.file)}, active_stream={bool(item.active_stream)}, last_state={item.last_state}")
                yield item
                return

        if item.is_parent_blocked():
            logger.debug(f"Skipping {item.log_string} ({item.id}) as it has a blocked parent, or is a blocked item")
            yield item

        if not item.streams:
            logger.debug(f"No streams available for {item.log_string} ({item.id})")
            yield item

        download_success = False
        
        # PRIORITY-AWARE LOGIC: Check for cached streams first, then uncached
        if item.streams:
            streams_to_try = [s for s in item.streams[:5] if s not in item.blacklisted_streams]  # Try top 5 non-blacklisted streams
            
            if not streams_to_try:
                logger.debug(f"All top streams are blacklisted for {item.log_string}")
                yield item
                return
            
            # PHASE 1: Look for cached streams first (highest priority)
            logger.debug(f"Phase 1: Checking {len(streams_to_try)} streams for cached availability")
            cached_streams = []
            
            for stream in streams_to_try:
                try:
                    container: Optional[TorrentContainer] = self.validate_stream_for_cache_or_download(stream, item)
                    if container:
                        # Found cached stream - add to cached list with priority
                        cached_streams.append((stream, container))
                        logger.log("DEBRID", f"Found cached stream: {stream.raw_title} [{stream.infohash}] (rank: {stream.rank})")
                except Exception as e:
                    error_msg = str(e)
                    logger.debug(f"Error checking cache for {stream.infohash}: {e}")
                    
                    # Only blacklist permanent errors during cache check
                    if any(error in error_msg for error in [
                        "Infringing", "Invalid", "Not Found", "503", "400"
                    ]):
                        logger.debug(f"Blacklisting {stream.infohash} due to permanent error: {error_msg}")
                        item.blacklist_stream(stream)
            
            # PHASE 2: Download best cached stream if any found
            if cached_streams:
                # Sort by rank to ensure we get the best cached stream
                cached_streams.sort(key=lambda x: x[0].rank, reverse=True)  # Higher rank = better
                best_cached_stream, container = cached_streams[0]
                
                logger.log("DEBRID", f"Downloading best cached stream: {best_cached_stream.raw_title} [{best_cached_stream.infohash}] (rank: {best_cached_stream.rank})")
                
                try:
                    download_result = self.download_cached_stream(best_cached_stream, container)
                    if self.update_item_attributes(item, download_result):
                        logger.log("DEBRID", f"Downloaded {item.log_string} from cached '{best_cached_stream.raw_title}' [{best_cached_stream.infohash}]")
                        download_success = True
                    else:
                        logger.debug(f"No matching files found for cached torrent {best_cached_stream.infohash}")
                        item.blacklist_stream(best_cached_stream)
                except Exception as e:
                    logger.debug(f"Failed to download cached stream {best_cached_stream.infohash}: {e}")
                    item.blacklist_stream(best_cached_stream)
            
            # PHASE 3: If no cached streams worked, try downloading best uncached stream
            if not download_success and streams_to_try:
                # Find the best uncached stream (highest rank that wasn't cached)
                uncached_streams = [s for s in streams_to_try if not any(s == cached[0] for cached in cached_streams)]
                
                if uncached_streams:
                    best_uncached_stream = uncached_streams[0]  # Already sorted by rank
                    logger.log("DEBRID", f"No cached streams available, starting download of best uncached: {best_uncached_stream.raw_title} [{best_uncached_stream.infohash}] (rank: {best_uncached_stream.rank})")
                    
                    try:
                        download_result = self.download_uncached_stream(best_uncached_stream, item)
                        if download_result:
                            if download_result.container and download_result.container.files:
                                # Became cached during setup
                                if self.update_item_attributes(item, download_result):
                                    logger.log("DEBRID", f"Downloaded {item.log_string} from '{best_uncached_stream.raw_title}' [{best_uncached_stream.infohash}] (became cached)")
                                    download_success = True
                                else:
                                    logger.debug(f"Failed to update item attributes for cached torrent {best_uncached_stream.infohash}")
                                    item.blacklist_stream(best_uncached_stream)
                            else:
                                # Truly uncached - mark as downloading
                                logger.log("DEBRID", f"Started download for {item.log_string} from '{best_uncached_stream.raw_title}' [{best_uncached_stream.infohash}]")
                                # Consider this a success for uncached downloads
                    except Exception as e:
                        error_msg = str(e)
                        logger.debug(f"Failed to set up download for {best_uncached_stream.infohash}: {e}")
                        
                        # Only blacklist permanent errors
                        if any(error in error_msg for error in [
                            "Infringing", "Invalid", "Not Found", "503", "400"
                        ]):
                            logger.debug(f"Blacklisting {best_uncached_stream.infohash} due to permanent error: {error_msg}")
                            item.blacklist_stream(best_uncached_stream)

        if not download_success:
            logger.debug(f"No streams successfully processed for {item.log_string} ({item.id})")

        yield item

    def validate_stream(self, stream: Stream, item: MediaItem) -> Optional[TorrentContainer]:
        """
        Validate a single stream by ensuring its files match the item's requirements.
        """
        container = self.get_instant_availability(stream.infohash, item.type)
        if not container:
            logger.debug(f"Stream {stream.infohash} is not cached or valid.")
            item.blacklist_stream(stream)
            return None

        valid_files = []
        for file in container.files or []:
            if isinstance(file, DebridFile):
                valid_files.append(file)
                continue

            try:
                debrid_file = DebridFile.create(
                    filename=file.filename,
                    filesize_bytes=file.filesize,
                    filetype=item.type,
                    file_id=file.file_id
                )

                if isinstance(debrid_file, DebridFile):
                    valid_files.append(debrid_file)
            except InvalidDebridFileException as e:
                logger.debug(f"{stream.infohash}: {e}")
                continue

        if valid_files:
            container.files = valid_files
            return container

        item.blacklist_stream(stream)
        return None

    def validate_stream_for_cache_or_download(self, stream: Stream, item: MediaItem) -> Optional[TorrentContainer]:
        """
        Check if stream is cached. If cached, return container for immediate download.
        If not cached, add to debrid service for download and return None.
        """
        logger.debug(f"[NEW METHOD] Validating stream {stream.infohash} for {item.log_string}")
        
        # Use the modified Real-Debrid function that doesn't delete uncached torrents
        container = self.get_instant_availability_or_download(stream.infohash, item.type)
        
        if not container:
            logger.debug(f"Stream {stream.infohash} is not cached, was added for download")
            # Don't blacklist - we want to keep this torrent downloading
            return None

        logger.debug(f"Stream {stream.infohash} is cached, validating files")
        # If we get here, the torrent was cached
        valid_files = []
        for file in container.files or []:
            if isinstance(file, DebridFile):
                valid_files.append(file)
                continue

            try:
                debrid_file = DebridFile.create(
                    filename=file.filename,
                    filesize_bytes=file.filesize,
                    filetype=item.type,
                    file_id=file.file_id
                )

                if isinstance(debrid_file, DebridFile):
                    valid_files.append(debrid_file)
            except InvalidDebridFileException as e:
                logger.debug(f"{stream.infohash}: {e}")
                continue

        if valid_files:
            logger.debug(f"Found {len(valid_files)} valid files for cached stream {stream.infohash}")
            container.files = valid_files
            return container

        logger.debug(f"No valid files found for cached stream {stream.infohash}, blacklisting")
        item.blacklist_stream(stream)
        return None

    def update_item_attributes(self, item: MediaItem, download_result: DownloadedTorrent) -> bool:
        """Update the item attributes with the downloaded files and active stream."""
        if not download_result.container:
            raise NotCachedException(f"No container found for {item.log_string} ({item.id})")

        episode_cap: int = None
        show: Optional[Show] = None
        if item.type in ("show", "season", "episode"):
            show: Optional[Show] = item if item.type == "show" else (item.parent if item.type == "season" else item.parent.parent)
            method_1 = sum(len(season.episodes) for season in show.seasons)
            try:
                method_2 = show.seasons[-1].episodes[-1].number
            except IndexError:
                # happens if theres a new season with no episodes yet
                method_2 = show.seasons[-2].episodes[-1].number
            episode_cap = max([method_1, method_2])

        found = False
        for file in download_result.container.files:
            file_data: ParsedFileData = parse_filename(file.filename)
            if item.type in ("show", "season", "episode"):
                if not file_data.episodes:
                    logger.debug(f"Skipping '{file.filename}' as it has no episodes")
                    continue
                elif 0 in file_data.episodes and len(file_data.episodes) == 1:
                    logger.debug(f"Skipping '{file.filename}' as it has an episode number of 0")
                    continue
                elif file_data.season == 0:
                    logger.debug(f"Skipping '{file.filename}' as it has a season number of 0")
                    continue
            if self.match_file_to_item(item, file_data, file, download_result, show, episode_cap):
                found = True

        return found

    def match_file_to_item(self,
            item: MediaItem,
            file_data: ParsedFileData,
            file: DebridFile,
            download_result: DownloadedTorrent,
            show: Optional[Show] = None,
            episode_cap: int = None
        ) -> bool:
        """Check if the file matches the item and update attributes."""
        found = False

        if item.type == "movie" and file_data.item_type == "movie":
            self._update_attributes(item, file, download_result)
            return True

        if item.type in ("show", "season", "episode"):
            season_number = file_data.season
            for file_episode in file_data.episodes:
                if episode_cap and file_episode > episode_cap:
                    # This is a sanity check to ensure the episode number is not greater than the total number of episodes in the show.
                    # If it is, we skip the episode as it is likely a mistake.
                    logger.debug(f"Invalid episode number {file_episode} for {show.log_string}. Skipping '{file.filename}'")
                    continue

                episode: Episode = show.get_episode(file_episode, season_number)
                if episode is None:
                    logger.debug(f"Episode {file_episode} from file does not match any episode in {show.log_string}. Metadata may be incorrect or wrong torrent for show.")
                    continue

                if episode.file:
                    continue

                if episode and episode.state not in [States.Completed, States.Symlinked, States.Downloaded]:
                    self._update_attributes(episode, file, download_result)
                    logger.debug(f"Matched episode {episode.log_string} to file {file.filename}")
                    found = True

        if found and item.type in ("show", "season"):
            item.active_stream = {"infohash": download_result.infohash, "id": download_result.info.id}

        return found

    def download_cached_stream(self, stream: Stream, container: TorrentContainer) -> DownloadedTorrent:
        """Download a cached stream"""
        torrent_id: int = self.add_torrent(stream.infohash)
        info: TorrentInfo = self.get_torrent_info(torrent_id)
        if container.file_ids:
            self.select_files(torrent_id, container.file_ids)
        return DownloadedTorrent(id=torrent_id, info=info, infohash=stream.infohash, container=container)

    def download_uncached_stream(self, stream: Stream, item: MediaItem) -> Optional[DownloadedTorrent]:
        """Set up download for an uncached stream"""
        try:
            logger.debug(f"Setting up uncached download for {stream.infohash}")
            
            # Check if torrent already exists in Real-Debrid
            existing_torrent = self.service._find_existing_torrent(stream.infohash)
            if existing_torrent:
                logger.debug(f"Found existing torrent {existing_torrent['id']} for {stream.infohash}")
                torrent_id = existing_torrent['id']
            else:
                # Add new torrent
                torrent_id = self.add_torrent(stream.infohash)
                logger.debug(f"Added new torrent {torrent_id} for {stream.infohash}")
            
            # Get torrent info
            info: TorrentInfo = self.get_torrent_info(torrent_id)
            logger.debug(f"Torrent {torrent_id} status: {info.status}")
            
            # If waiting for file selection, select video files
            if info.status == "waiting_files_selection":
                logger.debug(f"Selecting video files for torrent {torrent_id}")
                from program.services.downloaders.models import VALID_VIDEO_EXTENSIONS
                
                video_file_ids = []
                for file_id, file_info in info.files.items():
                    filename = file_info.get("filename", "")
                    if filename.endswith(tuple(ext.lower() for ext in VALID_VIDEO_EXTENSIONS)):
                        video_file_ids.append(int(file_id))
                
                if video_file_ids:
                    self.select_files(torrent_id, video_file_ids)
                    logger.debug(f"Selected {len(video_file_ids)} video files for torrent {torrent_id}")
                    # Get updated info after file selection
                    info = self.get_torrent_info(torrent_id)
                    logger.debug(f"After file selection, torrent {torrent_id} status: {info.status}")
                else:
                    logger.debug(f"No video files found in torrent {torrent_id}")
                    return None
            
            # Check if torrent became cached during the process
            if info.status == "downloaded":
                logger.log("DEBRID", f"Torrent {stream.infohash} became cached during setup, processing as cached download")
                # Create a container from the downloaded torrent
                container = self.service._process_torrent(torrent_id, stream.infohash, item.type)
                if container:
                    return DownloadedTorrent(id=torrent_id, info=info, infohash=stream.infohash, container=container)
                else:
                    logger.debug(f"Failed to process cached torrent {torrent_id}")
                    return None
            
            # For uncached torrents (downloading/queued), create a minimal container
            # This is needed because DownloadedTorrent requires a container
            from program.services.downloaders.models import TorrentContainer
            minimal_container = TorrentContainer(infohash=stream.infohash, files=[])
            
            # Return a DownloadedTorrent object so Riven can track it
            return DownloadedTorrent(id=torrent_id, info=info, infohash=stream.infohash, container=minimal_container)
            
        except Exception as e:
            logger.error(f"Failed to set up uncached download for {stream.infohash}: {e}")
            return None

    def _update_attributes(self, item: Union[Movie, Episode], debrid_file: DebridFile, download_result: DownloadedTorrent) -> None:
        """Update the item attributes with the downloaded files and active stream"""
        item.file = debrid_file.filename
        item.folder = download_result.info.name
        item.alternative_folder = download_result.info.alternative_filename
        item.active_stream = {"infohash": download_result.infohash, "id": download_result.info.id}

    def get_instant_availability(self, infohash: str, item_type: str) -> List[TorrentContainer]:
        """Check if the torrent is cached"""
        return self.service.get_instant_availability(infohash, item_type)

    def get_instant_availability_or_download(self, infohash: str, item_type: str) -> Optional[TorrentContainer]:
        """
        Check if torrent is cached. If cached, return container for immediate download.
        If not cached, add to debrid service for download and return None.
        """
        return self.service.get_instant_availability_or_download(infohash, item_type)

    def add_torrent(self, infohash: str) -> int:
        """Add a torrent by infohash"""
        return self.service.add_torrent(infohash)

    def get_torrent_info(self, torrent_id: int) -> TorrentInfo:
        """Get information about a torrent"""
        return self.service.get_torrent_info(torrent_id)

    def select_files(self, torrent_id: int, container: list[str]) -> None:
        """Select files from a torrent"""
        self.service.select_files(torrent_id, container)

    def delete_torrent(self, torrent_id: int) -> None:
        """Delete a torrent"""
        self.service.delete_torrent(torrent_id)
