"""backend/liked_login.py — 扫码登录到指定 profile 槽位 (喜欢/收藏面板用).

与 login_qr.py 的关系 (2026-11-14):
  login_qr 是**搜索链路**的登录, profile 写死槽位 1, 保持原样不动。
  本模块独立实现"登录到指定槽位", 供喜欢面板的账户切换用。

实现 (沿用 login_qr 已验证的两段式):
  ① 扫码段: 一次性干净 context 开 douyin 登录页 (持久 profile 直接开登录页会卡住
     不发 get_qrcode — login_qr 实测复现), 旁听 get_qrcode / check_qrconnect,
     拿到二维码 base64 + 扫码状态; 页面自己的 JS 负责签名/轮询/换码。
  ② 落库段: 把扫码段 cookies 注入目标槽位 profile —— 两处都要落:
       a) profile 目录 (add_cookies 到 persistent context, 下次启动带登录态);
       b) **当前活跃的 CDP Chrome** (若它正是该槽位) → 立即生效, 免重启。

纪律: 只做登录, 不碰喜欢/收藏数据; 全程 headless; 单例, 同一时刻一个会话。
"""
from __future__ import annotations

import threading
import time

from . import liked_profiles as _lp

WAIT_SECONDS = 300
LOGIN_URL = "https://www.douyin.com/login_page?service=https%3A%2F%2Fwww.douyin.com"


def _b64(qr: str) -> str:
    return qr if ";base64," in qr else "data:image/png;base64," + qr


class LikedQrSession:
    """单例扫码会话 (登录到指定槽位)。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._state: dict = {
            "phase": "idle", "qr": "", "avatar": "", "device": "",
            "slot": None, "error": "", "started_at": None, "done_at": None,
            "injected_live": False,
        }

    def start(self, slot: int) -> dict:
        slot = int(slot)
        if int(slot) < 1 or int(slot) > _lp.MAX_SLOT:
            return {**self.status(), "phase": "error", "error": f"槽位号超出 1-{_lp.MAX_SLOT}: {slot}"}
        with self._lock:
            if self._state["phase"] == "running":
                return dict(self._state)
            self._cancel.clear()
            self._state = {
                "phase": "running", "qr": "", "avatar": "", "device": "",
                "slot": slot, "error": "", "started_at": time.time(),
                "done_at": None, "injected_live": False,
            }
            threading.Thread(target=self._run, args=(slot,), daemon=True,
                             name="liked-qr-login").start()
            return dict(self._state)

    def cancel(self) -> None:
        self._cancel.set()

    def status(self) -> dict:
        with self._lock:
            return dict(self._state)

    def _set(self, **kw) -> None:
        with self._lock:
            self._state.update(kw)

    def _run(self, slot: int) -> None:
        from playwright.sync_api import sync_playwright

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled",
                          "--no-sandbox"])
                ctx = browser.new_context(user_agent=_lp.UA,
                                          viewport={"width": 1280, "height": 900})
                page = ctx.new_page()

                def on_resp(resp) -> None:
                    url = resp.url
                    try:
                        if "get_qrcode" in url:
                            d = (resp.json().get("data") or {})
                            if d.get("qrcode"):
                                self._set(qr=_b64(d["qrcode"]), avatar="", device="")
                        elif "check_qrconnect" in url:
                            d = (resp.json().get("data") or {})
                            st = d.get("status")
                            if st == "scanned":
                                info = d.get("scan_user_info") or {}
                                dev = d.get("scan_device_info") or {}
                                self._set(avatar=info.get("avatar_url") or "",
                                          device=dev.get("scan_device_display_name") or "")
                            elif st in ("expired", "refused") and d.get("qrcode"):
                                self._set(qr=_b64(d["qrcode"]), avatar="", device="")
                    except Exception:
                        pass

                page.on("response", on_resp)
                page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)
                try:
                    page.locator("text=扫码登录").first.click(timeout=5000)
                except Exception:
                    pass

                deadline = time.time() + WAIT_SECONDS
                ok = False
                while time.time() < deadline and not self._cancel.is_set():
                    page.wait_for_timeout(1000)
                    if self._has_session(ctx):
                        ok = True
                        break
                if ok:
                    page.wait_for_timeout(2500)
                try:
                    cookies = ctx.cookies("https://www.douyin.com") if ok else []
                finally:
                    try:
                        browser.close()
                    except Exception:
                        pass

                if self._cancel.is_set() and not ok:
                    self._set(phase="cancelled", done_at=time.time())
                    return
                if not ok:
                    self._set(phase="timeout", error="5 分钟内未完成扫码",
                              done_at=time.time())
                    return
                if not cookies:
                    self._set(phase="error", error="扫码成功但未取到 cookies",
                              done_at=time.time())
                    return

                # ---- ② 落库: profile 目录 + (若活跃) 当前 CDP Chrome ----
                # 若该槽位正被常驻 CDP Chrome 占用, **跳过 persistent launch**
                # (profile 目录锁会让 launch_persistent_context 卡死 — 2026-11-14
                #  实测"扫码确认后一直加载/无法取消"的根因), 直接注入 CDP;
                # Chrome 自己会把 cookie 落盘到 profile, 下次启动自带登录态。
                if _lp.cdp_alive() and _lp.active_slot() == int(slot):
                    injected = self._inject_live(pw, slot, cookies)
                    if injected:
                        _lp._invalidate_state_cache()
                        self._set(phase="success", done_at=time.time(),
                                  injected_live=True)
                        return
                self._persist_profile(pw, slot, cookies)
                injected = self._inject_live(pw, slot, cookies)
                _lp._invalidate_state_cache()
                self._set(phase="success", done_at=time.time(), injected_live=injected)
        except Exception as exc:
            self._set(phase="error", error=str(exc)[:200], done_at=time.time())

    def _persist_profile(self, pw, slot: int, cookies: list[dict]) -> None:
        """写进槽位 profile 目录 (下次启动即带登录态)。"""
        d = _lp.slot_dir(slot)
        d.mkdir(parents=True, exist_ok=True)
        _lp._clear_profile_lock(d)
        b = pw.chromium.launch_persistent_context(
            str(d), headless=True, user_agent=_lp.UA,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
        try:
            b.add_cookies(cookies)
            pg = b.pages[0] if b.pages else b.new_page()
            pg.goto("https://www.douyin.com/", wait_until="domcontentloaded",
                    timeout=30000)
            pg.wait_for_timeout(2000)
        finally:
            try:
                b.close()
            except Exception:
                pass

    def _inject_live(self, pw, slot: int, cookies: list[dict]) -> bool:
        """当前 CDP Chrome 就在该槽位 → 直接注入 cookie, 免重启立即生效。"""
        if not _lp.cdp_alive() or _lp.active_slot() != int(slot):
            return False
        try:
            b = pw.chromium.connect_over_cdp(_lp.CDP)
            ctx = b.contexts[0]
            ctx.add_cookies(cookies)
            return True
        except Exception:
            return False

    @staticmethod
    def _has_session(ctx) -> bool:
        try:
            for c in ctx.cookies():
                if c.get("name") == "sessionid" and c.get("value"):
                    return True
        except Exception:
            pass
        return False


liked_qr_session = LikedQrSession()
