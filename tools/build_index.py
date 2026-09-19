#!/usr/bin/env python3
"""
Merge DCS module default binds with the user's diff into one authoritative index.

  defaults.json + userdiff.json  ->  binds.json

The merge is by ACTION NAME, because the user's diff records a name for every
entry. That avoids having to recompute DCS's encoded command ids, which are not
resolvable for engine-level commands anyway.

Effective combos = (module defaults - user 'removed') + user 'added'

Also emits a combo -> actions reverse map and a CONFLICT list. Conflicts matter:
if one keystroke drives two actions, sending it does two things.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

# Canonical modifier ordering so "LCtrl+LShift+A" is always spelled the same way.
MOD_ORDER = ["LCtrl", "RCtrl", "LShift", "RShift", "LAlt", "RAlt", "LWin", "RWin"]
MOD_RANK = {m: i for i, m in enumerate(MOD_ORDER)}


def combo_str(key: str, reformers) -> str:
    mods = sorted([str(r) for r in (reformers or [])],
                  key=lambda m: MOD_RANK.get(m, 99))
    return "+".join(mods + [str(key)])


def parse_combos(entries):
    """Accept the shape used by BOTH defaults.json and the raw diff."""
    out = []
    if not entries:
        return out
    if isinstance(entries, dict):          # lua 1-based table -> dict of "1","2"
        entries = [entries[k] for k in sorted(entries, key=lambda x: str(x))]
    for e in entries:
        if not isinstance(e, dict):
            continue
        key = e.get("key")
        if not key:
            continue
        refs = e.get("reformers") or []
        if isinstance(refs, dict):
            refs = [refs[k] for k in sorted(refs, key=lambda x: str(x))]
        out.append(combo_str(key, refs))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--binds-dir", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "binds"))
    ap.add_argument("--aircraft", default="FA-18C_hornet")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    d_path = os.path.join(args.binds_dir, "defaults.json")
    u_path = os.path.join(args.binds_dir, "userdiff.json")
    out_path = args.out or os.path.join(args.binds_dir, "binds.json")

    for p in (d_path, u_path):
        if not os.path.exists(p):
            print("missing %s -- run dump_binds.lua first" % p, file=sys.stderr)
            return 1

    with open(d_path, encoding="utf-8") as fh:
        defaults = json.load(fh)
    with open(u_path, encoding="utf-8") as fh:
        userdiff = json.load(fh)

    # ---- defaults, keyed by name -------------------------------------------
    actions = {}
    dup_names = []
    for a in defaults.get("actions", []):
        name = a.get("name")
        if not name:
            continue
        combos = parse_combos(a.get("combos"))
        if name in actions:
            dup_names.append(name)
            actions[name]["default_combos"].extend(
                c for c in combos if c not in actions[name]["default_combos"])
            continue
        actions[name] = {
            "name": name,
            "category": a.get("category") or [],
            "default_combos": combos,
            "added": [],
            "removed": [],
        }

    # ---- apply the user's diff ---------------------------------------------
    key_diffs = (userdiff.get("diff") or {}).get("keyDiffs") or {}
    user_only = []
    for _cmd_id, entry in key_diffs.items():
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not name:
            continue
        added = parse_combos(entry.get("added"))
        removed = parse_combos(entry.get("removed"))
        if name not in actions:
            actions[name] = {"name": name, "category": ["(user)"],
                             "default_combos": [], "added": [], "removed": []}
            user_only.append(name)
        actions[name]["added"].extend(added)
        actions[name]["removed"].extend(removed)

    # ---- effective combos --------------------------------------------------
    out_actions = []
    for name, a in actions.items():
        eff = [c for c in a["default_combos"] if c not in a["removed"]]
        for c in a["added"]:
            if c not in eff:
                eff.append(c)
        if a["added"] and a["default_combos"]:
            source = "default+user"
        elif a["added"]:
            source = "user"
        elif a["default_combos"]:
            source = "default"
        else:
            source = "unbound"
        out_actions.append({
            "name": name,
            "category": a["category"],
            "combos": eff,
            "source": source,
            "default_combos": a["default_combos"],
            "user_added": a["added"],
            "user_removed": a["removed"],
        })
    out_actions.sort(key=lambda x: x["name"])

    # ---- reverse map + conflicts -------------------------------------------
    by_combo = {}
    for a in out_actions:
        for c in a["combos"]:
            by_combo.setdefault(c, []).append(a["name"])
    conflicts = {c: names for c, names in by_combo.items() if len(names) > 1}

    bound = [a for a in out_actions if a["combos"]]
    doc = {
        "schema": "dcs-copilot/binds/1",
        "aircraft": args.aircraft,
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": {"defaults": defaults.get("source"),
                    "user_diff": userdiff.get("source")},
        "counts": {
            "actions_total": len(out_actions),
            "actions_bound": len(bound),
            "user_customised": sum(1 for a in out_actions if a["user_added"]),
            "conflicting_combos": len(conflicts),
        },
        "conflicts": conflicts,
        "by_combo": by_combo,
        "actions": out_actions,
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)

    print("wrote %s" % out_path)
    print("  actions total    : %d" % len(out_actions))
    print("  actions bound    : %d" % len(bound))
    print("  user customised  : %d" % doc["counts"]["user_customised"])
    print("  duplicate names  : %d" % len(set(dup_names)))
    print("  user-only actions: %d" % len(user_only))
    print("  CONFLICTING combos: %d" % len(conflicts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
