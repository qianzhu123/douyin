"""粉丝团签到服务 — PC 直连抖音 webcast/fandom 两域 (两头配方 X-Tt-Token + sdk-version:2).

数据源: data/fansclub_tokens/*.json ({label, x_tt_token}) — 凭证由 root 真机
frida 抓取 (见 douyin-re-lab docs/09 §79), 电脑端无法登录获取, 只能重新加载。

端点 (全部只读或免费签到, gift/send 永不触碰):
- 团列表  GET  webcast.amemv.com/webcast/fansclub/user_auto_light_up_list/
- 日历    GET  fandom.starplaylet.com/ent/fandom/user/calendar/
- 签到    POST fandom.starplaylet.com/ent/fandom/join/ (join_type=3 action=1, 免费)
- 排名    GET  webcast.amemv.com/webcast/fansclub/check_in_rank_homepage/?anchor_id=X
"""
from __future__ import annotations

import calendar
import json
import ssl
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .config import DATA_DIR

TOKENS_DIR = DATA_DIR / "fansclub_tokens"

BASE_WEBCAST = "https://webcast.amemv.com"
BASE_FANDOM = "https://fandom.starplaylet.com"

# 固定设备参数 (两头配方公共 query; 与抓 token 的真机一致)
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

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

# 签到是幂等的 (14001=今日已签), 但并发无意义; 全局锁串行化
_sign_lock = threading.Lock()
_sign_state: dict[str, dict] = {}  # label -> {running, done, fail, skip, log[], started_at}


def _today_bj() -> tuple[int, str]:
    """北京时间当日 0 点时间戳 (UTC-8h) 与 YYYYMMDD 字符串。日历键样本验证过。"""
    lt = time.localtime()
    # 北京0点 = UTC0 + 8h (样本 1793203200 = UTC 10-28 16:00 = 北京 10-29 00:00)
    ts = int(calendar.timegm((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, 0))) + 8 * 3600
    return ts, f"{lt.tm_year:04d}{lt.tm_mon:02d}{lt.tm_mday:02d}"


def _req(method: str, base: str, path: str, params: dict, token: str, timeout: float = 15.0) -> dict:
    url = base + path + "?" + urllib.parse.urlencode({**DEV, "ts": int(time.time()), **params})
    headers = {"X-Tt-Token": token, "sdk-version": "2"}
    data = b"{}" if method == "POST" else None
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, headers=headers, data=data, method=method)
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as resp:
        return json.loads(resp.read())


def load_tokens() -> list[dict]:
    """扫描 token 目录, 返回 [{label, token}]. 文件格式 {label, x_tt_token}."""
    out = []
    if not TOKENS_DIR.exists():
        return out
    for p in sorted(TOKENS_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            token = str(data.get("x_tt_token") or "")
            if token:
                out.append({"label": str(data.get("label") or p.stem), "token": token})
        except Exception:
            continue
    return out


def club_list(token: str) -> list[dict]:
    q = {"count": 50, "offset": 0}
    data = _req("GET", BASE_WEBCAST, "/webcast/fansclub/user_auto_light_up_list/", q, token)
    if data.get("status_code") != 0:
        raise RuntimeError(f"club_list status={data.get('status_code')}")
    return data["data"]["user_auto_light_up_list"]


def calendar_map(token: str, anchor_id: str) -> tuple[dict, dict]:
    """返回 (calendar_map, cal_raw顶层精简). biz_extra 是 JSON 字符串需二次 parse."""
    cal = _req("GET", BASE_FANDOM, "/ent/fandom/user/calendar/",
               {"business_id": anchor_id, "business_type": 4}, token)
    if cal.get("status_code") != 0:
        return {}, {}
    be = cal.get("biz_extra")
    cm: dict = {}
    if isinstance(be, str):
        try:
            cm = json.loads(be).get("calendar_map") or {}
        except Exception:
            cm = {}
    elif isinstance(be, dict):
        cm = be.get("calendar_map") or {}
    info = {
        "current_streak": cal.get("current_streak"),
        "makeup_card_count": cal.get("makeup_card_count"),
        "fandom_id": cal.get("fandom_id") or "",
    }
    return cm, info


def rank_self(token: str, anchor_id: str) -> dict:
    rk = _req("GET", BASE_WEBCAST, "/webcast/fansclub/check_in_rank_homepage/",
              {"anchor_id": anchor_id}, token)
    if rk.get("status_code") != 0:
        return {}
    return (rk.get("data") or {}).get("self_item") or {}


def account_overview(label: str, token: str) -> dict:
    """单账户全量粉丝团信息 (等级/连签/今日签/热度/排名/补签卡/因子)."""
    today_ts, today_str = _today_bj()
    clubs = []
    try:
        raw = club_list(token)
    except Exception as exc:
        return {"label": label, "ok": False, "error": str(exc)[:120], "clubs": []}
    for f in raw:
        u = f.get("user_fansclub_info") or {}
        ai = f.get("anchor_info") or {}
        aid = str(u.get("anchor_id") or "")
        row = {
            "anchor_id": aid,
            "nickname": (ai.get("nickname") or "?"),
            "level": u.get("level"),
            "streak": 0,
            "signed": False,
            "heat": 0,
            "rank": None,
            "factor": None,
            "makeup_card": None,
            "is_circle": False,
            "pending": False,
        }
        try:
            cm, info = calendar_map(token, aid)
            row["streak"] = info.get("current_streak") or 0
            row["makeup_card"] = info.get("makeup_card_count")
            row["is_circle"] = bool(info.get("fandom_id"))
            if str(today_ts) in cm:
                row["signed"] = True
            # 日历渲染延迟 (§80): 今日条目未出但 streak>=1 → 可能已签, 标 pending
            if not row["signed"] and (row["streak"] or 0) >= 1:
                row["pending"] = True
            if cm:
                latest = cm[max(cm, key=lambda k: cm[k].get("date_str", ""))]
                row["factor"] = latest.get("score")
        except Exception:
            pass
        try:
            si = rank_self(token, aid)
            row["heat"] = si.get("score") or 0
            row["rank"] = si.get("rank")
        except Exception:
            pass
        clubs.append(row)
        time.sleep(0.25)
    clubs.sort(key=lambda r: (-(r["level"] or 0), -(r["heat"] or 0)))
    return {"label": label, "ok": True, "clubs": clubs,
            "signed_count": sum(1 for c in clubs if c["signed"])}


def _do_sign(label: str, token: str) -> None:
    state = _sign_state[label]
    today_ts, _ = _today_bj()
    ok = fail = skip = 0
    try:
        raw = club_list(token)
    except Exception as exc:
        state.update(running=False, done=True, fail=1,
                     log=[f"[error] 团列表失败: {str(exc)[:120]}"])
        return
    state["log"].append(f"共 {len(raw)} 团")
    for f in raw:
        u = f.get("user_fansclub_info") or {}
        aid = str(u.get("anchor_id") or "")
        name = ((f.get("anchor_info") or {}).get("nickname") or "?")[:14]
        level = u.get("level")
        try:
            cm, _ = calendar_map(token, aid)
            if str(today_ts) in cm:
                skip += 1
                state["log"].append(f"跳过 {name} (已签)")
                continue
        except Exception as exc:
            fail += 1
            state["log"].append(f"[error] {name} 日历失败: {str(exc)[:80]}")
            continue
        try:
            r = _req("POST", BASE_FANDOM, "/ent/fandom/join/",
                     {"business_id": aid, "join_type": 3, "action": 1,
                      "business_type": 3, "fandom_id": aid, "level": 0}, token)
            sc = r.get("status_code")
            if sc == 0:
                ok += 1
                qt = r.get("qiandao_text") or {}
                state["log"].append(f"OK {name} 连签{qt.get('continues_sign_days')}天")
            elif sc == 14001:
                # 日历缓存延迟时服务端幂等兜底, 视作成功
                ok += 1
                state["log"].append(f"OK {name} (已签, 幂等兜底)")
            else:
                fail += 1
                state["log"].append(f"[fail] {name} status={sc} {str(r.get('status_msg'))[:40]}")
        except Exception as exc:
            fail += 1
            state["log"].append(f"[error] {name} {str(exc)[:80]}")
        time.sleep(0.8)
    state.update(running=False, done=True, ok=ok, fail=fail, skip=skip)
    state["log"].append(f"完成: 签 {ok} / 跳过 {skip} / 失败 {fail}")


def start_sign(label: str, token: str) -> dict:
    """后台线程执行全量签到; 重复启动直接拒绝。"""
    with _sign_lock:
        st = _sign_state.get(label)
        if st and st.get("running"):
            return {"started": False, "reason": "already running"}
        _sign_state[label] = {
            "label": label, "running": True, "done": False,
            "ok": 0, "fail": 0, "skip": 0,
            "log": [], "started_at": time.time(),
        }
        t = threading.Thread(target=_do_sign, args=(label, token), daemon=True)
        t.start()
        return {"started": True}


def sign_status() -> list[dict]:
    with _sign_lock:
        return [dict(v) for v in _sign_state.values()]
