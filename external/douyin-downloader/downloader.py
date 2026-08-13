"""抖音无水印视频/图集下载器 + 互动数据与评论抓取 -- 核心下载逻辑"""

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright


def extract_aweme_id_from_url(url: str) -> str:
    ids = extract_aweme_ids_from_url(url)
    return ids[0] if ids else ""


def extract_aweme_ids_from_url(url: str) -> list[str]:
    """从抖音 URL 中提取 aweme_id（视频ID）。

    覆盖三类聚合页 URL（共享同一个 modal_id）以及标准详情页：
    - 发现页:   /jingxuan?modal_id=<id>
    - 搜索页:   /jingxuan/search/xxx?...&modal_id=<id>
    - 喜欢列表: /user/self?...&modal_id=<id>
    - 详情页:   /video/<id> 或 /note/<id>
    - 短链/其它: ?aweme_id=<id> 或路径中第一个纯数字段
    """
    if not url:
        return []
    ids: list[str] = []

    def add(value) -> None:
        text = str(value or "").strip()
        if text.isdigit() and text not in ids:
            ids.append(text)

    m = re.search(r"/(?:video|note)/(\d+)", url)
    if m:
        add(m.group(1))
        return ids
    qpos = url.find("?")
    if qpos != -1:
        for pair in url[qpos + 1:].split("&"):
            if "=" not in pair:
                continue
            key, _, value = pair.partition("=")
            key = key.strip()
            value = value.split("#", 1)[0].strip()
            if key in ("modal_id", "aweme_id", "awemeId", "video_id", "vid"):
                add(value)
    for seg in re.split(r"[/?#]", url):
        add(seg)
    return ids


def _detail_url_candidates(url: str) -> list[str]:
    return [
        candidate
        for aweme_id in extract_aweme_ids_from_url(url)
        for candidate in (
            f"https://www.douyin.com/video/{aweme_id}",
            f"https://www.douyin.com/note/{aweme_id}",
        )
    ]

# 中国时区 UTC+8
_CST = timezone(timedelta(hours=8))


def _print(msg: str):
    """安全输出，解决 Windows 控制台编码问题"""
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("utf-8", errors="replace").decode("utf-8", errors="replace"), flush=True)


def _sanitize_filename(title: str) -> str:
    """清理文件名中的非法字符"""
    title = re.sub(r'[<>:"/\\|?*]', '', title)
    title = title.strip().strip('.')
    if len(title) > 100:
        title = title[:100]
    if not title:
        title = f"douyin_{int(time.time())}"
    return title


def _format_timestamp(ts: int) -> str:
    """将 Unix 时间戳转为 YYYY-MM-DD HH:MM:SS 格式（中国时区）"""
    try:
        return datetime.fromtimestamp(ts, tz=_CST).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError):
        return str(ts)


# ════════════════════════════════════════════════════════════════
#  页面信息提取
# ════════════════════════════════════════════════════════════════

def _extract_content_type(page) -> str:
    """判断当前页面内容类型。返回: video | image | slide | unknown

    抖音内容类型:
    - video: 单个视频 (mediaType=0, /video/)
    - image: 纯图集 (mediaType=1, /note/, 无视频slide)
    - slide: 笔记/Slides (mediaType=42, isSlides=true, 混合视频+图片)
    """
    result = page.evaluate("""() => {
        const url = window.location.href;
        if (/\\/video\\//.test(url)) return { type: 'video', reason: 'URL=/video/' };

        // /note/ 页面 — 可能是纯图集，也可能是 slides（混合视频+图片）
        if (/\\/note\\//.test(url)) {
            const vd = window.SSR_RENDER_DATA?.app?.videoDetail;
            if (vd) {
                if (vd.isSlides || vd.mediaType === 42 || vd.awemeType === 68) {
                    return { type: 'slide', reason: '/note/ + SSR: isSlides/mediaType=42' };
                }
                if (vd.mediaType === 1 && vd.images?.length > 0) {
                    return { type: 'image', reason: '/note/ + SSR: mediaType=1' };
                }
            }
            // SSR 无 videoDetail 时，通过 DOM 特征判断
            const hasVideo = !!document.querySelector('video');
            const pageMatch = document.body.innerText.match(/\\b(\\d+)\\s*\\/\\s*(\\d+)\\b/);
            if (hasVideo && pageMatch && parseInt(pageMatch[2]) > 1) {
                return { type: 'slide', reason: '/note/ + DOM: has video + multi-page' };
            }
            return { type: 'image', reason: '/note/ (no SSR data, no video in DOM)' };
        }

        const app = window.SSR_RENDER_DATA?.app;
        const vd = app?.videoDetail;
        if (vd) {
            // 笔记/Slides 类型: 混合视频+图片
            if (vd.isSlides || vd.mediaType === 42 || vd.awemeType === 68) {
                return { type: 'slide', reason: 'SSR: isSlides/mediaType=42' };
            }
            // 纯图集: mediaType=1 且有 images
            if (vd.mediaType === 1 && vd.images?.length > 0) {
                return { type: 'image', reason: 'SSR: mediaType=1 + images' };
            }
            // 纯视频: 有 video 字段
            if (vd.video) {
                return { type: 'video', reason: 'SSR: video field exists' };
            }
            // 只有 images 没有 video → 纯图集
            if (vd.images?.length > 0) {
                return { type: 'image', reason: 'SSR: images only, no video' };
            }
        }

        const videoEl = document.querySelector('video');
        if (videoEl) {
            const src = videoEl.currentSrc || videoEl.src || '';
            if (src.includes('.mp3') || src.includes('ies-music')) return { type: 'image', reason: 'DOM: video src is audio' };
            if (src.includes('douyinvod') || src.includes('.mp4')
                || src.includes('/aweme/v1/play/')) return { type: 'video', reason: 'DOM: video src is play url/mp4/douyinvod' };
        }
        if (videoEl && videoEl.poster) return { type: 'video', reason: 'DOM: video element with poster' };

        return { type: 'unknown', reason: 'no match' };
    }""")

    content_type = result.get("type", "unknown") if isinstance(result, dict) else result
    reason = result.get("reason", "") if isinstance(result, dict) else ""
    _print(f"[DEBUG] Content type: {content_type} (reason: {reason})")
    return content_type


def _extract_slides_info(page) -> dict:
    """提取笔记/Slides 的所有 slide 信息（每个slide可以是视频或图片）

    优先从 SSR_RENDER_DATA 提取；如果没有 SSR，则用 API 提取；最后回退到 DOM。
    """
    # ── 先尝试 SSR ──
    ssr_info = page.evaluate("""() => {
        const vd = window.SSR_RENDER_DATA?.app?.videoDetail;
        if (!vd || !vd.images) return null;

        const slides = vd.images.map((item, i) => {
            const v = item.video;
            const clipType = item.clipType || 0;  // 2=图片, 4=视频, 5=影集视频帧(带 mp4)
            // 判定是否为视频页：以"有 item.video 且可下载直链"为准，不能只认 clipType===4。
            // 抖音影集(mediaType=42/isSlides)每页常有 clipType=5 且带 mp4，按图片处理会全部漏下载。
            const _playApi = v?.playApi || '';
            const _bitRateHas = (v?.bitRateList || []).some(b => !b.playApi?.includes('/dash/'));
            const hasRealVideo = !!(v && (_playApi || _bitRateHas));
            const isVideo = hasRealVideo;

            // 视频地址: 优先 bitRateList 中非 dash 的最高画质，再 playApi
            let video_url = '';
            if (isVideo && v) {
                const playApi = v.playApi || '';
                const bitRates = v.bitRateList || [];
                const nonDash = bitRates.filter(b => !b.playApi?.includes('/dash/'));
                nonDash.sort((a, b) => (b.qualityType || 0) - (a.qualityType || 0));
                if (nonDash.length > 0) {
                    video_url = nonDash[0].playApi || nonDash[0].playAddr?.[0]?.url || playApi;
                } else if (playApi) {
                    video_url = playApi;
                }
            }

            // 图片地址: 仅在非视频页时取；纯图集(clipType=2)兼容老逻辑
            const imageUrls = (!isVideo ? (item.urlList || []) : []);
            const bestImage = imageUrls.length ? (imageUrls.find(u => u.includes('.jpeg')) || imageUrls[0] || '') : '';

            return {
                index: i,
                clip_type: clipType,
                media_type: isVideo ? 'video' : 'image',
                video_url: video_url,
                image_urls: imageUrls,
                best_image_url: bestImage,
                width: item.width || 0,
                height: item.height || 0,
                duration: isVideo ? (v.duration || 0) : 0,
            };
        });

        const s = vd.stats || {};
        return {
            type: 'slide',
            title: vd.desc || vd.itemTitle || '',
            author: vd.authorInfo?.nickname || '',
            aweme_id: vd.awemeId || '',
            stats: {
                digg_count: s.diggCount || 0,
                comment_count: s.commentCount || 0,
                collect_count: s.collectCount || 0,
                share_count: s.shareCount || 0,
            },
            slides: slides,
        };
    }""")

    if ssr_info and ssr_info.get("slides"):
        _print("[+] 从 SSR 提取 slide 信息成功")
        return ssr_info

    # ── SSR 不可用，尝试从 URL 中提取 aweme_id 并用 API 获取 ──
    aweme_id = page.evaluate("""() => {
        const url = window.location.href;
        const m = url.match(/\\/note\\/(\\d+)/) || url.match(/\\/video\\/(\\d+)/);
        return m ? m[1] : '';
    }""")

    if aweme_id:
        _print(f"[*] SSR 无数据，尝试通过 API 获取 slide 信息 (aweme_id={aweme_id})...")
        api_info = _extract_slides_from_api(page, aweme_id)
        if api_info and api_info.get("slides"):
            return api_info

    # ── 最后回退到 DOM 逐页浏览 ──
    _print("[*] SSR 和 API 均不可用，尝试从 DOM 逐页浏览提取...")
    return _extract_slides_from_dom(page)


def _extract_slides_from_api(page, aweme_id: str) -> dict:
    """通过抖音 aweme detail API 提取 slide 信息。

    在浏览器内直接 fetch，利用现有的 cookies/credentials。
    """
    return page.evaluate("""async ({aweme_id, timeout_ms}) => {
        const url = '/aweme/v1/web/aweme/detail/?aweme_id=' + aweme_id
            + '&aid=6383&channel=channel_pc_web&device_platform=webapp';
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), timeout_ms);
        try {
            const resp = await fetch(url, { credentials: 'include', signal: controller.signal });
            clearTimeout(timer);
            const data = await resp.json();
            if (data.status_code !== 0) return null;

            const aweme = data.aweme_detail;
            if (!aweme || !aweme.images) return null;

            const slides = aweme.images.map((img, i) => {
                const v = img.video;
                const clipType = img.clip_type || 0;  // 2=图片, 4=视频, 5=影集视频帧(带 mp4)
                // 判定是否为视频页：以"有 img.video 且 play_addr/bit_rate 里有可下载直链"为准。
                // 不能只认 clipType===4 —— 抖音影集(Slides)的每页 clipType 常为 5，
                // 每页是带 mp4 的短视频，若按图片处理会全部漏下载(mediaType=42/isSlides=true)。
                const _videoUrl0 = (v && v.play_addr && (v.play_addr.url_list || [])[0]) || '';
                const _bitRateUrl0 = v && (v.bit_rate || []).some(b =>
                    !((b.play_addr && (b.play_addr.url_list || [])[0] || '').includes('/dash/')));
                const hasRealVideo = !!(v && (_videoUrl0 || _bitRateUrl0));
                const isVideo = hasRealVideo;

                // 视频地址: 优先 bit_rate 中非 dash 的最高画质，再 play_addr
                let video_url = '';
                if (isVideo && v) {
                    const bitRates = v.bit_rate || [];
                    const nonDash = bitRates.filter(b =>
                        !(b.play_addr?.url_list?.[0] || '').includes('/dash/'));
                    nonDash.sort((a, b) => (b.quality_type || 0) - (a.quality_type || 0));
                    if (nonDash.length > 0) {
                        video_url = nonDash[0].play_addr?.url_list?.[0] || '';
                    }
                    if (!video_url) {
                        video_url = (v.play_addr?.url_list || [])[0] || '';
                    }
                }

                // 图片地址: 仅在该页确实不是视频时才取(影集视频页无需图片地址);
                // 兼容老逻辑: 纯图集(clipType=2)时取 url_list
                const imageUrls = (!isVideo ? (img.url_list || []) : []);
                const bestImage = imageUrls.length ? (imageUrls.find(u => u.includes('.jpeg')) || imageUrls[0] || '') : '';

                return {
                    index: i,
                    clip_type: clipType,
                    media_type: isVideo ? 'video' : 'image',
                    video_url: video_url,
                    image_urls: imageUrls,
                    best_image_url: bestImage,
                    width: img.width || 0,
                    height: img.height || 0,
                    duration: isVideo ? (v.duration || 0) : 0,
                };
            });

            const s = aweme.statistics || {};
            return {
                type: 'slide',
                title: aweme.desc || '',
                author: aweme.author?.nickname || '',
                aweme_id: aweme.aweme_id || aweme_id,
                stats: {
                    digg_count: s.digg_count || 0,
                    comment_count: s.comment_count || 0,
                    collect_count: s.collect_count || 0,
                    share_count: s.share_count || 0,
                },
                slides: slides,
            };
        } catch (e) {
            clearTimeout(timer);
            return null;
        }
    }""", {"aweme_id": aweme_id, "timeout_ms": 10000})


def _extract_video_from_api(page, aweme_id: str) -> dict:
    """通过抖音 aweme detail API 提取单视频直链。

    某些 /video/ 作品在 SSR 不注入可下载直链(未登录/特定作品/直播回放等)，
    videoDetail 为空、<video> 是 blob/MSE。此时回退到该接口：它会返回
    aweme.video.bit_rate / play_addr 里的 douyinvod mp4 直链。
    在浏览器内 fetch，复用当前 cookies/credentials（detail 接口无需 a_bogus
    签名即可拿到 play_addr）。
    """
    return page.evaluate("""async ({aweme_id, timeout_ms}) => {
        const url = '/aweme/v1/web/aweme/detail/?aweme_id=' + aweme_id
            + '&aid=6383&channel=channel_pc_web&device_platform=webapp';
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), timeout_ms);
        try {
            const resp = await fetch(url, { credentials: 'include', signal: controller.signal });
            clearTimeout(timer);
            const data = await resp.json();
            if (data.status_code !== 0) return null;
            const a = data.aweme_detail;
            if (!a) return null;
            // 如果是影集(有 images)，不在这里处理，交给 _extract_slides_from_api
            if (a.images && a.images.length) return null;

            const v = a.video || null;
            let video_url = '';
            if (v) {
                const bitRates = v.bit_rate || [];
                const nonDash = bitRates.filter(b =>
                    !((b.play_addr?.url_list || [])[0] || '').includes('/dash/'));
                nonDash.sort((a, b) => (b.quality_type || 0) - (a.quality_type || 0));
                if (nonDash.length > 0) {
                    video_url = (nonDash[0].play_addr?.url_list || [])[0] || '';
                }
                if (!video_url) video_url = (v.play_addr?.url_list || [])[0] || '';
            }
            const s = a.statistics || {};
            return {
                type: 'video',
                video_url: video_url,
                title: a.desc || '',
                author: a.author?.nickname || '',
                aweme_id: a.aweme_id || aweme_id,
                stats: {
                    digg_count: s.digg_count || 0,
                    comment_count: s.comment_count || 0,
                    collect_count: s.collect_count || 0,
                    share_count: s.share_count || 0,
                },
            };
        } catch (e) {
            clearTimeout(timer);
            return null;
        }
    }""", {"aweme_id": aweme_id, "timeout_ms": 10000})


def _extract_slides_from_dom(page) -> dict:
    """从 DOM 逐页浏览笔记页面来提取每个 slide 的视频/图片信息。

    通过反复按 ArrowRight 切换页面，记录每一页的:
    - 视频: 当前笔记 slide 是视频时，检查 video 元素是否可见且在播放
    - 图片: 当前笔记 slide 是图片时，检查图片容器是否可见
    """
    # 获取总 slide 数
    total_slides = page.evaluate("""() => {
        const m = document.body.innerText.match(/\\b(\\d+)\\s*\\/\\s*(\\d+)\\b/);
        return m ? parseInt(m[2]) : 0;
    }""")
    if not total_slides:
        total_slides = 1
        _print("[!] 未检测到分页指示器，假设只有1页")
    else:
        _print(f"[+] 检测到笔记共 {total_slides} 页")

    # 获取标题和作者
    title = page.evaluate("""() => {
        const el = document.querySelector('h1') || document.querySelector('[data-e2e="video-title"]') || document.querySelector('[class*="title"]');
        return el ? el.textContent.trim() : '';
    }""")
    author = page.evaluate("""() => {
        const el = document.querySelector('[data-e2e="video-nickname"]') || document.querySelector('[class*="author"] [class*="name"]');
        return el ? el.textContent.trim() : '';
    }""")

    slides = []

    for page_idx in range(total_slides):
        # 收集当前页的 slide 信息
        # 关键区分逻辑：笔记中的视频slide有 video player 容器且可见，
        # 图片slide有静态图片容器
        slide_info = page.evaluate("""() => {
            // 判断当前笔记 slide 是视频还是图片
            // 方法1: 检查 basePlayerContainer 的 class —— 如果包含 'hidePlayer' 就是图片页
            const playerContainer = document.querySelector('.basePlayerContainer');
            const isPlayerHidden = playerContainer ?
                (playerContainer.classList.contains('hidePlayer') ||
                 playerContainer.classList.contains('hidden') ||
                 getComputedStyle(playerContainer).display === 'none' ||
                 getComputedStyle(playerContainer).visibility === 'hidden')
                : true;

            // 方法2: 如果 xgplayer 没有 xgplayer-playing/xgplayer-pause/xgplayer-ready 也不确定
            // 所以还要检查当前页面的 "大图" 区域是否有静态图片替换了视频

            // 方法3: 检查 note-detail-container 内，当前是否有可见的 video 标签
            // 笔记页面切换slide时，图片slide会使用 <img> 显示，视频slide使用 <video>
            // 但实际上同一个 video 元素在切换不同slide时可能只改变 src 不改变元素
            // 所以需要看 noteSideBar 中的缩略图指示器来判断——不，太复杂

            // 方法4: 最简单——检查注意看 .hidePlayer
            // 当笔记切到图片slide时，player container 会被加上 hidePlayer class
            let hasVideo = !isPlayerHidden;

            // 额外验证: 如果 player 显示了，但 video src 里没有真正内容，也不算
            if (hasVideo && playerContainer) {
                const v = playerContainer.querySelector('video');
                const src = v ? (v.currentSrc || v.src || '') : '';
                if (!src || src.includes('sf6-cdn-tos.douyinstatic.com')) {
                    // 这是抖音的占位符/错误视频，不是真正的slide内容
                    // 可能是图片slide但 player 还没隐藏
                    hasVideo = false;
                }
            }

            // 获取视频URL
            let videoSrc = '';
            if (hasVideo && playerContainer) {
                const v = playerContainer.querySelector('video');
                videoSrc = v ? (v.currentSrc || v.src || '') : '';
            }

            // 收集图片 (aweme-images) — 用于图片slide
            const imgs = document.querySelectorAll('img');
            let bestImg = '';
            const imgUrls = [];
            for (const img of imgs) {
                const src = img.src || '';
                if (src.includes('aweme-image') || src.includes('aweme_images')) {
                    imgUrls.push(src);
                    if (!bestImg || (src.includes('.jpeg') && !bestImg.includes('.jpeg'))) {
                        bestImg = src;
                    }
                }
            }

            return {
                index: page_idx,
                media_type: hasVideo ? 'video' : 'image',
                video_url: videoSrc,
                image_urls: imgUrls,
                best_image_url: bestImg,
                width: 0,
                height: 0,
                duration: 0,
                clip_type: hasVideo ? 4 : 2,
            };
        }""".replace("page_idx", str(page_idx)))

        slides.append(slide_info)
        _print(f"  页 {page_idx + 1}/{total_slides}: {'视频' if slide_info.get('media_type') == 'video' else '图片'}")

        # 翻到下一页（如果不是最后一页）
        if page_idx < total_slides - 1:
            page.keyboard.press("ArrowRight")
            page.wait_for_timeout(1500)

    return {
        "type": "slide",
        "title": title,
        "author": author,
        "aweme_id": "",
        "stats": {},
        "slides": slides,
    }


def _extract_video_info(page) -> dict:
    """从 SSR / DOM 提取视频直链、标题、作者"""
    return page.evaluate("""() => {
        const app = window.SSR_RENDER_DATA?.app;
        const vd = app?.videoDetail;
        let video_url = '', title = '', author = '';
        if (vd) {
            title = vd.desc || vd.itemTitle || '';
            author = vd.authorInfo?.nickname || '';
            const video = vd.video;
            if (video) {
                const playApi = video.playApi || '';
                const bitRates = video.bitRateList || [];
                const nonDash = bitRates.filter(b => !b.playApi?.includes('/dash/'));
                nonDash.sort((a, b) => (b.qualityType || 0) - (a.qualityType || 0));
                if (nonDash.length > 0) {
                    video_url = nonDash[0].playApi || nonDash[0].playAddr?.[0]?.url || playApi;
                } else if (playApi) {
                    video_url = playApi;
                }
            }
        }
        if (!video_url) {
            const video = document.querySelector('video');
            const src = video ? (video.currentSrc || video.src) : '';
            // 中转播放 URL(/aweme/v1/play/?...&a_bogus=...) 经 HTTP 跟随 302 即得 mp4，视同直链
            if (src.includes('douyinvod') || src.includes('.mp4') || src.includes('/aweme/v1/play/')) video_url = src;
        }
        if (!title) {
            const el = document.querySelector('h1') || document.querySelector('[data-e2e="video-title"]') || document.querySelector('[class*="title"]');
            title = el ? el.textContent.trim() : '';
        }
        if (!author) {
            const el = document.querySelector('[data-e2e="video-nickname"]') || document.querySelector('[class*="author"] [class*="name"]');
            author = el ? el.textContent.trim() : '';
        }
        return { type: 'video', video_url, title, author };
    }""")


def _extract_image_title_author(page) -> dict:
    return page.evaluate("""() => {
        const t = document.querySelector('h1') || document.querySelector('[data-e2e="video-title"]') || document.querySelector('[class*="title"]');
        const a = document.querySelector('[data-e2e="video-nickname"]') || document.querySelector('[class*="author"] [class*="name"]');
        return { title: t?.textContent.trim()||'', author: a?.textContent.trim()||'' };
    }""")


def _extract_stats(page) -> dict:
    """从 SSR 提取 aweme_id / title / author / stats"""
    raw = page.evaluate("""() => {
        const vd = window.SSR_RENDER_DATA?.app?.videoDetail;
        if (!vd) return null;
        return { awemeId: vd.awemeId, title: vd.desc||vd.itemTitle||'', author: vd.authorInfo?.nickname||'', stats: vd.stats||null };
    }""")
    if not raw:
        return {}
    s = raw.get("stats") or {}
    return {
        "aweme_id": raw.get("awemeId", ""),
        "title": raw.get("title", ""),
        "author": raw.get("author", ""),
        "stats": {
            "digg_count": s.get("diggCount", 0),
            "comment_count": s.get("commentCount", 0),
            "collect_count": s.get("collectCount", 0),
            "share_count": s.get("shareCount", 0),
        },
    }


# ════════════════════════════════════════════════════════════════
#  下载
# ════════════════════════════════════════════════════════════════

def _download_file(url: str, output_path: Path, max_retries: int = 2) -> int:
    headers = {
        "Accept": "*/*",
        "Accept-Encoding": "identity;q=1, *;q=0",
        "Origin": "https://www.douyin.com",
        "Referer": "https://www.douyin.com/",
        "Range": "bytes=0-",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    }
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            with httpx.stream("GET", url, headers=headers, timeout=60, follow_redirects=True) as r:
                r.raise_for_status()
                tmp = output_path.with_suffix(output_path.suffix + ".tmp")
                total = 0
                with open(tmp, "wb") as f:
                    for chunk in r.iter_bytes(65536):
                        f.write(chunk)
                        total += len(chunk)
                if output_path.exists():
                    output_path.unlink()
                tmp.rename(output_path)
                return total
        except (httpx.HTTPStatusError, httpx.TransportError) as e:
            last_error = e
            if attempt < max_retries:
                _print(f"  [!] 下载失败，第 {attempt+1} 次重试...")
                time.sleep(1)
    raise RuntimeError(f"下载失败（已重试 {max_retries} 次）: {last_error}")


def _scroll_and_collect_images(page, max_presses=80) -> list[str]:
    _print("[*] 正在逐张浏览图集以加载所有图片...")
    total_images = page.evaluate("""() => { const m=document.body.innerText.match(/\\b(\\d+)\\s*\\/\\s*(\\d+)\\b/); return m?parseInt(m[2]):0; }""")
    if total_images:
        _print(f"[+] 检测到图集共 {total_images} 张")
    page.evaluate("""() => { window.__imgUrls=new Set(); function c(){ document.querySelectorAll('img').forEach(i=>{const s=i.src||''; if(s.includes('aweme-image')||s.includes('aweme_images')) window.__imgUrls.add(s); }); } c(); window.__collectImages=c; }""")
    prev = page.evaluate("()=>window.__imgUrls.size"); stag = 0
    for _ in range(max_presses):
        page.keyboard.press("ArrowRight"); page.wait_for_timeout(400)
        page.evaluate("()=>window.__collectImages()")
        cur = page.evaluate("()=>window.__imgUrls.size")
        if cur == prev: stag += 1;
        else: stag = 0; _print(f"  已加载 {cur} 张...")
        prev = cur
        if stag >= 8: break
        if total_images and cur >= total_images: break
    return page.evaluate("""() => { window.__collectImages(); const b={}; for(const u of window.__imgUrls){const k=u.replace(/~[^:]*/,'').replace(/\\?.*/,''); if(!b[k]||u.includes('aweme-images-v2:300')||u.includes('aweme-images:q75')) b[k]=u; } return Object.values(b); }""") or []


def _download_image_via_playwright(page, url, output_path) -> int:
    resp = page.request.get(url)
    if not resp.ok:
        raise RuntimeError(f"下载图片失败: {resp.status}")
    body = resp.body()
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp.write_bytes(body)
    if output_path.exists(): output_path.unlink()
    tmp.rename(output_path)
    return len(body)


# ════════════════════════════════════════════════════════════════
#  评论抓取
# ════════════════════════════════════════════════════════════════

def _parse_comment(raw: dict) -> dict:
    return {
        "cid": raw.get("cid",""),
        "user": raw.get("user",{}).get("nickname",""),
        "text": raw.get("text",""),
        "create_time": _format_timestamp(raw.get("create_time",0)),
        "create_timestamp": raw.get("create_time",0),
        "digg_count": raw.get("digg_count",0),
        "reply_count": raw.get("reply_comment_total",0),
        "ip_label": raw.get("ip_label",""),
        "replies": [],
    }

def _parse_reply(raw: dict) -> dict:
    return {
        "cid": raw.get("cid",""),
        "user": raw.get("user",{}).get("nickname",""),
        "text": raw.get("text",""),
        "create_time": _format_timestamp(raw.get("create_time",0)),
        "create_timestamp": raw.get("create_time",0),
        "digg_count": raw.get("digg_count",0),
        "ip_label": raw.get("ip_label",""),
    }


def _fetch_all_comments(page, aweme_id: str, comment_count: int) -> list[dict]:
    """
    抓取主评论（不含回复）。使用 response 监听 + 逐页 fetch。
    回复抓取已移除——极易卡死且收益低。
    """
    all_comments: list[dict] = []
    _print(f"[*] 正在抓取主评论（共 {comment_count} 条，不含回复）...")

    # ── 注册 response 监听，捕获浏览器自然发出的评论 API ──
    captured_first_batch: list[dict] = []
    captured_pagination: dict = {"has_more": True, "cursor": 0}

    def _catch_comment_api(response):
        url = response.url
        if "/aweme/v1/web/comment/list/" in url and "/reply/" not in url:
            try:
                data = response.json()
                if data.get("status_code") == 0:
                    for c in data.get("comments", []):
                        captured_first_batch.append(c)
                    captured_pagination["has_more"] = bool(data.get("has_more"))
                    captured_pagination["cursor"] = data.get("cursor", 0)
            except Exception:
                pass

    page.on("response", _catch_comment_api)

    # ── 先打开评论区，触发浏览器自然发出第一条评论 API ──
    page.evaluate("""() => {
        const icon = document.querySelector('[data-e2e="feed-comment-icon"]');
        if (icon) {
            icon.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
            icon.dispatchEvent(new PointerEvent('click', { bubbles: true, cancelable: true }));
        }
    }""")
    page.wait_for_timeout(4000)

    # ── 去除监听器 ──
    page.remove_listener("response", _catch_comment_api)

    # 处理第一批捕获的数据
    if captured_first_batch:
        all_comments.extend(captured_first_batch)
        cursor = captured_pagination.get("cursor", 0)
        has_more = captured_pagination.get("has_more", False)
        _print(f"[+] 第 1 批（浏览器自动请求）: {len(captured_first_batch)} 条")
    else:
        cursor = 0
        has_more = True
        _print("[!] 未捕获到浏览器自动请求的评论，将从 cursor=0 开始抓取")

    # ── 主评论分页 fetch（带超时） ──
    page_num = 1
    consecutive_errors = 0
    while has_more and consecutive_errors < 3:
        page_num += 1
        try:
            result = page.evaluate("""async ({aweme_id, cursor, count, timeout_ms}) => {
                const base = window.location.origin + '/aweme/v1/web/comment/list/';
                const params = new URLSearchParams({
                    device_platform: 'webapp', aid: '6383', channel: 'channel_pc_web',
                    aweme_id: String(aweme_id), cursor: String(cursor),
                    count: String(count), item_type: '0',
                });
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), timeout_ms);
                try {
                    const resp = await fetch(base + '?' + params.toString(), { credentials: 'include', signal: controller.signal });
                    clearTimeout(timer);
                    const data = await resp.json();
                    if (data.status_code !== 0) return { error: 'status_code=' + data.status_code };
                    return { comments: data.comments || [], has_more: !!data.has_more, cursor: data.cursor || 0 };
                } catch (e) { clearTimeout(timer); return { error: e.name === 'AbortError' ? 'timeout' : e.message }; }
            }""", {"aweme_id": aweme_id, "cursor": cursor, "count": 20, "timeout_ms": 10000})
        except Exception as e:
            _print(f"[!] 第 {page_num} 批 evaluate 异常: {e}")
            consecutive_errors += 1
            page.wait_for_timeout(2000)
            continue

        if not result:
            consecutive_errors += 1
            page.wait_for_timeout(1000)
            continue

        if isinstance(result, dict) and result.get("error"):
            _print(f"[!] 第 {page_num} 批请求失败: {result['error']}")
            if result["error"] == "timeout":
                page.wait_for_timeout(3000)
            consecutive_errors += 1
            continue

        consecutive_errors = 0
        comments = result.get("comments", [])
        if not comments:
            break

        seen_cids = {c.get("cid") for c in all_comments}
        new_comments = [c for c in comments if c.get("cid") not in seen_cids]
        all_comments.extend(new_comments)
        cursor = result.get("cursor", 0)
        has_more = result.get("has_more", False)
        _print(f"[+] 第 {page_num} 批: {len(new_comments)} 条新评论（共 {len(all_comments)} 条）")

        if not has_more:
            break
        page.wait_for_timeout(800)

    if consecutive_errors >= 3:
        _print(f"[!] 评论分页连续 3 次失败，停止抓取")

    _print(f"[+] 主评论加载完成，共 {len(all_comments)} 条")

    # ── 仅解析主评论，不抓取回复 ──
    seen_cids = set()
    result_list = []
    for c in all_comments:
        cid = c.get("cid", "")
        if cid in seen_cids:
            continue
        seen_cids.add(cid)
        result_list.append(_parse_comment(c))

    return result_list


# ════════════════════════════════════════════════════════════════
#  JSON 保存
# ════════════════════════════════════════════════════════════════

def _save_json(output_dir: Path, meta: dict, comments: list) -> str:
    """保存完整的 JSON 数据（包含互动数据 + 评论）"""
    output = {
        "aweme_id": meta.get("aweme_id", ""),
        "title": meta.get("title", ""),
        "author": meta.get("author", ""),
        "stats": meta.get("stats", {}),
        "comment_count": len(comments),
        "comments": comments,
    }
    path = output_dir / (_sanitize_filename(meta.get("title", "douyin")) + ".json")
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


# ════════════════════════════════════════════════════════════════
#  目录结构决策
# ════════════════════════════════════════════════════════════════

def _decide_output_dir(base_path: Path, mode: int, title: str, content_type: str, wrap_folder: bool = False) -> Path:
    """
    决定输出目录策略:
    - wrap_folder=False: 直接放入调用方传入的根目录
    - wrap_folder=True: 在根目录下按标题创建子文件夹
    """
    if not wrap_folder:
        return base_path
    folder = base_path / _sanitize_filename(title)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _media_file_path(folder: Path, base_path: Path, title: str, index: int, ext: str, wrap_folder: bool = False, width: int = 3, prefix: str = "") -> Path:
    if wrap_folder or folder != base_path:
        return folder / f"{prefix}{index:0{width}d}{ext}"
    marker = f"{prefix}{index:0{width}d}"
    return folder / f"{_sanitize_filename(title)}_{marker}{ext}"


# ════════════════════════════════════════════════════════════════
#  单个 URL 处理（含独立浏览器实例，线程安全）
# ════════════════════════════════════════════════════════════════

def download_douyin(url: str, output_dir: str = "", mode: int = 1,
                    fetch_comments: bool = False, selected_indices: list | None = None,
                    wrap_folder: bool = False) -> dict:
    """
    下载抖音视频/图集 + 互动数据（可选评论）
    每个 URL 用独立的浏览器实例，可多线程并行。

    Args:
        url:             抖音链接
        output_dir:      输出目录
        mode:            1=仅下载视频/图集, 2=仅数据(JSON), 3=全部
        fetch_comments:  是否抓取评论（默认否，评论容易卡死）
        selected_indices: 图集/笔记类型时仅下载指定序号（1-based）的图片/视频；
                          None 表示全部下载。对单视频类型忽略。
        wrap_folder:     是否在输出根目录下按标题创建子文件夹
        fetch_comments: 是否抓取评论（默认否，评论容易卡死）

    Returns:
        dict: 下载结果
    """
    if not output_dir:
        output_dir = str(Path.cwd() / "downloads")
    base_path = Path(output_dir)
    base_path.mkdir(parents=True, exist_ok=True)
    wanted = set(int(i) for i in selected_indices) if selected_indices else None
    if wanted is not None:
        _print(f"[*] 图集选择下载: {sorted(wanted)} 共 {len(wanted)} 项")

    _print("[*] 正在启动浏览器...")

    # ── 登录态获取策略（三级回退）──
    # 抖音对未登录访客不注入可下载直链(SSR videoDetail 缺失, <video> 多为 blob/MSE)，
    # 必须用登录态 profile 才能在详情页拿到 <video> 的
    #   /aweme/v1/play/?...&a_bogus=... 直链(其 HTTP 302 跳转到 douyinvod mp4)。
    # profile 由 douyin-user-search 维护(含 sessionid/sid_guard 等 HttpOnly cookie)，
    # 为避免与 user-search 抢同一 profile 的锁，优先连常驻调试 Chrome(9222)，
    # 其自己独占 profile，downloader 只连不锁。
    # 环境变量覆盖：
    #   DOUYIN_CDP=http://127.0.0.1:9222   优先 CDP 连接(默认即此)；置空强制走 profile/裸启动
    #   DOUYIN_PROFILE=/path/to/profile    CDP 连不上时回退用此 profile 启动
    import os
    _cdp = os.environ.get("DOUYIN_CDP", "http://127.0.0.1:9222")
    _profile_env = os.environ.get("DOUYIN_PROFILE", "default")
    user_data_dir = None
    if _profile_env:
        if _profile_env == "default":
            _cand = Path(__file__).resolve().parent.parent / "douyin-user-search" / "douyin_profile"
            user_data_dir = str(_cand) if _cand.exists() else None
        else:
            user_data_dir = _profile_env
            if not Path(user_data_dir).exists():
                _print(f"[!] 指定的 DOUYIN_PROFILE 不存在: {user_data_dir}")
                user_data_dir = None

    with sync_playwright() as p:
        browser = None
        context = None
        page = None
        _owns_context = False  # True 表示本函数可安全 close context；CDP 复用常驻 context 时为 False
        if _cdp:
            try:
                _print(f"[*] 尝试 CDP 连接常驻 Chrome: {_cdp}")
                browser = p.chromium.connect_over_cdp(_cdp)
                # CDP 模式下 browser.new_context() 会在常驻 Chrome 弹出一个可见窗口，
                # 不符合"下载静默"的要求。改为复用常驻 Chrome 已有的默认 context，
                # 仅在其上 new_page() —— 新页在常驻 Chrome 内打开但不弹独立窗口，
                # 且本函数 finally 会把它 close 掉，不残留标签。
                ctxs = browser.contexts
                if ctxs:
                    context = ctxs[0]
                    _owns_context = False  # 复用常驻 Chrome 默认 context，不可关
                else:
                    context = browser.new_context(viewport={"width": 1600, "height": 900})
                    _owns_context = True
                page = context.new_page()
                _print("[+] CDP 连接成功（复用常驻 context）")
            except Exception as e:
                _print(f"[!] CDP 连接失败({e})，回退 profile 启动")
                browser = None
        if page is None and user_data_dir:
            try:
                _print(f"[*] 复用登录态 profile: {user_data_dir}")
                context = p.chromium.launch_persistent_context(
                    user_data_dir,
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                page = context.pages[0] if context.pages else context.new_page()
                _owns_context = True  # persistent context 需自己关
                _print("[+] profile 启动成功")
            except Exception as e:
                _print(f"[!] profile 启动失败({e})，回退裸启动")
                context = None
                page = None
        if page is None:
            _print("[*] 裸启动浏览器（无登录态，可能拿不到直链）")
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                viewport={"width": 1600, "height": 900},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
            )
            page = context.new_page()
            _owns_context = True  # 自有 context，需要关

        try:
            # 发现页/搜索页/喜欢列表等聚合页 SSR 里没有 videoDetail，
            # 先规约到标准详情页，才能拿到图集/视频数据。
            # v.douyin.com 短链会 302 跳转，让其自然落地后按落地页类型处理更为稳妥。
            is_short = "v.douyin.com" in url
            if "/video/" not in url and "/note/" not in url:
                candidates = _detail_url_candidates(url)
                if candidates and not is_short:
                    detail_url = candidates[0]
                    _print(f"[*] 聚合页，规约到详情页: {detail_url}")
                    url = detail_url
            _print(f"[*] 正在访问: {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            # 短链落地后若既非 /video/ 也非 /note/（极少见），按 aweme_id 规约到 /note/
            if is_short:
                landed = page.url or url
                if "/video/" not in landed and "/note/" not in landed:
                    candidates = _detail_url_candidates(landed) or _detail_url_candidates(url)
                    if candidates:
                        detail_url = candidates[0]
                        _print(f"[*] 短链落地异常，规约到详情页: {detail_url}")
                        page.goto(detail_url, wait_until="domcontentloaded", timeout=20000)
            # 等待页面加载：视频/图集/笔记页面
            try:
                page.wait_for_selector("video, h1, [data-e2e='video-title'], [class*='note'], [class*='note-detail']", timeout=15000)
            except Exception:
                pass
            # 等更长时间让 SPA 渲染完成（笔记页面的 SSR_RENDER_DATA 是后续注入的）
            page.wait_for_timeout(3000)

            # ── 检测并处理 /video/ → /note/ 重定向 ──
            current_url = page.url
            if "/video/" in url and "/note/" in current_url:
                _print(f"[*] 页面从 /video/ 重定向到了 /note/（这是笔记/Slides 类型）")

            # ── 提取互动数据 ──
            _print("[*] 正在提取互动数据...")
            meta = _extract_stats(page)
            if meta and meta.get("stats"):
                s = meta["stats"]
                _print(f"[+] 点赞: {s['digg_count']}  评论: {s['comment_count']}"
                       f"  收藏: {s['collect_count']}  转发: {s['share_count']}")
            else:
                _print("[!] 未能提取互动数据")

            # ── 精选页导航 ──
            aweme_id = meta.get("aweme_id", "")
            current_url = page.url
            video_url_from_ssr = ""

            if "/jingxuan" in current_url and aweme_id:
                video_url_from_ssr = page.evaluate("""() => {
                    const vd = window.SSR_RENDER_DATA?.app?.videoDetail;
                    if (!vd?.video) return '';
                    const b = vd.video.bitRateList||[];
                    const nd = b.filter(x=>!x.playApi?.includes('/dash/'));
                    nd.sort((a,b)=>(b.qualityType||0)-(a.qualityType||0));
                    return nd.length?nd[0].playApi||'':vd.video.playApi||'';
                }""")
                detail_url = f"https://www.douyin.com/video/{aweme_id}"
                _print(f"[*] 导航到视频详情页: {detail_url}")
                page.goto(detail_url, wait_until="domcontentloaded", timeout=20000)
                try:
                    page.wait_for_selector("video, h1, [data-e2e='video-title']", timeout=15000)
                except Exception:
                    pass
                page.wait_for_timeout(2000)
                if not video_url_from_ssr:
                    video_url_from_ssr = page.evaluate("""() => {
                        const vd=window.SSR_RENDER_DATA?.app?.videoDetail;
                        if(!vd?.video) return '';
                        const b=vd.video.bitRateList||[];
                        const nd=b.filter(x=>!x.playApi?.includes('/dash/'));
                        nd.sort((a,b)=>(b.qualityType||0)-(a.qualityType||0));
                        return nd.length?nd[0].playApi||'':vd.video.playApi||'';
                    }""")
                dm = _extract_stats(page)
                if dm and dm.get("stats"):
                    meta = dm

            # ── 判断内容类型 ──
            content_type = _extract_content_type(page)

            if content_type == "video":
                info = _extract_video_info(page)
                video_url = info.get("video_url", "") or video_url_from_ssr
                title = info.get("title", "") or meta.get("title", "")
                author = info.get("author", "") or meta.get("author", "")
                # SSR 没注入可下载直链时，回退到 aweme detail API 取平面 aweme.video
                # 直链(适用未登录/特定作品/直播回放等 SSR videoDetail 空、<video> 为 blob/MSE 的场景)
                if not video_url:
                    api_aweme_id = aweme_id
                    if not api_aweme_id:
                        api_aweme_id = (extract_aweme_ids_from_url(page.url) or [None])[0] or ""
                    if api_aweme_id:
                        api_info = _extract_video_from_api(page, api_aweme_id)
                        if api_info:
                            video_url = api_info.get("video_url", "")
                            title = title or api_info.get("title", "")
                            author = author or api_info.get("author", "")
                            aweme_id = aweme_id or api_info.get("aweme_id", "")
                            if not meta.get("stats"):
                                meta["stats"] = api_info.get("stats", {})
                if not video_url:
                    raise RuntimeError("未能提取到有效的视频地址")

                _print(f"[+] 类型: 视频")
                _print(f"[+] 标题: {title}")
                if author:
                    _print(f"[+] 作者: {author}")

                # ── 决定输出目录 ──
                out_dir = _decide_output_dir(base_path, mode, title, "video", wrap_folder)

                result = {
                    "type": "video",
                    "title": title,
                    "author": author,
                    "aweme_id": aweme_id,
                    "stats": meta.get("stats", {}),
                    "comments": [],
                    "comment_json": "",
                    "folder": str(out_dir) if out_dir != base_path else "",
                }

                # ── 抓取评论（仅当 fetch_comments=True 且 mode 2/3） ──
                if fetch_comments and mode in (2, 3) and meta.get("stats", {}).get("comment_count", 0) > 0:
                    try:
                        comments = _fetch_all_comments(page, aweme_id, meta["stats"]["comment_count"])
                        result["comments"] = comments
                        _print(f"[+] 评论抓取完成，共 {len(comments)} 条")
                    except Exception as e:
                        _print(f"[!] 评论抓取失败: {e}")

                    try:
                        result["comment_json"] = _save_json(out_dir, {
                            "aweme_id": aweme_id, "title": title, "author": author,
                            "stats": meta.get("stats", {}),
                        }, result["comments"])
                        _print(f"[+] 数据已保存: {result['comment_json']}")
                    except Exception as e:
                        _print(f"[!] 保存失败: {e}")

                # mode=2 且没有评论也要保存互动数据 JSON
                if mode == 2 and not result.get("comment_json"):
                    try:
                        result["comment_json"] = _save_json(out_dir, {
                            "aweme_id": aweme_id, "title": title, "author": author,
                            "stats": meta.get("stats", {}),
                        }, [])
                        _print(f"[+] 互动数据已保存: {result['comment_json']}")
                    except Exception as e:
                        _print(f"[!] 保存失败: {e}")

                # ── mode 1/3: 下载视频 ──
                if mode in (1, 3):
                    filename = _sanitize_filename(title) + ".mp4"
                    file_path = out_dir / filename
                    _print("[*] 正在下载视频...")
                    file_size = _download_file(video_url, file_path)
                    result["file_path"] = str(file_path)
                    result["file_size"] = file_size

                return result

            elif content_type == "slide":
                # ── 笔记/Slides 类型: 混合视频+图片 ──
                slide_info = _extract_slides_info(page)
                if not slide_info or not slide_info.get("slides"):
                    raise RuntimeError("未能从 SSR 提取 slide 信息，尝试回退到图集模式")

                title = slide_info.get("title", "") or meta.get("title", "")
                author = slide_info.get("author", "") or meta.get("author", "")
                aweme_id = slide_info.get("aweme_id", "") or aweme_id
                stats = slide_info.get("stats", {}) or meta.get("stats", {})
                slides = slide_info.get("slides", [])

                video_count = sum(1 for s in slides if s.get("media_type") == "video")
                image_count = sum(1 for s in slides if s.get("media_type") == "image")
                _print(f"[+] 类型: 笔记/Slides ({video_count} 个视频, {image_count} 张图片, 共 {len(slides)} 页)")
                _print(f"[+] 标题: {title}")
                if author:
                    _print(f"[+] 作者: {author}")

                folder = _decide_output_dir(base_path, mode, title, "slide", wrap_folder)

                result = {
                    "type": "slide",
                    "title": title,
                    "author": author,
                    "aweme_id": aweme_id,
                    "stats": stats,
                    "comments": [],
                    "comment_json": "",
                    "folder": str(folder),
                    "slide_count": len(slides),
                    "video_count": video_count,
                    "image_count": image_count,
                    "ok_count": 0,
                    "fail_count": 0,
                    "total_size": 0,
                }

                # ── 抓取评论（仅当 fetch_comments=True 且 mode 2/3） ──
                if fetch_comments and mode in (2, 3) and stats.get("comment_count", 0) > 0:
                    try:
                        comments = _fetch_all_comments(page, aweme_id, stats["comment_count"])
                        result["comments"] = comments
                        _print(f"[+] 评论抓取完成，共 {len(comments)} 条")
                    except Exception as e:
                        _print(f"[!] 评论抓取失败: {e}")

                    try:
                        result["comment_json"] = _save_json(folder, {
                            "aweme_id": aweme_id, "title": title, "author": author,
                            "stats": stats,
                        }, result["comments"])
                        _print(f"[+] 数据已保存: {result['comment_json']}")
                    except Exception as e:
                        _print(f"[!] 保存失败: {e}")

                if mode == 2 and not result.get("comment_json"):
                    try:
                        result["comment_json"] = _save_json(folder, {
                            "aweme_id": aweme_id, "title": title, "author": author,
                            "stats": stats,
                        }, [])
                        _print(f"[+] 互动数据已保存: {result['comment_json']}")
                    except Exception as e:
                        _print(f"[!] 保存失败: {e}")

                # ── mode 1/3: 逐个下载 slide ──
                if mode in (1, 3):
                    total_size = 0
                    ok_count = 0
                    fail_count = 0
                    for slide in slides:
                        idx = slide["index"] + 1
                        if wanted is not None and idx not in wanted:
                            continue
                        if slide.get("media_type") == "video":
                            # 下载视频
                            video_url = slide.get("video_url", "")
                            if not video_url:
                                _print(f"  [{idx}/{len(slides)}] ! 第 {idx} 页视频: 无法获取视频地址，跳过")
                                fail_count += 1
                                continue
                            filename = _sanitize_filename(title) + f"_S{idx:02d}.mp4"
                            file_path = folder / filename
                            _print(f"  [{idx}/{len(slides)}] 下载视频: {file_path.name}")
                            try:
                                total_size += _download_file(video_url, file_path)
                                ok_count += 1
                            except Exception as e:
                                fail_count += 1
                                _print(f"  [!] 第 {idx} 页视频下载失败: {e}")
                        else:
                            # 下载图片
                            img_url = slide.get("best_image_url", "")
                            if not img_url:
                                # 从 urlList 中任取一个
                                img_url = (slide.get("image_urls") or [""])[0]
                            if not img_url:
                                _print(f"  [{idx}/{len(slides)}] ! 第 {idx} 页图片: 无法获取图片地址，跳过")
                                fail_count += 1
                                continue
                            ext = ".webp"
                            if ".jpeg" in img_url or ".jpg" in img_url:
                                ext = ".jpg"
                            elif ".png" in img_url:
                                ext = ".png"
                            file_path = _media_file_path(folder, base_path, title, idx, ext, wrap_folder, width=2, prefix="S")
                            _print(f"  [{idx}/{len(slides)}] 下载图片: {file_path.name}")
                            try:
                                total_size += _download_image_via_playwright(page, img_url, file_path)
                                ok_count += 1
                            except Exception as e:
                                fail_count += 1
                                _print(f"  [!] 第 {idx} 页图片下载失败: {e}")

                    result["ok_count"] = ok_count
                    result["fail_count"] = fail_count
                    result["total_size"] = total_size

                return result

            elif content_type == "image":
                image_urls = _scroll_and_collect_images(page)
                if not image_urls:
                    raise RuntimeError("未能从页面中提取到图片地址")

                info = _extract_image_title_author(page)
                title = info.get("title", "") or meta.get("title", "")
                author = info.get("author", "") or meta.get("author", "")

                _print(f"[+] 类型: 图集 ({len(image_urls)} 张)")
                _print(f"[+] 标题: {title}")
                if author:
                    _print(f"[+] 作者: {author}")

                folder = _decide_output_dir(base_path, mode, title, "image", wrap_folder)

                result = {
                    "type": "image",
                    "title": title,
                    "author": author,
                    "aweme_id": aweme_id,
                    "stats": meta.get("stats", {}),
                    "comments": [],
                    "comment_json": "",
                    "image_dir": str(folder),
                    "folder": str(folder),
                    "image_count": len(image_urls),
                    "ok_count": 0,
                    "fail_count": 0,
                    "total_size": 0,
                }

                # ── 抓取评论（仅当 fetch_comments=True 且 mode 2/3） ──
                if fetch_comments and mode in (2, 3) and meta.get("stats", {}).get("comment_count", 0) > 0:
                    try:
                        comments = _fetch_all_comments(page, aweme_id, meta["stats"]["comment_count"])
                        result["comments"] = comments
                    except Exception as e:
                        _print(f"[!] 评论抓取失败: {e}")

                    try:
                        result["comment_json"] = _save_json(folder, {
                            "aweme_id": aweme_id, "title": title, "author": author,
                            "stats": meta.get("stats", {}),
                        }, result["comments"])
                        _print(f"[+] 数据已保存: {result['comment_json']}")
                    except Exception as e:
                        _print(f"[!] 保存失败: {e}")

                # mode=2 且没有评论也要保存互动数据
                if mode == 2 and not result.get("comment_json"):
                    try:
                        result["comment_json"] = _save_json(folder, {
                            "aweme_id": aweme_id, "title": title, "author": author,
                            "stats": meta.get("stats", {}),
                        }, [])
                        _print(f"[+] 互动数据已保存: {result['comment_json']}")
                    except Exception as e:
                        _print(f"[!] 保存失败: {e}")

                # ── mode 1/3: 下载图片 ──
                if mode in (1, 3):
                    total_size = 0
                    ok_count = 0
                    fail_count = 0
                    for i, img_url in enumerate(image_urls, 1):
                        if wanted is not None and i not in wanted:
                            continue
                        ext = ".webp"
                        if ".jpeg" in img_url or ".jpg" in img_url: ext = ".jpg"
                        elif ".png" in img_url: ext = ".png"
                        file_path = _media_file_path(folder, base_path, title, i, ext, wrap_folder, width=3)
                        if file_path.exists() and file_path.stat().st_size > 0:
                            ok_count += 1; continue
                        _print(f"  [{i}/{len(image_urls)}] 下载: {file_path.name}")
                        try:
                            total_size += _download_image_via_playwright(page, img_url, file_path)
                            ok_count += 1
                        except Exception as e:
                            fail_count += 1
                            _print(f"  [!] 第 {i} 张失败: {e}")
                    result["ok_count"] = ok_count
                    result["fail_count"] = fail_count
                    result["total_size"] = total_size

                return result

            else:
                raise RuntimeError("无法判断页面内容类型。请确认链接有效。")

        finally:
            # CDP 复用常驻 Chrome context 时：只关本次新建的 page，
            # 绝不关 context/browser（那会关掉用户常驻 Chrome）。
            # 自有浏览器（profile/裸启动）才 close context + browser。
            if not _owns_context:
                try:
                    if page is not None:
                        page.close()
                except Exception:
                    pass
                # browser 在 CDP 模式表示 CDP 连接句柄:close 仅断连,不杀常驻 Chrome
                try:
                    if browser is not None:
                        browser.close()
                except Exception:
                    pass
            else:
                try:
                    if context is not None:
                        context.close()
                except Exception:
                    pass
                try:
                    if browser is not None:
                        browser.close()
                except Exception:
                    pass


# ── 向后兼容 ──
def download_douyin_video(url: str, output_dir: str = "", wrap_folder: bool = False) -> dict:
    return download_douyin(url, output_dir, wrap_folder=wrap_folder)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python downloader.py <抖音链接> [-m 1|2|3]")
        sys.exit(1)

    # 简单解析 mode 参数
    mode = 3
    for i, arg in enumerate(sys.argv):
        if arg == "-m" and i + 1 < len(sys.argv):
            try:
                mode = int(sys.argv[i + 1])
            except ValueError:
                pass

    result = download_douyin(sys.argv[1], mode=mode)
    if result["type"] == "video":
        if result.get("file_path"):
            _print(f"\n[OK] 视频: {result['file_path']}  ({result['file_size']/(1024*1024):.2f} MB)")
        if result.get("comment_json"):
            _print(f"[OK] JSON:  {result['comment_json']}")
    else:
        if result.get("ok_count"):
            _print(f"\n[OK] 图集: {result['ok_count']}/{result['image_count']} 张")
        if result.get("comment_json"):
            _print(f"[OK] JSON:  {result['comment_json']}")

    stats = result.get("stats", {})
    if stats:
        _print(f"  👍 {stats.get('digg_count',0)}  💬 {stats.get('comment_count',0)}"
               f"  ⭐ {stats.get('collect_count',0)}  🔄 {stats.get('share_count',0)}")
