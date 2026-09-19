"""Validate and merge a trusted Terrain API JSON export into a versioned cache."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from webapp.backend.navigation import (NavigationService, CACHE_DIR, normalize_terrain, finite,
    valid_position, source_provenance, bearing_deg, distance_nm)
from webapp.tools.build_navigation import backed_up_write


def import_dump(path, cache_dir=CACHE_DIR):
    path = Path(path)
    if path.stat().st_size > 16_000_000:
        raise ValueError("Terrain dump exceeds 16 MB")
    dump = json.loads(path.read_text(encoding="utf-8-sig"))
    terrain = normalize_terrain(dump.get("terrain"))
    if dump.get("schema") != "dcs-terrain-api/1" or terrain is None:
        raise ValueError("Unsupported terrain dump schema or terrain")
    service = NavigationService(cache_dir)
    cache = service.caches.get(terrain)
    if not cache or cache["dcs_version"] != dump.get("dcs_version"):
        raise ValueError("Terrain/DCS version does not match the static cache; rebuild first")
    provenance = source_provenance(path, cache["dcs_version"], terrain, method="Terrain API dump JSON import")
    provenance["runtime_exported_utc"] = dump.get("exported_utc")
    by_id = {a["dcs_id"]: a for a in cache["airfields"]}
    seen = set()
    for af in dump.get("airfields", []):
        aid = af.get("dcs_id")
        if not isinstance(aid, int) or aid in seen:
            cache["quarantine"].append({"kind": "airfield", "source_id": aid, "reason": "Duplicate/invalid identifier", "provenance": provenance})
            continue
        seen.add(aid)
        ref = af.get("reference", {})
        if not valid_position(ref.get("lat"), ref.get("lon")):
            cache["quarantine"].append({"kind": "airfield", "source_id": aid, "reason": "Malformed reference coordinates", "provenance": provenance})
            continue
        if ref.get("elevation_m") is not None and (not finite(ref["elevation_m"]) or not -500 <= ref["elevation_m"] <= 9000):
            cache["quarantine"].append({"kind": "airfield", "source_id": aid, "reason": "Impossible elevation", "provenance": provenance})
            continue
        target = by_id.setdefault(aid, {"id": f"{terrain}:airfield:{aid}", "dcs_id": aid, "name": af.get("name") or f"Airfield {aid}",
            "frequencies": [], "navaids": [], "callsigns": [], "provenance": provenance})
        target.update(lat=ref["lat"], lon=ref["lon"], position_confidence="verified", position_provenance=provenance,
            local_position={"x": ref.get("x"), "z": ref.get("z")}, elevation_m=ref.get("elevation_m"),
            abandoned=af.get("abandoned"), runways=[], runway_geometry_status="Unknown")
        seen_runways = set()
        for runway in af.get("runways", []):
            try:
                rid = str(runway.get("id"))
                if runway.get("id") is None or rid in seen_runways:
                    raise ValueError("Duplicate/invalid runway identifier")
                seen_runways.add(rid)
                if runway.get("course_grid_rad") is not None and (not finite(runway["course_grid_rad"]) or abs(runway["course_grid_rad"]) > 2 * 3.141592654):
                    raise ValueError("Impossible runway grid course")
                ends = runway.get("ends", [])
                if len(ends) != 2 or not all(valid_position(e.get("threshold", {}).get("lat"), e.get("threshold", {}).get("lon")) for e in ends):
                    raise ValueError("Both runway-end coordinates are required")
                a, b = [e["threshold"] for e in ends]
                measured_length = distance_nm(a["lat"], a["lon"], b["lat"], b["lon"]) * 1852
                length = runway.get("length_m")
                if length is None:
                    length = measured_length
                if not finite(length) or not 50 <= length <= 10_000 or not 50 <= measured_length <= 10_000:
                    raise ValueError("Impossible runway length")
                if abs(length - measured_length) > max(100, length * .1):
                    raise ValueError("Runway length disagrees with end coordinates")
                width = runway.get("width_m")
                if width is not None and (not finite(width) or not 2 <= width <= 300):
                    raise ValueError("Impossible runway width")
                for index, end in enumerate(ends):
                    this, other = ends[index]["threshold"], ends[1-index]["threshold"]
                    if end.get("heading_true_deg") is not None and (not finite(end["heading_true_deg"]) or not 0 <= end["heading_true_deg"] < 360):
                        raise ValueError("Impossible runway heading")
                    end.update(lat=this["lat"], lon=this["lon"], heading_true_deg=bearing_deg(this["lat"], this["lon"], other["lat"], other["lon"]),
                        heading_mag_deg=None, provenance=provenance,
                        heading_provenance={**provenance, "confidence": "derived", "extraction_method": "WGS84 bearing between Terrain API runway ends; magnetic variation Unknown"})
                target["runways"].append({"id": rid, "length_m": length, "width_m": width, "surface": runway.get("surface"),
                    "ends": ends, "course_grid_rad": runway.get("course_grid_rad"), "provenance": provenance})
            except (TypeError, ValueError) as exc:
                cache["quarantine"].append({"kind": "runway", "source_id": f"{aid}:{runway.get('id')}", "reason": str(exc), "provenance": provenance})
        if target["runways"]:
            target["runway_geometry_status"] = "Terrain API geometry"
    cache["airfields"] = sorted(by_id.values(), key=lambda a: a["name"])
    coverage = cache["provenance"]["coverage"]
    coverage.update(airfields=len(by_id), airfield_positions=sum(a.get("lat") is not None for a in by_id.values()),
                    runways=sum(len(a["runways"]) for a in by_id.values()), quarantined=len(cache["quarantine"]))
    cache["provenance"]["sources"].append(provenance)
    destination = Path(cache_dir) / cache["dcs_version"] / f"{terrain}.json"
    backed_up_write(destination, cache)
    return cache


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dump", type=Path)
    args = parser.parse_args()
    from webapp.backend.smart import generation_lock
    with generation_lock(ROOT / "webapp/runtime"):
        cache = import_dump(args.dump)
    print(json.dumps(cache["provenance"]["coverage"], indent=2))


if __name__ == "__main__":
    main()
