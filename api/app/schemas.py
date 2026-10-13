from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class CameraCreate(BaseModel):
    name: str
    site: str = "default"
    rtsp_url: str
    codec: str = "h264"
    resolution: str = "unknown"
    frame_interval: int = Field(3, ge=1, description="run analytics every Nth frame")
    motion_threshold: int = Field(25, ge=1)
    min_area_frac: float = Field(0.01, gt=0, lt=1)
    cooldown_seconds: int = Field(10, ge=0)


class CameraOut(BaseModel):
    id: str
    name: str
    site: str
    path_name: str
    codec: str
    resolution: str
    analytics_profile: dict
    enabled: bool
    status: str
    last_seen_at: Optional[datetime] = None
    created_at: datetime
    hls_url: Optional[str] = None
    assigned_worker: Optional[str] = None

    class Config:
        from_attributes = True


class EventIn(BaseModel):
    event_id: Optional[str] = None
    camera_id: str
    event_type: str = "motion"
    start_ts: datetime
    end_ts: Optional[datetime] = None
    objects: list[dict[str, Any]] = []
    snapshot_ref: Optional[str] = None


class EventOut(BaseModel):
    event_id: str
    camera_id: str
    event_type: str
    start_ts: datetime
    end_ts: Optional[datetime] = None
    objects: list
    snapshot_ref: Optional[str] = None
    verification_status: str
    verification_note: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class AssignmentIn(BaseModel):
    worker_id: str
    ttl_seconds: int = 15
