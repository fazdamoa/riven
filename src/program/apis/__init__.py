from kink import di

from program.settings.manager import settings_manager

from .listrr_api import ListrrAPI, ListrrAPIError
from .mdblist_api import MdblistAPI, MdblistAPIError
from .overseerr_api import OverseerrAPI, OverseerrAPIError
from .plex_api import PlexAPI, PlexAPIError
from .tmdb_api import TMDBAPI, TMDBAPIError


def bootstrap_apis():
    __setup_tmdb()
    __setup_plex()
    __setup_mdblist()
    __setup_overseerr()
    __setup_listrr()

def __setup_tmdb():
    # Registered unconditionally - the indexer is not optional, and external id
    # resolution needs this too.
    tmdbApi = TMDBAPI(settings_manager.settings.indexer.tmdb)
    di[TMDBAPI] = tmdbApi

def __setup_plex():
    if not settings_manager.settings.updaters.plex.enabled:
        return
    plexApi = PlexAPI(settings_manager.settings.updaters.plex.token, settings_manager.settings.updaters.plex.url)
    di[PlexAPI] = plexApi

def __setup_overseerr():
    if not settings_manager.settings.content.overseerr.enabled:
        return
    overseerrApi = OverseerrAPI(settings_manager.settings.content.overseerr.api_key, settings_manager.settings.content.overseerr.url)
    di[OverseerrAPI] = overseerrApi

def __setup_mdblist():
    if not settings_manager.settings.content.mdblist.enabled:
        return
    mdblistApi = MdblistAPI(settings_manager.settings.content.mdblist.api_key)
    di[MdblistAPI] = mdblistApi

def __setup_listrr():
    if not settings_manager.settings.content.listrr.enabled:
        return
    listrrApi = ListrrAPI(settings_manager.settings.content.listrr.api_key)
    di[ListrrAPI] = listrrApi
