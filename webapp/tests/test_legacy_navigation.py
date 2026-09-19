import time
import pytest
from webapp.backend.legacy_navigation import legacy_navigation
from webapp.backend.navigation import NavigationService


@pytest.fixture(autouse=True)
def synthetic_navigation(navigation_cache, monkeypatch):
    monkeypatch.setattr("webapp.backend.legacy_navigation.NavigationService",
                        lambda: NavigationService(navigation_cache))


def test_legacy_shape_preserved_and_other_terrain_never_caucasus():
    state = {"written_epoch": time.time(), "stream": {"live": True, "data_age_seconds": 0},
             "aircraft": {"latitude": 25, "longitude": 55, "heading_deg": 90, "ias_kt": 999},
             "raw_latest": {"velocity": {"x": 100, "z": 0}}}
    result = legacy_navigation(state)
    assert result["terrain"] == "PersianGulf"
    assert len(result["airfields"]) == 5
    assert not any(f["field"] == "Batumi" for f in result["airfields"])
    field = result["airfields"][0]
    assert set(["field", "bearing_deg", "range_nm", "eta_minutes", "turn_deg", "turn_direction"]) <= set(field)
    assert result["position"]["ground_speed_kt"] < 200


def test_ambiguous_marianas_and_missing_groundspeed_fail_honestly():
    state = {"written_epoch": time.time(), "stream": {"live": True, "data_age_seconds": 0},
             "aircraft": {"latitude": 14, "longitude": 145, "ias_kt": 400}}
    assert legacy_navigation(state)["airfields"] == []
    state["aircraft"].update(latitude=42, longitude=42)
    result = legacy_navigation(state)
    assert result["airfields"]
    assert all(f["eta_minutes"] is None for f in result["airfields"])
