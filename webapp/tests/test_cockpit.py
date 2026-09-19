from copy import deepcopy
import json

import pytest

from webapp.backend.cockpit import build_cockpit_snapshot


def fixture(name="FA-18C_hornet"):
    verdicts = {"ownship": "AVAILABLE", "cockpit": "AVAILABLE", "sensor": "AVAILABLE"}
    raw = {"written_epoch": 100.0,
           "aircraft": {"aircraft": name, "unit_name": "jet", "model_time_s": 50.0},
           "stream": {"live": True, "exporter_connected": True, "data_age_seconds": 0.1},
           "capabilities": {"probe": {"aircraft": name, "settled": True}, "verdicts": verdicts},
           "cockpit_displays": {"age_seconds": 0.2, "display_session": "session-A",
               "received_epoch_by_id": {"3": 99.8}, "age_seconds_by_id": {"3": 0.2},
               "identity_by_id": {"3": {"aircraft": name, "unit_name": "jet",
                                       "display_session": "session-A", "model_time_s": 49.8}},
               "raw": {"3": {"MPD_FLIR_LaserStatus_label": "MASK", "arbitrary_page_label": "all text visible"}}}}
    health = {"aircraft": name, "dcs_running": True, "cockpit": True, "model_advancing": True,
              "telemetry_fresh": True, "displays_fresh": True, "export_capabilities": deepcopy(verdicts)}
    return raw, health


def display(result, display_id="3"):
    return next(item for item in result["displays"] if item["id"] == display_id)


def assert_hidden(result):
    assert all(not item["elements"] and not item["last_observed_elements"] for item in result["displays"])


def test_every_text_element_visible_with_independent_age_and_provenance():
    raw, health = fixture()
    raw["cockpit_displays"]["raw"]["3"].update({"": "unnamed", "unknown": "new page content", "empty": ""})
    result = build_cockpit_snapshot(raw, health, now=100.5)
    page = display(result)
    assert result["status"] == "Available" and result["text_only"] is True
    assert page["label"] == "Right DDI" and page["status"] == "Available"
    assert page["age_s"] == pytest.approx(0.7)
    assert "list_indication(3)" in page["source"]
    assert {item["name"]: item["value"] for item in page["elements"]} == raw["cockpit_displays"]["raw"]["3"]
    assert page["last_observed_elements"] == []


def test_known_display_labels_and_unknown_survey_slots():
    raw, health = fixture()
    raw["cockpit_displays"]["survey_ids"] = [0, 9, 12]
    result = build_cockpit_snapshot(raw, health, now=100)
    assert {item["id"]: item["label"] for item in result["displays"]} == {
        "0": "Display 0", "1": "HUD", "2": "Left DDI", "3": "Right DDI", "4": "AMPCD",
        "5": "IFEI", "6": "UFC", "7": "RWR", "9": "Display 9", "12": "Display 12"}
    assert display(result, "9")["status"] == "Unavailable"


def test_other_aircraft_gets_generic_text_without_hornet_semantics():
    raw, health = fixture(name="F-22A")
    page = display(build_cockpit_snapshot(raw, health, now=100))
    assert page["label"] == "Display 3" and page["status"] == "Available"
    assert page["elements"]  # transport provenance is generic, page meaning is not inherited


def test_fresh_other_display_does_not_make_old_ddi_current():
    raw, health = fixture()
    cockpit = raw["cockpit_displays"]
    cockpit["age_seconds"] = 0
    cockpit["received_epoch_by_id"]["3"] = 95
    cockpit["age_seconds_by_id"]["3"] = 5
    cockpit["raw"]["7"] = {"RWR_PrioritySetting": "N"}
    cockpit["received_epoch_by_id"]["7"] = 100
    cockpit["age_seconds_by_id"]["7"] = 0
    cockpit["identity_by_id"]["7"] = {**cockpit["identity_by_id"]["3"], "model_time_s": 50}
    result = build_cockpit_snapshot(raw, health, now=100)
    assert result["status"] == "Available" and display(result, "7")["elements"]
    old = display(result)
    assert old["status"] == "Stale" and old["elements"] == []
    assert old["last_observed_elements"] and old["age_s"] == 5


def test_paused_model_is_historical_only_even_with_fresh_wall_clock():
    raw, health = fixture()
    health["model_advancing"] = False
    page = display(build_cockpit_snapshot(raw, health, now=100))
    assert page["status"] == "Unavailable" and page["elements"] == []
    assert page["last_observed_elements"]


@pytest.mark.parametrize("source", ["raw", "health"])
@pytest.mark.parametrize("category", ["ownship", "cockpit", "sensor"])
def test_no_display_text_can_bypass_any_denied_export(source, category):
    raw, health = fixture()
    (raw["capabilities"]["verdicts"] if source == "raw" else health["export_capabilities"])[category] = "DENIED"
    result = build_cockpit_snapshot(raw, health, now=100)
    assert result["status"] == "Denied"
    assert_hidden(result)


@pytest.mark.parametrize("key,value", [("aircraft", "F-22A"), ("unit_name", "other"), ("display_session", "other"), ("model_time_s", 500)])
def test_session_identity_mismatch_never_appears_even_as_historical(key, value):
    raw, health = fixture()
    raw["cockpit_displays"]["identity_by_id"]["3"][key] = value
    result = build_cockpit_snapshot(raw, health, now=100)
    assert display(result)["status"] == "Unverified"
    assert_hidden(result)


def test_legacy_aggregate_clock_lacks_identity_proof_for_full_raw_text():
    raw, health = fixture()
    for key in ("display_session", "received_epoch_by_id", "age_seconds_by_id", "identity_by_id"):
        raw["cockpit_displays"].pop(key)
    result = build_cockpit_snapshot(raw, health, now=100)
    assert display(result)["status"] == "Unverified"
    assert_hidden(result)


@pytest.mark.parametrize("now", [90, 104, float("nan"), float("inf")])
def test_stale_ownship_or_bad_clock_withholds_everything(now):
    raw, health = fixture()
    result = build_cockpit_snapshot(raw, health, now=now)
    assert result["status"] == "Stale"
    assert_hidden(result)


@pytest.mark.parametrize("key", ["telemetry_fresh", "cockpit", "dcs_running"])
def test_unestablished_aircraft_hides_retained_text(key):
    raw, health = fixture()
    health[key] = False
    assert_hidden(build_cockpit_snapshot(raw, health, now=100))


@pytest.mark.parametrize("change", ["wrong_aircraft", "probe_aircraft", "unsettled", "unknown_capability", "disconnected"])
def test_inconsistent_context_fails_closed(change):
    raw, health = fixture()
    if change == "wrong_aircraft":
        health["aircraft"] = "F-22A"
    elif change == "probe_aircraft":
        raw["capabilities"]["probe"]["aircraft"] = "F-22A"
    elif change == "unsettled":
        raw["capabilities"]["probe"]["settled"] = False
    elif change == "unknown_capability":
        raw["capabilities"]["verdicts"]["sensor"] = "UNKNOWN"
    else:
        raw["stream"]["exporter_connected"] = False
    assert_hidden(build_cockpit_snapshot(raw, health, now=100))


@pytest.mark.parametrize("bad_epoch", [None, True, 101, -1, float("inf"), float("nan")])
def test_corrupt_individual_receive_epoch_is_not_shown_as_historical(bad_epoch):
    raw, health = fixture()
    raw["cockpit_displays"]["received_epoch_by_id"]["3"] = bad_epoch
    assert_hidden(build_cockpit_snapshot(raw, health, now=100))


def test_missing_inconsistent_individual_age_is_hidden():
    raw, health = fixture()
    raw["cockpit_displays"]["age_seconds_by_id"]["3"] = 50
    assert_hidden(build_cockpit_snapshot(raw, health, now=100))
    raw["cockpit_displays"].pop("age_seconds_by_id")
    assert_hidden(build_cockpit_snapshot(raw, health, now=100))


def test_blank_page_is_distinct_from_no_exported_page():
    raw, health = fixture()
    raw["cockpit_displays"]["raw"]["3"] = {}
    result = build_cockpit_snapshot(raw, health, now=100)
    assert display(result)["status"] == "Available" and not display(result)["elements"]
    assert display(result, "2")["status"] == "Unavailable"


def test_exact_text_preserved_without_truncation_or_interpretation():
    raw, health = fixture()
    text = "<b>raw display text</b>\n" + "X" * 3000
    raw["cockpit_displays"]["raw"]["3"] = {"_anonymous": text}
    page = display(build_cockpit_snapshot(raw, health, now=100))
    assert page["elements"] == [{"name": "_anonymous", "value": text}]


def test_no_raw_sensor_world_payload_or_nontext_objects_forwarded():
    raw, health = fixture()
    secret = "UNRELATED_WORLD_PAYLOAD"
    raw["world"] = [secret]
    raw["aircraft"]["sensor"] = {"targets": [secret]}
    raw["raw_latest"] = {"sensor": {"rwr_count": 99}, "world": [secret]}
    raw["cockpit_displays"]["raw"]["3"]["bad_nested"] = {"world": secret}
    result = build_cockpit_snapshot(raw, health, now=100)
    assert secret not in json.dumps(result) and "rwr_count" not in json.dumps(result)
    assert "Malformed" in " ".join(display(result)["notes"])


@pytest.mark.parametrize("raw,health", [(None, None), ([], []), ({"aircraft": []}, {}), ({"cockpit_displays": []}, {})])
def test_malformed_outer_state_fails_closed(raw, health):
    assert_hidden(build_cockpit_snapshot(raw, health, now=100))


def test_snapshot_input_is_not_mutated():
    raw, health = fixture()
    before = deepcopy((raw, health))
    build_cockpit_snapshot(raw, health, now=100)
    assert before == (raw, health)
