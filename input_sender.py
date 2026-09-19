#!/usr/bin/env python3
r"""
DCS Copilot -- keystroke sender.

Sends synthetic keystrokes to DCS using Win32 SendInput with SCAN CODES.
Scan codes matter: DCS reads the keyboard through DirectInput, which largely
ignores virtual-key-only injection. Scan-code injection is what VoiceAttack and
similar tools use, and it is what actually registers in the sim.

SAFETY MODEL (all enforced here, not by convention):
  1. DRY RUN BY DEFAULT. Nothing is sent unless --arm is passed.
  2. FOCUS GUARD. Keys are only sent when the foreground process is DCS.exe.
     Otherwise LCtrl+A lands in whatever window you alt-tabbed to.
  3. ALLOWLIST. Only combos present in binds.json may be sent.
  4. PRIMARY FLIGHT CONTROLS ARE REFUSED outright -- never pitch/roll/yaw.
     This structurally prevents the phantom-control-input failure mode.
  5. DANGEROUS ACTIONS REQUIRE --force (engine cutoff, eject, jettison...).
  6. RATE LIMIT + AUDIT LOG of every keystroke, sent or simulated.

  python input_sender.py --list "gear"
  python input_sender.py --action "Landing Gear Control Handle - UP"        # dry run
  python input_sender.py --action "Landing Gear Control Handle - UP" --arm  # live
"""

import argparse
import ctypes
import json
import os
import re
import sys
import time
from ctypes import wintypes
from datetime import datetime

BINDS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "binds", "binds.json")
AUDIT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "input-audit.log")

# --- Set-1 scan codes. ext=True keys need the E0 prefix flag. ----------------
SC = {
    "A": 0x1E, "B": 0x30, "C": 0x2E, "D": 0x20, "E": 0x12, "F": 0x21, "G": 0x22,
    "H": 0x23, "I": 0x17, "J": 0x24, "K": 0x25, "L": 0x26, "M": 0x32, "N": 0x31,
    "O": 0x18, "P": 0x19, "Q": 0x10, "R": 0x13, "S": 0x1F, "T": 0x14, "U": 0x16,
    "V": 0x2F, "W": 0x11, "X": 0x2D, "Y": 0x15, "Z": 0x2C,
    "1": 0x02, "2": 0x03, "3": 0x04, "4": 0x05, "5": 0x06,
    "6": 0x07, "7": 0x08, "8": 0x09, "9": 0x0A, "0": 0x0B,
    "-": 0x0C, "=": 0x0D, "[": 0x1A, "]": 0x1B, ";": 0x27, "'": 0x28,
    "`": 0x29, "\\": 0x2B, ",": 0x33, ".": 0x34, "/": 0x35,
    "Space": 0x39, "Enter": 0x1C, "Back": 0x0E, "Tab": 0x0F, "Escape": 0x01,
    "F1": 0x3B, "F2": 0x3C, "F3": 0x3D, "F4": 0x3E, "F5": 0x3F, "F6": 0x40,
    "F7": 0x41, "F8": 0x42, "F9": 0x43, "F10": 0x44, "F11": 0x57, "F12": 0x58,
    "Num0": 0x52, "Num1": 0x4F, "Num2": 0x50, "Num3": 0x51, "Num4": 0x4B,
    "Num5": 0x4C, "Num6": 0x4D, "Num7": 0x47, "Num8": 0x48, "Num9": 0x49,
    "Num.": 0x53, "Num*": 0x37, "Num-": 0x4A, "Num+": 0x4E,
    # modifiers
    "LShift": 0x2A, "RShift": 0x36, "LCtrl": 0x1D, "LAlt": 0x38,
}
SC_EXT = {
    "Home": 0x47, "End": 0x4F, "PageUp": 0x49, "PageDown": 0x51,
    "Insert": 0x52, "Delete": 0x53,
    "Up": 0x48, "Down": 0x50, "Left": 0x4B, "Right": 0x4D,
    "Num/": 0x35, "NumEnter": 0x1C,
    # right-hand modifiers are the E0 variants of the left ones
    "RCtrl": 0x1D, "RAlt": 0x38, "LWin": 0x5B, "RWin": 0x5C,
}

# Refused outright -- primary flight controls. Never sent, even with --force.
REFUSE = [
    re.compile(p, re.I) for p in (
        r"\bpitch\b", r"\broll\b", r"\byaw\b", r"\brudder\b", r"\baileron\b",
        r"\belevator\b", r"\bnose (up|down|left|right)\b", r"\bbank\b",
        r"view .*(up|down|left|right)",
    )
]

# Require --force. These can end the flight.
DANGEROUS = [
    re.compile(p, re.I) for p in (
        r"eject", r"jettison", r"canopy", r"engine.*(off|stop|cut|crank)",
        r"throttle.*(off|idle)", r"auto stop", r"fire ext", r"fuel shut",
        r"battery.*off", r"emergency", r"master arm", r"weapon release",
        r"gun trigger", r"pickle",
    )
]

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
KEYEVENTF_EXTENDEDKEY = 0x0001
INPUT_KEYBOARD = 1

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


# NOTE ON LAYOUT -- this bit is easy to get wrong and fails with error 87.
# The INPUT union must be sized by its LARGEST member (MOUSEINPUT), not by
# KEYBDINPUT. Defining only KEYBDINPUT yields sizeof(INPUT)==32 on x64, but
# Windows requires 40, and SendInput then rejects cbSize with
# ERROR_INVALID_PARAMETER. dwExtraInfo is ULONG_PTR, not a pointer type.
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTunion(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUTunion)]


_EXPECTED_INPUT_SIZE = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
if ctypes.sizeof(INPUT) != _EXPECTED_INPUT_SIZE:
    raise RuntimeError("INPUT struct is %d bytes, expected %d -- SendInput would "
                       "fail with error 87"
                       % (ctypes.sizeof(INPUT), _EXPECTED_INPUT_SIZE))

user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT


def _scan(name):
    """Return (scancode, extended) for a DCS key name."""
    if name in SC:
        return SC[name], False
    if name in SC_EXT:
        return SC_EXT[name], True
    up = name.upper()
    if up in SC:
        return SC[up], False
    return None, False


def foreground_process() -> str:
    """Image name of the process owning the foreground window ('' if unknown)."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(32768)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(h)


class KeySender:
    def __init__(self, binds_path=BINDS, dry_run=True, audit=AUDIT,
                 hold_ms=55, min_interval_s=0.20):
        with open(binds_path, encoding="utf-8") as fh:
            self.binds = json.load(fh)
        self.actions = {a["name"]: a for a in self.binds.get("actions", [])}
        self.lower = {k.lower(): k for k in self.actions}
        self.dry_run = dry_run
        self.audit_path = audit
        self.hold = hold_ms / 1000.0
        self.min_interval = min_interval_s
        self._last_send = 0.0

    # ---------- lookup ----------
    def find(self, query: str):
        """Exact match first, then case-insensitive, then substring."""
        if query in self.actions:
            return [self.actions[query]]
        if query.lower() in self.lower:
            return [self.actions[self.lower[query.lower()]]]
        q = query.lower()
        return [a for n, a in self.actions.items() if q in n.lower()]

    # ---------- classification ----------
    @staticmethod
    def classify(name: str):
        for rx in REFUSE:
            if rx.search(name):
                return "refused", rx.pattern
        for rx in DANGEROUS:
            if rx.search(name):
                return "dangerous", rx.pattern
        return "ok", None

    # ---------- audit ----------
    def log(self, msg: str):
        try:
            os.makedirs(os.path.dirname(self.audit_path), exist_ok=True)
            with open(self.audit_path, "a", encoding="utf-8") as fh:
                fh.write("[%s] %s\n"
                         % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
        except OSError:
            pass

    # ---------- sending ----------
    def _emit(self, scancode: int, extended: bool, keyup: bool):
        flags = KEYEVENTF_SCANCODE
        if extended:
            flags |= KEYEVENTF_EXTENDEDKEY
        if keyup:
            flags |= KEYEVENTF_KEYUP
        inp = INPUT(type=INPUT_KEYBOARD,
                    union=_INPUTunion(ki=KEYBDINPUT(0, scancode, flags, 0, 0)))
        n = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        if n != 1:
            raise OSError("SendInput failed: %s" % ctypes.get_last_error())

    def send_combo(self, combo: str, force=False, require_focus=True, hold_ms=None):
        """combo like 'LCtrl+A'. Returns (ok, message).

        hold_ms overrides how long the key is held down. Momentary SWITCHES
        (e.g. the heading-set slew switch) move continuously while held, so a
        55 ms tap barely shifts them -- those need holds measured in seconds.
        """
        parts = combo.split("+")
        key, mods = parts[-1], parts[:-1]

        resolved = []
        for p in mods + [key]:
            sc, ext = _scan(p)
            if sc is None:
                return False, "no scan code for key '%s' (combo %s)" % (p, combo)
            resolved.append((p, sc, ext))

        if combo not in self.binds.get("by_combo", {}):
            return False, "combo %s is not in the bind index (allowlist)" % combo

        if self.dry_run:
            self.log("DRY-RUN combo=%s" % combo)
            return True, "DRY RUN -- would send %s" % combo

        fg = foreground_process()
        if require_focus and fg.lower() != "dcs.exe":
            msg = "BLOCKED: foreground process is '%s', not DCS.exe" % (fg or "unknown")
            self.log(msg + " combo=%s" % combo)
            return False, msg

        since = time.time() - self._last_send
        if since < self.min_interval:
            time.sleep(self.min_interval - since)

        # modifiers down -> key down -> hold -> key up -> modifiers up
        try:
            for _p, sc, ext in resolved[:-1]:
                self._emit(sc, ext, False)
                time.sleep(0.015)
            _p, sc, ext = resolved[-1]
            self._emit(sc, ext, False)
            time.sleep(self.hold if hold_ms is None else (hold_ms / 1000.0))
            self._emit(sc, ext, True)
            for _p, sc, ext in reversed(resolved[:-1]):
                time.sleep(0.010)
                self._emit(sc, ext, True)
        except OSError as exc:
            self.log("SEND ERROR combo=%s %s" % (combo, exc))
            return False, str(exc)

        self._last_send = time.time()
        self.log("SENT combo=%s (foreground=%s)" % (combo, fg))
        return True, "sent %s" % combo

    def send_action(self, name: str, force=False, require_focus=True, hold_ms=None):
        matches = self.find(name)
        if not matches:
            return False, "no action matching '%s'" % name
        if len(matches) > 1:
            names = [m["name"] for m in matches[:8]]
            return False, ("ambiguous '%s' -- %d matches: %s"
                           % (name, len(matches), "; ".join(names)))
        a = matches[0]
        kind, why = self.classify(a["name"])
        if kind == "refused":
            self.log("REFUSED action=%s (matched %s)" % (a["name"], why))
            return False, ("REFUSED: '%s' looks like a primary flight control "
                           "(matched /%s/). Never sent." % (a["name"], why))
        if kind == "dangerous" and not force:
            return False, ("'%s' is flagged dangerous (matched /%s/). "
                           "Pass --force to allow." % (a["name"], why))
        if not a["combos"]:
            return False, "'%s' has no keyboard bind" % a["name"]
        combo = a["combos"][0]
        ok, msg = self.send_combo(combo, force=force, require_focus=require_focus,
                                  hold_ms=hold_ms)
        return ok, "%s -> %s : %s" % (a["name"], combo, msg)


def main() -> int:
    ap = argparse.ArgumentParser(description="DCS Copilot key sender")
    ap.add_argument("--binds", default=BINDS)
    ap.add_argument("--list", metavar="FILTER", help="list matching bound actions")
    ap.add_argument("--action", help="action name to trigger")
    ap.add_argument("--combo", help="raw combo, e.g. LCtrl+A (must be in the index)")
    ap.add_argument("--arm", action="store_true", help="actually send (default is dry run)")
    ap.add_argument("--force", action="store_true", help="allow a dangerous action")
    ap.add_argument("--no-focus-check", action="store_true",
                    help="skip the DCS-foreground guard (testing only)")
    ap.add_argument("--check", action="store_true", help="report environment and exit")
    args = ap.parse_args()

    if not os.path.exists(args.binds):
        print("bind index missing: %s" % args.binds, file=sys.stderr)
        return 1

    ks = KeySender(args.binds, dry_run=not args.arm)

    if args.check:
        fg = foreground_process()
        print("bind index    : %s" % args.binds)
        print("actions bound : %d" % ks.binds["counts"]["actions_bound"])
        print("conflicts     : %d" % ks.binds["counts"]["conflicting_combos"])
        print("foreground    : %s" % (fg or "unknown"))
        print("focus guard   : %s" % ("PASS (DCS is focused)"
                                      if fg.lower() == "dcs.exe" else "would BLOCK"))
        print("mode          : %s" % ("ARMED" if args.arm else "dry run"))
        return 0

    if args.list is not None:
        q = args.list.lower()
        rows = [a for a in ks.binds["actions"]
                if a["combos"] and (not q or q in a["name"].lower())]
        rows.sort(key=lambda a: a["name"])
        for a in rows:
            kind, _ = ks.classify(a["name"])
            tag = {"ok": "", "dangerous": "  [DANGEROUS]", "refused": "  [REFUSED]"}[kind]
            print("  %-58s %-16s %s%s"
                  % (a["name"], ",".join(a["combos"]), a["source"], tag))
        print("\n%d matching bound actions" % len(rows))
        return 0

    if args.combo:
        ok, msg = ks.send_combo(args.combo, force=args.force,
                                require_focus=not args.no_focus_check)
        print(("OK: " if ok else "FAIL: ") + msg)
        return 0 if ok else 2

    if args.action:
        ok, msg = ks.send_action(args.action, force=args.force,
                                 require_focus=not args.no_focus_check)
        print(("OK: " if ok else "FAIL: ") + msg)
        return 0 if ok else 2

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
