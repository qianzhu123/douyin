"""backend/boards_fetch.py — 抖音榜单面板数据 (阶段一: 纯 web 可达榜单)。

三类来源 (round 8 分析定论, tests/live_room_analysis/round8_boards_analysis.json):
1. 抖音热榜: GET www.douyin.com/aweme/v1/hot/search/list/ — 无签名直连可用。
2. 直播间榜单: CDP Chrome 进直播间页拦响应 —
   - audience (在线观众榜): 进房自动发, 零点击;
   - hour_entrance: 进房自动发, 给 exist_tabs (tab 名 + ranklist_type);
   - hour_detail: 点榜 tab 才发, ranklist_type 切换 (0=小时榜/全站百强, 3=人气榜;
     rt=6 新人榜已舍弃 — 2026-11-19 用户定调)。
   所有 webcast/* 带签名, 必须靠页面自己签好请求后拦响应 (a_bogus 绑参数,
   换 room_id 重放同 URL → 10011, 已实测)。

浏览器策略与 live_room.LiveRoomService 相同: 优先复用常驻 CDP Chrome (9222),
只开 page 不杀浏览器; CDP 不可达退化冷启 chromium。

前端面板入口: /api/boards/hot 与 /api/boards/live。
"""

from __future__ import annotations

import asyncio
import json
import re
import ssl
import urllib.parse
import urllib.request
from typing import Any

try:
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover
    async_playwright = None  # type: ignore

try:
    # app 域签名链 (unidbg 离线签头, aweme_search_protocol 与 lab 仓解耦);
    # 兼容两种加载语境: 脚本直跑 (backend 目录进 path) / backend 包内 (相对导入)
    try:
        import aweme_search_protocol as _asp  # type: ignore
    except ImportError:
        from . import aweme_search_protocol as _asp  # type: ignore
except ImportError:  # pragma: no cover
    _asp = None  # type: ignore

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)

SSL_CTX = ssl.create_default_context()
# app 域 (aweme.snssdk.com / webcast.amemv.com) 证书链与 web 域不同, 关校验 (T 系列配方)
SSL_CTX_INSECURE = ssl.create_default_context()
SSL_CTX_INSECURE.check_hostname = False
SSL_CTX_INSECURE.verify_mode = ssl.CERT_NONE

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

# 榜 tab: (tab 文本, 中文名) — 备用 trusted click 触发 hour_detail (重放失败时降级)
_LIVE_TABS: tuple[tuple[str, str], ...] = (
    ("小时榜", "小时榜"),
    ("人气榜", "人气榜"),
)

# ranklist_type 重放表 (2026-11-15 实测定论: 换 captured URL 里的 ranklist_type
# 在页内 fetch 重放有效 — 服务端只校验 a_bogus 与其余参数, rt 可换):
#   0=小时榜(tab_info.Y), 3=人气榜(tab_info.N, score "100万+")
#   (rt=6 新人榜 2026-11-19 用户定调舍弃, 不再拉取)
_RT_BOARDS: tuple[tuple[str, str], ...] = (
    ("hour", "0"),
    ("pop", "3"),
)


def _board_rows(ranks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # 2026-11-16 复测: hour_detail 行结构已变 — 单数 `user` → 复数 `users`
    # (list, 疑似主播+搭档多占位), 名次仍在 `index`。防御性双取: users[0] 优先,
    # 回退 user。2026-11-18 定论: 服务端把三榜 score/score_str 全收了 (web 端
    # hour_detail 行只剩 user/index/score_str/tags/event/labels, 无任何数值字段;
    # app 域 rt=12 的 score 字段也是恒 1 占位)。数值列唯一存活信号 = app 行的
    # delta (与上一名的分差, rank1 恒 0), 由 _enrich_hour_with_delta 合并。
    out = []
    for r in ranks[:BOARD_LIMIT]:
        u = None
        users = r.get("users")
        if isinstance(users, list) and users:
            u = users[0] or {}
        if not u:
            u = r.get("user") or {}
        out.append({
            "rank": r.get("index") if r.get("index") is not None else r.get("rank"),
            "nickname": u.get("nickname") or "",
            "sec_uid": u.get("sec_uid") or "",
            "score": r.get("score") or 0,
            "score_str": r.get("score_str") or "",
            # 2026-11-20: delta = 与上一名的分差 (app 链行有, web 链行无),
            # 小时榜数值列用它显示 "与上一名差 N" (官方 App 同款语义)
            "delta": r.get("delta") or 0,
            # 2026-11-16 晚: 行新增 tags (人气榜 "粉丝团：xxx"/"N人助力", 小时榜 "省份")
            "tags": [str(t) for t in (r.get("tags") or []) if t],
        })
    return out


# ---- 小时榜数值列现状 (2026-11-19 定论) ----
# web hour_detail rt=0 与 app 域 ranklist/hour rt=12 的绝对分数均已确认被服务端
# 撤掉 (web 行无数值字段; app 行 score 恒 1 占位)。app 链路唯一真实数值是
# delta (与紧邻上一名的分差), 曾据此还原相对累计分展示 — 2026-11-19 用户否决:
# 相对值无意义, 官方 UI 本来就显示"差N"。数值列不再造假数据, 老实留空; delta
# 探测链路已从主流程移除 (历史实现见 git f53a12f)。

# ---- 全站人气榜 (app 域, 2026-11-19 probe52 打通) ----
# GET webcast.amemv.com/webcast/ranklist/popularity/ ranklist_type=3, 需完整身份
# {anchor_id, room_id, sec_anchor_id} — 从 app /webcast/feed/ 在播房间取 (身份不全
# → sc=10011, probe45/47 教训)。行 gap_description = 官方人气值具体数值 ("100万+"
# /"74.8万", 逐名递减), contributor_text = "N人助力" — 数值列用 gap_description,
# 助力人数进 tags, 这是 web 链路拿不到、超出官方 UI 直观展示的数据。

_CN_NUM_UNIT = {"万": 10_000, "亿": 100_000_000}


def _parse_gap_value(s: str):
    """官方格式化人气值串 -> (数值, 是否封顶)。"100万+" -> (1_000_000, True);
    "74.8万" -> (748_000, False); 无数字/解析失败 -> None。"""
    s = (s or "").strip()
    if not s:
        return None
    capped = s.endswith("+") or "以上" in s
    s2 = s.rstrip("+").replace("以上", "").strip()
    m = re.match(r"^([\d.]+)([万亿]?)$", s2)
    if not m:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    unit = m.group(2)
    if unit in _CN_NUM_UNIT:
        val *= _CN_NUM_UNIT[unit]
    return int(round(val)), capped


def _resolve_popularity_scores(rows: list[dict[str, Any]]) -> None:
    """人气值标注 (2026-11-20 probe73/74 定论): rt=3 行无 delta 字段 (分差链
    只在小时榜 rt=12), 无锚可回推。官方 gap_description 本身分两档:
    - 非封顶精确串 ("88.3万"/"8900") → 解析成数值写 score, exact=True;
    - 封顶串 ("100万+") → exact=False, score_str 清空 (2026-11-20 用户定调:
      相对值/占位/封顶串都不算真数值, 不展示; 官方 App 也只显示到这)。"""
    for row in rows:
        p = _parse_gap_value(row.get("score_str") or "")
        if p and not p[1]:
            row["score"] = p[0]
            row["exact"] = True
        else:
            row["score"] = 0
            row["score_str"] = ""
            row["exact"] = False


def fetch_hour_deltas(count: int = 100) -> dict[str, Any]:
    """app 域小时榜 delta (与上一名分差, 第一名恒 0) — probe72 实测。
    绝对分无锚无解 (probe53-58 三链定论), 但 delta 是官方同款展示原料。"""
    if _asp is None:
        return {"ok": False, "reason": "app 签名链不可用"}
    try:
        q_feed = _asp._common_q(count=20)
        feed_url = ("https://webcast.amemv.com/webcast/feed/?" + urllib.parse.urlencode(q_feed))
        feed = _app_request(feed_url, _min_jar())
        rid = aid = sec = ""
        for item in (feed.get("data") or []):
            if not isinstance(item, dict):
                continue
            d = item.get("data") or {}
            owner = d.get("owner") or {}
            rid = d.get("id_str") or ""
            aid = owner.get("id_str") or str(owner.get("id") or "")
            sec = owner.get("sec_uid") or ""
            if rid and aid and sec and (d.get("status") in (0, 2)):
                break
        if not (rid and aid and sec):
            return {"ok": False, "reason": "feed 未取到在播房间身份"}
        q = _asp._common_q(offset=0, count=count)
        q.update({"room_id": rid, "rank_type": "12", "sec_anchor_id": sec,
                  "hour_info": "0"})
        url = ("https://webcast.amemv.com/webcast/ranklist/hour/?"
               + urllib.parse.urlencode(q))
        body = _app_request(url, _min_jar())
        if body.get("status_code") != 0:
            return {"ok": False, "reason": f"sc={body.get('status_code')}"}
        deltas = []
        for r in (body.get("data") or {}).get("ranks") or []:
            deltas.append({
                "rank": r.get("rank") if r.get("rank") is not None else r.get("index"),
                "delta": r.get("delta") or 0,
            })
        return {"ok": True, "deltas": deltas}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)[:120]}


def fetch_popularity_board(count: int = 100) -> dict[str, Any]:
    """全抖音人气榜 (app 域 unidbg 签名, probe52 实测 sc=0 100 行)。"""
    if _asp is None:
        return {"ok": False, "reason": "app 签名链不可用"}
    try:
        q_feed = _asp._common_q(count=20)
        feed_url = ("https://webcast.amemv.com/webcast/feed/?" + urllib.parse.urlencode(q_feed))
        feed = _app_request(feed_url, _min_jar())
        rid = aid = sec = ""
        for item in (feed.get("data") or []):
            if not isinstance(item, dict):
                continue
            d = item.get("data") or {}
            owner = d.get("owner") or {}
            rid = d.get("id_str") or ""
            aid = owner.get("id_str") or str(owner.get("id") or "")
            sec = owner.get("sec_uid") or ""
            if rid and aid and sec and (d.get("status") in (0, 2)):
                break
        if not (rid and aid and sec):
            return {"ok": False, "reason": "feed 未取到在播房间身份"}
        q = _asp._common_q()
        q.update({"anchor_id": aid, "room_id": rid, "sec_anchor_id": sec,
                  "sec_user_id": sec, "ranklist_type": "3"})
        url = ("https://webcast.amemv.com/webcast/ranklist/popularity/?"
               + urllib.parse.urlencode(q))
        body = _app_request(url, _min_jar())
        if body.get("status_code") != 0:
            return {"ok": False, "reason": f"sc={body.get('status_code')}"}
        data = body.get("data") or {}
        rows = []
        for r in (data.get("ranks") or [])[:count]:
            u = None
            users = r.get("users")
            if isinstance(users, list) and users:
                u = users[0] or {}
            if not u:
                u = r.get("user") or {}
            tags = [str(t) for t in (r.get("tags") or []) if t]
            helper = r.get("contributor_text") or ""
            if helper:
                tags.append(helper)
            rows.append({
                "rank": r.get("rank") if r.get("rank") is not None else r.get("index"),
                "nickname": u.get("nickname") or "",
                "sec_uid": u.get("sec_uid") or "",
                "score": r.get("score") or 0,
                # 官方人气值串 ("100万+" 封顶 / "88.3万" 精确), exact 由解析标定
                "score_str": r.get("gap_description") or "",
                "tags": tags,
            })
        _resolve_popularity_scores(rows)
        return {"ok": True, "rows": rows}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)[:120]}


# ---- 小时榜/人气榜探测备注 ----
# 房间身份提取 (probe20 配方, 探测脚本用): 房间页 HTML 数据 blob 里
# "roomId":"<15+位>" 与主播 "sec_uid":"MS4wLjABAAAA..."


async def _tab_cap(response, sink: list) -> None:
    """降级点 tab 路径的 hour_detail 捕获器 (sink 为外层 list)。"""
    try:
        if "/webcast/ranklist/hour_detail/" in response.url:
            sink.append(await response.json())
    except Exception:
        pass


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
            # 等进房自动请求 (audience + hour_entrance, 携完整签名参数)
            for _ in range(max(1, timeout_ms // 300)):
                if "audience" in captured and "entrance" in captured:
                    break
                await asyncio.sleep(0.3)
            # 页面渲染可能很慢 (实测 6s 时常卡"直播加载中", 10s+ 才出榜入口)
            for _ in range(10):
                if await page.evaluate(
                    '() => (document.body.innerText||"").includes("小时榜")'
                ):
                    break
                await asyncio.sleep(1)

            # ---- 榜单获取主链路: 点入口一次 + 页内 rt 重放 (2026-11-15 实测定论) ----
            # hour_detail 不进房自动发, 需点 [data-e2e="hour-rank-entrance"] 一次拿到
            # 带签名 URL; 之后换 ranklist_type 页内 fetch 重放有效 (服务端只验
            # a_bogus 与其余参数, rt 可换): 0=小时榜 3=人气榜 6=新人。比反复点 tab
            # 可靠 (榜浮层是 Lynx canvas, tab text 定位常点不中, 也点不出子榜 rt=6)。
            detail_req_urls: list = []
            page.on(
                "request",
                lambda req: detail_req_urls.append(req.url)
                if "/webcast/ranklist/hour_detail/" in req.url else None,
            )
            try:
                await page.locator('[data-e2e="hour-rank-entrance"]').first.click(
                    timeout=5000, force=True)
            except Exception:
                pass  # 无榜入口 (未开播/风控)
            for _ in range(20):
                if detail_req_urls:
                    break
                await asyncio.sleep(0.3)
            base_detail_url = detail_req_urls[0] if detail_req_urls else ""
            replayed: dict[str, list] = {}
            if base_detail_url:
                for key, rt in _RT_BOARDS:
                    u = base_detail_url
                    # rt=0 用原 URL (参数原样, 签名天然有效); 其余换 ranklist_type
                    if rt != "0":
                        import re as _re
                        u = _re.sub(r"ranklist_type=\d+", f"ranklist_type={rt}",
                                    base_detail_url)
                        if u == base_detail_url:  # URL 无该参数则追加
                            u = base_detail_url + f"&ranklist_type={rt}"
                    try:
                        body = await page.evaluate(
                            """async (u) => {
                              const r = await fetch(u, {credentials: 'include'});
                              return await r.json();
                            }""", u)
                        d = body.get("data") or {}
                        rows = d.get("ranks") or []
                        if rows:
                            replayed[key] = rows
                    except Exception:
                        continue

            # ---- 降级: 重放全失败才点 tab (Lynx 浮层 trusted click) ----
            if not replayed:
                hd: list = []
                page.on("response",
                        lambda r: asyncio.create_task(_tab_cap(r, hd)))
                for _name, _label in _LIVE_TABS:
                    try:
                        await page.locator(f"text={_name}").last.click(timeout=4000, force=True)
                    except Exception:
                        continue  # 房间无该榜 tab
                    before = len(hd)
                    for _ in range(20):  # 最多 ~6s 等响应
                        if len(hd) > before:
                            break
                        await asyncio.sleep(0.3)
                for h in hd:
                    d = h.get("data") or {}
                    rows = d.get("ranks") or []
                    if rows:
                        # tab_info 有 exist_tabs = 小时榜; null = 人气榜
                        replayed["hour" if d.get("tab_info") is not None else "pop"] = rows

            try:
                await page.close()
            except Exception:
                pass
            return _summarize_live(captured, replayed, web_rid)
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


def find_live_room() -> dict[str, Any]:
    """自动找一个在播房间 web_rid (小时榜/人气榜是全站榜, 无需用户输地址):
    广场页 HTML 抓 live.douyin.com/<数字> 链接。
    返回 {"ok", "web_rid", "source"} 或 {"ok": False, "reason"}。"""
    try:
        return asyncio.run(_find_live_room_async())
    except Exception as exc:
        return {"ok": False, "reason": str(exc)[:120]}


async def _find_live_room_async() -> dict[str, Any]:
    async with async_playwright() as p:
        browser, context, page, owns = await _acquire_page(p, 20000)
        try:
            try:
                await page.goto("https://live.douyin.com", wait_until="domcontentloaded",
                                timeout=20000)
            except Exception:
                pass
            for _ in range(8):
                html = await page.content()
                rid = _plaza_rid(html)
                if rid:
                    return {"ok": True, "web_rid": rid, "source": "plaza"}
                await asyncio.sleep(1.5)
            return {"ok": False, "reason": "广场未抓到在播房间"}
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


def _plaza_rid(html: str) -> str:
    import re
    m = re.search(r"live\.douyin\.com[\"'/=]+(\d{6,})", html)
    if m:
        return m.group(1)
    m = re.search(r'"web_rid":"(\d{6,})"', html)
    return m.group(1) if m else ""


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


def _summarize_live(captured: dict[str, Any], replayed: dict[str, list],
                    web_rid: str) -> dict[str, Any]:
    if "audience" not in captured and "entrance" not in captured and not replayed:
        return {"ok": False, "reason": "未拦到榜单请求 (房间不存在/未开播/风控)"}

    aud_data = (captured.get("audience") or {}).get("data") or {}
    ent_data = (captured.get("entrance") or {}).get("data") or {}

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
        "hour_board": _board_rows(replayed.get("hour") or []),
        "popularity_board": _board_rows(replayed.get("pop") or []),
        # web 链行无有效数值 (probe73/74: 全零占位); 真值由 app.py 用 app 链
        # rt=12 delta 覆盖 (hour_deltas + hour_board delta 合并)
    }


# ---- 全抖音榜 (app 域 unidbg 离线签名, 2026-11-15 实测通) ----
# unidbg 签名单次 ~12s, 每个请求都要单独签 (a_bogus 绑 URL, 不能复用)。

def _min_jar() -> str:
    """最小双键罐 (install_id + sessionid, e112 定论)。"""
    files = _asp._read_lab_files(_asp._lab_root())
    ck_pairs = [p.strip() for p in files["cookie"].strip().split(";") if "=" in p]
    ck_map = {p.split("=", 1)[0]: p.split("=", 1)[1] for p in ck_pairs}
    return f"install_id={ck_map['install_id']}\r\nsessionid={ck_map['sessionid']}"


def _app_request(url: str, min_jar: str) -> dict[str, Any]:
    """unidbg 签头 + GET app 域 URL, 返回 JSON。"""
    heads = _asp._sign_unidbg(_asp._lab_root(), url, min_jar)
    files = _asp._read_lab_files(_asp._lab_root())
    headers = dict(json.loads(files["headers"]))
    headers["Cookie"] = files["cookie"].strip().replace("\r\n", "; ")
    headers["User-Agent"] = ("com.ss.android.ugc.aweme/400601 (Linux; U; Android 11; "
                             "zh_CN; Pixel 2 XL; Build/RP1A.201005.004.A1)")
    headers.update(heads)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=25, context=SSL_CTX_INSECURE) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def fetch_fans_club_board(count: int = 50) -> dict[str, Any]:
    """全抖音粉丝团人气榜: GET webcast.amemv.com/webcast/ranklist/fans_club/
    (unidbg 签名, 2026-11-15 实测 sc=0)。
    ranks[]{rank, user{nickname}, score} + currency(人气) + period_description。"""
    if _asp is None:
        return {"ok": False, "reason": "app 签名链不可用"}
    try:
        q = _asp._common_q(offset=0, count=count)
        url = ("https://webcast.amemv.com/webcast/ranklist/fans_club/?"
               + urllib.parse.urlencode(q))
        body = _app_request(url, _min_jar())
        if body.get("status_code") != 0:
            return {"ok": False, "reason": f"sc={body.get('status_code')}"}
        data = body.get("data") or {}
        rows = []
        for r in (data.get("ranks") or [])[:count]:
            u = r.get("user") or {}
            rows.append({
                "rank": r.get("rank"),
                "nickname": u.get("nickname") or "",
                "sec_uid": u.get("sec_uid") or "",
                "score": r.get("score") or 0,
                "score_str": "",
            })
        return {
            "ok": True,
            "rows": rows,
            "period": data.get("period_description") or "",
            "currency": data.get("currency") or "",
        }
    except Exception as exc:
        return {"ok": False, "reason": str(exc)[:120]}


def fetch_city_rooms(city_code: str, count: int = 20) -> dict[str, Any]:
    """地区直播榜: GET webcast.amemv.com/webcast/feed/ 带 city_code
    (城市级, 如郑州 410100 / 广州 440100; 2026-11-15 实测两城列表完全不同,
    确认 city_code 真过滤)。房间按 total_user (在看人数) 降序即地区人气榜。"""
    if _asp is None:
        return {"ok": False, "reason": "app 签名链不可用"}
    try:
        q = _asp._common_q(city_code=str(city_code), count=count)
        url = ("https://webcast.amemv.com/webcast/feed/?" + urllib.parse.urlencode(q))
        body = _app_request(url, _min_jar())
        if body.get("status_code") != 0:
            return {"ok": False, "reason": f"sc={body.get('status_code')}"}
        rooms = []
        for item in (body.get("data") or []):
            if not isinstance(item, dict):
                continue
            d = item.get("data") or {}
            owner = d.get("owner") or {}
            stats = d.get("stats") or {}
            rooms.append({
                "room_id": d.get("id_str") or "",
                "title": d.get("title") or "",
                "nickname": owner.get("nickname") or "",
                "sec_uid": owner.get("sec_uid") or "",
                "user_count": d.get("user_count") or 0,
                "viewers": stats.get("total_user") or d.get("user_count") or 0,
            })
        rooms.sort(key=lambda r: r["viewers"], reverse=True)
        return {"ok": True, "rows": rooms[:count]}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)[:120]}


# 地级市列表缓存 (city/list 响应 ~百 KB, 24h 内复用)
_CITY_LIST_CACHE: dict[str, Any] = {"ts": 0.0, "cities": []}
_CITY_LIST_TTL_S = 86400


def fetch_city_list() -> dict[str, Any]:
    """地级市 code+name 映射 (地区榜城市下拉): GET aweme.snssdk.com/aweme/v1/city/list/
    2026-11-15 实测 sc=0, all[]{code,name,districts[]}; 只留地级市 (去重 code)。"""
    import time as _time
    now = _time.time()
    if _CITY_LIST_CACHE["cities"] and now - _CITY_LIST_CACHE["ts"] < _CITY_LIST_TTL_S:
        return {"ok": True, "cities": _CITY_LIST_CACHE["cities"]}
    if _asp is None:
        return {"ok": False, "reason": "app 签名链不可用"}
    try:
        q = _asp._common_q()
        url = ("https://aweme.snssdk.com/aweme/v1/city/list/?" + urllib.parse.urlencode(q))
        body = _app_request(url, _min_jar())
        if body.get("status_code") != 0:
            return {"ok": False, "reason": f"sc={body.get('status_code')}"}
        # 实测层级: all[] 即地级市列表 (code=地级市, name=市名),
        # districts[] 是区县 (code 重复市的 code, district_code 才是区县) — 只取 all 顶层。
        seen: set[str] = set()
        cities = []
        for city in (body.get("all") or []):
            code = str(city.get("code") or "")
            name = city.get("name") or ""
            if code and name and code not in seen:
                seen.add(code)
                cities.append({"code": code, "name": name})
        _CITY_LIST_CACHE["ts"] = now
        _CITY_LIST_CACHE["cities"] = cities
        return {"ok": True, "cities": cities}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)[:120]}
