import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4, UUID

import pytest
from webapp.backend.remote import RemoteService, RemoteError, InputCoordinator
from webapp.backend.remote_catalog import build_catalog, definitions
from webapp.backend.workflows import validate_workflow


ACTION = 'hornet.ufc.digit.1'


def catalog():
    actions = []
    modifiers = ['', 'LCtrl', 'LAlt', 'LShift', 'RCtrl', 'RAlt', 'RShift']
    for index, (ident, name, panel, label, device, command) in enumerate(definitions()):
        combo = '+'.join(part for part in (modifiers[index // 12], f'F{index % 12 + 1}') if part)
        actions.append(dict(name=name, device_id='keyboard', kind='key', verified_default=True, conflicts=[], combos=[combo],
                            signature=json.dumps(dict(cockpit_device_id=device, down=command, up=command, value_down=1, value_up=0)),
                            command_id=f'd{command}pnilu{command}cd{device}vd1vpnilvu0'))
    return dict(aircraft='FA-18C_hornet', status='Current', source_fingerprint='fingerprint', current_fingerprint='fingerprint', actions=actions)


def fixture(tmp_path, sender=None):
    now = [1000.]
    ctx = dict(aircraft='FA-18C_hornet', session='session', mission='mission', process='123:1', bindings_hash='fingerprint',
               telemetry_valid=True, model_advancing=True, setup_matches_process=True, bindings_valid=True, focused=True, display_valid=True)
    calls, bound, cockpit = [], catalog(), {}
    def context():
        return {**ctx, 'model_time': now[0], 'sample_epoch': now[0]}
    def emit(action, request):
        calls.append((copy.deepcopy(action), copy.deepcopy(request)))
        return dict(ok=True, sent=not request['dry_run'], dry_run=request['dry_run'], message='Simulated sender')
    remote = RemoteService(tmp_path, context, lambda ac: (bound, []), lambda: cockpit, sender=sender or emit, clock=lambda: now[0])
    connection = str(uuid4())
    remote.heartbeat('pc', connection)
    return SimpleNamespace(remote=remote, now=now, ctx=ctx, bound=bound, calls=calls, cockpit=cockpit, context=context, connection=connection)


def step(f, seconds=.3):
    f.now[0] += seconds
    f.remote.tick()


def live_start(f, **target):
    f.remote.enable(True, 'pc')
    run = f.remote.start(str(uuid4()), 'pc', **(target or dict(action_id=ACTION)))
    f.remote.tick()
    step(f)
    return run


def test_catalog_exact_button_commands_unbound_conflicts_and_wrong_signature():
    data = catalog()
    actual = build_catalog(data)
    assert len(actual) == 84 and all(a['available'] for a in actual)
    data['actions'][0]['signature'] = '{}'
    data['actions'][1]['command_id'] = 'd9999'
    data['actions'][2]['conflicts'] = [data['actions'][2]['combos'][0]]
    data['actions'][3]['combos'] = []
    data['actions'][4]['verified_default'] = False
    assert all(not a['available'] for a in build_catalog(data)[:5])
    data['aircraft'] = 'UnverifiedMod'
    assert build_catalog(data) == []


def test_preview_is_default_and_durable_duplicate_never_replays(tmp_path):
    f = fixture(tmp_path)
    ident = str(uuid4())
    f.remote.start(ident, 'pc', action_id=ACTION)
    f.remote.tick()
    step(f)
    prior = f.remote.start(ident, 'pc', action_id=ACTION)
    assert prior['status'] == 'Completed' and prior['mode'] == 'preview'
    assert len(f.calls) == 1 and f.calls[0][1]['dry_run'] is True
    with pytest.raises(RemoteError):
        f.remote.start(ident, 'pc', action_id='hornet.ufc.digit.2')
    with pytest.raises(RemoteError):
        f.remote.start(ident, 'other', action_id=ACTION)
    f.remote.close()


def test_concurrent_duplicate_claims_only_one_run(tmp_path):
    f = fixture(tmp_path)
    ident = str(uuid4())
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: f.remote.start(ident, 'pc', action_id=ACTION), range(12)))
    assert {r['id'] for r in results} == {ident}
    f.remote.tick()
    assert len(f.calls) == 1
    f.remote.close()


@pytest.mark.parametrize('change', ['session', 'process', 'aircraft', 'bindings_hash', 'mission'])
def test_context_change_disarms_and_cancels_without_send(tmp_path, change):
    f = fixture(tmp_path)
    f.remote.enable(True, 'pc')
    f.remote.start(str(uuid4()), 'pc', action_id=ACTION)
    f.ctx[change] = 'changed'
    f.remote.tick()
    assert not f.remote.arm and f.remote.active is None and not f.calls
    f.remote.close()


@pytest.mark.parametrize('key', ['telemetry_valid', 'model_advancing', 'setup_matches_process', 'bindings_valid'])
def test_stale_pause_setup_binding_gates(tmp_path, key):
    f = fixture(tmp_path)
    f.remote.enable(True, 'pc')
    f.ctx[key] = False
    assert not f.remote.status()['armed']
    with pytest.raises(RemoteError):
        f.remote.start(str(uuid4()), 'pc', action_id=ACTION)
    assert not f.calls
    f.remote.close()


def test_focus_refusal_has_no_automatic_retry_and_tap_expires(tmp_path):
    f = fixture(tmp_path)
    f.remote.enable(True, 'pc')
    run = f.remote.start(str(uuid4()), 'pc', action_id=ACTION)
    assert run['expires_at'] - run['created_at'] == 3
    f.ctx['focused'] = False
    f.remote.tick()
    f.ctx['focused'] = True
    step(f)
    assert not f.calls and f.remote.status()['last_run']['status'] == 'Rejected'
    f.remote.start(str(uuid4()), 'pc', action_id=ACTION)
    step(f, 3.1)
    assert not f.calls and f.remote.active is None
    f.remote.close()


def test_binding_changed_after_acceptance_blocks_send(tmp_path):
    f = fixture(tmp_path)
    f.remote.start(str(uuid4()), 'pc', action_id=ACTION)
    next(a for a in f.bound['actions'] if a['name'].endswith(' - 1'))['combos'] = ['LAlt+2']
    f.remote.tick()
    assert not f.calls and f.remote.status()['last_run']['status'] == 'Rejected'
    f.remote.close()


def test_final_validation_rejects_during_isolated_index_resolution(tmp_path, monkeypatch):
    f = fixture(tmp_path)
    f.remote.start(str(uuid4()), 'pc', action_id=ACTION)
    import webapp.backend.remote as module
    write = module.atomic_json
    def changed(path, value):
        write(path, value)
        f.ctx['bindings_valid'] = False
    monkeypatch.setattr(module, 'atomic_json', changed)
    f.remote.tick()
    assert not f.calls and f.remote.active is None
    f.remote.close()


def test_live_focus_settle_model_advance_and_unconfirmed_effect(tmp_path):
    f = fixture(tmp_path)
    live_start(f)
    assert len(f.calls) == 1 and not f.calls[0][1]['dry_run']
    step(f)
    assert f.remote.status()['last_run']['status'] == 'Unconfirmed'
    f.remote.close()


def test_heartbeat_reconnect_expiry_and_arm_expiry(tmp_path):
    f = fixture(tmp_path)
    f.remote.heartbeat('phone', str(uuid4()))
    f.remote.enable(True, 'phone')
    f.remote.heartbeat('phone', str(uuid4()))
    assert not f.remote.arm
    f.remote.enable(True, 'pc')
    step(f, 8.1)
    assert not f.remote.arm
    f.remote.heartbeat('pc', str(uuid4()))
    f.remote.enable(True, 'pc')
    f.remote.arm['expires_at'] = f.now[0]
    assert not f.remote.status()['armed']
    f.remote.close()


def test_only_explicitly_enabled_controller_sends_live(tmp_path):
    f = fixture(tmp_path)
    connection = str(uuid4())
    f.remote.heartbeat('phone', connection)
    enabled = f.remote.enable(True, 'phone')
    assert enabled['armed'] and enabled['mode'] == 'live'
    assert f.remote.arm['controllers'] == {'phone': connection}
    assert f.remote.status()['armed'] and f.remote.status()['mode'] == 'live'
    assert f.remote.status('pc')['mode'] == 'preview'
    assert not f.remote.status('pc')['armed']
    assert f.remote.status('pc')['arm_expires_at'] is None
    assert 'Preview mode on this screen' in f.remote.status('pc')['reason']

    preview = f.remote.start(str(uuid4()), 'pc', action_id=ACTION)
    assert preview['mode'] == 'preview'
    f.remote.tick()
    step(f)
    assert len(f.calls) == 1 and f.calls[0][1]['dry_run'] is True
    assert f.remote.status('phone')['armed']

    live = f.remote.start(str(uuid4()), 'phone', action_id=ACTION)
    assert live['mode'] == 'live'
    f.remote.tick()
    step(f)
    step(f)
    assert len(f.calls) == 2 and f.calls[1][1]['dry_run'] is False
    f.remote.enable(True, 'pc')
    assert f.remote.status('pc')['armed'] and not f.remote.status('phone')['armed']
    f.remote.close()


def test_phone_live_ignores_other_controller_expiry_connect_and_disconnect(tmp_path):
    f = fixture(tmp_path)
    connection = str(uuid4())
    f.remote.heartbeat('phone', connection)
    f.remote.enable(True, 'phone')
    f.now[0] += 4
    f.remote.heartbeat('phone', connection)
    step(f, 4.1)
    assert not f.remote.status('pc')['controller_connected']
    assert f.remote.status('phone')['armed']

    f.remote.start(str(uuid4()), 'phone', action_id=ACTION)
    pc_connection = str(uuid4())
    assert not f.remote.heartbeat('pc', pc_connection)['armed']
    assert not f.remote.enable(False, 'pc')['armed']
    assert not f.remote.disconnect('pc', pc_connection)['controller_connected']
    assert f.remote.status('phone')['armed'] and f.remote.active is not None
    f.remote.tick()
    step(f)
    step(f)
    assert len(f.calls) == 1 and f.calls[0][1]['dry_run'] is False

    f.remote.start(str(uuid4()), 'phone', action_id=ACTION)
    step(f, 8.1)
    assert not f.remote.status('phone')['armed'] and f.remote.active is None
    assert len(f.calls) == 1
    f.remote.close()


def test_phone_reconnect_cancels_old_request_without_replay_and_stale_disconnect_is_ignored(tmp_path):
    f = fixture(tmp_path)
    old_connection, new_connection = str(uuid4()), str(uuid4())
    f.remote.heartbeat('phone', old_connection)
    f.remote.enable(True, 'phone')
    ident = str(uuid4())
    f.remote.start(ident, 'phone', action_id=ACTION)
    f.remote.tick()
    assert not f.remote.heartbeat('phone', new_connection)['armed']
    duplicate = f.remote.start(ident, 'phone', action_id=ACTION)
    assert duplicate['status'] == 'Cancelled'
    step(f)
    assert not f.calls

    f.remote.enable(True, 'phone')
    stale = f.remote.disconnect('phone', old_connection)
    assert stale['armed'] and stale['controller_connected']
    f.remote.start(str(uuid4()), 'phone', action_id=ACTION)
    disconnected = f.remote.disconnect('phone', new_connection)
    assert not disconnected['armed'] and not disconnected['controller_connected']
    assert f.remote.active is None
    step(f)
    assert not f.calls
    f.remote.close()


@pytest.mark.parametrize('attempted,expected', [(0, 'Cancelled'), (1, 'Unconfirmed')])
def test_restart_consumes_pending_ids_and_never_replays(tmp_path, attempted, expected):
    f = fixture(tmp_path)
    ident = str(uuid4())
    f.remote.start(ident, 'pc', action_id=ACTION)
    f.remote.active['attempted'] = attempted
    f.remote._save(f.remote.active)
    f.remote.db.close()  # Abrupt prior process death, no orderly cancellation.
    second = fixture(tmp_path)
    assert second.remote.start(ident, 'pc', action_id=ACTION)['status'] == expected
    second.remote.tick()
    assert not second.calls and not second.remote.arm
    second.remote.close()


def workflow(steps):
    return dict(id='test-flow', name='Test flow', aircraft='FA-18C_hornet', steps=steps)


def test_workflow_wait_requires_new_individual_sample_same_session(tmp_path):
    f = fixture(tmp_path)
    f.remote.save_workflow(workflow([dict(type='wait', display_id='6', name='Cue', equals=':', timeout_s=3), dict(type='action', action_id=ACTION)]))
    f.cockpit.update(aircraft=f.ctx['aircraft'], remote_session='session', received_epoch_by_id={'6': 999}, displays=[dict(id='6', status='Available', elements=[dict(name='Cue', value=':')])])
    f.remote.enable(True, 'pc')
    f.remote.start(str(uuid4()), 'pc', workflow_id='test-flow')
    f.remote.tick()
    assert f.remote.active['step_index'] == 0
    f.cockpit['received_epoch_by_id']['6'] = 1000.2
    f.cockpit['remote_session'] = 'another'
    step(f)
    assert f.remote.active['step_index'] == 0
    f.cockpit['remote_session'] = 'session'
    step(f)
    assert f.remote.active['step_index'] == 1 and not f.calls
    step(f)
    step(f)
    assert len(f.calls) == 1
    f.remote.close()


def test_wait_timeout_and_preview_do_not_claim_observation(tmp_path):
    f = fixture(tmp_path)
    value = workflow([dict(type='wait', display_id='6', name='Cue', equals=':', timeout_s=1)])
    f.remote.save_workflow(value)
    f.remote.start(str(uuid4()), 'pc', workflow_id='test-flow')
    f.remote.tick()
    assert 'no observation' in f.remote.active['message']
    step(f)
    f.remote.enable(True, 'pc')
    f.remote.start(str(uuid4()), 'pc', workflow_id='test-flow')
    f.remote.tick()
    step(f, 1.1)
    assert f.remote.active is None and 'timed out' in f.remote.status()['last_run']['message']
    f.remote.close()


@pytest.mark.parametrize('steps', [[{'type': 'python', 'source': 'pass'}], [{'type': 'action', 'action_id': 'weapons.fire'}], [{'type': 'delay', 'seconds': -1}], [{'type': 'delay', 'seconds': float('nan')}], [{'type': 'action', 'action_id': ACTION, 'combo': 'A'}], [{'type': 'delay', 'seconds': 5}]*19])
def test_workflow_validation_rejects_arbitrary_or_unbounded_steps(steps):
    with pytest.raises(ValueError):
        validate_workflow(workflow(steps), {ACTION})


def test_stop_during_send_and_close_wait_for_receipt_no_guide_interleave(tmp_path):
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    def sender(action, request):
        entered.set()
        assert release.wait(3)
        return dict(ok=True, sent=False, dry_run=True)
    f = fixture(tmp_path, sender)
    f.remote.start(str(uuid4()), 'pc', action_id=ACTION)
    worker = threading.Thread(target=f.remote.tick)
    worker.start()
    assert entered.wait(3)
    f.remote.stop()
    assert not f.remote.coordinator.guide(lambda *args: {'ok': True})({}, {})['ok']
    with pytest.raises(RemoteError):
        f.remote.start(str(uuid4()), 'pc', action_id=ACTION)
    closer = threading.Thread(target=lambda: (f.remote.close(), closed.set()))
    closer.start()
    assert not closed.wait(.05)
    release.set()
    worker.join(3)
    closer.join(3)
    assert closed.is_set() and not worker.is_alive()


def test_guide_lease_is_nonblocking_and_remote_reservation_blocks_guide():
    lock = InputCoordinator()
    assert lock.reserve('remote')
    assert not lock.guide(lambda *args: {'ok': True})({}, {})['ok']
    assert not lock.reserve('other')
    lock.release('remote')
    assert lock.guide(lambda *args: {'ok': True})({}, {})['ok']


def test_script_shared_slot_immutable_revision_and_uuid_receipt(tmp_path):
    f = fixture(tmp_path)
    prepared = dict(id='sample', sha256='hash', source='return 42', aircraft=['*'])
    calls = []
    f.remote.script_provider = lambda: [dict(id='sample')]
    f.remote.script_prepare = lambda ident: copy.deepcopy(prepared)
    def execute(item, context, request, dry_run=False):
        assert str(UUID(request)) == request
        calls.append(request)
        return dict(ok=True, sent=not dry_run, dry_run=dry_run)
    f.remote.script_execute = execute
    f.remote.start(str(uuid4()), 'pc', script_id='sample')
    prepared['description'] = 'changed after approval'
    f.remote.tick()
    assert not calls
    f.remote.start(str(uuid4()), 'pc', script_id='sample')
    f.remote.tick()
    assert len(calls) == 1
    f.remote.close()


def test_malformed_local_workflow_file_is_withheld_without_ui_shape_leak(tmp_path):
    f = fixture(tmp_path)
    f.remote.workflows.path.write_text(json.dumps([dict(id='broken', steps=None)]))
    state = f.remote.status()
    assert state['workflows'] == [] and 'workflow_error' in state
    assert f.remote.workflows.path.read_text() == json.dumps([dict(id='broken', steps=None)])
    f.remote.close()


def test_ambiguous_live_partial_send_disarms_without_replay(tmp_path):
    calls = []
    def uncertain(action, request):
        calls.append(request)
        return dict(ok=False, sent=False, ambiguous=True, message='Release unverified')
    f = fixture(tmp_path, uncertain)
    live_start(f)
    assert not f.remote.arm and f.remote.active is None
    step(f)
    assert len(calls) == 1 and f.remote.status()['last_run']['status'] == 'Unconfirmed'
    f.remote.close()


def test_stop_during_script_preflight_revokes_private_execution_gate(tmp_path):
    f = fixture(tmp_path)
    entered, release = threading.Event(), threading.Event()
    attempted = []
    f.remote.script_prepare = lambda ident: dict(id=ident, sha256='hash', source='return 42', aircraft=['*'])
    def execute(prepared, context, request, dry_run=False):
        gate = context['_remote_authorized']
        assert callable(gate) and gate()
        entered.set()
        assert release.wait(3)
        if gate():
            attempted.append(request)
            return dict(ok=True, sent=True)
        return dict(ok=False, sent=False, message='Stopped during preflight.')
    f.remote.script_execute = execute
    f.remote.enable(True, 'pc')
    f.remote.start(str(uuid4()), 'pc', script_id='sample')
    f.remote.tick()
    f.now[0] += .3
    worker = threading.Thread(target=f.remote.tick)
    worker.start()
    assert entered.wait(3)
    f.remote.stop()
    release.set()
    worker.join(3)
    assert not worker.is_alive() and attempted == []
    assert '_remote_authorized' not in json.dumps(f.remote.status())
    assert '_remote_authorized' not in f.remote.db.execute('SELECT payload FROM remote_runs').fetchone()[0]
    f.remote.close()


@pytest.mark.parametrize('blocked', ['context', 'catalog'])
def test_read_only_status_cannot_hold_stop_behind_binding_scan(tmp_path, blocked):
    f = fixture(tmp_path)
    f.remote.enable(True, 'pc')
    entered, release = threading.Event(), threading.Event()
    results = []
    def slow_context():
        entered.set()
        assert release.wait(3)
        return f.context()
    def slow_catalog(aircraft):
        entered.set()
        assert release.wait(3)
        return f.bound, []
    if blocked == 'context':
        f.remote.context_provider = slow_context
    else:
        f.remote.catalog_provider = slow_catalog
    worker = threading.Thread(target=lambda: results.append(f.remote.status(include_catalog=blocked == 'catalog')))
    worker.start()
    assert entered.wait(3)
    stopped = threading.Event()
    stop_worker = threading.Thread(target=lambda: (f.remote.stop(), stopped.set()))
    stop_worker.start()
    try:
        assert stopped.wait(.5), 'Read-only status held the control state lock during source I/O'
    finally:
        release.set()
        worker.join(3)
        stop_worker.join(3)
    assert not worker.is_alive() and not stop_worker.is_alive()
    assert results[0]['armed'] is False and results[0]['mode'] == 'preview'
    assert not f.calls
    f.remote.close()


def test_compact_status_uses_supplied_ui_evidence_without_another_scan(tmp_path):
    f = fixture(tmp_path)
    f.remote.context_provider = lambda: pytest.fail('Supplied UI context was read again')
    result = f.remote.status(include_catalog=False, ctx=f.context())
    assert result['aircraft'] == 'FA-18C_hornet' and result['mode'] == 'preview'
    f.remote.close()
