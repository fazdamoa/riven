"""
TorBox Downloader - Refactored for Single-Torrent Downloads

Implements the same contract as RealDebridDownloader so the orchestrator
and manual scrape route work identically regardless of active provider.
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional, Tuple, Union

from loguru import logger
from requests import Session

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
from program.utils import get_version


# ---------------------------------------------------------------------------
# A.1 — Error taxonomy
# ---------------------------------------------------------------------------

class TorBoxErrorType(Enum):
    RATE_LIMITED = "rate_limited"       # 429 or ACTIVE_LIMIT / COOLDOWN_LIMIT / MONTHLY_LIMIT
    LEGAL_BLOCKED = "legal_blocked"     # 503 "Infringing Torrent"
    INVALID_TORRENT = "invalid_torrent" # 400 / BOZO_TORRENT / DOWNLOAD_TOO_LARGE
    NOT_FOUND = "not_found"             # 404 / ITEM_NOT_FOUND
    SERVICE_ERROR = "service_error"     # 5xx / DATABASE_ERROR / NO_SERVERS_AVAILABLE_ERROR
    PLAN_RESTRICTED = "plan_restricted" # PLAN_RESTRICTED_FEATURE
    TIMEOUT = "timeout"                 # request timeout
    UNKNOWN = "unknown"                 # fallback


# TorBox error-code strings → error type
_TORBOX_ERROR_CODE_MAP: Dict[str, TorBoxErrorType] = {
    "ACTIVE_LIMIT": TorBoxErrorType.RATE_LIMITED,
    "COOLDOWN_LIMIT": TorBoxErrorType.RATE_LIMITED,
    "MONTHLY_LIMIT": TorBoxErrorType.RATE_LIMITED,
    "BOZO_TORRENT": TorBoxErrorType.INVALID_TORRENT,
    "BOZO_NZB": TorBoxErrorType.INVALID_TORRENT,
    "DOWNLOAD_TOO_LARGE": TorBoxErrorType.INVALID_TORRENT,
    "ITEM_NOT_FOUND": TorBoxErrorType.NOT_FOUND,
    "DATABASE_ERROR": TorBoxErrorType.SERVICE_ERROR,
    "UNKNOWN_ERROR": TorBoxErrorType.SERVICE_ERROR,
    "NO_SERVERS_AVAILABLE_ERROR": TorBoxErrorType.SERVICE_ERROR,
    "PLAN_RESTRICTED_FEATURE": TorBoxErrorType.PLAN_RESTRICTED,
}


class TorBoxError(Exception):
    """Exception for TorBox errors with categorisation."""

    def __init__(self, message: str, error_type: TorBoxErrorType = TorBoxErrorType.UNKNOWN):
        super().__init__(message)
        self.error_type = error_type
        self.message = message


# ---------------------------------------------------------------------------
# A.2 — TorrentStatus dataclass
# ---------------------------------------------------------------------------

# States TorBox reports as "actively downloading / queued"
_DOWNLOADING_STATES = {
    "downloading", "queued", "metadl", "stalledDL (No seeds)".lower(),
    "uploading", "checking", "checkingdl",
    # extra casing variants TorBox occasionally emits
    "stalled (no seeds)",
}

# States TorBox reports as errors
_ERROR_STATES = {
    "failed", "error", "missingfiles", "corrupt",
    "inactive (>30 days)", "timedout (>2 days)",
}


@dataclass
class TorrentStatus:
    """Represents the current status of a torrent in TorBox."""
    torrent_id: str
    status: str                      # raw download_state from API (lowercased)
    progress: float = 0.0
    files: Optional[dict] = None
    error_message: Optional[str] = None
    # cached is determined from the boolean fields, not just the state string
    _is_cached_flag: bool = field(default=False, repr=False)

    @property
    def is_cached(self) -> bool:
        return self._is_cached_flag

    @property
    def is_downloading(self) -> bool:
        return self.status in _DOWNLOADING_STATES

    @property
    def is_error(self) -> bool:
        return self.status in _ERROR_STATES

    @property
    def needs_file_selection(self) -> bool:
        # TorBox always downloads all files — no selection step needed
        return False


# ---------------------------------------------------------------------------
# A.3 — Request handler
# ---------------------------------------------------------------------------

class TorBoxRequestHandler(BaseRequestHandler):
    def __init__(self, session: Session, base_url: str, request_logging: bool = False):
        super().__init__(
            session,
            response_type=ResponseType.DICT,
            base_url=base_url,
            custom_exception=TorBoxError,
            request_logging=request_logging,
        )

    def execute(self, method: HttpMethod, endpoint: str, **kwargs) -> Union[dict, list]:
        response = super()._request(method, endpoint, **kwargs)
        if response.status_code == 204:
            return {}

        data = response.data or {}

        # TorBox envelope: {success, error, detail, data}
        if isinstance(data, dict) and not data.get("success", True):
            error_code = data.get("error", "UNKNOWN_ERROR")
            detail = data.get("detail", error_code)
            error_type = _TORBOX_ERROR_CODE_MAP.get(error_code, TorBoxErrorType.UNKNOWN)
            raise TorBoxError(detail, error_type)

        # HTTP-level errors not already raised by BaseRequestHandler
        if not response.is_ok:
            status_code = response.status_code
            if status_code == 429:
                raise TorBoxError("Rate limit exceeded", TorBoxErrorType.RATE_LIMITED)
            elif status_code == 404:
                raise TorBoxError("Not found", TorBoxErrorType.NOT_FOUND)
            elif status_code == 400:
                raise TorBoxError("Bad request", TorBoxErrorType.INVALID_TORRENT)
            elif status_code == 503:
                msg = str(data)
                if "infringing" in msg.lower():
                    raise TorBoxError("Infringing torrent", TorBoxErrorType.LEGAL_BLOCKED)
                raise TorBoxError("Service unavailable", TorBoxErrorType.SERVICE_ERROR)
            elif status_code >= 500:
                raise TorBoxError(f"Server error {status_code}", TorBoxErrorType.SERVICE_ERROR)
            raise TorBoxError(f"Unexpected status {status_code}", TorBoxErrorType.UNKNOWN)

        # Return the payload
        if isinstance(data, dict):
            return data.get("data", data)
        return data


# ---------------------------------------------------------------------------
# A.4 — API session (rate-limit adjusted + local add-torrent guard)
# ---------------------------------------------------------------------------

class TorBoxAPI:
    """Handles TorBox API communication."""
    BASE_URL = "https://api.torbox.app/v1/api"

    def __init__(self, api_key: str, proxy_url: Optional[str] = None):
        self.api_key = api_key
        # TorBox general API limit is 300/min; keep headroom. The 60/hr createtorrent
        # limit is enforced separately by _check_add_rate_limit().
        rate_limit_params = get_rate_limit_params(per_minute=250)
        self.session = create_service_session(rate_limit_params=rate_limit_params)
        self.session.headers.update({"Authorization": f"Bearer {api_key}"})
        try:
            version = get_version()
        except Exception:
            version = "Unknown"
        self.session.headers.update({"User-Agent": f"Riven/{version} TorBox/1.0"})
        if proxy_url:
            self.session.proxies = {"http": proxy_url, "https": proxy_url}
        self.request_handler = TorBoxRequestHandler(self.session, self.BASE_URL)


# ---------------------------------------------------------------------------
# Main downloader class
# ---------------------------------------------------------------------------

class TorBoxDownloader(DownloaderBase):
    """TorBox downloader implementing the same contract as RealDebridDownloader."""

    # Local guard: TorBox limits createtorrent to 60 calls per hour per API token
    _MAX_ADDS_PER_HOUR = 60

    def __init__(self):
        self.key = "torbox"
        self.settings = settings_manager.settings.downloaders.torbox
        self.api: Optional[TorBoxAPI] = None
        self.initialized = self.validate()

        # A.4 — deque of timestamps of recent createtorrent calls (instance-level)
        self._add_timestamps: deque = deque()

        # Cache for find_existing_torrent to avoid hammering /mylist
        self._list_cache: Optional[list] = None
        self._list_cache_at: Optional[datetime] = None
        self._LIST_CACHE_TTL = 10  # seconds

    def validate(self) -> bool:
        if not self._validate_settings():
            return False
        self.api = TorBoxAPI(
            api_key=self.settings.api_key,
            proxy_url=self.PROXY_URL if self.PROXY_URL else None,
        )
        return self._validate_premium()

    def _validate_settings(self) -> bool:
        if not self.settings.enabled:
            return False
        if not self.settings.api_key:
            logger.warning("TorBox API key is not set")
            return False
        return True

    # A.5 — Premium validation
    def _validate_premium(self) -> bool:
        try:
            user_info = self.api.request_handler.execute(HttpMethod.GET, "user/me")
            plan = user_info.get("plan", 0)
            if not plan or int(plan) <= 0:
                logger.error("TorBox: premium membership required (plan=0)")
                return False

            expiry_str = user_info.get("premium_expires_at")
            if expiry_str:
                try:
                    expiration = datetime.strptime(expiry_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=None)
                    logger.info(f"TorBox: {premium_days_left(expiration)}")
                except ValueError:
                    logger.debug(f"TorBox: could not parse premium_expires_at: {expiry_str!r}")
            else:
                logger.info("TorBox: premium account active")
            return True
        except Exception as e:
            logger.error(f"TorBox: failed to validate premium status: {e}")
            return False

    # -----------------------------------------------------------------------
    # A.4 — Local hourly add-torrent guard
    # -----------------------------------------------------------------------

    def _check_add_rate_limit(self) -> None:
        """Raise TorBoxError(RATE_LIMITED) if the local 60/hr guard would be exceeded."""
        now = datetime.now()
        cutoff = now - timedelta(hours=1)
        # Prune old entries
        while self._add_timestamps and self._add_timestamps[0] < cutoff:
            self._add_timestamps.popleft()
        if len(self._add_timestamps) >= self._MAX_ADDS_PER_HOUR:
            oldest = self._add_timestamps[0]
            retry_in = int((oldest + timedelta(hours=1) - now).total_seconds())
            logger.warning(
                f"TorBox: local 60/hr add-torrent guard fired "
                f"({len(self._add_timestamps)} adds in last hour). "
                f"Retry in ~{retry_in}s."
            )
            raise TorBoxError(
                f"Local 60/hr add-limit guard: retry in {retry_in}s",
                TorBoxErrorType.RATE_LIMITED,
            )

    # -----------------------------------------------------------------------
    # Cache checking
    # -----------------------------------------------------------------------

    def check_cache(self, infohash: str) -> bool:
        """
        Check if a single torrent is cached in TorBox WITHOUT adding it.

        Returns True if cached, False otherwise (including on errors).
        """
        try:
            response = self.api.request_handler.execute(
                HttpMethod.GET,
                f"torrents/checkcached?hash={infohash}&format=object&list_files=false",
            )
            if not response:
                return False
            result = response.get(infohash) or response.get(infohash.lower())
            cached = bool(result)
            logger.debug(f"TorBox cache check: {infohash[:8]}... {'IS' if cached else 'is NOT'} cached")
            return cached
        except TorBoxError as e:
            if e.error_type in (TorBoxErrorType.RATE_LIMITED, TorBoxErrorType.SERVICE_ERROR):
                logger.debug(f"TorBox cache check suppressed ({e.error_type.value}): {e.message}")
            else:
                logger.debug(f"TorBox cache check failed for {infohash[:8]}...: {e.message}")
            return False
        except Exception as e:
            logger.debug(f"TorBox cache check error for {infohash[:8]}...: {e}")
            return False

    def check_cache_batch(self, infohashes: List[str]) -> Dict[str, bool]:
        """
        Check cache status for multiple hashes in batches of up to 100.

        TorBox accepts comma-separated hashes — much faster than RD's serial approach.
        Returns dict mapping infohash -> is_cached.
        """
        results: Dict[str, bool] = {h: False for h in infohashes}
        batch_size = 100

        for i in range(0, len(infohashes), batch_size):
            batch = infohashes[i : i + batch_size]
            joined = ",".join(batch)
            try:
                response = self.api.request_handler.execute(
                    HttpMethod.GET,
                    f"torrents/checkcached?hash={joined}&format=object&list_files=false",
                )
                if response:
                    for h in batch:
                        hit = response.get(h) or response.get(h.lower())
                        results[h] = bool(hit)
            except TorBoxError as e:
                logger.debug(f"TorBox batch cache check error: {e.message}")
            except Exception as e:
                logger.debug(f"TorBox batch cache check error: {e}")

        return results

    # -----------------------------------------------------------------------
    # Torrent management
    # -----------------------------------------------------------------------

    def add_torrent(self, infohash: str) -> Tuple[str, TorrentStatus]:
        """
        Add a torrent to TorBox by infohash.

        Returns (torrent_id, TorrentStatus).
        Raises TorBoxError with appropriate error_type.
        """
        self._check_add_rate_limit()

        try:
            magnet = f"magnet:?xt=urn:btih:{infohash.lower()}"
            response = self.api.request_handler.execute(
                HttpMethod.POST,
                "torrents/createtorrent",
                data={"magnet": magnet},
            )
            torrent_id = str(response["torrent_id"])
            logger.debug(f"TorBox: added torrent {infohash[:8]}... → id={torrent_id}")

            # Record the add for rate-limit tracking
            self._add_timestamps.append(datetime.now())
            # Invalidate list cache
            self._list_cache = None

            status = self.get_torrent_status(torrent_id)
            return torrent_id, status

        except TorBoxError:
            raise
        except Exception as e:
            error_msg = str(e)
            if "429" in error_msg:
                raise TorBoxError("Rate limit exceeded", TorBoxErrorType.RATE_LIMITED)
            elif "503" in error_msg:
                if "infringing" in error_msg.lower():
                    raise TorBoxError("Infringing torrent", TorBoxErrorType.LEGAL_BLOCKED)
                raise TorBoxError("Service unavailable", TorBoxErrorType.SERVICE_ERROR)
            elif "404" in error_msg:
                raise TorBoxError("Not found", TorBoxErrorType.NOT_FOUND)
            elif "400" in error_msg:
                raise TorBoxError("Invalid torrent", TorBoxErrorType.INVALID_TORRENT)
            raise TorBoxError(f"Failed to add torrent: {error_msg}", TorBoxErrorType.UNKNOWN)

    def get_torrent_status(self, torrent_id: str) -> TorrentStatus:
        """
        Get the current status of a torrent.

        Uses bypass_cache=true to avoid stale data.
        """
        try:
            data = self.api.request_handler.execute(
                HttpMethod.GET,
                f"torrents/mylist?id={torrent_id}&bypass_cache=true",
            )
            return self._build_torrent_status(torrent_id, data)
        except TorBoxError:
            raise
        except Exception as e:
            raise TorBoxError(f"Failed to get torrent status: {e}", TorBoxErrorType.UNKNOWN)

    def _build_torrent_status(self, torrent_id: str, data: dict) -> TorrentStatus:
        """Build a TorrentStatus from a TorBox /mylist item dict."""
        raw_state = data.get("download_state", "unknown")
        state = raw_state.lower()

        cached_flag = (
            data.get("cached") is True
            or (data.get("download_finished") is True and data.get("download_present") is True)
        )

        files = None
        if data.get("files"):
            files = {
                file["id"]: {
                    "path": file["name"],
                    "filename": file.get("short_name") or file["name"].split("/")[-1],
                    "bytes": file["size"],
                    "selected": True,
                }
                for file in data["files"]
            }

        return TorrentStatus(
            torrent_id=torrent_id,
            status=state,
            progress=data.get("progress", 0.0),
            files=files,
            _is_cached_flag=cached_flag,
        )

    def find_existing_torrent(self, infohash: str) -> Optional[Tuple[str, TorrentStatus]]:
        """
        Check if a torrent with this infohash already exists in the user's TorBox account.

        Results are cached for ~10s to avoid hammering /mylist during the scrape flow.
        Returns (torrent_id, TorrentStatus) if found, None otherwise.
        """
        try:
            now = datetime.now()
            if (
                self._list_cache is None
                or self._list_cache_at is None
                or (now - self._list_cache_at).total_seconds() > self._LIST_CACHE_TTL
            ):
                # bypass_cache so a just-added/just-cached torrent is not missed by
                # the dedup check (a miss costs a duplicate createtorrent against the
                # 60/hr add budget). The local 10s TTL bounds call volume.
                raw = self.api.request_handler.execute(HttpMethod.GET, "torrents/mylist?bypass_cache=true")
                # /mylist returns a list or a dict containing a list
                if isinstance(raw, list):
                    self._list_cache = raw
                elif isinstance(raw, dict):
                    # Sometimes wrapped: {"torrents": [...]}
                    self._list_cache = raw.get("torrents") or raw.get("data") or []
                else:
                    self._list_cache = []
                self._list_cache_at = now

            search_hash = infohash.lower().strip()
            for torrent in self._list_cache:
                torrent_hash = torrent.get("hash", "").lower().strip()
                if torrent_hash == search_hash:
                    torrent_id = str(torrent["id"])
                    status = self._build_torrent_status(torrent_id, torrent)
                    logger.debug(
                        f"TorBox: found existing torrent {infohash[:8]}... "
                        f"id={torrent_id} status={status.status}"
                    )
                    return torrent_id, status

            return None
        except Exception as e:
            logger.debug(f"TorBox: find_existing_torrent failed: {e}")
            return None

    def select_video_files(self, torrent_id: str) -> bool:
        """No-op — TorBox always downloads all files."""
        logger.debug(f"TorBox: select_video_files is a no-op for torrent {torrent_id}")
        return True

    def delete_torrent(self, torrent_id: str) -> bool:
        """Delete a torrent from TorBox."""
        try:
            self.api.request_handler.execute(
                HttpMethod.POST,
                "torrents/controltorrent",
                data={"id": int(torrent_id), "operation": "delete"},
            )
            logger.debug(f"TorBox: deleted torrent {torrent_id}")
            # Invalidate list cache
            self._list_cache = None
            return True
        except Exception as e:
            logger.error(f"TorBox: failed to delete torrent {torrent_id}: {e}")
            return False

    def process_completed_torrent(
        self, torrent_id: str, infohash: str, item_type: str
    ) -> Optional[TorrentContainer]:
        """
        Process a completed torrent and extract valid video files.

        Returns TorrentContainer with valid files, or None if none found.
        """
        try:
            status = self.get_torrent_status(torrent_id)

            if not status.is_cached:
                logger.debug(
                    f"TorBox: torrent {torrent_id} not completed yet (status={status.status})"
                )
                return None

            if not status.files:
                logger.debug(f"TorBox: torrent {torrent_id} has no files")
                return None

            valid_files = []
            for file_id, file_info in status.files.items():
                try:
                    debrid_file = DebridFile.create(
                        path=file_info.get("path", ""),
                        filename=file_info["filename"],
                        filesize_bytes=file_info["bytes"],
                        filetype=item_type,
                        file_id=int(file_id),
                    )
                    if isinstance(debrid_file, DebridFile):
                        valid_files.append(debrid_file)
                except InvalidDebridFileException as e:
                    logger.debug(f"TorBox: skipping file: {e}")
                    continue

            if not valid_files:
                logger.debug(f"TorBox: no valid video files in torrent {torrent_id}")
                return None

            logger.debug(
                f"TorBox: found {len(valid_files)} valid files in torrent {torrent_id}"
            )
            return TorrentContainer(infohash=infohash, files=valid_files)

        except Exception as e:
            logger.error(f"TorBox: failed to process torrent {torrent_id}: {e}")
            return None

    # -----------------------------------------------------------------------
    # Legacy interface — required by DownloaderBase and orchestrator compat
    # -----------------------------------------------------------------------

    def get_torrent_info(self, torrent_id: str) -> TorrentInfo:
        """Get torrent info in the legacy TorrentInfo format."""
        try:
            data = self.api.request_handler.execute(
                HttpMethod.GET,
                f"torrents/mylist?id={torrent_id}",
            )
            files = {
                file["id"]: {
                    "path": file["name"],
                    "filename": file.get("short_name") or file["name"].split("/")[-1],
                    "bytes": file["size"],
                    "selected": True,
                }
                for file in data.get("files", [])
            }
            created_at = None
            if data.get("created_at"):
                try:
                    created_at = datetime.fromisoformat(
                        data["created_at"].replace("Z", "+00:00")
                    ).replace(tzinfo=None)
                except ValueError:
                    pass
            return TorrentInfo(
                id=data["id"],
                name=data["name"],
                status=data.get("download_state"),
                infohash=data.get("hash"),
                bytes=data.get("size"),
                created_at=created_at,
                alternative_filename=None,
                progress=data.get("progress"),
                files=files,
            )
        except Exception as e:
            logger.error(f"TorBox: failed to get torrent info for {torrent_id}: {e}")
            raise

    def select_files(self, torrent_id: str, ids: Optional[List[int]] = None) -> None:
        """Legacy no-op — TorBox downloads all files automatically."""
        logger.debug(
            f"TorBox: select_files is a no-op for torrent {torrent_id} (TorBox downloads all files)"
        )

    def add_torrent_legacy(self, infohash: str) -> str:
        """Legacy compatibility — return torrent_id as string only."""
        torrent_id, _ = self.add_torrent(infohash)
        return torrent_id

    def get_instant_availability(self, infohash: str, item_type: str) -> Optional[TorrentContainer]:
        """
        Legacy method — check cache and process if available.

        DEPRECATED: use check_cache() + add_torrent() + process_completed_torrent().
        """
        logger.debug(f"[LEGACY] TorBox get_instant_availability for {infohash}")
        if not self.check_cache(infohash):
            return None
        existing = self.find_existing_torrent(infohash)
        if existing:
            torrent_id, status = existing
            if status.is_cached:
                return self.process_completed_torrent(torrent_id, infohash, item_type)
            return None
        try:
            torrent_id, status = self.add_torrent(infohash)
            if status.is_cached:
                return self.process_completed_torrent(torrent_id, infohash, item_type)
            return None
        except TorBoxError as e:
            logger.debug(f"TorBox: failed to add torrent {infohash}: {e.message}")
            return None

    def get_instant_availability_or_download(
        self, infohash: str, item_type: str
    ) -> Optional[TorrentContainer]:
        """Legacy compatibility — delegates to get_instant_availability."""
        return self.get_instant_availability(infohash, item_type)
