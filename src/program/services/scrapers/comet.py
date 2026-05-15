""" Comet scraper module """
import base64
import json
from typing import Dict

import regex
from loguru import logger
from requests import ConnectTimeout, ReadTimeout
from requests.exceptions import RequestException

from program.media.item import MediaItem
from program.services.scrapers.shared import (
    ScraperRequestHandler,
    _get_stremio_identifier,
)
from program.settings.manager import settings_manager
from program.utils.request import (
    HttpMethod,
    RateLimitExceeded,
    create_service_session,
    get_rate_limit_params,
)


class Comet:
    """Scraper for `Comet`"""

    def __init__(self):
        self.key = "comet"
        self.settings = settings_manager.settings.scraping.comet
        self.timeout = self.settings.timeout or 15
        
        rate_limit_params = get_rate_limit_params(per_hour=300) if self.settings.ratelimit else None
        session = create_service_session(rate_limit_params=rate_limit_params)
        self.request_handler = ScraperRequestHandler(session)
        self.initialized = self.validate()
        if not self.initialized:
            return
        logger.success("Comet initialized!")

    def validate(self) -> bool:
        """Validate the Comet settings."""
        if not self.settings.enabled:
            return False
        if not self.settings.url:
            logger.error("Comet URL is not configured and will not be used.")
            return False
        if "elfhosted" in self.settings.url.lower():
            logger.warning("Elfhosted Comet instance is no longer supported. Please use a different instance.")
            return False
        if not isinstance(self.settings.ratelimit, bool):
            logger.error("Comet ratelimit must be a valid boolean.")
            return False
        try:
            url = f"{self.settings.url}/manifest.json"
            response = self.request_handler.execute(HttpMethod.GET, url, timeout=self.timeout)
            if response.is_ok:
                return True
        except Exception as e:
            logger.error(f"Comet failed to initialize: {e}", )
        return False

    def get_encoded_config(self) -> str:
        """Generate the base64 encoded config string for Comet."""
        debrid_services = []
        
        # Real-Debrid
        rd_settings = settings_manager.settings.downloaders.real_debrid
        if rd_settings.enabled and rd_settings.api_key:
            debrid_services.append({
                "service": "realdebrid",
                "apiKey": rd_settings.api_key
            })
            
        # All-Debrid
        ad_settings = settings_manager.settings.downloaders.all_debrid
        if ad_settings.enabled and ad_settings.api_key:
            debrid_services.append({
                "service": "alldebrid",
                "apiKey": ad_settings.api_key
            })
            
        # TorBox
        tb_settings = settings_manager.settings.downloaders.torbox
        if tb_settings.enabled and tb_settings.api_key:
            debrid_services.append({
                "service": "torbox",
                "apiKey": tb_settings.api_key
            })

        config = {
            "maxResultsPerResolution": 100,
            "maxSize": 0,
            "cachedOnly": False,
            "sortCachedUncachedTogether": False,
            "removeTrash": True,
            "resultFormat": ["all"],
            "debridServices": debrid_services,
            "enableTorrent": False,
            "deduplicateStreams": False,
            "scrapeDebridAccountTorrents": False,
            "debridStreamProxyPassword": "",
            "languages": {
                "required": ["en"],
                "allowed": [],
                "exclude": [],
                "preferred": []
            },
            "resolutions": {
                "r2160p": True,
                "r1080p": True,
                "r720p": True,
                "r576p": False,
                "r480p": False,
                "r360p": False,
                "r240p": False,
                "unknown": False
            },
            "options": {
                "remove_ranks_under": 0,
                "allow_english_in_languages": False,
                "remove_unknown_languages": False
            }
        }
        return base64.b64encode(json.dumps(config).encode("utf-8")).decode("utf-8")

    def run(self, item: MediaItem) -> Dict[str, str]:
        """Scrape the comet site for the given media items
        and update the object with scraped streams"""
        try:
            return self.scrape(item)
        except RateLimitExceeded:
            logger.debug(f"Comet ratelimit exceeded for item: {item.log_string}")
        except ConnectTimeout:
            logger.warning(f"Comet connection timeout for item: {item.log_string}")
        except ReadTimeout:
            logger.warning(f"Comet read timeout for item: {item.log_string}")
        except RequestException as e:
            logger.error(f"Comet request exception: {str(e)}")
        except Exception as e:
            logger.error(f"Comet exception thrown: {str(e)}")
        return {}

    def scrape(self, item: MediaItem) -> Dict[str, str]:
        """Wrapper for `Comet` scrape method"""
        identifier, scrape_type, imdb_id = _get_stremio_identifier(item)
        encoded_config = self.get_encoded_config()
        url = f"{self.settings.url}/{encoded_config}/stream/{scrape_type}/{imdb_id}{identifier or ''}.json"

        response = self.request_handler.execute(HttpMethod.GET, url, timeout=self.timeout)
        if not response.is_ok or not hasattr(response.data, "streams") or not response.data.streams:
            logger.log("NOT_FOUND", f"No streams found for {item.log_string}")
            return {}

        torrents: Dict[str, str] = {}
        for stream in response.data.streams:
            info_hash = getattr(stream, "infoHash", None)
            if not info_hash:
                # Try to extract from URL if infoHash is missing (common in modern Comet)
                stream_url = getattr(stream, "url", "")
                match = regex.search(r"/playback/([a-fA-F0-9]{40})", stream_url)
                if match:
                    info_hash = match.group(1)
            
            if not info_hash:
                continue

            description = getattr(stream, "description", "")
            # Title is the first line, strip the document emoji if present
            raw_title = description.split("\n")[0].replace("📄 ", "").strip()
            torrents[info_hash] = raw_title

        if torrents:
            logger.log("SCRAPER", f"Found {len(torrents)} streams for {item.log_string}")
        else:
            logger.log("NOT_FOUND", f"No streams found for {item.log_string}")

        return torrents
