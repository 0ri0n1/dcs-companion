"""Read current mission metadata through existing local sources; never mutate DCS.

The two bridge payloads below are fixed metadata reads. No API accepts code,
hosts, mission paths or object queries from a browser request.
"""
from __future__ import annotations

import base64
import copy
from datetime import datetime, timezone
import http.client
import json
import math
from pathlib import Path, PureWindowsPath
import re
import threading
import time


SAVED = Path.home()/'Saved Games'/'DCS'
HOOK_QUERY = 'return {name=DCS.getMissionName(),model_time=DCS.getModelTime(),paused=DCS.getPause()}'
MISSION_QUERY = 'return {name=env.mission.sortie,theatre=env.mission.theatre,model_time=timer.getTime()}'
MAX_RESPONSE = 16384
MAX_LOG_READ = 4 * 1024 * 1024


def _text(value, maximum=256):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > maximum or any(ord(c) < 32 for c in value):
        return None
    if value.startswith('DictKey_') or 'stack traceback' in value or 'attempt to ' in value or '[string ' in value:
        return None
    return value


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _unavailable(detail, status='Unavailable'):
    return {'status': status, 'name': None, 'theatre': None, 'source': None, 'age_s': None, 'detail': detail}


def _fiddle_request(port, code, authorization, *, max_response=MAX_RESPONSE):
    """Fixed loopback origin; HTTP redirects and proxy settings cannot leak auth."""
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=0.4)
    try:
        path = '/' + base64.b64encode(code.encode('utf-8')).decode('ascii') + '?env=default'
        headers = {'Authorization': authorization} if authorization else {}
        connection.request('GET', path, headers=headers)
        response = connection.getresponse()
        raw = response.read(max_response + 1)
        if response.status != 200 or len(raw) > max_response:
            raise ValueError('Mission metadata bridge response unavailable')
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError('Mission metadata bridge returned an invalid envelope')
        result = payload.get('result')
        if not isinstance(result, dict):
            raise ValueError('Mission metadata bridge returned no metadata')
        return result
    finally:
        connection.close()


def fiddle_authorization(hook_path):
    """Read existing loopback bridge authentication without exposing credentials."""
    with Path(hook_path).open('r', encoding='utf-8-sig') as handle:
        configuration = handle.read(4096)
    if not re.search(r'^FIDDLE\.PORT\s*=\s*12080\b', configuration, re.M):
        raise ValueError('Expected existing local metadata bridge is unavailable')
    authorization = None
    if re.search(r'^FIDDLE\.AUTH\s*=\s*true\b', configuration, re.M):
        credentials = []
        for field in ('USERNAME', 'PASSWORD'):
            match = re.search(r'^FIDDLE\.' + field + r'\s*=\s*([\x27\x22])([^\r\n]*?)\1', configuration, re.M)
            if not match or '\\' in match.group(2) or len(match.group(2)) > 256:
                raise ValueError('Existing bridge authentication format unavailable')
            credentials.append(match.group(2))
        authorization = 'Basic ' + base64.b64encode(':'.join(credentials).encode('utf-8')).decode('ascii')
    return authorization


def read_bridge_metadata(hook_path):
    """Read fixed current metadata; never copy credentials into results or errors."""
    authorization = fiddle_authorization(hook_path)
    hook = _fiddle_request(12081, HOOK_QUERY, authorization)
    name = _text(hook.get('name'))
    if not name or not _number(hook.get('model_time')) or hook['model_time'] < 0:
        raise ValueError('Bridge has no current mission name')
    result = {'name': name, 'theatre': None, 'source': 'Existing DCS Fiddle hooks metadata (127.0.0.1:12081)',
              'detail': 'Name reported by DCS.getMissionName; no mission objects are queried.'}
    try:
        mission = _fiddle_request(12080, MISSION_QUERY, authorization)
        # A mission swap between the two reads must not combine generations.
        if _number(mission.get('model_time')) and abs(mission['model_time'] - hook['model_time']) <= 3:
            result['theatre'] = _text(mission.get('theatre'), 64)
            title = _text(mission.get('name'))
            if title:
                result['name'] = title
                result['detail'] = 'Title and theatre reported by the current mission metadata; no mission objects are queried.'
            result['source'] += ' + mission metadata (127.0.0.1:12080)'
    except (OSError, ValueError, http.client.HTTPException):
        pass
    if hook.get('paused') is True:
        result['detail'] += ' DCS reports this mission is paused; metadata remains read-only.'
    return result


class LogLifecycle:
    """Incremental parser of verified local Dispatcher lifecycle lines."""
    LINE = re.compile(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+)\s+\w+\s+(Dispatcher|EDCORE) \(Main\): (.*)$')

    def __init__(self, path):
        self.path = Path(path)
        self.identity = None
        self.file_identity = None
        self.offset = 0
        self.partial = b''
        self.active = None
        self.pending = None

    def _clear(self):
        self.active = self.pending = None

    def read(self, process_identity):
        try:
            started = float(str(process_identity).rsplit(':', 1)[1])
            if not math.isfinite(started):
                return None
            stat = self.path.stat()
            signature = (stat.st_dev, stat.st_ino, stat.st_ctime_ns)
            if self.identity != process_identity or self.file_identity != signature or stat.st_size < self.offset:
                self.identity, self.file_identity = process_identity, signature
                self.offset = max(0, stat.st_size - MAX_LOG_READ)
                self.partial = b''
                self._clear()
            if stat.st_size - self.offset > MAX_LOG_READ:
                self.offset = stat.st_size - MAX_LOG_READ
                self.partial = b''
                self._clear()
            with self.path.open('rb') as handle:
                handle.seek(self.offset)
                content = handle.read(MAX_LOG_READ)
                self.offset = handle.tell()
            lines = (self.partial + content).split(b'\n')
            self.partial = lines.pop()[-4096:]
            for raw in lines:
                line = raw.decode('utf-8', errors='replace').rstrip('\r')
                match = self.LINE.match(line)
                if not match:
                    continue
                stamp = datetime.strptime(match[1], '%Y-%m-%d %H:%M:%S.%f').replace(tzinfo=timezone.utc).timestamp()
                if stamp < started - 1:
                    continue
                logger, message = match[2], match[3]
                if logger == 'EDCORE' and message == '(dDispatcher)enterToState_:3':
                    self._clear()
                elif logger == 'Dispatcher' and re.match(r'(?i)^stopMission(?:\s|$)', message):
                    self._clear()
                elif logger == 'Dispatcher' and message.startswith('loadMission ') and not message.startswith('loadMission Done:'):
                    path = message[len('loadMission '):].strip().strip('"')
                    self.active = None
                    self.pending = {'path': path, 'theatre': None} if path.lower().endswith('.miz') else None
                elif logger == 'Dispatcher' and message.startswith('Terrain theatre ') and self.pending:
                    self.pending['theatre'] = _text(message[len('Terrain theatre '):], 64)
                elif logger == 'Dispatcher' and message == 'loadMission Done: Control passed to the player':
                    if self.pending:
                        self.active, self.pending = self.pending, None
            if not self.active:
                return None
            name = _text(PureWindowsPath(self.active['path']).stem)
            if not name:
                return None
            return {'name': name, 'theatre': self.active['theatre'],
                    'source': 'Current DCS log lifecycle (loaded filename)',
                    'detail': 'Loaded filename, not a resolved briefing title. DCS logged mission load and control handoff in this process.'}
        except (OSError, ValueError, IndexError, OverflowError):
            self._clear()
            return None


class MissionReader:
    def __init__(self, log_path=None, hook_path=None, *, clock=time.monotonic, bridge=None):
        self.log = LogLifecycle(log_path or SAVED/'Logs'/'dcs.log')
        self.hook_path = Path(hook_path or SAVED/'Scripts'/'Hooks'/'dcs-fiddle-server.lua')
        self.clock = clock
        self.bridge = bridge or (lambda: read_bridge_metadata(self.hook_path))
        self.lock = threading.RLock()
        self.identity = None
        self.revision = 0
        self.result = _unavailable('Waiting for an active DCS mission.')
        self.checked_at = None
        self.next_check = float('-inf')
        self.worker = None
        self.stopped = False

    @staticmethod
    def _identity(health):
        if not isinstance(health, dict) or not health.get('dcs_running') or not health.get('process_identity') or not health.get('session'):
            return None
        if health.get('status') in ('Offline', 'DCS menus/no aircraft') or not health.get('aircraft'):
            return None
        return (health['process_identity'], health['session'])

    def tick(self, health, raw=None):
        identity = self._identity(health)
        with self.lock:
            if self.stopped:
                return
            if identity != self.identity:
                self.identity = identity
                self.revision += 1
                self.result = _unavailable('Waiting for current mission metadata.' if identity else 'No active aircraft mission.')
                self.checked_at = None
                self.next_check = float('-inf')
            if identity is None:
                return
            now = self.clock()
            if now < self.next_check or (self.worker and self.worker.is_alive()):
                return
            self.next_check = now + 2
            raw = raw if isinstance(raw, dict) else {}
            metadata = raw.get('mission')
            if isinstance(metadata, dict) and health.get('telemetry_fresh'):
                name = _text(metadata.get('name') or metadata.get('title'))
                if name:
                    self.result = {'status': 'Current', 'name': name, 'theatre': _text(metadata.get('theatre') or metadata.get('terrain'), 64),
                                   'source': 'Current collector mission metadata', 'detail': 'Metadata carried by the current fresh exporter session.'}
                    self.checked_at = now
                    return
            self.worker = threading.Thread(target=self._refresh, args=(identity, self.revision), name='DCS mission metadata', daemon=True)
            self.worker.start()

    def _refresh(self, identity, revision):
        # Log access is off the API thread too. A missing bridge may use only a
        # complete current-process lifecycle; old cached filenames are not used.
        lifecycle = self.log.read(identity[0])
        try:
            result = self.bridge()
            if not isinstance(result, dict) or not _text(result.get('name')):
                raise ValueError('Current mission metadata unavailable')
            result = {key: result.get(key) for key in ('name', 'theatre', 'source', 'detail')}
            result['name'], result['theatre'] = _text(result['name']), _text(result['theatre'], 64)
            if result['theatre'] is None and lifecycle and lifecycle['name'] == result['name']:
                result['theatre'] = lifecycle['theatre']
                result['source'] = (result['source'] or 'Existing DCS bridge') + ' + current DCS log lifecycle'
        except (OSError, ValueError, http.client.HTTPException):
            result = lifecycle or _unavailable('Current mission metadata is unavailable; no filename is inferred from saved mission files.')
        result['status'] = 'Current' if result.get('name') else 'Unavailable'
        with self.lock:
            if not self.stopped and identity == self.identity and revision == self.revision:
                self.result, self.checked_at = result, self.clock()

    def snapshot(self, health, raw=None):
        self.tick(health, raw)
        with self.lock:
            result = copy.deepcopy(self.result)
            age = self.clock() - self.checked_at if self.checked_at is not None else None
            if age is not None and (age < 0 or age > 6):
                return _unavailable('Mission metadata is stale; waiting for a fresh read.', 'Stale')
            result['age_s'] = round(age, 3) if age is not None else None
            return result

    def close(self):
        with self.lock:
            self.stopped = True
            self.revision += 1
            self.identity = None
            self.result = _unavailable('Mission metadata reader stopped.')
