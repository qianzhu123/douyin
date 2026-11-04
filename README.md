# Douyin Web Dashboard

This project bundles the `douyin-monitor`, `douyin-downloader`, and `douyin-user-search` tools (under `external/`) in a self-contained web dashboard. No external paths or environment variables are required.

## Paths

- Monitor tool: `external/douyin-monitor`
- Downloader tool: `external/douyin-downloader`
- Search tool: `external/douyin-user-search`
- Project accounts: `data/users.json`
- Runtime profile cache: `data/profile_cache.json`
- Fansclub tokens: `data/fansclub_tokens/` (per-account `{label, x_tt_token}`)
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
- Submit Douyin video/share URLs to the downloader as background jobs. Aggregation-page URLs (discover page `/jingxuan?modal_id=`, search page `/jingxuan/search/...?...&modal_id=`, likes list `/user/self?...&modal_id=`) are auto-rewritten to the canonical `/video/{aweme_id}` detail page before parsing.
- Choose a custom download root directory and whether each work should be wrapped in a title folder.
- Post lists and comments for any configured account (custom count input), with one-click batch download of fetched posts.
- Fansclub signing dashboard: per-account club overview (level / streak / intimacy / rank), per-club sign and check buttons, sign-all (lamp-lit clubs only), and a daily automatic full check at 00:00 Beijing time that persists results to `data/fansclub_overview_cache.json`.
- Direct-connection recipes (two-headed token + origin-host) with automatic browser fallback for blocked read endpoints.

## Browser Extensions

A single Chrome/Edge Manifest V3 extension lives at `tools/douyin_extensions/douyin-helper/`:

- Recognizes the current page type (video / note / live room / user profile / short link / aggregation page) and shows a floating action button in the top-right corner;
- Video / note pages → one-click download submission; user profiles → one-click import into the local account list;
  live rooms → in-place detection showing room / anchor / current viewers / total viewers / session likes
  (captures `webcast/room/web/enter/` in MAIN world; total viewers permanently shows `10万+` after reaching 100k);
- Provides two shared actions: "open local dashboard" and "restart local backend".
- All local backend calls go through the extension's background service worker to avoid mixed-content restrictions on HTTPS pages.

The extension only calls `http://127.0.0.1:8000` and never downloads media itself.

## Polling Steps & Progress

Every detection endpoint (search / add / single check / live room / viewer count / polling) returns a structured `progress` step stream, which the frontend renders above the `message` so you can see which step is running and whether it errored.
Polling jobs also keep a scrolling progress window (`watch_jobs[i].progress`) showing whether the basic / live phases of each round completed.

## Polling Scope

When starting a polling job you can tick a subset of "basic info / live room" in the dialog.
Nothing ticked = run everything (old behavior, backward compatible). The backend `_watch_loop` skips the corresponding probe for unselected types,
avoiding wasted live-room probes on accounts known to never stream. Fansclub probing has been dropped from polling entirely
(in practice `max_level` is always 20 and `club_level` always 0 — no signal).

## Account Data

The dashboard uses `data/users.json` as its account source. On first startup, the backend imports existing entries from the original monitor tool's `settings.txt` only if `data/users.json` does not already exist.

Detection results are cached locally in `data/profile_cache.json`. This file is intentionally ignored by git because it is runtime state.
