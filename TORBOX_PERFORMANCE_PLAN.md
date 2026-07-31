# TorBox Performance & Stability Plan

**Status:** Planning — execute in a fresh session.
**Goal:** Fix the two observed teething issues from the TorBox migration and harden the surrounding code paths.
**Symptoms being addressed:**
1. Playback start is slow vs RD/zurg, and scrubbing is sluggish (once playing, streaming is fine).
2. Availability detection lags — TorBox GUI shows "cached"/"available" minutes before Riven acts on it.

**Scope:** rclone mount tuning (compose-only), the active-download poller, TorBox API rate limiting, symlinker backoff/refresh, manual-scrape session expiry, and one latent crash. No frontend changes.

---

## 0. Deployment context — read before starting

Production compose (on the server, NOT this repo's `docker-compose.yml`) runs:

- `riven` from image `fazdamoa/ftb-riven:tb` with `pull_policy: always`
- rclone mounts TorBox WebDAV at host `/opt/ftplexdata/mnt/torbox`; riven mounts `/opt/ftplexdata/mnt:/mnt`, so the mount appears at `/mnt/torbox` inside riven. (This pairing is correct — no path bug here.)
- The rclone `command` **already includes** `--rc --rc-addr :5572 --rc-no-auth`. Keep those flags.

Two classes of change in this plan:

- **Compose-only (Section 1):** apply directly to the production compose and restart the rclone container. No image rebuild. Do this first and re-test before touching code.
- **Code changes (Sections 2–6):** edits to this repo. They reach production only when the `fazdamoa/ftb-riven:tb` image is rebuilt from this code and re-pulled, OR the compose is switched to a local build (`build: .` + local tag, as this repo's dev compose does). Decide the publish path before starting Section 2. With `pull_policy: always` on a remote tag, an accidental `docker compose up` can silently revert code changes if the remote image is stale — prefer a distinct tag (e.g. `:tb-perf`) so the running version is unambiguous.

---

## 1. rclone mount retuning (compose-only — biggest win for symptom 1)

**File:** production `docker-compose.yml`, `rclone` service `command`.

### 1.1 Why the current flags are slow

| Current flag | Problem |
|---|---|
| `--vfs-read-chunk-size 256M` | First range request asks the CDN for 256MB. Playback cannot start until the head of the file lands in the VFS cache; TTFB on huge WebDAV ranges is poor. This is the slow-start cause. |
| `--vfs-read-chunk-size-limit off` | Chunk size doubles without bound (256M → 512M → 1G → …). Every scrub aborts the in-flight chunk and opens a new range at the seek offset; tearing down multi-GB requests makes seeking sluggish. |
| `--buffer-size 2G` | Per-open-file **in-memory** buffer, discarded on every seek and refilled from scratch. Also a memory bomb: 3–4 concurrent streams ≈ 6–8GB RAM in the rclone container. |
| `--vfs-read-ahead 2G` | Aggressive sequential prefetch competing with the playback read on the same connection budget. |
| `--dir-cache-time 30s` | With WebDAV Flatten ON the root is one huge listing. Expiring it every 30s forces constant full-root PROPFINDs, and a file open that hits an expired cache pays that listing cost before the first byte. Was set short to compensate for new-file visibility — Section 4 replaces that with on-demand RC `vfs/refresh`, so this can be long. |
| `--allow-non-empty` | Masks a dead mount: if FUSE fails, Plex/Riven silently see an empty directory instead of an error. Remove — fail loudly. |

### 1.2 New command

Replace the rclone `command` with (single line in YAML, shown wrapped here):

```
mount torbox: /data
  --allow-other
  --uid=1000 --gid=1000 --umask=002
  --vfs-cache-mode full
  --dir-cache-time 1h
  --vfs-cache-max-size 30G
  --vfs-cache-max-age 12h
  --buffer-size 32M
  --vfs-read-chunk-size 32M
  --vfs-read-chunk-size-limit 512M
  --vfs-read-ahead 256M
  --vfs-fast-fingerprint
  --no-checksum
  --no-modtime
  --cache-dir /cache
  --low-level-retries 10
  --rc --rc-addr :5572 --rc-no-auth
  --log-level INFO
```

As one line for the compose file:

```yaml
command: "mount torbox: /data --allow-other --uid=1000 --gid=1000 --umask=002 --vfs-cache-mode full --dir-cache-time 1h --vfs-cache-max-size 30G --vfs-cache-max-age 12h --buffer-size 32M --vfs-read-chunk-size 32M --vfs-read-chunk-size-limit 512M --vfs-read-ahead 256M --vfs-fast-fingerprint --no-checksum --no-modtime --cache-dir /cache --low-level-retries 10 --rc --rc-addr :5572 --rc-no-auth --log-level INFO"
```

Flag rationale: small first chunk → fast start; bounded chunk growth → fast seeks; modest buffer → cheap seek recovery and sane memory; `--vfs-fast-fingerprint` skips an extra PROPFIND on each open (`--no-checksum`/`--no-modtime` because WebDAV provides neither reliably); long `dir-cache-time` is safe because Riven triggers `vfs/refresh` on demand (Section 4) and rclone's own 15-min notice of TorBox changes is no longer load-bearing.

Security note: `--rc-no-auth` is acceptable only because port 5572 is **not** published in `ports:` — it is reachable solely on the compose network. Do not add a `ports:` mapping for it.

### 1.3 Apply and verify

```bash
docker compose up -d rclone          # recreate with new command
docker logs rclone --tail 20         # confirm mount came up, no flag errors
# RC reachable from riven's network:
docker exec riven curl -s -X POST http://rclone:5572/vfs/refresh -d recursive=true
# expect: {"result": {"": "OK"}}  (or similar OK json)
ls /opt/ftplexdata/mnt/torbox | head  # host sees content
```

Then test: start a movie (should begin in ~1–3s), scrub forward/back several times (each seek should resume in ~1–2s), and check `docker stats rclone` memory stays modest (<1GB with a couple of streams).

If start latency is good but sustained bitrate stutters on very high-bitrate remuxes, raise only `--vfs-read-ahead` to 512M. Do not raise `--buffer-size` past 64M.

---

## 2. Poll active downloads faster (symptom 2, delay source #1)

**File:** `src/program/program.py`, `_schedule_functions`

Current:

```python
self._check_active_downloads: {"interval": 60 * 5},  # Check every 5 minutes
```

A torrent that finishes caching on TorBox sits unnoticed for up to 5 minutes before Riven even asks. Each check is one cheap `mylist?id=...` call per active download.

Change to 60 seconds:

```python
self._check_active_downloads: {"interval": 60},  # TorBox: check every minute
```

Optional (better) — adaptive polling inside `Downloader.check_active_downloads` (`src/program/services/downloaders/__init__.py`): skip downloads whose `started_at` is older than 15 minutes unless `last_checked_at` is older than 5 minutes. Fresh downloads get 60s granularity, stale ones fall back to 5-min cadence. Only implement if the 60s flat interval generates noticeable API volume (it shouldn't at this library's scale — even 50 active downloads is 50 calls/min, under the limiter in Section 3).

---

## 3. Fix the API rate-limiter regression (symptom 2, delay source #2)

**File:** `src/program/services/downloaders/torbox.py`, `TorBoxAPI.__init__`

Current:

```python
rate_limit_params = get_rate_limit_params(per_minute=60)
```

The migration plan (A.4) specified 250/min against TorBox's actual 300/min general limit; the code shipped with 60. During scrape sessions (batch cache checks + status polls + adds) calls queue behind this limiter, adding artificial seconds-to-minutes of lag exactly when the GUI already says "cached".

Change to:

```python
# TorBox general API limit is 300/min; keep headroom. The 60/hr createtorrent
# limit is enforced separately by _check_add_rate_limit().
rate_limit_params = get_rate_limit_params(per_minute=250)
```

The local 60/hour `createtorrent` deque guard is unchanged and still protects the add endpoint.

---

## 4. Symlinker: backoff shape, refresh call, rglob, and a latent crash

**File:** `src/program/symlink.py`

### 4.1 Fix the missing `return` (latent IndexError)

In `Symlinker.run`, when there are no items:

```python
items = self._get_items_to_update(item)
if not items:
    logger.debug(f"No items to symlink for {item.log_string}")
    yield item
```

Execution falls through to `self._should_submit(items)` → `random.choice([])` → `IndexError` inside the generator. Add `return` after the `yield item`.

### 4.2 Replace exponential backoff with fixed-interval for TorBox

TorBox file appearance is a *bounded*-delay event: once cached, the file is visible within at most one ~15-min WebDAV cycle — and within seconds once the RC refresh works (Section 1 enabled it; this section makes Riven use it properly). Exponential backoff (`4s × 2^n`, capped 20 min) guarantees overshooting the moment the file appears: by attempt 7 the wait is already 8 minutes.

Replace `_calculate_next_attempt` behaviour for TorBox with a fixed 45-second retry, and keep the soft-reset trigger as an elapsed-attempts count that covers ~20 minutes:

```python
def _calculate_next_attempt(self, item) -> datetime:
    if self._is_torbox_active():
        # Fixed cadence: file appears within one WebDAV cycle (≤15 min),
        # usually within seconds of an RC vfs/refresh. Exponential backoff
        # is the wrong shape for a bounded-delay event.
        return datetime.now() + timedelta(seconds=45)
    base_delay = timedelta(seconds=4)
    next_attempt_delay = min(base_delay * (2 ** item.symlinked_times), timedelta(minutes=4))
    return datetime.now() + next_attempt_delay
```

And in `_get_soft_reset_threshold`, return `25` for TorBox (25 × 45s ≈ 19 minutes of trying before soft reset) instead of the current `10`. Keep `6` for RD.

### 4.3 Make the RC refresh recursive and verify it fires

`_trigger_torbox_refresh` posts `{"recursive": "false"}`. With Flatten ON a recursive refresh of the (flat) tree is cheap and also covers any non-flat leftovers. Change to:

```python
response = requests.post(f"{host}/vfs/refresh", json={"recursive": "true"}, timeout=10)
```

The 60s module-level throttle stays. With Section 1 applied, the debug log line `TorBox rclone RC vfs/refresh succeeded via http://rclone:5572` should now appear when a file is missing — previously this silently failed every time because `--rc` was never enabled.

### 4.4 Skip the recursive filesystem walk for TorBox

`_get_item_path` falls back to `rclone_path.rglob(item.file)` plus an `iterdir()` on every miss. Over WebDAV with a cold dir cache this is a PROPFIND storm, and with Flatten ON it is pure waste — the root check already covers TorBox. Guard it:

```python
# rglob fallback is only useful for zurg-style nested layouts; with TorBox
# WebDAV Flatten the root + folder checks above are exhaustive, and rglob
# over WebDAV is a PROPFIND storm.
torbox_active = False
try:
    torbox_active = settings_manager.settings.downloaders.torbox.enabled
except Exception:
    pass
if not torbox_active:
    try:
        for path in rclone_path.rglob(item.file):
            ...
```

Also skip the trailing `list(rclone_path.iterdir())` "force refresh" for TorBox — the RC refresh replaces it.

### 4.5 Bump the post-refresh settle sleep

`_should_submit` sleeps 2s after a refresh before re-checking. A recursive `vfs/refresh` on a large flat root can take slightly longer to complete. Bump to 5s. (The RC call itself is synchronous, so this is belt-and-braces; do not exceed 5s — this runs on the worker thread.)

---

## 5. Manual scrape session expiry (finish the half-done Section B item)

**File:** `src/routers/secure/scrape.py`, `ScrapingSession.__init__`

Current code imports `TorBoxDownloader` with a comment about the 15-minute bump, then unconditionally sets `_session_timeout_minutes = 5` — the migration plan's Section B bump was never implemented. Uncached manual scrapes expire before TorBox surfaces files.

Replace the constructor tail with:

```python
from program.settings.manager import settings_manager
_session_timeout_minutes = 5  # Real-Debrid default
try:
    if settings_manager.settings.downloaders.torbox.enabled:
        # TorBox WebDAV refresh cycle + add-then-cache delays need more headroom
        _session_timeout_minutes = 15
except Exception:
    pass
self.expires_at: datetime = datetime.now() + timedelta(minutes=_session_timeout_minutes)
```

Remove the now-dead `TorBoxDownloader` / `Downloader as _DL` imports in that block.

---

## 6. `find_existing_torrent` freshness (dedup correctness)

**File:** `src/program/services/downloaders/torbox.py`

The `/torrents/mylist` call deliberately omits `bypass_cache=true`, so a just-added or just-cached torrent can be missing/stale in the dedup check. Worst case: a duplicate `createtorrent` call, burning the 60/hr add budget. The instance-level 10s list cache already bounds call volume, so flipping to bypass is safe:

```python
raw = self.api.request_handler.execute(HttpMethod.GET, "torrents/mylist?bypass_cache=true")
```

Keep the 10s local TTL exactly as is.

---

## 7. Build & deploy the code changes

1. From `c:\projects\riven`: `docker build -t <your-registry-or-local>/riven:tb-perf .`
2. Point the production compose `riven` service at the new tag (and drop `pull_policy: always` if using a local-only tag, or push the tag if the server pulls from a registry).
3. `docker compose up -d riven`
4. Watch startup logs for: `Premium status` (TorBox validated), `Rclone path symlinks are pointed to: /mnt/torbox`, `Scheduled _check_active_downloads to run every 60 seconds`.

---

## 8. Verification checklist (in order)

1. **rclone flags live** — `docker inspect rclone --format '{{.Args}}'` shows the new command; no `--allow-non-empty`, no `2G` values.
2. **RC reachable** — `docker exec riven curl -s -X POST http://rclone:5572/vfs/refresh -d recursive=true` returns OK JSON.
3. **Playback start** — a not-recently-played file starts in ~1–3s in Plex.
4. **Scrubbing** — repeated seeks resume within ~1–2s; rclone memory stays <1GB under `docker stats`.
5. **Poller cadence** — riven log shows `Scheduled _check_active_downloads to run every 60 seconds`.
6. **Refresh fires** — request an uncached item; once TorBox shows it cached, riven debug log shows `vfs/refresh succeeded` and the symlink lands within ~1–2 minutes (vs minutes-to-tens-of-minutes before).
7. **Fixed-interval backoff** — `SYMLINKER` log lines for a pending item show "next attempt in 45 seconds" repeatedly rather than a doubling series.
8. **Manual scrape (uncached)** — start a manual session for an uncached torrent; session survives past 5 minutes (15-min expiry).
9. **No IndexError** — grep riven logs for `random.choice` / `IndexError` after a day of running: none.
10. **Rate limiter** — during a busy scrape, no long stalls between consecutive TorBox API debug lines (previously throttled to 1/sec at 60/min).

---

## 9. Rollback

- Section 1 only: restore the previous rclone `command` and `docker compose up -d rclone`.
- Code sections: repoint the compose `image:` at `fazdamoa/ftb-riven:tb` and `docker compose up -d riven`.
- All changes are independent; any section can be rolled back alone.

---

## 10. Out of scope

- Multi-downloader (RD + TorBox concurrently)
- Decypharr or alternative mount gateways
- Frontend changes
- The optional `DebridError` base-class refactor (still deferred from the migration plan)
- Adaptive per-download polling (noted in Section 2 as optional; only if 60s flat cadence proves noisy)

---

## Execution checklist

- [ ] Section 0 — Decide image tag/publish path for code changes
- [ ] Section 1 — Retune rclone mount flags (compose-only), verify playback start + scrub + RC reachability
- [ ] Section 2 — Poller interval 300s → 60s
- [ ] Section 3 — TorBox API limiter 60/min → 250/min
- [ ] Section 4 — Symlinker: missing `return`, fixed 45s TorBox backoff (threshold 25), recursive RC refresh, skip rglob/iterdir for TorBox, settle sleep 5s
- [ ] Section 5 — Scrape session expiry: 15 min when TorBox active
- [ ] Section 6 — `find_existing_torrent` uses `bypass_cache=true`
- [ ] Section 7 — Build, retag, deploy
- [ ] Section 8 — Verification 1–10 pass
