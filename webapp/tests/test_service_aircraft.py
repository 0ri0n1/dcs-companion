import copy
from pathlib import Path
from types import SimpleNamespace
import pytest
from webapp.backend.config import Settings
from webapp.backend.service import DashboardService


class AircraftBindings:
    def __init__(self, data_dir, aircraft="F-22A", **setup_options):
        self.aircraft = aircraft
        self.data_dir = Path(data_dir)
        self.install_root = self.data_dir / "install"
        self.allowlist_path = self.data_dir / (aircraft + ".json")
    def check_current(self):
        return {"valid": True, "status": "Current", "current_fingerprint": self.aircraft}
    def snapshot(self, aircraft=None):
        aircraft = aircraft or self.aircraft
        if aircraft != self.aircraft:
            return {"aircraft": aircraft, "status": "Unsupported", "actions": []}
        return {"aircraft": aircraft, "status": "Current", "source_fingerprint": aircraft,
                "current_fingerprint": aircraft, "actions": [{"name": aircraft + " controls"}],
                "allowlist_path": str(self.allowlist_path)}
    def get_safe_actions(self, aircraft=None):
        return [{"id": "f22.nav.next", "aircraft": "F-22A"}] if aircraft == self.aircraft == "F-22A" else []


@pytest.fixture
def hornet(tmp_path, monkeypatch):
    monkeypatch.setattr("webapp.backend.bindings.BindingService", AircraftBindings)
    service = DashboardService(Settings(data_dir=tmp_path / "data", runtime_dir=tmp_path / "runtime"))
    state = {"raw": {}, "aircraft": {"aircraft": "FA-18C_hornet"},
             "health": {"aircraft": "FA-18C_hornet", "telemetry_fresh": True, "displays_fresh": True,
                        "dcs_running": True, "status": "Live", "notes": []},
             "context": {"aircraft": "FA-18C_hornet"}}
    service.reader = SimpleNamespace(read=lambda: copy.deepcopy(state))
    yield service, state
    service.close()


def test_active_hornet_loads_own_catalogue_and_never_f22_guide_actions(hornet):
    service, _ = hornet
    assert service.binding_snapshot()["aircraft"] == "FA-18C_hornet"
    assert service.context()["bindings_hash"] == "FA-18C_hornet"
    assert service.safe_actions() == []
    snapshot = service.snapshot()
    assert snapshot["bindings"]["aircraft"] == "FA-18C_hornet"
    assert snapshot["guide"]["actions"] == []
    assert snapshot["binding_catalogs"][1]["active"]


def test_manual_f22_browse_does_not_change_flying_hornet_context(hornet):
    service, _ = hornet
    assert service.binding_snapshot("F-22A")["aircraft"] == "F-22A"
    assert service.context()["aircraft"] == "FA-18C_hornet"
    assert service.safe_actions() == []


def test_stale_hornet_keeps_own_static_controls_and_unknown_aircraft_inherits_none(hornet):
    service, state = hornet
    state["health"]["telemetry_fresh"] = False
    assert service.snapshot()["bindings"]["aircraft"] == "FA-18C_hornet"
    state["aircraft"]["aircraft"] = state["context"]["aircraft"] = "F-unknown"
    assert service.binding_snapshot()["status"] == "Unsupported"
    assert service.safe_actions() == []


def test_automatic_maintenance_has_separate_catalogue_jobs(hornet):
    service, _ = hornet
    assert service.smart.binding_jobs["bindings"].aircraft == "F-22A"
    assert service.smart.binding_jobs["bindings:FA-18C_hornet"].aircraft == "FA-18C_hornet"


def test_unreadable_inactive_catalogue_does_not_break_active_aircraft(hornet):
    service, _ = hornet
    def unreadable():
        raise OSError("file temporarily unavailable")
    service.binding_services["F-22A"].check_current = unreadable
    snapshot = service.snapshot()
    assert snapshot["bindings"]["aircraft"] == "FA-18C_hornet"
    assert snapshot["binding_catalogs"][0]["status"] == "Unavailable"


def test_unreadable_active_catalogue_disables_commands_and_returns_reason(hornet):
    service, _ = hornet
    def unreadable(*args):
        raise OSError("file temporarily unavailable")
    service.binding_services["FA-18C_hornet"].snapshot = unreadable
    assert service.binding_snapshot()["status"] == "Unavailable"
    assert service.context()["bindings_valid"] is False


def test_last_aircraft_is_only_a_static_display_preference(hornet):
    service, state = hornet
    service.snapshot()
    assert service.last_aircraft == "FA-18C_hornet"
    assert (service.settings.runtime_dir / "last-aircraft.json").is_file()
    state["aircraft"].clear()
    state["context"]["aircraft"] = None
    state["health"].update(telemetry_fresh=False, dcs_running=False)
    assert service.snapshot()["bindings"]["aircraft"] == "FA-18C_hornet"
    assert service.safe_actions() == []


def test_awareness_is_separate_from_onboard_sensors_and_command_context(hornet):
    service, state = hornet
    calls = []
    expected = {"mode": "mission-awareness", "status": "Available", "single_player": True,
                "contacts": [{"id": "enemy-fixture", "lat": 26, "lon": 55}]}
    service.awareness_reader.close()
    service.awareness_reader = SimpleNamespace(snapshot=lambda health, raw, mission: calls.append(
        (copy.deepcopy(health), copy.deepcopy(raw), copy.deepcopy(mission))) or copy.deepcopy(expected), close=lambda:None)
    context_before = service.context()
    result = service.snapshot()
    assert result["awareness"] == expected and len(calls) == 1
    assert result["sensors"]["contacts"] == []
    assert result["guide"]["actions"] == []
    assert service.context() == context_before
