"""Offline navigation records and generic geometry, independent of cockpit actions."""
from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import json
import math
import re
import site
import time
from datetime import datetime, timezone
from pathlib import Path

from .lua_data import array, extract_table

ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / "webapp" / "data" / "navigation"
SCHEMA = "dcs-navigation/1"
EARTH_NM = 6371008.8 / 1852
TERRAINS = ("Caucasus", "PersianGulf", "MarianaIslands", "MarianasWWII")
ALIASES = {re.sub(r"[^a-z0-9]", "", value.lower()): terrain for terrain, names in {
    "Caucasus": ["Caucasus"], "PersianGulf": ["PersianGulf", "Persian Gulf", "Strait of Hormuz"],
    "MarianaIslands": ["MarianaIslands", "Mariana Islands", "Marianas"],
    "MarianasWWII": ["MarianasWWII", "MarianaIslandsWWII", "Marianas WWII"],
}.items() for value in names}


def default_community_dir():
    """Locate optional pydcs data in this interpreter, without importing its code."""
    spec = importlib.util.find_spec('dcs')
    if spec and spec.origin:
        return Path(spec.origin).parent / 'terrain'
    return Path(site.getusersitepackages()) / 'dcs' / 'terrain'


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def valid_position(lat, lon):
    return finite(lat) and finite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180


def normalize_terrain(value):
    return ALIASES.get(re.sub(r"[^a-z0-9]", "", str(value).lower())) if value else None


def distance_nm(lat1, lon1, lat2, lon2):
    a, b = math.radians(lat1), math.radians(lat2)
    dlat, dlon = b - a, math.radians(lon2 - lon1)
    h = math.sin(dlat / 2)**2 + math.cos(a) * math.cos(b) * math.sin(dlon / 2)**2
    return EARTH_NM * 2 * math.asin(math.sqrt(min(1, max(0, h))))


def bearing_deg(lat1, lon1, lat2, lon2):
    a, b, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    return math.degrees(math.atan2(math.sin(dl) * math.cos(b),
        math.cos(a) * math.sin(b) - math.sin(a) * math.cos(b) * math.cos(dl))) % 360


def reciprocal_heading(degrees):
    return (degrees + 180) % 360


def destination_point(lat, lon, course_true_deg, distance):
    a, l, b, d = map(math.radians, (lat, lon, course_true_deg, 0))
    d = distance / EARTH_NM
    new_lat = math.asin(math.sin(a) * math.cos(d) + math.cos(a) * math.sin(d) * math.cos(b))
    new_lon = l + math.atan2(math.sin(b) * math.sin(d) * math.cos(a), math.cos(d) - math.sin(a) * math.sin(new_lat))
    return math.degrees(new_lat), (math.degrees(new_lon) + 540) % 360 - 180


def eta_seconds(distance, ground_speed_kt):
    return distance / ground_speed_kt * 3600 if finite(ground_speed_kt) and ground_speed_kt > 1 else None


def cross_track_nm(lat, lon, start_lat, start_lon, end_lat, end_lon):
    """Signed distance; positive is right of the start-to-end great circle."""
    d = distance_nm(start_lat, start_lon, lat, lon) / EARTH_NM
    angle = math.radians(bearing_deg(start_lat, start_lon, lat, lon) - bearing_deg(start_lat, start_lon, end_lat, end_lon))
    return math.asin(max(-1, min(1, math.sin(d) * math.sin(angle)))) * EARTH_NM


def wind_components(runway_heading_true_deg, wind_from_true_deg, speed_kt):
    angle = math.radians(wind_from_true_deg - runway_heading_true_deg)
    return {"headwind_kt": speed_kt * math.cos(angle), "crosswind_from_right_kt": speed_kt * math.sin(angle)}


def metres_to_feet(value):
    return value / 0.3048


def ms_to_knots(value):
    return value * 3600 / 1852


def approach_guidance(lat, lon, altitude_ft, threshold, course_true_deg, slope_deg=3.0):
    """Same along/cross convention as glidepath_monitor; only real thresholds."""
    if not valid_position(threshold.get("lat"), threshold.get("lon")) or not finite(threshold.get("elevation_m")):
        return {"status": "Unknown", "reason": "Verified threshold and elevation required"}
    distance = distance_nm(threshold["lat"], threshold["lon"], lat, lon)
    theta = math.radians(bearing_deg(threshold["lat"], threshold["lon"], lat, lon) - course_true_deg)
    along = -distance * math.cos(theta)
    cross = distance * math.sin(theta)
    target = metres_to_feet(threshold["elevation_m"]) + max(0, along) * metres_to_feet(1852) * math.tan(math.radians(slope_deg))
    return {"status": "advisory", "along_nm": along, "cross_track_nm": cross,
            "target_altitude_ft": target, "glidepath_error_ft": altitude_ft - target}


def top_of_descent_nm(altitude_ft, target_altitude_ft, slope_deg=3.0):
    if not 0 < slope_deg < 15:
        raise ValueError("Invalid descent angle")
    return max(0, altitude_ft - target_altitude_ft) * .3048 / (1852 * math.tan(math.radians(slope_deg)))


def final_approach_fix(threshold, course_true_deg, distance=5.0, slope_deg=3.0):
    if not valid_position(threshold.get("lat"), threshold.get("lon")) or not finite(threshold.get("elevation_m")):
        return {"status": "Unknown", "reason": "Verified runway threshold/elevation required"}
    lat, lon = destination_point(threshold["lat"], threshold["lon"], reciprocal_heading(course_true_deg), distance)
    altitude = metres_to_feet(threshold["elevation_m"] + distance * 1852 * math.tan(math.radians(slope_deg)))
    return {"status": "advisory", "lat": lat, "lon": lon, "altitude_ft": altitude, "distance_nm": distance,
            "course_true_deg": course_true_deg, "label": "Calculated fix — not a published instrument procedure"}


def runway_suitability(runway, aircraft_requirements=None):
    minimum = (aircraft_requirements or {}).get("minimum_runway_length_m")
    length = runway.get("length_m")
    if not finite(minimum) or not finite(length):
        return {"status": "Unknown", "reason": "Verified aircraft runway requirements and runway length required"}
    return {"status": "advisory", "length_suitable": length >= minimum,
            "note": "Length check only; weight, weather, surface and performance remain pilot responsibilities"}


def tacan_frequency(channel, mode):
    if not isinstance(channel, int) or not 1 <= channel <= 126 or mode not in ("X", "Y"):
        raise ValueError("Invalid TACAN channel/mode")
    # Exact installed BeaconTypes.lua getTACANFrequency receiver-frequency logic.
    base = (1088 if channel < 64 else 1025) if mode == "Y" else (962 if channel < 64 else 1151)
    return (base + channel - (1 if channel < 64 else 64)) * 1_000_000


def tacan_channel(frequency_hz):
    if not finite(frequency_hz) or frequency_hz % 1_000_000:
        raise ValueError("TACAN receiver frequency is not an integer MHz")
    mhz = frequency_hz / 1_000_000
    mode = "Y" if 1025 <= mhz <= 1150 else "X"
    channel = int(mhz - (961 if mhz <= (1087 if mode == "Y" else 1024) else 1087))
    if tacan_frequency(channel, mode) != frequency_hz:
        raise ValueError("Invalid TACAN receiver frequency")
    return channel, mode


def source_provenance(path, version, terrain, *, confidence="verified", method="constrained-literal-parser"):
    path = Path(path)
    return {"dcs_version": version, "terrain": terrain, "original_source_path": str(path.resolve()),
            "source_hash": hashlib.sha256(path.read_bytes()).hexdigest(),
            "source_modified_utc": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
            "extraction_date": datetime.now(timezone.utc).isoformat(), "extraction_method": method,
            "units": {"coordinates": "degrees", "local_position": "m", "elevation": "m", "frequency": "Hz", "heading": "degrees"},
            "coordinate_reference": "WGS84 geographic; DCS terrain-local x north/z east", "confidence": confidence,
            "mission_overlay_replaced": False}


def frequency_record(hz, purpose, provenance, modulation="Unknown", band=None):
    if not finite(hz) or not 100_000 <= hz <= 20_000_000_000:
        raise ValueError("Invalid frequency")
    return {"frequency_hz": hz, "value": hz / (1000 if hz < 1_000_000 else 1_000_000),
            "unit": "kHz" if hz < 1_000_000 else "MHz", "modulation": modulation,
            "purpose": purpose, "band": band or ("LF/MF" if hz < 3_000_000 else "VHF" if hz < 300_000_000 else "UHF"),
            "provenance": provenance}


def _community_points(directory, terrain, version):
    """Read pydcs Python AST as data, never import/execute its modules."""
    subdir = {"Caucasus": "caucasus", "PersianGulf": "persiangulf", "MarianaIslands": "marianaislands"}.get(terrain)
    if not directory or not subdir:
        return {}, None
    folder = Path(directory) / subdir
    source, projection = folder / "airports.py", folder / "projection.py"
    if not source.exists() or not projection.exists():
        return {}, None
    params = {}
    for node in ast.walk(ast.parse(projection.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "TransverseMercator":
            params = {k.arg: ast.literal_eval(k.value) for k in node.keywords}
    if not params:
        return {}, None
    pp = source_provenance(projection, version, terrain, confidence="community", method="Python AST literal data")
    projection_record = {"parameters": params, "provenance": pp}
    points = {}
    for cls in ast.parse(source.read_text(encoding="utf-8")).body:
        if not isinstance(cls, ast.ClassDef):
            continue
        attributes = {}
        for stmt in cls.body:
            if isinstance(stmt, ast.Assign) and isinstance(stmt.targets[0], ast.Name) and stmt.targets[0].id in ("id", "name"):
                attributes[stmt.targets[0].id] = ast.literal_eval(stmt.value)
        if "id" not in attributes:
            continue
        for node in ast.walk(cls):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "Point" and len(node.args) == 3:
                try:
                    x, z = ast.literal_eval(node.args[0]), ast.literal_eval(node.args[1])
                except ValueError:
                    continue
                lat, lon = local_to_geo(x, z, projection_record)
                points[attributes["id"]] = {**attributes, "lat": lat, "lon": lon, "local_position": {"x": x, "z": z},
                    "provenance": source_provenance(source, version, terrain, confidence="community", method="Python AST airport reference point; pyproj projection")}
                break
    return points, projection_record


def local_to_geo(x, z, projection):
    if not finite(x) or not finite(z) or not projection:
        return None, None
    try:
        from pyproj import CRS, Transformer
        p = projection["parameters"]
        crs = CRS.from_proj4(f"+proj=tmerc +lat_0=0 +lon_0={p['central_meridian']} +k={p['scale_factor']} +x_0={p['false_easting']} +y_0={p['false_northing']} +datum=WGS84 +units=m")
        lon, lat = Transformer.from_crs(crs, 4326, always_xy=True).transform(z, x)
        return (lat, lon) if valid_position(lat, lon) else (None, None)
    except (ImportError, KeyError, ValueError):
        return None, None


def build_terrain_cache(dcs_root, terrain, *, community_dir=None):
    dcs_root = Path(dcs_root)
    version = json.loads((dcs_root / "autoupdate.cfg").read_text(encoding="utf-8-sig"))["version"]
    folder = dcs_root / "Mods" / "terrains" / terrain
    sources = {p.name.lower(): p for p in folder.iterdir() if p.name.lower() in ("beacons.lua", "radio.lua")}
    bpath, rpath = sources["beacons.lua"], sources["radio.lua"]
    bp, rp = source_provenance(bpath, version, terrain), source_provenance(rpath, version, terrain)
    types_path = dcs_root / "Scripts" / "World" / "Radio" / "BeaconTypes.lua"
    tp = source_provenance(types_path, version, terrain, method="TACAN functions transcribed and exhaustively round-trip tested")
    marker_path = dcs_root / "MissionEditor" / "modules" / "Mission" / "BeaconData.lua"
    marker_prov = source_provenance(marker_path, version, terrain, confidence="derived",
        method="ME loadBeaconsMarkerT4 displays channel-only terrain TACAN as X; runtime not verified")
    beacons = array(extract_table(bpath.read_text(encoding="utf-8-sig"), "beacons"))
    radios = array(extract_table(rpath.read_text(encoding="utf-8-sig"), "radio"))
    airfields, navaids, quarantine, seen = {}, [], [], set()
    def field(identifier, name=None, provenance=None):
        aid = int(identifier)
        if aid not in airfields:
            airfields[aid] = {"id": f"{terrain}:airfield:{aid}", "dcs_id": aid, "name": name or f"Airfield {aid}",
                "lat": None, "lon": None, "local_position": None, "elevation_m": None,
                "position_confidence": "unknown", "runways": [], "runway_geometry_status": "Unknown",
                "frequencies": [], "navaids": [], "callsigns": [], "provenance": provenance or rp}
        elif name and airfields[aid]["name"].startswith("Airfield "):
            airfields[aid]["name"] = name
        return airfields[aid]
    for b in beacons:
        bid = b.get("beaconId")
        try:
            if not isinstance(bid, str) or bid in seen:
                raise ValueError("Missing/duplicate beacon identifier")
            seen.add(bid)
            geo, pos = b.get("positionGeo", {}), array(b.get("position"))
            lat, lon = geo.get("latitude"), geo.get("longitude")
            if not valid_position(lat, lon) or len(pos) != 3 or not all(finite(v) for v in pos):
                raise ValueError("Malformed coordinates")
            direction = b.get("direction")
            if direction is not None and (not finite(direction) or not -360 <= direction <= 360):
                raise ValueError("Impossible heading")
            if not -500 <= pos[1] <= 9000:
                raise ValueError("Impossible elevation")
            freq = frequency_record(b.get("frequency"), b.get("type"), bp) if b.get("frequency") is not None else None
            match = re.fullmatch(r"airfield(\d+)_\d+", bid)
            record = {"id": f"{terrain}:{bid}", "source_id": bid, "name": b.get("display_name", bid),
                "type": b.get("type", "Unknown").removeprefix("BEACON_TYPE_"), "callsign": b.get("callsign"),
                "lat": lat, "lon": lon, "local_position": {"x": pos[0], "y": pos[1], "z": pos[2]}, "elevation_m": pos[1],
                "frequency_hz": b.get("frequency"), "frequency": freq, "channel": b.get("channel"), "mode": None,
                "airfield_id": f"{terrain}:airfield:{match[1]}" if match else None,
                "direction_source_deg": direction, "localizer_front_course_true_deg": None,
                "runway_centerline_true_deg": None, "provenance": bp}
            if record["type"] == "TACAN":
                if record["frequency_hz"] is not None:
                    try:
                        channel, mode = tacan_channel(record["frequency_hz"])
                        if record["channel"] is not None and record["channel"] != channel:
                            raise ValueError("TACAN frequency/channel mismatch")
                        record.update(channel=channel, mode=mode, channel_provenance=tp)
                    except ValueError as exc:
                        quarantine.append({"kind": "frequency", "source_id": bid, "value": record["frequency_hz"], "reason": str(exc), "provenance": bp})
                        record.update(frequency=None, frequency_hz=None, frequency_status="Quarantined")
                if record["mode"] is None and isinstance(record["channel"], int) and 1 <= record["channel"] <= 126:
                    record.update(mode="X", channel_provenance=marker_prov, mode_confidence="derived")
                elif record["mode"] is None:
                    raise ValueError("Invalid TACAN channel")
            if record["type"] in ("ILS_LOCALIZER", "PRMG_LOCALIZER", "ICLS_LOCALIZER") and direction is not None:
                record["localizer_front_course_true_deg"] = (direction + 180) % 360
                record["course_provenance"] = {**bp, "confidence": "derived", "extraction_method": "reciprocal of signed DCS localizer emission direction; not pavement heading"}
            navaids.append(record)
            if match:
                field(match[1], record["name"], bp)["navaids"].append(record)
        except (ValueError, TypeError) as exc:
            quarantine.append({"kind": "beacon", "source_id": bid, "reason": str(exc), "provenance": bp})
    seen_radio = set()
    for radio in radios:
        rid = radio.get("radioId")
        try:
            match = re.fullmatch(r"airfield(\d+)_\d+", str(rid))
            if not match or rid in seen_radio:
                raise ValueError("Missing/non-airfield/duplicate radio identifier")
            seen_radio.add(rid)
            callsigns = []
            for block in array(radio.get("callsign")):
                for names in block.values():
                    callsigns.extend(v for v in array(names) if isinstance(v, str) and v not in callsigns)
            airfield = field(match[1])
            airfield["callsigns"] = callsigns
            if airfield["name"].startswith("Airfield ") and callsigns:
                airfield["name"] = callsigns[0]
            for band, values in radio.get("frequency", {}).items():
                values = array(values)
                try:
                    if len(values) != 2 or values[0] not in ("MODULATIONTYPE_AM", "MODULATIONTYPE_FM"):
                        raise ValueError("Invalid modulation/frequency pair")
                    airfield["frequencies"].append(frequency_record(values[1], "/".join(array(radio.get("role"))), rp, values[0].removeprefix("MODULATIONTYPE_"), band))
                except ValueError as exc:
                    quarantine.append({"kind": "frequency", "source_id": rid, "reason": str(exc), "provenance": rp})
        except (ValueError, TypeError) as exc:
            quarantine.append({"kind": "radio", "source_id": rid, "reason": str(exc), "provenance": rp})
    points, projection = _community_points(community_dir, terrain, version)
    for aid, point in points.items():
        af = field(aid, point["name"], point["provenance"])
        af.update(name=point["name"], lat=point["lat"], lon=point["lon"], local_position=point["local_position"],
                  position_confidence="community", position_provenance=point["provenance"])
    # The measured Batumi line is separate evidence; it supplies no thresholds/length.
    overrides = ROOT / "data" / "runway-overrides.json"
    if terrain == "Caucasus" and overrides.exists():
        for name, measured in json.loads(overrides.read_text(encoding="utf-8")).items():
            if name.startswith("_"):
                continue
            af = next((a for a in airfields.values() if a["name"] == name), None)
            if af:
                af["measured_centerline"] = {**measured, "provenance": source_provenance(overrides, version, terrain,
                    confidence="derived", method="existing empirical rollout least-squares fit; not a runway threshold")}
    afs = sorted(airfields.values(), key=lambda x: x["name"].lower())
    provenance = {"schema_version": SCHEMA, "dcs_version": version, "terrain": terrain, "sources": [bp, rp, tp, marker_prov],
        "coverage": {"beacon_source_records": len(beacons), "beacons": len(navaids), "standalone_navaids": sum(n["airfield_id"] is None for n in navaids),
            "beacon_equipped_airfields": sum(bool(a["navaids"]) for a in afs), "radio_source_records": len(radios),
            "airfields": len(afs), "airfield_positions": sum(a["lat"] is not None for a in afs),
            "atc_frequencies": sum(len(a["frequencies"]) for a in afs), "runways": 0, "quarantined": len(quarantine)}}
    if points:
        provenance["sources"].extend([next(iter(points.values()))["provenance"], projection["provenance"]])
    return {"schema": SCHEMA, "terrain": terrain, "dcs_version": version, "airfields": afs,
            "navaids": navaids, "quarantine": quarantine, "projection": projection, "provenance": provenance}


def detect_terrain(state, overlay=None):
    declarations = []
    for container in (state, state.get("raw_latest", {}), state.get("aircraft", {}), state.get("mission", {})):
        if isinstance(container, dict):
            declarations.extend(container[k] for k in ("terrain", "theatre", "theater") if container.get(k))
    if declarations:
        normalized = {normalize_terrain(t) for t in declarations}
        if None in normalized or len(normalized) != 1:
            return {"terrain": None, "status": "Unknown", "reason": "Unsupported or conflicting theatre identity", "source": "telemetry"}
        explicit = normalized.pop()
        if overlay and overlay.get("terrain") != explicit:
            return {"terrain": None, "status": "Unknown", "reason": "Mission overlay/telemetry theatre mismatch", "source": "identity conflict"}
        return {"terrain": explicit, "status": "verified", "source": "telemetry identity"}
    if overlay and normalize_terrain(overlay.get("terrain")):
        return {"terrain": normalize_terrain(overlay["terrain"]), "status": "verified", "source": "session-matched mission overlay"}
    ac = state.get("aircraft") or {}
    lat, lon = ac.get("latitude"), ac.get("longitude")
    if not valid_position(lat, lon):
        return {"terrain": None, "status": "Unknown", "reason": "No explicit theatre or valid ownship position", "source": None}
    # Conservative regions, intentionally do not guess which overlapping Marianas map.
    if 12 <= lat <= 22 and 140 <= lon <= 150:
        return {"terrain": None, "status": "Unknown", "reason": "Mariana Islands and Marianas WWII overlap; mission identity required", "source": "geographic ambiguity"}
    if 39 <= lat <= 48 and 27 <= lon <= 47:
        return {"terrain": "Caucasus", "status": "derived", "source": "ownship geographic region"}
    if 22 <= lat <= 31 and 47 <= lon <= 61:
        return {"terrain": "PersianGulf", "status": "derived", "source": "ownship geographic region"}
    return {"terrain": None, "status": "Unknown", "reason": "Position outside installed terrain detection regions", "source": None}


def ownship_navigation(state):
    ac, raw = state.get("aircraft") or {}, state.get("raw_latest") or {}
    result = {"lat": ac.get("latitude"), "lon": ac.get("longitude"), "heading_true_deg": ac.get("heading_deg"),
        "heading_mag_deg": ac.get("magnetic_yaw_deg"), "altitude_ft": ac.get("altitude_msl_ft"),
        "vertical_speed_fpm": ac.get("vertical_speed_fpm"), "local_position": raw.get("position"),
        "ground_speed_kt": None, "track_true_deg": None, "ground_speed_source": "Unknown"}
    # Export velocity components are DCS GRID axes: magnitude is valid, track needs
    # true-axis data or geographic position history (never silently call grid true).
    velocity = raw.get("velocity") or raw.get("vel") or {}
    if isinstance(velocity, dict) and finite(velocity.get("x")) and finite(velocity.get("z")):
        result.update(ground_speed_kt=ms_to_knots(math.hypot(velocity["x"], velocity["z"])), ground_speed_source="raw horizontal velocity")
    history = state.get("history", {})
    samples = history.get("samples", []) if isinstance(history, dict) else history
    samples = [s for s in (samples or []) if isinstance(s, dict) and valid_position(s.get("lat"), s.get("lon")) and finite(s.get("model_t"))]
    if len(samples) >= 2:
        end = samples[-1]
        start = next((s for s in reversed(samples[:-1]) if 3 <= end["model_t"] - s["model_t"] <= 10), None)
        # Reject old-session history and backwards model time.
        now_model = ac.get("model_time_s", raw.get("t"))
        if start and finite(now_model) and 0 <= now_model - end["model_t"] <= 2:
            speed = distance_nm(start["lat"], start["lon"], end["lat"], end["lon"]) * 3600 / (end["model_t"] - start["model_t"])
            if speed <= 2000:
                if speed > 1:
                    result["track_true_deg"] = bearing_deg(start["lat"], start["lon"], end["lat"], end["lon"])
                if result["ground_speed_kt"] is None:
                    result.update(ground_speed_kt=speed, ground_speed_source="geographic position history / model time")
    stream = state.get("stream") or {}
    age = stream.get("data_age_seconds")
    written = state.get("written_epoch")
    if finite(age) and finite(written):
        age += max(0, time.time() - written)
    result["telemetry_age_seconds"] = age
    result["fresh"] = bool(stream.get("live")) and finite(age) and age <= 3
    result["status"] = "Live" if result["fresh"] else "Stale telemetry" if valid_position(result["lat"], result["lon"]) else "Offline"
    return result


class NavigationService:
    def __init__(self, cache_dir=None, *, tolerant=False):
        """Load published caches; tolerant startup defers corrupt-file repair.

        Strict is the default for import/build verification. Dashboard startup
        can retain good entries and report failed entries to maintenance instead
        of failing before the repair controller has been created.
        """
        self.cache_dir = Path(cache_dir or CACHE_DIR)
        self.caches = {}
        self.load_errors = []
        manifest = self.cache_dir / "manifest.json"
        if manifest.exists():
            try:
                entries = json.loads(manifest.read_text(encoding="utf-8")).get("terrains", [])
                if not isinstance(entries, list):
                    raise ValueError("Navigation manifest terrain entries must be a list")
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                if not tolerant:
                    raise
                self.load_errors.append(f"{manifest}: {exc}")
                entries = []
            for entry in entries:
                path = manifest
                try:
                    path = (self.cache_dir / entry["path"]).resolve()
                    if self.cache_dir.resolve() not in path.parents:
                        if tolerant:
                            self.load_errors.append(f"{manifest}: cache path is outside navigation storage")
                        continue
                    cache = json.loads(path.read_text(encoding="utf-8"))
                    if cache.get("schema") == SCHEMA:
                        if tolerant and (not isinstance(cache.get("airfields"), list)
                                or not isinstance(cache.get("navaids"), list)
                                or not isinstance(cache.get("provenance", {}).get("coverage"), dict)):
                            raise ValueError("Malformed navigation cache shape")
                        self.caches[cache["terrain"]] = cache
                    elif tolerant:
                        self.load_errors.append(f"{path}: unsupported navigation cache schema")
                except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                    if not tolerant:
                        raise
                    self.load_errors.append(f"{path}: {exc}")

    def library(self, terrain):
        canonical = normalize_terrain(terrain)
        if canonical not in self.caches:
            raise ValueError("Unsupported or uncached terrain")
        cache = self.caches[canonical]
        return {"terrain": canonical, "status": "Offline library — selected terrain", "ownship": None,
            "airfields": cache["airfields"], "navaids": cache["navaids"], "nearest": [], "route": [],
            "provenance": cache["provenance"], "quarantine_count": len(cache["quarantine"])}

    def snapshot(self, state, mission=None, selected_id=None):
        detection = detect_terrain(state, mission)
        terrain = detection["terrain"]
        ownship = ownship_navigation(state)
        cache = self.caches.get(terrain, {})
        airfields = copy.deepcopy(cache.get("airfields", []))
        nearest = []
        if valid_position(ownship["lat"], ownship["lon"]):
            for field in airfields:
                if not valid_position(field.get("lat"), field.get("lon")):
                    continue
                d = distance_nm(ownship["lat"], ownship["lon"], field["lat"], field["lon"])
                nearest.append({**field, "distance_nm": d,
                    "bearing_true_deg": bearing_deg(ownship["lat"], ownship["lon"], field["lat"], field["lon"]),
                    "eta_seconds": eta_seconds(d, ownship["ground_speed_kt"]) if ownship["fresh"] else None,
                    "calculation_status": "Live" if ownship["fresh"] else "Last-known position — stale"})
        nearest.sort(key=lambda f: f["distance_nm"])
        selected = next((a for a in nearest + airfields if a["id"] == selected_id), None)
        overlay = None
        route = []
        if mission and mission.get("terrain") == terrain:
            from .mission import visible_overlay
            overlay = visible_overlay(mission, state)
            route = overlay.get("route", [])
            for index, point in enumerate(route):
                if valid_position(point.get("lat"), point.get("lon")) and valid_position(ownship["lat"], ownship["lon"]):
                    d = distance_nm(ownship["lat"], ownship["lon"], point["lat"], point["lon"])
                    point.update(distance_nm=d, bearing_true_deg=bearing_deg(ownship["lat"], ownship["lon"], point["lat"], point["lon"]),
                        eta_seconds=eta_seconds(d, ownship["ground_speed_kt"]) if ownship["fresh"] else None)
                    previous = route[index-1] if index else None
                    if previous and valid_position(previous.get("lat"), previous.get("lon")):
                        point["desired_course_true_deg"] = bearing_deg(previous["lat"], previous["lon"], point["lat"], point["lon"])
                        point["cross_track_nm"] = cross_track_nm(ownship["lat"], ownship["lon"], previous["lat"], previous["lon"], point["lat"], point["lon"])
                if point.get("id") == selected_id:
                    selected = point
        return {"terrain": terrain, "status": detection["status"], "detection": detection, "ownship": ownship,
            "airfields": airfields, "navaids": cache.get("navaids", []), "nearest": nearest[:12], "selected": selected,
            "route": route, "overlay": overlay, "provenance": cache.get("provenance", {}),
            "coverage": [c["provenance"]["coverage"] | {"terrain": t} for t, c in self.caches.items()],
            "runway_advisory": self.runway_advisory(selected, overlay), "quarantine_count": len(cache.get("quarantine", []))}

    @staticmethod
    def runway_advisory(selected, overlay):
        result = {"status": "Unknown", "label": "Advisory only — not ATC clearance or confirmed active runway", "suggestion": None}
        if not selected or not overlay:
            result["reason"] = "Select a runway and load session-matched mission weather"
            return result
        wind = (overlay.get("weather") or {}).get("surface_wind") or {}
        if not finite(wind.get("from_true_deg")) or not finite(wind.get("speed_kt")):
            result["reason"] = "Mission wind direction reference is unverified"
            return result
        candidates = []
        for runway in selected.get("runways", []):
            for end in runway.get("ends", []):
                if finite(end.get("heading_true_deg")):
                    candidates.append({"runway": end.get("designator"), **wind_components(end["heading_true_deg"], wind["from_true_deg"], wind["speed_kt"])})
        if candidates:
            result.update(status="advisory", suggestion=max(candidates, key=lambda c: c["headwind_kt"]))
        else:
            result["reason"] = "Runway geometry Unknown; Terrain API dump required"
        return result
