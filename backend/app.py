from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .schemas import AddUserRequest, DownloadPreviewRequest, DownloadRequest, QueryRequest, ReorderUsersRequest, SearchUsersRequest, WatchAdjustRequest, WatchStartRequest
from .services import DownloadService, MonitorService


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


@app.get("/api/users")
def users() -> dict:
    return {"users": monitor_service.list_users()}


@app.post("/api/users/search")
async def search_users(payload: SearchUsersRequest) -> dict:
    try:
        candidates = await monitor_service.search_users(payload.keyword)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"candidates": candidates}


@app.post("/api/users")
def add_user(payload: AddUserRequest) -> dict:
    try:
        result = monitor_service.add_user_payload(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"result": result, "users": monitor_service.list_users()}


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
    return {"results": await monitor_service.query_profiles(payload.targets)}


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
    )
    return {"watch": status}


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


@app.post("/api/watch/stop")
async def stop_watch(job_id: str = "") -> dict:
    return {"watch": await monitor_service.stop_watch(job_id)}


@app.post("/api/downloads")
def create_download(payload: DownloadRequest) -> dict:
    try:
        job = download_service.create_job(payload.text, payload.mode, payload.output_dir, payload.comments, payload.selected_urls, payload.selected_media)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"job": job}


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
