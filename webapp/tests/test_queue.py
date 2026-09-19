import concurrent.futures
import json
from uuid import uuid4
import pytest
from webapp.backend.command_queue import CommandQueue, QueueError, PRE_SEND


@pytest.fixture
def rig(tmp_path):
    clock = [100.0]
    ctx = dict(aircraft="F-22A", session="s1", mission="m1", process="p1", bindings_hash="b1",
               telemetry_valid=True, display_valid=True, focused=False, bindings_valid=True,
               model_advancing=True, sample_epoch=99.0, model_time=50.0)
    actions = [dict(id="f22.nav.next", name="Next Waypoint, Airfield Or Target", combo="LCtrl+`",
                    risk="benign", observable=False, aircraft="F-22A")]
    sent = []
    def sender(action, item):
        sent.append(item["request_id"])
        return {"ok": True, "sent": True, "dry_run": False}
    q = CommandQueue(tmp_path / "queue.db", lambda: dict(ctx), lambda: list(actions), sender,
                     clock=lambda: clock[0], dry_run=False, settle_s=.8)
    yield q, ctx, actions, clock, sent, tmp_path
    q.close()


def enqueue(rig):
    return rig[0].enqueue(str(uuid4()), "f22.nav.next", True)


def send(rig):
    q, ctx, _, clock, *_ = rig
    ctx["focused"] = True
    q.tick()
    clock[0] += 1
    ctx["model_time"] += 1
    q.tick()


def test_waits_for_focus_then_settles_once(rig):
    q, ctx, _, clock, sent, _ = rig
    item = enqueue(rig)
    q.tick()
    assert q.get()["state"] == "Awaiting DCS Focus" and not sent
    ctx["focused"] = True
    q.tick()
    assert q.get()["state"] == "Settling" and not sent
    clock[0] += .4
    q.tick()
    assert not sent
    clock[0] += .5
    ctx["model_time"] += .9
    q.tick()
    assert q.get()["state"] == "Confirming"
    assert sent == [item["request_id"]]
    clock[0] += 1
    q.tick()
    assert q.get()["state"] == "Unconfirmed"
    assert q.get()["detail"] == "Sent—state not observable"
    for _ in range(5):
        q.tick()
    assert len(sent) == 1


@pytest.mark.parametrize("key", ["aircraft", "session", "mission", "process", "bindings_hash"])
def test_identity_change_cancels(rig, key):
    q, ctx, *rest = rig
    enqueue(rig)
    ctx[key] = "changed"
    q.tick()
    assert q.get()["state"] == "Cancelled"
    assert not rig[4]


@pytest.mark.parametrize("key", ["telemetry_valid", "model_advancing", "bindings_valid"])
def test_invalid_telemetry_menu_pause_or_bindings_reject(rig, key):
    enqueue(rig)
    rig[1][key] = False
    rig[0].tick()
    assert rig[0].get()["state"] == "Rejected"
    assert not rig[4]


def test_stale_displays_independent(rig):
    rig[2][0]["requires_display"] = True
    enqueue(rig)
    rig[1]["display_valid"] = False
    rig[0].tick()
    assert rig[0].get()["state"] == "Rejected"


@pytest.mark.parametrize("name", ["Pitch Up", "Roll Right", "Yaw", "Rudder", "Aileron", "Elevator", "View Left", "Analog Throttle Axis", "Weapon release", "Gun trigger", "Jettison", "Ejection", "Engine shutdown", "Canopy", "Emergency", "Master arm"])
def test_dangerous_or_flight_control_refused_even_if_catalog_mislabels(rig, name):
    rig[2][0]["name"] = name
    assert enqueue(rig)["state"] == "Rejected"
    send(rig)
    assert not rig[4]


@pytest.mark.parametrize("phase", sorted(PRE_SEND))
def test_cancel_in_every_pre_send_state(rig, phase):
    item = enqueue(rig)
    rig[0]._transition(item, phase, "fixture")
    assert rig[0].cancel(item["request_id"])["state"] == "Cancelled"
    send(rig)
    assert not rig[4]


@pytest.mark.parametrize("phase", sorted(PRE_SEND))
def test_expiry_in_every_pre_send_state(rig, phase):
    item = enqueue(rig)
    rig[0]._transition(item, phase, "fixture")
    rig[3][0] += 16
    rig[0].tick()
    assert rig[0].get()["state"] == "Expired"
    assert not rig[4]


def test_duplicate_refresh_reconnect_and_second_client_no_replay(rig):
    q = rig[0]
    request = str(uuid4())
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: q.enqueue(request, "f22.nav.next", True), range(12)))
    assert all(item["request_id"] == request for item in results)
    with pytest.raises(QueueError):
        q.enqueue(str(uuid4()), "f22.nav.next", True)
    send(rig)
    for _ in range(5):
        q.enqueue(request, "f22.nav.next", True)
    assert len(rig[4]) == 1


def test_restart_cancels_pending_and_consumes_ambiguous_attempt(rig):
    q = rig[0]
    item = enqueue(rig)
    q.db.execute("UPDATE commands SET attempted=1 WHERE id=?", (item["request_id"],))
    q.db.commit()
    restarted = CommandQueue(rig[5] / "queue.db", lambda: rig[1], lambda: rig[2], q.sender)
    assert restarted.get()["state"] == "Unconfirmed"
    restarted.enqueue(item["request_id"], "f22.nav.next", True)
    restarted.tick()
    assert not rig[4]
    restarted.close()


def test_ambiguous_send_never_retried(rig):
    def fail(action, item):
        rig[4].append(item["request_id"])
        raise OSError("ambiguous")
    rig[0].sender = fail
    enqueue(rig)
    send(rig)
    assert rig[0].get()["state"] == "Unconfirmed"
    for _ in range(3):
        rig[0].tick()
    assert len(rig[4]) == 1


def test_confirmed_needs_fresh_observable_transition(rig):
    q, ctx, actions, clock, *_ = rig
    actions[0]["observable"] = True
    q.confirmer = lambda item, context: context.get("observed") == "expected"
    enqueue(rig)
    send(rig)
    ctx["observed"] = "expected"
    q.tick()
    assert q.get()["state"] == "Confirming"  # same sample cannot confirm
    ctx["sample_epoch"] += 2
    q.tick()
    assert q.get()["state"] == "Confirmed"


def test_dry_run_not_sent(rig):
    rig[0].sender = lambda action, item: {"ok": True, "sent": False, "dry_run": True}
    enqueue(rig)
    send(rig)
    assert rig[0].get()["state"] == "Unconfirmed"
    assert not rig[0].get()["sent"]
    assert "Sent" not in [h["state"] for h in rig[0].get()["history"]]


def test_requires_deliberate_confirmation_even_already_focused(rig):
    rig[1]["focused"] = True
    with pytest.raises(QueueError):
        rig[0].enqueue(str(uuid4()), "f22.nav.next", False)
    assert not rig[4]


def test_wrong_aircraft_rejected_at_selection(rig):
    rig[1]["aircraft"] = "FA-18C_hornet"
    assert enqueue(rig)["state"] == "Rejected"


def test_unknown_action_and_conflict_fail_closed(rig):
    assert rig[0].enqueue(str(uuid4()), "shell.execute", True)["state"] == "Rejected"
    rig[2][0]["conflicts"] = ["other"]
    assert enqueue(rig)["state"] == "Rejected"


def test_case_variant_uuid_cannot_replay(rig):
    request = "abcdefab-1234-4567-89ab-abcdefabcdef"
    q = rig[0]
    q.enqueue(request, "f22.nav.next", True)
    send(rig)
    q.enqueue(request.upper(), "f22.nav.next", True)
    assert len(rig[4]) == 1
    assert q.db.execute("SELECT count(*) FROM commands").fetchone()[0] == 1


def test_graceful_stop_cancels_and_refuses_new_work(rig):
    enqueue(rig)
    rig[0].stop()
    assert rig[0].get()["state"] == "Cancelled"
    with pytest.raises(QueueError):
        enqueue(rig)
    send(rig)
    assert not rig[4]


def test_pause_during_focus_settle_blocks_even_recent_telemetry(rig):
    enqueue(rig)
    rig[1]["focused"] = True
    rig[0].tick()
    rig[3][0] += 1
    rig[0].tick()
    assert rig[0].get()["state"] == "Rejected"
    assert not rig[4]


def test_automatic_binding_refresh_cancels_without_replaying(rig):
    item = enqueue(rig)
    rig[0].invalidate("Bindings changed; refresh pending")
    assert rig[0].get()["state"] == "Cancelled"
    rig[0].enqueue(item["request_id"], item["action_id"], True)
    send(rig)
    assert not rig[4]


def test_refresh_after_send_stops_confirmation_and_never_replays(rig):
    enqueue(rig)
    send(rig)
    assert len(rig[4]) == 1
    rig[0].invalidate("Bindings changed")
    assert rig[0].get()["state"] == "Unconfirmed"
    for _ in range(3):
        rig[0].tick()
    assert len(rig[4]) == 1
