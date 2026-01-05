# Changes Made - Single-Torrent Download System

## Summary

This overhaul implements a **single-torrent-at-a-time** download approach with proper database tracking, error categorization, and background status checking.

## Files Created

### 1. `src/program/services/downloaders/torrent_download.py`
New SQLAlchemy model for tracking download state:
- `TorrentDownload` - Tracks each download attempt
- `TorrentDownloadStatus` - Enum for status states
- Helper methods for querying active/failed downloads

### 2. `src/alembic/versions/20251229_0800_add_torrent_download_tracking.py`
Database migration to create the `TorrentDownload` table.

### 3. `SINGLE_TORRENT_DOWNLOAD_SYSTEM.md`
Comprehensive documentation of the new system.

## Files Modified

### 1. `src/program/services/downloaders/realdebrid.py`
Complete refactor with:
- `RealDebridErrorType` enum for error categorization
- `RealDebridError` exception class with error type
- `TorrentStatus` dataclass for status tracking
- `check_cache()` - Check cache WITHOUT adding torrents
- `add_torrent()` - Returns tuple of (torrent_id, TorrentStatus)
- `get_torrent_status()` - Get current torrent status
- `find_existing_torrent()` - Check if torrent exists in account
- `select_video_files()` - Select files for download
- `process_completed_torrent()` - Extract valid files
- Legacy compatibility methods preserved

### 2. `src/program/services/downloaders/__init__.py`
Complete refactor of Downloader service:
- Single-torrent-at-a-time flow
- Database tracking integration
- Proper error handling with cooldowns
- Background status checker support
- Legacy compatibility methods preserved

### 3. `src/program/program.py`
Added:
- `_check_active_downloads()` - Background job (every 5 minutes)
- `_cleanup_download_records()` - Daily cleanup of old records
- Scheduled these functions in `_schedule_functions()`

### 4. `00_START_HERE.md`
Updated documentation index with new fix information.

## Key Behaviors

### Error Handling
| Error | Action | Cooldown |
|-------|--------|----------|
| 451 Legal | `legal_error` status, skip to next | Never retry |
| 429 Rate Limit | `failed` status | 1 hour |
| 404 Not Found | `failed` status | 24 hours |
| 400 Invalid | `skipped` status | Never retry |
| 5xx Server | `failed` status | 6 hours |

### Download Flow
1. Check for existing active download in database
2. If found: Check RD status, process if ready
3. If not found: Get best stream (skip failed/blacklisted)
4. Create `TorrentDownload` record
5. Add ONE torrent to RD
6. If cached: Process immediately
7. If not cached: Mark as downloading, wait for background checker

### Background Checker (every 5 minutes)
1. Get all `pending`/`downloading` downloads from database
2. Check each torrent's status in RD
3. Update database status
4. Trigger state transitions for completed items

## Migration

Applied automatically on startup. Creates `TorrentDownload` table with indexes.

## Testing

After deployment, check logs for:
- "Attempting download:" followed by ONE stream
- "Torrent blocked (451):" for legal errors
- "Started download (not cached):" for uncached torrents
- "Downloaded (cached):" for instant downloads
- "Checked X active downloads" every 5 minutes
