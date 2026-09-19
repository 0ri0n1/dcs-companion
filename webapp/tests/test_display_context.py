"""Display streaming must not wait for input-catalogue refreshes."""
import threading
from types import SimpleNamespace

import pytest

from webapp.backend.service import DashboardService


@pytest.fixture
def context_service(monkeypatch):
    service = object.__new__(DashboardService)
    service.install_root, service.saved_games = 'E:/DCS World', 'C:/Saved Games/DCS'
    service._display_setup_lock = threading.Lock()
    service._display_setup_proof = None
    current = {'process': '123:456.0', 'session': 'mission-a', 'aircraft': 'FA-18C_hornet',
               'focused': True, 'model_advancing': True, 'telemetry_valid': True}
    service.reader = SimpleNamespace(read=lambda: {'context': dict(current)})
    service.binding_snapshot = lambda *_: pytest.fail('Display capture acquired the input catalogue')
    running = {'process_identity': current['process'], 'install_path': service.install_root,
               'profile_path': service.saved_games, 'ambiguous': False}
    calls = []
    def identify():
        calls.append(True)
        return dict(running)
    monkeypatch.setattr('webapp.backend.discovery.identify_running_setup', identify)
    clock = [10.0]
    monkeypatch.setattr('webapp.backend.service.time.monotonic', lambda: clock[0])
    return service, current, running, calls, clock


def test_verified_process_reuse_does_not_reuse_focus_or_mission(context_service):
    service, current, _, calls, _ = context_service
    assert service.display_context()['setup_matches_process'] is True
    current.update(focused=False, session='mission-b', model_advancing=False, telemetry_valid=False)
    refreshed = service.display_context()
    assert refreshed['setup_matches_process'] is True
    assert refreshed['session'] == 'mission-b'
    assert not refreshed['focused'] and not refreshed['model_advancing'] and not refreshed['telemetry_valid']
    assert len(calls) == 1


def test_new_process_and_changed_selected_roots_revalidate(context_service):
    service, current, running, calls, _ = context_service
    assert service.display_context()['setup_matches_process']
    current['process'] = '123:900.0'  # Reused PID is a different process.
    assert not service.display_context()['setup_matches_process']
    running['process_identity'] = current['process']
    service.saved_games = 'C:/Saved Games/DCS.openbeta'
    assert not service.display_context()['setup_matches_process']
    assert len(calls) == 3


def test_failed_identity_retries_without_a_restart(context_service):
    service, _, running, calls, clock = context_service
    running['ambiguous'] = True
    assert not service.display_context()['setup_matches_process']
    running['ambiguous'] = False
    assert not service.display_context()['setup_matches_process']
    clock[0] += 1.1
    assert service.display_context()['setup_matches_process']
    assert len(calls) == 2


def test_no_process_discards_previous_verified_match(context_service):
    service, current, _, calls, _ = context_service
    assert service.display_context()['setup_matches_process']
    current['process'] = None
    assert not service.display_context()['setup_matches_process']
    current['process'] = '123:456.0'
    assert service.display_context()['setup_matches_process']
    assert len(calls) == 2


def test_discovery_failure_cannot_authorize_capture(context_service, monkeypatch):
    service, _, _, _, _ = context_service
    def failed():
        raise OSError('Process identity unavailable')
    monkeypatch.setattr('webapp.backend.discovery.identify_running_setup', failed)
    assert not service.display_context()['setup_matches_process']


def test_selected_profile_mismatch_never_uses_a_cached_success(context_service):
    service, _, running, _, _ = context_service
    running['profile_path'] = 'C:/Saved Games/DCS-other'
    assert not service.display_context()['setup_matches_process']
    assert service._display_setup_proof['matches'] is False
