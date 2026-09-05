"""test_live_room.py — 离线校验 backend.live_room._summarize（服务层裁剪版）。

与 tests/live_room_analysis/test_extract_live_room.py 互补：后者验证原始
extract_live_room._summarize 全量归一；本测试只针对服务层用到的
backend/live_room._summarize，断言：
- Top10 观众榜截断（service 固定截 10，区别于 tests 那份的 20）
- 直播间卡字段齐全
- 未直播分支（status != 2 仍能归一，但调用方按 live_status==1 才展示）
- 空暴/无房间数 → None

不启动浏览器，纯离线喂样本。
运行： pytest -q tests/test_live_room.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.live_room import summarize_for_tests, AUDIENCE_RANK_LIMIT, _web_rid_from_profile, _summarize_fansclub  # noqa: E402

_SAMPLES = Path(__file__).resolve().parent / "live_room_analysis" / "samples"


def _load(name: str) -> dict:
    return json.loads((_SAMPLES / name).read_text(encoding="utf-8"))


def _captured():
    """复刻浏览器拦到的 captured dict 结构（enter 取壳里的 responseBody）。"""
    enter_full = _load("dy_enter_3587.json")
    enter = json.loads(enter_full["responseBody"]["text"])
    ranklist = _load("dy_ranklist_now.json")
    return {"enter": enter, "ranklist": ranklist}


def test_service_truncates_audience_to_top10():
    assert AUDIENCE_RANK_LIMIT == 10
    card = summarize_for_tests(_captured(), web_rid="31126587860")
    assert card is not None
    assert len(card["audience_rank_top"]) == AUDIENCE_RANK_LIMIT
    assert card["audience_rank_top"][0]["rank"] == 1


def test_live_room_card_fields_complete():
    card = summarize_for_tests(_captured(), web_rid="31126587860")
    assert card is not None
    expected = {
        "web_rid", "room_id_str", "status", "title", "user_count_str",
        "viewers", "total_user_str", "like_count", "partition",
        "similar_rooms", "audience_rank_top", "audience_rank_meta",
        "qrcode_url", "anchor", "stream_url",
    }
    assert expected <= set(card.keys())
    assert card["web_rid"] == "31126587860"
    assert card["room_id_str"] == "7664058258907188031"
    assert card["status"] == 2
    assert card["viewers"] == 1218
    assert card["like_count"] == 62610
    assert card["anchor"]["uid"] == "95052337920"
    # 类似直播间最多 3 条
    assert len(card["similar_rooms"]) <= 3


def test_empty_enter_returns_none():
    card = summarize_for_tests({"enter": {"data": {"data": []}}, "ranklist": {}}, web_rid="x")
    assert card is None


def test_missing_ranklist_still_summarizes():
    captured = {"enter": _captured()["enter"]}  # 无 ranklist
    card = summarize_for_tests(captured, web_rid="31126587860")
    assert card is not None
    assert card["audience_rank_top"] == []
    assert card["audience_rank_meta"]["total"] is None


def test_web_rid_extracted_from_homepage_room_data():
    """主页 profile/other 在直播中 room_data(JSON 串)含 web_rid，验证能换出来。"""
    import json
    room_data = {"web_rid": "31126587860", "room_id_str": "7664058258907188031", "status": 2}
    payload = {"user": {"live_status": 1, "room_data": json.dumps(room_data, ensure_ascii=False)}}
    assert _web_rid_from_profile(payload) == "31126587860"


def test_web_rid_handles_dict_room_data_and_MISSING():
    assert _web_rid_from_profile({"user": {"room_data": {"webRid": "9988"}}}) == "9988"
    assert _web_rid_from_profile({"user": {"room_data": None}}) == ""
    assert _web_rid_from_profile({"user": {"room_data": "not-json"}}) == ""
    assert _web_rid_from_profile({"user": {"room_data": {"owner": {"rid": "55667"}}}}) == "55667"


def test_summarize_fansclub_extracts_club_grade():
    """fansclub/homepage.club_info.club_level_info 才是真正的 anchor 团等级。

    max_level=20 是系统硬顶（实测所有 anchor 都 20），club_level 是请求者自己
    在团里的等级（未加入=0）。两者都不能区分 anchor。真实可比的"团等级"在
    club_level_info.level (0-9) 里，配合 club_level_info.max_level/cur_value/
    season_id 一起读。"""
    payload = {
        "data": {
            "status_code": 0,
            "club_info": {
                "anchor_name": "吕德华",
                "anchor_id": "3597225254730516",
                "max_level": 20,
                "club_level": 0,
                "total_fans_count": 2559410,
                "active_fans_count": 81434,
                "today_new_fans_count": 2,
                "num_of_fans_group": 6,
                "popularity_rank_hour": 0,
                "club_level_info": {
                    "level": 9,
                    "max_level": 9,
                    "cur_value": 469487,
                    "season_id": 4,
                    "icon": "...",
                },
            },
            "extra": {"fandom_id": "24944082228738"},
        }
    }
    out = _summarize_fansclub(payload["data"])
    # 真团等级（区别于系统硬顶 20 和用户自己等级 0）
    assert out["club_grade"] == 9
    assert out["club_grade_max"] == 9
    assert out["club_exp"] == 469487
    assert out["club_season"] == 4
    # 不应丢其它字段
    assert out["club_name"] == "吕德华"
    assert out["total_fans_count"] == 2559410
    assert out["active_fans_count"] == 81434
    assert out["num_of_fans_group"] == 6
    assert out["fandom_id"] == "24944082228738"


def test_summarize_fansclub_no_club_level_info_defaults_safely():
    """未开通粉丝团/老版本响应里 club_level_info 缺失时不应崩，给默认值。"""
    payload = {
        "data": {
            "status_code": 0,
            "club_info": {
                "anchor_name": "空团",
                "anchor_id": "111",
                "max_level": 20,
                "club_level": 0,
            },
        }
    }
    out = _summarize_fansclub(payload["data"])
    assert out["club_grade"] == 0
    assert out["club_grade_max"] == 9  # 默认 9
    assert out["club_exp"] == 0
    assert out["club_season"] == 0


def test_summarize_fansclub_empty_returns_empty():
    """{} / 非 dict 都应安全返回 {}（未登录态的兜底）。"""
    assert _summarize_fansclub({}) == {}
    assert _summarize_fansclub(None) == {}
    assert _summarize_fansclub("") == {}
