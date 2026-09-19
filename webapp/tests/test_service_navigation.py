import copy
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from webapp.backend.config import Settings
from webapp.backend.navigation import NavigationService, SCHEMA
from webapp.backend.service import DashboardService


class StubBindings:
    def __init__(self, data_dir, aircraft="F-22A", **setup_options):
        self.aircraft = aircraft
        self.data_dir = Path(data_dir)
        self.install_root = self.data_dir.parent / "fixture-dcs"
        self.saved_games = self.data_dir.parent / "fixture-saved"
        self.allowlist_path = self.data_dir / "fixture-allowlist.json"

    def snapshot(self, aircraft="F-22A"):
        return {"status": "Current", "source_fingerprint": "fixture", "current_fingerprint": "fixture",
                "allowlist_path": str(self.allowlist_path)}

    def check_current(self):
        return {"valid": True, "current_fingerprint": "fixture", "status": "Current"}

    def get_safe_actions(self, aircraft="F-22A"):
        return []


@pytest.fixture
def service_factory(tmp_path, monkeypatch):
    monkeypatch.setattr("webapp.backend.bindings.BindingService", StubBindings)
    monkeypatch.setattr("webapp.backend.adapters.get_adapter", lambda name, **setup_options: {"name": name})
    services = []
    settings = Settings(data_dir=tmp_path / "isolated-data", runtime_dir=tmp_path / "runtime",
                        state_path=tmp_path / "state.json")
    def create():
        service = DashboardService(settings)
        telemetry = {"raw": {}, "aircraft": {"aircraft": "F-22A"},
            "health": {"telemetry_fresh": True, "dcs_running": True, "displays_fresh": True,
                       "status": "Live", "notes": []}, "context": {"aircraft": "F-22A"}}
        # No DCS process enumeration or actual telemetry is accessed by tests.
        service.reader = SimpleNamespace(read=lambda: copy.deepcopy(telemetry))
        services.append(service)
        return service
    yield create, settings
    for service in services:
        service.close()


def cache_record(terrain):
    return {"schema": SCHEMA, "dcs_version": "fixture", "terrain": terrain,
        "airfields": [], "navaids": [], "quarantine": [], "provenance": {"coverage": {}}}


def test_strict_constructor_still_raises_tolerant_constructor_records_errors(tmp_path):
    (tmp_path / "manifest.json").write_text("malformed manifest")
    with pytest.raises(ValueError):
        NavigationService(tmp_path)
    tolerant = NavigationService(tmp_path, tolerant=True)
    assert tolerant.cache_dir == tmp_path
    assert tolerant.caches == {}
    assert tolerant.load_errors


def test_tolerant_constructor_keeps_good_entries_when_one_cache_is_bad(tmp_path):
    (tmp_path / "good.json").write_text(json.dumps(cache_record("Caucasus")))
    (tmp_path / "bad.json").write_text("malformed cache")
    (tmp_path / "manifest.json").write_text(json.dumps({"terrains": [{"path": "good.json"}, {"path": "bad.json"}]}))
    with pytest.raises(ValueError):
        NavigationService(tmp_path)
    tolerant = NavigationService(tmp_path, tolerant=True)
    assert set(tolerant.caches) == {"Caucasus"}
    assert len(tolerant.load_errors) == 1


@pytest.mark.parametrize("corruption", ["manifest", "cache", "missing-cache"])
def test_dashboard_starts_with_corrupt_custom_cache_and_stays_unavailable(service_factory, corruption):
    create, settings = service_factory
    nav_dir = settings.data_dir / "navigation"
    nav_dir.mkdir(parents=True)
    if corruption == "manifest":
        (nav_dir / "manifest.json").write_text("broken")
    else:
        (nav_dir / "manifest.json").write_text(json.dumps({"terrains": [{"path": "broken.json"}]}))
        if corruption == "cache":
            (nav_dir / "broken.json").write_text("broken")
    service = create()
    assert service.navigation.cache_dir == nav_dir
    assert service.navigation.caches == {}
    assert service.navigation.load_errors
    assert service.smart.navigation_valid is False
    assert service.snapshot()["navigation"]["status"] == "Updating navigation data"
    with pytest.raises(ValueError, match="updating"):
        service.navigation_library("Caucasus")


def test_corrupt_startup_cache_reaches_automatic_recovery(service_factory, monkeypatch):
    create, settings = service_factory
    nav_dir = settings.data_dir / "navigation"
    nav_dir.mkdir(parents=True)
    (nav_dir / "manifest.json").write_text("broken startup manifest")
    service = create()
    install = service.bindings.install_root
    install.mkdir()
    (install / "autoupdate.cfg").write_text('{"version":"9.9.9"}')
    for relative, text in {
        "Mods/terrains/Caucasus/Beacons.lua": "beacons={}",
        "Mods/terrains/Caucasus/Radio.lua": "radio={}",
        "Scripts/World/Radio/BeaconTypes.lua": "-- fixture",
        "MissionEditor/modules/Mission/BeaconData.lua": "-- fixture",
    }.items():
        path = install / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    monkeypatch.setattr("webapp.backend.navigation_maintenance.ROOT", settings.data_dir.parent)
    monkeypatch.setattr(service.smart.navigation, "_community_dir", lambda terrain: settings.data_dir / "absent-community")
    service.smart._refresh("navigation")
    assert service.smart.navigation_valid is True
    assert service.smart.navigation.check()["valid"] is True
    assert service.navigation_library("Caucasus")["terrain"] == "Caucasus"
    assert any(p.read_text() == "broken startup manifest" for p in nav_dir.glob("manifest.json.bak-*"))


def nav_payload():
    return {"terrain": "Caucasus", "airfields": [{"id": "old"}], "navaids": [{"id": "old"}],
        "nearest": [{"id": "old"}], "selected": {"id": "old"}, "route": [{"id": "old"}],
        "overlay": {"objects": [{"id": "old-carrier"}]}, "runway_advisory": {"suggestion": "old"},
        "provenance": {"confidence": "verified"}, "coverage": [{"terrain": "old"}], "ownship": None}


def test_invalid_generation_masks_overlay_advisory_and_provenance(service_factory):
    create, _ = service_factory
    service = create()
    service.navigation = SimpleNamespace(caches={"Caucasus": {}}, snapshot=lambda *a, **kw: nav_payload())
    service.smart.navigation_valid = False
    nav = service.snapshot()["navigation"]
    for key in ("airfields", "navaids", "nearest", "route"):
        assert nav[key] == []
    assert nav["selected"] is None and nav["overlay"] is None and nav["runway_advisory"] is None
    assert not nav["provenance"] and not nav["coverage"]


def test_old_inflight_snapshot_cannot_become_current_after_refresh(service_factory):
    create, _ = service_factory
    service = create()
    entered, release = threading.Event(), threading.Event()
    result, errors = [], []
    def slow_snapshot(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return nav_payload()
    service.navigation = SimpleNamespace(caches={"Caucasus": {}}, snapshot=slow_snapshot)
    service.smart.navigation_valid = True
    def run():
        try:
            result.append(service.snapshot())
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=run)
    worker.start()
    assert entered.wait(2)
    service.navigation = SimpleNamespace(caches={"Caucasus": {}}, snapshot=lambda *a, **kw: nav_payload())
    service.smart.navigation_valid = True
    release.set()
    worker.join(3)
    assert not worker.is_alive() and not errors
    nav = result[0]["navigation"]
    assert nav["status"] == "Updating navigation data"
    assert nav["airfields"] == [] and nav["overlay"] is None


@pytest.mark.parametrize("change", ["generation", "invalidated"])
def test_inflight_library_rejects_generation_or_validity_change(service_factory, change):
    create, _ = service_factory
    service = create()
    entered, release = threading.Event(), threading.Event()
    result, errors = [], []
    def slow_library(terrain):
        entered.set()
        assert release.wait(3)
        return nav_payload()
    service.navigation = SimpleNamespace(caches={"Caucasus": {}}, library=slow_library)
    service.smart.navigation_valid = True
    def run():
        try:
            result.append(service.navigation_library("Caucasus"))
        except ValueError as exc:
            errors.append(str(exc))
    worker = threading.Thread(target=run)
    worker.start()
    assert entered.wait(2)
    if change == "generation":
        service.navigation = SimpleNamespace(caches={"Caucasus": {}}, library=lambda terrain: nav_payload())
    else:
        service.smart.navigation_valid = False
    release.set()
    worker.join(3)
    assert not worker.is_alive()
    assert result == [] and len(errors) == 1 and "changed while loading" in errors[0]
