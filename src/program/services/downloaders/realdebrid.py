import time
from datetime import datetime
from typing import List, Optional, Union

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
)

from .shared import DownloaderBase, premium_days_left


class RealDebridError(Exception):
    """Base exception for Real-Debrid related errors"""

class RealDebridRequestHandler(BaseRequestHandler):
    def __init__(self, session: Session, base_url: str, request_logging: bool = False):
        super().__init__(session, response_type=ResponseType.DICT, base_url=base_url, custom_exception=RealDebridError, request_logging=request_logging)

    def execute(self, method: HttpMethod, endpoint: str, **kwargs) -> Union[dict, list]:
        response = super()._request(method, endpoint, **kwargs)
        if response.status_code == 204:
            return {}
        if not response.data and not response.is_ok:
            raise RealDebridError("Invalid JSON response from RealDebrid")
        return response.data

class RealDebridAPI:
    """Handles Real-Debrid API communication"""
    BASE_URL = "https://api.real-debrid.com/rest/1.0"

    def __init__(self, api_key: str, proxy_url: Optional[str] = None):
        self.api_key = api_key
        rate_limit_params = get_rate_limit_params(per_minute=60)
        self.session = create_service_session(rate_limit_params=rate_limit_params)
        self.session.headers.update({"Authorization": f"Bearer {api_key}"})
        if proxy_url:
            self.session.proxies = {"http": proxy_url, "https": proxy_url}
        self.request_handler = RealDebridRequestHandler(self.session, self.BASE_URL)

class RealDebridDownloader(DownloaderBase):
    """Main Real-Debrid downloader class implementing DownloaderBase"""

    def __init__(self):
        self.key = "realdebrid"
        self.settings = settings_manager.settings.downloaders.real_debrid
        self.api = None
        self.initialized = self.validate()

    def validate(self) -> bool:
        """
        Validate Real-Debrid settings and premium status
        Required by DownloaderBase
        """
        if not self._validate_settings():
            return False

        self.api = RealDebridAPI(
            api_key=self.settings.api_key,
            proxy_url=self.PROXY_URL if self.PROXY_URL else None
        )

        return self._validate_premium()

    def _validate_settings(self) -> bool:
        """Validate configuration settings"""
        if not self.settings.enabled:
            return False
        if not self.settings.api_key:
            logger.warning("Real-Debrid API key is not set")
            return False
        return True

    def _validate_premium(self) -> bool:
        """Validate premium status"""
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

    def get_instant_availability(self, infohash: str, item_type: str) -> Optional[TorrentContainer]:
        """
        Get instant availability for a single infohash.
        Creates a makeshift availability check since Real-Debrid no longer supports instant availability.
        NOW MODIFIED: Will keep uncached torrents for download instead of deleting them.
        """
        logger.debug(f"[OLD METHOD UPDATED] get_instant_availability called for {infohash}")
        
        # Just call our new method to maintain consistent behavior
        return self.get_instant_availability_or_download(infohash, item_type)

    def get_instant_availability_or_download(self, infohash: str, item_type: str) -> Optional[TorrentContainer]:
        """
        Enhanced method to properly detect cached torrents and handle uncached ones.
        If cached, return container and delete torrent.
        If not cached, keep torrent for download and return None.
        """
        container: Optional[TorrentContainer] = None
        torrent_id = None

        try:
            logger.debug(f"[ENHANCED RD] Checking availability for torrent {infohash}")
            
            # Enhanced check for existing torrents with better status handling
            existing_torrent = self._find_existing_torrent(infohash)
            if existing_torrent:
                logger.debug(f"Found existing torrent {existing_torrent['id']} for {infohash}")
                
                # If already downloaded/cached, process immediately
                if existing_torrent['status'] == "downloaded":
                    logger.log("DEBRID", f"Torrent {infohash} is already cached in RD")
                    container = self._process_torrent(existing_torrent['id'], infohash, item_type)
                    if container:
                        logger.debug(f"Successfully processed existing cached torrent {existing_torrent['id']}")
                        return container
                    else:
                        logger.debug(f"Failed to process existing cached torrent {existing_torrent['id']}, will try adding new one")
                
                # If downloading, return None but don't add another copy
                elif existing_torrent['status'] in ("downloading", "queued", "waiting_files_selection"):
                    logger.log("DEBRID", f"Torrent {infohash} is already downloading in RD (status: {existing_torrent['status']})")
                    return None
                
                # If in error state or other status, continue to try adding new one
                else:
                    logger.debug(f"Existing torrent {existing_torrent['id']} has status {existing_torrent['status']}, will try adding new one")
            
            # If no existing torrent found, or existing one couldn't be processed, add new one
            logger.debug(f"Adding new torrent for {infohash}")
            torrent_id = self.add_torrent(infohash)
            logger.debug(f"Added torrent {infohash} with ID {torrent_id}")
            
            # Process the newly added torrent using original logic
            container = self._process_torrent(torrent_id, infohash, item_type)
            
            if container is not None:
                # Torrent is cached (had files after processing)
                logger.log("DEBRID", f"Torrent {infohash} is cached, using instant download")
                try:
                    self.delete_torrent(torrent_id)
                    logger.debug(f"Deleted cached torrent {torrent_id}")
                except Exception as e:
                    logger.error(f"Failed to delete cached torrent {torrent_id}: {e}")
                return container
            else:
                # Torrent is not cached - keep it for download
                logger.log("DEBRID", f"Torrent {infohash} is not cached, keeping for download")
                return None
                
        except InvalidDebridFileException as e:
            logger.debug(f"{infohash}: {e}")
            # Still cleanup failed torrents
            if torrent_id is not None:
                try:
                    self.delete_torrent(torrent_id)
                except Exception as cleanup_e:
                    logger.error(f"Failed to delete torrent {torrent_id}: {cleanup_e}")
        except exceptions.ReadTimeout as e:
            logger.debug(f"Failed to get availability for {infohash}: [ReadTimeout] {e}")
            if torrent_id is not None:
                try:
                    self.delete_torrent(torrent_id)
                except Exception as cleanup_e:
                    logger.error(f"Failed to delete torrent {torrent_id}: {cleanup_e}")
        except Exception as e:
            # Enhanced error handling with specific error types
            error_msg = str(e)
            if "503" in error_msg or "Infringing" in error_msg:
                logger.debug(f"Failed to get availability for {infohash}: [503] Infringing Torrent")
            elif "429" in error_msg or "Rate Limit" in error_msg:
                logger.debug(f"Failed to get availability for {infohash}: [429] Rate Limit Exceeded")
            elif "404" in error_msg or "Not Found" in error_msg:
                logger.debug(f"Failed to get availability for {infohash}: [404] Torrent Not Found")
            elif "400" in error_msg or "not valid" in error_msg:
                logger.debug(f"Failed to get availability for {infohash}: [400] Invalid Torrent")
            else:
                logger.error(f"Failed to get availability for {infohash}: {e}")
            
            # Cleanup failed torrents
            if torrent_id is not None:
                try:
                    self.delete_torrent(torrent_id)
                except Exception as cleanup_e:
                    logger.error(f"Failed to delete torrent {torrent_id}: {cleanup_e}")

        return None

    def _find_existing_torrent(self, infohash: str) -> Optional[dict]:
        """
        Find an existing torrent by infohash in the user's Real-Debrid account.
        Enhanced with better hash comparison and logging.
        """
        try:
            torrents = self.api.request_handler.execute(HttpMethod.GET, "torrents")
            search_hash = infohash.lower().strip()
            
            for torrent in torrents:
                torrent_hash = torrent.get("hash", "").lower().strip()
                if torrent_hash == search_hash:
                    logger.debug(f"Found existing torrent {torrent['id']} for {infohash} with status {torrent.get('status', 'unknown')}")
                    return torrent
            
            logger.debug(f"No existing torrent found for {infohash} in {len(torrents)} torrents")
            return None
        except Exception as e:
            logger.debug(f"Failed to check existing torrents: {e}")
            return None

    def _process_torrent(self, torrent_id: str, infohash: str, item_type: str) -> Optional[TorrentContainer]:
        """Process a single torrent and return a TorrentContainer if valid."""
        logger.debug(f"Processing torrent {torrent_id} for infohash {infohash}")
        
        torrent_info = self.get_torrent_info(torrent_id)
        if not torrent_info:
            logger.debug(f"No torrent info found for {torrent_id} with infohash {infohash}")
            return None

        logger.debug(f"Torrent {torrent_id} ({infohash}) has status: {torrent_info.status}")
        
        torrent_files = []

        if not torrent_info.files:
            logger.debug(f"No files found in torrent {torrent_id} with infohash {infohash}")
            return None

        if torrent_info.status == "waiting_files_selection":
            logger.debug(f"Torrent {torrent_id} waiting for file selection")
            video_file_ids = [
                file_id for file_id, file_info in torrent_info.files.items()
                if file_info["filename"].endswith(tuple(ext.lower() for ext in VALID_VIDEO_EXTENSIONS))
            ]

            if not video_file_ids:
                logger.debug(f"No video files found in torrent {torrent_id} with infohash {infohash}")
                return None

            logger.debug(f"Selecting {len(video_file_ids)} video files for torrent {torrent_id}")
            self.select_files(torrent_id, video_file_ids)
            torrent_info = self.get_torrent_info(torrent_id)
            logger.debug(f"After file selection, torrent {torrent_id} status: {torrent_info.status}")

        if torrent_info.status == "downloaded":
            logger.debug(f"Processing files for downloaded torrent {torrent_id}")
            for file_id, file_info in torrent_info.files.items():
                try:
                    debrid_file = DebridFile.create(
                        path=file_info["path"],
                        filename=file_info["filename"],
                        filesize_bytes=file_info["bytes"],
                        filetype=item_type,
                        file_id=file_id
                    )

                    if isinstance(debrid_file, DebridFile):
                        torrent_files.append(debrid_file)
                except InvalidDebridFileException as e:
                    logger.debug(f"{infohash}: {e}")
                    continue

            if not torrent_files:
                logger.debug(f"No valid files found after validating files in torrent {torrent_id} with infohash {infohash}")
                return None

            logger.debug(f"Successfully processed {len(torrent_files)} files for torrent {torrent_id}")
            return TorrentContainer(infohash=infohash, files=torrent_files)

        # For any other status, this torrent is not ready for file processing
        logger.debug(f"Torrent {torrent_id} with infohash {infohash} has status '{torrent_info.status}', not ready for file processing")
        return None

    def add_torrent(self, infohash: str) -> str:
        """Add a torrent by infohash"""
        try:
            magnet = f"magnet:?xt=urn:btih:{infohash}"
            response = self.api.request_handler.execute(
                HttpMethod.POST,
                "torrents/addMagnet",
                data={"magnet": magnet.lower()}
            )
            return response["id"]
        except Exception as e:
            if len(e.args) > 0:
                if " 503 " in e.args[0]:
                    logger.debug(f"Failed to add torrent {infohash}: [503] Infringing Torrent or Service Unavailable")
                    raise RealDebridError("Infringing Torrent or Service Unavailable")
                elif " 429 " in e.args[0]:
                    logger.debug(f"Failed to add torrent {infohash}: [429] Rate Limit Exceeded")
                    raise RealDebridError("Rate Limit Exceeded")
                elif " 404 " in e.args[0]:
                    logger.debug(f"Failed to add torrent {infohash}: [404] Torrent Not Found or Service Unavailable")
                    raise RealDebridError("Torrent Not Found or Service Unavailable")
                elif " 400 " in e.args[0]:
                    logger.debug(f"Failed to add torrent {infohash}: [400] Torrent file is not valid. Magnet: {magnet}")
                    raise RealDebridError("Torrent file is not valid")
            else:
                logger.debug(f"Failed to add torrent {infohash}: {e}")
            
            raise RealDebridError(f"Failed to add torrent {infohash}: {e}")

    def select_files(self, torrent_id: str, ids: List[int] = None) -> None:
        """Select files from a torrent"""
        try:
            selection = ",".join(str(file_id) for file_id in ids) if ids else "all"
            self.api.request_handler.execute(
                HttpMethod.POST,
                f"torrents/selectFiles/{torrent_id}",
                data={"files": selection},
                timeout=25
            )
        except Exception as e:
            logger.error(f"Failed to select files for torrent {torrent_id}: {e}")
            raise

    def get_torrent_info(self, torrent_id: str) -> TorrentInfo:
        """Get information about a torrent"""
        try:
            data = self.api.request_handler.execute(HttpMethod.GET, f"torrents/info/{torrent_id}")
            files = {
                file["id"]: {
                    "path": file["path"], # we're gonna need this to weed out the junk files
                    "filename": file["path"].split("/")[-1],
                    "bytes": file["bytes"],
                    "selected": file["selected"]
                } for file in data["files"]
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

    def delete_torrent(self, torrent_id: str) -> None:
        """Delete a torrent"""
        try:
            self.api.request_handler.execute(HttpMethod.DELETE, f"torrents/delete/{torrent_id}")
        except Exception as e:
            logger.error(f"Failed to delete torrent {torrent_id}: {e}")
            raise
