import json
import math
import time
from pathlib import Path

import pytest

from webapp.backend.lua_data import LuaDataError, LuaDataParser, parse_assignment, extract_table, array
from webapp.backend.navigation import (
    NavigationService, bearing_deg, distance_nm, eta_seconds, cross_track_nm,
    reciprocal_heading, wind_components, destination_point, tacan_channel,
    tacan_frequency, detect_terrain, ownship_navigation, approach_guidance,
    top_of_descent_nm, metres_to_feet, ms_to_knots, build_terrain_cache, local_to_geo,
)
from webapp.tools.import_terrain_navigation import import_dump


def live_state(lat=41.6, lon=41.6):
    return {"aircraft": {"latitude": lat, "longitude": lon, "ias_kt": 999, "model_time_s": 10},
             "stream": {"live": True, "data_age_seconds": 0}, "written_epoch": time.time()}


def test_community_data_uses_active_interpreter_package_without_importing_it(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from webapp.backend import navigation
    origin = tmp_path / "venv" / "Lib" / "site-packages" / "dcs" / "__init__.py"
    def find_spec(name):
        assert name == "dcs"
        return SimpleNamespace(origin=str(origin))
    monkeypatch.setattr(navigation.importlib.util, "find_spec", find_spec)
    monkeypatch.setattr(navigation.site, "getusersitepackages",
                        lambda: (_ for _ in ()).throw(AssertionError("Active package takes priority")))
    assert navigation.default_community_dir() == origin.parent / "terrain"


def test_community_data_absent_package_uses_user_site_fallback(tmp_path, monkeypatch):
    from webapp.backend import navigation
    monkeypatch.setattr(navigation.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(navigation.site, "getusersitepackages", lambda: str(tmp_path / "user-site"))
    assert navigation.default_community_dir() == tmp_path / "user-site" / "dcs" / "terrain"


def test_geometry_golden():
    assert bearing_deg(0, 0, 0, 1) == pytest.approx(90)
    assert bearing_deg(0, 0, 1, 0) == pytest.approx(0)
    assert distance_nm(0, 0, 0, 1) == pytest.approx(60.04054, abs=.0001)
    assert distance_nm(0, 179, 0, -179) == pytest.approx(120.08108, abs=.0001)
    assert eta_seconds(60, 120) == 1800
    assert eta_seconds(60, None) is None
    assert eta_seconds(60, 0) is None
    assert reciprocal_heading(355) == 175
    assert cross_track_nm(-1, 5, 0, 0, 0, 10) == pytest.approx(60.04054, abs=.001)
    assert cross_track_nm(1, 5, 0, 0, 0, 10) == pytest.approx(-60.04054, abs=.001)
    assert destination_point(0, 0, 90, distance_nm(0, 0, 0, 1)) == pytest.approx((0, 1))
    assert metres_to_feet(304.8) == pytest.approx(1000)
    assert ms_to_knots(1852 / 3600) == pytest.approx(1)


def test_wind_and_approach_golden():
    assert wind_components(90, 90, 20) == pytest.approx({"headwind_kt": 20, "crosswind_from_right_kt": 0})
    assert wind_components(90, 180, 20) == pytest.approx({"headwind_kt": 0, "crosswind_from_right_kt": 20})
    assert wind_components(90, 270, 20)["headwind_kt"] == -20
    assert approach_guidance(0, 0, 1000, {}, 90)["status"] == "Unknown"
    lat, lon = destination_point(0, 0, 270, 3)
    result = approach_guidance(lat, lon, 0, {"lat": 0, "lon": 0, "elevation_m": 0}, 90)
    assert result["along_nm"] == pytest.approx(3)
    assert result["cross_track_nm"] == pytest.approx(0, abs=1e-9)
    assert result["target_altitude_ft"] == pytest.approx(955.3072, abs=.001)
    assert top_of_descent_nm(10000, 10000) == 0
    assert top_of_descent_nm(10000, 0) == pytest.approx(31.40, abs=.02)


@pytest.mark.parametrize("mode", ["X", "Y"])
@pytest.mark.parametrize("channel", range(1, 127))
def test_exact_tacan_roundtrip(channel, mode):
    assert tacan_channel(tacan_frequency(channel, mode)) == (channel, mode)


def test_tacan_boundary_cases():
    assert tacan_channel(1005_000_000) == (44, "X")
    assert tacan_channel(1154_000_000) == (67, "X")
    assert tacan_channel(1025_000_000) == (64, "Y")
    assert tacan_channel(1150_000_000) == (63, "Y")
    for bad in [0, 111_000_000, 1214_000_000, 977_000_001]:
        with pytest.raises(ValueError):
            tacan_channel(bad)


def test_literal_parser_no_execution_and_bounds():
    parsed = parse_assignment('mission = { ["text"] = "os.execute(\\"evil\\")", ["a"]={ [2]=2,[10]=10,[1]=1}, ["x"]=-1.25e2 }')
    assert parsed["text"] == 'os.execute("evil")'
    assert array(parsed["a"]) == [1, 2, 10]
    assert parsed["x"] == -125
    assert parse_assignment('mission={text=[=[hello\nworld]=]}')["text"] == "hello\nworld"
    assert extract_table("dofile('never executes')\nbeacons = { {display_name=_('Test'),type=BEACON_TYPE_TACAN} }", "beacons")[1]["display_name"] == "Test"
    for text in ['mission = os.execute("bad")', 'mission={};os.execute("bad")',
                 'mission={x=1,x=2}', 'mission={x=function() end}', 'mission={x=1+2}', 'mission={x=1e999}']:
        with pytest.raises(LuaDataError):
            parse_assignment(text)
    with pytest.raises(LuaDataError):
        LuaDataParser("{" * 100 + "}" * 100).value()
    with pytest.raises(LuaDataError):
        LuaDataParser("{1,2,3}", max_tokens=3)


def test_terrain_detection_fails_closed():
    assert detect_terrain(live_state())["terrain"] == "Caucasus"
    assert detect_terrain(live_state(25, 55))["terrain"] == "PersianGulf"
    assert detect_terrain(live_state(13.5, 144.8))["terrain"] is None
    assert detect_terrain(live_state(60, 10))["terrain"] is None
    assert detect_terrain({**live_state(), "terrain": "Syria"})["terrain"] is None
    assert detect_terrain({**live_state(13.5, 144.8), "theatre": "MarianasWWII"})["terrain"] == "MarianasWWII"
    assert detect_terrain({"terrain": "Caucasus"}, {"terrain": "PersianGulf"})["terrain"] is None


def test_ground_speed_never_ias_and_true_track_not_grid_velocity():
    state = live_state(0, 0)
    assert ownship_navigation(state)["ground_speed_kt"] is None
    state["raw_latest"] = {"velocity": {"x": 100, "z": 0}}
    assert ownship_navigation(state)["ground_speed_kt"] == pytest.approx(194.38445)
    assert ownship_navigation(state)["track_true_deg"] is None
    state.pop("raw_latest")
    lon = 1 / 60.04054
    state["aircraft"]["model_time_s"] = 10
    state["history"] = {"samples": [{"lat": 0, "lon": 0, "model_t": 0, "epoch": 100},
                                      {"lat": 0, "lon": lon, "model_t": 10, "epoch": 10000}]}
    assert ownship_navigation(state)["ground_speed_kt"] == pytest.approx(360, abs=.01)
    assert ownship_navigation(state)["track_true_deg"] == pytest.approx(90)
    state["aircraft"]["model_time_s"] = 1
    assert ownship_navigation(state)["ground_speed_kt"] is None


def test_stopped_collector_not_live():
    state = live_state()
    state["written_epoch"] -= 100
    assert ownship_navigation(state)["fresh"] is False


def test_cold_collector_null_fields(tmp_path):
    state = {"aircraft": None, "raw_latest": None, "history": None, "mission": None,
             "stream": {"live": False, "data_age_seconds": None}, "written_epoch": time.time()}
    result = NavigationService(tmp_path).snapshot(state)
    assert result["terrain"] is None
    assert result["airfields"] == []
    assert result["ownship"]["lat"] is None
    assert result["ownship"]["fresh"] is False
    assert result["ownship"]["ground_speed_kt"] is None


@pytest.mark.local_integration
def test_installed_caches_coverage_and_provenance():
    service = NavigationService()
    assert set(service.caches) == {"Caucasus", "PersianGulf", "MarianaIslands", "MarianasWWII"}
    expected = {"Caucasus": (164, 21), "PersianGulf": (101, 27), "MarianaIslands": (19, 5), "MarianasWWII": (0, 11)}
    for terrain, (beacons, radio) in expected.items():
        cache = service.caches[terrain]
        coverage = cache["provenance"]["coverage"]
        assert coverage["beacon_source_records"] == beacons
        assert coverage["radio_source_records"] == radio
        for record in cache["airfields"] + cache["navaids"]:
            prov = record["provenance"]
            for key in ("dcs_version", "terrain", "original_source_path", "source_hash", "source_modified_utc", "extraction_date", "extraction_method", "units", "coordinate_reference", "confidence", "mission_overlay_replaced"):
                assert key in prov
        assert all(a["runways"] == [] and a["runway_geometry_status"] == "Unknown" for a in cache["airfields"])
    caucasus = service.caches["Caucasus"]
    assert caucasus["provenance"]["coverage"]["beacon_equipped_airfields"] == 19
    assert caucasus["provenance"]["coverage"]["standalone_navaids"] == 64
    batumi = next(a for a in caucasus["airfields"] if a["dcs_id"] == 22)
    assert any(f["frequency_hz"] == 131_000_000 and f["modulation"] == "AM" for f in batumi["frequencies"])
    loc = next(n for n in batumi["navaids"] if n["type"] == "ILS_LOCALIZER")
    assert loc["localizer_front_course_true_deg"] == pytest.approx(125.584869)
    assert loc["runway_centerline_true_deg"] is None
    assert batumi["measured_centerline"]["course_true_deg"] == 130.10
    assert abs(loc["localizer_front_course_true_deg"] - batumi["measured_centerline"]["course_true_deg"]) > 4.5
    assert all(a["lat"] is None for a in service.caches["MarianasWWII"]["airfields"])


def test_no_cross_theatre_results_and_offline_library(navigation_cache):
    service = NavigationService(navigation_cache)
    pg = service.snapshot(live_state(25, 55))
    assert pg["terrain"] == "PersianGulf"
    assert pg["airfields"] and all(a["id"].startswith("PersianGulf:") for a in pg["airfields"])
    unknown = service.snapshot(live_state(13.5, 144.8))
    assert unknown["airfields"] == [] and unknown["nearest"] == []
    assert service.library("Caucasus")["ownship"] is None


@pytest.mark.local_integration
def test_projection_independently_matches_shipped_beacons():
    service = NavigationService()
    for terrain in ("Caucasus", "PersianGulf", "MarianaIslands"):
        cache = service.caches[terrain]
        for navaid in cache["navaids"][:10]:
            position = navaid["local_position"]
            lat, lon = local_to_geo(position["x"], position["z"], cache["projection"])
            assert distance_nm(lat, lon, navaid["lat"], navaid["lon"]) * 1852 < 2


def test_importer_quarantines_geometry_and_keeps_localizer_separate(tmp_path, navigation_cache):
    source = NavigationService(navigation_cache).caches["Caucasus"]
    version = source["dcs_version"]
    target = tmp_path / version
    target.mkdir()
    (target / "Caucasus.json").write_text(json.dumps(source))
    (tmp_path / "manifest.json").write_text(json.dumps({"terrains": [{"path": f"{version}/Caucasus.json"}]}))
    lat2, lon2 = destination_point(41.61, 41.60, 130.1, 1)
    dump = {"schema": "dcs-terrain-api/1", "terrain": "Caucasus", "dcs_version": version,
        "airfields": [{"dcs_id": 22, "reference": {"lat": 41.61, "lon": 41.6}, "runways": [
            {"id": 1, "width_m": 40, "ends": [{"designator": "13", "threshold": {"lat": 41.61, "lon": 41.6}},
                {"designator": "31", "threshold": {"lat": lat2, "lon": lon2}}]},
            {"id": 2, "length_m": 50000, "ends": [{"threshold": {"lat": 0, "lon": 0}}, {"threshold": {"lat": 0, "lon": 1}}]}]}]}
    path = tmp_path / "dump.json"
    path.write_text(json.dumps(dump))
    result = import_dump(path, tmp_path)
    batumi = next(a for a in result["airfields"] if a["dcs_id"] == 22)
    assert len(batumi["runways"]) == 1
    assert batumi["runways"][0]["ends"][0]["heading_true_deg"] == pytest.approx(130.1)
    assert batumi["runways"][0]["ends"][0]["heading_mag_deg"] is None
    assert any(q["reason"] == "Impossible runway length" for q in result["quarantine"])
    assert result["navaids"] == source["navaids"]
    assert list(target.glob("*.bak-*"))


def test_source_parser_quarantines_invalid_data(tmp_path):
    (tmp_path / "autoupdate.cfg").write_text('{"version":"test"}')
    folder = tmp_path / "Mods/terrains/Caucasus"
    folder.mkdir(parents=True)
    (folder / "Radio.lua").write_text("radio={}")
    (folder / "Beacons.lua").write_text("beacons={{beaconId='bad',positionGeo={latitude=500,longitude=0},position={0,0,0}}}")
    for rel in ("Scripts/World/Radio/BeaconTypes.lua", "MissionEditor/modules/Mission/BeaconData.lua"):
        file = tmp_path / rel
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("-- fixture")
    cache = build_terrain_cache(tmp_path, "Caucasus")
    assert cache["navaids"] == []
    assert cache["quarantine"][0]["reason"] == "Malformed coordinates"
