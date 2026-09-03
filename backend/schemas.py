from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


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


class FansclubRequest(BaseModel):
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


class WatchStartRequest(BaseModel):
    targets: list[str] = Field(default_factory=list)
    interval: int = 30
    end_at: str = ""
    duration_minutes: int = 30
    id: str = ""
    label: str = ""


class WatchAdjustRequest(BaseModel):
    interval: int | None = None
    duration_minutes: int | None = None
    end_at: str | None = None


class WatchEvent(BaseModel):
    time: str
    level: Literal["info", "live", "offline", "error"]
    message: str
    sec_uid: str = ""


class WatchStatus(BaseModel):
    running: bool = False
    interval: int = 30
    duration_minutes: int = 30
    round: int = 0
    started_at: str = ""
    end_at: str = ""
    last_checked_at: str = ""
    targets: list[UserEntry] = Field(default_factory=list)
    profiles: list[ProfileResult] = Field(default_factory=list)
    events: list[WatchEvent] = Field(default_factory=list)


class WatchJob(BaseModel):
    id: str
    label: str = ""
    running: bool = False
    interval: int = 30
    duration_minutes: int = 30
    round: int = 0
    started_at: str = ""
    end_at: str = ""
    last_checked_at: str = ""
    targets: list[UserEntry] = Field(default_factory=list)
    profiles: list[ProfileResult] = Field(default_factory=list)
    events: list[WatchEvent] = Field(default_factory=list)


class WatchJobsResult(BaseModel):
    jobs: list[WatchJob] = Field(default_factory=list)
    current_id: str = ""


class DownloadRequest(BaseModel):
    text: str
    mode: int = Field(default=1, ge=1, le=3)
    output_dir: str = ""
    wrap_folder: bool = False
    comments: bool = False
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
    selected_media: dict[str, list[int]] = Field(default_factory=dict)
    cancel_requested: bool = False
    logs: list[dict[str, str]] = Field(default_factory=list)
    results: list[dict[str, Any]] = Field(default_factory=list)
    error: str = ""


class AppSettings(BaseModel):
    download_output_dir: str = ""
    wrap_download_folder: bool = False


class AppSettingsRequest(BaseModel):
    download_output_dir: str = ""
    wrap_download_folder: bool = False


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
