import hashlib
import json
from pathlib import Path
from webapp.tests.lua_support import run_lua
from uuid import uuid4

import pytest

from webapp.backend.mission_scripts import ScriptRegistry, MAX_SOURCE, _lua_string


def register(directory, source='return "complete"', **overrides):
    directory.mkdir(exist_ok=True)
    (directory/'example.lua').write_text(source, encoding='utf-8')
    manifest = {'schema': 'dcs-companion/mission-script/1', 'id': 'example', 'name': 'Example',
                'description': 'A registered test routine.', 'context': 'mission',
                'aircraft': ['FA-18C_hornet'], 'enabled': True,
                'sha256': hashlib.sha256((directory/'example.lua').read_bytes()).hexdigest()}
    manifest.update(overrides)
    (directory/'example.json').write_text(json.dumps(manifest), encoding='utf-8')
    return manifest


def context():
    return {'process': '123:456', 'session': 'flight', 'mission': 'export-session:flight',
            'aircraft': 'FA-18C_hornet', 'bindings_hash': 'fingerprint', 'telemetry_valid': True,
            'model_advancing': True, 'setup_matches_process': True, 'bindings_valid': True,
            'model_time': 100}


def hook():
    return {'multiplayer': False, 'paused': False, 'aircraft': 'FA-18C_hornet',
            'name': 'Current mission', 'model_time': 100}


def make_registry(tmp_path, responses=(), ctx_provider=None):
    register(tmp_path)
    calls = []
    results = iter(responses)
    def request(port, program, authorization):
        calls.append((port, program))
        value = next(results)
        if isinstance(value, Exception):
            raise value
        return value
    registry = ScriptRegistry(tmp_path, tmp_path/'external-hook.lua', ctx_provider,
                              request=request, authorization=lambda _: 'private-bridge-credential')
    return registry, calls


def test_catalog_never_exposes_source_paths_or_credentials(tmp_path):
    register(tmp_path, 'local secret="PRIVATE_SOURCE"')
    public = ScriptRegistry(tmp_path, tmp_path/'private-hook.lua').public()
    serialized = json.dumps(public)
    assert public[0]['available'] and public[0]['atomic_stop_supported'] is False
    assert 'PRIVATE_SOURCE' not in serialized and str(tmp_path) not in serialized
    assert 'source' not in public[0]


@pytest.mark.parametrize('identifier', ['../example', 'example.lua', 'a/b', 'C:\\x', '', 'UPPER', 'x'*65, None])
def test_identifier_never_selects_a_path(tmp_path, identifier):
    register(tmp_path)
    with pytest.raises(ValueError):
        ScriptRegistry(tmp_path, tmp_path/'hook').prepare(identifier)


@pytest.mark.parametrize('override', [{'enabled': False}, {'enabled': 1}, {'context': 'export'},
    {'schema': 1}, {'id': 'different'}, {'entry': '../other.lua'}, {'name': '\n'},
    {'aircraft': []}, {'aircraft': ['*', 'FA-18C_hornet']}, {'aircraft': ['A', 'A']},
    {'aircraft': 'FA-18C_hornet'}, {'sha256': '0'*64}])
def test_unregistered_or_malformed_definition_is_unavailable(tmp_path, override):
    register(tmp_path, **override)
    registry = ScriptRegistry(tmp_path, tmp_path/'hook')
    assert registry.public()[0]['available'] is False
    with pytest.raises(ValueError):
        registry.prepare('example')


def test_size_change_and_reviewed_revision_are_enforced(tmp_path):
    register(tmp_path)
    registry = ScriptRegistry(tmp_path, tmp_path/'hook')
    prepared = registry.prepare('example')
    (tmp_path/'example.lua').write_text('return "changed!"', encoding='utf-8')
    assert registry.public()[0]['available'] is False
    register(tmp_path, source='--' + 'a'*MAX_SOURCE)
    with pytest.raises(ValueError):
        registry.prepare('example')
    assert prepared['source'] == 'return "complete"'


def test_changed_manifest_cannot_run_prepared_code(tmp_path):
    registry, calls = make_registry(tmp_path)
    prepared = registry.prepare('example')
    register(tmp_path, aircraft=['*'])
    result = registry.execute(prepared, context(), str(uuid4()))
    assert not result['ok'] and not calls


@pytest.mark.parametrize('field,value', [('aircraft', 'F-22A'), ('telemetry_valid', False),
    ('model_advancing', False), ('setup_matches_process', False), ('bindings_valid', False),
    ('process', ''), ('bindings_hash', None), ('model_time', float('nan'))])
def test_current_session_and_aircraft_required_even_for_preview(tmp_path, field, value):
    registry, calls = make_registry(tmp_path)
    ctx = context() | {field: value}
    result = registry.execute(registry.prepare('example'), ctx, str(uuid4()), dry_run=True)
    assert not result['ok'] and not calls


def test_preview_never_contacts_dcs(tmp_path):
    registry, calls = make_registry(tmp_path)
    result = registry.execute(registry.prepare('example'), context(), str(uuid4()), dry_run=True)
    assert result['ok'] and result['dry_run'] and not result['sent'] and calls == []


@pytest.mark.parametrize('field,value', [('multiplayer', True), ('multiplayer', None), ('multiplayer', 0),
    ('paused', True), ('paused', None), ('aircraft', 'F-22A'), ('name', ''), ('model_time', 105)])
def test_hooks_preflight_prevents_dispatch(tmp_path, field, value):
    registry, calls = make_registry(tmp_path, [hook() | {field: value}])
    result = registry.execute(registry.prepare('example'), context(), str(uuid4()))
    assert not result['sent'] and not result['ambiguous'] and len(calls) == 1


def test_session_changes_during_preflight_prevent_dispatch(tmp_path):
    values = iter([context(), context() | {'session': 'changed'}])
    registry, calls = make_registry(tmp_path, [hook()], lambda: next(values))
    result = registry.execute(registry.prepare('example'), context(), str(uuid4()))
    assert not result['sent'] and len(calls) == 1


def test_dcs_receipt_establishes_execution_not_cockpit_effect(tmp_path):
    request_id = str(uuid4())
    registry, calls = make_registry(tmp_path, [hook(), {'accepted': True},
        {'receipt': {'id': request_id, 'completed': True, 'ok': True}}])
    result = registry.execute(registry.prepare('example'), context(), request_id)
    assert result['ok'] and result['sent'] and not result['ambiguous']
    assert 'not independently verified' in result['message']
    assert [call[0] for call in calls] == [12081, 12081, 12080]
    assert 'DCS.isMultiplayer()~=false' in calls[1][1]


@pytest.mark.parametrize('receipt', [False, {}, {'id': 'other', 'completed': True, 'ok': True},
                                    {'completed': False}, {'completed': True, 'ok': False}])
def test_missing_or_failed_receipt_never_retries(tmp_path, receipt):
    request_id = str(uuid4())
    if isinstance(receipt, dict) and 'id' not in receipt:
        receipt['id'] = request_id
    registry, calls = make_registry(tmp_path, [hook(), {'accepted': True}, {'receipt': receipt}])
    result = registry.execute(registry.prepare('example'), context(), request_id)
    assert not result['ok'] and result['ambiguous'] and len(calls) == 3


@pytest.mark.parametrize('responses,ambiguous,count', [([OSError('private detail')], False, 1),
    ([hook(), TimeoutError('private detail')], True, 2),
    ([hook(), {'accepted': True}, TimeoutError('private detail')], True, 3),
    ([hook(), {'accepted': False}], False, 2)])
def test_bridge_failure_redacts_and_preserves_uncertainty(tmp_path, responses, ambiguous, count):
    registry, calls = make_registry(tmp_path, responses)
    result = registry.execute(registry.prepare('example'), context(), str(uuid4()))
    assert not result['ok'] and result['ambiguous'] == ambiguous and len(calls) == count
    assert 'private' not in json.dumps(result)


def test_real_lua_wrapper_deduplicates_and_blocks_changed_aircraft(tmp_path):
    script_id = str(uuid4())
    wrapper = ScriptRegistry._mission_program('runs=runs+1', script_id, 'FA-18C_hornet', 100)
    blocked_id = str(uuid4())
    blocked = ScriptRegistry._mission_program('runs=runs+1', blocked_id, 'F-22A', 100)
    run_lua(tmp_path, '''
local u={isExist=function() return true end,isActive=function() return true end,
 getLife=function() return 100 end,getTypeName=function() return 'FA-18C_hornet' end}
world={getPlayer=function() return u end};timer={getTime=function() return 100 end};runs=0
local f=assert(loadstring(''' + _lua_string(wrapper) + '''));f();f()
assert(runs==1)
assert(__DCS_COMPANION_SCRIPT_RUNS_V1.items[''' + _lua_string(script_id) + '''].ok==true)
assert(loadstring(''' + _lua_string(blocked) + '''))()
assert(runs==1)
assert(__DCS_COMPANION_SCRIPT_RUNS_V1.items[''' + _lua_string(blocked_id) + '''].blocked==true)
''')


def test_real_lua_hooks_wrapper_checks_mode_atomically_and_escapes_code(tmp_path):
    value = 'Quotes " slash \\ newline\n ]] =] é 123'
    mission = 'assert(' + _lua_string(value) + '==' + _lua_string(value) + '); runs=runs+1'
    dispatch = ScriptRegistry._dispatch_program(mission, hook())
    run_lua(tmp_path, '''
local mp=false; runs=0
DCS={isMultiplayer=function() return mp end,getPause=function() return false end,
getMissionName=function() return 'Current mission' end,getPlayerUnitType=function() return 'FA-18C_hornet' end,
getModelTime=function() return 100 end}
a_do_script=function(program) assert(loadstring(program))() end
net={dostring_in=function(env,program) assert(env=='mission');assert(loadstring(program))();return '',true end}
local dispatch=assert(loadstring(''' + _lua_string(dispatch) + '''))
assert(dispatch().accepted and runs==1)
mp=true;assert(not dispatch().accepted and runs==1)
''')
