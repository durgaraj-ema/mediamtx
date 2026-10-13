import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from .db import Base


def gen_id() -> str:
    return uuid.uuid4().hex


class Camera(Base):
    __tablename__ = "cameras"

    id = Column(String, primary_key=True, default=gen_id)
    name = Column(String, nullable=False)
    site = Column(String, nullable=True, default="default")
    # Encrypted at rest. Never returned by the API.
    rtsp_url_enc = Column(Text, nullable=False)
    path_name = Column(String, unique=True, nullable=False)
    codec = Column(String, default="unknown")
    resolution = Column(String, default="unknown")
    # Per-camera analytics profile: inference interval, motion threshold, etc.
    analytics_profile = Column(
        JSON,
        default=lambda: {
            "frame_interval": 3,
            "motion_threshold": 25,
            "min_area_frac": 0.01,
            "cooldown_seconds": 10,
        },
    )
    enabled = Column(Boolean, default=True)
    status = Column(String, default="unknown")  # unknown|online|offline
    last_seen_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    assignment = relationship(
        "CameraAssignment", back_populates="camera", uselist=False
    )


class CameraAssignment(Base):
    __tablename__ = "camera_assignments"

    camera_id = Column(String, ForeignKey("cameras.id"), primary_key=True)
    worker_id = Column(String, nullable=False)
    leased_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)

    camera = relationship("Camera", back_populates="assignment")


class LiveEvent(Base):
    __tablename__ = "live_events"

    event_id = Column(String, primary_key=True)  # deterministic hash, idempotent
    camera_id = Column(String, ForeignKey("cameras.id"), nullable=False)
    event_type = Column(String, nullable=False, default="motion")
    start_ts = Column(DateTime, nullable=False)
    end_ts = Column(DateTime, nullable=True)
    objects = Column(JSON, default=list)
    snapshot_ref = Column(Text, nullable=True)
    verification_status = Column(String, default="pending")
    verification_note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class RecordingSegment(Base):
    __tablename__ = "recording_segments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # MediaMTX organizes recordings by path_name, not camera.id, so this is
    # a soft reference (joined to cameras.path_name), not a DB-level FK.
    camera_path_name = Column(String, nullable=False)
    path = Column(Text, nullable=False, unique=True)
    bytes = Column(Integer, default=0)
    sha256 = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
