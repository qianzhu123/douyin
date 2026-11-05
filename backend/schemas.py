from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


ProgressLevel = Literal["info", "live", "offline", "error"]


class ProgressStep(BaseModel):
    """进度流的一条记录：单步名 + 状态 + 文案。"""

    step: str
    status: Literal["pending", "running", "done", "error"] = "pending"
    message: str = ""
    level: ProgressLevel = "info"
    time: str = ""


class ProgressLog(BaseModel):
    steps: list[ProgressStep] = Field(default_factory=list)


class UserEntry(BaseModel):
    label: str = ""
    url: str
    sec_uid: str
    nickname: str = ""
    unique_id: str = ""
    signature: str = ""
    avatar_url: str = ""
    follower_count: int = 0
    following_count: int = 0
    total_favorited: int = 0
    ip_location: str = ""
    last_checked_at: str = ""
    last_ok: bool | None = None
    last_error: str = ""
    last_profile: dict[str, Any] | None = None


class QueryRequest(BaseModel):
    targets: list[str] = Field(default_factory=list)


class SearchUsersRequest(BaseModel):
    keyword: str


class LiveRoomRequest(BaseModel):
    sec_uid: str = ""
    web_rid: str = ""
    room_id_str: str = ""


class LiveAnalysisRequest(BaseModel):
    room_id: str
    anchor_id: str = ""
    label: str = ""


class LiveMonitorStartRequest(BaseModel):
    rooms: list[dict[str, Any]] = Field(default_factory=list)  # [{room_id, label?}]
    interval: int = Field(default=120, ge=60, le=3600)
    duration_minutes: int = Field(default=0, ge=0, le=1440)
    label: str = ""


class PostListRequest(BaseModel):
    sec_uid: str
    max_count: int = Field(default=100, ge=1, le=400)
    label: str = ""
    fetch_all: bool = False


class CommentListRequest(BaseModel):
    aweme_id: str
    max_count: int = Field(default=100, ge=1, le=300)


class UserListRequest(BaseModel):
    sec_uid: str
    kind: str = Field(default="following", pattern="^(following|follower)$")
    max_count: int = Field(default=100, ge=1, le=200)


class PostBatchDownloadRequest(BaseModel):
    urls: list[str] = Field(default_factory=list)
    label: str = ""
    mode: int = Field(default=1, ge=1, le=3)
    output_dir: str = ""
    wrap_folder: bool = False
    comments: bool = False
    image_format: str = Field(default="auto", pattern="^(auto|jpg|jpeg|png|webp|avif)$")
    video_format: str = Field(default="auto", pattern="^(auto|mp4|webm|audio)$")


class LiveViewersRequest(BaseModel):
    sec_uid: str


class PaygradeRequest(BaseModel):
    sec_uid: str


class SearchCandidate(BaseModel):
    nickname: str = ""
    unique_id: str = ""
    signature: str = ""
    avatar_url: str = ""
    follower_count: int = 0
    following_count: int = 0
    total_favorited: int = 0
    ip_location: str = ""
    sec_uid: str
    homepage_url: str
    live_status: int = 0
    room_id: str = ""


class AddUserRequest(BaseModel):
    label: str
    sec_uid: str
    homepage_url: str = ""
    nickname: str = ""
    unique_id: str = ""
    signature: str = ""
    avatar_url: str = ""
    follower_count: int = 0
    following_count: int = 0
    total_favorited: int = 0
    ip_location: str = ""


class ReorderUsersRequest(BaseModel):
    sec_uids: list[str] = Field(default_factory=list)


class AddUserResult(BaseModel):
    added: bool
    entry: UserEntry
    message: str = ""


class ProfileResult(BaseModel):
    label: str = ""
    url: str = ""
    sec_uid: str
    ok: bool
    error: str = ""
    profile: dict[str, Any] | None = None


class DownloadRequest(BaseModel):
    text: str
    mode: int = Field(default=1, ge=1, le=3)
    output_dir: str = ""
    wrap_folder: bool = False
    comments: bool = False
    # 图片目标格式: auto=跟随源格式(默认), jpg/png/webp/avif=统一转码
    image_format: str = Field(default="auto", pattern="^(auto|jpg|jpeg|png|webp|avif)$")
    # 视频目标格式: auto=mp4原始字节(默认), mp4=重编码, webm=VP9+Opus, audio=提取mp3音轨
    video_format: str = Field(default="auto", pattern="^(auto|mp4|webm|audio)$")
    selected_urls: list[str] = Field(default_factory=list)
    selected_media: dict[str, list[int]] = Field(default_factory=dict)


class DownloadPreviewRequest(BaseModel):
    text: str
    deep: bool = False


class DownloadJob(BaseModel):
    id: str
    # cancelled: 用户手动取消；运行中被 cancel 后尽快停止后续 URL，已完成的保留。
    status: Literal["queued", "running", "done", "error", "cancelled"]
    created_at: str
    updated_at: str
    input: str
    urls: list[str]
    mode: int
    output_dir: str
    wrap_folder: bool = False
    comments: bool = False
    image_format: str = "auto"
    video_format: str = "auto"
    selected_media: dict[str, list[int]] = Field(default_factory=dict)
    cancel_requested: bool = False
    logs: list[dict[str, str]] = Field(default_factory=list)
    results: list[dict[str, Any]] = Field(default_factory=list)
    error: str = ""


class AppSettings(BaseModel):
    download_output_dir: str = ""
    wrap_download_folder: bool = False
    # 默认媒体下载格式: auto=跟随源格式(默认)
    image_format: str = "auto"   # jpg/png/webp/avif=统一转码
    video_format: str = "auto"   # mp4/webm/audio=ffmpeg 转码


class AppSettingsRequest(BaseModel):
    download_output_dir: str = ""
    wrap_download_folder: bool = False
    image_format: str = Field(default="auto", pattern="^(auto|jpg|jpeg|png|webp|avif)$")
    video_format: str = Field(default="auto", pattern="^(auto|mp4|webm|audio)$")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
