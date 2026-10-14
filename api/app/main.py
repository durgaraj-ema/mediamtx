import asyncio
import hashlib
import os
import random
import uuid
from datetime import datetime, timedelta

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from . import mediamtx_client
from .crypto import encrypt
from .db import Base, SessionLocal, engine, get_db
from .janitor import start_background as start_janitor
from .models import Camera, CameraAssignment, LiveEvent, RecordingSegment
from .redis_client import r as redis
from .schemas import AssignmentIn, CameraCreate, CameraOut, EventIn, EventOut

VI_ENABLE_LIVE = os.environ.get("VI_ENABLE_LIVE", "false").lower() == "true"
MEDIAMTX_RTSP_URL = os.environ.get("MEDIAMTX_RTSP_URL", "rtsp://localhost:8554")
MEDIAMTX_HLS_URL = os.environ.get("MEDIAMTX_HLS_URL", "http://localhost:8888")
ASSIGNMENT_STALE_SECONDS = 20

app = FastAPI(title="VI Live Camera Control Plane (POC)")


@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)
    start_janitor()


def _camera_out(cam: Camera, db: Session) -> CameraOut:
    worker = None
    a = db.query(CameraAssignment).filter_by(camera_id=cam.id).first()
    if a and a.expires_at > datetime.utcnow():
        worker = a.worker_id
    return CameraOut(
        id=cam.id,
        name=cam.name,
        site=cam.site,
        path_name=cam.path_name,
        codec=cam.codec,
        resolution=cam.resolution,
        analytics_profile=cam.analytics_profile,
        enabled=cam.enabled,
        status=cam.status,
        last_seen_at=cam.last_seen_at,
        created_at=cam.created_at,
        hls_url=f"{MEDIAMTX_HLS_URL}/{cam.path_name}/index.m3u8",
        assigned_worker=worker,
    )


# ---------------------------------------------------------------- cameras --

@app.post("/api/live/cameras", response_model=CameraOut)
def create_camera(payload: CameraCreate, db: Session = Depends(get_db)):
    if not VI_ENABLE_LIVE:
        raise HTTPException(503, "live ingest is disabled (VI_ENABLE_LIVE=false)")

    cam_id = uuid.uuid4().hex
    path_name = f"cam/{cam_id[:12]}"

    cam = Camera(
        id=cam_id,
        name=payload.name,
        site=payload.site,
        rtsp_url_enc=encrypt(payload.rtsp_url),
        path_name=path_name,
        codec=payload.codec,
        resolution=payload.resolution,
        analytics_profile={
            "frame_interval": payload.frame_interval,
            "motion_threshold": payload.motion_threshold,
            "min_area_frac": payload.min_area_frac,
            "cooldown_seconds": payload.cooldown_seconds,
        },
        enabled=True,
        status="unknown",
    )
    db.add(cam)
    db.commit()
    db.refresh(cam)

    try:
        mediamtx_client.add_path(path_name, payload.rtsp_url)
    except Exception as e:
        # Recording/routing config failed — surface it, but the camera row
        # (and credentials) are already safely stored for retry.
        raise HTTPException(502, f"camera saved but MediaMTX registration failed: {e}")

    return _camera_out(cam, db)


@app.get("/api/live/cameras", response_model=list[CameraOut])
def list_cameras(db: Session = Depends(get_db)):
    cams = db.query(Camera).order_by(Camera.created_at.desc()).all()
    # Refresh liveness from MediaMTX so the UI (which only polls
    # this list) never shows a stale "unknown" status.
    for cam in cams:
        live = mediamtx_client.path_status(cam.path_name)
        if live is not None:
            cam.status = "online" if live.get("ready") else "offline"
            cam.last_seen_at = datetime.utcnow()
    db.commit()
    return [_camera_out(cam, db) for cam in cams]


@app.get("/api/live/cameras/{camera_id}/status", response_model=CameraOut)
def camera_status(camera_id: str, db: Session = Depends(get_db)):
    cam = db.query(Camera).filter_by(id=camera_id).first()
    if not cam:
        raise HTTPException(404, "camera not found")
    live = mediamtx_client.path_status(cam.path_name)
    if live:
        cam.status = "online" if live.get("ready") else "offline"
        cam.last_seen_at = datetime.utcnow()
        db.commit()
    return _camera_out(cam, db)


@app.delete("/api/live/cameras/{camera_id}")
def delete_camera(camera_id: str, db: Session = Depends(get_db)):
    cam = db.query(Camera).filter_by(id=camera_id).first()
    if not cam:
        raise HTTPException(404, "camera not found")
    mediamtx_client.remove_path(cam.path_name)
    redis.delete(f"lease:cam:{camera_id}")
    db.query(CameraAssignment).filter_by(camera_id=camera_id).delete()
    db.delete(cam)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------- internal: --
# endpoints used by worker processes only (camera discovery + lease mirror)

@app.get("/internal/cameras/enabled")
def internal_enabled_cameras(db: Session = Depends(get_db)):
    """Cameras workers may claim. Returns the MediaMTX *restream* URL, never
    the original camera credentials — workers read a copy, never the camera
    (§25.2 "analytics reads a copy")."""
    cams = db.query(Camera).filter_by(enabled=True).all()
    return [
        {
            "id": c.id,
            "path_name": c.path_name,
            "restream_url": f"{MEDIAMTX_RTSP_URL}/{c.path_name}",
            "analytics_profile": c.analytics_profile,
        }
        for c in cams
    ]


@app.post("/internal/assignments/{camera_id}/lease")
def internal_lease(camera_id: str, payload: AssignmentIn, db: Session = Depends(get_db)):
    existing = db.query(CameraAssignment).filter_by(camera_id=camera_id).first()
    expires = datetime.utcnow() + timedelta(seconds=payload.ttl_seconds)
    if existing:
        existing.worker_id = payload.worker_id
        existing.leased_at = datetime.utcnow()
        existing.expires_at = expires
    else:
        db.add(
            CameraAssignment(
                camera_id=camera_id,
                worker_id=payload.worker_id,
                expires_at=expires,
            )
        )
    db.commit()
    return {"ok": True}


@app.delete("/internal/assignments/{camera_id}")
def internal_release(camera_id: str, db: Session = Depends(get_db)):
    db.query(CameraAssignment).filter_by(camera_id=camera_id).delete()
    db.commit()
    return {"ok": True}


# -------------------------------------------------------------- events --

def _deterministic_event_id(camera_id: str, event_type: str, start_ts: datetime, track_id: str = "") -> str:
    raw = f"{camera_id}|{track_id}|{start_ts.isoformat()}|{event_type}"
    return hashlib.sha256(raw.encode()).hexdigest()


async def _verify_event(event_id: str):
    """Stand-in for the GPU-scheduled VLM verification stage (§25.4/§25.5).
    Swap this for a real call into the existing job queue / GPU scheduler.
    """
    await asyncio.sleep(random.uniform(1, 3))
    db = SessionLocal()
    try:
        ev = db.query(LiveEvent).filter_by(event_id=event_id).first()
        if not ev:
            return
        roll = random.random()
        if roll < 0.05:
            ev.verification_status = "unavailable"
            ev.verification_note = "verification budget exceeded (simulated)"
        else:
            objs = ev.objects or []
            confident = any(o.get("confidence", 0) >= 0.5 for o in objs) if objs else roll > 0.3
            ev.verification_status = "confirmed" if confident else "rejected"
        db.commit()
    finally:
        db.close()


@app.post("/api/live/events", response_model=EventOut)
def ingest_event(payload: EventIn, background: BackgroundTasks, db: Session = Depends(get_db)):
    event_id = payload.event_id or _deterministic_event_id(
        payload.camera_id, payload.event_type, payload.start_ts
    )
    existing = db.query(LiveEvent).filter_by(event_id=event_id).first()
    if existing:
        return existing  # idempotent: replay/restart never duplicates

    ev = LiveEvent(
        event_id=event_id,
        camera_id=payload.camera_id,
        event_type=payload.event_type,
        start_ts=payload.start_ts,
        end_ts=payload.end_ts,
        objects=payload.objects,
        snapshot_ref=payload.snapshot_ref,
        verification_status="pending",
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)

    background.add_task(asyncio.run, _verify_event(event_id))
    return ev


@app.get("/api/live/events", response_model=list[EventOut])
def list_events(
    camera_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    q = db.query(LiveEvent)
    if camera_id:
        q = q.filter_by(camera_id=camera_id)
    if status:
        q = q.filter_by(verification_status=status)
    return q.order_by(LiveEvent.created_at.desc()).limit(min(limit, 500)).all()


@app.get("/api/live/recordings")
def list_recordings(db: Session = Depends(get_db)):
    rows = db.query(RecordingSegment).order_by(RecordingSegment.created_at.desc()).limit(200).all()
    return [
        {
            "camera_path_name": row.camera_path_name,
            "path": row.path,
            "bytes": row.bytes,
            "sha256": row.sha256,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@app.get("/api/live/workers")
def list_workers(db: Session = Depends(get_db)):
    """Observability: which worker owns which camera right now, and overall
    shard spread — the horizontal-scaling proof surface."""
    rows = db.query(CameraAssignment).all()
    by_worker: dict[str, list[str]] = {}
    for a in rows:
        if a.expires_at > datetime.utcnow():
            by_worker.setdefault(a.worker_id, []).append(a.camera_id)
    return [{"worker_id": w, "camera_count": len(c), "camera_ids": c} for w, c in by_worker.items()]


# ----------------------------------------------------------------- UI --

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")
