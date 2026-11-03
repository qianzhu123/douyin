"""backend/comments.py — 作品评论采集 (只读, 多协议降级链).

端点: GET /aweme/v2/comment/list/ (在 metasec 签名白名单, docs/06 §5)。
aweme/post 2026-11 被静默掐的先例 → 直连大概率同样空, 但先试再降级。

① 直连: §73 两头配方 (Cookie + x-tt-ttnet-origin-host, aweme.snssdk.com)
② 浏览器拦截: monitor 页面池开作品详情页, 拦 comment/list XHR,
   滚动评论区触发翻页 (cursor 由页面自己带)。
"""
from __future__ import annotations

import json
import ssl
import threading
import time
import urllib.parse
import urllib.request

# 同 aweme_id 评论拉取节流
_history: dict[str, float] = {}
_history_lock = threading.Lock()
_THROTTLE_S = 60

MAX_PAGES = 10
PAGE_SIZE = 20


def simplify_comment(c: dict) -> dict:
    """评论项 → 精简卡片 (含二级回复数, 回复正文按需拉取)."""
    user = c.get("user") or {}
    return {
        "cid": str(c.get("cid") or ""),
        "text": (c.get("text") or "").strip(),
        "create_time": c.get("create_time") or 0,
        "digg_count": c.get("digg_count") or 0,
        "reply_comment_total": c.get("reply_comment_total") or 0,
        "user_nickname": user.get("nickname") or "",
        "user_sec_uid": user.get("sec_uid") or "",
        "ip_label": c.get("ip_label") or "",
    }


def _req_direct_lab(aweme_id: str, cursor: int, count: int,
                    timeout: float = 15.0) -> dict:
    """§73 lab 配方直连 comment/list."""
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
        "aweme_id": aweme_id,
        "cursor": cursor,
        "count": count,
        "item_id": aweme_id,
    }
    url = ("https://aweme.snssdk.com/aweme/v2/comment/list/?"
           + urllib.parse.urlencode(q))
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


def _list_comments_browser(aweme_id: str, *, max_count: int = 100) -> dict:
    """通道B: monitor 页面池开作品页, 拦 comment/list XHR, 滚动翻页."""
    from .tool_loader import load_module
    from .config import MONITOR_DIR

    out: dict = {"aweme_id": aweme_id, "ok": False, "comments": [], "error": ""}
    seen: set[str] = set()
    batches: list[list[dict]] = []

    async def _run():
        mod = load_module("douyin_monitor_tool", MONITOR_DIR / "main.py")
        context = await mod.init_browser()
        page = await context.new_page()

        async def on_response(response):
            try:
                if "/aweme/v2/comment/list/" not in response.url:
                    return
                body = await response.json()
                items = body.get("comments") or []
                if items:
                    batches.append(items)
            except Exception:
                pass

        page.on("response", on_response)
        try:
            # /video/ 与 /note/ 都走同一评论区; 先 video, 页面自会重定向 note
            for path_form in (f"https://www.douyin.com/video/{aweme_id}",
                              f"https://www.douyin.com/note/{aweme_id}"):
                try:
                    await page.goto(path_form, wait_until="domcontentloaded",
                                    timeout=25000)
                    break
                except Exception:
                    continue
            await page.wait_for_timeout(3500)
            scrolls = max(2, min(max_count // PAGE_SIZE + 2, 12))
            for i in range(scrolls):
                n_before = sum(len(b) for b in batches)
                await page.mouse.wheel(0, 5000)
                await page.wait_for_timeout(1800)
                n_now = sum(len(b) for b in batches)
                if n_now >= max_count:
                    break
                if n_now == n_before and i > 2:
                    break
        finally:
            try:
                await page.close()
            except Exception:
                pass

    import asyncio as _aio
    import concurrent.futures as _cf

    def _bridge():
        try:
            _aio.get_running_loop()
        except RuntimeError:
            return _aio.run(_run())
        with _cf.ThreadPoolExecutor(1) as ex:
            return ex.submit(lambda: _aio.run(_run())).result(timeout=180)

    _bridge()

    for batch in batches:
        for item in batch:
            if isinstance(item, dict):
                cid = str(item.get("cid") or "")
                if cid and cid not in seen:
                    seen.add(cid)
                    out["comments"].append(simplify_comment(item))
    out["ok"] = bool(out["comments"])
    out["total"] = len(out["comments"])
    return out


def list_comments(aweme_id: str, *, max_count: int = 100) -> dict:
    """拉取作品评论. 降级链: ① lab 直连 → ② 浏览器拦截."""
    aweme_id = str(aweme_id).strip()
    if not aweme_id:
        raise ValueError("aweme_id required")

    now = time.time()
    with _history_lock:
        last = _history.get(aweme_id, 0)
        _history[aweme_id] = now
        throttled = (now - last) < _THROTTLE_S

    # ① 直连
    out: dict = {"aweme_id": aweme_id, "ok": False, "comments": [],
                 "throttled_recently": throttled}
    seen: set[str] = set()
    cursor, pages = 0, 0
    direct_blocked = False
    for _ in range(MAX_PAGES):
        data = _req_direct_lab(aweme_id, cursor, PAGE_SIZE)
        sc = data.get("status_code")
        if sc not in (0, None):
            out["direct_error"] = f"sc={sc} {str(data.get('status_msg') or '')[:60]}"
            direct_blocked = True
            break
        items = data.get("comments") or []
        got_new = False
        for item in items:
            if isinstance(item, dict):
                cid = str(item.get("cid") or "")
                if cid and cid not in seen:
                    seen.add(cid)
                    out["comments"].append(simplify_comment(item))
                    got_new = True
        pages += 1
        if not got_new or not data.get("has_more"):
            break
        cursor = data.get("cursor") or 0
        time.sleep(0.8)

    if out["comments"]:
        out["ok"] = True
        out["via"] = "direct"
        out["total"] = len(out["comments"])
        return out

    # ② 浏览器
    browser_result = _list_comments_browser(aweme_id, max_count=max_count)
    browser_result["throttled_recently"] = throttled
    browser_result["direct_error"] = out.get("direct_error") or (
        "direct empty (silently blocked)" if not direct_blocked else "")
    browser_result["via"] = "browser"
    return browser_result
