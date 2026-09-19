"""Bounded composed-desktop capture of an approved, unobscured DCS export.

The service must enable this only for an applied, server-owned export plan and
pass its rectangles here. This module is not a generic screenshot API. Coordinates
are physical desktop pixels; no display scaling or inferred rectangles are used.
Pre/post checks fence focus and geometry changes but are not an atomic OS lock.

Win32 references: SetThreadDpiAwarenessContext, GetDC, BitBlt and GetDIBits
(Microsoft Learn). A region-sized bitmap copies only its approved absolute
rectangle from the composed desktop DC, with SRCCOPY only (no CAPTUREBLT).
DCS's accelerated client DC can return an old backing image while its visible
exports are current. There is no alternate-source fallback, whole-desktop
bitmap, pixel file, or capture when the DCS ownership/visibility fences fail.
"""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from io import BytesIO
import math
import os
from pathlib import PureWindowsPath
import re
import time


MAX_DIMENSION = 1024
MAX_COORDINATE = 1_000_000
RECT_KEYS = {'x', 'y', 'width', 'height'}


class CaptureError(RuntimeError):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)

    def as_dict(self):
        return {'code': self.code, 'message': self.message}


def _rectangle(value, *, region=False):
    if not isinstance(value, dict) or set(value) != RECT_KEYS or any(type(v) is not int for v in value.values()):
        raise CaptureError('invalid_rectangle', 'Physical rectangles require exactly integer x, y, width and height.')
    result = dict(value)
    if (result['width'] <= 0 or result['height'] <= 0
            or any(abs(result[key]) > MAX_COORDINATE for key in RECT_KEYS)
            or region and (result['width'] > MAX_DIMENSION or result['height'] > MAX_DIMENSION)):
        raise CaptureError('invalid_rectangle', 'Capture regions must be positive and at most 1024 × 1024 physical pixels.')
    return result


def _contains(outer, inner):
    return (outer['x'] <= inner['x'] and outer['y'] <= inner['y']
            and inner['x']+inner['width'] <= outer['x']+outer['width']
            and inner['y']+inner['height'] <= outer['y']+outer['height'])


def _overlap(first, second):
    return (first['x'] < second['x']+second['width'] and second['x'] < first['x']+first['width']
            and first['y'] < second['y']+second['height'] and second['y'] < first['y']+first['height'])


def _process_identity(value):
    if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,9}:[0-9]+(?:\.[0-9]+)?', value):
        raise CaptureError('invalid_process', 'An exact DCS PID and process creation time are required.')
    pid, created = value.split(':')
    if int(pid) > 0xffffffff or not math.isfinite(float(created)) or float(created) <= 0:
        raise CaptureError('invalid_process', 'DCS process identity is invalid.')
    return value


def _monitor_for(rect, monitors):
    # Crossing a monitor edge or virtual-desktop gap is refused, not clipped.
    matches = [m for m in monitors if _contains(m, rect)]
    if not matches:
        raise CaptureError('outside_monitor', 'The export region must fit wholly inside one connected monitor.')
    return matches[0]


def _check_state(state, expected_process, expected_window, before=None):
    if state.get('name', '').casefold() != 'dcs.exe' or not state.get('hwnd'):
        raise CaptureError('focus_lost', 'Focus DCS. Display capture is unavailable while another application is foreground.')
    if state.get('process_identity') != expected_process:
        raise CaptureError('process_changed', 'The foreground DCS process differs from the approved process identity.')
    if state.get('minimized') or not state.get('visible'):
        raise CaptureError('window_minimized', 'DCS must be visible and not minimized.')
    if state.get('client') != expected_window or before and state.get('hwnd') != before.get('hwnd'):
        raise CaptureError('window_changed', 'The DCS client rectangle or window changed. Revalidate the export layout.')


def _encode_jpeg(pixels, width, height):
    try:
        from PIL import Image
    except ImportError as exc:
        raise CaptureError('dependency_unavailable', 'Pillow is required to encode DCS display images.') from exc
    if not isinstance(pixels, bytes) or len(pixels) != width*height*4:
        raise CaptureError('capture_failed', 'The bounded capture returned an incomplete pixel buffer.')
    try:
        # 32-bit top-down BGRX supplied by GDI. Alpha is not an opacity claim.
        with Image.frombytes('RGB', (width, height), pixels, 'raw', 'BGRX') as image:
            output = BytesIO()
            image.save(output, format='JPEG', quality=85, subsampling=0, optimize=False)
            return output.getvalue()
    except (OSError, ValueError, MemoryError) as exc:
        raise CaptureError('encoding_failed', 'The DCS display image could not be encoded.') from exc


def enumerate_monitors():
    """Active Windows monitor rectangles in physical pixels; no pixel capture."""
    native = _native()
    with native.physical_pixels():
        return native.monitors()


def foreground_dcs_window():
    """Read-only geometry helper for service validation, never enables capture."""
    native = _native()
    with native.physical_pixels():
        state = native.foreground()
        if state.get('name', '').casefold() != 'dcs.exe' or not state.get('visible') or state.get('minimized'):
            raise CaptureError('focus_lost', 'A visible DCS foreground window is required to validate its client rectangle.')
        return {'process_identity': state['process_identity'], **state['client']}


def capture_jpeg(rect, expected_process, expected_window):
    """Capture one server-approved rectangle, or raise CaptureError with no image.

    A successful copy establishes only that pixels were copied now. Black pixels
    can be a valid powered-off display or a GPU capture limitation. No black-frame
    rejection, aircraft-system assertion, rendered-frame freshness claim, cached
    fallback, arbitrary desktop rectangle, file output or input event is made here.
    """
    region, client = _rectangle(rect, region=True), _rectangle(expected_window)
    identity = _process_identity(expected_process)
    if not _contains(client, region):
        raise CaptureError('outside_window', 'The approved export region must fit wholly inside the DCS client area.')
    native = _native()
    with native.physical_pixels():
        monitor = _monitor_for(region, native.monitors())
        before = native.foreground()
        _check_state(before, identity, client)
        native.unobscured(before['hwnd'], region)
        # Last foreground/process/client read immediately before the bounded copy.
        immediate = native.foreground()
        _check_state(immediate, identity, client, before)
        try:
            pixels = native.grab_region(immediate['hwnd'], region, client)
        except CaptureError:
            raise
        except Exception as exc:
            raise CaptureError('capture_failed', 'DCS export-region capture failed. No alternate capture was attempted.') from exc
        captured = time.time()
        after = native.foreground()
        _check_state(after, identity, client, before)
        native.unobscured(after['hwnd'], region)
        current_monitor = _monitor_for(region, native.monitors())
        if current_monitor != monitor:
            raise CaptureError('monitor_changed', 'Monitor geometry changed during capture; the image was discarded.')
        jpeg = _encode_jpeg(pixels, region['width'], region['height'])
        # Encoding can take time; do not deliver a frame after a focus/resize switch.
        _check_state(native.foreground(), identity, client, before)
        return {'jpeg': jpeg, 'captured_epoch': captured, 'width': region['width'],
                'height': region['height'], 'process_identity': identity}


class _MONITORINFOEX(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.DWORD), ('rcMonitor', wintypes.RECT), ('rcWork', wintypes.RECT),
                ('dwFlags', wintypes.DWORD), ('szDevice', wintypes.WCHAR*32)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('biSize', wintypes.DWORD), ('biWidth', wintypes.LONG), ('biHeight', wintypes.LONG),
                ('biPlanes', wintypes.WORD), ('biBitCount', wintypes.WORD), ('biCompression', wintypes.DWORD),
                ('biSizeImage', wintypes.DWORD), ('biXPelsPerMeter', wintypes.LONG), ('biYPelsPerMeter', wintypes.LONG),
                ('biClrUsed', wintypes.DWORD), ('biClrImportant', wintypes.DWORD)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [('bmiHeader', _BITMAPINFOHEADER), ('bmiColors', wintypes.DWORD*1)]


def _rect_from_native(rect):
    return {'x': rect.left, 'y': rect.top, 'width': rect.right-rect.left, 'height': rect.bottom-rect.top}


class _Win32:
    def __init__(self):
        if os.name != 'nt':
            raise CaptureError('platform_unavailable', 'DCS display capture requires Windows.')
        try:
            self.user = ctypes.WinDLL('user32', use_last_error=True)
            self.gdi = ctypes.WinDLL('gdi32', use_last_error=True)
            self._declare()
        except (AttributeError, OSError) as exc:
            raise CaptureError('platform_unavailable', 'The required Windows capture APIs are unavailable.') from exc

    def _declare(self):
        P = ctypes.POINTER
        signatures = [
            (self.user, 'SetThreadDpiAwarenessContext', [ctypes.c_void_p], ctypes.c_void_p),
            (self.user, 'GetThreadDpiAwarenessContext', [], ctypes.c_void_p),
            (self.user, 'AreDpiAwarenessContextsEqual', [ctypes.c_void_p, ctypes.c_void_p], wintypes.BOOL),
            (self.user, 'GetForegroundWindow', [], wintypes.HWND),
            (self.user, 'GetWindowThreadProcessId', [wintypes.HWND, P(wintypes.DWORD)], wintypes.DWORD),
            (self.user, 'GetClientRect', [wintypes.HWND, P(wintypes.RECT)], wintypes.BOOL),
            (self.user, 'ClientToScreen', [wintypes.HWND, P(wintypes.POINT)], wintypes.BOOL),
            (self.user, 'IsIconic', [wintypes.HWND], wintypes.BOOL),
            (self.user, 'IsWindowVisible', [wintypes.HWND], wintypes.BOOL),
            (self.user, 'GetTopWindow', [wintypes.HWND], wintypes.HWND),
            (self.user, 'GetWindow', [wintypes.HWND, wintypes.UINT], wintypes.HWND),
            (self.user, 'GetWindowRect', [wintypes.HWND, P(wintypes.RECT)], wintypes.BOOL),
            (self.user, 'GetMonitorInfoW', [wintypes.HANDLE, P(_MONITORINFOEX)], wintypes.BOOL),
            (self.user, 'GetDC', [wintypes.HWND], wintypes.HDC),
            (self.user, 'ReleaseDC', [wintypes.HWND, wintypes.HDC], ctypes.c_int),
            (self.gdi, 'CreateCompatibleDC', [wintypes.HDC], wintypes.HDC),
            (self.gdi, 'CreateCompatibleBitmap', [wintypes.HDC, ctypes.c_int, ctypes.c_int], wintypes.HBITMAP),
            (self.gdi, 'SelectObject', [wintypes.HDC, wintypes.HANDLE], wintypes.HANDLE),
            (self.gdi, 'DeleteObject', [wintypes.HANDLE], wintypes.BOOL),
            (self.gdi, 'DeleteDC', [wintypes.HDC], wintypes.BOOL),
            (self.gdi, 'BitBlt', [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD], wintypes.BOOL),
            (self.gdi, 'GetDIBits', [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                                   ctypes.c_void_p, P(_BITMAPINFO), wintypes.UINT], ctypes.c_int),
        ]
        for library, name, args, result in signatures:
            function = getattr(library, name)
            function.argtypes, function.restype = args, result
        self.monitor_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC, P(wintypes.RECT), wintypes.LPARAM)
        self.user.EnumDisplayMonitors.argtypes = [wintypes.HDC, P(wintypes.RECT), self.monitor_callback, wintypes.LPARAM]
        self.user.EnumDisplayMonitors.restype = wintypes.BOOL

    @contextmanager
    def physical_pixels(self):
        # Thread scoped: do not mutate the backend's process-wide DPI policy.
        desired = ctypes.c_void_p(-4)  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        previous = self.user.SetThreadDpiAwarenessContext(desired)
        if not previous:
            raise CaptureError('dpi_unavailable', 'Physical-pixel DPI awareness could not be established.')
        try:
            if not self.user.AreDpiAwarenessContextsEqual(self.user.GetThreadDpiAwarenessContext(), desired):
                raise CaptureError('dpi_unavailable', 'Windows did not confirm per-monitor physical-pixel coordinates.')
            yield
        finally:
            if not self.user.SetThreadDpiAwarenessContext(previous):
                raise CaptureError('dpi_unavailable', 'The capture thread DPI context could not be restored.')

    def monitors(self):
        from .display_identity import adapter_metadata
        rows, errors = [], []
        @self.monitor_callback
        def visit(handle, hdc, rect, data):
            info = _MONITORINFOEX()
            info.cbSize = ctypes.sizeof(info)
            if not self.user.GetMonitorInfoW(handle, ctypes.byref(info)):
                errors.append(True)
                return False
            geometry = _rect_from_native(info.rcMonitor)
            if geometry['width'] <= 0 or geometry['height'] <= 0 or not info.szDevice:
                errors.append(True)
                return False
            rows.append({'id': info.szDevice, 'name': info.szDevice, 'primary': bool(info.dwFlags & 1),
                         **geometry, **adapter_metadata(info.szDevice)})
            return True
        if not self.user.EnumDisplayMonitors(None, None, visit, 0) or errors or not rows:
            raise CaptureError('monitor_unavailable', 'Connected monitor geometry could not be read reliably.')
        return sorted(rows, key=lambda row: (not row['primary'], row['id']))

    def foreground(self):
        import psutil
        hwnd = self.user.GetForegroundWindow()
        if not hwnd:
            raise CaptureError('focus_lost', 'No foreground DCS window is available.')
        pid = wintypes.DWORD()
        if not self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) or not pid.value:
            raise CaptureError('process_changed', 'Foreground process identity could not be read.')
        try:
            process = psutil.Process(pid.value)
            name, created, executable = process.name(), process.create_time(), process.exe()
            if not process.is_running() or PureWindowsPath(executable).name.casefold() != name.casefold():
                raise CaptureError('process_changed', 'Foreground process changed while checking its executable identity.')
        except (psutil.Error, OSError) as exc:
            raise CaptureError('process_changed', 'Foreground process identity is unavailable.') from exc
        rect, origin = wintypes.RECT(), wintypes.POINT(0, 0)
        if not self.user.GetClientRect(hwnd, ctypes.byref(rect)) or not self.user.ClientToScreen(hwnd, ctypes.byref(origin)):
            raise CaptureError('window_changed', 'The physical DCS client rectangle could not be read.')
        geometry = {'x': origin.x, 'y': origin.y, 'width': rect.right-rect.left, 'height': rect.bottom-rect.top}
        if self.user.GetForegroundWindow() != hwnd:
            raise CaptureError('focus_lost', 'Foreground focus changed while validating the DCS client area.')
        return {'hwnd': hwnd, 'name': name, 'process_identity': f'{pid.value}:{created}',
                'client': geometry, 'minimized': bool(self.user.IsIconic(hwnd)), 'visible': bool(self.user.IsWindowVisible(hwnd))}

    def unobscured(self, hwnd, region):
        # A foreground window can still have topmost overlays above it. Refuse
        # visible overlaps rather than assuming those pixels belong to DCS.
        current, visited = self.user.GetTopWindow(None), set()
        while current and current != hwnd and len(visited) < 1024:
            if current in visited:
                break
            visited.add(current)
            if self.user.IsWindowVisible(current) and not self.user.IsIconic(current):
                rect = wintypes.RECT()
                if not self.user.GetWindowRect(current, ctypes.byref(rect)):
                    raise CaptureError('window_obscured', 'An overlapping window could not be excluded from the export region.')
                if _overlap(_rect_from_native(rect), region):
                    raise CaptureError('window_obscured', 'Another visible window covers the export region. Move it before capturing.')
            current = self.user.GetWindow(current, 2)  # GW_HWNDNEXT
        if current != hwnd:
            raise CaptureError('window_changed', 'DCS window ordering changed during capture validation.')

    def grab_region(self, hwnd, region, client):
        # Physical absolute coordinates are essential: the source is the
        # composed desktop, while the approved DCS client remains its boundary.
        region, client = _rectangle(region, region=True), _rectangle(client)
        if not _contains(client, region):
            raise CaptureError('outside_window', 'The export region must remain within the approved DCS client area.')
        width, height = region['width'], region['height']
        source = memory = bitmap = old = None
        try:
            # Explicit source, not a fallback. All pre/post DCS ownership,
            # monitor and occlusion checks remain in capture_jpeg; only this
            # registered rectangle is copied into a rectangle-sized bitmap.
            source = self.user.GetDC(None)
            if not source:
                raise CaptureError('capture_failed', 'The composed export-region device context is unavailable.')
            memory = self.gdi.CreateCompatibleDC(source)
            bitmap = self.gdi.CreateCompatibleBitmap(source, width, height)
            if not memory or not bitmap:
                raise CaptureError('capture_failed', 'A bounded DCS pixel buffer could not be allocated.')
            old = self.gdi.SelectObject(memory, bitmap)
            if not old or old == ctypes.c_void_p(-1).value:
                old = None
                raise CaptureError('capture_failed', 'The bounded DCS pixel buffer could not be selected.')
            if not self.gdi.BitBlt(memory, 0, 0, width, height, source, region['x'], region['y'], 0x00CC0020):
                raise CaptureError('capture_failed', 'Windows could not copy the DCS export region; no alternate capture was attempted.')
            # GetDIBits requires the bitmap to be deselected from every DC.
            restored = self.gdi.SelectObject(memory, old)
            if not restored or restored == ctypes.c_void_p(-1).value:
                raise CaptureError('capture_failed', 'The DCS pixel buffer could not be prepared for encoding.')
            old = None
            info = _BITMAPINFO()
            info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
            info.bmiHeader.biWidth, info.bmiHeader.biHeight = width, -height
            info.bmiHeader.biPlanes, info.bmiHeader.biBitCount = 1, 32
            info.bmiHeader.biCompression = 0  # BI_RGB; top-down BGRX.
            pixels = ctypes.create_string_buffer(width*height*4)
            if self.gdi.GetDIBits(source, bitmap, 0, height, pixels, ctypes.byref(info), 0) != height:
                raise CaptureError('capture_failed', 'Windows returned an incomplete DCS export image.')
            return pixels.raw
        finally:
            if old and memory:
                self.gdi.SelectObject(memory, old)
            if memory:
                self.gdi.DeleteDC(memory)
            if bitmap:
                self.gdi.DeleteObject(bitmap)
            if source:
                self.user.ReleaseDC(None, source)


def _native():
    return _Win32()
