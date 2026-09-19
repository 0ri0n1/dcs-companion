import copy
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from webapp.backend.app import create_app
from webapp.backend.config import Settings
from webapp.backend.setup import SetupManager


@pytest.fixture
def rig(tmp_path):
    roots = [tmp_path / name for name in ("install-a", "install-b", "profile-a", "profile-b")]
    for root in roots:
        root.mkdir()
        (root / "untouched.txt").write_text(root.name)
    installs = [{"id": f"i{i}", "name": p.name, "path": str(p), "valid": True} for i, p in enumerate(roots[:2])]
    profiles = [{"id": f"p{i}", "name": p.name, "path": str(p), "valid": True} for i, p in enumerate(roots[2:])]
    calls = []
    def discover(dcs_root=None, saved_games=None):
        calls.append((dcs_root, saved_games))
        install = next((r for r in installs if r["path"] == str(dcs_root)), installs[0] if dcs_root is None else None)
        profile = next((r for r in profiles if r["path"] == str(saved_games)), profiles[0] if saved_games is None else None)
        return {"selected": {"install_path": install["path"] if install else None,
            "profile_path": profile["path"] if profile else None,
            "install_id": install["id"] if install else None, "profile_id": profile["id"] if profile else None},
            "install_candidates": copy.deepcopy(installs), "profile_candidates": copy.deepcopy(profiles),
            "issues": [], "inventory": {"devices": [], "counts": {}}}
    settings = Settings(runtime_dir=tmp_path / "runtime", origins=("http://testserver",))
    manager = SetupManager(settings, discover=discover)
    return manager, settings, discover, calls, roots, installs, profiles


def test_selection_persists_only_companion_config_and_applies_on_next_start(rig):
    manager, settings, discover, calls, roots, *_ = rig
    before = {str(p): p.read_bytes() for root in roots for p in root.rglob("*") if p.is_file()}
    active = dict(manager.active)
    result = manager.select("i1", "p1")
    assert result["pending_restart"] and result["active"] == active
    assert result["selected"]["profile_path"] == str(roots[3])
    assert json.loads(manager.path.read_text())["schema"] == 1
    replacement = SetupManager(settings, discover=discover)
    assert replacement.active["profile_path"] == str(roots[3])
    assert not replacement.snapshot()["pending_restart"]
    assert before == {str(p): p.read_bytes() for root in roots for p in root.rglob("*") if p.is_file()}


def test_removed_or_invalid_candidates_cannot_be_saved(rig):
    manager, _, _, _, _, installs, profiles = rig
    profiles.pop()
    with pytest.raises(ValueError, match="no longer available"):
        manager.select("i1", "p1")
    installs[0]["valid"] = False
    with pytest.raises(ValueError):
        manager.select("i0", "p0")
    assert not manager.path.exists()


def test_corrupt_config_does_not_fall_back_to_another_profile(rig):
    manager, settings, discover, *_ = rig
    manager.path.parent.mkdir(parents=True, exist_ok=True)
    manager.path.write_text("{corrupt")
    replacement = SetupManager(settings, discover=discover)
    assert replacement.active == {"install_path": None, "profile_path": None}
    assert replacement.snapshot()["status"] == "Needs attention"
    assert replacement.snapshot()["issues"]
    result = replacement.select("i0", "p0")
    assert result["status"] == "Ready" and result["pending_restart"]


def test_unknown_explicit_path_stays_unavailable(rig):
    _, settings, discover, _, roots, *_ = rig
    settings.saved_games = roots[0] / "missing"
    manager = SetupManager(settings, discover=discover)
    assert manager.active["profile_path"] is None
    assert manager.snapshot()["selection_locked"]
    with pytest.raises(ValueError, match="launch options"):
        manager.select("i0", "p0")


def test_complete_explicit_pair_recovers_from_corrupt_saved_config(rig):
    original, settings, discover, _, roots, *_ = rig
    original.path.parent.mkdir(parents=True, exist_ok=True)
    original.path.write_text("{corrupt")
    settings.install_root, settings.saved_games = roots[1], roots[3]
    manager = SetupManager(settings, discover=discover)
    assert manager.snapshot()["status"] == "Ready"
    assert manager.active == {"install_path": str(roots[1]), "profile_path": str(roots[3])}
    assert manager.snapshot()["selection_locked"]


def test_discovery_cached_but_refresh_and_selection_revalidate(rig):
    manager, _, _, calls, *_ = rig
    initial = len(calls)
    result = manager.snapshot()
    result["install_candidates"].clear()
    assert manager.snapshot()["install_candidates"]
    assert len(calls) == initial
    manager.snapshot(refresh=True)
    assert len(calls) == initial + 1
    manager.select("i1", "p1")
    assert len(calls) == initial + 3


def test_setup_endpoints_pairing_fixed_ids_local_selection_and_no_commands(rig):
    manager, settings, *_ = rig
    commands = []
    closed_feeds = []
    service = SimpleNamespace(setup=manager, setup_snapshot=manager.snapshot,
        tick=lambda: None, queue=SimpleNamespace(enqueue=lambda *a: commands.append(a)),
        display_feed=SimpleNamespace(close=lambda: closed_feeds.append(True)))
    headers = {"Origin": "http://testserver"}
    with TestClient(create_app(settings, service), client=("127.0.0.1", 1234)) as client:
        assert client.get("/api/setup").status_code == 401
        client.post("/api/session", json={"token": ""}, headers=headers)
        response = client.get("/api/setup?refresh=true")
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert response.json()["assignment_policy"] == "read-only"
        assert client.post("/api/setup/selection", json={"install_id": "i1", "profile_id": "p1", "path": "anything"}, headers=headers).status_code == 422
        assert client.post("/api/setup/selection", json={"install_id": "i1", "profile_id": "missing"}, headers=headers).status_code == 409
        assert client.post("/api/setup/selection", json={"install_id": "i1", "profile_id": "p1"}, headers=headers).json()["pending_restart"]
        assert closed_feeds == [True]
    settings.lan_enabled = True
    settings.pairing_token = "test-pairing-token"
    with TestClient(create_app(settings, service), client=("192.168.1.10", 1234)) as client:
        client.post("/api/session", json={"token": settings.pairing_token}, headers=headers)
        assert client.get("/api/setup").status_code == 200
        assert client.post("/api/setup/selection", json={"install_id": "i0", "profile_id": "p0"}, headers=headers).status_code == 403
    assert not commands


def test_absent_setup_boots_and_disables_paths_and_refresh(tmp_path, monkeypatch):
    monkeypatch.setattr("webapp.backend.discovery.discover_dcs", lambda **kwargs: {
        "selected": {"install_path": None, "profile_path": None}, "issues": ["No installation"],
        "install_candidates": [], "profile_candidates": [], "inventory": {}})
    from webapp.backend.service import DashboardService
    settings = Settings(runtime_dir=tmp_path / "runtime", data_dir=tmp_path / "data", state_path=tmp_path / "state.json")
    service = DashboardService(settings)
    try:
        service.reader.processes = lambda: {"running": False, "identity": None, "focused": False}
        service.smart.tick()
        assert service.setup_snapshot()["status"] == "Needs attention"
        assert service.binding_snapshot("F-22A")["status"] == "Unavailable"
        assert not service.safe_actions()
        assert not service.smart.thread
        snapshot = service.snapshot()
        assert snapshot["guide"]["actions"] == []
        assert snapshot["smart"]["title"] == "Choose your DCS setup"
        assert str(service.mission_reader.hook_path).startswith(str(settings.runtime_dir))
    finally:
        service.close()


@pytest.mark.parametrize("change", [None, "profile", "install", "identity", "unknown"])
def test_commands_require_same_running_installation_profile_and_process(tmp_path, monkeypatch, change):
    from webapp.backend.service import DashboardService
    from webapp.backend.command_queue import CommandQueue
    roots = {"install_path": str(tmp_path / "install"), "profile_path": str(tmp_path / "profile"),
             "process_identity": "123:456", "ambiguous": False}
    actual = dict(roots)
    if change == "profile": actual["profile_path"] = str(tmp_path / "other-profile")
    if change == "install": actual["install_path"] = str(tmp_path / "other-install")
    if change == "identity": actual["process_identity"] = "123:457"
    if change == "unknown": actual.update(ambiguous=True, profile_path=None)
    monkeypatch.setattr("webapp.backend.discovery.identify_running_setup", lambda: actual)
    service = DashboardService.__new__(DashboardService)
    service.install_root, service.saved_games = roots["install_path"], roots["profile_path"]
    service.reader = SimpleNamespace(read=lambda: {"context": {"process": "123:456", "aircraft": "F-22A",
        "session": "session", "mission": "mission", "model_time": 30,
        "telemetry_valid": True, "model_advancing": True}})
    service.binding_services = {"F-22A": SimpleNamespace(aircraft="F-22A", check_current=lambda: {
        "valid": True, "source_fingerprint": "hash", "current_fingerprint": "hash", "status": "Current"})}
    context = service.context()
    action = {"risk": "benign", "name": "Next Waypoint", "combo": "N", "aircraft": "F-22A"}
    assert context["bindings_valid"] is (change is None)
    if change is None:
        assert CommandQueue.validation(action, context) is None
    else:
        assert "setup does not match" in CommandQueue.validation(action, context)
