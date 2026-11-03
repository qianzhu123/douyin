"""backend/user_lists.py — 用户关注/粉丝列表 (只读, lab 两头配方直连).

端点: GET /aweme/v1/user/following/list/ + /aweme/v1/user/follower/list/
(aweme.snssdk.com, Cookie + x-tt-ttnet-origin-host, §73 配方)。
与 comment/list 同族读端点; 若被静默掐, 报 error 不降浏览器
(关注/粉丝列表在 web 端折叠很深, 页内滚动收益低, 直连空即空)。
"""
from __future__ import annotations

import json
import ssl
import threading
import time
import urllib.parse
import urllib.request

_history: dict[str, float] = {}
_history_lock = threading.Lock()
_THROTTLE_S = 60

MAX_PAGES = 10
PAGE_SIZE = 20


def _simplify_brief(u: dict) -> dict:
    """user_list 项 → 精简卡片."""
    return {
        "uid": str(u.get("uid") or ""),
        "sec_uid": str(u.get("sec_uid") or ""),
        "nickname": u.get("nickname") or "",
        "unique_id": u.get("unique_id") or "",
        "signature": (u.get("signature") or "").strip()[:100],
        "follower_count": u.get("follower_count") or 0,
        "total_favorited": u.get("total_favorited") or 0,
        "avatar": ((u.get("avatar_thumb") or {}).get("url_list") or [""])[0],
        "homepage": f"https://www.douyin.com/user/{u.get('sec_uid') or ''}",
    }


def _req_direct_lab(path: str, params: dict, timeout: float = 15.0) -> dict:
    """§73 lab 配方直连 (aweme.snssdk.com)."""
    from pathlib import Path as _Path
    ck_file = _Path(r"D:\code\android\androidre\douyin\tools\real_cookie.txt")
    if not ck_file.exists():
        return {"status_code": -2, "status_msg": "no lab cookie file"}
    ck = ck_file.read_text(encoding="utf-8").strip().replace("\r\n", "; ")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    q = {
        "version_code": 400600,
        "app_name": "aweme",
        "aid": 1128,
        "device_platform": "android",
        "channel": "huawei_1128_64",
        "device_type": "Pixel 2 XL",
        "device_brand": "google",
        "os_version": 11,
        "os_api": 30,
        "ts": int(time.time()),
        "device_id": "5967213509056",
        "iid": "1765185923070852",
        "cdid": "61eafaf6-6a6b-4423-bf00-bfa1b53b1fb5",
        **params,
    }
    url = "https://aweme.snssdk.com" + path + "?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={
        "Cookie": ck,
        "x-tt-ttnet-origin-host": "api6-normal-m-hj.amemv.com",
        "User-Agent": ("com.ss.android.ugc.aweme/400601 (Linux; U; Android 11; "
                       "zh_CN; Pixel 2 XL; Build/RP1A.201005.004.A1)"),
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        return {"status_code": -3, "status_msg": f"{type(exc).__name__}: {exc}"[:80]}


def list_user_list(sec_uid: str, kind: str, *, max_count: int = 100) -> dict:
    """拉取关注 (kind=following) 或粉丝 (kind=follower) 列表."""
    sec_uid = sec_uid.strip()
    if not sec_uid:
        raise ValueError("sec_uid required")
    if kind not in ("following", "follower"):
        raise ValueError("kind must be following|follower")

    now = time.time()
    key = f"{kind}:{sec_uid}"
    with _history_lock:
        last = _history.get(key, 0)
        _history[key] = now
        throttled = (now - last) < _THROTTLE_S

    out: dict = {"sec_uid": sec_uid, "kind": kind, "ok": False, "users": [],
                 "throttled_recently": throttled}
    seen: set[str] = set()
    # follower/following 列表端点 cursor 形状: offset 累加 (每页 count)
    offset, pages = 0, 0
    target_pages = max(1, min((max_count + PAGE_SIZE - 1) // PAGE_SIZE, MAX_PAGES))
    for _ in range(target_pages):
        data = _req_direct_lab(f"/aweme/v1/user/{kind}/list/", {
            "sec_user_id": sec_uid,
            "offset": offset,
            "count": PAGE_SIZE,
            "source_type": 4,
            "gps_access": 0,
        })
        sc = data.get("status_code")
        if sc not in (0, None):
            out["error"] = f"sc={sc} {str(data.get('status_msg') or '')[:60]}"
            break
        items = data.get("followings") or data.get("followers") or []
        got_new = 0
        for item in items:
            if isinstance(item, dict):
                su = str(item.get("sec_uid") or item.get("uid") or "")
                if su and su not in seen:
                    seen.add(su)
                    out["users"].append(_simplify_brief(item))
                    got_new += 1
        pages += 1
        out["pages"] = pages
        total = data.get("total") or 0
        if total:
            out["server_total"] = total
        has_more = data.get("has_more")
        if not got_new or has_more in (0, False, None) and not has_more:
            break
        offset += PAGE_SIZE
        time.sleep(0.8)

    out["ok"] = bool(out["users"])
    out["total"] = len(out["users"])
    return out
