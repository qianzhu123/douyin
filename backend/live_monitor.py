"""backend/live_monitor.py — 直播间实时监控 (PC 直连快照轮询, 零浏览器).

阶段二 (用户 2026-11-04)。复用两头配方 2.0 (X-Tt-Token + sdk-version, 见
live_analysis.py, lab §81 验证): 定时对房间 POST /webcast/room/enter/ 取快照,
礼貌 leave。全程直连, 不开浏览器 — 与旧 watch 轮询 (浏览器探测) 互补。

事件: 开播 / 下播 / 标题变化 / 观众数变化 (上次→本次, 抖动抑制: 变化<5% 不报)。
"""
from __future__ import annotations

import threading
import time
from collections import deque

from .live_analysis import _http, common_q, _room_brief, _pick_token

# 观众数变化超过该比例才报事件 (抑制高频抖动)
VIEWERS_DELTA_RATIO = 0.05
# 单房间两次 enter 最小间隔 (礼貌; 用户 interval 低于此值会被钳到该值)
MIN_ROOM_INTERVAL_S = 60
HISTORY_MAX = 360          # 每房间快照环 (默认 120s 间隔 = 12h)
EVENTS_MAX = 200


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
        """rooms: [{room_id, label?}]; duration_minutes=0 表示一直跑到 stop。"""
        with self._lock:
            if self._state["running"]:
                return {"started": False, "reason": "already running"}
            clean = []
            for r in rooms or []:
                rid = str((r or {}).get("room_id") or "").strip()
                if rid:
                    clean.append({"room_id": rid,
                                  "label": str((r or {}).get("label") or rid)})
            if not clean:
                return {"started": False, "reason": "no valid rooms"}
            interval = max(MIN_ROOM_INTERVAL_S, int(interval or 120))
            self._state = {"running": True, "interval": interval,
                           "rooms": clean, "started_at": time.time(),
                           "ended_at": None, "duration_minutes": duration_minutes,
                           "label": label or "直播监控"}
            self._rooms = {r["room_id"]: {"label": r["label"], "last": None,
                                          "history": deque(maxlen=HISTORY_MAX),
                                          "last_tick": 0.0, "was_live": False,
                                          "title": "", "viewers": None}
                           for r in clean}
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
            for rid, st in self._rooms.items():
                rooms.append({
                    "room_id": rid, "label": st["label"],
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
        """监控候选双来源 (2026-11-04):
        ① feed 在播房 (两头配方 /webcast/feed/, 实时可播房间) — 主来源;
        ② profile_cache 已知 room_id (历史场次, 下播后 room_id 失效 enter 30003,
           仅标注 stale 供参考)。
        """
        out: dict[str, dict] = {}
        # ① feed 在播房
        try:
            tok = _pick_token(None)
            for pull in ("1", "2", "3"):
                d = _http("GET", "/webcast/feed/",
                          common_q(count="20", offset="0", pull_type=pull), tok)
                for it in (d.get("data") or []):
                    r = it.get("data") or {}
                    rid = r.get("id_str")
                    if not rid or r.get("status") != 2:
                        continue
                    owner = r.get("owner") or {}
                    out[str(rid)] = {
                        "room_id": str(rid),
                        "label": owner.get("nickname") or rid,
                        "sec_uid": owner.get("sec_uid") or "",
                        "viewers": (r.get("room_view_stats") or {}).get("display_value"),
                        "stale": False,
                    }
                time.sleep(0.5)
        except Exception:
            pass
        # ② 缓存房间 (标 stale; feed 已收录的不重复)
        try:
            from .config import PROFILE_CACHE_FILE, USERS_FILE
            from .services import read_project_users, read_profile_cache
            users = {u.sec_uid: u.label for u in read_project_users(USERS_FILE)}
            cache = read_profile_cache(PROFILE_CACHE_FILE)
            for sec_uid, entry in (cache or {}).items():
                p = (entry or {}).get("profile") or {}
                rid = str(p.get("room_id") or "")
                if not rid or rid in out:
                    continue
                out[rid] = {"room_id": rid,
                            "label": users.get(sec_uid) or p.get("nickname") or rid[:10],
                            "sec_uid": sec_uid,
                            "viewers": None, "stale": True}
        except Exception:
            pass
        return list(out.values())

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
        for rid, st in items:
            with self._lock:
                if not self._state["running"]:
                    return
                due = time.time() - st["last_tick"] >= MIN_ROOM_INTERVAL_S
            if not due:
                continue
            self._snapshot(rid, st, tok)
            time.sleep(slot * 0.4)  # 房间间小间隔, 错峰

    def _snapshot(self, rid: str, st: dict, tok: str) -> None:
        q = common_q(room_id=rid, anchor_id="0", enter_type=0, mean_type=1,
                     room_tab="sale_hot", enter_from_merge="webcast_feed",
                     live_id=1, request_tag="from", action_type="click")
        ent = _http("POST", "/webcast/room/enter/", q, tok)
        st["last_tick"] = time.time()
        sc = ent.get("status_code")
        if sc != 0:
            hint = " (room_id 已失效, 下播后每场房间号独立 — 请刷新候选)" if sc == 30003 else ""
            self._add_event("error", f"{st['label']} enter sc={sc} "
                            f"{str(ent.get('status_msg') or '')[:40]}{hint}", rid)
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
