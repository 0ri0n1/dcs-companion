"""Optional true direction fields use only synthetic mission APIs and contacts."""
import pytest

from webapp.backend.awareness import AwarenessReader, MISSION_QUERY, _context, validate_response
from webapp.tests.test_awareness import available, friendly_payload, health, raw, run_lua


@pytest.mark.parametrize('field', ['track_true_deg', 'heading_true_deg'])
@pytest.mark.parametrize('value', [0, 90, 359.999])
def test_valid_true_directions_survive_on_both_coalitions(field, value):
    source = friendly_payload()
    for key in ('contacts', 'friendlies'):
        source['mission'][key][0][field] = value
    result = validate_response(source, _context(health(), raw()))
    assert result['status'] == 'Available'
    for key in ('contacts', 'friendlies'):
        assert result[key][0][field] == value
        assert result[key][0]['altitude_ft'] == 15000
        assert result[key][0]['speed_kt'] == 400


@pytest.mark.parametrize('field', ['track_true_deg', 'heading_true_deg'])
@pytest.mark.parametrize('value', [None, True, False, '90', -1, 360, float('nan'), float('inf'), {}])
def test_malformed_direction_is_omitted_without_dropping_position_or_other_direction(field, value):
    source = friendly_payload()
    other = 'heading_true_deg' if field == 'track_true_deg' else 'track_true_deg'
    for key in ('contacts', 'friendlies'):
        source['mission'][key][0].update({field: value, other: 45})
    result = validate_response(source, _context(health(), raw()))
    assert result['status'] == 'Available'
    for key in ('contacts', 'friendlies'):
        contact = result[key][0]
        assert field not in contact and contact[other] == 45
        assert contact['lat'] == 24.1 and contact['lon'] == 54.1
        assert contact['altitude_ft'] == 15000 and contact['speed_kt'] == 400


def test_direction_fields_expire_with_their_original_contact_sample():
    now = [10.]
    source = friendly_payload()
    for key in ('contacts', 'friendlies'):
        source['mission'][key][0].update(track_true_deg=75, heading_true_deg=70)
    reader = AwarenessReader(clock=lambda: now[0], request=lambda: source)
    result = available(reader)
    assert result['contacts'][0]['track_true_deg'] == 75
    assert result['friendlies'][0]['heading_true_deg'] == 70
    assert result['contacts'][0]['age_s'] == result['friendlies'][0]['age_s'] == 0
    reader.next_check = 100
    now[0] = 16
    stale = reader.snapshot(health(), raw())
    assert stale['status'] == 'Stale' and stale['contacts'] == [] and stale['friendlies'] == []
    reader.close()


LUA_STUB = r'''
local specs={}
local function unit(spec)
    local u={isExist=function() return true end,isActive=function() return true end,getLife=function() return 100 end,
             getID=function() return spec.id end,getName=function() return "Unit "..spec.id end,
             getTypeName=function() return "Fixture aircraft" end,getCoalition=function() return spec.side end,
             getPoint=function() return {x=0,y=3048,z=0} end}
    if not spec.missingVelocity then
        u.getVelocity=function() if spec.badVelocity then error("unavailable velocity") end;return spec.velocity end
    end
    if not spec.missingPosition then
        u.getPosition=function() if spec.badPosition then error("unavailable orientation") end;return {x=spec.forward} end
    end
    return u
end
local player=unit({id=1,side=2})
world={getPlayer=function() return player end}
Group={Category={AIRPLANE=0,HELICOPTER=1,GROUND=2,SHIP=3,TRAIN=4}}
coalition={side={RED=1,BLUE=2},getGroups=function(side,category)
    if category~=0 then return {} end
    local units={}
    for _,spec in ipairs(specs) do if spec.side==side then units[#units+1]=unit(spec) end end
    return {{isExist=function() return true end,getUnits=function() return units end}}
end}
timer={getTime=function() return 100 end};env={mission={theatre="Fixture"}}
local rotation=math.pi/6
coord={LOtoLL=function(p)
    return (math.cos(rotation)*p.x-math.sin(rotation)*p.z)/111000,
           (math.sin(rotation)*p.x+math.cos(rotation)*p.z)/111000
end}
'''


def test_executable_lua_converts_grid_vectors_to_true_track_and_body_heading(tmp_path):
    setup = r'''
specs={
 {id=10,side=1,velocity={x=100,y=900,z=0},forward={x=0,y=0,z=1}},
 {id=11,side=2,velocity={x=0,y=-900,z=100},forward={x=-1,y=0,z=0}},
 {id=12,side=1,velocity={x=0,y=100,z=0},forward={x=1,y=0,z=0}},
 {id=13,side=1,velocity={x=.1,y=100,z=0},forward={x=0,y=1,z=0}},
 {id=14,side=1,missingVelocity=true,missingPosition=true},
 {id=15,side=1,velocity={x=true,z=0},forward={x=0/0,z=1}},
 {id=16,side=1,badVelocity=true,badPosition=true},
 {id=17,side=1,velocity={x=1e308,z=1e308},forward={x=1e308,z=1e308}},
 {id=18,side=1,velocity=false,forward=false},
 {id=19,side=1,velocity={x=1852/3600,z=0}}
}
'''
    assertions = r'''
assert(not result.error and #result.contacts==9 and #result.friendlies==1)
local rows={}
for _,row in ipairs(result.contacts) do rows[row.id]=row;assert(row.altitude_ft==10000) end
local air=rows["10"]
assert(math.abs(air.speed_kt-100*3600/1852)<0.000001)
assert(math.abs(air.track_true_deg-30)<0.001)
assert(math.abs(air.heading_true_deg-120)<0.001)
assert(math.abs(result.friendlies[1].track_true_deg-120)<0.001)
assert(math.abs(result.friendlies[1].heading_true_deg-210)<0.001)
assert(rows["12"].speed_kt==0 and rows["12"].track_true_deg==nil)
assert(math.abs(rows["12"].heading_true_deg-30)<0.001)
assert(rows["13"].speed_kt>0 and rows["13"].track_true_deg==nil and rows["13"].heading_true_deg==nil)
assert(math.abs(rows["19"].speed_kt-1)<0.000001 and rows["19"].track_true_deg==nil)
for id=14,18 do
 assert(rows[tostring(id)].speed_kt==nil and rows[tostring(id)].track_true_deg==nil and rows[tostring(id)].heading_true_deg==nil)
end
'''
    program = LUA_STUB + setup + '\nlocal result=(function()\n' + MISSION_QUERY + '\nend)()\n' + assertions
    run_lua(tmp_path, program)


@pytest.mark.parametrize('conversion', [
    'if p.x~=0 or p.z~=0 then error("conversion unavailable") end;return 0,0',
    'if p.x~=0 or p.z~=0 then return 91,0 end;return 0,0',
    'if p.x~=0 or p.z~=0 then return 0,0/0 end;return 0,0',
    'return 0,0',
])
def test_optional_direction_conversion_failures_preserve_position_altitude_and_speed(tmp_path, conversion):
    setup = '''
specs={{id=10,side=1,velocity={x=100,z=0},forward={x=1,z=0}}}
coord.LOtoLL=function(p) CONVERSION end
'''.replace('CONVERSION', conversion)
    assertions = '''
assert(not result.error and #result.contacts==1)
local row=result.contacts[1]
assert(row.lat==0 and row.lon==0 and row.altitude_ft==10000 and row.speed_kt>0)
assert(row.heading_true_deg==nil and row.track_true_deg==nil)
'''
    program = LUA_STUB + setup + '\nlocal result=(function()\n' + MISSION_QUERY + '\nend)()\n' + assertions
    run_lua(tmp_path, program)
