# Douyin Web Dashboard

This project wraps the local `douyin-monitor` and `douyin-downloader` tools in a web dashboard.

## Paths

- Monitor tool: configured by `DOUYIN_MONITOR_DIR`.
- Downloader tool: configured by `DOUYIN_DOWNLOADER_DIR`.
- Search tool: configured by `DOUYIN_SEARCH_DIR`.
- Project accounts: `data/users.json`.
- Runtime profile cache: `data/profile_cache.json`.
- Default download output: `output`.

Override paths with environment variables:

```powershell
$env:DOUYIN_MONITOR_DIR="<path-to-douyin-monitor>"
$env:DOUYIN_DOWNLOADER_DIR="<path-to-douyin-downloader>"
$env:DOUYIN_SEARCH_DIR="<path-to-douyin-user-search>"
$env:DOUYIN_DOWNLOAD_OUTPUT="<path-to-output-directory>"
```

## Run

```powershell
python -m uvicorn backend.app:app --reload --host 127.0.0.1 --port 8000
npm install
npm run dev
```

Open `http://127.0.0.1:5173`.

## Windows Launcher

- VBS launcher: `scripts\start-douyin.vbs`
- EXE launcher: generated as `app\douyin.exe`
- Rebuild launcher: `scripts\build-launcher.ps1`

The launcher starts the backend on `127.0.0.1:8000`, starts the Vite UI on `127.0.0.1:5175`, then opens the browser.

## Features

- Show configured users by remark while calling the real homepage URL.
- Run selected users as a one-time profile query.
- Start/stop selected live polling with a refresh interval.
- Search Douyin users from the web UI and save selected accounts to `data/users.json`.
- Submit Douyin video/share URLs to the downloader as background jobs.

## Account Data

The dashboard uses `data/users.json` as its account source. On first startup, the backend imports existing entries from the original monitor tool's `settings.txt` only if `data/users.json` does not already exist.

Detection results are cached locally in `data/profile_cache.json`. This file is intentionally ignored by git because it is runtime state.
