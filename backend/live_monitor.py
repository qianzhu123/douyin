"""backend/live_monitor.py — 直播间实时监控 (PC 直连快照轮询, 零浏览器, 不进房).

方案 A (用户 2026-11-12 定论): 监控不得进直播间 — 旧版 POST /webcast/room/enter/
+ leave 会在服务端留反复进出场痕迹, 已废。人数改走 GET /webcast/feed/ 广场列表
(§81 两头配方, X-Tt-Token + sdk-version, 纯 GET 不进房), room_view_stats.display_value。
代价: 人数是展示值 ('1.2万'); 目标不在广场流时该轮缺数 (记 stale 事件)。

目标两种 (用户 2026-11-04: 账户维度优先):
- {sec_uid}  账户维度 — 每轮先直连 profile/other 轻量版 (/aweme/v1/user/,
  阶段一验证通) 解析**当前场次** room_id, 主播重新开播换房间号也跟得住。
  room_id 解析带 10 分钟缓存, 未开播轮空。
- {room_id}  房间维度 — 旧方式, 下播后 room_id 失效。

事件: 开播 / 下播 / 标题变化。人数在房间表实时展示, 不发事件。
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque

from .live_analysis import _http, common_q, _room_brief, _pick_token
from .aweme_read import _req_direct_lab

# 单房间两次 enter 最小间隔 (礼貌; 用户 interval 低于此值会被钳到该值)
# 2026-11-08 用户反馈: 120s 默认太长, 改 30s 默认 / 15s 下限 (账户维度每轮
# 先 profile 解析再 enter, 15s 对单目标=15s/次 profile + 15s/次 enter, 可接受)
MIN_ROOM_INTERVAL_S = 15
DEFAULT_INTERVAL_S = 30
HISTORY_MAX = 360          # 每房间快照环 (默认 120s 间隔 = 12h)
EVENTS_MAX = 200
# sec_uid -> room_id 解析缓存时长 (主播重新开播频率低, 10 分钟够)
_ROOM_RESOLVE_TTL_S = 600


class LiveMonitor:
    """后台线程快照轮询器。单实例 (app.py 持有), start/stop/status 线程安全。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state: dict = {"running": False, "interval": DEFAULT_INTERVAL_S,
                             "rooms": [], "started_at": None, "ended_at": None}
        # room_id -> {"label","last":brief,"history":deque,"last_tick":ts,"was_live":bool,"title":str,"viewers":int}
        self._rooms: dict[str, dict] = {}
        self._events: deque = deque(maxlen=EVENTS_MAX)
        self._label: str = ""

    # ---- 对外 API ----
    def start(self, rooms: list[dict], interval: int = DEFAULT_INTERVAL_S,
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
            interval = max(MIN_ROOM_INTERVAL_S, int(interval or DEFAULT_INTERVAL_S))
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

    def add_target(self, sec_uid: str, label: str = "") -> dict:
        """主播库动态加入 (2026-11-07): 监控运行中也可直接加目标, 立即生效。
        未在跑则自动启动单目标轮询 (用户语义: 加入即监控)。"""
        sec = str(sec_uid or "").strip()
        if not sec:
            return {"added": False, "reason": "sec_uid required"}
        with self._lock:
            if self._state["running"]:
                key = sec
                if key in self._rooms:
                    self._rooms[key]["label"] = label or self._rooms[key]["label"]
                    return {"added": False, "reason": "already monitored"}
                self._rooms[key] = {
                    "label": label or sec[:16], "sec_uid": sec, "room_id": "",
                    "resolved_rid": "", "resolved_at": 0.0, "last": None,
                    "history": deque(maxlen=HISTORY_MAX),
                    "last_tick": 0.0, "was_live": False,
                    "title": "", "viewers": None,
                }
                self._state["rooms"] = [
                    {"sec_uid": st["sec_uid"], "room_id": st["room_id"], "label": st["label"]}
                    for st in self._rooms.values()
                ]
                self._add_event("info", f"{label or sec[:16]} 加入监控")
                return {"added": True, "running": True}
        # 未在跑: 以该目标启动 (走 start 的全套初始化)
        r = self.start([{"sec_uid": sec, "label": label}])
        return {"added": bool(r.get("started")), "reason": r.get("reason", ""),
                "running": bool(r.get("started"))}

    def remove_target(self, sec_uid: str) -> dict:
        """主播库动态移出: 直接从轮询表摘除, 其余目标继续跑。
        摘到空则整个监控自动停 (没有空转的轮询线程)。"""
        sec = str(sec_uid or "").strip()
        with self._lock:
            removed = self._rooms.pop(sec, None)
            if not removed:
                return {"removed": False, "reason": "not monitored"}
            self._state["rooms"] = [
                {"sec_uid": st["sec_uid"], "room_id": st["room_id"], "label": st["label"]}
                for st in self._rooms.values()
            ]
            if not self._rooms and self._state["running"]:
                self._state["running"] = False
                self._state["ended_at"] = time.time()
                self._add_event("info", "全部目标已移出, 监控自动停止")
            else:
                self._add_event("info", f"{removed['label']} 移出监控")
        # 同步 users.json monitor 位 (2026-11-08): 面板移出/下播自动关闭都走这里,
        # 库状态必须跟监控真实状态一致, 否则库显示"监控中"但实际已摘 (用户报的 bug)
        _set_user_monitor(sec, False)
        return {"removed": True, "running": self._state["running"]}

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

    def _remove_locked(self, sec_uid: str, note: str = "") -> None:
        """摘除目标 (调用方必须已持 self._lock)。摘到空则整个监控停。"""
        sec = str(sec_uid or "").strip()
        if not sec:
            return
        removed = self._rooms.pop(sec, None)
        if not removed:
            return
        self._state["rooms"] = [
            {"sec_uid": st["sec_uid"], "room_id": st["room_id"], "label": st["label"]}
            for st in self._rooms.values()
        ]
        if note:
            self._add_event("info", note, sec)
        if not self._rooms and self._state["running"]:
            self._state["running"] = False
            self._state["ended_at"] = time.time()
            self._add_event("info", "全部目标已下播, 监控自动停止")
        # 下播自动关闭也同步 monitor 位 (2026-11-08): 库里不能残留"监控中"
        threading.Thread(target=_set_user_monitor, args=(sec, False), daemon=True).start()

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
            # 未开播: (2026-11-07 用户定论) 下播后该监控目标直接关闭 — 从轮询表
            # 摘除并停掉对它的探测, 不再无限轮空; 开播事件由主播库"加入"重新拉起。
            # was_live=True 说明这轮之前还在播 → 先发下播事件再摘。
            if st["was_live"]:
                self._add_event("offline", f"{st['label']} 已下播 (监控自动结束)", st["sec_uid"][:12])
                st["was_live"] = False
                self._remove_locked(st["sec_uid"], f"{st['label']} 下播自动关闭")
            return False
        if rid != st["resolved_rid"] and st["resolved_rid"]:
            # 换了场次 (重新开播), 换房不重复报开播 (was_live 仍 True 时)
            pass
        st["resolved_rid"] = rid
        st["resolved_at"] = now
        return True

    def _snapshot(self, rid: str, st: dict, tok: str) -> None:
        """快照 (2026-11-12 方案 A): 走 /webcast/feed/ 广场列表读人数, 不进房.

        旧版 POST enter/leave 会在服务端留下反复进出场痕迹 (用户 2026-11-11
        定论: 监控不得进直播间)。feed 是纯 GET 列表接口 (§81 两头配方已验),
        每条 room 带 room_view_stats.display_value。代价:
        - 人数是展示值 ('1.2万'), 非精确值
        - 目标房间不在广场推荐流时取不到 → 记 stale 事件, 下轮再试
        """
        feed_room = None
        for pull in ("1", "2", "3"):
            d = _http("GET", "/webcast/feed/",
                      common_q(count="20", offset="0", pull_type=pull), tok)
            for it in (d.get("data") or []):
                r = it.get("data") or {}
                if str(r.get("id_str") or "") == rid and r.get("status") == 2:
                    feed_room = r
                    break
            if feed_room:
                break
            time.sleep(0.5)
        st["last_tick"] = time.time()
        if feed_room is None:
            # 不在广场流: 可能未开播或无推荐权重。用 profile 的 live_status 区分 —
            # 仍在播 (resolved_rid 有效) 却不在流里 → 只能本轮缺数, 不发下播事件
            self._add_event("stale", f"{st['label']} 未在广场流 (人数本轮缺)", rid)
            return
        brief = _room_brief(feed_room)
        viewers = brief.get("viewers")
        viewers_int = _parse_viewers(viewers)
        snap = {"t": time.time(), "status": brief.get("status"), "viewers_str": viewers,
                "viewers": viewers_int, "like_count": brief.get("like_count"),
                "total_user": brief.get("total_user")}
        st["history"].append(snap)
        st["last"] = brief
        st["viewers"] = viewers_int

        live = brief.get("status") == 2
        # 开播 / 下播 (下播 → 该目标监控自动关闭, 2026-11-07 用户定论)
        if live and not st["was_live"]:
            self._add_event("live", f"{st['label']} 开播: {brief.get('title') or ''}", rid)
        elif not live and st["was_live"]:
            self._add_event("offline", f"{st['label']} 已下播 (监控自动结束)", rid)
            self._remove_locked(st["sec_uid"], f"{st['label']} 下播自动关闭")
        st["was_live"] = live
        # 标题变化
        new_title = brief.get("title") or ""
        if st["title"] and new_title and new_title != st["title"]:
            self._add_event("info", f"{st['label']} 改标题: {new_title[:30]}", rid)
        st["title"] = new_title
        # 观众涨跌事件已撤 (2026-11-05 用户定论: 只留开播/下播/标题, 人数表格里看)
        # 旧 enter/leave 礼貌离房已废 (方案 A: 不进房, 无需离房)
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


def _set_user_monitor(sec_uid: str, monitor: bool) -> None:
    """users.json monitor 位同步 (失败绝不影响监控主循环)。"""
    if not sec_uid:
        return
    try:
        from .config import USERS_FILE
        from .services import set_monitor_flag
        set_monitor_flag(USERS_FILE, sec_uid, monitor)
    except Exception:
        pass
