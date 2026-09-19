#!/usr/bin/env python3
r"""
DCS Copilot MCP server.

Exposes the telemetry bridge, cockpit display scraper and bind index as MCP
tools so Claude can interrogate and operate the aircraft conversationally.

DESIGN NOTES
------------
* The server is STATELESS. It never binds the UDP port -- collector.py owns
  that. This server only reads state/state.json, so it starts instantly, works
  whether or not DCS is running, and cannot contend with the collector.
* Every reading carries STALENESS. Handing an LLM 40-second-old altitude as if
  it were live is worse than returning nothing.
* Actions go through input_sender.KeySender, which enforces: DCS must be
  foreground, only combos in the bind index, primary flight controls REFUSED,
  dangerous actions require force, rate limiting, and an audit log.
* Pitch / roll / yaw / throttle-axis remain the pilot's. By instruction.

Run:  python mcp_server.py         (stdio transport)
"""

import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from mcp.server.fastmcp import FastMCP  # noqa: E402
from input_sender import KeySender, foreground_process  # noqa: E402

STATE = os.path.join(HERE, "state", "state.json")
BINDS = os.path.join(HERE, "binds", "binds.json")

mcp = FastMCP(
    "dcs-copilot",
    instructions=(
        "Live F/A-18C telemetry, cockpit display text, and cockpit switch actuation "
        "for DCS World. ALWAYS check dcs_status first -- data may be stale or DCS "
        "may be closed. Primary flight controls (pitch/roll/yaw/throttle axis) are "
        "the pilot's and are refused. Consult the fa18c-navigation skill before "
        "commanding navigation or autopilot actions."
    ),
)

def read_state(retries=6, delay=0.12):
    """Tolerate transient failures while the collector atomically swaps the file."""
    for _ in range(retries):
        try:
            with open(STATE, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            time.sleep(delay)
    return None


def staleness(s):
    st = (s or {}).get("stream") or {}
    age = st.get("data_age_seconds")
    return {
        "live": bool(st.get("live")),
        "data_age_seconds": age,
        "warning": None if st.get("live") else
                   ("NO RECENT DATA (age %s s) -- DCS may be closed, paused, or in a "
                    "menu. Do not report these values as current." % age),
    }


def _need_state():
    s = read_state()
    if s is None:
        return None, {"error": "Cannot read state file. Is collector.py running? "
                               "Start it with: python collector.py"}
    return s, None


def ufc_modes(s):
    """Engaged autopilot modes, read off the UFC. Cues flicker -- see skill."""
    el = (((s or {}).get("cockpit_displays") or {}).get("raw") or {}).get("6") or {}
    labels, cued = {}, set()
    for name, text in el.items():
        if name.startswith("UFC_OptionDisplay"):
            r = name.replace("UFC_OptionDisplay", "")
            if r.isdigit():
                labels[int(r)] = text
        elif name.startswith("UFC_OptionCueing"):
            r = name.replace("UFC_OptionCueing", "")
            if r.isdigit():
                cued.add(int(r))
    return {
        "ap_page_displayed": bool(labels),
        "rows": {str(r): labels[r] for r in sorted(labels)},
        "engaged": sorted(labels[r] for r in cued if r in labels),
        "engaged_rows": sorted(cued),
        "note": ("Cue elements can drop out of a single 1 Hz display packet while a "
                 "mode is still engaged. Do not conclude a mode dropped from one "
                 "reading -- corroborate with flight behaviour."),
    }


# ----------------------------------------------------------------- read tools


@mcp.tool()
def dcs_status() -> dict:
    """Is DCS running, is telemetry live, and what is the autopilot doing?

    ALWAYS call this before trusting any other reading.
    """
    s, err = _need_state()
    if err:
        return err
    st = s.get("stream") or {}
    ac = s.get("aircraft") or {}
    return {
        "stream": staleness(s),
        "exporter_connected": st.get("exporter_connected"),
        "packet_counts": st.get("packet_counts"),
        "collector_uptime_seconds": st.get("collector_uptime_seconds"),
        "aircraft": ac.get("aircraft"),
        "autopilot": ufc_modes(s),
        "dcs_focused": foreground_process().lower() == "dcs.exe",
        "focus_note": ("Keystrokes only reach DCS when it is the foreground window. "
                       "If the pilot is typing in chat, actions will be blocked."),
    }


@mcp.tool()
def dcs_aircraft() -> dict:
    """Ownship state in pilot units: altitude, speeds, attitude, config, stores."""
    s, err = _need_state()
    if err:
        return err
    return {"stream": staleness(s),
            "aircraft": s.get("aircraft") or {},
            "derived": s.get("derived") or {}}


@mcp.tool()
def dcs_fuel() -> dict:
    """Fuel remaining, measured burn rate, and endurance estimate."""
    s, err = _need_state()
    if err:
        return err
    d = s.get("derived") or {}
    return {
        "stream": staleness(s),
        "fuel_lb": d.get("fuel_lb"),
        "fuel_fraction": d.get("fuel_fraction"),
        "burn_lb_per_hr": d.get("fuel_burn_lb_per_hr"),
        "endurance_minutes_to_dry": d.get("endurance_minutes_to_dry"),
        "burn_sample_seconds": d.get("burn_sample_seconds"),
        "capacity_is_approximate": d.get("fuel_capacity_is_approximate"),
        "note": ("Endurance is to DRY TANKS at the current burn, not to bingo. "
                 "Burn is measured over the rolling history window, so it lags "
                 "throttle changes by up to that window."),
    }


@mcp.tool()
def dcs_navigation(max_results: int = 5) -> dict:
    """Position, true bearings and ground-speed ETA from the versioned navigation database.

    Keeps the existing response keys. Unknown theatre returns no foreign-map fields.
    """
    s, err = _need_state()
    if err:
        return err
    from webapp.backend.legacy_navigation import legacy_navigation
    return legacy_navigation(s, max_results)



@mcp.tool()
def dcs_cockpit_displays(display: str = "all") -> dict:
    """Live text scraped from cockpit displays.

    display: "ufc", "left_ddi", "right_ddi", "ampcd", or "all".
    Ids: 2=left DDI, 3=radar page, 4=AMPCD/HSI, 5=DDI, 6=UFC.
    """
    s, err = _need_state()
    if err:
        return err
    cd = s.get("cockpit_displays") or {}
    raw = cd.get("raw") or {}
    name_to_id = {"left_ddi": "2", "radar": "3", "ampcd": "4", "ddi5": "5", "ufc": "6"}
    if display != "all":
        did = name_to_id.get(display.lower())
        if did is None:
            return {"error": "Unknown display %r. Use one of: %s, all"
                             % (display, ", ".join(name_to_id))}
        raw = {did: raw.get(did, {})}
    return {
        "stream": staleness(s),
        "displays_age_seconds": cd.get("age_seconds"),
        "id_map": {"2": "left DDI", "3": "radar page", "4": "AMPCD/HSI",
                   "5": "DDI", "6": "UFC"},
        "displays": raw,
        "ufc_summary": cd.get("ufc"),
    }


@mcp.tool()
def dcs_autopilot() -> dict:
    """Which autopilot modes are engaged, read from the UFC cue characters."""
    s, err = _need_state()
    if err:
        return err
    ac = s.get("aircraft") or {}
    return {
        "stream": staleness(s),
        "modes": ufc_modes(s),
        "flight": {"altitude_msl_ft": ac.get("altitude_msl_ft"),
                   "vertical_speed_fpm": ac.get("vertical_speed_fpm"),
                   "ias_kt": ac.get("ias_kt"),
                   "bank_deg": ac.get("bank_deg"),
                   "heading_deg": ac.get("heading_deg")},
        "corroboration_hint": ("Vertical speed near zero corroborates BALT. Airspeed "
                               "steady through a vertical-speed swing corroborates ATC. "
                               "Trust behaviour over a single cue reading."),
    }


@mcp.tool()
def dcs_capabilities() -> dict:
    """Export capability probe results — what this session actually permits.

    On a multiplayer server, sensor/object export may be denied by server flags.
    """
    s, err = _need_state()
    if err:
        return err
    return {"stream": staleness(s), "capabilities": s.get("capabilities") or {}}


@mcp.tool()
def dcs_history(last_n: int = 20) -> dict:
    """Recent 1 Hz samples — for trends like climb rate, acceleration, turn rate.

    Use this instead of a single reading when judging whether the aircraft is
    STABLE. An oscillating aircraft crosses zero vertical speed twice per cycle
    and looks level at that instant.
    """
    s, err = _need_state()
    if err:
        return err
    h = s.get("history") or {}
    samples = (h.get("samples") or [])[-max(1, min(last_n, 300)):]
    vs = [x.get("vv_fpm") for x in samples if x.get("vv_fpm") is not None]
    return {
        "stream": staleness(s),
        "window_seconds": h.get("window_seconds"),
        "samples": samples,
        "stability": {
            "vs_peak_fpm": round(max(abs(v) for v in vs), 0) if vs else None,
            "is_stable": (max(abs(v) for v in vs) < 700) if vs else None,
            "note": "is_stable false means the aircraft is oscillating; altitude "
                    "hold will refuse to capture.",
        },
    }


@mcp.tool()
def dcs_find_bind(query: str, limit: int = 15) -> dict:
    """Search the bind index for cockpit actions and their key combos."""
    try:
        with open(BINDS, encoding="utf-8") as fh:
            b = json.load(fh)
    except OSError as exc:
        return {"error": "Cannot read bind index: %s" % exc}
    q = query.lower()
    hits = [{"name": a["name"], "combos": a["combos"], "source": a["source"],
             "category": a.get("category")}
            for a in b.get("actions", [])
            if a["combos"] and q in a["name"].lower()]
    return {"query": query, "match_count": len(hits), "matches": hits[:limit],
            "total_bound_actions": b.get("counts", {}).get("actions_bound")}


# --------------------------------------------------------------- action tool


@mcp.tool()
def dcs_press(action: str, dry_run: bool = True, force: bool = False,
              hold_ms: int = 0) -> dict:
    """Press a bound cockpit control by its exact action name.

    SAFETY: DCS must be the foreground window. Only combos in the bind index are
    permitted. Primary flight controls are REFUSED outright. Dangerous actions
    (eject, jettison, engine cutoff...) require force=True. Every press is logged.

    dry_run defaults to True -- call with dry_run=False to actually send.
    hold_ms > 0 holds the key down; needed for momentary SLEW switches such as
    the heading-set switch, which move continuously while held.

    Use dcs_find_bind first to get the exact action name.
    """
    try:
        ks = KeySender(dry_run=dry_run)
    except OSError as exc:
        return {"error": "Cannot load bind index: %s" % exc}
    ok, msg = ks.send_action(action, force=force,
                             hold_ms=hold_ms if hold_ms > 0 else None)
    return {
        "ok": ok,
        "message": msg,
        "dry_run": dry_run,
        "foreground_process": foreground_process(),
        "reminder": ("Verify the effect before assuming it worked -- read the cue "
                     "back with dcs_autopilot, or confirm in telemetry."),
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
