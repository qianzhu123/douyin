"""backend/liked_profiles.py — 喜欢/收藏面板的多账户登录态 (Chrome profile 槽位).

用户诉求 (2026-11-14): 面板要显示"当前登录是谁"、支持扫码登录、支持切换账户
展示——每个账户各自独立登录态。

设计 (定案 2026-11-14):
  - 每账户 = 一个独立 Chrome profile 目录 (槽位)。槽位 1 复用既有
    external/douyin-user-search/douyin_profile (9222 常驻 Chrome 现用, 不动它);
    槽位 2/3 = douyin_profile_2 / _3。
  - 同一时刻**只有一个槽位活跃** (9222 端口只服务一个 Chrome)。切换账户 =
    停旧 Chrome → 用目标槽位起新 Chrome → 等 CDP 就绪。
  - 登录态探测 = 直接问 CDP 要 cookie (不猜 profile 文件), 有 sessionid 即已登录;
    再开一个轻量页面读 /user/self 的昵称头像, 供 UI 显示"当前登录是谁"。

与 login_qr.py 的关系:
  login_qr 是搜索链路的登录 (写死槽位 1), 保持原样不动; 本模块独立实现
  "登录到指定槽位", 供喜欢面板用。两者共用槽位 1 的 profile 时互不冲突
  (都只是往同一个 profile 注入 cookie)。

纪律: 只读喜欢/收藏数据; 切换账户会重启 headless Chrome (静默, 不弹窗)。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROFILE_ROOT = PROJECT_ROOT / "external" / "douyin-user-search"
CHROME_EXE = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / \
    "Google" / "Chrome" / "Application" / "chrome.exe"
CDP_PORT = 9222
CDP = f"http://127.0.0.1:{CDP_PORT}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0")

# 槽位定义: id → profile 目录名 + 展示名。槽位 1 复用既有 profile (9222 现用)。
SLOTS = {
    1: {"dir": "douyin_profile", "name": "账户 1"},
    2: {"dir": "douyin_profile_2", "name": "账户 2"},
    3: {"dir": "douyin_profile_3", "name": "账户 3"},
}
DEFAULT_SLOT = 1


def slot_dir(slot: int) -> Path:
    s = SLOTS.get(int(slot)) or SLOTS[DEFAULT_SLOT]
    return PROFILE_ROOT / s["dir"]


def slot_name(slot: int) -> str:
    return (SLOTS.get(int(slot)) or SLOTS[DEFAULT_SLOT])["name"]


# ---------------- CDP 基础 ----------------

def cdp_alive(timeout: float = 1.5) -> bool:
    import httpx
    try:
        return httpx.get(f"{CDP}/json/version", timeout=timeout).status_code == 200
    except Exception:
        return False


def _cdp_pid() -> int | None:
    """当前占用 CDP 端口的 chrome PID (无则 None)。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-NetTCPConnection -LocalPort {CDP_PORT} -State Listen "
             f"-ErrorAction SilentlyContinue | Select-Object -First 1).OwningProcess"],
            capture_output=True, timeout=15)
        txt = (out.stdout or b"").decode("utf-8", "replace").strip()
        return int(txt) if txt.isdigit() else None
    except Exception:
        return None


def _active_profile_dir() -> str:
    """当前 CDP Chrome 的 --user-data-dir (判活跃槽位; 无则空串)。"""
    pid = _cdp_pid()
    if not pid:
        return ""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\").CommandLine"],
            capture_output=True, timeout=15)
        cl = (out.stdout or b"").decode("utf-8", "replace")
    except Exception:
        return ""
    import re
    m = re.search(r'--user-data-dir=(?:"([^"]+)"|(\S+))', cl)
    return (m.group(1) or m.group(2)) if m else ""


def active_slot() -> int | None:
    """当前活跃槽位号 (CDP Chrome 的 profile 落在哪个槽位)。"""
    cur = _active_profile_dir().rstrip("\\/").lower()
    if not cur:
        return None
    for sid in SLOTS:
        if str(slot_dir(sid)).rstrip("\\/").lower() == cur:
            return sid
    return None


def _clear_profile_lock(profile: Path) -> None:
    """删 Singleton* 锁 (上次非正常退出的残留)。"""
    if not profile.exists():
        return
    for p in profile.glob("Singleton*"):
        try:
            p.unlink()
        except Exception:
            pass


def _chrome_cmd(slot: int) -> list[str]:
    return [
        str(CHROME_EXE), "--headless=new",
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={slot_dir(slot)}",
        "--no-first-run", "--no-default-browser-check", "--disable-gpu",
        "--disable-features=AutomationControlled",
        "--window-position=-32000,-32000", "--window-size=1280,800",
        "--restore-last-session=false", "--noerrdialogs",
        "https://www.douyin.com",
    ]


def stop_cdp_chrome() -> bool:
    """停掉当前 CDP Chrome (只杀占用该端口的 chrome, 不碰用户自己的 Chrome)。"""
    pid = _cdp_pid()
    if not pid:
        return True
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, timeout=20)
    except Exception:
        pass
    for _ in range(20):
        if not cdp_alive(0.5):
            return True
        time.sleep(0.5)
    return not cdp_alive(0.5)


def start_cdp_chrome(slot: int, wait: float = 20.0) -> bool:
    """用指定槽位起 headless Chrome, 等 CDP 就绪。"""
    if not CHROME_EXE.exists():
        raise RuntimeError(f"未找到 Chrome: {CHROME_EXE}")
    d = slot_dir(slot)
    d.mkdir(parents=True, exist_ok=True)
    _clear_profile_lock(d)
    subprocess.Popen(_chrome_cmd(slot), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    t0 = time.time()
    while time.time() - t0 < wait:
        if cdp_alive(0.8):
            return True
        time.sleep(0.6)
    return False


def switch_slot(slot: int) -> dict:
    """切换到指定槽位 (停旧 → 起新)。返回 {ok, slot, restarted, error}。"""
    slot = int(slot)
    if slot not in SLOTS:
        return {"ok": False, "slot": slot, "error": f"未知槽位 {slot}"}
    cur = active_slot()
    if cur == slot and cdp_alive():
        return {"ok": True, "slot": slot, "restarted": False}
    stopped = stop_cdp_chrome()
    if not stopped:
        return {"ok": False, "slot": slot, "error": "旧 Chrome 未能停止 (端口仍占用)"}
    ok = start_cdp_chrome(slot)
    return {"ok": ok, "slot": slot, "restarted": True,
            "error": "" if ok else "Chrome 启动后 CDP 未就绪"}


# ---------------- 登录态探测 ----------------

def _bridge(coro_factory, timeout: float = 120.0):
    """在无事件循环的线程里跑 async playwright (后端是同步 FastAPI 端点)。"""
    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(1) as ex:
        return ex.submit(lambda: asyncio.run(coro_factory())).result(timeout=timeout)


def login_state(with_profile: bool = True) -> dict:
    """当前 CDP Chrome 的登录态。

    返回 {cdp, slot, logged_in, nickname, avatar, uid, error}
    logged_in 以 CDP cookie 里的 sessionid 为准 (不猜 profile 文件)。
    """
    out = {"cdp": cdp_alive(), "slot": None, "logged_in": False,
           "nickname": "", "avatar": "", "uid": "", "error": ""}
    if not out["cdp"]:
        out["slot"] = None
        return out
    out["slot"] = active_slot()

    async def _run():
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            b = await pw.chromium.connect_over_cdp(CDP)
            ctx = b.contexts[0]
            cks = await ctx.cookies("https://www.douyin.com")
            logged = any(c.get("name") in ("sessionid", "sid_tt") and c.get("value")
                         for c in cks)
            res = {"logged_in": logged, "nickname": "", "avatar": "", "uid": ""}
            if logged and with_profile:
                page = await ctx.new_page()
                try:
                    await page.goto("https://www.douyin.com/user/self",
                                    wait_until="domcontentloaded", timeout=25000)
                    await page.wait_for_timeout(4500)
                    info = await page.evaluate("""() => {
                      const q = (s) => document.querySelector(s);
                      // 昵称: self 页头部有账号名; 头像: 第一张头像 img
                      let nick = document.title.replace(/\\s*-\\s*抖音\\s*$/, '').trim();
                      const imgs = Array.from(document.querySelectorAll('img'))
                        .filter(i => /aweme-avatar|avatar/.test(i.src||''));
                      return {nick, avatar: imgs.length ? imgs[0].src : ''};
                    }""")
                    res["nickname"] = (info.get("nick") or "").strip()
                    res["avatar"] = info.get("avatar") or ""
                except Exception as e:
                    res["error"] = str(e)[:80]
                finally:
                    try:
                        await page.close()
                    except Exception:
                        pass
            return res

    try:
        r = _bridge(_run, timeout=90)
        out.update({k: r.get(k) for k in ("logged_in", "nickname", "avatar")})
    except Exception as exc:
        out["error"] = str(exc)[:120]
    return out
