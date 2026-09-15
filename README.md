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

单 Chrome/Edge Manifest V3 扩展位于 `tools/douyin_extensions/douyin-helper/`：

- 自动识别当前页面（视频/笔记/直播间/个人主页/短链/聚合页），右上角显示圆形悬浮按钮；
- 视频/笔记场景 → 一键提交下载；个人主页 → 一键导入本地账户列表；
  直播间 → 就地检测展示房间/主播/当前观看/累计观看/本场点赞（MAIN world 捕获
  `webcast/room/web/enter/`；累计观看达 10万 后永远展示 `10万+`）；
- 提供 "打开本地控制台" 和 "重启本地后端" 两个共用动作。
- 所有本地后端调用走扩展的 background service worker，避开 HTTPS 页面的 mixed-content 限制。

扩展只调用 `http://127.0.0.1:8000`，不直接下载媒体。

## Polling Steps & Progress

每个检测接口（搜索 / 添加 / 单检 / 直播间 / 观看人数 / 轮询）都会返回结构化的 `progress`
步骤流，前端把它附在 `message` 上方，告诉你当前正在执行哪一步、是否已报错。
轮询任务本身也有一个滚动进度窗（`watch_jobs[i].progress`），每轮显示
basic / live 两段是否完成。

## Polling Scope

启动轮询时可在弹窗里勾选 "基本信息 / 直播间" 中的部分，
不勾选 = 全部跑（旧行为，向后兼容）。后端 `_watch_loop` 会按所选类型跳过对应探测，
避免在已知的"从未开播"账户上白跑直播间探测。粉丝团探测已整体舍弃
（实测 max_level 全 20 / club_level 都 0，区分度无价值）。

## Account Data

The dashboard uses `data/users.json` as its account source. On first startup, the backend imports existing entries from the original monitor tool's `settings.txt` only if `data/users.json` does not already exist.

Detection results are cached locally in `data/profile_cache.json`. This file is intentionally ignored by git because it is runtime state.
