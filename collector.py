#!/usr/bin/env python3
"""
DCS Copilot -- Phase 1 collector.

Owns UDP 127.0.0.1:17781, renders a live human view (like listener.py did), AND
maintains a state file that other processes -- or Claude -- can read at any time.

  python collector.py

Writes  state/state.json  (atomically, ~4 Hz)

The state file carries:
  * latest ownship snapshot, in BOTH raw SI and pilot units (kt / ft / deg)
  * staleness -- how old the data is, so a reader never trusts dead numbers
  * capability verdicts from the last probe
  * a rolling ~5 minute history at 1 Hz, which is what makes trend answers
    (fuel burn, climb rate, endurance) possible rather than instantaneous only

Standard library only. Read-only with respect to DCS.
"""

import argparse
import json
import math
import os
import socket
import sys
import time
import uuid
from collections import deque
from datetime import datetime, timezone

HOST = "127.0.0.1"
PORT = 17781
STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
STATE_FILE = os.path.join(STATE_DIR, "state.json")

HISTORY_WINDOW_S = 300.0     # keep ~5 minutes
HISTORY_SAMPLE_S = 1.0       # downsample to 1 Hz for trends
STATE_WRITE_S = 0.25         # flush state file 4x/sec
PRINT_S = 0.5                # console line rate
STALE_AFTER_S = 3.0          # beyond this, the stream is not "live"

# Approximate INTERNAL fuel capacity, pounds. Used only to turn DCS's 0..1
# fuel fraction into something a pilot can reason about. Approximate on purpose.
AIRFRAME_FUEL_LB = {
    "FA-18C_hornet": 10860.0,
    "F-15C": 13455.0,
    "F-15ESE": 13123.0,
    "F-16C_50": 7162.0,
    "FA-18A": 10860.0,
}
DEFAULT_FUEL_LB = None  # unknown airframe -> report fraction only

RAD2DEG = 57.29577951308232
M2FT = 3.280839895013123
MPS2KT = 1.9438444924406046
MPS2FPM = 196.85039370078738

# LoGetAngleOfAttack returns DEGREES, unlike pitch/bank/heading which are radians.
# Verified empirically 2026-08-15 in an F/A-18C at Mach 0.96 level flight:
#   flight-path angle from velocity  = asin(vv/tas) = asin(1.71/301.8) = 0.325 deg
#   pitch - AOA (treating AOA as deg) = 1.507 - 1.194 = 0.313 deg   <- matches
# Treating AOA as radians would give 68 deg, which is impossible at 1 G.
AOA_FACTOR = 1.0

CATEGORY_ORDER = ["always", "ownship", "sensor", "object", "cockpit"]
VERDICT_NOTE = {
    "AVAILABLE": "returned real data",
    "DENIED": "ran but returned nil while in cockpit -> server flag off",
    "PENDING": "not in a cockpit yet -- inconclusive",
    "ERROR": "call raised an error",
    "MISSING_API": "function absent in this DCS build",
    "UNKNOWN": "no result",
}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def conv(value, factor):
    """Scale a value, tolerating None (a denied or absent field)."""
    if value is None:
        return None
    try:
        v = float(value) * factor
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return round(v, 4)


def fmt(value, spec: str, dash: str = "--") -> str:
    if value is None:
        return dash
    try:
        return format(float(value), spec)
    except (TypeError, ValueError):
        return str(value)


class Collector:
    def __init__(self, state_file: str, raw_log=None, capture=None):
        from webapp.tools.export_capture import ExportCapture
        self.capture = capture or ExportCapture()
        self.state_file = state_file
        self.raw_log = raw_log
        self.latest = None            # last telemetry packet (raw)
        self.latest_epoch = 0.0
        self.capabilities = None      # last probe packet
        self.probe_received_epoch = 0.0
        self.displays = {}            # id -> {element: text} from list_indication
        self.displays_epoch = 0.0
        self.display_received_epoch = {}  # per-id wall clock; aggregate age is not enough
        self.display_identity_by_id = {}
        self.display_session = uuid.uuid4().hex
        self.display_identity = None
        self.display_model_time = None
        self.indication_ids = []      # non-empty ids reported by the survey
        self.history = deque()
        self.last_hist = 0.0
        self.last_write = 0.0
        self.last_print = 0.0
        self.counts = {}
        self.connected = False
        self.session_started = time.time()

    # ---------- ingestion ----------
    def _reset_displays(self, clear_probe=False, clear_history=False):
        """Discard retained labels at exporter/aircraft boundaries."""
        self.displays = {}
        self.displays_epoch = 0.0
        self.display_received_epoch = {}
        self.display_identity_by_id = {}
        self.indication_ids = []
        self.display_session = uuid.uuid4().hex
        self.display_identity = None
        self.display_model_time = None
        self.capture.clear_latest()
        if clear_probe:
            self.capabilities = None
            self.probe_received_epoch = 0.0
        if clear_history:
            self.history.clear()
            self.last_hist = 0.0

    def _probe_matches_new_telemetry(self, pkt, now):
        """Exporter sends its new probe before the new slot's first telemetry."""
        probe = self.capabilities or {}
        model_t, probe_t = pkt.get("t"), probe.get("model_time")
        numeric = lambda value: (isinstance(value, (int, float))
                                 and not isinstance(value, bool) and math.isfinite(value))
        return (probe.get("settled") is True and probe.get("aircraft") == pkt.get("name")
                and self.probe_received_epoch > self.latest_epoch
                and 0 <= now - self.probe_received_epoch <= 1.0
                and numeric(model_t) and numeric(probe_t) and 0 <= model_t - probe_t <= 1.0)

    def on_packet(self, pkt: dict):
        invalid = self.capture.validation_error(pkt)
        if invalid:
            self.capture.reject(invalid)
            return False
        kind = pkt.get("type", "?")
        self.counts[kind] = self.counts.get(kind, 0) + 1
        now = time.time()

        if kind == "telemetry":
            identity = (pkt.get("name"), pkt.get("unit"))
            model_t = pkt.get("t")
            model_reset = (isinstance(model_t, (int, float))
                           and isinstance(self.display_model_time, (int, float))
                           and model_t < self.display_model_time - 0.05)
            if identity != self.display_identity or model_reset:
                # A probe can precede the first ownship packet. Keep that probe,
                # but never retain a previous aircraft/session's permissions.
                retain_probe = self._probe_matches_new_telemetry(pkt, now)
                startup = {}
                if self.display_identity is None and retain_probe:
                    for key in ("hello", "probe", "indication_survey"):
                        item = self.capture.latest.get(key)
                        if (item and item.get("session") == self.display_session
                                and 0 <= now - item["received_epoch"] <= 1.0):
                            startup[key] = item
                boundary = self.display_identity is not None
                self._reset_displays(clear_probe=boundary and not retain_probe, clear_history=boundary)
                if startup:
                    self.capture.associate_startup(startup, self.display_session, identity)
                    survey = startup.get("indication_survey", {}).get("packet", {}).get("ids")
                    if isinstance(survey, list):
                        self.indication_ids = survey
            self.display_identity = identity
            self.display_model_time = model_t
            self.latest = pkt
            self.latest_epoch = now
            self.connected = True
            if now - self.last_hist >= HISTORY_SAMPLE_S:
                self.last_hist = now
                self.history.append(self._history_sample(pkt, now))
                cutoff = now - HISTORY_WINDOW_S
                while self.history and self.history[0]["epoch"] < cutoff:
                    self.history.popleft()
        elif kind == "probe":
            self.capabilities = pkt
            self.probe_received_epoch = now
            self.connected = True
        elif kind == "indications":
            # One packet per display id; merge rather than replace.
            disp_id = str(pkt.get("id", "?"))
            self.displays[disp_id] = pkt.get("elements") or {}
            self.displays_epoch = now
            self.display_received_epoch[disp_id] = now
            identity = self.display_identity
            self.display_identity_by_id[disp_id] = {
                "aircraft": identity[0] if identity else None,
                "unit_name": identity[1] if identity else None,
                "display_session": self.display_session,
                "model_time_s": pkt.get("t"),
            }
            self.connected = True
        elif kind == "indication_survey":
            self.indication_ids = pkt.get("ids") or []
            self.connected = True
        elif kind == "hello":
            self._reset_displays(clear_probe=True, clear_history=True)
            self.latest, self.latest_epoch = None, 0.0
            self.connected = True
        elif kind == "bye":
            self._reset_displays(clear_probe=True, clear_history=True)
            self.latest, self.latest_epoch = None, 0.0
            self.connected = False

        return self.capture.record(pkt, now, self.display_session, self.display_identity)

    def _history_sample(self, pkt: dict, epoch: float) -> dict:
        return {
            "epoch": round(epoch, 2),
            "model_t": pkt.get("t"),
            "alt_ft": conv(pkt.get("alt_msl"), M2FT),
            "agl_ft": conv(pkt.get("alt_agl"), M2FT),
            "ias_kt": conv(pkt.get("ias"), MPS2KT),
            "mach": pkt.get("mach"),
            "hdg_deg": conv(pkt.get("heading"), RAD2DEG),
            "aoa_deg": conv(pkt.get("aoa"), AOA_FACTOR),
            "vv_fpm": conv(pkt.get("vv"), MPS2FPM),
            "fuel_frac": pkt.get("fuel_internal"),
            "lat": pkt.get("lat"),
            "lon": pkt.get("lon"),
        }

    # ---------- derivation ----------
    def _fuel_capacity(self):
        if not self.latest:
            return None
        return AIRFRAME_FUEL_LB.get(self.latest.get("name"), DEFAULT_FUEL_LB)

    def derive(self) -> dict:
        """Trend values. These need history -- that is why we keep it."""
        d = {}
        cap = self._fuel_capacity()
        pkt = self.latest or {}

        frac = pkt.get("fuel_internal")
        if frac is not None:
            d["fuel_fraction"] = round(float(frac), 4)
            if cap:
                d["fuel_lb"] = round(float(frac) * cap, 1)
                d["fuel_capacity_lb"] = cap
                d["fuel_capacity_is_approximate"] = True

        # Burn rate from the oldest usable history sample to the newest.
        usable = [h for h in self.history if h.get("fuel_frac") is not None]
        if len(usable) >= 2 and cap:
            a, b = usable[0], usable[-1]
            dt_hr = (b["epoch"] - a["epoch"]) / 3600.0
            dlb = (a["fuel_frac"] - b["fuel_frac"]) * cap
            if dt_hr > 0 and dlb > 0:     # ignore refuel / no-burn
                burn = dlb / dt_hr
                d["fuel_burn_lb_per_hr"] = round(burn, 0)
                d["burn_sample_seconds"] = round(b["epoch"] - a["epoch"], 1)
                if burn > 1:
                    d["endurance_minutes_to_dry"] = round(
                        (float(frac) * cap) / burn * 60.0, 1)

        # Climb/descent trend, averaged over history to smooth turbulence.
        vvs = [h["vv_fpm"] for h in self.history if h.get("vv_fpm") is not None]
        if vvs:
            d["vv_fpm_avg"] = round(sum(vvs) / len(vvs), 0)
        if pkt.get("vv") is not None:
            d["vv_fpm_now"] = conv(pkt.get("vv"), MPS2FPM)

        alts = [h["alt_ft"] for h in self.history if h.get("alt_ft") is not None]
        if len(alts) >= 2:
            d["alt_change_ft_over_window"] = round(alts[-1] - alts[0], 0)
        return d

    def pilot_units(self) -> dict:
        """Latest snapshot converted into units a pilot actually speaks."""
        p = self.latest
        if not p:
            return {}
        g = p.get("g") or {}
        out = {
            "aircraft": p.get("name"),
            "unit_name": p.get("unit"),
            "coalition": p.get("coalition"),
            "model_time_s": p.get("t"),
            "altitude_msl_ft": conv(p.get("alt_msl"), M2FT),
            "altitude_agl_ft": conv(p.get("alt_agl"), M2FT),
            "ias_kt": conv(p.get("ias"), MPS2KT),
            "tas_kt": conv(p.get("tas"), MPS2KT),
            "mach": p.get("mach"),
            "vertical_speed_fpm": conv(p.get("vv"), MPS2FPM),
            "heading_deg": conv(p.get("heading"), RAD2DEG),
            "magnetic_yaw_deg": conv(p.get("yaw_mag"), RAD2DEG),
            "pitch_deg": conv(p.get("pitch"), RAD2DEG),
            "bank_deg": conv(p.get("bank"), RAD2DEG),
            "aoa_deg": conv(p.get("aoa"), AOA_FACTOR),
            "g_load": g.get("y") if isinstance(g, dict) else None,
            "latitude": p.get("lat"),
            "longitude": p.get("lon"),
            "gear": p.get("gear"),
            "flaps": p.get("flaps"),
            "speedbrake": p.get("brake"),
            "hook": p.get("hook"),
            "wing_fold": p.get("wing"),
            "rpm": p.get("rpm"),
            "cannon_shells": p.get("shells"),
            "station_selected": p.get("station_current"),
            "stations": p.get("stations"),
            "stations_total": p.get("stations_total"),
        }
        if p.get("world_object_count") is not None:
            out["world_object_count"] = p["world_object_count"]
        if p.get("sensor") is not None:
            out["sensor"] = p["sensor"]
        return {k: v for k, v in out.items() if v is not None}

    # ---------- cockpit displays ----------
    def ufc_summary(self):
        """Pull the UFC option-select labels out of whichever display carries them.

        This is what removes button guessing: instead of asking which row is
        BALT, read UFC_OptionDisplay3 and know.
        """
        opts, scratch, source = {}, None, None
        for disp_id, elements in (self.displays or {}).items():
            if not isinstance(elements, dict):
                continue
            for name, text in elements.items():
                low = name.lower()
                if "optiondisplay" in low:
                    digits = "".join(c for c in name if c.isdigit())
                    if digits:
                        opts[digits] = text
                        source = disp_id
                elif "scratchpad" in low and "string1" in low:
                    scratch = text
                    source = source or disp_id
        if not opts and scratch is None:
            return None
        out = {"source_indication_id": source, "options": opts}
        if scratch is not None:
            out["scratchpad"] = scratch
        if opts:
            out["press_hint"] = {
                label: "UFC Option Select Pushbutton %s" % row
                for row, label in sorted(opts.items())
            }
        return out

    # ---------- state file ----------
    def build_state(self) -> dict:
        now = time.time()
        age = (now - self.latest_epoch) if self.latest_epoch else None
        live = bool(age is not None and age <= STALE_AFTER_S)

        verdicts, probe_meta = {}, {}
        if self.capabilities:
            verdicts = self.capabilities.get("verdicts", {}) or {}
            probe_meta = {
                "wallclock": self.capabilities.get("wallclock"),
                "settled": self.capabilities.get("settled"),
                "probe_number": self.capabilities.get("probe_number"),
                "pilot": self.capabilities.get("pilot"),
                "aircraft": self.capabilities.get("aircraft"),
                "model_time": self.capabilities.get("model_time"),
                "received_epoch": self.probe_received_epoch or None,
            }

        gated = {}
        for cat in ("sensor", "object", "cockpit"):
            v = verdicts.get(cat)
            if v and v != "AVAILABLE":
                gated[cat] = {
                    "verdict": v,
                    "explanation": VERDICT_NOTE.get(v, ""),
                    "flag": {"sensor": "allow_sensor_export",
                             "object": "allow_object_export",
                             "cockpit": "(not a documented flag)"}[cat],
                }

        return {
            "schema": "dcs-copilot/state/1",
            "written_utc": now_utc(),
            "written_epoch": round(now, 2),
            "stream": {
                "live": live,
                "data_age_seconds": round(age, 2) if age is not None else None,
                "stale_threshold_seconds": STALE_AFTER_S,
                "note": ("data is current" if live else
                         "NO RECENT DATA -- DCS may be closed, paused, or in a menu. "
                         "Do not report these values as current."),
                "exporter_connected": self.connected,
                "packet_counts": dict(self.counts),
                "collector_uptime_seconds": round(now - self.session_started, 1),
            },
            "capabilities": {
                "verdicts": verdicts,
                "probe": probe_meta,
                "unavailable": gated,
                "note": ("Verdicts are only trustworthy when probe.settled is true."
                         if verdicts else "No probe received yet."),
            },
            "aircraft": self.pilot_units(),
            "derived": self.derive(),
            "cockpit_displays": {
                "age_seconds": (round(now - self.displays_epoch, 2)
                                if self.displays_epoch else None),
                "received_epoch_by_id": dict(self.display_received_epoch),
                "age_seconds_by_id": {
                    key: round(now - epoch, 2)
                    for key, epoch in self.display_received_epoch.items()
                },
                "identity_by_id": dict(self.display_identity_by_id),
                "display_session": self.display_session,
                "survey_ids": self.indication_ids,
                "ufc": self.ufc_summary(),
                "raw": self.displays,
            },
            "history": {
                "window_seconds": HISTORY_WINDOW_S,
                "sample_interval_seconds": HISTORY_SAMPLE_S,
                "count": len(self.history),
                "samples": list(self.history),
            },
            "raw_latest": self.latest,
            "export_capture": self.capture.snapshot(),
        }

    def write_state(self, force: bool = False):
        now = time.time()
        if not force and (now - self.last_write) < STATE_WRITE_S:
            return
        self.last_write = now
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.build_state(), fh, indent=1)
            os.replace(tmp, self.state_file)   # atomic on Windows
        except OSError as exc:
            print("[%s] state write failed: %s" % (ts(), exc), file=sys.stderr)

    # ---------- console ----------
    def print_probe(self, pkt: dict):
        verdicts = pkt.get("verdicts", {}) or {}
        settled = pkt.get("settled")
        print("")
        print("=" * 64)
        print(" EXPORT CAPABILITY PROBE  #%s" % pkt.get("probe_number", "?"))
        print("=" * 64)
        print("  pilot    : %s" % pkt.get("pilot"))
        print("  aircraft : %s" % pkt.get("aircraft"))
        print("  settled  : %s%s" % (settled, "" if settled else
                                     "   <- NOT yet trustworthy (no cockpit)"))
        print("")
        for cat in CATEGORY_ORDER:
            v = verdicts.get(cat, "UNKNOWN")
            print("    %-9s %-12s %s" % (cat, v, VERDICT_NOTE.get(v, "")))
        print("")
        if settled:
            s, c = verdicts.get("sensor"), verdicts.get("cockpit")
            if s == "DENIED" and c == "AVAILABLE":
                print("  >> LOOPHOLE: sensor export DENIED but cockpit device reads")
                print("     AVAILABLE -- display scraping is not gated here.")
            elif s == "DENIED" and c == "DENIED":
                print("  >> No loophole: cockpit reads denied alongside sensor.")
            elif s == "AVAILABLE":
                print("  >> Permissive session: full sensor access.")
        print("=" * 64)

    def print_live(self):
        now = time.time()
        if now - self.last_print < PRINT_S or not self.latest:
            return
        self.last_print = now
        u = self.pilot_units()
        d = self.derive()
        bits = [
            ts(),
            "%-12s" % str(u.get("aircraft", "?"))[:12],
            "IAS %s" % fmt(u.get("ias_kt"), "5.0f"),
            "ALT %s" % fmt(u.get("altitude_msl_ft"), "6.0f"),
            "HDG %s" % fmt(u.get("heading_deg"), "5.1f"),
            "VS %s" % fmt(u.get("vertical_speed_fpm"), "6.0f"),
            "AOA %s" % fmt(u.get("aoa_deg"), "4.1f"),
            "G %s" % fmt(u.get("g_load"), "4.1f"),
        ]
        if d.get("fuel_lb") is not None:
            bits.append("FUEL %s lb" % fmt(d.get("fuel_lb"), "6.0f"))
        elif d.get("fuel_fraction") is not None:
            bits.append("FUEL %s" % fmt(d.get("fuel_fraction"), "5.3f"))
        if d.get("fuel_burn_lb_per_hr"):
            bits.append("BURN %s/hr" % fmt(d.get("fuel_burn_lb_per_hr"), "5.0f"))
        print("  ".join(bits))


def main() -> int:
    ap = argparse.ArgumentParser(description="DCS Copilot collector")
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--state", default=STATE_FILE, help="state file path")
    ap.add_argument("--raw-log", metavar="FILE", help="append raw packets as JSON lines")
    ap.add_argument("--archive-dir", metavar="DIR", help="bounded complete packet archive (default: beside state/exports)")
    ap.add_argument("--quiet", action="store_true", help="no live console output")
    ap.add_argument("--resume-checkpoint", metavar="FILE", help="one-use verified same-session upgrade checkpoint")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((args.host, args.port))
    except OSError as exc:
        print("Could not bind %s:%d -- %s" % (args.host, args.port, exc), file=sys.stderr)
        print("Another collector or listener is probably already running.", file=sys.stderr)
        return 1
    sock.settimeout(0.5)

    raw_fh = open(args.raw_log, "a", encoding="utf-8") if args.raw_log else None
    from webapp.tools.export_capture import ExportCapture
    archive = args.archive_dir or os.path.join(os.path.dirname(os.path.abspath(args.state)), "exports")
    col = Collector(args.state, raw_fh, capture=ExportCapture(archive))
    if args.resume_checkpoint:
        from webapp.tools.collector_checkpoint import restore_checkpoint
        restore_checkpoint(col, args.resume_checkpoint)
    col.write_state(force=True)

    print("DCS Copilot collector listening on %s:%d" % (args.host, args.port))
    print("State file: %s" % args.state)
    print("Waiting for DCS... (Ctrl-C to stop)")

    try:
        while True:
            try:
                data, _ = sock.recvfrom(65535)
            except socket.timeout:
                col.capture.flush_due()  # retain the final burst when DCS pauses
                col.write_state()      # keep staleness fresh even with no traffic
                continue

            text = data.decode("utf-8", errors="replace")
            if raw_fh:
                raw_fh.write(text + "\n")
                raw_fh.flush()
            try:
                pkt = json.loads(text)
            except json.JSONDecodeError:
                col.capture.reject("Invalid JSON datagram; not archived as a valid packet")
                col.write_state()
                continue
            if not isinstance(pkt, dict):
                col.capture.reject("Exporter packet must be a JSON object")
                col.write_state()
                continue

            first = not col.counts
            if not col.on_packet(pkt):
                col.write_state()
                continue
            if first:
                print("[%s] stream is live." % ts())

            kind = pkt.get("type")
            if kind == "probe" and not args.quiet:
                col.print_probe(pkt)
            elif kind == "hello" and not args.quiet:
                print("[%s] exporter connected (%s Hz)" % (ts(), pkt.get("rate_hz")))
            elif kind == "bye" and not args.quiet:
                print("[%s] exporter stopped." % ts())
            elif kind == "telemetry" and not args.quiet:
                col.print_live()

            col.write_state()
    except KeyboardInterrupt:
        pass
    finally:
        col.connected = False
        col.write_state(force=True)
        sock.close()
        col.capture.close()
        if raw_fh:
            raw_fh.close()

    print("\nStopped. Packets: %s"
          % (", ".join("%s=%d" % kv for kv in sorted(col.counts.items())) or "none"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
