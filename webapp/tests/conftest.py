"""Portable test data; installed DCS/profile checks require an explicit opt-in."""
import json
import os

import pytest


def pytest_collection_modifyitems(items):
    if os.environ.get("DCS_LOCAL_INTEGRATION") == "1":
        return
    skip = pytest.mark.skip(reason="Installed DCS/cache/profile check: set DCS_LOCAL_INTEGRATION=1 to opt in")
    for item in items:
        if item.get_closest_marker("local_integration"):
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def block_native_keyboard_input(monkeypatch):
    """A test must inject its own emitter; the suite can never press real keys."""
    import input_sender

    def forbidden_emit(*args, **kwargs):
        raise AssertionError("Tests must never emit native keyboard input")

    monkeypatch.setattr(input_sender.KeySender, "_emit", forbidden_emit)


@pytest.fixture
def navigation_cache(tmp_path):
    """Small synthetic libraries exercise real loading, navigation, and imports."""
    directory = tmp_path / "data" / "navigation"
    version = "fixture"
    (directory / version).mkdir(parents=True)
    entries = []
    for terrain, lat, lon in [("Caucasus", 41.61, 41.6), ("PersianGulf", 25., 55.),
                              ("MarianaIslands", 14., 145.), ("MarianasWWII", 14., 145.)]:
        fields = [{"id": f"{terrain}:airfield:{index}", "dcs_id": index,
                   "name": f"Fixture {terrain} {index}", "lat": lat + (index - 22) * .1,
                   "lon": lon + .1, "elevation_m": 10, "runways": [], "navaids": [],
                   "frequencies": [], "callsigns": [], "runway_geometry_status": "Unknown",
                   "position_confidence": "fixture", "provenance": {}}
                  for index in range(22, 28)]
        navaids = [{"id": f"{terrain}:localizer:22", "type": "ILS_LOCALIZER",
                    "localizer_front_course_true_deg": 125.5,
                    "runway_centerline_true_deg": None}] if terrain == "Caucasus" else []
        cache = {"schema": "dcs-navigation/1", "terrain": terrain, "dcs_version": version,
                 "airfields": fields, "navaids": navaids, "quarantine": [],
                 "projection": {"parameters": {"central_meridian": 45, "scale_factor": 1,
                                                 "false_easting": 0, "false_northing": 0}},
                 "provenance": {"sources": [], "coverage": {}}}
        relative = f"{version}/{terrain}.json"
        (directory / relative).write_text(json.dumps(cache), encoding="utf-8")
        entries.append({"terrain": terrain, "dcs_version": version, "path": relative})
    (directory / "manifest.json").write_text(json.dumps({"terrains": entries}), encoding="utf-8")
    return directory
