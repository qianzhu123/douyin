"""extract_live_room.py — 抖音直播间信息提取（实验性，置于 tests/live_room_analysis）。

复用项目 headless-chromium + response 拦截同套路（参照
external/douyin-monitor/main.py 的 fetch_profile），打开
`live.douyin.com/<web_rid>`，拦截首屏 `webcast/*` 接口并归一化。

依赖：playwright (`pip install playwright && playwright install chromium`)。
运行：
    python extract_live_room.py <web_rid>           # 打印摘要 JSON
    python extract_live_room.py <web_rid> --raw     # 另存原始 enter/body 到 out/

注意：
- 不计算 a_bogus / msToken，直接借用浏览器同源签好的请求。
- 直播间标题/昵称真正编码是 UTF-8；本脚本用 response.json() 直读，避免手动解码。
- 属实验脚本，字段以 README.md 记录为准；生产前请做 None 兜底与版本漂移兼容。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path
from typing import Any

try:
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover
    async_playwright = None  # type: ignore

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")

# 关键接口路径匹配（用于归一化时判定 body 来源）
ENTER = "/webcast/room/web/enter/"
RANKLIST = "/webcast/ranklist/audience/"
WISH = "/webcast/wish/list/"
GIFT = "/webcast/gift/list/"
USER_ME = "/webcast/user/me/"


async def fetch_live_room(web_rid: str, *, headless: bool = True,
                         save_raw_dir: Path | None = None) -> dict[str, Any]:
    """打开直播间，拦截关键 webcast 接口，返回归一化摘要。

    返回结构见 `_summarize`。如 save_raw_dir 非空，原始 JSON body 落盘。
    """
    if async_playwright is None:
        raise RuntimeError("playwright 未安装：pip install playwright && playwright install chromium")

    captured: dict[str, Any] = {}
    raws: dict[str, Any] = {}

    async def on_response(response) -> None:
        url = response.url
        for tag, needle in (("enter", ENTER), ("ranklist", RANKLIST),
                            ("wish", WISH), ("gift", GIFT), ("user_me", USER_ME)):
            if needle in url and tag not in captured:
                try:
                    if tag == "im_fetch":  # 预留：弹幕为 protobuf，不在此处理
                        return
                    body = await response.json()
                    captured[tag] = body
                    if save_raw_dir is not None:
                        raws[tag] = body
                except Exception:
                    pass

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = await browser.new_context(user_agent=UA, viewport={"width": 1600, "height": 1000})
        page = await context.new_page()
        page.on("response", lambda r: asyncio.create_task(on_response(r)))
        try:
            # 从 localStorage 拿 room_id_str 作为兜底来源之一
            await page.goto(f"https://live.douyin.com/{web_rid}",
                            wait_until="domcontentloaded", timeout=30000)
            # 等关键 enter 接口到位（最多 ~12s）
            for _ in range(40):
                if "enter" in captured:
                    break
                await asyncio.sleep(0.3)
            # 再给一点时间让榜单/心愿单等加载
            await asyncio.sleep(2.0)
            # DOM 侧兜底信息
            dom = await _read_dom(page)
        finally:
            await context.close()
            await browser.close()

    summary = _summarize(captured, web_rid, dom)
    if save_raw_dir is not None:
        save_raw_dir.mkdir(parents=True, exist_ok=True)
        (save_raw_dir / "raw_enter.json").write_text(
            json.dumps(raws.get("enter"), ensure_ascii=False, indent=2), encoding="utf-8")
        for tag in ("ranklist", "wish", "gift", "user_me"):
            if tag in raws:
                (save_raw_dir / f"raw_{tag}.json").write_text(
                    json.dumps(raws[tag], ensure_ascii=False, indent=2), encoding="utf-8")
        (save_raw_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


async def _read_dom(page) -> dict[str, Any]:
    return await page.evaluate(
        """() => ({
            title: document.title,
            metaDescription: (document.querySelector('meta[name="description"]')||{}).content || '',
            nickname: (document.querySelector('[data-e2e="live-room-nickname"]')||{}).textContent || '',
            infoBar: (document.querySelector('[data-e2e="rooom-info-bar-anchor"]')||{}).textContent || '',
            playRoom: localStorage.getItem('playRoom') || '',
            webcastUid: localStorage.getItem('__live_triple_screen_icon_key_new__') || '',
        })"""
    )


def _summarize(captured: dict[str, Any], web_rid: str, dom: dict[str, Any]) -> dict[str, Any]:
    """把候选接口 body 归一成统一结构；缺省回 DOM/localStorage 兜底。"""
    enter = captured.get("enter") or {}
    room = (((enter.get("data") or {}).get("data") or [{}])[0]) if enter else {}
    user = (enter.get("data") or {}).get("user") or {}
    owner = room.get("owner") or {}
    stream = (enter.get("data") or {}).get("web_stream_url") or {}
    rank = captured.get("ranklist") or {}
    rank_data = rank.get("data") or {}

    out: dict[str, Any] = {
        "web_rid": web_rid,
        "room_id_str": room.get("id_str") or (enter.get("data") or {}).get("enter_room_id") or dom.get("playRoom", "").split(",")[0],
        "status": room.get("status"),
        "title": room.get("title") or dom.get("metaDescription"),
        "user_count_str": room.get("user_count_str") or (room.get("stats") or {}).get("user_count_str"),
        "total_user_str": (room.get("stats") or {}).get("total_user_str"),
        "room_view_stats": room.get("room_view_stats"),
        "like_count": room.get("like_count"),
        "anchor": {
            "uid": user.get("id_str") or owner.get("id_str"),
            "sec_uid": user.get("sec_uid") or owner.get("sec_uid"),
            "nickname": user.get("nickname") or owner.get("nickname") or dom.get("nickname"),
            "avatar": ((owner.get("avatar_thumb") or {}).get("url_list") or [None])[0],
        },
        "partition": ((enter.get("data") or {}).get("partition_road_map") or {}).get("partition"),
        "has_commerce_goods": room.get("has_commerce_goods"),
        "ecom": _ecom(room),
        "stream_url": {
            "hls_pull_url": stream.get("hls_pull_url"),
            "flv_pull_url": stream.get("flv_pull_url"),
            "default_resolution": stream.get("default_resolution"),
        },
        "similar_rooms": _similar(enter.get("data") or {}),
    }

    if rank_data:
        out["audience_rank_top"] = [
            {
                "rank": r.get("rank"),
                "nickname": ((r.get("user") or {}).get("nickname")),
                "sec_uid": ((r.get("user") or {}).get("sec_uid")),
                "display_id": ((r.get("user") or {}).get("display_id")),
                "gender": ((r.get("user") or {}).get("gender")),
                "pay_grade_level": ((r.get("user") or {}).get("pay_grade") or {}).get("level"),
                "fans_club_level": (((r.get("user") or {}).get("fans_club") or {}).get("data") or {}).get("level"),
            }
            for r in (rank_data.get("ranks") or [])[:20]
        ]
        out["audience_rank_meta"] = {
            "total": rank_data.get("total"),
            "user_count_desc": rank_data.get("user_count_desc"),
            "has_more": rank_data.get("has_more"),
        }

    wish = (captured.get("wish") or {}).get("data") or {}
    if wish:
        out["wish"] = {
            "anchor_name": wish.get("anchor_name"),
            "common_wish_info": wish.get("common_wish_info"),
            "wish_switch": wish.get("wish_switch"),
        }

    out["dom_fallback"] = dom
    out["source_capture_keys"] = sorted(captured.keys())
    return out


def _ecom(room: dict[str, Any]) -> dict[str, Any] | None:
    ec = room.get("ecom_data") or {}
    cart = room.get("room_cart") or {}
    if not ec and not cart:
        return None
    return {
        "has_ecom": bool(room.get("has_commerce_goods")),
        "cart_total": cart.get("total"),
        "cart_flash_total": cart.get("flash_total"),
        "show_cart": cart.get("show_cart"),
        "ecom_keys": list(ec.keys()),
    }


def _similar(enter_data: dict[str, Any]) -> list[dict[str, Any]]:
    sims = enter_data.get("similar_rooms") or []
    out = []
    for s in sims[:12]:
        r = s.get("room") or {}
        out.append({
            "web_rid": s.get("web_rid"),
            "tag_name": s.get("tag_name"),
            "title": r.get("title"),
            "user_count_str": r.get("user_count_str"),
        })
    return out


def _main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("web_rid", nargs="?", default="31126587860",
                    help="直播间分享号，如 31126587860")
    ap.add_argument("--no-headless", action="store_true", help="显示浏览器窗口")
    ap.add_argument("--raw", action="store_true", help="把原始接口 body 落盘到 ./out")
    args = ap.parse_args()

    out_dir = Path(__file__).parent / "out" if args.raw else None
    summary = asyncio.run(fetch_live_room(args.web_rid, headless=not args.no_headless, save_raw_dir=out_dir))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()
