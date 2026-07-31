import asyncio
import re
from datetime import datetime, timedelta
from typing import Any, Dict, Literal, Optional, TypeAlias, Union
from uuid import uuid4

from PTT import parse_title
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from loguru import logger
from pydantic import BaseModel, RootModel
from RTN import ParsedData
from sqlalchemy import select

from program.db import db_functions
from program.db.db import db
from program.media.item import MediaItem
from program.media.stream import Stream as ItemStream
from program.services.downloaders import Downloader
from program.services.indexers.trakt import TraktIndexer
from program.services.scrapers import Scraping
from program.services.scrapers.shared import manual_rtn
from program.types import Event
from program.services.downloaders.models import TorrentContainer, TorrentInfo, DebridFile
from program.services.downloaders.realdebrid import RealDebridError, RealDebridErrorType
from program.services.downloaders.torbox import TorBoxError, TorBoxErrorType


class Stream(BaseModel):
    infohash: str
    raw_title: str
    parsed_title: str
    parsed_data: ParsedData
    rank: int
    lev_ratio: float
    is_cached: bool = False

class ScrapeItemResponse(BaseModel):
    message: str
    streams: Dict[str, Stream]

class StartSessionResponse(BaseModel):
    message: str
    session_id: str
    torrent_id: str
    torrent_info: TorrentInfo
    containers: Optional[TorrentContainer]
    expires_at: str

class SelectFilesResponse(BaseModel):
    message: str
    download_type: Literal["cached", "uncached"]

class UpdateAttributesResponse(BaseModel):
    message: str

class SessionResponse(BaseModel):
    message: str

ContainerMap: TypeAlias = Dict[str, DebridFile]

class Container(RootModel[ContainerMap]):
    """
    Root model for container mapping file IDs to file information.

    Example:
    {
        "4": {
            "filename": "show.s01e01.mkv",
            "filesize": 30791392598
        },
        "5": {
            "filename": "show.s01e02.mkv",
            "filesize": 25573181861
        }
    }
    """
    root: ContainerMap

SeasonEpisodeMap: TypeAlias = Dict[int, Dict[int, DebridFile]]

class ShowFileData(RootModel[SeasonEpisodeMap]):
    """
    Root model for show file data that maps seasons to episodes to file data.

    Example:
    {
        1: {  # Season 1
            1: {"filename": "path/to/s01e01.mkv"},  # Episode 1
            2: {"filename": "path/to/s01e02.mkv"}   # Episode 2
        },
        2: {  # Season 2
            1: {"filename": "path/to/s02e01.mkv"}   # Episode 1
        }
    }
    """

    root: SeasonEpisodeMap

class ScrapingSession:
    def __init__(self, id: str, item_id: str, magnet: str):
        self.id = id
        self.item_id = item_id
        self.magnet = magnet
        self.torrent_id: Optional[Union[int, str]] = None
        self.torrent_info: Optional[TorrentInfo] = None
        self.containers: Optional[TorrentContainer] = None
        self.selected_files: Optional[Dict[str, Dict[str, Union[str, int]]]] = None
        self.created_at: datetime = datetime.now()
        from program.settings.manager import settings_manager
        _session_timeout_minutes = 5  # Real-Debrid default
        try:
            if settings_manager.settings.downloaders.torbox.enabled:
                # TorBox WebDAV refresh cycle + add-then-cache delays need more headroom
                _session_timeout_minutes = 15
        except Exception:
            pass
        self.expires_at: datetime = datetime.now() + timedelta(minutes=_session_timeout_minutes)

class ScrapingSessionManager:
    def __init__(self):
        self.sessions: Dict[str, ScrapingSession] = {}
        self.downloader: Optional[Downloader] = None

    def set_downloader(self, downloader: Downloader):
        """Set the downloader for the session manager"""
        self.downloader = downloader

    def create_session(self, item_id: str, magnet: str) -> ScrapingSession:
        """Create a new scraping session"""
        session_id = str(uuid4())
        session = ScrapingSession(session_id, item_id, magnet)
        # Extend session expiry when TorBox is active (15-min WebDAV refresh cycle)
        if self.downloader and hasattr(self.downloader, 'service'):
            from program.services.downloaders.torbox import TorBoxDownloader
            if isinstance(self.downloader.service, TorBoxDownloader):
                session.expires_at = datetime.now() + timedelta(minutes=15)
        self.sessions[session_id] = session
        return session

    def get_session(self, session_id: str) -> Optional[ScrapingSession]:
        """Get a scraping session by ID"""
        session = self.sessions.get(session_id)
        if not session:
            return None

        if datetime.now() > session.expires_at:
            self.abort_session(session_id)
            return None

        return session

    def update_session(self, session_id: str, **kwargs) -> Optional[ScrapingSession]:
        """Update a scraping session"""
        session = self.get_session(session_id)
        if not session:
            return None

        for key, value in kwargs.items():
            if hasattr(session, key):
                setattr(session, key, value)

        return session

    def abort_session(self, session_id: str):
        """Abort a scraping session"""
        session = self.sessions.pop(session_id, None)
        if session and session.torrent_id and self.downloader:
            try:
                self.downloader.delete_torrent(session.torrent_id)
                logger.debug(f"Deleted torrent for aborted session {session_id}")
            except Exception as e:
                logger.error(f"Failed to delete torrent for session {session_id}: {e}")
        if session:
            logger.debug(f"Aborted session {session_id} for item {session.item_id}")

    def complete_session(self, session_id: str):
        """Complete a scraping session"""
        session = self.get_session(session_id)
        if not session:
            return

        logger.debug(f"Completing session {session_id} for item {session.item_id}")
        self.sessions.pop(session_id)

    def cleanup_expired(self, background_tasks: BackgroundTasks):
        """Cleanup expired scraping sessions"""
        current_time = datetime.now()
        expired = [
            session_id for session_id, session in self.sessions.items()
            if current_time > session.expires_at
        ]
        for session_id in expired:
            background_tasks.add_task(self.abort_session, session_id)

session_manager = ScrapingSessionManager()

router = APIRouter(prefix="/scrape", tags=["scrape"])

def initialize_downloader(downloader: Downloader):
    """Initialize downloader if not already set"""
    if not session_manager.downloader:
        session_manager.set_downloader(downloader)

@router.get(
    "/scrape/{id}",
    summary="Get streams for an item",
    operation_id="scrape_item"
)
def scrape_item(request: Request, id: str) -> ScrapeItemResponse:
    """Get streams for an item"""
    if id.startswith("tt"):
        imdb_id = id
        item_id = None
    else:
        imdb_id = None
        item_id = id

    if services := request.app.program.services:
        indexer = services[TraktIndexer]
        scraper = services[Scraping]
    else:
        raise HTTPException(status_code=412, detail="Scraping services not initialized")

    log_string = None
    with db.Session() as db_session:

        if imdb_id:
            prepared_item = MediaItem({"imdb_id": imdb_id})
            item = next(indexer.run(prepared_item))
        else:
            item: MediaItem = (
                db_session.execute(
                    select(MediaItem)
                    .where(MediaItem.id == item_id)
                )
                .unique()
                .scalar_one_or_none()
            )

        if not item:
            raise HTTPException(status_code=404, detail="Item not found")

        streams: Dict[str, Stream] = scraper.scrape(item, manual=True)
        log_string = item.log_string

    return {
        "message": f"Manually scraped streams for item {log_string}",
        "streams": streams
    }

@router.post(
    "/scrape/start_session",
    summary="Start a manual scraping session",
    operation_id="start_manual_session"
)
async def start_manual_session(
    request: Request,
    background_tasks: BackgroundTasks,
    item_id: str,
    magnet: str
) -> StartSessionResponse:
    session_manager.cleanup_expired(background_tasks)

    def get_info_hash(magnet: str) -> str:
        pattern = r"[A-Fa-f0-9]{40}"
        match = re.search(pattern, magnet)
        return match.group(0) if match else None

    info_hash = get_info_hash(magnet)
    if not info_hash:
        raise HTTPException(status_code=400, detail="Invalid magnet URI")

    # Identify item based on IMDb or database ID
    if item_id.startswith("tt"):
        imdb_id = item_id
        item_id = None
    else:
        imdb_id = None
        item_id = item_id

    if services := request.app.program.services:
        indexer = services[TraktIndexer]
        downloader = services[Downloader]
    else:
        raise HTTPException(status_code=412, detail="Required services not initialized")

    initialize_downloader(downloader)

    if imdb_id:
        prepared_item = MediaItem({"imdb_id": imdb_id})
        item = next(indexer.run(prepared_item))
    else:
        item = db_functions.get_item_by_id(item_id)

    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    logger.debug(f"[ManualScrape] Starting session for item={item.log_string}, infohash={info_hash[:16]}...")

    # Step 1: Check if torrent already exists in the user's debrid account
    existing_torrent = None
    torrent_id = None
    container = None
    service = downloader.service  # Get the underlying service (RealDebridDownloader, etc.)
    
    if hasattr(service, 'find_existing_torrent'):
        logger.debug(f"[ManualScrape] Step 1: Checking if torrent already exists in debrid account...")
        existing_torrent = service.find_existing_torrent(info_hash)
    
    if existing_torrent:
        torrent_id, status = existing_torrent
        logger.info(f"[ManualScrape] Found existing torrent in debrid: id={torrent_id}, status={status.status}, is_cached={status.is_cached}")
        
        if status.is_cached:
            logger.debug(f"[ManualScrape] Torrent is already cached, processing...")
            container = service.process_completed_torrent(torrent_id, info_hash, item.type)
        elif status.needs_file_selection:
            logger.debug(f"[ManualScrape] Torrent needs file selection, selecting files...")
            service.select_video_files(torrent_id)
            # Re-check status after file selection
            status = service.get_torrent_status(torrent_id)
            if status.is_cached:
                container = service.process_completed_torrent(torrent_id, info_hash, item.type)
        else:
            logger.warning(f"[ManualScrape] Existing torrent has status={status.status}, not usable")
    
    # Step 2: If not found or not usable, add the torrent to RD
    # RD will instantly complete it if it's cached on their servers
    if not container:
        logger.debug(f"[ManualScrape] Step 2: Adding torrent to debrid (will be instant if cached)...")
        try:
            import time
            
            # Use the underlying service directly to get both torrent_id and status
            add_result = service.add_torrent(info_hash)
            
            # Handle different return types (tuple vs string)
            if isinstance(add_result, tuple):
                torrent_id, status = add_result
            else:
                torrent_id = add_result
                status = service.get_torrent_status(torrent_id)
            
            logger.info(f"[ManualScrape] Added torrent: id={torrent_id}, status={status.status}")
            
            # Handle file selection if needed
            if status.needs_file_selection:
                logger.debug(f"[ManualScrape] Torrent needs file selection...")
                logger.debug(f"[ManualScrape] Available files: {list(status.files.keys()) if status.files else 'None'}")

                try:
                    service.select_video_files(torrent_id)
                    logger.debug(f"[ManualScrape] File selection complete")
                except Exception as select_err:
                    logger.error(f"[ManualScrape] File selection failed: {select_err}")

                # Wait for debrid service to process
                time.sleep(2)

                # Re-check status
                status = service.get_torrent_status(torrent_id)
                logger.debug(f"[ManualScrape] Status after file selection: {status.status}")
            
            # Poll for completion (cached torrents should complete quickly)
            max_wait = 10  # seconds
            wait_interval = 1
            waited = 0
            
            while not status.is_cached and not status.is_error and waited < max_wait:
                if status.status == "downloading" and status.progress == 0:
                    # Actually downloading from scratch - not cached
                    break
                logger.debug(f"[ManualScrape] Waiting for torrent... status={status.status}, progress={status.progress}")
                time.sleep(wait_interval)
                waited += wait_interval
                status = service.get_torrent_status(torrent_id)
            
            logger.info(f"[ManualScrape] Final status: {status.status}, is_cached={status.is_cached}")
            
            # Check final status
            if status.is_cached:
                logger.debug(f"[ManualScrape] Torrent is cached on RD, processing...")
                container = service.process_completed_torrent(torrent_id, info_hash, item.type)
                if container:
                    logger.debug(f"[ManualScrape] Got container with {len(container.files)} files")
                else:
                    logger.warning(f"[ManualScrape] process_completed_torrent returned None - checking files...")
                    # Debug: let's see what files are available
                    final_status = service.get_torrent_status(torrent_id)
                    logger.debug(f"[ManualScrape] Files in torrent: {final_status.files}")
            elif status.is_downloading or status.status == "queued":
                logger.warning(f"[ManualScrape] Torrent is NOT cached (status={status.status}, progress={status.progress}%)")
                service.delete_torrent(torrent_id)
                raise HTTPException(
                    status_code=400, 
                    detail=f"Torrent is not cached on Real-Debrid (would need to download). Status: {status.status}"
                )
            elif status.is_error:
                logger.error(f"[ManualScrape] Torrent has error status: {status.status}")
                service.delete_torrent(torrent_id)
                raise HTTPException(status_code=400, detail=f"Torrent error: {status.status}")
            else:
                logger.warning(f"[ManualScrape] Unexpected final status: {status.status}")
                
        except (RealDebridError, TorBoxError) as e:
            logger.error(f"[ManualScrape] Failed to add torrent: {e.message} (type={e.error_type})")
            if e.error_type in (RealDebridErrorType.LEGAL_BLOCKED, TorBoxErrorType.LEGAL_BLOCKED):
                raise HTTPException(status_code=400, detail="Torrent is blocked for legal reasons (DMCA)")
            if e.error_type in (RealDebridErrorType.RATE_LIMITED, TorBoxErrorType.RATE_LIMITED):
                raise HTTPException(status_code=429, detail=f"Rate limited: {e.message}")
            raise HTTPException(status_code=400, detail=f"Failed to add torrent: {e.message}")
        except HTTPException:
            raise  # Re-raise HTTP exceptions from the inner logic

    if not container:
        logger.warning(f"[ManualScrape] Failed to get container after all attempts")
        raise HTTPException(
            status_code=400, 
            detail="Could not process torrent. It may not be cached or has no valid video files."
        )
    
    logger.info(f"[ManualScrape] Success! Container has {len(container.files)} files, creating session...")
    session = session_manager.create_session(item_id or imdb_id, info_hash)

    try:
        # We already have torrent_id from the add/find operation above
        torrent_info: TorrentInfo = downloader.get_torrent_info(torrent_id)
        logger.debug(f"[ManualScrape] Got torrent_info: name={torrent_info.name}, status={torrent_info.status}")
        session_manager.update_session(session.id, torrent_id=torrent_id, torrent_info=torrent_info, containers=container)
        logger.info(f"[ManualScrape] Session {session.id} created successfully")
    except Exception as e:
        logger.error(f"[ManualScrape] Failed to create session: {e}")
        background_tasks.add_task(session_manager.abort_session, session.id)
        raise HTTPException(status_code=500, detail=str(e))

    data = {
        "message": "Started manual scraping session",
        "session_id": session.id,
        "torrent_id": torrent_id,
        "torrent_info": torrent_info,
        "containers": container,
        "expires_at": session.expires_at.isoformat()
    }

    return StartSessionResponse(**data)

@router.post(
    "/scrape/select_files/{session_id}",
    summary="Select files for torrent id, for this to be instant it requires files to be one of /manual/instant_availability response containers",
    operation_id="manual_select"
)
def manual_select_files(request: Request, session_id: str, files: Container) -> SelectFilesResponse:
    downloader: Downloader = request.app.program.services.get(Downloader)
    session = session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    if not session.torrent_id:
        session_manager.abort_session(session_id)
        raise HTTPException(status_code=500, detail="No torrent ID found")

    download_type = "uncached"
    if files.model_dump() in session.containers:
        download_type = "cached"

    try:
        downloader.select_files(session.torrent_id, [int(file_id) for file_id in files.root.keys()])
        session.selected_files = files.model_dump()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "message": f"Selected files for {session.item_id}",
        "download_type": download_type
    }

@router.post(
    "/scrape/update_attributes/{session_id}",
    summary="Match container files to item",
    operation_id="manual_update_attributes"
)
async def manual_update_attributes(request: Request, session_id, data: Union[DebridFile, ShowFileData]) -> UpdateAttributesResponse:
    logger.debug(f"[ManualScrape] update_attributes called with session_id={session_id}")
    logger.debug(f"[ManualScrape] Current sessions: {list(session_manager.sessions.keys())}")
    
    session = session_manager.get_session(session_id)
    log_string = None
    if not session:
        logger.error(f"[ManualScrape] Session {session_id} not found! Available: {list(session_manager.sessions.keys())}")
        raise HTTPException(status_code=404, detail="Session not found or expired")
    if not session.item_id:
        session_manager.abort_session(session_id)
        raise HTTPException(status_code=500, detail="No item ID found")

    logger.debug(f"[ManualScrape] update_attributes processing for item_id={session.item_id}")

    with db.Session() as db_session:
        item = None
        
        # Try to find the item
        if str(session.item_id).startswith("tt"):
            # It's an IMDb ID
            logger.debug(f"[ManualScrape] Looking up by IMDb ID: {session.item_id}")
            item = db_functions.get_item_by_external_id(imdb_id=session.item_id)
            logger.debug(f"[ManualScrape] get_item_by_external_id result: {item}")
            
            if not item:
                # Item not in DB yet, need to index it
                logger.debug(f"[ManualScrape] Item not found, indexing from Trakt...")
                prepared_item = MediaItem({"imdb_id": session.item_id})
                item = next(TraktIndexer().run(prepared_item))
                if not item:
                    raise HTTPException(status_code=404, detail="Unable to index item")
                logger.debug(f"[ManualScrape] Indexed item: {item.log_string}, saving to DB...")
                db_session.add(item)
                db_session.commit()
                db_session.refresh(item)
                logger.debug(f"[ManualScrape] Item saved with ID: {item.id}")
        else:
            # It's a database ID
            logger.debug(f"[ManualScrape] Looking up by DB ID: {session.item_id}")
            item = db_functions.get_item_by_id(session.item_id)

        if not item:
            logger.error(f"[ManualScrape] Item not found after all lookup attempts")
            raise HTTPException(status_code=404, detail="Item not found")

        item = db_session.merge(item)
        item_ids_to_submit = set()

        def update_item(item: MediaItem, data: DebridFile, session: ScrapingSession):
            request.app.program.em.cancel_job(item.id)
            item.reset()
            item.file = data.filename
            item.folder = data.filename
            item.alternative_folder = session.torrent_info.alternative_filename
            item.active_stream = {"infohash": session.magnet, "id": session.torrent_info.id}
            # Use the permissive ranker with trash removal off: the user has
            # explicitly chosen this torrent (possibly low-res), so the strict
            # ranker must not be allowed to raise GarbageTorrent here.
            torrent = manual_rtn.rank(session.torrent_info.name, session.magnet, remove_trash=False)
            item.streams.append(ItemStream(torrent))
            item_ids_to_submit.add(item.id)

        if item.type == "movie":
            update_item(item, data, session)

        else:
            logger.debug(f"[ManualScrape] Processing show data: {data}")
            logger.debug(f"[ManualScrape] Show has seasons: {[s.number for s in item.seasons] if hasattr(item, 'seasons') else 'N/A'}")
            
            for season_number, episodes in data.root.items():
                logger.debug(f"[ManualScrape] Processing season {season_number}, episodes: {list(episodes.keys())}")
                
                for episode_number, episode_data in episodes.items():
                    logger.debug(f"[ManualScrape] Looking for S{season_number:02d}E{episode_number:02d}")
                    
                    if item.type == "show":
                        episode = item.get_episode(episode_number, season_number)
                        if episode:
                            logger.debug(f"[ManualScrape] Found episode: {episode.log_string}")
                            update_item(episode, episode_data, session)
                        else:
                            logger.error(f"[ManualScrape] Failed to find episode {episode_number} for season {season_number} for {item.log_string}")
                            # List available seasons/episodes for debugging
                            if hasattr(item, 'seasons'):
                                for s in item.seasons:
                                    eps = [e.number for e in s.episodes] if hasattr(s, 'episodes') else []
                                    logger.debug(f"[ManualScrape] Available: Season {s.number} has episodes: {eps}")
                            continue
                    elif item.type == "season":
                        if (episode := item.parent.get_episode(episode_number, season_number)):
                            update_item(episode, episode_data, session)
                        else:
                            logger.error(f"Failed to find season {season_number} for {item.log_string}")
                            continue
                    elif item.type == "episode":
                        if season_number != item.parent.number and episode_number != item.number:
                            continue
                        update_item(item, episode_data, session)
                        break
                    else:
                        logger.error(f"Failed to find item type for {item.log_string}")
                        continue

        item.store_state()
        log_string = item.log_string
        db_session.merge(item)
        db_session.commit()

    for item_id in item_ids_to_submit:
        request.app.program.em.add_event(Event("ManualAPI", item_id))

    return {"message": f"Updated given data to {log_string}"}

@router.post("/scrape/abort_session/{session_id}", summary="Abort a manual scraping session", operation_id="abort_manual_session")
async def abort_manual_session(
    _: Request,
    background_tasks: BackgroundTasks,
    session_id: str
) -> SessionResponse:
    session = session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired")

    background_tasks.add_task(session_manager.abort_session, session_id)
    return {"message": f"Aborted session {session_id}"}

@router.post(
    "/scrape/complete_session/{session_id}",
    summary="Complete a manual scraping session",
    operation_id="complete_manual_session"
)
async def complete_manual_session(_: Request, session_id: str) -> SessionResponse:
    session = session_manager.get_session(session_id)

    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired")

    if not all([session.torrent_id, session.selected_files]):
        raise HTTPException(status_code=400, detail="Session is incomplete")

    session_manager.complete_session(session_id)
    return {"message": f"Completed session {session_id}"}

class ParseTorrentTitleResponse(BaseModel):
    message: str
    data: list[dict[str, Any]]

@router.post("/parse", summary="Parse an array of torrent titles", operation_id="parse_torrent_titles")
async def parse_torrent_titles(request: Request, titles: list[str]) -> ParseTorrentTitleResponse:
    parsed_titles = []
    if titles:
        for title in titles:
            data = {}
            data["raw_title"] = title
            parsed_data = parse_title(title)
            data = {**data, **parsed_data}
            parsed_titles.append(data)
        if parsed_titles:
            return ParseTorrentTitleResponse(message="Parsed torrent titles", data=parsed_titles)
    else:
        return ParseTorrentTitleResponse(message="No titles provided", data=[])
