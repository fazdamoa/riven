"""
TorBox downloader tests — rewritten for the new single-torrent contract.

Mirrors the structure of test_alldebrid_downloader.py and uses responses
(or unittest.mock) to intercept HTTP calls rather than patching module-level
functions that no longer exist.
"""

import json
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

TEST_DATA = Path(__file__).parent / "test_data"
UBUNTU_HASH = "3648baf850d5930510c1f172b534200ebb5496e6"
TORRENT_ID = "251993753"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load(filename: str) -> dict:
    with open(TEST_DATA / filename) as f:
        return json.load(f)


def _make_response(data: dict, status_code: int = 200) -> MagicMock:
    """Build a minimal mock that looks like a BaseRequestHandler response."""
    mock = MagicMock()
    mock.status_code = status_code
    mock.is_ok = status_code < 400
    mock.data = data
    return mock


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def settings(tmp_path):
    """Patch settings so TorBoxDownloader can be constructed without real config."""
    mock_settings = MagicMock()
    mock_settings.downloaders.torbox.enabled = True
    mock_settings.downloaders.torbox.api_key = "test-api-key"
    mock_settings.downloaders.proxy_url = None
    mock_settings.downloaders.video_extensions = ["mkv", "mp4", "avi"]
    mock_settings.downloaders.movie_filesize_mb_min = 100
    mock_settings.downloaders.movie_filesize_mb_max = 50000
    mock_settings.downloaders.episode_filesize_mb_min = 20
    mock_settings.downloaders.episode_filesize_mb_max = 10000
    with patch("program.services.downloaders.torbox.settings_manager") as mock_sm, \
         patch("program.services.downloaders.shared.settings_manager") as mock_sm2:
        mock_sm.settings = mock_settings
        mock_sm2.settings = mock_settings
        yield mock_settings


@pytest.fixture
def downloader(settings):
    """
    Return an initialised TorBoxDownloader with the validate() network call mocked
    to succeed.
    """
    from program.services.downloaders.torbox import TorBoxDownloader

    user_me_response = {
        "success": True,
        "data": {
            "plan": 2,
            "premium_expires_at": "2099-01-01T00:00:00Z",
        },
    }

    with patch(
        "program.services.downloaders.torbox.TorBoxRequestHandler.execute",
        return_value=user_me_response["data"],
    ):
        dl = TorBoxDownloader()

    assert dl.initialized, "TorBoxDownloader failed to initialize in test fixture"
    # Give it a real (mocked) request_handler
    dl.api.request_handler = MagicMock()
    return dl


# ---------------------------------------------------------------------------
# A.2 / A.3 — check_cache
# ---------------------------------------------------------------------------

class TestCheckCache:
    def test_cached_hash_returns_true(self, downloader):
        data = _load("torbox_checkcached_cached.json")["data"]
        downloader.api.request_handler.execute.return_value = data

        result = downloader.check_cache(UBUNTU_HASH)
        assert result is True

    def test_uncached_hash_returns_false(self, downloader):
        downloader.api.request_handler.execute.return_value = {}

        result = downloader.check_cache(UBUNTU_HASH)
        assert result is False

    def test_rate_limited_returns_false(self, downloader):
        from program.services.downloaders.torbox import TorBoxError, TorBoxErrorType

        downloader.api.request_handler.execute.side_effect = TorBoxError(
            "Rate limited", TorBoxErrorType.RATE_LIMITED
        )
        result = downloader.check_cache(UBUNTU_HASH)
        assert result is False

    def test_network_error_returns_false(self, downloader):
        downloader.api.request_handler.execute.side_effect = Exception("connection error")
        result = downloader.check_cache(UBUNTU_HASH)
        assert result is False


# ---------------------------------------------------------------------------
# check_cache_batch
# ---------------------------------------------------------------------------

class TestCheckCacheBatch:
    def test_batch_all_cached(self, downloader):
        h1 = "a" * 40
        h2 = "b" * 40
        data = {h1: {"name": "file1.mkv"}, h2: {"name": "file2.mkv"}}
        downloader.api.request_handler.execute.return_value = data

        results = downloader.check_cache_batch([h1, h2])
        assert results[h1] is True
        assert results[h2] is True

    def test_batch_partial_cache(self, downloader):
        h1 = "a" * 40
        h2 = "b" * 40
        data = {h1: {"name": "file1.mkv"}}  # h2 missing → not cached
        downloader.api.request_handler.execute.return_value = data

        results = downloader.check_cache_batch([h1, h2])
        assert results[h1] is True
        assert results[h2] is False

    def test_batch_error_returns_all_false(self, downloader):
        from program.services.downloaders.torbox import TorBoxError, TorBoxErrorType

        hashes = ["a" * 40, "b" * 40, "c" * 40]
        downloader.api.request_handler.execute.side_effect = TorBoxError(
            "Server error", TorBoxErrorType.SERVICE_ERROR
        )
        results = downloader.check_cache_batch(hashes)
        assert all(v is False for v in results.values())

    def test_batch_splits_into_chunks_of_100(self, downloader):
        hashes = [f"{i:040x}" for i in range(150)]
        downloader.api.request_handler.execute.return_value = {}

        downloader.check_cache_batch(hashes)
        # Should be called twice (100 + 50)
        assert downloader.api.request_handler.execute.call_count == 2


# ---------------------------------------------------------------------------
# add_torrent
# ---------------------------------------------------------------------------

class TestAddTorrent:
    def _mock_add_then_status(self, downloader, add_data: dict, status_data: dict):
        """Mock execute to return add_data on first call, status_data on second."""
        downloader.api.request_handler.execute.side_effect = [add_data, status_data]

    def test_returns_id_and_status(self, downloader):
        add_data = _load("torbox_createtorrent.json")["data"]
        status_data = _load("torbox_mylist_cached.json")["data"]
        self._mock_add_then_status(downloader, add_data, status_data)

        torrent_id, status = downloader.add_torrent(UBUNTU_HASH)
        assert torrent_id == TORRENT_ID
        assert status.is_cached is True

    def test_status_reflects_cached_flags(self, downloader):
        add_data = _load("torbox_createtorrent.json")["data"]
        status_data = _load("torbox_mylist_cached.json")["data"]
        self._mock_add_then_status(downloader, add_data, status_data)

        _, status = downloader.add_torrent(UBUNTU_HASH)
        assert status.is_cached is True
        assert status.is_error is False
        assert status.needs_file_selection is False

    def test_raises_rate_limited_on_429(self, downloader):
        from program.services.downloaders.torbox import TorBoxError, TorBoxErrorType

        downloader.api.request_handler.execute.side_effect = TorBoxError(
            "Rate limit exceeded", TorBoxErrorType.RATE_LIMITED
        )
        with pytest.raises(TorBoxError) as exc_info:
            downloader.add_torrent(UBUNTU_HASH)
        assert exc_info.value.error_type == TorBoxErrorType.RATE_LIMITED

    def test_local_hourly_guard_fires_after_60_calls(self, downloader):
        from program.services.downloaders.torbox import TorBoxError, TorBoxErrorType

        # Pre-fill the deque with 60 timestamps within the last hour
        now = datetime.now()
        downloader._add_timestamps = deque(
            [now - timedelta(minutes=30)] * 60
        )

        with pytest.raises(TorBoxError) as exc_info:
            downloader._check_add_rate_limit()
        assert exc_info.value.error_type == TorBoxErrorType.RATE_LIMITED

    def test_local_guard_allows_after_hour_elapses(self, downloader):
        # Timestamps older than 1 hour should be pruned → guard should not fire
        old_time = datetime.now() - timedelta(hours=2)
        downloader._add_timestamps = deque([old_time] * 60)
        # Should not raise
        downloader._check_add_rate_limit()


# ---------------------------------------------------------------------------
# get_torrent_status
# ---------------------------------------------------------------------------

class TestGetTorrentStatus:
    def test_cached_status(self, downloader):
        data = _load("torbox_mylist_cached.json")["data"]
        downloader.api.request_handler.execute.return_value = data

        status = downloader.get_torrent_status(TORRENT_ID)
        assert status.is_cached is True
        assert status.is_downloading is False
        assert status.is_error is False

    def test_downloading_status(self, downloader):
        data = _load("torbox_mylist_downloading.json")["data"]
        downloader.api.request_handler.execute.return_value = data

        status = downloader.get_torrent_status(TORRENT_ID)
        assert status.is_cached is False
        assert status.is_downloading is True
        assert status.is_error is False

    def test_error_status(self, downloader):
        data = _load("torbox_mylist_error.json")["data"]
        downloader.api.request_handler.execute.return_value = data

        status = downloader.get_torrent_status(TORRENT_ID)
        assert status.is_cached is False
        assert status.is_error is True

    @pytest.mark.parametrize("state,expected_downloading", [
        ("Downloading", True),
        ("downloading", True),
        ("queued", True),
        ("metaDL", True),
        ("Stalled (No Seeds)", True),
        ("stalledDL (No seeds)", True),
        ("uploading", True),
        ("checking", True),
        ("checkingDL", True),
        ("downloaded", False),
        ("failed", False),
    ])
    def test_state_string_casing(self, downloader, state, expected_downloading):
        """TorBox download_state strings have inconsistent casing — all should map correctly."""
        data = _load("torbox_mylist_cached.json")["data"].copy()
        data["download_state"] = state
        # Only states that are not "downloaded" but also not error should be downloading
        data["cached"] = (state == "downloaded")
        data["download_finished"] = (state == "downloaded")
        data["download_present"] = (state == "downloaded")

        status = downloader._build_torrent_status(TORRENT_ID, data)
        assert status.is_downloading == expected_downloading, (
            f"State '{state}': expected is_downloading={expected_downloading}, "
            f"got {status.is_downloading}"
        )

    def test_needs_file_selection_always_false(self, downloader):
        data = _load("torbox_mylist_cached.json")["data"]
        downloader.api.request_handler.execute.return_value = data

        status = downloader.get_torrent_status(TORRENT_ID)
        assert status.needs_file_selection is False


# ---------------------------------------------------------------------------
# find_existing_torrent
# ---------------------------------------------------------------------------

class TestFindExistingTorrent:
    def test_finds_matching_hash(self, downloader):
        list_data = _load("torbox_mylist_all.json")["data"]
        downloader.api.request_handler.execute.return_value = list_data

        result = downloader.find_existing_torrent(UBUNTU_HASH)
        assert result is not None
        torrent_id, status = result
        assert torrent_id == TORRENT_ID
        assert status.is_cached is True

    def test_returns_none_for_unknown_hash(self, downloader):
        list_data = _load("torbox_mylist_all.json")["data"]
        downloader.api.request_handler.execute.return_value = list_data

        result = downloader.find_existing_torrent("0" * 40)
        assert result is None

    def test_uses_cache_within_ttl(self, downloader):
        list_data = _load("torbox_mylist_all.json")["data"]
        downloader.api.request_handler.execute.return_value = list_data

        # First call populates cache
        downloader.find_existing_torrent(UBUNTU_HASH)
        # Second call should use cache — execute should only be called once
        downloader.find_existing_torrent(UBUNTU_HASH)
        assert downloader.api.request_handler.execute.call_count == 1

    def test_cache_invalidated_on_add_torrent(self, downloader):
        from program.services.downloaders.torbox import TorBoxError, TorBoxErrorType

        list_data = _load("torbox_mylist_all.json")["data"]
        add_data = _load("torbox_createtorrent.json")["data"]
        status_data = _load("torbox_mylist_cached.json")["data"]

        # First call: populate list cache
        downloader.api.request_handler.execute.return_value = list_data
        downloader.find_existing_torrent(UBUNTU_HASH)
        assert downloader._list_cache is not None

        # add_torrent should clear the cache
        downloader.api.request_handler.execute.side_effect = [add_data, status_data]
        downloader.add_torrent(UBUNTU_HASH)
        assert downloader._list_cache is None


# ---------------------------------------------------------------------------
# select_video_files
# ---------------------------------------------------------------------------

class TestSelectVideoFiles:
    def test_is_noop_and_returns_true(self, downloader):
        result = downloader.select_video_files(TORRENT_ID)
        assert result is True
        # Should NOT call the API
        downloader.api.request_handler.execute.assert_not_called()


# ---------------------------------------------------------------------------
# process_completed_torrent
# ---------------------------------------------------------------------------

class TestProcessCompletedTorrent:
    def test_returns_container_with_valid_files(self, downloader):
        # Use a video file with a realistic size
        data = _load("torbox_mylist_cached.json")["data"].copy()
        data["files"] = [
            {
                "id": 1,
                "name": "Big.Buck.Bunny.2008.1080p.BluRay.mkv",
                "short_name": "Big.Buck.Bunny.2008.1080p.BluRay.mkv",
                "size": 8_000_000_000,  # 8 GB — well within movie range
            }
        ]
        downloader.api.request_handler.execute.return_value = data

        container = downloader.process_completed_torrent(TORRENT_ID, UBUNTU_HASH, "movie")
        assert container is not None
        assert len(container.files) == 1
        assert container.files[0].filename == "Big.Buck.Bunny.2008.1080p.BluRay.mkv"

    def test_skips_sample_files(self, downloader):
        data = _load("torbox_mylist_cached.json")["data"].copy()
        data["files"] = [
            {
                "id": 1,
                "name": "sample.mkv",
                "short_name": "sample.mkv",
                "size": 50_000_000,
            }
        ]
        downloader.api.request_handler.execute.return_value = data

        container = downloader.process_completed_torrent(TORRENT_ID, UBUNTU_HASH, "movie")
        assert container is None

    def test_skips_non_video_files(self, downloader):
        data = _load("torbox_mylist_cached.json")["data"].copy()
        data["files"] = [
            {
                "id": 1,
                "name": "subtitle.srt",
                "short_name": "subtitle.srt",
                "size": 100_000,
            }
        ]
        downloader.api.request_handler.execute.return_value = data

        container = downloader.process_completed_torrent(TORRENT_ID, UBUNTU_HASH, "movie")
        assert container is None

    def test_returns_none_when_not_cached(self, downloader):
        data = _load("torbox_mylist_downloading.json")["data"]
        downloader.api.request_handler.execute.return_value = data

        container = downloader.process_completed_torrent(TORRENT_ID, UBUNTU_HASH, "movie")
        assert container is None

    def test_returns_none_when_no_files(self, downloader):
        data = _load("torbox_mylist_cached.json")["data"].copy()
        data["files"] = []
        downloader.api.request_handler.execute.return_value = data

        container = downloader.process_completed_torrent(TORRENT_ID, UBUNTU_HASH, "movie")
        assert container is None


# ---------------------------------------------------------------------------
# delete_torrent
# ---------------------------------------------------------------------------

class TestDeleteTorrent:
    def test_sends_correct_body(self, downloader):
        downloader.api.request_handler.execute.return_value = {}

        result = downloader.delete_torrent(TORRENT_ID)
        assert result is True

        call_kwargs = downloader.api.request_handler.execute.call_args
        # data kwarg should have int id and "delete" operation
        body = call_kwargs.kwargs.get("data") or call_kwargs.args[-1] if call_kwargs.args else {}
        # Accept positional or keyword 'data'
        if not body and call_kwargs.kwargs:
            body = call_kwargs.kwargs.get("data", {})
        assert body.get("id") == int(TORRENT_ID)
        assert body.get("operation") == "delete"

    def test_returns_false_on_exception(self, downloader):
        downloader.api.request_handler.execute.side_effect = Exception("network error")
        result = downloader.delete_torrent(TORRENT_ID)
        assert result is False


# ---------------------------------------------------------------------------
# Legacy methods
# ---------------------------------------------------------------------------

class TestLegacyMethods:
    def test_add_torrent_legacy_returns_string(self, downloader):
        add_data = _load("torbox_createtorrent.json")["data"]
        status_data = _load("torbox_mylist_cached.json")["data"]
        downloader.api.request_handler.execute.side_effect = [add_data, status_data]

        result = downloader.add_torrent_legacy(UBUNTU_HASH)
        assert isinstance(result, str)
        assert result == TORRENT_ID

    def test_select_files_is_noop(self, downloader):
        # Should not raise, should not call API
        downloader.select_files(TORRENT_ID, [1, 2, 3])
        downloader.api.request_handler.execute.assert_not_called()

    def test_get_torrent_info_returns_torrent_info(self, downloader):
        data = _load("torbox_mylist_cached.json")["data"]
        downloader.api.request_handler.execute.return_value = data

        info = downloader.get_torrent_info(TORRENT_ID)
        assert info.name == "ubuntu-24.04-desktop-amd64.iso"
        assert info.infohash == UBUNTU_HASH
