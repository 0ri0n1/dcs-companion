from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
from webapp.backend.app import create_app
from webapp.backend.config import Settings
from webapp.tests.test_remote import fixture, ACTION, workflow, step


ORIGIN = {'Origin': 'http://testserver'}


def clients(tmp_path):
    f = fixture(tmp_path)
    settings = Settings(lan_enabled=True, host='192.168.1.20', origins=('http://testserver',), pairing_token='x'*40)
    service = SimpleNamespace(remote=f.remote, tick=lambda: None)
    app = create_app(settings, service)
    pc = TestClient(app, client=('127.0.0.1', 1200))
    phone = TestClient(app, client=('192.168.1.90', 1201))
    return f, pc, phone


def pair(client, token=''):
    assert client.post('/api/session', json={'token': token}, headers=ORIGIN).status_code == 200
    connection = str(uuid4())
    assert client.post('/api/remote/heartbeat', json={'connection_id': connection}, headers=ORIGIN).status_code == 200
    return connection


def test_remote_requires_pairing_and_exact_origin(tmp_path):
    f, pc, phone = clients(tmp_path)
    with phone:
        assert phone.get('/api/remote').status_code == 401
        assert phone.get('/api/remote/status').status_code == 401
        assert phone.post('/api/remote/tap', json={'idempotency_key': str(uuid4()), 'action_id': ACTION}, headers=ORIGIN).status_code == 401
        assert phone.post('/api/remote/arm', json={'enabled': True}, headers=ORIGIN).status_code == 401
        assert phone.post('/api/remote/disconnect', json={'connection_id': str(uuid4())}, headers=ORIGIN).status_code == 401
        pair(phone, 'x'*40)
        assert phone.get('/api/remote').json()['can_edit'] is False
        assert phone.post('/api/remote/tap', json={'idempotency_key': str(uuid4()), 'action_id': ACTION}, headers={'Origin': 'http://evil.invalid'}).status_code == 403
        assert phone.post('/api/remote/arm', json={'enabled': True}, headers={'Origin': 'http://evil.invalid'}).status_code == 403
        assert phone.post('/api/remote/disconnect', json={'connection_id': str(uuid4())}, headers={'Origin': 'http://evil.invalid'}).status_code == 403
    f.remote.close()


def test_phone_can_enable_its_own_live_controls_but_only_pc_can_edit(tmp_path):
    f, pc, phone = clients(tmp_path)
    with pc, phone:
        pair(pc)
        pair(phone, 'x'*40)
        saved = workflow([{'type': 'action', 'action_id': ACTION}])
        assert phone.get('/api/remote').json()['can_arm'] is True
        assert phone.get('/api/remote/status').json()['can_arm'] is True
        assert phone.post('/api/remote/workflows', json=saved, headers=ORIGIN).status_code == 403
        assert phone.delete('/api/remote/workflows/test-flow', headers=ORIGIN).status_code == 403
        assert pc.post('/api/remote/workflows', json=saved, headers=ORIGIN).status_code == 200
        assert phone.post('/api/remote/arm', json={'enabled': True}, headers=ORIGIN).status_code == 200
        assert pc.get('/api/remote/status').json()['mode'] == 'preview'
        result = phone.post('/api/remote/run', json={'idempotency_key': str(uuid4()), 'workflow_id': 'test-flow'}, headers=ORIGIN)
        assert result.status_code == 200 and result.json()['mode'] == 'live'
        assert phone.post('/api/remote/stop', json={}, headers=ORIGIN).status_code == 200
        assert not f.remote.arm
        assert pc.get('/api/remote/status').json()['can_edit'] is True
    f.remote.close()


def test_phone_arms_and_taps_live_without_a_connected_pc(tmp_path):
    f, pc, phone = clients(tmp_path)
    with phone:
        phone_connection = pair(phone, 'x'*40)
        armed = phone.post('/api/remote/arm', json={'enabled': True}, headers=ORIGIN)
        assert armed.status_code == 200 and armed.json()['armed']
        body = {'idempotency_key': str(uuid4()), 'action_id': ACTION}
        result = phone.post('/api/remote/tap', json=body, headers=ORIGIN)
        assert result.status_code == 200 and result.json()['mode'] == 'live'
        f.remote.tick()
        step(f)
        step(f)
        assert len(f.calls) == 1 and f.calls[0][1]['dry_run'] is False
        repeated = phone.post('/api/remote/tap', json=body, headers=ORIGIN)
        assert repeated.status_code == 200 and repeated.json()['id'] == body['idempotency_key']
        f.remote.tick()
        assert len(f.calls) == 1
        disconnected = phone.post('/api/remote/disconnect', json={'connection_id': phone_connection}, headers=ORIGIN)
        assert disconnected.status_code == 200
        assert not disconnected.json()['armed'] and not disconnected.json()['controller_connected']
    f.remote.close()


def test_pairing_alone_cannot_arm_or_forge_controller_ownership(tmp_path):
    f, pc, phone = clients(tmp_path)
    with phone:
        assert phone.post('/api/session', json={'token': 'x'*40}, headers=ORIGIN).status_code == 200
        assert phone.post('/api/remote/arm', json={'enabled': True}, headers=ORIGIN).status_code == 409
        assert phone.post('/api/remote/arm', json={'enabled': True, 'owner': 'pc'}, headers=ORIGIN).status_code == 422
        assert phone.post('/api/remote/arm', json={'enabled': True, 'connection_id': f.connection}, headers=ORIGIN).status_code == 422
        assert phone.post('/api/remote/disconnect', json={'connection_id': f.connection, 'owner': 'pc'}, headers=ORIGIN).status_code == 422
        assert phone.post('/api/remote/disconnect', json={'connection_id': 'x'*36}, headers=ORIGIN).status_code == 409
        assert not f.remote.arm and not f.calls
    f.remote.close()


def test_no_caller_identity_modes_keys_lua_or_hold_duration(tmp_path):
    f, pc, phone = clients(tmp_path)
    with phone:
        pair(phone, 'x'*40)
        for key, value in [('combo', 'A'), ('owner', 'pc'), ('dry_run', False), ('hold_ms', 10000), ('lua', 'return 42')]:
            result = phone.post('/api/remote/tap', json={'idempotency_key': str(uuid4()), 'action_id': ACTION, key: value}, headers=ORIGIN)
            assert result.status_code == 422
        result = phone.post('/api/remote/run', json={'idempotency_key': str(uuid4()), 'workflow_id': 'unknown', 'steps': []}, headers=ORIGIN)
        assert result.status_code == 422
        assert not f.calls
    f.remote.close()


def test_saved_macro_is_frozen_for_run_and_api_duplicate_is_same_record(tmp_path):
    f, pc, phone = clients(tmp_path)
    with pc, phone:
        pair(pc)
        pair(phone, 'x'*40)
        saved = workflow([{'type': 'action', 'action_id': ACTION}])
        pc.post('/api/remote/workflows', json=saved, headers=ORIGIN)
        body = {'idempotency_key': str(uuid4()), 'workflow_id': 'test-flow'}
        first = phone.post('/api/remote/run', json=body, headers=ORIGIN)
        assert first.status_code == 200
        assert pc.post('/api/remote/workflows', json=saved, headers=ORIGIN).status_code == 409
        assert phone.post('/api/remote/run', json=body, headers=ORIGIN).json() == first.json()
        f.remote.tick()
        assert len(f.calls) == 1 and f.calls[0][1]['dry_run']
    f.remote.close()
