"""Cheap per-frame motion detector + simple centroid tracker.

Stand-in for the DeepStream pipeline (NVDEC decode -> nvstreammux batching
-> TensorRT detector -> nvtracker -> nvdsanalytics) described in §25.4. Same
job, same event contract — swap this module for a real DeepStream/Service
Maker pipeline without touching the worker's assignment/lease logic or the
API's event schema.

Reads frames from the MediaMTX restream only (never the camera directly),
per the "analytics reads a copy" principle.
"""
import time
import uuid
from dataclasses import dataclass, field

import cv2
import numpy as np


@dataclass
class CameraProfile:
    frame_interval: int = 3
    motion_threshold: int = 25
    min_area_frac: float = 0.01
    cooldown_seconds: int = 10


@dataclass
class DetectorState:
    prev_gray: np.ndarray | None = None
    last_event_ts: float = 0.0
    frame_idx: int = 0


class CameraDetector:
    """Runs in its own thread, one per assigned camera."""

    def __init__(self, camera_id: str, path_name: str, restream_url: str, profile: dict, on_event, on_status):
        self.camera_id = camera_id
        self.path_name = path_name
        self.restream_url = restream_url
        self.profile = CameraProfile(**{k: v for k, v in profile.items() if k in CameraProfile.__dataclass_fields__})
        self.on_event = on_event      # callback(event dict)
        self.on_status = on_status    # callback(camera_id, status: "online"/"offline")
        self._stop = False
        self._state = DetectorState()

    def stop(self):
        self._stop = True

    def _capture_url(self, url: str) -> str:
        # Force a short connect timeout and TCP so an unreachable / offline
        # path fails fast instead of letting FFmpeg spin on RTSP retries
        # (which can wedge the worker when cameras are added/removed).
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}rtsp_transport=tcp&timeout=2500000"

    def run(self):
        backoff = 1
        while not self._stop:
            cap = cv2.VideoCapture(self._capture_url(self.restream_url))
            if not cap.isOpened():
                self.on_status(self.camera_id, "offline")
                time.sleep(min(backoff, 15))
                backoff = min(backoff * 2, 15)
                continue

            self.on_status(self.camera_id, "online")
            backoff = 1
            self._process_stream(cap)
            cap.release()
            if not self._stop:
                self.on_status(self.camera_id, "offline")
                time.sleep(2)  # reconnect backoff; recording is unaffected (MediaMTX owns it)

    def _process_stream(self, cap: cv2.VideoCapture):
        s = self._state
        p = self.profile
        while not self._stop:
            ok, frame = cap.read()
            if not ok:
                return  # triggers reconnect in run()

            s.frame_idx += 1
            if s.frame_idx % p.frame_interval != 0:
                continue

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (21, 21), 0)

            if s.prev_gray is None:
                s.prev_gray = gray
                continue

            diff = cv2.absdiff(s.prev_gray, gray)
            s.prev_gray = gray
            _, thresh = cv2.threshold(diff, p.motion_threshold, 255, cv2.THRESH_BINARY)
            thresh = cv2.dilate(thresh, None, iterations=2)
            contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            frame_area = frame.shape[0] * frame.shape[1]
            objects = []
            for c in contours:
                area = cv2.contourArea(c)
                if area / frame_area < p.min_area_frac:
                    continue
                x, y, w, h = cv2.boundingRect(c)
                objects.append(
                    {
                        "class": "motion",
                        "confidence": round(min(1.0, area / frame_area * 20), 2),
                        "track_id": uuid.uuid4().hex[:8],
                        "bbox": [x, y, x + w, y + h],
                    }
                )

            now = time.time()
            if objects and (now - s.last_event_ts) >= p.cooldown_seconds:
                s.last_event_ts = now
                self.on_event(
                    {
                        "camera_id": self.camera_id,
                        "event_type": "motion",
                        "objects": objects,
                    }
                )
