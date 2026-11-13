"""dy1 喜欢/收藏列表 (CDP Chrome 登录态 DOM 采集).

2026-11-13 用户需求: 在 douyin 面板展示当前登录账户 (仅 dy1) 的点赞/收藏视频,
封面 + 标题 + 复制链接 + 跳转。链接必须是 /video/<id> 直链 (未登录态可看),
不用喜欢列表聚合链接 (需登录)。

数据链 (2026-11-13 实测定案):
  - monitor 页面池: 无 douyin 登录 cookie (0 cookies) → 不可用
  - 常驻 CDP Chrome (9222): douyin sessionid 在 → 唯一可用登录态
  - /user/self?showTab=like URL 参数不生效 (SPA 忽略) → 必须 UI 点击"喜欢"tab
  - 喜欢 tab 数据不出 XHR (无 aweme list 请求) → DOM 卡片是唯一稳定源
  - 卡片 a[href] = /video/<id> 或 /note/<id> 直链 + <img> 封面 + alt=作者:标题
    正好满足"直链"要求; img.alt 形状 "作者名：文案"
  - 收藏 tab 同理 (tab 文本"收藏")

翻页: 页内滚动加载; 无限滚动无总数, 以 max_items 截断。
注意: 只读操作, 只开新 page 用完即关, 不碰登录态本身。
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from .config import CDP_PROBE_URL  # noqa: F401  (探活一致性引用)
from urllib.parse import urlparse
import httpx

CDP = "http://127.0.0.1:9222"

_TABS = {
    "like": "喜欢",
    "collect": "收藏",
}


def cdp_alive(timeout: float = 1.5) -> bool:
    try:
        r = httpx.get(f"{CDP}/json/version", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def _normalize_video_url(href: str) -> str:
    """//www.douyin.com/video/x → https://www.douyin.com/video/x; 保留直链形状."""
    if href.startswith("//"):
        href = "https:" + href
    if href.startswith("/"):
        href = "https://www.douyin.com" + href
    # 只留直链部分 (去 query/fragment)
    return href.split("?")[0].split("#")[0]


def fetch_liked_items(kind: str = "like", max_items: int = 60,
                      scrolls: int = 10) -> dict[str, Any]:
    """拉 dy1 登录态的喜欢/收藏视频卡片.

    kind: "like" (喜欢) | "collect" (收藏)
    返回 {ok, via, items: [{aweme_id, url, cover, title, author}], total, error}
    """
    tab_text = _TABS.get(kind)
    if not tab_text:
        raise ValueError(f"kind must be one of {list(_TABS)}")

    out: dict[str, Any] = {"ok": False, "via": "cdp-dom", "kind": kind,
                           "items": [], "total": 0, "error": ""}

    async def _run():
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(CDP)
            context = browser.contexts[0]
            page = await context.new_page()
            seen: set[str] = set()

            async def collect():
                cards = await page.evaluate("""() => {
                  const out = [];
                  document.querySelectorAll('a[href]').forEach(a => {
                    const h = a.getAttribute('href') || '';
                    if (h.includes('/video/') || h.includes('/note/')) {
                      const img = a.querySelector('img');
                      out.push({href: h, cover: img ? (img.src || '') : '',
                                alt: img ? (img.alt || '') : ''});
                    }
                  });
                  return out;
                }""")
                added = 0
                for c in cards:
                    url = _normalize_video_url(c["href"])
                    m = re.search(r"/(?:video|note)/(\d+)", url)
                    if not m or url in seen:
                        continue
                    seen.add(url)
                    # img.alt 形状 "作者：文案" (全角冒号); 兜底用无冒号原文
                    alt = (c["alt"] or "").strip()
                    if "：" in alt:
                        author, title = alt.split("：", 1)
                    elif ":" in alt:
                        author, title = alt.split(":", 1)
                    else:
                        author, title = "", alt
                    out["items"].append({
                        "aweme_id": m.group(1),
                        "url": url,
                        "cover": c["cover"] or "",
                        "author": author.strip(),
                        "title": title.strip(),
                    })
                    added += 1
                return added

            try:
                await page.goto("https://www.douyin.com/user/self",
                                wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(6000)
                # 真实点击 tab (SPA 忽略 showTab URL 参数, 必须点)
                clicked = await page.evaluate("""(tabText) => {
                  const els = Array.from(document.querySelectorAll('span,div,a'));
                  const t = els.find(e => e.textContent.trim()===tabText && e.children.length===0);
                  if (!t) return false;
                  t.click(); return true;
                }""", tab_text)
                if not clicked:
                    out["error"] = f"self 页没找到“{tab_text}”tab (登录态可能失效)"
                    return
                await page.wait_for_timeout(5000)
                # 滚动加载翻页
                for _ in range(max(1, scrolls)):
                    await collect()
                    if len(seen) >= max_items:
                        break
                    stale = len(seen)
                    await page.mouse.wheel(0, 5000)
                    await page.wait_for_timeout(2200)
                    if len(seen) == stale and _ >= 3:
                        break
                await collect()
                out["ok"] = bool(out["items"])
                if not out["items"]:
                    out["error"] = f"“{tab_text}”tab 无卡片 (可能列表为空或被风控)"
            finally:
                try:
                    await page.close()
                except Exception:
                    pass

    def _bridge():
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(_run())
            return
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(1) as ex:
            ex.submit(lambda: asyncio.run(_run())).result(timeout=300)

    _bridge()
    out["total"] = len(out["items"])
    return out
