"""backend/boards_fetch.py — 抖音榜单面板数据 (阶段一: 纯 web 可达榜单)。

三类来源 (round 8 分析定论, tests/live_room_analysis/round8_boards_analysis.json):
1. 抖音热榜: GET www.douyin.com/aweme/v1/hot/search/list/ — 无签名直连可用。
2. 直播间榜单: CDP Chrome 进直播间页拦响应 —
   - audience (在线观众榜): 进房自动发, 零点击;
   - hour_entrance: 进房自动发, 给 exist_tabs (tab 名 + ranklist_type);
   - hour_detail: 点榜 tab 才发, ranklist_type 切换 (0=小时榜/全站百强, 3=人气榜, 6=新人)。
   所有 webcast/* 带签名, 必须靠页面自己签好请求后拦响应 (a_bogus 绑参数,
   换 room_id 重放同 URL → 10011, 已实测)。

浏览器策略与 live_room.LiveRoomService 相同: 优先复用常驻 CDP Chrome (9222),
只开 page 不杀浏览器; CDP 不可达退化冷启 chromium。

前端面板入口: /api/boards/hot 与 /api/boards/live。
"""

from __future__ import annotations

import asyncio
import json
import ssl
import urllib.request
from typing import Any

try:
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover
    async_playwright = None  # type: ignore

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)

SSL_CTX = ssl.create_default_context()

# 面板每榜保留条数
BOARD_LIMIT = 50


# ---- 热榜 (直连, 无浏览器) ----

def fetch_hot_board() -> dict[str, Any]:
    """抖音热榜 (含子榜标签)。直连 www.douyin.com, 200 + 48~51 词条。"""
    req = urllib.request.Request(
        "https://www.douyin.com/aweme/v1/hot/search/list/?detail_list_source=hot_board",
        headers={"User-Agent": UA},
    )
    with urllib.request.urlopen(req, timeout=12, context=SSL_CTX) as resp:
        d = json.loads(resp.read())
    words = (d.get("data") or {}).get("word_list") or []
    out = []
    for w in words[:BOARD_LIMIT]:
        awemes = w.get("aweme_infos") or []
        out.append({
            "word": w.get("word") or "",
            "hot_value": w.get("hot_value") or 0,
            "label": w.get("label"),  # 0/1=热 3=新 4=爆 9/16/17=热/沸/爆类标签
            "word_type": w.get("word_type"),
            "sentence_tag": w.get("sentence_tag"),
            "video_count": w.get("video_count") or 0,
            "related_words": [x.get("word") for x in (w.get("related_words") or []) if x.get("word")][:4],
            "top_aweme_desc": (awemes[0].get("desc") or "")[:60] if awemes else "",
        })
    return {"ok": True, "items": out, "active_time": (d.get("data") or {}).get("active_time") or ""}


# ---- 直播间榜单 (CDP 拦响应) ----

# 榜 tab: (tab 文本, 中文名) — 点 tab 触发 hour_detail; 归属看响应 tab_info
_LIVE_TABS: tuple[tuple[str, str], ...] = (
    ("小时榜", "小时榜"),
    ("人气榜", "人气榜"),
)


def _board_rows(ranks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in ranks[:BOARD_LIMIT]:
        u = r.get("user") or {}
        out.append({
            "rank": r.get("rank"),
            "nickname": u.get("nickname") or "",
            "sec_uid": u.get("sec_uid") or "",
            "score": r.get("score") or 0,
            "score_str": r.get("score_str") or "",
        })
    return out


def _entrance_tabs(data: dict[str, Any]) -> list[dict[str, Any]]:
    """hour_entrance.exist_tabs 归一: [{name, ranklist_type, sub_tabs:[{name, ranklist_type}]}]"""
    tabs = []
    for t in (data.get("exist_tabs") or []):
        tabs.append({
            "name": t.get("name") or "",
            "ranklist_type": t.get("ranklist_type"),
            "sub_tabs": [
                {"name": s.get("name") or "", "ranklist_type": s.get("ranklist_type")}
                for s in (t.get("sub_tabs") or [])
            ],
        })
    return tabs


def fetch_live_boards(web_rid: str, *, timeout_ms: int = 30000) -> dict[str, Any]:
    """进直播间拦全部榜单响应。

    流程: 开页 → 进 live.douyin.com/<web_rid> (自动发 audience + hour_entrance,
    拦响应) → 点各榜 tab 拦 hour_detail (ranklist_type 0/3/6) → 归一。
    房间不存在/未开播/风控 → ok=False + reason。
    """
    if async_playwright is None:
        return {"ok": False, "reason": "playwright 未安装"}
    web_rid = str(web_rid).strip()
    if not web_rid:
        return {"ok": False, "reason": "缺少直播间号"}

    try:
        return asyncio.run(_fetch_live_boards_async(web_rid, timeout_ms))
    except Exception as exc:  # 网络/风控/页面异常统一降级
        return {"ok": False, "reason": str(exc)[:120]}


async def _fetch_live_boards_async(web_rid: str, timeout_ms: int) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def on_response(response) -> None:
        rurl = response.url
        try:
            if "/webcast/ranklist/audience/" in rurl and "audience" not in captured:
                captured["audience"] = await response.json()
            elif "/webcast/ranklist/hour_entrance/" in rurl and "entrance" not in captured:
                captured["entrance"] = await response.json()
            elif "/webcast/ranklist/hour_detail/" in rurl:
                captured.setdefault("hour_detail", []).append(await response.json())
        except Exception:
            pass

    async with async_playwright() as p:
        browser, context, page, owns = await _acquire_page(p, timeout_ms)
        try:
            page.on("response", lambda r: asyncio.create_task(on_response(r)))
            try:
                await page.goto(f"https://live.douyin.com/{web_rid}",
                                wait_until="domcontentloaded", timeout=timeout_ms)
            except Exception:
                pass  # domcontentloaded 超时也继续等响应
            # 等进房自动请求 (audience + hour_entrance) + 页面渲染出"小时榜"入口
            for _ in range(max(1, timeout_ms // 300)):
                if "audience" in captured and "entrance" in captured:
                    break
                await asyncio.sleep(0.3)
            # 页面渲染可能很慢 (实测 6s 时常卡"直播加载中", 10s+ 才出榜入口);
            # 没等到 entrance 也会继续, 点得到 tab 就有 hour_detail。
            for _ in range(10):
                if await page.evaluate(
                    '() => (document.body.innerText||"").includes("小时榜")'
                ):
                    break
                await asyncio.sleep(1)

            # 点各榜 tab 触发 hour_detail。必须用 playwright trusted click (JS .click()
            # 不触发 React 合成事件); force=True 绕遮挡。tab 归属看响应 tab_info:
            # 有 exist_tabs = 小时榜; tab_info 为 null = 人气榜 (实测定论)。
            for _name, _label in _LIVE_TABS[:2]:  # 新人榜(rt=6)多数房间无此 tab, 点不到就跳过
                try:
                    await page.locator(f"text={_name}").last.click(timeout=4000, force=True)
                except Exception:
                    continue  # 房间无该榜 tab
                before = len(captured.get("hour_detail") or [])
                for _ in range(20):  # 最多 ~6s 等响应
                    if len(captured.get("hour_detail") or []) > before:
                        break
                    await asyncio.sleep(0.3)

            try:
                await page.close()
            except Exception:
                pass
            return _summarize_live(captured, web_rid)
        finally:
            try:
                await page.close()
            except Exception:
                pass
            if owns:
                try:
                    await context.close()
                except Exception:
                    pass
                try:
                    await browser.close()
                except Exception:
                    pass


async def _acquire_page(p, timeout_ms: int):
    """与 LiveRoomService._acquire_page 同策略: CDP 优先, 冷启兜底。
    返回 (browser, context, page, owns_browser)。"""
    cdp_alive = False
    try:
        import httpx
        resp = httpx.get("http://127.0.0.1:9222/json/version", timeout=1.5)
        cdp_alive = resp.status_code == 200
    except Exception:
        pass
    if cdp_alive:
        try:
            browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = await context.new_page()
            await page.set_viewport_size({"width": 1600, "height": 1000})
            return browser, context, page, False
        except Exception:
            pass  # CDP 竞态死亡 → 冷启
    browser = await p.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    )
    context = await browser.new_context(user_agent=UA, viewport={"width": 1600, "height": 1000})
    page = await context.new_page()
    return browser, context, page, True


def _summarize_live(captured: dict[str, Any], web_rid: str) -> dict[str, Any]:
    if "audience" not in captured and "entrance" not in captured and not captured.get("hour_detail"):
        return {"ok": False, "reason": "未拦到榜单请求 (房间不存在/未开播/风控)"}

    aud_data = (captured.get("audience") or {}).get("data") or {}
    ent_data = (captured.get("entrance") or {}).get("data") or {}

    boards: dict[str, list] = {"hour": [], "popularity": []}
    for resp in captured.get("hour_detail") or []:
        d = resp.get("data") or {}
        rows = _board_rows(d.get("ranks") or [])
        if not rows:
            continue
        # tab_info 有 exist_tabs = 小时榜; tab_info 为 null = 人气榜 (实测定论)
        if d.get("tab_info") is not None:
            boards["hour"] = rows
        else:
            boards["popularity"] = rows

    return {
        "ok": True,
        "web_rid": web_rid,
        "tabs": _entrance_tabs(ent_data),
        "audience_top": _board_rows(aud_data.get("ranks") or []),
        "audience_meta": {
            "total": aud_data.get("total"),
            "user_count_desc": aud_data.get("user_count_desc"),
            "has_more": aud_data.get("has_more"),
        },
        "period_remain_seconds": ent_data.get("period_remain_seconds"),
        "hour_board": boards["hour"],
        "popularity_board": boards["popularity"],
    }
