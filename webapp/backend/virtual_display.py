"""Plan and position one verified MttVDD monitor without editing physical displays.

No driver installation, DCS configuration, capture or input occurs here. The CLI
defaults to read-only planning. Apply is local orchestration only; callers must
not expose it as a phone-controlled arbitrary display-settings endpoint.

Win32 definitions: Microsoft Learn DEVMODEW, EnumDisplaySettingsExW and
ChangeDisplaySettingsExW. Those APIs use physical pixels independently of DPI.
The only mutation target is the exact enumerated Root\\MttVDD source. In
particular, no NULL/global device apply and no CDS_SET_PRIMARY are used.
"""
from __future__ import annotations

import argparse
import copy
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid

HARDWARE_ID = r"root\mttvdd"
CDS_UPDATEREGISTRY = 0x00000001
CDS_TEST = 0x00000002
CDS_NORESET = 0x10000000
DM_POSITION = 0x00000020
DM_DISPLAYORIENTATION = 0x00000080
DM_BITSPERPEL = 0x00040000
DM_PELSWIDTH = 0x00080000
DM_PELSHEIGHT = 0x00100000
DM_DISPLAYFLAGS = 0x00200000
DM_DISPLAYFREQUENCY = 0x00400000
MODE_FIELDS = DM_POSITION | DM_DISPLAYORIENTATION | DM_BITSPERPEL | DM_PELSWIDTH | DM_PELSHEIGHT | DM_DISPLAYFLAGS | DM_DISPLAYFREQUENCY
MODE_KEYS = ('x', 'y', 'width', 'height', 'frequency', 'bits_per_pixel', 'orientation', 'display_flags')
IDENTITY_KEYS = ('id', 'kind', 'adapter_device_id', 'adapter_hardware_id')
_LOCK = threading.RLock()


class VirtualDisplayError(RuntimeError):
    def __init__(self, code, message, *, details=None):
        self.code, self.message, self.details = code, message, details or {}
        super().__init__(message)

    def as_dict(self):
        return {'code': self.code, 'message': self.message, **self.details}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _identity(row):
    return {key: row.get(key) for key in IDENTITY_KEYS}


def _verified(row):
    return (row.get('kind') == 'virtual' and str(row.get('adapter_hardware_id', '')).casefold() == HARDWARE_ID
            and bool(row.get('adapter_device_id')) and bool(row.get('id')))


def _overlaps(a, b):
    return (a['x'] < b['x'] + b['width'] and b['x'] < a['x'] + a['width']
            and a['y'] < b['y'] + b['height'] and b['y'] < a['y'] + a['height'])


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [('dwSize', ctypes.c_uint32), ('cntUsage', ctypes.c_uint32),
                ('th32ProcessID', ctypes.c_uint32), ('th32DefaultHeapID', ctypes.c_size_t),
                ('th32ModuleID', ctypes.c_uint32), ('cntThreads', ctypes.c_uint32),
                ('th32ParentProcessID', ctypes.c_uint32), ('pcPriClassBase', ctypes.c_int32),
                ('dwFlags', ctypes.c_uint32), ('szExeFile', ctypes.c_uint16*260)]


def _process_census(kernel=None):
    """Complete Toolhelp process-name snapshot, no process handles or access probes.

    psutil can return an empty image name for Windows Secure System. Toolhelp
    supplies the OS process name independently; no special PID/name exemptions
    are used. A missing name, partial enumeration or API error remains blocked.
    Microsoft Learn: PROCESSENTRY32W and CreateToolhelp32Snapshot.
    """
    if kernel is None:
        if os.name != 'nt':
            raise OSError('Windows process census is unavailable.')
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        kernel.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        for name in ('Process32FirstW', 'Process32NextW'):
            function = getattr(kernel, name)
            function.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
            function.restype = ctypes.c_int
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.restype = ctypes.c_int
    handle = kernel.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS only.
    if handle in (None, 0, ctypes.c_void_p(-1).value):
        raise OSError('Windows process snapshot failed.')
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ctypes.set_last_error(0)
        available = kernel.Process32FirstW(handle, ctypes.byref(entry))
        rows = []
        while available:
            name = bytes(entry.szExeFile).decode('utf-16-le').split('\0', 1)[0]
            if not name.strip() or len(rows) >= 16384:
                raise OSError('Windows process names could not be read completely.')
            rows.append({'pid': int(entry.th32ProcessID), 'name': name})
            ctypes.set_last_error(0)
            available = kernel.Process32NextW(handle, ctypes.byref(entry))
        if not rows or ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
            raise OSError('Windows process enumeration was incomplete.')
        return rows
    finally:
        if not kernel.CloseHandle(handle):
            raise OSError('Windows process snapshot could not be closed.')


def _closed():
    try:
        rows = _process_census()
        return bool(rows) and all(isinstance(row.get('name'), str) and row['name'].strip()
                                  and row['name'].casefold() != 'dcs.exe' for row in rows)
    except (OSError, ValueError, AttributeError):
        return False


def _mode_valid(mode, *, inactive=False):
    return (isinstance(mode, dict) and set(mode) == set(MODE_KEYS)
            and all(type(mode[key]) is int for key in MODE_KEYS)
            and all(abs(mode[key]) <= 1_000_000 for key in MODE_KEYS)
            and (mode['width'] > 0 and mode['height'] > 0 or inactive and mode['width'] == mode['height'] == 0))


def _proof(rows, virtual_id):
    # Unknown adapters are protected too. Include registry modes so a pending
    # external configuration change cannot be silently folded into our action.
    return [copy.deepcopy(row) for row in rows if row['id'] != virtual_id]


def _check_result(code, step):
    if code != 0:
        descriptions = {1: 'Windows requires a restart', -1: 'the display driver rejected the change',
                        -2: 'the mode is unsupported', -3: 'Windows could not save the mode',
                        -4: 'invalid flags', -5: 'invalid display parameters', -6: 'DualView rejected the mode'}
        raise VirtualDisplayError('display_change_failed', f'{step}: {descriptions.get(code, "unknown Windows result")}.',
                                  details={'win32_result': code, 'step': step})


class VirtualDisplayManager:
    def __init__(self, snapshot_path=None, *, native=None, dcs_closed=None):
        self.snapshot_path = Path(snapshot_path) if snapshot_path is not None else Path(__file__).resolve().parents[1]/'runtime'/'virtual-display-position.json'
        self.native = native
        self.dcs_closed = dcs_closed or _closed

    def _api(self):
        if self.native is None:
            self.native = _Win32()
        return self.native

    def _rows(self):
        rows = copy.deepcopy(self._api().snapshot())
        if not rows or len(rows) > 64 or len({row.get('id') for row in rows}) != len(rows):
            raise VirtualDisplayError('display_unavailable', 'Display identities could not be enumerated uniquely.')
        for row in rows:
            if not row.get('id') or type(row.get('primary')) is not bool or type(row.get('active')) is not bool:
                raise VirtualDisplayError('display_unavailable', 'Display identity or state is incomplete.')
            if row['active'] and not _mode_valid(row.get('mode')):
                raise VirtualDisplayError('display_unavailable', 'An active display mode is unavailable.')
            if row.get('mode') is not None and not _mode_valid(row['mode'], inactive=not row['active']):
                raise VirtualDisplayError('display_unavailable', 'Display mode data is invalid.')
            if row.get('registry_mode') is not None and not _mode_valid(row['registry_mode'], inactive=True):
                raise VirtualDisplayError('display_unavailable', 'Saved display mode data is invalid.')
        return sorted(rows, key=lambda row: row['id'])

    def _build(self, rows):
        # Count claimed virtual adapters as well as exact hardware matches; an
        # incomplete second adapter identity must not be ignored.
        virtuals = [row for row in rows if row.get('kind') == 'virtual'
                    or str(row.get('adapter_hardware_id', '')).casefold() == HARDWARE_ID]
        if len(virtuals) != 1 or not _verified(virtuals[0]):
            raise VirtualDisplayError('virtual_identity', 'Exactly one identified Root\\MttVDD display source is required.')
        virtual = virtuals[0]
        primary = [row for row in rows if row['active'] and row['primary']]
        if len(primary) != 1 or primary[0].get('kind') != 'physical' or virtual['primary']:
            raise VirtualDisplayError('primary_identity', 'One physical primary display is required; the virtual display cannot be primary.')
        if virtual.get('registry_mode') is None:
            raise VirtualDisplayError('rollback_unavailable', 'The virtual display registry mode cannot be backed up.')
        p = primary[0]['mode']
        desired = dict(x=p['x'], y=p['y']+p['height'], width=p['width'], height=512,
                       frequency=60, bits_per_pixel=32, orientation=0, display_flags=0)
        if not _mode_valid(desired):
            raise VirtualDisplayError('invalid_geometry', 'The requested virtual geometry exceeds supported coordinates.')
        for row in rows:
            if row['id'] != virtual['id'] and row['active'] and _overlaps(desired, row['mode']):
                raise VirtualDisplayError('display_overlap', 'The export strip below the primary overlaps an existing display.')
        modes = self._api().modes(virtual['id'])
        signature = lambda m: tuple(m.get(k) for k in MODE_KEYS if k not in ('x', 'y'))
        if not any(_mode_valid(mode) and signature(mode) == signature(desired) for mode in modes):
            raise VirtualDisplayError('mode_unavailable', f'The virtual driver does not advertise {p["width"]} × 512 at 60 Hz, 32-bit, unrotated.')
        plan = {'schema': 1, 'virtual': _identity(virtual), 'current': copy.deepcopy(virtual), 'desired': desired,
                'before_physical': _proof(rows, virtual['id']),
                'status': 'Already configured' if virtual['active'] and virtual['mode'] == desired
                          and virtual['registry_mode'] == desired else 'Ready'}
        plan['plan_id'] = hashlib.sha256(_json(plan)).hexdigest()
        return plan

    def prepare(self):
        """Read-only plan. No file, driver or Windows settings writes."""
        with _LOCK:
            return self._build(self._rows())

    def _require_closed(self):
        try:
            closed = self.dcs_closed()
        except Exception as exc:
            raise VirtualDisplayError('dcs_state_unknown', 'DCS closure could not be verified.') from exc
        if closed is not True:
            raise VirtualDisplayError('dcs_running', 'Close DCS completely before changing virtual-display geometry.')

    def _save(self, value):
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.snapshot_path.with_name(self.snapshot_path.name+'.'+uuid.uuid4().hex+'.tmp')
        try:
            with temporary.open('xb') as handle:
                handle.write(_json(value))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.snapshot_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _guard(self, plan, *, before_change=False, record=None, phase=None):
        self._require_closed()
        rows = self._rows()
        if record is not None:
            record.setdefault('readbacks', []).append({'phase': phase, 'epoch': time.time(), 'displays': rows})
        virtuals = [row for row in rows if row.get('kind') == 'virtual'
                    or str(row.get('adapter_hardware_id', '')).casefold() == HARDWARE_ID]
        if len(virtuals) != 1 or not _verified(virtuals[0]) or _identity(virtuals[0]) != plan['virtual']:
            raise VirtualDisplayError('virtual_changed', 'The verified virtual-display identity changed.')
        if _proof(rows, plan['virtual']['id']) != plan['before_physical']:
            raise VirtualDisplayError('physical_changed', 'Another display changed; no further display mutations are permitted.')
        if virtuals[0]['primary']:
            raise VirtualDisplayError('primary_changed', 'The virtual display became primary; automatic changes are blocked.')
        if before_change and virtuals[0] != plan['current']:
            raise VirtualDisplayError('plan_changed', 'The virtual display changed after planning.')
        return virtuals[0]

    def _change(self, record, source, value, flags, step):
        result = self._api().change(source, value, flags)
        record.setdefault('calls', []).append({'step': step, 'source': source, 'value': copy.deepcopy(value),
                                               'flags': flags, 'result': result, 'epoch': time.time()})
        _check_result(result, step)

    def _rollback(self, plan, record):
        try:
            self._guard(plan, record=record, phase='before_rollback')
            previous = plan['current']
            restore = previous['mode'] if previous['active'] else {**previous['registry_mode'], 'width': 0, 'height': 0}
            source = plan['virtual']['id']
            self._change(record, source, restore, CDS_TEST, 'Test virtual rollback')
            self._guard(plan)
            self._change(record, source, restore, CDS_UPDATEREGISTRY, 'Apply virtual rollback')
            self._guard(plan, record=record, phase='after_rollback_apply')
            # The original saved mode may intentionally differ from its active mode.
            if previous['registry_mode'] != restore:
                self._change(record, source, previous['registry_mode'], CDS_UPDATEREGISTRY | CDS_NORESET, 'Restore saved virtual mode')
            after = self._guard(plan, record=record, phase='after_rollback')
            if after != previous:
                raise VirtualDisplayError('rollback_mismatch', 'Windows did not restore the original virtual display state.')
            return {'status': 'Restored'}
        except Exception as exc:
            return {'status': 'Blocked or failed', 'message': str(exc)}

    def apply(self, plan_id):
        """Apply a fresh read-only plan token; the requested rectangle is never client supplied."""
        with _LOCK:
            self._require_closed()
            plan = self.prepare()
            if not isinstance(plan_id, str) or plan_id != plan['plan_id']:
                raise VirtualDisplayError('plan_changed', 'Display setup changed. Prepare and review a new plan.')
            self._guard(plan, before_change=True)
            if plan['status'] == 'Already configured':
                return {'ok': True, 'status': 'Already configured', 'plan': plan, 'physical_unchanged': True}
            record = {'schema': 1, 'started_epoch': time.time(), 'status': 'Prepared', 'plan': plan}
            # A durable before-state is mandatory before the first registry write.
            self._save(record)
            source = plan['virtual']['id']
            mutated = False
            try:
                self._change(record, source, plan['desired'], CDS_TEST, 'Test virtual mode')
                self._guard(plan, before_change=True, record=record, phase='after_test')
                mutated = True  # Even a failure result may have updated registry state.
                # A direct named-device update is documented to apply and save
                # together. IDD returned success for NORESET on this Windows
                # build while exposing unchanged registry coordinates; never
                # rely on that staging result or issue a global NULL commit.
                self._change(record, source, plan['desired'], CDS_UPDATEREGISTRY, 'Apply virtual mode')
                after = self._guard(plan, record=record, phase='after_apply')
                if not after['active'] or after['mode'] != plan['desired'] or after['registry_mode'] != plan['desired']:
                    raise VirtualDisplayError('apply_mismatch', 'Windows did not apply the exact virtual mode.')
                record.update(status='Applied', finished_epoch=time.time(), after=after, physical_unchanged=True)
                self._save(record)
                return {'ok': True, 'status': 'Applied', 'plan': plan, 'after': after, 'physical_unchanged': True}
            except Exception as exc:
                rollback = self._rollback(plan, record) if mutated else {'status': 'Not needed'}
                record.update(status='Failed', finished_epoch=time.time(), error=str(exc), rollback=rollback)
                try:
                    self._save(record)
                except OSError:
                    pass  # The durable before-state still exists.
                error = exc if isinstance(exc, VirtualDisplayError) else VirtualDisplayError('apply_failed', str(exc))
                error.details['rollback'] = rollback
                raise error


class _POINTL(ctypes.Structure):
    _fields_ = [('x', ctypes.c_int32), ('y', ctypes.c_int32)]


class _DISPLAY_PART(ctypes.Structure):
    _fields_ = [('dmPosition', _POINTL), ('dmDisplayOrientation', ctypes.c_uint32), ('dmDisplayFixedOutput', ctypes.c_uint32)]


class _MODE_UNION(ctypes.Union):
    _anonymous_ = ('display',)
    _fields_ = [('display', _DISPLAY_PART), ('printer', ctypes.c_int16*8)]


class _DEVMODEW(ctypes.Structure):
    # uint16 arrays keep Windows WCHAR layout testable on non-Windows hosts too.
    _anonymous_ = ('mode',)
    _fields_ = [('dmDeviceName', ctypes.c_uint16*32), ('dmSpecVersion', ctypes.c_uint16),
                ('dmDriverVersion', ctypes.c_uint16), ('dmSize', ctypes.c_uint16), ('dmDriverExtra', ctypes.c_uint16),
                ('dmFields', ctypes.c_uint32), ('mode', _MODE_UNION), ('dmColor', ctypes.c_int16),
                ('dmDuplex', ctypes.c_int16), ('dmYResolution', ctypes.c_int16), ('dmTTOption', ctypes.c_int16),
                ('dmCollate', ctypes.c_int16), ('dmFormName', ctypes.c_uint16*32), ('dmLogPixels', ctypes.c_uint16),
                ('dmBitsPerPel', ctypes.c_uint32), ('dmPelsWidth', ctypes.c_uint32), ('dmPelsHeight', ctypes.c_uint32),
                ('dmDisplayFlags', ctypes.c_uint32), ('dmDisplayFrequency', ctypes.c_uint32),
                ('dmICMMethod', ctypes.c_uint32), ('dmICMIntent', ctypes.c_uint32), ('dmMediaType', ctypes.c_uint32),
                ('dmDitherType', ctypes.c_uint32), ('dmReserved1', ctypes.c_uint32), ('dmReserved2', ctypes.c_uint32),
                ('dmPanningWidth', ctypes.c_uint32), ('dmPanningHeight', ctypes.c_uint32)]


class _MODE_BUFFER(ctypes.Structure):
    _fields_ = [('mode', _DEVMODEW), ('private', ctypes.c_ubyte*4096)]


def _mode_dict(mode):
    return dict(x=mode.dmPosition.x, y=mode.dmPosition.y, width=mode.dmPelsWidth, height=mode.dmPelsHeight,
                frequency=mode.dmDisplayFrequency, bits_per_pixel=mode.dmBitsPerPel,
                orientation=mode.dmDisplayOrientation, display_flags=mode.dmDisplayFlags)


class _Win32:
    def __init__(self):
        if os.name != 'nt':
            raise VirtualDisplayError('platform_unavailable', 'Virtual display setup requires Windows.')
        from .display_identity import _DISPLAY_DEVICE
        self.device_type = _DISPLAY_DEVICE
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.user.EnumDisplayDevicesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(_DISPLAY_DEVICE), wintypes.DWORD]
        self.user.EnumDisplayDevicesW.restype = wintypes.BOOL
        self.user.EnumDisplaySettingsExW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(_DEVMODEW), wintypes.DWORD]
        self.user.EnumDisplaySettingsExW.restype = wintypes.BOOL
        self.user.ChangeDisplaySettingsExW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_DEVMODEW), wintypes.HWND, wintypes.DWORD, ctypes.c_void_p]
        self.user.ChangeDisplaySettingsExW.restype = ctypes.c_int32

    def _read(self, source, index):
        buffer = _MODE_BUFFER()
        buffer.mode.dmSize = ctypes.sizeof(_DEVMODEW)
        buffer.mode.dmDriverExtra = ctypes.sizeof(buffer.private)
        if not self.user.EnumDisplaySettingsExW(source, index, ctypes.byref(buffer.mode), 0):
            return None
        if buffer.mode.dmSize != ctypes.sizeof(_DEVMODEW) or buffer.mode.dmDriverExtra > ctypes.sizeof(buffer.private):
            raise VirtualDisplayError('native_mode_invalid', 'Windows returned an unsupported display mode structure.')
        return buffer

    def snapshot(self):
        from .display_capture import enumerate_monitors
        from .display_identity import adapter_metadata
        active = {row['id']: row for row in enumerate_monitors()}
        rows = []
        for index in range(64):
            device = self.device_type()
            device.cb = ctypes.sizeof(device)
            if not self.user.EnumDisplayDevicesW(None, index, ctypes.byref(device), 0):
                break
            if device.StateFlags & 8:  # DISPLAY_DEVICE_MIRRORING_DRIVER
                continue
            source = device.DeviceName
            attached = bool(device.StateFlags & 1)
            mode, registry = self._read(source, 0xffffffff), self._read(source, 0xfffffffe)
            value = _mode_dict(mode.mode) if mode is not None and attached else None
            if attached:
                if source not in active or value is None or any(active[source][k] != value[k] for k in ('x', 'y', 'width', 'height')):
                    raise VirtualDisplayError('display_race', 'Monitor geometry changed while reading display modes.')
                if bool(device.StateFlags & 4) != active[source]['primary']:
                    raise VirtualDisplayError('display_race', 'The primary display changed during enumeration.')
            rows.append({'id': source, **adapter_metadata(source), 'active': attached, 'primary': bool(device.StateFlags & 4),
                         'mode': value, 'registry_mode': _mode_dict(registry.mode) if registry is not None else None})
        else:
            raise VirtualDisplayError('display_unavailable', 'Display enumeration exceeded its bounded limit.')
        if set(active) != {row['id'] for row in rows if row['active']}:
            raise VirtualDisplayError('display_race', 'The active display list changed during enumeration.')
        return rows

    def _modes(self, source):
        for index in range(4096):
            mode = self._read(source, index)
            if mode is None:
                return
            yield mode
        raise VirtualDisplayError('mode_limit', 'Display-mode enumeration exceeded its bounded limit.')

    def modes(self, source):
        return [_mode_dict(buffer.mode) for buffer in self._modes(source)]

    def change(self, source, value, flags):
        from .display_identity import adapter_metadata
        # Defense in depth: no arbitrary physical target, even through this private API.
        if not source or not _verified({'id': source, **adapter_metadata(source)}):
            raise VirtualDisplayError('virtual_identity', 'Windows display mutations require exact Root\\MttVDD identity.')
        if flags not in (0, CDS_TEST, CDS_UPDATEREGISTRY, CDS_UPDATEREGISTRY | CDS_NORESET):
            raise VirtualDisplayError('invalid_flags', 'Only virtual test, direct update, saved-mode restore and named-device apply are supported.')
        if value is None:
            if flags != 0:
                raise VirtualDisplayError('invalid_flags', 'A named-device apply requires zero flags.')
            return int(self.user.ChangeDisplaySettingsExW(source, None, None, flags, None))
        if not _mode_valid(value, inactive=True):
            raise VirtualDisplayError('invalid_mode', 'Invalid virtual display mode.')
        signature = lambda m: tuple(m[k] for k in MODE_KEYS if k not in ('x', 'y'))
        current = self._read(source, 0xffffffff)
        position_only = current is not None and signature(_mode_dict(current.mode)) == signature(value)
        # For a pure move, preserve the active driver's private DEVMODE bytes.
        # An advertised timing can carry different private data from its active
        # instance; no resolution/frequency/orientation field needs changing.
        buffer = current if position_only else next((item for item in self._modes(source)
                            if signature(_mode_dict(item.mode)) == signature(value)), None)
        detached = value['width'] == value['height'] == 0
        if detached:
            buffer = self._read(source, 0xfffffffe)
        if buffer is None:
            raise VirtualDisplayError('mode_unavailable', 'The requested virtual mode is no longer advertised.')
        mode = buffer.mode
        mode.dmPosition.x, mode.dmPosition.y = value['x'], value['y']
        mode.dmPelsWidth, mode.dmPelsHeight = value['width'], value['height']
        mode.dmFields = DM_POSITION | DM_PELSWIDTH | DM_PELSHEIGHT if detached else DM_POSITION if position_only else MODE_FIELDS
        return int(self.user.ChangeDisplaySettingsExW(source, ctypes.byref(mode), None, flags, None))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('plan', 'apply'), nargs='?', default='plan')
    parser.add_argument('--plan-id')
    parser.add_argument('--snapshot', type=Path)
    args = parser.parse_args(argv)
    manager = VirtualDisplayManager(args.snapshot)
    try:
        result = manager.prepare() if args.command == 'plan' else manager.apply(args.plan_id)
        print(json.dumps(result, indent=2))
        return 0
    except (VirtualDisplayError, OSError) as exc:
        print(json.dumps({'ok': False, 'error': exc.as_dict() if isinstance(exc, VirtualDisplayError) else str(exc)}, indent=2))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
