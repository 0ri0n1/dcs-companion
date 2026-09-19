"""One-use, same-DCS-process restart checkpoint for the existing collector.

Keeps the current capability probe and rolling flight history during this upgrade.
Never restores cockpit display text, packet counters or receive timestamps.
Paused ownship samples may be old; their original age is preserved, never reset.
"""
from collections import deque
import copy
import json
import math
from pathlib import Path
import time

MAX_SAMPLE_AGE_S = 24 * 60 * 60


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def same_dcs_process(expected):
    from webapp.launcher import dcs_sessions
    return dcs_sessions() == [expected]


def restore_checkpoint(collector, path, *, now=None, validate=same_dcs_process):
    now = time.time() if now is None else now
    path = Path(path)
    try:
        if path.stat().st_size > 16_000_000:
            return False
        payload = json.loads(path.read_text(encoding="utf-8"))
        created = payload.get("created_epoch")
        if not _finite(now) or not _finite(created) or not 0 <= now-created <= 10:
            return False
        if payload.get("schema") != "dcs-companion/collector-checkpoint/1" or not validate(payload.get("dcs_session")):
            return False
        state = payload["state"]
        written = state["written_epoch"]
        age = state["stream"]["data_age_seconds"]
        if not all(_finite(n) for n in (written, age)):
            return False
        # The checkpoint must be fresh, but a paused simulator's last packet need
        # not be. Preserve written-age below so the resumed state stays stale.
        if written > now + 0.5 or age < 0 or not 0 <= now-written+age <= MAX_SAMPLE_AGE_S:
            return False
        latest = state.get("raw_latest")
        if not isinstance(latest, dict) or latest.get("type") != "telemetry" or not latest.get("name"):
            return False
        capability = state.get("capabilities") or {}
        probe = capability.get("probe") or {}
        samples = state.get("history", {}).get("samples", [])
        restored_history = deque(copy.deepcopy(s) for s in samples[-400:]
                                 if isinstance(s, dict) and _finite(s.get("epoch"))
                                 and 0 <= now-s["epoch"] <= 300) if isinstance(samples, list) else deque()
        # Complete validation before mutating the new collector instance.
        collector.latest = copy.deepcopy(latest)
        collector.latest_epoch = written-age
        collector.display_identity = (latest.get("name"), latest.get("unit"))
        collector.display_model_time = latest.get("t")
        if probe.get("settled") is True and probe.get("aircraft") == latest.get("name"):
            collector.capabilities = {**copy.deepcopy(probe), "type": "probe",
                                      "verdicts": copy.deepcopy(capability.get("verdicts") or {})}
            received = probe.get("received_epoch")
            collector.probe_received_epoch = received if _finite(received) and 0 < received <= written + 0.05 else 0.0
        collector.history = restored_history
        collector.last_hist = collector.history[-1]["epoch"] if collector.history else 0
        # A newly received telemetry packet must prove connectivity again.
        collector.connected = False
        return True
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False
    finally:
        # A checkpoint is never reusable by a later process or DCS session.
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
