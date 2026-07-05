"""Douyin watermark-free video/image downloader + comment scraper - CLI entry"""

import argparse
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from downloader import download_douyin


# ── 抖音 URL 匹配模式 ──
_DOUYIN_URL_PATTERNS = [
    r'(https?://v\.douyin\.com/\S+)',
    r'(https?://www\.douyin\.com/(?:video|note)/\S+)',
    r'(https?://www\.douyin\.com/jingxuan\S+)',
    r'(https?://www\.douyin\.com/user/\S+)',              # 用户主页 / 喜欢
]

# modal_id 匹配模式：从 user/self?...&modal_id=xxx 中提取视频ID
_MODAL_ID_PATTERN = re.compile(r'modal_id=(\d+)')

# 线程安全打印锁
_print_lock = threading.Lock()


def _expand_user_urls(urls: list[str]) -> list[str]:
    """将用户主页链接 (含 modal_id) 转换为作品链接。

    例如: https://www.douyin.com/user/self?...&modal_id=7650767047526435683
    →     https://www.douyin.com/video/7650767047526435683

    注意：抖音会自动将 /video/ 重定向到 /note/ 如果实际是笔记类型，
    所以统一用 /video/ 即可，不需要猜测类型。
    """
    expanded = []
    seen = set()
    for url in urls:
        m = _MODAL_ID_PATTERN.search(url)
        if m and '/user/' in url:
            video_id = m.group(1)
            direct_url = f"https://www.douyin.com/video/{video_id}"
            if direct_url not in seen:
                seen.add(direct_url)
                expanded.append(direct_url)
                print(f"[INFO] 从用户主页链接提取作品: {direct_url}")
        else:
            if url not in seen:
                seen.add(url)
                expanded.append(url)
    return expanded


def _extract_urls(raw: str) -> list[str]:
    """从原始粘贴文本中提取所有抖音 URL，并自动转换用户主页链接"""
    raw = raw.strip()
    urls = []
    seen = set()

    for pat in _DOUYIN_URL_PATTERNS:
        for m in re.finditer(pat, raw):
            url = m.group(1)
            url = re.sub(r'[:：。，！!？?]+$', '', url)
            if url not in seen:
                seen.add(url)
                urls.append(url)

    # 将用户主页链接转换为视频直链
    urls = _expand_user_urls(urls)
    return urls


def _is_file_path(text: str) -> bool:
    """判断输入是否为文件路径"""
    text = text.strip()
    if re.search(r'https?://', text):
        return False
    if Path(text).exists():
        return True
    if re.search(r'\.(txt|csv|json|url|list)$', text, re.IGNORECASE):
        return True
    return False


def _read_urls_from_file(file_path: str) -> list[str]:
    """Read URLs from a file, one per line."""
    path = Path(file_path)
    if not path.exists():
        print(f"[ERROR] File not found: {file_path}")
        sys.exit(1)

    urls = []
    seen = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        extracted = _extract_urls(line)
        for u in extracted:
            if u not in seen:
                seen.add(u)
                urls.append(u)
        if not extracted and re.match(r'https?://', line):
            if line not in seen:
                seen.add(line)
                urls.append(line)

    if not urls:
        print(f"[ERROR] No valid URLs found in: {file_path}")
        sys.exit(1)

    return urls


def _download_one(url: str, output_dir: str, mode: int = 3,
                   fetch_comments: bool = False,
                   idx: int = 0, total: int = 0) -> dict:
    """Download a single Douyin URL and return the result. Thread-safe."""
    prefix = f"[{idx}/{total}] " if total > 1 else ""
    try:
        result = download_douyin(url, output_dir, mode=mode,
                                 fetch_comments=fetch_comments)
    except KeyboardInterrupt:
        raise
    except Exception as e:
        with _print_lock:
            print(f"\n{prefix}[X] Download failed: {e}")
        return None

    return result


def _print_result(result: dict, mode: int = 3):
    """Pretty-print a download result, always showing stats."""
    if not result:
        return

    with _print_lock:
        print()
        stats = result.get("stats", {})

        # ── 统一文件列表 ──
        files = []
        if result.get("folder"):
            files.append(f"Folder:  {result['folder']}")
        if result.get("file_path"):
            files.append(f"Video:   {result['file_path']}")
        if result.get("comment_json"):
            files.append(f"JSON:    {result['comment_json']}")
        if result["type"] == "image" and result.get("image_dir"):
            files.insert(0, f"Images:  {result['image_dir']} ({result['ok_count']}/{result['image_count']} ok)")
        if result["type"] == "slide" and result.get("folder"):
            vc = result.get("video_count", 0)
            ic = result.get("image_count", 0)
            files.insert(0, f"Slides:  {result['folder']} ({vc} videos, {ic} images, {result['ok_count']}/{result['slide_count']} ok)")

        # ── 标题和作者 ──
        mode_labels = {1: "Video only", 2: "Stats JSON", 3: "All"}
        print(f"[OK] {mode_labels.get(mode, '?')} download complete!")
        print(f"   Title:  {result['title']}")
        if result.get("author"):
            print(f"   Author: {result['author']}")

        # ── 文件信息 ──
        for f in files:
            print(f"   {f}")

        # ── 视频大小 ──
        if result["type"] == "video" and result.get("file_size"):
            size_mb = result["file_size"] / (1024 * 1024)
            print(f"   Size:   {size_mb:.2f} MB")
        elif result["type"] in ("image", "slide") and result.get("total_size"):
            size_mb = result["total_size"] / (1024 * 1024)
            print(f"   Size:   {size_mb:.2f} MB")

        # ── 互动数据（始终显示） ──
        if stats:
            print(f"   ━━━ 互动数据 ━━━")
            print(f"   👍 Likes:    {stats.get('digg_count', 0)}")
            print(f"   💬 Comments: {stats.get('comment_count', 0)}")
            print(f"   ⭐ Collects: {stats.get('collect_count', 0)}")
            print(f"   🔄 Shares:   {stats.get('share_count', 0)}")

        # ── 评论统计 ──
        n = len(result.get("comments", []))
        if n > 0:
            total_replies = sum(len(c.get("replies", [])) for c in result["comments"])
            print(f"   💭 Comments scraped: {n} main, {total_replies} replies")


def main():
    parser = argparse.ArgumentParser(
        description="Douyin watermark-free downloader + comment scraper",
        epilog=(
            "Examples:\n"
            "  python main.py https://v.douyin.com/xxxxx/\n"
            "  python main.py --input 'share text with URL'\n"
            "  python main.py -f urls.txt -m 3 -o ./my_downloads\n"
            "  python main.py -f urls.txt -w 3    # 3 threads"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "urls",
        nargs="*",
        help="Douyin URL(s) or share text",
    )
    parser.add_argument(
        "--input",
        dest="raw_input",
        default="",
        help="Raw input: auto-detect URL, share text, or file path",
    )
    parser.add_argument(
        "-f", "--file",
        default="",
        help="Read URLs from a text file (one URL per line)",
    )
    parser.add_argument(
        "-m", "--mode",
        type=int,
        default=1,
        choices=[1, 2, 3],
        help="1=Video only (default), 2=Stats JSON only, 3=All",
    )
    parser.add_argument(
        "-o", "--output",
        default="",
        help="Output directory (default: ./downloads/)",
    )
    parser.add_argument(
        "-w", "--workers",
        type=int,
        default=1,
        help="Number of parallel download threads (default: 1, sequential)",
    )
    parser.add_argument(
        "-c", "--comments",
        action="store_true",
        default=False,
        help="Also scrape comments (off by default, can be slow)",
    )

    args = parser.parse_args()

    # ── 收集所有 URL ──
    all_urls: list[str] = []
    seen = set()

    def _add_url(u: str):
        if u not in seen:
            seen.add(u)
            all_urls.append(u)

    if args.raw_input:
        if _is_file_path(args.raw_input):
            file_urls = _read_urls_from_file(args.raw_input)
            file_urls = _expand_user_urls(file_urls)
            print(f"[INFO] Loaded {len(file_urls)} URL(s) from: {args.raw_input}")
            for u in file_urls:
                _add_url(u)
        else:
            extracted = _extract_urls(args.raw_input)
            if extracted:
                print(f"[INFO] Extracted {len(extracted)} URL(s) from share text")
                for u in extracted:
                    _add_url(u)
            else:
                print(f"[ERROR] No Douyin URL found in input")
                sys.exit(1)

    if args.urls:
        for raw in args.urls:
            extracted = _extract_urls(raw)
            if extracted:
                for u in extracted:
                    _add_url(u)
            elif re.match(r'https?://', raw.strip()):
                # 也尝试转换用户主页链接
                expanded = _expand_user_urls([raw.strip()])
                for u in expanded:
                    _add_url(u)
            else:
                print(f"[WARN] Skipped non-URL argument: {raw[:50]}")

    # 文件输入的 URL 也需要转换用户主页链接
    if args.file:
        file_urls = _read_urls_from_file(args.file)
        file_urls = _expand_user_urls(file_urls)
        print(f"[INFO] Loaded {len(file_urls)} URL(s) from: {args.file}")
        for u in file_urls:
            _add_url(u)

    if not all_urls:
        parser.error(
            "No URLs provided.\n"
            "Usage: python main.py <url_or_share_text>\n"
            "       python main.py --input <paste_text_or_file>\n"
            "       python main.py -f <file_with_urls>"
        )

    mode_labels = {1: "Video only", 2: "Stats JSON only", 3: "All (video+stats JSON)"}
    print(f"[INFO] Total: {len(all_urls)} URL(s)")
    print(f"[INFO] Mode:  {mode_labels.get(args.mode, '?')}")
    print(f"[INFO] Comments: {'ON' if args.comments else 'OFF (use -c to enable)'}")
    print(f"[INFO] Workers: {args.workers}")
    print(f"[INFO] Output: {args.output or './downloads/'}")
    print()

    total = len(all_urls)
    ok = 0
    fail = 0
    results_lock = threading.Lock()

    if args.workers <= 1 or total <= 1:
        # ── 顺序下载（单线程） ──
        for i, url in enumerate(all_urls, 1):
            print(f"{'=' * 50}")
            print(f"[{i}/{total}] Processing: {url}")
            print(f"{'=' * 50}")
            result = _download_one(url, args.output, mode=args.mode,
                                   fetch_comments=args.comments)
            if result:
                _print_result(result, mode=args.mode)
                ok += 1
            else:
                fail += 1
    else:
        # ── 多线程并行下载 ──
        max_workers = min(args.workers, total)
        print(f"[INFO] 启动 {max_workers} 个并行下载线程...")
        print()

        def _worker(i, url):
            with _print_lock:
                print(f"{'=' * 50}")
                print(f"[{i}/{total}] Processing: {url}")
                print(f"{'=' * 50}")
            result = _download_one(url, args.output, mode=args.mode,
                                   fetch_comments=args.comments,
                                   idx=i, total=total)
            return (i, url, result)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_worker, i, url): (i, url)
                for i, url in enumerate(all_urls, 1)
            }

            for future in as_completed(futures):
                i, url, result = future.result()
                if result:
                    _print_result(result, mode=args.mode)
                    with results_lock:
                        ok += 1
                else:
                    with results_lock:
                        fail += 1

    print()
    print(f"{'=' * 50}")
    print(f"Done! Success: {ok}, Failed: {fail}, Total: {total}")

    if fail > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
