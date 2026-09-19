"""Capture tests use synthetic pixels and fake Win32 APIs, never the desktop."""
from contextlib import contextmanager
import copy
import ctypes
from io import BytesIO
from types import SimpleNamespace

import pytest
from webapp.backend import display_capture as capture


PROCESS = '4321:1234567890.25'
CLIENT = dict(x=-1920, y=0, width=3840, height=1080)
REGION = dict(x=-1900, y=20, width=32, height=24)
MONITORS = [dict(id='display-left', name='Left', primary=False, x=-1920, y=0, width=1920, height=1080),
            dict(id='display-main', name='Main', primary=True, x=0, y=0, width=1920, height=1080)]


class FakeNative:
    def __init__(self):
        self.state = dict(hwnd=12, name='DCS.exe', process_identity=PROCESS, client=CLIENT.copy(), minimized=False, visible=True)
        self.monitor_rows = copy.deepcopy(MONITORS)
        self.captured = []
        self.events = []
        self.mutate = lambda: None
        self.dpi_fail = False
        self.obscured = False

    @contextmanager
    def physical_pixels(self):
        self.events.append('dpi-enter')
        if self.dpi_fail:
            raise capture.CaptureError('dpi_unavailable', 'DPI unavailable')
        try:
            yield
        finally:
            self.events.append('dpi-restore')

    def monitors(self):
        self.events.append('monitors')
        return copy.deepcopy(self.monitor_rows)

    def foreground(self):
        self.events.append('foreground')
        return copy.deepcopy(self.state)

    def unobscured(self, hwnd, region):
        self.events.append('occlusion')
        if self.obscured:
            raise capture.CaptureError('window_obscured', 'Overlay covers capture')

    def grab_region(self, hwnd, region, client):
        assert self.events[-1] == 'foreground'
        self.events.append('capture')
        self.captured.append((hwnd, region.copy(), client.copy()))
        self.mutate()
        return b'\0\0\0\0' * region['width'] * region['height']


@pytest.fixture
def native(monkeypatch):
    fake = FakeNative()
    monkeypatch.setattr(capture, '_native', lambda: fake)
    monkeypatch.setattr(capture, '_encode_jpeg', lambda pixels, width, height: b'\xff\xd8synthetic-jpeg\xff\xd9')
    return fake


def test_success_captures_only_explicit_bounded_region_with_negative_desktop_origin(native):
    result = capture.capture_jpeg(REGION, PROCESS, CLIENT)
    assert result['jpeg'].startswith(b'\xff\xd8')
    assert result['width'] == 32 and result['height'] == 24 and result['process_identity'] == PROCESS
    assert result['captured_epoch'] > 0
    assert native.captured == [(12, REGION, CLIENT)]
    assert native.events[native.events.index('capture')+1] == 'foreground'
    assert native.events[-1] == 'dpi-restore'


def test_enumerate_monitors_reads_only_geometry_and_preserves_physical_values(native):
    assert capture.enumerate_monitors() == MONITORS
    assert native.events == ['dpi-enter', 'monitors', 'dpi-restore']
    assert native.captured == []


def test_foreground_geometry_helper_cannot_enable_or_capture(native):
    assert capture.foreground_dcs_window() == {'process_identity': PROCESS, **CLIENT}
    assert not native.captured


@pytest.mark.parametrize('region', [None, {}, {'x':0,'y':0,'width':10,'height':10,'path':'arbitrary'},
                                    dict(x=0,y=0,width=0,height=4), dict(x=0,y=0,width=-1,height=4),
                                    dict(x=0,y=0,width=1025,height=4), dict(x=0,y=0,width=4,height=1025),
                                    dict(x=0,y=0,width=True,height=4), dict(x=0.0,y=0,width=4,height=4),
                                    dict(x=float('nan'),y=0,width=4,height=4)])
def test_invalid_rectangles_fail_before_any_capture(native, region):
    with pytest.raises(capture.CaptureError) as error:
        capture.capture_jpeg(region, PROCESS, CLIENT)
    assert error.value.code == 'invalid_rectangle' and not native.captured


@pytest.mark.parametrize('identity', [None, '', '4321', '4321:nan', '4321:inf', '0:12.0', '4321:0', '4321:-2.0', '4321:12:13'])
def test_invalid_process_identities_never_capture(native, identity):
    with pytest.raises(capture.CaptureError) as error:
        capture.capture_jpeg(REGION, identity, CLIENT)
    assert error.value.code == 'invalid_process' and not native.captured


def test_outside_window_and_cross_monitor_regions_fail_without_clipping(native):
    with pytest.raises(capture.CaptureError) as error:
        capture.capture_jpeg(dict(x=-1950,y=20,width=32,height=24), PROCESS, CLIENT)
    assert error.value.code == 'outside_window'
    with pytest.raises(capture.CaptureError) as error:
        capture.capture_jpeg(dict(x=-10,y=20,width=32,height=24), PROCESS, CLIENT)
    assert error.value.code == 'outside_monitor' and not native.captured


@pytest.mark.parametrize('update,code', [({'name':'browser.exe'}, 'focus_lost'), ({'process_identity':'4322:1234567890.25'}, 'process_changed'),
                                      ({'process_identity':'4321:1234567891.25'}, 'process_changed'), ({'minimized':True}, 'window_minimized'),
                                      ({'visible':False}, 'window_minimized'), ({'client':{**CLIENT,'width':3839}}, 'window_changed')])
def test_wrong_foreground_process_minimize_or_resize_never_captures(native, update, code):
    native.state.update(update)
    with pytest.raises(capture.CaptureError) as error:
        capture.capture_jpeg(REGION, PROCESS, CLIENT)
    assert error.value.code == code and not native.captured


@pytest.mark.parametrize('update,code', [({'name':'browser.exe'}, 'focus_lost'), ({'process_identity':'4321:1234567891.25'}, 'process_changed'),
                                      ({'hwnd':13}, 'window_changed'), ({'minimized':True}, 'window_minimized'),
                                      ({'client':{**CLIENT,'x':-1919}}, 'window_changed')])
def test_focus_process_resize_changes_during_capture_discard_pixels(native, update, code, monkeypatch):
    encoded = []
    monkeypatch.setattr(capture, '_encode_jpeg', lambda *args: encoded.append(args))
    native.mutate = lambda: native.state.update(update)
    with pytest.raises(capture.CaptureError) as error:
        capture.capture_jpeg(REGION, PROCESS, CLIENT)
    assert error.value.code == code and len(native.captured) == 1 and not encoded


def test_focus_change_during_encoding_discards_encoded_frame(native, monkeypatch):
    def encode(*args):
        native.state['name'] = 'browser.exe'
        return b'never-delivered'
    monkeypatch.setattr(capture, '_encode_jpeg', encode)
    with pytest.raises(capture.CaptureError, match='Focus DCS'):
        capture.capture_jpeg(REGION, PROCESS, CLIENT)


def test_monitor_change_after_capture_discards_frame(native):
    native.mutate = lambda: native.monitor_rows[0].update(width=1910)
    with pytest.raises(capture.CaptureError) as error:
        capture.capture_jpeg(REGION, PROCESS, CLIENT)
    assert error.value.code == 'monitor_changed'


def test_dpi_fail_and_overlay_never_reach_composed_capture(native):
    native.dpi_fail = True
    with pytest.raises(capture.CaptureError) as error:
        capture.capture_jpeg(REGION, PROCESS, CLIENT)
    assert error.value.code == 'dpi_unavailable' and not native.captured
    native.dpi_fail, native.obscured = False, True
    with pytest.raises(capture.CaptureError) as error:
        capture.capture_jpeg(REGION, PROCESS, CLIENT)
    assert error.value.code == 'window_obscured' and not native.captured


def test_capture_os_failure_is_typed_and_has_no_retry(native):
    calls = []
    def fail(*args):
        calls.append(args)
        raise OSError('mock native failure')
    native.grab_region = fail
    with pytest.raises(capture.CaptureError) as error:
        capture.capture_jpeg(REGION, PROCESS, CLIENT)
    assert error.value.code == 'capture_failed' and len(calls) == 1
    assert error.value.as_dict() == {'code':'capture_failed', 'message':str(error.value)}


class DpiUser:
    def __init__(self, established=True, confirmed=True, restored=True):
        self.calls = []
        self.established, self.confirmed, self.restored = established, confirmed, restored
    def SetThreadDpiAwarenessContext(self, value):
        self.calls.append(value)
        return 99 if self.established and len(self.calls) == 1 else 1 if len(self.calls) > 1 and self.restored else 0
    def GetThreadDpiAwarenessContext(self):
        return 12
    def AreDpiAwarenessContextsEqual(self, actual, desired):
        return self.confirmed


def test_native_dpi_context_restored_even_after_failure():
    native = capture._Win32.__new__(capture._Win32)
    native.user = DpiUser()
    with pytest.raises(RuntimeError, match='fake capture failure'):
        with native.physical_pixels():
            raise RuntimeError('fake capture failure')
    assert len(native.user.calls) == 2 and native.user.calls[1] == 99


@pytest.mark.parametrize('settings', [dict(established=False), dict(confirmed=False), dict(restored=False)])
def test_native_dpi_unverified_or_restore_failure_never_returns_image(settings):
    native = capture._Win32.__new__(capture._Win32)
    native.user = DpiUser(**settings)
    with pytest.raises(capture.CaptureError) as error:
        with native.physical_pixels():
            pass
    assert error.value.code == 'dpi_unavailable'


class FakeGDI:
    def __init__(self, fail=None):
        self.fail, self.calls = fail, []
    def CreateCompatibleDC(self, source):
        self.calls.append(('memory', source))
        return 2
    def CreateCompatibleBitmap(self, source, width, height):
        self.calls.append(('bitmap', source, width, height))
        return 3
    def SelectObject(self, memory, bitmap):
        self.calls.append(('select', memory, bitmap))
        return 4 if bitmap == 3 else 3
    def BitBlt(self, *args):
        self.calls.append(('blit', *args))
        return self.fail != 'blit'
    def GetDIBits(self, source, bitmap, start, height, pixels, info, usage):
        self.calls.append(('read', source, bitmap))
        assert self.calls[-2] == ('select', 2, 4)  # Deselect before reading.
        header = info._obj.bmiHeader
        assert header.biHeight == -height and header.biBitCount == 32 and header.biCompression == 0
        return 0 if self.fail == 'read' else height
    def DeleteObject(self, value): self.calls.append(('delete-bitmap', value))
    def DeleteDC(self, value): self.calls.append(('delete-dc', value))


@pytest.mark.parametrize('fail', [None, 'blit', 'read'])
@pytest.mark.parametrize('region,client', [(REGION, CLIENT),
    (dict(x=120, y=220, width=32, height=24), dict(x=100, y=200, width=2560, height=1952))])
def test_native_bitmap_uses_composed_dc_and_exact_absolute_region(fail, region, client):
    native = capture._Win32.__new__(capture._Win32)
    dc_calls = []
    native.user = SimpleNamespace(GetDC=lambda hwnd: dc_calls.append(('get', hwnd)) or 1,
                                  ReleaseDC=lambda hwnd, dc: dc_calls.append(('release', hwnd, dc)))
    native.gdi = FakeGDI(fail)
    if fail:
        with pytest.raises(capture.CaptureError):
            native.grab_region(12, region, client)
    else:
        assert len(native.grab_region(12, region, client)) == 32*24*4
    # GetDC(hwnd) returned a stale DCS backing image in the actual GPU case.
    # The explicit composed source copies only the bounded absolute rectangle.
    assert dc_calls == [('get',None), ('release',None,1)]
    assert ('bitmap',1,32,24) in native.gdi.calls
    assert ('blit',2,0,0,32,24,1,region['x'],region['y'],0x00CC0020) in native.gdi.calls
    assert native.gdi.calls[-2:] == [('delete-dc',2), ('delete-bitmap',3)]


def test_unavailable_composed_dc_never_falls_back_to_stale_client_dc():
    native = capture._Win32.__new__(capture._Win32)
    dc_calls = []
    native.user = SimpleNamespace(GetDC=lambda hwnd: dc_calls.append(hwnd) or 0)
    native.gdi = FakeGDI()
    with pytest.raises(capture.CaptureError) as error:
        native.grab_region(12, REGION, CLIENT)
    assert error.value.code == 'capture_failed'
    assert dc_calls == [None] and not native.gdi.calls


@pytest.mark.parametrize('region,code', [
    (dict(x=-1921, y=20, width=32, height=24), 'outside_window'),
    (dict(x=-1900, y=20, width=1025, height=24), 'invalid_rectangle')])
def test_native_composed_copy_refuses_invalid_coverage_before_opening_dc(region, code):
    native = capture._Win32.__new__(capture._Win32)
    dc_calls = []
    native.user = SimpleNamespace(GetDC=lambda hwnd: dc_calls.append(hwnd) or 1)
    native.gdi = FakeGDI()
    with pytest.raises(capture.CaptureError) as error:
        native.grab_region(12, region, CLIENT)
    assert error.value.code == code and not dc_calls and not native.gdi.calls


def test_focus_switch_during_preflight_cannot_reach_capture(native):
    native.unobscured = lambda *args: native.state.update(name='browser.exe')
    with pytest.raises(capture.CaptureError) as error:
        capture.capture_jpeg(REGION, PROCESS, CLIENT)
    assert error.value.code == 'focus_lost' and not native.captured


def test_black_synthetic_image_encodes_without_guessing_sensor_state():
    Image = pytest.importorskip('PIL.Image')
    jpeg = capture._encode_jpeg(bytes(16*12*4), 16, 12)
    with Image.open(BytesIO(jpeg)) as decoded:
        assert decoded.size == (16,12) and decoded.format == 'JPEG'
        assert decoded.getextrema() == ((0,0),(0,0),(0,0))


def test_synthetic_colored_pixels_have_correct_orientation_and_channels():
    Image = pytest.importorskip('PIL.Image')
    # Top half blue, bottom half red in a top-down BGRX buffer.
    pixels = bytes((255,0,0,0))*32*16 + bytes((0,0,255,0))*32*16
    with Image.open(BytesIO(capture._encode_jpeg(pixels,32,32))) as decoded:
        assert decoded.getpixel((8,4))[2] > 240
        assert decoded.getpixel((8,24))[0] > 240


def test_partial_pixel_buffer_is_rejected():
    pytest.importorskip('PIL.Image')
    with pytest.raises(capture.CaptureError) as error:
        capture._encode_jpeg(b'partial', 20, 20)
    assert error.value.code == 'capture_failed'
