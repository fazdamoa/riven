# TorBox Migration Plan

**Status:** Planning — execute in a fresh session.
**Goal:** Make the existing TorBox downloader functional with the same single-torrent flow used by Real-Debrid, and switch the deployment to use TorBox's native WebDAV via rclone (replacing zurg).
**Approach:** Native rclone + `webdav.torbox.app`. No Decypharr.
**Scope:** Backend only (`riven` container) plus the `docker-compose.yml` mount stack. Frontend untouched.

---

## 0. How code in this repo reaches production

The deployment compose pulls a pre-built image: `fazdamoa/ftb-riven:amd-updates`. That image is built externally from a fork or pipeline that mirrors this repo. **Local edits to `src/` do not reach the running container until that image is rebuilt and republished, or the compose is pointed at a locally-built image.**

Before starting Section A, decide one of:

- **Option 1 — Build locally and push to your own registry.** Tag the local image (e.g. `ghcr.io/<you>/riven:torbox`) and update the compose `image:` line. Use `docker build -t <tag> .` from `c:\projects\riven` using the existing `Dockerfile`. This is the cleanest path for iterating during development.
- **Option 2 — Build locally and use `image:` + `build:` in compose.** Add `build: .` alongside `image: riven-local:torbox` and run `docker compose build riven && docker compose up -d riven`. Simplest for a single-host setup.
- **Option 3 — Push to wherever `fazdamoa/ftb-riven` is built from.** Only if you control that pipeline. Avoid if you don't — pinning to someone else's tag is fragile.

The plan below assumes **Option 2** for development (build from this repo on each iteration) and is agnostic on the production publishing choice. Add a `build:` directive to the riven service in Section G when you make compose changes.

---

## 1. Why this work is needed

The current state of `c:\projects\riven`:

- `src/program/services/downloaders/torbox.py` exists and has a working API client, but its `DownloaderBase` interface is the **old** Riven contract (`get_instant_availability`, `get_instant_availability_or_download`, `add_torrent → str`, `get_torrent_info`, `delete_torrent`, `select_files`).
- `src/program/services/downloaders/realdebrid.py` was refactored into a **new** contract built around a `TorrentStatus` dataclass: `check_cache`, `find_existing_torrent`, `add_torrent → (id, TorrentStatus)`, `get_torrent_status`, `select_video_files`, `process_completed_torrent`.
- The orchestrator in `src/program/services/downloaders/__init__.py` (`Downloader._process_existing_download`, `Downloader._start_download`) calls the **new** contract methods.
- The manual scrape route in `src/routers/secure/scrape.py:328` and surrounding code calls `service.get_torrent_status`, `service.find_existing_torrent`, `service.process_completed_torrent`, `service.select_video_files`, and expects `service.add_torrent` to return `(torrent_id, status)`.

Result: when TorBox is the active service, both the automated download loop and the manual scrape route fail with `AttributeError: 'TorBoxDownloader' object has no attribute 'get_torrent_status'` (observed in production logs `26-05-15 10:11:43`).

Two further moving parts:

- The deployment currently mounts zurg+Real-Debrid into `/mnt/zurg`. The plan replaces that with rclone mounting `webdav.torbox.app` into `/mnt/torbox`.
- The symlinker (`src/program/symlink.py`) has provider-specific code: `_trigger_zurg_refresh` PROPFINDs zurg's WebDAV when files are missing. We add a TorBox-equivalent refresh and extend backoff to tolerate TorBox's 15-minute WebDAV refresh ceiling.

---

## 2. End state

A user with a TorBox standard-plan API key can configure Riven (via env vars or the config UI) with `RIVEN_DOWNLOADERS_TORBOX_ENABLED=true` and `RIVEN_DOWNLOADERS_TORBOX_API_KEY=...`, and the system will:

1. Scrape, rank, and find streams as before.
2. Cache-check each candidate against TorBox via `/torrents/checkcached`.
3. Add only cached torrents (default), tracking them in the `TorrentDownload` table.
4. Look up the resulting files on a rclone-mounted `webdav.torbox.app` at `/mnt/torbox`.
5. Symlink them into `/mnt/library` for Plex.
6. Handle 429s, 5xxs, and TorBox-specific limits (active slots, 60/hour add limit) without infinite-loop spam.

The manual scrape endpoint works the same way it does for Real-Debrid.

---

## 3. Plan of work

Each section is a single discrete change. Execute top-to-bottom in a fresh session; verify after each.

### Section A — Rewrite the TorBox downloader to the new contract

**File:** `src/program/services/downloaders/torbox.py`

#### A.1 — Add `TorBoxError` taxonomy

Mirror `RealDebridErrorType` / `RealDebridError` but with TorBox-specific cases. Required types:

- `RATE_LIMITED` — HTTP 429, or TorBox `ACTIVE_LIMIT`, `COOLDOWN_LIMIT`, or `MONTHLY_LIMIT` error codes in the JSON body
- `LEGAL_BLOCKED` — HTTP 503 with "Infringing Torrent" in detail
- `INVALID_TORRENT` — HTTP 400, or `BOZO_TORRENT` / `BOZO_NZB` / `DOWNLOAD_TOO_LARGE`
- `NOT_FOUND` — HTTP 404, or `ITEM_NOT_FOUND`
- `SERVICE_ERROR` — HTTP 5xx, or `DATABASE_ERROR` / `UNKNOWN_ERROR` / `NO_SERVERS_AVAILABLE_ERROR`
- `PLAN_RESTRICTED` — `PLAN_RESTRICTED_FEATURE`
- `TIMEOUT` — request timeout
- `UNKNOWN` — fallback

Parse the TorBox response envelope (`{success, error, detail, data}`) in `TorBoxRequestHandler.execute` and raise `TorBoxError(detail, type)` when `success` is false, mapping the `error` string to the type. Keep `detail` as the user-facing message.

#### A.2 — Add `TorrentStatus` dataclass

Mirror the RD one. Map TorBox `download_state` values to the properties:

- `is_cached` — `data["cached"] is True` OR `data["download_finished"] is True AND data["download_present"] is True`. The boolean fields are more reliable than the string state for TorBox.
- `is_downloading` — `download_state` in `{"downloading", "queued", "metaDL", "stalledDL (No seeds)", "uploading", "checking", "checkingDL"}` (case-insensitive substring match is safest — TorBox sometimes returns mixed-case multi-word states like `"Downloading"` and `"Stalled (No seeds)"`).
- `is_error` — `download_state` in `{"failed", "error", "missingFiles", "corrupt", "inactive (>30 days)", "timedout (>2 days)"}` OR HTTP 503 was returned for this torrent
- `needs_file_selection` — always `False` on TorBox (TorBox always downloads all files; this is documented)
- Carry `files`, `progress`, `torrent_id`, `error_message` like RD does

Lowercase the `download_state` once on construction and compare against lowercase lookup sets — TorBox's state strings have inconsistent casing.

#### A.3 — Implement the new method surface

Replace the current methods with these (signatures must match RD's exactly so the orchestrator and scrape route work):

| Method | TorBox endpoint | Notes |
|---|---|---|
| `check_cache(infohash) -> bool` | `GET /torrents/checkcached?hash={hash}&format=object&list_files=false` | Set `list_files=false` since we're only checking. Treat empty response or missing hash key as "not cached". Treat 429/503 as not cached and log debug. |
| `check_cache_batch(hashes) -> dict[str, bool]` | `GET /torrents/checkcached?hash={h1},{h2},...&format=object` | TorBox accepts comma-separated hashes, batch up to 100 per call. Loop and merge. This is a strict win over the RD implementation, which had to do them serially. |
| `add_torrent(infohash) -> tuple[str, TorrentStatus]` | `POST /torrents/createtorrent` with `data={"magnet": magnet}` | Response data carries `torrent_id` and `hash`. After add, call `get_torrent_status` once to populate the returned status. Use lowercase magnet. **Be aware: this endpoint is rate-limited to 60/hour per API token.** Catch and re-raise 429 as `TorBoxError(RATE_LIMITED)` with a clear message. |
| `get_torrent_status(torrent_id) -> TorrentStatus` | `GET /torrents/mylist?id={torrent_id}&bypass_cache=true` | `bypass_cache=true` ensures we don't get stale data. Read `data.download_state`, `data.cached`, `data.download_finished`, `data.download_present`, `data.progress`, `data.files`. |
| `find_existing_torrent(infohash) -> Optional[tuple[str, TorrentStatus]]` | `GET /torrents/mylist` then filter | TorBox doesn't have a search-by-hash; we have to list and match. Cache the result for ~10s inside the instance to avoid hammering during the scrape session flow (it gets called multiple times in `start_manual_session`). |
| `select_video_files(torrent_id) -> bool` | n/a | Return `True` immediately — TorBox always downloads all files. Log debug to indicate it was a no-op. |
| `process_completed_torrent(torrent_id, infohash, item_type) -> Optional[TorrentContainer]` | re-uses `get_torrent_status` | Build `DebridFile` objects from `status.files`, applying the same skip-sample, video-extension, and filesize rules as RD. |
| `delete_torrent(torrent_id) -> bool` | `POST /torrents/controltorrent` with `data={"id": int(torrent_id), "operation": "delete"}` | Return `True` on success, `False` on failure. |
| `get_torrent_info(torrent_id) -> TorrentInfo` | `GET /torrents/mylist?id={torrent_id}` | Keep this — the orchestrator's `_update_item_attributes` calls it for folder name. Set `name=data["name"]`, `infohash=data["hash"]`, `bytes=data["size"]`, `created_at=data["created_at"]` (parse the ISO timestamp), and build `files` dict from `data["files"]` using `id`, `name` (full path), `short_name` (basename), `size`. |
| `select_files(torrent_id, ids=None) -> None` | n/a | Keep as a no-op for the manual scrape route, which calls `downloader.select_files(...)` to confirm the user's file picks. Log debug. |
| `add_torrent_legacy(infohash) -> str` | calls new `add_torrent` | The orchestrator's legacy compat path checks `hasattr(self.service, 'add_torrent_legacy')` and uses it if present; provide it for symmetry. Return just the id. |

Keep `get_instant_availability` and `get_instant_availability_or_download` as legacy wrappers that call the new methods, identical to how `realdebrid.py` does it. The orchestrator's legacy compat block expects them.

#### A.4 — Rate-limiting configuration

The current `TorBoxAPI.__init__` sets `per_minute=60` on the session. TorBox's actual limit is **300/min** for most endpoints and **60/hour** for `createtorrent`. Change the session-wide limit to `per_minute=250` (leaving headroom under 300) and add a separate guard around `add_torrent` calls: track `createtorrent` calls in an instance-level deque of timestamps and raise `TorBoxError(RATE_LIMITED, "Local 60/hr add-limit guard")` before hitting the wire if we'd exceed it. This prevents a backfill-by-Sonarr scenario from hard-failing on a hard limit and instead defers cleanly. Log a `WARNING` when the guard fires.

#### A.5 — Premium validation

The current `_validate_premium` reads `user_info["premium_expires_at"]` — confirm this is still the key returned by `GET /user/me`. If TorBox uses `premium_expires_at` or `plan` or both, prefer reading `plan > 0` AND parsing the expiry; the standard plan returns plan code 2.

---

### Section B — Update the manual scrape route

**File:** `src/routers/secure/scrape.py`

The route already prefers `service.add_torrent` returning `(id, status)` (line ~338, checks `isinstance(add_result, tuple)`). With Section A done, this path "just works".

The single inline RD-specific call to `service.api.request_handler.execute(HttpMethod.POST, f"torrents/selectFiles/{torrent_id}", ...)` at lines ~352–356 must be replaced with `service.select_video_files(torrent_id)`. The current code is reaching past the abstraction for a quick fix; remove the inline call entirely and rely on the orchestrator-style flow.

No other changes required in this file — `RealDebridError` import stays, just be aware TorBox raises `TorBoxError` not `RealDebridError`. Either:

- (preferred) Catch both `RealDebridError` and `TorBoxError` in the `except` block, since the route is now provider-agnostic, OR
- Define a common `DebridError` base class in `shared.py` and have both `RealDebridError` and `TorBoxError` inherit from it. This is the cleaner long-term shape but a bigger change; defer to Section F.

For this section, use option 1.

Additionally: bump `ScrapingSession.expires_at` from 5 minutes to 15 minutes when the active downloader is TorBox. The current 5-minute window is too tight given TorBox's 15-min WebDAV refresh cycle and possible add-then-cache delays.

---

### Section C — Symlinker: TorBox-aware refresh and backoff

**File:** `src/program/symlink.py`

#### C.1 — Generalise `_trigger_zurg_refresh`

Rename to `_trigger_mount_refresh` and dispatch on the active downloader:

```python
def _trigger_mount_refresh() -> bool:
    from program.settings.manager import settings_manager
    s = settings_manager.settings.downloaders
    if s.torbox.enabled:
        return _trigger_torbox_refresh()
    # fall back to zurg (RD path)
    return _trigger_zurg_refresh()
```

#### C.2 — Implement `_trigger_torbox_refresh`

Hit `https://webdav.torbox.app/?refresh` (confirm exact path with a manual curl during execution — TorBox's docs say there is a per-account refresh URL but don't precisely document the path). On success the WebDAV listing reflects new state within ~1s. Use a 5s timeout. Cache "last refresh" timestamp on the function (or a module-level dict) and rate-limit to one refresh per 60s — TorBox will throttle if hammered.

The refresh requires auth. Read the TorBox API key from settings and send it as a Bearer header. If TorBox's refresh URL needs WebDAV basic auth (email/password) instead, this is **NOT** something we want to store — emit a debug log "TorBox refresh requires WebDAV credentials; skipping" and return False. The 15-min built-in refresh will catch up regardless; the manual refresh is a nice-to-have.

#### C.3 — Extend backoff for TorBox

In `_calculate_next_attempt`, the current curve is `4s × 2^n` with `n` capped at 6 (so max ~4 minutes after 6 attempts). For TorBox, raise the cap so the curve reaches 20 minutes — uncached/pending files can take that long to appear because TorBox's WebDAV refreshes on its own 15-min schedule. Concretely, change the soft-reset trigger from `symlinked_times == 6` to:

- `symlinked_times == 6` if active downloader is RD (current behaviour)
- `symlinked_times == 10` if active downloader is TorBox

And ensure `_calculate_next_attempt` clamps the delay at e.g. 20 minutes for TorBox vs 4 minutes for RD.

Read the active downloader once at module load (`from program.settings.manager import settings_manager`) rather than per-call — settings are stable for the process lifetime.

---

### Section D — Verify settings, env-var wiring, and orchestrator pickup

**Files:** `src/program/settings/models.py`, `src/program/services/downloaders/__init__.py`

#### D.1 — Settings model

`TorBoxModel` already exists with `enabled` and `api_key`. No change needed.

#### D.2 — Env-var smoke test

The Dockerfile/compose uses `RIVEN_FORCE_ENV=true`. Verify these two env vars wire through correctly:

- `RIVEN_DOWNLOADERS_TORBOX_ENABLED=true`
- `RIVEN_DOWNLOADERS_TORBOX_API_KEY=...`

Test by running `python -c "from program.settings.manager import settings_manager; print(settings_manager.settings.downloaders.torbox)"` inside the container with those env vars set.

#### D.3 — Single-service selection

`Downloader.__init__` picks the first initialized service:

```python
self.service = next((s for s in self.services.values() if s.initialized), None)
```

This iterates `{RealDebridDownloader, AllDebridDownloader, TorBoxDownloader}` in dict order. If a user has both RD and TorBox enabled with valid keys (e.g. during transition), RD wins. Document this as expected behaviour; the workaround is to set `RIVEN_DOWNLOADERS_REAL_DEBRID_ENABLED=false` when migrating. (Multi-downloader support is a future enhancement, **not in scope**.)

---

### Section E — Rewrite the TorBox test

**File:** `src/tests/test_torbox_downloader.py`

The existing test is stale: it imports module-level functions (`add_torrent`, `get_status`, etc.) that don't exist in `torbox.py` and references JSON fixtures (`src/tests/test_data/torbox_magnet_*.json`) that don't exist in the tree. Delete and rewrite.

Model the new test on whatever exists for `RealDebridDownloader` (or write fresh mirroring `test_alldebrid_downloader.py` structure if RD has none). At minimum, cover:

- `check_cache(infohash)` returns `True` for a cached hash, `False` for an uncached hash, `False` on 429
- `check_cache_batch([h1, h2, h3])` returns a dict and handles partial-cache responses
- `add_torrent(infohash)` returns `(id, TorrentStatus)` and the status reflects `cached=True`/`is_cached=True` when the response says so
- `add_torrent` raises `TorBoxError(RATE_LIMITED)` on 429 from createtorrent
- The local 60/hour guard fires after 60 simulated calls within an hour without hitting the network
- `get_torrent_status(id)` maps `download_finished + download_present + cached` correctly to `is_cached=True`
- `get_torrent_status(id)` maps various `download_state` strings to `is_downloading` / `is_error` correctly
- `process_completed_torrent` skips samples, non-video files, and out-of-range filesizes (use small fake files inside the bounds)
- `delete_torrent` posts the correct body shape `{"id": int, "operation": "delete"}`
- `find_existing_torrent` returns `None` when the hash isn't in the list and the tuple when it is

Use the same `requests-mock` or `responses` library pattern that `test_alldebrid_downloader.py` uses; check that file first to stay consistent. Create real fixture JSON files at `src/tests/test_data/torbox_*.json` based on the TorBox API docs sample responses (or capture them with a manual curl during execution).

---

### Section F — Optional cleanup: common `DebridError` base

**Files:** `src/program/services/downloaders/shared.py`, `realdebrid.py`, `torbox.py`, `__init__.py`, `routers/secure/scrape.py`

Define `DebridError(Exception)` with `error_type: DebridErrorType` enum (union of RD + TorBox cases). Make `RealDebridError` and `TorBoxError` subclasses. Update the orchestrator and scrape route to catch `DebridError` instead of `RealDebridError`.

This is **optional** — Section B's option 1 (catch both explicitly) works fine for the migration. Do this only if there's appetite for the cleanup after the rest is green. Mark it as a follow-up issue if you skip it.

---

### Section G — Deployment: replace zurg with rclone+TorBox

**Files:** `docker-compose.yml`, a new `rclone-torbox.conf`

#### G.1 — TorBox WebDAV settings

In the TorBox web dashboard → Settings → Integrations: enable **WebDAV Flatten ON**. This produces a flat root layout (`/file.mkv`), which matches the existing `_get_item_path` fast path in `symlink.py` (root-first lookup). With Flatten ON, no rglob fallback is needed for most files.

#### G.2 — New rclone config

Create `c:\projects\riven\rclone-torbox.conf`:

```ini
[torbox]
type = webdav
url = https://webdav.torbox.app
vendor = other
user = YOUR_TORBOX_EMAIL
pass = OBSCURED_PASSWORD
```

Generate the obscured password with `rclone obscure <password>` — do not commit the plaintext. Use the same email/password as the TorBox website login. **This is the WebDAV auth, NOT the API key.**

If using SSO and you don't have a password, the WebDAV requires a password — set one via the TorBox account settings page first. (Document this in the README / DEPLOYMENT_GUIDE.md.)

#### G.3 — Compose file changes

Replace the `zurg` service entirely (it's only relevant for RD) and reconfigure the existing `rclone` service:

```yaml
  rclone:
    image: rclone/rclone:latest
    container_name: rclone
    restart: unless-stopped
    environment:
      TZ: Europe/London
      PUID: 1000
      PGID: 1000
    volumes:
      - /opt/ftplexdata/mnt/torbox:/data:rshared
      - /opt/ftplexdata/rclone-torbox.conf:/config/rclone/rclone.conf
      - /opt/ftplexdata/rclone/cache:/cache
    cap_add:
      - SYS_ADMIN
    security_opt:
      - apparmor:unconfined
    devices:
      - /dev/fuse:/dev/fuse:rwm
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
    # NOTE: TorBox WebDAV refreshes its listing every 15 min, so dir-cache-time
    # should be short to pick up changes promptly. Keep buffer/cache settings
    # consistent with the zurg config for streaming smoothness.
    command: >
      mount torbox: /data
      --allow-non-empty
      --allow-other
      --uid=1000 --gid=1000 --umask=002
      --vfs-cache-mode full
      --dir-cache-time 30s
      --vfs-cache-max-size 30G
      --vfs-cache-max-age 3h
      --buffer-size 2G
      --vfs-read-chunk-size 256M
      --vfs-read-chunk-size-limit off
      --vfs-read-ahead 2G
      --cache-dir /cache
      --low-level-retries 10
      --log-level INFO
```

Riven service env-var changes:

```yaml
  riven:
    # During development: build from this repo instead of pulling the
    # pre-built fazdamoa image. Remove `image:` if you don't need to tag,
    # or set it to a local tag like `riven-local:torbox` and `docker compose
    # build riven` before bringing it up.
    build: .
    image: riven-local:torbox
    # was: image: fazdamoa/ftb-riven:amd-updates
    # was: pull_policy: always
    environment:
      # ... existing ...
      - RIVEN_SYMLINK_RCLONE_PATH=/mnt/torbox     # was /mnt/zurg/__all__
      - RIVEN_DOWNLOADERS_REAL_DEBRID_ENABLED=false
      - RIVEN_DOWNLOADERS_TORBOX_ENABLED=true
      - RIVEN_DOWNLOADERS_TORBOX_API_KEY=${TORBOX_API_KEY}
```

Remove the `depends_on: zurg` from riven, riven_postgres, and rclone. Remove the entire `zurg` service block.

The host mount path under `/opt/ftplexdata/mnt/torbox` needs creating before first run: `mkdir -p /opt/ftplexdata/mnt/torbox`.

Add `TORBOX_API_KEY` to the `.env` file alongside `PLEX_TOKEN`.

#### G.4 — Plex library path

Plex's library still scans `/mnt/library` (the symlink destination), so no Plex-side changes needed. The library may pick up "file moved" events for any media currently scanned as `/mnt/zurg/__all__/...` resolving to `/mnt/torbox/...` after the cutover. Plan a single library refresh after first successful symlink creation.

---

### Section H — Documentation

**Files:** new `TORBOX_DEPLOYMENT.md`, update `DEPLOYMENT_GUIDE.md`

Write a short page covering:

- Required TorBox plan tier and limits (Standard: 5 active slots, 60/hour adds, 200GB max torrent size, no cool-down)
- Enabling WebDAV Flatten in the TorBox dashboard
- Generating the rclone obscured password
- Setting a TorBox password if signed up via SSO
- Env vars to set
- Expected behaviour: 15-min WebDAV refresh, longer symlink backoff than RD
- How to switch back to RD if needed (set `RIVEN_DOWNLOADERS_TORBOX_ENABLED=false` and `RIVEN_DOWNLOADERS_REAL_DEBRID_ENABLED=true`, swap `RIVEN_SYMLINK_RCLONE_PATH`, restore zurg service)

Add a one-line entry to `CHANGELOG.md` under unreleased: "TorBox downloader refactored to single-torrent contract; native rclone+TorBox WebDAV deployment supported."

---

## 4. Verification steps (in order)

After each section, run these in a fresh shell on the dev box:

1. **A complete** — `pytest src/tests/test_torbox_downloader.py -v` after Section E; before E, just verify module imports cleanly: `python -c "from program.services.downloaders.torbox import TorBoxDownloader, TorBoxError; print('ok')"`
2. **B complete** — `python -c "from routers.secure.scrape import start_manual_session; print('ok')"`
3. **C complete** — `python -c "from program.symlink import _trigger_mount_refresh; print('ok')"`
4. **Full backend boot test** — with TorBox env vars set, `docker compose build riven && docker compose up -d riven` (or whichever build flow you chose in Section 0) and watch logs for `Premium status: ... days left` (validation passed) and no `'TorBoxDownloader' object has no attribute` tracebacks.
5. **Manual scrape end-to-end** — pick a known-cached movie (e.g. Big Buck Bunny), submit a magnet via the frontend → should add to TorBox, return a container, allow file selection, and produce a working symlink within 1–2 minutes.
6. **Uncached add** — submit a magnet for something not in TorBox cache. Expect: `add_torrent` succeeds, status enters `downloading`, orchestrator marks it as active in `TorrentDownload`, polling picks up completion when it caches.
7. **Rate-limit guard** — simulate 60 `createtorrent` calls within an hour (or manually trip it via a unit test); verify the local guard fires before the 61st network call.
8. **Mount smoke test** — `ls /mnt/torbox` from inside the rclone container shows TorBox content; the path matches what `_get_item_path` looks for.
9. **Symlink smoke test** — verify symlinks exist at `/mnt/library/movies/<title>/<title>.mkv` pointing into `/mnt/torbox/<file>.mkv` and that Plex can play one.

---

## 5. Risks and known gotchas

- **60/hour add limit is the real ceiling on aggressive scraping.** Backfilling an entire show library will trip this. The local guard avoids hard failures but it means a library bootstrap takes longer than on RD. Document this.
- **TorBox WebDAV is read-only.** Riven's symlinker never tries to write through the mount, so this is fine, but any future feature that did would break.
- **WebDAV freshness lag.** Cached-but-not-yet-visible window can be up to 15 min. The extended backoff in Section C absorbs this for normal flows; the manual scrape session expiry bump in Section B handles it for interactive sessions.
- **`bypass_cache=true` on `mylist`.** Doubles internal DB load per TorBox's docs. Use it on `get_torrent_status` (where freshness matters) but **NOT** on `find_existing_torrent` (where the 1hr server-side cache is fine).
- **API-key vs WebDAV-password are different credentials.** The API client uses the API key (bearer). rclone uses the WebDAV password (account password). Don't confuse them in env vars or docs.
- **CloudFlare-only serving.** Brief PROPFIND-says-yes-GET-says-404 windows occasionally happen on TorBox's CDN. rclone retries handle it. The `--low-level-retries 10` flag in the mount command above addresses this.
- **The orchestrator's in-memory rate limit (`_download_attempts`) is global to the process.** It will treat TorBox the same as RD for retry counting — fine for now but worth noting.

---

## 6. Out of scope

- Decypharr or any multi-debrid gateway
- Concurrent RD + TorBox operation (one or the other, not both at once)
- Frontend changes — the existing settings UI already exposes TorBox via the model
- Usenet support (`/usenet/createusenetdownload` etc.)
- TorBox web-download support (`/webdl/`)
- The `DebridError` base-class refactor (Section F is explicitly optional)
- Migrating existing RD library data to TorBox (Plex will rescan; old broken symlinks will be cleaned up by Riven's `fix_broken_symlinks` flow)

---

## 7. Execution checklist

- [ ] Section 0 — Decide on image build/publish flow (local build vs registry push)
- [ ] Section A — Rewrite `torbox.py` to new contract (A.1 errors, A.2 status, A.3 methods, A.4 rate limit, A.5 premium)
- [ ] Section B — Fix manual scrape route to use `select_video_files`, catch `TorBoxError`, bump session expiry for TorBox
- [ ] Section C — Generalise mount refresh and extend backoff for TorBox
- [ ] Section D — Confirm settings/env-var wiring
- [ ] Section E — Rewrite TorBox test
- [ ] Section F — *(optional)* `DebridError` base class
- [ ] Section G — Replace zurg with rclone+TorBox in compose
- [ ] Section H — Document the deployment
- [ ] Verification 1–9 pass
