"""Bounded connection/health events. No packets, credentials, paths or inputs."""
from collections import deque
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import math
from pathlib import Path
import threading
import time
import uuid


DETAILS = {
    "server_started": "Companion backend started.",
    "server_stopped": "Companion backend stopped.",
    "restart_requested": "A local companion restart or stop was requested.",
    "stream_opened": "A browser opened the live data stream.",
    "stream_closed": "A live data stream closed; this can also mean a tab closed or reloaded.",
    "stream_error": "The live stream failed while producing an update.",
    "session_expired": "A live stream needs the browser to pair again.",
    "snapshot_failed": "A dashboard update could not be produced.",
    "snapshot_slow": "Producing a dashboard update took longer than one second.",
    "snapshot_recovered": "Dashboard update generation returned below one second.",
    "telemetry_fresh": "Fresh DCS telemetry is available.",
    "telemetry_stale": "DCS telemetry is stale or unavailable; the browser may still be connected.",
    "awareness_available": "Current mission-awareness data is available.",
    "awareness_unavailable": "Current mission-awareness data is unavailable; this is separate from the browser connection.",
}


class ConnectionDiagnostics:
    def __init__(self, runtime_dir=None, *, clock=time.time, max_bytes=1024*1024):
        self.clock = clock
        self.server_id = uuid.uuid4().hex[:12]
        self.started_at = self._timestamp()
        self.events = deque(maxlen=200)
        self.lock = threading.RLock()
        self.last_health = {}
        self.last_observed = float("-inf")
        self.stopping = False
        self.write_error = False
        self.handler = None
        if runtime_dir is not None:
            try:
                root = Path(runtime_dir)
                root.mkdir(parents=True, exist_ok=True)
                self.handler = RotatingFileHandler(root / "connection-events.jsonl", maxBytes=max_bytes,
                                                   backupCount=2, encoding="utf-8", delay=True)
                self.handler.setFormatter(logging.Formatter("%(message)s"))
            except OSError:
                self.write_error = True

    def _timestamp(self):
        return datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(timespec="milliseconds")

    def event(self, kind, **fields):
        if kind not in DETAILS:
            return
        with self.lock:
            row = {"timestamp": self._timestamp(), "kind": kind, "detail": DETAILS[kind], "server_id": self.server_id}
            # Explicit allowlist: never log arbitrary exception messages or request data.
            duration = fields.get("duration_ms")
            if isinstance(duration, (int, float)) and math.isfinite(duration) and duration >= 0:
                row["duration_ms"] = round(duration, 1)
            if fields.get("channel") in ("poll", "stream"):
                row["channel"] = fields["channel"]
            self.events.append(row)
            if self.handler:
                try:
                    line = json.dumps(row, ensure_ascii=True)
                    record = logging.LogRecord("connection", logging.INFO, "", 0, line, (), None)
                    # Handle the actual write here so disk errors become visible, not fatal.
                    if self.handler.shouldRollover(record):
                        self.handler.doRollover()
                    if self.handler.stream is None:
                        self.handler.stream = self.handler._open()
                    self.handler.stream.write(line + "\n")
                    self.handler.flush()
                except OSError:
                    self.write_error = True

    def observe(self, snapshot, duration_ms, observed_at, channel):
        health = snapshot.get("health") or {}
        awareness = snapshot.get("awareness") or {}
        values = {"telemetry": health.get("telemetry_fresh") is True,
                  "awareness": awareness.get("status") == "Available",
                  "slow": duration_ms >= 1000}
        names = {"telemetry": ("telemetry_stale", "telemetry_fresh"),
                 "awareness": ("awareness_unavailable", "awareness_available"),
                 "slow": ("snapshot_recovered", "snapshot_slow")}
        with self.lock:
            # A slow older concurrent request cannot overwrite a later observation.
            if observed_at < self.last_observed:
                return
            self.last_observed = observed_at
            for key, value in values.items():
                previous = self.last_health.get(key)
                if previous != value and not (previous is None and key == "slow" and not value):
                    self.event(names[key][int(value)], duration_ms=duration_ms if key == "slow" else None, channel=channel)
                self.last_health[key] = value

    def snapshot(self):
        with self.lock:
            return {"server_id": self.server_id, "started_at": self.started_at,
                    "events": [dict(row) for row in self.events], "log_file": "connection-events.jsonl",
                    "log_write_error": self.write_error, "retention": "Current process: 200 events; disk: three files of about 1 MiB each."}

    def close(self):
        with self.lock:
            if self.handler:
                self.handler.close()
