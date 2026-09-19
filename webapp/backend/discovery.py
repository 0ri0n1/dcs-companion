"""Bounded, read-only DCS setup discovery; saved devices are not connection proof.

Explicit overrides never fall back silently. Candidate IDs identify normalized
paths without embedding paths in IDs. No Lua/profile file is ever executed.
"""
from __future__ import annotations

import copy
import ctypes
from functools import lru_cache
import hashlib
import itertools
import math
import os
from pathlib import Path, PureWindowsPath
import re
import threading
import time
import uuid


MAX_CANDIDATES = 128
MAX_DIRECTORY_ENTRIES = 1024
MAX_DIFF_FILES = 10000
MAX_TEXT_BYTES = 2 * 1024 * 1024


class SetupUnavailable(ValueError):
    """No unique, validated installation and profile could be selected."""


def _path(value):
    if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        return None
    try:
        return Path(value).expanduser().resolve()
    except (OSError, ValueError, RuntimeError):
        return None


def _key(path):
    return str(path).replace('\\', '/').rstrip('/').casefold()


def _identifier(kind, path):
    return kind + '-' + hashlib.sha256(_key(path).encode('utf-8')).hexdigest()[:20]


def _is_install(path):
    try:
        return bool(path and (path/'Config'/'Input').is_dir() and
                    any((path/folder/'DCS.exe').is_file() for folder in ('bin', 'bin-mt')))
    except OSError:
        return False


def install_from_executable(exe):
    """Validate one process executable without registry or inventory scans."""
    path = _path(exe)
    if not path or path.name.casefold() != 'dcs.exe' or path.parent.name.casefold() not in ('bin', 'bin-mt'):
        return None
    root = path.parent.parent
    return root if path.is_file() and _is_install(root) else None


def _is_profile(path):
    try:
        return bool(path and (path/'Config').is_dir() and
                    ((path/'Config'/'Input').is_dir() or (path/'Config'/'options.lua').is_file()))
    except OSError:
        return False


class _KnownFolderGUID(ctypes.Structure):
    _fields_ = [('Data1', ctypes.c_uint32), ('Data2', ctypes.c_uint16),
                ('Data3', ctypes.c_uint16), ('Data4', ctypes.c_ubyte * 8)]


@lru_cache(maxsize=1)
def _windows_saved_games_known_folder():
    """Resolve this user's shell path once per companion process, not per frame.

    Only the folder location is cached; process identity, selected profile and
    binding source checks remain live. Restart after redirecting Saved Games in
    Windows. Explicit discovery resolvers bypass this cache entirely.
    """
    output = ctypes.c_wchar_p()
    release = None
    try:
        # Private DLL/function wrappers avoid concurrently changing argtypes on
        # ctypes.windll's shared function object. GUID has one stable definition.
        shell = ctypes.WinDLL('shell32', use_last_error=True)
        ole = ctypes.WinDLL('ole32', use_last_error=True)
        function = shell.SHGetKnownFolderPath
        function.argtypes = [ctypes.POINTER(_KnownFolderGUID), ctypes.c_uint32,
                             ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
        function.restype = ctypes.c_int32
        release = ole.CoTaskMemFree
        release.argtypes, release.restype = [ctypes.c_void_p], None
        guid = _KnownFolderGUID.from_buffer_copy(uuid.UUID('4C5C32FF-BB9D-43B0-B5B4-2D72E54EAAA4').bytes_le)
        # KF_FLAG_DONT_VERIFY avoids potentially blocking filesystem/network
        # verification by the shell. _is_profile still validates actual roots.
        # https://learn.microsoft.com/windows/win32/api/shlobj_core/ne-shlobj_core-known_folder_flag
        if function(ctypes.byref(guid), 0x4000, None, ctypes.byref(output)) == 0 and output.value:
            return _path(output.value)
    except (OSError, ValueError, AttributeError):
        pass
    finally:
        if output and release is not None:
            release(ctypes.cast(output, ctypes.c_void_p))
    return None


def saved_games_known_folder(home=None, known_folder=None):
    """Respect redirected Windows Saved Games; fall back without creating it.

    FOLDERID_SavedGames: Microsoft Learn /windows/win32/shell/knownfolderid.
    """
    if known_folder is not None:
        try:
            resolved = _path(known_folder() if callable(known_folder) else known_folder)
            if resolved:
                return resolved
        except (OSError, ValueError):
            pass
    elif os.name == 'nt':
        resolved = _windows_saved_games_known_folder()
        if resolved:
            return resolved
    return _path(home or Path.home())/'Saved Games'


def _entries(path, issues):
    """Only one directory level; stop before an unexpected huge tree is scanned."""
    try:
        result = []
        with os.scandir(path) as iterator:
            for entry in iterator:
                if len(result) >= MAX_DIRECTORY_ENTRIES:
                    issues.append(f'Directory inventory is limited to {MAX_DIRECTORY_ENTRIES} entries: {path}')
                    break
                result.append(Path(entry.path))
        return sorted(result, key=lambda item: item.name.casefold())
    except OSError:
        return []


def _read_text(path, maximum=MAX_TEXT_BYTES):
    with Path(path).open('rb') as handle:
        value = handle.read(maximum+1)
    if len(value) > maximum:
        raise ValueError('Discovery text file exceeds its size bound')
    return value.decode('utf-8-sig', errors='strict')


def _vdf(text):
    """Constrained Valve KeyValues parser: data only, bounded depth and tokens."""
    token = re.compile(r'\s+|//[^\r\n]*|"((?:\\.|[^"\\])*)"|([{}])|([^\s{}"]+)')
    values, offset = [], 0
    for match in token.finditer(text):
        if match.start() != offset:
            raise ValueError('Invalid Valve data')
        offset = match.end()
        if match[1] is not None:
            value = re.sub(r'\\([\\"])', r'\1', match[1])
            values.append(('value', value))
        elif match[2]: values.append((match[2], match[2]))
        elif match[3]: values.append(('value', match[3]))
        if len(values) > 20000:
            raise ValueError('Valve data token limit reached')
    if offset != len(text):
        raise ValueError('Incomplete Valve data')
    cursor = 0
    def table(depth=0, nested=False):
        nonlocal cursor
        if depth > 12:
            raise ValueError('Valve data nesting limit reached')
        result = {}
        while cursor < len(values):
            kind, key = values[cursor]
            cursor += 1
            if kind == '}' and nested:
                return result
            if kind != 'value' or cursor >= len(values):
                raise ValueError('Invalid Valve key')
            kind, value = values[cursor]
            cursor += 1
            if kind == '{': value = table(depth+1, True)
            elif kind != 'value': raise ValueError('Invalid Valve value')
            if key.casefold() in result:
                raise ValueError('Duplicate Valve key')
            result[key.casefold()] = value
        if nested:
            raise ValueError('Unclosed Valve data')
        return result
    return table()


def _registry_sources():
    """Current-user/machine product locations, never other users' hives."""
    try:
        import winreg
    except ImportError:
        return [], []
    installs, steam = [], []
    views = (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY)
    for hive_name in ('HKEY_CURRENT_USER', 'HKEY_LOCAL_MACHINE'):
        hive = getattr(winreg, hive_name)
        for view in views:
            def opened(name):
                return winreg.OpenKey(hive, name, 0, winreg.KEY_READ | view)
            try:
                with opened(r'SOFTWARE\Eagle Dynamics') as key:
                    for index in range(64):
                        try: product = winreg.EnumKey(key, index)
                        except OSError: break
                        if 'dcs' not in product.casefold(): continue
                        with opened('SOFTWARE\\Eagle Dynamics\\' + product) as product_key:
                            for field in ('Path', 'InstallPath', 'InstallLocation'):
                                try:
                                    value = winreg.QueryValueEx(product_key, field)[0]
                                    if isinstance(value, str): installs.append({'path': value, 'evidence': f'{hive_name} Eagle Dynamics {product}'})
                                except OSError: pass
            except OSError: pass
            try:
                with opened(r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall') as key:
                    for index in range(1024):
                        try: product = winreg.EnumKey(key, index)
                        except OSError: break
                        try:
                            with opened('SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\' + product) as product_key:
                                name = winreg.QueryValueEx(product_key, 'DisplayName')[0]
                                if isinstance(name, str) and name.casefold().startswith('dcs world'):
                                    value = winreg.QueryValueEx(product_key, 'InstallLocation')[0]
                                    if isinstance(value, str): installs.append({'path': value, 'evidence': f'{hive_name} uninstall record {name}'})
                        except OSError: pass
            except OSError: pass
            try:
                with opened(r'SOFTWARE\Valve\Steam') as key:
                    for field in ('SteamPath', 'InstallPath'):
                        try:
                            value = winreg.QueryValueEx(key, field)[0]
                            if isinstance(value, str): steam.append(value)
                        except OSError: pass
            except OSError: pass
    return installs[:MAX_CANDIDATES], steam[:MAX_CANDIDATES]


def _steam_candidates(roots, issues):
    libraries, result = {}, []
    for value in list(roots)[:MAX_CANDIDATES]:
        root = _path(value)
        if not root: continue
        libraries[_key(root)] = root
        try:
            folders = _vdf(_read_text(root/'steamapps'/'libraryfolders.vdf')).get('libraryfolders', {})
            if not isinstance(folders, dict): continue
            for key, item in folders.items():
                if not key.isdecimal(): continue
                path = _path(item.get('path') if isinstance(item, dict) else item)
                if path and len(libraries) < MAX_CANDIDATES: libraries[_key(path)] = path
        except FileNotFoundError: pass
        except (OSError, ValueError, UnicodeError): issues.append(f'Steam library metadata could not be read: {root}')
    for library in libraries.values():
        manifest = library/'steamapps'/'appmanifest_223750.acf'
        try:
            app = _vdf(_read_text(manifest)).get('appstate', {})
            folder = app.get('installdir') if isinstance(app, dict) and app.get('appid') == '223750' else None
            if not isinstance(folder, str) or not folder or any(char in folder for char in '/\\') or folder in ('.', '..'):
                raise ValueError('Invalid DCS Steam install directory')
            result.append({'path': library/'steamapps'/'common'/folder, 'evidence': f'Steam DCS manifest {manifest}'})
        except FileNotFoundError: pass
        except (OSError, ValueError, UnicodeError): issues.append(f'DCS Steam manifest could not be read: {manifest}')
        result.append({'path': library/'steamapps'/'common'/'DCSWorld', 'evidence': 'Conventional Steam DCS directory'})
        result.append({'path': library/'steamapps'/'common'/'DCS World Steam Edition', 'evidence': 'Conventional Steam DCS directory'})
    return result


def _processes():
    try:
        import psutil
        result = []
        # Do not populate/read Process.info: psutil shares cached Process
        # instances with concurrent telemetry and setup censuses.
        for count, process in enumerate(psutil.process_iter()):
            try:
                info = process.as_dict(attrs=['name', 'pid'])
            except psutil.NoSuchProcess:
                continue
            if (info.get('name') or '').casefold() == 'dcs.exe':
                try:
                    info.update(exe=process.exe(), cmdline=process.cmdline(), create_time=process.create_time())
                except psutil.Error:
                    info.update(exe=None, cmdline=None, create_time=None)
                result.append(info)
            if len(result) >= 17 or count >= 4095: break
        return result
    except (OSError, ImportError):
        return []


def _profile_for_process(process, root, saved_roots, issues):
    args = process.get('cmdline')
    if not isinstance(args, (list, tuple)) or not all(isinstance(arg, str) for arg in args):
        issues.append('A running DCS process has unreadable launch arguments; its profile is unknown.')
        return []
    values = []
    for index, arg in enumerate(args):
        if arg.casefold() == '-w':
            if index+1 >= len(args):
                issues.append('A running DCS process has an incomplete -w profile argument.')
                return []
            values.append(args[index+1])
        elif arg.casefold().startswith('-w='): values.append(arg[3:])
    if len(values) > 1:
        issues.append('A running DCS process specifies multiple -w profiles; no profile is inferred.')
        return []
    evidence = 'Running DCS -w profile argument'
    if not values:
        variant = root/'dcs_variant.txt'
        try:
            name = _read_text(variant, 256).strip()
            evidence = 'Running DCS installation dcs_variant.txt'
        except FileNotFoundError:
            name, evidence = 'DCS', 'Running DCS default profile (no -w or dcs_variant.txt)'
        except (OSError, ValueError, UnicodeError):
            issues.append('The running DCS profile variant cannot be read.')
            return []
    else: name = values[0].strip().strip('"')
    if not name or '\x00' in name:
        issues.append('The running DCS profile name is empty or invalid.')
        return []
    if Path(name).is_absolute() or PureWindowsPath(name).is_absolute():
        return [(_path(name), evidence)]
    if name in ('.', '..') or any(char in name for char in '/\\:'):
        issues.append('The running DCS -w value is neither a profile name nor an absolute path.')
        return []
    return [(folder/name, evidence) for folder in saved_roots]


def identify_running_setup(process_snapshot=None, *, profile_roots=None, home=None, known_folder=None):
    """Identify current process roots without registry, Steam, or inventory work.

    Unknown arguments, multiple processes, or missing paths cannot establish a
    unique active setup. Process identity matches the telemetry PID:creation-time
    format, including process creation time to prevent PID reuse confusion.
    """
    values = _processes() if process_snapshot is None else process_snapshot() if callable(process_snapshot) else process_snapshot
    processes = [item for item in itertools.islice(values or [], 17) if isinstance(item, dict) and
                 isinstance(item.get('name') or Path(item.get('exe') or '').name, str) and
                 (item.get('name') or Path(item.get('exe') or '').name).casefold() == 'dcs.exe']
    if not processes:
        roots = []
    elif profile_roots is not None:
        roots = [_path(value) for value in itertools.islice(profile_roots, MAX_CANDIDATES)]
    else:
        roots = [saved_games_known_folder(home, known_folder)]
    roots = list({_key(value): value for value in roots if value}.values())
    sessions, issues = [], []
    for process in processes:
        root = install_from_executable(process.get('exe'))
        valid = []
        if root:
            valid = [_path(path) for path, _ in _profile_for_process(process, root, roots, issues) if _is_profile(path)]
        else:
            issues.append('A running DCS executable cannot be validated.')
        unique_profiles = {_key(path): path for path in valid}
        selected = next(iter(unique_profiles.values())) if len(unique_profiles) == 1 else None
        stamp, pid = process.get('create_time'), process.get('pid')
        identity = f'{pid}:{stamp}' if (type(pid) is int and pid > 0 and isinstance(stamp, (int, float)) and
                                      not isinstance(stamp, bool) and math.isfinite(stamp) and stamp > 0) else None
        sessions.append({'pid': pid if type(pid) is int else None, 'process_identity': identity,
                         'install_path': str(root) if root else None, 'profile_path': str(selected) if selected else None})
        if root and selected is None:
            issues.append('The running DCS profile cannot be uniquely established.')
    unique = len(sessions) == 1 and bool(sessions[0]['install_path'] and sessions[0]['profile_path'])
    if len(sessions) > 1: issues.append('Multiple DCS processes are running; active setup is ambiguous.')
    selected = sessions[0] if unique else {}
    return {'running': bool(sessions), 'unique': unique, 'ambiguous': bool(sessions) and not unique,
            'install_path': selected.get('install_path'), 'profile_path': selected.get('profile_path'),
            'process_identity': selected.get('process_identity'), 'sessions': sessions,
            'issues': list(dict.fromkeys(issues))}


def _inventory(install, profile, issues):
    installed, saved, devices = [], [], {}
    module_roots = []
    if install:
        module_roots.extend(((install/'Mods'/'aircraft', 'Installed DCS module'),
                             (install/'CoreMods'/'aircraft', 'Installed DCS core module')))
    if profile: module_roots.append((profile/'Mods'/'aircraft', 'Saved Games aircraft mod'))
    for modules, source in module_roots:
        for module in _entries(modules, issues):
            if not module.is_dir(): continue
            for inputs in _entries(module/'Input', issues):
                if inputs.is_dir() and any((inputs/kind).is_dir() for kind in ('keyboard', 'joystick', 'mouse')):
                    installed.append({'id': _identifier('aircraft', inputs), 'name': inputs.name, 'module': module.name,
                                      'input_name': inputs.name, 'path': str(inputs), 'source': source})
    diff_count = 0
    if profile:
        for aircraft in _entries(profile/'Config'/'Input', issues):
            if not aircraft.is_dir(): continue
            configured, count = set(), 0
            for kind in _entries(aircraft, issues):
                if not kind.is_dir(): continue
                for file in _entries(kind, issues):
                    if not file.is_file() or not file.name.casefold().endswith('.diff.lua'): continue
                    if diff_count >= MAX_DIFF_FILES:
                        issues.append(f'Saved input inventory reached the {MAX_DIFF_FILES}-file limit.')
                        return {'installed_aircraft': installed, 'saved_aircraft': saved, 'devices': list(devices.values()),
                                'counts': {'installed_aircraft': len(installed), 'saved_aircraft': len(saved), 'devices': len(devices), 'diff_files': diff_count}, 'truncated': True}
                    stem = file.name[:-9]
                    match = re.search(r'\{([0-9a-fA-F-]{36})\}$', stem)
                    guid = None
                    if match:
                        try: guid = str(uuid.UUID(match[1])).upper()
                        except ValueError: pass
                    name = stem[:match.start()].rstrip() if guid else stem
                    identity = kind.name.casefold() + ':' + (guid or name.casefold())
                    device = devices.setdefault(identity, {'id': 'device-'+hashlib.sha256(identity.encode()).hexdigest()[:20],
                        'name': name, 'guid': guid, 'type': kind.name, 'connection': 'Unknown', 'source': 'Saved configuration',
                        'aircraft': [], 'diff_files': []})
                    if aircraft.name not in device['aircraft']: device['aircraft'].append(aircraft.name)
                    device['diff_files'].append({'aircraft': aircraft.name, 'path': str(file)})
                    configured.add(identity)
                    count, diff_count = count+1, diff_count+1
            saved.append({'id': _identifier('saved-aircraft', aircraft), 'name': aircraft.name, 'path': str(aircraft),
                          'device_count': len(configured), 'diff_file_count': count})
    return {'installed_aircraft': installed, 'saved_aircraft': saved, 'devices': list(devices.values()),
            'counts': {'installed_aircraft': len(installed), 'saved_aircraft': len(saved), 'devices': len(devices), 'diff_files': diff_count},
            'truncated': False}


def discover_dcs(dcs_root=None, saved_games=None, *, process_snapshot=None, install_candidates=None,
                 profile_roots=None, registry_candidates=None, steam_roots=None, home=None,
                 environ=None, known_folder=None):
    """Return candidates, fail-closed selection, and selected setup inventories.

    Injecting install_candidates replaces automatic install locations (except
    explicit injected registry/Steam sources and running processes). Injecting
    process_snapshot=[] disables OS process probing. Profile roots are Saved
    Games parent folders, while saved_games is an explicit DCS profile folder.
    """
    issues, installs, profiles = [], {}, {}
    environment = os.environ if environ is None else environ
    user_home = _path(home or Path.home())
    roots = [_path(value) for value in profile_roots] if profile_roots is not None else [saved_games_known_folder(user_home, known_folder)]
    roots = list({_key(value): value for value in roots if value}.values())[:MAX_CANDIDATES]
    processes = _processes() if process_snapshot is None else process_snapshot() if callable(process_snapshot) else process_snapshot
    processes = list(itertools.islice(processes or [], 17))
    processes = [item for item in processes if isinstance(item, dict) and
                 (item.get('name') or Path(item.get('exe') or '').name).casefold() == 'dcs.exe']

    def add(collection, value, evidence, validator, kind):
        path = _path(value)
        if path is None or not validator(path): return None
        key = _key(path)
        if key not in collection:
            if len(collection) >= MAX_CANDIDATES:
                issues.append(f'{kind.capitalize()} candidate limit reached; selection may be incomplete.')
                return None
            collection[key] = {'id': _identifier(kind, path), 'name': path.name, 'path': str(path), 'evidence': [], 'valid': True}
        if evidence not in collection[key]['evidence']: collection[key]['evidence'].append(evidence)
        return key

    running_installs, running_profiles = set(), set()
    unresolved_running = False
    for process in processes:
        install = install_from_executable(process.get('exe'))
        if install is None:
            unresolved_running = True
            issues.append('A running DCS executable could not be validated; automatic selection is unavailable.')
            continue
        running_installs.add(add(installs, install, 'Running DCS executable', _is_install, 'install'))
        profile_values = _profile_for_process(process, install, roots, issues)
        found = []
        for profile, evidence in profile_values:
            key = add(profiles, profile, evidence, _is_profile, 'profile')
            if key: found.append(key)
        if len(set(found)) != 1:
            unresolved_running = True
            issues.append('The running DCS profile is missing or ambiguous; choose its actual Saved Games profile.')
        running_profiles.update(found)

    reg_installs, reg_steam = ([], [])
    if install_candidates is None and (registry_candidates is None or steam_roots is None):
        reg_installs, reg_steam = _registry_sources()
    candidates = list(install_candidates or [])
    candidates.extend(reg_installs if registry_candidates is None else registry_candidates)
    steam = list(reg_steam if steam_roots is None else steam_roots)
    if install_candidates is None:
        for variable in ('ProgramFiles', 'ProgramFiles(x86)'):
            folder = _path(environment.get(variable))
            if not folder: continue
            steam.append(folder/'Steam')
            candidates.extend({'path': folder/'Eagle Dynamics'/name, 'evidence': 'Conventional DCS installation directory'}
                              for name in ('DCS World', 'DCS World OpenBeta'))
        candidates.extend({'path': user_home/'Games'/name, 'evidence': 'Conventional user Games directory'}
                          for name in ('DCS World', 'DCS World OpenBeta'))
    candidates.extend(_steam_candidates(steam, issues))
    candidate_scan_limited = len(candidates) > MAX_CANDIDATES
    if candidate_scan_limited:
        issues.append('Installation source candidates exceeded the discovery limit; automatic installation selection is unavailable.')
    for candidate in candidates[:MAX_CANDIDATES]:
        path = candidate.get('path') if isinstance(candidate, dict) else candidate
        evidence = candidate.get('evidence', 'Configured discovery candidate') if isinstance(candidate, dict) else 'Configured discovery candidate'
        add(installs, path, str(evidence), _is_install, 'install')
    for root in roots:
        for name in ('DCS', 'DCS.openbeta'):
            add(profiles, root/name, 'Saved Games standard DCS profile', _is_profile, 'profile')
        for candidate in _entries(root, issues):
            if candidate.name.casefold().startswith('dcs.'):
                add(profiles, candidate, 'Saved Games named DCS profile (requires explicit or running selection)', _is_profile, 'profile')

    def choose(collection, override, running, label, validator, kind):
        if override is not None:
            key = add(collection, override, 'Explicit setup override', validator, kind)
            if not key:
                issues.append(f'The explicit {label} path is missing or invalid; no fallback was selected.')
            return key
        if unresolved_running:
            return None
        if running:
            if len(running) == 1: return next(iter(running))
            issues.append(f'Multiple running DCS {label} paths were found; choose one explicitly.')
            return None
        eligible = collection if kind == 'install' else {key: value for key, value in collection.items()
                    if Path(value['path']).name.casefold() in ('dcs', 'dcs.openbeta')}
        if len(eligible) == 1: return next(iter(eligible))
        issues.append(f'Multiple DCS {label} candidates were found; choose one explicitly.' if eligible else
                      f'No validated DCS {label} was found; choose its path in setup.')
        return None

    install_key = choose(installs, dcs_root, running_installs, 'installation', _is_install, 'install')
    if candidate_scan_limited and dcs_root is None and not running_installs:
        install_key = None
    profile_key = choose(profiles, saved_games, running_profiles, 'Saved Games profile', _is_profile, 'profile')
    selected_install, selected_profile = installs.get(install_key), profiles.get(profile_key)
    install_path = Path(selected_install['path']) if selected_install else None
    profile_path = Path(selected_profile['path']) if selected_profile else None
    inventory = _inventory(install_path, profile_path, issues)
    runtime = identify_running_setup(processes, profile_roots=roots, home=user_home)
    matches = None if not runtime['running'] else bool(runtime['unique'] and install_path and profile_path and
        _key(runtime['install_path']) == _key(install_path) and _key(runtime['profile_path']) == _key(profile_path))
    return {'status': 'Ready' if install_path and profile_path else 'Setup required',
            'selected': {'install_path': str(install_path) if install_path else None, 'profile_path': str(profile_path) if profile_path else None,
                         'install_id': selected_install['id'] if selected_install else None,
                         'profile_id': selected_profile['id'] if selected_profile else None},
            'install_candidates': sorted(installs.values(), key=lambda item: item['path'].casefold()),
            'profile_candidates': sorted(profiles.values(), key=lambda item: item['path'].casefold()),
            'saved_games_roots': [str(root) for root in roots], 'issues': list(dict.fromkeys(issues)), 'inventory': inventory,
            'running': runtime, 'runtime_matches_selected': matches}


discover_setup = discover_dcs


def resolve_setup_paths(install_root=None, saved_games=None, **kwargs):
    snapshot = discover_dcs(install_root, saved_games, **kwargs)
    selected = snapshot['selected']
    if not selected['install_path'] or not selected['profile_path']:
        raise SetupUnavailable('; '.join(snapshot['issues']) or 'DCS installation and profile require selection.')
    return Path(selected['install_path']), Path(selected['profile_path'])


class DCSDiscovery:
    def __init__(self, dcs_root=None, saved_games=None, *, clock=time.monotonic, **kwargs):
        self.dcs_root, self.saved_games, self.kwargs, self.clock = dcs_root, saved_games, kwargs, clock
        self.lock = threading.RLock()
        self.result, self.checked_at = None, float('-inf')

    def snapshot(self, refresh=False):
        with self.lock:
            now = self.clock()
            if refresh or self.result is None or not 0 <= now-self.checked_at < 10:
                self.result = discover_dcs(self.dcs_root, self.saved_games, **self.kwargs)
                self.checked_at = now
            return copy.deepcopy(self.result)
