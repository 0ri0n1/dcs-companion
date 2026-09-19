"""Read-only, per-device DCS binding resolution and fail-closed cache invalidation.

This extends the existing dump_binds.lua/build_index.py workflow. No input is sent
here and the original binds/binds.json is never read as current binding truth.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
WEBAPP = ROOT / 'webapp'
BINDING_CATALOGS = {
    'F-22A': {'key': 'f22', 'name': 'F-22A Raptor', 'saved_input': 'F-22A',
              'module_location': 'saved_games', 'module_folder': 'f-22a', 'input_folder': 'F-22A',
              'display_only': False},
    'FA-18C_hornet': {'key': 'f18', 'name': 'F/A-18C Hornet', 'saved_input': 'FA-18C_hornet',
                     'module_location': 'install', 'module_folder': 'FA-18C', 'input_folder': 'FA-18C',
                     'display_only': True},
}
SUPPORTED_AIRCRAFT = tuple(BINDING_CATALOGS)
SAFE_ACTIONS = {
    'f22.nav.next': ('Next Waypoint, Airfield Or Target', 'Advance the current navigation selection; target depends on current mode.'),
    'f22.nav.previous': ('Previous Waypoint, Airfield Or Target', 'Move to the previous navigation selection; target depends on current mode.'),
    'f22.nav.mode': ('(1) Navigation Modes', 'Cycle navigation modes. The initial and resulting submode are not verified by this adapter.'),
}

# Reuse canonical DCS modifier ordering and raw-table handling from legacy code.
_spec = importlib.util.spec_from_file_location('copilot_bind_index', ROOT / 'tools' / 'build_index.py')
_legacy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_legacy)
parse_combos = _legacy.parse_combos


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def path_identity(path):
    """Windows source identity, independent of spelling/case and current directory."""
    return str(Path(path).expanduser().resolve()).replace('\\', '/').casefold()


def resolve_binding_roots(install_root=None, saved_games=None):
    """Explicit fixture roots bypass installation validation; defaults use discovery."""
    from .discovery import SetupUnavailable, resolve_setup_paths
    if any(value is not None and not str(value).strip() for value in (install_root, saved_games)):
        raise SetupUnavailable('DCS installation and Saved Games paths must not be empty.')
    if install_root is not None and saved_games is not None:
        return Path(install_root).expanduser().resolve(), Path(saved_games).expanduser().resolve()
    return resolve_setup_paths(install_root, saved_games)


def category_list(value):
    return [value] if isinstance(value, str) else list(value or [])


def command_signature(action):
    keys = ('down','pressed','up','cockpit_device_id','value_down','value_pressed','value_up','action')
    fields = {k: action[k] for k in keys if action.get(k) is not None}
    return json.dumps(fields, sort_keys=True) if fields else 'name:' + action['name']


def merge_actions(defaults, diff, device_id, scope='aircraft'):
    """Merge by exact action name, preserving unbound controls and axis filters.

    Engine command numbers are not exported by the offline Lua runtime. Symbolic
    command signatures remain recorded; diff-provided numeric IDs are preserved.
    Unknown user-only names are shown but can never enter the command allowlist.
    """
    by_name = {}
    for source in defaults:
        name = source.get('name')
        if not isinstance(name, str):
            continue
        kind = source.get('kind', 'key')
        key = (kind, name)
        combos = parse_combos(source.get('combos'))
        if key in by_name:
            current = by_name[key]
            current['default_combos'] = sorted(set(current['default_combos'] + combos))
            continue
        token = hashlib.sha256(f'{scope}:{kind}:{name}'.encode()).hexdigest()[:16]
        by_name[key] = {
            'id': f'{device_id}:{token}', 'name': name, 'kind': kind,
            'category': category_list(source.get('category')), 'device_id': device_id,
            'scope': scope, 'default_combos': combos, 'user_added': [],
            'user_removed': [], 'user_changed': [], 'conflicts': [],
            'signature': command_signature(source), 'verified_default': True,
            'command_id': None,
        }
    for kind, field in [('key','keyDiffs'), ('axis','axisDiffs')]:
        entries = diff.get(field, {}) or {}
        if not isinstance(entries, dict):
            continue
        for command_id, entry in entries.items():
            name = entry.get('name')
            if not name:
                continue
            key = (kind, name)
            if key not in by_name:
                token = hashlib.sha256(f'{scope}:{kind}:{name}'.encode()).hexdigest()[:16]
                by_name[key] = {'id':f'{device_id}:{token}','name':name,'kind':kind,
                    'category':['User assignment'],'device_id':device_id,'scope':scope,
                    'default_combos':[],'user_added':[],'user_removed':[],
                    'user_changed':[],'conflicts':[],'signature':f'diff:{command_id}',
                    'verified_default':False}
            action = by_name[key]
            action['command_id'] = command_id
            for old,new in [('added','user_added'),('removed','user_removed'),('changed','user_changed')]:
                action[new].extend(parse_combos(entry.get(old)))
            if kind == 'axis':
                action['axis_filters'] = entry.get('changed', [])
    for action in by_name.values():
        removed = set(action['user_removed'])
        # Installed Data.lua protects defaults that became newly conflicting
        # after a module update, otherwise clears combos mentioned by any diff
        # before applying that command's added/removed/changed entries.
        peers=[a for a in by_name.values() if a['kind']==action['kind']]
        mentioned=set(c for a in peers for field in ('user_added','user_removed','user_changed') for c in a[field])
        added_names={c:{a['name'] for a in peers if c in a['user_added']} for c in mentioned}
        removed_names={c:{a['name'] for a in peers if c in a['user_removed']} for c in mentioned}
        updated=any(c in mentioned and (added_names[c] or removed_names[c]) and
                    action['name'] not in added_names[c] | removed_names[c] for c in action['default_combos'])
        effective_defaults=action['default_combos'] if updated else [c for c in action['default_combos'] if c not in mentioned]
        action['default_updated_conflict']=updated
        action['combos'] = list(dict.fromkeys([c for c in effective_defaults if c not in removed] + action['user_added'] + action['user_changed']))
        # DCS Data.lua applyDiffToCommands_ reapplies changed as an assignment.
        action['source'] = 'default+user' if action['user_added'] or removed or action['user_changed'] else 'default'
        if not action['combos']:
            action['source'] = 'unbound'
        action['validation'] = 'static-source-verified' if action['verified_default'] else 'user-only-unverified'
    return sorted(by_name.values(), key=lambda a: (a['name'].lower(), a['kind']))


def detect_conflicts(actions):
    """Device GUID is part of the collision key. Shared names on two devices do not conflict."""
    reverse = {}
    for action in actions:
        action['conflicts'] = []
        for combo in action['combos']:
            reverse.setdefault((action['device_id'], action['kind'], combo), []).append(action)
    conflicts = []
    for (device, kind, combo), group in reverse.items():
        signatures = {a.get('signature', a['name']) for a in group}
        if len(signatures) > 1:
            names = list(dict.fromkeys(a['name'] for a in group))
            row = {'device_id':device,'kind':kind,'combo':combo,'actions':names,
                   'scopes':sorted({a.get('scope','aircraft') for a in group}),
                   'severity':'context-dependent' if any(a.get('scope')=='CommandMenu' for a in group) else 'conflict'}
            conflicts.append(row)
            for action in group:
                action['conflicts'].append(combo)
    return sorted(conflicts, key=lambda x: (x['device_id'], x['combo']))


def source_record(path):
    path = Path(path)
    if not path.is_file():
        return {'path':str(path),'sha256':None,'modified_utc':None,'exists':False}
    stat = path.stat()
    return {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'modified_utc':datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            'size':stat.st_size,'exists':True}


def fingerprint(sources):
    return hashlib.sha256(json.dumps(
        [(s['path'].lower(), s['sha256']) for s in sources], sort_keys=True).encode()).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, indent=2)
    if path.exists():
        old = path.read_bytes()
        # Preserve generated cache before replacement; content-addressed backups.
        backup = path.parent / 'backups' / (path.name + '.' + hashlib.sha256(old).hexdigest()[:12])
        if not backup.exists():
            backup.parent.mkdir(exist_ok=True)
            backup.write_bytes(old)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(data, encoding='utf-8')
    os.replace(temp, path)


class BindingService:
    supported_aircraft = SUPPORTED_AIRCRAFT

    def __init__(self, data_dir=None, install_root=None, saved_games=None, *, aircraft='F-22A', setup_error=None):
        if aircraft not in BINDING_CATALOGS:
            raise ValueError(f'Unsupported binding catalog: {aircraft}')
        self.aircraft = aircraft
        self.catalog = dict(BINDING_CATALOGS[aircraft])
        self.data_dir = Path(data_dir or WEBAPP / 'data')
        self.setup_error = setup_error
        self.install_root = self.saved_games = None
        if not self.setup_error:
            from .discovery import SetupUnavailable
            try:
                self.install_root, self.saved_games = resolve_binding_roots(install_root, saved_games)
            except SetupUnavailable as exc:
                self.setup_error = str(exc)
        self.cache_path = self.data_dir / 'bindings' / (self.catalog['key'] + '-snapshot.json')
        self.allowlist_path = self.data_dir / 'bindings' / (self.catalog['key'] + '-allowlist.json')
        self._lock = threading.RLock()
        self._snapshot = None
        self._device_history = {}
        self._log_census_cache = {}
        if self.cache_path.is_file():
            try:
                cached = json.loads(self.cache_path.read_text(encoding='utf-8'))
                if self._cache_matches_setup(cached):
                    self._snapshot = cached
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        self._restore_device_history()

    @property
    def setup_identity(self):
        if self.setup_error:
            return None
        return {'install_root': path_identity(self.install_root),
                'saved_games': path_identity(self.saved_games),
                'module_input_root': path_identity(self.module_root)}

    def _cache_matches_setup(self, cached):
        if self.setup_error or not isinstance(cached, dict) or cached.get('aircraft') != self.aircraft:
            return False
        identity = cached.get('setup_identity')
        if identity is not None:
            return identity == self.setup_identity
        # Earlier generations predate explicit identity. Their source manifest
        # must independently prove BOTH the installation and the user profile.
        sources = cached.get('sources')
        if not isinstance(sources, list):
            return cached.get('source_fingerprint') == fingerprint(self.current_sources())
        paths = {path_identity(s['path']) for s in sources if isinstance(s, dict) and s.get('path')}
        expected = {path_identity(self.install_root/'autoupdate.cfg'),
                    path_identity(self.saved_games/'Logs'/'dcs.log') + '#input-device-identities'}
        module_prefix = path_identity(self.module_root) + '/'
        return expected <= paths and any(p.startswith(module_prefix) for p in paths)

    def _restore_device_history(self):
        """Recover observed identities, never assignments, from this catalog only."""
        if self.setup_error:
            return
        paths = [self.cache_path]
        backup_dir = self.cache_path.parent / 'backups'
        if backup_dir.is_dir():
            paths += sorted(backup_dir.glob(self.cache_path.name + '.*'),
                            key=lambda p: p.stat().st_mtime, reverse=True)[:16]
        expected = str(self.saved_games/'Logs'/'dcs.log') + '#INPUT-device-identities'
        module_prefix = str(self.module_root).replace('\\', '/').lower() + '/'
        for path in paths:
            try:
                if not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
                    continue
                raw = path.read_bytes()
                cached = json.loads(raw)
                if cached.get('schema') != 'dcs-companion/bindings/1' or cached.get('aircraft') != self.aircraft:
                    continue
                if not self._cache_matches_setup(cached):
                    continue
                sources = cached.get('sources') or []
                if fingerprint(sources) != cached.get('source_fingerprint'):
                    continue
                if not any(str(s.get('path', '')).replace('\\', '/').lower().startswith(module_prefix) for s in sources):
                    continue
                proof = next((s for s in sources if s.get('path') == expected and s.get('sha256')), None)
                if not proof:
                    continue
                for record in cached.get('observed_devices', []):
                    if isinstance(record, dict) and self._valid_device_name(record.get('name')) and record.get('source_sha256'):
                        self._device_history.setdefault(record['name'], copy.deepcopy(record))
                # Migrate earlier caches that had no explicit observation ledger.
                for device in cached.get('devices', []):
                    name = device.get('name')
                    if device.get('status') == 'Last seen in DCS; defaults' and self._valid_device_name(name):
                        self._device_history.setdefault(name, {'name': name, 'source_path': expected,
                            'source_sha256': proof['sha256'], 'observed_utc': cached.get('generated_utc'),
                            'evidence_cache_path': str(path), 'evidence_cache_sha256': hashlib.sha256(raw).hexdigest(),
                            'method': 'Device identity retained from this aircraft catalog and its recorded DCS log census.'})
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                continue

    @staticmethod
    def _valid_device_name(name):
        return isinstance(name, str) and len(name) <= 256 and bool(re.fullmatch(
            r'[^\r\n]+ \{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}', name))

    def inherit_device_history(self, other):
        """Carry evidence into an isolated rebuild; never copy bindings or commands."""
        if self.setup_error or other.setup_error or self.aircraft != other.aircraft or self.setup_identity != other.setup_identity:
            raise ValueError('Device history belongs to a different aircraft, installation or Saved Games directory.')
        with other._lock:
            other.last_seen_devices()
            history = copy.deepcopy(other._device_history)
        with self._lock:
            for name, record in history.items():
                self._device_history.setdefault(name, record)

    @property
    def module_root(self):
        if self.setup_error:
            return None
        parent = self.saved_games if self.catalog['module_location'] == 'saved_games' else self.install_root
        return parent / 'Mods' / 'aircraft' / self.catalog['module_folder'] / 'Input' / self.catalog['input_folder']

    @property
    def user_input_root(self):
        if self.setup_error:
            return None
        return self.saved_games / 'Config' / 'Input' / self.catalog['saved_input']

    @property
    def cockpit_root(self):
        return self.module_root.parents[1] / 'Cockpit' / 'Scripts' if self.module_root else None

    def source_paths(self):
        if self.setup_error:
            return []
        roots = [self.module_root, self.install_root / 'Config' / 'Input' / 'Aircrafts',
                 self.install_root / 'Config' / 'Input' / 'UiLayer',
                 self.install_root / 'Config' / 'Input' / 'VoiceChat',
                 self.install_root / 'Config' / 'Input' / 'CommandMenu']
        user = self.saved_games / 'Config' / 'Input'
        roots += [self.user_input_root] + [user / n for n in ('Default','UiLayer','VoiceChat','CommandMenu')]
        if self.aircraft == 'FA-18C_hornet':
            roots.append(self.install_root / 'Config' / 'Input' / 'Supercarrier' / 'Input')
        paths = {p for root in roots if root.is_dir() for p in root.rglob('*.lua')}
        paths.update([self.install_root / 'autoupdate.cfg',
            self.install_root / 'Scripts' / 'Input' / 'DefaultAssignments.lua',
            self.install_root / 'Scripts' / 'Input' / 'Data.lua',
            user / 'wizard.lua', user / 'modifiers.lua', user / 'disabled.lua',
            WEBAPP / 'tools' / 'extract_bindings.lua', ROOT / 'tools' / 'build_index.py',
            Path(__file__), WEBAPP / 'backend' / 'adapters.py',
            self.cockpit_root/'command_defs.lua', self.cockpit_root/'devices.lua'])
        if self.aircraft == 'F-22A':
            paths.add(self.cockpit_root/'Systems'/'ICP_System.lua')
        return sorted(paths, key=lambda p: str(p).lower())

    def current_sources(self):
        if self.setup_error:
            return []
        sources = [source_record(p) for p in self.source_paths()]
        # Log text changes during flight; only the relevant device census is a
        # binding dependency. Newly enumerated identities invalidate the cache.
        log = self.saved_games/'Logs'/'dcs.log'
        names = self.last_seen_devices()
        sources.append({'path':str(log)+'#INPUT-device-identities',
            'sha256':hashlib.sha256(json.dumps(names).encode()).hexdigest(),
            'modified_utc':None,'exists':log.is_file(),
            'method':'SHA256 of sorted current/previous DCS log identities plus retained observed-device evidence; connection state remains Unknown'})
        return sources

    def last_seen_devices(self):
        if self.setup_error:
            return []
        with self._lock:
            for filename in ('dcs.log', 'dcs.log.old'):
                log = self.saved_games/'Logs'/filename
                try:
                    before = self._log_signature(log.stat())
                    if self._log_census_cache.get(filename) == before:
                        continue
                    raw = log.read_bytes()
                except OSError:
                    # Disappearance/read failure must not make a future file at
                    # the same path look already processed. Retain only history.
                    self._log_census_cache.pop(filename, None)
                    continue
                names = re.findall(r'INPUT[^\n]*created \[[^\n]+?\] with full id \[([^\n]+? \{[0-9A-Fa-f-]+\})\]', raw.decode('utf-8', errors='replace'))
                digest = None
                for name in names:
                    if self._valid_device_name(name) and name not in self._device_history:
                        digest = digest or hashlib.sha256(raw).hexdigest()
                        self._device_history[name] = {'name': name, 'source_path': str(log),
                            'source_sha256': digest, 'observed_utc': utc_now(),
                            'method': 'DCS INPUT created/full-id record; historical observation, not live connection.'}
                try:
                    after = self._log_signature(log.stat())
                except OSError:
                    after = None
                if after == before:
                    self._log_census_cache[filename] = after
                else:
                    # Rotation or append during read: inspect again next call.
                    self._log_census_cache.pop(filename, None)
            return sorted(self._device_history)

    @staticmethod
    def _log_signature(stat):
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

    def safe_command_ids(self):
        """Resolve the three eligible numeric commands from installed mod code.

        Unlike the legacy name merge, eligibility additionally checks a diff's
        DCS command ID. A stale or relabelled diff must not authorize another action.
        """
        if self.setup_error or self.aircraft != 'F-22A':
            return {}
        cockpit=self.cockpit_root
        definitions=(cockpit/'command_defs.lua').read_text(encoding='utf-8')
        icp=(cockpit/'Systems'/'ICP_System.lua').read_text(encoding='utf-8')
        patterns=[('f22.nav.next',definitions,r'^\s*PlaneChangeTarget\s*=\s*(\d+)'),
                  ('f22.nav.mode',definitions,r'^\s*PlaneModeNAV\s*=\s*(\d+)'),
                  ('f22.nav.previous',icp,r'^\s*local\s+PrevSteer\s*=\s*(\d+)')]
        out={}
        for action_id,text,pattern in patterns:
            values=set(re.findall(pattern,text,re.M))
            if len(values)==1:
                out[action_id]='d'+values.pop()+'pnilunilcdnilvdnilvpnilvunil'
        return out

    def check_current(self):
        """SHA256 every watched source, including additions/deletions; never rerun Lua here."""
        with self._lock:
            unavailable = self.setup_error
            if not unavailable and not (self.module_root/'keyboard'/'default.lua').is_file():
                unavailable = f'{self.aircraft} keyboard defaults are not installed in the selected setup. Install this module or choose another aircraft.'
            if unavailable:
                return {'valid':False, 'source_fingerprint':None, 'current_fingerprint':None,
                        'status':'Unavailable', 'reason':unavailable, 'rebuild_eligible':False}
            current = fingerprint(self.current_sources())
            valid = bool(self._snapshot and self._snapshot.get('source_fingerprint') == current
                         and self._snapshot.get('aircraft', self.aircraft) == self.aircraft)
            return {'valid':valid,'source_fingerprint':self._snapshot.get('source_fingerprint') if self._snapshot else None,
                    'current_fingerprint':current,'status':'Current' if valid else 'Invalidated',
                    'rebuild_eligible':True}

    def snapshot(self, aircraft=None):
        aircraft = self.aircraft if aircraft is None else aircraft
        with self._lock:
            if aircraft != self.aircraft:
                return {'aircraft':aircraft,'status':'Unsupported','devices':[],'actions':[],
                        'conflicts':[],'source_fingerprint':None,'reason':'No binding inheritance across aircraft.'}
            check = self.check_current()
            owned = self._snapshot if self._snapshot and self._snapshot.get('aircraft', self.aircraft) == self.aircraft else None
            result = copy.deepcopy(owned or {'aircraft':self.aircraft,'devices':[],'actions':[],'conflicts':[]})
            result.update(check)
            if check['status'] == 'Unavailable':
                result.update(devices=[], actions=[], conflicts=[], safe_actions=[], observed_devices=[])
            result['setup_identity'] = self.setup_identity
            result['catalog'] = dict(self.catalog)
            result['aircraft'] = self.aircraft
            result['display_only'] = self.catalog['display_only']
            if self.catalog['display_only']:
                result['safe_actions'] = []
            result['allowlist_path'] = str(self.allowlist_path)
            result['physical_input'] = {'status':'Unavailable','reason':'No verified Windows Raw Input listener is installed by this MVP.'}
            return result

    def get_safe_actions(self, aircraft=None):
        aircraft = self.aircraft if aircraft is None else aircraft
        if aircraft != self.aircraft or self.catalog['display_only'] or not self.check_current()['valid']:
            return []
        return copy.deepcopy(self._snapshot.get('safe_actions', []))

    def _extract(self, path, device='Keyboard'):
        if self.setup_error:
            raise RuntimeError(self.setup_error)
        helper = WEBAPP / 'tools' / 'extract_bindings.lua'
        with tempfile.TemporaryDirectory(prefix='dcs-bindings-') as tmp:
            out = Path(tmp) / 'extracted.json'
            args = [str(self.install_root/'bin'/'luae.exe'),str(helper),str(self.install_root),
                    str(path),device,str(self.saved_games/'Config'/'Input'/'wizard.lua'),str(out)]
            run = subprocess.run(args, capture_output=True, text=True, timeout=30,
                                 creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if run.returncode or not out.is_file():
                raise RuntimeError(f'Binding extraction failed for {path}: {run.stdout} {run.stderr}')
            return json.loads(out.read_text(encoding='utf-8'))

    def _diff(self, path, device):
        return self._extract(path, device)['raw'] if path.is_file() else {}

    def device_specs(self):
        """Configured and observed DCS identities; no inferred physical connection."""
        if self.setup_error:
            return []
        specs = {'keyboard': {'id':'keyboard', 'name':'Keyboard', 'dtype':'keyboard',
                             'guid':None, 'diff':None, 'configured':True, 'aliases':['Keyboard']}}
        configured = sorted((self.user_input_root/'joystick').glob('*.diff.lua'),
                            key=lambda p: (p.name.casefold(), p.name))
        candidates = [(p.name[:-9], p) for p in configured]
        candidates += [(name, None) for name in self.last_seen_devices()]
        for name, diff_path in candidates:
            match = re.search(r'\{([0-9A-Fa-f-]+)\}$', name)
            guid = match.group(1).upper() if match else None
            device_id = 'joystick:' + (guid or name.casefold())
            prior = specs.get(device_id)
            if prior:
                if name not in prior['aliases']:
                    prior['aliases'].append(name)
                if diff_path is not None and prior['diff'] != diff_path:
                    prior['ambiguous'] = True
                continue
            specs[device_id] = {'id':device_id, 'name':name, 'dtype':'joystick', 'guid':guid,
                               'diff':diff_path, 'configured':diff_path is not None,
                               'aliases':[name]}
        return list(specs.values())

    def refresh(self):
        """Explicit local rebuild. Never writes DCS files or the legacy index."""
        with self._lock:
            if self.setup_error:
                raise RuntimeError(self.setup_error)
            before = self.current_sources()
            if not (self.module_root/'keyboard'/'default.lua').is_file():
                raise RuntimeError(f'{self.aircraft} defaults are not installed; no fallback aircraft will be used.')
            user = self.saved_games/'Config'/'Input'
            disabled = self._diff(user/'disabled.lua','Keyboard').get('devices',{})
            if not isinstance(disabled, dict): disabled = {}
            actions,devices = [],[]
            for spec in self.device_specs():
                name, dtype, diff_path = spec['name'], spec['dtype'], spec['diff']
                guid, device_id = spec['guid'], spec['id']
                generic = re.sub(r' \{[0-9A-Fa-f-]+\}$','',name)
                kind = 'tartarus' if 'tartarus' in name.lower() else 'hotas' if 'hotas' in name.lower() else dtype
                devices.append({'id':device_id,'name':name,'type':kind,'guid':guid,
                    'aliases':spec['aliases'],
                    'status':'Ambiguous saved profiles' if spec.get('ambiguous') else 'Disabled' if any(disabled.get(alias) for alias in spec['aliases']) else 'Configured' if spec['configured'] else 'Last seen in DCS; defaults',
                    'connected':'Unknown','physical_mapping_status':'Unknown' if kind=='tartarus' else 'DCS control identifiers',
                    'notes':'DCS JOY numbers are verified; Synapse physical keycap/keyboard emission mapping is Unknown.' if kind=='tartarus' else None})
                if spec.get('ambiguous'):
                    devices[-1]['notes'] = 'Multiple saved filenames identify this same DCS GUID. Assignments are withheld until the active device profile is resolved.'
                    continue
                if name in self._device_history:
                    devices[-1]['observation'] = copy.deepcopy(self._device_history[name])
                default_path = self.module_root/dtype/(generic+'.lua')
                if not default_path.is_file(): default_path=self.module_root/dtype/'default.lua'
                defaults = self._extract(default_path,name)['actions']
                diff_path = diff_path or self.user_input_root/dtype/(name+'.diff.lua')
                actions += merge_actions(defaults,self._diff(diff_path,name),device_id)
                # UI and radio/voice layers are additional contexts in DCS, not another aircraft.
                for layer in ('UiLayer','VoiceChat','CommandMenu'):
                    layer_path=self.install_root/'Config'/'Input'/layer/dtype/'default.lua'
                    if not layer_path.is_file(): continue
                    layer_actions=self._extract(layer_path,name)['actions']
                    layer_diff=user/layer/dtype/(name+'.diff.lua')
                    actions += merge_actions(layer_actions,self._diff(layer_diff,name),device_id,layer)
            conflicts=detect_conflicts(actions)
            for action in actions:
                action['aircraft'] = self.aircraft
            sources=self.current_sources()
            if fingerprint(before)!=fingerprint(sources):
                raise RuntimeError('Binding sources changed during extraction; cache was not published. Retry refresh.')
            source_hash=fingerprint(sources)
            safe=[]
            expected_ids=self.safe_command_ids()
            keyboard_enabled = not disabled.get('Keyboard')
            for action_id,(name,expectation) in (SAFE_ACTIONS.items() if self.aircraft == 'F-22A' else []):
                match=next((a for a in actions if a['name']==name and a['device_id']=='keyboard' and a['scope']=='aircraft'),None)
                if not match or not match['verified_default'] or not keyboard_enabled: continue
                expected_id=expected_ids.get(action_id)
                if not expected_id or match['command_id'] not in (None,expected_id): continue
                available=[c for c in match['combos'] if c not in match['conflicts']]
                if available:
                    safe.append({'id':action_id,'name':name,'combo':available[0],'risk':'benign','aircraft':'F-22A',
                        'observable':False,'requires_display':False,'expected_result':expectation,
                        'bindings_fingerprint':source_hash,'validation':'static-verified-runtime-unverified',
                        'command_id':expected_id,'device_id':'keyboard'})
            version=json.loads((self.install_root/'autoupdate.cfg').read_text(encoding='utf-8'))['version']
            result={'schema':'dcs-companion/bindings/1','aircraft':self.aircraft,'status':'Current',
                'setup_identity':self.setup_identity,
                'generated_utc':utc_now(),'dcs_version':version,'source_fingerprint':source_hash,
                'sources':sources,'devices':devices,'actions':actions,
                'observed_devices':[copy.deepcopy(self._device_history[name]) for name in sorted(self._device_history)],
                'conflicts':[c for c in conflicts if c['severity']=='conflict'],
                'contextual_overlaps':[c for c in conflicts if c['severity']=='context-dependent'],
                'safe_actions':safe,'modifiers':self._diff(self.user_input_root/'modifiers.lua','Keyboard'),
                'catalog':dict(self.catalog),'display_only':self.catalog['display_only'],
                'module_input_root':str(self.module_root),'saved_input_root':str(self.user_input_root),
                'authority':'Installed module and shared defaults + per-device Saved Games diffs. HTML is never read.',
                'validation':'Static source resolution only; aircraft behavior and physical input are unverified.',
                'counts':{'actions_total':len(actions),'actions_bound':sum(bool(a['combos']) for a in actions),
                          'devices':len(devices),'conflicting_combos':sum(c['severity']=='conflict' for c in conflicts),
                          'contextual_overlaps':sum(c['severity']=='context-dependent' for c in conflicts)}}
            allowlist={'schema':'dcs-copilot/binds/1','aircraft':self.aircraft,'source_fingerprint':source_hash,
                'setup_identity':self.setup_identity,
                'generated_utc':result['generated_utc'],'conflicts':{},
                'actions':[{'name':a['name'],'combos':[a['combo']],'category':['Navigation'],'source':'verified-source'} for a in safe],
                'by_combo':{a['combo']:[a['name']] for a in safe}}
            atomic_json(self.allowlist_path,allowlist)
            atomic_json(self.cache_path,result)
            self._snapshot=result
            return self.snapshot()
