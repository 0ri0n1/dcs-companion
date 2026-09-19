import json
import time
from collector import Collector
from webapp.backend.config import Settings
from webapp.backend.service import DashboardService


def test_cold_collector_full_service_stays_informative_and_disallows_input(tmp_path, navigation_cache):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(Collector(str(path)).build_state()))
    service = DashboardService(Settings(state_path=path, runtime_dir=tmp_path,
        data_dir=navigation_cache.parent, install_root=tmp_path/'Install', saved_games=tmp_path/'Saved'))
    try:
        service.reader.processes = lambda: {"running": False, "identity": None, "focused": False}
        result = service.snapshot()
        assert result["health"]["status"] == "Offline"
        assert result["navigation"]["ownship"] is None
        assert len(result["navigation"]["available_terrains"]) == 4
        assert result["bindings"]["aircraft"] == "F-22A"
        assert all(not action["enabled"] for action in result["guide"]["actions"])
        assert "sensor" not in result["aircraft"]
    finally:
        service.close()


def test_live_other_aircraft_does_not_get_f22_guide_bindings(tmp_path):
    path = tmp_path / "state.json"
    state = Collector(str(path)).build_state()
    state["written_epoch"] = time.time()
    state["stream"].update(live=True, data_age_seconds=0, exporter_connected=True)
    state["aircraft"] = {"aircraft": "F-15C", "model_time_s": 50, "latitude": 42, "longitude": 42}
    path.write_text(json.dumps(state))
    service = DashboardService(Settings(state_path=path, runtime_dir=tmp_path,
        data_dir=tmp_path/'data', install_root=tmp_path/'Install', saved_games=tmp_path/'Saved'))
    try:
        service.reader.processes = lambda: {"running": True, "identity": "test-process", "focused": False}
        result = service.snapshot()
        assert result["bindings"]["status"] == "Unsupported"
        assert result["guide"]["actions"] == []
        assert result["navigation"]["terrain"] == "Caucasus"
    finally:
        service.close()
