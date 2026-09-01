"""
抖音用户信息监控工具
- 查询用户主页信息（关注、粉丝、获赞、直播状态等）
- 轮询检测直播状态，开播时弹窗通知

用户输入支持三种方式（空格分隔多个）：
  1. 关键词 → 在 settings.txt 的注释行中模糊匹配（如 "梦鱼"、"Whys"）
  2. 完整 URL → 直接解析（如 "https://www.douyin.com/user/MS4wLjAB..."）
  3. sec_uid → 直接使用（如 "MS4wLjABAAAA..."）
  4. 不传参 → 使用 settings.txt 中的全部用户
"""

import asyncio
import json
import re
import sys
import threading
from pathlib import Path
from datetime import datetime

from playwright.async_api import async_playwright, Page, BrowserContext


# ─── 配置加载 ───────────────────────────────────────────────────────

SETTINGS_FILE = Path(__file__).parent / "settings.txt"
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

POLL_INTERVAL = 30

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)


def parse_interval(args: list[str]) -> tuple[list[str], int]:
    """从参数列表中提取 --interval=N 参数"""
    rest = []
    interval = POLL_INTERVAL
    i = 0
    while i < len(args):
        arg = args[i]
        m = re.match(r"^--interval=(\d+)$", arg)
        if m:
            interval = int(m.group(1))
            i += 1
            continue
        m = re.match(r"^-i=(\d+)$", arg)
        if m:
            interval = int(m.group(1))
            i += 1
            continue
        if arg in ("--interval", "-i") and i + 1 < len(args) and args[i + 1].isdigit():
            interval = int(args[i + 1])
            i += 2
            continue
        rest.append(arg)
        i += 1
    if interval < 5:
        print(f"  ⚠️  间隔不能小于5秒，已自动调整为5秒")
        interval = 5
    return rest, interval


def parse_settings(path: Path = SETTINGS_FILE) -> list[dict]:
    """解析 settings.txt，注释行(# xxx)作为标签绑定下一行 URL"""
    entries: list[dict] = []
    if not path.exists():
        return entries
    current_label = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            current_label = stripped.lstrip("#").strip()
            continue
        sec_uid = extract_sec_uid(stripped)
        if sec_uid:
            entries.append({"label": current_label, "url": stripped, "sec_uid": sec_uid})
            current_label = ""
    return entries


def match_keywords(settings: list[dict], keywords: list[str]) -> list[dict]:
    """根据关键词在 settings 中模糊匹配"""
    matched = []
    seen: set[str] = set()
    for entry in settings:
        label = entry.get("label", "").lower()
        for kw in keywords:
            if kw.lower() in label:
                if entry["sec_uid"] not in seen:
                    matched.append(entry)
                    seen.add(entry["sec_uid"])
                break
    return matched


def classify_arg(arg: str, settings: list[dict]) -> dict | None:
    """对单个输入参数分类：URL / sec_uid / 关键词"""
    sec_uid = extract_sec_uid(arg)
    if sec_uid:
        label = ""
        for entry in settings:
            if entry["sec_uid"] == sec_uid:
                label = entry.get("label", "")
                break
        return {"label": label, "url": arg, "sec_uid": sec_uid}

    if arg.startswith("MS4w") and len(arg) > 20:
        label = ""
        for entry in settings:
            if entry["sec_uid"] == arg:
                label = entry.get("label", "")
                break
        return {"label": label, "url": f"https://www.douyin.com/user/{arg}", "sec_uid": arg}

    matches = match_keywords(settings, [arg])
    if matches:
        return matches[0]
    return None


def resolve_targets(user_args: list[str] | None = None) -> list[dict]:
    """解析目标用户列表"""
    settings = parse_settings()
    if not user_args:
        return settings

    results: list[dict] = []
    seen: set[str] = set()
    unmatched: list[str] = []

    for arg in user_args:
        arg = arg.strip()
        if not arg:
            continue
        entry = classify_arg(arg, settings)
        if entry and entry["sec_uid"] not in seen:
            results.append(entry)
            seen.add(entry["sec_uid"])
        else:
            unmatched.append(arg)

    if unmatched:
        batch_matched = match_keywords(settings, unmatched)
        for entry in batch_matched:
            if entry["sec_uid"] not in seen:
                results.append(entry)
                seen.add(entry["sec_uid"])

    if not results:
        print("  ⚠️  输入的参数均无法匹配，回退到 settings.txt 全部用户")
        return settings
    return results


def extract_sec_uid(url: str) -> str | None:
    m = re.search(r"user/([A-Za-z0-9_-]+)", url)
    return m.group(1) if m else None


# ─── 浏览器核心（持久化 context + 页面池）────────────────────────────

_PLAYWRIGHT = None
_BROWSER = None
_CONTEXT: BrowserContext | None = None
_page_pool: dict[str, Page] = {}


async def init_browser():
    """初始化浏览器（只启动一次，context 复用）。

    进程级池偶尔会因抖音侧通道掐断/页面崩溃导致 _CONTEXT 被关闭、底层
    chromium 进程死亡，而 _BROWSER/_CONTEXT 仍指向旧对象。下一次调用
    `context.new_page()` 会抛 `TargetClosedError` 并把池永久毒化。
    这里每次都检查底层连接是否还活着，死了就 `close_browser()` 后重建。
    """
    global _PLAYWRIGHT, _BROWSER, _CONTEXT

    if _CONTEXT is not None:
        try:
            # browser.connected / context.closed 不存在稳定属性，靠发起一次
            # `pages` 元数据调用作为心跳；底层 WS 已断时这一行会抛 ConnectionError。
            await asyncio.wait_for(_CONTEXT.pages, timeout=2.0)
        except Exception:
            # 池死了 — 静默清空,落到下面的重建分支
            try:
                await close_browser()
            except Exception:
                _BROWSER = None
                _CONTEXT = None
                _PLAYWRIGHT = None
                _page_pool.clear()

    if _CONTEXT is not None:
        return _CONTEXT

    _PLAYWRIGHT = await async_playwright().start()
    _BROWSER = await _PLAYWRIGHT.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    )
    _CONTEXT = await _BROWSER.new_context(user_agent=UA, viewport={"width": 1280, "height": 800})
    return _CONTEXT


async def close_browser():
    global _PLAYWRIGHT, _BROWSER, _CONTEXT, _page_pool
    for page in _page_pool.values():
        try:
            await page.close()
        except Exception:
            pass
    _page_pool.clear()
    if _CONTEXT:
        await _CONTEXT.close()
        _CONTEXT = None
    if _BROWSER:
        await _BROWSER.close()
        _BROWSER = None
    if _PLAYWRIGHT:
        await _PLAYWRIGHT.stop()
        _PLAYWRIGHT = None


# 连续失败计数 & 退避
_fail_count: dict[str, int] = {}
_MAX_RETRIES = 2         # 单次获取最多重试次数
_BACKOFF_ROUNDS = 3      # 连续失败 N 整轮后进入退避（跳过若干轮）


async def fetch_profile(sec_uid: str, _retry: int = 0) -> dict | None:
    """
    获取用户数据，内置重试和退避：
    - 单次请求最多重试 _MAX_RETRIES 次（关闭旧页面重建）
    - 连续 _BACKOFF_ROUNDS 轮失败后，跳过该用户若干轮再试
    """
    context = await init_browser()
    captured = {}

    async def handle_response(response):
        if "user/profile/other" in response.url and response.status == 200:
            try:
                body = await response.json()
                if body.get("user"):
                    captured["data"] = body["user"]
            except Exception:
                pass

    if sec_uid in _page_pool:
        page = _page_pool[sec_uid]
        page.on("response", handle_response)
        try:
            await page.reload(wait_until="domcontentloaded", timeout=20000)
            for _ in range(20):
                if "data" in captured:
                    break
                await asyncio.sleep(0.3)
        except Exception:
            try:
                await page.close()
            except Exception:
                pass
            _page_pool.pop(sec_uid, None)
            # reload 失败 → 重试（重新打开页面）
            if _retry < _MAX_RETRIES:
                return await fetch_profile(sec_uid, _retry + 1)
            return None
        finally:
            try:
                page.remove_listener("response", handle_response)
            except Exception:
                pass
    else:
        page = await context.new_page()
        page.on("response", handle_response)
        try:
            await page.goto(
                f"https://www.douyin.com/user/{sec_uid}",
                wait_until="domcontentloaded",
                timeout=25000,
            )
            for _ in range(20):
                if "data" in captured:
                    break
                await asyncio.sleep(0.3)

            if "data" not in captured:
                try:
                    result = await page.evaluate("""
                        () => {
                            const scripts = document.querySelectorAll('script[id="RENDER_DATA"]');
                            if (scripts.length > 0) {
                                const raw = decodeURIComponent(scripts[0].textContent);
                                const parsed = JSON.parse(raw);
                                const user = parsed.app?.user?.info;
                                if (user) return user;
                            }
                            return null;
                        }
                    """)
                    if result:
                        captured["data"] = result
                except Exception:
                    pass

            if "data" in captured:
                _page_pool[sec_uid] = page
            else:
                await page.close()
                # 首次打开也没拿到数据 → 重试
                if _retry < _MAX_RETRIES:
                    return await fetch_profile(sec_uid, _retry + 1)
                return None
        except Exception:
            try:
                await page.close()
            except Exception:
                pass
            if _retry < _MAX_RETRIES:
                return await fetch_profile(sec_uid, _retry + 1)
            return None
        finally:
            try:
                page.remove_listener("response", handle_response)
            except Exception:
                pass

    raw = captured.get("data")
    if not raw:
        # 没拿到数据，但页面还在（reload 成功了只是没拦截到 API）
        if sec_uid in _page_pool:
            try:
                await _page_pool[sec_uid].close()
            except Exception:
                pass
            _page_pool.pop(sec_uid, None)
        if _retry < _MAX_RETRIES:
            return await fetch_profile(sec_uid, _retry + 1)
        return None
    return simplify(raw)


async def fetch_profiles_parallel(entries: list[dict], skip_set: set[str] | None = None) -> list[dict | None]:
    """并行获取多个用户数据，skip_set 中的用户本次跳过"""
    if skip_set is None:
        skip_set = set()
    tasks = []
    for e in entries:
        if e["sec_uid"] in skip_set:
            tasks.append(_skip())
        else:
            tasks.append(fetch_profile(e["sec_uid"]))
    return await asyncio.gather(*tasks)


async def _skip():
    return None


def simplify(user: dict) -> dict:
    """从原始 JSON 中提取关键字段"""
    live_status = user.get("live_status", 0)
    room_data_str = user.get("room_data") or user.get("roomData") or ""
    room_id = user.get("room_id", 0)
    if isinstance(room_id, str):
        room_id = int(room_id) if room_id.isdigit() else 0

    live_viewers = None
    if room_data_str and isinstance(room_data_str, str) and room_data_str.startswith("{"):
        try:
            rd = json.loads(room_data_str)
            live_viewers = rd.get("user_count")
            if rd.get("status") in (2, 4):
                live_status = 1
        except Exception:
            pass

    follower_count = user.get("follower_count", user.get("followerCount", 0))
    following_count = user.get("following_count", user.get("followingCount", 0))
    total_favorited = user.get("total_favorited", user.get("totalFavorited", 0))
    aweme_count = user.get("aweme_count", user.get("awemeCount", 0))

    return {
        "nickname": user.get("nickname", ""),
        "sec_uid": user.get("sec_uid") or user.get("secUid", ""),
        "uid": user.get("uid", ""),
        "unique_id": user.get("unique_id", ""),
        "follower_count": follower_count,
        "following_count": following_count,
        "total_favorited": total_favorited,
        "aweme_count": aweme_count,
        "live_status": live_status,
        "room_id": room_id,
        "live_viewers": live_viewers,
        "signature": user.get("signature", ""),
        "ip_location": user.get("ip_location", ""),
    }


def format_number(n) -> str:
    if isinstance(n, str):
        try:
            n = int(n)
        except ValueError:
            return n
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f}万"
    return str(n)


def format_profile(info: dict) -> str:
    name = info["nickname"]
    fans = format_number(info["follower_count"])
    follow = format_number(info["following_count"])
    likes = format_number(info["total_favorited"])
    works = format_number(info["aweme_count"])
    live = "🔴 直播中" if info["live_status"] == 1 else "⬜ 未直播"
    viewers = ""
    if info["live_status"] == 1 and info.get("live_viewers"):
        viewers = f" 👥{format_number(info['live_viewers'])}人"
    uid_display = info.get("unique_id") or info.get("uid", "")
    sig = info.get("signature", "").replace("\n", " ")[:50]
    ip = info.get("ip_location", "")
    return (
        f"  👤 {name}  {uid_display}\n"
        f"  粉丝 {fans}  关注 {follow}  获赞 {likes}  作品 {works}\n"
        f"  {live}{viewers}  {ip}\n"
        f"  签名: {sig}"
    )


# ─── Windows 通知 ───────────────────────────────────────────────────

def send_windows_notification(title: str, message: str):
    try:
        from winotify import Notification, audio
        toast = Notification(app_id="Douyin Monitor", title=title, msg=message, duration="short")
        toast.set_audio(audio.Default, loop=False)
        toast.show()
        return
    except ImportError:
        pass
    try:
        from plyer import notification
        notification.notify(title=title, message=message, app_name="Douyin Monitor", timeout=10)
        return
    except ImportError:
        pass
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x40)
    except Exception:
        pass


# ─── 指令: query ─────────────────────────────────────────────────────

async def cmd_query(user_args: list[str] | None = None):
    users = resolve_targets(user_args)
    if not users:
        print("❌ 没有可查询的用户")
        return

    source_desc = _describe_source(user_args, users)
    print(f"📋 {source_desc}  |  共 {len(users)} 个用户\n")

    results = await fetch_profiles_parallel(users)

    for i, (entry, info) in enumerate(zip(users, results)):
        label = entry.get("label", "")
        hint = f"({label})" if label else ""
        short_uid = entry["sec_uid"][:24] + "..."
        print(f"[{i+1}/{len(users)}] {short_uid} {hint}")
        if info:
            print(format_profile(info))
            save_user_data(info)
        else:
            print(f"  ⚠️  未能获取数据")
        print()

    await close_browser()


# ─── 指令: watch ─────────────────────────────────────────────────────

_notified_live: set[str] = set()


async def cmd_watch(user_args: list[str] | None = None, interval: int = POLL_INTERVAL):
    users = resolve_targets(user_args)
    if not users:
        print("❌ 没有可监控的用户")
        return

    source_desc = _describe_source(user_args, users)
    print(f"👁️ 开始监控直播状态  |  {source_desc}")
    print(f"📋 监控 {len(users)} 个用户  |  间隔 {interval}s\n")

    round_num = 0
    try:
        while True:
            round_num += 1
            now = datetime.now().strftime("%H:%M:%S")
            print(f"--- 第 {round_num} 轮 [{now}] ---")

            # 计算本轮需要跳过的用户（退避中）
            skip_set: set[str] = set()
            for uid, fails in _fail_count.items():
                if fails >= _BACKOFF_ROUNDS:
                    skip_set.add(uid)

            if skip_set:
                print(f"  🔄 {len(skip_set)} 个用户退避中（跳过本轮）")

            results = await fetch_profiles_parallel(users, skip_set)

            for entry, info in zip(users, results):
                sec_uid = entry["sec_uid"]
                label = entry.get("label", "")

                # 跳过退避中的用户
                if sec_uid in skip_set:
                    follow = _fail_count.get(sec_uid, 0)
                    skip_left = max(0, follow - _BACKOFF_ROUNDS + 2)
                    tag = label or sec_uid[:20] + "..."
                    print(f"  ⏭️  {tag} (退避中，{skip_left} 轮后重试)")
                    # 递减退避计数
                    _fail_count[sec_uid] = follow - 1
                    if _fail_count[sec_uid] <= 0:
                        _fail_count.pop(sec_uid, None)
                    continue

                if not info:
                    tag = label or sec_uid[:20] + "..."
                    print(f"  ⚠️  {tag} 获取失败")
                    _fail_count[sec_uid] = _fail_count.get(sec_uid, 0) + 1
                    continue

                # 成功 → 重置失败计数
                _fail_count.pop(sec_uid, None)

                nickname = info["nickname"]
                is_live = info["live_status"] == 1

                if is_live:
                    viewers = info.get("live_viewers")
                    viewers_str = f" 👥{format_number(viewers)}人" if viewers else ""
                    print(f"  🔴 {nickname} 正在直播{viewers_str}")

                    if sec_uid not in _notified_live:
                        _notified_live.add(sec_uid)
                        msg = f"{nickname} 开播了！"
                        if viewers:
                            msg += f" 当前 {format_number(viewers)} 人观看"
                        threading.Thread(
                            target=send_windows_notification,
                            args=("🔴 直播提醒", msg),
                            daemon=True,
                        ).start()
                        print(f"  📢 已发送通知: {nickname} 开播！")
                else:
                    print(f"  ⬜ {nickname} 未直播")
                    _notified_live.discard(sec_uid)

            print(f"  ⏳ {interval}s 后下一轮...\n")
            await asyncio.sleep(interval)

    except KeyboardInterrupt:
        print("\n⏹️  停止监控")
    finally:
        await close_browser()


# ─── 辅助 ─────────────────────────────────────────────────────────────

def _describe_source(user_args: list[str] | None, users: list[dict]) -> str:
    if not user_args:
        return "来源: settings.txt (全部)"
    labels = []
    for arg in user_args:
        arg = arg.strip()
        if not arg:
            continue
        if extract_sec_uid(arg):
            labels.append("URL")
        elif arg.startswith("MS4w") and len(arg) > 20:
            labels.append("sec_uid")
        else:
            labels.append(f'"{arg}"')
    settings = parse_settings()
    from_settings = sum(1 for u in users if any(u["sec_uid"] == s["sec_uid"] for s in settings))
    if from_settings == len(users) and len(users) == len(user_args):
        return f"来源: 关键词匹配 ({', '.join(labels)})"
    return f"来源: 手动输入 ({', '.join(labels)})"


def save_user_data(info: dict):
    uid = info.get("uid") or info.get("sec_uid", "unknown")
    path = DATA_DIR / f"{uid}.json"
    record = {"updated_at": datetime.now().isoformat(), "data": info}
    history = []
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing, list):
                history = existing
            else:
                history = [existing]
        except Exception:
            pass
    history.append(record)
    if len(history) > 100:
        history = history[-100:]
    path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


# ─── 主入口 ─────────────────────────────────────────────────────────

HELP_TEXT = """
Douyin Monitor - Live Status & Profile Tracker

Usage:
  python main.py query [keyword/URL/sec_uid ...]             Query profile info
  python main.py watch [keyword/URL/sec_uid ...] [-i SECS]   Watch live status
  python main.py help                                        Show help

Input (space-separated, multiple allowed):
  keyword    Fuzzy match against settings.txt labels
  URL        Full profile URL
  sec_uid    Encrypted user ID
  (empty)    Use all users from settings.txt

Interval (watch only):
  --interval=N  or  -i N  or  -i=N   Poll interval in seconds (default 30, min 5)

Examples:
  python main.py query                        -> all users in settings.txt
  python main.py query "https://..."          -> query by URL
  python main.py watch                        -> watch all, 30s interval
  python main.py watch -i 10                  -> 10s interval
  python main.py watch "https://..." -i 5     -> watch one user, 5s interval
"""


def read_from_file(filepath: str) -> tuple[list[str], int | None]:
    """
    从 bat 临时文件读取参数。
    文件格式：第1行=用户输入，第2行=间隔秒数。
    """
    p = Path(filepath)
    if not p.exists():
        return [], None
    lines = p.read_text(encoding="utf-8-sig").splitlines()

    user_line = lines[0].strip() if len(lines) > 0 else ""
    user_args = user_line.split() if user_line else []

    interval = None
    if len(lines) > 1:
        interval_str = lines[1].strip()
        if interval_str.isdigit():
            interval = int(interval_str)

    return user_args, interval


def interactive_prompt(cmd: str) -> tuple[list[str] | None, int | None]:
    """
    Python 处理交互式输入（彻底摆脱 CMD 的 & 解析问题）。
    返回 (user_args, interval) — interval 仅 watch 使用。
    输入的 URL 可能含 & 和 ? ，不能简单 split。
    策略：逐段识别 — 找到 https:// 就一直读到下一个 https:// 或行尾。
    """
    print("Press Enter to use settings.txt,")
    print("or input URLs/keywords (space-separated):")
    print()
    user_line = input().strip()

    if not user_line:
        print(f"[Users] settings.txt")
        user_args = None
    else:
        print(f"[Users] Custom input")
        # 智能分割：URL 不被空格切断
        user_args = _smart_split(user_line)

    interval = None
    if cmd == "watch":
        print()
        print("Press Enter for default 30s,")
        print("or input interval in seconds (min 5):")
        print()
        interval_str = input().strip()
        if not interval_str:
            print(f"[Interval] 30s (default)")
        else:
            try:
                interval = int(interval_str)
                print(f"[Interval] {interval}s")
            except ValueError:
                print(f"[Interval] Invalid, using 30s (default)")

    return user_args, interval


def _smart_split(line: str) -> list[str]:
    """
    智能分割用户输入行：
    - 如果行中没有 URL，直接按空格分割（关键词模式）
    - 如果行中有 URL（含 https://），URL 作为整体不被分割
      URL 之间用空格分隔，关键词同样用空格分隔
    """
    if "://" not in line:
        return line.split()

    # 包含 URL 的行：按 https:// 位置切割
    parts = []
    remaining = line.strip()
    while remaining:
        remaining = remaining.lstrip()
        if not remaining:
            break
        # 如果当前位置是 URL 开头
        if remaining.startswith("http://") or remaining.startswith("https://"):
            # 找下一个 URL 的起始位置（以 http 或 https 开头）
            next_url_pos = len(remaining)
            for proto in (" https://", " http://"):
                idx = remaining.find(proto, 1)
                if idx != -1 and idx < next_url_pos:
                    next_url_pos = idx
            url_part = remaining[:next_url_pos].strip()
            if url_part:
                parts.append(url_part)
            remaining = remaining[next_url_pos:]
        else:
            # 非URL内容（关键词），按空格取第一个
            first_space = remaining.find(" ")
            first_url = len(remaining)
            for proto in (" https://", " http://"):
                idx = remaining.find(proto)
                if idx != -1 and idx < first_url:
                    first_url = idx
            if first_space != -1 and first_space < first_url:
                kw = remaining[:first_space].strip()
                if kw:
                    parts.append(kw)
                remaining = remaining[first_space:]
            else:
                kw = remaining[:first_url].strip()
                if kw:
                    parts.append(kw)
                remaining = remaining[first_url:]

    return parts


def main():
    args = sys.argv[1:]

    if not args or args[0] in ("help", "-h", "--help"):
        print(HELP_TEXT.strip())
        sys.exit(0)

    cmd = args[0].lower()
    rest = args[1:]

    # 交互模式：bat 不传任何参数，Python 自己处理输入
    if rest and rest[0] == "--interactive":
        user_args, interval = interactive_prompt(cmd)
        if cmd == "query":
            asyncio.run(cmd_query(user_args))
        elif cmd == "watch":
            i = interval if interval is not None else POLL_INTERVAL
            if i < 5:
                i = 5
            asyncio.run(cmd_watch(user_args, i))
        sys.exit(0)

    # 命令行模式
    if cmd == "query":
        if "--from-file" in rest:
            idx = rest.index("--from-file")
            if idx + 1 < len(rest):
                file_user_args, _ = read_from_file(rest[idx + 1])
                rest = file_user_args + rest[:idx] + rest[idx + 2:]
        asyncio.run(cmd_query(rest if rest else None))
    elif cmd == "watch":
        file_interval = None
        if "--from-file" in rest:
            idx = rest.index("--from-file")
            if idx + 1 < len(rest):
                file_user_args, file_interval = read_from_file(rest[idx + 1])
                rest = file_user_args + rest[:idx] + rest[idx + 2:]
        user_args, cli_interval = parse_interval(rest)
        final_interval = file_interval if file_interval is not None else cli_interval
        if final_interval < 5:
            final_interval = 5
        asyncio.run(cmd_watch(user_args if user_args else None, final_interval))
    else:
        print(f"Unknown command: {cmd}")
        print("Run 'python main.py help' for usage")
        sys.exit(1)


if __name__ == "__main__":
    main()
