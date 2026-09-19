"""Avoid repeated whole-log scans without caching binding-source fingerprints."""
import os
from pathlib import Path
from unittest.mock import patch

from webapp.backend.bindings import BindingService, atomic_json, fingerprint


ONE = 'Generic Stick {11111111-2222-3333-4444-555555555555}'
TWO = 'Generic Stick {22222222-2222-3333-4444-555555555555}'


def line(name=ONE):
    return f'INFO INPUT: created [Joystick] with full id [{name}]\n'


def service(tmp_path):
    result = BindingService(tmp_path/'data', tmp_path/'Install', tmp_path/'Saved')
    result.saved_games.joinpath('Logs').mkdir(parents=True)
    default = result.module_root/'keyboard'/'default.lua'
    default.parent.mkdir(parents=True)
    default.write_text('return {}')
    return result, result.saved_games/'Logs'/'dcs.log'


def test_unchanged_current_and_old_logs_read_once(tmp_path):
    bindings, log = service(tmp_path)
    old = log.with_name('dcs.log.old')
    log.write_text(line())
    old.write_text(line(TWO))
    reads = []
    read_bytes = Path.read_bytes
    def read(path):
        if path in (log, old): reads.append(path)
        return read_bytes(path)
    with patch.object(Path, 'read_bytes', read):
        assert bindings.last_seen_devices() == [ONE, TWO]
        for _ in range(4): assert bindings.last_seen_devices() == [ONE, TWO]
    assert reads == [log, old]


def test_append_new_device_invalidates_source_fingerprint(tmp_path):
    bindings, log = service(tmp_path)
    log.write_text(line())
    before = fingerprint(bindings.current_sources())
    with log.open('a') as output: output.write(line(TWO))
    assert fingerprint(bindings.current_sources()) != before
    assert bindings.last_seen_devices() == [ONE, TWO]


def test_unrelated_log_append_does_not_rehash_known_observations(tmp_path):
    bindings, log = service(tmp_path)
    log.write_text(line())
    assert bindings.last_seen_devices() == [ONE]
    original = dict(bindings._device_history[ONE])
    with log.open('a') as output: output.write('INFO flight message\n')
    with patch('webapp.backend.bindings.hashlib.sha256', side_effect=AssertionError('No new device, no log hash')):
        assert bindings.last_seen_devices() == [ONE]
    assert bindings._device_history[ONE] == original


def test_rotation_and_delete_recreate_detect_new_identity(tmp_path):
    bindings, log = service(tmp_path)
    log.write_text(line())
    assert bindings.last_seen_devices() == [ONE]
    log.replace(log.with_name('dcs.log.old'))
    log.write_text(line(TWO))
    assert bindings.last_seen_devices() == [ONE, TWO]
    log.unlink()
    bindings.last_seen_devices()
    assert 'dcs.log' not in bindings._log_census_cache
    log.write_text(line())
    bindings.last_seen_devices()
    assert 'dcs.log' in bindings._log_census_cache


def test_replacement_same_size_mtime_is_not_cached_as_old_file(tmp_path):
    bindings, log = service(tmp_path)
    log.write_text(line())
    original = log.stat()
    assert bindings.last_seen_devices() == [ONE]
    replacement = log.with_suffix('.replacement')
    replacement.write_text(line(TWO))
    os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
    os.replace(replacement, log)
    assert bindings.last_seen_devices() == [ONE, TWO]


def test_append_during_read_does_not_cache_old_content_under_new_stat(tmp_path):
    bindings, log = service(tmp_path)
    log.write_text(line())
    read_bytes = Path.read_bytes
    changed = False
    def read(path):
        nonlocal changed
        data = read_bytes(path)
        if path == log and not changed:
            changed = True
            with log.open('a') as output: output.write(line(TWO))
        return data
    with patch.object(Path, 'read_bytes', read):
        assert bindings.last_seen_devices() == [ONE]
        assert 'dcs.log' not in bindings._log_census_cache
        assert bindings.last_seen_devices() == [ONE, TWO]


def test_restored_history_survives_rotation_and_new_device_still_invalidates(tmp_path):
    bindings, log = service(tmp_path)
    log.write_text(line())
    sources = bindings.current_sources()
    atomic_json(bindings.cache_path, {'schema':'dcs-companion/bindings/1',
        'aircraft':bindings.aircraft, 'setup_identity':bindings.setup_identity,
        'sources':sources, 'source_fingerprint':fingerprint(sources),
        'observed_devices':list(bindings._device_history.values())})
    log.write_text('INFO new log without INPUT census\n')
    restored = BindingService(tmp_path/'data', bindings.install_root, bindings.saved_games)
    assert restored.last_seen_devices() == [ONE]
    before = fingerprint(restored.current_sources())
    with log.open('a') as output: output.write(line(TWO))
    assert fingerprint(restored.current_sources()) != before


def test_full_binding_sha_checks_are_not_stat_cached(tmp_path):
    bindings, log = service(tmp_path)
    log.write_text(line())
    default = bindings.module_root/'keyboard'/'default.lua'
    before = fingerprint(bindings.current_sources())
    stat = default.stat()
    default.write_text('return []')  # Equal length, different contents.
    os.utime(default, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert fingerprint(bindings.current_sources()) != before
