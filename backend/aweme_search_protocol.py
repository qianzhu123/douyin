"""backend/aweme_search_protocol.py — App 域协议搜索通道 (oracle 签名直发).

用户诉求 (2026-11-08): 添加主播的名称搜索首选协议通道, 替代浏览器 XHR 拦截;
浏览器三级链 (raw → external → CDP) 保留作降级。

配方移植自 lab 仓 (D:/code/android/androidre/douyin):
- tools/e112f_signed_search.py  v1 GET 37 参数模板 + e103b 头族
- tools/sign_client.py          设备 oracle (frida 27042, libmetasec 0x2e01c0)
- docs/e112_search.md §5.2/§5.3 定论:
  * oracle 只认最小双键罐 install_id=<真值>\r\nsessionid=<真值> (多余键致拒,
    2026-11-08 复证: 6键 OK / 第7键起全 null)
  * 发送用全量真罐 + e103b 头族
  * sc=0 无 antispam/hit_shark 壳 (签名白名单破 shark)

可达性 (启用条件, 全满足才走协议, 否则静默回落浏览器链):
  ① adb devices 有 device 状态的机子
  ② frida-server 27042 端口可达 (adb forward 已建)
  ③ 设备上有非 musically 的 aweme 活进程 (§89: musically=Splash 假进程)

响应解析 (2026-11-08 用户确认: 全树扫, 不依赖 type=4 user_list 卡):
  user 卡按 rank 抖动 (陈泽两次搜索一次有一 lần无), 但 aweme 卡的 author 对象
  恒带完整资料 → 递归扫全树收 (nickname+sec_uid) 对象, sec_uid 去重,
  author.follower_count 可信; relation_label.user_list 无粉丝数作次选。
纪律: 只读 GET; 真机不在不重试不拖时。

签名源演进 (2026-11-12):
  ① unidbg 离线签名 (E37f v26 快照移植 SEVEN HEADS, 首选) — 纯 Java 离线出七头
     blob ~12s/次, 不依赖真机/frida/aweme 进程; 只需 lab 仓 e37i 快照文件 +
     真实 sessionid (签名输入)。旧负结果 (E17b 工作队列随机态) 已被快照移植推翻。
  ② 设备 oracle (frida 27042 直调, 降级) — 真机在且 probe 通过才用, 5s 超时。
两级都失败 → 抛 RuntimeError 由上层静默回落浏览器链。
"""
from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# ---- 通道总开关 (2026-11-12: unidbg 离线签名首选, 设备 oracle 降级) ----
CHANNEL_ENABLED = True

# ---- lab 仓资源定位 (配置化, 找不到即通道不可用 → 静默回落) ----
_LAB_ROOTS = [
    Path(r"D:\code\android\androidre\douyin"),
]
ADB_SERIAL = "192.168.43.35:5555"
FRIDA_PORT = 27042
SIGN_TIMEOUT_S = 5      # 设备 oracle 单次签名超时 (超时即回落, 不拖时)
UNIDBG_TIMEOUT_S = 120  # unidbg 离线签名超时 (~12s 常态, 首次含 mvn compile 放宽)
HTTP_TIMEOUT_S = 15
ORACLE_MAX_CALLS = 40  # 单 oracle 会话签名次数上限 (§48.3 会话漂移防线, 超限重建)

MVN = r"D:\tools\Dev\Runtime\apache-maven\bin\mvn"
JDK = r"D:\tools\Dev\Runtime\jdk21"

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def _lab_root() -> Path | None:
    for p in _LAB_ROOTS:
        if (p / "tools" / "sign_client.py").exists() \
                and (p / "unidbg-lab" / "pom.xml").exists():
            return p
    return None


def _read_lab_files(root: Path) -> dict[str, str] | None:
    try:
        return {
            "headers": (root / "logs" / "e103b_headers.json").read_text(encoding="utf-8"),
            "cookie": (root / "tools" / "real_cookie.txt").read_text(encoding="utf-8"),
        }
    except Exception:
        return None


# ---- 可达性探测 (cheap, 每次搜索前跑; 失败立刻回落) ----
_probe_cache: dict = {"ts": 0.0, "ok": False}
_PROBE_TTL = 30  # 30s 内复用探测结果


def probe_available() -> tuple[bool, str]:
    """三条件探测: adb 设备 / frida 端口 / aweme 活进程。返回 (ok, reason)。"""
    import socket

    now = time.time()
    if now - _probe_cache["ts"] < _PROBE_TTL:
        return _probe_cache["ok"], _probe_cache.get("reason", "")

    ok, reason = False, ""
    try:
        out = subprocess.run(["adb", "-s", ADB_SERIAL, "get-state"],
                             capture_output=True, timeout=5).stdout.decode().strip()
        if out != "device":
            reason = f"adb state={out or 'none'}"
        else:
            with socket.create_connection(("127.0.0.1", FRIDA_PORT), timeout=2):
                pass  # 端口可达
            ps = subprocess.run(
                ["adb", "-s", ADB_SERIAL, "shell", "ps -A -o PID,NAME,STAT"],
                capture_output=True, timeout=8).stdout.decode(errors="replace")
            live = [l for l in ps.splitlines()
                    if "com.ss.android.ugc.aweme" in l and " Z " not in l]
            if not live:
                reason = "aweme 进程不在跑"
            else:
                ok = True
    except Exception as exc:
        reason = f"探测失败: {str(exc)[:60]}"

    _probe_cache.update(ts=now, ok=ok, reason=reason)
    return ok, reason


# ---- oracle 会话 (懒建, 漂移自愈: 冷启重建) ----
_oracle_lock = threading.Lock()
_oracle = None            # SignOracle 实例 (lab 模块动态加载)
_oracle_calls = 0         # 本会话已签次数
_lab_mods: dict = {}


def _load_lab_modules(root: Path) -> dict:
    """动态加载 lab 仓 sign_client (隔离 sys.path, 不污染后端包)。"""
    if "sc" in _lab_mods:
        return _lab_mods
    import importlib.util
    import sys

    old_path = sys.path[:]
    sys.path.insert(0, str(root / "tools"))
    try:
        spec = importlib.util.spec_from_file_location(
            "douyin_lab_sign_client", root / "tools" / "sign_client.py")
        sc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sc)
        sc.ADB = f"adb -s {ADB_SERIAL}"
        sc.FRIDA_EP = f"127.0.0.1:{FRIDA_PORT}"
        _lab_mods["sc"] = sc
    finally:
        sys.path[:] = old_path
    return _lab_mods


def _get_oracle(root: Path) -> Any:
    """取健康 oracle; 漂移/超限则重建 (SignOracle.start 会 attach 现有进程)。"""
    global _oracle, _oracle_calls
    sc = _load_lab_modules(root)["sc"]
    with _oracle_lock:
        if _oracle is not None:
            if _oracle_calls >= ORACLE_MAX_CALLS:
                try:
                    _oracle.detach()
                except Exception:
                    pass
                _oracle = None
            else:
                return _oracle
        orc = sc.SignOracle(auto_start=False)
        orc.start()
        _oracle = orc
        _oracle_calls = 0
        return orc


def _sign(root: Path, url: str, min_jar: str) -> dict | None:
    """签一次; 返回 heads dict 或 None (超时/null/漂移)。"""
    global _oracle_calls
    try:
        orc = _get_oracle(root)
        r = orc.sign(url, min_jar, timeout=SIGN_TIMEOUT_S)
        _oracle_calls += 1
        if "heads" in r and r["heads"]:
            return r["heads"]
        # 失败一次即判会话不健康 → 弃用 (下次搜索重建)
        try:
            orc.detach()
        except Exception:
            pass
        with _oracle_lock:
            globals()["_oracle"] = None
        return None
    except Exception:
        with _oracle_lock:
            globals()["_oracle"] = None
        return None


# ---- unidbg 离线签名 (E37f v26 快照移植, 2026-11-12 SEVEN HEADS) ----
_unidbg_lock = threading.Lock()  # mvn 仓并发写保护 (同一 JVM 目标串行签)


def _sign_unidbg(root: Path, url: str, min_jar: str) -> dict:
    """离线签一次 → heads dict。失败/超时抛 RuntimeError (由上层回落 oracle/浏览器)。

    纯 Java 离线: JNI_OnLoad + e37i 快照移植 + 直调 0x2e01c0, 无真机/frida 依赖。
    cookie 的 \\r\\n 用 | 转义传命令行 (v26 约定); 出签行 BLOB=X-H|v|X-H|v|...
    """
    ck_pipe = min_jar.replace("\r\n", "|")
    cmd = [MVN + ".cmd", "-q", "compile", "exec:java",
           "-Dexec.mainClass=com.re.douyin.E37fSnapshotTransplant",
           f"-Dexec.args={url} {ck_pipe}"]
    env = dict(os.environ)
    env["JAVA_HOME"] = JDK
    with _unidbg_lock:
        try:
            proc = subprocess.run(cmd, cwd=str(root / "unidbg-lab"), env=env,
                                  capture_output=True, timeout=UNIDBG_TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"unidbg 签名超时 >{UNIDBG_TIMEOUT_S}s") from exc
    out = (proc.stdout or b"").decode("utf-8", errors="replace") + \
          (proc.stderr or b"").decode("utf-8", errors="replace")
    m = re.search(r"^BLOB=(.*)$", out, re.M)
    if not m:
        raise RuntimeError(f"unidbg 未出 BLOB (rc={proc.returncode}), "
                           f"尾部: {out[-200:]}")
    heads: dict[str, str] = {}
    parts = m.group(1).strip().split("|")
    for i in range(len(parts) - 1):
        if re.fullmatch(r"X-[A-Za-z-]+", parts[i]):
            heads[parts[i]] = parts[i + 1]
    if not heads:
        raise RuntimeError("unidbg blob 解析出 0 头")
    return heads


# ---- 37 参数模板 (e112f common_q 原样移植) ----
def _common_q(**extra) -> dict:
    q = dict(iid="1765185923070852", device_id="5967213509056", ac="wifi",
             channel="huawei_1128_64", aid=1128, app_name="aweme",
             version_code=400600, version_name="40.6.0", device_platform="android",
             os="android", ssmix="a", device_type="Pixel 2 XL", device_brand="google",
             language="zh", os_api=30, os_version=11, manifest_version_code=400601,
             resolution="1440*2712", dpi=560, update_version_code=40609900,
             _rticket=int(time.time() * 1000), package="com.ss.android.ugc.aweme",
             first_launch_timestamp=0, cpu_support64="true", host_abi="arm64-v8a",
             is_guest_mode=0, app_type="normal", minor_status=0, appTheme="light",
             is_preinstall=0, need_personal_recommend=1, is_android_pad=0,
             is_android_fold=0, ts=int(time.time()),
             cdid="61eafaf6-6a6b-4423-bf00-bfa1b53b1fb5")
    q.update(extra)
    return q


# ---- 响应解析: 全树扫 user 对象 (author 优先, sec_uid 去重) ----
# display_info 粉丝文本: "粉丝: 4152.2万" / "粉丝: 1234" / "粉丝: 1.2亿"
_FANS_RE = re.compile(r"^粉丝[::]\s*([\d.]+)(万|亿)?$")
# 抖音号文本: "抖音号：9896719." (display_info 里兜底)
_UID_RE = re.compile(r"^抖音号[::]\s*(.+)$")


def _parse_wan(text: str) -> int:
    """'4152.2万'/'1.2亿'/'1234' → int。"""
    text = text.strip()
    try:
        if text.endswith("万"):
            return int(float(text[:-1]) * 10000)
        if text.endswith("亿"):
            return int(float(text[:-1]) * 100000000)
        return int(float(text))
    except ValueError:
        return 0


def _extract_users(body: dict) -> list[dict]:
    """递归收 (nickname+sec_uid) 对象。author 类 (带 follower_count) 优先排序。"""
    hits: dict[str, dict] = {}

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            # raw_data 是内嵌 JSON **字符串** (dynamic_patch.small_user 卡, 2026-11-08):
            # 真正带粉丝数的 user_list 藏在里面, 必须解出来递归, 否则全树扫漏掉唯一粉丝源
            rd = o.get("raw_data")
            if isinstance(rd, str) and '"user_list"' in rd:
                try:
                    walk(json.loads(rd))
                except Exception:
                    pass
            sec = o.get("sec_uid")
            if sec and o.get("nickname"):
                cur = hits.get(sec)
                # 已有带粉丝数的记录则不降级覆盖; 否则可被带粉丝数的补强
                enriched = {
                    "sec_uid": sec,
                    "nickname": o.get("nickname") or "",
                    "unique_id": o.get("unique_id") or o.get("short_id") or "",
                    "signature": o.get("signature") or "",
                    "follower_count": int(o.get("follower_count") or 0),
                    "following_count": int(o.get("following_count") or 0),
                    "total_favorited": int(o.get("total_favorited") or 0),
                    "live_status": int(o.get("live_status") or 0),
                    "room_id": str(o.get("room_id_str") or o.get("room_id") or ""),
                }
                av = (o.get("avatar_thumb") or o.get("avatar_medium")
                      or o.get("avatar_larger") or {})
                urls = (av.get("url_list") or []) if isinstance(av, dict) else []
                enriched["avatar_url"] = urls[0] if urls else ""
                # display_info 粉丝数 (2026-11-08 实测: 接口已不给结构化 follower_count,
                # 粉丝数藏在 display_info[].text 如 "粉丝: 4152.2万" → 解析回数值)
                for di in (o.get("display_info") or []):
                    if isinstance(di, dict):
                        txt = str(di.get("text") or "")
                        m = _FANS_RE.match(txt.strip())
                        if m and not enriched["follower_count"]:
                            enriched["follower_count"] = _parse_wan(m.group(1))
                if cur is None:
                    hits[sec] = enriched
                elif enriched["follower_count"] and not cur["follower_count"]:
                    # 补粉丝数等丰富字段 (relation_label 出的无粉丝壳)
                    for k, v in enriched.items():
                        if v and not cur.get(k):
                            cur[k] = v
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(body)
    # 查询词命中的排前, 再按粉丝数降序
    return sorted(hits.values(),
                  key=lambda u: (-u["follower_count"], -u["live_status"]))


def _is_antispam_shell(body: dict) -> bool:
    nil = ((body.get("business_config") or {}).get("search_nil_info") or {})
    return bool(nil.get("search_nil_type") in ("antispam_check", "hit_shark"))


# ---- 对外主入口 ----
def search(keyword: str, count: int = 20) -> tuple[list[dict], str]:
    """协议通道搜索。返回 (candidates, note); 不可用/失败抛 RuntimeError 由上层回落。

    candidates 为 SearchCandidate 兼容 dict 列表; note 为进度标注 ("protocol+...")。
    签名两级: unidbg 离线 (首选, 无真机) → 设备 oracle (降级, 真机在才通)。
    """
    root = _lab_root()
    if root is None:
        raise RuntimeError("lab 仓不存在")
    if not CHANNEL_ENABLED:
        raise RuntimeError("协议通道已禁用 (走浏览器登录链)")
    files = _read_lab_files(root)
    if files is None:
        raise RuntimeError("lab 头族/罐文件缺失")

    sid = f"{int(time.time() * 1000)}7000000000000000000"
    q = _common_q(query=keyword, count=count, offset=0, search_id=sid,
                  search_channel="aweme_general", enter_from="search_page",
                  is_global_rewrite=0, history_vid="", match_user_keyword=1,
                  sort_type=0, publish_time=0, filter_type=0)
    url = ("https://aweme.snssdk.com/aweme/v1/general/search/single/?"
           + urllib.parse.urlencode(q))

    # 最小双键罐 (§5.2 定论): 真值 install_id + sessionid
    ck_pairs = [p.strip() for p in files["cookie"].strip().split(";") if "=" in p]
    ck_map = {p.split("=", 1)[0]: p.split("=", 1)[1] for p in ck_pairs}
    if "install_id" not in ck_map or "sessionid" not in ck_map:
        raise RuntimeError("真罐缺 install_id/sessionid")
    min_jar = f"install_id={ck_map['install_id']}\r\nsessionid={ck_map['sessionid']}"

    # 签名两级: unidbg 离线首选; oracle 需真机, probe 不过直接跳过不拖时
    heads: dict[str, str] | None = None
    sign_note = ""
    try:
        heads = _sign_unidbg(root, url, min_jar)
        sign_note = "unidbg 离线签名"
    except Exception as exc:
        ok, reason = probe_available()
        if not ok:
            raise RuntimeError(f"unidbg 签名失败({str(exc)[:60]}), oracle 也不可达: {reason}")
        heads = _sign(root, url, min_jar)
        if not heads:
            raise RuntimeError("unidbg 与设备 oracle 双双签名失败")
        sign_note = "oracle 七头"

    headers = dict(json.loads(files["headers"]))
    headers["Cookie"] = files["cookie"].strip().replace("\r\n", "; ")
    headers["User-Agent"] = ("com.ss.android.ugc.aweme/400601 (Linux; U; Android 11; "
                             "zh_CN; Pixel 2 XL; Build/RP1A.201005.004.A1)")
    headers.update(heads)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S, context=CTX) as resp:
        body = json.loads(resp.read().decode("utf-8", errors="replace"))

    if body.get("status_code") != 0:
        raise RuntimeError(f"协议搜索 sc={body.get('status_code')}")
    if _is_antispam_shell(body):
        raise RuntimeError("协议搜索命中 antispam/hit_shark 壳")
    users = _extract_users(body)
    if not users:
        raise RuntimeError("协议搜索无用户结果")
    return users, f"protocol ({sign_note}, {len(users)} 候选)"
