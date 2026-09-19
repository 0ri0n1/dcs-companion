"""Fresh command proofs must not copy the entire cockpit binding catalogue."""
import os
from types import SimpleNamespace

import pytest

from webapp.backend.bindings import BindingService, fingerprint
from webapp.backend.service import DashboardService


HORNET = 'FA-18C_hornet'


def context_service(tmp_path, monkeypatch, aircraft=HORNET):
    service = object.__new__(DashboardService)
    service.install_root, service.saved_games = tmp_path/'install', tmp_path/'profile'
    state = {'aircraft': aircraft, 'process': '123:456.0', 'session': 'flight', 'mission': 'mission',
             'focused': True, 'telemetry_valid': True, 'model_advancing': True, 'model_time': 20.0}
    reads = []
    def read():
        reads.append(True)
        return {'context': dict(state)}
    service.reader = SimpleNamespace(read=read)
    running = dict(ambiguous=False, process_identity=state['process'],
                   install_path=str(service.install_root), profile_path=str(service.saved_games))
    monkeypatch.setattr('webapp.backend.discovery.identify_running_setup', lambda: dict(running))
    service.binding_snapshot = lambda *_: pytest.fail('Command context copied a full binding snapshot')
    proof = dict(valid=True, status='Current', source_fingerprint='original', current_fingerprint='original')
    checks = []
    def check():
        checks.append(True)
        return dict(proof)
    catalog = SimpleNamespace(aircraft=aircraft, check_current=check,
                              snapshot=lambda *_: pytest.fail('Full catalog snapshot is not required for context'))
    service.binding_services = {aircraft: catalog}
    return SimpleNamespace(service=service, state=state, running=running, proof=proof,
                           checks=checks, reads=reads, catalog=catalog)


@pytest.mark.parametrize('aircraft', [HORNET, 'F-22A'])
def test_every_context_gets_a_fresh_small_proof_and_fresh_telemetry(tmp_path, monkeypatch, aircraft):
    f = context_service(tmp_path, monkeypatch, aircraft)
    original = f.service.context()
    assert original['bindings_valid'] and original['bindings_hash'] == 'original'
    f.state.update(focused=False, model_advancing=False, model_time=21.0)
    f.proof.update(valid=False, status='Invalidated', current_fingerprint='changed')
    current = f.service.context()
    assert current['bindings_valid'] is False and current['bindings_hash'] == 'changed'
    assert not current['focused'] and not current['model_advancing']
    assert current['model_time'] == 21.0
    assert len(f.reads) == len(f.checks) == 2
    assert original['bindings_valid'] and original['bindings_hash'] == 'original'


@pytest.mark.parametrize('change', [
    {'valid': False}, {'valid': None}, {'valid': 1},
    {'status': 'Unavailable'}, {'status': 'Unsupported'}, {'status': 'Error'},
    {'status': 'Invalidated'}, {'status': 'Unknown'},
    {'current_fingerprint': 'changed'}, {'source_fingerprint': None},
])
def test_invalid_or_contradictory_proof_never_authorizes_commands(tmp_path, monkeypatch, change):
    f = context_service(tmp_path, monkeypatch)
    f.proof.update(change)
    assert f.service.context()['bindings_valid'] is False
    assert len(f.checks) == 1


@pytest.mark.parametrize('aircraft', ['UnregisteredModule', None])
def test_missing_catalog_does_not_inherit_another_aircraft(tmp_path, monkeypatch, aircraft):
    f = context_service(tmp_path, monkeypatch)
    f.state['aircraft'] = aircraft
    result = f.service.context()
    assert result['bindings_valid'] is False and result['bindings_hash'] is None
    assert not f.checks


def test_wrong_aircraft_catalog_cannot_authorize_even_with_current_proof(tmp_path, monkeypatch):
    f = context_service(tmp_path, monkeypatch)
    f.catalog.aircraft = 'F-22A'
    assert f.service.context()['bindings_valid'] is False
    assert not f.checks


@pytest.mark.parametrize('failure', [OSError('unreadable'), ValueError('invalid'), TypeError('wrong type')])
def test_unreadable_binding_sources_fail_closed(tmp_path, monkeypatch, failure):
    f = context_service(tmp_path, monkeypatch)
    def failed():
        raise failure
    f.catalog.check_current = failed
    result = f.service.context()
    assert result['bindings_valid'] is False and result['bindings_hash'] is None


def test_invalid_proof_type_fails_closed(tmp_path, monkeypatch):
    f = context_service(tmp_path, monkeypatch)
    f.catalog.check_current = lambda: None
    assert f.service.context()['bindings_valid'] is False


def test_running_profile_must_still_match_on_every_command_check(tmp_path, monkeypatch):
    f = context_service(tmp_path, monkeypatch)
    assert f.service.context()['bindings_valid']
    f.running['profile_path'] = str(tmp_path/'other-profile')
    result = f.service.context()
    assert not result['setup_matches_process'] and not result['bindings_valid']
    assert len(f.checks) == 2


def test_equal_size_and_mtime_source_edit_still_invalidates_context(tmp_path, monkeypatch):
    f = context_service(tmp_path, monkeypatch)
    bindings = BindingService(tmp_path/'data', f.service.install_root, f.service.saved_games, aircraft=HORNET)
    source = bindings.module_root/'keyboard'/'default.lua'
    source.parent.mkdir(parents=True)
    source.write_text('return {}')
    bindings._snapshot = {'aircraft': HORNET, 'source_fingerprint': fingerprint(bindings.current_sources())}
    bindings.snapshot = lambda *_: pytest.fail('Full snapshot copied')
    f.service.binding_services[HORNET] = bindings
    before = f.service.context()
    assert before['bindings_valid']
    stat = source.stat()
    source.write_text('return []')
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    after = f.service.context()
    assert not after['bindings_valid']
    assert after['bindings_hash'] != before['bindings_hash']
