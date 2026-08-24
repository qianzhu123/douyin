// 抖音直播间人数悬浮窗 — content script
// 设计要点:
//  - 在直播间右上角展示一个圆形按钮,显示当前在线人数。
//  - 点击按钮展开详情面板,展示房间标题、主播、累计观看、点赞、跳转本地控制台。
//  - 默认每 30 秒刷新一次。位置/字号持久化到 localStorage。
//  - 数据抓取策略沿用原版:已知 selector 优先,再走文本扫描与"较大整数"兜底。

(function () {
  'use strict';

  if (window.__dyLvInjected) return;
  window.__dyLvInjected = true;

  const REFRESH_MS = 30 * 1000;
  const STORE_KEY = 'dy_lv_state_v1';
  const DEFAULTS = { left: null, top: 96, right: 24, width: null, height: null, fontSize: 13 };
  const DASHBOARD_URL = 'http://127.0.0.1:5175';

  let state = { ...DEFAULTS };

  // ---------- DOM 抓取 ----------
  function toNum(s) {
    if (s == null) return null;
    const m = String(s).match(/[\d,]+/);
    if (!m) return null;
    const n = parseInt(m[0].replace(/,/g, ''), 10);
    return Number.isFinite(n) ? n : null;
  }

  function findByText() {
    const nodes = document.querySelectorAll('span, div, p');
    for (const n of nodes) {
      const t = (n.textContent || '').trim();
      const m = t.match(/^([\d,]+)\s*(观看|人气|在看|在线|人)?$/);
      if (m) {
        const v = toNum(m[1]);
        if (v != null && v > 0) return v;
      }
    }
    return null;
  }

  function findByKnownSelectors() {
    const sels = [
      "[data-e2e='live-room-watching-count']",
      "[data-e2e='live-viewer-count']",
      '.webcast-chatroom___watching-count',
      '.watching-count',
      '.audience-count',
      '.live-audience-count',
    ];
    for (const s of sels) {
      const el = document.querySelector(s);
      if (el) {
        const n = toNum(el.textContent);
        if (n != null && n > 0) return n;
      }
    }
    return null;
  }

  function findByIntlNumber() {
    const candidates = [];
    const nodes = document.querySelectorAll('span, div, p, strong, b');
    for (const n of nodes) {
      if (n.children.length === 0) {
        const v = toNum(n.textContent);
        if (v != null && v > 50) candidates.push(v);
      }
    }
    return candidates.length ? Math.max(...candidates) : null;
  }

  function getViewers() {
    return findByKnownSelectors() || findByText() || findByIntlNumber();
  }

  // ---------- 房间元数据(给详情面板) ----------
  function getRoomMeta() {
    const title =
      document.querySelector('[data-e2e="live-room-title"]')?.textContent?.trim()
      || document.querySelector('h1')?.textContent?.trim()
      || document.title.replace(/ - 抖音$/, '').trim()
      || '';
    const anchor =
      document.querySelector('[data-e2e="live-room-anchor-name"]')?.textContent?.trim()
      || document.querySelector('[class*="anchor-name"]')?.textContent?.trim()
      || document.querySelector('[class*="host-name"]')?.textContent?.trim()
      || '';
    const likeText =
      document.querySelector('[data-e2e="live-room-like-count"]')?.textContent
      || document.querySelector('[class*="like-count"]')?.textContent
      || '';
    const totalText =
      document.querySelector('[data-e2e="live-room-total-user"]')?.textContent
      || document.querySelector('[class*="total-user"]')?.textContent
      || '';
    return {
      title,
      anchor,
      like_count: toNum(likeText),
      total_user: toNum(totalText),
      web_rid: location.pathname.replace(/^\//, '').split('/')[0] || '',
      url: location.href,
    };
  }

  // ---------- UI ----------
  let fab, numEl, panel, panelTitle, panelAnchor, panelLike, panelTotal;

  function saveState() {
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify(state));
    } catch (e) {
      /* ignore */
    }
  }

  function loadState() {
    try {
      const raw = localStorage.getItem(STORE_KEY);
      if (raw) state = { ...DEFAULTS, ...JSON.parse(raw) };
    } catch (e) {
      /* ignore */
    }
  }

  function applyPos() {
    if (!fab) return;
    if (state.left != null) {
      fab.style.left = state.left + 'px';
      fab.style.right = 'auto';
    } else if (state.right != null) {
      fab.style.right = state.right + 'px';
      fab.style.left = 'auto';
    }
    fab.style.top = state.top + 'px';
    if (state.width != null) fab.style.width = state.width + 'px';
    if (state.height != null) fab.style.height = state.height + 'px';
    if (state.fontSize) {
      fab.style.fontSize = state.fontSize + 'px';
    }
  }

  function render(n) {
    if (!numEl) return;
    if (n == null || n <= 0) {
      numEl.classList.add('dy-lv-err');
      numEl.textContent = '--';
    } else {
      numEl.classList.remove('dy-lv-err');
      numEl.textContent = n.toLocaleString();
    }
  }

  function refresh() {
    try {
      render(getViewers());
      if (panel && !panel.classList.contains('dy-lv-hidden')) {
        populatePanel();
      }
    } catch (e) {
      render(null);
    }
  }

  function buildUI() {
    fab = document.createElement('button');
    fab.id = 'dy-lv-fab';
    fab.className = 'dy-lv-fab';
    fab.type = 'button';
    fab.title = '点击查看直播间详情';
    fab.innerHTML = `
      <svg class="dy-lv-eye" viewBox="0 0 24 24" aria-hidden="true">
        <path d="M12 5C7 5 2.73 8.11 1 12c1.73 3.89 6 7 11 7s9.27-3.11 11-7c-1.73-3.89-6-7-11-7zm0 12a5 5 0 1 1 0-10 5 5 0 0 1 0 10zm0-8a3 3 0 1 0 0 6 3 3 0 0 0 0-6z"/>
      </svg>
      <span class="dy-lv-num dy-lv-err">--</span>
    `;
    numEl = fab.querySelector('.dy-lv-num');
    fab.addEventListener('click', togglePanel);
    document.documentElement.appendChild(fab);

    panel = document.createElement('div');
    panel.id = 'dy-lv-panel';
    panel.className = 'dy-lv-panel dy-lv-hidden';
    panel.innerHTML = `
      <h4>直播间详情</h4>
      <div class="dy-lv-meta" data-role="title">加载中...</div>
      <div class="dy-lv-row"><span>主播</span><span data-role="anchor">--</span></div>
      <div class="dy-lv-row"><span>当前观看</span><span data-role="viewers">--</span></div>
      <div class="dy-lv-row"><span>累计观看</span><span data-role="total">--</span></div>
      <div class="dy-lv-row"><span>点赞</span><span data-role="like">--</span></div>
      <button class="dy-lv-btn" data-action="open">打开本地控制台</button>
      <button class="dy-lv-btn" data-action="refresh">立即刷新</button>
    `;
    panelTitle = panel.querySelector('[data-role="title"]');
    panelAnchor = panel.querySelector('[data-role="anchor"]');
    panelLike = panel.querySelector('[data-role="like"]');
    panelTotal = panel.querySelector('[data-role="total"]');
    panel.querySelector('[data-action="open"]').addEventListener('click', () => {
      window.open(DASHBOARD_URL, '_blank', 'noopener,noreferrer');
    });
    panel.querySelector('[data-action="refresh"]').addEventListener('click', refresh);
    document.documentElement.appendChild(panel);

    loadState();
    applyPos();
    bindDrag();
  }

  function populatePanel() {
    const meta = getRoomMeta();
    if (panelTitle) panelTitle.textContent = meta.title || meta.url || '直播间';
    if (panelAnchor) panelAnchor.textContent = meta.anchor || '--';
    if (panelLike) panelLike.textContent = meta.like_count != null ? meta.like_count.toLocaleString() : '--';
    if (panelTotal) panelTotal.textContent = meta.total_user != null ? meta.total_user.toLocaleString() : '--';
    const viewersEl = panel.querySelector('[data-role="viewers"]');
    if (viewersEl) {
      const v = getViewers();
      viewersEl.textContent = v != null ? v.toLocaleString() : '--';
    }
  }

  function togglePanel() {
    if (!panel) return;
    panel.classList.toggle('dy-lv-hidden');
    if (!panel.classList.contains('dy-lv-hidden')) populatePanel();
  }

  // ---------- 拖动 ----------
  function bindDrag() {
    let dragging = false, sx, sy, sl, st;
    fab.addEventListener('mousedown', (e) => {
      dragging = true;
      sx = e.clientX;
      sy = e.clientY;
      const r = fab.getBoundingClientRect();
      sl = r.left;
      st = r.top;
      state.left = sl;
      state.right = null;
      state.top = st;
      e.preventDefault();
    });
    window.addEventListener('mousemove', (e) => {
      if (!dragging) return;
      state.left = Math.max(0, sl + (e.clientX - sx));
      state.top = Math.max(0, st + (e.clientY - sy));
      fab.style.left = state.left + 'px';
      fab.style.right = 'auto';
      fab.style.top = state.top + 'px';
    });
    window.addEventListener('mouseup', () => {
      if (dragging) {
        dragging = false;
        saveState();
      }
    });
  }

  // ---------- 启动 ----------
  function start() {
    buildUI();
    refresh();
    setInterval(refresh, REFRESH_MS);
  }

  if (document.readyState === 'complete' || document.readyState === 'interactive') {
    setTimeout(start, 1500);
  } else {
    window.addEventListener('DOMContentLoaded', () => setTimeout(start, 1500));
  }
})();
