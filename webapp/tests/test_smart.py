import json
from pathlib import Path
from types import SimpleNamespace
import threading
import pytest
from webapp.backend.bindings import BindingService, atomic_json, fingerprint, source_record
from webapp.backend.smart import (SmartMaintenance, autostart_status, generation_lock,
                                  readiness, refresh_bindings_safely)


class FixtureBindings(BindingService):
    def current_sources(self):
        return [source_record(self.install_root / "controls.txt")]

    def refresh(self):
        revision = fingerprint(self.current_sources())
        combo = (self.install_root / "controls.txt").read_text()
        self._snapshot = {"source_fingerprint": revision, "actions": [], "safe_actions": [], "combo": combo}
        atomic_json(self.allowlist_path, {"source_fingerprint": revision, "combo": combo})
        atomic_json(self.cache_path, self._snapshot)


@pytest.fixture
def binding_fixture(tmp_path):
    install = tmp_path / "install"
    install.mkdir()
    (install / "controls.txt").write_text("N")
    live = FixtureBindings(tmp_path / "live", install, tmp_path / "saved")
    default = live.module_root / "keyboard" / "default.lua"
    default.parent.mkdir(parents=True)
    default.write_text("return {}")
    live.refresh()
    return live, install / "controls.txt", tmp_path / "runtime"


def test_refresh_publishes_current_generation_and_backs_up_old_cache(binding_fixture):
    live, source, runtime = binding_fixture
    original = live.cache_path.read_bytes()
    source.write_text("P")
    assert not live.check_current()["valid"]
    assert refresh_bindings_safely(live, runtime, factory=FixtureBindings)
    assert live.check_current()["valid"]
    assert live._snapshot["combo"] == "P"
    assert live.allowlist_path.is_relative_to(live.data_dir)
    assert any(p.read_bytes() == original for p in (live.cache_path.parent / "backups").iterdir())
    assert not list(runtime.glob("binding-refresh-*"))


def test_source_changes_mid_build_never_publish(binding_fixture):
    live, source, runtime = binding_fixture
    old = live.cache_path.read_bytes()
    source.write_text("P")
    class Changing(FixtureBindings):
        def refresh(self):
            super().refresh()
            source.write_text("Q")
    with pytest.raises(RuntimeError, match="changed"):
        refresh_bindings_safely(live, runtime, factory=Changing)
    assert live.cache_path.read_bytes() == old
    assert not live.check_current()["valid"]


def test_stop_prevents_staged_publication(binding_fixture):
    live, source, runtime = binding_fixture
    old = live.cache_path.read_bytes()
    source.write_text("P")
    stopped = threading.Event()
    stopped.set()
    assert not refresh_bindings_safely(live, runtime, stopped, factory=FixtureBindings)
    assert live.cache_path.read_bytes() == old


def test_generation_lock_excludes_second_publisher(tmp_path):
    with generation_lock(tmp_path):
        with pytest.raises((RuntimeError, OSError)):
            with generation_lock(tmp_path):
                pytest.fail("A second writer acquired the same generation lock")


def test_slow_staging_does_not_hold_live_binding_lock(binding_fixture):
    live, source, runtime = binding_fixture
    entered, release = threading.Event(), threading.Event()
    class Slow(FixtureBindings):
        def refresh(self):
            entered.set()
            assert release.wait(3)
            super().refresh()
    source.write_text("P")
    thread = threading.Thread(target=refresh_bindings_safely, args=(live, runtime), kwargs={"factory": Slow})
    thread.start()
    assert entered.wait(2)
    assert live._lock.acquire(timeout=.1)
    live._lock.release()
    assert not live.check_current()["valid"]
    release.set()
    thread.join(3)
    assert not thread.is_alive()


def test_maintenance_debounces_and_cancels_before_background_refresh(tmp_path):
    now, invalidations, jobs = [0], [], []
    bindings = SimpleNamespace(check_current=lambda: {"valid": False, "current_fingerprint": "new"})
    nav = SimpleNamespace(check=lambda: {"valid": True})
    service = SimpleNamespace(settings=SimpleNamespace(runtime_dir=tmp_path), bindings=bindings,
                              queue=SimpleNamespace(invalidate=invalidations.append))
    smart = SmartMaintenance(service, clock=lambda: now[0], navigation=nav)
    smart._refresh = jobs.append
    smart.tick()
    assert len(invalidations) == 1 and not jobs
    now[0] = 1
    smart.tick()
    assert not jobs
    now[0] = 3
    smart.tick()
    smart.thread.join(1)
    assert jobs == ["bindings"]
    smart.close()


def test_error_retry_is_delayed_and_never_sends(tmp_path, monkeypatch):
    now = [100.0]
    service = SimpleNamespace(settings=SimpleNamespace(runtime_dir=tmp_path), bindings=object())
    smart = SmartMaintenance(service, clock=lambda: now[0], navigation=SimpleNamespace())
    monkeypatch.setattr("webapp.backend.smart.refresh_bindings_safely", lambda *a: (_ for _ in ()).throw(ValueError("bad source")))
    smart._refresh("bindings")
    assert smart.retry_at["bindings"] == 160
    assert smart.reports["bindings"]["status"] == "Error"


def test_watcher_status_must_have_recent_heartbeat(tmp_path):
    path = tmp_path / "autostart-status.json"
    path.write_text(json.dumps({"enabled": True, "updated_epoch": 100, "state": "Waiting for DCS", "secret": "not exposed"}))
    assert autostart_status(tmp_path, 105)["enabled"]
    assert "secret" not in autostart_status(tmp_path, 105)
    assert not autostart_status(tmp_path, 116)["enabled"]
    path.write_text("corrupt")
    assert not autostart_status(tmp_path, 105)["enabled"]


def test_readiness_is_actionable_without_promising_unsupported_controls():
    current = {"status": "Current"}
    assert readiness({"dcs_running": False}, current, current, current)[1] == "Ready for DCS"
    assert "choose an aircraft" in readiness({"dcs_running": True, "status": "DCS menus/no aircraft"}, current, current, current)[1]
    assert "unavailable" in readiness({"dcs_running": True, "telemetry_fresh": True}, {"status": "Unsupported"}, current, current)[2]
    assert readiness({"dcs_running": False}, current, {"status": "Error"}, current)[0] == "attention"
    assert readiness({"dcs_running": True}, current, current, {"status": "Waiting"})[1] == "Updating your controls"
    failed = readiness({"dcs_running": True}, {"status": "Invalidated"}, current, {"status": "Error", "detail": "Extraction failed"})
    assert failed == ("attention", "Controls need attention", "Extraction failed")
