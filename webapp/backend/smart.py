"""Automatic local cache upkeep and plain-language readiness. Never sends input."""
from contextlib import contextmanager
import copy
import json
import logging
from pathlib import Path
import tempfile
import threading
import time
from .bindings import BindingService, atomic_json, fingerprint


@contextmanager
def generation_lock(runtime_dir):
    """Serialize automatic and manual cache publishers across Windows processes."""
    directory = Path(runtime_dir)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "cache-maintenance.lock").open("a+b") as handle:
        handle.seek(0)
        try:
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except ImportError:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("A data refresh is already running; automatic retry will follow.") from exc
        yield


def refresh_bindings_safely(live, runtime_dir, stopped=None, factory=BindingService):
    """Resolve in isolation, then publish only a still-current generation."""
    stopped = stopped or threading.Event()
    runtime_dir = Path(runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="binding-refresh-", dir=runtime_dir) as directory:
        candidate = factory(data_dir=Path(directory), install_root=live.install_root, saved_games=live.saved_games,
                            aircraft=getattr(live, "aircraft", "F-22A"))
        if hasattr(candidate, "inherit_device_history"):
            candidate.inherit_device_history(live)
        candidate.refresh()
        result = copy.deepcopy(candidate._snapshot)
        allowlist = json.loads(candidate.allowlist_path.read_text(encoding="utf-8"))
        expected = result["source_fingerprint"]
        if not candidate.check_current()["valid"]:
            raise RuntimeError("Bindings changed during refresh; waiting for them to settle.")
        if stopped.is_set():
            return False
        # No queue lock is taken while the binding lock is held. Only short
        # fingerprint/publication work occurs here; all Lua ran in staging.
        with live._lock:
            if fingerprint(live.current_sources()) != expected:
                raise RuntimeError("Bindings changed before publication; old controls remain blocked.")
            if stopped.is_set():
                return False
            atomic_json(live.allowlist_path, allowlist)
            atomic_json(live.cache_path, result)
            live._snapshot = result
        return True


def autostart_status(runtime_dir, now=None):
    now = time.time() if now is None else now
    try:
        path = Path(runtime_dir) / "autostart-status.json"
        if path.stat().st_size > 16384:
            raise ValueError("Oversized status")
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        age = now - float(data["updated_epoch"])
        if not 0 <= age <= 15:
            return {"enabled": False, "state": "Unavailable", "detail": "Automatic-start helper heartbeat is unavailable."}
        # Explicit selection prevents any future status-file secrets becoming API fields.
        return {key: data.get(key) for key in ("enabled", "state", "last_error", "next_retry_epoch", "dashboard_url")}
    except (OSError, ValueError, TypeError, KeyError):
        return {"enabled": False, "state": "Not enabled"}


def readiness(health, bindings, nav_status, binding_status, *, automatic=False):
    """One actionable explanation. Missing facts do not become operational claims."""
    if nav_status.get("status") == "Unavailable":
        return "attention", "Choose your DCS setup", nav_status.get("detail", "Open Your setup to select your DCS installation.")
    if binding_status.get("status") == "Unavailable":
        return "attention", "Controls unavailable", binding_status.get("detail", "Aircraft defaults are unavailable in the selected setup.")
    if binding_status.get("status") == "Error":
        return "attention", "Controls need attention", binding_status.get("detail", "Automatic refresh will retry; commands remain blocked.")
    if binding_status.get("status") in ("Refreshing", "Waiting") or bindings.get("status") == "Invalidated":
        return "waiting", "Updating your controls", "Your saved DCS bindings changed. The dashboard is rebuilding them automatically; queued actions are cancelled."
    if nav_status.get("status") in ("Refreshing", "Waiting"):
        return "waiting", "Updating navigation data", "Installed terrain data changed. Local libraries are rebuilding automatically."
    if nav_status.get("status") == "Error":
        return "attention", "Navigation data needs attention", nav_status.get("detail", "Automatic refresh will retry.")
    if not health.get("dcs_running"):
        return "waiting", "Ready for DCS", "Start DCS normally. The dashboard will connect when an aircraft becomes available." if automatic else "Start DCS to connect your aircraft. Static controls and airfield libraries are ready."
    if health.get("status") == "DCS menus/no aircraft":
        return "waiting", "DCS detected — choose an aircraft", "The connection is ready. Enter a cockpit and live navigation will appear automatically."
    if not health.get("telemetry_fresh"):
        return "waiting", "Waiting for fresh aircraft data", "DCS may be paused or loading. The dashboard reconnects automatically; commands stay blocked until data advances."
    if health.get("status") == "Limited export":
        return "attention", "DCS is limiting exported data", "Available navigation remains usable. Export restrictions are respected."
    if bindings.get("status") == "Unsupported":
        return "ready", "Navigation connected", "This aircraft can use the shared scope. Its cockpit controls have not been verified, so they remain unavailable."
    if not health.get("displays_fresh"):
        return "ready", "Aircraft connected", "Ownship data is current. Cockpit displays are unavailable or stale; display-dependent actions remain blocked."
    if bindings.get("aircraft") == "FA-18C_hornet":
        return "ready", "Hornet connected", "Your Hornet controls and available cockpit readouts are loaded. Open Remote for touch panels and saved workflows."
    return "ready", "Aircraft connected", "Navigation and controls are current. Observe mode stays the default; each command still needs your confirmation."


class SmartMaintenance:
    def __init__(self, service, *, clock=time.monotonic, navigation=None):
        self.service, self.clock = service, clock
        self.runtime_dir = service.settings.runtime_dir
        if navigation is None:
            from .navigation_maintenance import NavigationMaintenance
            navigation = NavigationMaintenance(service.navigation, service.bindings.install_root,
                                               service.navigation.cache_dir, self.runtime_dir)
        self.navigation = navigation
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.thread = None
        self.last_check = float("-inf")
        self.pending = {}
        catalogs = getattr(service, "binding_services", {"F-22A": service.bindings})
        self.binding_jobs = {"bindings" if name == "F-22A" else "bindings:" + name: catalog
                             for name, catalog in catalogs.items()}
        self.retry_at = {kind: 0 for kind in (*self.binding_jobs, "navigation")}
        self.reports = {"bindings": {"status": "Current", "detail": "Watching for saved binding changes."},
                        "navigation": {"status": "Waiting", "detail": "Installed navigation sources are being checked."}}
        self.navigation_valid = False
        self.navigation_generation = 0
        for kind in self.binding_jobs:
            self.reports.setdefault(kind, {"status": "Current", "detail": "Watching for saved binding changes."})

    def _report(self, kind, status, detail):
        with self.lock:
            self.reports[kind] = {"status": status, "detail": detail}

    def tick(self):
        now = self.clock()
        if self.stopped.is_set() or now - self.last_check < 3:
            return
        self.last_check = now
        checks = {}
        for kind, catalog in self.binding_jobs.items():
            try:
                check = catalog.check_current()
                if check.get("rebuild_eligible") is False:
                    checks[kind] = {**check, "fingerprint": check.get("current_fingerprint")}
                    continue
                checks[kind] = {"valid": check["valid"], "fingerprint": check["current_fingerprint"]}
            except (OSError, ValueError) as exc:
                checks[kind] = {"valid": False, "fingerprint": "unreadable-bindings"}
                self._report(kind, "Error", f"Saved controls could not be read: {exc}. Retrying automatically.")
        try:
            checks["navigation"] = self.navigation.check()
        except (OSError, ValueError) as exc:
            checks["navigation"] = {"valid": False, "fingerprint": "unreadable-navigation"}
            self._report("navigation", "Error", f"Navigation sources could not be read: {exc}. Retrying automatically.")
        self.navigation_valid = bool(checks["navigation"]["valid"])
        busy = self.thread is not None and self.thread.is_alive()
        for kind, check in checks.items():
            if check.get("rebuild_eligible") is False:
                self.pending.pop(kind, None)
                self._report(kind, "Unavailable", check.get("reason") or check.get("detail") or "This aircraft's defaults are not installed.")
                continue
            if check["valid"]:
                self.pending.pop(kind, None)
                if not busy:
                    self._report(kind, "Current", "Current — saved source changes are watched automatically.")
                continue
            revision = check.get("fingerprint") or check.get("source_fingerprint") or "unknown"
            prior = self.pending.get(kind)
            if prior is None or prior[0] != revision:
                self.pending[kind] = (revision, now)
                self.retry_at[kind] = 0
                if kind in self.binding_jobs:
                    self.service.queue.invalidate("Bindings changed; automatic refresh cancelled the pending command.")
                self._report(kind, "Waiting", "Source changes detected. Waiting briefly for file writes to settle.")
                continue
            if busy or now - prior[1] < 2 or now < self.retry_at[kind]:
                continue
            self._report(kind, "Refreshing", "Rebuilding local data in the background. DCS files are read-only.")
            self.thread = threading.Thread(target=self._refresh, args=(kind,), name="DCS cache refresh", daemon=True)
            self.thread.start()
            busy = True

    def _refresh(self, kind):
        try:
            with generation_lock(self.runtime_dir):
                if kind in self.binding_jobs:
                    refreshed = refresh_bindings_safely(self.binding_jobs[kind], self.runtime_dir, self.stopped)
                else:
                    replacement = self.navigation.refresh()
                    refreshed = not self.stopped.is_set()
                    if refreshed:
                        self.service.navigation = replacement
                        self.navigation.service = replacement
                        self.navigation_generation += 1
                        self.navigation_valid = True
                if refreshed:
                    self._report(kind, "Current", "Automatically updated from the current installed sources.")
        except Exception as exc:
            logging.exception("Automatic %s refresh failed; commands are not retried", kind)
            self.retry_at[kind] = self.clock() + 60
            self._report(kind, "Error", f"{exc} Automatic refresh will retry in one minute; unverified data stays unavailable.")

    def snapshot(self, health, bindings):
        with self.lock:
            aircraft = bindings.get("aircraft", "F-22A")
            key = "bindings" if aircraft == "F-22A" else "bindings:" + aircraft
            binding_status = dict(self.reports.get(key, {"status": "Unsupported", "detail": "No installed binding adapter."}))
            nav_status = dict(self.reports["navigation"])
            nav_status["generation"] = self.navigation_generation
        startup = autostart_status(self.runtime_dir)
        status, title, detail = readiness(health, bindings, nav_status, binding_status, automatic=bool(startup.get("enabled")))
        return {"status": status, "title": title, "detail": detail, "automatic": True,
                "binding_refresh": binding_status, "navigation_refresh": nav_status, "autostart": startup}

    def close(self):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=3)
