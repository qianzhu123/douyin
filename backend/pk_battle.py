"""backend/pk_battle.py — 直播间 PK 战况 / 红包·福袋 看板数据 (e155)。

数据链: lab 仓 e155 裸 ws 捕获器 (tools/e155_pk_ws.py, e113c 配方 + e155
betterproto 解码器) 实时解码 PK/红包推送, JSONL 落
D:/code/android/androidre/douyin/logs/e155/pk_events.jsonl。

本模块只做读取+聚合 (零浏览器零签名, 与 webcast 拦响应链解耦):
- read_pk_events(room_id): PK 战况 (双方精确分值/助力榜/battle 设置)
- read_box_events(room_id): 红包/福袋 (个数/倒计时/类型)
ws 捕获器由 lab 侧按需挂房 (禁高频: ≥5 分钟/轮, 拒绝码即停)。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

LAB_E155_DIR = r"D:\code\android\androidre\douyin\logs\e155"
EVENTS_JSONL = os.path.join(LAB_E155_DIR, "pk_events.jsonl")

PK_METHOD_HINTS = ("LinkMicArmies", "LinkMicBattle", "BattleStatus")


def read_events(room_id: str, limit: int = 500) -> list[dict[str, Any]]:
    """Read the tail of the e155 JSONL, newest last, filtered by room."""
    if not os.path.exists(EVENTS_JSONL):
        return []
    rows: list[dict[str, Any]] = []
    try:
        with open(EVENTS_JSONL, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    for line in lines[-limit * 4:]:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if room_id and row.get("room_id") != room_id:
            continue
        rows.append(row)
    return rows[-limit:]


def read_pk_events(room_id: str) -> dict[str, Any]:
    rows = [r for r in read_events(room_id) if r.get("method", "") in
            ("WebcastLinkMicArmies", "WebcastLinkMicArmiesMethod",
             "WebcastLinkMicBattleMethod", "WebcastLinkMicBattleFinishMethod")]
    return {"ok": True, "room_id": room_id, "events": rows,
            "updated_at": int(time.time())}


def read_box_events(room_id: str) -> dict[str, Any]:
    rows = [r for r in read_events(room_id)
            if "LuckyBox" in r.get("method", "")
            or "RedPacket" in r.get("method", "")]
    return {"ok": True, "room_id": room_id, "events": rows,
            "updated_at": int(time.time())}


def capture_status() -> dict[str, Any]:
    """Is the ws capture alive + which room is it mounted on."""
    stats_files = []
    if os.path.isdir(os.path.join(LAB_E155_DIR, "ws")):
        stats_files = sorted(
            f for f in os.listdir(os.path.join(LAB_E155_DIR, "ws"))
            if f.startswith("method_stats_"))
    mounted = stats_files[-1].replace("method_stats_", "").replace(".json", "") if stats_files else ""
    jsonl_mtime = os.path.getmtime(EVENTS_JSONL) if os.path.exists(EVENTS_JSONL) else 0
    return {"ok": True, "mounted_room": mounted,
            "jsonl_exists": os.path.exists(EVENTS_JSONL),
            "last_event_age_s": int(time.time() - jsonl_mtime) if jsonl_mtime else None}
