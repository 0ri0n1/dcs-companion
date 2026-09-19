"""Deterministic scheduling evidence; all senders are injected and never touch DCS."""
import json
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

from webapp.backend.app import _worker_interval
from webapp.backend.remote import RemoteError
from webapp.tests.test_remote import ACTION, fixture


def test_tap_settles_from_acceptance_and_finishes_on_sender_receipt(tmp_path):
    f = fixture(tmp_path)
    original_context = f.remote.context_provider
    observations, handoffs = [], []

    def measured_context():
        f.now[0] += .08
        observations.append(f.now[0])
        return original_context()

    def measured_sender(action, request):
        # The durable claim must exist before the injected sender is invoked.
        stored = json.loads(f.remote.db.execute('SELECT payload FROM remote_runs').fetchone()[0])
        assert stored['attempted'] == 1 and stored['events'][-1]['status'] == 'Attempted'
        handoffs.append(f.now[0])
        f.now[0] += .08
        return dict(ok=True, sent=True, dry_run=False)

    f.remote.context_provider = measured_context
    f.remote.injected_sender = measured_sender
    f.remote.enable(True, 'pc')
    service = SimpleNamespace(remote=f.remote)
    already_sleeping = _worker_interval(service)
    run = f.remote.start(str(uuid4()), 'pc', action_id=ACTION)
    acceptance_sample = observations[-1]
    f.now[0] += already_sleeping
    ticks = 0
    while f.remote.active:
        f.remote.tick()
        ticks += 1
        assert ticks < 10
        if f.remote.active:
            f.now[0] += _worker_interval(service)

    final = f.remote.status()['last_run']
    assert len(handoffs) == 1
    assert handoffs[0] - acceptance_sample >= .25
    assert handoffs[0] - run['created_at'] <= .50
    assert final['finished_at'] - run['created_at'] <= .60
    assert final['finished_at'] == pytest.approx(handoffs[0] + .08)
    assert final['status'] == 'Unconfirmed' and ticks == 2
    # Every send still obtains fresh tick, final-validation and dispatch samples.
    assert len([sample for sample in observations if acceptance_sample < sample <= handoffs[0]]) >= 3
    f.remote.close()


@pytest.mark.parametrize('change', ['focused', 'model_advancing', 'telemetry_valid', 'bindings_valid', 'session', 'bindings_hash'])
def test_acceptance_focus_sample_never_bypasses_current_send_guards(tmp_path, change):
    f = fixture(tmp_path)
    f.remote.enable(True, 'pc')
    key = str(uuid4())
    f.remote.start(key, 'pc', action_id=ACTION)
    f.now[0] += .3
    f.ctx[change] = False if isinstance(f.ctx[change], bool) else 'changed'
    f.remote.tick()
    assert not f.calls and f.remote.active is None
    previous = f.remote.start(key, 'pc', action_id=ACTION)
    assert previous['status'] in ('Rejected', 'Cancelled')
    f.remote.tick()
    assert not f.calls
    f.remote.close()


def test_acceptance_sample_still_requires_new_model_time(tmp_path):
    f = fixture(tmp_path)
    original_context = f.remote.context_provider
    f.remote.context_provider = lambda: {**original_context(), 'model_time': 500}
    f.remote.enable(True, 'pc')
    f.remote.start(str(uuid4()), 'pc', action_id=ACTION)
    f.now[0] += .3
    f.remote.tick()
    assert not f.calls and f.remote.active is None
    assert 'Model time did not advance' in f.remote.status()['last_run']['message']
    f.remote.close()


def test_final_tap_remains_busy_until_sender_release_and_duplicate_never_replays(tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def sender(action, request):
        calls.append(request)
        entered.set()
        assert release.wait(3)
        return dict(ok=True, sent=True, dry_run=False)

    f = fixture(tmp_path, sender)
    f.remote.enable(True, 'pc')
    key = str(uuid4())
    f.remote.start(key, 'pc', action_id=ACTION)
    f.now[0] += .3
    worker = threading.Thread(target=f.remote.tick)
    worker.start()
    try:
        assert entered.wait(3)
        assert f.remote.inflight and f.remote.active is not None
        with pytest.raises(RemoteError):
            f.remote.start(str(uuid4()), 'pc', action_id=ACTION)
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and not f.remote.inflight and f.remote.active is None
    result = f.remote.start(key, 'pc', action_id=ACTION)
    assert result['status'] == 'Unconfirmed' and len(calls) == 1
    f.remote.tick()
    assert len(calls) == 1
    f.remote.close()


def test_next_tap_keeps_post_release_spacing_and_independent_focus_window(tmp_path):
    f = fixture(tmp_path)
    f.remote.enable(True, 'pc')
    f.remote.start(str(uuid4()), 'pc', action_id=ACTION)
    f.now[0] += .3
    f.remote.tick()
    assert len(f.calls) == 1 and f.remote.active is None
    f.remote.start(str(uuid4()), 'pc', action_id=ACTION)
    f.now[0] += .20
    f.remote.tick()
    assert len(f.calls) == 1 and f.remote.active is not None
    f.now[0] += .06
    f.remote.tick()
    assert len(f.calls) == 2 and f.remote.active is None
    f.remote.close()


def test_cheap_context_does_not_get_polled_repeatedly_during_known_focus_wait(tmp_path):
    f = fixture(tmp_path)
    f.remote.enable(True, 'pc')
    service = SimpleNamespace(remote=f.remote)
    idle_interval = _worker_interval(service)
    f.remote.start(str(uuid4()), 'pc', action_id=ACTION)
    context = f.remote.context_provider
    checks = []

    def checked_context():
        checks.append(f.now[0])
        return context()

    f.remote.context_provider = checked_context
    f.now[0] += idle_interval
    f.remote.tick()
    assert not f.calls
    remaining = _worker_interval(service)
    assert .09 <= remaining <= .11
    f.now[0] += remaining
    f.remote.tick()
    assert len(f.calls) == 1 and f.remote.active is None
    assert len(checks) == 4  # First tick, send tick, final proof and dispatch proof.
    assert _worker_interval(service) == idle_interval
    f.remote.close()
