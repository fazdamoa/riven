# Technical Deep Dive: Blacklist Removal & Download Logic Improvements

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        Scraper                                   │
│                  (Finds streams & ranks them)                    │
└────────────────────┬────────────────────────────────────────────┘
                     │ Passes ranked streams
                     ↓
┌─────────────────────────────────────────────────────────────────┐
│                     Downloader Service                           │
│  Phase 1: Check cache status for top N streams                  │
│  Phase 2: Download best cached stream if available              │
│  Phase 3: Start download for best uncached stream               │
└────────────────────┬────────────────────────────────────────────┘
                     │ Calls
                     ↓
┌─────────────────────────────────────────────────────────────────┐
│              Real-Debrid Downloader                              │
│  • Check existing torrents (avoid duplicates)                   │
│  • Add new torrents only when needed                            │
│  • Never delete valid torrents                                  │
│  • Return cache status intelligently                            │
└─────────────────────────────────────────────────────────────────┘
```

## State Machine Changes

### Old Flow (Problematic)
```
Scraped → Check Cache
         ├─ Cached? → Download → Downloaded ✅
         └─ Not Cached? → BLACKLIST → Scrape Again ❌
```

### New Flow (Fixed)
```
Scraped → Check Cache
         ├─ Cached? → Download → Downloaded ✅
         ├─ Downloading? → Wait → Check Again Later ⏳
         └─ Not Cached? → Add to RD → Mark active_stream → Wait ⏳
                                                           ↓
                                    Next Pass: Check Again (eventually cached)
```

## Code Changes - Deep Dive

### 1. Real-Debrid: `get_instant_availability_or_download()`

#### Old Logic Flow
```python
1. Add torrent to RD
2. Check if cached
3. If cached: Process files, DELETE torrent, return container
4. If not cached: DELETE torrent, return None
5. On ANY error: DELETE torrent, raise exception
```

**Problems:**
- Deleted valid downloading torrents
- Lost progress on uncached downloads
- Forced re-scraping for already-processing torrents

#### New Logic Flow
```python
1. Check for EXISTING torrent first (avoid duplicates)
   ├─ If exists AND downloaded: Process & return ✅
   ├─ If exists AND downloading: Return None (check later) ⏳
   └─ If exists AND error: Continue to step 2
   
2. Add NEW torrent (if needed)
   ├─ Get status immediately after add
   ├─ If instantly cached: Process & return ✅
   └─ If downloading: Return None (let it download) ⏳
   
3. NEVER delete torrents (let RD manage lifecycle)
```

**Improvements:**
- Detects and reuses existing torrents
- Allows downloads to complete naturally
- Returns proper status for each scenario

#### Key Status Handling

```python
# Status checks with clear outcomes
statuses = {
    "downloaded": "Process immediately and return files",
    "downloading": "Return None, will check again later",
    "queued": "Return None, waiting in RD queue",
    "waiting_files_selection": "Select video files, then check status",
    "magnet_conversion": "Return None, magnet being processed",
    "error": "Try adding fresh torrent",
    "virus": "Try adding fresh torrent",
    "dead": "Try adding fresh torrent",
}
```

### 2. Downloader Service: `run()` Method

#### Phase-Based Processing

**Phase 1: Cache Discovery**
```python
# Scan top N streams for instant availability
for stream in streams_to_try[:5]:
    container = validate_stream_for_cache_or_download(stream, item)
    if container:
        # Found cached stream - save for Phase 2
        cached_streams.append((stream, container))
    # NO BLACKLISTING - just continue checking
```

**Phase 2: Cached Download**
```python
if cached_streams:
    best_stream, container = max(cached_streams, key=lambda x: x[0].rank)
    try:
        download_result = download_cached_stream(best_stream, container)
        update_item_attributes(item, download_result)
        # Success! Mark as downloaded
    except Exception as e:
        # Log error but DON'T blacklist
        # Could be temporary issue (network, etc.)
```

**Phase 3: Uncached Download**
```python
if not download_success and uncached_streams:
    best_uncached = uncached_streams[0]  # Highest rank
    try:
        download_result = download_uncached_stream(best_uncached, item)
        
        if download_result.container.files:
            # Became cached during setup - process immediately
            update_item_attributes(item, download_result)
        else:
            # Still uncached - mark as downloading
            item.active_stream = {
                "infohash": download_result.infohash,
                "id": download_result.id
            }
            download_success = True  # This IS success for uncached
    except Exception as e:
        # Log but DON'T blacklist - could work on retry
```

### 3. Smart Duplicate Detection

#### `_find_existing_torrent()` Enhancement

```python
def _find_existing_torrent(self, infohash: str) -> Optional[dict]:
    """
    Searches user's RD account for existing torrent.
    Prevents duplicate additions.
    """
    torrents = self.api.get_torrents()
    search_hash = infohash.lower().strip()
    
    for torrent in torrents:
        torrent_hash = torrent.get("hash", "").lower().strip()
        if torrent_hash == search_hash:
            return torrent
    
    return None
```

**Why This Matters:**
- Real-Debrid only shows existing torrents via `GET /torrents`
- No native "check if exists" endpoint
- Must search through user's torrent list
- Prevents adding same torrent multiple times

### 4. Exception Handling Philosophy

#### Old Approach (Destructive)
```python
except Exception as e:
    logger.error(f"Error: {e}")
    if torrent_id:
        self.delete_torrent(torrent_id)  # ❌ Destroys progress
    item.blacklist_stream(stream)  # ❌ Blocks future attempts
    raise
```

#### New Approach (Preservative)
```python
except Exception as e:
    error_msg = str(e)
    
    # Log specific error types for debugging
    if "503" in error_msg:
        logger.debug("Infringing torrent or service issue")
    elif "429" in error_msg:
        logger.debug("Rate limit - will retry naturally")
    else:
        logger.error(f"Unexpected error: {e}")
    
    # NO DELETION - RD will clean up if needed
    # NO BLACKLISTING - might work on retry
    # Just return None and try again later
```

## Database Schema Impact

### New Field Usage: `active_stream`

```python
# MediaItem model
active_stream: Optional[dict] = {
    "infohash": str,  # Track which stream is downloading
    "id": str         # Real-Debrid torrent ID
}
```

**Purpose:**
- Tracks items with uncached downloads in progress
- Prevents re-scraping while download completes
- Allows status checks on next pass

**Usage Pattern:**
```python
# When starting uncached download
item.active_stream = {
    "infohash": stream.infohash,
    "id": torrent_id
}

# On next pass
if item.active_stream:
    # Check if torrent finished
    torrent = get_torrent_info(item.active_stream["id"])
    if torrent.status == "downloaded":
        # Download complete - process files
        process_and_complete(item)
```

## Performance Characteristics

### API Call Reduction

#### Before (Per Item)
```
1. Check cache: 1 API call
2. Add torrent: 1 API call  
3. Check status: 1 API call
4. Delete torrent: 1 API call  ❌ Wasteful
5. Try next stream: Repeat steps 1-4
   
Total: 4N calls (where N = number of streams tried)
```

#### After (Per Item)
```
1. Check existing: 1 API call (searches local list)
2. Add torrent: 1 API call (only if not exists)
3. Check status: 1 API call
4. (No deletion) ✅ Saved
   
Total: 2-3 calls per successful download
```

**Savings: ~40-50% reduction in API calls**

### Time Complexity

#### Stream Validation
- Old: O(N) where all N streams might be blacklisted
- New: O(1) - processes first cached or best uncached only

#### Duplicate Detection
- Check existing: O(M) where M = user's torrents in RD
- Typically M < 100, so effectively O(1)
- Much faster than re-adding and deleting

### Memory Usage

#### Old (Blacklist Growth)
```python
# Blacklist grows unbounded
item.blacklisted_streams = [
    stream1, stream2, stream3, ...  # Never cleared
]
# Eventually: Huge blacklist, no streams left
```

#### New (Minimal State)
```python
# Only track current download
item.active_stream = {"infohash": "...", "id": "..."}
# ~100 bytes vs potentially MB of blacklist data
```

## Edge Cases Handled

### 1. Torrent Becomes Cached During Processing
```python
# Add torrent (uncached)
torrent_id = add_torrent(infohash)

# Check status immediately
info = get_torrent_info(torrent_id)

if info.status == "downloaded":
    # Was cached all along! Process immediately
    container = _process_torrent(torrent_id, infohash, item_type)
    return container
else:
    # Still downloading - return None
    return None
```

### 2. Concurrent Scrapes for Same Item
```python
# Thread 1: Checks for existing torrent
existing = _find_existing_torrent(infohash)
if existing:
    return process(existing)  # Reuse

# Thread 2: Also checks
existing = _find_existing_torrent(infohash)
if existing:
    return process(existing)  # Also reuse - no duplicate
```

### 3. Real-Debrid API Failures
```python
try:
    result = api_call()
except RateLimitError:
    # Don't blacklist - natural rate limiting will retry
    return None
except TimeoutError:
    # Don't delete - torrent might be fine
    return None
except APIError as e:
    # Log for debugging but don't destroy state
    logger.error(f"API error: {e}")
    return None
```

### 4. Metadata Mismatch
```python
# Torrent has files but they don't match expected metadata
if not matching_files:
    logger.debug(f"No matching files - metadata might be wrong")
    # DON'T blacklist - torrent is valid, might match later
    # User can fix metadata or torrent might match different item
    return None
```

## Testing Strategy

### Unit Tests Needed
```python
def test_existing_torrent_reuse():
    """Verify existing torrents are reused, not re-added"""
    
def test_downloading_status_handling():
    """Verify downloading torrents return None, not deleted"""
    
def test_no_blacklisting():
    """Verify no blacklist calls in download flow"""
    
def test_active_stream_tracking():
    """Verify uncached downloads set active_stream"""
    
def test_cached_immediate_processing():
    """Verify cached torrents process immediately"""
```

### Integration Tests Needed
```python
def test_full_cached_download_flow():
    """End-to-end cached torrent download"""
    
def test_full_uncached_download_flow():
    """End-to-end uncached torrent download with retry"""
    
def test_multiple_items_same_torrent():
    """Verify no duplicate torrents for different items"""
    
def test_error_recovery():
    """Verify graceful handling of API errors"""
```

### Manual Testing Checklist
- [ ] Add item with cached stream → completes in one pass
- [ ] Add item with uncached stream → shows active_stream, completes later
- [ ] Force API error → no deletion, no blacklist
- [ ] Multiple items same torrent → reuses existing
- [ ] Check RD dashboard → no spam deletions

## Metrics to Monitor

### Success Indicators
- ↑ Download success rate (should reach 90%+)
- ↓ Re-scrape frequency (should be rare)
- ↓ API call volume (40-50% reduction)
- ↓ Blacklist growth (should be near zero)
- ↓ Duplicate torrents in RD

### Warning Signs
- Items stuck in Scraped state for >24h (check RD health)
- Duplicate torrents appearing (rare race condition)
- Excessive "error" status torrents (upstream issue)

## Backward Compatibility

### Database Migration: None Required
- New fields are optional
- Old items continue to work
- Blacklists ignored but not removed (harmless)

### API Compatibility: Maintained
- No breaking changes to models
- Same service interface
- Enhanced behavior is transparent

### Configuration: No Changes
- All settings continue to work
- No new configuration needed
- Existing deployments unaffected

## Future Enhancements

### Possible Improvements
1. **Smart retry scheduling**: Exponential backoff for failed items
2. **Torrent health monitoring**: Track download progress
3. **Batch operations**: Check multiple torrents at once
4. **Cache preloading**: Pre-check top streams before download phase
5. **Historical success tracking**: Learn which streams work best

### Not Recommended
- ❌ Re-introducing blacklisting (defeats the purpose)
- ❌ Auto-deleting torrents (let RD manage)
- ❌ Aggressive caching (RD does this better)

## Conclusion

This fix transforms Riven from a "trial and error with punishment" system to a "check and wait" system that respects the natural lifecycle of debrid torrents. The key insight is:

> **Absence of instant cache ≠ Failure**

By removing the false equivalence between "not cached" and "should blacklist", we enable Riven to work with Real-Debrid's actual behavior rather than against it.
