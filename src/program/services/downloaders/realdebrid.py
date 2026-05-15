"""
Real-Debrid Downloader - Refactored for Single-Torrent Downloads

Key principles:
1. ONE torrent per media item at a time
2. Never add torrents just to check cache - use instant availability API
3. Proper error handling with specific error types
4. Clear status reporting for download tracking
"""

from datetime import datetime
from enum import Enum
from typing import List, Optional, Tuple, Union
from dataclasses import dataclass

from loguru import logger
from requests import Session, exceptions

from program.services.downloaders.models import (
    VALID_VIDEO_EXTENSIONS,
    DebridFile,
    InvalidDebridFileException,
    TorrentContainer,
    TorrentInfo,
)
from program.settings.manager import settings_manager
from program.utils.request import (
    BaseRequestHandler,
    HttpMethod,
    ResponseType,
    create_service_session,
    get_rate_limit_params,
    get_retry_policy,
)

from .shared import DownloaderBase, premium_days_left


class RealDebridErrorType(Enum):
    """Categorized error types for Real-Debrid operations."""
    LEGAL_BLOCKED = "legal_blocked"      # 451 - DMCA/legal takedown
    RATE_LIMITED = "rate_limited"        # 429 - Too many requests
    NOT_FOUND = "not_found"              # 404 - Torrent not found
    INVALID_TORRENT = "invalid_torrent"  # 400 - Bad magnet/torrent
    SERVICE_ERROR = "service_error"      # 5xx - Server errors
    TIMEOUT = "timeout"                  # Request timeout
    UNKNOWN = "unknown"                  # Unknown error


class RealDebridError(Exception):
    """Exception for Real-Debrid errors with categorization."""
    def __init__(self, message: str, error_type: RealDebridErrorType = RealDebridErrorType.UNKNOWN):
        super().__init__(message)
        self.error_type = error_type
        self.message = message


@dataclass
class TorrentStatus:
    """Represents the current status of a torrent in Real-Debrid."""
    torrent_id: str
    status: str
    progress: float
    files: Optional[dict] = None
    error_message: Optional[str] = None
    
    @property
    def is_cached(self) -> bool:
        return self.status == "downloaded"
    
    @property
    def is_downloading(self) -> bool:
        return self.status in ("downloading", "queued", "magnet_conversion", "waiting_files_selection")
    
    @property
    def is_error(self) -> bool:
        return self.status in ("error", "magnet_error", "virus", "dead")
    
    @property
    def needs_file_selection(self) -> bool:
        return self.status == "waiting_files_selection"


class RealDebridRequestHandler(BaseRequestHandler):
    """Request handler with proper timeout and error categorization."""
    
    def __init__(self, session: Session, base_url: str, request_logging: bool = False):
        super().__init__(session, response_type=ResponseType.DICT, base_url=base_url, custom_exception=RealDebridError, request_logging=request_logging)
        self.timeout = 30

    def execute(self, method: HttpMethod, endpoint: str, **kwargs) -> Union[dict, list]:
        try:
            response = super()._request(method, endpoint, **kwargs)
            if response.status_code == 204:
                return {}
            if not response.data and not response.is_ok:
                raise RealDebridError("Invalid JSON response", RealDebridErrorType.SERVICE_ERROR)
            return response.data
        except exceptions.Timeout:
            raise RealDebridError("Request timed out", RealDebridErrorType.TIMEOUT)
        except Exception as e:
            # Categorize the error
            error_msg = str(e)
            if "451" in error_msg:
                raise RealDebridError(error_msg, RealDebridErrorType.LEGAL_BLOCKED)
            elif "429" in error_msg:
                raise RealDebridError(error_msg, RealDebridErrorType.RATE_LIMITED)
            elif "404" in error_msg:
                raise RealDebridError(error_msg, RealDebridErrorType.NOT_FOUND)
            elif "400" in error_msg:
                raise RealDebridError(error_msg, RealDebridErrorType.INVALID_TORRENT)
            elif "50" in error_msg:  # 500, 502, 503, etc.
                raise RealDebridError(error_msg, RealDebridErrorType.SERVICE_ERROR)
            raise


class RealDebridAPI:
    """Handles Real-Debrid API communication."""
    BASE_URL = "https://api.real-debrid.com/rest/1.0"

    def __init__(self, api_key: str, proxy_url: Optional[str] = None):
        self.api_key = api_key
        rate_limit_params = get_rate_limit_params(per_minute=200)
        retry_policy = get_retry_policy(retries=2, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
        self.session = create_service_session(rate_limit_params=rate_limit_params, retry_policy=retry_policy)
        self.session.headers.update({"Authorization": f"Bearer {api_key}"})
        if proxy_url:
            self.session.proxies = {"http": proxy_url, "https": proxy_url}
        self.request_handler = RealDebridRequestHandler(self.session, self.BASE_URL)


class RealDebridDownloader(DownloaderBase):
    """
    Real-Debrid downloader with single-torrent-at-a-time logic.
    
    Key methods:
    - check_cache(): Check if hash is cached WITHOUT adding to RD
    - add_torrent(): Add a torrent to RD and return status
    - get_torrent_status(): Get current status of a torrent
    - process_completed_torrent(): Extract files from a completed torrent
    """

    def __init__(self):
        self.key = "realdebrid"
        self.settings = settings_manager.settings.downloaders.real_debrid
        self.api = None
        self.initialized = self.validate()

    def validate(self) -> bool:
        """Validate Real-Debrid settings and premium status."""
        if not self._validate_settings():
            return False
        
        self.api = RealDebridAPI(
            api_key=self.settings.api_key,
            proxy_url=self.PROXY_URL if self.PROXY_URL else None
        )
        
        return self._validate_premium()

    def _validate_settings(self) -> bool:
        """Validate configuration settings."""
        if not self.settings.enabled:
            return False
        if not self.settings.api_key:
            logger.warning("Real-Debrid API key is not set")
            return False
        return True

    def _validate_premium(self) -> bool:
        """Validate premium status."""
        try:
            user_info = self.api.request_handler.execute(HttpMethod.GET, "user")
            if not user_info.get("premium"):
                logger.error("Premium membership required")
                return False

            expiration = datetime.fromisoformat(
                user_info["expiration"].replace("Z", "+00:00")
            ).replace(tzinfo=None)
            logger.info(premium_days_left(expiration))
            return True
        except Exception as e:
            logger.error(f"Failed to validate premium status: {e}")
            return False

    # =========================================================================
    # CACHE CHECKING - Does NOT add torrents to RD
    # =========================================================================
    
    def check_cache(self, infohash: str) -> bool:
        """
        Check if a torrent is cached in Real-Debrid WITHOUT adding it.
        Uses the instant availability API endpoint.
        
        Returns:
            True if cached, False otherwise
        """
        try:
            response = self.api.request_handler.execute(
                HttpMethod.GET,
                f"torrents/instantAvailability/{infohash}"
            )
            
            # Response format: {"hash": {"rd": [{"filename": "...", "filesize": ...}, ...]}}
            if response and infohash.lower() in response:
                rd_data = response[infohash.lower()].get("rd", [])
                if rd_data and len(rd_data) > 0:
                    logger.debug(f"Cache check: {infohash[:8]}... IS cached ({len(rd_data)} variants)")
                    return True
            
            logger.debug(f"Cache check: {infohash[:8]}... is NOT cached")
            return False
            
        except RealDebridError as e:
            if e.error_type == RealDebridErrorType.LEGAL_BLOCKED:
                logger.debug(f"Cache check: {infohash[:8]}... blocked (451)")
                return False
            # 403 is common - instant availability API not available for all accounts
            if "403" in str(e) or "Forbidden" in str(e):
                logger.debug(f"Cache check: API not available (403), assuming not cached")
                return False
            logger.debug(f"Cache check failed for {infohash[:8]}...: {e}")
            return False
        except Exception as e:
            logger.debug(f"Cache check failed for {infohash[:8]}...: {e}")
            return False

    def check_cache_batch(self, infohashes: List[str]) -> dict[str, bool]:
        """
        Check cache status for multiple hashes at once.
        
        Returns:
            Dict mapping infohash -> is_cached
        """
        results = {}
        
        # RD's instant availability accepts multiple hashes separated by /
        # But we'll do them individually for reliability
        for infohash in infohashes:
            results[infohash] = self.check_cache(infohash)
        
        return results

    # =========================================================================
    # TORRENT MANAGEMENT - Adds/manages torrents in RD
    # =========================================================================
    
    def add_torrent(self, infohash: str) -> Tuple[str, TorrentStatus]:
        """
        Add a torrent to Real-Debrid by infohash.
        
        Returns:
            Tuple of (torrent_id, TorrentStatus)
            
        Raises:
            RealDebridError with appropriate error_type
        """
        try:
            magnet = f"magnet:?xt=urn:btih:{infohash.lower()}"
            response = self.api.request_handler.execute(
                HttpMethod.POST,
                "torrents/addMagnet",
                data={"magnet": magnet}
            )
            
            torrent_id = response["id"]
            logger.debug(f"Added torrent {infohash[:8]}... with ID {torrent_id}")
            
            # Get initial status
            status = self.get_torrent_status(torrent_id)
            return torrent_id, status
            
        except RealDebridError:
            raise
        except Exception as e:
            error_msg = str(e)
            
            # Categorize HTTP errors
            if "451" in error_msg:
                raise RealDebridError(
                    f"Torrent blocked for legal reasons: {infohash}",
                    RealDebridErrorType.LEGAL_BLOCKED
                )
            elif "429" in error_msg:
                raise RealDebridError(
                    f"Rate limit exceeded",
                    RealDebridErrorType.RATE_LIMITED
                )
            elif "404" in error_msg:
                raise RealDebridError(
                    f"Torrent not found: {infohash}",
                    RealDebridErrorType.NOT_FOUND
                )
            elif "400" in error_msg:
                raise RealDebridError(
                    f"Invalid torrent: {infohash}",
                    RealDebridErrorType.INVALID_TORRENT
                )
            else:
                raise RealDebridError(
                    f"Failed to add torrent: {error_msg}",
                    RealDebridErrorType.UNKNOWN
                )

    def get_torrent_status(self, torrent_id: str) -> TorrentStatus:
        """
        Get the current status of a torrent in Real-Debrid.
        
        Returns:
            TorrentStatus object with current state
        """
        try:
            data = self.api.request_handler.execute(HttpMethod.GET, f"torrents/info/{torrent_id}")
            
            files = None
            if data.get("files"):
                files = {
                    file["id"]: {
                        "path": file["path"],
                        "filename": file["path"].split("/")[-1],
                        "bytes": file["bytes"],
                        "selected": file.get("selected", 0)
                    }
                    for file in data["files"]
                }
            
            return TorrentStatus(
                torrent_id=torrent_id,
                status=data.get("status", "unknown"),
                progress=data.get("progress", 0),
                files=files,
            )
            
        except RealDebridError:
            raise
        except Exception as e:
            raise RealDebridError(f"Failed to get torrent status: {e}", RealDebridErrorType.UNKNOWN)

    def find_existing_torrent(self, infohash: str) -> Optional[Tuple[str, TorrentStatus]]:
        """
        Check if a torrent with this infohash already exists in the user's RD account.
        
        Returns:
            Tuple of (torrent_id, TorrentStatus) if found, None otherwise
        """
        try:
            torrents = self.api.request_handler.execute(HttpMethod.GET, "torrents")
            search_hash = infohash.lower().strip()
            
            for torrent in torrents:
                torrent_hash = torrent.get("hash", "").lower().strip()
                if torrent_hash == search_hash:
                    torrent_id = torrent["id"]
                    status = self.get_torrent_status(torrent_id)
                    logger.debug(f"Found existing torrent {infohash[:8]}... with status: {status.status}")
                    return torrent_id, status
            
            return None
            
        except Exception as e:
            logger.debug(f"Failed to check existing torrents: {e}")
            return None

    def select_video_files(self, torrent_id: str) -> bool:
        """
        Select video files for a torrent that's waiting for file selection.
        
        Returns:
            True if selection was successful, False otherwise
        """
        try:
            status = self.get_torrent_status(torrent_id)
            
            if not status.needs_file_selection:
                logger.debug(f"Torrent {torrent_id} doesn't need file selection (status: {status.status})")
                return True
            
            if not status.files:
                logger.warning(f"Torrent {torrent_id} has no files to select")
                return False
            
            # Check if auto_select_all_files is enabled
            if getattr(self.settings, "auto_select_all_files", True):
                # Select all files
                self.api.request_handler.execute(
                    HttpMethod.POST,
                    f"torrents/selectFiles/{torrent_id}",
                    data={"files": "all"}
                )
                logger.debug(f"Selected ALL files for torrent {torrent_id}")
            else:
                # Select only video files
                video_file_ids = []
                for file_id, file_info in status.files.items():
                    filename = file_info.get("filename", "").lower()
                    if any(filename.endswith(ext.lower()) for ext in VALID_VIDEO_EXTENSIONS):
                        video_file_ids.append(int(file_id))
                
                if not video_file_ids:
                    logger.warning(f"No video files found in torrent {torrent_id}")
                    return False
                
                selection = ",".join(str(fid) for fid in video_file_ids)
                self.api.request_handler.execute(
                    HttpMethod.POST,
                    f"torrents/selectFiles/{torrent_id}",
                    data={"files": selection}
                )
                logger.debug(f"Selected {len(video_file_ids)} video files for torrent {torrent_id}")
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to select files for torrent {torrent_id}: {e}")
            return False

    def delete_torrent(self, torrent_id: str) -> bool:
        """
        Delete a torrent from Real-Debrid.
        
        Returns:
            True if deleted successfully, False otherwise
        """
        try:
            self.api.request_handler.execute(HttpMethod.DELETE, f"torrents/delete/{torrent_id}")
            logger.debug(f"Deleted torrent {torrent_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete torrent {torrent_id}: {e}")
            return False

    # =========================================================================
    # FILE PROCESSING - Extract usable files from completed torrents
    # =========================================================================
    
    def process_completed_torrent(self, torrent_id: str, infohash: str, item_type: str) -> Optional[TorrentContainer]:
        """
        Process a completed torrent and extract valid video files.
        
        Args:
            torrent_id: The RD torrent ID
            infohash: The torrent infohash
            item_type: Type of media item (movie, show, season, episode)
            
        Returns:
            TorrentContainer with valid files, or None if no valid files found
        """
        try:
            status = self.get_torrent_status(torrent_id)
            
            if not status.is_cached:
                logger.debug(f"Torrent {torrent_id} is not completed (status: {status.status})")
                return None
            
            if not status.files:
                logger.debug(f"Torrent {torrent_id} has no files")
                return None
            
            # Extract valid video files
            valid_files = []
            for file_id, file_info in status.files.items():
                try:
                    debrid_file = DebridFile.create(
                        path=file_info.get("path", ""),
                        filename=file_info["filename"],
                        filesize_bytes=file_info["bytes"],
                        filetype=item_type,
                        file_id=int(file_id)
                    )
                    if isinstance(debrid_file, DebridFile):
                        valid_files.append(debrid_file)
                except InvalidDebridFileException as e:
                    logger.debug(f"Skipping file: {e}")
                    continue
            
            if not valid_files:
                logger.debug(f"No valid video files in torrent {torrent_id}")
                return None
            
            logger.debug(f"Found {len(valid_files)} valid files in torrent {torrent_id}")
            return TorrentContainer(infohash=infohash, files=valid_files)
            
        except Exception as e:
            logger.error(f"Failed to process torrent {torrent_id}: {e}")
            return None

    # =========================================================================
    # LEGACY COMPATIBILITY - Methods for backward compatibility
    # =========================================================================
    
    def get_instant_availability(self, infohash: str, item_type: str) -> Optional[TorrentContainer]:
        """
        Legacy method - Check instant availability and process if cached.
        
        DEPRECATED: Use check_cache() + add_torrent() + process_completed_torrent() instead
        """
        logger.debug(f"[LEGACY] get_instant_availability called for {infohash}")
        
        # First check if it's cached
        if not self.check_cache(infohash):
            return None
        
        # Check if it already exists in RD
        existing = self.find_existing_torrent(infohash)
        if existing:
            torrent_id, status = existing
            if status.is_cached:
                return self.process_completed_torrent(torrent_id, infohash, item_type)
            return None
        
        # Add it and check status
        try:
            torrent_id, status = self.add_torrent(infohash)
            
            if status.needs_file_selection:
                self.select_video_files(torrent_id)
                status = self.get_torrent_status(torrent_id)
            
            if status.is_cached:
                return self.process_completed_torrent(torrent_id, infohash, item_type)
            
            return None
            
        except RealDebridError as e:
            logger.debug(f"Failed to add torrent {infohash}: {e}")
            return None

    def get_instant_availability_or_download(self, infohash: str, item_type: str) -> Optional[TorrentContainer]:
        """
        Legacy method - For backward compatibility.
        
        DEPRECATED: Use the new single-torrent flow instead
        """
        return self.get_instant_availability(infohash, item_type)

    def add_torrent_legacy(self, infohash: str) -> str:
        """Legacy method - Add torrent and return ID only."""
        torrent_id, _ = self.add_torrent(infohash)
        return torrent_id

    def get_torrent_info(self, torrent_id: str) -> TorrentInfo:
        """
        Legacy method - Get torrent info in old format.
        """
        try:
            data = self.api.request_handler.execute(HttpMethod.GET, f"torrents/info/{torrent_id}")
            files = {
                file["id"]: {
                    "path": file["path"],
                    "filename": file["path"].split("/")[-1],
                    "bytes": file["bytes"],
                    "selected": file["selected"]
                }
                for file in data.get("files", [])
            }
            return TorrentInfo(
                id=data["id"],
                name=data["filename"],
                status=data["status"],
                infohash=data["hash"],
                bytes=data["bytes"],
                created_at=data["added"],
                alternative_filename=data.get("original_filename", None),
                progress=data.get("progress", None),
                files=files,
            )
        except Exception as e:
            logger.error(f"Failed to get torrent info for {torrent_id}: {e}")
            raise

    def select_files(self, torrent_id: str, ids: List[int] = None) -> None:
        """Legacy method - Select files from a torrent."""
        try:
            selection = ",".join(str(file_id) for file_id in ids) if ids else "all"
            self.api.request_handler.execute(
                HttpMethod.POST,
                f"torrents/selectFiles/{torrent_id}",
                data={"files": selection}
            )
        except Exception as e:
            logger.error(f"Failed to select files for torrent {torrent_id}: {e}")
            raise

    # Keep old method names for compatibility
    def _find_existing_torrent(self, infohash: str) -> Optional[dict]:
        """Legacy compatibility - returns dict format."""
        result = self.find_existing_torrent(infohash)
        if result:
            torrent_id, status = result
            return {
                "id": torrent_id,
                "status": status.status,
                "hash": infohash,
            }
        return None

    def check_instant_availability_api(self, infohash: str) -> Optional[bool]:
        """Legacy compatibility - returns Optional[bool]."""
        try:
            return self.check_cache(infohash)
        except Exception:
            return None
