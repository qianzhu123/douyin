"""backend/liked_store.py — 喜欢/收藏数据的按槽位本地持久化.

用户诉求 (2026-11-14): 切换账户后不想重新拉取 → 数据按槽位落盘,
切回来直接读本地文件 (秒开), 不必再开页面滚动。

形状: data/liked/<slot>_<kind>.json
  {"ts": 拉取时间, "slot": 1, "kind": "like", "items": [...], "complete": bool}

合并规则: 按 aweme_id 去重, 新数据在前 (抖音列表是最新在前, 滚动顺序即时间倒序);
两批拼接时旧批里与新版重复的丢弃, 顺序保持 "新批在前, 旧批剩余在后" —
近似时间倒序, 对展示足够。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .config import PROJECT_ROOT

STORE_DIR = PROJECT_ROOT / "data" / "liked"


def _path(slot: int, kind: str) -> Path:
    return STORE_DIR / f"{int(slot)}_{kind}.json"


def load(slot: int, kind: str) -> dict | None:
    """读槽位缓存; 无/坏文件返回 None。"""
    p = _path(slot, kind)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(d, dict) or not isinstance(d.get("items"), list):
            return None
        return d
    except Exception:
        return None


def save(slot: int, kind: str, items: list, complete: bool = True) -> None:
    """覆盖写 (调用方先 load 再合并)。"""
    try:
        STORE_DIR.mkdir(parents=True, exist_ok=True)
        _path(slot, kind).write_text(
            json.dumps({"ts": time.time(), "slot": int(slot), "kind": kind,
                        "items": items, "complete": bool(complete)},
                       ensure_ascii=False),
            encoding="utf-8")
    except Exception:
        pass  # 持久化失败不阻塞在线功能


def merge(old_items: list, new_items: list, cap: int = 5000) -> list:
    """新批在前合并去重 (按 aweme_id)。"""
    seen: set[str] = set()
    out: list = []
    for it in list(new_items) + list(old_items):
        k = it.get("aweme_id") or it.get("url")
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(it)
        if len(out) >= cap:
            break
    return out


def filter_items(items: list, q: str = "", author: str = "") -> list:
    """本地缓存即时筛选: q 匹配标题/作者, author 精确前缀匹配作者。"""
    q = (q or "").strip().lower()
    a = (author or "").strip().lower()
    if not q and not a:
        return list(items)
    out = []
    for it in items:
        title = (it.get("title") or "").lower()
        au = (it.get("author") or "").lower()
        if a and not au.startswith(a):
            continue
        if q and q not in title and q not in au:
            continue
        out.append(it)
    return out
