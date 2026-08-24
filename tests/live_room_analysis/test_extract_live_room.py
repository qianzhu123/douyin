"""test_extract_live_room.py — 离线校验 extract_live_room._summarize 的归一逻辑。

不启动浏览器，直接喂 samples/ 里的真实接口 JSON，断言关键字段被正确抽出。
运行： pytest -q tests/live_room_analysis/test_extract_live_room.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from extract_live_room import _summarize  # noqa: E402


def _load(name: str):
    p = Path(__file__).resolve().parent / "samples" / name
    return json.loads(p.read_text(encoding="utf-8"))


def _captured_from_samples():
    """从样本重建 captured dict（与浏览器拦到的结构一致）。"""
    enter_full = _load("dy_enter_3587.json")          # 带壳 {url,responseBody,...}
    enter = json.loads(enter_full["responseBody"]["text"])
    ranklist = _load("dy_ranklist_now.json")          # 直接是顶层 body
    wish = _load("dy_wish_3599.json")
    gift = _load("dy_giftlist_3487.json")
    return {"enter": enter, "ranklist": ranklist, "wish": wish, "gift": gift}


def test_core_room_fields():
    s = _summarize(_captured_from_samples(), "31126587860", {
        "nickname": "歌手刘筝", "metaDescription": "",
        "playRoom": "7664058258907188031,7664058258907188031", "webcastUid": "",
        "infoBar": "",
    })
    assert s["web_rid"] == "31126587860"
    assert s["room_id_str"] == "7664058258907188031"
    assert s["status"] == 2                                 # 直播中
    assert s["like_count"] == 62610
    assert s["has_commerce_goods"] is True                  # 带橱窗
    assert s["anchor"]["uid"] == "95052337920"
    assert s["anchor"]["sec_uid"] and s["anchor"]["sec_uid"].startswith("MS4w")
    assert s["room_view_stats"]["display_value"] == 1218
    assert s["partition"]["id_str"] == "10000"
    assert len(s["similar_rooms"]) >= 3
    assert {"ranklist", "enter", "wish", "gift"} <= set(s["source_capture_keys"])


def test_audience_rank_top():
    s = _summarize(_captured_from_samples(), "31126587860", {
        "nickname": "", "metaDescription": "", "playRoom": "", "webcastUid": "", "infoBar": "",
    })
    assert "audience_rank_top" in s
    top = s["audience_rank_top"]
    assert len(top) == 20
    r1 = top[0]
    assert r1["rank"] == 1
    assert r1["pay_grade_level"] == 29
    assert r1["fans_club_level"] == 16
    assert r1["sec_uid"].startswith("MS4w")
    assert s["audience_rank_meta"]["has_more"] is False


def test_wish_and_fallback():
    s = _summarize(_captured_from_samples(), "31126587860", {
        "nickname": "歌手刘筝", "metaDescription": "x", "playRoom": "x",
        "webcastUid": "y", "infoBar": "z",
    })
    assert "wish" in s
    assert s["dom_fallback"]["nickname"] == "歌手刘筝"
    assert s["dom_fallback"]["playRoom"] == "x"
