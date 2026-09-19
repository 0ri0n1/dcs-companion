import pytest

import collector


def setup(monkeypatch, tmp_path):
    clock = [100.0]
    monkeypatch.setattr(collector.time, "time", lambda: clock[0])
    col = collector.Collector(str(tmp_path / "state.json"))
    col.on_packet({"type": "hello"})
    col.on_packet({"type": "telemetry", "name": "FA-18C_hornet", "unit": "jet", "t": 50})
    col.on_packet({"type": "probe", "settled": True, "aircraft": "FA-18C_hornet", "verdicts": {"sensor": "AVAILABLE"}})
    col.on_packet({"type": "indications", "id": 3, "t": 50, "elements": {"MPD_FLIR_LaserStatus_label": "MASK"}})
    return col, clock


def test_per_display_age_is_not_refreshed_by_another_display(monkeypatch, tmp_path):
    col, clock = setup(monkeypatch, tmp_path)
    clock[0] = 105
    col.on_packet({"type": "indications", "id": 7, "t": 55, "elements": {"RWR_PrioritySetting": "N"}})
    displays = col.build_state()["cockpit_displays"]
    assert displays["age_seconds"] == 0
    assert displays["age_seconds_by_id"] == {"3": 5.0, "7": 0.0}
    assert displays["received_epoch_by_id"] == {"3": 100.0, "7": 105.0}
    identity = displays["identity_by_id"]["3"]
    assert identity == {"aircraft": "FA-18C_hornet", "unit_name": "jet", "model_time_s": 50, "display_session": displays["display_session"]}


@pytest.mark.parametrize("packet", [
    {"type": "hello"}, {"type": "bye"},
    {"type": "telemetry", "name": "F-22A", "unit": "jet", "t": 51},
    {"type": "telemetry", "name": "FA-18C_hornet", "unit": "new jet", "t": 51},
    {"type": "telemetry", "name": "FA-18C_hornet", "unit": "jet", "t": 1},
])
def test_lifecycle_aircraft_unit_and_model_reset_clear_display_and_probe(monkeypatch, tmp_path, packet):
    col, _ = setup(monkeypatch, tmp_path)
    previous_session = col.display_session
    col.on_packet(packet)
    state = col.build_state()
    displays = state["cockpit_displays"]
    assert displays["raw"] == {} and displays["received_epoch_by_id"] == {}
    assert displays["identity_by_id"] == {} and displays["age_seconds_by_id"] == {}
    assert displays["age_seconds"] is None and displays["ufc"] is None
    assert displays["display_session"] != previous_session
    assert state["capabilities"]["verdicts"] == {}


def test_first_telemetry_preserves_initial_probe_but_discards_unattributed_labels(monkeypatch, tmp_path):
    clock = [100.0]
    monkeypatch.setattr(collector.time, "time", lambda: clock[0])
    col = collector.Collector(str(tmp_path / "state.json"))
    col.on_packet({"type": "hello"})
    col.on_packet({"type": "probe", "settled": True, "aircraft": "FA-18C_hornet", "verdicts": {"sensor": "AVAILABLE"}})
    col.on_packet({"type": "indications", "id": 3, "t": 50, "elements": {"old": "label"}})
    assert col.display_identity_by_id["3"]["aircraft"] is None
    col.on_packet({"type": "telemetry", "name": "FA-18C_hornet", "unit": "jet", "t": 50})
    assert not col.displays
    assert col.capabilities["verdicts"]["sensor"] == "AVAILABLE"


def test_ordinary_telemetry_keeps_display_receive_time_and_blank_clears_labels(monkeypatch, tmp_path):
    col, clock = setup(monkeypatch, tmp_path)
    session = col.display_session
    clock[0] = 101
    col.on_packet({"type": "telemetry", "name": "FA-18C_hornet", "unit": "jet", "t": 51})
    assert col.display_session == session
    assert col.display_received_epoch["3"] == 100
    col.on_packet({"type": "indications", "id": 3, "t": 51, "elements": {}})
    assert col.displays["3"] == {} and col.display_received_epoch["3"] == 101


def test_new_slot_probe_preceding_telemetry_survives_validated_transition(monkeypatch, tmp_path):
    col, clock = setup(monkeypatch, tmp_path)
    clock[0] = 101
    col.on_packet({"type": "probe", "settled": True, "aircraft": "FA-18C_hornet",
                   "model_time": 1.0, "verdicts": {"sensor": "AVAILABLE"}})
    col.on_packet({"type": "indications", "id": 3, "t": 1.0, "elements": {"new": "label"}})
    clock[0] = 101.125
    col.on_packet({"type": "telemetry", "name": "FA-18C_hornet", "unit": "new jet", "t": 1.125})
    state = col.build_state()
    assert state["capabilities"]["verdicts"] == {"sensor": "AVAILABLE"}
    assert state["capabilities"]["probe"]["model_time"] == 1.0
    assert state["capabilities"]["probe"]["received_epoch"] == 101
    assert state["cockpit_displays"]["raw"] == {}  # wait for next new-session display packet


@pytest.mark.parametrize("bad_probe", [
    {"model_time": 50.0}, {"model_time": 2.0}, {"model_time": None},
    {"aircraft": "F-22A"}, {"settled": False},
])
def test_recent_but_unmatched_probe_does_not_cross_reset(monkeypatch, tmp_path, bad_probe):
    col, clock = setup(monkeypatch, tmp_path)
    clock[0] = 101
    probe = {"type": "probe", "settled": True, "aircraft": "FA-18C_hornet", "model_time": 1.0,
             "verdicts": {"sensor": "AVAILABLE"}}
    probe.update(bad_probe)
    col.on_packet(probe)
    col.on_packet({"type": "telemetry", "name": "FA-18C_hornet", "unit": "new jet", "t": 1.0})
    assert col.capabilities is None


def test_old_matching_probe_is_not_reused(monkeypatch, tmp_path):
    col, clock = setup(monkeypatch, tmp_path)
    clock[0] = 101
    col.on_packet({"type": "probe", "settled": True, "aircraft": "FA-18C_hornet", "model_time": 1.0})
    clock[0] = 103
    col.on_packet({"type": "telemetry", "name": "FA-18C_hornet", "unit": "new jet", "t": 1.0})
    assert col.capabilities is None


def test_delayed_prior_session_display_never_reaches_current_sensor_values(monkeypatch, tmp_path):
    from webapp.backend.sensors import build_sensor_snapshot
    col, clock = setup(monkeypatch, tmp_path)
    clock[0] = 101
    col.on_packet({"type": "telemetry", "name": "FA-18C_hornet", "unit": "new jet", "t": 1.0})
    col.on_packet({"type": "probe", "settled": True, "aircraft": "FA-18C_hornet", "model_time": 1.0,
                   "verdicts": {"sensor": "AVAILABLE", "cockpit": "AVAILABLE", "ownship": "AVAILABLE"}})
    col.on_packet({"type": "indications", "id": 3, "t": 50.0,
                   "elements": {"MPD_FLIR_LaserStatus_label": "MASK"}})
    health = {"aircraft": "FA-18C_hornet", "dcs_running": True, "telemetry_fresh": True,
              "displays_fresh": True, "model_advancing": True, "cockpit": True}
    result = build_sensor_snapshot(col.build_state(), health, now=101)
    assert all(not item["fields"] and not item["last_observed_fields"] for item in result["layers"])
