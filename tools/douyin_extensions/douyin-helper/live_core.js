// live_core.js — 直播间检测的纯函数（无 DOM 依赖，node 可直接 require 测试）。
//
// manifest 以 ["live_core.js", "content.js"] 顺序注入同一隔离世界，
// content.js 通过 window.__dyDlhLiveCore 取用；字段对照见 live_core.test.js。
(function (root) {
  'use strict';

  // “10万+”封顶阈值：与抖音对累计观看的展示规则对齐——达到 10万 后
  // 页面只给 "10万+"，不再给具体数字，展示端同样封顶。
  const CAP_NUM = 100000;

  // 把 "1,234" / "8.1万" / "10万+" / "3.4亿" 解析为 { ok, raw, num, capped, scale }。
  // capped 表示源文本自带 "+"（页面已封顶展示）。
  function parseCnCount(input) {
    const raw = String(input == null ? '' : input).trim();
    const capped = raw.includes('+');
    const compact = raw.replace(/[,，\s_]/g, '');
    const m = compact.match(/^([\d.]+)(万|亿)?/);
    if (!m) return { ok: false, raw, num: null, capped, scale: '' };
    const n = parseFloat(m[1]);
    if (!Number.isFinite(n)) return { ok: false, raw, num: null, capped, scale: '' };
    const scale = m[2] || '';
    let num = n;
    if (scale === '万') num = n * 1e4;
    else if (scale === '亿') num = n * 1e8;
    return { ok: true, raw, num: Math.round(num), capped, scale };
  }

  // 展示规则：
  //   亿级照抄页面（"3.4亿"/"1.2亿+"）；≥10万（或 latched=true 表示此前
  //   已到过 10万）一律展示 "10万+"，不展示具体数字；10万 以下整数补
  //   千分位，带万单位的短文案（"8.1万"）原样照抄。
  function formatCount(parsed, latched) {
    if (!parsed || !parsed.ok || parsed.num == null) return null;
    if (parsed.scale === '亿') return parsed.raw;
    if (parsed.num >= 1e8) return (Math.round(parsed.num / 1e7) / 10) + '亿+';
    if (latched || parsed.num >= CAP_NUM) return '10万+';
    if (parsed.scale === '万' || parsed.capped) return parsed.raw;
    return parsed.num.toLocaleString('en-US');
  }

  // 归一 enter 响应（字段对照 tests/live_room_analysis/samples/dy_enter_3587.json）：
  //   data.data[0].stats.user_count_str           当前观看 ("1218")
  //   data.data[0].room_view_stats.display_short  当前观看兜底 ("1218")
  //   data.data[0].stats.total_user_str           累计观看 ("3万+" / "10万+")
  //   data.data[0].like_count                     本场点赞 (62610)
  //   data.data[0].owner / data.user              主播昵称
  //   data.data[0].status                         2=直播中
  function summarizeEnter(payload) {
    const data = (payload && payload.data) || {};
    let room = data.data;
    if (Array.isArray(room)) room = room[0] || {};
    if (!room || typeof room !== 'object') room = {};
    const stats = room.stats || {};
    const viewStats = room.room_view_stats || {};
    const owner = room.owner || {};
    const user = data.user || {};
    const like = typeof room.like_count === 'number' && room.like_count > 0
      ? room.like_count
      : (typeof stats.like_count === 'number' && stats.like_count > 0 ? stats.like_count : null);
    return {
      ok: !!(room.title || owner.nickname || user.nickname || room.id_str),
      status: typeof room.status === 'number' ? room.status : null,
      title: room.title || '',
      anchor: user.nickname || owner.nickname || '',
      viewersText: stats.user_count_str || viewStats.display_short || room.user_count_str || '',
      totalText: stats.total_user_str || '',
      likeNum: like,
      web_rid: String(data.web_rid || room.web_rid || ''),
      room_id_str: String(room.id_str || data.enter_room_id || ''),
    };
  }

  const api = { CAP_NUM, parseCnCount, formatCount, summarizeEnter };
  root.__dyDlhLiveCore = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
