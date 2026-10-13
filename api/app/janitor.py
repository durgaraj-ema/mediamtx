"""Recording-segment manifest janitor.

Scans the shared /recordings volume (written by MediaMTX) and hashes any
segment file MediaMTX hasn't appended to in a while, recording its SHA-256
into recording_segments — the forensic manifest (§25.7). Runs as a plain
background thread so no extra service is needed for the POC.
"""
import hashlib
import os
import threading
import time

from sqlalchemy.exc import IntegrityError

from .db import SessionLocal
from .models import RecordingSegment

RECORDINGS_DIR = os.environ.get("RECORDINGS_DIR", "/recordings")
SCAN_INTERVAL_SECONDS = int(os.environ.get("JANITOR_INTERVAL_SECONDS", "20"))
STABLE_AGE_SECONDS = 5  # only hash files not modified in the last N seconds


def _sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _scan_once():
    if not os.path.isdir(RECORDINGS_DIR):
        return
    db = SessionLocal()
    try:
        known = {row.path for row in db.query(RecordingSegment.path).all()}
        now = time.time()
        for root, _dirs, files in os.walk(RECORDINGS_DIR):
            for fname in files:
                full = os.path.join(root, fname)
                if full in known:
                    continue
                try:
                    mtime = os.path.getmtime(full)
                    size = os.path.getsize(full)
                except OSError:
                    continue
                if now - mtime < STABLE_AGE_SECONDS:
                    continue  # still being written
                camera_path = os.path.relpath(root, RECORDINGS_DIR).split(os.sep)[0]
                seg = RecordingSegment(
                    camera_path_name=camera_path,
                    path=full,
                    bytes=size,
                    sha256=_sha256_of(full),
                )
                db.add(seg)
                try:
                    db.commit()
                except IntegrityError:
                    db.rollback()
    finally:
        db.close()


def run_forever():
    while True:
        try:
            _scan_once()
        except Exception as e:  # keep the janitor alive across transient errors
            print(f"[janitor] scan error: {e}")
        time.sleep(SCAN_INTERVAL_SECONDS)


def start_background():
    t = threading.Thread(target=run_forever, daemon=True)
    t.start()
