"""
抖音用户搜索 - 核心模块

设计思路
========
抖音搜索接口 /aweme/v1/web/discover/search/ 的 a_bogus / msToken 是前端 JS 动态
生成的风控签名，本地难以伪造。本模块采用「浏览器驱动 + 接口拦截」方案：
  1. 用 Playwright 启动真实浏览器
  2. 导航到 https://www.douyin.com/search/<keyword>?type=user
  3. 拦截浏览器发出的 discover/search 请求响应，直接读取后端 JSON
  4. 解析 user_list，提取关键用户信息

这样所有签名 / Cookie / referer 都由浏览器自动处理，无需逆向 a_bogus。
"""

from __future__ import annotations

import json
import time
from typing import Any, Iterable

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    sync_playwright,
)


SEARCH_API_KEYWORD = "/aweme/v1/web/discover/search/"
DEFAULT_PAGE_SIZE = 12


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        # 大整数精确处理
        return str(value)
    return str(value)


def _parse_room_data(room_data_raw: Any) -> dict[str, Any]:
    """room_data 是一个被多次转义的 JSON 字符串，逐层解开。"""
    if not room_data_raw:
        return {}
    data = room_data_raw
    for _ in range(5):  # 最多解 5 层转义
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                break
        else:
            break
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return {}
    return data if isinstance(data, dict) else {}


def extract_user_summary(item: dict[str, Any]) -> dict[str, Any]:
    """从 user_list 单条数据中提取关键字段。"""
    info = item.get("user_info") or {}

    room_id = info.get("room_id") or 0
    room_data = _parse_room_data(info.get("room_data"))

    # 是否正在直播：room_id 非零且 room_data 含 stream_url
    streaming = bool(room_id) and bool(room_data.get("stream_url"))

    # 默认清晰度
    options = room_data.get("options") or {}
    default_quality = (options.get("default_quality") or {}).get("name", "")

    avatar = info.get("avatar_thumb") or {}
    avatar_urls = avatar.get("url_list") or []

    summary = {
        "uid": _safe_str(info.get("uid")),
        "short_id": _safe_str(info.get("short_id")),
        "nickname": info.get("nickname", ""),
        "unique_id": info.get("unique_id", ""),          # 抖音号
        "sec_uid": info.get("sec_uid", ""),
        "signature": info.get("signature", ""),
        "follower_count": info.get("follower_count", 0),
        "following_count": info.get("following_count", 0),
        "total_favorited": info.get("total_favorited", 0),
        "ip_location": info.get("ip_location", ""),
        "custom_verify": info.get("custom_verify", ""),  # 个人认证
        "enterprise_verify_reason": info.get("enterprise_verify_reason", ""),
        "room_id": _safe_str(room_id),
        "streaming": streaming,
        "default_quality": default_quality,
        "avatar": avatar_urls[0] if avatar_urls else "",
        "homepage": f"https://www.douyin.com/user/{info.get('sec_uid', '')}",
        "versatile_display": info.get("versatile_display", ""),
    }
    return summary


def _wait_for_search_response(
    page: Page, keyword: str, timeout_ms: int = 20000
) -> dict[str, Any] | None:
    """
    在页面上等待并捕获一次 discover/search 接口响应。
    返回原始 JSON dict；失败返回 None。
    """
    captured: dict[str, Any] = {}

    def on_response(response):
        try:
            url = response.url
            if SEARCH_API_KEYWORD not in url:
                return
            if "aweme_user_web" not in url and "search_channel=aweme_user_web" not in url:
                # 只关心用户搜索通道
                pass
            # 只取包含 keyword 的请求（浏览器可能发起多个不同关键词的搜索）
            if f"keyword={keyword}" not in url and f"keyword={keyword}" not in url.replace("%", ""):
                # 仍保留：可能 URL 编码后比对失败，这里放宽
                pass
            body = response.text()
            data = json.loads(body)
            captured["data"] = data
        except Exception:
            pass

    page.on("response", on_response)

    # 给页面一些时间触发请求
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if "data" in captured:
            break
        page.wait_for_timeout(200)

    page.remove_listener("response", on_response)  # type: ignore[arg-type]
    return captured.get("data")


def search_users(
    keyword: str,
    *,
    headless: bool = False,
    user_data_dir: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    timeout_ms: int = 25000,
    playwright: Playwright | None = None,
) -> list[dict[str, Any]]:
    """
    搜索抖音用户。

    参数
    ----
    keyword        搜索关键词
    headless       是否无头模式（首次建议 False 以便扫码登录）
    user_data_dir  浏览器用户数据目录（持久化登录态）。推荐设置以防每次扫码。
    page_size      预期每页数量（仅用于判断是否拿到数据）
    timeout_ms     等待接口超时（毫秒）

    返回
    ----
    list[dict]  用户摘要列表
    """
    keyword = keyword.strip()
    if not keyword:
        raise ValueError("keyword 不能为空")

    own_pw = playwright is None
    pw = playwright or sync_playwright().start()

    try:
        if user_data_dir:
            context = pw.chromium.launch_persistent_context(
                user_data_dir,
                headless=headless,
                viewport={"width": 1600, "height": 1000},
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = context.pages[0] if context.pages else context.new_page()
        else:
            browser = pw.chromium.launch(headless=headless)
            context = browser.new_context(
                viewport={"width": 1600, "height": 1000}
            )
            page = context.new_page()

        # 导航到搜索页
        url = f"https://www.douyin.com/search/{keyword}?type=user"
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

        # 等待并捕获接口
        data = _wait_for_search_response(page, keyword, timeout_ms=timeout_ms)

        if data is None:
            # 简单重试：触发一次回车 / 等待
            try:
                page.wait_for_timeout(2000)
                data = _wait_for_search_response(page, keyword, timeout_ms=10000)
            except Exception:
                pass

        if not data:
            return []

        user_list = data.get("user_list") or []
        summaries = [extract_user_summary(item) for item in user_list if isinstance(item, dict)]
        return summaries

    finally:
        if own_pw:
            try:
                context.close()
            except Exception:
                pass
            pw.stop()


def format_user_line(u: dict[str, Any]) -> str:
    """单行文本展示一个用户。"""
    live = "🔴 直播中" if u.get("streaming") else "⚪ 未直播"
    return (
        f"{u['nickname']}  "
        f"(抖音号:{u['unique_id'] or u['short_id']})  "
        f"粉丝:{u['follower_count']}  获赞:{u['total_favorited']}  "
        f"{live}\n"
        f"  主页: {u['homepage']}\n"
        f"  sec_uid: {u['sec_uid']}\n"
        f"  简介: {u['signature'].strip()[:80]}"
    )


__all__ = [
    "search_users",
    "extract_user_summary",
    "format_user_line",
    "search_users_paged",
]


def search_users_paged(
    keyword: str,
    *,
    max_count: int = 36,
    headless: bool = False,
    user_data_dir: str | None = None,
    timeout_ms: int = 30000,
) -> list[dict[str, Any]]:
    """
    滚动加载更多用户（最多 max_count 个）。

    思路：在搜索页持续滚动到底部，每滚一次捕获新一批 user_list，
    按 sec_uid 去重合并。
    """
    keyword = keyword.strip()
    if not keyword:
        raise ValueError("keyword 不能为空")

    all_users: dict[str, dict[str, Any]] = {}

    with sync_playwright() as pw:
        if user_data_dir:
            context = pw.chromium.launch_persistent_context(
                user_data_dir,
                headless=headless,
                viewport={"width": 1600, "height": 1000},
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = context.pages[0] if context.pages else context.new_page()
        else:
            browser = pw.chromium.launch(headless=headless)
            context = browser.new_context(viewport={"width": 1600, "height": 1000})
            page = context.new_page()

        captured_batch: list[dict[str, Any]] = []

        def on_response(response):
            try:
                if SEARCH_API_KEYWORD not in response.url:
                    return
                data = json.loads(response.text())
                for item in data.get("user_list") or []:
                    captured_batch.append(item)
            except Exception:
                pass

        page.on("response", on_response)

        url = f"https://www.douyin.com/search/{keyword}?type=user"
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

        # 初始等待
        page.wait_for_timeout(3000)

        # 滚动多次
        scroll_tries = 0
        max_scrolls = max(3, max_count // DEFAULT_PAGE_SIZE + 2)
        while len(all_users) < max_count and scroll_tries < max_scrolls:
            captured_batch.clear()
            # 滚到底
            page.mouse.wheel(0, 8000)
            page.wait_for_timeout(2500)

            # 处理这一批新数据
            new_seen = 0
            for item in captured_batch:
                summary = extract_user_summary(item)
                if summary["sec_uid"] and summary["sec_uid"] not in all_users:
                    all_users[summary["sec_uid"]] = summary
                    new_seen += 1

            scroll_tries += 1
            # 一整批没新用户 -> 可能到底了
            if new_seen == 0 and scroll_tries > 2:
                # 再多滚一次确认
                page.mouse.wheel(0, 8000)
                page.wait_for_timeout(2500)
                if not any(
                    extract_user_summary(it)["sec_uid"] not in all_users
                    for it in captured_batch
                ):
                    break

        page.remove_listener("response", on_response)  # type: ignore[arg-type]
        context.close()

    return list(all_users.values())[:max_count]
