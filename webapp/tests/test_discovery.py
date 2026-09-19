from pathlib import Path

import pytest

from webapp.backend.discovery import (DCSDiscovery, SetupUnavailable, _vdf, discover_dcs,
                                      discover_setup, identify_running_setup, install_from_executable, resolve_setup_paths,
                                      saved_games_known_folder)


def install(path, bin_name='bin'):
    (path/bin_name).mkdir(parents=True)
    (path/bin_name/'DCS.exe').write_bytes(b'fixture executable marker; never executed')
    (path/'Config'/'Input').mkdir(parents=True)
    return path


def profile(path):
    (path/'Config'/'Input').mkdir(parents=True)
    return path


def options(tmp_path, installs=()):
    return {'process_snapshot': [], 'install_candidates': list(installs),
            'profile_roots': [tmp_path/'Saved Games'], 'home': tmp_path, 'environ': {}}


def process(root, *args, bin_name='bin'):
    exe = root/bin_name/'DCS.exe'
    return {'name': 'DCS.exe', 'exe': str(exe), 'cmdline': [str(exe), *args], 'pid': 100}


def test_unique_install_and_profile_selected_without_machine_specific_paths(tmp_path):
    dcs = install(tmp_path/'Game installation')
    saved = profile(tmp_path/'Saved Games'/'DCS')
    out = discover_setup(**options(tmp_path, [dcs]))
    assert out['status'] == 'Ready'
    assert out['selected']['install_path'] == str(dcs)
    assert out['selected']['profile_path'] == str(saved)
    assert out['issues'] == []
    assert discover_setup is discover_dcs


@pytest.mark.parametrize('bin_name', ['bin', 'bin-mt'])
def test_process_executable_fast_validation_supports_both_bins(tmp_path, bin_name):
    dcs = install(tmp_path/'DCS World', bin_name)
    assert install_from_executable(dcs/bin_name/'DCS.exe') == dcs
    assert install_from_executable(dcs/bin_name/'DCS_updater.exe') is None
    assert install_from_executable(dcs/'DCS.exe') is None
    assert install_from_executable(None) is None
    assert install_from_executable('') is None


def test_executable_alone_is_not_a_valid_install(tmp_path):
    exe = tmp_path/'Random'/'bin'/'DCS.exe'
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b'fixture')
    assert install_from_executable(exe) is None


def test_multiple_installs_and_profiles_are_never_guessed(tmp_path):
    one, two = install(tmp_path/'One'), install(tmp_path/'Two', 'bin-mt')
    profile(tmp_path/'Saved Games'/'DCS')
    profile(tmp_path/'Saved Games'/'DCS.openbeta')
    out = discover_dcs(**options(tmp_path, [one, two]))
    assert out['selected']['install_path'] is None
    assert out['selected']['profile_path'] is None
    assert len(out['install_candidates']) == 2 and len(out['profile_candidates']) == 2
    assert any('Multiple' in issue for issue in out['issues'])


def test_running_process_selects_install_and_named_w_profile(tmp_path):
    one, two = install(tmp_path/'One'), install(tmp_path/'Two', 'bin-mt')
    profile(tmp_path/'Saved Games'/'DCS')
    selected = profile(tmp_path/'Saved Games'/'DCS.training')
    kwargs = options(tmp_path, [one, two])
    kwargs['process_snapshot'] = [process(two, '-w', 'DCS.training', bin_name='bin-mt')]
    out = discover_dcs(**kwargs)
    assert out['selected']['install_path'] == str(two)
    assert out['selected']['profile_path'] == str(selected)
    assert 'Running DCS -w profile argument' in next(p for p in out['profile_candidates'] if p['path'] == str(selected))['evidence']


def test_absolute_w_profile_outside_default_saved_games_root(tmp_path):
    dcs = install(tmp_path/'Game')
    target = profile(tmp_path/'Another user location'/'Flying profile')
    kwargs = options(tmp_path)
    kwargs['process_snapshot'] = [process(dcs, '-w', str(target))]
    out = discover_dcs(**kwargs)
    assert out['selected']['profile_path'] == str(target)


def test_running_without_w_uses_variant_or_default_and_not_other_profile(tmp_path):
    dcs = install(tmp_path/'Game')
    normal = profile(tmp_path/'Saved Games'/'DCS')
    beta = profile(tmp_path/'Saved Games'/'DCS.openbeta')
    kwargs = options(tmp_path)
    kwargs['process_snapshot'] = [process(dcs)]
    assert discover_dcs(**kwargs)['selected']['profile_path'] == str(normal)
    (dcs/'dcs_variant.txt').write_text('DCS.openbeta\n')
    assert discover_dcs(**kwargs)['selected']['profile_path'] == str(beta)


@pytest.mark.parametrize('args', [('-w', 'DCS.missing'), ('-w',), ('-w', '..\\elsewhere'),
                                  ('-w', 'DCS', '-w', 'DCS.openbeta')])
def test_unknown_running_profile_never_falls_back_to_existing_default(tmp_path, args):
    dcs = install(tmp_path/'Game')
    profile(tmp_path/'Saved Games'/'DCS')
    kwargs = options(tmp_path, [dcs])
    kwargs['process_snapshot'] = [process(dcs, *args)]
    out = discover_dcs(**kwargs)
    assert out['selected']['profile_path'] is None
    assert out['status'] == 'Setup required'


def test_unreadable_process_args_fail_closed(tmp_path):
    dcs = install(tmp_path/'Game')
    profile(tmp_path/'Saved Games'/'DCS')
    kwargs = options(tmp_path, [dcs])
    kwargs['process_snapshot'] = [dict(process(dcs), cmdline=None)]
    assert discover_dcs(**kwargs)['selected']['profile_path'] is None


def test_multiple_running_profiles_stay_ambiguous_even_in_same_install(tmp_path):
    dcs = install(tmp_path/'Game')
    profile(tmp_path/'Saved Games'/'DCS')
    profile(tmp_path/'Saved Games'/'DCS.openbeta')
    kwargs = options(tmp_path)
    kwargs['process_snapshot'] = [process(dcs, '-w', 'DCS'), process(dcs, '-w', 'DCS.openbeta')]
    out = discover_dcs(**kwargs)
    assert out['selected']['install_path'] == str(dcs)
    assert out['selected']['profile_path'] is None


def test_explicit_override_resolves_ambiguity_and_invalid_override_never_falls_back(tmp_path):
    one, two = install(tmp_path/'One'), install(tmp_path/'Two')
    normal = profile(tmp_path/'Saved Games'/'DCS')
    special = profile(tmp_path/'Saved Games'/'DCS.special')
    kwargs = options(tmp_path, [one, two])
    assert resolve_setup_paths(two, special, **kwargs) == (two, special)
    out = discover_dcs(tmp_path/'Removed installation', normal, **kwargs)
    assert out['selected']['install_path'] is None
    assert any('explicit installation' in issue for issue in out['issues'])
    with pytest.raises(SetupUnavailable, match='explicit'):
        resolve_setup_paths(one, tmp_path/'Removed profile', **kwargs)


def test_named_custom_profile_is_visible_but_not_selected_automatically(tmp_path):
    dcs = install(tmp_path/'Game')
    target = profile(tmp_path/'Saved Games'/'DCS.training')
    out = discover_dcs(**options(tmp_path, [dcs]))
    assert out['selected']['profile_path'] is None
    assert out['profile_candidates'][0]['path'] == str(target)


def test_known_folder_redirection_and_reliable_fallback(tmp_path):
    redirected = tmp_path/'Redirected games'
    assert saved_games_known_folder(tmp_path, lambda: redirected) == redirected
    def unavailable(): raise OSError('fixture')
    assert saved_games_known_folder(tmp_path, unavailable) == tmp_path/'Saved Games'
    dcs = install(tmp_path/'Game')
    target = profile(redirected/'DCS')
    out = discover_dcs(process_snapshot=[], install_candidates=[dcs], home=tmp_path, known_folder=lambda: redirected)
    assert out['selected']['profile_path'] == str(target)


@pytest.fixture
def fake_known_folder_native(monkeypatch, tmp_path):
    """Exercise the ctypes contract without calling Windows or changing folders."""
    import ctypes
    from types import SimpleNamespace
    from webapp.backend import discovery
    calls, releases = [], []
    result = {'path': str(tmp_path/'Redirected games'), 'hresult': 0}

    class Query:
        def __call__(self, guid, flags, token, output):
            calls.append((bytes(guid._obj), flags, token))
            output._obj.value = result['path']
            return result['hresult']

    class Release:
        def __call__(self, value):
            releases.append(value.value)

    query, release = Query(), Release()

    def load(name, **kwargs):
        assert kwargs == {'use_last_error': True}
        if name == 'shell32':
            return SimpleNamespace(SHGetKnownFolderPath=query)
        assert name == 'ole32'
        return SimpleNamespace(CoTaskMemFree=release)

    monkeypatch.setattr(ctypes, 'WinDLL', load, raising=False)
    discovery._windows_saved_games_known_folder.cache_clear()
    yield discovery, result, calls, releases, query, release
    discovery._windows_saved_games_known_folder.cache_clear()


def test_known_folder_native_is_cached_for_snapshot_workers(fake_known_folder_native):
    import ctypes
    import uuid
    from concurrent.futures import ThreadPoolExecutor
    discovery, result, calls, releases, query, release = fake_known_folder_native
    expected = Path(result['path'])
    # Setup discovery warms this path before API worker threads start.
    assert discovery._windows_saved_games_known_folder() == expected
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(lambda _: discovery._windows_saved_games_known_folder(), range(64))) == [expected]*64
    assert calls == [(uuid.UUID('4C5C32FF-BB9D-43B0-B5B4-2D72E54EAAA4').bytes_le, 0x4000, None)]
    assert len(releases) == 1 and releases[0]
    assert ctypes.sizeof(discovery._KnownFolderGUID) == 16
    assert query.argtypes[0] == ctypes.POINTER(discovery._KnownFolderGUID)
    assert release.argtypes == [ctypes.c_void_p]
    assert release.restype is None


def test_failed_known_folder_lookup_is_not_repeated_on_every_request(fake_known_folder_native):
    discovery, result, calls, releases, _, _ = fake_known_folder_native
    result['hresult'] = -1
    assert discovery._windows_saved_games_known_folder() is None
    assert discovery._windows_saved_games_known_folder() is None
    assert len(calls) == len(releases) == 1


def test_explicit_known_folder_resolver_remains_live(monkeypatch, tmp_path):
    from webapp.backend import discovery
    monkeypatch.setattr(discovery, '_windows_saved_games_known_folder',
                        lambda: pytest.fail('Explicit resolver must bypass the native cache'))
    locations = iter((tmp_path/'First', tmp_path/'Second'))
    resolver = lambda: next(locations)
    assert saved_games_known_folder(tmp_path, resolver) == tmp_path/'First'
    assert saved_games_known_folder(tmp_path, resolver) == tmp_path/'Second'


def test_idle_process_check_does_not_resolve_known_folder():
    def blocked_resolver():
        pytest.fail('No DCS process needs no shell folder lookup')
    result = identify_running_setup([], known_folder=blocked_resolver)
    assert not result['running'] and not result['unique'] and not result['ambiguous']
    assert result['process_identity'] is None and result['sessions'] == []


@pytest.mark.parametrize('modern', [True, False])
def test_steam_library_manifest_is_data_only_and_points_to_validated_install(tmp_path, modern):
    steam = tmp_path/'Steam'
    (steam/'steamapps').mkdir(parents=True)
    library = tmp_path/'Other Steam Library'
    dcs = install(library/'steamapps'/'common'/'DCS World Steam Edition')
    escaped = str(library).replace('\\', '\\\\')
    item = f'"path" "{escaped}"' if modern else f'"{escaped}"'
    body = f'"1" {{ {item} "apps" {{ "223750" "123" }} }}' if modern else f'"1" {item}'
    (steam/'steamapps'/'libraryfolders.vdf').write_text('"libraryfolders" { '+body+' }')
    (library/'steamapps'/'appmanifest_223750.acf').write_text('"AppState" { "appid" "223750" "installdir" "DCS World Steam Edition" }')
    profile(tmp_path/'Saved Games'/'DCS')
    kwargs = options(tmp_path)
    kwargs['steam_roots'] = [steam]
    out = discover_dcs(**kwargs)
    assert out['selected']['install_path'] == str(dcs)
    assert any('Steam DCS manifest' in item for item in out['install_candidates'][0]['evidence'])


def test_malformed_steam_manifest_does_not_resolve_path_outside_library(tmp_path):
    steam = tmp_path/'Steam'
    (steam/'steamapps').mkdir(parents=True)
    outside = install(tmp_path/'External game')
    (steam/'steamapps'/'appmanifest_223750.acf').write_text('"AppState" { "appid" "223750" "installdir" "../../../External game" }')
    out = discover_dcs(**options(tmp_path), steam_roots=[steam])
    assert out['selected']['install_path'] is None
    assert any('manifest' in item for item in out['issues'])


def test_registry_candidates_still_need_executable_and_inputs(tmp_path):
    dcs = install(tmp_path/'Registered game')
    profile(tmp_path/'Saved Games'/'DCS')
    out = discover_dcs(**options(tmp_path), registry_candidates=[
        {'path': dcs, 'evidence': 'HKCU Eagle Dynamics fixture'}, {'path': tmp_path/'Stale registry', 'evidence': 'HKLM fixture'}])
    assert len(out['install_candidates']) == 1
    assert out['selected']['install_path'] == str(dcs)


def test_device_census_preserves_guid_type_aircraft_and_unknown_connection_without_lua_eval(tmp_path):
    dcs = install(tmp_path/'Game')
    saved = profile(tmp_path/'Saved Games'/'DCS')
    (dcs/'Mods'/'aircraft'/'Hornet'/'Input'/'FA-18C'/'keyboard').mkdir(parents=True)
    (saved/'Mods'/'aircraft'/'Custom jet'/'Input'/'CustomJet'/'joystick').mkdir(parents=True)
    guid = '6F1D2B61-D5A0-11CF-BFC7-444553540000'
    for aircraft in ('FA-18C_hornet', 'F-22A'):
        for kind, name in [('keyboard', 'Keyboard.diff.lua'), ('joystick', f'Test HOTAS {{{guid}}}.diff.lua')]:
            folder = saved/'Config'/'Input'/aircraft/kind
            folder.mkdir(parents=True)
            (folder/name).write_text('error("This file must never be executed or parsed")')
    out = discover_dcs(**options(tmp_path, [dcs]))
    inv = out['inventory']
    assert inv['counts'] == {'installed_aircraft': 2, 'saved_aircraft': 2, 'devices': 2, 'diff_files': 4}
    joystick = next(item for item in inv['devices'] if item['type'] == 'joystick')
    assert joystick['name'] == 'Test HOTAS' and joystick['guid'] == guid
    assert joystick['connection'] == 'Unknown' and joystick['source'] == 'Saved configuration'
    assert set(joystick['aircraft']) == {'FA-18C_hornet', 'F-22A'}
    assert len(joystick['diff_files']) == 2


def test_candidates_and_devices_have_stable_opaque_ids_and_merged_evidence(tmp_path):
    dcs = install(tmp_path/'Secret installation location')
    saved = profile(tmp_path/'Saved Games'/'DCS')
    first = discover_dcs(**options(tmp_path, [dcs, {'path': dcs, 'evidence': 'Second source'}]))
    second = discover_dcs(dcs, saved, **options(tmp_path, [dcs]))
    assert len(first['install_candidates']) == 1
    assert first['selected']['install_id'] == second['selected']['install_id']
    assert 'Secret' not in first['selected']['install_id']
    assert 'Second source' in first['install_candidates'][0]['evidence']


def test_cache_is_detached_bounded_and_refreshable(tmp_path):
    now = [0.]
    dcs = install(tmp_path/'Game')
    profile(tmp_path/'Saved Games'/'DCS')
    service = DCSDiscovery(clock=lambda: now[0], **options(tmp_path, [dcs]))
    first = service.snapshot()
    first['selected']['profile_path'] = 'mutated'
    assert service.snapshot()['selected']['profile_path'] != 'mutated'
    profile(tmp_path/'Saved Games'/'DCS.openbeta')
    assert service.snapshot()['status'] == 'Ready'
    now[0] = 10
    assert service.snapshot()['selected']['profile_path'] is None


@pytest.mark.parametrize('content', ['"x" { "a" "b"', '"x" { "a" "b" "a" "c" }', '"x" {'*15+'}'*15])
def test_vdf_parser_rejects_incomplete_duplicate_or_deep_tables(content):
    with pytest.raises(ValueError): _vdf(content)


def test_running_setup_helper_has_process_creation_identity_and_no_inventory_or_registry(tmp_path, monkeypatch):
    dcs = install(tmp_path/'Game', 'bin-mt')
    saved = profile(tmp_path/'Saved Games'/'DCS.training')
    p = dict(process(dcs, '-w', 'DCS.training', bin_name='bin-mt'), create_time=1234.5)
    monkeypatch.setattr('webapp.backend.discovery._inventory', lambda *a: pytest.fail('No inventory work'))
    monkeypatch.setattr('webapp.backend.discovery._registry_sources', lambda: pytest.fail('No registry work'))
    result = identify_running_setup([p], profile_roots=[tmp_path/'Saved Games'])
    assert result['unique'] and not result['ambiguous']
    assert result['install_path'] == str(dcs) and result['profile_path'] == str(saved)
    assert result['process_identity'] == '100:1234.5'
    assert result['sessions'][0]['process_identity'] == '100:1234.5'


def test_running_setup_no_process_unknown_profile_multiple_process_and_missing_creation_time(tmp_path):
    dcs = install(tmp_path/'Game')
    profile(tmp_path/'Saved Games'/'DCS')
    kwargs = {'profile_roots': [tmp_path/'Saved Games']}
    assert identify_running_setup([], **kwargs)['running'] is False
    unknown = identify_running_setup([process(dcs, '-w', 'DCS.missing')], **kwargs)
    assert unknown['ambiguous'] and unknown['profile_path'] is None
    multiple = identify_running_setup([process(dcs), process(dcs)], **kwargs)
    assert multiple['ambiguous'] and multiple['process_identity'] is None and multiple['install_path'] is None
    no_time = identify_running_setup([process(dcs)], **kwargs)
    assert no_time['process_identity'] is None


def test_manual_selected_profile_cannot_masquerade_as_running_profile(tmp_path):
    dcs = install(tmp_path/'Game')
    active = profile(tmp_path/'Saved Games'/'DCS')
    offline = profile(tmp_path/'Saved Games'/'DCS.other')
    kwargs = options(tmp_path)
    kwargs['process_snapshot'] = [dict(process(dcs), create_time=1111.25)]
    result = discover_dcs(dcs, offline, **kwargs)
    assert result['status'] == 'Ready' and result['selected']['profile_path'] == str(offline)
    assert result['running']['profile_path'] == str(active)
    assert result['runtime_matches_selected'] is False


def test_excess_candidate_inputs_do_not_silently_select_first_subset(tmp_path, monkeypatch):
    dcs = install(tmp_path/'Game')
    profile(tmp_path/'Saved Games'/'DCS')
    monkeypatch.setattr('webapp.backend.discovery.MAX_CANDIDATES', 2)
    out = discover_dcs(**options(tmp_path, [dcs, tmp_path/'Missing one', tmp_path/'Missing two']))
    assert out['selected']['install_path'] is None
    assert any('limit' in issue for issue in out['issues'])
