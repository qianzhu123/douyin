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
TOKEN_LOG_FILE = DATA_DIR / "fansclub_token_log.json"

# token 生命周期 (2026-11-12 定): 10-31 抓的 11-12 全失效 → 实测寿命 ~12 天。
# 第 age_warn_days 天起黄色预警, 面板显示年龄; 掉登录/刷新均记 token_log。
TOKEN_AGE_WARN_DAYS = 10


def token_log() -> list[dict]:
    """token 事件日志 [{ts, label, event, detail}] (掉登录/刷新时间线)."""
    try:
        return json.loads(TOKEN_LOG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _log_token_event(label: str, event: str, detail: str = "") -> None:
    events = token_log()
    events.append({"ts": time.time(), "label": label, "event": event,
                   "detail": detail[:120]})
    try:
        TOKEN_LOG_FILE.write_text(
            json.dumps(events[-500:], ensure_ascii=False, indent=1),
            encoding="utf-8")
    except Exception:
        pass


def token_health() -> dict:
    """四账户 token 年龄健康表 (面板预警数据源)."""
    out = []
    for a in load_tokens():
        p = TOKENS_DIR / f"{a['label']}.json"
        captured_at = None
        try:
            captured_at = json.loads(p.read_text(encoding="utf-8")).get("captured_at")
        except Exception:
            pass
        age_days = (time.time() - captured_at) / 86400 if captured_at else None
        out.append({
            "label": a["label"],
            "captured_at": captured_at,
            "age_days": round(age_days, 1) if age_days is not None else None,
            "warn": bool(age_days is None or age_days >= TOKEN_AGE_WARN_DAYS),
        })
    return {"accounts": out, "warn_age_days": TOKEN_AGE_WARN_DAYS,
            "events": token_log()[-50:]}
# 检测结果落盘缓存: 后端定时检测的结果存这里, 页面打开直接读最新份,
# 不再依赖"页面当时开着才能把结果拉回前端缓存" (2026-11-03 断链修复)
OVERVIEW_CACHE_FILE = DATA_DIR / "fansclub_overview_cache.json"

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
    """北京时间当日 0 点的 UTC 时间戳与 YYYYMMDD 字符串。

    日历 days[].date 的口径 (服务端样本验证): 北京 11-01 的条目 date=1793462400
    = UTC 10-31 16:00 = 北京 11-01 00:00。即"北京当日0点的UTC时间戳"。
    旧实现 +8h 算成 1793520000 (UTC 11-01 08:00), 比真条目晚 8h → 永远匹配不上,
    已签被误判未签 (2026-11-01 修复, 本机 UTC+8 时区下 time.gmtime(0点) 本身
    已是"北京日期 16:00 UTC", 无需再加 8h)。
    """
    lt = time.localtime()
    # 本机时钟按北京时间跑: 本机日期的 0 点在 UTC 上就是 北京当日0点 - 8h
    ts = calendar.timegm((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, 0)) - 8 * 3600
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


def deploy_token(label: str, token: str, source: str = "manual") -> dict:
    """写入/更新 token 文件 (重抓流程终点; captured_at 供年龄预警)."""
    if not token or len(token) < 50:
        return {"ok": False, "reason": "token 形状不对"}
    TOKENS_DIR.mkdir(parents=True, exist_ok=True)
    (TOKENS_DIR / f"{label}.json").write_text(
        json.dumps({"label": label, "x_tt_token": token,
                    "captured_at": time.time()}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    _log_token_event(label, "refreshed", source)
    return {"ok": True, "label": label}


def club_list(token: str) -> list[dict]:
    q = {"count": 50, "offset": 0}
    data = _req("GET", BASE_WEBCAST, "/webcast/fansclub/user_auto_light_up_list/", q, token)
    if data.get("status_code") != 0:
        raise RuntimeError(f"club_list status={data.get('status_code')}")
    return data["data"]["user_auto_light_up_list"]


def calendar_map(token: str, anchor_id: str) -> tuple[dict, dict]:
    """返回 (已签日期集合 {date_ts: day}, cal_raw顶层精简).

    真实签到记录在 months[].days[] ({date: 北京0点ts, type:1=已签, time:签到时刻}),
    biz_extra.calendar_map 恒为空 {} (2026-11-01 实测确证, 旧实现查错地方 → 永远判未签).
    """
    cal = _req("GET", BASE_FANDOM, "/ent/fandom/user/calendar/",
               {"business_id": anchor_id, "business_type": 4}, token)
    if cal.get("status_code") != 0:
        return {}, {}
    days: dict = {}
    for mth in cal.get("months") or []:
        for d in mth.get("days") or []:
            ts = d.get("date")
            if ts and d.get("type") == 1:  # type=1 已签
                days[str(ts)] = d
    info = {
        "current_streak": cal.get("current_streak"),
        "makeup_card_count": cal.get("makeup_card_count"),
        "fandom_id": cal.get("fandom_id") or "",
        # 入圈判定 (§82): has_join_circle=False → 日历不适用, 不是"未签"
        "has_join_circle": cal.get("has_join_circle"),
        "allow_sign_days_range": cal.get("allow_sign_days_range"),
    }
    return days, info


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
        err = str(exc)[:120]
        # 掉登录事件记录 (20003 = User doesn't login): 面板横幅数据源
        if "20003" in err:
            _log_token_event(label, "invalid", err)
        return {"label": label, "ok": False, "error": err, "clubs": []}
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
            # 亲密度数据源: user_auto_light_up_list (§08-fansclub 4.3/§6)
            "intimacy": u.get("intimacy") or 0,
            "next_level_intimacy": u.get("next_level_intimacy"),
            "badge_status": u.get("status"),  # 1=点亮 2=熄灭(长时间未观看)
            "today_intimacy": u.get("increased_intimacy_today") or 0,  # 今日增量
            "watch_duration": u.get("watching_duration") or 0,  # 今日观看秒数
            "progress_text": ((u.get("biz_extra") or {}).get("fansclub_progress_text") or ""),
            "join_days": None,
            "has_join_circle": None,  # False=未入圈, 签到日历不适用
        }
        pt = u.get("participate_time") or 0
        if pt:
            row["join_days"] = int((time.time() - pt) / 86400)
        try:
            cm, info = calendar_map(token, aid)
            row["streak"] = info.get("current_streak") or 0
            row["makeup_card"] = info.get("makeup_card_count")
            row["is_circle"] = bool(info.get("fandom_id"))
            row["has_join_circle"] = info.get("has_join_circle")
            if str(today_ts) in cm:
                row["signed"] = True
            # 状态只有已签/未签两种 (用户指定 2026-11-04): 旧 pending 语义
            # (streak>=1 且日历未出今日条目) 实为"昨天签过", 不是待定, 已删
            if cm:
                latest_key = max(cm.keys())  # date ts 字符串, 字典序=时间序
                row["factor"] = cm[latest_key].get("time")  # 签到时刻
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
    clubs.sort(key=lambda r: (-(r["level"] or 0), -(r["intimacy"] or 0)))
    # days 亲密度归档随响应带回 (2026-11-09): 前端检测后整体覆盖 overview,
    # 不带 days 会把前端缓存的历史归档冲掉 → 昨日亲密度列变 '—'
    try:
        cached = load_overview_cache().get(label) or {}
        days = cached.get("days") or {}
    except Exception:
        days = {}
    return {"label": label, "ok": True, "clubs": clubs, "days": days,
            "signed_count": sum(1 for c in clubs if c["signed"])}


def sign_club(label: str, token: str, anchor_id: str) -> dict:
    """单团签到 (每主播行按钮; bt=4 join, 与全量签同形状, 幂等)。"""
    aid = str(anchor_id or "").strip()
    if not aid:
        return {"ok": False, "msg": "anchor_id 为空"}
    try:
        r = _req("POST", BASE_FANDOM, "/ent/fandom/join/",
                 {"business_id": aid, "join_type": 3, "action": 1,
                  "business_type": 4, "fandom_id": aid, "level": 0}, token)
    except Exception as exc:
        return {"ok": False, "msg": str(exc)[:120]}
    sc = r.get("status_code")
    if sc == 0:
        qt = r.get("qiandao_text") or {}
        return {"ok": True, "msg": f"连签{qt.get('continues_sign_days')}天",
                "continues_sign_days": qt.get("continues_sign_days")}
    if sc == 14001:
        return {"ok": True, "msg": "已签 (幂等兜底)"}
    return {"ok": False, "msg": f"status={sc} {str(r.get('status_msg'))[:60]}"}


def check_club(label: str, token: str, anchor_id: str) -> dict:
    """单团检测 (每主播行按钮; 拉该团日历+排名, 返回与 overview 行同构的单行)。"""
    aid = str(anchor_id or "").strip()
    if not aid:
        return {"ok": False, "msg": "anchor_id 为空"}
    today_ts, _ = _today_bj()
    row = {
        "anchor_id": aid, "nickname": None, "level": None, "streak": 0,
        "signed": False, "makeup_card": None, "is_circle": False,
        "intimacy": 0, "next_level_intimacy": None, "badge_status": None,
        "watch_duration": 0, "progress_text": "", "join_days": None,
        "has_join_circle": None, "heat": 0, "rank": None, "factor": None,
    }
    # 灯亮列表取基础字段 (昵称/等级/亲密度/勋章)
    # 注意: 新加的粉丝团可能有同步延迟不在列表里, 此时仍继续检测 (日历/排名只认 anchor_id)
    in_list = False
    try:
        for f in club_list(token):
            u = f.get("user_fansclub_info") or {}
            if str(u.get("anchor_id") or "") == aid:
                ai = f.get("anchor_info") or {}
                row.update({
                    "nickname": ai.get("nickname") or "?",
                    "level": u.get("level"),
                    "intimacy": u.get("intimacy") or 0,
                    "next_level_intimacy": u.get("next_level_intimacy"),
                    "badge_status": u.get("status"),
                    "watch_duration": u.get("watching_duration") or 0,
                    "progress_text": ((u.get("biz_extra") or {}).get("fansclub_progress_text") or ""),
                })
                pt = u.get("participate_time") or 0
                if pt:
                    row["join_days"] = int((time.time() - pt) / 86400)
                in_list = True
                break
    except Exception as exc:
        return {"ok": False, "msg": f"灯亮列表失败: {str(exc)[:100]}"}
    if not in_list:
        row["nickname"] = "(新团, 详情待同步)"
    # 日历
    try:
        cm, info = calendar_map(token, aid)
        row["streak"] = info.get("current_streak") or 0
        row["makeup_card"] = info.get("makeup_card_count")
        row["is_circle"] = bool(info.get("fandom_id"))
        row["has_join_circle"] = info.get("has_join_circle")
        if str(today_ts) in cm:
            row["signed"] = True
        if cm:
            latest_key = max(cm.keys())  # date ts 字符串, 字典序=时间序
            row["factor"] = cm[latest_key].get("time")  # 签到时刻 (上次签到列)
    except Exception:
        pass
    # 排名
    try:
        si = rank_self(token, aid)
        row["heat"] = si.get("score") or 0
        row["rank"] = si.get("rank")
    except Exception:
        pass
    return {"ok": True, "club": row, "checked_at": time.time()}


def _do_sign(label: str, token: str) -> None:
    # 【2026-11-01 恢复】用户澄清: 禁用指"先不要直接对所有账户签", 不是废弃签到。
    # 恢复 join 签到 (保连签/灯牌), 但保留实测结论: PC join 不发亲密度(+5 不兑现),
    # 修复亲密度需真机链路 (e87 lab 进行中)。手动触发单账户签, 不再自动全量签。
    state = _sign_state[label]
    today_ts, _ = _today_bj()
    ok = fail = skip = 0
    signed_anchors: list[str] = []  # 本轮签成功的团 (anchor_id), 前端据此直接置已签
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
        # 只签灯牌点亮的团 (用户指定 2026-11-04): badge status 1=点亮 2=熄灭
        if (u.get("status") or 0) != 1:
            skip += 1
            state["log"].append(f"跳过 {name} (灯牌熄灭)")
            continue
        try:
            cm, info = calendar_map(token, aid)
            # 未入圈团 bt=4 join 也能签上 (2026-11-02 复仇学姐实证, 保连签),
            # 只是灯灭(badge=2)不发亲密度, 不再跳过
            if str(today_ts) in cm:
                skip += 1
                state["log"].append(f"跳过 {name} (已签)")
                continue
        except Exception as exc:
            fail += 1
            state["log"].append(f"[error] {name} 日历失败: {str(exc)[:80]}")
            continue
        try:
            # business_type=4 = 正常签到发亲密度(+5); =3 是入圈类不发分
            # (2026-11-02 e95: coke老师 bt=4 PC join +5 实证, APP 抓包 e90 同形状)
            r = _req("POST", BASE_FANDOM, "/ent/fandom/join/",
                     {"business_id": aid, "join_type": 3, "action": 1,
                      "business_type": 4, "fandom_id": aid, "level": 0}, token)
            sc = r.get("status_code")
            if sc == 0:
                ok += 1
                signed_anchors.append(aid)
                qt = r.get("qiandao_text") or {}
                state["log"].append(f"OK {name} 连签{qt.get('continues_sign_days')}天")
            elif sc == 14001:
                # 日历缓存延迟时服务端幂等兜底, 视作成功
                ok += 1
                signed_anchors.append(aid)
                state["log"].append(f"OK {name} (已签, 幂等兜底)")
            else:
                fail += 1
                state["log"].append(f"[fail] {name} status={sc} {str(r.get('status_msg'))[:40]}")
        except Exception as exc:
            fail += 1
            state["log"].append(f"[error] {name} {str(exc)[:80]}")
        time.sleep(0.8)
    state.update(running=False, done=True, ok=ok, fail=fail, skip=skip,
                 signed_anchors=signed_anchors,
                 finished_at=time.time())
    state["log"].append(f"完成: 签 {ok} / 跳过 {skip} / 失败 {fail}")
    # 2026-11-09 用户澄清: 签到完不要自动检测 (用户否认说过"签到完必须检测",
    # 此前自动检测是误记) — 一键签到只做签到; 状态要刷新由用户自己点检测。


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


# ---- 全账户检查 (后台线程池并发; 每天0点前端触发 + 手动触发共用) ----
_check_state: dict = {"running": False, "done_labels": [], "current": None,
                      "labels": [], "started_at": None, "finished_at": None}
_check_lock = threading.Lock()


def start_check_all() -> dict:
    """后台线程池并发检查全部账户; 进行中重复启动直接拒绝。"""
    with _check_lock:
        if _check_state["running"]:
            return {"started": False, "reason": "already running",
                    "state": {k: v for k, v in _check_state.items()}}
        tokens = load_tokens()
        if not tokens:
            return {"started": False, "reason": "no tokens"}
        _check_state.update(running=True, done_labels=[], current=None,
                            labels=[a["label"] for a in tokens],
                            started_at=time.time(), finished_at=None)
        t = threading.Thread(target=_do_check_all, daemon=True)
        t.start()
        return {"started": True}


def _save_overview_cache(label: str, overview: dict) -> None:
    """检测结果落盘 (断链修复: 定时检测的结果不再只存在于函数返回值)。

    同时写 per-date 亲密度归档 (days.{YYYYMMDD}.{anchor_id} = intimacy):
    昨日亲密度列的数据源 — 凌晨 0 点自动检测到的 intimacy 即"昨天结束时的值"。
    只存数字不存整份 overview (防体积膨胀), 保留最近 7 天。

    2026-11-08 修 bug: 当天归档只落第一次 — 凌晨 0:00 自动检测是全天最早的一次,
    之后的白天检测 (手动/签到后自动/全量) 一律不覆写, 否则 yesterdayKey 第二天
    读到的是昨晚最新累计值而不是凌晨快照 (用户实测: 昨日列显示成总亲密度)。
    """
    try:
        data: dict = {}
        if OVERVIEW_CACHE_FILE.exists():
            data = json.loads(OVERVIEW_CACHE_FILE.read_text(encoding="utf-8"))
        overview = dict(overview)
        overview["checked_at"] = time.time()
        if overview.get("ok"):
            _, today_str = _today_bj()
            days = overview.get("days") if isinstance(overview.get("days"), dict) else {}
            # 旧结构 days 可能整体被覆盖: 从文件里恢复该 label 的历史再合并今天
            prev_days = (data.get(label) or {}).get("days") or {}
            days = {**prev_days, **days}
            if today_str not in days:
                # 只在当天尚无归档时写入 (首次=凌晨自动检测, 即昨日结束值)
                days[today_str] = {str(c.get("anchor_id")): (c.get("intimacy") or 0)
                                   for c in (overview.get("clubs") or [])}
            # 只留最近 7 天 (按日期字符串排序即时间序, YYYYMMDD 字典序=时间序)
            overview["days"] = dict(sorted(days.items())[-7:])
        data[label] = overview
        OVERVIEW_CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass  # 缓存失败不影响检测本身


def load_overview_cache() -> dict:
    """读落盘缓存 (页面打开时取最新检测结果用)。"""
    try:
        if OVERVIEW_CACHE_FILE.exists():
            return json.loads(OVERVIEW_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _do_check_all() -> None:
    from concurrent.futures import ThreadPoolExecutor
    tokens = {a["label"]: a["token"] for a in load_tokens()}

    def one(label: str) -> None:
        try:
            ov = account_overview(label, tokens[label])
            if ov.get("ok"):
                _save_overview_cache(label, ov)  # 结果落盘, 页面打开可读
        except Exception:
            pass  # 失败也计入完成
        with _check_lock:
            _check_state["done_labels"].append(label)

    # 全部账户并发 (各账户独立 token/连接, 无共享状态; 团数 12-33, 并发安全)
    with ThreadPoolExecutor(max_workers=len(tokens) or 1) as ex:
        for label in list(_check_state["labels"]):
            with _check_lock:
                if not _check_state["running"]:
                    return
                _check_state["current"] = label
            ex.submit(one, label)
    with _check_lock:
        _check_state.update(running=False, current=None, finished_at=time.time())


def check_all_status() -> dict:
    with _check_lock:
        return dict(_check_state)


# ---- 每天北京时间 12 点自动全量检测 (后端内置定时器, 不依赖前端开着) ----
# 用户指定固定时刻 (2026-11-02): 每天 12:00 检测一次, 不做高频轮询。
# 实现每分钟醒一次看钟, 到点且当日未跑过就触发; 若凌晨时后端没开机,
# 当天任意时刻启动后端都会补跑一次 (跨日补跑语义)。
_auto_check_day: str = ""  # 已自动检测过的日期 (YYYYMMDD)

AUTO_CHECK_HOUR = 0   # 每天凌晨 00:00 整点自动全量检测 (用户指定; 12点旧版已废弃)
AUTO_CHECK_MIN = 0


def _auto_check_loop() -> None:
    global _auto_check_day
    while True:
        try:
            lt = time.localtime()
            day_str = f"{lt.tm_year:04d}{lt.tm_mon:02d}{lt.tm_mday:02d}"
            due = (lt.tm_hour, lt.tm_min) >= (AUTO_CHECK_HOUR, AUTO_CHECK_MIN)
            with _check_lock:
                running = _check_state["running"]
            # 当天 00:05 及以后且未跑过 → 触发; start_check_all 内部抢 _check_lock,
            # 必须在锁外调用 (Lock 不可重入, 锁内调 = 死锁, 2026-11-02 实测踩坑)
            if due and day_str != _auto_check_day and not running:
                started = start_check_all()
                if started.get("started") or started.get("reason") == "no tokens":
                    _auto_check_day = day_str  # 无凭证也标记, 不再空转重试
        except Exception:
            pass
        time.sleep(60)  # 每分钟看一次钟


def start_auto_check_daemon() -> None:
    t = threading.Thread(target=_auto_check_loop, daemon=True, name="fansclub-auto-check")
    t.start()
