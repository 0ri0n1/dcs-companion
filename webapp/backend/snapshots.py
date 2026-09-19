"""Share only an in-progress read; never cache telemetry for a later request."""
from concurrent.futures import Future
import threading


class SharedSnapshot:
    def __init__(self):
        self._lock = threading.Lock()
        self._inflight = None

    def read(self, build):
        with self._lock:
            future = self._inflight
            leader = future is None
            if leader:
                future = self._inflight = Future()
        if not leader:
            return future.result()
        try:
            value = build()
        except BaseException as exc:
            future.set_exception(exc)
            raise
        else:
            future.set_result(value)
            return value
        finally:
            with self._lock:
                if self._inflight is future:
                    self._inflight = None
