# TMDB Indexer Migration Plan

Replace Trakt with TMDB as the metadata/indexing provider for Riven.

Status: **plan only — nothing implemented yet**
Branch: `fraser`
Scope owner: indexer + ID resolution. Trakt *content lists* are a separate decision (see §8).

---

## 1. Why this work is needed

- Trakt's API now requires a paid plan for the access level Riven uses.
- Trakt rate limits (the client is configured for 1000 calls / 300s in
  `src/program/apis/trakt_api.py:65`) throttle library imports and bulk re-index.
- The hardcoded fallback client ID in `src/program/apis/trakt_api.py:50-53` is a shared
  community key — it is a single point of failure and is already rate-limit contended.
- TMDB is free for personal use, has no daily cap, and a far higher throughput ceiling
  (~50 req/s soft limit).

---

## 2. What Trakt actually does in this repo today

Four distinct jobs, only one of which is "the indexer". They must be separated before
anything is rewritten, because they have different replacements.

### 2.1 Metadata indexing (the target of this migration)

`src/program/services/indexers/trakt.py` → `TraktIndexer`, backed by
`src/program/apis/trakt_api.py`.

| Call | Trakt endpoint | Purpose |
|---|---|---|
| `create_item_from_imdb_id` (`trakt_api.py:222`) | `/search/imdb/{id}?extended=full` | IMDb → Movie/Show with full metadata |
| `get_show` (`trakt_api.py:191`) | `/shows/{imdb}/seasons?extended=episodes,full` | **whole season+episode tree in one call** |
| `get_show_aliases` (`trakt_api.py:199`) | `/{type}/{imdb}/aliases` | alternative titles for scraper matching |
| `map_item_from_data` (`trakt_api.py:303`) | — | maps response → `Movie`/`Show`/`Season`/`Episode` |

Fields produced: `title`, `year`, `aired_at`, `trakt_id`, `imdb_id`, `tvdb_id`, `tmdb_id`,
`genres`, `network`, `country`, `language`, `aliases`, `is_anime`, `number`.

Entry points into the indexer:
- `src/program/state_transition.py:26` — every `Requested` item
- `src/program/program.py:523` (`_enhance_item`) — symlink→DB bootstrap
- `src/routers/secure/items.py:641` — manual reindex endpoint
- `src/routers/secure/scrape.py:219,282,510` — manual scrape by IMDb ID

### 2.2 External-ID resolution (must also move — it is on the same rate limit)

- `get_imdbid_from_tmdb` (`trakt_api.py:256`) — used by
  `src/program/apis/overseerr_api.py:116`, `src/program/apis/listrr_api.py:63`,
  `src/routers/secure/webhooks.py:58`
- `get_imdbid_from_tvdb` (`trakt_api.py:270`) — used by `src/routers/secure/webhooks.py:60`

TMDB does this natively and better (`/find/{id}?external_source=...`, plus
`/movie/{id}` and `/tv/{id}/external_ids` return `imdb_id` directly).

### 2.3 Content lists (NOT in this migration — see §8)

`src/program/services/content/trakt.py` → `TraktContent` (watchlist, collection, user
lists, trending, popular, most-watched). Also `perform_oauth_flow` /
`handle_oauth_callback` (`trakt_api.py:354,371`) wired to
`src/routers/secure/default.py:93-108`.

### 2.4 Database identity (the hard part)

`MediaItem.id` is `f"{type}_{trakt_id}"` — `src/program/media/item.py:137-144`.

**Every row in the database — movies, shows, seasons and episodes — is primary-keyed on a
Trakt ID.** `Season.parent_id`, `Episode.parent_id`, `StreamRelation`,
`StreamBlacklistRelation` and `Subtitle.parent_id` all FK onto it. This, not the API
client, is the risk in this migration.

### 2.5 Already present but dead

- `src/program/services/indexers/tmdb.py` — a 454-line TMDB client that **nothing
  imports**. Wrong layer (an API client living in `services/indexers/`), hardcoded
  someone else's read token at line 10, and the Pydantic return types are fiction — the
  shared `get()` helper returns `SimpleNamespace`, not these models. Several models are
  also wrong (`TmdbItem.title` is required but TV results use `name`;
  `TmdbSeasonItem.poster_path` is required but is frequently null).
  **Treat as reference material, not a starting point.** Delete it in Section G.
- `src/program/apis/tvmaze_api.py` — also unimported. Relevant later (§5, air times).

---

## 3. End state

- `src/program/apis/tmdb_api.py` — `TMDBAPI`, same shape as the other API clients
  (`BaseRequestHandler`, `create_service_session`, registered in `di`).
- `src/program/services/indexers/tmdb.py` — `TMDBIndexer`, same public contract as
  `TraktIndexer`: `key`, `initialized`, `run(item, log_msg=True)` generator,
  `should_submit(item)` staticmethod.
- `MediaItem.id` derived from TMDB ID (§4.1), with an Alembic data migration that
  rewrites existing rows **offline, with no network calls**.
- Trakt code paths for indexing and ID resolution deleted; `TraktContent` left working
  but explicitly optional.
- Settings: `content.trakt` untouched; new `indexer.tmdb` block with API key + knobs.

---

## 4. Design decisions to lock before writing code

### 4.1 Item ID scheme — decide this first

Options:

**(A) `{type}_{tmdb_id}`** — closest to today. Needs TMDB season/episode IDs, so
season/episode rows can only be re-keyed by re-querying TMDB. Existing rows have no TMDB
season/episode ID stored, so the Alembic migration would need thousands of network calls.

**(B) Derive children from the parent** — `movie_{tmdb}`, `show_{tmdb}`,
`season_{show_tmdb}_{n}`, `episode_{show_tmdb}_{s}_{e}`. Deterministic, no per-season API
call needed to compute a key.

**(C) Key on IMDb instead** — `movie_tt…`, `show_tt…`, `season_tt…_{n}`,
`episode_tt…_{s}_{e}`.

**Recommendation: (B).** Rationale:
- The `tmdb_id` column is *already populated* on existing rows — Trakt returned
  `ids.tmdb` and `map_item_from_data` stored it (`trakt_api.py:324`). So the Alembic
  migration is a pure SQL re-key using data already in the table. No network.
- Season/episode keys are computed from the parent show + numbers, which the DB already
  has, so children re-key offline too.
- It keeps the provider-native ID as the identity, matching the direction of travel.

Against (C): the scraping layer is IMDb-centric (`get_top_imdb_id`,
`_get_stremio_identifier` in `src/program/services/scrapers/shared.py:208-217`), so IMDb
keying is tempting — but TMDB does not guarantee an IMDb ID on every title, and we would
be keying on a foreign provider's ID while indexing from TMDB. Note that IMDb ID is still
*required downstream* regardless — see §4.4.

Fallback for rows where the migration cannot resolve a TMDB ID: quarantine them (§6.C),
do not silently drop.

### 4.2 Keep the `trakt_id` column

Leave `MediaItem.trakt_id` (`item.py:25`) in place, nullable, no longer written. Cheap
insurance and lets the migration be rolled back. Also fix the three pre-existing bugs in
`to_dict()` while in there: `item.py:265` returns `tmdb_id` under the `trakt_id` key, and
`item.py:268` returns `tvdb_id` under the `tmdb_id` key.

### 4.3 One request per show, not one per season

This is the main performance risk. Trakt returned the entire season+episode tree in a
single call (`trakt_api.py:195`). The naive TMDB port is `1 + N` requests per show.

Use `append_to_response` — TMDB allows up to **20** appended keys:

```
GET /3/tv/{id}?append_to_response=external_ids,alternative_titles,keywords,season/1,season/2,…
```

Budget: 16 season slots + 4 metadata keys = 20. Shows with >16 seasons fall back to
batched follow-up requests. Result: 1 request for the overwhelming majority of shows,
2–3 for long-runners. Document the fallback path in a `logger.debug`.

Movies: `GET /3/movie/{id}?append_to_response=alternative_titles,external_ids,keywords` —
always 1 request.

### 4.4 IMDb ID is still mandatory

Every scraper keys off IMDb (`scrapers/__init__.py:29,99-100`, `shared.py:208-217`,
`jackett.py:165`, `prowlarr.py:253,263`). The indexer must therefore still resolve and
store `imdb_id` for movies and shows.

- Movie: `/movie/{id}` details include `imdb_id` directly.
- TV: `/tv/{id}/external_ids` gives both `imdb_id` and `tvdb_id` — append it (§4.3).
- If TMDB returns no IMDb ID: log at `warning`, still index the item, and let the
  existing `keyword_services` scraper path handle it (`scrapers/__init__.py:100` already
  branches on missing IMDb). Do **not** hard-fail as `TraktIndexer` does at
  `indexers/trakt.py:61-63`.

### 4.5 `is_anime` needs reimplementing

Trakt's version (`trakt_api.py:460-478`) tests for genres `animation|donghua|anime` plus
country in `jp|kr|cn|hk`. TMDB has no `anime` genre.

Replacement, in priority order:
1. **TMDB keyword `210024` ("anime")** via `append_to_response=keywords`. This is the
   accurate signal and is strictly better than what Trakt gave us.
2. Fallback heuristic: genre ID `16` (Animation) **and** (`origin_country` ∩
   `{JP, KR, CN, HK, TW}` or `original_language` ∈ `{ja, ko, zh}`).

Keep the existing behaviour of computing it once on the show and propagating down to
seasons/episodes (`indexers/trakt.py:35-48` `copy_items`, and `item.py:686,795`).

### 4.6 Aliases — watch the case

`get_show_aliases` returned `{country_code: [titles]}` with **lowercase** Trakt country
codes. `src/program/services/scrapers/shared.py:86` filters that dict against
`ranking_settings.languages.exclude`, which holds lowercase codes.

TMDB `alternative_titles` returns `iso_3166_1` **uppercase**. The mapper must
`.lower()` the key or alias-based language exclusion silently stops working — a silent
scraper-quality regression with no error anywhere.

Shape: movie → `{"titles": [{"iso_3166_1", "title", "type"}]}`; TV →
`{"results": [...]}`. Note the different top-level key. Also fold in
`original_title` / `original_name` as an alias under the origin country — Trakt's alias
list usually included it and RTN matching benefits.

### 4.7 `aired_at` loses time-of-day — behaviour change, needs a decision

Trakt `first_aired` was a full UTC timestamp. TMDB `air_date` is a bare `YYYY-MM-DD`.

`is_released` (`item.py:205-208`) compares `aired_at <= datetime.now()`, and
`_determine_state` (`item.py:228-230`) uses it to move items out of `Unreleased`. Parsing
a bare date to local midnight makes items eligible for scraping up to ~24h before the
episode actually exists → wasted scrape cycles and `Failed` items.

Options:
- **(i) `air_date` + 1 day at 00:00 UTC** *(recommended default)*. Trakt's timestamps for
  US primetime were typically 00:00–04:00 UTC the following day, so this approximates the
  old behaviour closely and errs on the safe side.
- (ii) Bare midnight + a `indexer.tmdb.air_date_grace_hours` setting (default 24).
- (iii) Wire up the already-present-but-unused `src/program/apis/tvmaze_api.py` for exact
  air times on TV. Most accurate, most work, extra dependency. Defer.

Go with (i), and expose (ii) as the knob so it can be tuned without a code change.

### 4.8 Auth and rate limiting

- Auth via `Authorization: Bearer <API Read Access Token>` (the v4 token works against v3
  endpoints). Settings key `indexer.tmdb.api_key`, env
  `RIVEN_INDEXER_TMDB_API_KEY`, with legacy env fallback `TMDB_API_KEY`.
- **Do not ship a hardcoded token.** `services/indexers/tmdb.py:10` currently embeds a
  live third-party read token; that file is being deleted, and the token must not be
  carried over. Treat it as leaked and do not reuse it anywhere.
- `get_rate_limit_params(max_calls=40, period=10)` — comfortably under TMDB's limit.
- Enable response caching like TVMaze does (`tvmaze_api.py:34-36`):
  `get_cache_params("tmdb", 86400)`, bypassable via `SKIP_TMDB_CACHE=true`. Show metadata
  is near-static; this is where the real speedup over Trakt comes from.

---

## 5. Field mapping reference

| MediaItem field | Trakt source | TMDB source |
|---|---|---|
| `title` | `title` | movie `title` / tv `name` |
| `year` | `year` | `release_date[:4]` / `first_air_date[:4]` |
| `aired_at` | `released` / `first_aired` | `release_date` / `first_air_date` / season `air_date` / episode `air_date` (§4.7) |
| `imdb_id` | `ids.imdb` | movie `imdb_id` / tv `external_ids.imdb_id` |
| `tvdb_id` | `ids.tvdb` | tv `external_ids.tvdb_id`; **null for movies** |
| `tmdb_id` | `ids.tmdb` | `id` |
| `trakt_id` | `ids.trakt` | — (stop writing, §4.2) |
| `genres` | `genres` (lowercase slugs) | `genres[].name` — **lowercase these** to preserve behaviour |
| `network` | `network` | `networks[0].name` (tv only) |
| `country` | `country` (lowercase) | `origin_country[0]` / `production_countries[0].iso_3166_1` — **lowercase** |
| `language` | `language` | `original_language` |
| `aliases` | `/aliases` | `alternative_titles` (§4.6) |
| `is_anime` | genre+country | keyword 210024, else genre 16 + origin (§4.5) |
| `number` | `number` | `season_number` / `episode_number` |

Two recurring traps: TMDB returns country/genre values **capitalised** where Trakt
returned them lowercase, and `tvdb_id` simply does not exist for movies.

Season 0 (specials) is skipped today at `indexers/trakt.py:115-116` — keep skipping it.

---

## 6. Plan of work

### Section A — `src/program/apis/tmdb_api.py`

New file, modelled on `trakt_api.py` structure (`TMDBAPIError`, `TMDBRequestHandler`,
`TMDBAPI`). Methods:

- `validate()` → `GET /3/configuration`
- `get_movie(tmdb_id)` → details + `alternative_titles,external_ids,keywords`
- `get_show(tmdb_id, season_numbers)` → details + appended seasons (§4.3)
- `get_season(tmdb_id, n)` → fallback for shows exceeding the append budget
- `find_by_external_id(external_id, source)` → `/find/{id}?external_source={imdb_id|tvdb_id}`
- `get_imdbid_from_tmdb(tmdb_id, type)` — signature-compatible with the Trakt method
- `get_imdbid_from_tvdb(tvdb_id, type)` — same
- `search(query, type, year)` — for the keyword path and future UI search
- `map_item_from_data(data, item_type, show_context)` — the §5 mapping
- `_get_aliases`, `_is_anime`, `_parse_air_date` helpers

Register in `src/program/apis/__init__.py` with a `__setup_tmdb()` following the existing
pattern. Unlike the other services, it must initialise **unconditionally** (like Trakt
does at `__init__.py:20-21`) — the indexer is not optional.

### Section B — `src/program/services/indexers/tmdb.py`

Delete the existing dead file's contents, replace with `TMDBIndexer`.

Port from `indexers/trakt.py`, keeping the contract identical:
- `copy_attributes` / `copy_items` (`trakt.py:26-54`) move over **unchanged** — they are
  provider-agnostic and preserve user state across a reindex. Do not "improve" them here.
- `run()` resolves the entry point: item has `tmdb_id` → use directly; else `imdb_id` →
  `/find`; else bail.
- `_add_seasons_to_show()` consumes the appended season payloads rather than looping.
- `should_submit()` copies over verbatim.

Update `src/program/services/indexers/__init__.py` to export `TMDBIndexer`.

### Section C — Database migration

New Alembic revision, `down_revision = '834cba7d26b4'`.

Order matters — FKs must be updated before the parents they point at, or inside a single
`ON UPDATE CASCADE`-free manual sweep:

1. Add a temp `new_id` column to `MediaItem`.
2. Populate for movies/shows: `{type}_{tmdb_id}` where `tmdb_id IS NOT NULL`.
3. Populate seasons: `season_{parent.tmdb_id}_{number}`; episodes:
   `episode_{grandparent.tmdb_id}_{parent.number}_{number}`.
4. Rewrite `Season.parent_id`, `Episode.parent_id`, `StreamRelation.parent_id`,
   `StreamBlacklistRelation.parent_id`, `Subtitle.parent_id`.
5. Swap `new_id` → `id` across `MediaItem`, `Movie`, `Show`, `Season`, `Episode`.
6. Rows with no resolvable `tmdb_id`: **do not delete.** Set `last_state = 'Failed'` and
   log the count and IMDb IDs so they can be re-requested manually.

Write a `downgrade()` that reverses using the retained `trakt_id` column (§4.2).

**Non-negotiable: this migration must be exercised against a copy of the real
`riven-db` before it runs anywhere else.** Take a `pg_dump` first.

Escape hatch to document for users who would rather not migrate: drop the DB and
re-bootstrap from symlinks via `_init_db_from_symlinks` (`program.py:527`) — this is a
supported path and for many libraries is faster and cleaner than the data migration.

### Section D — Call sites

Mechanical `TraktIndexer` → `TMDBIndexer` swap:
- `src/program/state_transition.py:5,26`
- `src/program/program.py:23,82,523`
- `src/routers/secure/items.py:20,641` — also update the "no data returned from Trakt"
  message at line 654
- `src/routers/secure/scrape.py:19,219,282,510` — note line 510 constructs
  `TraktIndexer()` directly rather than pulling from `services`; keep that shape but
  point at `TMDBIndexer`
- `src/tests/test_states_processing.py:127-208`

ID resolution swap (§2.2), pointing at `TMDBAPI`:
- `src/program/apis/overseerr_api.py:8,40,116`
- `src/program/apis/listrr_api.py:5,37,63`
- `src/routers/secure/webhooks.py:9,52,58,60`

Overseerr and Listrr both speak TMDB IDs natively, so these become *cheaper* — worth
noting in the changelog.

### Section E — Settings and env

`src/program/settings/models.py`, extend `IndexerModel` (line 324):

```python
class TMDBIndexerModel(Observable):
    api_key: str = ""
    language: str = "en-US"
    include_adult: bool = False
    air_date_grace_hours: int = 24
    request_timeout: int = 30

class IndexerModel(Observable):
    update_interval: int = 60 * 60
    tmdb: TMDBIndexerModel = TMDBIndexerModel()
```

Add to `.env.example` near the existing indexer block (line 24):
`RIVEN_INDEXER_TMDB_API_KEY`, `SKIP_TMDB_CACHE`. Leave the `TRAKT_API_CLIENT_ID` entry
but comment it as content-lists-only.

Check `src/tests/test_settings_migration.py` still passes — settings changes have bitten
this repo before.

### Section F — Tests

- `src/tests/test_data/` — capture real TMDB fixtures, following the
  `torbox_*.json` convention already in that directory: movie details, TV details with
  appended seasons, `/find` by IMDb, alternative titles, a >16-season show, an anime show,
  a show with a null `imdb_id`.
- New `src/tests/test_tmdb_indexer.py`: mapping correctness, the §4.6 case-lowering, §4.5
  anime detection, §4.7 air-date handling, ID generation for all four types, and the
  append-budget fallback.
- New `src/tests/test_tmdb_migration.py`: run the Alembic revision against a seeded
  SQLite/PG fixture, assert FK integrity and that unresolvable rows are quarantined, not
  dropped.
- Update `src/tests/test_states_processing.py`.

### Section G — Trakt teardown

Delete, once Section F is green:
- `TraktIndexer` class and `src/program/services/indexers/trakt.py`
- `create_item_from_imdb_id`, `get_show`, `get_show_aliases`, `map_item_from_data`,
  `get_imdbid_from_tmdb`, `get_imdbid_from_tvdb`, `_get_imdb_id_from_list`,
  `_get_formatted_date`, `_is_anime` from `trakt_api.py`
- The hardcoded `CLIENT_ID` fallback (`trakt_api.py:50-53`) — require an explicit key.

Keep (content lists still need them): `TraktContent`, `_fetch_data`, the list/watchlist/
trending/popular/collection getters, OAuth, `extract_user_list_from_url`,
`resolve_short_url`, the `TRAKT` log level (`utils/logging.py:54`).

If `TraktContent` is *also* being dropped (§8), `trakt_api.py` and
`content/trakt.py` delete entirely, along with `src/routers/secure/default.py:93-108`,
the `TraktModel` settings block, and the `TRAKT` log level.

Also delete `src/program/apis/tvmaze_api.py` unless §4.7 option (iii) is adopted — it is
dead code that imports `TraktModel` for no reason (`tvmaze_api.py:4,31`).

### Section H — Documentation

- `README.md:27` — services table.
- `CHANGELOG.md` — breaking change notice: **item IDs change**, API consumers reading
  `id` must re-fetch; state the DB migration and the drop-and-rebootstrap alternative.
- New `TMDB_DEPLOYMENT.md` covering API key setup and the migration/rollback runbook,
  matching the `TORBOX_DEPLOYMENT.md` convention.

---

## 7. Verification, in order

1. `TMDBAPI.validate()` against a real key.
2. Index a movie (`tt0111161`) — check all §5 fields, confirm 1 HTTP request.
3. Index a normal show (`tt0903747`) — season/episode tree, confirm 1 HTTP request.
4. Index a long-runner (`tt0096697`, 30+ seasons) — confirm the append-budget fallback
   fires and the tree is still complete.
5. Index an anime (`tt2560140`) — `is_anime` true, aliases populated with lowercase keys.
6. Index a title with no IMDb ID — indexes with a warning, does not crash.
7. Run the Alembic migration against a **copy** of the production DB; assert row counts
   match pre-migration, zero orphaned FKs, and quarantined rows are logged.
8. Full pipeline on a fresh request: Requested → Indexed → Scraped → Downloaded →
   Symlinked → Completed.
9. Reindex an existing item via `POST /items/{id}/reindex` — user state (`file`, `folder`,
   `symlinked`, `streams`) survives, i.e. `copy_items` still works.
10. Overseerr webhook with a TMDB ID → correct IMDb resolution.
11. `_init_db_from_symlinks` on a small symlink tree.
12. Full test suite.

---

## 8. Explicitly out of scope (decide separately)

- **`TraktContent`** (watchlist / lists / trending). TMDB has no watchlist equivalent
  without per-user OAuth, and its `/trending` and `/discover` are not like-for-like with
  Trakt's. If the Trakt plan is being cancelled outright this *must* be addressed, but as
  its own piece of work — likely TMDB `/trending` + `/discover` for the
  trending/popular/most-watched settings, and MDBList (already integrated,
  `content/mdblist.py`) for user lists. Flagging it now: if the paid plan lapses mid-way,
  content lists break independently of the indexer.
- Frontend/UI changes for the ID format change.
- Removing the `trakt_id` column (§4.2 — keep for one release, drop later).
- TVMaze air times (§4.7 option iii).

---

## 9. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| ID migration corrupts the DB | **High** | Test on a dump first; `pg_dump` before running; documented drop-and-rebootstrap fallback |
| Rows with no `tmdb_id` | Medium | Quarantine as `Failed` + log; never silently drop |
| Alias case mismatch silently degrades scraping | Medium | §4.6 — explicit test with a non-`us` country code |
| N+1 requests per show | Medium | §4.3 `append_to_response` + response cache; assert request counts in tests |
| Early scraping from date-only `air_date` | Medium | §4.7 +1 day default, tunable |
| Anime misdetection changes ranking | Low | Keyword 210024 primary; fixture test on a known anime |
| Trakt content lists break separately | Medium | §8 — track as its own item |

---

## 10. Checklist

- [ ] §4.1 ID scheme confirmed (recommendation: option B)
- [ ] §4.7 air-date behaviour confirmed (recommendation: option i)
- [ ] §8 decision on `TraktContent` — keeping or replacing
- [ ] A — `apis/tmdb_api.py` + DI registration
- [ ] B — `services/indexers/tmdb.py`
- [ ] C — Alembic migration, tested on a DB copy
- [ ] D — call sites swapped
- [ ] E — settings + env
- [ ] F — fixtures and tests
- [ ] G — Trakt indexer teardown
- [ ] H — docs and changelog
- [ ] §7 verification run end to end
