"""
Douyin Login Helper (auto-detect login, no manual Enter needed)
================================================================
Launches a visible Chromium. You scan the QR code. The script polls the
page and auto-detects when login has succeeded (avatar / logged-in DOM
appears), saves the persistent profile, then closes automatically.

Usage:
    python login.py
    python login.py --profile ./douyin_profile      (default)
    python login.py --profile ./douyin_profile --timeout 300
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

# Force UTF-8 stdout so emoji / Chinese don't crash on Windows GBK console
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


LOGGED_IN_HINTS = """
() => {
  // Try several signals that douyin shows only when logged in
  // 1) avatar image in the top nav
  const avatar = document.querySelector('img[src*="aweme-avatar"], img[src*="touxiang"], .avatarImg img, [data-e2e="user-info"] img');
  // 2) login button absent / replaced by avatar
  const loginBtn = document.querySelector('[data-e2e="login"], a[href*="login"]');
  // 3) cookies that indicate logged in
  const cookies = document.cookie || '';
  const hasSession = cookies.indexOf('sessionid=') !== -1 && cookies.indexOf('sessionid=;') === -1;
  return {
    hasAvatar: !!avatar,
    loginBtnVisible: !!loginBtn,
    hasSessionCookie: hasSession,
    cookieLen: cookies.length,
  };
}
"""


def main() -> int:
    p = argparse.ArgumentParser(description="Douyin login helper (auto-detect)")
    p.add_argument("--profile", default="./douyin_profile", help="persistent profile dir")
    p.add_argument("--timeout", type=int, default=300, help="max wait seconds for login")
    args = p.parse_args()

    profile_dir = str(Path(args.profile).resolve())
    print(f"\n=== Douyin Login Helper ===")
    print(f"Profile dir : {profile_dir}")
    print(f"Timeout     : {args.timeout}s")
    print()
    print("A browser window will open. Please:")
    print("  1. Click 登录 / log in if a login modal is not shown")
    print("  2. Scan the QR code with the Douyin mobile app")
    print("  3. Wait — the window will close automatically once login is detected")
    print()

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            profile_dir,
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2500)

        # try clicking the login button to open the modal if present
        try:
            login_btn = page.query_selector('[data-e2e="login"], a[href*="login"]:not([href*="login_check"])')
            if login_btn:
                login_btn.click()
                page.wait_for_timeout(1500)
        except Exception:
            pass

        print("Waiting for login... (you can also press Ctrl+C to bail)")
        detected = False
        for i in range(args.timeout):
            try:
                info = page.evaluate(LOGGED_IN_HINTS)
                if info.get("hasSessionCookie") or (info.get("hasAvatar") and not info.get("loginBtnVisible")):
                    detected = True
                    print(f"[{i}s] Login detected: {info}")
                    break
                if i % 10 == 0:
                    print(f"[{i}s] still waiting... {info}")
            except Exception as e:
                if i % 10 == 0:
                    print(f"[{i}s] page check error: {e}")
            page.wait_for_timeout(1000)

        page.wait_for_timeout(2500)  # let cookies fully settle
        ctx.close()

    if detected:
        print("\n✅ Login saved. You can now run: python raw.py <keyword>")
        return 0
    print("\n⚠️  Login not detected within timeout. Profile still saved (maybe partially).")
    print("   You can re-run login.py to retry.")
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
