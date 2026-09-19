import json
import input_sender
from webapp.backend.sender import GuardedSender, _releasing_sender


def test_partial_original_emitter_failure_gets_keyup_cleanup_without_second_press(tmp_path, monkeypatch):
    emitted = []
    def emit(self, scancode, extended, keyup):
        emitted.append((scancode, extended, keyup))
        if len(emitted) == 2:
            raise OSError('simulated partial SendInput failure')
    monkeypatch.setattr(input_sender.KeySender, '_emit', emit)
    monkeypatch.setattr(input_sender, 'foreground_process', lambda: 'DCS.exe')
    index, audit = tmp_path/'index.json', tmp_path/'audit.log'
    name = 'UFC Keyboard Pushbutton - 1'
    index.write_text(json.dumps(dict(actions=[dict(name=name, combos=['LAlt+1'])], by_combo={'LAlt+1': [name]})))
    sender = GuardedSender(index, audit, dry_run=False, ensure_release=True)
    result = sender(dict(name=name, combo='LAlt+1'), dict(request_id='test'))
    assert not result['sent'] and result['ambiguous']
    assert len(emitted) == 4
    assert [x[2] for x in emitted] == [False, False, True, True]
    assert sender.instance.held == []


def test_dry_run_bounded_sender_never_emits(tmp_path, monkeypatch):
    monkeypatch.setattr(input_sender.KeySender, '_emit', lambda *args: (_ for _ in ()).throw(AssertionError('No real input')))
    index, audit = tmp_path/'index.json', tmp_path/'audit.log'
    name = 'UFC Keyboard Pushbutton - 1'
    index.write_text(json.dumps(dict(actions=[dict(name=name, combos=['LAlt+1'])], by_combo={'LAlt+1': [name]})))
    sender = GuardedSender(index, audit, dry_run=True, ensure_release=True)
    result = sender(dict(name=name, combo='LAlt+1'), dict(request_id='test'))
    assert result['ok'] and result['dry_run'] and not result['sent']


def test_cleanup_failure_is_not_reported_as_a_confirmed_release(tmp_path, monkeypatch):
    monkeypatch.setattr(input_sender.KeySender, '_emit', lambda *args: (_ for _ in ()).throw(OSError('OS failure')))
    monkeypatch.setattr(input_sender, 'foreground_process', lambda: 'DCS.exe')
    index, audit = tmp_path/'index.json', tmp_path/'audit.log'
    name = 'UFC Keyboard Pushbutton - 1'
    index.write_text(json.dumps(dict(actions=[dict(name=name, combos=['LAlt+1'])], by_combo={'LAlt+1': [name]})))
    sender = GuardedSender(index, audit, dry_run=False, ensure_release=True)
    result = sender(dict(name=name, combo='LAlt+1'), dict(request_id='test'))
    assert result['ambiguous'] and not result['sent']
    assert 'release could not be verified' in audit.read_text()
