"""
Douyin User Search - Test Script (pure English output, minimal)
================================================================
Enter a keyword -> search Douyin -> print the returned user list.

Usage:
    python test_search.py <keyword>
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

from raw import _collect_raw, DEFAULT_PROFILE  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="Douyin user search (minimal)")
    p.add_argument("keyword", help="search keyword")
    p.add_argument("--headless", action="store_true", help="run headless")
    p.add_argument("--more", action="store_true", help="scroll for more (~80)")
    p.add_argument("--profile", default=DEFAULT_PROFILE, help="login profile dir")
    args = p.parse_args()

    keyword = args.keyword.strip()
    if not keyword:
        print("Error: keyword is empty.")
        return 1

    profile_dir = Path(args.profile).resolve()
    if not profile_dir.exists():
        print(f"Login profile not found at {profile_dir}")
        print("Run login.bat first to scan the QR code. Trying anyway...\n")

    print(f"Searching Douyin users: {keyword}\n")

    try:
        items, diag = _collect_raw(
            keyword,
            headless=args.headless,
            user_data_dir=str(profile_dir) if profile_dir.exists() else None,
            more=args.more,
            timeout_ms=30000,
        )
    except Exception as e:
        print(f"Search failed: {e}")
        return 20

    print(f"discover/search API calls: {diag.get('search_calls')}"
          f" | last status: {diag.get('last_status')}")
    print(f"Users returned: {len(items)}\n")

    if not items:
        print("No users returned. Possible: not logged in / risk-control slider / endpoint changed.")
        return 30

    print(f"{'#':>3}  {'Nickname':<24} {'DouyinID':<22} {'Followers':>12}  {'Live':<6}  sec_uid")
    print("-" * 110)
    for i, item in enumerate(items, 1):
        info = item.get("user_info") or {}
        nickname = (info.get("nickname") or "")[:22]
        unique_id = (info.get("unique_id") or info.get("short_id") or "")[:20]
        followers = info.get("follower_count", 0)
        room_id = info.get("room_id") or 0
        live = "LIVE" if room_id else "-"
        sec_uid = (info.get("sec_uid") or "")[:24]
        print(f"{i:>3}  {nickname:<24} {unique_id:<22} {followers:>12,}  {live:<6}  {sec_uid}")

    out_dir = Path("results")
    out_dir.mkdir(exist_ok=True)
    safe = "".join(c for c in keyword if c.isalnum() or c in "_-") or "kw"
    out_file = out_dir / f"test_{safe}_{int(time.time())}.json"
    out_file.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nRaw JSON saved to: {out_file}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
