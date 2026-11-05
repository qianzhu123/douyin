"""backend/live_monitor.py — 直播间实时监控 (PC 直连快照轮询, 零浏览器).

阶段二 (用户 2026-11-04)。复用两头配方 2.0 (X-Tt-Token + sdk-version, 见
live_analysis.py, lab §81 验证): 定时对房间 POST /webcast/room/enter/ 取快照,
礼貌 leave。全程直连, 不开浏览器 — 与旧 watch 轮询 (浏览器探测) 互补。

目标两种 (用户 2026-11-04: 账户维度优先):
- {sec_uid}  账户维度 — 每轮先直连 profile/other 轻量版 (/aweme/v1/user/,
  阶段一验证通) 解析**当前场次** room_id, 主播重新开播换房间号也跟得住。
  room_id 解析带 10 分钟缓存, 未开播轮空。
- {room_id}  房间维度 — 旧方式, 下播后 room_id 失效 (enter 30003)。

事件: 开播 / 下播 / 标题变化 / 观众数变化 (上次→本次, 抖动抑制: 变化<5% 不报)。
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque

from .live_analysis import _http, common_q, _room_brief, _pick_token
from .aweme_read import _req_direct_lab

# 观众数变化超过该比例才报事件 (抑制高频抖动)
VIEWERS_DELTA_RATIO = 0.05
# 单房间两次 enter 最小间隔 (礼貌; 用户 interval 低于此值会被钳到该值)
MIN_ROOM_INTERVAL_S = 60
HISTORY_MAX = 360          # 每房间快照环 (默认 120s 间隔 = 12h)
EVENTS_MAX = 200
# sec_uid -> room_id 解析缓存时长 (主播重新开播频率低, 10 分钟够)
_ROOM_RESOLVE_TTL_S = 600


class LiveMonitor:
    """后台线程快照轮询器。单实例 (app.py 持有), start/stop/status 线程安全。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state: dict = {"running": False, "interval": 120,
                             "rooms": [], "started_at": None, "ended_at": None}
        # room_id -> {"label","last":brief,"history":deque,"last_tick":ts,"was_live":bool,"title":str,"viewers":int}
        self._rooms: dict[str, dict] = {}
        self._events: deque = deque(maxlen=EVENTS_MAX)
        self._label: str = ""

    # ---- 对外 API ----
    def start(self, rooms: list[dict], interval: int = 120,
              duration_minutes: int = 0, label: str = "") -> dict:
        """rooms: [{room_id, label?}] 或 [{sec_uid, label?}] (账户维度优先);
        duration_minutes=0 表示一直跑到 stop。"""
        with self._lock:
            if self._state["running"]:
                return {"started": False, "reason": "already running"}
            clean = []
            for r in rooms or []:
                r = r or {}
                sec = str(r.get("sec_uid") or "").strip()
                rid = str(r.get("room_id") or "").strip()
                if sec:
                    clean.append({"sec_uid": sec, "room_id": "",
                                  "label": str(r.get("label") or sec[:16])})
                elif rid:
                    clean.append({"sec_uid": "", "room_id": rid,
                                  "label": str(r.get("label") or rid)})
            if not clean:
                return {"started": False, "reason": "no valid rooms"}
            interval = max(MIN_ROOM_INTERVAL_S, int(interval or 120))
            self._state = {"running": True, "interval": interval,
                           "rooms": clean, "started_at": time.time(),
                           "ended_at": None, "duration_minutes": duration_minutes,
                           "label": label or "直播监控"}
            # 内部状态以 sec_uid 或 room_id 为主键 (二者必有其一)
            self._rooms = {}
            for r in clean:
                key = r["sec_uid"] or r["room_id"]
                self._rooms[key] = {"label": r["label"], "sec_uid": r["sec_uid"],
                                    "room_id": r["room_id"],
                                    "resolved_rid": r["room_id"],
                                    "resolved_at": time.time() if r["room_id"] else 0.0,
                                    "last": None,
                                    "history": deque(maxlen=HISTORY_MAX),
                                    "last_tick": 0.0, "was_live": False,
                                    "title": "", "viewers": None}
            self._events.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="live-monitor")
            self._thread.start()
            return {"started": True, "interval": interval, "rooms": len(clean)}

    def stop(self) -> dict:
        with self._lock:
            if self._state["running"]:
                self._state["running"] = False
                self._state["ended_at"] = time.time()
                self._add_event("info", "监控已停止")
            return {"running": False}

    def status(self) -> dict:
        with self._lock:
            rooms = []
            for key, st in self._rooms.items():
                rooms.append({
                    "key": key,
                    "sec_uid": st["sec_uid"],
                    "room_id": st["resolved_rid"] or st["room_id"],
                    "label": st["label"],
                    "last": st["last"],
                    "viewers": st["viewers"],
                    "title": st["title"],
                    "was_live": st["was_live"],
                    "last_tick": st["last_tick"] or None,
                    "history": list(st["history"])[-120:],
                })
            return {
                "running": self._state["running"],
                "interval": self._state["interval"],
                "label": self._state.get("label") or "",
                "started_at": self._state["started_at"],
                "ended_at": self._state["ended_at"],
                "rooms": rooms,
                "events": list(self._events)[-80:],
            }

    def candidates(self) -> list[dict]:
        """监控候选三来源 (2026-11-04 改版: 账户维度为主):
        ① 账户列表 (users.json 全部账户, sec_uid 维度 — 推荐主来源, 不受场次失效影响);
        ② feed 在播房 (两头配方 /webcast/feed/, 即时在播的陌生房间);
        ③ profile_cache 历史房间号 (标 stale, 仅参考)。
        """
        out: list[dict] = []
        seen_sec: set[str] = set()
        # ① 账户列表全部账户 (账户维度, 永不 stale)
        try:
            from .config import PROFILE_CACHE_FILE, USERS_FILE
            from .services import read_project_users, read_profile_cache
            cache = read_profile_cache(PROFILE_CACHE_FILE)
            for u in read_project_users(USERS_FILE):
                if u.sec_uid in seen_sec:
                    continue
                seen_sec.add(u.sec_uid)
                p = (cache.get(u.sec_uid) or {}).get("profile") or {}
                out.append({"sec_uid": u.sec_uid, "room_id": "",
                            "label": u.label or p.get("nickname") or u.sec_uid[:10],
                            "viewers": None, "stale": False, "account": True})
        except Exception:
            pass
        # ② feed 在播房 (不在账户列表里的陌生在播主播)
        try:
            tok = _pick_token(None)
            for pull in ("1", "2", "3"):
                d = _http("GET", "/webcast/feed/",
                          common_q(count="20", offset="0", pull_type=pull), tok)
                for it in (d.get("data") or []):
                    r = it.get("data") or {}
                    rid = r.get("id_str")
                    owner = r.get("owner") or {}
                    sec = owner.get("sec_uid") or ""
                    if not rid or r.get("status") != 2 or sec in seen_sec:
                        continue
                    seen_sec.add(sec)
                    out.append({"room_id": str(rid), "sec_uid": sec,
                                "label": owner.get("nickname") or rid,
                                "viewers": (r.get("room_view_stats") or {}).get("display_value"),
                                "stale": False, "account": False})
                time.sleep(0.5)
        except Exception:
            pass
        return out

    # ---- 内部 ----
    def _add_event(self, level: str, message: str, room_id: str = "") -> None:
        self._events.append({"time": time.time(), "level": level,
                             "message": message, "room_id": room_id})

    def _loop(self) -> None:
        while True:
            with self._lock:
                running = self._state["running"]
                interval = self._state["interval"]
                started = self._state["started_at"]
                duration = self._state.get("duration_minutes") or 0
            if not running:
                return
            if duration and time.time() - started > duration * 60:
                with self._lock:
                    self._state["running"] = False
                    self._state["ended_at"] = time.time()
                self._add_event("info", "监控时长已到, 自动结束")
                return
            tick_start = time.time()
            try:
                self._tick()
            except Exception as exc:
                self._add_event("error", f"轮次异常: {str(exc)[:80]}")
            elapsed = time.time() - tick_start
            # 每房间错峰: 房间间隔 = interval / 房数, 总轮时长 ≈ interval
            with self._lock:
                n = max(1, len(self._rooms))
            sleep_s = max(5.0, interval / n - elapsed / n)
            # 分片睡眠以便快速响应 stop
            deadline = time.time() + sleep_s
            while time.time() < deadline:
                with self._lock:
                    if not self._state["running"]:
                        return
                time.sleep(min(2.0, max(0.2, deadline - time.time())))

    def _tick(self) -> None:
        with self._lock:
            items = list(self._rooms.items())
            interval = self._state["interval"]
        if not items:
            return
        slot = max(3.0, interval / max(1, len(items)))
        try:
            tok = _pick_token(None)
        except Exception as exc:
            self._add_event("error", f"无凭证: {str(exc)[:60]}")
            time.sleep(5)
            return
        for key, st in items:
            with self._lock:
                if not self._state["running"]:
                    return
                due = time.time() - st["last_tick"] >= MIN_ROOM_INTERVAL_S
            if not due:
                continue
            # 账户维度: 先解析当前场次 room_id (带 TTL 缓存)
            if st["sec_uid"] and not self._resolve_room(st):
                time.sleep(slot * 0.4)
                continue
            rid = st["resolved_rid"]
            if rid:
                self._snapshot(rid, st, tok)
            time.sleep(slot * 0.4)  # 房间间小间隔, 错峰

    def _resolve_room(self, st: dict) -> bool:
        """账户维度: 直连 profile/other 轻量版解析当前场次 room_id。

        缓存 TTL 内直接用上次结果; 解析失败/未开播返回 False (本轮跳过)。
        """
        now = time.time()
        if st["resolved_rid"] and now - st["resolved_at"] < _ROOM_RESOLVE_TTL_S:
            return True
        st["last_tick"] = now  # 解析也算一次探测, 钳制频率
        try:
            prof = _req_direct_lab("GET", "/aweme/v1/user/",
                                   {"sec_user_id": st["sec_uid"]})
        except Exception as exc:
            self._add_event("error", f"{st['label']} profile 解析失败: "
                            f"{str(exc)[:60]}", st["sec_uid"][:12])
            return False
        u = prof.get("user") or {}
        rid = str(u.get("room_id_str") or u.get("room_id") or "").strip()
        if not rid or rid == "0" or u.get("live_status") != 1:
            # 未开播: 清掉旧解析, 下轮再试 (开播/下播事件由 _snapshot 侧维护)
            if st["was_live"]:
                self._add_event("offline", f"{st['label']} 已下播", st["sec_uid"][:12])
                st["was_live"] = False
            st["resolved_rid"] = ""
            st["resolved_at"] = now
            return False
        if rid != st["resolved_rid"] and st["resolved_rid"]:
            # 换了场次 (重新开播), 换房不重复报开播 (was_live 仍 True 时)
            pass
        st["resolved_rid"] = rid
        st["resolved_at"] = now
        return True

    def _snapshot(self, rid: str, st: dict, tok: str) -> None:
        q = common_q(room_id=rid, anchor_id="0", enter_type=0, mean_type=1,
                     room_tab="sale_hot", enter_from_merge="webcast_feed",
                     live_id=1, request_tag="from", action_type="click")
        ent = _http("POST", "/webcast/room/enter/", q, tok)
        st["last_tick"] = time.time()
        sc = ent.get("status_code")
        if sc != 0:
            if sc == 30003:
                # 房间失效: 账户维度目标下轮自动重解析; 房间维度目标需手动换
                if st.get("sec_uid"):
                    st["resolved_rid"] = ""  # 强制下轮重新解析
                    st["resolved_at"] = 0.0
                    hint = " (场次已失效, 下轮自动重新解析)"
                else:
                    hint = " (room_id 已失效, 下播后每场房间号独立 — 请刷新候选)"
            else:
                hint = ""
            self._add_event("error", f"{st['label']} enter sc={sc} "
                            f"{str(ent.get('status_msg') or '')[:40]}{hint}",
                            st.get("sec_uid") or rid)
            return
        room = ent.get("data") or {}
        brief = _room_brief(room if isinstance(room, dict) else {})
        status = brief.get("status")
        viewers = brief.get("viewers")
        viewers_int = _parse_viewers(viewers)
        snap = {"t": time.time(), "status": status, "viewers_str": viewers,
                "viewers": viewers_int, "like_count": brief.get("like_count"),
                "total_user": brief.get("total_user")}
        st["history"].append(snap)
        st["last"] = brief
        st["viewers"] = viewers_int

        live = status == 2
        # 开播 / 下播
        if live and not st["was_live"]:
            self._add_event("live", f"{st['label']} 开播: {brief.get('title') or ''}", rid)
        elif not live and st["was_live"]:
            self._add_event("offline", f"{st['label']} 已下播", rid)
        st["was_live"] = live
        # 标题变化
        new_title = brief.get("title") or ""
        if st["title"] and new_title and new_title != st["title"]:
            self._add_event("info", f"{st['label']} 改标题: {new_title[:30]}", rid)
        st["title"] = new_title
        # 观众数变化 (>5%)
        prev = None
        for h in reversed(list(st["history"])[:-1]):
            if h.get("viewers") is not None:
                prev = h["viewers"]
                break
        if prev is not None and viewers_int is not None and prev > 0:
            ratio = abs(viewers_int - prev) / prev
            if ratio >= VIEWERS_DELTA_RATIO:
                arrow = "+" if viewers_int > prev else "-"
                self._add_event("info", f"{st['label']} 观众 {prev}→{viewers_int} "
                                f"({arrow}{ratio * 100:.0f}%)", rid)
        # 礼貌离房
        try:
            ql = common_q(room_id=rid, room_id_str=rid, leave_type=0, reason=2)
            _http("POST", "/webcast/room/leave/", ql, tok)
        except Exception:
            pass
        # 回写 profile_cache (2026-11-05 打通断链): 账户表格的直播间/人数列直接吃到监控数据。
        # 只动 live 相关字段, 基础信息 (粉丝/昵称等) 一概不碰 — 与手动检测解耦。
        if st.get("sec_uid"):
            try:
                self._write_back(st["sec_uid"], live, viewers_int, brief)
            except Exception:
                pass  # 回写失败绝不影响监控主循环

    def _write_back(self, sec_uid: str, live: bool, viewers_int: int | None,
                    brief: dict) -> None:
        """监控快照 → profile_cache。仅刷新 live_status/live_viewers/live_room 浅字段;
        账户无 cache 记录时跳过 (不凭空造档案, 基础信息仍以手动检测为准)。"""
        from .config import PROFILE_CACHE_FILE
        from .services import read_profile_cache
        cache = read_profile_cache(PROFILE_CACHE_FILE)
        entry = cache.get(sec_uid)
        if not isinstance(entry, dict):
            return
        profile = entry.get("profile")
        if not isinstance(profile, dict):
            return
        profile = dict(profile)
        profile["live_status"] = 1 if live else 0
        profile["live_viewers"] = viewers_int if live else None
        room = dict(profile.get("live_room") or {})
        room["room_id_str"] = brief.get("room_id") or room.get("room_id_str")
        room["status"] = brief.get("status")
        room["title"] = brief.get("title") or room.get("title")
        if viewers_int is not None:
            room["viewers"] = viewers_int
        profile["live_room"] = room
        entry["profile"] = profile
        entry["last_checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        cache[sec_uid] = entry
        PROFILE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PROFILE_CACHE_FILE.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def _parse_viewers(value) -> int | None:
    """'1.2万' / 12345 → int; 解析失败返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    try:
        if s.endswith("万"):
            return int(float(s[:-1]) * 10000)
        if s.endswith("亿"):
            return int(float(s[:-1]) * 100000000)
        return int(float(s))
    except ValueError:
        return None
