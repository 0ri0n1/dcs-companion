import asyncio
import base64
import copy
import json
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from webapp.backend.app import create_app
from webapp.backend.auth import COOKIE
from webapp.backend.config import Settings
from webapp.backend.display_feed import DisplayFeedService, FeedError, PANELS

SERVICES = []


@pytest.fixture(autouse=True)
def close_services():
    yield
    for service in SERVICES:
        service.close()
    SERVICES.clear()


def fixture():
    now = [100.]
    ctx = dict(process='123:1', session='flight', aircraft='FA-18C_hornet',
               telemetry_valid=True, model_advancing=True, focused=True, setup_matches_process=True)
    plan = dict(revision='layout', window=dict(x=0, y=0, width=5120, height=1440),
                panels={key: dict(x=2560+i*512, y=0, width=512, height=512) for i, key in enumerate(PANELS)})
    setup = SimpleNamespace(capture_plan=lambda: copy.deepcopy(plan), snapshot=lambda: {'status': 'Applied'})
    calls = []
    def capture(region, process, window):
        calls.append((region, process, window, threading.current_thread().name))
        return dict(jpeg=b'\xff\xd8fake\xff\xd9', captured_epoch=now[0], width=512, height=512, process_identity=process)
    service = DisplayFeedService(setup, lambda: dict(ctx), capture=capture, clock=lambda: now[0])
    SERVICES.append(service)
    return SimpleNamespace(now=now, ctx=ctx, plan=plan, setup=setup, calls=calls, service=service, capture=capture)


def wait_for(predicate, timeout=1.5):
    end = time.monotonic()+timeout
    while time.monotonic() < end:
        result = predicate()
        if result:
            return result
        time.sleep(.005)
    raise AssertionError('Timed out waiting for test producer')


def connect(f, panels=None):
    f.service.enable(True)
    revision = f.service.status()['revision']
    ident = f.service.subscribe(list(PANELS) if panels is None else panels, revision)
    return revision, ident


def latest(f, ident):
    return f.service.stream_snapshot(ident, {})


def test_disabled_by_default_and_enable_alone_does_not_capture():
    f = fixture()
    assert f.service.status()['status'] == 'Disabled'
    with pytest.raises(FeedError):
        f.service.frame('left_mdi', f.service.status()['revision'])
    f.service.enable(True)
    assert f.service.status()['status'] == 'Ready'
    assert f.service.producer is None and not f.calls
    assert 'armed' not in f.service.status()


@pytest.mark.parametrize('cycle_work', [0, .010, .100])
def test_fifteen_fps_target_paces_one_producer_without_catchup_bursts(cycle_work):
    f = fixture()
    mono, starts, waits = [0.], [], []
    f.service.monotonic = lambda: mono[0]
    status = f.service.status()
    assert status['target_fps'] == 15 and status['frame_interval_ms'] == 67
    def wait(delay):
        waits.append(delay)
        mono[0] += delay
    f.service.wakeup = SimpleNamespace(wait=wait, clear=lambda: None, set=lambda: None)
    f.service.enabled = True
    f.service.legacy_until = 100
    def cycle():
        starts.append(mono[0])
        mono[0] += cycle_work
        if len(starts) == 3:
            f.service.closed = True
    f.service._produce_cycle = cycle
    f.service._produce()
    interval = max(1/15, cycle_work)
    assert starts == pytest.approx([0, interval, 2*interval])
    assert all(delay > 0 for delay in waits)
    assert not f.calls


def test_backend_recreation_has_distinct_revision_with_identical_dcs_and_layout():
    first, restarted = fixture(), fixture()
    assert first.plan == restarted.plan and first.ctx == restarted.ctx
    first.service.enable(True)
    restarted.service.enable(True)
    original_revision = first.service.status()['revision']
    restarted_revision = restarted.service.status()['revision']
    assert original_revision and restarted_revision != original_revision
    # Refreshing the same instance must not continually reset frontend sequence
    # tracking; only a recreated service starts a new sequence domain here.
    for _ in range(3):
        assert first.service.status()['revision'] == original_revision
        assert restarted.service.status()['revision'] == restarted_revision
    with pytest.raises(FeedError) as error:
        restarted.service.subscribe(['left_mdi'], original_revision)
    assert error.value.status_code == 409
    assert not first.calls and not restarted.calls


def proof_fixture():
    f = fixture()
    f.mono, f.proof_calls, f.context_calls = [10.0], [], []
    f.service.monotonic = lambda: f.mono[0]
    def read_plan():
        f.proof_calls.append(f.mono[0])
        return f.plan  # Deliberately mutable: the service must own a copy.
    def context():
        f.context_calls.append(dict(f.ctx))
        return dict(f.ctx)
    f.setup.capture_plan = read_plan
    f.service.context_provider = context
    f.service.enable(True)
    f.proof_calls.clear()
    return f


def test_layout_proof_reused_only_through_250ms_and_context_remains_fresh():
    f = proof_fixture()
    initial = f.service._refresh_gate()
    assert initial['status'] == 'Ready' and len(f.proof_calls) == 1
    assert len(f.context_calls) == 2  # Miss also checks context after file I/O.
    f.plan['revision'] = 'changed-files'
    f.mono[0] = 10.249
    assert f.service._refresh_gate()['revision'] == initial['revision']
    assert len(f.proof_calls) == 1 and len(f.context_calls) == 3
    f.mono[0] = 10.25
    assert f.service._refresh_gate()['revision'] == initial['revision']
    f.mono[0] = 10.250001
    assert f.service._refresh_gate()['revision'] != initial['revision']
    assert len(f.proof_calls) == 2


def expire_cached_proof_during_use(f, *, waiter=False):
    initial = f.service._refresh_gate()
    f.mono[0] = 10.249
    original = f.service._layout_for_context
    attempts = []
    def crossing_boundary(*args, **kwargs):
        attempts.append(kwargs.get('force_fresh', False))
        proof, reread_context, freshly_validated = original(*args, **kwargs)
        if not freshly_validated:
            f.mono[0] += .002
        return proof, reread_context or waiter, freshly_validated
    f.service._layout_for_context = crossing_boundary
    return initial, attempts


@pytest.mark.parametrize('waiter', [False, True])
def test_reused_proof_crossing_expiry_gets_one_new_validation(waiter):
    f = proof_fixture()
    initial, attempts = expire_cached_proof_during_use(f, waiter=waiter)
    current = f.service._refresh_gate()
    assert current['status'] == 'Ready' and current['revision'] == initial['revision']
    assert attempts == [False, True] and len(f.proof_calls) == 2
    assert f.service._layout_proof['started_mono'] > 10.25
    assert f.service.last_context_error is None


@pytest.mark.parametrize('failure', ['slow-proof', 'slow-context', 'error', 'missing', 'process', 'session'])
def test_boundary_refresh_is_bounded_and_never_accepts_a_failed_new_proof(failure):
    f = proof_fixture()
    _, attempts = expire_cached_proof_during_use(f)
    f.service.frames = {'left_mdi': {'must': 'be cleared'}}
    original = f.setup.capture_plan
    fresh_reads = []
    def fresh_proof():
        fresh_reads.append(True)
        if failure == 'error':
            raise OSError('Could not verify current files')
        if failure == 'missing':
            return None
        if failure == 'slow-proof':
            f.mono[0] += .251
        if failure in ('process', 'session'):
            f.ctx[failure] = 'changed-during-refresh'
        if failure == 'slow-context':
            previous = f.service.context_provider
            def delayed_context():
                f.mono[0] += .251
                return previous()
            f.service.context_provider = delayed_context
        return original()
    f.setup.capture_plan = fresh_proof
    current = f.service._refresh_gate()
    assert current['status'] != 'Ready' and not f.service.frames
    assert attempts == [False, True] and len(fresh_reads) == 1
    if failure in ('process', 'session'):
        assert 'process or mission session changed' in current['detail']
    assert f.service._layout_proof is None or f.service._layout_proof['plan'] is None


def test_boundary_refresh_never_retries_a_process_or_session_mismatch():
    f = proof_fixture()
    _, attempts = expire_cached_proof_during_use(f, waiter=True)
    previous = f.service.context_provider
    reads = []
    def context():
        reads.append(True)
        if len(reads) == 2:
            f.ctx['session'] = 'changed-while-waiting'
        return previous()
    f.service.context_provider = context
    current = f.service._refresh_gate()
    assert current['status'] == 'Unavailable'
    assert 'process or mission session changed' in current['detail']
    assert attempts == [False] and len(f.proof_calls) == 1


@pytest.mark.parametrize('key', ['process', 'session'])
def test_layout_proof_never_crosses_process_or_session(key):
    f = proof_fixture()
    initial = f.service._refresh_gate()
    f.ctx[key] = 'new-'+key
    assert f.service._refresh_gate()['revision'] != initial['revision']
    assert len(f.proof_calls) == 2


@pytest.mark.parametrize('change', ['enable', 'apply', 'restore', 'close'])
def test_layout_cache_immediately_invalidated_by_service_changes(change):
    f = proof_fixture()
    f.service._refresh_gate()
    assert f.service._layout_proof is not None
    if change == 'enable':
        f.service.enable(False)
    elif change == 'close':
        f.service.close()
    else:
        setattr(f.setup, change, lambda *args: {'ok': True})
        getattr(f.service, change)(*(['review'] if change == 'apply' else []))
    assert f.service._layout_proof is None


@pytest.mark.parametrize('failure', ['changed-files', 'exception', 'slow-proof'])
def test_expired_proof_cannot_fall_back_after_failure(failure):
    f = proof_fixture()
    f.service._refresh_gate()
    f.service.frames = {'left_mdi': {'must': 'be cleared'}}
    f.mono[0] += .251
    def failing_proof():
        if failure == 'exception':
            raise OSError('Unavailable')
        if failure == 'slow-proof':
            f.mono[0] += .251
            return f.plan
        return None
    f.setup.capture_plan = failing_proof
    gate = f.service._refresh_gate()
    assert gate['status'] != 'Ready' and not f.service.frames
    assert f.service._layout_proof is None or f.service._layout_proof['plan'] is None


def test_typed_context_error_retained_after_recovery_without_changing_fences():
    f = proof_fixture()
    original = f.setup.capture_plan
    def slow_proof():
        result = original()
        f.mono[0] += .251
        return result
    f.setup.capture_plan = slow_proof
    gate = f.service._refresh_gate()
    assert gate['status'] == 'Unavailable'
    assert gate['detail'] == 'The display layout proof expired during validation.'
    saved = dict(f.service.last_context_error)
    assert saved == {'occurred_epoch': 100., 'type': 'FeedError', 'detail': gate['detail']}
    f.setup.capture_plan = original
    assert f.service._refresh_gate()['status'] == 'Ready'
    status = f.service.status()
    assert status['last_context_error'] == saved
    status['last_context_error']['detail'] = 'caller mutation'
    assert f.service.last_context_error == saved


def test_unknown_context_error_text_is_redacted_and_diagnostic_is_bounded():
    f = proof_fixture()
    def broken():
        raise RuntimeError('secret-token-and-private-path')
    f.service.context_provider = broken
    assert f.service._refresh_gate()['status'] == 'Unavailable'
    status = f.service.status()
    assert 'secret-token' not in json.dumps(status)
    assert status['last_context_error']['type'] == 'RuntimeError'
    assert set(status['last_context_error']) == {'occurred_epoch', 'type', 'detail'}


@pytest.mark.parametrize('change', ['read-error', 'process-missing', 'model-reset', 'lifecycle'])
def test_mismatch_diagnostics_distinguish_actual_session_evidence_and_stay_bounded(change):
    f = proof_fixture()
    f.ctx['telemetry_evidence'] = {'read_status': 'ok', 'read_failures': 0, 'hello': 1, 'bye': 0,
                                   'generation': 1, 'process_matches': 1, 'private': 'secret-path'}
    f.ctx['model_time'] = 50
    def changed():
        evidence = dict(f.ctx['telemetry_evidence'])
        f.ctx['session'] = 'secret-session-value'
        if change == 'read-error':
            evidence.update(read_status='permission', read_failures=1, hello=None, bye=None,
                            last_read_error={'status': 'permission', 'winerror': 32, 'epoch': 100,
                                             'path': 'secret-path'})
            f.ctx['telemetry_valid'] = False
        elif change == 'process-missing':
            f.ctx['process'] = None
            evidence['process_matches'] = 0
        elif change == 'model-reset':
            f.ctx['model_time'] = 1
            evidence.update(generation=2, generation_reason='model-reset')
        else:
            evidence['hello'] = 2
        f.ctx['telemetry_evidence'] = evidence
        return f.plan
    f.setup.capture_plan = changed
    assert f.service._refresh_gate()['status'] == 'Unavailable'
    diagnostic = f.service.last_context_error['mismatch']
    assert 'session' in diagnostic['changed']
    assert ('process' in diagnostic['changed']) == (change == 'process-missing')
    assert diagnostic['before']['hello'] == 1
    after = diagnostic['after']
    if change == 'read-error':
        assert after['last_read_error']['winerror'] == 32 and after['hello'] is None
    elif change == 'process-missing':
        assert after['process_matches'] == 0
    elif change == 'model-reset':
        assert after['generation'] == 2 and after['generation_reason'] == 'model-reset'
    else:
        assert after['hello'] == 2
    assert 'secret' not in json.dumps(diagnostic) and len(json.dumps(diagnostic)) < 2500
    f.setup.capture_plan = lambda: f.plan
    status = f.service.status()
    assert status['status'] == 'Ready' or change in ('read-error', 'process-missing')
    status['last_context_error']['mismatch']['after']['hello'] = 999
    assert f.service.last_context_error['mismatch']['after']['hello'] != 999


@pytest.mark.parametrize('change', ['process', 'session', 'focus', 'time'])
def test_context_is_rechecked_after_full_proof_read(change):
    f = proof_fixture()
    def changed_during_proof():
        if change in ('process', 'session'):
            f.ctx[change] = 'new-'+change
        elif change == 'focus':
            f.ctx['focused'] = False
        else:
            # Proof itself finishes quickly, but its subsequent context read
            # cannot make an expired proof appear newly validated.
            original = f.service.context_provider
            def slow_context():
                f.mono[0] += .251
                return original()
            f.service.context_provider = slow_context
        return f.plan
    f.setup.capture_plan = changed_during_proof
    assert f.service._refresh_gate()['status'] != 'Ready'
    if change != 'focus':
        assert f.service._layout_proof is None


def test_waiter_rechecks_context_even_when_it_reuses_a_fresh_proof():
    f = proof_fixture()
    f.service._refresh_gate()
    consumed = threading.Event()
    original = f.service.context_provider
    def context():
        result = original()
        consumed.set()
        return result
    f.service.context_provider = context
    results = []
    with f.service.layout_proof_lock:
        worker = threading.Thread(target=lambda: results.append(f.service._refresh_gate()))
        worker.start()
        assert consumed.wait(1)
        f.ctx['session'] = 'changed-while-waiting'
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert results[0]['status'] == 'Unavailable'
    assert f.service._layout_proof is None
    assert len(f.proof_calls) == 1  # It reused the proof, then refused old context.


def test_layout_proof_plans_are_immutable_across_cache_boundaries():
    f = proof_fixture()
    first = f.service._refresh_gate()
    original_x = first['plan']['panels']['left_mdi']['x']
    f.plan['panels']['left_mdi']['x'] += 500
    first['plan']['panels']['left_mdi']['x'] += 1000
    second = f.service._refresh_gate()
    assert second['plan']['panels']['left_mdi']['x'] == original_x
    assert len(f.proof_calls) == 1


def test_fresh_setup_snapshot_can_invalidate_proof_before_250ms():
    f = proof_fixture()
    f.service._refresh_gate()
    f.setup.snapshot = lambda: {'status': 'changed'}
    f.setup.capture_plan = lambda: None
    assert f.service.status()['status'] != 'Ready'


def test_known_invalid_snapshot_cannot_be_overwritten_by_an_inflight_old_proof():
    f = proof_fixture()
    f.service._refresh_gate()
    f.mono[0] += .251
    entered, release, snapshot_seen = threading.Event(), threading.Event(), threading.Event()
    reads = []
    def proof():
        reads.append(True)
        if len(reads) == 1:
            old = copy.deepcopy(f.plan)
            entered.set()
            assert release.wait(1)
            return old
        return None  # The fresh read observes the changed files.
    f.setup.capture_plan = proof
    old_results, status_results = [], []
    old_reader = threading.Thread(target=lambda: old_results.append(f.service._refresh_gate()))
    old_reader.start()
    assert entered.wait(1)
    def snapshot():
        snapshot_seen.set()
        return {'status': 'changed'}
    f.setup.snapshot = snapshot
    status_reader = threading.Thread(target=lambda: status_results.append(f.service.status()))
    status_reader.start()
    assert snapshot_seen.wait(1)
    wait_for(lambda: f.service.gate['status'] == 'Unavailable')
    release.set()
    old_reader.join(timeout=1)
    status_reader.join(timeout=1)
    assert not old_reader.is_alive() and not status_reader.is_alive()
    assert old_results[0]['status'] != 'Ready' and status_results[0]['status'] != 'Ready'
    assert len(reads) == 2 and not f.service.frames
    assert f.service._layout_proof is None or f.service._layout_proof['plan'] is None


def test_known_invalid_snapshot_also_fences_an_already_copied_cache_hit():
    f = proof_fixture()
    f.service._refresh_gate()
    copied, release = threading.Event(), threading.Event()
    original = f.service._layout_for_context
    def copied_before_invalidation(*args, **kwargs):
        result = original(*args, **kwargs)
        if threading.current_thread().name == 'old-cache-reader':
            copied.set()
            assert release.wait(1)
        return result
    f.service._layout_for_context = copied_before_invalidation
    results = []
    worker = threading.Thread(target=lambda: results.append(f.service._refresh_gate()), name='old-cache-reader')
    worker.start()
    assert copied.wait(1)
    f.setup.snapshot = lambda: {'status': 'changed'}
    f.setup.capture_plan = lambda: None
    assert f.service.status()['status'] != 'Ready'
    release.set()
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert results[0]['status'] != 'Ready' and f.service.gate['status'] != 'Ready'


def test_single_dedicated_producer_multiplexes_three_panels_and_shared_encoding():
    f = fixture()
    revision, first = connect(f)
    wait_for(lambda: len(latest(f, first)[1]) == 3)
    second = f.service.subscribe(list(PANELS), revision)
    first_frames, second_frames = latest(f, first)[1], latest(f, second)[1]
    assert len(first_frames) == len(second_frames) == 3
    assert all(a[3] is b[3] for a, b in zip(first_frames, second_frames))
    assert {call[3] for call in f.calls} == {'DCS-display-producer'}
    packets = [json.loads(row[3]) for row in first_frames]
    assert {p['panel'] for p in packets} == set(PANELS)
    assert all(p['type'] == 'frame' and p['revision'] == revision for p in packets)
    assert all(base64.b64decode(p['jpeg_base64']) == b'\xff\xd8fake\xff\xd9' for p in packets)
    assert all(len(row[3]) < f.service.MAX_PACKET_BYTES for row in first_frames)
    producer = f.service.producer
    f.service.unsubscribe(second)
    assert f.service.producer is producer


def test_stream_sequences_increase_and_no_per_viewer_backlog():
    f = fixture()
    revision, ident = connect(f, ['left_mdi'])
    first = wait_for(lambda: latest(f, ident)[1])
    seen = first[0][1]
    wait_for(lambda: f.service.sequences['left_mdi'] >= seen+2)
    state, frames, ended = f.service.stream_snapshot(ident, {'left_mdi': seen})
    assert not ended and len(frames) == 1
    assert frames[0][1] >= seen+2
    f.service.unsubscribe(ident)
    reconnect = f.service.subscribe(['left_mdi'], revision)
    assert latest(f, reconnect)[1][0][1] >= frames[0][1]


@pytest.mark.parametrize('panels', [[], ['left_mdi']*2, ['desktop'], ['left_mdi','right_mdi','ampcd','desktop'], [None], [{}]])
def test_only_one_to_three_registered_unique_panels(panels):
    f = fixture()
    f.service.enable(True)
    with pytest.raises(FeedError) as error:
        f.service.subscribe(panels, f.service.status()['revision'])
    assert error.value.status_code == 422
    assert not f.calls


def test_subscriber_bound_and_disconnect_releases_slot():
    f = fixture()
    revision, ident = connect(f)
    ids = [ident]+[f.service.subscribe(['left_mdi'], revision) for _ in range(7)]
    with pytest.raises(FeedError) as error:
        f.service.subscribe(['ampcd'], revision)
    assert error.value.status_code == 429
    f.service.unsubscribe(ids.pop())
    assert f.service.subscribe(['ampcd'], revision)


def test_no_native_work_after_last_subscriber_disconnect_or_disable():
    f = fixture()
    _, ident = connect(f)
    wait_for(lambda: len(latest(f, ident)[1]) == 3)
    f.service.unsubscribe(ident)
    wait_for(lambda: not f.service.frames)
    count = len(f.calls)
    time.sleep(.12)
    assert len(f.calls) == count
    assert f.service.producer.is_alive()  # Idle daemon waits; it does not poll DCS.
    f.service.close()
    assert not f.service.producer.is_alive()


def test_legacy_frame_reader_only_leases_shared_producer_and_initially_returns_bounded_wait():
    f = fixture()
    f.service.enable(True)
    revision = f.service.status()['revision']
    with pytest.raises(FeedError, match='shared display producer') as error:
        f.service.frame('left_mdi', revision)
    assert error.value.status_code == 503
    wait_for(lambda: f.service.frames.get('left_mdi'))
    image = f.service.frame('left_mdi', revision)
    assert image['jpeg'] == b'\xff\xd8fake\xff\xd9'
    assert {call[3] for call in f.calls} == {'DCS-display-producer'}
    assert len(f.service.frames) == 3


@pytest.mark.parametrize('key,value', [('telemetry_valid', False), ('focused', False),
    ('model_advancing', False), ('setup_matches_process', False), ('aircraft', 'F-22A'),
    ('session', None), ('process', None), ('aircraft', None)])
def test_ineligible_context_clears_all_frames_and_ends_old_streams(key, value):
    f = fixture()
    _, ident = connect(f)
    wait_for(lambda: len(latest(f, ident)[1]) == 3)
    f.ctx[key] = value
    f.service.status()
    state, frames, ended = latest(f, ident)
    assert ended and not frames and not f.service.frames
    assert state['status'] != 'Ready'


@pytest.mark.parametrize('change', ['session', 'process', 'layout'])
def test_revision_change_ends_old_stream_without_old_pixels(change):
    f = fixture()
    revision, ident = connect(f)
    wait_for(lambda: len(latest(f, ident)[1]) == 3)
    if change == 'layout': f.plan['revision'] = 'new-layout'
    else: f.ctx[change] = 'changed'
    if change == 'layout':
        wait_for(lambda: f.service.status()['revision'] != revision, timeout=.7)
    current = f.service.status()
    assert current['revision'] != revision
    state, frames, ended = latest(f, ident)
    assert ended and not frames
    with pytest.raises(FeedError) as error:
        f.service.subscribe(['left_mdi'], revision)
    assert error.value.status_code == 409


def test_disable_is_prompt_while_native_capture_blocked_and_late_result_cannot_publish():
    f = fixture()
    entered, release = threading.Event(), threading.Event()
    def capture(*args):
        entered.set()
        assert release.wait(2)
        return f.capture(*args)
    f.service.capture = capture
    _, ident = connect(f)
    assert entered.wait(1)
    started = time.monotonic()
    f.service.enable(False)
    assert time.monotonic()-started < .1
    state, frames, ended = latest(f, ident)
    assert ended and not frames and not state['enabled']
    release.set()
    wait_for(lambda: len(f.calls) == 1)
    time.sleep(.02)
    assert not f.service.frames


def test_slow_context_read_never_holds_state_lock_and_cannot_overwrite_disable():
    f = fixture()
    entered, release = threading.Event(), threading.Event()
    def context():
        if threading.current_thread().name == 'DCS-display-producer':
            entered.set()
            assert release.wait(2)
        return dict(f.ctx)
    f.service.context_provider = context
    _, ident = connect(f)
    assert entered.wait(1)
    start = time.monotonic()
    f.service.enable(False)
    assert time.monotonic()-start < .1
    assert latest(f, ident)[2]
    release.set()
    time.sleep(.03)
    assert not f.calls and not f.service.frames


@pytest.mark.parametrize('field,value', [('jpeg', b'not-jpeg'), ('jpeg', b'\xff\xd8'+b'x'*2097152+b'\xff\xd9'),
    ('captured_epoch', 97), ('captured_epoch', 101), ('captured_epoch', float('nan')),
    ('width', 1024), ('height', 0), ('process_identity', 'other')],
    ids=['not-jpeg', 'oversized', 'old', 'future', 'nan', 'wrong-width', 'wrong-height', 'wrong-process'])
def test_bad_capture_never_publishes(field, value):
    f = fixture()
    f.service.capture = lambda *args: {**f.capture(*args), field: value}
    _, ident = connect(f)
    wait_for(lambda: f.service.last_error)
    assert not f.service.frames
    state, frames, ended = latest(f, ident)
    assert ended and not frames and state['status'] != 'Ready'


def test_stale_frame_and_stale_context_are_never_drained_from_memory():
    f = fixture()
    _, ident = connect(f)
    wait_for(lambda: len(latest(f, ident)[1]) == 3)
    # Hold only the state lock so the producer cannot race these two read-only checks.
    with f.service.lock:
        f.now[0] += 1.01
        assert not latest(f, ident)[1]
        f.service.gate['checked_mono'] = f.service.monotonic()-1.01
        state, frames, ended = latest(f, ident)
        assert ended and not frames and state['status'] == 'Unavailable'


def test_missing_layout_and_closed_service_never_capture_or_modify_setup():
    f = fixture()
    f.setup.capture_plan = lambda: None
    with pytest.raises(FeedError):
        f.service.enable(True)
    f.service.close()
    for action in (lambda: f.service.prepare('screen'), lambda: f.service.apply('plan'), f.service.restore):
        with pytest.raises(FeedError, match='Restart the companion'):
            action()
    assert not f.calls


def api_fixture(f):
    settings = Settings(lan_enabled=True, host='192.168.1.20', origins=('http://testserver',), pairing_token='x'*40)
    app = create_app(settings, SimpleNamespace(display_feed=f.service, tick=lambda: None))
    return app


def test_api_phone_read_only_auth_and_exact_origin_unchanged():
    f = fixture()
    app = api_fixture(f)
    origin = {'Origin': 'http://testserver'}
    with TestClient(app, client=('192.168.1.40', 1000)) as phone:
        for path in ('/api/display-feed', '/api/display-feed/stream?panels=left_mdi&revision=x',
                     '/api/display-feed/left_mdi/frame.jpg?revision=x'):
            assert phone.get(path).status_code == 401
        assert phone.post('/api/session', json={'token': 'x'*40}, headers=origin).status_code == 200
        assert phone.get('/api/display-feed').json()['can_edit'] is False
        for path, body in [('enabled', {'enabled': True}), ('plan', {'monitor_id': 'screen'}),
                           ('apply', {'plan_id': 'plan'}), ('restore', {})]:
            assert phone.post('/api/display-feed/'+path, json=body, headers=origin).status_code == 403
        f.service.enable(True)
        revision = f.service.status()['revision']
        assert phone.get('/api/display-feed/stream', params={'panels': 'desktop', 'revision': revision}).status_code == 422
        assert phone.get('/api/display-feed/stream', params={'panels': 'left_mdi,left_mdi', 'revision': revision}).status_code == 422
        assert phone.get('/api/display-feed/stream', params={'panels': 'left_mdi', 'revision': 'old'}).status_code == 409
        assert phone.get('/api/display-feed', headers={'Origin': 'http://evil.invalid'}).status_code == 403


def stream_request(app, token):
    async def receive():
        await asyncio.sleep(.001)
        return {'type': 'http.request', 'body': b'', 'more_body': False}
    return Request({'type': 'http', 'method': 'GET', 'path': '/api/display-feed/stream',
                    'headers': [(b'host', b'testserver'), (b'cookie', f'{COOKIE}={token}'.encode())],
                    'client': ('192.168.1.40', 123), 'query_string': b''}, receive=receive)


async def open_stream(f, app, panels='left_mdi,right_mdi,ampcd'):
    auth = app.state.auth
    with auth.lock:
        token = auth._session_locked(auth.clock())
    request = stream_request(app, token)
    endpoint = next(route.endpoint for route in app.routes if getattr(route, 'path', None) == '/api/display-feed/stream')
    response = await endpoint(request, panels=panels, revision=f.service.status()['revision'])
    return response, token


async def next_packet(iterator):
    return json.loads(await asyncio.wait_for(iterator.__anext__(), timeout=1))


def test_async_stream_sends_status_then_all_panels_and_releases_on_disconnect():
    f = fixture()
    f.service.enable(True)
    app = api_fixture(f)
    async def exercise():
        response, _ = await open_stream(f, app)
        assert response.media_type == 'application/x-ndjson'
        assert response.headers['cache-control'] == 'no-store'
        iterator = response.body_iterator
        initial = await next_packet(iterator)
        assert initial['type'] == 'status' and initial['status'] == 'Ready'
        panels = set()
        while len(panels) < 3:
            packet = await next_packet(iterator)
            if packet['type'] == 'frame': panels.add(packet['panel'])
        assert panels == set(PANELS)
        await iterator.aclose()
        assert not f.service.subscribers
    asyncio.run(exercise())


def test_stream_auth_revocation_ends_without_more_pixels():
    f = fixture()
    f.service.enable(True)
    app = api_fixture(f)
    async def exercise():
        response, token = await open_stream(f, app)
        iterator = response.body_iterator
        assert (await next_packet(iterator))['type'] == 'status'
        with app.state.auth.lock:
            app.state.auth.sessions.pop(token)
        packets = []
        async for data in iterator:
            packets.append(json.loads(data))
        assert all(p['type'] == 'status' for p in packets)
        assert not f.service.subscribers
    asyncio.run(exercise())


def test_slow_stream_reader_gets_latest_frames_not_a_backlog_or_expired_frame():
    f = fixture()
    f.service.enable(True)
    app = api_fixture(f)
    async def exercise():
        response, _ = await open_stream(f, app)
        iterator = response.body_iterator
        assert (await next_packet(iterator))['type'] == 'status'
        first = await next_packet(iterator)
        while first['type'] != 'frame': first = await next_packet(iterator)
        before = dict(f.service.sequences)
        await asyncio.sleep(.25)
        following = await next_packet(iterator)
        while following['type'] != 'frame': following = await next_packet(iterator)
        assert following['sequence'] > before[following['panel']]
        await iterator.aclose()
    asyncio.run(exercise())


def test_cancelled_subscribe_releases_late_lease():
    f = fixture()
    f.service.enable(True)
    app = api_fixture(f)
    entered, release = threading.Event(), threading.Event()
    original = f.service.subscribe
    def slow(*args):
        entered.set()
        assert release.wait(2)
        return original(*args)
    f.service.subscribe = slow
    async def exercise():
        task = asyncio.create_task(open_stream(f, app))
        while not entered.is_set(): await asyncio.sleep(.002)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        for _ in range(100):
            await asyncio.sleep(.005)
            if f.service.producer is not None and not f.service.subscribers:
                break
        assert not f.service.subscribers
    asyncio.run(exercise())


def test_pc_enable_still_rejects_arbitrary_rectangles_and_non_boolean_modes():
    f = fixture()
    app = api_fixture(f)
    with TestClient(app, client=('127.0.0.1', 1000)) as pc:
        origin = {'Origin': 'http://testserver'}
        pc.post('/api/session', json={}, headers=origin)
        for body in ({'enabled': True, 'rectangle': {}}, {'enabled': 'yes'}):
            assert pc.post('/api/display-feed/enabled', json=body, headers=origin).status_code == 422
        assert not f.calls
        assert pc.post('/api/display-feed/enabled', json={'enabled': True}, headers=origin).status_code == 200
