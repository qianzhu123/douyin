"""
抖音用户搜索 CLI

用法
====
    # 交互式（推荐首次用，会打开浏览器，可能需要扫码登录）
    python cli.py

    # 直接传关键词
    python cli.py hanghang

    # 拉更多用户（滚动加载，最多 36 个）
    python cli.py hanghang --more

    # 无头模式（已登录态持久化后才建议用）
    python cli.py hanghang --headless

    # 持久化登录态（避免每次扫码）
    python cli.py hanghang --profile ./douyin_profile

输出
====
    屏幕打印 + 落地 results/<keyword>_<时间戳>.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# 允许直接运行 cli.py（不作为包导入）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from douyin_search import (  # noqa: E402
    format_user_line,
    search_users,
    search_users_paged,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="抖音用户搜索（基于 Playwright 真实浏览器，自动处理 a_bogus/msToken 签名）"
    )
    parser.add_argument("keyword", nargs="?", help="搜索关键词（不填则交互式输入）")
    parser.add_argument(
        "--more", action="store_true",
        help="滚动加载更多用户（最多 36 个，默认只取第一页约 12 个）",
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="无头模式（首次登录后建议保持登录态再启用）",
    )
    parser.add_argument(
        "--profile", default=None,
        help="浏览器用户数据目录，持久化登录态。例如 ./douyin_profile",
    )
    parser.add_argument(
        "--no-save", action="store_true",
        help="不写入 JSON 文件，只打印到屏幕",
    )
    args = parser.parse_args()

    keyword = args.keyword or input("请输入搜索关键词: ").strip()
    if not keyword:
        print("关键词不能为空")
        return 1

    print(f"\n🔍 搜索抖音用户: {keyword}")
    print("   (浏览器启动中，请稍候；首次未登录会跳到登录页，扫码后重试即可)\n")

    user_data_dir = args.profile
    if user_data_dir:
        # 转为绝对路径
        user_data_dir = str(Path(user_data_dir).resolve())

    try:
        if args.more:
            users = search_users_paged(
                keyword,
                max_count=36,
                headless=args.headless,
                user_data_dir=user_data_dir,
            )
        else:
            users = search_users(
                keyword,
                headless=args.headless,
                user_data_dir=user_data_dir,
            )
    except Exception as e:
        print(f"❌ 搜索失败: {e}")
        return 2

    if not users:
        print("⚠️  没抓到用户。可能原因：未登录 / 接口变更 / 页面被风控拦截。")
        print("    建议：去掉 --headless，首次加 --profile ./douyin_profile 完成扫码登录。")
        return 3

    print(f"✅ 共找到 {len(users)} 个用户:\n")
    print("=" * 70)
    for i, u in enumerate(users, 1):
        print(f"[{i}] {format_user_line(u)}")
        print("-" * 70)

    if not args.no_save:
        out_dir = Path("results")
        out_dir.mkdir(exist_ok=True)
        safe_kw = "".join(c for c in keyword if c.isalnum() or c in "_-") or "kw"
        # 用一个稳定的时间戳（CLI 一次性运行可用 time.time）
        import time
        ts = int(time.time())
        out_file = out_dir / f"{safe_kw}_{ts}.json"
        out_file.write_text(
            json.dumps(users, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n💾 已保存到: {out_file}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
