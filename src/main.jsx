import React from 'react';
import { createRoot } from 'react-dom/client';
import { Activity, Download, ExternalLink, FileSearch, FileUp, Loader2, Plus, RefreshCcw, RotateCw, Search, Square, Trash2 } from 'lucide-react';
import { formatDuration } from './duration.js';
import { buildDownloadInputText, mergeDownloadPreviews, removeDownloadPreviewItem } from './downloadPreviewState.js';
import './styles.css';

// 把后端 progress 步骤拼成可滚动文本，挂在 message 上方。
// 空数组直接返回原 message。
function progressSummary(progress) {
  if (!progress || !Array.isArray(progress.steps) || progress.steps.length === 0) return '';
  return progress.steps
    .map((step) => {
      const tag = step.status === 'error' ? '✗' : step.status === 'running' ? '…' : step.status === 'done' ? '✓' : '·';
      return `${tag} ${step.message || step.step}`;
    })
    .join('\n');
}

function extractDetailFromError(error) {
  // 兼容后端 {detail: {error, progress}} 形态——把 progress 透出来。
  try {
    const msg = error && error.message ? error.message : String(error || '');
    return msg;
  } catch (e) {
    return String(error || '');
  }
}

const API_BASE = 'http://127.0.0.1:8000';

function formatNumber(value) {
  if (value === null || value === undefined || value === '') return '-';
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return String(value);
  if (numeric >= 100000000) return `${(numeric / 100000000).toFixed(1)}亿`;
  if (numeric >= 10000) return `${(numeric / 10000).toFixed(1)}万`;
  return numeric.toLocaleString();
}

// 检测/签到时刻展示: MM-DD HH:mm (带日期不带年份, 用户指定 2026-11-04)
function fmtDayTime(ms) {
  const d = new Date(ms);
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  const hh = String(d.getHours()).padStart(2, '0');
  const mi = String(d.getMinutes()).padStart(2, '0');
  return `${mm}-${dd} ${hh}:${mi}`;
}

function formatTime(value) {
  if (!value) return '-';
  return String(value).replace('T', ' ');
}

function localNowIso() {
  const now = new Date();
  const pad = (value) => String(value).padStart(2, '0');
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}T${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
}

function mergeAccount(account) {
  // watch 轮询已移除, 账户数据只来自 profile_cache (monitor/手动检测/监控回写)
  const profile = account.last_profile || {};
  return { ...account, profile, last_checked_at: account.last_checked_at };
}

// ---- 粉丝团签到面板 (PC 直连, 凭证来自 data/fansclub_tokens/) ----
// ---- 左侧快速导航 (用户指定 2026-11-04: 项目内容过多, 侧边栏跳转) ----
const NAV_ITEMS = [
  { id: 'sec-accounts', label: '账户与直播' },
  { id: 'sec-fansclub', label: '粉丝团' },
  { id: 'sec-posts', label: '作品/评论' },
  { id: 'sec-download', label: '视频下载' },
  { id: 'sec-logs', label: '日志' },
];

function SideNav({ active, onActive }) {
  React.useEffect(() => {
    const onScroll = () => {
      // 以"最后一个滚过视口上沿 1/3 的 section"为当前项
      let cur = '';
      for (const item of NAV_ITEMS) {
        const el = document.getElementById(item.id);
        if (el && el.getBoundingClientRect().top < window.innerHeight / 3) cur = item.id;
      }
      onActive(cur);
    };
    onScroll();
    window.addEventListener('scroll', onScroll, { passive: true });
    return () => window.removeEventListener('scroll', onScroll);
  }, [onActive]);
  const jump = (id) => {
    const el = document.getElementById(id);
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };
  return (
    <nav className="side-nav">
      {NAV_ITEMS.map((item) => (
        <button
          key={item.id}
          className={`side-nav-item${active === item.id ? ' active' : ''}`}
          onClick={() => jump(item.id)}
        >
          {item.label}
        </button>
      ))}
    </nav>
  );
}

function FansClubPanel({ apiRef }) {
  const [accounts, setAccounts] = React.useState([]);
  const [selected, setSelected] = React.useState('');
  // 各账户检查结果缓存: {label: overview} — 切换不丢, 刷新也不丢(localStorage), 手动「检查」才更新
  const [overviewMap, setOverviewMap] = React.useState(() => {
    try { return JSON.parse(localStorage.getItem('fansclub_overview') || '{}'); } catch { return {}; }
  });
  const [loadingLabel, setLoadingLabel] = React.useState(''); // 正在检查的账户(单个), 避免切账户误显检查中
  const [error, setError] = React.useState('');
  // 今日亲密度基线: light_up_list 的 increased_intimacy_today 是死值(PC直连不同步),
  // 改为前端自算: 当天首次检查记基线, 之后显示 当前-基线
  const [baseMap, setBaseMap] = React.useState(() => {
    try { return JSON.parse(localStorage.getItem('fansclub_today_base') || '{}'); } catch { return {}; }
  });
  const [signJobs, setSignJobs] = React.useState([]);
  const [signingAll, setSigningAll] = React.useState(false);
  // 向顶栏暴露动作与状态 (用户指定 2026-11-04: 滚到粉丝团区域时顶栏按钮组切换为粉丝团操作)
  React.useEffect(() => {
    if (!apiRef) return undefined;
    apiRef.current = {
      loadAccounts,
      checkAllNow: async () => {
        try {
          const r = await fetch(`${API_BASE}/api/fansclub/check-all`, { method: 'POST' });
          const d = await r.json();
          if (d.started) setCheckAll({ running: true, done_labels: [], labels: [] });
          else if (d.reason === 'already running') setCheckAll(d.state);
          else setError(d.reason || '未能启动');
        } catch (e) { setError(e.message); }
      },
      signAll,
      state: { signingAll, checkAllRunning: !!checkAll?.running, accounts: accounts.length },
    };
    return () => { apiRef.current = null; };
  });

  const loadAccounts = React.useCallback(async () => {
    try {
      const r = await fetch(`${API_BASE}/api/fansclub/accounts`);
      const d = await r.json();
      setAccounts(d.accounts || []);
      setSelected((prev) => (d.accounts?.some((a) => a.label === prev) ? prev : (d.accounts?.[0]?.label || '')));
    } catch (e) {
      setError('加载账户失败: ' + e.message);
    }
  }, []);

  const loadOverview = React.useCallback(async (label) => {
    if (!label) return;
    setLoadingLabel(label);
    setError('');
    try {
      const r = await fetch(`${API_BASE}/api/fansclub/overview?label=${encodeURIComponent(label)}`);
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        throw new Error(d.detail || r.statusText);
      }
      const d = await r.json();
      // 今日基线 (增量数据源): 每团独立, 当天还没有该团基线才记
      const todayKey = new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 10); // 北京日期(UTC+8), 裸toISOString在早8点前算成昨天
      setBaseMap((bm) => {
        const day = bm[todayKey] || {};
        const base = { ...(day[label] || {}) };
        let added = false;
        (d.clubs || []).forEach((c) => {
          if (base[c.anchor_id] == null) { base[c.anchor_id] = c.intimacy || 0; added = true; }
        });
        if (!added) return bm;
        const next = { ...bm, [todayKey]: { ...day, [label]: base } };
        try { localStorage.setItem('fansclub_today_base', JSON.stringify(next)); } catch { /* 忽略 */ }
        return next;
      });
      setOverviewMap((m) => {
        const next = { ...m, [label]: { ...d, checked_at: Date.now() } };
        try { localStorage.setItem('fansclub_overview', JSON.stringify(next)); } catch { /* 忽略容量异常 */ }
        return next;
      });
    } catch (e) {
      setError(e.message);
      setOverviewMap((m) => {
        const next = { ...m, [label]: null };
        try { localStorage.setItem('fansclub_overview', JSON.stringify(next)); } catch { /* 忽略 */ }
        return next;
      });
    } finally {
      setLoadingLabel('');
    }
  }, []);

  React.useEffect(() => { loadAccounts(); }, [loadAccounts]);
  // 挂载时同步后端落盘缓存 (断链修复: 定时检测在页面没开时也把结果存后端了,
  // 打开页面取最新份覆盖比它旧的本地缓存; 同步时同时记今日基线, 否则
  // "今日亲密度"列因无基线显示 '-' — 后端 0 点检测的数据同样构成基线)
  React.useEffect(() => {
    (async () => {
      try {
        const r = await fetch(`${API_BASE}/api/fansclub/overview-cache`);
        if (!r.ok) return;
        const d = await r.json();
        const cache = d.cache || {};
        const bjToday = new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 10);
        setBaseMap((bm) => {
          // 每个账户: 当天还没有该账户基线时, 用后端缓存结果记基线
          const day = bm[bjToday] || {};
          let added = false;
          const nextDay = { ...day };
          for (const [label, ov] of Object.entries(cache)) {
            if (!ov || !ov.ok || nextDay[label]) continue;
            const base = {};
            (ov.clubs || []).forEach((c) => { base[c.anchor_id] = c.intimacy || 0; });
            nextDay[label] = base;
            added = true;
          }
          if (!added) return bm;
          const next = { ...bm, [bjToday]: nextDay };
          try { localStorage.setItem('fansclub_today_base', JSON.stringify(next)); } catch { /* 忽略 */ }
          return next;
        });
        setOverviewMap((m) => {
          let changed = false;
          const next = { ...m };
          for (const [label, ov] of Object.entries(cache)) {
            if (!ov || !ov.ok) continue;
            const cur = m[label];
            // 后端缓存更新才覆盖 (checked_at 后端存的是秒, 前端是毫秒)
            // 注意: 即使后端较旧也要合并 days (亲密度历史归档只存在后端, 前端本地无此数据)
            const backendAt = (ov.checked_at || 0) * (ov.checked_at < 1e12 ? 1000 : 1);
            if (!cur || (cur.checked_at || 0) < backendAt) {
              next[label] = { ...ov, checked_at: backendAt || Date.now() };
              changed = true;
            } else if (ov.days && (!cur.days || Object.keys(ov.days).length >= Object.keys(cur.days).length)) {
              // 后端较旧但带 days 归档: 只补历史, 其余字段不动
              next[label] = { ...cur, days: ov.days };
              changed = true;
            }
          }
          if (changed) {
            try { localStorage.setItem('fansclub_overview', JSON.stringify(next)); } catch { /* 忽略 */ }
            return next;
          }
          return m;
        });
      } catch { /* ignore */ }
    })();
  }, []);
  // 切换账户不自动拉取也不清缓存: 已检查过的账户随时切回查看, 「检查」才更新

  // ---- 每天 0 点后首次见到时, 自动全量检查一遍 (后台串行, 前端只触发+轮询) ----
  const [checkAll, setCheckAll] = React.useState(null); // 后端 check-all 状态
  const autoCheckRef = React.useRef('');
  React.useEffect(() => {
    const tick = async () => {
      const todayKey = new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 10); // 北京日期(UTC+8), 裸toISOString在早8点前算成昨天
      const flagKey = 'fansclub_autocheck_day';
      let lastDay = '';
      try { lastDay = localStorage.getItem(flagKey) || ''; } catch { /* ignore */ }
      if (lastDay === todayKey || autoCheckRef.current === todayKey) return;
      autoCheckRef.current = todayKey; // 防并发重复触发
      try {
        const r = await fetch(`${API_BASE}/api/fansclub/check-all`, { method: 'POST' });
        const d = await r.json();
        if (d.started || d.reason === 'already running') {
          try { localStorage.setItem(flagKey, todayKey); } catch { /* ignore */ }
        } else {
          autoCheckRef.current = ''; // 没触发成功, 允许下个周期再试
        }
      } catch { autoCheckRef.current = ''; }
    };
    tick(); // 打开页面先判一次 (0点后第一次打开补跑)
    const t = setInterval(tick, 60 * 1000); // 每分钟看一次是否跨了 0 点
    return () => clearInterval(t);
  }, []);

  // 全量检查进行中每 3s 轮询状态; 结束后把本轮账户结果写进缓存
  const fetchOverviewSilent = React.useCallback(async (label) => {
    try {
      const r = await fetch(`${API_BASE}/api/fansclub/overview?label=${encodeURIComponent(label)}`);
      if (!r.ok) return;
      const d = await r.json();
      const todayKey = new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 10); // 北京日期(UTC+8), 裸toISOString在早8点前算成昨天
      setBaseMap((bm) => {
        const day = bm[todayKey] || {};
        const base = { ...(day[label] || {}) };
        let added = false;
        (d.clubs || []).forEach((c) => {
          if (base[c.anchor_id] == null) { base[c.anchor_id] = c.intimacy || 0; added = true; }
        });
        if (!added) return bm;
        const next = { ...bm, [todayKey]: { ...day, [label]: base } };
        try { localStorage.setItem('fansclub_today_base', JSON.stringify(next)); } catch { /* 忽略 */ }
        return next;
      });
      setOverviewMap((m) => {
        const next = { ...m, [label]: { ...d, checked_at: Date.now() } };
        try { localStorage.setItem('fansclub_overview', JSON.stringify(next)); } catch { /* 忽略 */ }
        return next;
      });
    } catch { /* ignore */ }
  }, []);

  const lastCheckAllDone = React.useRef('');
  React.useEffect(() => {
    if (!checkAll || checkAll.running) return undefined;
    const doneKey = String(checkAll.finished_at || '');
    if (!checkAll.done_labels?.length || lastCheckAllDone.current === doneKey) return undefined;
    lastCheckAllDone.current = doneKey;
    checkAll.done_labels.forEach((label) => fetchOverviewSilent(label));
    return undefined;
  }, [checkAll, fetchOverviewSilent]);
  React.useEffect(() => {
    if (!checkAll?.running) return undefined;
    const t = setInterval(async () => {
      try {
        const r = await fetch(`${API_BASE}/api/fansclub/check-status`);
        setCheckAll(await r.json());
      } catch { /* ignore */ }
    }, 3000);
    return () => clearInterval(t);
  }, [checkAll?.running]);

  // 轮询签到任务状态 (运行中每 2s); 后端重启等导致 fetch 连续失败时自动解除禁用
  const hasRunning = signJobs.some((j) => j.running);
  const pollFailRef = React.useRef(0);
  // 单团签到: 每主播行独立按钮, 状态 {aid: 'signing'|'ok:...'|'err:...'}
  // (声明必须在下面的 sign-all effect 之前 — 它的依赖数组引用了 clubSignState)
  const [clubSignState, setClubSignState] = React.useState({});
  // 签到全部完成后: 用后端 sign-status 里每个账户的 signed_anchors 直接置已签
  // + 后端已自动检测落盘, 这里把全部签过账户的检测结果拉进表格
  // (旧版只刷 selected 一个账户 — 一键签到全部后其余账户永远显示未签, 2026-11-07 修复)
  const wasRunningRef = React.useRef(false);
  React.useEffect(() => {
    if (wasRunningRef.current && !hasRunning && !signingAll) {
      wasRunningRef.current = false;
      // ① 本会话签到的团直接置已签 (后端 sign-status 每账户带回 signed_anchors)
      setOverviewMap((m) => {
        const next = { ...m };
        for (const j of signJobs) {
          const anchors = j.signed_anchors || [];
          if (!anchors.length || !next[j.label]?.clubs) continue;
          const aidSet = new Set(anchors);
          next[j.label] = {
            ...next[j.label],
            clubs: next[j.label].clubs.map((x) => (aidSet.has(x.anchor_id) ? { ...x, factor: Math.floor(Date.now() / 1000) } : x)),
          };
        }
        return next;
      });
      // ② 后端签到完成已自动检测并落盘, 拉全部签过账户的最新结果
      //    (无签成功的账户也拉 — 状态列要反映"跳过/失败"后的真值)
      for (const j of signJobs) {
        if (j.label) fetchOverviewSilent(j.label);
      }
    }
    if (hasRunning) wasRunningRef.current = true;
  }, [hasRunning, signingAll, signJobs, fetchOverviewSilent]);
  React.useEffect(() => {
    // 挂载即同步一次: 防止后端重启后残留 signingAll=true 永久禁用按钮
    (async () => {
      try {
        const r = await fetch(`${API_BASE}/api/fansclub/sign-status`);
        const d = await r.json();
        setSignJobs(d.jobs || []);
        if (!(d.jobs || []).some((j) => j.running)) setSigningAll(false);
      } catch { /* ignore */ }
    })();
  }, []);
  React.useEffect(() => {
    if (!hasRunning && !signingAll) return undefined;
    const t = setInterval(async () => {
      try {
        const r = await fetch(`${API_BASE}/api/fansclub/sign-status`);
        const d = await r.json();
        pollFailRef.current = 0;
        setSignJobs(d.jobs || []);
        if (!(d.jobs || []).some((j) => j.running)) setSigningAll(false);
      } catch {
        pollFailRef.current += 1;
        if (pollFailRef.current >= 3) setSigningAll(false); // 连续失败3次=后端不在, 解除禁用
      }
    }, 2000);
    return () => clearInterval(t);
  }, [hasRunning, signingAll]);

  async function signOne(label) {
    if (!label) return;
    setError('');
    try {
      const r = await fetch(`${API_BASE}/api/fansclub/sign/${encodeURIComponent(label)}`, { method: 'POST' });
      const d = await r.json();
      if (!d.started) setError(d.reason || '未能启动');
      else setSigningAll(true);
    } catch (e) { setError(e.message); }
  }

  async function signAll() {
    setError('');
    try {
      const r = await fetch(`${API_BASE}/api/fansclub/sign-all`, { method: 'POST' });
      const d = await r.json();
      const started = Object.values(d.results || {}).filter((x) => x.started).length;
      if (started === 0) setError('没有可启动的签到任务');
      else setSigningAll(true);
    } catch (e) { setError(e.message); }
  }

  const jobFor = (label) => signJobs.find((j) => j.label === label);
  // (clubSignState 已在上方 sign-all effect 前声明, 此处不重复)
  // 跨天重置: 北京日期变化时清空单团签到状态 (昨天的"已签"不能再禁今天的按钮)
  const signStateDayRef = React.useRef(new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 10));
  React.useEffect(() => {
    const t = setInterval(() => {
      const d = new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 10);
      if (d !== signStateDayRef.current) {
        signStateDayRef.current = d;
        setClubSignState({}); // 解开所有"已签"禁用, 以重新检测/签到结果为准
      }
    }, 30 * 1000);
    return () => clearInterval(t);
  }, []);
  // 单团检测: 行内按钮刷新该团最新状态
  const [clubCheckBusy, setClubCheckBusy] = React.useState('');
  async function checkClub(label, c) {
    if (clubCheckBusy) return;
    setClubCheckBusy(c.anchor_id);
    try {
      const r = await fetch(`${API_BASE}/api/fansclub/check-club/${encodeURIComponent(label)}?anchor_id=${encodeURIComponent(c.anchor_id)}`);
      const d = await r.json();
      if (!d.ok) throw new Error(d.msg || '检测失败');
      // 只更新该团数据行; 账户级 checked_at 不动 (账户检测时间只随账户级检测变, 用户指定)
      setOverviewMap((m) => {
        const ov = m[label];
        if (!ov || !ov.clubs) return m;
        const clubs = ov.clubs.map((x) => {
          if (x.anchor_id !== c.anchor_id) return x;
          const merged = { ...x, ...d.club };
          // 日历有渲染延迟: 刚签的条目未出现时 factor 为空, 保留旧值不回退
          if (d.club.factor == null && x.factor != null) merged.factor = x.factor;
          return merged;
        });
        const next = { ...m, [label]: { ...ov, clubs } };
        try { localStorage.setItem('fansclub_overview', JSON.stringify(next)); } catch { /* 忽略 */ }
        return next;
      });
      // 今日基线 (增量数据源): 该团当天还没有基线时记一条
      const bjToday = new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 10);
      setBaseMap((bm) => {
        const day = bm[bjToday] || {};
        const base = { ...(day[label] || {}) };
        if (base[c.anchor_id] == null) {
          base[c.anchor_id] = d.club.intimacy || 0;
          const next = { ...bm, [bjToday]: { ...day, [label]: base } };
          try { localStorage.setItem('fansclub_today_base', JSON.stringify(next)); } catch { /* 忽略 */ }
          return next;
        }
        return bm;
      });
      showToast('ok', `${d.club?.nickname || c.nickname} 检测完成${d.club?.streak != null ? `（连签${d.club.streak}天）` : ''}`);
    } catch (e) {
      showToast('err', `${c.nickname} 检测失败：${e.message}`);
    } finally {
      setClubCheckBusy('');
    }
  }
  // toast 提示 (签到成功/失败统一弹, 3.5s 自动消失)
  const [toast, setToast] = React.useState(null); // {kind:'ok'|'err', text}
  const toastTimer = React.useRef(null);
  function showToast(kind, text) {
    setToast({ kind, text });
    clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), 3500);
  }
  async function signClub(label, c) {
    const aid = c.anchor_id;
    setClubSignState((s) => ({ ...s, [aid]: 'signing' }));
    try {
      const r = await fetch(`${API_BASE}/api/fansclub/sign-club/${encodeURIComponent(label)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ anchor_id: aid }),
      });
      const d = await r.json();
      if (d.ok) {
        setClubSignState((s) => ({ ...s, [aid]: `ok:${d.msg || '已签'}` }));
        // 签到成功即写上次签到时刻=现在 (日历接口有渲染延迟, 不等它)
        setOverviewMap((m) => {
          const ov = m[label];
          if (!ov || !ov.clubs) return m;
          const now = Math.floor(Date.now() / 1000);
          const clubs = ov.clubs.map((x) => (x.anchor_id === aid ? { ...x, factor: now } : x));
          const next = { ...m, [label]: { ...ov, clubs } };
          try { localStorage.setItem('fansclub_overview', JSON.stringify(next)); } catch { /* 忽略 */ }
          return next;
        });
        showToast('ok', `${c.nickname} 签到成功${d.msg ? `（${d.msg}）` : ''}`);
      } else {
        setClubSignState((s) => ({ ...s, [aid]: `err:${d.msg || '失败'}` }));
        showToast('err', `${c.nickname} 签到失败：${d.msg || '未知错误'}`);
      }
    } catch (e) {
      setClubSignState((s) => ({ ...s, [aid]: `err:${e.message}` }));
      showToast('err', `${c.nickname} 签到失败：${e.message}`);
    }
  }
  const overview = overviewMap[selected] ?? null; // 当前账户的缓存结果
  const clubs = overview?.clubs || [];
  // 上次检测时间 (该账户缓存写入时刻)
  const checkedAt = (overview && overview.checked_at) || null;
  // 今日亲密度 = 当前 - 当天基线 (服务端 increased_intimacy_today 字段是死值, 不用)
  // 注意用北京日期: toISOString 是 UTC, 北京早上8点前会算成昨天 → 增量错天恒0
  const todayKey = new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 10); // 北京日期(UTC+8), 裸toISOString在早8点前算成昨天
  // 跨天清理: 只留今天(和昨天兜底)的基线, 防无限增长
  React.useEffect(() => {
    setBaseMap((bm) => {
      if (bm[todayKey]) return bm;
      // 昨天的北京日期: 北京今天 0 点往前推 24h (now-16h 的 UTC 即北京昨天)
      const yesterday = new Date(Date.now() - 16 * 3600 * 1000).toISOString().slice(0, 10);
      const next = {};
      if (bm[yesterday]) next[yesterday] = bm[yesterday];
      try { localStorage.setItem('fansclub_today_base', JSON.stringify(next)); } catch { /* 忽略 */ }
      return next;
    });
  }, [todayKey]);
  const todayBase = (baseMap[todayKey] || {})[selected] || {};
  const todayGainFor = (c) => {
    const b = todayBase[c.anchor_id];
    if (b == null) return 0; // 无基线 (账户今天从未检测过) → 显示 0 (用户指定, 不展示空)
    const g = (c.intimacy || 0) - b;
    return g > 0 ? `+${g}` : '0';
  };
  // 昨日亲密度: 凌晨 0 点自动检测的归档 (overview.days[昨日YYYYMMDD][anchor_id] = intimacy)。
  // 北京日期口径与 todayKey 相同 (+8h); 昨天无归档 (新账户/后端当天没跑) → '—' (有 title 说明, 不误导)
  const yesterdayKey = new Date(Date.now() - 16 * 3600 * 1000).toISOString().slice(0, 10).replace(/-/g, '');
  const yIntimacyFor = (c) => {
    const snap = overview?.days?.[yesterdayKey];
    if (!snap) return null;
    const v = snap[c.anchor_id];
    return v == null ? null : v;
  };
  const checking = loadingLabel === selected; // 只有当前账户自己在检查才算
  // 全量检查时当前账户是否正在被检查 (tab 上有橙色呼吸点, 但表格不打断)
  const checkingCurrent = loadingLabel === selected;
  const checkedLabelRef = React.useRef(new Set()); // 已检查过的账户(标记用, 与缓存同步)
  Object.keys(overviewMap).forEach((k) => { if (overviewMap[k]) checkedLabelRef.current.add(k); });

  return (
    <section className="panel fansclub-panel" id="sec-fansclub">
      <div className="panel-head">
        <div>
          <h2>粉丝团签到</h2>
          <p>
            凭证账户 {accounts.length} 个{overview?.ok && ` · ${overview.label}：${clubs.length} 团，今日已签 ${clubs.filter((c) => (c.factor && new Date(c.factor * 1000) >= new Date(`${todayKey}T00:00:00+08:00`)) || clubSignState[c.anchor_id]?.startsWith('ok:')).length}`}
          </p>
        </div>
        {/* 重载凭证/检查全部/一键签到全部已移至顶栏 (滚到本区域时顶栏切换为这三个置顶按钮, 用户指定 2026-11-04) */}
      </div>

      {error && <div className="notice">{error}</div>}
      {accounts.length === 0 && (
        <div className="empty">
          未找到凭证文件。请在 data/fansclub_tokens/ 放入 {'{label, x_tt_token}'}.json。
        </div>
      )}

      {accounts.length > 0 && (
        <div className="fansclub-toolbar">
          {accounts.map((a) => {
            const job = jobFor(a.label);
            const isSel = selected === a.label;
            return (
              <div key={a.label} className={`fansclub-tab-wrap${isSel ? ' active' : ''}`}>
                <button
                  className={`fansclub-tab${isSel ? ' active' : ''}`}
                  onClick={() => setSelected(a.label)}
                >
                  {a.label}
                  {checkedLabelRef.current.has(a.label) && <span className="tab-check-dot" title="已检查（结果保存在本机）" />}
                  {loadingLabel === a.label && <span className="tab-check-dot checking" title="检查中" />}
                  {job?.running && ' ⏳'}
                  {job?.done && !job.running && ` ✓${job.ok}/${job.ok + job.fail}`}
                </button>
                {/* 账户级信息挂在账户正下方: 上次检测时间 */}
                {(() => {
                  const ov = overviewMap[a.label];
                  if (loadingLabel === a.label) return <span className="tab-check-at">检测中…</span>;
                  return ov?.checked_at
                    ? <span className="tab-check-at">{fmtDayTime(ov.checked_at)}</span>
                    : <span className="tab-check-at">未检测</span>;
                })()}
              </div>
            );
          })}
          <button
            className="secondary fansclub-sign-one"
            onClick={() => loadOverview(selected)}
            disabled={!selected || checking}
            title="拉取当前账户全部粉丝团的最新状态（连签/亲密度/入团天数），其他账户检查中也可并发检查"
          >
            <Search size={16} /> {checking ? '检查中…' : '检查'}
          </button>
          <button
            className="secondary"
            onClick={() => signOne(selected)}
            disabled={!selected || signingAll || !!jobFor(selected)?.running}
            title={`并发签到 ${selected || ''} 全部未签团（免费 join 端点, 幂等; 后台线程, 不阻塞页面）`}
          >
            {jobFor(selected)?.running ? '签到中…' : '签到本账户'}
          </button>
        </div>
      )}

      {checking && !clubs.length && <div className="empty">检测中…</div>}
      {!checking && !overview && !error && accounts.length > 0 && (
        <div className="empty">点「检查」拉取 {selected} 的粉丝团状态</div>
      )}
      {!checking && clubs.length > 0 && (
        <div className={`fansclub-table${checking ? ' checking' : ''}`}>
          <div className="fansclub-row fansclub-header">
            <span>主播</span><span>等级</span><span>勋章</span><span>连签</span><span>状态</span><span>上次签到</span><span>上次检测</span><span title="昨天结束时的亲密度 (来自每日凌晨 0 点自动检测的归档)">昨日亲密度</span><span>今日亲密度</span><span>亲密度</span><span>距下一级</span><span>入团</span><span>操作</span>
          </div>
          {clubs.map((c) => {
            const noCircle = c.has_join_circle === false;
            const distNext = (c.next_level_intimacy != null && c.next_level_intimacy > (c.intimacy || 0))
              ? c.next_level_intimacy - (c.intimacy || 0) : null;
            return (
              <div key={c.anchor_id} className="fansclub-row">
                <span title={`${c.nickname}${c.progress_text ? ` · ${c.progress_text}` : ''}`}>
                  {c.nickname}
                </span>
                {checking ? (
                  // 检查当前账户时: 保留行框架, 数值列写「检测中」
                  <>
                    <span className="cell-checking" colSpan={10}>检测中…</span>
                  </>
                ) : (
                  <>
                    <span>LV{c.level}</span>
                    <span className={c.badge_status === 2 ? 'badge-off' : 'badge-on'} title={c.badge_status === 2 ? '勋章熄灭（长时间未观看）' : '勋章点亮'}>
                      {c.badge_status === 2 ? '熄' : '亮'}
                    </span>
                    <span>{c.streak}天</span>
                    {(() => {
                      // 状态列按检测时刻的"当天"判定: 跨天后昨天的已签显示"昨签", 今日重置为可签
                      const factorToday = c.factor && new Date(c.factor * 1000) >= new Date(`${todayKey}T00:00:00+08:00`);
                      const st = clubSignState[c.anchor_id] || '';
                      const isOk = st.startsWith('ok:') || factorToday;
                      // 状态只有已签/未签两种 (用户指定 2026-11-04, 旧"待定"已从后端删除)
                      const label = noCircle ? <span className="nocircle">未入圈</span>
                        : isOk ? '已签' : '未签';
                      return <span className={isOk ? 'signed' : 'unsigned'}>{label}</span>;
                    })()}
                    <span className="signed-at-cell" title={c.factor ? `该团最近一次签到: ${new Date(c.factor * 1000).toLocaleString('zh-CN')}` : '无记录'}>
                      {c.factor ? fmtDayTime(c.factor * 1000) : '—'}
                    </span>
                    <span className="signed-at-cell" title="该团最近一次检测时刻">
                      {checkedAt ? fmtDayTime(checkedAt) : '—'}
                    </span>
                    <span className="y-intimacy"
                      title={yIntimacyFor(c) != null ? '昨天结束时的亲密度 (凌晨0点自动检测归档)' : '昨日无归档 (后端凌晨未检测或账户较新)'}>
                      {yIntimacyFor(c) != null ? yIntimacyFor(c).toLocaleString() : '—'}
                    </span>
                    <span className={todayGainFor(c) && todayGainFor(c) !== '0' ? 'today-gain' : 'muted'}
                      title={todayGainFor(c) && todayGainFor(c) !== '0' ? '今日亲密度增量' : undefined}>
                      {todayGainFor(c)}
                    </span>
                    <span title={distNext != null ? `下一级需 ${c.next_level_intimacy?.toLocaleString()} 亲密度` : undefined}>
                      {(c.intimacy || 0).toLocaleString()}
                    </span>
                    <span className="dist-next" title={distNext != null ? `距 LV${(c.level || 0) + 1} 还差 ${distNext.toLocaleString()} 亲密度` : '已到顶或未知'}>
                      {distNext != null ? distNext.toLocaleString() : '—'}
                    </span>
                    <span>{c.join_days != null ? `${c.join_days}天` : '-'}</span>
                    <span className="club-sign-cell">
                      <button
                        className="club-check-btn"
                        disabled={clubCheckBusy === c.anchor_id}
                        title={`检测该主播最新状态${c.factor ? `（上次签到 ${fmtDayTime(c.factor * 1000)}）` : ''}`}
                        onClick={() => checkClub(selected, c)}
                      >
                        {clubCheckBusy === c.anchor_id ? '…' : '检'}
                      </button>
                      {clubSignState[c.anchor_id]?.startsWith('signing') ? (
                        <button className="club-sign-btn" disabled>…</button>
                      ) : clubSignState[c.anchor_id]?.startsWith('ok:')
                        || (c.factor && new Date(c.factor * 1000) >= new Date(`${todayKey}T00:00:00+08:00`)) ? (
                        // 今日已签(本会话签到成功, 或检测结果的上次签到时刻是今天): 置灰不可点
                        // c.signed 是上次检测快照(可能是昨天), 不用于今天的按钮判断
                        <button className="club-sign-btn club-sign-disabled" disabled
                          title={clubSignState[c.anchor_id]?.slice(3) || '今日已签'}>
                          已签
                        </button>
                      ) : (
                        <button
                          className="club-sign-btn"
                          title="签到该主播粉丝团（免费 join，幂等）"
                          onClick={() => signClub(selected, c)}
                        >
                          签
                        </button>
                      )}
                      {clubSignState[c.anchor_id]?.startsWith('err:') && (
                        <span className="club-sign-err" title={clubSignState[c.anchor_id].slice(4)}>!</span>
                      )}
                    </span>
                  </>
                )}
              </div>
            );
          })}
        </div>
      )}
      {toast && (
        <div className={`fc-toast ${toast.kind}`} role="status">
          {toast.kind === 'ok' ? '✓ ' : '✗ '}{toast.text}
        </div>
      )}
      {checkedAt && (
        <div className="fansclub-checked-at">
          上次检测：{fmtDayTime(checkedAt)}
          {checking && ' · 本次检测中…'}
        </div>
      )}
    </section>
  );
}

// ---- 直播实时监控 (阶段二: PC 直连快照轮询, 零浏览器; 嵌在账户列表面板内) ----
// 目标双维度: sec_uid 账户维度 (每轮自动解析当前场次 room_id, 重新开播也跟得住)
//            room_id 房间维度 (feed 在播房/手动加; 下播后失效)
function LiveMonitorPanel({ embedded, onOpenLibrary }) {
  // 主播库形态 (2026-11-07 用户定论): 目标从主播库弹窗勾选「加入监控」, 加入即监控;
  // 不再有"全量开关/临时粘贴目标"。下播后该目标由后端自动关闭 (live_monitor._remove_locked)。
  const [status, setStatus] = React.useState(null); // 后端 /api/live-monitor/status
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState('');
  const running = !!(status && status.running);

  const loadStatus = React.useCallback(async () => {
    try {
      const r = await fetch(`${API_BASE}/api/live-monitor/status`);
      setStatus(await r.json());
    } catch { /* ignore */ }
  }, []);

  React.useEffect(() => { loadStatus(); }, [loadStatus]);
  React.useEffect(() => {
    // 轮询状态: 运行中 5s, 空闲 30s
    const t = setInterval(loadStatus, running ? 5000 : 30000);
    return () => clearInterval(t);
  }, [running, loadStatus]);

  const stopMonitor = async () => {
    setBusy(true);
    try { await fetch(`${API_BASE}/api/live-monitor/stop`, { method: 'POST' }); await loadStatus(); }
    catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  };

  const rooms = (status && status.rooms) || [];
  const events = (status && status.events) || [];
  const fmtT = (ts) => (ts ? fmtDayTime(ts * 1000) : '—');
  const levelColor = (lv) => (lv === 'live' ? 'lm-live' : lv === 'offline' ? 'lm-off' : lv === 'error' ? 'lm-err' : '');

  return (
    <div className={`live-monitor-box${embedded ? ' embedded' : ''}`}>
      <div className="lm-bar">
        <strong className="lm-name">直播监控</strong>
        <span className="lm-sub">
          {running
            ? `监控中 · ${rooms.length} 目标 · 间隔 ${status.interval}s`
            : '空闲 · 从主播库加入主播后自动开始轮询（零浏览器直连）'}
        </span>
        <span className="lm-spacer" />
        <button className="secondary" onClick={onOpenLibrary} title="打开主播库：加入 / 移出监控，查看详情">
          <Search size={16} /> 主播库
        </button>
        {running && (
          <button onClick={stopMonitor} disabled={busy}>全部停止</button>
        )}
      </div>

      {err && <div className="notice">{err}</div>}

      {running && rooms.length > 0 && (
        <div className="lm-rooms">
          <div className="lm-room lm-head">
            <span>主播</span><span>状态</span><span>观众</span><span>标题</span><span>最近快照</span>
          </div>
          {rooms.map((r) => (
            <div key={r.key} className="lm-room">
              <span>{r.label}</span>
              <span className={r.was_live ? 'lm-on' : 'lm-off'}>{r.was_live ? '直播中' : '解析中…'}</span>
              <span>{r.viewers != null ? r.viewers.toLocaleString() : '—'}</span>
              <span className="lm-title" title={r.title}>{r.title || '—'}</span>
              <span>{fmtT(r.last_tick)}</span>
            </div>
          ))}
        </div>
      )}
      {running && rooms.length === 0 && (
        <div className="empty">启动中 — 首轮快照几秒内出现</div>
      )}

      {events.length > 0 && (
        <div className="lm-events">
          {events.slice().reverse().map((e, i) => (
            <div key={i} className={`lm-event ${levelColor(e.level)}`}>
              <span className="lm-ev-time">{fmtT(e.time)}</span>
              <span className="lm-ev-msg">{e.message}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ---- 主播库弹窗 (2026-11-07): 替代旧账户大表格 ----
// 库 = users.json; 每行一个主播: 监控开关 (加入/移出即生效) / 详情按钮 / 删除。
// 「详情」弹小窗展示最近一次检测结果 (profile_cache), 可手动刷基本。
function StreamerLibraryModal({ onClose, onChanged }) {
  const [users, setUsers] = React.useState([]);
  const [busyUid, setBusyUid] = React.useState('');
  const [err, setErr] = React.useState('');
  const [manual, setManual] = React.useState(''); // 主页链接/短链/sec_uid/抖音号/名称
  const [addBusy, setAddBusy] = React.useState(false);
  const [detailFor, setDetailFor] = React.useState(null); // {row, loading, error}
  const [deleteFor, setDeleteFor] = React.useState(null);
  const monitorCount = users.filter((u) => u.monitor).length;

  const load = React.useCallback(async () => {
    try {
      const r = await fetch(`${API_BASE}/api/users`);
      const d = await r.json();
      setUsers(d.users || []);
    } catch (e) { setErr(e.message); }
  }, []);

  React.useEffect(() => { load(); }, [load]);

  // 加入 / 移出监控: monitor 位落库 + live-monitor add/remove 动态挂摘 (一条龙)
  async function toggleMonitor(u) {
    setBusyUid(u.sec_uid); setErr('');
    const next = !u.monitor;
    try {
      const r = await fetch(`${API_BASE}/api/users/monitor-toggle`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sec_uid: u.sec_uid, monitor: next }),
      });
      if (!r.ok) throw new Error((await r.json()).detail || '开关失败');
      await fetch(`${API_BASE}/api/live-monitor/${next ? 'add' : 'remove'}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sec_uid: u.sec_uid, label: u.label || u.nickname || u.sec_uid.slice(0, 12) }),
      });
      await load();
      if (onChanged) await onChanged();
    } catch (e) { setErr(e.message); }
    finally { setBusyUid(''); }
  }

  // 粘贴解析入库 (复用 resolve-link 三段解析); 解析成功 → 入库 + 默认纳入监控
  async function addManual() {
    const v = manual.trim();
    if (!v) { setErr('请输入主页链接 / 短链 / sec_uid / 抖音号 / 名称'); return; }
    setAddBusy(true); setErr('');
    try {
      const r = await fetch(`${API_BASE}/api/users/resolve-link`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ keyword: v }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : '解析失败');
      if (d.kind === 'none') throw new Error('未解析到账户 — 换主页链接试试');
      const c = d.kind === 'sec_uid'
        ? { sec_uid: d.sec_uid, nickname: '', unique_id: '' }
        : d.candidate;
      const label = c.nickname || c.unique_id || c.sec_uid.slice(0, 12);
      const ar = await fetch(`${API_BASE}/api/users`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label, sec_uid: c.sec_uid, homepage_url: c.homepage_url || `https://www.douyin.com/user/${c.sec_uid}`, nickname: c.nickname || '', monitor: true }),
      });
      const ad = await ar.json();
      if (!ar.ok) throw new Error(ad.detail?.error || ad.detail || '入库失败');
      // 入库默认 monitor=true → 同步挂进监控
      await fetch(`${API_BASE}/api/live-monitor/add`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sec_uid: c.sec_uid, label }),
      });
      setManual('');
      await load();
      if (onChanged) await onChanged();
    } catch (e) { setErr(e.message); }
    finally { setAddBusy(false); }
  }

  // 详情: 拉该主播的检测结果 (先读库缓存, 再手动 /api/query 刷新)
  async function openDetail(u) {
    setDetailFor({ row: u, loading: true, error: '' });
    try {
      const r = await fetch(`${API_BASE}/api/query`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ targets: [u.sec_uid] }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail?.error || d.detail || '检测失败');
      const result = (d.results || [])[0];
      if (!result || !result.ok) throw new Error(result?.error || '检测失败');
      setDetailFor({ row: { ...u, ...result }, loading: false, error: '' });
      await load();
      if (onChanged) await onChanged();
    } catch (e) {
      setDetailFor({ row: u, loading: false, error: e.message });
    }
  }

  async function confirmDelete() {
    const u = deleteFor;
    if (!u) return;
    setBusyUid(u.sec_uid); setErr('');
    try {
      // 先摘监控再删库 (监控在跑时删库会留下幽灵目标)
      await fetch(`${API_BASE}/api/live-monitor/remove`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sec_uid: u.sec_uid }),
      });
      const r = await fetch(`${API_BASE}/api/users/${encodeURIComponent(u.sec_uid)}`, { method: 'DELETE' });
      if (!r.ok) throw new Error((await r.json()).detail || '删除失败');
      setDeleteFor(null);
      await load();
      if (onChanged) await onChanged();
    } catch (e) { setErr(e.message); }
    finally { setBusyUid(''); }
  }

  return (
    <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div className="modal library-modal" onMouseDown={(event) => event.stopPropagation()}>
        <div className="modal-head">
          <div>
            <h2>主播库</h2>
            <p>共 {users.length} 人 · 监控中 {monitorCount} — 勾选即监控，下播自动结束该目标</p>
          </div>
          <button className="secondary" onClick={onClose}>关闭</button>
        </div>

        {err && <div className="notice">{err}</div>}

        <div className="lm-add-row">
          <input className="lm-add-input" value={manual}
            onChange={(e) => setManual(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') addManual(); }}
            placeholder="添加主播：主页链接 / 短链 / sec_uid / 抖音号 / 名称，回车解析入库并纳入监控"
          />
          <button className="secondary" onClick={addManual} disabled={addBusy}>
            <Plus size={16} /> {addBusy ? '解析中…' : '添加'}
          </button>
        </div>

        <div className="candidate-list">
          {users.map((u) => {
            const profile = u.last_profile || {};
            const live = profile.live_status === 1;
            return (
              <article className={`candidate-row lib-row${u.monitor ? ' monitored' : ''}`} key={u.sec_uid}>
                <label className="select-cell" title={u.monitor ? '监控中（点击移出）' : '未监控（点击加入）'}>
                  <input type="checkbox" checked={!!u.monitor}
                    disabled={busyUid === u.sec_uid}
                    onChange={() => toggleMonitor(u)} />
                </label>
                <img src={u.avatar_url || ''} alt="" />
                <div>
                  <strong>{u.label || u.nickname || '未备注'}</strong>
                  <small>
                    {live
                      ? <span className="live-pill-inline">直播中 {profile.live_viewers != null ? `· ${formatNumber(profile.live_viewers)}人` : ''}</span>
                      : '未直播'}
                    {u.last_checked_at ? ` · 检测 ${formatTime(u.last_checked_at)}` : ''}
                  </small>
                  <span title={u.sec_uid}>
                    粉丝 {formatNumber(profile.follower_count || u.follower_count)}
                    {profile.live_room?.anchor?.paygrade_level ? ` · Lv ${profile.live_room.anchor.paygrade_level}` : ''}
                  </span>
                </div>
                <div className="lib-actions">
                  <button className="secondary" onClick={() => openDetail(u)} disabled={detailFor?.loading}
                    title="检测并查看详情 (基本信息+直播间)">
                    详情
                  </button>
                  <a className="icon-button" href={u.url} target="_blank" rel="noreferrer" title="打开主页">
                    <ExternalLink size={15} />
                  </a>
                  <button className="danger icon-button" onClick={() => setDeleteFor(u)} title="从主播库删除">
                    <Trash2 size={15} />
                  </button>
                </div>
              </article>
            );
          })}
          {users.length === 0 && <div className="empty">主播库为空 — 用上方输入框添加主播</div>}
        </div>
      </div>

      {detailFor && (
        <StreamerDetailModal detail={detailFor} onClose={() => setDetailFor(null)} />
      )}
      {deleteFor && (
        <DeleteAccountModal
          row={deleteFor}
          busy={busyUid === deleteFor.sec_uid}
          onClose={() => setDeleteFor(null)}
          onConfirm={confirmDelete}
        />
      )}
    </div>
  );
}

// 主播库详情弹窗: 基本信息 + 直播中显示直播间人数/标题, 数据来自 /api/query 结果
function StreamerDetailModal({ detail, onClose }) {
  const row = detail.row || {};
  const profile = row.profile || {};
  const live = profile.live_status === 1;
  return (
    <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div className="modal compact-modal" onMouseDown={(event) => event.stopPropagation()}>
        <div className="modal-head">
          <div>
            <h2>{row.label || profile.nickname || '主播详情'}</h2>
            <p>{detail.loading ? '检测中…' : (detail.error || '最近一次检测结果')}</p>
          </div>
          <button className="secondary" onClick={onClose}>关闭</button>
        </div>
        {detail.loading ? (
          <div className="empty"><Loader2 size={18} className="spin" /> 正在检测该主播…</div>
        ) : detail.error ? (
          <div className="notice">{detail.error}</div>
        ) : (
          <div className="detail-grid">
            <Info label="抖音号" value={profile.unique_id || row.unique_id || '-'} />
            <Info label="粉丝" value={formatNumber(profile.follower_count || row.follower_count)} />
            <Info label="关注" value={formatNumber(profile.following_count || row.following_count)} />
            <Info label="获赞" value={formatNumber(profile.total_favorited || row.total_favorited)} />
            <Info label="IP地址" value={profile.ip_location || row.ip_location || '-'} />
            <Info label="直播状态" value={live ? '直播中' : '未直播'} />
            {live && <Info label="直播间人数" value={formatNumber(profile.live_viewers)} />}
            {live && profile.live_room?.title && <Info label="直播标题" value={String(profile.live_room.title).slice(0, 40)} />}
            <Info label="上次检测" value={formatTime(row.last_checked_at)} />
          </div>
        )}
      </div>
    </div>
  );
}

function PostsPanel({ accounts }) {
  const [secUid, setSecUid] = React.useState('');
  // 自定义数量 (用户指定 2026-11-04: 仅支持自定义, 不做预设档位)
  const [maxCount, setMaxCount] = React.useState('50');
  const [posts, setPosts] = React.useState(null);
  const [meta, setMeta] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState('');
  const [checked, setChecked] = React.useState(new Set());
  const [commentsFor, setCommentsFor] = React.useState(null); // {aweme_id, desc}
  const [comments, setComments] = React.useState(null);
  const [commentsBusy, setCommentsBusy] = React.useState(false);
  const [commentsError, setCommentsError] = React.useState('');
  const [batchBusy, setBatchBusy] = React.useState(false);

  const load = React.useCallback(async () => {
    if (!secUid) return;
    setBusy(true);
    setError('');
    setPosts(null);
    setMeta(null);
    setChecked(new Set());
    setCommentsFor(null);
    try {
      const n = Math.max(1, Math.min(400, parseInt(maxCount, 10) || 0));
      if (!n) throw new Error('数量必须是 1-400 的数字');
      const r = await fetch(`${API_BASE}/api/posts/list`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sec_uid: secUid, max_count: n }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || r.statusText);
      setPosts(d.posts || []);
      setMeta(d);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }, [secUid, maxCount]);

  const batchDownload = React.useCallback(async () => {
    const urls = (posts || []).filter((p) => checked.has(p.aweme_id)).map((p) => p.page_url);
    if (!urls.length) return;
    setBatchBusy(true);
    setError('');
    try {
      const label = (accounts.find((a) => a.sec_uid === secUid)?.label) || secUid.slice(0, 12);
      const r = await fetch(`${API_BASE}/api/posts/batch-download`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ urls, label, mode: 1 }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || r.statusText);
      setError(`已投递下载任务 (${urls.length} 个作品), 到视频下载面板看进度`);
    } catch (e) {
      setError('批量下载失败: ' + e.message);
    } finally {
      setBatchBusy(false);
    }
  }, [posts, checked, secUid, accounts]);

  const openComments = React.useCallback(async (post) => {
    setCommentsFor({ aweme_id: post.aweme_id, desc: post.desc });
    setComments(null);
    setCommentsError('');
    setCommentsBusy(true);
    try {
      const r = await fetch(`${API_BASE}/api/comments/list`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ aweme_id: post.aweme_id, max_count: 50 }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || r.statusText);
      setComments(d);
    } catch (e) {
      setCommentsError(e.message);
    } finally {
      setCommentsBusy(false);
    }
  }, []);

  const toggleCheck = (awemeId) => {
    setChecked((prev) => {
      const next = new Set(prev);
      if (next.has(awemeId)) next.delete(awemeId);
      else next.add(awemeId);
      return next;
    });
  };

  return (
    <section className="panel posts-panel" id="sec-posts">
      <div className="panel-head">
        <div>
          <h2>作品列表 / 评论</h2>
          <p>拉取账户作品列表 (直连→浏览器降级链)，可勾选一键批量投喂下载器；每条作品可看评论</p>
        </div>
        <FileSearch size={18} />
      </div>
      <div className="live-analysis-form">
        <select value={secUid} onChange={(e) => setSecUid(e.target.value)} disabled={busy}>
          <option value="">选择账户…</option>
          {(accounts || []).map((a) => (
            <option key={a.sec_uid} value={a.sec_uid}>{a.label || a.nickname || a.sec_uid.slice(0, 16)}</option>
          ))}
        </select>
        <input
          className="posts-count-input"
          value={maxCount}
          onChange={(e) => setMaxCount(e.target.value.replace(/\D/g, ''))}
          onKeyDown={(e) => { if (e.key === 'Enter') load(); }}
          placeholder="数量(1-400)"
          inputMode="numeric"
          disabled={busy}
          title="自定义拉取条数 (1-400)"
        />
        <button onClick={load} disabled={busy || !secUid}>
          {busy ? '拉取中…' : '拉取作品'}
        </button>
      </div>
      {error && <div className="notice">{error}</div>}
      {meta && posts && (
        <div className="posts-meta">
          共 {posts.length} 条 · 数据来源: {meta.via === 'direct' ? '直连' : '浏览器'}
          {checked.size > 0 && (
            <button className="posts-batch-btn" onClick={batchDownload} disabled={batchBusy}>
              <Download size={14} /> 批量下载已选 ({checked.size})
            </button>
          )}
        </div>
      )}
      {posts && posts.length > 0 && (
        <div className="posts-table">
          {posts.map((p) => (
            <div key={p.aweme_id} className={`posts-row${checked.has(p.aweme_id) ? ' checked' : ''}`}>
              <label className="posts-check">
                <input type="checkbox" checked={checked.has(p.aweme_id)} onChange={() => toggleCheck(p.aweme_id)} />
              </label>
              <div className="posts-main">
                <div className="posts-desc" title={p.desc}>{p.desc || '(无文案)'}</div>
                <div className="posts-stats">
                  {p.is_image_post ? `图集×${p.image_count}` : formatNumber(p.digg_count) + ' 赞'}
                  {' · '}评 {formatNumber(p.comment_count)}{' · '}藏 {formatNumber(p.collect_count)}
                  {' · '}{p.aweme_id}
                </div>
              </div>
              <div className="posts-actions">
                <button onClick={() => openComments(p)} title="查看评论">评论</button>
                <a href={p.page_url} target="_blank" rel="noreferrer" title="打开原页"><ExternalLink size={14} /></a>
              </div>
            </div>
          ))}
        </div>
      )}
      {posts && posts.length === 0 && <div className="empty">未拉到作品 (可能全部仅粉丝可见或被风控)</div>}
      {commentsFor && (
        <div className="comments-drawer">
          <div className="comments-head">
            <span title={commentsFor.desc}>{commentsFor.desc || commentsFor.aweme_id}</span>
            <button onClick={() => { setCommentsFor(null); setComments(null); }}>收起</button>
          </div>
          {commentsBusy && <div className="empty">评论拉取中…</div>}
          {commentsError && <div className="notice">{commentsError}</div>}
          {comments && (
            <div className="comments-list">
              {comments.comments.map((c) => (
                <div key={c.cid} className="comment-row">
                  <div className="comment-text">{c.text}</div>
                  <div className="comment-meta">
                    {c.user_nickname} · 赞 {formatNumber(c.digg_count)}
                    {c.reply_comment_total > 0 && ` · 回复 ${c.reply_comment_total}`}
                    {c.ip_label && ` · ${c.ip_label}`}
                  </div>
                </div>
              ))}
              {comments.comments.length === 0 && <div className="empty">无评论</div>}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

function LiveAnalysisPanel() {  const [input, setInput] = React.useState('');
  const [result, setResult] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState('');

  async function analyze() {
    if (!input.trim()) return;
    setBusy(true);
    setError('');
    setResult(null);
    try {
      // 支持: 纯 room_id / 直播间链接 (live.douyin.com/<web_rid 或 room_id>)
      let roomId = input.trim();
      const m = roomId.match(/live\.douyin\.com\/(\d+)/);
      if (m) roomId = m[1];
      roomId = roomId.replace(/\D/g, '');
      if (!roomId) throw new Error('无法从输入解析出房间号');
      const r = await fetch(`${API_BASE}/api/live/analyze`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ room_id: roomId }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || r.statusText);
      if (!d.ok) throw new Error(d.error || '进房失败');
      setResult(d);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const room = result?.room;
  const fc = result?.fansclub;
  const cr = result?.checkin_rank;

  return (
    <section className="panel live-analysis-panel">
      <div className="panel-head">
        <div>
          <h2>直播间分析</h2>
          <p>输入直播间链接或房间号，拉取房间数据/粉丝团面板/签到榜（只读，不进弹幕不送礼）</p>
        </div>
      </div>
      <div className="live-analysis-form">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') analyze(); }}
          placeholder="如 https://live.douyin.com/123456 或纯房间号"
          disabled={busy}
        />
        <button onClick={analyze} disabled={busy || !input.trim()}>
          {busy ? '分析中…' : '分析'}
        </button>
      </div>
      {error && <div className="notice">{error}</div>}
      {room && (
        <div className="live-analysis-result">
          <div className="live-stat-grid">
            <div className="live-stat"><span className="k">主播</span><span className="v">{room.nickname}</span></div>
            <div className="live-stat"><span className="k">标题</span><span className="v">{room.title}</span></div>
            <div className="live-stat"><span className="k">在线观众</span><span className="v">{(room.viewers || 0).toLocaleString()}</span></div>
            <div className="live-stat"><span className="k">累计进房</span><span className="v">{(room.total_user || 0).toLocaleString()}</span></div>
            <div className="live-stat"><span className="k">点赞</span><span className="v">{(room.like_count || 0).toLocaleString()}</span></div>
            <div className="live-stat"><span className="k">本场涨粉</span><span className="v">{room.follow_gained ?? '-'}</span></div>
            <div className="live-stat"><span className="k">带货</span><span className="v">{room.has_goods ? '是' : '否'}</span></div>
            <div className="live-stat"><span className="k">送礼</span><span className="v">{room.gift_disabled ? '主播已关' : '开放'}</span></div>
          </div>
          {fc && (fc.total_fans != null) && (
            <div className="live-stat-grid">
              <div className="live-stat"><span className="k">粉丝团</span><span className="v">{fc.anchor_name}·{fc.fans_name}</span></div>
              <div className="live-stat"><span className="k">活跃粉丝</span><span className="v">{(fc.active_fans || 0).toLocaleString()}</span></div>
              <div className="live-stat"><span className="k">总粉丝</span><span className="v">{(fc.total_fans || 0).toLocaleString()}</span></div>
              <div className="live-stat"><span className="k">今日新增</span><span className="v">+{fc.today_new_fans || 0}</span></div>
            </div>
          )}
          {cr && (cr.top?.length > 0) && (
            <div className="live-rank">
              <div className="live-rank-title">签到热度榜 (我: {cr.my_score ?? 0} 分 #{cr.my_rank || '未上榜'})</div>
              {cr.top.map((t) => (
                <div key={t.rank} className="live-rank-row">
                  <span>#{t.rank}</span><span>{(t.score || 0).toLocaleString()}</span>
                </div>
              ))}
              {cr.my_desc && <div className="live-rank-desc">{cr.my_desc}</div>}
            </div>
          )}
          {result?.luckybox?.count > 0 && (
            <div className="notice">检测到 {result.luckybox.count} 个福袋 (免费活动)</div>
          )}
        </div>
      )}
    </section>
  );
}

function App() {
  const [accounts, setAccounts] = React.useState([]);
  // 左侧导航/顶栏按钮组共用的当前区块 (滚到粉丝团区域时顶栏切为粉丝团操作, 用户指定 2026-11-04)
  const [activeSection, setActiveSection] = React.useState('');
  const fansclubApi = React.useRef(null);
  const [downloadJobs, setDownloadJobs] = React.useState([]);
  // 账户大表格已撤 (2026-11-07): 库操作全在主播库弹窗; 这两个状态是顶栏检测按钮与弹窗刷新共用
  const [libraryOpen, setLibraryOpen] = React.useState(false);
  const [message, setMessage] = React.useState('');
  const [busy, setBusy] = React.useState(false);
  const [detectingUids, setDetectingUids] = React.useState(new Set());
  const [deleteTarget, setDeleteTarget] = React.useState(null);
  // watch 轮询已整体移除 (2026-11-05): 定时盯直播由直播监控 (LiveMonitorPanel) 承担
  const [downloadText, setDownloadText] = React.useState('');
  const [downloadMode, setDownloadMode] = React.useState(1);
  const [downloadOutputDir, setDownloadOutputDir] = React.useState('');
  const [downloadWrapFolder, setDownloadWrapFolder] = React.useState(false);
  const [downloadImageFormat, setDownloadImageFormat] = React.useState('auto');
  const [downloadVideoFormat, setDownloadVideoFormat] = React.useState('auto');
  const [downloadPreview, setDownloadPreview] = React.useState(null);
  const [selectedDownloadUrls, setSelectedDownloadUrls] = React.useState(new Set());
  const [selectedDownloadMedia, setSelectedDownloadMedia] = React.useState({});
  const [previewBusy, setPreviewBusy] = React.useState(false);
  const [activityLogs, setActivityLogs] = React.useState(() => {
    try {
      return JSON.parse(window.localStorage.getItem('douyinActivityLogs') || '[]');
    } catch (error) {
      return [];
    }
  });
  const accountsRef = React.useRef([]);

  const rows = React.useMemo(
    () => accounts.map((account) => mergeAccount(account, null)),
    [accounts],
  );
  const detectLogs = React.useMemo(
    () => activityLogs.filter((entry) => entry.source === '检测').slice(0, 80),
    [activityLogs],
  );
  const downloadLogs = React.useMemo(
    () => downloadJobs.flatMap((job) => (job.logs || []).map((entry) => ({
      time: entry.time,
      level: entry.level,
      message: entry.message,
      source: `下载 ${job.id.slice(0, 8)}`,
    })))
      .sort((a, b) => String(b.time).localeCompare(String(a.time)))
      .slice(0, 80),
    [downloadJobs],
  );

  function appendLog(level, message, source = '界面') {
    setActivityLogs((current) => [
      { time: localNowIso(), level, message, source },
      ...current,
    ].slice(0, 80));
  }

  React.useEffect(() => {
    window.localStorage.setItem('douyinActivityLogs', JSON.stringify(activityLogs.slice(0, 80)));
  }, [activityLogs]);

  React.useEffect(() => {
    accountsRef.current = accounts;
  }, [accounts]);

  const loadAccounts = React.useCallback(async () => {
    const response = await fetch(`${API_BASE}/api/users`);
    if (!response.ok) throw new Error('账户列表加载失败');
    const data = await response.json();
    setAccounts(data.users || []);
  }, []);

  const loadDownloads = React.useCallback(async () => {
    const response = await fetch(`${API_BASE}/api/downloads`);
    if (!response.ok) return;
    const data = await response.json();
    setDownloadJobs(data.jobs || []);
  }, []);

  const loadSettings = React.useCallback(async () => {
    const response = await fetch(`${API_BASE}/api/settings`);
    if (!response.ok) return;
    const data = await response.json();
    const settings = data.settings || {};
    setDownloadOutputDir(settings.download_output_dir || '');
    setDownloadWrapFolder(Boolean(settings.wrap_download_folder));
    setDownloadImageFormat(settings.image_format || 'auto');
    setDownloadVideoFormat(settings.video_format || 'auto');
  }, []);

  React.useEffect(() => {
    loadAccounts().catch((error) => setMessage(error.message));
    loadDownloads().catch(() => {});
    loadSettings().catch(() => {});
  }, [loadAccounts, loadDownloads, loadSettings]);

  React.useEffect(() => {
    const timer = window.setInterval(() => {
      loadAccounts().catch(() => {});
      loadDownloads().catch(() => {});
    }, 3000);
    return () => window.clearInterval(timer);
  }, [loadAccounts, loadDownloads]);

  async function postJson(path, body = {}) {
    let response;
    try {
      response = await fetch(`${API_BASE}${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
    } catch (error) {
      const err = new Error('无法连接后端服务，请确认 127.0.0.1:8000 正在运行。');
      err.cause = error;
      throw err;
    }
    let data;
    try {
      data = await response.json();
    } catch (parseError) {
      throw new Error(`后端返回非 JSON（HTTP ${response.status}），可能后端崩溃，请查看 backend.err.log。`);
    }
    if (!response.ok) {
      const detail = data && data.detail;
      let message;
      let progress;
      if (detail && typeof detail === 'object') {
        message = detail.error || `请求失败 (${response.status})`;
        progress = detail.progress;
      } else {
        message = detail || `请求失败 (${response.status})`;
      }
      const err = new Error(message);
      err.progress = progress;
      throw err;
    }
    return data;
  }

  function setMessageWithProgress(message, error) {
    const detail = error && error.progress ? progressSummary(error.progress) : '';
    setMessage(detail ? `${message}\n${detail}` : message);
  }

  // 主播库顶栏快捷检测 (顶栏「刷基本」按钮): 只检测库内勾选目标不可行 (勾选在弹窗内),
  // 顶栏按钮语义 = 刷新全部主播的基本信息 (库不大, 27 个并发直连很快)
  async function detectTargets(targets = []) {
    const actualTargets = targets.length ? targets : rows.map((row) => row.sec_uid);
    const pendingTargets = actualTargets.filter((uid) => !detectingUids.has(uid));
    if (pendingTargets.length === 0) return;

    setDetectingUids((current) => new Set([...current, ...pendingTargets]));
    setMessage(`正在检测 ${pendingTargets.length} 个主播的基本信息…\n· 解析目标账户`);
    try {
      const data = await postJson('/api/query', { targets: pendingTargets });
      await loadAccounts();
      const results = data.results || [];
      const failures = results.filter((result) => !result.ok);
      results.forEach((result) => {
        const label = result.label || result.sec_uid;
        appendLog(result.ok ? 'info' : 'error', result.ok ? `${label} 基本信息已刷新` : `${label} 基本信息检测失败：${result.error || '可能触发风控或接口暂无数据'}`, '检测');
      });
      setMessage(failures.length ? `已检测 ${results.length} 个主播，${failures.length} 个失败` : `已检测 ${results.length} 个主播`);
    } catch (error) {
      setMessageWithProgress(`检测 ${pendingTargets.length} 个主播失败：${error.message}`, error);
      appendLog('error', error.message, '检测');
    } finally {
      setDetectingUids((current) => {
        const next = new Set(current);
        pendingTargets.forEach((uid) => next.delete(uid));
        return next;
      });
    }
  }


  async function deleteAccount(row) {
    setBusy(true);
    try {
      const response = await fetch(`${API_BASE}/api/users/${encodeURIComponent(row.sec_uid)}`, { method: 'DELETE' });
      let data = {};
      try {
        data = await response.json();
      } catch (error) {
        data = {};
      }
      if (!response.ok) throw new Error(data.detail || '删除账户失败');
      const nextUsers = data.users || [];
      setAccounts(nextUsers);
      setDeleteTarget(null);
      setMessage('账户已删除');
      appendLog('info', `账户已删除：${row.label || row.profile?.nickname || row.sec_uid}`, '界面');
    } catch (error) {
      setMessage(error.message);
      appendLog('error', error.message, '界面');
    } finally {
      setBusy(false);
    }
  }


  async function submitDownload() {
    const downloadInputText = buildDownloadInputText(downloadText, downloadPreview);
    if (!downloadInputText.trim()) {
      setMessage('请输入抖音视频、图集链接或分享文案');
      return;
    }
    const selectedUrls = Array.from(selectedDownloadUrls);
    const mediaPayload = {};
    Object.entries(selectedDownloadMedia).forEach(([url, indices]) => {
      if (indices && indices.length) mediaPayload[url] = indices;
    });
    setBusy(true);
    try {
      const settingsData = await postJson('/api/settings', {
        download_output_dir: downloadOutputDir,
        wrap_download_folder: downloadWrapFolder,
        image_format: downloadImageFormat,
        video_format: downloadVideoFormat,
      });
      const actualSettings = settingsData.settings || {};
      const actualOutputDir = actualSettings.download_output_dir || downloadOutputDir;
      setDownloadOutputDir(actualOutputDir);
      await postJson('/api/downloads', {
        text: downloadInputText,
        mode: Number(downloadMode),
        output_dir: actualOutputDir,
        wrap_folder: downloadWrapFolder,
        comments: false,
        image_format: downloadImageFormat,
        video_format: downloadVideoFormat,
        selected_urls: selectedUrls,
        selected_media: mediaPayload,
      });
      setDownloadText('');
      await loadDownloads();
      setMessage(`下载任务已提交，输出到 ${actualOutputDir}`);
      const mediaCount = Object.values(mediaPayload).reduce((sum, arr) => sum + arr.length, 0);
      appendLog('info', `下载任务已提交：${selectedUrls.length || '全部'} 个链接${mediaCount ? `，含 ${mediaCount} 张指定图片` : ''}${downloadWrapFolder ? '，按标题创建文件夹' : '，直接保存到根目录'}`, '下载');
    } catch (error) {
      setMessage(error.message);
      appendLog('error', error.message, '下载');
    } finally {
      setBusy(false);
    }
  }

  async function saveDownloadSettings() {
    setBusy(true);
    try {
      const data = await postJson('/api/settings', {
        download_output_dir: downloadOutputDir,
        wrap_download_folder: downloadWrapFolder,
        image_format: downloadImageFormat,
        video_format: downloadVideoFormat,
      });
      const settings = data.settings || {};
      setDownloadOutputDir(settings.download_output_dir || '');
      setDownloadWrapFolder(Boolean(settings.wrap_download_folder));
      setDownloadImageFormat(settings.image_format || 'auto');
      setDownloadVideoFormat(settings.video_format || 'auto');
      setMessage('下载设置已保存');
      appendLog('info', `下载设置已保存：${settings.download_output_dir || ''}${settings.wrap_download_folder ? '，按标题创建文件夹' : '，直接保存到根目录'}，图片 ${settings.image_format || 'auto'}，视频 ${settings.video_format || 'auto'}`, '下载');
    } catch (error) {
      setMessage(error.message);
      appendLog('error', error.message, '下载');
    } finally {
      setBusy(false);
    }
  }

  async function cancelDownloadJob(jobId) {
    if (!jobId) return;
    try {
      await postJson(`/api/downloads/${jobId}/cancel`, {});
      await loadDownloads();
      setMessage('已请求取消下载任务');
      appendLog('warning', '已请求取消下载任务（正在运行的链接完成后停止后续）', '下载');
    } catch (error) {
      setMessage(error.message);
      appendLog('error', error.message, '下载');
    }
  }

  async function deleteDownloadJob(jobId) {
    if (!jobId) return;
    try {
      const resp = await fetch(`${API_BASE}/api/downloads/${jobId}`, { method: 'DELETE' });
      if (!resp.ok) throw new Error(`删除下载任务失败 (${resp.status})`);
      await loadDownloads();
      setMessage('下载任务已删除');
      appendLog('info', '下载任务已删除', '下载');
    } catch (error) {
      setMessage(error.message);
      appendLog('error', error.message, '下载');
    }
  }

  async function previewDownload(overrideText) {
    // onClick 直接绑本函数时，React 会把事件对象作为第一个参数传入；
    // 此时取到的不是字符串，必须忽略并回退到 downloadText，否则 text.trim 报错
    // 导致"解析预览"按钮点了完全没反应、也没有预览内容渲染。
    const raw = (typeof overrideText === 'string') ? overrideText : downloadText;
    const text = raw || '';
    if (!text.trim()) {
      setMessage('请输入抖音视频、图集链接或分享文案');
      return;
    }
    setPreviewBusy(true);
    try {
      const data = await postJson('/api/downloads/preview', { text, deep: true });
      const preview = data.preview || { items: [] };
      const merged = mergeDownloadPreviews(downloadPreview, preview, selectedDownloadUrls);
      setDownloadPreview(merged.preview);
      setSelectedDownloadUrls(merged.selectedUrls);
      setMessage(`已解析 ${preview.items?.length || 0} 个下载链接`);
      appendLog('info', `下载预览已解析 ${preview.items?.length || 0} 个链接，当前共 ${merged.preview.items.length} 个`, '下载');
    } catch (error) {
      setMessage(error.message);
      appendLog('error', error.message, '下载');
    } finally {
      setPreviewBusy(false);
    }
  }

  // alias used by the file-upload auto-preview path
  function previewDownloadWithText(text) {
    previewDownload(text);
  }

  function toggleDownloadUrl(url) {
    setSelectedDownloadUrls((current) => {
      const next = new Set(current);
      if (next.has(url)) next.delete(url);
      else next.add(url);
      return next;
    });
  }

  // Read a .txt file of links (one per line), append to downloadText, and
  // optionally auto-trigger batch preview. Supports both plaintext link lists
  // and Douyin share text where URLs are embedded after "复制此链接".
  async function handleLinkFileUpload(event, { autoPreview = false } = {}) {
    const file = event.target.files && event.target.files[0];
    // reset the input so the same file can be re-selected later
    event.target.value = '';
    if (!file) return;
    if (file.size > 1024 * 1024) {
      setMessage('文件过大 (>1MB)，请仅上传包含链接的文本文件');
      return;
    }
    let raw = '';
    try {
      raw = await file.text();
    } catch (err) {
      setMessage(`读取文件失败：${err.message}`);
      return;
    }
    const urls = extractDouyinUrlsFromText(raw);
    if (!urls.length) {
      setMessage('文件中未识别到抖音链接');
      return;
    }
    setDownloadText((cur) => {
      const existing = cur.trim();
      const combined = existing ? `${existing}\n${urls.join('\n')}` : urls.join('\n');
      return combined;
    });
    setMessage(`从文件 ${file.name} 中加载 ${urls.length} 个链接`);
    appendLog('info', `从文件 ${file.name} 中加载 ${urls.length} 个链接`, '下载');
    if (autoPreview) {
      // give setDownloadText a tick, then preview
      setTimeout(() => previewDownloadWithText(urls.join('\n')), 30);
    }
  }

  // Resolve links that may live in a given text string (used by both the
  // textarea preview and the file upload). Mirrors backend's URL extraction so
  // the client can show accurate counts.
  function extractDouyinUrlsFromText(text) {
    const seen = new Set();
    const out = [];
    // mirror backend extract_douyin_urls (backend/services.py) so client counts
    // match what the server will actually resolve.
    const re = /https?:\/\/(?:v\.douyin\.com\/|www\.douyin\.com\/|(?:www\.)?iesdouyin\.com\/).+?(?=https?:\/\/(?:v\.douyin\.com\/|www\.douyin\.com\/|(?:www\.)?iesdouyin\.com\/)|[\s"'<>,。；、)）]|$)/gi;
    let m;
    while ((m = re.exec(text)) !== null) {
      const cleaned = m[0].replace(/[.,;!?]+$/, '');
      if ((cleaned.includes('douyin.com') || cleaned.includes('iesdouyin.com')) && !seen.has(cleaned)) {
        seen.add(cleaned);
        out.push(cleaned);
      }
    }
    return out;
  }

  function toggleDownloadMedia(url, index) {
    setSelectedDownloadMedia((current) => {
      const next = { ...current };
      const set = new Set(next[url] || []);
      if (set.has(index)) set.delete(index);
      else set.add(index);
      if (set.size) next[url] = Array.from(set).sort((a, b) => a - b);
      else delete next[url];
      return next;
    });
  }

  function setDownloadMediaBulk(url, indices) {
    setSelectedDownloadMedia((current) => {
      const next = { ...current };
      if (indices && indices.length) next[url] = indices;
      else delete next[url];
      return next;
    });
  }

  function removeDownloadPreview(url) {
    const next = removeDownloadPreviewItem(downloadPreview, selectedDownloadUrls, selectedDownloadMedia, url);
    setDownloadPreview(next.preview);
    setSelectedDownloadUrls(next.selectedUrls);
    setSelectedDownloadMedia(next.selectedMedia);
  }

  return (
    <main className="app-shell">
      <SideNav active={activeSection} onActive={setActiveSection} />
      <header className="topbar">
        <div>
          <h1>Douyin Monitor</h1>
          <p>账户信息 + 实时直播监控</p>
        </div>
        {activeSection === 'sec-fansclub' && fansclubApi.current ? (
          /* 粉丝团区域: 顶栏按钮组切换为粉丝团操作 (检测类+签到) */
          <div className="top-actions">
            <button className="secondary" onClick={() => fansclubApi.current?.loadAccounts()} title="重新扫描 data/fansclub_tokens/ 下的凭证文件">
              <RotateCw size={16} /> 重载凭证
            </button>
            <button
              className="secondary"
              onClick={() => fansclubApi.current?.checkAllNow()}
              disabled={fansclubApi.current?.state?.checkAllRunning}
              title="后台并发检查全部账户（每天 0 点后打开页面也会自动执行一次）"
            >
              <Search size={16} /> {fansclubApi.current?.state?.checkAllRunning ? '全量检测中…' : '检测全部'}
            </button>
            <button
              onClick={() => fansclubApi.current?.signAll()}
              disabled={!fansclubApi.current?.state?.accounts || fansclubApi.current?.state?.signingAll}
              title="并发签到全部账户（各账户独立线程, 互不阻塞; 免费 join 端点, 幂等）"
            >
              <Activity size={16} /> {fansclubApi.current?.state?.signingAll ? '签到中…' : '一键签到全部'}
            </button>
          </div>
        ) : (
          <div className="top-actions">
            <button className="secondary" onClick={() => setLibraryOpen(true)}>
              <Plus size={16} /> 主播库
            </button>
            <button
              onClick={() => detectTargets([])}
              disabled={detectingUids.size > 0}
              title="刷新全部主播的基本信息（昵称/粉丝/直播状态）"
            >
              <RefreshCcw size={16} /> {detectingUids.size > 0 ? '检测中…' : '刷基本'}
            </button>
            <button className="icon-button" onClick={() => { loadAccounts(); loadDownloads(); }} title="刷新">
              <RefreshCcw size={18} />
            </button>
          </div>
        )}
      </header>

      <section className="content list-layout">
        {message && (
          <div className={`notice${message.includes('\n') ? ' multi' : ''}`}>
            {message}
          </div>
        )}

        <section className="panel account-panel" id="sec-accounts">
          <div className="panel-head">
            <div>
              <h2>直播监控</h2>
              <p>开播提醒 / 人数显示 — 监控目标从主播库勾选加入，下播自动结束</p>
            </div>
            <div className="panel-actions">
              <button onClick={() => setLibraryOpen(true)}>
                <Search size={16} /> 主播库 ({accounts.length})
              </button>
            </div>
          </div>

          <LiveMonitorPanel embedded onOpenLibrary={() => setLibraryOpen(true)} />
        </section>

        <FansClubPanel apiRef={fansclubApi} />
        {/* 直播间分析面板暂缓接入: 直播数据面分析仍在逆向 lab 进行, 完成后再接 (git 历史 8d79d39) */}

        <PostsPanel accounts={accounts} />

        <section className="panel download-panel" id="sec-download">
          <div className="panel-head">
            <div>
              <h2>视频下载</h2>
              <p>默认输出目录：{downloadOutputDir || 'Downloads'}</p>
            </div>
            <Download size={18} />
          </div>
          <div className="download-form">
            <textarea
              value={downloadText}
              onChange={(event) => {
                setDownloadText(event.target.value);
              }}
              placeholder={"可粘贴一个或多个抖音链接（每行一个），或完整分享文案。也可上传 .txt 文件，见下方按钮。"}
            />
            <select value={downloadMode} onChange={(event) => setDownloadMode(event.target.value)}>
              <option value={1}>仅下载媒体</option>
              <option value={2}>仅保存互动数据</option>
              <option value={3}>媒体 + 数据</option>
            </select>
            <button className={`secondary${previewBusy ? ' btn-busy' : ''}`} onClick={() => previewDownload()} disabled={busy || previewBusy}>
              <FileSearch size={16} /> {previewBusy ? '解析中' : '解析预览'}
            </button>
            <button className={busy ? 'btn-busy' : ''} onClick={submitDownload} disabled={busy || previewBusy || (downloadPreview && selectedDownloadUrls.size === 0)}>提交下载</button>
          </div>
          <div className="download-upload-row">
            <label className="file-upload-btn secondary" title="选择 .txt 文件（每行一个链接），加载后自动解析">
              <FileUp size={16} />
              <span>上传链接文件 (.txt)</span>
              <input
                type="file"
                accept=".txt,text/plain"
                onChange={(e) => handleLinkFileUpload(e, { autoPreview: true })}
                style={{ display: 'none' }}
              />
            </label>
            <button className="secondary" onClick={() => {
              const urls = extractDouyinUrlsFromText(downloadText);
              setDownloadText(urls.join('\n'));
              setMessage(urls.length ? `已整理为 ${urls.length} 个链接` : '当前文本未识别到链接');
            }} disabled={busy || previewBusy} title="将当前文本框中的所有链接整理成每行一个">
              整理链接
            </button>
            {downloadText.trim() && (
              <button className="secondary" onClick={() => setDownloadText('')} disabled={busy || previewBusy}>
                清空
              </button>
            )}
          </div>
          <div className="download-options">
            <input
              value={downloadOutputDir}
              onChange={(event) => setDownloadOutputDir(event.target.value)}
              placeholder="C:\Users\Light\Downloads"
            />
            <label className="checkbox-line">
              <input
                type="checkbox"
                checked={downloadWrapFolder}
                onChange={(event) => setDownloadWrapFolder(event.target.checked)}
              />
              按作品标题创建文件夹
            </label>
            <label className="checkbox-line" title="图片保存格式：跟随源格式或统一转码（webp→jpg 含透明图自动白底合成）">
              图片
              <select
                value={downloadImageFormat}
                onChange={(event) => setDownloadImageFormat(event.target.value)}
                style={{ marginLeft: 4 }}
              >
                <option value="auto">跟随源格式</option>
                <option value="jpg">JPG</option>
                <option value="png">PNG</option>
                <option value="webp">WebP</option>
                <option value="avif">AVIF</option>
              </select>
            </label>
            <label className="checkbox-line" title="视频保存格式：转码依赖 ffmpeg，失败自动降级保留原始 mp4；混合链接(视频+图片)中视频按此选项、图片按上面选项各自处理">
              视频
              <select
                value={downloadVideoFormat}
                onChange={(event) => setDownloadVideoFormat(event.target.value)}
                style={{ marginLeft: 4 }}
              >
                <option value="auto">原始 mp4</option>
                <option value="mp4">重编码 MP4</option>
                <option value="webm">WebM (VP9)</option>
                <option value="audio">仅提取音频 MP3</option>
              </select>
            </label>
            <button className="secondary" onClick={saveDownloadSettings} disabled={busy}>保存下载设置</button>
          </div>
          {downloadPreview && (
            <DownloadPreview
              preview={downloadPreview}
              selectedUrls={selectedDownloadUrls}
              selectedMedia={selectedDownloadMedia}
              onToggle={toggleDownloadUrl}
              onToggleMedia={toggleDownloadMedia}
              onToggleMediaBulk={setDownloadMediaBulk}
              onRemove={removeDownloadPreview}
            />
          )}
          <div className="job-list">
            {downloadJobs.map((job) => (
              <div className="job-row" key={job.id}>
                <div className="job-summary">
                  <span className={`job-status ${job.status}`}>{job.status}</span>
                  <span>{job.urls.length} 个链接</span>
                  <small>{job.output_dir}</small>
                  <small>{job.wrap_folder ? '按标题文件夹保存' : '直接保存到根目录'}</small>
                  {job.error && <strong>{job.error}</strong>}
                  <span className="job-actions">
                    {(job.status === 'queued' || job.status === 'running') && (
                      <button className="secondary small-icon" onClick={() => cancelDownloadJob(job.id)} title="取消下载（正在运行的链接完成后停止后续）">
                        <Square size={14} /> 取消
                      </button>
                    )}
                    <button className="danger icon-button small-icon" onClick={() => deleteDownloadJob(job.id)} title="删除下载任务">
                      <Trash2 size={15} />
                    </button>
                  </span>
                </div>
                <div className="job-detail">
                  {(job.results || []).map((result, index) => (
                    <small className={result.ok === false ? 'error-text' : ''} key={`${job.id}-${index}`}>
                      {result.ok === false ? '失败' : '完成'}：{result.title || result.url || result.error}
                    </small>
                  ))}
                  {(job.logs || []).slice(-4).map((entry, index) => (
                    <small key={`${job.id}-log-${index}`}>{formatTime(entry.time)} {entry.message}</small>
                  ))}
                </div>
              </div>
            ))}
            {downloadJobs.length === 0 && <div className="empty">暂无下载任务</div>}
          </div>
        </section>

        <div className="log-grid" id="sec-logs">
          <LogPanel title="检测日志" description="单次账户信息与直播信息检测结果。" logs={detectLogs} />
          <LogPanel title="下载日志" description="下载任务的解析、执行和失败信息。" logs={downloadLogs} />
        </div>
      </section>

      {libraryOpen && (
        <StreamerLibraryModal
          onClose={() => setLibraryOpen(false)}
          onChanged={loadAccounts}
        />
      )}

      {deleteTarget && (
        <DeleteAccountModal
          row={deleteTarget}
          busy={busy}
          onClose={() => setDeleteTarget(null)}
          onConfirm={() => deleteAccount(deleteTarget)}
        />
      )}
    </main>
  );
}

function Info({ label, value }) {
  return (
    <div className="info-item">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function DeleteAccountModal({ row, busy, onClose, onConfirm }) {
  const profile = row.profile || {};
  const displayName = row.label || profile.nickname || row.nickname || row.sec_uid;
  return (
    <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) onClose(); }}>
      <div className="modal compact-modal" onMouseDown={(event) => event.stopPropagation()}>
        <div className="modal-head">
          <div>
            <h2>删除账户</h2>
            <p>该操作会从本地账户列表中移除此账户。</p>
          </div>
          <Trash2 size={20} />
        </div>
        <div className="danger-summary">
          <strong>{displayName}</strong>
          <small>{row.sec_uid}</small>
        </div>
        <div className="modal-actions">
          <button className="secondary" onClick={onClose} disabled={busy}>取消</button>
          <button className="danger" onClick={onConfirm} disabled={busy}>
            {busy ? '删除中' : '确认删除'}
          </button>
        </div>
      </div>
    </div>
  );
}

createRoot(document.getElementById('root')).render(<App />);
