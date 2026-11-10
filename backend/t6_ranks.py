"""T6 主播榜单只读层 (2026-11-10).

数据源: 逆向 lab 仓的 SQLite 快照库 (D:/code/android/androidre/douyin/data/t6_snapshots.db),
由 lab 侧计划任务 douyin_t6_daily 每天 00:05 采集一轮。本模块**只读**, 不写不采。

字段口径 (用户 2026-11-10 确认):
- active_fans_count = 灯牌点亮人数 (实时存量, 用户已验证 陈泽 121 万)
- "昨日新增粉丝团/灯牌" 无官方直读字段 → 相邻两日收盘值差
"""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

T6_DB = Path(os.environ.get(
    "T6_SNAPSHOT_DB",
    r"D:\code\android\androidre\douyin\data\t6_snapshots.db"))


def _connect() -> sqlite3.Connection | None:
    if not T6_DB.exists():
        return None
    # 只读 URI 打开, 不会碰 lab 仓的文件锁
    return sqlite3.connect(f"file:{T6_DB.as_posix()}?mode=ro", uri=True,
                           timeout=3.0)


def _day(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _hhmm(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def status() -> dict:
    """库可用性 + 最近采集时间 (前端显示数据新鲜度)."""
    db = _connect()
    if db is None:
        return {"available": False, "reason": f"快照库不存在: {T6_DB}"}
    try:
        row = db.execute("SELECT MAX(ts), COUNT(DISTINCT sec_uid) FROM snapshots").fetchone()
        club_row = db.execute("SELECT MAX(ts) FROM club_info").fetchone()
        days = [r[0] for r in db.execute(
            "SELECT DISTINCT date(ts,'unixepoch','localtime') d FROM snapshots ORDER BY d")]
        return {
            "available": True,
            "last_profile_ts": row[0],
            "last_profile_at": _hhmm(row[0]) if row[0] else None,
            "last_club_ts": club_row[0],
            "last_club_at": _hhmm(club_row[0]) if club_row[0] else None,
            "anchors": row[1],
            "snapshot_days": days,
        }
    finally:
        db.close()


def _day_close_follower(db, day: str) -> dict[str, int]:
    """每主播当日最后一条 follower_count."""
    return dict(db.execute(
        "SELECT sec_uid, follower_count FROM snapshots s "
        "WHERE date(ts,'unixepoch','localtime')=? AND ts = "
        "(SELECT MAX(ts) FROM snapshots s2 WHERE s2.sec_uid=s.sec_uid "
        " AND date(s2.ts,'unixepoch','localtime')=?)", (day, day)).fetchall())


def _day_close_club(db, day: str) -> dict[str, tuple[int, int]]:
    """每主播当日最后一条 (total_fans, light_up)."""
    out = {}
    for aid, tf, lu in db.execute(
            "SELECT anchor_id, total_fans, light_up FROM club_info c "
            "WHERE date(ts,'unixepoch','localtime')=? AND ts = "
            "(SELECT MAX(ts) FROM club_info c2 WHERE c2.anchor_id=c.anchor_id "
            " AND date(c2.ts,'unixepoch','localtime')=?)", (day, day)):
        out[aid] = (tf or 0, lu or 0)
    return out


def _nicknames(db) -> dict[str, str]:
    return {r[0]: r[1] for r in db.execute(
        "SELECT sec_uid, MAX(nickname) FROM snapshots GROUP BY sec_uid")}


def yesterday_ranks() -> dict:
    """昨日三榜: 相邻两快照日收盘差值 (完整跨天差 = 爱播查查口径)."""
    db = _connect()
    if db is None:
        return {"available": False}
    try:
        days = [r[0] for r in db.execute(
            "SELECT DISTINCT date(ts,'unixepoch','localtime') d FROM snapshots ORDER BY d")]
        if len(days) < 2:
            return {"available": False, "reason": "快照日不足两天",
                    "days": days}
        prev_day, last_day = days[-2], days[-1]
        fc_prev, fc_last = _day_close_follower(db, prev_day), _day_close_follower(db, last_day)
        cl_prev, cl_last = _day_close_club(db, prev_day), _day_close_club(db, last_day)
        uid2su = {r[0]: r[1] for r in db.execute(
            "SELECT uid, sec_uid FROM snapshots WHERE uid != ''")}
        nick = _nicknames(db)
        fan_board, club_board, light_board = [], [], []
        for su in fc_last:
            if su not in fc_prev:
                continue
            item = {"sec_uid": su, "nickname": nick.get(su, su[:12]),
                    "follower_gain": fc_last[su] - fc_prev[su]}
            aid = uid2su.get(su)
            if aid and aid in cl_prev and aid in cl_last:
                item["club_gain"] = cl_last[aid][0] - cl_prev[aid][0]
                item["light_gain"] = cl_last[aid][1] - cl_prev[aid][1]
            fan_board.append(item)
        club_board = sorted(
            (i for i in fan_board if i.get("club_gain") is not None),
            key=lambda x: -x["club_gain"])
        light_board = sorted(
            (i for i in fan_board if i.get("light_gain") is not None),
            key=lambda x: -x["light_gain"])
        fan_board.sort(key=lambda x: -x["follower_gain"])
        return {"available": True, "prev_day": prev_day, "day": last_day,
                "fan_board": fan_board[:50],
                "club_board": club_board[:50],
                "light_board": light_board[:50]}
    finally:
        db.close()


def live_ranks() -> dict:
    """当日实时榜: 当日已见 max-min 差值 (轮次越多越接近全天) + 存量榜."""
    db = _connect()
    if db is None:
        return {"available": False}
    try:
        today = time.strftime("%Y-%m-%d")
        nick = _nicknames(db)
        uid2su = {r[0]: r[1] for r in db.execute(
            "SELECT uid, sec_uid FROM snapshots WHERE uid != ''")}
        # 当日粉丝增量 (max-min)
        fan_rows = db.execute(
            "SELECT sec_uid, MAX(follower_count), MIN(follower_count) "
            "FROM snapshots WHERE date(ts,'unixepoch','localtime')=? "
            "GROUP BY sec_uid", (today,)).fetchall()
        fan_board = sorted(
            ({"sec_uid": su, "nickname": nick.get(su, su[:12]),
              "follower_gain": (hi or 0) - (lo or 0)} for su, hi, lo in fan_rows),
            key=lambda x: -x["follower_gain"])
        # 当日团/灯牌增量
        club_rows = db.execute(
            "SELECT anchor_id, MAX(total_fans), MIN(total_fans), "
            "MAX(light_up), MIN(light_up) FROM club_info "
            "WHERE date(ts,'unixepoch','localtime')=? GROUP BY anchor_id",
            (today,)).fetchall()
        club_map = {}
        for aid, thi, tlo, lhi, llo in club_rows:
            club_map[aid] = {"club_gain": (thi or 0) - (tlo or 0),
                             "light_gain": (lhi or 0) - (llo or 0)}
        for item in fan_board:
            aid = next((a for a, s in uid2su.items() if s == item["sec_uid"]), None)
            g = club_map.get(aid)
            if g:
                item.update(g)
        # 存量榜 (最新快照)
        latest = {}
        for aid, tf, lu, tn, cname in db.execute(
                "SELECT anchor_id, total_fans, light_up, today_new, club_name "
                "FROM club_info c WHERE ts = (SELECT MAX(ts) FROM club_info c2 "
                "WHERE c2.anchor_id=c.anchor_id)"):
            su = uid2su.get(aid)
            latest[aid] = {"anchor_id": aid, "sec_uid": su,
                           "nickname": nick.get(su, cname or aid),
                           "total_fans": tf or 0, "light_up": lu or 0,
                           "today_new": tn or 0, "club_name": cname}
        stock_board = sorted(latest.values(), key=lambda x: -x["total_fans"])
        light_stock = sorted(latest.values(), key=lambda x: -x["light_up"])
        return {"available": True, "day": today,
                "fan_board": fan_board[:50],
                "club_board": sorted((i for i in fan_board if "club_gain" in i),
                                     key=lambda x: -x["club_gain"])[:50],
                "light_board": sorted((i for i in fan_board if "light_gain" in i),
                                      key=lambda x: -x["light_gain"])[:50],
                "stock_board": stock_board[:50],
                "light_stock_board": light_stock[:50]}
    finally:
        db.close()
