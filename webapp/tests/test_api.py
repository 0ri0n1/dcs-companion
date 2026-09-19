from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from webapp.backend.app import create_app
from webapp.backend.config import Settings


class FakeService:
    def __init__(self):
        self.queue = SimpleNamespace(enqueue=lambda *a: {"state": "Queued"}, cancel=lambda x: {"state": "Cancelled"})
    def snapshot(self, clients=0):
        return {"health": {"status": "Offline"}, "clients": clients}
    def tick(self):
        pass
    def binding_snapshot(self, aircraft=None):
        return {"aircraft": aircraft or "FA-18C_hornet", "actions": [], "status": "Current"}


def make_client(lan=False):
    settings = Settings(lan_enabled=lan, host="192.168.1.20" if lan else "127.0.0.1",
                        origins=("http://testserver",), pairing_token="x"*40)
    return TestClient(create_app(settings, FakeService()), client=("192.168.1.90" if lan else "127.0.0.1", 1234))


@pytest.mark.parametrize("path", ["/api/state", "/api/events", "/api/bindings", "/api/export/archive/0"])
def test_unauthenticated_cannot_read(path):
    with make_client(True) as client:
        assert client.get(path).status_code == 401


def test_lan_auth_origin_host_and_protected_command():
    with make_client(True) as client:
        headers = {"Origin": "http://testserver"}
        command = {"request_id": str(uuid4()), "action_id": "f22.nav.next", "confirmed": True}
        assert client.post("/api/commands", json=command, headers=headers).status_code == 401
        assert client.post("/api/session", json={"token": "wrong"}, headers=headers).status_code == 401
        assert client.post("/api/session", json={"token": "x"*40}, headers=headers).status_code == 200
        assert client.get("/api/state").status_code == 200
        assert client.post("/api/commands", json=command, headers=headers).status_code == 200
        assert client.post("/api/commands", json=command, headers={"Origin": "http://evil.example"}).status_code == 403
        assert client.post("/api/commands", json=command).status_code == 403
        assert client.get("/api/state", headers={"Host": "evil.example"}).status_code == 403
        assert client.get("/api/state").headers["cache-control"] == "no-store"


def test_loopback_pairing_cookie_and_no_arbitrary_payloads():
    with make_client() as client:
        headers = {"Origin": "http://testserver"}
        assert client.post("/api/session", json={"token": ""}, headers=headers).status_code == 200
        response = client.post("/api/commands", json={"request_id": str(uuid4()), "action_id": "f22.nav.next", "confirmed": True, "shell": "arbitrary"}, headers=headers)
        assert response.status_code == 422
        assert client.get("/api/state", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_lan_brute_pairing_rate_limit():
    with make_client(True) as client:
        for _ in range(10):
            assert client.post("/api/session", json={"token": "wrong"}, headers={"Origin": "http://testserver"}).status_code == 401
        assert client.post("/api/session", json={"token": "x"*40}, headers={"Origin": "http://testserver"}).status_code == 429


def test_authenticated_aircraft_catalogue_selection_and_invalid_aircraft():
    with make_client() as client:
        assert client.get("/api/bindings?aircraft=FA-18C_hornet").status_code == 401
        assert client.post("/api/session", json={"token": ""}, headers={"Origin": "http://testserver"}).status_code == 200
        assert client.get("/api/bindings").json()["aircraft"] == "FA-18C_hornet"
        assert client.get("/api/bindings?aircraft=F-22A").json()["aircraft"] == "F-22A"
        assert client.get("/api/bindings?aircraft=FA-18C_hornet").json()["aircraft"] == "FA-18C_hornet"
        assert client.get("/api/bindings?aircraft=../../anything").status_code == 422


def test_state_preserves_nested_json_without_reencoding_catalogue_on_event_loop(monkeypatch):
    payload = {"health": {"status": "Live"}, "bindings": {"actions": [
        {"name": "Left DDI \u2192 menu", "value": [None, True, 1, 1.25, {"label": "A\\B"}]}
    ]}}
    service = FakeService()
    service.snapshot = lambda _clients: payload
    # Returning a plain dict would invoke FastAPI's recursive object encoder.
    def unexpected(*args, **kwargs):
        raise AssertionError('Snapshot JSON must be encoded once in its worker')
    monkeypatch.setattr('fastapi.routing.jsonable_encoder', unexpected)
    settings = Settings(origins=("http://testserver",))
    with TestClient(create_app(settings, service), client=("127.0.0.1", 1234)) as client:
        assert client.post('/api/session', json={}, headers={'Origin': 'http://testserver'}).status_code == 200
        response = client.get('/api/state')
        assert response.status_code == 200
        assert response.json() == payload
        assert response.headers['content-type'] == 'application/json'
        assert response.headers['cache-control'] == 'no-store'


def test_archive_download_is_bounded_authenticated_and_complete_lines_only(tmp_path):
    settings = Settings(state_path=tmp_path / "state.json", origins=("http://testserver",))
    archive = tmp_path / "exports"
    archive.mkdir()
    (archive / "packets.jsonl").write_bytes(b'{"packet":{"type":"telemetry"}}\n{"partial":')
    with TestClient(create_app(settings, FakeService()), client=("127.0.0.1", 1234)) as client:
        assert client.get("/api/export/archive/0").status_code == 401
        client.post("/api/session", json={"token": ""}, headers={"Origin": "http://testserver"})
        response = client.get("/api/export/archive/0")
        assert response.status_code == 200 and response.content == b'{"packet":{"type":"telemetry"}}\n'
        assert response.headers["cache-control"] == "no-store"
        assert "attachment" in response.headers["content-disposition"]
        assert client.get("/api/export/archive/4").status_code == 404
        assert client.get("/api/export/archive/1").status_code == 404
