from pathlib import Path
from uuid import uuid4

from webapp.backend.mission_scripts import ScriptRegistry
from webapp.tests.test_remote import fixture, step
from webapp.tests.test_remote_api import clients, pair, ORIGIN


def test_full_size_workflow_can_be_saved_and_oversize_body_is_rejected(tmp_path):
    f, pc, phone = clients(tmp_path)
    with pc:
        pair(pc)
        definition = {'id': 'long-wait', 'name': 'Long exact display conditions', 'aircraft': 'FA-18C_hornet',
                      'steps': [{'type': 'wait', 'display_id': '2', 'name': 'n' * 160,
                                 'equals': 'v' * 200, 'timeout_s': 1} for _ in range(32)]}
        response = pc.post('/api/remote/workflows', json=definition, headers=ORIGIN)
        assert response.status_code == 200, response.text
        assert len(response.json()['steps']) == 32
        assert pc.post('/api/remote/workflows', content=b'x' * 16001, headers=ORIGIN).status_code == 413
        assert pc.post('/api/remote/tap', content=b'x' * 4097, headers=ORIGIN).status_code == 413
    f.remote.close()


def test_registered_example_preview_through_engine_never_contacts_dcs(tmp_path):
    f = fixture(tmp_path / 'runtime')
    def forbidden(*args, **kwargs):
        raise AssertionError('A preview must not contact the mission bridge')
    library = Path(__file__).resolve().parents[1] / 'scripts'
    registry = ScriptRegistry(library, tmp_path / 'hook.lua', context_provider=f.context,
                              request=forbidden, authorization=forbidden)
    f.remote.script_provider = registry.public
    f.remote.script_prepare = registry.prepare
    f.remote.script_execute = registry.execute
    assert f.remote.status('pc')['scripts'][0]['id'] == 'mission-status'
    key = str(uuid4())
    f.remote.start(key, 'pc', script_id='mission-status')
    f.remote.tick()
    step(f)
    result = f.remote.start(key, 'pc', script_id='mission-status')
    assert result['status'] == 'Completed' and result['mode'] == 'preview'
    assert result['events'][0]['status'] == 'Preview'
    assert not f.calls
    f.remote.close()
