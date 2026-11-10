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


def _day_open_follower(db, day: str) -> dict[str, int]:
    """每主播当日第一条 follower_count."""
    return dict(db.execute(
        "SELECT sec_uid, follower_count FROM snapshots s "
        "WHERE date(ts,'unixepoch','localtime')=? AND ts = "
        "(SELECT MIN(ts) FROM snapshots s2 WHERE s2.sec_uid=s.sec_uid "
        " AND date(s2.ts,'unixepoch','localtime')=?)", (day, day)).fetchall())


def _day_open_club(db, day: str) -> dict[str, tuple[int, int]]:
    """每主播当日第一条 (total_fans, light_up)."""
    out = {}
    for aid, tf, lu in db.execute(
            "SELECT anchor_id, total_fans, light_up FROM club_info c "
            "WHERE date(ts,'unixepoch','localtime')=? AND ts = "
            "(SELECT MIN(ts) FROM club_info c2 WHERE c2.anchor_id=c.anchor_id "
            " AND date(c2.ts,'unixepoch','localtime')=?)", (day, day)):
        out[aid] = (tf or 0, lu or 0)
    return out


def yesterday_ranks() -> dict:
    """昨日榜: 最近一个**完整**快照日的全天增量 (2026-11-11 修语义).

    此前取最后两个快照日收盘差 → 凌晨时段变成 23:55↔00:05 的 12 分钟
    边界差 ≈ 0 (用户报"全是 0")。修正: 目标日 = 今天之前最近的快照日;
    基准 = 前一快照日收盘 (有则完整跨天差), 否则目标日当日首快照 (近似,
    缺凌晨窗口, window 字段标注口径)。
    """
    db = _connect()
    if db is None:
        return {"available": False}
    try:
        days = [r[0] for r in db.execute(
            "SELECT DISTINCT date(ts,'unixepoch','localtime') d FROM snapshots ORDER BY d")]
        today = time.strftime("%Y-%m-%d")
        complete = [d for d in days if d < today]
        if not complete:
            return {"available": False, "reason": "还没有完整快照日 (今天之后才有)",
                    "days": days}
        target = complete[-1]
        idx = days.index(target)
        prev_day = days[idx - 1] if idx > 0 else None
        fc_target = _day_close_follower(db, target)
        cl_target = _day_close_club(db, target)
        if prev_day:
            fc_base = _day_close_follower(db, prev_day)
            cl_base = _day_close_club(db, prev_day)
            window = f"{prev_day} 收盘 → {target} 收盘"
        else:
            fc_base = _day_open_follower(db, target)
            cl_base = _day_open_club(db, target)
            window = f"{target} 当日首末快照差 (缺凌晨窗口, 近似)"
        su2uid = {r[1]: r[0] for r in db.execute(
            "SELECT uid, sec_uid FROM snapshots WHERE uid != ''")}
        nick = _nicknames(db)
        fan_board, club_board, light_board = [], [], []
        for su in fc_target:
            base = fc_base.get(su)
            if base is None:
                continue
            item = {"sec_uid": su, "nickname": nick.get(su, su[:12]),
                    "follower_gain": fc_target[su] - base}
            aid = su2uid.get(su)
            if aid and aid in cl_base and aid in cl_target:
                item["club_gain"] = cl_target[aid][0] - cl_base[aid][0]
                item["light_gain"] = cl_target[aid][1] - cl_base[aid][1]
            fan_board.append(item)
        club_board = sorted(
            (i for i in fan_board if i.get("club_gain") is not None),
            key=lambda x: -x["club_gain"])
        light_board = sorted(
            (i for i in fan_board if i.get("light_gain") is not None),
            key=lambda x: -x["light_gain"])
        fan_board.sort(key=lambda x: -x["follower_gain"])
        last_ts = db.execute("SELECT MAX(ts) FROM snapshots").fetchone()[0]
        return {"available": True, "prev_day": prev_day, "day": target,
                "window": window,
                "as_of": datetime.fromtimestamp(last_ts).strftime("%H:%M") if last_ts else None,
                "fan_board": fan_board[:50],
                "club_board": club_board[:50],
                "light_board": light_board[:50]}
    finally:
        db.close()


def live_ranks() -> dict:
    """当日实时榜: 较昨日收盘的增量 (2026-11-11 修口径) + 存量榜.

    此前算"今天第一条 vs 最后一条"→ 凌晨只有一条快照时恒 0 (用户报 0)。
    修正: 基准 = 今天之前最后一条快照 (昨日收盘, 通常 23:55 边界轮),
    增量 = 最新值 − 昨日收盘, 00:00 起的涨幅立即显现。掉粉为负,
    禁 max-min (2026-11-10 用户指错)。
    """
    db = _connect()
    if db is None:
        return {"available": False}
    try:
        today = time.strftime("%Y-%m-%d")
        nick = _nicknames(db)
        su2uid = {r[1]: r[0] for r in db.execute(
            "SELECT uid, sec_uid FROM snapshots WHERE uid != ''")}
        # 基准 = 昨日收盘 (今天之前的最后一条); 无则退回今天第一条
        fan_base = dict(db.execute(
            "SELECT sec_uid, follower_count FROM snapshots s "
            "WHERE ts = (SELECT MAX(ts) FROM snapshots s2 "
            " WHERE s2.sec_uid=s.sec_uid AND s2.ts < ?) AND s.sec_uid IN "
            "(SELECT DISTINCT sec_uid FROM snapshots "
            " WHERE date(ts,'unixepoch','localtime')=?)",
            (int(time.mktime(time.strptime(today, "%Y-%m-%d"))), today)).fetchall())
        fan_latest = dict(db.execute(
            "SELECT sec_uid, follower_count FROM snapshots s "
            "WHERE ts = (SELECT MAX(ts) FROM snapshots s2 "
            " WHERE s2.sec_uid=s.sec_uid)", ()).fetchall())
        # club 基准/最新
        club_base, club_latest = {}, {}
        for aid, tf, lu in db.execute(
                "SELECT anchor_id, total_fans, light_up FROM club_info c "
                "WHERE ts = (SELECT MAX(ts) FROM club_info c2 "
                " WHERE c2.anchor_id=c.anchor_id AND c2.ts < ?)",
                (int(time.mktime(time.strptime(today, "%Y-%m-%d"))),)):
            club_base[aid] = (tf or 0, lu or 0)
        for aid, tf, lu in db.execute(
                "SELECT anchor_id, total_fans, light_up FROM club_info c "
                "WHERE ts = (SELECT MAX(ts) FROM club_info c2 "
                " WHERE c2.anchor_id=c.anchor_id)"):
            club_latest[aid] = (tf or 0, lu or 0)
        fan_board = []
        for su, latest_v in fan_latest.items():
            base = fan_base.get(su)
            if base is None:
                continue
            item = {"sec_uid": su, "nickname": nick.get(su, su[:12]),
                    "follower_gain": latest_v - base}
            aid = su2uid.get(su)
            if aid and aid in club_base and aid in club_latest:
                item["club_gain"] = club_latest[aid][0] - club_base[aid][0]
                item["light_gain"] = club_latest[aid][1] - club_base[aid][1]
            fan_board.append(item)
        # 存量榜 (最新快照)
        latest = {}
        for aid, tf, lu, tn, cname in db.execute(
                "SELECT anchor_id, total_fans, light_up, today_new, club_name "
                "FROM club_info c WHERE ts = (SELECT MAX(ts) FROM club_info c2 "
                "WHERE c2.anchor_id=c.anchor_id)"):
            su = su2uid.get(aid)
            latest[aid] = {"anchor_id": aid, "sec_uid": su,
                           "nickname": nick.get(su, cname or aid),
                           "total_fans": tf or 0, "light_up": lu or 0,
                           "today_new": tn or 0, "club_name": cname}
        stock_board = sorted(latest.values(), key=lambda x: -x["total_fans"])
        light_stock = sorted(latest.values(), key=lambda x: -x["light_up"])
        # 数据时点 (面板显示新鲜度)
        last_ts = db.execute("SELECT MAX(ts) FROM club_info").fetchone()[0]
        return {"available": True, "day": today,
                "as_of": datetime.fromtimestamp(last_ts).strftime("%H:%M") if last_ts else None,
                "fan_board": sorted(fan_board, key=lambda x: -x["follower_gain"])[:50],
                "club_board": sorted((i for i in fan_board if "club_gain" in i),
                                     key=lambda x: -x["club_gain"])[:50],
                "light_board": sorted((i for i in fan_board if "light_gain" in i),
                                      key=lambda x: -x["light_gain"])[:50],
                "stock_board": stock_board[:50],
                "light_stock_board": light_stock[:50]}
    finally:
        db.close()
