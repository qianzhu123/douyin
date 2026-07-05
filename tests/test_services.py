from backend.config import DOWNLOAD_OUTPUT_DIR
from backend.services import (
    DownloadService,
    MonitorService,
    extract_douyin_urls,
    parse_search_candidates,
    parse_search_summaries,
    read_profile_cache,
    read_project_users,
    delete_project_user,
    reorder_project_users,
    upsert_profile_cache,
    upsert_project_user,
)
from backend.tool_loader import load_module
from pathlib import Path
import os
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


def test_monitor_settings_parse_labels_and_urls():
    monitor_dir = os.getenv("DOUYIN_MONITOR_DIR")
    if not monitor_dir:
        pytest.skip("DOUYIN_MONITOR_DIR is not configured")
    module = load_module("test_douyin_monitor_tool", Path(monitor_dir) / "main.py")

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


def test_default_download_output_is_project_output():
    assert DOWNLOAD_OUTPUT_DIR.name == "output"


def test_download_job_runs_from_sync_context_and_records_logs():
    class FakeDownloader:
        @staticmethod
        def download_douyin(url, output_dir, mode, fetch_comments=False):
            return {"type": "video", "title": "demo", "url": url, "output_dir": output_dir, "mode": mode}

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
    assert any("Queued" in entry["message"] for entry in finished.logs)
    assert any("Finished" in entry["message"] for entry in finished.logs)


def test_download_preview_returns_selectable_url_items():
    service = DownloadService.__new__(DownloadService)
    service.module = object()
    service.jobs = {}

    preview = service.preview("https://www.douyin.com/video/111 https://www.douyin.com/video/222")

    assert [item["index"] for item in preview["items"]] == [1, 2]
    assert all(item["selected"] for item in preview["items"])
    assert preview["items"][0]["url"] == "https://www.douyin.com/video/111"


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
