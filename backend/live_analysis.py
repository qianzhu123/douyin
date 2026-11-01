"""backend/live_analysis.py — 直播间 PC 直连分析 (两头配方 2.0, 只读).

配方来自 douyin-re-lab §81/§83 (tools/live_room.py 验证): X-Tt-Token +
sdk-version:2 + 真机全参数 query 即通 webcast 读域. token 复用
data/fansclub_tokens/ 同一批账户.

端点 (全部只读; gift/send 及消耗钻石端点永不触碰):
- POST /webcast/room/enter/                     房间全量 (标题/观众/点赞/流/统计)
- GET  /webcast/fansclub/homepage/              粉丝团面板 (活跃/总粉/今日新增)
- GET  /webcast/fansclub/check_in_rank_homepage/  签到双榜 + self
- GET  /webcast/luckybox/box/list/              福袋列表 (免费活动, 只读)
- POST /webcast/room/leave/                     礼貌离房
"""
from __future__ import annotations

import json
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .config import DATA_DIR
from .fansclub import load_tokens

BASE = "https://webcast.amemv.com"
UA = ("com.ss.android.ugc.aweme/400601 (Linux; U; Android 11; zh_CN; "
      "Pixel 2 XL; Build/RP1A.201005.004.A1)")
DEV = dict(device_id="5967213509056", iid="1765185923070852",
           cdid="61eafaf6-6a6b-4423-bf00-bfa1b53b1fb5")
# 与抓 token 真机一致的用户身份 (dy1); 换账户时一并换
USER_ID = "3079066458797438"
SEC_UID = "MS4wLjABAAAA2_RT_u21n0XtG9wTqtcRSOnBPh495b9GTncIAZEZCsZ4nNAWDm_ngtrJ_vJEP2BP"

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

# 同房间 enter 节流: 10 分钟内不重复进 (礼貌 + 防风控)
_enter_history: dict[str, float] = {}
_history_lock = threading.Lock()
ENTER_COOLDOWN_S = 600


def common_q(**extra) -> dict:
    """真机 webcast 请求全参数形状 (§81; 缺任一项部分端点拒答)."""
    q = dict(aid="1128", app_name="aweme", version_code="400600", device_platform="android",
             channel="huawei_1128_64", device_type="Pixel 2 XL", device_brand="google",
             os_version="11", os_api="30", **DEV, ts=int(time.time()),
             webcast_sdk_version="4470", live_sdk_version="400601", gamecp_sdk_version="4470",
             webcast_language="zh", webcast_locale="zh_CN", webcast_gps_access="2",
             device_score="7.7856", address_book_access="2", user_id=USER_ID,
             sec_user_id=SEC_UID, is_pad="false", is_android_pad="0", is_landscape="false",
             carrier_region="CN", ac="wifi", language="zh", os="android", ssmix="a",
             resolution="1440*2712", dpi="560", update_version_code="40609900",
             _rticket=str(int(time.time() * 1000)), package="com.ss.android.ugc.aweme",
             manifest_version_code="400601", version_name="40.6.0",
             first_launch_timestamp="0", last_deeplink_update_version_code="40609900",
             cpu_support64="true", host_abi="arm64-v8a", is_guest_mode="false",
             app_type="normal", minor_status="0", appTheme="0", is_preinstall="false",
             need_personal_recommend="true", is_android_fold="false")
    q.update(extra)
    return q


def _http(method: str, path: str, q: dict, tok: str, timeout: float = 15.0) -> dict:
    url = BASE + path + "?" + urllib.parse.urlencode(q)
    headers = {"X-Tt-Token": tok, "sdk-version": "2", "User-Agent": UA}
    data = b"{}" if method == "POST" else None
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, headers=headers, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()
    if not body:
        return {}
    try:
        return json.loads(body)
    except Exception:
        return {}


def _pick_token(label: str | None) -> str:
    accounts = load_tokens()
    if not accounts:
        raise RuntimeError("no fansclub tokens; 先在粉丝团面板配置凭证")
    if label:
        for a in accounts:
            if a["label"] == label:
                return a["token"]
        raise RuntimeError(f"no token for label: {label}")
    return accounts[0]["token"]  # 默认 dy1


def _room_brief(room: dict) -> dict:
    owner = room.get("owner") or {}
    st = room.get("stats") or {}
    rvs = room.get("room_view_stats") or {}
    return {
        "room_id": room.get("id_str"),
        "title": room.get("title"),
        "nickname": owner.get("nickname"),
        "anchor_id": owner.get("id_str"),
        "sec_uid": owner.get("sec_uid"),
        "status": room.get("status"),
        "viewers": rvs.get("display_value"),
        "total_user": st.get("total_user"),
        "like_count": room.get("like_count"),
        "follow_gained": st.get("follow_count"),
        "start_time": room.get("start_time"),
        "gift_disabled": bool((room.get("im_info") or {}).get("hide_gift_message")),
        "has_goods": bool(room.get("has_commerce_goods")),
    }


def analyze_room(room_id: str, anchor_id: str | None = None, label: str | None = None) -> dict:
    """进房拉全量 + 粉丝团面板 + 签到榜 + 福袋 (只读, 全失败也不抛)."""
    tok = _pick_token(label)
    out: dict = {"room_id": room_id, "ok": False}
    # 节流提示 (仍执行, 但标注近期已进过)
    now = time.time()
    with _history_lock:
        last = _enter_history.get(room_id, 0)
        _enter_history[room_id] = now
        out["entered_recently"] = (now - last) < ENTER_COOLDOWN_S
    q = common_q(room_id=room_id, anchor_id=anchor_id or "0", enter_type=0, mean_type=1,
                 room_tab="sale_hot", enter_from_merge="webcast_feed", live_id=1,
                 request_tag="from", action_type="click")
    ent = _http("POST", "/webcast/room/enter/", q, tok)
    if ent.get("status_code") != 0:
        out["error"] = f"enter sc={ent.get('status_code')} {str(ent.get('status_msg') or '')[:60]}"
        return out
    room = ent.get("data") or {}
    brief = _room_brief(room)
    aid = anchor_id or brief.get("anchor_id") or "0"
    out.update(ok=True, room=brief)

    time.sleep(1.5)
    hp = _http("GET", "/webcast/fansclub/homepage/",
               common_q(anchor_id=aid, request_scene=1, action=1, room_id=0, source=16), tok)
    ci = (hp.get("data") or {}).get("club_info") or {}
    out["fansclub"] = {
        "anchor_name": ci.get("anchor_name"),
        "active_fans": ci.get("active_fans_count"),
        "total_fans": ci.get("total_fans_count"),
        "today_new_fans": ci.get("today_new_fans_count"),
        "max_level": ci.get("max_level"),
        "fans_name": ci.get("fans_name"),
    }

    time.sleep(1.5)
    rk = _http("GET", "/webcast/fansclub/check_in_rank_homepage/",
               common_q(room_id="0", anchor_id=aid, rank_type=0), tok)
    rd = rk.get("data") or {}
    si = rd.get("self_item") or {}
    out["checkin_rank"] = {
        "my_score": si.get("score"),
        "my_rank": si.get("rank"),
        "my_desc": si.get("desc"),
        "top": [
            {"rank": it.get("rank"), "score": it.get("score")}
            for it in (rd.get("item_list") or [])[:5]
        ],
        "history": [
            {"date": it.get("timestamp"), "rank": it.get("rank"), "checkins": it.get("check_in_count")}
            for it in ((rd.get("record") or {}).get("item_list") or [])[:7]
        ],
    }

    time.sleep(1.5)
    lb = _http("GET", "/webcast/luckybox/box/list/", common_q(room_id=room_id), tok)
    boxes = lb.get("data")
    out["luckybox"] = {"count": len(boxes) if isinstance(boxes, list) else 0,
                       "boxes": boxes if isinstance(boxes, list) else []}

    time.sleep(1.2)
    ql = common_q(room_id=room_id, room_id_str=room_id, leave_type=0, reason=2)
    lv = _http("POST", "/webcast/room/leave/", ql, tok)
    out["leave_sc"] = lv.get("status_code")
    return out
