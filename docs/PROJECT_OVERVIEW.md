# Douyin Web Dashboard Project Overview

## Summary

Douyin Web Dashboard is a local web application for managing Douyin account monitoring workflows. It combines a FastAPI backend, a React and Vite frontend, and existing local Python utilities for Douyin profile checks, live status polling, search-assisted account management, and video download jobs.

The application is designed for local use on Windows. It provides a clear browser-based dashboard instead of terminal-only logs, with account rows, profile metrics, live state, polling controls, download previews, and runtime logs.

## Core Features

- Account list management backed by `data/users.json`.
- One-time profile and live status detection for selected accounts.
- Timed live polling with configurable interval and duration.
- Account search and save workflow from the web interface.
- Account ordering, masking, deletion, and right-click account metadata actions.
- Local cache of the most recent profile and live detection results.
- Douyin video and image post preview before submitting download jobs.
- Background download task tracking with status and logs.
- Windows launcher scripts and a locally generated launcher executable.

## Architecture

The backend is implemented with FastAPI. It exposes APIs for account management, profile queries, live polling, user search, download previews, and download jobs. It loads the existing local monitor and downloader Python utilities through a small module loader instead of duplicating their core logic.

The frontend is implemented with React and Vite. It renders a list-first dashboard where each account row exposes selection, one-time detection, polling, masking, deletion, homepage access, and context-menu metadata actions. Runtime logs are split into detection, polling, and download sections so failures and background activity are visible without reading terminal output.

## Runtime Paths

The three helper utilities are bundled inside the project under `external/`, and their locations are hard-coded relative to the project root in `backend/config.py`. No environment variables are used.

- Monitor utility: `external/douyin-monitor`
- Downloader utility: `external/douyin-downloader`
- Search utility: `external/douyin-user-search`
- Account data: `data/users.json`
- Profile cache: `data/profile_cache.json`
- Download output: `output`

## Development

Install frontend and backend dependencies:

```powershell
npm install
pip install -r requirements.txt
```

Run the backend:

```powershell
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

Run the frontend:

```powershell
npm run dev -- --port 5175
```

Open the dashboard at:

```text
http://127.0.0.1:5175
```

## Validation

Use the test suite and production build before publishing changes:

```powershell
pytest -q
npm run build
```

## Security And Data Notes

This project is intended for local personal use. Runtime data, logs, downloaded media, and profile cache files should not be treated as public artifacts. The account JSON file may contain account identifiers and should be reviewed before publishing the repository publicly.

The application does not expose Douyin request signatures or credential details in the frontend. Sensitive request material should remain in local runtime code or browser session state only.
