from copy import deepcopy
import json

import pytest

from webapp.backend.sensors import build_sensor_snapshot


def fixture(legacy=False):
    raw = {"written_epoch": 100.0, "aircraft": {
        "aircraft": "FA-18C_hornet", "unit_name": "pilot-jet", "model_time_s": 50.0},
        "stream": {"live": True, "exporter_connected": True, "data_age_seconds": 0.1},
        "capabilities": {"probe": {"settled": True, "aircraft": "FA-18C_hornet"},
                         "verdicts": {"ownship": "AVAILABLE", "cockpit": "AVAILABLE", "sensor": "AVAILABLE"}},
        "cockpit_displays": {"age_seconds": 0.2, "raw": {"3": {
            "MPD_FLIR_LaserStatus_label": "MASK", "MPD_FLIR_LSTC_code_A": "1688",
            "MPD_FLIR_LTDC_code_A": "1688", "MPD_FLIR_TrackMode_A": "INR AUTO",
            "MPD_FLIR_TGT_Range_A": "11.4 TGT", "MPD_AFLIR_ZoomValue": "1.0"}}}}
    if not legacy:
        raw["cockpit_displays"].update({"received_epoch_by_id": {"3": 99.8},
            "age_seconds_by_id": {"3": 0.2}, "display_session": "session-A",
            "identity_by_id": {"3": {"aircraft": "FA-18C_hornet", "unit_name": "pilot-jet",
                "display_session": "session-A", "model_time_s": 49.8}}})
    health = {"aircraft": "FA-18C_hornet", "dcs_running": True, "cockpit": True,
              "model_advancing": True, "telemetry_fresh": True, "displays_fresh": True,
              "export_capabilities": deepcopy(raw["capabilities"]["verdicts"])}
    return raw, health


def layer(snapshot, layer_id):
    return next(item for item in snapshot["layers"] if item["id"] == layer_id)


def no_values(snapshot):
    assert all(not item["fields"] and not item["last_observed_fields"] for item in snapshot["layers"])
    assert snapshot["contacts"] == []


def test_measured_atflir_labels_have_exact_provenance_and_independent_age():
    raw, health = fixture()
    snapshot = build_sensor_snapshot(raw, health, now=100.5)
    assert snapshot["status"] == "Available"
    laser = layer(snapshot, "laser")
    field = laser["fields"][0]
    assert field["id"] == "laser.status_label" and field["value"] == "MASK"
    assert field["display_id"] == "3" and field["age_s"] == pytest.approx(0.7)
    assert field["element"] == "MPD_FLIR_LaserStatus_label"
    assert "laser_firing" not in json.dumps(snapshot)
    assert layer(snapshot, "radar")["status"] == "Unverified"
    assert layer(snapshot, "rwr")["fields"] == []


def test_legacy_aggregate_age_cannot_establish_current_display_values():
    raw, health = fixture(legacy=True)
    snapshot = build_sensor_snapshot(raw, health, now=100)
    laser = layer(snapshot, "laser")
    assert laser["status"] == "Unverified" and laser["fields"] == []
    assert laser["last_observed_fields"][0]["value"] == "MASK"
    assert laser["last_observed_fields"][0]["age_s"] is None


@pytest.mark.parametrize("legacy", [True, False])
@pytest.mark.parametrize("gate", ["sensor", "cockpit", "ownship"])
def test_export_denial_never_leaks_labels_even_when_cockpit_export_allowed(legacy, gate):
    raw, health = fixture(legacy)
    raw["capabilities"]["verdicts"][gate] = "DENIED"
    snapshot = build_sensor_snapshot(raw, health, now=100)
    assert snapshot["status"] == "Export denied"
    no_values(snapshot)


@pytest.mark.parametrize("field", ["model_advancing", "cockpit", "dcs_running", "telemetry_fresh", "displays_fresh"])
def test_paused_offline_or_stale_health_hides_all_values(field):
    raw, health = fixture(legacy=True)
    health[field] = False
    no_values(build_sensor_snapshot(raw, health, now=100))


def test_wall_clock_age_prevents_frozen_file_labels_even_if_health_claims_fresh():
    raw, health = fixture()
    assert build_sensor_snapshot(raw, health, now=104)["status"] == "Stale"
    no_values(build_sensor_snapshot(raw, health, now=104))
    no_values(build_sensor_snapshot(raw, health, now=90))


def test_fresh_other_display_does_not_refresh_old_flir_labels():
    raw, health = fixture()
    displays = raw["cockpit_displays"]
    displays["age_seconds"] = 0.0
    displays["received_epoch_by_id"]["3"] = 80
    displays["age_seconds_by_id"]["3"] = 20
    displays["received_epoch_by_id"]["7"] = 100
    displays["raw"]["7"] = {"RWR_PrioritySetting": "N"}
    snapshot = build_sensor_snapshot(raw, health, now=100)
    no_values(snapshot)
    assert layer(snapshot, "laser")["status"] == "Stale"


@pytest.mark.parametrize("change", ["aircraft", "unit_name", "display_session", "model_time_s"])
def test_display_identity_and_model_time_cannot_cross_sessions(change):
    raw, health = fixture()
    raw["cockpit_displays"]["identity_by_id"]["3"][change] = 10 if change == "model_time_s" else "other"
    no_values(build_sensor_snapshot(raw, health, now=100))


@pytest.mark.parametrize("epoch", [None, True, float("nan"), float("inf"), -1, 101])
def test_missing_partial_or_invalid_timestamp_has_no_legacy_fallback(epoch):
    raw, health = fixture()
    raw["cockpit_displays"]["received_epoch_by_id"]["3"] = epoch
    no_values(build_sensor_snapshot(raw, health, now=100))


@pytest.mark.parametrize("age", [None, True, float("nan"), -1, 20])
def test_missing_or_inconsistent_per_display_age_is_withheld(age):
    raw, health = fixture()
    raw["cockpit_displays"]["age_seconds_by_id"]["3"] = age
    no_values(build_sensor_snapshot(raw, health, now=100))


@pytest.mark.parametrize("name", ["F-22A", "F-16C_50", None])
def test_no_hornet_inheritance_for_other_aircraft(name):
    raw, health = fixture()
    raw["aircraft"]["aircraft"] = name
    health["aircraft"] = name
    snapshot = build_sensor_snapshot(raw, health, now=100)
    assert snapshot["status"] == "Unsupported"
    no_values(snapshot)


def test_capability_probe_must_be_settled_and_match_current_aircraft():
    raw, health = fixture()
    raw["capabilities"]["probe"]["aircraft"] = "F-22A"
    no_values(build_sensor_snapshot(raw, health, now=100))
    raw["capabilities"]["probe"] = {"settled": False, "aircraft": "FA-18C_hornet"}
    no_values(build_sensor_snapshot(raw, health, now=100))


def test_generic_sensor_and_world_data_are_never_exposed_or_classified():
    raw, health = fixture()
    secret = "SHOULD_NEVER_LEAVE_COLLECTOR"
    raw["aircraft"]["sensor"] = {"rwr_count": 71, "tws": [{"Type": secret}], "locked": [secret]}
    raw["raw_latest"] = {"sensor": {"tws": [secret]}, "world": [secret]}
    raw["world"] = {"enemies": [secret]}
    raw["cockpit_displays"]["raw"]["3"]["Unknown_enemy_position"] = secret
    snapshot = build_sensor_snapshot(raw, health, now=100)
    assert secret not in json.dumps(snapshot) and "71" not in json.dumps(snapshot)
    assert snapshot["contacts"] == [] and snapshot["world_data_excluded"] is True


def test_absent_or_bad_laser_label_does_not_mean_off_and_no_coordinates():
    raw, health = fixture()
    raw["cockpit_displays"]["raw"]["3"] = {"MPD_FLIR_LaserStatus_label": "EVIL\ntext", "MPD_FLIR_TGT_Latitude": "42.000"}
    snapshot = build_sensor_snapshot(raw, health, now=100)
    assert layer(snapshot, "laser")["status"] == "Unavailable"
    no_values(snapshot)
    assert "42.000" not in json.dumps(snapshot)


@pytest.mark.parametrize("raw,health", [(None, None), ([], []), ({"cockpit_displays": []}, {}), ({"aircraft": []}, {})])
def test_malformed_missing_snapshots_fail_closed(raw, health):
    no_values(build_sensor_snapshot(raw, health, now=100))


def test_does_not_mutate_input_snapshot():
    raw, health = fixture()
    before = deepcopy((raw, health))
    build_sensor_snapshot(raw, health, now=100)
    assert (raw, health) == before
