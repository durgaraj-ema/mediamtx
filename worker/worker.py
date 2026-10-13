"""DeepStream-worker stand-in (§25.3 "deepstream worker").

Horizontal scaling model: workers are stateless and interchangeable. Each
instance independently tries to lease cameras in Redis (SET NX ... EX ttl)
up to its own capacity. No central scheduler assigns shards and nothing is
hardcoded — scale out by starting more worker replicas
(`docker compose up --scale worker=N`); each new replica just starts
grabbing unleased cameras. A crashed worker's leases expire within
LEASE_TTL seconds and another worker picks those cameras up automatically,
with no duplicate events (event_id is deterministic).
"""
import os
import socket
import threading
import time
import uuid
from datetime import datetime, timezone

import httpx
import redis

from detector import CameraDetector

API_URL = os.environ.get("API_URL", "http://localhost:8000")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
WORKER_CAPACITY = int(os.environ.get("WORKER_CAPACITY", "50"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "5"))
LEASE_TTL = max(POLL_INTERVAL * 3, 15)

WORKER_ID = f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"

r = redis.from_url(REDIS_URL, decode_responses=True)
http = httpx.Client(base_url=API_URL, timeout=10)


class Worker:
    def __init__(self):
        self.owned: dict[str, CameraDetector] = {}
        self.threads: dict[str, threading.Thread] = {}
        self.lock = threading.Lock()

    def lease_key(self, camera_id: str) -> str:
        return f"lease:cam:{camera_id}"

    def try_acquire(self, camera_id: str) -> bool:
        return bool(r.set(self.lease_key(camera_id), WORKER_ID, nx=True, ex=LEASE_TTL))

    def renew(self, camera_id: str) -> bool:
        # only renew if we still hold it
        val = r.get(self.lease_key(camera_id))
        if val != WORKER_ID:
            return False
        r.expire(self.lease_key(camera_id), LEASE_TTL)
        return True

    def release(self, camera_id: str):
        val = r.get(self.lease_key(camera_id))
        if val == WORKER_ID:
            r.delete(self.lease_key(camera_id))
        try:
            http.delete(f"/internal/assignments/{camera_id}")
        except Exception:
            pass

    def emit_event(self, event: dict):
        event["start_ts"] = datetime.now(timezone.utc).isoformat()
        try:
            http.post("/api/live/events", json=event)
        except Exception as e:
            print(f"[{WORKER_ID}] failed to post event: {e}")

    def set_status(self, camera_id: str, status: str):
        pass  # status surfaced via /status polling from the UI; kept lightweight here

    def start_camera(self, cam: dict):
        detector = CameraDetector(
            camera_id=cam["id"],
            path_name=cam["path_name"],
            restream_url=cam["restream_url"],
            profile=cam["analytics_profile"],
            on_event=self.emit_event,
            on_status=self.set_status,
        )
        t = threading.Thread(target=detector.run, daemon=True)
        with self.lock:
            self.owned[cam["id"]] = detector
            self.threads[cam["id"]] = t
        t.start()
        try:
            http.post(
                f"/internal/assignments/{cam['id']}/lease",
                json={"worker_id": WORKER_ID, "ttl_seconds": LEASE_TTL},
            )
        except Exception as e:
            print(f"[{WORKER_ID}] failed to record assignment: {e}")

    def stop_camera(self, camera_id: str):
        with self.lock:
            det = self.owned.pop(camera_id, None)
            self.threads.pop(camera_id, None)
        if det:
            det.stop()
        self.release(camera_id)

    def reconcile(self):
        try:
            resp = http.get("/internal/cameras/enabled")
            resp.raise_for_status()
            cameras = {c["id"]: c for c in resp.json()}
        except Exception as e:
            print(f"[{WORKER_ID}] failed to fetch camera list: {e}")
            return

        with self.lock:
            owned_ids = set(self.owned.keys())

        # drop cameras we own that are no longer enabled/present
        for camera_id in owned_ids - cameras.keys():
            print(f"[{WORKER_ID}] releasing {camera_id} (disabled/removed)")
            self.stop_camera(camera_id)

        # renew leases for cameras we still own
        for camera_id in owned_ids & cameras.keys():
            if not self.renew(camera_id):
                print(f"[{WORKER_ID}] lost lease for {camera_id}, stopping locally")
                self.stop_camera(camera_id)
            else:
                try:
                    http.post(
                        f"/internal/assignments/{camera_id}/lease",
                        json={"worker_id": WORKER_ID, "ttl_seconds": LEASE_TTL},
                    )
                except Exception:
                    pass

        # try to pick up unassigned cameras, bounded by capacity
        with self.lock:
            free_capacity = WORKER_CAPACITY - len(self.owned)
        if free_capacity <= 0:
            return

        for camera_id, cam in cameras.items():
            if free_capacity <= 0:
                break
            with self.lock:
                if camera_id in self.owned:
                    continue
            if self.try_acquire(camera_id):
                print(f"[{WORKER_ID}] acquired {camera_id} ({cam['path_name']})")
                self.start_camera(cam)
                free_capacity -= 1

    def run_forever(self):
        print(f"[{WORKER_ID}] starting, capacity={WORKER_CAPACITY}, poll={POLL_INTERVAL}s")
        while True:
            self.reconcile()
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    Worker().run_forever()
