"""backend/live_room.py — 抖音直播间详情探测（实验性接入）。

设计来自 tests/live_room_analysis/ 的 js-reverse 分析结论：
- 核心接口 `GET /webcast/room/web/enter/`，一次拿到房间/主播/流/电商/同推房。
- 观众榜 `GET /webcast/ranklist/audience/`，Top200 上榜观众。
- 所有 `webcast/*` 带 a_bogus+msToken 签名，靠浏览器同源签好的请求、
  用 response 拦截读取，不自算签名（与 external/douyin-monitor/main.py
  fetch_profile 同套路）。

仅被检测链路在 live_status==1 时调用，避免对未直播账户空跑直播间探测。
失败一律降级返回 None，绝不影响主页 profile 已有结果。

值得注意：本服务每次探测自起 chromium → close，不复用 monitor 的页面池，
避免与 fetch_profile 的主页拦监互相污染。
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.request
import websockets  # type: ignore
from pathlib import Path
from typing import Any

try:
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover - 直播间探测为可选能力
    async_playwright = None  # type: ignore

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)

ENTER = "/webcast/room/web/enter/"
RANKLIST = "/webcast/ranklist/audience/"
PROFILE_OTHER = "/aweme/v1/web/user/profile/other/"

# 直播间卡片保留的观众榜条数（详情卡紧凑展示）
AUDIENCE_RANK_LIMIT = 10

# web_rid 可能存放的字段名（主页 room_data JSON 里逐个尝试）
_WEB_RID_KEYS = ("web_rid", "webRid", "web_rid_str", "rid")

# 与 downloader.preview_douyin_url 同样使用常驻 Chrome 拿登录态：CDP 端点 + 落盘 profile。
# 优先 CDP（用户已手动登录的常驻 Chrome），CDP 不可达时退化到 launch_persistent_context
# （复用 SEARCH_PROFILE_DIR 里的 cookie）。这俩都连不上再裸启动 headless（无登录态，
# fansclub/homepage 必返 20003，调用方应自行决定要不要写到 cache）。
_CDP_DEFAULT = "http://127.0.0.1:9222"


def _resolve_login_profile() -> str:
    """默认登录 profile 路径——与 start-douyin.ps1 的 $LoginProfile 保持一致。"""
    candidates = []
    env = os.environ.get("DOUYIN_LOGIN_PROFILE")
    if env:
        candidates.append(Path(env))
    # 与 start-douyin.ps1 保持一致：external\douyin-user-search\douyin_profile
    candidates.append(Path(__file__).resolve().parents[1] / "external" / "douyin-user-search" / "douyin_profile")
    for path in candidates:
        if path.exists():
            return str(path)
    return ""


async def _fetch_fansclub_via_cdp_raw(cdp_url: str, web_rid: str, anchor_id: str, timeout_s: float) -> dict[str, Any] | None:
    """绕过 playwright 直接走 raw CDP WS 拦 fansclub/homepage。

    背景：playwright 在当前版本的 sync/async connect_over_cdp 会无限挂起
    （已实测 — WS 握手成功但后续 Target.setAutoAttach 等无响应），所以
    这里直接用 websockets 库驱动 Chrome DevTools Protocol，对 live.douyin.com
    的现有 page 调 Runtime.evaluate 触发 fetch，再读 .text() 拿正文。

    已知坑：CDP-Chrome 上 page tab#0 偶尔会卡住 Runtime.evaluate（已实测：
    tab#0 timeout、tab#1+ 正常），所以遍历 page tabs 时按"找第一个非 timeout
    的"作为兜底。如果 web_rid 对应的 tab 不在 list 里、或者首 tab timeout，
    也自动顺延到下一个 live.douyin.com page。
    """
    if not anchor_id or not web_rid:
        return None
    try:
        with urllib.request.urlopen(cdp_url.rstrip("/") + "/json", timeout=5) as resp:
            tabs = json.loads(resp.read())
    except Exception:
        return None
    fetch_url = (
        "https://live.douyin.com/webcast/fansclub/homepage/?aid=6383"
        "&channel=channel_pc_web&device_platform=webapp"
        f"&anchor_id={anchor_id}&request_scene=4&action=2&source=2"
    )
    # 优先 web_rid 匹配，其次任意 live.douyin.com page
    page_tabs = [t for t in tabs if t.get("type") == "page"]
    ordered = []
    for t in page_tabs:
        if web_rid and web_rid in (t.get("url") or ""):
            ordered.append(t)
    for t in page_tabs:
        if "live.douyin.com" in (t.get("url") or "") and t not in ordered:
            ordered.append(t)

    async def call(ws, method: str, params: dict, mid: int, timeout: float):
        await ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError(f"{method} timed out")
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
            if msg.get("id") == mid:
                return msg

    for target in ordered:
        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            continue
        try:
            async with websockets.connect(ws_url, max_size=20 * 1024 * 1024, ping_interval=None, open_timeout=4) as ws:
                mid_state = {"n": 0}

                async def call(method: str, params: dict, timeout: float = 10.0) -> dict:
                    mid_state["n"] += 1
                    await ws.send(json.dumps({"id": mid_state["n"], "method": method, "params": params}))
                    deadline = asyncio.get_event_loop().time() + timeout
                    while True:
                        remaining = deadline - asyncio.get_event_loop().time()
                        if remaining <= 0:
                            raise asyncio.TimeoutError(f"{method} timed out")
                        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
                        if msg.get("id") == mid_state["n"]:
                            return msg

                # 如果 tab 当前不在 live.douyin.com 上（CORS/无登录态），
                # 先导航到 live.douyin.com/<web_rid> 再 fetch fansclub/homepage。
                # 仅当页面 URL 不含 live.douyin.com 时才导航，避免破坏用户当前页面。
                cur_url = (((await call("Runtime.evaluate", {
                    "expression": "location.href",
                })).get("result") or {}).get("result") or {}).get("value") or ""
                if "live.douyin.com" not in cur_url or (web_rid and web_rid not in cur_url):
                    try:
                        await call("Page.enable", {}, timeout=4)
                        await call("Page.navigate", {"url": f"https://live.douyin.com/{web_rid}"}, timeout=10)
                        # 等页面拉到 webcast 接口返回（fansclub 是页面加载后自动发的，
                        # 但 anchor cookie 通常要等几秒；保守等 6s）。
                        await asyncio.sleep(6)
                    except Exception:
                        pass

                r = await call("Runtime.evaluate", {
                    "expression": (
                        "fetch(" + json.dumps(fetch_url) + ", {credentials:'include'})"
                        ".then(r => r.text()).then(t => t)"
                    ),
                    "awaitPromise": True,
                    "returnByValue": True,
                }, timeout=timeout_s)
                value = (((r or {}).get("result") or {}).get("result") or {}).get("value")
                if not isinstance(value, str):
                    continue
                payload = json.loads(value)
                data = payload.get("data") or {}
                if not data:
                    return None
                return _summarize_fansclub(data)
        except (asyncio.TimeoutError, Exception):
            continue
    return None


class LiveRoomService:
    """打开直播间、拦 webcast 接口并归一为直播间卡片 dict。

    线程安全：每次 fetch_overview 内部独立 async 上下文，可被多账户并发调用
    （调用方负责限并发，本类不做信号量）。
    """

    def __init__(self, *, headless: bool = True, timeout_ms: int = 30000,
                 enter_wait_ms: int = 12000) -> None:
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.enter_wait_ms = enter_wait_ms

    async def close(self) -> None:
        """兼容 MonitorService.shutdown 调用；本服务无持久态，no-op。"""
        return

    async def fetch_fansclub(self, *, anchor_id: str = "",
                             web_rid: str = "",
                             room_id_str: str = "",
                             sec_uid: str = "") -> dict[str, Any] | None:
        """仅探测 anchor 粉丝团主页（团等级/成员数），不触发 enter/ranklist。

        web_rid 必须已知；用 web_rid 加载直播间页面触发 fansclub/homepage。
        必须登录态（webcast 域 Vip 端强约束），未登录返 20003 → 静默 None。

        浏览器优先级（与 downloader.preview_douyin_url 保持一致）：
          1) 复用常驻 Chrome（CDP=http://127.0.0.1:9222） — 已有登录态的入口；
          2) launch_persistent_context 走 SEARCH_PROFILE_DIR — 有 cookie 但没启 Chrome；
          3) 裸启动 headless（无登录态）— 仅用于兜底，多数情况下会返 20003。
        """
        if async_playwright is None:
            return None
        if not web_rid:
            return None

        # 0) 优先：raw CDP 复用常驻 Chrome 的现有 tab（最快路径，已实测稳定）。
        cdp_url = os.environ.get("DOUYIN_CDP", _CDP_DEFAULT)
        fast = await _fetch_fansclub_via_cdp_raw(cdp_url, web_rid, anchor_id, timeout_s=12.0)
        if fast:
            return fast

        # 1) 兜底：playwright 路径（仅在 raw CDP 不可用时跑一次）。

        captured: dict[str, Any] = {}
        fansclub_url = (
            "https://live.douyin.com/webcast/fansclub/homepage/?aid=6383"
            "&channel=channel_pc_web&device_platform=webapp"
            f"&anchor_id={anchor_id}&request_scene=1&action=1&source=1"
        )
        live_url = f"https://live.douyin.com/{web_rid}"

        async def on_response(response) -> None:
            if "fansclub/homepage" in response.url and "fansclub" not in captured:
                try:
                    captured["fansclub"] = await response.json()
                except Exception:
                    pass

        browser = None
        context = None
        page = None
        owns_context = False
        playwright = None
        try:
            cdp_url = os.environ.get("DOUYIN_CDP", _CDP_DEFAULT)
            if cdp_url:
                try:
                    playwright = await async_playwright().start()
                    browser = await playwright.chromium.connect_over_cdp(cdp_url)
                    ctxs = browser.contexts
                    if ctxs:
                        context = ctxs[0]
                        owns_context = False
                    else:
                        context = await browser.new_context(viewport={"width": 1600, "height": 1000})
                        owns_context = True
                    page = await context.new_page()
                except Exception:
                    browser = None
                    context = None
                    page = None
                    if playwright is not None:
                        try:
                            await playwright.stop()
                        except Exception:
                            pass
                        playwright = None
            if page is None:
                profile = _resolve_login_profile()
                if profile:
                    try:
                        playwright = await async_playwright().start()
                        context = await playwright.chromium.launch_persistent_context(
                            profile, headless=True,
                            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
                        )
                        page = context.pages[0] if context.pages else await context.new_page()
                        owns_context = True
                    except Exception:
                        context = None
                        page = None
                        if playwright is not None:
                            try:
                                await playwright.stop()
                            except Exception:
                                pass
                            playwright = None
            if page is None:
                playwright = await async_playwright().start()
                browser = await playwright.chromium.launch(
                    headless=self.headless,
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
                )
                context = await browser.new_context(
                    user_agent=UA, viewport={"width": 1600, "height": 1000}
                )
                page = await context.new_page()
                owns_context = True
            page.on("response", lambda r: asyncio.create_task(on_response(r)))

            try:
                await page.goto(live_url, wait_until="domcontentloaded",
                                timeout=self.timeout_ms)
            except Exception:
                return None

            # 触发 fansclub/homepage —— 通常页面加载后一次 fetch 即可拦到。
            # 已登录的常驻 Chrome / persistent context 通常也会自动发一次，
            # 主动 fetch 仅在什么都没拦到的兜底路径里生效。
            try:
                await page.evaluate(
                    """async ({url}) => {
                        try { await fetch(url, {credentials: 'include'}); } catch (e) {}
                    }""",
                    {"url": fansclub_url},
                )
            except Exception:
                pass

            for _ in range(20):
                if "fansclub" in captured:
                    break
                await asyncio.sleep(0.3)
        except Exception:
            return None
        finally:
            # CDP 复用常驻 Chrome 时只关 page（不动 browser/context）；
            # 自主创建的浏览器/context/Playwright 关闭后释放。
            if owns_context:
                try:
                    if context is not None:
                        await context.close()
                except Exception:
                    pass
                try:
                    if browser is not None:
                        await browser.close()
                except Exception:
                    pass
            else:
                try:
                    if page is not None:
                        await page.close()
                except Exception:
                    pass
                try:
                    if browser is not None:
                        await browser.close()
                except Exception:
                    pass
            if playwright is not None:
                try:
                    await playwright.stop()
                except Exception:
                    pass

        if "fansclub" not in captured:
            return None
        data = captured["fansclub"].get("data") or {}
        return _summarize_fansclub(data) if data else None

    async def fetch_anchor_paygrade(self, *, web_rid: str = "") -> int | None:
        """仅探测 anchor 本人 paygrade（hover popup 解析）。

        不依赖 anchor 是否在直播（回放页 status=4 也能拿），但需要 web_rid 已知。
        失败返回 None。
        """
        if async_playwright is None or not web_rid:
            return None
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=self.headless,
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
                )
                context = await browser.new_context(
                    user_agent=UA, viewport={"width": 1600, "height": 1000}
                )
                page = await context.new_page()
                try:
                    await page.goto(f"https://live.douyin.com/{web_rid}",
                                    wait_until="domcontentloaded",
                                    timeout=self.timeout_ms)
                except Exception:
                    await context.close()
                    await browser.close()
                    return None
                await asyncio.sleep(15)  # 等 anchor bar 渲染（status=4 回放页较慢）
                bar = page.locator('[data-e2e="rooom-info-bar-anchor"]')
                if await bar.count() == 0:
                    await context.close()
                    await browser.close()
                    return None
                await bar.hover()
                await asyncio.sleep(2.5)
                level = await page.evaluate("""
                    () => {
                        const popup = document.querySelector('.semi-popover-content');
                        const userName = popup?.querySelector('.user_name, [class*="user_name"]');
                        if (!userName) return null;
                        const imgs = userName.querySelectorAll('img');
                        for (const img of imgs) {
                            const m = (img.src || '').match(/new_user_grade_level_v1_(\\d+)/);
                            if (m) return parseInt(m[1]);
                        }
                        return null;
                    }
                """)
                await context.close()
                await browser.close()
                if isinstance(level, int) and 0 < level <= 75:
                    return level
        except Exception:
            return None
        return None

    async def fetch_overview(self, *, web_rid: str = "",
                             room_id_str: str = "",
                             sec_uid: str = "") -> dict[str, Any] | None:
        """探测直播间。

        入参优先级：web_rid（直播短号，可直接拼 live.douyin.com/<web_rid>）>
        room_id_str（数字房间号，主页接口可拿到，但本身不能直接拼直播间 URL，
        需先经主页 room_data 换出 web_rid）> sec_uid（用来回主页拦 profile/other
        取 room_data 再换 web_rid）。

        主页 profile/other 在直播中会把 room_data（JSON 串）带回，内含 web_rid。
        实测:live.douyin.com/?room_id=<数字> 与 live.douyin.com/<纯数字room_id>
        都不触发 webcast/room/web/enter/，只有 /<web_rid> 才触发，所以必须先换出
        web_rid 再进直播间。

        返回 None 表示未能拿到直播间数据（未直播/风控/网络/未安装 playwright）。
        """
        if async_playwright is None:
            return None
        if not web_rid and not room_id_str and not sec_uid:
            return None

        captured: dict[str, Any] = {}

        async def on_response(response) -> None:
            rurl = response.url
            if ENTER in rurl and "enter" not in captured:
                try:
                    captured["enter"] = await response.json()
                except Exception:
                    pass
            elif RANKLIST in rurl and "ranklist" not in captured:
                try:
                    captured["ranklist"] = await response.json()
                except Exception:
                    pass
            elif PROFILE_OTHER in rurl and "profile" not in captured:
                try:
                    captured["profile"] = await response.json()
                except Exception:
                    pass

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=self.headless,
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
                )
                context = await browser.new_context(
                    user_agent=UA, viewport={"width": 1600, "height": 1000}
                )
                page = await context.new_page()
                page.on("response", lambda r: asyncio.create_task(on_response(r)))

                # 第一阶段：缺 web_rid 时回主页拿 room_data 换 web_rid。
                resolved_web_rid = web_rid
                if not resolved_web_rid and sec_uid:
                    home = f"https://www.douyin.com/user/{sec_uid}"
                    try:
                        await page.goto(home, wait_until="domcontentloaded",
                                        timeout=self.timeout_ms)
                    except Exception:
                        pass
                    for _ in range(max(1, self.enter_wait_ms // 300)):
                        if "profile" in captured:
                            break
                        await asyncio.sleep(0.3)
                    resolved_web_rid = _web_rid_from_profile(captured.get("profile") or {})

                if not resolved_web_rid:
                    await context.close()
                    await browser.close()
                    return None

                # 第二阶段：用 web_rid 进直播间拦 enter/ranklist（复用同一 context）。
                live_url = f"https://live.douyin.com/{resolved_web_rid}"
                try:
                    await page.goto(live_url, wait_until="domcontentloaded",
                                    timeout=self.timeout_ms)
                except Exception:
                    await context.close()
                    await browser.close()
                    return None
                for _ in range(max(1, self.enter_wait_ms // 300)):
                    if "enter" in captured:
                        break
                    await asyncio.sleep(0.3)
                if "enter" not in captured:
                    await context.close()
                    await browser.close()
                    return None
                await asyncio.sleep(1.5)  # 榜单/心愿单加载余量

                # 第三阶段：anchor 头像 hover 弹 popup，解析 paygrade.level (1-75)。
                # 这是拿 anchor **本人** 荣耀等级的最简便方式 (viewer 即可)，无需登录态。
                # 仅当 status==2 (直播中) 时 popup 才稳定 — status==4 (回放页) 实测也能拿。
                anchor_paygrade: int | None = None
                try:
                    bar = page.locator('[data-e2e="rooom-info-bar-anchor"]')
                    if await bar.count() > 0:
                        await bar.hover()
                        await asyncio.sleep(2.5)
                        pg_result = await page.evaluate("""
                            () => {
                                const popup = document.querySelector('.semi-popover-content');
                                const userName = popup?.querySelector('.user_name, [class*="user_name"]');
                                if (!userName) return null;
                                const imgs = userName.querySelectorAll('img');
                                for (const img of imgs) {
                                    const m = (img.src || '').match(/new_user_grade_level_v1_(\\d+)/);
                                    if (m) return parseInt(m[1]);
                                }
                                return null;
                            }
                        """)
                        if isinstance(pg_result, int) and 0 < pg_result <= 75:
                            anchor_paygrade = pg_result
                except Exception:
                    anchor_paygrade = None

                # 第四阶段：拦 fansclub/homepage 拿 anchor 粉丝团元数据（团等级/成员数）。
                # 必须登录态（webcast 域 Vip 端强约束），未登录返 20003 → 静默 None。
                try:
                    enter_root = captured.get("enter") or {}
                    enter_data_inner = enter_root.get("data") or {}
                    anchor_uid = (enter_data_inner.get("user") or {}).get("id_str") or ""
                    room_id_guess = (
                        (enter_data_inner.get("room") or {}).get("id_str")
                        or enter_data_inner.get("enter_room_id")
                        or ""
                    )
                    if anchor_uid:
                        await page.evaluate(
                            """async ({url}) => {
                                try {
                                    await fetch(url, {credentials: 'include'});
                                } catch (e) {}
                            }""",
                            {
                                "url": (
                                    "https://live.douyin.com/webcast/fansclub/homepage/?aid=6383"
                                    "&channel=channel_pc_web&device_platform=webapp"
                                    f"&anchor_id={anchor_uid}&request_scene=1&action=1&source=1"
                                )
                            },
                        )
                        await asyncio.sleep(2.0)
                except Exception:
                    pass

                await context.close()
                await browser.close()
        except Exception:
            return None

        return _summarize(captured, web_rid=resolved_web_rid, anchor_paygrade=anchor_paygrade)


def _summarize(captured: dict[str, Any], *, web_rid: str = "",
               anchor_paygrade: int | None = None) -> dict[str, Any] | None:
    """把拦到的 enter/ranklist 归一为直播间卡片 dict；失败返回 None。

    与 tests/live_room_analysis/extract_live_room.py._summarize 同源逻辑，
    服务层在此固定裁观众榜为 Top10。
    """
    enter = captured.get("enter") or {}
    enter_data = enter.get("data") or {}
    rooms = enter_data.get("data") or []
    if not rooms:
        return None
    room = rooms[0] if isinstance(rooms, list) else rooms
    user = enter_data.get("user") or {}
    owner = room.get("owner") or {}
    stream = enter_data.get("web_stream_url") or {}

    # 直播间状态：status==2 视为直播中；否则仍归一但前端按 live_status==1 才展示
    rank = captured.get("ranklist") or {}
    rank_data = rank.get("data") or {}
    ranks = rank_data.get("ranks") or []

    # fansclub/homepage 拦到的 anchor 粉丝团元数据
    fansclub_payload = captured.get("profile") or {}
    fansclub_data = (fansclub_payload.get("data") or {}) if isinstance(fansclub_payload, dict) else {}

    card: dict[str, Any] = {
        "web_rid": web_rid or _guess_web_rid(room, enter_data),
        "room_id_str": room.get("id_str") or enter_data.get("enter_room_id") or "",
        "status": room.get("status"),
        "title": room.get("title") or "",
        "user_count_str": room.get("user_count_str") or (room.get("stats") or {}).get("user_count_str"),
        "viewers": _int((room.get("room_view_stats") or {}).get("display_value")),
        "total_user_str": (room.get("stats") or {}).get("total_user_str"),
        "like_count": room.get("like_count"),
        "partition": ((enter_data.get("partition_road_map") or {}).get("partition") or {}),
        "similar_rooms": _similar(enter_data),
        "audience_rank_top": _audience_rank(ranks[:AUDIENCE_RANK_LIMIT]),
        "audience_rank_meta": {
            "total": rank_data.get("total"),
            "user_count_desc": rank_data.get("user_count_desc"),
            "has_more": rank_data.get("has_more"),
        },
        "qrcode_url": enter_data.get("qrcode_url") or "",
        "anchor": {
            "uid": user.get("id_str") or owner.get("id_str") or "",
            "sec_uid": user.get("sec_uid") or owner.get("sec_uid") or "",
            "nickname": user.get("nickname") or owner.get("nickname") or "",
            "avatar": ((owner.get("avatar_thumb") or {}).get("url_list") or [None])[0] or "",
            "paygrade_level": anchor_paygrade,
        },
        "fansclub": _summarize_fansclub(fansclub_data),
        "stream_url": {
            "hls_pull_url": stream.get("hls_pull_url") or "",
            "flv_pull_url": stream.get("flv_pull_url") or "",
            "default_resolution": stream.get("default_resolution") or "",
        },
    }
    return card


def _guess_web_rid(room: dict[str, Any], enter_data: dict[str, Any]) -> str:
    for src in (enter_data, room):
        for key in ("web_rid", "webRid", "web_rid_str"):
            val = src.get(key)
            if val:
                return str(val)
    return ""


def _web_rid_from_profile(profile_payload: dict[str, Any]) -> str:
    """从主页 profile/other 响应里换出 web_rid。

    直播中的主页响应：body.user.room_data 是个 JSON 串，内含 web_rid 等房间信息。
    兼容字段名 web_rid / webRid / web_rid_str / rid，并兼顾 owner 嵌套。
    未直播 / 无 room_data / 提不到 → 返回 ""。
    """
    user = profile_payload.get("user") if isinstance(profile_payload.get("user"), dict) else profile_payload
    room_raw = user.get("room_data") or user.get("roomData")
    room_data: dict[str, Any] = {}
    if isinstance(room_raw, dict):
        room_data = room_raw
    elif isinstance(room_raw, str) and room_raw.startswith("{"):
        try:
            parsed = json.loads(room_raw)
            if isinstance(parsed, dict):
                room_data = parsed
        except json.JSONDecodeError:
            room_data = {}
    if not room_data:
        return ""

    def _scan(node: Any) -> str:
        if isinstance(node, dict):
            for key in _WEB_RID_KEYS:
                val = node.get(key)
                if val:
                    return str(val)
            for v in node.values():
                found = _scan(v)
                if found:
                    return found
        elif isinstance(node, list):
            for v in node:
                found = _scan(v)
                if found:
                    return found
        return ""

    return _scan(room_data)


def _similar(enter_data: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for s in (enter_data.get("similar_rooms") or [])[:3]:
        r = s.get("room") or {}
        out.append({
            "web_rid": str(s.get("web_rid") or ""),
            "title": r.get("title") or "",
            "user_count_str": r.get("user_count_str") or "",
        })
    return out


def _audience_rank(ranks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in ranks:
        u = r.get("user") or {}
        fc = u.get("fans_club") or {}
        fcd = fc.get("data") or {}
        badge = fcd.get("badge") or {}
        icons = badge.get("icons") or {}
        out.append({
            "rank": r.get("rank"),
            "nickname": u.get("nickname") or "",
            "sec_uid": u.get("sec_uid") or "",
            "display_id": u.get("display_id") or "",
            "gender": u.get("gender"),
            "pay_grade_level": (u.get("pay_grade") or {}).get("level"),
            "pay_grade_min_diamond": (u.get("pay_grade") or {}).get("this_grade_min_diamond"),
            "fans_club_level": fcd.get("level"),
            "fans_club_status": fcd.get("user_fans_club_status"),
            "guard_status": fcd.get("user_guard_status"),
            "guard_expired_time": fcd.get("guard_expired_time"),
            "club_name": fcd.get("club_name") or "",
            "club_anchor_id": fcd.get("anchor_id"),
            "club_badge_url": ((icons.get("2") or {}).get("url_list") or [None])[0] or "",
            "club_badge_advanced_url": ((icons.get("4") or {}).get("url_list") or [None])[0] or "",
        })
    return out


def _summarize_fansclub(data: dict[str, Any]) -> dict[str, Any]:
    """归一 fansclub/homepage 响应（anchor 粉丝团元数据）。失败/未登录返回 {}。

    响应结构差异：
      旧版（未登录或早期版本）：data 在根层（data.max_level 等）；
      新版（登录态实测）：实际字段包在 data.club_info 里，data 顶层只剩
      status_code/extra/is_sign_up_guard 等；max_level/club_level/total_fans_count
      等都在 data.club_info 里。club_info 缺字段时回落到 data 顶层。
    """
    if not isinstance(data, dict) or not data:
        return {}
    club_info = data.get("club_info") or {}

    def pick(key: str, default: Any = 0) -> Any:
        # 优先 club_info，再回落顶层 data，最后 fallback 到 extra 段（fandom_id 在 extra）
        if isinstance(club_info, dict) and club_info.get(key) not in (None, ""):
            return club_info[key]
        if data.get(key) not in (None, ""):
            return data[key]
        extra = data.get("extra") or {}
        if isinstance(extra, dict) and extra.get(key) not in (None, ""):
            return extra[key]
        return default

    # 团名/anchor_name 同义多版本；club_name 在 club_info 里也可能带 "未开通" 等
    club_name = (
        (club_info.get("anchor_name") if isinstance(club_info, dict) else None)
        or (club_info.get("club_name") if isinstance(club_info, dict) else None)
        or data.get("anchor_name")
        or data.get("club_name")
        or ""
    )
    return {
        "club_name": club_name,
        "anchor_id": str(pick("anchor_id", "") or ""),
        "active_fans_count": _int(pick("active_fans_count")),
        "total_fans_count": _int(pick("total_fans_count")),
        "today_new_fans_count": _int(pick("today_new_fans_count")),
        # max_level 是系统硬顶 (实测全 anchor 都 20)，保留但 UI 不要展示；
        # club_level 是"请求者自己在该团里的等级"（未加入 = 0），不能区分 anchor。
        "max_level": _int(pick("max_level")),
        "club_level": _int(pick("club_level")),
        # 真正的"粉丝团等级"在 club_info.club_level_info 里：level=0-9, max_level=9,
        # cur_value=经验值（达到 max 后归 0 重置一赛季）。注意 club_level_info 是嵌套
        # dict 不在 pick() 范围内，直接从 club_info 读，缺则给默认。
        "club_grade": _int(club_info.get("club_level_info", {}).get("level")) if isinstance(club_info, dict) else 0,
        "club_grade_max": _int(club_info.get("club_level_info", {}).get("max_level", 9)) if isinstance(club_info, dict) else 9,
        "club_exp": _int(club_info.get("club_level_info", {}).get("cur_value")) if isinstance(club_info, dict) else 0,
        "club_season": _int(club_info.get("club_level_info", {}).get("season_id")) if isinstance(club_info, dict) else 0,
        "fans_name": pick("fans_name", "") or "",
        "fansclub_mode": _int(pick("fansclub_mode")),
        # 这俩是 request_scene=4 才有的真实 anchor 指标 — popularity_rank_hour 是
        # 该 anchor 最近一小时热度排名（数字越小越热门），num_of_fans_group 是
        # 其下子团（XX分团/家族团）数量。request_scene=1 时它们都是 0。
        "popularity_rank_hour": _int(pick("popularity_rank_hour")),
        "num_of_fans_group": _int(pick("num_of_fans_group")),
        "fandom_id": str(pick("fandom_id", "") or ""),
    }


def _int(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n


def summarize_for_tests(captured: dict[str, Any], *, web_rid: str = "",
                        anchor_paygrade: int | None = None) -> dict[str, Any] | None:
    """供 tests/test_live_room.py 离线喂样本调用（不触碰浏览器）。"""
    return _summarize(captured, web_rid=web_rid, anchor_paygrade=anchor_paygrade)
