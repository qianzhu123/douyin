from __future__ import annotations

import asyncio
import json
import math
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from .config import (
    DOWNLOAD_OUTPUT_DIR,
    DOWNLOAD_TIMEOUT,
    DOWNLOADER_DIR,
    MONITOR_DIR,
    PROFILE_CACHE_FILE,
    PROJECT_ROOT,
    SEARCH_DIR,
    SEARCH_HEADLESS,
    SEARCH_PROFILE_DIR,
    SETTINGS_FILE,
    USERS_FILE,
)
from .schemas import (
    AddUserResult,
    AppSettings,
    DownloadJob,
    ProfileResult,
    SearchCandidate,
    UserEntry,
    now_iso,
)
from .tool_loader import load_module
from .live_room import LiveRoomService
from .download_runner import run_download_subprocess

try:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover - dependency is declared for runtime
    PlaywrightTimeoutError = TimeoutError
    async_playwright = None


# --- watch 轮询整体移除 (2026-11-05, 用户定论: 与直播监控功能重复, 只留监控) ---
# 原始 watch 状态类与轮询循环已删; 定时盯直播由 LiveMonitor (live_monitor.py) 承担,
# 定时刷基本信息未来如需再在 LiveMonitor 上加 basic 快照类型。


class MonitorService:
    def __init__(self) -> None:
        self.module = load_module("douyin_monitor_tool", MONITOR_DIR / "main.py")
        self.search_module = load_module("douyin_user_search_tool", SEARCH_DIR / "douyin_search.py") if (SEARCH_DIR / "douyin_search.py").exists() else None
        self.raw_search_module = load_module("douyin_user_raw_search_tool", SEARCH_DIR / "raw.py") if (SEARCH_DIR / "raw.py").exists() else None
        ensure_project_users(USERS_FILE, self.module.parse_settings())
        self._search_lock = asyncio.Lock()
        self.live_room = LiveRoomService()
        self._live_room_sem = asyncio.Semaphore(3)

    @staticmethod
    def _cdp_alive(timeout: float = 1.5) -> bool:
        """常驻 Chrome 9222 探活 (与 live_room._cdp_alive 同源)。"""
        try:
            import httpx
            resp = httpx.get("http://127.0.0.1:9222/json/version", timeout=timeout)
            return resp.status_code == 200
        except Exception:
            return False

    def list_users(self) -> list[UserEntry]:
        cache = read_profile_cache(PROFILE_CACHE_FILE)
        users = []
        for user in read_project_users(USERS_FILE):
            cached = cache.get(user.sec_uid, {})
            cached_profile = cached.get("profile") if isinstance(cached.get("profile"), dict) else None
            if cached_profile:
                cached_profile = normalize_live_timing(
                    cached_profile,
                    cached_profile,
                    str(cached.get("last_checked_at") or now_iso()),
                )
            users.append(
                UserEntry(
                    **{
                        **user.model_dump(),
                        "last_checked_at": str(cached.get("last_checked_at") or ""),
                        "last_ok": cached.get("ok") if "ok" in cached else None,
                        "last_error": str(cached.get("error") or ""),
                        "last_profile": cached_profile,
                    }
                )
            )
        return users

    async def search_users(self, keyword: str) -> list[SearchCandidate]:
        keyword = keyword.strip()
        if not keyword:
            raise ValueError("Search keyword is required.")
        if async_playwright is None:
            raise RuntimeError("Playwright is not installed.")

        async with self._search_lock:
            return await self._search_users_locked(keyword)

    async def _search_users_locked(self, keyword: str) -> list[SearchCandidate]:
        # 协议通道首选 (2026-11-08): App 域 oracle 签名直发, 不依赖浏览器/登录态。
        # 不可达 (真机不在/签名失败/antispam 壳) 静默回落浏览器三级链, 行为不变。
        try:
            from . import aweme_search_protocol as proto
            users, note = await asyncio.to_thread(proto.search, keyword)
            candidates = [SearchCandidate(
                nickname=u.get("nickname", ""), unique_id=u.get("unique_id", ""),
                signature=u.get("signature", ""), avatar_url=u.get("avatar_url", ""),
                follower_count=int(u.get("follower_count") or 0),
                following_count=int(u.get("following_count") or 0),
                total_favorited=int(u.get("total_favorited") or 0),
                ip_location="", sec_uid=u["sec_uid"],
                homepage_url=f"https://www.douyin.com/user/{u['sec_uid']}",
                live_status=int(u.get("live_status") or 0),
                room_id=u.get("room_id", ""),
            ) for u in users][:12]
            # 粉丝数补齐 (2026-11-13): general 搜索只给精确命中粉丝数, 泛化候选
            # follower_count 恒 0 (服务器端脱敏)。复用主播监控的 profile 工具
            # (常驻 Chromium 池 + user/profile/other XHR) 并行补齐 fans=0 的候选。
            need = [c.sec_uid for c in candidates if not c.follower_count]
            if need:
                try:
                    # 串行补齐 (2026-11-13 实测: 并行 5 页互踩全 None, 串行 3/3 成功);
                    # 单个 ~7s, 最坏 6 候选 ~40s, _search_with_budget 60s 内。
                    fans_map: dict[str, int] = {}
                    for sec in need:
                        try:
                            info = await asyncio.wait_for(
                                self.module.fetch_profile(sec), timeout=15)
                            if info and info.get("sec_uid"):
                                fans_map[info["sec_uid"]] = int(info.get("follower_count") or 0)
                        except Exception:
                            continue
                    if fans_map:
                        candidates = [
                            c.model_copy(update={"follower_count": fans_map[c.sec_uid]})
                            if c.sec_uid in fans_map else c
                            for c in candidates
                        ]
                except Exception:
                    pass  # 补齐失败不影响候选返回 (显示 0)
            return candidates
        except Exception:
            pass  # 协议通道任何失败 → 浏览器链 (不拖时: 协议内部 5s 超时)

        if self.raw_search_module is not None:
            candidates = await asyncio.to_thread(self._search_users_with_raw_module, keyword)
            if candidates:
                return candidates[:12]

        if self.search_module is not None:
            candidates = await asyncio.to_thread(self._search_users_with_external_module, keyword)
            if candidates:
                return candidates[:12]

        payload: dict[str, Any] | None = None
        try:
            async with async_playwright() as playwright:
                # CDP 优先 (2026-11-05, 同 live_room 收编): 复用常驻 Chrome 9222 免冷启;
                # CDP 不可达才冷启 chromium。探测完只关 page, 常驻浏览器绝不杀。
                browser = None
                owns_browser = False
                context = None
                if self._cdp_alive():
                    try:
                        browser = await playwright.chromium.connect_over_cdp("http://127.0.0.1:9222")
                        context = browser.contexts[0] if browser.contexts else await browser.new_context()
                    except Exception:
                        browser = None
                if browser is None:
                    browser = await playwright.chromium.launch(
                        headless=True,
                        args=["--disable-blink-features=AutomationControlled"],
                    )
                    owns_browser = True
                    context = await browser.new_context(
                        viewport={"width": 1600, "height": 1000},
                        user_agent=self.module.UA,
                    )
                page = await context.new_page()
                try:
                    await page.set_viewport_size({"width": 1600, "height": 1000})
                except Exception:
                    pass
                response_seen = asyncio.Event()

                async def capture_response(response) -> None:
                    nonlocal payload
                    url = response.url
                    if "/aweme/v1/web/discover/search/" not in url or "search_channel=aweme_user_web" not in url:
                        return
                    try:
                        payload = await response.json()
                        response_seen.set()
                    except Exception:
                        return

                page.on("response", lambda response: asyncio.create_task(capture_response(response)))

                try:
                    try:
                        await page.goto(
                            f"https://www.douyin.com/search/{quote(keyword)}?type=user",
                            wait_until="domcontentloaded",
                            timeout=30000,
                        )
                        await asyncio.wait_for(response_seen.wait(), timeout=25)
                    except asyncio.TimeoutError:
                        await page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=30000)
                        await page.locator("input[placeholder*='搜索']").first.fill(keyword, timeout=12000)
                        await page.keyboard.press("Enter")
                        await page.wait_for_timeout(3000)
                        try:
                            await page.get_by_text("用户", exact=True).click(timeout=10000)
                        except PlaywrightTimeoutError:
                            pass
                        try:
                            await asyncio.wait_for(response_seen.wait(), timeout=20)
                        except asyncio.TimeoutError:
                            raise RuntimeError("Timed out waiting for Douyin user search response.")
                finally:
                    try:
                        await page.close()
                    except Exception:
                        pass
                    if owns_browser:
                        try:
                            await context.close()
                        except Exception:
                            pass
                        try:
                            await browser.close()
                        except Exception:
                            pass
        except Exception as exc:
            # 手搓浏览器链任何异常 (输入框超时/页面导航失败) → 可读错误,
            # 不让裸 playwright TimeoutError 冒泡成前端 "Failed to fetch"
            if isinstance(exc, RuntimeError):
                raise
            raise RuntimeError(f"浏览器搜索失败: {type(exc).__name__}: {str(exc)[:120]}") from exc

        if not payload:
            # 全链失败给可读错误 (2026-11-09): 协议通道离线+浏览器链被风控时,
            # 原样抛 playwright TimeoutError 前端只见 "Failed to fetch" 没法排查
            raise RuntimeError(
                "搜索不可用: 协议通道离线 (真机抖音未运行) 且浏览器链无结果 "
                "(douyin.com 未返回搜索响应, 可能登录墙/风控)")
        candidates = parse_search_candidates(payload)
        return candidates[:12]

    def _search_users_with_raw_module(self, keyword: str) -> list[SearchCandidate]:
        raw_items, _diag = self.raw_search_module._collect_raw(
            keyword,
            headless=SEARCH_HEADLESS,
            user_data_dir=str(SEARCH_PROFILE_DIR),
            more=False,
            timeout_ms=30000,
        )
        return parse_search_candidates({"user_list": raw_items})

    def _search_users_with_external_module(self, keyword: str) -> list[SearchCandidate]:
        summaries = self.search_module.search_users(
            keyword,
            headless=SEARCH_HEADLESS,
            user_data_dir=str(SEARCH_PROFILE_DIR),
        )
        return parse_search_summaries(summaries)

    async def _enrich_search_candidates(self, candidates: list[SearchCandidate]) -> list[SearchCandidate]:
        targets = [
            {"label": candidate.nickname, "url": candidate.homepage_url, "sec_uid": candidate.sec_uid}
            for candidate in candidates
        ]
        try:
            profiles = await asyncio.wait_for(self.module.fetch_profiles_parallel(targets), timeout=25)
        except Exception:
            return candidates

        enriched: list[SearchCandidate] = []
        for candidate, profile in zip(candidates, profiles):
            if isinstance(profile, dict):
                update = {}
                for key in ("following_count", "follower_count", "total_favorited", "ip_location"):
                    value = profile.get(key)
                    if value not in (None, ""):
                        update[key] = value
                enriched.append(candidate.model_copy(update=update))
            else:
                enriched.append(candidate)
        return enriched

    def add_user(self, label: str, sec_uid: str, homepage_url: str = "") -> AddUserResult:
        return upsert_project_user(USERS_FILE, {"label": label, "sec_uid": sec_uid, "url": homepage_url})

    def add_user_payload(self, payload: dict[str, Any]) -> AddUserResult:
        return upsert_project_user(USERS_FILE, payload)

    def reorder_users(self, sec_uids: list[str]) -> list[UserEntry]:
        return reorder_project_users(USERS_FILE, sec_uids)

    def delete_user(self, sec_uid: str) -> list[UserEntry]:
        return delete_project_user(USERS_FILE, sec_uid)

    def set_monitor(self, sec_uid: str, monitor: bool) -> list[UserEntry]:
        return set_monitor_flag(USERS_FILE, sec_uid, monitor)

    def resolve_targets(self, target_ids: list[str]) -> list[dict[str, Any]]:
        if not target_ids:
            return [entry.model_dump() for entry in self.list_users()]

        settings = {entry.sec_uid: entry for entry in self.list_users()}
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for target in target_ids:
            target = target.strip()
            entry = settings.get(target)
            if entry and entry.sec_uid not in seen:
                results.append(entry.model_dump())
                seen.add(entry.sec_uid)
                continue
            sec_uid = self.module.extract_sec_uid(target)
            if sec_uid and sec_uid not in seen:
                results.append({"label": "", "url": target, "sec_uid": sec_uid})
                seen.add(sec_uid)
                continue
            for user in settings.values():
                if target.lower() in user.label.lower() and user.sec_uid not in seen:
                    results.append(user.model_dump())
                    seen.add(user.sec_uid)
        return results

    async def query_profiles(self, target_ids: list[str]) -> list[ProfileResult]:
        targets = self.resolve_targets(target_ids)
        if not targets:
            return []

        # chromium 进程级池被抖音侧掐断后会永久毒化（_CONTEXT 指死对象 / new_page 抛
        # TargetClosedError）。init_browser() 现在加了心跳重建，但**只在下一次调用时**生效。
        # 这里再做一次同请求内的兜底：第一次 fetch_profiles_parallel 抛 TargetClosedError
        # → close_browser() 强制清池 → 重试一次。第二轮 init_browser() 走"心跳查死 → 重建"
        # 路径拉新 chromium。让用户点一次"检测"就能成，多等 5-7s 即可。
        try:
            results = await self.module.fetch_profiles_parallel(targets)
        except Exception as exc:
            if not _is_pool_death(exc):
                raise
            await self._reset_monitor_pool()
            results = await self.module.fetch_profiles_parallel(targets)
        profiles = [self._to_profile_result(entry, info) for entry, info in zip(targets, results)]
        # 对直播中的 profile 并发补直播间详情（信号量内部限并发；未直播/失败置 None）
        await asyncio.gather(*(self._enrich_live_room(p) for p in profiles))
        upsert_profile_cache(PROFILE_CACHE_FILE, [profile.model_dump() for profile in profiles])
        return profiles

    async def _reset_monitor_pool(self) -> None:
        """主动把 monitor 模块的 chromium 池置 None；下一次 init_browser() 会重建。"""
        close_fn = getattr(self.module, "close_browser", None)
        if close_fn is None:
            return
        try:
            await close_fn()
        except Exception:
            # 死池上 close 也会抛,清掉全局兜底
            try:
                self.module._CONTEXT = None
                self.module._BROWSER = None
                self.module._PLAYWRIGHT = None
                if hasattr(self.module, "_page_pool"):
                    self.module._page_pool.clear()
            except Exception:
                pass

    async def shutdown(self) -> None:
        await self.module.close_browser()
        await self.live_room.close()

    async def _enrich_live_room(self, profile: ProfileResult) -> None:
        """对有 web_rid 的 profile 补直播间详情（含 paygrade）。

        触发条件：profile.web_rid 已知（monitor 抓到过直播后写库）。
        不再受 live_status==1 限制——web_rid 永久稳定，anchor 离线时也能开
        live.douyin.com/<web_rid> 看回放 + hover 拿 paygrade。
        信号量≤3 限并发浏览器数；失败一律降级（不改写已 None 的 live_room）。
        """
        if not profile.ok or not isinstance(profile.profile, dict):
            return
        info = profile.profile
        web_rid = str(info.get("web_rid") or "")
        room_id_str = str(info.get("room_id") or "")
        if not web_rid and not room_id_str:
            info["live_room"] = None
            return
        # 不抹掉已有 live_room.web_rid (有的话) —— 避免被 None 覆盖后丢失。
        async with self._live_room_sem:
            try:
                room = await self.live_room.fetch_overview(
                    web_rid=web_rid,
                    room_id_str=room_id_str,
                    sec_uid=profile.sec_uid,
                )
            except Exception:
                room = None
        if room is not None:
            info["live_room"] = room
        elif "live_room" not in info:
            info["live_room"] = None

    async def fetch_live_room(self, *, sec_uid: str = "", web_rid: str = "",
                              room_id_str: str = "") -> dict[str, Any] | None:
        """手动再探测一次直播间（供 /api/live-room 端点）。

        优先用入参 web_rid / room_id_str；否则按 sec_uid 从 profile_cache.json
        取 cached profile.web_rid（web_rid 已知即可触发，不依赖 live_status）。

        探测成功后会回写 profile_cache.json 里对应账户的 profile.live_room，
        并只刷新 live_room 段 + 派生字段（live_status=1 当 status==2 时）。
        基础信息（昵称/粉丝/直播状态等）一概不写，与 basic 检测解耦。

        探测失败（room=None）不写 cache，保留上一次结果。
        """
        if not web_rid and not room_id_str:
            if sec_uid:
                cached = read_profile_cache(PROFILE_CACHE_FILE).get(sec_uid, {}).get("profile")
                if isinstance(cached, dict):
                    web_rid = str(cached.get("web_rid") or "")
                    room_id_str = str(cached.get("room_id") or "")
                    # 兼容：web_rid 也可能只存于 live_room.web_rid（旧 cache 数据）。
                    if not web_rid:
                        web_rid = str((cached.get("live_room") or {}).get("web_rid") or "")
            if not web_rid and not room_id_str and not sec_uid:
                return None
        async with self._live_room_sem:
            try:
                room = await self.live_room.fetch_overview(
                    web_rid=web_rid, room_id_str=room_id_str, sec_uid=sec_uid
                )
            except Exception:
                room = None
        if sec_uid and room is not None:
            self._merge_live_room_into_cache(sec_uid, room)
        return room

    async def fetch_live_viewers(self, sec_uid: str) -> dict[str, Any] | None:
        """仅刷新直播间人数（profile.live_viewers / live_room.viewers）。

        走与 fetch_live_room 相同的 enter 接口，但只取 viewers/user_count_str/
        status 三个字段写回 cache；其它字段（title/anchor/audience_rank 等）保留
        原值，避免探测耗时与数据抖动。status==2 时同步把 live_status=1 写回。
        """
        if not sec_uid:
            return None
        cached = read_profile_cache(PROFILE_CACHE_FILE).get(sec_uid, {}).get("profile")
        web_rid = ""
        room_id_str = ""
        if isinstance(cached, dict):
            web_rid = str(cached.get("web_rid") or "")
            if not web_rid:
                web_rid = str((cached.get("live_room") or {}).get("web_rid") or "")
            room_id_str = str(cached.get("room_id") or "")
        if not web_rid and not room_id_str:
            return None
        async with self._live_room_sem:
            try:
                room = await self.live_room.fetch_overview(
                    web_rid=web_rid, room_id_str=room_id_str, sec_uid=sec_uid
                )
            except Exception:
                room = None
        if not room:
            return None
        if sec_uid:
            self._merge_live_viewers_into_cache(sec_uid, room)
        return {"viewers": room.get("viewers") or 0, "user_count_str": room.get("user_count_str") or "", "status": room.get("status")}

    async def fetch_anchor_paygrade(self, sec_uid: str) -> int | None:
        """手动探测 anchor 本人 paygrade（hover popup），供前端"刷等级"按钮。

        需要 cache 中已存 web_rid；失败 → None。
        """
        if not sec_uid:
            return None
        cached = read_profile_cache(PROFILE_CACHE_FILE).get(sec_uid, {}).get("profile")
        web_rid = str((cached or {}).get("web_rid") or "") if isinstance(cached, dict) else ""
        if not web_rid:
            return None
        async with self._live_room_sem:
            try:
                level = await self.live_room.fetch_anchor_paygrade(web_rid=web_rid)
            except Exception:
                level = None
        if isinstance(level, int) and sec_uid:
            self._merge_anchor_paygrade_into_cache(sec_uid, level)
        return level

    def _merge_live_room_into_cache(self, sec_uid: str, room: dict[str, Any]) -> None:
        """把手动探测到的直播间详情合并写回 cache。只更新 profile.live_room
        与派生字段，基础信息（昵称/粉丝数等）一概不动。"""
        cache = read_profile_cache(PROFILE_CACHE_FILE)
        entry = cache.get(sec_uid)
        if not isinstance(entry, dict):
            return
        profile = entry.get("profile")
        if not isinstance(profile, dict):
            return
        profile["live_room"] = room
        # 同时把 web_rid 提升到 profile 顶层（方便后续 paygrade 复用）
        if room.get("web_rid") and not profile.get("web_rid"):
            profile["web_rid"] = room["web_rid"]
        # status==2 时把 live_status 也对齐；status==4 (回放) 不强行覆盖，避免误把
        # 已下播账户重新标记成"直播中"。
        if room.get("status") == 2:
            profile["live_status"] = 1
        entry["profile"] = profile
        entry["last_checked_at"] = now_iso()
        cache[sec_uid] = entry
        PROFILE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PROFILE_CACHE_FILE.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _merge_anchor_paygrade_into_cache(self, sec_uid: str, level: int) -> None:
        """把 anchor 荣耀等级写回 cache 的 profile.live_room.anchor.paygrade_level。"""
        if not isinstance(level, int):
            return
        cache = read_profile_cache(PROFILE_CACHE_FILE)
        entry = cache.get(sec_uid)
        if not isinstance(entry, dict):
            return
        profile = entry.get("profile")
        if not isinstance(profile, dict):
            return
        room = profile.get("live_room")
        if not isinstance(room, dict):
            room = {}
        anchor = room.get("anchor")
        if not isinstance(anchor, dict):
            anchor = {}
        anchor["paygrade_level"] = level
        room["anchor"] = anchor
        profile["live_room"] = room
        entry["profile"] = profile
        entry["last_checked_at"] = now_iso()
        cache[sec_uid] = entry
        PROFILE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PROFILE_CACHE_FILE.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _merge_live_viewers_into_cache(self, sec_uid: str, room: dict[str, Any]) -> None:
        """只刷人数：profile.live_viewers + live_room.viewers/user_count_str/status。
        其它字段（title/anchor/audience_rank 等）原样保留。"""
        cache = read_profile_cache(PROFILE_CACHE_FILE)
        entry = cache.get(sec_uid)
        if not isinstance(entry, dict):
            return
        profile = entry.get("profile")
        if not isinstance(profile, dict):
            return
        # 顶层 live_viewers 与 live_room.viewers/user_count_str 同步刷新
        viewers = room.get("viewers")
        if isinstance(viewers, int) and viewers >= 0:
            profile["live_viewers"] = viewers
        status = room.get("status")
        # status==2 直播中；status==4 回放；status==0 等其它值视为已下播
        if status == 2:
            profile["live_status"] = 1
        elif status in (0, 4):
            profile["live_status"] = 0
            profile["live_viewers"] = None  # 已下播人数清零，避免和缓存值混淆
        # 更新 live_room 内对应字段，其它不动
        room_existing = profile.get("live_room") if isinstance(profile.get("live_room"), dict) else {}
        room_existing = dict(room_existing)  # 拷贝，不污染原引用
        if isinstance(viewers, int) and viewers >= 0:
            room_existing["viewers"] = viewers
        if room.get("user_count_str"):
            room_existing["user_count_str"] = room["user_count_str"]
        if status is not None:
            room_existing["status"] = status
        profile["live_room"] = room_existing
        entry["profile"] = profile
        entry["last_checked_at"] = now_iso()
        cache[sec_uid] = entry
        PROFILE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PROFILE_CACHE_FILE.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _to_profile_result(self, entry: dict[str, Any], info: dict[str, Any] | None) -> ProfileResult:
        if not info:
            return ProfileResult(
                label=entry.get("label", ""),
                url=entry.get("url", ""),
                sec_uid=entry["sec_uid"],
                ok=False,
                error="Unable to fetch profile.",
            )
        profile = profile_from_monitor_info(self.module, info)
        previous_profile = read_profile_cache(PROFILE_CACHE_FILE).get(entry["sec_uid"], {}).get("profile")
        if not isinstance(previous_profile, dict):
            previous_profile = {}
        profile = normalize_live_timing(profile, previous_profile, now_iso())
        return ProfileResult(
            label=entry.get("label", ""),
            url=entry.get("url", ""),
            sec_uid=entry["sec_uid"],
            ok=True,
            profile=profile,
        )



def _is_pool_death(exc: BaseException) -> bool:
    """判断异常是不是 chromium 池毒化(playwright WS / context 死)。

    触发链路:`context.new_page()` 抛 `playwright._impl._errors.TargetClosedError`;
    或 playwright 内部 connection 抛 `ConnectionError`/`Error` (playwright driver 失联)。
    """
    name = type(exc).__name__
    if name in {"TargetClosedError", "ConnectionError", "Error"}:
        return True
    # playwright 把 "Error" 暴露成顶层类 + 子类,但某些版本内部抛 ConnectionResetError 等
    msg = str(exc) or ""
    if any(s in msg for s in ("Target page, context or browser has been closed",
                              "Connection closed", "Browser has been closed",
                              "Connection reset", "Broken pipe")):
        return True
    return False


def normalize_download_dir(value: str | Path | None = None) -> str:
    raw = str(value or "").strip()
    path = Path(raw).expanduser() if raw else DOWNLOAD_OUTPUT_DIR
    if not path.is_absolute():
        path = (PROJECT_ROOT / path).resolve()
    return str(path)


def load_app_settings() -> AppSettings:
    settings = AppSettings(
        download_output_dir=normalize_download_dir(DOWNLOAD_OUTPUT_DIR),
        wrap_download_folder=False,
    )
    if not SETTINGS_FILE.exists():
        return settings
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return settings

    return AppSettings(
        download_output_dir=normalize_download_dir(raw.get("download_output_dir") or settings.download_output_dir),
        wrap_download_folder=bool(raw.get("wrap_download_folder", settings.wrap_download_folder)),
        image_format=str(raw.get("image_format") or "auto"),
        video_format=str(raw.get("video_format") or "auto"),
    )


def save_app_settings(download_output_dir: str, wrap_download_folder: bool, image_format: str = "auto", video_format: str = "auto") -> AppSettings:
    settings = AppSettings(
        download_output_dir=normalize_download_dir(download_output_dir),
        wrap_download_folder=bool(wrap_download_folder),
        image_format=image_format if image_format in ("auto", "jpg", "jpeg", "png", "webp", "avif") else "auto",
        video_format=video_format if video_format in ("auto", "mp4", "webm", "audio") else "auto",
    )
    path = Path(settings.download_output_dir)
    if path.exists() and not path.is_dir():
        raise ValueError("Download output path exists but is not a directory.")
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"Cannot create download output directory: {exc}") from exc

    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(settings.model_dump_json(indent=2), encoding="utf-8")
    return settings


class DownloadService:
    def __init__(self) -> None:
        self.module = load_module("douyin_downloader_tool", DOWNLOADER_DIR / "downloader.py")
        self.jobs: dict[str, DownloadJob] = {}
        # 全局最多 4 个工作线程：1 个给 job 调度(_dispatch_job)，其余跑 URL 子进程。
        # 即同一 job 内多 URL 并发上限 3，多 job 之间共享这个池。
        self._executor = ThreadPoolExecutor(max_workers=4)
        # 正在排队的 URL 计数(交由 _executor 调度；用于判断当前 job 是否还有 URL
        # 在跑)。cancel/delete 时据此等待或放弃。job_id -> 剩余未完成 URL 数。
        self._pending: dict[str, int] = {}
        self._state_lock = threading.Lock()

    def create_job(self, text: str, mode: int, output_dir: str, comments: bool, selected_urls: list[str] | None = None, selected_media: dict[str, list[int]] | None = None, wrap_folder: bool = False, image_format: str = "auto", video_format: str = "auto") -> DownloadJob:
        urls = extract_douyin_urls(text)
        if selected_urls:
            selected = set(selected_urls)
            urls = [url for url in urls if url in selected]
        if not urls:
            raise ValueError("No Douyin URL found in input.")

        resolved_output = normalize_download_dir(output_dir or load_app_settings().download_output_dir)
        normalized_media: dict[str, list[int]] = {}
        if selected_media:
            for url, indices in selected_media.items():
                clean = [int(i) for i in indices if isinstance(i, int) or (isinstance(i, str) and i.isdigit())]
                if clean:
                    normalized_media[url] = sorted(set(clean))
        job = DownloadJob(
            id=str(uuid.uuid4()),
            status="queued",
            created_at=now_iso(),
            updated_at=now_iso(),
            input=text,
            urls=urls,
            mode=mode,
            output_dir=resolved_output,
            wrap_folder=wrap_folder,
            comments=comments,
            image_format=image_format if image_format in ("auto", "jpg", "jpeg", "png", "webp", "avif") else "auto",
            video_format=video_format if video_format in ("auto", "mp4", "webm", "audio") else "auto",
            selected_media=normalized_media,
        )
        self.jobs[job.id] = job
        self._add_job_log(job.id, "info", f"Queued {len(urls)} URL(s).")
        self._ensure_executor().submit(self._dispatch_job, job.id)
        return job

    def preview(self, text: str, deep: bool = False) -> dict[str, Any]:
        urls = extract_douyin_urls(text)
        if not urls:
            raise ValueError("No Douyin URL found in input.")
        items = [self._preview_item(url, index, deep) for index, url in enumerate(urls, 1)]
        return {"items": items}

    def create_job_from_post_list(self, urls: list[str], label: str, mode: int, output_dir: str, comments: bool, wrap_folder: bool = False, image_format: str = "auto", video_format: str = "auto") -> DownloadJob:
        """作品列表一键批量下载: 跳过 extract_douyin_urls 文本解析,
        直接吃 aweme_read 给出的 /video/{id} URL 列表."""
        urls = [u.strip() for u in urls if u and u.strip()]
        if not urls:
            raise ValueError("No post URLs provided.")

        resolved_output = normalize_download_dir(output_dir or load_app_settings().download_output_dir)
        job = DownloadJob(
            id=str(uuid.uuid4()),
            status="queued",
            created_at=now_iso(),
            updated_at=now_iso(),
            input=f"[作品列表批量] {label} ({len(urls)} 个作品)",
            urls=urls,
            mode=mode,
            output_dir=resolved_output,
            wrap_folder=wrap_folder,
            comments=comments,
            image_format=image_format if image_format in ("auto", "jpg", "jpeg", "png", "webp", "avif") else "auto",
            video_format=video_format if video_format in ("auto", "mp4", "webm", "audio") else "auto",
        )
        self.jobs[job.id] = job
        self._add_job_log(job.id, "info", f"Queued {len(urls)} post URL(s) from {label}.")
        self._ensure_executor().submit(self._dispatch_job, job.id)
        return job

    def list_jobs(self) -> list[DownloadJob]:
        return sorted(self.jobs.values(), key=lambda job: job.created_at, reverse=True)

    def get_job(self, job_id: str) -> DownloadJob | None:
        return self.jobs.get(job_id)

    def _dispatch_job(self, job_id: str) -> None:
        """调度一个 job：多 URL 并发提交到共享线程池并等所有完成。

        不再串行逐个跑——以前一个卡 240s 后面全排队。现在同一 job 内 up to 3 个
        URL 子进程并行，任一卡住不阻塞其余。仍在跑的子进程撞 240s 超时由子进程
        兜底回收。cancel_requested 被检查：发起后不再提交新 URL，已开的会跑完。
        """
        from concurrent.futures import as_completed

        job = self.jobs[job_id]
        # 启动前再检查一次取消(可能在排队期间就被取消了)。
        with self._state_lock:
            if job.cancel_requested:
                job.status = "cancelled"
                job.updated_at = now_iso()
                self._add_job_log(job_id, "info", "Job cancelled before start.")
                self._pending.pop(job_id, None)
                return
        job.status = "running"
        job.updated_at = now_iso()
        self._add_job_log(job_id, "info", "Started download job.")

        results: list[dict[str, Any]] = []
        results_lock = threading.Lock()
        failed = 0
        index_of = {url: i for i, url in enumerate(job.urls, 1)}
        total = len(job.urls)
        with self._state_lock:
            self._pending[job_id] = total

        executor = self._ensure_executor()
        futures = {}
        for url in job.urls:
            with self._state_lock:
                if job.cancel_requested:
                    break
            futures[executor.submit(self._download_one_subprocess, job_id, url, index_of[url], total)] = url

        try:
            for future in as_completed(futures):
                url = futures[future]
                try:
                    outcome = future.result()
                except Exception as exc:  # 兜底：子工作线程自身异常
                    outcome = {"url": url, "ok": False, "error": f"调度失败: {exc}"}
                with results_lock:
                    results.append(outcome)
                    if not outcome.get("ok", False):
                        failed += 1
                job.updated_at = now_iso()
                with self._state_lock:
                    self._pending[job_id] = max(0, int(self._pending.get(job_id, 0)) - 1)
        finally:
            with self._state_lock:
                self._pending.pop(job_id, None)

        with self._state_lock:
            cancelled = job.cancel_requested
        job.results = results
        if cancelled:
            job.status = "cancelled"
            job.error = f"已取消：{failed} 个失败/未完成，已完成 {len(results) - failed}。"
            self._add_job_log(job_id, "warning", "Job cancelled.")
        elif failed:
            job.status = "error"
            job.error = f"{failed} of {total} URL(s) failed."
        else:
            job.status = "done"
            self._add_job_log(job_id, "info", "Finished all downloads.")
        job.updated_at = now_iso()

    def _download_one_subprocess(self, job_id: str, url: str, index: int, total: int) -> dict[str, Any]:
        """单 URL 下载：子进程隔离 + 240s 硬超时。在共享线程池的工作线程里跑。"""
        job = self.jobs.get(job_id)
        if job is None:
            return {"url": url, "ok": False, "error": "Job not found."}
        # 开始前检查取消：未开跑的直接标记跳过，不浪费子进程。
        with self._state_lock:
            if job.cancel_requested:
                self._add_job_log(job_id, "warning", f"[{index}/{total}] Skipped (cancelled): {url}")
                return {"url": url, "ok": False, "error": "cancelled", "cancelled": True}
        wanted = job.selected_media.get(url) if job.selected_media else None
        self._add_job_log(job_id, "info", f"[{index}/{total}] Processing {url}")
        job.updated_at = now_iso()
        # download_douyin 跑在独立子进程；CDP 死亡致 sync_playwright 无限挂时，
        # subprocess.run(timeout=DOWNLOAD_TIMEOUT) 会 kill 子进程兜底回收。
        try:
            result = run_download_subprocess(
                url,
                job.output_dir,
                job.mode,
                job.comments,
                wanted,
                job.wrap_folder,
                image_format=job.image_format,
                video_format=job.video_format,
                timeout=DOWNLOAD_TIMEOUT,
            )
        except TimeoutError as exc:
            self._add_job_log(job_id, "error", f"[{index}/{total}] 下载超时({DOWNLOAD_TIMEOUT}s)已终止")
            return {"url": url, "ok": False, "error": f"下载超时({DOWNLOAD_TIMEOUT}s)已终止: {exc}"}
        except Exception as exc:
            self._add_job_log(job_id, "error", f"[{index}/{total}] Failed: {exc}")
            return {"url": url, "ok": False, "error": str(exc)}
        result = result if isinstance(result, dict) else {"result": result}
        result["url"] = url
        result["ok"] = True
        self._add_job_log(job_id, "info", f"[{index}/{total}] Finished {result.get('title') or url}")
        return result

    def _ensure_executor(self) -> ThreadPoolExecutor:
        if not hasattr(self, "_executor"):
            self._executor = ThreadPoolExecutor(max_workers=4)
        return self._executor

    def cancel_job(self, job_id: str) -> bool:
        """请求取消一个下载 job。

        设置 cancel_requested 标志位；_dispatch_job 在每个 URL 开始前会检查，
        已在子进程里跑的 URL 仍要等它走完(或撞 240s 超时)——这是 sync_playwright
        挂死时同进程无法 interrupt 的固有限制，UI 上立即把状态置 cancelled 即可。
        """
        job = self.jobs.get(job_id)
        if not job:
            return False
        if job.status in {"done", "error", "cancelled"}:
            return False
        with self._state_lock:
            job.cancel_requested = True
        # 已经排队但还没开跑的 URL 不会被 submit(因为调度器会先看到标志)；
        # 把状态先置 cancelled 让前端立刻反馈，跑完的收尾在 _dispatch_job 里覆盖。
        if job.status != "running":
            job.status = "cancelled"
            self._add_job_log(job_id, "info", "Cancelled before start.")
        else:
            self._add_job_log(job_id, "warning", "取消请求已记录，正在运行的链接完成后停止后续。")
        job.updated_at = now_iso()
        return True

    def delete_job(self, job_id: str) -> bool:
        """删除一个下载 job 的记录。运行中的先置取消标志再清理。"""
        job = self.jobs.get(job_id)
        if not job:
            return False
        # 运行中也允许删除：置 cancel 标志(尽力让后续 URL 跳过)。
        with self._state_lock:
            job.cancel_requested = True
        self._add_job_log(job_id, "info", "Job deleted.")
        self.jobs.pop(job_id, None)
        self._pending.pop(job_id, None)
        return True

    def _add_job_log(self, job_id: str, level: str, message: str) -> None:
        job = self.jobs.get(job_id)
        if not job:
            return
        job.logs.append({"time": now_iso(), "level": level, "message": message})
        job.updated_at = now_iso()

    def _preview_item(self, url: str, index: int, deep: bool) -> dict[str, Any]:
        item: dict[str, Any] = {
            "index": index,
            "url": url,
            "selected": True,
            "status": "parsed",
            "type": infer_douyin_url_type(url),
            "title": "",
            "author": "",
            "duration": 0,
            "cover_url": "",
            "media": [],
            "error": "",
        }
        if not deep:
            return item
        try:
            details = preview_douyin_url(self.module, url)
            item.update(details)
            item["status"] = "ready"
        except Exception as exc:
            item["status"] = "error"
            item["error"] = str(exc)
        return item


def extract_douyin_urls(text: str) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    # 允许用户漏填协议：把裸 www.douyin.com / v.douyin.com / iesdouyin.com
    # 开头的链接自动补上 https://，再走统一正则。
    if text:
        text = re.sub(
            r"(?<![\w./:-])(www\.douyin\.com|v\.douyin\.com|(?:www\.)?iesdouyin\.com)",
            r"https://\1",
            text,
        )
    pattern = re.compile(
        r"https?://(?:v\.douyin\.com/|www\.douyin\.com/|(?:www\.)?iesdouyin\.com/).+?"
        r"(?=https?://(?:v\.douyin\.com/|www\.douyin\.com/|(?:www\.)?iesdouyin\.com/)|[\s\"'<>，。；、)）]|$)"
    )
    for match in pattern.finditer(text):
        cleaned = match.group(0).rstrip(".,;!?")
        if ("douyin.com" in cleaned or "iesdouyin.com" in cleaned) and cleaned not in seen:
            seen.add(cleaned)
            urls.append(cleaned)
    return expand_aggregation_urls(urls)


# 聚合页(用户主页/喜欢/发现/搜索)用 modal_id 指向某条作品。直接 goto 聚合页 SSR
# 里没有 videoDetail，downloader 会卡在等不到直链直至 240s 超时——这是下载频繁报
# "超时已终止"的直接诱因。这里在入队阶段就规约成 /video/<id> 直链，省得每条白等。
_MODAL_ID_RE = re.compile(r"modal_id=(\d+)")


def expand_aggregation_urls(urls: list[str]) -> list[str]:
    """把带 modal_id 的聚合页 URL 规约成 `/video/<id>` 直链，其余原样保留。

    仅对尚未指向 /video/ /note/ 的聚合页(/user/、/jingxuan 等)做转换，且 modal_id
    必须是纯数字作品 id。v.douyin.com 短链交给 downloader 自然落地，不动。
    """
    expanded: list[str] = []
    seen: set[str] = set()
    for url in urls:
        converted = False
        if "/video/" not in url and "/note/" not in url and "v.douyin.com" not in url:
            m = _MODAL_ID_RE.search(url)
            if m and ("/user/" in url or "/jingxuan" in url):
                direct = f"https://www.douyin.com/video/{m.group(1)}"
                if direct not in seen:
                    seen.add(direct)
                    expanded.append(direct)
                converted = True
        if not converted and url not in seen:
            seen.add(url)
            expanded.append(url)
    return expanded


def infer_douyin_url_type(url: str) -> str:
    if "/note/" in url:
        return "note"
    if "/video/" in url:
        return "video"
    if "/jingxuan" in url:
        return "video"
    if extract_aweme_id_from_url(url):
        return "video"
    return "link"


def extract_aweme_id_from_url(url: str) -> str:
    ids = extract_aweme_ids_from_url(url)
    return ids[0] if ids else ""


def extract_aweme_ids_from_url(url: str) -> list[str]:
    """从抖音 URL 中提取 aweme_id（视频ID）。

    覆盖三类聚合页 URL（它们共享同一个 modal_id 参数）：
    - 发现页:  /jingxuan?modal_id=<id>
    - 搜索页:  /jingxuan/search/xxx?...&modal_id=<id>
    - 喜欢列表: /user/self?...&modal_id=<id>
    也兼容标准详情页 /video/<id>、/note/<id> 与 ?aweme_id=<id>。
    """
    if not url:
        return []
    ids: list[str] = []

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if text.isdigit() and text not in ids:
            ids.append(text)

    path_match = re.search(r"/(?:video|note)/(\d+)", url)
    if path_match:
        add(path_match.group(1))
        return ids
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ("modal_id", "aweme_id", "awemeId", "video_id", "vid"):
        values = query.get(key) or []
        for value in values:
            add(value)
    # v.douyin.com 短链等：路径段里第一个纯数字串
    for segment in parsed.path.split("/"):
        add(segment)
    return ids


def preview_douyin_url(module: Any, url: str) -> dict[str, Any]:
    # 与 downloader.download_douyin 同源的三级登录态回退(CDP→profile→裸启动)，
    # 复用常驻 Chrome 默认 context 实现"预览静默、不弹可见窗口"。
    import os as _os
    _cdp = _os.environ.get("DOUYIN_CDP", "http://127.0.0.1:9222")
    _profile = str(SEARCH_PROFILE_DIR) if SEARCH_PROFILE_DIR.exists() else ""
    browser = None
    context = None
    page = None
    owns_context = False
    playwright = module.sync_playwright().start()
    try:
        if _cdp:
            try:
                browser = playwright.chromium.connect_over_cdp(_cdp)
                ctxs = browser.contexts
                if ctxs:
                    context = ctxs[0]
                    owns_context = False
                else:
                    context = browser.new_context(viewport={"width": 1600, "height": 900})
                    owns_context = True
                page = context.new_page()
            except Exception:
                browser = None
        if page is None and _profile:
            try:
                context = playwright.chromium.launch_persistent_context(
                    _profile,
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                page = context.pages[0] if context.pages else context.new_page()
                owns_context = True
            except Exception:
                context = None
                page = None
        if page is None:
            browser = playwright.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                viewport={"width": 1600, "height": 900},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
            )
            page = context.new_page()
            owns_context = True

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            # 发现页/搜索页/喜欢列表等聚合页 SSR 里没有 videoDetail，
            # 需要先规约到标准详情页才能拿到图集/视频数据。
            # v.douyin.com 短链会 302 跳转到 /note/<id> 或 /video/<id>；用落地 URL 判断更准。
            landed = page.url or url
            resolved = landed
            try:
                page.wait_for_selector("video, h1, [data-e2e='video-title'], [class*='note'], [class*='note-detail']", timeout=12000)
            except Exception:
                pass
            page.wait_for_timeout(1500)

            content_type = module._extract_content_type(page)
            needs_resolve = "/video/" not in resolved and "/note/" not in resolved
            # 聚合页 content_type 多为 unknown；详情页通常为 video/image/slide
            if needs_resolve or content_type in ("unknown", ""):
                aweme_ids = extract_aweme_ids_from_url(url) or extract_aweme_ids_from_url(resolved)
                if aweme_ids:
                    candidates = [
                        candidate
                        for aweme_id in aweme_ids
                        for candidate in (
                            f"https://www.douyin.com/video/{aweme_id}",
                            f"https://www.douyin.com/note/{aweme_id}",
                        )
                    ]
                    for candidate in candidates:
                        try:
                            page.goto(candidate, wait_until="domcontentloaded", timeout=20000)
                        except Exception:
                            continue
                        try:
                            page.wait_for_selector("video, h1, [data-e2e='video-title'], [class*='note'], [class*='note-detail']", timeout=12000)
                        except Exception:
                            pass
                        page.wait_for_timeout(1500)
                        resolved = candidate
                        content_type = module._extract_content_type(page)
                        if content_type not in ("unknown", ""):
                            break

            meta = module._extract_stats(page) or {}
            # /note/ 纯图集在无头环境常拿不到 SSR，content_type 可能误判为 unknown；
            # 这种情况下用 slides info（含 API 回退）兜底拿全部图片。
            if content_type == "unknown" and "/note/" in resolved:
                content_type = "image"
            cover_url = _extract_cover_url(page)
            details: dict[str, Any] = {
                "type": content_type,
                "title": meta.get("title", ""),
                "author": meta.get("author", ""),
                "cover_url": cover_url,
                "duration": 0,
                "media": [],
            }
            # SSR stats 四计数透出 (2026-11-13): _extract_stats 已挖到 digg/comment/
            # collect/share, 之前丢弃 — 预览面板直接展示, 免再发请求
            if meta.get("stats"):
                details["stats"] = meta["stats"]
            if content_type == "video":
                info = module._extract_video_info(page)
                details["title"] = info.get("title") or details["title"]
                details["author"] = info.get("author") or details["author"]
                aweme_ids = extract_aweme_ids_from_url(url) or extract_aweme_ids_from_url(resolved)
                aweme_ids = aweme_ids or [meta.get("aweme_id", "")]
                details["duration"] = _extract_preview_video_duration(page, aweme_ids)
                details["media"] = [
                    {
                        "index": 1,
                        "type": "video",
                        "duration": details["duration"],
                        "cover_url": cover_url,
                    }
                ]
            elif content_type in ("slide", "image"):
                # 优先 slides info（SSR→API→DOM 三级回退，能拿到完整图集元数据）
                slide_info = module._extract_slides_info(page) or {}
                slides = slide_info.get("slides") or []
                if slides:
                    details["title"] = slide_info.get("title") or details["title"]
                    details["author"] = slide_info.get("author") or details["author"]
                    details["media"] = [
                        {
                            "index": int(slide.get("index", index - 1)) + 1,
                            "type": slide.get("media_type") or "image",
                            "duration": _normalize_duration(slide.get("duration") or 0),
                            "cover_url": slide.get("best_image_url") or (slide.get("image_urls") or [""])[0],
                        }
                        for index, slide in enumerate(slides, 1)
                    ]
                else:
                    # 最后一道兜底：DOM 逐张滚动收集图集图片
                    images = _collect_image_set(page, module)
                    details["media"] = [
                        {"index": index, "type": "image", "duration": 0, "cover_url": image_url}
                        for index, image_url in enumerate(images, 1)
                    ]
                if not details["media"]:
                    # slides 信息与 DOM 均无果，至少返回封面
                    details["media"] = [{"index": 1, "type": "image", "duration": 0, "cover_url": cover_url}] if cover_url else []
                if details["media"] and not details["cover_url"]:
                    details["cover_url"] = details["media"][0].get("cover_url") or ""
            return details
        finally:
            # 与 downloader 一致：CDP 复用常驻 context 时只关 page，
            # 自有浏览器(profile/裸启动)才关 context+browser。
            if not owns_context:
                try:
                    if page is not None:
                        page.close()
                except Exception:
                    pass
                try:
                    if browser is not None:
                        browser.close()
                except Exception:
                    pass
            else:
                try:
                    if context is not None:
                        context.close()
                except Exception:
                    pass
                try:
                    if browser is not None:
                        browser.close()
                except Exception:
                    pass
    finally:
        try:
            playwright.stop()
        except Exception:
            pass


def _collect_image_set(page, module: Any) -> list[str]:
    """优先复用 downloader 的逐张滚动收集；失败则退回 DOM img 选择器。"""
    try:
        images = module._scroll_and_collect_images(page) or []
        if images:
            return images
    except Exception:
        pass
    return _extract_image_previews(page)



def _normalize_duration(value: Any) -> int:
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(duration) or duration < 0:
        return 0
    if duration > 1000:
        duration = duration / 1000
    return int(duration)


def _normalize_duration_seconds(value: Any) -> int:
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(duration) or duration < 0:
        return 0
    return int(duration)


def _extract_duration_from_payload(payload: Any, aweme_id: str | list[str] = "") -> int:
    targets = [str(value).strip() for value in (aweme_id if isinstance(aweme_id, list) else [aweme_id]) if str(value or "").strip()]
    best_any = 0

    def duration_from_record(record: dict[str, Any]) -> int:
        video = record.get("video") if isinstance(record.get("video"), dict) else {}
        for value in (
            video.get("duration"),
            record.get("duration"),
            record.get("durationSec"),
            record.get("videoDuration"),
        ):
            duration = _normalize_duration(value)
            if duration:
                return duration
        return 0

    def record_matches(record: dict[str, Any]) -> bool:
        if not targets:
            return False
        for key in ("aweme_id", "awemeId", "awemeIdStr", "id", "itemId", "group_id", "groupId"):
            value = record.get(key)
            if value is not None and str(value) in targets:
                return True
        return False

    def walk(node: Any) -> int:
        nonlocal best_any
        if isinstance(node, dict):
            duration = duration_from_record(node)
            if duration and not best_any:
                best_any = duration
            if duration and record_matches(node):
                return duration
            for value in node.values():
                found = walk(value)
                if found:
                    return found
        elif isinstance(node, list):
            for value in node:
                found = walk(value)
                if found:
                    return found
        return 0

    matched = walk(payload)
    return matched or (0 if targets else best_any)


def _extract_preview_video_duration(page, aweme_id: str | list[str] = "") -> int:
    return _extract_video_duration(page, aweme_id)


def _extract_video_duration(page, aweme_id: str | list[str] = "") -> int:
    snapshot = page.evaluate(
        """() => {
            const vd = window.SSR_RENDER_DATA?.app?.videoDetail;
            const raw = vd?.video?.duration || vd?.duration || 0;
            const video = document.querySelector('video');
            return {
                direct: raw,
                videoDuration: video && Number.isFinite(video.duration) ? Math.round(video.duration) : 0,
                ssr: window.SSR_RENDER_DATA || null,
            };
        }"""
    )
    duration = _extract_duration_from_payload(snapshot.get("ssr"), aweme_id)
    if duration:
        return duration
    duration = _normalize_duration(snapshot.get("direct"))
    if duration:
        return duration
    initial_video_duration = _normalize_duration_seconds(snapshot.get("videoDuration"))
    if initial_video_duration > 1:
        return initial_video_duration
    try:
        page.wait_for_function(
            "() => { const video = document.querySelector('video'); return video && Number.isFinite(video.duration) && video.duration > 2; }",
            timeout=5000,
        )
        return _normalize_duration_seconds(page.evaluate("() => Math.round(document.querySelector('video')?.duration || 0)"))
    except Exception:
        return initial_video_duration


def _extract_cover_url(page) -> str:
    # 2026-10 实测: 抖音详情页已不再注入 SSR_RENDER_DATA.videoDetail（RENDER_DATA
    # 壳里没有作品数据），旧 SSR 路径恒空；而裸 querySelector('img[src*="aweme"]')
    # 会误抓侧栏作者头像(aweme-avatar)，导致预览封面显示登录用户头像。
    # 现按可靠性排序: 播放器容器内封面图 → og:image → SSR(仅当真有) 。
    return page.evaluate(
        """() => {
            // 1. 播放器容器内的封面 img（xgplayer poster，image-cut-tos 特征，
            //    实测 440px 宽，是最可靠的"当前作品封面"来源）
            const player = document.querySelector("video");
            const scope = player?.closest("[class*='player'], [class*='video-detail']") || player?.parentElement;
            if (scope) {
                const imgs = Array.from(scope.querySelectorAll('img'));
                const pick = (pred) => imgs.find(pred);
                const isCover = (s) => s.includes('image-cut-tos')
                    || (s.includes('douyinpic') && !s.includes('avatar'));
                const big = pick((i) => i.naturalWidth >= 200 && isCover(i.src));
                if (big) return big.src;
            }
            // 2. og:image（未登录/蜘蛛场景可能有）
            const og = document.querySelector('meta[property="og:image"]')?.content;
            if (og) return og;
            // 3. SSR 数据（保留旧路径兜底，抖音若恢复注入则直接命中）
            const vd = window.SSR_RENDER_DATA?.app?.videoDetail;
            const video = vd?.video || {};
            const cover = video.cover || video.originCover || video.dynamicCover || vd?.cover;
            const urls = cover?.urlList || cover?.url_list || cover?.url_list_0 || [];
            if (Array.isArray(urls) && urls.length) return urls[0];
            // 4. 最后才用全页图选择器，但明确排除头像和静态资源，
            //    避免误抓作者头像/界面图标
            const img = document.querySelector(
                'img[src*="douyinpic"]:not([src*="avatar"]):not([src*=".svg"]):not([src*=".png"])'
            );
            return img?.src || '';
        }"""
    )


def _extract_image_previews(page) -> list[str]:
    return page.evaluate(
        """() => {
            const vd = window.SSR_RENDER_DATA?.app?.videoDetail;
            const fromSsr = (vd?.images || []).map((image) => {
                const urls = image.urlList || image.url_list || [];
                return urls.find((url) => String(url).includes('.jpeg')) || urls[0] || '';
            }).filter(Boolean);
            if (fromSsr.length) return fromSsr;
            return Array.from(document.querySelectorAll('img[src*="aweme-image"], img[src*="aweme_images"]'))
                .map((img) => img.src)
                .filter(Boolean)
                .slice(0, 12);
        }"""
    )


def ensure_project_users(users_file: Path, imported_settings: list[dict[str, Any]]) -> None:
    if users_file.exists():
        return
    users_file.parent.mkdir(parents=True, exist_ok=True)
    users = [
        UserEntry(
            label=str(entry.get("label") or ""),
            url=str(entry.get("url") or ""),
            sec_uid=str(entry.get("sec_uid") or ""),
        )
        for entry in imported_settings
        if entry.get("sec_uid") and entry.get("url")
    ]
    _write_project_users(users_file, users)


def read_project_users(users_file: Path = USERS_FILE) -> list[UserEntry]:
    if not users_file.exists():
        return []
    try:
        raw = json.loads(users_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []

    users: list[UserEntry] = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("sec_uid") or not item.get("url"):
            continue
        users.append(UserEntry(**item))
    return users


def reorder_project_users(users_file: Path, sec_uids: list[str]) -> list[UserEntry]:
    users = read_project_users(users_file)
    if not users:
        return []
    order = [uid for uid in sec_uids if uid]
    index_by_uid = {uid: index for index, uid in enumerate(order)}
    reordered = sorted(
        enumerate(users),
        key=lambda item: (index_by_uid.get(item[1].sec_uid, len(order) + item[0])),
    )
    next_users = [user for _index, user in reordered]
    _write_project_users(users_file, next_users)
    return next_users


def delete_project_user(users_file: Path, sec_uid: str) -> list[UserEntry]:
    target = sec_uid.strip()
    if not target:
        raise ValueError("sec_uid is required.")
    users = read_project_users(users_file)
    next_users = [user for user in users if user.sec_uid != target]
    if len(next_users) == len(users):
        raise ValueError("User not found.")
    _write_project_users(users_file, next_users)
    return next_users


def set_monitor_flag(users_file: Path, sec_uid: str, monitor: bool) -> list[UserEntry]:
    """主播库监控开关 (2026-11-07): 只翻 monitor 位, 其它字段不动。"""
    target = sec_uid.strip()
    users = read_project_users(users_file)
    hit = False
    for index, user in enumerate(users):
        if user.sec_uid == target:
            merged = {**user.model_dump(), "monitor": bool(monitor)}
            users[index] = UserEntry(**merged)
            hit = True
            break
    if not hit:
        raise ValueError("User not found.")
    _write_project_users(users_file, users)
    return users


def read_profile_cache(cache_file: Path = PROFILE_CACHE_FILE) -> dict[str, dict[str, Any]]:
    if not cache_file.exists():
        return {}
    try:
        raw = json.loads(cache_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return raw if isinstance(raw, dict) else {}


def upsert_profile_cache(cache_file: Path, results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    cache = read_profile_cache(cache_file)
    checked_at = now_iso()
    for result in results:
        sec_uid = str(result.get("sec_uid") or "")
        if not sec_uid:
            continue
        previous_profile = cache.get(sec_uid, {}).get("profile")
        if not isinstance(previous_profile, dict):
            previous_profile = {}
        profile = result.get("profile") if isinstance(result.get("profile"), dict) else None
        if profile:
            profile = normalize_live_timing(profile, previous_profile, checked_at)
        cache[sec_uid] = {
            "ok": bool(result.get("ok")),
            "error": str(result.get("error") or ""),
            "profile": profile,
            "last_checked_at": checked_at,
        }
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    return cache


def normalize_live_timing(profile: dict[str, Any], previous_profile: dict[str, Any], checked_at: str) -> dict[str, Any]:
    normalized = {**profile}
    if normalized.get("live_status") == 1:
        live_start_at = normalized.get("live_start_at") or previous_profile.get("live_start_at") or checked_at
        normalized["live_start_at"] = live_start_at
        normalized["live_duration_seconds"] = _duration_seconds(live_start_at)
    else:
        normalized["live_start_at"] = ""
        normalized["live_duration_seconds"] = None
    return normalized


def upsert_project_user(users_file: Path, data: dict[str, Any]) -> AddUserResult:
    label = str(data.get("label") or data.get("nickname") or "").strip()
    sec_uid = str(data.get("sec_uid") or "").strip()
    if not label:
        raise ValueError("User label is required.")
    if not sec_uid:
        raise ValueError("sec_uid is required.")

    url = str(data.get("url") or data.get("homepage_url") or "").strip()
    if not url or "/user/" not in url:
        url = f"https://www.douyin.com/user/{sec_uid}"

    incoming = UserEntry(
        label=label,
        url=url,
        sec_uid=sec_uid,
        monitor=bool(data.get("monitor", False)),  # 入库≠监控, 默认不进监控 (2026-11-07)
        nickname=str(data.get("nickname") or ""),
        unique_id=str(data.get("unique_id") or ""),
        signature=str(data.get("signature") or ""),
        avatar_url=str(data.get("avatar_url") or ""),
        follower_count=int(data.get("follower_count") or 0),
        following_count=int(data.get("following_count") or 0),
        total_favorited=int(data.get("total_favorited") or 0),
        ip_location=str(data.get("ip_location") or ""),
    )

    users = read_project_users(users_file)
    for index, user in enumerate(users):
        if user.sec_uid == sec_uid:
            merged = {**user.model_dump(), **incoming.model_dump(exclude_defaults=True)}
            # monitor 是开关不是资料字段: 显式传入才覆盖 (重复添加不悄悄改勾选状态)
            if "monitor" in data:
                merged["monitor"] = bool(data["monitor"])
            users[index] = UserEntry(**merged)
            _write_project_users(users_file, users)
            return AddUserResult(added=False, entry=users[index], message="User already exists; metadata updated.")

    users.append(incoming)
    _write_project_users(users_file, users)
    return AddUserResult(added=True, entry=incoming, message="User added.")


def _write_project_users(users_file: Path, users: list[UserEntry]) -> None:
    users_file.parent.mkdir(parents=True, exist_ok=True)
    users_file.write_text(
        json.dumps([user.model_dump() for user in users], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def parse_search_candidates(payload: dict[str, Any]) -> list[SearchCandidate]:
    user_list = payload.get("user_list")
    if user_list is None and isinstance(payload.get("data"), dict):
        user_list = payload["data"].get("user_list")
    if not isinstance(user_list, list):
        return []

    candidates: list[SearchCandidate] = []
    seen: set[str] = set()
    for item in user_list:
        if not isinstance(item, dict):
            continue
        user = item.get("user_info") if isinstance(item.get("user_info"), dict) else item
        sec_uid = str(user.get("sec_uid") or "").strip()
        if not sec_uid or sec_uid in seen:
            continue
        seen.add(sec_uid)
        avatar = user.get("avatar_thumb") if isinstance(user.get("avatar_thumb"), dict) else {}
        avatar_list = avatar.get("url_list") if isinstance(avatar.get("url_list"), list) else []
        room_id = str(user.get("room_id_str") or user.get("room_id") or "")
        candidates.append(
            SearchCandidate(
                nickname=str(user.get("nickname") or ""),
                unique_id=str(user.get("unique_id") or user.get("short_id") or ""),
                signature=str(user.get("signature") or ""),
                avatar_url=str(avatar_list[0]) if avatar_list else "",
                follower_count=int(user.get("follower_count") or 0),
                following_count=int(user.get("following_count") or 0),
                total_favorited=int(user.get("total_favorited") or 0),
                ip_location=str(user.get("ip_location") or ""),
                sec_uid=sec_uid,
                homepage_url=f"https://www.douyin.com/user/{sec_uid}",
                live_status=_extract_live_status(user),
                room_id=room_id,
            )
        )
    return candidates


def parse_search_summaries(summaries: list[dict[str, Any]]) -> list[SearchCandidate]:
    candidates: list[SearchCandidate] = []
    seen: set[str] = set()
    for user in summaries:
        if not isinstance(user, dict):
            continue
        sec_uid = str(user.get("sec_uid") or "").strip()
        if not sec_uid or sec_uid in seen:
            continue
        seen.add(sec_uid)
        candidates.append(
            SearchCandidate(
                nickname=str(user.get("nickname") or ""),
                unique_id=str(user.get("unique_id") or user.get("short_id") or ""),
                signature=str(user.get("signature") or ""),
                avatar_url=str(user.get("avatar") or user.get("avatar_url") or ""),
                follower_count=int(user.get("follower_count") or 0),
                following_count=int(user.get("following_count") or 0),
                total_favorited=int(user.get("total_favorited") or 0),
                ip_location=str(user.get("ip_location") or ""),
                sec_uid=sec_uid,
                homepage_url=str(user.get("homepage") or user.get("homepage_url") or f"https://www.douyin.com/user/{sec_uid}"),
                live_status=1 if user.get("streaming") else 0,
                room_id=str(user.get("room_id") or ""),
            )
        )
    return candidates


def profile_from_monitor_info(module: Any, info: dict[str, Any]) -> dict[str, Any]:
    if "live_viewers" in info or "aweme_count" in info:
        return {**info}
    return module.simplify(info)


def enrich_profile(profile: dict[str, Any], raw_user: dict[str, Any]) -> dict[str, Any]:
    room_data = _load_room_data(raw_user)
    live_start_at = _extract_live_start_at(room_data, raw_user)
    enriched = {**profile}
    enriched["live_start_at"] = live_start_at
    enriched["live_duration_seconds"] = _duration_seconds(live_start_at) if live_start_at else None
    if room_data and not enriched.get("live_viewers"):
        enriched["live_viewers"] = room_data.get("user_count")
    return enriched


def _load_room_data(user: dict[str, Any]) -> dict[str, Any]:
    raw_room = user.get("room_data") or user.get("roomData")
    if isinstance(raw_room, dict):
        return raw_room
    if isinstance(raw_room, str) and raw_room.startswith("{"):
        try:
            parsed = json.loads(raw_room)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _extract_live_start_at(room_data: dict[str, Any], raw_user: dict[str, Any]) -> str:
    candidates = [
        room_data.get("create_time"),
        room_data.get("start_time"),
        room_data.get("live_start_time"),
        room_data.get("stream_start_time"),
        raw_user.get("live_start_time"),
    ]
    for value in candidates:
        if not value:
            continue
        try:
            timestamp = int(value)
        except (TypeError, ValueError):
            continue
        if timestamp > 10_000_000_000:
            timestamp = timestamp // 1000
        return datetime.fromtimestamp(timestamp).isoformat(timespec="seconds")
    return ""


def _duration_seconds(start_at: str) -> int | None:
    try:
        return max(0, int((datetime.now() - datetime.fromisoformat(start_at)).total_seconds()))
    except ValueError:
        return None


def append_monitor_user(settings_path: Path, label: str, sec_uid: str, homepage_url: str = "") -> AddUserResult:
    label = label.strip()
    sec_uid = sec_uid.strip()
    if not label:
        raise ValueError("User label is required.")
    if not sec_uid:
        raise ValueError("sec_uid is required.")

    url = homepage_url.strip() or f"https://www.douyin.com/user/{sec_uid}"
    if "/user/" not in url:
        url = f"https://www.douyin.com/user/{sec_uid}"

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    existing = settings_path.read_text(encoding="utf-8") if settings_path.exists() else ""
    for entry in _parse_settings_text(existing):
        if entry["sec_uid"] == sec_uid:
            return AddUserResult(
                added=False,
                entry=UserEntry(label=entry["label"], url=entry["url"], sec_uid=sec_uid),
                message="User already exists in settings.txt.",
            )

    prefix = "" if not existing or existing.endswith("\n") else "\n"
    with settings_path.open("a", encoding="utf-8", newline="\n") as file:
        file.write(f"{prefix}\n# {label}\n{url}\n")

    return AddUserResult(added=True, entry=UserEntry(label=label, url=url, sec_uid=sec_uid), message="User added.")


def _extract_live_status(user: dict[str, Any]) -> int:
    raw_room = user.get("room_data")
    if not raw_room:
        return 0
    try:
        room_data = json.loads(raw_room) if isinstance(raw_room, str) else raw_room
    except json.JSONDecodeError:
        return 0
    if isinstance(room_data, dict) and int(room_data.get("status") or 0) == 2:
        return 1
    return 0


def _parse_settings_text(text: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    current_label = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            current_label = stripped.lstrip("#").strip()
            continue
        match = re.search(r"user/([A-Za-z0-9_-]+)", stripped)
        if match:
            entries.append({"label": current_label, "url": stripped, "sec_uid": match.group(1)})
            current_label = ""
    return entries
