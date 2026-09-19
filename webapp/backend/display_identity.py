"""Read Windows display-adapter identity; never infer a virtual screen from its name."""
import ctypes
from ctypes import wintypes
import os


class _DISPLAY_DEVICE(ctypes.Structure):
    _fields_ = [('cb', wintypes.DWORD), ('DeviceName', wintypes.WCHAR*32),
                ('DeviceString', wintypes.WCHAR*128), ('StateFlags', wintypes.DWORD),
                ('DeviceID', wintypes.WCHAR*128), ('DeviceKey', wintypes.WCHAR*128)]


def adapter_kind(device_id, hardware_ids):
    identities = {str(value).casefold() for value in hardware_ids}
    if r'root\mttvdd' in identities:
        return 'virtual'
    if str(device_id).casefold().startswith('pci\\'):
        return 'physical'
    return 'unknown'


def hardware_ids(device_id):
    import winreg
    if not device_id or len(device_id) > 256:
        return []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, 'SYSTEM\\CurrentControlSet\\Enum\\'+device_id) as key:
            value, _ = winreg.QueryValueEx(key, 'HardwareID')
            return [item for item in value[:16] if isinstance(item, str) and len(item) <= 256] if isinstance(value, list) else []
    except OSError:
        return []


def matching_hardware_id(device_key):
    import winreg
    prefix = '\\Registry\\Machine\\'
    if not str(device_key).casefold().startswith(prefix.casefold()):
        return ''
    path = device_key[len(prefix):]
    if not path.casefold().startswith('system\\currentcontrolset\\control\\video\\'):
        return ''
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            value, _ = winreg.QueryValueEx(key, 'MatchingDeviceId')
            return value if isinstance(value, str) and len(value) <= 256 else ''
    except OSError:
        return ''


def adapter_metadata(source_id):
    empty = {'kind': 'unknown', 'adapter_device_id': '', 'adapter_description': '', 'adapter_hardware_id': ''}
    if os.name != 'nt':
        return empty
    user = ctypes.WinDLL('user32', use_last_error=True)
    enumerate_device = user.EnumDisplayDevicesW
    enumerate_device.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(_DISPLAY_DEVICE), wintypes.DWORD]
    enumerate_device.restype = wintypes.BOOL
    for index in range(64):
        device = _DISPLAY_DEVICE()
        device.cb = ctypes.sizeof(device)
        if not enumerate_device(None, index, ctypes.byref(device), 0):
            break
        if device.DeviceName == source_id:
            matching = matching_hardware_id(device.DeviceKey)
            identities = [matching] if matching else hardware_ids(device.DeviceID)
            return {'kind': adapter_kind(device.DeviceID, identities), 'adapter_hardware_id': matching,
                    'adapter_device_id': device.DeviceID, 'adapter_description': device.DeviceString}
    return empty
