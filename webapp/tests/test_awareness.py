import copy
import json
from pathlib import Path
import threading
import time

import pytest

from webapp.backend.awareness import (AwarenessReader, HOOK_QUERY, MISSION_QUERY,
                                      _context, read_bridge_awareness, validate_response)
from webapp.tests.lua_support import run_lua


def health():
    return {'dcs_running': True, 'telemetry_fresh': True, 'model_advancing': True, 'cockpit': True,
            'process_identity': '12:1000', 'session': 'one', 'aircraft': 'F-22A', 'telemetry_age_s': .2}


def raw():
    return {'stream': {'live': True, 'exporter_connected': True},
            'aircraft': {'aircraft': 'F-22A', 'unit_name': 'Player 1', 'coalition': 2,
                         'latitude': 24, 'longitude': 54, 'model_time_s': 100}}


def payload():
    metadata = {'multiplayer': False, 'paused': False, 'name': 'Mission', 'model_time': 100, 'aircraft': 'F-22A'}
    return {'before': metadata.copy(), 'after': metadata.copy(),
            'mission': {'ownship': {'id': '1', 'name': 'Player 1', 'type': 'F-22A', 'coalition': 2, 'lat': 24, 'lon': 54},
                        'model_time': 100, 'theatre': 'PersianGulf', 'total_count': 1,
                        'omitted_count': 0, 'truncated': False, 'contacts': [
                            {'id': '2', 'name': 'Enemy', 'type': 'MiG-29A', 'category': 'airplane',
                             'coalition': 1, 'lat': 24.1, 'lon': 54.1, 'altitude_ft': 15000, 'speed_kt': 400}]}}


def finish(reader):
    if reader.worker:
        reader.worker.join(2)
        assert not reader.worker.is_alive()


def available(reader, h=None, r=None):
    h, r = h or health(), r or raw()
    reader.snapshot(h, r)
    finish(reader)
    return reader.snapshot(h, r)


def test_valid_contacts_are_explicitly_mission_awareness_and_detached():
    p = payload()
    result = validate_response(p, _context(health(), raw()))
    assert result['status'] == 'Available'
    assert result['mode'] == 'mission-awareness'
    assert result['single_player'] and result['model_advancing']
    assert result['ownship_unit'] == 'Player 1'
    assert result['contacts'][0]['coalition'] == 'red'
    assert 'not aircraft detections' in result['detail']
    p['mission']['contacts'][0]['lat'] = 0
    assert result['contacts'][0]['lat'] == 24.1


@pytest.mark.parametrize('field,value', [('multiplayer', True), ('multiplayer', None), ('multiplayer', 0),
                                       ('paused', True), ('paused', None), ('aircraft', 'FA-18C_hornet'),
                                       ('name', 'Other mission'), ('model_time', 50), ('model_time', 106)])
def test_counterpart_hook_fences_fail_closed(field, value):
    p = payload()
    p['after'][field] = value
    assert validate_response(p, _context(health(), raw()))['contacts'] == []


@pytest.mark.parametrize('field,value', [('name', None), ('type', 'FA-18C_hornet'),
                                       ('coalition', 1), ('coalition', True), ('id', None)])
def test_live_player_must_match_current_exporter(field, value):
    p = payload()
    p['mission']['ownship'][field] = value
    assert validate_response(p, _context(health(), raw()))['status'] != 'Available'


@pytest.mark.parametrize('field,value', [('lat', 91), ('lon', float('nan')), ('lat', True),
                                       ('coalition', 2), ('coalition', True), ('category', 'weapon'),
                                       ('id', ''), ('altitude_ft', float('inf'))])
def test_bad_contact_is_never_presented_as_valid(field, value):
    p = payload()
    p['mission']['contacts'][0][field] = value
    assert validate_response(p, _context(health(), raw()))['contacts'] == []


def test_duplicates_bad_counts_and_mismatched_mission_time_rejected():
    for edit in ('duplicate', 'total', 'time', 'truncated'):
        p = payload()
        m = p['mission']
        if edit == 'duplicate':
            m['contacts'] *= 2
            m['total_count'] = 2
        elif edit == 'total': m['total_count'] = 100
        elif edit == 'time': m['model_time'] = 90
        else: m['truncated'] = True
        assert validate_response(p, _context(health(), raw()))['status'] != 'Available'


def test_zero_units_is_valid_and_distinct_from_unavailable():
    p = payload()
    p['mission'].update(contacts={}, total_count=0)
    result = validate_response(p, _context(health(), raw()))
    assert result['status'] == 'Available' and result['total_count'] == 0 and result['contacts'] == []


def test_cap_and_coordinate_omissions_are_explicit():
    p = payload()
    row = p['mission']['contacts'][0]
    p['mission'].update(contacts=[dict(row, id=str(i+100)) for i in range(1000)], total_count=1002,
                        omitted_count=1, truncated=True)
    result = validate_response(p, _context(health(), raw()))
    assert result['status'] == 'Available'
    assert result['truncated'] and result['omitted_count'] == 1 and result['total_count'] == 1002


@pytest.mark.parametrize('change', [{'model_advancing': False}, {'telemetry_fresh': False},
                                  {'telemetry_age_s': 4}, {'dcs_running': False}, {'cockpit': False},
                                  {'process_identity': None}, {'session': None}])
def test_ineligible_health_never_calls_bridge_and_clears_contacts(change):
    calls = []
    reader = AwarenessReader(request=lambda: calls.append(1) or payload())
    assert available(reader)['status'] == 'Available'
    h = health()
    h.update(change)
    assert reader.snapshot(h, raw())['contacts'] == []
    assert len(calls) == 1
    reader.close()


@pytest.mark.parametrize('r', [None, {}, {'aircraft': None}, {'aircraft': []}, {'stream': 'bad'},
                              dict(raw(), aircraft=dict(raw()['aircraft'], unit_name=None)),
                              dict(raw(), aircraft=dict(raw()['aircraft'], coalition=True))])
def test_cold_or_malformed_collector_never_calls_bridge(r):
    reader = AwarenessReader(request=lambda: pytest.fail('must not query'))
    assert reader.snapshot(health(), r)['contacts'] == []
    reader.close()


def test_disabled_and_closed_reader_never_query():
    reader = AwarenessReader(enabled=False, request=lambda: pytest.fail('must not query'))
    assert reader.snapshot(health(), raw())['status'] == 'Disabled'
    reader.close()


def test_disable_inflight_then_reenable_requires_a_new_read():
    entered, release = threading.Event(), threading.Event()
    calls = []
    def request():
        calls.append(1)
        entered.set()
        release.wait(1)
        return payload()
    reader = AwarenessReader(request=request)
    reader.snapshot(health(), raw())
    assert entered.wait(.5)
    reader.enabled = False
    assert reader.snapshot(health(), raw())['status'] == 'Disabled'
    release.set()
    finish(reader)
    assert reader.result['contacts'] == []
    reader.enabled = True
    assert reader.snapshot(health(), raw())['contacts'] == []
    finish(reader)
    assert reader.snapshot(health(), raw())['status'] == 'Available'
    assert len(calls) == 2
    reader.close()


def test_worker_is_nonblocking_and_cannot_publish_across_session_or_close():
    entered, release = threading.Event(), threading.Event()
    def request():
        entered.set()
        release.wait(1)
        return payload()
    reader = AwarenessReader(request=request)
    before = time.monotonic()
    assert reader.snapshot(health(), raw())['contacts'] == []
    assert time.monotonic()-before < .1
    assert entered.wait(.5)
    changed = dict(health(), session='next')
    assert reader.snapshot(changed, raw())['contacts'] == []
    reader.close()
    release.set()
    finish(reader)
    assert reader.result['contacts'] == []


def test_request_latency_age_and_model_reset_expire_without_ghosts():
    now = [10.]
    def request():
        now[0] += 1
        return payload()
    reader = AwarenessReader(clock=lambda: now[0], request=request)
    result = available(reader)
    assert result['age_s'] == 1 and result['contacts'][0]['age_s'] == 1
    reset = raw()
    reset['aircraft']['model_time_s'] = 10
    assert reader.snapshot(health(), reset)['contacts'] == []
    reader.next_check = 100
    now[0] = 16
    assert reader.snapshot(health(), raw())['status'] == 'Stale'
    reader.close()


def test_current_theatre_disagreement_hides_positions():
    reader = AwarenessReader(request=payload)
    assert available(reader)['status'] == 'Available'
    assert reader.snapshot(health(), raw(), {'status': 'Current', 'theatre': 'Caucasus'})['contacts'] == []
    reader.close()


def test_exception_does_not_leak_credentials_or_previous_contacts():
    def request(): raise OSError('Basic PRIVATE credential')
    reader = AwarenessReader(request=request)
    snap = available(reader)
    assert snap['contacts'] == [] and 'PRIVATE' not in str(snap)
    reader.close()


def test_fixed_request_stays_on_existing_loopback_ports(monkeypatch, tmp_path):
    hook = tmp_path/'hook.lua'
    hook.write_text('FIDDLE.PORT = 12080\nFIDDLE.AUTH = false\n')
    calls = []
    p = payload()
    def request(port, code, auth, **kwargs):
        calls.append((port, code, auth, kwargs))
        return p['mission'] if port == 12080 else p['before']
    monkeypatch.setattr('webapp.backend.awareness._fiddle_request', request)
    assert read_bridge_awareness(hook) == p
    assert calls == [(12081, HOOK_QUERY, None, {}),
                     (12080, MISSION_QUERY, None, {'max_response': 2*1024*1024}),
                     (12081, HOOK_QUERY, None, {})]


@pytest.mark.parametrize('field,value', [('multiplayer', True), ('multiplayer', None), ('multiplayer', 0),
                                       ('paused', True), ('paused', None), ('name', None),
                                       ('aircraft', None), ('model_time', -1)])
def test_preflight_mp_unknown_paused_or_missing_identity_never_sends_enemy_query(monkeypatch, tmp_path, field, value):
    hook = tmp_path/'hook.lua'
    hook.write_text('FIDDLE.PORT = 12080\nFIDDLE.AUTH = false\n')
    calls = []
    before = payload()['before']
    before[field] = value
    def request(port, code, auth, **kwargs):
        calls.append(port)
        assert port == 12081
        return before
    monkeypatch.setattr('webapp.backend.awareness._fiddle_request', request)
    result = read_bridge_awareness(hook)
    assert calls == [12081] and 'mission' not in result


@pytest.mark.parametrize('field,value', [('multiplayer', True), ('multiplayer', None), ('paused', True),
                                       ('name', 'Next'), ('aircraft', 'FA-18C_hornet'), ('model_time', 10)])
def test_postflight_transition_discards_returned_contacts(monkeypatch, tmp_path, field, value):
    hook = tmp_path/'hook.lua'
    hook.write_text('FIDDLE.PORT = 12080\nFIDDLE.AUTH = false\n')
    p = payload()
    p['after'][field] = value
    replies = iter([p['before'], p['mission'], p['after']])
    monkeypatch.setattr('webapp.backend.awareness._fiddle_request', lambda *a, **k: next(replies))
    result = read_bridge_awareness(hook)
    assert 'mission' not in result
    assert validate_response(result, _context(health(), raw()))['contacts'] == []


def test_actual_mission_lua_liveness_enemy_individual_units_categories_and_units(tmp_path):
    program = r'''
local function unit(id,side,active,life,exists)
 return {isExist=function() return exists end,isActive=function() return active end,getLife=function() return life end,
 getID=function() return id end,getName=function() return "Unit "..id end,getTypeName=function() return "Type" end,
 getCoalition=function() return side end,getPoint=function() return {x=1,y=3048,z=2} end,
 getVelocity=function() return {x=1852/3600,y=100,z=0} end}
end
local player=unit("1",2,true,100,true)
world={getPlayer=function() return player end}
Group={Category={AIRPLANE=0,HELICOPTER=1,GROUND=2,SHIP=3,TRAIN=4}}
local groups={}
for c=0,4 do
 local units={unit(10+c*10,1,true,100,true),unit(11+c*10,1,true,100,true),
              unit(12+c*10,1,false,100,true),unit(13+c*10,1,true,0,true),
              unit(14+c*10,2,true,100,true),unit(15+c*10,0,true,100,true),
              unit(16+c*10,1,true,100,false),player}
 groups[c]={{isExist=function() return true end,getUnits=function() return units end}}
end
coalition={side={RED=1,BLUE=2},getGroups=function(side,category) assert(side==1 or side==2);return groups[category] end}
timer={getTime=function() return 100 end};env={mission={theatre="PersianGulf"}}
coord={LOtoLL=function(point) return 24,54 end}
fiddlejson={encode=function(value) return value end}
local result=(function()
QUERY
end)()
assert(result.total_count==10 and #result.contacts==10 and result.omitted_count==0 and not result.truncated)
assert(result.friendly_total_count==5 and #result.friendlies==5 and not result.friendly_truncated)
for _,row in ipairs(result.friendlies) do assert(row.coalition==2 and row.id~=result.ownship.id) end
local categories={}
for _,row in ipairs(result.contacts) do
 assert(row.coalition==1 and row.altitude_ft==10000 and math.abs(row.speed_kt-1)<0.00001)
 categories[row.category]=true
end
assert(categories.airplane and categories.helicopter and categories.ground and categories.ship and categories.train)
'''.replace('QUERY', MISSION_QUERY)
    run_lua(tmp_path, program)


def test_actual_mission_lua_nil_or_unborn_player_never_enumerates(tmp_path):
    program = '''
world={getPlayer=function() return nil end}
coalition={getGroups=function() error("must not enumerate without player") end}
fiddlejson={encode=function(value) return value end}
local result=(function()
QUERY
end)()
assert(result.error and not result.contacts)
'''.replace('QUERY', MISSION_QUERY)
    run_lua(tmp_path, program)


def test_actual_mission_lua_missing_category_never_enumerates(tmp_path):
    program = '''
world={getPlayer=function() return {isExist=function() return true end,isActive=function() return true end,
getLife=function() return 10 end,getCoalition=function() return 2 end,getID=function() return 1 end,
getName=function() return "Player" end,getTypeName=function() return "F-22A" end,
getPoint=function() return {x=1,y=1,z=1} end} end}
coord={LOtoLL=function() return 24,54 end}
coalition={side={RED=1,BLUE=2},getGroups=function() error("must not enumerate unknown category") end}
Group={Category={AIRPLANE=0,HELICOPTER=1,GROUND=2}}
timer={getTime=function() return 100 end};env={mission={theatre="PersianGulf"}}
fiddlejson={encode=function(value) return value end}
local result=(function()
QUERY
end)()
assert(result.error and not result.contacts)
'''.replace('QUERY', MISSION_QUERY)
    run_lua(tmp_path, program)


def test_export_coalition_label_is_not_interpreted_as_iff():
    r = raw()
    r['aircraft']['coalition'] = 'Enemies'
    context = _context(health(), r)
    assert context['coalition'] is None
    result = validate_response(payload(), context)
    assert result['status'] == 'Available' and result['contacts'][0]['coalition'] == 'red'


def test_player_position_from_different_mission_fails_even_same_name_and_type():
    p = payload()
    p['mission']['ownship']['lat'] = 42
    assert validate_response(p, _context(health(), raw()))['contacts'] == []


def test_export_unit_label_is_distinct_from_live_mission_internal_name():
    p = payload()
    p['mission']['ownship']['name'] = 'Aerial-3-1'
    r = raw()
    r['aircraft']['unit_name'] = 'New callsign'
    result = validate_response(p, _context(health(), r))
    assert result['status'] == 'Available'
    assert result['ownship_unit'] == 'New callsign'
    assert result['mission_player_unit_name'] == 'Aerial-3-1'


def friendly_payload():
    p = payload()
    row = dict(p['mission']['contacts'][0], id='3', name='Friendly', coalition=2)
    p['mission'].update(friendlies=[row], friendly_total_count=1, friendly_omitted_count=0, friendly_truncated=False)
    return p


def test_friendly_and_enemy_coalitions_are_independent_with_matching_age():
    reader = AwarenessReader(request=friendly_payload)
    snap = available(reader)
    assert snap['contacts'][0]['coalition'] == 'red'
    assert snap['friendlies'][0]['coalition'] == 'blue'
    assert snap['friendlies'][0]['age_s'] == snap['age_s']
    h = dict(health(), model_advancing=False)
    masked = reader.snapshot(h, raw())
    assert masked['contacts'] == [] and masked['friendlies'] == []
    reader.close()


@pytest.mark.parametrize('field,value', [('id','1'), ('id','2'), ('coalition',0), ('coalition',1), ('lat',91)])
def test_ownship_duplicate_neutral_or_wrong_side_friend_never_published(field,value):
    p = friendly_payload()
    p['mission']['friendlies'][0][field] = value
    result = validate_response(p, _context(health(), raw()))
    assert result['status'] != 'Available'
    assert result['contacts'] == [] and result['friendlies'] == []


def test_friendly_shared_response_cap_is_explicit():
    p = friendly_payload()
    row = p['mission']['friendlies'][0]
    p['mission'].update(friendlies=[dict(row,id=str(i+100)) for i in range(999)],
                        friendly_total_count=1001, friendly_truncated=True)
    result = validate_response(p, _context(health(), raw()))
    assert result['status'] == 'Available'
    assert len(result['contacts'])+len(result['friendlies']) == 1000
    assert result['truncated'] is False and result['friendly_truncated'] is True
    assert result['friendly_total_count'] == 1001
