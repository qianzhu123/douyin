from pathlib import Path

from backend.config import DOWNLOAD_OUTPUT_DIR, DOWNLOADER_DIR, MONITOR_DIR
from backend.services import (
    DownloadService,
    MonitorService,
    _extract_duration_from_payload,
    _extract_preview_video_duration,
    _extract_video_duration,
    extract_douyin_urls,
    extract_aweme_ids_from_url,
    load_app_settings,
    parse_search_candidates,
    parse_search_summaries,
    read_profile_cache,
    read_project_users,
    save_app_settings,
    delete_project_user,
    reorder_project_users,
    upsert_profile_cache,
    upsert_project_user,
)
from backend.tool_loader import load_module
import pytest
import time


def test_extract_douyin_urls_from_share_text():
    text = "复制口令 https://v.douyin.com/abc123/ ，再看 https://www.douyin.com/video/123456。"

    assert extract_douyin_urls(text) == [
        "https://v.douyin.com/abc123/",
        "https://www.douyin.com/video/123456",
    ]


def test_extract_douyin_urls_splits_adjacent_www_links():
    text = "https://www.douyin.com/video/111https://www.douyin.com/note/222"

    assert extract_douyin_urls(text) == [
        "https://www.douyin.com/video/111",
        "https://www.douyin.com/note/222",
    ]


def test_extract_aweme_ids_includes_modal_and_vid_candidates():
    url = (
        "https://www.douyin.com/user/MS4w?from_tab_name=main"
        "&modal_id=7652964976545565988&vid=7627686789402103076"
    )

    assert extract_aweme_ids_from_url(url) == [
        "7652964976545565988",
        "7627686789402103076",
    ]


def test_downloader_aggregation_detail_candidates_are_video_first():
    downloader = load_module("test_douyin_downloader_candidates", DOWNLOADER_DIR / "downloader.py")
    url = (
        "https://www.douyin.com/user/MS4w?from_tab_name=main"
        "&modal_id=7652964976545565988&vid=7627686789402103076"
    )

    assert downloader.extract_aweme_ids_from_url(url) == [
        "7652964976545565988",
        "7627686789402103076",
    ]
    assert downloader._detail_url_candidates(url)[:2] == [
        "https://www.douyin.com/video/7652964976545565988",
        "https://www.douyin.com/note/7652964976545565988",
    ]
    assert downloader._detail_url_candidates("https://www.douyin.com/jingxuan?modal_id=7627686789402103076")[:2] == [
        "https://www.douyin.com/video/7627686789402103076",
        "https://www.douyin.com/note/7627686789402103076",
    ]


def test_monitor_settings_parse_labels_and_urls():
    module = load_module("test_douyin_monitor_tool", MONITOR_DIR / "main.py")

    users = module.parse_settings()

    assert users
    assert all(user["label"] for user in users)
    assert all(user["url"].startswith("https://www.douyin.com/user/") for user in users)
    assert all(user["sec_uid"].startswith("MS4w") for user in users)


def test_parse_search_candidates_extracts_display_fields():
    payload = {
        "user_list": [
            {
                "user_info": {
                    "nickname": "HangHang",
                    "unique_id": "HHang02",
                    "signature": "每天晚上10点-4点直播",
                    "follower_count": 11434748,
                    "following_count": 482,
                    "total_favorited": 99462816,
                    "ip_location": "IP属地：广东",
                    "sec_uid": "MS4wLjABAAAAZX-0fCG3Wa8mkZpyvTgB-QA3jTvFWY1TSJagDaemIpA",
                    "room_id_str": "7658626418156538650",
                    "avatar_thumb": {"url_list": ["https://example.test/avatar.jpeg"]},
                    "room_data": "{\"status\":2,\"user_count\":92048}",
                }
            }
        ]
    }

    candidates = parse_search_candidates(payload)

    assert candidates[0].nickname == "HangHang"
    assert candidates[0].unique_id == "HHang02"
    assert candidates[0].avatar_url == "https://example.test/avatar.jpeg"
    assert candidates[0].follower_count == 11434748
    assert candidates[0].following_count == 482
    assert candidates[0].total_favorited == 99462816
    assert candidates[0].ip_location == "IP属地：广东"
    assert candidates[0].homepage_url.endswith(candidates[0].sec_uid)
    assert candidates[0].live_status == 1


def test_parse_search_summaries_preserves_following_and_ip_location():
    summaries = [
        {
            "nickname": "HangHang",
            "unique_id": "HHang02",
            "follower_count": 11434748,
            "following_count": 482,
            "total_favorited": 99462816,
            "ip_location": "IP属地：广东",
            "sec_uid": "MS4wLjABAAAAZX-0fCG3Wa8mkZpyvTgB-QA3jTvFWY1TSJagDaemIpA",
            "homepage": "https://www.douyin.com/user/MS4wLjABAAAAZX-0fCG3Wa8mkZpyvTgB-QA3jTvFWY1TSJagDaemIpA",
        }
    ]

    candidates = parse_search_summaries(summaries)

    assert candidates[0].following_count == 482
    assert candidates[0].ip_location == "IP属地：广东"


def test_upsert_project_user_is_idempotent(tmp_path):
    users_file = tmp_path / "users.json"
    sec_uid = "MS4wLjABAAAAExampleUser"
    url = f"https://www.douyin.com/user/{sec_uid}"

    first = upsert_project_user(users_file, {"label": "测试用户", "sec_uid": sec_uid, "url": url})
    second = upsert_project_user(users_file, {"label": "测试用户", "sec_uid": sec_uid, "url": url})
    users = read_project_users(users_file)

    assert first.added is True
    assert second.added is False
    assert len(users) == 1
    assert users[0].sec_uid == sec_uid


def test_reorder_project_users_moves_known_users_and_keeps_unknown_order(tmp_path):
    users_file = tmp_path / "users.json"
    first = upsert_project_user(users_file, {"label": "一", "sec_uid": "uid-1", "url": "https://www.douyin.com/user/uid-1"})
    second = upsert_project_user(users_file, {"label": "二", "sec_uid": "uid-2", "url": "https://www.douyin.com/user/uid-2"})
    third = upsert_project_user(users_file, {"label": "三", "sec_uid": "uid-3", "url": "https://www.douyin.com/user/uid-3"})

    users = reorder_project_users(users_file, [third.entry.sec_uid, first.entry.sec_uid])

    assert [user.sec_uid for user in users] == [third.entry.sec_uid, first.entry.sec_uid, second.entry.sec_uid]
    assert [user.sec_uid for user in read_project_users(users_file)] == [third.entry.sec_uid, first.entry.sec_uid, second.entry.sec_uid]


def test_delete_project_user_removes_matching_account(tmp_path):
    users_file = tmp_path / "users.json"
    upsert_project_user(users_file, {"label": "一", "sec_uid": "uid-1", "url": "https://www.douyin.com/user/uid-1"})
    upsert_project_user(users_file, {"label": "二", "sec_uid": "uid-2", "url": "https://www.douyin.com/user/uid-2"})

    users = delete_project_user(users_file, "uid-1")

    assert [user.sec_uid for user in users] == ["uid-2"]
    assert [user.sec_uid for user in read_project_users(users_file)] == ["uid-2"]


def test_default_download_output_is_user_downloads():
    assert DOWNLOAD_OUTPUT_DIR == Path.home() / "Downloads"


def test_download_job_runs_from_sync_context_and_records_logs():
    class FakeDownloader:
        @staticmethod
        def download_douyin(url, output_dir, mode, fetch_comments=False, selected_indices=None, wrap_folder=False):
            return {
                "type": "video",
                "title": "demo",
                "url": url,
                "output_dir": output_dir,
                "mode": mode,
                "wrap_folder": wrap_folder,
            }

    service = DownloadService.__new__(DownloadService)
    service.module = FakeDownloader()
    service.jobs = {}

    job = service.create_job("https://www.douyin.com/video/111", 1, "", False)
    deadline = time.time() + 2
    while service.get_job(job.id).status in {"queued", "running"} and time.time() < deadline:
        time.sleep(0.02)

    finished = service.get_job(job.id)
    assert finished.status == "done"
    assert finished.results[0]["title"] == "demo"
    assert finished.output_dir == str(Path.home() / "Downloads")
    assert finished.wrap_folder is False
    assert finished.results[0]["wrap_folder"] is False
    assert any("Queued" in entry["message"] for entry in finished.logs)
    assert any("Finished" in entry["message"] for entry in finished.logs)


def test_download_job_uses_custom_output_dir_and_wrap_folder(tmp_path):
    class FakeDownloader:
        @staticmethod
        def download_douyin(url, output_dir, mode, fetch_comments=False, selected_indices=None, wrap_folder=False):
            return {"type": "video", "output_dir": output_dir, "wrap_folder": wrap_folder}

    service = DownloadService.__new__(DownloadService)
    service.module = FakeDownloader()
    service.jobs = {}

    job = service.create_job("https://www.douyin.com/video/111", 1, str(tmp_path), False, wrap_folder=True)
    deadline = time.time() + 2
    while service.get_job(job.id).status in {"queued", "running"} and time.time() < deadline:
        time.sleep(0.02)

    finished = service.get_job(job.id)
    assert finished.output_dir == str(tmp_path)
    assert finished.wrap_folder is True
    assert finished.results[0]["output_dir"] == str(tmp_path)
    assert finished.results[0]["wrap_folder"] is True


def test_app_settings_persist_download_directory_and_wrap_flag(tmp_path, monkeypatch):
    import backend.services as services

    settings_file = tmp_path / "settings.json"
    download_dir = tmp_path / "media"
    monkeypatch.setattr(services, "SETTINGS_FILE", settings_file)

    saved = save_app_settings(str(download_dir), True)
    loaded = load_app_settings()

    assert saved.download_output_dir == str(download_dir)
    assert saved.wrap_download_folder is True
    assert loaded == saved
    assert download_dir.is_dir()


def test_downloader_output_dir_uses_root_unless_wrapped(tmp_path):
    downloader = load_module("test_douyin_downloader_tool", DOWNLOADER_DIR / "downloader.py")

    assert downloader._decide_output_dir(tmp_path, 1, "作品标题", "image", False) == tmp_path

    wrapped = downloader._decide_output_dir(tmp_path, 1, "作品标题", "image", True)
    assert wrapped == tmp_path / "作品标题"
    assert wrapped.is_dir()


def test_downloader_unwrapped_media_file_names_include_title(tmp_path):
    downloader = load_module("test_douyin_downloader_tool_for_names", DOWNLOADER_DIR / "downloader.py")

    unwrapped = downloader._media_file_path(tmp_path, tmp_path, "作品标题", 1, ".webp", False, width=3)
    wrapped = downloader._media_file_path(tmp_path / "作品标题", tmp_path, "作品标题", 1, ".webp", True, width=3)

    assert unwrapped == tmp_path / "作品标题_001.webp"
    assert wrapped == tmp_path / "作品标题" / "001.webp"


def test_download_preview_returns_selectable_url_items():
    service = DownloadService.__new__(DownloadService)
    service.module = object()
    service.jobs = {}

    preview = service.preview("https://www.douyin.com/video/111 https://www.douyin.com/video/222")

    assert [item["index"] for item in preview["items"]] == [1, 2]
    assert all(item["selected"] for item in preview["items"])
    assert preview["items"][0]["url"] == "https://www.douyin.com/video/111"


def test_extract_duration_from_nested_search_payload_matches_aweme_id():
    payload = {
        "router": {
            "loaderData": {
                "search-modal": {
                    "aweme_list": [
                        {
                            "aweme_id": "111",
                            "video": {"duration": 15000},
                        },
                        {
                            "aweme_id": "222",
                            "video": {"duration": 42000},
                        },
                    ]
                }
            }
        }
    }

    assert _extract_duration_from_payload(payload, "222") == 42


def test_extract_video_duration_waits_past_initial_one_second_metadata():
    class Page:
        def __init__(self):
            self.waited = False

        def evaluate(self, script):
            if "SSR_RENDER_DATA" in script:
                return {"direct": 0, "videoDuration": 1, "ssr": None}
            if "document.querySelector('video')" in script:
                return 1965
            raise AssertionError(f"Unexpected script: {script}")

        def wait_for_function(self, script, timeout=0):
            assert "video.duration > 2" in script
            self.waited = True

    page = Page()

    assert _extract_video_duration(page, "7627686789402103076") == 1965
    assert page.waited


def test_preview_video_duration_keeps_dom_seconds_over_1000():
    class Page:
        def evaluate(self, script):
            if "SSR_RENDER_DATA" in script:
                return {"direct": 0, "videoDuration": 1965, "ssr": None}
            raise AssertionError(f"Unexpected script: {script}")

    assert _extract_preview_video_duration(Page(), "7627686789402103076") == 1965


def test_profile_cache_upsert_persists_last_checked_at(tmp_path):
    cache_file = tmp_path / "profile_cache.json"
    sec_uid = "MS4wLjABAAAAExampleUser"

    upsert_profile_cache(
        cache_file,
        [
            {
                "sec_uid": sec_uid,
                "ok": True,
                "profile": {
                    "nickname": "测试用户",
                    "unique_id": "testid",
                    "follower_count": 12,
                    "following_count": 3,
                    "total_favorited": 45,
                    "ip_location": "IP属地：上海",
                    "live_status": 1,
                    "live_viewers": 99,
                },
            }
        ],
    )

    cache = read_profile_cache(cache_file)

    assert cache[sec_uid]["profile"]["nickname"] == "测试用户"
    assert cache[sec_uid]["profile"]["live_viewers"] == 99
    assert cache[sec_uid]["last_checked_at"]


def test_profile_cache_sets_first_seen_live_start(tmp_path):
    cache_file = tmp_path / "profile_cache.json"
    sec_uid = "MS4wLjABAAAALiveUser"

    upsert_profile_cache(
        cache_file,
        [
            {
                "sec_uid": sec_uid,
                "ok": True,
                "profile": {"live_status": 1, "live_viewers": 10},
            }
        ],
    )
    first = read_profile_cache(cache_file)[sec_uid]["profile"]["live_start_at"]
    upsert_profile_cache(
        cache_file,
        [
            {
                "sec_uid": sec_uid,
                "ok": True,
                "profile": {"live_status": 1, "live_viewers": 12},
            }
        ],
    )

    cache = read_profile_cache(cache_file)

    assert cache[sec_uid]["profile"]["live_start_at"] == first
    assert cache[sec_uid]["profile"]["live_duration_seconds"] >= 0


def test_profile_result_preserves_simplified_live_viewers():
    service = MonitorService.__new__(MonitorService)

    class FakeMonitor:
        @staticmethod
        def simplify(_info):
            raise AssertionError("simplified monitor result should not be simplified again")

    service.module = FakeMonitor()
    sec_uid = "MS4wLjABAAAALiveUser"

    result = service._to_profile_result(
        {"label": "直播用户", "url": f"https://www.douyin.com/user/{sec_uid}", "sec_uid": sec_uid},
        {
            "nickname": "直播用户",
            "sec_uid": sec_uid,
            "follower_count": 100,
            "following_count": 2,
            "total_favorited": 300,
            "live_status": 1,
            "room_id": 123,
            "live_viewers": 4567,
        },
    )

    assert result.ok is True
    assert result.profile["live_viewers"] == 4567
    assert result.profile["live_start_at"]
