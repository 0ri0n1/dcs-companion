from copy import deepcopy
from pathlib import Path
import json
import pytest
from webapp.backend.telemetry import TelemetryReader


def fixture():
    return {"written_epoch": 100, "stream": {"live": True, "data_age_seconds": .1, "exporter_connected": True,
            "collector_uptime_seconds": 10, "packet_counts": {"hello": 1, "bye": 0}},
            "aircraft": {"aircraft": "F-22A", "latitude": 42, "longitude": 42, "model_time_s": 50},
            "cockpit_displays": {"age_seconds": 50}}


def test_frozen_file_grows_stale_and_display_age_is_independent():
    clock = [100.0]
    reader = TelemetryReader(Path("unused"), clock=lambda: clock[0])
    process = {"running": True, "identity": "p1", "focused": True}
    result = reader.normalize(fixture(), process)
    assert result["health"]["telemetry_fresh"]
    assert not result["health"]["displays_fresh"]
    assert not result["context"]["model_advancing"]
    clock[0] = 104
    result = reader.normalize(fixture(), process)
    assert not result["health"]["telemetry_fresh"]
    assert result["health"]["telemetry_age_s"] == 4.1


def test_model_time_advancing_and_reset_changes_session():
    clock = [100.0]
    reader = TelemetryReader(Path("unused"), clock=lambda: clock[0])
    p = {"running": True, "identity": "p1", "focused": True}
    first = fixture()
    reader.normalize(first, p)
    first["written_epoch"] = 101
    first["stream"]["collector_uptime_seconds"] = 11
    first["aircraft"]["model_time_s"] = 51
    clock[0] = 101
    live = reader.normalize(first, p)
    assert live["context"]["model_advancing"]
    first["written_epoch"] = 102
    first["stream"]["collector_uptime_seconds"] = 12
    first["aircraft"]["model_time_s"] = 1
    clock[0] = 102
    reset = reader.normalize(first, p)
    assert reset["context"]["session"] != live["context"]["session"]


def test_offline_menu_and_no_sensor_payload():
    reader = TelemetryReader(Path("unused"), clock=lambda: 100)
    state = fixture()
    state["aircraft"]["sensor"] = {"targets": ["unverified"]}
    result = reader.normalize(state, {"running": False, "identity": None})
    assert result["health"]["status"] == "Offline"
    assert "sensor" not in result["aircraft"]
    state["stream"]["exporter_connected"] = False
    assert reader.normalize(state, {"running": True, "identity": "p"})["health"]["status"] == "DCS menus/no aircraft"


def test_bad_clock_and_coordinates_fail_closed():
    reader = TelemetryReader(Path("unused"), clock=lambda: 90)
    data = fixture()
    assert not reader.normalize(data, {"running": True})["health"]["telemetry_fresh"]
    reader.clock = lambda: 100
    data["aircraft"]["latitude"] = float("nan")
    assert not reader.normalize(data, {"running": True})["context"]["telemetry_valid"]


def test_read_failure_is_diagnosed_without_reusing_previous_telemetry(tmp_path, monkeypatch):
    path = tmp_path / 'state.json'
    path.write_text(json.dumps(fixture()))
    process = {'running': True, 'identity': 'p1', 'focused': True, 'matches': 1}
    waits = []
    reader = TelemetryReader(path, clock=lambda: 100, processes=lambda: process, sleep=waits.append)
    good = reader.read()['context']
    original = Path.read_text
    def sharing_failure(current, *args, **kwargs):
        error = PermissionError('a secret path must never appear in diagnostics')
        error.winerror = 32
        raise error
    monkeypatch.setattr(Path, 'read_text', sharing_failure)
    failed = reader.read()
    assert failed['raw'] == {} and not failed['context']['telemetry_valid']
    assert failed['context']['session'] != good['session']
    evidence = failed['context']['telemetry_evidence']
    assert evidence['read_status'] == 'permission' and evidence['read_failures'] == 6
    assert evidence['read_attempts'] == 6 and waits == [.12] * 5
    assert evidence['last_read_error'] == {'status': 'permission', 'epoch': 100, 'winerror': 32}
    assert 'secret' not in json.dumps(failed)
    monkeypatch.setattr(Path, 'read_text', original)
    recovered = reader.read()['context']
    assert recovered['session'] == good['session']
    assert recovered['telemetry_evidence']['read_status'] == 'ok'
    assert recovered['telemetry_evidence']['last_read_error'] == evidence['last_read_error']


@pytest.mark.parametrize('error', [PermissionError('private'), OSError('private'),
                                  json.JSONDecodeError('private', '', 0)])
def test_transient_file_failure_reopens_current_data_and_keeps_session(tmp_path, monkeypatch, error):
    path = tmp_path / 'state.json'
    data = fixture()
    path.write_text(json.dumps(data))
    clock, waits = [100.], []
    process = {'running': True, 'identity': 'p1', 'focused': True, 'matches': 1}
    def wait(delay):
        waits.append(delay)
        clock[0] += delay
    reader = TelemetryReader(path, clock=lambda: clock[0], processes=lambda: process, sleep=wait)
    before = reader.read()['context']
    original, attempts = Path.read_text, []
    def interrupted_read(current, *args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            data['written_epoch'] = 100.12
            data['stream']['collector_uptime_seconds'] = 10.12
            data['aircraft']['model_time_s'] = 50.12
            path.write_text(json.dumps(data))
            raise error
        return original(current, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', interrupted_read)
    after = reader.read()['context']
    assert len(attempts) == 2 and waits == [.12]
    assert after['session'] == before['session'] and after['telemetry_valid']
    assert after['model_time'] == 50.12 and after['model_advancing']
    assert after['telemetry_evidence']['read_status'] == 'ok'
    assert after['telemetry_evidence']['read_attempts'] == 2
    assert after['telemetry_evidence']['read_failures'] == 1


def test_retry_does_not_renew_age_or_hide_process_change(tmp_path, monkeypatch):
    path = tmp_path / 'state.json'
    data = fixture()
    data['stream']['data_age_seconds'] = 2.95
    path.write_text(json.dumps(data))
    clock = [100.]
    process = {'running': True, 'identity': 'p1', 'focused': True}
    def wait(delay):
        clock[0] += delay
        process['identity'] = 'new-process'
    reader = TelemetryReader(path, clock=lambda: clock[0], processes=lambda: process, sleep=wait)
    before = reader.read()['context']
    original, attempts = Path.read_text, []
    def interrupted_read(current, *args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise PermissionError('sharing violation')
        return original(current, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', interrupted_read)
    after = reader.read()
    assert after['health']['telemetry_age_s'] == pytest.approx(3.07)
    assert not after['context']['telemetry_valid']
    assert after['context']['process'] == 'new-process'
    assert after['context']['session'] != before['session']
