# Riven — Project Notes for Claude

## What is Riven?

Riven is a Python backend service (FastAPI + SQLAlchemy + PostgreSQL) that automates torrent-based media acquisition for Plex/Jellyfin/Emby. It integrates with:
- **Debrid services**: Real-Debrid, AllDebrid, TorBox
- **Scrapers**: Torrentio, Knightcrawler, Orionoid, Mediafusion, Comet, Jackett, Prowlarr, Zilean
- **Content sources**: Overseerr, Plex Watchlist, MDBList, Listrr, Trakt
- **Media servers**: Plex, Jellyfin, Emby (via symlink)
- **Torrent ranking**: `rank-torrent-name` (RTN) library

**Version at time of writing**: 0.23.5  
**Entry point**: `src/main.py`  
**Package manager**: Poetry  
**Python**: 3.11+

---

## Directory Structure

```
C:\projects\riven\
├── src/
│   ├── main.py                         # FastAPI app, scheduler
│   ├── auth.py                         # API key auth
│   ├── program/
│   │   ├── program.py                  # Main program loop / scheduler
│   │   ├── state_transition.py         # State machine: determines next service per item
│   │   ├── symlink.py                  # Symlink creation logic
│   │   ├── types.py
│   │   ├── settings/
│   │   │   ├── models.py               # All Pydantic settings models (AppModel, RTNSettingsModel, etc.)
│   │   │   ├── manager.py              # SettingsManager: load/save/env-var injection
│   │   │   ├── versions.py             # RTN ranking profile models (default, best, custom)
│   │   │   └── migratable.py          # MigratableBaseModel base class
│   │   ├── media/
│   │   │   ├── item.py                 # MediaItem, Movie, Show, Season, Episode (SQLAlchemy)
│   │   │   ├── stream.py               # Stream, StreamRelation, StreamBlacklistRelation
│   │   │   ├── state.py                # States enum
│   │   │   └── subtitle.py
│   │   ├── services/
│   │   │   ├── scrapers/
│   │   │   │   ├── __init__.py         # Scraping service: runs all scrapers, merges results
│   │   │   │   └── shared.py           # _parse_results(), rtn.rank(), stream filtering
│   │   │   ├── downloaders/
│   │   │   │   ├── __init__.py         # Downloader: single-torrent-at-a-time logic
│   │   │   │   ├── realdebrid.py
│   │   │   │   ├── alldebrid.py
│   │   │   │   ├── torbox.py
│   │   │   │   ├── shared.py           # DownloaderBase ABC, parse_filename()
│   │   │   │   ├── models.py           # TorrentContainer, DebridFile, etc.
│   │   │   │   └── torrent_download.py # TorrentDownload DB model (state tracking)
│   │   │   ├── content/                # Overseerr, Trakt, Plex watchlist, etc.
│   │   │   ├── indexers/               # Trakt indexer (metadata)
│   │   │   ├── updaters/               # Plex/Jellyfin/Emby library refresh
│   │   │   └── post_processing/        # Subliminal (subtitles)
│   │   ├── db/
│   │   │   ├── db.py
│   │   │   └── db_functions.py
│   │   └── managers/
│   │       ├── event_manager.py
│   │       ├── sse_manager.py
│   │       └── websocket_manager.py
│   ├── routers/
│   │   └── secure/
│   │       ├── settings.py             # GET/POST /settings/* endpoints
│   │       ├── items.py
│   │       └── ...
│   └── alembic/                        # DB migrations
```

---

## Item Lifecycle / State Machine

States (defined in `media/state.py`):

```
Requested → Indexed → Scraped → Downloaded → Symlinked → Completed
                                                              ↓
                                                        PostProcessing (subtitles)
```

`state_transition.py` maps each state to the next service to invoke.

- **Requested** → TraktIndexer (fetch metadata)
- **Indexed** → Scraping (find torrents)
- **Scraped** → Downloader (add to debrid)
- **Downloaded** → Symlinker
- **Symlinked** → Updater (notify Plex/Jellyfin)
- **Completed** → PostProcessing (optional)

Show-level items recurse into seasons and episodes.

---

## Torrent Ranking (RTN)

The `rank-torrent-name` (RTN) library ranks torrent titles.

### Key files

| File | Role |
|------|------|
| `settings/models.py` → `RTNSettingsModel` | Settings passed to RTN + our custom fields |
| `settings/versions.py` → `RankModels` | Profiles: `default`, `best`, `custom` |
| `services/scrapers/shared.py` → `_parse_results()` | Where RTN is called per torrent |

### RTN instance

```python
# shared.py (module-level, constructed once at import)
rtn = RTN(ranking_settings, ranking_model)
```

### Per-torrent ranking call

```python
torrent = rtn.rank(
    raw_title=raw_title,
    infohash=infohash,
    correct_title=correct_title,
    remove_trash=settings_manager.settings.ranking.options["remove_all_trash"],
    aliases=aliases
)
```

`torrent.rank` is an int. `torrent.fetch` indicates whether RTN considers it valid.

---

## Settings System

### Model hierarchy

```
AppModel
├── scraping: ScraperModel
├── ranking: RTNSettingsModel       ← extends RTN's SettingsModel
│   ├── options: dict               ← RTN's options (remove_all_trash, etc.)
│   ├── min_rank_movie: int         ← OUR FIELD (see below)
│   └── min_rank_show: int          ← OUR FIELD (see below)
├── downloaders: DownloadersModel
├── symlink: SymlinkModel
├── updaters: UpdatersModel
├── content: ContentModel
├── indexer: IndexerModel
├── database: DatabaseModel
├── notifications: NotificationsModel
└── post_processing: PostProcessing
```

### Env var injection

`SettingsManager.check_environment()` walks the nested settings dict and maps env vars of the form:

```
RIVEN_{SECTION}_{KEY}=value
```

So `ranking.min_rank_movie` is overridable via:

```
RIVEN_RANKING_MIN_RANK_MOVIE=-200
RIVEN_RANKING_MIN_RANK_SHOW=-500
```

Env vars are applied on first boot (no settings.json) or when `RIVEN_FORCE_ENV=true`.

Types are auto-coerced: bool, int, float, list (JSON), str.

### Settings persistence

- Stored at `data/settings.json` (path from `SETTINGS_FILENAME` env var)
- Loaded/saved via `settings_manager.load()` / `settings_manager.save()`
- API: `GET /settings/get/all`, `POST /settings/set/all`, `POST /settings/set`

---

## Per-Type Rank Thresholds (Custom Feature)

### Problem

RTN's `SettingsModel` has a single global `options["min_rank"]` (or remove_all_trash flag). We want to accept lower-quality streams for TV shows than for movies.

### Implementation

**`src/program/settings/models.py`** — `RTNSettingsModel` adds two fields:

```python
class RTNSettingsModel(SettingsModel, Observable):
    min_rank_movie: int = Field(default=-10000)  # effectively no filter
    min_rank_show: int = Field(default=-10000)   # effectively no filter
```

**`src/program/services/scrapers/shared.py`** — the key problem is that RTN's own `options["remove_ranks_under"]` fires *inside* `rtn.rank()` as a `GarbageTorrent` exception, before we can apply per-type logic. So we temporarily lower RTN's threshold to `min(min_rank_movie, min_rank_show)` before the ranking loop, then restore it in a `finally` block. Our per-type check then runs after `rtn.rank()` returns:

```python
# Lower RTN's internal threshold to the minimum of our two so it doesn't
# pre-filter torrents our per-type logic should be deciding on
original_rtu = ranking.options.get("remove_ranks_under", 0)
effective_rtu = min(ranking.min_rank_movie, ranking.min_rank_show)
if effective_rtu < original_rtu:
    ranking.options["remove_ranks_under"] = effective_rtu

try:
    for infohash, raw_title in results.items():
        torrent = rtn.rank(...)
        if torrent.rank < min_rank:  # per-type threshold
            continue
        # ... other filters ...
finally:
    ranking.options["remove_ranks_under"] = original_rtu  # always restore
```

Item types are: `"movie"`, `"show"`, `"season"`, `"episode"`.
Shows, seasons, and episodes all use `min_rank_show`.

### Important gotcha

RTN's `options["remove_ranks_under"]` is the *global* GUI setting ("remove ranks under" in the UI). If you set `min_rank_show` lower than that value, the code overrides RTN's threshold for the duration of show scraping so that RTN doesn't silently discard torrents that should pass the show threshold. The GUI value for `remove_ranks_under` effectively becomes the floor for movies only (since `min_rank_movie` defaults to matching it).

### Env vars

```bash
RIVEN_RANKING_MIN_RANK_MOVIE=0      # e.g. reject anything below 0 for movies
RIVEN_RANKING_MIN_RANK_SHOW=-200    # more lenient for TV
```

---

## Downloader Notes

- **Single-torrent-at-a-time**: only one active torrent per media item
- State tracked in `TorrentDownload` DB table (added in migration `20251229_0800_...`)
- Exponential backoff when all streams are exhausted (2, 4, 8, ... 512 min, max 10 retries then 7-day cooldown)
- Blacklisted streams are tracked separately from failed downloads
- RD-specific: 451 = legal block (skip, don't retry), 429 = rate limit (1hr cooldown), 404 = torrent gone

---

## Key Gotchas

- `scraping_settings` and `ranking_settings` in `shared.py` are **module-level** — they're resolved once at import time. If you need fresh settings, call `settings_manager.settings.ranking` directly (as `_get_min_rank_for_item` does).
- `rtn` instance in `shared.py` is also module-level. Changing ranking settings at runtime won't rebuild the RTN instance — would need a service restart or re-init.
- `item.type` is a string (`"movie"`, `"show"`, `"season"`, `"episode"`), not an enum.
- `Stream.rank` is an `int` stored in DB. Streams are sorted descending by rank before being attached to an item.
- The frontend is a **separate project** not in this repo.
