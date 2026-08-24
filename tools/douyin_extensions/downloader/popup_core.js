// popup_core.js — 公共 URL 解析与设置回退（download + live-overlay 共用同一思路）。
//
// 关键点：
// - 抖音同一条作品在不同页面场景下 URL 模板不同，但最终都规约到 /video/<aweme_id> 或
//   /note/<aweme_id>。这里把"是否可下载"+"提取 sec_uid"+"提取 aweme_id"统一抽象。
// - 与后端 backend/services.py::expand_aggregation_urls + extract_aweme_ids_from_url
//   同源：modal_id / aweme_id / vid 等字段等价；/user/self（喜欢列表）和
//   /user/<sec_uid>?from_tab_name=main（个人主页+推荐 modal）都吃 modal_id。

export function parseSecUid(url) {
  try {
    const parsed = new URL(url);
    const match = parsed.pathname.match(/\/user\/([^/?#]+)/);
    return match ? decodeURIComponent(match[1]) : '';
  } catch (error) {
    return '';
  }
}

export function extractAwemeIds(url) {
  if (!url) return [];
  const ids = [];
  const seen = new Set();
  const add = (v) => {
    const t = String(v || '').trim();
    if (/^\d{10,}$/.test(t) && !seen.has(t)) {
      seen.add(t);
      ids.push(t);
    }
  };
  const pathMatch = url.match(/\/(?:video|note)\/(\d+)/);
  if (pathMatch) {
    add(pathMatch[1]);
    return ids;
  }
  try {
    const parsed = new URL(url);
    const queryKeys = ['modal_id', 'aweme_id', 'awemeId', 'video_id', 'vid'];
    for (const key of queryKeys) {
      const values = parsed.searchParams.getAll(key);
      for (const v of values) add(v);
    }
    for (const seg of parsed.pathname.split('/')) add(seg);
  } catch (error) {
    // 非标准 URL 走裸正则兜底
    const m = url.match(/modal_id=(\d+)/);
    if (m) add(m[1]);
  }
  return ids;
}

export function canonicalDetailUrl(url) {
  // 把聚合页 / 短链规约成 `/video/<aweme_id>`；无 aweme_id 则原样返回。
  if (!url) return '';
  if (url.includes('v.douyin.com/')) return url; // 短链交给后端 302 处理
  if (/\/(?:video|note)\/\d+/.test(url)) return url;
  const ids = extractAwemeIds(url);
  if (!ids.length) return url;
  return `https://www.douyin.com/video/${ids[0]}`;
}

// "可以下载到本地"判定：覆盖以下场景
//  1. 详情页 /video/<id>、/note/<id>
//  2. 短链 v.douyin.com/...
//  3. 推荐 /jingxuan?modal_id=...
//  4. 搜索 /jingxuan/search/...?...&modal_id=...
//  5. 喜欢列表 /user/self?...&modal_id=...
//  6. 个人主页 modal 弹窗 /user/<sec_uid>?from_tab_name=main&modal_id=...
//  7. /discover 与 /follow 等聚合页(都有 modal_id)
// 即：只要能拿到 aweme_id 就算可下载；URL 本身不需要长得像"详情页"。
export function canDownload(url) {
  if (!url) return false;
  if (!/douyin\.com/.test(url)) return false;
  if (/^https:\/\/v\.douyin\.com\//.test(url)) return true;
  if (/\/(?:video|note)\/\d+/.test(url)) return true;
  if (/[?&]modal_id=/.test(url)) return true;
  if (/[?&](?:aweme_id|awemeId|video_id|vid)=/.test(url)) return true;
  return false;
}

export function isProfilePage(url) {
  return Boolean(parseSecUid(url || ''));
}

export async function getSettingsWithFallback(fetchSettings) {
  try {
    return await fetchSettings();
  } catch (error) {
    if (error?.status === 404 || /not found/i.test(error?.message || '')) {
      return {
        download_output_dir: '',
        wrap_download_folder: false,
      };
    }
    throw error;
  }
}
