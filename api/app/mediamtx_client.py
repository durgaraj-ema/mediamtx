"""Thin client for MediaMTX's control API.

Camera paths are registered and removed at runtime through this API — never
by editing mediamtx.yml — so adding camera #1001 doesn't touch or restart
the router, and other cameras are unaffected (§25.4 "dynamic streams").
"""
import os

import httpx

MEDIAMTX_API_URL = os.environ.get("MEDIAMTX_API_URL", "http://localhost:9997")


def add_path(path_name: str, rtsp_source_url: str) -> None:
    body = {
        "source": rtsp_source_url,
        "sourceOnDemand": False,
        "record": True,
        "recordPath": f"/recordings/{path_name}/%Y-%m-%d_%H-%M-%S-%f",
        "recordFormat": "fmp4",
        "recordSegmentDuration": "60s",
    }
    with httpx.Client(timeout=10) as client:
        r = client.post(f"{MEDIAMTX_API_URL}/v3/config/paths/add/{path_name}", json=body)
        if r.status_code not in (200, 201):
            # path may already exist (e.g. API restarted) — try replace
            r2 = client.post(
                f"{MEDIAMTX_API_URL}/v3/config/paths/replace/{path_name}", json=body
            )
            r2.raise_for_status()


def remove_path(path_name: str) -> None:
    with httpx.Client(timeout=10) as client:
        client.delete(f"{MEDIAMTX_API_URL}/v3/config/paths/delete/{path_name}")


def path_status(path_name: str) -> dict | None:
    with httpx.Client(timeout=5) as client:
        r = client.get(f"{MEDIAMTX_API_URL}/v3/paths/get/{path_name}")
        if r.status_code != 200:
            return None
        return r.json()
