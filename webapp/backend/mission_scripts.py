"""PC-registered mission scripts; browsers submit identifiers, never Lua or paths.

This is not a Lua sandbox. A PC owner explicitly registers trusted source by its
SHA-256. The existing DCS mission bridge executes it once; cancellation can stop
pending steps but cannot preempt Lua already executing inside the simulation.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
from uuid import UUID

from .live_mission import _fiddle_request, fiddle_authorization

MAX_SOURCE = 16384
MAX_MANIFEST = 4096
MAX_SCRIPTS = 64
ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")
HOOK_STATE = ('return {multiplayer=DCS.isMultiplayer(),paused=DCS.getPause(),'
              'name=DCS.getMissionName(),model_time=DCS.getModelTime(),'
              'aircraft=DCS.getPlayerUnitType()}')
IDENTITY = ("process", "session", "mission", "aircraft", "bindings_hash")


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _label(value, limit):
    return isinstance(value, str) and 0 < len(value.strip()) <= limit and not any(ord(c) < 32 for c in value)


def _lua_string(value):
    # UTF-8 Lua strings with fixed-width decimal escapes for control bytes. Keep
    # nested dispatch bounded instead of multiplying every source byte by four.
    encoded = []
    for character in value:
        if character in ('"', '\\'):
            encoded.append('\\' + character)
        elif ord(character) < 32 or ord(character) == 127:
            encoded.append('\\%03d' % ord(character))
        else:
            encoded.append(character)
    return '"' + ''.join(encoded) + '"'


def _result(ok=False, *, sent=False, dry_run=False, ambiguous=False, message):
    return dict(ok=ok, sent=sent, dry_run=dry_run, ambiguous=ambiguous, message=message)


class ScriptRegistry:
    def __init__(self, directory, hook_path, context_provider=None, *, request=_fiddle_request,
                 authorization=fiddle_authorization):
        self.directory = Path(directory).resolve()
        self.hook_path = Path(hook_path)
        self.context_provider = context_provider
        self.request, self.authorization = request, authorization

    def _file(self, script_id, extension, maximum):
        if not isinstance(script_id, str) or not ID.fullmatch(script_id):
            raise ValueError('Unknown registered script identifier.')
        path = self.directory / (script_id + extension)
        if path.is_symlink() or path.resolve().parent != self.directory or not path.is_file():
            raise ValueError('Registered script files must stay in the PC script library.')
        with path.open('rb') as handle:
            content = handle.read(maximum + 1)
        if len(content) > maximum:
            raise ValueError('Registered script exceeds the library size limit.')
        return content

    def prepare(self, script_id):
        try:
            manifest = json.loads(self._file(script_id, '.json', MAX_MANIFEST).decode('utf-8-sig'))
            allowed = {'schema', 'id', 'name', 'description', 'context', 'aircraft', 'sha256', 'enabled'}
            if not isinstance(manifest, dict) or set(manifest) != allowed:
                raise ValueError('Invalid registered script manifest.')
            if (manifest['schema'] != 'dcs-companion/mission-script/1' or manifest['id'] != script_id
                    or manifest['context'] != 'mission' or manifest['enabled'] is not True
                    or not _label(manifest['name'], 80) or not _label(manifest['description'], 300)
                    or not isinstance(manifest['sha256'], str) or not SHA.fullmatch(manifest['sha256'])):
                raise ValueError('Script is disabled or its manifest is invalid.')
            aircraft = manifest['aircraft']
            if (not isinstance(aircraft, list) or not 1 <= len(aircraft) <= 16
                    or any(not _label(value, 80) for value in aircraft) or len(set(aircraft)) != len(aircraft)
                    or '*' in aircraft and aircraft != ['*']):
                raise ValueError('Script aircraft selection is invalid.')
            source_bytes = self._file(script_id, '.lua', MAX_SOURCE)
            digest = hashlib.sha256(source_bytes).hexdigest()
            if digest != manifest['sha256']:
                raise ValueError('Script source changed; register its reviewed revision on the PC before running it.')
            source = source_bytes.decode('utf-8-sig')
            if not source.strip() or '\x00' in source:
                raise ValueError('Script source is empty or invalid.')
            return {**manifest, 'source': source}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError('Registered script files are missing or unreadable.') from exc

    def public(self):
        rows = []
        # A missing library is normal on a clean install; no directory is created.
        for path in sorted(self.directory.glob('*.json'))[:MAX_SCRIPTS]:
            if not ID.fullmatch(path.stem):
                continue
            try:
                item = self.prepare(path.stem)
                rows.append({key: item[key] for key in ('id', 'name', 'description', 'context', 'aircraft', 'sha256')}
                            | {'status': 'Registered', 'available': True, 'atomic_stop_supported': False})
            except ValueError as exc:
                rows.append({'id': path.stem, 'name': path.stem, 'context': 'mission',
                             'description': str(exc), 'status': 'Unavailable', 'available': False,
                             'aircraft': [], 'atomic_stop_supported': False})
        return rows

    def _context_valid(self, ctx, prepared):
        if (not isinstance(ctx, dict) or any(not ctx.get(key) for key in IDENTITY)
                or ctx.get('telemetry_valid') is not True or ctx.get('model_advancing') is not True
                or ctx.get('setup_matches_process') is not True or ctx.get('bindings_valid') is not True
                or not _finite(ctx.get('model_time')) or ctx['model_time'] < 0):
            return False
        authorized = ctx.get('_remote_authorized')
        if authorized is not None and (not callable(authorized) or authorized() is not True):
            return False
        if prepared['aircraft'] != ['*'] and ctx['aircraft'] not in prepared['aircraft']:
            return False
        if self.context_provider:
            current = self.context_provider()
            if (any(current.get(key) != ctx.get(key) for key in IDENTITY)
                    or current.get('telemetry_valid') is not True or current.get('model_advancing') is not True
                    or current.get('setup_matches_process') is not True or current.get('bindings_valid') is not True):
                return False
        return True

    def execute(self, prepared, ctx, request_id, dry_run=False):
        try:
            run_id = str(UUID(request_id))
            current = self.prepare(prepared['id'])
            if current != prepared:
                raise ValueError('Registered script definition changed before execution.')
            if not self._context_valid(ctx, current):
                raise ValueError('Script needs the same current aircraft, setup and advancing flight.')
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            return _result(message=str(exc) if isinstance(exc, ValueError) else 'Invalid registered script request.')
        if dry_run:
            return _result(True, dry_run=True, message='Preview complete. No Lua was sent to DCS.')
        submitted = False
        try:
            authorization = self.authorization(self.hook_path)
            before = self.request(12081, HOOK_STATE, authorization)
            if (before.get('multiplayer') is not False or before.get('paused') is not False
                    or before.get('aircraft') != ctx['aircraft'] or not _label(before.get('name'), 256)
                    or not _finite(before.get('model_time')) or abs(before['model_time']-ctx['model_time']) > 3
                    or not self._context_valid(ctx, current)):
                return _result(message='Mission scripts require a verified, current single-player flight.')
            mission_program = self._mission_program(current['source'], run_id, ctx['aircraft'], before['model_time'])
            dispatch = self._dispatch_program(mission_program, before)
            if not self._context_valid(ctx, current):
                return _result(message='Script stopped or its flight context changed before dispatch.')
            submitted = True
            result = self.request(12081, dispatch, authorization)
            if result.get('accepted') is False:
                return _result(message='DCS rejected the script because its mission context changed or its bridge is unavailable.')
            if result.get('accepted') is not True:
                return _result(ambiguous=True, message='Script delivery is uncertain; it will not be retried.')
            receipt_query = ('local r=rawget(_G,"__DCS_COMPANION_SCRIPT_RUNS_V1"); '
                             'return {receipt=r and r.items[' + _lua_string(run_id) + '] or false}')
            receipt = self.request(12080, receipt_query, authorization).get('receipt')
            if not isinstance(receipt, dict) or receipt.get('id') != run_id:
                return _result(sent=True, ambiguous=True, message='Script was submitted; completion was not observed. No retry.')
            if receipt.get('blocked') is True:
                return _result(message='Script was blocked inside DCS because the aircraft or mission time changed.')
            if receipt.get('completed') is not True or receipt.get('ok') is not True:
                return _result(sent=True, ambiguous=True, message='Script did not complete successfully; earlier effects may have occurred. No retry.')
            return _result(True, sent=True, message='Registered Lua completed in DCS. Its cockpit effects were not independently verified.')
        except Exception:
            # Neither source, bridge credentials nor arbitrary script errors enter API output.
            return _result(ambiguous=submitted, message=('Script completion is uncertain; no automatic retry.' if submitted
                                                       else 'The existing local DCS mission bridge is unavailable.'))

    @staticmethod
    def _mission_program(source, run_id, aircraft, model_time):
        return ('local id=' + _lua_string(run_id) + '\n'
                'local store=rawget(_G,"__DCS_COMPANION_SCRIPT_RUNS_V1")\n'
                'if type(store)~="table" then store={items={},order={}}; rawset(_G,"__DCS_COMPANION_SCRIPT_RUNS_V1",store) end\n'
                'if store.items[id] then return end\n'
                'local r={id=id,completed=false,ok=false}; store.items[id]=r; store.order[#store.order+1]=id\n'
                'while #store.order>64 do store.items[table.remove(store.order,1)]=nil end\n'
                'local u=world.getPlayer(); local t=timer.getTime()\n'
                'if not u or not u:isExist() or not u:isActive() or u:getLife()<=0 or u:getTypeName()~=' + _lua_string(aircraft)
                + ' or type(t)~="number" or math.abs(t-' + repr(float(model_time)) + ')>3 then r.blocked=true; return end\n'
                'local ok=pcall(function()\n' + source + '\nend)\n'
                'r.completed=true; r.ok=ok==true\n')

    @staticmethod
    def _dispatch_program(program, before):
        mission_command = 'a_do_script(' + _lua_string(program) + ')'
        return ('if DCS.isMultiplayer()~=false or DCS.getPause()~=false or DCS.getMissionName()~='
                + _lua_string(before['name']) + ' or DCS.getPlayerUnitType()~=' + _lua_string(before['aircraft'])
                + ' or math.abs(DCS.getModelTime()-' + repr(float(before['model_time']))
                + ')>3 then return {accepted=false} end\n'
                'local ok=pcall(function() net.dostring_in("mission",' + _lua_string(mission_command) + ') end)\n'
                'return {accepted=ok==true}\n')
