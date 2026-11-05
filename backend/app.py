from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .schemas import AddUserRequest, AppSettingsRequest, DownloadPreviewRequest, DownloadRequest, LiveRoomRequest, LiveViewersRequest, PaygradeRequest, ProgressLog, ProgressStep, QueryRequest, ReorderUsersRequest, SearchUsersRequest
from .services import DownloadService, MonitorService, load_app_settings, save_app_settings


app = FastAPI(title="Douyin Monitor Dashboard")
monitor_service = MonitorService()
download_service = DownloadService()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
        "http://localhost:5175",
        "http://127.0.0.1:5175",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("shutdown")
async def on_shutdown() -> None:
    await monitor_service.shutdown()


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/settings")
def settings() -> dict:
    return {"settings": load_app_settings()}


@app.post("/api/settings")
def update_settings(payload: AppSettingsRequest) -> dict:
    try:
        settings = save_app_settings(payload.download_output_dir, payload.wrap_download_folder, payload.image_format, payload.video_format)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"settings": settings}


@app.get("/api/users")
def users() -> dict:
    return {"users": monitor_service.list_users()}


@app.post("/api/users/search")
async def search_users(payload: SearchUsersRequest) -> dict:
    progress = ProgressLog()
    progress.steps.append(ProgressStep(step="validate", status="running", message=f"校验关键词 '{payload.keyword}'"))
    if not payload.keyword or not payload.keyword.strip():
        progress.steps.append(ProgressStep(step="validate", status="error", message="关键词为空", level="error"))
        raise HTTPException(status_code=400, detail={"error": "Search keyword is required.", "progress": progress.model_dump()})
    progress.steps.append(ProgressStep(step="validate", status="done", message="关键词合法"))
    progress.steps.append(ProgressStep(step="playwright", status="running", message="准备启动浏览器"))
    try:
        candidates = await monitor_service.search_users(payload.keyword)
    except ValueError as exc:
        progress.steps.append(ProgressStep(step="playwright", status="error", message=str(exc), level="error"))
        raise HTTPException(status_code=400, detail={"error": str(exc), "progress": progress.model_dump()}) from exc
    except RuntimeError as exc:
        progress.steps.append(ProgressStep(step="search_response", status="error", message=str(exc), level="error"))
        raise HTTPException(status_code=502, detail={"error": str(exc), "progress": progress.model_dump()}) from exc
    progress.steps.append(ProgressStep(step="playwright", status="done", message="浏览器已就绪"))
    progress.steps.append(ProgressStep(step="search_response", status="done", message=f"返回 {len(candidates)} 条候选用户"))
    return {"candidates": candidates, "progress": progress.model_dump()}


@app.post("/api/users/resolve-link")
async def resolve_share_link(payload: SearchUsersRequest) -> dict:
    """粘贴输入解析 (2026-11-05, 用户定论: 监控目标不再走候选 chip 选主播):
    ① douyin.com/user/MS4w... 主页链接 → 正则直提取 sec_uid;
    ② v.douyin.com/xxx 短链 → NoRedirect 302 取 Location (lab §73.5 配方);
    ③ 其它 → 交给现有搜索链路 (名称/抖音号)。
    返回 {kind: "sec_uid"|"candidate"|"none", sec_uid?/candidate?}。
    """
    import re as _re

    import httpx

    from .schemas import SearchCandidate

    text = (payload.keyword or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="输入为空")

    # ① 主页链接 / 直接粘贴 sec_uid
    m = _re.search(r"(?:/user/)?(MS4w[A-Za-z0-9_-]{20,})", text)
    if m:
        return {"kind": "sec_uid", "sec_uid": m.group(1)}

    # ② 短链 302 解析 (带重试; 失败降级搜索)
    if _re.search(r"v\.douyin\.com|[a-z0-9-]+\.(douyin|iesdouyin)\.com/", text):
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(text, follow_redirects=False)
                loc = resp.headers.get("location", "")
            m2 = _re.search(r"(MS4w[A-Za-z0-9_-]{20,})", loc)
            if m2:
                return {"kind": "sec_uid", "sec_uid": m2.group(1)}
            if loc:
                # 短链可能跳到中间页, 对 Location 再走一遍 ①
                m3 = _re.search(r"(MS4w[A-Za-z0-9_-]{20,})", loc)
                if m3:
                    return {"kind": "sec_uid", "sec_uid": m3.group(1)}
        except Exception:
            pass  # 短链解析失败 → 落搜索

    # ③ 名称/抖音号 → 搜索 (取首条)。搜索链路被登录墙/验证码拦截时降级报错, 不 500
    # (2026-11-05 实测: douyin.com 搜索页已强制登录, profile 会话被风控时全链路空转;
    #  三级模块各烧 20-50s 后超时 → 总卡 2 分钟, 故这里加 60s 总闸快速失败)
    async def _search_with_budget() -> dict:
        candidates = await monitor_service.search_users(text)
        if candidates:
            return {"kind": "candidate", "candidate": candidates[0].model_dump()}
        return {"kind": "none"}

    import asyncio as _asyncio

    try:
        return await _asyncio.wait_for(_search_with_budget(), timeout=60)
    except _asyncio.TimeoutError:
        raise HTTPException(
            status_code=503,
            detail="搜索链路超时 (60s) — 多半被登录墙/验证码拦截。请改粘主页链接/短链/sec_uid, 或重登 external/douyin-user-search/douyin_profile (login.bat)",
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"搜索链路不可用 (登录墙/验证码拦截): {str(exc)[:120]} — 请改粘主页链接/短链/sec_uid, 或重登 external/douyin-user-search/douyin_profile",
        )


@app.post("/api/users")
def add_user(payload: AddUserRequest) -> dict:
    progress = ProgressLog()
    progress.steps.append(ProgressStep(step="validate", status="running", message="校验账户字段"))
    try:
        result = monitor_service.add_user_payload(payload.model_dump())
    except ValueError as exc:
        progress.steps.append(ProgressStep(step="validate", status="error", message=str(exc), level="error"))
        raise HTTPException(status_code=400, detail={"error": str(exc), "progress": progress.model_dump()}) from exc
    progress.steps.append(ProgressStep(step="validate", status="done", message="字段合法"))
    progress.steps.append(ProgressStep(step="persist", status="done", message="已写入 data/users.json"))
    return {"result": result, "users": monitor_service.list_users(), "progress": progress.model_dump()}


@app.post("/api/users/reorder")
def reorder_users(payload: ReorderUsersRequest) -> dict:
    return {"users": monitor_service.reorder_users(payload.sec_uids)}


@app.delete("/api/users/{sec_uid}")
def delete_user(sec_uid: str) -> dict:
    try:
        return {"users": monitor_service.delete_user(sec_uid)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/query")
async def query_profiles(payload: QueryRequest) -> dict:
    progress = ProgressLog()
    progress.steps.append(ProgressStep(step="resolve", status="running", message="解析目标账户"))
    targets = monitor_service.resolve_targets(payload.targets)
    if not targets:
        progress.steps.append(ProgressStep(step="resolve", status="error", message="无可用账户", level="error"))
        raise HTTPException(status_code=400, detail={"error": "No targets resolved.", "progress": progress.model_dump()})
    progress.steps.append(ProgressStep(step="resolve", status="done", message=f"已解析 {len(targets)} 个账户"))
    progress.steps.append(ProgressStep(step="browser", status="running", message="准备/复用浏览器实例"))
    try:
        results = await monitor_service.query_profiles(payload.targets)
    except Exception as exc:
        progress.steps.append(ProgressStep(step="browser", status="error", message=str(exc), level="error"))
        raise HTTPException(status_code=502, detail={"error": str(exc), "progress": progress.model_dump()}) from exc
    progress.steps.append(ProgressStep(step="browser", status="done", message="浏览器实例就绪"))
    progress.steps.append(ProgressStep(step="profile_api", status="done", message=f"profile/other 已并发拉取 {len(results)} 个"))
    failed = sum(1 for r in results if not r.ok)
    progress.steps.append(ProgressStep(
        step="summary", status="error" if failed else "done",
        message=f"成功 {len(results) - failed}/{len(results)}，失败 {failed}",
        level="error" if failed else "info",
    ))
    return {"results": results, "progress": progress.model_dump()}


@app.post("/api/live-room")
async def live_room(payload: LiveRoomRequest) -> dict:
    """手动再探测一次直播间详情（供前端"刷新直播间"按钮）。"""
    progress = ProgressLog()
    progress.steps.append(ProgressStep(step="validate", status="running", message="读取 web_rid/room_id"))
    progress.steps.append(ProgressStep(step="playwright", status="running", message="启动浏览器并 goto 直播间"))
    room = await monitor_service.fetch_live_room(
        sec_uid=payload.sec_uid.strip(),
        web_rid=payload.web_rid.strip(),
        room_id_str=payload.room_id_str.strip(),
    )
    if room is None:
        progress.steps.append(ProgressStep(step="playwright", status="error", message="直播间探测失败（可能无 web_rid 或风控）", level="error"))
    else:
        progress.steps.append(ProgressStep(step="playwright", status="done", message=f"已捕获 enter/ranklist（web_rid={room.get('web_rid') or '-'}）"))
    return {"live_room": room, "progress": progress.model_dump()}


@app.post("/api/live-viewers")
async def live_viewers(payload: LiveViewersRequest) -> dict:
    """仅刷新直播间人数（profile.live_viewers + live_room.viewers），其它字段不动。"""
    progress = ProgressLog()
    progress.steps.append(ProgressStep(step="validate", status="running", message="读取 cache 中 web_rid"))
    data = await monitor_service.fetch_live_viewers(payload.sec_uid.strip())
    if data is None:
        progress.steps.append(ProgressStep(step="validate", status="error", message="缺 web_rid 或探测失败", level="error"))
    else:
        progress.steps.append(ProgressStep(step="enter", status="done", message=f"人数已刷新 ({data.get('viewers') or 0})"))
    return {"live_viewers": data, "progress": progress.model_dump()}


@app.post("/api/anchor-paygrade")
async def anchor_paygrade(payload: PaygradeRequest) -> dict:
    """手动探测 anchor 本人 paygrade（hover popup 解析），需要 cache 里有 web_rid。"""
    level = await monitor_service.fetch_anchor_paygrade(payload.sec_uid.strip())
    return {"paygrade_level": level}


@app.post("/api/downloads")
def create_download(payload: DownloadRequest) -> dict:
    try:
        job = download_service.create_job(
            payload.text,
            payload.mode,
            payload.output_dir,
            payload.comments,
            payload.selected_urls,
            payload.selected_media,
            payload.wrap_folder,
            payload.image_format,
            payload.video_format,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"job": job}


@app.post("/api/downloads/{job_id}/cancel")
def cancel_download(job_id: str) -> dict:
    ok = download_service.cancel_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Download job not found or already finished.")
    return {"job": download_service.get_job(job_id)}


@app.delete("/api/downloads/{job_id}")
def delete_download(job_id: str) -> dict:
    ok = download_service.delete_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Download job not found.")
    return {"deleted": job_id, "jobs": download_service.list_jobs()}


@app.post("/api/downloads/preview")
def preview_download(payload: DownloadPreviewRequest) -> dict:
    try:
        return {"preview": download_service.preview(payload.text, payload.deep)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/downloads")
def downloads() -> dict:
    return {"jobs": download_service.list_jobs()}


@app.get("/api/downloads/{job_id}")
def download(job_id: str) -> dict:
    job = download_service.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Download job not found.")
    return {"job": job}


# ── 管理：重启后端服务（前端扩展/控制台按钮调用） ──
# 触发 scripts/start-douyin.ps1 幂等重启：health 通过则复用，未通过则清掉 8000 端口旧进程再起。
# 后端进程会自我退出（PS 脚本不感知到 launch 终止信号，这里通过 Stop-Process 兜底），
# 所以调用方需要在 1.5s 内失联后重试 /api/health。
import os as _os_admin
import subprocess as _sp_admin
import threading as _th_admin
from pathlib import Path as _P_admin

from .config import PROJECT_ROOT as _PROJECT_ROOT

# ---- 粉丝团签到 (PC 直连, 凭证来自 data/fansclub_tokens/, 详见 backend/fansclub.py) ----
from . import fansclub as _fc

_fc.start_auto_check_daemon()  # 每天凌晨 00:05 自动全量检测 (后端内置定时器, 不依赖前端; 后端没开机则当天启动时补跑)


@app.get("/api/fansclub/accounts")
def fansclub_accounts() -> dict:
    """列出本地凭证账户 (不含 token 本体)。"""
    return {"accounts": [{"label": a["label"]} for a in _fc.load_tokens()]}


@app.get("/api/fansclub/overview")
def fansclub_overview(label: str) -> dict:
    """单账户全量粉丝团信息 (等级/连签/今日签/热度/排名)。"""
    tokens = {a["label"]: a["token"] for a in _fc.load_tokens()}
    if label not in tokens:
        raise HTTPException(status_code=404, detail=f"no token for label: {label}")
    try:
        return _fc.account_overview(label, tokens[label])
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:160]) from exc


@app.post("/api/fansclub/sign/{label}")
def fansclub_sign(label: str) -> dict:
    """一键签到该账户全部未签团 (免费 join 端点, 幂等)。"""
    tokens = {a["label"]: a["token"] for a in _fc.load_tokens()}
    if label not in tokens:
        raise HTTPException(status_code=404, detail=f"no token for label: {label}")
    return _fc.start_sign(label, tokens[label])


@app.post("/api/fansclub/sign-all")
def fansclub_sign_all() -> dict:
    """一键签到全部账户。"""
    results = {}
    for a in _fc.load_tokens():
        results[a["label"]] = _fc.start_sign(a["label"], a["token"])
    return {"results": results}


@app.get("/api/fansclub/sign-status")
def fansclub_sign_status() -> dict:
    return {"jobs": _fc.sign_status()}


@app.post("/api/fansclub/sign-club/{label}")
def fansclub_sign_club(label: str, payload: dict) -> dict:
    """单团签到 (每主播行按钮; body {anchor_id}; bt=4 join, 幂等)。"""
    tokens = {a["label"]: a["token"] for a in _fc.load_tokens()}
    if label not in tokens:
        raise HTTPException(status_code=404, detail=f"no token for label: {label}")
    aid = str((payload or {}).get("anchor_id") or "").strip()
    if not aid:
        raise HTTPException(status_code=400, detail="anchor_id required")
    return _fc.sign_club(label, tokens[label], aid)


@app.get("/api/fansclub/check-club/{label}")
def fansclub_check_club(label: str, anchor_id: str) -> dict:
    """单团检测 (每主播行按钮; 返回该团最新状态行)。"""
    tokens = {a["label"]: a["token"] for a in _fc.load_tokens()}
    if label not in tokens:
        raise HTTPException(status_code=404, detail=f"no token for label: {label}")
    return _fc.check_club(label, tokens[label], anchor_id)


@app.get("/api/fansclub/overview-cache")
def fansclub_overview_cache() -> dict:
    """后端落盘的最新检测结果 (定时检测在页面没开时也有效)。"""
    return {"cache": _fc.load_overview_cache()}


@app.post("/api/fansclub/check-all")
def fansclub_check_all() -> dict:
    """后台串行检查全部账户 (每天0点前端自动触发 + 手动按钮共用)。"""
    return _fc.start_check_all()


@app.get("/api/fansclub/check-status")
def fansclub_check_status() -> dict:
    return _fc.check_all_status()


# ---- 用户作品列表 (两头配方直连, 只读, 见 backend/aweme_read.py) ----
from . import aweme_read as _ar
from .schemas import PostListRequest


@app.post("/api/posts/list")
def posts_list(payload: PostListRequest) -> dict:
    """拉取指定用户作品列表 (cursor 翻页; 只读; 凭证同粉丝团 tokens)."""
    try:
        return _ar.list_posts(payload.sec_uid.strip(), max_count=payload.max_count,
                              label=payload.label.strip() or None,
                              fetch_all=payload.fetch_all)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:160]) from exc


# ---- 作品评论采集 (只读, 见 backend/comments.py) ----
from . import comments as _cm
from .schemas import CommentListRequest


@app.post("/api/comments/list")
def comments_list(payload: CommentListRequest) -> dict:
    """拉取作品评论 (直连→浏览器降级链; 只读)."""
    try:
        return _cm.list_comments(payload.aweme_id.strip(), max_count=payload.max_count)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:160]) from exc


# ---- 作品列表一键批量下载 (作品列表 → 下载器直投) ----
from .schemas import PostBatchDownloadRequest


@app.post("/api/posts/batch-download")
def posts_batch_download(payload: PostBatchDownloadRequest) -> dict:
    """把作品列表拿到的 /video/{id} URL 一次性投给下载器."""
    try:
        job = download_service.create_job_from_post_list(
            payload.urls,
            payload.label,
            payload.mode,
            payload.output_dir,
            payload.comments,
            payload.wrap_folder,
            payload.image_format,
            payload.video_format,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"job": job}


# ---- 关注/粉丝列表 (只读, 见 backend/user_lists.py; 2026-11 直连被收紧, 端点保留) ----
from . import user_lists as _ul
from .schemas import UserListRequest


@app.post("/api/user-lists")
def user_lists(payload: UserListRequest) -> dict:
    """拉取关注/粉丝列表 (直连; 被收紧时报 error)."""
    try:
        return _ul.list_user_list(payload.sec_uid.strip(), payload.kind.strip(),
                                  max_count=payload.max_count)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:160]) from exc


# ---- 直播间 PC 直连分析 (两头配方 2.0, 只读, 见 backend/live_analysis.py) ----
from . import live_analysis as _la
from .schemas import LiveAnalysisRequest


@app.post("/api/live/analyze")
def live_analyze(payload: LiveAnalysisRequest) -> dict:
    """进房拉房间全量+粉丝团面板+签到榜+福袋 (只读; 同房间 10 分钟节流标注)。"""
    try:
        return _la.analyze_room(payload.room_id.strip(), payload.anchor_id.strip() or None,
                                payload.label.strip() or None)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:160]) from exc


# ---- 直播实时监控 (PC 直连快照轮询, 零浏览器, 见 backend/live_monitor.py) ----
from .live_monitor import LiveMonitor
from .schemas import LiveMonitorStartRequest

_live_monitor = LiveMonitor()


@app.post("/api/live-monitor/start")
def live_monitor_start(payload: LiveMonitorStartRequest) -> dict:
    """启动直播监控 (后台线程直连快照; 开播/下播/观众变化事件)。"""
    r = _live_monitor.start(payload.rooms, interval=payload.interval,
                            duration_minutes=payload.duration_minutes,
                            label=payload.label.strip())
    if not r.get("started"):
        raise HTTPException(status_code=400, detail=r.get("reason") or "未能启动")
    return r


@app.post("/api/live-monitor/stop")
def live_monitor_stop() -> dict:
    return _live_monitor.stop()


@app.get("/api/live-monitor/status")
def live_monitor_status() -> dict:
    return _live_monitor.status()


# /api/live-monitor/candidates 已删 (2026-11-05): 候选 chip 选主播形态废弃,
# 监控目标 = 账户列表全部 (前端 /api/users 直取) + 粘贴解析 (/api/users/resolve-link)。


@app.post("/api/fansclub/admin/restart-backend")
def admin_restart_backend() -> dict:
    """幂等重启后端（8000）。前端调用后预计 1-3s 内会失联。"""
    project_root = _P_admin(_PROJECT_ROOT).resolve()
    script = project_root / "scripts" / "start-douyin.ps1"
    if not script.exists():
        raise HTTPException(status_code=500, detail=f"missing script: {script}")
    # 异步执行（不等它完成；它会自己 kill 旧后端再起新的），
    # 启它的 PS 进程会立刻把当前 python 进程 stop。
    def _kick():
        try:
            _sp_admin.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                cwd=str(project_root),
                creationflags=getattr(_sp_admin, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            pass
    _th_admin.Thread(target=_kick, daemon=True).start()
    return {"queued": True, "script": str(script)}
