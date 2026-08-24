// content.js — Douyin Downloader Helper page-side floating button.
//
// 设计目标：
//   - 在抖音 web 上打开任意可下载/可导入的页面(详情、聚合、个人主页),在右上角显示一个
//     圆形悬浮按钮,点击展开详情面板,提供下载/导入/打开控制台三个动作。
//   - 不再依赖用户去 chrome://extensions 点 popup 触发,降低使用门槛。
//   - 与 popup.html 共用 popup_core.js 的解析逻辑(URL → 可下载/可导入)。
//
// 页面场景覆盖(详见下方 _dyDlhCore 内联片段,对应原 popup_core.js::canDownload):
//   - /video/<id>  详情页(主页点击进入)
//   - /note/<id>   图集笔记(搜索点击进入)
//   - /user/<sec_uid>?modal_id=<id>  个人主页 modal 弹窗(主页推荐、关注 modal)
//   - /user/self?modal_id=<id>       喜欢列表 modal
//   - /user/following?modal_id=<id>  关注列表 modal(URL 形态同喜欢,共用 modal_id 规则)
//   - /jingxuan?modal_id=<id>        推荐流
//   - /jingxuan/search/...&modal_id=<id>  搜索结果
//   - /discover?...&modal_id=<id>    发现页 modal(同 /jingxuan,后端已支持)
//   - v.douyin.com/...               短链(交给后端 302)
//
// 注:MV3 content_script 不支持 import/export,原 popup_core.js 必须内联在此。
//     popup.html 端不再需要这套函数(已退化为纯说明页),所以无副作用。
const _dyDlhCore = (() => {
  function parseSecUid(url) {
    try {
      const parsed = new URL(url);
      const match = parsed.pathname.match(/\/user\/([^/?#]+)/);
      return match ? decodeURIComponent(match[1]) : '';
    } catch (error) {
      return '';
    }
  }

  function extractAwemeIds(url) {
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
      const m = url.match(/modal_id=(\d+)/);
      if (m) add(m[1]);
    }
    return ids;
  }

  function canonicalDetailUrl(url) {
    if (!url) return '';
    if (url.includes('v.douyin.com/')) return url;
    if (/\/(?:video|note)\/\d+/.test(url)) return url;
    const ids = extractAwemeIds(url);
    if (!ids.length) return url;
    return `https://www.douyin.com/video/${ids[0]}`;
  }

  function canDownload(url) {
    if (!url) return false;
    if (!/douyin\.com/.test(url)) return false;
    if (/^https:\/\/v\.douyin\.com\//.test(url)) return true;
    if (/\/(?:video|note)\/\d+/.test(url)) return true;
    if (/[?&]modal_id=/.test(url)) return true;
    if (/[?&](?:aweme_id|awemeId|video_id|vid)=/.test(url)) return true;
    return false;
  }

  function isProfilePage(url) {
    return Boolean(parseSecUid(url || ''));
  }

  return { parseSecUid, canonicalDetailUrl, canDownload, isProfilePage };
})();
const { canDownload, canonicalDetailUrl, isProfilePage, parseSecUid } = _dyDlhCore;

const API_BASE = 'http://127.0.0.1:8000';
const DASHBOARD_URL = 'http://127.0.0.1:5175';

const ICON_DOWNLOAD_SVG = `
<svg viewBox="0 0 24 24" aria-hidden="true">
  <path d="M12 3a1 1 0 0 1 1 1v9.586l2.293-2.293a1 1 0 0 1 1.414 1.414l-4 4a1 1 0 0 1-1.414 0l-4-4a1 1 0 0 1 1.414-1.414L11 13.586V4a1 1 0 0 1 1-1z"/>
  <path d="M5 18a1 1 0 0 1 1-1h12a1 1 0 1 1 0 2H6a1 1 0 0 1-1-1z"/>
</svg>`;

(function () {
  if (window.__dyDlhInjected) return;
  window.__dyDlhInjected = true;

  let fab = null;
  let panel = null;
  let statusEl = null;
  let busy = false;

  function getApiBase() {
    // 仅用于展示/调试;真正发请求走 background service worker。
    try {
      return window.localStorage.getItem('dy_dlh_api_base') || API_BASE;
    } catch (e) {
      return API_BASE;
    }
  }

  function setStatus(message, kind) {
    if (!statusEl) return;
    statusEl.textContent = message;
    statusEl.className = 'dy-dlh-status' + (kind ? ' ' + kind : '');
  }

  function ensureFab() {
    if (fab) return fab;
    fab = document.createElement('button');
    fab.id = 'dy-dlh-fab';
    fab.className = 'dy-dlh-fab';
    fab.type = 'button';
    fab.title = 'Douyin Downloader Helper';
    fab.innerHTML = ICON_DOWNLOAD_SVG;
    fab.addEventListener('click', () => {
      if (busy) return;
      togglePanel();
    });
    document.documentElement.appendChild(fab);
    return fab;
  }

  function ensurePanel() {
    if (panel) return panel;
    panel = document.createElement('div');
    panel.id = 'dy-dlh-panel';
    panel.className = 'dy-dlh-panel dy-dlh-hidden';
    panel.innerHTML = `
      <h4>抖音下载助手</h4>
      <div class="dy-dlh-url" data-role="url"></div>
      <button class="dy-dlh-btn" data-action="download">下载当前作品</button>
      <button class="dy-dlh-btn secondary" data-action="import">导入当前主页</button>
      <button class="dy-dlh-btn secondary" data-action="open">打开本地控制台</button>
      <div class="dy-dlh-status" data-role="status"></div>
    `;
    panel.querySelector('[data-action="download"]').addEventListener('click', onDownload);
    panel.querySelector('[data-action="import"]').addEventListener('click', onImport);
    panel.querySelector('[data-action="open"]').addEventListener('click', () => {
      window.open(DASHBOARD_URL, '_blank', 'noopener,noreferrer');
    });
    statusEl = panel.querySelector('[data-role="status"]');
    document.documentElement.appendChild(panel);
    return panel;
  }

  function togglePanel() {
    const p = ensurePanel();
    p.classList.toggle('dy-dlh-hidden');
    if (!p.classList.contains('dy-dlh-hidden')) refresh();
  }

  function currentContext() {
    const url = window.location.href || '';
    const downloadable = canDownload(url);
    const profileUrl = isProfilePage(url);
    const secUid = parseSecUid(url);
    const detailUrl = downloadable ? canonicalDetailUrl(url) : '';
    return { url, downloadable, profileUrl, secUid, detailUrl };
  }

  function refresh() {
    const panelEl = ensurePanel();
    const ctx = currentContext();
    const urlEl = panelEl.querySelector('[data-role="url"]');
    urlEl.textContent = ctx.url;
    const downloadBtn = panelEl.querySelector('[data-action="download"]');
    const importBtn = panelEl.querySelector('[data-action="import"]');
    downloadBtn.disabled = !ctx.downloadable;
    importBtn.disabled = !ctx.profileUrl;
    if (!ctx.downloadable && !ctx.profileUrl) {
      setStatus('当前页面不是抖音视频/笔记/主页。', 'error');
    } else {
      setStatus('');
    }
  }

  function setBusy(state) {
    busy = state;
    if (fab) fab.classList.toggle('dy-dlh-busy', state);
    if (panel) {
      panel.querySelectorAll('button').forEach((b) => {
        b.disabled = b.disabled || state;
      });
    }
  }

  // 所有本地后端调用走 background service worker,避开 HTTPS 页面的 mixed-content 限制。
  function apiCall(method, path, body) {
    return new Promise((resolve, reject) => {
      try {
        chrome.runtime.sendMessage(
          { type: 'dy_dlh_request', method, path, body },
          (response) => {
            const err = chrome.runtime.lastError;
            if (err) {
              reject(new Error(`扩展后台通信失败:${err.message || err}`));
              return;
            }
            if (!response) {
              reject(new Error('扩展后台无响应'));
              return;
            }
            if (!response.ok) {
              const error = new Error(response.error || '本地后端请求失败');
              error.status = response.status;
              reject(error);
              return;
            }
            resolve(response.data);
          },
        );
      } catch (error) {
        reject(new Error(`扩展后台调用异常:${error.message || error}`));
      }
    });
  }

  async function apiPost(path, body) {
    return apiCall('POST', path, body);
  }

  async function apiGet(path) {
    return apiCall('GET', path);
  }

  async function getSettings() {
    try {
      const data = await apiGet('/api/settings');
      return data.settings || {};
    } catch (error) {
      if (error.status === 404) return { download_output_dir: '', wrap_download_folder: false };
      throw error;
    }
  }

  async function extractProfileFromPage() {
    const title = document.title
      .replace(/ - 抖音$/, '')
      .replace(/的抖音主页.*$/, '')
      .trim();
    const nickname =
      document.querySelector('[data-e2e="user-title"]')?.textContent?.trim()
      || document.querySelector('h1')?.textContent?.trim()
      || title;
    const signature =
      document.querySelector('[data-e2e="user-signature"]')?.textContent?.trim()
      || document.querySelector('[class*="signature"]')?.textContent?.trim()
      || '';
    const avatarUrl =
      document.querySelector('img[src*="douyinpic"]')?.src
      || document.querySelector('img[src*="aweme-avatar"]')?.src
      || '';
    return { nickname, signature, avatar_url: avatarUrl };
  }

  async function onDownload() {
    const ctx = currentContext();
    if (!ctx.downloadable) {
      setStatus('当前页面不是可下载的抖音作品页。', 'error');
      return;
    }
    setBusy(true);
    setStatus('正在提交下载任务...');
    try {
      const settings = await getSettings();
      const data = await apiPost('/api/downloads', {
        text: ctx.detailUrl || ctx.url,
        mode: 1,
        output_dir: settings.download_output_dir || '',
        wrap_folder: Boolean(settings.wrap_download_folder),
        comments: false,
        selected_urls: [],
        selected_media: {},
      });
      setStatus(`已提交下载任务:${data.job?.id?.slice(0, 8) || ''}`, 'ok');
    } catch (error) {
      setStatus(error.message || '提交下载失败', 'error');
    } finally {
      setBusy(false);
    }
  }

  async function onImport() {
    const ctx = currentContext();
    if (!ctx.profileUrl) {
      setStatus('当前页面不是抖音个人主页。', 'error');
      return;
    }
    setBusy(true);
    setStatus('正在导入主页...');
    try {
      const profile = await extractProfileFromPage();
      const label = profile.nickname || ctx.secUid;
      await apiPost('/api/users', {
        label,
        sec_uid: ctx.secUid,
        homepage_url: ctx.url,
        nickname: profile.nickname || '',
        signature: profile.signature || '',
        avatar_url: profile.avatar_url || '',
      });
      setStatus(`已导入:${label}`, 'ok');
    } catch (error) {
      setStatus(error.message || '导入失败', 'error');
    } finally {
      setBusy(false);
    }
  }

  function bootstrap() {
    ensureFab();
    // 首次进入按需展开详情面板;保持"按钮常驻"避免挡住视频区。
    refresh();
  }

  if (document.readyState === 'complete' || document.readyState === 'interactive') {
    setTimeout(bootstrap, 600);
  } else {
    window.addEventListener('DOMContentLoaded', () => setTimeout(bootstrap, 600));
  }
})();
