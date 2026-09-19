import copy
import json
from webapp.backend.export_data import ExportDataReader


def fixture():
    raw = {"written_epoch": 100, "stream": {"exporter_connected": True, "live": True, "data_age_seconds": 0, "packet_counts": {"telemetry": 10}},
           "aircraft": {"aircraft": "jet", "ias_kt": 200}, "cockpit_displays": {"display_session": "s"},
           "capabilities": {"verdicts": {}}, "export_capture": {"latest": {
               "telemetry": {"received_epoch": 100, "session": "s", "aircraft": "jet",
                             "packet": {"type": "telemetry", "future_field": {"value": 123}, "sensor": {"rwr_count": 2}}},
               "future": {"received_epoch": 100, "session": "s", "aircraft": "jet", "packet": {"type": "future", "new": True}}}}}
    health = {"aircraft": "jet", "telemetry_fresh": True, "model_advancing": True,
              "telemetry_age_s": 0, "dcs_running": True}
    return raw, health


def test_unknown_received_fields_visible_only_as_diagnostics(tmp_path):
    raw, health = fixture()
    result = ExportDataReader(tmp_path/"absent").snapshot(raw, health, now=100)
    groups = {g["id"]: g for g in result["groups"]}
    assert groups["packet:telemetry"]["data"]["future_field"] == {"value": 123}
    assert groups["packet:telemetry"]["status"] == "Unverified"
    assert groups["packet:future"]["data"]["new"] is True
    assert "contacts" not in result


def test_menu_changed_session_and_denied_exports_never_leak_values(tmp_path):
    raw, health = fixture()
    reader = ExportDataReader(tmp_path/"absent")
    raw["export_capture"]["latest"]["future"]["session"] = "old"
    raw["capabilities"]["verdicts"]["sensor"] = "DENIED"
    groups = {g["id"]: g for g in reader.snapshot(raw, health, now=100)["groups"]}
    assert "sensor" not in groups["packet:telemetry"]["data"]
    assert "packet:future" not in groups
    health["dcs_running"] = False
    assert [g["id"] for g in reader.snapshot(raw, health, now=100)["groups"]] == ["stream"]


def test_stale_exports_are_explicitly_last_exported(tmp_path):
    raw, health = fixture()
    health.update(telemetry_fresh=False, model_advancing=False)
    result = ExportDataReader(tmp_path/"absent").snapshot(raw, health, now=110)
    assert result["status"] == "Last exported"
    packet = next(g for g in result["groups"] if g["id"] == "packet:telemetry")
    assert packet["status"] == "Last exported" and packet["age_s"] == 10


def test_full_probe_requires_exact_current_probe_match(tmp_path):
    raw, health = fixture()
    probe = {"settled": True, "aircraft": "jet", "wallclock": "today", "probe_number": 1, "model_time": 50}
    raw["capabilities"]["probe"] = copy.deepcopy(probe)
    path = tmp_path/"probe.json"
    path.write_text(json.dumps({**probe, "results": [{"new_api": "value"}]}))
    reader = ExportDataReader(path)
    assert any(g["id"] == "probe_report" for g in reader.snapshot(raw, health, now=100)["groups"])
    raw["capabilities"]["probe"]["model_time"] = 1
    assert not any(g["id"] == "probe_report" for g in reader.snapshot(raw, health, now=100)["groups"])


def test_frozen_snapshot_cannot_reuse_old_health_freshness(tmp_path):
    raw, health = fixture()
    result = ExportDataReader(tmp_path/"absent").snapshot(raw, health, now=1000)
    assert result["status"] == "Last exported"
    ownship = next(g for g in result["groups"] if g["id"] == "aircraft")
    assert ownship["status"] == "Last exported" and ownship["age_s"] == 900


def test_health_denial_wins_over_raw_available(tmp_path):
    raw, health = fixture()
    raw["capabilities"]["verdicts"]["sensor"] = "AVAILABLE"
    health["export_capabilities"] = {"sensor": "DENIED"}
    result = ExportDataReader(tmp_path/"absent").snapshot(raw, health, now=100)
    packet = next(g for g in result["groups"] if g["id"] == "packet:telemetry")
    assert "sensor" not in packet["data"]


def test_capture_boundary_never_retags_legacy_raw_latest_as_new_session(tmp_path):
    raw, health = fixture()
    raw["raw_latest"] = {"type": "telemetry", "old_session_value": "do not retag"}
    raw["export_capture"]["latest"].clear()
    result = ExportDataReader(tmp_path/"absent").snapshot(raw, health, now=100)
    assert not any(g["id"] == "packet:telemetry" for g in result["groups"])
