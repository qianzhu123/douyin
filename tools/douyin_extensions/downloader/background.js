// background.js — service worker,代理 content.js 的 API 请求。
//
// 为什么需要它:
// - MV3 content_script 跑在页面同源 (https://www.douyin.com) 的隔离上下文里。
// - 从 HTTPS 页面向 http://127.0.0.1:8000 发 fetch 会被浏览器当作 mixed-content 直接 block,
//   错误统一表现为 "TypeError: Failed to fetch"。仅在 manifest 加 host_permissions 无法绕过。
// - service worker 在扩展自己的 origin (chrome-extension://...) 里发起 fetch,
//   不受页面 mixed-content 限制, 也不再需要后端配 CORS。
//
// 消息协议:
//   { type: 'dy_dlh_request', method, path, body? }
//   -> { ok: true, data } | { ok: false, error, status }

const API_BASE_DEFAULT = 'http://127.0.0.1:8000';

async function getApiBase() {
  const stored = await chrome.storage.local.get('dy_dlh_api_base').catch(() => ({}));
  return stored.dy_dlh_api_base || API_BASE_DEFAULT;
}

async function handleRequest(message) {
  const { method = 'GET', path = '', body } = message;
  if (!path.startsWith('/')) {
    return { ok: false, error: 'path 必须以 / 开头' };
  }
  const apiBase = await getApiBase();
  const url = `${apiBase}${path}`;
  const init = {
    method,
    headers: { 'Content-Type': 'application/json' },
  };
  if (body !== undefined && method !== 'GET' && method !== 'HEAD') {
    init.body = typeof body === 'string' ? body : JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(url, init);
  } catch (error) {
    return { ok: false, error: `本地后端连接失败:${error.message || error}` };
  }
  const text = await response.text();
  let data;
  try {
    data = text ? JSON.parse(text) : {};
  } catch (error) {
    return {
      ok: false,
      status: response.status,
      error: `本地后端返回非 JSON:${text.slice(0, 200)}`,
    };
  }
  if (!response.ok) {
    return {
      ok: false,
      status: response.status,
      error: data.detail || `本地后端请求失败:${response.status}`,
    };
  }
  return { ok: true, data };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || message.type !== 'dy_dlh_request') return false;
  handleRequest(message).then(sendResponse);
  return true; // 异步响应需要保持通道开放
});
