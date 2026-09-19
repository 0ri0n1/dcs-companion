"""Local setup selection. Discovery reads DCS; selection only writes app runtime."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import threading
import time


class SetupManager:
    def __init__(self, settings, *, discover=None, clock=time.monotonic):
        from .discovery import discover_dcs
        self.discover = discover or discover_dcs
        self.settings = settings
        self.path = settings.runtime_dir / "setup-selection.json"
        self.clock = clock
        self.lock = threading.RLock()
        self.last_scan = float("-inf")
        self.result = None
        self.config_error = None
        self.overrides = {}
        try:
            if self.path.is_file():
                if self.path.stat().st_size > 8192:
                    raise ValueError("Saved setup is too large")
                value = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(value, dict) or value.get("schema") != 1:
                    raise ValueError("Saved setup format is invalid")
                for key in ("install_path", "profile_path"):
                    if not isinstance(value.get(key), str) or not value[key]:
                        raise ValueError("Saved setup paths are invalid")
                self.overrides = value
        except (OSError, ValueError, TypeError) as exc:
            self.config_error = f"Saved companion setup could not be read: {exc}. Choose your setup again."
        # A complete explicit pair supersedes persisted selection, including a
        # damaged file. Partial overrides still require a usable remaining path.
        if settings.install_root and settings.saved_games:
            self.config_error = None
        self.active = {"install_path": None, "profile_path": None}
        initial = self.snapshot(refresh=True)
        self.active = {key: initial.get("selected", {}).get(key) for key in self.active}

    def snapshot(self, refresh=False):
        with self.lock:
            if self.result is None or refresh or self.clock() - self.last_scan >= 10:
                install = self.settings.install_root or self.overrides.get("install_path")
                profile = self.settings.saved_games or self.overrides.get("profile_path")
                self.result = self.discover(dcs_root=install, saved_games=profile)
                self.last_scan = self.clock()
            result = copy.deepcopy(self.result)
            if self.config_error:
                result.setdefault("issues", []).insert(0, self.config_error)
                result["selected"] = {"install_path": None, "profile_path": None,
                                      "install_id": None, "profile_id": None}
            selected = result.get("selected", {})
            result["status"] = "Ready" if selected.get("install_path") and selected.get("profile_path") else "Needs attention"
            result["active"] = dict(self.active)
            result["pending_restart"] = any(selected.get(k) != v for k, v in self.active.items())
            result["assignment_policy"] = "read-only"
            result["selection_locked"] = bool(self.settings.install_root or self.settings.saved_games)
            return result

    def select(self, install_id, profile_id):
        with self.lock:
            if self.settings.install_root or self.settings.saved_games:
                raise ValueError("Setup paths are set by launch options. Change those options on the DCS computer.")
            current = self.snapshot(refresh=True)
            install = next((row for row in current.get("install_candidates", []) if row["id"] == install_id), None)
            profile = next((row for row in current.get("profile_candidates", []) if row["id"] == profile_id), None)
            if not install or not profile or install.get("valid") is False or profile.get("valid") is False:
                raise ValueError("That installation or profile is no longer available. Refresh the detected setup.")
            # Revalidate selected paths immediately before saving, without executing Lua.
            candidate = self.discover(dcs_root=install["path"], saved_games=profile["path"])
            selected = candidate.get("selected", {})
            if not selected.get("install_path") or not selected.get("profile_path"):
                raise ValueError("Selected DCS paths are unavailable. Refresh and choose again.")
            value = {"schema": 1, "install_path": selected["install_path"], "profile_path": selected["profile_path"]}
            from .bindings import atomic_json
            atomic_json(self.path, value)
            self.overrides = value
            self.config_error = None
            self.result = candidate
            self.last_scan = self.clock()
            return self.snapshot()
