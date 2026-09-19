import asyncio
from types import SimpleNamespace

import pytest

from webapp.backend.jev import JevAdvisor, JevUnavailable, compose_advisory, reduce_snapshot


def settings(tmp_path, **overrides):
    values = {
        "jev_api_key": "",
        "jev_enabled": True,
        "jev_mode": "mock",
        "jev_model": "jev-latest",
        "jev_timeout_s": 12.0,
        "runtime_dir": tmp_path,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def snapshot(*, age=0.4, available=True):
    return {
        "health": {
            "aircraft": "FA-18C_hornet", "terrain": "PersianGulf",
            "status": "Live", "telemetry_fresh": True, "model_advancing": True,
        },
        "aircraft": {
            "latitude": 25.0, "longitude": 55.0, "altitude_ft": 18000,
            "ground_speed_kt": 410,
        },
        "awareness": {
            "status": "Available" if available else "Stale",
            "single_player": True, "model_advancing": True, "age_s": age,
            "contacts": [{
                "id": "42", "name": "Convoy Alpha", "type": "Truck",
                "category": "ground", "lat": 25.1, "lon": 55.2,
                "altitude_ft": 80, "age_s": age,
            }],
        },
        "navigation": {"nearest": [{"id": "OMAA", "name": "Abu Dhabi", "distance_nm": 20}]},
        "raw_latest": {"must_not_leave": "secret"},
    }


def test_reducer_is_an_explicit_privacy_boundary():
    reduced = reduce_snapshot(snapshot(), "find the convoy")
    assert set(reduced) == {"request", "session", "ownship", "contacts", "nearby_navigation"}
    assert "must_not_leave" not in str(reduced)
    assert reduced["contacts"][0]["id"] == "42"
    assert reduced["contacts"][0]["distance_nm"] > 0


def test_stale_or_unavailable_awareness_never_supplies_candidates():
    assert reduce_snapshot(snapshot(age=6), "find it")["contacts"] == []
    assert reduce_snapshot(snapshot(available=False), "find it")["contacts"] == []


def test_mock_advisory_selects_but_never_executes(tmp_path):
    advisor = JevAdvisor(settings(tmp_path))
    result = asyncio.run(advisor.evaluate("find the convoy", snapshot()))
    assert result["proposal_allowed"] is True
    assert result["contact"]["id"] == "42"
    assert result["execution"] == "display-only"
    assert (tmp_path / "jev-audit.jsonl").is_file()


def test_missing_key_fails_closed(tmp_path):
    advisor = JevAdvisor(settings(tmp_path, jev_mode="live"))
    with pytest.raises(JevUnavailable):
        asyncio.run(advisor.evaluate("diagnose telemetry", snapshot()))


def test_low_sufficiency_blocks_contact_proposal():
    state = reduce_snapshot(snapshot(), "find the convoy")
    response = {
        "model": "jev-test",
        "answers": {
            "intent": {"type": "choice", "choice": "prepare_waypoint", "confidence": .99, "probabilities": {}},
            "selected_contact": {"type": "choice", "choice": "42", "confidence": .99, "probabilities": {}},
            "data_sufficient": {"type": "noul", "noul": .5},
            "flight_phase": {"type": "choice", "choice": "cruise", "confidence": .8, "probabilities": {}},
            "workload": {"type": "score", "score": .5, "confidence": .8, "legend": {}, "probabilities": {}},
        },
        "usage": {},
    }
    result = compose_advisory(state, response, 100)
    assert result["proposal_allowed"] is False
    assert result["contact"] is None
