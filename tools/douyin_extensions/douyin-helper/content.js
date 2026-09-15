// content.js — Douyin Helper 单扩展 page-side 浮动按钮。
//
// 设计目标：
//   - 一个扩展搞定原 downloader + live-overlay 两个扩展的全部场景。
//   - content.js 在每个匹配页面（视频 / 笔记 / 直播 / 个人主页 / 短链）右上角
//     渲染一个悬浮按钮，根据 location 自动判定 "kind"，展开不同面板。
//   - 所有本地后端调用走 background service worker，避开 HTTPS 页面的
//     mixed-content 限制（chrome-extension:// 不受页面 CSP 约束）。
//
// 判定矩阵：
//   live.douyin.com/<web_rid>           →  kind=live         就地检测：房间/主播/观看/累计/点赞
//   www.douyin.com/user/<sec_uid>       →  kind=profile      导入主页 + 检测
//   www.douyin.com/(video|note)/<id>    →  kind=video        下载作品
//   www.douyin.com/... ?modal_id=...    →  kind=video        下载作品（聚合页）
//   v.douyin.com/...                    →  kind=video        下载作品（短链）
//   其它                                →  kind=other        仅显示当前位置
//
// 直播间数据源（见 live-capture.js / live_core.js）：
//   主：MAIN world hook 页面自身的 webcast/room/web/enter/ 响应——累计观看、
//       本场点赞根本不在 DOM 里，只在接口里；DOM 兜底只覆盖标题/主播/当前观看。
//   展示：累计观看达到 10万 后永远展示 "10万+"（与抖音页面封顶规则一致）。
//
// MV3 content_script 不支持 import/export，所以 _dyDlhCore 全部内联在此。

(function () {
  if (window.__dyDlhInjected) return;
  window.__dyDlhInjected = true;

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

    function isDouyin(url) {
      return !!url && /douyin\.com/.test(url);
    }

    function isLive(url) {
      try {
        const u = new URL(url);
        return /(^|\.)live\.douyin\.com$/.test(u.hostname);
      } catch (e) {
        return false;
      }
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
      // 个人主页特征：/user/<sec_uid> 而非 /user/self|/user/following
      if (!url) return false;
      const m = url.match(/\/user\/([^/?#]+)/);
      if (!m) return false;
      const seg = m[1];
      return seg && seg !== 'self' && seg !== 'following' && seg !== 'self_follower';
    }

    function classify(url) {
      if (isLive(url)) return 'live';
      if (!isDouyin(url)) return 'other';
      if (isProfilePage(url)) return 'profile';
      if (canDownload(url)) return 'video';
      return 'other';
    }

    return {
      classify,
      isLive,
      canDownload,
      isProfilePage,
      parseSecUid,
      canonicalDetailUrl,
    };
  })();

  const { classify, parseSecUid, canonicalDetailUrl } = _dyDlhCore;

  const API_BASE = 'http://127.0.0.1:8000';
  const DASHBOARD_URL = 'http://127.0.0.1:5175';

  const ICON_DOWNLOAD_SVG = `
<svg viewBox="0 0 24 24" aria-hidden="true">
  <path d="M12 3a1 1 0 0 1 1 1v9.586l2.293-2.293a1 1 0 0 1 1.414 1.414l-4 4a1 1 0 0 1-1.414 0l-4-4a1 1 0 0 1 1.414-1.414L11 13.586V4a1 1 0 0 1 1-1z"/>
  <path d="M5 18a1 1 0 0 1 1-1h12a1 1 0 1 1 0 2H6a1 1 0 0 1-1-1z"/>
</svg>`;

  let fab = null;
  let panel = null;
  let statusEl = null;
  let busy = false;

  function getApiBase() {
    try {
      return window.localStorage.getItem('dy_dlh_api_base') || API_BASE;
    } catch (e) {
      return API_BASE;
    }
  }

  function setStatus(message, kind) {
    if (!statusEl) return;
    statusEl.textContent = message || '';
    statusEl.className = 'dy-dlh-status' + (kind ? ' ' + kind : '');
  }

  function ensureFab() {
    if (fab) return fab;
    fab = document.createElement('button');
    fab.id = 'dy-dlh-fab';
    fab.className = 'dy-dlh-fab';
    fab.type = 'button';
    fab.title = 'Douyin Helper';
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
      <h4>抖音助手 <span class="dy-dlh-kind" data-role="kind">…</span></h4>
      <div class="dy-dlh-url" data-role="url"></div>
      <div class="dy-dlh-rows" data-role="rows"></div>
      <button class="dy-dlh-btn" data-action="primary">…</button>
      <div class="dy-dlh-divider"></div>
      <button class="dy-dlh-btn secondary" data-action="open">打开本地控制台</button>
      <button class="dy-dlh-btn secondary" data-action="restart">重启本地后端</button>
      <div class="dy-dlh-status" data-role="status"></div>
    `;
    panel.querySelector('[data-action="open"]').addEventListener('click', () => {
      window.open(DASHBOARD_URL, '_blank', 'noopener,noreferrer');
    });
    panel.querySelector('[data-action="restart"]').addEventListener('click', onRestartBackend);
    statusEl = panel.querySelector('[data-role="status"]');
    document.documentElement.appendChild(panel);
    return panel;
  }

  function togglePanel() {
    const p = ensurePanel();
    p.classList.toggle('dy-dlh-hidden');
    if (!p.classList.contains('dy-dlh-hidden')) refresh();
  }

  // ---------- 直播间页面检测（live.douyin.com/<web_rid>） ----------
  //
  // 数据源优先级：
  //   1. live-capture.js（MAIN world）捕获的 webcast/room/web/enter/ 响应
  //      ——权威值：标题/主播/当前观看/累计观看/本场点赞。
  //   2. DOM 兜底——选择器与文案随抖音改版易碎，只覆盖 标题/主播/当前观看。
  let capturedEnter = null; // { at, payload } 最后一次 enter 响应

  function liveCore() {
    return window.__dyDlhLiveCore || null;
  }

  function requestCaptureResend() {
    try {
      window.postMessage({ source: 'dy-dlh-live', type: 'request' }, '*');
    } catch (e) {
      /* MAIN world 未注入（如扩展刚装完未刷新）时无应答，走 DOM 兜底 */
    }
  }

  window.addEventListener('message', (event) => {
    if (event.source !== window) return;
    const d = event.data;
    if (!d || d.source !== 'dy-dlh-live' || d.type !== 'enter' || !d.payload) return;
    capturedEnter = { at: d.at || Date.now(), payload: d.payload };
    if (panel && !panel.classList.contains('dy-dlh-hidden')
        && classify(location.href) === 'live') {
      renderLive();
    }
  });

  function enterSummary() {
    const core = liveCore();
    if (!core || !capturedEnter) return null;
    const s = core.summarizeEnter(capturedEnter.payload);
    return s && s.ok ? s : null;
  }

  // ---- DOM 兜底 ----
  function readLiveDom() {
    const title =
      document.querySelector('[data-e2e="live-room-title"]')?.textContent?.trim()
      || document.querySelector('h1')?.textContent?.trim()
      || document.title.replace(/ - 抖音$/, '').trim()
      || '';
    // 注意 rooom-info-bar 是抖音自己的拼写错误（三个 o），后端同款选择器已验证。
    const anchor =
      document.querySelector('[data-e2e="live-room-nickname"]')?.textContent?.trim()
      || document.querySelector('[data-e2e="rooom-info-bar-anchor"] .user_name')?.textContent?.trim()
      || document.querySelector('[data-e2e="rooom-info-bar-anchor"] [class*="name"]')?.textContent?.trim()
      || document.querySelector('[class*="anchor-name"]')?.textContent?.trim()
      || document.querySelector('[class*="host-name"]')?.textContent?.trim()
      || '';
    return { title, anchor, viewers: findDomViewers() };
  }

  function findDomViewers() {
    const core = liveCore();
    const sels = [
      "[data-e2e='live-room-watching-count']",
      "[data-e2e='live-viewer-count']",
      "[data-e2e='room_room_user_count']",
      '.webcast-chatroom___watching-count',
      '.watching-count',
      '.audience-count',
      '.live-audience-count',
    ];
    for (const s of sels) {
      const el = document.querySelector(s);
      const parsed = el && core && core.parseCnCount(el.textContent);
      if (parsed && parsed.ok && parsed.num > 0) return parsed;
    }
    // 文案兜底：页面展示的 "1218在线观众" / "8.1万观众" 这类叶子节点。
    for (const n of document.querySelectorAll('span, div, p')) {
      if (n.childElementCount > 0) continue;
      const m = (n.textContent || '').trim()
        .match(/^([\d.,]+\s*[万亿]?\+?)\s*(?:在线)?(?:观众|观看|人气)$/);
      if (!m) continue;
      const parsed = core && core.parseCnCount(m[1]);
      if (parsed && parsed.ok && parsed.num > 0) return parsed;
    }
    return null;
  }

  // ---- 累计观看 10万+ 封顶标记（按 web_rid 记，本标签页会话内永久生效） ----
  const latchMem = Object.create(null);

  function latchKey(webRid) {
    return `dy_dlh_cap_total_${webRid || 'norid'}`;
  }

  function isLatched(webRid) {
    try {
      if (window.sessionStorage.getItem(latchKey(webRid)) === '1') return true;
    } catch (e) {
      /* sessionStorage 不可用时退化到内存 */
    }
    return latchMem[latchKey(webRid)] || false;
  }

  function setLatched(webRid) {
    try {
      window.sessionStorage.setItem(latchKey(webRid), '1');
    } catch (e) {
      /* ignore */
    }
    latchMem[latchKey(webRid)] = true;
  }

  function currentWebRid() {
    return location.pathname.replace(/^\//, '').split('/')[0] || '';
  }

  function renderLive() {
    const core = liveCore();
    const panelEl = ensurePanel();
    const rowsEl = panelEl.querySelector('[data-role="rows"]');
    const primary = panelEl.querySelector('[data-action="primary"]');
    const cap = enterSummary();
    const dom = readLiveDom();

    rowsEl.innerHTML = '';
    rowsEl.appendChild(rowEl('房间', cap?.title || dom.title || '直播间'));
    rowsEl.appendChild(rowEl('主播', cap?.anchor || dom.anchor || '--'));
    rowsEl.appendChild(rowEl('来源', cap ? '页面接口捕获' : '页面 DOM（接口未捕获）'));

    // 当前观看：接口优先，DOM 兜底。
    let viewersDisp = '--';
    const vParsed = (!cap?.viewersText && dom.viewers) ? dom.viewers : null;
    const vText = cap?.viewersText || (vParsed && vParsed.raw) || '';
    if (vText) {
      const parsed = vParsed || (core && core.parseCnCount(vText));
      viewersDisp = (core && core.formatCount(parsed, false)) || vText;
    }
    rowsEl.appendChild(rowEl('观看', viewersDisp));

    // 累计观看：仅接口可得；达到 10万 后永远展示 "10万+"。
    let totalDisp = '--';
    if (cap && cap.totalText && core) {
      const parsed = core.parseCnCount(cap.totalText);
      totalDisp = core.formatCount(parsed, isLatched(cap.web_rid || currentWebRid()))
        || cap.totalText;
      if (parsed.ok && parsed.num != null && parsed.num >= core.CAP_NUM) {
        setLatched(cap.web_rid || currentWebRid());
      }
    }
    rowsEl.appendChild(rowEl('累计', totalDisp));

    rowsEl.appendChild(rowEl('点赞', cap && cap.likeNum != null
      ? cap.likeNum.toLocaleString('en-US')
      : '--'));

    primary.textContent = '重新检测';
    primary.disabled = false;
    primary.onclick = () => {
      requestCaptureResend();
      renderLive();
      setStatus('已重新检测', 'ok');
    };
    if (!cap) {
      setStatus('接口数据未捕获：若是刚安装扩展，请刷新本页（F5）后再打开面板', 'error');
    } else {
      setStatus('');
    }
  }

  // ---------- 个人主页解析（www.douyin.com/user/<sec_uid>） ----------
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
    const followersText =
      document.querySelector('[data-e2e="user-info-follow"]')?.textContent
      || document.querySelector('[class*="follower"]')?.textContent
      || '';
    const ipText =
      document.querySelector('[data-e2e="user-info-ip"]')?.textContent
      || '';
    return {
      nickname,
      signature,
      avatar_url: avatarUrl,
      follower_text: followersText,
      ip_location: ipText,
    };
  }

  // ---------- 后端调用（统一走 background） ----------
  function apiCall(method, path, body) {
    return new Promise((resolve, reject) => {
      try {
        chrome.runtime.sendMessage(
          { type: 'dy_dlh_request', method, path, body },
          (response) => {
            const err = chrome.runtime.lastError;
            if (err) {
              reject(new Error(`扩展后台通信失败：${err.message || err}`));
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
        reject(new Error(`扩展后台调用异常：${error.message || error}`));
      }
    });
  }
  const apiPost = (path, body) => apiCall('POST', path, body);
  const apiGet = (path) => apiCall('GET', path);

  async function getSettings() {
    try {
      const data = await apiGet('/api/settings');
      return data.settings || {};
    } catch (error) {
      if (error.status === 404) return { download_output_dir: '', wrap_download_folder: false };
      throw error;
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

  // ---------- 渲染 ----------
  function refresh() {
    const panelEl = ensurePanel();
    const url = window.location.href || '';
    const kind = classify(url);
    const kindEl = panelEl.querySelector('[data-role="kind"]');
    const urlEl = panelEl.querySelector('[data-role="url"]');
    const rowsEl = panelEl.querySelector('[data-role="rows"]');
    const primary = panelEl.querySelector('[data-action="primary"]');
    if (kindEl) kindEl.textContent = labelKind(kind);
    if (urlEl) urlEl.textContent = url;
    if (rowsEl) rowsEl.innerHTML = '';
    primary.onclick = null;
    primary.textContent = '…';
    primary.disabled = true;

    if (kind === 'live') {
      renderLive();
    } else if (kind === 'profile') {
      extractProfileFromPage().then((p) => {
        rowsEl.appendChild(rowEl('昵称', p.nickname || '--'));
        rowsEl.appendChild(rowEl('签名', (p.signature || '').slice(0, 60) || '--'));
        rowsEl.appendChild(rowEl('IP', p.ip_location || '--'));
        primary.textContent = '导入到本地账户列表';
        primary.disabled = false;
        primary.onclick = () => onImportProfile(url);
      }).catch(() => {
        primary.textContent = '导入当前主页';
        primary.disabled = false;
        primary.onclick = () => onImportProfile(url);
      });
      setStatus('');
    } else if (kind === 'video') {
      const detail = canonicalDetailUrl(url);
      rowsEl.appendChild(rowEl('类型', detail.includes('/note/') ? '笔记' : '视频'));
      rowsEl.appendChild(rowEl('作品', detail));
      primary.textContent = '提交到本地下载';
      primary.disabled = false;
      primary.onclick = () => onDownload(detail);
      setStatus('');
    } else {
      rowsEl.appendChild(rowEl('提示', '当前页面不是抖音视频 / 笔记 / 直播间 / 个人主页。'));
      primary.disabled = true;
      setStatus('当前页面无匹配操作', 'error');
    }
  }

  function rowEl(k, v) {
    const el = document.createElement('div');
    el.className = 'dy-dlh-row';
    el.innerHTML = `<span>${escapeHtml(String(k || ''))}</span><strong>${escapeHtml(String(v || '--'))}</strong>`;
    return el;
  }

  function labelKind(kind) {
    return ({
      video: '视频 / 笔记',
      live: '直播间',
      profile: '个人主页',
      other: '其它',
    })[kind] || '其它';
  }

  function escapeHtml(text) {
    return text
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // ---------- 主操作 ----------
  async function onDownload(detailUrl) {
    setBusy(true);
    setStatus('正在提交下载任务…');
    try {
      const settings = await getSettings();
      const data = await apiPost('/api/downloads', {
        text: detailUrl,
        mode: 1,
        output_dir: settings.download_output_dir || '',
        wrap_folder: Boolean(settings.wrap_download_folder),
        comments: false,
        selected_urls: [],
        selected_media: {},
      });
      setStatus(`已提交下载任务：${data.job?.id?.slice(0, 8) || ''}`, 'ok');
    } catch (error) {
      setStatus(error.message || '提交下载失败', 'error');
    } finally {
      setBusy(false);
    }
  }

  async function onImportProfile(url) {
    setBusy(true);
    setStatus('正在解析主页字段并导入…');
    try {
      const secUid = parseSecUid(url);
      const profile = await extractProfileFromPage().catch(() => ({}));
      const label = profile.nickname || secUid;
      await apiPost('/api/users', {
        label,
        sec_uid: secUid,
        homepage_url: url,
        nickname: profile.nickname || '',
        signature: profile.signature || '',
        avatar_url: profile.avatar_url || '',
        ip_location: profile.ip_location || '',
      });
      setStatus(`已导入主页：${label}`, 'ok');
    } catch (error) {
      setStatus(error.message || '导入失败', 'error');
    } finally {
      setBusy(false);
    }
  }

  async function onRestartBackend() {
    setBusy(true);
    setStatus('正在请求重启本地后端（8000）…');
    try {
      await apiPost('/api/admin/restart-backend', {});
      setStatus('已发送重启请求；预计 1-3 秒后端会失联重启，控制台刷新后继续使用。', 'ok');
      setTimeout(() => window.open(DASHBOARD_URL, '_blank', 'noopener,noreferrer'), 400);
    } catch (error) {
      setStatus(error.message || '重启请求失败', 'error');
    } finally {
      setBusy(false);
    }
  }

  // ---------- 启动 ----------
  function bootstrap() {
    ensureFab();
    refresh();
    // MAIN world 的 enter 捕获通常早于本脚本（document_idle），主动要一次缓存。
    requestCaptureResend();
    // 直播间面板打开期间每 3s 就地刷新一次（当前观看会动；累计 10万+ 后恒显封顶值）。
    setInterval(() => {
      if (!panel || panel.classList.contains('dy-dlh-hidden')) return;
      if (classify(location.href) !== 'live') return;
      renderLive();
    }, 3000);
  }

  if (document.readyState === 'complete' || document.readyState === 'interactive') {
    setTimeout(bootstrap, 600);
  } else {
    window.addEventListener('DOMContentLoaded', () => setTimeout(bootstrap, 600));
  }

  // SPA 路由变化时（抖音 web 用了 History API）重新判定 kind。
  let lastUrl = location.href;
  setInterval(() => {
    if (location.href !== lastUrl) {
      lastUrl = location.href;
      refresh();
    }
  }, 1000);
})();
