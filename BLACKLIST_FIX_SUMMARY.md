# Riven Blacklist Fix - Summary of Changes

## Problem
Riven was aggressively blacklisting valid torrents, particularly when:
1. Torrents were not cached but still downloading
2. Temporary errors occurred (timeouts, rate limits, etc.)
3. Files didn't match metadata but the torrent itself was valid
4. Real-Debrid showed torrents as "indexed" even though they were cached

This caused the downloader to:
- Skip valid streams unnecessarily
- Delete torrents that were actively downloading
- Never retry streams that could work later
- Waste time re-scraping when good torrents were already available

## Solution Overview
**Core Philosophy: NEVER blacklist valid torrents. Let Real-Debrid manage torrent lifecycle.**

### Changes Made

#### 1. **Real-Debrid Downloader** (`src/program/services/downloaders/realdebrid.py`)

##### `get_instant_availability_or_download()` - Major Improvements
**Before:** 
- Deleted cached torrents after processing
- Deleted torrents on any error
- Poor status handling

**After:**
- **Never deletes torrents** - lets Real-Debrid manage cleanup
- Enhanced status detection:
  - `downloaded` → Process and return immediately (cached)
  - `downloading/queued/waiting_files_selection` → Return None, check later
  - `error/virus/dead` → Try adding fresh torrent
  - Unknown status → Try processing anyway
- Checks for existing torrents before adding duplicates
- Properly handles instantly cached torrents vs downloading ones

##### Exception Handling
**Before:** Deleted torrents on ReadTimeout and other errors

**After:** 
- Never deletes torrents on errors
- Real-Debrid manages its own cleanup
- Problematic torrents will show error status on next check

#### 2. **Downloader Service** (`src/program/services/downloaders/__init__.py`)

##### `validate_stream()` - Removed Blacklisting
**Before:** Blacklisted streams that weren't cached

**After:** 
- Only validates cached streams
- Returns None for uncached (no blacklisting)
- Never blacklists - just returns validation result

##### `validate_stream_for_cache_or_download()` - Improved Logic
**Before:** Blacklisted streams with no valid files

**After:**
- Never blacklists
- Returns None for uncached/downloading (will check again later)
- Logs issues but doesn't permanently block streams

##### `run()` - Smart Download Strategy
**Three-Phase Approach:**

**Phase 1: Check for Cached Streams**
- Scans top 5 non-blacklisted streams
- Identifies which are instantly cached
- **Removed:** Blacklisting on cache check errors

**Phase 2: Download Best Cached Stream**
- Selects highest-ranked cached stream
- Downloads immediately
- **Removed:** Blacklisting on download failures
- **Improved:** Better error logging

**Phase 3: Start Uncached Download**
- If no cached streams, downloads best uncached stream
- Marks item with `active_stream` to track download
- Treats uncached downloads as success (not failure)
- **Removed:** Blacklisting for temporary errors

#### 3. **Key Behavioral Changes**

| Scenario | Old Behavior | New Behavior |
|----------|-------------|--------------|
| Torrent not cached | Blacklist → Try next | Keep checking → Will download |
| Temporary error (timeout) | Blacklist + Delete | Log error → Retry later |
| No matching files | Blacklist | Log → May need metadata update |
| Already downloading | Add duplicate → Blacklist | Detect existing → Skip duplicate |
| Rate limit hit | Blacklist + Delete | Log → Wait → Retry |
| Torrent becomes cached | Blacklist → Re-scrape | Detect → Download immediately |

## Benefits

### 1. **No More False Blacklisting**
- Valid torrents never get permanently blocked
- Temporary errors don't prevent future attempts
- Metadata mismatches don't blacklist good torrents

### 2. **Better Real-Debrid Integration**
- Respects Real-Debrid's torrent management
- Doesn't delete torrents unnecessarily
- Properly detects downloading vs cached states

### 3. **Improved Download Success Rate**
- Keeps uncached torrents downloading
- Retries on next pass if torrent finishes
- Handles "indexed" torrents correctly

### 4. **Cleaner Logs**
- Better status messages
- Clear distinction between cached/uncached/downloading
- Reduced error spam from aggressive blacklisting

## Configuration Impact

**No configuration changes needed!** Your existing settings will work as-is:

```json
{
  "options": {
    "title_similarity": 0.7,
    "remove_all_trash": true,
    "remove_ranks_under": 4998,
    "remove_unknown_languages": false,
    "allow_english_in_languages": true,
    "enable_fetch_speed_mode": true,
    "remove_adult_content": true
  }
}
```

These scraping settings continue to work - the changes only affect **download** behavior.

## Testing Recommendations

### 1. **Monitor Real-Debrid Dashboard**
- Check torrents aren't being deleted unnecessarily
- Verify downloads complete properly
- Watch for duplicate torrents (should be prevented)

### 2. **Check Riven Logs**
Look for these improved log messages:
- `"Torrent {hash} is already cached and ready"` ✅
- `"Torrent {hash} is downloading - will check again on next pass"` ✅
- `"Started download for {item} from {stream}"` ✅
- No more: `"Blacklisting {hash} due to..."` ❌

### 3. **Verify Item States**
Items should progress naturally:
- `Scraped` → `Downloaded` (for cached)
- `Scraped` → stays `Scraped` with `active_stream` (for uncached, rechecks later)

## Expected Behavior Examples

### Example 1: Cached Torrent
```
Phase 1: Checking 5 streams for cached availability
Found cached stream: Black.Phone.2.2025.2160p [hash] (rank: 17050)
Downloaded Black Phone 2 from cached 'Black.Phone.2.2025.2160p' [hash]
✅ Success - Item completed
```

### Example 2: Uncached Torrent
```
Phase 1: Checking 5 streams for cached availability
Stream [hash] is not cached or is downloading - will check again later
Phase 3: Starting download of best uncached stream
Started download for Black Phone 2 from 'Black.Phone.2.2025.2160p' [hash]
✅ Success - Will recheck on next scrape pass
```

### Example 3: Already Downloading
```
Found existing torrent [id] for [hash] with status: downloading
Torrent [hash] is downloading - will check again on next pass
✅ Success - No duplicate added
```

## Migration Notes

### Clearing Old Blacklists (Optional)
If you want to give previously blacklisted streams a fresh chance, you can:

1. **Database method** (if you know SQL):
   ```sql
   UPDATE media_item SET blacklisted_streams = '[]';
   ```

2. **UI method** (if available):
   - Reset/retry failed items
   - Manual scrape action

3. **Natural method**:
   - Just let Riven continue - it will find new streams on next scrape
   - Old blacklists won't affect new streams

### No Breaking Changes
- All existing functionality preserved
- No API changes
- Backward compatible with existing database

## Troubleshooting

### "Items stuck in Scraped state"
**Likely cause:** Uncached torrent is downloading
**Solution:** Wait for Real-Debrid to finish download (check RD dashboard)
**Expected:** Item will complete on next Riven pass

### "Same torrent added multiple times"
**Likely cause:** Very rare edge case with timing
**Solution:** Fixed by existing torrent detection in this patch
**Monitor:** Should not happen with new code

### "No streams being processed"
**Likely cause:** All streams truly unavailable (503 errors, etc.)
**Solution:** Normal - scraper will find new streams on next run
**Check:** Real-Debrid API status, network connectivity

## Performance Impact

✅ **Positive impacts:**
- Fewer API calls (no unnecessary deletions)
- Better resource utilization (keeps valid torrents)
- Faster downloads (uses cached torrents immediately)
- Less re-scraping (doesn't blacklist good streams)

❌ **No negative impacts:**
- No additional API calls
- No increased storage (RD manages cleanup)
- No performance degradation

## Summary

This fix transforms Riven's download strategy from **aggressive blacklisting** to **smart retry logic**, respecting Real-Debrid's torrent management and significantly improving download success rates.

**Key takeaway:** Let Real-Debrid do what it does best (manage torrents), and let Riven do what it does best (find and match content).
