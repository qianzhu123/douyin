# Douyin Web Dashboard

This project bundles the `douyin-monitor`, `douyin-downloader`, and `douyin-user-search` tools (under `external/`) in a self-contained web dashboard. No external paths or environment variables are required.

## Paths

- Monitor tool: `external/douyin-monitor`
- Downloader tool: `external/douyin-downloader`
- Search tool: `external/douyin-user-search`
- Project accounts: `data/users.json`
- Runtime profile cache: `data/profile_cache.json`
- Download settings: `data/settings.json`
- Default download output: the current user's `Downloads` directory

All tool locations are hard-coded relative to the project root in `backend/config.py`. **No environment variables are used or expected.**

## Run

```powershell
python -m uvicorn backend.app:app --reload --host 127.0.0.1 --port 8000
npm install
npm run dev
```

Open `http://127.0.0.1:5175`.

## Windows Launcher

- VBS launcher: `scripts/start-douyin.vbs`
- EXE launcher: generated as `app/douyin.exe`
- Rebuild launcher: `scripts/build-launcher.ps1`

The launcher starts the backend on `127.0.0.1:8000`, starts the Vite UI on `127.0.0.1:5175`, then opens the browser.
If frontend dependencies are missing, the launcher runs `npm install` before starting Vite.

## Features

- Show configured users by remark while calling the real homepage URL.
- Run selected users as a one-time profile query.
- Start/stop selected live polling with a refresh interval.
- Search Douyin users from the web UI and save selected accounts to `data/users.json`.
- Submit Douyin video/share URLs to the downloader as background jobs. Aggregation-page URLs (发现页 `/jingxuan?modal_id=`, 搜索页 `/jingxuan/search/...?...&modal_id=`, 喜欢列表 `/user/self?...&modal_id=`) are auto-rewritten to the canonical `/video/{aweme_id}` detail page before parsing.
- Choose a custom download root directory and whether each work should be wrapped in a title folder.

## Browser Extensions

Two local Chrome/Edge Manifest V3 extensions live under `tools/douyin_extensions/`:

- `downloader/` — Douyin Downloader Helper. On any Douyin web page (video / note / short link / recommended / search / liked / following / profile modal), shows a floating round button in the top-right that submits the current URL to the local downloader.
- `live-overlay/` — Douyin 直播间人数悬浮窗. On any `live.douyin.com/<web_rid>` room, shows a floating round button with the current viewer count; clicking it expands a detail panel with room title, anchor, like / total-user counts, and a link to the local dashboard.

Both extensions call the local backend at `http://127.0.0.1:8000`; they do not download media themselves.

## Account Data

The dashboard uses `data/users.json` as its account source. On first startup, the backend imports existing entries from the original monitor tool's `settings.txt` only if `data/users.json` does not already exist.

Detection results are cached locally in `data/profile_cache.json`. This file is intentionally ignored by git because it is runtime state.
