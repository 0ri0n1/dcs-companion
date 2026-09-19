from concurrent.futures import Future, ThreadPoolExecutor
import threading

import pytest

from webapp.backend.snapshots import SharedSnapshot


@pytest.mark.parametrize('failure', [False, True])
def test_concurrent_observers_share_one_build_then_next_read_is_fresh(monkeypatch, failure):
    entered, joined, release = threading.Event(), threading.Event(), threading.Event()

    class JoinedFuture(Future):
        def result(self, timeout=None):
            joined.set()
            return super().result(timeout)

    monkeypatch.setattr('webapp.backend.snapshots.Future', JoinedFuture)
    broker = SharedSnapshot()
    calls = []
    def build():
        calls.append(True)
        entered.set()
        if not release.wait(2):
            raise RuntimeError('test did not release builder')
        if failure:
            raise ValueError('unavailable observation')
        return {'generation': len(calls)}

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(broker.read, build)
        assert entered.wait(2)
        second = pool.submit(broker.read, build)
        try:
            assert joined.wait(2)
            assert len(calls) == 1
        finally:
            release.set()
        if failure:
            for result in (first, second):
                with pytest.raises(ValueError, match='unavailable observation'):
                    result.result(2)
        else:
            assert first.result(2) is second.result(2)
            assert first.result(2) == {'generation': 1}
    # Neither successful observations nor errors persist as a cache.
    if failure:
        assert broker.read(lambda: {'recovered': True}) == {'recovered': True}
    else:
        assert broker.read(build) == {'generation': 2}


def test_independent_app_instances_never_share_observations():
    first, second = SharedSnapshot(), SharedSnapshot()
    assert first.read(lambda: {'app': 1}) == {'app': 1}
    assert second.read(lambda: {'app': 2}) == {'app': 2}
