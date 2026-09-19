"""All display APIs are fakes. These tests never read or change the desktop."""
import copy
import ctypes
import json

import pytest

from webapp.backend import virtual_display as vd


def mode(x=0, y=0, width=2560, height=1440, **extra):
    return dict(x=x, y=y, width=width, height=height, frequency=60, bits_per_pixel=32,
                orientation=0, display_flags=0, **extra)


def display(source, kind, value, *, primary=False, active=True):
    return {'id': source, 'kind': kind, 'adapter_device_id': ('ROOT\\DISPLAY\\0000' if kind == 'virtual' else 'PCI\\1234')+source,
            'adapter_hardware_id': r'Root\MttVDD' if kind == 'virtual' else 'PCI\\VEN_10DE',
            'adapter_description': 'Arbitrary description', 'primary': primary, 'active': active,
            'mode': copy.deepcopy(value), 'registry_mode': copy.deepcopy(value)}


class FakeNative:
    def __init__(self):
        self.rows = [display('p1', 'physical', mode(), primary=True),
                     display('p2', 'physical', mode(x=2560)),
                     display('v1', 'virtual', mode(x=5120, height=512))]
        self.available = [mode(width=2560, height=512)]
        self.calls = []
        self.hook = None
        self.results = {}

    def snapshot(self):
        return copy.deepcopy(self.rows)

    def modes(self, source):
        assert source == 'v1'
        return copy.deepcopy(self.available)

    def change(self, source, value, flags):
        assert source == 'v1', 'a physical monitor mutation was attempted'
        self.calls.append((source, copy.deepcopy(value), flags))
        row = next(row for row in self.rows if row['id'] == source)
        if flags in (vd.CDS_UPDATEREGISTRY, vd.CDS_UPDATEREGISTRY | vd.CDS_NORESET):
            row['registry_mode'] = copy.deepcopy(value)
        if flags in (0, vd.CDS_UPDATEREGISTRY):
            assert value is not None or flags == 0
            row['mode'] = copy.deepcopy(row['registry_mode'])
            row['active'] = row['mode']['width'] > 0
            if not row['active']:
                row['mode'] = None
        if self.hook:
            self.hook(len(self.calls), row)
        return self.results.get(len(self.calls), 0)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    # No accidental fallback to real Windows is permitted in any test.
    monkeypatch.setattr(vd, '_Win32', lambda: pytest.fail('Unexpected real Windows API construction'))
    native = FakeNative()
    manager = vd.VirtualDisplayManager(tmp_path/'snapshot.json', native=native, dcs_closed=lambda: True)
    return manager, native


def test_plan_is_read_only_exact_below_primary_and_keeps_both_physical_screens(setup):
    manager, native = setup
    plan = manager.prepare()
    assert plan['desired'] == mode(y=1440, height=512)
    assert len(plan['before_physical']) == 2
    assert plan['virtual']['adapter_hardware_id'] == r'Root\MttVDD'
    assert not native.calls and not manager.snapshot_path.exists()


def test_devmode_abi_matches_windows_unicode_structure():
    assert ctypes.sizeof(vd._DEVMODEW) == 220
    assert vd._DEVMODEW.dmFields.offset == 72
    assert vd._DEVMODEW.dmPosition.offset == 76
    assert vd._DEVMODEW.dmPelsWidth.offset == 172
    assert vd._DEVMODEW.dmDisplayFrequency.offset == 184
    assert vd._MODE_BUFFER.private.offset == 220


@pytest.mark.parametrize('mutate,code', [
    (lambda n: n.rows.pop(), 'virtual_identity'),
    (lambda n: n.rows.append(display('v2', 'virtual', mode(x=8000))), 'virtual_identity'),
    (lambda n: n.rows[-1].update(kind='unknown'), 'virtual_identity'),
    (lambda n: n.rows[-1].update(adapter_hardware_id=r'root\mttvdd-spoof'), 'virtual_identity'),
    (lambda n: n.rows[-1].update(adapter_device_id=''), 'virtual_identity'),
    (lambda n: n.rows[-1].update(primary=True), 'primary_identity'),
    (lambda n: n.rows[0].update(kind='unknown'), 'primary_identity'),
    (lambda n: n.rows[-1].update(registry_mode=None), 'rollback_unavailable'),
    (lambda n: n.rows[1]['mode'].update(x=0, y=1440), 'display_overlap'),
    (lambda n: n.available.clear(), 'mode_unavailable'),
    (lambda n: n.available[0].update(frequency=59), 'mode_unavailable'),
    (lambda n: n.available[0].update(orientation=1), 'mode_unavailable'),
    (lambda n: n.available[0].update(display_flags=2), 'mode_unavailable'),
])
def test_unsafe_or_ambiguous_plan_rejected_without_writes(setup, mutate, code):
    manager, native = setup
    mutate(native)
    with pytest.raises(vd.VirtualDisplayError) as error:
        manager.prepare()
    assert error.value.code == code
    assert not native.calls and not manager.snapshot_path.exists()


def test_description_never_establishes_identity(setup):
    manager, native = setup
    native.rows[-1].update(kind='unknown', adapter_hardware_id='', adapter_description='Virtual Display Driver')
    with pytest.raises(vd.VirtualDisplayError, match='Root'):
        manager.prepare()


def test_only_virtual_named_device_is_tested_and_applied_with_explicit_mode(setup):
    manager, native = setup
    physical_before = copy.deepcopy(native.rows[:2])
    result = manager.apply(manager.prepare()['plan_id'])
    assert result['ok'] and result['physical_unchanged']
    assert native.calls == [('v1', mode(y=1440, height=512), vd.CDS_TEST),
                            ('v1', mode(y=1440, height=512), vd.CDS_UPDATEREGISTRY)]
    assert native.rows[:2] == physical_before
    saved = json.loads(manager.snapshot_path.read_text())
    assert saved['status'] == 'Applied'
    assert saved['plan']['current']['mode']['x'] == 5120
    assert saved['after']['mode']['y'] == 1440


def test_durable_before_state_precedes_registry_write(setup):
    manager, native = setup
    def verify(count, row):
        if count == 2:
            saved = json.loads(manager.snapshot_path.read_text())
            assert saved['status'] == 'Prepared'
            assert saved['plan']['current']['mode']['x'] == 5120
    native.hook = verify
    manager.apply(manager.prepare()['plan_id'])


def test_failed_backup_blocks_every_native_change(setup, monkeypatch):
    manager, native = setup
    monkeypatch.setattr(manager, '_save', lambda value: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):
        manager.apply(manager.prepare()['plan_id'])
    assert not native.calls


@pytest.mark.parametrize('closed', [False, None, 1])
def test_dcs_must_be_proven_closed(setup, closed):
    manager, native = setup
    plan = manager.prepare()
    manager.dcs_closed = lambda: closed
    with pytest.raises(vd.VirtualDisplayError) as error:
        manager.apply(plan['plan_id'])
    assert error.value.code == 'dcs_running'
    assert not native.calls


def test_changed_plan_or_tampered_token_never_applied(setup):
    manager, native = setup
    token = manager.prepare()['plan_id']
    native.rows[1]['mode']['frequency'] = 144
    with pytest.raises(vd.VirtualDisplayError) as error:
        manager.apply(token)
    assert error.value.code == 'plan_changed'
    assert not native.calls
    with pytest.raises(vd.VirtualDisplayError):
        manager.apply({'desired': mode()})
    assert not native.calls


def test_already_configured_is_noop(setup):
    manager, native = setup
    native.rows[-1]['mode'] = mode(y=1440, height=512)
    native.rows[-1]['registry_mode'] = mode(y=1440, height=512)
    result = manager.apply(manager.prepare()['plan_id'])
    assert result['status'] == 'Already configured'
    assert not native.calls and not manager.snapshot_path.exists()


def test_mode_test_failure_never_changes_registry(setup):
    manager, native = setup
    native.results[1] = -2
    with pytest.raises(vd.VirtualDisplayError) as error:
        manager.apply(manager.prepare()['plan_id'])
    assert error.value.details['rollback']['status'] == 'Not needed'
    assert len(native.calls) == 1


def test_apply_failure_restores_only_original_virtual_mode(setup):
    manager, native = setup
    before = copy.deepcopy(native.rows)
    native.results[2] = -1
    with pytest.raises(vd.VirtualDisplayError) as error:
        manager.apply(manager.prepare()['plan_id'])
    assert error.value.details['rollback']['status'] == 'Restored'
    assert native.rows == before
    assert all(call[0] == 'v1' for call in native.calls)
    assert json.loads(manager.snapshot_path.read_text())['rollback']['status'] == 'Restored'


def test_restart_required_is_failure_and_rolled_back_not_silent_success(setup):
    manager, native = setup
    before = copy.deepcopy(native.rows)
    native.results[2] = 1
    with pytest.raises(vd.VirtualDisplayError) as error:
        manager.apply(manager.prepare()['plan_id'])
    assert error.value.details['win32_result'] == 1
    assert native.rows == before


def test_physical_change_after_apply_is_reported_and_never_undone(setup):
    manager, native = setup
    def changed(count, row):
        if count == 2:
            native.rows[0]['mode']['width'] = 1920
    native.hook = changed
    with pytest.raises(vd.VirtualDisplayError) as error:
        manager.apply(manager.prepare()['plan_id'])
    assert error.value.code == 'physical_changed'
    assert error.value.details['rollback']['status'] == 'Blocked or failed'
    assert len(native.calls) == 2


def test_identity_swap_after_test_blocks_registry_write(setup):
    manager, native = setup
    native.hook = lambda count, row: row.update(adapter_device_id='replaced') if count == 1 else None
    with pytest.raises(vd.VirtualDisplayError) as error:
        manager.apply(manager.prepare()['plan_id'])
    assert error.value.code == 'virtual_changed'
    assert len(native.calls) == 1


def test_dcs_start_between_test_and_apply_blocks_mutation(setup):
    manager, native = setup
    native.hook = lambda count, row: setattr(manager, 'dcs_closed', lambda: False)
    with pytest.raises(vd.VirtualDisplayError) as error:
        manager.apply(manager.prepare()['plan_id'])
    assert error.value.code == 'dcs_running'
    assert len(native.calls) == 1


def test_dcs_start_after_apply_blocks_any_further_mutation_or_rollback(setup):
    manager, native = setup
    def started(count, row):
        if count == 2:
            manager.dcs_closed = lambda: False
    native.hook = started
    with pytest.raises(vd.VirtualDisplayError) as error:
        manager.apply(manager.prepare()['plan_id'])
    assert error.value.details['rollback']['status'] == 'Blocked or failed'
    assert len(native.calls) == 2
    assert native.rows[-1]['mode'] == mode(y=1440, height=512)


def test_wrong_driver_apply_geometry_is_not_success_and_can_restore(setup):
    manager, native = setup
    def changed(count, row):
        if count == 2:
            row['mode']['height'] = 768
    native.hook = changed
    with pytest.raises(vd.VirtualDisplayError) as error:
        manager.apply(manager.prepare()['plan_id'])
    assert error.value.code == 'apply_mismatch'
    assert error.value.details['rollback']['status'] == 'Restored'


def test_inactive_virtual_can_be_planned_and_enabled(setup):
    manager, native = setup
    native.rows[-1].update(active=False, mode=None)
    result = manager.apply(manager.prepare()['plan_id'])
    assert result['after']['active']
    assert result['after']['mode'] == mode(y=1440, height=512)


def test_nonzero_primary_origin_plan_uses_physical_coordinates(setup):
    manager, native = setup
    native.rows[0]['mode'].update(x=-2560, y=-1440)
    native.rows[0]['registry_mode'].update(x=-2560, y=-1440)
    assert manager.prepare()['desired'] == mode(x=-2560, y=0, height=512)


def test_unknown_secondary_is_protected_and_overlap_is_refused(setup):
    manager, native = setup
    native.rows[1].update(kind='unknown')
    plan = manager.prepare()
    assert plan['before_physical'][1]['kind'] == 'unknown'
    native.rows[1]['mode'].update(x=0, y=1440)
    with pytest.raises(vd.VirtualDisplayError, match='overlaps'):
        manager.prepare()


def test_saved_physical_change_after_test_is_also_protected(setup):
    manager, native = setup
    def changed(count, row):
        if count == 1:
            native.rows[1]['registry_mode']['width'] = 1920
    native.hook = changed
    with pytest.raises(vd.VirtualDisplayError) as error:
        manager.apply(manager.prepare()['plan_id'])
    assert error.value.code == 'physical_changed'
    assert len(native.calls) == 1


def test_rollback_preserves_distinct_prior_registry_mode(setup):
    manager, native = setup
    native.rows[-1]['registry_mode']['x'] = 6000
    before = copy.deepcopy(native.rows)
    native.results[2] = -1
    with pytest.raises(vd.VirtualDisplayError):
        manager.apply(manager.prepare()['plan_id'])
    assert native.rows == before


def test_current_desired_but_registry_different_is_not_false_noop(setup):
    manager, native = setup
    native.rows[-1]['mode'] = mode(y=1440, height=512)
    plan = manager.prepare()
    assert plan['status'] == 'Ready'
    assert manager.apply(plan['plan_id'])['status'] == 'Applied'
    assert native.rows[-1]['registry_mode'] == mode(y=1440, height=512)


def test_failed_activation_rolls_back_to_inactive_and_original_registry(setup):
    manager, native = setup
    native.rows[-1].update(active=False, mode=None)
    before = copy.deepcopy(native.rows)
    native.results[2] = -1
    with pytest.raises(vd.VirtualDisplayError) as error:
        manager.apply(manager.prepare()['plan_id'])
    assert error.value.details['rollback']['status'] == 'Restored'
    assert native.rows == before


def buffer_for(value):
    buffer = vd._MODE_BUFFER()
    buffer.mode.dmSize = 220
    buffer.mode.dmPosition.x, buffer.mode.dmPosition.y = value['x'], value['y']
    for key, member in [('width', 'dmPelsWidth'), ('height', 'dmPelsHeight'), ('frequency', 'dmDisplayFrequency'),
                        ('bits_per_pixel', 'dmBitsPerPel'), ('orientation', 'dmDisplayOrientation'), ('display_flags', 'dmDisplayFlags')]:
        setattr(buffer.mode, member, value[key])
    return buffer


@pytest.fixture
def native_wrapper(monkeypatch):
    from webapp.backend import display_identity
    # Bypass constructor only; exercise actual wrapper with fake DLL methods.
    native = object.__new__(vd._Win32)
    # _Win32 is patched only in the separate setup fixture, never here.
    calls = []
    class DLL:
        def ChangeDisplaySettingsExW(self, source, ptr, hwnd, flags, param):
            result = None
            if ptr is not None:
                value = ctypes.cast(ptr, ctypes.POINTER(vd._DEVMODEW)).contents
                result = {'mode': vd._mode_dict(value), 'fields': value.dmFields, 'extra': value.dmDriverExtra}
            calls.append((source, result, hwnd, flags, param))
            return 0
    native.user = DLL()
    monkeypatch.setattr(display_identity, 'adapter_metadata', lambda source: {
        'kind': 'virtual', 'adapter_hardware_id': r'root\mttvdd', 'adapter_device_id': r'ROOT\DISPLAY\1234'})
    native._modes = lambda source: iter([buffer_for(mode(height=512))])
    native._read = lambda source, index: buffer_for(mode(height=512))
    return native, calls


def test_native_wrapper_uses_verified_enumerated_mode_and_only_named_apply(native_wrapper):
    native, calls = native_wrapper
    target = mode(y=1440, height=512)
    native.change('v1', target, vd.CDS_TEST)
    native.change('v1', target, vd.CDS_UPDATEREGISTRY)
    native.change('v1', target, vd.CDS_UPDATEREGISTRY | vd.CDS_NORESET)
    native.change('v1', None, 0)
    assert all(call[0] == 'v1' and call[2] is None and call[4] is None for call in calls)
    assert calls[0][1]['mode'] == target
    assert calls[0][1]['fields'] == vd.DM_POSITION
    assert calls[-1][1] is None


def test_native_wrapper_refuses_unadvertised_mode(native_wrapper):
    native, calls = native_wrapper
    with pytest.raises(vd.VirtualDisplayError, match='no longer advertised'):
        native.change('v1', mode(width=1920, height=512), vd.CDS_TEST)
    assert not calls


@pytest.mark.parametrize('source,flags', [(None, 0), ('', 0), ('v1', 0x10), ('v1', 0x10000000), ('v1', 0x8)])
def test_native_wrapper_rejects_global_apply_primary_or_unsupported_flags(native_wrapper, source, flags):
    native, calls = native_wrapper
    with pytest.raises(vd.VirtualDisplayError):
        native.change(source, None, flags)
    assert not calls


def test_native_wrapper_rechecks_hardware_identity_before_any_call(native_wrapper, monkeypatch):
    from webapp.backend import display_identity
    native, calls = native_wrapper
    monkeypatch.setattr(display_identity, 'adapter_metadata', lambda source: {
        'kind': 'physical', 'adapter_hardware_id': 'PCI\\VEN_10DE', 'adapter_device_id': 'PCI\\1234'})
    with pytest.raises(vd.VirtualDisplayError, match='exact Root'):
        native.change('v1', mode(height=512), vd.CDS_TEST)
    assert not calls


def test_native_detach_is_bounded_to_virtual_zero_dimensions(native_wrapper):
    native, calls = native_wrapper
    native.change('v1', mode(width=0, height=0), vd.CDS_UPDATEREGISTRY | vd.CDS_NORESET)
    assert calls[0][1]['mode']['width'] == calls[0][1]['mode']['height'] == 0
    assert calls[0][1]['fields'] == vd.DM_POSITION | vd.DM_PELSWIDTH | vd.DM_PELSHEIGHT


def test_enum_mode_buffer_initialized_and_private_capacity_is_bounded(monkeypatch):
    native = object.__new__(vd._Win32)
    class DLL:
        def EnumDisplaySettingsExW(self, source, index, pointer, flags):
            value = ctypes.cast(pointer, ctypes.POINTER(vd._DEVMODEW)).contents
            assert value.dmSize == 220 and value.dmDriverExtra == 4096
            assert source == 'v1' and index == 0 and flags == 0
            value.dmDriverExtra = 0
            value.dmPelsWidth, value.dmPelsHeight = 2560, 512
            return 1
    native.user = DLL()
    assert native._read('v1', 0).mode.dmPelsHeight == 512


def test_enum_rejects_private_buffer_size_report_outside_capacity():
    native = object.__new__(vd._Win32)
    class DLL:
        def EnumDisplaySettingsExW(self, source, index, pointer, flags):
            ctypes.cast(pointer, ctypes.POINTER(vd._DEVMODEW)).contents.dmDriverExtra = 4097
            return 1
    native.user = DLL()
    with pytest.raises(vd.VirtualDisplayError, match='unsupported display mode structure'):
        native._read('v1', 0)


@pytest.fixture
def process_kernel(monkeypatch):
    error = [0]
    monkeypatch.setattr(vd.ctypes, 'set_last_error', lambda value: error.__setitem__(0, value), raising=False)
    monkeypatch.setattr(vd.ctypes, 'get_last_error', lambda: error[0], raising=False)
    class Kernel:
        def __init__(self):
            self.rows = [(0, '[System Process]'), (945, 'Secure System'), (4400, 'python.exe')]
            self.index = 0
            self.closed = []
            self.handle = 123
            self.end_error = 18
        def CreateToolhelp32Snapshot(self, flags, process_id):
            assert flags == 2 and process_id == 0  # No module/heap/thread snapshots.
            return self.handle
        def Process32FirstW(self, handle, pointer):
            self.index = 0
            return self.Process32NextW(handle, pointer)
        def Process32NextW(self, handle, pointer):
            assert handle == 123
            if self.index >= len(self.rows):
                error[0] = self.end_error
                return 0
            entry = ctypes.cast(pointer, ctypes.POINTER(vd._PROCESSENTRY32W)).contents
            assert entry.dwSize == ctypes.sizeof(vd._PROCESSENTRY32W)
            process_id, name = self.rows[self.index]
            self.index += 1
            entry.th32ProcessID = process_id
            encoded = name.encode('utf-16-le')
            for index in range(260):
                entry.szExeFile[index] = int.from_bytes(encoded[index*2:index*2+2], 'little')
            return 1
        def CloseHandle(self, handle):
            self.closed.append(handle)
            return 1
    return Kernel()


def test_native_process_census_resolves_secure_system_without_pid_exemption(process_kernel, monkeypatch):
    rows = vd._process_census(process_kernel)
    assert rows[1] == {'pid': 945, 'name': 'Secure System'}
    assert process_kernel.closed == [123]
    monkeypatch.setattr(vd, '_process_census', lambda: rows)
    assert vd._closed() is True


@pytest.mark.parametrize('name', ['DCS.exe', 'dcs.EXE'])
def test_native_census_detects_dcs_regardless_of_psutil_name(name, process_kernel, monkeypatch):
    process_kernel.rows.append((778, name))
    rows = vd._process_census(process_kernel)
    monkeypatch.setattr(vd, '_process_census', lambda: rows)
    assert vd._closed() is False


@pytest.mark.parametrize('rows', [[], [{'pid': 380, 'name': ''}], [{'pid': 4, 'name': None}], [{'pid': 5}]])
def test_empty_or_opaque_census_never_proves_dcs_closed(rows, monkeypatch):
    monkeypatch.setattr(vd, '_process_census', lambda: rows)
    assert vd._closed() is False


def test_opaque_toolhelp_process_rejects_complete_census_and_closes_handle(process_kernel):
    process_kernel.rows.append((321, ''))
    with pytest.raises(OSError, match='completely'):
        vd._process_census(process_kernel)
    assert process_kernel.closed == [123]


def test_partial_toolhelp_failure_is_not_treated_as_end_of_list(process_kernel):
    process_kernel.end_error = 5
    with pytest.raises(OSError, match='incomplete'):
        vd._process_census(process_kernel)
    assert process_kernel.closed == [123]


@pytest.mark.parametrize('handle', [None, 0, ctypes.c_void_p(-1).value])
def test_failed_native_snapshot_never_walked_or_closed(process_kernel, handle):
    process_kernel.handle = handle
    with pytest.raises(OSError, match='snapshot failed'):
        vd._process_census(process_kernel)
    assert not process_kernel.closed


def test_process_census_exception_fails_closed(monkeypatch):
    monkeypatch.setattr(vd, '_process_census', lambda: (_ for _ in ()).throw(OSError('denied')))
    assert vd._closed() is False


def test_processentry32_layout_accounts_for_pointer_alignment():
    assert ctypes.sizeof(vd._PROCESSENTRY32W) == (568 if ctypes.sizeof(ctypes.c_void_p) == 8 else 556)
    assert vd._PROCESSENTRY32W.szExeFile.offset == (44 if ctypes.sizeof(ctypes.c_void_p) == 8 else 36)


def test_direct_apply_records_readbacks_and_rejects_success_without_change(setup):
    manager, native = setup
    original = copy.deepcopy(native.rows[-1])
    def ignored(count, row):
        if count == 2:
            row.update(copy.deepcopy(original))
    native.hook = ignored
    with pytest.raises(vd.VirtualDisplayError) as error:
        manager.apply(manager.prepare()['plan_id'])
    assert error.value.code == 'apply_mismatch'
    report = json.loads(manager.snapshot_path.read_text())
    after = next(item for item in report['readbacks'] if item['phase'] == 'after_apply')
    assert next(row for row in after['displays'] if row['id'] == 'v1') == original
    assert report['calls'][1]['flags'] == vd.CDS_UPDATEREGISTRY
    assert report['calls'][1]['result'] == 0
    assert report['rollback']['status'] == 'Restored'


def test_position_only_native_move_preserves_current_driver_private_data(native_wrapper):
    native, calls = native_wrapper
    active = buffer_for(mode(x=5120, height=512))
    active.mode.dmDriverExtra = 24
    active.private[0] = 77
    native._read = lambda source, index: active
    native._modes = lambda source: (_ for _ in ()).throw(AssertionError('position move must reuse current mode'))
    native.change('v1', mode(y=1440, height=512), vd.CDS_UPDATEREGISTRY)
    assert calls[0][1]['extra'] == 24 and active.private[0] == 77
    assert calls[0][1]['fields'] == vd.DM_POSITION


def test_resolution_change_uses_supported_advertised_mode(native_wrapper):
    native, calls = native_wrapper
    native._modes = lambda source: iter([buffer_for(mode(width=1920, height=512))])
    native.change('v1', mode(width=1920, height=512), vd.CDS_UPDATEREGISTRY)
    assert calls[0][1]['fields'] == vd.MODE_FIELDS
    assert calls[0][1]['mode']['width'] == 1920
