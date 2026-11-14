"""dy1/多账户 喜欢/收藏列表 (CDP Chrome 登录态 DOM 采集).

用户诉求 (2026-11-13 / 11-14):
  面板展示当前登录账户的点赞/收藏视频, 封面 + 标题 + 复制链接 + 跳转。
  链接必须是 /video/<id> 直链 (未登录态可看), 不用喜欢列表聚合链接 (需登录)。

数据链 (2026-11-13 实测定案):
  - monitor 页面池: 无 douyin 登录 cookie → 不可用
  - 常驻 CDP Chrome (9222): douyin sessionid 在 → 唯一可用登录态
  - /user/self?showTab=like URL 参数不生效 (SPA 忽略) → 必须 UI 点击"喜欢"tab
  - 喜欢 tab 数据不出 XHR (无 aweme list 请求) → DOM 卡片是唯一稳定源

2026-11-14 修两个实测 bug (探针 probe2/probe4 定位):
  1) **页脚 SEO 链接混入** (用户反馈"最后几项完全是杂乱链接"):
     全页扫 a[href] 会把 <footer.user-page-footer> 里的 SEO 链接一起吃进来,
     它们形状是 https://.../video/<id>?source=Baiduspider, 无 img、尺寸 600x14,
     且排在最后 → 正是"最后几项杂乱"。判据改为三重过滤:
       有封面 img + 不在 footer 内 + 卡片尺寸 >=80x80 (真卡片实测统一 159x241)。
  2) **滚动不加载** (用户反馈"不展示完全"):
     滚 window 无效 (实测 window sh=900=ch, 不可滚)。真实滚动容器是
     div.parent-route-container (route-scroll-container, 实测 3668/900)。
     滚对容器后: 54 → 493 张 (12 轮)。

分页 (用户 2026-11-14 定案):
  喜欢列表可能上千条, 不必一次全拉。策略 = 首屏 16 + 后台默认补到 40 (三排24 的
  余量) + 用户点"继续加载"时按需继续滚。故本模块暴露 target 参数, 由调用方
  决定这次要多少张; 结果按 max(已拉) 累积缓存, 不重复重拉。

翻页: 页内滚动加载; 无限滚动无总数, 以 target 截断。
注意: 只读操作, 只开新 page 用完即关, 不碰登录态本身。
"""
from __future__ import annotations

import re
from typing import Any, Callable

from .config import CDP_PROBE_URL  # noqa: F401  (探活一致性引用)
import httpx

CDP = "http://127.0.0.1:9222"

_TABS = {
    "like": "喜欢",
    "collect": "收藏",
}

# 真卡片三判据 (2026-11-14 实测): 有封面 + 不在 footer + 有卡片尺寸
# 真卡片实测统一 159x241; 页脚 SEO 链接 600x14 / 0x0 且无 img。
_COLLECT_JS = r"""() => {
  const out = [];
  document.querySelectorAll('a[href]').forEach(a => {
    const h = a.getAttribute('href') || '';
    if (!/\/(video|note)\/\d+/.test(h)) return;
    const img = a.querySelector('img');
    if (!img) return;                                   // 判据1: 必须有封面图
    let p = a, inFooter = false;
    for (let i = 0; i < 12 && p; i++) {
      if (String(p.tagName).toLowerCase() === 'footer') { inFooter = true; break; }
      p = p.parentElement;
    }
    if (inFooter) return;                               // 判据2: 排除页脚 SEO 链接
    const r = a.getBoundingClientRect();
    if (r.width < 80 || r.height < 80) return;          // 判据3: 卡片应有尺寸
    out.push({ href: h, cover: img.src || '', alt: img.alt || '' });
  });
  return out;
}"""

# 找页面真实滚动容器 (window 常不可滚; SPA 用内部 route-scroll-container)
_SCROLLER_JS = r"""() => {
  const cs = [...document.querySelectorAll('*')].filter(e => {
    const s = getComputedStyle(e);
    return /auto|scroll/.test(s.overflowY) && e.scrollHeight > e.clientHeight + 100;
  }).sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
  window.__likedScroller = cs[0] || null;
  return cs.length;
}"""

_SCROLL_JS = r"""() => {
  const s = window.__likedScroller;
  if (s) s.scrollTop = s.scrollHeight;
  window.scrollTo(0, document.body.scrollHeight);
}"""


def cdp_alive(timeout: float = 1.5) -> bool:
    try:
        r = httpx.get(f"{CDP}/json/version", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def _normalize_video_url(href: str) -> str:
    """//www.douyin.com/video/x → https://www.douyin.com/video/x; 只留直链."""
    if href.startswith("//"):
        href = "https:" + href
    if href.startswith("/"):
        href = "https://www.douyin.com" + href
    return href.split("?")[0].split("#")[0]


def _split_alt(alt: str) -> tuple[str, str]:
    """img.alt 形状 "作者：文案" (全角冒号); 兜底半角; 再兜底全是标题."""
    alt = (alt or "").strip()
    if "：" in alt:
        a, t = alt.split("：", 1)
        return a.strip(), t.strip()
    if ":" in alt:
        a, t = alt.split(":", 1)
        return a.strip(), t.strip()
    return "", alt


def _bridge(coro_factory, timeout: float = 300.0):
    """在无事件循环的线程里跑 async playwright (后端为同步 FastAPI 端点)。"""
    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(1) as ex:
        return ex.submit(lambda: asyncio.run(coro_factory())).result(timeout=timeout)


def fetch_liked_items(kind: str = "like", target: int = 16,
                      on_progress: Callable[..., None] | None = None,
                      query: str = "", author: str = "",
                      should_stop: Callable[[], bool] | None = None) -> dict[str, Any]:
    """拉当前登录态的喜欢/收藏卡片, 最多 target 张 (滚动到够为止)。

    kind:   "like"(喜欢) | "collect"(收藏)
    target: 本次要拉到多少张 (调用方按"首屏16/后台40/继续加载+8"决定)
    on_progress: 每滚一轮回调 on_progress(已扫过, 已匹配) (供后端写进度)
    query/author: 定向模式 — 边滚边匹配 (标题含query 或 作者前缀author),
                  凑够 target 个**匹配项**即停 (不必拉全量)。
    should_stop: 每轮滚动前探针, True 即提前退出 (用户点终止, 已扫到的保留)。
    返回 {ok, via, kind, items, total, error, cancelled}
    """
    tab_text = _TABS.get(kind)
    if not tab_text:
        raise ValueError(f"kind must be one of {list(_TABS)}")
    target = max(1, int(target))

    out: dict[str, Any] = {"ok": False, "via": "cdp-dom", "kind": kind,
                           "items": [], "total": 0, "error": "", "cancelled": False}
    seen: dict[str, dict] = {}   # 闭包共享: _run 内 collect() 写入, 外层读取 (勿放 _run 内)

    async def _run():
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(CDP)
            context = browser.contexts[0]
            page = await context.new_page()

            q_l = (query or "").strip().lower()
            a_l = (author or "").strip().lower()

            def matches(it: dict) -> bool:
                if not q_l and not a_l:
                    return True
                t = (it.get("title") or "").lower()
                au = (it.get("author") or "").lower()
                if a_l and not au.startswith(a_l):
                    return False
                if q_l and q_l not in t and q_l not in au:
                    return False
                return True

            async def collect() -> int:
                cards = await page.evaluate(_COLLECT_JS)
                for c in cards:
                    url = _normalize_video_url(c["href"])
                    m = re.search(r"/(?:video|note)/(\d+)", url)
                    if not m or url in seen:
                        continue
                    author_, title = _split_alt(c["alt"])
                    seen[url] = {
                        "aweme_id": m.group(1),
                        "url": url,
                        "cover": c["cover"] or "",
                        "author": author_,
                        "title": title,
                    }
                return len(seen)

            def matched_n() -> int:
                return sum(1 for v in seen.values() if matches(v))

            try:
                await page.goto("https://www.douyin.com/user/self",
                                wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(6000)
                # 真实点击 tab (SPA 忽略 showTab URL 参数, 必须点)
                clicked = await page.evaluate("""(tabText) => {
                  const els = Array.from(document.querySelectorAll('span,div,a'));
                  const t = els.find(e => e.textContent.trim() === tabText && e.children.length === 0);
                  if (!t) return false;
                  t.click();
                  return true;
                }""", tab_text)
                if not clicked:
                    out["error"] = f"self 页没找到“{tab_text}”tab (登录态可能失效)"
                    return
                await page.wait_for_timeout(5000)
                await collect()
                if on_progress:
                    on_progress(len(seen), matched_n())

                # 反复滚真实容器直到够数或不再增长 (2026-11-14: window 不可滚, 必须滚容器)
                stalls = 0
                for i in range(120):
                    if should_stop is not None and should_stop():
                        out["cancelled"] = True
                        break
                    goal = matched_n() if (q_l or a_l) else len(seen)
                    if goal >= target:
                        break
                    await page.evaluate(_SCROLLER_JS)
                    await page.evaluate(_SCROLL_JS)
                    try:
                        await page.keyboard.press("End")
                    except Exception:
                        pass
                    await page.wait_for_timeout(2200)
                    before = len(seen)
                    await collect()
                    if on_progress:
                        on_progress(len(seen), matched_n())
                    if len(seen) == before:
                        stalls += 1
                        if stalls >= 3:
                            break        # 连续 3 轮无新增 = 到底了
                    else:
                        stalls = 0

                # 定向模式只回传匹配项; 顺序 = 抖音页面扫描序 (dict 保序, 新滚到的在后)
                items = [v for v in seen.values() if matches(v)] if (q_l or a_l) \
                    else list(seen.values())
                out["items"] = items
                out["total"] = len(items)
                out["ok"] = bool(items) or bool(out.get("cancelled"))
                if not seen:
                    out["error"] = f"“{tab_text}”tab 无卡片 (可能列表为空或被风控)"
                elif (q_l or a_l) and not items:
                    out["error"] = f"滚动范围内无匹配 (扫过 {len(seen)} 个)"
                return
            finally:
                try:
                    await page.close()
                except Exception:
                    pass

    _bridge(_run, timeout=420)
    out["total"] = len(out["items"])
    return out
