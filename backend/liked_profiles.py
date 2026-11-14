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

# 槽位定义 (2026-11-14 四轮改动态): 不再写死 1-3 — 用户要能添加任意多个账户。
# 槽位 N → douyin_profile (N=1) / douyin_profile_N; 上限 MAX_SLOT 防滥用。
# 现存槽位由 discover_slots() 扫描 profile 目录 + 本地数据文件得出。
SLOTS = {
    1: {"dir": "douyin_profile", "name": "账户 1"},
    2: {"dir": "douyin_profile_2", "name": "账户 2"},
    3: {"dir": "douyin_profile_3", "name": "账户 3"},
}
DEFAULT_SLOT = 1
MAX_SLOT = 8


def slot_dir(slot: int) -> Path:
    n = int(slot)
    if n < 1 or n > MAX_SLOT:
        n = DEFAULT_SLOT
    return PROFILE_ROOT / ("douyin_profile" if n == 1 else f"douyin_profile_{n}")


def slot_name(slot: int) -> str:
    return f"账户 {int(slot)}"


def discover_slots() -> list[int]:
    """现存槽位 = 有 profile 目录的 + 有本地喜欢/收藏数据的 + 当前活跃的。"""
    import re as _re
    found = {DEFAULT_SLOT}
    if PROFILE_ROOT.exists():
        for p in PROFILE_ROOT.iterdir():
            if not p.is_dir():
                continue
            if p.name == "douyin_profile":
                found.add(1)
            else:
                m = _re.fullmatch(r"douyin_profile_(\d+)", p.name)
                if m and 2 <= int(m.group(1)) <= MAX_SLOT:
                    found.add(int(m.group(1)))
    # 本地数据文件 (data/liked/<slot>_<kind>.json)
    try:
        from .config import DATA_DIR
        sd = DATA_DIR / "liked"
        if sd.exists():
            for f in sd.iterdir():
                m = _re.fullmatch(r"(\d+)_(like|collect)\.json", f.name)
                if m and 1 <= int(m.group(1)) <= MAX_SLOT:
                    found.add(int(m.group(1)))
    except Exception:
        pass
    act = active_slot()
    if act:
        found.add(act)
    return sorted(found)


def next_slot() -> int | None:
    """下一个可添加的空槽位 (无则 None)。"""
    used = set(discover_slots())
    for n in range(1, MAX_SLOT + 1):
        if n not in used:
            return n
    return None


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
    if slot < 1 or slot > MAX_SLOT:
        return {"ok": False, "slot": slot, "error": f"槽位号超出 1-{MAX_SLOT}: {slot}"}
    cur = active_slot()
    if cur == slot and cdp_alive():
        return {"ok": True, "slot": slot, "restarted": False}
    stopped = stop_cdp_chrome()
    if not stopped:
        return {"ok": False, "slot": slot, "error": "旧 Chrome 未能停止 (端口仍占用)"}
    ok = start_cdp_chrome(slot)
    _invalidate_state_cache()
    return {"ok": ok, "slot": slot, "restarted": True,
            "error": "" if ok else "Chrome 启动后 CDP 未就绪"}


def delete_slot(slot: int) -> dict:
    """删除槽位: 清 profile 目录 + 本地喜欢/收藏数据 (退出该账户登录态)。

    被删槽位若是活跃槽位, 先停掉对应 Chrome。profile 目录删除后, 槽位从
    discover_slots 中消失 (动态槽位, 四轮) — 前端按钮列表随之缩短。
    """
    import shutil
    slot = int(slot)
    if slot < 2 or slot > MAX_SLOT:
        return {"ok": False, "error": f"槽位 {slot} 不允许删除 (槽位 1 共用, 上限 {MAX_SLOT})"}
    if active_slot() == slot:
        stop_cdp_chrome()
    d = slot_dir(slot)
    removed_profile = False
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
        removed_profile = True
    _invalidate_state_cache()
    return {"ok": True, "slot": slot, "removed_profile": removed_profile}


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


# ---------------- 登录态探测 ----------------

# 登录态 TTL 缓存 (2026-11-14 三轮): probe 每次开页烧 4.5-8s 是面板卡顿元凶,
# 60s 内直接回缓存; 切换/删除槽位后由调用方 _invalidate_state_cache() 清掉。
_STATE_TTL = 60.0
_state_cache: dict = {"ts": 0.0, "data": None}


def _invalidate_state_cache() -> None:
    _state_cache["ts"] = 0.0
    _state_cache["data"] = None


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
    """当前 CDP Chrome 的登录态 (60s TTL 缓存, 切换/登录/删除时清)。

    返回 {cdp, slot, logged_in, nickname, avatar, uid, error}
    logged_in 以 CDP cookie 里的 sessionid 为准 (不猜 profile 文件)。
    """
    now = time.time()
    if _state_cache["data"] is not None and now - _state_cache["ts"] < _STATE_TTL:
        return dict(_state_cache["data"])
    out = {"cdp": cdp_alive(), "slot": None, "logged_in": False,
           "nickname": "", "avatar": "", "uid": "", "error": ""}
    if not out["cdp"]:
        out["slot"] = None
        _state_cache.update({"ts": now, "data": dict(out)})
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
        # 切槽位瞬间旧 Chrome 被杀, 轮询中的 probe 页会被腰斩 → "page has been
        # closed" 瞬态误报 (2026-11-14 用户实测)。不再把这种写进 error 展示,
        # 只标注 probe 失败; 登录态以 cookie 探测单独重试一次为准。
        msg = str(exc)
        if "has been closed" in msg or "Target closed" in msg:
            try:
                b2_probe = cdp_alive()
                out["error"] = ""
                if not b2_probe:
                    out["cdp"] = False
            except Exception:
                pass
        else:
            out["error"] = msg[:120]
    _state_cache.update({"ts": time.time(), "data": dict(out)})
    return out
