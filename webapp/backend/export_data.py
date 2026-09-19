"""All received fields as explicitly diagnostic data, never tactical tracks."""
import copy
import json
from pathlib import Path
import time

from .sensors import _dict, _finite


class ExportDataReader:
    def __init__(self, probe_path=None):
        self.probe_path = Path(probe_path) if probe_path else Path.home() / "Saved Games/DCS/Logs/dcs-copilot-probe.json"
        self.signature = None
        self.probe = None

    def full_probe(self, raw):
        summary = _dict(_dict(raw.get("capabilities")).get("probe"))
        if not summary.get("settled") or not summary.get("wallclock"):
            return None
        try:
            stat = self.probe_path.stat()
            if stat.st_size > 2_000_000:
                return None
            signature = (stat.st_mtime_ns, stat.st_size)
            if self.signature != signature:
                value = json.loads(self.probe_path.read_text(encoding="utf-8-sig"))
                self.probe, self.signature = _dict(value), signature
            if all(self.probe.get(key) == summary.get(key) for key in
                   ("aircraft", "wallclock", "probe_number", "model_time", "settled")):
                return copy.deepcopy(self.probe)
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return None

    def snapshot(self, raw, health, now=None):
        raw, health = _dict(raw), _dict(health)
        now = time.time() if now is None else now
        capture = _dict(raw.get("export_capture"))
        aircraft = _dict(raw.get("aircraft")).get("aircraft")
        session = _dict(raw.get("cockpit_displays")).get("display_session")
        written = raw.get("written_epoch")
        reported_age = _dict(raw.get("stream")).get("data_age_seconds")
        clock_valid = _finite(written) and _finite(now) and now >= written - .5
        telemetry_age = (max(0, now-written) + reported_age
                         if clock_valid and _finite(reported_age) and reported_age >= 0 else None)
        live = bool(health.get("telemetry_fresh") and health.get("model_advancing")
                    and _dict(raw.get("stream")).get("live") is True
                    and telemetry_age is not None and telemetry_age <= 3)
        connected = bool(health.get("dcs_running") and _dict(raw.get("stream")).get("exporter_connected"))
        matching = connected and aircraft and health.get("aircraft") == aircraft
        verdicts = _dict(_dict(raw.get("capabilities")).get("verdicts"))
        denied = {key for source in (verdicts, _dict(health.get("export_capabilities")))
                  for key, value in source.items() if value == "DENIED"}
        groups = []
        result = {"status": "Available" if matching and live else "Last exported" if matching else "Unavailable",
                  "captured_at": raw.get("written_epoch"), "aircraft": aircraft, "session": health.get("session"),
                  "collector_session": session,
                  "groups": groups, "packet_types": sorted(_dict(_dict(raw.get("stream")).get("packet_counts"))),
                  "recording": capture.get("recording") or {"status": "Unavailable", "error": "Collector capture is not installed."},
                  "notes": ["Complete received packets are retained in a rolling local archive (64 MiB by default). Oldest files rotate automatically.",
                            "Diagnostic exports are not validated enemy contacts. Sensor fields and world objects never populate the tactical map.",
                            "The exporter can omit, trim or reject data before sending it. Missing data and FLIR video cannot be reconstructed.",
                            "Existing rwr_count counts chaff/flare inventory keys, not threats. LoGetTWSInfo is a threat-warning API, not a radar track feed."]}

        def add(key, label, data, *, status=None, age=None, source="Existing collector state", detail=None):
            groups.append({"id": key, "label": label, "source": source,
                           "status": status or ("Available" if live else "Last exported"),
                           "age_s": age, "data": data, "detail": detail})

        add("stream", "Connection and packet counts", raw.get("stream") or {}, status="Diagnostics",
            age=max(0, now - raw["written_epoch"]) if _finite(raw.get("written_epoch")) else None)
        if not matching:
            result["notes"].append("Enter a cockpit to inspect exports for the current aircraft. Previous session values are withheld.")
            return result
        if "ownship" not in denied:
            add("aircraft", "Aircraft in pilot units", {k: v for k, v in _dict(raw.get("aircraft")).items()
                if k not in ("sensor", "world_object_count")}, age=telemetry_age)
            add("derived", "Fuel and flight trends", raw.get("derived") or {}, age=telemetry_age,
                detail="Calculated from the received samples; fuel capacity estimates remain approximate.")
            add("history", "Recent flight history", raw.get("history") or {}, status="History", age=telemetry_age)
        add("capabilities", "Export availability", raw.get("capabilities") or {}, status="Diagnostics")
        full_probe = self.full_probe(raw) if not denied.intersection({"sensor", "object", "ownship", "cockpit"}) else None
        if full_probe:
            add("probe_report", "Complete exporter probe report", full_probe, status="Diagnostics",
                source=str(self.probe_path), detail="File report matches the current received probe; values are probe-time diagnostics.")
        # The latest telemetry is also retained for collectors installed before capture.
        packets = _dict(capture.get("latest"))
        if "export_capture" not in raw:
            if raw.get("raw_latest"):
                packets = {**packets, "telemetry": {"packet": raw["raw_latest"], "session": session,
                           "aircraft": aircraft, "received_epoch": now-telemetry_age if _finite(telemetry_age) else None}}
        for key, item in packets.items():
            item = _dict(item)
            if item.get("session") != session or item.get("aircraft") not in (None, aircraft):
                continue
            packet = copy.deepcopy(_dict(item.get("packet")))
            kind = packet.get("type")
            if kind == "indications" and denied.intersection({"sensor", "ownship", "cockpit"}):
                add("packet:"+key, "Display export "+str(packet.get("id")), None, status="Export denied")
                continue
            if kind == "telemetry":
                if "ownship" in denied:
                    add("packet:"+key, "Raw telemetry", None, status="Export denied")
                    continue
                if "sensor" in denied:
                    packet.pop("sensor", None)
                if "object" in denied:
                    for name in ("objects", "world_objects", "world_object_count"):
                        packet.pop(name, None)
            elif kind not in ("probe", "hello", "bye", "indication_survey", "oversize", "indications") and denied:
                add("packet:"+key, "Unclassified export "+str(kind), None, status="Export denied")
                continue
            epoch = item.get("received_epoch")
            age = max(0, now-epoch) if _finite(epoch) and epoch <= now+.5 else None
            status = "Unverified" if live and age is not None and age <= 3 else "Last exported"
            add("packet:"+key, "Raw "+str(key), packet, status=status, age=age,
                source="Exporter UDP → existing collector", detail="Complete received fields. Diagnostic meanings are unverified; not a contact feed.")
        return result
