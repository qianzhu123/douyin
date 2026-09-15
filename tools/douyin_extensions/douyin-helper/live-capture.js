// live-capture.js — MAIN world 注入脚本（manifest 里 run_at: document_start）。
//
// 为什么需要它：
//   直播间的 累计观看 / 当前观看 / 本场点赞 不在页面 DOM 里，而是页面自己
//   加载时 GET /webcast/room/web/enter/ 带回来的（stats.total_user_str 等，
//   带 a_bogus + msToken签名，content script 无法自签重放）。hook 页面世界
//   的 fetch / XMLHttpRequest，把这两个接口的响应原样 postMessage 给隔离
//   世界的 content.js；只读不拦截、不修改、不阻塞任何请求。
//
// 与 content.js 的桥接协议（window.postMessage，跨世界安全）：
//   MAIN -> ISOLATED: { source: 'dy-dlh-live', type: 'enter'|'ranklist', at, payload }
//   ISOLATED -> MAIN: { source: 'dy-dlh-live', type: 'request' }
//     content.js 通常晚于 enter 注入（content 是 document_idle），用缓存补发。
(function () {
  if (window.__dyDlhLiveCaptureHooked) return;
  window.__dyDlhLiveCaptureHooked = true;

  const KIND_ENTER = 'enter';
  const KIND_RANKLIST = 'ranklist';
  const MARKERS = {
    enter: '/webcast/room/web/enter/',
    ranklist: '/webcast/ranklist/audience/',
  };
  const cache = { enter: null, ranklist: null };

  function post(type, payload) {
    try {
      window.postMessage({ source: 'dy-dlh-live', type, at: Date.now(), payload }, '*');
    } catch (e) {
      /* 桥接失败不影响页面 */
    }
  }

  function pickKind(url) {
    const u = String(url || '');
    for (const kind of Object.keys(MARKERS)) {
      if (u.includes(MARKERS[kind])) return kind;
    }
    return '';
  }

  function onCapture(kind, url, bodyText) {
    let payload;
    try {
      payload = JSON.parse(bodyText);
    } catch (e) {
      return;
    }
    if (!payload || typeof payload !== 'object') return;
    cache[kind] = { at: Date.now(), url: String(url || ''), payload };
    post(kind, payload);
  }

  // ---- hook fetch ----
  const origFetch = window.fetch;
  if (typeof origFetch === 'function') {
    window.fetch = function (input, init) {
      const kind = pickKind(input && input.url ? input.url : input);
      const p = origFetch.apply(this, arguments);
      if (kind) {
        p.then((res) => {
          try {
            res.clone().text()
              .then((t) => onCapture(kind, res.url || input, t))
              .catch(() => {});
          } catch (e) {
            /* clone 不可用时静默放弃，不影响页面 */
          }
        }).catch(() => {});
      }
      return p;
    };
  }

  // ---- hook XMLHttpRequest ----
  const XHR = window.XMLHttpRequest;
  if (XHR && XHR.prototype) {
    const origOpen = XHR.prototype.open;
    XHR.prototype.open = function (method, url) {
      try {
        this.__dyDlhKind = pickKind(url);
        this.__dyDlhUrl = String(url || '');
      } catch (e) {
        /* ignore */
      }
      return origOpen.apply(this, arguments);
    };
    const origSend = XHR.prototype.send;
    XHR.prototype.send = function () {
      const xhr = this;
      if (xhr.__dyDlhKind) {
        xhr.addEventListener('load', () => {
          try {
            const text = xhr.responseType === 'json'
              ? JSON.stringify(xhr.response)
              : xhr.responseText;
            if (text) onCapture(xhr.__dyDlhKind, xhr.__dyDlhUrl, text);
          } catch (e) {
            /* ignore */
          }
        });
      }
      return origSend.apply(this, arguments);
    };
  }

  // content.js 晚启动（或面板重新打开）时，用缓存补发一次。
  window.addEventListener('message', (event) => {
    if (event.source !== window) return;
    const d = event.data;
    if (!d || d.source !== 'dy-dlh-live' || d.type !== 'request') return;
    for (const kind of [KIND_ENTER, KIND_RANKLIST]) {
      if (cache[kind]) post(kind, cache[kind].payload);
    }
  });
})();
