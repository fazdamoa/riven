import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import List, Optional, Tuple

from requests import Session

from program import MediaItem
from program.media import Episode, Movie, Season, Show
from program.settings.models import TMDBIndexerModel
from program.utils.request import (
    BaseRequestHandler,
    HttpMethod,
    ResponseObject,
    ResponseType,
    create_service_session,
    get_cache_params,
    get_rate_limit_params,
    logger,
)


class TMDBAPIError(Exception):
    """Base exception for TMDBApi related errors"""


class TMDBRequestHandler(BaseRequestHandler):
    def __init__(
        self,
        session: Session,
        response_type=ResponseType.SIMPLE_NAMESPACE,
        request_logging: bool = False,
    ):
        super().__init__(
            session,
            response_type=response_type,
            custom_exception=TMDBAPIError,
            request_logging=request_logging,
        )

    def execute(self, method: HttpMethod, endpoint: str, **kwargs) -> ResponseObject:
        return super()._request(method, endpoint, **kwargs)


class TMDBAPI:
    """Handles TMDB API communication"""

    BASE_URL = "https://api.themoviedb.org/3"

    # TMDB accepts at most 20 append_to_response keys per request. We spend two on
    # metadata and the rest on seasons, which keeps the overwhelming majority of
    # shows to a single HTTP request.
    MAX_APPEND_KEYS = 20
    METADATA_APPENDS = ("external_ids", "alternative_titles")
    MAX_SEASON_APPENDS = MAX_APPEND_KEYS - len(METADATA_APPENDS)

    ANIMATION_GENRE_ID = 16
    ANIME_COUNTRIES = {"jp", "kr", "cn", "hk", "tw"}
    ANIME_LANGUAGES = {"ja", "ko", "zh"}

    def __init__(self, settings: TMDBIndexerModel):
        self.settings = settings
        rate_limit_params = get_rate_limit_params(max_calls=40, period=10)
        cache_params = get_cache_params("tmdb", 86400)
        use_cache = os.environ.get("SKIP_TMDB_CACHE", "false").lower() != "true"
        session = create_service_session(
            rate_limit_params=rate_limit_params,
            use_cache=use_cache,
            cache_params=cache_params,
        )
        session.headers.update(
            {
                "Content-type": "application/json",
                "Authorization": f"Bearer {self.settings.api_key}",
            }
        )
        self.request_handler = TMDBRequestHandler(session)

    # -- plumbing ---------------------------------------------------------------

    def _params(self, **extra) -> dict:
        params = {"language": self.settings.language}
        if self.settings.include_adult:
            params["include_adult"] = "true"
        params.update({k: v for k, v in extra.items() if v is not None})
        return params

    def _get(self, endpoint: str, **params) -> Optional[SimpleNamespace]:
        """GET an endpoint, returning parsed data or None on any failure."""
        try:
            response = self.request_handler.execute(
                HttpMethod.GET,
                f"{self.BASE_URL}/{endpoint}",
                params=self._params(**params),
                timeout=self.settings.request_timeout,
            )
        except TMDBAPIError as e:
            logger.error(f"TMDB request to {endpoint} failed: {e}")
            return None
        if not response.is_ok or not response.data:
            logger.error(f"TMDB request to {endpoint} returned no data")
            return None
        return response.data

    def validate(self) -> bool:
        """Check the configured API key can reach TMDB."""
        if not self.settings.api_key:
            logger.error("TMDB API key is not set.")
            return False
        return self._get("configuration") is not None

    # -- lookups ----------------------------------------------------------------

    def find_by_external_id(
        self, external_id: str, source: str = "imdb_id"
    ) -> Optional[SimpleNamespace]:
        """Wrapper for TMDB's /find endpoint. `source` is imdb_id or tvdb_id."""
        if not external_id:
            return None
        return self._get(f"find/{external_id}", external_source=source)

    def get_tmdb_id_from_imdb(
        self, imdb_id: str, item_type: str = None
    ) -> Optional[Tuple[int, str]]:
        """Resolve an IMDb id to a (tmdb_id, media_type) pair.

        `media_type` is TMDB's own vocabulary - "movie" or "tv".
        """
        data = self.find_by_external_id(imdb_id, "imdb_id")
        if not data:
            return None

        movies = getattr(data, "movie_results", []) or []
        shows = getattr(data, "tv_results", []) or []

        # Honour a requested type where we have one, so a show doesn't get indexed
        # as a movie just because TMDB listed it first.
        if item_type in ("movie",) and movies:
            return movies[0].id, "movie"
        if item_type in ("show", "tv") and shows:
            return shows[0].id, "tv"

        if movies:
            return movies[0].id, "movie"
        if shows:
            return shows[0].id, "tv"

        logger.debug(f"TMDB has no movie or tv result for {imdb_id}")
        return None

    def get_movie(self, tmdb_id: str | int) -> Optional[SimpleNamespace]:
        """Movie details with alternative titles and external ids attached."""
        if not tmdb_id:
            return None
        return self._get(
            f"movie/{tmdb_id}",
            append_to_response=",".join(self.METADATA_APPENDS),
        )

    def get_show(self, tmdb_id: str | int) -> Optional[SimpleNamespace]:
        """Show details with every season's episode list attached.

        Season payloads are appended optimistically on the first request - we don't
        know how many seasons exist until the response arrives, and TMDB simply
        omits appends for seasons that aren't there. Any season the show turns out
        to have but we didn't ask for is fetched in follow-up batches, so long
        runners cost 2-3 requests instead of one per season.

        Returns the details object with a `season_data` dict of
        {season_number: season payload} attached.
        """
        if not tmdb_id:
            return None

        appends = list(self.METADATA_APPENDS) + [
            f"season/{n}" for n in range(1, self.MAX_SEASON_APPENDS + 1)
        ]
        data = self._get(f"tv/{tmdb_id}", append_to_response=",".join(appends))
        if not data:
            return None

        season_data = self._collect_appended_seasons(data)

        # Season 0 is specials - deliberately skipped, as it was with Trakt.
        wanted = {
            s.season_number
            for s in (getattr(data, "seasons", []) or [])
            if getattr(s, "season_number", 0) > 0
        }
        missing = sorted(wanted - season_data.keys())
        if missing:
            logger.debug(
                f"TMDB show {tmdb_id} has {len(missing)} season(s) beyond the append "
                f"budget, fetching in follow-up requests: {missing}"
            )
            season_data.update(self._fetch_remaining_seasons(tmdb_id, missing))

        data.season_data = season_data
        return data

    def _collect_appended_seasons(self, data: SimpleNamespace) -> dict:
        """Pull `season/N` keys out of an appended response."""
        seasons = {}
        for key, value in vars(data).items():
            if not key.startswith("season/"):
                continue
            try:
                number = int(key.split("/", 1)[1])
            except ValueError:
                continue
            if value is not None:
                seasons[number] = value
        return seasons

    def _fetch_remaining_seasons(self, tmdb_id: str | int, missing: List[int]) -> dict:
        """Fetch seasons that didn't fit in the first request's append budget."""
        seasons = {}
        for i in range(0, len(missing), self.MAX_APPEND_KEYS):
            batch = missing[i : i + self.MAX_APPEND_KEYS]
            data = self._get(
                f"tv/{tmdb_id}",
                append_to_response=",".join(f"season/{n}" for n in batch),
            )
            if data:
                seasons.update(self._collect_appended_seasons(data))
        return seasons

    def get_season(
        self, tmdb_id: str | int, season_number: int
    ) -> Optional[SimpleNamespace]:
        """Single season with its episode list."""
        if not tmdb_id:
            return None
        return self._get(f"tv/{tmdb_id}/season/{season_number}")

    def search(
        self, query: str, item_type: str = "multi", year: int = None
    ) -> List[SimpleNamespace]:
        """Search TMDB by title. `item_type` is movie, tv or multi."""
        if not query:
            return []
        year_param = {"year": year} if year and item_type == "movie" else {}
        if year and item_type == "tv":
            year_param = {"first_air_date_year": year}
        data = self._get(f"search/{item_type}", query=query, **year_param)
        return getattr(data, "results", []) or [] if data else []

    # -- external id resolution (drop-in replacements for the Trakt helpers) -----

    def get_imdbid_from_tmdb(self, tmdb_id: str, type: str = "movie") -> Optional[str]:
        """Resolve a TMDB id to an IMDb id."""
        if not tmdb_id:
            return None

        if type in ("show", "tv"):
            data = self._get(f"tv/{tmdb_id}/external_ids")
            imdb_id = getattr(data, "imdb_id", None) if data else None
        else:
            data = self._get(f"movie/{tmdb_id}")
            imdb_id = getattr(data, "imdb_id", None) if data else None

        if imdb_id and imdb_id.startswith("tt"):
            return imdb_id
        logger.error(f"Failed to fetch imdb_id for tmdb_id: {tmdb_id}")
        return None

    def get_imdbid_from_tvdb(self, tvdb_id: str, type: str = "show") -> Optional[str]:
        """Resolve a TVDB id to an IMDb id, via TMDB."""
        if not tvdb_id:
            return None

        data = self.find_by_external_id(tvdb_id, "tvdb_id")
        if not data:
            logger.error(f"Failed to fetch imdb_id for tvdb_id: {tvdb_id}")
            return None

        shows = getattr(data, "tv_results", []) or []
        movies = getattr(data, "movie_results", []) or []
        if shows:
            return self.get_imdbid_from_tmdb(shows[0].id, "tv")
        if movies:
            return self.get_imdbid_from_tmdb(movies[0].id, "movie")

        logger.error(f"Failed to fetch imdb_id for tvdb_id: {tvdb_id}")
        return None

    # -- mapping ----------------------------------------------------------------

    def map_item_from_data(
        self,
        data,
        item_type: str,
        show_genres: List[str] = None,
        show_imdb_id: str = None,
    ) -> Optional[MediaItem]:
        """Map TMDB API data to a MediaItem subclass.

        `show_genres` and `show_imdb_id` carry parent context down to seasons and
        episodes, which TMDB doesn't repeat on child payloads.
        """
        if item_type not in ["movie", "show", "season", "episode"]:
            logger.debug(f"Unknown item type {item_type}, cannot map")
            return None

        if item_type == "movie":
            item = self._map_movie(data)
        elif item_type == "show":
            item = self._map_show(data)
        else:
            item = self._map_child(data, item_type, show_genres, show_imdb_id)

        if item is None:
            return None

        item["type"] = item_type
        item["requested_at"] = datetime.now()
        item["is_anime"] = self._is_anime(item)

        match item_type:
            case "movie":
                return Movie(item)
            case "show":
                return Show(item)
            case "season":
                return Season(item)
            case "episode":
                return Episode(item)

    def _map_movie(self, data) -> dict:
        countries = getattr(data, "production_countries", []) or []
        origin = getattr(data, "origin_country", []) or []
        country = origin[0] if origin else (
            getattr(countries[0], "iso_3166_1", None) if countries else None
        )
        release_date = getattr(data, "release_date", None)

        return {
            "title": getattr(data, "title", None),
            "year": self._year_from(release_date),
            "aired_at": self._parse_air_date(release_date),
            "imdb_id": getattr(data, "imdb_id", None),
            "tvdb_id": None,  # TMDB has no tvdb mapping for movies
            "tmdb_id": getattr(data, "id", None),
            "genres": self._genres(data),
            "network": None,
            "country": country.lower() if country else None,
            "language": getattr(data, "original_language", None),
            "aliases": self._get_aliases(data, "movie"),
        }

    def _map_show(self, data) -> dict:
        external = getattr(data, "external_ids", None)
        networks = getattr(data, "networks", []) or []
        origin = getattr(data, "origin_country", []) or []
        first_air_date = getattr(data, "first_air_date", None)

        return {
            "title": getattr(data, "name", None),
            "year": self._year_from(first_air_date),
            "aired_at": self._parse_air_date(first_air_date),
            "imdb_id": getattr(external, "imdb_id", None) if external else None,
            "tvdb_id": getattr(external, "tvdb_id", None) if external else None,
            "tmdb_id": getattr(data, "id", None),
            "genres": self._genres(data),
            "network": getattr(networks[0], "name", None) if networks else None,
            "country": origin[0].lower() if origin else None,
            "language": getattr(data, "original_language", None),
            "aliases": self._get_aliases(data, "show"),
        }

    def _map_child(
        self, data, item_type: str, show_genres: List[str], show_imdb_id: str
    ) -> dict:
        air_date = getattr(data, "air_date", None)
        number = getattr(
            data, "season_number" if item_type == "season" else "episode_number", None
        )
        if number is None:
            logger.debug(f"TMDB {item_type} payload has no number, skipping")
            return None

        return {
            "title": getattr(data, "name", None),
            "year": self._year_from(air_date),
            "aired_at": self._parse_air_date(air_date),
            # Seasons and episodes inherit the show's imdb id - the scrapers key
            # off the top of the hierarchy anyway (MediaItem.get_top_imdb_id).
            "imdb_id": show_imdb_id,
            "tvdb_id": None,
            "tmdb_id": getattr(data, "id", None),
            "genres": show_genres,
            "number": number,
        }

    # -- helpers ----------------------------------------------------------------

    @staticmethod
    def _year_from(date_string: str) -> Optional[int]:
        if not date_string or len(date_string) < 4:
            return None
        try:
            return int(date_string[:4])
        except ValueError:
            return None

    def _parse_air_date(self, date_string: str) -> Optional[datetime]:
        """Parse a bare YYYY-MM-DD air date, offset past midnight.

        TMDB gives a date with no time. Treating that as local midnight would make
        an item look released up to a day early, so we push it out by the
        configured grace period (24h by default).
        """
        if not date_string:
            return None
        try:
            aired = datetime.strptime(date_string, "%Y-%m-%d")
        except ValueError:
            logger.debug(f"Could not parse TMDB air date: {date_string}")
            return None
        return aired + timedelta(hours=self.settings.air_date_grace_hours)

    @staticmethod
    def _genres(data) -> List[str]:
        """TMDB returns capitalised genre names; Trakt gave lowercase slugs and the
        rest of the codebase expects lowercase."""
        return [
            genre.name.lower()
            for genre in (getattr(data, "genres", []) or [])
            if getattr(genre, "name", None)
        ]

    @staticmethod
    def _get_aliases(data, item_type: str) -> dict:
        """Build {country_code: [titles]} from TMDB alternative titles.

        Country codes are lowercased: TMDB returns them uppercase, but
        scrapers.shared filters this dict against the lowercase codes in
        ranking.languages.exclude. Getting this wrong silently disables
        alias-based language exclusion.
        """
        alternative = getattr(data, "alternative_titles", None)
        if not alternative:
            return {}

        # Movies return `titles`, TV returns `results`.
        entries = getattr(alternative, "titles", None) or getattr(
            alternative, "results", None
        ) or []

        aliases: dict[str, list[str]] = {}
        for entry in entries:
            country = getattr(entry, "iso_3166_1", None)
            title = getattr(entry, "title", None)
            if not country or not title:
                continue
            country = country.lower()
            aliases.setdefault(country, [])
            if title not in aliases[country]:
                aliases[country].append(title)

        # The original title is a useful alias for release matching and TMDB keeps
        # it outside the alternative titles list.
        original = getattr(data, "original_title", None) or getattr(
            data, "original_name", None
        )
        if original:
            origin = getattr(data, "origin_country", []) or []
            key = origin[0].lower() if origin else "original"
            aliases.setdefault(key, [])
            if original not in aliases[key]:
                aliases[key].append(original)

        return aliases

    def _is_anime(self, item: dict) -> bool:
        """Detect anime from genre and origin.

        TMDB has no anime genre, so this is Animation plus an East Asian origin.
        The precise signal is TMDB keyword 210024, but that costs an
        append_to_response slot we'd rather spend on season data.
        """
        if item.get("type") in ("season", "episode"):
            # Computed on the show and copied down - see Show.propagate_attributes_to_childs.
            return False

        if "animation" not in (item.get("genres") or []):
            return False

        country = (item.get("country") or "").lower()
        language = (item.get("language") or "").lower()
        return country in self.ANIME_COUNTRIES or language in self.ANIME_LANGUAGES
