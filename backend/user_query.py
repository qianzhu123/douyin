"""任意用户查询 — 消费等级 + 粉丝团等级 (对应第三方 App "查询用户粉丝团" 的只读复刻).

端点链 (见 douyin-re-lab e122/e122b/e122c, 2026-11-23 全链验证):
1. 解析输入: 支持 数字uid / sec_uid / 抖音主页链接(短链或 /user/<sec_uid>/)
2. 消费等级  GET webcast.amemv.com/webcast/user/?target_uid=&user_open_id=
   &packed_level=2&with_host_info=0   → pay_grade{level,total_diamond_count,...}
   (需要签名头: unidbg 离线签名, 无真机依赖; e122 实测 target_uid 是唯一生效参数名)
3. 粉丝团列表 GET fandom.starplaylet.com/ent/fandom/user/fandoms/?uid=&offset=&count=
   &is_login=1   → 该用户加过的全部团 (top-level "fandoms", X-Tt-Token 即可, 无需签名;
   sc=8 表示 token 失效/游客态)
4. 每团个人数据 GET fandom.starplaylet.com/ent/fandom/personal/meta/?fandom_id=
   &target_user_id=   → joined_days/continues_sign_days/light_medal_cnt
   (等级/亲密度在 fandoms 列表项不含 — 灯牌等级只对登录者本人经
   webcast/fansclub/me/ 可查; 他人团等级服务端不下发, 显示"未公开")

凭证: 复用 data/fansclub_tokens/ 的 X-Tt-Token (fansclub.py load_tokens);
webcast 域签名走 douyin-re-lab 的 unidbg_runner (e121_unidbg_client 同款, ~12s/签)。

只读: 全链仅 GET, 不触碰 participate/gift/send 等任何写端点。
"""
from __future__ import annotations

import json
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

from .config import DATA_DIR

LAB_ROOT = Path(r"D:\code\android\androidre\douyin")

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

BASE_WEBCAST = "https://webcast.amemv.com"
BASE_FANDOM = "https://fandom.starplaylet.com"

UA = ("com.ss.android.ugc.aweme/400601 (Linux; U; Android 11; zh_CN; Pixel 2 XL; "
      "Build/RP1A.201005.004.A1)")

# 固定设备参数 (与 fansclub.py DEV 同源: 抓 token 的真机身份)
DEV = {
    "aid": 1128,
    "app_name": "aweme",
    "version_code": 400600,
    "device_platform": "android",
    "device_type": "Pixel 2 XL",
    "device_brand": "google",
    "os_version": 11,
    "os_api": 30,
    "channel": "huawei_1128_64",
    "device_id": "5967213509056",
    "iid": "1765185923070852",
    "cdid": "61eafaf6-6a6b-4423-bf00-bfa1b53b1fb5",
}

# webcast 域 Cookie (unidbg 签名 jar 与请求头同一身份; 与真机抓包一致)
_LAB_COOKIE_FILE = LAB_ROOT / "tools" / "real_cookie.txt"


def _cookie() -> str:
    return _LAB_COOKIE_FILE.read_text(encoding="utf-8").strip().replace("\r\n", "; ")


def _common_q(**extra) -> dict:
    q = {**DEV, "ac": "wifi", "os": "android", "ssmix": "a", "language": "zh",
         "manifest_version_code": 400601, "resolution": "1440*2712", "dpi": 560,
         "update_version_code": 40609900, "_rticket": int(time.time() * 1000),
         "package": "com.ss.android.ugc.aweme", "first_launch_timestamp": 0,
         "cpu_support64": "true", "host_abi": "arm64-v8a", "is_guest_mode": 0,
         "app_type": "normal", "minor_status": 0, "appTheme": "light",
         "is_android_pad": 0, "is_android_fold": 0, "ts": int(time.time())}
    q.update(extra)
    return q


# ---- unidbg 离线签名 (lab e121 同款; 懒加载 + 全局串行由 lab 侧锁保证) ----

_signer = None


def _signer_mod():
    global _signer
    if _signer is None:
        sys.path.insert(0, str(LAB_ROOT / "tools"))
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "e121_unidbg_client", LAB_ROOT / "tools" / "e121_unidbg_client.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _signer = mod
    return _signer


def _signed_get(url: str) -> dict:
    """unidbg 签名 + Cookie GET (webcast 域; 每签 ~12s, JVM 进程一次一签)."""
    mod = _signer_mod()
    ck = _cookie()
    iid = re.search(r"install_id=([^;]+)", ck).group(1)
    sid = re.search(r"sessionid=([^;]+)", ck).group(1)
    minjar = f"install_id={iid}\r\nsessionid={sid}"
    heads = mod.unidbg_sign(url, minjar)
    headers = {**mod.HDRS, "Cookie": ck, "User-Agent": mod.UA, **heads}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20, context=CTX) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _token_get(url: str, token: str) -> dict:
    """X-Tt-Token bare GET (fandom 域配方; fansclub._req 同形)."""
    headers = {"User-Agent": UA, "X-Tt-Token": token, "sdk-version": "2",
               "x-tt-ttnet-origin-host": "fandom.starplaylet.com",
               "Accept": "application/json"}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20, context=CTX) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


# ---- 输入解析 ----

_SEC_RE = re.compile(r"MS4wLjABAAAA[0-9A-Za-z_-]{38,}")
_SHORT_LINK_RE = re.compile(r"https?://v\.douyin\.com/[A-Za-z0-9_.\-]+/?", re.I)


def resolve_short_link(url: str) -> str | None:
    """v.douyin.com 短链 → 302 Location 提取 sec_uid (app.py resolve-link 同配方).

    分享口令里的完整文本可直接传入 — 先正则抠出短链再解。
    """
    m = _SHORT_LINK_RE.search(url or "")
    if not m:
        return None
    import httpx
    try:
        r = httpx.get(m.group(0), follow_redirects=False, timeout=8)
        loc = r.headers.get("location", "") or ""
    except Exception:
        return None
    m2 = _SEC_RE.search(loc)
    return m2.group(0) if m2 else None


# 抖音号 (unique_id): 字母数字 1-30 位, 可含 . _ (纯数字除外 — 纯数字优先按 uid)
_DOUYIN_ID_RE = re.compile(r"^[A-Za-z0-9._\-]{4,30}$")


def resolve_douyin_id(douyin_id: str) -> str | None:
    """抖音号 → sec_uid (aweme/v1/search/sug/ 联想卡; unidbg 签名).

    sug 的联想词 extra_info 对精确抖音号命中会带账号卡 sec_uid
    (陈泽 9896719. 实测; 非抖音号无结果, 精确度可靠, 覆盖 best-effort)。
    """
    q = _common_q(keyword=douyin_id, source="search_sug_app")
    url = "https://aweme.snssdk.com/aweme/v1/search/sug/?" + urlencode(q)
    try:
        d = _signed_get(url)
    except Exception:
        return None
    s = json.dumps(d, ensure_ascii=False) if isinstance(d, dict) else ""
    m = _SEC_RE.search(s)
    return m.group(0) if m else None


def parse_query(text: str) -> dict:
    """识别输入形态: {'kind': 'uid'|'sec_uid'|'douyin_id', 'value': str}.

    支持: 纯数字 uid / sec_uid 原文 / 含 sec_uid 的主页链接 / v.douyin.com 短链
    (含整段分享口令文本 — 302 Location 解析) / 抖音号 (字母数字, sug 联想解析)。
    """
    text = (text or "").strip()
    if not text:
        return {"kind": "invalid", "value": "", "reason": "输入为空"}
    if text.isdigit() and 6 <= len(text) <= 20:
        return {"kind": "uid", "value": text}
    m = _SEC_RE.search(text)
    if m:
        return {"kind": "sec_uid", "value": m.group(0)}
    if _SHORT_LINK_RE.search(text):
        sec = resolve_short_link(text)
        if sec:
            return {"kind": "sec_uid", "value": sec}
        return {"kind": "invalid", "value": text,
                "reason": "短链解析失败 (网络/链接失效), 请改粘主页长链或 sec_uid"}
    if _DOUYIN_ID_RE.match(text) and not text.isdigit():
        return {"kind": "douyin_id", "value": text}
    return {"kind": "invalid", "value": text, "reason": "无法识别: 需 uid / sec_uid / 抖音号 / 主页链接"}


# ---- sec_uid → uid ----

def resolve_uid(sec_uid: str, token: str) -> str | None:
    """解析 sec_uid → uid (aweme/v1/user/profile/other/, 需签名).

    webcast/user/ 只收数字 uid, 所以 sec_uid 输入必须先走这一步。
    配方同 lab e113_t6_poller (source_type=4; aweme-eagle user/n 已测 502 弃用)。
    """
    q = _common_q(sec_user_id=sec_uid, source_type=4)
    url = "https://aweme.snssdk.com/aweme/v1/user/profile/other/?" + urlencode(q)
    try:
        d = _signed_get(url)
    except Exception:
        return None
    u = (d.get("user") or {}) if isinstance(d, dict) else {}
    uid = u.get("uid") or u.get("id_str") or u.get("id")
    return str(uid) if uid else None


# ---- 三段查询 ----

def pay_grade_of(uid: str) -> dict | None:
    """消费等级 (webcast/user/?target_uid=; 任意用户; 需签名).

    20001 为偶发限频/瞬时错误 (2026-11-23 用户实测): 失败重试 1 次, 间隔 1.5s。
    """
    from urllib.parse import urlencode
    d = None
    for attempt in range(2):
        q = _common_q(target_uid=uid, user_open_id=uid, packed_level=2, with_host_info=0)
        url = BASE_WEBCAST + "/webcast/user/?" + urlencode(q)
        d = _signed_get(url)
        if isinstance(d, dict) and d.get("status_code") in (0, None):
            break
        if attempt == 0:
            time.sleep(1.5)
    if not isinstance(d, dict) or d.get("status_code") not in (0, None):
        raise RuntimeError(f"webcast/user status={d.get('status_code') if isinstance(d, dict) else '?'}")
    data = d.get("data") if isinstance(d.get("data"), dict) else d
    u = data.get("user") or data
    pg = u.get("pay_grade") or {}
    av = (u.get("avatar_medium") or u.get("avatar_thumb") or {})
    av_urls = av.get("url_list") or []
    return {
        "uid": str(u.get("id_str") or u.get("id") or uid),
        "sec_uid": u.get("sec_uid") or (data.get("sec_uid") if isinstance(data, dict) else "") or "",
        "nickname": u.get("nickname"),
        "avatar": av_urls[0] if av_urls else (av.get("uri") or ""),
        "pay_level": pg.get("level"),
        "total_diamond": pg.get("total_diamond_count"),
        "grade_min": pg.get("this_grade_min_diamond"),
        "grade_max": pg.get("this_grade_max_diamond"),
        "next_diamond": pg.get("next_diamond"),
    }


def fandoms_of(uid: str, token: str) -> list[dict]:
    """任意用户的全部粉丝团 (fandom 域; X-Tt-Token 即可)."""
    out, offset = [], 0
    while True:
        q = urlencode({**DEV, "uid": uid, "offset": offset, "count": 20,
                       "is_login": 1})
        d = _token_get(f"{BASE_FANDOM}/ent/fandom/user/fandoms/?{q}", token)
        if d.get("status_code") != 0:
            sc = d.get("status_code")
            if sc == 8:
                raise RuntimeError("token 失效 (sc=8 游客态), 请重抓凭证")
            raise RuntimeError(f"fandoms status={sc}")
        page = d.get("fandoms") or []
        out.extend(page)
        if not d.get("has_more") or not page or len(out) >= 200:
            break
        offset += len(page)
        time.sleep(0.3)
    return out


def _pick_token() -> str:
    """从 fansclub_tokens 取第一个可用 token (查询身份影响 personal/meta 的
    is_login 视角, 但目标用户数据由 target_user_id 决定)."""
    tokens_dir = DATA_DIR / "fansclub_tokens"
    for p in sorted(tokens_dir.glob("*.json")):
        try:
            t = json.loads(p.read_text(encoding="utf-8")).get("x_tt_token")
            if t:
                return str(t)
        except Exception:
            continue
    raise RuntimeError("data/fansclub_tokens/ 无可用 token")


def query_user(raw_text: str) -> dict:
    """总入口: 解析 → (sec_uid 解析 uid) → pay_grade + fandoms + per-fandom meta.

    返回 {ok, input, user{...}, clubs[{...}], errors[...]} — 单段失败不整体失败.
    """
    parsed = parse_query(raw_text)
    if parsed["kind"] == "invalid":
        return {"ok": False, "input": raw_text, "reason": parsed["reason"],
                "user": None, "clubs": [], "errors": []}

    token = _pick_token()
    errors: list[str] = []

    # 1. uid (sec_uid 需解析; 抖音号需先 sug 联想出 sec_uid)
    sec_seen: str | None = None  # 途中拿到的 sec_uid (库对照用)
    if parsed["kind"] == "sec_uid":
        sec_seen = parsed["value"]
        uid = resolve_uid(parsed["value"], token)
        if not uid:
            return {"ok": False, "input": raw_text,
                    "reason": "sec_uid 解析 uid 失败 (签名/网络)", "user": None,
                    "clubs": [], "errors": []}
    elif parsed["kind"] == "douyin_id":
        sec = resolve_douyin_id(parsed["value"])
        if not sec:
            return {"ok": False, "input": raw_text,
                    "reason": "抖音号未匹配到用户 — 确认号码无误, 或改用「分享链接」粘贴",
                    "user": None, "clubs": [], "errors": []}
        sec_seen = sec
        uid = resolve_uid(sec, token)
        if not uid:
            return {"ok": False, "input": raw_text,
                    "reason": "sec_uid 解析 uid 失败 (签名/网络)", "user": None,
                    "clubs": [], "errors": []}
    else:
        uid = parsed["value"]

    # 2. 消费等级
    user: dict = {"uid": uid}
    if sec_seen:
        user["sec_uid"] = sec_seen
    try:
        pg = pay_grade_of(uid)
        if pg:
            user.update(pg)
    except Exception as exc:
        errors.append(f"消费等级: {str(exc)[:120]}")

    # 3. 粉丝团
    clubs: list[dict] = []
    try:
        entries = fandoms_of(uid, token)
        for e in entries:
            lfc = e.get("live_fans_club_info") or {}
            clubs.append({
                "fandom_id": str(e.get("fandom_id") or ""),
                "title": e.get("title") or (lfc.get("anchor_uid") or ""),
                "anchor_uid": str(lfc.get("anchor_uid") or ""),
                "fans_count": lfc.get("fans_count"),
                "join_user_cnt": e.get("join_user_cnt"),
                "hot_value": e.get("hot_value"),
                "has_signed": e.get("has_signed"),
                "continue_sign_days": e.get("continue_sign_days"),
                "user_join_status": e.get("user_join_status"),
                "level": None,       # 他人团等级服务端不下发 → 前端显示"未公开"
                "joined_days": None,
                "continues_sign_days": None,
                "light_medal_cnt": None,
                "_meta_done": False,
            })
        # 4. 每团 personal/meta (入团天数等; 上限 30 团防刷)
        for c in clubs[:30]:
            q = urlencode({**DEV, "fandom_id": c["fandom_id"],
                           "target_user_id": uid})
            try:
                m = _token_get(f"{BASE_FANDOM}/ent/fandom/personal/meta/?{q}", token)
                if m.get("status_code") == 0:
                    meta = m.get("personal_fandom_meta") or {}
                    # 灯牌等级在响应顶层的 live_fans_club_badge (不在 meta 子对象;
                    # e122 首版漏提导致前端显示"未公开", 2026-11-23 修正)。
                    # badge 对象缺席 = 该团灯牌从未点亮 (sc=0 已入团), 不是未公开。
                    badge = m.get("live_fans_club_badge")
                    c.update({
                        "joined_days": meta.get("joined_days"),
                        "continues_sign_days": meta.get("continues_sign_days"),
                        "light_medal_cnt": meta.get("light_medal_cnt"),
                        "level": (badge or {}).get("level"),
                        "club_name": (badge or {}).get("club_name") or "",
                        "fans_badge_status": (badge or {}).get("fans_badge_status"),
                        "badge_absent": badge is None,
                        "_meta_done": True,
                    })
            except Exception as exc:
                errors.append(f"meta[{c['title'][:12]}]: {str(exc)[:80]}")
            time.sleep(0.3)
    except Exception as exc:
        errors.append(f"粉丝团: {str(exc)[:120]}")

    return {"ok": True, "input": raw_text, "user": user, "clubs": clubs,
            "errors": errors, "queried_at": int(time.time())}
