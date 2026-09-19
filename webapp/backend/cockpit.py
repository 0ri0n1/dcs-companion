"""Full exported cockpit text, with no pixel reconstruction or system inference."""
import time

from .sensors import AIRCRAFT, MAX_AGE_S, _age, _dict, _finite


HORNET_DISPLAY_LABELS = {"1": "HUD", "2": "Left DDI", "3": "Right DDI", "4": "AMPCD",
                         "5": "IFEI", "6": "UFC", "7": "RWR"}


def _display_ids(displays):
    # Seven known display slots stay visible even when their pages have no text.
    ids = set(HORNET_DISPLAY_LABELS)
    ids.update(key for key in _dict(displays.get("raw")) if isinstance(key, str))
    survey = displays.get("survey_ids")
    if isinstance(survey, list):
        ids.update(str(value) for value in survey
                   if isinstance(value, (str, int)) and not isinstance(value, bool))
    return sorted(ids, key=lambda value: (0, int(value)) if value.lstrip("-").isdigit() and len(value) < 10 else (1, value))


def _context_gate(raw, health, now):
    """The complete text feed may contain sensor text, so respect all denials."""
    ac, stream = _dict(raw.get("aircraft")), _dict(raw.get("stream"))
    name = ac.get("aircraft")
    capability = _dict(raw.get("capabilities"))
    probe = _dict(capability.get("probe"))
    verdicts = _dict(capability.get("verdicts"))
    health_verdicts = _dict(health.get("export_capabilities"))
    if any(source.get(key) == "DENIED" for source in (verdicts, health_verdicts)
           for key in ("ownship", "cockpit", "sensor")):
        return "Denied", "Ownship, cockpit or sensor export is denied; display text is withheld."
    if not isinstance(name, str) or not name:
        return "Unavailable", "No current aircraft identity."
    if health.get("aircraft") != name:
        return "Unverified", "Aircraft identity differs from the health snapshot."
    if health.get("dcs_running") is not True or stream.get("exporter_connected") is not True:
        return "Unavailable", "No connected aircraft telemetry; retained text is withheld."
    if (probe.get("settled") is not True or probe.get("aircraft") != name
            or any(verdicts.get(key) != "AVAILABLE" for key in ("ownship", "cockpit", "sensor"))):
        return "Unverified", "A settled matching-aircraft export probe is required."
    written = raw.get("written_epoch")
    if not _finite(now) or not _finite(written) or now < written - 0.5:
        return "Stale", "Collector time cannot establish freshness."
    age = _age(stream.get("data_age_seconds"), max(0.0, now - written))
    if (stream.get("live") is not True or health.get("telemetry_fresh") is not True
            or age is None or age > MAX_AGE_S):
        return "Stale", "Ownship is stale; display identity cannot be established as current."
    if health.get("cockpit") is not True:
        return "Unavailable", "A current cockpit has not been established."
    return None, None


def _display_evidence(display_id, raw, now):
    """Apply sensors.py's independent timestamp contract to any aircraft text.

    Returns identity/time validation independently from age, so genuine old text
    may be shown as historical without guessing across an aircraft/session change.
    """
    displays, ac = _dict(raw.get("cockpit_displays")), _dict(raw.get("aircraft"))
    identity = _dict(_dict(displays.get("identity_by_id")).get(display_id))
    session = displays.get("display_session")
    if (not isinstance(session, str) or not session
            or identity.get("display_session") != session
            or identity.get("aircraft") != ac.get("aircraft")
            or not ac.get("unit_name") or identity.get("unit_name") != ac.get("unit_name")):
        return None, False, "Display aircraft/unit/session identity is unverified."
    epoch = _dict(displays.get("received_epoch_by_id")).get(display_id)
    written = raw.get("written_epoch")
    if not _finite(epoch) or epoch <= 0 or epoch > written + 0.05 or now < epoch - 0.5:
        return None, False, "Individual display receive time is unverified."
    age_at_write = _dict(displays.get("age_seconds_by_id")).get(display_id)
    if not _finite(age_at_write) or age_at_write < 0 or abs(written - epoch - age_at_write) > 0.1:
        return None, False, "Individual display age metadata is missing or inconsistent."
    age = max(0.0, now - epoch)
    display_t, ownship_t = identity.get("model_time_s"), ac.get("model_time_s")
    if not _finite(display_t) or not _finite(ownship_t) or display_t > ownship_t + 1.0:
        return age, False, "Display model time does not match the current session."
    current = age <= MAX_AGE_S and ownship_t - display_t <= MAX_AGE_S
    return age, True, None if current else "Individual display text is stale."


def build_cockpit_snapshot(raw, health, now=None):
    """Return every exported string element as text, never executable markup.

    `elements` is reserved for independently fresh values. Historical values live
    only in `last_observed_elements`; neither array crosses denial/identity gates.
    No world data, raw sensor table, target classification or action is consulted.
    """
    raw, health = _dict(raw), _dict(health)
    now = time.time() if now is None else now
    displays = _dict(raw.get("cockpit_displays"))
    name = _dict(raw.get("aircraft")).get("aircraft")
    name = name if isinstance(name, str) else None
    written = raw.get("written_epoch")
    elapsed = max(0.0, now - written) if _finite(now) and _finite(written) else 0.0
    display_age = _age(displays.get("age_seconds"), elapsed)
    result = {"aircraft": name, "status": "Unavailable", "text_only": True,
              "telemetry_age_s": _age(_dict(raw.get("stream")).get("data_age_seconds"), elapsed),
              "display_age_s": display_age, "displays": [],
              "notes": ["Exact exported cockpit text only; images, geometry and unexported page content are unavailable."]}
    gate, reason = _context_gate(raw, health, now)
    raw_displays = _dict(displays.get("raw"))
    for display_id in _display_ids(displays):
        item = {"id": display_id,
                "label": HORNET_DISPLAY_LABELS.get(display_id, f"Display {display_id}") if name == AIRCRAFT else f"Display {display_id}",
                "status": "Unavailable", "age_s": None, "source": f"list_indication({display_id}) via collector.py",
                "elements": [], "last_observed_elements": [], "notes": []}
        result["displays"].append(item)
        if gate:
            item["status"], item["notes"] = gate, [reason]
            continue
        elements = raw_displays.get(display_id)
        if not isinstance(elements, dict):
            item["notes"] = ["No text packet for this display in the current session."]
            continue
        age, identity_valid, note = _display_evidence(display_id, raw, now)
        item["age_s"] = round(age, 3) if age is not None else None
        if not identity_valid:
            item["status"], item["notes"] = "Unverified", [note]
            continue
        text = [{"name": key, "value": value} for key, value in elements.items()
                if isinstance(key, str) and isinstance(value, str)]
        text.sort(key=lambda element: element["name"])
        if len(text) != len(elements):
            item["notes"].append("Malformed non-text elements were omitted.")
        if health.get("model_advancing") is not True:
            item["status"] = "Unavailable"
            item["last_observed_elements"] = text
            item["notes"].append("Simulation is paused or advancement unverified; last observed text only.")
        elif note or display_age is None or display_age > MAX_AGE_S or health.get("displays_fresh") is not True:
            item["status"] = "Stale"
            item["last_observed_elements"] = text
            item["notes"].append(note or "Aggregate display stream is stale; last observed text only.")
        else:
            item["status"] = "Available"
            item["elements"] = text
            if not text:
                item["notes"].append("The fresh display packet contains no text elements.")
    if gate:
        result["status"] = gate
        result["notes"].append(reason)
    else:
        statuses = {item["status"] for item in result["displays"]}
        result["status"] = next((status for status in ("Available", "Stale", "Unverified") if status in statuses), "Unavailable")
    return result
