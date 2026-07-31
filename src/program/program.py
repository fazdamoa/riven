import linecache
import os
import threading
import time
from datetime import datetime
from queue import Empty

from apscheduler.schedulers.background import BackgroundScheduler
from rich.live import Live

from program.apis import bootstrap_apis
from program.managers.event_manager import EventManager
from program.media.item import Episode, MediaItem, Movie, Season, Show
from program.media.state import States
from program.services.content import (
    Listrr,
    Mdblist,
    Overseerr,
    PlexWatchlist,
    TraktContent,
)
from program.services.downloaders import Downloader
from program.services.indexers.trakt import TraktIndexer
from program.services.libraries import SymlinkLibrary
from program.services.libraries.symlink import fix_broken_symlinks
from program.services.post_processing import PostProcessing
from program.services.scrapers import Scraping
from program.services.updaters import Updater
from program.settings.manager import settings_manager
from program.settings.models import get_version
from program.utils import data_dir_path
from program.utils.logging import create_progress_bar, log_cleaner, logger
from program.utils.request import RateLimitExceeded

from .state_transition import process_event
from .symlink import Symlinker
from .types import Event

if settings_manager.settings.tracemalloc:
    import tracemalloc

from sqlalchemy import func, select, text

from program.db import db_functions
from program.db.db import (
    create_database_if_not_exists,
    db,
    run_migrations,
    vacuum_and_analyze_index_maintenance,
)


class Program(threading.Thread):
    """Program class"""

    def __init__(self):
        super().__init__(name="Riven")
        self.initialized = False
        self.running = False
        self.services = {}
        self.enable_trace = settings_manager.settings.tracemalloc
        self.em = EventManager()
        if self.enable_trace:
            tracemalloc.start()
            self.malloc_time = time.monotonic()-50
            self.last_snapshot = None

    def initialize_apis(self):
        bootstrap_apis()

    def initialize_services(self):
        """Initialize all services."""
        self.requesting_services = {
            Overseerr: Overseerr(),
            PlexWatchlist: PlexWatchlist(),
            Listrr: Listrr(),
            Mdblist: Mdblist(),
            TraktContent: TraktContent(),
        }

        self.services = {
            TraktIndexer: TraktIndexer(),
            Scraping: Scraping(),
            Symlinker: Symlinker(),
            Updater: Updater(),
            Downloader: Downloader(),
            # Depends on Symlinker having created the file structure so needs
            # to run after it
            SymlinkLibrary: SymlinkLibrary(),
            PostProcessing: PostProcessing(),
        }

        self.all_services = {
            **self.requesting_services,
            **self.services
        }

        if len([service for service in self.requesting_services.values() if service.initialized]) == 0:
            logger.warning("No content services initialized, items need to be added manually.")
        if not self.services[Scraping].initialized:
            logger.error("No Scraping service initialized, you must enable at least one.")
        if not self.services[Downloader].initialized:
            logger.error("No Downloader service initialized, you must enable at least one.")
        if not self.services[Updater].initialized:
            logger.error("No Updater service initialized, you must enable at least one.")

        if self.enable_trace:
            self.last_snapshot = tracemalloc.take_snapshot()


    def validate(self) -> bool:
        """Validate that all required services are initialized."""
        return all(s.initialized for s in self.services.values())

    def validate_database(self) -> bool:
        """Validate that the database is accessible."""
        try:
            with db.Session() as session:
                session.execute(text("SELECT 1"))
                return True
        except Exception:
            logger.error("Database connection failed. Is the database running?")
            return False

    def start(self):
        latest_version = get_version()
        logger.log("PROGRAM", f"Riven v{latest_version} starting!")

        settings_manager.register_observer(self.initialize_apis)
        settings_manager.register_observer(self.initialize_services)
        os.makedirs(data_dir_path, exist_ok=True)

        if not settings_manager.settings_file.exists():
            logger.log("PROGRAM", "Settings file not found, creating default settings")
            settings_manager.save()

        self.initialize_apis()
        self.initialize_services()

        max_worker_env_vars = [var for var in os.environ if var.endswith("_MAX_WORKERS")]
        if max_worker_env_vars:
            for var in max_worker_env_vars:
                logger.log("PROGRAM", f"{var} is set to {os.environ[var]} workers")

        if not self.validate():
            logger.log("PROGRAM", "----------------------------------------------")
            logger.error("Riven is waiting for configuration to start!")
            logger.log("PROGRAM", "----------------------------------------------")

        while not self.validate():
            time.sleep(1)

        if not self.validate_database():
            # We should really make this configurable via frontend...
            logger.log("PROGRAM", "Database not found, trying to create database")
            if not create_database_if_not_exists():
                logger.error("Failed to create database, exiting")
                return
            logger.success("Database created successfully")

        run_migrations()
        self._cleanup_stale_downloads_on_startup()
        self._init_db_from_symlinks()

        with db.Session() as session:
            movies_symlinks = session.execute(select(func.count(Movie.id)).where(Movie.symlinked == True)).scalar_one() # noqa
            episodes_symlinks = session.execute(select(func.count(Episode.id)).where(Episode.symlinked == True)).scalar_one() # noqa
            total_symlinks = movies_symlinks + episodes_symlinks
            total_movies = session.execute(select(func.count(Movie.id))).scalar_one()
            total_shows = session.execute(select(func.count(Show.id))).scalar_one()
            total_seasons = session.execute(select(func.count(Season.id))).scalar_one()
            total_episodes = session.execute(select(func.count(Episode.id))).scalar_one()
            total_items = session.execute(select(func.count(MediaItem.id))).scalar_one()

            logger.log("ITEM", f"Movies: {total_movies} (Symlinks: {movies_symlinks})")
            logger.log("ITEM", f"Shows: {total_shows}")
            logger.log("ITEM", f"Seasons: {total_seasons}")
            logger.log("ITEM", f"Episodes: {total_episodes} (Symlinks: {episodes_symlinks})")
            logger.log("ITEM", f"Total Items: {total_items} (Symlinks: {total_symlinks})")

        self.executors = []
        self.scheduler = BackgroundScheduler()
        self._schedule_services()
        self._schedule_functions()

        super().start()
        self.scheduler.start()
        logger.success("Riven is running!")
        self.initialized = True

    def _cleanup_stale_downloads_on_startup(self) -> None:
        """
        Clean up stale TorrentDownload state left over from a previous session.

        On every restart we:
        1. Delete PENDING/DOWNLOADING records — their RD torrent IDs are
           unreliable (could have been cleaned up by RD, or just abandoned).
           These items still have streams and will be retried normally.
        2. Delete __NO_STREAMS_COOLDOWN__ sentinel records — cooldowns should
           not survive a restart; a fresh scrape may have found new streams.
        3. For items whose every non-blacklisted stream hash now appears in a
           FAILED/SKIPPED TorrentDownload record, wipe their streams entirely
           so state_transition sends them back to Scraping rather than looping
           through Downloader forever.
        """
        from program.services.downloaders.torrent_download import TorrentDownload, TorrentDownloadStatus
        from program.media.item import MediaItem
        from program.media.stream import Stream, StreamRelation, StreamBlacklistRelation
        from sqlalchemy import delete

        with db.Session() as session:
            # 1. Drop active (non-terminal) download records — they are stale.
            stale_statuses = [
                TorrentDownloadStatus.PENDING.value,
                TorrentDownloadStatus.DOWNLOADING.value,
                TorrentDownloadStatus.READY.value,
            ]
            stale_count = session.query(TorrentDownload).filter(
                TorrentDownload.status.in_(stale_statuses)
            ).count()
            if stale_count:
                session.query(TorrentDownload).filter(
                    TorrentDownload.status.in_(stale_statuses)
                ).delete(synchronize_session=False)
                logger.info(f"Startup cleanup: removed {stale_count} stale active download record(s).")

            # 2. Drop no-streams cooldown sentinels so items are retried fresh.
            cooldown_count = session.query(TorrentDownload).filter(
                TorrentDownload.raw_title == "__NO_STREAMS_COOLDOWN__"
            ).count()
            if cooldown_count:
                session.query(TorrentDownload).filter(
                    TorrentDownload.raw_title == "__NO_STREAMS_COOLDOWN__"
                ).delete(synchronize_session=False)
                logger.info(f"Startup cleanup: removed {cooldown_count} no-streams cooldown record(s).")

            session.commit()

            # 3. Find items in Scraped state where all available stream hashes
            #    are exhausted (every hash is FAILED/SKIPPED in TorrentDownload).
            #    Reset their streams so they go back to Scraping.
            exhausted_statuses = [
                TorrentDownloadStatus.FAILED.value,
                TorrentDownloadStatus.SKIPPED.value,
                TorrentDownloadStatus.LEGAL_ERROR.value,
            ]
            scraped_items = session.execute(
                select(MediaItem)
                .where(MediaItem.last_state == States.Scraped)
                .where(MediaItem.type.in_(["movie", "episode", "season"]))
            ).unique().scalars().all()

            streams_reset_count = 0
            for item in scraped_items:
                # Get non-blacklisted stream infohashes for this item via joins
                blacklisted_hashes = {
                    row[0] for row in
                    session.query(Stream.infohash)
                    .join(StreamBlacklistRelation, StreamBlacklistRelation.stream_id == Stream.id)
                    .filter(StreamBlacklistRelation.media_item_id == item.id)
                    .all()
                }
                available_hashes = [
                    row[0] for row in
                    session.query(Stream.infohash)
                    .join(StreamRelation, StreamRelation.child_id == Stream.id)
                    .filter(StreamRelation.parent_id == item.id)
                    .all()
                    if row[0] not in blacklisted_hashes
                ]

                if not available_hashes:
                    continue

                failed_hashes = {
                    row[0] for row in
                    session.query(TorrentDownload.infohash)
                    .filter(
                        TorrentDownload.media_item_id == item.id,
                        TorrentDownload.status.in_(exhausted_statuses),
                    )
                    .all()
                }

                if all(h in failed_hashes for h in available_hashes):
                    # Every stream is exhausted — wipe streams so Scraping re-runs.
                    session.execute(
                        delete(StreamRelation).where(StreamRelation.parent_id == item.id)
                    )
                    session.execute(
                        delete(StreamBlacklistRelation)
                        .where(StreamBlacklistRelation.media_item_id == item.id)
                    )
                    # Also remove those TorrentDownload failure records so the
                    # fresh scrape result can be attempted with a clean slate.
                    session.query(TorrentDownload).filter(
                        TorrentDownload.media_item_id == item.id,
                        TorrentDownload.status.in_(exhausted_statuses),
                    ).delete(synchronize_session=False)
                    streams_reset_count += 1

            if streams_reset_count:
                session.commit()
                logger.info(
                    f"Startup cleanup: reset streams for {streams_reset_count} item(s) "
                    f"with all streams exhausted — they will be re-scraped."
                )

        logger.debug("Startup cleanup complete.")

    def _retry_library(self) -> None:
        """Retry items that failed to download."""
        with db.Session() as session:
            item_ids = db_functions.retry_library(session)
            for item_id in item_ids:
                self.em.add_event(Event(emitted_by="RetryLibrary", item_id=item_id))

            if item_ids:
                logger.log("PROGRAM", f"Successfully retried {len(item_ids)} incomplete items")
            else:
                logger.log("PROGRAM", "No items required retrying")

    def _update_ongoing(self) -> None:
        """Update state for ongoing and unreleased items."""
        with db.Session() as session:
            updated_items = db_functions.update_ongoing(session)
            for item_id, previous_state, new_state in updated_items:
                self.em.add_event(Event(emitted_by="UpdateOngoing", item_id=item_id))
                logger.debug(f"Updated state for item {item_id} from {previous_state} to {new_state}")

            if updated_items:
                logger.log("PROGRAM", f"Successfully updated {len(updated_items)} items with a new state")
            else:
                logger.log("PROGRAM", "No items required state updates")

    def _check_active_downloads(self) -> None:
        """Check status of active Real-Debrid downloads and process completed ones."""
        try:
            downloader = self.services.get(Downloader)
            if not downloader or not downloader.initialized:
                return
            
            processed = downloader.check_active_downloads()
            
            if processed > 0:
                logger.log("PROGRAM", f"Checked {processed} active downloads")
                
                # Trigger state transitions for any items that completed
                from program.services.downloaders.torrent_download import TorrentDownload, TorrentDownloadStatus
                with db.Session() as session:
                    ready_downloads = TorrentDownload.get_ready_downloads(session)
                    for download in ready_downloads:
                        self.em.add_event(Event(emitted_by="DownloadChecker", item_id=download.media_item_id))
        except Exception as e:
            logger.error(f"Error checking active downloads: {e}")

    def _cleanup_download_records(self) -> None:
        """Clean up old completed/failed download records."""
        try:
            downloader = self.services.get(Downloader)
            if not downloader or not downloader.initialized:
                return
            
            deleted = downloader.cleanup_old_downloads(days=30)
            if deleted > 0:
                logger.log("PROGRAM", f"Cleaned up {deleted} old download records")
        except Exception as e:
            logger.error(f"Error cleaning up download records: {e}")

    def _audit_symlinks(self) -> None:
        """DB-vs-disk symlink audit: reset and re-acquire items the database
        marks as symlinked whose symlink no longer exists on disk."""
        try:
            from program.services.libraries.symlink import audit_missing_symlinks
            item_ids = audit_missing_symlinks(
                settings_manager.settings.symlink.library_path,
                settings_manager.settings.symlink.rclone_path,
            )
            for item_id in item_ids:
                self.em.add_event(Event(emitted_by="RetryLibrary", item_id=item_id))
        except Exception as e:
            logger.error(f"Error running symlink audit: {e}")

    def _schedule_functions(self) -> None:
        """Schedule each service based on its update interval."""
        scheduled_functions = {
            self._update_ongoing: {"interval": 60 * 60 * 4},
            self._retry_library: {"interval": 60 * 60 * 24},
            self._check_active_downloads: {"interval": 60},  # TorBox: check every minute
            self._cleanup_download_records: {"interval": 60 * 60 * 24},  # Daily cleanup
            log_cleaner: {"interval": 60 * 60},
            vacuum_and_analyze_index_maintenance: {"interval": 60 * 60 * 24},
        }

        if settings_manager.settings.symlink.repair_symlinks:
            scheduled_functions[fix_broken_symlinks] = {
                "interval": 60 * 60 * settings_manager.settings.symlink.repair_interval,
                "args": [settings_manager.settings.symlink.library_path, settings_manager.settings.symlink.rclone_path]
            }
            # DB-vs-disk audit: catches items marked symlinked whose symlink
            # was deleted from disk (invisible to fix_broken_symlinks).
            scheduled_functions[self._audit_symlinks] = {
                "interval": 60 * 60 * settings_manager.settings.symlink.repair_interval,
            }
            # logger.warning("Symlink repair is disabled, this will be re-enabled in the future.")

        for func, config in scheduled_functions.items():
            self.scheduler.add_job(
                func,
                "interval",
                seconds=config["interval"],
                args=config.get("args"),
                id=f"{func.__name__}",
                max_instances=config.get("max_instances", 1),
                replace_existing=True,
                next_run_time=datetime.now(),
                misfire_grace_time=30
            )
            logger.debug(f"Scheduled {func.__name__} to run every {config['interval']} seconds.")

    def _schedule_services(self) -> None:
        """Schedule each service based on its update interval."""
        scheduled_services = {**self.requesting_services, SymlinkLibrary: self.services[SymlinkLibrary]}
        for service_cls, service_instance in scheduled_services.items():
            if not service_instance.initialized:
                continue
            if not (update_interval := getattr(service_instance.settings, "update_interval", False)):
                continue

            self.scheduler.add_job(
                self.em.submit_job,
                "interval",
                seconds=update_interval,
                args=[service_cls, self],
                id=f"{service_cls.__name__}_update",
                max_instances=1,
                replace_existing=True,
                next_run_time=datetime.now() if service_cls != SymlinkLibrary else None,
                coalesce=False,
            )
            logger.debug(f"Scheduled {service_cls.__name__} to run every {update_interval} seconds.")

    def display_top_allocators(self, snapshot, key_type="lineno", limit=10):
        import psutil
        process = psutil.Process(os.getpid())
        top_stats = snapshot.compare_to(self.last_snapshot, "lineno")

        logger.debug("Top %s lines" % limit)
        for index, stat in enumerate(top_stats[:limit], 1):
            frame = stat.traceback[0]
            # replace "/path/to/module/file.py" with "module/file.py"
            filename = os.sep.join(frame.filename.split(os.sep)[-2:])
            logger.debug("#%s: %s:%s: %.1f KiB"
                % (index, filename, frame.lineno, stat.size / 1024))
            line = linecache.getline(frame.filename, frame.lineno).strip()
            if line:
                logger.debug("    %s" % line)

        other = top_stats[limit:]
        if other:
            size = sum(stat.size for stat in other)
            logger.debug("%s other: %.1f MiB" % (len(other), size / (1024 * 1024)))
        total = sum(stat.size for stat in top_stats)
        logger.debug("Total allocated size: %.1f MiB" % (total / (1024 * 1024)))
        logger.debug(f"Process memory: {process.memory_info().rss / (1024 * 1024):.2f} MiB")

    def dump_tracemalloc(self):
        if time.monotonic() - self.malloc_time > 60:
            self.malloc_time = time.monotonic()
            snapshot = tracemalloc.take_snapshot()
            self.display_top_allocators(snapshot)

    def run(self):
        while self.initialized:
            if not self.validate():
                time.sleep(1)
                continue

            try:
                event: Event = self.em.next()
                if self.enable_trace:
                    self.dump_tracemalloc()
            except Empty:
                if self.enable_trace:
                    self.dump_tracemalloc()
                time.sleep(0.1)
                continue

            existing_item: MediaItem = db_functions.get_item_by_id(event.item_id)

            next_service, items_to_submit = process_event(
                event.emitted_by, existing_item, event.content_item
            )

            for item_to_submit in items_to_submit:
                if not next_service:
                    self.em.add_event_to_queue(Event("StateTransition", item_id=item_to_submit.id))
                else:
                    # We are in the database, pass on id.
                    if item_to_submit.id:
                        event = Event(next_service, item_id=item_to_submit.id)
                    # We are not, lets pass the MediaItem
                    else:
                        event = Event(next_service, content_item=item_to_submit)

                    # Event will be added to running when job actually starts in submit_job
                    self.em.submit_job(next_service, self, event)

    def stop(self):
        if not self.initialized:
            return

        if hasattr(self, "executors"):
            for executor in self.executors:
                if not executor["_executor"]._shutdown:
                    executor["_executor"].shutdown(wait=False)
        if hasattr(self, "scheduler") and self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        logger.log("PROGRAM", "Riven has been stopped.")

    def _enhance_item(self, item: MediaItem) -> MediaItem | None:
        try:
            enhanced_item = next(self.services[TraktIndexer].run(item, log_msg=False))
            return enhanced_item
        except StopIteration:
            return None

    def _init_db_from_symlinks(self):
        """Initialize the database from symlinks."""
        start_time = datetime.now()
        with db.Session() as session:
            # Check if database is empty
            if not session.execute(select(func.count(MediaItem.id))).scalar_one():
                if not settings_manager.settings.map_metadata:
                    return

                logger.log("PROGRAM", "Collecting items from symlinks, this may take a while depending on library size")
                try:
                    items = self.services[SymlinkLibrary].run()
                    errors = []
                    added_items = set()

                    # Convert items to list and get total count
                    items_list = [item for item in items if isinstance(item, (Movie, Show))]
                    total_items = len(items_list)
                    progress, console = create_progress_bar(total_items)
                    task = progress.add_task("Enriching items with metadata", total=total_items, log="")
                    processed_count = 0

                    with Live(progress, console=console, refresh_per_second=10):
                        for item in items_list:
                            processed_count += 1
                            log_message = ""

                            try:
                                # Skip duplicates
                                if not item or item.imdb_id in added_items:
                                    errors.append(f"Duplicate symlink directory found for {item.log_string if item else 'Unknown'}")
                                    log_message = f"Skipped duplicate: {item.log_string if item else 'Unknown'}"
                                    progress.update(task, advance=1, log=log_message)
                                    continue

                                if db_functions.get_item_by_id(item.id, session=session):
                                    errors.append(f"Duplicate item found in database for id: {item.id}")
                                    log_message = f"Already in database: {item.log_string}"
                                    progress.update(task, advance=1, log=log_message)
                                    continue

                                # Enhance item with Trakt data
                                try:
                                    enhanced_item = self._enhance_item(item)
                                    if not enhanced_item:
                                        errors.append(f"Failed to enhance {item.log_string} ({item.imdb_id}) with Trakt Indexer")
                                        log_message = f"Failed to enhance: {item.log_string}"
                                        progress.update(task, advance=1, log=log_message)
                                        continue
                                except RateLimitExceeded:
                                    # Rate limit hit - wait and retry once
                                    logger.warning(f"Rate limit hit for {item.log_string}, waiting 10 seconds...")
                                    time.sleep(10)
                                    try:
                                        enhanced_item = self._enhance_item(item)
                                        if not enhanced_item:
                                            errors.append(f"Failed to enhance {item.log_string} after retry")
                                            log_message = f"Failed after retry: {item.log_string}"
                                            progress.update(task, advance=1, log=log_message)
                                            continue
                                    except Exception as e:
                                        errors.append(f"Rate limit retry failed for {item.log_string}: {str(e)}")
                                        log_message = f"Retry failed: {item.log_string}"
                                        progress.update(task, advance=1, log=log_message)
                                        continue
                                except Exception as e:
                                    errors.append(f"Error enhancing {item.log_string}: {str(e)}")
                                    log_message = f"Error: {item.log_string}"
                                    progress.update(task, advance=1, log=log_message)
                                    continue

                                # Save to database
                                enhanced_item.store_state()
                                session.add(enhanced_item)
                                added_items.add(item.imdb_id)

                                # Commit every 25 items to avoid large transactions
                                if processed_count % 25 == 0:
                                    session.commit()

                                log_message = f"Successfully Indexed {enhanced_item.log_string}"
                                progress.update(task, advance=1, log=log_message)

                            except Exception as e:
                                errors.append(f"Unexpected error for {item.log_string}: {str(e)}")
                                continue

                        # Final commit for any remaining items
                        session.commit()
                        
                        progress.update(task, log="Finished Indexing Symlinks!")

                    if errors:
                        logger.error("Errors encountered during initialization")
                        for error in errors:
                            logger.error(error)

                except Exception as e:
                    session.rollback()
                    logger.error(f"Failed to initialize database from symlinks: {str(e)}")
                    return

                elapsed_time = datetime.now() - start_time
                total_seconds = elapsed_time.total_seconds()
                hours, remainder = divmod(total_seconds, 3600)
                minutes, seconds = divmod(remainder, 60)
                logger.success(f"Database initialized, time taken: h{int(hours):02d}:m{int(minutes):02d}:s{int(seconds):02d}")
