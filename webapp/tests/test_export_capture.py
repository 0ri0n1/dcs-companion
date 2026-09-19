import json
import pytest
import collector as collector_module
from webapp.tools.export_capture import ExportCapture
from collector import Collector


def test_unknown_fields_and_types_preserved_and_disk_rotates(tmp_path):
    capture = ExportCapture(tmp_path, file_bytes=700, files=3)
    for i in range(30):
        capture.record({"type": "future", "new": {"arbitrary": [i, "x"*20]}}, 100+i, "s", ("jet", "unit"))
    capture.close()
    paths = list(tmp_path.glob("*.jsonl"))
    assert len(paths) == 3 and sum(p.stat().st_size for p in paths) <= 2100
    last = [json.loads(row) for row in (tmp_path/"packets.jsonl").read_text().splitlines()][-1]
    assert last["packet"]["new"]["arbitrary"][0] == 29
    assert capture.snapshot()["latest"]["future"] == last
    assert capture.snapshot()["recording"]["received_packets"] == 30


def test_individual_displays_unknown_types_and_memory_bound():
    capture = ExportCapture(latest_slots=3)
    for i in range(5):
        capture.record({"type": "indications", "id": i, "elements": {"a": i}}, 100, "s", None)
    assert list(capture.latest) == ["indications:2", "indications:3", "indications:4"]
    assert capture.evicted == 2
    capture.clear_latest()
    assert not capture.latest and capture.received == 5


def test_disk_failure_does_not_interrupt_collection(tmp_path):
    invalid = tmp_path / "file"
    invalid.write_text("not a directory")
    capture = ExportCapture(invalid)
    capture.record({"type": "future", "value": "still collected"}, 100, "s", None)
    assert capture.latest["future"]["packet"]["value"] == "still collected"
    assert capture.snapshot()["recording"]["status"] == "Error"
    assert capture.write_failures == 1


def test_collector_session_change_clears_latest_but_archive_keeps_both(tmp_path):
    capture = ExportCapture(tmp_path / "archive")
    collector = Collector(str(tmp_path/"state.json"), capture=capture)
    collector.on_packet({"type": "telemetry", "name": "jet", "unit": "one", "t": 50})
    collector.on_packet({"type": "future", "test": "old"})
    collector.on_packet({"type": "telemetry", "name": "jet", "unit": "two", "t": 1})
    assert list(collector.build_state()["export_capture"]["latest"]) == ["telemetry"]
    capture.close()
    assert len((tmp_path/"archive/packets.jsonl").read_text().splitlines()) == 3


@pytest.mark.parametrize("packet", [
    {"type": "future", "value": float("nan")}, {"type": "future", "value": float("inf")},
    {"type": {"future": 1}}, {"type": []}, {"type": None}, ["not", "an object"],
])
def test_invalid_received_input_is_counted_without_poisoning_archive_or_collection(tmp_path, packet):
    capture = ExportCapture(tmp_path)
    col = Collector(str(tmp_path / "state.json"), capture=capture)
    assert col.on_packet(packet) is False
    assert not col.latest and not col.counts and not capture.latest
    assert col.on_packet({"type": "telemetry", "name": "jet", "unit": "one", "t": 1}) is True
    capture.close()
    metadata = capture.snapshot()["recording"]
    assert metadata["invalid_packets"] == 1 and metadata["received_packets"] == 2
    assert metadata["archived_packets"] == 1 and metadata["last_invalid_error"]
    assert len((tmp_path / "packets.jsonl").read_text().splitlines()) == 1
    assert col.latest["name"] == "jet"


def test_direct_capture_caller_also_rejects_nonfinite_packet(tmp_path):
    capture = ExportCapture(tmp_path)
    assert capture.record({"type": "future", "value": float("nan")}, 100, "s", None) is False
    assert capture.invalid_packets == 1 and not list(tmp_path.glob("*.jsonl"))
    assert capture.record({"type": "future", "value": "valid"}, 101, "s", None) is True
    capture.close()


@pytest.mark.parametrize("packet", [
    {"type": "hello"}, {"type": "bye"},
    {"type": "telemetry", "name": "FA-18C_hornet", "unit": "one", "t": 51, "fuel_internal": .4},
    {"type": "telemetry", "name": "F-22A", "unit": "two", "t": 51, "fuel_internal": .4},
    {"type": "telemetry", "name": "F-22A", "unit": "one", "t": 1, "fuel_internal": .4},
])
def test_real_session_boundary_clears_flight_history_and_derived_cross_aircraft_trend(tmp_path, monkeypatch, packet):
    clock = [100.0]
    monkeypatch.setattr(collector_module.time, "time", lambda: clock[0])
    col = Collector(str(tmp_path / "state.json"))
    col.on_packet({"type": "telemetry", "name": "F-22A", "unit": "one", "t": 50,
                   "fuel_internal": .9, "alt_msl": 1000})
    assert len(col.history) == 1
    clock[0] = 102
    col.on_packet(packet)
    assert len(col.history) == (1 if packet["type"] == "telemetry" else 0)
    assert col.derive().get("fuel_burn_lb_per_hr") is None
    assert col.derive().get("alt_change_ft_over_window") is None


def startup(col, clock, *, fresh=True, matching=True):
    col.on_packet({"type": "hello", "new_hello_field": "preserved"})
    col.on_packet({"type": "indication_survey", "ids": [0, 1, 3]})
    if not fresh:
        clock[0] += 2
    col.on_packet({"type": "probe", "settled": True,
                   "aircraft": "F-22A" if matching else "other", "model_time": .0,
                   "verdicts": {"sensor": "AVAILABLE"}})
    col.on_packet({"type": "indications", "id": 3, "t": 0, "elements": {"unattributed": "withheld"}})
    clock[0] += .125
    col.on_packet({"type": "telemetry", "name": "F-22A", "unit": "one", "t": .125})


def test_fresh_matching_startup_preserves_safe_diagnostics_without_duplicate_archive_receipts(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(collector_module.time, "time", lambda: clock[0])
    capture = ExportCapture(tmp_path)
    col = Collector(str(tmp_path / "state.json"), capture=capture)
    startup(col, clock)
    assert set(capture.latest) == {"hello", "probe", "indication_survey", "telemetry"}
    assert col.indication_ids == [0, 1, 3]
    assert col.displays == {}  # pre-identity pages remain untrusted
    assert all(item["session"] == col.display_session for item in capture.latest.values())
    hello = capture.latest["hello"]
    assert hello["packet"]["new_hello_field"] == "preserved" and hello["received_epoch"] == 100
    assert hello["received_session"] != hello["session"] and hello["association"]
    assert capture.received == capture.archived_packets == 5
    capture.close()
    lines = [json.loads(line) for line in (tmp_path / "packets.jsonl").read_text().splitlines()]
    assert len(lines) == 5 and lines[0]["session"] == hello["received_session"]


def test_old_startup_survey_and_hello_not_associated_with_new_identity(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(collector_module.time, "time", lambda: clock[0])
    col = Collector(str(tmp_path / "state.json"))
    startup(col, clock, fresh=False)
    assert set(col.capture.latest) == {"probe", "telemetry"}
    assert col.indication_ids == []


def test_unmatched_probe_cannot_attribute_startup_packets_to_new_aircraft(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(collector_module.time, "time", lambda: clock[0])
    col = Collector(str(tmp_path / "state.json"))
    startup(col, clock, matching=False)
    assert set(col.capture.latest) == {"telemetry"} and col.indication_ids == []


def test_archive_status_waits_until_a_successful_write(tmp_path):
    capture = ExportCapture(tmp_path)
    assert capture.snapshot()["recording"]["status"] == "Waiting for packets"
    assert not (tmp_path / "packets.jsonl").exists()
    capture.reject("invalid datagram")
    assert capture.snapshot()["recording"]["status"] == "Waiting for packets"
    capture.record({"type": "hello"}, 100, "s", None)
    assert capture.snapshot()["recording"]["status"] == "Recording"
    assert (tmp_path / "packets.jsonl").stat().st_size > 0
    capture.close()


@pytest.mark.parametrize("boundary", ["hello", "bye"])
def test_exporter_boundary_drops_recent_ownship_before_any_new_telemetry(tmp_path, monkeypatch, boundary):
    from webapp.backend.telemetry import TelemetryReader
    from webapp.backend.export_data import ExportDataReader
    clock = [100.0]
    monkeypatch.setattr(collector_module.time, "time", lambda: clock[0])
    col = Collector(str(tmp_path / "state.json"))
    reader = TelemetryReader(tmp_path / "unused", clock=lambda: clock[0])
    process = {"running": True, "identity": "one", "focused": True}
    pkt = {"type": "telemetry", "name": "F-22A", "unit": "one", "lat": 40, "lon": 40,
           "old_session_only": "must-not-cross-the-boundary", "t": 10}
    col.on_packet(pkt)
    reader.normalize(col.build_state(), process)
    clock[0] = 100.125
    col.on_packet({**pkt, "t": 11})
    before = reader.normalize(col.build_state(), process)
    assert before["health"]["telemetry_fresh"] and before["health"]["model_advancing"]
    clock[0] = 100.2
    col.on_packet({"type": boundary})
    state = col.build_state()
    after = reader.normalize(state, process)
    assert not after["health"]["telemetry_fresh"] and not after["health"]["cockpit"]
    assert after["health"]["aircraft"] is None and state["raw_latest"] is None
    assert col.latest is None and col.latest_epoch == 0.0
    assert not col.history and state["derived"] == {}
    diagnostic = ExportDataReader(tmp_path / "missing-probe").snapshot(state, after["health"], now=clock[0])
    assert "must-not-cross-the-boundary" not in json.dumps(diagnostic)


def test_idle_flush_makes_last_buffered_packet_downloadable_without_another_packet(tmp_path):
    capture = ExportCapture(tmp_path)
    capture.record({"type": "future", "value": 1}, 100, "s", None)
    capture.record({"type": "future", "value": 2}, 100.2, "s", None)
    path = tmp_path / "packets.jsonl"
    assert len(path.read_text().splitlines()) == 1
    capture.flush_due(now=100.5)
    assert len(path.read_text().splitlines()) == 1
    capture.flush_due(now=101.1)
    assert len(path.read_text().splitlines()) == 2
    capture.close()


def test_idle_disk_failure_is_visible_and_does_not_escape_collector_loop(tmp_path):
    class FailingHandle:
        def flush(self):
            raise OSError("test disk full")
        def close(self):
            pass
    capture = ExportCapture(tmp_path)
    capture.handle = FailingHandle()
    capture.flush_due(now=100)
    assert capture.handle is None and capture.write_failures == 1
    assert capture.snapshot()["recording"]["status"] == "Error"
    assert capture.error == "test disk full"
