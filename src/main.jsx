import React from 'react';
import { createRoot } from 'react-dom/client';
import { Activity, Clock, Copy, Download, ExternalLink, Eye, EyeOff, FileSearch, GripVertical, Plus, RefreshCcw, Search, Square, Trash2 } from 'lucide-react';
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

function formatDuration(seconds) {
  if (!seconds && seconds !== 0) return '-';
  const total = Number(seconds);
  if (!Number.isFinite(total)) return '-';
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (hours > 0) return `${hours}小时${minutes}分`;
  return `${minutes}分钟`;
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
  const [watch, setWatch] = React.useState({ running: false, profiles: [], events: [], targets: [] });
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
  const [downloadPreview, setDownloadPreview] = React.useState(null);
  const [selectedDownloadUrls, setSelectedDownloadUrls] = React.useState(new Set());
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
    const response = await fetch(`${API_BASE}/api/watch`);
    if (!response.ok) return;
    const data = await response.json();
    setWatch(data.watch || {});
  }, []);

  const loadDownloads = React.useCallback(async () => {
    const response = await fetch(`${API_BASE}/api/downloads`);
    if (!response.ok) return;
    const data = await response.json();
    setDownloadJobs(data.jobs || []);
  }, []);

  React.useEffect(() => {
    loadAccounts().catch((error) => setMessage(error.message));
    loadWatch().catch(() => {});
    loadDownloads().catch(() => {});
  }, [loadAccounts, loadWatch, loadDownloads]);

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
      const data = await postJson('/api/watch/start', { targets: pollTargets, interval, duration_minutes: durationMinutes });
      setWatch(data.watch);
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
    setBusy(true);
    try {
      const data = await postJson('/api/watch/stop');
      setWatch(data.watch);
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

  async function submitDownload() {
    if (!downloadText.trim()) {
      setMessage('请输入抖音视频、图集链接或分享文案');
      return;
    }
    const selectedUrls = Array.from(selectedDownloadUrls);
    setBusy(true);
    try {
      await postJson('/api/downloads', {
        text: downloadText,
        mode: Number(downloadMode),
        comments: false,
        selected_urls: selectedUrls,
      });
      setDownloadText('');
      setDownloadPreview(null);
      setSelectedDownloadUrls(new Set());
      await loadDownloads();
      setMessage('下载任务已提交，默认输出到项目 output 文件夹');
      appendLog('info', `下载任务已提交：${selectedUrls.length || '全部'} 个链接`, '下载');
    } catch (error) {
      setMessage(error.message);
      appendLog('error', error.message, '下载');
    } finally {
      setBusy(false);
    }
  }

  async function previewDownload() {
    if (!downloadText.trim()) {
      setMessage('请输入抖音视频、图集链接或分享文案');
      return;
    }
    setPreviewBusy(true);
    try {
      const data = await postJson('/api/downloads/preview', { text: downloadText, deep: true });
      const preview = data.preview || { items: [] };
      setDownloadPreview(preview);
      setSelectedDownloadUrls(new Set((preview.items || []).filter((item) => item.selected).map((item) => item.url)));
      setMessage(`已解析 ${preview.items?.length || 0} 个下载链接`);
      appendLog('info', `下载预览已解析 ${preview.items?.length || 0} 个链接`, '下载');
    } catch (error) {
      setMessage(error.message);
      appendLog('error', error.message, '下载');
    } finally {
      setPreviewBusy(false);
    }
  }

  function toggleDownloadUrl(url) {
    setSelectedDownloadUrls((current) => {
      const next = new Set(current);
      if (next.has(url)) next.delete(url);
      else next.add(url);
      return next;
    });
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
          <button onClick={() => openPollModal(checkedTargets)} disabled={busy || checkedTargets.length === 0 || watch.running}>
            <Activity size={16} /> 轮询选中
          </button>
          {watch.running && (
            <button className="secondary" onClick={() => openPollModal([])} disabled={busy}>
              <Clock size={16} /> 调整轮询
            </button>
          )}
          {watch.running && (
            <button className="secondary" onClick={stopPolling} disabled={busy}>
              <Square size={16} /> 停止轮询
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
                  <Square size={16} /> 停止轮询
                </button>
              )}
            </div>
          </div>

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
              <p>默认输出目录：output</p>
            </div>
            <Download size={18} />
          </div>
          <div className="download-form">
            <textarea
              value={downloadText}
              onChange={(event) => {
                setDownloadText(event.target.value);
                setDownloadPreview(null);
                setSelectedDownloadUrls(new Set());
              }}
              placeholder="粘贴 https://v.douyin.com/... 或完整分享文案"
            />
            <select value={downloadMode} onChange={(event) => setDownloadMode(event.target.value)}>
              <option value={1}>仅下载媒体</option>
              <option value={2}>仅保存互动数据</option>
              <option value={3}>媒体 + 数据</option>
            </select>
            <button className="secondary" onClick={previewDownload} disabled={busy || previewBusy}>
              <FileSearch size={16} /> {previewBusy ? '解析中' : '解析预览'}
            </button>
            <button onClick={submitDownload} disabled={busy || previewBusy || (downloadPreview && selectedDownloadUrls.size === 0)}>提交下载</button>
          </div>
          {downloadPreview && (
            <DownloadPreview
              preview={downloadPreview}
              selectedUrls={selectedDownloadUrls}
              onToggle={toggleDownloadUrl}
            />
          )}
          <div className="job-list">
            {downloadJobs.map((job) => (
              <div className="job-row" key={job.id}>
                <div className="job-summary">
                  <span className={`job-status ${job.status}`}>{job.status}</span>
                  <span>{job.urls.length} 个链接</span>
                  <small>{job.output_dir}</small>
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
    </section>
  );
}

function DownloadPreview({ preview, selectedUrls, onToggle }) {
  return (
    <div className="preview-list">
      {(preview.items || []).map((item) => (
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
              {item.type || 'link'} · {item.duration ? formatDuration(item.duration) : '时长未知'} · {item.media?.length || 1} 个媒体项
            </span>
            {item.error && <small className="error-text">{item.error}</small>}
            {item.media?.length > 1 && (
              <div className="media-strip">
                {item.media.slice(0, 8).map((media) => (
                  <div className="media-chip" key={`${item.url}-${media.index}`}>
                    {media.cover_url ? <img src={media.cover_url} alt="" /> : <span>{media.type}</span>}
                    <small>{media.index}</small>
                  </div>
                ))}
              </div>
            )}
          </div>
        </article>
      ))}
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
