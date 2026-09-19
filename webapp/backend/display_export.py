"""Reviewed, reversible DCS monitor setup. Never evaluates configuration Lua.

Only plan() writes while DCS is running, and only to Companion runtime storage.
apply()/restore() require DCS completely closed. Native Hornet viewports are
verified from the selected install; no installation files are ever written.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from functools import lru_cache
from pathlib import Path
import re
import stat
import threading
import time
import uuid

from .lua_data import LuaDataParser, _TOKEN, parse_assignment

PROFILE = "DCSCompanionDisplays"
MAX_FILE = 2 * 1024 * 1024
FIELDS = ("multiMonitorSetup", "width", "height", "aspect", "fullScreen")
MONITOR_FIELDS = ("id", "name", "primary", "x", "y", "width", "height", "kind",
                  "adapter_device_id", "adapter_description", "adapter_hardware_id")
VIEWPORT_FILES = {
    "LEFT_MFCD": "Mods/aircraft/FA-18C/Cockpit/Scripts/Multipurpose_Display_Group/MDI_IP1556A/indicator/MDI_left_viewport_cfg.lua",
    "RIGHT_MFCD": "Mods/aircraft/FA-18C/Cockpit/Scripts/Multipurpose_Display_Group/MDI_IP1556A/indicator/MDI_right_viewport_cfg.lua",
    "CENTER_MFCD": "Mods/aircraft/FA-18C/Cockpit/Scripts/Multipurpose_Display_Group/AMPCD/indicator/AMPCD_viewport_cfg.lua",
}


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _json(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _confined(root, path):
    root, path = Path(root).absolute(), Path(path).absolute()
    if not path.is_relative_to(root):
        raise ValueError("Display setup path is outside its selected directory.")
    for item in (path, *path.parents):
        try:
            # One fresh metadata read checks both conditions. The former
            # is_symlink/is_junction pair performed two native reads per ancestor.
            metadata = item.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(metadata.st_mode) or
                getattr(metadata, "st_reparse_tag", 0) == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)):
            raise ValueError("Display setup cannot use symbolic links or junctions.")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Display setup path escapes its selected directory.")
    return path


def _read(root, path):
    path = _confined(root, path)
    with path.open("rb") as handle:
        data = handle.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise ValueError("Display setup file exceeds its size limit.")
    return data


def _atomic_write(path, data):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _processes():
    import psutil
    found = []
    for process in psutil.process_iter(["name", "pid"]):
        try:
            if str(process.info.get("name", "")).casefold() == "dcs.exe":
                found.append({**process.info, "create_time": process.create_time()})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            # Inability to prove a process closed must not permit writes.
            found.append({"name": "DCS.exe", "create_time": None})
    return found


def _monitors():
    from .display_capture import enumerate_monitors
    return enumerate_monitors()


def _table_fields(parser, start):
    parser.i = start
    parser.take("{")
    fields = {}
    while parser.peek() != "}":
        if parser.peek() == "[":
            parser.take("[")
            key = parser.value()
            parser.take("]")
            parser.take("=")
        elif parser.peek(1) == "=":
            key = parser.take()[1]
            parser.take("=")
        else:
            raise ValueError("Options tables must have explicit field names.")
        begin = parser.i
        parser.value()
        fields[key] = (begin, parser.i)
        if parser.peek() in (",", ";"):
            parser.take()
    parser.take("}")
    return fields


@lru_cache(maxsize=8)
def _validated_options(data):
    """Cache parsing by exact bytes, never by mtime; every read remains fresh."""
    text = data.decode("utf-8-sig", errors="strict")
    value = parse_assignment(text, "options")
    graphics = value.get("graphics")
    if not isinstance(graphics, dict) or any(k not in graphics for k in FIELDS):
        raise ValueError("Graphics options must contain all five display setup fields.")
    if not isinstance(graphics["multiMonitorSetup"], str) or type(graphics["fullScreen"]) is not bool:
        raise ValueError("Graphics display fields have unsupported types.")
    for field in ("width", "height", "aspect"):
        number = graphics[field]
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number <= 0:
            raise ValueError("Graphics dimensions are invalid.")
    vr = value.get("VR", {})
    if not isinstance(vr, dict):
        raise ValueError("VR options have an unsupported structure.")
    if vr.get("enable", False):
        raise ValueError("This display export setup requires DCS VR disabled.")
    return tuple(graphics[key] for key in FIELDS)


def _options(data, replacements=None):
    """Validate the entire literal assignment; replace only five scalar spans."""
    values = _validated_options(data)
    if replacements is None:
        return dict(zip(FIELDS, values))
    text = data.decode("utf-8-sig", errors="strict")
    parser = LuaDataParser(text)
    outer = _table_fields(parser, 2)
    inner = _table_fields(parser, outer["graphics"][0])
    spans = [(m.start(), m.end()) for m in _TOKEN.finditer(text)
             if not m.group().isspace() and not m.group().startswith("--")]
    edits = []
    for field in FIELDS:
        begin, end = inner[field]
        replacement = replacements[field]
        literal = ("true" if replacement else "false") if isinstance(replacement, bool) else (
            json.dumps(replacement) if isinstance(replacement, str) else format(replacement, ".14g"))
        edits.append((spans[begin][0], spans[end-1][1], literal))
    for begin, end, literal in sorted(edits, reverse=True):
        text = text[:begin] + literal + text[end:]
    encoded = text.encode("utf-8")
    return (b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b"") + encoded


def _topology(monitors, selected):
    if not isinstance(monitors, list) or not 2 <= len(monitors) <= 32:
        raise ValueError("Display export needs a primary monitor and a separate export display.")
    clean = []
    for item in monitors:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ValueError("Monitor identity is unavailable.")
        if any(type(item.get(k)) is not int for k in ("x", "y", "width", "height")):
            raise ValueError("Monitor geometry is unavailable.")
        if not (1 <= item["width"] <= 16384 and 1 <= item["height"] <= 16384):
            raise ValueError("Monitor dimensions are unsupported.")
        if any(abs(item[k]) > 1_000_000 for k in ("x", "y")):
            raise ValueError("Monitor position is unsupported.")
        kind = item.get("kind", "unknown")
        if kind not in ("virtual", "physical", "unknown"):
            raise ValueError("Monitor classification is unavailable.")
        entry = {k: item.get(k) for k in MONITOR_FIELDS}
        entry["kind"] = kind
        clean.append(entry)
    clean.sort(key=lambda item: item["id"])
    if len({m["id"] for m in clean}) != len(clean) or sum(m["primary"] is True for m in clean) != 1:
        raise ValueError("One unambiguous primary monitor is required.")
    primary = next(m for m in clean if m["primary"] is True)
    secondary = next((m for m in clean if m["id"] == selected and m["primary"] is not True), None)
    if secondary is None:
        raise ValueError("Choose a non-primary display for exports.")
    a, b = primary, secondary
    if any(m[k] < 512 for m in (a, b) for k in ("width", "height")):
        raise ValueError("The main and export displays must each be at least 512 pixels wide and high.")
    horizontal = (a["y"] == b["y"] and a["height"] == b["height"] and
                  (a["x"] + a["width"] == b["x"] or b["x"] + b["width"] == a["x"]))
    vertical = (a["x"] == b["x"] and a["width"] == b["width"] and
                (a["y"] + a["height"] == b["y"] or b["y"] + b["height"] == a["y"]))
    if not horizontal and not vertical:
        raise ValueError("Monitors must share a full aligned edge; gaps, overlap and staggered layouts are unsupported.")
    x, y = min(a["x"], b["x"]), min(a["y"], b["y"])
    window = {"x": x, "y": y, "width": max(a["x"]+a["width"], b["x"]+b["width"])-x,
              "height": max(a["y"]+a["height"], b["y"]+b["height"])-y}
    if window["width"] > 16384 or window["height"] > 16384:
        raise ValueError("Combined monitor dimensions exceed the supported render size.")
    for other in clean:
        if other["id"] in (a["id"], b["id"]):
            continue
        if (other["x"] < window["x"]+window["width"] and other["x"]+other["width"] > window["x"] and
                other["y"] < window["y"]+window["height"] and other["y"]+other["height"] > window["y"]):
            raise ValueError("Another monitor overlaps the DCS render area; move the export display so other screens remain outside it.")
    if b["width"] >= 1536:
        offsets, size = [(0, 0), (512, 0), (1024, 0)], (1536, 512)
    elif b["height"] >= 1536:
        offsets, size = [(0, 0), (0, 512), (0, 1024)], (512, 1536)
    elif b["width"] >= 1024 and b["height"] >= 1024:
        offsets, size = [(0, 0), (512, 0), (0, 512)], (1024, 1024)
    else:
        raise ValueError("The export display cannot hold three 512-pixel display exports.")
    origin = b["x"] + (b["width"]-size[0])//2, b["y"] + (b["height"]-size[1])//2
    panels = {name: {"x": origin[0]+dx, "y": origin[1]+dy, "width": 512, "height": 512}
              for name, (dx, dy) in zip(("left_mdi", "right_mdi", "ampcd"), offsets)}
    return clean, primary, secondary, window, panels


def _profile(primary, window, panels):
    def rectangle(r):
        return f'x = {r["x"]-window["x"]}; y = {r["y"]-window["y"]}; width = {r["width"]}; height = {r["height"]};'
    text = ('-- Managed by DCS Companion. Restore through Display setup.\n'
            'name = "DCS Companion displays"\nDescription = "Hornet displays on a dedicated secondary monitor"\n'
            'Viewports = { Center = { ' + rectangle(primary) +
            f' viewDx = 0; viewDy = 0; aspect = {primary["width"]/primary["height"]:.14g}; }} }}\n'
            'GU_MAIN_VIEWPORT = Viewports.Center\nUIMainView = Viewports.Center\n')
    for label, panel in zip(VIEWPORT_FILES, ("left_mdi", "right_mdi", "ampcd")):
        text += label + " = { " + rectangle(panels[panel]) + " }\n"
    return text.encode("utf-8")


class DisplayExportSetup:
    """Dependencies: monitors()->list; processes()->DCS-only dicts with create_time.

    An optional process name is filtered when provided. Unknown process start
    time prevents capture. writer(Path, bytes) must atomically replace a file.
    """
    def __init__(self, runtime_dir, install_root, saved_games, *, monitors=None,
                 processes=None, writer=None, clock=None):
        self.runtime = Path(runtime_dir).absolute() / "display-export"
        self.install = Path(install_root).absolute() if install_root else None
        self.saved_games = Path(saved_games).absolute() if saved_games else None
        self.monitors = monitors or _monitors
        self.processes = processes or _processes
        self.writer = writer or _atomic_write
        self.clock = clock or time.time
        self.lock = threading.RLock()

    @property
    def options_path(self):
        if self.saved_games is None:
            raise ValueError("Select a validated DCS Saved Games profile first.")
        return _confined(self.saved_games, self.saved_games / "Config/options.lua")

    @property
    def profile_path(self):
        return _confined(self.saved_games, self.saved_games / f"Config/MonitorSetup/{PROFILE}.lua")

    def _running(self):
        rows = self.processes()
        if not isinstance(rows, list):
            raise ValueError("DCS process state could not be verified.")
        return [p for p in rows if not p.get("name") or str(p["name"]).casefold() == "dcs.exe"]

    def _closed(self):
        if self._running():
            raise ValueError("Close DCS completely before applying or restoring display setup.")

    def _monitor_inventory(self):
        # Capture's native enumeration uses a typed RuntimeError. Convert only
        # that expected failure at this boundary so setup stays fail-closed.
        from .display_capture import CaptureError
        try:
            return self.monitors()
        except CaptureError:
            raise ValueError("Monitor enumeration is unavailable; reconnect displays and retry.") from None

    def _runtime_write(self, relative, data):
        path = _confined(self.runtime, self.runtime / relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.writer(path, data)

    def _load(self, name):
        try:
            return json.loads(_read(self.runtime, self.runtime/name))
        except FileNotFoundError:
            return None

    def _state(self):
        state = self._load("applied.json")
        if state is not None and (not isinstance(state, dict) or state.get("schema") != 1):
            raise ValueError("Display setup record is not recognized.")
        return state

    def _files_proof(self):
        if self.install is None or self.saved_games is None:
            raise ValueError("Select a validated DCS installation and Saved Games profile first.")
        # _read validates confinement immediately before opening. Avoid the
        # identical scan in the public property followed by another in _read.
        options = _read(self.saved_games, self.saved_games / "Config/options.lua")
        current = _options(options)
        if current["multiMonitorSetup"].casefold() not in ("1camera", PROFILE.casefold()):
            raise ValueError("A custom monitor profile is already selected; restore the one-screen setup first.")
        native = {}
        for label, relative in VIEWPORT_FILES.items():
            content = _read(self.install, self.install/relative)
            text = content.decode("utf-8-sig")
            if not re.search(r'try_find_assigned_viewport\s*\(\s*[\"\']'+label+r'[\"\']\s*\)', text):
                raise ValueError("Installed Hornet viewport support could not be verified.")
            native[label] = _hash(content)
        proof = {"options_hash": _hash(options), "install": str(self.install), "saved_games": str(self.saved_games),
                 "native": native}
        return proof, options, current

    def _proof(self, monitor_id):
        proof, options, current = self._files_proof()
        monitors, primary, secondary, window, panels = _topology(self._monitor_inventory(), monitor_id)
        proof.update(monitors=monitors, monitor_id=monitor_id)
        return proof, options, current, primary, secondary, window, panels

    def _owned_files(self, state, proof=None, current=None):
        """Restore ownership is independent of attached monitor topology."""
        if not state or state.get("status") != "applied":
            return False
        if proof is None:
            proof, _, current = self._files_proof()
        # DCS rewrites unrelated options and formatting. Those changes must not
        # disable capture or be overwritten on restore. Owned fields remain strict.
        before = {k: state["proof"][k] for k in ("install", "saved_games", "native")}
        now = {k: proof[k] for k in ("install", "saved_games", "native")}
        expected = state["changes"]
        # DCS's Options UI stores lowercased filename stems, independently of
        # the profile's display name. Windows resolves either spelling equally.
        equal = all(current[k].casefold() == expected[k].casefold() if k == "multiMonitorSetup" else
                    math.isclose(current[k], expected[k], rel_tol=1e-12, abs_tol=1e-12) if k == "aspect" else
                    current[k] == expected[k] for k in FIELDS)
        return (now == before and equal and
                _hash(_read(self.saved_games, self.saved_games / f"Config/MonitorSetup/{PROFILE}.lua")) == state["profile_hash"])

    def _matching(self, state):
        if not state or state.get("status") != "applied":
            return False
        proof, _, current, _, _, window, panels = self._proof(state["monitor_id"])
        # Unrelated monitors may be attached or moved outside this canvas. The
        # topology validator still checks every monitor for overlap each read.
        def selected_inventory(rows):
            return [m for m in rows if m.get("primary") is True or m["id"] == state["monitor_id"]]
        return (self._owned_files(state, proof, current) and
                selected_inventory(proof["monitors"]) == selected_inventory(state["proof"]["monitors"]) and
                window == state["window"] and panels == state["panels"])

    def _active(self, state):
        rows = self._running()
        return (len(rows) == 1 and isinstance(rows[0].get("create_time"), (int, float)) and
                not isinstance(rows[0].get("create_time"), bool) and
                math.isfinite(rows[0]["create_time"]) and rows[0]["create_time"] > state["applied_epoch"])

    def snapshot(self):
        with self.lock:
            result = {"status": "not-configured", "aircraft": "FA-18C_hornet", "restart_required": True,
                      "secondary_monitor_consumed": True, "monitors": [], "can_apply": False,
                      "can_restore": False, "can_plan": False, "profile_name": PROFILE,
                      "export_kind": "unknown", "physical_monitors_preserved": False,
                      "detail": "Use a verified virtual export display to keep other physical screens free, or explicitly dedicate a secondary monitor."}
            try:
                result["dcs_running"] = bool(self._running())
                state = self._state()
                owned = self._owned_files(state) if state else False
                result["can_restore"] = owned and not result["dcs_running"]
                monitors = self._monitor_inventory()
                result["monitors"] = [{**{k: m.get(k) for k in MONITOR_FIELDS}, "kind": m.get("kind", "unknown")} for m in monitors]
                if state:
                    try:
                        matches = self._matching(state)
                    except (ValueError, OSError):
                        matches = False
                    active = matches and self._active(state)
                    result.update(status="active" if active else "pending-restart" if matches else "changed",
                                  restart_required=not active, monitor_id=state.get("monitor_id"),
                                  window=state.get("window"), panels=state.get("panels"),
                                  export_kind=state.get("export_kind", "unknown"),
                                  physical_monitors_preserved=bool(matches and state.get("physical_monitors_preserved")))
                    result["detail"] = ("Display configuration is ready; live capture also requires a matching DCS window." if active else
                                        "Close and restart DCS to load the configured display exports." if matches else
                                        "The display layout changed. Capture is disabled; original display settings can be restored with DCS closed." if owned else
                                        "Owned display files changed. Capture and automatic restore are disabled.")
                else:
                    eligible = []
                    for m in monitors:
                        if m.get("primary"):
                            continue
                        try:
                            self._proof(m["id"])
                            if self.profile_path.exists():
                                raise ValueError("An unowned Companion monitor profile already exists.")
                            eligible.append(m["id"])
                        except (ValueError, OSError) as exc:
                            result["detail"] = str(exc) if isinstance(exc, ValueError) else "Display setup files could not be read."
                    result["eligible_monitor_ids"] = eligible
                    virtual = [m["id"] for m in monitors if m["id"] in eligible and m.get("kind") == "virtual"]
                    result["recommended_monitor_id"] = virtual[0] if virtual else None
                    if virtual:
                        result["detail"] = "A verified virtual export display is available; other physical monitors stay free."
                    elif eligible:
                        result["detail"] = "No verified virtual export display is ready. A physical or unclassified secondary screen can be explicitly reserved for exports."
                    result["can_plan"] = bool(eligible)
                    result["can_apply"] = bool(eligible) and not result["dcs_running"]
                    if not eligible:
                        result["status"] = "unavailable"
                return result
            except (ValueError, OSError, KeyError, TypeError) as exc:
                result.update(status="unavailable", detail=str(exc) if isinstance(exc, ValueError) else "Display setup could not be verified.")
                return result

    def plan(self, monitor_id):
        with self.lock:
            if not isinstance(monitor_id, str) or len(monitor_id) > 256:
                raise ValueError("Choose an available secondary monitor.")
            if self._state():
                raise ValueError("Restore the existing Companion display setup before making another plan.")
            proof, original, current, primary, secondary, window, panels = self._proof(monitor_id)
            if current["multiMonitorSetup"].casefold() != "1camera" or self.profile_path.exists():
                raise ValueError("An existing Companion monitor profile is not owned by this setup.")
            changes = {"multiMonitorSetup": PROFILE, "width": window["width"], "height": window["height"],
                       "aspect": window["width"]/window["height"], "fullScreen": False}
            # Validate edits now, before presenting a review.
            edited = _options(original, changes)
            profile = _profile(primary, window, panels)
            plan_id = str(uuid.uuid4())
            record = {"schema": 1, "plan_id": plan_id, "proof": proof, "monitor_id": monitor_id,
                      "created_epoch": self.clock(), "window": window, "panels": panels, "changes": changes,
                      "options_hash": _hash(edited), "profile_hash": _hash(profile),
                      "export_kind": secondary["kind"], "physical_monitors_preserved": secondary["kind"] == "virtual"}
            self._runtime_write(f"plans/{plan_id}.json", _json(record))
            return {"plan_id": plan_id, "status": "review", "monitor_id": monitor_id,
                    "monitor_name": secondary["name"], "restart_required": True,
                    "export_kind": secondary["kind"], "physical_monitors_preserved": secondary["kind"] == "virtual",
                    "secondary_monitor_consumed": True, "window": window, "panels": panels,
                    "changes": [{"field": key, "before": current[key], "after": changes[key]} for key in FIELDS],
                    "files": ["Config/options.lua", f"Config/MonitorSetup/{PROFILE}.lua"],
                    "detail": ("The cockpit stays on the primary monitor. Three 512-pixel exports use the verified virtual display; other physical monitors stay free. Close DCS before applying."
                               if secondary["kind"] == "virtual" else
                               "The cockpit stays on the primary monitor. The selected secondary screen is reserved for three 512-pixel display exports. Close DCS before applying.")}

    def apply(self, plan_id):
        with self.lock:
            self._closed()
            try:
                normalized = str(uuid.UUID(plan_id))
            except (TypeError, ValueError, AttributeError):
                raise ValueError("Invalid display setup plan.") from None
            if normalized != plan_id:
                raise ValueError("Invalid display setup plan.")
            if self._state():
                raise ValueError("A Companion display setup is already recorded.")
            record = self._load(f"plans/{plan_id}.json")
            if not record or record.get("schema") != 1:
                raise ValueError("Display setup plan is unavailable. Create a new plan.")
            proof, original, current, primary, secondary, window, panels = self._proof(record["monitor_id"])
            if proof != record["proof"] or self.profile_path.exists() or current["multiMonitorSetup"].casefold() != "1camera":
                raise ValueError("Display setup changed after review. Create a new plan.")
            changes = {"multiMonitorSetup": PROFILE, "width": window["width"], "height": window["height"],
                       "aspect": window["width"]/window["height"], "fullScreen": False}
            options, profile = _options(original, changes), _profile(primary, window, panels)
            if _hash(options) != record["options_hash"] or _hash(profile) != record["profile_hash"]:
                raise ValueError("Display plan content changed. Create a new plan.")
            self._runtime_write(f"backups/{plan_id}/options.lua", original)
            state = {**record, "status": "applying", "original_hash": _hash(original), "applied_epoch": self.clock()}
            self._runtime_write("applied.json", _json(state))
            wrote_profile = wrote_options = False
            try:
                # A second guard immediately before any Saved Games mutation.
                self._closed()
                if _read(self.saved_games, self.options_path) != original or self.profile_path.exists():
                    raise ValueError("DCS options changed before display setup could be applied.")
                self.profile_path.parent.mkdir(parents=True, exist_ok=True)
                self.writer(self.profile_path, profile)
                wrote_profile = True
                self._closed()
                self.writer(self.options_path, options)
                wrote_options = True
                proof, *_ = self._proof(record["monitor_id"])
                state.update(status="applied", proof=proof)
                self._runtime_write("applied.json", _json(state))
            except BaseException:
                # Roll back only exact bytes this transaction wrote; never erase another edit.
                rollback_ok = True
                try:
                    if wrote_options:
                        if _read(self.saved_games, self.options_path) != options:
                            raise ValueError("Options changed during rollback.")
                        self.writer(self.options_path, original)
                    if wrote_profile:
                        if _read(self.saved_games, self.profile_path) != profile:
                            raise ValueError("Monitor profile changed during rollback.")
                        self.profile_path.unlink()
                except (OSError, ValueError):
                    rollback_ok = False
                if rollback_ok:
                    _confined(self.runtime, self.runtime/"applied.json").unlink(missing_ok=True)
                raise
            return {"ok": True, "status": "pending-restart", "restart_required": True,
                    "detail": "Display setup applied and original options backed up. Start DCS to load the exports."}

    def restore(self):
        with self.lock:
            self._closed()
            state = self._state()
            if not state or not self._owned_files(state):
                raise ValueError("Display files changed or are not owned by this setup; automatic restore is unavailable.")
            backup = _read(self.runtime, self.runtime/f'backups/{state["plan_id"]}/options.lua')
            if _hash(backup) != state["original_hash"]:
                raise ValueError("The original display options backup cannot be verified.")
            options = _read(self.saved_games, self.options_path)
            restored = backup if _hash(options) == state["options_hash"] else _options(options, _options(backup))
            profile = _read(self.saved_games, self.profile_path)
            self._closed()
            restored_options = removed_profile = False
            try:
                self.writer(self.options_path, restored)
                restored_options = True
                self.profile_path.unlink()
                removed_profile = True
                _confined(self.runtime, self.runtime/"applied.json").unlink()
            except BaseException:
                if restored_options and _read(self.saved_games, self.options_path) == restored:
                    self.writer(self.options_path, options)
                if removed_profile and not self.profile_path.exists():
                    self.writer(self.profile_path, profile)
                raise
            return {"ok": True, "status": "restored", "restart_required": True,
                    "detail": "Original display settings restored; unrelated options were preserved and the Companion monitor profile was removed."}

    def capture_plan(self):
        with self.lock:
            try:
                state = self._state()
                if not self._matching(state) or not self._active(state):
                    return None
                return {"revision": _hash(_json(state)), "aircraft": "FA-18C_hornet",
                        # _matching already freshly read/confined this exact path.
                        "applied_epoch": state["applied_epoch"],
                        "profile_path": str(self.saved_games / f"Config/MonitorSetup/{PROFILE}.lua"),
                        "window": state["window"], "panels": state["panels"]}
            except (ValueError, OSError, KeyError, TypeError):
                return None
