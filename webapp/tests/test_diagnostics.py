import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from webapp.backend.app import create_app
from webapp.backend.config import Settings
from webapp.backend.diagnostics import ConnectionDiagnostics
from webapp.backend.__main__ import local_listeners


def test_event_log_is_bounded_rotates_and_omits_supplied_secrets(tmp_path):
    diagnostics = ConnectionDiagnostics(tmp_path, max_bytes=1024)
    for _ in range(240):
        diagnostics.event("stream_opened", token="secret-token", detail="private-mission", path="private-path")
    diagnostics.event("unknown-event", token="not-allowed")
    assert len(diagnostics.snapshot()["events"]) == 200
    diagnostics.close()
    files = sorted(tmp_path.glob("connection-events.jsonl*"))
    assert len(files) == 3
    text = "".join(path.read_text() for path in files)
    assert "secret-token" not in text and "private-mission" not in text and "private-path" not in text
    assert all(json.loads(line)["kind"] == "stream_opened" for line in text.splitlines())


def test_health_transitions_distinguish_telemetry_stream_and_slow_updates():
    diagnostics = ConnectionDiagnostics()
    diagnostics.observe({"health": {"telemetry_fresh": True}, "awareness": {"status": "Available"}}, 100, 10, "stream")
    diagnostics.observe({"health": {"telemetry_fresh": True}, "awareness": {"status": "Available"}}, 100, 11, "stream")
    assert len(diagnostics.events) == 2
    diagnostics.observe({"health": {"telemetry_fresh": False}, "awareness": {"status": "Paused"}}, 1500, 12, "poll")
    assert [r["kind"] for r in diagnostics.events][-3:] == ["telemetry_stale", "awareness_unavailable", "snapshot_slow"]
    diagnostics.observe({"health": {"telemetry_fresh": True}, "awareness": {"status": "Available"}}, 500, 9, "stream")
    assert diagnostics.events[-1]["kind"] == "snapshot_slow"  # late old response ignored
    diagnostics.observe({"health": {"telemetry_fresh": True}, "awareness": {"status": "Available"}}, 500, 13, "stream")
    assert diagnostics.events[-1]["kind"] == "snapshot_recovered"
    assert not any(row["kind"] == "stream_error" for row in diagnostics.events)


def test_log_disk_error_is_reported_without_breaking_live_data(tmp_path):
    diagnostics = ConnectionDiagnostics(tmp_path)
    diagnostics.handler._open = Mock(side_effect=OSError("no space"))
    diagnostics.event("server_started")
    assert diagnostics.snapshot()["log_write_error"]
    assert diagnostics.snapshot()["events"][0]["kind"] == "server_started"
    diagnostics.close()


def test_diagnostics_route_requires_session_and_never_returns_packets():
    settings = Settings(origins=("http://testserver",))
    service = SimpleNamespace(tick=lambda: None, snapshot=lambda clients: {
        "health": {"telemetry_fresh": True}, "raw_packet": "never-in-diagnostics"})
    app = create_app(settings, service)
    with TestClient(app, client=("127.0.0.1", 1)) as client:
        assert client.get("/api/diagnostics").status_code == 401
        client.post("/api/session", json={"token": ""}, headers={"Origin": "http://testserver"})
        client.get("/api/state").raise_for_status()
        result = client.get("/api/diagnostics")
        assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
        assert "never-in-diagnostics" not in result.text
        assert result.json()["log_file"] == "connection-events.jsonl"
        assert result.json()["events"][0]["kind"] == "server_started"


def test_failed_snapshot_records_fixed_reason_without_exception_secret():
    def fail(clients):
        raise ValueError("secret credential from some future error")
    service = SimpleNamespace(tick=lambda: None, snapshot=fail)
    app = create_app(Settings(origins=("http://testserver",)), service)
    with TestClient(app, client=("127.0.0.1", 1), raise_server_exceptions=False) as client:
        client.post("/api/session", json={"token": ""}, headers={"Origin": "http://testserver"})
        assert client.get("/api/state").status_code == 500
        result = client.get("/api/diagnostics")
        assert "snapshot_failed" in result.text and "secret credential" not in result.text


def test_tablet_listener_binds_only_selected_interface_and_loopback(monkeypatch):
    sockets = []
    def make(*args):
        sock = Mock()
        sockets.append(sock)
        return sock
    monkeypatch.setattr("webapp.backend.__main__.socket.socket", make)
    result = local_listeners(Settings(host="192.168.1.20", lan_enabled=True))
    assert result == sockets and len(result) == 2
    sockets[0].bind.assert_called_once_with(("192.168.1.20", 18787))
    sockets[1].bind.assert_called_once_with(("127.0.0.1", 18787))


def test_listener_failure_closes_every_partially_bound_socket(monkeypatch):
    sockets = [Mock(), Mock()]
    sockets[1].bind.side_effect = OSError("already bound")
    monkeypatch.setattr("webapp.backend.__main__.socket.socket", Mock(side_effect=sockets))
    with pytest.raises(OSError):
        local_listeners(Settings(host="192.168.1.20", lan_enabled=True))
    assert all(sock.close.call_count == 1 for sock in sockets)


def test_wildcard_listener_is_refused():
    with pytest.raises(ValueError):
        local_listeners(Settings(host="0.0.0.0", lan_enabled=True))
