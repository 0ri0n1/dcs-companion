"""Read the existing collector file. Never open a telemetry socket."""
import hashlib
import json
import math
import time
import threading
from pathlib import Path
import psutil


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def process_snapshot():
    matches = []
    disappeared = 0
    # process_iter reuses Process objects across threads. Its optional attrs
    # overwrite shared proc.info; keep this census in private dictionaries.
    for proc in psutil.process_iter():
        try:
            info = proc.as_dict(attrs=["pid", "name", "create_time"])
        except psutil.NoSuchProcess:
            disappeared += 1
            continue
        if (info["name"] or "").lower() == "dcs.exe":
            matches.append(info)
    if len(matches) != 1:
        return {"running": bool(matches), "identity": None, "focused": False,
                "matches": len(matches), "disappeared": disappeared}
    proc = matches[0]
    try:
        from input_sender import foreground_process
        focused = foreground_process().lower() == "dcs.exe"
    except (ImportError, AttributeError, OSError):
        focused = False
    return {"running": True, "identity": f"{proc['pid']}:{proc['create_time']}", "focused": focused,
            "matches": 1, "disappeared": disappeared}


class TelemetryReader:
    READ_ATTEMPTS = 6
    READ_RETRY_DELAY = .12

    def __init__(self, path: Path, clock=time.time, processes=process_snapshot, *, sleep=time.sleep):
        self.path, self.clock, self.processes = path, clock, processes
        self.sleep = sleep
        self.last_model = None
        self.last_sample = None
        self.advanced_at = None
        self.collector_epoch = None
        self.generation = 0
        self.generation_reason = None
        self.read_failures = 0
        self.last_read_error = None
        self.lock = threading.RLock()

    def read(self):
        with self.lock:
            return self._read()

    def _read(self):
        # Windows can deny an open briefly while the collector replaces its
        # file. Reopen current state on each bounded attempt. Never substitute
        # the last successful sample; exhausted reads still normalize as invalid.
        for attempt in range(self.READ_ATTEMPTS):
            read_status = 'ok'
            try:
                if self.path.stat().st_size > 16_000_000:
                    raise ValueError("oversize state")
                data = json.loads(self.path.read_text(encoding="utf-8-sig"))
                if not isinstance(data, dict):
                    raise ValueError("invalid state")
                break
            except (OSError, ValueError) as exc:
                data = {}
                read_status = ('missing' if isinstance(exc, FileNotFoundError) else
                               'permission' if isinstance(exc, PermissionError) else
                               'io' if isinstance(exc, OSError) else 'invalid')
                self.read_failures += 1
                self.last_read_error = {'status': read_status, 'epoch': self.clock(),
                                        'winerror': getattr(exc, 'winerror', None)}
                if attempt + 1 < self.READ_ATTEMPTS:
                    self.sleep(self.READ_RETRY_DELAY)
        result = self.normalize(data, self.processes())
        result['context']['telemetry_evidence'].update(
            read_status=read_status, read_attempts=attempt+1, read_failures=self.read_failures,
            last_read_error=dict(self.last_read_error) if self.last_read_error else None)
        return result

    def normalize(self, data, process):
        with self.lock:
            return self._normalize(data, process)

    def _normalize(self, data, process):
        now = self.clock()
        stream = data.get("stream") or {}
        ac = data.get("aircraft") or {}
        written = data.get("written_epoch")
        elapsed = now - written if finite(written) else None
        valid_clock = elapsed is not None and -0.5 <= elapsed
        age = stream.get("data_age_seconds")
        age = max(0, elapsed) + age if valid_clock and finite(age) and age >= 0 else None
        disp = (data.get("cockpit_displays") or {}).get("age_seconds")
        disp = max(0, elapsed) + disp if valid_clock and finite(disp) and disp >= 0 else None
        fresh = bool(stream.get("live") and age is not None and age <= 3.0)
        model = ac.get("model_time_s")
        sample = written - stream.get("data_age_seconds", 0) if finite(written) and finite(stream.get("data_age_seconds")) else None
        if sample != self.last_sample and finite(model):
            if self.last_model is not None and model < self.last_model - 0.05:
                self.generation += 1
                self.generation_reason = 'model-reset'
                self.advanced_at = None
            elif self.last_model is not None and model > self.last_model:
                self.advanced_at = now
            self.last_model, self.last_sample = model, sample
        advancing = self.advanced_at is not None and now - self.advanced_at <= 2.0
        uptime = stream.get("collector_uptime_seconds")
        if finite(written) and finite(uptime):
            epoch = written - uptime
            if self.collector_epoch is None or abs(epoch - self.collector_epoch) > 1:
                self.collector_epoch = epoch
                self.generation += 1
                self.generation_reason = 'collector-epoch'
        counts = stream.get("packet_counts") or {}
        session_evidence = (process.get("identity"), self.generation, counts.get("hello"), counts.get("bye"))
        session = hashlib.sha256(repr(session_evidence).encode()).hexdigest()[:20]
        explicit_mission = data.get("mission") or (data.get("raw_latest") or {}).get("mission")
        mission = json.dumps(explicit_mission, sort_keys=True) if explicit_mission else f"export-session:{session}"
        lat, lon = ac.get("latitude"), ac.get("longitude")
        position_valid = finite(lat) and finite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180
        cockpit = bool(ac.get("aircraft") and position_valid and stream.get("exporter_connected") and fresh)
        status = "Offline" if not process.get("running") else "DCS menus/no aircraft" if not stream.get("exporter_connected") or not ac.get("aircraft") else "Stale telemetry" if not fresh else "Live"
        capability = data.get("capabilities") or {}
        settled = bool((capability.get("probe") or {}).get("settled"))
        verdicts = capability.get("verdicts") or {}
        sensor = "Export denied" if settled and verdicts.get("sensor") == "DENIED" else "Stale" if not fresh else "Unverified"
        display_fresh = bool(fresh and disp is not None and disp <= 3.0)
        if status == "Live" and settled and verdicts.get("ownship") == "DENIED":
            status = "Limited export"
            cockpit = False
        health = {"status": status, "aircraft": ac.get("aircraft"), "mission": mission,
                  "mission_identity_source": "collector exporter lifecycle + DCS process + model-time reset; title unavailable" if not explicit_mission else "collector",
                  "session": session, "telemetry_age_s": age, "display_age_s": disp,
                  "telemetry_fresh": fresh, "displays_fresh": display_fresh,
                  "dcs_running": process.get("running", False), "dcs_focused": process.get("focused", False),
                  "process_identity": process.get("identity"), "cockpit": cockpit,
                  "model_advancing": advancing, "export_capabilities": verdicts if settled else {},
                  "sensor_status": sensor, "physical_input_status": "Unavailable",
                  "notes": ["Ownship and cockpit-display ages are evaluated separately.",
                            "Physical device observation is not installed; browser keys are not device evidence.",
                            "Onboard sensor contacts await aircraft-specific validation; mission awareness is a separate single-player layer.",
                            "Observed state changes do not establish who caused them."]}
        return {"raw": data, "health": health, "aircraft": {k: v for k, v in ac.items() if k not in ("sensor", "stations", "world_object_count")},
                "context": {"aircraft": ac.get("aircraft"), "session": session, "mission": mission,
                            "process": process.get("identity"), "telemetry_valid": fresh and cockpit,
                            "display_valid": display_fresh, "focused": process.get("focused", False),
                            "model_advancing": advancing, "sample_epoch": sample, "model_time": model,
                            "telemetry_evidence": {
                                "generation": self.generation, "generation_reason": self.generation_reason,
                                "collector_epoch": self.collector_epoch, "written_epoch": written,
                                "hello": counts.get("hello"), "bye": counts.get("bye"),
                                "process_matches": process.get("matches"),
                                "process_disappeared": process.get("disappeared")}}}
