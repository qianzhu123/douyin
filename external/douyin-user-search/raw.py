"""
Douyin User Search - Raw Mode
=================================
No data cleaning. Returns the FULL raw user_list from the API as-is.

Usage (Python):
    python raw.py <keyword>
    python raw.py <keyword> --profile ./douyin_profile
    python raw.py <keyword> --more          # scroll for more (max 36)
    python raw.py                            # prompt for keyword

Output:
    Prints raw JSON array to stdout AND saves to results/raw_<keyword>_<ts>.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.sync_api import sync_playwright  # noqa: E402

SEARCH_API = "/aweme/v1/web/discover/search/"
DEFAULT_PROFILE = "./douyin_profile"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0"
)


def _collect_raw(keyword: str, *, headless: bool, user_data_dir: str | None,
                more: bool, timeout_ms: int) -> list[dict]:
    """Drive the browser, capture every discover/search response, return ALL raw user_list items concatenated."""
    keyword = keyword.strip()
    if not keyword:
        raise ValueError("keyword is empty")

    raw_items: list[dict] = []
    seen_uids: set[str] = set()
    diag: dict = {"search_calls": 0, "last_status": None, "last_err": None}

    # Collect response objects during the run; read bodies AFTER the page settles
    # to avoid re-entrancy issues with Playwright sync API inside the event callback.
    captured_responses: list = []

    with sync_playwright() as pw:
        if user_data_dir:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir, headless=headless,
                user_agent=UA,
                viewport={"width": 1600, "height": 1000},
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
        else:
            browser = pw.chromium.launch(headless=headless,
                                          args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
            ctx = browser.new_context(user_agent=UA, viewport={"width": 1600, "height": 1000})
            page = ctx.new_page()

        def on_response(response):
            try:
                if SEARCH_API not in response.url:
                    return
                # only the user-search channel
                if "search_channel=aweme_user_web" not in response.url:
                    return
                diag["search_calls"] += 1
                diag["last_status"] = response.status
                captured_responses.append(response)
            except Exception as e:
                diag["last_err"] = f"on_response: {e}"

        page.on("response", on_response)

        url = f"https://www.douyin.com/search/{keyword}?type=user"
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(3000)

        if more:
            max_iters = 8
            for i in range(max_iters):
                last = len(raw_items)
                page.mouse.wheel(0, 8000)
                page.wait_for_timeout(2500)
                if len(raw_items) == last and i > 1:
                    page.mouse.wheel(0, 8000)
                    page.wait_for_timeout(2500)
                    if len(raw_items) == last:
                        break
        else:
            # single page: wait longer and do one scroll to trigger lazy user lists
            page.wait_for_timeout(5000)
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(3000)

        # Now read bodies of all captured responses (page is idle)
        for resp in captured_responses:
            try:
                if resp.status != 200:
                    continue
                body = resp.text()
                data = json.loads(body)
                for item in data.get("user_list") or []:
                    if not isinstance(item, dict):
                        continue
                    info = item.get("user_info") or {}
                    uid = str(info.get("uid") or info.get("sec_uid") or "")
                    if uid and uid in seen_uids:
                        continue
                    if uid:
                        seen_uids.add(uid)
                    raw_items.append(item)  # untouched, as-is
            except Exception as e:
                diag["last_err"] = f"read body: {e}"

        ctx.close()

    return raw_items, diag


def main() -> int:
    p = argparse.ArgumentParser(description="Douyin user search - raw output, no cleaning")
    p.add_argument("keyword", nargs="?", help="search keyword")
    p.add_argument("--headless", action="store_true", help="run headless")
    p.add_argument(
        "--profile", default=DEFAULT_PROFILE,
        help=f"browser user-data dir for persistent login (default: {DEFAULT_PROFILE})",
    )
    p.add_argument(
        "--no-profile", action="store_true",
        help="do not use any persistent profile (fresh browser, no login)",
    )
    p.add_argument("--more", action="store_true", help="scroll for more (max ~36)")
    p.add_argument("--no-save", action="store_true", help="do not save JSON file")
    args = p.parse_args()

    keyword = args.keyword or input("Enter search keyword: ").strip()
    if not keyword:
        print("Error: keyword is empty")
        return 1

    user_data_dir = None if args.no_profile else str(Path(args.profile).resolve())

    print(f"\nSearching Douyin users: {keyword}")
    if user_data_dir:
        print(f"(Using login profile: {user_data_dir})")
        if not Path(user_data_dir).exists():
            print("  ⚠️  Profile dir not found. Run `python login.py` first to log in.")
    else:
        print("(No profile - fresh browser. Login-required endpoints may fail.)")
    print()

    try:
        items, diag = _collect_raw(keyword, headless=args.headless,
                                    user_data_dir=user_data_dir, more=args.more,
                                    timeout_ms=30000)
    except Exception as e:
        print(f"Search failed: {e}")
        return 2

    print(f"\n--- Diagnostics ---")
    print(f"discover/search API calls observed: {diag['search_calls']}")
    if diag.get("last_status") is not None:
        print(f"  last response status: {diag['last_status']}")
    if diag.get("last_err"):
        print(f"  last parse error: {diag['last_err']}")

    print(f"\nGot {len(items)} raw user records.\n")
    out = json.dumps(items, ensure_ascii=False, indent=2)
    print(out)

    if not args.no_save:
        out_dir = Path("results")
        out_dir.mkdir(exist_ok=True)
        safe_kw = "".join(c for c in keyword if c.isalnum() or c in "_-") or "kw"
        ts = int(time.time())
        out_file = out_dir / f"raw_{safe_kw}_{ts}.json"
        out_file.write_text(out, encoding="utf-8")
        print(f"\nSaved to: {out_file}")

    return 0 if items else 3


if __name__ == "__main__":
    raise SystemExit(main())
