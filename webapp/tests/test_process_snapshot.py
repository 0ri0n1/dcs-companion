"""Process censuses must not exchange psutil's shared Process.info dictionaries."""
import sys
from types import SimpleNamespace

import psutil
import pytest

from webapp.backend import discovery, telemetry


class Process:
    def __init__(self, pid=42, name="DCS.exe", created=100.25):
        self.values = {"pid": pid, "name": name, "create_time": created}
        self.info = {"name": "unrelated.exe"}

    def as_dict(self, attrs):
        return {key: self.values[key] for key in attrs}

    def exe(self):
        return "X:/fixture/bin/DCS.exe"

    def cmdline(self):
        return [self.exe(), "-w", "DCS"]

    def create_time(self):
        return self.values["create_time"]


def foreground(monkeypatch, read=lambda: "DCS.exe"):
    monkeypatch.setitem(sys.modules, "input_sender", SimpleNamespace(foreground_process=read))


def test_discovery_interleaving_cannot_replace_telemetry_attributes(monkeypatch):
    """Model psutil's same cached Process yielded to two overlapping iterators.

    With process_iter(attrs), discovery replaces the full telemetry info with
    just name/pid before telemetry consumes it. The old code raises KeyError.
    """
    process = Process()
    calls, discovered = [], []
    inside = False

    def process_iter(attrs=None):
        nonlocal inside
        calls.append(attrs)
        if attrs is not None:
            process.info = process.as_dict(attrs)
        if not inside:
            inside = True
            discovered.extend(discovery._processes())
        yield process

    monkeypatch.setattr(psutil, "process_iter", process_iter)
    foreground(monkeypatch)
    assert telemetry.process_snapshot() == {"running": True, "identity": "42:100.25", "focused": True,
                                            "matches": 1, "disappeared": 0}
    assert discovered == [{**process.values, "exe": process.exe(), "cmdline": process.cmdline()}]
    assert calls == [None, None]
    # Neither consumer writes the cache attribute or retains an alias to it.
    process.info["name"] = "changed.exe"
    assert discovered[0]["name"] == "DCS.exe"


def test_process_identity_and_foreground_are_fresh_on_each_census(monkeypatch):
    process = Process()
    monkeypatch.setattr(psutil, "process_iter", lambda: iter([process]))
    focused = ["DCS.exe"]
    foreground(monkeypatch, lambda: focused[0])
    assert telemetry.process_snapshot() == {"running": True, "identity": "42:100.25", "focused": True,
                                            "matches": 1, "disappeared": 0}
    process.values["create_time"] = 200.5
    focused[0] = "other.exe"
    assert telemetry.process_snapshot() == {"running": True, "identity": "42:200.5", "focused": False,
                                            "matches": 1, "disappeared": 0}


@pytest.mark.parametrize("processes,running", [([], False), ([Process(), Process(43)], True)])
def test_only_exactly_one_dcs_process_can_have_identity(monkeypatch, processes, running):
    monkeypatch.setattr(psutil, "process_iter", lambda: iter(processes))
    foreground(monkeypatch, lambda: pytest.fail("Ambiguous/offline census must not claim focus"))
    assert telemetry.process_snapshot() == {"running": running, "identity": None, "focused": False,
                                            "matches": len(processes), "disappeared": 0}


def test_process_exiting_during_private_read_is_skipped(monkeypatch):
    class Exited(Process):
        def as_dict(self, attrs):
            raise psutil.NoSuchProcess(self.values["pid"])

    live = Process()
    monkeypatch.setattr(psutil, "process_iter", lambda: iter([Exited(41), live]))
    foreground(monkeypatch)
    result = telemetry.process_snapshot()
    assert result["identity"] == "42:100.25"
    assert result["disappeared"] == 1
    assert discovery._processes() == [{**live.values, "exe": live.exe(), "cmdline": live.cmdline()}]
