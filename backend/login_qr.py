"""backend/login_qr.py — 抖音扫码登录 (网页 QR → douyin_profile 会话).

用户诉求 (2026-11-08): 界面上加登录按钮, 点击显示二维码扫码登录,
替代手动跑 external/douyin-user-search/login.bat。

实现 (两段式, 2026-11-08 实测定案):
① 扫码段: 一次性干净 context 打开 douyin.com 登录页 (持久 profile 直接开
  登录页会卡住不发 get_qrcode — profile 旧状态干扰, 实测复现; 干净 context
  秒发)。页面自己的 JS 处理签名/轮询/换码, 我们旁听:
  - /passport/web/get_qrcode/     → data.qrcode (base64 PNG) 展示给用户
  - /passport/web/check_qrconnect/ → status: new/scanned/confirmed/expired
  confirmed 后页面跳转, ctx.cookies() 出现 sessionid。
② 落库段: 用持久 profile 再开一个 context, add_cookies 把扫码段拿到的
  douyin.com cookies 注入 → douyin_profile 持久化, 搜索链路立即可用。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

PROFILE_DIR = (
    Path(__file__).resolve().parent.parent
    / "external" / "douyin-user-search" / "douyin_profile"
)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0"
)
LOGIN_URL = "https://www.douyin.com/login_page?service=https%3A%2F%2Fwww.douyin.com"
WAIT_SECONDS = 300  # 扫码总时限


def _b64(qr: str) -> str:
    return qr if ";base64," in qr else "data:image/png;base64," + qr


class QrLoginSession:
    """单例扫码登录会话 (app.py 持有)。同一时刻只允许一个线程占用 profile。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: dict = {
            "phase": "idle",  # idle/running/success/cancelled/timeout/error
            "qr": "", "avatar": "", "device": "",
            "already_logged_in": False,
            "error": "", "started_at": None, "done_at": None,
        }

    # ---- 对外 API ----
    def start(self) -> dict:
        with self._lock:
            if self._state["phase"] == "running":
                return dict(self._state)
            self._cancel.clear()
            self._state = {
                "phase": "running", "qr": "", "avatar": "", "device": "",
                "already_logged_in": False,
                "error": "", "started_at": time.time(), "done_at": None,
            }
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="qr-login")
            self._thread.start()
            return dict(self._state)

    def cancel(self) -> None:
        self._cancel.set()

    def status(self) -> dict:
        with self._lock:
            return dict(self._state)

    def _set(self, **kw) -> None:
        with self._lock:
            self._state.update(kw)

    # ---- 线程主体 ----
    def _run(self) -> None:
        from playwright.sync_api import sync_playwright

        try:
            with sync_playwright() as pw:
                # ---- ① 扫码段: 干净 context (持久 profile 开登录页会卡, 见模块注释) ----
                browser = pw.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled",
                          "--no-sandbox"])
                ctx = browser.new_context(user_agent=UA,
                                          viewport={"width": 1280, "height": 900})
                page = ctx.new_page()

                def on_resp(resp) -> None:
                    url = resp.url
                    try:
                        if "get_qrcode" in url:
                            d = (resp.json().get("data") or {})
                            if d.get("qrcode"):
                                self._set(qr=_b64(d["qrcode"]),
                                          avatar="", device="")
                        elif "check_qrconnect" in url:
                            d = (resp.json().get("data") or {})
                            st = d.get("status")
                            if st == "scanned":
                                info = d.get("scan_user_info") or {}
                                dev = (d.get("scan_device_info") or {})
                                self._set(
                                    avatar=info.get("avatar_url") or "",
                                    device=dev.get("scan_device_display_name") or "")
                            elif st in ("expired", "refused") and d.get("qrcode"):
                                # 页面 SDK 会自动换码, 跟新展示
                                self._set(qr=_b64(d["qrcode"]),
                                          avatar="", device="")
                    except Exception:
                        pass  # 单条响应解析失败不影响主流程

                page.on("response", on_resp)
                page.goto(LOGIN_URL, wait_until="domcontentloaded",
                          timeout=30000)
                page.wait_for_timeout(2000)
                # 切到扫码 tab (页面默认落在验证码登录)
                try:
                    page.locator("text=扫码登录").first.click(timeout=5000)
                except Exception:
                    pass  # 有些变体直接落扫码页

                deadline = time.time() + WAIT_SECONDS
                ok = False
                while time.time() < deadline and not self._cancel.is_set():
                    page.wait_for_timeout(1000)
                    if self._has_session(ctx):
                        ok = True
                        break

                if ok:
                    page.wait_for_timeout(2500)  # 等 cookie 链路落定
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

                # ---- ② 落库段: 把扫码段 cookies 注入持久 profile ----
                self._persist_cookies(pw, cookies)
                self._set(phase="success", done_at=time.time())
        except Exception as exc:
            self._set(phase="error", error=str(exc)[:200], done_at=time.time())

    def _persist_cookies(self, pw, cookies: list[dict]) -> None:
        """扫码段拿到的 douyin.com cookies 写进 douyin_profile (搜索链路用)。"""
        if not cookies:
            raise RuntimeError("扫码成功但未取到 cookies")
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        browser = pw.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=True, user_agent=UA,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled",
                  "--no-sandbox"])
        try:
            browser.add_cookies(cookies)
            # 访问一次主页让 profile 落盘 storage state
            pg = browser.pages[0] if browser.pages else browser.new_page()
            pg.goto("https://www.douyin.com/", wait_until="domcontentloaded",
                    timeout=30000)
            pg.wait_for_timeout(2000)
        finally:
            try:
                browser.close()
            except Exception:
                pass

    @staticmethod
    def _has_session(ctx) -> bool:
        try:
            for c in ctx.cookies():
                if c.get("name") == "sessionid" and c.get("value"):
                    return True
        except Exception:
            pass
        return False


qr_login_session = QrLoginSession()
