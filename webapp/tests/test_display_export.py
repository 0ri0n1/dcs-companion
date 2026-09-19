import json
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from webapp.backend.display_export import (DisplayExportSetup, FIELDS, PROFILE, VIEWPORT_FILES,
                                    _atomic_write, _options, _profile, _topology)


ORIGINAL = b'''-- preserve this comment\r\noptions = {\r\n  VR = { enable = false },\r\n  graphics = {\r\n    ["multiMonitorSetup"] = "1camera", -- chosen profile\r\n    width = 2560, height = 1440, aspect = 1.7777777777778,\r\n    fullScreen = false, textures = 2,\r\n    note = "width = 123; { graphics = ignored }",\r\n  },\r\n  sound = { volume = 60 },\r\n}\r\n'''


def monitors():
    return [dict(id="primary", name="Primary", primary=True, x=0, y=0, width=2560, height=1440),
            dict(id="secondary", name="Secondary", primary=False, x=2560, y=0, width=2560, height=1440)]


@pytest.fixture
def setup(tmp_path):
    install, saved = tmp_path/"install", tmp_path/"saved"
    (saved/"Config").mkdir(parents=True)
    (saved/"Config/options.lua").write_bytes(ORIGINAL)
    for label, relative in VIEWPORT_FILES.items():
        path = install/relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'try_find_assigned_viewport("{label}")\n', encoding="utf-8")
    processes = []
    devices = monitors()
    service = DisplayExportSetup(tmp_path/"runtime", install, saved,
                                 processes=lambda: processes, monitors=lambda: devices, clock=lambda: 1000.0)
    service.test_processes = processes
    service.test_monitors = devices
    return service


def applied(setup):
    review = setup.plan("secondary")
    result = setup.apply(review["plan_id"])
    assert result["ok"]
    return review


def test_plan_never_writes_saved_games_even_while_running(setup):
    setup.test_processes.append({"name": "DCS.exe", "create_time": 900})
    review = setup.plan("secondary")
    assert setup.options_path.read_bytes() == ORIGINAL
    assert not setup.profile_path.exists()
    assert len(review["changes"]) == 5
    assert review["window"] == dict(x=0, y=0, width=5120, height=1440)
    assert review["panels"]["left_mdi"] == dict(x=3072, y=464, width=512, height=512)
    assert review["secondary_monitor_consumed"] is True


def test_apply_running_refused_without_mutation(setup):
    plan = setup.plan("secondary")
    setup.test_processes.append({"name": "DCS.exe", "create_time": 900})
    with pytest.raises(ValueError, match="Close DCS"):
        setup.apply(plan["plan_id"])
    assert setup.options_path.read_bytes() == ORIGINAL
    assert not setup.profile_path.exists()


@pytest.mark.parametrize("change", ["options", "geometry", "native", "existing"])
def test_stale_plan_refused(setup, change):
    plan = setup.plan("secondary")
    if change == "options":
        setup.options_path.write_bytes(ORIGINAL + b"-- changed\n")
    elif change == "geometry":
        setup.test_monitors[1]["x"] += 1
    elif change == "native":
        path = setup.install/next(iter(VIEWPORT_FILES.values()))
        path.write_text(path.read_text() + "-- new version")
    else:
        setup.profile_path.parent.mkdir()
        setup.profile_path.write_text("-- somebody else's profile")
    before = setup.options_path.read_bytes()
    with pytest.raises(ValueError):
        setup.apply(plan["plan_id"])
    assert setup.options_path.read_bytes() == before


def test_apply_changes_only_owned_spans_and_exact_backup(setup):
    review = applied(setup)
    edited = setup.options_path.read_bytes()
    assert b"-- preserve this comment\r\n" in edited
    assert b'-- chosen profile\r\n' in edited
    assert b'    note = "width = 123; { graphics = ignored }",\r\n' in edited
    assert b'  sound = { volume = 60 },\r\n' in edited
    assert _options(edited)["multiMonitorSetup"] == PROFILE
    backup = setup.runtime/f'backups/{review["plan_id"]}/options.lua'
    assert backup.read_bytes() == ORIGINAL
    profile = setup.profile_path.read_text()
    for label in VIEWPORT_FILES:
        assert label in profile
    assert 'width = 2560; height = 1440;' in profile
    assert setup.snapshot()["status"] == "pending-restart"
    assert setup.capture_plan() is None


def test_capture_requires_new_dcs_process_and_owned_geometry(setup):
    applied(setup)
    setup.test_processes.append({"create_time": 999})
    assert setup.capture_plan() is None
    setup.test_processes[0]["create_time"] = 1001
    capture = setup.capture_plan()
    assert capture["aircraft"] == "FA-18C_hornet"
    assert capture["applied_epoch"] == 1000
    assert capture["profile_path"] == str(setup.profile_path)
    assert capture["panels"]["left_mdi"]["x"] == 3072
    assert setup.snapshot()["status"] == "active"
    setup.test_monitors[1]["x"] += 1
    assert setup.capture_plan() is None


@pytest.mark.parametrize("stamp", [None, float("nan"), True, "1001", 1000])
def test_capture_refuses_unproven_process_start(setup, stamp):
    applied(setup)
    setup.test_processes.append({"create_time": stamp})
    assert setup.capture_plan() is None


def test_capture_accepts_unrelated_dcs_options_rewrite(setup):
    applied(setup)
    setup.test_processes.append({"create_time": 1001})
    edited = setup.options_path.read_bytes().replace(b"volume = 60", b"volume = 40") + b"-- saved by DCS\n"
    setup.options_path.write_bytes(edited)
    assert setup.capture_plan() is not None
    setup.options_path.write_bytes(edited.replace(b"width = 5120", b"width = 5119"))
    assert setup.capture_plan() is None


def test_dcs_lowercase_profile_rewrite_allows_capture_and_preserves_other_edits_on_restore(setup):
    applied(setup)
    setup.test_processes.append({"create_time": 1001})
    assert setup.capture_plan() is not None
    rewritten = setup.options_path.read_bytes().replace(PROFILE.encode(), PROFILE.lower().encode())
    rewritten = rewritten.replace(b"volume = 60", b"volume = 40") + b"-- saved by DCS Options\n"
    setup.options_path.write_bytes(rewritten)
    assert setup.capture_plan() is not None
    assert setup.snapshot()["status"] == "active"
    setup.test_processes.clear()
    assert setup.restore()["ok"]
    restored = setup.options_path.read_bytes()
    assert _options(restored) == _options(ORIGINAL)
    assert b"volume = 40" in restored and restored.endswith(b"-- saved by DCS Options\n")
    assert not setup.profile_path.exists()


def test_review_still_binds_exact_original_bytes_when_case_changes(setup):
    review = setup.plan("secondary")
    changed = ORIGINAL.replace(b'"1camera"', b'"1Camera"')
    setup.options_path.write_bytes(changed)
    with pytest.raises(ValueError, match="changed after review"):
        setup.apply(review["plan_id"])
    assert setup.options_path.read_bytes() == changed
    assert not setup.profile_path.exists()


def test_restore_is_exact_when_no_other_edit(setup):
    applied(setup)
    assert setup.restore()["ok"]
    assert setup.options_path.read_bytes() == ORIGINAL
    assert not setup.profile_path.exists()
    assert setup.snapshot()["status"] == "not-configured"


def test_restore_preserves_unrelated_changes(setup):
    applied(setup)
    changed = setup.options_path.read_bytes().replace(b"volume = 60", b"volume = 40") + b"-- game rewrite\n"
    setup.options_path.write_bytes(changed)
    assert setup.restore()["ok"]
    restored = setup.options_path.read_bytes()
    assert b"volume = 40" in restored and restored.endswith(b"-- game rewrite\n")
    assert _options(restored) == _options(ORIGINAL)


@pytest.mark.parametrize("change", ["options", "profile", "running"])
def test_restore_refuses_conflicts(setup, change):
    applied(setup)
    if change == "options":
        setup.options_path.write_bytes(setup.options_path.read_bytes().replace(b"width = 5120", b"width = 5000"))
    elif change == "profile":
        setup.profile_path.write_text("-- user edited")
    else:
        setup.test_processes.append({"create_time": 1001})
    before, profile = setup.options_path.read_bytes(), setup.profile_path.read_bytes()
    with pytest.raises(ValueError):
        setup.restore()
    assert setup.options_path.read_bytes() == before
    assert setup.profile_path.read_bytes() == profile


@pytest.mark.parametrize("failed_path", ["options.lua", "applied.json"])
def test_partial_apply_rolls_back(setup, failed_path):
    plan = setup.plan("secondary")
    failed = False
    def writer(path, data):
        nonlocal failed
        is_final_state = path.name == "applied.json" and json.loads(data)["status"] == "applied"
        if not failed and ((failed_path == "options.lua" and path == setup.options_path) or
                           (failed_path == "applied.json" and is_final_state)):
            failed = True
            raise OSError("injected write failure")
        _atomic_write(path, data)
    setup.writer = writer
    with pytest.raises(OSError, match="injected"):
        setup.apply(plan["plan_id"])
    assert setup.options_path.read_bytes() == ORIGINAL
    assert not setup.profile_path.exists()
    assert not (setup.runtime/"applied.json").exists()


def test_restore_rollback_if_profile_delete_fails(setup, monkeypatch):
    applied(setup)
    original_options = setup.options_path.read_bytes()
    unlink = Path.unlink
    def fail(path, *args, **kwargs):
        if path == setup.profile_path:
            raise OSError("injected delete failure")
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", fail)
    with pytest.raises(OSError, match="injected"):
        setup.restore()
    assert setup.options_path.read_bytes() == original_options
    assert setup.profile_path.exists()


@pytest.mark.parametrize("mutation", ["one", "three", "overlap", "gap", "staggered", "duplicate", "too-small"])
def test_unsupported_topologies_fail(setup, mutation):
    devices = setup.test_monitors
    if mutation == "one": devices.pop()
    elif mutation == "three": devices.append(dict(devices[1], id="third"))
    elif mutation == "overlap": devices[1]["x"] = 1000
    elif mutation == "gap": devices[1]["x"] = 2561
    elif mutation == "staggered": devices[1]["y"] = 1
    elif mutation == "duplicate": devices[1]["id"] = "primary"
    else:
        for item in devices: item.update(width=800, height=800)
        devices[1]["x"] = 800
    with pytest.raises(ValueError): setup.plan("secondary")


@pytest.mark.parametrize("secondary", [("left", -2560, 0), ("above", 0, -1440), ("below", 0, 1440)])
def test_normalizes_negative_and_vertical_arrangements(setup, secondary):
    _, x, y = secondary
    setup.test_monitors[1].update(x=x, y=y)
    review = applied(setup)
    assert review["window"]["x"] == min(0, x)
    assert review["window"]["y"] == min(0, y)
    panel = review["panels"]["left_mdi"]
    assert panel["x"] == x+512 and panel["y"] == y+464
    profile = setup.profile_path.read_text()
    assert f'x = {panel["x"]-min(x, 0)}; y = {panel["y"]-min(y, 0)};' in profile


@pytest.mark.parametrize("content", [
    ORIGINAL.replace(b"width = 2560", b"width = calculate()"),
    ORIGINAL.replace(b"width = 2560", b"width = 2560, width = 1920"),
    ORIGINAL.replace(b"height = 1440,", b""),
    ORIGINAL.replace(b"enable = false", b"enable = true"),
    ORIGINAL.replace(b'"1camera"', b'"my-custom-profile"'),
    ORIGINAL+b"os.execute('not allowed')\n",
])
def test_ambiguous_or_executable_options_refused(setup, content):
    setup.options_path.write_bytes(content)
    with pytest.raises(ValueError): setup.plan("secondary")
    assert setup.options_path.read_bytes() == content


def test_bom_and_comments_preserved(setup):
    setup.options_path.write_bytes(b"\xef\xbb\xbf"+ORIGINAL)
    applied(setup)
    assert setup.options_path.read_bytes().startswith(b"\xef\xbb\xbf-- preserve")
    setup.restore()
    assert setup.options_path.read_bytes() == b"\xef\xbb\xbf"+ORIGINAL


def test_missing_setup_is_safe(tmp_path):
    service = DisplayExportSetup(tmp_path, None, None, monitors=monitors, processes=lambda: [])
    assert service.snapshot()["status"] == "unavailable"
    assert service.capture_plan() is None
    with pytest.raises(ValueError, match="Select a validated"):
        service.plan("secondary")


def test_selected_primary_and_path_like_ids_refused(setup):
    for ident in ("primary", "../secondary", "", None):
        with pytest.raises(ValueError): setup.plan(ident)
    for ident in ("../applied", "", None, "not-a-uuid"):
        with pytest.raises(ValueError): setup.apply(ident)


def test_unowned_existing_profile_not_overwritten(setup):
    setup.profile_path.parent.mkdir()
    setup.profile_path.write_text("-- mine")
    with pytest.raises(ValueError, match="not owned"):
        setup.plan("secondary")
    assert setup.profile_path.read_text() == "-- mine"


def test_symlink_options_refused(setup, tmp_path):
    external = tmp_path/"external.lua"
    external.write_bytes(ORIGINAL)
    setup.options_path.unlink()
    try:
        setup.options_path.symlink_to(external)
    except OSError:
        pytest.skip("Symlink creation is unavailable")
    with pytest.raises(ValueError, match="symbolic"):
        setup.plan("secondary")


@pytest.mark.parametrize("kind", ["symlink", "junction"])
@pytest.mark.parametrize("location", ["options", "profile", "native", "ancestor"])
def test_capture_rechecks_links_after_a_successful_proof(setup, monkeypatch, kind, location):
    applied(setup)
    setup.test_processes.append({"create_time": 1001})
    assert setup.capture_plan() is not None
    target = {"options": setup.options_path, "profile": setup.profile_path,
              "native": setup.install/next(iter(VIEWPORT_FILES.values())),
              "ancestor": setup.saved_games.parent}[location]
    original_lstat = Path.lstat
    def changed_metadata(path, *args, **kwargs):
        if path == target:
            return SimpleNamespace(st_mode=stat.S_IFLNK if kind == "symlink" else stat.S_IFDIR,
                                   st_reparse_tag=0xA000000C if kind == "symlink" else 0xA0000003)
        return original_lstat(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", changed_metadata)
    # This also runs on Windows without permission to create real symlinks.
    # A previously valid proof cannot conceal a replaced file or ancestor.
    assert setup.capture_plan() is None


def test_capture_refuses_unreadable_path_metadata(setup, monkeypatch):
    applied(setup)
    setup.test_processes.append({"create_time": 1001})
    assert setup.capture_plan() is not None
    target = setup.options_path
    original_lstat = Path.lstat
    def denied_metadata(path, *args, **kwargs):
        if path == target:
            raise PermissionError("Cannot verify this path")
        return original_lstat(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", denied_metadata)
    assert setup.capture_plan() is None


def test_record_roundtrip_survives_service_restart(setup):
    applied(setup)
    other = DisplayExportSetup(setup.runtime.parent, setup.install, setup.saved_games,
                               monitors=monitors, processes=lambda: [{"create_time": 1001}])
    assert other.capture_plan() is not None
    other.processes = lambda: []
    other.restore()
    assert setup.options_path.read_bytes() == ORIGINAL


@pytest.mark.parametrize("configured", [False, True])
def test_monitor_enumeration_capture_error_fails_closed(setup, configured):
    from webapp.backend.display_capture import CaptureError
    if configured:
        applied(setup)
        setup.test_processes.append({"create_time": 1001})
        assert setup.capture_plan() is not None
    original = setup.options_path.read_bytes()
    def failed_monitor_inventory():
        raise CaptureError("monitor_unavailable", "Native enumeration failed")
    setup.monitors = failed_monitor_inventory
    snapshot = setup.snapshot()
    assert snapshot["status"] == "unavailable"
    assert "Monitor enumeration is unavailable" in snapshot["detail"]
    assert snapshot["can_apply"] is False
    assert snapshot["can_restore"] is False
    assert setup.capture_plan() is None
    if not configured:
        with pytest.raises(ValueError, match="Monitor enumeration is unavailable"):
            setup.plan("secondary")
    assert setup.options_path.read_bytes() == original


def add_virtual_below(setup):
    for monitor in setup.test_monitors:
        monitor.update(kind="physical", adapter_device_id="PCI\\DISPLAY", adapter_description="Physical GPU")
    virtual = dict(id="virtual", name="Export surface", primary=False, x=0, y=1440,
                   width=2560, height=512, kind="virtual", adapter_device_id="ROOT\\VIRTUAL_DISPLAY",
                   adapter_description="Verified virtual adapter", adapter_hardware_id="root\\mttvdd")
    setup.test_monitors.append(virtual)
    return virtual


def test_virtual_export_preserves_second_physical_monitor(setup):
    add_virtual_below(setup)
    review = setup.plan("virtual")
    assert review["window"] == dict(x=0, y=0, width=2560, height=1952)
    assert review["panels"] == {
        "left_mdi": dict(x=512, y=1440, width=512, height=512),
        "right_mdi": dict(x=1024, y=1440, width=512, height=512),
        "ampcd": dict(x=1536, y=1440, width=512, height=512),
    }
    assert review["export_kind"] == "virtual"
    assert review["physical_monitors_preserved"] is True
    assert setup.snapshot()["recommended_monitor_id"] == "virtual"
    setup.apply(review["plan_id"])
    assert _options(setup.options_path.read_bytes())["width"] == 2560
    assert _options(setup.options_path.read_bytes())["height"] == 1952
    assert 'x = 512; y = 1440; width = 512; height = 512;' in setup.profile_path.read_text()
    setup.test_processes.append({"create_time": 1001})
    assert setup.capture_plan() is not None
    assert setup.snapshot()["physical_monitors_preserved"] is True


def test_monitor_names_do_not_classify_virtual_devices(setup):
    setup.test_monitors[1]["name"] = "Virtual Display Driver"
    review = setup.plan("secondary")
    assert review["export_kind"] == "unknown"
    assert review["physical_monitors_preserved"] is False
    assert setup.snapshot()["recommended_monitor_id"] is None


def test_physical_export_remains_explicitly_supported(setup):
    setup.test_monitors[1]["kind"] = "physical"
    setup.test_monitors.append(dict(id="other", name="Third", primary=False,
                                   x=0, y=-600, width=800, height=600, kind="physical"))
    review = setup.plan("secondary")
    assert review["export_kind"] == "physical"
    assert review["physical_monitors_preserved"] is False
    assert review["window"] == dict(x=0, y=0, width=5120, height=1440)


@pytest.mark.parametrize("rectangle", [dict(x=2500, y=1400, width=800, height=600),
                                        dict(x=0, y=1500, width=800, height=600)])
def test_unrelated_monitor_must_not_overlap_virtual_render_canvas(setup, rectangle):
    add_virtual_below(setup)
    setup.test_monitors.append(dict(id="other", name="Third physical", primary=False, kind="physical", **rectangle))
    with pytest.raises(ValueError, match="Another monitor overlaps"):
        setup.plan("virtual")


def test_native_adapter_identity_binds_review_and_capture(setup):
    virtual = add_virtual_below(setup)
    review = setup.plan("virtual")
    virtual["adapter_device_id"] = "ROOT\\DIFFERENT_DISPLAY"
    with pytest.raises(ValueError, match="changed after review"):
        setup.apply(review["plan_id"])
    review = setup.plan("virtual")
    setup.apply(review["plan_id"])
    setup.test_processes.append({"create_time": 1001})
    assert setup.capture_plan() is not None
    virtual["adapter_hardware_id"] = "root\\changedvdd"
    assert setup.capture_plan() is None
    virtual["adapter_hardware_id"] = "root\\mttvdd"
    virtual["kind"] = "unknown"
    assert setup.capture_plan() is None


def test_unrelated_outside_monitor_can_change_without_disabling_capture(setup):
    add_virtual_below(setup)
    review = setup.plan("virtual")
    setup.apply(review["plan_id"])
    setup.test_processes.append({"create_time": 1001})
    assert setup.capture_plan() is not None
    setup.test_monitors[1].update(x=3000, y=20, adapter_device_id="PCI\\REPLACED")
    assert setup.capture_plan() is not None
    setup.test_monitors.append(dict(id="third", name="Other screen", primary=False,
                                   x=-1000, y=0, width=1000, height=1000, kind="physical"))
    assert setup.capture_plan() is not None
    setup.test_monitors[-1]["x"] = -999
    assert setup.capture_plan() is None


@pytest.mark.parametrize("change", ["attached", "overlap", "detached", "enumeration-failed"])
def test_restore_does_not_require_original_monitor_topology(setup, change):
    applied(setup)
    if change == "attached":
        add_virtual_below(setup)
    elif change == "overlap":
        setup.test_monitors.append(dict(id="new", name="New monitor", primary=False,
                                       x=0, y=0, width=800, height=600, kind="virtual"))
    elif change == "detached":
        setup.test_monitors.pop()
    else:
        from webapp.backend.display_capture import CaptureError
        def failed():
            raise CaptureError("monitor_unavailable", "No enumeration")
        setup.monitors = failed
    assert setup.snapshot()["can_restore"] is True
    assert setup.restore()["ok"]
    assert setup.options_path.read_bytes() == ORIGINAL
    assert not setup.profile_path.exists()


def test_restore_with_changed_topology_still_requires_native_and_owned_file_proof(setup):
    applied(setup)
    add_virtual_below(setup)
    path = setup.install/next(iter(VIEWPORT_FILES.values()))
    path.write_text(path.read_text()+"-- different installed viewport\n")
    before = setup.options_path.read_bytes()
    with pytest.raises(ValueError, match="changed or are not owned"):
        setup.restore()
    assert setup.options_path.read_bytes() == before


def test_old_record_without_new_export_metadata_can_restore(setup):
    applied(setup)
    state_path = setup.runtime/"applied.json"
    state = json.loads(state_path.read_text())
    state.pop("export_kind")
    state.pop("physical_monitors_preserved")
    for monitor in state["proof"]["monitors"]:
        for key in ("kind", "adapter_device_id", "adapter_description", "adapter_hardware_id"):
            monitor.pop(key)
    state_path.write_text(json.dumps(state))
    add_virtual_below(setup)
    assert setup.restore()["ok"]
    assert setup.options_path.read_bytes() == ORIGINAL
