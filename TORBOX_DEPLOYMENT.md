# TorBox Deployment Guide

This guide covers deploying Riven with TorBox as the debrid provider, using rclone to mount the TorBox native WebDAV at `/mnt/torbox`.

---

## Prerequisites

- A TorBox account on the **Standard plan or above** (plan code ≥ 2)
- `rclone` available on the host (or via Docker — already in `docker-compose.yml`)
- The host path `/opt/ftplexdata/mnt/torbox` created before first run

```bash
mkdir -p /opt/ftplexdata/mnt/torbox
mkdir -p /opt/ftplexdata/rclone/cache
```

---

## TorBox account limits (Standard plan)

| Limit | Value |
|---|---|
| Active download slots | 5 concurrent torrents |
| Add-torrent rate | 60 per hour per API token |
| Max torrent size | 200 GB |
| WebDAV listing refresh | ~15 minutes (built-in) |

> **Note:** Backfilling a large library will hit the 60/hour add limit. Riven's local guard fires before the 61st request so downloads queue cleanly rather than hard-failing. A full library bootstrap may take several hours.

---

## Step 1 — Enable WebDAV Flatten

In the TorBox web dashboard:

1. Go to **Settings → Integrations**
2. Enable **WebDAV Flatten: ON**

With Flatten ON, all your files appear at the root of the WebDAV mount (`/mnt/torbox/file.mkv`). This matches Riven's fast-path file lookup in the symlinker. Without it, rglob fallback is used, which is slower.

---

## Step 2 — Set a TorBox account password (SSO users)

The TorBox WebDAV uses your account **email and password** for authentication — **not** the API key. If you signed up via Google/Discord SSO and don't have a password set, go to your TorBox account settings and set one before continuing.

---

## Step 3 — Generate the rclone obscured password

On the host (or any machine with rclone installed):

```bash
rclone obscure YOUR_TORBOX_ACCOUNT_PASSWORD
```

Copy the output — this is `RCLONE_OBSCURED_PASSWORD` used in the config below.

> Do **not** commit the plaintext password anywhere. Only the obscured value goes in the config file.

---

## Step 4 — Create the rclone config

Create `/opt/ftplexdata/rclone-torbox.conf`:

```ini
[torbox]
type = webdav
url = https://webdav.torbox.app
vendor = other
user = YOUR_TORBOX_EMAIL
pass = RCLONE_OBSCURED_PASSWORD
```

Replace `YOUR_TORBOX_EMAIL` and `RCLONE_OBSCURED_PASSWORD` with your values.

---

## Step 5 — Set environment variables

Add to your `.env` file (same directory as `docker-compose.yml`):

```env
TORBOX_API_KEY=your_torbox_api_key_here
```

The API key is found in your TorBox dashboard under **Settings → API**.

---

## Step 6 — Build and start

```bash
# From c:\projects\riven (or the Linux equivalent)
docker compose build riven
docker compose up -d
```

Watch the Riven logs for confirmation:

```
INFO | TorBox: Your account expires in NNN days.
```

If you see `TorBox: premium membership required`, your API key is wrong or the account plan is 0.

---

## Expected behaviour

- **Cached torrents:** added, processed, and symlinked within ~1 minute
- **Uncached torrents:** added to TorBox for download; files appear in `/mnt/torbox` within ~15 minutes of TorBox completing the download (WebDAV refresh interval)
- **Symlinker backoff:** up to 20 minutes per retry cycle when TorBox is active (extended from the 4-minute RD default to accommodate the 15-min WebDAV lag)
- **Manual scrape sessions:** expire after 15 minutes (extended from 5 minutes for RD)

---

## Switching back to Real-Debrid

1. Update your `.env` / compose env:
   ```env
   RIVEN_DOWNLOADERS_TORBOX_ENABLED=false
   RIVEN_DOWNLOADERS_REAL_DEBRID_ENABLED=true
   RIVEN_DOWNLOADERS_REAL_DEBRID_API_KEY=your_rd_key
   RIVEN_SYMLINK_RCLONE_PATH=/mnt/zurg/__all__
   ```
2. Restore the `zurg` service in `docker-compose.yml` and swap the `rclone` command back to the zurg endpoint
3. Rebuild and restart: `docker compose up -d`

Old TorBox symlinks pointing to `/mnt/torbox/...` will be broken until Riven's `fix_broken_symlinks` flow cleans them up. Run a Plex library scan after the first successful RD symlink is created.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `AttributeError: 'TorBoxDownloader' object has no attribute 'get_torrent_status'` | Running old image | `docker compose build riven && docker compose up -d riven` |
| Files appear in TorBox but not in `/mnt/torbox` | WebDAV listing lag | Wait up to 15 min; check `docker logs rclone` for mount errors |
| `rclone: failed to mount` | FUSE not available or path not created | Confirm `/dev/fuse` exists and `/opt/ftplexdata/mnt/torbox` is created |
| `401` in rclone logs | Wrong WebDAV credentials | Re-check email and obscured password in `rclone-torbox.conf` |
| Rate limited after ~60 adds | TorBox 60/hr limit | Normal — Riven queues gracefully. Wait ~1 hour. |
