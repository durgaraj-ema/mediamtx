# Live Camera Ingest — POC (Revision 3 architecture)

Proves out the chain from the plan: **cameras → MediaMTX → GPU analytics worker → events → verification → UI**,
built so the pieces that must scale to 1000+ cameras (camera registry, stream routing, worker assignment)
are dynamic end-to-end — nothing is a static YAML list or a fixed camera↔worker map.

## What's real vs. simulated

| Component | This POC | Production (per the plan) |
|---|---|---|
| Stream routing, recording, RTSP/HLS/WebRTC | **Real MediaMTX**, paths added/removed at runtime via its control API | same |
| Camera registry, RBAC-ready schema, dynamic path mgmt | **Real** (Postgres + FastAPI) | same |
| Decode + detect + track | OpenCV frame-differencing motion detector (`worker/detector.py`) | NVIDIA DeepStream (NVDEC + TensorRT + nvtracker), see swap note below |
| Worker horizontal scaling / sharding | **Real** — Redis-lease self-assignment, add replicas to add capacity | same mechanism, capacity numbers come from §25.8 measurement |
| VLM verification | Simulated random outcome after a delay (`api/app/main.py::_verify_event`) | real GPU-scheduled VLM call |
| Recording manifest (SHA-256) | **Real** — background janitor hashes completed segments | same |
| Credential encryption | **Real** (Fernet, demo key) — swap key source for a KMS in production | KMS-backed |

The event schema, idempotent `event_id`, and the "recording never depends on analytics" separation are
enforced exactly as designed, so swapping the detector for a real DeepStream pipeline later is a drop-in
replacement for `worker/detector.py` — the worker's lease/assignment loop and the API's event contract
don't change.

## Why this scales to 1000+ cameras without hardcoding

- **Cameras are rows, not config.** Adding camera #1001 is `POST /api/live/cameras`; MediaMTX gets the new
  path via its runtime control API (`api/app/mediamtx_client.py`) — no file edit, no restart, other cameras
  unaffected.
- **Workers are stateless and interchangeable.** Each worker polls the camera list and tries to lease
  unclaimed cameras in Redis (`SET NX ... EX ttl`) up to its own `WORKER_CAPACITY`. There is no central
  scheduler and no camera→worker table to hand-edit. To add capacity: `docker compose up --scale worker=20`.
  A crashed worker's leases expire and another worker picks those cameras up within one poll interval —
  visible live in the UI's "Worker shard distribution" panel.
- **Analytics never touches the camera directly.** Workers read the MediaMTX *restream* URL
  (`rtsp://mediamtx:8554/<path>`), so recording and live view are unaffected by worker load or crashes.

## Run it

```
docker compose up --build
```

Then open **http://localhost:8000**. A synthetic test-pattern "camera" is published automatically
(`testsource` service) so you can click **"Use demo test-pattern camera"** → **Add camera** and see the
whole pipeline — live preview, motion events, verification status — without any real RTSP camera.

To add a real camera, just paste its RTSP URL (`rtsp://user:pass@host:554/stream`) into the form.

### Scale workers

```
docker compose up --scale worker=5
```

Watch the "Worker shard distribution" panel rebalance as replicas join — no config change needed.

## Key endpoints

- `POST /api/live/cameras` — register a camera (encrypts credentials, creates the MediaMTX path)
- `GET /api/live/cameras` — registry + live status + assigned worker
- `GET /api/live/events` — event feed with verification status
- `GET /api/live/workers` — current shard distribution
- `GET /api/live/recordings` — hashed recording-segment manifest
- `GET /internal/cameras/enabled` / `POST /internal/assignments/{id}/lease` — worker-internal, not for the UI

## Known POC shortcuts (called out, not hidden)

- Single MediaMTX instance (plan notes multi-host MediaMTX is a later extension, §25.8).
- Motion-diff detector instead of DeepStream — accuracy is not the point here, the dynamic-registry and
  horizontal-scaling mechanics are.
- Verification is a random delay, not a real VLM call.
- No auth/RBAC on the UI/API yet (plan requires per-camera RBAC + short-lived viewer tokens, §25.10) —
  out of scope for this POC's purpose (proving the dynamic/horizontal-scaling plumbing).
