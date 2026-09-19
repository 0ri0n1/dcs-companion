import copy
from datetime import datetime, timezone
import threading
import time

import pytest

from webapp.backend.live_mission import (HOOK_QUERY, MISSION_QUERY, LogLifecycle, MissionReader,
                                         read_bridge_metadata)


START = datetime(2026, 9, 18, 20, 50, tzinfo=timezone.utc).timestamp()
IDENTITY = f'123:{START}'


def line(message, second=10, logger='Dispatcher'):
    return f'2026-09-18 20:50:{second:02d}.000 INFO    {logger} (Main): {message}\n'


def lifecycle(name='Known mission.miz'):
    return (line('loadMission C:\\Missions\\' + name) +
            line('Terrain theatre PersianGulf', 11) +
            line('loadMission Done: Control passed to the player', 12))


def health():
    return {'dcs_running': True, 'process_identity': IDENTITY, 'session': 'session1',
            'status': 'Live', 'aircraft': 'F-22A', 'telemetry_fresh': True}


def result(name='Bridge mission'):
    return {'name': name, 'theatre': 'PersianGulf', 'source': 'Fixture metadata bridge', 'detail': 'Read-only fixture'}


def finish(reader):
    if reader.worker:
        reader.worker.join(2)
        assert not reader.worker.is_alive()


def test_log_requires_complete_lifecycle_and_current_process(tmp_path):
    path = tmp_path/'dcs.log'
    path.write_text(line('loadMission C:\\Missions\\Known mission.miz'))
    reader = LogLifecycle(path)
    assert reader.read(IDENTITY) is None
    with path.open('a') as handle:
        handle.write(line('Terrain theatre PersianGulf', 11) + line('loadMission Done: Control passed to the player', 12))
    snapshot = reader.read(IDENTITY)
    assert snapshot['name'] == 'Known mission'
    assert snapshot['theatre'] == 'PersianGulf'
    assert 'filename' in snapshot['source']
    assert reader.read(f'456:{START+60}') is None


def test_editor_load_unrelated_miz_text_and_missing_handoff_are_not_active(tmp_path):
    path = tmp_path/'dcs.log'
    path.write_text(line('load terrain from PersianGulf') + line('recent mission C:\\Missions\\Old.miz') +
                    line('loadMission C:\\Missions\\Loading.miz'))
    assert LogLifecycle(path).read(IDENTITY) is None


@pytest.mark.parametrize('ending,logger', [('stopMission', 'Dispatcher'),
                                         ('(dDispatcher)enterToState_:3', 'EDCORE'),
                                         ('loadMission C:\\Missions\\Next.miz', 'Dispatcher')])
def test_log_end_or_new_load_invalidates_old_name(tmp_path, ending, logger):
    path = tmp_path/'dcs.log'
    path.write_text(lifecycle())
    reader = LogLifecycle(path)
    assert reader.read(IDENTITY)['name'] == 'Known mission'
    with path.open('a') as handle: handle.write(line(ending, 20, logger))
    assert reader.read(IDENTITY) is None


def test_log_rotation_drops_previous_filename(tmp_path):
    path = tmp_path/'dcs.log'
    path.write_text(lifecycle())
    reader = LogLifecycle(path)
    assert reader.read(IDENTITY)
    path.write_text('New log with no lifecycle\n')
    assert reader.read(IDENTITY) is None


def test_metadata_snapshot_is_nonblocking_and_old_worker_cannot_cross_session(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def bridge():
        entered.set()
        release.wait(1)
        return result('Old mission')
    reader = MissionReader(tmp_path/'missing.log', bridge=bridge)
    h = health()
    before = time.monotonic()
    assert reader.snapshot(h, {})['name'] is None
    assert time.monotonic()-before < .1
    assert entered.wait(.5)
    h['session'] = 'session2'
    assert reader.snapshot(h, {})['name'] is None
    release.set()
    finish(reader)
    assert reader.result['name'] is None
    reader.close()


@pytest.mark.parametrize('change', [{'dcs_running':False}, {'status':'DCS menus/no aircraft'},
                                  {'process_identity':None}, {'session':None}])
def test_menus_offline_and_missing_identity_clear_name_immediately(tmp_path, change):
    reader = MissionReader(tmp_path/'missing.log', bridge=lambda: result())
    h = health()
    reader.snapshot(h, {})
    finish(reader)
    assert reader.snapshot(h, {})['name'] == 'Bridge mission'
    h.update(change)
    assert reader.snapshot(h, {})['name'] is None
    reader.close()


def test_bridge_failure_uses_only_confirmed_current_log_lifecycle(tmp_path):
    path = tmp_path/'dcs.log'
    path.write_text(lifecycle())
    def unavailable(): raise OSError('Private error content must not enter the response')
    reader = MissionReader(path, bridge=unavailable)
    reader.snapshot(health(), {})
    finish(reader)
    snap = reader.snapshot(health(), {})
    assert snap['name'] == 'Known mission'
    assert 'filename' in snap['source']
    assert 'Private' not in str(snap)
    reader.close()


def test_unavailable_sources_have_no_cached_filename_fallback(tmp_path):
    def unavailable(): raise OSError('offline')
    reader = MissionReader(tmp_path/'missing.log', bridge=unavailable)
    reader.snapshot(health(), {'last_mission_path':'C:\\Missions\\Old.miz'})
    finish(reader)
    assert reader.snapshot(health(), {})['status'] == 'Unavailable'
    assert reader.result['name'] is None
    reader.close()


def test_refresh_is_rate_limited_and_stale_result_is_hidden(tmp_path):
    now, calls = [0.], []
    reader = MissionReader(tmp_path/'missing.log', clock=lambda:now[0], bridge=lambda:(calls.append(1) or result()))
    reader.snapshot(health(), {})
    finish(reader)
    now[0] = 1.9
    assert reader.snapshot(health(), {})['name'] == 'Bridge mission'
    assert len(calls) == 1
    now[0] = 2.1
    reader.snapshot(health(), {})
    finish(reader)
    assert len(calls) == 2
    now[0], reader.next_check = 9, 20
    stale = reader.snapshot(health(), {})
    assert stale['status'] == 'Stale' and stale['name'] is None and stale['theatre'] is None
    reader.close()


def test_fresh_explicit_collector_metadata_needs_no_bridge(tmp_path):
    def forbidden(): pytest.fail('Unexpected bridge query')
    reader = MissionReader(tmp_path/'missing.log', bridge=forbidden)
    snapshot = reader.snapshot(health(), {'mission':{'title':'Collector title','theatre':'Caucasus'}})
    assert snapshot['name'] == 'Collector title'
    assert snapshot['source'] == 'Current collector mission metadata'
    reader.close()


def test_paused_or_stale_ownship_does_not_erase_fresh_bridge_metadata(tmp_path):
    reader = MissionReader(tmp_path/'missing.log', bridge=lambda:result())
    h = health()
    h.update(status='Stale telemetry',telemetry_fresh=False,model_advancing=False)
    reader.snapshot(h,{})
    finish(reader)
    assert reader.snapshot(h,{})['name'] == 'Bridge mission'
    reader.close()


def test_bridge_uses_only_fixed_metadata_requests_and_hides_dictionary_keys(tmp_path, monkeypatch):
    path = tmp_path/'hook.lua'
    path.write_text("FIDDLE.PORT = 12080\nFIDDLE.AUTH = true\nFIDDLE.USERNAME = 'fixture'\nFIDDLE.PASSWORD = 'fixture-secret'\n")
    calls = []
    def request(port, code, authorization):
        calls.append((port,code,authorization))
        return {'name':'Actual DCS name','model_time':10} if port==12081 else {
            'name':'DictKey_sortie_5','theatre':'PersianGulf','model_time':10.1}
    monkeypatch.setattr('webapp.backend.live_mission._fiddle_request',request)
    snapshot = read_bridge_metadata(path)
    assert snapshot['name'] == 'Actual DCS name'
    assert snapshot['theatre'] == 'PersianGulf'
    assert [(p,c) for p,c,_ in calls] == [(12081,HOOK_QUERY),(12080,MISSION_QUERY)]
    assert all(auth.startswith('Basic ') for _,_,auth in calls)
    assert 'fixture-secret' not in str(snapshot)
    assert not any(word in HOOK_QUERY+MISSION_QUERY for word in ('getAllUnits','getWorldObjects','getGroups','spawn','setPause','load_mission'))


def test_mismatched_bridge_model_times_do_not_mix_missions(tmp_path, monkeypatch):
    path = tmp_path/'hook.lua'
    path.write_text('FIDDLE.PORT = 12080\nFIDDLE.AUTH = false\n')
    monkeypatch.setattr('webapp.backend.live_mission._fiddle_request',lambda port,*_: {
        'name':'Hooks name','model_time':100} if port==12081 else {'name':'New mission','model_time':1,'theatre':'Caucasus'})
    snapshot = read_bridge_metadata(path)
    assert snapshot['name'] == 'Hooks name'
    assert snapshot['theatre'] is None


def test_bridge_failure_discards_previously_known_name(tmp_path):
    now, fails = [0.], [False]
    def bridge():
        if fails[0]: raise OSError('offline')
        return result()
    reader = MissionReader(tmp_path/'missing.log', bridge=bridge, clock=lambda:now[0])
    reader.snapshot(health(), {})
    finish(reader)
    assert reader.result['name'] == 'Bridge mission'
    now[0], fails[0] = 3, True
    reader.snapshot(health(), {})
    finish(reader)
    assert reader.snapshot(health(), {})['name'] is None
    reader.close()
