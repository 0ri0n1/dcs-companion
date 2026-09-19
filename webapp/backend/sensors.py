"""Bounded read-only Hornet display observations; no generic/world contacts.

Labels are defined in the installed Hornet FLIR_AG.lua / FLIR_CMN.lua and were
observed without cockpit actuation. Text is not proof of physical laser output.
The collector is the only UDP owner; this module consumes its state snapshot.
"""
import math
import re
import time


AIRCRAFT = "FA-18C_hornet"
MAX_AGE_S = 3.0
SOURCE = "DCS list_indication via existing Export.lua and collector.py"
FLIR_AG = "FA-18C/Cockpit/Scripts/Multipurpose_Display_Group/Common/indicator/Pages/MPD/FLIR/FLIR_AG.lua"
FLIR_CMN = "FA-18C/Cockpit/Scripts/Multipurpose_Display_Group/Common/indicator/Pages/MPD/FLIR/FLIR_CMN.lua"

# Each value is an exact readout, never a parsed contact, allegiance or beam state.
# ATFLIR fields only: no invented inheritance for another aircraft/pod.
FIELD_SPECS = (
    ("flir", "flir.track_mode", "Tracking label", "MPD_FLIR_TrackMode_A", r"(?:SCENE|AUTO|INR|INR SCENE|INR AUTO)", FLIR_CMN),
    ("flir", "flir.zoom", "Zoom readout", "MPD_AFLIR_ZoomValue", r"\d{1,2}\.\d", FLIR_CMN),
    ("flir", "flir.zoom_text", "Zoom label", "MPD_AFLIR_ZoomValue_Z", r"Z\d{1,2}\.\d", FLIR_CMN),
    ("flir", "flir.focus", "Focus readout", "MPD_AFLIR_FocusValue", r"-?\d{1,3}", FLIR_CMN),
    ("flir", "flir.mask_label", "Pod mask label", "MPD_AFLIR_MaskLabel", r"(?:M WARN|MASK)", FLIR_CMN),
    ("flir", "flir.target_range", "Designated range label", "MPD_FLIR_TGT_Range_A", r"\d{1,4}\.\d TGT", FLIR_AG),
    ("laser", "laser.status_label", "Laser status text", "MPD_FLIR_LaserStatus_label", r"(?:L ARM|M ARM|LTD/R|MARK|LTD|MASK)", FLIR_AG),
    ("laser", "laser.lst_code", "LST code readout", "MPD_FLIR_LSTC_code_A", r"\d{4}", FLIR_CMN),
    ("laser", "laser.ltdr_code", "LTD/R code readout", "MPD_FLIR_LTDC_code_A", r"\d{4}", FLIR_CMN),
    ("laser", "laser.lst_label", "LST label", "MPD_FLIR_LSTC_label_A", r"LST", FLIR_CMN),
    ("laser", "laser.ltdr_label", "LTD/R label", "MPD_FLIR_LTDC_label_A", r"LTD/R", FLIR_CMN),
)


def _dict(value):
    return value if isinstance(value, dict) else {}


def _finite(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def _age(value, elapsed=0):
    return value + elapsed if _finite(value) and value >= 0 else None


def _layer(layer_id, title, aggregate_age):
    return {"id": layer_id, "title": title, "status": "Unverified", "source": SOURCE,
            "age_s": None, "aggregate_age_s": aggregate_age,
            "age_scope": "individual display receive time required", "fields": [],
            "last_observed_fields": [], "notes": []}


def _gate(raw, health, now):
    """No labels cross unknown/stale/denied/session-inconsistent boundaries."""
    aircraft = _dict(raw.get("aircraft"))
    name = aircraft.get("aircraft")
    stream = _dict(raw.get("stream"))
    caps = _dict(raw.get("capabilities"))
    verdicts, probe = _dict(caps.get("verdicts")), _dict(caps.get("probe"))
    if name != AIRCRAFT:
        return "Unsupported", "No verified sensor-display adapter for this aircraft."
    if health.get("aircraft") != name:
        return "Unverified", "Aircraft identity does not match the current health snapshot."
    if health.get("dcs_running") is not True or stream.get("exporter_connected") is not True:
        return "Offline", "No connected aircraft telemetry."
    # Even a cockpit scrape may not bypass a server's sensor-export denial.
    health_verdicts = _dict(health.get("export_capabilities"))
    if any(v.get(cat) == "DENIED" for v in (verdicts, health_verdicts)
           for cat in ("sensor", "cockpit", "ownship")):
        return "Export denied", "Sensor, cockpit or ownship export is denied."
    if (probe.get("settled") is not True or probe.get("aircraft") != name
            or any(verdicts.get(cat) != "AVAILABLE" for cat in ("sensor", "cockpit", "ownship"))):
        return "Unverified", "A settled, matching aircraft export-capability probe is required."
    written = raw.get("written_epoch")
    if not _finite(now) or not _finite(written) or now < written - 0.5:
        return "Stale", "The collector clock cannot establish freshness."
    elapsed = max(0.0, now - written)
    telemetry_age = _age(stream.get("data_age_seconds"), elapsed)
    display_age = _age(_dict(raw.get("cockpit_displays")).get("age_seconds"), elapsed)
    if (stream.get("live") is not True or health.get("telemetry_fresh") is not True
            or health.get("displays_fresh") is not True
            or telemetry_age is None or telemetry_age > MAX_AGE_S
            or display_age is None or display_age > MAX_AGE_S):
        return "Stale", "Ownship or aggregate display data is stale."
    if health.get("cockpit") is not True or health.get("model_advancing") is not True:
        return "Unavailable", "Aircraft simulation is paused, unestablished or outside a verified cockpit."
    return None, None


def _display_age(display_id, raw, now):
    displays = _dict(raw.get("cockpit_displays"))
    if not any(key in displays for key in ("received_epoch_by_id", "age_seconds_by_id", "identity_by_id")):
        return None, "legacy"
    epoch = _dict(displays.get("received_epoch_by_id")).get(display_id)
    identity = _dict(_dict(displays.get("identity_by_id")).get(display_id))
    aircraft = _dict(raw.get("aircraft"))
    session = displays.get("display_session")
    if (not isinstance(session, str) or not session
            or identity.get("display_session") != session
            or identity.get("aircraft") != AIRCRAFT
            or not aircraft.get("unit_name")
            or identity.get("unit_name") != aircraft.get("unit_name")):
        return None, "identity"
    written = raw.get("written_epoch")
    if not _finite(epoch) or epoch <= 0 or epoch > written + 0.05 or now < epoch - 0.5:
        return None, "invalid"
    age_at_write = _dict(displays.get("age_seconds_by_id")).get(display_id)
    if not _finite(age_at_write) or age_at_write < 0 or abs(written - epoch - age_at_write) > 0.1:
        return None, "invalid"
    age = max(0.0, now - epoch)
    # Independent model time also rejects a delayed packet from an old page/session.
    display_t, ownship_t = identity.get("model_time_s"), aircraft.get("model_time_s")
    if (not _finite(display_t) or not _finite(ownship_t)
            or display_t > ownship_t + 1.0 or ownship_t - display_t > MAX_AGE_S):
        return age, "stale"
    return age, "current" if age <= MAX_AGE_S else "stale"


def build_sensor_snapshot(raw, health, now=None):
    """Return only allowlisted exact display strings with independent provenance.

    Legacy collector snapshots may yield explicitly historical text, never current
    fields. Generic LoGetTWSInfo/locked/rwr_count and world objects are not read.
    """
    raw, health = _dict(raw), _dict(health)
    now = time.time() if now is None else now
    written = raw.get("written_epoch")
    elapsed = max(0.0, now - written) if _finite(now) and _finite(written) else 0.0
    telemetry_age = _age(_dict(raw.get("stream")).get("data_age_seconds"), elapsed)
    display_age = _age(_dict(raw.get("cockpit_displays")).get("age_seconds"), elapsed)
    layers = [_layer(key, title, display_age) for key, title in
              (("flir", "FLIR display"), ("laser", "Laser display labels"),
               ("radar", "Radar contacts"), ("rwr", "Radar warning receiver"))]
    name = _dict(raw.get("aircraft")).get("aircraft")
    result = {"aircraft": name if isinstance(name, str) else None, "status": "Unverified",
              "telemetry_age_s": telemetry_age, "display_age_s": display_age,
              "display_freshness": "Each display requires its own receive time and matching aircraft/session.",
              "layers": layers, "contacts": [], "world_data_excluded": True}
    denied_status, note = _gate(raw, health, now)
    if denied_status:
        result["status"] = denied_status
        for layer in layers:
            layer["status"], layer["notes"] = denied_status, [note]
        return result
    indexed = {layer["id"]: layer for layer in layers}
    display_dict = _dict(_dict(raw.get("cockpit_displays")).get("raw"))
    reasons = {"flir": set(), "laser": set()}
    for display_id in ("2", "3", "4"):
        elements = _dict(display_dict.get(display_id))
        age, state = _display_age(display_id, raw, now)
        for layer_id, field_id, label, element, pattern, definition in FIELD_SPECS:
            value = elements.get(element)
            if not isinstance(value, str) or len(value) > 64 or not re.fullmatch(pattern, value.strip()):
                continue
            reasons[layer_id].add(state)
            if state not in ("current", "legacy"):
                continue
            field = {"id": field_id, "label": label, "value": value, "display_id": display_id,
                     "element": element, "source": f"list_indication({display_id}).{element}",
                     "definition_source": definition, "age_s": round(age, 3) if age is not None else None,
                     "confidence": "MEASURED exact exported text; DOC local indicator definition"}
            indexed[layer_id]["fields" if state == "current" else "last_observed_fields"].append(field)
    for layer_id in ("flir", "laser"):
        layer = indexed[layer_id]
        if layer["fields"]:
            layer["status"] = "Available"
            layer["age_s"] = max(field["age_s"] for field in layer["fields"])
            layer["age_scope"] = "individual display receive time"
        elif layer["last_observed_fields"]:
            layer["age_scope"] = "aggregate display stream only"
            layer["notes"].append("Last exported display text — individual display age unknown.")
        elif "stale" in reasons[layer_id]:
            layer["status"] = "Stale"
            layer["notes"].append("The display carrying these labels has stopped refreshing.")
        elif reasons[layer_id]:
            layer["notes"].append("Display identity or receive time is unverified; values withheld.")
        else:
            layer["status"] = "Unavailable"
            layer["notes"].append("No verified ATFLIR labels on a currently exported MPD page.")
    indexed["laser"]["notes"].append("Labels and codes do not establish laser emission. Missing labels do not mean laser off.")
    indexed["flir"]["notes"].append("Display text only; the current exporter does not provide a FLIR video image.")
    indexed["radar"]["notes"] = ["No validated radar contact feed. Generic TWS/locked tables are withheld; a designation does not establish an enemy."]
    indexed["rwr"]["notes"] = ["Threat contacts and counts are unverified. Existing rwr_count is derived from chaff/flare inventory and is not a threat count."]
    if any(layer["status"] == "Available" for layer in layers):
        result["status"] = "Available"
    return result
