import json
import pytest
import collector as collector_module
from collector import Collector
from webapp.tools.collector_checkpoint import restore_checkpoint


def checkpoint(tmp_path, **changes):
    data = {"schema": "dcs-companion/collector-checkpoint/1", "created_epoch": 100,
            "dcs_session": {"pid": 1, "created": 1, "exe": "fixture"},
            "state": {"written_epoch": 100, "stream": {"data_age_seconds": 0},
                      "raw_latest": {"type": "telemetry", "name": "FA-18C_hornet", "unit": "pilot", "t": 500},
                      "capabilities": {"probe": {"settled": True, "aircraft": "FA-18C_hornet"},
                                       "verdicts": {"sensor": "AVAILABLE", "cockpit": "AVAILABLE"}},
                      "cockpit_displays": {"raw": {"3": {"old": "MASK"}}},
                      "history": {"samples": [{"epoch": 50}, {"epoch": 100}]}}}
    data.update(changes)
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps(data))
    return path


def test_same_session_resume_preserves_probe_history_but_not_display_evidence(tmp_path):
    col = Collector(str(tmp_path / "state.json"))
    path = checkpoint(tmp_path)
    assert restore_checkpoint(col, path, now=101, validate=lambda session: session["pid"] == 1)
    assert col.capabilities["verdicts"]["sensor"] == "AVAILABLE"
    assert len(col.history) == 2
    assert col.latest_epoch == 100
    assert col.display_identity == ("FA-18C_hornet", "pilot")
    assert col.displays == {} and col.display_received_epoch == {} and col.display_identity_by_id == {}
    assert not col.connected and not col.counts
    assert not path.exists()


def test_old_checkpoint_is_consumed_without_restoring(tmp_path):
    col = Collector(str(tmp_path / "state.json"))
    path = checkpoint(tmp_path)
    assert not restore_checkpoint(col, path, now=111, validate=lambda _: True)
    assert col.latest is None and col.capabilities is None and not path.exists()


def test_changed_dcs_process_never_restores_probe(tmp_path):
    col = Collector(str(tmp_path / "state.json"))
    assert not restore_checkpoint(col, checkpoint(tmp_path), now=101, validate=lambda _: False)
    assert col.capabilities is None


def test_resumed_probe_invalidates_on_first_packet_from_other_mission(tmp_path):
    col = Collector(str(tmp_path / "state.json"))
    assert restore_checkpoint(col, checkpoint(tmp_path), now=101, validate=lambda _: True)
    col.on_packet({"type": "telemetry", "name": "FA-18C_hornet", "unit": "pilot", "t": 1})
    assert col.capabilities is None and col.displays == {}


def test_corrupt_checkpoint_does_not_break_collector_startup(tmp_path):
    col = Collector(str(tmp_path / "state.json"))
    path = tmp_path / "checkpoint.json"
    path.write_text("corrupt")
    assert not restore_checkpoint(col, path, now=101, validate=lambda _: True)
    assert not path.exists()


def paused_checkpoint(tmp_path, age=110):
    path = checkpoint(tmp_path, created_epoch=1000)
    data = json.loads(path.read_text())
    data["state"]["written_epoch"] = 1000
    data["state"]["stream"]["data_age_seconds"] = age
    data["state"]["capabilities"]["probe"]["received_epoch"] = 880
    path.write_text(json.dumps(data))
    return path


def test_paused_resume_keeps_original_staleness_and_probe_without_live_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(collector_module.time, "time", lambda: 1001)
    col = Collector(str(tmp_path / "state.json"))
    assert restore_checkpoint(col, paused_checkpoint(tmp_path), now=1001, validate=lambda _: True)
    state = col.build_state()
    assert col.latest_epoch == 890 and state["stream"]["data_age_seconds"] == 111
    assert state["stream"]["live"] is False and state["stream"]["exporter_connected"] is False
    assert state["capabilities"]["verdicts"]["sensor"] == "AVAILABLE"
    assert state["capabilities"]["probe"]["received_epoch"] == 880
    assert state["cockpit_displays"]["raw"] == {} and state["cockpit_displays"]["received_epoch_by_id"] == {}
    assert not col.capture.latest and not col.counts


@pytest.mark.parametrize("packet", [
    {"type": "telemetry", "name": "FA-18C_hornet", "unit": "pilot", "t": 1},
    {"type": "telemetry", "name": "F-22A", "unit": "pilot", "t": 501},
    {"type": "telemetry", "name": "FA-18C_hornet", "unit": "another", "t": 501},
])
def test_paused_resume_probe_is_invalidated_by_first_new_session_packet(tmp_path, monkeypatch, packet):
    monkeypatch.setattr(collector_module.time, "time", lambda: 1001)
    col = Collector(str(tmp_path / "state.json"))
    assert restore_checkpoint(col, paused_checkpoint(tmp_path), now=1001, validate=lambda _: True)
    col.on_packet(packet)
    assert col.capabilities is None
    assert not col.displays


def test_paused_resume_accepts_same_session_next_packet_without_refreshing_displays(tmp_path, monkeypatch):
    monkeypatch.setattr(collector_module.time, "time", lambda: 1001)
    col = Collector(str(tmp_path / "state.json"))
    assert restore_checkpoint(col, paused_checkpoint(tmp_path), now=1001, validate=lambda _: True)
    col.on_packet({"type": "telemetry", "name": "FA-18C_hornet", "unit": "pilot", "t": 500.125})
    assert col.capabilities["verdicts"]["sensor"] == "AVAILABLE"
    assert col.connected and not col.displays


@pytest.mark.parametrize("age", [86400, -1, True, float("nan"), float("inf")])
def test_paused_sample_still_has_finite_bounded_age(tmp_path, age):
    col = Collector(str(tmp_path / "state.json"))
    assert not restore_checkpoint(col, paused_checkpoint(tmp_path, age), now=1001, validate=lambda _: True)
    assert col.latest is None and col.capabilities is None


def test_paused_checkpoint_still_expires_in_ten_seconds_and_checks_process(tmp_path):
    for now, validate in [(1011, lambda _: True), (1001, lambda _: False)]:
        col = Collector(str(tmp_path / "state.json"))
        assert not restore_checkpoint(col, paused_checkpoint(tmp_path), now=now, validate=validate)
        assert col.latest is None and col.capabilities is None


def test_future_written_state_and_malformed_history_do_not_partially_restore(tmp_path):
    for mutate in [lambda data: data["state"].update(written_epoch=1100),
                   lambda data: data["state"].update(history=[])]:
        col = Collector(str(tmp_path / "state.json"))
        path = paused_checkpoint(tmp_path)
        data = json.loads(path.read_text())
        mutate(data)
        path.write_text(json.dumps(data))
        assert not restore_checkpoint(col, path, now=1001, validate=lambda _: True)
        assert col.latest is None and col.capabilities is None
