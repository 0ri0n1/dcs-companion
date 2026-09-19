"""Per-response evidence reuse must never become a command-context cache."""
import copy
from collections import Counter
from types import SimpleNamespace

import pytest

from webapp.backend.service import DashboardService


def rig(tmp_path, monkeypatch, aircraft='F-22A'):
    calls = Counter()
    ctx = dict(aircraft=aircraft, process='123:456', session='flight', mission='mission',
               model_time=20., telemetry_valid=True, model_advancing=True, focused=True)
    telemetry = dict(context=ctx, raw={}, health=dict(telemetry_fresh=True, dcs_running=True, notes=[]),
                     aircraft={'aircraft': aircraft})
    action = dict(id='f22.nav.next', name='Next Waypoint', combo='N', risk='benign', aircraft='F-22A')
    proofs = {name: dict(aircraft=name, status='Current', source_fingerprint=name+'-original',
                        current_fingerprint=name+'-original', safe_actions=[action] if name == 'F-22A' else [],
                        display_only=name != 'F-22A') for name in ('F-22A', 'FA-18C_hornet')}

    class Catalog:
        def __init__(self, name): self.name = self.aircraft = name
        def snapshot(self, name):
            calls['snapshot:'+self.name] += 1
            return copy.deepcopy(proofs[self.name])
        def check_current(self):
            calls['check:'+self.name] += 1
            proof = proofs[self.name]
            return {key: proof[key] for key in ('status', 'source_fingerprint', 'current_fingerprint')} | {
                'valid': proof['status'] == 'Current' and proof['source_fingerprint'] == proof['current_fingerprint']}
        def get_safe_actions(self, name):
            calls['safe:'+self.name] += 1
            return copy.deepcopy(proofs[self.name]['safe_actions']) if proofs[self.name]['status'] == 'Current' else []

    service = DashboardService.__new__(DashboardService)
    service.settings = SimpleNamespace(dry_run=True, lan_enabled=False, runtime_dir=tmp_path)
    service.install_root, service.saved_games = tmp_path/'install', tmp_path/'profile'
    service.last_aircraft = aircraft
    service.binding_services = {name: Catalog(name) for name in proofs}
    service.bindings = service.binding_services['F-22A']
    def read():
        calls['telemetry'] += 1
        return copy.deepcopy(telemetry)
    service.reader = SimpleNamespace(read=read)
    service.mission_reader = SimpleNamespace(snapshot=lambda *_: {})
    service.awareness_reader = SimpleNamespace(snapshot=lambda *_: {})
    service.export_reader = SimpleNamespace(snapshot=lambda *_: {})
    service.navigation = SimpleNamespace(caches={}, snapshot=lambda *_, **__: {})
    service.smart = SimpleNamespace(navigation_valid=True, snapshot=lambda *_: {})
    service.overlay = service.overlay_error = None
    service.queue = SimpleNamespace(get=lambda: None)
    remote_contexts = []
    def remote_status(*, include_catalog, ctx):
        assert include_catalog is False
        remote_contexts.append(dict(ctx))
        return {'armed': False, 'mode': 'preview'}
    service.remote = SimpleNamespace(status=remote_status)
    def identify():
        calls['setup'] += 1
        return dict(ambiguous=False, process_identity=ctx['process'],
                    install_path=str(service.install_root), profile_path=str(service.saved_games))
    monkeypatch.setattr('webapp.backend.discovery.identify_running_setup', identify)
    def adapters(**_):
        calls['adapters'] += 1
        return [dict(id='f22', identifiers=['F-22A']), dict(id='fa18', identifiers=['FA-18C_hornet'])]
    monkeypatch.setattr('webapp.backend.adapters.list_adapters', adapters)
    monkeypatch.setattr('webapp.backend.sensors.build_sensor_snapshot', lambda *_: {})
    monkeypatch.setattr('webapp.backend.cockpit.build_cockpit_snapshot', lambda *_: {})
    return SimpleNamespace(service=service, calls=calls, proofs=proofs, telemetry=telemetry, remote_contexts=remote_contexts)


@pytest.mark.parametrize('aircraft', ['F-22A', 'FA-18C_hornet'])
def test_each_ui_snapshot_reads_each_binding_catalogue_only_once(tmp_path, monkeypatch, aircraft):
    f = rig(tmp_path, monkeypatch, aircraft)
    result = f.service.snapshot()
    other = next(name for name in f.proofs if name != aircraft)
    assert f.calls == Counter({'telemetry': 1, 'snapshot:'+aircraft: 1, 'check:'+other: 1, 'setup': 1, 'adapters': 1})
    assert f.remote_contexts[0]['bindings_hash'] == result['bindings']['current_fingerprint']
    assert f.remote_contexts[0]['bindings_valid'] is True
    assert result['adapter']['identifiers'] == [aircraft]
    assert all(row['status'] == 'Current' for row in result['binding_catalogs'])
    if aircraft == 'F-22A':
        assert result['guide']['actions'][0]['enabled'] is True
    else:
        assert result['guide']['actions'] == []
    assert 'bindings_hash' not in f.telemetry['context']


def test_command_context_and_safe_actions_recheck_after_a_ui_snapshot(tmp_path, monkeypatch):
    f = rig(tmp_path, monkeypatch)
    original = f.service.snapshot()
    f.proofs['F-22A'].update(status='Invalidated', current_fingerprint='new-source-content')
    current = f.service.context()
    assert current['bindings_valid'] is False and current['bindings_hash'] == 'new-source-content'
    assert f.calls['snapshot:F-22A'] == 1 and f.calls['check:F-22A'] == 1 and f.calls['telemetry'] == 2
    assert f.service.safe_actions() == []
    assert f.calls['safe:F-22A'] == 1 and f.calls['telemetry'] == 3
    assert original['bindings']['current_fingerprint'] == 'F-22A-original'
    assert f.service.snapshot()['guide']['actions'] == []


def test_ui_snapshot_does_not_show_guide_actions_from_wrong_generation(tmp_path, monkeypatch):
    f = rig(tmp_path, monkeypatch)
    f.proofs['F-22A']['current_fingerprint'] = 'changed-even-if-status-is-current'
    result = f.service.snapshot()
    assert result['guide']['actions'] == []
    assert f.remote_contexts[0]['bindings_valid'] is False
