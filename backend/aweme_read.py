"""backend/aweme_read.py — 用户作品列表 (只读, 多协议降级链).

① 直连: §73 两头配方 (Cookie + x-tt-ttnet-origin-host, aweme.snssdk.com,
   /aweme/v1/aweme/post/)。2026-11-03 实测被静默掐 (sc=0 空列表全账户一致,
   连 lab dy_analyze.py 同样空; profile/other 仍全量正常) — 保留作第一层,
   若抖音放开即自动恢复。Cookie 来自 lab 仓 tools/real_cookie.txt (真机抓包)。
② 浏览器拦截: monitor 页面池 (external/douyin-monitor init_browser) 开用户
   主页, 拦 /aweme/v1/web/aweme/post/ XHR, 滚动加载翻页。与 fetch_profile
   同池同 lifecycle, 不新增常驻浏览器进程。
"""
from __future__ import annotations

import json
import ssl
import threading
import time
import urllib.parse
import urllib.request

# 同 sec_uid 作品列表拉取节流 (礼貌 + 防风控): 60s 内不重复翻全量
_history: dict[str, float] = {}
_history_lock = threading.Lock()
_THROTTLE_S = 60

# 单次翻页上限 (安全阀, 防止 cursor 死循环把账号刷爆)
MAX_PAGES = 20
PAGE_SIZE = 10


def simplify_aweme(item: dict) -> dict:
    """作品列表项 → 精简卡片 (下载器可直接吃 aweme_id)."""
    video = item.get("video") or {}
    cover = (video.get("cover") or {}).get("url_list") or []
    stats = item.get("statistics") or {}
    author = item.get("author") or {}
    return {
        "aweme_id": str(item.get("aweme_id") or ""),
        "desc": (item.get("desc") or "").strip(),
        "create_time": item.get("create_time") or 0,
        "duration_ms": video.get("duration") or 0,
        "cover": cover[0] if cover else "",
        "is_image_post": bool(item.get("images")),
        "image_count": len(item.get("images") or []),
        "digg_count": stats.get("digg_count") or 0,
        "comment_count": stats.get("comment_count") or 0,
        "collect_count": stats.get("collect_count") or 0,
        "share_count": stats.get("share_count") or 0,
        "page_url": f"https://www.douyin.com/video/{item.get('aweme_id')}",
        "author_nickname": author.get("nickname") or "",
    }


def list_posts(sec_uid: str, *, max_count: int = 100, label: str | None = None,
               fetch_all: bool = False) -> dict:
    """拉取用户作品列表. 多协议降级链:

    ① 直连 (两头配方 Cookie + origin-host, §73): sc=0 但 2026-11 起被
       静默掐数据 (aweme_list 空, 全账户一致) — 仍保留, 万一放开即恢复.
    ② 浏览器拦截 (monitor 页面池开主页, 拦 aweme/post XHR): 抖音自己
       签好的请求, 风控最松; 翻页靠页内滚动加载.
    """
    sec_uid = sec_uid.strip()
    if not sec_uid:
        raise ValueError("sec_uid required")
    result = _list_posts_direct_lab(sec_uid, max_count=max_count)
    if result.get("posts"):
        result["via"] = "direct"
        return result
    browser_result = _list_posts_browser(sec_uid, max_count=max_count)
    browser_result["direct_error"] = result.get("error") or (
        "direct empty (silently blocked)" if result.get("pages") == 0 else "direct empty")
    browser_result["via"] = "browser"
    return browser_result


def _list_posts_direct_lab(sec_uid: str, *, max_count: int = 100) -> dict:
    """通道A: §73 lab 配方直连 (Cookie + x-tt-ttnet-origin-host), cursor 翻页."""
    if not sec_uid:
        raise ValueError("sec_uid required")

    out: dict = {"sec_uid": sec_uid, "ok": False, "posts": [], "pages": 0}
    now = time.time()
    with _history_lock:
        last = _history.get(sec_uid, 0)
        _history[sec_uid] = now
        out["throttled_recently"] = (now - last) < _THROTTLE_S

    cursor = 0
    target_pages = max(1, min((max_count + PAGE_SIZE - 1) // PAGE_SIZE, MAX_PAGES))
    for _ in range(target_pages):
        data = _req_direct_lab("GET", "/aweme/v1/aweme/post/", {
            "sec_user_id": sec_uid,
            "count": PAGE_SIZE,
            "max_cursor": cursor,
            "source": 0,
            "user_avatar_shrink": "144_144",
            "locate_query": "false",
            "show_live_replay_strategy": 1,
            "need_capture_scenes": 0,
            "need_list": 0,
        })
        if data.get("status_code") not in (0, None):
            out["error"] = f"sc={data.get('status_code')} {str(data.get('status_msg') or '')[:60]}"
            break
        aweme_list = data.get("aweme_list") or []
        for item in aweme_list:
            if isinstance(item, dict):
                out["posts"].append(simplify_aweme(item))
        out["pages"] += 1
        if not data.get("has_more"):
            out["has_more_exhausted"] = True
            break
        cursor = data.get("max_cursor") or 0
        time.sleep(0.8)  # 翻页间隔, 礼貌限速

    out["ok"] = bool(out["posts"])
    out["total"] = len(out["posts"])
    return out


def _req_direct_lab(method: str, path: str, params: dict, timeout: float = 15.0) -> dict:
    """§73 lab 配方直连: aweme.snssdk.com + Cookie + x-tt-ttnet-origin-host.

    Cookie 来源: lab 仓 tools/real_cookie.txt (真机抓包). profile/other 仍全量
    正常, aweme/post 2026-11 起被静默掐 (sc=0 空列表) — 保留作降级链第一层.
    """
    import ssl as _ssl
    from pathlib import Path as _Path
    ck_file = _Path(r"D:\code\android\androidre\douyin\tools\real_cookie.txt")
    if not ck_file.exists():
        return {"status_code": -2, "status_msg": "no lab cookie file"}
    ck = ck_file.read_text(encoding="utf-8").strip().replace("\r\n", "; ")
    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    q = {
        "publish_video_strategy_type": 2,
        "version_code": 400600,
        "app_name": "aweme",
        "aid": 1128,
        "device_platform": "android",
        "channel": "huawei_1128_64",
        "device_type": "Pixel 2 XL",
        "device_brand": "google",
        "os_version": 11,
        "os_api": 30,
        "device_brand2": "google",
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


def _list_posts_browser(sec_uid: str, *, max_count: int = 100) -> dict:
    """通道B: monitor 页面池开主页, 拦 aweme/post XHR, 滚动加载翻页.

    复用 external/douyin-monitor 的浏览器基建 (init_browser + 页面池),
    与 fetch_profile 同池同 lifecycle — 不新增常驻浏览器.
    注意: response.json() 必须在页面存活期内读, 关页后 body 失效.
    """
    import asyncio
    from .tool_loader import load_module
    from .config import MONITOR_DIR

    out: dict = {"sec_uid": sec_uid, "ok": False, "posts": [], "pages": 0,
                 "error": ""}
    seen: set[str] = set()
    captured_batches: list[list[dict]] = []

    async def _run():
        mod = load_module("douyin_monitor_tool", MONITOR_DIR / "main.py")
        context = await mod.init_browser()
        page = await context.new_page()

        async def on_response(response):
            try:
                if "/aweme/v1/web/aweme/post/" not in response.url:
                    return
                body = await response.json()
                items = body.get("aweme_list") or []
                if items:
                    captured_batches.append(items)
            except Exception:
                pass

        page.on("response", on_response)
        try:
            await page.goto(f"https://www.douyin.com/user/{sec_uid}",
                            wait_until="domcontentloaded", timeout=25000)
            await page.wait_for_timeout(3000)
            scrolls = max(2, min(max_count // 10 + 2, 15))
            for i in range(scrolls):
                n_before = sum(len(b) for b in captured_batches)
                await page.mouse.wheel(0, 6000)
                await page.wait_for_timeout(1800)
                n_now = sum(len(b) for b in captured_batches)
                if n_now >= max_count:
                    break
                if n_now == n_before and i > 2:
                    break
        finally:
            try:
                await page.close()
            except Exception:
                pass

    # FastAPI 端点在此走 asyncio.to_thread (无 running loop) → asyncio.run 即可;
    # 保险起见若恰有 running loop (如被 async 直接调用), 落子线程独立 loop,
    # 因为 monitor 的 init_browser 状态是全局的, 不能嵌套两个 loop 争抢.
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

    for batch in captured_batches:
        out["pages"] += 1
        for item in batch:
            if isinstance(item, dict):
                aid = str(item.get("aweme_id") or "")
                if aid and aid not in seen:
                    seen.add(aid)
                    out["posts"].append(simplify_aweme(item))
    out["ok"] = bool(out["posts"])
    out["total"] = len(out["posts"])
    return out


def user_post_urls(sec_uid: str, *, max_count: int = 100, label: str | None = None,
                   fetch_all: bool = False) -> list[str]:
    """便捷方法: 直接返回可投喂下载器的作品 URL 列表."""
    result = list_posts(sec_uid, max_count=max_count, label=label, fetch_all=fetch_all)
    return [p["page_url"] for p in result.get("posts", [])]
