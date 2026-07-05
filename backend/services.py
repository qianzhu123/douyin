from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .config import (
    DOWNLOAD_OUTPUT_DIR,
    DOWNLOADER_DIR,
    MONITOR_DIR,
    PROFILE_CACHE_FILE,
    SEARCH_DIR,
    SEARCH_HEADLESS,
    SEARCH_PROFILE_DIR,
    USERS_FILE,
)
from .schemas import AddUserResult, DownloadJob, ProfileResult, SearchCandidate, UserEntry, WatchEvent, WatchStatus, now_iso
from .tool_loader import load_module

try:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover - dependency is declared for runtime
    PlaywrightTimeoutError = TimeoutError
    async_playwright = None


class MonitorService:
    def __init__(self) -> None:
        self.module = load_module("douyin_monitor_tool", MONITOR_DIR / "main.py")
        self.search_module = load_module("douyin_user_search_tool", SEARCH_DIR / "douyin_search.py") if (SEARCH_DIR / "douyin_search.py").exists() else None
        self.raw_search_module = load_module("douyin_user_raw_search_tool", SEARCH_DIR / "raw.py") if (SEARCH_DIR / "raw.py").exists() else None
        ensure_project_users(USERS_FILE, self.module.parse_settings())
        self._watch_task: asyncio.Task | None = None
        self._watch_lock = asyncio.Lock()
        self._watch_targets: list[dict[str, Any]] = []
        self._watch_profiles: dict[str, ProfileResult] = {}
        self._watch_events: deque[WatchEvent] = deque(maxlen=100)
        self._live_now: set[str] = set()
        self._interval = 30
        self._duration_minutes = 30
        self._round = 0
        self._started_at = ""
        self._end_at = ""
        self._last_checked_at = ""
        self._search_lock = asyncio.Lock()

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
        if self.raw_search_module is not None:
            candidates = await asyncio.to_thread(self._search_users_with_raw_module, keyword)
            if candidates:
                return candidates[:12]

        if self.search_module is not None:
            candidates = await asyncio.to_thread(self._search_users_with_external_module, keyword)
            if candidates:
                return candidates[:12]

        payload: dict[str, Any] | None = None
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = await browser.new_context(
                viewport={"width": 1600, "height": 1000},
                user_agent=self.module.UA,
            )
            page = await context.new_page()
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
                await context.close()
                await browser.close()

        if not payload:
            return []
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

        results = await self.module.fetch_profiles_parallel(targets)
        profiles = [self._to_profile_result(entry, info) for entry, info in zip(targets, results)]
        upsert_profile_cache(PROFILE_CACHE_FILE, [profile.model_dump() for profile in profiles])
        return profiles

    async def start_watch(self, target_ids: list[str], interval: int, duration_minutes: int = 30, end_at: str = "") -> WatchStatus:
        async with self._watch_lock:
            await self.stop_watch()
            self._watch_targets = self.resolve_targets(target_ids)
            self._interval = max(5, interval)
            self._duration_minutes = max(1, duration_minutes)
            self._round = 0
            self._started_at = now_iso()
            self._end_at = end_at or (datetime_from_iso(self._started_at) + timedelta(minutes=self._duration_minutes)).isoformat(timespec="seconds")
            self._last_checked_at = ""
            self._watch_profiles = {}
            self._watch_events.clear()
            self._live_now.clear()
            self._add_event("info", f"Started live polling for {len(self._watch_targets)} user(s).")
            self._watch_task = asyncio.create_task(self._watch_loop())
            return self.watch_status()

    async def stop_watch(self) -> WatchStatus:
        if self._watch_task and not self._watch_task.done():
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass
            self._add_event("info", "Stopped live polling.")
        self._watch_task = None
        return self.watch_status()

    def watch_status(self) -> WatchStatus:
        running = self._watch_task is not None and not self._watch_task.done()
        return WatchStatus(
            running=running,
            interval=self._interval,
            duration_minutes=self._duration_minutes,
            round=self._round,
            started_at=self._started_at,
            end_at=self._end_at,
            last_checked_at=self._last_checked_at,
            targets=[UserEntry(**entry) for entry in self._watch_targets],
            profiles=list(self._watch_profiles.values()),
            events=list(self._watch_events),
        )

    async def shutdown(self) -> None:
        await self.stop_watch()
        await self.module.close_browser()

    async def _watch_loop(self) -> None:
        while True:
            if self._end_at and datetime_from_iso(self._end_at) <= datetime_from_iso(now_iso()):
                self._add_event("info", "Polling stopped after configured duration.")
                self._watch_task = None
                return
            self._round += 1
            self._last_checked_at = now_iso()
            try:
                results = await self.module.fetch_profiles_parallel(self._watch_targets)
                for entry, info in zip(self._watch_targets, results):
                    profile = self._to_profile_result(entry, info)
                    self._watch_profiles[entry["sec_uid"]] = profile
                    self._record_live_transition(entry, profile)
                upsert_profile_cache(PROFILE_CACHE_FILE, [profile.model_dump() for profile in self._watch_profiles.values()])
                failed = sum(1 for profile in self._watch_profiles.values() if not profile.ok)
                self._add_event("info", f"Round {self._round}: checked {len(self._watch_targets)} user(s), failed {failed}.")
            except Exception as exc:
                self._add_event("error", f"Polling failed: {exc}")
            await asyncio.sleep(self._interval)

    def _record_live_transition(self, entry: dict[str, Any], profile: ProfileResult) -> None:
        sec_uid = entry["sec_uid"]
        label = entry.get("label") or sec_uid[:16]
        if not profile.ok or not profile.profile:
            self._add_event("error", f"{label}: query failed", sec_uid)
            return

        info = profile.profile
        is_live = info.get("live_status") == 1
        nickname = info.get("nickname") or label
        if is_live and sec_uid not in self._live_now:
            self._live_now.add(sec_uid)
            viewers = info.get("live_viewers")
            suffix = f" ({viewers} viewers)" if viewers not in (None, "") else ""
            started = info.get("live_start_at")
            started_suffix = f", started at {started}" if started else ""
            self._add_event("live", f"{nickname} is live{suffix}{started_suffix}.", sec_uid)
        elif not is_live and sec_uid in self._live_now:
            self._live_now.remove(sec_uid)
            self._add_event("offline", f"{nickname} is offline.", sec_uid)

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

    def _add_event(self, level: str, message: str, sec_uid: str = "") -> None:
        self._watch_events.append(WatchEvent(time=now_iso(), level=level, message=message, sec_uid=sec_uid))


class DownloadService:
    def __init__(self) -> None:
        self.module = load_module("douyin_downloader_tool", DOWNLOADER_DIR / "downloader.py")
        self.jobs: dict[str, DownloadJob] = {}
        self._executor = ThreadPoolExecutor(max_workers=3)

    def create_job(self, text: str, mode: int, output_dir: str, comments: bool, selected_urls: list[str] | None = None) -> DownloadJob:
        urls = extract_douyin_urls(text)
        if selected_urls:
            selected = set(selected_urls)
            urls = [url for url in urls if url in selected]
        if not urls:
            raise ValueError("No Douyin URL found in input.")

        resolved_output = str(Path(output_dir) if output_dir else DOWNLOAD_OUTPUT_DIR)
        job = DownloadJob(
            id=str(uuid.uuid4()),
            status="queued",
            created_at=now_iso(),
            updated_at=now_iso(),
            input=text,
            urls=urls,
            mode=mode,
            output_dir=resolved_output,
            comments=comments,
        )
        self.jobs[job.id] = job
        self._add_job_log(job.id, "info", f"Queued {len(urls)} URL(s).")
        self._ensure_executor().submit(self._run_job, job.id)
        return job

    def preview(self, text: str, deep: bool = False) -> dict[str, Any]:
        urls = extract_douyin_urls(text)
        if not urls:
            raise ValueError("No Douyin URL found in input.")
        items = [self._preview_item(url, index, deep) for index, url in enumerate(urls, 1)]
        return {"items": items}

    def list_jobs(self) -> list[DownloadJob]:
        return sorted(self.jobs.values(), key=lambda job: job.created_at, reverse=True)

    def get_job(self, job_id: str) -> DownloadJob | None:
        return self.jobs.get(job_id)

    def _run_job(self, job_id: str) -> None:
        job = self.jobs[job_id]
        job.status = "running"
        job.updated_at = now_iso()
        self._add_job_log(job_id, "info", "Started download job.")
        try:
            results: list[dict[str, Any]] = []
            failed = 0
            for index, url in enumerate(job.urls, 1):
                self._add_job_log(job_id, "info", f"[{index}/{len(job.urls)}] Processing {url}")
                try:
                    result = self.module.download_douyin(url, job.output_dir, job.mode, job.comments)
                    result = result if isinstance(result, dict) else {"result": result}
                    result["url"] = url
                    result["ok"] = True
                    results.append(result)
                    self._add_job_log(job_id, "info", f"[{index}/{len(job.urls)}] Finished {result.get('title') or url}")
                except Exception as exc:
                    failed += 1
                    results.append({"url": url, "ok": False, "error": str(exc)})
                    self._add_job_log(job_id, "error", f"[{index}/{len(job.urls)}] Failed: {exc}")
            job.results = results
            job.status = "error" if failed else "done"
            if failed:
                job.error = f"{failed} of {len(job.urls)} URL(s) failed."
            else:
                self._add_job_log(job_id, "info", "Finished all downloads.")
        except Exception as exc:
            job.status = "error"
            job.error = str(exc)
            self._add_job_log(job_id, "error", str(exc))
        finally:
            job.updated_at = now_iso()

    def _ensure_executor(self) -> ThreadPoolExecutor:
        if not hasattr(self, "_executor"):
            self._executor = ThreadPoolExecutor(max_workers=3)
        return self._executor

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
    pattern = re.compile(
        r"https?://(?:v\.douyin\.com/|www\.douyin\.com/|(?:www\.)?iesdouyin\.com/).+?"
        r"(?=https?://(?:v\.douyin\.com/|www\.douyin\.com/|(?:www\.)?iesdouyin\.com/)|[\s\"'<>，。；、)）]|$)"
    )
    for match in pattern.finditer(text):
        cleaned = match.group(0).rstrip(".,;!?")
        if ("douyin.com" in cleaned or "iesdouyin.com" in cleaned) and cleaned not in seen:
            seen.add(cleaned)
            urls.append(cleaned)
    return urls


def infer_douyin_url_type(url: str) -> str:
    if "/note/" in url:
        return "note"
    if "/video/" in url:
        return "video"
    if "/jingxuan" in url:
        return "video"
    return "link"


def preview_douyin_url(module: Any, url: str) -> dict[str, Any]:
    with module.sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            viewport={"width": 1600, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        )
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            try:
                page.wait_for_selector("video, h1, [data-e2e='video-title'], [class*='note'], [class*='note-detail']", timeout=12000)
            except Exception:
                pass
            page.wait_for_timeout(1500)
            meta = module._extract_stats(page) or {}
            content_type = module._extract_content_type(page)
            cover_url = _extract_cover_url(page)
            details: dict[str, Any] = {
                "type": content_type,
                "title": meta.get("title", ""),
                "author": meta.get("author", ""),
                "cover_url": cover_url,
                "duration": 0,
                "media": [],
            }
            if content_type == "video":
                info = module._extract_video_info(page)
                details["title"] = info.get("title") or details["title"]
                details["author"] = info.get("author") or details["author"]
                details["duration"] = _normalize_duration(_extract_video_duration(page))
                details["media"] = [
                    {
                        "index": 1,
                        "type": "video",
                        "duration": details["duration"],
                        "cover_url": cover_url,
                    }
                ]
            elif content_type == "slide":
                slide_info = module._extract_slides_info(page) or {}
                details["title"] = slide_info.get("title") or details["title"]
                details["author"] = slide_info.get("author") or details["author"]
                slides = slide_info.get("slides") or []
                details["media"] = [
                    {
                        "index": int(slide.get("index", index - 1)) + 1,
                        "type": slide.get("media_type") or "image",
                        "duration": _normalize_duration(slide.get("duration") or 0),
                        "cover_url": slide.get("best_image_url") or (slide.get("image_urls") or [""])[0],
                    }
                    for index, slide in enumerate(slides, 1)
                ]
            elif content_type == "image":
                images = _extract_image_previews(page)
                details["media"] = [
                    {"index": index, "type": "image", "duration": 0, "cover_url": image_url}
                    for index, image_url in enumerate(images, 1)
                ]
                if images and not details["cover_url"]:
                    details["cover_url"] = images[0]
            return details
        finally:
            context.close()
            browser.close()


def _normalize_duration(value: Any) -> int:
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return 0
    if duration > 1000:
        duration = duration / 1000
    return int(duration)


def _extract_video_duration(page) -> int:
    return page.evaluate(
        """() => {
            const vd = window.SSR_RENDER_DATA?.app?.videoDetail;
            const raw = vd?.video?.duration || vd?.duration || 0;
            if (raw) return raw;
            const video = document.querySelector('video');
            return video && Number.isFinite(video.duration) ? Math.round(video.duration) : 0;
        }"""
    )


def _extract_cover_url(page) -> str:
    return page.evaluate(
        """() => {
            const vd = window.SSR_RENDER_DATA?.app?.videoDetail;
            const video = vd?.video || {};
            const cover = video.cover || video.originCover || video.dynamicCover || vd?.cover;
            const urls = cover?.urlList || cover?.url_list || cover?.url_list_0 || [];
            if (Array.isArray(urls) && urls.length) return urls[0];
            const img = document.querySelector('img[src*="douyinpic"], img[src*="aweme"]');
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


def datetime_from_iso(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.max


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
