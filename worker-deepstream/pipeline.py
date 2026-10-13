"""Real DeepStream pipeline (§25.4 of the plan).

    rtspsrc (from MediaMTX) -> decode (NVDEC via uridecodebin/nvv4l2decoder)
      -> nvstreammux (batch of N streams)
      -> nvinfer (TensorRT primary detector)
      -> nvtracker
      -> nvdsanalytics (ROI / line crossing)
      -> probe (metadata extraction) -> event emitter
      -> fakesink (headless: no display sink, §25.4)

Requires an NVIDIA GPU + the DeepStream SDK (gi.repository.Gst and pyds,
both provided by the base image in Dockerfile — not pip-installable).

Design notes:
  * Reads exclusively from MediaMTX's restream URL, never the camera itself
    (§25.2 "analytics reads a copy") — see worker.py, which passes
    `cam["restream_url"]`.
  * One pipeline per worker process; cameras are added/removed by requesting
    or releasing an nvstreammux sink pad and tearing down only that camera's
    source bin, so one camera change never restarts the others (§25.4
    "dynamic streams", §25.9 "dynamic add/remove").
  * DeepStream 9.1 release notes deprecate `pyds` in favour of Service Maker
    (`pyservicemaker`). This module isolates all SDK-specific code so a
    future port only touches this file — worker.py's lease/assignment loop
    and the event contract it posts to the API are unchanged either way.
"""
from __future__ import annotations

import configparser
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

import pyds  # noqa: E402  (provided by the DeepStream SDK image)

Gst.init(None)

MUX_BATCH_SIZE = int(os.environ.get("DS_MUX_BATCH_SIZE", "30"))
MUX_WIDTH = int(os.environ.get("DS_MUX_WIDTH", "1280"))
MUX_HEIGHT = int(os.environ.get("DS_MUX_HEIGHT", "720"))
PGIE_CONFIG = os.environ.get("DS_INFER_CONFIG", "/app/config/pgie_config.txt")
TRACKER_CONFIG = os.environ.get("DS_TRACKER_CONFIG", "/app/config/tracker_config.yml")
ANALYTICS_CONFIG = os.environ.get("DS_ANALYTICS_CONFIG", "/app/config/nvdsanalytics_config.txt")
GPU_ID = int(os.environ.get("DS_GPU_ID", "0"))
DEFAULT_COOLDOWN_SECONDS = 10


def _read_tracker_config(path: str) -> dict:
    """tracker_config.yml is DeepStream's own ini-style [tracker] section."""
    cp = configparser.ConfigParser()
    cp.read(path)
    return dict(cp["tracker"]) if cp.has_section("tracker") else {}


class _SourceBin:
    """One RTSP source (uridecodebin, NVDEC-backed) wired into a requested
    nvstreammux sink pad. Lives entirely inside the shared pipeline; torn
    down independently of every other camera."""

    def __init__(self, camera_id: str, restream_url: str, pipeline: Gst.Pipeline, mux_pad_index: int, streammux: Gst.Element):
        self.camera_id = camera_id
        self.mux_pad_index = mux_pad_index
        self._pipeline = pipeline
        self._streammux = streammux
        self._linked = False
        self.bin = Gst.Bin.new(f"source-bin-{camera_id}")

        decode = Gst.ElementFactory.make("uridecodebin", f"uridecode-{camera_id}")
        if decode is None:
            raise RuntimeError("failed to create uridecodebin — is the DeepStream SDK image in use?")
        decode.set_property("uri", restream_url)
        decode.connect("pad-added", self._on_pad_added)
        decode.connect("child-added", self._on_child_added)
        self.bin.add(decode)

        pipeline.add(self.bin)

    def _on_child_added(self, _child_proxy, obj, name, _user_data=None):
        if "rtspsrc" in name:
            obj.set_property("latency", 200)
            obj.set_property("protocols", 4)  # GST_RTSP_LOWER_TRANS_TCP — same reliability posture as MediaMTX's own TCP-only profile

    def _on_pad_added(self, _decode, pad: Gst.Pad):
        caps = pad.get_current_caps() or pad.query_caps(None)
        if caps.get_size() == 0 or not caps.get_structure(0).get_name().startswith("video"):
            return
        ghost = Gst.GhostPad.new(f"src-{self.camera_id}", pad)
        ghost.set_active(True)
        self.bin.add_pad(ghost)
        self._link()

    def _link(self):
        if self._linked:
            return
        src_pad = self.bin.get_static_pad(f"src-{self.camera_id}")
        if src_pad is None:
            return
        try:
            sink_pad = self._streammux.request_pad_simple(f"sink_{self.mux_pad_index}")
        except AttributeError:  # older gst-python without request_pad_simple
            sink_pad = self._streammux.get_request_pad(f"sink_{self.mux_pad_index}")
        self.sink_pad = sink_pad
        if src_pad.link(sink_pad) != Gst.PadLinkReturn.OK:
            raise RuntimeError(f"failed to link source {self.camera_id} to streammux pad {self.mux_pad_index}")
        self._linked = True

    def teardown(self):
        self.bin.set_state(Gst.State.NULL)
        if getattr(self, "sink_pad", None) is not None:
            self._streammux.release_request_pad(self.sink_pad)
        self._pipeline.remove(self.bin)


@dataclass
class _CameraState:
    source_bin: _SourceBin
    profile: dict = field(default_factory=dict)
    last_event_ts: float = 0.0


class DeepStreamPipeline:
    """One shared GStreamer pipeline per worker process/GPU shard (§25.8).
    Cameras are assigned to a worker by worker.py's lease loop; this class
    only knows how to attach/detach sources from the batch."""

    def __init__(self, on_event, on_status):
        self.on_event = on_event      # callback(event dict) -> None
        self.on_status = on_status    # callback(camera_id, "online"/"offline") -> None

        self.pipeline = Gst.Pipeline.new("vi-deepstream-worker")
        self.streammux = Gst.ElementFactory.make("nvstreammux", "stream-muxer")
        self.streammux.set_property("batch-size", MUX_BATCH_SIZE)
        self.streammux.set_property("width", MUX_WIDTH)
        self.streammux.set_property("height", MUX_HEIGHT)
        self.streammux.set_property("live-source", 1)
        self.streammux.set_property("batched-push-timeout", 40000)
        self.streammux.set_property("gpu-id", GPU_ID)
        self.pipeline.add(self.streammux)

        self.pgie = Gst.ElementFactory.make("nvinfer", "primary-detector")
        self.pgie.set_property("config-file-path", PGIE_CONFIG)
        self.pgie.set_property("gpu-id", GPU_ID)
        self.pipeline.add(self.pgie)

        self.tracker = Gst.ElementFactory.make("nvtracker", "tracker")
        for key, value in _read_tracker_config(TRACKER_CONFIG).items():
            try:
                self.tracker.set_property(key.replace("-", "_"), value)
            except TypeError:
                pass  # property type mismatch (string vs int/bool) — set the common ones explicitly below
        self.tracker.set_property("gpu-id", GPU_ID)
        self.pipeline.add(self.tracker)

        self.analytics = Gst.ElementFactory.make("nvdsanalytics", "analytics")
        self.analytics.set_property("config-file", ANALYTICS_CONFIG)
        self.pipeline.add(self.analytics)

        self.sink = Gst.ElementFactory.make("fakesink", "sink")
        self.sink.set_property("sync", False)
        self.sink.set_property("qos", False)
        self.pipeline.add(self.sink)

        self.streammux.link(self.pgie)
        self.pgie.link(self.tracker)
        self.tracker.link(self.analytics)
        self.analytics.link(self.sink)

        probe_pad = self.analytics.get_static_pad("src")
        probe_pad.add_probe(Gst.PadProbeType.BUFFER, self._on_buffer, None)

        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self.bus.connect("message", self._on_bus_message)

        self._cameras: dict[str, _CameraState] = {}
        self._free_pad_indices = list(range(MUX_BATCH_SIZE))
        self._lock = threading.Lock()
        self._loop = GLib.MainLoop()
        self._thread: threading.Thread | None = None

    # -------------------------------------------------------------- lifecycle

    def start(self):
        self.pipeline.set_state(Gst.State.PLAYING)
        self._thread = threading.Thread(target=self._loop.run, daemon=True)
        self._thread.start()

    def stop(self):
        self._loop.quit()
        self.pipeline.set_state(Gst.State.NULL)

    def _on_bus_message(self, _bus, message: Gst.Message):
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print(f"[deepstream] pipeline ERROR: {err} ({debug})")
        elif t == Gst.MessageType.EOS:
            print("[deepstream] pipeline EOS")
        return True

    # ---------------------------------------------------------- camera add/remove

    def add_camera(self, camera_id: str, restream_url: str, profile: dict):
        with self._lock:
            if camera_id in self._cameras or not self._free_pad_indices:
                return False
            pad_index = self._free_pad_indices.pop(0)
            source = _SourceBin(camera_id, restream_url, self.pipeline, pad_index, self.streammux)
            source.bin.sync_state_with_parent()
            self._cameras[camera_id] = _CameraState(source_bin=source, profile=profile)
            self.on_status(camera_id, "online")
            return True

    def remove_camera(self, camera_id: str):
        with self._lock:
            state = self._cameras.pop(camera_id, None)
            if not state:
                return
            self._free_pad_indices.append(state.source_bin.mux_pad_index)
        state.source_bin.teardown()
        self.on_status(camera_id, "offline")

    def capacity_remaining(self) -> int:
        with self._lock:
            return len(self._free_pad_indices)

    # ----------------------------------------------------------------- metadata

    def _camera_for_source_id(self, source_id: int) -> str | None:
        for camera_id, state in self._cameras.items():
            if state.source_bin.mux_pad_index == source_id:
                return camera_id
        return None

    def _on_buffer(self, _pad, info: Gst.PadProbeInfo, _user_data):
        gst_buffer = info.get_buffer()
        if not gst_buffer:
            return Gst.PadProbeReturn.OK

        batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
        l_frame = batch_meta.frame_meta_list
        while l_frame is not None:
            frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
            camera_id = self._camera_for_source_id(frame_meta.source_id)

            if camera_id:
                objects = []
                l_obj = frame_meta.obj_meta_list
                while l_obj is not None:
                    obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)
                    rect = obj_meta.rect_params
                    objects.append(
                        {
                            "class": obj_meta.obj_label,
                            "confidence": round(float(obj_meta.confidence), 2),
                            "track_id": str(obj_meta.object_id),
                            "bbox": [
                                int(rect.left),
                                int(rect.top),
                                int(rect.left + rect.width),
                                int(rect.top + rect.height),
                            ],
                        }
                    )
                    l_obj = l_obj.next if l_obj.next else None

                if objects:
                    self._maybe_emit(camera_id, objects)

            l_frame = l_frame.next if l_frame.next else None

        return Gst.PadProbeReturn.OK

    def _maybe_emit(self, camera_id: str, objects: list[dict]):
        state = self._cameras.get(camera_id)
        if not state:
            return
        cooldown = state.profile.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS)
        now = time.monotonic()
        if now - state.last_event_ts < cooldown:
            return
        state.last_event_ts = now
        self.on_event(
            {
                "camera_id": camera_id,
                "event_type": "object",
                "start_ts": datetime.now(timezone.utc).isoformat(),
                "objects": objects,
            }
        )
