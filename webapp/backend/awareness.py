"""Explicit single-player mission awareness, separate from aircraft sensors.

Only a fixed read-only program is sent to the already installed loopback hook.
Hooks verify single-player mode before and after the existing mission bridge
read. Results are withheld if either check or any identity fence disagrees. No browser input becomes Lua or a path.
"""
from __future__ import annotations

import copy
import http.client
import math
from pathlib import Path
import threading
import time

from .live_mission import SAVED, _fiddle_request, _number, _text, fiddle_authorization

MAX_CONTACTS = 1000
MAX_RESPONSE = 2 * 1024 * 1024
SOURCE = 'Existing DCS Fiddle bridge: single-player mission scripting (not onboard radar)'

# This entire program is application-owned. It is never constructed from HTTP
# input, a mission name, a unit name, or user-supplied Lua. The result is encoded
# by the existing mission bridge. Hooks mode checks bracket the request; the
# installed runtime cannot forward mission return values through a_do_script.
MISSION_QUERY = r'''
local function finite(v) return type(v)=="number" and v==v and v~=math.huge and v~=-math.huge end
local function angle(y,x)
    if type(math.atan2)=="function" then return math.atan2(y,x) end
    if x>0 then return math.atan(y/x) end
    if x<0 then return math.atan(y/x)+(y>=0 and math.pi or -math.pi) end
    if y>0 then return math.pi/2 end
    if y<0 then return -math.pi/2 end
    return nil
end
local function true_direction(p,lat,lon,vector)
    if type(vector)~="table" or not finite(vector.x) or not finite(vector.z) then return nil end
    local length=math.sqrt(vector.x*vector.x+vector.z*vector.z)
    if not finite(length) or length<0.000001 then return nil end
    -- DCS vector components use local grid axes, not true north. Project a
    -- short normalized displacement through the same geographic conversion
    -- as the contact, then measure its initial bearing from true north.
    local q={x=p.x+100*vector.x/length,y=p.y,z=p.z+100*vector.z/length}
    if not finite(q.x) or not finite(q.z) then return nil end
    local ok,lat2,lon2=pcall(function() return coord.LOtoLL(q) end)
    if not ok or not finite(lat2) or not finite(lon2) or math.abs(lat2)>90 or math.abs(lon2)>180 then return nil end
    local a,b,d=lat*math.pi/180,lat2*math.pi/180,(lon2-lon)*math.pi/180
    local east=math.sin(d)*math.cos(b)
    local north=math.cos(a)*math.sin(b)-math.sin(a)*math.cos(b)*math.cos(d)
    if not finite(east) or not finite(north) or east*east+north*north<0.000000000000000000000001 then return nil end
    local direction=angle(east,north)
    return direction and (direction*180/math.pi+360)%360 or nil
end
local function living(u)
    return u and u:isExist()==true and u:isActive()==true and finite(u:getLife()) and u:getLife()>0
end
local function identifier(u)
    local id=u:getID()
    if type(id)=="string" and string.match(id,"^%d+$") then id=tonumber(id) end
    if not finite(id) or id<0 or id%1~=0 then error("Invalid unit identity") end
    return tostring(id)
end
local function ownship()
    local u=world.getPlayer()
    if not living(u) then return nil end
    local side=u:getCoalition()
    if side~=coalition.side.RED and side~=coalition.side.BLUE then return nil end
    local p=u:getPoint()
    if type(p)~="table" or not finite(p.x) or not finite(p.y) or not finite(p.z) then return nil end
    local lat,lon=coord.LOtoLL(p)
    if not finite(lat) or not finite(lon) or math.abs(lat)>90 or math.abs(lon)>180 then return nil end
    return {id=identifier(u),name=u:getName(),type=u:getTypeName(),coalition=side,lat=lat,lon=lon}
end
local function snapshot()
    local owner=ownship()
    if not owner then return {error="No current live player unit"} end
    local result={ownship=owner,model_time=timer.getTime(),theatre=env.mission.theatre,
                  contacts={},total_count=0,omitted_count=0,truncated=false,
                  friendlies={},friendly_total_count=0,friendly_omitted_count=0,friendly_truncated=false}
    local enemy=owner.coalition==coalition.side.RED and coalition.side.BLUE or coalition.side.RED
    local categories={{Group.Category.AIRPLANE,"airplane"},{Group.Category.HELICOPTER,"helicopter"},
                      {Group.Category.GROUND,"ground"},{Group.Category.SHIP,"ship"}}
    if type(Group.Category.TRAIN)=="number" then categories[#categories+1]={Group.Category.TRAIN,"train"} end
    local category_ids={}
    for _,category in ipairs(categories) do
        if not finite(category[1]) or category_ids[category[1]] then return {error="Unit categories unavailable"} end
        category_ids[category[1]]=true
    end
    local seen, scanned={},0
    for _, side in ipairs({enemy,owner.coalition}) do
    local friendly=side==owner.coalition
    local total_key=friendly and "friendly_total_count" or "total_count"
    local omitted_key=friendly and "friendly_omitted_count" or "omitted_count"
    local rows=friendly and result.friendlies or result.contacts
    for _, category in ipairs(categories) do
        local groups=coalition.getGroups(side,category[1])
        if type(groups)~="table" then return {error="Group enumeration unavailable"} end
        for _, group in pairs(groups) do
            local ok, units=pcall(function()
                if group:isExist()~=true then return {} end
                return group:getUnits()
            end)
            if not ok or type(units)~="table" then return {error="Group changed during enumeration"} end
            for _,unit in pairs(units) do
                scanned=scanned+1
                if scanned>25000 then return {error="Mission exceeds bounded enumeration capacity"} end
                local readable, row=pcall(function()
                    if not living(unit) or unit:getCoalition()~=side then return nil end
                    local id=identifier(unit)
                    if id==owner.id or seen[id] then return nil end
                    seen[id]=true
                    result[total_key]=result[total_key]+1
                    local p=unit:getPoint()
                    if type(p)~="table" or not finite(p.x) or not finite(p.y) or not finite(p.z) then
                        result[omitted_key]=result[omitted_key]+1; return nil
                    end
                    local lat,lon=coord.LOtoLL(p)
                    if not finite(lat) or not finite(lon) or math.abs(lat)>90 or math.abs(lon)>180 then
                        result[omitted_key]=result[omitted_key]+1; return nil
                    end
                    local record={id=id,name=unit:getName(),type=unit:getTypeName(),category=category[2],
                                  coalition=side,lat=lat,lon=lon,altitude_ft=p.y/0.3048}
                    local valid,v=pcall(function() return unit:getVelocity() end)
                    if valid and type(v)=="table" and finite(v.x) and finite(v.z) then
                        local horizontal=math.sqrt(v.x*v.x+v.z*v.z)
                        local speed=horizontal*3600/1852
                        if finite(speed) then
                            record.speed_kt=speed
                            if horizontal>1852/3600 then record.track_true_deg=true_direction(p,lat,lon,v) end
                        end
                    end
                    local oriented,position=pcall(function() return unit:getPosition() end)
                    if oriented and type(position)=="table" then
                        record.heading_true_deg=true_direction(p,lat,lon,position.x)
                    end
                    return record
                end)
                if not readable then return {error="Unit changed during enumeration"} end
                if row and #result.contacts+#result.friendlies<1000 then rows[#rows+1]=row end
            end
        end
    end
    end
    local after=ownship()
    if not after or after.id~=owner.id or after.name~=owner.name or after.type~=owner.type or after.coalition~=owner.coalition then
        return {error="Player unit changed during enumeration"}
    end
    result.truncated=result.total_count-result.omitted_count>#result.contacts
    result.friendly_truncated=result.friendly_total_count-result.friendly_omitted_count>#result.friendlies
    return result
end
return snapshot()
'''.strip()

HOOK_QUERY = ('return {multiplayer=DCS.isMultiplayer(),paused=DCS.getPause(),name=DCS.getMissionName(),'
              'model_time=DCS.getModelTime(),aircraft=DCS.getPlayerUnitType()}')


def read_bridge_awareness(hook_path):
    authorization = fiddle_authorization(hook_path)
    before = _fiddle_request(12081, HOOK_QUERY, authorization)
    # Fail closed before sending any enemy query. An absent/zero/unknown mode
    # value is not a single-player assertion. Paused mission HTTP handlers wait
    # for model time, so they must not receive an enumeration request either.
    if (before.get('multiplayer') is not False or before.get('paused') is not False or
            not _text(before.get('name')) or not _text(before.get('aircraft')) or
            not _number(before.get('model_time')) or before['model_time'] < 0):
        return {'before': before}
    mission = _fiddle_request(12080, MISSION_QUERY, authorization, max_response=MAX_RESPONSE)
    after = _fiddle_request(12081, HOOK_QUERY, authorization)
    result = {'before': before, 'after': after}
    if (after.get('multiplayer') is False and after.get('paused') is False and
            after.get('name') == before['name'] and after.get('aircraft') == before['aircraft'] and
            _number(after.get('model_time')) and 0 <= after['model_time']-before['model_time'] <= 3):
        result['mission'] = mission
    return result


def _empty(detail, status='Unavailable', *, single_player=None):
    return {'status': status, 'mode': 'mission-awareness', 'source': SOURCE, 'age_s': None,
            'session': None, 'aircraft': None, 'ownship_unit': None, 'single_player': single_player,
            'model_advancing': False, 'contacts': [], 'friendlies': [], 'total_count': None, 'omitted_count': 0,
            'friendly_total_count': None, 'friendly_omitted_count': 0, 'friendly_truncated': False,
            'truncated': False, 'detail': detail}


def _coordinates(lat, lon):
    return _number(lat) and _number(lon) and -90 <= lat <= 90 and -180 <= lon <= 180


def _context(health, raw):
    if not isinstance(health, dict) or not isinstance(raw, dict):
        return None
    if (health.get('dcs_running') is not True or health.get('telemetry_fresh') is not True or
            health.get('model_advancing') is not True or health.get('cockpit') is not True or
            not _text(health.get('process_identity')) or not _text(health.get('session'))):
        return None
    aircraft, stream = raw.get('aircraft') or {}, raw.get('stream') or {}
    if not isinstance(aircraft, dict) or not isinstance(stream, dict):
        return None
    if (stream.get('exporter_connected') is not True or stream.get('live') is not True or
            not _number(health.get('telemetry_age_s')) or not 0 <= health['telemetry_age_s'] <= 3 or
            not _coordinates(aircraft.get('latitude'), aircraft.get('longitude'))):
        return None
    kind, unit = _text(aircraft.get('aircraft')), _text(aircraft.get('unit_name'))
    side, model = aircraft.get('coalition'), aircraft.get('model_time_s')
    if (not kind or kind != health.get('aircraft') or not unit or isinstance(side, bool) or
            isinstance(side, (int, float)) and side not in (1, 2) or
            side is not None and not isinstance(side, (str, int, float)) or not _number(model) or model < 0):
        return None
    identity = (health['process_identity'], health['session'], kind, unit, side)
    return {'identity': identity, 'session': health['session'], 'aircraft': kind, 'unit': unit,
            'coalition': side if type(side) is int and side in (1, 2) else None, 'model_time': model,
            'lat': aircraft['latitude'], 'lon': aircraft['longitude']}


def _near_ownship(owner, context):
    if not _coordinates(owner.get('lat'), owner.get('lon')):
        return False
    lat1, lat2 = math.radians(owner['lat']), math.radians(context['lat'])
    delta = math.radians(owner['lon']-context['lon'])
    a = math.sin((lat1-lat2)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(delta/2)**2
    return 3440.065 * 2 * math.asin(math.sqrt(max(0, min(1, a)))) <= 5


def validate_response(payload, context):
    """Treat every bridge field as untrusted and withhold contradictory results."""
    if not isinstance(payload, dict):
        return _empty('Current mission-awareness source is unavailable.')
    before, after = payload.get('before'), payload.get('after')
    if not isinstance(before, dict):
        return _empty('Single-player mode could not be verified.')
    if before.get('multiplayer') is True or isinstance(after, dict) and after.get('multiplayer') is True:
        return _empty('Mission awareness is disabled in multiplayer.', 'Disabled', single_player=False)
    if before.get('multiplayer') is not False or not isinstance(after, dict) or after.get('multiplayer') is not False:
        return _empty('Single-player mode could not be verified.')
    if before.get('paused') is not False or after.get('paused') is not False:
        return _empty('Mission awareness waits for an advancing flight.', 'Paused', single_player=True)
    name = _text(before.get('name'))
    if not name or after.get('name') != name or before.get('aircraft') != context['aircraft'] or after.get('aircraft') != context['aircraft']:
        return _empty('Mission or aircraft changed during the read.')
    first, last = before.get('model_time'), after.get('model_time')
    if not _number(first) or not _number(last) or not 0 <= last-first <= 3 or abs(first-context['model_time']) > 4:
        return _empty('Mission time does not match the current exporter session.')
    mission = payload.get('mission')
    if not isinstance(mission, dict) or mission.get('error'):
        return _empty('Current live mission units could not be read safely.', single_player=True)
    stamp = mission.get('model_time')
    if not _number(stamp) or not first-0.1 <= stamp <= last+0.1:
        return _empty('Mission and hooks model times do not match.')
    owner = mission.get('ownship')
    if (not isinstance(owner, dict) or not _text(owner.get('id')) or not _text(owner.get('name')) or
            owner.get('type') != context['aircraft'] or type(owner.get('coalition')) is not int or
            owner['coalition'] not in (1, 2) or
            context['coalition'] is not None and owner['coalition'] != context['coalition'] or
            not _near_ownship(owner, context)):
        return _empty('Live player unit does not match current ownship telemetry.')
    groups, counts, ids = {}, {}, {owner['id']}
    # The mission player's numeric coalition determines allegiance. Export
    # presentation labels and country names are never treated as IFF evidence.
    remaining = MAX_CONTACTS
    for key, prefix, side in (('contacts', '', 3-owner['coalition']),
                              ('friendlies', 'friendly_', owner['coalition'])):
        rows = mission.get(key, [] if key == 'friendlies' else None)
        if rows == {}: rows = []  # Existing Lua JSON encoder's empty-table form.
        total = mission.get(prefix+'total_count', 0 if prefix else None)
        omitted = mission.get(prefix+'omitted_count', 0 if prefix else None)
        truncated = mission.get(prefix+'truncated', False if prefix else None)
        if (not isinstance(rows, list) or len(rows) > remaining or type(total) is not int or not 0 <= total <= 25000 or
                type(omitted) is not int or not 0 <= omitted <= total or type(truncated) is not bool or
                len(rows) != min(remaining, total-omitted) or truncated != (total-omitted > len(rows))):
            return _empty('Mission-awareness response counts are inconsistent.')
        contacts = []
        for row in rows:
            if (not isinstance(row, dict) or not _text(row.get('id')) or row['id'] in ids or not _text(row.get('name')) or
                    not _text(row.get('type')) or row.get('category') not in ('airplane', 'helicopter', 'ground', 'ship', 'train') or
                    type(row.get('coalition')) is not int or row['coalition'] != side or
                    not _coordinates(row.get('lat'), row.get('lon')) or not _number(row.get('altitude_ft'))):
                return _empty('A live contact failed identity, coalition, or coordinate validation.')
            ids.add(row['id'])
            contact = {field: row[field] for field in ('id', 'name', 'type', 'category', 'lat', 'lon', 'altitude_ft')}
            contact['coalition'] = 'red' if side == 1 else 'blue'
            if _number(row.get('speed_kt')) and 0 <= row['speed_kt'] <= 10000:
                contact['speed_kt'] = row['speed_kt']
            for field in ('track_true_deg', 'heading_true_deg'):
                if _number(row.get(field)) and 0 <= row[field] < 360:
                    contact[field] = row[field]
            contacts.append(contact)
        groups[key] = sorted(contacts, key=lambda row: row['id'])
        counts.update({prefix+'total_count': total, prefix+'omitted_count': omitted, prefix+'truncated': truncated})
        remaining -= len(rows)
    if counts['total_count']+counts['friendly_total_count'] > 25000:
        return _empty('Mission-awareness response exceeds its census limit.')
    detail = 'Live friendly and enemy units from single-player mission scripting. These are not aircraft detections or IFF replies. Ownship, neutral units, static objects and scenery are excluded.'
    for prefix, label in (('', 'enemy'), ('friendly_', 'friendly')):
        if counts[prefix+'truncated']:
            detail += f" Partial {label} export: {counts[prefix+'total_count']} live units counted; shared response limit reached."
        if counts[prefix+'omitted_count']:
            detail += f" {counts[prefix+'omitted_count']} {label} units omitted because coordinates were unavailable."
    return {'status': 'Available', 'mode': 'mission-awareness', 'source': SOURCE, 'age_s': 0,
            'session': context['session'], 'aircraft': context['aircraft'], 'ownship_unit': context['unit'],
            'mission_player_unit_id': owner['id'], 'mission_player_unit_name': owner['name'],
            'single_player': True, 'model_advancing': True, **groups, **counts, 'detail': detail,
            'theatre': _text(mission.get('theatre'), 64), 'mission_name': name, 'model_time': stamp}


class AwarenessReader:
    def __init__(self, enabled=True, hook_path=None, *, clock=time.monotonic, request=None):
        self.enabled, self.clock = enabled, clock
        self.hook_path = Path(hook_path or SAVED/'Scripts'/'Hooks'/'dcs-fiddle-server.lua')
        self.request = request or (lambda: read_bridge_awareness(self.hook_path))
        self.lock = threading.RLock()
        self.identity = None
        self.revision = 0
        self.result = _empty('Waiting for verified single-player flight.')
        self.checked_at = None
        self.next_check = float('-inf')
        self.worker = None
        self.stopped = False

    def _refresh(self, context, revision):
        started = self.clock()
        try:
            result = validate_response(self.request(), context)
        except (OSError, ValueError, TypeError, http.client.HTTPException):
            result = _empty('Current mission-awareness bridge is unavailable; no contacts are retained.')
        with self.lock:
            if self.enabled and not self.stopped and context['identity'] == self.identity and revision == self.revision:
                self.result = result
                self.checked_at = started  # Network latency counts toward contact age.

    def snapshot(self, health, raw, mission=None):
        context = _context(health, raw)
        with self.lock:
            if not self.enabled or self.stopped:
                self.identity = None
                self.revision += 1
                self.result = _empty('Mission awareness is disabled.', 'Disabled')
                self.checked_at = None
                self.next_check = float('-inf')
                return _empty('Mission awareness is disabled.', 'Disabled')
            identity = context['identity'] if context else None
            if identity != self.identity:
                self.identity, self.revision = identity, self.revision+1
                self.result, self.checked_at = _empty('Waiting for verified single-player flight.'), None
                self.next_check = float('-inf')
            if context is None:
                return _empty('Mission awareness waits for fresh, advancing ownship telemetry.')
            now = self.clock()
            if now >= self.next_check and not (self.worker and self.worker.is_alive()):
                self.next_check = now+2
                self.worker = threading.Thread(target=self._refresh, args=(copy.deepcopy(context), self.revision),
                                               name='DCS single-player mission awareness', daemon=True)
                self.worker.start()
            age = now-self.checked_at if self.checked_at is not None else None
            result = copy.deepcopy(self.result)
            if age is not None and not 0 <= age <= 5:
                return _empty('Mission-awareness contacts are stale.', 'Stale')
            if result['status'] == 'Available':
                stamp = result.get('model_time')
                if not _number(stamp) or not -1 <= context['model_time']-stamp <= 5:
                    return _empty('Mission-awareness contacts belong to a different model time.', 'Stale')
                # Mission overlay metadata is optional. If it carries a current
                # theatre, a disagreement cannot be drawn on that map.
                if isinstance(mission, dict) and mission.get('status') == 'Current' and mission.get('theatre') and result.get('theatre') and mission['theatre'] != result['theatre']:
                    return _empty('Mission-awareness theatre differs from current mission metadata.')
                result['age_s'] = round(age, 3)
                for contact in result['contacts']+result['friendlies']:
                    contact['age_s'] = round(age, 3)
            else:
                result['age_s'] = round(age, 3) if age is not None else None
            return result

    def close(self):
        with self.lock:
            self.stopped = True
            self.revision += 1
            self.identity = None
            self.result = _empty('Mission awareness is disabled.', 'Disabled')
