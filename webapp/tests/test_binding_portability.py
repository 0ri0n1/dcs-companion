"""Portable source identity and honest unavailable/device states (no DCS required)."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from webapp.backend.bindings import BindingService, atomic_json, fingerprint
from webapp.backend.adapters import get_adapter, list_adapters


def make_service(tmp_path, *, install='Install', saved='Saved', aircraft='FA-18C_hornet'):
    service = BindingService(tmp_path/'data', tmp_path/install, tmp_path/saved, aircraft=aircraft)
    default = service.module_root/'keyboard'/'default.lua'
    default.parent.mkdir(parents=True, exist_ok=True)
    default.write_text('return {}')
    return service


def publish(service, *, legacy=False):
    sources = service.current_sources()
    cached = {'schema':'dcs-companion/bindings/1', 'aircraft':service.aircraft,
              'source_fingerprint':fingerprint(sources), 'sources':sources,
              'actions':[{'name':'Profile-specific assignment'}],
              'observed_devices':list(service._device_history.values())}
    if not legacy:
        cached['setup_identity'] = service.setup_identity
    atomic_json(service.cache_path, cached)
    return cached


def test_explicit_paths_bypass_discovery_even_for_fixture_directories(tmp_path):
    with patch('webapp.backend.discovery.resolve_setup_paths', side_effect=AssertionError('must not discover')):
        service = make_service(tmp_path)
    assert service.install_root == tmp_path/'Install'
    assert service.saved_games == tmp_path/'Saved'


def test_default_paths_use_discovery_selected_pair(tmp_path):
    with patch('webapp.backend.discovery.resolve_setup_paths', return_value=(tmp_path/'Other Install', tmp_path/'DCS.profile')) as discover:
        service = BindingService(tmp_path/'data')
    discover.assert_called_once_with(None, None)
    assert service.install_root == tmp_path/'Other Install'
    assert service.saved_games == tmp_path/'DCS.profile'


def test_unavailable_setup_boots_without_filesystem_fallback(tmp_path):
    from webapp.backend.discovery import SetupUnavailable
    with patch('webapp.backend.discovery.resolve_setup_paths', side_effect=SetupUnavailable('Choose one of two DCS profiles.')):
        service = BindingService(tmp_path/'data')
    assert service.install_root is None and service.saved_games is None
    snapshot = service.snapshot()
    assert snapshot['status'] == 'Unavailable'
    assert snapshot['rebuild_eligible'] is False
    assert snapshot['actions'] == snapshot['devices'] == []
    assert 'two DCS profiles' in snapshot['reason']
    assert service.current_sources() == []
    assert service.get_safe_actions() == []
    with pytest.raises(RuntimeError, match='two DCS profiles'):
        service.refresh()
    assert not service.cache_path.exists()


def test_setup_error_prevents_independent_rediscovery(tmp_path):
    with patch('webapp.backend.discovery.resolve_setup_paths', side_effect=AssertionError('must not rediscover')):
        service = BindingService(tmp_path/'data', setup_error='Selected profile is no longer available.')
        assert service.snapshot()['status'] == 'Unavailable'
        assert list_adapters(setup_error=service.setup_error) == []


@pytest.mark.parametrize('install,saved', [('', 'Saved'), ('Install', ''), (' ', ' ')])
def test_empty_paths_never_become_current_directory(tmp_path, install, saved):
    service = BindingService(tmp_path/'data', install, saved)
    assert service.install_root is None
    assert service.snapshot()['status'] == 'Unavailable'


def test_missing_module_disables_automatic_rebuild_until_installed(tmp_path):
    service = BindingService(tmp_path/'data', tmp_path/'Install', tmp_path/'Saved')
    check = service.check_current()
    assert check['status'] == 'Unavailable' and not check['rebuild_eligible']
    assert 'Install this module' in check['reason']
    default = service.module_root/'keyboard'/'default.lua'
    default.parent.mkdir(parents=True)
    default.write_text('return {}')
    check = service.check_current()
    assert check['status'] == 'Invalidated' and check['rebuild_eligible']


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('install,saved', [('OtherInstall','Saved'), ('Install','OtherSaved')])
def test_disk_cache_cannot_cross_installation_or_profile(tmp_path, legacy, install, saved):
    original = make_service(tmp_path)
    publish(original, legacy=legacy)
    other = make_service(tmp_path, install=install, saved=saved)
    assert other._snapshot is None
    assert other.snapshot()['actions'] == []


@pytest.mark.parametrize('legacy', [False, True])
def test_same_setup_cache_restores_and_remains_source_checked(tmp_path, legacy):
    original = make_service(tmp_path)
    publish(original, legacy=legacy)
    loaded = make_service(tmp_path)
    assert loaded._snapshot is not None
    assert loaded.check_current()['valid']
    (loaded.module_root/'keyboard'/'default.lua').write_text('return {keyCommands={}}')
    assert not loaded.check_current()['valid']


@pytest.mark.parametrize('aircraft', ['F-22A', 'FA-18C_hornet'])
def test_historical_device_evidence_isolated_by_both_roots(tmp_path, aircraft):
    original = make_service(tmp_path, aircraft=aircraft)
    name = 'Generic Stick {11111111-2222-3333-4444-555555555555}'
    log = original.saved_games/'Logs'/'dcs.log'
    log.parent.mkdir(parents=True)
    log.write_text(f'INFO INPUT: created [Joystick] with full id [{name}]\n')
    publish(original)
    log.unlink()
    for install, saved in [('OtherInstall','Saved'), ('Install','OtherSaved')]:
        other = make_service(tmp_path, install=install, saved=saved, aircraft=aircraft)
        assert other.last_seen_devices() == []
        with pytest.raises(ValueError, match='installation'):
            other.inherit_device_history(original)


def test_same_guid_log_alias_does_not_duplicate_configured_device(tmp_path):
    service = make_service(tmp_path)
    name = 'Generic Stick {aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee}'
    diff = service.user_input_root/'joystick'/(name+'.diff.lua')
    diff.parent.mkdir(parents=True)
    diff.write_text('return {}')
    with patch.object(service, 'last_seen_devices', return_value=[name.upper(), name]):
        specs = service.device_specs()
    assert len(specs) == 2
    assert specs[1]['id'] == 'joystick:AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE'
    assert specs[1]['name'] == name and specs[1]['configured']
    assert len(specs[1]['aliases']) == 2


def test_distinct_guids_same_device_model_remain_distinct(tmp_path):
    service = make_service(tmp_path)
    names = [f'Generic Stick {{{digit*8}-2222-3333-4444-555555555555}}' for digit in '12']
    with patch.object(service, 'last_seen_devices', return_value=names):
        specs = service.device_specs()
    assert len(specs) == 3 and specs[1]['id'] != specs[2]['id']


def test_same_guid_multiple_saved_profile_names_withholds_guess(tmp_path):
    service = make_service(tmp_path)
    root = service.user_input_root/'joystick'
    root.mkdir(parents=True)
    for prefix in ['Old Stick', 'Renamed Stick']:
        (root/(prefix+' {11111111-2222-3333-4444-555555555555}.diff.lua')).write_text('return {}')
    specs = service.device_specs()
    assert len(specs) == 2 and specs[1]['ambiguous']
    (service.install_root/'autoupdate.cfg').write_text('{"version":"fixture"}')
    with patch.object(service, '_extract', return_value={
            'actions':[{'name':'Visible keyboard control','combos':[{'key':'N'}]}], 'raw':{}}):
        result = service.refresh()
    ambiguous = next(device for device in result['devices'] if device['id'] != 'keyboard')
    assert ambiguous['status'] == 'Ambiguous saved profiles'
    assert ambiguous['connected'] == 'Unknown'
    assert not any(row['device_id'] == ambiguous['id'] for row in result['actions'])


def test_adapters_use_supplied_setup_and_do_not_enable_uninstalled_modules(tmp_path):
    install, saved = tmp_path/'OtherInstall', tmp_path/'OtherProfile'
    (install/'Mods'/'aircraft'/'FA-18C').mkdir(parents=True)
    with patch('webapp.backend.discovery.resolve_setup_paths', side_effect=AssertionError('must not discover')):
        hornet = get_adapter('FA-18C_hornet', install, saved)
        raptor = get_adapter('F-22A', install, saved)
    assert hornet['installed'] and not hornet['command_enabled']
    assert str(saved) in hornet['binding_catalog']['saved_input_path']
    assert not raptor['installed'] and not raptor['command_enabled']
    assert raptor['safe_action_ids'] == []


def test_build_cli_uses_saved_setup_selection(tmp_path, monkeypatch, capsys):
    from webapp.tools import build_bindings
    from webapp.backend.config import Settings
    settings = Settings(runtime_dir=tmp_path/'runtime', data_dir=tmp_path/'data')
    observed = {}
    def manager(selected_settings):
        assert selected_settings is settings
        return SimpleNamespace(active={'install_path':str(tmp_path/'ChosenInstall'),
                                       'profile_path':str(tmp_path/'ChosenProfile')})
    def service(data, install, saved, **kwargs):
        observed.update(data=data, install=install, saved=saved, **kwargs)
        return SimpleNamespace(check_current=lambda:{'rebuild_eligible':False, 'reason':'Fixture module is unavailable'})
    monkeypatch.setattr('webapp.backend.config.Settings.from_env', lambda:settings)
    monkeypatch.setattr('webapp.backend.setup.SetupManager', manager)
    monkeypatch.setattr(build_bindings, 'BindingService', service)
    monkeypatch.setattr('sys.argv', ['build_bindings.py', '--aircraft', 'FA-18C_hornet'])
    assert build_bindings.main() == 2
    assert observed['install'] == str(tmp_path/'ChosenInstall')
    assert observed['saved'] == str(tmp_path/'ChosenProfile')
    assert observed['setup_error'] is None
    assert json.loads(capsys.readouterr().out)['status'] == 'Unavailable'


def test_build_cli_preserves_explicit_pair_without_default_selection(tmp_path, monkeypatch, capsys):
    from webapp.tools import build_bindings
    from webapp.backend.config import Settings
    monkeypatch.setattr('webapp.backend.config.Settings.from_env', lambda:Settings())
    monkeypatch.setattr('webapp.backend.setup.SetupManager', lambda *_:pytest.fail('Explicit pair must not be replaced'))
    monkeypatch.setattr('sys.argv', ['build_bindings.py', '--install-root', str(tmp_path/'Install'),
                                    '--saved-games', str(tmp_path/'Saved'), '--data-dir', str(tmp_path/'data')])
    assert build_bindings.main() == 2
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'Unavailable' and 'not installed' in result['reason']
