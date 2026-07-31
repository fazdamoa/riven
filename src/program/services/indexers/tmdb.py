"""TMDB indexer module"""

from datetime import datetime, timedelta
from typing import Generator, Optional, Tuple, Union

from kink import di
from loguru import logger

from program.apis.tmdb_api import TMDBAPI
from program.media.item import Episode, MediaItem, Movie, Season, Show
from program.settings.manager import settings_manager


class TMDBIndexer:
    """TMDB indexer class"""
    key = "TMDBIndexer"

    def __init__(self):
        self.key = "tmdbindexer"
        self.ids = []
        self.initialized = True
        self.settings = settings_manager.settings.indexer
        self.failed_ids = set()
        self.api = di[TMDBAPI]

    @staticmethod
    def copy_attributes(source, target):
        """Copy attributes from source to target."""
        attributes = ["file", "folder", "update_folder", "symlinked", "is_anime", "symlink_path", "subtitles", "requested_by", "requested_at", "overseerr_id", "active_stream", "requested_id", "streams"]
        for attr in attributes:
            target.set(attr, getattr(source, attr, None))

    def copy_items(self, itema: MediaItem, itemb: MediaItem):
        """Copy attributes from itema to itemb recursively."""
        is_anime = itema.is_anime or itemb.is_anime
        if itema.type == "mediaitem" and itemb.type == "show":
            itema.seasons = itemb.seasons
        if itemb.type == "show" and itema.type != "movie":
            for seasona in itema.seasons:
                for seasonb in itemb.seasons:
                    if seasona.number == seasonb.number:  # Check if seasons match
                        for episodea in seasona.episodes:
                            for episodeb in seasonb.episodes:
                                if episodea.number == episodeb.number:  # Check if episodes match
                                    self.copy_attributes(episodea, episodeb)
                                    episodeb.set("is_anime", is_anime)
                        seasonb.set("is_anime", is_anime)
            itemb.set("is_anime", is_anime)
        elif itemb.type == "movie":
            self.copy_attributes(itema, itemb)
            itemb.set("is_anime", is_anime)
        else:
            logger.error(f"Item types {itema.type} and {itemb.type} do not match cant copy metadata")
        return itemb

    def run(self, in_item: MediaItem, log_msg: bool = True) -> Generator[Union[Movie, Show, Season, Episode], None, None]:
        """Run the TMDB indexer for the given item."""
        if not in_item:
            logger.error("Item is None")
            return

        identifier = in_item.imdb_id or in_item.tmdb_id
        if not identifier:
            logger.error(f"Item {in_item.log_string} has no imdb_id or tmdb_id, cannot index it")
            return

        if identifier in self.failed_ids:
            return

        item_type = in_item.type if in_item.type != "mediaitem" else None
        resolved = self._resolve_tmdb_id(in_item, item_type)
        if not resolved:
            logger.error(f"Failed to resolve {identifier} on TMDB")
            self.failed_ids.add(identifier)
            return

        tmdb_id, media_type = resolved
        item = self._build_item(tmdb_id, media_type)

        if not item:
            logger.error(f"Failed to index item with id: {identifier}")
            self.failed_ids.add(identifier)
            return

        if item.type not in ("movie", "show"):
            logger.error(f"Indexed id {identifier} returned the wrong item type: {item.type}")
            self.failed_ids.add(identifier)
            return

        # The scrapers key off IMDb ids. TMDB doesn't have one for every title, so
        # warn rather than fail - those items fall through to the keyword scrapers.
        if not item.imdb_id:
            logger.warning(f"TMDB has no imdb_id for {item.log_string} (tmdb: {tmdb_id}); IMDb-based scrapers will be skipped")

        item = self.copy_items(in_item, item)
        item.indexed_at = datetime.now()

        if log_msg: # used for mapping symlinks to database, need to hide this log message
            logger.info(f"Indexed {identifier} as {item.type.title()}: {item.log_string}")
        yield item

    def _resolve_tmdb_id(self, in_item: MediaItem, item_type: Optional[str]) -> Optional[Tuple[int, str]]:
        """Work out which TMDB record this item refers to.

        Returns a (tmdb_id, media_type) pair, where media_type is TMDB's own
        vocabulary - "movie" or "tv".
        """
        if in_item.tmdb_id and item_type in ("movie", "show"):
            return in_item.tmdb_id, "movie" if item_type == "movie" else "tv"

        if in_item.imdb_id:
            return self.api.get_tmdb_id_from_imdb(in_item.imdb_id, item_type)

        return None

    def _build_item(self, tmdb_id: int, media_type: str) -> Optional[MediaItem]:
        """Fetch and map a movie or a full show tree from TMDB."""
        if media_type == "movie":
            data = self.api.get_movie(tmdb_id)
            return self.api.map_item_from_data(data, "movie") if data else None

        data = self.api.get_show(tmdb_id)
        if not data:
            return None

        show = self.api.map_item_from_data(data, "show")
        if show:
            self._add_seasons_to_show(show, data)
        return show

    @staticmethod
    def should_submit(item: MediaItem) -> bool:
        if not item.indexed_at or not item.title:
            return True

        settings = settings_manager.settings.indexer

        try:
            interval = timedelta(seconds=settings.update_interval)
            return datetime.now() - item.indexed_at > interval
        except Exception:
            logger.error(f"Failed to parse date: {item.indexed_at} with format: {interval}")
            return False

    def _add_seasons_to_show(self, show: Show, data):
        """Attach seasons and episodes from an already-fetched show payload.

        `season_data` is populated by TMDBAPI.get_show, which appends the season
        payloads to the show request rather than fetching them one at a time.
        """
        season_data = getattr(data, "season_data", None) or {}
        if not season_data:
            logger.debug(f"TMDB returned no season data for {show.log_string}")
            return

        for number in sorted(season_data):
            if number == 0:
                # Specials - skipped, as they were with Trakt.
                continue

            payload = season_data[number]
            # Appended season payloads don't always carry their own number.
            if getattr(payload, "season_number", None) is None:
                payload.season_number = number

            season_item = self.api.map_item_from_data(payload, "season", show.genres, show.imdb_id)
            if not season_item:
                continue

            for episode in getattr(payload, "episodes", None) or []:
                episode_item = self.api.map_item_from_data(episode, "episode", show.genres, show.imdb_id)
                if episode_item:
                    season_item.add_episode(episode_item)

            show.add_season(season_item)
