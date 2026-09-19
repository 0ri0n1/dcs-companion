from types import SimpleNamespace
from urllib.parse import urlsplit
from fastapi.testclient import TestClient
from webapp.backend.app import create_app
from webapp.backend.config import Settings


def test_pc_issues_qr_phone_redeems_once_and_gets_session_cookie():
    origin = "http://192.168.1.20:18787"
    settings = Settings(host="192.168.1.20", lan_enabled=True, pairing_token="long-secret-not-in-qr",
                        origins=(origin,))
    service = SimpleNamespace(tick=lambda: None, snapshot=lambda clients: {"health": {"status": "Live"}})
    app = create_app(settings, service)
    headers = {"Origin": origin}
    with TestClient(app, base_url=origin, client=("127.0.0.1", 10)) as pc:
        assert pc.post("/api/pairing-ticket", json={}, headers=headers).status_code == 401
        pc.post("/api/session", json={"token": ""}, headers=headers).raise_for_status()
        response = pc.post("/api/pairing-ticket", json={}, headers=headers)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        ticket = urlsplit(response.json()["url"]).fragment.removeprefix("pair=")
        assert settings.pairing_token not in response.text
        assert response.json()["expires_in"] == 120
        with TestClient(app, base_url=origin, client=("192.168.1.99", 11)) as phone:
            assert phone.get("/api/state").status_code == 401
            assert phone.post("/api/session", json={"token": "x", "ticket": ticket}, headers=headers).status_code == 400
            paired = phone.post("/api/session", json={"ticket": ticket}, headers=headers)
            assert paired.status_code == 200
            cookie = paired.headers["set-cookie"].lower()
            assert "httponly" in cookie and "samesite=strict" in cookie
            assert phone.get("/api/state").status_code == 200
            assert phone.post("/api/session", json={"ticket": ticket}, headers=headers).status_code == 401
            assert phone.post("/api/pairing-ticket", json={}, headers=headers).status_code == 403
            report = phone.get("/api/diagnostics").text
            assert ticket not in report and settings.pairing_token not in report
