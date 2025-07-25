"""Debug utilities for investigating stream and download issues"""

from loguru import logger
from program.db.db_functions import get_item_by_id, reset_streams
from program.media.state import States
from program.services.downloaders.realdebrid import RealDebridDownloader
from program.utils.request import HttpMethod


def debug_item_streams(item_id: str):
    """Debug an item's stream status and RD torrents"""
    item = get_item_by_id(item_id)
    if not item:
        print(f"Item {item_id} not found")
        return
    
    print(f"Item: {item.log_string}")
    print(f"State: {item.last_state}")
    print(f"Regular streams: {len(item.streams)}")
    print(f"Blacklisted streams: {len(item.blacklisted_streams)}")
    print(f"Active stream: {item.active_stream}")
    
    if item.streams:
        print("\\nRegular streams:")
        for i, stream in enumerate(item.streams[:5]):  # Show top 5
            print(f"  {i+1}. {stream.infohash}: {stream.raw_title}")
    
    if item.blacklisted_streams:
        print("\\nBlacklisted streams:")
        for i, stream in enumerate(item.blacklisted_streams[:5]):  # Show top 5
            print(f"  {i+1}. {stream.infohash}: {stream.raw_title}")
    
    # Check what's actually in Real-Debrid
    rd = RealDebridDownloader()
    if rd.initialized:
        try:
            torrents = rd.api.request_handler.execute(HttpMethod.GET, "torrents")
            print(f"\\nReal-Debrid torrents ({len(torrents)} total):")
            
            # Look for torrents that match our streams
            all_stream_hashes = set()
            for stream in item.streams + item.blacklisted_streams:
                all_stream_hashes.add(stream.infohash.lower())
            
            matching_torrents = []
            for torrent in torrents:
                torrent_hash = torrent.get('hash', '').lower()
                if torrent_hash in all_stream_hashes:
                    matching_torrents.append(torrent)
            
            if matching_torrents:
                print("  Matching torrents found:")
                for torrent in matching_torrents:
                    print(f"    {torrent['hash']}: {torrent['status']} - {torrent.get('filename', 'N/A')}")
            else:
                print("  No matching torrents found in Real-Debrid")
                
        except Exception as e:
            print(f"Failed to get RD torrents: {e}")


def fix_stuck_item(item_id: str):
    """Fix a stuck item by clearing blacklist and active stream"""
    item = get_item_by_id(item_id)
    if not item:
        print(f"Item {item_id} not found")
        return
    
    print(f"Fixing stuck item: {item.log_string}")
    
    # Clear blacklist and streams
    reset_streams(item)
    print("  - Cleared streams and blacklist")
    
    # Clear active stream
    item.active_stream = None
    print("  - Cleared active stream")
    
    # Reset state to allow re-scraping
    item.store_state(States.Scraped)
    print(f"  - Set state to {States.Scraped}")
    
    print(f"Fixed {item.log_string} - item can now be re-processed")


def retry_item_with_blacklist_clear(item_id: str):
    """Retry an item by clearing its blacklist but keeping streams"""
    item = get_item_by_id(item_id)
    if not item:
        print(f"Item {item_id} not found")
        return
    
    print(f"Retrying item with blacklist clear: {item.log_string}")
    
    # Move blacklisted streams back to regular streams
    count = item.retry_blacklisted_streams()
    print(f"  - Moved {count} blacklisted streams back to regular streams")
    
    # Clear active stream if it exists
    if item.active_stream:
        item.active_stream = None
        print("  - Cleared active stream")
    
    # Set state to allow re-processing
    item.store_state(States.Scraped)
    print(f"  - Set state to {States.Scraped}")
    
    print(f"Item {item.log_string} ready for retry with all streams available")


def check_rd_torrent_exists(infohash: str):
    """Check if a specific torrent exists in Real-Debrid"""
    rd = RealDebridDownloader()
    if not rd.initialized:
        print("Real-Debrid not initialized")
        return
    
    try:
        existing = rd._find_existing_torrent(infohash)
        if existing:
            print(f"Torrent {infohash} found in RD:")
            print(f"  ID: {existing['id']}")
            print(f"  Status: {existing['status']}")
            print(f"  Filename: {existing.get('filename', 'N/A')}")
            return existing
        else:
            print(f"Torrent {infohash} not found in Real-Debrid")
            return None
    except Exception as e:
        print(f"Error checking RD torrent: {e}")
        return None
