"""backend/liked_store.py — 喜欢/收藏数据的按槽位本地持久化.

用户诉求 (2026-11-14): 切换账户后不想重新拉取 → 数据按槽位落盘,
切回来直接读本地文件 (秒开), 不必再开页面滚动。

形状: data/liked/<slot>_<kind>.json
  {"ts": 拉取时间, "slot": 1, "kind": "like", "items": [...], "complete": bool}

合并规则 (2026-11-14 四轮修正): 按 aweme_id 去重, **本轮扫描序为准** —
抖音喜欢列表是最新在前, 滚动顺序即真实时间倒序, 所以本轮拉到的序列就是
用户在抖音上看到的顺序。合并时新批按原序在前, 旧批里新批没有的项 (更早
拉到的、本轮没滚到的) 按旧序补尾 — 展示顺序与抖音一致。
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
    """本轮扫描序优先合并 (按 aweme_id)。

    新批的相对顺序 = 抖音页面顺序, 原序保留在前; 旧批中本轮没有的项按旧序
    补尾 — 整体仍近似抖音时间倒序, 用户看到的顺序与抖音一致。
    """
    seen: set[str] = set()
    out: list = []
    for it in list(new_items):
        k = it.get("aweme_id") or it.get("url")
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(it)
        if len(out) >= cap:
            return out
    for it in list(old_items):
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
