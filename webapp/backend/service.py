import json
import logging
import threading
import time
from .config import ROOT
from .telemetry import TelemetryReader
from .command_queue import CommandQueue
from .sender import GuardedSender


class DashboardService:
    def __init__(self, settings):
        from .bindings import BindingService
        from .navigation import NavigationService
        self.settings = settings
        settings.runtime_dir.mkdir(parents=True, exist_ok=True)
        from .setup import SetupManager
        self.setup = SetupManager(settings)
        self.install_root = self.setup.active["install_path"]
        self.saved_games = self.setup.active["profile_path"]
        setup_error = None if self.install_root and self.saved_games else "Open Your setup and select an available DCS installation and Saved Games profile."
        from pathlib import Path
        # Missing setup never falls back to another profile's live bridge or log.
        profile = Path(self.saved_games) if self.saved_games else settings.runtime_dir / "unconfigured-profile"
        self.reader = TelemetryReader(settings.state_path)
        from .export_data import ExportDataReader
        self.export_reader = ExportDataReader(probe_path=profile / "Logs" / "dcs-copilot-probe.json")
        from .live_mission import MissionReader
        self.mission_reader = MissionReader(log_path=profile / "Logs" / "dcs.log", hook_path=profile / "Scripts" / "Hooks" / "dcs-fiddle-server.lua")
        from .awareness import AwarenessReader
        self.awareness_reader = AwarenessReader(enabled=settings.mission_awareness and not setup_error,
                                               hook_path=profile / "Scripts" / "Hooks" / "dcs-fiddle-server.lua")
        binding_options = dict(data_dir=settings.data_dir, install_root=self.install_root,
                               saved_games=self.saved_games, setup_error=setup_error)
        self.bindings = BindingService(**binding_options)
        self.binding_services = {"F-22A": self.bindings,
                                 "FA-18C_hornet": BindingService(**binding_options, aircraft="FA-18C_hornet")}
        self.last_aircraft = "F-22A"
        try:
            saved = json.loads((settings.runtime_dir / "last-aircraft.json").read_text(encoding="utf-8"))
            if saved.get("aircraft") in self.binding_services:
                self.last_aircraft = saved["aircraft"]
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        self.navigation = NavigationService(settings.data_dir / "navigation", tolerant=True)
        self.overlay = None
        self.overlay_error = None
        if settings.mission_path:
            try:
                from .mission import parse_mission
                self.overlay = parse_mission(settings.mission_path)
            except Exception as exc:
                self.overlay_error = f"Mission overlay unavailable: {exc}"
        bind = self.bindings.snapshot("F-22A")
        self.sender = GuardedSender(bind.get("allowlist_path") or settings.data_dir / "bindings" / "f22-allowlist.json",
                                    ROOT / "state" / "input-audit.log", settings.dry_run)
        from .remote import InputCoordinator, RemoteService
        self.input_coordinator = InputCoordinator()
        self.queue = CommandQueue(settings.runtime_dir / "commands.sqlite3", self.context, self.safe_actions,
                                  self.input_coordinator.guide(self.sender), dry_run=settings.dry_run, expiry_s=settings.expiry_s, settle_s=settings.settle_s)
        self.remote = RemoteService(settings.runtime_dir, self.context, self.remote_catalog_source, self.remote_cockpit,
                                    coordinator=self.input_coordinator, guide_busy=self.guide_busy)
        from .mission_scripts import ScriptRegistry
        from .config import APP_ROOT
        self.script_registry = ScriptRegistry(APP_ROOT / "scripts", profile / "Scripts" / "Hooks" / "dcs-fiddle-server.lua",
                                              context_provider=self.context)
        self.remote.script_provider = self.script_registry.public
        self.remote.script_prepare = self.script_registry.prepare
        self.remote.script_execute = self.script_registry.execute
        from .display_export import DisplayExportSetup
        from .display_feed import DisplayFeedService
        self._display_setup_lock = threading.Lock()
        self._display_setup_proof = None
        self.display_feed = DisplayFeedService(
            DisplayExportSetup(settings.runtime_dir, self.install_root, self.saved_games), self.display_context)
        from .smart import SmartMaintenance
        self.smart = SmartMaintenance(self)

    def guide_busy(self):
        result = self.queue.get()
        return bool(result and result.get('state') in ('Selected', 'Queued', 'Awaiting DCS Focus', 'Settling', 'Revalidating', 'Sent', 'Confirming'))

    def remote_catalog_source(self, aircraft):
        bound = self.binding_snapshot(aircraft)
        catalog = self.binding_services.get(aircraft)
        safe = catalog.get_safe_actions(aircraft) if catalog and aircraft == 'F-22A' else []
        return bound, safe

    def remote_cockpit(self):
        from .cockpit import build_cockpit_snapshot
        state = self.reader.read()
        result = build_cockpit_snapshot(state['raw'], state['health'])
        result['remote_session'] = state['context']['session']
        result['received_epoch_by_id'] = (state['raw'].get('cockpit_displays') or {}).get('received_epoch_by_id') or {}
        return result

    def setup_snapshot(self, refresh=False):
        result = self.setup.snapshot(refresh=refresh)
        result["bindings"] = []
        for aircraft in self.binding_services:
            bound = self.binding_snapshot(aircraft)
            result["bindings"].append({"aircraft": aircraft,
                "label": "F/A-18C Hornet" if aircraft == "FA-18C_hornet" else "F-22A Raptor",
                "status": bound.get("status"), "counts": bound.get("counts", {}),
                "reason": bound.get("reason"), "display_only": bound.get("display_only", True)})
        return result

    def binding_snapshot(self, aircraft=None):
        if aircraft is None:
            aircraft = self.reader.read()["aircraft"].get("aircraft") or self.last_aircraft
        catalog = self.binding_services.get(aircraft, self.bindings)
        try:
            result = catalog.snapshot(aircraft)
        except (OSError, ValueError, TypeError) as exc:
            result = {"aircraft": aircraft, "status": "Unavailable", "actions": [], "devices": [],
                      "safe_actions": [], "reason": f"Saved controls could not be read: {exc}"}
        result.setdefault("source_hash", result.get("source_fingerprint"))
        result.setdefault("generated_at", result.get("generated_utc"))
        return result

    def context(self):
        """Command context always obtains a new telemetry and binding proof."""
        state = self.reader.read()
        aircraft = state["context"].get("aircraft")
        catalog = self.binding_services.get(aircraft)
        bound = {"status": "Unsupported", "valid": False}
        if catalog is not None and catalog.aircraft == aircraft:
            try:
                # Command guards need a fresh source proof, not a deep copy of
                # every device/action. check_current still hashes all sources.
                bound = catalog.check_current()
                if not isinstance(bound, dict):
                    raise TypeError("Invalid binding proof")
            except (OSError, ValueError, TypeError):
                bound = {"status": "Unavailable", "valid": False}
        ctx = self._context_from_evidence(state, bound)
        ctx["bindings_valid"] = bool(ctx["bindings_valid"] and bound.get("valid") is True and bound.get("status") == "Current")
        return ctx

    def _context_from_evidence(self, state, bound):
        """Combine this one read-only snapshot's evidence without rescanning it."""
        ctx = dict(state["context"])
        ctx["setup_matches_process"] = False
        if ctx.get("process") and self.install_root and self.saved_games:
            from .discovery import identify_running_setup
            from .bindings import path_identity
            running = identify_running_setup()
            ctx["setup_matches_process"] = bool(
                not running.get("ambiguous") and running.get("process_identity") == ctx["process"]
                and running.get("install_path") and running.get("profile_path")
                and path_identity(running["install_path"]) == path_identity(self.install_root)
                and path_identity(running["profile_path"]) == path_identity(self.saved_games))
        ctx["bindings_hash"] = bound.get("current_fingerprint") or bound.get("source_fingerprint")
        ctx["bindings_valid"] = bool(ctx["setup_matches_process"] and bound.get("source_fingerprint") and bound.get("source_fingerprint") == bound.get("current_fingerprint", bound.get("source_fingerprint")) and bound.get("status") not in ("Invalidated", "Unsupported", "Unavailable", "Error"))
        return ctx

    def display_context(self):
        """Fresh display evidence without acquiring or rebuilding input catalogues.

        A process's executable and launch profile cannot change during its
        lifetime. Cache only an independently verified match for that exact
        PID/creation-time and selected roots; failed discovery is retried.
        Telemetry, focus, aircraft and mission evidence are always read anew.
        Input paths continue using the stricter, uncached binding context above.
        """
        ctx = dict(self.reader.read()['context'])
        ctx['setup_matches_process'] = False
        key = (ctx.get('process'), self.install_root, self.saved_games)
        if not all(key):
            with self._display_setup_lock:
                self._display_setup_proof = None
            return ctx
        now = time.monotonic()
        with self._display_setup_lock:
            proof = self._display_setup_proof
        if proof and proof['key'] == key and (proof['matches'] or now-proof['checked'] < 1):
            ctx['setup_matches_process'] = proof['matches']
            return ctx
        from .discovery import identify_running_setup
        from .bindings import path_identity
        try:
            running = identify_running_setup()
            matches = bool(not running.get('ambiguous') and running.get('process_identity') == key[0]
                and running.get('install_path') and running.get('profile_path')
                and path_identity(running['install_path']) == path_identity(self.install_root)
                and path_identity(running['profile_path']) == path_identity(self.saved_games))
        except (OSError, ValueError, TypeError):
            matches = False
        with self._display_setup_lock:
            self._display_setup_proof = {'key': key, 'matches': matches, 'checked': now}
        ctx['setup_matches_process'] = matches
        return ctx

    def safe_actions(self):
        aircraft = self.reader.read()["context"].get("aircraft")
        catalog = self.binding_services.get(aircraft)
        try:
            return catalog.get_safe_actions(aircraft) if catalog else []
        except (OSError, ValueError, TypeError):
            return []

    def catalog_metadata(self, active, current=None):
        rows = []
        for name, catalog in self.binding_services.items():
            try:
                # The active catalogue was already hashed by binding_snapshot.
                # Other catalogues retain their own independent source check.
                status = current["status"] if current and current.get("aircraft") == name else catalog.check_current()["status"]
            except (OSError, ValueError, TypeError):
                status = "Unavailable"
            rows.append({"aircraft": name, "label": "F/A-18C Hornet" if name == "FA-18C_hornet" else "F-22A Raptor",
                         "status": status, "active": name == active,
                         "execution": "Momentary panel buttons in Remote; Guide is read-only" if name == "FA-18C_hornet" else "Explicit benign navigation actions"})
        return rows

    def remember_aircraft(self, aircraft):
        if aircraft in self.binding_services and aircraft != self.last_aircraft:
            self.last_aircraft = aircraft
            from .bindings import atomic_json
            try:
                atomic_json(self.settings.runtime_dir / "last-aircraft.json", {"aircraft": aircraft})
            except OSError:
                pass  # Display preference persistence cannot interrupt telemetry.

    def snapshot(self, clients=0):
        from .adapters import get_adapter, list_adapters
        telemetry = self.reader.read()
        health = telemetry["health"]
        aircraft = telemetry["aircraft"]
        active_mission = self.mission_reader.snapshot(health, telemetry["raw"])
        awareness = self.awareness_reader.snapshot(health, telemetry["raw"], active_mission)
        if health["telemetry_fresh"] and health["dcs_running"]:
            self.remember_aircraft(aircraft.get("aircraft"))
        binding_aircraft = aircraft.get("aircraft") or self.last_aircraft
        bound = self.binding_snapshot(binding_aircraft or "F-22A")
        # A configured file is only a planning document. Never promote it to the
        # active mission merely because its theatre/aircraft happen to match.
        mission_meta = telemetry["raw"].get("mission") or {}
        matched_overlay = self.overlay if isinstance(mission_meta, dict) and self.overlay and mission_meta.get("mission_identity") == self.overlay.get("mission_identity") else None
        engine = self.navigation
        navigation_was_valid = self.smart.navigation_valid
        nav = engine.snapshot(telemetry["raw"], mission=matched_overlay)
        nav["available_terrains"] = list(engine.caches)
        if not navigation_was_valid or not self.smart.navigation_valid or engine is not self.navigation:
            nav.update(status="Updating navigation data", airfields=[], navaids=[], nearest=[], selected=None,
                       route=[], overlay=None, runway_advisory=None, provenance={}, coverage={})
        if self.overlay:
            nav["planned_mission"] = {"title": self.overlay.get("title"), "terrain": self.overlay["terrain"],
                                      "status": "Session matched" if matched_overlay else "Planning only — active mission identity unavailable",
                                      "source_path": self.overlay["source_path"], "mission_identity": self.overlay["mission_identity"]}
        # A stopped collector file must never create a live ownship symbol.
        if not health["telemetry_fresh"] or not health["dcs_running"]:
            nav["ownship"] = None
            nav["nearest"] = []
            nav["live"] = False
        else:
            nav["live"] = True
        health["terrain"] = nav.get("terrain")
        health["binding_status"] = bound.get("status")
        health["binding_aircraft"] = binding_aircraft
        if self.overlay_error:
            health["notes"].append(self.overlay_error)
        if nav.get("ownship"):
            aircraft.update(ground_speed_kt=nav["ownship"].get("ground_speed_kt"), ground_track_deg=nav["ownship"].get("track_true_deg"))
        ctx = self._context_from_evidence(telemetry, bound)
        guide_actions = []
        # Display the safe actions in the very same verified binding generation.
        # Queue/start/send paths still call safe_actions()/context() afresh.
        safe = bound.get("safe_actions", []) if (binding_aircraft == "F-22A"
            and ctx.get("aircraft") == "F-22A" and not bound.get("display_only")
            and bound.get("status") == "Current" and bound.get("source_fingerprint")
            and bound.get("source_fingerprint") == bound.get("current_fingerprint")) else []
        for action in safe:
            reason = CommandQueue.validation(action, ctx)
            guide_actions.append({**action, "title": action["name"], "purpose": "Select an aircraft navigation reference.",
                                  "binding": action.get("combo"), "location": "Resolved F-22 keyboard control",
                                  "expected_result": action.get("expected_result", "Navigation selection changes; verify in the cockpit."),
                                  "observable": False, "enabled": reason is None, "reason": reason,
                                  "current_observed_state": "Navigation-mode state is not runtime verified."})
        adapter_options = dict(install_root=self.install_root, saved_games=self.saved_games)
        adapters = list_adapters(**adapter_options) if self.install_root and self.saved_games else []
        # Resolving one adapter used to rebuild/stat the entire list six times.
        adapter = next((item for item in adapters if binding_aircraft in item['identifiers']), None)
        if adapter is None:
            adapter = get_adapter(binding_aircraft, setup_error="No supported active adapter") if adapters else {"status": "Unavailable", "command_enabled": False}
        binding_catalogs = self.catalog_metadata(binding_aircraft, bound)
        from .sensors import build_sensor_snapshot
        sensors = build_sensor_snapshot(telemetry["raw"], health)
        from .cockpit import build_cockpit_snapshot
        cockpit = build_cockpit_snapshot(telemetry["raw"], health)
        export_data = self.export_reader.snapshot(telemetry["raw"], health)
        remote = self.remote.status(include_catalog=False, ctx=ctx)
        return {"server": {"dry_run": self.settings.dry_run, "lan_enabled": self.settings.lan_enabled,
                           "remote_armed": remote['armed'], "remote_mode": remote['mode'],
                           "connected_clients": clients, "version": "1.0.0", "validation": "Not yet flight-tested"},
                "health": health, "aircraft": aircraft, "bindings": bound, "binding_catalogs": binding_catalogs,
                "sensors": sensors, "cockpit": cockpit, "mission": active_mission,
                "awareness": awareness,
                "export_data": export_data, "adapters": adapters,
                "adapter": adapter, "navigation": nav, "guide": {"actions": guide_actions, "mode": "Observe", "sequence_available": False},
                "smart": self.smart.snapshot(health, bound),
                "queue": self.queue.get(), "remote": remote}

    def tick(self):
        self.queue.tick()
        self.remote.tick()
        self.smart.tick()

    def navigation_library(self, terrain):
        engine = self.navigation
        if not self.smart.navigation_valid:
            raise ValueError("Navigation sources changed. The library is updating automatically; please wait.")
        result = engine.library(terrain)
        if not self.smart.navigation_valid or engine is not self.navigation:
            raise ValueError("Navigation data changed while loading. Please retry the library.")
        return result

    def close(self):
        self.display_feed.close()
        self.awareness_reader.close()
        self.mission_reader.close()
        self.smart.close()
        self.remote.close()
        self.queue.close()
