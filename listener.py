#!/usr/bin/env python3
"""
DCS Copilot -- Phase 1 UDP telemetry listener.

Receives the JSON datagrams emitted by Saved Games\\DCS\\Scripts\\Export.lua,
prints a live one-line status, renders the capability probe when it arrives,
and optionally appends everything to a .jsonl file.

Standard library only. Read-only: this never talks back to DCS.

  python listener.py                 # live view
  python listener.py --log run.jsonl # also record raw packets
  python listener.py --raw           # dump every packet verbatim
"""

import argparse
import json
import socket
import sys
import time
from datetime import datetime

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 17781

CATEGORY_ORDER = ["always", "ownship", "sensor", "object", "cockpit"]

# Verdict -> (symbol, meaning). Kept ASCII-safe for the Windows console.
VERDICT_NOTE = {
    "AVAILABLE": "returned real data",
    "DENIED": "ran but returned nil while in cockpit -> server flag off",
    "PENDING": "not in a cockpit yet -- inconclusive",
    "ERROR": "call raised an error",
    "MISSING_API": "function absent in this DCS build",
    "UNKNOWN": "no result",
}


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def fmt(value, spec: str, dash: str = "--") -> str:
    """Format a number, tolerating None so a denied field prints as '--'."""
    if value is None:
        return dash
    try:
        return format(float(value), spec)
    except (TypeError, ValueError):
        return str(value)


def render_probe(pkt: dict) -> str:
    verdicts = pkt.get("verdicts", {}) or {}
    settled = pkt.get("settled")
    lines = []
    lines.append("")
    lines.append("=" * 64)
    lines.append(" EXPORT CAPABILITY PROBE  #%s" % pkt.get("probe_number", "?"))
    lines.append("=" * 64)
    lines.append("  wallclock : %s" % pkt.get("wallclock"))
    lines.append("  pilot     : %s" % pkt.get("pilot"))
    lines.append("  aircraft  : %s" % pkt.get("aircraft"))
    lines.append("  settled   : %s%s" % (
        settled,
        "" if settled else "   <- verdicts NOT yet trustworthy (no cockpit)",
    ))
    lines.append("")
    for cat in CATEGORY_ORDER:
        v = verdicts.get(cat, "UNKNOWN")
        lines.append("    %-9s %-12s %s" % (cat, v, VERDICT_NOTE.get(v, "")))
    lines.append("")
    if settled:
        sensor = verdicts.get("sensor")
        cockpit = verdicts.get("cockpit")
        if sensor == "DENIED" and cockpit == "AVAILABLE":
            lines.append("  >> NOTE: sensor export DENIED but cockpit device reads")
            lines.append("     AVAILABLE. That is the loophole -- cockpit/display")
            lines.append("     scraping is not gated by allow_sensor_export here.")
        elif sensor == "DENIED" and cockpit == "DENIED":
            lines.append("  >> NOTE: cockpit reads are denied alongside sensor export.")
            lines.append("     No loophole on this server.")
    lines.append("  Full detail: Saved Games\\DCS\\Logs\\dcs-copilot-probe.txt")
    lines.append("=" * 64)
    return "\n".join(lines)


def render_telemetry(pkt: dict) -> str:
    name = pkt.get("name") or pkt.get("unit") or "?"
    parts = [
        "%s" % ts(),
        "%-12s" % str(name)[:12],
        "IAS %s" % fmt(mps_to_kt(pkt.get("ias")), "6.1f"),
        "ALT %s" % fmt(m_to_ft(pkt.get("alt_msl")), "7.0f"),
        "AGL %s" % fmt(m_to_ft(pkt.get("alt_agl")), "7.0f"),
        "HDG %s" % fmt(rad_to_deg(pkt.get("heading")), "5.1f"),
        "AOA %s" % fmt(rad_to_deg(pkt.get("aoa")), "5.1f"),
        "VV %s" % fmt(pkt.get("vv"), "6.1f"),
        "M %s" % fmt(pkt.get("mach"), "4.2f"),
        "FUEL %s" % fmt(pkt.get("fuel_internal"), "5.3f"),
    ]
    g = pkt.get("g") or {}
    if isinstance(g, dict) and g.get("y") is not None:
        parts.append("G %s" % fmt(g.get("y"), "4.1f"))
    if pkt.get("world_object_count") is not None:
        parts.append("OBJ %s" % pkt["world_object_count"])
    sensor = pkt.get("sensor") or {}
    if isinstance(sensor, dict) and sensor.get("tws_count") is not None:
        parts.append("TWS %s" % sensor["tws_count"])
    return "  ".join(parts)


def rad_to_deg(v):
    if v is None:
        return None
    return float(v) * 57.2957795130823


def m_to_ft(v):
    if v is None:
        return None
    return float(v) * 3.280839895


def mps_to_kt(v):
    if v is None:
        return None
    return float(v) * 1.9438444924406


def main() -> int:
    ap = argparse.ArgumentParser(description="DCS Copilot UDP telemetry listener")
    ap.add_argument("--host", default=DEFAULT_HOST,
                    help="bind address (default %s)" % DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help="bind port (default %d)" % DEFAULT_PORT)
    ap.add_argument("--log", metavar="FILE",
                    help="append every packet to FILE as JSON lines")
    ap.add_argument("--raw", action="store_true",
                    help="print each packet verbatim instead of a formatted line")
    ap.add_argument("--hz", type=float, default=2.0,
                    help="max telemetry lines printed per second (default 2)")
    args = ap.parse_args()

    # Without this, Python block-buffers when stdout is a file or a pipe and the
    # live view appears frozen. Harmless when attached to a console.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((args.host, args.port))
    except OSError as exc:
        print("Could not bind %s:%d -- %s" % (args.host, args.port, exc),
              file=sys.stderr)
        print("Another listener may already be running.", file=sys.stderr)
        return 1
    sock.settimeout(1.0)

    logfh = open(args.log, "a", encoding="utf-8") if args.log else None

    print("Listening on %s:%d  (Ctrl-C to stop)" % (args.host, args.port))
    print("Waiting for DCS... start a mission with the exporter installed.")

    counts = {}
    min_interval = 1.0 / args.hz if args.hz > 0 else 0.0
    last_print = 0.0
    last_seen = None

    try:
        while True:
            try:
                data, _addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except KeyboardInterrupt:
                break

            text = data.decode("utf-8", errors="replace")
            if logfh:
                logfh.write(text + "\n")
                logfh.flush()

            try:
                pkt = json.loads(text)
            except json.JSONDecodeError as exc:
                print("[%s] malformed packet (%s): %s" % (ts(), exc, text[:200]))
                continue

            kind = pkt.get("type", "?")
            counts[kind] = counts.get(kind, 0) + 1

            if last_seen is None:
                print("[%s] stream is live." % ts())
            last_seen = time.time()

            if args.raw:
                print(json.dumps(pkt, sort_keys=True))
                continue

            if kind == "telemetry":
                now = time.time()
                if now - last_print >= min_interval:
                    last_print = now
                    print(render_telemetry(pkt))
            elif kind == "probe":
                print(render_probe(pkt))
            elif kind == "hello":
                print("[%s] exporter connected: rate=%s Hz  (%s)"
                      % (ts(), pkt.get("rate_hz"), pkt.get("wallclock")))
            elif kind == "bye":
                print("[%s] exporter stopped. sent=%s errors=%s"
                      % (ts(), pkt.get("sent"), pkt.get("errors")))
            elif kind == "oversize":
                print("[%s] exporter dropped an oversize %s packet (%s bytes)"
                      % (ts(), pkt.get("kind"), pkt.get("dropped_bytes")))
            else:
                print("[%s] %s" % (ts(), json.dumps(pkt)[:300]))
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
        if logfh:
            logfh.close()

    print("\nPackets received: %s"
          % (", ".join("%s=%d" % kv for kv in sorted(counts.items())) or "none"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
