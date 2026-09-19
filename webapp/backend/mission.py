"""Read-only, bounded .miz navigation extraction. Mission Lua is never executed."""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

from .lua_data import LuaDataError, parse_assignment, array
from .navigation import (normalize_terrain, source_provenance, local_to_geo, finite,
                         valid_position, ms_to_knots, NavigationService)

MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_MISSION_BYTES = 16 * 1024 * 1024
MAX_MEMBERS = 5000


class MissionError(ValueError):
    pass


def _walk_tasks(value):
    if not isinstance(value, dict):
        return
    if value.get("id") in ("ActivateBeacon", "ActivateICLS", "Tanker", "AWACS"):
        yield value
    for child in value.values():
        if isinstance(child, dict):
            yield from _walk_tasks(child)


def _position(value, projection):
    x, z = value.get("x"), value.get("y")
    if not finite(x) or not finite(z) or abs(x) > 10_000_000 or abs(z) > 10_000_000:
        return {"lat": None, "lon": None, "local_position": None, "position_status": "Unknown"}
    lat, lon = local_to_geo(x, z, projection)
    return {"lat": lat, "lon": lon, "local_position": {"x": x, "z": z},
            "position_status": "derived" if valid_position(lat, lon) else "Unknown"}


def parse_mission(path, navigation_service=None):
    try:
        return _parse_mission(path, navigation_service)
    except (AttributeError, TypeError, KeyError, IndexError) as exc:
        raise MissionError(f"Malformed mission navigation structure: {type(exc).__name__}") from exc


def _parse_mission(path, navigation_service=None):
    path = Path(path)
    if path.suffix.lower() != ".miz" or path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise MissionError("Expected a .miz archive below 512 MiB")
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > MAX_MEMBERS or sum(i.file_size for i in members) > MAX_ARCHIVE_BYTES:
                raise MissionError("Mission archive resource limit exceeded")
            targets = [i for i in members if i.filename == "mission"]
            if len(targets) != 1:
                raise MissionError("Archive must contain exactly one mission member")
            member = targets[0]
            if member.file_size > MAX_MISSION_BYTES or member.flag_bits & 1:
                raise MissionError("Mission member too large or encrypted")
            if member.file_size / max(1, member.compress_size) > 1000:
                raise MissionError("Mission member compression ratio exceeds limit")
            # No archive member is ever extracted to the filesystem.
            data = archive.read(member)
            warehouse_members = [i for i in members if i.filename == "warehouses"]
            warehouses = {}
            if len(warehouse_members) > 1:
                raise MissionError("Duplicate warehouses member")
            if warehouse_members:
                wi = warehouse_members[0]
                if wi.file_size > MAX_MISSION_BYTES or wi.flag_bits & 1 or wi.file_size / max(1, wi.compress_size) > 1000:
                    raise MissionError("Warehouses member exceeds resource limits")
                warehouses = parse_assignment(archive.read(wi).decode("utf-8-sig"), "warehouses")
        mission = parse_assignment(data.decode("utf-8-sig"))
    except (UnicodeError, zipfile.BadZipFile, LuaDataError, RecursionError) as exc:
        raise MissionError(f"Unsupported or malformed mission data: {exc}") from exc
    terrain = normalize_terrain(mission.get("theatre"))
    if terrain is None:
        raise MissionError("Unsupported or missing mission theatre")
    service = navigation_service or NavigationService()
    cache = service.caches.get(terrain, {})
    provenance = source_provenance(path, cache.get("dcs_version", "Unknown"), terrain,
        method="bounded ZIP mission member; constrained literal Lua parser")
    provenance["coordinate_reference"] = "DCS terrain-local x/y (north/east); WGS84 derived with community projection when available"
    provenance["mission_overlay_replaced"] = True
    projection = cache.get("projection")
    overlay = {"schema": "dcs-mission-navigation/1", "terrain": terrain, "source_path": str(path.resolve()),
        "source_hash": provenance["source_hash"], "mission_identity": hashlib.sha256(data).hexdigest(),
        "title": mission.get("sortie"), "date": mission.get("date"), "start_time": mission.get("start_time"),
        "weather": {}, "routes": [], "objects": [], "beacons": [], "ownership": [], "quarantine": [],
        "position_status": "Mission planned positions — not live tracks", "provenance": provenance,
        "session_match_required": True}
    raw_weather = mission.get("weather", {})
    wind = raw_weather.get("wind", {}) if isinstance(raw_weather, dict) else {}
    ground = wind.get("atGround", {})
    overlay["weather"] = {"source": "DCS mission weather", "raw": raw_weather,
        "surface_wind": {"speed_kt": ms_to_knots(ground["speed"]) if finite(ground.get("speed")) else None,
            "dcs_direction_deg": ground.get("dir"), "from_true_deg": None,
            "direction_status": "Unverified — mission direction convention needs runtime validation"}, "provenance": provenance}
    seen = set()
    for coalition, coalition_data in mission.get("coalition", {}).items():
        if not isinstance(coalition_data, dict):
            continue
        for country in array(coalition_data.get("country")):
            for category in ("plane", "helicopter", "ship", "static", "vehicle"):
                for group in array(country.get(category, {}).get("group")):
                    gid = group.get("groupId")
                    if gid is None or gid in seen:
                        overlay["quarantine"].append({"kind": "group", "id": gid, "reason": "Missing/duplicate group identifier", "provenance": provenance})
                        continue
                    seen.add(gid)
                    units = array(group.get("units"))
                    tasks = list(_walk_tasks(group))
                    roles = {task["id"] for task in tasks}
                    players = [u for u in units if u.get("skill") in ("Player", "Client")]
                    if players:
                        points = []
                        for index, point in enumerate(array(group.get("route", {}).get("points")), 1):
                            points.append({"id": f"{gid}:wp:{index}", "index": index, "name": point.get("name") or f"Waypoint {index}",
                                "type": point.get("type"), "altitude_m": point.get("alt"), "altitude_type": point.get("alt_type"),
                                **_position(point, projection), "provenance": provenance})
                        overlay["routes"].append({"id": gid, "name": group.get("name"), "coalition": coalition,
                            "units": [{"name": u.get("name"), "aircraft": u.get("type")} for u in players],
                            "points": points, "provenance": provenance})
                    for unit in units:
                        typ = str(unit.get("type", ""))
                        if "FARP" in typ.upper() or unit.get("category") == "Heliports":
                            role = "FARP"
                        elif category == "ship" and ("ActivateICLS" in roles or "ActivateBeacon" in roles or any(s in typ.lower() for s in ("cvn", "stennis", "kuz", "tarawa", "forrestal", "invincible", "hermes"))):
                            role = "carrier/navigation ship"
                        elif category in ("plane", "helicopter") and "Tanker" in roles:
                            role = "tanker"
                        elif category == "plane" and "AWACS" in roles:
                            role = "AWACS"
                        elif roles.intersection({"ActivateBeacon", "ActivateICLS"}):
                            role = "mission navigation beacon"
                        else:
                            continue
                        record = {"id": unit.get("unitId"), "group_id": gid, "name": unit.get("name"), "type": typ,
                            "role": role, "coalition": coalition, **_position(unit, projection),
                            "position_label": "Mission planned position — not a live sensor track", "provenance": provenance}
                        overlay["objects"].append(record)
                        for task in tasks:
                            if task["id"] not in ("ActivateBeacon", "ActivateICLS"):
                                continue
                            params = task.get("params", {})
                            if params.get("unitId") not in (None, unit.get("unitId")) or (params.get("unitId") is None and unit is not units[0]):
                                continue
                            channel = params.get("channel")
                            if channel is not None and (not isinstance(channel, int) or not 1 <= channel <= (20 if task["id"] == "ActivateICLS" else 126)):
                                overlay["quarantine"].append({"kind": "beacon", "id": unit.get("unitId"), "reason": "Invalid mission beacon channel", "provenance": provenance})
                                continue
                            overlay["beacons"].append({"id": f"{unit.get('unitId')}:{task['id']}", "unit_id": unit.get("unitId"),
                                "name": unit.get("name"), "task": task["id"], "coalition": coalition,
                                "channel": channel, "mode": params.get("modeChannel"), "callsign": params.get("callsign"),
                                "frequency_hz": params.get("frequency"), **_position(unit, projection),
                                "position_label": "Mission task assignment / initial position — not confirmed active", "provenance": provenance})
    for aid, warehouse in warehouses.get("airports", {}).items():
        if not isinstance(warehouse, dict):
            continue
        coalition = str(warehouse.get("coalition", "")).lower()
        coalition = "neutrals" if coalition == "neutral" else coalition
        if coalition in ("blue", "red", "neutrals"):
            overlay["ownership"].append({"id": f"{terrain}:airfield:{aid}", "dcs_id": aid,
                "coalition": coalition, "provenance": {**provenance, "archive_member": "warehouses"}})
    overlay["ownership_status"] = "Mission warehouse assignments — not live capture state" if overlay["ownership"] else "Static airfield ownership Unknown"
    return overlay


def visible_overlay(overlay, state):
    """Only own-coalition navigation support; no enemy or world-object display."""
    ac, raw = state.get("aircraft") or {}, state.get("raw_latest") or {}
    coalition = raw.get("coalition_id", ac.get("coalition_id"))
    coalition = {1: "red", 2: "blue", 0: "neutrals"}.get(coalition, coalition)
    if coalition not in ("red", "blue", "neutrals"):
        # Collector's Enemies/Allies labels are not a stable red/blue identity.
        coalition = None
    matching_routes = [r for r in overlay["routes"] if any(u.get("name") == ac.get("unit_name") and
        u.get("aircraft") == ac.get("aircraft") for u in r["units"])]
    if len(matching_routes) == 1:
        coalition = matching_routes[0]["coalition"]
    route = matching_routes[0]["points"] if len(matching_routes) == 1 else []
    return {"terrain": overlay["terrain"], "mission_identity": overlay["mission_identity"], "weather": overlay["weather"],
        "route": [dict(p) for p in route], "route_status": "Matched ownship unit and aircraft" if route else "Ownship mission route Unknown",
        "objects": [o for o in overlay["objects"] if coalition and o["coalition"] == coalition],
        "beacons": [b for b in overlay["beacons"] if coalition and b["coalition"] == coalition],
        "ownership": [o for o in overlay["ownership"] if coalition and o.get("coalition") == coalition],
        "position_status": overlay["position_status"], "provenance": overlay["provenance"],
        "visibility_note": "Only own-coalition mission navigation objects; coalition Unknown hides all objects"}
