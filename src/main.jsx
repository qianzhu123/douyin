import React from 'react';
import { createRoot } from 'react-dom/client';
import { Activity, Clock, Copy, Download, ExternalLink, Eye, EyeOff, FileSearch, FileUp, GripVertical, Plus, RefreshCcw, Search, Square, Trash2 } from 'lucide-react';
import { formatDuration } from './duration.js';
import { buildDownloadInputText, mergeDownloadPreviews, removeDownloadPreviewItem } from './downloadPreviewState.js';
import './styles.css';

const API_BASE = 'http://127.0.0.1:8000';

function formatNumber(value) {
  if (value === null || value === undefined || value === '') return '-';
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return String(value);
  if (numeric >= 100000000) return `${(numeric / 100000000).toFixed(1)}亿`;
  if (numeric >= 10000) return `${(numeric / 10000).toFixed(1)}万`;
  return numeric.toLocaleString();
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

function mergeAccount(account, watch) {
  const watched = (watch.profiles || []).find((item) => item.sec_uid === account.sec_uid);
  const profile = watched?.profile || account.last_profile || {};
  return {
    ...account,
    liveProfile: watched?.profile,
    profile,
    last_checked_at: watched ? (watch.last_checked_at || account.last_checked_at) : account.last_checked_at,
  };
}

function App() {
  const [accounts, setAccounts] = React.useState([]);
  const [watchJobs, setWatchJobs] = React.useState([]);
  const [watchCurrentId, setWatchCurrentId] = React.useState('');
  const [downloadJobs, setDownloadJobs] = React.useState([]);
  const [selectedUid, setSelectedUid] = React.useState('');
  const [checkedUids, setCheckedUids] = React.useState(new Set());
  const [contextInfo, setContextInfo] = React.useState(null);
  const [message, setMessage] = React.useState('');
  const [busy, setBusy] = React.useState(false);
  const [detectingUids, setDetectingUids] = React.useState(new Set());
  const [addOpen, setAddOpen] = React.useState(false);
  const [pollOpen, setPollOpen] = React.useState(false);
  const [deleteTarget, setDeleteTarget] = React.useState(null);
  const [pollTargets, setPollTargets] = React.useState([]);
  const [downloadText, setDownloadText] = React.useState('');
  const [downloadMode, setDownloadMode] = React.useState(1);
  const [downloadOutputDir, setDownloadOutputDir] = React.useState('');
  const [downloadWrapFolder, setDownloadWrapFolder] = React.useState(false);
  const [downloadPreview, setDownloadPreview] = React.useState(null);
  const [selectedDownloadUrls, setSelectedDownloadUrls] = React.useState(new Set());
  const [selectedDownloadMedia, setSelectedDownloadMedia] = React.useState({});
  const [previewBusy, setPreviewBusy] = React.useState(false);
  const [draggedUid, setDraggedUid] = React.useState('');
  const [hiddenUids, setHiddenUids] = React.useState(() => {
    try {
      return new Set(JSON.parse(window.localStorage.getItem('douyinHiddenUids') || '[]'));
    } catch (error) {
      return new Set();
    }
  });
  const [activityLogs, setActivityLogs] = React.useState(() => {
    try {
      return JSON.parse(window.localStorage.getItem('douyinActivityLogs') || '[]');
    } catch (error) {
      return [];
    }
  });
  const accountsRef = React.useRef([]);
  const contextMenuRef = React.useRef(null);

  // Backward-compat: synthesize a single `watch` object from the current job so
  // mergeAccount / pollingLogs / toolbar (watch.running) keep working unchanged.
  const watch = React.useMemo(() => {
    if (!watchJobs.length) return { running: false, profiles: [], events: [], targets: [] };
    const current = watchJobs.find((job) => job.id === watchCurrentId) || watchJobs[watchJobs.length - 1];
    return current || { running: false, profiles: [], events: [], targets: [] };
  }, [watchJobs, watchCurrentId]);

  const rows = React.useMemo(
    () => accounts.map((account) => mergeAccount(account, watch)),
    [accounts, watch],
  );
  const selected = rows.find((row) => row.sec_uid === selectedUid) || rows[0];
  const checkedTargets = React.useMemo(() => Array.from(checkedUids), [checkedUids]);
  const allChecked = rows.length > 0 && rows.every((row) => checkedUids.has(row.sec_uid));
  const detectLogs = React.useMemo(
    () => activityLogs.filter((entry) => entry.source === '检测').slice(0, 80),
    [activityLogs],
  );
  const pollingLogs = React.useMemo(() => {
    const watchLogs = (watch.events || []).map((event) => ({
      time: event.time,
      level: event.level,
      message: event.message,
      source: '轮询',
    }));
    return [...activityLogs.filter((entry) => entry.source === '轮询'), ...watchLogs]
      .sort((a, b) => String(b.time).localeCompare(String(a.time)))
      .slice(0, 80);
  }, [activityLogs, watch.events]);
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
    window.localStorage.setItem('douyinHiddenUids', JSON.stringify(Array.from(hiddenUids)));
  }, [hiddenUids]);

  React.useEffect(() => {
    accountsRef.current = accounts;
  }, [accounts]);

  React.useEffect(() => {
    if (!contextInfo) return undefined;
    function closeContextMenu(event) {
      if (contextMenuRef.current?.contains(event.target)) return;
      setContextInfo(null);
    }
    function closeOnEscape(event) {
      if (event.key === 'Escape') setContextInfo(null);
    }
    document.addEventListener('pointerdown', closeContextMenu);
    document.addEventListener('keydown', closeOnEscape);
    return () => {
      document.removeEventListener('pointerdown', closeContextMenu);
      document.removeEventListener('keydown', closeOnEscape);
    };
  }, [contextInfo]);

  const loadAccounts = React.useCallback(async () => {
    const response = await fetch(`${API_BASE}/api/users`);
    if (!response.ok) throw new Error('账户列表加载失败');
    const data = await response.json();
    setAccounts(data.users || []);
    setSelectedUid((current) => current || data.users?.[0]?.sec_uid || '');
    setCheckedUids((current) => {
      const valid = new Set((data.users || []).map((user) => user.sec_uid));
      return new Set(Array.from(current).filter((uid) => valid.has(uid)));
    });
  }, []);

  const loadWatch = React.useCallback(async () => {
    const response = await fetch(`${API_BASE}/api/watch/jobs`);
    if (!response.ok) return;
    const data = await response.json();
    setWatchJobs(data.jobs || []);
    setWatchCurrentId(data.current_id || '');
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
  }, []);

  React.useEffect(() => {
    loadAccounts().catch((error) => setMessage(error.message));
    loadWatch().catch(() => {});
    loadDownloads().catch(() => {});
    loadSettings().catch(() => {});
  }, [loadAccounts, loadWatch, loadDownloads, loadSettings]);

  React.useEffect(() => {
    const timer = window.setInterval(() => {
      loadAccounts().catch(() => {});
      loadWatch().catch(() => {});
      loadDownloads().catch(() => {});
    }, 3000);
    return () => window.clearInterval(timer);
  }, [loadAccounts, loadWatch, loadDownloads]);

  async function postJson(path, body = {}) {
    let response;
    try {
      response = await fetch(`${API_BASE}${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
    } catch (error) {
      throw new Error('无法连接后端服务，请确认 127.0.0.1:8000 正在运行。');
    }
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '请求失败');
    return data;
  }

  async function detectTargets(targets = []) {
    const actualTargets = targets.length ? targets : rows.map((row) => row.sec_uid);
    const pendingTargets = actualTargets.filter((uid) => !detectingUids.has(uid));
    if (pendingTargets.length === 0) return;

    setDetectingUids((current) => new Set([...current, ...pendingTargets]));
    setMessage(`正在检测 ${pendingTargets.length} 个账户的信息和直播状态...`);
    try {
      const data = await postJson('/api/query', { targets: pendingTargets });
      await loadAccounts();
      const results = data.results || [];
      const failures = results.filter((result) => !result.ok);
      results.forEach((result) => {
        const label = result.label || result.sec_uid;
        appendLog(result.ok ? 'info' : 'error', result.ok ? `${label} 检测完成` : `${label} 检测失败：${result.error || '可能触发风控或接口暂无数据'}`, '检测');
      });
      setMessage(failures.length ? `已检测 ${results.length} 个账户，${failures.length} 个失败` : `已检测 ${results.length} 个账户`);
    } catch (error) {
      setMessage(error.message);
      appendLog('error', error.message, '检测');
    } finally {
      setDetectingUids((current) => {
        const next = new Set(current);
        pendingTargets.forEach((uid) => next.delete(uid));
        return next;
      });
    }
  }

  function openPollModal(targets) {
    const runningTargets = (watch.targets || []).map((target) => target.sec_uid);
    setPollTargets(targets.length ? targets : runningTargets);
    setPollOpen(true);
  }

  function toggleAllAccounts() {
    setCheckedUids((current) => {
      if (rows.length > 0 && rows.every((row) => current.has(row.sec_uid))) {
        return new Set();
      }
      return new Set(rows.map((row) => row.sec_uid));
    });
  }

  function toggleAccount(uid) {
    setCheckedUids((current) => {
      const next = new Set(current);
      if (next.has(uid)) next.delete(uid);
      else next.add(uid);
      return next;
    });
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      setMessage('已复制');
    } catch (error) {
      setMessage('复制失败');
    }
  }

  function toggleHidden(uid) {
    setHiddenUids((current) => {
      const next = new Set(current);
      if (next.has(uid)) next.delete(uid);
      else next.add(uid);
      return next;
    });
  }

  async function reorderAccounts(nextRows) {
    setAccounts(nextRows);
    try {
      await postJson('/api/users/reorder', { sec_uids: nextRows.map((row) => row.sec_uid) });
      appendLog('info', '账户顺序已保存', '界面');
    } catch (error) {
      setMessage(error.message);
      appendLog('error', error.message, '界面');
      loadAccounts().catch(() => {});
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
      setContextInfo(null);
      setCheckedUids((current) => {
        const next = new Set(current);
        next.delete(row.sec_uid);
        return next;
      });
      setHiddenUids((current) => {
        const next = new Set(current);
        next.delete(row.sec_uid);
        return next;
      });
      setSelectedUid((current) => (current === row.sec_uid ? nextUsers[0]?.sec_uid || '' : current));
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

  function handleRowDragOver(event, targetUid) {
    event.preventDefault();
    const edge = 72;
    if (event.clientY < edge) {
      window.scrollBy({ top: -24, behavior: 'auto' });
    } else if (window.innerHeight - event.clientY < edge) {
      window.scrollBy({ top: 24, behavior: 'auto' });
    }
    if (!draggedUid || draggedUid === targetUid) return;

    setAccounts((current) => {
      const fromIndex = current.findIndex((account) => account.sec_uid === draggedUid);
      const toIndex = current.findIndex((account) => account.sec_uid === targetUid);
      if (fromIndex < 0 || toIndex < 0 || fromIndex === toIndex) return current;
      const next = [...current];
      const [moved] = next.splice(fromIndex, 1);
      next.splice(toIndex, 0, moved);
      accountsRef.current = next;
      return next;
    });
  }

  function finishDrag() {
    if (!draggedUid) return;
    setDraggedUid('');
    reorderAccounts(accountsRef.current);
  }

  async function startPolling({ interval, durationMinutes }) {
    setBusy(true);
    try {
      const label = `轮询 ${new Date().toLocaleTimeString()}`;
      await postJson('/api/watch/start', {
        targets: pollTargets,
        interval,
        duration_minutes: durationMinutes,
        label,
      });
      await loadWatch();
      setPollOpen(false);
      setMessage('轮询检测已启动');
      appendLog('info', `轮询检测已启动：${pollTargets.length} 个账户，间隔 ${interval}s，持续 ${durationMinutes} 分钟`, '轮询');
    } catch (error) {
      setMessage(error.message);
      appendLog('error', error.message, '轮询');
    } finally {
      setBusy(false);
    }
  }

  async function stopPolling() {
    const jobId = watchCurrentId || (watchJobs[watchJobs.length - 1] || {}).id;
    if (!jobId) {
      setMessage('当前没有运行中的轮询任务');
      return;
    }
    await stopPollingJob(jobId);
  }

  async function stopPollingJob(jobId) {
    if (!jobId) return;
    setBusy(true);
    try {
      await postJson(`/api/watch/${jobId}/stop`, {});
      await loadWatch();
      setMessage('轮询检测已停止');
      appendLog('info', '轮询检测已停止', '轮询');
      await loadAccounts();
    } catch (error) {
      setMessage(error.message);
      appendLog('error', error.message, '轮询');
    } finally {
      setBusy(false);
    }
  }

  async function adjustPollingJob(jobId, patch) {
    if (!jobId) return;
    try {
      await postJson(`/api/watch/${jobId}/adjust`, patch);
      await loadWatch();
    } catch (error) {
      setMessage(error.message);
      appendLog('error', error.message, '轮询');
    }
  }

  async function removePollingJob(jobId) {
    if (!jobId) return;
    setBusy(true);
    try {
      const resp = await fetch(`${API_BASE}/api/watch/${jobId}`, { method: 'DELETE' });
      if (!resp.ok) throw new Error(`删除轮询任务失败 (${resp.status})`);
      await loadWatch();
      setMessage('轮询任务已移除');
      appendLog('info', '轮询任务已移除', '轮询');
      await loadAccounts();
    } catch (error) {
      setMessage(error.message);
      appendLog('error', error.message, '轮询');
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
      });
      const settings = data.settings || {};
      setDownloadOutputDir(settings.download_output_dir || '');
      setDownloadWrapFolder(Boolean(settings.wrap_download_folder));
      setMessage('下载设置已保存');
      appendLog('info', `下载设置已保存：${settings.download_output_dir || ''}${settings.wrap_download_folder ? '，按标题创建文件夹' : '，直接保存到根目录'}`, '下载');
    } catch (error) {
      setMessage(error.message);
      appendLog('error', error.message, '下载');
    } finally {
      setBusy(false);
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
      <header className="topbar">
        <div>
          <h1>Douyin Monitor</h1>
          <p>账户主页信息与直播状态列表</p>
        </div>
        <div className="top-actions">
          <button className="secondary" onClick={() => setAddOpen(true)}>
            <Plus size={16} /> 添加账户
          </button>
          <button
            onClick={() => detectTargets(checkedTargets)}
            disabled={checkedTargets.length === 0 || checkedTargets.every((uid) => detectingUids.has(uid))}
          >
            <RefreshCcw size={16} /> 检测选中
          </button>
          <button onClick={() => openPollModal(checkedTargets)} disabled={busy || checkedTargets.length === 0 || watchJobs.some((job) => job.running)}>
            <Activity size={16} /> 轮询选中
          </button>
          {watchJobs.length > 0 && (
            <button className="secondary" onClick={() => setPollOpen(true)} disabled={busy}>
              <Clock size={16} /> 调整轮询
            </button>
          )}
          {watchJobs.length > 0 && (
            <button className="secondary" onClick={stopPolling} disabled={busy}>
              <Square size={16} /> 停止当前
            </button>
          )}
          <button className="icon-button" onClick={() => { loadAccounts(); loadWatch(); loadDownloads(); }} title="刷新">
            <RefreshCcw size={18} />
          </button>
        </div>
      </header>

      <section className="content list-layout">
        {message && <div className="notice">{message}</div>}

        <section className="panel account-panel">
          <div className="panel-head">
            <div>
              <h2>账户列表</h2>
              <p>{watch.running ? `轮询中，第 ${watch.round || 0} 轮，间隔 ${watch.interval}s，持续 ${watch.duration_minutes || 30} 分钟，上次 ${formatTime(watch.last_checked_at)}` : '账户主页信息与直播信息'}</p>
            </div>
            <div className="panel-actions">
              {hiddenUids.size > 0 && (
                <button className="secondary" onClick={() => setHiddenUids(new Set())}>
                  <Eye size={16} /> 恢复信息 {hiddenUids.size}
                </button>
              )}
              {watch.running && (
                <button className="secondary" onClick={stopPolling} disabled={busy}>
                  <Square size={16} /> 停止当前
                </button>
              )}
            </div>
          </div>

          {watchJobs.length > 0 && (
            <div className="watch-jobs">
              {watchJobs.map((job) => (
                <WatchJobCard
                  key={job.id}
                  job={job}
                  busy={busy}
                  onStop={() => stopPollingJob(job.id)}
                  onAdjust={(patch) => adjustPollingJob(job.id, patch)}
                  onRemove={() => removePollingJob(job.id)}
                />
              ))}
            </div>
          )}

          <div className="account-list">
            <div className="account-header">
              <label className="select-cell">
                <input type="checkbox" checked={allChecked} onChange={toggleAllAccounts} />
              </label>
              <span>账户</span>
              <span>粉丝</span>
              <span>关注</span>
              <span>获赞</span>
              <span>IP</span>
              <span>直播信息</span>
              <span>人数</span>
              <span>上次检测</span>
              <span>操作</span>
            </div>
            {rows.map((row) => (
              <AccountRow
                key={row.sec_uid}
                row={row}
                selected={row.sec_uid === selectedUid}
                checked={checkedUids.has(row.sec_uid)}
                busy={busy}
                detecting={detectingUids.has(row.sec_uid)}
                hidden={hiddenUids.has(row.sec_uid)}
                dragging={draggedUid === row.sec_uid}
                onSelect={() => setSelectedUid(row.sec_uid)}
                onToggle={() => toggleAccount(row.sec_uid)}
                onHide={() => toggleHidden(row.sec_uid)}
                onDragStart={() => setDraggedUid(row.sec_uid)}
                onDragOver={(event) => handleRowDragOver(event, row.sec_uid)}
                onDragEnd={finishDrag}
                onContext={(event) => {
                  event.preventDefault();
                  setContextInfo({ row, x: event.clientX, y: event.clientY });
                }}
                onDetect={() => detectTargets([row.sec_uid])}
                onPoll={() => openPollModal([row.sec_uid])}
                onDelete={() => setDeleteTarget(row)}
              />
            ))}
            {rows.length === 0 && <div className="empty">暂无账户，请先添加账户</div>}
          </div>
        </section>

        {selected && <DetailPanel row={selected} hidden={hiddenUids.has(selected.sec_uid)} />}

        <section className="panel download-panel">
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

        <div className="log-grid">
          <LogPanel title="检测日志" description="单次账户信息与直播信息检测结果。" logs={detectLogs} />
          <LogPanel title="轮询日志" description="轮询启动、停止、每轮检测摘要和开播/下播事件。" logs={pollingLogs} />
          <LogPanel title="下载日志" description="下载任务的解析、执行和失败信息。" logs={downloadLogs} />
        </div>
      </section>

      {addOpen && (
        <AddAccountModal
          busy={busy}
          setBusy={setBusy}
          setMessage={setMessage}
          onClose={() => setAddOpen(false)}
          onAdded={async () => {
            await loadAccounts();
            setAddOpen(false);
          }}
        />
      )}

      {pollOpen && (
        <PollModal
          targetCount={pollTargets.length}
          initialInterval={watch.interval || 30}
          initialDurationMinutes={watch.duration_minutes || 30}
          onClose={() => setPollOpen(false)}
          onStart={startPolling}
        />
      )}

      {contextInfo && (
        <div
          ref={contextMenuRef}
          className="context-menu"
          style={{ left: contextInfo.x, top: contextInfo.y }}
        >
          <strong>{contextInfo.row.profile?.unique_id || contextInfo.row.unique_id || '无抖音号'}</strong>
          <small>{contextInfo.row.sec_uid}</small>
          <button className="secondary" onClick={() => {
            copyText(contextInfo.row.profile?.unique_id || contextInfo.row.unique_id || contextInfo.row.sec_uid);
            setContextInfo(null);
          }}>
            <Copy size={14} /> 复制
          </button>
        </div>
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

function AccountRow({ row, selected, checked, busy, detecting, hidden, dragging, onSelect, onToggle, onHide, onDragStart, onDragOver, onDragEnd, onContext, onDetect, onPoll, onDelete }) {
  const profile = row.profile || {};
  const live = profile.live_status === 1;
  const failed = row.last_ok === false;
  const masked = '******';
  return (
    <div
      className={`${selected ? 'account-row selected' : 'account-row'}${hidden ? ' hidden-row' : ''}${dragging ? ' dragging' : ''}`}
      draggable
      onClick={onSelect}
      onContextMenu={onContext}
      onDragStart={onDragStart}
      onDragOver={onDragOver}
      onDragEnd={onDragEnd}
      role="button"
      tabIndex={0}
    >
      <label className="select-cell" onClick={(event) => event.stopPropagation()}>
        <input type="checkbox" checked={checked} onChange={onToggle} />
      </label>
      <span className="account-name">
        <GripVertical size={15} className="drag-handle" />
        <span>
          <strong>{hidden ? masked : (row.label || profile.nickname || '未备注')}</strong>
          {!hidden && failed && <small className="error-text">{row.last_error || '检测失败'}</small>}
        </span>
      </span>
      <span>{hidden ? masked : formatNumber(profile.follower_count || row.follower_count)}</span>
      <span>{hidden ? masked : formatNumber(profile.following_count || row.following_count)}</span>
      <span>{hidden ? masked : formatNumber(profile.total_favorited || row.total_favorited)}</span>
      <span>{hidden ? masked : (profile.ip_location || row.ip_location || '-')}</span>
      <span>
        <span className={hidden ? 'hidden-pill' : failed ? 'failed-pill' : live ? 'live-pill' : 'idle-pill'} title={hidden ? '' : row.last_error || ''}>
          {hidden ? masked : failed ? '检测失败' : live ? '直播中' : '未直播'}
        </span>
      </span>
      <span>{hidden ? masked : live ? formatNumber(profile.live_viewers) : '-'}</span>
      <span>{hidden ? masked : formatTime(row.last_checked_at)}</span>
      <span className="row-actions" onClick={(event) => event.stopPropagation()}>
        <button onClick={onDetect} disabled={detecting}>{detecting ? '检测中' : '检测'}</button>
        <button className="secondary" onClick={onPoll} disabled={busy}>轮询</button>
        <button className="secondary icon-button small-icon" onClick={onHide} title={hidden ? '恢复信息' : '隐藏信息'}>
          {hidden ? <Eye size={15} /> : <EyeOff size={15} />}
        </button>
        <button className="danger icon-button small-icon" onClick={onDelete} disabled={busy} title="删除账户">
          <Trash2 size={15} />
        </button>
        <a href={row.url} target="_blank" rel="noreferrer" title="打开主页">
          <ExternalLink size={16} />
        </a>
      </span>
    </div>
  );
}

function DetailPanel({ row, hidden }) {
  const profile = row.profile || {};
  const masked = '******';
  const display = (value) => (hidden ? masked : value);
  return (
    <section className="panel detail-panel">
      <div className="panel-head">
        <div>
          <h2>{hidden ? masked : (row.label || profile.nickname || '账户详情')}</h2>
          <p>{hidden ? masked : '最近一次检测结果'}</p>
        </div>
      </div>
      <div className="detail-grid">
        <Info label="关注" value={display(formatNumber(profile.following_count || row.following_count))} />
        <Info label="粉丝" value={display(formatNumber(profile.follower_count || row.follower_count))} />
        <Info label="获赞" value={display(formatNumber(profile.total_favorited || row.total_favorited))} />
        <Info label="IP地址" value={display(profile.ip_location || row.ip_location || '-')} />
        <Info label="直播状态" value={display(profile.live_status === 1 ? '直播中' : '未直播')} />
        <Info label="直播间人数" value={display(formatNumber(profile.live_viewers))} />
        <Info label="开播时间" value={display(formatTime(profile.live_start_at))} />
        <Info label="持续时间" value={display(formatDuration(profile.live_duration_seconds))} />
        <Info label="上次检测" value={display(formatTime(row.last_checked_at))} />
      </div>
      {profile.live_room && profile.live_status === 1 && !hidden && (
        <LiveRoomCard data={profile.live_room} />
      )}
    </section>
  );
}

function LiveRoomCard({ data }) {
  const partition = data.partition || {};
  const similar = data.similar_rooms || [];
  const audience = data.audience_rank_top || [];
  const meta = data.audience_rank_meta || {};
  const stream = data.stream_url || {};
  const anchor = data.anchor || {};
  return (
    <div className="live-room-card">
      <div className="live-room-card-head">
        <span className="live-dot" />
        <h3>{data.title || '直播间详情'}</h3>
        <a className="live-room-link" href={`https://live.douyin.com/${data.web_rid}`} target="_blank" rel="noreferrer">
          打开直播间
        </a>
      </div>
      <div className="live-room-grid">
        <Info label="在线观看" value={formatNumber(data.viewers) !== '-' ? formatNumber(data.viewers) : (data.user_count_str || '-')} />
        <Info label="累计观看" value={formatNumber(data.total_user_str)} />
        <Info label="本场点赞" value={formatNumber(data.like_count)} />
        <Info label="分区" value={partition.title || '-'} />
      </div>
      {similar.length > 0 && (
        <div className="live-room-tags">
          <span className="live-room-tags-label">同推房：</span>
          {similar.map((room, index) => (
            <a key={index} className="live-room-tag" href={`https://live.douyin.com/${room.web_rid}`} target="_blank" rel="noreferrer">
              {room.title || room.web_rid} · {room.user_count_str}
            </a>
          ))}
        </div>
      )}
      {audience.length > 0 && (
        <div className="audience-rank">
          <div className="audience-rank-head">
            <h4>观众榜 Top{audience.length}</h4>
            <span className="audience-rank-meta">{meta.user_count_desc || ''}</span>
          </div>
          <table>
            <thead>
              <tr>
                <th>#</th>
                <th>昵称</th>
                <th>付费等级</th>
                <th>粉丝团</th>
              </tr>
            </thead>
            <tbody>
              {audience.map((u) => (
                <tr key={u.sec_uid || u.rank}>
                  <td>{u.rank}</td>
                  <td>{u.nickname || '-'}</td>
                  <td>{u.pay_grade_level ?? '-'}</td>
                  <td>{u.fans_club_level ?? '-'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="live-room-foot">
        <span className="live-room-anchor">主播：{anchor.nickname || '-'}</span>
        {data.qrcode_url && <img className="live-room-qr" src={data.qrcode_url} alt="直播间二维码" />}
      </div>
      <p className="live-room-stream">流地址：{stream.hls_pull_url || stream.flv_pull_url || '未开放'}</p>
    </div>
  );
}

function DownloadPreview({ preview, selectedUrls, selectedMedia, onToggle, onToggleMedia, onToggleMediaBulk, onRemove }) {
  const dragState = React.useRef({ active: false, url: '', mode: 'add', last: -1 });

  const beginDrag = (event, item, firstIndex) => {
    if (event.button !== 0) return;
    const picked = new Set(selectedMedia[item.url] || []);
    dragState.current = {
      active: true,
      url: item.url,
      mode: picked.has(firstIndex) ? 'remove' : 'add',
      last: firstIndex,
    };
    applyDrag(firstIndex);
    event.preventDefault();
  };

  const applyDrag = (index) => {
    const ds = dragState.current;
    if (!ds.active) return;
    if (ds.last === index) return;
    ds.last = index;
    const current = new Set(selectedMedia[ds.url] || []);
    if (ds.mode === 'add') current.add(index);
    else current.delete(index);
    onToggleMediaBulk(ds.url, Array.from(current).sort((a, b) => a - b));
  };

  const endDrag = () => {
    if (dragState.current.active) dragState.current.active = false;
  };

  React.useEffect(() => {
    window.addEventListener('mouseup', endDrag);
    return () => window.removeEventListener('mouseup', endDrag);
  }, []);

  return (
    <div className="preview-list">
      {(preview.items || []).map((item) => {
        const isImageType = item.type === 'image' || item.type === 'slide';
        const media = item.media || [];
        const multiImage = isImageType && media.length > 1;
        const picked = new Set(selectedMedia[item.url] || []);
        const allPicked = multiImage && picked.size === media.length;
        const nonePicked = multiImage && picked.size === 0;
        const toggleAll = () => {
          if (allPicked) {
            media.forEach((m) => onToggleMedia(item.url, m.index));
          } else {
            const toAdd = media.filter((m) => !picked.has(m.index));
            toAdd.forEach((m) => onToggleMedia(item.url, m.index));
          }
        };
        return (
        <article className="preview-row" key={item.url}>
          <label className="select-cell">
            <input type="checkbox" checked={selectedUrls.has(item.url)} onChange={() => onToggle(item.url)} />
          </label>
          <div className="preview-cover">
            {item.cover_url ? <img src={item.cover_url} alt="" /> : <span>{item.type}</span>}
          </div>
          <div className="preview-main">
            <strong>{item.title || `链接 ${item.index}`}</strong>
            <small>{item.author || item.url}</small>
            <span>
              {item.type || 'link'} · {item.duration ? formatDuration(item.duration) : '时长未知'} · {media.length || 1} 个媒体项
              {multiImage && picked.size > 0 ? ` · 已选 ${picked.size} 张` : ''}
            </span>
            {item.error && <small className="error-text">{item.error}</small>}
            {multiImage && (
              <div
                className="media-strip"
                onMouseLeave={endDrag}
              >
                <button className="secondary media-toggle-all" onClick={toggleAll} type="button">
                  {allPicked ? '取消全选' : '全选'}
                </button>
                <small className="media-hint">提示：按住鼠标在图片上拖动可批量勾选/取消</small>
                {media.map((m) => {
                  const checked = picked.has(m.index);
                  return (
                    <div
                      className={`media-chip${checked ? ' picked' : ''}`}
                      key={`${item.url}-${m.index}`}
                      title={`第 ${m.index} 张 · ${m.type}`}
                      onMouseDown={(e) => beginDrag(e, item, m.index)}
                      onMouseEnter={() => applyDrag(m.index)}
                      onClick={(e) => { e.preventDefault(); onToggleMedia(item.url, m.index); }}
                    >
                      {m.cover_url ? <img src={m.cover_url} alt="" loading="lazy" draggable="false" /> : <span>{m.type}</span>}
                      <small>{m.index}</small>
                    </div>
                  );
                })}
                {nonePicked && <small className="error-text">未勾选图片时将下载全部 {media.length} 张</small>}
              </div>
            )}
          </div>
          <button className="icon-button preview-remove" onClick={() => onRemove(item.url)} title="删除该预览" type="button">
            <Trash2 size={16} />
          </button>
        </article>
        );
      })}
    </div>
  );
}

function LogPanel({ title, description, logs }) {
  return (
    <section className="panel log-panel">
      <div className="panel-head">
        <div>
          <h2>{title}</h2>
          <p>{description}</p>
        </div>
      </div>
      <div className="log-box">
        {logs.map((entry, index) => (
          <div className={`log-line ${entry.level}`} key={`${entry.time}-${index}`}>
            <span>{formatTime(entry.time)}</span>
            <strong>{entry.source}</strong>
            <p>{entry.message}</p>
          </div>
        ))}
        {logs.length === 0 && <div className="empty">暂无日志</div>}
      </div>
    </section>
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

function AddAccountModal({ busy, setBusy, setMessage, onClose, onAdded }) {
  const [keyword, setKeyword] = React.useState('');
  const [candidates, setCandidates] = React.useState([]);

  async function postJson(path, body = {}) {
    let response;
    try {
      response = await fetch(`${API_BASE}${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
    } catch (error) {
      throw new Error('无法连接后端服务，请确认 127.0.0.1:8000 正在运行。');
    }
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '请求失败');
    return data;
  }

  async function searchUsers() {
    if (!keyword.trim()) {
      setMessage('请输入需要添加的账户名称');
      return;
    }
    setBusy(true);
    try {
      const data = await postJson('/api/users/search', { keyword: keyword.trim() });
      setCandidates(data.candidates || []);
      setMessage(`找到 ${data.candidates?.length || 0} 个候选用户`);
    } catch (error) {
      setMessage(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function addCandidate(candidate) {
    setBusy(true);
    try {
      await postJson('/api/users', {
        ...candidate,
        label: candidate.nickname || candidate.unique_id || keyword,
      });
      setMessage('账户已保存到 data/users.json');
      await onAdded();
    } catch (error) {
      setMessage(error.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) onClose(); }}>
      <div className="modal" onMouseDown={(event) => event.stopPropagation()}>
        <div className="modal-head">
          <div>
            <h2>添加账户</h2>
          </div>
          <button className="secondary" onClick={onClose}>关闭</button>
        </div>
        <div className="search-form">
          <input
            value={keyword}
            onChange={(event) => setKeyword(event.target.value)}
            onKeyDown={(event) => { if (event.key === 'Enter') searchUsers(); }}
            placeholder="输入账户名称"
          />
          <button onClick={searchUsers} disabled={busy}>
            <Search size={16} /> 搜索
          </button>
        </div>
        <div className="candidate-list">
          {candidates.map((candidate) => (
            <article className="candidate-row" key={candidate.sec_uid}>
              <img src={candidate.avatar_url || ''} alt="" />
              <div>
                <strong>{candidate.nickname || '未命名用户'}</strong>
                <small>{candidate.unique_id ? `抖音号：${candidate.unique_id}` : candidate.sec_uid.slice(0, 18)}</small>
                <span>
                  粉丝 {formatNumber(candidate.follower_count)} · 关注 {formatNumber(candidate.following_count)} · 获赞 {formatNumber(candidate.total_favorited)}
                  {candidate.ip_location ? ` · ${candidate.ip_location}` : ''}
                </span>
              </div>
              <button onClick={() => addCandidate(candidate)} disabled={busy}>
                <Plus size={16} /> 添加
              </button>
            </article>
          ))}
          {candidates.length === 0 && <div className="empty">搜索结果会显示在这里</div>}
        </div>
      </div>
    </div>
  );
}

function WatchJobCard({ job, busy, onStop, onAdjust, onRemove }) {
  const [editing, setEditing] = React.useState(false);
  const [interval, setIntervalValue] = React.useState(job.interval || 30);
  const [durationMinutes, setDurationMinutes] = React.useState(job.duration_minutes || 30);
  React.useEffect(() => {
    setIntervalValue(job.interval || 30);
    setDurationMinutes(job.duration_minutes || 30);
  }, [job.interval, job.duration_minutes]);

  const targetCount = (job.targets || []).length;
  const lastEvent = (job.events || [])[0];
  return (
    <div className={`watch-job-card${job.running ? ' running' : ''}`}>
      <div className="watch-job-head">
        <span className="watch-job-label">{job.label || job.id.slice(0, 8)}</span>
        <span className={`watch-job-state ${job.running ? 'live' : 'idle'}`}>
          {job.running ? '运行中' : '已停止'}
        </span>
      </div>
      <div className="watch-job-meta">
        <span>目标 {targetCount}</span>
        <span>第 {job.round || 0} 轮</span>
        <span>间隔 {job.interval}s</span>
        <span>持续 {job.duration_minutes || 30}m</span>
        <span>上次 {formatTime(job.last_checked_at)}</span>
      </div>
      {editing && (
        <div className="watch-job-edit">
          <label>间隔(s)
            <input type="number" min="5" value={interval} onChange={(event) => setIntervalValue(Number(event.target.value) || 5)} />
          </label>
          <label>持续(m)
            <input type="number" min="1" value={durationMinutes} onChange={(event) => setDurationMinutes(Number(event.target.value) || 1)} />
          </label>
          <button className="secondary" onClick={() => { onAdjust({ interval, duration_minutes: durationMinutes }); setEditing(false); }}>应用</button>
          <button className="secondary" onClick={() => setEditing(false)}>取消</button>
        </div>
      )}
      {lastEvent && <div className="watch-job-event">{lastEvent.message}</div>}
      <div className="watch-job-actions">
        <button className="secondary" onClick={() => setEditing((value) => !value)} disabled={busy}>调整</button>
        <button className="secondary" onClick={onStop} disabled={busy || !job.running}>停止</button>
        <button className="danger" onClick={onRemove} disabled={busy}>移除</button>
      </div>
    </div>
  );
}

function PollModal({ targetCount, initialInterval, initialDurationMinutes, onClose, onStart }) {
  const [interval, setIntervalValue] = React.useState(initialInterval || 30);
  const [durationMinutes, setDurationMinutes] = React.useState(initialDurationMinutes || 30);

  return (
    <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div className="modal compact-modal" onMouseDown={(event) => event.stopPropagation()}>
        <div className="modal-head">
          <div>
            <h2>轮询检测</h2>
            <p>将检测 {targetCount} 个账户。</p>
          </div>
          <Clock size={20} />
        </div>
        <label className="field">
          间隔秒数
          <input value={interval} onChange={(event) => setIntervalValue(event.target.value)} inputMode="numeric" />
        </label>
        <label className="field">
          检测时长（分钟）
          <input value={durationMinutes} onChange={(event) => setDurationMinutes(event.target.value)} inputMode="numeric" />
        </label>
        <div className="modal-actions">
          <button className="secondary" onClick={onClose}>取消</button>
          <button onClick={() => onStart({ interval: Number(interval) || 30, durationMinutes: Number(durationMinutes) || 30 })}>开始轮询</button>
        </div>
      </div>
    </div>
  );
}

createRoot(document.getElementById('root')).render(<App />);
