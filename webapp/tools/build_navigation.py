"""Read installed source files and create versioned caches; no DCS writes."""
from __future__ import annotations
import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from webapp.backend.navigation import build_terrain_cache, TERRAINS, CACHE_DIR, SCHEMA, default_community_dir


def backed_up_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        shutil.copy2(path, path.with_name(path.name + ".bak-" + stamp))
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def build(args):
    manifest = {"schema": SCHEMA, "terrains": []}
    for terrain in TERRAINS:
        if not (args.dcs_root / "Mods/terrains" / terrain).exists():
            continue
        cache = build_terrain_cache(args.dcs_root, terrain, community_dir=args.community_dir)
        relative = Path(cache["dcs_version"]) / f"{terrain}.json"
        backed_up_write(args.output / relative, cache)
        manifest["terrains"].append({"terrain": terrain, "dcs_version": cache["dcs_version"], "path": relative.as_posix(), "coverage": cache["provenance"]["coverage"]})
        print(terrain, json.dumps(cache["provenance"]["coverage"]))
    backed_up_write(args.output / "manifest.json", manifest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dcs-root", type=Path)
    parser.add_argument("--output", type=Path, default=CACHE_DIR)
    parser.add_argument("--community-dir", type=Path, default=default_community_dir())
    args = parser.parse_args()
    if args.dcs_root is None:
        from webapp.backend.config import Settings
        from webapp.backend.setup import SetupManager
        setup = SetupManager(Settings.from_env()).snapshot()
        selected = setup.get("selected", {}).get("install_path")
        if not selected:
            parser.error("No unique DCS installation. Open Your setup or pass --dcs-root.")
        args.dcs_root = Path(selected)
    from webapp.backend.smart import generation_lock
    with generation_lock(ROOT / "webapp/runtime"):
        build(args)


if __name__ == "__main__":
    main()
