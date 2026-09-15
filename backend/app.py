from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .schemas import AddUserRequest, AppSettingsRequest, DownloadPreviewRequest, DownloadRequest, LiveRoomRequest, LiveViewersRequest, PaygradeRequest, ProgressLog, ProgressStep, QueryRequest, ReorderUsersRequest, SearchUsersRequest, WatchAdjustRequest, WatchStartRequest
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
        settings = save_app_settings(payload.download_output_dir, payload.wrap_download_folder)
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


@app.get("/api/watch")
def watch_status(job_id: str = "") -> dict:
    return {"watch": monitor_service.watch_status(job_id)}


@app.get("/api/watch/jobs")
def list_watch_jobs() -> dict:
    return {"jobs": [job.model_dump() for job in monitor_service.list_watch_jobs()],
            "current_id": monitor_service._watch_current_id}


@app.post("/api/watch/start")
async def start_watch(payload: WatchStartRequest) -> dict:
    job_id = (payload.id or "").strip()
    label = (payload.label or "").strip()
    # Backward-compat: feed end_at + duration_minutes through to the service.
    status = await monitor_service.start_watch(
        payload.targets,
        payload.interval,
        payload.duration_minutes,
        end_at=payload.end_at,
        job_id=job_id,
        label=label,
        poll_types=payload.poll_types,
    )
    progress = ProgressLog()
    progress.steps.append(ProgressStep(step="create_job", status="done", message=f"job_id={status.id or '-'}"))
    progress.steps.append(ProgressStep(step="schedule", status="done", message=f"间隔 {status.interval}s，持续 {status.duration_minutes}m"))
    types = status.poll_types or ["basic", "live"]
    progress.steps.append(ProgressStep(step="scope", status="done", message=f"轮询范围：{' / '.join(types)}"))
    return {"watch": status, "progress": progress.model_dump()}


@app.post("/api/watch/{job_id}/adjust")
def adjust_watch(job_id: str, payload: WatchAdjustRequest) -> dict:
    job = monitor_service.adjust_watch(
        job_id,
        interval=payload.interval,
        duration_minutes=payload.duration_minutes,
        end_at=payload.end_at if payload.end_at is not None else "",
    )
    return {"job": job.model_dump()}


@app.get("/api/watch/{job_id}/status")
def watch_job_status(job_id: str) -> dict:
    return {"watch": monitor_service.watch_status(job_id)}


@app.post("/api/watch/{job_id}/stop")
async def stop_watch_job(job_id: str) -> dict:
    return {"watch": await monitor_service.stop_watch(job_id)}


@app.delete("/api/watch/{job_id}")
async def delete_watch_job(job_id: str) -> dict:
    await monitor_service.remove_watch_job(job_id)
    return {"jobs": [job.model_dump() for job in monitor_service.list_watch_jobs()],
            "current_id": monitor_service._watch_current_id}


@app.post("/api/watch/stop")
async def stop_watch(job_id: str = "") -> dict:
    return {"watch": await monitor_service.stop_watch(job_id)}


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


@app.post("/api/admin/restart-backend")
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
