# Downloads And Extension Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make downloads default to the user's Downloads folder, allow a custom root directory, add an explicit title-folder wrapping switch, and provide a local browser extension that calls the existing backend from Douyin pages.

**Architecture:** Keep the FastAPI backend as the single source of truth for downloading and account import. The React UI and extension only submit URLs, root directory, and wrap preference; the Python downloader applies the final filesystem layout.

**Tech Stack:** FastAPI, Pydantic, Python pathlib, pytest, React/Vite, Chrome/Edge Manifest V3.

---

### Task 1: Backend Contracts And Tests

**Files:**
- Modify: `tests/test_services.py`
- Modify: `backend/config.py`
- Modify: `backend/schemas.py`
- Modify: `backend/services.py`
- Modify: `backend/app.py`

- [ ] Add tests proving the default output directory is `Path.home() / "Downloads"` and `DownloadService.create_job()` stores and passes `wrap_folder`.
- [ ] Run the targeted tests and confirm they fail because the current code still defaults to project `output` and has no `wrap_folder` field.
- [ ] Add settings helpers backed by `data/settings.json`.
- [ ] Add `GET /api/settings` and `POST /api/settings`.
- [ ] Extend `DownloadRequest` and `DownloadJob` with `wrap_folder`.
- [ ] Pass the resolved root directory and wrap preference into the downloader.

### Task 2: Downloader Directory Semantics

**Files:**
- Modify: `tests/test_services.py`
- Modify: `external/douyin-downloader/downloader.py`

- [ ] Add tests for `_decide_output_dir(base, mode, title, content_type, wrap_folder)`.
- [ ] Add tests for the media filename helper so unwrapped image sets use `标题_001.webp` in the root directory.
- [ ] Update `_decide_output_dir()` so `wrap_folder=false` returns the passed root directory and `wrap_folder=true` creates a title folder.
- [ ] Update video, slide, image, and JSON save paths to use the selected output directory.

### Task 3: React UI

**Files:**
- Modify: `src/main.jsx`
- Modify: `src/styles.css`

- [ ] Load `/api/settings` on startup.
- [ ] Add a download directory input and title-folder checkbox to the download panel.
- [ ] Save settings through `/api/settings`.
- [ ] Submit `output_dir` and `wrap_folder` with download jobs.
- [ ] Keep controls compact and avoid adding new large components.

### Task 4: Browser Extension

**Files:**
- Create: `tools/douyin_browser_extension/manifest.json`
- Create: `tools/douyin_browser_extension/popup.html`
- Create: `tools/douyin_browser_extension/popup.js`
- Create: `tools/douyin_browser_extension/README.md`

- [ ] Add a Manifest V3 popup extension with permissions for Douyin and `127.0.0.1:8000`.
- [ ] Add "download current work" behavior for video, note, short-link, and modal URLs.
- [ ] Add "import current profile" behavior for `/user/<sec_uid>` pages.
- [ ] Read backend settings so the extension uses the same default directory and wrap preference.

### Task 5: Verification

**Files:**
- All changed files.

- [ ] Run targeted pytest tests.
- [ ] Run full pytest suite.
- [ ] Run `npm run build`.
- [ ] Run `node --check tools/douyin_browser_extension/popup.js`.
- [ ] Review `git diff --stat` and summarize changed files.
