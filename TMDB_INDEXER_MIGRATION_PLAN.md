# TMDB Indexer Migration Plan

Replace Trakt with TMDB. **Trakt API access is dead — indexing has been broken for ~1
week. This is an outage fix.**

Status: **plan only — nothing implemented yet**
Branch: `fraser`

## Decisions (locked)

| # | Decision | Choice |
|---|---|---|
| 1 | Existing database | **Hard reset and rebuild from symlinks** (§2). Removes the ID migration, the adoption shim, and most of the risk in earlier drafts |
| 2 | Item ID scheme | **IMDb-keyed** — `movie_tt…`, `show_tt…`, `season_tt…_{n}`, `episode_tt…_{s}_{e}` (§4) |
| 3 | Air dates | **`air_date` + 1 day at 00:00 UTC**, tunable via `air_date_grace_hours` (§5.3) |
| 4 | Anime handling | **Minimal.** No anime in this library; keep the field correct, don't invest in accuracy (§5.2) |
| 5 | Trakt | **Deleted entirely** — indexer, API client, content service, OAuth, settings, log level (§7) |
| 6 | Indexer toggle | **Dropped.** A rollback path to a dead API is worthless |

---

## 1. Current state — what the outage has and hasn't broken

Verified against the code:

**Nothing is permanently damaged.** `failed_attempts` is incremented only by the *scraper*
(`services/scrapers/__init__.py:82`), never the indexer. When `TraktIndexer.run` fails it
adds to an in-memory `failed_ids` set and `return`s without yielding
(`indexers/trakt.py:78-83`), so `run_thread_with_db_item` returns `None`
(`db/db_functions.py:459-461`) and nothing is written to the DB. `failed_ids` is
in-process and clears on restart.

Items already in the DB sit at `States.Requested` (`item.py:230-231`). Content items that
failed to index never entered the DB at all — those recover on their own, since content
services re-emit their full list each poll and `add_item` (`event_manager.py:338`)
re-queues anything not already present.

---

## 2. The approach: hard reset, rebuild from symlinks

Given re-downloads are acceptable, there is a much better option than either the Alembic
re-key or the ID-adoption shim from earlier drafts — and it avoids re-downloading anyway.

`SymlinkLibrary.run()` (`services/libraries/symlink.py:69-86`) reconstructs the library
from the symlinks already on disk:

- `process_items` (`symlink.py:88`) and `process_shows` (`symlink.py:138`) extract
  `imdb_id` and title from filenames, and season/episode numbers from the directory tree.
- Every recovered item gets `symlinked=True` and `update_folder="updated"`
  (`symlink.py:176-181`), which `_determine_state` (`item.py:218-219`) maps directly to
  **`States.Completed`**.
- `_init_db_from_symlinks` (`program.py:527`) then runs each stub through the indexer to
  fill in metadata, and `copy_items` carries the file mapping onto the indexed tree.

So the sequence is: `pg_dump` → `--hard_reset_db` → restart → library rebuilds from disk,
metadata freshly indexed from TMDB, everything lands at `Completed`, **no re-downloads**.

This eliminates from the plan: the Alembic re-key migration, the ID-adoption shim, mixed
ID formats, `test_id_adoption.py`, and the two highest-severity rows of the old risk table.

**Verified live against TMDB (2026-07-31).** A stubbed `Completed` show shaped exactly as
`process_shows` builds it survives a full TMDB re-index: ids unchanged, `file`, `folder`,
`symlinked` and `update_folder` intact, every stubbed episode still `Completed`, seasons
that had no files correctly not `Completed`. Same for a movie. See §9.

### 2.1 What the reset actually costs

State that exists only in the database and not on disk is lost:

- **In-flight items** — anything `Requested`/`Indexed`/`Scraped`/`Downloaded` but not yet
  symlinked, including the week's backlog. Recovered automatically: content services
  re-emit their lists on next poll (§1). Anything manually requested needs re-requesting.
- **`blacklisted_streams`** — previously-rejected torrents can be selected again. Expect
  some churn on first scrape of anything not already complete.
- **`scraped_times` / `scraped_at`** — scrape backoff (`scrapers/__init__.py:149-168`)
  resets to zero.
- **`requested_by` / `requested_at` / `overseerr_id`** — cosmetic, plus Overseerr status
  linkage for existing items.
- Any symlink whose filename the regex can't parse (`symlink.py:142-146`) is skipped and
  its item is simply not recovered.

Related pre-existing defect, found while verifying the above: `copy_items` calls
`copy_attributes` on **episodes** and **movies** but never on the show itself
(`indexers/trakt.py:38-51`), so a show row loses `requested_by`, `requested_at`,
`requested_id` and `overseerr_id` on *every* re-index — not just at reset. Byte-identical
in `HEAD`, so Trakt has been doing this all along; it does not affect state or download
preservation, which live on episodes. Out of scope here, worth fixing separately.

### 2.2 One destructive side effect — read before running

`process_shows` **deletes episode files it cannot parse**:

```python
episode_numbers: list[int] = parse_title(episode).get("episodes", [])
if not episode_numbers:
    logger.log("NOT_FOUND", f"Can't extract episode number at path …")
    # Delete the episode since it can't be indexed
    os.remove(directory / show / season / episode)     # symlink.py:169
```

This is pre-existing upstream behaviour, not something this plan introduces, and it
removes the *symlink* rather than the source file. But the symlink scan is not read-only,
so: **take a filesystem-level listing of the library before the reset** so anything
removed can be identified and re-linked. Consider patching this to log-and-skip rather
than delete as part of this work — it is a one-line change and there is no good reason for
a library scan to delete data.

### 2.3 The residual risk: numbering differences

`copy_items` (`indexers/trakt.py:38-48`) matches seasons and episodes **by number**. Your
symlink tree was named according to Trakt's numbering. Where TMDB disagrees, those
episodes won't match, will land as `Indexed` rather than `Completed`, and will re-scrape
and re-download.

You've accepted re-downloads, so this is not a blocker. Two practical consequences worth
knowing:

- The main offender is anime (absolute vs. seasonal numbering) — **not applicable here**.
  Remaining cases are shows one provider splits and the other keeps whole, and folded
  miniseries. Expect a small number, not a large one.
- Re-downloaded episodes get symlinked under TMDB's numbering, so the old Trakt-numbered
  symlinks become orphaned. Worth a sweep for empty/duplicate season directories
  afterwards. `repair_symlinks` (`settings/models.py:86`) may help.

§5.4 measures this precisely before you commit.

---

## 3. What Trakt does here today

### 3.1 Metadata indexing — replaced

`services/indexers/trakt.py` → `TraktIndexer`, backed by `apis/trakt_api.py`.

| Call | Trakt endpoint | Purpose |
|---|---|---|
| `create_item_from_imdb_id` (`trakt_api.py:222`) | `/search/imdb/{id}?extended=full` | IMDb → Movie/Show |
| `get_show` (`trakt_api.py:191`) | `/shows/{imdb}/seasons?extended=episodes,full` | **whole tree in one call** |
| `get_show_aliases` (`trakt_api.py:199`) | `/{type}/{imdb}/aliases` | alternative titles for scrapers |
| `map_item_from_data` (`trakt_api.py:303`) | — | response → `Movie`/`Show`/`Season`/`Episode` |

Entry points: `state_transition.py:26`, `program.py:523` (`_enhance_item`),
`items.py:641`, `scrape.py:219,282,510`.

### 3.2 External-ID resolution — replaced

`get_imdbid_from_tmdb` (`trakt_api.py:256`) — `overseerr_api.py:116`, `listrr_api.py:63`,
`webhooks.py:58`; `get_imdbid_from_tvdb` (`trakt_api.py:270`) — `webhooks.py:60`.

Also currently broken, partially masked because `get_imdbid_from_overseerr`
(`webhooks.py:49-60`) tries `req.media.imdbId` first. TMDB does this natively via
`/find/{id}?external_source=…`; Overseerr and Listrr already speak TMDB IDs, so most of
these calls disappear rather than being translated.

### 3.3 Content lists — deleted (§7)

`services/content/trakt.py` → `TraktContent`, plus OAuth at `trakt_api.py:354,371` wired
to `routers/secure/default.py:93-108`. Unused.

### 3.4 Dead code already in the repo

- `src/program/services/indexers/tmdb.py` — 454-line TMDB client **nothing imports**.
  Wrong layer, and its Pydantic return types are fiction (the shared `get()` helper
  returns `SimpleNamespace`). Models are wrong too: `TmdbItem.title` is required but TV
  results use `name`; `TmdbSeasonItem.poster_path` is required but often null.
  **Reference material, not a starting point.** It hardcodes a live third-party TMDB read
  token at line 10 — treat as leaked, do not carry it over.
- `src/program/apis/tvmaze_api.py` — unimported; imports `TraktModel` for no reason.

---

## 4. Item IDs

`movie_tt0111161`, `show_tt0903747`, `season_tt0903747_1`, `episode_tt0903747_1_2`.

With a fresh database there's no compatibility constraint, so pick what fits the codebase.
IMDb keying wins on two counts:

- **It matches the symlink rebuild.** `SymlinkLibrary` produces stubs carrying only
  `imdb_id` and `title` (`symlink.py:107,147`), so an IMDb-keyed item gets its final ID at
  construction, before TMDB is ever called. TMDB keying would leave every stub with a
  `None` id until indexed.
- The pipeline is already IMDb-centric: `get_top_imdb_id` (`item.py:388`),
  `_get_stremio_identifier` (`scrapers/shared.py:208-217`), `jackett.py:165`,
  `prowlarr.py:253,263`, `scrapers/__init__.py:29,99-100`.

IDs are opaque throughout — `get_item_by_id` (`db_functions.py:24`) is pure equality, and
nothing parses the format. The one place that inspects an ID is `scrape.py:211,272`, which
checks `id.startswith("tt")` to tell an IMDb ID from a DB ID; `movie_tt…` does not start
with `tt`, so that check still works. Verified.

Fallback where TMDB has no IMDb ID: `{type}_tmdb{id}`, logged at `warning`. Such items are
already on the `keyword_services` scraper path (`scrapers/__init__.py:100`), so this is not
a regression. The type prefix keeps the namespaces from colliding.

Implementation note: `__generate_composite_key` (`item.py:137-144`) receives only the item
dict, so a season has no access to its parent's IMDb ID at construction. Assign child IDs
in `add_season` (`item.py:618`) / `add_episode` (`item.py:756`), which already set `parent`.

Drop the now-unused `trakt_id` column (`item.py:25`) and the `to_dict()` bugs alongside it:
`item.py:265` returns `tmdb_id` under the `trakt_id` key; `item.py:268` returns `tvdb_id`
under the `tmdb_id` key.

---

## 5. TMDB design decisions

### 5.1 One request per show

Trakt returned the whole tree in one call (`trakt_api.py:195`); the naive TMDB port is
`1 + N`. Use `append_to_response` (max **20** keys):

```
GET /3/tv/{id}?append_to_response=external_ids,alternative_titles,season/1,season/2,…
```

17 season slots + 2 metadata keys (no `keywords` — see §5.2). Shows with more seasons get
batched follow-ups. Movies are one request:
`GET /3/movie/{id}?append_to_response=alternative_titles,external_ids`.

This matters more than usual here: the symlink rebuild indexes the **entire library in one
pass**, so per-show request count is the dominant cost of the initial import.

### 5.2 `is_anime` — keep it correct, keep it cheap

Trakt's version (`trakt_api.py:460-478`) tested genres plus country. TMDB has no `anime`
genre. Since there's no anime in this library and `separate_anime_dirs` defaults to `False`
(`settings/models.py:85`), don't spend an `append_to_response` slot on the `keywords` call.

Heuristic only: genre ID `16` (Animation) **and** (`origin_country` ∩ `{JP,KR,CN,HK,TW}`
or `original_language` ∈ `{ja,ko,zh}`).

Keep computing once on the show and propagating down (`trakt.py:35-48`,
`item.py:686,795`) — the field still feeds ranking, so it must be present and sane, just
not perfect. *(If anime is ever added, upgrade to TMDB keyword `210024`.)*

### 5.3 Two field-mapping traps

**Case.** Trakt returned country and genre values **lowercase**; TMDB returns them
**uppercase**. `scrapers/shared.py:86` filters aliases against
`ranking_settings.languages.exclude`, which holds lowercase codes. Miss this and
alias-based language exclusion silently stops working with no error anywhere. Lowercase
`iso_3166_1`, `genres[].name` and `origin_country` on the way in.

Alias payload shapes differ: movie → `{"titles": [...]}`, TV → `{"results": [...]}`. Fold
`original_title`/`original_name` in as an alias — Trakt usually included it and RTN
matching benefits.

**Air dates — `air_date` + 1 day at 00:00 UTC.** Trakt `first_aired` was a full UTC
timestamp; TMDB `air_date` is a bare `YYYY-MM-DD`. `is_released` (`item.py:205-208`)
compares `aired_at <= datetime.now()`, so naive midnight parsing makes items scrapeable up
to ~24h early → wasted scrapes. Trakt's timestamps for US primetime were typically
00:00–04:00 UTC the following day, so +1 day closely approximates prior behaviour and errs
safe. Expose `air_date_grace_hours` (default 24).

### 5.4 Pre-reset diff — measure the numbering delta

Before the hard reset, run a read-only pass that walks the **current** database and
compares each show's season/episode structure against TMDB, reporting divergences. Needs
`TMDBAPI` (§6A) but not `TMDBIndexer`, so it slots in between the two.

Output: per-show numbering deltas plus a total count of episodes that would land as
`Indexed` rather than `Completed` after the rebuild — i.e. exactly how much re-downloading
to expect. Cheap to build, turns §2.3 from a guess into a number.

If the count is small, proceed. If it's surprisingly large, that's a signal something is
wrong with the mapping rather than with TMDB.

### 5.5 IMDb ID still mandatory downstream

Every scraper keys off IMDb (§4). `TMDBIndexer` must resolve and store it: movie details
include `imdb_id` directly; TV needs `external_ids` appended (which also yields `tvdb_id`
— note `tvdb_id` does not exist for movies). Where TMDB returns none, log `warning` and
index anyway — do **not** hard-fail as `TraktIndexer` does at `trakt.py:61-63`.

Note the symlink rebuild is entirely IMDb-driven (`symlink.py:142`), so any library item
TMDB can't match by IMDb will fail to import. Log these as a list at the end of the rebuild.

### 5.6 Auth and rate limiting

- `Authorization: Bearer <API Read Access Token>` (v4 token works on v3 endpoints).
  Setting `indexer.tmdb.api_key`, env `RIVEN_INDEXER_TMDB_API_KEY`, legacy fallback
  `TMDB_API_KEY`. **Ship no hardcoded token** (§3.4).
- `get_rate_limit_params(max_calls=40, period=10)`.
- Caching as TVMaze does it (`tvmaze_api.py:34-36`): `get_cache_params("tmdb", 86400)`,
  bypass via `SKIP_TMDB_CACHE=true`. Matters for the full-library import — a retry after a
  partial failure should hit cache, not TMDB.

### 5.7 Field mapping reference

| MediaItem field | Trakt | TMDB |
|---|---|---|
| `title` | `title` | movie `title` / tv `name` |
| `year` | `year` | `release_date[:4]` / `first_air_date[:4]` |
| `aired_at` | `released` / `first_aired` | `release_date` / `first_air_date` / `air_date` (§5.3) |
| `imdb_id` | `ids.imdb` | movie `imdb_id` / tv `external_ids.imdb_id` |
| `tvdb_id` | `ids.tvdb` | tv `external_ids.tvdb_id`; **null for movies** |
| `tmdb_id` | `ids.tmdb` | `id` |
| `genres` | lowercase slugs | `genres[].name` — **lowercase** |
| `network` | `network` | `networks[0].name` (tv only) |
| `country` | lowercase | `origin_country[0]` / `production_countries[0].iso_3166_1` — **lowercase** |
| `language` | `language` | `original_language` |
| `aliases` | `/aliases` | `alternative_titles` (§5.3) |
| `is_anime` | genre+country | genre 16 + origin (§5.2) |
| `number` | `number` | `season_number` / `episode_number` |

---

## 6. Plan of work

### Section A — `src/program/apis/tmdb_api.py`

Modelled on `trakt_api.py` (`TMDBAPIError`, `TMDBRequestHandler`, `TMDBAPI`):

- `validate()` → `GET /3/configuration`
- `get_movie(tmdb_id)`, `get_show(tmdb_id, season_numbers)` (§5.1), `get_season(tmdb_id, n)`
- `find_by_external_id(external_id, source)` → `/find/{id}?external_source=…`
- `get_imdbid_from_tmdb(tmdb_id, type)` / `get_imdbid_from_tvdb(tvdb_id, type)` —
  signature-compatible with the Trakt methods so Section C is a drop-in swap
- `search(query, type, year)`
- `map_item_from_data(data, item_type, show_context)` — the §5.7 mapping
- `_get_aliases`, `_is_anime`, `_parse_air_date`

Register in `apis/__init__.py` via `__setup_tmdb()`, unconditionally.

### Section B — `src/program/services/indexers/tmdb.py`

Replace the dead file's contents with `TMDBIndexer`.

- `copy_attributes` / `copy_items` move over **unchanged** — provider-agnostic, and what
  carries the symlink file mapping onto the indexed tree during the rebuild (§2).
- `run()`: resolve entry point (`tmdb_id`, else `imdb_id` via `/find`, else bail).
- `_add_seasons_to_show()` consumes appended season payloads instead of looping; keep
  skipping season 0 (`trakt.py:115-116`).
- `should_submit()` copies over verbatim.

Swap call sites — direct replacement, no toggle: `state_transition.py:5,26`,
`program.py:23,82,523`, `items.py:20,641` (also the "no data returned from Trakt" message
at line 654), `scrape.py:19,219,282,510`. Note `scrape.py:510` constructs `TraktIndexer()`
directly rather than pulling from `services` — fix that here.

### Section C — External-ID resolution swap

Point at `TMDBAPI`: `apis/overseerr_api.py:8,40,116`, `apis/listrr_api.py:5,37,63`,
`routers/secure/webhooks.py:9,52,58,60`.

### Section D — Settings, env, schema

```python
class TMDBIndexerModel(Observable):
    api_key: str = ""
    language: str = "en-US"
    include_adult: bool = False
    air_date_grace_hours: int = 24
    request_timeout: int = 30

class IndexerModel(Observable):
    update_interval: int = 86400        # raised from 60 * 60
    tmdb: TMDBIndexerModel = TMDBIndexerModel()
```

`.env.example`: add `RIVEN_INDEXER_TMDB_API_KEY`, `SKIP_TMDB_CACHE`.

Alembic revision dropping `MediaItem.trakt_id` (§4) — a formality given the reset, but
keeps migration history coherent for a fresh install.

The settings models use Pydantic's default `extra="ignore"` — `MigratableBaseModel`
(`settings/migratable.py:4`) sets no `model_config` and there is no `extra="forbid"`
anywhere — so removed blocks leave stale keys in `settings.json` harmlessly ignored, and
`settings_manager.save()` drops them on next write. **Verified.** Confirm
`test_settings_migration.py` still passes.

### Section E — Tests

- `src/tests/test_data/` — real TMDB fixtures following the existing `torbox_*.json`
  convention: movie details, TV with appended seasons, `/find` by IMDb, alternative titles,
  a >17-season show, a show with null `imdb_id`.
- `test_tmdb_indexer.py` — mapping, §5.3 lowercasing, §5.3 air dates, ID generation for all
  four types, append-budget fallback.
- **`test_symlink_rebuild.py`** — the one that matters now. Feed `SymlinkLibrary`-shaped
  stubs through `TMDBIndexer` and assert the file mapping survives onto the indexed tree
  and items land at `Completed`. Guards §2 directly.
  `test_symlink_library.py` and `test_symlink_creation.py` already exist — extend rather
  than duplicate.
- Update `test_states_processing.py:127-208`.

### Section F — Documentation

- `README.md:27` — services table.
- `CHANGELOG.md` — Trakt removed; TMDB API key now required; database reset required, with
  the §8 runbook.
- `TMDB_DEPLOYMENT.md` — API key setup and the reset runbook, matching
  `TORBOX_DEPLOYMENT.md`.

---

## 7. Trakt removal

No reason to defer — the code is non-functional. Do it with Section B.

Delete: `apis/trakt_api.py`, `services/indexers/trakt.py`, `services/content/trakt.py`,
`apis/tvmaze_api.py`.

Edit: `apis/__init__.py:9,20-21` · `services/content/__init__.py:8,10` ·
`services/indexers/__init__.py` · `types.py:11,35` ·
`settings/models.py:163-186,193` (`TraktOauthModel`, `TraktModel`, `ContentModel.trakt`) ·
`routers/secure/default.py:10,93-108` (both OAuth endpoints) · `utils/logging.py:54`
(`TRAKT` log level) · `.env.example:24-25,104-106,355-356` · `README.md:27` ·
`db_functions.py:411` (`calendar[...]["trakt_id"]`).

---

## 8. Cutover runbook

1. `pg_dump` of `riven-db`. **Also take a full recursive listing of the symlink library**
   (§2.2 — the scan can delete unparseable episode symlinks).
2. Record a baseline count of items by `last_state`.
3. Deploy Sections A–D + §7 with the TMDB API key configured. Do **not** reset yet.
4. Run the §5.4 diff read-only against the current DB. Review the numbering delta.
5. Verify §9.1-9.5 against the deployed build.
6. Stop Riven. `--hard_reset_db`.
7. Restart. `_init_db_from_symlinks` rebuilds the library via TMDB. Watch for the
   §5.5 unmatched-IMDb list and the §2.2 deletion log.
8. Compare final `last_state` counts against the step-2 baseline. Items short of
   `Completed` are the §2.3 numbering casualties — let them re-scrape.
9. Re-request anything that was in flight before the reset (§2.1).
10. Sweep for orphaned season directories left by re-numbered shows (§2.3).

---

## 9. Verification

1. `TMDBAPI.validate()` against a real key.
2. Index a movie (`tt0111161`) and a show (`tt0903747`) — check §5.7 fields, confirm 1
   HTTP request each.
3. Long-runner (`tt0096697`, 30+ seasons) — append-budget fallback fires, tree complete.
4. Title with no IMDb ID — indexes with a warning, does not crash.
5. Symlink-stub round trip: stub → `TMDBIndexer` → `Completed` with file mapping intact.
6. Full test suite.
7. Post-reset: §8.8 state-count comparison.
8. Full pipeline on a fresh request: Requested → … → Completed.

---

## 10. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Symlink scan deletes unparseable episode symlinks | **Medium** | §2.2 — filesystem listing first; consider patching `symlink.py:169` to log-and-skip |
| Numbering differences → re-downloads + orphaned symlinks | Medium | Accepted; §5.4 quantifies it first; §8.10 sweep |
| In-flight items lost in the reset | Medium | §2.1 — content services re-emit; re-request manual items |
| Blacklisted streams lost → bad torrents return | Low-Medium | Accepted; they re-blacklist on failure |
| Alias case mismatch silently degrades scraping | Medium | §5.3; test with a non-`us` country code |
| Full-library import is slow or rate-limited | Medium | §5.1 `append_to_response` + §5.6 cache so retries are cheap |
| Early scraping from date-only `air_date` | Medium | §5.3 +1 day default, tunable |
| Library items TMDB can't match by IMDb | Low | §5.5 — log the list at end of rebuild |

---

## 11. Checklist

- [x] Existing DB — hard reset, rebuild from symlinks
- [x] ID scheme — IMDb-keyed
- [x] Air-date handling — `air_date` + 1 day, tunable
- [x] Anime — minimal heuristic, no keyword call
- [x] Trakt — delete entirely
- [ ] A — `apis/tmdb_api.py` + DI registration
- [ ] B — `services/indexers/tmdb.py`, call sites swapped
- [ ] C — external-ID resolution → TMDB
- [ ] D — settings, env, drop `trakt_id`
- [ ] §7 — Trakt removed
- [ ] E — fixtures, `test_symlink_rebuild.py`
- [ ] §5.4 diff built and reviewed
- [ ] §8 cutover runbook executed
- [ ] F — docs and changelog
